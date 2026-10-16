"""``Null`` 引擎 —— 写入被丢弃,读取永远为空。

.. code-block:: text

   CREATE TABLE sink (id UInt64, v Float64) ENGINE = Null
   INSERT INTO sink SELECT * FROM huge_table          -- 数据到这,蒸发

**它存什么?** 什么都不存。``write`` 收到 block 后**直接返回**,
``read`` 立刻结束 —— 连一个字节都不落。

**它不能做什么?** 不能保留任何数据,不能参与任何"读回自己写入内容"的操作,
``count()`` 永远是 0。

**什么时候该选它(而不是 MergeTree)?** 有两个非常具体的用途:

1. **只测查询、不测写入**:``INSERT INTO null_target SELECT … FROM big``
   让你把"算这条 SELECT 要多久"与"把结果写下去要多久"分开量。
   真实场景里也常用它做**基线压测**:把 ``Null`` 表当作 sink,
   整条流水线照跑,但没有落盘的开销。
2. **物化视图的丢弃目标**:``CREATE MATERIALIZED VIEW mv TO null_target AS …``
   —— 你可能只想借物化视图的**插入触发器**做副作用(比如计数、发通知),
   又不想要一份落盘副本。``ENGINE = Null`` 就是这个"只触发不存储"的落点。

.. note::

   真实 ClickHouse 的 ``StorageNull`` 甚至**不为每张表存放任何状态**
   (它的 ``totalRows`` 恒为 0),这正是它极轻量的原因。mini-clickhouse
   照抄这个语义。

.. warning::

   ``Null`` **不是** ``Nullable``。前者是一个表引擎(丢弃整张表的写入),
   后者是一个数据类型(某一列可以存 NULL)。把两者混起来是初学者最常见的
   误读之一。
"""

from __future__ import annotations

from typing import Any, Iterator, List

from ..block import Block, Chunk, ColumnWithTypeAndName
from ._common import _stored_columns
from .base import IStorage, StorageFeatures, register_storage_engine

__all__ = ["StorageNull"]


class StorageNull(IStorage):
    """空引擎 —— 见模块文档。"""

    engine_name = "Null"

    #: 无索引、无分区、不支持 mutation;并行读无所谓(反正读不出东西)。
    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: dict = dict(params or {})

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        return _stored_columns(self.table)

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        # 永远为空 —— 直接返回一个空迭代器。
        #
        # 注意这里是 **return,不是 yield**:如果写成 `if False: yield`,
        # 函数会变成生成器,调用方拿到的是"还没执行的生成器",
        # 一些断言"read() 返回长度为 0 的列表"的测试就会失败。
        return iter(())

    def write(self, ctx: Any, block: Block) -> None:
        """丢弃写入 —— 但要先**校验结构**,不能连列名错了都放过。

        真实 ClickHouse 在 ``INSERT`` 时同样会解析并转换输入的 block
        (它只是在最后一步丢掉),所以"插一个不存在的列"在 Null 表上
        **依然报错**。这个细节很容易被忽略,但它保证了 Null 表可以用作
        "查询形状的验收器"。
        """
        from ._common import _align_block

        stored = [c for c in self.table.columns if c.is_stored()]
        _align_block(block, stored, strict=True, table_name=self.full_table_name())

    def row_count(self) -> int:
        return 0

    def size_in_bytes(self) -> int:
        return 0

    def truncate(self) -> None:
        # 本来就是空的。
        return

    def shutdown(self) -> None:
        self._shutdown = True


def _factory(table, storage_id, context, engine_args, params) -> StorageNull:
    return StorageNull(table, storage_id, context, engine_args, params)


register_storage_engine(
    "Null",
    _factory,
    description="丢弃一切写入,读取永远为空(压测基线 / 物化视图丢弃目标)。",
    aliases=("NULL",),
)
