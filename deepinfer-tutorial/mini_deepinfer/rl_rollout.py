"""RL rollout planning for post-training inference.

RL post-training (RLHF, GRPO, verifier-based RL) turns the inference engine
into a *sampler for a trainer*.  The objective is different from online
serving: one prompt produces a group of completions, the trainer needs the
behaviour-policy log-probabilities, weights change every few steps, and a
rollout may be suspended and resumed.  This module keeps the parts that an
engine has to get right:

* group planning and shared-prefix accounting;
* group-relative advantages (GRPO) with a stable zero-variance path;
* a cost model that separates prefill, decode and KV traffic;
* weight-version and KV-namespace rules, so a policy update cannot silently
  reuse KV computed by the previous policy;
* partial-rollout state and staleness checks.

It is deliberately independent of the serving engine.  A production rollout
system can reuse the same invariants while plugging in a real trainer, a
reward model, and a distributed weight-sync path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Sequence

import numpy as np


@dataclass(frozen=True)
class RolloutConfig:
    """Policy and scheduling knobs for one rollout worker."""

    group_size: int = 8
    max_new_tokens: int = 1024
    max_prompt_tokens: int = 8192
    temperature: float = 1.0
    top_p: float = 1.0
    partial_rollout: bool = True
    max_staleness_steps: int = 1

    def __post_init__(self) -> None:
        if self.group_size < 1:
            raise ValueError("group_size must be positive")
        if self.max_new_tokens < 1:
            raise ValueError("max_new_tokens must be positive")
        if self.max_prompt_tokens < 1:
            raise ValueError("max_prompt_tokens must be positive")
        if not 0.0 < self.temperature <= 10.0:
            raise ValueError("temperature must be in (0, 10]")
        if not 0.0 < self.top_p <= 1.0:
            raise ValueError("top_p must be in (0, 1]")
        if self.max_staleness_steps < 0:
            raise ValueError("max_staleness_steps must be non-negative")


@dataclass(frozen=True)
class PromptGroup:
    """One prompt and the number of completions to sample from it."""

    prompt_id: str
    prompt_tokens: int
    group_size: int

    def __post_init__(self) -> None:
        if not self.prompt_id:
            raise ValueError("prompt_id must not be empty")
        if self.prompt_tokens < 1:
            raise ValueError("prompt_tokens must be positive")
        if self.group_size < 1:
            raise ValueError("group_size must be positive")



@dataclass(frozen=True)
class GroupPlan:
    """A group placed in a rollout wave."""

    prompt_id: str
    prompt_tokens: int
    group_size: int
    output_budget: int

    @property
    def shared_prefill_tokens(self) -> int:
        """Prefill is paid once per prompt when the group shares a prefix.

        The engine may still choose to prefill per sample; this field is the
        lower bound that group-aware scheduling should exploit.
        """
        return self.prompt_tokens

    @property
    def reserved_tokens(self) -> int:
        """KV/compute budget for the group (shared prompt + all outputs)."""
        return self.prompt_tokens + self.group_size * self.output_budget


def plan_groups(
    prompts: Sequence[tuple[str, int]],
    *,
    group_size: int = 8,
    output_budget: int = 1024,
    max_batch_tokens: int = 32_768,
) -> list[list[GroupPlan]]:
    """Pack prompt groups into waves under a token budget.

    A group is the unit of admission: splitting a group across waves would
    break the shared-prefix optimisation and make the advantage computation
    harder to reason about.  An oversized group gets its own wave rather than
    being silently truncated; the caller decides whether to reject it.
    """
    if group_size < 1 or output_budget < 1 or max_batch_tokens < 1:
        raise ValueError("group and token limits must be positive")
    plans = [
        GroupPlan(
            prompt_id=prompt_id,
            prompt_tokens=tokens,
            group_size=group_size,
            output_budget=output_budget,
        )
        for prompt_id, tokens in prompts
    ]
    waves: list[list[GroupPlan]] = []
    current: list[GroupPlan] = []
    current_tokens = 0
    # Longer groups first reduces fragmentation: a large group that does not
    # fit is more likely to stand alone either way, but packing it last would
    # waste the tail of many small waves.
    for plan in sorted(plans, key=lambda item: (-item.reserved_tokens, item.prompt_id)):
        if current and current_tokens + plan.reserved_tokens > max_batch_tokens:
            waves.append(current)
            current = []
            current_tokens = 0
        current.append(plan)
        current_tokens += plan.reserved_tokens
    if current:
        waves.append(current)
    return waves


def group_advantages(rewards: Iterable[float], *, eps: float = 1e-8) -> np.ndarray:
    """GRPO group-relative advantage: ``(r - mean) / std``.

    If every sample in the group receives the same reward, all advantages are
    zero.  That is intentional: a zero-variance group carries no learning
    signal, and dividing by ``std + eps`` avoids a NaN that would poison the
    trainer.  The caller should log such groups, because they are wasted
    rollouts.
    """
    values = np.asarray(list(rewards), dtype=np.float64)
    if values.ndim != 1 or values.size == 0:
        raise ValueError("rewards must be a non-empty 1-D sequence")
    if not np.all(np.isfinite(values)):
        raise ValueError("rewards must be finite")
    centered = values - values.mean()
    std = float(values.std())
    if std <= eps:
        return np.zeros_like(values)
    return centered / (std + eps)


@dataclass(frozen=True)
class RolloutCost:
    prefill_tokens: int
    decode_tokens: int
    kv_token_steps: int
    gpu_seconds: float
    cost_usd: float


def estimate_rollout_cost(
    *,
    group_size: int,
    prompt_tokens: int,
    output_tokens: int,
    prefill_tokens_per_s: float,
    decode_tokens_per_s: float,
    kv_bytes_per_token: float,
    kv_bandwidth_gbs: float,
    gpu_hour_cost: float,
    share_prefix: bool = True,
) -> RolloutCost:
    """First-order cost of one prompt group.

    Prefill is charged once when ``share_prefix`` is true; decode is always
    charged per sample.  KV traffic is counted as token-steps because each
    decode step reads the growing context, then converted to time with an
    effective KV bandwidth.  The result is a lower bound: it ignores queueing,
    reward-model cost, verifier cost and retries.
    """
    if group_size < 1 or prompt_tokens < 1 or output_tokens < 1:
        raise ValueError("group and token counts must be positive")
    if min(prefill_tokens_per_s, decode_tokens_per_s, kv_bandwidth_gbs) <= 0:
        raise ValueError("throughput and bandwidth must be positive")
    if kv_bytes_per_token <= 0 or gpu_hour_cost < 0:
        raise ValueError("invalid KV size or GPU cost")

    prefill_tokens = prompt_tokens * (1 if share_prefix else group_size)
    decode_tokens = group_size * output_tokens
    # Each generated token reads the context that existed before it plus its
    # own KV write.  Summing 0..output_tokens-1 gives the triangular term.
    kv_token_steps = group_size * (
        prompt_tokens * output_tokens + output_tokens * (output_tokens - 1) // 2
    )
    prefill_s = prefill_tokens / prefill_tokens_per_s
    decode_s = decode_tokens / decode_tokens_per_s
    kv_s = kv_token_steps * kv_bytes_per_token / (kv_bandwidth_gbs * 1e9)
    gpu_seconds = prefill_s + decode_s + kv_s
    return RolloutCost(
        prefill_tokens=prefill_tokens,
        decode_tokens=decode_tokens,
        kv_token_steps=kv_token_steps,
        gpu_seconds=gpu_seconds,
        cost_usd=gpu_seconds / 3600.0 * gpu_hour_cost,
    )


@dataclass(frozen=True)
class WeightVersion:
    """Identity of the policy that produced a rollout."""

    model_id: str
    step: int

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("model_id must not be empty")
        if self.step < 0:
            raise ValueError("step must be non-negative")

    def bump(self, steps: int = 1) -> "WeightVersion":
        if steps < 1:
            raise ValueError("steps must be positive")
        return replace(self, step=self.step + steps)

    def staleness(self, trainer_step: int) -> int:
        if trainer_step < 0:
            raise ValueError("trainer_step must be non-negative")
        return max(0, trainer_step - self.step)

    def acceptable(self, trainer_step: int, max_staleness_steps: int) -> bool:
        if max_staleness_steps < 0:
            raise ValueError("max_staleness_steps must be non-negative")
        return self.staleness(trainer_step) <= max_staleness_steps


@dataclass(frozen=True)
class KVNamespace:
    """Cache namespace that makes policy/KV mismatches impossible to ignore.

    A rollout's KV is only valid for the exact policy, tokenizer and template
    that produced it.  Putting the weight step in the namespace is the
    simplest way to make a stale cache a *miss* rather than a silent wrong
    answer.
    """

    model_id: str
    weight_step: int
    tokenizer_hash: str
    template_hash: str = ""
    policy: str = "rollout"

    def key(self, prefix_hash: str) -> str:
        import hashlib

        raw = "|".join(
            [
                self.model_id,
                str(self.weight_step),
                self.tokenizer_hash,
                self.template_hash,
                self.policy,
                prefix_hash,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def matches(self, other: "KVNamespace") -> bool:
        return self == other


@dataclass
class PartialRollout:
    """A rollout that can be suspended at a token budget and resumed later."""

    prompt_id: str
    budget: int
    generated: int = 0
    suspended: bool = False
    finished: bool = False

    def __post_init__(self) -> None:
        if not self.prompt_id:
            raise ValueError("prompt_id must not be empty")
        if self.budget < 1:
            raise ValueError("budget must be positive")
        if self.generated < 0:
            raise ValueError("generated must be non-negative")
        if self.generated > self.budget:
            raise ValueError("generated cannot exceed budget")

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.generated)

    def advance(self, tokens: int) -> int:
        """Consume up to ``tokens`` from the budget; return tokens consumed."""
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        if self.finished or self.suspended:
            return 0
        used = min(tokens, self.remaining)
        self.generated += used
        if self.generated >= self.budget:
            self.finished = True
        return used

    def suspend(self) -> None:
        if self.finished:
            raise ValueError("cannot suspend a finished rollout")
        self.suspended = True

    def resume(self) -> None:
        if self.finished:
            raise ValueError("cannot resume a finished rollout")
        self.suspended = False


def rollout_throughput(
    *,
    group_size: int,
    prompt_tokens: int,
    output_tokens: int,
    prefill_tokens_per_s: float,
    decode_tokens_per_s: float,
    concurrency: int = 1,
    share_prefix: bool = True,
) -> dict[str, float]:
    """Steady-state token and sample throughput for a rollout workload.

    Prefill and decode can overlap across concurrent groups, so the pipeline
    rate is limited by the slower stage rather than their sum.  This is the
    same max-stage argument used for zero-overhead serving, applied to a
    trainer-facing workload.
    """
    if group_size < 1 or prompt_tokens < 1 or output_tokens < 1:
        raise ValueError("counts must be positive")
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    if prefill_tokens_per_s <= 0 or decode_tokens_per_s <= 0:
        raise ValueError("throughput must be positive")
    prefill_per_group = prompt_tokens * (1 if share_prefix else group_size)
    decode_per_group = group_size * output_tokens
    prefill_s = prefill_per_group / prefill_tokens_per_s
    decode_s = decode_per_group / decode_tokens_per_s
    stage_s = max(prefill_s, decode_s)
    groups_per_s = concurrency / stage_s if stage_s > 0 else 0.0
    samples_per_s = groups_per_s * group_size
    return {
        "stage_s": stage_s,
        "groups_per_s": groups_per_s,
        "samples_per_s": samples_per_s,
        "tokens_per_s": samples_per_s * output_tokens,
        "prefill_s": prefill_s,
        "decode_s": decode_s,
        "bottleneck": "prefill" if prefill_s >= decode_s else "decode",
    }
