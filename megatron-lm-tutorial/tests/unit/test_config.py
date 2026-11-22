import pytest

from mini_megatron.config import ExperimentConfig, ModelConfig, ParallelConfig


def test_roundtrip(tmp_path, tiny_config):
    path = tmp_path / "cfg.json"
    tiny_config.save_json(path)
    loaded = ExperimentConfig.from_json(path)
    assert loaded.to_dict() == tiny_config.to_dict()


def test_invalid_tp_divisibility():
    cfg = ExperimentConfig(
        model=ModelConfig(hidden_size=64, num_attention_heads=4, num_query_groups=2),
        parallel=ParallelConfig(tensor_model_parallel_size=3),
    )
    with pytest.raises(ValueError):
        cfg.validate(world_size=3)


def test_invalid_batch_accumulation(tiny_config):
    tiny_config.train.global_batch_size = 3
    with pytest.raises(ValueError):
        tiny_config.validate(world_size=1)


def test_sequence_parallel_and_cp_disallowed(tiny_config):
    tiny_config.parallel.tensor_model_parallel_size = 2
    tiny_config.parallel.sequence_parallel = True
    tiny_config.parallel.context_parallel_size = 2
    with pytest.raises(ValueError):
        tiny_config.validate(world_size=4)


def test_tied_embeddings_require_pp1(tiny_config):
    tiny_config.model.tie_word_embeddings = True
    tiny_config.parallel.pipeline_model_parallel_size = 2
    with pytest.raises(ValueError):
        tiny_config.validate(world_size=2)


def test_seq_len_must_divide_sp_and_cp(tiny_config):
    tiny_config.parallel.tensor_model_parallel_size = 2
    tiny_config.parallel.sequence_parallel = True
    tiny_config.train.seq_len = 17
    with pytest.raises(ValueError):
        tiny_config.validate(world_size=2)

    tiny_config = ExperimentConfig.from_dict(tiny_config.to_dict())
    tiny_config.parallel.context_parallel_size = 2
    tiny_config.parallel.sequence_parallel = False
    tiny_config.train.seq_len = 17
    with pytest.raises(ValueError):
        tiny_config.validate(world_size=2)


def test_rope_requires_even_head_dim(tiny_config):
    tiny_config.model.hidden_size = 36
    tiny_config.model.num_attention_heads = 4  # head_dim = 9
    tiny_config.model.num_query_groups = 4
    with pytest.raises(ValueError, match="even"):
        tiny_config.validate(world_size=1)


def test_zero_query_groups_is_value_error():
    cfg = ExperimentConfig(
        model=ModelConfig(num_query_groups=0),
        parallel=ParallelConfig(),
    )
    with pytest.raises(ValueError):
        cfg.validate(world_size=1)
