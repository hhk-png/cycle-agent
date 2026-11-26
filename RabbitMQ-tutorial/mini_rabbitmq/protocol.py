"""AMQP 0-9-1 线协议的一个受控子集。

实现范围：连接/信道握手、交换机/队列声明、绑定、发布、消费、拉取、
确认、QoS、事务、publisher confirms、心跳与关闭。没有实现的方法会返回
NOT_IMPLEMENTED，而不是静默假装成功。

字段表类型和帧布局遵循 AMQP 0-9-1；这只覆盖教学所需子集，不宣称完整
通过 AMQP 一致性测试套件。
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .errors import FrameError, ProtocolError, SyntaxError_


PROTOCOL_HEADER = b"AMQP\x00\x00\x09\x01"
FRAME_METHOD = 1
FRAME_HEADER = 2
FRAME_BODY = 3
FRAME_HEARTBEAT = 8
FRAME_END = 0xCE
FRAME_OVERHEAD = 8  # 7 字节头 + 1 字节 frame-end
BASIC_CLASS = 60


METHOD_NAMES: dict[tuple[int, int], str] = {
    (10, 10): "connection.start",
    (10, 11): "connection.start-ok",
    (10, 20): "connection.secure",
    (10, 21): "connection.secure-ok",
    (10, 30): "connection.tune",
    (10, 31): "connection.tune-ok",
    (10, 40): "connection.open",
    (10, 41): "connection.open-ok",
    (10, 50): "connection.close",
    (10, 51): "connection.close-ok",
    (10, 60): "connection.blocked",
    (10, 61): "connection.unblocked",
    (10, 70): "connection.update-secret",
    (10, 71): "connection.update-secret-ok",
    (20, 10): "channel.open",
    (20, 11): "channel.open-ok",
    (20, 20): "channel.flow",
    (20, 21): "channel.flow-ok",
    (20, 40): "channel.close",
    (20, 41): "channel.close-ok",
    (40, 10): "exchange.declare",
    (40, 11): "exchange.declare-ok",
    (40, 20): "exchange.delete",
    (40, 21): "exchange.delete-ok",
    (40, 30): "exchange.bind",
    (40, 31): "exchange.bind-ok",
    (40, 40): "exchange.unbind",
    (40, 41): "exchange.unbind-ok",
    (50, 10): "queue.declare",
    (50, 11): "queue.declare-ok",
    (50, 20): "queue.bind",
    (50, 21): "queue.bind-ok",
    (50, 30): "queue.purge",
    (50, 31): "queue.purge-ok",
    (50, 40): "queue.delete",
    (50, 41): "queue.delete-ok",
    (50, 50): "queue.unbind",
    (50, 51): "queue.unbind-ok",
    (60, 10): "basic.qos",
    (60, 11): "basic.qos-ok",
    (60, 20): "basic.consume",
    (60, 21): "basic.consume-ok",
    (60, 30): "basic.cancel",
    (60, 31): "basic.cancel-ok",
    (60, 40): "basic.publish",
    (60, 50): "basic.return",
    (60, 60): "basic.deliver",
    (60, 70): "basic.get",
    (60, 71): "basic.get-ok",
    (60, 72): "basic.get-empty",
    (60, 80): "basic.ack",
    (60, 90): "basic.reject",
    (60, 100): "basic.recover-async",
    (60, 110): "basic.recover",
    (60, 111): "basic.recover-ok",
    (60, 120): "basic.nack",
    (85, 10): "confirm.select",
    (85, 11): "confirm.select-ok",
    (90, 10): "tx.select",
    (90, 11): "tx.select-ok",
    (90, 20): "tx.commit",
    (90, 21): "tx.commit-ok",
    (90, 30): "tx.rollback",
    (90, 31): "tx.rollback-ok",
}


def method_name(class_id: int, method_id: int) -> str:
    return METHOD_NAMES.get((class_id, method_id), f"unknown({class_id},{method_id})")


class Writer:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def octet(self, value: int) -> "Writer":
        self.buffer += struct.pack(">B", value & 0xFF)
        return self

    def short(self, value: int) -> "Writer":
        self.buffer += struct.pack(">H", value)
        return self

    def long(self, value: int) -> "Writer":
        self.buffer += struct.pack(">I", value)
        return self

    def longlong(self, value: int) -> "Writer":
        self.buffer += struct.pack(">Q", value)
        return self

    def shortstr(self, value: str | bytes) -> "Writer":
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if len(raw) > 255:
            raise ValueError("shortstr exceeds 255 bytes")
        self.buffer += struct.pack(">B", len(raw)) + raw
        return self

    def longstr(self, value: str | bytes) -> "Writer":
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        self.buffer += struct.pack(">I", len(raw)) + raw
        return self

    def table(self, value: dict[str, Any] | None) -> "Writer":
        self.buffer += encode_table(value or {})
        return self

    def bits(self, *values: Any) -> "Writer":
        values = [bool(value) for value in values]
        for start in range(0, len(values), 8):
            octet = 0
            for position, value in enumerate(values[start : start + 8]):
                if value:
                    octet |= 1 << position
            self.buffer += struct.pack(">B", octet)
        return self

    def raw(self, value: bytes) -> "Writer":
        self.buffer += value
        return self

    def payload(self) -> bytes:
        return bytes(self.buffer)


class Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def _take(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise SyntaxError_("truncated AMQP field")
        chunk = self.data[self.offset : self.offset + size]
        self.offset += size
        return chunk

    def octet(self) -> int:
        return struct.unpack(">B", self._take(1))[0]

    def short(self) -> int:
        return struct.unpack(">H", self._take(2))[0]

    def long(self) -> int:
        return struct.unpack(">I", self._take(4))[0]

    def longlong(self) -> int:
        return struct.unpack(">Q", self._take(8))[0]

    def shortstr(self) -> str:
        size = self.octet()
        return self._take(size).decode("utf-8", errors="replace")

    def longstr(self) -> bytes:
        size = self.long()
        return self._take(size)

    def table(self) -> dict[str, Any]:
        return decode_table(self)

    def bits(self, count: int) -> list[bool]:
        result = []
        while len(result) < count:
            octet = self.octet()
            for bit in range(8):
                result.append(bool(octet & (1 << bit)))
        return result[:count]

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def rest(self) -> bytes:
        return self._take(self.remaining)


def encode_field_value(value: Any) -> bytes:
    if value is None:
        return b"V"
    if isinstance(value, bool):
        return b"t" + (b"\x01" if value else b"\x00")
    if isinstance(value, int):
        if -(2**31) <= value < 2**31:
            return b"I" + struct.pack(">i", value)
        if -(2**63) <= value < 2**63:
            return b"L" + struct.pack(">q", value)
        raise ValueError("integer out of AMQP field range")
    if isinstance(value, float):
        return b"d" + struct.pack(">d", value)
    if isinstance(value, Decimal):
        sign, digits, exponent = value.as_tuple()
        scale = max(0, -exponent)
        integer = int(value.scaleb(scale))
        return b"D" + struct.pack(">Bi", scale, integer)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return b"S" + struct.pack(">I", len(raw)) + raw
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return b"x" + struct.pack(">I", len(raw)) + raw
    if isinstance(value, dict):
        return b"F" + encode_table(value)
    if isinstance(value, (list, tuple)):
        body = b"".join(encode_field_value(item) for item in value)
        return b"A" + struct.pack(">I", len(body)) + body
    if hasattr(value, "timestamp"):
        return b"T" + struct.pack(">Q", int(value.timestamp()))
    raise TypeError(f"unsupported AMQP field value: {type(value)!r}")


def encode_table(value: dict[str, Any]) -> bytes:
    body = bytearray()
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("AMQP table keys must be strings")
        key_raw = key.encode("utf-8")
        if len(key_raw) > 255:
            raise ValueError("AMQP table key exceeds 255 bytes")
        body += struct.pack(">B", len(key_raw)) + key_raw
        body += encode_field_value(item)
    return struct.pack(">I", len(body)) + bytes(body)


def decode_field_value(reader: Reader, field_type: str) -> Any:
    if field_type == "t":
        return reader.octet() != 0
    if field_type == "b":
        return struct.unpack(">b", reader._take(1))[0]
    if field_type == "B":
        return reader.octet()
    if field_type == "U":
        return struct.unpack(">h", reader._take(2))[0]
    if field_type == "u":
        return reader.short()
    if field_type == "I":
        return struct.unpack(">i", reader._take(4))[0]
    if field_type == "i":
        return reader.long()
    if field_type == "L":
        return struct.unpack(">q", reader._take(8))[0]
    if field_type == "l":
        return reader.longlong()
    if field_type == "f":
        return struct.unpack(">f", reader._take(4))[0]
    if field_type == "d":
        return struct.unpack(">d", reader._take(8))[0]
    if field_type == "D":
        scale = reader.octet()
        raw = struct.unpack(">i", reader._take(4))[0]
        return Decimal(raw).scaleb(-scale)
    if field_type == "S":
        return reader.longstr().decode("utf-8", errors="replace")
    if field_type == "x":
        return reader.longstr()
    if field_type == "A":
        size = reader.long()
        end = reader.offset + size
        if end > len(reader.data):
            raise SyntaxError_("truncated AMQP array")
        nested = Reader(reader.data[reader.offset : end])
        result = []
        while nested.remaining:
            item_type = chr(nested.octet())
            result.append(decode_field_value(nested, item_type))
        reader.offset = end
        return result
    if field_type == "T":
        return struct.unpack(">Q", reader._take(8))[0]
    if field_type == "F":
        return decode_table(reader)
    if field_type == "V":
        return None
    raise SyntaxError_(f"unknown AMQP field type '{field_type}'")


def decode_table(reader: Reader) -> dict[str, Any]:
    size = reader.long()
    end = reader.offset + size
    if end > len(reader.data):
        raise SyntaxError_("truncated AMQP field table")
    table_reader = Reader(reader.data[reader.offset : end])
    result: dict[str, Any] = {}
    while table_reader.remaining:
        name = table_reader.shortstr()
        field_type = chr(table_reader.octet())
        result[name] = decode_field_value(table_reader, field_type)
    reader.offset = end
    return result


BASIC_PROPERTIES = (
    (15, "content_type", "shortstr"),
    (14, "content_encoding", "shortstr"),
    (13, "headers", "table"),
    (12, "delivery_mode", "octet"),
    (11, "priority", "octet"),
    (10, "correlation_id", "shortstr"),
    (9, "reply_to", "shortstr"),
    (8, "expiration", "shortstr"),
    (7, "message_id", "shortstr"),
    (6, "timestamp", "longlong"),
    (5, "type", "shortstr"),
    (4, "user_id", "shortstr"),
    (3, "app_id", "shortstr"),
    (2, "cluster_id", "shortstr"),
)


def encode_content_header(properties: dict[str, Any], body_size: int) -> bytes:
    flags = 0
    payload = bytearray()
    for bit, key, kind in BASIC_PROPERTIES:
        value = properties.get(key)
        if value is None:
            continue
        if kind == "table":
            if not value:
                continue
            flags |= 1 << bit
            payload += encode_table(value)
        elif kind == "octet":
            flags |= 1 << bit
            payload += struct.pack(">B", int(value))
        elif kind == "shortstr":
            flags |= 1 << bit
            raw = str(value).encode("utf-8")
            if len(raw) > 255:
                raise ValueError(f"property {key} exceeds shortstr limit")
            payload += struct.pack(">B", len(raw)) + raw
        elif kind == "longlong":
            flags |= 1 << bit
            payload += struct.pack(">Q", int(value))
        else:  # pragma: no cover - 防御性
            raise ValueError(kind)
    return struct.pack(">HHQH", BASIC_CLASS, 0, body_size, flags) + bytes(payload)


def decode_content_header(payload: bytes) -> tuple[int, int, dict[str, Any]]:
    reader = Reader(payload)
    class_id = reader.short()
    weight = reader.short()
    body_size = reader.longlong()
    flags = reader.short()
    while flags & 1:
        # 目前没有属性需要第二字，但按规范跳过 continuation word。
        flags = reader.short()
    properties: dict[str, Any] = {}
    for bit, key, kind in BASIC_PROPERTIES:
        if not flags & (1 << bit):
            continue
        if kind == "table":
            properties[key] = reader.table()
        elif kind == "octet":
            properties[key] = reader.octet()
        elif kind == "shortstr":
            properties[key] = reader.shortstr()
        elif kind == "longlong":
            properties[key] = reader.longlong()
    del weight
    return class_id, body_size, properties


def encode_method(class_id: int, method_id: int, args: bytes = b"") -> bytes:
    return struct.pack(">HH", class_id, method_id) + args


def decode_method(payload: bytes) -> tuple[int, int, Reader]:
    reader = Reader(payload)
    class_id = reader.short()
    method_id = reader.short()
    return class_id, method_id, reader


def encode_frame(frame_type: int, channel: int, payload: bytes) -> bytes:
    if not (0 <= channel <= 0xFFFF):
        raise ValueError("channel out of range")
    return struct.pack(">BHI", frame_type, channel, len(payload)) + payload + bytes([FRAME_END])


def read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("peer closed the socket")
        chunks += chunk
    return bytes(chunks)


def read_frame(sock: socket.socket, max_frame_size: int = 0) -> tuple[int, int, bytes]:
    header = read_exact(sock, 7)
    frame_type, channel, size = struct.unpack(">BHI", header)
    if max_frame_size and size + FRAME_OVERHEAD > max_frame_size:
        raise FrameError(
            f"frame size {size + FRAME_OVERHEAD} exceeds negotiated {max_frame_size}"
        )
    payload = read_exact(sock, size) if size else b""
    end = read_exact(sock, 1)[0]
    if end != FRAME_END:
        raise FrameError("bad frame-end octet")
    return frame_type, channel, payload


def write_frame(
    sock: socket.socket, frame_type: int, channel: int, payload: bytes
) -> None:
    sock.sendall(encode_frame(frame_type, channel, payload))


@dataclass
class IncomingContent:
    channel: int
    method_payload: bytes
    class_id: int
    body_size: int
    properties: dict[str, Any]
    body: bytearray


def send_method_args(class_id: int, method_id: int, writer: Writer) -> bytes:
    return encode_method(class_id, method_id, writer.payload())


class BufferReader(Reader):
    """语义别名，便于服务端代码表达“读取方法参数字段”。"""
