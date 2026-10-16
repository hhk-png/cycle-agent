"""``minich.system`` —— ``system`` 库的全部实现。

这是 mini-clickhouse 对 **``src/Storages/System/``** 的对应物:真实
ClickHouse 把每一张系统表实现成一个 ``IStorage``,这里则实现成
:class:`minich.system.base.ISystemTable`。

.. code-block:: text

   ┌─ 本包的对外接口(解释器只用这四个)──────────────────────────┐
   │  is_system_table(name) -> bool                              │
   │  system_table_names() -> List[str]                          │
   │  registry() -> Dict[str, ISystemTable]                      │
   │  get_system_table_header(ctx, name) -> Block   (零行 header) │
   │  get_system_table_chunks(ctx, name) -> Iterator[Chunk]       │
   └─────────────────────────────────────────────────────────────┘

文件分工:

.. code-block:: text

   base.py            接口(ISystemTable)、注册表、"惰性分块"公共设施
   tables.py          databases / tables / columns / parts / part_log /
                      mutations / merges / merge_tree_settings /
                      dictionaries                       (结构 & 引擎状态)
   system_tables.py   data_type_families / table_engines / formats /
                      functions / settings / clusters / 权限族 /
                      contributors / build_options / time_zones /
                      disk_space / disks / warnings          (注册表 & 配置)
   replication_tables.py  replicas                             (复制看板)
   metrics_tables.py  metrics / events / asynchronous_metrics    (计数器 & gauge)
   logs_tables.py     query_log / query_thread_log / processes /
                      text_log / errors / crash_log / metric_log (运行时日志)
   helpers.py         one / numbers / numbers_mt                 (辅助表)

**导入这个包 = 完成全部注册**(和 :mod:`minich.storages` 的"导入即注册"
是同一套约定),之后 ``Context().execute("SELECT * FROM system.tables")``
就能跑。

.. note::

   **哪些是真实的、哪些是降级的?**

   * 真实且有数据:``databases`` / ``tables`` / ``columns`` / ``settings`` /
     ``functions`` / ``data_type_families`` / ``formats`` / ``table_engines`` /
     ``metrics`` / ``events`` / ``query_log`` / ``build_options`` /
     ``time_zones`` / ``disk_space`` / ``disks`` / ``one`` / ``numbers`` 等;
   * 结构对、数据来自可选来源(来源不在就空):``parts`` / ``part_log`` /
     ``mutations`` / ``merges``(MergeTree 引擎的 part / 事件 / mutation /
     在途合并)、``dictionaries``(``ctx.dictionaries``)、
     ``clusters``(``ctx.clusters``)、``replicas``(``ctx.replicas`` /
     ``ctx.replication``,即一个 ``LocalCluster``)、权限族(``ctx.access``);
   * 结构对但**刻意恒空**(不伪造):``query_thread_log``(单线程)、
     ``crash_log`` / ``metric_log``、``contributors``、
     ``errors``(计数钩子 ``record_error`` 已就位,但解释器尚未调用)。

   注意 ``merges`` 与 ``replicas`` 在**空闲 / 未配复制**的实例上为空是
   **正常状态**,不是故障 —— 各自的 docstring 都专门写了这一点。

   每一张表的 docstring 都会说清它属于哪一类,以及**为什么**。
"""

from __future__ import annotations

from .base import (
    DEFAULT_MAX_BLOCK_SIZE,
    ISystemTable,
    NumbersSystemTable,
    SimpleSystemTable,
    get_system_table,
    get_system_table_chunks,
    get_system_table_header,
    is_system_table,
    numbers_default_limit,
    register_system_table,
    registry,
    set_numbers_default_limit,
    system_table_names,
)
from .logs_tables import record_error
from . import (
    helpers,
    logs_tables,
    metrics_tables,
    replication_tables,
    system_tables,
    tables,
)

__all__ = [
    # ── 接口约定 ────────────────────────────────────────────────────────
    "is_system_table",
    "get_system_table_header",
    "get_system_table_chunks",
    "registry",
    "system_table_names",
    # ── 类型与工具 ──────────────────────────────────────────────────────
    "ISystemTable",
    "SimpleSystemTable",
    "NumbersSystemTable",
    "get_system_table",
    "register_system_table",
    "DEFAULT_MAX_BLOCK_SIZE",
    "numbers_default_limit",
    "set_numbers_default_limit",
    # ── 可选集成点 ──────────────────────────────────────────────────────
    "record_error",
]


def _register_all() -> None:
    """让每个子模块把自己的表登记进注册表(顺序无关,但显式列出更好读)。"""
    tables.register()
    system_tables.register()
    replication_tables.register()
    metrics_tables.register()
    logs_tables.register()
    helpers.register()


_register_all()
