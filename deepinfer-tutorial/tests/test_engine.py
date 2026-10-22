from __future__ import annotations

import json

import numpy as np
import pytest

from mini_deepinfer.block_manager import BlockManager
from mini_deepinfer.cache_engine import PagedKVCache
from mini_deepinfer.config import EngineConfig
from mini_deepinfer.engine import LLMEngine
from mini_deepinfer.llm import LLM
from mini_deepinfer.sampling_params import SamplingParams
from mini_deepinfer.sequence import Sequence


def build_llm(model, draft_model=None, **overrides) -> LLM:
    values = dict(
        block_size=16,
        num_blocks=128,
        max_num_seqs=8,
        max_num_batched_tokens=256,
        max_model_len=256,
        enable_chunked_prefill=True,
    )
    values.update(overrides)
    config = EngineConfig(**values)
    return LLM(model=model, config=config, draft_model=draft_model)


def test_greedy_generation_is_deterministic(model) -> None:
    llm = build_llm(model)
    params = SamplingParams(temperature=0.0, max_tokens=16)
    first = llm.generate("DeepInfer is", params)[0]
    second = llm.generate("DeepInfer is", params)[0]
    assert first == second
    assert len(llm.engine.tokenizer.encode(first)) <= 16


def test_stop_preempted_requests_still_return_terminal_output(model) -> None:
    llm = build_llm(
        model,
        block_size=8,
        num_blocks=12,
        max_num_seqs=3,
        max_num_batched_tokens=64,
        max_model_len=64,
        stop_preempted_request=True,
    )
    prompts = ["DeepInfer is a framework"] * 3
    outputs = llm.generate(
        prompts,
        [SamplingParams(temperature=0.0, max_tokens=35) for _ in prompts],
        return_objects=True,
    )
    assert all(output.finished for output in outputs)
    assert all(output.finish_reason in {"preempted", "length"} for output in outputs)
    assert llm.engine.stats()["metrics"]["requests_finished"] == 3


def test_speculative_decoding_stops_at_max_model_len(model, draft_model) -> None:
    llm = build_llm(
        model,
        draft_model=draft_model,
        enable_speculative=True,
        speculative_tokens=4,
        confidence_threshold=0.0,
        block_size=8,
        num_blocks=32,
        max_num_batched_tokens=128,
        max_model_len=64,
    )
    prompt = "DeepInfer " * 5
    output = llm.generate(prompt, SamplingParams(temperature=0.0, max_tokens=20))[0]
    assert output
    assert llm.engine.has_unfinished_requests() is False


def test_prefills_are_interleaved_with_decode(model) -> None:
    # Eight short prompts, one admitted per prefill step.  A prefill must not
    # immediately schedule another prefill while requests are running.
    llm = build_llm(
        model,
        max_num_seqs=16,
        max_num_batched_tokens=16,
        max_model_len=16,
        enable_chunked_prefill=False,
    )
    engine = llm.engine
    for index in range(8):
        engine.add_request(f"prompt{index:02d}", SamplingParams(temperature=0.0, max_tokens=2))
    phases: list[str] = []
    while engine.has_unfinished_requests():
        before = engine.metrics.summary()
        engine.step()
        after = engine.metrics.summary()
        if after["prefill_tokens"] > before["prefill_tokens"]:
            phases.append("P")
        elif after["decode_tokens"] > before["decode_tokens"]:
            phases.append("D")
    assert phases.count("P") >= 2
    assert "PP" not in "".join(phases), phases


def test_add_request_accepts_token_id_list(model) -> None:
    llm = build_llm(model)
    engine = llm.engine
    tokens = engine.tokenizer.encode("DeepInfer is")
    seq = engine.add_request(tokens, SamplingParams(temperature=0.0, max_tokens=4))
    assert seq.prompt_token_ids == tokens
    while engine.has_unfinished_requests():
        engine.step()
    assert seq.is_finished()
    assert seq.num_output_tokens == 4


def test_batch_invariance(model) -> None:
    llm = build_llm(model)
    prompts = ["DeepInfer is", "Once upon a time", "The cat sat on", "A small robot"]
    params = [
        SamplingParams(temperature=0.8, top_p=0.9, max_tokens=12, seed=100 + i)
        for i in range(len(prompts))
    ]
    batched = llm.generate(prompts, params)
    sequential = [llm.generate(prompt, sp)[0] for prompt, sp in zip(prompts, params)]
    assert batched == sequential


def test_chunked_prefill_matches_full_prefill(model) -> None:
    prompt = "DeepInfer is an inference engine for large language models."
    full = build_llm(
        model,
        max_num_batched_tokens=256,
        enable_chunked_prefill=False,
        max_model_len=128,
    ).generate(prompt, SamplingParams(temperature=0.0, max_tokens=12))[0]
    chunked = build_llm(
        model,
        max_num_batched_tokens=16,
        block_size=8,
        enable_chunked_prefill=True,
        max_model_len=128,
    ).generate(prompt, SamplingParams(temperature=0.0, max_tokens=12))[0]
    assert full == chunked


def test_prefix_cache_hit_and_same_output(model) -> None:
    llm = build_llm(model, block_size=8, num_blocks=64, max_num_batched_tokens=32)
    prompt = "DeepInfer is an inference engine that"
    params = SamplingParams(temperature=0.0, max_tokens=8)
    first = llm.generate(prompt, params, return_objects=True)[0]
    second = llm.generate(prompt, params, return_objects=True)[0]
    assert second.prefix_cache_hit > 0
    assert first.text == second.text
    assert llm.engine.block_manager.cache_hits > 0


def test_preemption_still_finishes_requests(model) -> None:
    llm = build_llm(
        model,
        block_size=8,
        num_blocks=12,
        max_num_batched_tokens=48,
        max_num_seqs=4,
        max_model_len=80,
        stop_preempted_request=False,
    )
    outputs = llm.generate(
        [
            "A small robot lived at the edge of a village.",
            "The cat sat on the mat and looked at the dog.",
            "DeepInfer is an inference engine.",
        ],
        SamplingParams(temperature=0.7, max_tokens=35, seed=7),
    )
    assert len(outputs) == 3
    assert all(outputs)
    assert llm.engine.metrics.requests_finished == 3
    assert llm.engine.metrics.preemptions >= 1


def test_abort_request(model) -> None:
    llm = build_llm(model)
    seq = llm.engine.add_request("DeepInfer is", SamplingParams(max_tokens=32))
    llm.engine.step()
    assert llm.engine.abort_request(seq.request_id) == 1
    assert not llm.engine.has_unfinished_requests()


def test_abort_waiting_request_releases_prefix_blocks(model) -> None:
    llm = build_llm(model, block_size=8, num_blocks=64, max_num_batched_tokens=32)
    prompt = "DeepInfer is an inference engine that"
    llm.generate(prompt, SamplingParams(temperature=0.0, max_tokens=8))
    seq = llm.engine.add_request(prompt, SamplingParams(max_tokens=8))
    assert seq.block_table  # prefix-cache hit before the request runs
    assert llm.engine.abort_request(seq.request_id) == 1
    assert all(block.ref_count == 0 for block in llm.engine.block_manager.blocks)


def test_structured_regex(model) -> None:
    llm = build_llm(model)
    params = SamplingParams(
        temperature=0.8,
        max_tokens=16,
        seed=3,
        so_args={"schema_type": "regex", "schema": r"[a-z]+(?: [a-z]+)*"},
    )
    text = llm.generate("DeepInfer is", params)[0]
    import re

    assert re.fullmatch(r"[a-z]+(?: [a-z]+)*", text)


def test_structured_json(model) -> None:
    llm = build_llm(model)
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"],
        "additionalProperties": False,
    }
    params = SamplingParams(
        temperature=0.8,
        max_tokens=80,
        seed=4,
        so_args={"schema_type": "json", "schema": json.dumps(schema)},
    )
    text = llm.generate("DeepInfer is", params)[0]
    obj = json.loads(text)
    assert set(obj) == {"name", "age"}
    assert isinstance(obj["age"], int)


def test_structured_ebnf(model) -> None:
    llm = build_llm(model)
    params = SamplingParams(
        temperature=0.8,
        max_tokens=8,
        seed=5,
        so_args={"schema_type": "ebnf", "schema": 'start = "ok" | "yes" | "no" ;'},
    )
    assert llm.generate("DeepInfer is", params)[0] in {"ok", "yes", "no"}


def test_speculative_greedy_matches_target(model, draft_model) -> None:
    prompt = "DeepInfer is"
    params = SamplingParams(temperature=0.0, max_tokens=24)
    target = build_llm(model).generate(prompt, params)[0]
    speculative = build_llm(
        model,
        draft_model=draft_model,
        enable_speculative=True,
        speculative_tokens=4,
        confidence_threshold=0.0,
    ).generate(prompt, params)[0]
    assert speculative == target


def test_streaming_deltas_reconstruct_text(model) -> None:
    llm = build_llm(model)
    chunks: list[str] = []
    final = llm.generate(
        "DeepInfer is",
        SamplingParams(temperature=0.0, max_tokens=12),
        stream=lambda _rid, delta, _finished: chunks.append(delta),
    )[0]
    assert "".join(chunks) == final


def test_speculative_streaming_matches_final_at_max_tokens(model, draft_model) -> None:
    llm = build_llm(
        model,
        draft_model=draft_model,
        enable_speculative=True,
        speculative_tokens=4,
        confidence_threshold=0.0,
    )
    chunks: list[str] = []
    final = llm.generate(
        "DeepInfer is",
        SamplingParams(temperature=0.0, max_tokens=2),
        stream=lambda _rid, delta, _finished: chunks.append(delta),
    )[0]
    # The verifier may produce several tokens in one step, but only the
    # committed prefix (here capped by max_tokens) may be streamed.
    assert "".join(chunks) == final


def test_speculative_streaming_matches_final_with_stop_token(model, draft_model) -> None:
    llm = build_llm(
        model,
        draft_model=draft_model,
        enable_speculative=True,
        speculative_tokens=4,
        confidence_threshold=0.0,
    )
    stop_ids = llm.engine.tokenizer.encode(" ")
    chunks: list[str] = []
    final = llm.generate(
        "DeepInfer is",
        SamplingParams(
            temperature=0.0,
            max_tokens=8,
            stop_token_ids=stop_ids,
        ),
        stream=lambda _rid, delta, _finished: chunks.append(delta),
    )[0]
    assert "".join(chunks) == final


def test_metrics_and_stats_shape(model) -> None:
    llm = build_llm(model)
    llm.generate("DeepInfer is", SamplingParams(max_tokens=8, temperature=0.0))
    stats = llm.engine.stats()
    assert stats["metrics"]["requests_finished"] == 1
    assert stats["kv_cache_bytes"] > 0
    assert stats["block_manager"]["blocks"] == 128


def test_ignored_prompt_is_reported(model) -> None:
    llm = build_llm(model, max_model_len=8)
    result = llm.generate("this prompt is much longer than eight tokens", SamplingParams(max_tokens=4))
    assert result == [""]


def test_single_token_request_finishes_in_prefill(model) -> None:
    llm = build_llm(model)
    result = llm.generate(
        "DeepInfer is",
        SamplingParams(temperature=0.0, max_tokens=1),
        return_objects=True,
    )[0]
    assert result.finished
    assert result.output_tokens == 1
    assert not llm.engine.has_unfinished_requests()


def test_paged_forward_matches_full_forward(model) -> None:
    """The page-table path must be numerically identical to a dense forward."""
    ids = [65, 66, 67, 68, 69, 70]
    full, _, _ = model.forward_full(ids)
    seq = Sequence(0, "paged-parity", ids, SamplingParams(temperature=0.0, max_tokens=1))
    cache = PagedKVCache(
        model.config.n_layers,
        BlockManager(32, block_size=8),
        model.config.n_kv_heads,
        model.config.head_dim,
    )
    paged, _, _ = model.forward_paged(seq, 0, len(ids), cache)
    np.testing.assert_allclose(full, paged, atol=1e-5)
