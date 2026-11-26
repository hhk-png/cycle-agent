"""MiniFlink 的基础数据模型。

这个模块刻意只依赖 Python 标准库。它定义流元素、时间、窗口与输出标签，
后面的运行时、算子和连接器都围绕这些不可变对象工作。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")

MAX_WATERMARK = (1 << 63) - 1
MIN_WATERMARK = -(1 << 63)


@dataclass(frozen=True, slots=True)
class Duration:
    """毫秒精度的时间长度，对应 Flink 的 ``Time``/``Duration``。"""

    millis: int

    def __post_init__(self) -> None:
        if self.millis < 0:
            raise ValueError("Duration 不能为负数")

    @classmethod
    def of_millis(cls, millis: int) -> "Duration":
        return cls(int(millis))

    @classmethod
    def of_seconds(cls, seconds: float) -> "Duration":
        return cls(int(seconds * 1000))

    @classmethod
    def of_minutes(cls, minutes: float) -> "Duration":
        return cls.of_seconds(minutes * 60)

    @classmethod
    def of_hours(cls, hours: float) -> "Duration":
        return cls.of_minutes(hours * 60)

    def to_millis(self) -> int:
        return self.millis

    def __add__(self, other: "Duration") -> "Duration":
        return Duration(self.millis + other.millis)

    def __mul__(self, factor: int) -> "Duration":
        return Duration(self.millis * factor)

    __rmul__ = __mul__

    def __str__(self) -> str:
        return f"{self.millis}ms"


ZERO = Duration(0)


@dataclass(frozen=True, slots=True)
class Record:
    """流中的一条数据。

    ``timestamp`` 是事件时间；``key`` 在 keyBy 之后由运行时填写，供分区、
    键控状态和窗口使用。生产 Flink 中 key 属于 ``StreamRecord`` 的内部字段，
    这里为了让教学实现更直观而显式保留。
    """

    value: Any
    timestamp: int | None = None
    key: Any = None
    headers: dict[str, str] = field(default_factory=dict, compare=False)

    def with_value(self, value: Any) -> "Record":
        return Record(value, self.timestamp, self.key, dict(self.headers))

    def with_key(self, key: Any) -> "Record":
        return Record(self.value, self.timestamp, key, dict(self.headers))


@dataclass(frozen=True, slots=True)
class Watermark:
    """事件时间水位线，表示“时间小于该值的记录已经到齐”。"""

    timestamp: int

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, int):
            raise TypeError("watermark 必须是整数毫秒")

    def __repr__(self) -> str:
        if self.timestamp == MAX_WATERMARK:
            return "Watermark(+INF)"
        return f"Watermark({self.timestamp})"


@dataclass(frozen=True, slots=True)
class Barrier:
    """检查点屏障。

    MiniFlink 的本地运行时采用微批同步快照，不真正传播 Barrier；保留该类型
    是为了在扩展练习和测试中显式表达 Flink 的屏障语义。
    """

    checkpoint_id: int


@dataclass(frozen=True, slots=True)
class EndOfStream:
    """某个输入通道结束。多输入算子需要所有通道都结束后才能 close。"""


@dataclass(frozen=True, slots=True)
class SideOutputRecord:
    """算子返回的、带标签的侧输出记录。

    与 ``RuntimeContext.emit_side_output`` 不同，它显式携带产生它的记录
    时间戳和 key，避免定时器回调产生的侧输出被错误归到“最后一条数据记录”。
    """

    tag: str
    value: Any
    timestamp: int | None = None
    key: Any = None


StreamElement = Record | Watermark | Barrier | EndOfStream | SideOutputRecord


@dataclass(frozen=True, slots=True, order=True)
class Window(Generic[T]):
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("窗口结束时间不能早于开始时间")

    def contains(self, timestamp: int) -> bool:
        return self.start <= timestamp < self.end

    def overlap(self, other: "Window") -> bool:
        return self.start < other.end and other.start < self.end

    def merge(self, other: "Window") -> "Window":
        return Window(min(self.start, other.start), max(self.end, other.end))

    def __str__(self) -> str:
        return f"[{self.start}, {self.end})"


@dataclass(frozen=True, slots=True)
class WindowResult:
    """窗口算子的默认输出。"""

    key: Any
    window: Window
    value: Any


@dataclass(frozen=True, slots=True)
class OutputTag:
    """侧输出标签。"""

    id: str
    type_name: str = "generic"

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("OutputTag 的 id 不能为空")


@dataclass(frozen=True, slots=True)
class Timer:
    timestamp: int
    key: Any


class Collector(Generic[T]):
    """用户函数向运行时发射数据的收集器。"""

    __slots__ = ("_values",)

    def __init__(self) -> None:
        self._values: list[T] = []

    def collect(self, value: T) -> None:
        self._values.append(value)

    def collect_all(self, values: list[T]) -> None:
        self._values.extend(values)

    def get_values(self) -> list[T]:
        return list(self._values)

    def clear(self) -> None:
        self._values.clear()

    def __len__(self) -> int:
        return len(self._values)


def new_id(prefix: str = "op") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class UnsupportedOperationError(NotImplementedError):
    """MiniFlink 有意不支持、并且文档中明确标注的能力。"""


class JobExecutionError(RuntimeError):
    """作业执行失败。"""
