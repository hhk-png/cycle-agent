"""A dependency-free linear regression flavor used by the tutorial.

It is deliberately small, but not a toy in one important sense: it implements
the same lifecycle as a real flavor -- fit, predict, serialize parameters,
reload, and expose metadata for autologging.  The closed-form ridge solution
is solved with Gaussian elimination so the tutorial does not require NumPy.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Sequence

from ..errors import InvalidParameter
from .data import normalize_rows, normalize_targets

_FIT_CALLBACKS: list[Callable[..., None]] = []


def register_fit_callback(callback: Callable[..., None]) -> None:
    if callback not in _FIT_CALLBACKS:
        _FIT_CALLBACKS.append(callback)


def unregister_fit_callback(callback: Callable[..., None]) -> None:
    if callback in _FIT_CALLBACKS:
        _FIT_CALLBACKS.remove(callback)


def clear_fit_callbacks() -> None:
    _FIT_CALLBACKS.clear()


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve A x = b with partial-pivot Gaussian elimination."""
    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot_row = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot_row][column]) < 1e-12:
            raise InvalidParameter(
                "design matrix is singular; add regularization (alpha > 0), "
                "remove collinear features, or add more rows"
            )
        augmented[column], augmented[pivot_row] = augmented[pivot_row], augmented[column]
        pivot = augmented[column][column]
        for item in range(column, size + 1):
            augmented[column][item] /= pivot
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0:
                continue
            for item in range(column, size + 1):
                augmented[row][item] -= factor * augmented[column][item]
    return [augmented[index][size] for index in range(size)]


class LinearRegression:
    def __init__(self, *, fit_intercept: bool = True, alpha: float = 0.0) -> None:
        if not isinstance(fit_intercept, bool):
            raise InvalidParameter("fit_intercept must be a boolean")
        try:
            alpha_value = float(alpha)
        except (TypeError, ValueError) as exc:
            raise InvalidParameter("alpha must be a non-negative number") from exc
        if not math.isfinite(alpha_value) or alpha_value < 0:
            raise InvalidParameter("alpha must be a finite, non-negative number")
        self.fit_intercept = fit_intercept
        self.alpha = alpha_value
        self.coef_: list[float] = []
        self.intercept_: float = 0.0
        self.feature_names_: list[str] = []
        self.n_features_in_: int = 0
        self.training_r2_: float | None = None
        self.training_rmse_: float | None = None

    def get_params(self) -> dict[str, Any]:
        return {"fit_intercept": self.fit_intercept, "alpha": self.alpha}

    def fit(self, X: Any, y: Any) -> "LinearRegression":
        names, rows = normalize_rows(X, require_numeric=True)
        targets = normalize_targets(y)
        if not rows:
            raise InvalidParameter("cannot fit on an empty dataset")
        if len(rows) != len(targets):
            raise InvalidParameter(f"X has {len(rows)} rows but y has {len(targets)} targets")
        numeric_targets = []
        for index, value in enumerate(targets):
            if isinstance(value, bool):
                raise InvalidParameter(f"target at index {index} must be numeric")
            try:
                numeric_targets.append(float(value))
            except (TypeError, ValueError) as exc:
                raise InvalidParameter(f"target at index {index} is not numeric: {value!r}") from exc
        width = len(rows[0])
        if width == 0:
            raise InvalidParameter("at least one feature is required")
        if any(len(row) != width for row in rows):
            raise InvalidParameter("all rows must have the same number of features")
        design = [[1.0, *row] if self.fit_intercept else list(row) for row in rows]
        n_columns = width + (1 if self.fit_intercept else 0)
        xtx = [[0.0] * n_columns for _ in range(n_columns)]
        xty = [0.0] * n_columns
        for row, target in zip(design, numeric_targets):
            for i in range(n_columns):
                xty[i] += row[i] * target
                for j in range(n_columns):
                    xtx[i][j] += row[i] * row[j]
        if self.alpha:
            for index in range(n_columns):
                if self.fit_intercept and index == 0:
                    continue
                xtx[index][index] += self.alpha
        solution = _solve(xtx, xty)
        if self.fit_intercept:
            self.intercept_ = solution[0]
            self.coef_ = solution[1:]
        else:
            self.intercept_ = 0.0
            self.coef_ = solution
        self.feature_names_ = names
        self.n_features_in_ = width
        predictions = self.predict(X)
        self.training_rmse_ = math.sqrt(
            sum((prediction - target) ** 2 for prediction, target in zip(predictions, numeric_targets))
            / len(numeric_targets)
        )
        mean_target = sum(numeric_targets) / len(numeric_targets)
        denominator = sum((target - mean_target) ** 2 for target in numeric_targets)
        numerator = sum((prediction - target) ** 2 for prediction, target in zip(predictions, numeric_targets))
        self.training_r2_ = 1.0 - numerator / denominator if denominator else (1.0 if numerator == 0 else 0.0)
        for callback in list(_FIT_CALLBACKS):
            callback(self, X, y, {
                "training_rmse": self.training_rmse_,
                "training_r2": self.training_r2_,
            })
        return self

    def predict(self, X: Any) -> list[float]:
        if not self.coef_ and self.n_features_in_ == 0:
            raise InvalidParameter("model is not fitted; call fit() or load a fitted model")
        names, rows = normalize_rows(X, feature_names=self.feature_names_, require_numeric=True)
        if names != self.feature_names_:
            raise InvalidParameter("feature order mismatch")
        if rows and len(rows[0]) != self.n_features_in_:
            raise InvalidParameter(
                f"model expects {self.n_features_in_} features, got {len(rows[0])}"
            )
        return [self.intercept_ + sum(coef * value for coef, value in zip(self.coef_, row)) for row in rows]

    def score(self, X: Any, y: Any) -> float:
        points = self.predict(X)
        targets = normalize_targets(y)
        if len(points) != len(targets):
            raise InvalidParameter("prediction and target lengths differ")
        numeric_targets = [float(item) for item in targets]
        mean_target = sum(numeric_targets) / len(numeric_targets)
        denominator = sum((target - mean_target) ** 2 for target in numeric_targets)
        numerator = sum((point - target) ** 2 for point, target in zip(points, numeric_targets))
        return 1.0 - numerator / denominator if denominator else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": "linear_regression",
            "fit_intercept": self.fit_intercept,
            "alpha": self.alpha,
            "coef": list(self.coef_),
            "intercept": self.intercept_,
            "feature_names": list(self.feature_names_),
            "n_features_in": self.n_features_in_,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LinearRegression":
        if data.get("model_type") != "linear_regression":
            raise InvalidParameter("serialized object is not a linear_regression model")
        model = cls(fit_intercept=bool(data.get("fit_intercept", True)), alpha=float(data.get("alpha", 0.0)))
        model.coef_ = [float(item) for item in data.get("coef", [])]
        model.intercept_ = float(data.get("intercept", 0.0))
        model.feature_names_ = [str(item) for item in data.get("feature_names", [])]
        model.n_features_in_ = int(data.get("n_features_in", len(model.coef_)))
        if model.n_features_in_ != len(model.coef_):
            raise InvalidParameter("invalid model: n_features_in does not match coefficient count")
        return model
