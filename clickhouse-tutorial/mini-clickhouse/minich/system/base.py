"""``system`` 库的地基 —— 表接口、注册表、以及"惰性分块"的公共设施。

真实 ClickHouse 里 ``system.*`` 不是"一堆硬编码的假表",而是**真正的
``IStorage`` 实现**(``src/Storages/System/StorageSystem*.cpp``)。也就是说:

.. code-block:: text

   SELECT * FROM system.tables;
        │
        ▼
   StorageSystemTables::read()   ← 和其他存储引擎**同一个接口**
        │  产出 Chunk(列式)
        ▼
   解释器认为它和 MergeTree 没有任何区别

这样设计的好处是:``system`` 表能被 JOIN、能被 GROUP BY、能被下推谓词,
**不需要给解释器开任何后门**。mini-clickhouse 沿用这个模型,只是把
实现换成纯 Python 的函数。

.. code-block:: text

   minich/system/base.py            ← 接口与注册表(本文件)
        │
        ├─ ISystemTable              name / get_header / get_chunks
        │     └─ SimpleSystemTable   给一张 (列定义, 行提供函数) 就够
        │           └─ NumbersSystemTable  system.numbers 的无限流
        │
        ├─ registry() / system_table_names() / is_system_table()
        ├─ get_system_table_header(...)   零行 Block(只有列名 + 类型)
        └─ get_system_table_chunks(...)   流式产出 Chunk

**两条硬约束**(整个包都遵守,违反会在测试里立刻暴露):

1. ``get_chunks`` 必须是**惰性**的 —— 不能先把所有行塞进一个 list 再
   yield。``system.query_log`` 在生产上可能有几十万行,物化它等于把
   内存打爆(真实 ClickHouse 也是流式读的)。
2. 每个 chunk 的行数**不能超过** ``max_block_size`` —— 与所有其它存储
   引擎一致,否则下游处理器(尤其是聚合)会看到"超大的块",
   缓存局部性与内存预估全部失效。
"""

from __future__ import annotations

import difflib
from typing import Any, Callable, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type

__all__ = [
    "ISystemTable",
    "SimpleSystemTable",
    "NumbersSystemTable",
    "register_system_table",
    "registry",
    "system_table_names",
    "is_system_table",
    "get_system_table",
    "get_system_table_header",
    "get_system_table_chunks",
    "DEFAULT_MAX_BLOCK_SIZE",
    "numbers_default_limit",
    "set_numbers_default_limit",
    "to_text",
]


def to_text(value: Any) -> str:
    """把任意值渲染成系统表里 ``String`` 列的文本形态。

    真实 ClickHouse 的 ``system.settings.value`` / ``system.tables.total_rows``
    这类混合来源的列都是字符串或数字,但**布尔**会显示成 ``0``/``1``
    (不是 ``True``/``False``),``None`` 显示成空串。这个小函数统一这件事,
    免得每个表的实现各写一遍。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


#: ``ctx.settings`` 拿不到时用的兜底块大小。
#:
#: 注意:真实 ClickHouse 的 ``max_block_size`` 默认是 65409(= 65536 - 127,
#: 见 :mod:`minich.settings` 里的解释)。这里取 ``8192`` 只是"没有上下文
#: 时的保守值" —— 有 ``ctx`` 时永远以 ``ctx.settings.max_block_size`` 为准。
DEFAULT_MAX_BLOCK_SIZE = 8192

#: 一行的类型 —— 一个 tuple,顺序与列定义一致。
Row = Sequence[Any]
#: 行提供函数:``ctx -> Iterable[Row]``。**必须是生成器或惰性序列**。
RowProvider = Callable[[Any], Iterable[Row]]


# ══════════════════════════════════════════════════════════════════════════
# 表接口
# ══════════════════════════════════════════════════════════════════════════


class ISystemTable:
    """一张系统表的接口 —— 相当于 ``IStorage`` 在 ``system`` 库里的最小化。

    真实 ClickHouse 的 ``StorageSystemXxx`` 继承完整 ``IStorage``(几十个
    虚函数);mini-clickhouse 只保留解释器真正会调的两个:
    :meth:`get_header`(零行 Block = 列名 + 类型)与 :meth:`get_chunks`
    (流式数据)。

    子类要么自己实现这两个方法,要么用 :class:`SimpleSystemTable`
    (给一张列定义表 + 一个行提供函数)。
    """

    #: 表名(不含 ``system.`` 前缀)。注册表用它会查表。
    name: str = ""

    def get_header(self, ctx: Any = None) -> Block:
        """返回**零行**的 Block —— 只有列名与类型。

        这是"这张表长什么样"的权威来源:列裁剪、``SELECT *`` 展开、
        类型检查全看它。实现里**不能**触发真正的数据读取
        (否则打开一张空表也会去扫磁盘)。
        """
        raise_error(
            ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
            f"System table {self.name} does not implement get_header()",
        )

    def get_chunks(self, ctx: Any = None) -> Iterator[Chunk]:
        """流式产出数据块 —— **惰性、分块**。

        每次 yield 的 ``Chunk`` 行数不超过 ``max_block_size``。
        """
        raise_error(
            ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
            f"System table {self.name} does not implement get_chunks()",
        )
        yield  # pragma: no cover - 让类型检查知道这是生成器

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<SystemTable {self.name}>"


def _resolve_type(spec: Any) -> DataType:
    """列定义里的类型可以写类型名(``"UInt64"``)或现成的 :class:`DataType`。"""
    if isinstance(spec, DataType):
        return spec
    return get_data_type(str(spec))


def _make_header(columns: Sequence[Tuple[str, Any]]) -> Block:
    """把 ``[(名字, 类型名), ...]`` 变成零行的 header Block。"""
    return Block([ColumnWithTypeAndName(n, _resolve_type(t)) for n, t in columns])


def _rows_to_chunk(header: Block, rows: Sequence[Row]) -> Chunk:
    """把一批行(**列式**地)装进一个 Chunk。

    这里做了一件真实 ClickHouse 在写入端才会做的事:把"一行行"的值
    按列拆开、逐列 append。之所以慢也要这么做,是因为**列存的本质**
    就是"同一列的值连续存放" —— 系统表是数据的**生产者**,
    生产者必须从源头就把形状摆对,否则下游每个处理器都要付一次转置成本。

    ``normalize`` 负责把 Python 值(``"2026-10-15 10:00:00"``、``True``、
    ``[1,2]``)转成该类型的**内部表示**(epoch 秒、0/1、数组列),
    这一步不能省 —— 否则 ``DateTime`` 列里会躺着一个字符串,
    格式化时才炸。
    """
    n = len(rows)
    cols: List[Any] = []
    for i, cwt in enumerate(header.data):
        column = cwt.type.create_column()
        dtype = cwt.type
        for row in rows:
            column.append(dtype.normalize(row[i]))
        if column.size() != n:  # pragma: no cover - 防御性
            raise_error(
                ErrorCode.LOGICAL_ERROR_IN_MINICH,
                f"System table column '{cwt.name}' produced {column.size()} "
                f"value(s) for {n} row(s)",
            )
        cols.append(column)
    return Chunk(header.clone_empty(), cols, n)


def max_block_size(ctx: Any) -> int:
    """当前会话的 ``max_block_size`` —— 块大小的唯一来源。

    与 :mod:`minich.storages._common` 里的同类函数保持一致:取不到
    设置就退回 :data:`DEFAULT_MAX_BLOCK_SIZE`,**绝不**因为"设置模块
    缺了个名字"就把系统表读挂。
    """
    settings = getattr(ctx, "settings", None) if ctx is not None else None
    value: Any = DEFAULT_MAX_BLOCK_SIZE
    if settings is not None:
        try:
            value = settings.try_get("max_block_size", DEFAULT_MAX_BLOCK_SIZE)
        except Exception:  # pragma: no cover - 设置模块降级
            value = DEFAULT_MAX_BLOCK_SIZE
    try:
        value = int(value)
    except Exception:  # pragma: no cover - 防御性
        value = DEFAULT_MAX_BLOCK_SIZE
    return value if value > 0 else DEFAULT_MAX_BLOCK_SIZE


class SimpleSystemTable(ISystemTable):
    """**最常用的系统表实现** —— 给列定义 + 一个"行提供函数"就够了。

    .. code-block:: python

       @register
       class Tables(SimpleSystemTable):
           name = "tables"
           columns = [("database", "String"), ("name", "String"), ...]
           def rows(self, ctx):
               for db in ctx.catalog.databases.values():
                   ...

    或者更直接:

    .. code-block:: python

       SimpleSystemTable("one", [("dummy", "UInt8")], lambda ctx: [(0,)])

    ``row_provider`` 收到的是 ``ctx``,返回一个 **Iterable[tuple]**。
    想流式(推荐)就返回生成器;返回 list 也可以,只要表本身不大
    (比如 ``system.one``)。

    子类也可以覆盖 :meth:`rows` 来提供行 —— 两种写法等价,选顺手的。
    """

    #: 列定义 —— ``[(名字, 类型名), ...]``,顺序就是输出的列顺序。
    columns: Sequence[Tuple[str, Any]] = ()

    def __init__(
        self,
        name: str = "",
        columns: Optional[Sequence[Tuple[str, Any]]] = None,
        row_provider: Optional[RowProvider] = None,
    ) -> None:
        if name:
            self.name = name
        if columns is not None:
            self.columns = list(columns)
        self._row_provider = row_provider
        self._header: Optional[Block] = None

    # ── 子类可覆盖 ──────────────────────────────────────────────────────

    def rows(self, ctx: Any) -> Iterable[Row]:
        """产出该表的行。默认转交给构造时给的 ``row_provider``。"""
        if self._row_provider is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
                f"System table {self.name} has no row provider",
            )
        return self._row_provider(ctx)

    # ── ISystemTable ────────────────────────────────────────────────────

    def get_header(self, ctx: Any = None) -> Block:
        if self._header is None:
            self._header = _make_header(self.columns)
        return self._header.clone_empty()

    def get_chunks(self, ctx: Any = None) -> Iterator[Chunk]:
        header = self.get_header(ctx)
        if not header.data:
            return
        block_size = max_block_size(ctx)
        buf: List[Row] = []
        for row in self.rows(ctx):
            if len(row) != len(header.data):
                raise_error(
                    ErrorCode.LOGICAL_ERROR_IN_MINICH,
                    f"System table {self.name} produced a row with {len(row)} "
                    f"value(s), expected {len(header.data)}",
                )
            buf.append(row)
            if len(buf) >= block_size:
                yield _rows_to_chunk(header, buf)
                buf = []
        if buf:
            yield _rows_to_chunk(header, buf)


class NumbersSystemTable(ISystemTable):
    """``system.numbers`` / ``system.numbers_mt`` —— **惰性、无界**的行号流。

    .. code-block:: text

       SELECT * FROM system.numbers LIMIT 5;   -- 0,1,2,3,4

    真实 ClickHouse 的 ``system.numbers`` 是**真正无限**的:它把
    ``0,1,2,3,…`` 一直流下去,直到上游(LIMIT、客户端取消、
    或者 ``max_execution_time``)喊停。如果忘了写 ``LIMIT``,
    那条查询**永远不会返回**。

    .. warning::

       **mini-clickhouse 在这里做了唯一一处"不忠实"的简化。**

       解释器调 :func:`get_system_table_chunks(ctx, name)` 时**没有**把
       查询的 ``LIMIT`` 传进来(接口就是这样约定的),所以我们无法
       "读到 LIMIT 就停"。为了不让 ``SELECT * FROM system.numbers``
       把进程卡死,这里给了一个**默认上界**:

       .. code-block:: text

          DEFAULT_NUMBERS_LIMIT = 10_000 行(可用 set_numbers_default_limit 改)

       这一条**必须**写在报告里。真实 ClickHouse 无此上界;
       教程里凡是用 ``system.numbers`` 的地方都应显式写 ``LIMIT``。
    """

    columns: Sequence[Tuple[str, Any]] = (("number", "UInt64"),)

    def __init__(self, name: str, mt: bool = False) -> None:
        self.name = name
        self.mt = mt

    def get_header(self, ctx: Any = None) -> Block:
        return _make_header(self.columns)

    def get_chunks(self, ctx: Any = None) -> Iterator[Chunk]:
        header = self.get_header(ctx)
        block_size = max_block_size(ctx)
        limit = numbers_default_limit()
        # 用 numpy.arange 一次造一块:既省 Python 循环,又天然是"分块"的
        # (上游算完一块丢一块,内存占用与"上界"无关)。
        offset = 0
        while offset < limit:
            count = min(block_size, limit - offset)
            values = np.arange(offset, offset + count, dtype=np.uint64)
            column = get_data_type("UInt64").create_column()
            column.append_many(values)
            yield Chunk(header.clone_empty(), [column], count)
            offset += count


# ══════════════════════════════════════════════════════════════════════════
# system.numbers 的默认上界(见 NumbersSystemTable 的说明)
# ══════════════════════════════════════════════════════════════════════════

#: ``system.numbers`` 在没有 LIMIT 时的默认行数上界。测试可以改它。
#: 选 10000 是因为它小到不会把内存打爆、又大到足够演示"惰性分块"。
DEFAULT_NUMBERS_LIMIT = 10_000
_NUMBERS_LIMIT = DEFAULT_NUMBERS_LIMIT


def numbers_default_limit() -> int:
    """当前 ``system.numbers`` 的默认上界(没有 LIMIT 时生效)。"""
    return _NUMBERS_LIMIT


def set_numbers_default_limit(n: int) -> None:
    """改 ``system.numbers`` 的默认上界 —— **主要给测试用**。

    ``n <= 0`` 表示恢复默认值。真实 ClickHouse 没有这个概念,
    它只认查询里的 ``LIMIT`` —— 别在生产里依赖这个函数。
    """
    global _NUMBERS_LIMIT
    _NUMBERS_LIMIT = DEFAULT_NUMBERS_LIMIT if n is None or int(n) <= 0 else int(n)


# ══════════════════════════════════════════════════════════════════════════
# 注册表
# ══════════════════════════════════════════════════════════════════════════

#: 表名(小写)→ ISystemTable 实例。
_REGISTRY: dict = {}


def register_system_table(table: ISystemTable) -> ISystemTable:
    """注册一张系统表。名字统一按**原样**存放,查找时大小写不敏感。"""
    if not table.name:
        raise_error(
            ErrorCode.LOGICAL_ERROR_IN_MINICH,
            "A system table must have a non-empty name",
        )
    _REGISTRY[table.name] = table
    return table


def registry() -> dict:
    """当前注册表的**快照** —— ``{名字: ISystemTable}``。

    返回的是浅拷贝:调用方可以随便遍历,但改这个 dict 不会影响注册表。
    """
    return dict(_REGISTRY)


def system_table_names() -> List[str]:
    """所有已注册的系统表名(排序)。"""
    return sorted(_REGISTRY)


def _lookup(name: str) -> Optional[ISystemTable]:
    """按名字查表 —— 先精确匹配,再大小写不敏感。"""
    hit = _REGISTRY.get(name)
    if hit is not None:
        return hit
    low = name.lower()
    for key, table in _REGISTRY.items():
        if key.lower() == low:
            return table
    return None


def is_system_table(name: str) -> bool:
    """``name`` 是不是一张已注册的系统表。

    解释器用它来决定"``FROM xxx`` 是走目录还是走系统表注册表"
    (见 :meth:`minich.execution.interpreter.InterpreterSelectQuery._read_table`)。
    """
    return _lookup(name) is not None


def _near_misses(name: str, n: int = 5) -> List[str]:
    """给"表名写错了"的报错用的近邻建议(基于编辑距离)。"""
    return difflib.get_close_matches(name, system_table_names(), n=n, cutoff=0.4)


def get_system_table(name: str) -> ISystemTable:
    """按名字取一张系统表;取不到抛 ``UNKNOWN_TABLE`` 并列出近邻。

    报错里一定要给建议 —— 真实 ClickHouse 也会在 ``Unknown table 'system.x'``
    后面附上"Did you mean …",因为 ``system`` 库有几十上百张表,
    拼错一个字母太常见了。
    """
    table = _lookup(name)
    if table is None:
        suggest = _near_misses(name)
        hint = f" Did you mean: {', '.join(suggest)}?" if suggest else ""
        raise_error(
            ErrorCode.UNKNOWN_TABLE,
            f"Unknown system table 'system.{name}'.{hint} "
            f"Known tables: {', '.join(system_table_names()[:12])}"
            f"{' …' if len(_REGISTRY) > 12 else ''}",
        )
    return table


# ══════════════════════════════════════════════════════════════════════════
# 对外接口(解释器只用这两个)
# ══════════════════════════════════════════════════════════════════════════


def get_system_table_header(ctx: Any = None, name: Optional[str] = None) -> Block:
    """返回 ``system.<name>`` 的**零行** header(列名 + 类型)。

    签名刻意做成了"两种调用方式都行":

    * ``get_system_table_header(ctx, name)`` —— 与接口约定一致;
    * ``get_system_table_header(name)`` —— 解释器在"读到零个 chunk、
      需要构造一个空结果 header"时就是这么调的
      (见 ``InterpreterSelectQuery._read_system_table``)。

    实现里靠"第二个参数是不是 ``None``"来区分,这样两个调用点都不必改。
    """
    if name is None:
        name, ctx = ctx, None
    return get_system_table(name).get_header(ctx)


def get_system_table_chunks(ctx: Any, name: str) -> Iterator[Chunk]:
    """流式产出 ``system.<name>`` 的数据块 —— 解释器的主入口。

    .. code-block:: text

       解释器 _read_system_table()
            │
            ├─ 逐 chunk 取出、merge 成一个 Block(因为 mini-clickhouse 的
            │  源阶段是"物化"的,见 _build_source)
            └─ 但**接口本身是流式的** —— 这样 system.numbers 这种
               无界表、system.processes 这种"读的时刻才确定"的表
               都能被正确表达

    未知表名抛 ``UNKNOWN_TABLE``。
    """
    table = get_system_table(name)
    yield from table.get_chunks(ctx)
