from __future__ import annotations

import numpy as np
import pytest

from mini_verl.autograd import Tensor
from mini_verl.nn import TinyLinearLM, TinyLinearValue, TinyLM, TinyValueModel, log_probs_from_logits, token_log_probs


def test_transformer_shapes():
    model = TinyLM(vocab_size=20, max_seq_len=16, dim=16, n_layer=2, n_head=2, seed=0)
    ids = np.random.default_rng(0).integers(0, 20, size=(3, 7))
    logits = model(ids)
    assert logits.shape == (3, 7, 20)
    assert model.num_parameters() > 0


def test_causal_mask_hides_future_tokens():
    model = TinyLM(vocab_size=20, max_seq_len=16, dim=16, n_layer=1, n_head=2, seed=0)
    a = np.asarray([[1, 2, 3, 4, 5]], dtype=np.int64)
    b = a.copy()
    b[0, -1] = 19
    logits_a = model(a).data
    logits_b = model(b).data
    assert np.allclose(logits_a[:, :-1], logits_b[:, :-1], atol=1e-6)
    assert not np.allclose(logits_a[:, -1], logits_b[:, -1])


def test_value_model_shape():
    model = TinyValueModel(vocab_size=20, max_seq_len=16, dim=16, n_layer=1, n_head=2)
    values = model(np.zeros((2, 5), dtype=np.int64))
    assert values.shape == (2, 5)


def test_linear_models():
    policy = TinyLinearLM(vocab_size=20, max_seq_len=16, dim=8, seed=0)
    value = TinyLinearValue(vocab_size=20, max_seq_len=16, dim=8, seed=0)
    ids = np.asarray([[1, 2, 3]], dtype=np.int64)
    assert policy(ids).shape == (1, 3, 20)
    assert value(ids).shape == (1, 3)


def test_state_dict_roundtrip_and_strict():
    model = TinyLM(vocab_size=10, max_seq_len=8, dim=8, n_layer=1, n_head=1)
    state = model.state_dict()
    clone = TinyLM(vocab_size=10, max_seq_len=8, dim=8, n_layer=1, n_head=1, seed=1)
    clone.load_state_dict(state)
    assert all(np.allclose(clone.state_dict()[key], value) for key, value in state.items())
    with pytest.raises(KeyError):
        clone.load_state_dict({"bogus": np.zeros(1)}, strict=True)
    with pytest.raises(ValueError):
        clone.load_state_dict({"tok_emb.weight": np.zeros((1, 1))}, strict=False)


def test_token_log_probs_mask():
    model = TinyLinearLM(vocab_size=10, max_seq_len=8, dim=8)
    ids = np.asarray([[1, 2, 3, 4]], dtype=np.int64)
    mask = np.asarray([[0, 0, 1, 1]], dtype=np.float32)
    log_probs = token_log_probs(model, ids, mask)
    assert log_probs.shape == (1, 4)
    assert np.allclose(log_probs.data[0, :2], 0.0)
    assert np.all(log_probs.data[0, 2:] < 0.0)


def test_log_probs_from_logits_matches_manual():
    model = TinyLinearLM(vocab_size=10, max_seq_len=8, dim=8)
    ids = np.asarray([[1, 2, 3]], dtype=np.int64)
    mask = np.asarray([[0, 1, 1]], dtype=np.float32)
    logits = model(ids)
    got = log_probs_from_logits(logits, ids, mask).data
    manual = np.log(np.exp(logits.data) / np.exp(logits.data).sum(axis=-1, keepdims=True))
    assert np.allclose(got[0, 1], manual[0, 0, 2], atol=1e-5)
    assert np.allclose(got[0, 2], manual[0, 1, 3], atol=1e-5)
