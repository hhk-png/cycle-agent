"""Input normalization shared by training and scoring.

MLflow models accept dataframes, arrays, dictionaries and lists of records.
MiniMLflow intentionally supports a small subset:

* ``list[dict]`` (records)
* ``dict[str, list]`` (columnar data)
* ``dict[str, scalar]`` (a single record)
* ``list[list]`` when feature names are known (e.g. a saved model)
* objects exposing ``.tolist()`` (NumPy arrays, without importing NumPy)

Anything else fails with a useful error.  Keeping one normalizer prevents the
classic bug where training and serving interpret columns in different orders.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..errors import InvalidParameter


def _maybe_tolist(data: Any) -> Any:
    if hasattr(data, "tolist") and not isinstance(data, (list, tuple, dict)):
        try:
            return data.tolist()
        except Exception:  # noqa: BLE001
            return data
    return data


def normalize_rows(
    data: Any,
    *,
    feature_names: Sequence[str] | None = None,
    require_numeric: bool = True,
) -> tuple[list[str], list[list[Any]]]:
    data = _maybe_tolist(data)
    names: list[str] = list(feature_names) if feature_names else []
    rows: list[list[Any]] = []

    if isinstance(data, dict):
        values = list(data.values())
        scalar_columns = all(not isinstance(value, (list, tuple, dict)) for value in values)
        if scalar_columns:
            names = list(data.keys())
            rows = [[data[name] for name in names]]
        else:
            names = list(data.keys())
            columns = []
            for name in names:
                column = _maybe_tolist(data[name])
                if not isinstance(column, (list, tuple)):
                    raise InvalidParameter(f"column {name!r} must be a sequence")
                columns.append(list(column))
            lengths = {len(column) for column in columns}
            if len(lengths) > 1:
                raise InvalidParameter("all columns must have the same length")
            if not columns:
                return [], []
            rows = [list(row) for row in zip(*columns)]
    elif isinstance(data, (list, tuple)):
        if not data:
            return names, []
        if all(isinstance(item, dict) for item in data):
            if not names:
                for record in data:
                    for key in record:
                        if key not in names:
                            names.append(str(key))
            rows = []
            for index, record in enumerate(data):
                missing = [name for name in names if name not in record]
                if missing:
                    raise InvalidParameter(f"record {index} is missing columns: {missing}")
                rows.append([record[name] for name in names])
        elif all(isinstance(item, (list, tuple)) for item in data):
            if not names:
                width = len(data[0])
                names = [f"x{i}" for i in range(width)]
            width = len(names)
            for index, row in enumerate(data):
                if len(row) != width:
                    raise InvalidParameter(
                        f"row {index} has {len(row)} values, expected {width} feature columns"
                    )
                rows.append(list(row))
        else:
            raise InvalidParameter("list input must contain either dict records or list rows")
    else:
        raise InvalidParameter(
            "unsupported model input; expected list[dict], dict of columns, or list[list]"
        )

    if feature_names and names != list(feature_names):
        # A columnar dict may be in any order; re-order by the model's schema.
        lookup = {name: index for index, name in enumerate(names)}
        missing = [name for name in feature_names if name not in lookup]
        if missing:
            raise InvalidParameter(f"input is missing required columns: {missing}")
        rows = [[row[lookup[name]] for name in feature_names] for row in rows]
        names = list(feature_names)

    if require_numeric:
        converted: list[list[float]] = []
        for row_index, row in enumerate(rows):
            numeric_row: list[float] = []
            for column_index, value in enumerate(row):
                if isinstance(value, bool):
                    numeric_row.append(1.0 if value else 0.0)
                    continue
                try:
                    numeric_row.append(float(value))
                except (TypeError, ValueError) as exc:
                    raise InvalidParameter(
                        f"value at row {row_index}, column {names[column_index]!r} is not numeric: {value!r}"
                    ) from exc
            converted.append(numeric_row)
        return names, converted
    return names, rows


def normalize_targets(targets: Any) -> list[Any]:
    targets = _maybe_tolist(targets)
    if isinstance(targets, dict):
        if len(targets) != 1:
            raise InvalidParameter("target dict must contain exactly one column")
        targets = next(iter(targets.values()))
    if not isinstance(targets, (list, tuple)):
        raise InvalidParameter("targets must be a 1-D sequence")
    return list(targets)
