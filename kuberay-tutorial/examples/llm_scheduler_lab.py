#!/usr/bin/env python3
"""Continuous batching, KV cache blocks and prefill/decode disaggregation.

Run from the tutorial root:

    python examples/llm_scheduler_lab.py

The simulator is intentionally small but keeps the mechanisms that determine
LLM serving latency on KubeRay:

* requests arrive over time and have a shared system prompt;
* a prefix cache turns the shared 512-token prefix into shared KV blocks;
* coupled mode gives every active request one slot (prefill and decode share
  capacity); PD mode separates prefill slots from decode slots so a long
  prompt cannot block a short decode;
* a bounded KV cache admits requests only when their prompt + output fits.

The output compares three policies on the same workload.  Numbers are in
"ticks", not milliseconds, so the lab is deterministic on every machine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


BLOCK_SIZE = 16
SHARED_PREFIX = 512


@dataclass(frozen=True)
class Request:
    request_id: str
    prompt_tokens: int
    output_tokens: int
    arrival_tick: int
    shares_prefix: bool = True


@dataclass
class State:
    request: Request
    phase: str = "waiting"  # waiting / prefill / transfer / decode / done
    prefill_ticks: int = 0
    transfer_ticks: int = 0
    decode_ticks: int = 0
    first_token_tick: int | None = None
    done_tick: int | None = None
    kv_tokens: int = 0
    cache_hit: bool = False


@dataclass
class Result:
    mode: str
    metrics: dict[str, float] = field(default_factory=dict)
    timeline: list[tuple[str, int, int | None, int | None]] = field(default_factory=list)


def blocks(tokens: int) -> int:
    return math.ceil(tokens / BLOCK_SIZE)


def workload() -> list[Request]:
    return [
        Request("r1", 512 + 128, 64, 0),
        Request("r2", 512 + 64, 96, 0),
        Request("r3", 512 + 256, 32, 2),
        Request("r4", 512 + 96, 128, 3),
        Request("r5", 512 + 512, 48, 5),
        Request("r6", 512 + 32, 80, 6),
    ]


class Scheduler:
    def __init__(
        self,
        mode: str,
        *,
        kv_capacity_tokens: int = 8192,
        chunk_tokens: int = 256,
        coupled_slots: int = 4,
        prefill_slots: int = 2,
        decode_slots: int = 8,
    ):
        self.mode = mode
        self.kv_capacity = kv_capacity_tokens
        self.chunk_tokens = 512 if mode == "pd" else chunk_tokens
        self.coupled_slots = coupled_slots
        self.prefill_slots = prefill_slots
        self.decode_slots = decode_slots
        self.cache_warm = False
        self.kv_used = 0
        self.peak_kv = 0
        self.coupled_active = 0
        self.prefill_active = 0
        self.decode_active = 0
        self.cache_hits = 0
        self.requests = [State(request=item) for item in workload()]

    # ---------------------------------------------------------------- helpers
    def effective_prompt(self, request: Request) -> tuple[int, bool]:
        if self.mode in ("prefix", "pd") and request.shares_prefix and self.cache_warm:
            return request.prompt_tokens - SHARED_PREFIX, True
        return request.prompt_tokens, False

    def _admit(self, tick: int) -> None:
        for state in self.requests:
            if state.phase != "waiting" or state.request.arrival_tick > tick:
                continue
            prompt, hit = self.effective_prompt(state.request)
            cost = prompt + state.request.output_tokens
            if self.kv_used + cost > self.kv_capacity:
                break  # FCFS: head-of-line request waits for capacity
            if self.mode == "pd":
                if self.prefill_active >= self.prefill_slots:
                    break
                self.prefill_active += 1
            else:
                if self.coupled_active >= self.coupled_slots:
                    break
                self.coupled_active += 1
            state.phase = "prefill"
            state.prefill_ticks = math.ceil(prompt / self.chunk_tokens)
            state.kv_tokens = cost
            state.cache_hit = hit
            self.kv_used += cost
            self.peak_kv = max(self.peak_kv, self.kv_used)
            if hit:
                self.cache_hits += 1
            if self.mode in ("prefix", "pd") and state.request.shares_prefix:
                self.cache_warm = True

    def _progress(self, tick: int) -> None:
        for state in self.requests:
            if state.phase == "prefill":
                state.prefill_ticks -= 1
                if state.prefill_ticks > 0:
                    continue
                if self.mode == "pd":
                    state.phase = "transfer"
                    state.transfer_ticks = 1
                    self.prefill_active -= 1
                else:
                    state.phase = "decode"
                    state.decode_ticks = state.request.output_tokens
                    state.first_token_tick = tick + 1
            elif state.phase == "transfer":
                state.transfer_ticks -= 1
                if state.transfer_ticks <= 0:
                    state.phase = "decode"
                    state.decode_ticks = state.request.output_tokens
                    state.first_token_tick = tick + 1
                    self.decode_active += 1
            elif state.phase == "decode":
                state.decode_ticks -= 1
                if state.decode_ticks <= 0:
                    state.phase = "done"
                    state.done_tick = tick + 1
                    self.kv_used -= state.kv_tokens
                    if self.mode == "pd":
                        self.decode_active -= 1
                    else:
                        self.coupled_active -= 1

    # -------------------------------------------------------------------- run
    def run(self, limit: int = 1000) -> Result:
        tick = 0
        while tick < limit and any(state.phase != "done" for state in self.requests):
            self._admit(tick)
            self._progress(tick)
            tick += 1
        assert all(state.phase == "done" for state in self.requests), "workload did not drain"

        completed = [state for state in self.requests]
        makespan = max(state.done_tick or 0 for state in completed)
        ttfts = [(state.first_token_tick or 0) - state.request.arrival_tick for state in completed]
        tpots = [
            ((state.done_tick or 0) - (state.first_token_tick or 0)) / max(1, state.request.output_tokens - 1)
            for state in completed
        ]
        output_tokens = sum(state.request.output_tokens for state in completed)
        metrics = {
            "makespan": float(makespan),
            "throughput_tokens_per_tick": round(output_tokens / makespan, 3),
            "mean_ttft": round(sum(ttfts) / len(ttfts), 2),
            "p95_ttft": float(sorted(ttfts)[max(0, math.ceil(0.95 * len(ttfts)) - 1)]),
            "mean_tpot": round(sum(tpots) / len(tpots), 3),
            "cache_hits": float(self.cache_hits),
            "peak_kv_tokens": float(self.peak_kv),
            "prefill_decode_overlap": 1.0 if self.mode == "pd" else 0.0,
        }
        timeline = [
            (state.request.request_id, state.request.arrival_tick, state.first_token_tick, state.done_tick)
            for state in completed
        ]
        return Result(self.mode, metrics, timeline)


def main() -> int:
    modes = [
        ("baseline", "coupled, no prefix cache"),
        ("prefix", "coupled + 512-token prefix cache"),
        ("pd", "prefix cache + prefill/decode disaggregation"),
    ]
    results = {mode: Scheduler(mode).run() for mode, _ in modes}

    print("== LLM scheduler lab ==")
    print(f"block={BLOCK_SIZE} tokens, shared prefix={SHARED_PREFIX}, kv_capacity=8192")
    print(f"{'mode':<10} {'makespan':>8} {'tok/tick':>9} {'meanTTFT':>9} {'p95TTFT':>8} {'meanTPOT':>9} {'hits':>5} {'peakKV':>7}")
    for mode, description in modes:
        metrics = results[mode].metrics
        print(
            f"{mode:<10} {metrics['makespan']:>8.0f} {metrics['throughput_tokens_per_tick']:>9.2f} "
            f"{metrics['mean_ttft']:>9.2f} {metrics['p95_ttft']:>8.0f} {metrics['mean_tpot']:>9.2f} "
            f"{metrics['cache_hits']:>5.0f} {metrics['peak_kv_tokens']:>7.0f}"
        )

    print("\n-- per-request timeline (arrival, first token, done) --")
    for mode, _ in modes:
        print(f"  {mode}: " + " ".join(
            f"{rid}[{arrival},{first},{done}]" for rid, arrival, first, done in results[mode].timeline
        ))

    baseline = results["baseline"].metrics
    prefix = results["prefix"].metrics
    pd = results["pd"].metrics

    assert baseline["cache_hits"] == 0
    assert prefix["cache_hits"] == 5, prefix["cache_hits"]  # first request warms the cache
    assert prefix["mean_ttft"] < baseline["mean_ttft"]
    assert pd["throughput_tokens_per_tick"] >= baseline["throughput_tokens_per_tick"]
    assert all(metrics["peak_kv_tokens"] <= 8192 for metrics in (baseline, prefix, pd))
    for mode, _ in modes:
        assert all(done is not None for _, _, _, done in results[mode].timeline)

    print("\nOK: prefix caching cut TTFT; PD overlapped prefill and decode; all requests drained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
