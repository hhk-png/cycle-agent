"""Logical parallelism experiments.

None of this is real multi-process execution.  The functions partition arrays
across logical ranks and perform the same collective operations a distributed
engine would issue.  That is enough to test correctness of the partitioning,
the all-to-all dispatch pattern, and the cost model used in the tutorial.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .cache_engine import PagedKVCache
from .model.ops import softmax, silu
from .sequence import Sequence


def all_gather(shards: list[np.ndarray], axis: int = 0) -> np.ndarray:
    """Return the tensor every rank sees after an all-gather.

    Each rank contributes one shard and receives every other rank's shard, so
    the result is the concatenation of all shards.  Keeping the input as a
    list makes the rank-local ownership explicit: a rank may only read its own
    shard before the collective, not the full tensor.
    """
    if not shards:
        raise ValueError("all_gather needs at least one shard")
    if any(shard.shape[axis] != shards[0].shape[axis] for shard in shards[1:]):
        raise ValueError("all shards must have the same size along the gather axis")
    return np.concatenate(shards, axis=axis)


def split_shards(tensor: np.ndarray, num_ranks: int, axis: int = 0) -> list[np.ndarray]:
    if tensor.shape[axis] % num_ranks:
        raise ValueError("tensor length must be divisible by the rank count")
    return list(np.split(tensor, num_ranks, axis=axis))


@dataclass
class TPStats:
    rank: int
    shard_shape: tuple[int, ...]
    communicated_bytes: int = 0
    collective: str = "none"


def _split_last(matrix: np.ndarray, parts: int) -> list[np.ndarray]:
    if matrix.shape[-1] % parts:
        raise ValueError("last dimension must be divisible by tp_size")
    return list(np.split(matrix, parts, axis=-1))


def _split_first(matrix: np.ndarray, parts: int) -> list[np.ndarray]:
    if matrix.shape[0] % parts:
        raise ValueError("first dimension must be divisible by tp_size")
    return list(np.split(matrix, parts, axis=0))


def _all_reduce_sum(shards: list[np.ndarray]) -> np.ndarray:
    total = np.zeros_like(shards[0])
    for shard in shards:
        total = total + shard
    return total


def tensor_parallel_linear(
    x: np.ndarray,
    weight: np.ndarray,
    *,
    tp_size: int,
    mode: str = "column",
    bias: np.ndarray | None = None,
) -> tuple[np.ndarray, list[TPStats]]:
    """Logical tensor parallelism for one linear layer.

    ``column`` splits the output dimension: every rank owns a slice of the
    weight columns, computes a slice of the output, and the results are
    all-gathered.  ``row`` splits the input dimension: every rank owns a slice
    of the weight rows, computes a partial sum, and the partial sums are
    all-reduced.  The mathematical result is identical to a single rank.
    """
    if tp_size < 1:
        raise ValueError("tp_size must be positive")
    if x.ndim != 2 or weight.ndim != 2:
        raise ValueError("x and weight must be 2-D")
    if x.shape[1] != weight.shape[0]:
        raise ValueError("x and weight shapes do not match")
    if mode == "column":
        weight_shards = _split_last(weight, tp_size)
        if bias is not None:
            if bias.shape[0] != weight.shape[1]:
                raise ValueError("bias length must match the output dimension")
            bias_shards = list(np.split(bias, tp_size))
        else:
            bias_shards = None
        stats: list[TPStats] = []
        outputs = []
        for rank, shard in enumerate(weight_shards):
            out = x @ shard
            if bias_shards is not None:
                out = out + bias_shards[rank].reshape(1, -1)
            outputs.append(out)
            stats.append(
                TPStats(
                    rank=rank,
                    shard_shape=tuple(shard.shape),
                    communicated_bytes=int(out.nbytes * (tp_size - 1)),
                    collective="all_gather",
                )
            )
        # all_gather concatenates the output shards along the feature axis.
        return np.concatenate(outputs, axis=-1), stats
    if mode == "row":
        # Row parallelism splits the input feature dimension of x and the
        # corresponding rows of the weight; each rank produces a partial sum.
        x_shards = _split_last(x, tp_size)
        weight_shards = _split_first(weight, tp_size)
        partials = [part @ shard for part, shard in zip(x_shards, weight_shards)]
        result = _all_reduce_sum(partials)
        if bias is not None:
            result = result + bias.reshape(1, -1)
        bytes_moved = int(result.nbytes * 2 * (tp_size - 1) / tp_size)
        stats = [
            TPStats(
                rank=rank,
                shard_shape=tuple(shard.shape),
                communicated_bytes=bytes_moved,
                collective="all_reduce",
            )
            for rank, shard in enumerate(weight_shards)
        ]
        return result, stats
    raise ValueError("mode must be 'column' or 'row'")


def tensor_parallel_mlp(
    hidden: np.ndarray,
    w1: np.ndarray,
    w3: np.ndarray,
    w2: np.ndarray,
    *,
    tp_size: int,
) -> tuple[np.ndarray, int]:
    """SwiGLU MLP with column-parallel up projections and a row-parallel down
    projection.  Only one all-reduce is needed at the end of the block."""
    if tp_size < 1:
        raise ValueError("tp_size must be positive")
    up1, stats1 = tensor_parallel_linear(hidden, w1, tp_size=tp_size, mode="column")
    up3, stats3 = tensor_parallel_linear(hidden, w3, tp_size=tp_size, mode="column")
    intermediate = silu(up1) * up3
    down, stats2 = tensor_parallel_linear(intermediate, w2, tp_size=tp_size, mode="row")
    moved = (
        sum(stat.communicated_bytes for stat in stats1)
        + sum(stat.communicated_bytes for stat in stats3)
        + sum(stat.communicated_bytes for stat in stats2)
    )
    return down, moved


def context_parallel_attention(
    q: np.ndarray,
    k: np.ndarray,
    v: np.ndarray,
    mask: np.ndarray,
    cp_size: int,
) -> tuple[np.ndarray, int]:
    """Context-parallel attention with a mirror all-gather of K/V.

    Each rank owns a slice of the query sequence and computes attention for
    that slice against the gathered keys/values.  The mathematical result is
    identical to single-rank attention; only the placement changes.
    """
    q_shards = split_shards(q, cp_size, axis=0)
    k_shards = split_shards(k, cp_size, axis=0)
    v_shards = split_shards(v, cp_size, axis=0)
    outputs: list[np.ndarray] = []
    boundary = 0
    moved = 0
    for rank, shard in enumerate(q_shards):
        # Before the collective this rank owns only k_shards[rank] and
        # v_shards[rank].  The all-gather gives every rank the full K/V; the
        # simulation concatenates all shards, which is what each rank receives.
        gathered_k = all_gather(k_shards, axis=0)
        gathered_v = all_gather(v_shards, axis=0)
        moved += (cp_size - 1) * (k_shards[rank].nbytes + v_shards[rank].nbytes)
        k_heads = gathered_k.transpose(1, 0, 2)
        v_heads = gathered_v.transpose(1, 0, 2)
        shard_len = shard.shape[0]
        shard_mask = mask[boundary : boundary + shard_len]
        scores = np.einsum("hqd,htd->hqt", shard.transpose(1, 0, 2), k_heads) / np.sqrt(q.shape[-1])
        scores = np.where(shard_mask[None], scores, -np.inf)
        probs = softmax(scores, axis=-1)
        outputs.append(np.einsum("hqt,htd->hqd", probs, v_heads).transpose(1, 0, 2))
        boundary += shard_len
    return np.concatenate(outputs, axis=0), int(moved)

@dataclass
class Expert:
    w1: np.ndarray
    w3: np.ndarray
    w2: np.ndarray

    def __call__(self, hidden: np.ndarray) -> np.ndarray:
        return (silu(hidden @ self.w1) * (hidden @ self.w3)) @ self.w2


@dataclass
class EPStats:
    tokens: int = 0
    dispatched: int = 0
    bytes_sent: int = 0
    rank_tokens: list[int] = field(default_factory=list)


def expert_parallel_moe(
    hidden: np.ndarray,
    gate_weight: np.ndarray,
    experts: list[Expert],
    top_k: int,
    *,
    ep_size: int,
    placement: list[int] | None = None,
    shared_expert: Expert | None = None,
) -> tuple[np.ndarray, EPStats]:
    """Dispatch tokens to their top-k experts across logical EP ranks.

    ``placement[expert_id]`` is the owning rank.  The reference result is
    computed locally first, then the same math is performed rank by rank after
    an explicit dispatch, so the test can compare the two.
    """
    num_experts = len(experts)
    if placement is None:
        placement = [expert % ep_size for expert in range(num_experts)]
    logits = hidden @ gate_weight
    probs = softmax(logits, axis=-1)
    top = np.argpartition(-probs, kth=top_k - 1, axis=-1)[:, :top_k]
    top_probs = np.take_along_axis(probs, top, axis=-1)
    top_probs = top_probs / top_probs.sum(axis=-1, keepdims=True)

    output = np.zeros_like(hidden)
    if shared_expert is not None:
        output += shared_expert(hidden)

    # Simulate all-to-all: rank r receives (token, expert, weight) tuples for
    # the experts it owns.
    inbox: list[list[tuple[int, int, float]]] = [[] for _ in range(ep_size)]
    for token in range(hidden.shape[0]):
        for slot in range(top_k):
            expert = int(top[token, slot])
            inbox[placement[expert]].append((token, expert, float(top_probs[token, slot])))
    dispatched = sum(len(bucket) for bucket in inbox)
    bytes_sent = dispatched * hidden.shape[1] * hidden.dtype.itemsize * 2

    for rank, bucket in enumerate(inbox):
        if not bucket:
            continue
        expert_inputs: dict[int, list[int]] = {}
        for token, expert, _ in bucket:
            expert_inputs.setdefault(expert, []).append(token)
        for expert, token_ids in expert_inputs.items():
            rows = hidden[np.asarray(token_ids)]
            expert_out = experts[expert](rows)
            for row, token in enumerate(token_ids):
                weight = next(w for t, e, w in bucket if t == token and e == expert)
                output[token] += weight * expert_out[row]

    stats = EPStats(
        tokens=hidden.shape[0],
        dispatched=dispatched,
        bytes_sent=bytes_sent,
        rank_tokens=[len(bucket) for bucket in inbox],
    )
    return output, stats


def export_sequence_kv(
    kv_cache: PagedKVCache, seq: Sequence
) -> dict[str, np.ndarray | int]:
    """Serialise a sequence's KV for a prefill -> decode handoff."""
    k, v = kv_cache.gather(seq)
    return {
        "k": k.copy(),
        "v": v.copy(),
        "num_tokens": seq.num_computed_tokens,
        "block_size": kv_cache.block_size,
    }


def import_sequence_kv(
    kv_cache: PagedKVCache, seq: Sequence, payload: dict
) -> int:
    """Write an exported KV payload into a fresh cache/sequence."""
    if payload["block_size"] != kv_cache.block_size:
        raise ValueError("source and destination block sizes differ")
    num_tokens = int(payload["num_tokens"])
    if num_tokens > seq.length:
        raise ValueError("payload is longer than the destination sequence")
    k = payload["k"]
    v = payload["v"]
    kv_cache.ensure_capacity(seq, num_tokens)
    for layer in range(kv_cache.num_layers):
        for token in range(num_tokens):
            block = token // kv_cache.block_size
            offset = token % kv_cache.block_size
            block_id = seq.block_table[block]
            if kv_cache.block_manager.blocks[block_id].ref_count > 1:
                kv_cache._cow_if_shared(seq, block)
                block_id = seq.block_table[block]
            kv_cache.k[layer, block_id, offset] = k[layer, token]
            kv_cache.v[layer, block_id, offset] = v[layer, token]
    seq.num_computed_tokens = num_tokens
    return int(k.nbytes + v.nbytes)


@dataclass
class PrefillTierBalancer:
    """Bucket requests by context length and assign engine quotas."""

    boundaries: tuple[int, ...] = (16_000, 64_000, 256_000)

    def tier(self, length: int) -> int:
        for index, boundary in enumerate(self.boundaries):
            if length < boundary:
                return index
        return len(self.boundaries)

    def assign(self, lengths: list[int], num_engines: int) -> list[int]:
        if num_engines < 1:
            raise ValueError("num_engines must be positive")
        tiers = [self.tier(length) for length in lengths]
        weights = np.zeros(len(self.boundaries) + 1, dtype=np.float64)
        for tier in tiers:
            weights[tier] += 1
        if weights.sum() == 0:
            return [0] * len(lengths)

        active = [index for index in range(len(weights)) if weights[index] > 0]
        quotas = np.zeros(len(weights), dtype=int)
        for index in active:
            quotas[index] = max(1, int(round(weights[index] / weights.sum() * num_engines)))
        # Trim quotas from the largest allocations, but never below 1 for an
        # active tier unless there are fewer engines than active tiers.
        while quotas.sum() > num_engines:
            candidates = [index for index in active if quotas[index] > 1]
            if not candidates:
                # More active tiers than engines: some tiers must share an
                # engine.  Give the surplus back proportionally by weight.
                keep = sorted(
                    active, key=lambda index: weights[index], reverse=True
                )[:num_engines]
                for index in active:
                    quotas[index] = 1 if index in keep else 0
                break
            victim = max(candidates, key=lambda index: quotas[index])
            quotas[victim] -= 1
        while quotas.sum() < num_engines:
            quotas[max(active, key=lambda index: weights[index])] += 1

        cursor = np.concatenate([[0], np.cumsum(quotas)])
        used = {index: 0 for index in active}
        assignment: list[int] = []
        for position, tier in enumerate(tiers):
            quota = int(quotas[tier])
            if quota <= 0:
                # No dedicated engine for this tier; spread across all
                # engines instead of sending everything to engine 0.
                assignment.append(position % num_engines)
            else:
                start = int(cursor[tier])
                assignment.append(start + used[tier] % quota)
                used[tier] += 1
        return assignment


@dataclass
class DecodeDualPathBalancer:
    """Balance request count and total KV length with one weighted score.

    This is a teaching approximation, not the production dual-path policy.
    The score makes the concurrency term grow with request length, so a long
    request is routed away from a busy rank; it does not implement two
    separate branches ("short -> low count", "long -> low KV").
    """

    def choose(self, ranks: list[dict[str, float]], request_length: int) -> int:
        """Pick the rank with the lowest combined normalised load."""
        if not ranks:
            raise ValueError("at least one rank is required")
        best = 0
        best_score = float("inf")
        for index, rank in enumerate(ranks):
            count = rank.get("count", 0)
            kv_len = rank.get("kv_len", 0)
            score = count * (1.0 + request_length / 4096.0) + kv_len / 8192.0
            if score < best_score:
                best_score = score
                best = index
        return best
