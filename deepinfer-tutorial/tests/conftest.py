from __future__ import annotations

from pathlib import Path

import pytest

from mini_deepinfer.config import EngineConfig
from mini_deepinfer.model.tiny_transformer import TinyTransformer


ASSETS = Path(__file__).resolve().parents[1] / "mini_deepinfer" / "assets"


@pytest.fixture(scope="session")
def model() -> TinyTransformer:
    return TinyTransformer.from_file(ASSETS / "tiny_lm.npz")


@pytest.fixture(scope="session")
def draft_model() -> TinyTransformer:
    return TinyTransformer.from_file(ASSETS / "draft_lm.npz")


@pytest.fixture()
def engine_config() -> EngineConfig:
    return EngineConfig(
        block_size=16,
        num_blocks=128,
        max_num_seqs=8,
        max_num_batched_tokens=256,
        max_model_len=256,
        enable_chunked_prefill=True,
    )
