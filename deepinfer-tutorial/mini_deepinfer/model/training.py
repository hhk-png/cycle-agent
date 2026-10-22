"""Manual backpropagation for the dense toy Transformer.

Frameworks such as PyTorch are intentionally not required: the tutorial ships
a self-contained trainer so that the checkpoint can be reproduced with only
NumPy.  The backward pass is written out explicitly, which also makes the
memory/FLOP discussion in the tutorial concrete.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import ModelConfig
from .ops import apply_rope, apply_rope_backward, rope_tables, sigmoid, silu, softmax


def init_weights(config: ModelConfig, seed: int = 0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    d = config.d_model
    kv = config.kv_dim
    weights: dict[str, np.ndarray] = {
        "tok_emb": rng.normal(0.0, 0.02, (config.vocab_size, d)).astype(np.float32),
        "lnf": np.ones(d, dtype=np.float32),
    }
    for layer in range(config.n_layers):
        prefix = f"layers.{layer}"
        weights[f"{prefix}.ln1"] = np.ones(d, dtype=np.float32)
        weights[f"{prefix}.ln2"] = np.ones(d, dtype=np.float32)
        weights[f"{prefix}.wq"] = rng.normal(0.0, 0.02, (d, d)).astype(np.float32)
        weights[f"{prefix}.wk"] = rng.normal(0.0, 0.02, (d, kv)).astype(np.float32)
        weights[f"{prefix}.wv"] = rng.normal(0.0, 0.02, (d, kv)).astype(np.float32)
        weights[f"{prefix}.wo"] = rng.normal(0.0, 0.02, (d, d)).astype(np.float32)
        weights[f"{prefix}.w1"] = rng.normal(0.0, 0.02, (d, config.ffn_dim)).astype(np.float32)
        weights[f"{prefix}.w2"] = rng.normal(0.0, 0.02, (config.ffn_dim, d)).astype(np.float32)
        weights[f"{prefix}.w3"] = rng.normal(0.0, 0.02, (d, config.ffn_dim)).astype(np.float32)
    return weights


def _rms_forward(x: np.ndarray, w: np.ndarray, eps: float):
    rms = np.sqrt(np.mean(np.square(x), axis=-1, keepdims=True) + eps)
    y = (x / rms) * w
    return y, rms


def _rms_backward(dy: np.ndarray, x: np.ndarray, w: np.ndarray, rms: np.ndarray):
    dw = np.sum(dy * x / rms, axis=tuple(range(dy.ndim - 1)))
    mean_term = np.mean((w * dy) * x, axis=-1, keepdims=True) / (rms ** 3)
    dx = (w * dy) / rms - x * mean_term
    return dx, dw


@dataclass
class TrainCache:
    ids: np.ndarray
    targets: np.ndarray
    x0: np.ndarray
    layers: list[dict]
    hidden: np.ndarray
    rms_final: np.ndarray


def forward_train(
    weights: dict[str, np.ndarray],
    config: ModelConfig,
    ids: np.ndarray,
    cos: np.ndarray,
    sin: np.ndarray,
    *,
    need_cache: bool = True,
):
    """Teacher-forced forward.

    Shapes: ``ids``/``targets`` are ``[B, T]``; logits are ``[B, T, V]``.
    """
    b, t = ids.shape
    d = config.d_model
    x = weights["tok_emb"][ids]
    layers: list[dict] = []
    positions = np.arange(t, dtype=np.int64)
    scale = 1.0 / np.sqrt(config.head_dim)

    for layer in range(config.n_layers):
        prefix = f"layers.{layer}"
        x_in = x
        h1, rms1 = _rms_forward(x, weights[f"{prefix}.ln1"], config.rms_norm_eps)
        q = (h1 @ weights[f"{prefix}.wq"]).reshape(b, t, config.n_heads, config.head_dim)
        k = (h1 @ weights[f"{prefix}.wk"]).reshape(b, t, config.n_kv_heads, config.head_dim)
        v = (h1 @ weights[f"{prefix}.wv"]).reshape(b, t, config.n_kv_heads, config.head_dim)
        q_rot = apply_rope(q.reshape(b * t, config.n_heads, -1), np.tile(positions, b), cos, sin).reshape(b, t, config.n_heads, -1)
        k_rot = apply_rope(k.reshape(b * t, config.n_kv_heads, -1), np.tile(positions, b), cos, sin).reshape(b, t, config.n_kv_heads, -1)
        repeat = config.n_heads // config.n_kv_heads
        k_exp = np.repeat(k_rot, repeat, axis=2)
        v_exp = np.repeat(v, repeat, axis=2)

        # [B, H, T, D]
        qh = q_rot.transpose(0, 2, 1, 3)
        kh = k_exp.transpose(0, 2, 1, 3)
        vh = v_exp.transpose(0, 2, 1, 3)
        scores = np.einsum("bhtd,bhsd->bhts", qh, kh, optimize=True) * scale
        mask = np.tril(np.ones((t, t), dtype=bool))
        window = config.window_size if layer in config.swa_layers else None
        if window is not None:
            rows = np.arange(t)[:, None]
            cols = np.arange(t)[None, :]
            mask &= cols > (rows - window)
        scores = np.where(mask[None, None, :, :], scores, -np.inf)
        probs = softmax(scores, axis=-1)
        context = np.einsum("bhts,bhsd->bhtd", probs, vh, optimize=True)
        context_flat = context.transpose(0, 2, 1, 3).reshape(b, t, d)
        attn_out = context_flat @ weights[f"{prefix}.wo"]
        x1 = x + attn_out

        h2, rms2 = _rms_forward(x1, weights[f"{prefix}.ln2"], config.rms_norm_eps)
        a = h2 @ weights[f"{prefix}.w1"]
        gate = h2 @ weights[f"{prefix}.w3"]
        act = silu(a) * gate
        ffn_out = act @ weights[f"{prefix}.w2"]
        x = x1 + ffn_out

        if need_cache:
            layers.append(
                dict(
                    x_in=x_in, h1=h1, rms1=rms1, q=q, k=k, v=v, q_rot=q_rot,
                    k_rot=k_rot, probs=probs, context=context,
                    context_flat=context_flat, x1=x1, h2=h2, rms2=rms2,
                    a=a, gate=gate, act=act, ffn_out=ffn_out,
                )
            )

    hidden, rms_final = _rms_forward(x, weights["lnf"], config.rms_norm_eps)
    if config.tie_embeddings:
        logits = hidden @ weights["tok_emb"].T
    else:
        logits = hidden @ weights["lm_head"].T
    cache = TrainCache(ids, np.zeros_like(ids), weights["tok_emb"][ids], layers, hidden, rms_final)
    return logits, cache


def loss_and_grads(
    weights: dict[str, np.ndarray],
    config: ModelConfig,
    ids: np.ndarray,
    targets: np.ndarray,
    cos: np.ndarray,
    sin: np.ndarray,
) -> tuple[float, dict[str, np.ndarray]]:
    logits, cache = forward_train(weights, config, ids, cos, sin)
    b, t, v = logits.shape
    shifted = logits - np.max(logits, axis=-1, keepdims=True)
    log_probs = shifted - np.log(np.sum(np.exp(shifted), axis=-1, keepdims=True))
    loss = float(-np.mean(log_probs.reshape(-1, v)[np.arange(b * t), targets.reshape(-1)]))

    dlogits = np.exp(log_probs)
    flat = dlogits.reshape(-1, v)
    flat[np.arange(b * t), targets.reshape(-1)] -= 1.0
    dlogits /= b * t

    grads: dict[str, np.ndarray] = {}
    if config.tie_embeddings:
        d_hidden = dlogits @ weights["tok_emb"]
        grads["tok_emb"] = dlogits.reshape(-1, v).T @ cache.hidden.reshape(-1, config.d_model)
    else:
        d_hidden = dlogits @ weights["lm_head"]
        grads["lm_head"] = dlogits.reshape(-1, v).T @ cache.hidden.reshape(-1, config.d_model)
    # ``cache.hidden`` is already normalized; the RMS input is the residual
    # stream.  Recover it from the cached normalized value.
    x_before_final = cache.hidden * cache.rms_final / weights["lnf"]
    dx, grads["lnf"] = _rms_backward(d_hidden, x_before_final, weights["lnf"], cache.rms_final)

    for layer in reversed(range(config.n_layers)):
        prefix = f"layers.{layer}"
        c = cache.layers[layer]
        dffn = dx
        grads[f"{prefix}.w2"] = c["act"].reshape(-1, config.ffn_dim).T @ dffn.reshape(-1, config.d_model)
        dact = dffn @ weights[f"{prefix}.w2"].T
        da = dact * c["gate"] * sigmoid(c["a"]) * (1.0 + c["a"] * (1.0 - sigmoid(c["a"])))
        dgate = dact * silu(c["a"])
        grads[f"{prefix}.w1"] = c["h2"].reshape(-1, config.d_model).T @ da.reshape(-1, config.ffn_dim)
        grads[f"{prefix}.w3"] = c["h2"].reshape(-1, config.d_model).T @ dgate.reshape(-1, config.ffn_dim)
        dh2 = da @ weights[f"{prefix}.w1"].T + dgate @ weights[f"{prefix}.w3"].T
        dx1, grads[f"{prefix}.ln2"] = _rms_backward(dh2, c["x1"], weights[f"{prefix}.ln2"], c["rms2"])
        dx1 = dx1 + dx

        dattn = dx1
        grads[f"{prefix}.wo"] = c["context_flat"].reshape(-1, config.d_model).T @ dattn.reshape(-1, config.d_model)
        dcontext = (dattn @ weights[f"{prefix}.wo"].T).reshape(b, t, config.n_heads, config.head_dim)
        dcontext_h = dcontext.transpose(0, 2, 1, 3)
        repeat = config.n_heads // config.n_kv_heads

        probs = c["probs"]
        dprobs = np.einsum("bhtd,bhsd->bhts", dcontext_h, np.repeat(c["v"].transpose(0, 2, 1, 3), repeat, axis=1), optimize=True)
        dvh = np.einsum("bhts,bhtd->bhsd", probs, dcontext_h, optimize=True)
        dscores = probs * (dprobs - np.sum(dprobs * probs, axis=-1, keepdims=True))
        scale = 1.0 / np.sqrt(config.head_dim)
        dqh = np.einsum("bhts,bhsd->bhtd", dscores, np.repeat(c["k_rot"].transpose(0, 2, 1, 3), repeat, axis=1), optimize=True) * scale
        dkh = np.einsum("bhts,bhtd->bhsd", dscores, c["q_rot"].transpose(0, 2, 1, 3), optimize=True) * scale
        # Sum the repeated GQA heads back into the KV head.
        dk_exp = dkh.transpose(0, 2, 1, 3).reshape(b, t, config.n_heads, config.head_dim)
        dv_exp = dvh.transpose(0, 2, 1, 3).reshape(b, t, config.n_heads, config.head_dim)
        dk = dk_exp.reshape(b, t, config.n_kv_heads, repeat, config.head_dim).sum(axis=3)
        dv = dv_exp.reshape(b, t, config.n_kv_heads, repeat, config.head_dim).sum(axis=3)

        dq_rot = dqh.transpose(0, 2, 1, 3)
        dk_rot = dk
        positions_tiled = np.tile(np.arange(t, dtype=np.int64), b)
        dq = apply_rope_backward(dq_rot.reshape(b * t, config.n_heads, -1), positions_tiled, cos, sin).reshape(b, t, config.n_heads, config.head_dim)
        dk = apply_rope_backward(dk_rot.reshape(b * t, config.n_kv_heads, -1), positions_tiled, cos, sin).reshape(b, t, config.n_kv_heads, config.head_dim)

        grads[f"{prefix}.wq"] = c["h1"].reshape(-1, config.d_model).T @ dq.reshape(-1, config.d_model)
        grads[f"{prefix}.wk"] = c["h1"].reshape(-1, config.d_model).T @ dk.reshape(-1, config.kv_dim)
        grads[f"{prefix}.wv"] = c["h1"].reshape(-1, config.d_model).T @ dv.reshape(-1, config.kv_dim)
        dh1 = (
            dq.reshape(-1, config.d_model) @ weights[f"{prefix}.wq"].T
            + dk.reshape(-1, config.kv_dim) @ weights[f"{prefix}.wk"].T
            + dv.reshape(-1, config.kv_dim) @ weights[f"{prefix}.wv"].T
        )
        dln1, grads[f"{prefix}.ln1"] = _rms_backward(
            dh1.reshape(b, t, config.d_model), c["x_in"],
            weights[f"{prefix}.ln1"], c["rms1"],
        )
        dx = dln1 + dx1

    # Token embedding gradient (tied output projection already accumulated).
    grad_tok = grads.pop("tok_emb", np.zeros_like(weights["tok_emb"]))
    np.add.at(grad_tok, cache.ids.reshape(-1), dx.reshape(-1, config.d_model))
    grads["tok_emb"] = grad_tok
    return loss, grads


class Adam:
    def __init__(self, params: dict[str, np.ndarray], lr: float = 3e-3, betas=(0.9, 0.95), eps=1e-8, weight_decay=0.01):
        self.params = params
        self.lr = lr
        self.betas = betas
        self.eps = eps
        self.weight_decay = weight_decay
        self.m = {k: np.zeros_like(v) for k, v in params.items()}
        self.v = {k: np.zeros_like(v) for k, v in params.items()}
        self.t = 0

    def step(self, grads: dict[str, np.ndarray]) -> None:
        self.t += 1
        b1, b2 = self.betas
        for key, grad in grads.items():
            if key not in self.params:
                continue
            g = grad.astype(np.float32, copy=False)
            self.m[key] = b1 * self.m[key] + (1 - b1) * g
            self.v[key] = b2 * self.v[key] + (1 - b2) * (g * g)
            m_hat = self.m[key] / (1 - b1**self.t)
            v_hat = self.v[key] / (1 - b2**self.t)
            update = m_hat / (np.sqrt(v_hat) + self.eps)
            if self.weight_decay and not key.endswith("ln1") and not key.endswith("ln2") and key != "lnf":
                update = update + self.weight_decay * self.params[key]
            self.params[key] -= self.lr * update


def clip_grads(grads: dict[str, np.ndarray], max_norm: float) -> float:
    total = np.sqrt(sum(float(np.sum(g * g)) for g in grads.values()))
    if total > max_norm and total > 0:
        factor = max_norm / total
        for key in grads:
            grads[key] *= factor
    return total
