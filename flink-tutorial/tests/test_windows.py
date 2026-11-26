from miniflink import (
    AggregateFunction,
    CountEvictor,
    CountTrigger,
    Duration,
    GlobalWindows,
    ProcessWindowFunction,
    Record,
    SessionWindows,
    SlidingEventTimeWindows,
    StreamExecutionEnvironment,
    SumAggregate,
    TumblingEventTimeWindows,
    TumblingProcessingTimeWindows,
    WatermarkStrategy,
    Window,
)


def _stream(events):
    env = StreamExecutionEnvironment()
    return env.from_collection(
        events, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
    )


def test_tumbling_event_time_aggregate():
    result = (
        _stream(
            [
                Record(("a", 1), timestamp=1000),
                Record(("a", 2), timestamp=1500),
                Record(("a", 3), timestamp=2500),
            ]
        )
        .key_by(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    assert [(item.window.start, item.value) for item in result] == [(1000, 3), (2000, 3)]


def test_sliding_window_generates_overlaps():
    result = (
        _stream([Record(("a", 1), timestamp=1500)])
        .key_by(lambda row: row[0])
        .window(
            SlidingEventTimeWindows.of(
                Duration.of_seconds(2), Duration.of_seconds(1)
            )
        )
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    assert sorted(item.window.start for item in result) == [0, 1000]


def test_session_window_merges():
    result = (
        _stream(
            [
                Record(("a", 1), timestamp=1000),
                Record(("a", 2), timestamp=1500),
                Record(("a", 3), timestamp=5000),
            ]
        )
        .key_by(lambda row: row[0])
        .window(SessionWindows.with_gap(Duration.of_seconds(1)))
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    assert result[0].window == Window(1000, 2500)
    assert result[0].value == 3


def test_window_all_runs_with_single_parallelism():
    result = (
        _stream([Record(1, timestamp=1000), Record(2, timestamp=1100)])
        .window_all(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .aggregate(SumAggregate())
        .execute_and_collect()
    )
    assert result[0].value == 3


def test_count_trigger_fires_early():
    result = (
        _stream(
            [
                Record(("a", 1), timestamp=1000),
                Record(("a", 2), timestamp=1100),
                Record(("a", 3), timestamp=1200),
            ]
        )
        .key_by(lambda row: row[0])
        .window(GlobalWindows())
        .trigger(CountTrigger.of(2))
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    assert any(item.value == 3 for item in result)


class CollectWindow(ProcessWindowFunction):
    def process(self, key, window, values, ctx, out):
        out.collect((key, window.start, window.end, list(values)))


def test_process_window_function_and_evictor():
    result = (
        _stream(
            [
                Record(("a", 1), timestamp=1000),
                Record(("a", 2), timestamp=1100),
                Record(("a", 3), timestamp=1200),
            ]
        )
        .key_by(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .process(CollectWindow())
        .execute_and_collect()
    )
    assert result[0][3] == [("a", 1), ("a", 2), ("a", 3)]


def test_processing_time_window_flushes():
    result = (
        StreamExecutionEnvironment()
        .from_collection(
            [
                Record(("a", 1), timestamp=1),
                Record(("a", 2), timestamp=2),
            ]
        )
        .key_by(lambda row: row[0])
        .window(TumblingProcessingTimeWindows.of(Duration.of_seconds(1)))
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    assert result[0].value == 3


def test_allowed_lateness_keeps_window_until_cleanup():
    result = (
        _stream(
            [
                Record(("a", 1), timestamp=1000),
                Record(("a", 2), timestamp=2500),
                Record(("a", 3), timestamp=1200),
            ]
        )
        .key_by(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .allowed_lateness(1000)
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    # 窗口 [1000,2000) 先输出 1，迟到记录 3 到达后再次输出 4。
    values = [item.value for item in result if item.window.start == 1000]
    assert values == [1, 4]


def test_sliding_window_does_not_assign_non_containing_window():
    # size=10, slide=6, ts=11：只应属于 [6,16)，不应额外落入 [0,10)。
    result = (
        _stream([Record(("a", 1), timestamp=11)])
        .key_by(lambda row: row[0])
        .window(
            SlidingEventTimeWindows.of(
                Duration.of_millis(10), Duration.of_millis(6)
            )
        )
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    assert [(item.window.start, item.window.end) for item in result] == [(6, 16)]


def test_session_late_record_merges_into_longer_session():
    # b 的时间戳把 watermark 推到 21；a 的 ts=10 对原始 [10,20) 已晚，
    # 但它能与 [15,25) 合并成 [10,25)，因此不应被丢弃。
    result = (
        _stream(
            [
                Record(("a", 15), timestamp=15),
                Record(("b", 21), timestamp=21),
                Record(("a", 10), timestamp=10),
            ]
        )
        .key_by(lambda row: row[0])
        .window(SessionWindows.with_gap(Duration.of_millis(10)))
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    a_results = [item for item in result if item.key == "a"]
    assert [(item.window.start, item.window.end, item.value) for item in a_results] == [
        (10, 25, 25)
    ]


def test_processing_time_window_does_not_refire_every_record():
    result = (
        StreamExecutionEnvironment()
        .from_collection(
            [
                Record(("a", 1), timestamp=1),
                Record(("a", 2), timestamp=2),
                Record(("a", 3), timestamp=15),
                Record(("a", 4), timestamp=16),
            ]
        )
        .key_by(lambda row: row[0])
        .window(TumblingProcessingTimeWindows.of(Duration.of_millis(10)))
        .allowed_lateness(100)
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    zero_window = [item for item in result if item.window.start == 0]
    assert [item.value for item in zero_window] == [3]


class OpenAwareSum(AggregateFunction):
    def open(self, context):
        self.base = 10

    def create_accumulator(self):
        return 0

    def add(self, value, accumulator):
        return accumulator + value[1] + getattr(self, "base", 0)

    def get_result(self, accumulator):
        return accumulator


def test_window_aggregate_receives_open_lifecycle():
    result = (
        _stream([Record(("a", 0), timestamp=1000)])
        .key_by(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .aggregate(OpenAwareSum())
        .execute_and_collect()
    )
    assert [item.value for item in result] == [10]


class EmitRecords(ProcessWindowFunction):
    def process(self, key, window, values, ctx, out):
        out.collect(list(values))


def test_count_evictor_after_fire_keeps_current_records():
    result = (
        _stream(
            [
                Record(("a", 1), timestamp=1000),
                Record(("a", 2), timestamp=1000),
                Record(("a", 3), timestamp=1000),
                Record(("a", 4), timestamp=1000),
            ]
        )
        .key_by(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .trigger(CountTrigger.of(2))
        .evictor(CountEvictor(1, do_evict_after=True))
        .process(EmitRecords())
        .execute_and_collect()
    )
    assert result == [[("a", 1), ("a", 2)], [("a", 2), ("a", 3), ("a", 4)]]


def test_session_fired_then_late_merge_refires():
    result = (
        _stream(
            [
                Record(("a", 5), timestamp=5),
                Record(("a", 9), timestamp=9),
                Record(("b", 20), timestamp=20),
                Record(("a", 12), timestamp=12),
            ]
        )
        .key_by(lambda row: row[0])
        .window(SessionWindows.with_gap(Duration.of_millis(10)))
        .allowed_lateness(10)
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    a_values = [item.value for item in result if item.key == "a"]
    assert a_values == [14, 26]


def test_session_late_bridge_merges_chained_sessions():
    result = (
        _stream(
            [
                Record(("a", 5), timestamp=5),
                Record(("a", 9), timestamp=9),
                Record(("b", 25), timestamp=25),
                Record(("a", 25), timestamp=25),
                Record(("a", 16), timestamp=16),
            ]
        )
        .key_by(lambda row: row[0])
        .window(SessionWindows.with_gap(Duration.of_millis(10)))
        .allowed_lateness(10)
        .aggregate(SumAggregate(1))
        .execute_and_collect()
    )
    a_values = [item.value for item in result if item.key == "a"]
    assert a_values[-1] == 55
