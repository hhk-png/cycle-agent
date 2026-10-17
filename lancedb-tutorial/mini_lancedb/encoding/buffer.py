"""缓冲区、页表与对齐。

Lance 文件在字节层面只有两种东西：

* **buffer（缓冲区）** —— 一段不可变的连续字节，页面的内容都在这里；
* **page table（页表）** —— 一个 ``(offset, length)`` 的数组，指向 buffer。

文件里所有"结构信息"（每个列有哪些页、每页用了什么编码、这一页的 min/max）
都放在页表之后的 column metadata 里。读取时只要先读文件尾部的
固定长度 footer（几十字节），就能知道整张表在哪，然后**按需**只读需要的
那几页 —— 这是列存和行存最本质的区别。

对齐：Lance 把 buffer 的起始位置对齐到 64 字节。原因很实际：

* NVMe / 对象存储的预读粒度通常是 4 KiB，64 字节对齐能让一次
  ``pread`` 尽量落在同一个物理页里；
* SIMD 解码器（AVX-512 是 64 字节宽一条指令）对对齐内存的
  未对齐访问惩罚最小；
* mmap 之后如果按 64 字节对齐，很多操作可以保证不跨 cache line。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..errors import CorruptedFileError

__all__ = [
    "ALIGNMENT",
    "align_up",
    "is_aligned",
    "BufferLocation",
    "PageTable",
    "Buffer",
    "BufferRole",
    "pack_locations",
    "unpack_locations",
]

#: 缓冲区对齐字节数。真实 Lance 用的是 64。
ALIGNMENT = 64

# 页表项：offset(u64) + size(u64) = 16 字节
_LOCATION_STRUCT = struct.Struct("<QQ")
LOCATION_SIZE = _LOCATION_STRUCT.size


class BufferRole:
    """缓冲区在页面里扮演的角色。

    Lance v2 的 flatbuffer 里每个 buffer 都带一个类型标记，读的时候
    才知道"这几段字节分别是什么"。这里用字符串常量表达同样的意思。
    """

    #: 数值/编码后的主数据
    VALUES = "values"
    #: 有效性位图（哪些行是 null）
    VALIDITY = "validity"
    #: 字典的取值表
    DICTIONARY = "dictionary"
    #: 行号索引 / 下标
    INDICES = "indices"
    #: 变长类型的 offsets
    OFFSETS = "offsets"
    #: 变长类型的原始字节
    DATA = "data"
    #: 页级统计（min/max/null_count）
    STATS = "stats"
    #: 压缩字典表等辅助数据
    AUX = "aux"


def align_up(n: int, alignment: int = ALIGNMENT) -> int:
    """把 ``n`` 向上取整到 ``alignment`` 的倍数。"""

    if alignment <= 1:
        return n
    return (n + alignment - 1) // alignment * alignment


def is_aligned(n: int, alignment: int = ALIGNMENT) -> bool:
    return n % alignment == 0


@dataclass(frozen=True)
class BufferLocation:
    """一个 buffer 在文件里的位置。页表的元素就是它。"""

    offset: int
    size: int

    @property
    def end(self) -> int:
        return self.offset + self.size

    def __str__(self) -> str:  # pragma: no cover
        return f"[{self.offset}:{self.offset + self.size})"


class PageTable:
    """页表 —— 文件里一个扁平的 ``BufferLocation[]``。

    设计上只有一个要求：**固定宽度、可随机访问**。这样读取任意一个
    buffer 的成本都是 ``O(1)``（先读页表，再读那一段），不需要从头解析。
    """

    __slots__ = ("entries",)

    def __init__(self, entries: Optional[List[BufferLocation]] = None) -> None:
        self.entries: List[BufferLocation] = list(entries or [])

    def __len__(self) -> int:
        return len(self.entries)

    def __getitem__(self, i: int) -> BufferLocation:
        return self.entries[i]

    def __iter__(self):
        return iter(self.entries)

    def append(self, offset: int, size: int) -> int:
        self.entries.append(BufferLocation(offset, size))
        return len(self.entries) - 1

    def add(self, loc: BufferLocation) -> int:
        self.entries.append(loc)
        return len(self.entries) - 1

    def get(self, i: int) -> BufferLocation:
        try:
            return self.entries[i]
        except IndexError:
            raise CorruptedFileError(
                "页表越界", index=i, table_size=len(self.entries)
            ) from None

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    def to_bytes(self) -> bytes:
        return pack_locations(self.entries)

    @staticmethod
    def from_bytes(data: bytes) -> "PageTable":
        return PageTable(unpack_locations(data))


def pack_locations(locations: Sequence[BufferLocation]) -> bytes:
    """页表序列化：定长 16 字节/项，小端。"""

    out = bytearray(len(locations) * LOCATION_SIZE)
    mv = memoryview(out)
    for i, loc in enumerate(locations):
        _LOCATION_STRUCT.pack_into(mv, i * LOCATION_SIZE, loc.offset, loc.size)
    return bytes(out)


def unpack_locations(data: bytes) -> List[BufferLocation]:
    if len(data) % LOCATION_SIZE != 0:
        raise CorruptedFileError(
            "页表字节数不是 16 的整数倍", size=len(data)
        )
    out: List[BufferLocation] = []
    for i in range(0, len(data), LOCATION_SIZE):
        off, size = _LOCATION_STRUCT.unpack_from(data, i)
        out.append(BufferLocation(off, size))
    return out


@dataclass
class Buffer:
    """一个已经写好的缓冲区。"""

    data: bytes
    role: str = BufferRole.VALUES
    #: 所属页号（便于调试时看清"这一页由哪几段组成"）
    page: int = 0

    @property
    def size(self) -> int:
        return len(self.data)

    def __str__(self) -> str:  # pragma: no cover
        return f"Buffer({self.role}, {self.size} B)"


@dataclass
class BufferPlan:
    """写文件前的"排布计划"。

    把"有哪些 buffer、各自多大、对齐后放在哪"这件事独立出来，
    是因为它直接决定了文件大小和后续的读放大：

    * 对齐会把文件撑大（平均浪费 32 字节/buffer），
    * 但换来的是范围读的次数变少、页边界不跨物理块。

    真实 Lance 里这套逻辑就是 ``page_table`` 加一层 offset 计算。
    """

    alignment: int = ALIGNMENT
    _locations: List[BufferLocation] = dc_field(default_factory=list)
    _cursor: int = 0

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def padding_bytes(self) -> int:
        """对齐带来的额外字节数 —— 衡量"对齐的代价"。"""

        return sum(
            align_up(loc.offset, self.alignment) - loc.offset for loc in self._locations
        )

    def place(self, size: int) -> BufferLocation:
        start = align_up(self._cursor, self.alignment)
        loc = BufferLocation(start, size)
        self._locations.append(loc)
        self._cursor = start + size
        return loc

    def locations(self) -> List[BufferLocation]:
        return list(self._locations)

    def plan(self, sizes: Iterable[int]) -> List[BufferLocation]:
        return [self.place(s) for s in sizes]

    def summary(self) -> Dict[str, Any]:
        return {
            "buffers": len(self._locations),
            "payload_bytes": sum(l.size for l in self._locations),
            "aligned_bytes": align_up(self._cursor, self.alignment),
            "padding_bytes": self.padding_bytes,
        }
