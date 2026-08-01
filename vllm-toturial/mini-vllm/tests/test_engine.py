"""End-to-end tests for the engine: determinism, batching equivalence,
chunked prefill, prefix caching, preemption and streaming."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minivllm.config import (  # noqa: E402
    CacheConfig,
    EngineConfig,
    ModelConfig,
    SamplingParams,
    SchedulerConfig,
)
from minivllm.engine import LLMEngine  # noqa: E402
from minivllm.tokenizer import CharTokenizer  # noqa: E402


def make_engine(**overrides):
    tok = CharTokenizer()
    model_cfg = ModelConfig(vocab_size=tok.vocab_size, n_embd=32, n_layer=2,
                            n_head=4, head_dim=8, block_size=64)
    cfg = EngineConfig(
        model=model_cfg,
        cache=CacheConfig(block_size=8, num_gpu_blocks=64),
        scheduler=SchedulerConfig(max_num_seqs=8, max_num_batched_tokens=256),
        seed=0,
        enable_prefix_caching=overrides.pop("prefix", True),
    )
    for k, v in overrides.items():
        if k == "num_blocks":
            cfg.cache.num_gpu_blocks = v
        elif k == "max_batched_tokens":
            cfg.scheduler.max_num_batched_tokens = v
        elif k == "max_seqs":
            cfg.scheduler.max_num_seqs = v
        else:
            setattr(cfg, k, v)
    return LLMEngine(cfg, tokenizer=tok)


def gen_text(engine, prompt, max_tokens=20, **sp):
    params = SamplingParams(seed=1234, temperature=0.8,
                            max_tokens=max_tokens, **sp)
    out = engine.generate(prompt, params)
    return out.outputs[0].text, out.outputs[0].token_ids


def test_deterministic():
    e1, e2 = make_engine(), make_engine()
    t1, ids1 = gen_text(e1, "the quick brown fox")
    t2, ids2 = gen_text(e2, "the quick brown fox")
    assert ids1 == ids2, "identical engines must produce identical tokens"
    assert t1 == t2


def test_batching_equivalent_to_solo():
    prompts = ["the quick brown", "a fox jumps over"]
    # solo
    solo = []
    for p in prompts:
        e = make_engine()
        _, ids = gen_text(e, p)
        solo.append(ids)
    # batched (two requests in one engine)
    e = make_engine()
    params = SamplingParams(seed=1234, temperature=0.8, max_tokens=20)
    for p in prompts:
        e.add_request(p, params)
    batched = {req.request_id: req for step in range(500)
               for req in e.step()}
    got = []
    for p in prompts:
        # find the output for each prompt
        for req in batched.values():
            if req.prompt == p:
                got.append(req.outputs[0].token_ids)
                break
    assert got == solo, f"batching changed the output:\n solo={solo}\n batched={got}"


def test_chunked_prefill_matches_full():
    prompt = "vllm uses paged attention to store the kv cache in blocks"
    full = make_engine(max_batched_tokens=256)
    _, ids_full = gen_text(full, prompt, max_tokens=15)
    chunked = make_engine(max_batched_tokens=6)  # tiny budget -> many chunks
    _, ids_chunked = gen_text(chunked, prompt, max_tokens=15)
    assert ids_full == ids_chunked, \
        f"chunked prefill changed output:\n full={ids_full}\n chunked={ids_chunked}"


def test_prefix_caching_reuses_blocks():
    prompt = "the kv cache stores the keys and the values of every token"
    tok = CharTokenizer()
    cfg = EngineConfig(
        model=ModelConfig(vocab_size=tok.vocab_size, n_embd=32, n_layer=2,
                          n_head=4, head_dim=8, block_size=64),
        cache=CacheConfig(block_size=8, num_gpu_blocks=64),
        enable_prefix_caching=True,
    )
    e = LLMEngine(cfg, tokenizer=tok)
    prompt_ids = tok.encode(prompt)

    # first request fills its blocks and registers the prefix on finish
    out = e.generate(prompt, SamplingParams(temperature=0, max_tokens=8))
    assert out.finished
    n_free_before = e.block_manager.num_free_blocks

    # second request with the same prompt should reuse the cached prefix
    e.add_request(prompt, SamplingParams(temperature=0, max_tokens=8))
    seq = e.seqs[e.scheduler.waiting[0].seq_id] if e.scheduler.waiting else None
    # force admission by stepping until the seq is running
    for _ in range(10):
        e.step()
        if seq is None or seq.state != "WAITING":
            break
    # the prefix should have been matched
    prefix_len, blocks = e.block_manager.match_prefix(prompt_ids)
    assert prefix_len == len(prompt_ids), f"expected full prefix match, got {prefix_len}"


def test_preemption_recovers():
    """Oversubscribed KV cache + several long prompts must still finish."""
    tok = CharTokenizer()
    cfg = EngineConfig(
        model=ModelConfig(vocab_size=tok.vocab_size, n_embd=32, n_layer=2,
                          n_head=4, head_dim=8, block_size=64),
        # 20 blocks * 8 slots = 160 tokens; prompts below are ~60 tokens each
        # and 6 of them *cannot* all be resident at once -> preemption runs.
        cache=CacheConfig(block_size=8, num_gpu_blocks=20),
        scheduler=SchedulerConfig(max_num_seqs=4, max_num_batched_tokens=64),
        enable_prefix_caching=False,
        seed=0,
    )
    e = LLMEngine(cfg, tokenizer=tok)
    prompts = [
        "preemption is triggered when the kv cache runs out of blocks",
        "the scheduler evicts a running sequence and recomputes it later",
        "this is the recompute mode of vllm preemption",
        "every sequence must eventually finish even under memory pressure",
        "the block manager frees the blocks of the preempted sequence",
        "chunked prefill splits the prompt into several chunks",
    ]
    for p in prompts:
        e.add_request(p, SamplingParams(temperature=0, max_tokens=10))

    steps = 0
    while e.has_pending() and steps < 20000:
        e.step()
        steps += 1
    assert not e.has_pending(), "engine did not drain all requests"
    for seq in e.seqs.values():
        assert seq.is_finished, f"seq {seq.seq_id} did not finish"


def test_streaming_yields_tokens():
    e = make_engine()
    got = [o.outputs[0].text for o in e.generate_stream(
        "streaming produces tokens", SamplingParams(temperature=0, max_tokens=10))]
    assert got, "streaming produced no output"
    # cumulative text grows by appending, so each earlier snapshot is a prefix
    for prev, cur in zip(got, got[1:]):
        assert cur.startswith(prev), f"{cur!r} does not extend {prev!r}"


if __name__ == "__main__":
    test_deterministic()
    test_batching_equivalent_to_solo()
    test_chunked_prefill_matches_full()
    test_prefix_caching_reuses_blocks()
    test_preemption_recovers()
    test_streaming_yields_tokens()
    print("test_engine: OK")
