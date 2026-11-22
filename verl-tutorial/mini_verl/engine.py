"""Training engines and optimizers.

Mirrors the split used by verl v0.9.1:

* ``BaseEngine`` is the backend-facing contract (forward/backward, optimizer,
  checkpoint, per-tensor export).
* ``FSDPEngine`` is the MiniVerl dense-replica implementation.  Each worker
  owns a full model, gradients are averaged by the worker group, and the
  optimizer state is replicated.  This is deliberately the simplest correct
  data-parallel baseline; the FSDP/Megatron chapters explain how production
  engines shard the same quantities.
* ``EngineRegistry`` chooses a backend from ``strategy``.
"""

from __future__ import annotations

import contextlib
import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Generator, Iterator

import numpy as np

from .autograd import Tensor, no_grad
from .config import EngineConfig, OptimConfig

__all__ = [
    "BaseEngine",
    "FSDPEngine",
    "MegatronEngine",
    "EngineRegistry",
    "AdamW",
    "SGD",
    "clip_gradients",
    "estimate_memory",
]


class Optimizer:
    def __init__(self, model, config: OptimConfig):
        self.model = model
        self.config = config
        self.step_count = 0
        self.state: dict[str, dict[str, np.ndarray]] = {}

    def zero_grad(self) -> None:
        for parameter in self.model.parameters():
            parameter.grad = None

    def state_dict(self) -> dict[str, Any]:
        return {"step_count": self.step_count, "state": {key: {k: v.copy() for k, v in value.items()} for key, value in self.state.items()}}

    def load_state_dict(self, payload: dict[str, Any]) -> None:
        self.step_count = int(payload.get("step_count", 0))
        self.state = {
            key: {k: np.asarray(v, dtype=np.float32).copy() for k, v in value.items()}
            for key, value in payload.get("state", {}).items()
        }

    @abstractmethod
    def step_named(self, named_grads: dict[str, np.ndarray]) -> None:  # pragma: no cover
        raise NotImplementedError

    def current_lr(self) -> float:
        lr = self.config.lr
        warmup = int(self.config.total_training_steps * self.config.lr_warmup_steps_ratio) if self.config.total_training_steps > 0 else 0
        if warmup > 0 and self.step_count < warmup:
            return lr * (self.step_count + 1) / warmup
        if self.config.lr_scheduler_type == "cosine" and self.config.total_training_steps > 0:
            progress = min(1.0, self.step_count / self.config.total_training_steps)
            return lr * 0.5 * (1.0 + math.cos(math.pi * progress))
        return lr


class SGD(Optimizer):
    def __init__(self, model, config: OptimConfig, momentum: float = 0.9):
        super().__init__(model, config)
        self.momentum = momentum

    def step_named(self, named_grads: dict[str, np.ndarray]) -> None:
        lr = self.current_lr()
        for name, parameter in self.model.named_parameters():
            # Frozen base weights (LoRA) must not move even if their gradient
            # slot is a zero array: weight decay would otherwise update them.
            if not parameter.requires_grad:
                continue
            grad = named_grads.get(name)
            if grad is None:
                continue
            state = self.state.setdefault(name, {"momentum": np.zeros_like(parameter.data)})
            state["momentum"] = self.momentum * state["momentum"] + grad
            parameter.data -= lr * state["momentum"]
            if self.config.weight_decay:
                parameter.data -= lr * self.config.weight_decay * parameter.data
        self.step_count += 1


class AdamW(Optimizer):
    def step_named(self, named_grads: dict[str, np.ndarray]) -> None:
        lr = self.current_lr()
        beta1, beta2 = self.config.betas
        for name, parameter in self.model.named_parameters():
            if not parameter.requires_grad:
                continue
            grad = named_grads.get(name)
            if grad is None:
                continue
            state = self.state.setdefault(
                name,
                {"m": np.zeros_like(parameter.data), "v": np.zeros_like(parameter.data)},
            )
            state["m"] = beta1 * state["m"] + (1.0 - beta1) * grad
            state["v"] = beta2 * state["v"] + (1.0 - beta2) * grad * grad
            m_hat = state["m"] / (1.0 - beta1 ** (self.step_count + 1))
            v_hat = state["v"] / (1.0 - beta2 ** (self.step_count + 1))
            parameter.data -= lr * (m_hat / (np.sqrt(v_hat) + self.config.eps) + self.config.weight_decay * parameter.data)
        self.step_count += 1


def clip_gradients(named_grads: dict[str, np.ndarray], max_norm: float) -> tuple[dict[str, np.ndarray], float]:
    if max_norm <= 0:
        return named_grads, 0.0
    total = math.sqrt(sum(float(np.sum(grad * grad)) for grad in named_grads.values()))
    if total > max_norm and total > 0:
        scale = max_norm / (total + 1e-6)
        return {name: grad * scale for name, grad in named_grads.items()}, total
    return named_grads, total


class BaseEngine(ABC):
    def __init__(self, model, optim_config: OptimConfig, engine_config: EngineConfig, strategy: str = "fsdp"):
        self.model = model
        self.optim_config = optim_config
        self.engine_config = engine_config
        self.strategy = strategy
        self.optimizer: Optimizer | None = None
        self.offloaded = False
        self._pending_grad_metrics: dict[str, float] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        self.optimizer = AdamW(self.model, self.optim_config)

    @property
    def is_param_offload_enabled(self) -> bool:
        return self.engine_config.param_offload

    @property
    def is_optimizer_offload_enabled(self) -> bool:
        return self.engine_config.optimizer_offload

    @contextlib.contextmanager
    def train_mode(self):
        self.model.train()
        yield

    @contextlib.contextmanager
    def eval_mode(self):
        was_training = self.model.training
        self.model.eval()
        try:
            yield
        finally:
            if was_training:
                self.model.train()

    def optimizer_zero_grad(self) -> None:
        assert self.optimizer is not None
        self.optimizer.zero_grad()

    def optimizer_step(self) -> float:
        assert self.optimizer is not None
        before = float(sum(float(np.sum(p.data * p.data)) for p in self.model.parameters()))
        self.optimizer.step_named(self.get_gradients())
        after = float(sum(float(np.sum(p.data * p.data)) for p in self.model.parameters()))
        return abs(after - before)

    def lr_scheduler_step(self) -> float:
        assert self.optimizer is not None
        return self.optimizer.current_lr()

    # ------------------------------------------------------------------
    # computation
    # ------------------------------------------------------------------
    def forward_backward_batch(
        self,
        data: dict[str, Any],
        loss_function: Callable,
        forward_only: bool = False,
        zero_grad: bool = True,
    ):
        if forward_only:
            with no_grad(), self.eval_mode():
                return loss_function(self.model, data)
        with self.train_mode():
            if zero_grad:
                self.optimizer_zero_grad()
            loss, metrics = loss_function(self.model, data)
            loss.backward()
        return loss, metrics

    def train_batch(self, data: dict[str, Any], loss_function: Callable) -> dict[str, float]:
        loss, metrics = self.forward_backward_batch(data, loss_function, forward_only=False)
        grad_metrics = self.finalize_update()
        output = dict(metrics)
        output.update(grad_metrics)
        output["loss"] = float(loss.item())
        return output

    def infer_batch(self, data: dict[str, Any], loss_function: Callable | None = None):
        if loss_function is None:
            raise ValueError("infer_batch needs a loss/metric function")
        return self.forward_backward_batch(data, loss_function, forward_only=True)

    # ------------------------------------------------------------------
    # gradients
    # ------------------------------------------------------------------
    def get_gradients(self) -> dict[str, np.ndarray]:
        grads = {}
        for name, parameter in self.model.named_parameters():
            if parameter.grad is not None:
                grads[name] = parameter.grad.copy()
            else:
                grads[name] = np.zeros_like(parameter.data)
        return grads

    def set_gradients(self, grads: dict[str, np.ndarray]) -> None:
        for name, parameter in self.model.named_parameters():
            if name in grads:
                parameter.grad = np.asarray(grads[name], dtype=np.float32).copy()

    def finalize_update(self) -> dict[str, float]:
        assert self.optimizer is not None
        grads, total_norm = clip_gradients(self.get_gradients(), self.optim_config.clip_grad)
        param_norm = math.sqrt(sum(float(np.sum(p.data * p.data)) for p in self.model.parameters()))
        self.optimizer.step_named(grads)
        lr = self.optimizer.current_lr()
        self.optimizer.zero_grad()
        self._pending_grad_metrics = {
            "grad_norm": total_norm,
            "param_norm": param_norm,
            "lr": lr,
        }
        return dict(self._pending_grad_metrics)

    def pop_grad_metrics(self) -> dict[str, float]:
        metrics = dict(self._pending_grad_metrics)
        self._pending_grad_metrics = {}
        return metrics

    # ------------------------------------------------------------------
    # parameters / checkpoints
    # ------------------------------------------------------------------
    def get_per_tensor_param(self) -> Generator[tuple[str, np.ndarray], None, None]:
        for name, parameter in self.model.named_parameters():
            yield name, parameter.data.copy()

    def state_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.state_dict(),
            "optimizer": self.optimizer.state_dict() if self.optimizer else {},
            "strategy": self.strategy,
        }

    def load_state_dict(self, state: dict[str, Any]) -> None:
        self.model.load_state_dict(state["model"])
        if self.optimizer is not None and state.get("optimizer"):
            self.optimizer.load_state_dict(state["optimizer"])

    def save_checkpoint(self, path: str, prefix: str = "model") -> None:
        Path(path).mkdir(parents=True, exist_ok=True)
        arrays = {f"{prefix}.{name}": value for name, value in self.model.state_dict().items()}
        np.savez(Path(path) / f"{prefix}.npz", **arrays)
        if self.optimizer is not None:
            payload = self.optimizer.state_dict()
            opt_arrays = {"__step_count__": np.asarray([payload["step_count"]])}
            for name, value in payload["state"].items():
                for key, array in value.items():
                    opt_arrays[f"{name}::{key}"] = array
            np.savez(Path(path) / f"{prefix}_optimizer.npz", **opt_arrays)

    def load_checkpoint(self, path: str, prefix: str = "model") -> None:
        model_path = Path(path) / f"{prefix}.npz"
        arrays = np.load(model_path)
        self.model.load_state_dict({key[len(prefix) + 1 :]: arrays[key] for key in arrays.files})
        opt_path = Path(path) / f"{prefix}_optimizer.npz"
        if self.optimizer is not None and opt_path.exists():
            arrays = np.load(opt_path)
            state: dict[str, dict[str, np.ndarray]] = {}
            for key in arrays.files:
                if key == "__step_count__":
                    continue
                name, field = key.split("::", 1)
                state.setdefault(name, {})[field] = arrays[key]
            self.optimizer.load_state_dict({"step_count": int(arrays["__step_count__"][0]), "state": state})

    def to(self, device: str, model: bool = True, optimizer: bool = True, grad: bool = True) -> None:
        if device not in {"cpu", "device", "cuda", "npu"}:
            raise ValueError("device must be cpu/device")
        self.offloaded = device == "cpu"

    def memory_bytes(self) -> dict[str, int]:
        params = sum(p.size for p in self.model.parameters()) * 4
        grads = params
        optimizer = 2 * params if isinstance(self.optimizer, AdamW) else params
        return {"params": params, "grads": grads, "optimizer": optimizer}

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(strategy={self.strategy}, offload={self.offloaded})"


class FSDPEngine(BaseEngine):
    """Dense replica engine used by MiniVerl.

    The name is a teaching homage: production FSDP shards parameters, gradients
    and optimizer state.  Here every worker holds a replica and the worker
    group all-reduces gradients, which is exactly the semantics FSDP preserves
    from the user's point of view.
    """


class MegatronEngine(BaseEngine):
    """Same dense math, but the worker may export weights in TP layout.

    Real Megatron shards each linear along TP/PP/CP/EP.  MiniVerl implements
    those layouts in ``mini_verl.parallel`` and ``mini_verl.sharding``; the
    training math stays dense so the PPO loop is independent of the backend,
    just as ``BaseEngine`` makes it in verl.
    """


class EngineRegistry:
    @staticmethod
    def new(strategy: str, model, optim_config: OptimConfig, engine_config: EngineConfig) -> BaseEngine:
        if strategy in {"fsdp", "fsdp2"}:
            engine = FSDPEngine(model, optim_config, engine_config, strategy=strategy)
        elif strategy in {"megatron", "megatron_lm"}:
            engine = MegatronEngine(model, optim_config, engine_config, strategy=strategy)
        else:
            raise ValueError(f"unknown engine strategy {strategy!r}")
        engine.initialize()
        return engine


def estimate_memory(
    num_parameters: int,
    bytes_per_param: int = 2,
    optimizer: str = "adamw",
    gradient_checkpointing: bool = False,
    world_size: int = 1,
    zero_stage: int = 0,
    activation_tokens: int = 0,
    hidden_size: int = 4096,
    num_layers: int = 32,
    kv_heads: int = 8,
    head_dim: int = 128,
    sequence_length: int = 4096,
    batch_size: int = 1,
) -> dict[str, float]:
    """Rough training-memory estimator (GiB).

    The coefficients are intentionally simple and documented in Chapter 32:
    parameters + gradients + Adam moments (2x), plus activations and a KV-cache
    proxy for rollout.  It is a planning tool, not a profiler.
    """
    gib = 1024**3
    param = num_parameters * bytes_per_param
    grad = num_parameters * bytes_per_param
    optim = num_parameters * (8 if optimizer == "adamw" else 4)
    shard = max(1, world_size if zero_stage >= 1 else 1)
    optim = optim / shard
    if zero_stage >= 2:
        grad = grad / shard
    if zero_stage >= 3:
        param = param / shard
    layers_per_rank = max(1, num_layers)
    activation = (
        batch_size
        * activation_tokens
        * hidden_size
        * layers_per_rank
        * bytes_per_param
        * (1 if gradient_checkpointing else 8)
    )
    kv_cache = batch_size * sequence_length * num_layers * 2 * kv_heads * head_dim * bytes_per_param
    return {
        "params_gib": param / gib,
        "grads_gib": grad / gib,
        "optimizer_gib": optim / gib,
        "activations_gib": activation / gib,
        "kv_cache_gib": kv_cache / gib,
        "total_gib": (param + grad + optim + activation + kv_cache) / gib,
    }
