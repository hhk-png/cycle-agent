import pytest

from miniflink import (
    AggregatingStateDescriptor,
    AverageAggregate,
    KeyedProcessFunction,
    ListStateDescriptor,
    MapStateDescriptor,
    OutputTag,
    ProcessFunction,
    Record,
    ReducingStateDescriptor,
    StateTtlConfig,
    StreamExecutionEnvironment,
    ValueStateDescriptor,
    WatermarkStrategy,
)
from miniflink.state import KeyedStateBackend, TtlStateVisibility, TtlUpdateType


class StatefulCounter(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        state = ctx.get_state(ValueStateDescriptor("count", lambda: 0))
        state.update(state.get() + 1)
        out.collect((ctx.current_key, state.get()))


def test_value_state_is_isolated_by_key():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection(
            [
                Record(("a", 1), timestamp=1),
                Record(("b", 1), timestamp=2),
                Record(("a", 2), timestamp=3),
            ],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda row: row[0])
        .process(StatefulCounter())
        .execute_and_collect()
    )
    assert result == [("a", 1), ("b", 1), ("a", 2)]


class ListCollector(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        state = ctx.get_list_state(ListStateDescriptor("items", list))
        state.add(value)
        out.collect(state.get())


def test_list_state():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([Record(1, 1), Record(2, 2)], watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0))
        .key_by(lambda value: "k")
        .process(ListCollector())
        .execute_and_collect()
    )
    assert result[-1] == [1, 2]


class MapUpdater(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        state = ctx.get_map_state(MapStateDescriptor("m", dict))
        state.put(value[0], value[1])
        out.collect(sorted(state.items()))


def test_map_state():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([Record(("a", 1), 1), Record(("b", 2), 2)], watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0))
        .key_by(lambda value: "k")
        .process(MapUpdater())
        .execute_and_collect()
    )
    assert result[-1] == [("a", 1), ("b", 2)]


class SumReducer:
    def reduce(self, a, b):
        return a + b


class Reducing(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        state = ctx.get_reducing_state(
            ReducingStateDescriptor("sum", lambda a, b: a + b, lambda: 0)
        )
        state.add(value)
        out.collect(state.get())


def test_reducing_state():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([1, 2, 3])
        .key_by(lambda value: "k")
        .process(Reducing())
        .execute_and_collect()
    )
    assert result == [1, 3, 6]


class Aggregating(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        descriptor = AggregatingStateDescriptor("agg", AverageAggregate(), lambda: 0.0)
        state = ctx.get_aggregating_state(descriptor)
        state.add(value)
        out.collect(state.get())


def test_aggregating_state():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([1, 2, 3])
        .key_by(lambda value: "k")
        .process(Aggregating())
        .execute_and_collect()
    )
    assert result == [1.0, 1.5, 2.0]


class EventTimer(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        ctx.timer_service.register_event_time_timer(ctx.timestamp + 500, ctx.current_key)
        out.collect(("event", value))

    def on_timer(self, timestamp, ctx, out):
        out.collect(("timer", timestamp, ctx.current_key))


def test_event_time_timers_fire_on_watermark():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection(
            [Record("a", timestamp=1000), Record("b", timestamp=2000)],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda value: value)
        .process(EventTimer())
        .execute_and_collect()
    )
    assert ("timer", 1500, "a") in result
    assert ("timer", 2500, "b") in result


class ProcessingTimer(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        out.collect(("event", value))
        ctx.timer_service.register_processing_time_timer(0, ctx.current_key)

    def on_timer(self, timestamp, ctx, out):
        out.collect(("processing", ctx.current_key))


def test_processing_time_timers_fire():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([1, 2])
        .key_by(lambda value: value)
        .process(ProcessingTimer())
        .execute_and_collect()
    )
    assert ("processing", 1) in result
    assert ("processing", 2) in result


def test_keyed_state_snapshot_restore():
    backend = KeyedStateBackend(clock=lambda: 1000)
    backend.set_current_key("k")
    backend.write_current("x", 42)
    snapshot = backend.snapshot()
    restored = KeyedStateBackend(clock=lambda: 1000)
    restored.restore(snapshot)
    restored.set_current_key("k")
    assert restored.read_current("x", lambda: None) == 42


def test_ttl_expiration_with_injected_clock():
    now = [1000]
    backend = KeyedStateBackend(clock=lambda: now[0])
    backend.set_current_key("k")
    config = StateTtlConfig.new_config(100)
    backend.write_current("x", 1)
    now[0] = 1050
    assert backend.read_current("x", lambda: None, config) == 1
    now[0] = 1200
    assert backend.read_current("x", lambda: None, config) is None

    # ON_READ 会在每次读取时续期；DISABLED 则不做 TTL 过期。
    read_config = StateTtlConfig(100, update_type=TtlUpdateType.ON_READ)
    backend.write_current("y", 1)
    now[0] = 1050
    assert backend.read_current("y", lambda: None, read_config) == 1
    now[0] = 1100
    assert backend.read_current("y", lambda: None, read_config) == 1
    disabled = StateTtlConfig(100, update_type=TtlUpdateType.DISABLED)
    now[0] = 99_999
    assert backend.read_current("y", lambda: None, disabled) == 1


def test_state_ttl_visibility_can_return_expired_value():
    now = [1000]
    backend = KeyedStateBackend(clock=lambda: now[0])
    backend.set_current_key("k")
    config = StateTtlConfig(
        100, visibility=TtlStateVisibility.RETURN_EXPIRED_IF_NOT_CLEANED
    )
    backend.write_current("x", 1)
    now[0] = 2000
    assert backend.read_current("x", lambda: None, config) == 1


class FutureProcessingTimer(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        out.collect(("event", value))
        ctx.timer_service.register_processing_time_timer(10 ** 15, ctx.current_key)

    def on_timer(self, timestamp, ctx, out):
        out.collect(("future-timer", ctx.current_key))


def test_future_processing_timer_does_not_fire_at_end_of_input():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([1])
        .key_by(lambda value: value)
        .process(FutureProcessingTimer())
        .execute_and_collect()
    )
    assert ("event", 1) in result
    assert not any(item[0] == "future-timer" for item in result)


class TimerSideOutput(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        if ctx.current_key == "a":
            ctx.timer_service.register_event_time_timer(1500, ctx.current_key)

    def on_timer(self, timestamp, ctx, out):
        ctx.output(OutputTag("timer-side"), ("timer", ctx.current_key))


class CaptureMeta(ProcessFunction):
    def process_element(self, value, ctx, out):
        out.collect((ctx.current_key, ctx.timestamp, value))


def test_timer_side_output_uses_timer_key_and_timestamp():
    env = StreamExecutionEnvironment()
    stream = (
        env.from_collection(
            [Record(("a", 1), timestamp=1500), Record(("b", 2), timestamp=2000)],
            watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
        )
        .key_by(lambda row: row[0])
        .process(TimerSideOutput())
    )
    side = stream.get_side_output(OutputTag("timer-side")).process(CaptureMeta())
    assert side.execute_and_collect() == [("a", 1500, ("timer", "a"))]
