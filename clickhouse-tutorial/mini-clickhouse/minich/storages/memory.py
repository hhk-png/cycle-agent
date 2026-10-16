"""``Memory`` 引擎 —— 数据**全在内存**,进程重启即失。

.. code-block:: text

   CREATE TABLE t (id UInt64, name String) ENGINE = Memory
                                        ^^^^^^^^^^^^^^
   内存里长这样(列存,不是行存):

     t._columns = {
        'id'   : ColumnVector(UInt64)  [1, 2, 3, …]
        'name' : ColumnString           ['a','b',…]
     }
     t._rows = 3

Memory 引擎**回答三个问题**:

1. **它存什么?**
   存"已经解析好的列对象",原封不动地放在进程内存里。没有文件、没有
   索引、没有分区、没有排序键 —— 一张表在内存里就是几个 ``IColumn``。

2. **它不能做什么?**
   * 不能持久化:进程一退(或 ``DETACH``/``DROP``)数据就没了 ——
     真实的 Memory 引擎**连服务端正常关闭都不保证**保住数据;
   * 没有主键索引,任何 ``WHERE`` 都是全表扫描;
   * 没有分区,不能按分区裁剪;
   * 不参与后台 merge,没有 TTL,没有稀疏索引。

3. **什么时候该选它(而不是 MergeTree)?**
   * **小维表 / 字典表**:几百到几万行、要反复 join / ``IN`` 的查找表。
     把它放进内存,省掉每一次查询的磁盘 IO,收益比"数据会不会丢"更重要
     (数据本来就能从上游重新灌);
   * **会话内临时结果**:``MEMORY`` 表在**同一个 session 内**跨查询可见,
     可以当成"比临时表更持久一点"的中间结果区;
   * **压测对照**:想量出"磁盘 vs 内存"的差距,用同一份数据分别建
     Memory 与 MergeTree 两张表跑同一条查询。

.. warning::

   **``TRUNCATE`` 不会把内存还给操作系统。** 这是真实 ClickHouse 的行为,
   也是很多"我 TRUNCATE 了怎么内存没降"疑问的答案:``TRUNCATE`` 释放的是
   ``ic_column`` 里的数据缓冲,但分配器(``jemalloc``)通常**不**把这块内存
   ``munmap`` 回 OS,而是留在自己的空闲池里等下一次分配复用。所以
   ``system.metrics`` 里的 ``MemoryTracking`` 会降,但 RSS 未必降。

   另外 ``max_memory_usage``(默认由服务端配置)对 Memory 表是**真的生效**的:
   往 Memory 表里灌超过配额的量会直接抛 ``MEMORY_LIMIT_EXCEEDED``,
   而不是像 MergeTree 那样把数据刷到磁盘。

.. note::

   mini-clickhouse 的 Memory 引擎把数据存成一个"名字 → 列"的字典,
   而不是一个 ``list[Block]``。这样 ``ALTER`` 系列操作(增删改名)只需要动
   字典,不需要逐 block 改 —— 与"内存引擎的元数据操作应当 O(1) / O(列数)"
   的直觉一致。真实 ClickHouse 的 ``StorageMemory`` 内部是一块
   ``BlocksList``(block 列表),我们在文档里标出这个差异。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ..types import DataType
from ._common import (
    _align_block,
    _check_chunk,
    _max_block_size,
    _read_limit,
    _requested_columns,
    _settings_of,
    _slice_chunk,
    _stored_columns,
)
from .base import IStorage, StorageFeatures, register_storage_engine

__all__ = ["StorageMemory"]


class StorageMemory(IStorage):
    """内存表引擎 —— 见模块文档。"""

    engine_name = "Memory"

    #: Memory 支持 ``ALTER ... UPDATE/DELETE``(靠重建),但**没有**索引/分区。
    features = StorageFeatures(mutations=True)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        #: 名字 → 列对象。缺的列在读取时用"默认值填充列"补上 ——
        #: 这让 ``ALTER ADD COLUMN`` 变成一次纯元数据操作(不用回填数据)。
        self._columns: Dict[str, Any] = {}
        #: 总行数。每个列对象都恰好有这么多行(缺失列在读取时才生成)。
        self._rows: int = 0

    # ── 元信息 ──────────────────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        """只返回**物理列**(ALIAS / MATERIALIZED 不落盘,引擎不该给它们找位置)。"""
        return _stored_columns(self.table)

    def row_count(self) -> int:
        # 这是一个"可以走捷径的接口":不必扫、不必数,直接返回维护好的计数。
        return self._rows

    def size_in_bytes(self) -> int:
        return sum(c.byte_size() for c in self._columns.values())

    def describe_stats(self) -> Dict[str, Any]:
        return {"rows": self._rows, "bytes": self.size_in_bytes(),
                "columns": len(self._columns)}

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        expected = _requested_columns(ctx, self.get_columns())
        if self._rows == 0 or not expected:
            return
        columns = [self._column_for(c.name, c.type) for c in expected]
        settings = _settings_of(ctx)
        block_size = _max_block_size(settings)
        limit = _read_limit(ctx)
        start = 0
        produced = 0
        while start < self._rows:
            count = min(block_size, self._rows - start)
            if limit is not None:
                count = min(count, limit - produced)
                if count <= 0:
                    break
            chunk = _slice_chunk(expected, columns, start, count)
            _check_chunk(chunk, expected)
            yield chunk
            start += count
            produced += count

    def _column_for(self, name: str, dtype: DataType) -> Any:
        """取某一列 —— 没有(刚 ``ADD COLUMN`` 进来)就造一个默认值填充列。"""
        column = self._columns.get(name)
        if column is None:
            column = dtype.create_column()
            column.append_many_default(self._rows)
            self._columns[name] = column
        return column

    # ── 写 ──────────────────────────────────────────────────────────────

    def write(self, ctx: Any, block: Block) -> None:
        stored = [c for c in self.table.columns if c.is_stored()]
        aligned = _align_block(block, stored, strict=True, table_name=self.full_table_name())
        with self._lock:
            rows = aligned.rows()
            if rows == 0:
                return
            # 先把"缺列"补齐到当前行数,再统一 append —— 保证所有列行数一致。
            for col_def in stored:
                self._column_for(col_def.name, col_def.type)
            for col_def in stored:
                src = aligned.get_by_name(col_def.name)
                self._columns[col_def.name].insert_range_from(src.column, 0, rows)
            self._rows += rows

    # ── 生命周期 ────────────────────────────────────────────────────────

    def truncate(self) -> None:
        """``TRUNCATE`` —— **O(1)**:直接丢掉列对象。

        注意(见模块文档):这不等于把内存还给操作系统。
        """
        with self._lock:
            self._columns = {}
            self._rows = 0

    def flush(self) -> None:
        # 内存引擎没有"更持久的位置"可刷 —— no-op。
        return

    def shutdown(self) -> None:
        """幂等关闭 —— Memory 没有后台线程,只做标记。"""
        if self._shutdown:
            return
        with self._lock:
            self._shutdown = True

    # ── ALTER ───────────────────────────────────────────────────────────

    def alter_add_column(self, column: Any, after: Optional[str] = None) -> None:
        super().alter_add_column(column, after)
        # 新列在物理上先不存在,读取时才按默认值补 —— 所以这里什么都不用做。
        # 但若同名旧列还在字典里(理论上不会),清掉它以免读到脏数据。
        with self._lock:
            self._columns.pop(column.name, None)

    def alter_drop_column(self, name: str) -> None:
        super().alter_drop_column(name)
        with self._lock:
            self._columns.pop(name, None)

    def alter_rename_column(self, old: str, new: str) -> None:
        super().alter_rename_column(old, new)
        with self._lock:
            column = self._columns.pop(old, None)
            if column is not None:
                self._columns[new] = column

    def alter_modify_column(self, name: str, new_type: DataType, default_expr: Any = None) -> None:
        super().alter_modify_column(name, new_type, default_expr)
        with self._lock:
            old_column = self._columns.get(name)
            if old_column is None:
                return
            # 逐值转换成新类型 —— 真实 ClickHouse 会开一个 mutation 把整列重写。
            converted = new_type.create_column()
            for i in range(old_column.size()):
                value = old_column.get(i)
                converted.append(None if value is None else new_type.normalize(value))
            self._columns[name] = converted

    def mutate(self, command: Any) -> str:
        """``ALTER ... UPDATE/DELETE`` —— **靠重建**(重建 = 换掉内存里的列)。

        引擎层不解析 SQL,也不做表达式求值(那是解释器的活)。所以它接受一个
        **已经把块映射到块**的可调用对象::

            def drop_old(block):           # block 是一批"带数据的列"
                keep = [block.get_by_name('ts').column.get(i) >= cutoff
                        for i in range(block.rows())]
                return block.filter(keep)

            engine.mutate(drop_old)

        ``command`` 也可以是一个对象,只要它有 ``apply(block) -> block`` 方法。
        其它形态一律 ``NOT_IMPLEMENTED`` —— 因为"没有解释器就求不了值"。
        """
        with self._lock:
            block = self._to_block()
            if callable(command):
                new_block = command(block)
            elif hasattr(command, "apply") and callable(command.apply):
                new_block = command.apply(block)
            else:
                raise_error(
                    ErrorCode.NOT_IMPLEMENTED,
                    "Memory.mutate expects a callable (or an object with apply()) "
                    "that maps a Block to a new Block; the engine does not evaluate "
                    "SQL predicates itself",
                )
            if not isinstance(new_block, Block):
                raise_error(
                    ErrorCode.LOGICAL_ERROR,
                    "The mutation callable must return a Block",
                )
            self._load_block(new_block)
            return f"memory-mutation-{self._rows}"

    # ── 内部 ────────────────────────────────────────────────────────────

    def _to_block(self) -> Block:
        """把内存里的列拼成一个带数据的 Block(用于 mutation 重建)。"""
        out = Block([])
        for col_def in self.table.columns:
            if not col_def.is_stored():
                continue
            column = self._column_for(col_def.name, col_def.type)
            out.add_column(ColumnWithTypeAndName(col_def.name, col_def.type, column))
        return out

    def _load_block(self, block: Block) -> None:
        """用一个新 Block **整体替换**内存数据(重建语义)。"""
        self._columns = {}
        for col_def in self.table.columns:
            if not col_def.is_stored():
                continue
            src = block.try_get_by_name(col_def.name)
            if src is None:
                self._columns[col_def.name] = col_def.type.create_column()
            else:
                self._columns[col_def.name] = src.column
        self._rows = block.rows()


# ── 注册 ────────────────────────────────────────────────────────────────


def _factory(table, storage_id, context, engine_args, params) -> StorageMemory:
    return StorageMemory(table, storage_id, context, engine_args, params)


register_storage_engine(
    "Memory",
    _factory,
    description="数据全部驻留内存,进程重启即失;最快的查找表引擎。",
    aliases=("MEMORY",),
)
