"""A small reverse-mode autograd engine used by MiniVerl.

The real verl delegates model computation to PyTorch/FSDP/Megatron. MiniVerl
cannot depend on a GPU stack, so it implements just enough differentiable
tensor algebra to train a tiny decoder-only Transformer on CPU with NumPy.

Design notes
------------
* Every operation records its parents and a local backward closure.  The graph
  is walked in reverse topological order, exactly like micrograd but with
  ndarray kernels.
* Broadcasting is handled by ``_unbroadcast`` so gradients always have the
  shape of the corresponding input.
* ``no_grad()`` disables graph construction for rollout/inference and makes the
  memory/throughput accounting in MiniVerl realistic.
"""

from __future__ import annotations

import contextlib
import math
from typing import Iterable, Iterator, Sequence

import numpy as np

__all__ = [
    "Tensor",
    "Parameter",
    "no_grad",
    "is_grad_enabled",
    "concat",
    "stack",
    "softmax",
    "log_softmax",
    "layer_norm",
    "embedding",
    "gather_last_dim",
    "masked_fill",
    "relu",
    "gelu",
    "maximum",
    "minimum",
    "where",
    "clip",
    "sigmoid",
    "softplus",
    "log_sigmoid",
]

_GRAD_ENABLED = True


def is_grad_enabled() -> bool:
    return _GRAD_ENABLED


@contextlib.contextmanager
def no_grad() -> Iterator[None]:
    global _GRAD_ENABLED
    previous = _GRAD_ENABLED
    _GRAD_ENABLED = False
    try:
        yield
    finally:
        _GRAD_ENABLED = previous


def _as_tensor(value) -> "Tensor":
    if isinstance(value, Tensor):
        return value
    return Tensor(np.asarray(value, dtype=np.float32))


def _unbroadcast(grad: np.ndarray, shape: tuple[int, ...]) -> np.ndarray:
    """Sum a broadcast gradient back down to ``shape``."""
    if grad.shape == shape:
        return grad
    while grad.ndim > len(shape):
        grad = grad.sum(axis=0)
    for axis, size in enumerate(shape):
        if size == 1 and grad.shape[axis] != 1:
            grad = grad.sum(axis=axis, keepdims=True)
    return grad.reshape(shape)


def _accumulate(tensor: "Tensor", grad: np.ndarray) -> None:
    if not tensor.requires_grad:
        return
    if tensor.grad is None:
        tensor.grad = np.zeros_like(tensor.data, dtype=np.float32)
    tensor.grad += grad.astype(np.float32, copy=False)


class Tensor:
    """A NumPy array with a reverse-mode gradient tape."""

    __array_priority__ = 1000

    def __init__(
        self,
        data,
        requires_grad: bool = False,
        _children: Iterable["Tensor"] = (),
        _op: str = "",
    ) -> None:
        if isinstance(data, Tensor):
            data = data.data
        array = np.asarray(data, dtype=np.float32)
        self.data = array
        self.requires_grad = bool(requires_grad) and _GRAD_ENABLED
        self.grad: np.ndarray | None = None
        self._backward = lambda: None
        self._prev = set(_children)
        self._op = _op

    # ------------------------------------------------------------------
    # metadata
    # ------------------------------------------------------------------
    @property
    def shape(self) -> tuple[int, ...]:
        return self.data.shape

    @property
    def ndim(self) -> int:
        return self.data.ndim

    @property
    def size(self) -> int:
        return self.data.size

    @property
    def dtype(self):
        return self.data.dtype

    def __len__(self) -> int:
        return len(self.data)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Tensor(shape={self.shape}, op={self._op or 'leaf'})"

    # ------------------------------------------------------------------
    # graph helpers
    # ------------------------------------------------------------------
    def _make(
        self,
        data,
        parents: Iterable["Tensor"],
        backward,
        op: str,
    ) -> "Tensor":
        out = Tensor(data=data, requires_grad=any(p.requires_grad for p in parents), _children=parents, _op=op)
        out._backward = backward

        def _run() -> None:
            if out.grad is not None:
                backward(out.grad)

        out._backward = _run
        return out

    def backward(self, grad: np.ndarray | None = None) -> None:
        if grad is None:
            if self.data.size != 1:
                raise ValueError("backward() without grad requires a scalar output")
            grad = np.ones_like(self.data)
        self.grad = np.asarray(grad, dtype=np.float32)

        topo: list[Tensor] = []
        visited: set[int] = set()

        def build(node: Tensor) -> None:
            if id(node) in visited:
                return
            visited.add(id(node))
            for parent in node._prev:
                build(parent)
            topo.append(node)

        build(self)
        for node in reversed(topo):
            node._backward()

    def detach(self) -> "Tensor":
        return Tensor(self.data.copy(), requires_grad=False)

    def zero_grad(self) -> None:
        self.grad = None

    # ------------------------------------------------------------------
    # arithmetic
    # ------------------------------------------------------------------
    def __add__(self, other) -> "Tensor":
        other = _as_tensor(other)
        return self._make(
            self.data + other.data,
            (self, other),
            lambda g: (_accumulate(self, _unbroadcast(g, self.shape)), _accumulate(other, _unbroadcast(g, other.shape))),
            "add",
        )

    __radd__ = __add__

    def __sub__(self, other) -> "Tensor":
        other = _as_tensor(other)
        return self._make(
            self.data - other.data,
            (self, other),
            lambda g: (_accumulate(self, _unbroadcast(g, self.shape)), _accumulate(other, _unbroadcast(-g, other.shape))),
            "sub",
        )

    def __rsub__(self, other) -> "Tensor":
        return _as_tensor(other).__sub__(self)

    def __mul__(self, other) -> "Tensor":
        other = _as_tensor(other)
        return self._make(
            self.data * other.data,
            (self, other),
            lambda g: (
                _accumulate(self, _unbroadcast(g * other.data, self.shape)),
                _accumulate(other, _unbroadcast(g * self.data, other.shape)),
            ),
            "mul",
        )

    __rmul__ = __mul__

    def __truediv__(self, other) -> "Tensor":
        other = _as_tensor(other)
        return self._make(
            self.data / other.data,
            (self, other),
            lambda g: (
                _accumulate(self, _unbroadcast(g / other.data, self.shape)),
                _accumulate(other, _unbroadcast(-g * self.data / (other.data**2), other.shape)),
            ),
            "div",
        )

    def __neg__(self) -> "Tensor":
        return self._make(-self.data, (self,), lambda g: _accumulate(self, -g), "neg")

    def __matmul__(self, other) -> "Tensor":
        other = _as_tensor(other)
        a, b = self.data, other.data
        if a.ndim < 2 or b.ndim < 2:
            raise ValueError("matmul expects tensors with at least 2 dimensions")
        out = a @ b

        def backward(g: np.ndarray) -> None:
            ga = g @ np.swapaxes(b, -1, -2)
            gb = np.swapaxes(a, -1, -2) @ g
            _accumulate(self, _unbroadcast(ga, a.shape))
            _accumulate(other, _unbroadcast(gb, b.shape))

        return self._make(out, (self, other), backward, "matmul")

    # ------------------------------------------------------------------
    # reductions and shape ops
    # ------------------------------------------------------------------
    def sum(self, axis=None, keepdims: bool = False) -> "Tensor":
        out = self.data.sum(axis=axis, keepdims=keepdims)

        def backward(g: np.ndarray) -> None:
            if axis is None:
                full = np.broadcast_to(g, self.shape)
            else:
                axes = (axis,) if isinstance(axis, int) else tuple(axis)
                normalized = {ax % self.ndim for ax in axes}
                if keepdims:
                    expanded_shape = tuple(self.shape[ax] if ax not in normalized else 1 for ax in range(self.ndim))
                else:
                    expanded_shape = tuple(1 if ax in normalized else self.shape[ax] for ax in range(self.ndim))
                full = np.broadcast_to(np.reshape(g, expanded_shape), self.shape)
            _accumulate(self, full)

        return self._make(out, (self,), backward, "sum")

    def mean(self, axis=None, keepdims: bool = False) -> "Tensor":
        count = self.data.size if axis is None else np.prod(
            [self.shape[a] for a in ((axis,) if isinstance(axis, int) else axis)]
        )
        return self.sum(axis=axis, keepdims=keepdims) * (1.0 / float(count))

    def reshape(self, *shape) -> "Tensor":
        if len(shape) == 1 and isinstance(shape[0], (tuple, list)):
            shape = tuple(shape[0])
        original = self.shape
        out = self.data.reshape(shape)
        return self._make(out, (self,), lambda g: _accumulate(self, g.reshape(original)), "reshape")

    def transpose(self, *axes) -> "Tensor":
        if not axes:
            axes = tuple(reversed(range(self.ndim)))
        elif len(axes) == 1 and isinstance(axes[0], (tuple, list)):
            axes = tuple(axes[0])
        inverse = tuple(np.argsort(axes))
        out = np.transpose(self.data, axes)
        return self._make(out, (self,), lambda g: _accumulate(self, np.transpose(g, inverse)), "transpose")

    @property
    def T(self) -> "Tensor":
        return self.transpose()

    def __getitem__(self, index) -> "Tensor":
        out = self.data[index]

        def backward(g: np.ndarray) -> None:
            full = np.zeros_like(self.data)
            if isinstance(index, tuple) and any(
                isinstance(item, (np.ndarray, list)) for item in index
            ):
                np.add.at(full, index, g)
            elif isinstance(index, (np.ndarray, list)):
                np.add.at(full, index, g)
            else:
                full[index] += g
            _accumulate(self, full)

        return self._make(out, (self,), backward, "getitem")

    # ------------------------------------------------------------------
    # elementwise math
    # ------------------------------------------------------------------
    def exp(self) -> "Tensor":
        out = np.exp(self.data)
        return self._make(out, (self,), lambda g: _accumulate(self, g * out), "exp")

    def log(self) -> "Tensor":
        return self._make(np.log(self.data), (self,), lambda g: _accumulate(self, g / self.data), "log")

    def sqrt(self) -> "Tensor":
        out = np.sqrt(self.data)
        return self._make(out, (self,), lambda g: _accumulate(self, g * 0.5 / out), "sqrt")

    def pow(self, exponent: float) -> "Tensor":
        out = np.power(self.data, exponent)
        return self._make(
            out,
            (self,),
            lambda g: _accumulate(self, g * exponent * np.power(self.data, exponent - 1.0)),
            "pow",
        )

    def __pow__(self, exponent: float) -> "Tensor":
        return self.pow(exponent)

    def relu(self) -> "Tensor":
        mask = (self.data > 0).astype(np.float32)
        return self._make(self.data * mask, (self,), lambda g: _accumulate(self, g * mask), "relu")

    def gelu(self) -> "Tensor":
        # tanh approximation, same shape as the common Transformer implementation
        x = self.data
        c = math.sqrt(2.0 / math.pi)
        inner = c * (x + 0.044715 * x**3)
        t = np.tanh(inner)
        out = 0.5 * x * (1.0 + t)
        d_inner = c * (1.0 + 3 * 0.044715 * x**2)
        derivative = 0.5 * (1.0 + t) + 0.5 * x * (1.0 - t**2) * d_inner
        return self._make(out, (self,), lambda g: _accumulate(self, g * derivative), "gelu")

    def clip(self, low: float, high: float) -> "Tensor":
        out = np.clip(self.data, low, high)
        mask = ((self.data >= low) & (self.data <= high)).astype(np.float32)
        return self._make(out, (self,), lambda g: _accumulate(self, g * mask), "clip")

    # ------------------------------------------------------------------
    # convenience
    # ------------------------------------------------------------------
    def item(self) -> float:
        return float(self.data.item())

    def numpy(self) -> np.ndarray:
        return self.data


class Parameter(Tensor):
    """A leaf tensor that is updated by an optimizer."""

    def __init__(self, data) -> None:
        super().__init__(data, requires_grad=True)


# ----------------------------------------------------------------------
# functional ops
# ----------------------------------------------------------------------


def concat(tensors: Sequence[Tensor], axis: int = 0) -> Tensor:
    tensors = [_as_tensor(t) for t in tensors]
    out = np.concatenate([t.data for t in tensors], axis=axis)
    sizes = [t.shape[axis] for t in tensors]

    def backward(g: np.ndarray) -> None:
        start = 0
        for tensor, size in zip(tensors, sizes):
            sl = [slice(None)] * g.ndim
            sl[axis] = slice(start, start + size)
            _accumulate(tensor, g[tuple(sl)])
            start += size

    return Tensor(out, requires_grad=any(t.requires_grad for t in tensors), _children=tensors, _op="concat")._make(
        out, tensors, backward, "concat"
    )


def stack(tensors: Sequence[Tensor], axis: int = 0) -> Tensor:
    tensors = [_as_tensor(t) for t in tensors]
    out = np.stack([t.data for t in tensors], axis=axis)

    def backward(g: np.ndarray) -> None:
        for i, tensor in enumerate(tensors):
            _accumulate(tensor, np.take(g, i, axis=axis))

    return Tensor(out, requires_grad=any(t.requires_grad for t in tensors), _children=tensors, _op="stack")._make(
        out, tensors, backward, "stack"
    )


def softmax(x: Tensor, axis: int = -1) -> Tensor:
    shifted = x.data - np.max(x.data, axis=axis, keepdims=True)
    exp = np.exp(shifted)
    out = exp / np.sum(exp, axis=axis, keepdims=True)

    def backward(g: np.ndarray) -> None:
        _accumulate(x, out * (g - np.sum(g * out, axis=axis, keepdims=True)))

    return x._make(out, (x,), backward, "softmax")


def log_softmax(x: Tensor, axis: int = -1) -> Tensor:
    shifted = x.data - np.max(x.data, axis=axis, keepdims=True)
    logsumexp = np.log(np.sum(np.exp(shifted), axis=axis, keepdims=True))
    out = shifted - logsumexp
    probs = np.exp(out)

    def backward(g: np.ndarray) -> None:
        _accumulate(x, g - probs * np.sum(g, axis=axis, keepdims=True))

    return x._make(out, (x,), backward, "log_softmax")


def layer_norm(x: Tensor, gamma: Tensor, beta: Tensor, eps: float = 1e-5) -> Tensor:
    # Normalize over the feature dimension only, independently at every
    # (batch, time) position.  Averaging over all axes would silently couple
    # different token positions and is a common hand-written-backward bug.
    reduce_axes = tuple(range(x.ndim - 1))
    feature_axis = -1
    mean = x.data.mean(axis=feature_axis, keepdims=True)
    var = x.data.var(axis=feature_axis, keepdims=True)
    inv_std = 1.0 / np.sqrt(var + eps)
    xhat = (x.data - mean) * inv_std
    out = xhat * gamma.data + beta.data

    def backward(g: np.ndarray) -> None:
        gamma_grad = np.sum(g * xhat, axis=reduce_axes) if reduce_axes else (g * xhat)
        beta_grad = np.sum(g, axis=reduce_axes) if reduce_axes else g
        _accumulate(gamma, np.asarray(gamma_grad).reshape(gamma.shape))
        _accumulate(beta, np.asarray(beta_grad).reshape(beta.shape))
        dxhat = g * gamma.data
        dx = inv_std * (
            dxhat
            - dxhat.mean(axis=feature_axis, keepdims=True)
            - xhat * (dxhat * xhat).mean(axis=feature_axis, keepdims=True)
        )
        _accumulate(x, dx)

    return x._make(out, (x, gamma, beta), backward, "layer_norm")


def embedding(weight: Tensor, indices: np.ndarray) -> Tensor:
    idx = np.asarray(indices, dtype=np.int64)
    out = weight.data[idx]

    def backward(g: np.ndarray) -> None:
        grad = np.zeros_like(weight.data)
        np.add.at(grad, idx.reshape(-1), g.reshape(-1, weight.shape[-1]))
        _accumulate(weight, grad)

    return weight._make(out, (weight,), backward, "embedding")


def gather_last_dim(x: Tensor, indices: np.ndarray) -> Tensor:
    idx = np.asarray(indices, dtype=np.int64)
    out = np.take_along_axis(x.data, idx[..., None], axis=-1)[..., 0]
    vocab = x.shape[-1]

    def backward(g: np.ndarray) -> None:
        grad = np.zeros_like(x.data)
        flat_rows = np.arange(g.size).reshape(g.shape)
        flat_idx = flat_rows * vocab + idx
        np.add.at(grad.reshape(-1), flat_idx.reshape(-1), g.reshape(-1))
        _accumulate(x, grad)

    return x._make(out, (x,), backward, "gather")


def masked_fill(x: Tensor, mask: np.ndarray, value: float) -> Tensor:
    mask = np.asarray(mask, dtype=bool)
    out = np.where(mask, value, x.data)

    def backward(g: np.ndarray) -> None:
        _accumulate(x, np.where(mask, 0.0, g))

    return x._make(out, (x,), backward, "masked_fill")


def relu(x: Tensor) -> Tensor:
    return x.relu()


def gelu(x: Tensor) -> Tensor:
    return x.gelu()


def maximum(a, b) -> Tensor:
    a, b = _as_tensor(a), _as_tensor(b)
    out = np.maximum(a.data, b.data)
    mask_a = (a.data >= b.data).astype(np.float32)

    def backward(g: np.ndarray) -> None:
        _accumulate(a, _unbroadcast(g * mask_a, a.shape))
        _accumulate(b, _unbroadcast(g * (1.0 - mask_a), b.shape))

    return a._make(out, (a, b), backward, "maximum")


def clip(x: Tensor, low: float, high: float) -> Tensor:
    return x.clip(low, high)


def where(condition: np.ndarray, a, b) -> Tensor:
    """Select elementwise; gradient flows to the selected branch only."""
    a, b = _as_tensor(a), _as_tensor(b)
    condition = np.asarray(condition, dtype=bool)
    out = np.where(condition, a.data, b.data)

    def backward(g: np.ndarray) -> None:
        _accumulate(a, _unbroadcast(np.where(condition, g, 0.0), a.shape))
        _accumulate(b, _unbroadcast(np.where(condition, 0.0, g), b.shape))

    return a._make(out, (a, b), backward, "where")


def minimum(a, b) -> Tensor:
    return -maximum(-_as_tensor(a), -_as_tensor(b))


def sigmoid(x: Tensor) -> Tensor:
    """Numerically stable logistic sigmoid (no overflow for large |x|)."""
    z = x.data
    out = np.empty_like(z, dtype=np.float32)
    positive = z >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    exp_z = np.exp(z[~positive])
    out[~positive] = exp_z / (1.0 + exp_z)

    def backward(g: np.ndarray) -> None:
        _accumulate(x, g * out * (1.0 - out))

    return x._make(out, (x,), backward, "sigmoid")


def softplus(x: Tensor) -> Tensor:
    """stable ``log(1 + exp(x))``; gradient is ``sigmoid(x)``."""
    z = x.data
    out = np.where(z > 0, z + np.log1p(np.exp(-np.abs(z))), np.log1p(np.exp(-np.abs(z))))
    sig = np.empty_like(z, dtype=np.float32)
    positive = z >= 0
    sig[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    exp_z = np.exp(z[~positive])
    sig[~positive] = exp_z / (1.0 + exp_z)

    def backward(g: np.ndarray) -> None:
        _accumulate(x, g * sig)

    return x._make(out, (x,), backward, "softplus")


def log_sigmoid(x: Tensor) -> Tensor:
    """stable ``log(sigmoid(x)) = -softplus(-x)``.

    Bradley-Terry losses use this instead of ``log(sigmoid(z))``: for
    ``z = -80`` the naive composition underflows to ``log(0) = -inf`` while
    this form returns ``-80`` and a finite gradient.
    """
    z = x.data
    out = -np.logaddexp(0.0, -z)

    # d/dz log sigmoid(z) = sigmoid(-z)
    neg = -z
    sig = np.empty_like(z, dtype=np.float32)
    positive = neg >= 0
    sig[positive] = 1.0 / (1.0 + np.exp(-neg[positive]))
    exp_neg = np.exp(neg[~positive])
    sig[~positive] = exp_neg / (1.0 + exp_neg)

    def backward(g: np.ndarray) -> None:
        _accumulate(x, g * sig)

    return x._make(out, (x,), backward, "log_sigmoid")
