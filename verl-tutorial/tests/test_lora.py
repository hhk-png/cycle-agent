from __future__ import annotations

import numpy as np
import pytest

from mini_verl.config import OptimConfig
from mini_verl.engine import AdamW
from mini_verl.lora import (
    LoRALinear,
    adapter_state_dict,
    apply_lora,
    lora_modules,
    lora_parameters,
    merge_lora_inplace,
)
from mini_verl.nn import Linear, TinyLinearLM, TinyLM, log_probs_from_logits


def _lm(seed: int = 0) -> TinyLM:
    return TinyLM(vocab_size=12, max_seq_len=8, dim=16, n_layer=1, n_head=2, seed=seed)


def _ids() -> np.ndarray:
    return np.random.default_rng(0).integers(0, 12, size=(3, 6)).astype(np.int64)


def test_initial_adapter_is_identity():
    base = Linear(8, 5, rng=np.random.default_rng(0))
    adapter = LoRALinear(base, rank=4, alpha=8, seed=1)
    x = np.random.default_rng(1).standard_normal((2, 3, 8)).astype(np.float32)
    from mini_verl.autograd import Tensor

    before = base(Tensor(x)).data
    after = adapter(Tensor(x)).data
    assert np.allclose(before, after, atol=1e-6)
    assert np.allclose(adapter.delta_weight(), 0.0)


def test_only_adapters_are_trainable():
    model = _lm()
    paths = apply_lora(model, rank=4, alpha=8, seed=3)
    assert paths, "expected at least one target layer"
    frozen = [p for p in model.parameters() if not p.requires_grad]
    trainable = list(model.trainable_parameters())
    assert frozen
    assert len(trainable) == 2 * len(paths)
    assert model.num_trainable_parameters() == sum(p.size for p in trainable)


def test_gradients_reach_adapters_only():
    model = _lm()
    apply_lora(model, rank=4, alpha=8, seed=0)
    ids = _ids()
    mask = np.ones_like(ids, dtype=np.float32)
    loss = -(log_probs_from_logits(model(ids), ids, mask) * mask).sum()
    model.zero_grad()
    loss.backward()
    for parameter in lora_parameters(model):
        assert parameter.grad is not None
    for parameter in model.parameters():
        if not parameter.requires_grad:
            assert parameter.grad is None


def test_merge_matches_unmerged_forward():
    ids = _ids()
    model = _lm(seed=2)
    apply_lora(model, rank=4, alpha=8, seed=5)
    for _, module in lora_modules(model):
        module.A.data[...] = np.random.default_rng(7).standard_normal(module.A.shape).astype(np.float32) * 0.1
        module.B.data[...] = np.random.default_rng(8).standard_normal(module.B.shape).astype(np.float32) * 0.1

    clone = _lm(seed=2)
    apply_lora(clone, rank=4, alpha=8, seed=5)
    clone.load_state_dict(model.state_dict())
    expected = model(ids).data
    assert merge_lora_inplace(clone) > 0
    assert np.allclose(clone(ids).data, expected, atol=1e-5)
    assert not list(lora_modules(clone))


def test_merge_updates_base_weight_in_place():
    base = Linear(6, 4, rng=np.random.default_rng(0))
    adapter = LoRALinear(base, rank=2, alpha=4, seed=1)
    adapter.A.data[...] = 0.2
    adapter.B.data[...] = 0.3
    expected = base.weight.data + (adapter.A.data @ adapter.B.data) * adapter.scaling
    original = base.weight.data.copy()
    adapter.merge_()
    assert np.allclose(base.weight.data, expected)
    assert not np.allclose(base.weight.data, original)
    # After merging, the module is just the base layer with the new weight.
    from mini_verl.autograd import Tensor

    x = Tensor(np.ones((1, 6), dtype=np.float32))
    assert np.allclose(adapter(x).data, (x @ Tensor(expected)).data, atol=1e-6)


def test_state_dict_roundtrip_with_adapters():
    model = _lm(seed=1)
    apply_lora(model, rank=4, alpha=8, seed=2)
    state = model.state_dict()
    assert any(name.endswith(".A") for name in state)
    clone = _lm(seed=9)
    apply_lora(clone, rank=4, alpha=8, seed=3)
    clone.load_state_dict(state)
    assert all(np.allclose(state[key], clone.state_dict()[key]) for key in state)


def test_apply_lora_respects_targets_and_rank_zero():
    model = Linear(4, 4)
    assert apply_lora(model, rank=0) == []
    wrapped = apply_lora(model, rank=2, targets=("weight",))
    assert wrapped == []
    model2 = TinyLM(vocab_size=8, max_seq_len=4, dim=8, n_layer=1, n_head=2)
    paths = apply_lora(model2, rank=2, targets=("lm_head",))
    assert paths == ["lm_head"]
    assert isinstance(model2.lm_head, LoRALinear)


def test_optimizer_trains_adapters_and_leaves_base_frozen():
    model = TinyLinearLM(vocab_size=10, max_seq_len=8, dim=8, seed=0)
    apply_lora(model, rank=4, alpha=8, seed=1, targets=("lm_head",))
    frozen_before = {name: p.data.copy() for name, p in model.named_parameters() if not p.requires_grad}
    adapters_before = {name: value.copy() for name, value in adapter_state_dict(model).items()}
    optimizer = AdamW(model, OptimConfig(lr=0.05, total_training_steps=40))
    ids = np.random.default_rng(2).integers(0, 10, size=(8, 6)).astype(np.int64)
    mask = np.ones_like(ids, dtype=np.float32)

    def loss_value() -> float:
        logp = log_probs_from_logits(model(ids), ids, mask)
        return float(-(logp * mask).sum().data / mask.sum())

    before_loss = loss_value()
    for _ in range(40):
        model.zero_grad()
        logp = log_probs_from_logits(model(ids), ids, mask)
        loss = -(logp * mask).sum()
        loss.backward()
        grads = {name: p.grad for name, p in model.named_parameters() if p.grad is not None}
        optimizer.step_named(grads)

    after_loss = loss_value()
    assert after_loss < before_loss * 0.95
    for name, value in frozen_before.items():
        assert np.allclose(model.state_dict()[name], value), f"frozen parameter changed: {name}"
    adapters_after = adapter_state_dict(model)
    assert any(not np.allclose(adapters_after[name], value) for name, value in adapters_before.items())


def test_weight_decay_does_not_move_frozen_base():
    model = TinyLinearLM(vocab_size=10, max_seq_len=8, dim=8, seed=0)
    apply_lora(model, rank=4, alpha=8, seed=1, targets=("lm_head",))
    frozen_before = {name: p.data.copy() for name, p in model.named_parameters() if not p.requires_grad}
    optimizer = AdamW(model, OptimConfig(lr=0.05, weight_decay=0.1, total_training_steps=10))
    # Mimic BaseEngine.get_gradients(): every parameter gets a slot, frozen ones
    # receive zeros.  Without an explicit requires_grad check, AdamW's weight
    # decay would still move them.
    grads = {
        name: (p.grad if p.grad is not None else np.zeros_like(p.data))
        for name, p in model.named_parameters()
    }
    optimizer.step_named(grads)
    for name, value in frozen_before.items():
        assert np.allclose(model.state_dict()[name], value), f"frozen parameter moved: {name}"


def test_lora_modules_are_discoverable_in_lists():
    model = TinyLM(vocab_size=8, max_seq_len=4, dim=8, n_layer=2, n_head=2)
    paths = apply_lora(model, rank=2, alpha=4, seed=0)
    discovered = {path for path, _ in lora_modules(model)}
    assert set(paths) == discovered
    assert any(path.startswith("blocks.") for path in paths)
    with pytest.raises(ValueError):
        LoRALinear(Linear(2, 2), rank=0)
