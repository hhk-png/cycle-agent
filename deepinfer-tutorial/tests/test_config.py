from __future__ import annotations

import pytest

from mini_deepinfer.config import EngineConfig, ModelConfig, ParallelConfig
from mini_deepinfer.errors import ConfigError
from mini_deepinfer.llm import LLM


@pytest.mark.parametrize(
    "overrides",
    [
        {"block_size": 0},
        {"num_blocks": 0},
        {"max_num_seqs": 0},
        {"max_num_batched_tokens": 0},
        {"speculative_tokens": -1},
    ],
)
def test_engine_config_rejects_non_positive_values(overrides) -> None:
    with pytest.raises(ConfigError):
        EngineConfig(**overrides)


def test_engine_config_rejects_block_larger_than_token_budget() -> None:
    with pytest.raises(ConfigError):
        EngineConfig(block_size=64, max_num_batched_tokens=32)


def test_engine_config_requires_chunked_prefill_for_small_budget() -> None:
    with pytest.raises(ConfigError):
        EngineConfig(
            max_num_batched_tokens=128,
            max_model_len=256,
            enable_chunked_prefill=False,
        )


def test_engine_config_rejects_speculative_window_larger_than_block() -> None:
    with pytest.raises(ConfigError):
        EngineConfig(block_size=4, speculative_tokens=4)


def test_engine_config_rejects_kv_capacity_below_model_length() -> None:
    with pytest.raises(ConfigError):
        EngineConfig(block_size=16, num_blocks=4, max_model_len=128)


def test_parallel_config_rejects_non_positive_sizes() -> None:
    with pytest.raises(ConfigError):
        ParallelConfig(expert_parallel_size=0)


def test_model_config_rejects_bad_head_geometry() -> None:
    with pytest.raises(ConfigError):
        ModelConfig(d_model=64, n_heads=3, head_dim=16)
    with pytest.raises(ConfigError):
        ModelConfig(d_model=64, n_kv_heads=0)


def test_model_config_rejects_out_of_range_layer_indices() -> None:
    with pytest.raises(ConfigError):
        ModelConfig(swa_layers=(9,))
    with pytest.raises(ConfigError):
        ModelConfig(n_encoder_layers=99)


def test_llm_rejects_model_length_beyond_checkpoint() -> None:
    with pytest.raises(ValueError):
        LLM(
            model="mini_deepinfer/assets/tiny_lm.npz",
            config=EngineConfig(
                max_model_len=1024,
                num_blocks=256,
                block_size=16,
                max_num_batched_tokens=256,
            ),
        )
