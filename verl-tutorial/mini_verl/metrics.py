"""Metrics used by the training loop and the tutorial's experiment tables."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .protocol import DataProto

__all__ = [
    "RunningMean",
    "MetricsAggregator",
    "compute_data_metrics",
    "compute_timing_metrics",
    "compute_throughput_metrics",
    "compute_validation_metrics",
    "compute_pass_at_k",
    "estimate_flops_per_token",
    "MetricsLogger",
]


class RunningMean:
    def __init__(self) -> None:
        self.total = 0.0
        self.count = 0

    def update(self, value: float, n: int = 1) -> None:
        self.total += float(value) * n
        self.count += n

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0


class MetricsAggregator:
    def __init__(self) -> None:
        self.values: dict[str, RunningMean] = {}

    def add(self, metrics: dict[str, float], n: int = 1) -> None:
        for key, value in metrics.items():
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                self.values.setdefault(key, RunningMean()).update(float(value), n)

    def merge(self, other: "MetricsAggregator") -> None:
        for key, running in other.values.items():
            target = self.values.setdefault(key, RunningMean())
            target.total += running.total
            target.count += running.count

    def result(self) -> dict[str, float]:
        return {key: running.mean for key, running in self.values.items()}


def _masked_mean(array: np.ndarray, mask: np.ndarray) -> float:
    denom = float(mask.sum())
    return float(np.sum(array * mask) / denom) if denom else 0.0


def compute_data_metrics(data: DataProto) -> dict[str, float]:
    metrics: dict[str, float] = {}
    mask = data.batch["response_mask"]
    lengths = mask.sum(axis=-1)
    metrics["data/response_length_mean"] = float(lengths.mean()) if len(data) else 0.0
    metrics["data/response_length_max"] = float(lengths.max()) if len(data) else 0.0
    metrics["data/response_length_min"] = float(lengths.min()) if len(data) else 0.0
    if "rm_scores" in data.batch:
        scores = data.batch["rm_scores"]
        metrics["data/reward_mean"] = float(scores.mean())
        metrics["data/reward_std"] = float(scores.std()) if scores.size > 1 else 0.0
        metrics["data/reward_max"] = float(scores.max()) if scores.size else 0.0
        metrics["data/reward_min"] = float(scores.min()) if scores.size else 0.0
        metrics["data/accuracy"] = float(np.mean(scores > 0.5))
    if "advantages" in data.batch:
        advantages = data.batch["advantages"]
        metrics["data/advantage_mean"] = _masked_mean(advantages, mask)
        metrics["data/advantage_std"] = float(np.std(advantages[mask > 0])) if mask.sum() else 0.0
    if "values" in data.batch:
        metrics["data/value_mean"] = _masked_mean(data.batch["values"], mask)
    if "old_log_probs" in data.batch and "ref_log_prob" in data.batch:
        diff = data.batch["old_log_probs"] - data.batch["ref_log_prob"]
        metrics["data/kl_old_ref"] = _masked_mean(diff, mask)
    if "rollout_log_probs" in data.batch and "old_log_probs" in data.batch:
        diff = data.batch["rollout_log_probs"] - data.batch["old_log_probs"]
        metrics["data/rollout_train_logp_gap"] = _masked_mean(np.abs(diff), mask)
    if "truncated" in data.batch:
        metrics["data/truncated_ratio"] = float(np.mean(data.batch["truncated"] > 0))
    elif "response_length" in data.batch:
        # Fallback for batches without an explicit truncation flag.  Prefer the
        # explicit flag: a natural EOS exactly at max_response_length is *not*
        # a truncation.
        max_response_length = int(
            data.meta_info.get("max_response_length", data.meta_info.get("max_response_len", mask.shape[-1]))
        )
        metrics["data/truncated_ratio"] = float(
            np.mean(data.batch["response_length"] >= max_response_length)
        )
    return metrics


def compute_timing_metrics(timings: dict[str, float]) -> dict[str, float]:
    result = {f"timing/{key}_s": float(value) for key, value in timings.items()}
    total = sum(float(value) for value in timings.values())
    result["timing/total_s"] = total
    return result


def estimate_flops_per_token(num_parameters: int, training: bool = True) -> float:
    """6N for training (forward+backward), 2N for inference."""
    return float(6 * num_parameters if training else 2 * num_parameters)


def compute_throughput_metrics(
    num_tokens: int,
    elapsed_seconds: float,
    num_gpus: int,
    num_parameters: int,
    num_samples: int | None = None,
    training: bool = True,
    peak_flops_per_gpu: float = 3.12e14,
) -> dict[str, float]:
    elapsed = max(float(elapsed_seconds), 1e-9)
    tokens_per_s = num_tokens / elapsed
    flops = num_tokens * estimate_flops_per_token(num_parameters, training)
    mfu = flops / (elapsed * max(num_gpus, 1) * peak_flops_per_gpu)
    return {
        "perf/tokens_per_s": tokens_per_s,
        "perf/samples_per_s": (num_samples / elapsed) if num_samples else 0.0,
        "perf/tflops": flops / elapsed / 1e12,
        "perf/mfu": mfu,
        "perf/gpus": float(num_gpus),
    }


def compute_pass_at_k(scores: np.ndarray, n: int, k: int) -> float:
    """Unbiased pass@k estimator over groups of ``n`` samples."""
    if n < k:
        raise ValueError("n must be >= k")
    groups = len(scores) // n
    if groups == 0:
        return 0.0
    total = 0.0
    for group in range(groups):
        correct = int(np.sum(scores[group * n : (group + 1) * n] > 0.5))
        if n - correct < k:
            total += 1.0
        else:
            total += 1.0 - math.comb(n - correct, k) / math.comb(n, k)
    return total / groups


def compute_validation_metrics(scores: np.ndarray, n: int = 1, ks: Iterable[int] = (1,)) -> dict[str, float]:
    metrics = {"val/accuracy": float(np.mean(scores > 0.5)) if len(scores) else 0.0}
    for k in ks:
        if n >= k:
            metrics[f"val/pass@{k}"] = compute_pass_at_k(scores, n, k)
    return metrics


class MetricsLogger:
    def __init__(self, log_dir: str | Path, project: str, experiment: str, backends: Iterable[str] = ("console",)):
        self.directory = Path(log_dir) / project / experiment
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.directory / "metrics.jsonl"
        self.backends = list(backends)

    def log(self, metrics: dict[str, float], step: int) -> None:
        payload = {"step": step, **{key: float(value) for key, value in metrics.items()}}
        with open(self.jsonl, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if "console" in self.backends:
            compact = ", ".join(
                f"{key}={value:.4g}"
                for key, value in metrics.items()
                if key.startswith(("data/reward", "data/accuracy", "actor/loss", "critic/vf_loss", "val/"))
            )
            print(f"[step {step:04d}] {compact}")
