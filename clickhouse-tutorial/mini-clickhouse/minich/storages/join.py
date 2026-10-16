"""``Join`` 引擎 —— 一张**预建好的哈希表**,专供 ``joinGet`` 与 ``JOIN`` 用。

.. code-block:: text

   CREATE TABLE id_to_name (id UInt64, name String)
   ENGINE = Join(ANY, LEFT, id);
   INSERT INTO id_to_name VALUES (1,'alice'), (2,'bob');
   SELECT joinGet('id_to_name', 'name', 2);       -- → 'bob'

**它存什么?** 一个内存里的哈希表:``join 键 → 命中的行``。键来自建表时
声明的 ``key`` 列,值就是整行。

**它不能做什么?**
* 不能持久化(进程重启即失,和 Memory 一样);
* 不能有索引/分区/排序;
* 语义上它是**右表**:真正的匹配逻辑(LEFT/INNER/``ANY`` 取第一条)发生在
  join 执行器里,引擎只负责"给定一个键,把候选行吐出来"。

**什么时候该选它(而不是 MergeTree)?**
"维表 join"的经典优化:一张几十万行的维表被反复 join 成千上万次,
每次都从磁盘读一遍太浪费。``Join`` 引擎把维表**常驻内存 + 预建哈希**,
于是 ``joinGet`` 与 ``JOIN`` 都变成一次哈希查找。

.. code-block:: text

   strictness × kind —— 两个**正交**的维度(第 13 章):

     strictness  含义
     ANY         键命中多行时**只取第一行**(ClickHouse 最常用,宽表 join 首选)
     ALL         键命中多行时**全部保留**(笛卡尔,可能放大行数)
     SEMI / ANTI 只看"有没有匹配",不复制右表的列

     kind        含义
     INNER / LEFT / RIGHT / FULL / CROSS

   建表时的 ``Join(ANY, LEFT, id)`` 就是"strictness = ANY,kind = LEFT,
   键列 = id"。``joinGet`` 只支持 ``ANY`` —— 因为它要返回**一个**值。

.. note::

   真实 ClickHouse 的 ``joinGet`` 还有一个 ``joinGetOrNull`` 变体:找不到键时
   是返回该类型的默认值(``joinGet``)还是 NULL(``joinGetOrNull``)。
   mini-clickhouse 把这两者统一成"带 ``default`` 参数的方法":默认返回 None,
   调用方需要零值就传 ``default=``。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ._common import (
    _align_block,
    _check_chunk,
    _make_chunk,
    _max_block_size,
    _read_limit,
    _requested_columns,
    _settings_of,
    _stored_columns,
)
from .base import IStorage, StorageFeatures, register_storage_engine

__all__ = ["StorageJoin", "join_get"]

#: 合法的严格度 / 方向(供参数校验与错误消息使用)
_STRICTNESSES = ("ANY", "ALL", "ASOF", "SEMI", "ANTI")
_KINDS = ("LEFT", "INNER", "RIGHT", "FULL", "CROSS", "PASTE")


def _arg_str(arg: Any) -> str:
    """把引擎参数(可能是字符串,也可能是 AST 节点)转成字符串。"""
    if isinstance(arg, str):
        return arg
    for attr in ("name", "value"):
        value = getattr(arg, attr, None)
        if isinstance(value, str):
            return value
    return str(arg)


class StorageJoin(IStorage):
    """内存哈希表引擎 —— 见模块文档。"""

    engine_name = "Join"

    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        args = [_arg_str(a) for a in (engine_args or [])]
        # 前两个参数(如果像)分别是 strictness 与 kind;其余都是键列名。
        strictness = "ANY"
        kind = "LEFT"
        keys: List[str] = []
        for arg in args:
            upper = arg.upper()
            if not keys and upper in _STRICTNESSES and strictness == "ANY" and not keys:
                strictness = upper
                continue
            if not keys and upper in _KINDS:
                kind = upper
                continue
            keys.append(arg)
        self.strictness = strictness
        self.kind = kind
        #: 键列名。空的话退化成"用全部物理列" —— 宽松处理,便于测试。
        self.key_columns: List[str] = keys or [c.name for c in _stored_columns(table)]
        #: 键元组 → 命中行(每行是一个 名字→值 的 dict)。
        self._hash: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
        self._count = 0

    # ── 元信息 ──────────────────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        return _stored_columns(self.table)

    def get_name(self) -> str:
        params = [self.strictness, self.kind] + self.key_columns
        return f"{self.engine_name}({', '.join(params)})"

    def row_count(self) -> int:
        return self._count

    def size_in_bytes(self) -> int:
        total = 0
        for _, rows in self._hash.items():
            for row in rows:
                total += sum(16 if not isinstance(v, str) else len(v) for v in row.values())
        return total

    def describe_stats(self) -> Dict[str, Any]:
        return {
            "rows": self._count,
            "keys": len(self._hash),
            "strictness": self.strictness,
            "kind": self.kind,
        }

    # ── 写 ──────────────────────────────────────────────────────────────

    def write(self, ctx: Any, block: Block) -> None:
        stored = [c for c in self.table.columns if c.is_stored()]
        aligned = _align_block(block, stored, strict=True, table_name=self.full_table_name())
        names = [c.name for c in aligned.data]
        columns = {name: aligned.get_by_name(name).column for name in names}
        with self._lock:
            for i in range(aligned.rows()):
                row = {name: columns[name].get(i) for name in names}
                key = tuple(row.get(k) for k in self.key_columns)
                self._hash.setdefault(key, []).append(row)
                self._count += 1

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        expected = _requested_columns(ctx, self.get_columns())
        if not expected or self._count == 0:
            return
        # 把哈希表摊平成一个行序列,再按列拼回去。
        all_rows: List[Dict[str, Any]] = []
        for rows in self._hash.values():
            all_rows.extend(rows)
        settings = _settings_of(ctx)
        block_size = _max_block_size(settings)
        limit = _read_limit(ctx)
        start = 0
        produced = 0
        while start < len(all_rows):
            count = min(block_size, len(all_rows) - start)
            if limit is not None:
                count = min(count, limit - produced)
                if count <= 0:
                    break
            columns = []
            for cwt in expected:
                column = cwt.type.create_column()
                for row in all_rows[start : start + count]:
                    column.append(row.get(cwt.name))
                columns.append(column)
            chunk = _make_chunk(expected, columns)
            _check_chunk(chunk, expected)
            yield chunk
            start += count
            produced += count

    # ── joinGet ─────────────────────────────────────────────────────────

    def _key_tuple(self, key: Any) -> Tuple[Any, ...]:
        if isinstance(key, tuple):
            if len(self.key_columns) == 1 and len(key) == 1:
                return key
            return key
        return (key,)

    def lookup(self, key: Any) -> List[Dict[str, Any]]:
        """按 join 键查候选行 —— join 执行器与 ``joinGet`` 都走这里。"""
        return self._hash.get(self._key_tuple(key), [])

    def join_get(self, attr: str, key: Any, default: Any = None) -> Any:
        """``joinGet(table, attr, key)`` 的落地。

        ``ANY`` 严格度返回**第一**个命中行的 ``attr``(这正是 ``joinGet``
        的数据模型);``ALL`` 返回所有命中行的列表。找不到时返回 ``default``。
        """
        rows = self.lookup(key)
        if not rows:
            return default
        if self.strictness == "ALL":
            return [row.get(attr) for row in rows]
        return rows[0].get(attr)

    # ── 生命周期 ────────────────────────────────────────────────────────

    def truncate(self) -> None:
        with self._lock:
            self._hash = {}
            self._count = 0

    def shutdown(self) -> None:
        self._shutdown = True


def join_get(table: Any, attr: str, key: Any, default: Any = None) -> Any:
    """模块级 ``joinGet`` —— ``table`` 可以是 :class:`minich.catalog.Table`
    或 :class:`StorageJoin` 本身。

    真实 ClickHouse 里 ``joinGet('db.t', 'attr', key)`` 要求 ``t`` 用
    ``Join`` 引擎建表;这里做同样的检查。
    """
    engine = getattr(table, "engine", table)
    if not isinstance(engine, StorageJoin):
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"joinGet requires a Join table, got "
            f"{getattr(engine, 'engine_name', type(engine).__name__)}",
        )
    if attr not in [c.name for c in engine.get_columns()]:
        raise_error(
            ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
            f"No such column '{attr}' in join table {engine.full_table_name()}",
        )
    return engine.join_get(attr, key, default)


def _factory(table, storage_id, context, engine_args, params) -> StorageJoin:
    return StorageJoin(table, storage_id, context, engine_args, params)


register_storage_engine(
    "Join",
    _factory,
    description="内存哈希表,支持 joinGet(table, attr, key) 与预建 JOIN 右表。",
    aliases=("JOIN",),
)
