import sqlite3

from miniflink import (
    InMemoryKafka,
    KafkaSink,
    KafkaSource,
    Record,
    StreamExecutionEnvironment,
    SqliteSink,
    WatermarkStrategy,
)
from miniflink.connectors import KafkaMessage


def test_inmemory_kafka_publish_and_read():
    bus = InMemoryKafka()
    bus.create_topic("t", 2)
    message = bus.publish("t", "v", key="k", timestamp=100)
    assert message == KafkaMessage(0, "v", 100, "k")
    assert bus.read("t", 0, 0) == message
    assert bus.read("t", 0, 1) is None
    assert bus.latest_offset("t", 0) == 1


def test_kafka_commit_offset():
    bus = InMemoryKafka()
    bus.create_topic("t")
    bus.commit("g", "t", 0, 5)
    assert bus.committed("g", "t", 0) == 5
    assert bus.committed("other", "t", 0) == 0


def test_kafka_source_and_sink_pipeline():
    bus = InMemoryKafka()
    bus.create_topic("in")
    for index in range(3):
        bus.publish("in", index, timestamp=1000 + index)
    env = StreamExecutionEnvironment()
    stream = env.from_kafka(bus, "in", group_id="g").map(lambda value: value * 2)
    env.add_sink(lambda: KafkaSink(bus, "out"), stream)
    env.execute("kafka")
    assert [message.value for message in bus.messages("out")] == [0, 2, 4]


def test_kafka_source_snapshot_restore():
    bus = InMemoryKafka()
    bus.create_topic("in")
    for index in range(4):
        bus.publish("in", index, timestamp=index)
    source = KafkaSource(bus, "in", "g", 0, 1)
    first = source.next_element()
    second = source.next_element()
    snap = source.snapshot()
    resumed = KafkaSource(bus, "in", "g", 0, 1)
    resumed.restore(snap)
    third = resumed.next_element()
    assert isinstance(first, Record)
    assert getattr(first, "value") == 0
    assert getattr(second, "value") == 1
    assert getattr(third, "value") == 2


def test_kafka_source_commit_offsets():
    bus = InMemoryKafka()
    bus.create_topic("in")
    bus.publish("in", 1)
    source = KafkaSource(bus, "in", "g", 0, 1)
    source.next_element()
    source.commit_offsets()
    assert bus.committed("g", "in", 0) == 1


def test_file_source_and_text_sink(tmp_path):
    source_path = tmp_path / "in.txt"
    source_path.write_text("a\nb\na\n", encoding="utf-8")
    out_path = tmp_path / "out.txt"
    env = StreamExecutionEnvironment()
    (
        env.read_text_file(str(source_path))
        .map(lambda line: (line, 1))
        .key_by(lambda pair: pair[0])
        .reduce(lambda a, b: (a[0], a[1] + b[1]))
        .write_as_text(str(out_path), formatter=lambda pair: f"{pair[0]}:{pair[1]}")
    )
    env.execute("file")
    content = out_path.read_text(encoding="utf-8")
    assert "a:2" in content
    assert "b:1" in content


def test_sqlite_sink(tmp_path):
    database = tmp_path / "sink.db"
    env = StreamExecutionEnvironment()
    stream = env.from_collection([(1, "a"), (2, "b")])
    env.add_sink(
        lambda: SqliteSink(
            str(database),
            "INSERT INTO t (id, name) VALUES (?, ?)",
        ),
        stream,
    )
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    connection.commit()
    connection.close()
    env.execute("sqlite")
    connection = sqlite3.connect(database)
    rows = connection.execute("SELECT id, name FROM t ORDER BY id").fetchall()
    connection.close()
    assert rows == [(1, "a"), (2, "b")]


def test_kafka_source_with_watermark_strategy():
    bus = InMemoryKafka()
    bus.create_topic("t")
    bus.publish("t", {"ts": 1000, "v": 1}, timestamp=1000)
    env = StreamExecutionEnvironment()
    rows = env.from_kafka(
        bus,
        "t",
        watermark_strategy=WatermarkStrategy.for_bounded_out_of_orderness(
            0, timestamp_assigner=lambda row: row["ts"]
        ),
    ).execute_and_collect()
    assert rows == [{"ts": 1000, "v": 1}]
