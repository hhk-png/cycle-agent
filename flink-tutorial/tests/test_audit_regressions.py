"""独立审计发现的语义回归测试。

这些用例刻意不依赖内部实现细节，只通过公开 API 复现审计中的反例，
防止后续修改把已修复的行为改回去。
"""

import json

import pytest

from miniflink import (
    AggregateFunction,
    AggregatingStateDescriptor,
    AsyncFunction,
    BroadcastProcessFunction,
    CoProcessFunction,
    CountAggregate,
    CountTrigger,
    Duration,
    EventTimeTrigger,
    GlobalWindows,
    InMemoryKafka,
    KafkaSource,
    KeyedProcessFunction,
    ListStateDescriptor,
    MapStateDescriptor,
    OutputTag,
    Pattern,
    ProcessFunction,
    ProcessWindowFunction,
    Record,
    ReduceFunction,
    RuntimeContext,
    StreamExecutionEnvironment,
    SumAggregate,
    TableEnvironment,
    TimeEvictor,
    TumblingEventTimeWindows,
    ValueStateDescriptor,
    Watermark,
    WatermarkStrategy,
    pattern,
)
from miniflink.cep import CepOperator
from miniflink.cli import main as cli_main
from miniflink.operators import CollectSink, IntervalJoinOperator, JoinOperator


class ConnectedCounter(CoProcessFunction):
    def process_element1(self, value, ctx, out):
        state = ctx.get_state(ValueStateDescriptor("count", lambda: 0))
        state.update(state.get() + 1)
        out.collect((value, state.get()))

    def process_element2(self, value, ctx, out):
        pass


def test_keyed_connect_hashes_keyed_side():
    env = StreamExecutionEnvironment(parallelism=2)
    left = env.from_collection(["a", "a", "b", "b"], parallelism=2)
    right = env.from_collection([], parallelism=2)
    result = (
        left.key_by(lambda value: value)
        .connect(right)
        .process(ConnectedCounter())
        .execute_and_collect()
    )
    assert sorted(result) == [("a", 1), ("a", 2), ("b", 1), ("b", 2)]


class SideWindow(ProcessWindowFunction):
    def process(self, key, window, values, ctx, out):
        ctx.output(OutputTag("win-side"), (key, window.start))
        out.collect((key, window.start))


class CaptureMeta(ProcessFunction):
    def process_element(self, value, ctx, out):
        out.collect((value, ctx.current_key, ctx.timestamp))


def test_window_side_output_carries_window_metadata():
    env = StreamExecutionEnvironment()
    strategy = WatermarkStrategy.for_bounded_out_of_orderness(0)
    stream = env.from_collection(
        [Record(("b", 1), timestamp=100), Record(("a", 1), timestamp=50)],
        watermark_strategy=strategy,
    )
    result = (
        stream.key_by(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .process(SideWindow())
    )
    side = (
        result.get_side_output(OutputTag("win-side"))
        .process(CaptureMeta())
        .execute_and_collect()
    )
    assert sorted(side) == [
        (("a", 0), "a", 999),
        (("b", 0), "b", 999),
    ]


class ChainedTimer(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        out.collect(("e", value))
        ctx.timer_service.register_event_time_timer(1500, ctx.current_key)

    def on_timer(self, timestamp, ctx, out):
        out.collect(("t", timestamp))
        if timestamp == 1500:
            ctx.timer_service.register_event_time_timer(1600, ctx.current_key)


def test_chained_event_timers_fire_in_same_advance():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection(
            [Record(("a", 1), timestamp=1000)],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda row: row[0])
        .process(ChainedTimer())
        .execute_and_collect()
    )
    assert [item for item in result if item[0] == "t"] == [("t", 1500), ("t", 1600)]


def test_count_trigger_does_not_fire_on_cleanup():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection(
            [Record(("a", 1), timestamp=100)],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda row: row[0])
        .window(GlobalWindows())
        .trigger(CountTrigger.of(3))
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    assert result == []


def test_tumble_start_handles_float_timestamp():
    env = TableEnvironment()
    env.from_elements([{"ts": 1500.5}], name="events")
    rows = env.sql("SELECT TUMBLE_START(ts, 1000) AS w FROM events").execute()
    assert rows == [{"w": 1000}]


def test_cli_csv_infers_numeric_types(tmp_path, capsys):
    path = tmp_path / "amounts.csv"
    path.write_text("name,amount\na,9\nb,10\nc,2\n", encoding="utf-8")
    code = cli_main(
        [
            "sql",
            "--table",
            f"t={path}",
            "--query",
            "SELECT name, amount FROM t WHERE amount >= 9 ORDER BY amount",
        ]
    )
    assert code == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["name"] for row in rows] == ["a", "b"]


class MutatingState(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        state = ctx.get_state(ValueStateDescriptor("state", lambda: {}))
        state.update({"n": 1})
        leaked = state.get()
        leaked["n"] = 99
        out.collect(state.get())


def test_value_state_get_returns_snapshot():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([Record(("a", 1), timestamp=1)])
        .key_by(lambda row: row[0])
        .process(MutatingState())
        .execute_and_collect()
    )
    assert result == [{"n": 1}]


class StatefulWindow(ProcessWindowFunction):
    def process(self, key, window, values, ctx, out):
        state = ctx.window_state()
        state["n"] = state.get("n", 0) + 1
        out.collect((key, window.start, state["n"], ctx.timestamp))


def test_window_state_persists_across_refires():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection(
            [
                Record(("a", 1), timestamp=100),
                Record(("a", 2), timestamp=500),
                Record(("a", 3), timestamp=1500),
                Record(("a", 4), timestamp=600),
            ],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .allowed_lateness(Duration.of_seconds(1))
        .process(StatefulWindow())
        .execute_and_collect()
    )
    assert result == [("a", 0, 1, 999), ("a", 0, 2, 999), ("a", 1000, 1, 1999)]


def test_window_side_output_reaches_downstream_window():
    env = StreamExecutionEnvironment()
    tag = OutputTag("win-side")
    result = (
        env.from_collection(
            [Record(("b", 1), timestamp=100), Record(("a", 1), timestamp=50)],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .process(SideWindow())
    )
    downstream = (
        result.get_side_output(tag)
        .key_by(lambda value: value[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .aggregate(CountAggregate())
        .execute_and_collect()
    )
    assert sorted((item.key, item.window.start, item.value) for item in downstream) == [
        ("a", 0, 1),
        ("b", 0, 1),
    ]


def test_kafka_source_snapshot_keeps_pending_watermark():
    bus = InMemoryKafka()
    bus.create_topic("in")
    bus.publish("in", "a", timestamp=2000)
    strategy = WatermarkStrategy.for_bounded_out_of_orderness(0)
    source = KafkaSource(bus, "in", "g", 0, 1, strategy=strategy)
    assert isinstance(source.next_element(), Record)
    snapshot = source.snapshot()
    restored = KafkaSource(bus, "in", "g", 0, 1, strategy=strategy)
    restored.restore(snapshot)
    element = restored.next_element()
    assert isinstance(element, Watermark)
    assert element.timestamp == 2000


class CaptureTimestamp(ProcessFunction):
    def process_element(self, value, ctx, out):
        out.collect((value, ctx.timestamp))


def test_window_join_output_uses_window_max_timestamp():
    env = StreamExecutionEnvironment()
    left = env.from_collection(
        [Record(("k", "L"), timestamp=1000)],
        watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
    )
    right = env.from_collection(
        [Record(("k", "R"), timestamp=1500)],
        watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
    )
    result = (
        left.join(right)
        .where(lambda row: row[0])
        .equal_to(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(5)))
        .apply(lambda l, r: (l[1], r[1]))
        .process(CaptureTimestamp())
        .execute_and_collect()
    )
    assert result == [(("L", "R"), 4999)]


def test_interval_join_drops_records_below_watermark():
    op = IntervalJoinOperator(0, 1000, lambda l, r: (l, r))
    op.process_record(Record(("k", "r"), 5000, key="k"), input_index=1)
    op.process_watermark(5000)
    assert op.process_record(Record(("k", "l"), 4000, key="k"), input_index=0) == []


def test_interval_join_output_uses_max_timestamp():
    op = IntervalJoinOperator(0, 5000, lambda l, r: (l, r))
    op.process_record(Record(("k", "r"), 2000, key="k"), input_index=1)
    output = op.process_record(Record(("k", "l"), 1000, key="k"), input_index=0)
    assert len(output) == 1
    assert output[0].timestamp == 2000


def test_single_step_one_or_more_accumulates_prefixes():
    env = StreamExecutionEnvironment()
    events = [
        Record(("u", 1), timestamp=1000),
        Record(("u", 2), timestamp=1100),
        Record(("u", 3), timestamp=1200),
    ]
    p = Pattern.begin("x").where(lambda event: True).one_or_more()
    result = (
        pattern(
            env.from_collection(
                events, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
            ).key_by(lambda event: event[0]),
            p,
        )
        .select(lambda match: match.all("x"))
        .execute_and_collect()
    )
    assert result == [[("u", 1)], [("u", 1), ("u", 2)], [("u", 1), ("u", 2), ("u", 3)]]


def test_global_windows_without_trigger_emits_nothing():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection(
            [Record(("a", 1), timestamp=1000)],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda row: row[0])
        .window(GlobalWindows())
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    assert result == []


# ---- 2026-11 第二轮独立审计：以下反例在修复前均会失败 ----


def test_collect_sink_restore_keeps_writing_to_restored_values():
    sink = CollectSink()
    sink.open(None)
    sink.process_record(Record("a"))
    snapshot = sink.snapshot()

    restored = CollectSink()
    restored.open(None)
    restored.restore(snapshot)
    restored.process_record(Record("b"))
    # 修复前 restore 会整体替换 values，绑定方法仍指向旧列表，'b' 丢失。
    assert restored.values == ["a", "b"]


class ChainedProcessingTimer(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        ctx.timer_service.register_processing_time_timer(0, ctx.current_key)

    def on_timer(self, timestamp, ctx, out):
        out.collect(("timer", timestamp))
        if timestamp < 2:
            ctx.timer_service.register_processing_time_timer(
                timestamp + 1, ctx.current_key
            )


def test_chained_processing_timers_fire_in_same_tick():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([Record(("a", 1), timestamp=1)])
        .key_by(lambda row: row[0])
        .process(ChainedProcessingTimer())
        .execute_and_collect()
    )
    # 修复前只会触发 0 和 1，2 要等下一条记录或永远不触发。
    assert result == [("timer", 0), ("timer", 1), ("timer", 2)]


class CollectWindowValues(ProcessWindowFunction):
    def process(self, key, window, values, ctx, out):
        out.collect((window.start, list(values)))


def test_allowed_lateness_does_not_bypass_count_trigger():
    env = StreamExecutionEnvironment()
    events = [
        Record(("a", 1), timestamp=100),
        Record(("a", 2), timestamp=200),
        Record(("a", 3), timestamp=1100),  # 把 watermark 推到 1100
        Record(("a", 4), timestamp=150),   # 迟到但仍在 allowedLateness 内
    ]
    result = (
        env.from_collection(
            events, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
        )
        .key_by(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .allowed_lateness(Duration.of_millis(500))
        .trigger(CountTrigger.of(5))
        .process(CollectWindowValues())
        .execute_and_collect()
    )
    # 窗口内只有 3 条记录，CountTrigger(5) 返回 CONTINUE；修复前会被
    # allowedLateness 分支无条件强制发射。
    assert result == []


class ValuesOf(ProcessWindowFunction):
    def process(self, key, window, values, ctx, out):
        out.collect([value["v"] for value in values])


def test_time_evictor_uses_record_timestamps():
    env = StreamExecutionEnvironment()
    events = [Record({"v": i, "ts": 1000 + i}, timestamp=1000 + i) for i in range(10)]
    result = (
        env.from_collection(
            events, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
        )
        .key_by(lambda row: "a")
        .window(TumblingEventTimeWindows.of(Duration.of_millis(100_000)))
        .evictor(TimeEvictor.of(Duration.of_millis(3)))
        .process(ValuesOf())
        .execute_and_collect()
    )
    # 修复前 records 只存 value，TimeEvictor 读不到 timestamp，等于不驱逐。
    assert result == [[6, 7, 8, 9]]


class MutatingMapState(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        state = ctx.get_map_state(MapStateDescriptor("m", dict))
        state.put("k", [1])
        leaked = state.get("k")
        leaked.append(2)
        out.collect(state.get("k"))


def test_map_state_get_returns_snapshot():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([Record(("a", 1), timestamp=1)])
        .key_by(lambda row: row[0])
        .process(MutatingMapState())
        .execute_and_collect()
    )
    assert result == [[1]]


def test_runtime_context_processing_time_uses_injected_clock():
    ctx = RuntimeContext(clock=lambda: 12_345)
    assert ctx.processing_time() == 12_345


class StatefulAsync(AsyncFunction):
    def __init__(self):
        self.seen = []

    def async_invoke(self, value):
        self.seen.append(value)
        return value


def test_async_function_instance_is_cloned_per_subtask():
    env = StreamExecutionEnvironment(parallelism=2)
    env.from_collection([1, 2], parallelism=2).async_map(
        StatefulAsync(), capacity=2
    ).print()
    execution = env.create_execution()
    async_tasks = [
        task for task in execution.tasks if task.node.name == "async-map"
    ]
    assert len(async_tasks) == 2
    assert async_tasks[0].operator.function is not async_tasks[1].operator.function


# ---- 2026-11 第三轮审计：窗口 join 与 CEP 的长期状态卫生 ----


def test_window_join_drops_late_records_and_prunes_emitted_tokens():
    op = JoinOperator(
        lambda row: row[0],
        lambda row: row[0],
        TumblingEventTimeWindows.of(Duration.of_seconds(1)),
        lambda left, right: (left, right),
    )
    op.open(None)
    op.process_record(Record(("k", "L"), 1000, key="k"), input_index=0)
    op.process_record(Record(("k", "R"), 1100, key="k"), input_index=1)
    emitted = op.process_watermark(2000)
    assert any(isinstance(item, Record) for item in emitted)

    # watermark 已越过 [1000, 2000)：这条记录属于迟到数据，不能入状态，
    # 也不能依赖 emitted token 永久占内存来阻止它再次发射。
    assert op.process_record(
        Record(("k", "LATE"), 1500, key="k"), input_index=0
    ) == []
    assert op.emitted == set()
    assert op.left == {}
    assert op.right == {}


def test_cep_drops_empty_key_state():
    pattern = Pattern.begin("hit").where(lambda value: value == "hit")
    op = CepOperator(pattern, lambda match: match["hit"])
    op.open(None)

    op.process_record(Record("miss", 1000, key="idle"), input_index=0)

    # 不匹配的 key 不应留下空 partial 列表或只增不减的位置计数器。
    assert op.partials == {}
    assert op.positions == {}


# ---- 2026-11 第四轮审计：窗口快照、GlobalWindows、CEP 清理、状态别名与生命周期 ----


def test_window_aggregate_emits_snapshot_not_live_accumulator():
    class ListAcc(AggregateFunction):
        def create_accumulator(self):
            return []

        def add(self, value, accumulator):
            accumulator.append(value[1])
            return accumulator

        def get_result(self, accumulator):
            # 合法的 Flink 用法：直接返回 accumulator 本身
            return accumulator

        def merge(self, a, b):
            a.extend(b)
            return a

    env = StreamExecutionEnvironment()
    result = (
        env.from_collection(
            [
                Record(("a", 1), 100),
                Record(("a", 3), 1500),
                Record(("a", 2), 120),
            ],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda record: record[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .allowed_lateness(1000)
        .aggregate(ListAcc())
        .execute_and_collect()
    )
    # 第一次发射必须是 [1] 的快照；迟到记录只能影响后续发射。
    assert [item.value for item in result] == [[1], [1, 2], [3]]


def test_cep_within_cleanup_prunes_positions():
    pattern = (
        Pattern.begin("a")
        .where(lambda value: value == "hit")
        .next("b")
        .where(lambda value: value == "hit")
        .within(500)
    )
    op = CepOperator(pattern, lambda match: match["a"])
    op.open(None)

    for index in range(5):
        op.process_record(Record("hit", 1000, key=f"k{index}"))

    # watermark 推进到 2000，cutoff=1500，所有 partial 都被清掉；
    # positions 不能留下每个 key 的计数。
    op.process_watermark(2000)
    assert op.partials == {}
    assert op.positions == {}
    assert op.snapshot()["positions"] == {}


def test_global_windows_event_time_trigger_fires_at_end_of_stream():
    result = (
        StreamExecutionEnvironment()
        .from_collection(
            [Record(("a", 1), 1000), Record(("a", 2), 1100)],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda record: record[0])
        .window(GlobalWindows())
        .trigger(EventTimeTrigger.create())
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    # GlobalWindows 默认 NeverTrigger；显式挂 EventTimeTrigger 时，
    # 结束时的 MAX_WATERMARK 必须能触发一次。
    assert [item.value for item in result] == [3]


def test_state_write_path_is_snapshotted():
    class ListAgg(AggregateFunction):
        def create_accumulator(self):
            return []

        def add(self, value, accumulator):
            accumulator.append(value)
            return accumulator

        def get_result(self, accumulator):
            return accumulator

        def merge(self, a, b):
            a.extend(b)
            return a

    class Mutator(KeyedProcessFunction):
        def process_element(self, value, ctx, out):
            value_state = ctx.get_state(ValueStateDescriptor("v", lambda: None))
            obj = {"n": 1}
            value_state.update(obj)
            obj["n"] = 99
            out.collect(("value", value_state.get()))

            list_state = ctx.get_list_state(ListStateDescriptor("l", list))
            item = [1]
            list_state.add(item)
            item.append(2)
            out.collect(("list", list_state.get()))

            map_state = ctx.get_map_state(MapStateDescriptor("m", dict))
            map_value = [1]
            map_state.put("k", map_value)
            map_value.append(2)
            out.collect(("map", map_state.get("k")))

            agg = ctx.get_aggregating_state(
                AggregatingStateDescriptor("agg", ListAgg(), list)
            )
            agg.add(1)
            snapshot = agg.get()
            snapshot.append("leak")
            out.collect(("agg", agg.get()))

    result = (
        StreamExecutionEnvironment()
        .from_collection([Record("x", 1)])
        .key_by(lambda value: "k")
        .process(Mutator())
        .execute_and_collect()
    )
    assert result == [
        ("value", {"n": 1}),
        ("list", [[1]]),
        ("map", [1]),
        ("agg", [1]),
    ]


_LIFECYCLE_EVENTS: list[str] = []


def test_close_lifecycle_for_all_function_types():
    _LIFECYCLE_EVENTS.clear()

    class R(ReduceFunction):
        def reduce(self, a, b):
            return a + b

        def open(self, context):
            _LIFECYCLE_EVENTS.append("reduce-open")

        def close(self):
            _LIFECYCLE_EVENTS.append("reduce-close")

    class A(AggregateFunction):
        def create_accumulator(self):
            return 0

        def add(self, value, accumulator):
            return accumulator + value

        def get_result(self, accumulator):
            return accumulator

        def merge(self, a, b):
            return a + b

        def open(self, context):
            _LIFECYCLE_EVENTS.append("agg-open")

        def close(self):
            _LIFECYCLE_EVENTS.append("agg-close")

    class C(CoProcessFunction):
        def process_element1(self, value, ctx, out):
            out.collect(value)

        def process_element2(self, value, ctx, out):
            out.collect(value)

        def open(self, context):
            _LIFECYCLE_EVENTS.append("co-open")

        def close(self):
            _LIFECYCLE_EVENTS.append("co-close")

    class B(BroadcastProcessFunction):
        def process_element(self, value, ctx, out):
            out.collect(value)

        def process_broadcast_element(self, value, ctx, out):
            out.collect(value)

        def open(self, context):
            _LIFECYCLE_EVENTS.append("broadcast-open")

        def close(self):
            _LIFECYCLE_EVENTS.append("broadcast-close")

    class Async(AsyncFunction):
        def async_invoke(self, value):
            return value

        def open(self, context):
            _LIFECYCLE_EVENTS.append("async-open")

        def close(self):
            _LIFECYCLE_EVENTS.append("async-close")

    env = StreamExecutionEnvironment()
    env.from_collection([1, 2]).key_by(lambda value: "k").reduce(R()).print()
    env.from_collection([1, 2]).key_by(lambda value: "k").aggregate(A()).print()
    left = env.from_collection([1])
    right = env.from_collection([2])
    left.connect(right).process(C()).print()
    main = env.from_collection([1]).key_by(lambda value: "k")
    rules = env.from_collection([("r", 1)])
    main.connect(rules.broadcast()).process(B()).print()
    env.from_collection([1]).async_map(Async()).print()
    env.execute()

    assert sorted(_LIFECYCLE_EVENTS) == sorted(
        [
            "reduce-open",
            "reduce-close",
            "agg-open",
            "agg-close",
            "co-open",
            "co-close",
            "broadcast-open",
            "broadcast-close",
            "async-open",
            "async-close",
        ]
    )


def test_table_rejects_heterogeneous_rows():
    env = TableEnvironment()
    with pytest.raises(ValueError, match="统一 schema"):
        env.from_elements([{"a": 1}, {"a": 2, "b": 3}], name="heterogeneous")


def test_table_rejects_zero_column_input():
    env = TableEnvironment()
    with pytest.raises(ValueError, match="至少需要一列"):
        env.from_data_stream(
            StreamExecutionEnvironment().from_collection([]),
            name="empty",
        )
