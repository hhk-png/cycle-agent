"""Tests for chapter 11: distribution shaping, seeds, stops and streaming."""

from __future__ import annotations

import numpy as np
import pytest

from mini_deepinfer.sampling import distribution, sample_token
from mini_deepinfer.sampling_params import SamplingParams
from mini_deepinfer.sequence import Sequence
from mini_deepinfer.tokenizer import StreamingDetokenizer, Tokenizer


LOGITS = np.array([2.0, 1.0, 0.5, 0.0, -0.5, -1.0, -2.0, -4.0], dtype=np.float64)


def test_temperature_sharpens_the_distribution() -> None:
    cold = distribution(LOGITS, SamplingParams(temperature=0.5))
    warm = distribution(LOGITS, SamplingParams(temperature=2.0))
    assert cold[0] > warm[0]
    assert cold[0] > 0.8
    assert np.isclose(cold.sum(), 1.0)
    assert np.isclose(warm.sum(), 1.0)


def test_top_k_limits_the_support() -> None:
    probs = distribution(LOGITS, SamplingParams(temperature=1.0, top_k=3))
    assert np.count_nonzero(probs) == 3
    assert probs[0] > probs[1] > probs[2] > 0


def test_top_p_keeps_a_variable_support() -> None:
    narrow = distribution(LOGITS, SamplingParams(temperature=1.0, top_p=0.7))
    wide = distribution(LOGITS, SamplingParams(temperature=1.0, top_p=0.95))
    assert np.count_nonzero(narrow) < np.count_nonzero(wide)
    assert np.isclose(narrow.sum(), 1.0)


def test_greedy_is_one_hot() -> None:
    probs = distribution(LOGITS, SamplingParams(temperature=0.0))
    assert probs[0] == pytest.approx(1.0)
    assert np.count_nonzero(probs) == 1


def test_per_sequence_rngs_are_reproducible() -> None:
    params = SamplingParams(temperature=0.8, top_p=0.9, seed=1234)
    seq_a = Sequence(0, "a", [1], params)
    seq_b = Sequence(0, "b", [1], params)
    stream_a = [sample_token(LOGITS, params, seq_a.rng)[0] for _ in range(8)]
    stream_b = [sample_token(LOGITS, params, seq_b.rng)[0] for _ in range(8)]
    assert stream_a == stream_b


def test_logprob_matches_the_sampled_distribution() -> None:
    params = SamplingParams(temperature=0.8, top_p=0.9, seed=7)
    token, logprob, _ = sample_token(LOGITS, params, np.random.default_rng(7))
    probs = distribution(LOGITS, params)
    assert np.exp(logprob) == pytest.approx(probs[token], rel=1e-9)


def test_stop_string_can_span_tokens() -> None:
    tokenizer = Tokenizer()
    params = SamplingParams(temperature=0.0, max_tokens=12, stop=("</answer>",))
    seq = Sequence(0, "stop", [ord("x")], params)
    for token_id in b"</answer>":
        seq.append_token(token_id, 0.0)
        finished = seq.check_stop(tokenizer)
    assert finished is True
    assert seq.finish_reason == "stop"
    assert tokenizer.decode(seq.output_token_ids).endswith("</answer>")


def test_min_tokens_delays_eos() -> None:
    tokenizer = Tokenizer()
    params = SamplingParams(
        temperature=0.0,
        max_tokens=4,
        min_tokens=2,
        eos_token_id=tokenizer.eos_id,
    )
    seq = Sequence(0, "eos", [1], params)
    seq.append_token(tokenizer.eos_id, 0.0)
    assert seq.check_stop(tokenizer) is False
    seq.append_token(tokenizer.eos_id, 0.0)
    assert seq.check_stop(tokenizer) is True
    assert seq.finish_reason == "stop"


def test_streaming_detokenizer_buffers_split_utf8() -> None:
    tokenizer = Tokenizer()
    stream = StreamingDetokenizer(tokenizer)
    deltas = [stream.feed(byte) for byte in "你好".encode("utf-8")]
    deltas.append(stream.flush())
    assert "".join(deltas) == "你好"


def test_all_masked_logits_raise() -> None:
    params = SamplingParams(temperature=1.0)
    mask = np.zeros(LOGITS.shape, dtype=bool)
    with pytest.raises(ValueError):
        distribution(LOGITS, params, mask)
