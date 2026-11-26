"""Remote write/read codecs.

Prometheus' remote protocols are protobuf messages compressed with Snappy.
Depending on third-party protobuf would violate the tutorial's "stdlib plus
PyYAML" rule, so this module implements just enough wire-format decoding for
``prometheus.WriteRequest`` and Snappy block compression/decompression.

The encoder is literal-only Snappy: valid, uncompressed, and useful for tests
and for a Mini sender.  A production sender should use the C++/Go library.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .labels import Labels

# ---------------------------------------------------------------------------
# Snappy block format


def snappy_decompress(data: bytes) -> bytes:
    length, position = _read_varint(data, 0)
    out = bytearray()
    while position < len(data):
        tag = data[position]
        position += 1
        kind = tag & 0x03
        if kind == 0:  # literal
            size = tag >> 2
            if size < 60:
                size += 1
            else:
                extra = size - 59
                if position + extra > len(data):
                    raise ValueError("truncated snappy literal length")
                size = int.from_bytes(data[position : position + extra], "little") + 1
                position += extra
            if position + size > len(data):
                raise ValueError("truncated snappy literal")
            out.extend(data[position : position + size])
            position += size
        elif kind == 1:  # copy, 1-byte offset
            size = 4 + ((tag >> 2) & 0x07)
            if position >= len(data):
                raise ValueError("truncated snappy copy")
            offset = ((tag & 0xE0) << 3) | data[position]
            position += 1
            _copy(out, offset, size)
        elif kind == 2:  # copy, 2-byte offset
            size = (tag >> 2) + 1
            if position + 2 > len(data):
                raise ValueError("truncated snappy copy")
            offset = int.from_bytes(data[position : position + 2], "little")
            position += 2
            _copy(out, offset, size)
        else:  # copy, 4-byte offset
            size = (tag >> 2) + 1
            if position + 4 > len(data):
                raise ValueError("truncated snappy copy")
            offset = int.from_bytes(data[position : position + 4], "little")
            position += 4
            _copy(out, offset, size)
    if len(out) != length:
        raise ValueError(f"snappy length mismatch: expected {length}, got {len(out)}")
    return bytes(out)


def _copy(out: bytearray, offset: int, size: int) -> None:
    if offset <= 0 or offset > len(out):
        raise ValueError("invalid snappy copy offset")
    for _ in range(size):
        out.append(out[-offset])


def snappy_compress(data: bytes) -> bytes:
    """Literal-only Snappy encoder (valid, intentionally not compressing)."""
    out = bytearray()
    out.extend(_encode_varint(len(data)))
    position = 0
    while position < len(data):
        chunk = data[position : position + 65536]
        position += len(chunk)
        size = len(chunk) - 1
        if size < 60:
            out.append(size << 2)
        else:
            # Encode the literal length as 1..4 little-endian bytes.
            raw = size.to_bytes((size.bit_length() + 7) // 8, "little")
            out.append((59 + len(raw)) << 2)
            out.extend(raw)
        out.extend(chunk)
    return bytes(out)


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while position < len(data):
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7
        if shift > 63:
            break
    raise ValueError("truncated varint")


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


# ---------------------------------------------------------------------------
# Protobuf wire format


@dataclass
class Field:
    number: int
    wire_type: int
    value: object


def parse_protobuf(data: bytes) -> list[Field]:
    fields: list[Field] = []
    position = 0
    while position < len(data):
        key, position = _read_varint(data, position)
        number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, position = _read_varint(data, position)
        elif wire_type == 1:
            if position + 8 > len(data):
                raise ValueError("truncated fixed64")
            value = data[position : position + 8]
            position += 8
        elif wire_type == 2:
            length, position = _read_varint(data, position)
            if position + length > len(data):
                raise ValueError("truncated length-delimited field")
            value = data[position : position + length]
            position += length
        elif wire_type == 5:
            if position + 4 > len(data):
                raise ValueError("truncated fixed32")
            value = data[position : position + 4]
            position += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields.append(Field(number, wire_type, value))
    return fields


def _parse_label(data: bytes) -> tuple[str, str]:
    name = value = ""
    for field in parse_protobuf(data):
        if field.number == 1:
            name = field.value.decode("utf-8")
        elif field.number == 2:
            value = field.value.decode("utf-8")
    return name, value


def decode_write_request(body: bytes, *, content_encoding: str = "snappy") -> list[tuple[Labels, int, float]]:
    raw = body
    if content_encoding.lower() in ("snappy", ""):
        if content_encoding.lower() == "snappy":
            raw = snappy_decompress(body)
    elif content_encoding.lower() != "identity":
        raise ValueError(f"unsupported content-encoding {content_encoding!r}")
    samples: list[tuple[Labels, int, float]] = []
    for field in parse_protobuf(raw):
        if field.number != 1 or field.wire_type != 2:
            continue
        labels_items: list[tuple[str, str]] = []
        sample_pairs: list[tuple[int, float]] = []
        for inner in parse_protobuf(field.value):
            if inner.number == 1 and inner.wire_type == 2:
                labels_items.append(_parse_label(inner.value))
            elif inner.number == 2 and inner.wire_type == 2:
                value = 0.0
                timestamp = 0
                for sample_field in parse_protobuf(inner.value):
                    if sample_field.number == 1 and sample_field.wire_type == 1:
                        value = struct.unpack("<d", sample_field.value)[0]
                    elif sample_field.number == 2 and sample_field.wire_type == 0:
                        timestamp = _signed64(sample_field.value)
                sample_pairs.append((timestamp, value))
            elif inner.number == 4 and inner.wire_type == 2:
                # Native histograms are not stored by Mini-Prometheus.  A
                # production receiver would decode them here; failing loudly
                # is better than silently losing data.
                raise ValueError("native histogram remote write is not supported")
        labels = Labels(labels_items)
        if not labels.get("__name__"):
            continue
        for timestamp, value in sample_pairs:
            samples.append((labels, timestamp, value))
    return samples


def _signed64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


def encode_write_request(series: list[tuple[Labels, list[tuple[int, float]]]]) -> bytes:
    """Encode a WriteRequest (used by tests and the mini remote-write sender)."""
    out = bytearray()
    for labels, samples in series:
        ts = bytearray()
        for name, value in labels.items():
            label = bytearray()
            label.extend(_tag(1, 2)); label.extend(_ld(name.encode()))
            label.extend(_tag(2, 2)); label.extend(_ld(value.encode()))
            ts.extend(_tag(1, 2)); ts.extend(_ld(bytes(label)))
        for timestamp, value in samples:
            sample = bytearray()
            sample.extend(_tag(1, 1)); sample.extend(struct.pack("<d", value))
            sample.extend(_tag(2, 0)); sample.extend(_encode_varint(timestamp & ((1 << 64) - 1)))
            ts.extend(_tag(2, 2)); ts.extend(_ld(bytes(sample)))
        out.extend(_tag(1, 2)); out.extend(_ld(bytes(ts)))
    return bytes(out)


def _tag(number: int, wire_type: int) -> bytes:
    return _encode_varint((number << 3) | wire_type)


def _ld(data: bytes) -> bytes:
    return _encode_varint(len(data)) + data
