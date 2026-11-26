"""Model signatures: infer, serialize, and enforce input schemas.

A signature is a contract.  It is not a replacement for data validation in the
application, but it catches the most expensive integration failure early: a
silent column reorder or a type change at the serving boundary.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..errors import InvalidParameter
from .data import normalize_rows

_ALLOWED_TYPES = {"boolean", "long", "double", "string", "binary", "datetime"}


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidParameter("signature column name must not be empty")
        if self.type not in _ALLOWED_TYPES:
            raise InvalidParameter(f"unsupported signature type {self.type!r}")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "type": self.type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ColumnSpec":
        return cls(name=str(data["name"]), type=str(data["type"]))


@dataclass(frozen=True)
class ModelSignature:
    inputs: tuple[ColumnSpec, ...] = field(default_factory=tuple)
    outputs: tuple[ColumnSpec, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": [item.to_dict() for item in self.outputs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ModelSignature | None":
        if data is None:
            return None
        inputs = tuple(ColumnSpec.from_dict(item) for item in (data.get("inputs") or []))
        outputs = tuple(ColumnSpec.from_dict(item) for item in (data.get("outputs") or []))
        return cls(inputs=inputs, outputs=outputs)


def _infer_value_type(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (bytes, bytearray)):
        return "binary"
    if isinstance(value, (_dt.datetime, _dt.date)):
        return "datetime"
    return "string"


def _merge_types(types: list[str]) -> str:
    if not types:
        return "string"
    unique = set(types)
    if unique <= {"long", "double"}:
        return "double" if "double" in unique else "long"
    if len(unique) == 1:
        return types[0]
    return "string"


def _infer_columns(records: list[dict[str, Any]]) -> list[ColumnSpec]:
    names: list[str] = []
    for record in records:
        for key in record:
            if key not in names:
                names.append(str(key))
    specs: list[ColumnSpec] = []
    for name in names:
        observed = [_infer_value_type(record.get(name)) for record in records if name in record]
        specs.append(ColumnSpec(name=name, type=_merge_types([item for item in observed if item])))
    return specs


def infer_signature(input_example: Any, output_example: Any = None) -> ModelSignature:
    """Infer a pragmatic signature from an example.

    Input may be records or a columnar dict.  Missing values do not force a
    column to string if another row provides a type.  Mixed int/float becomes
    ``double``; incompatible types become ``string`` (an explicit escape hatch
    that keeps the contract useful without pretending to infer a union type).
    """
    records: list[dict[str, Any]]
    if isinstance(input_example, dict):
        _names, rows = normalize_rows(input_example, require_numeric=False)
        names = list(input_example.keys())
        records = [dict(zip(names, row)) for row in rows]
    elif isinstance(input_example, list) and all(isinstance(item, dict) for item in input_example):
        records = [dict(item) for item in input_example]
    else:
        _names, rows = normalize_rows(input_example, require_numeric=False)
        records = [dict(zip(_names, row)) for row in rows]
    inputs = tuple(_infer_columns(records))
    outputs = ()
    if output_example is not None:
        values = output_example
        if hasattr(values, "tolist"):
            try:
                values = values.tolist()
            except Exception:  # noqa: BLE001
                pass
        if isinstance(values, (list, tuple)):
            flattened = list(values)
            if flattened and isinstance(flattened[0], (list, tuple)):
                flattened = [item for row in flattened for item in row]
            types = [_infer_value_type(item) for item in flattened]
            outputs = (ColumnSpec("prediction", _merge_types([t for t in types if t])),)
        else:
            outputs = (ColumnSpec("prediction", _infer_value_type(values) or "string"),)
    return ModelSignature(inputs=inputs, outputs=outputs)


def validate_input(signature: ModelSignature | None, data: Any) -> None:
    if signature is None or not signature.inputs:
        return
    columns = [spec.name for spec in signature.inputs]
    # Supplying feature_names lets positional rows (list[list]) be validated
    # against the signature rather than rejected for lacking column names.
    _names, rows = normalize_rows(data, feature_names=columns, require_numeric=False)
    positions = {name: index for index, name in enumerate(columns)}
    for row_index, row in enumerate(rows):
        for spec in signature.inputs:
            value = row[positions[spec.name]]
            if value is None:
                continue
            _check_type(spec, value, row_index)


def _check_type(spec: ColumnSpec, value: Any, row_index: int) -> None:
    ok = False
    if spec.type == "boolean":
        ok = isinstance(value, bool)
    elif spec.type == "long":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif spec.type == "double":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif spec.type == "string":
        ok = isinstance(value, str)
    elif spec.type == "binary":
        ok = isinstance(value, (bytes, bytearray))
    elif spec.type == "datetime":
        ok = isinstance(value, (_dt.datetime, _dt.date, str))
    if not ok:
        raise InvalidParameter(
            f"column {spec.name!r} expects {spec.type}, got {type(value).__name__} at row {row_index}"
        )
