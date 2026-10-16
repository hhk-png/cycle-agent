"""列的基类,以及"定长标量"的两种物化方式。

``IColumn`` 是 mini-clickhouse 里最重要的接口 —— 比 ``DataType`` 还重要。
执行引擎的每一个处理器、每一个函数、每一次聚合,操作的都是 ``IColumn``,
而不是"一行行记录"。**这是 ClickHouse 与行存数据库最根本的分歧点**:

.. code-block:: text

   行存(MySQL/PostgreSQL)          列存(ClickHouse)
   ┌──────────────────────┐         ┌──────────────────────┐
   │ row 0: id=1,name='a' │         │ id:   [1, 2, 3, ...] │  ← 一块 65536 行
   │ row 1: id=2,name='b' │         │ name: ['a','b','c',…]│     (block)
   │ row 2: id=3,name='c' │         └──────────────────────┘
   └──────────────────────┘         一次函数调用处理**一整列**,
   一次函数调用处理**一行**          循环体里没有"行"的概念

由此派生出 ClickHouse 的三个关键性质,后面每一章都会用到:

1. **压缩率高**:同一列的值类型相同、分布相似,压缩算法有规律可利用
   (见第 18 章:Delta / DoubleDelta / Gorilla 都是**列内**编码);
2. **向量化**:``plus(a, b)`` 的实现是 ``for i in range(n)`` 而不是
   "取第 i 行的 a、取第 i 行的 b、相加、放回第 i 行" —— 没有虚函数调用、
   没有分支预测失败(见第 10 章);
3. **按列裁剪**:``SELECT name FROM t`` 只需要读 ``name`` 那一列的文件,
   别的列连磁盘都不用碰(见第 05 章)。

``IColumn`` 的接口刻意做得和真实 ClickHouse 的 ``IColumn`` **同名同义**,
这样读者拿真实源码对照时不会有割裂感。差异写在每个模块的末尾。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any, Iterable, Iterator, List, Optional, Sequence

import numpy as np

from ..errors import ErrorCode, MiniChError, raise_error

__all__ = [
    "IColumn",
    "ColumnVector",
    "ColumnBigInt",
    "ColumnUUID",
    "ColumnNothing",
    "column_from_values",
]


class IColumn(ABC):
    """列接口。

    约定(与真实 ClickHouse 一致):

    * **列是可变的**。``append`` / ``pop_back`` / ``filter``(就地)都会改自己。
      引擎里"一个 block 流过 pipeline"指的就是**同一批列对象**被一个个处理器
      改写,而不是每步复制一份。
    * **``filter`` 是就地操作**。真实 ClickHouse 的 ``IColumn::filter``
      就是把不符合条件的行从列里删掉,返回 ``None``。mini-clickhouse
      保留了就地语义,但**额外**返回 ``self`` 便于链式调用
      (真实 CH 返回 void;这是个无害的便利性偏离)。
    * **``permute`` 是返回新列**。因为它不改变"有哪些值",只改变顺序。
    * **越界读是逻辑错误**。所有 ``get(i)`` 在 ``i >= size()`` 时抛
      ``ARGUMENT_OUT_OF_BOUND`` —— 真实 CH 在 release 构建里是 UB,
      这里给了诊断,符合我们"绝不静默"的总原则。
    """

    # ── 规模 ────────────────────────────────────────────────────────────

    @abstractmethod
    def size(self) -> int:
        """行数。"""

    def empty(self) -> bool:
        return self.size() == 0

    # ── 读 ──────────────────────────────────────────────────────────────

    @abstractmethod
    def get(self, n: int) -> Any:
        """取第 ``n`` 行的值(Python 对象)。"""

    def get_many(self, indexes: Sequence[int]) -> "IColumn":
        """按索引列表取一个**新列** —— 真实 CH 的 ``IColumn::getMany``。

        这是 ``ORDER BY`` / ``JOIN`` / ``LIMIT BY`` 的公共底座:
        "算出目标顺序的索引数组,一次性重排"。
        """
        out = self.clone_empty()
        for i in indexes:
            out.append(self.get(i))
        return out

    def iter_values(self) -> Iterator[Any]:
        """逐个产出值(方便测试与调试;热路径请用索引循环)。

        .. note::

           这个方法叫 ``iter_values`` 而不是 ``values``,是因为
           :class:`~minich.columns.composite.ColumnMap` 有一个
           **数据属性**叫 ``values``(Map 的值列)。真实 ClickHouse 里
           这两个名字不会冲突(``IColumn`` 没有 ``values()`` 这个方法),
           但在 Python 里属性会遮蔽方法。

           所以我们把"通用迭代器"命名为 ``iter_values()``,
           而 ``values()`` 保留成它的别名 —— 一个 Map 列上调用
           ``.values()`` 拿到的会是**值列**(符合直觉的那个)。
           这条差异写在 ``ColumnMap`` 的类文档里。
        """
        for i in range(self.size()):
            yield self.get(i)

    def values(self) -> Iterator[Any]:
        """``iter_values()`` 的别名(可能被复合列的数据属性遮蔽)。"""
        return self.iter_values()

    def to_list(self) -> List[Any]:
        return list(self.iter_values())

    # ── 写 ──────────────────────────────────────────────────────────────

    @abstractmethod
    def append(self, value: Any) -> None:
        """追加一个值。"""

    def append_default(self) -> None:
        """追加一个默认值(零值)。"""
        self.append(self.default_value())

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def append_many_default(self, n: int) -> None:
        for _ in range(n):
            self.append_default()

    def insert_from(self, other: "IColumn", n: int) -> None:
        """从 ``other`` 的第 ``n`` 行取一个值追加到自己末尾。"""
        self.append(other.get(n))

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        """批量取 ``other[start:start+length]`` 追加到自己末尾。"""
        if start < 0 or length < 0 or start + length > other.size():
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"insert_range_from out of bounds: start={start}, length={length}, "
                f"size={other.size()}",
            )
        for i in range(start, start + length):
            self.append(other.get(i))

    def insert_many_from(self, other: "IColumn", position: int, length: int) -> None:
        """把 ``other`` 的第 ``position`` 个值**重复** ``length`` 次追加进来。

        真实 CH 用它做 ``arrayJoin`` 的反向(把展开后的选择再聚回去)。
        """
        v = other.get(position)
        for _ in range(length):
            self.append(v)

    def pop_back(self, n: int = 1) -> None:
        """从尾部删掉 ``n`` 行(就地)。"""
        if n < 0 or n > self.size():
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"pop_back({n}) on a column of size {self.size()}",
            )
        self.cut(self.size() - n, n)

    def resize(self, n: int) -> None:
        """把列调整到恰好 ``n`` 行:多了截断,少了补默认值。"""
        cur = self.size()
        if n < cur:
            self.pop_back(cur - n)
        elif n > cur:
            self.append_many_default(n - cur)

    # ── 复制 ────────────────────────────────────────────────────────────

    @abstractmethod
    def clone_empty(self) -> "IColumn":
        """新建一个同类型的**空**列。"""

    def clone(self) -> "IColumn":
        out = self.clone_empty()
        out.insert_range_from(self, 0, self.size())
        return out

    def clone_resized(self, n: int) -> "IColumn":
        """复制一份再调整到 ``n`` 行(真实 CH 的 ``cloneResized``)。

        ``n < size`` 保留**前 n 行**,``n > size`` 用默认值补足 —— 关键是
        **先复制再 resize**,而不是从空列 resize(那会把已有数据全丢成默认值)。
        """
        out = self.clone()
        out.resize(n)
        return out

    # ── 变换 ────────────────────────────────────────────────────────────

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        """**就地**保留 ``mask[i]`` 为真的行。

        ``mask`` 的长度必须等于 ``size()`` —— 真实 CH 会 assert,
        我们抛 ``SIZES_OF_COLUMNS_DOESNT_MATCH``。长度不符意味着
        "两个 block 没对齐",这是引擎里最严重的一类 bug,
        绝不能被静默吞掉(第 10 章有一个真实案例)。
        """
        if len(mask) != self.size():
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != column size {self.size()}",
            )
        new = self.clone_empty()
        for i, keep in enumerate(mask):
            if keep:
                new.append(self.get(i))
        self.__dict__.update(new.__dict__)
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        """返回按 ``indexes`` 重排后的**新列**(不改自己)。"""
        return self.get_many(indexes)

    def replicate(self, offsets: Sequence[int]) -> "IColumn":
        """``arrayJoin`` 的逆操作。

        ``offsets`` 是 **ColumnArray 的累积偏移**:第 ``i`` 个数组占据
        ``[offsets[i-1], offsets[i])``。结果列的第 ``j`` 个值 = 原列中
        "第 j 个元素所属的那个数组" 的值。
        真实 CH 的签名是 ``replicate(const Offsets & offsets)``。
        """
        out = self.clone_empty()
        prev = 0
        for i, off in enumerate(offsets):
            for _ in range(off - prev):
                out.append(self.get(i))
            prev = off
        return out

    def cut(self, start: int, length: int) -> None:
        """就地删掉 ``[start, start+length)``。默认实现用重建。"""
        if start < 0 or length < 0 or start + length > self.size():
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"cut(start={start}, length={length}) on column of size {self.size()}",
            )
        kept = [self.get(i) for i in range(start)] + [
            self.get(i) for i in range(start + length, self.size())
        ]
        new = self.clone_empty()
        new.append_many(kept)
        self.__dict__.update(new.__dict__)

    # ── 比较与哈希 ──────────────────────────────────────────────────────

    @abstractmethod
    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        """比较 ``self[n]`` 与 ``other[m]``,返回 -1 / 0 / 1。

        NULL 的处理(真实 CH 的语义,必须记住):**NULL 被认为是最大的值**,
        且**两个 NULL 相等**。所以 ``ORDER BY x ASC`` 默认把 NULL 排在最后,
        ``ORDER BY x DESC`` 默认把 NULL 排在最前 —— 想要别的顺序要写
        ``NULLS FIRST/LAST``。第 14 章会把这条规则单独拎出来讲。
        """

    def compare_at_with_collation(self, n: int, other: "IColumn", m: int, collator: Any) -> int:
        """带排序规则的比较 —— 默认退化成 ``compare_at``。

        ``ColumnString`` 会覆盖它来实现 ``COLLATE 'tr'`` 之类的语言相关排序。
        """
        if collator is not None and hasattr(collator, "compare"):
            return collator.compare(self.get(n), other.get(m))
        return self.compare_at(n, other, m)

    def hash_value(self, n: int) -> Any:
        """返回一个可哈希的键(用于 GROUP BY / DISTINCT / JOIN 的哈希表)。

        .. note::

           真实 ClickHouse 不做这件事 —— 它把 key 列**按行序列化成一段
           连续字节**(``serializeKeys``),再用这段字节做哈希与比较。
           好处是"一次 memcmp 比完所有 key 列"、缓存友好。

           mini-clickhouse 返回 Python 元组:语义完全相同
           (等价的序列化 ⇒ 相等的元组),但在 Python 里元组哈希是 C 层的,
           比手工拼字节串快得多。第 12 章会对比这两种做法。
        """
        return self.get(n)

    # ── 统计 ────────────────────────────────────────────────────────────

    def byte_size(self) -> int:
        """**内存中**的近似字节数。

        .. warning::

           这个值**不等于** ``system.parts`` 里的 ``bytes_on_disk``。
           磁盘大小取决于压缩编码,列在内存里的表示与落盘表示是两回事
           (真实 CH 也是两套:``IColumn::byteSize()`` vs
           ``IMergeTreeDataPart::getBytesOnDisk()``)。第 18 章会用数据说明
           这两者能差多少倍。
        """
        return self.size() * 16

    def is_const(self) -> bool:
        """是否是常量列(所有行的值相同)。"""
        return False

    def convert_to_full_column(self) -> "IColumn":
        """把常量列展开成完整列;普通列返回自己。"""
        return self

    def is_sparse(self) -> bool:
        return False

    def default_value(self) -> Any:
        return 0

    def contains_default(self) -> bool:
        """列里是否有任何值是默认值(用于跳过"全零列"的写入优化)。"""
        dv = self.default_value()
        return any(self.get(i) == dv for i in range(self.size()))

    def has_enough_capacity(self, other: "IColumn") -> bool:
        return True

    # ── 调试 ────────────────────────────────────────────────────────────

    def dump_structure(self, indent: int = 0) -> str:  # pragma: no cover
        pad = "  " * indent
        return f"{pad}{type(self).__name__}(size={self.size()})"

    def __len__(self) -> int:
        return self.size()

    def __repr__(self) -> str:  # pragma: no cover
        head = self.to_list()[:6]
        more = ", …" if self.size() > 6 else ""
        return f"{type(self).__name__}[{self.size()}]({head}{more})"


# ══════════════════════════════════════════════════════════════════════════
# ColumnVector —— 定长标量的默认物化
# ══════════════════════════════════════════════════════════════════════════


class ColumnVector(IColumn):
    """一段**连续**的定长值 —— 用 NumPy 定长数组承载。

    真实 ClickHouse 里对应 ``ColumnVector<T>``,底层是一个
    ``PaddedPODArray<T>``:带**容量倍增**的连续内存块,并且尾部做了
    padding(便于 SIMD 越界读而不用处理尾巴)。

    mini-clickhouse 用 ``np.ndarray`` + 自己管容量,把这两件事都保留了下来:

    .. code-block:: text

      append 时:
        _size < capacity        → 直接写,不动内存
        _size == capacity       → 容量翻倍(capacity = max(2*cap, MIN_CAPACITY))
                                  ↑ 这就是"均摊 O(1)"的来源

      ┌──────────── np.ndarray(容量) ────────────┐
      │ [已用 .................][ 未用 / 垃圾   ]│
      └──────────────────────────────────────────┘
                              ▲
                              _size

    为什么不用 Python ``list``? 因为 list 存的是**装箱后的对象指针**:
    一个 100 万行的 ``UInt64`` 在 list 里是 100 万个 PyObject(每个 28 字节)
    + 100 万个指针,而在 ndarray 里是紧凑的 8 MB。这个差异正是列存的立足点,
    所以教学实现也应当把它做出来 —— 否则"内存压缩率"的讨论就落空了。

    .. note::

       **整数溢出的语义**。真实 ClickHouse 的 ``UInt8`` 算 ``250 + 10``
       会**回绕**成 ``4``(C++ 无符号溢出是定义良好的),而 ``Int8`` 溢出
       是 UB —— 但实际实现里也是回绕。mini-clickhouse 在 ``normalize``
       阶段做同样的回绕,因此两边结果一致。第 03 章有专门一节讲这个,
       因为它是"从 MySQL 迁过来的人最容易被坑的一条"。
    """

    #: 首次分配容量 —— 真实 CH 的 PaddedPODArray 从一个很小的值起步
    MIN_CAPACITY = 32

    def __init__(self, dtype: Any = np.uint64, data: Optional[Sequence[Any]] = None) -> None:
        self.dtype = np.dtype(dtype)
        if data is None:
            self._data = np.zeros(self.MIN_CAPACITY, dtype=self.dtype)
            self._size = 0
        else:
            arr = np.asarray(list(data), dtype=self.dtype)
            self._data = arr
            self._size = arr.shape[0]

    # ── 容量管理 ────────────────────────────────────────────────────────

    def reserve(self, n: int) -> None:
        """确保容量至少 ``n``。"""
        if n <= self._data.shape[0]:
            return
        new_cap = max(n, self._data.shape[0] * 2, self.MIN_CAPACITY)
        grown = np.zeros(new_cap, dtype=self.dtype)
        grown[: self._size] = self._data[: self._size]
        self._data = grown

    def capacity(self) -> int:
        return int(self._data.shape[0])

    def shrink_to_fit(self) -> None:
        self._data = self._data[: self._size].copy()

    # ── 规模 ────────────────────────────────────────────────────────────

    def size(self) -> int:
        return self._size

    # ── 读 ──────────────────────────────────────────────────────────────

    def get(self, n: int) -> Any:
        if n < 0 or n >= self._size:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Index {n} out of bounds in {type(self).__name__} of size {self._size}",
            )
        return self._data[n].item()

    def get_raw(self, n: int) -> Any:
        """不做边界检查的读 —— 热路径用(引擎内部保证索引合法)。"""
        return self._data[n].item()

    def numpy_view(self) -> np.ndarray:
        """返回底层数组的**前 ``size()`` 行**视图(只读语义:调用方不要改)。

        这是 mini-clickhouse 里"向量化"真正发生的地方:算术函数直接
        ``arr1 + arr2``,整个计算发生在 NumPy 的 C/SIMD 层。
        """
        return self._data[: self._size]

    def get_many(self, indexes: Sequence[int]) -> "IColumn":
        idx = np.asarray(indexes, dtype=np.intp)
        return ColumnVector(self.dtype, self._data[: self._size][idx])

    # ── 写 ──────────────────────────────────────────────────────────────

    def append(self, value: Any) -> None:
        if self._size == self._data.shape[0]:
            self.reserve(self._size + 1)
        self._data[self._size] = value
        self._size += 1

    def append_many(self, values: Iterable[Any]) -> None:
        seq = values if isinstance(values, (list, tuple, np.ndarray)) else list(values)
        n = len(seq)
        if n == 0:
            return
        self.reserve(self._size + n)
        self._data[self._size : self._size + n] = np.asarray(seq, dtype=self.dtype)
        self._size += n

    def append_many_default(self, n: int) -> None:
        if n <= 0:
            return
        self.reserve(self._size + n)
        self._data[self._size : self._size + n] = 0
        self._size += n

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        if isinstance(other, ColumnVector) and other.dtype == self.dtype:
            if start < 0 or length < 0 or start + length > other.size():
                raise_error(
                    ErrorCode.ARGUMENT_OUT_OF_BOUND,
                    f"insert_range_from({start}, {length}) on column of size {other.size()}",
                )
            self.reserve(self._size + length)
            self._data[self._size : self._size + length] = other._data[start : start + length]
            self._size += length
            return
        super().insert_range_from(other, start, length)

    def insert_many_from(self, other: "IColumn", position: int, length: int) -> None:
        if length <= 0:
            return
        self.reserve(self._size + length)
        self._data[self._size : self._size + length] = other.get(position)
        self._size += length

    def cut(self, start: int, length: int) -> None:
        if start < 0 or length < 0 or start + length > self._size:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"cut({start}, {length}) on column of size {self._size}",
            )
        tail_start = start + length
        tail_len = self._size - tail_start
        if tail_len > 0:
            self._data[start : start + tail_len] = self._data[tail_start : self._size]
        self._size -= length

    def resize(self, n: int) -> None:
        if n < 0:
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"resize({n})")
        if n <= self._data.shape[0]:
            if n > self._size:
                self._data[self._size : n] = 0
            self._size = n
            return
        self.reserve(n)
        self._data[self._size : n] = 0
        self._size = n

    # ── 变换 ────────────────────────────────────────────────────────────

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        if len(mask) != self._size:
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != column size {self._size}",
            )
        m = np.asarray(mask, dtype=bool)
        new = self._data[: self._size][m]
        self._data = new
        self._size = int(new.shape[0])
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        return self.get_many(indexes)

    # ── 比较 ────────────────────────────────────────────────────────────

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        a, b = self.get(n), other.get(m)
        if a < b:
            return -1
        return 1 if a > b else 0

    def hash_value(self, n: int) -> Any:
        return self.get(n)

    # ── 统计与默认值 ────────────────────────────────────────────────────

    def byte_size(self) -> int:
        return self._size * self.dtype.itemsize

    def default_value(self) -> Any:
        return 0

    def clone_empty(self) -> "IColumn":
        return ColumnVector(self.dtype)

    def is_float_column(self) -> bool:
        return self.dtype.kind == "f"

    def dump_structure(self, indent: int = 0) -> str:  # pragma: no cover
        pad = "  " * indent
        return (
            f"{pad}ColumnVector(dtype={self.dtype}, size={self._size}, "
            f"capacity={self.capacity()})"
        )


# ══════════════════════════════════════════════════════════════════════════
# 超宽整数 / Decimal128+ —— NumPy 装不下的那些
# ══════════════════════════════════════════════════════════════════════════


class ColumnBigInt(IColumn):
    """``Int128/UInt128/Int256/UInt256`` 与 ``Decimal128/Decimal256`` 的列。

    NumPy 没有 128/256 位整型,真实 ClickHouse 用的是 GCC 的
    ``__int128`` / 自定义的 ``wide::integer<256>``。mini-clickhouse
    退回 Python 的**任意精度整数** —— 语义完全正确(而且不会溢出!),
    只是内存占用远大于 16/32 字节。

    这类列在我们的测试里只用于验证类型系统与序列化,不参与性能讨论。

    .. note::

       ``Decimal128`` 在这里存的是**未缩放**的整数(即 ``1.23`` 存成 ``123``)。
       所有 Decimal 都一样 —— 小数点位置由 ``DataType`` 记住,
       列只存整数。这就是"Decimal 是定点数、不是浮点数"的落地方式。
    """

    def __init__(self, signed: bool = True, bits: int = 128,
                 data: Optional[Sequence[int]] = None) -> None:
        self.signed = signed
        self.bits = bits
        self._data: List[int] = list(data) if data is not None else []

    def size(self) -> int:
        return len(self._data)

    def get(self, n: int) -> Any:
        if n < 0 or n >= len(self._data):
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"Index {n} out of bounds")
        return self._data[n]

    def append(self, value: Any) -> None:
        self._data.append(int(value))

    def append_many(self, values: Iterable[Any]) -> None:
        self._data.extend(int(v) for v in values)

    def clone_empty(self) -> "IColumn":
        return ColumnBigInt(self.signed, self.bits)

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        self._data.extend(other.get(i) for i in range(start, start + length))

    def cut(self, start: int, length: int) -> None:
        del self._data[start : start + length]

    def pop_back(self, n: int = 1) -> None:
        del self._data[len(self._data) - n:]

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        if len(mask) != len(self._data):
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != column size {len(self._data)}",
            )
        self._data = [v for v, keep in zip(self._data, mask) if keep]
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        return ColumnBigInt(self.signed, self.bits, [self._data[i] for i in indexes])

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        a, b = self.get(n), other.get(m)
        if a < b:
            return -1
        return 1 if a > b else 0

    def byte_size(self) -> int:
        return len(self._data) * (self.bits // 8)

    def clone(self) -> "IColumn":
        return ColumnBigInt(self.signed, self.bits, self._data)


class ColumnUUID(IColumn):
    """``UUID`` 列 —— 内部存 128 位整数。

    真实 ClickHouse 的 UUID 列就是 ``ColumnVector<UInt128>``。
    我们因为 NumPy 没有 uint128 而复用 :class:`ColumnBigInt`,
    但**比较语义**要特别处理:UUID 是**无符号**的 128 位量,
    格式化时按 8-4-4-4-12 分组。
    """

    def __init__(self, data: Optional[Sequence[int]] = None) -> None:
        self._data: List[int] = [int(v) & ((1 << 128) - 1) for v in (data or [])]

    def size(self) -> int:
        return len(self._data)

    def get(self, n: int) -> Any:
        return self._data[n]

    def append(self, value: Any) -> None:
        self._data.append(int(value) & ((1 << 128) - 1))

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def clone_empty(self) -> "IColumn":
        return ColumnUUID()

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        self._data.extend(int(other.get(i)) & ((1 << 128) - 1)
                          for i in range(start, start + length))

    def cut(self, start: int, length: int) -> None:
        del self._data[start : start + length]

    def pop_back(self, n: int = 1) -> None:
        del self._data[len(self._data) - n:]

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        if len(mask) != len(self._data):
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != column size {len(self._data)}",
            )
        self._data = [v for v, keep in zip(self._data, mask) if keep]
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        return ColumnUUID([self._data[i] for i in indexes])

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        a, b = self.get(n), other.get(m)
        if a < b:
            return -1
        return 1 if a > b else 0

    def byte_size(self) -> int:
        return len(self._data) * 16


class ColumnNothing(IColumn):
    """``Nothing`` 类型的列 —— 只能存 NULL,且**永远为空**。

    ``Nothing`` 出现在这些地方:

    * ``SELECT NULL``—— 返回 ``Nullable(Nothing)``,一个有零个值的列;
    * ``arrayJoin([])``—— 空数组的展开;
    * ``if(0, 'a', NULL)`` —— 分支推导出的公共类型。

    真实 ClickHouse 的 ``ColumnNothing`` 有一条"size 恒为 0,
    任何 append 都抛异常"的硬约束。这条约束很有用:它把
    "空结果集"和"有一列 NULL"这两件事区分开了(大多数函数遇到
    ``Nothing`` 时应当返回空列,而不是报错)。
    """

    def size(self) -> int:
        return 0

    def get(self, n: int) -> Any:
        raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, "ColumnNothing is always empty")

    def append(self, value: Any) -> None:
        """**写入一个真实值**是非法的 —— ``Nothing`` 装不下任何东西。"""
        raise_error(
            ErrorCode.LOGICAL_ERROR,
            "Cannot append to ColumnNothing: the Nothing type has no values",
        )

    def append_default(self) -> None:
        """但**写默认值是允许的,而且是必需的** —— 它是个空操作。

        为什么? 因为 ``SELECT NULL`` 的列类型是 ``Nullable(Nothing)``,
        而 ``ColumnNullable.append(None)`` 会往嵌套列里写一个默认值。
        如果那一步抛错,``SELECT NULL``、``if(x, NULL, y)``、
        ``coalesce`` 这些最常见的写法就全都用不了。

        语义上也说得通:``Nothing`` **永远是 0 行**,"往一个不存在的
        序列末尾追加一个不存在的值"就是什么都不做。真实 ClickHouse 的
        ``ColumnNothing`` 同样是"size 恒为 0"的特殊列。

        .. note::

           这样做的副作用是:``Nullable(Nothing)`` 列的 ``null_map``
           有 N 项而嵌套列有 0 项 —— 长度不一致。真实 ClickHouse 里
           这也是一个被特意保留的**特例**(所以它的 ``ColumnNullable``
           在 ``Nothing`` 上有专门的判断)。所有会遍历嵌套列的操作
           (``filter``/``permute``/``insert_range_from``)都必须
           对这个情况免疫,这也是下面几个方法被改成空操作的原因。
        """
        return

    def append_many(self, values: Iterable[Any]) -> None:
        for _ in values:
            self.append(None)

    def append_many_default(self, n: int) -> None:
        return

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        return

    def insert_many_from(self, other: "IColumn", position: int, length: int) -> None:
        return

    def insert_from(self, other: "IColumn", n: int) -> None:
        return

    def resize(self, n: int) -> None:
        return

    def clone_empty(self) -> "IColumn":
        return ColumnNothing()

    def clone(self) -> "IColumn":
        return ColumnNothing()

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        return ColumnNothing()

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        return 0

    def byte_size(self) -> int:
        return 0

    def is_nothing(self) -> bool:  # type: ignore[override]
        return True


# ══════════════════════════════════════════════════════════════════════════
# 便捷构造
# ══════════════════════════════════════════════════════════════════════════


def column_from_values(values: Sequence[Any], dtype: Any = None) -> ColumnVector:
    """从 Python 值列表快速造一个 :class:`ColumnVector`。

    ``dtype`` 省略时按值猜(整数 → int64,浮点 → float64,
    布尔 → uint8)。**猜出来的类型不一定是你想要的** —— 与真实
    ClickHouse 一样,``1`` 默认是 ``UInt8`` 不是 ``Int64``,
    所以生产代码永远应当显式给类型。这个函数只服务于测试与示例。
    """
    if dtype is not None:
        return ColumnVector(dtype, values)
    if not values:
        return ColumnVector(np.uint64, [])
    sample = next((v for v in values if v is not None), 0)
    if isinstance(sample, bool):
        dt = np.uint8
    elif isinstance(sample, int):
        dt = np.int64 if any(v is not None and v < 0 for v in values) else np.uint64
    elif isinstance(sample, float):
        dt = np.float64
    else:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"column_from_values cannot infer a numeric dtype from {type(sample).__name__}",
        )
    return ColumnVector(dt, values)
