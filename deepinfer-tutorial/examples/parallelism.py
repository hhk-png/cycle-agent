"""Context parallel, expert parallel and a prefill -> decode KV handoff."""

from __future__ import annotations

import numpy as np

from _common import add_project_root

add_project_root()

from mini_deepinfer.block_manager import BlockManager
from mini_deepinfer.cache_engine import PagedKVCache
from mini_deepinfer.model.ops import attention, causal_mask, softmax
from mini_deepinfer.model.tiny_transformer import load_model
from mini_deepinfer.parallel import (
    Expert,
    context_parallel_attention,
    expert_parallel_moe,
    export_sequence_kv,
    import_sequence_kv,
    tensor_parallel_linear,
    tensor_parallel_mlp,
)
from mini_deepinfer.sampling_params import SamplingParams
from mini_deepinfer.sequence import Sequence
from pathlib import Path


def main() -> None:
    rng = np.random.default_rng(0)
    q = rng.normal(size=(8, 4, 16)).astype(np.float32)
    k = rng.normal(size=(8, 4, 16)).astype(np.float32)
    v = rng.normal(size=(8, 4, 16)).astype(np.float32)
    mask = causal_mask(np.arange(8), 8)
    reference = attention(q, k, v, mask)
    cp_out, moved = context_parallel_attention(q, k, v, mask, cp_size=2)
    print("context parallel max error:", float(np.max(np.abs(cp_out - reference))))
    print("context parallel bytes gathered:", moved)

    hidden = rng.normal(size=(6, 32)).astype(np.float32)
    gate = (rng.normal(size=(32, 4)) * 0.1).astype(np.float32)
    experts = [
        Expert(
            (rng.normal(size=(32, 48)) * 0.1).astype(np.float32),
            (rng.normal(size=(32, 48)) * 0.1).astype(np.float32),
            (rng.normal(size=(48, 32)) * 0.1).astype(np.float32),
        )
        for _ in range(4)
    ]
    ep_out, stats = expert_parallel_moe(hidden, gate, experts, 2, ep_size=2)
    probs = softmax(hidden @ gate, axis=-1)
    top = np.argsort(-probs, axis=-1)[:, :2]
    weights = np.take_along_axis(probs, top, axis=-1)
    weights /= weights.sum(axis=-1, keepdims=True)
    local = np.zeros_like(hidden)
    for token in range(6):
        for slot, expert in enumerate(top[token]):
            local[token] += weights[token, slot] * experts[expert](hidden[token : token + 1])[0]
    print("expert parallel max error:", float(np.max(np.abs(ep_out - local))))
    print("expert dispatch:", stats)

    x = rng.normal(size=(6, 32)).astype(np.float32)
    weight = (rng.normal(size=(32, 48)) * 0.1).astype(np.float32)
    reference_linear = x @ weight
    for tp_size in (1, 2, 4):
        column, column_stats = tensor_parallel_linear(
            x, weight, tp_size=tp_size, mode="column"
        )
        row, row_stats = tensor_parallel_linear(
            x, weight, tp_size=tp_size, mode="row"
        )
        print(
            f"TP={tp_size} column error:",
            float(np.max(np.abs(column - reference_linear))),
            "bytes:", sum(stat.communicated_bytes for stat in column_stats),
        )
        print(
            f"TP={tp_size} row error   :",
            float(np.max(np.abs(row - reference_linear))),
            "bytes:", sum(stat.communicated_bytes for stat in row_stats),
        )

    w1 = (rng.normal(size=(32, 64)) * 0.1).astype(np.float32)
    w3 = (rng.normal(size=(32, 64)) * 0.1).astype(np.float32)
    w2 = (rng.normal(size=(64, 32)) * 0.1).astype(np.float32)
    from mini_deepinfer.model.ops import silu

    reference_mlp = (silu(x @ w1) * (x @ w3)) @ w2
    for tp_size in (1, 2, 4):
        mlp, moved = tensor_parallel_mlp(x, w1, w3, w2, tp_size=tp_size)
        print(
            f"TP={tp_size} MLP error    :",
            float(np.max(np.abs(mlp - reference_mlp))),
            "bytes:", moved,
        )

    model = load_model(Path(__file__).resolve().parents[1] / "mini_deepinfer/assets/tiny_lm.npz")
    prompt = [65, 66, 67, 68]
    source = Sequence(1, "prefill", prompt, SamplingParams(temperature=0.0, max_tokens=4))
    source_cache = PagedKVCache(model.config.n_layers, BlockManager(32, 8), model.config.n_kv_heads, model.config.head_dim)
    logits, _, _ = model.forward_paged(source, 0, len(prompt), source_cache)
    source.num_computed_tokens = len(prompt)
    payload = export_sequence_kv(source_cache, source)
    target = Sequence(2, "decode", prompt, SamplingParams(temperature=0.0, max_tokens=4))
    target_cache = PagedKVCache(model.config.n_layers, BlockManager(32, 8), model.config.n_kv_heads, model.config.head_dim)
    moved = import_sequence_kv(target_cache, target, payload)
    print("PD handoff bytes:", moved, "target computed:", target.num_computed_tokens)
    next_token = int(np.argmax(logits[-1]))
    source.append_token(next_token)
    target.append_token(next_token)
    a, _, _ = model.forward_paged(source, len(prompt), len(prompt) + 1, source_cache)
    b, _, _ = model.forward_paged(target, len(prompt), len(prompt) + 1, target_cache)
    print("decode-after-handoff max error:", float(np.max(np.abs(a - b))))


if __name__ == "__main__":
    main()
