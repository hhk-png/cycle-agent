"""DataStream API：环境、流、键控流、窗口流、连接流与 join。"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from .checkpointing import CheckpointConfig, CheckpointingMode, CheckpointStorage, MemoryCheckpointStorage
from .core import Duration, EndOfStream, OutputTag, Record, Watermark, Window
from .functions import (
    AggregateFunction,
    AsyncFunction,
    CoProcessFunction,
    FlatMapFunction,
    KeyedProcessFunction,
    MapFunction,
    ProcessFunction,
    ReduceFunction,
    RichFlatMapFunction,
    RuntimeContext,
    as_filter_function,
    as_flat_map_function,
    as_map_function,
    as_reduce_function,
)
from .graph import Edge, JobGraph, Node, Partitioning, build_graph, make_node
from .operators import (
    AggregateOperator,
    AsyncMapOperator,
    BroadcastCoProcessOperator,
    CoProcessOperator,
    CollectionSource,
    CollectSink,
    FileSource,
    FilterOperator,
    FlatMapOperator,
    IntervalJoinOperator,
    JoinOperator,
    KeyByOperator,
    MapOperator,
    PassThroughOperator,
    PrintSink,
    ProcessOperator,
    ReduceOperator,
    SinkOperator,
    SourceOperator,
    TextFileSink,
    WindowOperatorAdapter,
)
from .runtime import JobExecution
from .state import KeyedStateBackend
from .watermarks import WatermarkStrategy
from .windows import (
    Evictor,
    Trigger,
    WindowAssigner,
    WindowOperator,
    CountAggregate,
    MaxAggregate,
    MinAggregate,
    SumAggregate,
)


def _clone(value: Any) -> Any:
    """每个并行子任务应有独立的用户函数实例，避免隐式共享可变字段。"""
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


class RuntimeExecutionMode(str, Enum):
    STREAMING = "streaming"
    BATCH = "batch"


class TimeCharacteristic(str, Enum):
    EVENT_TIME = "event_time"
    PROCESSING_TIME = "processing_time"


class StreamExecutionEnvironment:
    def __init__(self, parallelism: int = 1) -> None:
        if parallelism <= 0:
            raise ValueError("并行度必须大于 0")
        self.parallelism = parallelism
        self.default_parallelism = parallelism
        self.checkpointing_enabled = False
        self.checkpoint_config = CheckpointConfig()
        self.checkpoint_storage: CheckpointStorage = MemoryCheckpointStorage()
        self.restart_strategy: Any = None
        self.execution_mode = RuntimeExecutionMode.STREAMING
        self.time_characteristic = TimeCharacteristic.EVENT_TIME
        self.config: dict[str, Any] = {}
        self._sink_nodes: list[Node] = []
        self.last_execution: JobExecution | None = None
        self._terminal_counter = 0

    @classmethod
    def get_execution_environment(cls) -> "StreamExecutionEnvironment":
        return cls()

    @classmethod
    def create_local_environment(cls, parallelism: int = 1) -> "StreamExecutionEnvironment":
        return cls(parallelism)

    def set_parallelism(self, parallelism: int) -> "StreamExecutionEnvironment":
        if parallelism <= 0:
            raise ValueError("并行度必须大于 0")
        self.parallelism = parallelism
        self.default_parallelism = parallelism
        return self

    def get_parallelism(self) -> int:
        return self.parallelism

    def set_runtime_mode(self, mode: RuntimeExecutionMode) -> "StreamExecutionEnvironment":
        self.execution_mode = mode
        return self

    def set_stream_time_characteristic(self, characteristic: TimeCharacteristic) -> None:
        self.time_characteristic = characteristic

    def enable_checkpointing(
        self,
        interval_ms: int = 10_000,
        *,
        storage: CheckpointStorage | None = None,
        mode: CheckpointingMode = CheckpointingMode.EXACTLY_ONCE,
        min_pause_ms: int = 500,
        timeout_ms: int = 60_000,
        micro_batch_records: int = 50,
    ) -> "StreamExecutionEnvironment":
        self.checkpointing_enabled = True
        self.checkpoint_config = CheckpointConfig(
            interval_ms=interval_ms,
            timeout_ms=timeout_ms,
            min_pause_ms=min_pause_ms,
            mode=mode,
            micro_batch_records=micro_batch_records,
        )
        self.checkpoint_storage = storage or MemoryCheckpointStorage()
        return self

    def disable_checkpointing(self) -> None:
        self.checkpointing_enabled = False

    def set_restart_strategy(self, strategy: Any) -> "StreamExecutionEnvironment":
        self.restart_strategy = strategy
        return self

    def configure(self, key: str, value: Any) -> "StreamExecutionEnvironment":
        self.config[key] = value
        return self

    def from_collection(
        self,
        data: Iterable[Any],
        *,
        name: str = "collection-source",
        watermark_strategy: WatermarkStrategy | None = None,
        parallelism: int | None = None,
    ) -> "DataStream":
        values = list(data)
        par = parallelism or self.parallelism

        def factory(index: int, subtasks: int) -> SourceOperator:
            return CollectionSource(
                values,
                index,
                subtasks,
                strategy=watermark_strategy,
                source_name=name,
            )

        node = make_node(name, factory, parallelism=par, is_source=True)
        return DataStream(self, node)

    def from_elements(self, *values: Any, **kwargs: Any) -> "DataStream":
        return self.from_collection(values, **kwargs)

    def from_sequence(self, start: int, end: int, **kwargs: Any) -> "DataStream":
        return self.from_collection(range(start, end), **kwargs)

    def read_text_file(
        self,
        path: str,
        *,
        watermark_strategy: WatermarkStrategy | None = None,
        parallelism: int | None = None,
    ) -> "DataStream":
        par = parallelism or self.parallelism

        def factory(index: int, subtasks: int) -> SourceOperator:
            return FileSource(
                path, index, subtasks, strategy=watermark_strategy
            )

        node = make_node(f"file-source:{os.path.basename(path)}", factory, parallelism=par, is_source=True)
        return DataStream(self, node)

    def add_source(
        self,
        source_factory: Callable[[int, int], SourceOperator] | Callable[[], SourceOperator],
        *,
        name: str = "custom-source",
        parallelism: int | None = None,
    ) -> "DataStream":
        par = parallelism or self.parallelism

        def factory(index: int, subtasks: int) -> SourceOperator:
            try:
                result = source_factory(index, subtasks)  # type: ignore[misc]
            except TypeError:
                result = source_factory()  # type: ignore[call-arg]
            if not isinstance(result, SourceOperator):
                raise TypeError("source_factory 必须返回 SourceOperator")
            return result

        node = make_node(name, factory, parallelism=par, is_source=True)
        return DataStream(self, node)

    def from_kafka(
        self,
        bus: Any,
        topic: str,
        *,
        group_id: str = "miniflink",
        watermark_strategy: WatermarkStrategy | None = None,
        parallelism: int = 1,
    ) -> "DataStream":
        from .connectors import KafkaSource

        def factory(index: int, subtasks: int) -> SourceOperator:
            return KafkaSource(
                bus,
                topic,
                group_id,
                index,
                subtasks,
                strategy=watermark_strategy,
            )

        node = make_node(f"kafka-source:{topic}", factory, parallelism=parallelism, is_source=True)
        return DataStream(self, node)

    def add_sink(
        self,
        sink_factory: Callable[[], SinkOperator],
        stream: "DataStream",
        *,
        name: str = "sink",
        parallelism: int = 1,
    ) -> "DataStream":
        return stream._add_sink_node(sink_factory, name=name, parallelism=parallelism)

    def execute(self, job_name: str = "MiniFlink Job") -> JobExecution:
        if not self._sink_nodes:
            raise RuntimeError("作业没有 sink；请先调用 print/sink_to/write_as_text")
        graph = build_graph(self._sink_nodes)
        execution = JobExecution(
            graph,
            checkpoint_storage=self.checkpoint_storage,
            checkpoint_config=self.checkpoint_config,
            checkpointing_enabled=self.checkpointing_enabled,
        )
        execution.metric_group.add_group("job").counter("restarts").inc(0)
        execution.run()
        self.last_execution = execution
        return execution

    def get_execution_plan(self) -> JobGraph:
        if not self._sink_nodes:
            raise RuntimeError("作业没有 sink")
        return build_graph(self._sink_nodes)

    def explain(self) -> str:
        return self.get_execution_plan().describe()

    def get_job_graph(self) -> JobGraph:
        return self.get_execution_plan()

    def create_execution(self, terminal: "DataStream | None" = None) -> JobExecution:
        terminals = [terminal.node] if terminal is not None else list(self._sink_nodes)
        if not terminals:
            raise RuntimeError("没有终止节点")
        return JobExecution(
            build_graph(terminals),
            checkpoint_storage=self.checkpoint_storage,
            checkpoint_config=self.checkpoint_config,
            checkpointing_enabled=self.checkpointing_enabled,
        )


class DataStream:
    def __init__(self, env: StreamExecutionEnvironment, node: Node) -> None:
        self.env = env
        self.node = node
        self.parallelism = node.parallelism
        self._is_broadcast = False

    # ---- 基本转换 ----

    def map(
        self,
        function: Callable[[Any], Any] | MapFunction,
        *,
        name: str = "map",
    ) -> "DataStream":
        fn = as_map_function(function)

        def factory(index: int, subtasks: int) -> MapOperator:
            return MapOperator(_clone(fn))

        return self._derive(name, factory)

    def filter(
        self,
        function: Callable[[Any], bool] | Any,
        *,
        name: str = "filter",
    ) -> "DataStream":
        fn = as_filter_function(function)

        def factory(index: int, subtasks: int) -> FilterOperator:
            return FilterOperator(_clone(fn))

        return self._derive(name, factory)

    def flat_map(
        self,
        function: Callable[[Any], Iterable[Any]] | FlatMapFunction | RichFlatMapFunction,
        *,
        name: str = "flat-map",
    ) -> "DataStream":
        fn = function if isinstance(function, (FlatMapFunction, RichFlatMapFunction)) else as_flat_map_function(function)

        def factory(index: int, subtasks: int) -> FlatMapOperator:
            return FlatMapOperator(_clone(fn))  # type: ignore[arg-type]

        return self._derive(name, factory)

    def key_by(
        self,
        selector: Callable[[Any], Any],
        *,
        name: str = "key-by",
        parallelism: int | None = None,
    ) -> "KeyedStream":
        par = parallelism or self.parallelism

        def factory(index: int, subtasks: int) -> KeyByOperator:
            return KeyByOperator(selector)

        node = make_node(
            name,
            factory,
            parallelism=par,
            inputs=[Edge(self.node, self._forward_partition(par), input_index=0)],
        )
        return KeyedStream(self.env, DataStream(self.env, node))

    def process(
        self,
        function: ProcessFunction,
        *,
        name: str = "process",
    ) -> "DataStream":
        def factory(index: int, subtasks: int) -> ProcessOperator:
            return ProcessOperator(_clone(function), keyed=False)

        return self._derive(name, factory)

    def assign_timestamps_and_watermarks(
        self, strategy: WatermarkStrategy, *, name: str = "watermarks"
    ) -> "DataStream":
        from .watermarks import TimestampAssignerOperator

        def factory(index: int, subtasks: int) -> TimestampAssignerOperator:
            return TimestampAssignerOperator(strategy)

        return self._derive(name, factory)

    def rebalance(self) -> "DataStream":
        def factory(index: int, subtasks: int) -> PassThroughOperator:
            return PassThroughOperator()

        return self._derive("rebalance", factory, partitioning=Partitioning.REBALANCE)

    def broadcast(self) -> "DataStream":
        stream = copy.copy(self)

        def factory(index: int, subtasks: int) -> PassThroughOperator:
            return PassThroughOperator()

        node = make_node(
            "broadcast",
            factory,
            parallelism=self.parallelism,
            inputs=[Edge(self.node, Partitioning.BROADCAST, input_index=0)],
        )
        node.is_broadcast = True
        stream = DataStream(self.env, node)
        stream._is_broadcast = True
        return stream

    def union(self, *others: "DataStream", name: str = "union") -> "DataStream":
        streams = [self, *others]
        for stream in streams:
            if stream.env is not self.env:
                raise ValueError("union 的流必须属于同一个执行环境")
        par = max(stream.parallelism for stream in streams)

        def factory(index: int, subtasks: int) -> PassThroughOperator:
            return PassThroughOperator()

        inputs = [
            Edge(stream.node, self._forward_partition(par), input_index=i)
            for i, stream in enumerate(streams)
        ]
        node = make_node(name, factory, parallelism=par, inputs=inputs)
        return DataStream(self.env, node)

    def connect(self, other: "DataStream") -> "ConnectedStreams":
        if other.env is not self.env:
            raise ValueError("connect 的流必须属于同一个执行环境")
        right = other.stream if isinstance(other, KeyedStream) else other
        return ConnectedStreams(
            self,
            right,
            left_keyed=False,
            right_keyed=isinstance(other, KeyedStream),
        )

    def join(self, other: "DataStream") -> "JoinBuilder":
        if other.env is not self.env:
            raise ValueError("join 的流必须属于同一个执行环境")
        return JoinBuilder(self, other)

    def window_all(self, assigner: WindowAssigner) -> "WindowedStream":
        keyed = self.key_by(lambda _: "__all__", parallelism=1)
        return keyed.window(assigner)

    def get_side_output(self, tag: OutputTag, *, name: str | None = None) -> "DataStream":
        def factory(index: int, subtasks: int) -> PassThroughOperator:
            return PassThroughOperator()

        node = make_node(
            name or f"side-output:{tag.id}",
            factory,
            parallelism=self.parallelism,
            inputs=[
                Edge(
                    self.node,
                    self._forward_partition(self.parallelism),
                    output_tag=tag.id,
                    input_index=0,
                )
            ],
        )
        return DataStream(self.env, node)

    def async_map(
        self,
        function: AsyncFunction | Callable[[Any], Any],
        *,
        capacity: int = 10,
        name: str = "async-map",
    ) -> "DataStream":
        def factory(index: int, subtasks: int) -> AsyncMapOperator:
            # 与其他算子一致：每个 subtask 一份独立函数实例，避免可变状态共享。
            return AsyncMapOperator(_clone(function), capacity=capacity)

        return self._derive(name, factory)

    # ---- 输出 ----

    def sink_to(
        self,
        sink_function: Callable[[Any], None],
        *,
        name: str = "sink",
        parallelism: int = 1,
    ) -> "DataStream":
        def factory() -> SinkOperator:
            return SinkOperator(sink_function, name=name)

        return self._add_sink_node(factory, name=name, parallelism=parallelism)

    def print(self, *, prefix: str = "") -> "DataStream":
        def factory() -> PrintSink:
            return PrintSink(prefix)

        return self._add_sink_node(factory, name="print", parallelism=1)

    def write_as_text(
        self,
        path: str,
        *,
        formatter: Callable[[Any], str] = str,
    ) -> "DataStream":
        def factory() -> TextFileSink:
            return TextFileSink(path, formatter)

        return self._add_sink_node(factory, name="text-file-sink", parallelism=1)

    def execute_and_collect(self) -> list[Any]:
        sink = CollectSink()

        def factory(index: int, subtasks: int) -> CollectSink:
            return sink

        node = make_node(
            "collect",
            factory,
            parallelism=1,
            inputs=[Edge(self.node, self._consume_partition(1), input_index=0)],
        )
        execution = JobExecution(
            build_graph([node]),
            checkpoint_storage=self.env.checkpoint_storage,
            checkpoint_config=self.env.checkpoint_config,
            checkpointing_enabled=self.env.checkpointing_enabled,
        )
        execution.run()
        self.env.last_execution = execution
        return sink.values

    def collect(self) -> list[Any]:
        return self.execute_and_collect()

    # ---- 元数据 ----

    def name(self, name: str) -> "DataStream":
        self.node.name = name
        return self

    def uid(self, uid: str) -> "DataStream":
        if not uid:
            raise ValueError("uid 不能为空")
        self.node.uid = uid
        return self

    def set_parallelism(self, parallelism: int) -> "DataStream":
        if parallelism <= 0:
            raise ValueError("并行度必须大于 0")
        self.node.parallelism = parallelism
        self.parallelism = parallelism
        return self

    def get_parallelism(self) -> int:
        return self.parallelism

    # ---- 内部 ----

    def _derive(
        self,
        name: str,
        factory: Callable[[int, int], Any],
        *,
        partitioning: Partitioning | None = None,
    ) -> "DataStream":
        node = make_node(
            name,
            factory,
            parallelism=self.parallelism,
            inputs=[
                Edge(
                    self.node,
                    partitioning or self._forward_partition(self.parallelism),
                    input_index=0,
                )
            ],
        )
        return DataStream(self.env, node)

    def _add_sink_node(
        self,
        factory: Callable[[], Any],
        *,
        name: str,
        parallelism: int,
    ) -> "DataStream":
        node = make_node(
            name,
            lambda index, subtasks: factory(),
            parallelism=parallelism,
            inputs=[Edge(self.node, self._consume_partition(parallelism), input_index=0)],
        )
        self.env._sink_nodes.append(node)
        return self

    def _forward_partition(self, target_parallelism: int) -> Partitioning:
        if self.parallelism == target_parallelism:
            return Partitioning.FORWARD
        return Partitioning.REBALANCE

    @staticmethod
    def _consume_partition(target_parallelism: int) -> Partitioning:
        # 上游多并行度汇聚到单并行度 sink 时必须 shuffle
        return Partitioning.REBALANCE if target_parallelism == 1 else Partitioning.REBALANCE

    def __repr__(self) -> str:
        return f"DataStream({self.node.name}, parallelism={self.parallelism})"


class KeyedStream:
    def __init__(self, env: StreamExecutionEnvironment, stream: DataStream) -> None:
        self.env = env
        self.stream = stream
        self.node = stream.node
        self.parallelism = stream.parallelism

    def reduce(
        self,
        function: Callable[[Any, Any], Any] | ReduceFunction,
        *,
        name: str = "reduce",
    ) -> DataStream:
        fn = as_reduce_function(function)
        return self._derive_keyed(name, lambda: ReduceOperator(_clone(fn)))

    def aggregate(self, function: AggregateFunction, *, name: str = "aggregate") -> DataStream:
        return self._derive_keyed(name, lambda: AggregateOperator(_clone(function)))

    def sum(self, field: str | int | None = None, *, name: str = "sum") -> DataStream:
        return self.aggregate(SumAggregate(field), name=name)

    def min(self, field: str | int | None = None, *, name: str = "min") -> DataStream:
        return self.aggregate(MinAggregate(field), name=name)

    def max(self, field: str | int | None = None, *, name: str = "max") -> DataStream:
        return self.aggregate(MaxAggregate(field), name=name)

    def count(self, *, name: str = "count") -> DataStream:
        return self.aggregate(CountAggregate(), name=name)

    def process(self, function: KeyedProcessFunction, *, name: str = "keyed-process") -> DataStream:
        return self._derive_keyed(name, lambda: ProcessOperator(_clone(function), keyed=True))

    def window(self, assigner: WindowAssigner) -> "WindowedStream":
        return WindowedStream(self, assigner)

    def connect(self, other: DataStream) -> "ConnectedStreams | BroadcastConnectedStreams":
        if getattr(other, "_is_broadcast", False):
            return BroadcastConnectedStreams(self, other)
        right = other.stream if isinstance(other, KeyedStream) else other
        return ConnectedStreams(
            self.stream,
            right,
            left_keyed=True,
            right_keyed=isinstance(other, KeyedStream),
        )

    def interval_join(self, other: "KeyedStream") -> "IntervalJoinBuilder":
        if other.env is not self.env:
            raise ValueError("两个流必须属于同一个执行环境")
        return IntervalJoinBuilder(self, other)

    def as_data_stream(self) -> DataStream:
        return self.stream

    def _derive_keyed(self, name: str, factory: Callable[[], Any]) -> DataStream:
        node = make_node(
            name,
            lambda index, subtasks: factory(),
            parallelism=self.parallelism,
            inputs=[Edge(self.node, Partitioning.HASH, input_index=0)],
        )
        return DataStream(self.env, node)


class _ReduceAggregate(AggregateFunction):
    def __init__(self, reduce_function: ReduceFunction) -> None:
        self.reduce_function = reduce_function

    def create_accumulator(self) -> Any:
        return None

    def add(self, value: Any, accumulator: Any) -> Any:
        if accumulator is None:
            return value
        return self.reduce_function.reduce(accumulator, value)

    def get_result(self, accumulator: Any) -> Any:
        return accumulator

    def merge(self, a: Any, b: Any) -> Any:
        if a is None:
            return b
        if b is None:
            return a
        return self.reduce_function.reduce(a, b)


class WindowedStream:
    def __init__(self, keyed: KeyedStream, assigner: WindowAssigner) -> None:
        self.keyed = keyed
        self.env = keyed.env
        self.assigner = assigner
        self.allowed_lateness_ms = 0
        self._trigger: Trigger | None = None
        self._evictor: Evictor | None = None
        self.late_tag: OutputTag | None = None

    def allowed_lateness(self, lateness: Duration | int) -> "WindowedStream":
        value = lateness.millis if isinstance(lateness, Duration) else int(lateness)
        if value < 0:
            raise ValueError("允许迟到时间不能为负数")
        result = copy.copy(self)
        result.allowed_lateness_ms = value
        return result

    def trigger(self, trigger: Trigger) -> "WindowedStream":
        result = copy.copy(self)
        result._trigger = trigger
        return result

    def evictor(self, evictor: Evictor) -> "WindowedStream":
        result = copy.copy(self)
        result._evictor = evictor
        return result

    def side_output_late_data(self, tag: OutputTag) -> "WindowedStream":
        result = copy.copy(self)
        result.late_tag = tag
        return result

    def reduce(self, function: Callable[[Any, Any], Any] | ReduceFunction, *, name: str = "window-reduce") -> DataStream:
        fn = as_reduce_function(function)
        return self._build(aggregate_function=_ReduceAggregate(fn), name=name)

    def aggregate(self, aggregate_function: AggregateFunction, *, name: str = "window-aggregate") -> DataStream:
        return self._build(aggregate_function=aggregate_function, name=name)

    def process(self, function: Any, *, name: str = "window-process") -> DataStream:
        return self._build(process_window_function=function, name=name)

    def apply(self, function: Any, *, name: str = "window-apply") -> DataStream:
        return self._build(window_function=function, name=name)

    def _build(
        self,
        *,
        aggregate_function: AggregateFunction | None = None,
        process_window_function: Any = None,
        window_function: Any = None,
        name: str,
    ) -> DataStream:
        if sum(x is not None for x in (aggregate_function, process_window_function, window_function)) != 1:
            raise ValueError("窗口必须且只能指定 aggregate/process/apply 之一")
        def make_operator() -> WindowOperator:
            return WindowOperator(
                _clone(self.assigner),
                aggregate_function=_clone(aggregate_function),
                process_window_function=_clone(process_window_function),
                window_function=_clone(window_function),
                trigger=_clone(self._trigger),
                evictor=_clone(self._evictor),
                allowed_lateness_ms=self.allowed_lateness_ms,
                late_output_tag=self.late_tag,
            )

        def factory(index: int, subtasks: int) -> WindowOperatorAdapter:
            return WindowOperatorAdapter(make_operator())

        node = make_node(
            name,
            factory,
            parallelism=self.keyed.parallelism,
            inputs=[Edge(self.keyed.node, Partitioning.HASH, input_index=0)],
        )
        return DataStream(self.env, node)


class ConnectedStreams:
    def __init__(
        self,
        left: DataStream,
        right: DataStream,
        *,
        left_keyed: bool = False,
        right_keyed: bool = False,
    ) -> None:
        self.left = left
        self.right = right
        self.left_keyed = left_keyed
        self.right_keyed = right_keyed
        self.env = left.env
        self.parallelism = max(left.parallelism, right.parallelism)

    def _partition(self, stream: DataStream, keyed: bool) -> Partitioning:
        # 键控输入必须按 key 哈希，否则同一 key 会落到不同 subtask，
        # 各自维护一份 keyed state，静默破坏“同 key 同 subtask”的语义。
        if keyed:
            return Partitioning.HASH
        return stream._forward_partition(self.parallelism)

    def process(self, function: CoProcessFunction, *, name: str = "co-process") -> DataStream:
        def factory(index: int, subtasks: int) -> CoProcessOperator:
            return CoProcessOperator(_clone(function))

        node = make_node(
            name,
            factory,
            parallelism=self.parallelism,
            inputs=[
                Edge(self.left.node, self._partition(self.left, self.left_keyed), input_index=0),
                Edge(self.right.node, self._partition(self.right, self.right_keyed), input_index=1),
            ],
        )
        return DataStream(self.env, node)


class BroadcastConnectedStreams:
    """键控流 + 广播流的连接。广播流每个子任务持有一份完整副本。"""

    def __init__(self, keyed: KeyedStream, broadcast: DataStream) -> None:
        self.keyed = keyed
        self.broadcast_stream = broadcast
        self.env = keyed.env
        self.parallelism = keyed.parallelism

    def process(self, function: Any, *, name: str = "broadcast-process") -> DataStream:
        from .functions import BroadcastProcessFunction

        if not isinstance(function, BroadcastProcessFunction):
            raise TypeError("广播连接需要 BroadcastProcessFunction")
        self.broadcast_stream.node.parallelism = self.parallelism
        self.broadcast_stream.parallelism = self.parallelism

        def factory(index: int, subtasks: int) -> BroadcastCoProcessOperator:
            return BroadcastCoProcessOperator(_clone(function))

        node = make_node(
            name,
            factory,
            parallelism=self.parallelism,
            inputs=[
                Edge(self.keyed.node, Partitioning.HASH, input_index=0),
                Edge(
                    self.broadcast_stream.node,
                    Partitioning.FORWARD,
                    input_index=1,
                ),
            ],
        )
        return DataStream(self.env, node)


class JoinBuilder:
    def __init__(self, left: DataStream, right: DataStream) -> None:
        self.left = left
        self.right = right

    def where(self, key_selector: Callable[[Any], Any]) -> "JoinWhereBuilder":
        return JoinWhereBuilder(self.left, self.right, key_selector)


class JoinWhereBuilder:
    def __init__(self, left: DataStream, right: DataStream, left_key: Callable[[Any], Any]) -> None:
        self.left = left
        self.right = right
        self.left_key = left_key

    def equal_to(self, right_key: Callable[[Any], Any]) -> "JoinWindowBuilder":
        return JoinWindowBuilder(self.left, self.right, self.left_key, right_key)


class JoinWindowBuilder:
    def __init__(
        self,
        left: DataStream,
        right: DataStream,
        left_key: Callable[[Any], Any],
        right_key: Callable[[Any], Any],
    ) -> None:
        self.left = left
        self.right = right
        self.left_key = left_key
        self.right_key = right_key

    def window(self, assigner: WindowAssigner) -> "JoinApplyBuilder":
        return JoinApplyBuilder(
            self.left, self.right, self.left_key, self.right_key, assigner
        )


class JoinApplyBuilder:
    def __init__(
        self,
        left: DataStream,
        right: DataStream,
        left_key: Callable[[Any], Any],
        right_key: Callable[[Any], Any],
        assigner: WindowAssigner,
    ) -> None:
        self.left = left
        self.right = right
        self.left_key = left_key
        self.right_key = right_key
        self.assigner = assigner

    def apply(self, join_function: Callable[[Any, Any], Any], *, name: str = "window-join") -> DataStream:
        left_keyed = self.left.key_by(self.left_key)
        right_keyed = self.right.key_by(self.right_key)
        parallelism = min(left_keyed.parallelism, right_keyed.parallelism)

        def factory(index: int, subtasks: int) -> JoinOperator:
            return JoinOperator(
                self.left_key,
                self.right_key,
                _clone(self.assigner),
                _clone(join_function),
            )

        node = make_node(
            name,
            factory,
            parallelism=parallelism,
            inputs=[
                Edge(left_keyed.node, Partitioning.HASH, input_index=0),
                Edge(right_keyed.node, Partitioning.HASH, input_index=1),
            ],
        )
        return DataStream(self.left.env, node)


class IntervalJoinBuilder:
    def __init__(self, left: KeyedStream, right: KeyedStream) -> None:
        self.left = left
        self.right = right
        self.lower_ms = 0
        self.upper_ms = 0

    def between(self, lower: Duration, upper: Duration) -> "IntervalJoinBuilder":
        self.lower_ms = lower.millis
        self.upper_ms = upper.millis
        if self.lower_ms > self.upper_ms:
            raise ValueError("区间下界不能大于上界")
        return self

    def process(self, join_function: Callable[[Any, Any], Any], *, name: str = "interval-join") -> DataStream:
        def factory(index: int, subtasks: int) -> IntervalJoinOperator:
            return IntervalJoinOperator(self.lower_ms, self.upper_ms, _clone(join_function))

        parallelism = min(self.left.parallelism, self.right.parallelism)
        node = make_node(
            name,
            factory,
            parallelism=parallelism,
            inputs=[
                Edge(self.left.node, Partitioning.HASH, input_index=0),
                Edge(self.right.node, Partitioning.HASH, input_index=1),
            ],
        )
        return DataStream(self.left.env, node)
