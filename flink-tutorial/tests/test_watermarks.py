import pytest

from miniflink import (
    Duration,
    OutputTag,
    ProcessWindowFunction,
    Record,
    SlidingEventTimeWindows,
    StreamExecutionEnvironment,
    SumAggregate,
    TumblingEventTimeWindows,
    WatermarkStrategy,
    Window,
)
from miniflink.watermarks import WatermarkGenerator


def test_monotonous_strategy_assigns_existing_timestamp():
    strategy = WatermarkStrategy.for_monotonous_timestamps()
    env = StreamExecutionEnvironment()
    out = env.from_collection(
        [Record(("a", 1), timestamp=1000)], watermark_strategy=strategy
    ).execute_and_collect()
    assert out == [("a", 1)]


def test_custom_timestamp_assigner():
    strategy = WatermarkStrategy.for_bounded_out_of_orderness(
        0, timestamp_assigner=lambda row: row["ts"]
    )
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([{"ts": 100, "v": 1}], watermark_strategy=strategy)
        .map(lambda row: row["v"])
        .execute_and_collect()
    )
    assert result == [1]


def test_missing_timestamp_raises_clear_error():
    env = StreamExecutionEnvironment()
    with pytest.raises(Exception):
        env.from_collection([object()], watermark_strategy=WatermarkStrategy.for_monotonous_timestamps()).execute_and_collect()


def test_watermark_generator_bounded_out_of_orderness():
    generator = WatermarkGenerator(
        WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_millis(500))
    )
    assert generator.on_record(1000) == 500
    assert generator.on_record(1200) == 700
    assert generator.on_record(1100) is None


def test_watermark_generator_periodic_interval():
    strategy = WatermarkStrategy.for_bounded_out_of_orderness(0)
    strategy.periodic_interval_records = 2
    generator = WatermarkGenerator(strategy)
    assert generator.on_record(100) is None
    assert generator.on_record(200) == 200


def test_assign_timestamps_and_watermarks_operator():
    env = StreamExecutionEnvironment()
    stream = env.from_collection(
        [{"ts": 1000, "v": 1}, {"ts": 2000, "v": 2}]
    ).assign_timestamps_and_watermarks(
        WatermarkStrategy.for_bounded_out_of_orderness(
            0, timestamp_assigner=lambda row: row["ts"]
        )
    )
    result = (
        stream.key_by(lambda row: "all")
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .reduce(lambda a, b: {"ts": a["ts"], "v": a["v"] + b["v"]})
        .execute_and_collect()
    )
    assert sorted(window.window for window in result) == [Window(1000, 2000), Window(2000, 3000)]


def test_watermark_is_min_across_union_inputs():
    env = StreamExecutionEnvironment()
    left = env.from_collection(
        [Record(1, timestamp=1000)],
        watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
    )
    right = env.from_collection(
        [Record(2, timestamp=500)],
        watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
    )
    union = left.union(right).key_by(lambda value: "k")
    windows = (
        union.window(TumblingEventTimeWindows.of(Duration.of_millis(400)))
        .aggregate(SumAggregate())
        .execute_and_collect()
    )
    # 窗口 400-800 在 watermark 达到 500 后不会结束；所有窗口最终由结束信号冲刷。
    assert windows
    assert sum(item.value for item in windows) == 3


class KeepLate(ProcessWindowFunction):
    def process(self, key, window, values, ctx, out):
        out.collect((key, window.start, list(values)))


def test_late_record_goes_to_side_output():
    env = StreamExecutionEnvironment()
    data = [
        Record(("a", 1), timestamp=100),
        Record(("a", 2), timestamp=5000),
        Record(("a", 3), timestamp=100),
    ]
    stream = (
        env.from_collection(
            data, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
        )
        .key_by(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .allowed_lateness(100)
        .side_output_late_data(OutputTag("late"))
        .process(KeepLate())
    )
    late = stream.get_side_output(OutputTag("late"))
    assert late.execute_and_collect() == [("a", 3)]


def test_sliding_late_side_output_only_when_all_windows_late():
    env = StreamExecutionEnvironment()
    data = [
        Record(("a", 1), timestamp=5),
        Record(("b", 1), timestamp=100),
        Record(("a", 2), timestamp=5),
    ]
    stream = (
        env.from_collection(
            data, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
        )
        .key_by(lambda row: row[0])
        .window(
            SlidingEventTimeWindows.of(
                Duration.of_millis(20), Duration.of_millis(10)
            )
        )
        .side_output_late_data(OutputTag("late"))
        .aggregate(SumAggregate(1))
    )
    late = stream.get_side_output(OutputTag("late"))
    assert late.execute_and_collect() == [("a", 2)]


def test_sliding_element_partially_late_is_not_side_output():
    env = StreamExecutionEnvironment()
    data = [
        Record(("b", 1), timestamp=25),
        Record(("a", 1), timestamp=15),
    ]
    stream = (
        env.from_collection(
            data, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
        )
        .key_by(lambda row: row[0])
        .window(
            SlidingEventTimeWindows.of(
                Duration.of_millis(20), Duration.of_millis(10)
            )
        )
        .side_output_late_data(OutputTag("late"))
        .aggregate(SumAggregate(1))
    )
    late = stream.get_side_output(OutputTag("late"))
    assert late.execute_and_collect() == []
