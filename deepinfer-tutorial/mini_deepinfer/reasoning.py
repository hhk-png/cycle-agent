"""Reasoning-model serving helpers.

Long chain-of-thought changes the shape of a request: most output tokens may be
invisible reasoning, the decode phase runs much longer, and the KV footprint
grows with tokens the user never sees.  The helpers here cover three pieces:

* a budget that separates reasoning tokens from answer tokens;
* a stream parser that turns one token stream into ``reasoning`` and
  ``content`` channels, including markers split across chunks;
* a first-order capacity and cost model for reasoning-heavy traffic.

They are deliberately model-agnostic: the engine does not need to know how a
particular model marks its reasoning, only how to account for and expose it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReasoningBudget:
    """Hard limits for one request.

    ``max_reasoning_tokens`` bounds the invisible part; ``max_answer_tokens``
    bounds the visible part.  ``min_answer_tokens`` reserves room so that a
    model cannot spend the entire budget thinking and return an empty answer.
    """

    max_reasoning_tokens: int = 4096
    max_answer_tokens: int = 1024
    min_answer_tokens: int = 64

    def __post_init__(self) -> None:
        if self.max_reasoning_tokens < 0:
            raise ValueError("max_reasoning_tokens must be non-negative")
        if self.max_answer_tokens < 1:
            raise ValueError("max_answer_tokens must be positive")
        if not 0 <= self.min_answer_tokens <= self.max_answer_tokens:
            raise ValueError("min_answer_tokens must be in [0, max_answer_tokens]")

    @property
    def total_tokens(self) -> int:
        return self.max_reasoning_tokens + self.max_answer_tokens

    def answer_reserve_ratio(self) -> float:
        return self.min_answer_tokens / self.max_answer_tokens


@dataclass
class ReasoningTracker:
    budget: ReasoningBudget
    reasoning_tokens: int = 0
    answer_tokens: int = 0
    budget_exceeded: bool = False
    answer_exhausted: bool = False

    def add_reasoning(self, count: int = 1) -> bool:
        if count < 0:
            raise ValueError("count must be non-negative")
        self.reasoning_tokens += count
        if self.reasoning_tokens > self.budget.max_reasoning_tokens:
            self.budget_exceeded = True
            return False
        return True

    def add_answer(self, count: int = 1) -> bool:
        if count < 0:
            raise ValueError("count must be non-negative")
        self.answer_tokens += count
        if self.answer_tokens > self.budget.max_answer_tokens:
            self.answer_exhausted = True
            return False
        return True

    @property
    def reasoning_remaining(self) -> int:
        return max(0, self.budget.max_reasoning_tokens - self.reasoning_tokens)

    @property
    def answer_remaining(self) -> int:
        return max(0, self.budget.max_answer_tokens - self.answer_tokens)

    @property
    def should_force_answer(self) -> bool:
        """True when the answer reserve is at risk."""
        return self.reasoning_remaining <= 0 or (
            self.answer_remaining < self.budget.min_answer_tokens
            and self.reasoning_tokens >= self.budget.max_reasoning_tokens
        )

    @property
    def visible_tokens(self) -> int:
        return self.answer_tokens

    @property
    def total_tokens(self) -> int:
        return self.reasoning_tokens + self.answer_tokens

    def summary(self) -> dict[str, float | int | bool]:
        return {
            "reasoning_tokens": self.reasoning_tokens,
            "answer_tokens": self.answer_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_remaining": self.reasoning_remaining,
            "answer_remaining": self.answer_remaining,
            "budget_exceeded": self.budget_exceeded,
            "answer_exhausted": self.answer_exhausted,
            "should_force_answer": self.should_force_answer,
        }


@dataclass
class ThinkingStreamParser:
    """Split a token stream into reasoning and visible content.

    The parser keeps a small tail buffer so a marker split across two chunks is
    still recognised.  ``feed`` returns a list of ``(channel, text)`` events;
    the caller decides whether to forward reasoning to the client, store it, or
    discard it.
    """

    open_marker: str = "<thinking>"
    close_marker: str = "</thinking>"
    in_reasoning: bool = False
    _buffer: str = ""
    _started: bool = False

    def __post_init__(self) -> None:
        if not self.open_marker or not self.close_marker:
            raise ValueError("markers must not be empty")
        if self.open_marker == self.close_marker:
            raise ValueError("markers must differ")

    def feed(self, delta: str) -> list[tuple[str, str]]:
        if not delta:
            return []
        self._buffer += delta
        events: list[tuple[str, str]] = []
        while self._buffer:
            marker = self.close_marker if self.in_reasoning else self.open_marker
            position = self._buffer.find(marker)
            if position >= 0:
                if position:
                    events.append(("reasoning" if self.in_reasoning else "content", self._buffer[:position]))
                self._buffer = self._buffer[position + len(marker) :]
                self.in_reasoning = not self.in_reasoning
                self._started = True
                continue
            # No complete marker: emit everything except a tail that could be
            # the beginning of a marker on the next chunk.
            keep = max(0, len(marker) - 1)
            if len(self._buffer) > keep:
                emit = self._buffer[: len(self._buffer) - keep]
                self._buffer = self._buffer[len(self._buffer) - keep :]
                events.append(("reasoning" if self.in_reasoning else "content", emit))
            break
        return events

    def finish(self) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        if self._buffer:
            events.append(("reasoning" if self.in_reasoning else "content", self._buffer))
            self._buffer = ""
        return events

    @property
    def reasoning_started(self) -> bool:
        return self._started

    def reset(self) -> None:
        self.in_reasoning = False
        self._buffer = ""
        self._started = False


@dataclass
class ReasoningCapacity:
    output_tokens_per_request: float
    baseline_output_tokens: float
    latency_s: float
    concurrency: float
    kv_bytes: float
    decode_gpus: float
    decode_gpus_with_headroom: float
    cost_per_request: float
    baseline_cost_per_request: float

    @property
    def token_multiplier(self) -> float:
        return self.output_tokens_per_request / max(1.0, self.baseline_output_tokens)

    @property
    def cost_multiplier(self) -> float:
        return self.cost_per_request / max(1e-12, self.baseline_cost_per_request)

    def as_dict(self) -> dict[str, float]:
        return {
            "output_tokens_per_request": self.output_tokens_per_request,
            "latency_s": self.latency_s,
            "concurrency": self.concurrency,
            "kv_bytes": self.kv_bytes,
            "decode_gpus": self.decode_gpus,
            "decode_gpus_with_headroom": self.decode_gpus_with_headroom,
            "cost_per_request": self.cost_per_request,
            "baseline_cost_per_request": self.baseline_cost_per_request,
        }


def plan_reasoning_capacity(
    *,
    qps: float,
    prompt_tokens: float,
    reasoning_tokens: float,
    answer_tokens: float,
    bytes_per_token: float = 890.0,
    aggregate_decode_tokens_per_s_per_gpu: float = 3000.0,
    per_user_decode_tokens_per_s: float = 100.0,
    ttft_s: float = 0.4,
    gpu_hour_cost: float = 5.57,
    headroom: float = 1.3,
) -> ReasoningCapacity:
    """Estimate latency, KV, GPU count and cost for reasoning traffic.

    The baseline is the same request with the reasoning tokens removed; the
    difference is the price of thinking.  All inputs are averages; use p95
    values for a tail-latency estimate.
    """
    if qps < 0:
        raise ValueError("qps must be non-negative")
    if min(prompt_tokens, reasoning_tokens, answer_tokens) < 0:
        raise ValueError("token counts must be non-negative")
    if aggregate_decode_tokens_per_s_per_gpu <= 0 or per_user_decode_tokens_per_s <= 0:
        raise ValueError("throughput values must be positive")
    if headroom < 1.0:
        raise ValueError("headroom must be at least 1")

    output = reasoning_tokens + answer_tokens
    latency = ttft_s + output / per_user_decode_tokens_per_s
    concurrency = qps * latency
    kv_bytes = concurrency * (prompt_tokens + output) * bytes_per_token
    decode_gpus = qps * output / aggregate_decode_tokens_per_s_per_gpu
    decode_gpus_headroom = decode_gpus * headroom
    decode_gpu_seconds = output / aggregate_decode_tokens_per_s_per_gpu
    cost_per_request = decode_gpu_seconds * gpu_hour_cost / 3600.0
    baseline_output = answer_tokens
    baseline_gpu_seconds = baseline_output / aggregate_decode_tokens_per_s_per_gpu
    baseline_cost = baseline_gpu_seconds * gpu_hour_cost / 3600.0
    return ReasoningCapacity(
        output_tokens_per_request=output,
        baseline_output_tokens=baseline_output,
        latency_s=latency,
        concurrency=concurrency,
        kv_bytes=kv_bytes,
        decode_gpus=decode_gpus,
        decode_gpus_with_headroom=decode_gpus_headroom,
        cost_per_request=cost_per_request,
        baseline_cost_per_request=baseline_cost,
    )


def reasoning_cost_multiplier(
    *,
    reasoning_tokens: float,
    answer_tokens: float,
    kv_growth_weight: float = 0.25,
) -> float:
    """A simple cost multiplier relative to an answer-only request.

    Decode cost scales with output tokens; KV cost scales with total context
    and therefore also with reasoning tokens, with a smaller weight.
    """
    if reasoning_tokens < 0 or answer_tokens <= 0:
        raise ValueError("token counts are invalid")
    if kv_growth_weight < 0:
        raise ValueError("kv_growth_weight must be non-negative")
    decode_ratio = (reasoning_tokens + answer_tokens) / answer_tokens
    kv_ratio = 1.0 + kv_growth_weight * (reasoning_tokens / answer_tokens)
    return decode_ratio * kv_ratio
