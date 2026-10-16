"""执行引擎 —— 从 AST 到结果。

.. code-block:: text

   sql/parser.py  ──►  AST
                        │
                        ▼
   analyzer.py    ──►  "聚合前 / 聚合 / 聚合后"三段式分析
                        │
                        ▼
   optimizer.py   ──►  规则改写(平凡 count、PREWHERE 下推、LIMIT 下推…)
                        │
                        ▼
   processors.py  ──►  处理器图(源 → 变换 → 汇聚)
                        │
                        ▼
   interpreter.py ──►  编排全部,产出 QueryResult
                        │
                        ▼
   context.py     ──►  会话状态、统计、查询日志

===========================  ======================================================
模块                          一句话
===========================  ======================================================
``context``                  ``Context``(会话)与 ``QueryResult``(结果)
``analyzer``                 把查询拆成三段,抽出聚合函数
``expression``               **表达式求值器** —— AST → 列
``processors``               处理器与流水线(推送模型)
``joins``                    哈希连接、strictness、ASOF
``window``                   窗口函数
``optimizer``                确定性改写规则
``explain``                  ``EXPLAIN`` 的六种形态
``interpreter``              编排者,``Context.execute`` 的落地点
===========================  ======================================================

.. note::

   教程第 10 章(查询执行引擎)就是围绕 ``processors.py`` 与
   ``interpreter.py`` 写的。第 22 章(查询优化)围绕 ``optimizer.py``。
   第 13 章(JOIN)围绕 ``joins.py``。
"""

from __future__ import annotations

from .context import Context, QueryInfo, QueryResult, QueryStatus
from .expression import Evaluator, Scope, infer_literal_type
from .interpreter import execute_query, get_interpreter

__all__ = [
    "Context",
    "QueryResult",
    "QueryInfo",
    "QueryStatus",
    "execute_query",
    "get_interpreter",
    "Evaluator",
    "Scope",
    "infer_literal_type",
]
