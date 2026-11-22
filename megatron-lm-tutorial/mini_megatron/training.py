"""Training loop with gradient accumulation, DDP/ZeRO, evaluation and logging."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import torch

from .checkpoint import load_checkpoint, save_checkpoint
from .config import ExperimentConfig
from .data.dataset import BatchProvider, create_dataset
from .parallel import comm
from .parallel.layers import set_model_parallel_init_seed
from .optim.adamw import (
    ZeRO1Optimizer,
    all_reduce_gradients,
    build_optimizer,
    clip_gradients,
    get_lr,
    set_lr,
)
from .optim.distributed_optimizer import DistributedOptimizer
from .parallel.state import (
    get_context_parallel_group,
    get_context_parallel_world_size,
    get_data_parallel_group,
    get_data_parallel_rank,
    get_data_parallel_world_size,
    get_rank,
    get_world_size,
)
from .parallel.transformer import TransformerStage
from .pipeline.schedules import build_schedule


def _is_dist() -> bool:
    return comm.is_initialized()


def global_mean(value: torch.Tensor) -> torch.Tensor:
    if not _is_dist() or get_data_parallel_world_size() <= 1:
        return value.detach()
    out = comm.all_reduce(value.detach(), op=comm.SUM, group=get_data_parallel_group())
    out = out / get_data_parallel_world_size()
    return out


class Trainer:
    def __init__(
        self,
        config: ExperimentConfig,
        *,
        stage: Optional[TransformerStage] = None,
        device: Optional[str] = None,
    ) -> None:
        self.config = config
        self.world_size = get_world_size()
        config.validate(self.world_size)
        self.device = torch.device(device or config.train.device)
        self.dp_size = get_data_parallel_world_size()
        self.grad_accum = config.train.global_batch_size // (
            config.train.micro_batch_size * self.dp_size
        )
        if self.grad_accum < 1:
            raise ValueError("global_batch_size is too small for the data-parallel layout")

        # Model-parallel peers must start from the same RNG stream; otherwise
        # TP shards and DP replicas would disagree.  Data is differentiated by
        # the data-parallel rank inside BatchProvider, not by the model seed.
        torch.manual_seed(config.train.seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(config.train.seed)

        if stage is None:
            # Offset sharded-layer initialisation by TP rank so the shards are
            # different slices of one matrix, not copies of the same slice.
            set_model_parallel_init_seed(config.train.seed)
            self.stage = TransformerStage(config.model, config.parallel)
        else:
            self.stage = stage
        self.stage.to(self.device)
        self.optimizer = build_optimizer(self.stage, config.train)
        self.zero_optimizer: Optional[ZeRO1Optimizer] = None
        self.dist_optimizer: Optional[DistributedOptimizer] = None
        if config.parallel.distributed_optimizer:
            if config.parallel.distributed_optimizer_style == "flat":
                self.dist_optimizer = DistributedOptimizer(
                    self.stage,
                    lr=config.train.lr,
                    betas=(config.train.adam_beta1, config.train.adam_beta2),
                    eps=config.train.adam_eps,
                    weight_decay=config.train.weight_decay,
                    group=get_data_parallel_group(),
                )
            elif self.dp_size > 1:
                self.zero_optimizer = ZeRO1Optimizer(
                    self.stage, self.optimizer, group=get_data_parallel_group()
                )
        self.schedule = build_schedule(self.stage, config.parallel, config.train)

        train_ds = create_dataset(config.model, config.train, seed=config.train.seed)
        val_ds = create_dataset(config.model, config.train, seed=config.train.seed + 9973)
        self.train_provider = BatchProvider(
            train_ds,
            micro_batch_size=config.train.micro_batch_size,
            grad_accumulation_steps=self.grad_accum,
            data_parallel_rank=get_data_parallel_rank(),
            data_parallel_size=self.dp_size,
            seq_len=config.train.seq_len,
            seed=config.train.seed,
        )
        self.val_provider = BatchProvider(
            val_ds,
            micro_batch_size=config.train.micro_batch_size,
            grad_accumulation_steps=self.grad_accum,
            data_parallel_rank=get_data_parallel_rank(),
            data_parallel_size=self.dp_size,
            seq_len=config.train.seq_len,
            seed=config.train.seed + 9973,
        )
        self.history: List[Dict[str, float]] = []

    @property
    def _optimizer_for_lr(self):
        if self.dist_optimizer:
            return self.dist_optimizer
        return self.zero_optimizer.optimizer if self.zero_optimizer else self.optimizer

    def _set_lr(self, lr: float) -> None:
        if self.dist_optimizer:
            self.dist_optimizer.set_lr(lr)
        elif self.zero_optimizer:
            self.zero_optimizer.set_lr(lr)
        else:
            set_lr(self.optimizer, lr)

    def _zero_grad(self) -> None:
        if self.dist_optimizer:
            self.dist_optimizer.zero_grad()
        elif self.zero_optimizer:
            self.zero_optimizer.zero_grad()
        else:
            self.optimizer.zero_grad(set_to_none=True)

    def _optim_step(self) -> None:
        if self.dist_optimizer:
            self.dist_optimizer.step()
        elif self.zero_optimizer:
            self.zero_optimizer.step()
        else:
            self.optimizer.step()

    def train_step(self, step: int) -> Dict[str, float]:
        self.stage.train()
        self._zero_grad()
        started = time.perf_counter()
        micro_batches = [
            (x.to(self.device), y.to(self.device))
            for x, y in self.train_provider.get_micro_batches(step)
        ]
        loss, aux = self.schedule.forward_backward(micro_batches)

        if self.dist_optimizer:
            # The flat optimizer owns the DP reduction: it reduce-scatters the
            # flat gradient buffer and updates only the local shard.  CP is a
            # different axis and still has to be summed first.
            if get_context_parallel_world_size() > 1:
                all_reduce_gradients(
                    self.stage,
                    get_context_parallel_group(),
                    average=False,
                    skip_expert_parallel=False,
                )
            self.dist_optimizer.prepare()
            grad_norm = self.dist_optimizer.clip_grad_norm(
                self.config.train.grad_clip
            )
        else:
            all_reduce_gradients(self.stage, get_data_parallel_group())
            if get_context_parallel_world_size() > 1:
                # Each CP rank owns a different sequence slice; sum the gradient
                # contributions of all slices into every replica.
                all_reduce_gradients(
                    self.stage,
                    get_context_parallel_group(),
                    average=False,
                    skip_expert_parallel=False,
                )
            grad_norm = clip_gradients(self.stage, self.config.train.grad_clip)
        lr = get_lr(
            step,
            max_steps=self.config.train.max_steps,
            warmup_steps=self.config.train.warmup_steps,
            base_lr=self.config.train.lr,
            min_lr=self.config.train.min_lr,
            style=self.config.train.lr_decay_style,
        )
        self._set_lr(lr)
        self._optim_step()

        elapsed = time.perf_counter() - started
        loss_mean = float(global_mean(loss).item())
        aux_mean = float(global_mean(aux).item())
        tokens = self.config.train.global_batch_size * self.config.train.seq_len
        record = {
            "step": step,
            "loss": loss_mean,
            "aux_loss": aux_mean,
            "lr": lr,
            "grad_norm": float(grad_norm.item()),
            "tokens_per_sec": tokens / max(elapsed, 1e-9),
        }
        self.history.append(record)
        if get_rank() == 0 and step % max(1, self.config.train.log_interval) == 0:
            print(json.dumps(record, ensure_ascii=False), flush=True)
        if self.config.train.log_jsonl and get_rank() == 0:
            Path(self.config.train.log_jsonl).parent.mkdir(parents=True, exist_ok=True)
            with open(self.config.train.log_jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        return record

    @torch.no_grad()
    def evaluate(self, step: int = 0) -> float:
        self.stage.eval()
        losses = []
        for i in range(max(1, self.config.train.eval_iters)):
            batches = [
                (x.to(self.device), y.to(self.device))
                for x, y in self.val_provider.get_micro_batches(
                    step, sample_offset=i * 101
                )
            ]
            loss, _ = self.schedule.forward_only(batches)
            losses.append(loss.detach())
        value = torch.stack(losses).mean()
        value = global_mean(value)
        return float(value.item())

    def save(self, directory: str | Path, step: int) -> Path:
        return save_checkpoint(
            directory,
            stage=self.stage,
            optimizer=self.dist_optimizer or self.zero_optimizer or self.optimizer,
            step=step,
            config=self.config,
            dp_size=self.dp_size,
            extra={"grad_accum": self.grad_accum},
        )

    def resume(self, path: str | Path) -> int:
        info = load_checkpoint(
            path,
            stage=self.stage,
            optimizer=self.dist_optimizer or self.zero_optimizer or self.optimizer,
            config=self.config,
            dp_size=self.dp_size,
        )
        return int(info["step"])

    def fit(self, *, resume: Optional[str] = None, save_final: bool = True) -> List[Dict[str, float]]:
        start = 0
        if resume:
            # ``save_checkpoint`` is called with ``step + 1`` after a completed
            # update, so the stored step is the *number of completed steps*
            # (0..last-1).  The next step to run is therefore ``last``, not
            # ``last + 1``; the latter silently skips one optimizer step.
            start = self.resume(resume)
        for step in range(start, self.config.train.max_steps):
            self.train_step(step)
            if (
                self.config.train.eval_interval > 0
                and (step + 1) % self.config.train.eval_interval == 0
            ):
                val = self.evaluate(step)
                if get_rank() == 0:
                    print(json.dumps({"step": step + 1, "val_loss": val}), flush=True)
            if (
                self.config.train.save_interval > 0
                and (step + 1) % self.config.train.save_interval == 0
            ):
                self.save(self.config.train.checkpoint_dir, step + 1)
        if save_final and self.config.train.checkpoint_dir:
            self.save(self.config.train.checkpoint_dir, self.config.train.max_steps)
        return self.history
