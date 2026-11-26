"""Streams 与 Super Streams 的教学模型。

与经典队列不同，Stream 是追加日志：消息保留、消费者维护自己的 offset、
读取不删除消息。真实 RabbitMQ 还实现分段文件、chunk、stream 协议和
super stream 分区；这里保留语义骨架，便于实验 retention 和 offset。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class StreamEntry:
    offset: int
    timestamp: float
    body: bytes
    headers: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "timestamp": self.timestamp,
            "body": self.body.decode("utf-8", errors="replace"),
            "headers": self.headers,
            "properties": self.properties,
        }


class Stream:
    def __init__(
        self,
        name: str,
        *,
        max_age_seconds: int | None = None,
        max_length_bytes: int = 0,
        max_segment_size_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.name = name
        self.max_age_seconds = max_age_seconds
        self.max_length_bytes = max(0, max_length_bytes)
        self.max_segment_size_bytes = max_segment_size_bytes
        self._entries: list[StreamEntry] = []
        self._next_offset = 0
        self._bytes = 0
        self._lock = threading.RLock()
        self._appended = 0
        self._trimmed = 0

    def append(
        self,
        body: bytes | str,
        *,
        headers: dict[str, Any] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> StreamEntry:
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        with self._lock:
            entry = StreamEntry(
                offset=self._next_offset,
                timestamp=time.time(),
                body=raw,
                headers=dict(headers or {}),
                properties=dict(properties or {}),
            )
            self._next_offset += 1
            self._entries.append(entry)
            self._bytes += len(raw) + 64
            self._appended += 1
            self._trim_locked()
            return entry

    def _trim_locked(self) -> None:
        cutoff = None
        if self.max_age_seconds is not None:
            cutoff = time.time() - self.max_age_seconds
        while self._entries:
            entry = self._entries[0]
            age_trim = cutoff is not None and entry.timestamp < cutoff
            size_trim = self.max_length_bytes and self._bytes > self.max_length_bytes
            if not age_trim and not size_trim:
                break
            removed = self._entries.pop(0)
            self._bytes -= len(removed.body) + 64
            self._trimmed += 1

    @property
    def first_offset(self) -> int | None:
        with self._lock:
            return self._entries[0].offset if self._entries else None

    @property
    def last_offset(self) -> int | None:
        with self._lock:
            return self._entries[-1].offset if self._entries else None

    @property
    def next_offset(self) -> int:
        with self._lock:
            return self._next_offset

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def read(
        self,
        offset: int = 0,
        count: int = 100,
        *,
        max_bytes: int = 1_048_576,
    ) -> list[StreamEntry]:
        with self._lock:
            if not self._entries:
                return []
            start = max(offset, self.first_offset or 0)
            result: list[StreamEntry] = []
            total = 0
            for entry in self._entries:
                if entry.offset < start:
                    continue
                if len(result) >= count:
                    break
                if result and total + len(entry.body) > max_bytes:
                    break
                result.append(entry)
                total += len(entry.body)
            return list(result)

    def subscribe(
        self,
        name: str = "consumer",
        *,
        start: str | int = "next",
    ) -> "StreamConsumer":
        with self._lock:
            if isinstance(start, int):
                offset = start
            elif start == "first":
                offset = self.first_offset if self.first_offset is not None else self._next_offset
            elif start == "last":
                offset = self.last_offset if self.last_offset is not None else self._next_offset
            elif start == "next":
                offset = self._next_offset
            else:
                raise ValueError("start must be first, last, next or an integer offset")
            return StreamConsumer(self, name, offset)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "first_offset": self.first_offset,
                "last_offset": self.last_offset,
                "next_offset": self._next_offset,
                "entries": len(self._entries),
                "size_bytes": self._bytes,
                "appended_total": self._appended,
                "trimmed_total": self._trimmed,
                "max_age_seconds": self.max_age_seconds,
                "max_length_bytes": self.max_length_bytes,
                "max_segment_size_bytes": self.max_segment_size_bytes,
            }


class StreamConsumer:
    def __init__(self, stream: Stream, name: str, offset: int) -> None:
        self.stream = stream
        self.name = name
        self.offset = offset

    def read(self, count: int = 100, *, max_bytes: int = 1_048_576) -> list[StreamEntry]:
        entries = self.stream.read(self.offset, count, max_bytes=max_bytes)
        if entries:
            self.offset = entries[-1].offset + 1
        return entries

    def seek(self, offset: int) -> None:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        self.offset = offset


class SuperStream:
    """按 routing key 哈希分区的 super stream 模型。"""

    def __init__(self, name: str, partitions: int = 3, **stream_kwargs: Any) -> None:
        if partitions < 1:
            raise ValueError("partitions must be at least 1")
        self.name = name
        self.partitions = [
            Stream(f"{name}-{index}", **stream_kwargs) for index in range(partitions)
        ]

    def partition_for(self, routing_key: str) -> Stream:
        digest = hashlib.sha256(routing_key.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % len(self.partitions)
        return self.partitions[index]

    def append(
        self,
        routing_key: str,
        body: bytes | str,
        *,
        headers: dict[str, Any] | None = None,
    ) -> StreamEntry:
        return self.partition_for(routing_key).append(body, headers=headers)

    def read_all(self, offset: int = 0, count: int = 100) -> list[StreamEntry]:
        result: list[StreamEntry] = []
        for partition in self.partitions:
            remaining = count - len(result)
            if remaining <= 0:
                break
            result.extend(partition.read(offset, remaining))
        return result

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "partitions": [partition.stats() for partition in self.partitions],
            "entries": sum(len(partition._entries) for partition in self.partitions),
        }

    @property
    def entries(self) -> list[Iterable[StreamEntry]]:
        return [list(partition._entries) for partition in self.partitions]
