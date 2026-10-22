"""Inspect sampling: temperature, top-k/top-p, seeds, stops and streaming.

The script uses a fixed logit vector so the output is deterministic and does
not depend on the toy checkpoint.  It is the runnable companion to chapter 11.
"""

from __future__ import annotations

import numpy as np

from _common import add_project_root

add_project_root()

from mini_deepinfer.sampling import distribution, sample_token
from mini_deepinfer.sampling_params import SamplingParams
from mini_deepinfer.sequence import Sequence
from mini_deepinfer.tokenizer import StreamingDetokenizer, Tokenizer


def banner(title: str) -> None:
    print(f"\n== {title} ==")


LOGITS = np.array([2.0, 1.0, 0.5, 0.0, -0.5, -1.0, -2.0, -4.0], dtype=np.float64)


def show_distribution(name: str, params: SamplingParams) -> None:
    probs = distribution(LOGITS, params)
    order = np.argsort(-probs)[:4]
    pairs = ", ".join(f"token {int(i)}={probs[i]:.4f}" for i in order if probs[i] > 0)
    print(f"{name:<26} {pairs}")


def main() -> None:
    banner("distribution shaping")
    show_distribution("greedy", SamplingParams(temperature=0.0))
    show_distribution("temperature=0.5", SamplingParams(temperature=0.5))
    show_distribution("temperature=1.0", SamplingParams(temperature=1.0))
    show_distribution("temperature=2.0", SamplingParams(temperature=2.0))
    show_distribution("top-k=3", SamplingParams(temperature=1.0, top_k=3))
    show_distribution("top-p=0.7", SamplingParams(temperature=1.0, top_p=0.7))

    banner("seeded sampling")
    params = SamplingParams(temperature=0.8, top_p=0.9, seed=1234)
    seq_a = Sequence(0, "a", [1], params)
    seq_b = Sequence(0, "b", [1], params)
    tokens_a = [sample_token(LOGITS, params, seq_a.rng)[0] for _ in range(5)]
    tokens_b = [sample_token(LOGITS, params, seq_b.rng)[0] for _ in range(5)]
    print("sequence a:", tokens_a)
    print("sequence b:", tokens_b)
    print("same seed, same stream:", tokens_a == tokens_b)

    banner("logprobs")
    logprob_params = SamplingParams(temperature=0.8, top_p=0.9, seed=1234, top_logprobs=3)
    token, logprob, top = sample_token(LOGITS, logprob_params, np.random.default_rng(7))
    print(f"sampled={token} logprob={logprob:.4f}")
    print("top logprobs:", [(i, round(p, 4)) for i, p in top])

    banner("stop conditions")
    tokenizer = Tokenizer()
    stop_params = SamplingParams(
        temperature=0.0,
        max_tokens=12,
        min_tokens=2,
        stop=("</answer>",),
    )
    seq = Sequence(0, "stop", [ord("x")], stop_params)
    for token_id in b"</answer>":
        seq.append_token(token_id, 0.0)
        if seq.check_stop(tokenizer):
            break
    text = tokenizer.decode(seq.output_token_ids)
    trimmed = text.split("</answer>", 1)[0]
    print("finish_reason:", seq.finish_reason)
    print("decoded:", text)
    print("text before stop:", repr(trimmed))
    print("number of output tokens:", len(seq.output_token_ids))

    banner("streaming UTF-8")
    stream = StreamingDetokenizer(tokenizer)
    pieces = []
    for token_id in "你好".encode("utf-8"):
        pieces.append(stream.feed(token_id))
    pieces.append(stream.flush())
    joined = "".join(pieces)
    print("delta lengths:", [len(piece) for piece in pieces])
    print("joined codepoints:", [hex(ord(ch)) for ch in joined])
    print("joined utf-8 bytes:", joined.encode("utf-8").hex())

    banner("what to notice")
    print("1. temperature changes the shape, not the ranking.")
    print("2. top-p keeps a variable number of tokens; top-k keeps a fixed number.")
    print("3. per-sequence RNGs make the stream independent of batch order.")
    print("4. a stop string can span tokens; check the decoded buffer, not one token.")
    print("5. a multi-byte character may arrive one byte at a time; buffer incomplete UTF-8.")


if __name__ == "__main__":
    main()
