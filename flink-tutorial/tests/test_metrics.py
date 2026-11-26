from miniflink.metrics import (
    Counter,
    Gauge,
    LoggingReporter,
    Meter,
    MetricGroup,
)


def test_counter():
    counter = Counter("c")
    counter.inc()
    counter.inc(3)
    counter.dec()
    assert counter.snapshot() == 3


def test_gauge():
    value = [1]
    gauge = Gauge("g", lambda: value[0])
    value[0] = 5
    assert gauge.snapshot() == 5


def test_meter():
    meter = Meter("m")
    meter.mark(10)
    snapshot = meter.snapshot()
    assert snapshot["count"] == 10
    assert snapshot["rate_per_second"] >= 0


def test_metric_group_nested_snapshot():
    group = MetricGroup("job")
    group.counter("records").inc(2)
    task = group.add_group("task")
    task.counter("records").inc(1)
    task.gauge("watermark", lambda: 100)
    snapshot = group.snapshot()
    assert snapshot["job.records"] == 2
    assert snapshot["task"]["job.task.records"] == 1
    assert snapshot["task"]["job.task.watermark"] == 100


def test_logging_reporter_keeps_history():
    reporter = LoggingReporter()
    reporter.report({"a": 1})
    reporter.report({"a": 2})
    assert reporter.history == [{"a": 1}, {"a": 2}]
