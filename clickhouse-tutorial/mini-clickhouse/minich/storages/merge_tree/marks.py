"""granule(颗粒)与 mark(标记)—— MergeTree 索引体系的原子单位。

.. code-block:: text

   ┌────────────────────────────────────────────────────────────────────┐
   │  一个 part 的数据,按"granule"分段 —— granule 是**读写的最小单位**   │
   └────────────────────────────────────────────────────────────────────┘

   part 的物理布局(以 ``ORDER BY (id)``、``index_granularity = 4`` 为例):

   行号:   0    1    2    3  │  4    5    6    7  │  8    9
           └──── granule 0 ──┘└──── granule 1 ──┘└─ granule 2 ─
                              ▲                  ▲
                              │                  │
   primary.idx:            id=0                id=4                id=8
                          (每个 granule 的第一个**排序键值**)

   <id>.bin:      [压缩块0][压缩块1][压缩块2]      ← 一个 granule 一个压缩块
   <id>.mrk2:      (0, 0, 4)(45, 0, 4)(90, 0, 2)   ← 定位到每个 granule 的三元组
                    ▲  ▲  ▲
                    │  │  └ rows_in_granule      = 4 / 4 / 2
                    │  └── offset_in_decompressed_block = 0
                    └───── offset_in_compressed_file  = 45 / 90

.. code-block:: text

   ┌────────────────────────────────────────────────────────────────────┐
   │  三个概念必须分清,混起来就会写出错误的索引代码:                     │
   └────────────────────────────────────────────────────────────────────┘

   * **granule** —— "若干**连续的行**",数量由 ``index_granularity`` 决定;
   * **mark**   —— "一个 granule 在某个列文件里的**位置**",
     三元组 ``(offset_in_compressed_file, offset_in_decompressed_block,
     rows_in_granule)``,存在 ``<column>.mrk2`` 里;
   * **primary.idx** —— "每个 granule 的第一行的**排序键值**",
     稀疏索引查的就是它。

   于是"用索引跳过数据"的完整链条是:

   .. code-block:: text

     WHERE user_id = 42                    ← SQL
        │  KeyCondition
        ▼
     user_id ∈ [42, 42]                    ← 值域
        │  在 primary.idx 上二分
        ▼
     mark 17 .. mark 17                    ← granule 号(MarkRange)
        │  查 <user_id>.mrk2
        ▼
     (offset=1234, decomp=0, rows=8192)    ← 字节偏移
        │  只解压这一段
        ▼
     8192 行的 user_id 列                    ← 实际读到的数据

   .. note::

      **为什么稀疏索引就够了?** 因为数据在 part 内**按排序键有序**。
      既然有序,``id=42`` 只可能出现在"第一个 id >= 42 的 granule"里。
      稀疏索引的"疏"换来的是:**100 万行的 part,索引可能只有 200 行** ——
      索引小到可以常驻内存,而定位依然是 O(log n) 次比较。
      这就是 ClickHouse 敢在"海量数据"上做实时查询的底层原因。

   .. warning::

      **每个 granule 是一个独立压缩块(mini-clickhouse 的简化)。**
      真实 ClickHouse 会把**多个 granule 打包进同一个压缩块**
      (``min_compress_block_size`` 默认 64 KiB、``max_compress_block_size``
      默认 1 MiB),这样块头与解压的固定开销被摊薄。代价是
      ``.mrk2`` 的第二列 ``offset_in_decompressed_block`` 才有意义
      ——「跳过前面几个 granule,从块内这个字节偏移开始读」。
      mini-clickhouse 一个 granule 一个块,所以这一列**恒为 0**;
      好处是"读第 i 个 granule 只需 seek + 解压一个块",教学演示更直接。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from ...errors import ErrorCode, raise_error
from ...util.stopwatch import ProfileEvents

__all__ = [
    "Mark",
    "MarkRange",
    "RANGE_MAX",
    "round_up_to_multiple",
    "round_down_to_multiple",
    "mark_range_for_rows",
    "IndexGranularity",
    "DEFAULT_INDEX_GRANULARITY",
    "DEFAULT_INDEX_GRANULARITY_BYTES",
    "MIN_INDEX_GRANULARITY_BYTES",
]


# ══════════════════════════════════════════════════════════════════════════
# 真实 ClickHouse 的默认常量
# ══════════════════════════════════════════════════════════════════════════

#: ``index_granularity`` 的默认值 —— 真实 ClickHouse 就是 8192。
#:
#: 为什么是 8192? 这是"索引大小"与"读放大"的折中:
#:
#: * 太大 → 索引小、内存省,但每读一个 granule 都要解压很多用不上的行;
#: * 太小 → 索引大(内存吃紧),而且每个 granule 都要摊一次块头开销。
#:
#: 8192 恰好让"主键为 UInt64"时索引约为数据量的 ``8/8192 ≈ 0.1%``,
#: 而一次 granule 解压大约是几十微秒量级 —— 两边都在舒适区。
DEFAULT_INDEX_GRANULARITY = 8192

#: ``index_granularity_bytes`` 的默认值 —— 10 MiB。**自适应粒度**的上界。
DEFAULT_INDEX_GRANULARITY_BYTES = 10 * 1024 * 1024

#: ``min_index_granularity_bytes`` 的默认值 —— 1 KiB。
#: 自适应切分时,"再小的 granule 也不能小于这个字节数",
#: 否则宽行表(一行 1 MB)会退化成"一行一个 granule",索引反而爆炸。
MIN_INDEX_GRANULARITY_BYTES = 1024

#: 用 ``UInt64`` 的最大值当作"无穷远"的 mark 号 —— 真实 CH 的 ``RANGE_MAX``。
RANGE_MAX = (1 << 64) - 1


# ══════════════════════════════════════════════════════════════════════════
# Mark:一个 granule 在某个列文件里的位置
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Mark:
    """``<column>.mrk2`` 里的一行 —— 定位一个 granule。

    字段顺序与真实 ClickHouse 的 ``MarkInCompressedFile`` **完全一致**:

    .. code-block:: text

       real ClickHouse 的 .mrk2 每个 granule 记两个数:
         offset_in_compressed_file     在该列 .bin 文件里的字节偏移
         offset_in_decompressed_block  在**解压后**数据流里的字节偏移

       而 "rows_in_granule" 在 .mrk2 里**只记一次**(每行 mark 的末尾),
       不在每个 (offset, offset) 对里重复

    我们把这个三元组直接建模成一个对象,读起来更清楚;
    序列化时仍按真实格式写(见 :mod:`minich.storages.merge_tree.part`)。

    ``rows_in_granule`` 是**这个 granule 的行数** —— 它不是常数!
    最后一个 granule 通常不满,自适应粒度下更是个个不同。
    任何"granule 大小 = index_granularity"的假设都是 bug 的温床。
    """

    offset_in_compressed_file: int
    offset_in_decompressed_block: int
    rows_in_granule: int

    def __post_init__(self) -> None:
        if self.offset_in_compressed_file < 0 or self.offset_in_decompressed_block < 0:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"Mark offsets must be non-negative: {self}",
            )
        if self.rows_in_granule < 0:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"Mark rows_in_granule must be non-negative: {self}",
            )

    def as_index_granule(self) -> tuple:
        return (self.offset_in_compressed_file, self.offset_in_decompressed_block)

    def __repr__(self) -> str:  # pragma: no cover - 纯格式化
        return (
            f"Mark(comp={self.offset_in_compressed_file}, "
            f"decomp={self.offset_in_decompressed_block}, "
            f"rows={self.rows_in_granule})"
        )


# ══════════════════════════════════════════════════════════════════════════
# MarkRange:一段连续的 granule
# ══════════════════════════════════════════════════════════════════════════

#: 用 ``RANGE_MAX`` 表示"到最后一个 granule"(而不是 None)——
#: 与真实 ClickHouse 的 ``MarkRange`` 一致,好处是**比较与算数都能直接用**。
@dataclass
class MarkRange:
    """``[begin, end)`` 形式的 granule 区间 —— 半开区间!

    半开区间是刻意选的:``MarkRange(0, 3)`` 表示 granule 0/1/2。
    这样"两段不相交"正好是 ``a.end <= b.begin``,不需要 ±1 的心算 ——
    真实 ClickHouse 的 ``MarkRange`` 也是半开区间,并且用同一个理由。

    ``end = RANGE_MAX`` 表示"一直到头"。注意 ``RANGE_MAX`` 是一个
    **哨兵值**而不是"无穷大" —— 它比任何真实 mark 号都大,但参与
    加减小心的代码会出错,所以下面所有方法都避免对它做算术。
    """

    begin: int = 0
    end: int = RANGE_MAX

    # ── 基本属性 ────────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        return self.begin >= self.end

    def is_full_range(self) -> bool:
        return self.begin == 0 and self.end == RANGE_MAX

    def get_marks_count(self) -> int:
        if self.end == RANGE_MAX:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                "MarkRange.get_marks_count() on an open-ended range; "
                "bind it to a part first with `with_end(n)`",
            )
        return max(0, self.end - self.begin)

    def with_end(self, end: int) -> "MarkRange":
        """把开区间收口到一个具体的 ``end``(读写 part 前必须先收口)。"""
        return MarkRange(self.begin, min(self.end, end))

    def with_begin(self, begin: int) -> "MarkRange":
        return MarkRange(max(self.begin, begin), self.end)

    # ── 集合运算 ────────────────────────────────────────────────────────

    def intersect_with(self, other: "MarkRange") -> "MarkRange":
        """求交 —— 结果可能是空区间(``is_empty()`` 为真)。"""
        return MarkRange(max(self.begin, other.begin), min(self.end, other.end))

    def contains(self, other: "MarkRange") -> bool:
        return self.begin <= other.begin and other.end <= self.end

    def intersects(self, other: "MarkRange") -> bool:
        return self.begin < other.end and other.begin < self.end

    def union_with(self, other: "MarkRange") -> "MarkRange":
        """求并(要求两段**相邻或相交**)—— 用于把碎片合成大段。"""
        if self.end < other.begin or other.end < self.begin:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"Cannot union disjoint ranges {self} and {other}: "
                f"there would be a hole in between",
            )
        return MarkRange(min(self.begin, other.begin), max(self.end, other.end))

    def __repr__(self) -> str:  # pragma: no cover
        end = "MAX" if self.end == RANGE_MAX else str(self.end)
        return f"MarkRange({self.begin}, {end})"


# ══════════════════════════════════════════════════════════════════════════
# 区间算术
# ══════════════════════════════════════════════════════════════════════════


def round_up_to_multiple(x: int, m: int) -> int:
    """把 ``x`` 向上取整到 ``m`` 的倍数(``m`` 必须是正数)。"""
    if m <= 0:
        raise_error(ErrorCode.LOGICAL_ERROR, f"Multiple must be positive, got {m}")
    return ((x + m - 1) // m) * m


def round_down_to_multiple(x: int, m: int) -> int:
    """把 ``x`` 向下取整到 ``m`` 的倍数。"""
    if m <= 0:
        raise_error(ErrorCode.LOGICAL_ERROR, f"Multiple must be positive, got {m}")
    return (x // m) * m


def mark_range_for_rows(rows: int, index_granularity: int) -> MarkRange:
    """覆盖 ``[0, rows)`` 这些行所需的 mark 区间。

    一个 part 有 ``rows`` 行、粒度是 ``g``,那么 mark 数是
    ``ceil(rows / g)`` —— 注意是 **向上**取整:最后一个不满的 granule
    也算一个 mark,否则最后那些行就"没有索引"了。
    """
    if rows < 0:
        raise_error(ErrorCode.LOGICAL_ERROR, f"rows must be non-negative, got {rows}")
    marks = (rows + index_granularity - 1) // index_granularity if index_granularity else 0
    return MarkRange(0, marks)


def intersect_ranges(lhs: Sequence[MarkRange], rhs: Sequence[MarkRange]) -> List[MarkRange]:
    """两个 MarkRange 列表求交 —— 结果已排序且**已合并相邻段**。

    双指针扫描:两个列表都按 ``begin`` 有序,所以可以线性求交
    (真实 ClickHouse 的 ``MarkRanges`` 同样假设有序并做线性归并)。
    """
    out: List[MarkRange] = []
    i = j = 0
    while i < len(lhs) and j < len(rhs):
        r = lhs[i].intersect_with(rhs[j])
        if not r.is_empty():
            out.append(r)
        if lhs[i].end <= rhs[j].end:
            i += 1
        else:
            j += 1
    return merge_ranges(out)


def merge_ranges(ranges: Sequence[MarkRange]) -> List[MarkRange]:
    """把重叠/相邻的区间合并 —— 减少后续 IO 的碎片。

    真实 ClickHouse 在拿到一批 mark range 后会做同样的事
    (``MergeTreeReadPool`` 的 ``markRanges`` 会走 ``Ranges`` 的归并):
    与其发起 100 次"读 1 个 granule"的随机 IO,不如合并成
    3 次"读 33 个 granule"的顺序 IO —— 顺序 IO 能跑到磁盘带宽,
    随机 IO 受寻道时间限制。
    """
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: (r.begin, r.end))
    out = [ordered[0]]
    for r in ordered[1:]:
        last = out[-1]
        if r.is_empty():
            continue
        if r.begin <= last.end:  # 重叠或相邻
            out[-1] = MarkRange(last.begin, max(last.end, r.end))
        else:
            out.append(r)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 粒度
# ══════════════════════════════════════════════════════════════════════════


class IndexGranularity:
    """一个 part 的 granule 划分。

    支持两种模式,由 ``enable_mixed_granularity_parts`` 切换:

    .. code-block:: text

       ┌── 固定粒度(老式,默认 index_granularity = 8192)──────────────┐
       │  每 8192 行一刀,整齐划一 ——                                     │
       │  索引大小 = (行数 / 8192) × 键宽,非常好估算                     │
       └──────────────────────────────────────────────────────────────┘

       ┌── 自适应粒度(enable_mixed_granularity_parts = 1,默认开)──────┐
       │  切分条件:**行数达到 index_granularity**                      │
       │            **或 字节数达到 index_granularity_bytes(10 MiB)**   │
       │  但绝不切出一个小于 min_index_granularity_bytes(1 KiB)的碎块  │
       └──────────────────────────────────────────────────────────────┘

    **为什么要有自适应粒度?** 因为"宽行"会摧毁固定粒度:

    .. code-block:: text

       场景:一行 1 KB(典型日志),index_granularity = 8192
             → 一个 granule = 8192 × 1 KB = 8 MB   ← 还算合理

       场景:一行 1 MB(典型文档/向量),index_granularity = 8192
             → 一个 granule = 8192 × 1 MB = 8 GB  ← 灾难!

   8 GB 的 granule 意味着:① 做任何过滤都要解压 8 GB;② 索引里
   每个 granule 只记"第一行的键",中间几百万行完全没法定位 ——
   **索引等于失效**。所以从 19.1 起,ClickHouse 用**字节数**而不是
   行数来切 granule:不管一行多大,一个 granule 最多 10 MiB。
    这样"宽行表的索引依然有效",代价是索引条数变多(索引更大)。

    .. warning::

       mini-clickhouse 的 :meth:`compute_granularity` 收的是
       **整块的 (rows, bytes)**,并按**行均字节数**均匀切分。
       真实 ClickHouse 的 ``MergeTreeBlockSizePredictor`` 会随着
       写入**动态估计**每行的字节数(因为实际行宽在波动),
       切出来的边界会略有不同。我们选均匀切分是为了**确定性** ——
       同样的输入必须切出同样的 granule(测试会断言 granule 数)。
    """

    __slots__ = (
        "_granularity",
        "_granularity_bytes",
        "_min_bytes",
        "_is_adaptive",
        "granularities",
        "total_rows",
        # 前缀和缓存:``get_rows_before_mark`` / ``mark_for_row`` 用
        "_prefix",
        "_prefix_len",
    )

    def __init__(
        self,
        index_granularity: int = DEFAULT_INDEX_GRANULARITY,
        index_granularity_bytes: int = DEFAULT_INDEX_GRANULARITY_BYTES,
        *,
        adaptive: bool = True,
        min_index_granularity_bytes: int = MIN_INDEX_GRANULARITY_BYTES,
    ) -> None:
        if index_granularity <= 0:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"index_granularity must be positive, got {index_granularity}",
            )
        self._granularity = index_granularity
        self._granularity_bytes = index_granularity_bytes
        self._min_bytes = min_index_granularity_bytes
        #: ``index_granularity_bytes == 0`` 或显式关闭 → 退回固定粒度
        self._is_adaptive = adaptive and index_granularity_bytes > 0
        #: 每个 granule 的行数(自适应模式下逐块算出来;固定模式下就是常数)
        self.granularities: List[int] = []
        self.total_rows = 0
        # 前缀和缓存 —— 第一次查询时惰性建立
        self._prefix: Optional[List[int]] = None
        self._prefix_len = -1

    # ── 属性 ────────────────────────────────────────────────────────────

    @property
    def index_granularity(self) -> int:
        return self._granularity

    @property
    def index_granularity_bytes(self) -> int:
        return self._granularity_bytes

    @property
    def min_index_granularity_bytes(self) -> int:
        return self._min_bytes

    def is_adaptive(self) -> bool:
        return self._is_adaptive

    # ── 划分 ────────────────────────────────────────────────────────────

    def compute_granularity(self, rows: int, bytes_: int) -> List[int]:
        """返回**每个 granule 的行数**列表(和为 ``rows``)。

        规则(与真实 ClickHouse 的语义一致):

        .. code-block:: text

           bytes_per_row = bytes / rows
           bytes_limit   = index_granularity_bytes / bytes_per_row
                          ↑ "多少行才会撑到 10 MiB"

           limit = min(index_granularity, bytes_limit)
           若 limit 对应的字节数 < min_index_granularity_bytes:
               limit 提升到 min_index_granularity_bytes 对应的行数
              (即"绝不切出太小的 granule")

           然后按 limit 一刀一刀切,最后一块是余数。
        """
        if rows < 0:
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"rows must be >= 0, got {rows}")
        if rows == 0:
            self.granularities = []
            self.total_rows = 0
            return []

        if not self._is_adaptive:
            limit = self._granularity
        else:
            bytes_per_row = (bytes_ / rows) if rows else 1.0
            if bytes_per_row <= 0:
                bytes_per_row = 1.0
            by_bytes = int(self._granularity_bytes / bytes_per_row)
            limit = min(self._granularity, max(by_bytes, 1))
            # 绝不切出小于 min_index_granularity_bytes 的 granule
            if limit * bytes_per_row < self._min_bytes:
                min_rows = int(self._min_bytes / bytes_per_row) + 1
                limit = min(self._granularity, max(limit, min_rows))
            limit = max(1, limit)

        out: List[int] = []
        remaining = rows
        while remaining > 0:
            take = limit if remaining >= limit else remaining
            out.append(take)
            remaining -= take
        self.granularities = out
        self.total_rows = rows
        self._prefix = None
        self._prefix_len = -1
        return out

    def initialize(self, rows: int, bytes_: int) -> None:
        """同 :meth:`compute_granularity`,但**只在还没初始化时**做。"""
        if self.total_rows == rows and self.granularities:
            return
        self.compute_granularity(rows, bytes_)

    def set_granularities(self, granularities: Sequence[int]) -> None:
        """从别处(读取 part 时的 ``.mrk2``)恢复粒度。"""
        self.granularities = list(granularities)
        self.total_rows = sum(self.granularities)
        self._prefix = None
        self._prefix_len = -1

    # ── 查询 ────────────────────────────────────────────────────────────

    def get_mark_count(self) -> int:
        return len(self.granularities)

    def get_rows_in_granule(self, mark: int) -> int:
        if mark < 0 or mark >= len(self.granularities):
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Granule {mark} out of range (part has {len(self.granularities)} granules)",
            )
        return self.granularities[mark]

    def get_rows_before_mark(self, mark: int) -> int:
        """granule ``mark`` 的**第一行**在 part 里的行号。

        用前缀和缓存:第一次调用是 O(n),之后每个 mark O(1)。
        真实 ClickHouse 不缓存这个(它用 ``IMergeTreeDataPart::getMarkStartingRow``
        线性或二分),但对于 200k 行 / 25 个 granule 的规模,
        前缀和是最省心的选择,而且**绝不会算错**。
        """
        if mark <= 0:
            return 0
        if mark >= len(self.granularities):
            mark = len(self.granularities)
        self._ensure_prefix()
        return self._prefix[mark]

    def mark_for_row(self, row: int) -> int:
        """行号 → 它落在哪个 granule(二分)。

        ``_prefix`` 是**前缀和**(``[0, g0, g0+g1, …]``,比 granule 数多一项),
        ``bisect_right`` 给出的是"插入位置",也就是**下一个** granule 的下标
        —— 所以要 ``- 1`` 才是"这一行所属的 granule"。
        (不 ``- 1`` 的话 ``mark_for_row(0) == 1``,整条映射整体偏移一格。)
        """
        if row < 0:
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"row must be >= 0, got {row}")
        if row >= self.total_rows:
            return len(self.granularities)
        self._ensure_prefix()
        import bisect

        return bisect.bisect_right(self._prefix, row) - 1

    def mark_range_for_rows(self, begin_row: int, end_row: int) -> MarkRange:
        """行区间 ``[begin, end)`` → granule 区间(可能把边界"放大")。

        注意结果**只会变宽不会变窄** —— 因为 granule 是读写的最小单位,
        要覆盖第 5 行的 granule,必然连带读出那一整个 granule。
        """
        return MarkRange(self.mark_for_row(begin_row), self.mark_for_row(end_row - 1) + 1
                         if end_row > begin_row else self.mark_for_row(begin_row))

    def _ensure_prefix(self) -> None:
        if self._prefix is not None and self._prefix_len == len(self.granularities):
            return
        prefix = [0]
        acc = 0
        for g in self.granularities:
            acc += g
            prefix.append(acc)
        self._prefix = prefix
        self._prefix_len = len(self.granularities)

    def __repr__(self) -> str:  # pragma: no cover
        mode = "adaptive" if self._is_adaptive else "fixed"
        return (
            f"IndexGranularity({mode}, index_granularity={self._granularity}, "
            f"bytes={self._granularity_bytes}, granules={len(self.granularities)}, "
            f"rows={self.total_rows})"
        )
