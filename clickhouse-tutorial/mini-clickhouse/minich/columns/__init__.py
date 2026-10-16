"""列的物化实现。

按"数据长什么样"分成三层,和真实 ClickHouse 的目录结构对应:

===================  ==============================  ===============================
模块                 实现的列                        真实 ClickHouse 对应
===================  ==============================  ===============================
``base``             ``IColumn`` / ``ColumnVector``   ``IColumn`` / ``ColumnVector<T>``
``string``           ``ColumnString`` 等              ``ColumnString`` / ``ColumnFixedString``
``composite``        ``Nullable``/``Array``/…         ``ColumnsNullable`` 等
===================  ==============================  ===============================

**这里没有 ``ColumnSparse`` 之外的"编码列"** —— 低基数、稀疏这类
"编码"在真实 ClickHouse 里既有列级实现(``ColumnLowCardinality``、
``ColumnSparse``)也有序列化级实现(``SerializationLowCardinality``),
mini-clickhouse 只做前者,因为教学重点是"数据长什么样",不是"字节怎么写"。
"""

from __future__ import annotations

from .base import (
    ColumnBigInt,
    ColumnNothing,
    ColumnUUID,
    ColumnVector,
    IColumn,
    column_from_values,
)
from .composite import (
    ColumnAggregateFunction,
    ColumnArray,
    ColumnConst,
    ColumnLowCardinality,
    ColumnMap,
    ColumnNullable,
    ColumnSparse,
    ColumnTuple,
)
from .string import ColumnFixedString, ColumnString

__all__ = [
    "IColumn",
    "ColumnVector",
    "ColumnBigInt",
    "ColumnUUID",
    "ColumnNothing",
    "column_from_values",
    "ColumnString",
    "ColumnFixedString",
    "ColumnNullable",
    "ColumnArray",
    "ColumnTuple",
    "ColumnMap",
    "ColumnLowCardinality",
    "ColumnConst",
    "ColumnSparse",
    "ColumnAggregateFunction",
]
