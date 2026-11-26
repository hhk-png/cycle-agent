from __future__ import annotations

import datetime
import socket
import struct
from decimal import Decimal

import pytest

from mini_rabbitmq.errors import FrameError, SyntaxError_
from mini_rabbitmq.protocol import (
    FRAME_END,
    FRAME_METHOD,
    PROTOCOL_HEADER,
    Reader,
    Writer,
    decode_content_header,
    decode_method,
    encode_content_header,
    encode_frame,
    encode_method,
    encode_table,
    method_name,
    read_frame,
)


def test_writer_reader_primitives():
    payload = (
        Writer()
        .octet(7)
        .short(300)
        .long(70000)
        .longlong(2**40)
        .shortstr("héllo")
        .longstr(b"bytes")
        .payload()
    )
    reader = Reader(payload)
    assert reader.octet() == 7
    assert reader.short() == 300
    assert reader.long() == 70000
    assert reader.longlong() == 2**40
    assert reader.shortstr() == "héllo"
    assert reader.longstr() == b"bytes"
    assert reader.remaining == 0


def test_shortstr_length_limit():
    with pytest.raises(ValueError):
        Writer().shortstr("x" * 256)


def test_reader_truncated_field():
    reader = Reader(b"\x00\x00")
    with pytest.raises(SyntaxError_):
        reader.long()


def test_bits_pack_lsb_first():
    payload = Writer().bits(True, False, True, True).payload()
    assert payload == bytes([0b00001101])
    assert Reader(payload).bits(4) == [True, False, True, True]


def test_bits_more_than_one_octet():
    values = [True, False, False, False, False, False, False, False, True, True]
    payload = Writer().bits(*values).payload()
    assert len(payload) == 2
    assert Reader(payload).bits(10) == values


def test_field_table_roundtrip_all_common_types():
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    table = {
        "bool": True,
        "int": 42,
        "negative": -7,
        "big": 2**40,
        "float": 1.5,
        "decimal": Decimal("12.34"),
        "text": "hello",
        "bytes": b"\x00\x01",
        "none": None,
        "list": [1, "two", False],
        "nested": {"a": 1},
        "timestamp": now,
    }
    decoded = Reader(encode_table(table)).table()
    assert decoded["bool"] is True
    assert decoded["int"] == 42
    assert decoded["negative"] == -7
    assert decoded["big"] == 2**40
    assert decoded["float"] == 1.5
    assert decoded["decimal"] == Decimal("12.34")
    assert decoded["text"] == "hello"
    assert decoded["bytes"] == b"\x00\x01"
    assert decoded["none"] is None
    assert decoded["list"] == [1, "two", False]
    assert decoded["nested"] == {"a": 1}
    assert decoded["timestamp"] == now


def test_field_table_key_length_limit():
    with pytest.raises(ValueError):
        encode_table({"k" * 256: 1})


def test_unsupported_field_type():
    with pytest.raises(TypeError):
        encode_table({"x": object()})


def test_content_header_roundtrip():
    properties = {
        "content_type": "application/json",
        "content_encoding": "utf-8",
        "headers": {"trace": "abc", "retries": 2},
        "delivery_mode": 2,
        "priority": 5,
        "correlation_id": "c-1",
        "reply_to": "reply-q",
        "expiration": "30000",
        "message_id": "m-1",
        "timestamp": 1700000000,
        "type": "event",
        "user_id": "guest",
        "app_id": "tests",
        "cluster_id": "cluster",
    }
    payload = encode_content_header(properties, 123)
    class_id, body_size, decoded = decode_content_header(payload)
    assert class_id == 60
    assert body_size == 123
    assert decoded == properties


def test_content_header_empty_properties():
    payload = encode_content_header({}, 0)
    class_id, body_size, decoded = decode_content_header(payload)
    assert class_id == 60
    assert body_size == 0
    assert decoded == {}


def test_content_header_omits_empty_headers():
    payload = encode_content_header({"headers": {}, "delivery_mode": 1}, 4)
    _, _, decoded = decode_content_header(payload)
    assert "headers" not in decoded
    assert decoded["delivery_mode"] == 1


def test_method_encode_decode():
    args = Writer().shortstr("queue").long(3).payload()
    payload = encode_method(50, 11, args)
    class_id, method_id, reader = decode_method(payload)
    assert (class_id, method_id) == (50, 11)
    assert reader.shortstr() == "queue"
    assert reader.long() == 3


def test_method_names_known_and_unknown():
    assert method_name(10, 10) == "connection.start"
    assert method_name(60, 120) == "basic.nack"
    assert "unknown" in method_name(999, 999)


def test_frame_encoding_layout():
    frame = encode_frame(FRAME_METHOD, 3, b"payload")
    frame_type, channel, size = struct.unpack(">BHI", frame[:7])
    assert frame_type == FRAME_METHOD
    assert channel == 3
    assert size == 7
    assert frame[7:-1] == b"payload"
    assert frame[-1] == FRAME_END


def test_read_frame_roundtrip():
    left, right = socket.socketpair()
    try:
        left.sendall(encode_frame(FRAME_METHOD, 2, b"abc"))
        assert read_frame(right) == (FRAME_METHOD, 2, b"abc")
    finally:
        left.close()
        right.close()


def test_read_frame_bad_end():
    left, right = socket.socketpair()
    try:
        frame = bytearray(encode_frame(FRAME_METHOD, 1, b"x"))
        frame[-1] = 0
        left.sendall(frame)
        with pytest.raises(FrameError):
            read_frame(right)
    finally:
        left.close()
        right.close()


def test_read_frame_max_size():
    left, right = socket.socketpair()
    try:
        left.sendall(encode_frame(FRAME_METHOD, 1, b"x" * 100))
        with pytest.raises(FrameError):
            read_frame(right, max_frame_size=20)
    finally:
        left.close()
        right.close()


def test_protocol_header_bytes():
    assert PROTOCOL_HEADER == b"AMQP\x00\x00\x09\x01"


def test_table_truncation_detected():
    encoded = bytearray(encode_table({"a": "b"}))
    encoded[3] = 200  # 声称的表长度超出实际
    with pytest.raises(SyntaxError_):
        Reader(bytes(encoded)).table()
