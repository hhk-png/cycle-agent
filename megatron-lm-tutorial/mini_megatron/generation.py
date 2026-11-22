"""Autoregressive sampling for the single-stage (PP=1) mini model."""
from __future__ import annotations

from typing import Optional

import torch

from .parallel import comm
from .parallel.state import (
    get_tensor_model_parallel_group,
    get_tensor_model_parallel_world_size,
    get_pipeline_model_parallel_world_size,
    get_context_parallel_world_size,
)


def _gather_vocab(logits: torch.Tensor) -> torch.Tensor:
    world = get_tensor_model_parallel_world_size()
    if world == 1:
        return logits
    logits = logits.contiguous()
    pieces = [torch.empty_like(logits) for _ in range(world)]
    comm.all_gather(pieces, logits, group=get_tensor_model_parallel_group())
    return torch.cat(pieces, dim=-1)


def sample_next_token(
    logits: torch.Tensor,
    *,
    temperature: float = 1.0,
    top_k: Optional[int] = None,
    top_p: Optional[float] = None,
    generator: Optional[torch.Generator] = None,
) -> torch.Tensor:
    """Sample one token per row from full-vocabulary logits ``[b, V]``."""
    if temperature <= 0:
        return logits.argmax(dim=-1, keepdim=True)
    logits = logits / temperature
    if top_k is not None and top_k > 0:
        k = min(top_k, logits.size(-1))
        values, _ = torch.topk(logits, k)
        threshold = values[:, -1].unsqueeze(-1)
        logits = logits.masked_fill(logits < threshold, float("-inf"))
    if top_p is not None and 0 < top_p < 1:
        sorted_logits, sorted_idx = torch.sort(logits, descending=True, dim=-1)
        probs = torch.softmax(sorted_logits, dim=-1)
        cumulative = torch.cumsum(probs, dim=-1)
        remove = cumulative - probs > top_p
        sorted_logits = sorted_logits.masked_fill(remove, float("-inf"))
        logits = torch.full_like(logits, float("-inf")).scatter(
            1, sorted_idx, sorted_logits
        )
    probs = torch.softmax(logits, dim=-1)
    return torch.multinomial(probs, num_samples=1, generator=generator)


@torch.no_grad()
def generate(
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
    if get_pipeline_model_parallel_world_size() > 1:
        raise NotImplementedError(
            "mini generation supports PP=1; see the tutorial chapter on "
            "pipeline-parallel decoding for the production design"
        )
    if get_context_parallel_world_size() > 1:
        raise NotImplementedError("mini generation does not combine CP with KV-cache decoding")
    stage.eval()
    device = next(stage.parameters()).device
    ids = input_ids.to(device)
    finished = torch.zeros(ids.size(0), dtype=torch.bool, device=ids.device)
    for _ in range(max_new_tokens):
        out = stage(input_ids=ids, return_logits=True)
        assert out.logits is not None
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
        if bool(finished.all()):
            break
    return ids
