from __future__ import annotations

import pytest

from mini_verl.config import PPOConfig, load_config


def test_defaults_are_valid():
    config = PPOConfig()
    config.validate()
    assert config.algorithm.adv_estimator == "grpo"
    assert config.actor_rollout_ref.rollout.n == 8
    assert config.trainer.n_gpus_per_node == 1


def test_override_nested():
    config = PPOConfig()
    config.apply_overrides(
        [
            "algorithm.adv_estimator=gae",
            "actor_rollout_ref.actor.ppo_epochs=2",
            "actor_rollout_ref.actor.optim.lr=0.02",
            "trainer.total_training_steps=3",
            "data.train_batch_size=8",
            "actor_rollout_ref.actor.ppo_mini_batch_size=32",
        ]
    )
    assert config.algorithm.adv_estimator == "gae"
    assert config.actor_rollout_ref.actor.ppo_epochs == 2
    assert config.actor_rollout_ref.actor.optim.lr == 0.02
    assert config.trainer.total_training_steps == 3


def test_unknown_key_fails():
    with pytest.raises(KeyError):
        PPOConfig().apply_overrides(["nope=1"])


def test_yaml_roundtrip(tmp_path):
    config = PPOConfig()
    config.algorithm.adv_estimator = "rloo"
    config.actor_rollout_ref.rollout.n = 4
    path = tmp_path / "config.yaml"
    config.to_yaml(path)
    loaded = load_config(path)
    assert loaded.algorithm.adv_estimator == "rloo"
    assert loaded.actor_rollout_ref.rollout.n == 4


def test_invalid_values_fail():
    config = PPOConfig()
    config.actor_rollout_ref.rollout.n = 0
    with pytest.raises(ValueError):
        config.validate()

    config = PPOConfig()
    config.actor_rollout_ref.actor.loss_agg_mode = "nonsense"
    with pytest.raises(ValueError):
        config.validate()

    config = PPOConfig()
    config.trainer.trainer_mode = "turbo"
    with pytest.raises(ValueError):
        config.validate()


def test_bare_override_in_config_position():
    # argparse assigns the first positional to ``config``; a bare key=value
    # must still be treated as an override, as documented in the README.
    config = load_config("trainer.total_training_steps=3", ["algorithm.adv_estimator=gae"])
    assert config.trainer.total_training_steps == 3
    assert config.algorithm.adv_estimator == "gae"


def test_append_extra():
    config = PPOConfig()
    config.apply_overrides(["+dpo_beta=0.2"])
    assert config.extra["dpo_beta"] == 0.2


def test_coercion_bool_int_float():
    config = PPOConfig()
    config.apply_overrides(
        [
            "actor_rollout_ref.actor.use_kl_loss=true",
            "actor_rollout_ref.actor.kl_loss_coef=0.02",
            "trainer.val_before_train=false",
            "trainer.total_training_steps=null",
        ]
    )
    assert config.actor_rollout_ref.actor.use_kl_loss is True
    assert config.actor_rollout_ref.actor.kl_loss_coef == 0.02
    assert config.trainer.val_before_train is False
    assert config.trainer.total_training_steps is None


def test_resolve_path():
    config = PPOConfig()
    assert config.resolve_path("algorithm.adv_estimator") == "grpo"
    with pytest.raises(KeyError):
        config.resolve_path("algorithm.nope")
