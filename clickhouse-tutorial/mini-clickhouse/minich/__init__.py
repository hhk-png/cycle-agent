"""mini-clickhouse —— 一个可读、可运行、可验证的列式 OLAP 数据库教学实现。

.. code-block:: python

   import minich

   ctx = minich.Context()                       # 一个"服务端"(内存)
   ctx.execute("CREATE TABLE hits (id UInt64, url String) "
               "ENGINE = MergeTree ORDER BY id")
   ctx.execute("INSERT INTO hits SELECT number, concat('/p/', toString(number % 10)) "
               "FROM numbers(1000)")
   print(ctx.execute("SELECT url, count() FROM hits GROUP BY url ORDER BY url").text())

   ctx.shutdown()                               # 关掉后台合并线程

也可以用起来更像真实客户端::

   from minich import Client
   with Client.local() as c:                    # 进程内
       print(c.execute("SELECT 1 + 1").scalar())

命令行入口::

   python -m minich demo
   python -m minich local -q "SELECT count() FROM numbers(1000000)"
   python -m minich client                      # 交互式 REPL
   python -m minich server --port 9000          # 起一个(简化协议)服务端

.. note::

   **两份不同的"端口"**:``import minich`` 拿到的 ``Context`` 是**服务端**
   (它拥有目录、存储引擎、后台线程);``Client`` 是**客户端**
   (它只负责把 SQL 发出去、把结果收回来)。在进程内模式下两者住在同一个
   进程里 —— 真实 ClickHouse 里它们隔着网络,而这条边界正是
   :mod:`minich.protocol` 与 :mod:`minich.client` 要演示的东西。

**包结构**(每一层对应真实 ClickHouse 的一个源码目录):

==========================  ============================================
mini-clickhouse             真实 ClickHouse(``src/``)
==========================  ============================================
``minich/errors.py``         ``Common/ErrorCodes.cpp``
``minich/types/``            ``DataTypes/``
``minich/columns/``          ``Columns/``
``minich/block.py``          ``Core/Block.h`` / ``Chunk.h``
``minich/serialization.py``  ``DataTypes/Serializations/``
``minich/compression.py``    ``Compression/``
``minich/sql/``              ``Parsers/``
``minich/execution/``        ``Processors/`` + ``Interpreters/``
``minich/functions/``        ``Functions/``
``minich/aggregates/``       ``AggregateFunctions/``
``minich/storages/``         ``Storages/``
``minich/system/``           ``Storages/System/``
``minich/keeper.py``         ``Coordination/``
``minich/replication.py``    ``Storages/StorageReplicatedMergeTree``
``minich/dictionaries.py``   ``Dictionaries/``
``minich/access.py``         ``Access/``
``minich/settings.py``       ``Core/Settings.cpp``
``minich/formats/``          ``Formats/``
``minich/client.py``         ``Client/``
==========================  ============================================

完整的能力矩阵(哪些实现了、哪些没有、为什么)见 ``README.md``;
工程手册见教程第 32 章(附录 B)。
"""

from __future__ import annotations

__version__ = "0.1.0"

#: mini-clickhouse 对标的真实 ClickHouse 版本。所有"真实行为"的描述
#: 都以这个版本为准 —— 版本不同,默认值和写法的差别可能很大
#: (例如 24.x 之后的 ``JSON`` 类型、``Variant``/``Dynamic`` 类型)。
TARGET_CLICKHOUSE_VERSION = "25.3"


def __getattr__(name: str):
    """惰性导出 —— ``import minich`` 不应该把整个引擎都加载进来。

    为什么需要它? 因为 :class:`~minich.execution.context.Context` 会
    连带导入解释器、存储引擎、函数库…… 那是一个几百毫秒的链条。
    而 ``import minich`` 在很多场景下只是想知道版本号(比如
    ``python -c "import minich; print(minich.__version__)"``)。

    代价是:``from minich import Context`` 仍然会触发真实的导入
    (这是对的),但 ``minich.__version__`` 不会。
    真实 ClickHouse 的 client 库也做同样的事(PEP 562 的
    ``__getattr__`` 就是为这种"重包 + 轻入口"设计的)。
    """
    if name in ("Context", "QueryResult", "QueryInfo"):
        from .execution.context import Context, QueryInfo, QueryResult

        return {"Context": Context, "QueryResult": QueryResult, "QueryInfo": QueryInfo}[name]
    if name == "Client":
        from .client import Client

        return Client
    if name == "MiniChError":
        from .errors import MiniChError

        return MiniChError
    if name in ("parse", "format_ast"):
        from .sql import format_ast, parse

        return {"parse": parse, "format_ast": format_ast}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    "TARGET_CLICKHOUSE_VERSION",
    "Context",
    "Client",
    "QueryResult",
    "MiniChError",
]
