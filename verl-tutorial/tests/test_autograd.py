from __future__ import annotations

import numpy as np
import pytest

from conftest import finite_difference
from mini_verl.autograd import (
    Tensor,
    clip,
    concat,
    embedding,
    gather_last_dim,
    gelu,
    layer_norm,
    log_softmax,
    masked_fill,
    maximum,
    minimum,
    no_grad,
    relu,
    softmax,
    stack,
    where,
)


def tensor(array, requires_grad=True):
    return Tensor(np.asarray(array, dtype=np.float32), requires_grad=requires_grad)


def test_add_backward_broadcast():
    x = tensor([[1.0, 2.0], [3.0, 4.0]])
    b = tensor([[0.5, -0.5]])
    out = (x + b).sum()
    out.backward()
    assert np.allclose(x.grad, 1.0)
    assert np.allclose(b.grad, [[2.0, 2.0]])


def test_mul_backward_broadcast():
    x = tensor([[1.0, 2.0], [3.0, 4.0]])
    y = tensor([2.0, 3.0])
    (x * y).sum().backward()
    assert np.allclose(x.grad, [[2.0, 3.0], [2.0, 3.0]])
    assert np.allclose(y.grad, [4.0, 6.0])


def test_sub_and_div():
    x = tensor([2.0, 4.0])
    y = tensor([1.0, 2.0])
    ((x - y) + (x / y)).sum().backward()
    assert np.allclose(x.grad, [2.0, 1.5])
    assert np.allclose(y.grad, [-1.0 - 2.0, -1.0 - 1.0])


def test_matmul_2d():
    a = np.arange(6, dtype=np.float32).reshape(2, 3) / 10
    b = np.arange(12, dtype=np.float32).reshape(3, 4) / 10
    x, y = tensor(a), tensor(b)
    (x @ y).sum().backward()
    expected_a = np.ones((2, 4), dtype=np.float32) @ b.T
    expected_b = a.T @ np.ones((2, 4), dtype=np.float32)
    assert np.allclose(x.grad, expected_a)
    assert np.allclose(y.grad, expected_b)


def test_matmul_batched():
    a = np.random.default_rng(0).normal(size=(2, 3, 4)).astype(np.float32)
    b = np.random.default_rng(1).normal(size=(2, 4, 5)).astype(np.float32)
    x, y = tensor(a), tensor(b)
    out = (x @ y).sum()
    out.backward()
    assert x.grad.shape == a.shape
    assert y.grad.shape == b.shape


def test_sum_axis_modes():
    x = tensor(np.arange(24, dtype=np.float32).reshape(2, 3, 4))
    x.sum(axis=1).sum().backward()
    assert np.allclose(x.grad, 1.0)
    x = tensor(np.ones((2, 3, 4), dtype=np.float32))
    x.sum(axis=-1, keepdims=True).sum().backward()
    assert np.allclose(x.grad, 1.0)
    x = tensor(np.ones((2, 3, 4), dtype=np.float32))
    x.sum(axis=(0, 2)).sum().backward()
    assert np.allclose(x.grad, 1.0)


def test_mean_and_neg():
    x = tensor([1.0, 2.0, 3.0])
    (-x.mean()).backward()
    assert np.allclose(x.grad, [-1 / 3, -1 / 3, -1 / 3])


def test_reshape_transpose():
    x = tensor(np.arange(6, dtype=np.float32).reshape(2, 3))
    (x.reshape(3, 2).transpose(1, 0)).sum().backward()
    assert x.grad.shape == (2, 3)
    assert np.allclose(x.grad, 1.0)


def test_getitem_backward():
    x = tensor(np.arange(9, dtype=np.float32).reshape(3, 3))
    x[1, :].sum().backward()
    expected = np.zeros((3, 3), dtype=np.float32)
    expected[1, :] = 1
    assert np.allclose(x.grad, expected)


def test_getitem_advanced_index():
    x = tensor(np.arange(6, dtype=np.float32).reshape(3, 2))
    x[[0, 2]].sum().backward()
    assert np.allclose(x.grad, [[1, 1], [0, 0], [1, 1]])


def test_concat_stack():
    a, b = tensor([1.0, 2.0]), tensor([3.0])
    concat([a, b]).sum().backward()
    assert np.allclose(a.grad, 1.0)
    assert np.allclose(b.grad, 1.0)
    a, b = tensor([1.0, 2.0]), tensor([3.0, 4.0])
    stack([a, b]).sum().backward()
    assert np.allclose(a.grad, 1.0)


def test_elementwise_maths():
    x = tensor([0.5, 1.5, 2.5])
    (x.exp() + x.log() + x.sqrt() + x.pow(2)).sum().backward()
    expected = np.exp(x.data) + 1 / x.data + 0.5 / np.sqrt(x.data) + 2 * x.data
    assert np.allclose(x.grad, expected, rtol=1e-5)


def test_relu_gelu_clip():
    x = tensor([-2.0, -0.5, 0.5, 2.0])
    relu(x).sum().backward()
    assert np.allclose(x.grad, [0, 0, 1, 1])
    x = tensor([-0.5, 0.5])
    gelu(x).sum().backward()
    assert np.all(np.isfinite(x.grad))
    x = tensor([-3.0, 0.0, 3.0])
    clip(x, -1.0, 1.0).sum().backward()
    assert np.allclose(x.grad, [0, 1, 0])


def test_softmax_log_softmax():
    x = tensor([[1.0, 2.0, 3.0], [0.0, -1.0, 4.0]])
    softmax(x).sum().backward()
    assert np.allclose(x.grad, 0.0, atol=1e-6)
    x = tensor([[1.0, 2.0, 3.0]])
    (log_softmax(x) * np.asarray([[0.2, 0.3, 0.5]], dtype=np.float32)).sum().backward()
    assert x.grad.shape == x.data.shape


def test_layer_norm():
    rng = np.random.default_rng(0)
    x = tensor(rng.normal(size=(2, 3, 4)).astype(np.float32))
    gamma = tensor(np.ones(4, dtype=np.float32))
    beta = tensor(np.zeros(4, dtype=np.float32))
    out = layer_norm(x, gamma, beta)
    out.sum().backward()
    assert x.grad.shape == x.data.shape
    assert gamma.grad.shape == (4,)
    assert np.allclose(beta.grad, 6.0)


def test_embedding_gather():
    weight = tensor(np.arange(12, dtype=np.float32).reshape(6, 2))
    idx = np.asarray([0, 2, 2], dtype=np.int64)
    embedding(weight, idx).sum().backward()
    assert np.allclose(weight.grad[0], [1, 1])
    assert np.allclose(weight.grad[2], [2, 2])
    x = tensor(np.arange(12, dtype=np.float32).reshape(2, 3, 2))
    gather_last_dim(x, np.asarray([[0, 1, 1], [1, 0, 0]])).sum().backward()
    assert x.grad.shape == x.data.shape


def test_masked_fill_where_minmax():
    x = tensor([1.0, 2.0, 3.0])
    masked_fill(x, np.asarray([False, True, False]), -1.0).sum().backward()
    assert np.allclose(x.grad, [1, 0, 1])
    a, b = tensor([1.0, 5.0]), tensor([4.0, 2.0])
    where(np.asarray([True, False]), a, b).sum().backward()
    assert np.allclose(a.grad, [1, 0])
    assert np.allclose(b.grad, [0, 1])
    a, b = tensor([1.0, 5.0]), tensor([4.0, 2.0])
    maximum(a, b).sum().backward()
    assert np.allclose(a.grad, [0, 1])
    a, b = tensor([1.0, 5.0]), tensor([4.0, 2.0])
    minimum(a, b).sum().backward()
    assert np.allclose(a.grad, [1, 0])


def test_no_grad_blocks_graph():
    x = tensor([1.0, 2.0])
    with no_grad():
        y = (x * 2).sum()
    assert not y.requires_grad
    y.backward()
    assert x.grad is None


def test_grad_accumulates():
    x = tensor([1.0, 2.0])
    (x * 2).sum().backward()
    (x * 3).sum().backward()
    assert np.allclose(x.grad, [5.0, 5.0])


def test_backward_requires_scalar_or_grad():
    x = tensor([1.0, 2.0])
    with pytest.raises(ValueError):
        (x * 2).backward()


def test_finite_difference_layer_norm():
    rng = np.random.default_rng(3)
    array = rng.normal(size=(2, 4)).astype(np.float32)
    gamma = Tensor(np.ones(4, dtype=np.float32), requires_grad=True)
    beta = Tensor(np.zeros(4, dtype=np.float32), requires_grad=True)

    def fn(a):
        x = Tensor(a, requires_grad=False)
        return float(layer_norm(x, gamma, beta).pow(2).sum().item())

    x = tensor(array)
    layer_norm(x, gamma, beta).pow(2).sum().backward()
    numeric = finite_difference(fn, array.copy())
    assert np.allclose(x.grad, numeric, atol=2e-2, rtol=2e-2)
