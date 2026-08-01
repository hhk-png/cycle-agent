"""Train the tiny GPT model from scratch (pure NumPy) and save a checkpoint.

Usage:
    python scripts/train.py --out artifacts/tinygpt
    python scripts/train.py --steps 400 --out artifacts/tinygpt
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minivllm.checkpoint import save_checkpoint  # noqa: E402
from minivllm.config import EngineConfig, ModelConfig  # noqa: E402
from minivllm.data import CORPUS, REPEAT  # noqa: E402
from minivllm.model import TinyGPT  # noqa: E402
from minivllm.tokenizer import CharTokenizer  # noqa: E402
from minivllm.training import TinyGPTTrainer  # noqa: E402


def make_batches(data, seq_len, batch_size, rng):
    n = len(data)
    starts = list(range(0, n - seq_len - 1, seq_len))
    if not starts:
        raise ValueError("corpus too small")
    while True:
        rng.shuffle(starts)
        for i in range(0, len(starts) - batch_size + 1, batch_size):
            xs, ys = [], []
            for p in starts[i:i + batch_size]:
                chunk = data[p:p + seq_len + 1]
                xs.append(chunk[:-1])
                ys.append(chunk[1:])
            yield (np.asarray(xs, dtype=np.int64),
                   np.asarray(ys, dtype=np.int64))


def main():
    parser = argparse.ArgumentParser(description="Train a tiny GPT in NumPy")
    parser.add_argument("--out", type=str, default="artifacts/tinygpt")
    parser.add_argument("--steps", type=int, default=400)
    parser.add_argument("--lr", type=float, default=0.02)
    parser.add_argument("--seq-len", type=int, default=48)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--embd", type=int, default=64)
    parser.add_argument("--layers", type=int, default=2)
    parser.add_argument("--heads", type=int, default=4)
    args = parser.parse_args()

    tokenizer = CharTokenizer()
    corpus = CORPUS * REPEAT
    data = tokenizer.encode(corpus) + [tokenizer.eos_token_id]
    print(f"[train] corpus: {len(corpus)} chars, vocab: {tokenizer.vocab_size}")

    model_cfg = ModelConfig(
        vocab_size=tokenizer.vocab_size,
        n_embd=args.embd,
        n_layer=args.layers,
        n_head=args.heads,
        head_dim=args.embd // args.heads,
        block_size=128,
    )
    model = TinyGPT(model_cfg, seed=0)
    trainer = TinyGPTTrainer(model, lr=args.lr)
    batches = make_batches(data, args.seq_len, args.batch_size,
                           np.random.default_rng(0))

    t0 = time.time()
    for step in range(1, args.steps + 1):
        x, y = next(batches)
        loss = trainer.train_step(x, y)
        if step == 1 or step % 50 == 0 or step == args.steps:
            print(f"[train] step {step:4d}/{args.steps}  loss {loss:.4f}  "
                  f"({time.time() - t0:6.1f}s)", flush=True)

    cfg = EngineConfig(model=model_cfg)
    save_checkpoint(model, cfg, tokenizer, args.out)
    print(f"[train] checkpoint saved to {args.out}")


if __name__ == "__main__":
    main()
