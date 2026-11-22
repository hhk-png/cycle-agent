"""Hydra-like configuration for MiniVerl.

Field names deliberately follow real verl (``actor_rollout_ref.actor``,
``algorithm.adv_estimator``, ``rollout.n``, ...) so that a MiniVerl config can
be read side by side with ``verl/trainer/config/ppo_trainer.yaml``.  The parser
supports ``a.b.c=value`` CLI overrides, which is enough for the tutorial's
experiments and keeps the dependency surface at PyYAML only.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

__all__ = [
    "OptimConfig",
    "EngineConfig",
    "ModelConfig",
    "PolicyLossConfig",
    "ActorConfig",
    "RefConfig",
    "CriticConfig",
    "RolloutConfig",
    "DataConfig",
    "AlgorithmConfig",
    "RewardConfig",
    "TrainerConfig",
    "ActorRolloutRefConfig",
    "PPOConfig",
    "load_config",
]


@dataclass
class OptimConfig:
    lr: float = 0.01
    weight_decay: float = 0.0
    betas: tuple[float, float] = (0.9, 0.999)
    eps: float = 1e-8
    clip_grad: float = 1.0
    lr_scheduler_type: str = "constant"
    lr_warmup_steps_ratio: float = 0.0
    total_training_steps: int = -1


@dataclass
class EngineConfig:
    strategy: str = "fsdp"
    param_offload: bool = False
    optimizer_offload: bool = False
    reshard_after_forward: bool = True
    ulysses_sequence_parallel_size: int = 1
    use_remove_padding: bool = False
    use_dynamic_bsz: bool = False
    ppo_max_token_len_per_gpu: int = 8192


@dataclass
class ModelConfig:
    path: str = "tiny-random"
    arch: str = "transformer"
    hidden_size: int = 64
    num_layers: int = 2
    num_heads: int = 4
    max_seq_len: int = 64
    enable_gradient_checkpointing: bool = False
    use_remove_padding: bool = False
    lora_rank: int = 0
    lora_alpha: int = 16
    lora_dropout: float = 0.0
    lora_targets: tuple[str, ...] | None = None


@dataclass
class PolicyLossConfig:
    """Policy-loss selector, analogous to ``actor.policy_loss.loss_mode``."""

    loss_mode: str = "vanilla"  # vanilla | gspo | cispo | dapo (dapo = vanilla + clip-higher)


@dataclass
class ActorConfig:
    strategy: str = "fsdp"
    rollout_n: int = 1
    ppo_mini_batch_size: int = 64
    ppo_micro_batch_size: int | None = 64
    ppo_epochs: int = 1
    clip_ratio: float = 0.2
    clip_ratio_low: float = 0.2
    clip_ratio_high: float = 0.2
    clip_ratio_c: float = 3.0
    entropy_coeff: float = 0.0
    calculate_entropy: bool = True
    use_kl_loss: bool = False
    kl_loss_coef: float = 0.001
    kl_loss_type: str = "low_var_kl"
    loss_agg_mode: str = "token-mean"
    policy_loss: PolicyLossConfig = field(default_factory=PolicyLossConfig)
    use_dynamic_bsz: bool = False
    ppo_max_token_len_per_gpu: int = 8192
    shuffle: bool = True
    optim: OptimConfig = field(default_factory=OptimConfig)
    fsdp_config: EngineConfig = field(default_factory=EngineConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)


@dataclass
class RefConfig:
    strategy: str = "fsdp"
    log_prob_micro_batch_size: int | None = 16
    fsdp_config: EngineConfig = field(default_factory=lambda: EngineConfig(reshard_after_forward=True))
    engine: EngineConfig = field(default_factory=EngineConfig)


@dataclass
class CriticConfig:
    strategy: str = "fsdp"
    enable: bool | None = None
    ppo_mini_batch_size: int = 64
    ppo_micro_batch_size: int | None = 64
    ppo_epochs: int = 1
    cliprange_value: float = 0.5
    loss_agg_mode: str = "token-mean"
    optim: OptimConfig = field(default_factory=lambda: OptimConfig(lr=1e-3))
    fsdp_config: EngineConfig = field(default_factory=EngineConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    model: ModelConfig = field(default_factory=ModelConfig)


@dataclass
class SamplingConfig:
    top_k: int = -1
    top_p: float = 1.0
    temperature: float = 0.0
    n: int = 1
    do_sample: bool = False


@dataclass
class RolloutConfig:
    name: str = "naive"
    mode: str = "sync"
    n: int = 8
    temperature: float = 1.0
    top_k: int = -1
    top_p: float = 1.0
    prompt_length: int = 32
    response_length: int = 16
    gpu_memory_utilization: float = 0.5
    tensor_model_parallel_size: int = 1
    pipeline_model_parallel_size: int = 1
    data_parallel_size: int = 1
    expert_parallel_size: int = 1
    free_cache_engine: bool = True
    do_sample: bool = True
    enable_prefix_caching: bool = True
    max_num_batched_tokens: int = 8192
    max_num_seqs: int = 256
    calculate_log_probs: bool = True
    ignore_eos: bool = False
    allowed_chars: str | None = None
    val_kwargs: SamplingConfig = field(default_factory=SamplingConfig)
    multi_turn: dict[str, Any] = field(default_factory=dict)
    checkpoint_engine: dict[str, Any] = field(default_factory=lambda: {"backend": "naive"})
    disaggregation: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "prefill_replicas": 1,
            "decode_replicas": 1,
            "transfer_backend": "nixl",
            "kv_bytes_per_element": 2,
        }
    )


@dataclass
class DataConfig:
    dataset: str = "bandit"
    train_files: str | None = None
    val_files: str | None = None
    prompt_key: str = "prompt"
    max_prompt_length: int = 96
    max_response_length: int = 16
    train_batch_size: int = 16
    val_batch_size: int | None = 8
    gen_batch_size: int | None = None
    train_size: int = 64
    val_size: int = 16
    dataloader_num_workers: int = 0
    shuffle: bool = True
    seed: int = 0
    filter_overlong_prompts: bool = True
    return_raw_chat: bool = True


@dataclass
class FilterGroupsConfig:
    enable: bool = False
    metric: str = "reward"
    max_num_gen_batches: int = 0
    max_inflight_gen_batches: int = 1


@dataclass
class KLControlConfig:
    type: str = "fixed"
    kl_coef: float = 0.001
    horizon: int = 10000
    target_kl: float = 0.01


@dataclass
class AlgorithmConfig:
    adv_estimator: str = "grpo"
    gamma: float = 1.0
    lam: float = 1.0
    norm_adv_by_std_in_grpo: bool = True
    use_kl_in_reward: bool = False
    kl_penalty: str = "kl"
    kl_ctrl: KLControlConfig = field(default_factory=KLControlConfig)
    filter_groups: FilterGroupsConfig = field(default_factory=FilterGroupsConfig)
    rollout_correction: dict[str, Any] = field(
        default_factory=lambda: {
            "enable": False,
            "bypass_mode": True,
            "truncate_ratio": 10.0,
            "normalize": False,
        }
    )
    overlong_penalty_factor: float = 0.0
    max_response_length: int = 16


@dataclass
class RewardConfig:
    reward_manager: str = "naive"
    reward_fn: str = "math"
    custom_reward_function: dict[str, Any] = field(default_factory=dict)
    reward_model: dict[str, Any] = field(default_factory=lambda: {"enable": False})
    # MiniVerl extension: process reward model serving.  Real verl has no
    # ``reward.prm`` key; PRM training is a recipe built on top of the reward
    # loop.  The key exists here so one PPO config can select a verifier, a
    # scalar RM, or a PRM without touching trainer code.
    prm: dict[str, Any] = field(default_factory=lambda: {"enable": False})
    overlong_penalty_factor: float = 0.0


@dataclass
class TrainerConfig:
    total_epochs: int = 1
    total_training_steps: int | None = 60
    n_gpus_per_node: int = 1
    nnodes: int = 1
    project_name: str = "mini_verl"
    experiment_name: str = "default"
    logger: list[str] = field(default_factory=lambda: ["console"])
    log_dir: str = "outputs"
    save_freq: int = -1
    test_freq: int = -1
    val_before_train: bool = True
    val_only: bool = False
    critic_warmup: int = 0
    balance_batch: bool = True
    resume_mode: str = "auto"
    resume_from_path: str | None = None
    max_ckpt_to_keep: int = 2
    use_v1: bool = True
    trainer_mode: str = "sync"
    parameter_sync_step: int = 1
    num_warmup_batches: int = 1
    max_off_policy_threshold: int = 8
    max_off_policy_strategy: str = "drop"
    seed: int = 1


@dataclass
class ActorRolloutRefConfig:
    hybrid_engine: bool = True
    actor: ActorConfig = field(default_factory=ActorConfig)
    ref: RefConfig = field(default_factory=RefConfig)
    rollout: RolloutConfig = field(default_factory=RolloutConfig)
    model: ModelConfig = field(default_factory=ModelConfig)


@dataclass
class PPOConfig:
    actor_rollout_ref: ActorRolloutRefConfig = field(default_factory=ActorRolloutRefConfig)
    data: DataConfig = field(default_factory=DataConfig)
    critic: CriticConfig = field(default_factory=CriticConfig)
    algorithm: AlgorithmConfig = field(default_factory=AlgorithmConfig)
    reward: RewardConfig = field(default_factory=RewardConfig)
    trainer: TrainerConfig = field(default_factory=TrainerConfig)
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_yaml(self, path: str | Path) -> None:
        Path(path).write_text(
            yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    def save(self, path: str | Path) -> None:
        self.to_yaml(path)

    def copy(self) -> "PPOConfig":
        return copy.deepcopy(self)

    def resolve_path(self, dotted: str) -> Any:
        node: Any = self
        for part in dotted.split("."):
            if dataclasses.is_dataclass(node):
                if not hasattr(node, part):
                    raise KeyError(f"unknown config key: {dotted}")
                node = getattr(node, part)
            elif isinstance(node, dict):
                node = node[part]
            else:
                raise KeyError(f"cannot descend into {type(node).__name__} at {part!r}")
        return node

    def set_path(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node: Any = self
        for part in parts[:-1]:
            if dataclasses.is_dataclass(node):
                if not hasattr(node, part):
                    raise KeyError(f"unknown config key: {dotted}")
                node = getattr(node, part)
            elif isinstance(node, dict):
                node = node.setdefault(part, {})
            else:
                raise KeyError(f"cannot descend into {type(node).__name__} at {part!r}")
        leaf = parts[-1]
        if dataclasses.is_dataclass(node):
            if not hasattr(node, leaf):
                raise KeyError(f"unknown config key: {dotted}")
            current = getattr(node, leaf)
            setattr(node, leaf, _coerce(value, current))
        elif isinstance(node, dict):
            node[leaf] = value
        else:
            raise KeyError(dotted)

    def apply_overrides(self, overrides: list[str] | tuple[str, ...]) -> "PPOConfig":
        for raw in overrides:
            if not raw:
                continue
            text = raw
            append = text.startswith("+")
            if append:
                text = text[1:]
            if "=" not in text:
                raise ValueError(f"override must look like key=value, got {raw!r}")
            key, value = text.split("=", 1)
            value = _parse_scalar(value)
            if append:
                # Append semantics are only used for free-form dictionaries.
                parent, leaf = key.rsplit(".", 1) if "." in key else ("", key)
                target = self.resolve_path(parent) if parent else self.extra
                if isinstance(target, dict):
                    target[leaf] = value
                else:
                    raise KeyError(f"cannot append to non-dict key {parent!r}")
            else:
                self.set_path(key, value)
        self.validate()
        return self

    def validate(self) -> None:
        errors: list[str] = []
        if self.data.train_batch_size <= 0:
            errors.append("data.train_batch_size must be positive")
        if self.actor_rollout_ref.rollout.n < 1:
            errors.append("rollout.n must be >= 1")
        if self.actor_rollout_ref.actor.ppo_mini_batch_size <= 0:
            errors.append("actor.ppo_mini_batch_size must be positive")
        total_sequences = self.data.train_batch_size * self.actor_rollout_ref.rollout.n
        if self.actor_rollout_ref.actor.ppo_mini_batch_size > total_sequences:
            errors.append("actor.ppo_mini_batch_size cannot exceed train_batch_size * rollout.n")
        if total_sequences % self.actor_rollout_ref.actor.ppo_mini_batch_size:
            errors.append("train_batch_size * rollout.n must be divisible by ppo_mini_batch_size")
        if self.actor_rollout_ref.rollout.tensor_model_parallel_size > self.trainer.n_gpus_per_node * self.trainer.nnodes:
            errors.append("rollout.tensor_model_parallel_size exceeds the available GPUs")
        if self.actor_rollout_ref.actor.loss_agg_mode not in {
            "token-mean",
            "token-sum",
            "seq-mean-token-sum",
            "seq-mean-token-sum-norm",
            "seq-mean-token-mean",
        }:
            errors.append(f"unknown loss_agg_mode {self.actor_rollout_ref.actor.loss_agg_mode!r}")
        if self.algorithm.adv_estimator not in {
            "gae",
            "grpo",
            "rloo",
            "reinforce_plus_plus",
            "remax",
            "gspo",
            "drgrpo",
            "dapo",
        }:
            # grpo_passk exists in real verl but needs a pass@k group reward
            # that MiniVerl's tiny verifier does not model; reject it early
            # instead of failing inside compute_advantage.
            errors.append(f"unknown advantage estimator {self.algorithm.adv_estimator!r}")
        if self.actor_rollout_ref.actor.policy_loss.loss_mode not in {"vanilla", "gspo", "cispo", "dapo"}:
            # DPPO/DRO/SAPO etc. are documented in the tutorial but not
            # implemented in MiniVerl; fail early instead of silently using
            # vanilla.
            errors.append(
                f"unknown policy_loss.loss_mode {self.actor_rollout_ref.actor.policy_loss.loss_mode!r}"
            )
        if self.trainer.trainer_mode not in {"sync", "colocate_async", "separate_async"}:
            errors.append(f"unknown trainer_mode {self.trainer.trainer_mode!r}")
        if self.reward.reward_manager not in {"naive", "dapo"}:
            errors.append(f"unknown reward.reward_manager {self.reward.reward_manager!r}")
        if self.actor_rollout_ref.model.arch not in {"linear", "transformer"}:
            errors.append(f"unknown model.arch {self.actor_rollout_ref.model.arch!r}")
        if self.algorithm.kl_penalty not in {"kl", "abs", "mse", "low_var_kl"}:
            errors.append(f"unknown algorithm.kl_penalty {self.algorithm.kl_penalty!r}")
        if self.trainer.resume_mode not in {"auto", "disable"}:
            errors.append(f"unknown trainer.resume_mode {self.trainer.resume_mode!r}")
        if self.algorithm.use_kl_in_reward and self.algorithm.kl_ctrl.type not in {"fixed", "adaptive"}:
            errors.append("kl_ctrl.type must be 'fixed' or 'adaptive'")
        if self.actor_rollout_ref.actor.use_kl_loss and self.actor_rollout_ref.actor.kl_loss_type not in {
            "kl",
            "abs",
            "mse",
            "low_var_kl",
        }:
            # Real verl also supports vocabulary-level "full" KL; MiniVerl only
            # stores scalar ref log-probs, so reject it rather than crash later.
            errors.append(f"unknown kl_loss_type {self.actor_rollout_ref.actor.kl_loss_type!r}")
        if errors:
            raise ValueError("invalid config:\n  - " + "\n  - ".join(errors))


# ----------------------------------------------------------------------
# construction helpers
# ----------------------------------------------------------------------


def _coerce(value: Any, current: Any) -> Any:
    if value is None:
        return None
    if current is None:
        return value
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.lower() in {"true", "1", "yes", "on"}
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, tuple):
        return tuple(value) if isinstance(value, (list, tuple)) else current
    return value


def _parse_scalar(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if (text.startswith("[") and text.endswith("]")) or (text.startswith("{") and text.endswith("}")):
        try:
            return json.loads(text.replace("'", '"'))
        except json.JSONDecodeError:
            pass
    return text


def _construct(cls, data: Mapping[str, Any]):
    if not dataclasses.is_dataclass(cls):
        return data
    kwargs: dict[str, Any] = {}
    hints = {field.name: field for field in dataclasses.fields(cls)}
    unknown: list[str] = []
    for key, value in data.items():
        if key not in hints:
            unknown.append(str(key))
            continue
        annotation = hints[key].type
        current = getattr(cls(), key) if key not in kwargs else kwargs[key]
        if dataclasses.is_dataclass(type(current)) and isinstance(value, Mapping):
            kwargs[key] = _construct(type(current), value)
        else:
            kwargs[key] = _coerce(value, current)
    if unknown:
        warnings.warn(
            f"{cls.__name__}: ignoring unknown keys {sorted(unknown)}; "
            "check for typos (e.g. loss_agg_modee)",
            stacklevel=2,
        )
    return cls(**kwargs)


def from_dict(data: Mapping[str, Any]) -> PPOConfig:
    config = PPOConfig()
    for key, value in data.items():
        if not hasattr(config, key):
            config.extra[key] = value
            continue
        if key == "extra":
            config.extra.update(value)
            continue
        current = getattr(config, key)
        if dataclasses.is_dataclass(current) and isinstance(value, Mapping):
            setattr(config, key, _construct(type(current), value))
        else:
            setattr(config, key, value)
    config.validate()
    return config


def load_config(path: str | Path | None = None, overrides: list[str] | tuple[str, ...] = ()) -> PPOConfig:
    overrides = list(overrides)
    # The CLI has two optional positionals: ``config`` and ``overrides``.
    # argparse gives the first positional to ``config``, so a bare
    # ``key=value`` invocation would otherwise be treated as a file path.
    if path is not None and not Path(path).exists() and "=" in str(path):
        overrides.insert(0, str(path))
        path = None
    if path is None:
        config = PPOConfig()
    else:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        config = from_dict(payload)
    config.apply_overrides(overrides)
    return config
