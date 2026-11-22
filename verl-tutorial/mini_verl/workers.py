"""Actor/ref/critic workers.

These classes mirror ``verl.workers.engine_workers.ActorRolloutRefWorker`` and
``TrainingWorker``: each RPC is marked with a dispatch mode, and data-parallel
training RPCs leave gradients pending for the worker group to all-reduce.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .config import PPOConfig
from .data import CharTokenizer
from .engine import EngineRegistry
from .losses import actor_loss_fn, critic_loss_fn, logprob_metric_fn, value_metric_fn
from .nn import build_policy, build_value
from .protocol import DataProto
from .single_controller.decorator import Dispatch, register
from .single_controller.worker import Worker

__all__ = ["ActorRolloutRefWorker", "CriticWorker", "TrainingWorker"]


def _model_config(config: PPOConfig, role: str):
    return config.critic.model if role == "critic" else config.actor_rollout_ref.model


def _calibrate_action_head(model, tokenizer: CharTokenizer, chars: str) -> None:
    """Make the initial bandit policy uniform over the allowed actions.

    A randomly initialized LM puts wildly different logits on ``x/y/z/w``.  One
    action then dominates every group, all GRPO advantages become zero, and the
    teaching run looks like "RL does not learn".  Zeroing the action columns of
    the LM head is a task-specific initialization, not part of the algorithm.
    """
    head = getattr(model, "lm_head", None)
    if head is None:
        return
    # With LoRA the head is a LoRALinear; calibrate the frozen base weight.
    base_head = getattr(head, "base", head)
    for char in chars:
        token_id = tokenizer.stoi.get(char)
        if token_id is not None:
            base_head.weight.data[:, token_id] = 0.0


def _max_seq_len(config: PPOConfig) -> int:
    return max(
        config.actor_rollout_ref.model.max_seq_len,
        config.data.max_prompt_length + config.data.max_response_length,
        config.actor_rollout_ref.rollout.prompt_length + config.actor_rollout_ref.rollout.response_length,
    )


def _allowed_ids(config: PPOConfig, tokenizer: CharTokenizer) -> np.ndarray | None:
    chars = config.actor_rollout_ref.rollout.allowed_chars
    if not chars:
        return None
    ids = [tokenizer.stoi[char] for char in chars if char in tokenizer.stoi]
    ids.append(tokenizer.eos_token_id)
    return np.asarray(sorted(set(ids)), dtype=np.int64)


def _to_batch(data: DataProto) -> dict[str, Any]:
    batch = {key: value for key, value in data.batch.items()}
    for key, value in data.meta_info.items():
        if isinstance(value, (int, float, bool)):
            batch[f"_{key}" if not key.startswith("_") else key] = value
    return batch


def _merge_metrics(total: dict[str, float], weights: dict[str, float], metrics: dict[str, float], count: int) -> None:
    for key, value in metrics.items():
        total[key] = total.get(key, 0.0) + float(value) * count
        weights[key] = weights.get(key, 0.0) + count


def _average(total: dict[str, float], weights: dict[str, float]) -> dict[str, float]:
    return {key: value / max(weights[key], 1e-8) for key, value in total.items()}


class ActorRolloutRefWorker(Worker):
    def __init__(
        self,
        rank: int = 0,
        world_size: int = 1,
        local_rank: int = 0,
        group: Any = None,
        config: PPOConfig | None = None,
        tokenizer: CharTokenizer | None = None,
        role: str = "actor_rollout_ref",
        seed: int = 0,
        **kwargs,
    ):
        super().__init__(rank, world_size, local_rank, group, **kwargs)
        if config is None:
            raise ValueError("ActorRolloutRefWorker needs a config")
        self.config = config
        self.role = role
        self.tokenizer = tokenizer or CharTokenizer()
        model_config = _model_config(config, "actor")
        self.actor = build_policy(
            model_config,
            vocab_size=self.tokenizer.vocab_size,
            max_seq_len=_max_seq_len(config),
            seed=seed,
        )
        if config.data.dataset == "bandit" and config.actor_rollout_ref.rollout.allowed_chars:
            _calibrate_action_head(self.actor, self.tokenizer, config.actor_rollout_ref.rollout.allowed_chars)
        self.engine = EngineRegistry.new(
            config.actor_rollout_ref.actor.strategy,
            self.actor,
            config.actor_rollout_ref.actor.optim,
            config.actor_rollout_ref.actor.engine,
        )
        self.ref_engine = None
        self.ref_model = None
        if "ref" in role or config.actor_rollout_ref.actor.use_kl_loss:
            self.ref_model = build_policy(
                model_config,
                vocab_size=self.tokenizer.vocab_size,
                max_seq_len=_max_seq_len(config),
                seed=seed,
            )
            self.ref_model.load_state_dict(self.actor.state_dict())
            for parameter in self.ref_model.parameters():
                parameter.requires_grad = False
            self.ref_model.eval()
            self.ref_engine = EngineRegistry.new(
                config.actor_rollout_ref.ref.strategy,
                self.ref_model,
                config.actor_rollout_ref.actor.optim,
                config.actor_rollout_ref.ref.engine,
            )
        self._post_metrics: dict[str, float] = {}

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        return {"rank": self.rank, "params": self.actor.num_parameters()}

    # ------------------------------------------------------------------
    # inference RPCs
    # ------------------------------------------------------------------
    def _infer_logprob(self, data: DataProto, engine) -> DataProto:
        batch = _to_batch(data)
        allowed = _allowed_ids(self.config, self.tokenizer)
        if allowed is not None:
            batch["allowed_token_ids"] = allowed
        result = engine.infer_batch(batch, logprob_metric_fn)
        tensor = result[0]
        values = np.asarray(tensor.data, dtype=np.float32)
        output = DataProto(
            batch=dict(data.batch, old_log_probs=values),
            non_tensor_batch=dict(data.non_tensor_batch),
            meta_info=dict(data.meta_info),
        )
        return output

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_log_prob(self, data: DataProto) -> DataProto:
        return self._infer_logprob(data, self.engine)

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_ref_log_prob(self, data: DataProto) -> DataProto:
        if self.ref_engine is None:
            raise RuntimeError("reference model is not enabled")
        batch = _to_batch(data)
        allowed = _allowed_ids(self.config, self.tokenizer)
        if allowed is not None:
            batch["allowed_token_ids"] = allowed
        result = self.ref_engine.infer_batch(batch, logprob_metric_fn)
        values = np.asarray(result[0].data, dtype=np.float32)
        return DataProto(
            batch=dict(data.batch, ref_log_prob=values),
            non_tensor_batch=dict(data.non_tensor_batch),
            meta_info=dict(data.meta_info),
        )

    # ------------------------------------------------------------------
    # training RPCs
    # ------------------------------------------------------------------
    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO, sync_gradients=True)
    def update_actor(self, data: DataProto) -> dict[str, float]:
        actor_cfg = self.config.actor_rollout_ref.actor
        batch_base = _to_batch(data)
        allowed = _allowed_ids(self.config, self.tokenizer)
        dp_size = self.world_size
        global_tokens = float(data.meta_info.get("global_num_tokens", data.batch["response_mask"].sum()))
        global_batch_size = float(data.meta_info.get("global_batch_size", len(data)))
        micro_size = int(actor_cfg.ppo_micro_batch_size or len(data))
        total: dict[str, float] = {}
        weights: dict[str, float] = {}
        if True:
            order = np.arange(len(data))
            if actor_cfg.shuffle:
                rng = np.random.default_rng(self.config.trainer.seed + self.rank)
                rng.shuffle(order)
            for start in range(0, len(data), micro_size):
                indices = order[start : start + micro_size]
                micro = data.select_idxs(indices)
                batch = _to_batch(micro)
                if allowed is not None:
                    batch["allowed_token_ids"] = allowed
                batch.update(
                    {
                        "use_kl_loss": actor_cfg.use_kl_loss,
                        "kl_loss_type": actor_cfg.kl_loss_type,
                        "kl_loss_coef": actor_cfg.kl_loss_coef,
                        "entropy_coeff": actor_cfg.entropy_coeff,
                        "calculate_entropy": actor_cfg.calculate_entropy,
                        "loss_agg_mode": actor_cfg.loss_agg_mode,
                        "clip_ratio": actor_cfg.clip_ratio,
                        "clip_ratio_low": actor_cfg.clip_ratio_low,
                        "clip_ratio_high": actor_cfg.clip_ratio_high,
                        "clip_ratio_c": actor_cfg.clip_ratio_c,
                        "use_gspo": (
                            self.config.actor_rollout_ref.actor.policy_loss.loss_mode == "gspo"
                            or self.config.algorithm.adv_estimator == "gspo"
                        ),
                        "use_cispo": (
                            self.config.actor_rollout_ref.actor.policy_loss.loss_mode == "cispo"
                        ),
                        "_dp_size": dp_size,
                        "_global_num_tokens": max(global_tokens, 1.0),
                        "_global_batch_size": max(global_batch_size, 1.0),
                    }
                )
                first = start == 0
                loss, metrics = self.engine.forward_backward_batch(
                    batch,
                    actor_loss_fn,
                    forward_only=False,
                    zero_grad=first,
                )
                metrics = dict(metrics)
                metrics["actor/num_tokens"] = float(micro.batch["response_mask"].sum())
                _merge_metrics(total, weights, metrics, len(micro))
        return _average(total, weights)

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO, sync_gradients=True)
    def sft_train_step(self, data: DataProto) -> dict[str, float]:
        from .losses import sft_loss_fn

        micro_size = int(self.config.actor_rollout_ref.actor.ppo_micro_batch_size or len(data))
        total: dict[str, float] = {}
        weights: dict[str, float] = {}
        global_tokens = float(data.meta_info.get("global_num_tokens", data.batch["response_mask"].sum()))
        global_batch_size = float(data.meta_info.get("global_batch_size", len(data)))
        for start in range(0, len(data), micro_size):
            micro = data[start : start + micro_size]
            batch = _to_batch(micro)
            batch.update(
                {
                    "_dp_size": self.world_size,
                    "_global_num_tokens": max(global_tokens, 1.0),
                    "_global_batch_size": max(global_batch_size, 1.0),
                }
            )
            _, metrics = self.engine.forward_backward_batch(
                batch, sft_loss_fn, forward_only=False, zero_grad=(start == 0)
            )
            _merge_metrics(total, weights, dict(metrics), len(micro))
        return _average(total, weights)

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO, sync_gradients=True)
    def dpo_train_step(self, data: DataProto) -> dict[str, float]:
        from .losses import dpo_loss_fn

        def loss_fn(model, batch):
            return dpo_loss_fn(model, batch, ref_model=self.ref_model, beta=self.config.extra.get("dpo_beta", 0.1))

        batch = _to_batch(data)
        batch["_dp_size"] = float(self.world_size)
        _, metrics = self.engine.forward_backward_batch(
            batch, loss_fn, forward_only=False, zero_grad=True
        )
        return dict(metrics)

    def finalize_update(self) -> None:
        self._post_metrics = self.engine.finalize_update()

    def post_update_metrics(self) -> dict[str, float]:
        metrics = dict(self._post_metrics)
        self._post_metrics = {}
        return metrics

    # ------------------------------------------------------------------
    # state / checkpoint
    # ------------------------------------------------------------------
    @register(dispatch_mode=Dispatch.RANK_ZERO)
    def get_actor_state_dict(self) -> dict[str, np.ndarray]:
        return self.actor.state_dict()

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def load_actor_state_dict(self, state: dict[str, np.ndarray]) -> None:
        # Only the actor moves.  The reference model is the *initial* policy
        # used by the KL term; copying the resumed actor into it would make KL
        # collapse to zero after a resume.
        self.actor.load_state_dict(state)

    @register(dispatch_mode=Dispatch.RANK_ZERO)
    def get_optimizer_state_dict(self) -> dict[str, Any]:
        if self.engine.optimizer is None:
            return {"step_count": 0, "state": {}}
        return self.engine.optimizer.state_dict()

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def load_optimizer_state_dict(self, state: dict[str, Any]) -> None:
        if self.engine.optimizer is not None and state:
            self.engine.optimizer.load_state_dict(state)

    @register(dispatch_mode=Dispatch.RANK_ZERO)
    def save_checkpoint(self, path: str, step: int) -> str:
        self.engine.save_checkpoint(path, prefix="actor")
        if self.ref_engine is not None:
            self.ref_engine.save_checkpoint(path, prefix="ref")
        return path

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def load_checkpoint(self, path: str) -> None:
        self.engine.load_checkpoint(path, prefix="actor")
        if self.ref_engine is not None:
            self.ref_engine.load_checkpoint(path, prefix="ref")

    @register(dispatch_mode=Dispatch.RANK_ZERO)
    def memory_report(self) -> dict[str, float]:
        report = self.engine.memory_bytes()
        return {key: float(value) for key, value in report.items()}


class CriticWorker(Worker):
    def __init__(
        self,
        rank: int = 0,
        world_size: int = 1,
        local_rank: int = 0,
        group: Any = None,
        config: PPOConfig | None = None,
        tokenizer: CharTokenizer | None = None,
        seed: int = 0,
        **kwargs,
    ):
        super().__init__(rank, world_size, local_rank, group, **kwargs)
        if config is None:
            raise ValueError("CriticWorker needs a config")
        self.config = config
        self.tokenizer = tokenizer or CharTokenizer()
        self.model = build_value(
            config.critic.model,
            vocab_size=self.tokenizer.vocab_size,
            max_seq_len=_max_seq_len(config),
            seed=seed + 1,
        )
        self.engine = EngineRegistry.new(
            config.critic.strategy,
            self.model,
            config.critic.optim,
            config.critic.engine,
        )
        self._post_metrics: dict[str, float] = {}

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        return {"rank": self.rank, "params": self.model.num_parameters()}

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_values(self, data: DataProto) -> DataProto:
        result = self.engine.infer_batch(_to_batch(data), value_metric_fn)
        values = np.asarray(result[0].data, dtype=np.float32)
        return DataProto(
            batch=dict(data.batch, values=values),
            non_tensor_batch=dict(data.non_tensor_batch),
            meta_info=dict(data.meta_info),
        )

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO, sync_gradients=True)
    def update_critic(self, data: DataProto) -> dict[str, float]:
        cfg = self.config.critic
        micro_size = int(cfg.ppo_micro_batch_size or len(data))
        global_tokens = float(data.meta_info.get("global_num_tokens", data.batch["response_mask"].sum()))
        global_batch_size = float(data.meta_info.get("global_batch_size", len(data)))
        total: dict[str, float] = {}
        weights: dict[str, float] = {}
        if True:
            for start in range(0, len(data), micro_size):
                micro = data[start : start + micro_size]
                batch = _to_batch(micro)
                batch.update(
                    {
                        "cliprange_value": cfg.cliprange_value,
                        "loss_agg_mode": cfg.loss_agg_mode,
                        "_dp_size": self.world_size,
                        "_global_num_tokens": max(global_tokens, 1.0),
                        "_global_batch_size": max(global_batch_size, 1.0),
                    }
                )
                first = start == 0
                _, metrics = self.engine.forward_backward_batch(
                    batch, critic_loss_fn, forward_only=False, zero_grad=first
                )
                _merge_metrics(total, weights, dict(metrics), len(micro))
        return _average(total, weights)

    def finalize_update(self) -> None:
        self._post_metrics = self.engine.finalize_update()

    def post_update_metrics(self) -> dict[str, float]:
        metrics = dict(self._post_metrics)
        self._post_metrics = {}
        return metrics

    @register(dispatch_mode=Dispatch.RANK_ZERO)
    def get_state_dict(self) -> dict[str, np.ndarray]:
        return self.model.state_dict()

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def load_state_dict(self, state: dict[str, np.ndarray]) -> None:
        self.model.load_state_dict(state)

    @register(dispatch_mode=Dispatch.RANK_ZERO)
    def get_optimizer_state_dict(self) -> dict[str, Any]:
        if self.engine.optimizer is None:
            return {"step_count": 0, "state": {}}
        return self.engine.optimizer.state_dict()

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def load_optimizer_state_dict(self, state: dict[str, Any]) -> None:
        if self.engine.optimizer is not None and state:
            self.engine.optimizer.load_state_dict(state)

    @register(dispatch_mode=Dispatch.RANK_ZERO)
    def save_checkpoint(self, path: str, step: int) -> str:
        self.engine.save_checkpoint(path, prefix="critic")
        return path

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def load_checkpoint(self, path: str) -> None:
        self.engine.load_checkpoint(path, prefix="critic")

    @register(dispatch_mode=Dispatch.RANK_ZERO)
    def memory_report(self) -> dict[str, float]:
        return {key: float(value) for key, value in self.engine.memory_bytes().items()}


class TrainingWorker(ActorRolloutRefWorker):
    """Alias used in the docs to make the verl naming explicit."""
