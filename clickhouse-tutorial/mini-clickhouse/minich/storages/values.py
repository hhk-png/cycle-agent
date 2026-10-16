"""``Values`` 引擎 —— 把一段字面量数据当成一张表。

.. code-block:: text

   CREATE TABLE cities ENGINE = Values('name String', 'zip UInt32');
   -- 解释器随后把数据"贴"上去:
   --   engine.attach_rows(block_from_rows([...]))

**它存什么?** 一份**已经解析好的** Block(列数据)。数据来源不是磁盘、
不是网络,而是 SQL 文本里的一串字面量(``VALUES`` / ``SELECT`` 的结果)。

**它不能做什么?**
* 不能持久化 —— 数据随语句生命周期存在内存里;
* 不能 ``INSERT`` 追加(它是"只读的一次性数据");
* 没有索引/分区/排序。

**什么时候该选它(而不是 MergeTree)?**
几乎不用它建持久表。它真正出现的场合是**表函数**:
``SELECT * FROM values('a UInt8', (1), (2))`` 或 ``(SELECT ...)`` 内联成子表。
所以引擎这里的职责很简单:提供 ``attach_rows`` / ``set_data`` 两个接口,
让解释器把解析好的数据挂上来、再 ``read`` 出去。

.. note::

   真实 ClickHouse 的实现里,``Values`` 引擎对应 ``StorageValues``,它
   持有一个 ``Values``(一个"列 → 数据向量"的映射)。mini-clickhouse 直接
   复用了 :class:`minich.block.Block`,因为它本来就是"成批的同构列"。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..catalog import ColumnDefinition
from ..errors import ErrorCode, raise_error
from ._common import (
    _align_block,
    _check_chunk,
    _make_chunk,
    _max_block_size,
    _parse_structure,
    _read_limit,
    _requested_columns,
    _settings_of,
    _stored_columns,
)
from .base import IStorage, StorageFeatures, register_storage_engine

__all__ = ["StorageValues"]


class StorageValues(IStorage):
    """字面量表 —— 见模块文档。"""

    engine_name = "Values"

    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        #: 引擎参数就是"结构字符串":Values('a UInt8', 'b String')。
        self.structure = [str(a) for a in (engine_args or [])]
        self._data: Optional[Block] = None
        if not _stored_columns(table) and self.structure:
            # 建表语句没写列定义 —— 从引擎参数里补出来。
            self._define_columns(self.structure)

    def _define_columns(self, structure: List[str]) -> None:
        columns: List[ColumnDefinition] = []
        for raw in structure:
            for cwt in _parse_structure(raw):
                columns.append(ColumnDefinition(cwt.name, cwt.type))
        self.table.columns = columns

    # ── 数据挂载(解释器调用)─────────────────────────────────────────

    def attach_rows(self, block: Block) -> None:
        """把一批行挂到这张虚拟表上(数据已按表结构解析好)。"""
        stored = [c for c in self.table.columns if c.is_stored()]
        self._data = _align_block(block, stored, strict=False,
                                  table_name=self.full_table_name())

    #: ``set_data`` 是 ``attach_rows`` 的别名,便于调用方按语义选名字。
    set_data = attach_rows

    # ── 元信息 ──────────────────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        return _stored_columns(self.table)

    def row_count(self) -> int:
        return self._data.rows() if self._data is not None else 0

    def size_in_bytes(self) -> int:
        if self._data is None:
            return 0
        return sum(c.column.byte_size() for c in self._data.data)

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        if self._data is None or self._data.rows() == 0:
            return
        expected = _requested_columns(ctx, self.get_columns())
        if not expected:
            return
        settings = _settings_of(ctx)
        block_size = _max_block_size(settings)
        limit = _read_limit(ctx)
        total = self._data.rows()
        start = 0
        produced = 0
        while start < total:
            count = min(block_size, total - start)
            if limit is not None:
                count = min(count, limit - produced)
                if count <= 0:
                    break
            columns = []
            for cwt in expected:
                src = self._data.try_get_by_name(cwt.name)
                column = cwt.type.create_column()
                if src is not None:
                    column.insert_range_from(src.column, start, count)
                else:
                    column.append_many_default(count)
                columns.append(column)
            chunk = _make_chunk(expected, columns)
            _check_chunk(chunk, expected)
            yield chunk
            start += count
            produced += count

    # ── 写 ──────────────────────────────────────────────────────────────

    def write(self, ctx: Any, block: Block) -> None:
        raise_error(
            ErrorCode.TABLE_IS_READ_ONLY,
            f"Table {self.full_table_name()} is read-only (Values is filled once "
            f"by the interpreter via attach_rows)",
        )

    def truncate(self) -> None:
        self._data = None

    def shutdown(self) -> None:
        self._shutdown = True


def _factory(table, storage_id, context, engine_args, params) -> StorageValues:
    return StorageValues(table, storage_id, context, engine_args, params)


register_storage_engine(
    "Values",
    _factory,
    description="把一段字面量(VALUES)当成表;数据由解释器 attach_rows 挂载。",
    aliases=("VALUES",),
)
