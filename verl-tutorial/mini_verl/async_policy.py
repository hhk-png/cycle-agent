"""Replay buffer, staleness control and rollout importance correction."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .protocol import DataProto

__all__ = ["VersionedBatch", "ReplayBuffer", "compute_rollout_correction"]


@dataclass
class VersionedBatch:
    data: DataProto
    version: int
    produced_at: int


class ReplayBuffer:
    """Version-aware queue used by ``colocate_async``/``separate_async``.

    ``max_off_policy_threshold`` is expressed in parameter versions, matching
    verl's V1 sampler.  ``drop`` discards stale groups; ``keep`` keeps them and
    exposes the staleness so rollout importance sampling can correct the bias.
    """

    def __init__(
        self,
        max_off_policy_threshold: int = 8,
        strategy: str = "drop",
    ):
        self.max_off_policy_threshold = max_off_policy_threshold
        self.strategy = strategy
        if strategy not in {"drop", "keep"}:
            raise ValueError("strategy must be 'drop' or 'keep'")
        self.batches: list[VersionedBatch] = []
        self.dropped = 0

    def add(self, data: DataProto, version: int, produced_at: int) -> None:
        self.batches.append(VersionedBatch(data=data, version=version, produced_at=produced_at))

    def __len__(self) -> int:
        return len(self.batches)

    def sample(self, batch_size: int, current_version: int) -> tuple[DataProto, dict[str, float]]:
        if not self.batches:
            raise IndexError("replay buffer is empty")
        selected: list[DataProto] = []
        staleness: list[int] = []
        versions: list[int] = []
        remaining: list[VersionedBatch] = []
        for batch in self.batches:
            age = current_version - batch.version
            if self.strategy == "drop" and age > self.max_off_policy_threshold:
                self.dropped += len(batch.data)
                continue
            remaining.append(batch)
        self.batches = remaining
        while self.batches and sum(len(item) for item in selected) < batch_size:
            batch = self.batches.pop(0)
            selected.append(batch.data)
            staleness.extend([current_version - batch.version] * len(batch.data))
            versions.extend([batch.version] * len(batch.data))
        if not selected:
            raise IndexError("no fresh batches available")
        data = DataProto.concat(selected)
        if len(data) > batch_size:
            data = data[:batch_size]
            staleness = staleness[:batch_size]
            versions = versions[:batch_size]
        # ``rollout_version`` is the policy that actually produced each
        # trajectory, not the version at consumption time.  Overwriting it with
        # ``current_version`` would erase exactly the information needed to
        # audit off-policy data.
        data.batch["rollout_version"] = np.asarray(versions, dtype=np.int64)
        data.batch["staleness"] = np.asarray(staleness, dtype=np.int64)
        metrics = {
            "async/staleness_mean": float(np.mean(staleness)) if staleness else 0.0,
            "async/staleness_max": float(np.max(staleness)) if staleness else 0.0,
            "async/dropped_samples": float(self.dropped),
            "async/buffer_batches": float(len(self.batches)),
        }
        return data, metrics


def compute_rollout_correction(
    data: DataProto,
    current_log_probs: np.ndarray,
    truncate_ratio: float = 10.0,
    normalize: bool = False,
) -> tuple[np.ndarray, dict[str, float]]:
    """Truncated importance weights between behavior and current policy.

    ``rollout_log_probs`` is the log-prob under the sampler that produced the
    trajectory; ``current_log_probs`` is recomputed by the training engine.
    The ratio is ``exp(current - behavior)``; weights are truncated to
    ``[1/truncate, truncate]``.  ``normalize=True`` additionally divides by the
    masked mean (self-normalized IS).  The default keeps the textbook formula:
    a uniform behavior/current shift must *not* cancel out.
    """
    if "rollout_log_probs" not in data.batch:
        raise KeyError("data has no rollout_log_probs")
    mask = data.batch["response_mask"]
    behavior = data.batch["rollout_log_probs"]
    log_ratio = (current_log_probs - behavior) * mask
    ratio = np.exp(np.clip(log_ratio, -20.0, 20.0))
    lower = 1.0 / max(truncate_ratio, 1.0)
    upper = max(truncate_ratio, 1.0)
    truncated = np.clip(ratio, lower, upper) * mask
    if normalize:
        denom = float(np.sum(truncated)) / max(float(mask.sum()), 1.0)
        truncated = truncated / max(denom, 1e-8)
    weights = np.where(mask > 0, truncated, 1.0)
    metrics = {
        "rollout_is/ratio_mean": float(np.sum(ratio * mask) / max(float(mask.sum()), 1.0)),
        "rollout_is/weight_mean": float(np.sum(weights * mask) / max(float(mask.sum()), 1.0)),
        "rollout_is/truncated_frac": float(
            np.sum(((ratio > upper) | (ratio < lower)) & (mask > 0)) / max(float(mask.sum()), 1.0)
        ),
    }
    return weights.astype(np.float32), metrics
