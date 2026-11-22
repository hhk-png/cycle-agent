"""DataProto: the batch contract shared by controller, workers and rollout.

Real verl stores tensor fields in a ``TensorDict`` and non-tensor fields in
NumPy object arrays.  MiniVerl keeps the same three-part shape:

* ``batch``: ``dict[str, np.ndarray]``, every array has the same first dim;
* ``non_tensor_batch``: ``dict[str, np.ndarray[dtype=object]]``;
* ``meta_info``: arbitrary Python metadata (temperature, versions, ...).

The operations here are the ones the PPO loop actually needs: split/chunk for
dispatch, concat for collect, repeat for ``rollout.n``, reorder for length
balancing, and padding to a world-size divisor.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import numpy as np

__all__ = ["DataProto", "pad_dataproto_to_divisor", "unpad_dataproto"]


def _as_object_array(values: Iterable[Any]) -> np.ndarray:
    array = np.empty(len(list(values)) if not isinstance(values, np.ndarray) else len(values), dtype=object)
    for i, value in enumerate(values):
        array[i] = value
    return array


class DataProto:
    def __init__(
        self,
        batch: Mapping[str, np.ndarray] | None = None,
        non_tensor_batch: Mapping[str, np.ndarray] | None = None,
        meta_info: Mapping[str, Any] | None = None,
    ) -> None:
        self.batch: dict[str, np.ndarray] = {}
        self.non_tensor_batch: dict[str, np.ndarray] = {}
        self.meta_info: dict[str, Any] = dict(meta_info or {})

        for key, value in (batch or {}).items():
            array = np.asarray(value)
            if array.ndim == 0:
                raise ValueError(f"batch field {key!r} must have a leading batch dimension")
            self.batch[key] = array
        for key, value in (non_tensor_batch or {}).items():
            array = np.asarray(value, dtype=object)
            if array.ndim == 0:
                raise ValueError(f"non_tensor field {key!r} must have a leading batch dimension")
            self.non_tensor_batch[key] = array

        self._validate()

    # ------------------------------------------------------------------
    # basic protocol
    # ------------------------------------------------------------------
    def _validate(self) -> None:
        lengths = {array.shape[0] for array in self.batch.values()}
        lengths |= {array.shape[0] for array in self.non_tensor_batch.values()}
        if len(lengths) > 1:
            raise ValueError(f"all fields must share the batch dimension, got {sorted(lengths)}")

    def __len__(self) -> int:
        if self.batch:
            return next(iter(self.batch.values())).shape[0]
        if self.non_tensor_batch:
            return next(iter(self.non_tensor_batch.values())).shape[0]
        return 0

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"DataProto(len={len(self)}, tensor={sorted(self.batch)}, "
            f"non_tensor={sorted(self.non_tensor_batch)}, meta={sorted(self.meta_info)})"
        )

    def keys(self) -> list[str]:
        return list(self.batch) + list(self.non_tensor_batch)

    def copy(self) -> "DataProto":
        return DataProto(
            {key: value.copy() for key, value in self.batch.items()},
            {key: value.copy() for key, value in self.non_tensor_batch.items()},
            dict(self.meta_info),
        )

    # ------------------------------------------------------------------
    # selection
    # ------------------------------------------------------------------
    def __getitem__(self, index) -> "DataProto":
        if isinstance(index, int):
            # Keep a batch dimension of one, matching DataProto slicing.
            index = slice(index, index + 1)
        return DataProto(
            {key: value[index] for key, value in self.batch.items()},
            {key: value[index] for key, value in self.non_tensor_batch.items()},
            dict(self.meta_info),
        )

    def select_idxs(self, indices: np.ndarray | list[int]) -> "DataProto":
        indices = np.asarray(indices, dtype=np.int64)
        return DataProto(
            {key: value[indices] for key, value in self.batch.items()},
            {key: value[indices] for key, value in self.non_tensor_batch.items()},
            dict(self.meta_info),
        )

    def reorder(self, indices: np.ndarray | list[int]) -> "DataProto":
        return self.select_idxs(indices)

    def slice(self, start: int, end: int) -> "DataProto":
        return self[slice(start, end)]

    # ------------------------------------------------------------------
    # splitting / merging
    # ------------------------------------------------------------------
    def chunk(self, chunks: int) -> list["DataProto"]:
        if chunks <= 0:
            raise ValueError("chunks must be positive")
        size = len(self)
        if size % chunks:
            raise ValueError(f"cannot split {size} samples into {chunks} equal chunks; pad first")
        step = size // chunks
        return [self[i * step : (i + 1) * step] for i in range(chunks)]

    def split(self, size: int) -> list["DataProto"]:
        if size <= 0:
            raise ValueError("size must be positive")
        return [self[i : min(i + size, len(self))] for i in range(0, len(self), size)]

    @classmethod
    def concat(cls, items: Iterable["DataProto"]) -> "DataProto":
        items = list(items)
        if not items:
            raise ValueError("cannot concat an empty list")
        batch: dict[str, np.ndarray] = {}
        non_tensor: dict[str, np.ndarray] = {}
        for key in items[0].batch:
            batch[key] = np.concatenate([item.batch[key] for item in items], axis=0)
        for key in items[0].non_tensor_batch:
            non_tensor[key] = np.concatenate([item.non_tensor_batch[key] for item in items], axis=0)
        meta = dict(items[0].meta_info)
        for item in items[1:]:
            for key, value in item.meta_info.items():
                meta.setdefault(key, value)
        return cls(batch, non_tensor, meta)

    def union(self, other: "DataProto") -> "DataProto":
        if len(self) != len(other):
            raise ValueError(f"cannot union batches of length {len(self)} and {len(other)}")
        batch = dict(self.batch)
        for key, value in other.batch.items():
            if key in batch and not np.array_equal(batch[key], value):
                raise ValueError(f"conflicting tensor field {key!r}")
            batch[key] = value
        non_tensor = dict(self.non_tensor_batch)
        for key, value in other.non_tensor_batch.items():
            if key in non_tensor and not np.array_equal(non_tensor[key], value):
                raise ValueError(f"conflicting non-tensor field {key!r}")
            non_tensor[key] = value
        meta = dict(self.meta_info)
        meta.update(other.meta_info)
        return DataProto(batch, non_tensor, meta)

    def repeat(self, times: int, interleave: bool = True) -> "DataProto":
        if times < 1:
            raise ValueError("times must be >= 1")
        indices = np.repeat(np.arange(len(self)), times) if interleave else np.tile(np.arange(len(self)), times)
        return self.select_idxs(indices)

    def pad_to_divisor(self, divisor: int) -> tuple["DataProto", int]:
        return pad_dataproto_to_divisor(self, divisor)

    # ------------------------------------------------------------------
    # field helpers
    # ------------------------------------------------------------------
    def pop(self, key: str) -> np.ndarray:
        if key in self.batch:
            return self.batch.pop(key)
        if key in self.non_tensor_batch:
            return self.non_tensor_batch.pop(key)
        raise KeyError(key)

    def rename(self, mapping: Mapping[str, str]) -> "DataProto":
        for old, new in mapping.items():
            if old in self.batch:
                self.batch[new] = self.batch.pop(old)
            elif old in self.non_tensor_batch:
                self.non_tensor_batch[new] = self.non_tensor_batch.pop(old)
            else:
                raise KeyError(old)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch": {key: value.copy() for key, value in self.batch.items()},
            "non_tensor_batch": {key: value.copy() for key, value in self.non_tensor_batch.items()},
            "meta_info": dict(self.meta_info),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DataProto":
        return cls(payload.get("batch"), payload.get("non_tensor_batch"), payload.get("meta_info"))


def pad_dataproto_to_divisor(data: DataProto, divisor: int) -> tuple[DataProto, int]:
    if divisor <= 0:
        raise ValueError("divisor must be positive")
    if len(data) == 0:
        return data, 0
    if len(data) % divisor == 0:
        return data, 0
    pad = divisor - len(data) % divisor
    # Repeat the first ``pad`` samples; callers must drop the tail after collect.
    indices = np.concatenate([np.arange(len(data)), np.arange(pad)])
    padded = data.select_idxs(indices)
    # The duplicates only exist to satisfy the equal-chunk invariant.  Their
    # loss masks must be cleared, otherwise the numerator counts them while the
    # denominator (computed before padding) does not, inflating the gradient.
    for key in ("response_mask", "chosen_mask", "rejected_mask"):
        if key in padded.batch:
            padded.batch[key] = padded.batch[key].copy()
            padded.batch[key][-pad:] = 0.0
    # Pair-level losses (DPO) do not look at response_mask, so expose a
    # sample-level mask as well.
    loss_mask = np.ones(len(padded), dtype=np.float32)
    loss_mask[-pad:] = 0.0
    padded.batch["loss_mask"] = loss_mask
    padded.meta_info["_padding_size"] = pad
    return padded, pad


def unpad_dataproto(data: DataProto, pad: int) -> DataProto:
    if pad <= 0:
        return data
    return data[: len(data) - pad]
