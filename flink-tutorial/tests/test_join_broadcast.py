import pytest

from miniflink import (
    BroadcastProcessFunction,
    CoProcessFunction,
    Duration,
    MapStateDescriptor,
    Record,
    StreamExecutionEnvironment,
    TumblingEventTimeWindows,
    WatermarkStrategy,
)


def _records():
    return [
        Record(("k", 1), timestamp=1000),
        Record(("k", 2), timestamp=1500),
    ]


def test_window_join_emits_cartesian_product():
    env = StreamExecutionEnvironment()
    left = env.from_collection(
        _records(), watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
    )
    right = env.from_collection(
        [Record(("k", "a"), 1100), Record(("k", "b"), 1200)],
        watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
    )
    result = (
        left.join(right)
        .where(lambda row: row[0])
        .equal_to(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .apply(lambda l, r: (l[1], r[1]))
        .execute_and_collect()
    )
    assert sorted(result) == [(1, "a"), (1, "b"), (2, "a"), (2, "b")]


def test_window_join_without_matching_keys_is_empty():
    env = StreamExecutionEnvironment()
    left = env.from_collection(
        _records(), watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
    )
    right = env.from_collection(
        [Record(("other", "a"), 1100)],
        watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
    )
    result = (
        left.join(right)
        .where(lambda row: row[0])
        .equal_to(lambda row: row[0])
        .window(TumblingEventTimeWindows.of(Duration.of_seconds(1)))
        .apply(lambda l, r: (l, r))
        .execute_and_collect()
    )
    assert result == []


def test_interval_join_matches_time_range():
    env = StreamExecutionEnvironment()
    left = env.from_collection(
        [Record(("k", "pay"), 1000)],
        watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
    ).key_by(lambda row: row[0])
    right = env.from_collection(
        [Record(("k", "ship"), 2000), Record(("k", "late"), 9000)],
        watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
    ).key_by(lambda row: row[0])
    result = (
        left.interval_join(right)
        .between(Duration.of_seconds(0), Duration.of_seconds(2))
        .process(lambda l, r: (l[1], r[1]))
        .execute_and_collect()
    )
    assert result == [("pay", "ship")]


def test_interval_join_rejects_inverted_bounds():
    env = StreamExecutionEnvironment()
    left = env.from_collection([1]).key_by(lambda value: value)
    right = env.from_collection([1]).key_by(lambda value: value)
    with pytest.raises(ValueError):
        left.interval_join(right).between(Duration.of_seconds(2), Duration.of_seconds(1))


class Pairer(CoProcessFunction):
    def process_element1(self, value, ctx, out):
        out.collect(("left", value))

    def process_element2(self, value, ctx, out):
        out.collect(("right", value))


def test_connect_co_process():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([1, 2])
        .connect(env.from_collection(["a", "b"]))
        .process(Pairer())
        .execute_and_collect()
    )
    assert sorted(result) == [("left", 1), ("left", 2), ("right", "a"), ("right", "b")]


class Enrich(BroadcastProcessFunction):
    def process_element(self, value, ctx, out):
        state = ctx.get_broadcast_state(MapStateDescriptor("rules", dict))
        out.collect((value, state.get(value)))

    def process_broadcast_element(self, value, ctx, out):
        state = ctx.get_broadcast_state(MapStateDescriptor("rules", dict))
        state.put(*value)


def test_broadcast_state_enriches_main_stream():
    env = StreamExecutionEnvironment()
    main = env.from_collection(["a", "b"])
    rules = env.from_collection([("a", 1), ("b", 2)])
    result = (
        main.key_by(lambda value: value)
        .connect(rules.broadcast())
        .process(Enrich())
        .execute_and_collect()
    )
    assert sorted(result) == [("a", 1), ("b", 2)]


def test_broadcast_requires_broadcast_process_function():
    env = StreamExecutionEnvironment()
    main = env.from_collection([1]).key_by(lambda value: value)
    with pytest.raises(TypeError):
        main.connect(env.from_collection([2]).broadcast()).process(Pairer())
