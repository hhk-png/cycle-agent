"""Weight-only int8 quantization.

Real vLLM ships several quantisation backends (GPTQ, AWQ, FP8, GGUF) that
keep the weights in low precision *and* run de-quantised or fused kernels at
inference time.  The mini version implements the data-structure half of that
story:

1. symmetric, per-column ``int8`` quantisation of every 2-D weight matrix;
2. reporting of the memory saving;
3. de-quantising back to ``float32`` so the existing NumPy matmuls still work.

This keeps the demo self-contained while faithfully showing the mechanics and
the accuracy cost (quantisation error) that motivate vLLM's more advanced
schemes (group-wise scales, KV-cache quantisation, ...).
"""

from __future__ import annotations

from typing import Dict, Tuple

import numpy as np

from .checkpoint import _flatten_state_dict, _unflatten_state_dict
from .model import TinyGPT


def quantize_matrix(w: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Symmetric per-column int8 quantization.

    ``w`` has shape ``(in_features, out_features)``; the scale is chosen per
    output column so that ``max(|w| / scale) <= 127``.
    """
    w64 = w.astype(np.float64)
    scale = np.max(np.abs(w64), axis=0) / 127.0
    scale = np.where(scale == 0, 1.0, scale)  # avoid division by zero
    q = np.round(w64 / scale).astype(np.int8)
    return q, scale.astype(np.float32)


def quantize_state_dict(sd: Dict[str, np.ndarray]):
    """Quantize every 2-D weight matrix; non-matrix tensors (e.g. layer-norm b)
    stay as-is.  Checkpoint ``state_dict``'s are flattened, so per-layer weight
    matrices (``wte``/``wpe`` plus the ``*_w`` projections) arrive as either
    plain 2-D arrays or ``(n_layers, in, out)`` 3-D stacks; each layer is
    quantised independently with its own per-output-column scale, exactly like
    a real engine quantises each weight tensor on its own.

    Returns ``{name: (q_int8, scale_or_None)}``.
    """
    qd: Dict[str, tuple] = {}
    for k, v in sd.items():
        if v.ndim == 2:
            qd[k] = quantize_matrix(v)
        elif v.ndim == 3:
            # (n_layers, in, out): quantise per layer, keep one per-output-
            # column scale per layer; the (n_layers, 1, out) shape lets the
            # scale broadcast against the (n_layers, in, out) tensor.
            qs, ss = [], []
            for layer in range(v.shape[0]):
                q, s = quantize_matrix(v[layer])
                qs.append(q)
                ss.append(s[None, :])          # (1, out) -> stack to (L, 1, out)
            qd[k] = (np.stack(qs), np.stack(ss))
        else:
            qd[k] = (v, None)
    return qd


def dequantize_state_dict(qd) -> Dict[str, np.ndarray]:
    sd: Dict[str, np.ndarray] = {}
    for k, (q, scale) in qd.items():
        sd[k] = q.astype(np.float32) * scale if scale is not None else q
    return sd


def quantize_model(model: TinyGPT) -> Tuple[TinyGPT, Dict[str, float]]:
    """Return ``(dequantized_model, stats)`` simulating an int8 engine load."""
    flat = _flatten_state_dict(model.state_dict())
    qd = quantize_state_dict(flat)

    orig_bytes = sum(v.nbytes for v in flat.values())
    q_bytes = sum(q.nbytes + (s.nbytes if s is not None else 0) for q, s in qd.values())

    deq = dequantize_state_dict(qd)
    max_err = max(float(np.max(np.abs(deq[k] - flat[k]))) for k in flat)

    new_model = TinyGPT(model.config)
    new_model.load_state_dict(_unflatten_state_dict(deq))
    stats = {
        "original_bytes": float(orig_bytes),
        "quantized_bytes": float(q_bytes),
        "compression_ratio": q_bytes / orig_bytes,
        "max_abs_error": max_err,
    }
    return new_model, stats
