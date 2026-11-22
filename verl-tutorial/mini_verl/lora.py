"""LoRA adapters for MiniVerl.

The real verl exposes LoRA through ``actor_rollout_ref.model.lora`` and lets
FSDP/Megatron train only the adapter while the base weights stay frozen.  This
module reproduces the same contract on top of MiniVerl's small autograd engine:

* ``LoRALinear`` wraps a frozen ``Linear`` and learns ``A`` and ``B``;
* ``apply_lora`` injects adapters into selected linear layers;
* ``merge_lora_inplace`` folds ``scale * A @ B`` into the base weight, which is
  what a rollout engine needs when it does not implement adapter inference.

The zero-initialised ``B`` means a freshly wrapped model is numerically
identical to the base model, so reference log-probabilities and rollout
behaviour do not change until the first optimizer step.
"""

from __future__ import annotations

import math
from typing import Iterator

import numpy as np

from .autograd import Parameter, Tensor
from .nn import Linear, Module

DEFAULT_TARGETS = ("c_attn", "c_proj", "fc_in", "fc_out", "lm_head")


class LoRALinear(Module):
    """``y = W x + scale * B A x`` with ``W`` frozen."""

    def __init__(
        self,
        base: Linear,
        rank: int = 8,
        alpha: int = 16,
        seed: int = 0,
        dropout: float = 0.0,
    ):
        super().__init__()
        if rank <= 0:
            raise ValueError("LoRA rank must be positive")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.base = base
        self.rank = int(rank)
        self.alpha = int(alpha)
        self.scaling = float(alpha) / float(rank)
        self.dropout = float(dropout)
        self.merged = False
        self._dropout_rng = np.random.default_rng(seed + 7)

        # Freeze every parameter of the wrapped layer.  The autograd engine
        # skips tensors whose ``requires_grad`` is false, so no gradients are
        # ever allocated for the base weights.
        for parameter in base.parameters():
            parameter.requires_grad = False

        rng = np.random.default_rng(seed)
        bound = 1.0 / math.sqrt(base.in_features)
        self.A = Parameter((rng.standard_normal((base.in_features, self.rank)) * bound).astype(np.float32))
        self.B = Parameter(np.zeros((self.rank, base.out_features), dtype=np.float32))

    # ------------------------------------------------------------------
    # forward / merge
    # ------------------------------------------------------------------
    def forward(self, x: Tensor) -> Tensor:
        out = self.base(x)
        if self.merged:
            return out
        adapter_input = x
        if self.training and self.dropout > 0:
            keep = self._dropout_rng.random(x.shape) >= self.dropout
            adapter_input = x * (keep.astype(np.float32) / (1.0 - self.dropout))
        delta = (adapter_input @ self.A @ self.B) * self.scaling
        return out + delta

    def delta_weight(self) -> np.ndarray:
        """Return the dense ``d_out x d_in`` update in PyTorch layout."""

        return (self.A.data @ self.B.data).T * self.scaling

    def merged_weight(self) -> np.ndarray:
        return self.base.weight.data + (self.A.data @ self.B.data) * self.scaling

    def merge_(self) -> None:
        """Fold the adapter into the base weight (irreversible)."""

        if self.merged:
            return
        self.base.weight.data[...] = self.merged_weight()
        self.merged = True

    def extra_repr(self) -> str:  # pragma: no cover - cosmetic
        return f"rank={self.rank}, alpha={self.alpha}, scaling={self.scaling:.3f}"


# ----------------------------------------------------------------------
# module tree helpers
# ----------------------------------------------------------------------
def _walk_linear(parent: Module, prefix: str = "") -> Iterator[tuple[Module, object, str, Linear]]:
    """Yield ``(parent, attribute_or_index, path, linear)`` for all Linears."""

    for name, value in list(parent.__dict__.items()):
        if name == "training":
            continue
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, Linear):
            yield parent, name, path, value
        elif isinstance(value, Module):
            yield from _walk_linear(value, path)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                item_path = f"{path}.{index}"
                if isinstance(item, Linear):
                    yield parent, (name, index), item_path, item
                elif isinstance(item, Module):
                    yield from _walk_linear(item, item_path)


def _replace(parent: Module, location, new_module: Module) -> None:
    if isinstance(location, tuple):
        name, index = location
        container = getattr(parent, name)
        if isinstance(container, tuple):
            items = list(container)
            items[index] = new_module
            setattr(parent, name, tuple(items))
        else:
            container[index] = new_module
    else:
        setattr(parent, location, new_module)


def apply_lora(
    model: Module,
    rank: int = 8,
    alpha: int = 16,
    targets: tuple[str, ...] | list[str] | None = None,
    seed: int = 0,
    dropout: float = 0.0,
) -> list[str]:
    """Wrap matching ``Linear`` layers in-place and return their paths.

    Matching is by the final attribute name (``c_attn``, ``fc_in``, ...), the
    same granularity as verl's ``target_modules``.  Reapplying the function to
    an already wrapped model is a no-op for those layers.
    """

    if rank <= 0:
        return []
    if isinstance(targets, str):
        # A CLI override such as lora_targets=fc_in arrives as a string;
        # set("fc_in") would silently become a set of characters.
        wanted = {item.strip() for item in targets.split(",") if item.strip()}
    else:
        wanted = set(targets or DEFAULT_TARGETS)
    replaced: list[str] = []
    for parent, location, path, linear in list(_walk_linear(model)):
        if path.rsplit(".", 1)[-1] not in wanted:
            continue
        adapter = LoRALinear(linear, rank=rank, alpha=alpha, seed=seed + len(replaced), dropout=dropout)
        _replace(parent, location, adapter)
        replaced.append(path)
    if replaced:
        freeze_base_parameters(model)
    return replaced


def freeze_base_parameters(model: Module) -> int:
    """Freeze everything except the ``A``/``B`` tensors of LoRA adapters.

    ``LoRALinear`` freezes its own base layer, but embeddings, norms and
    non-target linears would otherwise stay trainable.  Real LoRA freezes the
    whole base model, so the parameter count seen by the optimizer is exactly
    the adapter size.
    """

    adapter_ids = {id(parameter) for parameter in lora_parameters(model)}
    frozen = 0
    for parameter in model.parameters():
        if id(parameter) not in adapter_ids and parameter.requires_grad:
            parameter.requires_grad = False
            frozen += 1
    return frozen


def lora_modules(model: Module, prefix: str = "") -> Iterator[tuple[str, LoRALinear]]:
    for name, value in model.__dict__.items():
        if name == "training":
            continue
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, LoRALinear):
            yield path, value
        elif isinstance(value, Module):
            yield from lora_modules(value, path)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, LoRALinear):
                    yield f"{path}.{index}", item
                elif isinstance(item, Module):
                    yield from lora_modules(item, f"{path}.{index}")


def lora_parameters(model: Module) -> Iterator[Parameter]:
    for _, module in lora_modules(model):
        yield module.A
        yield module.B


def merge_lora_inplace(model: Module) -> int:
    """Merge every adapter into its base layer and unwrap it.

    Returns the number of merged adapters.  Only call this on a throw-away
    copy of the policy (for example the copy sent to a rollout engine); the
    training copy must keep its adapters.
    """

    merged = 0

    def visit(parent: Module) -> None:
        nonlocal merged
        for name, value in list(parent.__dict__.items()):
            if name == "training":
                continue
            if isinstance(value, LoRALinear):
                value.merge_()
                setattr(parent, name, value.base)
                merged += 1
            elif isinstance(value, Module):
                visit(value)
            elif isinstance(value, (list, tuple)):
                items = list(value)
                changed = False
                for index, item in enumerate(items):
                    if isinstance(item, LoRALinear):
                        item.merge_()
                        items[index] = item.base
                        merged += 1
                        changed = True
                    elif isinstance(item, Module):
                        visit(item)
                if changed:
                    setattr(parent, name, items if isinstance(value, list) else tuple(items))

    visit(model)
    return merged


def adapter_state_dict(model: Module) -> dict[str, np.ndarray]:
    """Only the trainable LoRA tensors, keyed by their module path."""

    return {f"{path}.{name}": getattr(module, name).data.copy() for path, module in lora_modules(model) for name in ("A", "B")}
