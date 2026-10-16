"""``File`` 引擎 —— **文件本身就是表**。

.. code-block:: text

   CREATE TABLE hits ENGINE = File(TSV, '/data/hits.tsv');
   SELECT * FROM hits;                 -- 读的时候解析 hits.tsv
   INSERT INTO hits VALUES (...);      -- 写的时候把**整个文件**重写一遍

**它存什么?** 存"一份外部格式文件的位置与格式"。真正的数据在文件里,
不在 ClickHouse 的任何内部布局里。

**它不能做什么?**
* **没有索引**:没有主键、没有分区、没有 skip index;
* **没有增量写**:每次 ``INSERT`` 都是"读回整个文件 → 合并新行 → 重写整个文件"
  —— 数据量一大就非常慢;
* 没有并发保护:两个写入者会互相覆盖;
* 文件被外部程序改坏时,读报的是格式层的解析错误。

**什么时候该选它(而不是 MergeTree)?**
"方便",而不是"性能/可靠"。典型场景:

* 把一份 CSV/TSV 临时挂进 ClickHouse 做一次查询(用完就删表);
* 与外部流水线交换文件(对方写 CSV,你用 ``File`` 表读);
* 教学 / 演示 —— 它把"格式 + 存储"这两件事解耦得很清楚(第 17、18 章)。

真要持久化 + 高频写,必须用 ``MergeTree``(见第 05 章)。

.. note::

   真实 ClickHouse 的 ``StorageFile`` 会**推断**格式(按文件扩展名)与结构
   (按文件头)。mini-clickhouse 里:**格式**支持按扩展名推断,
   但**结构**必须由建表语句给出(解释器把列定义放进 ``table.columns``),
   因为从任意文本里猜类型是另一大块工作(第 17 章)。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List, Optional

from ..block import Block, Chunk, ColumnWithTypeAndName, chunk_from_block
from ..catalog import ColumnDefinition
from ..errors import ErrorCode, raise_error
from ._common import (
    _check_chunk,
    _read_limit,
    _requested_columns,
    _settings_of,
    _stored_columns,
    get_input_format_compat,
    get_output_format_compat,
)
from .base import IStorage, StorageFeatures, register_storage_engine

__all__ = ["StorageFile"]

#: 按扩展名推断格式 —— 与真实 ClickHouse 的 `FormatFactory` 一个子集
_EXTENSION_FORMATS = {
    ".tsv": "TSV",
    ".tab": "TSV",
    ".csv": "CSV",
    ".json": "JSON",
    ".jsonl": "JSONEachRow",
    ".ndjson": "JSONEachRow",
    ".values": "Values",
}


def _looks_like_format(name: str) -> bool:
    """判断一个字符串是不是已注册(或有兜底)的格式名。"""
    try:
        get_input_format_compat(name)
        return True
    except Exception:
        return False


def _format_from_path(path: str) -> str:
    _, ext = os.path.splitext(path.lower())
    return _EXTENSION_FORMATS.get(ext, "TSV")


class StorageFile(IStorage):
    """文件表引擎 —— 见模块文档。"""

    engine_name = "File"

    #: 没有索引;append_only 为 False(插入是整文件重写,不是纯追加)。
    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        args = [a if isinstance(a, str) else getattr(a, "value", getattr(a, "name", None)) or str(a)
                for a in (engine_args or [])]
        path: Optional[str] = None
        fmt: Optional[str] = None
        for token in args:
            if fmt is None and _looks_like_format(token):
                fmt = token
            elif path is None:
                path = token
            elif fmt is None:
                fmt = token
        self.path: str = self.params.get("path") or path or ""
        self.format: str = self.params.get("format") or fmt or (
            _format_from_path(self.path) if self.path else "TSV"
        )
        #: 建表没写列定义时,从文件头推断出来的结构(只推断一次)。
        self._inferred: Optional[List[ColumnWithTypeAndName]] = None

    # ── 元信息 ──────────────────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        stored = _stored_columns(self.table)
        if stored:
            return stored
        if self._inferred is None:
            self._inferred = self._infer_columns()
        return self._inferred

    def _infer_columns(self) -> List[ColumnWithTypeAndName]:
        """``CREATE TABLE t ENGINE = File(...)`` 没写列定义时,从文件头推断。

        真实 ClickHouse 对带列名的格式(``CSVWithNames`` / ``JSON`` / …)
        会这么干。推断成功后同时补上 ``table.columns``,这样 ``INSERT`` 也知道
        该写哪些列。
        """
        if not self.path or not os.path.exists(self.path):
            return []
        try:
            reader = get_input_format_compat(self.format)
            with open(self.path, "r", encoding="utf-8", newline="") as fh:
                text = fh.read()
            for block in reader.read(text, Block([]), None):
                columns = [c.clone_empty() for c in block.data]
                self.table.columns = [ColumnDefinition(c.name, c.type) for c in columns]
                return columns
        except Exception:
            return []
        return []

    def get_name(self) -> str:
        return f"File({self.format}, '{self.path}')"

    def row_count(self) -> int:
        # File 表没有维护行数 —— 只能真读一遍(这正是"没有元数据"的代价)。
        n = 0
        for chunk in self.read(None):
            n += chunk.num_rows
        return n

    def size_in_bytes(self) -> int:
        return os.path.getsize(self.path) if self.path and os.path.exists(self.path) else 0

    def describe_stats(self) -> Dict[str, Any]:
        return {"path": self.path, "format": self.format,
                "exists": bool(self.path and os.path.exists(self.path))}

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        expected = _requested_columns(ctx, self.get_columns())
        if not expected:
            return
        if not self.path or not os.path.exists(self.path):
            # 文件不存在 → 空表(写的时候才会创建)。
            return
        settings = _settings_of(ctx)
        fmt = get_input_format_compat(self.format)
        sample = Block([c.clone_empty() for c in expected])
        with open(self.path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        limit = _read_limit(ctx)
        produced = 0
        for block in fmt.read(text, sample, settings):
            chunk = chunk_from_block(block)
            if limit is not None:
                if produced >= limit:
                    return
                remaining = limit - produced
                if chunk.num_rows > remaining:
                    chunk = chunk.slice(0, remaining)
            _check_chunk(chunk, expected)
            produced += chunk.num_rows
            yield chunk

    # ── 写 ──────────────────────────────────────────────────────────────

    def write(self, ctx: Any, block: Block) -> None:
        if not self.path:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"File engine for {self.full_table_name()} has no path",
            )
        expected = self.get_columns()
        # 1) 读回已有内容(如果文件存在)
        existing = Block([])
        if os.path.exists(self.path):
            for chunk in self.read(None):
                existing.merge(chunk.get_block_with_data())
        # 2) 合并新行
        incoming = Block([])
        for cwt in expected:
            src = block.try_get_by_name(cwt.name)
            if src is None:
                column = cwt.type.create_column()
                column.append_many_default(block.rows())
            else:
                column = src.column
            incoming.add_column(ColumnWithTypeAndName(cwt.name, cwt.type, column))
        existing.merge(incoming)
        # 3) 整文件重写
        settings = _settings_of(ctx)
        fmt = get_output_format_compat(self.format)
        out = _StringWriter()
        fmt.write_prefix(existing, out, settings)
        fmt.write(existing, out, settings)
        fmt.write_suffix(existing, out, settings)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(out.getvalue())

    def truncate(self) -> None:
        if self.path and os.path.exists(self.path):
            open(self.path, "w", encoding="utf-8").close()

    def shutdown(self) -> None:
        self._shutdown = True


class _StringWriter:
    """格式输出器需要的最小可写文本流。"""

    __slots__ = ("_parts",)

    def __init__(self) -> None:
        self._parts: List[str] = []

    def write(self, text: str) -> int:
        self._parts.append(text)
        return len(text)

    def getvalue(self) -> str:
        return "".join(self._parts)


def _factory(table, storage_id, context, engine_args, params) -> StorageFile:
    return StorageFile(table, storage_id, context, engine_args, params)


register_storage_engine(
    "File",
    _factory,
    description="把一份外部格式文件当成表;读解析、写整文件重写(无索引,不用于持久化)。",
    aliases=("FILE",),
)
