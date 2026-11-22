"""Parallelism simulators: tensor parallel, pipeline, MoE and Ulysses SP.

These are *numerical* simulators.  They compute the same result as a dense
reference by explicitly splitting/merging arrays, which makes the collective
semantics visible without NCCL.  Production verl delegates the same operations
to PyTorch DTensor/FSDP/Megatron and to NCCL.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "ProcessGroup",
    "ColumnParallelLinear",
    "RowParallelLinear",
    "PipelineSchedule",
    "pipeline_forward",
    "moe_route",
    "record_moe_routes",
    "replay_moe_routes",
    "router_replay_report",
    "ulysses_split",
    "ulysses_merge",
    "sequence_parallel_mlp",
    "kv_cache_bytes",
]


@dataclass
class ProcessGroup:
    rank: int
    size: int

    def __post_init__(self) -> None:
        if not (0 <= self.rank < self.size):
            raise ValueError("rank must be in [0, size)")


def _split(array: np.ndarray, parts: int, axis: int) -> list[np.ndarray]:
    if array.shape[axis] % parts:
        raise ValueError(f"axis {axis} of shape {array.shape} is not divisible by {parts}")
    return [np.ascontiguousarray(part) for part in np.split(array, parts, axis=axis)]


class ColumnParallelLinear:
    """``Y = X W + b`` with W split along the output dimension.

    Each rank owns ``W_i`` of shape ``(in, out/tp)`` and produces a slice of the
    output.  An all-gather along the last dimension reconstructs Y.
    """

    def __init__(self, weight: np.ndarray, bias: np.ndarray | None = None, tp_size: int = 2):
        self.tp_size = tp_size
        self.weight_shards = _split(weight, tp_size, axis=1)
        self.bias_shards = _split(bias, tp_size, axis=0) if bias is not None else None

    def local_forward(self, x: np.ndarray, rank: int) -> np.ndarray:
        out = x @ self.weight_shards[rank]
        if self.bias_shards is not None:
            out = out + self.bias_shards[rank]
        return out

    def forward(self, x: np.ndarray) -> np.ndarray:
        return np.concatenate([self.local_forward(x, rank) for rank in range(self.tp_size)], axis=-1)


class RowParallelLinear:
    """``Y = X W + b`` with W split along the input dimension.

    Each rank owns ``W_i`` of shape ``(in/tp, out)``, consumes the matching
    slice of X, and contributes a partial output.  An all-reduce (sum)
    reconstructs Y; the bias is added once.
    """

    def __init__(self, weight: np.ndarray, bias: np.ndarray | None = None, tp_size: int = 2):
        self.tp_size = tp_size
        self.weight_shards = _split(weight, tp_size, axis=0)
        self.bias = bias

    def local_forward(self, x: np.ndarray, rank: int) -> np.ndarray:
        return x @ self.weight_shards[rank]

    def forward(self, x: np.ndarray) -> np.ndarray:
        input_shards = _split(x, self.tp_size, axis=-1)
        partials = [self.local_forward(input_shards[rank], rank) for rank in range(self.tp_size)]
        out = np.sum(np.stack(partials, axis=0), axis=0)
        if self.bias is not None:
            out = out + self.bias
        return out


def _mlp(x: np.ndarray, w1: np.ndarray, b1: np.ndarray, w2: np.ndarray, b2: np.ndarray) -> np.ndarray:
    hidden = np.maximum(x @ w1 + b1, 0.0)
    return hidden @ w2 + b2


def sequence_parallel_mlp(
    x: np.ndarray,
    w1: np.ndarray,
    b1: np.ndarray,
    w2: np.ndarray,
    b2: np.ndarray,
    sp_size: int,
) -> np.ndarray:
    """Sequence-parallel MLP: split tokens, compute, all-gather.

    Column-parallel fc1 followed by row-parallel fc2 is the standard Megatron
    arrangement; here the sequence dimension is what gets split by Ulysses.
    """
    chunks = ulysses_split(x, sp_size)
    outs = [_mlp(chunk, w1, b1, w2, b2) for chunk in chunks]
    return ulysses_merge(outs, sp_size)


def ulysses_split(x: np.ndarray, sp_size: int) -> list[np.ndarray]:
    if x.ndim != 3:
        raise ValueError("expected (batch, seq, dim)")
    return _split(x, sp_size, axis=1)


def ulysses_merge(chunks: list[np.ndarray], sp_size: int) -> np.ndarray:
    if len(chunks) != sp_size:
        raise ValueError(f"expected {sp_size} chunks, got {len(chunks)}")
    return np.concatenate(chunks, axis=1)


@dataclass
class PipelineSchedule:
    """1F1B bubble accounting for a synchronous pipeline."""

    num_microbatches: int
    num_stages: int
    forward_time: float = 1.0
    backward_time: float = 2.0

    def bubble_ratio(self) -> float:
        """Fraction of pipeline slots that are idle.

        For a balanced 1F1B schedule the pipeline is full for
        ``num_microbatches`` slots and has ``num_stages - 1`` warmup/cooldown
        slots, so the idle fraction is ``(p-1) / (m + p - 1)``.
        """
        if self.num_microbatches <= 0:
            raise ValueError("num_microbatches must be positive")
        if self.num_stages <= 1:
            return 0.0
        return (self.num_stages - 1) / (self.num_microbatches + self.num_stages - 1)

    def timeline(self) -> list[tuple[int, int, str, float, float]]:
        """Return (microbatch, stage, phase, start, end) slots."""
        slots: list[tuple[int, int, str, float, float]] = []
        forward_done: dict[int, float] = {}
        for micro in range(self.num_microbatches):
            for stage in range(self.num_stages):
                start = max(
                    forward_done.get(micro, 0.0),
                    max([end for (m, s, _, _, end) in slots if s == stage and m < micro] or [0.0]),
                )
                slots.append((micro, stage, "forward", start, start + self.forward_time))
                forward_done[micro] = start + self.forward_time
        backward_start = max(end for _, _, _, _, end in slots)
        for micro in reversed(range(self.num_microbatches)):
            for stage in reversed(range(self.num_stages)):
                start = max(
                    backward_start,
                    max([end for (m, s, _, _, end) in slots if s == stage] or [0.0]),
                )
                slots.append((micro, stage, "backward", start, start + self.backward_time))
                backward_start = start + self.backward_time
        return sorted(slots, key=lambda item: (item[3], item[1]))


def pipeline_forward(stages, microbatches: list[np.ndarray]) -> list[np.ndarray]:
    """Run microbatches through a list of stage functions.

    This is the correctness check: a pipeline must produce exactly the same
    outputs as applying the stages sequentially to the full batch.
    """
    outputs = []
    for micro in microbatches:
        value = micro
        for stage in stages:
            value = stage(value)
        outputs.append(value)
    return outputs


def _route_top_idx(record):
    """Accept either a bare top-k array or a record dict."""
    if isinstance(record, dict):
        if "top_idx" not in record:
            raise ValueError("route record is missing 'top_idx'")
        return np.asarray(record["top_idx"])
    return np.asarray(record)


def _route_accepted(record):
    if isinstance(record, dict) and "accepted" in record:
        return np.asarray(record["accepted"], dtype=bool)
    return None


def moe_route(
    router_logits: np.ndarray,
    top_k: int = 2,
    capacity_factor: float = 1.25,
    expert_outputs: np.ndarray | None = None,
    replay_top_idx: np.ndarray | None = None,
    replay_mask: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """Top-k MoE routing with capacity dropping and optional route replay.

    ``router_logits`` is ``(tokens, experts)``.  Returns assignments, gate
    weights, per-expert token counts and (optionally) the combined output given
    ``expert_outputs`` of shape ``(experts, tokens, dim)``.

    ``replay_top_idx`` replaces the router's own top-k choice with recorded
    expert ids, which is how training keeps the same expert assignment that the
    rollout backend used.  ``replay_mask`` selects rows to replay (``True``) and
    rows to route natively (``False``); the mask follows verl's row-level
    replay semantics, not a per-slot mask.

    ``top_idx`` is the pre-capacity choice.  Whether a slot actually ran is in
    ``accepted``; a replay with a different ``capacity_factor`` can reproduce
    ``top_idx`` while dropping a different set of slots.  Use
    ``record_moe_routes(..., with_accepted=True)`` and
    ``router_replay_report`` when that distinction matters.
    """
    logits = np.asarray(router_logits)
    if logits.ndim != 2:
        raise ValueError(f"router_logits must be 2D (tokens, experts), got shape {logits.shape}")
    if np.iscomplexobj(logits) or not np.issubdtype(logits.dtype, np.number):
        raise ValueError("router_logits must be a real numeric array")
    logits = logits.astype(np.float32)
    tokens, experts = logits.shape
    if experts < 1:
        raise ValueError("router_logits must have at least one expert")
    if not isinstance(top_k, (int, np.integer)) or isinstance(top_k, bool):
        raise ValueError("top_k must be an integer")
    top_k = int(top_k)
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if top_k > experts:
        raise ValueError("top_k cannot exceed the number of experts")
    if replay_mask is not None and replay_top_idx is None:
        raise ValueError("replay_mask requires replay_top_idx")
    if tokens and not np.isfinite(logits.max(axis=-1)).all():
        raise ValueError("each row needs at least one finite router logit")

    shifted = logits - logits.max(axis=-1, keepdims=True)
    probs = np.exp(shifted)
    denominator = probs.sum(axis=-1, keepdims=True)
    probs = probs / denominator
    native_top_idx = np.argsort(-probs, axis=-1)[:, :top_k]

    if replay_top_idx is None:
        top_idx = native_top_idx
        replayed = False
    else:
        recorded = np.asarray(replay_top_idx)
        if recorded.shape != (tokens, top_k):
            raise ValueError(
                f"replay_top_idx must have shape {(tokens, top_k)}, got {recorded.shape}"
            )
        if np.iscomplexobj(recorded) or not np.issubdtype(recorded.dtype, np.number):
            raise ValueError("replay_top_idx must be a real numeric array")
        if not np.issubdtype(recorded.dtype, np.integer):
            if not np.all(recorded == np.floor(recorded)):
                raise ValueError("replay_top_idx must contain integer expert ids")
            recorded = recorded.astype(np.int64)
        if tokens and (recorded.min() < 0 or recorded.max() >= experts):
            raise ValueError("replay_top_idx contains an out-of-range expert id")
        if replay_mask is None:
            top_idx = recorded
            replayed = True
        else:
            mask = np.asarray(replay_mask, dtype=bool)
            if mask.shape != (tokens,):
                raise ValueError(
                    f"replay_mask must have shape {(tokens,)}, got {mask.shape}"
                )
            top_idx = np.where(mask[:, None], recorded, native_top_idx)
            replayed = bool(mask.any())

    top_prob = np.take_along_axis(probs, top_idx, axis=-1)
    top_prob = top_prob / top_prob.sum(axis=-1, keepdims=True)

    capacity = max(1, int(np.ceil(tokens * top_k / experts * capacity_factor)))
    counts = np.zeros(experts, dtype=np.int64)
    accepted = np.zeros((tokens, top_k), dtype=bool)
    for token in range(tokens):
        for slot in range(top_k):
            expert = int(top_idx[token, slot])
            if counts[expert] < capacity:
                counts[expert] += 1
                accepted[token, slot] = True

    gates = np.where(accepted, top_prob, 0.0).astype(np.float32)
    dropped_slots = int((~accepted).sum())
    dropped_tokens = int((~accepted).any(axis=1).sum()) if tokens else 0
    result = {
        "top_idx": top_idx,
        "native_top_idx": native_top_idx,
        "replayed": np.asarray([replayed]),
        "gates": gates,
        "accepted": accepted,
        "expert_counts": counts,
        "capacity": np.asarray([capacity]),
        "dropped_tokens": np.asarray([dropped_tokens]),
        "dropped_slots": np.asarray([dropped_slots]),
    }
    if expert_outputs is not None:
        expert_outputs = np.asarray(expert_outputs, dtype=np.float32)
        if (
            expert_outputs.ndim != 3
            or expert_outputs.shape[0] != experts
            or expert_outputs.shape[1] != tokens
        ):
            raise ValueError(
                "expert_outputs must have shape "
                f"({experts}, {tokens}, dim), got {expert_outputs.shape}"
            )
        output = np.zeros((tokens, expert_outputs.shape[-1]), dtype=np.float32)
        for token in range(tokens):
            for slot in range(top_k):
                if accepted[token, slot]:
                    output[token] += gates[token, slot] * expert_outputs[int(top_idx[token, slot]), token]
        result["output"] = output
    return result


def record_moe_routes(
    route_result: dict[str, np.ndarray],
    with_accepted: bool = False,
) -> np.ndarray | dict[str, np.ndarray]:
    """Copy the routing decisions out of a routing result for later replay.

    The default return is the ``(tokens, top_k)`` array.  Pass
    ``with_accepted=True`` to also record ``accepted``; this is what lets a
    report distinguish "same top-k, different capacity drop" from a true match.
    """
    top_idx = np.asarray(route_result["top_idx"], dtype=np.int64).copy()
    if not with_accepted:
        return top_idx
    return {
        "top_idx": top_idx,
        "accepted": np.asarray(route_result["accepted"], dtype=bool).copy(),
    }


def replay_moe_routes(
    layer_logits: list[np.ndarray],
    recorded_routes: list[np.ndarray | dict[str, np.ndarray]],
    top_k: int = 2,
    capacity_factor: float = 1.25,
) -> list[dict[str, np.ndarray]]:
    """Replay one recorded routing table per layer.

    The layer count check is the important part.  A model with hash-routed
    layers (DeepSeek V4 has three before the learned routers) records more
    entries than a Megatron implementation that only intercepts learned top-k.
    If the two lists differ, every route after the missing layer is replayed
    against the wrong layer even when the tensor shapes agree.  Fail loudly here
    instead of producing a plausible-looking but wrong update.
    """
    if len(layer_logits) != len(recorded_routes):
        raise ValueError(
            "router replay layer mismatch: "
            f"{len(layer_logits)} layers of logits vs {len(recorded_routes)} recorded routes; "
            "record hash-router layers as well as learned routers"
        )
    return [
        moe_route(
            logits,
            top_k=top_k,
            capacity_factor=capacity_factor,
            replay_top_idx=_route_top_idx(recorded),
        )
        for logits, recorded in zip(layer_logits, recorded_routes)
    ]


def router_replay_report(
    recorded_routes: list[np.ndarray | dict[str, np.ndarray]],
    replayed_routes: list[np.ndarray | dict[str, np.ndarray]],
) -> dict[str, object]:
    """Compare recorded and replayed routes layer by layer.

    Two different questions are reported:

    * ``top_idx`` equality (or ``mismatched_tokens``) answers "did the same
      experts get selected?" — an early-layer mismatch points at ordering, a
      later one at a mask or an alignment bug.
    * If both sides carry ``accepted`` (recorded with
      ``with_accepted=True``), ``mismatched_assignments`` answers "did the same
      slots actually run?" — a capacity-factor change can keep ``top_idx``
      identical while dropping a different set of slots.

    Layers whose shapes differ are listed in ``shape_mismatched_layers``; they
    are counted as mismatched layers but do not contribute to
    ``mismatched_tokens`` (there is no token-wise comparison to make).
    """
    if len(recorded_routes) != len(replayed_routes):
        raise ValueError(
            f"cannot compare {len(recorded_routes)} recorded layers with "
            f"{len(replayed_routes)} replayed layers"
        )
    mismatched_layers: list[int] = []
    shape_mismatched_layers: list[int] = []
    mismatched_tokens = 0
    mismatched_assignments = 0
    accepted_compared = 0
    for layer, (recorded, replayed) in enumerate(zip(recorded_routes, replayed_routes)):
        recorded_top = _route_top_idx(recorded)
        replayed_top = _route_top_idx(replayed)
        if recorded_top.shape != replayed_top.shape:
            shape_mismatched_layers.append(layer)
            mismatched_layers.append(layer)
            continue
        row_diff = (
            np.any(recorded_top != replayed_top, axis=-1)
            if recorded_top.ndim == 2
            else recorded_top != replayed_top
        )
        count = int(np.sum(row_diff))
        if count:
            mismatched_layers.append(layer)
            mismatched_tokens += count
        recorded_accepted = _route_accepted(recorded)
        replayed_accepted = _route_accepted(replayed)
        if recorded_accepted is not None and replayed_accepted is not None:
            accepted_compared += 1
            if recorded_accepted.shape != replayed_accepted.shape:
                shape_mismatched_layers.append(layer)
                mismatched_layers.append(layer)
                continue
            accepted_diff = (
                np.any(recorded_accepted != replayed_accepted, axis=-1)
                if recorded_accepted.ndim == 2
                else recorded_accepted != replayed_accepted
            )
            assignment_count = int(np.sum(accepted_diff))
            if assignment_count:
                mismatched_assignments += assignment_count
                mismatched_layers.append(layer)
    unique_layers = sorted(set(mismatched_layers))
    return {
        "layers": len(recorded_routes),
        "exact_match": not unique_layers,
        "mismatched_layers": unique_layers,
        "first_mismatch": unique_layers[0] if unique_layers else None,
        "mismatched_tokens": mismatched_tokens,
        "mismatched_assignments": mismatched_assignments,
        "shape_mismatched_layers": sorted(set(shape_mismatched_layers)),
        "accepted_compared": accepted_compared,
    }


def kv_cache_bytes(
    batch_size: int,
    sequence_length: int,
    num_layers: int,
    num_kv_heads: int,
    head_dim: int,
    bytes_per_element: int = 2,
) -> int:
    return batch_size * sequence_length * num_layers * 2 * num_kv_heads * head_dim * bytes_per_element
