"""Tiny decoder-only Transformer and a value head, implemented on MiniVerl autograd.

The point of this module is *not* language quality.  It is to have a real
sequence model with real gradients so that every RL system component
(rollout, old/ref log-prob, critic, PPO update, checkpoint, weight sync) is
exercised end to end without a GPU dependency.
"""

from __future__ import annotations

import math
from typing import Iterator

import numpy as np

from .autograd import (
    Parameter,
    Tensor,
    embedding,
    gather_last_dim,
    layer_norm,
    log_softmax,
    masked_fill,
    softmax,
)


class Module:
    """Minimal module system: parameters, state_dict, train/eval."""

    def __init__(self) -> None:
        self.training = True

    def forward(self, *args, **kwargs) -> Tensor:  # pragma: no cover - interface
        raise NotImplementedError

    def __call__(self, *args, **kwargs) -> Tensor:
        return self.forward(*args, **kwargs)

    def named_parameters(self, prefix: str = "") -> Iterator[tuple[str, Parameter]]:
        for name, value in self.__dict__.items():
            if name == "training":
                continue
            path = f"{prefix}.{name}" if prefix else name
            if isinstance(value, Parameter):
                yield path, value
            elif isinstance(value, Module):
                yield from value.named_parameters(path)
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    if isinstance(item, Module):
                        yield from item.named_parameters(f"{path}.{index}")

    def parameters(self) -> Iterator[Parameter]:
        for _, parameter in self.named_parameters():
            yield parameter

    def trainable_parameters(self) -> Iterator[Parameter]:
        """Parameters that receive gradients (used by LoRA/adapters)."""

        for parameter in self.parameters():
            if parameter.requires_grad:
                yield parameter

    def num_trainable_parameters(self) -> int:
        return sum(parameter.size for parameter in self.trainable_parameters())

    def state_dict(self) -> dict[str, np.ndarray]:
        return {name: parameter.data.copy() for name, parameter in self.named_parameters()}

    def load_state_dict(self, state: dict[str, np.ndarray], strict: bool = True) -> None:
        own = dict(self.named_parameters())
        missing = set(own) - set(state)
        unexpected = set(state) - set(own)
        if strict and (missing or unexpected):
            raise KeyError(f"state_dict mismatch: missing={sorted(missing)}, unexpected={sorted(unexpected)}")
        for name, parameter in own.items():
            if name in state:
                array = np.asarray(state[name], dtype=np.float32)
                if array.shape != parameter.data.shape:
                    raise ValueError(f"{name}: expected shape {parameter.data.shape}, got {array.shape}")
                parameter.data[...] = array

    def zero_grad(self) -> None:
        for parameter in self.parameters():
            parameter.grad = None

    def train(self) -> None:
        self.training = True
        for module in self._children():
            module.train()

    def eval(self) -> None:
        self.training = False
        for module in self._children():
            module.eval()

    def _children(self) -> Iterator["Module"]:
        for value in self.__dict__.values():
            if isinstance(value, Module):
                yield value
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, Module):
                        yield item

    def num_parameters(self) -> int:
        return sum(parameter.size for parameter in self.parameters())

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(params={self.num_parameters()})"


def _init(rng: np.random.Generator, shape: tuple[int, ...], scale: float) -> np.ndarray:
    return (rng.standard_normal(shape) * scale).astype(np.float32)


class Linear(Module):
    def __init__(self, in_features: int, out_features: int, bias: bool = True, rng: np.random.Generator | None = None):
        super().__init__()
        rng = rng or np.random.default_rng(0)
        self.in_features = in_features
        self.out_features = out_features
        self.weight = Parameter(_init(rng, (in_features, out_features), 1.0 / math.sqrt(in_features)))
        self.bias = Parameter(np.zeros(out_features, dtype=np.float32)) if bias else None

    def forward(self, x: Tensor) -> Tensor:
        out = x @ self.weight
        if self.bias is not None:
            out = out + self.bias
        return out


class Embedding(Module):
    def __init__(self, num_embeddings: int, embedding_dim: int, rng: np.random.Generator | None = None):
        super().__init__()
        rng = rng or np.random.default_rng(0)
        self.num_embeddings = num_embeddings
        self.embedding_dim = embedding_dim
        self.weight = Parameter(_init(rng, (num_embeddings, embedding_dim), 0.02))

    def forward(self, indices: np.ndarray) -> Tensor:
        return embedding(self.weight, indices)


class LayerNorm(Module):
    def __init__(self, dim: int, eps: float = 1e-5):
        super().__init__()
        self.gamma = Parameter(np.ones(dim, dtype=np.float32))
        self.beta = Parameter(np.zeros(dim, dtype=np.float32))
        self.eps = eps

    def forward(self, x: Tensor) -> Tensor:
        return layer_norm(x, self.gamma, self.beta, self.eps)


class CausalSelfAttention(Module):
    def __init__(self, dim: int, n_head: int, rng: np.random.Generator):
        super().__init__()
        if dim % n_head:
            raise ValueError("dim must be divisible by n_head")
        self.dim = dim
        self.n_head = n_head
        self.head_dim = dim // n_head
        self.c_attn = Linear(dim, 3 * dim, rng=rng)
        self.c_proj = Linear(dim, dim, rng=rng)

    def forward(self, x: Tensor) -> Tensor:
        batch, seq, dim = x.shape
        qkv = self.c_attn(x)
        q = qkv[:, :, :dim].reshape(batch, seq, self.n_head, self.head_dim).transpose(0, 2, 1, 3)
        k = qkv[:, :, dim : 2 * dim].reshape(batch, seq, self.n_head, self.head_dim).transpose(0, 2, 1, 3)
        v = qkv[:, :, 2 * dim :].reshape(batch, seq, self.n_head, self.head_dim).transpose(0, 2, 1, 3)

        scores = (q @ k.transpose(0, 1, 3, 2)) * (1.0 / math.sqrt(self.head_dim))
        causal = np.triu(np.ones((seq, seq), dtype=bool), k=1)
        scores = masked_fill(scores, causal[None, None, :, :], -1e9)
        attention = softmax(scores, axis=-1)
        y = (attention @ v).transpose(0, 2, 1, 3).reshape(batch, seq, dim)
        return self.c_proj(y)


class MLP(Module):
    def __init__(self, dim: int, hidden: int, rng: np.random.Generator):
        super().__init__()
        self.fc_in = Linear(dim, hidden, rng=rng)
        self.fc_out = Linear(hidden, dim, rng=rng)

    def forward(self, x: Tensor) -> Tensor:
        return self.fc_out(self.fc_in(x).gelu())


class Block(Module):
    def __init__(self, dim: int, n_head: int, rng: np.random.Generator):
        super().__init__()
        self.ln_1 = LayerNorm(dim)
        self.attn = CausalSelfAttention(dim, n_head, rng)
        self.ln_2 = LayerNorm(dim)
        self.mlp = MLP(dim, 4 * dim, rng)

    def forward(self, x: Tensor) -> Tensor:
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x


class TinyTransformer(Module):
    """A GPT-style trunk shared by the policy and value models."""

    def __init__(
        self,
        vocab_size: int,
        max_seq_len: int,
        dim: int = 32,
        n_layer: int = 1,
        n_head: int = 2,
        seed: int = 0,
    ):
        super().__init__()
        rng = np.random.default_rng(seed)
        self.vocab_size = vocab_size
        self.max_seq_len = max_seq_len
        self.dim = dim
        self.tok_emb = Embedding(vocab_size, dim, rng)
        self.pos_emb = Embedding(max_seq_len, dim, rng)
        self.blocks = [Block(dim, n_head, rng) for _ in range(n_layer)]
        self.ln_f = LayerNorm(dim)

    def trunk(self, input_ids: np.ndarray) -> Tensor:
        input_ids = np.asarray(input_ids, dtype=np.int64)
        if input_ids.ndim != 2:
            raise ValueError(f"input_ids must be (batch, seq), got {input_ids.shape}")
        batch, seq = input_ids.shape
        if seq > self.max_seq_len:
            raise ValueError(f"sequence length {seq} exceeds max_seq_len {self.max_seq_len}")
        positions = np.arange(seq, dtype=np.int64)[None, :].repeat(batch, axis=0)
        x = self.tok_emb(input_ids) + self.pos_emb(positions)
        for block in self.blocks:
            x = block(x)
        return self.ln_f(x)


class TinyLM(TinyTransformer):
    def __init__(
        self,
        vocab_size: int,
        max_seq_len: int,
        dim: int = 32,
        n_layer: int = 1,
        n_head: int = 2,
        seed: int = 0,
    ):
        super().__init__(vocab_size, max_seq_len, dim, n_layer, n_head, seed)
        rng = np.random.default_rng(seed + 10_000)
        self.lm_head = Linear(dim, vocab_size, bias=False, rng=rng)

    def forward(self, input_ids: np.ndarray) -> Tensor:
        hidden = self.trunk(input_ids)
        return self.lm_head(hidden)


class TinyLinearLM(Module):
    """A context-free linear LM used for the bandit teaching task.

    The last prompt token determines the logits; this is enough for a
    contextual bandit and is dramatically cheaper than the Transformer on CPU.
    It still exposes the same forward contract, so PPO/GRPO code is unchanged.
    """

    def __init__(self, vocab_size: int, max_seq_len: int, dim: int = 32, seed: int = 0):
        super().__init__()
        rng = np.random.default_rng(seed)
        self.vocab_size = vocab_size
        self.max_seq_len = max_seq_len
        self.dim = dim
        self.tok_emb = Embedding(vocab_size, dim, rng)
        self.ln_f = LayerNorm(dim)
        self.lm_head = Linear(dim, vocab_size, bias=False, rng=rng)

    def trunk(self, input_ids: np.ndarray) -> Tensor:
        input_ids = np.asarray(input_ids, dtype=np.int64)
        return self.ln_f(self.tok_emb(input_ids))

    def forward(self, input_ids: np.ndarray) -> Tensor:
        return self.lm_head(self.trunk(input_ids))


class TinyLinearValue(Module):
    def __init__(self, vocab_size: int, max_seq_len: int, dim: int = 32, seed: int = 0):
        super().__init__()
        rng = np.random.default_rng(seed)
        self.vocab_size = vocab_size
        self.max_seq_len = max_seq_len
        self.dim = dim
        self.tok_emb = Embedding(vocab_size, dim, rng)
        self.ln_f = LayerNorm(dim)
        self.value_head = Linear(dim, 1, rng=rng)

    def forward(self, input_ids: np.ndarray) -> Tensor:
        hidden = self.ln_f(self.tok_emb(np.asarray(input_ids, dtype=np.int64)))
        return self.value_head(hidden).reshape(input_ids.shape[0], input_ids.shape[1])


class TinyValueModel(TinyTransformer):
    def __init__(
        self,
        vocab_size: int,
        max_seq_len: int,
        dim: int = 32,
        n_layer: int = 1,
        n_head: int = 2,
        seed: int = 0,
    ):
        super().__init__(vocab_size, max_seq_len, dim, n_layer, n_head, seed)
        rng = np.random.default_rng(seed + 20_000)
        self.value_head = Linear(dim, 1, rng=rng)

    def forward(self, input_ids: np.ndarray) -> Tensor:
        hidden = self.trunk(input_ids)
        return self.value_head(hidden).reshape(input_ids.shape[0], input_ids.shape[1])


def apply_vocab_mask(logits: Tensor, allowed_token_ids: np.ndarray | None) -> Tensor:
    if allowed_token_ids is None:
        return logits
    ids = np.asarray(allowed_token_ids, dtype=np.int64)
    mask = np.full(logits.shape[-1], -1e9, dtype=np.float32)
    mask[ids] = 0.0
    return logits + mask


def log_probs_from_logits(
    logits: Tensor,
    input_ids: np.ndarray,
    response_mask: np.ndarray,
    allowed_token_ids: np.ndarray | None = None,
) -> Tensor:
    """Align next-token log-probabilities with their target positions."""
    input_ids = np.asarray(input_ids, dtype=np.int64)
    response_mask = np.asarray(response_mask, dtype=np.float32)
    logits = apply_vocab_mask(logits, allowed_token_ids)
    shifted_logits = logits[:, :-1, :]
    log_probs = log_softmax(shifted_logits, axis=-1)
    targets = input_ids[:, 1:]
    gathered = gather_last_dim(log_probs, targets)
    batch, _ = input_ids.shape
    zero = Tensor(np.zeros((batch, 1), dtype=np.float32))
    from .autograd import concat

    full = concat([zero, gathered], axis=1)
    return full * response_mask


def token_log_probs(
    model: TinyLM,
    input_ids: np.ndarray,
    response_mask: np.ndarray,
    allowed_token_ids: np.ndarray | None = None,
) -> Tensor:
    """Log-probability of each *response* token under a causal LM.

    Logits at position ``t`` score token ``t+1``.  The returned tensor has the
    same shape as ``input_ids``; prompt positions are zeroed by the mask.
    """
    return log_probs_from_logits(model(input_ids), input_ids, response_mask, allowed_token_ids)


def entropy_from_logits(logits: Tensor, response_mask: np.ndarray) -> Tensor:
    log_probs = log_softmax(logits, axis=-1)
    probs = log_probs.exp()
    entropy = -(probs * log_probs).sum(axis=-1)
    return entropy * np.asarray(response_mask, dtype=np.float32)


def build_policy(config, vocab_size: int, max_seq_len: int, seed: int = 0):
    if getattr(config, "arch", "transformer") == "linear":
        model = TinyLinearLM(vocab_size, max_seq_len, dim=getattr(config, "hidden_size", 32), seed=seed)
    else:
        model = TinyLM(
            vocab_size=vocab_size,
            max_seq_len=max_seq_len,
            dim=getattr(config, "hidden_size", 32),
            n_layer=getattr(config, "num_layers", 1),
            n_head=getattr(config, "num_heads", 2),
            seed=seed,
        )
    rank = int(getattr(config, "lora_rank", 0) or 0)
    if rank > 0:
        from .lora import apply_lora

        apply_lora(
            model,
            rank=rank,
            alpha=int(getattr(config, "lora_alpha", 16) or 16),
            targets=getattr(config, "lora_targets", None),
            seed=seed + 1,
            dropout=float(getattr(config, "lora_dropout", 0.0) or 0.0),
        )
    return model


def build_value(config, vocab_size: int, max_seq_len: int, seed: int = 0):
    if getattr(config, "arch", "transformer") == "linear":
        return TinyLinearValue(vocab_size, max_seq_len, dim=getattr(config, "hidden_size", 32), seed=seed)
    return TinyValueModel(
        vocab_size=vocab_size,
        max_seq_len=max_seq_len,
        dim=getattr(config, "hidden_size", 32),
        n_layer=getattr(config, "num_layers", 1),
        n_head=getattr(config, "num_heads", 2),
        seed=seed,
    )
