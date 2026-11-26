"""窗口分配器、触发器、驱逐器与内置聚合函数。"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Iterable, TypeVar

from .core import MAX_WATERMARK, Collector, Duration, Record, Window, WindowResult
from .functions import AggregateFunction, RuntimeContext

T = TypeVar("T")
R = TypeVar("R")
ACC = TypeVar("ACC")


class WindowAssigner:
    def assign_windows(
        self, value: Any, timestamp: int, context: RuntimeContext | None = None
    ) -> list[Window]:
        raise NotImplementedError

    @property
    def is_event_time(self) -> bool:
        return True

    @property
    def is_session(self) -> bool:
        return False


class TumblingEventTimeWindows(WindowAssigner):
    def __init__(self, size: Duration, offset: int = 0) -> None:
        if size.millis <= 0:
            raise ValueError("窗口大小必须大于 0")
        self.size = size.millis
        self.offset = offset

    @classmethod
    def of(cls, size: Duration, offset: int = 0) -> "TumblingEventTimeWindows":
        return cls(size, offset)

    def assign_windows(self, value: Any, timestamp: int, context: RuntimeContext | None = None) -> list[Window]:
        start = timestamp - ((timestamp - self.offset) % self.size)
        return [Window(start, start + self.size)]

    def __repr__(self) -> str:
        return f"TumblingEventTimeWindows({self.size}ms)"


class TumblingProcessingTimeWindows(TumblingEventTimeWindows):
    @property
    def is_event_time(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"TumblingProcessingTimeWindows({self.size}ms)"


class SlidingEventTimeWindows(WindowAssigner):
    def __init__(self, size: Duration, slide: Duration, offset: int = 0) -> None:
        if size.millis <= 0 or slide.millis <= 0:
            raise ValueError("窗口大小和滑动步长必须大于 0")
        if slide.millis > size.millis:
            raise ValueError("滑动步长不能大于窗口大小")
        self.size = size.millis
        self.slide = slide.millis
        self.offset = offset

    @classmethod
    def of(cls, size: Duration, slide: Duration, offset: int = 0) -> "SlidingEventTimeWindows":
        return cls(size, slide, offset)

    def assign_windows(self, value: Any, timestamp: int, context: RuntimeContext | None = None) -> list[Window]:
        last_start = timestamp - ((timestamp - self.offset) % self.slide)
        # Flink 的滑动窗口只包含“窗口区间覆盖该时间戳”的窗口，即
        # start <= timestamp < start + size。不能简单用固定步数迭代，
        # 否则 size 不是 slide 的整数倍时会多算一个不包含该记录的窗口。
        starts: list[int] = []
        start = last_start
        while start > timestamp - self.size:
            starts.append(start)
            start -= self.slide
        return [Window(start, start + self.size) for start in starts]

    def __repr__(self) -> str:
        return f"SlidingEventTimeWindows({self.size}ms, {self.slide}ms)"


class SlidingProcessingTimeWindows(SlidingEventTimeWindows):
    @property
    def is_event_time(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"SlidingProcessingTimeWindows({self.size}ms, {self.slide}ms)"


class SessionWindows(WindowAssigner):
    def __init__(self, gap: Duration) -> None:
        if gap.millis <= 0:
            raise ValueError("会话间隔必须大于 0")
        self.gap = gap.millis

    @classmethod
    def with_gap(cls, gap: Duration) -> "SessionWindows":
        return cls(gap)

    @property
    def is_session(self) -> bool:
        return True

    def assign_windows(self, value: Any, timestamp: int, context: RuntimeContext | None = None) -> list[Window]:
        return [Window(timestamp, timestamp + self.gap)]


class GlobalWindows(WindowAssigner):
    def assign_windows(self, value: Any, timestamp: int, context: RuntimeContext | None = None) -> list[Window]:
        return [Window(0, MAX_WATERMARK)]

    @property
    def is_event_time(self) -> bool:
        # 与 Flink 一致：GlobalWindows.isEventTime() 为 false；
        # 默认 trigger 是 NeverTrigger，必须显式配置 trigger 才会有输出。
        return False


class TriggerResult(str, Enum):
    CONTINUE = "continue"
    FIRE = "fire"
    PURGE = "purge"
    FIRE_AND_PURGE = "fire_and_purge"


class Trigger:
    def on_element(self, value: Any, timestamp: int, window: Window, count: int) -> TriggerResult:
        return TriggerResult.CONTINUE

    def on_event_time(self, watermark: int, window: Window) -> TriggerResult:
        return TriggerResult.CONTINUE

    def on_processing_time(self, now: int, window: Window) -> TriggerResult:
        return TriggerResult.CONTINUE


class NeverTrigger(Trigger):
    """与 Flink ``GlobalWindows`` 的默认 trigger 一致：永远不触发。"""

    @classmethod
    def create(cls) -> "NeverTrigger":
        return cls()


class EventTimeTrigger(Trigger):
    def on_event_time(self, watermark: int, window: Window) -> TriggerResult:
        return TriggerResult.FIRE if watermark >= window.end else TriggerResult.CONTINUE

    @classmethod
    def create(cls) -> "EventTimeTrigger":
        return cls()


class ProcessingTimeTrigger(Trigger):
    def on_processing_time(self, now: int, window: Window) -> TriggerResult:
        return TriggerResult.FIRE if now >= window.end else TriggerResult.CONTINUE

    @classmethod
    def create(cls) -> "ProcessingTimeTrigger":
        return cls()


class CountTrigger(Trigger):
    def __init__(self, count: int) -> None:
        if count <= 0:
            raise ValueError("count 必须大于 0")
        self.count = count

    @classmethod
    def of(cls, count: int) -> "CountTrigger":
        return cls(count)

    def on_element(self, value: Any, timestamp: int, window: Window, count: int) -> TriggerResult:
        return TriggerResult.FIRE if count % self.count == 0 else TriggerResult.CONTINUE


class PurgingTrigger(Trigger):
    def __init__(self, wrapped: Trigger) -> None:
        self.wrapped = wrapped

    @classmethod
    def of(cls, trigger: Trigger) -> "PurgingTrigger":
        return cls(trigger)

    def on_element(self, value: Any, timestamp: int, window: Window, count: int) -> TriggerResult:
        return _as_purging(self.wrapped.on_element(value, timestamp, window, count))

    def on_event_time(self, watermark: int, window: Window) -> TriggerResult:
        return _as_purging(self.wrapped.on_event_time(watermark, window))

    def on_processing_time(self, now: int, window: Window) -> TriggerResult:
        return _as_purging(self.wrapped.on_processing_time(now, window))


def _as_purging(result: TriggerResult) -> TriggerResult:
    if result is TriggerResult.FIRE:
        return TriggerResult.FIRE_AND_PURGE
    return result


class Evictor:
    do_evict_after = False

    def evict_before_fire(self, records: list[Any], window: Window) -> list[Any]:
        return records

    def evict_after_fire(self, records: list[Any], window: Window) -> list[Any]:
        return records


class CountEvictor(Evictor):
    def __init__(self, max_count: int, do_evict_after: bool = False) -> None:
        if max_count <= 0:
            raise ValueError("max_count 必须大于 0")
        self.max_count = max_count
        self.do_evict_after = do_evict_after

    @classmethod
    def of(cls, max_count: int) -> "CountEvictor":
        return cls(max_count)

    def evict_before_fire(self, records: list[Any], window: Window) -> list[Any]:
        if self.do_evict_after:
            return records
        return records[-self.max_count :] if len(records) > self.max_count else records

    def evict_after_fire(self, records: list[Any], window: Window) -> list[Any]:
        if not self.do_evict_after:
            return records
        return records[-self.max_count :] if len(records) > self.max_count else records


class TimeEvictor(Evictor):
    def __init__(self, window_size: Duration) -> None:
        self.window_size = window_size.millis

    @classmethod
    def of(cls, window_size: Duration) -> "TimeEvictor":
        return cls(window_size)

    def evict_before_fire(self, records: list[Any], window: Window) -> list[Any]:
        if not records:
            return records
        cutoff = max(getattr(r, "timestamp", 0) or 0 for r in records) - self.window_size
        return [r for r in records if (getattr(r, "timestamp", 0) or 0) >= cutoff]


@dataclass(frozen=True)
class WindowEmission:
    value: Any
    window: Window
    key: Any


@dataclass
class _WindowBucket:
    # 保留 Record 而不是裸 value：Evictor（尤其 TimeEvictor）需要事件时间戳，
    # 而 ProcessWindowFunction 只需要 value。快照会连同时间戳一起恢复。
    records: list[Record] = field(default_factory=list)
    accumulator: Any = None
    count: int = 0
    fired: bool = False
    window_state: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "records": copy.deepcopy(self.records),
            "accumulator": copy.deepcopy(self.accumulator),
            "count": self.count,
            "fired": self.fired,
            "window_state": copy.deepcopy(self.window_state),
        }

    @classmethod
    def restore(cls, snapshot: dict[str, Any]) -> "_WindowBucket":
        return cls(
            records=copy.deepcopy(snapshot.get("records", [])),
            accumulator=copy.deepcopy(snapshot.get("accumulator")),
            count=snapshot.get("count", 0),
            fired=snapshot.get("fired", False),
            window_state=copy.deepcopy(snapshot.get("window_state", {})),
        )


class WindowOperator:
    """窗口算子内核，供 KeyedStream.window(...).aggregate/process 使用。"""

    def __init__(
        self,
        assigner: WindowAssigner,
        *,
        aggregate_function: AggregateFunction | None = None,
        process_window_function: Any = None,
        window_function: Any = None,
        trigger: Trigger | None = None,
        evictor: Evictor | None = None,
        allowed_lateness_ms: int = 0,
        late_output_tag: Any = None,
    ) -> None:
        self.assigner = assigner
        self.aggregate_function = aggregate_function
        self.process_window_function = process_window_function
        self.window_function = window_function
        if trigger is not None:
            self.trigger = trigger
        elif isinstance(assigner, GlobalWindows):
            self.trigger = NeverTrigger()
        elif assigner.is_event_time:
            self.trigger = EventTimeTrigger()
        else:
            self.trigger = ProcessingTimeTrigger()
        self.evictor = evictor
        self.allowed_lateness_ms = allowed_lateness_ms
        self.late_output_tag = late_output_tag
        self._windows: dict[Any, dict[tuple[int, int], _WindowBucket]] = {}
        self._watermark = -(1 << 63)
        self._context: RuntimeContext | None = None

    def open(self, context: RuntimeContext) -> None:
        self._context = context

    def process_record(self, record: Any) -> list[Any]:
        assert self._context is not None
        key = record.key
        if key is None:
            raise RuntimeError("窗口算子必须作用于 keyBy 之后的流")
        timestamp = record.timestamp
        if timestamp is None:
            timestamp = int(time.time() * 1000)
            record = record.__class__(
                record.value, timestamp, record.key, dict(record.headers)
            )
        windows = self.assigner.assign_windows(record.value, timestamp, self._context)
        output: list[Any] = []
        late_windows = [
            window for window in windows if self._is_late_record(key, window)
        ]
        # Flink 只在“该元素所属的所有窗口都已过期”时才把它送入迟到侧输出；
        # 如果元素仍属于某个未过期窗口，它应正常参与该窗口，且不产生侧输出。
        if windows and len(late_windows) == len(windows):
            # 迟到数据默认丢弃；只有显式配置了侧输出才向下游发送。
            # 不要把它堆在算子内部——那会随 checkpoint 无限增长。
            if self.late_output_tag is not None:
                self._context.emit_side_output(self.late_output_tag, record.value)
            output.extend(self._advance_processing_time(timestamp))
            return output
        for window in windows:
            if window in late_windows:
                continue
            bucket = self._get_or_create(key, window)
            if self.assigner.is_session:
                bucket, window = self._merge_sessions(key, window)
            self._add(bucket, record)
            result = self.trigger.on_element(record.value, timestamp, window, bucket.count)
            output.extend(self._handle_trigger(key, window, bucket, result))
            # Flink 的 allowedLateness 语义：窗口已触发但仍在允许迟到范围内时，
            # 新到的迟到记录会更新结果并再次向下游发射。是否发射必须由 trigger
            # 决定——CountTrigger 等在未达到阈值时返回 CONTINUE，不能被这里
            # 无条件“补发”，否则会绕过触发器语义。
            if window.end <= self._watermark < window.end + self.allowed_lateness_ms:
                result = self.trigger.on_event_time(self._watermark, window)
                output.extend(self._handle_trigger(key, window, bucket, result))
        output.extend(self._advance_processing_time(timestamp))
        return output

    def process_watermark(self, watermark: int) -> list[Any]:
        # 处理时间窗口由本地时钟/记录时间推进，不能被事件时间 watermark 清理。
        # GlobalWindows 例外：它的 is_event_time 为 False（与 Flink 一致），
        # 但用户可以显式挂 EventTimeTrigger；此时 watermark 仍应驱动触发。
        if not self.assigner.is_event_time and not isinstance(
            self.assigner, GlobalWindows
        ):
            return []
        if watermark <= self._watermark:
            return []
        self._watermark = watermark
        output: list[Any] = []
        for key in list(self._windows):
            for boundaries in list(self._windows.get(key, {})):
                window = Window(*boundaries)
                bucket = self._windows[key][boundaries]
                effective_end = window.end + self.allowed_lateness_ms
                if watermark >= effective_end:
                    # 即使 watermark 一次跳过整个窗口，也要给 trigger 一次
                    # on_event_time 的机会；但 trigger 返回 CONTINUE 时不能
                    # 强行发射（Flink 在清理时不会绕过 trigger）。
                    if not bucket.fired:
                        result = self.trigger.on_event_time(watermark, window)
                        output.extend(self._handle_trigger(key, window, bucket, result))
                    self._remove(key, window)
                elif watermark >= window.end and not bucket.fired:
                    result = self.trigger.on_event_time(watermark, window)
                    output.extend(self._handle_trigger(key, window, bucket, result))
        return output

    def end_of_input(self) -> list[Any]:
        output: list[Any] = []
        for key in list(self._windows):
            for boundaries in list(self._windows.get(key, {})):
                window = Window(*boundaries)
                bucket = self._windows[key][boundaries]
                if not bucket.fired:
                    if self.assigner.is_event_time:
                        result = self.trigger.on_event_time(window.end, window)
                    elif isinstance(self.assigner, GlobalWindows):
                        # GlobalWindows 可能挂 EventTimeTrigger，也可能挂
                        # ProcessingTimeTrigger；先给事件时间一次机会，
                        # CONTINUE 时再按处理时间收尾。
                        result = self.trigger.on_event_time(window.end, window)
                        if result is TriggerResult.CONTINUE:
                            result = self.trigger.on_processing_time(window.end, window)
                    else:
                        result = self.trigger.on_processing_time(window.end, window)
                    output.extend(self._handle_trigger(key, window, bucket, result))
                self._remove(key, window)
        return output

    def snapshot(self) -> dict[str, Any]:
        return {
            "windows": [
                (
                    copy.deepcopy(key),
                    [
                        (start, end, bucket.snapshot())
                        for (start, end), bucket in buckets.items()
                    ],
                )
                for key, buckets in self._windows.items()
            ],
            "watermark": self._watermark,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self._watermark = snapshot.get("watermark", -(1 << 63))
        self._windows = {}
        for key, entries in snapshot.get("windows", []):
            restored: dict[tuple[int, int], _WindowBucket] = {}
            for start, end, bucket_state in entries:
                restored[(int(start), int(end))] = _WindowBucket.restore(bucket_state)
            self._windows[copy.deepcopy(key)] = restored

    def _get_or_create(self, key: Any, window: Window) -> _WindowBucket:
        buckets = self._windows.setdefault(key, {})
        boundaries = (window.start, window.end)
        if boundaries not in buckets:
            buckets[boundaries] = _WindowBucket()
        return buckets[boundaries]

    def _merge_sessions(
        self, key: Any, window: Window
    ) -> tuple[_WindowBucket, Window]:
        buckets = self._windows.setdefault(key, {})
        merged = window
        merge_keys: list[tuple[int, int]] = []
        for boundaries in list(buckets):
            other = Window(*boundaries)
            if other.overlap(merged):
                merged = merged.merge(other)
                merge_keys.append(boundaries)
        if not merge_keys:
            return self._get_or_create(key, window), window
        target = _WindowBucket()
        for boundaries in merge_keys:
            source = buckets.pop(boundaries)
            target.records.extend(source.records)
            target.count += source.count
            target.fired = target.fired or source.fired
            if self.aggregate_function is not None:
                if target.accumulator is None:
                    target.accumulator = source.accumulator
                elif source.accumulator is not None:
                    target.accumulator = self.aggregate_function.merge(
                        target.accumulator, source.accumulator
                    )
        # 会话被迟到记录延长后，结束时间还在未来；旧结果已经不代表合并后的
        # 会话，必须允许它在新的结束时间再次触发。
        if merged.end > self._watermark:
            target.fired = False
        buckets[(merged.start, merged.end)] = target
        return target, merged

    def _add(self, bucket: _WindowBucket, record: Any) -> None:
        bucket.count += 1
        if self.aggregate_function is not None:
            if bucket.accumulator is None:
                bucket.accumulator = self.aggregate_function.create_accumulator()
            bucket.accumulator = self.aggregate_function.add(
                record.value, bucket.accumulator
            )
        else:
            bucket.records.append(record)

    def _handle_trigger(
        self, key: Any, window: Window, bucket: _WindowBucket, result: TriggerResult
    ) -> list[Any]:
        output: list[Any] = []
        if result in (TriggerResult.FIRE, TriggerResult.FIRE_AND_PURGE):
            output.extend(self._fire(key, window, bucket))
            bucket.fired = True
        if result in (TriggerResult.PURGE, TriggerResult.FIRE_AND_PURGE):
            self._remove(key, window)
        return output

    def _fire(self, key: Any, window: Window, bucket: _WindowBucket) -> list[Any]:
        assert self._context is not None
        # 窗口触发的上下文语义：key 是窗口 key，timestamp 是窗口最大时间戳
        # （Flink 的 maxTimestamp = end - 1），侧输出也归到这个 key/时间。
        self._context.set_current_key(key)
        self._context.current_timestamp = window.end - 1
        evict_after = bool(
            self.evictor is not None and getattr(self.evictor, "do_evict_after", False)
        )
        if self.aggregate_function is not None:
            # get_result 合法地可以直接返回 accumulator 本身；必须在这里做
            # 快照，否则后续迟到记录继续修改 accumulator 时，已经发射出去
            # 的结果会被“追溯改写”。Flink 在发射时会序列化，语义上等价。
            value = copy.deepcopy(
                self.aggregate_function.get_result(bucket.accumulator)
            )
            result = WindowResult(key, window, value)
            return [WindowEmission(result, window, key)]

        records = list(bucket.records)
        if self.evictor is not None and not evict_after:
            records = self.evictor.evict_before_fire(records, window)
        # Evictor 看到的是带时间戳的 Record；用户函数只看到 value。
        values = [record.value for record in records]
        if self.process_window_function is not None:
            out: Collector[Any] = Collector()
            ctx = _make_window_context(self._context, window, bucket.window_state)
            self.process_window_function.process(key, window, values, ctx, out)
            emissions = [
                WindowEmission(value, window, key) for value in out.get_values()
            ]
        elif self.window_function is not None:
            out = Collector()
            self.window_function.apply(key, window, values, out)
            emissions = [
                WindowEmission(value, window, key) for value in out.get_values()
            ]
        else:
            emissions = [WindowEmission(WindowResult(key, window, values), window, key)]
        # Flink 的 evictAfter 作用在窗口状态上：本次触发仍看到完整记录，
        # 淘汰后的状态供下一次触发使用。
        if self.evictor is not None and evict_after:
            bucket.records = self.evictor.evict_after_fire(bucket.records, window)
        return emissions

    def _advance_processing_time(self, now: int) -> list[Any]:
        output: list[Any] = []
        if self.assigner.is_event_time:
            return output
        for key in list(self._windows):
            for boundaries in list(self._windows.get(key, {})):
                window = Window(*boundaries)
                bucket = self._windows[key][boundaries]
                if now >= window.end + self.allowed_lateness_ms:
                    # 与事件时间清理一致：给 trigger 一次机会，但不能绕过它
                    # 强行发射（CountTrigger 等可能仍返回 CONTINUE）。
                    if not bucket.fired:
                        result = self.trigger.on_processing_time(now, window)
                        output.extend(self._handle_trigger(key, window, bucket, result))
                    self._remove(key, window)
                elif now >= window.end and not bucket.fired:
                    # ProcessingTimeTrigger 在 now >= end 后会一直返回 FIRE；
                    # 这里必须用 fired 去重，否则每条记录都会重复发射同一个窗口。
                    result = self.trigger.on_processing_time(now, window)
                    output.extend(self._handle_trigger(key, window, bucket, result))
        return output

    def _is_late(self, window: Window) -> bool:
        return window.end + self.allowed_lateness_ms <= self._watermark

    def _is_late_record(self, key: Any, window: Window) -> bool:
        """判断记录是否对所有可能合并的会话都已过期。

        会话窗口的“有效结束时间”是合并后的结束时间，不能用记录初始生成的
        ``[ts, ts+gap)`` 直接判断；否则本可并入长会话的记录会被误丢。
        """
        if not self.assigner.is_session:
            return self._is_late(window)
        merged_end = window.end
        for start, end in self._windows.get(key, {}):
            other = Window(start, end)
            if other.overlap(window):
                merged_end = max(merged_end, end)
        return merged_end + self.allowed_lateness_ms <= self._watermark

    def _remove(self, key: Any, window: Window) -> None:
        buckets = self._windows.get(key)
        if not buckets:
            return
        buckets.pop((window.start, window.end), None)
        if not buckets:
            self._windows.pop(key, None)


def _make_window_context(
    runtime: RuntimeContext,
    window: Window,
    window_state: dict[str, Any] | None = None,
) -> Any:
    from .functions import WindowContext

    return WindowContext(runtime, window, window_state)


# ---- 常用聚合函数 ----


class SumAggregate(AggregateFunction):
    def __init__(self, field: str | int | None = None) -> None:
        self.field = field

    def create_accumulator(self) -> float:
        return 0

    def add(self, value: Any, accumulator: float) -> float:
        return accumulator + _pick(value, self.field)

    def get_result(self, accumulator: float) -> float:
        return accumulator

    def merge(self, a: float, b: float) -> float:
        return a + b


class CountAggregate(AggregateFunction):
    def create_accumulator(self) -> int:
        return 0

    def add(self, value: Any, accumulator: int) -> int:
        return accumulator + 1

    def get_result(self, accumulator: int) -> int:
        return accumulator

    def merge(self, a: int, b: int) -> int:
        return a + b


class MinAggregate(AggregateFunction):
    def __init__(self, field: str | int | None = None) -> None:
        self.field = field

    def create_accumulator(self) -> Any:
        return None

    def add(self, value: Any, accumulator: Any) -> Any:
        candidate = _pick(value, self.field)
        return candidate if accumulator is None else min(accumulator, candidate)

    def get_result(self, accumulator: Any) -> Any:
        return accumulator

    def merge(self, a: Any, b: Any) -> Any:
        if a is None:
            return b
        if b is None:
            return a
        return min(a, b)


class MaxAggregate(MinAggregate):
    def add(self, value: Any, accumulator: Any) -> Any:
        candidate = _pick(value, self.field)
        return candidate if accumulator is None else max(accumulator, candidate)

    def merge(self, a: Any, b: Any) -> Any:
        if a is None:
            return b
        if b is None:
            return a
        return max(a, b)


class AverageAggregate(AggregateFunction):
    def __init__(self, field: str | int | None = None) -> None:
        self.field = field

    def create_accumulator(self) -> tuple[float, int]:
        return (0.0, 0)

    def add(self, value: Any, accumulator: tuple[float, int]) -> tuple[float, int]:
        return (accumulator[0] + _pick(value, self.field), accumulator[1] + 1)

    def get_result(self, accumulator: tuple[float, int]) -> float:
        return accumulator[0] / accumulator[1] if accumulator[1] else 0.0

    def merge(self, a: tuple[float, int], b: tuple[float, int]) -> tuple[float, int]:
        return (a[0] + b[0], a[1] + b[1])


def _pick(value: Any, field: str | int | None) -> Any:
    if field is None:
        return value
    if isinstance(field, int):
        return value[field]
    if isinstance(value, dict):
        return value[field]
    return getattr(value, field)
