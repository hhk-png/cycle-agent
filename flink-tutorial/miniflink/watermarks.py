"""时间戳分配与 watermark 生成策略。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .core import MAX_WATERMARK, Duration, EndOfStream, Record, StreamElement, Watermark


class TimestampAssigner:
    def extract_timestamp(self, value: Any, record_timestamp: int | None = None) -> int:
        raise NotImplementedError


class LambdaTimestampAssigner(TimestampAssigner):
    def __init__(self, function: Callable[[Any], int]) -> None:
        self.function = function

    def extract_timestamp(self, value: Any, record_timestamp: int | None = None) -> int:
        return int(self.function(value))


class RecordTimestampAssigner(TimestampAssigner):
    def extract_timestamp(self, value: Any, record_timestamp: int | None = None) -> int:
        if record_timestamp is not None:
            return record_timestamp
        if isinstance(value, Record) and value.timestamp is not None:
            return value.timestamp
        timestamp = getattr(value, "timestamp", None)
        if timestamp is None:
            raise ValueError(
                "记录没有时间戳；请使用 with_timestamp_assigner 指定事件时间字段"
            )
        return int(timestamp)


@dataclass
class WatermarkStrategy:
    """与 Flink ``WatermarkStrategy`` 对齐的最小实现。"""

    timestamp_assigner: TimestampAssigner | None = None
    out_of_orderness_ms: int = 0
    idleness_ms: int | None = None
    periodic_interval_records: int = 1
    emit_watermarks: bool = True

    @classmethod
    def for_monotonous_timestamps(cls) -> "WatermarkStrategy":
        return cls(timestamp_assigner=RecordTimestampAssigner(), out_of_orderness_ms=0)

    @classmethod
    def for_bounded_out_of_orderness(
        cls,
        delay: Duration | int,
        timestamp_assigner: TimestampAssigner | Callable[[Any], int] | None = None,
    ) -> "WatermarkStrategy":
        delay_ms = delay.millis if isinstance(delay, Duration) else int(delay)
        if delay_ms < 0:
            raise ValueError("乱序延迟不能为负数")
        resolved: TimestampAssigner
        if timestamp_assigner is None:
            resolved = RecordTimestampAssigner()
        elif isinstance(timestamp_assigner, TimestampAssigner):
            resolved = timestamp_assigner
        else:
            resolved = LambdaTimestampAssigner(timestamp_assigner)
        return cls(timestamp_assigner=resolved, out_of_orderness_ms=delay_ms)

    @classmethod
    def no_watermarks(cls) -> "WatermarkStrategy":
        return cls(emit_watermarks=False, timestamp_assigner=RecordTimestampAssigner())

    def with_timestamp_assigner(self, assigner: TimestampAssigner | Callable[[Any], int]) -> "WatermarkStrategy":
        resolved = assigner if isinstance(assigner, TimestampAssigner) else LambdaTimestampAssigner(assigner)
        return WatermarkStrategy(
            timestamp_assigner=resolved,
            out_of_orderness_ms=self.out_of_orderness_ms,
            idleness_ms=self.idleness_ms,
            periodic_interval_records=self.periodic_interval_records,
            emit_watermarks=self.emit_watermarks,
        )

    def with_idleness(self, idleness: Duration | int) -> "WatermarkStrategy":
        idleness_ms = idleness.millis if isinstance(idleness, Duration) else int(idleness)
        return WatermarkStrategy(
            timestamp_assigner=self.timestamp_assigner or RecordTimestampAssigner(),
            out_of_orderness_ms=self.out_of_orderness_ms,
            idleness_ms=idleness_ms,
            periodic_interval_records=self.periodic_interval_records,
            emit_watermarks=self.emit_watermarks,
        )


class WatermarkGenerator:
    def __init__(self, strategy: WatermarkStrategy) -> None:
        self.strategy = strategy
        self.max_timestamp = -(1 << 63)
        self.last_watermark = -(1 << 63)
        self.records_since_emission = 0

    def on_record(self, timestamp: int) -> int | None:
        self.max_timestamp = max(self.max_timestamp, timestamp)
        self.records_since_emission += 1
        if not self.strategy.emit_watermarks:
            return None
        if self.records_since_emission < self.strategy.periodic_interval_records:
            return None
        self.records_since_emission = 0
        watermark = self.max_timestamp - self.strategy.out_of_orderness_ms
        if watermark > self.last_watermark:
            self.last_watermark = watermark
            return watermark
        return None

    def on_periodic(self) -> int | None:
        if not self.strategy.emit_watermarks:
            return None
        watermark = self.max_timestamp - self.strategy.out_of_orderness_ms
        if self.max_timestamp > -(1 << 63) and watermark > self.last_watermark:
            self.last_watermark = watermark
            return watermark
        return None

    def snapshot(self) -> dict[str, int]:
        return {
            "max": self.max_timestamp,
            "last": self.last_watermark,
            "count": self.records_since_emission,
        }

    def restore(self, snapshot: dict[str, int]) -> None:
        self.max_timestamp = snapshot.get("max", -(1 << 63))
        self.last_watermark = snapshot.get("last", -(1 << 63))
        self.records_since_emission = snapshot.get("count", 0)


class TimestampAssignerOperator:
    """在流中间分配时间戳的算子（对应 ``assignTimestampsAndWatermarks``）。"""

    def __init__(self, strategy: WatermarkStrategy) -> None:
        self.strategy = strategy
        self.generator = WatermarkGenerator(strategy)
        self.assigner = strategy.timestamp_assigner or RecordTimestampAssigner()
        self._context: Any = None
        self._ended = False

    def open(self, context: Any) -> None:
        self._context = context

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        timestamp = self.assigner.extract_timestamp(record.value, record.timestamp)
        emitted = Record(record.value, timestamp, record.key, dict(record.headers))
        output: list[StreamElement] = [emitted]
        watermark = self.generator.on_record(timestamp)
        if watermark is not None:
            output.append(Watermark(watermark))
        return output

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        return [Watermark(watermark)]

    def end_of_input(self) -> list[StreamElement]:
        if self._ended:
            return []
        self._ended = True
        return [Watermark(MAX_WATERMARK), EndOfStream()]

    def poll(self) -> list[StreamElement]:
        return []

    def snapshot(self) -> dict[str, Any]:
        return {"generator": self.generator.snapshot()}

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.generator.restore(snapshot.get("generator", {}))
