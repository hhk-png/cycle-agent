import pytest
import torch

from mini_megatron.checkpoint_reshard import (
    _global_layer_key,
    _shard_dim,
    full_state_from_stage_states,
    merge_optimizer_state_by_name,
    optimizer_state_by_name,
    optimizer_state_dict_from_by_name,
    reshard_full_state,
    split_optimizer_state_by_name,
)
from mini_megatron.config import ExperimentConfig
from mini_megatron.optim.adamw import build_optimizer
from mini_megatron.parallel.state import initialize_model_parallel
from mini_megatron.parallel.transformer import TransformerStage
from mini_megatron.sharding import make_stage_state


def _full_state(config):
    initialize_model_parallel(1, 1, 1)
    torch.manual_seed(11)
    stage = TransformerStage(config.model, config.parallel)
    return stage.state_dict()


def test_optimizer_state_by_name_rejects_flat_shard():
    """A flat optimizer payload has no name-keyed state; reject it loudly."""
    flat_payload = {
        "format": "mini-distributed-optimizer/v1",
        "master_shard": torch.zeros(4),
        "exp_avg": torch.zeros(4),
        "exp_avg_sq": torch.zeros(4),
        "step": 3,
    }
    with pytest.raises(ValueError):
        optimizer_state_by_name(flat_payload, {"w": torch.zeros(4)})


def test_tp_pp_sharding_roundtrip_is_exact():
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    full = _full_state(cfg)
    states = {}
    for pp in range(2):
        for tp in range(2):
            states[(pp, tp)] = make_stage_state(
                full,
                pp_rank=pp,
                pp_size=2,
                tp_rank=tp,
                tp_size=2,
                num_layers=cfg.model.num_layers,
            )
    merged = full_state_from_stage_states(
        states, pp_size=2, tp_size=2, num_layers=cfg.model.num_layers
    )
    assert set(merged) == set(full)
    for key in full:
        assert torch.equal(merged[key], full[key]), key


def test_reshard_to_new_layout_then_back():
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    full = _full_state(cfg)
    cfg.parallel.tensor_model_parallel_size = 2
    cfg.parallel.pipeline_model_parallel_size = 2
    target = reshard_full_state(full, cfg)
    merged = full_state_from_stage_states(
        target, pp_size=2, tp_size=2, num_layers=cfg.model.num_layers
    )
    for key in full:
        assert torch.equal(merged[key], full[key]), key


def test_optimizer_state_reshard_roundtrip():
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    initialize_model_parallel(1, 1, 1)
    torch.manual_seed(17)
    stage = TransformerStage(cfg.model, cfg.parallel)
    optimizer = build_optimizer(stage, cfg.train)

    ids = torch.randint(0, cfg.model.vocab_size, (2, cfg.train.seq_len))
    labels = torch.randint(0, cfg.model.vocab_size, ids.shape)
    out = stage(input_ids=ids, labels=labels)
    out.loss.backward()
    optimizer.step()

    optimizer_state = optimizer.state_dict()
    full_opt = optimizer_state_by_name(optimizer_state, stage.state_dict())
    template = optimizer_state["param_groups"][0]

    full_model = stage.state_dict()
    stage_states = {}
    for pp in range(2):
        for tp in range(2):
            stage_states[(pp, tp)] = make_stage_state(
                full_model,
                pp_rank=pp,
                pp_size=2,
                tp_rank=tp,
                tp_size=2,
                num_layers=cfg.model.num_layers,
            )
    split = split_optimizer_state_by_name(full_opt, stage_states, tp_size=2)

    layers_per_stage = cfg.model.num_layers // 2
    for (pp, tp), local in split.items():
        for local_name, entry in local.items():
            global_name = _global_layer_key(local_name, pp, layers_per_stage)
            reference = full_opt[global_name]
            assert set(entry) == set(reference), local_name
            for state_key, value in reference.items():
                if state_key in {"exp_avg", "exp_avg_sq"} and _shard_dim(global_name) is not None:
                    expected = torch.chunk(
                        value, 2, dim=_shard_dim(global_name)
                    )[tp]
                else:
                    expected = value
                assert torch.equal(entry[state_key], expected), (local_name, state_key)

    merged = merge_optimizer_state_by_name(
        split, pp_size=2, tp_size=2, num_layers=cfg.model.num_layers
    )
    for name, reference in full_opt.items():
        for state_key, value in reference.items():
            assert torch.equal(merged[name][state_key], value), (name, state_key)

    local_state = optimizer_state_dict_from_by_name(
        split[(0, 0)], stage_states[(0, 0)], template
    )
    assert set(local_state["state"]) == set(range(len(split[(0, 0)])))
    assert local_state["param_groups"][0]["weight_decay"] == template["weight_decay"]


def test_gelu_fc1_fc2_sharding_roundtrip():
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    cfg.model.activation = "gelu"
    cfg.model.num_layers = 2
    initialize_model_parallel(1, 1, 1)
    torch.manual_seed(19)
    stage = TransformerStage(cfg.model, cfg.parallel)
    full = stage.state_dict()
    states = {}
    for pp in range(2):
        for tp in range(2):
            states[(pp, tp)] = make_stage_state(
                full,
                pp_rank=pp,
                pp_size=2,
                tp_rank=tp,
                tp_size=2,
                num_layers=cfg.model.num_layers,
            )
    for (pp, tp), state in states.items():
        assert state["layers.0.mlp.fc1.weight"].shape == (64, 64)
        assert state["layers.0.mlp.fc2.weight"].shape == (64, 64)
    merged = full_state_from_stage_states(
        states, pp_size=2, tp_size=2, num_layers=cfg.model.num_layers
    )
    for key in full:
        assert torch.equal(merged[key], full[key]), key


def test_tied_embedding_optimizer_state_roundtrip():
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    cfg.model.tie_word_embeddings = True
    cfg.model.num_layers = 2
    initialize_model_parallel(1, 1, 1)
    torch.manual_seed(23)
    stage = TransformerStage(cfg.model, cfg.parallel)
    optimizer = build_optimizer(stage, cfg.train)
    ids = torch.randint(0, cfg.model.vocab_size, (2, cfg.train.seq_len))
    labels = torch.randint(0, cfg.model.vocab_size, ids.shape)
    out = stage(input_ids=ids, labels=labels)
    out.loss.backward()
    optimizer.step()

    state = optimizer.state_dict()
    by_name = optimizer_state_by_name(state, stage.state_dict())
    assert "embedding.weight" in by_name
    assert "output_layer.weight" not in by_name  # tied alias, one optimizer entry

    stage_states = {
        (0, tp): make_stage_state(
            stage.state_dict(),
            pp_rank=0,
            pp_size=1,
            tp_rank=tp,
            tp_size=2,
            num_layers=cfg.model.num_layers,
        )
        for tp in range(2)
    }
    split = split_optimizer_state_by_name(by_name, stage_states, tp_size=2)
    merged = merge_optimizer_state_by_name(
        split, pp_size=1, tp_size=2, num_layers=cfg.model.num_layers
    )
    for name, reference in by_name.items():
        for key, value in reference.items():
            assert torch.equal(merged[name][key], value), (name, key)


def test_expert_replicated_moe_reshards_like_a_dense_model():
    """EP=1 experts are replicated, so TP/PP resharding is well-defined."""
    cfg = ExperimentConfig.from_json("configs/tiny_moe.json")
    full = _full_state(cfg)
    assert any(".experts." in key for key in full)
    cfg.parallel.tensor_model_parallel_size = 2
    cfg.parallel.pipeline_model_parallel_size = 2
    target = reshard_full_state(full, cfg)
    merged = full_state_from_stage_states(
        target, pp_size=2, tp_size=2, num_layers=cfg.model.num_layers
    )
    for key in full:
        assert torch.equal(merged[key], full[key]), key


def test_reshard_rejects_only_a_real_ep_target():
    cfg = ExperimentConfig.from_json("configs/tiny_moe.json")
    full = _full_state(cfg)
    cfg.parallel.expert_model_parallel_size = 2
    with pytest.raises(NotImplementedError):
        reshard_full_state(full, cfg)
