"""Lightweight metrics collection and Prometheus-style exposition."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np


@dataclass
class EngineMetrics:
    started_at: float = field(default_factory=time.perf_counter)
    requests_added: int = 0
    requests_finished: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    prefill_tokens: int = 0
    decode_tokens: int = 0
    steps: int = 0
    speculative_steps: int = 0
    speculative_accepted: int = 0
    speculative_verified: int = 0
    preemptions: int = 0
    ttft_s: list[float] = field(default_factory=list)
    tpot_s: list[float] = field(default_factory=list)
    e2e_s: list[float] = field(default_factory=list)
    last_step_time: float | None = None
    last_decode_tokens: int = 0
    last_decode_step_time: float | None = None

    def record_request(self, prompt_tokens: int) -> None:
        self.requests_added += 1
        self.prompt_tokens += prompt_tokens

    def record_step(self, *, prefill_tokens: int, decode_tokens: int, elapsed: float) -> None:
        self.steps += 1
        self.prefill_tokens += prefill_tokens
        self.decode_tokens += decode_tokens
        self.last_step_time = elapsed
        # Keep the decode token count and its elapsed time paired.  A prefill
        # step must not reuse the previous decode token count with its own
        # (much longer) elapsed time, which would report a fake regression.
        if decode_tokens:
            self.last_decode_tokens = decode_tokens
            self.last_decode_step_time = elapsed

    def record_finished(self, seq) -> None:
        self.requests_finished += 1
        self.output_tokens += seq.num_output_tokens
        if seq.metrics.first_token_latency is not None:
            self.ttft_s.append(seq.metrics.first_token_latency)
        if seq.metrics.e2e_latency is not None:
            self.e2e_s.append(seq.metrics.e2e_latency)
            if seq.num_output_tokens > 1:
                # Time per output token after the first one.
                self.tpot_s.append(
                    (seq.metrics.e2e_latency - (seq.metrics.first_token_latency or 0.0))
                    / (seq.num_output_tokens - 1)
                )

    @property
    def elapsed_s(self) -> float:
        return max(1e-9, time.perf_counter() - self.started_at)

    def summary(self) -> dict[str, float | int]:
        def mean(values: list[float]) -> float:
            return float(np.mean(values)) if values else 0.0

        def percentile(values: list[float], q: float) -> float:
            return float(np.percentile(values, q)) if values else 0.0

        return {
            "requests_added": self.requests_added,
            "requests_finished": self.requests_finished,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "prefill_tokens": self.prefill_tokens,
            "decode_tokens": self.decode_tokens,
            "steps": self.steps,
            # Lifetime average: useful for totals, misleading for spotting a
            # sudden regression because old fast steps dominate the ratio.
            "output_tokens_per_s": self.output_tokens / self.elapsed_s,
            "last_decode_tokens_per_s": (
                self.last_decode_tokens / self.last_decode_step_time
                if self.last_decode_step_time and self.last_decode_tokens
                else 0.0
            ),
            "ttft_mean_ms": mean(self.ttft_s) * 1000,
            "ttft_p95_ms": percentile(self.ttft_s, 95) * 1000,
            "tpot_mean_ms": mean(self.tpot_s) * 1000,
            "e2e_mean_ms": mean(self.e2e_s) * 1000,
            "speculative_steps": self.speculative_steps,
            "speculative_acceptance": (
                self.speculative_accepted / self.speculative_verified
                if self.speculative_verified
                else 0.0
            ),
            "preemptions": self.preemptions,
        }

    def prometheus(self) -> str:
        summary = self.summary()
        lines = []
        for key, value in summary.items():
            metric = f"mini_deepinfer_{key}"
            lines.append(f"# TYPE {metric} gauge")
            lines.append(f"{metric} {value}")
        return "\n".join(lines) + "\n"
