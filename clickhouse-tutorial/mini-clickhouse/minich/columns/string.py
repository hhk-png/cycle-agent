"""字符串列的两种物化:**变长**与**定长**。

``ColumnString`` 是 ClickHouse 里最有教学价值的一个数据结构,因为它的
内存布局直接回答了"列存怎么存变长数据"这个问题:

.. code-block:: text

   ColumnString:  offsets(累积末尾偏移) + chars(所有字节挤在一起)
   ┌──────────────────────────────────────────────────────┐
   │ chars:   |h|e|l|l|o| |w|o|r|l|d|x| …                 │
   └──────────────────────────────────────────────────────┘
              ▲         ▲             ▲
   offsets:  [5,        11,           12, …]
              └ 第 0 个字符串是 chars[0:5] = "hello"
                        └ 第 1 个是 chars[5:11] = " world"
                                    └ 第 2 个是 chars[11:12] = "x"

三个要点:

1. **offsets 是累积的,不是长度**。这样取第 i 个字符串的起点是
   ``offsets[i-1]``、终点是 ``offsets[i]``,不需要前缀和;
   并且"第 i 个字符串的长度"= ``offsets[i] - offsets[i-1]``
   也是一次减法。
2. **chars 是一整块连续内存**。所以扫一整列字符串只需要顺序访问内存,
   而且压缩算法(LZ4/ZSTD)可以在这块上做窗口匹配 —— 变长数据在这里
   退化成了"一块字节",跟压缩定长列没有本质区别。
3. **append 是 O(1) 均摊**:往 chars 尾部写、往 offsets 尾部加一个数。

对比一下 Python 的 ``list[str]``:每个字符串是一个独立对象(带 49 字节
以上的头部),指针散落在堆里。这就是为什么"用 Python 写列存"必须显式
把布局做出来,否则讨论压缩率就没有意义了。

.. note::

   **String 是二进制安全的**。真实 ClickHouse 的 ``String`` 不校验编码,
   可以存任意字节(包括 0x00)。mini-clickhouse 用
   ``utf-8 + surrogateescape`` 解码,这样任意字节都能往返而不丢信息 ——
   你往一个 ``String`` 列里插 ``b"\\xff"``,取出来的字符串再编码回
   ``b"\\xff"``,一字不差。第 17 章讲格式时,这一条会决定 CSV 导出能不能
   无损往返。
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

from ..errors import ErrorCode, raise_error
from .base import IColumn

__all__ = ["ColumnString", "ColumnFixedString"]

#: 二进制安全的编解码参数 —— ``surrogateescape`` 让任意字节都能往返
_ENCODING = "utf-8"
_ERRORS = "surrogateescape"


class ColumnString(IColumn):
    """变长字符串列 —— ``offsets`` + ``chars``。"""

    def __init__(self, values: Optional[Sequence[str]] = None) -> None:
        self.offsets: List[int] = []
        self.chars = bytearray()
        if values:
            self.append_many(values)

    # ── 规模 ────────────────────────────────────────────────────────────

    def size(self) -> int:
        return len(self.offsets)

    # ── 读 ──────────────────────────────────────────────────────────────

    def _bounds(self, n: int) -> tuple:
        if n < 0 or n >= len(self.offsets):
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Index {n} out of bounds in ColumnString of size {len(self.offsets)}",
            )
        start = self.offsets[n - 1] if n > 0 else 0
        return start, self.offsets[n]

    def get_data_at(self, n: int) -> bytes:
        """取第 ``n`` 行的**原始字节**(不解码)—— 序列化时用这个。"""
        start, end = self._bounds(n)
        return bytes(self.chars[start:end])

    def get(self, n: int) -> str:
        return self.get_data_at(n).decode(_ENCODING, _ERRORS)

    def get_size_at(self, n: int) -> int:
        """第 ``n`` 行的字节长度(不算偏移数组的开销)。"""
        start, end = self._bounds(n)
        return end - start

    # ── 写 ──────────────────────────────────────────────────────────────

    def append_data(self, data: bytes) -> None:
        """直接追加一段字节(序列化反解时用,避免一次编解码往返)。"""
        self.chars += data
        self.offsets.append(len(self.chars))

    def append(self, value: Any) -> None:
        if isinstance(value, memoryview):
            value = bytes(value)
        if isinstance(value, (bytes, bytearray)):
            self.append_data(bytes(value))
            return
        if not isinstance(value, str):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Cannot insert {type(value).__name__} into a String column; "
                f"expected str or bytes",
            )
        self.chars += value.encode(_ENCODING, _ERRORS)
        self.offsets.append(len(self.chars))

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def append_many_default(self, n: int) -> None:
        # 空串:偏移重复 n 次,chars 不动 —— 比逐个 append("") 少做 n 次字节操作
        last = self.offsets[-1] if self.offsets else 0
        self.offsets.extend([last] * n)

    # ── 变换 ────────────────────────────────────────────────────────────

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        if start < 0 or length < 0 or start + length > other.size():
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"insert_range_from({start}, {length}) on column of size {other.size()}",
            )
        if isinstance(other, ColumnString):
            base = len(self.chars)
            for i in range(start, start + length):
                s, e = other.offsets[i - 1] if i > 0 else 0, other.offsets[i]
                self.chars += other.chars[s:e]
                self.offsets.append(len(self.chars))
            del base
            return
        super().insert_range_from(other, start, length)

    def insert_many_from(self, other: "IColumn", position: int, length: int) -> None:
        if length <= 0:
            return
        data = other.get_data_at(position) if isinstance(other, ColumnString) else \
            str(other.get(position)).encode(_ENCODING, _ERRORS)
        for _ in range(length):
            self.append_data(data)

    def cut(self, start: int, length: int) -> None:
        if start < 0 or length < 0 or start + length > self.size():
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"cut({start}, {length}) on column of size {self.size()}",
            )
        kept = [self.get_data_at(i) for i in range(start)] + [
            self.get_data_at(i) for i in range(start + length, self.size())
        ]
        self.offsets = []
        self.chars = bytearray()
        for d in kept:
            self.append_data(d)

    def pop_back(self, n: int = 1) -> None:
        if n < 0 or n > len(self.offsets):
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"pop_back({n}) on a ColumnString of size {len(self.offsets)}",
            )
        if n == 0:
            return
        new_size = len(self.offsets) - n
        new_len = self.offsets[new_size - 1] if new_size > 0 else 0
        del self.chars[new_len:]
        del self.offsets[new_size:]

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        if len(mask) != len(self.offsets):
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != column size {len(self.offsets)}",
            )
        kept = [self.get_data_at(i) for i, keep in enumerate(mask) if keep]
        self.offsets = []
        self.chars = bytearray()
        for d in kept:
            self.append_data(d)
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        out = ColumnString()
        for i in indexes:
            out.append_data(self.get_data_at(i))
        return out

    def get_many(self, indexes: Sequence[int]) -> "IColumn":
        return self.permute(indexes)

    def replicate(self, offsets: Sequence[int]) -> "IColumn":
        out = ColumnString()
        prev = 0
        for i, off in enumerate(offsets):
            data = self.get_data_at(i)
            for _ in range(off - prev):
                out.append_data(data)
            prev = off
        return out

    # ── 比较 ────────────────────────────────────────────────────────────

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        # 先比字节长度? 不行 —— 那是字典序的错。真实 CH 用的是
        # memcmp 语义(逐字节比较,短的先结束则更小),
        # 恰好等价于 Python 的 bytes 比较。
        a = self.get_data_at(n)
        b = other.get_data_at(m) if isinstance(other, ColumnString) else \
            str(other.get(m)).encode(_ENCODING, _ERRORS)
        if a == b:
            return 0
        return -1 if a < b else 1

    def hash_value(self, n: int) -> Any:
        return self.get_data_at(n)

    # ── 统计 ────────────────────────────────────────────────────────────

    def byte_size(self) -> int:
        # 真实 CH 的 ColumnString::byteSize() = chars.size() + offsets.size()*8
        return len(self.chars) + len(self.offsets) * 8

    def clone_empty(self) -> "IColumn":
        return ColumnString()

    def default_value(self) -> Any:
        return ""

    def dump_structure(self, indent: int = 0) -> str:  # pragma: no cover
        pad = "  " * indent
        return (
            f"{pad}ColumnString(size={self.size()}, chars={len(self.chars)}B, "
            f"offsets={len(self.offsets)}×8B)"
        )


class ColumnFixedString(IColumn):
    """``FixedString(N)`` 列 —— 每行恰好 N 字节,没有偏移数组。

    布局比 ``ColumnString`` 还简单:一整块 ``N × rows`` 的字节,

    .. code-block:: text

      chars: |a|b|c|\\0|  |x|y|\\0|\\0|  |1|2|3|4|
             └── 第0行 ──┘  └── 第1行 ──┘  └─ 第2行 ─┘

    取第 i 行就是 ``chars[i*N : (i+1)*N]`` —— **一次乘法,零间接**。
    这就是它比 ``String`` 快的原因,也是它的代价:定长必须补齐。

    补齐规则(与真实 ClickHouse 一致):

    * 短于 N 的串**补 0 字节**(不是补空格);
    * 长于 N 的串**抛异常**,不截断 —— 真实 CH 的报错是
      ``SIZE_OF_FIXED_STRING_DOESNT_MATCH``。这条很关键:
      静默截断会让用户以为数据存进去了。

    典型用途:定长 ID、哈希摘要、二进制协议字段。
    """

    def __init__(self, n: int, values: Optional[Sequence[Any]] = None) -> None:
        if n <= 0:
            raise_error(
                ErrorCode.SIZE_OF_FIXED_STRING_DOESNT_MATCH,
                f"FixedString size must be positive, got {n}",
            )
        self.n = n
        self.chars = bytearray()
        if values:
            self.append_many(values)

    def size(self) -> int:
        return len(self.chars) // self.n

    def _bounds(self, i: int) -> tuple:
        if i < 0 or i >= self.size():
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Index {i} out of bounds in ColumnFixedString(N={self.n}) "
                f"of size {self.size()}",
            )
        return i * self.n, (i + 1) * self.n

    def get_data_at(self, n: int) -> bytes:
        """返回**定长 N 字节**(不剥尾部 0)—— 序列化与比较用。"""
        s, e = self._bounds(n)
        return bytes(self.chars[s:e])

    def get(self, n: int) -> str:
        """返回**剥掉尾部 0 字节**后的字符串(用户看到的样子)。"""
        return self.get_data_at(n).rstrip(b"\x00").decode(_ENCODING, _ERRORS)

    def append(self, value: Any) -> None:
        if isinstance(value, str):
            data = value.encode(_ENCODING, _ERRORS)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            data = bytes(value)
        else:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Cannot insert {type(value).__name__} into FixedString({self.n})",
            )
        if len(data) > self.n:
            raise_error(
                ErrorCode.SIZE_OF_FIXED_STRING_DOESNT_MATCH,
                f"Too large value for FixedString({self.n}): "
                f"{len(data)} bytes ({value!r})",
            )
        self.chars += data
        self.chars += b"\x00" * (self.n - len(data))

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def append_many_default(self, n: int) -> None:
        self.chars += b"\x00" * (self.n * n)

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        if start < 0 or length < 0 or start + length > other.size():
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, "insert_range_from out of bounds")
        if isinstance(other, ColumnFixedString) and other.n == self.n:
            self.chars += other.chars[start * self.n : (start + length) * self.n]
            return
        super().insert_range_from(other, start, length)

    def cut(self, start: int, length: int) -> None:
        del self.chars[start * self.n : (start + length) * self.n]

    def pop_back(self, n: int = 1) -> None:
        if n > self.size():
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"pop_back({n}) too large")
        del self.chars[(self.size() - n) * self.n :]

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        if len(mask) != self.size():
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != column size {self.size()}",
            )
        out = bytearray()
        for i, keep in enumerate(mask):
            if keep:
                s, e = self._bounds(i)
                out += self.chars[s:e]
        self.chars = out
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        out = ColumnFixedString(self.n)
        for i in indexes:
            s, e = self._bounds(i)
            out.chars += self.chars[s:e]
        return out

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        a = self.get_data_at(n)
        b = other.get_data_at(m) if isinstance(other, ColumnFixedString) else None
        if b is None:
            b = str(other.get(m)).encode(_ENCODING, _ERRORS)
            b = b + b"\x00" * max(0, self.n - len(b))
        if a == b:
            return 0
        return -1 if a < b else 1

    def hash_value(self, n: int) -> Any:
        return self.get_data_at(n)

    def byte_size(self) -> int:
        return len(self.chars)

    def clone_empty(self) -> "IColumn":
        return ColumnFixedString(self.n)

    def default_value(self) -> Any:
        return ""
