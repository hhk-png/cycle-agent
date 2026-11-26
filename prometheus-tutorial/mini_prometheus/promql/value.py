"""PromQL result types.

``Scalar``/``StringValue`` are single values; ``Vector`` is an instant vector
(one sample per series); ``Matrix`` is a range vector.  Every vector item
carries both the *evaluation* timestamp (what the API returns) and the
*source* timestamp (what ``timestamp()`` reports).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..labels import Labels


class Value:
    type: str


@dataclass(slots=True)
class Scalar(Value):
    value: float

    type = "scalar"

    def __bool__(self) -> bool:
        return True


@dataclass(slots=True)
class StringValue(Value):
    value: str

    type = "string"

    def __bool__(self) -> bool:
        return True


@dataclass(slots=True)
class VectorItem:
    labels: Labels
    value: float
    timestamp: int
    source_timestamp: int | None = None

    def __post_init__(self) -> None:
        if self.source_timestamp is None:
            self.source_timestamp = self.timestamp

    def with_value(self, value: float, *, drop_name: bool = False) -> "VectorItem":
        labels = self.labels
        if drop_name:
            labels = labels.without("__name__")
        return VectorItem(labels, value, self.timestamp, self.source_timestamp)


@dataclass(slots=True)
class Vector(Value):
    items: list[VectorItem] = field(default_factory=list)

    type = "vector"

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return True


@dataclass(slots=True)
class Series:
    labels: Labels
    samples: list[tuple[int, float]] = field(default_factory=list)

    type = "series"


@dataclass(slots=True)
class Matrix(Value):
    series: list[Series] = field(default_factory=list)
    range_ms: int = 0
    start_time: int = 0
    end_time: int = 0

    type = "matrix"

    def __len__(self) -> int:
        return len(self.series)

    def __bool__(self) -> bool:
        return True


@dataclass
class QueryResult:
    value: Value
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def result_type(self) -> str:
        return self.value.type
