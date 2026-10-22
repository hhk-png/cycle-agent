"""Sampling parameters, following the shape of DeepInfer's ``SamplingParams``."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .errors import ConfigError

SAMPLING_EPS = 1e-5
_UINT64_MASK = (1 << 64) - 1


def _new_seed() -> int:
    return random.SystemRandom().getrandbits(64)


@dataclass
class SamplingParams:
    temperature: float = 1.0
    top_p: float = 1.0
    top_k: int = -1
    max_tokens: int = 32
    min_tokens: int = 0
    ignore_eos: bool = False
    eos_token_id: int | None = None
    stop_token_ids: tuple[int, ...] = ()
    stop: tuple[str, ...] = ()
    seed: int | None = None
    logprobs: bool = False
    top_logprobs: int = 0
    so_args: dict | None = None
    # Trigger/bound behaviour mirrors DeepInfer's structured-output extension:
    # generation is free until the trigger token sequence appears.
    so_trigger_token_ids: tuple[int, ...] = ()
    so_bound_token_id: int | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.temperature < 0:
            raise ConfigError("temperature must be non-negative")
        if not 0.0 < self.top_p <= 1.0:
            raise ConfigError("top_p must be in (0, 1]")
        if self.top_k < -1 or self.top_k == 0:
            raise ConfigError("top_k must be -1 or a positive integer")
        if self.max_tokens < 1:
            raise ConfigError("max_tokens must be >= 1")
        if self.min_tokens < 0:
            raise ConfigError("min_tokens must be >= 0")
        if self.top_logprobs < 0:
            raise ConfigError("top_logprobs must be >= 0")
        if self.seed is not None and not 0 <= self.seed <= _UINT64_MASK:
            raise ConfigError("seed must fit in uint64")
        if self.temperature < SAMPLING_EPS:
            # Match DeepInfer/vLLM: temperature 0 means greedy and top-p/top-k
            # are disabled rather than left in a contradictory state.
            self.temperature = SAMPLING_EPS
            self.top_p = 1.0
            self.top_k = -1
        if self.seed is None:
            self.seed = _new_seed()
        self.effective_seed = int(self.seed) & _UINT64_MASK

    @property
    def greedy(self) -> bool:
        return self.temperature <= SAMPLING_EPS + 1e-12

    def copy(self) -> SamplingParams:
        return SamplingParams(
            temperature=self.temperature,
            top_p=self.top_p,
            top_k=self.top_k,
            max_tokens=self.max_tokens,
            min_tokens=self.min_tokens,
            ignore_eos=self.ignore_eos,
            eos_token_id=self.eos_token_id,
            stop_token_ids=tuple(self.stop_token_ids),
            stop=tuple(self.stop),
            seed=self.seed,
            logprobs=self.logprobs,
            top_logprobs=self.top_logprobs,
            so_args=dict(self.so_args) if self.so_args else None,
            so_trigger_token_ids=tuple(self.so_trigger_token_ids),
            so_bound_token_id=self.so_bound_token_id,
            extra=dict(self.extra),
        )
