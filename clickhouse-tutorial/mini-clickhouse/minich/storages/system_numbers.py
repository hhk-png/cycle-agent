"""``system.numbers`` —— 一张**无限**的系统表。

.. code-block:: text

   SELECT * FROM system.numbers LIMIT 5;              -- 0,1,2,3,4
   SELECT count() FROM system.numbers LIMIT 1000000;  -- 1000000,不物化

``system.numbers`` 与表函数 ``numbers(...)`` 生成的东西一模一样,区别只在
**参数**:

* ``numbers(10)`` —— 有界区间,参数写死;
* ``system.numbers`` —— **无界**,``0,1,2,3,…`` 一直流下去,必须配 ``LIMIT``
  才有意义(否则这条查询永远不返回)。

这就是为什么它在 ``system`` 库里、而且是一个**表**(可以直接 ``FROM``),
而不是一个函数 —— 函数调用要写参数,而它没有"结束"这个参数。

.. code-block:: text

   system.numbers 的读取:

     InterpreterSelectQuery
        │  ① 看到 FROM system.numbers
        │  ② 注意到 LIMIT 5,下推到 ReadContext.limit = 5
        ▼
     StorageSystemNumbers.read(ctx)  ── 只产出 5 行就停 ──▶ 上游

   **没有 LIMIT 时**:它会一直产出块,直到上游主动停止(比如外面还有
   别的 LIMIT,或者客户端取消)。mini-clickhouse 如实照做 ——
   ``read()`` 是个**无限生成器**。所以请永远给它配 ``LIMIT``。

.. note::

   真实 ClickHouse 里 ``system.numbers`` 与 ``system.numbers_mt`` 是两张表
   (后者多线程)。mini-clickhouse 用同一个存储类,靠 ``mt`` 参数区分,
   但**两者都单线程执行**(GIL 所限)—— 见 :mod:`minich.storages.numbers`
   的说明。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..catalog import ColumnDefinition, StorageID, Table
from ..errors import ErrorCode, raise_error
from ._common import _max_block_size, _read_limit, _settings_of
from .base import IStorage, StorageFeatures
from .numbers import NUMBERS_COLUMN, NUMBERS_TYPE, NumbersSource

__all__ = ["StorageSystemNumbers", "make_system_numbers_table", "register_system_numbers"]

#: 没有 LIMIT 时用的"看似无限"的边界 —— 足够大到实际等价于无限,
#: 又不会让 `count()` 的整数运算失去意义。
_HUGE = 1 << 62


class StorageSystemNumbers(IStorage):
    """``system.numbers`` / ``system.numbers_mt`` 的存储实现。"""

    engine_name = "SystemNumbers"

    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        self.mt: bool = bool(self.params.get("mt", False))
        #: 可选的固定上界(用于 ``system.numbers`` 的"限量"变体)。
        self.limit: Optional[int] = self.params.get("limit")

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        return [ColumnWithTypeAndName(NUMBERS_COLUMN, NUMBERS_TYPE)]

    def get_name(self) -> str:
        return "SystemNumbers" + ("_mt" if self.mt else "")

    def row_count(self) -> int:
        """无界表没有"行数" —— 返回 0(未知),不是"空表"。

        真实 ClickHouse 的 ``system.numbers`` 在 ``system.tables`` 里的
        ``total_rows`` 也是空的。
        """
        return 0

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        settings = _settings_of(ctx)
        block_size = _max_block_size(settings)
        limit = _read_limit(ctx)
        if limit is None:
            limit = self.limit
        end = _HUGE if limit is None else min(_HUGE, limit)
        source = NumbersSource(0, end, 1)
        yield from source.iter_chunks(block_size, limit=None if limit is None else limit)

    def write(self, ctx: Any, block: Block) -> None:
        raise_error(
            ErrorCode.TABLE_IS_READ_ONLY,
            f"Table {self.full_table_name()} is read-only (system.numbers is generated)",
        )

    def shutdown(self) -> None:
        self._shutdown = True


def make_system_numbers_table(name: str = "numbers", mt: bool = False) -> Table:
    """造一张 ``system.numbers``(或 ``numbers_mt``)的 :class:`Table`。

    它自己不建目录、不落盘 —— 只是把 :class:`StorageSystemNumbers` 包一层
    元数据,这样 ``FROM system.numbers`` 能像普通表一样被解析。
    """
    storage_id = StorageID("system", name)
    columns = [ColumnDefinition(NUMBERS_COLUMN, NUMBERS_TYPE)]
    table = Table(storage_id, columns)
    engine = StorageSystemNumbers(
        table, storage_id, context=None, engine_args=[], params={"mt": mt}
    )
    table.engine = engine
    return table


def register_system_numbers(catalog: Any) -> None:
    """把 ``system.numbers`` 与 ``system.numbers_mt`` 注册进目录(如果还没有)。"""
    system = catalog.try_get_database("system")
    if system is None:
        return
    if system.get_table("numbers") is None:
        system.add_table(make_system_numbers_table("numbers", mt=False))
    if system.get_table("numbers_mt") is None:
        system.add_table(make_system_numbers_table("numbers_mt", mt=True))
