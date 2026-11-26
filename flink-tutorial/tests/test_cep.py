import pytest

from miniflink import (
    Duration,
    Pattern,
    Record,
    StreamExecutionEnvironment,
    WatermarkStrategy,
    pattern,
)


def _run(events, pattern_obj):
    env = StreamExecutionEnvironment()
    stream = env.from_collection(
        events, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
    )
    return (
        pattern(stream.key_by(lambda event: event[0]), pattern_obj)
        .select(lambda match: (match.key, match["start"], match["end"]))
        .execute_and_collect()
    )


def test_simple_sequence():
    events = [
        Record(("u1", "fail"), 1000),
        Record(("u1", "fail"), 1200),
    ]
    p = (
        Pattern.begin("start")
        .where(lambda event: event[1] == "fail")
        .next("end")
        .where(lambda event: event[1] == "fail")
    )
    assert _run(events, p) == [("u1", ("u1", "fail"), ("u1", "fail"))]

    # times(n) 的重复步骤必须保留原条件，不能变成“任意事件”。
    times_pattern = (
        Pattern.begin("a").where(lambda event: event[1] == "fail").times(2)
    )
    times_result = (
        pattern(
            StreamExecutionEnvironment()
            .from_collection(
                events,
                watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0),
            )
            .key_by(lambda event: event[0]),
            times_pattern,
        )
        .select(lambda match: match["a"])
        .execute_and_collect()
    )
    assert len(times_result) == 1


def test_strict_next_breaks_on_intervening_event():
    events = [
        Record(("u1", "fail"), 1000),
        Record(("u1", "ok"), 1100),
        Record(("u1", "fail"), 1200),
    ]
    p = (
        Pattern.begin("start")
        .where(lambda event: event[1] == "fail")
        .next("end")
        .where(lambda event: event[1] == "fail")
    )
    assert _run(events, p) == []

    # 其他 key 的记录不应打断当前 key 的严格 next 匹配。
    interleaved = [
        Record(("u1", "fail"), 1000),
        Record(("u2", "ok"), 1100),
        Record(("u1", "fail"), 1200),
    ]
    assert len(_run(interleaved, p)) == 1


def test_followed_by_allows_other_events():
    events = [
        Record(("u1", "fail"), 1000),
        Record(("u1", "ok"), 1100),
        Record(("u1", "fail"), 1200),
    ]
    p = (
        Pattern.begin("start")
        .where(lambda event: event[1] == "fail")
        .followed_by("end")
        .where(lambda event: event[1] == "fail")
    )
    assert len(_run(events, p)) == 1


def test_within_filters_old_partial_matches():
    events = [
        Record(("u1", "fail"), 1000),
        Record(("u1", "fail"), 10000),
    ]
    p = (
        Pattern.begin("start")
        .where(lambda event: event[1] == "fail")
        .next("end")
        .where(lambda event: event[1] == "fail")
        .within(Duration.of_seconds(2))
    )
    assert _run(events, p) == []


def test_one_or_more_emits_on_each_extra_match():
    events = [
        Record(("u1", "fail"), 1000),
        Record(("u1", "fail"), 1100),
        Record(("u1", "fail"), 1200),
    ]
    p = Pattern.begin("fail").where(lambda event: event[1] == "fail").one_or_more()
    env = StreamExecutionEnvironment()
    stream = env.from_collection(
        events, watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(0)
    )
    result = (
        pattern(stream.key_by(lambda event: event[0]), p)
        .select(lambda match: match["fail"])
        .execute_and_collect()
    )
    assert len(result) == 3


def test_duplicate_step_name_rejected():
    with pytest.raises(ValueError):
        Pattern.begin("a").next("a")
    with pytest.raises(ValueError):
        Pattern.begin("a").where(lambda event: True).one_or_more().next("b")


def test_unknown_match_key_raises():
    from miniflink.cep import Match

    match = Match("k", {"a": [1]}, 0, 1)
    with pytest.raises(KeyError):
        match["b"]
    assert match.get("b", "x") == "x"
