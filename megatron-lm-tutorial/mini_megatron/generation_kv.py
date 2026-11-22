"""Autoregressive decoding with a per-layer KV cache (educational build).

``generation.py`` recomputes the entire prefix for every new token.  This
module keeps the K/V of every layer and feeds only the new token, which is the
core of every production inference engine:

    prefill(tokens) -> logits + cache
    decode(next_token, cache) -> logits + updated cache

The implementation is deliberately small: one request, PP=1, no paged memory,
no continuous batching, no prefix sharing.  It exists to make the subtle parts
visible and testable: global RoPE positions, cache concatenation order, TP head
sharding, and the fact that cached logits must equal a full forward pass.
"""
from __future__ import annotations

from typing import Optional

import torch

from .generation import _gather_vocab, sample_next_token
from .parallel.state import (
    get_context_parallel_world_size,
    get_pipeline_model_parallel_world_size,
)


@torch.no_grad()
def generate_with_cache(
    stage,
    input_ids: torch.Tensor,
    *,
    max_new_tokens: int = 32,
    temperature: float = 1.0,
    top_k: Optional[int] = None,
    top_p: Optional[float] = None,
    eos_token_id: Optional[int] = None,
    generator: Optional[torch.Generator] = None,
) -> torch.Tensor:
    """Greedy/sampling decoding with a KV cache.

    Only PP=1 and CP=1 are supported.  The stage must have been built without
    sequence parallelism (the CLI does this for generation): appending one token
    at a time would otherwise violate the ``seq % tp == 0`` layout invariant.
    """
    if get_pipeline_model_parallel_world_size() > 1:
        raise NotImplementedError(
            "mini KV-cache decoding supports PP=1; see chapter 40 for the "
            "production prefill/decode and pipeline designs"
        )
    if get_context_parallel_world_size() > 1:
        raise NotImplementedError("mini KV-cache decoding does not support CP")
    if getattr(stage.parallel, "sequence_parallel", False):
        raise NotImplementedError(
            "build the inference stage with sequence_parallel=False"
        )

    stage.eval()
    device = next(stage.parameters()).device
    ids = input_ids.to(device)
    finished = torch.zeros(ids.size(0), dtype=torch.bool, device=ids.device)

    out = stage(input_ids=ids, return_logits=True, use_cache=True)
    if out.past_key_values is None or out.logits is None:
        raise RuntimeError("stage did not return logits/cache")
    cache = out.past_key_values
    past_len = ids.size(1)

    for step in range(max_new_tokens):
        logits = _gather_vocab(out.logits[:, -1, :].float())
        next_token = sample_next_token(
            logits,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            generator=generator,
        )
        if eos_token_id is not None:
            next_token = torch.where(
                finished.unsqueeze(-1),
                torch.full_like(next_token, eos_token_id),
                next_token,
            )
            finished = finished | (next_token.squeeze(-1) == eos_token_id)
        ids = torch.cat((ids, next_token), dim=1)

        if step + 1 >= max_new_tokens or bool(finished.all()):
            break

        out = stage(
            input_ids=next_token,
            return_logits=True,
            use_cache=True,
            past_key_values=cache,
            position_offset=past_len,
        )
        if out.past_key_values is None or out.logits is None:
            raise RuntimeError("stage did not return logits/cache during decode")
        cache = out.past_key_values
        past_len += 1

    return ids


@torch.no_grad()
def cached_step(
    stage,
    input_ids: torch.Tensor,
    *,
    past_key_values: Optional[list] = None,
    position_offset: int = 0,
):
    """One forward step with an optional cache; returns ``(logits, cache)``."""
    out = stage(
        input_ids=input_ids,
        return_logits=True,
        use_cache=True,
        past_key_values=past_key_values,
        position_offset=position_offset,
    )
    if out.logits is None or out.past_key_values is None:
        raise RuntimeError("stage did not return logits/cache")
    return out.logits, out.past_key_values
