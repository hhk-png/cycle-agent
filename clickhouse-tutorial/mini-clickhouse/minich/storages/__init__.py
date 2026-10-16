"""表引擎 —— "这张表怎么存、怎么读"。

**导入即注册**:每个模块在末尾调用
:func:`minich.storages.base.register_storage_engine`,把引擎名绑到
一个工厂上。所以只要 ``import minich.storages``,``CREATE TABLE ...
ENGINE = MergeTree`` 就能解析到实现。

===================  ================================================================
引擎                  一句话
===================  ================================================================
``MergeTree`` 家族    **核心**。追加写 + 后台 merge + 稀疏索引 + 分区
``Memory``           数据在内存,没有索引,重启即失
``Log``/``TinyLog``/  最简磁盘引擎,一列一个文件,只允许追加
``StripeLog``
``Null``             吞掉一切写入,读取永远为空(压测用)
``Set``              只存去重后的第一列(``IN`` 的半连接优化)
``Join``             内存哈希表(供 ``joinGet`` 用)
``Buffer``           内存攒批,定期刷入目标表
``Merge``            把多张同构表当成一张读
``View``             普通视图(查询时重跑子查询)
``MaterializedView`` **插入触发器**(不是定时刷新)
``File`` / ``URL``   文件/网络即表
``Values``           ``VALUES`` 字面量即表
``Replicated*``      复制表(见第 20 章)
===================  ================================================================

**没有 ``Distributed``。** 分片代理需要多个真实节点才能有意义,一个单进程
的教学实现里它只会变成"假装分发"的空壳 —— 所以它被明确排除在外,而不是
做一个能跑但会误导人的版本(见第 21 章与第 32 章 B.6)。``Replicated*`` 则
是真的注册了:它在没有 Keeper 可用时退化为本地单副本,而不是假装复制成功。

.. warning::

   **引擎选择在 ClickHouse 里比在 MySQL 里重要一个量级。**
   ``MergeTree`` 能做而 ``Log`` 不能做的事包括:按主键裁剪、
   按分区裁剪、去重(Replacing)、增量聚合(Aggregating)、
   TTL、mutation、复制。写日志表用 ``Log`` 看着简单,
   但当你需要"按时间范围查最近一小时"时就会后悔。
   第 04 章整章讲这个。
"""

from __future__ import annotations

# ── 核心 ────────────────────────────────────────────────────────────────
from . import merge_tree  # noqa: F401

# ── 简单引擎 ────────────────────────────────────────────────────────────
from . import memory  # noqa: F401
from . import log  # noqa: F401
from . import null  # noqa: F401
from . import set as set_engine  # noqa: F401
from . import join  # noqa: F401
from . import buffer  # noqa: F401
from . import merge as merge_engine  # noqa: F401
from . import values  # noqa: F401
from . import view  # noqa: F401
from . import file as file_engine  # noqa: F401
from . import url  # noqa: F401
from . import numbers  # noqa: F401
from . import system_numbers  # noqa: F401

# ── 表函数 ──────────────────────────────────────────────────────────────
from . import table_functions  # noqa: F401

from .base import (  # noqa: F401
    IStorage,
    ReadContext,
    StorageFeatures,
    create_storage,
    register_storage_engine,
    registered_storage_engines,
    storage_engine_exists,
)

__all__ = [
    "IStorage",
    "ReadContext",
    "StorageFeatures",
    "create_storage",
    "register_storage_engine",
    "registered_storage_engines",
    "storage_engine_exists",
]
