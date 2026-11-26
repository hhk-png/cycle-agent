"""算子实现。

运行时只认识 ``Operator`` 协议：处理记录、处理 watermark、输入结束、快照和
恢复。具体的 map/filter/window/join 都通过组合这个协议实现。
"""

from __future__ import annotations

import copy
import os
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from .core import (
    MAX_WATERMARK,
    Collector,
    EndOfStream,
    OutputTag,
    Record,
    SideOutputRecord,
    StreamElement,
    Watermark,
    Window,
    WindowResult,
)
from .functions import (
    AggregateFunction,
    AsyncFunction,
    CoProcessFunction,
    FlatMapFunction,
    KeyedProcessFunction,
    MapFunction,
    ProcessContext,
    ProcessFunction,
    ProcessJoinFunction,
    ReduceFunction,
    RichFlatMapFunction,
    RuntimeContext,
    WindowFunction,
    ProcessWindowFunction,
    as_filter_function,
    as_flat_map_function,
    as_map_function,
    as_reduce_function,
)
from .state import (
    KeyedStateBackend,
    MapStateDescriptor,
    ValueStateDescriptor,
)
from .watermarks import (
    RecordTimestampAssigner,
    TimestampAssigner,
    WatermarkGenerator,
    WatermarkStrategy,
)
from .windows import WindowAssigner, WindowOperator


class Operator:
    """所有运行时算子的最小协议。"""

    def open(self, context: RuntimeContext) -> None:
        self.context = context

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        return [record]

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        return [Watermark(watermark)]

    def end_of_input(self) -> list[StreamElement]:
        return [Watermark(MAX_WATERMARK), EndOfStream()]

    def poll(self) -> list[StreamElement]:
        return []

    def snapshot(self) -> dict[str, Any]:
        return {}

    def restore(self, snapshot: dict[str, Any]) -> None:
        pass

    def close(self) -> None:
        pass


class SourceOperator(Operator):
    """源算子不处理输入，由调度器调用 ``next_element``。

    约定：``is_exhausted`` 为真后，应先返回 ``Watermark(MAX)``，再返回
    ``EndOfStream``；调度器会把结束信号广播给所有下游通道。
    """

    def __init__(self) -> None:
        self._end_emitted = False

    def is_exhausted(self) -> bool:
        return True

    def next_element(self) -> StreamElement | None:
        return None

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        raise RuntimeError("SourceOperator 不能处理输入记录")

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        return []


class CollectionSource(SourceOperator):
    def __init__(
        self,
        data: Iterable[Any],
        subtask_index: int,
        parallelism: int,
        *,
        strategy: WatermarkStrategy | None = None,
        source_name: str = "collection",
    ) -> None:
        super().__init__()
        values = list(data)
        self.data = [values[i] for i in range(subtask_index, len(values), parallelism)]
        self.subtask_index = subtask_index
        self.parallelism = parallelism
        self.source_name = source_name
        self.strategy = strategy
        self.assigner: TimestampAssigner | None = (
            strategy.timestamp_assigner if strategy else None
        )
        self.generator = WatermarkGenerator(strategy) if strategy else None
        self.offset = 0
        self._pending: deque[StreamElement] = deque()
        self._final_watermark_emitted = False

    def next_element(self) -> StreamElement | None:
        if self._pending:
            return self._pending.popleft()
        if self.offset < len(self.data):
            raw = self.data[self.offset]
            self.offset += 1
            if isinstance(raw, Record):
                record = raw
                if self.assigner is not None:
                    ts = self.assigner.extract_timestamp(raw.value, raw.timestamp)
                    record = Record(raw.value, ts, raw.key, dict(raw.headers))
            else:
                ts = (
                    self.assigner.extract_timestamp(raw, None)
                    if self.assigner is not None
                    else None
                )
                record = Record(raw, ts)
            if self.generator is not None:
                wm = self.generator.on_record(record.timestamp or 0)
                if wm is not None:
                    self._pending.append(Watermark(wm))
            return record
        if not self._final_watermark_emitted:
            self._final_watermark_emitted = True
            return Watermark(MAX_WATERMARK)
        if not self._end_emitted:
            self._end_emitted = True
            return EndOfStream()
        return None

    def is_exhausted(self) -> bool:
        return self._end_emitted and not self._pending

    def snapshot(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "pending": [
                ("record", e.value, e.timestamp, e.key)
                if isinstance(e, Record)
                else ("watermark", e.timestamp)
                for e in self._pending
            ],
            "final": self._final_watermark_emitted,
            "end": self._end_emitted,
            "generator": self.generator.snapshot() if self.generator else None,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.offset = snapshot.get("offset", 0)
        self._final_watermark_emitted = snapshot.get("final", False)
        # 若检查点是在作业结束后创建的，恢复时需要重新发送一次 EndOfStream，
        # 否则新执行图中的下游 task 会一直等待输入结束。
        self._end_emitted = False
        self._pending.clear()
        for item in snapshot.get("pending", []):
            if item[0] == "record":
                self._pending.append(Record(item[1], item[2], item[3]))
            else:
                self._pending.append(Watermark(item[1]))
        if self.generator and snapshot.get("generator"):
            self.generator.restore(snapshot["generator"])


class FileSource(CollectionSource):
    def __init__(
        self,
        path: str,
        subtask_index: int,
        parallelism: int,
        *,
        strategy: WatermarkStrategy | None = None,
        encoding: str = "utf-8",
    ) -> None:
        with open(path, "r", encoding=encoding) as handle:
            lines = [line.rstrip("\n") for line in handle]
        super().__init__(
            lines,
            subtask_index,
            parallelism,
            strategy=strategy,
            source_name=f"file:{os.path.basename(path)}",
        )


class MapOperator(Operator):
    def __init__(self, function: MapFunction) -> None:
        self.function = function

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        self.function.open(context)

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        return [record.with_value(self.function.map(record.value))]

    def close(self) -> None:
        self.function.close()


class FilterOperator(Operator):
    def __init__(self, function: Any) -> None:
        self.function = as_filter_function(function)

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        self.function.open(context)

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        return [record] if self.function.filter(record.value) else []

    def close(self) -> None:
        self.function.close()


class FlatMapOperator(Operator):
    def __init__(self, function: FlatMapFunction | RichFlatMapFunction) -> None:
        self.function = function

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        self.function.open(context)

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        out: Collector[Any] = Collector()
        self.function.flat_map(record.value, out)
        return [record.with_value(value) for value in out.get_values()]

    def close(self) -> None:
        self.function.close()


class PassThroughOperator(Operator):
    """union / side output / rebalance 等只改变拓扑、不改变数据的节点。"""

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        return [record]

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        return [Watermark(watermark)]


class KeyByOperator(Operator):
    def __init__(self, selector: Callable[[Any], Any]) -> None:
        self.selector = selector

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        return [record.with_key(self.selector(record.value))]


class ReduceOperator(Operator):
    def __init__(self, function: ReduceFunction) -> None:
        self.function = as_reduce_function(function)
        self.descriptor = ValueStateDescriptor("reduce-current", lambda: None)

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        self.function.open(context)

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        state = self.context.get_state(self.descriptor)
        current = state.get()
        value = record.value if current is None else self.function.reduce(current, record.value)
        state.update(value)
        return [record.with_value(value)]

    def close(self) -> None:
        self.function.close()


class AggregateOperator(Operator):
    def __init__(self, function: AggregateFunction) -> None:
        self.function = function
        self.descriptor = ValueStateDescriptor("aggregate-current", lambda: None)

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        self.function.open(context)

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        state = self.context.get_state(self.descriptor)
        acc = state.get()
        if acc is None:
            acc = self.function.create_accumulator()
        acc = self.function.add(record.value, acc)
        state.update(acc)
        return [record.with_value(self.function.get_result(acc))]

    def close(self) -> None:
        self.function.close()


def _collect_side_outputs(ctx: RuntimeContext) -> dict[str, list[Any]]:
    return ctx.drain_side_outputs()


class ProcessOperator(Operator):
    def __init__(self, function: ProcessFunction, *, keyed: bool = False) -> None:
        self.function = function
        self.keyed = keyed

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        self.function.open(context)

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        self.context.current_timestamp = record.timestamp
        self.context.current_watermark = self.context.timer_service.current_watermark
        out: Collector[Any] = Collector()
        self.function.process_element(
            record.value, ProcessContext(self.context), out
        )
        output: list[StreamElement] = [
            record.with_value(value) for value in out.get_values()
        ]
        # 先把本条记录产生的侧输出取走，避免紧接着到期的 processing timer
        # 把它们误认为定时器回调的侧输出。
        for records in self.context.drain_side_output_records().values():
            output.extend(records)
        output.extend(self._fire_processing_timers())
        return output

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        self.context.timer_service.current_watermark = max(
            self.context.timer_service.current_watermark, watermark
        )
        output: list[StreamElement] = []
        if isinstance(self.function, KeyedProcessFunction):
            # on_timer 里可能注册新的、同样已到期的 timer；必须循环排空，
            # 否则链式 timer 会丢到下一次 watermark（或永远不触发）。
            guard = 0
            while True:
                timers = self.context.timer_service.pop_event_timers(watermark)
                if not timers:
                    break
                guard += 1
                if guard > 10_000:
                    raise RuntimeError("timer 链在同一个 watermark 上无法收敛")
                for timestamp, key in timers:
                    output.extend(self._call_timer(timestamp, key))
        output.append(Watermark(watermark))
        return output

    def end_of_input(self) -> list[StreamElement]:
        # 作业结束时只触发已经到期的处理时间计时器；未来计时器应保持未触发，
        # 与 Flink “处理时间不会因为输入结束而跳跃到未来”的语义一致。
        output = self._fire_processing_timers()
        output.extend([Watermark(MAX_WATERMARK), EndOfStream()])
        return output

    def _fire_processing_timers(self, *, all_timers: bool = False) -> list[StreamElement]:
        if not isinstance(self.function, KeyedProcessFunction):
            return []
        now = self.context.processing_time()
        output: list[StreamElement] = []
        # 与事件时间 timer 一样，on_timer 里可能注册新的、已经到期的
        # processing timer（例如“立即重试”）。必须循环排空，否则链式 timer
        # 只能等到下一条记录/作业结束才触发。
        guard = 0
        while True:
            if all_timers:
                timers = self.context.timer_service.pop_processing_timers(MAX_WATERMARK)
            else:
                timers = self.context.timer_service.pop_processing_timers(now)
            if not timers:
                break
            guard += 1
            if guard > 10_000:
                raise RuntimeError("processing timer 链在同一时刻无法收敛")
            for timestamp, key in timers:
                output.extend(self._call_timer(timestamp, key))
        return output

    def _call_timer(self, timestamp: int, key: Any) -> list[StreamElement]:
        self.context.set_current_key(key)
        self.context.current_timestamp = timestamp
        out: Collector[Any] = Collector()
        self.function.on_timer(timestamp, ProcessContext(self.context), out)
        output: list[StreamElement] = [
            Record(value, timestamp, key) for value in out.get_values()
        ]
        # 定时器中的侧输出在 emit 时已经带上当前 timer 的 key/timestamp。
        for records in self.context.drain_side_output_records().values():
            output.extend(records)
        return output

    def close(self) -> None:
        self.function.close()


class BroadcastCoProcessOperator(Operator):
    def __init__(self, function: Any) -> None:
        self.function = function
        self.broadcast_state: dict[str, dict[Any, Any]] = {}

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        self.function.open(context)

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        from .functions import BroadcastContext

        self.context.current_timestamp = record.timestamp
        out: Collector[Any] = Collector()
        ctx = BroadcastContext(self.context, self.broadcast_state, is_broadcast_side=(input_index == 1))
        if input_index == 0:
            self.context.set_current_key(record.key)
            self.function.process_element(record.value, ctx, out)
        elif input_index == 1:
            self.function.process_broadcast_element(record.value, ctx, out)
        else:
            raise ValueError("广播连接只支持两个输入")
        return [record.with_value(value) for value in out.get_values()]

    def snapshot(self) -> dict[str, Any]:
        return {"broadcast_state": copy.deepcopy(self.broadcast_state)}

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.broadcast_state = copy.deepcopy(snapshot.get("broadcast_state", {}))

    def close(self) -> None:
        self.function.close()


class CoProcessOperator(Operator):
    def __init__(self, function: CoProcessFunction) -> None:
        self.function = function

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        self.function.open(context)

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        self.context.current_timestamp = record.timestamp
        out: Collector[Any] = Collector()
        ctx = ProcessContext(self.context)
        if input_index == 0:
            self.function.process_element1(record.value, ctx, out)
        elif input_index == 1:
            self.function.process_element2(record.value, ctx, out)
        else:
            raise ValueError(f"CoProcess 只支持两个输入，收到 {input_index}")
        return [record.with_value(value) for value in out.get_values()]

    def close(self) -> None:
        self.function.close()


class WindowOperatorAdapter(Operator):
    def __init__(self, window_operator: WindowOperator) -> None:
        self.window_operator = window_operator

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        self.window_operator.open(context)
        # 增量聚合函数也可能是有状态的 RichFunction（例如带缓存的 UDAF），
        # 必须和 ProcessWindowFunction 一样收到 open/close 生命周期回调。
        aggregate = getattr(self.window_operator, "aggregate_function", None)
        if aggregate is not None and hasattr(aggregate, "open"):
            aggregate.open(context)
        process_function = getattr(self.window_operator, "process_window_function", None)
        if process_function is not None and hasattr(process_function, "open"):
            process_function.open(context)
        window_function = getattr(self.window_operator, "window_function", None)
        if window_function is not None and hasattr(window_function, "open"):
            window_function.open(context)

    def close(self) -> None:
        for name in ("aggregate_function", "process_window_function", "window_function"):
            function = getattr(self.window_operator, name, None)
            if function is not None and hasattr(function, "close"):
                function.close()

    def _drain_side_records(self) -> list[StreamElement]:
        output: list[StreamElement] = []
        for records in self.context.drain_side_output_records().values():
            output.extend(records)
        return output

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        values = self.window_operator.process_record(record)
        output: list[StreamElement] = [
            Record(value=emission.value, timestamp=emission.window.end - 1, key=emission.key)
            for emission in values
        ]
        output.extend(self._drain_side_records())
        return output

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        values = self.window_operator.process_watermark(watermark)
        output: list[StreamElement] = [
            Record(value=emission.value, timestamp=emission.window.end - 1, key=emission.key)
            for emission in values
        ]
        # 侧输出必须先于 watermark 下发，否则下游窗口会把它们当成迟到数据。
        output.extend(self._drain_side_records())
        output.append(Watermark(watermark))
        return output

    def end_of_input(self) -> list[StreamElement]:
        values = self.window_operator.end_of_input()
        output: list[StreamElement] = [
            Record(value=emission.value, timestamp=emission.window.end - 1, key=emission.key)
            for emission in values
        ]
        output.extend(self._drain_side_records())
        output.extend([Watermark(MAX_WATERMARK), EndOfStream()])
        return output

    def snapshot(self) -> dict[str, Any]:
        return {"window": self.window_operator.snapshot()}

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.window_operator.restore(snapshot.get("window", {}))


class JoinOperator(Operator):
    """事件时间窗口 join 的教学实现。

    两条输入会先分别 keyBy 并路由到相同 key 的实例；每侧按窗口缓存记录，
    watermark 越过窗口结束时输出笛卡尔积，随后清理缓存。
    """

    def __init__(
        self,
        left_key,
        right_key,
        assigner: WindowAssigner,
        join_function: Callable[[Any, Any], Any],
    ) -> None:
        self.left_key = left_key
        self.right_key = right_key
        self.assigner = assigner
        self.join_function = join_function
        self.left: dict[Any, dict[tuple[int, int], list[Record]]] = {}
        self.right: dict[Any, dict[tuple[int, int], list[Record]]] = {}
        self.watermark = -(1 << 63)
        self.emitted: set[tuple[Any, int, int]] = set()

    def open(self, context: RuntimeContext) -> None:
        super().open(context)

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        if record.key is None:
            raise RuntimeError("join 的两侧必须先 keyBy")
        ts = record.timestamp or 0
        windows = self.assigner.assign_windows(record.value, ts, self.context)
        store = self.left if input_index == 0 else self.right
        accepted: list[Window] = []
        for window in windows:
            # allowedLateness=0 的窗口 join 中，watermark 已越过窗口结束的
            # 记录属于迟到数据。必须在入状态前丢弃：否则它会一直堆到下一个
            # watermark，而 emitted token 又会永久保留，长跑作业状态只增不减。
            if window.end <= self.watermark:
                continue
            store.setdefault(record.key, {}).setdefault(
                (window.start, window.end), []
            ).append(record)
            accepted.append(window)
        # 未越过 watermark 的窗口可能立即具备两侧数据；迟到记录不再补发。
        output: list[StreamElement] = []
        for window in accepted:
            output.extend(self._try_emit(record.key, window, self.watermark))
        return output

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        self.watermark = max(self.watermark, watermark)
        output: list[StreamElement] = []
        keys = set(self.left) | set(self.right)
        for key in list(keys):
            boundaries = set(self.left.get(key, {})) | set(self.right.get(key, {}))
            for start, end in list(boundaries):
                window = Window(start, end)
                if watermark >= window.end:
                    output.extend(self._try_emit(key, window, watermark, force=True))
                    self.left.get(key, {}).pop((start, end), None)
                    self.right.get(key, {}).pop((start, end), None)
                    # 保留 emitted token 直到 watermark 确实越过窗口结束；
                    # process_record 会在入状态前丢弃迟到记录，因此越过之后
                    # token 不再需要，可以安全清理，避免长跑作业内存增长。
            # 窗口清空后移除空 key，避免长期运行时状态里堆满空字典。
            for store in (self.left, self.right):
                if key in store and not store[key]:
                    del store[key]
        self.emitted = {
            token for token in self.emitted if token[2] > watermark
        }
        output.append(Watermark(watermark))
        return output

    def end_of_input(self) -> list[StreamElement]:
        output: list[StreamElement] = []
        for key in set(self.left) | set(self.right):
            boundaries = set(self.left.get(key, {})) | set(self.right.get(key, {}))
            for start, end in boundaries:
                output.extend(self._try_emit(key, Window(start, end), MAX_WATERMARK, force=True))
        self.left.clear()
        self.right.clear()
        self.emitted.clear()
        output.extend([Watermark(MAX_WATERMARK), EndOfStream()])
        return output

    def _try_emit(
        self,
        key: Any,
        window: Window,
        watermark: int,
        *,
        force: bool = False,
    ) -> list[StreamElement]:
        if not force and watermark < window.end:
            return []
        left = self.left.get(key, {}).get((window.start, window.end), [])
        right = self.right.get(key, {}).get((window.start, window.end), [])
        if not left or not right:
            return []
        token = (key, window.start, window.end)
        if token in self.emitted:
            return []
        self.emitted.add(token)
        # 与 Flink 的 WindowJoin 一致：输出使用窗口内最大时间戳 end - 1，
        # 而不是 end，否则下游事件时间窗口会把结果分到下一个窗口。
        output = [
            Record(
                self.join_function(l.value, r.value),
                timestamp=window.end - 1,
                key=key,
            )
            for l in left
            for r in right
        ]
        return output

    def snapshot(self) -> dict[str, Any]:
        return {
            "left": copy.deepcopy(self.left),
            "right": copy.deepcopy(self.right),
            "watermark": self.watermark,
            "emitted": copy.deepcopy(self.emitted),
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.left = copy.deepcopy(snapshot.get("left", {}))
        self.right = copy.deepcopy(snapshot.get("right", {}))
        self.watermark = snapshot.get("watermark", -(1 << 63))
        self.emitted = copy.deepcopy(snapshot.get("emitted", set()))


class IntervalJoinOperator(Operator):
    """双流区间 join：左流每条记录匹配右流时间区间内的记录。"""

    def __init__(
        self,
        lower_ms: int,
        upper_ms: int,
        join_function: Callable[[Any, Any], Any],
    ) -> None:
        if lower_ms > upper_ms:
            raise ValueError("区间下界不能大于上界")
        self.lower_ms = lower_ms
        self.upper_ms = upper_ms
        self.join_function = join_function
        self.right_buffer: dict[Any, list[Record]] = {}
        self.watermark = -(1 << 63)
        self.left_buffer: dict[Any, list[Record]] = {}

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        if record.key is None:
            raise RuntimeError("interval join 必须先 keyBy")
        ts = record.timestamp or 0
        # Flink 的 interval join 会丢弃低于当前 watermark 的迟到记录，
        # 而不是让它与已缓存的另一侧记录补配对。
        if ts < self.watermark:
            return []
        if input_index == 0:
            self.left_buffer.setdefault(record.key, []).append(record)
            matches = [
                r
                for r in self.right_buffer.get(record.key, [])
                if ts + self.lower_ms <= (r.timestamp or 0) <= ts + self.upper_ms
            ]
            return [
                # 输出时间取两侧记录中较大的事件时间，供下游窗口正确归属。
                Record(
                    self.join_function(record.value, r.value),
                    timestamp=max(ts, r.timestamp or 0),
                    key=record.key,
                )
                for r in matches
            ]
        if input_index == 1:
            self.right_buffer.setdefault(record.key, []).append(record)
            matches = [
                l
                for l in self.left_buffer.get(record.key, [])
                if (l.timestamp or 0) + self.lower_ms <= ts <= (l.timestamp or 0) + self.upper_ms
            ]
            return [
                Record(
                    self.join_function(l.value, record.value),
                    timestamp=max(l.timestamp or 0, ts),
                    key=record.key,
                )
                for l in matches
            ]
        raise ValueError("interval join 只接受两个输入")

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        self.watermark = max(self.watermark, watermark)
        # 左侧记录只有在 watermark - upper 之后仍可能匹配未来右侧记录；
        # 右侧记录只有在 watermark + lower 之后才可能匹配未来左侧记录。
        left_cutoff = watermark - self.upper_ms
        right_cutoff = watermark + self.lower_ms
        for key in list(self.right_buffer):
            self.right_buffer[key] = [
                r for r in self.right_buffer[key] if (r.timestamp or 0) >= right_cutoff
            ]
            if not self.right_buffer[key]:
                del self.right_buffer[key]
        for key in list(self.left_buffer):
            self.left_buffer[key] = [
                r for r in self.left_buffer[key] if (r.timestamp or 0) >= left_cutoff
            ]
            if not self.left_buffer[key]:
                del self.left_buffer[key]
        return [Watermark(watermark)]

    def end_of_input(self) -> list[StreamElement]:
        return [Watermark(MAX_WATERMARK), EndOfStream()]

    def snapshot(self) -> dict[str, Any]:
        return {
            "left": copy.deepcopy(self.left_buffer),
            "right": copy.deepcopy(self.right_buffer),
            "watermark": self.watermark,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.left_buffer = copy.deepcopy(snapshot.get("left", {}))
        self.right_buffer = copy.deepcopy(snapshot.get("right", {}))
        self.watermark = snapshot.get("watermark", -(1 << 63))


class AsyncMapOperator(Operator):
    """按序等待的 Async I/O 教学实现。

    生产 Flink 的 Async I/O 依赖异步客户端、容量控制与 checkpoint 中的
    in-flight 记录；这里用线程池模拟“发起请求”和“按序回收”。
    """

    def __init__(self, function: AsyncFunction | Callable[[Any], Any], capacity: int = 10) -> None:
        self.function = function
        self.capacity = capacity
        self.executor = ThreadPoolExecutor(max_workers=capacity)
        self.pending: deque[tuple[Record, Future]] = deque()
        self._closed = False

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        open_function = getattr(self.function, "open", None)
        if callable(open_function):
            open_function(context)

    def _invoke(self, value: Any) -> Any:
        if isinstance(self.function, AsyncFunction):
            return self.function.async_invoke(value)
        return self.function(value)

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        future = self.executor.submit(self._invoke, record.value)
        self.pending.append((record, future))
        return []

    def poll(self) -> list[StreamElement]:
        output: list[StreamElement] = []
        # 保持发起顺序；队首未完成时阻塞等待，确保检查点不会切到 in-flight。
        while self.pending:
            record, future = self.pending[0]
            value = future.result()
            self.pending.popleft()
            output.append(record.with_value(value))
        return output

    def end_of_input(self) -> list[StreamElement]:
        output: list[StreamElement] = []
        while self.pending:
            record, future = self.pending.popleft()
            output.append(record.with_value(future.result()))
        output.extend([Watermark(MAX_WATERMARK), EndOfStream()])
        return output

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            close = getattr(self.function, "close", None)
            if callable(close):
                close()
            self.executor.shutdown(wait=True)


class SinkOperator(Operator):
    def __init__(self, sink_function: Callable[[Any], None], *, name: str = "sink") -> None:
        self.sink_function = sink_function
        self.name = name

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        self.sink_function(record.value)
        return []

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        return []

    def end_of_input(self) -> list[StreamElement]:
        return [EndOfStream()]


class CollectSink(SinkOperator):
    def __init__(self) -> None:
        self.values: list[Any] = []
        super().__init__(self.values.append, name="collect-sink")

    def snapshot(self) -> dict[str, Any]:
        return {"values": copy.deepcopy(self.values)}

    def restore(self, snapshot: dict[str, Any]) -> None:
        # sink_function 在构造时绑定的是 self.values.append；如果这里整体替换
        # 列表对象，绑定方法仍指向旧列表，恢复后的输出会“消失”。原地切片赋值
        # 既保留对象身份，也保留 sink 的写入入口。
        self.values[:] = copy.deepcopy(snapshot.get("values", []))


class PrintSink(SinkOperator):
    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        super().__init__(lambda value: print(f"{self.prefix}{value}"), name="print-sink")


class TextFileSink(SinkOperator):
    """按行写文件；采用临时文件 + rename，输入结束后一次性可见。"""

    def __init__(self, path: str, formatter: Callable[[Any], str] = str) -> None:
        self.path = path
        self.formatter = formatter
        self._handle = None
        self._tmp_path = path + ".tmp"
        super().__init__(self._write, name=f"file-sink:{os.path.basename(path)}")

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._handle = open(self._tmp_path, "w", encoding="utf-8")

    def _write(self, value: Any) -> None:
        assert self._handle is not None
        self._handle.write(self.formatter(value) + "\n")

    def end_of_input(self) -> list[StreamElement]:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            self._handle = None
            os.replace(self._tmp_path, self.path)
        return [EndOfStream()]

    def snapshot(self) -> dict[str, Any]:
        # 本地教学版不实现两阶段提交；文件在作业结束时原子出现。
        return {}

    def restore(self, snapshot: dict[str, Any]) -> None:
        pass


class EndOfStreamOperator(Operator):
    """内部用于收集终止信号。"""

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        return []

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        return []
