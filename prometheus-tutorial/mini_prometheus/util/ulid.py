"""A tiny ULID generator (Crockford base32, 26 characters).

ULIDs sort lexicographically by creation time, which is why Prometheus uses
them as block directory names.  This implementation is monotonic within the
same millisecond by incrementing the random part.
"""

from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last_ms = -1
_last_random = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(timestamp_ms: int | None = None) -> str:
    global _last_ms, _last_random
    now = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    with _lock:
        if now == _last_ms:
            _last_random += 1
        else:
            _last_ms = now
            _last_random = int.from_bytes(os.urandom(10), "big")
        random_part = _last_random & ((1 << 80) - 1)
    return _encode(now, 10) + _encode(random_part, 16)


def ulid_time(value: str) -> int:
    """Recover the millisecond timestamp from a ULID."""
    if len(value) != 26:
        raise ValueError(f"invalid ULID {value!r}")
    number = 0
    for char in value.upper():
        index = _ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"invalid ULID {value!r}")
        number = number * 32 + index
    return number >> 80
