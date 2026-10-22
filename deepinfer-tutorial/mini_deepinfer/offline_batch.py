"""Offline batch serving: plan waves, run them, report throughput.

Offline inference optimises a different objective from online serving.  There
is no user waiting for the first token; what matters is makespan, GPU
utilisation, cost per token and whether a batch finishes before its deadline.
This module implements the small but real scheduling layer around the engine:

* collect requests with priority and deadline;
* pack them into waves under a batch-size and token budget;
* run each wave through the regular engine;
* report throughput, tail latency and deadline misses.

The packing is intentionally simple (priority/deadline order plus first-fit).
It is enough to expose the trade-offs that production batch schedulers optimise.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .llm import LLM
from .sampling_params import SamplingParams


@dataclass
class BatchItem:
    request_id: str
    prompt: str
    max_tokens: int = 32
    temperature: float = 0.0
    seed: int | None = 0
    priority: int = 0
    deadline_s: float | None = None
    tenant: str = "default"
    estimated_prompt_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if self.deadline_s is not None and self.deadline_s <= 0:
            raise ValueError("deadline_s must be positive")
        if self.estimated_prompt_tokens is not None and self.estimated_prompt_tokens < 1:
            raise ValueError("estimated_prompt_tokens must be positive")

    @property
    def prompt_tokens(self) -> int:
        # A byte-level proxy; the planner only needs relative sizes.
        return self.estimated_prompt_tokens or max(1, len(self.prompt.encode("utf-8")))


def default_cost(item: BatchItem) -> int:
    """Tokens the scheduler reserves for one request."""
    return item.prompt_tokens + item.max_tokens


@dataclass
class BatchWave:
    indices: list[int]
    estimated_tokens: int

    def __len__(self) -> int:
        return len(self.indices)


def plan_waves(
    items: list[BatchItem],
    *,
    max_batch_size: int,
    max_batched_tokens: int,
    cost_fn: Callable[[BatchItem], int] = default_cost,
) -> list[BatchWave]:
    """Pack requests into waves.

    Ordering is by priority (high first), then earliest deadline, then size.
    A request that alone exceeds the token budget still gets its own wave; the
    alternative would be to reject it, which is a policy decision the caller
    should make explicitly.
    """
    if max_batch_size < 1 or max_batched_tokens < 1:
        raise ValueError("batch limits must be positive")
    order = sorted(
        range(len(items)),
        key=lambda index: (
            -items[index].priority,
            math.inf if items[index].deadline_s is None else items[index].deadline_s,
            cost_fn(items[index]),
            index,
        ),
    )
    waves: list[BatchWave] = []
    current: list[int] = []
    current_cost = 0
    for index in order:
        cost = max(1, int(cost_fn(items[index])))
        if current and (
            len(current) >= max_batch_size or current_cost + cost > max_batched_tokens
        ):
            waves.append(BatchWave(current, current_cost))
            current = []
            current_cost = 0
        current.append(index)
        current_cost += cost
    if current:
        waves.append(BatchWave(current, current_cost))
    return waves


@dataclass
class BatchResult:
    request_id: str
    text: str
    finish_reason: str | None
    prompt_tokens: int
    output_tokens: int
    latency_s: float
    deadline_met: bool
    tenant: str = "default"


@dataclass
class BatchReport:
    results: list[BatchResult] = field(default_factory=list)
    waves: int = 0
    elapsed_s: float = 0.0
    total_prompt_tokens: int = 0
    total_output_tokens: int = 0

    @property
    def output_tokens_per_s(self) -> float:
        return self.total_output_tokens / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def requests_per_s(self) -> float:
        return len(self.results) / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def percentile(self, values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        position = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[position]

    @property
    def p50_latency_s(self) -> float:
        return self.percentile([result.latency_s for result in self.results], 0.50)

    @property
    def p95_latency_s(self) -> float:
        return self.percentile([result.latency_s for result in self.results], 0.95)

    @property
    def deadline_miss_rate(self) -> float:
        scheduled = [result for result in self.results if result.deadline_met is not None]
        if not scheduled:
            return 0.0
        misses = sum(1 for result in scheduled if not result.deadline_met)
        return misses / len(scheduled)

    @property
    def goodput_requests_per_s(self) -> float:
        if self.elapsed_s <= 0:
            return 0.0
        met = sum(1 for result in self.results if result.deadline_met)
        return met / self.elapsed_s

    def as_dict(self) -> dict[str, float | int]:
        return {
            "requests": len(self.results),
            "waves": self.waves,
            "elapsed_s": self.elapsed_s,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_output_tokens": self.total_output_tokens,
            "output_tokens_per_s": self.output_tokens_per_s,
            "requests_per_s": self.requests_per_s,
            "p50_latency_s": self.p50_latency_s,
            "p95_latency_s": self.p95_latency_s,
            "deadline_miss_rate": self.deadline_miss_rate,
            "goodput_requests_per_s": self.goodput_requests_per_s,
        }


def run_offline_batch(
    llm: LLM,
    items: list[BatchItem],
    *,
    max_batch_size: int = 8,
    max_batched_tokens: int = 512,
    cost_fn: Callable[[BatchItem], int] = default_cost,
    waves: list[BatchWave] | None = None,
) -> BatchReport:
    """Execute an offline batch and return an auditable report.

    The engine still does continuous batching inside each wave; the wave is the
    admission unit for the offline scheduler.  Running fewer, larger waves
    raises GPU utilisation but also raises the tail latency of the whole batch.
    """
    if not items:
        return BatchReport()
    scheduled = waves if waves is not None else plan_waves(
        items,
        max_batch_size=max_batch_size,
        max_batched_tokens=max_batched_tokens,
        cost_fn=cost_fn,
    )
    report = BatchReport(waves=len(scheduled))
    started = time.perf_counter()
    for wave in scheduled:
        wave_items = [items[index] for index in wave.indices]
        params = [
            SamplingParams(
                temperature=item.temperature,
                max_tokens=item.max_tokens,
                seed=item.seed,
            )
            for item in wave_items
        ]
        outputs = llm.generate(
            [item.prompt for item in wave_items],
            params,
            return_objects=True,
        )
        for item, output in zip(wave_items, outputs):
            latency = output.e2e_latency or 0.0
            deadline_met = (
                True if item.deadline_s is None else latency <= item.deadline_s
            )
            report.results.append(
                BatchResult(
                    request_id=item.request_id,
                    text=output.text,
                    finish_reason=output.finish_reason,
                    prompt_tokens=output.prompt_tokens,
                    output_tokens=output.output_tokens,
                    latency_s=latency,
                    deadline_met=deadline_met,
                    tenant=item.tenant,
                )
            )
            report.total_prompt_tokens += output.prompt_tokens
            report.total_output_tokens += output.output_tokens
    report.elapsed_s = time.perf_counter() - started
    return report
