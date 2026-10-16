"""SQL 前端 —— 词法、语法、语法树。

三层,和真实 ClickHouse 的 ``src/Parsers/`` 一一对应:

===========================  ===========================  ============================
mini-clickhouse              真实 ClickHouse              职责
===========================  ===========================  ============================
``lexer.py``                 ``Lexer.cpp``                文本 → Token
``parser.py``                ``Parser*.cpp``              Token → AST
``ast_.py``                  ``AST*.h``                   AST 节点定义 + 回打 SQL
===========================  ===========================  ============================

.. code-block:: text

   "SELECT a+1 FROM t WHERE b>2"
        │
        │ tokenize()        ← 注释/空白在这里丢掉,位置信息保留在 token.pos
        ▼
   [IDENT(SELECT) IDENT(a) PUNCT(+) NUMBER(1) IDENT(FROM) …]
        │
        │ Parser.parse_statement()   ← 递归下降 + 有界回溯
        ▼
   SelectQuery(selects=[BinaryOp(+, Identifier(a), Literal(1))],
               from_=TableReference(t),
               where=BinaryOp(>, Identifier(b), Literal(2)))
        │
        │ 交给 minich/execution/ 做表达式分析、优化、执行(第 10、22 章)
        ▼

**使用入口**::

    from minich.sql import parse, format_ast
    tree = parse("SELECT count() FROM hits WHERE url LIKE '/a%'")
    print(tree.to_sql())          # 规范化后的 SQL
    print(format_ast(tree))       # 树形展示(EXPLAIN AST 就用它)
"""

from __future__ import annotations

from .ast_ import format_ast
from .lexer import KEYWORDS, Token, TokenKind, tokenize
from .parser import Parser, parse, parse_expression_text

__all__ = [
    "parse",
    "parse_expression_text",
    "Parser",
    "tokenize",
    "Token",
    "TokenKind",
    "KEYWORDS",
    "format_ast",
]
