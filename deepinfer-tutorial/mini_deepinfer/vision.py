"""A tiny vision encoder lab for the EPD story.

It is a patch embedding plus one transformer block and an aligner, not a
faithful DeepSeek-ViT.  It demonstrates how image patches become LLM tokens
and why the vision encoder can be deployed as a separate E stage.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .model.ops import attention, rms_norm, silu


@dataclass
class TinyViTConfig:
    image_size: int = 16
    patch_size: int = 4
    in_channels: int = 3
    d_model: int = 32
    n_heads: int = 2
    ffn_dim: int = 64
    out_dim: int = 64
    seed: int = 0


class TinyViT:
    def __init__(self, config: TinyViTConfig | None = None) -> None:
        self.config = config or TinyViTConfig()
        cfg = self.config
        if cfg.image_size % cfg.patch_size:
            raise ValueError("image_size must be divisible by patch_size")
        self.grid = cfg.image_size // cfg.patch_size
        self.num_patches = self.grid * self.grid
        rng = np.random.default_rng(cfg.seed)
        d = cfg.d_model
        patch_dim = cfg.patch_size * cfg.patch_size * cfg.in_channels
        self.patch_proj = rng.normal(0, 0.02, (patch_dim, d)).astype(np.float32)
        self.pos = rng.normal(0, 0.02, (self.num_patches, d)).astype(np.float32)
        self.wq = rng.normal(0, 0.02, (d, d)).astype(np.float32)
        self.wk = rng.normal(0, 0.02, (d, d)).astype(np.float32)
        self.wv = rng.normal(0, 0.02, (d, d)).astype(np.float32)
        self.wo = rng.normal(0, 0.02, (d, d)).astype(np.float32)
        self.w1 = rng.normal(0, 0.02, (d, cfg.ffn_dim)).astype(np.float32)
        self.w3 = rng.normal(0, 0.02, (d, cfg.ffn_dim)).astype(np.float32)
        self.w2 = rng.normal(0, 0.02, (cfg.ffn_dim, d)).astype(np.float32)
        self.ln1 = np.ones(d, dtype=np.float32)
        self.ln2 = np.ones(d, dtype=np.float32)
        self.aligner = rng.normal(0, 0.02, (d, cfg.out_dim)).astype(np.float32)

    def patchify(self, image: np.ndarray) -> np.ndarray:
        cfg = self.config
        image = np.asarray(image, dtype=np.float32)
        if image.shape != (cfg.image_size, cfg.image_size, cfg.in_channels):
            raise ValueError(
                f"expected image shape {(cfg.image_size, cfg.image_size, cfg.in_channels)}, "
                f"got {image.shape}"
            )
        patches = []
        for row in range(self.grid):
            for col in range(self.grid):
                patch = image[
                    row * cfg.patch_size : (row + 1) * cfg.patch_size,
                    col * cfg.patch_size : (col + 1) * cfg.patch_size,
                    :,
                ]
                patches.append(patch.reshape(-1))
        return np.stack(patches)

    def encode(self, image: np.ndarray) -> np.ndarray:
        cfg = self.config
        tokens = self.patchify(image) @ self.patch_proj + self.pos
        h, _ = rms_norm(tokens, self.ln1, 1e-5)
        q = (h @ self.wq).reshape(-1, cfg.n_heads, cfg.d_model // cfg.n_heads)
        k = (h @ self.wk).reshape(-1, cfg.n_heads, cfg.d_model // cfg.n_heads)
        v = (h @ self.wv).reshape(-1, cfg.n_heads, cfg.d_model // cfg.n_heads)
        mask = np.ones((tokens.shape[0], tokens.shape[0]), dtype=bool)
        context = attention(q, k, v, mask).reshape(-1, cfg.d_model)
        tokens = tokens + context @ self.wo
        h, _ = rms_norm(tokens, self.ln2, 1e-5)
        tokens = tokens + (silu(h @ self.w1) * (h @ self.w3)) @ self.w2
        return tokens @ self.aligner
