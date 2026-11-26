"""内置连接器：集合、文件、内存 Kafka、SQLite。

真实 Flink 连接器通常实现 FLIP-27 Source、Sink V2、分区发现、offset 提交、
两阶段提交与指标。MiniFlink 用内存消息总线模拟 Kafka 的 offset 语义，方便在
单机上验证 exactly-once 状态恢复和 offset 回放。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .core import MAX_WATERMARK, EndOfStream, Record, StreamElement, Watermark
from .operators import SinkOperator, SourceOperator
from .watermarks import TimestampAssigner, WatermarkGenerator, WatermarkStrategy


@dataclass(frozen=True)
class KafkaMessage:
    offset: int
    value: Any
    timestamp: int
    key: Any = None
    headers: dict[str, str] = field(default_factory=dict)


class InMemoryKafka:
    def __init__(self) -> None:
        self._topics: dict[str, list[list[KafkaMessage]]] = {}
        self._committed: dict[tuple[str, str, int], int] = {}
        self._lock = threading.RLock()

    def create_topic(self, topic: str, partitions: int = 1) -> int:
        if partitions <= 0:
            raise ValueError("分区数必须大于 0")
        with self._lock:
            if topic in self._topics:
                return len(self._topics[topic])
            self._topics[topic] = [[] for _ in range(partitions)]
            return partitions

    def partitions(self, topic: str) -> int:
        with self._lock:
            return len(self._topics.get(topic, []))

    def publish(
        self,
        topic: str,
        value: Any,
        *,
        key: Any = None,
        timestamp: int | None = None,
        partition: int = 0,
    ) -> KafkaMessage:
        with self._lock:
            if topic not in self._topics:
                self.create_topic(topic)
            buckets = self._topics[topic]
            if partition < 0 or partition >= len(buckets):
                raise ValueError(f"分区越界: {partition}")
            bucket = buckets[partition]
            message = KafkaMessage(
                offset=len(bucket),
                value=value,
                timestamp=timestamp if timestamp is not None else int(time.time() * 1000),
                key=key,
            )
            bucket.append(message)
            return message

    def read(self, topic: str, partition: int, offset: int) -> KafkaMessage | None:
        with self._lock:
            bucket = self._topics.get(topic, [])[partition]
            if offset >= len(bucket):
                return None
            return bucket[offset]

    def latest_offset(self, topic: str, partition: int) -> int:
        with self._lock:
            return len(self._topics.get(topic, [])[partition])

    def commit(self, group: str, topic: str, partition: int, offset: int) -> None:
        with self._lock:
            self._committed[(group, topic, partition)] = offset

    def committed(self, group: str, topic: str, partition: int) -> int:
        with self._lock:
            return self._committed.get((group, topic, partition), 0)

    def messages(self, topic: str, partition: int = 0) -> list[KafkaMessage]:
        with self._lock:
            return list(self._topics.get(topic, [])[partition])


class KafkaSource(SourceOperator):
    """单消费者组、手工 offset 的简化 Kafka Source。"""

    def __init__(
        self,
        bus: InMemoryKafka,
        topic: str,
        group_id: str,
        subtask_index: int,
        parallelism: int,
        *,
        strategy: WatermarkStrategy | None = None,
        start_offset: int | None = None,
    ) -> None:
        super().__init__()
        self.bus = bus
        self.topic = topic
        self.group_id = group_id
        self.subtask_index = subtask_index
        self.parallelism = parallelism
        self.partitions = [
            p
            for p in range(bus.partitions(topic))
            if p % parallelism == subtask_index
        ]
        self.offsets: dict[int, int] = {
            p: (
                start_offset
                if start_offset is not None
                else bus.committed(group_id, topic, p)
            )
            for p in self.partitions
        }
        self.strategy = strategy
        self.assigner: TimestampAssigner | None = (
            strategy.timestamp_assigner if strategy else None
        )
        self.generator = WatermarkGenerator(strategy) if strategy else None
        self._pending: list[StreamElement] = []
        self._partition_cursor = 0
        self._final_emitted = False
        self._ended = False

    def next_element(self) -> StreamElement | None:
        if self._pending:
            return self._pending.pop(0)
        if not self.partitions:
            if not self._final_emitted:
                self._final_emitted = True
                return Watermark(MAX_WATERMARK)
            if not self._ended:
                self._ended = True
                return EndOfStream()
            return None
        for _ in range(len(self.partitions)):
            partition = self.partitions[self._partition_cursor]
            self._partition_cursor = (self._partition_cursor + 1) % len(self.partitions)
            offset = self.offsets[partition]
            message = self.bus.read(self.topic, partition, offset)
            if message is None:
                continue
            self.offsets[partition] = offset + 1
            timestamp = (
                self.assigner.extract_timestamp(message.value, message.timestamp)
                if self.assigner
                else message.timestamp
            )
            record = Record(
                message.value, timestamp, message.key, dict(message.headers)
            )
            if self.generator:
                watermark = self.generator.on_record(timestamp)
                if watermark is not None:
                    self._pending.append(Watermark(watermark))
            return record
        if not self._final_emitted:
            self._final_emitted = True
            return Watermark(MAX_WATERMARK)
        if not self._ended:
            self._ended = True
            return EndOfStream()
        return None

    def is_exhausted(self) -> bool:
        return self._ended and not self._pending

    def snapshot(self) -> dict[str, Any]:
        # 生产环境应把 offset 作为检查点状态，并让 sink 的两阶段提交与
        # checkpoint 完成绑定；这里展示的是 source offset 的快照部分。
        return {
            "offsets": dict(self.offsets),
            "final": self._final_emitted,
            "ended": self._ended,
            # 微批边界上可能已经生成了 watermark 但还没下发；必须一起快照，
            # 否则恢复后 generator 不会重复发射同一个 watermark。
            "pending": [
                element.timestamp
                for element in self._pending
                if isinstance(element, Watermark)
            ],
            "generator": self.generator.snapshot() if self.generator else None,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.offsets = {int(k): v for k, v in snapshot.get("offsets", {}).items()}
        self._final_emitted = snapshot.get("final", False)
        # 与 CollectionSource 相同：从已结束的检查点恢复时重发结束信号。
        self._ended = False
        self._pending.clear()
        for timestamp in snapshot.get("pending", []):
            self._pending.append(Watermark(int(timestamp)))
        if self.generator and snapshot.get("generator"):
            self.generator.restore(snapshot["generator"])

    def commit_offsets(self) -> None:
        for partition, offset in self.offsets.items():
            self.bus.commit(self.group_id, self.topic, partition, offset)


class KafkaSink(SinkOperator):
    def __init__(self, bus: InMemoryKafka, topic: str, *, key_selector: Any = None) -> None:
        self.bus = bus
        self.topic = topic
        self.key_selector = key_selector
        super().__init__(self._send, name=f"kafka-sink:{topic}")

    def _send(self, value: Any) -> None:
        key = self.key_selector(value) if self.key_selector else None
        self.bus.publish(self.topic, value, key=key)

    def snapshot(self) -> dict[str, Any]:
        return {"latest": {p: self.bus.latest_offset(self.topic, p) for p in range(self.bus.partitions(self.topic))}}

    def restore(self, snapshot: dict[str, Any]) -> None:
        # 真实 KafkaSink 需要事务或幂等写入；这里只记录位置，不截断总线。
        pass


class SqliteSink(SinkOperator):
    """SQLite sink，用于演示 JDBC 连接器的批提交与事务边界。"""

    def __init__(self, path: str, insert_sql: str, *, batch_size: int = 100) -> None:
        self.path = path
        self.insert_sql = insert_sql
        self.batch_size = batch_size
        self._connection: sqlite3.Connection | None = None
        self._pending = 0
        super().__init__(self._write, name=f"sqlite-sink:{path}")

    def open(self, context: Any) -> None:
        super().open(context)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._pending = 0

    def _write(self, value: Any) -> None:
        assert self._connection is not None
        params = value if isinstance(value, (tuple, list)) else (value,)
        self._connection.execute(self.insert_sql, params)
        self._pending += 1
        if self._pending >= self.batch_size:
            self._connection.commit()
            self._pending = 0

    def end_of_input(self) -> list[StreamElement]:
        if self._connection is not None:
            self._connection.commit()
            self._connection.close()
            self._connection = None
        return [EndOfStream()]
