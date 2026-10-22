"""LoRA adapters for multi-model and multi-adapter serving.

DeepInfer itself is bound to one model, so this module is an extension rather
than a copy of a production component.  It exists because a real inference
platform rarely serves a single checkpoint: teams keep a base model plus a
handful of fine-tuned variants, and they want one engine, one KV cache design
and one scheduler to serve all of them.

The mathematics is exact.  The interesting parts for a systems reader are the
bookkeeping around it: adapter residency, LRU eviction, per-row adapter
dispatch, and the fact that rank-padding must preserve the scaling factor.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class LoRAConfig:
    """Shape of a low-rank update.

    ``scaling`` is ``alpha / rank``.  When an adapter is padded to a larger
    rank for batched execution, ``alpha`` must be scaled with the rank,
    otherwise the update silently changes magnitude.
    """

    rank: int = 8
    alpha: float = 16.0
    target_modules: tuple[str, ...] = ("q_proj", "v_proj")

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("rank must be >= 1")
        if self.alpha <= 0:
            raise ValueError("alpha must be positive")
        object.__setattr__(self, "target_modules", tuple(self.target_modules))
        if not self.target_modules:
            raise ValueError("at least one target module is required")
        if len(set(self.target_modules)) != len(self.target_modules):
            raise ValueError("target_modules contains duplicates")

    @property
    def scaling(self) -> float:
        return self.alpha / self.rank


@dataclass
class LoRAAdapter:
    """A set of low-rank deltas keyed by module name.

    ``tensors[module]`` is ``(A, B)`` with shapes ``(in_features, rank)`` and
    ``(rank, out_features)``.  The effective weight is ``base + scaling * A @ B``.
    Every target module declared in the config must be present; a partial
    adapter is rejected at construction time rather than silently behaving as
    an identity on the missing module.
    """

    adapter_id: str
    config: LoRAConfig
    tensors: dict[str, tuple[np.ndarray, np.ndarray]]
    base_model: str = ""
    tenant: str = ""

    def __post_init__(self) -> None:
        if not self.adapter_id:
            raise ValueError("adapter_id must not be empty")
        missing = [name for name in self.config.target_modules if name not in self.tensors]
        if missing:
            raise ValueError(f"adapter {self.adapter_id!r} is missing modules: {missing}")
        for module, (a, b) in self.tensors.items():
            if module not in self.config.target_modules:
                raise ValueError(f"module {module!r} is not in target_modules")
            a = np.asarray(a, dtype=np.float32)
            b = np.asarray(b, dtype=np.float32)
            if a.ndim != 2 or b.ndim != 2:
                raise ValueError(f"{module}: A and B must be 2-D")
            if a.shape[1] != self.config.rank or b.shape[0] != self.config.rank:
                raise ValueError(
                    f"{module}: expected rank {self.config.rank}, "
                    f"got A{a.shape} and B{b.shape}"
                )
            if not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)):
                raise ValueError(f"{module}: adapter contains non-finite values")
            a.setflags(write=False)
            b.setflags(write=False)
            self.tensors[module] = (a, b)

    @property
    def nbytes(self) -> int:
        return int(sum(a.nbytes + b.nbytes for a, b in self.tensors.values()))

    def delta(self, module: str) -> np.ndarray:
        """Return the dense low-rank delta for one module."""
        try:
            a, b = self.tensors[module]
        except KeyError as exc:
            raise KeyError(f"adapter {self.adapter_id!r} has no module {module!r}") from exc
        return (self.config.scaling * (a @ b)).astype(np.float32)

    def project(self, module: str, x: np.ndarray) -> np.ndarray:
        """Return the low-rank update ``scaling * x @ A @ B``.

        The result has shape ``(..., out_features)``.  It is added to the base
        linear output, not to the input; ``in_features`` and ``out_features``
        do not have to match.
        """
        x = np.asarray(x, dtype=np.float32)
        a, b = self.tensors[module]
        return self.config.scaling * ((x @ a) @ b)

    def apply(
        self, module: str, x: np.ndarray, base_output: np.ndarray
    ) -> np.ndarray:
        """Add the adapter to a base linear output: ``base + x @ A @ B``."""
        base_output = np.asarray(base_output, dtype=np.float32)
        delta = self.project(module, x)
        if base_output.shape != delta.shape:
            raise ValueError(
                f"base_output shape {base_output.shape} != delta shape {delta.shape}"
            )
        return base_output + delta

    def merged_weight(self, module: str, base: np.ndarray) -> np.ndarray:
        """Fold the adapter into a base weight, as a deployment optimization."""
        base = np.asarray(base, dtype=np.float32)
        delta = self.delta(module)
        if base.shape != delta.shape:
            raise ValueError(f"base shape {base.shape} != delta shape {delta.shape}")
        return base + delta

    def rank_padded(self, target_rank: int) -> "LoRAAdapter":
        """Return an equivalent adapter padded to ``target_rank``.

        Batching adapters of different ranks requires a common rank.  Padding
        A with zero columns and B with zero rows leaves the product unchanged;
        the config's alpha is scaled so ``alpha / rank`` stays constant.
        """
        if target_rank < self.config.rank:
            raise ValueError("target_rank must be >= the adapter rank")
        if target_rank == self.config.rank:
            return self
        padded: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for module, (a, b) in self.tensors.items():
            pa = np.zeros((a.shape[0], target_rank), dtype=np.float32)
            pb = np.zeros((target_rank, b.shape[1]), dtype=np.float32)
            pa[:, : a.shape[1]] = a
            pb[: b.shape[0], :] = b
            padded[module] = (pa, pb)
        config = LoRAConfig(
            rank=target_rank,
            alpha=self.config.alpha * target_rank / self.config.rank,
            target_modules=self.config.target_modules,
        )
        return LoRAAdapter(
            adapter_id=self.adapter_id,
            config=config,
            tensors=padded,
            base_model=self.base_model,
            tenant=self.tenant,
        )


def apply_batched(
    module: str,
    x: np.ndarray,
    base_output: np.ndarray,
    adapter_ids: Sequence[str | None],
    adapters: Mapping[str, LoRAAdapter],
) -> np.ndarray:
    """Apply per-row adapters to a batch of linear outputs.

    ``None`` means "base model, no adapter".  Rows are grouped by adapter so
    each adapter's low-rank GEMM runs once; the output is reassembled in the
    original row order.  This is the CPU analogue of a batched LoRA kernel.
    """
    x = np.asarray(x, dtype=np.float32)
    base_output = np.asarray(base_output, dtype=np.float32)
    if x.ndim != 2 or base_output.ndim != 2:
        raise ValueError("x and base_output must be 2-D")
    if x.shape[0] != base_output.shape[0]:
        raise ValueError("x and base_output must have the same batch dimension")
    if len(adapter_ids) != x.shape[0]:
        raise ValueError("adapter_ids length must match the batch dimension")

    out = base_output.copy()
    groups: dict[str, list[int]] = {}
    for row, adapter_id in enumerate(adapter_ids):
        if adapter_id is not None:
            groups.setdefault(adapter_id, []).append(row)

    for adapter_id, rows in groups.items():
        if adapter_id not in adapters:
            raise KeyError(f"unknown adapter {adapter_id!r}")
        index = np.asarray(rows, dtype=np.int64)
        adapter = adapters[adapter_id]
        out[index] = base_output[index] + adapter.project(module, x[index])
    return out


class AdapterRegistry:
    """Resident adapter set with LRU eviction and optional pinning.

    A production registry also needs ref-counting against in-flight requests:
    an adapter cannot be evicted while a batch is using it.  ``pin`` is the
    teaching version of that idea.
    """

    def __init__(self, max_adapters: int = 8, max_bytes: int | None = None) -> None:
        if max_adapters < 1:
            raise ValueError("max_adapters must be >= 1")
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive or None")
        self.max_adapters = max_adapters
        self.max_bytes = max_bytes
        self._items: "OrderedDict[str, LoRAAdapter]" = OrderedDict()
        self._pinned: set[str] = set()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, adapter_id: object) -> bool:
        return adapter_id in self._items

    @property
    def resident_bytes(self) -> int:
        return sum(adapter.nbytes for adapter in self._items.values())

    def register(self, adapter: LoRAAdapter, *, pin: bool = False) -> None:
        if adapter.adapter_id in self._items:
            raise ValueError(f"adapter {adapter.adapter_id!r} is already registered")
        if self.max_bytes is not None and adapter.nbytes > self.max_bytes:
            raise ValueError(
                f"adapter {adapter.adapter_id!r} needs {adapter.nbytes} bytes, "
                f"exceeding max_bytes={self.max_bytes}"
            )
        self._evict_for(adapter.nbytes)
        self._items[adapter.adapter_id] = adapter
        if pin:
            self._pinned.add(adapter.adapter_id)

    def _evict_for(self, incoming_bytes: int) -> None:
        while True:
            too_many = len(self._items) + 1 > self.max_adapters
            too_large = (
                self.max_bytes is not None
                and self.resident_bytes + incoming_bytes > self.max_bytes
            )
            if not too_many and not too_large:
                return
            victim = next(
                (key for key in self._items if key not in self._pinned),
                None,
            )
            if victim is None:
                raise ValueError("adapter capacity exceeded and every resident adapter is pinned")
            del self._items[victim]
            self.evictions += 1

    def get(self, adapter_id: str) -> LoRAAdapter:
        if adapter_id not in self._items:
            self.misses += 1
            raise KeyError(adapter_id)
        self.hits += 1
        self._items.move_to_end(adapter_id)
        return self._items[adapter_id]

    def remove(self, adapter_id: str) -> None:
        self._items.pop(adapter_id)
        self._pinned.discard(adapter_id)

    def pin(self, adapter_id: str) -> None:
        if adapter_id not in self._items:
            raise KeyError(adapter_id)
        self._pinned.add(adapter_id)

    def unpin(self, adapter_id: str) -> None:
        self._pinned.discard(adapter_id)

    def stats(self) -> dict[str, int]:
        return {
            "resident": len(self._items),
            "pinned": len(self._pinned),
            "bytes": self.resident_bytes,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
        }


def make_adapter(
    adapter_id: str,
    in_features: int,
    out_features: int,
    *,
    rank: int = 4,
    alpha: float = 8.0,
    modules: Iterable[str] = ("q_proj", "v_proj"),
    seed: int = 0,
    base_model: str = "",
    tenant: str = "",
) -> LoRAAdapter:
    """Build a deterministic random adapter for tests and examples."""
    modules = tuple(modules)
    config = LoRAConfig(rank=rank, alpha=alpha, target_modules=modules)
    rng = np.random.default_rng(seed)
    tensors = {
        module: (
            rng.normal(0.0, 0.02, size=(in_features, rank)).astype(np.float32),
            rng.normal(0.0, 0.02, size=(rank, out_features)).astype(np.float32),
        )
        for module in modules
    }
    return LoRAAdapter(
        adapter_id=adapter_id,
        config=config,
        tensors=tensors,
        base_model=base_model,
        tenant=tenant,
    )
