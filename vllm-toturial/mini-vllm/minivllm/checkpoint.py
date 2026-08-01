"""Save / load a model checkpoint plus config and tokenizer.

A checkpoint is a directory with three files::

    checkpoint/
        model.npz       -- all weights (np.savez)
        config.json     -- EngineConfig (and its sub-configs)
        tokenizer.json  -- CharTokenizer vocabulary

This is the mini equivalent of a HuggingFace model repository (``pytorch_model.bin``
+ ``config.json`` + ``tokenizer.json``) that vLLM loads from disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Union

import numpy as np

from .config import EngineConfig
from .model import TinyGPT
from .tokenizer import CharTokenizer

# per-layer parameters are stored as Python lists of arrays in the model and
# flattened to a single (n_layers, ...) array on disk.
_LIST_KEYS = (
    "ln1_w", "ln1_b", "c_attn_w", "c_attn_b", "c_proj_w", "c_proj_b",
    "ln2_w", "ln2_b", "c_mlp_w", "c_mlp_b", "c_mlp_p_w", "c_mlp_p_b",
)


def _flatten_state_dict(sd: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    out = {}
    for k, v in sd.items():
        out[k] = np.stack(v) if isinstance(v, list) else v
    return out


def _unflatten_state_dict(flat: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    out = {}
    for k, v in flat.items():
        if k in _LIST_KEYS and v.ndim >= 2:
            out[k] = [v[i] for i in range(v.shape[0])]
        else:
            out[k] = v
    return out


def save_checkpoint(model: TinyGPT, config: EngineConfig, tokenizer: CharTokenizer,
                    path: Union[str, Path]) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path / "model.npz", **_flatten_state_dict(model.state_dict()))
    with open(path / "config.json", "w", encoding="utf-8") as f:
        json.dump(config.to_json(), f, indent=2, ensure_ascii=False)
    tokenizer.save(path / "tokenizer.json")


def load_checkpoint(path: Union[str, Path]):
    """Load a checkpoint and return ``(TinyGPT, EngineConfig, CharTokenizer)``."""
    path = Path(path)
    with np.load(path / "model.npz") as npz:
        flat = {k: v for k, v in npz.items()}
    with open(path / "config.json", "r", encoding="utf-8") as f:
        cfg = EngineConfig.from_json(json.load(f))
    tokenizer = CharTokenizer.load(path / "tokenizer.json")

    # make sure the loaded weights match the declared config
    cfg.model.vocab_size = tokenizer.vocab_size
    cfg.model.block_size = max(cfg.model.block_size, cfg.max_model_len or 0)
    model = TinyGPT(cfg.model)
    model.load_state_dict(_unflatten_state_dict(flat))
    return model, cfg, tokenizer
