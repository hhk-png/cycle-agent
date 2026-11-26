"""用户函数、运行时上下文与计时器。

MiniFlink 的算子分两类：

* 无状态函数：map/filter/flatMap/reduce/aggregate，签名与 Flink 接近；
* 富函数与 ProcessFunction：可以访问 RuntimeContext、状态、侧输出和计时器。

为了让示例足够短，普通函数也可以直接传 Python lambda；显式继承这些类比
Java 中实现接口更接近真实 Flink 的写法。
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Generic, Iterable, TypeVar

from .core import Collector, OutputTag, SideOutputRecord, Window
from .state import (
    AggregatingState,
    AggregatingStateDescriptor,
    KeyedStateBackend,
    ListState,
    ListStateDescriptor,
    MapState,
    MapStateDescriptor,
    ReducingState,
    ReducingStateDescriptor,
    StateDescriptor,
    ValueState,
    ValueStateDescriptor,
)

T = TypeVar("T")
R = TypeVar("R")
K = TypeVar("K")
V = TypeVar("V")
ACC = TypeVar("ACC")
OUT = TypeVar("OUT")
T1 = TypeVar("T1")
T2 = TypeVar("T2")


class Function(ABC):
    def open(self, context: "RuntimeContext") -> None:
        pass

    def close(self) -> None:
        pass


class MapFunction(Function, Generic[T, R]):
    @abstractmethod
    def map(self, value: T) -> R: ...


class FilterFunction(Function, Generic[T]):
    @abstractmethod
    def filter(self, value: T) -> bool: ...


class FlatMapFunction(Function, Generic[T, R]):
    @abstractmethod
    def flat_map(self, value: T, out: Collector[R]) -> None: ...


class ReduceFunction(Function, Generic[T]):
    @abstractmethod
    def reduce(self, value1: T, value2: T) -> T: ...


class AggregateFunction(Function, Generic[T, ACC, R]):
    @abstractmethod
    def create_accumulator(self) -> ACC: ...

    @abstractmethod
    def add(self, value: T, accumulator: ACC) -> ACC: ...

    @abstractmethod
    def get_result(self, accumulator: ACC) -> R: ...

    def merge(self, a: ACC, b: ACC) -> ACC:
        raise NotImplementedError("该聚合函数不支持会话窗口合并")


class RichFlatMapFunction(Function, Generic[T, R]):
    @abstractmethod
    def flat_map(self, value: T, out: Collector[R]) -> None: ...


class TimerService:
    """计时器服务。事件时间计时器由 watermark 推进触发。"""

    def __init__(self) -> None:
        self._event_timers: dict[int, set[Any]] = {}
        self._processing_timers: dict[int, set[Any]] = {}
        self.current_watermark: int = -(1 << 63)

    def register_event_time_timer(self, timestamp: int, key: Any) -> None:
        self._event_timers.setdefault(timestamp, set()).add(key)

    def register_processing_time_timer(self, timestamp: int, key: Any) -> None:
        self._processing_timers.setdefault(timestamp, set()).add(key)

    def delete_event_time_timer(self, timestamp: int, key: Any) -> None:
        bucket = self._event_timers.get(timestamp)
        if bucket:
            bucket.discard(key)
            if not bucket:
                del self._event_timers[timestamp]

    def delete_processing_time_timer(self, timestamp: int, key: Any) -> None:
        bucket = self._processing_timers.get(timestamp)
        if bucket:
            bucket.discard(key)
            if not bucket:
                del self._processing_timers[timestamp]

    def pop_event_timers(self, watermark: int) -> list[tuple[int, Any]]:
        ready: list[tuple[int, Any]] = []
        for timestamp in sorted(list(self._event_timers)):
            if timestamp > watermark:
                break
            for key in sorted(self._event_timers.pop(timestamp), key=repr):
                ready.append((timestamp, key))
        return ready

    def pop_processing_timers(self, now: int) -> list[tuple[int, Any]]:
        ready: list[tuple[int, Any]] = []
        for timestamp in sorted(list(self._processing_timers)):
            if timestamp > now:
                break
            for key in sorted(self._processing_timers.pop(timestamp), key=repr):
                ready.append((timestamp, key))
        return ready

    def earliest_event_time(self) -> int | None:
        return min(self._event_timers) if self._event_timers else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "event": {ts: sorted(keys, key=repr) for ts, keys in self._event_timers.items()},
            "processing": {
                ts: sorted(keys, key=repr) for ts, keys in self._processing_timers.items()
            },
            "watermark": self.current_watermark,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self._event_timers = {
            ts: set(keys) for ts, keys in snapshot.get("event", {}).items()
        }
        self._processing_timers = {
            ts: set(keys) for ts, keys in snapshot.get("processing", {}).items()
        }
        self.current_watermark = snapshot.get("watermark", -(1 << 63))


class RuntimeContext:
    """算子运行期上下文。

    一个 Task 持有独立的状态后端；这样即使多个算子在同一个进程里运行，
    同名状态也不会互相污染。
    """

    def __init__(
        self,
        *,
        task_name: str = "task",
        subtask_index: int = 0,
        parallelism: int = 1,
        state_backend: KeyedStateBackend | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.task_name = task_name
        self.subtask_index = subtask_index
        self.parallelism = parallelism
        self.state_backend = state_backend or KeyedStateBackend(clock=clock)
        self.timer_service = TimerService()
        self.current_key: Any = None
        self.current_timestamp: int | None = None
        self.current_watermark: int = -(1 << 63)
        self._side_outputs: dict[str, list[SideOutputRecord]] = {}
        self._clock = clock
        self.metric_group: Any = None

    def set_current_key(self, key: Any) -> None:
        self.current_key = key
        self.state_backend.set_current_key(key)

    def get_state(self, descriptor: ValueStateDescriptor[T]) -> ValueState[T]:
        return ValueState(self.state_backend, descriptor)

    def get_list_state(self, descriptor: ListStateDescriptor[T]) -> ListState[T]:
        return ListState(self.state_backend, descriptor)

    def get_map_state(
        self, descriptor: MapStateDescriptor[K, V]
    ) -> MapState[K, V]:
        return MapState(self.state_backend, descriptor)

    def get_reducing_state(self, descriptor: ReducingStateDescriptor[T]) -> ReducingState[T]:
        return ReducingState(self.state_backend, descriptor)

    def get_aggregating_state(
        self, descriptor: AggregatingStateDescriptor[T]
    ) -> AggregatingState[T]:
        return AggregatingState(self.state_backend, descriptor)

    def emit_side_output(self, tag: OutputTag, value: Any) -> None:
        # 在发射时捕获 key/timestamp，避免 window/timer 回调产生的侧输出
        # 被错误归到“最后一条数据记录”或“前一条记录”的元数据上。
        self._side_outputs.setdefault(tag.id, []).append(
            SideOutputRecord(
                tag=tag.id,
                value=value,
                timestamp=self.current_timestamp,
                key=self.current_key,
            )
        )

    def drain_side_output_records(self) -> dict[str, list[SideOutputRecord]]:
        result = {
            tag: list(records) for tag, records in self._side_outputs.items()
        }
        self._side_outputs.clear()
        return result

    def drain_side_outputs(self) -> dict[str, list[Any]]:
        return {
            tag: [record.value for record in records]
            for tag, records in self.drain_side_output_records().items()
        }

    def get_side_output(self, tag: OutputTag) -> list[Any]:
        return [record.value for record in self._side_outputs.get(tag.id, [])]

    def processing_time(self) -> int:
        # 注入 clock 时使用注入值；这让 TimerService/TTL 的测试可确定复现。
        if self._clock is not None:
            return int(self._clock())
        import time

        return int(time.time() * 1000)


class ProcessContext:
    def __init__(self, runtime: RuntimeContext) -> None:
        self._runtime = runtime

    @property
    def timestamp(self) -> int | None:
        return self._runtime.current_timestamp

    @property
    def current_key(self) -> Any:
        return self._runtime.current_key

    @property
    def watermark(self) -> int:
        return self._runtime.current_watermark

    @property
    def timer_service(self) -> TimerService:
        return self._runtime.timer_service

    def output(self, tag: OutputTag, value: Any) -> None:
        self._runtime.emit_side_output(tag, value)

    def get_state(self, descriptor: ValueStateDescriptor[T]) -> ValueState[T]:
        return self._runtime.get_state(descriptor)

    def get_list_state(self, descriptor: ListStateDescriptor[T]) -> ListState[T]:
        return self._runtime.get_list_state(descriptor)

    def get_map_state(self, descriptor: MapStateDescriptor[K, V]) -> MapState[K, V]:
        return self._runtime.get_map_state(descriptor)

    def get_reducing_state(self, descriptor: ReducingStateDescriptor[T]) -> ReducingState[T]:
        return self._runtime.get_reducing_state(descriptor)

    def get_aggregating_state(
        self, descriptor: AggregatingStateDescriptor[T]
    ) -> AggregatingState[T]:
        return self._runtime.get_aggregating_state(descriptor)


class ProcessFunction(Function, Generic[T, R]):
    @abstractmethod
    def process_element(self, value: T, ctx: ProcessContext, out: Collector[R]) -> None: ...


class KeyedProcessFunction(ProcessFunction[T, R], Generic[K, T, R]):
    @abstractmethod
    def process_element(self, value: T, ctx: ProcessContext, out: Collector[R]) -> None: ...

    def on_timer(self, timestamp: int, ctx: ProcessContext, out: Collector[R]) -> None:
        pass


class WindowContext(ProcessContext):
    def __init__(
        self,
        runtime: RuntimeContext,
        window: Window,
        window_state: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(runtime)
        self.window = window
        self._window_state = window_state if window_state is not None else {}

    def window_state(self) -> dict[str, Any]:
        return self._window_state


class WindowFunction(Function, Generic[T, R]):
    @abstractmethod
    def apply(
        self,
        key: Any,
        window: Window,
        values: Iterable[T],
        out: Collector[R],
    ) -> None: ...


class ProcessWindowFunction(Function, Generic[T, R]):
    @abstractmethod
    def process(
        self,
        key: Any,
        window: Window,
        values: Iterable[T],
        ctx: WindowContext,
        out: Collector[R],
    ) -> None: ...


class CoProcessFunction(Function, Generic[T1, T2, R]):
    @abstractmethod
    def process_element1(self, value: T1, ctx: ProcessContext, out: Collector[R]) -> None: ...

    @abstractmethod
    def process_element2(self, value: T2, ctx: ProcessContext, out: Collector[R]) -> None: ...


class ProcessJoinFunction(Function, Generic[T1, T2, R]):
    @abstractmethod
    def process_element(
        self, left: T1, right: T2, ctx: ProcessContext, out: Collector[R]
    ) -> None: ...


class BroadcastMapState:
    """广播状态：每个子任务持有一份只读副本，由广播流更新。"""

    def __init__(self, store: dict[str, dict[Any, Any]], name: str) -> None:
        self._store = store
        self._name = name

    def get(self, key: Any, default: Any = None) -> Any:
        return copy.deepcopy(self._store.setdefault(self._name, {}).get(key, default))

    def put(self, key: Any, value: Any) -> None:
        self._store.setdefault(self._name, {})[key] = value

    def put_all(self, values: dict[Any, Any]) -> None:
        self._store.setdefault(self._name, {}).update(values)

    def remove(self, key: Any) -> None:
        self._store.setdefault(self._name, {}).pop(key, None)

    def contains(self, key: Any) -> bool:
        return key in self._store.setdefault(self._name, {})

    def items(self):
        return copy.deepcopy(list(self._store.setdefault(self._name, {}).items()))

    def clear(self) -> None:
        self._store[self._name] = {}

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._store)

    def restore(self, snapshot: dict[str, Any]) -> None:
        self._store.clear()
        self._store.update(copy.deepcopy(snapshot))


class BroadcastContext:
    def __init__(
        self,
        runtime: RuntimeContext,
        store: dict[str, dict[Any, Any]],
        *,
        is_broadcast_side: bool,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self.is_broadcast_side = is_broadcast_side

    @property
    def timestamp(self) -> int | None:
        return self._runtime.current_timestamp

    @property
    def current_key(self) -> Any:
        return self._runtime.current_key

    def get_broadcast_state(self, descriptor: MapStateDescriptor[K, V]) -> BroadcastMapState:
        return BroadcastMapState(self._store, descriptor.name)

    def output(self, tag: OutputTag, value: Any) -> None:
        self._runtime.emit_side_output(tag, value)


class BroadcastProcessFunction(Function, Generic[T, R]):
    @abstractmethod
    def process_element(
        self, value: T, ctx: BroadcastContext, out: Collector[R]
    ) -> None: ...

    @abstractmethod
    def process_broadcast_element(
        self,
        value: Any,
        ctx: BroadcastContext,
        out: Collector[R],
    ) -> None: ...


class AsyncFunction(Function, Generic[T, R]):
    @abstractmethod
    def async_invoke(self, value: T) -> R: ...


# ---- 把普通 Python 可调用对象适配成 Flink 风格函数 ----


class LambdaMapFunction(MapFunction[T, R]):
    def __init__(self, function: Callable[[T], R]) -> None:
        self.function = function

    def map(self, value: T) -> R:
        return self.function(value)


class LambdaFilterFunction(FilterFunction[T]):
    def __init__(self, function: Callable[[T], bool]) -> None:
        self.function = function

    def filter(self, value: T) -> bool:
        return self.function(value)


class LambdaFlatMapFunction(FlatMapFunction[T, R]):
    def __init__(self, function: Callable[[T], Iterable[R]]) -> None:
        self.function = function

    def flat_map(self, value: T, out: Collector[R]) -> None:
        out.collect_all(list(self.function(value)))


class LambdaReduceFunction(ReduceFunction[T]):
    def __init__(self, function: Callable[[T, T], T]) -> None:
        self.function = function

    def reduce(self, value1: T, value2: T) -> T:
        return self.function(value1, value2)


class LambdaKeySelector:
    def __init__(self, function: Callable[[Any], Any]) -> None:
        self.function = function

    def get_key(self, value: Any) -> Any:
        return self.function(value)


def as_map_function(function: Callable[[T], R] | MapFunction[T, R]) -> MapFunction[T, R]:
    if isinstance(function, MapFunction):
        return function
    return LambdaMapFunction(function)


def as_filter_function(
    function: Callable[[T], bool] | FilterFunction[T],
) -> FilterFunction[T]:
    if isinstance(function, FilterFunction):
        return function
    return LambdaFilterFunction(function)


def as_flat_map_function(
    function: Callable[[T], Iterable[R]] | FlatMapFunction[T, R],
) -> FlatMapFunction[T, R]:
    if isinstance(function, FlatMapFunction):
        return function
    return LambdaFlatMapFunction(function)


def as_reduce_function(
    function: Callable[[T, T], T] | ReduceFunction[T],
) -> ReduceFunction[T]:
    if isinstance(function, ReduceFunction):
        return function
    return LambdaReduceFunction(function)
