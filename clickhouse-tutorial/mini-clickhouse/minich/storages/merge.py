"""``Merge`` 引擎 —— 把多张同构表**看成一张**虚拟表。

.. code-block:: text

   CREATE TABLE logs_all AS logs_2024_01 ENGINE =
       Merge(currentDatabase(), '^logs_');

   SELECT count() FROM logs_all;      -- 等于把 logs_2024_01, logs_2024_02, … 加起来

**它存什么?** 什么都不存。它是一张**纯虚拟表**:自己没有任何数据/文件,
``read`` 时按正则找出匹配的所有表,把它们的读取**串起来吐出去**。

**它不能做什么?**
* 不能写(``INSERT`` 进去会报只读)—— 它连"往哪写"都不知道;
* 不做去重、不做排序(真实 ClickHouse 的 ``Merge`` 有一个 ``MergeTree``
   变体专门处理冲突,普通 ``Merge`` 只是简单拼接);
* 不保证顺序:各子表读出来的先后由"表名排序"决定,不是时间顺序。

**什么时候该选它(而不是 MergeTree)?**
"按时间分表"的老架构(每天/每月一张 ``logs_YYYYMM``)想让查询像在查一张表时。
它让你**不用**改数据布局就能得到"统一视图",代价是每次查询都要把所有匹配表
挨个打开、逐个读 —— 所以 ``Merge`` 适合"表数量可控(几十张)"的场景;
几百上千张分表时应该改用 ``MergeTree`` 的 ``PARTITION BY``。

.. note::

   ``Merge(currentDatabase(), '…')`` 里的 ``currentDatabase()`` 是一个
   函数调用,求值结果是"当前数据库"。mini-clickhouse 把它识别成一个
   特殊标记并替换成表自己的数据库名 —— 与真实 ClickHouse 的处理一致。

.. warning::

   ``Merge`` 读的是**子表的物理数据**,不经过子表的任何视图/过滤逻辑。
   所以如果某张子表本身是 ``View``,``Merge`` 读到的会是"它 read 出来的东西"
   (在我们的实现里对 ``View`` 会抛 ``NOT_IMPLEMENTED``,因为需要解释器)。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ._common import (
    _check_chunk,
    _make_chunk,
    _requested_columns,
    _stored_columns,
)
from .base import IStorage, StorageFeatures, register_storage_engine

__all__ = ["StorageMerge"]


def _arg_str(arg: Any) -> str:
    if isinstance(arg, str):
        return arg
    name = getattr(arg, "name", None)
    if isinstance(name, str):
        return name
    return str(arg)


class StorageMerge(IStorage):
    """虚拟合并表 —— 见模块文档。"""

    engine_name = "Merge"

    #: 它把子表的读取串起来,本身没有任何存储能力。
    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        raw = [_arg_str(a) for a in (engine_args or [])]
        database: Optional[str] = None
        pattern = ".*"
        if len(raw) >= 2:
            database, pattern = raw[0], raw[1]
        elif len(raw) == 1:
            pattern = raw[0]
        if database in (None, "", "currentDatabase()", "current_database()", "DATABASE()"):
            database = storage_id.database
        self.database = database
        self.pattern = pattern
        try:
            self._regex = re.compile(pattern)
        except re.error as exc:  # pragma: no cover - 非法正则
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"Merge engine got an invalid table regexp {pattern!r}: {exc}",
            )

    # ── 子表发现 ────────────────────────────────────────────────────────

    def matched_tables(self) -> List[Any]:
        """按正则找出现在数据库里的子表(名字排序,保证确定性)。"""
        catalog = getattr(self.context, "catalog", None)
        if catalog is None:
            return []
        out = []
        for table in catalog.tables_of(self.database):
            if table.name == self.table.name:
                continue  # 别把自己读进来(会无限递归)
            if self._regex.search(table.name):
                out.append(table)
        out.sort(key=lambda t: t.name)
        return out

    # ── 元信息 ──────────────────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        own = _stored_columns(self.table)
        if own:
            return own
        # 建表时没写列定义 —— 从第一张匹配表借一份结构(真实 CH 要求显式写列)。
        for table in self.matched_tables():
            if table.engine is not None:
                return table.engine.get_columns()
        return []

    def get_name(self) -> str:
        return f"Merge({self.database}, '{self.pattern}')"

    def row_count(self) -> int:
        return sum(
            t.engine.row_count() for t in self.matched_tables() if t.engine is not None
        )

    def describe_stats(self) -> Dict[str, Any]:
        matched = [t.name for t in self.matched_tables()]
        return {"database": self.database, "regexp": self.pattern,
                "matched_tables": len(matched), "tables": matched}

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        expected = _requested_columns(ctx, self.get_columns())
        if not expected:
            return
        for table in self.matched_tables():
            engine = table.engine
            if engine is None:
                continue
            for chunk in engine.read(ctx):
                reordered = self._reorder(chunk, expected)
                _check_chunk(reordered, expected)
                yield reordered

    def _reorder(self, chunk: Chunk, expected: List[ColumnWithTypeAndName]) -> Chunk:
        """把子表 chunk 的列重排/补齐到 ``expected``(按名字)。"""
        columns = []
        for cwt in expected:
            pos = chunk.header.get_position_by_name(cwt.name)
            if pos < 0:
                column = cwt.type.create_column()
                column.append_many_default(chunk.num_rows)
            else:
                column = chunk.columns[pos]
            columns.append(column)
        return _make_chunk(expected, columns)

    # ── 写 ──────────────────────────────────────────────────────────────

    def write(self, ctx: Any, block: Block) -> None:
        raise_error(
            ErrorCode.TABLE_IS_READ_ONLY,
            f"Table {self.full_table_name()} is read-only (Merge is a virtual table)",
        )


def _factory(table, storage_id, context, engine_args, params) -> StorageMerge:
    return StorageMerge(table, storage_id, context, engine_args, params)


register_storage_engine(
    "Merge",
    _factory,
    description="把 db 下匹配正则的多张同构表拼成一张只读虚拟表。",
    aliases=("MERGE",),
)
