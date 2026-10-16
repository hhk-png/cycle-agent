"""合并(merge)—— 把多个有序 part 归并成一个。

合并是 MergeTree 的**心脏**:没有它,``INSERT`` 只会不断产生小 part,
查询要打开几千个文件,"列存 + 稀疏索引"的所有优势都被 IO 开销淹没。

.. code-block:: text

   ╔══════════════════════════════════════════════════════════════════════╗
   ║  k 路归并(k-way merge):把 k 个**各自有序**的流合成一个有序流        ║
   ╚══════════════════════════════════════════════════════════════════════╝

     part A: [1, 4, 7, 10]        ┌───────────────────────────────┐
     part B: [2, 3, 9, 11]   ──▶  │  最小堆:每次弹出全局最小的那个 │ ──▶ [1,2,3,4,7,
     part C: [5, 6, 8, 12]        │  弹出后,从**那个流**再补一个   │      9,10,11,12]
                                  └───────────────────────────────┘
     复杂度 O(N log k) —— 而不是"全部读进来排一遍"的 O(N log N)
     内存 O(k) 而不是 O(N)(每个流只缓存当前那个"最小元素")

   为什么"k 个流各自有序"这个前提成立? 因为**每个 part 内部都按排序键有序**
   —— 这是写 part 时"先排序再切 granule"的直接后果。合并能成立,
   全靠写入时那一次排序。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  合并**不只是**排序 —— 它还是"引擎家族语义"发生的地方                 │
   └──────────────────────────────────────────────────────────────────────┘

   归并时,排序键**相等**的一组行会连续被弹出。对这组行做什么,
   完全由引擎家族决定:

   .. code-block:: text

     MergeTree                 不动 —— 全部保留(有重复就有重复)

     ReplacingMergeTree        只保留 version 最大的那一行  ← 事实上的去重
     SummingMergeTree          把指定的数值列相加,合成一行
     AggregatingMergeTree      把 AggregateFunction 的**状态**合并
     CollapsingMergeTree       把 +1 / -1 配对抵消
     VersionedCollapsingMergeTree  同上,但按 version 配对

   ╔══════════════════════════════════════════════════════════════════════╗
   ║  ★ 最重要的一句话:合并是**异步**的,所以**最终一致**而非立即一致。 ║
   ║                                                                      ║
   ║    INSERT 3 行 → 立刻 SELECT → 看到 **3 行**(哪怕其中 2 行键相同)  ║
   ║       ……等后台合并跑完(或手工 OPTIMIZE)……                        ║
   ║    SELECT    → 看到 **1 行**                                        ║
   ║                                                                      ║
   ║  这不是 bug,是设计。要立即看到"合并后的结果",有两种办法:          ║
   ║    ① ``OPTIMIZE TABLE t FINAL``  —— 强制合并(会阻塞、很重)          ║
   ║    ② ``SELECT ... FROM t FINAL`` —— **查询时归并**(见下面的代价)     ║
   ╚══════════════════════════════════════════════════════════════════════╝

   .. warning::

      **``FROM t FINAL`` 的代价。** 它让"读取"退化成"读取 + 归并",
      于是放弃了 MergeTree 读路径上最值钱的那个性质:

      .. code-block:: text

         不带 FINAL:  每个 granule 可以**独立、并行**地读,
                      甚至只读需要的列的一部分 —— 因为"要读什么"
                      在开读之前就已经由 primary.idx 算清楚了。

         带 FINAL:    必须**先按排序键把数据对齐**才能去重,
                      于是同一个排序键的行必须落到同一个执行线程上
                      —— 这就把并行度从"按 granule"降到了"按 key 的分片",
                      而且``max_threads`` 对它的效果显著变差。

      官方文档的建议是:能靠后台合并收敛的就别用 FINAL,
      必须实时的场景用 ``GROUP BY`` + ``argMax`` 之类的查询写法替代。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ...block import Block, ColumnWithTypeAndName
from ...columns import IColumn
from ...errors import ErrorCode, raise_error
from ...types import DataType, get_data_type
from ...types.composite import DataTypeAggregateFunction, DataTypeSimpleAggregateFunction
from ...util.keys import normalize_key_value
from .marks import MarkRange
from .part import MergeTreePart, PartWriteContext, _Descending, sorting_key_value, write_part

__all__ = [
    "MergeTreeMergeConfig",
    "MergeTreePartReader",
    "MergeTreeMergingIterator",
    "MergeTreeDataPartWriter",
    "BlockRowSource",
    "merge_parts",
]


# ══════════════════════════════════════════════════════════════════════════
# 家族配置
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class MergeTreeMergeConfig:
    """合并时需要的"家族语义"配置 —— 从引擎参数来。

    .. code-block:: text

       ENGINE = ReplacingMergeTree(ver)          → family='Replacing', version='ver'
       ENGINE = SummingMergeTree((a, b))         → family='Summing',  sum=['a','b']
       ENGINE = CollapsingMergeTree(sign)        → family='Collapsing', sign='sign'
       ENGINE = AggregatingMergeTree()           → family='Aggregating'
       ENGINE = MergeTree()                      → family='MergeTree'
    """

    family: str = "MergeTree"
    #: ``ReplacingMergeTree(ver)`` / ``VersionedCollapsingMergeTree(sign, ver)``
    version_column: str = ""
    #: ``ReplacingMergeTree(ver, is_deleted)`` —— 标记删除的列
    is_deleted_column: str = ""
    #: ``SummingMergeTree((a, b))`` —— 空列表表示"自动选所有非键数值列"
    sum_columns: List[str] = field(default_factory=list)
    #: ``CollapsingMergeTree(sign)`` 的符号列
    sign_column: str = ""

    def is_replacing(self) -> bool:
        return self.family == "Replacing"

    def is_summing(self) -> bool:
        return self.family == "Summing"

    def is_aggregating(self) -> bool:
        return self.family == "Aggregating"

    def is_collapsing(self) -> bool:
        return self.family == "Collapsing"

    def is_versioned_collapsing(self) -> bool:
        return self.family == "VersionedCollapsing"


# ══════════════════════════════════════════════════════════════════════════
# 从一个 part 里按行顺序读
# ══════════════════════════════════════════════════════════════════════════


class MergeTreePartReader:
    """把一个 part 的若干列读成"内存里的一批列",按 part 内的行序排列。

    真实 ClickHouse 的 ``MergeTreeReader`` 是**流式**的(读一个 granule
    处理一个),因为它的 part 可能是 TB 级。mini-clickhouse 一次读完
    —— 对一个几 MB 的 part 这最省心,而且不改变语义。

    它额外负责算排序键:合并需要"每一行的排序键值"来驱动堆,
    而排序键可能是**表达式**(``toYYYYMM(d)``),所以这里要保留
    求值能力(见 :func:`_sorting_key_reader`)。
    """

    def __init__(
        self,
        part: MergeTreePart,
        columns: Sequence[str],
        key_exprs: Sequence[Any],
        key_reverse: Sequence[bool],
        ctx: Any = None,
        mark_range: Optional[MarkRange] = None,
        events: Any = None,
    ) -> None:
        self.part = part
        self.columns = list(columns)
        self.key_exprs = list(key_exprs)
        self.key_reverse = list(key_reverse)
        self.ctx = ctx
        self.events = events
        self.marks = part.get_marks_count()
        self.mark_range = mark_range or MarkRange(0, self.marks)

        self.data: Dict[str, IColumn] = {}
        for name in self.columns:
            if part.has_column(name):
                self.data[name] = part.read_column(ctx, name, self.mark_range, events)
            else:
                # ALTER ADD COLUMN 之后的老 part 缺列 → 读出来补默认值
                self.data[name] = self._default_column(part, name)
        self.rows = len(next(iter(self.data.values()))) if self.data else 0
        self._keys = self._build_keys()

    def _default_column(self, part: MergeTreePart, name: str) -> IColumn:
        """``ALTER ADD COLUMN`` 之后的老 part 缺这一列时,合并路径要补什么。

        .. warning::

           **必须和读路径用同一个实现**
           (:func:`minich.storages.merge_tree.part.missing_column_default`)。

           以前这里用 ``append_many_default()`` 补**类型零值**,后果是:

           .. code-block:: text

              ALTER TABLE a ADD COLUMN w UInt64 DEFAULT 5
              SELECT w FROM a        → 5, 5     ✅ 读时补默认值
              OPTIMIZE TABLE a                     ← 合并把 0 **物理写进**磁盘
              SELECT w FROM a        → 0, 0     ❌ 前后不一致

           而且随之而来的 mutation(``UPDATE w = 9 WHERE w = 5``)会因为
           那一列已经变成 0 而**匹配不到任何行**,却报告成功。
        """
        from .part import _fixup_aggregate_column, missing_column_default

        cdef = self._column_def(part, name)
        if cdef is not None:
            return _fixup_aggregate_column(
                missing_column_default(cdef, part.rows), self._column_type(part, name)
            )
        col = self._column_type(part, name).create_column()
        if part.rows > 0:
            col.append_many_default(part.rows)
        return _fixup_aggregate_column(col, self._column_type(part, name))

    def _column_def(self, part: MergeTreePart, name: str) -> Any:
        """从 part 所属的表里取列的**定义**(带 ``default_expr_sql``)。"""
        table = getattr(part, "table", None)
        if table is None or not hasattr(table, "get_column"):
            return None
        return table.get_column(name)

    def _column_type(self, part: MergeTreePart, name: str) -> DataType:
        t = part.column_types.get(name)
        if t is not None:
            return t
        table = part.table
        if table is not None:
            c = table.get_column(name) if hasattr(table, "get_column") else None
            if c is not None:
                return c.type
        return get_data_type("String")

    def _build_keys(self) -> List[tuple]:
        """预先把每一行的排序键算出来 —— 堆的比较用的就是它。"""
        if not self.key_exprs:
            return [()] * self.rows
        block = Block([
            ColumnWithTypeAndName(n, self._column_type(self.part, n), c)
            for n, c in self.data.items()
        ])
        from .expression import evaluate_expression

        key_cols: List[IColumn] = []
        for e in self.key_exprs:
            c = evaluate_expression(e, block).column
            key_cols.append(c.convert_to_full_column() if c.is_const() else c)
        out: List[tuple] = []
        for i in range(self.rows):
            vals = []
            for j, c in enumerate(key_cols):
                v = sorting_key_value(c.get(i))
                if j < len(self.key_reverse) and self.key_reverse[j]:
                    v = _Descending(v)
                vals.append(v)
            out.append(tuple(vals))
        return out

    def key_at(self, row: int) -> tuple:
        return self._keys[row]

    def value(self, column: str, row: int) -> Any:
        return self.data[column].get(row)

    def column_type(self, column: str) -> DataType:
        """某一列的类型 —— 归并迭代器需要它来判定"是不是聚合状态列"。"""
        return self._column_type(self.part, column)

    def order_key(self) -> Tuple[int, int, int]:
        """跨 part 的"新近程度" —— 用来给 ReplacingMergeTree 打破平局。

        取 ``(max_block_number, level, 0)``:block 号大的更新
        (``INSERT`` 时 block 号递增),level 大的更"熟"。
        """
        return (self.part.get_max_block_number(), self.part.get_level(), 0)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MergeTreePartReader {self.part.name} rows={self.rows}>"


# ══════════════════════════════════════════════════════════════════════════
# 归并迭代器
# ══════════════════════════════════════════════════════════════════════════


class MergeTreeMergingIterator:
    """k 路归并 + 家族语义的**行级**迭代器。

    它产出的是"已经按家族规则处理过的行":每个元素是
    ``(key_tuple, [(reader, row) ...])`` 形式的一组"同键行"
    —— 组内行的处理由 :meth:`_emit_group` 决定。
    """

    def __init__(
        self,
        readers: Sequence[MergeTreePartReader],
        config: MergeTreeMergeConfig,
        key_columns: Sequence[str],
    ) -> None:
        self.readers = list(readers)
        self.config = config
        self.key_columns = list(key_columns)
        self._heap: List[Tuple[tuple, int, int]] = []
        for idx, r in enumerate(self.readers):
            if r.rows == 0:
                continue
            heapq.heappush(self._heap, (r.key_at(0), idx, 0))

    # ── 归并 ────────────────────────────────────────────────────────────

    def _next_row(self) -> Optional[Tuple[tuple, int, int]]:
        if not self._heap:
            return None
        key, idx, row = heapq.heappop(self._heap)
        nxt = row + 1
        if nxt < self.readers[idx].rows:
            heapq.heappush(self._heap, (self.readers[idx].key_at(nxt), idx, nxt))
        return key, idx, row

    def groups(self) -> Iterator[Tuple[tuple, List[Tuple[int, int]]]]:
        """产出"同排序键的一组行" —— 组内按 (新近程度, 行号) 排序。"""
        while True:
            first = self._next_row()
            if first is None:
                return
            key, idx, row = first
            group: List[Tuple[int, int]] = [(idx, row)]
            while True:
                if not self._heap:
                    break
                nxt_key = self._heap[0][0]
                if nxt_key != key:
                    break
                k2, i2, r2 = self._next_row()  # type: ignore[misc]
                group.append((i2, r2))
            group.sort(key=lambda pr: (self.readers[pr[0]].order_key(), pr[1]))
            yield key, group

    def rows(self) -> Iterator[Dict[str, Any]]:
        """产出合并后的**行字典** —— 每行是 ``{列名: 值}``。"""
        for key, group in self.groups():
            for row in self._emit_group(key, group):
                yield row

    # ── 家族语义 ────────────────────────────────────────────────────────

    def _emit_group(self, key: tuple, group: List[Tuple[int, int]]) -> Iterable[Dict[str, Any]]:
        cfg = self.config
        if cfg.is_replacing():
            return self._emit_replacing(group)
        if cfg.is_summing():
            return self._emit_summing(group)
        if cfg.is_aggregating():
            return self._emit_aggregating(group)
        if cfg.is_collapsing():
            return self._emit_collapsing(group, versioned=False)
        if cfg.is_versioned_collapsing():
            return self._emit_collapsing(group, versioned=True)
        # 普通 MergeTree:原样保留
        return [self._row_dict(idx, row) for idx, row in group]

    def _row_dict(self, idx: int, row: int) -> Dict[str, Any]:
        r = self.readers[idx]
        return {c: r.value(c, row) for c in self._columns() if c in r.data}

    def _columns(self) -> List[str]:
        seen: List[str] = []
        for r in self.readers:
            for c in r.data:
                if c not in seen:
                    seen.append(c)
        return seen

    def _emit_replacing(self, group: List[Tuple[int, int]]) -> List[Dict[str, Any]]:
        """``ReplacingMergeTree`` —— 键相同的行只留"最新"的那一条。

        .. code-block:: text

           排序键 id,version 列 ver:
              id=1, ver=100  ← 旧的
              id=1, ver=200  ← 新的     ✅ 保留这一条
              id=1, ver=150

           如果没声明 ver 列:按"新近程度"取最后一条
                            (block 号大的算更新)

           ``is_deleted`` 列(ClickHouse 23.8+):
              id=1, ver=200, is_deleted=1  → **整组丢弃**
              ↑ 这就是"用 ReplacingMergeTree 做软删除"的写法
        """
        cfg = self.config
        chosen = group[-1]
        if cfg.version_column:
            best = None
            for idx, row in group:
                v = self.readers[idx].value(cfg.version_column, row)
                rank = (normalize_key_value(v), self.readers[idx].order_key(), row)
                if best is None or rank > best[0]:
                    best = (rank, (idx, row))
            chosen = best[1]
        idx, row = chosen
        if cfg.is_deleted_column:
            flag = self.readers[idx].value(cfg.is_deleted_column, row)
            if _truthy(flag):
                return []
        return [self._row_dict(idx, row)]

    def _emit_summing(self, group: List[Tuple[int, int]]) -> List[Dict[str, Any]]:
        """``SummingMergeTree`` —— 键相同的行把数值列相加。

        哪些列被相加? 建表时列出来的那些;没列就**自动**取"所有不在排序键里
        的数值列"(真实 ClickHouse 的默认行为)。

        .. warning::

           **相加是会溢出的**,而且 ClickHouse **不检查**。``UInt8`` 的
           ``250 + 10 = 4``。这是 SummingMergeTree 最经典的坑,
           汇总列的类型一定要够宽(通常直接上 ``UInt64``/``Int64``)。
        """
        first_idx, first_row = group[0]
        out = self._row_dict(first_idx, first_row)
        sum_cols = self.config.sum_columns or self._auto_sum_columns()
        # 键列的值取第一条(它们本来都相同)
        for c in sum_cols:
            total = None
            for idx, row in group:
                v = self.readers[idx].value(c, row)
                if v is None:
                    continue
                total = v if total is None else total + v
            if total is not None:
                # 求和用的是 Python 的任意精度整数,回写窄类型列会**越界** ——
                # 必须按列类型回绕(见 _wrap_sum_to_column)。
                out[c] = _wrap_sum_to_column(self.readers[first_idx].column_type(c), total)
        # ``SimpleAggregateFunction`` 列:按它**声明的**函数折叠(``sum``/``min``/``max``/
        # ``any``/``anyLast``…),而不是像普通列那样"取第一行"。这类列不在
        # _auto_sum_columns 的数值列清单里(它不是数值类型),所以要单独处理;
        # 它的语义与 SummingMergeTree 的"同键折叠"天然契合。
        for c in self._columns():
            dtype = self.readers[0].column_type(c)
            if isinstance(dtype, DataTypeSimpleAggregateFunction):
                merged = self._fold_simple_column(c, dtype, group)
                if merged is not None:
                    out[c] = merged
        return [out]

    def _auto_sum_columns(self) -> List[str]:
        """``SummingMergeTree()`` 不给列清单时,自动挑出"该求和"的列。

        .. warning::

           必须**先剥掉 ``Nullable`` 再判数值** —— ``Nullable`` 是一个
           独立的类型族,``Nullable(Int32).is_numeric()`` 是 ``False``。
           不剥的话,``SummingMergeTree()``(不给列清单)会**静默漏掉**
           所有可空数值列,于是那些列退化成"取第一行",``FINAL`` 得到
           错误的结果;而显式写了 ``SummingMergeTree(v)`` 却是对的 ——
           同一个引擎的两种写法行为不一致,几乎不可能被用户发现。

           真实 ClickHouse 对可空列同样求和(NULL 参与时按聚合的
           NULL 语义跳过;求和循环里已有跳过 NULL 的代码)。
        """
        key_set = set(self.key_columns)
        cols: List[str] = []
        for r in self.readers:
            for c in r.data:
                if c in key_set or c in cols:
                    continue
                t = r.column_type(c)
                if t.is_nullable():
                    t = t.nested  # type: ignore[attr-defined]
                if t.is_numeric() and not t.is_date() and not t.is_date_time():
                    cols.append(c)
        return cols

    def _emit_aggregating(self, group: List[Tuple[int, int]]) -> List[Dict[str, Any]]:
        """``AggregatingMergeTree`` —— 把 ``AggregateFunction`` 的**状态**合并。

        这一族的关键在于:列里存的不是值,是**状态**
        (``AggregateFunction(sum, UInt64)`` 列里是"求和状态")。
        合并状态用的是聚合函数自己的 ``merge(dst, src)`` ——
        这正是"可合并状态"这个设计存在的意义(见第 12 章)。

        查询时用 ``-Merge`` 组合器把状态"读完",得到最终结果::

            SELECT id, sumMerge(s) FROM t GROUP BY id;
        """
        out = self._row_dict(group[0][0], group[0][1])
        for c in self._columns():
            dtype = self.readers[0].column_type(c)
            if isinstance(dtype, DataTypeSimpleAggregateFunction):
                # ``SimpleAggregateFunction`` 列:状态**就是**普通值,合并 = 按声明的
                # 函数把组内的值折叠起来(不是"取第一行",也无需 ``clone_state``/``merge``
                # 那套二进制状态逻辑 —— 见 _fold_simple_column)。
                merged = self._fold_simple_column(c, dtype, group)
                if merged is not None:
                    out[c] = merged
                continue
            func = _aggregate_func_of(dtype)
            if func is None:
                continue
            acc = None
            for idx, row in group:
                state = self.readers[idx].value(c, row)
                if state is None:
                    continue
                if acc is None:
                    acc = func.clone_state(state)
                else:
                    func.merge(acc, state)
            if acc is not None:
                out[c] = acc
        return [out]

    def _fold_simple_column(
        self, column: str, dtype: Any, group: List[Tuple[int, int]]
    ) -> Any:
        """把一组行里 ``SimpleAggregateFunction`` 列的**普通值**按声明的函数折叠。

        .. code-block:: text

           sum      组内值相加        min      取组内最小值
           max      取组内最大值      any      取第一个(组内顺序)
           anyLast  取最后一个

        ``SimpleAggregateFunction`` 的语义就是"**状态即结果本身**"(见
        :class:`minich.types.composite.DataTypeSimpleAggregateFunction`),
        所以合并就是把组内的值按声明函数两两合并 —— 这与 ``AggregateFunction``
        "合并一段二进制状态"是两回事。

        实现上仍复用聚合函数对象:它的**内部**状态表示未必是裸值
        (``min``/``max`` 用 ``[has, value]``,``any`` 用 ``[has, value]``),
        所以这里把值喂进 ``add()``、再用 ``insert_result_into()`` 取出结果,
        而不是直接对裸值调 ``merge()``。这样"状态就是值"的那一小撮函数
        (``sum``/``min``/``max``/``any``/``anyLast``/…)都能自动覆盖。

        返回 ``None`` 表示这一列**没有可折叠的值**(组内全是 NULL)——调用方
        保留第一行的值。
        """
        func = _simple_aggregate_func_of(dtype)
        if func is None:
            return None
        value_type = dtype.value_type
        values = value_type.create_column()
        for idx, row in group:
            v = self.readers[idx].value(column, row)
            if v is not None:
                values.append(v)
        if values.size() == 0:
            return None
        state = func.create_state()
        for i in range(values.size()):
            func.add(state, [values], i)
        result = value_type.create_column()
        func.insert_result_into(state, result)
        return result.get(0)

    def _emit_collapsing(self, group: List[Tuple[int, int]], versioned: bool) -> List[Dict[str, Any]]:
        """``CollapsingMergeTree`` —— 把 ``+1`` 与 ``-1`` 配对抵消。

        .. code-block:: text

           ┌── 为什么要"折叠"? ──────────────────────────────────────────┐
           │  这是"用追加代替更新"的一个经典方案:                        │
           │    要"改"一行 → 追加一条 sign=-1 的(抵消旧值)              │
           │                + 追加一条 sign=+1 的(新值)                 │
           │  合并时两两抵消,不留下垃圾;                                  │
           │  查询时用 ``SUM(x*sign) GROUP BY key`` 得到"当前值"。       │
           │                                                            │
           │  它要求写入方保证**严格的 +1 与 -1 成对出现**,而且          │
           │  **顺序要对**(+1 必须在 -1 之前,否则中间会看到负数)。     │
           │  这就是它被 VersionedCollapsingMergeTree 取代的原因:       │
           │  后者用 version 列,天然容忍乱序写入。                       │
           └────────────────────────────────────────────────────────────┘

        .. warning::

           真实 ClickHouse 的折叠**不是"精确配对"**,而是"够抵消就行":
           如果一组里有 3 条 +1 和 1 条 -1,它留下 **2 条** +1(具体留哪
           两条由实现决定)。mini-clickhouse 的做法是:统计
           ``sum(sign)``,``<= 0`` 就整组丢弃,否则留下最后一条 +1。
           语义上等价于"查询结果正确",但并不逐条对应真实 CH 的产物。
        """
        cfg = self.config
        sign_col = cfg.sign_column
        if not sign_col:
            return [self._row_dict(idx, row) for idx, row in group]
        if versioned and cfg.version_column:
            # 按 (version, sign) 配对:同一个 version 下的 +1/-1 互相抵消
            ordered = sorted(
                group,
                key=lambda pr: (
                    normalize_key_value(self.readers[pr[0]].value(cfg.version_column, pr[1])),
                    normalize_key_value(self.readers[pr[0]].value(sign_col, pr[1])),
                ),
            )
        else:
            ordered = group
        total_sign = 0
        for idx, row in ordered:
            v = self.readers[idx].value(sign_col, row)
            total_sign += int(v or 0)
        if total_sign <= 0:
            return []
        # 留下 sign=+1 的最后一条(与"未抵消的那一行"等价)
        for idx, row in reversed(ordered):
            if int(self.readers[idx].value(sign_col, row) or 0) > 0:
                return [self._row_dict(idx, row)]
        return []


class BlockRowSource:
    """把一个内存 Block 包装成"归并行源" —— 供 ``FROM t FINAL`` 使用。

    ``FINAL`` 的语义与"把选中的行当成一个单 part 去合并"完全一样,
    所以复用同一套归并 + 家族折叠逻辑,而不是另写一份。
    索引 / 归并的**唯一**差别是:这些行可能来自多个 part、
    而且**不保证已排序** —— 所以这里要先按排序键把行**全局排一遍**
    (:class:`MergeTreeMergingIterator` 是 k 路归并,只合并**相邻**同键行;
    part 之间拼接出来的键不是全局有序的,同键行不相邻 → 一行也折不掉),
    堆里比较的"键"也由**表达式**现算(与写 part 时同一套排序键)。
    """

    def __init__(
        self,
        block: Block,
        types: Dict[str, DataType],
        key_exprs: Sequence[Any],
        key_reverse: Sequence[bool],
    ) -> None:
        self.block = block
        self.types = dict(types)
        self.rows = block.rows()
        self.part = None
        from .expression import evaluate_expression

        key_cols: List[IColumn] = []
        for e in key_exprs:
            c = evaluate_expression(e, block).column
            key_cols.append(c.convert_to_full_column() if c.is_const() else c)
        self._keys: List[tuple] = []
        for i in range(self.rows):
            vals = []
            for j, c in enumerate(key_cols):
                v = sorting_key_value(c.get(i))
                if j < len(key_reverse) and key_reverse[j]:
                    v = _Descending(v)
                vals.append(v)
            self._keys.append(tuple(vals))
        # 归并迭代器只合并**相邻**同键行,而这里的行是多 part 拼接起来的、
        # 不保证全局有序 —— 所以先按排序键做一次稳定排序。不排的话
        # 同一个键的行不相邻,Replacing/Summing/Collapsing 就什么也折叠不掉。
        order = sorted(range(self.rows), key=self._keys.__getitem__)
        if order != list(range(self.rows)):
            block.permute(order)
            self._keys = [self._keys[i] for i in order]
        self.data: Dict[str, IColumn] = {c.name: c.column for c in block.data}

    def key_at(self, row: int) -> tuple:
        return self._keys[row]

    def value(self, column: str, row: int) -> Any:
        return self.data[column].get(row)

    def column_type(self, column: str) -> DataType:
        return self.types.get(column, get_data_type("String"))

    def order_key(self) -> Tuple[int, int, int]:
        return (0, 0, 0)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BlockRowSource rows={self.rows}>"


def _wrap_sum_to_column(dtype: DataType, total: Any) -> Any:
    """把求和结果**回绕**到列类型的值域里 —— 真实 ClickHouse 的整数溢出语义。

    ``SummingMergeTree`` 的求和走 Python 的任意精度整数,而回写窄类型列时
    NumPy 会因为越界抛**裸** ``OverflowError``(``UInt8`` 的 ``250 + 10`` →
    260 越界)。真实 ClickHouse 不报错,就是 C++ 的回绕(``250 + 10 = 4``),
    所以这里显式走列类型自己的 :meth:`DataTypeNumber.wrap` —— **不自己实现
    回绕**,免得有符号/无符号/位宽的细节各写一套。

    没有 ``wrap`` 的类型(非整数)原样返回;回绕本身失败时抛 ``MiniChError``,
    不把裸异常漏给上层。
    """
    # 可空列的回绕发生在**内层**类型上(``Nullable`` 自己没有 ``wrap``)——
    # 不剥包装的话 ``Nullable(Int32)`` 的求和永不回绕,窄列的溢出语义
    # 会与不可空列不一致。
    if dtype.is_nullable():
        dtype = dtype.nested  # type: ignore[attr-defined]
    wrap = getattr(dtype, "wrap", None)
    if wrap is None:
        return total
    try:
        return wrap(total)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御性兜底
        raise_error(
            ErrorCode.CANNOT_CONVERT_TYPE,
            f"Cannot represent sum {total!r} in column type {dtype.get_name()}: {exc}",
        )


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return bool(value)


def _aggregate_func_of(dtype: DataType) -> Any:
    """从列类型里取聚合函数实例(``AggregateFunction`` 家族)。

    ``SimpleAggregateFunction`` 的**状态就是值**,合并方式不同,由
    :func:`_simple_aggregate_func_of` 单独处理 —— 这里只认真正的
    ``AggregateFunction``。
    """
    if isinstance(dtype, DataTypeAggregateFunction):
        from ...aggregates import get_aggregate_function

        try:
            return get_aggregate_function(dtype.func_name, dtype.params, dtype.arg_types)
        except Exception:
            return None
    return None


def _simple_aggregate_func_of(dtype: DataType) -> Any:
    """从 ``SimpleAggregateFunction`` 里取聚合函数实例。

    与 :func:`_aggregate_func_of` 的区别:``SimpleAggregateFunction`` 没有
    "参数类型列表",它的唯一参数类型就是 ``value_type``。返回的函数对象在这里
    只用来提供 ``create_state``/``add``/``insert_result_into`` 这套"折叠一个
    普通值序列"的能力(状态的具体表示是它内部的事)。

    不是 ``SimpleAggregateFunction``、或函数名不认识时返回 ``None``。
    """
    if not isinstance(dtype, DataTypeSimpleAggregateFunction):
        return None
    from ...aggregates import get_aggregate_function

    try:
        return get_aggregate_function(dtype.func_name, [], [dtype.value_type])
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════
# 写合并结果
# ══════════════════════════════════════════════════════════════════════════


class MergeTreeDataPartWriter:
    """把合并后的行写成新 part。

    真实 ClickHouse 的 ``MergeTreeDataPartWriterWide`` 是**流式**的:
    它一边从归并迭代器拿块,一边写文件,内存占用与 part 大小无关。
    mini-clickhouse 先把所有行收集成一个 Block,再交给
    :func:`minich.storages.merge_tree.part.write_part` ——
    对一个 20 万行的演示来说内存完全可以接受,而且**写出的 part 完全一样**
    (同样的排序、同样的 granule 切分、同样原子提交)。
    """

    def __init__(self, data: Any) -> None:
        self.data = data

    def build_block(
        self,
        columns: Sequence[str],
        rows: Iterable[Dict[str, Any]],
    ) -> Block:
        """把行字典流拼成一个 Block(列顺序 = ``columns``)。"""
        from .part import create_column_for_type

        headers = []
        for name in columns:
            cdef = self.data.table.get_column(name)
            if cdef is None:
                raise_error(
                    ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                    f"Column {name!r} is not defined in table {self.data.full_table_name()}",
                )
            headers.append(ColumnWithTypeAndName(
                name, cdef.type, create_column_for_type(cdef.type)
            ))
        block = Block(headers)
        for row in rows:
            for c in block.data:
                c.column.append(row[c.name])
        return block

    def write(
        self,
        part_name: str,
        block: Block,
        partition_value: Any,
    ) -> MergeTreePart:
        context = self.data.make_write_context(
            partition_value=partition_value,
            level=parse_level(part_name),
            block_number=parse_max_block(part_name),
            ttl_info=self.data.compute_ttl_info_for_block(block),
        )
        path = f"{self.data.data_path}/{part_name}"
        return write_part(self.data.disk, path, block, context)


def parse_level(part_name: str) -> int:
    from .part import parse_part_name

    return parse_part_name(part_name).level


def parse_max_block(part_name: str) -> int:
    from .part import parse_part_name

    return parse_part_name(part_name).max_block


# ══════════════════════════════════════════════════════════════════════════
# 顶层入口
# ══════════════════════════════════════════════════════════════════════════


def merge_parts(
    data: Any,
    parts: Sequence[MergeTreePart],
    ctx: Any = None,
    events: Any = None,
    mutations: Optional[Sequence[Any]] = None,
) -> Optional[MergeTreePart]:
    """把 ``parts`` 合并成一个新 part 并返回它(``parts`` 一个就直接返回 None)。

    步骤:

    .. code-block:: text

       parts ──► 每个 part 读成内存列(MergeTreePartReader)
                  │  (顺带求值排序键 → 堆的比较键)
                  ▼
               k 路归并(最小堆)→ 同键成组
                  │
                  ▼  按引擎家族折叠(去重/求和/合并状态/抵消)
              行字典流
                  │
                  ▼
              拼成 Block ──► write_part(原子提交)──► 新 part
                              ↑
                        level = max(levels)+1
                        block 号区间 = [min(min_block), max(max_block)]

    ``mutations`` 传入时,会在**读出来之后、归并之前**对每个源 part 的行
    套用 mutation —— 这正是真实 ClickHouse 的语义:
    **mutation 是在合并时顺带完成的,所以"改完"要等下一次合并**。
    """
    if len(parts) < 2:
        return None

    cfg = data.merge_config
    columns = [n for n in data.stored_column_names()]
    # 需要额外读出来的"家族列"(它们可能不在 SELECT 的列里,但合并要用)
    extra = [c for c in (cfg.version_column, cfg.is_deleted_column, cfg.sign_column)
             if c and c not in columns]
    read_columns = columns + extra

    readers: List[MergeTreePartReader] = []
    for p in sorted(parts, key=lambda x: (x.get_max_block_number(), x.get_name())):
        r = MergeTreePartReader(p, read_columns, data.sorting_key_exprs,
                                data.sorting_key_reverse, ctx, None, events)
        if mutations:
            r = _apply_mutations_to_reader(data, r, mutations, ctx)
        readers.append(r)

    iterator = MergeTreeMergingIterator(readers, cfg, data.key_column_names())

    new_name = _merged_part_name(parts)
    writer = MergeTreeDataPartWriter(data)
    block = writer.build_block(read_columns, iterator.rows())
    if block.rows() == 0:
        # 全部行被抵消/删除 → 不产出新 part(调用方负责把旧 part 丢掉)
        if events is not None:
            events.increment("MergedRows", 0)
        return _EmptyMergeResult(new_name)

    part = writer.write(new_name, block, data.partition_value_of(readers[0].part))
    if events is not None:
        events.increment("MergedRows", block.rows())
        events.increment("MergedUncompressedBytes",
                         sum(c.column.byte_size() for c in block.data))
    return part


def _merged_part_name(parts: Sequence[MergeTreePart]) -> str:
    """合并结果的 part 名 —— 沿用真实 ClickHouse 的 ``MergeTreePartInfo`` 规则。

    .. code-block:: text

       输入: 202403_1_1_0 与 202403_2_2_0
              │    │ │ │
              │    └─┴─┴── min/max block 与 level
              └──────── partition id

       输出: 202403_1_2_1
              │      │ │ │
              │      │ │ └── level = max(0, 0) + 1 = 1
              │      └─┴──── block 区间取【并集】:1..2
              └─────────── partition id 不变
    """
    from .part import format_part_name

    pid = parts[0].get_partition_id()
    for p in parts[1:]:
        if p.get_partition_id() != pid:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"Cannot merge parts from different partitions: "
                f"{pid} vs {p.get_partition_id()}",
            )
    min_block = min(p.get_min_block_number() for p in parts)
    max_block = max(p.get_max_block_number() for p in parts)
    level = max(p.get_level() for p in parts) + 1
    return format_part_name(pid, min_block, max_block, level)


class _EmptyMergeResult:
    """合并后"一行不剩"的占位结果 —— 调用方据此删掉所有输入 part。

    真实 ClickHouse 里这种情况发生在:所有行都被 TTL 删了、
    都被 collapsing 抵消了、或都被 mutation 删了。它不会写一个空 part
    (MergeTree 不允许空 part),而是把所有输入 part 标记成 obsolete。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.rows = 0
        self.empty = True

    def rows_count(self) -> int:
        return 0

    def is_empty(self) -> bool:
        return True

    def get_partition_id(self) -> str:
        from .part import parse_part_name

        return parse_part_name(self.name).partition_id

    def get_name(self) -> str:
        return self.name

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EmptyMergeResult {self.name}>"


def _apply_mutations_to_reader(
    data: Any,
    reader: MergeTreePartReader,
    mutations: Sequence[Any],
    ctx: Any,
) -> MergeTreePartReader:
    """把 mutation 套用到一个已读出的 part 上(就地过滤 / 赋值)。

    .. code-block:: text

       mutation 的执行模型(与真实 ClickHouse 一致):

         ALTER TABLE t DELETE WHERE x < 0
                │
                ├── 记录一条"待完成的 mutation"(带编号)
                │       system.mutations 里能看到它(parts_to_do > 0)
                │
                └── **不立刻改数据**,而是:
                      每次这个 part 参与合并时,顺手把谓词套用一遍
                      ↑ 这就是为什么"mutation 可能要等很久才生效"

       好处:不产生额外的随机写(与合并共用一次顺序写);
       代价:一条 DELETE 的生效时间 = 不确定(取决于合并频率)。
       想看它什么时候完成 → ``SELECT * FROM system.mutations``。
    """
    from .mutations import apply_mutation_to_columns

    for entry in mutations:
        for command in entry.commands:
            reader = apply_mutation_to_columns(reader, command, data, ctx)
    return reader
