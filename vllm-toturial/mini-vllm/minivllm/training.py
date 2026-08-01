"""Train a TinyGPT from scratch using pure NumPy (educational).

vLLM does not train models; it *serves* pre-trained ones.  This module exists
only so the tutorial can produce real weights for the mini engine without
depending on the HuggingFace hub.  It implements the standard GPT-2 training
loop: causal multi-head attention, MLP blocks, layer norms and a tied word
embedding, with a full backward pass in NumPy.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np

from .model import TinyGPT

_SQRT_2PI = float(np.sqrt(2.0 / np.pi))


def _gelu(x: np.ndarray) -> np.ndarray:
    return 0.5 * x * (1.0 + np.tanh(_SQRT_2PI * (x + 0.044715 * x ** 3)))


def _gelu_grad(x: np.ndarray) -> np.ndarray:
    t = np.tanh(_SQRT_2PI * (x + 0.044715 * x ** 3))
    inner = 1.0 + 3.0 * 0.044715 * x ** 2
    return 0.5 * (1.0 + t) + 0.5 * x * (1.0 - t ** 2) * _SQRT_2PI * inner


def _softmax(x: np.ndarray) -> np.ndarray:
    x = x - np.max(x, axis=-1, keepdims=True)
    e = np.exp(x)
    return e / np.sum(e, axis=-1, keepdims=True)


def _ln_forward(x: np.ndarray, w: np.ndarray, b: np.ndarray, eps: float = 1e-5):
    mean = x.mean(axis=-1, keepdims=True)
    var = x.var(axis=-1, keepdims=True)
    xhat = (x - mean) / np.sqrt(var + eps)
    return xhat * w + b, (x, mean, var, eps)


class TinyGPTTrainer:
    """Training loop helpers over a :class:`TinyGPT` instance."""

    def __init__(self, model: TinyGPT, lr: float = 0.02, momentum: float = 0.9):
        self.model = model
        self.cfg = model.config
        self.lr = lr
        self.momentum = momentum
        self._velocities: Dict[str, List[np.ndarray]] = {}

    # ------------------------------------------------------------------ #
    def forward(self, x: np.ndarray) -> dict:
        """Full transformer forward.  Returns intermediate activations."""
        cfg = self.cfg
        B, T = x.shape
        d = cfg.n_embd
        positions = np.broadcast_to(np.arange(T, dtype=np.int64)[None, :], (B, T))

        tok_emb = self.model.wte[x]
        pos_emb = self.model.wpe[positions]
        h = tok_emb + pos_emb
        caches = [{"tok_emb": tok_emb, "pos_emb": pos_emb, "positions": positions, "x": x}]

        for l in range(cfg.n_layer):
            ln1_out, ln1_cache = _ln_forward(h, self.model.ln1_w[l], self.model.ln1_b[l])
            qkv = ln1_out @ self.model.c_attn_w[l] + self.model.c_attn_b[l]
            q, k, v = np.split(qkv, 3, axis=-1)
            q = q.reshape(B, T, cfg.n_head, cfg.head_dim).transpose(0, 2, 1, 3)
            k = k.reshape(B, T, cfg.n_head, cfg.head_dim).transpose(0, 2, 1, 3)
            v = v.reshape(B, T, cfg.n_head, cfg.head_dim).transpose(0, 2, 1, 3)

            scores = np.einsum("bhik,bhjk->bhij", q, k) / np.sqrt(cfg.head_dim)
            mask = np.triu(np.ones((T, T), dtype=bool), k=1)[None, None]
            scores = np.where(mask, -np.inf, scores)
            probs = _softmax(scores)
            attn = np.einsum("bhij,bhjk->bhik", probs, v)
            attn = attn.transpose(0, 2, 1, 3).reshape(B, T, d)
            h2 = h + attn @ self.model.c_proj_w[l] + self.model.c_proj_b[l]

            ln2_out, ln2_cache = _ln_forward(h2, self.model.ln2_w[l], self.model.ln2_b[l])
            mlp_pre = ln2_out @ self.model.c_mlp_w[l] + self.model.c_mlp_b[l]
            mlp_gelu = _gelu(mlp_pre)
            mlp_out = mlp_gelu @ self.model.c_mlp_p_w[l] + self.model.c_mlp_p_b[l]
            h = h2 + mlp_out

            caches.append(dict(
                l=l, h=h, ln1_cache=ln1_cache, ln1_out=ln1_out, qkv=qkv,
                q=q, k=k, v=v, scores=scores, probs=probs, attn=attn,
                h2=h2, ln2_cache=ln2_cache, ln2_out=ln2_out, mlp_pre=mlp_pre,
                mlp_gelu=mlp_gelu, mlp_out=mlp_out,
            ))

        ln_f_out, ln_f_cache = _ln_forward(h, self.model.ln_f_w, self.model.ln_f_b)
        logits = ln_f_out @ self.model.wte.T
        return dict(logits=logits, ln_f_out=ln_f_out, ln_f_cache=ln_f_cache,
                    caches=caches)

    # ------------------------------------------------------------------ #
    def compute_loss(self, logits: np.ndarray, targets: np.ndarray) -> Tuple[float, np.ndarray]:
        B, T, V = logits.shape
        probs = _softmax(logits.astype(np.float64))
        logp = np.log(np.take_along_axis(probs, targets[..., None], axis=-1)[..., 0] + 1e-12)
        loss = -float(logp.mean())
        # gradient of cross entropy: (softmax - onehot) / (B*T)
        dlogits = probs.copy()
        dlogits[np.arange(B)[:, None], np.arange(T)[None, :], targets] -= 1.0
        dlogits /= (B * T)
        return loss, dlogits

    # ------------------------------------------------------------------ #
    def backward(self, x: np.ndarray, dlogits: np.ndarray,
                 fwd: dict) -> Dict[str, list]:
        """Backprop through the whole transformer.

        Returns a gradient dict with exactly the same keys/shapes as
        ``model.state_dict()`` (lists of per-layer arrays where appropriate).
        """
        cfg = self.cfg
        B, T, d = x.shape[0], x.shape[1], cfg.n_embd
        grads: Dict[str, list] = {}

        def add_grad(name, value):
            if name not in grads:
                grads[name] = []
            grads[name].append(value)

        # ---- final layer norm + lm head (tied with wte) ----
        ln_f_out = fwd["ln_f_out"]
        dln_f = dlogits @ self.model.wte
        d_wte_head = np.einsum("btk,btv->vk", ln_f_out, dlogits)  # lm-head part

        dh = _ln_backward_unwrapped(dln_f, fwd["ln_f_cache"])
        dw, db = _ln_grads(dln_f, fwd["ln_f_cache"])
        add_grad("ln_f_w", dw)
        add_grad("ln_f_b", db)

        # ---- walk the layers in reverse ----
        n_layers = cfg.n_layer
        for li in range(n_layers - 1, -1, -1):
            c = fwd["caches"][li + 1]

            # MLP block
            d_mlp_out = dh
            d_mlp_gelu = d_mlp_out @ self.model.c_mlp_p_w[li].T
            add_grad("c_mlp_p_w", c["mlp_gelu"].reshape(-1, cfg.hidden_dim).T @
                     d_mlp_out.reshape(-1, d))
            add_grad("c_mlp_p_b", d_mlp_out.sum(axis=(0, 1)))
            d_mlp_pre = d_mlp_gelu * _gelu_grad(c["mlp_pre"])
            add_grad("c_mlp_w", c["ln2_out"].reshape(-1, d).T @ d_mlp_pre.reshape(-1, cfg.hidden_dim))
            add_grad("c_mlp_b", d_mlp_pre.sum(axis=(0, 1)))
            d_ln2 = d_mlp_pre @ self.model.c_mlp_w[li].T
            dw, db = _ln_grads(d_ln2, c["ln2_cache"])
            add_grad("ln2_w", dw)
            add_grad("ln2_b", db)
            # gradient wrt h2 = (through LN2) + (residual shortcut)
            dh = _ln_backward_unwrapped(d_ln2, c["ln2_cache"]) + dh

            # attention block
            d_attn_proj = dh  # h = h2 + mlp_out, dh is gradient wrt h2
            attn = c["attn"]  # (B, nh, T, hd) already transposed? no, attn is (B,T,d)
            d_attn = d_attn_proj @ self.model.c_proj_w[li].T  # (B,T,d)
            add_grad("c_proj_w", attn.reshape(-1, d).T @ d_attn_proj.reshape(-1, d))
            add_grad("c_proj_b", d_attn_proj.sum(axis=(0, 1)))

            d_attn = d_attn.reshape(B, T, cfg.n_head, cfg.head_dim).transpose(0, 2, 1, 3)
            probs = c["probs"]  # (B, nh, T, T)
            v = c["v"]
            dv = np.einsum("bhij,bhik->bhjk", probs, d_attn)
            dp = np.einsum("bhik,bhjk->bhij", d_attn, v)
            dscores = probs * (dp - (dp * probs).sum(axis=-1, keepdims=True))
            dscores = dscores / np.sqrt(cfg.head_dim)

            q, k = c["q"], c["k"]
            dq = np.einsum("bhij,bhjk->bhik", dscores, k)
            dk = np.einsum("bhji,bhjk->bhik", dscores, q)

            def _unhead(arr):
                return arr.transpose(0, 2, 1, 3).reshape(B, T, d)

            dqkv = np.concatenate([_unhead(dq), _unhead(dk), _unhead(dv)], axis=-1)
            d_ln1 = dqkv @ self.model.c_attn_w[li].T
            add_grad("c_attn_w", c["ln1_out"].reshape(-1, d).T @ dqkv.reshape(-1, 3 * d))
            add_grad("c_attn_b", dqkv.sum(axis=(0, 1)))

            dw, db = _ln_grads(d_ln1, c["ln1_cache"])
            add_grad("ln1_w", dw)
            add_grad("ln1_b", db)
            # gradient wrt h = (through LN1) + (residual from h2)
            dh = _ln_backward_unwrapped(d_ln1, c["ln1_cache"]) + dh

        # ---- embeddings ----
        c0 = fwd["caches"][0]
        x, positions = c0["x"], c0["positions"]
        dwte_emb = np.zeros_like(self.model.wte)
        dwpe = np.zeros_like(self.model.wpe)
        np.add.at(dwte_emb, x.ravel(), dh.reshape(-1, d))
        np.add.at(dwpe, positions.ravel(), dh.reshape(-1, d))

        # wte is shared between the embedding and the lm head
        dwte = dwte_emb + d_wte_head

        # reverse each per-layer list (we appended from layer 0 upward, but
        # backprop walked the layers in reverse order, so each list is
        # currently ordered last-layer-first)
        merged = {}
        for name, parts in grads.items():
            merged[name] = parts[::-1]
        merged["wte"] = [dwte]
        merged["wpe"] = [dwpe]
        return merged

    # ------------------------------------------------------------------ #
    def apply_grads(self, grads: Dict[str, list]) -> None:
        for name, parts in grads.items():
            current = getattr(self.model, name)
            if isinstance(current, list):
                for i, g in enumerate(parts):
                    current[i] -= self.lr * g
            else:
                setattr(self.model, name, current - self.lr * parts[0])

    def train_step(self, x: np.ndarray, targets: np.ndarray) -> float:
        fwd = self.forward(x)
        loss, dlogits = self.compute_loss(fwd["logits"], targets)
        grads = self.backward(x, dlogits, fwd)
        self.apply_grads(grads)
        return loss


# ---------------------------------------------------------------------- #
# layer-norm helpers (unwrapped so we can pass w/b explicitly)
# ---------------------------------------------------------------------- #
def _ln_forward_unwrapped(x, w, b, eps=1e-5):
    mean = x.mean(axis=-1, keepdims=True)
    var = x.var(axis=-1, keepdims=True)
    xhat = (x - mean) / np.sqrt(var + eps)
    return xhat * w + b, (x, mean, var, eps)


def _ln_backward_unwrapped(dy, cache):
    x, mean, var, eps = cache
    N = x.shape[-1]
    xc = x - mean
    inv = 1.0 / np.sqrt(var + eps)
    xhat = xc * inv
    dvar = (dy * xc).sum(axis=-1, keepdims=True) * (-0.5) * inv ** 3
    dmean = (dy * -inv).sum(axis=-1, keepdims=True)
    return dy * inv + dvar * (2.0 * xc) / N + dmean / N


def _ln_grads(dy, cache):
    x, mean, var, eps = cache
    xhat = (x - mean) / np.sqrt(var + eps)
    dw = (dy * xhat).sum(axis=(0, 1))
    db = dy.sum(axis=(0, 1))
    return dw, db
