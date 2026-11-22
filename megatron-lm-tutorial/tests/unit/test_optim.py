import torch

from mini_megatron.optim.adamw import Muon, get_lr


def test_muon_decreases_quadratic():
    torch.manual_seed(0)
    w = torch.nn.Parameter(torch.randn(8, 8))
    target = torch.eye(8)
    opt = Muon([w], lr=0.05)
    losses = []
    for _ in range(20):
        opt.zero_grad()
        loss = ((w - target) ** 2).sum()
        loss.backward()
        losses.append(loss.item())
        opt.step()
    assert losses[-1] < losses[0]


def test_lr_schedule_shapes():
    common = dict(max_steps=10, warmup_steps=2, base_lr=1.0, min_lr=0.1)
    assert get_lr(0, style="cosine", **common) == 0.5
    assert get_lr(1, style="cosine", **common) == 1.0
    assert get_lr(9, style="cosine", **common) < 0.2
    assert get_lr(2, style="constant", **common) == 1.0
    assert get_lr(5, style="linear", **common) > 0.1
    assert get_lr(5, style="inverse_sqrt", **common) > 0.1
