"""Speculative decoding (greedy target) with a bigram draft model.

Speculative decoding is a *lossless* acceleration trick: a cheap **draft**
model proposes K tokens, the expensive **target** model verifies all K in a
single forward pass, and only the longest verified prefix is kept (plus one
"bonus" token from the target).  Real vLLM implements this in
``vllm/spec_decode/`` using a draft worker (e.g. ``EagleWorker`` /
``MedusaWorker``) and a target worker that verifies the drafts in parallel.

Here the draft model is a character bigram, so the demo illustrates the
*algorithm* (draft -> verify -> accept/reject) even though a bigram is too
weak to be any faster.  The verification step reuses the exact same paged KV
cache as normal decoding, which is the important part.
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from .kv_cache import BlockManager
from .model import TinyGPT
from .sequence import Sequence
from .tokenizer import CharTokenizer


class BigramDraftModel:
    """A character bigram language model (Laplace-smoothed counts)."""

    def __init__(self, tokenizer: CharTokenizer, corpus: str):
        v = tokenizer.vocab_size
        ids = tokenizer.encode(corpus)
        counts = np.full((v, v), 0.5, dtype=np.float64)  # Laplace smoothing
        for a, b in zip(ids, ids[1:]):
            counts[a, b] += 1.0
        self.log_probs = np.log(counts / counts.sum(axis=1, keepdims=True))

    def draft(self, context: List[int], num_tokens: int) -> List[int]:
        """Greedily propose ``num_tokens`` tokens from the current context."""
        tokens: List[int] = []
        cur = context[-1] if context else 0
        for _ in range(num_tokens):
            cur = int(np.argmax(self.log_probs[cur]))
            tokens.append(cur)
        return tokens


def _forward_one(model: TinyGPT, block_manager: BlockManager, seq: Sequence,
                 token_id: int, position: int) -> np.ndarray:
    x = np.asarray([token_id], dtype=np.int64).reshape(1, 1)
    pos = np.asarray([position], dtype=np.int64).reshape(1, 1)
    return model.forward(x, pos, [seq], block_manager)[0, -1, :]


def speculative_generate(
    model: TinyGPT,
    tokenizer: CharTokenizer,
    block_manager: BlockManager,
    draft_model: BigramDraftModel,
    prompt_ids: List[int],
    max_new_tokens: int,
    num_speculative_tokens: int = 5,
) -> dict:
    """Run greedy speculative decoding for one sequence.

    Returns a dict with ``output_ids``, ``draft_count``, ``accepted_count``,
    ``target_forwards`` and ``steps`` so the demo can report acceptance rates.
    """
    seq = Sequence(seq_id=0, prompt_ids=list(prompt_ids), sampling_params=None)  # type: ignore[arg-type]
    block_manager.ensure_blocks(seq, len(prompt_ids))

    # --- prefill the prompt -------------------------------------------------
    x = np.asarray(prompt_ids, dtype=np.int64).reshape(1, -1)
    pos = np.arange(len(prompt_ids), dtype=np.int64).reshape(1, -1)
    logits = model.forward(x, pos, [seq], block_manager)[0]  # (L, V)
    seq.cached_len = len(prompt_ids)
    # last_logits predicts the token after the prompt
    last_logits = logits[-1]

    target_forwards = 1  # the prefill pass
    steps = 0
    draft_count = 0
    accepted_total = 0

    while len(seq.output_ids) < max_new_tokens:
        steps += 1
        cur_len = len(seq.all_ids)          # == seq.cached_len (invariant)
        K = min(num_speculative_tokens, max_new_tokens - len(seq.output_ids))

        # 1) draft K tokens (they will occupy positions cur_len..cur_len+K)
        draft = draft_model.draft(seq.all_ids, K)
        draft_count += len(draft)

        # 2) verify all K in a single target forward
        block_manager.ensure_blocks(seq, cur_len + K)
        seq.cached_len = cur_len            # cached prefix before the drafts
        dx = np.asarray(draft, dtype=np.int64).reshape(1, -1)
        dpos = np.arange(cur_len, cur_len + K, dtype=np.int64).reshape(1, -1)
        vlogits = model.forward(dx, dpos, [seq], block_manager)[0]  # (K, V)
        target_forwards += 1

        # 3) figure out which drafts are accepted
        # predictions[j] is the target's choice for position cur_len+j
        preds = [int(np.argmax(last_logits))]
        preds += [int(np.argmax(vlogits[j])) for j in range(K - 1)]
        bonus = int(np.argmax(vlogits[K - 1]))  # for position cur_len + K

        m = 0
        while m < K and draft[m] == preds[m]:
            m += 1

        # 4) keep accepted drafts + the verifier's bonus token
        if m < K:
            kept = draft[:m] + [preds[m]]
        else:
            kept = draft[:K] + [bonus]
        kept = kept[: max_new_tokens - len(seq.output_ids)]

        for t in kept:
            seq.output_ids.append(t)
        accepted_total += m

        # 5) repair the KV cache around the bonus position so the next step
        #    (and future drafts) see a consistent cache.
        #    The bonus token is at position cur_len + m (m < K) or
        #    cur_len + K (m == K).  Re-run a single forward for it.
        if len(seq.output_ids) >= max_new_tokens:
            break
        if m < K:
            bonus_pos, bonus_token = cur_len + m, preds[m]
        else:
            bonus_pos, bonus_token = cur_len + K, bonus
        seq.cached_len = bonus_pos            # bonus attends to everything before it
        last_logits = _forward_one(model, block_manager, seq, bonus_token, bonus_pos)
        target_forwards += 1
        seq.cached_len = len(seq.all_ids)     # all real tokens are now cached

    return {
        "output_ids": list(seq.output_ids),
        "draft_count": draft_count,
        "accepted_count": accepted_total,
        "target_forwards": target_forwards,
        "steps": steps,
    }
