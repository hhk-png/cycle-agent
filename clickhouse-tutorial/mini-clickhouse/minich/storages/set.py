"""``Set`` 引擎 —— 只保留**第一列的互异值**。

.. code-block:: text

   CREATE TABLE allowed_ids (id UInt64) ENGINE = Set
   INSERT INTO allowed_ids VALUES (1), (2), (2), (3)      -- 存进去的只有 {1,2,3}
   SELECT * FROM big WHERE user_id IN allowed_ids         -- IN 的半连接

**它存什么?** 只存**第一列**的去重值,放在一个哈希集合里。

**它不能做什么?**
* 不能有第二列 —— 多出来的列会被忽略(真实 ClickHouse 直接报错);
* **不保留重复与顺序**:插了两次 ``2`` 只会留下一个 ``2``,而且读出来的
  顺序**不保证**与插入顺序一致(真实 CH 的读顺序是按值排序的);
* 不能做 ``ALTER``、不能参与 ``JOIN``、不支持有意义的 ``SELECT *`` ——
  它的存在意义就是"给 ``IN`` 用"。

**什么时候该选它(而不是 MergeTree)?**
就是 ``IN`` 优化本身。把"允许的取值"放进一张 ``Set`` 表,查询里的
``x IN (SELECT …)`` 或 ``x IN set_table`` 就能走**半连接**:
只把集合读进内存,然后逐行做 O(1) 的成员测试 —— 与逐行建哈希表相比,
集合只建一次、可复用(第 13 章)。

.. warning::

   **内存有上限。** ``max_rows_in_set``(默认 0 = 不限,生产上常设成
   25 万)约束的就是这张表:超过就直接抛 ``TOO_MANY_ROWS``。
   ``Set`` 引擎本身**完全驻留内存**,一个几千万行的 ``Set`` 会把内存吃光。

.. note::

   真实 ClickHouse 的 ``Set`` 引擎会把值**排序后落在磁盘**上
   (``Set`` 是 ``IMergeTreeDataPart`` 的一层封装,底层是持久的),
   并且支持 ``Set`` 的 ``persistent`` 参数控制"是否复用上一次的集合结果"。
   mini-clickhouse 只做**内存**里的去重集合 —— 语义(成员测试)一致,
   持久化那一层省掉,并在报告里标出这个差异。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..errors import ErrorCode, error_code, raise_error
from ._common import (
    _check_chunk,
    _make_chunk,
    _max_block_size,
    _requested_columns,
    _setting,
    _settings_of,
    _stored_columns,
)
from .base import IStorage, StorageFeatures, register_storage_engine

__all__ = ["StorageSet"]


class StorageSet(IStorage):
    """集合表 —— 见模块文档。"""

    engine_name = "Set"

    #: 无索引、无分区、不支持 mutation。它本质上是一个"半连接右表"。
    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        #: 用 dict 而不是 set —— 既要"去重"又要"稳定顺序",dict 一手包办。
        self._values: Dict[Any, None] = {}

    # ── 元信息 ──────────────────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        """``Set`` 表只有**第一列**有物理意义。"""
        stored = _stored_columns(self.table)
        return stored[:1]

    def row_count(self) -> int:
        return len(self._values)

    def size_in_bytes(self) -> int:
        columns = self.get_columns()
        if not columns:
            return 0
        return len(self._values) * columns[0].type.get_size_of_value_in_memory()

    def describe_stats(self) -> Dict[str, Any]:
        return {"rows": len(self._values), "max_rows_in_set": self._max_rows()}

    def _max_rows(self) -> int:
        settings = _settings_of(self.context)
        value = _setting(settings, "max_rows_in_set", 0)
        try:
            return int(value)
        except Exception:
            return 0

    # ── 写 ──────────────────────────────────────────────────────────────

    def write(self, ctx: Any, block: Block) -> None:
        if block is None or block.columns() == 0:
            return
        first = block.data[0]
        if block.columns() > 1:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"ENGINE = Set stores only the first column, but {block.columns()} "
                f"were inserted",
            )
        column = first.column
        limit = self._max_rows()
        settings = _settings_of(ctx) or _settings_of(self.context)
        limit_setting = _setting(settings, "max_rows_in_set", limit)
        try:
            limit = int(limit_setting)
        except Exception:
            pass
        with self._lock:
            for i in range(column.size()):
                value = column.get(i)
                if value in self._values:
                    continue  # 静默去重 —— 与真实 CH 一致,不报错、不计数
                if limit and len(self._values) >= limit:
                    raise_error(
                        error_code("TOO_MANY_ROWS"),
                        f"Set has more than max_rows_in_set rows ({limit})",
                    )
                self._values[value] = None

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        expected = _requested_columns(ctx, self.get_columns())
        if not expected or not self._values:
            return
        dtype = expected[0].type
        settings = _settings_of(ctx)
        block_size = _max_block_size(settings)
        values = list(self._values.keys())
        header = [expected[0]]
        for start in range(0, len(values), block_size):
            column = dtype.create_column()
            column.append_many(values[start : start + block_size])
            chunk = _make_chunk(header, [column])
            _check_chunk(chunk, header)
            yield chunk

    # ── 成员测试(供 IN / 半连接使用)───────────────────────────────────

    def contains(self, value: Any) -> bool:
        """O(1) 成员测试 —— 这就是 ``Set`` 引擎存在的全部理由。"""
        return value in self._values

    # ── 生命周期 ────────────────────────────────────────────────────────

    def truncate(self) -> None:
        with self._lock:
            self._values = {}

    def shutdown(self) -> None:
        self._shutdown = True


def _factory(table, storage_id, context, engine_args, params) -> StorageSet:
    return StorageSet(table, storage_id, context, engine_args, params)


register_storage_engine(
    "Set",
    _factory,
    description="只保留第一列的去重值,用于 IN 的半连接优化。",
    aliases=("SET",),
)
