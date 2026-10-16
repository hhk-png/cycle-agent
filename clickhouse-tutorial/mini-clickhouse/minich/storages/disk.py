"""磁盘抽象 —— 存储层的"字节容器"接口。

.. code-block:: text

   这个模块是 :mod:`minich.storages.merge_tree.disk` 的**再导出入口**。

   为什么要有这么一个"空"模块? 因为在 mini-clickhouse 的目录划分里,
   **"磁盘"不属于 MergeTree** —— 它是所有需要落盘的引擎共用的底座
   (``Log`` / ``TinyLog`` / ``StripeLog`` 也写磁盘,
   ``Distributed`` 与 ``Merge`` 也会间接用到)。

   所以模块放在 ``minich/storages/`` 这一层更符合它的抽象层级;
   而实现跟着 MergeTree 一起放在 ``merge_tree/`` 里,
   是为了让"读完 MergeTree 这几章"的人**只需要打开一个目录**。

   两处指向同一个对象,没有第二份实现 —— 因此不存在"改了这边忘了那边"的风险。

.. note::

   真实 ClickHouse 把磁盘放在 ``src/Disks/``,与存储引擎
   (``src/Storages/``)平级 —— 我们的层级划分与它一致。

   核心理念只重复一遍(完整版见实现模块的 docstring):

   .. code-block:: text

     MergeTreeData ──► IDisk ──► DiskLocal / DiskS3 / DiskHDFS
                         │
                         └── atomic_write(): 写 tmp → os.replace
                             ↑ "读方永远看不到半个 part" 的全部分量
"""

from __future__ import annotations

from .merge_tree.disk import (  # noqa: F401  (再导出,不是第二份实现)
    DiskLocal,
    DiskSpace,
    IDisk,
    get_available_space,
)

__all__ = ["IDisk", "DiskLocal", "DiskSpace", "get_available_space"]
