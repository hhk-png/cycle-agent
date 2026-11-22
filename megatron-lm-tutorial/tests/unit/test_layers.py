import torch

from mini_megatron.parallel.layers import (
    ColumnParallelLinear,
    RowParallelLinear,
    VocabParallelEmbedding,
    simulate_fp8,
)
from mini_megatron.parallel.state import initialize_model_parallel


def test_linear_world_one_matches_torch():
    initialize_model_parallel(1, 1, 1)
    x = torch.randn(2, 3, 8)
    col = ColumnParallelLinear(8, 16, bias=True, gather_output=True)
    row = RowParallelLinear(16, 4, bias=True)
    full1 = torch.nn.Linear(8, 16, bias=True)
    full2 = torch.nn.Linear(16, 4, bias=True)
    with torch.no_grad():
        full1.weight.copy_(col.weight)
        full1.bias.copy_(col.bias)
        full2.weight.copy_(row.weight)
        full2.bias.copy_(row.bias)
    assert torch.allclose(col(x), full1(x), atol=1e-6)
    assert torch.allclose(row(col(x)), full2(full1(x)), atol=1e-6)


def test_vocab_embedding_world_one():
    initialize_model_parallel(1, 1, 1)
    emb = VocabParallelEmbedding(32, 8)
    ids = torch.randint(0, 32, (2, 5))
    ref = torch.nn.functional.embedding(ids, emb.weight)
    assert torch.allclose(emb(ids), ref, atol=1e-6)


def test_fp8_simulation_bounded_error():
    torch.manual_seed(0)
    x = torch.randn(1024)
    y = simulate_fp8(x)
    rel = (x - y).abs().mean() / x.abs().mean()
    assert rel < 0.05
    assert torch.isfinite(y).all()


def test_fp8_simulation_preserves_gradient():
    """The forward is quantised; the backward is a straight-through estimator."""
    torch.manual_seed(0)
    x = torch.randn(64, requires_grad=True)
    y = simulate_fp8(x)
    y.sum().backward()
    assert x.grad is not None
    assert torch.allclose(x.grad, torch.ones_like(x))
    assert float(x.grad.abs().sum()) > 0


def test_fully_masked_query_row_is_finite():
    """A padded query row must not produce NaN (all keys masked)."""
    from mini_megatron.parallel.attention import CoreAttention

    initialize_model_parallel(1, 1, 1)
    attn = CoreAttention()
    q = torch.randn(1, 2, 3, 4, requires_grad=True)
    k = torch.randn(1, 2, 5, 4, requires_grad=True)
    v = torch.randn(1, 2, 5, 4, requires_grad=True)
    mask = torch.zeros(1, 5)  # every query sees no valid key
    out = attn(
        q, k, v, num_query_groups=2, q_start=0, kv_len=5, attention_mask=mask
    )
    assert torch.isfinite(out).all()
    out.sum().backward()
    for tensor in (q, k, v):
        assert tensor.grad is not None
        assert torch.isfinite(tensor.grad).all()
