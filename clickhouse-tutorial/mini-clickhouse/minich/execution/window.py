"""窗口函数 —— ``OVER (PARTITION BY ... ORDER BY ... ROWS/RANGE ...)``。

窗口函数在 ClickHouse 里是"**用得不多但一旦需要就不可替代**"的一类:
它让你在同一次扫描里既看到"当前行"又看到"这一组的聚合值"。

.. code-block:: sql

   -- 每个用户按时间排序,取最近 3 次登录的间隔
   SELECT user_id, ts,
          ts - lagInFrame(ts) OVER (PARTITION BY user_id ORDER BY ts) AS gap
   FROM logins;

**执行需要两遍**(所以它是 blocking operator):

.. code-block:: text

   ① 分区 + 排序:按 PARTITION BY 分桶、按 ORDER BY 排好
   ② 逐分区扫一遍,按帧(frame)算出每个位置的值

第二遍能不能做到 O(n) 取决于**帧的类型**:

===========================  ==========================  ==============
帧                           含义                        能否增量
===========================  ==========================  ==============
``ROWS BETWEEN UNBOUNDED     累计到当前行(最常见的        ✅ 前缀和
  PRECEDING AND CURRENT ROW`` "累计到当前")
``ROWS BETWEEN 2 PRECEDING    滑动窗口                     🟡 加减法
  AND CURRENT ROW``                                        (可增量)
``ROWS BETWEEN UNBOUNDED      整个分区                     ✅ 一趟算完
  PRECEDING AND UNBOUNDED
  FOLLOWING``
``RANGE ...``                按**值**而不是按行         ⚠️ 需要处理并列值
===========================  ==========================  ==============

真实 ClickHouse 的 ``WindowTransform`` 会为"可增量"的帧做专门优化,
并且在**数据已经按 PARTITION BY, ORDER BY 有序**时跳过排序
(这是窗口函数最常见的性能问题:一个 ``ORDER BY`` 冲突就能让
整个优化失效)。mini-clickhouse 不做这两项优化 —— 一律全排 + 逐位置扫。
第 14 章会给出量化对比。

.. warning::

   **``RANGE`` 与 ``ROWS`` 的差别在"并列值"上**。假设
   ``ORDER BY k`` 里有两行 ``k = 5``:

   * ``ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`` —— 这两行
     得到**不同**的累计值(它们是两行);
   * ``RANGE ...`` —— 这两行得到**相同**的累计值(它们"同时"到达)。

   SQL 标准里"没有 ORDER BY 时默认帧是整个分区,有 ORDER BY 时是
   ``RANGE UNBOUNDED PRECEDING``"。mini-clickhouse **把 RANGE 当成
   ROWS 处理** —— 所以在有并列值的数据上会与真实 ClickHouse 不一致。
   这个差异写在下面 :func:`_frame_bounds` 的注释里,教程第 14 章有例子。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..columns import ColumnConst, IColumn
from ..errors import ErrorCode, MiniChError, raise_error
from ..sql import ast_ as A
from .expression import Evaluator, Scope

__all__ = ["execute_window_functions", "RANKING_FUNCTIONS", "OFFSET_FUNCTIONS"]

#: 排名类窗口函数 —— 它们不需要"帧",只需要分区内有序。
RANKING_FUNCTIONS = frozenset({
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile",
})

#: 偏移类窗口函数 —— 取"相对当前行某个位置"的值。
OFFSET_FUNCTIONS = frozenset({
    "lag", "lead", "laginframe", "leadinframe", "first_value", "last_value",
    "nth_value", "first_value_respect_nulls", "last_value_respect_nulls",
})

#: 能在窗口上使用的聚合函数(集合比普通聚合小得多 ——
#: 因为窗口聚合必须支持"部分区间重算",而像 ``uniq`` 这种
#: 近似算法的状态是**不可减的**)。
WINDOW_AGGREGATES = frozenset({
    "sum", "avg", "count", "min", "max", "any", "anylast",
    "stddevpop", "stddevsamp", "varpop", "varsamp",
    "grouparray", "grouparraylast", "sumkahan", "median",
})


def execute_window_functions(
    ctx: Any,
    window_functions: Sequence[Tuple[A.WindowFunction, str]],
    blocks: Sequence[Block],
    scope_columns: Optional[Dict[str, ColumnWithTypeAndName]] = None,
) -> Optional[Chunk]:
    """对一批 block 计算所有窗口函数,返回**追加了结果列**的 Chunk。"""
    if not blocks:
        return None
    merged = blocks[0].clone_empty()
    for b in blocks:
        if merged.is_empty():
            merged = b.clone_empty()
        merged.merge(b)
    rows = merged.rows()
    if rows == 0:
        return None

    scope_columns = dict(scope_columns or {})
    scope = Scope(scope_columns)
    for c in merged.data:
        scope.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, c.column))

    out_block = merged.clone_with_columns()
    for wf, out_name in window_functions:
        column = _compute_one(ctx, wf, merged, scope, rows)
        dtype = _infer_result_type(wf, merged, scope, ctx, rows)
        out_block.add_column(ColumnWithTypeAndName(out_name, dtype, column))
    return Chunk(out_block.clone_empty(),
                 [c.column for c in out_block.data], out_block.rows())


def _compute_one(
    ctx: Any, wf: A.WindowFunction, block: Block, scope: Scope, rows: int
) -> IColumn:
    func = wf.function
    if not isinstance(func, A.FunctionCall):
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Only function calls can be used with OVER, got "
            f"{func.to_sql() if hasattr(func, 'to_sql') else func}",
        )
    name = func.name.lower()
    spec = wf.window or A.WindowSpec()

    partitions = _partition(block, spec, scope, ctx, rows)
    out_values: List[Any] = [None] * rows

    for part in partitions:
        for slot, row in enumerate(part):
            value = _value_at(name, func, spec, part, slot, block, scope, ctx, rows)
            out_values[row] = value

    return _column_from_typed(out_values, _infer_result_type(wf, block, scope, ctx, rows))


def _partition(
    block: Block, spec: A.WindowSpec, scope: Scope, ctx: Any, rows: int
) -> List[List[int]]:
    """把行号按 ``PARTITION BY`` 分组,组内按 ``ORDER BY`` 排序。

    返回的是**行号列表的列表** —— 这样后续计算只处理行号,
    不用搬数据(与真实 ClickHouse 的 "permutation" 思路一致)。
    """
    ev = Evaluator(ctx, scope, rows)
    if spec.partition_by:
        key_cols = [ev.evaluate(e).column for e in spec.partition_by]
        buckets: Dict[Any, List[int]] = {}
        for row in range(rows):
            key = tuple(_hashable(c.get(row)) for c in key_cols)
            buckets.setdefault(key, []).append(row)
        groups = list(buckets.values())
    else:
        groups = [list(range(rows))]

    if not spec.order_by:
        return groups

    order_cols = []
    reverse = []
    nulls_first = []
    for e in spec.order_by:
        order_cols.append(ev.evaluate(e.expr).column)
        reverse.append(e.direction == "DESC")
        # NULL 的位置与引擎自己的 ``ORDER BY`` 保持**完全一致**:
        # 不写修饰词时,``ASC`` 把 NULL 放最后、``DESC`` 放最前
        # (等价于"NULL 被当作最大的值")。显式的 ``NULLS FIRST/LAST``
        # 覆盖这个默认 —— 与 processors.py 里的排序键是同一套规则。
        if e.nulls == "FIRST":
            nulls_first.append(True)
        elif e.nulls == "LAST":
            nulls_first.append(False)
        else:
            nulls_first.append(e.direction == "DESC")

    out: List[List[int]] = []
    for group in groups:
        indexes = list(group)
        for c in range(len(order_cols) - 1, -1, -1):
            col = order_cols[c]
            rev = reverse[c]
            nf = nulls_first[c]
            indexes.sort(
                key=lambda i, col=col, rev=rev, nf=nf: _SortKey(col.get(i), rev, nf)
            )
        out.append(indexes)
    return out


class _SortKey:
    """窗口排序用的键 —— NULL 的位置由 ``nulls_first`` 决定(与方向无关)。

    .. warning::

       这里必须和 :class:`minich.execution.processors._SortKey` 用**同一套**
       NULL 规则,否则 ``ORDER BY v`` 与 ``row_number() OVER (ORDER BY v)``
       会把 NULL 排到不同的位置:

       * 不写修饰词时,NULL 相当于"最大的值",``ASC`` → NULL 在最后、
         ``DESC`` → NULL 在最前(即 ``nulls_first = (direction == "DESC")``);
       * 早先的实现把 ASC/DESC 下的 NULL 顺序写反了(ASC 把 NULL 排到最前),
         于是 ``row_number() OVER (ORDER BY v)`` 给 NULL 的排名与 ClickHouse 相反。
    """

    __slots__ = ("value", "is_null", "reverse", "nulls_first")

    def __init__(self, value: Any, reverse: bool, nulls_first: bool) -> None:
        self.is_null = value is None
        self.value = value
        self.reverse = reverse
        self.nulls_first = nulls_first

    def __lt__(self, other: "_SortKey") -> bool:
        if self.is_null or other.is_null:
            if self.is_null and other.is_null:
                return False
            # NULL 的位置只由 nulls_first 决定,与 reverse 无关
            return self.is_null if self.nulls_first else not self.is_null
        if self.value == other.value:
            # 相等绝不能取反 —— 否则 x < x 成立,会打乱稳定排序
            return False
        result = self.value < other.value
        return (not result) if self.reverse else result


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


def _value_at(
    name: str,
    func: A.FunctionCall,
    spec: A.WindowSpec,
    part: List[int],
    slot: int,
    block: Block,
    scope: Scope,
    ctx: Any,
    rows: int,
) -> Any:
    """算出分区内第 ``slot`` 个位置上的窗口函数值。"""
    ev = Evaluator(ctx, scope, rows)
    row = part[slot]

    # ── 排名类 ──────────────────────────────────────────────────────────
    if name == "row_number":
        return slot + 1
    if name == "rank":
        # 有并列时跳号:1, 1, 3
        if slot == 0:
            return 1
        prev = _order_tuple(spec, part[slot - 1], block, ev)
        cur = _order_tuple(spec, part[slot], block, ev)
        if cur == prev:
            return _rank_cache(part, slot, block, ev, spec)[slot]
        return slot + 1
    if name == "dense_rank":
        return _dense_rank(part, slot, block, ev, spec)
    if name == "percent_rank":
        # ``percent_rank`` 用的是 **rank(有并列就跳号)**,不是行位置:
        #   (rank - 1) / (n - 1)
        # ``ORDER BY x``,x = 1, 1, 2 → rank = 1, 1, 3 → (0.0, 0.0, 1.0)。
        # 早先的实现写成 ``slot / (n - 1)``,把并列行算成了 (0.0, 0.5, 1.0)。
        n = len(part)
        if n <= 1:
            return 0.0
        rank = _rank_cache(part, slot, block, ev, spec)[slot]
        return (rank - 1) / (n - 1)
    if name == "cume_dist":
        n = len(part)
        if not n:
            return 0.0
        # "值 ≤ 当前值的行数 / 总行数" —— 这里的 "≤" 是按 **ORDER BY 的顺序**
        # 定义的,而 ``part`` 已经排好序,所以"排到当前行(含并列)为止"的行数
        # 就是当前行所在**并列组**的结束位置 + 1:从 ``slot`` 往后数到第一个
        # 排序键不同的行为止。
        #
        # 这样写有两点好处:① 自动尊重 ASC/DESC;
        # ② **不会拿 None 去做大小比较** —— 早先的实现直接
        # ``_order_tuple(...) <= cur``,一旦 ORDER BY 列含 NULL 就抛
        # ``TypeError: '<=' not supported between instances of 'NoneType' and 'int'``。
        # NULL 在这里天然被当作"最大的值":并列的 NULL 自成一组、排在末尾。
        cur = _order_tuple(spec, part[slot], block, ev)
        count = slot + 1
        for j in range(slot + 1, n):
            if _order_tuple(spec, part[j], block, ev) != cur:
                break
            count += 1
        return count / n
    if name == "ntile":
        buckets = _int_arg(func, 0, 1)
        n = len(part)
        if buckets <= 0:
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND, "ntile() requires a positive argument")
        base = n // buckets
        rem = n % buckets
        # 前面的桶多分一行 —— 与 SQL 标准一致
        bigger = rem * (base + 1)
        return (slot // (base + 1) + 1) if slot < bigger else \
            (rem + (slot - bigger) // base + 1 if base else buckets)

    # ── 偏移类 ──────────────────────────────────────────────────────────
    if name in ("lag", "laginframe", "lead", "leadinframe"):
        offset = _int_arg(func, 1, 1)
        default = _const_arg(func, 2)
        direction = -1 if name.startswith("lag") else 1
        target = slot + direction * offset
        if name in ("laginframe", "leadinframe"):
            # ``*InFrame`` 变体**尊重窗口帧**:目标行落在帧外 → 默认值。
            # 有 ORDER BY 时默认帧是 ``[0, slot]``,右边界就是当前行,所以
            # ``leadInFrame`` 在默认帧下**永远**拿不到下一行 —— 这是真实
            # ClickHouse 的行为(见教程 §16.10)。
            start, end = _frame_bounds(spec, slot, len(part))
        else:
            # 普通 ``lag``/``lead`` **无视窗口帧**:永远按**整个分区**取行
            # (等价于帧 [UNBOUNDED PRECEDING, UNBOUNDED FOLLOWING])。
            # 真实 CH 的 C++ 实现用一个 ``full_partition_default_frame``
            # 模板参数区分这两族;把普通 lag/lead 也夹到帧里会让
            # ``lead(x) OVER (ORDER BY x)`` 永远返回默认值。
            start, end = 0, len(part) - 1
        if target < start or target > end:
            return default
        return _arg_value(func, 0, part[target], block, ev)
    if name in ("first_value", "first_value_respect_nulls"):
        start, end = _frame_bounds(spec, slot, len(part))
        if end < start:          # 空帧 → 没有"第一个值"
            return None
        if name == "first_value_respect_nulls":
            return _arg_value(func, 0, part[start], block, ev)
        # 默认的 ``first_value`` **忽略 NULL**(真实 CH:``first_value`` 是
        # ``any`` 的别名,默认 IGNORE NULLS),所以从帧头往后找第一个非 NULL。
        for i in range(start, end + 1):
            v = _arg_value(func, 0, part[i], block, ev)
            if v is not None:
                return v
        return None
    if name in ("last_value", "last_value_respect_nulls"):
        start, end = _frame_bounds(spec, slot, len(part))
        if end < start:          # 空帧 → 没有"最后一个值"
            return None
        if name == "last_value_respect_nulls":
            return _arg_value(func, 0, part[end], block, ev)
        # 默认的 ``last_value`` 同样忽略 NULL(它是 ``anyLast`` 的别名)。
        for i in range(end, start - 1, -1):
            v = _arg_value(func, 0, part[i], block, ev)
            if v is not None:
                return v
        return None
    if name == "nth_value":
        n = _int_arg(func, 1, 1)
        # 取的是**帧内**第 n 个位置的值(不是整个分区的第 n 行);帧内不足 n 行
        # 或 n < 1 时不报错,返回 NULL(真实 ClickHouse 的行为)。
        # 早先的实现返回 ``part[n-1]``(整分区第 n 行),让每一行都拿到同一个值。
        start, end = _frame_bounds(spec, slot, len(part))
        target = start + n - 1
        if n < 1 or target > end:
            return None
        return _arg_value(func, 0, part[target], block, ev)

    # ── 聚合类 ──────────────────────────────────────────────────────────
    if name not in WINDOW_AGGREGATES:
        raise_error(
            ErrorCode.UNKNOWN_FUNCTION,
            f"Function {func.name} is not supported as a window function",
        )
    start, end = _frame_bounds(spec, slot, len(part))
    values = [_arg_value(func, 0, part[i], block, ev) for i in range(start, end + 1)]
    return _aggregate(name, values, func, block, ev, part, start, end)


def _order_tuple(spec: A.WindowSpec, row: int, block: Block, ev: Evaluator) -> tuple:
    out = []
    for e in spec.order_by:
        out.append(_hashable(ev.evaluate(e.expr).column.get(row)))
    return tuple(out)


def _rank_cache(part: List[int], slot: int, block: Block, ev: Evaluator,
                spec: A.WindowSpec) -> List[int]:
    ranks: List[int] = []
    current_rank = 1
    for i in range(len(part)):
        if i == 0:
            ranks.append(1)
            continue
        if _order_tuple(spec, part[i], block, ev) == _order_tuple(spec, part[i - 1], block, ev):
            ranks.append(ranks[-1])
        else:
            ranks.append(i + 1)
    return ranks


def _dense_rank(part: List[int], slot: int, block: Block, ev: Evaluator,
                spec: A.WindowSpec) -> int:
    rank = 1
    for i in range(1, slot + 1):
        if _order_tuple(spec, part[i], block, ev) != _order_tuple(spec, part[i - 1], block, ev):
            rank += 1
    return rank


def _frame_bounds(spec: A.WindowSpec, slot: int, n: int) -> Tuple[int, int]:
    """算出当前行的帧 ``[start, end]``(闭区间)。

    **默认帧**(SQL 标准):

    * 有 ``ORDER BY`` → ``RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW``;
    * 没有 ``ORDER BY`` → 整个分区。

    .. warning::

       我们把 ``RANGE`` **当成 ROWS 处理** —— 区别只在有并列排序键时
       暴露出来(见模块文档)。真实 ClickHouse 会按"值相等"扩展帧,
       所以 ``RANGE`` 下并列的行会拿到相同的累计值。
    """
    if spec.frame is None:
        if spec.order_by:
            return 0, slot
        return 0, n - 1

    frame = spec.frame
    start = _bound(frame.start, frame.start_offset, slot, n, is_start=True)
    end = _bound(frame.end, frame.end_offset, slot, n, is_start=False)
    # 与合法区间 [0, n-1] 求交,**保留"空帧"**(end < start)。
    #
    # 绝不能把 ``end`` 强行抬到 ``start`` —— 那会把
    # ``ROWS BETWEEN 2 PRECEDING AND 1 PRECEDING`` 在第 1 行(本应是**空帧**)
    # 退化成"只有当前行一个元素",于是 ``sum`` 从 0 变成当前行的值。
    # callers 必须把 ``end < start`` 当成"零个元素"(聚合返回单位元 /
    # 取值类返回 NULL)。
    start = max(0, start)
    end = min(end, n - 1)
    return start, end


def _bound(kind: Optional[str], offset: Optional[A.Node], slot: int, n: int,
           is_start: bool) -> int:
    if kind in (None, "CURRENT"):
        return slot
    if kind == "UNBOUNDED_PRECEDING":
        return 0
    if kind == "UNBOUNDED_FOLLOWING":
        return n - 1
    delta = 0
    if isinstance(offset, A.Literal) and isinstance(offset.value, int):
        delta = offset.value
    if kind == "PRECEDING":
        return slot - delta
    if kind == "FOLLOWING":
        return slot + delta
    return slot


def _frame_end(spec: A.WindowSpec, slot: int, n: int) -> int:
    return _frame_bounds(spec, slot, n)[1]


def _arg_value(func: A.FunctionCall, index: int, row: int, block: Block,
               ev: Evaluator) -> Any:
    if index >= len(func.args):
        return None
    return ev.evaluate(func.args[index]).column.get(row)


def _int_arg(func: A.FunctionCall, index: int, default: int) -> int:
    if index >= len(func.args):
        return default
    node = func.args[index]
    if isinstance(node, A.Literal) and isinstance(node.value, int):
        return node.value
    return default


def _const_arg(func: A.FunctionCall, index: int) -> Any:
    if index >= len(func.args):
        return None
    node = func.args[index]
    if isinstance(node, A.Literal):
        return node.value
    return None


def _aggregate(
    name: str, values: List[Any], func: A.FunctionCall, block: Block,
    ev: Evaluator, part: List[int], start: int, end: int,
) -> Any:
    """窗口聚合 —— **逐位置重算**(不做前缀和优化)。

    ``sum``/``count``/``min``/``max``/``avg`` 这里都是朴素实现
    (O(n) 每个位置 = O(n²) 总量)。真实 ClickHouse 对
    ``ROWS UNBOUNDED PRECEDING`` 这类帧做**增量**计算(加到当前行即可),
    所以它是 O(n)。教程第 14 章量化了这个差别。
    """
    clean = [v for v in values if v is not None]
    if name == "count":
        if func.args:
            return len(clean)
        return len(values)
    if name == "sum":
        return sum(clean) if clean else 0
    if name == "avg":
        return (sum(clean) / len(clean)) if clean else None
    if name == "min":
        return min(clean) if clean else None
    if name == "max":
        return max(clean) if clean else None
    if name in ("any", "anylast"):
        return clean[0] if name == "any" and clean else (clean[-1] if clean else None)
    if name == "grouparray":
        return list(values)
    if name == "grouparraylast":
        return [values[-1]] if values else []
    if name == "sumkahan":
        return math.fsum(clean) if clean else 0.0
    if name in ("varpop", "stddevpop", "varsamp", "stddevsamp"):
        n = len(clean)
        if n == 0:
            return None
        mean = sum(clean) / n
        var = sum((v - mean) ** 2 for v in clean) / (n if name.endswith("pop") else max(n - 1, 1))
        return math.sqrt(var) if "stddev" in name else var
    if name == "median":
        if not clean:
            return None
        s = sorted(clean)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
    raise_error(ErrorCode.NOT_IMPLEMENTED, f"Window aggregate {name} is not implemented")


def _sum_window_type(inner: Any) -> Any:
    """窗口 ``sum()`` 的结果类型 —— **按输入类型的有无符号推断**。

    真实 ClickHouse 的 ``sum`` 会把小整数**自动提升**:
    ``UInt8/16/32 → UInt64``、``Int8/16/32 → Int64``;``UInt64``/``Int64``
    保持不变,宽整数(``UInt128/256``、``Int128/256``)保持自身位宽;
    浮点一律 ``Float64``;``Decimal`` 保持十进制语义。

    早先的实现对"非浮点、非 Decimal"**一律返回 UInt64** —— 于是窗口 ``sum()``
    对**有符号整型**会把负结果按 UInt64 回绕:``sum(Int32) = -6`` 打印成
    ``18446744073709551610``。(**普通聚合**的 ``sum()`` 走另一条代码路径,
    本来就对 —— 这里只修窗口路径。)
    """
    from ..types import get_data_type

    if inner.is_decimal():
        return inner
    if inner.is_float():
        return get_data_type("Float64")
    signed = getattr(inner, "signed", None)
    bits = getattr(inner, "bits", None)
    if bits is not None and bits > 64:
        # 宽整数不做 64 位截断 —— 保持原类型
        return inner
    if signed:
        return get_data_type("Int64")
    # 无符号整数(UInt*)与属性未知的类型(如 Bool/Nullable)都退回 UInt64,
    # 保持与既有行为一致。
    return get_data_type("UInt64")


def _infer_result_type(
    wf: A.WindowFunction, block: Block, scope: Scope, ctx: Any, rows: int
) -> Any:
    """推断窗口函数的结果类型 —— 与真实 ClickHouse 的规则对齐。"""
    from ..types import get_data_type
    from ..types.composite import DataTypeArray

    func = wf.function
    name = func.name.lower() if isinstance(func, A.FunctionCall) else ""
    if name in ("row_number", "rank", "dense_rank", "ntile"):
        return get_data_type("UInt64")
    if name in ("percent_rank", "cume_dist"):
        return get_data_type("Float64")
    if name == "count":
        return get_data_type("UInt64")
    if name in ("grouparray", "grouparraylast"):
        inner = _arg_type(func, block, scope, ctx, rows)
        return DataTypeArray(inner)
    if name in ("avg", "median", "varpop", "varsamp", "stddevpop", "stddevsamp"):
        return get_data_type("Float64")
    if name == "sum":
        return _sum_window_type(_arg_type(func, block, scope, ctx, rows))
    if name in ("min", "max", "any", "anylast", "lag", "lead", "laginframe",
                "leadinframe", "first_value", "last_value", "nth_value",
                "first_value_respect_nulls", "last_value_respect_nulls"):
        return _arg_type(func, block, scope, ctx, rows)
    return get_data_type("Float64")


def _arg_type(func: A.FunctionCall, block: Block, scope: Scope, ctx: Any, rows: int) -> Any:
    from ..types import get_data_type

    if isinstance(func, A.FunctionCall) and func.args:
        try:
            ev = Evaluator(ctx, scope, rows)
            return ev.evaluate(func.args[0]).type
        except MiniChError:
            pass
    return get_data_type("String")


def _column_from_typed(values: List[Any], dtype: Any) -> IColumn:
    """按类型建列 —— 顺手做一次 normalize(处理 Decimal/Date 等)。"""
    col = dtype.create_column()
    for v in values:
        if v is None:
            col.append_default()
        else:
            try:
                col.append(dtype.normalize(v))
            except MiniChError:
                col.append(v)
    return col
