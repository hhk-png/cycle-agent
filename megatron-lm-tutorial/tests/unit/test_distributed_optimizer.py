"""Unit tests for the flat DP-sharded optimizer.

The multi-process path is covered by ``tests/distributed_optimizer_equiv.py``.
Here we pin the single-rank contract: AdamW equivalence, clipping, checkpoint
round-trip, and the padding/decay-mask rules.
"""
from __future__ import annotations

import copy
from pathlib import Path

import torch

from mini_megatron.config import ExperimentConfig
from mini_megatron.optim.adamw import build_optimizer
from mini_megatron.optim.distributed_optimizer import DistributedOptimizer
from mini_megatron.parallel.transformer import TransformerStage


def _pair(seed: int = 11):
    root = Path(__file__).resolve().parents[2]
    cfg = ExperimentConfig.from_json(root / "configs" / "tiny.json")
    torch.manual_seed(seed)
    ref = TransformerStage(cfg.model, cfg.parallel)
    torch.manual_seed(seed)
    flat = TransformerStage(cfg.model, cfg.parallel)
    flat.load_state_dict(ref.state_dict())
    ref_opt = build_optimizer(ref, cfg.train)
    flat_opt = DistributedOptimizer(
        flat,
        lr=cfg.train.lr,
        betas=(cfg.train.adam_beta1, cfg.train.adam_beta2),
        eps=cfg.train.adam_eps,
        weight_decay=cfg.train.weight_decay,
    )
    return cfg, ref, flat, ref_opt, flat_opt


def _batch(cfg, seed: int = 0):
    g = torch.Generator().manual_seed(seed)
    x = torch.randint(0, cfg.model.vocab_size, (2, 8), generator=g)
    y = torch.randint(0, cfg.model.vocab_size, (2, 8), generator=g)
    return x, y


def test_flat_optimizer_matches_adamw():
    cfg, ref, flat, ref_opt, flat_opt = _pair()
    x, y = _batch(cfg)
    for _ in range(3):
        ref_opt.zero_grad(set_to_none=True)
        flat_opt.zero_grad()
        lr = ref(input_ids=x, labels=y).loss
        lf = flat(input_ids=x, labels=y).loss
        assert torch.equal(lr, lf)
        lr.backward()
        lf.backward()
        ref_opt.step()
        flat_opt.step()
    for pa, pb in zip(ref.parameters(), flat.parameters()):
        assert torch.equal(pa.detach(), pb.detach())


def test_flat_optimizer_clip_norm_and_restore():
    cfg, ref, flat, ref_opt, flat_opt = _pair(seed=19)
    x, y = _batch(cfg, seed=3)
    ref_opt.zero_grad(set_to_none=True)
    flat_opt.zero_grad()
    ref(input_ids=x, labels=y).loss.backward()
    flat(input_ids=x, labels=y).loss.backward()

    norm = flat_opt.clip_grad_norm(0.01)
    assert norm > 0
    # After clipping, the local shard norm must be at most the clip value.
    assert flat_opt.grad_shard.norm() <= 0.010001

    flat_opt.step()
    snapshot = copy.deepcopy(flat_opt.state_dict())
    before = [p.detach().clone() for p in flat.parameters()]

    flat_opt.load_state_dict(snapshot)
    for a, b in zip(before, flat.parameters()):
        assert torch.allclose(a, b)
    assert flat_opt.step_count == 1

    # A checkpoint from a different layout is rejected, not silently applied.
    bad = dict(snapshot)
    bad["padded_numel"] = snapshot["padded_numel"] + 1
    try:
        flat_opt.load_state_dict(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("expected layout mismatch to raise")


def test_flat_optimizer_padding_and_decay_mask():
    cfg, _, flat, _, flat_opt = _pair(seed=23)
    total = sum(p.numel() for p in flat.parameters() if p.requires_grad)
    assert flat_opt.total_numel == total
    assert flat_opt.padded_numel % flat_opt.world == 0
    assert flat_opt.padded_numel >= total
    # The pad region is never decayed and never written back to a parameter.
    assert float(flat_opt.decay_mask[total:].abs().sum()) == 0.0
    # 1-D parameters (norms/biases) are excluded from weight decay.
    offset = 0
    for p in flat.parameters():
        if not p.requires_grad:
            continue
        n = p.numel()
        expected = 0.0 if (p.ndim <= 1 or p.shape[-1] == 1) else 1.0
        assert torch.all(flat_opt.decay_mask[offset : offset + n] == expected)
        offset += n


def test_flat_grad_norm_applies_tp_replication_and_cp_weights():
    """The norm is global: TP replicas count once and CP sums count once."""
    _, _, _, _, flat_opt = _pair(seed=29)
    flat_opt.grad_shard = torch.tensor([3.0, 4.0])
    flat_opt.shard_start = 0
    flat_opt.shard_end = 2
    flat_opt.norm_weight[:2] = torch.tensor([0.5, 1.0])  # first is TP-replicated
    flat_opt.cp_size = 2
    flat_opt._prepared = True
    # world=1 here, so all_reduce is a no-op; expected = sqrt((9*0.5+16)/2).
    expected = ((9.0 * 0.5 + 16.0) / 2.0) ** 0.5
    assert abs(float(flat_opt.grad_norm()) - expected) < 1e-6
