import torch

from mini_megatron.config import ExperimentConfig
from mini_megatron.generation import generate
from mini_megatron.generation_kv import cached_step, generate_with_cache
from mini_megatron.parallel.state import initialize_model_parallel
from mini_megatron.parallel.transformer import TransformerStage


def _stage():
    initialize_model_parallel(1, 1, 1)
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    torch.manual_seed(5)
    return cfg, TransformerStage(cfg.model, cfg.parallel).eval()


def test_cached_logits_match_full_forward():
    cfg, stage = _stage()
    prompt = torch.tensor([[1, 2, 3, 4, 5]])
    full = stage(input_ids=prompt, return_logits=True).logits
    cached, cache = cached_step(stage, prompt)
    assert torch.allclose(cached, full, atol=1e-6)
    assert cache[0][0].size(2) == prompt.size(1)

    ids = prompt
    for i in range(3):
        next_token = cached[:, -1, :].argmax(dim=-1, keepdim=True)
        ids = torch.cat((ids, next_token), dim=1)
        expected = stage(input_ids=ids, return_logits=True).logits[:, -1, :]
        cached, cache = cached_step(
            stage,
            next_token,
            past_key_values=cache,
            position_offset=prompt.size(1) + i,
        )
        assert cache[0][0].size(2) == ids.size(1)
        assert torch.allclose(cached[:, -1, :], expected, atol=1e-5)


def test_generate_with_cache_matches_full_recompute():
    cfg, stage = _stage()
    prompt = torch.tensor([[1, 2, 3]])
    a = generate(stage, prompt, max_new_tokens=6, temperature=0)
    b = generate_with_cache(stage, prompt, max_new_tokens=6, temperature=0)
    assert torch.equal(a, b), (a, b)
    assert b.size(1) == prompt.size(1) + 6


def test_cache_sampling_is_reproducible_with_generator():
    cfg, stage = _stage()
    prompt = torch.tensor([[1, 2, 3]])
    g1 = torch.Generator().manual_seed(11)
    g2 = torch.Generator().manual_seed(11)
    a = generate_with_cache(stage, prompt, max_new_tokens=4, temperature=0.8, generator=g1)
    b = generate_with_cache(stage, prompt, max_new_tokens=4, temperature=0.8, generator=g2)
    assert torch.equal(a, b)
