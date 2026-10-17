"""编解码小工具：varint 与增量编码。

放在这里而不是 ``encoding/codecs.py`` 里，是为了让 ``format`` 包
不必依赖整个编码栈 —— 删除文件、事务文件这些元数据只想用
最朴素的 varint，不需要把 LZ4、FSST 全都拉进来。
"""

from __future__ import annotations

from typing import List, Sequence

from ..errors import CorruptedFileError

__all__ = [
    "varint_encode_list",
    "varint_decode_list",
    "delta_encode_list",
    "delta_decode_list",
]


def zigzag(x: int) -> int:
    return (x << 1) ^ (x >> 63) if x < 0 else (x << 1)


def unzigzag(u: int) -> int:
    return (u >> 1) ^ -(u & 1)


def varint_encode_list(values: Sequence[int]) -> bytes:
    """LEB128 编码（走 zigzag，所以负数也是小字节）。"""

    out = bytearray()
    for v in values:
        u = zigzag(int(v))
        while True:
            b = u & 0x7F
            u >>= 7
            if u:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
    return bytes(out)


def varint_decode_list(data: bytes, count: int) -> List[int]:
    out: List[int] = []
    i = 0
    n = len(data)
    while len(out) < count:
        shift = 0
        u = 0
        while True:
            if i >= n:
                raise CorruptedFileError("varint 数据被截断", decoded=len(out), expected=count)
            b = data[i]
            i += 1
            u |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
            if shift > 63:
                raise CorruptedFileError("varint 超过 64 位")
        out.append(unzigzag(u))
    return out


def delta_encode_list(values: Sequence[int]) -> bytes:
    """首值 + 增量。用于"单调递增的行号列表"。"""

    if len(values) == 0:
        return b""
    vals = [int(v) for v in values]
    return varint_encode_list([vals[0]] + [vals[i] - vals[i - 1] for i in range(1, len(vals))])


def delta_decode_list(data: bytes, count: int) -> List[int]:
    if count == 0:
        return []
    deltas = varint_decode_list(data, count)
    out = [deltas[0]]
    for d in deltas[1:]:
        out.append(out[-1] + d)
    return out
