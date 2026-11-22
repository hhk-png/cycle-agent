"""A minimal DPO trainer sharing the actor/ref worker stack."""

from __future__ import annotations

from typing import Any

import numpy as np

from ..config import PPOConfig
from ..data import CharTokenizer, build_dataset
from ..metrics import MetricsAggregator, MetricsLogger
from ..protocol import DataProto
from ..single_controller.worker_group import LocalWorkerGroup, ResourcePool
from ..workers import ActorRolloutRefWorker

__all__ = ["DPOTrainer"]


class DPOTrainer:
    def __init__(self, config: PPOConfig):
        self.config = config
        self.tokenizer = CharTokenizer()
        self.dataset = build_dataset(config.data.dataset, config.data.train_size, config.data.seed)
        self.batch_size = config.data.train_batch_size
        self.world_size = max(1, config.trainer.n_gpus_per_node * config.trainer.nnodes)
        self.group = LocalWorkerGroup(
            ResourcePool.single_node(self.world_size),
            ActorRolloutRefWorker,
            {"config": config, "tokenizer": self.tokenizer, "role": "actor_rollout_ref", "seed": config.trainer.seed},
            name="dpo",
        )
        self.group.init_model()
        self.logger = MetricsLogger(
            config.trainer.log_dir, config.trainer.project_name, config.trainer.experiment_name, config.trainer.logger
        )
        self.aggregator = MetricsAggregator()

    def _make_batch(self, start: int) -> DataProto:
        samples = [self.dataset[(start + i) % len(self.dataset)] for i in range(self.batch_size)]
        prompt_len = self.config.data.max_prompt_length
        response_len = self.config.data.max_response_length
        prompts = np.zeros((len(samples), prompt_len), dtype=np.int64)
        prompt_mask = np.zeros((len(samples), prompt_len), dtype=np.float32)
        prompt_lengths = np.zeros(len(samples), dtype=np.int64)
        chosen = np.zeros((len(samples), response_len), dtype=np.int64)
        rejected = np.zeros((len(samples), response_len), dtype=np.int64)
        chosen_mask = np.zeros((len(samples), response_len), dtype=np.float32)
        rejected_mask = np.zeros((len(samples), response_len), dtype=np.float32)
        for row, sample in enumerate(samples):
            p = self.tokenizer.encode(sample.prompt)[-prompt_len:]
            c = self.tokenizer.encode(sample.answer, add_eos=True)[:response_len]
            r = list(c)
            if r:
                r[0] = (r[0] + 1) % self.tokenizer.vocab_size
            prompts[row, : len(p)] = p
            prompt_mask[row, : len(p)] = 1.0
            prompt_lengths[row] = len(p)
            chosen[row, : len(c)] = c
            chosen_mask[row, : len(c)] = 1.0
            rejected[row, : len(r)] = r
            rejected_mask[row, : len(r)] = 1.0
        return DataProto(
            batch={
                "prompt_ids": prompts,
                "prompt_mask": prompt_mask,
                "prompt_lengths": prompt_lengths,
                "chosen_ids": chosen,
                "chosen_mask": chosen_mask,
                "rejected_ids": rejected,
                "rejected_mask": rejected_mask,
            },
            non_tensor_batch={"prompts": np.asarray([s.prompt for s in samples], dtype=object)},
        )

    def fit(self) -> dict[str, Any]:
        total = self.config.trainer.total_training_steps or len(self.dataset) // self.batch_size
        history: dict[str, float] = {}
        for step in range(total):
            batch = self._make_batch(step * self.batch_size)
            batch.meta_info["global_num_pairs"] = float(len(batch))
            metrics = self.group.dpo_train_step(batch)
            if isinstance(metrics, list):
                merged: dict[str, float] = {}
                for item in metrics:
                    for key, value in item.items():
                        merged[key] = merged.get(key, 0.0) + float(value) / len(metrics)
                metrics = merged
            self.aggregator.add(metrics, len(batch))
            history = self.aggregator.result()
            self.logger.log(metrics, step + 1)
        return {"steps": total, "history": history}
