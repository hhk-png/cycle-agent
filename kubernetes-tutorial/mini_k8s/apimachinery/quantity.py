"""Parse and compare Kubernetes resource quantities.

Kubernetes distinguishes decimal and binary suffixes.  CPU is normally written
as millicores (``250m``), memory as bytes with a binary suffix (``512Mi``).
This module normalises both to floats where one CPU core == 1.0.
"""

from __future__ import annotations

import math
import re

_QUANTITY = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))([a-zA-Z]*)$")

_DECIMAL = {
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "": 1.0,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
}

_BINARY = {
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}


def parse_quantity(value: str | int | float) -> float:
    if isinstance(value, bool):
        raise ValueError("a boolean is not a quantity")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("quantity must be finite")
        return float(value)
    text = str(value).strip()
    if text in ("0", "+0", "-0"):
        return 0.0
    if re.match(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+$", text):
        return float(text)
    match = _QUANTITY.match(text)
    if not match:
        raise ValueError(f"invalid quantity: {value!r}")
    number, suffix = match.groups()
    if suffix in _BINARY:
        return float(number) * _BINARY[suffix]
    if suffix in _DECIMAL:
        return float(number) * _DECIMAL[suffix]
    # Kubernetes also accepts exponent notation, e.g. 1e3.
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"invalid quantity: {value!r}") from exc


def format_quantity(value: float, *, resource: str = "") -> str:
    if resource == "cpu":
        millis = int(round(value * 1000))
        return f"{millis}m" if millis % 1000 else str(millis // 1000)
    units = [(2**60, "Ei"), (2**50, "Pi"), (2**40, "Ti"), (2**30, "Gi"), (2**20, "Mi"), (2**10, "Ki")]
    for factor, suffix in units:
        if value >= factor and value % factor == 0:
            return f"{int(value // factor)}{suffix}"
    return str(int(value))


def quantity_add(*values: str | float) -> float:
    return sum(parse_quantity(v) for v in values)


def quantity_leq(left: str | float, right: str | float) -> bool:
    return parse_quantity(left) <= parse_quantity(right) + 1e-12


def quantity_equal(left: str | float, right: str | float) -> bool:
    return math.isclose(parse_quantity(left), parse_quantity(right), rel_tol=1e-9, abs_tol=1e-9)


def add_resource_maps(*maps: dict[str, str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for mapping in maps:
        for key, value in (mapping or {}).items():
            result[key] = result.get(key, 0.0) + parse_quantity(value)
    return result


def resource_map_leq(left: dict[str, float], right: dict[str, float]) -> bool:
    """True when every resource in *left* fits within *right*."""
    for key, value in left.items():
        if value > right.get(key, 0.0) + 1e-12:
            return False
    return True
