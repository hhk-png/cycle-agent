"""复合列:NULL、数组、元组、映射、低基数、常量、稀疏、聚合状态。

前面两个模块里的列都是"一行一个标量"。这个模块里的列,**一行也可以是
一个容器**。ClickHouse 处理容器的思路非常统一 —— **嵌套列 + 位置数组**:

.. code-block:: text

   Array(UInt8) 的列,值 = [[1,2],[3],[],[4,5,6]]
   ┌────────────────────────────────────────────────────────────┐
   │ offsets: [2,        3,      3,        6      ]             │
   │                     │       │         └ 累积到 6 个元素     │
   │                     │       └ 第 2 个数组是空的!           │
   │                     └ 第 1 个数组结束于 3                 │
   ├────────────────────────────────────────────────────────────┤
   │ nested:  [1, 2, 3, 4, 5, 6]   ← 一个平铺的 UInt8 列        │
   └────────────────────────────────────────────────────────────┘

于是"遍历一个数组列的所有元素"就是"遍历 nested 列",而 nested 列是一个
**普通列** —— 所有为标量列写的函数、压缩编码、聚合都能直接复用。
``Map`` / ``Tuple`` / ``Nested`` 全是这个套路的变体。

这个设计带来的一个直接后果,是 ClickHouse 的一个著名现象:
**数组列的内存占用取决于元素总数,不取决于数组个数**。10 亿行、
每行 1 个元素的 ``Array(String)`` 和一个 10 亿行的 ``String`` 在存储上
几乎没区别。第 16 章讨论 ``arrayJoin`` 的性能时会用到这一点。

.. warning::

   ``ColumnNullable`` 的 NULL 表示法是**位置图 + 默认值**:

   .. code-block:: text

     null_map: [0, 1, 0, 1]        ← 1 表示这一行是 NULL
     nested:   [1, 0, 3, 0]        ← NULL 的位置上塞的是**零值**(不是垃圾)

   为什么 NULL 位置上要塞零值而不是留空? 因为 nested 列必须是**稠密的**
   —— 只有稠密列才能享受定长、向量化、memcmp 比较。代价是 NULL 行的
   零值会占内存(一个 ``Nullable(String)`` 里如果有大量 NULL,
   每个 NULL 仍然要占 offsets 的 8 字节)。第 03 章会讲
   ``Nullable`` 的"隐藏成本",以及为什么 ClickHouse 官方建议
   **能不用 Nullable 就不用**。
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

from ..errors import ErrorCode, raise_error
from .base import IColumn

__all__ = [
    "ColumnNullable",
    "ColumnArray",
    "ColumnTuple",
    "ColumnMap",
    "ColumnLowCardinality",
    "ColumnConst",
    "ColumnSparse",
    "ColumnAggregateFunction",
]


class ColumnNullable(IColumn):
    """可空列 —— ``null_map`` + ``nested``。"""

    def __init__(self, nested: Optional[IColumn] = None) -> None:
        self.nested: IColumn = nested if nested is not None else _placeholder()
        #: ``True`` 表示该行是 NULL。真实 CH 用 UInt8(0/1),不是 bool 数组。
        self.null_map: List[bool] = []

    # ── 规模 ────────────────────────────────────────────────────────────

    def size(self) -> int:
        return len(self.null_map)

    def is_null_at(self, n: int) -> bool:
        return self.null_map[n]

    # ── 读 ──────────────────────────────────────────────────────────────

    def get(self, n: int) -> Any:
        if n < 0 or n >= len(self.null_map):
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Index {n} out of bounds in ColumnNullable of size {self.size()}",
            )
        return None if self.null_map[n] else self.nested.get(n)

    def hash_value(self, n: int) -> Any:
        # NULL 在分组时是一个**独立的分组**。真实 CH 的序列化键里,
        # NULL 写的是一个特殊的哨兵字节(0),非 NULL 写 1 再跟值 ——
        # 我们用元组表达同样的区分。
        if self.null_map[n]:
            return (0, None)
        return (1, self.nested.hash_value(n))

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        a_null = self.null_map[n] if isinstance(other, ColumnNullable) else False
        b_null = other.null_map[m] if isinstance(other, ColumnNullable) else False
        if a_null or b_null:
            # 真实 CH 的规则:两个 NULL 相等,NULL 比任何非 NULL 都大。
            if a_null and b_null:
                return 0
            return 1 if a_null else -1
        if isinstance(other, ColumnNullable):
            return self.nested.compare_at(n, other.nested, m)
        return self.nested.compare_at(n, other, m)

    # ── 写 ──────────────────────────────────────────────────────────────

    def append(self, value: Any) -> None:
        if value is None:
            self.null_map.append(True)
            self.nested.append_default()
        else:
            self.null_map.append(False)
            self.nested.append(value)

    def append_nested(self, value: Any, is_null: bool = False) -> None:
        """绕过 NULL 判定直接追加(nested 列已经准备好值的时候用)。"""
        self.null_map.append(is_null)
        self.nested.append(value)

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def append_many_default(self, n: int) -> None:
        # Nullable 的**默认值是 NULL**,不是 nested 的零值。
        # 这就是 `ALTER TABLE ... ADD COLUMN c Nullable(UInt8)` 之后
        # 老数据读出来是 NULL 而不是 0 的原因。
        self.null_map.extend([True] * n)
        self.nested.append_many_default(n)

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        if isinstance(other, ColumnNullable):
            self.null_map.extend(other.null_map[start : start + length])
            self.nested.insert_range_from(other.nested, start, length)
            return
        for i in range(start, start + length):
            self.append(other.get(i))

    def insert_many_from(self, other: "IColumn", position: int, length: int) -> None:
        if isinstance(other, ColumnNullable):
            is_null = other.null_map[position]
            self.null_map.extend([is_null] * length)
            self.nested.insert_many_from(other.nested, position, length)
            return
        super().insert_many_from(other, position, length)

    def cut(self, start: int, length: int) -> None:
        del self.null_map[start : start + length]
        self.nested.cut(start, length)

    def pop_back(self, n: int = 1) -> None:
        if n > self.size():
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"pop_back({n}) too large")
        del self.null_map[len(self.null_map) - n :]
        self.nested.pop_back(n)

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        if len(mask) != self.size():
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != column size {self.size()}",
            )
        self.nested.filter(mask)
        self.null_map = [v for v, keep in zip(self.null_map, mask) if keep]
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        out = ColumnNullable(self.nested.clone_empty())
        out.nested.insert_range_from(self.nested, 0, 0)  # 保持空
        out.nested = self.nested.permute(indexes)
        out.null_map = [self.null_map[i] for i in indexes]
        return out

    def get_many(self, indexes: Sequence[int]) -> "IColumn":
        return self.permute(indexes)

    def replicate(self, offsets: Sequence[int]) -> "IColumn":
        out = ColumnNullable(self.nested.clone_empty())
        out.nested = self.nested.replicate(offsets)
        prev = 0
        for i, off in enumerate(offsets):
            out.null_map.extend([self.null_map[i]] * (off - prev))
            prev = off
        return out

    # ── 其它 ────────────────────────────────────────────────────────────

    def clone_empty(self) -> "IColumn":
        return ColumnNullable(self.nested.clone_empty())

    def default_value(self) -> Any:
        return None

    def byte_size(self) -> int:
        return self.nested.byte_size() + len(self.null_map)

    def count_nulls(self) -> int:
        return sum(1 for v in self.null_map if v)

    def convert_to_full_column(self) -> "IColumn":
        return self

    def dump_structure(self, indent: int = 0) -> str:  # pragma: no cover
        pad = "  " * indent
        return (
            f"{pad}ColumnNullable(size={self.size()}, "
            f"nulls={self.count_nulls()})\n"
            + self.nested.dump_structure(indent + 1)
        )


class ColumnArray(IColumn):
    """数组列 —— ``offsets``(累积)+ ``nested``(平铺)。"""

    def __init__(self, nested: Optional[IColumn] = None) -> None:
        self.nested: IColumn = nested if nested is not None else _placeholder()
        #: 累积**元素**偏移。``offsets[i]`` = 前 i+1 个数组的元素总数。
        self.offsets: List[int] = []

    # ── 规模 ────────────────────────────────────────────────────────────

    def size(self) -> int:
        return len(self.offsets)

    def elements(self) -> int:
        """元素总数(不是行数)—— 数组列的内存占用看这个数。"""
        return self.offsets[-1] if self.offsets else 0

    # ── 读 ──────────────────────────────────────────────────────────────

    def get(self, n: int) -> Any:
        if n < 0 or n >= len(self.offsets):
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Index {n} out of bounds in ColumnArray of size {self.size()}",
            )
        start = self.offsets[n - 1] if n > 0 else 0
        end = self.offsets[n]
        return [self.nested.get(i) for i in range(start, end)]

    def get_element(self, n: int, element: int) -> Any:
        """取第 n 个数组的第 element 个元素(越界返回 None)。"""
        start = self.offsets[n - 1] if n > 0 else 0
        end = self.offsets[n]
        if element < 0 or start + element >= end:
            return None
        return self.nested.get(start + element)

    def hash_value(self, n: int) -> Any:
        return tuple(self.get(n))

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        """数组的字典序比较 —— 逐元素比,短的先结束则更小。"""
        a, b = self.get(n), other.get(m)
        for x, y in zip(a, b):
            if x == y:
                continue
            # 复用数据类型语义:元素级比较交给 Python 的 < (对数值/字符串
            # 都成立),NULL 的情况由 nested 是 ColumnNullable 时接管。
            return -1 if x < y else 1
        if len(a) == len(b):
            return 0
        return -1 if len(a) < len(b) else 1

    # ── 写 ──────────────────────────────────────────────────────────────

    def append(self, value: Any) -> None:
        if value is None:
            raise_error(
                ErrorCode.CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN,
                "Cannot insert NULL into a non-Nullable Array column; "
                "use Nullable(Array(...)) instead",
            )
        if not isinstance(value, (list, tuple)):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Cannot insert {type(value).__name__} into Array; expected list",
            )
        for v in value:
            self.nested.append(v)
        self.offsets.append(self.nested.size())

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def append_many_default(self, n: int) -> None:
        # 空数组的默认值是 **[]**,不是 [0]。这一条经常被误会:
        # `Array(UInt8)` 的零值不是 `[0]` 而是 `[]`。
        last = self.offsets[-1] if self.offsets else 0
        self.offsets.extend([last] * n)

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        if isinstance(other, ColumnArray):
            for i in range(start, start + length):
                s = other.offsets[i - 1] if i > 0 else 0
                e = other.offsets[i]
                self.nested.insert_range_from(other.nested, s, e - s)
                self.offsets.append(self.nested.size())
            return
        super().insert_range_from(other, start, length)

    def cut(self, start: int, length: int) -> None:
        if start < 0 or length < 0 or start + length > self.size():
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, "cut out of bounds")
        elem_start = self.offsets[start - 1] if start > 0 else 0
        elem_end = self.offsets[start + length - 1] if length > 0 else elem_start
        del self.offsets[start : start + length]
        self.nested.cut(elem_start, elem_end - elem_start)
        # 被删掉的元素之后的偏移要整体前移
        removed = elem_end - elem_start
        for i in range(start, len(self.offsets)):
            self.offsets[i] -= removed

    def pop_back(self, n: int = 1) -> None:
        if n > self.size():
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"pop_back({n}) too large")
        self.cut(self.size() - n, n)

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        if len(mask) != self.size():
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != column size {self.size()}",
            )
        # 元素掩码:一个元素属于第 i 个数组,当且仅当第 i 个数组被保留
        elem_mask: List[bool] = []
        for i, keep in enumerate(mask):
            s = self.offsets[i - 1] if i > 0 else 0
            elem_mask.extend([keep] * (self.offsets[i] - s))
        self.nested.filter(elem_mask)
        new_offsets: List[int] = []
        acc = 0
        for i, keep in enumerate(mask):
            if not keep:
                continue
            s = self.offsets[i - 1] if i > 0 else 0
            acc += self.offsets[i] - s
            new_offsets.append(acc)
        self.offsets = new_offsets
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        out = ColumnArray(self.nested.clone_empty())
        for i in indexes:
            s = self.offsets[i - 1] if i > 0 else 0
            e = self.offsets[i]
            out.nested.insert_range_from(self.nested, s, e - s)
            out.offsets.append(out.nested.size())
        return out

    def get_many(self, indexes: Sequence[int]) -> "IColumn":
        return self.permute(indexes)

    def replicate(self, offsets: Sequence[int]) -> "IColumn":
        """真实 CH 里 ``ColumnArray::replicate`` 用于把展开后的行聚回数组。

        这里实现的是**成对使用**的那一半:``offsets`` 是**另一个**数组列
        的累积偏移,表示"self 的第 i 个值应当被重复几次"。
        """
        out = ColumnArray(self.nested.clone_empty())
        prev = 0
        for i, off in enumerate(offsets):
            for _ in range(off - prev):
                arr = self.get(i)
                out.append(arr)
            prev = off
        return out

    def clone_empty(self) -> "IColumn":
        return ColumnArray(self.nested.clone_empty())

    def default_value(self) -> Any:
        return []

    def byte_size(self) -> int:
        return self.nested.byte_size() + len(self.offsets) * 8

    def dump_structure(self, indent: int = 0) -> str:  # pragma: no cover
        pad = "  " * indent
        return (
            f"{pad}ColumnArray(rows={self.size()}, elements={self.elements()})\n"
            + self.nested.dump_structure(indent + 1)
        )


class ColumnTuple(IColumn):
    """元组列 —— 若干个**等长**的列竖着摞起来。

    注意它和 ``ColumnArray`` 的对称性:

    * ``Array(T)`` 是"**行数不定、元素平铺**" → 用 offsets 切;
    * ``Tuple(A, B)`` 是"**行数固定、成员并列**" → 直接存若干个列。

    所以 ``Tuple`` 的每一行是"横着的一条",而 ``Array`` 的每一行是
    "竖着的一段"。这个区分在 ``arrayJoin(t)`` 和 ``t.1`` 的写法上
    体现得非常直接。
    """

    def __init__(self, columns: Optional[Sequence[IColumn]] = None) -> None:
        self.columns: List[IColumn] = list(columns) if columns else []

    def size(self) -> int:
        return self.columns[0].size() if self.columns else 0

    def get(self, n: int) -> Any:
        return tuple(c.get(n) for c in self.columns)

    def hash_value(self, n: int) -> Any:
        return tuple(c.hash_value(n) for c in self.columns)

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        for i, c in enumerate(self.columns):
            r = c.compare_at(n, other.columns[i], m)
            if r:
                return r
        return 0

    def append(self, value: Any) -> None:
        if not isinstance(value, (list, tuple)):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Cannot insert {type(value).__name__} into Tuple; expected tuple",
            )
        if len(value) != len(self.columns):
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_IN_TUPLE_DOESNT_MATCH,
                f"Tuple has {len(self.columns)} elements but value has {len(value)}",
            )
        for c, v in zip(self.columns, value):
            c.append(v)

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def append_many_default(self, n: int) -> None:
        for c in self.columns:
            c.append_many_default(n)

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        for i, c in enumerate(self.columns):
            c.insert_range_from(other.columns[i], start, length)

    def insert_many_from(self, other: "IColumn", position: int, length: int) -> None:
        for i, c in enumerate(self.columns):
            c.insert_many_from(other.columns[i], position, length)

    def cut(self, start: int, length: int) -> None:
        for c in self.columns:
            c.cut(start, length)

    def pop_back(self, n: int = 1) -> None:
        for c in self.columns:
            c.pop_back(n)

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        for c in self.columns:
            c.filter(mask)
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        return ColumnTuple([c.permute(indexes) for c in self.columns])

    def get_many(self, indexes: Sequence[int]) -> "IColumn":
        return self.permute(indexes)

    def clone_empty(self) -> "IColumn":
        return ColumnTuple([c.clone_empty() for c in self.columns])

    def default_value(self) -> Any:
        return tuple(c.default_value() for c in self.columns)

    def byte_size(self) -> int:
        return sum(c.byte_size() for c in self.columns)

    def get_subcolumn(self, index: int) -> IColumn:
        return self.columns[index]

    def dump_structure(self, indent: int = 0) -> str:  # pragma: no cover
        pad = "  " * indent
        inner = "\n".join(c.dump_structure(indent + 1) for c in self.columns)
        return f"{pad}ColumnTuple(size={self.size()}, members={len(self.columns)})\n{inner}"


class ColumnMap(IColumn):
    """映射列 —— ``offsets`` + ``keys`` + ``values``。

    真实 ClickHouse 的 ``ColumnMap`` **继承自** ``ColumnArray``,因为
    ``Map(K,V)`` 在存储上就是 ``Array(Tuple(K,V))``。我们把这条继承关系
    写出来(而不是复制一份代码),是因为它解释了一个常见困惑:

        ``mapKeys(m)`` 和 ``arrayMap(x -> x.1, m)`` 在 ClickHouse 里
        是**同一个东西的两种写法** —— 因为 ``Map`` 真的就是数组。

    .. note::

       **这里有一个 Python 特有的名字遮蔽问题**。``ColumnMap`` 的两个
       数据属性叫 ``keys`` 与 ``values``,而 ``IColumn`` 有一个通用的
       迭代方法 ``values()`` —— 属性会遮蔽方法,所以在一个 Map 列上
       调用 ``.values()`` 得到的是**值列**而不是迭代器。
       我们把通用迭代器改名成 ``iter_values()`` 来避开这个坑
       (见 :meth:`IColumn.iter_values`)。真实 ClickHouse 没有这个问题,
       因为它没有"``values()`` 迭代器"这个方法。
    """

    def __init__(self, keys: Optional[IColumn] = None, values: Optional[IColumn] = None) -> None:
        self.keys: IColumn = keys if keys is not None else _placeholder()
        self.values: IColumn = values if values is not None else _placeholder()
        self.offsets: List[int] = []

    def size(self) -> int:
        return len(self.offsets)

    def elements(self) -> int:
        return self.offsets[-1] if self.offsets else 0

    def _span(self, n: int) -> tuple:
        if n < 0 or n >= len(self.offsets):
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Index {n} out of bounds in ColumnMap of size {self.size()}",
            )
        return (self.offsets[n - 1] if n > 0 else 0), self.offsets[n]

    def get(self, n: int) -> Any:
        s, e = self._span(n)
        return {self.keys.get(i): self.values.get(i) for i in range(s, e)}

    def hash_value(self, n: int) -> Any:
        s, e = self._span(n)
        return tuple(sorted(
            ((self.keys.hash_value(i), self.values.hash_value(i)) for i in range(s, e)),
            key=repr,
        ))

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        a, b = self.get(n), other.get(m)
        if a == b:
            return 0
        return -1 if len(a) < len(b) else 1

    def append(self, value: Any) -> None:
        if not isinstance(value, dict):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Cannot insert {type(value).__name__} into Map; expected dict",
            )
        for k, v in value.items():
            self.keys.append(k)
            self.values.append(v)
        self.offsets.append(self.keys.size())

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def append_many_default(self, n: int) -> None:
        last = self.offsets[-1] if self.offsets else 0
        self.offsets.extend([last] * n)

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        if isinstance(other, ColumnMap):
            for i in range(start, start + length):
                s = other.offsets[i - 1] if i > 0 else 0
                e = other.offsets[i]
                self.keys.insert_range_from(other.keys, s, e - s)
                self.values.insert_range_from(other.values, s, e - s)
                self.offsets.append(self.keys.size())
            return
        super().insert_range_from(other, start, length)

    def cut(self, start: int, length: int) -> None:
        elem_start = self.offsets[start - 1] if start > 0 else 0
        elem_end = self.offsets[start + length - 1] if length > 0 else elem_start
        del self.offsets[start : start + length]
        self.keys.cut(elem_start, elem_end - elem_start)
        self.values.cut(elem_start, elem_end - elem_start)
        removed = elem_end - elem_start
        for i in range(start, len(self.offsets)):
            self.offsets[i] -= removed

    def pop_back(self, n: int = 1) -> None:
        self.cut(self.size() - n, n)

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        elem_mask: List[bool] = []
        for i, keep in enumerate(mask):
            s = self.offsets[i - 1] if i > 0 else 0
            elem_mask.extend([keep] * (self.offsets[i] - s))
        self.keys.filter(elem_mask)
        self.values.filter(elem_mask)
        new_offsets: List[int] = []
        acc = 0
        for i, keep in enumerate(mask):
            if not keep:
                continue
            s = self.offsets[i - 1] if i > 0 else 0
            acc += self.offsets[i] - s
            new_offsets.append(acc)
        self.offsets = new_offsets
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        out = ColumnMap(self.keys.clone_empty(), self.values.clone_empty())
        for i in indexes:
            s, e = self._span(i)
            out.keys.insert_range_from(self.keys, s, e - s)
            out.values.insert_range_from(self.values, s, e - s)
            out.offsets.append(out.keys.size())
        return out

    def clone_empty(self) -> "IColumn":
        return ColumnMap(self.keys.clone_empty(), self.values.clone_empty())

    def default_value(self) -> Any:
        return {}

    def byte_size(self) -> int:
        return self.keys.byte_size() + self.values.byte_size() + len(self.offsets) * 8


class ColumnLowCardinality(IColumn):
    """低基数列 —— **字典 + 索引**。

    .. code-block:: text

      LowCardinality(String) 列,值 = ['GET','POST','GET','GET','PUT']
      dictionary: ['GET', 'POST', 'PUT']        ← 去重后的唯一值
      indexes:    [ 0,     1,     0,     0,   2 ]  ← 只是小整数

    为什么快 / 为什么省:

    * 比较两行是否相等,比的是**整数**而不是字符串;
    * GROUP BY 一个低基数列时,哈希表的大小受**字典大小**限制
      (1000 万个 GET,字典里只有 3 项);
    * 落盘时 dictionary 单独压缩,indexes 用一个位宽够用的整数类型。

    ``max_cardinality`` 未显式指定时,真实 ClickHouse 的**自适应阈值**是:
    当字典超过 ``低基数上限``(默认 ``100000``)时,列会在**写入时**
    退化成普通 ``String`` 列 —— 这个"退化"是不可逆的,但不会报错。
    第 03 章会讲这个自动降级什么时候会让你踩坑。

    .. note::

       低基数在 mini-clickhouse 里**只实现存储与相等比较**,不实现
       真实 CH 那套"字典编码的 SIMD 批量查找"。理由是后者需要一个
       针对整数索引的专用指令序列,在 Python 里演示不出收益 ——
       教程会明确标注这一点,而不是假装实现了。
    """

    def __init__(self, dictionary: Optional[IColumn] = None) -> None:
        self.dictionary: IColumn = dictionary if dictionary is not None else _placeholder()
        self.indexes: List[int] = []
        self._lookup: dict = {}

    def size(self) -> int:
        return len(self.indexes)

    def get(self, n: int) -> Any:
        if n < 0 or n >= len(self.indexes):
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Index {n} out of bounds in ColumnLowCardinality of size {self.size()}",
            )
        return self.dictionary.get(self.indexes[n])

    def get_index_at(self, n: int) -> int:
        return self.indexes[n]

    def hash_value(self, n: int) -> Any:
        # 关键优化:用**字典索引**当哈希值。这就是低基数在 GROUP BY 上快的原因。
        return self.indexes[n]

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        if isinstance(other, ColumnLowCardinality) and \
                self.dictionary is other.dictionary:
            a, b = self.indexes[n], other.indexes[m]
            if a == b:
                return 0
            return -1 if a < b else 1
        a, b = self.get(n), other.get(m)
        if a == b:
            return 0
        return -1 if a < b else 1

    def append(self, value: Any) -> None:
        key = value if not isinstance(value, (list, dict)) else repr(value)
        idx = self._lookup.get(key)
        if idx is None:
            idx = self.dictionary.size()
            self.dictionary.append(value)
            self._lookup[key] = idx
        self.indexes.append(idx)

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def append_many_default(self, n: int) -> None:
        self.append_many([self.dictionary.default_value()] * n)

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        for i in range(start, start + length):
            self.append(other.get(i))

    def insert_many_from(self, other: "IColumn", position: int, length: int) -> None:
        v = other.get(position)
        for _ in range(length):
            self.append(v)

    def cut(self, start: int, length: int) -> None:
        del self.indexes[start : start + length]

    def pop_back(self, n: int = 1) -> None:
        del self.indexes[len(self.indexes) - n :]

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        self.indexes = [v for v, keep in zip(self.indexes, mask) if keep]
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        out = ColumnLowCardinality(self.dictionary)
        out._lookup = dict(self._lookup)
        out.indexes = [self.indexes[i] for i in indexes]
        return out

    def get_many(self, indexes: Sequence[int]) -> "IColumn":
        return self.permute(indexes)

    def replicate(self, offsets: Sequence[int]) -> "IColumn":
        out = ColumnLowCardinality(self.dictionary)
        out._lookup = dict(self._lookup)
        prev = 0
        for i, off in enumerate(offsets):
            out.indexes.extend([self.indexes[i]] * (off - prev))
            prev = off
        return out

    def clone_empty(self) -> "IColumn":
        return ColumnLowCardinality(self.dictionary.clone_empty())

    def default_value(self) -> Any:
        # 默认值来自**字典列**(``LowCardinality(String)`` 的默认值是 ``''``,
        # 不是基类硬编码的 ``0``)——
        # 否则 ``append_default()`` 会把 ``int 0`` 塞进 ``String`` 字典,
        # ``INSERT NULL`` 之类的默认值路径会直接抛 "Cannot insert int into a String column"。
        return self.dictionary.default_value()

    def cardinality(self) -> int:
        return self.dictionary.size()

    def byte_size(self) -> int:
        return self.dictionary.byte_size() + len(self.indexes) * 4

    def dump_structure(self, indent: int = 0) -> str:  # pragma: no cover
        pad = "  " * indent
        return (
            f"{pad}ColumnLowCardinality(rows={self.size()}, "
            f"cardinality={self.cardinality()})"
        )


class ColumnConst(IColumn):
    """常量列 —— 存**一个**值 + 一个"有几行"的计数。

    ``SELECT 1 FROM t`` 里的那个 ``1`` 就是常量列。它的价值在于:

    * 内存 O(1) 而不是 O(rows);
    * 向量化函数可以**特化**:``plus(const, column)`` 只需要做
      "标量 + 向量",不需要为每一行取出常量值。

    真实 ClickHouse 的 ``ColumnConst`` 有 ``sizeness`` 与 ``data`` 两部分,
    并且 ``is_const()`` 是它的唯一标识。它有一个容易踩的语义:
    **常量列上的 append 不会改变 size(除非 size==0)** ——
    因为 size 是显式设的,不是数出来的。第 10 章会展示
    "忘了 ``convertToFullColumn`` 导致结果行数不对"的真实 bug。
    """

    def __init__(self, data: Optional[IColumn] = None, size: int = 0) -> None:
        self.data: IColumn = data if data is not None else _placeholder()
        self.sizeness = size

    def size(self) -> int:
        return self.sizeness

    def get(self, n: int) -> Any:
        if self.data.size() == 0:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                "Cannot read from an empty ColumnConst",
            )
        return self.data.get(0)

    def hash_value(self, n: int) -> Any:
        return self.data.hash_value(0)

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        return self.data.compare_at(0, other, m)

    def append(self, value: Any) -> None:
        """常量列的 append 语义很特别:

        * 若当前是空的(size==0),写入值并把 size 置 1;
        * 否则**要求值与已有的相等**,只把 size 加一;
        * 值不等 → 抛异常。

        真实 CH 是 ``ColumnConst::insert``,行为一致。
        """
        if self.sizeness == 0:
            self.data.append(value)
            self.sizeness = 1
            return
        if not _same_value(self.data.get(0), value):
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"Cannot insert a different value into a constant column: "
                f"has {self.data.get(0)!r}, got {value!r}",
            )
        self.sizeness += 1

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def append_many_default(self, n: int) -> None:
        if self.sizeness == 0:
            self.data.append_default()
        self.sizeness += n

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        v = other.get(start)
        for _ in range(length):
            self.append(v)

    def insert_many_from(self, other: "IColumn", position: int, length: int) -> None:
        v = other.get(position)
        for _ in range(length):
            self.append(v)

    def cut(self, start: int, length: int) -> None:
        if length >= self.sizeness:
            self.data = self.data.clone_empty()
            self.sizeness = 0
            return
        self.sizeness -= length

    def pop_back(self, n: int = 1) -> None:
        self.cut(self.sizeness - n, n)

    def resize(self, n: int) -> None:
        self.sizeness = n

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        if len(mask) != self.sizeness:
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != column size {self.sizeness}",
            )
        self.sizeness = sum(1 for m in mask if m)
        if self.sizeness == 0:
            self.data = self.data.clone_empty()
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        return ColumnConst(self.data.clone(), len(indexes))

    def get_many(self, indexes: Sequence[int]) -> "IColumn":
        return ColumnConst(self.data.clone(), len(indexes))

    def replicate(self, offsets: Sequence[int]) -> "IColumn":
        total = offsets[-1] if offsets else 0
        return ColumnConst(self.data.clone(), total)

    def clone_empty(self) -> "IColumn":
        return ColumnConst(self.data.clone_empty(), 0)

    def clone(self) -> "IColumn":
        return ColumnConst(self.data.clone(), self.sizeness)

    def clone_resized(self, n: int) -> "IColumn":
        return ColumnConst(self.data.clone(), n)

    def is_const(self) -> bool:
        return True

    def convert_to_full_column(self) -> "IColumn":
        """把常量列展开成真正的完整列。

        .. warning::

           任何"就地改写"的处理器(比如 ``arrayJoin``、``ORDER BY``)
           在拿到常量列时**必须先调用这个方法**。否则:
           ``SELECT arrayJoin([1,2,3]) FROM t`` —— 如果 ``[1,2,3]``
           是常量列,展开后会得到 3 行而不是 ``3 × |t|`` 行。
           这是真实 ClickHouse 里被修过不止一次的 bug 类型。
        """
        out = self.data.clone_empty()
        out.insert_many_from(self.data, 0, self.sizeness)
        return out

    def byte_size(self) -> int:
        return self.data.byte_size()

    def default_value(self) -> Any:
        return self.data.default_value()


class ColumnSparse(IColumn):
    """稀疏列 —— 只存**非默认值**的位置与值。

    .. code-block:: text

      值:        [0, 0, 7, 0, 0, 0, 3]     (默认值是 0)
      offsets:   [2,        6]             ← 第 2 行、第 6 行有非零值
      values:    [7,        3]

    真实 ClickHouse 在 ``ratio_of_defaults_for_sparse_serialization``
    (默认 ``0.9``)以上时,写入会自动改用稀疏序列化。典型场景是
    事件表里那些"99% 的行都是 0"的布尔/计数列 —— 稀疏编码能把这列
    压到几乎为零。

    稀疏列支持的比较、过滤等操作在真实 CH 里是**受限**的
    (它优先在写入路径出现,读到内存里通常会先转成稠密)。
    mini-clickhouse 把这条限制如实实现:``compare_at`` 可用,
    但 ``permute``/``replicate`` 会先转稠密。
    """

    def __init__(self, values: Optional[IColumn] = None, default_value: Any = 0) -> None:
        self.values: IColumn = values if values is not None else _placeholder()
        self.offsets: List[int] = []
        self._default = default_value

    def size(self) -> int:
        # 稀疏列必须显式记住总行数(offsets 只记非默认行)
        return self._total_size if hasattr(self, "_total_size") else 0

    def _set_total(self, n: int) -> None:
        self._total_size = n

    def append(self, value: Any) -> None:
        n = self.size()
        if value != self._default:
            self.offsets.append(n)
            self.values.append(value)
        self._total_size = n + 1

    def append_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.append(v)

    def append_many_default(self, n: int) -> None:
        self._total_size = self.size() + n

    def get(self, n: int) -> Any:
        if n < 0 or n >= self.size():
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"Index {n} out of bounds")
        # offsets 是有序的,用 bisect 找
        import bisect
        pos = bisect.bisect_left(self.offsets, n)
        if pos < len(self.offsets) and self.offsets[pos] == n:
            return self.values.get(pos)
        return self._default

    def hash_value(self, n: int) -> Any:
        return self.get(n)

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        a, b = self.get(n), other.get(m)
        if a == b:
            return 0
        return -1 if a < b else 1

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        dense = self.convert_to_full_column()
        dense.filter(mask)
        self._adopt(dense)
        return self

    def _adopt(self, dense: IColumn) -> None:
        self.values = dense.clone_empty()
        self.offsets = []
        self._total_size = 0
        for v in dense.iter_values():
            self.append(v)

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        return self.convert_to_full_column().permute(indexes)

    def clone_empty(self) -> "IColumn":
        out = ColumnSparse(self.values.clone_empty(), self._default)
        out._set_total(0)
        return out

    def convert_to_full_column(self) -> "IColumn":
        out = self.values.clone_empty()
        for i in range(self.size()):
            out.append(self.get(i))
        return out

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        for i in range(start, start + length):
            self.append(other.get(i))

    def cut(self, start: int, length: int) -> None:
        dense = self.convert_to_full_column()
        dense.cut(start, length)  # 就地把 [start, start+length) 删掉返回 None
        self._adopt(dense)

    def is_sparse(self) -> bool:
        return True

    def byte_size(self) -> int:
        return self.values.byte_size() + len(self.offsets) * 8

    def default_value(self) -> Any:
        return self._default


class ColumnAggregateFunction(IColumn):
    """聚合中间状态列 —— 对应类型 ``AggregateFunction(sum, UInt64)``。

    这一列的"值"根本不是普通数据,而是一个**聚合状态对象**
    (比如"到目前为止的和、计数、一些 Sketch 字节")。

    它存在的意义是让 ``AggregatingMergeTree`` 与
    ``-State`` / ``-Merge`` 组合器成为可能:

    .. code-block:: sql

       -- 写入时存"状态"而不是"结果"
       INSERT INTO t SELECT uniqState(user_id) FROM raw GROUP BY day;
       -- 查询时把状态合并
       SELECT day, uniqMerge(state) FROM t GROUP BY day;

    这样"预聚合"就可以分级、跨表、跨时间合并 —— 这是 ClickHouse
    做物化视图增量聚合的基石(第 21 章)。
    """

    def __init__(self, func: Any = None, states: Optional[list] = None) -> None:
        self.func = func
        self.states: List[Any] = list(states) if states else []

    def size(self) -> int:
        return len(self.states)

    def get(self, n: int) -> Any:
        if n < 0 or n >= len(self.states):
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, f"Index {n} out of bounds")
        return self.states[n]

    def hash_value(self, n: int) -> Any:
        return self.func.serialize_state(self.states[n])

    def compare_at(self, n: int, other: "IColumn", m: int) -> int:
        a = self.func.serialize_state(self.states[n])
        b = self.func.serialize_state(other.get(m))
        if a == b:
            return 0
        return -1 if a < b else 1

    def append(self, value: Any) -> None:
        self.states.append(value)

    def append_many(self, values: Iterable[Any]) -> None:
        self.states.extend(values)

    def append_default(self) -> None:
        self.states.append(self.func.create_state() if self.func else None)

    def append_many_default(self, n: int) -> None:
        for _ in range(n):
            self.append_default()

    def insert_range_from(self, other: "IColumn", start: int, length: int) -> None:
        self.states.extend(other.states[start : start + length])

    def insert_many_from(self, other: "IColumn", position: int, length: int) -> None:
        st = other.states[position]
        self.states.extend(
            [self.func.clone_state(st) if self.func else st] * length
        )

    def cut(self, start: int, length: int) -> None:
        del self.states[start : start + length]

    def pop_back(self, n: int = 1) -> None:
        del self.states[len(self.states) - n :]

    def filter(self, mask: Sequence[bool]) -> "IColumn":
        if len(mask) != len(self.states):
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != column size {len(self.states)}",
            )
        self.states = [s for s, keep in zip(self.states, mask) if keep]
        return self

    def permute(self, indexes: Sequence[int]) -> "IColumn":
        out = ColumnAggregateFunction(self.func)
        out.states = [self.func.clone_state(self.states[i]) if self.func
                      else self.states[i] for i in indexes]
        return out

    def get_many(self, indexes: Sequence[int]) -> "IColumn":
        return self.permute(indexes)

    def clone_empty(self) -> "IColumn":
        return ColumnAggregateFunction(self.func)

    def byte_size(self) -> int:
        return sum(
            len(self.func.serialize_state(s)) if self.func else 8 for s in self.states
        )

    def is_aggregate(self) -> bool:
        return True


# ══════════════════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════════════════


def _same_value(a: Any, b: Any) -> bool:
    """常量列的"值是否相同"判定 —— **NaN 必须等于它自己**。

    为什么不能直接用 ``!=``:``float('nan') != float('nan')`` 为真,于是
    ``SELECT nan`` 会被判成"往常量列里塞了一个不同的值"并抛
    ``LOGICAL_ERROR`` —— 而它塞的明明就是同一个 NaN。

    同样的道理适用于 ``ORDER BY`` 与 ``GROUP BY``:ClickHouse 的语义里
    NaN **等于自己**(所以它们会落进同一个分组、排在一起)。Python 的
    默认比较是反过来的,必须在每一处显式处理。
    """
    if a is b:
        return True
    if isinstance(a, float) and isinstance(b, float):
        import math

        if math.isnan(a) and math.isnan(b):
            return True
    try:
        return bool(a == b)
    except Exception:  # pragma: no cover - 不可比较的对象退化成"不相同"
        return False


def _placeholder() -> IColumn:
    """占位列 —— 当嵌套列类型还没确定时先放一个空的 ``UInt8`` 列。

    真实 ClickHouse 不允许这样做(类型必须在构造时就完整)。我们允许,
    是因为 Python 的类型声明是运行时的,`ColumnArray()` 无参构造在测试里
    很方便。**任何用到占位列的真实计算都会失败**,所以它不会被静默带进结果。
    """
    from .base import ColumnVector

    return ColumnVector()
