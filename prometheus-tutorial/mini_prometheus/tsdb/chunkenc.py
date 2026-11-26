"""Gorilla-style XOR chunk encoding.

Prometheus stores samples in fixed-size chunks using the Facebook Gorilla
scheme: integer timestamps are delta-of-delta encoded, floating point values
are XORed with the previous value and only the meaningful middle bits are
written.  This implementation follows the same idea (and bit layout for
timestamps) but is *not* byte-compatible with Prometheus' ``chunkenc``
package: the value-window control bits are simplified to one "reuse/new"
flag.  The round-trip properties and the compression behaviour are the same,
which is what the tutorial tries to teach.
"""

from __future__ import annotations

import struct

_DEFAULT_SAMPLES_PER_CHUNK = 120


class BitWriter:
    __slots__ = ("_buffer", "_bits")

    def __init__(self) -> None:
        self._buffer = 0
        self._bits = 0

    def write_bit(self, bit: int) -> None:
        self._buffer = (self._buffer << 1) | (bit & 1)
        self._bits += 1

    def write_bits(self, value: int, count: int) -> None:
        if count < 0 or count > 64:
            raise ValueError("bit count out of range")
        value &= (1 << count) - 1 if count < 64 else (1 << 64) - 1
        self._buffer = (self._buffer << count) | value
        self._bits += count

    def bytes(self) -> bytes:
        padding = (-self._bits) % 8
        value = self._buffer << padding
        length = (self._bits + padding) // 8
        return value.to_bytes(length, "big") if length else b""

    @property
    def bit_length(self) -> int:
        return self._bits


class BitReader:
    __slots__ = ("_data", "_position", "_total")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0
        self._total = len(data) * 8

    def read_bit(self) -> int:
        if self._position >= self._total:
            raise EOFError("unexpected end of chunk")
        byte = self._data[self._position // 8]
        bit = (byte >> (7 - (self._position % 8))) & 1
        self._position += 1
        return bit

    def read_bits(self, count: int) -> int:
        if count < 0 or count > 64:
            raise ValueError("bit count out of range")
        if self._position + count > self._total:
            raise EOFError("unexpected end of chunk")
        value = 0
        remaining = count
        while remaining:
            byte_index = self._position // 8
            bit_offset = self._position % 8
            available = 8 - bit_offset
            take = min(available, remaining)
            chunk = self._data[byte_index]
            value = (value << take) | (
                (chunk >> (available - take)) & ((1 << take) - 1)
            )
            self._position += take
            remaining -= take
        return value

    @property
    def position(self) -> int:
        return self._position


def _leading_zeros(value: int) -> int:
    if value == 0:
        return 64
    return 64 - value.bit_length()


def _trailing_zeros(value: int) -> int:
    if value == 0:
        return 64
    return (value & -value).bit_length() - 1


class XORChunk:
    __slots__ = ("_writer", "_num", "_min_time", "_max_time", "_prev_ts", "_prev_delta",
                 "_prev_value", "_prev_leading", "_prev_trailing", "_has_window",
                 "_sealed", "_raw", "_decoded")

    def __init__(self) -> None:
        self._writer = BitWriter()
        self._num = 0
        self._min_time = 0
        self._max_time = 0
        self._prev_ts = 0
        self._prev_delta = 0
        self._prev_value = 0
        self._prev_leading = 0
        self._prev_trailing = 0
        self._has_window = False
        self._sealed = False
        self._raw: bytes | None = None

    @property
    def num_samples(self) -> int:
        return self._num

    @property
    def min_time(self) -> int:
        return self._min_time

    @property
    def max_time(self) -> int:
        return self._max_time

    def append(self, timestamp: int, value: float) -> None:
        if self._sealed:
            raise RuntimeError("cannot append to a sealed chunk")
        if self._num == 0:
            self._min_time = timestamp
            self._max_time = timestamp
            self._write_u64(timestamp)
            self._write_u64(struct.unpack(">Q", struct.pack(">d", float(value)))[0])
            self._prev_ts = timestamp
            self._prev_value = value
            self._num = 1
            return
        if timestamp <= self._prev_ts:
            raise ValueError("chunk timestamps must be strictly increasing")
        delta = timestamp - self._prev_ts
        ddd = delta - self._prev_delta
        self._write_delta_of_delta(ddd)
        self._prev_delta = delta
        self._prev_ts = timestamp
        self._write_value(value)
        self._prev_value = value
        self._max_time = timestamp
        self._num += 1

    def _write_u64(self, value: int) -> None:
        self._writer.write_bits(value, 64)

    def _write_delta_of_delta(self, ddd: int) -> None:
        if ddd == 0:
            self._writer.write_bit(0)
            return
        self._writer.write_bit(1)
        if -8192 <= ddd < 8192:
            self._writer.write_bits(0, 2)
            self._writer.write_bits(ddd & 0x3FFF, 14)
        elif -65536 <= ddd < 65536:
            self._writer.write_bits(1, 2)
            self._writer.write_bits(ddd & 0x1FFFF, 17)
        elif -524288 <= ddd < 524288:
            self._writer.write_bits(2, 2)
            self._writer.write_bits(ddd & 0xFFFFF, 20)
        else:
            self._writer.write_bits(3, 2)
            self._writer.write_bits(ddd & ((1 << 64) - 1), 64)

    def _write_value(self, value: float) -> None:
        bits = struct.unpack(">Q", struct.pack(">d", float(value)))[0]
        previous = struct.unpack(">Q", struct.pack(">d", float(self._prev_value)))[0]
        xor = bits ^ previous
        if xor == 0:
            self._writer.write_bit(0)
            return
        self._writer.write_bit(1)
        leading = _leading_zeros(xor)
        trailing = _trailing_zeros(xor)
        if (
            self._has_window
            and leading >= self._prev_leading
            and trailing >= self._prev_trailing
        ):
            self._writer.write_bit(0)
            meaningful = 64 - self._prev_leading - self._prev_trailing
            self._writer.write_bits(xor >> self._prev_trailing, meaningful)
            return
        self._writer.write_bit(1)
        self._writer.write_bits(leading, 6)
        meaningful = 64 - leading - trailing
        self._writer.write_bits(meaningful - 1, 6)
        self._writer.write_bits(xor >> trailing, meaningful)
        self._prev_leading = leading
        self._prev_trailing = trailing
        self._has_window = True

    def seal(self) -> bytes:
        if self._raw is None:
            payload = self._writer.bytes()
            self._raw = struct.pack(">I", self._num) + payload
            self._sealed = True
        return self._raw

    def bytes(self) -> bytes:
        return self.seal()

    def snapshot_bytes(self) -> bytes:
        """Serialize without sealing; safe for snapshots of a live head."""
        if self._raw is not None:
            return self._raw
        return struct.pack(">I", self._num) + self._writer.bytes()

    @classmethod
    def from_bytes(cls, data: bytes) -> "XORChunk":
        chunk = cls()
        chunk._raw = data
        chunk._sealed = True
        chunk._num = struct.unpack(">I", data[:4])[0]
        chunk._decode()
        return chunk

    def _decode(self) -> None:
        assert self._raw is not None
        reader = BitReader(self._raw[4:])
        count = self._num
        if count == 0:
            self._decoded = []
            return
        raw_timestamp = reader.read_bits(64)
        # Timestamps are stored as 64 raw bits; sign-extend so legal negative
        # timestamps (pre-1970 data, remote write) round-trip correctly.
        timestamp = (
            raw_timestamp - (1 << 64)
            if raw_timestamp >= (1 << 63)
            else raw_timestamp
        )
        value_bits = reader.read_bits(64)
        value = struct.unpack(">d", struct.pack(">Q", value_bits))[0]
        samples = [(timestamp, value)]
        prev_ts = timestamp
        prev_delta = 0
        prev_value_bits = value_bits
        prev_leading = 0
        prev_trailing = 0
        has_window = False
        for _ in range(1, count):
            if reader.read_bit() == 0:
                ddd = 0
            else:
                control = reader.read_bits(2)
                if control == 0:
                    raw = reader.read_bits(14)
                    ddd = raw - (1 << 14) if raw >= (1 << 13) else raw
                elif control == 1:
                    raw = reader.read_bits(17)
                    ddd = raw - (1 << 17) if raw >= (1 << 16) else raw
                elif control == 2:
                    raw = reader.read_bits(20)
                    ddd = raw - (1 << 20) if raw >= (1 << 19) else raw
                else:
                    raw = reader.read_bits(64)
                    ddd = raw - (1 << 64) if raw >= (1 << 63) else raw
            delta = prev_delta + ddd
            timestamp = prev_ts + delta
            prev_delta = delta
            if reader.read_bit() == 0:
                value_bits = prev_value_bits
            else:
                if reader.read_bit() == 0:
                    if not has_window:
                        raise ValueError("corrupt chunk: value window reused before set")
                    meaningful = 64 - prev_leading - prev_trailing
                    xor = reader.read_bits(meaningful) << prev_trailing
                else:
                    leading = reader.read_bits(6)
                    meaningful = reader.read_bits(6) + 1
                    trailing = 64 - leading - meaningful
                    if trailing < 0:
                        raise ValueError("corrupt chunk: invalid value window")
                    xor = reader.read_bits(meaningful) << trailing
                    prev_leading = leading
                    prev_trailing = trailing
                    has_window = True
                value_bits = prev_value_bits ^ xor
            value = struct.unpack(">d", struct.pack(">Q", value_bits))[0]
            samples.append((timestamp, value))
            prev_ts = timestamp
            prev_value_bits = value_bits
        self._decoded = samples  # type: ignore[attr-defined]
        self._min_time = samples[0][0]
        self._max_time = samples[-1][0]

    def samples(self) -> list[tuple[int, float]]:
        if self._raw is not None:
            if not hasattr(self, "_decoded"):
                self._decode()
            return self._decoded  # type: ignore[attr-defined]
        if not self._num:
            return []
        # Reading must not seal an active chunk: queries during ingestion are
        # routine, and sealing would make the next append fail.  Decode a
        # temporary copy from the current bit stream instead.
        snapshot = struct.pack(">I", self._num) + self._writer.bytes()
        return XORChunk.from_bytes(snapshot).samples()

    def __len__(self) -> int:
        return self._num

    def __repr__(self) -> str:
        return f"XORChunk(n={self._num}, {self._min_time}..{self._max_time})"


DEFAULT_SAMPLES_PER_CHUNK = _DEFAULT_SAMPLES_PER_CHUNK
