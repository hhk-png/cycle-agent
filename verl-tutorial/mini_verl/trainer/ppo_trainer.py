"""The MiniVerl PPO/GRPO trainer.

The loop is intentionally close to verl's ``PPOTrainer._step_once``:

    rollout -> reward -> balance -> old/ref log-prob -> values -> advantage
    -> critic update -> actor update -> metrics/checkpoint/validation

Every distributed call goes through ``LocalWorkerGroup`` dispatch, so the
trainer code sees the same RPC-shaped API as the real Ray controller.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np

from ..agent import AgentLoopManager, AgentLoop
from ..algo.core_algos import compute_advantage, filter_groups_for_dapo, get_kl_controller, kl_penalty
from ..async_policy import ReplayBuffer, compute_rollout_correction
from ..checkpoint import CheckpointManager
from ..config import PPOConfig
from ..data import CharTokenizer, build_dataset, make_batches
from ..metrics import (
    MetricsAggregator,
    MetricsLogger,
    compute_data_metrics,
    compute_throughput_metrics,
    compute_timing_metrics,
    compute_validation_metrics,
)
from ..protocol import DataProto
from ..rewards import DAPORewardManager, RewardManager
from ..rollout import SamplingParams, build_rollout
from ..sharding import CheckpointEngineManager
from ..single_controller.worker_group import LocalWorkerGroup, ResourcePool
from ..workers import ActorRolloutRefWorker, CriticWorker

__all__ = ["PPOTrainer"]


class PPOTrainer:
    def __init__(self, config: PPOConfig):
        self.config = config
        self.global_steps = 0
        self.epoch = 0
        self.metrics: MetricsAggregator = MetricsAggregator()
        self._train_cursor = 0
        self._val_cursor = 0
        self._initialized = False
        self._async_buffer: ReplayBuffer | None = None
        self._pending_prompts: DataProto | None = None
        # Actor-update version currently loaded into the rollout replicas.
        # ``checkpoint_engine.version`` counts syncs, which is not the same
        # quantity when parameter_sync_step > 1.
        self._rollout_policy_step = 0

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def init(self) -> "PPOTrainer":
        if self._initialized:
            return self
        config = self.config
        np.random.seed(config.trainer.seed)
        self.tokenizer = CharTokenizer()
        self._ensure_model_capacity()

        self.train_dataset = build_dataset(config.data.dataset, config.data.train_size, config.data.seed)
        self.val_dataset = build_dataset(config.data.dataset, config.data.val_size, config.data.seed + 1)
        self.train_batches = make_batches(
            self.train_dataset,
            config.data.train_batch_size,
            self.tokenizer,
            config.data.max_prompt_length,
            config.data.max_response_length,
            shuffle=config.data.shuffle,
            seed=config.data.seed,
            filter_overlong_prompts=config.data.filter_overlong_prompts,
        )
        if not self.train_batches:
            raise ValueError("dataset smaller than one training batch")
        self.val_batches = make_batches(
            self.val_dataset,
            config.data.val_batch_size or config.data.val_size,
            self.tokenizer,
            config.data.max_prompt_length,
            config.data.max_response_length,
            shuffle=False,
            seed=config.data.seed,
            filter_overlong_prompts=config.data.filter_overlong_prompts,
        )

        world_size = max(1, config.trainer.n_gpus_per_node * config.trainer.nnodes)
        self.world_size = world_size

        # The optimizer's scheduler horizon counts local optimizer updates.
        # Without this wiring cosine/warmup silently use a constant LR because
        # OptimConfig.total_training_steps stays at its -1 default.
        # MiniVerl's parameter_sync_step is a *sync frequency* in global steps,
        # not V1's sub-step split, so the optimizer horizon is just the number
        # of global training steps.
        optimizer_steps = int(config.trainer.total_training_steps or len(self.train_batches))
        config.actor_rollout_ref.actor.optim.total_training_steps = optimizer_steps
        config.critic.optim.total_training_steps = optimizer_steps
        self.use_ref = config.actor_rollout_ref.actor.use_kl_loss or config.algorithm.use_kl_in_reward
        actor_role = "actor_rollout_ref" if self.use_ref else "actor_rollout"
        self.actor_group = LocalWorkerGroup(
            ResourcePool.single_node(world_size, config.trainer.n_gpus_per_node),
            ActorRolloutRefWorker,
            {
                "config": config,
                "tokenizer": self.tokenizer,
                "role": actor_role,
                "seed": config.trainer.seed,
            },
            name=actor_role,
        )
        self.actor_group.init_model()
        # ReMax uses a greedy rollout baseline, not a learned critic.  Only GAE
        # (or an explicit critic.enable=true) needs the critic worker.
        self.use_critic = config.algorithm.adv_estimator == "gae" or config.critic.enable is True
        self.critic_group = None
        if self.use_critic:
            self.critic_group = LocalWorkerGroup(
                ResourcePool.single_node(world_size, config.trainer.n_gpus_per_node),
                CriticWorker,
                {"config": config, "tokenizer": self.tokenizer, "seed": config.trainer.seed},
                name="critic",
            )
            self.critic_group.init_model()

        self.rollout = build_rollout(
            config.actor_rollout_ref.rollout,
            self.tokenizer,
            seed=config.trainer.seed,
            model_config=config.actor_rollout_ref.model,
        )
        backend = config.actor_rollout_ref.rollout.checkpoint_engine.get("backend", "naive")
        self.checkpoint_engine = CheckpointEngineManager(backend=backend, replicas=1)
        self._sync_weights(initial=True)

        self.reward_manager: RewardManager
        overlong_penalty = (
            config.reward.overlong_penalty_factor
            if config.reward.overlong_penalty_factor
            else config.algorithm.overlong_penalty_factor
        )
        prm_config = dict(config.reward.prm or {})
        reward_model_config = dict(config.reward.reward_model or {})
        if prm_config.get("enable") and reward_model_config.get("enable"):
            raise ValueError(
                "enable only one of reward.prm and reward.reward_model; "
                "two reward managers would silently shadow each other"
            )
        if prm_config.get("enable"):
            # A PRM is a *serving* switch, like ``reward_model.enable``.
            # Training happens in ``trainer/main_prm.py``.  The manager scores
            # every step prefix, aggregates it, and writes the same
            # ``rm_scores``/``token_level_rewards`` fields as a scalar RM, so
            # DAPO filtering, GAE/GRPO and validation need no special case.
            model_path = prm_config.get("model_path") or prm_config.get("path")
            if not model_path:
                raise ValueError(
                    "reward.prm.enable=true requires reward.prm.model_path"
                )
            from ..prm import ProcessRewardManager

            self.reward_manager = ProcessRewardManager(
                model_path,
                tokenizer=self.tokenizer,
                batch_size=int(prm_config.get("batch_size", 64)),
                # ``None`` means "take it from the checkpoint metadata"; an
                # explicit config value still overrides.
                aggregation=prm_config.get("aggregation"),
                step_tokens=prm_config.get("step_tokens"),
                normalize=str(prm_config.get("normalize", "none")),
                overlong_penalty_factor=overlong_penalty,
                outcome_reward_fn=prm_config.get("outcome_reward_fn"),
                outcome_weight=float(prm_config.get("outcome_weight", 0.0)),
                prm_weight=float(prm_config.get("prm_weight", 1.0)),
            )
        elif reward_model_config.get("enable"):
            # ``reward_model.enable`` is an *inference* switch in real verl and
            # in MiniVerl: it serves a trained scalar RM through the reward
            # manager instead of calling a rule function.  Training happens in
            # ``trainer/main_rm.py``.
            model_path = reward_model_config.get("model_path") or reward_model_config.get("path")
            if not model_path:
                raise ValueError(
                    "reward.reward_model.enable=true requires reward.reward_model.model_path"
                )
            from ..reward_model import ModelRewardManager

            self.reward_manager = ModelRewardManager(
                model_path,
                tokenizer=self.tokenizer,
                batch_size=int(reward_model_config.get("batch_size", 64)),
                normalize=str(reward_model_config.get("normalize", "none")),
                overlong_penalty_factor=overlong_penalty,
            )
        elif config.reward.reward_manager == "dapo":
            self.reward_manager = DAPORewardManager(
                config.reward.reward_fn, overlong_penalty_factor=overlong_penalty
            )
        else:
            self.reward_manager = RewardManager(
                config.reward.reward_fn, overlong_penalty_factor=overlong_penalty
            )
        self.kl_controller = (
            get_kl_controller(
                config.algorithm.kl_ctrl.type,
                config.algorithm.kl_ctrl.kl_coef,
                config.algorithm.kl_ctrl.horizon,
                config.algorithm.kl_ctrl.target_kl,
            )
            if config.algorithm.use_kl_in_reward
            else None
        )

        self.logger = MetricsLogger(
            config.trainer.log_dir,
            config.trainer.project_name,
            config.trainer.experiment_name,
            config.trainer.logger,
        )
        self.checkpoint_manager = CheckpointManager(
            Path(config.trainer.log_dir) / "checkpoints" / config.trainer.project_name / config.trainer.experiment_name,
            max_to_keep=config.trainer.max_ckpt_to_keep,
        )
        if config.trainer.resume_mode != "disable":
            self._maybe_resume()
        self._initialized = True

        if config.trainer.trainer_mode in {"colocate_async", "separate_async"}:
            self._async_buffer = ReplayBuffer(
                config.trainer.max_off_policy_threshold, config.trainer.max_off_policy_strategy
            )
            for _ in range(max(1, config.trainer.num_warmup_batches)):
                self._produce_async_batch()
        return self

    def _ensure_model_capacity(self) -> None:
        config = self.config
        required = config.data.max_prompt_length + config.data.max_response_length
        config.actor_rollout_ref.model.max_seq_len = max(config.actor_rollout_ref.model.max_seq_len, required)
        config.critic.model.max_seq_len = max(config.critic.model.max_seq_len, required)
        config.actor_rollout_ref.rollout.prompt_length = config.data.max_prompt_length
        config.actor_rollout_ref.rollout.response_length = config.data.max_response_length
        config.actor_rollout_ref.rollout.n = config.actor_rollout_ref.rollout.n
        config.actor_rollout_ref.actor.rollout_n = config.actor_rollout_ref.rollout.n
        if config.data.dataset == "bandit":
            # Constrained decoding keeps the teaching task learnable on CPU:
            # the policy chooses among four actions instead of ~90 characters.
            if config.actor_rollout_ref.rollout.allowed_chars is None:
                config.actor_rollout_ref.rollout.allowed_chars = "xyzw"
            if config.reward.reward_fn == "math":
                config.reward.reward_fn = "bandit"
            # The bandit task only needs the last prompt token; a linear policy
            # avoids paying Transformer attention cost for a contextual bandit.
            if config.actor_rollout_ref.model.arch == "transformer":
                config.actor_rollout_ref.model.arch = "linear"
                config.actor_rollout_ref.model.hidden_size = min(config.actor_rollout_ref.model.hidden_size, 32)
            config.critic.model.arch = "linear"

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def fit(self) -> dict[str, Any]:
        self.init()
        if self.config.trainer.val_before_train:
            val_metrics = self.validate()
            self.logger.log(val_metrics, self.global_steps)
            if self.config.trainer.val_only:
                return {"val": val_metrics, "steps": self.global_steps}

        total = self.config.trainer.total_training_steps or len(self.train_batches)
        final_metrics: dict[str, float] = {}
        for _ in range(total):
            if self.global_steps >= total:
                break
            step_metrics = self.train_step()
            self.metrics.add(step_metrics)
            self.logger.log(step_metrics, self.global_steps + 1)
            self.global_steps += 1
            final_metrics = step_metrics
            if (
                self.config.trainer.test_freq > 0
                and self.global_steps % self.config.trainer.test_freq == 0
            ):
                self.logger.log(self.validate(), self.global_steps)
            if (
                self.config.trainer.save_freq > 0
                and self.global_steps % self.config.trainer.save_freq == 0
            ):
                self.save_checkpoint()
        # The final actor update must reach the rollout copy: otherwise
        # validate()/evaluate()/ReMax use a policy up to S-1 updates stale when
        # parameter_sync_step > 1.
        self._sync_weights()
        return {"last": final_metrics, "steps": self.global_steps, "history": self.metrics.result()}

    def train_step(self) -> dict[str, float]:
        if not self._initialized:
            self.init()
        timings: dict[str, float] = {}
        step_start = time.perf_counter()
        async_metrics: dict[str, float] = {}

        # 1. acquire prompts and generate
        t0 = time.perf_counter()
        if self._async_buffer is not None:
            try:
                data, async_metrics = self._async_buffer.sample(
                    self.config.data.train_batch_size * self.config.actor_rollout_ref.rollout.n,
                    self.global_steps,
                )
            except IndexError:
                # All buffered samples were dropped as stale (e.g. threshold=0).
                # Produce one fresh batch instead of crashing the step.
                self._produce_async_batch()
                data, async_metrics = self._async_buffer.sample(
                    self.config.data.train_batch_size * self.config.actor_rollout_ref.rollout.n,
                    self.global_steps,
                )
        else:
            prompts = self._next_train_batch()
            data = self._rollout_batch(prompts)
        timings["gen"] = time.perf_counter() - t0

        # 2. reward
        t0 = time.perf_counter()
        data = self.reward_manager.run(data)
        timings["reward"] = time.perf_counter() - t0

        filter_metrics: dict[str, float] = {}
        all_filtered = False
        if self.config.algorithm.filter_groups.enable:
            metric_key = self.config.algorithm.filter_groups.metric
            if metric_key == "reward":
                filter_scores = data.batch["rm_scores"]
            elif metric_key in data.batch:
                filter_scores = data.batch[metric_key]
            elif metric_key in data.non_tensor_batch:
                filter_scores = np.asarray(data.non_tensor_batch[metric_key], dtype=np.float32)
            else:
                raise KeyError(
                    f"algorithm.filter_groups.metric={metric_key!r} is not present in the batch"
                )
            keep, filter_metrics = filter_groups_for_dapo(
                filter_scores, data.non_tensor_batch["uid"], metric_key
            )
            if len(keep) > 0:
                data = data.select_idxs(keep)
            else:
                # Every group has zero variance.  DAPO regenerates in this
                # case; MiniVerl records the event and skips the update instead
                # of taking an AdamW step driven purely by weight decay.
                all_filtered = True
                filter_metrics["dapo/all_filtered"] = 1.0

        # 3. balance sequence lengths across DP ranks
        t0 = time.perf_counter()
        if self.config.trainer.balance_batch:
            data, balance_metrics = self._balance_batch(data)
        else:
            balance_metrics = {}
        timings["balance"] = time.perf_counter() - t0

        data.meta_info["global_num_tokens"] = int(data.batch["response_mask"].sum())
        data.meta_info["global_batch_size"] = len(data)

        # 4. old log-prob
        t0 = time.perf_counter()
        correction = self.config.algorithm.rollout_correction
        correction_enabled = bool(correction.get("enable", False))
        if correction_enabled and "rollout_log_probs" not in data.batch:
            raise ValueError(
                "algorithm.rollout_correction.enable=true requires rollout_log_probs; "
                "check that the rollout engine records behavior log-probs"
            )
        # Correction is a property of the data (behavior policy vs current
        # policy), not of the async mode.  A sync run at temperature != 1 also
        # needs it; gating on _async_buffer made enable=true a silent no-op.
        use_bypass = correction_enabled and correction.get("bypass_mode", True)
        use_is_correction = correction_enabled and not correction.get("bypass_mode", True)
        if use_bypass:
            data.batch["old_log_probs"] = data.batch["rollout_log_probs"] * data.batch["response_mask"]
        else:
            data = self.actor_group.compute_log_prob(data)
        timings["old_log_prob"] = time.perf_counter() - t0

        # 5. reference log-prob
        if self.use_ref:
            t0 = time.perf_counter()
            data = self.actor_group.compute_ref_log_prob(data)
            timings["ref"] = time.perf_counter() - t0

        # 6. rollout correction for async data
        if use_is_correction:
            weights, is_metrics = compute_rollout_correction(
                data,
                data.batch["old_log_probs"],
                float(self.config.algorithm.rollout_correction.get("truncate_ratio", 10.0)),
                bool(self.config.algorithm.rollout_correction.get("normalize", False)),
            )
            data.batch["rollout_is_weights"] = weights
            async_metrics.update(is_metrics)

        # 7. critic values
        if self.use_critic and self.critic_group is not None:
            t0 = time.perf_counter()
            data = self.critic_group.compute_values(data)
            timings["values"] = time.perf_counter() - t0

        # 8. in-reward KL and advantage
        t0 = time.perf_counter()
        data = self._compute_advantage(data)
        timings["adv"] = time.perf_counter() - t0

        # 9. critic update
        critic_metrics: dict[str, float] = {}
        if self.use_critic and self.critic_group is not None and not all_filtered:
            t0 = time.perf_counter()
            critic_metrics = self._update_worker(
                self.critic_group,
                data,
                self.config.critic.ppo_mini_batch_size,
                "update_critic",
                epochs=self.config.critic.ppo_epochs,
            )
            timings["update_critic"] = time.perf_counter() - t0

        # 10. actor update
        actor_metrics: dict[str, float] = {}
        if self.global_steps >= self.config.trainer.critic_warmup and not all_filtered:
            t0 = time.perf_counter()
            actor_metrics = self._update_worker(
                self.actor_group,
                data,
                self.config.actor_rollout_ref.actor.ppo_mini_batch_size,
                "update_actor",
                epochs=self.config.actor_rollout_ref.actor.ppo_epochs,
            )
            timings["update_actor"] = time.perf_counter() - t0

        # 11. async refill: produce the next batch *before* the weight sync so
        # it is stamped with the pre-sync version.  Consuming it after the sync
        # gives a real, nonzero staleness (the whole point of async RL).
        if self._async_buffer is not None:
            self._produce_async_batch()

        # 12. weight sync
        t0 = time.perf_counter()
        if self.global_steps % max(1, self.config.trainer.parameter_sync_step) == 0:
            sync_stats = self._sync_weights()
            self._sync_metrics = sync_stats.as_dict()
        else:
            self._sync_metrics = {}
        timings["update_weights"] = time.perf_counter() - t0

        if self.kl_controller is not None and "ref_log_prob" in data.batch:
            mean_kl = float(
                np.sum((data.batch["old_log_probs"] - data.batch["ref_log_prob"]) * data.batch["response_mask"])
                / max(float(data.batch["response_mask"].sum()), 1.0)
            )
            self.kl_controller.update(mean_kl, 1)

        elapsed = time.perf_counter() - step_start
        metrics: dict[str, float] = {}
        metrics.update(compute_data_metrics(data))
        metrics.update(compute_timing_metrics(timings))
        metrics.update(balance_metrics)
        metrics.update(async_metrics)
        metrics.update(filter_metrics)
        metrics.update(self._sync_metrics)
        metrics.update(actor_metrics)
        metrics.update(critic_metrics)
        if "rm_scores" in data.batch:
            metrics["train/reward_mean"] = float(data.batch["rm_scores"].mean())
        num_tokens = int(data.batch["response_mask"].sum())
        num_params = sum(p.size for p in self.actor_group.workers[0].actor.parameters())
        metrics.update(
            compute_throughput_metrics(
                num_tokens=num_tokens,
                elapsed_seconds=elapsed,
                num_gpus=self.world_size,
                num_parameters=num_params,
                num_samples=len(data),
                training=True,
            )
        )
        metrics["train/step_time_s"] = elapsed
        return metrics

    # ------------------------------------------------------------------
    # generation / data movement
    # ------------------------------------------------------------------
    def _next_train_batch(self) -> DataProto:
        if self._train_cursor >= len(self.train_batches):
            self._train_cursor = 0
            self.epoch += 1
        batch = self.train_batches[self._train_cursor]
        self._train_cursor += 1
        return batch

    def _rollout_batch(self, prompts: DataProto) -> DataProto:
        n = self.config.actor_rollout_ref.rollout.n
        repeated = prompts.repeat(n, interleave=True) if n > 1 else prompts
        if self.rollout.model is None:
            self.rollout.wake_up()
        data = self.rollout.generate_sequences(repeated)
        if self.config.actor_rollout_ref.rollout.free_cache_engine:
            self.rollout.sleep()
        return data

    def _produce_async_batch(self) -> None:
        assert self._async_buffer is not None
        prompts = self._next_train_batch()
        data = self._rollout_batch(prompts)
        data = self.reward_manager.run(data)
        # Stamp with the actor-update counter, not the weight-sync counter:
        # with parameter_sync_step > 1 the actor updates every step but the
        # sync version does not, so the latter would under-report staleness.
        self._async_buffer.add(data, self._rollout_policy_step, self.global_steps)

    def _balance_batch(self, data: DataProto) -> tuple[DataProto, dict[str, float]]:
        lengths = data.batch["response_mask"].sum(axis=-1)
        order = np.argsort(-lengths, kind="stable")
        groups: list[list[int]] = [[] for _ in range(self.world_size)]
        # Longest-processing-time-first with fixed capacities.  DP dispatch pads
        # to a multiple of W by appending duplicates, then splits into equal
        # chunks of ceil(N/W).  The *real* sample count in chunk i is therefore
        # min(chunk, max(0, N - i*chunk)); use that, not divmod(N, W), so the
        # reported imbalance matches what each rank actually receives.
        #
        # This ordering benefits the full-batch RPCs (old/ref log-prob, values).
        # Actor updates shuffle and re-split into mini-batches, so they are not
        # load-balanced by this pass; that is a documented MiniVerl boundary.
        chunk = (len(data) + self.world_size - 1) // self.world_size
        capacities = [
            min(chunk, max(0, len(data) - rank * chunk)) for rank in range(self.world_size)
        ]
        loads = [0.0] * self.world_size
        for index in order:
            candidates = [rank for rank in range(self.world_size) if len(groups[rank]) < capacities[rank]]
            if not candidates:
                # Defensive fallback; total capacity equals N, so this should
                # only happen if the caller passed inconsistent state.
                candidates = list(range(self.world_size))
            rank = min(candidates, key=lambda r: (loads[r], r))
            groups[rank].append(int(index))
            loads[rank] += float(lengths[index])
        permutation = np.asarray([index for group in groups for index in group], dtype=np.int64)
        reordered = data.select_idxs(permutation)
        per_rank = [float(lengths[group].sum()) for group in groups]
        mean = float(np.mean(per_rank)) if per_rank else 0.0
        imbalance = (max(per_rank) - min(per_rank)) / max(mean, 1e-8) if per_rank else 0.0
        return reordered, {"data/seqlen_imbalance": imbalance}

    # ------------------------------------------------------------------
    # advantage / updates
    # ------------------------------------------------------------------
    def _compute_advantage(self, data: DataProto) -> DataProto:
        cfg = self.config.algorithm
        mask = data.batch["response_mask"]
        rewards = data.batch["token_level_rewards"]
        if cfg.use_kl_in_reward and "ref_log_prob" in data.batch:
            kl = kl_penalty(data.batch["old_log_probs"], data.batch["ref_log_prob"], cfg.kl_penalty)
            rewards = rewards - self.kl_controller.value * kl
            data.batch["token_level_rewards"] = rewards
        index = data.non_tensor_batch.get("uid")
        if index is None:
            index = np.arange(len(data))
        values = data.batch.get("values")
        baselines = None
        if cfg.adv_estimator == "remax":
            baselines = self._remax_greedy_baselines(data, index)
        advantages, returns = compute_advantage(
            cfg.adv_estimator,
            rewards,
            mask,
            values=values,
            index=index,
            gamma=cfg.gamma,
            lam=cfg.lam,
            norm_adv_by_std_in_grpo=cfg.norm_adv_by_std_in_grpo,
            reward_baselines=baselines,
            truncated=data.batch.get("truncated"),
        )
        data.batch["advantages"] = advantages.astype(np.float32)
        data.batch["returns"] = returns.astype(np.float32)
        return data

    def _remax_greedy_baselines(self, data: DataProto, index: np.ndarray) -> np.ndarray:
        """One greedy rollout per prompt, scored by the same reward manager.

        ReMax's baseline is not ``max(sampled rewards)``: using the best sample
        would always give that sample zero advantage.  The greedy trajectory is
        generated separately, so a lucky sampled success can still have a
        positive advantage.
        """

        first_positions: dict[Any, int] = {}
        for position, key in enumerate(index):
            first_positions.setdefault(key, position)
        prompt_rows = data.select_idxs(list(first_positions.values()))
        was_asleep = self.rollout.model is None
        if was_asleep:
            self.rollout.wake_up()
        try:
            greedy = self.rollout.generate_sequences(
                prompt_rows,
                SamplingParams(
                    temperature=0.0,
                    do_sample=False,
                    max_tokens=self.config.data.max_response_length,
                ),
            )
            greedy = self.reward_manager.run(greedy)
            greedy_scores = greedy.batch["rm_scores"]
        finally:
            if was_asleep and self.config.actor_rollout_ref.rollout.free_cache_engine:
                self.rollout.sleep()
        mapping = {
            key: float(score) for key, score in zip(first_positions.keys(), greedy_scores)
        }
        return np.asarray([mapping[key] for key in index], dtype=np.float32)

    def _update_worker(
        self,
        group: LocalWorkerGroup,
        data: DataProto,
        mini_size: int,
        method: str,
        epochs: int = 1,
    ) -> dict[str, float]:
        total: dict[str, float] = {}
        weights: dict[str, float] = {}
        rng = np.random.default_rng(self.config.trainer.seed + self.global_steps * 1000)
        for epoch in range(max(1, epochs)):
            order = np.arange(len(data))
            if method == "update_actor" and self.config.actor_rollout_ref.actor.shuffle:
                rng.shuffle(order)
            shuffled = data.select_idxs(order)
            for mini in shuffled.split(int(mini_size)):
                if len(mini) == 0:
                    continue
                # Each optimizer step consumes its own mini-batch; normalize by
                # that mini-batch's global token count, not the whole step.
                mini.meta_info["global_num_tokens"] = int(mini.batch["response_mask"].sum())
                mini.meta_info["global_batch_size"] = len(mini)
                metrics = getattr(group, method)(mini)
                # DP training RPCs return one metric dict per rank; average them.
                if isinstance(metrics, list):
                    merged: dict[str, float] = {}
                    for item in metrics:
                        for key, value in item.items():
                            merged[key] = merged.get(key, 0.0) + float(value) / len(metrics)
                    metrics = merged
                for key, value in metrics.items():
                    total[key] = total.get(key, 0.0) + float(value) * len(mini)
                    weights[key] = weights.get(key, 0.0) + len(mini)
        return {key: value / max(weights[key], 1e-8) for key, value in total.items()}

    def _sync_weights(self, initial: bool = False):
        actor_state = self.actor_group.get_actor_state_dict()
        if self.rollout.model is None and not initial:
            # Keep the rollout asleep: update_weights stores the snapshot and
            # wake_up will materialize it on demand.
            pass
        stats = self.checkpoint_engine.update_weights(actor_state, [self.rollout])
        self._rollout_policy_step = 0 if initial else self.global_steps + 1
        return stats

    # ------------------------------------------------------------------
    # validation / checkpoint
    # ------------------------------------------------------------------
    def validate(self) -> dict[str, float]:
        if not self._initialized:
            self.init()
        # Validation must not run on a rollout copy left behind by a sync
        # interval (parameter_sync_step > 1).
        self._sync_weights()
        was_asleep = self.rollout.model is None
        if was_asleep:
            self.rollout.wake_up()
        scores = []
        for prompts in self.val_batches:
            data = self.rollout.generate_sequences(
                prompts,
                SamplingParams(temperature=0.0, do_sample=False, max_tokens=self.config.data.max_response_length),
            )
            data = self.reward_manager.run(data)
            scores.extend(data.batch["rm_scores"].tolist())
        if was_asleep and self.config.actor_rollout_ref.rollout.free_cache_engine:
            self.rollout.sleep()
        return compute_validation_metrics(np.asarray(scores, dtype=np.float32), n=1, ks=(1,))

    def evaluate(
        self,
        n: int = 1,
        ks: tuple[int, ...] | list[int] = (1,),
        split: str = "val",
        temperature: float | None = None,
        top_p: float = 1.0,
    ) -> dict[str, float]:
        """Standalone pass@k evaluation, analogous to ``verl/trainer/main_eval.py``.

        Unlike :meth:`validate`, this does not reuse the training batch or its
        sampling configuration.  It draws ``n`` independent samples per prompt,
        keeps each prompt's samples contiguous (``interleave=True``), and feeds
        them to the same reward manager before computing the unbiased pass@k
        estimator.

        For a fair evaluation use a *naive* reward manager: training reward
        shaping (DAPO overlong penalty, format bonuses) changes the quantity
        being measured.
        """
        if not self._initialized:
            self.init()
        self._sync_weights()
        ks = tuple(sorted({int(k) for k in ks}))
        if not ks:
            raise ValueError("ks must contain at least one k")
        if n < max(ks):
            raise ValueError(f"n={n} must be >= max(ks)={max(ks)}")
        if split not in {"val", "train"}:
            raise ValueError(f"unknown split {split!r}")
        batches = self.val_batches if split == "val" else self.train_batches

        was_asleep = self.rollout.model is None
        if was_asleep:
            self.rollout.wake_up()
        # Evaluation must not inherit training-time shaping (DAPO overlong
        # penalty, format bonuses, ...).  Rebuild a naive manager around the
        # same reward source instead of trusting config alone.  Model rewards
        # clone the RM with shaping disabled; rule rewards rebuild a naive
        # manager around the same function.
        if hasattr(self.reward_manager, "without_shaping"):
            eval_manager = self.reward_manager.without_shaping()
        else:
            eval_manager = RewardManager(self.reward_manager.reward_fn)
        sampled = n > 1 or (temperature is not None and temperature > 0)
        sampling = SamplingParams(
            temperature=(1.0 if temperature is None else temperature) if sampled else 0.0,
            top_p=top_p,
            do_sample=sampled,
            max_tokens=self.config.data.max_response_length,
        )
        scores: list[float] = []
        for prompts in batches:
            repeated = prompts.repeat(n, interleave=True) if n > 1 else prompts
            data = self.rollout.generate_sequences(repeated, sampling)
            data = eval_manager.run(data)
            scores.extend(np.asarray(data.batch["rm_scores"], dtype=np.float32).tolist())
        if was_asleep and self.config.actor_rollout_ref.rollout.free_cache_engine:
            self.rollout.sleep()
        # Accuracy and pass@k must use the same complete prompt groups; a
        # trailing partial group would bias one metric but not the other.
        usable = (len(scores) // n) * n
        if usable:
            scores = scores[:usable]
        return compute_validation_metrics(np.asarray(scores, dtype=np.float32), n=n, ks=ks)

    def save_checkpoint(self) -> str:
        actor_state = self.actor_group.get_actor_state_dict()
        actor_optimizer = self.actor_group.get_optimizer_state_dict()
        critic_state = self.critic_group.get_state_dict() if self.critic_group is not None else None
        critic_optimizer = (
            self.critic_group.get_optimizer_state_dict() if self.critic_group is not None else None
        )
        directory = self.checkpoint_manager.save(
            self.global_steps,
            actor_state,
            critic_state,
            config=self.config,
            tokenizer=self.tokenizer,
            metrics=self.metrics.result(),
            actor_optimizer=actor_optimizer,
            critic_optimizer=critic_optimizer,
            extra_state={
                "train_cursor": self._train_cursor,
                "epoch": self.epoch,
                "kl_coef": float(self.kl_controller.value) if self.kl_controller is not None else None,
                # The rollout sampler's RNG is part of training state: without
                # it a resumed run draws different tokens and diverges from an
                # uninterrupted run even with the same seed.
                "rollout_rng": self.rollout.rng.bit_generator.state,
            },
        )
        return str(directory)

    def _maybe_resume(self) -> None:
        path = self.config.trainer.resume_from_path
        if path:
            directory = Path(path)
            if not directory.exists():
                raise FileNotFoundError(f"resume_from_path does not exist: {directory}")
            prefix = "global_step_"
            if not directory.name.startswith(prefix):
                raise ValueError(
                    f"resume_from_path must end with global_step_<N>, got {directory.name!r}"
                )
            try:
                step = int(directory.name[len(prefix) :])
            except ValueError as exc:
                raise ValueError(
                    f"resume_from_path must end with global_step_<N>, got {directory.name!r}"
                ) from exc
            # Load from the requested run, not from this run's default log dir.
            manager = CheckpointManager(directory.parent, max_to_keep=self.config.trainer.max_ckpt_to_keep)
        else:
            manager = self.checkpoint_manager
            step = manager.latest()
        if step is None:
            return
        actor_state, critic_state, state = manager.load(step)
        self.actor_group.load_actor_state_dict(actor_state)
        if self.critic_group is not None and critic_state is not None:
            self.critic_group.load_state_dict(critic_state)
        actor_optimizer, critic_optimizer = manager.load_optimizers(step)
        if actor_optimizer is not None:
            self.actor_group.load_optimizer_state_dict(actor_optimizer)
        if self.critic_group is not None and critic_optimizer is not None:
            self.critic_group.load_optimizer_state_dict(critic_optimizer)
        self.global_steps = int(state.get("step", step))
        # Restore training-progress state when present (new checkpoints).
        self._train_cursor = int(state.get("train_cursor", 0))
        self.epoch = int(state.get("epoch", 0))
        if self.kl_controller is not None and state.get("kl_coef") is not None:
            self.kl_controller.value = float(state["kl_coef"])
        rollout_rng = state.get("rollout_rng")
        if isinstance(rollout_rng, dict) and "state" in rollout_rng:
            self.rollout.rng.bit_generator.state = rollout_rng
        self._sync_weights()

    # ------------------------------------------------------------------
    # agent convenience
    # ------------------------------------------------------------------
    def agent_loop(self, max_turns: int = 3) -> AgentLoopManager:
        loop = AgentLoop(
            self.actor_group.workers[0].actor,
            self.tokenizer,
            max_turns=max_turns,
            seed=self.config.trainer.seed,
        )
        return AgentLoopManager(loop)
