"""聚合函数系统 —— mini-clickhouse 的 ``AggregateFunctions/``。

对照真实 ClickHouse 的目录:

============================  ==================================================
mini-clickhouse               真实 ClickHouse(``src/AggregateFunctions/``)
============================  ==================================================
``base.py``                   ``IAggregateFunction.h`` + ``AggregateFunctionFactory``
``combinators.py``            ``AggregateFunctionCombinator.h`` 及 ``-If/-Array/...``
``count.py``                  ``AggregateFunctionCount.cpp``
``sum.py``                    ``AggregateFunctionSum.cpp`` 等
``average.py``                ``AggregateFunctionAvg.cpp``
``minmax.py``                 ``AggregateFunctionMinMax.cpp``
``any.py``                    ``AggregateFunctionAny.cpp``
``uniq.py``                   ``AggregateFunctionUniq.cpp`` + ``HyperLogLog.h``
``quantile.py``               ``AggregateFunctionQuantile.h`` 及各种草图
``argminmax.py``              ``AggregateFunctionArgMinMax.cpp``
``group_array.py``            ``AggregateFunctionGroupArray.cpp``
``statistics.py``             ``AggregateFunctionStatistics*.cpp`` 等
============================  ==================================================

导入本包即完成**全部注册** —— 之后就能用::

    from minich.aggregates import get_aggregate_function

    f = get_aggregate_function("sumIf", [], [UInt64, UInt8])
    f = get_aggregate_function("quantiles(0.5, 0.9)", [], [UInt64])
    f = get_aggregate_function("sumMerge", [], [agg_state_type])

组合器(``-If`` / ``-Array`` / ``-Merge`` / ``-State`` / ``-Distinct`` /
``-OrNull`` / ``-OrDefault`` / ``-Resample`` / ``-ForEach``)在
:func:`get_aggregate_function` 内部按后缀解析,所以
``sum`` 一个函数就派生出 ``sumIf`` / ``sumArray`` / ``sumState`` / ``sumMerge``
……几十个可用的名字。
"""

from __future__ import annotations

# ── 先注册组合器后缀,再注册具体函数;顺序不影响最终可解析性 ──
from . import combinators  # noqa: F401  (导入即触发 register_combinator)
from .base import (
    ByteReader,
    ByteWriter,
    IAggregateFunction,
    ValueColumn,
    aggregate_function_exists,
    get_aggregate_function,
    register_aggregate,
    register_combinator,
    split_combinators,
    registered_aggregate_functions,
)

# ── 各函数模块:导入即触发 register_aggregate ──
from . import argminmax  # noqa: F401
from . import average  # noqa: F401
from . import count  # noqa: F401
from . import group_array  # noqa: F401
from . import minmax  # noqa: F401
from . import quantile  # noqa: F401
from . import statistics  # noqa: F401
from . import sum  # noqa: F401
from . import uniq  # noqa: F401
from . import any as _any_module  # noqa: F401  (`any` 是保留名,用别名导入)
from .uniq import HyperLogLog

__all__ = [
    "IAggregateFunction",
    "ByteWriter",
    "ByteReader",
    "ValueColumn",
    "register_aggregate",
    "register_combinator",
    "split_combinators",
    "get_aggregate_function",
    "aggregate_function_exists",
    "registered_aggregate_functions",
    "HyperLogLog",
]
