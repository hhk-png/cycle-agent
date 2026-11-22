"""Supervised fine-tuning on the same engine/worker stack as PPO."""

from __future__ import annotations

from typing import Any

import numpy as np

from ..config import PPOConfig
from ..data import CharTokenizer, build_dataset, make_batches
from ..metrics import MetricsAggregator, MetricsLogger
from ..single_controller.worker_group import LocalWorkerGroup, ResourcePool
from ..workers import ActorRolloutRefWorker

__all__ = ["SFTTrainer"]


class SFTTrainer:
    def __init__(self, config: PPOConfig):
        self.config = config
        self.tokenizer = CharTokenizer()
        self.dataset = build_dataset(config.data.dataset, config.data.train_size, config.data.seed)
        self.batches = make_batches(
            self.dataset,
            config.data.train_batch_size,
            self.tokenizer,
            config.data.max_prompt_length,
            config.data.max_response_length,
            shuffle=config.data.shuffle,
            seed=config.data.seed,
        )
        self.world_size = max(1, config.trainer.n_gpus_per_node * config.trainer.nnodes)
        self.group = LocalWorkerGroup(
            ResourcePool.single_node(self.world_size),
            ActorRolloutRefWorker,
            {"config": config, "tokenizer": self.tokenizer, "role": "actor", "seed": config.trainer.seed},
            name="sft",
        )
        self.group.init_model()
        self.logger = MetricsLogger(
            config.trainer.log_dir, config.trainer.project_name, config.trainer.experiment_name, config.trainer.logger
        )
        self.aggregator = MetricsAggregator()

    def fit(self) -> dict[str, Any]:
        total = self.config.trainer.total_training_steps or len(self.batches)
        cursor = 0
        history: dict[str, float] = {}
        for step in range(total):
            if cursor >= len(self.batches):
                cursor = 0
            batch = self.batches[cursor]
            cursor += 1
            batch.meta_info["global_num_tokens"] = int(batch.batch["response_mask"].sum())
            batch.meta_info["global_batch_size"] = len(batch)
            metrics = self.group.sft_train_step(batch)
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
