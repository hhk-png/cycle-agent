from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
import torch

from mini_megatron.parallel import comm
from mini_megatron.parallel.state import destroy_model_parallel


@pytest.fixture(autouse=True)
def clean_parallel_state():
    destroy_model_parallel()
    yield
    comm.destroy()
    destroy_model_parallel()


@pytest.fixture
def tiny_config():
    from mini_megatron.config import ExperimentConfig

    return ExperimentConfig.from_json(ROOT / "configs" / "tiny.json")


@pytest.fixture(autouse=True)
def deterministic():
    torch.manual_seed(7)
    yield
