import pytest

from miniflink.core import (
    MAX_WATERMARK,
    Collector,
    Duration,
    OutputTag,
    Record,
    Watermark,
    Window,
    WindowResult,
)


def test_duration_arithmetic():
    assert Duration.of_seconds(2).to_millis() == 2000
    assert (Duration.of_seconds(1) + Duration.of_millis(500)).to_millis() == 1500
    assert (Duration.of_seconds(1) * 3).to_millis() == 3000
    with pytest.raises(ValueError):
        Duration(-1)


def test_window_contains_overlap_merge():
    left = Window(0, 10)
    right = Window(5, 20)
    assert left.contains(0)
    assert not left.contains(10)
    assert left.overlap(right)
    assert left.merge(right) == Window(0, 20)


def test_record_is_immutable_and_preserves_headers():
    record = Record("value", 100, "k", {"trace": "abc"})
    changed = record.with_value("other")
    assert changed.value == "other"
    assert changed.timestamp == 100
    assert changed.key == "k"
    assert changed.headers["trace"] == "abc"
    keyed = record.with_key("key2")
    assert keyed.key == "key2"


def test_watermark_repr_and_validation():
    assert "INF" in repr(Watermark(MAX_WATERMARK))
    with pytest.raises(TypeError):
        Watermark("x")  # type: ignore[arg-type]


def test_output_tag_and_collector():
    tag = OutputTag("late")
    assert tag.id == "late"
    with pytest.raises(ValueError):
        OutputTag("")
    out = Collector()
    out.collect(1)
    out.collect_all([2, 3])
    assert out.get_values() == [1, 2, 3]
    assert len(out) == 3
    out.clear()
    assert out.get_values() == []


def test_window_result_fields():
    result = WindowResult("k", Window(0, 10), 5)
    assert result.key == "k"
    assert result.window.end == 10
    assert result.value == 5
