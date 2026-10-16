"""磁盘上的"最简单的三种引擎" —— ``TinyLog`` / ``Log`` / ``StripeLog``。

它们排在 ``MergeTree`` 之前讲,因为它们把"列式存储"剥到只剩骨架:
**一个文件存一列、追加写、没有索引**。理解了它们,再看 MergeTree 的
part / granule / mark 才有对照物。

.. code-block:: text

   TinyLog                     Log                        StripeLog
   ┌──────────────────┐        ┌──────────────────┐       ┌──────────────────┐
   │ a.bin            │        │ a.bin  a.mrk     │       │ data.bin         │
   │ b.bin            │        │ b.bin  b.mrk     │       │ index.mrk        │
   │ c.bin            │        │ c.bin  c.mrk     │       │ sizes.json       │
   └──────────────────┘        └──────────────────┘       └──────────────────┘
   一列一个文件                一列一个文件 + 自己的       所有列挤在一个
   整块一起写,无 mark          mark 文件,可独立读/写      data.bin 里,一条一条

三者对比(这张表就是本节最该记住的东西):

.. code-block:: text

   维度              TinyLog              Log                  StripeLog
   ───────────────  ───────────────────  ───────────────────  ───────────────────
   文件数           每列 1 个            每列 2 个            固定 3 个
   能不能只读一列   能(文件隔离)        能(文件隔离)        能(靠 sizes 跳过列)
   能不能并发读     弱(整块一起写)       **能**(列间独立)   能(单文件内按块)
   写放大           低                   低                   低(单文件顺序写)
   好搬运           差(文件多)           差(文件多)           **好(一个 data.bin)**
   崩溃后           最易留下一列比另一    比 TinyLog 略好      一个文件,最不易"半表"
                    列长(无 mark 校验)  (mark 可校验)       (但单文件损坏=全表)
   ───────────────  ───────────────────  ───────────────────  ───────────────────

**它们存什么?** 追加写的列数据。一次 ``INSERT`` 的数据 = 一批"块"(block),
每个块内部按列序列化后落盘。

**它们不能做什么?**
* 没有主键、没有稀疏索引 —— 任何 ``WHERE`` 都是全表扫描;
* 不能并行读之外的任何优化(没有分区、没有排序键、没有 skip index);
* ``features.append_only = True``:只允许追加,不允许就地改已有数据。

**什么时候该选它们(而不是 MergeTree)?**
* ``TinyLog``:数据量小、只写不常读的**日志/缓冲**文件(比如本地调试用);
* ``Log``:需要"边写边读"的小表 —— 每个列独立,读一列不会被其他列的写阻塞;
* ``StripeLog``:需要"整张表是一个文件"的场合(便于 ``scp`` / 备份 / 归档)。

**只要数据量上去了、或者要做范围查询,就必须换 MergeTree** —— 见第 04 章。

.. warning::

   **与真实 ClickHouse 的一处刻意差异(必须在报告里标出)。**

   真实的 TinyLog / Log 靠"读到文件结尾(EOF)"来判断一行行数据读完了没有,
   而且它们的 ``Serialization`` 支持**跨块流式反序列化**
   (``DeserializeBinaryBulkState``)。mini-clickhouse 的序列化简化掉了那个
   流式状态机,因此对 ``Nullable`` / ``Array`` 这种"分两段写"的类型,
   **必须按写它时的那个块边界来反序列化**,不能把多个块的字节拼起来一次读。

   带来的具体后果:我们给三种引擎都保留了"块索引"元信息——
   ``TinyLog`` 额外写一个 ``blocks.json``(真实 TinyLog 没有),
   ``Log`` 用 ``<col>.mrk``、``StripeLog`` 用 ``index.mrk`` + ``sizes.json``
   (后两者的文件名与真实一致,只是内容是我们自己定义的简化格式)。
   三个引擎的**文件布局与追加语义**与真实一致,唯独这份索引是教学实现的
   必要补充。第 05 章会拿真实 MergeTree 的 ``.mrk`` 对照这里的 mark。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ..types import DataType
from ._common import (
    _align_block,
    _check_chunk,
    _max_block_size,
    _read_json,
    _read_limit,
    _requested_columns,
    _resolve_data_path,
    _serialize_column,
    _settings_of,
    _slice_chunk,
    _stored_columns,
    _write_json,
)
from .base import IStorage, StorageFeatures, register_storage_engine

__all__ = ["StorageTinyLog", "StorageLog", "StorageStripeLog"]


#: 三个引擎共用的能力声明:只追加、无索引、无分区、无排序键。
_LOG_FEATURES = StorageFeatures(append_only=True, parallel_read=False)


class _LogFamilyStorage(IStorage):
    """``TinyLog`` / ``Log`` / ``StripeLog`` 的共同底座。

    子类需要实现三个"落盘单位"的方法:

    * :meth:`_append_aligned` —— 把一个对齐后的 block 追加到磁盘;
    * :meth:`_read_column` —— 读出某一列的**全部**数据(逐块拼接);
    * :meth:`_clear_files` —— 清空目录里的数据文件(``TRUNCATE``/重写用)。
    """

    features = _LOG_FEATURES

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        self._dir = _resolve_data_path(context, self.params, storage_id, self.engine_name)

    # ── 元信息 ──────────────────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        return _stored_columns(self.table)

    def size_in_bytes(self) -> int:
        total = 0
        for name in os.listdir(self._dir):
            path = os.path.join(self._dir, name)
            if os.path.isfile(path):
                total += os.path.getsize(path)
        return total

    def describe_stats(self) -> Dict[str, Any]:
        return {"path": self._dir, "rows": self.row_count()}

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        stored = self.get_columns()
        expected = _requested_columns(ctx, stored)
        if not expected or self.row_count() == 0:
            return
        columns = [self._read_column(c.name, c.type) for c in expected]
        total = columns[0].size() if columns else 0
        settings = _settings_of(ctx)
        block_size = _max_block_size(settings)
        limit = _read_limit(ctx)
        start = 0
        produced = 0
        while start < total:
            count = min(block_size, total - start)
            if limit is not None:
                count = min(count, limit - produced)
                if count <= 0:
                    break
            chunk = _slice_chunk(expected, columns, start, count)
            _check_chunk(chunk, expected)
            yield chunk
            start += count
            produced += count

    # ── 写 ──────────────────────────────────────────────────────────────

    def write(self, ctx: Any, block: Block) -> None:
        stored = [c for c in self.table.columns if c.is_stored()]
        aligned = _align_block(block, stored, strict=True, table_name=self.full_table_name())
        if aligned.rows() == 0:
            return
        with self._lock:
            self._append_aligned(aligned)

    # ── 变更(靠整体重写)─────────────────────────────────────────────

    def truncate(self) -> None:
        with self._lock:
            self._clear_files()

    def alter_add_column(self, column: Any, after: Optional[str] = None) -> None:
        data = self._read_all_block()
        super().alter_add_column(column, after)
        self._rewrite_from_block(data)

    def alter_drop_column(self, name: str) -> None:
        data = self._read_all_block()
        data.erase(name)
        super().alter_drop_column(name)
        self._rewrite_from_block(data)

    def alter_rename_column(self, old: str, new: str) -> None:
        data = self._read_all_block()
        src = data.try_get_by_name(old)
        super().alter_rename_column(old, new)
        if src is not None:
            data.erase(old)
            data.add_column(ColumnWithTypeAndName(new, src.type, src.column))
        self._rewrite_from_block(data)

    def alter_modify_column(self, name: str, new_type: DataType, default_expr: Any = None) -> None:
        from ._common import _convert_column

        data = self._read_all_block()
        src = data.try_get_by_name(name)
        super().alter_modify_column(name, new_type, default_expr)
        if src is not None:
            converted = _convert_column(src.column, new_type)
            data.erase(name)
            data.add_column(ColumnWithTypeAndName(name, new_type, converted))
        self._rewrite_from_block(data)

    # ── 内部工具 ────────────────────────────────────────────────────────

    def _read_all_block(self) -> Block:
        """把整表读成一个带数据的 Block(``ALTER`` 重写用)。"""
        out = Block([])
        for cwt in self.get_columns():
            column = self._read_column(cwt.name, cwt.type)
            out.add_column(ColumnWithTypeAndName(cwt.name, cwt.type, column))
        return out

    def _rewrite_from_block(self, block: Block) -> None:
        """用一个新的 Block **整体重写**磁盘上的数据(单块)。

        先把 block 对齐到**当前**表的物理列:``ALTER ADD COLUMN`` 之后新列
        会被补成默认值,``ALTER DROP COLUMN`` 之后多余的列会被丢掉。
        """
        stored = [c for c in self.table.columns if c.is_stored()]
        aligned = _align_block(block, stored, strict=False, table_name=self.full_table_name())
        with self._lock:
            self._clear_files()
            self._rewrite_block(aligned)

    def _rewrite_block(self, block: Block) -> None:
        raise_error(ErrorCode.NOT_IMPLEMENTED, "subclass must implement _rewrite_block")

    def _append_aligned(self, aligned: Block) -> None:
        raise_error(ErrorCode.NOT_IMPLEMENTED, "subclass must implement _append_aligned")

    def _read_column(self, name: str, dtype: DataType) -> Any:
        raise_error(ErrorCode.NOT_IMPLEMENTED, "subclass must implement _read_column")

    def _clear_files(self) -> None:
        raise_error(ErrorCode.NOT_IMPLEMENTED, "subclass must implement _clear_files")

    def shutdown(self) -> None:
        self._shutdown = True


# ══════════════════════════════════════════════════════════════════════════
# TinyLog —— 一列一个文件,整块一起写,没有 mark
# ══════════════════════════════════════════════════════════════════════════


class StorageTinyLog(_LogFamilyStorage):
    """``TinyLog`` —— 最简单的磁盘引擎。"""

    engine_name = "TinyLog"

    def _blocks_path(self) -> str:
        return os.path.join(self._dir, "blocks.json")

    def _col_path(self, name: str) -> str:
        return os.path.join(self._dir, f"{name}.bin")

    def _append_aligned(self, aligned: Block) -> None:
        rows = aligned.rows()
        # 所有列**一起**写 —— 这是 TinyLog 与 Log 的核心差别:写的时候
        # 所有列被一把锁串起来,所以它并发写得最慢。
        for cwt in aligned.data:
            with open(self._col_path(cwt.name), "ab") as fh:
                fh.write(_serialize_column(cwt.type, cwt.column))
        blocks = _read_json(self._blocks_path(), []) or []
        blocks.append(rows)
        _write_json(self._blocks_path(), blocks)

    def _row_blocks(self) -> List[int]:
        return list(_read_json(self._blocks_path(), []) or [])

    def row_count(self) -> int:
        return sum(self._row_blocks())

    def _read_column(self, name: str, dtype: DataType) -> Any:
        blocks = self._row_blocks()
        path = self._col_path(name)
        if not os.path.exists(path):
            column = dtype.create_column()
            column.append_many_default(sum(blocks))
            return column
        with open(path, "rb") as fh:
            data = fh.read()
        out = dtype.create_column()
        cursor = 0
        for rows in blocks:
            piece, cursor = _deserialize_block(dtype, data, rows, cursor)
            out.insert_range_from(piece, 0, rows)
        return out

    def _rewrite_block(self, block: Block) -> None:
        for cwt in block.data:
            with open(self._col_path(cwt.name), "wb") as fh:
                fh.write(_serialize_column(cwt.type, cwt.column))
        _write_json(self._blocks_path(), [block.rows()] if block.rows() else [])

    def _clear_files(self) -> None:
        for name in list(os.listdir(self._dir)):
            path = os.path.join(self._dir, name)
            if os.path.isfile(path):
                os.remove(path)


# ══════════════════════════════════════════════════════════════════════════
# Log —— 一列一个文件,每列**独立**写 + 自己的 mark
# ══════════════════════════════════════════════════════════════════════════


class StorageLog(_LogFamilyStorage):
    """``Log`` —— 每列独立写、可并发读。"""

    engine_name = "Log"

    def _col_path(self, name: str) -> str:
        return os.path.join(self._dir, f"{name}.bin")

    def _mrk_path(self, name: str) -> str:
        return os.path.join(self._dir, f"{name}.mrk")

    def _marks(self, name: str) -> List[List[int]]:
        return list(_read_json(self._mrk_path(name), []) or [])

    def _append_aligned(self, aligned: Block) -> None:
        rows = aligned.rows()
        # 每一列**独立**追加:各写各的文件、各记各的 mark —— 这就是
        # "读一个列的前缀不会被别的列的写阻塞" 的由来。
        for cwt in aligned.data:
            path = self._col_path(cwt.name)
            offset = os.path.getsize(path) if os.path.exists(path) else 0
            payload = _serialize_column(cwt.type, cwt.column)
            with open(path, "ab") as fh:
                fh.write(payload)
            marks = self._marks(cwt.name)
            marks.append([offset, rows])
            _write_json(self._mrk_path(cwt.name), marks)

    def row_count(self) -> int:
        for cwt in self.get_columns():
            marks = self._marks(cwt.name)
            if marks:
                return sum(m[1] for m in marks)
        return 0

    def _read_column(self, name: str, dtype: DataType) -> Any:
        marks = self._marks(name)
        out = dtype.create_column()
        path = self._col_path(name)
        if not marks or not os.path.exists(path):
            # 这一列从来没有被写过(比如刚好 ADD COLUMN 之后)—— 补默认值。
            out.append_many_default(self.row_count())
            return out
        with open(path, "rb") as fh:
            data = fh.read()
        for offset, rows in marks:
            piece, _ = _deserialize_block(dtype, data, rows, offset)
            out.insert_range_from(piece, 0, rows)
        return out

    def _rewrite_block(self, block: Block) -> None:
        for cwt in block.data:
            with open(self._col_path(cwt.name), "wb") as fh:
                fh.write(_serialize_column(cwt.type, cwt.column))
            _write_json(self._mrk_path(cwt.name), [[0, block.rows()]] if block.rows() else [])

    def _clear_files(self) -> None:
        for name in list(os.listdir(self._dir)):
            path = os.path.join(self._dir, name)
            if os.path.isfile(path):
                os.remove(path)


# ══════════════════════════════════════════════════════════════════════════
# StripeLog —— 所有列挤在**一个** data.bin 里
# ══════════════════════════════════════════════════════════════════════════


class StorageStripeLog(_LogFamilyStorage):
    """``StripeLog`` —— 单文件布局,最便于搬运。"""

    engine_name = "StripeLog"

    def _data_path(self) -> str:
        return os.path.join(self._dir, "data.bin")

    def _index_path(self) -> str:
        return os.path.join(self._dir, "index.mrk")

    def _sizes_path(self) -> str:
        return os.path.join(self._dir, "sizes.json")

    def _index(self) -> List[List[int]]:
        return list(_read_json(self._index_path(), []) or [])

    def _sizes(self) -> Dict[str, Any]:
        return _read_json(self._sizes_path(), {"rows": 0, "columns": [], "sizes": []}) or \
            {"rows": 0, "columns": [], "sizes": []}

    def _append_aligned(self, aligned: Block) -> None:
        rows = aligned.rows()
        names = [c.name for c in aligned.data]
        offset = os.path.getsize(self._data_path()) if os.path.exists(self._data_path()) else 0
        stripe_sizes: List[int] = []
        with open(self._data_path(), "ab") as fh:
            # 一条 stripe = 一个 block 里所有列,按列顺序依次写进同一个文件。
            for cwt in aligned.data:
                payload = _serialize_column(cwt.type, cwt.column)
                fh.write(payload)
                stripe_sizes.append(len(payload))
        index = self._index()
        index.append([offset, rows])
        _write_json(self._index_path(), index)

        meta = self._sizes()
        if not meta.get("columns"):
            meta["columns"] = names
        meta["rows"] = int(meta.get("rows", 0)) + rows
        meta.setdefault("sizes", []).append(stripe_sizes)
        _write_json(self._sizes_path(), meta)

    def row_count(self) -> int:
        return sum(rows for _, rows in self._index())

    def _read_column(self, name: str, dtype: DataType) -> Any:
        meta = self._sizes()
        names = list(meta.get("columns") or [])
        if name not in names:
            out = dtype.create_column()
            out.append_many_default(int(meta.get("rows", 0)))
            return out
        col_index = names.index(name)
        index = self._index()
        sizes = list(meta.get("sizes") or [])
        out = dtype.create_column()
        if not os.path.exists(self._data_path()):
            return out
        with open(self._data_path(), "rb") as fh:
            data = fh.read()
        for k, (stripe_offset, rows) in enumerate(index):
            stripe = sizes[k] if k < len(sizes) else []
            base = stripe_offset + sum(stripe[:col_index])
            length = stripe[col_index] if col_index < len(stripe) else 0
            piece, _ = _deserialize_block(dtype, data[base : base + length], rows, 0)
            out.insert_range_from(piece, 0, rows)
        return out

    def _rewrite_block(self, block: Block) -> None:
        rows = block.rows()
        names = [c.name for c in block.data]
        with open(self._data_path(), "wb") as fh:
            stripe_sizes = []
            for cwt in block.data:
                payload = _serialize_column(cwt.type, cwt.column)
                fh.write(payload)
                stripe_sizes.append(len(payload))
        _write_json(self._index_path(), [[0, rows]] if rows else [])
        _write_json(self._sizes_path(), {
            "rows": rows,
            "columns": names,
            "sizes": [stripe_sizes] if rows else [],
        })

    def _clear_files(self) -> None:
        for name in list(os.listdir(self._dir)):
            path = os.path.join(self._dir, name)
            if os.path.isfile(path):
                os.remove(path)


# ══════════════════════════════════════════════════════════════════════════
# 共用的单块反序列化
# ══════════════════════════════════════════════════════════════════════════


def _deserialize_block(dtype: DataType, data: bytes, rows: int, offset: int) -> tuple:
    """从 ``data[offset:]`` 解出一个 ``rows`` 行的**块**。

    返回 ``(列, 新偏移)`` —— 新偏移就是这一块占用的字节数累加,
    正好可以当作下一块的起点(见模块文档里"为什么必须按块读")。
    """
    from ..serialization import get_serialization

    ser = get_serialization(dtype)
    column, new_pos = ser.deserialize_binary(data, rows, offset)
    return column, new_pos


# ── 注册 ────────────────────────────────────────────────────────────────


def _tinylog_factory(table, storage_id, context, engine_args, params):
    return StorageTinyLog(table, storage_id, context, engine_args, params)


def _log_factory(table, storage_id, context, engine_args, params):
    return StorageLog(table, storage_id, context, engine_args, params)


def _stripelog_factory(table, storage_id, context, engine_args, params):
    return StorageStripeLog(table, storage_id, context, engine_args, params)


register_storage_engine(
    "TinyLog", _tinylog_factory,
    description="每列一个 .bin,整块一起写,无 mark;最小巧的磁盘引擎。",
    aliases=("TINYLOG",),
)
register_storage_engine(
    "Log", _log_factory,
    description="每列一个 .bin + .mrk,列间可独立写/并发读。",
    aliases=("LOG",),
)
register_storage_engine(
    "StripeLog", _stripelog_factory,
    description="所有列写进单个 data.bin,配 index.mrk + sizes.json,便于搬运。",
    aliases=("STRIPELOG",),
)
