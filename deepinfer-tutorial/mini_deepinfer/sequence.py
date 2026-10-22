"""Request state and output records."""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .sampling_params import SamplingParams


class SequenceStatus(enum.Enum):
    WAITING = "waiting"
    RUNNING = "running"
    FINISHED_STOPPED = "stopped"
    FINISHED_LENGTH_CAPPED = "length_capped"
    FINISHED_ABORTED = "aborted"
    FINISHED_PREEMPTED = "preempted"
    FINISHED_IGNORED = "ignored"
    FINISHED_SO_FAILED = "so_failed"

    @property
    def finished(self) -> bool:
        return self.name.startswith("FINISHED")


@dataclass
class RequestMetrics:
    arrival_time: float = field(default_factory=time.perf_counter)
    first_token_time: float | None = None
    finish_time: float | None = None
    first_token_latency: float | None = None
    e2e_latency: float | None = None
    token_times: list[float] = field(default_factory=list)

    def mark_first_token(self, now: float | None = None) -> None:
        if self.first_token_time is None:
            self.first_token_time = time.perf_counter() if now is None else now
            self.first_token_latency = self.first_token_time - self.arrival_time

    def mark_finished(self, now: float | None = None) -> None:
        if self.finish_time is None:
            self.finish_time = time.perf_counter() if now is None else now
            self.e2e_latency = self.finish_time - self.arrival_time

    def as_dict(self) -> dict[str, float | None]:
        return {
            "arrival_time": self.arrival_time,
            "first_token_latency": self.first_token_latency,
            "e2e_latency": self.e2e_latency,
        }


class Sequence:
    """One request: prompt, generated tokens, KV bookkeeping and status."""

    def __init__(
        self,
        seq_id: int,
        request_id: str,
        prompt_token_ids: list[int] | np.ndarray,
        sampling_params: SamplingParams,
        *,
        arrival_time: float | None = None,
        prefix_cache_hit: int = 0,
    ) -> None:
        self.seq_id = seq_id
        self.request_id = request_id
        self.prompt_token_ids = [int(x) for x in np.asarray(prompt_token_ids).reshape(-1)]
        if not self.prompt_token_ids:
            raise ValueError("prompt must not be empty")
        self.output_token_ids: list[int] = []
        self.output_logprobs: list[float] = []
        self.sampling_params = sampling_params
        self.status = SequenceStatus.WAITING

        # KV paging state.
        self.block_table: list[int] = []
        self.num_computed_tokens = 0
        self.prefix_cache_hit = prefix_cache_hit
        self.num_cached_tokens = prefix_cache_hit

        self.metrics = RequestMetrics(
            arrival_time=time.perf_counter() if arrival_time is None else arrival_time
        )
        self.finish_reason: str | None = None
        self.so_matcher: Any | None = None
        self.draft_token_ids: list[int] = []
        self.draft_confidence: list[float] = []
        self.extra: dict[str, Any] = {}
        self._decoded_cache: str | None = None
        self._decode_cache_len = 0
        self.last_token_logprob: float = 0.0
        self.top_logprobs: list[tuple[int, float]] = []
        self.rng = np.random.default_rng(sampling_params.effective_seed)
        self.draft_rng = np.random.default_rng(
            (sampling_params.effective_seed ^ 0x9E3779B97F4A7C15) & ((1 << 64) - 1)
        )

    # ------------------------------------------------------------------
    # Token accounting
    # ------------------------------------------------------------------
    @property
    def num_prompt_tokens(self) -> int:
        return len(self.prompt_token_ids)

    @property
    def num_output_tokens(self) -> int:
        return len(self.output_token_ids)

    @property
    def length(self) -> int:
        return self.num_prompt_tokens + self.num_output_tokens

    @property
    def num_uncomputed_tokens(self) -> int:
        return self.length - self.num_computed_tokens

    def all_token_ids(self) -> list[int]:
        return self.prompt_token_ids + self.output_token_ids

    def append_token(self, token_id: int, logprob: float = 0.0) -> None:
        self.output_token_ids.append(int(token_id))
        self.output_logprobs.append(float(logprob))
        self.last_token_logprob = float(logprob)
        self._decoded_cache = None

    def pop_last_token(self) -> int:
        if not self.output_token_ids:
            raise IndexError("no output token to pop")
        self._decoded_cache = None
        self.output_logprobs.pop()
        return self.output_token_ids.pop()

    def is_finished(self) -> bool:
        return self.status.finished

    # ------------------------------------------------------------------
    # Stopping
    # ------------------------------------------------------------------
    def check_stop(self, tokenizer, output_text: str | None = None) -> bool:
        sp = self.sampling_params
        # min_tokens delays natural stops (EOS and stop strings), but it must
        # not override the hard length caps: a request with
        # min_tokens > max_tokens still stops at max_tokens.
        if self.num_output_tokens >= sp.min_tokens:
            if not sp.ignore_eos and sp.eos_token_id is not None and self.output_token_ids[-1] == sp.eos_token_id:
                self.status = SequenceStatus.FINISHED_STOPPED
                self.finish_reason = "stop"
                return True
            if self.output_token_ids and self.output_token_ids[-1] in sp.stop_token_ids:
                self.status = SequenceStatus.FINISHED_STOPPED
                self.finish_reason = "stop"
                return True
            if sp.stop:
                if output_text is None:
                    output_text = tokenizer.decode(self.output_token_ids)
                if any(s and output_text.endswith(s) for s in sp.stop):
                    self.status = SequenceStatus.FINISHED_STOPPED
                    self.finish_reason = "stop"
                    return True
        if self.num_output_tokens >= sp.max_tokens:
            self.status = SequenceStatus.FINISHED_LENGTH_CAPPED
            self.finish_reason = "length"
            return True
        if self.length >= self.extra.get("max_model_len", 1 << 60):
            self.status = SequenceStatus.FINISHED_LENGTH_CAPPED
            self.finish_reason = "length"
            return True
        return False

    # ------------------------------------------------------------------
    # Preemption and speculation
    # ------------------------------------------------------------------
    def reset_for_recompute(self) -> None:
        """Drop KV and recompute prompt + generated tokens from scratch."""
        self.block_table = []
        self.num_computed_tokens = 0
        self.status = SequenceStatus.WAITING
        # Keep the original TTFT metric: recomputation does not change when
        # the user first saw a token.
        self.prefix_cache_hit = 0
        self.num_cached_tokens = 0
        self.extra.pop("prefix_registered_blocks", None)

    def snapshot(self) -> dict[str, Any]:
        """Capture state that speculative decoding may need to roll back."""
        return {
            "num_output": len(self.output_token_ids),
            "num_computed": self.num_computed_tokens,
            "status": self.status,
            "last_logprob": self.last_token_logprob,
            "so_state": self.so_matcher.snapshot() if self.so_matcher is not None else None,
        }

    def restore(self, snap: dict[str, Any]) -> None:
        del self.output_token_ids[snap["num_output"] :]
        del self.output_logprobs[snap["num_output"] :]
        self.num_computed_tokens = snap["num_computed"]
        self.status = snap["status"]
        self.last_token_logprob = snap["last_logprob"]
        self._decoded_cache = None
        if self.so_matcher is not None and snap["so_state"] is not None:
            self.so_matcher.restore(snap["so_state"])

    def decoded_output(self, tokenizer) -> str:
        if self._decoded_cache is None or self._decode_cache_len != len(self.output_token_ids):
            self._decoded_cache = tokenizer.decode(self.output_token_ids)
            self._decode_cache_len = len(self.output_token_ids)
        return self._decoded_cache


@dataclass
class RequestOutput:
    request_id: str
    seq_id: int
    token_ids: list[int]
    text: str
    finished: bool
    finish_reason: str | None
    prompt_tokens: int
    output_tokens: int
    metrics: RequestMetrics
    logprobs: list[float] = field(default_factory=list)
    prefix_cache_hit: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def first_token_latency(self) -> float | None:
        return self.metrics.first_token_latency

    @property
    def e2e_latency(self) -> float | None:
        return self.metrics.e2e_latency
