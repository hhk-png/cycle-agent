"""语法分析 —— Token 流 → AST。

递归下降,手写。这一层要处理三类"麻烦",每一类都来自 ClickHouse 特有的设计:

.. code-block:: text

   麻烦 1  同一个词在不同位置含义不同
           `key`、`table`、`range`、`interval` 既是关键字也是合法列名;
           `LIMIT 5 BY x` 里的 BY 属于 LIMIT,而 `GROUP BY` 里的属于 GROUP。

   麻烦 2  有些语法需要回溯才能定
           `(a, b)` 是元组还是"带括号的表达式列表"?
           `(x) -> x + 1` 是 lambda 还是"表达式后跟箭头"?
           `(SELECT 1)` 是子查询还是元组里放一个子查询?
           这里用 `_try(...)` 做**有界回溯**:记下位置,失败就回退。

   麻烦 3  类型名不是"表达式"
           `Enum8('a' = 1, 'b' = 2)`、`Decimal(10, 2)`、`Tuple(x UInt8)`
           —— 这些**不能**用表达式解析器去啃(它们不是表达式)。
           我们直接从**原始 SQL 文本**里按括号配平切一段出来,
           交给 :func:`minich.types.get_data_type` 解析。
           这是真实 ClickHouse 用 `ParserDataType` 专门处理的地方。

.. note::

   mini-clickhouse 的解析器**不做**下列事情(真实 ClickHouse 有):

   * ``WITH FILL`` / ``LIMIT ... WITH TIES`` / ``ASOF JOIN`` 的完整语义;
   * ``SETTINGS`` 里的子查询(``format_xxx = (SELECT ...)``);
   * 多语句(一次只解析一条,``;`` 之后的内容被忽略交给调用方);
   * `EXPLAIN` 的全部选项(只支持 kind + settings)。

   这些不做的部分**不会静默出错** —— 遇到时会抛带位置信息的
   ``SYNTAX_ERROR``,而不是解析成一个错的树。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence, Tuple

from ..errors import ErrorCode, MiniChError, raise_error
from ..literals import parse_literal
from . import ast_ as A
from .lexer import Token, TokenKind, iter_tokens

__all__ = ["parse", "Parser", "parse_expression_text"]


#: 括号配对表(类型名切段时用)
_OPEN = {"(": ")", "[": "]", "{": "}"}

#: 这些词在 FROM 之后不能作为表名的开始(它们是子句关键字)
_NOT_TABLE_START = frozenset({
    "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "SETTINGS", "FORMAT",
    "UNION", "INTO", "PREWHERE", "ARRAY", "WINDOW", "OFFSET", "ON", "USING",
})


class Parser:
    """SQL 解析器。

    一个实例只解析**一条语句**。要解析多条请反复调用 :meth:`parse_statement`。
    """

    def __init__(self, sql: str) -> None:
        self.sql = sql
        #: 已经切出来的 token(见 :meth:`_fill`)。
        #:
        #: .. warning::
        #:
        #:    **不是**"整条 SQL 的全部 token"。``INSERT INTO t FORMAT TSV``
        #:    之后跟的是**数据**,把它也切一遍是错的:TSV/CSV 的值里
        #:    出现的 ``\\`` 根本不是合法 token,于是"还没取 payload 就先报
        #:    SYNTAX_ERROR"。所以这里改成按需切(``_token_iter``),
        #:    一旦取走 payload 就立刻停手(``_payload_taken``)。
        self.tokens: List[Token] = []
        self._token_iter = iter_tokens(sql)
        self._lex_exhausted = False
        #: ``FORMAT <name>`` 之后的原文已经被交给格式解析器 —— 后面那段
        #: 文本**不再是 SQL**,不许再拿它做词法分析。
        self._payload_taken = False
        self.index = 0
        #: 括号深度 —— 只用于错误消息的可读性
        self.depth = 0
        #: 解析深度上限 —— 对应真实的 ``max_parser_depth``(默认 1000)。
        #: 没有它,一个 ``((((((…`` 就能把递归下降打成栈溢出。
        #:
        #: .. warning::
        #:
        #:    **这里的数字要比真实 ClickHouse 小得多,而且不是随便定的。**
        #:    ClickHouse 的解析器是 C++ 的,一层语法嵌套大约消耗十几个
        #:    栈帧;CPython 的默认递归上限是 **1000 帧**,而我们每深入
        #:    一层表达式要穿过 ``_parse_or → _parse_and → … → _parse_primary``
        #:    这一整条链(实测约 12 个 Python 帧)。
        #:    所以深度上限必须设在 ``1000 / 12`` 之下 —— 否则守卫**永远
        #:    先被 Python 的 ``RecursionError`` 抢走**,读者看到的是一句
        #:    ``maximum recursion depth exceeded``,而不是我们精心写好的
        #:    ``QUERY_IS_TOO_LARGE``。这正是"守卫写了但够不着"这类 bug
        #:    的典型形状:代码看着有保护,实际上保护从来没生效过。
        self.max_depth = 60
        self._current_depth = 0

    # ── Token 访问 ──────────────────────────────────────────────────────

    def _fill(self, upto: int) -> None:
        """把 token 缓冲填到至少 ``upto`` 个(切不出来就停)。

        两个停手条件:
        * 词法器跑完了(最后一定是 ``EOF``);
        * ``_payload_taken`` —— ``FORMAT`` 后面的原文是数据,切不得。
          这时候 ``peek`` 会**夹**在最后一个 token 上,而不是抛异常。
        """
        while (not self._lex_exhausted and not self._payload_taken
               and len(self.tokens) <= upto):
            try:
                tok = next(self._token_iter)
            except StopIteration:  # pragma: no cover - 词法器总以 EOF 收尾
                self._lex_exhausted = True
                break
            self.tokens.append(tok)
            if tok.kind == TokenKind.EOF:
                self._lex_exhausted = True
                break

    def peek(self, offset: int = 0) -> Token:
        self._fill(self.index + offset)
        i = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[i]

    def next(self) -> Token:
        self._fill(self.index)
        i = min(self.index, len(self.tokens) - 1)
        tok = self.tokens[i]
        if tok.kind != TokenKind.EOF:
            self.index += 1
        return tok

    def at_end(self) -> bool:
        # 取走 ``FORMAT`` 的 payload 之后,剩下的原文是数据不是 SQL ——
        # 对语法分析而言"语句到此结束"。
        if self._payload_taken:
            return True
        return self.peek().kind == TokenKind.EOF

    def error(self, message: str, token: Optional[Token] = None) -> None:
        tok = token if token is not None else self.peek()
        near = self.sql[max(0, tok.pos - 20) : tok.pos + 30].replace("\n", " ")
        raise_error(
            ErrorCode.SYNTAX_ERROR,
            f"{message} (at position {tok.pos}, line {tok.line}: …{near}…)",
        )

    # ── 关键字 / 标点 ───────────────────────────────────────────────────

    def accept_keyword(self, *words: str) -> bool:
        tok = self.peek()
        if tok.kind == TokenKind.IDENT and not tok.quoted and tok.upper in words:
            self.next()
            return True
        return False

    def accept_keywords(self, *words: str) -> bool:
        """匹配**一串**关键字(全部匹配才算成功,否则回退)。"""
        save = self.index
        for w in words:
            if not self.accept_keyword(w):
                self.index = save
                return False
        return True

    def expect_keyword(self, *words: str) -> None:
        if not self.accept_keyword(*words):
            self.error(f"Expected {' or '.join(words)}")

    def peek_keyword(self, *words: str) -> bool:
        tok = self.peek()
        return tok.kind == TokenKind.IDENT and not tok.quoted and tok.upper in words

    def peek_keywords(self, *words: str) -> bool:
        for i, w in enumerate(words):
            tok = self.peek(i)
            if tok.kind != TokenKind.IDENT or tok.quoted or tok.upper != w:
                return False
        return True

    def accept_punct(self, *chars: str) -> bool:
        tok = self.peek()
        if tok.kind == TokenKind.PUNCT and tok.value in chars:
            self.next()
            return True
        return False

    def expect_punct(self, ch: str) -> None:
        if not self.accept_punct(ch):
            self.error(f"Expected {ch!r}")

    def peek_punct(self, *chars: str) -> bool:
        tok = self.peek()
        return tok.kind == TokenKind.PUNCT and tok.value in chars

    # ── 标识符 ──────────────────────────────────────────────────────────

    def parse_identifier(self, what: str = "identifier") -> str:
        """取一个标识符 —— 关键字也可以当标识符(但**不加引号时**要小心)。"""
        tok = self.peek()
        if tok.kind == TokenKind.IDENT:
            self.next()
            return tok.value
        if tok.kind == TokenKind.STRING:
            # ClickHouse 接受把表名写成字符串(历史包袱)
            self.next()
            return tok.value
        self.error(f"Expected {what}")

    def parse_qualified_name(self) -> Tuple[Optional[str], str]:
        """解析 ``db.table`` 或 ``table``。"""
        first = self.parse_identifier("table name")
        if self.accept_punct("."):
            second = self.parse_identifier("table name")
            return first, second
        return None, first

    # ── 回溯 ────────────────────────────────────────────────────────────

    def _try(self, fn: Callable[[], Any], default: Any = None) -> Any:
        """有界回溯:失败则回退到调用前的位置并返回 ``default``。

        .. warning::

           回溯是**指数级风险**的来源。真实 ClickHouse 用
           ``max_parser_depth`` 与尽量少用回溯来控制它。
           mini-clickhouse 只在两处用它(元组 vs lambda、
           子查询 vs 元组),并且每次回溯都在一个有限的 token 窗口内。
        """
        save = self.index
        try:
            return fn()
        except MiniChError:
            self.index = save
            return default

    def _enter(self) -> None:
        self._current_depth += 1
        if self._current_depth > self.max_depth:
            raise_error(
                ErrorCode.QUERY_IS_TOO_LARGE,
                f"Maximum parse depth ({self.max_depth}) exceeded; "
                f"the query is nested too deeply",
            )

    def _leave(self) -> None:
        self._current_depth -= 1

    # ══════════════════════════════════════════════════════════════════════
    # 语句
    # ══════════════════════════════════════════════════════════════════════

    def parse_statement(self) -> A.Node:
        """解析一条语句(跳过前导的 ``;``)。"""
        while self.accept_punct(";"):
            pass
        if self.at_end():
            raise_error(ErrorCode.EMPTY_QUERY, "Empty query")
        tok = self.peek()
        if tok.kind == TokenKind.IDENT and not tok.quoted:
            handler = _STATEMENT_DISPATCH.get(tok.upper)
            if handler is not None:
                node = handler(self)
                self._finish_statement()
                return node
        if self.peek_punct("("):
            node = self.parse_select()
            self._finish_statement()
            return node
        self.error(f"Unexpected token {tok.value!r} at the start of a statement")

    def _finish_statement(self) -> None:
        """吃掉可选的 ``;``,不允许后面还有别的东西(除了注释)。"""
        self.accept_punct(";")

    # ── SELECT ──────────────────────────────────────────────────────────

    def parse_select(self) -> A.SelectQuery:
        self._enter()
        try:
            return self._parse_select_body()
        finally:
            self._leave()

    def _parse_select_body(self) -> A.SelectQuery:
        with_exprs = self._parse_with_clause()
        if self.accept_punct("("):
            # `(SELECT ...) UNION ALL SELECT ...` 这种带括号的整体
            inner = self.parse_select()
            self.expect_punct(")")
            query = inner
            if with_exprs:
                query.with_exprs = with_exprs + query.with_exprs
        else:
            self.expect_keyword("SELECT")
            query = A.SelectQuery(pos=self.peek().pos)
            query.with_exprs = with_exprs
            query.distinct = self.accept_keyword("DISTINCT")
            query.selects = self._parse_select_list()
            self._parse_from_and_rest(query)
        self._parse_union_tail(query)
        self._parse_format_tail(query)
        return query

    def _parse_with_clause(self) -> List[A.WithElement]:
        if not self.peek_keyword("WITH"):
            return []
        self.next()
        out: List[A.WithElement] = []
        while True:
            pos = self.peek().pos
            # 形态 A: name AS (subquery)
            # ``WITH name AS (SELECT …)`` —— 判据是"**名字后面跟着 AS**",
            # 而不是"名字后面跟着左括号"。早期版本用的是后者,
            # 于是 ``WITH x AS (SELECT 1)`` 会先被当成"名字 x、下一 token 是 AS"
            # 而进不来这个分支,回落成表达式形态再报错。
            if (self.peek().kind == TokenKind.IDENT
                    and self.peek(1).is_keyword("AS")
                    and self.peek(2).is_punct("(")):
                save = self.index
                name = self.parse_identifier()
                self.expect_keyword("AS")
                self.expect_punct("(")
                if self.peek_keyword("SELECT", "WITH"):
                    sub = self.parse_select()
                    self.expect_punct(")")
                    out.append(A.WithElement(name=name, expr=sub, is_subquery=True, pos=pos))
                    if not self.accept_punct(","):
                        break
                    continue
                self.index = save
            # 形态 B: expr AS name
            expr = self.parse_expression()
            self.expect_keyword("AS")
            name = self.parse_identifier("alias")
            out.append(A.WithElement(name=name, expr=expr, is_subquery=False, pos=pos))
            if not self.accept_punct(","):
                break
        return out

    def _parse_select_list(self) -> List[A.Node]:
        out: List[A.Node] = []
        while True:
            out.append(self._parse_select_item())
            if not self.accept_punct(","):
                break
        return out

    def _parse_select_item(self) -> A.Node:
        tok = self.peek()
        if tok.is_punct("*"):
            self.next()
            star = A.StarExpr(pos=tok.pos)
            self._parse_star_modifiers(star)
            return self._maybe_alias(star)
        if tok.kind == TokenKind.IDENT and self.peek(1).is_punct(".") and self.peek(2).is_punct("*"):
            qual = self.parse_identifier()
            self.expect_punct(".")
            self.expect_punct("*")
            star = A.StarExpr(qualified=qual, pos=tok.pos)
            self._parse_star_modifiers(star)
            return self._maybe_alias(star)
        expr = self.parse_expression()
        return self._maybe_alias(expr)

    def _parse_star_modifiers(self, star: A.StarExpr) -> None:
        """``* EXCEPT (a, b)`` 与 ``* REPLACE (expr AS name)``。"""
        while True:
            if self.peek_keyword("EXCEPT"):
                self.next()
                self._parse_bracket_list(star.except_columns, allow_parens=True)
                continue
            if self.peek_keyword("REPLACE"):
                self.next()
                self.expect_punct("(")
                while True:
                    e = self.parse_expression()
                    self.expect_keyword("AS")
                    name = self.parse_identifier("alias")
                    star.replace_columns.append((e, name))
                    if not self.accept_punct(","):
                        break
                self.expect_punct(")")
                continue
            break

    def _parse_bracket_list(self, out: List[str], allow_parens: bool = True) -> None:
        parens = self.accept_punct("(") if allow_parens else False
        while True:
            out.append(self.parse_identifier())
            if not self.accept_punct(","):
                break
        if parens:
            self.expect_punct(")")

    def _maybe_alias(self, expr: A.Node) -> A.Node:
        if self.accept_keyword("AS"):
            return A.AliasExpr(expr=expr, alias=self.parse_identifier("alias"), pos=expr.pos)
        # 隐式别名:`SELECT a b` —— 但后面跟子句关键字时不算别名
        tok = self.peek()
        if tok.kind == TokenKind.IDENT and not tok.quoted and tok.upper not in _CLAUSE_KEYWORDS:
            if not self.peek_punct(","):
                name = self.parse_identifier()
                return A.AliasExpr(expr=expr, alias=name, pos=expr.pos)
        if tok.kind == TokenKind.IDENT and tok.quoted:
            name = self.parse_identifier()
            return A.AliasExpr(expr=expr, alias=name, pos=expr.pos)
        return expr

    def _parse_from_and_rest(self, query: A.SelectQuery) -> None:
        if self.accept_keyword("FROM"):
            query.from_ = self._parse_table_expression()
        # ``ARRAY JOIN`` / ``LEFT ARRAY JOIN`` / ``ARRAY LEFT JOIN`` 三种写法
        # 在 ClickHouse 里都合法 —— 前两种是标准写法,第三种是它的历史顺序。
        while True:
            left = False
            if self.peek_keywords("LEFT", "ARRAY"):
                self.next()
                self.next()
                left = True
            elif self.accept_keyword("ARRAY"):
                left = self.accept_keyword("LEFT")
            else:
                break
            self.expect_keyword("JOIN")
            expr = self.parse_expression()
            alias = None
            if self.accept_keyword("AS"):
                alias = self.parse_identifier("alias")
            query.array_joins.append(A.ArrayJoinExpr(expr=expr, left=left, alias=alias))
        if self.accept_keyword("PREWHERE"):
            query.prewhere = self.parse_expression()
        if self.accept_keyword("WHERE"):
            query.where = self.parse_expression()
        if self.accept_keyword("GROUP"):
            self.expect_keyword("BY")
            query.group_by = self._parse_group_by()
        if self.accept_keyword("HAVING"):
            query.having = self.parse_expression()
        if self.accept_keyword("WINDOW"):
            while True:
                name = self.parse_identifier("window name")
                self.expect_keyword("AS")
                self.expect_punct("(")
                spec = self._parse_window_spec_body()
                self.expect_punct(")")
                query.windows.append((name, spec))
                if not self.accept_punct(","):
                    break
        if self.accept_keyword("ORDER"):
            self.expect_keyword("BY")
            query.order_by = self._parse_order_by()
        if self.accept_keyword("LIMIT"):
            self._parse_limit(query)
        if self.accept_keyword("OFFSET"):
            query.offset = self.parse_expression()
        if self.accept_keyword("SETTINGS"):
            query.settings = self._parse_settings_list()

    def _parse_group_by(self) -> A.GroupByClause:
        clause = A.GroupByClause(pos=self.peek().pos)
        if self.peek_keywords("GROUPING", "SETS"):
            self.next()
            self.next()
            self.expect_punct("(")
            while True:
                self.expect_punct("(")
                group: List[A.Node] = []
                if not self.accept_punct(")"):
                    while True:
                        group.append(self.parse_expression())
                        if not self.accept_punct(","):
                            break
                    self.expect_punct(")")
                else:
                    pass
                clause.grouping_sets.append(group)
                if not self.accept_punct(","):
                    break
            self.expect_punct(")")
            return clause
        if self.accept_keyword("ALL"):
            # GROUP BY ALL —— 按 SELECT 里所有非聚合表达式分组
            clause.elements.append(A.Identifier(name="__ALL__", pos=self.peek().pos))
        else:
            while True:
                clause.elements.append(self.parse_expression())
                if not self.accept_punct(","):
                    break
        while True:
            if self.accept_keywords("WITH", "ROLLUP"):
                clause.with_rollup = True
            elif self.accept_keywords("WITH", "CUBE"):
                clause.with_cube = True
            elif self.accept_keywords("WITH", "TOTALS"):
                clause.with_totals = True
            else:
                break
        return clause

    def _parse_order_by(self) -> List[A.OrderByElement]:
        out: List[A.OrderByElement] = []
        while True:
            expr = self.parse_expression()
            direction = "ASC"
            nulls = None
            collate = None
            if self.accept_keyword("ASC"):
                direction = "ASC"
            elif self.accept_keyword("DESC"):
                direction = "DESC"
            if self.accept_keyword("NULLS"):
                if self.accept_keyword("FIRST"):
                    nulls = "FIRST"
                else:
                    self.expect_keyword("LAST")
                    nulls = "LAST"
            if self.accept_keyword("COLLATE"):
                collate = self._parse_string_literal()
            element = A.OrderByElement(
                expr=expr, direction=direction, nulls=nulls, collate=collate, pos=expr.pos
            )
            if self.accept_keywords("WITH", "FILL"):
                element.with_fill = True
            out.append(element)
            if not self.accept_punct(","):
                break
        return out

    def _parse_limit(self, query: A.SelectQuery) -> None:
        """``LIMIT n`` / ``LIMIT n, m`` / ``LIMIT n OFFSET m`` / ``LIMIT n BY exprs``。"""
        first = self.parse_expression()
        if self.accept_punct(","):
            # MySQL 风格:`LIMIT offset, count`
            second = self.parse_expression()
            query.offset = first
            query.limit = second
        else:
            query.limit = first
        # ``LIMIT n BY expr`` 与 ``LIMIT n OFFSET m BY expr`` 都合法 ——
        # OFFSET 可以出现在 BY 之前。早期版本只认前一种,
        # 于是第二种会走到 ''LIMIT 的结束'' 并把 ``BY`` 留成悬挂 token。
        if self.accept_keyword("OFFSET") and query.offset is None:
            query.offset = self.parse_expression()
        if self.accept_keyword("BY"):
            elements: List[A.Node] = []
            while True:
                elements.append(self.parse_expression())
                if not self.accept_punct(","):
                    break
            query.limit_by = A.LimitByClause(
                limit=query.limit, offset=query.offset, elements=elements
            )
            query.limit = None
            query.offset = None

    def _parse_settings_list(self) -> List[Tuple[str, A.Node]]:
        out: List[Tuple[str, A.Node]] = []
        while True:
            name = self.parse_identifier("setting name")
            self.expect_punct("=")
            out.append((name, self.parse_expression()))
            if not self.accept_punct(","):
                break
        return out

    def _parse_union_tail(self, query: A.SelectQuery) -> None:
        while True:
            save = self.index
            if self.accept_keyword("UNION"):
                distinct = self.accept_keyword("DISTINCT")
                self.accept_keyword("ALL")
                if not distinct:
                    query.union_distinct = False
                else:
                    query.union_distinct = True
                branch = self._parse_select_branch()
                query.unions.append(branch)
                continue
            self.index = save
            break

    def _parse_select_branch(self) -> A.SelectQuery:
        """UNION 的右侧分支(可以带自己的 WITH)。"""
        with_exprs = self._parse_with_clause()
        if self.accept_punct("("):
            inner = self.parse_select()
            self.expect_punct(")")
            return inner
        self.expect_keyword("SELECT")
        q = A.SelectQuery(pos=self.peek().pos)
        q.with_exprs = with_exprs
        q.distinct = self.accept_keyword("DISTINCT")
        q.selects = self._parse_select_list()
        self._parse_from_and_rest(q)
        return q

    def _parse_format_tail(self, query: A.SelectQuery) -> None:
        if self.accept_keyword("INTO"):
            self.expect_keyword("OUTFILE")
            query.into_outfile = self._parse_string_literal()
        if self.accept_keyword("FORMAT"):
            query.format = self.parse_identifier("format name")

    def _parse_string_literal(self) -> str:
        tok = self.peek()
        if tok.kind != TokenKind.STRING:
            self.error("Expected a string literal")
        self.next()
        return tok.value

    # ── FROM ────────────────────────────────────────────────────────────

    def _parse_table_expression(self) -> A.Node:
        left = self._parse_table_factor()
        while True:
            join = self._try_parse_join(left)
            if join is None:
                break
            left = join
        return left

    def _parse_table_factor(self) -> A.Node:
        if self.accept_punct("("):
            if self.peek_keyword("SELECT", "WITH"):
                sub = self.parse_select()
                self.expect_punct(")")
                ref = A.SubqueryRef(query=sub)
                if self.accept_keyword("AS"):
                    ref.alias = self.parse_identifier("alias")
                elif self.peek().kind == TokenKind.IDENT and not self.peek().quoted \
                        and self.peek().upper not in _CLAUSE_KEYWORDS \
                        and not self._next_is_join_keyword():
                    ref.alias = self.parse_identifier("alias")
                return ref
            inner = self._parse_table_expression()
            self.expect_punct(")")
            return inner
        pos = self.peek().pos
        database, table = self.parse_qualified_name()
        ref = A.TableReference(database=database, table=table, pos=pos)
        if self.peek_punct("("):
            # 表函数
            self.expect_punct("(")
            args: List[A.Node] = []
            if not self.accept_punct(")"):
                while True:
                    args.append(self.parse_expression())
                    if not self.accept_punct(","):
                        break
                self.expect_punct(")")
            tfn = A.TableFunctionCall(name=table, args=args, pos=pos)
            ref = A.TableReference(table_function=tfn, pos=pos)
        if self.accept_keyword("FINAL"):
            ref.final = True
        if self.accept_keyword("SAMPLE"):
            ref.sample = self.parse_expression()
        if self.accept_keyword("AS"):
            ref.alias = self.parse_identifier("alias")
        elif self.peek().kind == TokenKind.IDENT and not self.peek().quoted \
                and self.peek().upper not in _CLAUSE_KEYWORDS \
                and not self._next_is_join_keyword():
            ref.alias = self.parse_identifier("alias")
        return ref

    def _next_is_join_keyword(self) -> bool:
        tok = self.peek()
        if tok.kind != TokenKind.IDENT:
            return False
        return tok.upper in ("JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS",
                             "GLOBAL", "ANY", "ALL", "ASOF", "SEMI", "ANTI", "PASTE")

    def _try_parse_join(self, left: A.Node) -> Optional[A.JoinExpr]:
        save = self.index
        if self.accept_punct(","):
            # `FROM a, b` 等价于 CROSS JOIN
            right = self._parse_table_factor()
            return A.JoinExpr(left=left, right=right, kind="CROSS", strictness="ALL",
                              pos=self.peek().pos)
        global_ = self.accept_keyword("GLOBAL")
        strictness = "ALL"
        kind = "INNER"
        saw_any = False
        # 顺序可以是 `LEFT ANY JOIN` 也可以是 `ANY LEFT JOIN`
        for _ in range(3):
            if self.accept_keyword("ANY"):
                strictness, saw_any = "ANY", True
            elif self.accept_keyword("ALL"):
                strictness = "ALL"
            elif self.accept_keyword("ASOF"):
                strictness = "ASOF"
            elif self.accept_keyword("SEMI"):
                strictness = "SEMI"
            elif self.accept_keyword("ANTI"):
                strictness = "ANTI"
            elif self.accept_keyword("INNER"):
                kind = "INNER"
            elif self.accept_keyword("LEFT"):
                kind = "LEFT"
                self.accept_keyword("OUTER")
            elif self.accept_keyword("RIGHT"):
                kind = "RIGHT"
                self.accept_keyword("OUTER")
            elif self.accept_keyword("FULL"):
                kind = "FULL"
                self.accept_keyword("OUTER")
            elif self.accept_keyword("CROSS"):
                kind = "CROSS"
            elif self.accept_keyword("PASTE"):
                kind = "PASTE"
            else:
                break
        if not self.accept_keyword("JOIN"):
            self.index = save
            return None
        # STRAIGHT_JOIN 之类的修饰在这个位置(mini-clickhouse 不支持,但不要误解成普通 JOIN)
        right = self._parse_table_factor()
        on_expr = None
        using: List[str] = []
        if self.accept_keyword("ON"):
            on_expr = self.parse_expression()
        elif self.accept_keyword("USING"):
            self.expect_punct("(")
            while True:
                using.append(self.parse_identifier())
                if not self.accept_punct(","):
                    break
            self.expect_punct(")")
        elif kind not in ("CROSS", "PASTE"):
            # ``CROSS JOIN`` 与 ``PASTE JOIN`` 都是"按位置拼"的,
            # 本来就没有连接条件 —— 要求它们写 ON 是无意义的。
            self.error("JOIN requires either ON or USING")
        del saw_any
        return A.JoinExpr(
            left=left, right=right, kind=kind, strictness=strictness,
            on_expr=on_expr, using_columns=using, global_=global_,
        )

    # ══════════════════════════════════════════════════════════════════════
    # 表达式
    # ══════════════════════════════════════════════════════════════════════

    def parse_expression(self) -> A.Node:
        self._enter()
        try:
            return self._parse_or()
        finally:
            self._leave()

    def _parse_or(self) -> A.Node:
        left = self._parse_and()
        while self.peek_keyword("OR"):
            pos = self.next().pos
            right = self._parse_and()
            left = A.BinaryOp(op="OR", left=left, right=right, pos=pos)
        return left

    def _parse_and(self) -> A.Node:
        left = self._parse_not()
        while self.peek_keyword("AND"):
            pos = self.next().pos
            right = self._parse_not()
            left = A.BinaryOp(op="AND", left=left, right=right, pos=pos)
        return left

    def _parse_not(self) -> A.Node:
        if self.peek_keyword("NOT") and not self.peek(1).is_punct("("):
            pos = self.next().pos
            operand = self._parse_not()
            return A.UnaryOp(op="NOT", operand=operand, pos=pos)
        return self._parse_comparison()

    def _parse_comparison(self) -> A.Node:
        left = self._parse_concat()
        while True:
            tok = self.peek()
            if tok.kind == TokenKind.PUNCT and tok.value in _COMPARISON_OPS:
                self.next()
                right = self._parse_concat()
                left = A.BinaryOp(op=tok.value, left=left, right=right, pos=tok.pos)
                continue
            if self.peek_keyword("IS"):
                pos = self.next().pos
                negated = self.accept_keyword("NOT")
                self.expect_keyword("NULL")
                left = A.IsNullExpr(expr=left, negated=negated, pos=pos)
                continue
            if self.peek_keyword("IN"):
                self.next()
                right = self._parse_in_rhs()
                left = A.InExpr(left=left, right=right, negated=False, pos=tok.pos)
                continue
            if self.peek_keyword("BETWEEN"):
                self.next()
                low = self._parse_concat()
                self.expect_keyword("AND")
                high = self._parse_concat()
                left = A.BetweenExpr(expr=left, low=low, high=high, negated=False, pos=tok.pos)
                continue
            if self.peek_keyword("LIKE", "ILIKE", "REGEXP"):
                kw = self.next()
                pattern = self._parse_concat()
                left = A.LikeExpr(
                    left=left, pattern=pattern, negated=False,
                    case_insensitive=kw.upper == "ILIKE",
                    is_regexp=kw.upper == "REGEXP", pos=kw.pos,
                )
                continue
            if self.peek_keyword("NOT") and self.peek(1).kind == TokenKind.IDENT:
                nxt = self.peek(1).upper
                if nxt in ("IN", "LIKE", "ILIKE", "BETWEEN", "REGEXP"):
                    self.next()
                    self.next()
                    if nxt == "IN":
                        right = self._parse_in_rhs()
                        left = A.InExpr(left=left, right=right, negated=True, pos=tok.pos)
                    elif nxt == "BETWEEN":
                        low = self._parse_concat()
                        self.expect_keyword("AND")
                        high = self._parse_concat()
                        left = A.BetweenExpr(expr=left, low=low, high=high, negated=True, pos=tok.pos)
                    else:
                        pattern = self._parse_concat()
                        left = A.LikeExpr(
                            left=left, pattern=pattern, negated=True,
                            case_insensitive=nxt == "ILIKE", is_regexp=nxt == "REGEXP",
                            pos=tok.pos,
                        )
                    continue
            break
        return left

    def _parse_in_rhs(self) -> A.Node:
        """``IN`` 的右操作数:子查询、元组、或单个表达式。"""
        if self.peek_punct("("):
            save = self.index
            self.next()
            if self.peek_keyword("SELECT", "WITH"):
                sub = self.parse_select()
                self.expect_punct(")")
                return A.SubqueryExpr(query=sub)
            self.index = save
            return self._parse_primary()
        # `x IN table_name` 也是合法的(等价于 IN (SELECT ... FROM table))
        return self._parse_concat()

    def _parse_concat(self) -> A.Node:
        left = self._parse_additive()
        while self.peek_punct("||"):
            pos = self.next().pos
            right = self._parse_additive()
            left = A.BinaryOp(op="||", left=left, right=right, pos=pos)
        return left

    def _parse_additive(self) -> A.Node:
        left = self._parse_multiplicative()
        while self.peek_punct("+", "-"):
            tok = self.next()
            right = self._parse_multiplicative()
            left = A.BinaryOp(op=tok.value, left=left, right=right, pos=tok.pos)
        return left

    def _parse_multiplicative(self) -> A.Node:
        left = self._parse_unary()
        while self.peek_punct("*", "/", "%"):
            tok = self.next()
            right = self._parse_unary()
            left = A.BinaryOp(op=tok.value, left=left, right=right, pos=tok.pos)
        return left

    def _parse_unary(self) -> A.Node:
        tok = self.peek()
        if tok.kind == TokenKind.PUNCT and tok.value in ("-", "+", "~", "!"):
            self.next()
            operand = self._parse_unary()
            if tok.value == "-":
                # 负数字面量的特例:`-1` 直接当字面量,便于常量折叠
                if isinstance(operand, A.Literal) and isinstance(operand.value, (int, float)):
                    return A.Literal(value=-operand.value, pos=tok.pos)
            return A.UnaryOp(op=tok.value, operand=operand, pos=tok.pos)
        if tok.kind == TokenKind.IDENT and not tok.quoted and tok.upper == "NOT":
            self.next()
            operand = self._parse_unary()
            return A.UnaryOp(op="NOT", operand=operand, pos=tok.pos)
        return self._parse_postfix()

    def _parse_postfix(self) -> A.Node:
        node = self._parse_primary()
        while True:
            if self.peek_punct("["):
                pos = self.next().pos
                index = self.parse_expression()
                self.expect_punct("]")
                node = A.IndexExpr(base=node, index=index, pos=pos)
                continue
            if self.peek_punct("::"):
                self.next()
                type_name = self.parse_type_name()
                node = A.CastExpr(expr=node, type_name=type_name, pos=pos_of(node))
                continue
            if self.peek_punct(".") and (
                self.peek(1).kind == TokenKind.NUMBER
                or (self.peek(1).kind == TokenKind.IDENT)
            ):
                # `t.1`(元组元素)或 `x.field`
                self.next()
                nxt = self.next()
                if nxt.kind == TokenKind.NUMBER:
                    node = A.IndexExpr(
                        base=node, index=A.Literal(value=int(nxt.value), pos=nxt.pos), pos=nxt.pos
                    )
                else:
                    node = A.FunctionCall(
                        name="tupleElement", args=[node, A.Literal(value=nxt.value, pos=nxt.pos)],
                        combinator=None, pos=nxt.pos,
                    )
                continue
            break
        return node

    # ── primary ─────────────────────────────────────────────────────────

    def _parse_primary(self) -> A.Node:
        tok = self.peek()

        # lambda: `x -> expr`
        if tok.kind == TokenKind.IDENT and self.peek(1).is_punct("->"):
            name = self.parse_identifier()
            self.next()  # ->
            body = self.parse_expression()
            return A.Lambda(params=[name], body=body, pos=tok.pos)

        if tok.kind == TokenKind.NUMBER:
            self.next()
            return A.Literal(value=tok.value, pos=tok.pos)
        if tok.kind == TokenKind.STRING:
            self.next()
            return A.Literal(value=tok.value, type_name="String", pos=tok.pos)
        if tok.kind == TokenKind.PARAM:
            self.next()
            return A.ParameterExpr(name=tok.value[0], type_name=tok.value[1], pos=tok.pos)

        if tok.kind == TokenKind.PUNCT and tok.value == "[":
            self.next()
            items: List[A.Node] = []
            if not self.accept_punct("]"):
                while True:
                    items.append(self.parse_expression())
                    if not self.accept_punct(","):
                        break
                self.expect_punct("]")
            return A.ArrayLiteral(items=items, pos=tok.pos)

        if tok.kind == TokenKind.PUNCT and tok.value == "{":
            self.next()
            pairs: List[Tuple[A.Node, A.Node]] = []
            if not self.accept_punct("}"):
                while True:
                    k = self.parse_expression()
                    self.expect_punct(":")
                    v = self.parse_expression()
                    pairs.append((k, v))
                    if not self.accept_punct(","):
                        break
                self.expect_punct("}")
            return A.MapLiteral(pairs=pairs, pos=tok.pos)

        if tok.kind == TokenKind.PUNCT and tok.value == "(":
            return self._parse_paren_expression()

        if tok.kind == TokenKind.PUNCT and tok.value == "*":
            self.next()
            return A.StarExpr(pos=tok.pos)

        if tok.kind == TokenKind.IDENT:
            upper = tok.upper if not tok.quoted else ""
            if upper == "NULL":
                self.next()
                return A.Literal(value=None, pos=tok.pos)
            if upper in ("TRUE", "FALSE"):
                self.next()
                return A.Literal(value=upper == "TRUE", type_name="Bool", pos=tok.pos)
            if upper in ("NAN", "INF", "INFINITY") and not self.peek(1).is_punct("("):
                # ``nan`` / ``inf`` / ``-inf`` 是**浮点字面量**,不是标识符。
                # 真实 ClickHouse 直接支持它们(这也是它唯一能"写出"
                # 非有限值的方式,因为 ``CAST('nan' AS Float64)`` 走的是
                # 另一条路)。漏掉它们的症状很隐蔽:``SELECT nan`` 报
                # "Unknown identifier: 'nan'" —— 一条错误信息指向了
                # 完全错误的方向(读者会去查列名,而问题在词法/语法层)。
                import math as _math

                self.next()
                value = {"NAN": _math.nan, "INF": _math.inf, "INFINITY": _math.inf}[upper]
                return A.Literal(value=value, type_name="Float64", pos=tok.pos)
            if upper == "CASE":
                return self._parse_case()
            if upper == "CAST":
                return self._parse_cast()
            if upper == "INTERVAL":
                return self._parse_interval()
            if upper == "EXTRACT":
                return self._parse_extract()
            if upper == "EXISTS" and self.peek(1).is_punct("("):
                self.next()
                self.expect_punct("(")
                sub = self.parse_select()
                self.expect_punct(")")
                return A.FunctionCall(
                    name="exists", args=[A.SubqueryExpr(query=sub)], pos=tok.pos
                )
            if upper == "SUBSTRING" and self.peek(1).is_punct("("):
                return self._parse_substring()
            if upper == "TRIM" and self.peek(1).is_punct("("):
                return self._parse_trim()
            # 普通标识符 / 函数调用
            return self._parse_identifier_or_call()

        self.error(f"Unexpected token {tok.value!r} in expression")

    def _parse_paren_expression(self) -> A.Node:
        """``(`` 开头的表达式 —— 子查询 / 元组 / lambda / 普通括号。"""
        pos = self.peek().pos
        # lambda: (x, y) -> expr
        lam = self._try(self._parse_paren_lambda)
        if lam is not None:
            return lam
        save = self.index
        self.next()  # (
        # 子查询
        if self.peek_keyword("SELECT", "WITH"):
            sub = self.parse_select()
            self.expect_punct(")")
            return A.SubqueryExpr(query=sub, pos=pos)
        first = self.parse_expression()
        if self.accept_punct(","):
            items = [first]
            while True:
                items.append(self.parse_expression())
                if not self.accept_punct(","):
                    break
            self.expect_punct(")")
            return A.TupleLiteral(items=items, pos=pos)
        self.expect_punct(")")
        return self.parse_postfix_tail(first)

    def parse_postfix_tail(self, node: A.Node) -> A.Node:
        """给"括号里的表达式"补上后置运算(``(a)[1]``、``(a).x``)。"""
        while True:
            if self.peek_punct("["):
                self.next()
                index = self.parse_expression()
                self.expect_punct("]")
                node = A.IndexExpr(base=node, index=index, pos=pos_of(node))
                continue
            if self.peek_punct("::"):
                self.next()
                node = A.CastExpr(expr=node, type_name=self.parse_type_name(), pos=pos_of(node))
                continue
            break
        return node

    def _parse_paren_lambda(self) -> Optional[A.Lambda]:
        pos = self.peek().pos
        self.expect_punct("(")
        params: List[str] = []
        if not self.accept_punct(")"):
            while True:
                if self.peek().kind != TokenKind.IDENT:
                    self.error("Expected a lambda parameter name")
                params.append(self.parse_identifier())
                if not self.accept_punct(","):
                    break
            self.expect_punct(")")
        if not self.accept_punct("->"):
            self.error("Expected '->' after lambda parameters")
        body = self.parse_expression()
        return A.Lambda(params=params, body=body, pos=pos)

    def _parse_case(self) -> A.CaseExpr:
        pos = self.next().pos  # CASE
        operand = None
        if not self.peek_keyword("WHEN"):
            operand = self.parse_expression()
        whens: List[Tuple[A.Node, A.Node]] = []
        while self.accept_keyword("WHEN"):
            cond = self.parse_expression()
            self.expect_keyword("THEN")
            res = self.parse_expression()
            whens.append((cond, res))
        else_expr = None
        if self.accept_keyword("ELSE"):
            else_expr = self.parse_expression()
        self.expect_keyword("END")
        return A.CaseExpr(operand=operand, whens=whens, else_expr=else_expr, pos=pos)

    def _parse_cast(self) -> A.Node:
        """``CAST(x AS T)`` / ``CAST(x, 'T')`` —— 两种写法都支持。"""
        pos = self.next().pos
        self.expect_punct("(")
        expr = self.parse_expression()
        if self.accept_keyword("AS"):
            type_name = self.parse_type_name()
            self.expect_punct(")")
            return A.CastExpr(expr=expr, type_name=type_name, pos=pos)
        self.expect_punct(",")
        type_tok = self.peek()
        if type_tok.kind != TokenKind.STRING:
            self.error("Expected a type name string in CAST")
        self.next()
        self.expect_punct(")")
        return A.CastExpr(expr=expr, type_name=type_tok.value, pos=pos)

    def _parse_interval(self) -> A.IntervalExpr:
        pos = self.next().pos  # INTERVAL
        amount = self._parse_unary()
        kind_tok = self.peek()
        if kind_tok.kind != TokenKind.IDENT:
            self.error("Expected an interval unit (DAY, HOUR, …)")
        self.next()
        kind = kind_tok.upper.rstrip("S") if kind_tok.upper.endswith("S") else kind_tok.upper
        # 复数形式(DAYS/WEEKS/MONTHS…)在 ClickHouse 里也接受
        if kind_tok.upper in ("NANOSECONDS", "MICROSECONDS", "MILLISECONDS"):
            kind = kind_tok.upper.rstrip("S")
        return A.IntervalExpr(amount=amount, kind=kind, pos=pos)

    def _parse_extract(self) -> A.FunctionCall:
        """``EXTRACT(part FROM expr)`` → ``to<Part>(expr)``。

        同时支持**函数写法** ``extract(haystack, pattern)`` —— 真实 ClickHouse
        里它是 ``extractAll`` 的别名。两种写法的第一个 token 不同(日期部件是
        裸标识符、函数实参是任意表达式),所以这里做一次**有界回溯**:
        先试 ``part FROM expr``,不是就给普通函数调用。
        """
        pos = self.next().pos
        self.expect_punct("(")

        def _try_from_form() -> A.FunctionCall:
            part = self.parse_identifier("date part")
            self.expect_keyword("FROM")
            expr = self.parse_expression()
            self.expect_punct(")")
            return A.FunctionCall(name=f"to{part.capitalize()}", args=[expr], pos=pos)

        got = self._try(_try_from_form)
        if got is not None:
            return got
        # 回溯到 ``(`` 之后,按普通函数调用解析:``extract(a, b, ...)``。
        args = [self.parse_expression()]
        while self.accept_punct(","):
            args.append(self.parse_expression())
        self.expect_punct(")")
        return A.FunctionCall(name="extract", args=args, pos=pos)

    def _parse_substring(self) -> A.FunctionCall:
        """``SUBSTRING(s FROM a FOR b)`` 与 ``substring(s, a, b)`` 都支持。"""
        pos = self.next().pos
        self.expect_punct("(")
        args = [self.parse_expression()]
        if self.accept_keyword("FROM"):
            args.append(self.parse_expression())
            if self.accept_keyword("FOR"):
                args.append(self.parse_expression())
            self.expect_punct(")")
            return A.FunctionCall(name="substring", args=args, pos=pos)
        while self.accept_punct(","):
            args.append(self.parse_expression())
        self.expect_punct(")")
        return A.FunctionCall(name="substring", args=args, pos=pos)

    def _parse_trim(self) -> A.FunctionCall:
        """``TRIM(BOTH 'x' FROM s)`` 等四种写法。"""
        pos = self.next().pos
        self.expect_punct("(")
        mode = "trimBoth"
        if self.accept_keyword("LEADING"):
            mode = "trimLeft"
        elif self.accept_keyword("TRAILING"):
            mode = "trimRight"
        elif self.accept_keyword("BOTH"):
            mode = "trimBoth"
        args: List[A.Node] = []
        if self.peek().kind == TokenKind.STRING and not self.peek(1).is_punct(")", ","):
            chars = A.Literal(value=self._parse_string_literal(), type_name="String")
            self.expect_keyword("FROM")
            args = [self.parse_expression(), chars]
            self.expect_punct(")")
            return A.FunctionCall(name=mode, args=args, pos=pos)
        while True:
            args.append(self.parse_expression())
            if not self.accept_punct(","):
                break
        self.expect_punct(")")
        return A.FunctionCall(name=mode, args=args, pos=pos)

    def _parse_call_arguments(self) -> Tuple[List[A.Node], bool]:
        """解析函数调用的实参列表,返回 ``(参数, 是否写了 DISTINCT)``。

        两个小语法糖都在这里消化:

        .. code-block:: text

           count(*)           →  count()           ``*`` 在计数函数里不代表任何列
           count(DISTINCT x)  →  countDistinct(x)  转成 -Distinct 组合器

        **为什么要在解析期做?** 因为这两个写法在语义上是"另一种写法",
        而不是"另一种运算" —— 把它们留到执行期,聚合函数的实现里就
        到处都是 `if len(args) == 0` 和 `if distinct:` 的分支。
        真实 ClickHouse 也是在解析/分析阶段把它们换算掉的。
        """
        self.expect_punct("(")
        args: List[A.Node] = []
        distinct = False
        if not self.accept_punct(")"):
            if self.accept_keyword("DISTINCT"):
                distinct = True
            while True:
                if self.peek_punct("*"):
                    # ``count(*)`` 里的 ``*`` —— 它**不是**通配符,
                    # 只是"我不关心哪一列"的惯用写法。吞掉它、**不产出实参**,
                    # 让 ``count()`` 去处理"数行数"这件事。
                    # (``count(a, *)`` 这种写法也一并容忍。)
                    self.next()
                else:
                    args.append(self.parse_expression())
                if not self.accept_punct(","):
                    break
            self.expect_punct(")")
        return args, distinct

    def _parse_identifier_or_call(self) -> A.Node:
        pos = self.peek().pos
        parts: List[str] = [self.parse_identifier()]
        # 多级名字:`a.b.c`
        while self.peek_punct(".") and self.peek(1).kind == TokenKind.IDENT:
            self.next()
            parts.append(self.parse_identifier())
        if len(parts) == 2 and not self.peek_punct("("):
            # ``表.列`` —— **保持成一个限定标识符** ``e.user``。
            #
            # 为什么不能在这里就降级成 ``tupleElement(e, 'user')``?
            # 因为那样一来"名字解析"就丢了:JOIN 的 ON 条件要按名字
            # 判断一个表达式属于**左表还是右表**,而 ``e`` 不是列、
            # 是表别名 —— ``tupleElement(e, ...)`` 找不到 ``e``,
            # 于是 ``ON e.user = u.user`` 会被判成"没有等值条件"。
            # (这个坑在实现时确实踩到了。)
            return A.Identifier(name=".".join(parts), pos=pos)
        if len(parts) > 2 and not self.peek_punct("("):
            # 三级以上(``a.b.c``)—— 这是**元组字段访问**,用链式 tupleElement 表达
            node: A.Node = A.Identifier(name=parts[0], pos=pos)
            for p in parts[1:]:
                node = A.FunctionCall(
                    name="tupleElement", args=[node, A.Literal(value=p)], pos=pos
                )
            return node
        name = ".".join(parts)
        if self.peek_punct("("):
            # 第一个括号里装的可能是**参数**也可能是**实参** ——
            # 只有后面还跟着一个左括号时,它才是参数(`quantile(0.5)(x)`)。
            # 所以这里先按"实参"的规则解析(它会消化 `*` 与 `DISTINCT`),
            # 再看后面还有没有括号。
            first, first_distinct = self._parse_call_arguments()
            if self.peek_punct("("):
                args, distinct = self._parse_call_arguments()
                if distinct:
                    # ``uniq(DISTINCT x)`` 这种写法少见但合法
                    name = name + "Distinct"
                call = A.FunctionCall(name=name, args=args, params=first, pos=pos)
            else:
                if first_distinct:
                    # ``count(DISTINCT x)`` 是 ``countDistinct(x)`` 的语法糖 ——
                    # 真实 ClickHouse 允许在**聚合函数**里写 ``DISTINCT``,
                    # 它等价于给函数名加上 ``-Distinct`` 组合器。
                    name = name + "Distinct"
                call = A.FunctionCall(name=name, args=first, pos=pos)
            # 窗口函数
            if self.peek_keyword("OVER"):
                self.next()
                call = self._wrap_window(call)
            return call
        if self.peek_keyword("OVER"):
            ident = A.Identifier(name=name, pos=pos)
            self.next()
            return self._wrap_window(ident)
        return A.Identifier(name=name, quoted=self.tokens[self.index - 1].quoted, pos=pos)

    def _wrap_window(self, fn: A.Node) -> A.WindowFunction:
        if self.peek_punct("("):
            self.next()
            spec = self._parse_window_spec_body()
            self.expect_punct(")")
        else:
            spec = A.WindowSpec(name=self.parse_identifier("window name"))
        return A.WindowFunction(function=fn, window=spec, pos=pos_of(fn))

    def _parse_window_spec_body(self) -> A.WindowSpec:
        spec = A.WindowSpec(pos=self.peek().pos)
        if self.accept_keyword("PARTITION"):
            self.expect_keyword("BY")
            while True:
                spec.partition_by.append(self.parse_expression())
                if not self.accept_punct(","):
                    break
        if self.accept_keyword("ORDER"):
            self.expect_keyword("BY")
            spec.order_by = self._parse_order_by()
        if self.peek_keyword("ROWS", "RANGE"):
            spec.frame = self._parse_window_frame()
        return spec

    def _parse_window_frame(self) -> A.WindowFrame:
        ftype = self.next().upper
        if self.accept_keyword("BETWEEN"):
            start, start_off = self._parse_frame_bound()
            self.expect_keyword("AND")
            end, end_off = self._parse_frame_bound()
            return A.WindowFrame(type=ftype, start=start, start_offset=start_off,
                                 end=end, end_offset=end_off)
        start, start_off = self._parse_frame_bound()
        return A.WindowFrame(type=ftype, start=start, start_offset=start_off,
                             end="CURRENT", end_offset=None)

    def _parse_frame_bound(self) -> Tuple[str, Optional[A.Node]]:
        if self.accept_keyword("UNBOUNDED"):
            if self.accept_keyword("PRECEDING"):
                return "UNBOUNDED_PRECEDING", None
            self.expect_keyword("FOLLOWING")
            return "UNBOUNDED_FOLLOWING", None
        if self.accept_keywords("CURRENT", "ROW"):
            return "CURRENT", None
        offset = self.parse_expression()
        if self.accept_keyword("PRECEDING"):
            return "PRECEDING", offset
        self.expect_keyword("FOLLOWING")
        return "FOLLOWING", offset

    # ── 类型名 ──────────────────────────────────────────────────────────

    def parse_type_name(self) -> str:
        """从**原始 SQL 文本**里切出一个类型名。

        .. note::

           为什么不复用表达式解析器? 因为类型名里有 ``=``
           (``Enum8('a' = 1)``)、有裸标识符(``Tuple(a UInt8)``)、
           还有 ``Decimal(10, 2)`` 这种"数字参数"。硬套表达式语法
           会走不通,真实 ClickHouse 也是用专门的 ``ParserDataType``。

           实现上更简单也更可靠的做法是:**按括号配平切原文**。
           代价是类型名里不能有注释(真实 CH 也不允许)。
        """
        start_tok = self.peek()
        if start_tok.kind != TokenKind.IDENT:
            self.error("Expected a data type name")
        start = start_tok.pos
        self.next()
        end = start + _token_width(self.sql, start_tok)
        # 类型名最多只有**一层**后缀括号(括号内部递归由 get_data_type 处理):
        #   UInt8 / Array(Nullable(UInt8)) / Tuple(a UInt8, b String) / Decimal(10, 2)
        # 所以这里只要把第一对括号整体吃掉即可,不需要更复杂的规则。
        while self.peek_punct("("):
            depth = 0
            while True:
                tok = self.next()
                if tok.kind == TokenKind.EOF:
                    self.error("Unterminated data type name (missing ')')", start_tok)
                if tok.kind == TokenKind.PUNCT and tok.value == "(":
                    depth += 1
                elif tok.kind == TokenKind.PUNCT and tok.value == ")":
                    depth -= 1
                elif tok.kind == TokenKind.PUNCT and tok.value in ("[", "{"):
                    depth += 1
                elif tok.kind == TokenKind.PUNCT and tok.value in ("]", "}"):
                    depth -= 1
                end = tok.pos + _token_width(self.sql, tok)
                if depth <= 0:
                    break
        text = self.sql[start:end].strip()
        if not text:
            self.error("Empty data type name")
        return text

    # ══════════════════════════════════════════════════════════════════════
    # 其它语句
    # ══════════════════════════════════════════════════════════════════════

    def parse_insert(self) -> A.InsertQuery:
        pos = self.next().pos  # INSERT
        self.expect_keyword("INTO")
        self.accept_keyword("TABLE")
        query = A.InsertQuery(pos=pos)
        if self.accept_keyword("FUNCTION"):
            name = self.parse_identifier("table function")
            self.expect_punct("(")
            args: List[A.Node] = []
            if not self.accept_punct(")"):
                while True:
                    args.append(self.parse_expression())
                    if not self.accept_punct(","):
                        break
                self.expect_punct(")")
            query.table_function = A.TableFunctionCall(name=name, args=args, pos=pos)
        else:
            query.database, query.table = self.parse_qualified_name()
            if self.accept_punct("("):
                if not self.accept_punct(")"):
                    while True:
                        query.columns.append(self.parse_identifier())
                        if not self.accept_punct(","):
                            break
                    self.expect_punct(")")
        if self.accept_keyword("SETTINGS"):
            query.settings = self._parse_settings_list()
        if self.accept_keyword("VALUES"):
            query.values = self._parse_values_rows()
        elif self.peek_keyword("SELECT", "WITH"):
            query.select = self.parse_select()
        elif self.accept_keyword("FORMAT"):
            # .. warning::
            #
            #    ``FORMAT`` 之后**不是 SQL,是数据**。这里要做的第一件事就是
            #    "别再用词法器碰它" —— 只把格式名这一个 token 取出来
            #    (不能走 ``parse_identifier``:它可能向后看一个 token,
            #    那就又切到数据里去了),然后按**原文偏移**把剩下的一整段
            #    切片交给格式解析器。
            #
            #    以前是"整条 SQL 先 tokenize 一遍,再从当前 token 的位置切片",
            #    于是数据里出现 ``\\``(TSV/CSV 对任何含反斜杠的值都这么写)
            #    就直接报 ``Unexpected character '\\'`` —— 自己写出去的文本
            #    自己读不回来。真实 ClickHouse 的 TSVRowInputFormat 也是
            #    "把剩余字节流原样拿走",不经过 SQL 词法器。
            fmt_tok = self.next()
            if fmt_tok.kind not in (TokenKind.IDENT, TokenKind.STRING):
                self.error("Expected a format name", fmt_tok)
            query.format = fmt_tok.value
            start = fmt_tok.pos + _token_width(self.sql, fmt_tok)
            n = len(self.sql)
            while start < n and self.sql[start] in " \t\r\n":
                start += 1
            # 数据部分由调用方从 sql 的当前位置取
            query.data = self.sql[start:]
            self._payload_taken = True
        return query

    def _parse_values_rows(self) -> List[List[A.Node]]:
        rows: List[List[A.Node]] = []
        while True:
            self.expect_punct("(")
            row: List[A.Node] = []
            if not self.accept_punct(")"):
                while True:
                    row.append(self.parse_expression())
                    if not self.accept_punct(","):
                        break
                self.expect_punct(")")
            rows.append(row)
            if not self.accept_punct(","):
                break
        return rows

    def parse_create(self) -> A.Node:
        pos = self.next().pos  # CREATE
        self.accept_keywords("OR", "REPLACE")
        temporary = self.accept_keyword("TEMPORARY")
        # CREATE ... VIEW / DICTIONARY / FUNCTION / USER / DATABASE / TABLE
        if self.peek_keyword("DATABASE"):
            self.next()
            q = A.CreateDatabaseQuery(pos=pos)
            q.if_not_exists = self._parse_if_not_exists()
            q.name = self.parse_identifier("database name")
            if self.accept_keyword("ENGINE"):
                self.expect_punct("=")
                q.engine = self.parse_identifier("engine name")
                q.engine_params = self._parse_engine_params()
            self.accept_keyword("COMMENT")
            return q
        materialized = self.accept_keyword("MATERIALIZED")
        if self.accept_keyword("VIEW") or materialized:
            return self._parse_create_view(pos, materialized)
        if self.accept_keyword("DICTIONARY"):
            return self._parse_create_dictionary(pos)
        if self.accept_keyword("FUNCTION"):
            q = A.CreateFunctionQuery(pos=pos)
            q.if_not_exists = self._parse_if_not_exists()
            q.name = self.parse_identifier("function name")
            self.expect_keyword("AS")
            q.body = self.parse_expression()
            return q
        if self.peek_keyword("USER", "ROLE", "QUOTA", "ROW", "SETTINGS"):
            return self._parse_create_access(pos)
        self.expect_keyword("TABLE")
        return self._parse_create_table(pos, temporary)

    def _parse_if_not_exists(self) -> bool:
        if self.accept_keywords("IF", "NOT", "EXISTS"):
            return True
        return False

    def _parse_engine_params(self) -> List[Any]:
        params: List[Any] = []
        if self.accept_punct("("):
            if not self.accept_punct(")"):
                while True:
                    tok = self.peek()
                    if tok.kind == TokenKind.STRING:
                        params.append(self._parse_string_literal())
                    elif tok.kind == TokenKind.NUMBER:
                        self.next()
                        params.append(tok.value)
                    else:
                        params.append(self.parse_identifier())
                    if not self.accept_punct(","):
                        break
                self.expect_punct(")")
        return params

    def _parse_create_table(self, pos: int, temporary: bool) -> A.CreateTableQuery:
        q = A.CreateTableQuery(pos=pos, temporary=temporary)
        q.if_not_exists = self._parse_if_not_exists()
        q.database, q.table = self.parse_qualified_name()
        if self.accept_keyword("ON"):
            self.expect_keyword("CLUSTER")
            q.cluster = self.parse_identifier("cluster name")
        if self.accept_keyword("UUID"):
            q.uuid = self._parse_string_literal()
        if self.peek_punct("("):
            self._parse_column_list(q)
        if self.accept_keyword("ENGINE"):
            self.accept_punct("=")
            q.engine = self.parse_identifier("engine name")
            q.engine_params = self._parse_engine_params()
        self._parse_storage_clauses(q)
        if self.accept_keyword("COMMENT"):
            q.comment = self._parse_string_literal()
        if self.accept_keyword("AS"):
            if self.peek_keyword("SELECT", "WITH"):
                q.as_select = self.parse_select()
            else:
                d, t = self.parse_qualified_name()
                q.as_table = A.TableReference(database=d, table=t)
        elif self.accept_keyword("SELECT"):
            self.index -= 1
            q.as_select = self.parse_select()
        return q

    def _parse_column_list(self, q: A.CreateTableQuery) -> None:
        self.expect_punct("(")
        while True:
            if self.accept_keyword("INDEX"):
                q.indexes.append(self._parse_index_definition())
            elif self.accept_keyword("CONSTRAINT"):
                name = self.parse_identifier("constraint name")
                kind = "CHECK"
                if self.accept_keyword("ASSUME"):
                    kind = "ASSUME"
                else:
                    self.expect_keyword("CHECK")
                expr = self.parse_expression()
                q.constraints.append(A.ConstraintDefinition(name=name, expr=expr, kind=kind))
            elif self.accept_keyword("PROJECTION"):
                name = self.parse_identifier("projection name")
                self.expect_punct("(")
                sub = self.parse_select()
                self.expect_punct(")")
                q.projections.append(A.ProjectionDefinition(name=name, query=sub))
            elif self.accept_keyword("PRIMARY"):
                self.expect_keyword("KEY")
                exprs = self._parse_expression_list_parens()
                q.primary_key.extend(exprs)
            else:
                q.columns.append(self._parse_column_definition())
            if not self.accept_punct(","):
                break
        self.expect_punct(")")

    def _parse_expression_list_parens(self) -> List[A.Node]:
        out: List[A.Node] = []
        if self.accept_punct("("):
            while True:
                out.append(self.parse_expression())
                if not self.accept_punct(","):
                    break
            self.expect_punct(")")
        else:
            out.append(self.parse_expression())
        return out

    def _parse_key_expression_list(self) -> List[A.Node]:
        """``CREATE TABLE ... ORDER BY`` 的**键表达式**列表(物化视图同款)。

        与 ``SELECT ... ORDER BY``(:meth:`_parse_order_by`)的关键区别:
        这里返回的是**表达式本身**,存储层要拿它去求值(当排序键),
        所以方向没有地方放 —— 只能作为**附加属性**挂在表达式节点上。
        读它的人是 :func:`minich.storages.merge_tree.merge_tree._directions_of`,
        它找的正是 ``getattr(e, "direction", "ASC")``(以及
        ``sorting_key_reverse`` 那一整套)。

        两种写法都接受(与真实 ClickHouse 一致 —— 括号只是分组,
        不改变"这是几个排序键"的语义):

        .. code-block:: sql

           ORDER BY (k1 ASC, k2 DESC)     -- 推荐的写法:两列,方向写在元素上
           ORDER BY k1, k2 DESC           -- 不带括号,等价

        ``PRIMARY KEY`` **不走这里** —— 真实 ClickHouse 的主键列表只接
        表达式,不接 ``ASC``/``DESC``(它总是排序键的前缀)。

        .. warning::

           方向词必须在这里消费掉。以前这一支直接调
            :meth:`_parse_expression_list_parens`,它不认识 ``ASC``/``DESC``
           —— 于是 ``ORDER BY k DESC`` 报 "Unexpected trailing token 'DESC'",
           引擎里那套 DESC 支持**永远走不到**(实打实的死代码)。
        """
        out: List[A.Node] = []
        paren = self.accept_punct("(")
        while True:
            expr = self.parse_expression()
            direction = "ASC"
            if self.accept_keyword("ASC"):
                direction = "ASC"
            elif self.accept_keyword("DESC"):
                direction = "DESC"
            # ``NULLS FIRST/LAST``:排序键里没有意义(存储顺序不保留 NULL 位置),
            # 但真实 CH 的语法接受它 —— 吃掉,不往引擎传。
            if self.accept_keyword("NULLS"):
                if not (self.accept_keyword("FIRST") or self.accept_keyword("LAST")):
                    self.error("NULLS requires FIRST or LAST")
            expr.direction = direction  # type: ignore[attr-defined]
            out.append(expr)
            if not self.accept_punct(","):
                break
        if paren:
            self.expect_punct(")")
        return out

    def _parse_column_definition(self) -> A.ColumnDefinition:
        pos = self.peek().pos
        col = A.ColumnDefinition(pos=pos)
        col.name = self.parse_identifier("column name")
        col.type_name = self.parse_type_name()
        while True:
            if self.accept_keyword("NOT"):
                self.expect_keyword("NULL")
                col.not_null = True
                continue
            if self.accept_keyword("DEFAULT"):
                col.default_kind = "DEFAULT"
                col.default_expr = self.parse_expression()
                continue
            if self.accept_keyword("MATERIALIZED"):
                col.default_kind = "MATERIALIZED"
                col.default_expr = self.parse_expression()
                continue
            if self.accept_keyword("ALIAS"):
                col.default_kind = "ALIAS"
                col.default_expr = self.parse_expression()
                continue
            if self.accept_keyword("EPHEMERAL"):
                col.default_kind = "EPHEMERAL"
                if not self.peek_punct(",", ")"):
                    col.default_expr = self.parse_expression()
                continue
            if self.accept_keyword("COMMENT"):
                col.comment = self._parse_string_literal()
                continue
            if self.accept_keyword("CODEC"):
                col.codec = self._parse_codec_text()
                continue
            if self.accept_keyword("TTL"):
                col.ttl = self.parse_expression()
                continue
            if self.accept_keyword("AUTO_INCREMENT"):
                col.auto_increment = True
                continue
            if self.accept_keyword("PRIMARY"):
                self.expect_keyword("KEY")
                col.primary_key = True
                continue
            break
        return col

    def _parse_codec_text(self) -> str:
        self.expect_punct("(")
        depth = 1
        start = self.peek().pos
        end = start
        while depth > 0:
            tok = self.next()
            if tok.kind == TokenKind.EOF:
                self.error("Unterminated CODEC clause")
            if tok.kind == TokenKind.PUNCT and tok.value == "(":
                depth += 1
            elif tok.kind == TokenKind.PUNCT and tok.value == ")":
                depth -= 1
                if depth == 0:
                    break
            end = tok.pos + _token_width(self.sql, tok)
        return self.sql[start:end].strip()

    def _parse_index_definition(self) -> A.IndexDefinition:
        idx = A.IndexDefinition()
        if_not_exists = self._parse_if_not_exists()
        del if_not_exists
        idx.name = self.parse_identifier("index name")
        idx.expr = self.parse_expression()
        self.expect_keyword("TYPE")
        idx.type = self.parse_identifier("index type")
        if self.peek_punct("("):
            self.expect_punct("(")
            if not self.accept_punct(")"):
                while True:
                    tok = self.peek()
                    if tok.kind == TokenKind.NUMBER:
                        self.next()
                        idx.type_params.append(tok.value)
                    elif tok.kind == TokenKind.STRING:
                        idx.type_params.append(self._parse_string_literal())
                    else:
                        idx.type_params.append(self.parse_identifier())
                    if not self.accept_punct(","):
                        break
                self.expect_punct(")")
        if self.accept_keyword("GRANULARITY"):
            tok = self.next()
            if tok.kind != TokenKind.NUMBER:
                self.error("GRANULARITY requires a number")
            idx.granularity = int(tok.value)
        return idx

    def _parse_storage_clauses(self, q: A.CreateTableQuery) -> None:
        while True:
            if self.accept_keyword("ORDER"):
                self.expect_keyword("BY")
                q.order_by.extend(self._parse_key_expression_list())
                continue
            if self.accept_keyword("PARTITION"):
                self.expect_keyword("BY")
                q.partition_by = self.parse_expression()
                continue
            if self.accept_keyword("PRIMARY"):
                self.expect_keyword("KEY")
                q.primary_key.extend(self._parse_expression_list_parens())
                continue
            if self.accept_keyword("SAMPLE"):
                self.expect_keyword("BY")
                q.sample_by = self.parse_expression()
                continue
            if self.accept_keyword("TTL"):
                q.ttl.append(self._parse_ttl_definition())
                continue
            if self.accept_keyword("SETTINGS"):
                while True:
                    name = self.parse_identifier("setting name")
                    self.expect_punct("=")
                    tok = self.peek()
                    if tok.kind == TokenKind.NUMBER:
                        self.next()
                        q.settings.append((name, tok.value))
                    elif tok.kind == TokenKind.STRING:
                        q.settings.append((name, self._parse_string_literal()))
                    else:
                        q.settings.append((name, self.parse_identifier()))
                    if not self.accept_punct(","):
                        break
                continue
            break

    def _parse_ttl_definition(self) -> A.TTLDefinition:
        ttl = A.TTLDefinition()
        ttl.expr = self.parse_expression()
        if self.accept_keyword("DELETE"):
            ttl.action = "DELETE"
        elif self.accept_keyword("TO"):
            if self.accept_keyword("DISK"):
                ttl.action = "TO DISK"
            else:
                self.expect_keyword("VOLUME")
                ttl.action = "TO VOLUME"
            ttl.target = self._parse_string_literal()
        elif self.accept_keyword("RECOMPRESS"):
            ttl.action = "RECOMPRESS"
            self.expect_keyword("CODEC")
            ttl.codec = self._parse_codec_text()
        if self.accept_keyword("WHERE"):
            ttl.where = self.parse_expression()
        if self.accept_keyword("GROUP"):
            self.expect_keyword("BY")
            while True:
                ttl.group_by.append(self.parse_expression())
                if not self.accept_punct(","):
                    break
        if self.accept_keyword("SET"):
            while True:
                name = self.parse_identifier()
                self.expect_punct("=")
                ttl.set_exprs.append((name, self.parse_expression()))
                if not self.accept_punct(","):
                    break
        return ttl

    def _parse_create_view(self, pos: int, materialized: bool) -> A.CreateViewQuery:
        q = A.CreateViewQuery(pos=pos, materialized=materialized)
        q.if_not_exists = self._parse_if_not_exists()
        q.database, q.name = self.parse_qualified_name()
        if self.accept_keyword("ON"):
            self.expect_keyword("CLUSTER")
            q.cluster = self.parse_identifier()
        if self.accept_punct("("):
            # 列清单(可选)
            while not self.accept_punct(")"):
                self.next()
        if self.accept_keyword("TO"):
            d, t = self.parse_qualified_name()
            q.to_table = A.TableReference(database=d, table=t)
        elif self.accept_keyword("ENGINE"):
            self.accept_punct("=")
            q.engine = self.parse_identifier("engine name")
            q.engine_params = self._parse_engine_params()
            if self.accept_keyword("ORDER"):
                self.expect_keyword("BY")
                # 与建表语句同一套:物化视图的排序键也允许 ``DESC``
                # (见 :meth:`_parse_key_expression_list`)。
                q.order_by.extend(self._parse_key_expression_list())
            if self.accept_keyword("PARTITION"):
                self.expect_keyword("BY")
                q.partition_by = self.parse_expression()
            if self.accept_keyword("PRIMARY"):
                self.expect_keyword("KEY")
                q.primary_key.extend(self._parse_expression_list_parens())
        # ``POPULATE`` 的**标准位置**在 ``AS`` 之前
        # (真实 ClickHouse:``… [ENGINE = engine] [POPULATE] AS SELECT …``)。
        # 放在这里不只是"更像真的":``AS SELECT col FROM t POPULATE`` 里的
        # 那个 ``POPULATE`` 会被解析器当成**隐式别名**吃掉(见
        # ``test_lossy_materialized_view_populate_becomes_an_alias``),
        # 于是建出来的物化视图**不回填**,而且不报错 —— 一个静默的错误。
        if self.accept_keyword("POPULATE"):
            q.populate = True
        self.expect_keyword("AS")
        q.select = self.parse_select()
        # 兼容旧写法:``AS SELECT … POPULATE`` 也接受(SELECT 后面还能被
        # 解析器"剩下"一个 POPULATE 时才到得了这里)。
        if self.accept_keyword("POPULATE"):
            q.populate = True
        return q

    def _parse_create_dictionary(self, pos: int) -> A.CreateDictionaryQuery:
        q = A.CreateDictionaryQuery(pos=pos)
        q.if_not_exists = self._parse_if_not_exists()
        q.database, q.name = self.parse_qualified_name()
        self.expect_punct("(")
        while True:
            attr_name = self.parse_identifier("attribute name")
            attr_type = self.parse_type_name()
            default = None
            if self.accept_keyword("DEFAULT"):
                default = self.parse_expression()
            q.attributes.append((attr_name, attr_type, default))
            if not self.accept_punct(","):
                break
        self.expect_punct(")")
        if self.accept_keyword("PRIMARY"):
            self.expect_keyword("KEY")
            while True:
                q.primary_key.append(self.parse_identifier())
                if not self.accept_punct(","):
                    break
        while True:
            if self.accept_keyword("SOURCE"):
                self.expect_punct("(")
                q.source = self.parse_identifier("source name")
                self.expect_punct("(")
                if not self.accept_punct(")"):
                    # 源的参数是**一串 ``键 值`` 对**,分隔既可以是逗号,
                    # 也可以是纯空白(真实 ClickHouse 写的是
                    # ``SOURCE(CLICKHOUSE(HOST 'x' PORT 9000 TABLE 'd'))`` ——
                    # 空白分隔)。我们两者都收,统一展平成一个列表,
                    # 由 ``minich/dictionaries.py`` 按交替键/值重组。
                    while True:
                        tok = self.peek()
                        if tok.kind == TokenKind.STRING:
                            q.source_params.append(self._parse_string_literal())
                        elif tok.kind == TokenKind.NUMBER:
                            self.next()
                            q.source_params.append(tok.value)
                        else:
                            q.source_params.append(self.parse_identifier())
                        if self.accept_punct(","):
                            continue
                        if self.peek_punct(")"):
                            break
                    self.expect_punct(")")
                self.expect_punct(")")
                continue
            if self.accept_keyword("LAYOUT"):
                self.expect_punct("(")
                q.layout = self.parse_identifier("layout name")
                if self.peek_punct("("):
                    self.expect_punct("(")
                    if not self.accept_punct(")"):
                        # 布局参数和源参数一样,是 ``键 值`` 对(空白或逗号分隔),
                        # 例如 ``LAYOUT(CACHE(SIZE_IN_CELLS 1000))``。
                        while True:
                            tok = self.peek()
                            if tok.kind == TokenKind.NUMBER:
                                self.next()
                                q.layout_params.append(tok.value)
                            else:
                                q.layout_params.append(self.parse_identifier())
                            if self.accept_punct(","):
                                continue
                            if self.peek_punct(")"):
                                break
                        self.expect_punct(")")
                self.expect_punct(")")
                continue
            if self.accept_keyword("LIFETIME"):
                self.expect_punct("(")
                if self.accept_punct(")"):
                    q.lifetime = "0"
                else:
                    # ``LIFETIME(300)`` / ``LIFETIME(300, 360)`` /
                    # ``LIFETIME(MIN 300 MAX 360)`` —— 三种写法都收成一段文本,
                    # 由 minich/dictionaries.py 的 parse_lifetime() 解释。
                    parts: List[str] = []
                    while not self.peek_punct(")"):
                        tok = self.next()
                        if tok.kind == TokenKind.STRING:
                            parts.append(str(tok.value))
                        else:
                            parts.append(str(tok.value))
                    q.lifetime = " ".join(parts)
                    self.expect_punct(")")
                continue
            if self.accept_keyword("COMMENT"):
                q.comment = self._parse_string_literal()
                continue
            break
        return q

    def _parse_create_access(self, pos: int) -> A.CreateUserQuery:
        q = A.CreateUserQuery(pos=pos)
        if self.accept_keyword("ROW"):
            self.expect_keyword("POLICY")
            q.kind = "ROW POLICY"
        elif self.accept_keyword("SETTINGS"):
            self.expect_keyword("PROFILE")
            q.kind = "SETTINGS PROFILE"
        else:
            tok = self.next()
            q.kind = tok.upper
        q.if_not_exists = self._parse_if_not_exists()
        q.name = self.parse_identifier("name")
        # 后续子句原样收集到语句结束(mini-clickhouse 不做完整的访问控制语法)
        while not self.at_end() and not self.peek_punct(";"):
            tok = self.next()
            q.clauses.append(str(tok.value))
        return q

    def parse_drop(self) -> A.DropQuery:
        pos = self.next().pos  # DROP
        q = A.DropQuery(pos=pos)
        if self.accept_keyword("DATABASE"):
            q.kind = "DATABASE"
        elif self.accept_keyword("DICTIONARY"):
            q.kind = "DICTIONARY"
        elif self.accept_keyword("VIEW"):
            q.kind = "VIEW"
        elif self.accept_keyword("FUNCTION"):
            q.kind = "FUNCTION"
        elif self.accept_keyword("USER"):
            q.kind = "USER"
        elif self.accept_keyword("ROLE"):
            q.kind = "ROLE"
        elif self.accept_keyword("TABLE"):
            q.kind = "TABLE"
        elif self.peek_keyword("TEMPORARY"):
            self.next()
            self.expect_keyword("TABLE")
            q.kind = "TABLE"
        else:
            self.error("Expected TABLE, DATABASE, VIEW, DICTIONARY or FUNCTION after DROP")
        q.if_exists = self._parse_if_exists()
        while True:
            d, t = self.parse_qualified_name()
            q.names.append(A.TableReference(database=d, table=t))
            if not self.accept_punct(","):
                break
        if self.accept_keyword("ON"):
            self.expect_keyword("CLUSTER")
            q.cluster = self.parse_identifier()
        if self.accept_keyword("NO"):
            self.expect_keyword("DELAY")
            q.no_delay = True
        if self.accept_keyword("SYNC"):
            q.sync = True
        return q

    def _parse_if_exists(self) -> bool:
        return self.accept_keywords("IF", "EXISTS")

    def parse_alter(self) -> A.AlterQuery:
        pos = self.next().pos  # ALTER
        if self.accept_keyword("USER") or self.accept_keyword("ROLE") \
                or self.accept_keyword("QUOTA") or self.accept_keyword("ROW") \
                or self.accept_keyword("SETTINGS"):
            q = A.CreateUserQuery(pos=pos, kind="ALTER")
            while not self.at_end():
                q.clauses.append(str(self.next().value))
            wrapper = A.AlterQuery(pos=pos)
            wrapper.commands.append(A.AlterCommand(kind="ACCESS"))
            wrapper.table = " ".join(q.clauses)
            return wrapper
        self.expect_keyword("TABLE")
        q = A.AlterQuery(pos=pos)
        q.if_exists = self._parse_if_exists()
        q.database, q.table = self.parse_qualified_name()
        if self.accept_keyword("ON"):
            self.expect_keyword("CLUSTER")
            q.cluster = self.parse_identifier()
        while True:
            q.commands.append(self._parse_alter_command())
            if not self.accept_punct(","):
                break
        return q

    def _parse_alter_command(self) -> A.AlterCommand:
        cmd = A.AlterCommand(pos=self.peek().pos)
        if self.accept_keyword("ADD"):
            if self.accept_keyword("COLUMN"):
                self._parse_if_not_exists()
                cmd.kind = "ADD_COLUMN"
                cmd.column = self._parse_column_definition()
                if self.accept_keyword("AFTER"):
                    cmd.setting_name = self.parse_identifier()
                elif self.accept_keyword("FIRST"):
                    cmd.setting_name = "__first__"
                return cmd
            if self.accept_keyword("INDEX"):
                cmd.kind = "ADD_INDEX"
                cmd.index = self._parse_index_definition()
                if self.accept_keyword("AFTER"):
                    cmd.name = self.parse_identifier()
                return cmd
            if self.accept_keyword("PROJECTION"):
                cmd.kind = "ADD_PROJECTION"
                cmd.name = self.parse_identifier()
                self.expect_punct("(")
                cmd.expr = self.parse_select()
                self.expect_punct(")")
                return cmd
            if self.accept_keyword("CONSTRAINT"):
                cmd.kind = "ADD_CONSTRAINT"
                self.accept_keywords("IF", "NOT", "EXISTS")
                cmd.name = self.parse_identifier("constraint name")
                if not self.accept_keyword("ASSUME"):
                    self.expect_keyword("CHECK")
                cmd.predicate = self.parse_expression()
                return cmd
            self.error("Unsupported ALTER ADD form")
        if self.accept_keyword("DROP"):
            if self.accept_keyword("COLUMN"):
                self._parse_if_exists()
                cmd.kind = "DROP_COLUMN"
                cmd.name = self.parse_identifier()
                return cmd
            if self.accept_keyword("INDEX"):
                self._parse_if_exists()
                cmd.kind = "DROP_INDEX"
                cmd.name = self.parse_identifier()
                return cmd
            if self.accept_keyword("PARTITION"):
                cmd.kind = "DROP_PARTITION"
                cmd.partition = self.parse_expression()
                return cmd
            if self.accept_keyword("PART"):
                cmd.kind = "DROP_PART"
                cmd.partition = self.parse_expression()
                return cmd
            if self.accept_keyword("DETACHED"):
                self.expect_keyword("PARTITION")
                cmd.kind = "DROP_DETACHED_PARTITION"
                cmd.partition = self.parse_expression()
                return cmd
            if self.accept_keyword("PROJECTION"):
                cmd.kind = "DROP_PROJECTION"
                cmd.name = self.parse_identifier()
                return cmd
            self.error("Unsupported ALTER DROP form")
        if self.accept_keyword("MATERIALIZE"):
            if self.accept_keyword("INDEX"):
                cmd.kind = "MATERIALIZE_INDEX"
                cmd.name = self.parse_identifier()
            elif self.accept_keyword("PROJECTION"):
                cmd.kind = "MATERIALIZE_PROJECTION"
                cmd.name = self.parse_identifier()
            else:
                self.expect_keyword("COLUMN")
                cmd.kind = "MATERIALIZE_COLUMN"
                cmd.name = self.parse_identifier()
            if self.accept_keyword("IN"):
                self.expect_keyword("PARTITION")
                cmd.partition = self.parse_expression()
            return cmd
        if self.accept_keyword("MODIFY"):
            if self.accept_keyword("COLUMN"):
                self._parse_if_exists()
                cmd.kind = "MODIFY_COLUMN"
                cmd.column = self._parse_column_definition()
                if self.accept_keyword("AFTER"):
                    cmd.setting_name = self.parse_identifier()
                elif self.accept_keyword("FIRST"):
                    cmd.setting_name = "__first__"
                return cmd
            if self.accept_keyword("TTL"):
                cmd.kind = "MODIFY_TTL"
                cmd.ttl = self._parse_ttl_definition()
                return cmd
            if self.accept_keyword("SETTING"):
                cmd.kind = "MODIFY_SETTING"
                cmd.setting_name = self.parse_identifier()
                self.expect_punct("=")
                cmd.setting_value = self.parse_expression()
                return cmd
            if self.accept_keyword("ORDER"):
                self.expect_keyword("BY")
                cmd.kind = "MODIFY_ORDER_BY"
                exprs = self._parse_expression_list_parens()
                cmd.expr = exprs[0] if exprs else None
                return cmd
            if self.accept_keyword("SAMPLE"):
                self.expect_keyword("BY")
                cmd.kind = "MODIFY_SAMPLE_BY"
                cmd.expr = self.parse_expression()
                return cmd
            if self.accept_keyword("QUOTA"):
                cmd.kind = "MODIFY_QUOTA"
                return cmd
            self.error("Unsupported ALTER MODIFY form")
        if self.accept_keyword("RENAME"):
            self.expect_keyword("COLUMN")
            cmd.kind = "RENAME_COLUMN"
            cmd.name = self.parse_identifier()
            self.expect_keyword("TO")
            cmd.new_name = self.parse_identifier()
            return cmd
        if self.accept_keyword("COMMENT"):
            self.expect_keyword("COLUMN")
            cmd.kind = "COMMENT_COLUMN"
            cmd.name = self.parse_identifier()
            cmd.setting_value = self._parse_string_literal()
            return cmd
        if self.accept_keyword("CLEAR"):
            self.expect_keyword("COLUMN")
            cmd.kind = "CLEAR_COLUMN"
            cmd.name = self.parse_identifier()
            if self.accept_keyword("IN"):
                self.expect_keyword("PARTITION")
                cmd.partition = self.parse_expression()
            return cmd
        if self.accept_keyword("UPDATE"):
            cmd.kind = "UPDATE"
            while True:
                name = self.parse_identifier()
                self.expect_punct("=")
                cmd.update_assignments.append((name, self.parse_expression()))
                if not self.accept_punct(","):
                    break
            self.expect_keyword("WHERE")
            cmd.predicate = self.parse_expression()
            return cmd
        if self.accept_keyword("DELETE"):
            cmd.kind = "DELETE"
            self.expect_keyword("WHERE")
            cmd.predicate = self.parse_expression()
            return cmd
        if self.accept_keyword("DETACH"):
            if self.accept_keyword("PARTITION"):
                cmd.kind = "DETACH_PARTITION"
                cmd.partition = self.parse_expression()
                return cmd
            if self.accept_keyword("PART"):
                cmd.kind = "DETACH_PART"
                cmd.partition = self.parse_expression()
                return cmd
            self.error("Unsupported ALTER DETACH form")
        if self.accept_keyword("ATTACH"):
            if self.accept_keyword("PARTITION"):
                cmd.kind = "ATTACH_PARTITION"
                cmd.partition = self.parse_expression()
                return cmd
            if self.accept_keyword("PART"):
                cmd.kind = "ATTACH_PART"
                cmd.name = self._parse_string_literal()
                return cmd
            self.error("Unsupported ALTER ATTACH form")
        if self.accept_keyword("MOVE"):
            if self.accept_keyword("PARTITION"):
                cmd.kind = "MOVE_PARTITION"
                cmd.partition = self.parse_expression()
                self.expect_keyword("TO")
                if self.accept_keyword("DISK"):
                    cmd.setting_name = "DISK"
                elif self.accept_keyword("VOLUME"):
                    cmd.setting_name = "VOLUME"
                elif self.accept_keyword("TABLE"):
                    cmd.setting_name = "TABLE"
                else:
                    self.error("MOVE PARTITION TO requires DISK, VOLUME or TABLE")
                cmd.setting_value = self.parse_expression()
                return cmd
            self.error("Unsupported ALTER MOVE form")
        if self.accept_keyword("FREEZE"):
            cmd.kind = "FREEZE"
            if self.accept_keyword("PARTITION"):
                cmd.partition = self.parse_expression()
            return cmd
        if self.accept_keyword("FETCH"):
            cmd.kind = "FETCH"
            self.expect_keyword("PARTITION")
            cmd.partition = self.parse_expression()
            self.expect_keyword("FROM")
            cmd.setting_value = self.parse_expression()
            return cmd
        if self.accept_keyword("RESET"):
            self.expect_keyword("SETTING")
            cmd.kind = "RESET_SETTING"
            cmd.setting_name = self.parse_identifier()
            return cmd
        if self.accept_keyword("REMOVE"):
            self.expect_keyword("TTL")
            cmd.kind = "REMOVE_TTL"
            return cmd
        if self.accept_keyword("START"):
            self.expect_keyword("MERGES")
            cmd.kind = "START_MERGES"
            return cmd
        if self.accept_keyword("STOP"):
            self.expect_keyword("MERGES")
            cmd.kind = "STOP_MERGES"
            return cmd
        self.error(f"Unsupported ALTER command: {self.peek().value!r}")

    def parse_show(self) -> A.ShowQuery:
        pos = self.next().pos  # SHOW
        q = A.ShowQuery(pos=pos)
        q.full = self.accept_keyword("FULL")
        if self.accept_keyword("CREATE"):
            q.kind = "CREATE"
            self.accept_keyword("TABLE")
            self.accept_keyword("DICTIONARY")
            q.database, q.table = self.parse_qualified_name()
            return q
        if self.accept_keyword("TABLES"):
            q.kind = "TABLES"
            if self.accept_keyword("FROM") or self.accept_keyword("IN"):
                q.database = self.parse_identifier()
            self._show_like_where(q)
            return q
        if self.accept_keyword("DATABASES"):
            q.kind = "DATABASES"
            self._show_like_where(q)
            return q
        if self.accept_keyword("COLUMNS"):
            q.kind = "COLUMNS"
            if self.accept_keyword("FROM") or self.accept_keyword("IN"):
                q.database, q.table = self.parse_qualified_name()
            self._show_like_where(q)
            return q
        if self.accept_keyword("DICTIONARIES"):
            q.kind = "DICTIONARIES"
            if self.accept_keyword("FROM") or self.accept_keyword("IN"):
                q.database = self.parse_identifier()
            return q
        if self.accept_keyword("SETTINGS"):
            q.kind = "SETTINGS"
            q.changed = self.accept_keyword("CHANGED")
            self._show_like_where(q)
            return q
        if self.accept_keyword("PROCESSLIST"):
            q.kind = "PROCESSLIST"
            return q
        if self.accept_keyword("CLUSTERS"):
            q.kind = "CLUSTERS"
            return q
        if self.accept_keyword("ENGINES"):
            q.kind = "ENGINES"
            return q
        if self.accept_keyword("FUNCTIONS"):
            q.kind = "FUNCTIONS"
            self._show_like_where(q)
            return q
        if self.accept_keyword("GRANTS"):
            q.kind = "GRANTS"
            # ``SHOW GRANTS [FOR user]`` —— 用户名存进 ``q.name``。
            # (真实 ClickHouse 支持 ``FOR``;解释器这一版默认只报当前用户,
            #  但我们**先把它解析下来**,免得语句直接语法报错。)
            if self.accept_keyword("FOR"):
                q.name = self.parse_identifier("user")
            return q
        if self.accept_keyword("USERS"):
            q.kind = "USERS"
            return q
        if self.accept_keyword("ROLES"):
            q.kind = "ROLES"
            return q
        if self.accept_keyword("QUOTAS"):
            q.kind = "QUOTAS"
            return q
        if self.accept_keyword("ACCESS"):
            q.kind = "ACCESS"
            return q
        if self.accept_keyword("MERGES"):
            q.kind = "MERGES"
            return q
        self.error("Unsupported SHOW form")

    def _show_like_where(self, q: A.ShowQuery) -> None:
        if self.accept_keyword("LIKE"):
            q.like = self._parse_string_literal()
        if self.accept_keyword("ILIKE"):
            q.like = self._parse_string_literal().lower()
        if self.accept_keyword("WHERE"):
            q.where = self.parse_expression()
        if self.accept_keyword("LIMIT"):
            q.limit = self.parse_expression()

    def parse_system(self) -> A.SystemQuery:
        pos = self.next().pos  # SYSTEM
        q = A.SystemQuery(pos=pos)
        words: List[str] = []
        while self.peek().kind == TokenKind.IDENT and not self.peek_punct(";"):
            if self.peek().upper in _SYSTEM_STOP_WORDS and words:
                break
            words.append(self.next().upper)
        q.command = " ".join(words)
        if self.peek_keyword("FROM"):
            self.next()
            q.database, q.table = self.parse_qualified_name()
        elif self.peek().kind == TokenKind.IDENT and q.command.split()[-1:] in (
            ["MERGES"], ["REPLICA"], ["REPLICATED"], ["DICTIONARY"], ["DICTIONARIES"],
            ["TTL"], ["DISTRIBUTED"], ["FETCHES"], ["MOVES"],
        ):
            try:
                q.database, q.table = self.parse_qualified_name()
            except MiniChError:
                pass
        while not self.at_end() and not self.peek_punct(";"):
            q.args.append(self.next().value)
        return q

    def parse_explain(self) -> A.ExplainQuery:
        pos = self.next().pos  # EXPLAIN
        q = A.ExplainQuery(pos=pos)
        kinds = ("AST", "SYNTAX", "PLAN", "PIPELINE", "ESTIMATE", "QUERY", "TABLE",
                 "CURRENT", "PARSE")
        if self.peek().kind == TokenKind.IDENT and self.peek().upper in kinds:
            first = self.next().upper
            if first == "QUERY" and self.accept_keyword("TREE"):
                q.kind = "QUERY TREE"
            elif first == "TABLE":
                self.expect_keyword("OVERRIDE")
                q.kind = "TABLE OVERRIDE"
            elif first == "PARSE":
                self.expect_keyword("QUERY")
                q.kind = "PARSE QUERY"
            else:
                q.kind = first
        # `EXPLAIN PLAN header = 1` 这类选项
        while self.peek().kind == TokenKind.IDENT and self.peek(1).is_punct("="):
            name = self.parse_identifier()
            self.expect_punct("=")
            q.settings.append((name, self.parse_expression()))
            self.accept_punct(",")
        q.statement = self.parse_statement()
        return q

    def parse_set(self) -> A.SetQuery:
        pos = self.next().pos  # SET
        q = A.SetQuery(pos=pos)
        if self.accept_keyword("GLOBAL"):
            q.scope = "GLOBAL"
        elif self.accept_keyword("SESSION"):
            q.scope = "SESSION"
        elif self.accept_keyword("LOCAL"):
            q.scope = "LOCAL"
        while True:
            name = self.parse_identifier("setting name")
            self.expect_punct("=")
            if self.peek_keyword("DEFAULT"):
                self.next()
                q.items.append((name, None))
            else:
                tok = self.peek()
                if tok.kind == TokenKind.NUMBER:
                    self.next()
                    q.items.append((name, tok.value))
                elif tok.kind == TokenKind.STRING:
                    q.items.append((name, self._parse_string_literal()))
                elif tok.kind == TokenKind.IDENT and tok.upper in ("TRUE", "FALSE"):
                    self.next()
                    q.items.append((name, tok.upper == "TRUE"))
                else:
                    q.items.append((name, self.parse_expression()))
            if not self.accept_punct(","):
                break
        return q

    def parse_grant(self, revoke: bool = False) -> A.GrantQuery:
        pos = self.next().pos
        q = A.GrantQuery(pos=pos, revoke=revoke)
        while True:
            tok = self.peek()
            if tok.is_punct(","):
                self.next()
                continue
            if tok.kind == TokenKind.IDENT and tok.upper in ("ON", "TO", "FROM"):
                break
            if tok.kind == TokenKind.PUNCT:
                if tok.value == "(" and q.privileges:
                    # 列级权限:``SELECT(a, b) ON db.t`` —— 把列名并进上一条
                    # 权限,存成 ``"SELECT(a, b)"``,交给 access.py 展开成
                    # 一行一列的授权(与真实 ClickHouse 一致)。
                    self.expect_punct("(")
                    cols: List[str] = []
                    if not self.accept_punct(")"):
                        while True:
                            cols.append(self.parse_identifier("column"))
                            if not self.accept_punct(","):
                                break
                        self.expect_punct(")")
                    q.privileges[-1] = (q.privileges[-1]
                                        + "(" + ", ".join(cols) + ")")
                    continue
                break
            q.privileges.append(self.next().upper)
        if self.accept_keyword("ON"):
            parts: List[str] = []
            while not self.peek_keyword("TO", "FROM"):
                t = self.next()
                parts.append(str(t.value))
            q.on = "".join(parts)
        self.expect_keyword("FROM" if revoke else "TO")
        while True:
            q.to.append(self.parse_identifier("user or role"))
            if not self.accept_punct(","):
                break
        if self.accept_keywords("WITH", "GRANT", "OPTION"):
            q.with_grant_option = True
        return q

    def parse_optimize(self) -> A.OptimizeQuery:
        pos = self.next().pos  # OPTIMIZE
        self.expect_keyword("TABLE")
        q = A.OptimizeQuery(pos=pos)
        q.database, q.table = self.parse_qualified_name()
        if self.accept_keyword("ON"):
            self.expect_keyword("CLUSTER")
            self.parse_identifier()
        if self.accept_keyword("PARTITION"):
            q.partition = self.parse_expression()
        if self.accept_keyword("FINAL"):
            q.final = True
        if self.accept_keyword("DEDUPLICATE"):
            q.deduplicate = True
            if self.accept_keyword("BY"):
                while True:
                    self.parse_expression()
                    if not self.accept_punct(","):
                        break
        if self.accept_keyword("CLEANUP"):
            q.cleanup = True
        return q

    def parse_describe(self) -> A.DescribeQuery:
        pos = self.next().pos
        self.accept_keyword("TABLE")
        q = A.DescribeQuery(pos=pos)
        if self.accept_punct("("):
            q.subquery = self.parse_select()
            self.expect_punct(")")
            return q
        d, t = self.parse_qualified_name()
        if self.peek_punct("("):
            self.expect_punct("(")
            args: List[A.Node] = []
            if not self.accept_punct(")"):
                while True:
                    args.append(self.parse_expression())
                    if not self.accept_punct(","):
                        break
                self.expect_punct(")")
            q.table_function = A.TableFunctionCall(name=t, args=args)
            return q
        q.database, q.table = d, t
        return q

    def parse_kill(self) -> A.KillQuery:
        pos = self.next().pos
        q = A.KillQuery(pos=pos)
        if self.accept_keyword("MUTATION"):
            q.kind = "MUTATION"
        else:
            self.expect_keyword("QUERY")
            q.kind = "QUERY"
        if self.accept_keyword("ON"):
            self.expect_keyword("CLUSTER")
            self.parse_identifier()
        self.expect_keyword("WHERE")
        q.where = self.parse_expression()
        while True:
            if self.accept_keyword("SYNC"):
                q.sync = True
            elif self.accept_keyword("ASYNC"):
                q.sync = False
            elif self.accept_keyword("TEST"):
                q.test = True
            else:
                break
        return q


#: 语句首关键字 → 处理函数
_STATEMENT_DISPATCH: dict = {}


def _register_dispatch() -> None:
    _STATEMENT_DISPATCH.update({
        "SELECT": Parser.parse_select,
        "WITH": Parser.parse_select,
        "(": Parser.parse_select,
        "INSERT": Parser.parse_insert,
        "CREATE": Parser.parse_create,
        "ATTACH": Parser.parse_create,
        "DROP": Parser.parse_drop,
        "DETACH": Parser.parse_drop,
        "ALTER": Parser.parse_alter,
        "RENAME": lambda self: _parse_rename(self),
        "TRUNCATE": lambda self: _parse_truncate(self),
        "OPTIMIZE": Parser.parse_optimize,
        "SHOW": Parser.parse_show,
        "DESCRIBE": Parser.parse_describe,
        "DESC": Parser.parse_describe,
        "EXPLAIN": Parser.parse_explain,
        "SYSTEM": Parser.parse_system,
        "SET": Parser.parse_set,
        "USE": lambda self: _parse_use(self),
        "KILL": Parser.parse_kill,
        "GRANT": lambda self: self.parse_grant(False),
        "REVOKE": lambda self: self.parse_grant(True),
        "EXISTS": lambda self: _parse_exists(self),
        "CHECK": lambda self: _parse_check(self),
        "SELECT": Parser.parse_select,
    })


def _parse_rename(p: Parser) -> A.RenameQuery:
    p.next()  # RENAME
    p.expect_keyword("TABLE")
    q = A.RenameQuery()
    while True:
        d1, t1 = p.parse_qualified_name()
        p.expect_keyword("TO")
        d2, t2 = p.parse_qualified_name()
        q.pairs.append((
            A.TableReference(database=d1, table=t1),
            A.TableReference(database=d2, table=t2),
        ))
        if not p.accept_punct(","):
            break
    return q


def _parse_truncate(p: Parser) -> A.TruncateQuery:
    p.next()  # TRUNCATE
    p.accept_keyword("TEMPORARY")
    p.expect_keyword("TABLE")
    q = A.TruncateQuery()
    q.if_exists = p._parse_if_exists()
    q.database, q.table = p.parse_qualified_name()
    if p.accept_keyword("ON"):
        p.expect_keyword("CLUSTER")
        p.parse_identifier()
    return q


def _parse_use(p: Parser) -> A.UseQuery:
    p.next()  # USE
    return A.UseQuery(database=p.parse_identifier("database name"))


def _parse_exists(p: Parser) -> A.ExistsQuery:
    p.next()  # EXISTS
    p.accept_keyword("TEMPORARY")
    p.expect_keyword("TABLE")
    q = A.ExistsQuery()
    q.database, q.table = p.parse_qualified_name()
    return q


def _parse_check(p: Parser) -> A.DescribeQuery:
    p.next()  # CHECK
    p.expect_keyword("TABLE")
    q = A.DescribeQuery()
    q.database, q.table = p.parse_qualified_name()
    return q


_register_dispatch()


#: 出现在表达式之后、表示"表达式结束"的关键字 —— 隐式别名的判定要用
_CLAUSE_KEYWORDS = frozenset({
    "FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "SETTINGS",
    "FORMAT", "UNION", "INTO", "PREWHERE", "ARRAY", "WINDOW", "AS", "ON",
    "USING", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "GLOBAL",
    "ANY", "ALL", "ASOF", "SEMI", "ANTI", "PASTE", "FINAL", "SAMPLE",
    "ASC", "DESC", "NULLS", "BY", "WITH", "TOTALS", "ROLLUP", "CUBE",
    "AND", "OR", "NOT", "IN", "BETWEEN", "IS", "LIKE", "ILIKE", "REGEXP",
    "THEN", "ELSE", "END", "WHEN", "ELSEIF", "INTERVAL", "OUTFILE", "TO",
})

_COMPARISON_OPS = frozenset({"=", "==", "!=", "<>", "<", ">", "<=", ">=", "<=>"})

#: 类型名在顶层遇到这些词就结束(它们是子句关键字)
_TYPE_TERMINATORS = frozenset({
    "DEFAULT", "MATERIALIZED", "ALIAS", "EPHEMERAL", "COMMENT", "CODEC", "TTL",
    "NOT", "NULL", "PRIMARY", "KEY", "ORDER", "BY", "PARTITION", "SAMPLE",
    "SETTINGS", "ENGINE", "AS", "SELECT", "WHERE", "GROUP", "HAVING", "LIMIT",
    "IF", "AFTER", "FIRST", "TO", "TYPE", "GRANULARITY", "CHECK", "CONSTRAINT",
    "INDEX", "PROJECTION", "AUTO_INCREMENT", "ON", "FROM", "AND", "OR", "ASC",
    "DESC", "IN", "IS", "LIKE", "BETWEEN", "USING", "JOIN", "INNER", "LEFT",
    "RIGHT", "FULL", "CROSS", "VALUES", "WHEN", "THEN", "ELSE", "END", "POPULATE",
})

_SYSTEM_STOP_WORDS = frozenset({"WHERE", "LIMIT", "SETTINGS", "FORMAT"})


def _token_width(sql: str, tok: Token) -> int:
    """一个 token 在原文里占的字符宽度(切类型名时要用)。"""
    if tok.kind == TokenKind.STRING:
        # 原始形式带引号,可能比解码后的值长 → 从原文里量
        if tok.pos < len(sql) and sql[tok.pos] in "'\"":
            quote = sql[tok.pos]
            j = tok.pos + 1
            while j < len(sql):
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == quote:
                    return j - tok.pos + 1
                j += 1
        return len(str(tok.value)) + 2
    if tok.kind == TokenKind.IDENT and tok.quoted:
        return len(tok.value) + 2
    if tok.kind == TokenKind.PARAM:
        return len(tok.value[0]) + len(tok.value[1]) + 3
    return len(str(tok.value))


def pos_of(node: A.Node) -> int:
    return getattr(node, "pos", -1)


# ══════════════════════════════════════════════════════════════════════════
# 对外接口
# ══════════════════════════════════════════════════════════════════════════


def parse(sql: str) -> A.Node:
    """把一条 SQL 解析成 AST。

    .. code-block:: python

       >>> parse("SELECT a + 1 FROM t WHERE b > 2").to_sql()
       "SELECT a + 1 FROM t WHERE b > 2"

    ``parse`` 之后会检查"是否还有剩余 token" —— 这样
    ``SELECT 1 garbage`` 之类的输入会**报错**而不是被静默忽略,
    这是真实 ClickHouse 的行为(它报 ``SYNTAX_ERROR``)。
    """
    parser = Parser(sql)
    node = parser.parse_statement()
    if not parser.at_end():
        # ``INSERT INTO t FORMAT CSV`` **后面跟着的是数据,不是 SQL** ——
        # 它已经被 ``InterpreterInsertQuery`` 当成 payload 取走了
        # (``InsertQuery.data``),所以这里不能报"多余的 token"。
        # 这正是手写递归下降相对 yacc 的一个好处:解析器可以在
        # 中途**停下来**并把剩下的原始文本交出去,而不用把数据也
        # 塞进语法规则里。真实 ClickHouse 也是这么做的。
        if isinstance(node, A.InsertQuery) and node.format is not None:
            return node
        tok = parser.peek()
        parser.error(f"Unexpected trailing token {tok.value!r} after the statement")
    return node


def parse_expression_text(text: str) -> A.Node:
    """只解析一个表达式(不要求是完整语句)。

    用在 ``SETTINGS`` 的值、``TTL`` 表达式这些"嵌在别处的小语法"上,
    以及测试里。
    """
    parser = Parser(text)
    node = parser.parse_expression()
    if not parser.at_end():
        tok = parser.peek()
        parser.error(f"Unexpected token {tok.value!r} after the expression")
    return node
