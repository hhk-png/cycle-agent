"""Train the tiny checkpoint used by the tutorial.

Usage:
    python train_tiny.py --steps 2500 --out mini_deepinfer/assets/tiny_lm.npz
    python train_tiny.py --quick

Only NumPy is required.  On a laptop CPU the default run takes a few minutes.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from mini_deepinfer.config import ModelConfig
from mini_deepinfer.corpus import corpus_bytes
from mini_deepinfer.model.ops import rope_tables
from mini_deepinfer.model.training import Adam, clip_grads, forward_train, init_weights, loss_and_grads
from mini_deepinfer.model.tiny_transformer import TinyTransformer


def make_config(args) -> ModelConfig:
    return ModelConfig(
        vocab_size=264,
        d_model=args.d_model,
        n_layers=args.layers,
        n_heads=args.heads,
        n_kv_heads=args.kv_heads,
        head_dim=args.d_model // args.heads,
        ffn_dim=args.ffn,
        max_seq_len=512,
        window_size=args.window,
        swa_layers=(0,),
    )


def batch_from(data: np.ndarray, batch: int, seq_len: int, rng: np.random.Generator):
    if len(data) <= seq_len + 1:
        raise ValueError("corpus is too small for the requested sequence length")
    starts = rng.integers(0, len(data) - seq_len - 1, size=batch)
    ids = np.stack([data[s : s + seq_len] for s in starts]).astype(np.int64)
    targets = np.stack([data[s + 1 : s + seq_len + 1] for s in starts]).astype(np.int64)
    return ids, targets


def evaluate(weights, config, data, rng, *, batches=4, batch=8, seq_len=48, cos=None, sin=None):
    losses = []
    for _ in range(batches):
        ids, targets = batch_from(data, batch, seq_len, rng)
        logits, _ = forward_train(weights, config, ids, cos, sin, need_cache=False)
        shifted = logits - np.max(logits, axis=-1, keepdims=True)
        log_probs = shifted - np.log(np.sum(np.exp(shifted), axis=-1, keepdims=True))
        losses.append(
            float(-np.mean(log_probs.reshape(-1, log_probs.shape[-1])[
                np.arange(batch * seq_len), targets.reshape(-1)
            ]))
        )
    return float(np.mean(losses))


def generate_sample(weights, config, prompt: str, max_new_tokens: int = 80) -> str:
    model = TinyTransformer(config, weights)
    ids = list(prompt.encode("utf-8"))
    for _ in range(max_new_tokens):
        logits, _, _ = model.forward_full(ids)
        next_id = int(np.argmax(logits[-1]))
        ids.append(next_id)
        if next_id == 258:  # <|eos|>
            break
    return bytes(i for i in ids if i < 256).decode("utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=2500)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--seq-len", type=int, default=48)
    parser.add_argument("--lr", type=float, default=3e-3)
    parser.add_argument("--d-model", type=int, default=64)
    parser.add_argument("--layers", type=int, default=2)
    parser.add_argument("--heads", type=int, default=4)
    parser.add_argument("--kv-heads", type=int, default=2)
    parser.add_argument("--ffn", type=int, default=128)
    parser.add_argument("--window", type=int, default=64)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--chars", type=int, default=120_000)
    parser.add_argument("--out", type=Path, default=Path("mini_deepinfer/assets/tiny_lm.npz"))
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        args.steps = min(args.steps, 300)
        args.chars = min(args.chars, 20_000)

    rng = np.random.default_rng(args.seed)
    data = np.frombuffer(corpus_bytes(args.seed, args.chars), dtype=np.uint8).astype(np.int64)
    split = int(len(data) * 0.95)
    train_data, val_data = data[:split], data[split:]
    config = make_config(args)
    weights = init_weights(config, seed=args.seed)
    cos, sin = rope_tables(config.max_seq_len, config.head_dim, config.rope_theta)
    optimizer = Adam(weights, lr=args.lr)
    warmup = max(10, args.steps // 20)

    print(
        f"corpus={len(data)} chars, params={sum(v.size for v in weights.values()):,}, "
        f"config={config.d_model}d/{config.n_layers}L"
    )
    started = time.perf_counter()
    best_val = float("inf")
    for step in range(1, args.steps + 1):
        # Warmup + cosine decay.
        if step <= warmup:
            lr = args.lr * step / warmup
        else:
            progress = (step - warmup) / max(1, args.steps - warmup)
            lr = args.lr * (0.1 + 0.9 * 0.5 * (1.0 + np.cos(np.pi * progress)))
        optimizer.lr = lr

        ids, targets = batch_from(train_data, args.batch, args.seq_len, rng)
        loss, grads = loss_and_grads(weights, config, ids, targets, cos, sin)
        clip_grads(grads, 1.0)
        optimizer.step(grads)

        if step % 100 == 0 or step == 1:
            val = evaluate(weights, config, val_data, rng, cos=cos, sin=sin)
            best_val = min(best_val, val)
            print(
                f"step {step:5d} loss {loss:.4f} val {val:.4f} lr {lr:.2e} "
                f"elapsed {time.perf_counter() - started:.1f}s"
            )
        if step % 500 == 0:
            print("sample:", generate_sample(weights, config, "Once upon a time", 60).replace("\n", " "))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"config_json": np.array(json.dumps(config.__dict__, default=list))}
    payload.update(weights)
    np.savez_compressed(args.out, **payload)
    print(f"saved {args.out} ({args.out.stat().st_size / 1024:.0f} KiB), best val {best_val:.4f}")
    print("sample:", generate_sample(weights, config, "DeepInfer is", 80).replace("\n", " "))


if __name__ == "__main__":
    main()
