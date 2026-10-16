"""数据跳过索引(skip index)—— 给**非主键列**也装上一双眼睛。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  稀疏主键索引只能帮"排序键前缀"上的过滤 —— 其它列怎么办?               │
   └──────────────────────────────────────────────────────────────────────┘

   表 ``ORDER BY (date, id)``,数据里还有一个 ``url`` 列。查询:

   .. code-block:: sql

      SELECT count() FROM hits WHERE url = 'http://example.com/x';

   ``url`` 不是排序键的一部分,所以主键索引**完全用不上** —— 只能
   一个一个 granule 读出来过滤。如果 ``url`` 的取值很稀疏,这等于
   "全表扫描 + 逐行判断"。

   跳过索引就是为这类查询准备的。它在**每个 granule 组**上记录一份
   "摘要",查询时先问摘要"这个 granule 里**可能**有满足条件的行吗?",

   .. code-block:: text

     granule 组(默认 granularity=1 → 一个 granule 一组,8192 行)
     ┌──────────────────────────────────────────────────────────────┐
     │  rows 0..8191 : url ∈ {...}   → skp_idx_url.idx 第 0 条         │
     │  rows 8192..16383 : url ∈ {..} → skp_idx_url.idx 第 1 条        │
     └──────────────────────────────────────────────────────────────┘
                                  │
              WHERE url='...'  →  摘要.may_be_true('=' , '...')
                                  │
                        ┌─────────┴─────────┐
                     False                 True
                  「一定没有」           「可能有」
                   整组跳过 ✓            读出来再过滤

   ╔══════════════════════════════════════════════════════════════════════╗
   ║  **最重要的不变量(必须背下来)**                                     ║
   ║                                                                      ║
   ║      ``may_be_true`` 允许"错报真"(false positive),                   ║
   ║      但**绝不允许"错报假"**(false negative)。                        ║
   ║                                                                      ║
   ║  返回 False 等于宣称"这个 granule 里一定没有匹配的行",于是整组被     ║
   ║  跳过。如果那里其实有匹配行,查询就会**少返回数据 —— 结果错误**。    ║
   ║  这比"慢"严重一个数量级:慢只是体验问题,错是数据问题。                ║
   ║                                                                      ║
   ║  所以每个 ``may_be_true`` 的默认返回值都是 **True**(不确定就放行)。  ║
   ╚══════════════════════════════════════════════════════════════════════╝

   真实 ClickHouse 的跳过索引有十几种(``minmax``/``set``/``bloom_filter``/
   ``ngrambf_v1``/``tokenbf_v1``/``bloom_filter``/``hypothesis``/``vector`` …),
   它们共享同一个接口 :class:`IMergeTreeIndex`。mini-clickhouse 实现其中
   最有教学价值的五个。

   .. warning::

      **跳过索引对小 part 是负收益。** 它自己也要占磁盘、也要在写入时
      计算,而每个 part 至少要有一个 granule 组 —— 一个 100 行的小 part
      里,索引条数可能比数据还"贵"。真实 ClickHouse 因此在
      ``INSERT`` 时**不建**跳过索引,只在 merge 时才建(``index_granularity``
      也是这个逻辑)。mini-clickhouse 为了演示直观,在 ``write_part``
      里**总是**建索引,并在文档里标出这个差异。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...block import Block, ColumnWithTypeAndName
from ...columns import ColumnString, ColumnVector, IColumn
from ...errors import ErrorCode, raise_error
from ...serialization import deserialize_binary, read_var_uint, serialize_binary, write_var_uint
from ...types import DataType, get_data_type
from ...util.hashing import siphash64
from .marks import MarkRange

__all__ = [
    "IndexCondition",
    "IMergeTreeIndex",
    "MinMaxIndex",
    "SetIndex",
    "BloomFilterIndex",
    "NgramBFIndex",
    "TokenBFIndex",
    "IndexDescription",
    "IndexGranule",
    "IndexHelper",
    "MergeTreeIndexReader",
    "collect_index_conditions",
    "create_index",
    "supported_index_types",
    "BloomFilter",
]


# ══════════════════════════════════════════════════════════════════════════
# 查询条件 —— 跳过索引看到的"一个条件"
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class IndexCondition:
    """一个可以用跳过索引评估的条件 —— ``<index_expr> <op> <value>``。

    真实 ClickHouse 里对应 ``IndexCondition``(它挂着一个
    ``ASTPtr`` + ``SetPtr``),由 ``KeyCondition`` 的同一套机制从
    ``WHERE`` 里榨出来。这里只保留"操作符 + 值",因为跳过索引
    能利用的信息本来就只有这些。
    """

    op: str
    value: Any
    value2: Any = None
    #: 该条件是否被 ``NOT`` 取反过(取反后多数索引只能"放弃")
    negated: bool = False

    def describe(self) -> str:  # pragma: no cover - 调试用
        if self.op == "between":
            return f"BETWEEN {self.value!r} AND {self.value2!r}"
        if self.op in ("in", "notIn"):
            return f"{self.op.upper()} {list(self.value)[:4]!r}"
        return f"{self.op} {self.value!r}"

    def swapped(self) -> "IndexCondition":
        """把 ``v < col`` 翻成 ``col > v`` —— 比较的两侧可以互换。"""
        flip = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "==": "=",
                "!=": "!=", "<>": "!="}
        return IndexCondition(flip.get(self.op, self.op), self.value, self.value2,
                              self.negated)


# ══════════════════════════════════════════════════════════════════════════
# 索引基类
# ══════════════════════════════════════════════════════════════════════════


class IndexGranule:
    """一个"索引粒度组"上算出来的摘要 —— 抽象基类。

    真实 ClickHouse 的 ``IMergeTreeIndexGranule`` 有 ``serialize``/
    ``deserialize``/``size`` 三个方法;我们额外要求 ``may_be_true`` ——
    因为"拿摘要来回答一个条件"是它的唯一用途。
    """

    def may_be_true(self, cond: IndexCondition) -> bool:
        return True  # 默认:什么都不知道 → 放行(绝不产生 false negative)

    def serialize(self) -> bytes:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{type(self).__name__}.serialize")

    def aggregate_size(self) -> int:  # pragma: no cover - 只用于统计
        return len(self.serialize())


class IMergeTreeIndex:
    """跳过索引的接口 —— 与真实 ClickHouse 的 ``IMergeTreeIndex`` 同名。

    ``build`` 收一个**只有索引表达式那一列**的 Block(行数 =
    一个粒度组),返回序列化后的摘要字节。
    """

    #: 索引类型名(``minmax`` / ``set`` / ``bloom_filter`` …)
    index_type: str = "unknown"

    def __init__(self, name: str, expr_sql: str, expr: Any,
                 params: Sequence[Any], granularity: int, data_type: DataType) -> None:
        self.name = name
        self.expr_sql = expr_sql
        self.expr = expr
        self.params = list(params)
        self.granularity = max(1, int(granularity))
        self.data_type = data_type

    # ── 必须实现 ────────────────────────────────────────────────────────

    def build(self, column: IColumn) -> bytes:
        """算出一个粒度组的摘要并序列化。"""
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.index_type}.build")

    def deserialize(self, data: bytes) -> IndexGranule:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.index_type}.deserialize")

    # ── 描述 ────────────────────────────────────────────────────────────

    def describe(self) -> str:
        params = ""
        if self.params:
            params = "(" + ", ".join(str(p) for p in self.params) + ")"
        return (
            f"INDEX {self.name} {self.expr_sql} TYPE {self.index_type}{params} "
            f"GRANULARITY {self.granularity}"
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.name} on {self.expr_sql}>"


# ── 值比较 ──────────────────────────────────────────────────────────────


def _cmp(a: Any, b: Any) -> Optional[int]:
    """比较两个值;``None`` 表示**无法比较**。

    .. warning::

       **``None`` 绝不能被当成"不满足条件"。**

       这是跳过索引里最阴的一类 bug:比如索引列存的是 ``Date``(内部是
       "天数"整数),而 ``WHERE d >= '2024-01-04'`` 送来的是**字符串**
       字面量 —— ``19726 >= '2024-01-04'`` 在 Python 里抛 ``TypeError``。
       以前的实现把这种情况编码成 ``-2``,而 ``>=``/``>``/``between``
       的分支把它当成"条件不成立"→ **整个 granule 被跳过 → 丢数据**。

       正确做法是把它当成"**我什么都不知道**" → 放行(返回 True)。
       "不确定就放行"是跳过索引唯一安全的默认值。
    """
    if a is None or b is None:
        if a is None and b is None:
            return 0
        return 1 if a is None else -1
    if a == b:
        return 0
    try:
        return -1 if a < b else 1
    except TypeError:
        return None


def _orderable(a: Any, b: Any) -> Optional[int]:
    """``_cmp`` 的显式别名 —— 读代码时一眼能看出"这里可能比不了"。"""
    return _cmp(a, b)


def _value_blob(value: Any, dtype: DataType) -> bytes:
    """把一个值按**列类型**序列化成字节 —— 哈希与 minmax 都用它。

    真实 ClickHouse 用列自己的 ``serializeValueIntoArena``/``getHash``,
    保证"写入时看到的字节"与"查询时看到的字节"一致。我们同样走
    :func:`minich.serialization.serialize_binary`,这样 ``Date``
    (内部是天数)与 ``String``(内部是字节)都能得到稳定的字节表示。
    """
    col = dtype.create_column()
    col.append(dtype.normalize(value) if not dtype.is_nothing() else None)
    return serialize_binary(dtype, col)


# ══════════════════════════════════════════════════════════════════════════
# minmax
# ══════════════════════════════════════════════════════════════════════════


class MinMaxGranule(IndexGranule):
    __slots__ = ("min_values", "max_values")

    def __init__(self, min_values: List[Any], max_values: List[Any]) -> None:
        self.min_values = min_values
        self.max_values = max_values

    def _bounds(self, cond: IndexCondition) -> Optional[Tuple[Any, Any]]:
        """取出"被比较的那一对 (min, max)"。

        单列索引 → 标量;多列(元组)索引 → 按条件值**整元组**比较。
        元组条件与索引列**元数不同**、或者条件值不是元组 → 比不了 → ``None``。
        """
        if len(self.min_values) != 1:
            v = cond.value
            if isinstance(v, (list, tuple)) and len(v) == len(self.min_values):
                return tuple(self.min_values), tuple(self.max_values)
            return None
        return self.min_values[0], self.max_values[0]

    def may_be_true(self, cond: IndexCondition) -> bool:
        """任何一个分支只要**比不了**,就必须返回 True(放行)。"""
        bounds = self._bounds(cond)
        if bounds is None:
            return True
        lo, hi = bounds
        op, v = cond.op, cond.value

        def out_of_range(w: Any) -> Optional[bool]:
            """``w`` 是否可能落在 [lo, hi] 内 —— 比不了返回 ``None``。"""
            c1, c2 = _orderable(lo, w), _orderable(w, hi)
            if c1 is None or c2 is None:
                return None
            return c1 <= 0 and c2 <= 0

        if op == "=":
            r = out_of_range(v)
            return True if r is None else r
        if op == "<":
            c = _orderable(lo, v)
            return True if c is None else c < 0
        if op == "<=":
            c = _orderable(lo, v)
            return True if c is None else c <= 0
        if op == ">":
            c = _orderable(hi, v)
            return True if c is None else c > 0
        if op == ">=":
            c = _orderable(hi, v)
            return True if c is None else c >= 0
        if op == "!=":
            # 只有"整组所有值都等于 v"时才能跳过 —— 即 min == max == v
            c1, c2 = _orderable(lo, v), _orderable(hi, v)
            if c1 is None or c2 is None:
                return True
            return not (c1 == 0 and c2 == 0)
        if op == "between":
            c1 = _orderable(hi, cond.value)
            c2 = _orderable(lo, cond.value2)
            if c1 is None or c2 is None:
                return True
            return c1 >= 0 and c2 <= 0
        if op == "in":
            for w in cond.value:
                r = out_of_range(w)
                if r is None:
                    return True      # 有一个值比不了 → 整条 IN 无法裁决
                if r:
                    return True
            return False
        if op == "notIn":
            all_equal = True
            for w in cond.value:
                c1, c2 = _orderable(lo, w), _orderable(hi, w)
                if c1 is None or c2 is None:
                    return True
                if not (c1 == 0 and c2 == 0):
                    all_equal = False
            return not all_equal
        return True


class MinMaxIndex(IMergeTreeIndex):
    """``TYPE minmax`` —— 每个粒度组记 (min, max)。

    这是最便宜、最通用的跳过索引:一对值就能回答"范围类"条件。

    .. code-block:: text

       组 0: url ∈ ['a', 'm']   ← WHERE url > 'z' → 比 max 还大 → **跳过**
       组 1: url ∈ ['n', 'z']   ← WHERE url > 'z' → 有可能         → 读

    真实 ClickHouse 的 ``minmax`` 索引还有一个 ``max_rows_...`` 之类的
    优化,并且对**字符串**会做截断(只存前缀以省空间)。我们存完整值,
    更占空间但更好懂。
    """

    index_type = "minmax"

    def build(self, column: IColumn) -> bytes:
        if column.size() == 0:
            return b""
        vals = [column.get(i) for i in range(column.size())]
        vals = [v for v in vals if v is not None]
        if not vals:
            return b""
        try:
            lo, hi = min(vals), max(vals)
        except TypeError:  # pragma: no cover - 不可比较的类型
            return b""
        out = bytearray()
        lo_b = _value_blob(lo, self.data_type)
        hi_b = _value_blob(hi, self.data_type)
        write_var_uint(out, len(lo_b))
        out += lo_b
        write_var_uint(out, len(hi_b))
        out += hi_b
        return bytes(out)

    def deserialize(self, data: bytes) -> MinMaxGranule:
        if not data:
            # 空的粒度组:min/max 用一个"永远放行"的哨兵表示
            return _EmptyGranule()
        lo_len, pos = read_var_uint(data, 0)
        lo = deserialize_binary(self.data_type, data[pos:pos + lo_len], 1).get(0)
        pos += lo_len
        hi_len, pos = read_var_uint(data, pos)
        hi = deserialize_binary(self.data_type, data[pos:pos + hi_len], 1).get(0)
        return MinMaxGranule([lo], [hi])


class _EmptyGranule(IndexGranule):
    """空粒度组 —— 一律放行(空组里没有行,读不读都一样,但放行最安全)。"""

    __slots__ = ()

    def may_be_true(self, cond: IndexCondition) -> bool:
        return True

    def serialize(self) -> bytes:
        return b""


# ══════════════════════════════════════════════════════════════════════════
# set
# ══════════════════════════════════════════════════════════════════════════


class SetGranule(IndexGranule):
    __slots__ = ("values", "truncated")

    def __init__(self, values: List[Any], truncated: bool) -> None:
        self.values = values
        self.truncated = truncated

    def _contains(self, value: Any) -> Optional[bool]:
        """组里是否有等于 ``value`` 的值 —— 有"比不了"的值就返回 ``None``。"""
        for v in self.values:
            c = _orderable(v, value)
            if c is None:
                return None
            if c == 0:
                return True
        return False

    def may_be_true(self, cond: IndexCondition) -> bool:
        if self.truncated:
            # 值太多,已经截断了 —— 摘要**不再完备**,必须放行。
            return True
        op = cond.op
        if op == "=":
            r = self._contains(cond.value)
            return True if r is None else r
        if op == "in":
            for w in cond.value:
                r = self._contains(w)
                if r is None:
                    return True
                if r:
                    return True
            return False
        if op == "!=":
            # 只有"整组只有一个值,且那个值就是 v"时才可跳过
            if len(self.values) != 1:
                return True
            c = _orderable(self.values[0], cond.value)
            if c is None:
                return True
            return c != 0
        return True


class SetIndex(IMergeTreeIndex):
    """``TYPE set(N)`` —— 每个粒度组记**最多 N 个不同值**。

    超过 N 个不同值就**截断**,并把 ``truncated`` 置真 —— 从此以后
    这个组"什么都不知道",一律放行。这就是为什么 ``set`` 只适合
    低基数列:唯一值一多,索引就退化成"永远返回 True"。

    真实 ClickHouse 的 ``set`` 用的是 ``Set``(有序集合)并做二分,
    我们退化成线性扫描 —— 对 N ≤ 100 的规模没有区别。
    """

    index_type = "set"

    def __init__(self, name: str, expr_sql: str, expr: Any,
                 params: Sequence[Any], granularity: int, data_type: DataType) -> None:
        super().__init__(name, expr_sql, expr, params, granularity, data_type)
        self.max_values = int(params[0]) if params else 100
        if self.max_values <= 0:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"set index {name}: max_rows must be positive, got {self.max_values}",
            )

    def build(self, column: IColumn) -> bytes:
        seen: List[Any] = []
        truncated = False
        for i in range(column.size()):
            v = column.get(i)
            if any(_cmp(v, s) == 0 for s in seen):
                continue
            if len(seen) >= self.max_values:
                truncated = True
                break
            seen.append(v)
        out = bytearray()
        out.append(1 if truncated else 0)
        write_var_uint(out, len(seen))
        for v in seen:
            blob = _value_blob(v, self.data_type) if not self.data_type.is_nothing() \
                else b""
            write_var_uint(out, len(blob))
            out += blob
        return bytes(out)

    def deserialize(self, data: bytes) -> SetGranule:
        truncated = bool(data[0])
        count, pos = read_var_uint(data, 1)
        values: List[Any] = []
        for _ in range(count):
            ln, pos = read_var_uint(data, pos)
            values.append(deserialize_binary(self.data_type, data[pos:pos + ln], 1).get(0))
            pos += ln
        return SetGranule(values, truncated)


# ══════════════════════════════════════════════════════════════════════════
# 布隆过滤器
# ══════════════════════════════════════════════════════════════════════════


class BloomFilter:
    """一个**真的**布隆过滤器 —— 位数组 + k 个哈希。

    公式直接来自 Bloom 1970 年的原论文(也是真实 ClickHouse
    ``BloomFilter`` 用的):

    .. code-block:: text

       给定"预期插入 n 个元素"与"可接受的误判率 p":

           位数组大小   m = -n · ln(p) / (ln 2)²
           哈希个数     k = (m / n) · ln 2 ≈ 0.693 · m/n

       ┌── 直觉 ─────────────────────────────────────────────────────┐
       │  m 太小 → 位很快被占满 → 全是误判 → 索引没用                 │
       │  m 太大 → 索引自己变成大文件 → 得不偿失                      │
       │  k 太多 → 每次插入要算 k 次哈希;比较表中的 k ≈ 7 是甜点       │
       └────────────────────────────────────────────────────────────┘

    ``might_contain`` **只会误判为真,绝不会误判为假** —— 这正是
    跳过索引需要的那条不变量(把不存在的说成存在,只是多读一点;
    把存在的说成不存在,就是丢数据)。

    .. note::

       真实 ClickHouse 用两次 ``Hash`` 调用配合"双重哈希"
       (``h_i = h1 + i·h2``)生成 k 个位置,并且哈希是**列级**
       ``getHash``(对数值列就是它的字节)的 ``hash_combine``。
       mini-clickhouse 用 :func:`minich.util.hashing.siphash64` 对
       **序列化后的值字节**做双重哈希 —— 算法同类(都是把值映射成
       稳定的字节哈希),但**字节输入与真实 CH 不同**,所以同一个值
       在两个系统里的位图不同。这**不影响各自系统内的正确性**。
    """

    __slots__ = ("bits", "num_hashes", "seed", "_data")

    def __init__(self, bits: int, num_hashes: int, seed: int = 0) -> None:
        if bits <= 0 or num_hashes <= 0:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"BloomFilter needs positive bits/hashes, got ({bits}, {num_hashes})",
            )
        self.bits = bits
        self.num_hashes = num_hashes
        self.seed = seed
        self._data = bytearray((bits + 7) // 8)

    @classmethod
    def with_parameters(cls, n: int, false_positive_rate: float, seed: int = 0) -> "BloomFilter":
        """按 ``(n, p)`` 算出 ``(m, k)`` —— 就是上面那两个公式。"""
        if n <= 0:
            n = 1
        if not (0 < false_positive_rate < 1):
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"false_positive_rate must be in (0, 1), got {false_positive_rate}",
            )
        ln2 = math.log(2.0)
        m = int(math.ceil(-n * math.log(false_positive_rate) / (ln2 * ln2)))
        m = max(m, 64)
        k = max(1, int(round((m / n) * ln2)))
        return cls(m, k, seed)

    def _positions(self, data: bytes) -> List[int]:
        h1 = siphash64(data, self.seed & 0xFFFFFFFFFFFFFFFF,
                       (self.seed >> 8) & 0xFFFFFFFFFFFFFFFF)
        h2 = siphash64(data + b"\x01", self.seed & 0xFFFFFFFFFFFFFFFF,
                       (self.seed >> 8) & 0xFFFFFFFFFFFFFFFF)
        out = []
        for i in range(self.num_hashes):
            out.append((h1 + i * h2) % self.bits)
        return out

    def add(self, data: bytes) -> None:
        for pos in self._positions(data):
            self._data[pos >> 3] |= 1 << (pos & 7)

    def might_contain(self, data: bytes) -> bool:
        for pos in self._positions(data):
            if not (self._data[pos >> 3] & (1 << (pos & 7))):
                return False   # 有一位是 0 → **一定**不在
        return True            # 全为 1 → 可能在(可能误判)

    def serialize(self) -> bytes:
        out = bytearray()
        write_var_uint(out, self.bits)
        write_var_uint(out, self.num_hashes)
        write_var_uint(out, self.seed & 0xFFFFFFFFFFFFFFFF)
        out += self._data
        return bytes(out)

    @classmethod
    def deserialize(cls, data: bytes) -> "BloomFilter":
        bits, pos = read_var_uint(data, 0)
        num_hashes, pos = read_var_uint(data, pos)
        seed, pos = read_var_uint(data, pos)
        bf = cls(bits, num_hashes, seed)
        bf._data = bytearray(data[pos:pos + (bits + 7) // 8])
        return bf


class BloomFilterGranule(IndexGranule):
    __slots__ = ("filters",)

    def __init__(self, filters: List[BloomFilter]) -> None:
        self.filters = filters

    def may_be_true(self, cond: IndexCondition) -> bool:
        if not self.filters:
            return True
        op = cond.op
        if op == "=":
            return self.filters[0].might_contain(_to_bytes(cond.value))
        if op == "in":
            return any(self.filters[0].might_contain(_to_bytes(v)) for v in cond.value)
        if op in ("like", "match"):
            # 布隆过滤器回答不了"包含某个子串" —— 保守放行
            return True
        return True


def _to_bytes(value: Any) -> bytes:
    """把值转成哈希输入的字节。

    真实 ClickHouse 对**不同列类型**有不同的哈希输入(数值是
    原始字节、字符串是内容)。我们统一走"字符串形态的 utf-8 字节":
    简单、稳定、对字符串列与数值列都成立。**代价**是"5"(Int)与
    "'5'"(String)会落到同一个位图 —— 这在同一个索引列上不可能同时
    出现两种类型,所以安全。
    """
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, bool):
        return b"1" if value else b"0"
    if isinstance(value, (int, float)):
        return str(value).encode("utf-8", "surrogateescape")
    return str(value).encode("utf-8", "surrogateescape")


class BloomFilterIndex(IMergeTreeIndex):
    """``TYPE bloom_filter([false_positive_rate])`` —— 整值级别的布隆过滤器。

    适合"高基数、等值查询"的列(用户 ID、URL、trace_id…):
    ``minmax`` 在高基数上几乎没用(每组的 min/max 跨度极大),
    而布隆过滤器对"某个具体值在不在这一组里"回答得又快又准。

    .. warning::

       真实 ClickHouse 的 ``bloom_filter`` 参数是
       ``size_of_bloom_filter_in_bytes``(默认 256,单位字节),
       **不是**误判率。它这样设计是因为"误判率"要依赖"组里有多少个
       元素",而那是运行时才知道的。mini-clickhouse 反过来接受
       误判率 ``p``,自己按公式算 ``m`` —— 更贴合教科书,但**参数含义
       与真实 CH 不同**,不要照搬。
    """

    index_type = "bloom_filter"

    def __init__(self, name: str, expr_sql: str, expr: Any,
                 params: Sequence[Any], granularity: int, data_type: DataType) -> None:
        super().__init__(name, expr_sql, expr, params, granularity, data_type)
        self.false_positive_rate = float(params[0]) if params else 0.01

    def build(self, column: IColumn) -> bytes:
        n = max(1, column.size())
        # 每个粒度组一个过滤器(真实 CH 在 granularity > 1 时对组内的
        # 每个 granule 各建一个 —— 这里是"组级一个",文档里标注了差异)
        bf = BloomFilter.with_parameters(n, self.false_positive_rate)
        for i in range(column.size()):
            bf.add(_to_bytes(column.get(i)))
        out = bytearray()
        write_var_uint(out, 1)
        blob = bf.serialize()
        write_var_uint(out, len(blob))
        out += blob
        return bytes(out)

    def deserialize(self, data: bytes) -> BloomFilterGranule:
        if not data:
            return BloomFilterGranule([])
        count, pos = read_var_uint(data, 0)
        filters: List[BloomFilter] = []
        for _ in range(count):
            ln, pos = read_var_uint(data, pos)
            filters.append(BloomFilter.deserialize(data[pos:pos + ln]))
            pos += ln
        return BloomFilterGranule(filters)


# ══════════════════════════════════════════════════════════════════════════
# 字符串专用的 n-gram / token 布隆过滤器
# ══════════════════════════════════════════════════════════════════════════


def _split_ngrams(text: str, n: int, pad: bool = True) -> List[bytes]:
    """把字符串切成**字节级**的 n-gram。

    .. code-block:: text

       n = 3,文本 "foobar"

       pad=True (写入时,也是 ``=`` 查询时用):
           数据 = \\x01 f o o b a r \\x02
           gram = \\x01fo, foo, oob, oba, bar, ar\\x02
                  ^^^^^                        ^^^^^
                  边界 gram(只有"整串就是它"时才会出现)

       pad=False(``LIKE '%…%'`` 的**片段**用它):
           gram = foo, oob, oba, bar
                  ↑ 只要求**内部** gram

    为什么要区分? 因为 ``LIKE '%foo%'`` 的 ``foo`` 是一个**子串**,
    它在数据里出现的形态是 ``…foo…`` —— 如果拿"补了哨兵的 foo"
    (``\\x01fo``/``oo\\x02``)去过滤器里问,那些边界 gram **永远不可能存在**,
    于是过滤器回答"没有",整个 granule 被跳过 →
    ``WHERE url LIKE '%foo%'`` **返回空集**(数据丢了)。

    所以规则是:

    * 写入 / ``=`` / ``IN``:整串,``pad=True``(与数据里的形态完全一致);
    * ``LIKE`` 的片段:``pad=False``(只要求内部 gram)。

    ``n`` 大于片段长度时返回空列表 —— 调用方据此"无法判断"从而放行。
    """
    data = text.encode("utf-8", "surrogateescape")
    if pad:
        data = b"\x01" + data + b"\x02"
    if n <= 0 or len(data) < n:
        return []
    return [data[i:i + n] for i in range(len(data) - n + 1)]


def _split_tokens(text: str) -> List[bytes]:
    """按**非字母数字**切词 —— 与真实 ClickHouse 的 ``tokenbf_v1`` 一致。

    例如 ``"http://example.com/path?a=1"`` 切成
    ``["http", "example", "com", "path", "a", "1"]``。

    这个切法直接决定了它能加速哪些查询:``tokenbf_v1`` 对
    `WHERE url LIKE '%example%'` 有效(example 是一个 token),
    但对 `WHERE url LIKE '%xample%'` **无效**(xample 不是完整的 token)。
    这是使用 ``tokenbf_v1`` 最容易踩的坑。
    """
    out: List[bytes] = []
    cur: List[str] = []
    for ch in text:
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur).encode("utf-8", "surrogateescape"))
                cur = []
    if cur:
        out.append("".join(cur).encode("utf-8", "surrogateescape"))
    return out


class StringBloomGranule(IndexGranule):
    """``ngrambf_v1`` / ``tokenbf_v1`` 共用的"用 LIKE 模式问过滤器"逻辑。

    .. code-block:: text

       三条查询路径,语义完全不同 —— 混起来就是 false negative:

         url = 'foo'
           整串相等 → 用**与写入完全相同**的键(补哨兵的 gram / 整串的 token)

         url LIKE '%foo%'
           foo 是**子串** → 只能要求:
             ngram: foo 的**内部** gram(不补哨兵)
             token: **只有当 foo 两边在模式里被非字母数字字符(或串首尾)
                    夹住时**,才能要求它的 token
                    ↑ 否则 'exa' 可能只是 'example' 的一部分,
                      而 token 过滤器里存的是 'example' → 问它就必然答"没有"

         url LIKE 'foo%'
           前缀 → 同上(两边不都被夹住 → token 过滤器放弃)
    """

    def __init__(self, filters: List[BloomFilter]) -> None:
        self.filters = filters

    def may_be_true(self, cond: IndexCondition) -> bool:
        if not self.filters:
            return True
        bf = self.filters[0]
        op = cond.op
        if op == "like" and isinstance(cond.value, str):
            for fragment, left_ok, right_ok in self._like_fragments(cond.value):
                r = self._fragment_might_be_true(bf, fragment, left_ok, right_ok)
                if r is False:
                    return False
            return True
        if op == "=" and isinstance(cond.value, str):
            return self._whole_value_might_be_true(bf, cond.value)
        if op == "in":
            for v in cond.value:
                if not isinstance(v, str):
                    continue
                if self._whole_value_might_be_true(bf, v):
                    return True
            return False
        # 其它条件(范围、数值比较)语义上无法用 token/n-gram 回答 → 放行
        return True

    # ── 子类实现 ────────────────────────────────────────────────────────

    def _like_fragments(self, pattern: str) -> List[Tuple[str, bool, bool]]:
        """LIKE 模式里"必须原样出现"的片段 + 两侧是否被夹住。

        两个子类抽的**不是同一批片段**,这是刻意的:

        * ``ngrambf_v1`` 靠"子串的 n-gram",所以抽**字面量整段**
          (含空格等分隔符,更精确);
        * ``tokenbf_v1`` 靠"完整 token",所以抽**字母数字串**,
          并判断它在模式里是否被非字母数字(或串首尾)夹住。
        """
        raise_error(ErrorCode.NOT_IMPLEMENTED, "subclass must implement")

    def _fragment_might_be_true(self, bf: BloomFilter, fragment: str,
                                left_ok: bool, right_ok: bool) -> bool:
        raise_error(ErrorCode.NOT_IMPLEMENTED, "subclass must implement")

    def _whole_value_might_be_true(self, bf: BloomFilter, value: str) -> bool:
        raise_error(ErrorCode.NOT_IMPLEMENTED, "subclass must implement")


def _like_literal_runs(pattern: str) -> List[str]:
    """LIKE 模式里被通配符隔开的**字面量整段**(``%``/``_`` 之外的最长连续段)。"""
    out: List[str] = []
    cur: List[str] = []
    for ch in pattern:
        if ch in "%_":
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _like_token_runs_with_anchors(pattern: str) -> List[Tuple[str, bool, bool]]:
    """LIKE 模式里"一定是完整 token"的字母数字串,以及它的左右是否被夹住。

    .. code-block:: text

       模式             抽出的串   左夹住  右夹住   能不能问 token 过滤器
       ───────────────  ─────────  ──────  ──────  ──────────────────────────
       '% tokenbf_v1'   tokenbf    ✗      ✗      ✗ 可能只是 tokenbf_v1x 的一部分
       '%foo%'          foo        ✗      ✗      ✗ 可能只是 foobar 的一部分
       '% example %'    example    ✓      ✓      ✓ 两边是空格 → 必是完整 token
       'foo%'           foo        ✓      ✗      ✗ 可能是 foobar 的前缀
       '%a-b%'          a, b       ✓      ✓      ✓ '-' 是非字母数字 → 完整 token

    "被夹住"的判据:**紧邻该串的模式元素是"字面量字符"**(它不在字母
    数字串里,所以必然是非字母数字),或者已经到**模式的首/尾**。
    紧邻的是 ``%`` 或 ``_`` 时不算 —— 它们能匹配字母数字,
    于是这个串可能落在**更大的 token 内部**。

    ★ 注意判据是"紧邻**串**的字符",而不是"紧邻**片段**的字符"。
    ``'% example %'`` 的字面量整段是 ``" example "``(带空格),
    但它的字母数字串是 ``example``,两侧紧邻的是**空格这个字面量字符**
    → 被夹住 → 可以放心地问 token 过滤器。
    早期版本按"整段的边界"判断,于是 ``LIKE '% example %'``
    完全用不上 tokenbf —— 过度保守,等于把 tokenbf 废掉了。
    """
    out: List[Tuple[str, bool, bool]] = []
    cur: List[str] = []
    left_ok = False
    prev_literal = False        # 紧邻的模式元素是字面量字符?
    for i, ch in enumerate(pattern):
        if ch in "%_":
            if cur:
                out.append(("".join(cur), left_ok, False))
                cur = []
            prev_literal = False
            continue
        if ch.isalnum():
            if not cur:
                # 新的字母数字串开始:串首 或 紧邻一个字面量字符 → 左边界成立
                left_ok = (i == 0) or prev_literal
            cur.append(ch)
            prev_literal = True
            continue
        # 非字母数字的**字面量**字符 —— 它是天然的分隔符
        if cur:
            out.append(("".join(cur), left_ok, True))
            cur = []
        prev_literal = True
    if cur:
        out.append(("".join(cur), left_ok, True))     # 模式结尾 → 右边界成立
    return out


class NgramBFGranule(StringBloomGranule):
    __slots__ = ("n",)

    def __init__(self, filters: List[BloomFilter], n: int) -> None:
        super().__init__(filters)
        self.n = n

    def _like_fragments(self, pattern: str) -> List[Tuple[str, bool, bool]]:
        # n-gram 只看"子串的内部 gram",所以用**字面量整段**(含分隔符),
        # 两个边界标志对它没有意义(恒为 True)。
        return [(f, True, True) for f in _like_literal_runs(pattern)]

    def _fragment_might_be_true(self, bf: BloomFilter, fragment: str,
                                left_ok: bool, right_ok: bool) -> bool:
        # LIKE 的片段只要求**内部** gram(不补 \\x01/\\x02 哨兵)。
        grams = _split_ngrams(fragment, self.n, pad=False)
        if not grams:
            return True     # 片段比 n 还短 → 什么也证明不了
        return all(bf.might_contain(g) for g in grams)

    def _whole_value_might_be_true(self, bf: BloomFilter, value: str) -> bool:
        grams = _split_ngrams(value, self.n, pad=True)
        if not grams:
            return True
        return all(bf.might_contain(g) for g in grams)


class TokenBFGranule(StringBloomGranule):
    __slots__ = ()

    def _like_fragments(self, pattern: str) -> List[Tuple[str, bool, bool]]:
        return _like_token_runs_with_anchors(pattern)

    def _fragment_might_be_true(self, bf: BloomFilter, fragment: str,
                                left_ok: bool, right_ok: bool) -> bool:
        # ★ 只有**两侧都被夹住**时,这个串才必然是数据里的完整 token。
        #   否则(比如 '%exa%')它可能是 'example' 的一部分 ——
        #   而过滤器里存的是 'example',问 'exa' 必然答"没有" → 会丢数据。
        if not (left_ok and right_ok):
            return True
        tokens = _split_tokens(fragment)
        if not tokens:
            return True
        return all(bf.might_contain(t) for t in tokens)

    def _whole_value_might_be_true(self, bf: BloomFilter, value: str) -> bool:
        tokens = _split_tokens(value)
        if not tokens:
            return True
        return all(bf.might_contain(t) for t in tokens)


class _StringBFIndexBase(IMergeTreeIndex):
    """n-gram / token 布隆过滤器共用的 build/deserialize。"""

    granularity_param_name = "size"

    def __init__(self, name: str, expr_sql: str, expr: Any,
                 params: Sequence[Any], granularity: int, data_type: DataType) -> None:
        super().__init__(name, expr_sql, expr, params, granularity, data_type)
        if not data_type.is_string():
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Index {name}: {self.index_type} only supports String/FixedString "
                f"columns, got {data_type.get_name()}",
            )
        self.size_bytes = int(params[0]) if len(params) > 0 else 256
        self.num_hashes = int(params[1]) if len(params) > 1 else 3
        self.seed = int(params[2]) if len(params) > 2 else 0
        if self.size_bytes <= 0:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Index {name}: {self.index_type} size must be positive",
            )

    def _iter_keys(self, value: str) -> List[bytes]:
        raise_error(ErrorCode.NOT_IMPLEMENTED, "subclass")

    def build(self, column: IColumn) -> bytes:
        bf = BloomFilter(self.size_bytes * 8, self.num_hashes, self.seed)
        for i in range(column.size()):
            v = column.get(i)
            if v is None:
                continue
            for key in self._iter_keys(str(v)):
                bf.add(key)
        out = bytearray()
        write_var_uint(out, 1)
        blob = bf.serialize()
        write_var_uint(out, len(blob))
        out += blob
        return bytes(out)

    def _make_granule(self, filters: List[BloomFilter]) -> StringBloomGranule:
        raise_error(ErrorCode.NOT_IMPLEMENTED, "subclass")

    def deserialize(self, data: bytes) -> StringBloomGranule:
        if not data:
            return self._make_granule([])
        count, pos = read_var_uint(data, 0)
        filters: List[BloomFilter] = []
        for _ in range(count):
            ln, pos = read_var_uint(data, pos)
            filters.append(BloomFilter.deserialize(data[pos:pos + ln]))
            pos += ln
        return self._make_granule(filters)


class NgramBFIndex(_StringBFIndexBase):
    """``TYPE ngrambf_v1(n, size, hashes, seed)`` —— 字节级 n-gram 布隆过滤器。

    对 ``LIKE '%substr%'`` 特别有效:它把**子串**拆成 n-gram 存下来,
    所以"部分匹配"也能被它回答。这是它和 ``tokenbf_v1`` 的核心区别。
    """

    index_type = "ngrambf_v1"

    def __init__(self, name: str, expr_sql: str, expr: Any,
                 params: Sequence[Any], granularity: int, data_type: DataType) -> None:
        if len(params) < 4:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"ngrambf_v1 requires (n, size, hashes, seed), got {list(params)}",
            )
        self.n = int(params[0])
        super().__init__(name, expr_sql, expr, [params[1], params[2], params[3]],
                         granularity, data_type)
        if self.n <= 0:
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, "ngrambf_v1: n must be positive")

    def _iter_keys(self, value: str) -> List[bytes]:
        return _split_ngrams(value, self.n)

    def _make_granule(self, filters: List[BloomFilter]) -> StringBloomGranule:
        return NgramBFGranule(filters, self.n)


class TokenBFIndex(_StringBFIndexBase):
    """``TYPE tokenbf_v1(size, hashes, seed)`` —— 按 token 的布隆过滤器。

    它存的是"**完整的 token**",所以:

    * ``WHERE url LIKE '%example%'``  → 有效(example 是 token)
    * ``WHERE url LIKE '%xample%'``   → 无效(xample 不是 token,只能放行)
    * ``WHERE hasToken(url, 'example')`` → 有效(语义完全对上)
    """

    index_type = "tokenbf_v1"

    def __init__(self, name: str, expr_sql: str, expr: Any,
                 params: Sequence[Any], granularity: int, data_type: DataType) -> None:
        if len(params) < 3:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"tokenbf_v1 requires (size, hashes, seed), got {list(params)}",
            )
        super().__init__(name, expr_sql, expr, params, granularity, data_type)

    def _iter_keys(self, value: str) -> List[bytes]:
        return _split_tokens(value)

    def _make_granule(self, filters: List[BloomFilter]) -> StringBloomGranule:
        return TokenBFGranule(filters)


# ══════════════════════════════════════════════════════════════════════════
# 工厂
# ══════════════════════════════════════════════════════════════════════════

#: 索引类型名(小写)→ 类。
_INDEX_TYPES: Dict[str, type] = {
    "minmax": MinMaxIndex,
    "set": SetIndex,
    "bloom_filter": BloomFilterIndex,
    "ngrambf_v1": NgramBFIndex,
    "tokenbf_v1": TokenBFIndex,
}


def create_index(
    name: str,
    index_type: str,
    expr_sql: str,
    expr: Any,
    params: Sequence[Any],
    granularity: int,
    data_type: DataType,
) -> IMergeTreeIndex:
    """按 ``TYPE`` 名字建一个索引实例。"""
    key = index_type.lower()
    cls = _INDEX_TYPES.get(key)
    if cls is None:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Unknown skip index type {index_type!r}. "
            f"Known: {', '.join(sorted(_INDEX_TYPES))}",
        )
    return cls(name, expr_sql, expr, params, granularity, data_type)


def supported_index_types() -> List[str]:
    return sorted(_INDEX_TYPES)


# ══════════════════════════════════════════════════════════════════════════
# 描述与工具
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class IndexDescription:
    """建表时写的一条 ``INDEX ... TYPE ... GRANULARITY ...``。"""

    name: str
    expr_sql: str
    expr: Any
    index_type: str
    params: List[Any] = field(default_factory=list)
    granularity: int = 1
    #: 索引表达式的结果类型(建表时由解释器填入;取不到就按 String 兜底)
    data_type: Optional[DataType] = None

    def build_instance(self) -> IMergeTreeIndex:
        dtype = self.data_type if self.data_type is not None else get_data_type("String")
        return create_index(self.name, self.index_type, self.expr_sql, self.expr,
                            self.params, self.granularity, dtype)


class IndexHelper:
    """一组跳数索引的**构建器**与**读取器**。

    真实 ClickHouse 里对应的是一对类:``MergeTreeIndexFactory`` +
    ``MergeTreeIndexBuilder``(写)与 ``MergeTreeIndexReader``(读)。
    我们把两者合并成一个,因为它们的共同状态只有"索引列表 + 粒度"。

    .. code-block:: text

       写:  block(整个 part) ──► 按粒度组切 ──► 每个索引各自 build ──►
            skp_idx_<name>.idx =  [组0 长度][组0 字节][组1 长度][组1 字节] …

       读:  skp_idx_<name>.idx ──► 反序列化每个组 ──►
            对每个 (mark_range ∩ 组范围) 调 may_be_true ──► 能跳掉的 mark 集合
    """

    def __init__(self, descriptions: Sequence[IndexDescription]) -> None:
        self.descriptions = list(descriptions)
        self.indices: List[IMergeTreeIndex] = [d.build_instance() for d in self.descriptions]

    # ── 写 ──────────────────────────────────────────────────────────────

    def build_all(
        self,
        block: Block,
        index_granularity: Any,
    ) -> Dict[str, bytes]:
        """对整块的每一列索引表达式求值,并按粒度组写出 ``skp_idx_<name>.idx``。

        返回 ``{索引名: 文件字节}``。

        .. code-block:: text

           ★ 粒度组必须落在**真实的 granule 边界**上

              ``GRANULARITY 1``  → 一个 granule 一组
              ``GRANULARITY 2``  → 每两个 granule 一组
              ``GRANULARITY 10`` → 每十个 granule 一组

           注意是"**几个 granule** 一组",不是"多少行一组"。
           这个区别在自适应粒度下是致命的:granule 的行数**不是常数**
           (每个 granule 可能 1024 行,也可能只有 3 行),
           所以用"平均行数"去推组的边界,组就会与 granule **错位** ——
           某一行被记录进了 A 组,查询时却按 B 组去问,
           于是"这一组里没有这个值"这个回答是**错的**,
           整个 granule 被跳过 → **丢数据**。

           所以这里按 granule 的**序号**切组,边界永远是精确的。
        """
        from .expression import evaluate_expression

        granularities = _granule_rows(index_granularity)
        out: Dict[str, bytes] = {}
        for idx in self.indices:
            col = evaluate_expression(idx.expr, block).column
            if col.is_const():
                col = col.convert_to_full_column()
            out[idx.name] = _build_groups(idx, col, granularities, idx.granularity)
        return out

    # ── 读 ──────────────────────────────────────────────────────────────

    def load(self, name: str, data: bytes,
             group_granularity: int = 1) -> "MergeTreeIndexReader":
        """从 ``skp_idx_<name>.idx`` 的字节里恢复出每个粒度组的摘要。

        ``group_granularity`` 是索引的 ``GRANULARITY``(几个 granule 一组),
        读取方靠它 + part 的 ``IndexGranularity`` 精确地算出
        "第 m 个 mark 属于第几组"。
        """
        idx = next((i for i in self.indices if i.name == name), None)
        if idx is None:
            raise_error(ErrorCode.LOGICAL_ERROR, f"No index named {name!r} is described")
        granules: List[IndexGranule] = []
        pos = 0
        while pos < len(data):
            ln, pos = read_var_uint(data, pos)
            granules.append(idx.deserialize(data[pos:pos + ln]))
            pos += ln
        return MergeTreeIndexReader(idx, granules, group_granularity, name)


def _granule_rows(index_granularity: Any) -> List[int]:
    """从 IndexGranularity 取出"每个 granule 有多少行"。

    这是跳数索引**唯一**可以依赖的分组依据 —— 组 = 连续的若干 granule。
    """
    gran = getattr(index_granularity, "granularities", None)
    if gran:
        return list(gran)
    if isinstance(index_granularity, int):
        total = getattr(index_granularity, "total_rows", None)
        if total is not None:
            n = max(1, (total + index_granularity - 1) // index_granularity)
            return [index_granularity] * max(0, n - 1) + \
                ([total - index_granularity * (n - 1)] if total % index_granularity else [])
        return []
    total = getattr(index_granularity, "total_rows", 0)
    g = getattr(index_granularity, "index_granularity", 8192)
    if total:
        n = (total + g - 1) // g
        return [g] * max(0, n - 1) + \
            ([total - g * (n - 1)] if total % g else [])
    return []


def _build_groups(index: IMergeTreeIndex, column: IColumn,
                  granularities: Sequence[int], group_granularity: int) -> bytes:
    """按"每 ``group_granularity`` 个 granule 一组"写出索引文件。

    组边界 = **granule 边界**的整除点。因为 granule 行数会变
    (自适应粒度),所以这里**绝不能**用"平均行数 × 组大小"来推边界。
    """
    out = bytearray()
    if not granularities:
        # 拿不到 granularity(比如直接调 IndexHelper.build_all 传了个整数)——
        # 退化成"整块一组"。保守但**正确**:查询时组数对不上会让
        # may_be_true 一律放行,而不是误判为"没有"。
        blob = index.build(column)
        write_var_uint(out, len(blob))
        out += blob
        return bytes(out)

    group_granularity = max(1, int(group_granularity))
    starts: List[int] = []
    acc = 0
    for g in granularities:
        starts.append(acc)
        acc += g
    n = len(granularities)
    gi = 0
    while gi < n:
        first_row = starts[gi]
        last_granule = min(n, gi + group_granularity)
        end_row = starts[last_granule - 1] + granularities[last_granule - 1]
        sub = column.clone_empty()
        sub.insert_range_from(column, first_row, end_row - first_row)
        blob = index.build(sub)
        write_var_uint(out, len(blob))
        out += blob
        gi = last_granule
    return bytes(out)


class MergeTreeIndexReader:
    """按 mark 区间询问一组跳数索引 —— 读路径上"能不能跳过"的裁决者。

    ``group_granularity`` 是索引的 ``GRANULARITY``(几个 granule 一组)。
    mark ``m`` 属于第 ``m // group_granularity`` 组 —— 这个映射是
    **精确**的,不依赖"granule 有多少行",所以自适应粒度下也不会错位。
    """

    def __init__(self, index: IMergeTreeIndex, granules: List[IndexGranule],
                 group_granularity: int = 1, name: str = "") -> None:
        self.index = index
        self.granules = granules
        self.group_granularity = max(1, int(group_granularity))
        self.name = name or index.name

    def group_count(self) -> int:
        return len(self.granules)

    def group_for_mark(self, mark: int) -> int:
        """mark 号 → 它属于哪个索引粒度组。"""
        return mark // self.group_granularity

    def may_be_true_for_mark_range(
        self,
        mark_range: MarkRange,
        conds: Sequence[IndexCondition],
        index_granularity: Any = None,
    ) -> bool:
        """这段 mark 里**有没有可能**存在满足任一条件的行。

        多个条件是 ``OR`` 关系(任一为真就可能);条件是 ``AND`` 时
        调用方应分别判定。真实 ClickHouse 里一组 ``IndexCondition``
        的语义就是"析取"(``IndexCondition::mayBeTrueInRange`` 把
        ``=`` 展开成多个值也是这个道理)。

        mark → 组的映射用的是 ``mark // group_granularity``,
        与构建时"每 N 个 granule 一组"**完全对称** —— 这一点必须严格
        成立,否则就是"记进了 A 组、问的是 B 组",会产生
        **false negative(丢数据)**。
        """
        if not conds:
            return True
        if not self.granules:
            return True
        end = mark_range.end
        if end == (1 << 64) - 1:
            end = (len(self.granules) * self.group_granularity)
        if end <= mark_range.begin:
            return False
        first = max(0, mark_range.begin // self.group_granularity)
        last = min(len(self.granules),
                   (end - 1) // self.group_granularity + 1)
        for g in range(first, last):
            granule = self.granules[g]
            if any(granule.may_be_true(c) for c in conds):
                return True
        return False

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MergeTreeIndexReader {self.name} groups={len(self.granules)}>"


# ══════════════════════════════════════════════════════════════════════════
# 从 WHERE 里抽条件
# ══════════════════════════════════════════════════════════════════════════

_COMPARISON_OPS = {"=", "==", "!=", "<>", "<", "<=", ">", ">="}


def collect_index_conditions(expr: Any, index_expr_sql: str,
                             data_type: Any = None) -> List[IndexCondition]:
    """从 ``WHERE`` 里抽出所有"关于这个索引表达式"的**必要条件**。

    .. code-block:: text

       WHERE a=1 AND b<2 AND (c=3 OR d=4) AND url='x'
               │        │          │              │
               ▼        ▼          ▼              ▼
            收集     收集     **丢弃**(OR 分支)  收集

    **为什么 OR 分支必须整个丢掉?** 因为从 ``X OR Y`` 里只抽 ``X``
    是不安全的:一个满足 ``Y`` 的行可能不满足 ``X``,如果我们按
    "index 说没有满足 X 的行 → 跳过这个 granule",就会漏掉那些
    "因为 Y 而匹配"的行。所以遇到 OR **一条都不收集** ——
    丢掉的只是优化机会,不是正确性。

    这就是跳过索引的第二条纪律:**宁可少跳,不可错跳。**

    .. warning::

       **``data_type`` 不能省。** 索引列存的是**内部表示**:
       ``Date`` 是天数(``19726``)、``DateTime`` 是秒、``Decimal`` 是
       未缩放的整数。而 ``WHERE d >= '2024-01-04'`` 送来的是**字符串**。
       不归一化的话:

       .. code-block:: text

          19726 >= '2024-01-04'   →  Python 抛 TypeError
                                  →  _cmp 返回 None(比不了)
                                  →  现在的实现放行(安全),但**裁剪失效**
          而且 bloom_filter 更糟:它哈希的是**字节**,
          b'19726' != b'2024-01-04' → 过滤器答"没有" → **丢数据**

       所以这里用索引表达式的**结果类型**把字面量归一化;
       归一化失败的条件直接**丢弃**(不裁剪,但绝不误裁)。
    """
    from ...sql import ast_ as A

    conds: List[IndexCondition] = []

    def same(node: Any) -> bool:
        try:
            return node is not None and node.to_sql() == index_expr_sql
        except Exception:  # pragma: no cover
            return False

    def norm(v: Any) -> Tuple[Any, bool]:
        """归一化一个条件值;``(值, 是否可用)``。"""
        if data_type is None:
            return v, True
        try:
            return data_type.normalize(v), True
        except Exception:
            return v, False

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, A.BinaryOp) and node.op.upper() == "AND":
            walk(node.left)
            walk(node.right)
            return
        if isinstance(node, A.BinaryOp) and node.op in _COMPARISON_OPS:
            if same(node.left) and same(node.right):
                return
            if same(node.left):
                v = _const_value(node.right)
                if v is not _NO_VALUE:
                    v, ok = norm(v)
                    if ok:
                        conds.append(IndexCondition(node.op, v))
            elif same(node.right):
                v = _const_value(node.left)
                if v is not _NO_VALUE:
                    v, ok = norm(v)
                    if ok:
                        conds.append(IndexCondition(node.op, v).swapped())
            return
        if isinstance(node, A.InExpr) and not node.negated:
            if same(node.left):
                values = _const_list(node.right)
                if values is not None:
                    normed = [norm(v) for v in values]
                    if all(ok for _, ok in normed):
                        conds.append(IndexCondition("in", [v for v, _ in normed]))
            return
        if isinstance(node, A.BetweenExpr) and not node.negated:
            if same(node.expr):
                lo = _const_value(node.low)
                hi = _const_value(node.high)
                if lo is not _NO_VALUE and hi is not _NO_VALUE:
                    lo, ok1 = norm(lo)
                    hi, ok2 = norm(hi)
                    if ok1 and ok2:
                        conds.append(IndexCondition("between", lo, hi))
            return
        if isinstance(node, A.LikeExpr) and not node.negated:
            if same(node.left):
                pat = _const_value(node.pattern)
                if isinstance(pat, str):
                    conds.append(IndexCondition("like", pat))
            return
        # OR / 函数 / 其它一切 —— 丢弃(安全)
        return

    walk(expr)
    return conds


class _NoValue:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<no-value>"


_NO_VALUE = _NoValue()


def _const_value(node: Any) -> Any:
    """从一个"应该是常量"的 AST 节点取出值。

    .. warning::

       **``evaluate_constant`` 返回 ``None`` 意味着"折不了",必须当成
       未知值(:data:`_NO_VALUE`),而不是"值为 ``None`` 的常量"。**

       这么写是有具体教训的:``abs(-10)`` / ``toFloat64(10)`` / ``round(1.6)``
       / ``length('abc')`` / ``toInt8(10)`` 这些右值,``evaluate_constant``
       当前都折不出结果、返回 ``None``。若把 ``None`` 当真实常量送进
       :class:`IndexCondition`,minmax 在 ``_cmp`` 里会把 ``None`` 当成一个
       **极值**参与比较(``_cmp`` 的 NULL 语义),于是判成"没有任何 mark
       匹配" → **整表被跳空**(假阴性、丢行)。

       放进 :data:`_NO_VALUE` 后,条件根本不会被收集,各索引类型
       (minmax / set / bloom_filter …)**一律保守放行** —— 这正是
       "比较不了就放行"这条纪律要求的。纯字面量/``toInt32`` 等能正常
       折叠的右值不受影响。
    """
    from ...sql import ast_ as A
    from .expression import evaluate_constant

    if node is None:
        return _NO_VALUE
    if isinstance(node, A.Literal):
        return node.value
    if isinstance(node, A.TupleLiteral):
        # ``(a, b) = (1, 2)`` 这类元组索引的条件值 —— 必须是 Python 元组
        vals = []
        for item in node.items:
            v = _const_value(item)
            if v is _NO_VALUE:
                return _NO_VALUE
            vals.append(v)
        return tuple(vals)
    if isinstance(node, A.CastExpr):
        try:
            v = evaluate_constant(node)
        except Exception:
            return _NO_VALUE
        # 折不了 → None → 未知值(见上面的 warning)
        return _NO_VALUE if v is None else v
    if isinstance(node, A.FunctionCall):
        try:
            v = evaluate_constant(node)
        except Exception:
            return _NO_VALUE
        return _NO_VALUE if v is None else v
    if isinstance(node, A.UnaryOp) and node.op == "-":
        inner = _const_value(node.operand)
        if inner is _NO_VALUE:
            return _NO_VALUE
        try:
            return -inner
        except Exception:
            return _NO_VALUE
    return _NO_VALUE


def _const_list(node: Any) -> Optional[List[Any]]:
    from ...sql import ast_ as A

    if node is None:
        return None
    if isinstance(node, A.ArrayLiteral):
        out = []
        for item in node.items:
            v = _const_value(item)
            if v is _NO_VALUE:
                return None
            out.append(v)
        return out
    if isinstance(node, A.TupleLiteral):
        return _const_list_from_items(node.items)
    if isinstance(node, A.Literal) and isinstance(node.value, (list, tuple)):
        return list(node.value)
    if isinstance(node, A.FunctionCall) and node.name.lower() in ("array", "tuple"):
        return _const_list_from_items(node.args)
    return None


def _const_list_from_items(items: Sequence[Any]) -> Optional[List[Any]]:
    out = []
    for item in items:
        v = _const_value(item)
        if v is _NO_VALUE:
            return None
        out.append(v)
    return out
