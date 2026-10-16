"""SQL 前端(词法 → 语法 → AST)的测试。

这一层是 mini-clickhouse 与真实 ClickHouse 最"一一对应"的地方:
``minich/sql/lexer.py`` ↔ ``src/Parsers/Lexer.cpp``、
``parser.py`` ↔ ``src/Parsers/Parser*.cpp``、
``ast_.py`` ↔ ``src/Parsers/AST*.h``。
所以这里的测试也刻意照抄真实 ClickHouse 的三条"规矩":

1. **能解析的 SQL 必须能回打成 SQL,而且回打是"文本不动点"。**
   每个 AST 节点都有 ``to_sql()``(真实 ClickHouse 的 ``formatImpl()``)。
   但回打**不等于**逐字还原原文 —— 解析会把语法糖展开(``a::T`` → ``CAST(a AS T)``)、
   把默认值补全(``ORDER BY a`` → ``ORDER BY a ASC``)、给表名加反引号。
   所以正确的断言是"不动点":``p1 = parse(sql).to_sql()`` 之后再解析一次,
   ``parse(p1).to_sql()`` 必须还是 ``p1``。**不要**写 ``to_sql() == sql``。

2. **错误码是一等公民。** 失败一律是 ``MiniChError``,靠 ``e.code`` 区分,
   不靠异常类型(真实 ClickHouse 也只有一个 ``DB::Exception``)。
   ``str(e)`` 的格式是 ``Code: 61. DB::Exception: … (SYNTAX_ERROR)``。

3. **位置信息在消息文本里,不在属性上。** 没有 ``e.pos``/``e.position``,
   位置写成 ``at position <N>, line <L>``。所以只能正则匹配。

.. note::

   文件末尾两节是"缺陷记录":
   * ``# 6. 回归`` —— 写作期间被独立审计挖出来、**已修**的 11 条语法缺陷,
     留作正向回归;
   * ``# 7. 仍然坏着`` —— 还没修的,用 ``xfail()`` 钉死在案。
"""

from __future__ import annotations

import re
import sys

import pytest

from minich.errors import ErrorCode, MiniChError, error_code
from minich.sql import (
    KEYWORDS,
    Parser,
    Token,
    TokenKind,
    format_ast,
    parse,
    parse_expression_text,
    tokenize,
)
from minich.sql import ast_ as A

# ══════════════════════════════════════════════════════════════════════════
# 常量与辅助
# ══════════════════════════════════════════════════════════════════════════

SYNTAX_ERROR = ErrorCode.SYNTAX_ERROR                            # 61
EMPTY_QUERY = ErrorCode.EMPTY_QUERY                              # 62
CANNOT_PARSE_QUOTED_STRING = ErrorCode.CANNOT_PARSE_QUOTED_STRING  # 28
#: 同样是"错误表里有、``ErrorCode`` 类里没有"的例子(29)。
CANNOT_PARSE_ESCAPE_SEQUENCE = error_code("CANNOT_PARSE_ESCAPE_SEQUENCE")  # 29

#: 真实 ClickHouse 的 QUERY_IS_TOO_LARGE(124)。曾经 ``ErrorCode`` 类里**没有**
#: 这个属性(只有错误表 ``_CODES`` 里有),深度守卫因此抛 AttributeError —— 已修。
QUERY_IS_TOO_LARGE = ErrorCode.QUERY_IS_TOO_LARGE

#: 报错消息里位置信息的形态:``… (at position 12, line 1: …)``
_POSITION_RE = re.compile(r"at position \d+, line \d+")


def fixpoint(sql: str):
    """把一条 SQL 回打成文本,再解析一次并回打 —— 返回 ``(p1, p2)``。

    教学点:测试**永远**比较 ``p1 == p2``(不动点),而不是 ``p1 == sql``。
    """
    p1 = parse(sql).to_sql()
    p2 = parse(p1).to_sql()
    return p1, p2


def _sid(sql: str, limit: int = 44) -> str:
    """把 SQL 变成安全的 pytest 用例 id(只留字母数字/下划线)。"""
    s = re.sub(r"[^0-9A-Za-z]+", "_", sql).strip("_")
    return (s[:limit] or "empty")


# ══════════════════════════════════════════════════════════════════════════
# 1. 回打不动点语料库 —— 按特性分组,每条都必须是"无损"的
# ══════════════════════════════════════════════════════════════════════════
#
# 这一节是本套件的主体:每一条语句都必须
#   ① 解析不报错 ② parse(parse(sql).to_sql()).to_sql() == parse(sql).to_sql()
# 有损的语法(见第 5 节)刻意**不**放进来。

CORPUS: dict[str, list[str]] = {
    # ── 字面量与一元/二元运算 ─────────────────────────────────────────
    "literal": [
        "SELECT 1",
        "SELECT 1.5",
        "SELECT -1",
        "SELECT .5",
        "SELECT 0x1f",
        "SELECT 'a'",
        "SELECT 'it\\'s'",
        "SELECT true, false, NULL",
        "SELECT -1.5",
        "SELECT 1 + 2 * 3 - 4 / 5 % 2",
        "SELECT a || b",
        "SELECT ~a",
        "SELECT NOT a",
        "SELECT 1 = 1, 2 != 3, 4 <> 5, 6 >= 7, 8 <= 9, 10 == 11",
        "SELECT a IS NULL",
        "SELECT a IS NOT NULL",
    ],
    # ── 谓词:IN / BETWEEN / LIKE / CASE ─────────────────────────────
    "predicate": [
        "SELECT a IN (1, 2, 3)",
        "SELECT a NOT IN (1, 2)",
        "SELECT a IN (SELECT x FROM t)",
        "SELECT (a, b) IN ((1, 2), (3, 4))",
        "SELECT a BETWEEN 1 AND 10",
        "SELECT a NOT BETWEEN 1 AND 10",
        "SELECT a LIKE '%x%'",
        "SELECT a NOT LIKE '%x%'",
        "SELECT a ILIKE '%x%'",
        "SELECT a REGEXP '^x'",
        "SELECT a NOT REGEXP '^x'",
        "SELECT CASE WHEN a > 1 THEN 'x' ELSE 'y' END",
        "SELECT CASE a WHEN 1 THEN 'x' WHEN 2 THEN 'y' END",
        "SELECT CASE WHEN a THEN 1 END",
    ],
    # ── 类型转换 / 复合字面量 / lambda ──────────────────────────────
    "expression": [
        "SELECT CAST(a AS UInt32)",
        "SELECT CAST(a, 'UInt32')",
        "SELECT a::UInt32",
        "SELECT [1, 2, 3]",
        "SELECT []",
        "SELECT (1, 2)",
        "SELECT {}",
        "SELECT {'a': 1, 'b': 2}",
        "SELECT x -> x + 1",
        "SELECT (x, y) -> x + y",
        "SELECT arrayMap(x -> x * 2, arr)",
        "SELECT quantile(0.9)(x)",
        "SELECT count()",
        "SELECT count(*) FROM t",
        "SELECT count(DISTINCT x) FROM t",
        "SELECT n.f1 FROM t",
        "SELECT t.a.b.c",
        "SELECT x.1",
        "SELECT [1,2][1]",
        "SELECT arr[1]",
        "SELECT m['k']",
        "SELECT (a).5",
        "SELECT {p:UInt32}",
        "SELECT * FROM t WHERE a = {p:String}",
        "SELECT now() + INTERVAL 1 HOUR",
    ],
    # ── INTERVAL / 日期时间语法糖 ────────────────────────────────────
    "interval": [
        "SELECT INTERVAL 1 DAY",
        "SELECT INTERVAL 3 MONTHS",
        "SELECT EXTRACT(DAY FROM d)",
        "SELECT EXTRACT(YEAR FROM d)",
        "SELECT SUBSTRING(s FROM 1 FOR 2)",
        "SELECT substring(s, 1, 2)",
        "SELECT TRIM(BOTH 'x' FROM s)",
        "SELECT TRIM(LEADING 'x' FROM s)",
        "SELECT TRIM(TRAILING 'x' FROM s)",
        "SELECT TRIM(s)",
        "SELECT EXISTS (SELECT 1 FROM t)",
    ],
    # ── SELECT 列表与 DISTINCT / * ──────────────────────────────────
    "select_list": [
        "SELECT DISTINCT a FROM t",
        "SELECT * FROM t",
        "SELECT t.* FROM t",
        "SELECT * EXCEPT (a, b) FROM t",
        "SELECT * REPLACE (a + 1 AS a) FROM t",
        "SELECT a AS b FROM t",
        "SELECT a b FROM t",
        "SELECT 1 FORMAT JSON",
    ],
    # ── FROM:表 / 库 / FINAL / SAMPLE / 表函数 / 子查询 ─────────────
    "from": [
        "SELECT * FROM db.t",
        "SELECT * FROM t FINAL",
        "SELECT * FROM t SAMPLE 0.1",
        "SELECT * FROM numbers(10)",
        "SELECT * FROM (SELECT 1) AS x",
        "SELECT * FROM (SELECT 1) x",
        "SELECT * FROM (SELECT 1)",
        "SELECT * FROM t AS x",
        "SELECT (SELECT 1)",
    ],
    # ── JOIN:方向(kind)与严格性(strictness)是两个正交维度 ─────────
    "join": [
        "SELECT * FROM a JOIN b ON a.x = b.x",
        "SELECT * FROM a INNER JOIN b ON a.x = b.x",
        "SELECT * FROM a LEFT JOIN b ON a.x = b.x",
        "SELECT * FROM a LEFT OUTER JOIN b ON a.x = b.x",
        "SELECT * FROM a RIGHT JOIN b ON a.x = b.x",
        "SELECT * FROM a FULL OUTER JOIN b ON a.x = b.x",
        "SELECT * FROM a CROSS JOIN b",
        "SELECT * FROM a, b",
        "SELECT * FROM a JOIN b USING (x, y)",
        "SELECT * FROM a ANY LEFT JOIN b ON a.x = b.x",
        "SELECT * FROM a LEFT ANY JOIN b ON a.x = b.x",
        "SELECT * FROM a PASTE JOIN b",
        "SELECT * FROM a ASOF JOIN b ON a.x = b.x",
        "SELECT * FROM a SEMI JOIN b ON a.x = b.x",
        "SELECT * FROM a ANTI JOIN b ON a.x = b.x",
        "SELECT * FROM a GLOBAL JOIN b ON a.x = b.x",
        "SELECT * FROM a JOIN b ON a.x = b.x JOIN c ON b.y = c.y",
    ],
    # ── 过滤 / 分组 / 聚合修饰符 ────────────────────────────────────
    "filter_group": [
        "SELECT * FROM t PREWHERE a > 1 WHERE b < 2",
        "SELECT * FROM t GROUP BY a",
        "SELECT * FROM t GROUP BY a WITH ROLLUP",
        "SELECT * FROM t GROUP BY a WITH CUBE",
        "SELECT * FROM t GROUP BY a WITH TOTALS",
        "SELECT * FROM t GROUP BY a WITH ROLLUP WITH TOTALS",
        "SELECT * FROM t GROUP BY GROUPING SETS ((a), (b))",
        "SELECT * FROM t GROUP BY GROUPING SETS ((a, b), ())",
        "SELECT * FROM t GROUP BY ALL",
        "SELECT * FROM t GROUP BY a HAVING count() > 1",
    ],
    # ── 窗口函数 ────────────────────────────────────────────────────
    "window": [
        "SELECT count() OVER () FROM t",
        "SELECT row_number() OVER (PARTITION BY a ORDER BY b) FROM t",
        "SELECT row_number() OVER (ORDER BY b ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) FROM t",
        "SELECT sum(x) OVER (ORDER BY k RANGE UNBOUNDED PRECEDING) FROM t",
        "SELECT sum(x) OVER (PARTITION BY a, b ORDER BY c DESC) FROM t",
        "SELECT * FROM t WINDOW w AS (PARTITION BY a)",
    ],
    # ── ORDER BY / LIMIT ────────────────────────────────────────────
    "order_limit": [
        "SELECT * FROM t ORDER BY a",
        "SELECT * FROM t ORDER BY a DESC, b ASC",
        "SELECT * FROM t ORDER BY a DESC NULLS FIRST",
        "SELECT * FROM t ORDER BY a NULLS LAST",
        "SELECT * FROM t ORDER BY a COLLATE 'tr'",
        "SELECT * FROM t LIMIT 10",
        "SELECT * FROM t LIMIT 10 OFFSET 5",
        "SELECT * FROM t LIMIT 3 BY a",
        "SELECT * FROM t LIMIT 3 OFFSET 1 BY a",
        "SELECT * FROM t SETTINGS max_threads = 4",
    ],
    # ── UNION / WITH / ARRAY JOIN / INTO OUTFILE ────────────────────
    "compound": [
        "SELECT 1 UNION ALL SELECT 2",
        "SELECT 1 UNION DISTINCT SELECT 2",
        "SELECT 1 UNION SELECT 2",
        "SELECT 1 FROM a UNION ALL SELECT 2 FROM b",
        "WITH 1 AS x SELECT x",
        "WITH (SELECT max(x) FROM t) AS m SELECT m",
        "WITH x AS (SELECT 1 AS v) SELECT v FROM x",
        "SELECT * FROM t ARRAY JOIN arr",
        "SELECT * FROM t ARRAY JOIN arr AS a",
        "SELECT * FROM t LEFT ARRAY JOIN arr",
        "SELECT * FROM t ARRAY LEFT JOIN arr",
    ],
    # ── INSERT ──────────────────────────────────────────────────────
    "insert": [
        "INSERT INTO t VALUES (1, 2)",
        "INSERT INTO t (a, b) VALUES (1, 2), (3, 4)",
        "INSERT INTO db.t VALUES (1)",
        "INSERT INTO TABLE t VALUES (1)",
        "INSERT INTO t SELECT * FROM u",
        "INSERT INTO t FORMAT CSV",
        "INSERT INTO FUNCTION file('x.csv') VALUES (1)",
    ],
    # ── CREATE TABLE:列定义与存储子句 ───────────────────────────────
    "create_table": [
        "CREATE TABLE t (a UInt32, b String) ENGINE = Memory",
        "CREATE TABLE IF NOT EXISTS db.t (a UInt32) ENGINE = MergeTree ORDER BY a",
        "CREATE TABLE t (a UInt32 DEFAULT 1) ENGINE = Memory",
        "CREATE TABLE t (b String MATERIALIZED a, a String) ENGINE = Memory",
        "CREATE TABLE t (c String ALIAS a, a String) ENGINE = Memory",
        "CREATE TABLE t (d UInt32 EPHEMERAL 1) ENGINE = Memory",
        "CREATE TABLE t (a UInt32 NOT NULL) ENGINE = Memory",
        "CREATE TABLE t (b String COMMENT 'hi') ENGINE = Memory",
        "CREATE TABLE t (c UInt32 CODEC(ZSTD)) ENGINE = Memory",
        "CREATE TABLE t (d DateTime TTL d + INTERVAL 1 DAY) ENGINE = Memory",
        "CREATE TABLE t (a UInt32, b UInt32, PRIMARY KEY (a)) ENGINE = MergeTree",
        "CREATE TABLE t (a Nullable(UInt8), b Array(String), c Tuple(x UInt8, y String), "
        "d Decimal(10, 2), e Enum8('a' = 1, 'b' = 2)) ENGINE = Memory",
        "CREATE TABLE t (a UInt32, INDEX i a TYPE minmax GRANULARITY 1) ENGINE = MergeTree ORDER BY a",
        "CREATE TABLE t (a UInt32, CONSTRAINT c CHECK a > 0) ENGINE = Memory",
        "CREATE TABLE t (a UInt32, PROJECTION p (SELECT a, sum(a) GROUP BY a)) ENGINE = MergeTree ORDER BY a",
        "CREATE TABLE t (a UInt32) ENGINE = MergeTree PARTITION BY a ORDER BY a PRIMARY KEY a "
        "SAMPLE BY a TTL a + INTERVAL 1 DAY SETTINGS index_granularity = 8192",
        "CREATE TABLE t ENGINE = Memory AS SELECT 1",
        "CREATE TABLE t AS other",
        "CREATE TABLE t AS db.other",
        "CREATE TEMPORARY TABLE t (a UInt32)",
        "CREATE OR REPLACE TABLE t (a UInt32) ENGINE = Memory",
        "CREATE TABLE t ON CLUSTER c (a UInt32) ENGINE = Memory",
        "CREATE TABLE t UUID '123e4567-e89b-12d3-a456-426614174000' (a UInt32) ENGINE = Memory",
        "CREATE TABLE t (a UInt32) ENGINE = Distributed('c', 'd', 't')",
    ],
    # ── CREATE 其它对象 ─────────────────────────────────────────────
    "create_other": [
        "CREATE DATABASE db",
        "CREATE DATABASE IF NOT EXISTS db ENGINE = Atomic",
        "CREATE VIEW v AS SELECT 1",
        "CREATE MATERIALIZED VIEW mv ENGINE = Memory AS SELECT 1",
        "CREATE MATERIALIZED VIEW mv TO t AS SELECT 1",
        "CREATE FUNCTION f AS (x) -> x * 2",
        "CREATE USER u",
        "CREATE ROLE r",
        "CREATE USER IF NOT EXISTS u IDENTIFIED BY 'p'",
        "CREATE DICTIONARY d (id UInt64, name String DEFAULT 'x') PRIMARY KEY id "
        "SOURCE(CLICKHOUSE(HOST 'x' PORT 9000)) LAYOUT(FLAT) LIFETIME(300)",
    ],
    # ── DROP ────────────────────────────────────────────────────────
    "drop": [
        "DROP TABLE t",
        "DROP TABLE IF EXISTS t",
        "DROP TABLE db.t",
        "DROP DATABASE db",
        "DROP DICTIONARY d",
        "DROP VIEW v",
        "DROP FUNCTION f",
        "DROP USER u",
        "DROP ROLE r",
        "DROP TABLE t SYNC",
        "DROP TABLE a, b",
        "DROP TEMPORARY TABLE t",
    ],
    # ── ALTER TABLE:能无损回打的那些子命令 ──────────────────────────
    "alter": [
        "ALTER TABLE t ADD COLUMN a UInt32",
        "ALTER TABLE t ADD COLUMN IF NOT EXISTS a UInt32 AFTER b",
        "ALTER TABLE t DROP COLUMN a",
        "ALTER TABLE t DROP COLUMN IF EXISTS a",
        "ALTER TABLE t MODIFY COLUMN a String",
        "ALTER TABLE t RENAME COLUMN a TO b",
        "ALTER TABLE t COMMENT COLUMN a 'hi'",
        "ALTER TABLE t UPDATE a = 1 WHERE b > 0",
        "ALTER TABLE t DELETE WHERE a > 0",
        "ALTER TABLE t DROP PARTITION 'x'",
        "ALTER TABLE t DROP PART 'x'",
        "ALTER TABLE t DETACH PARTITION 'x'",
        "ALTER TABLE t ATTACH PARTITION 'x'",
        "ALTER TABLE t ADD INDEX i a TYPE minmax GRANULARITY 1",
        "ALTER TABLE t DROP INDEX i",
        "ALTER TABLE t MODIFY TTL a + INTERVAL 1 DAY",
        "ALTER TABLE t MODIFY SETTING index_granularity = 4096",
        "ALTER TABLE t RESET SETTING index_granularity",
        "ALTER TABLE t MODIFY ORDER BY (a)",
        "ALTER TABLE t FREEZE",
        "ALTER TABLE t FREEZE PARTITION 'x'",
        "ALTER TABLE t CLEAR COLUMN a IN PARTITION 'x'",
        "ALTER TABLE t ADD COLUMN a UInt32, DROP COLUMN b",
        "ALTER TABLE IF EXISTS t ADD COLUMN a UInt32",
    ],
    # ── RENAME / TRUNCATE / OPTIMIZE ────────────────────────────────
    "ddl_misc": [
        "RENAME TABLE a TO b",
        "RENAME TABLE a TO b, c TO d",
        "TRUNCATE TABLE t",
        "TRUNCATE TABLE IF EXISTS t",
        "OPTIMIZE TABLE t",
        "OPTIMIZE TABLE t PARTITION 'x' FINAL",
        "OPTIMIZE TABLE t DEDUPLICATE",
        "OPTIMIZE TABLE t FINAL DEDUPLICATE BY a, b",
    ],
    # ── SHOW / DESCRIBE / EXPLAIN ───────────────────────────────────
    "introspection": [
        "SHOW TABLES",
        "SHOW TABLES FROM db",
        "SHOW TABLES LIKE 'x%'",
        "SHOW TABLES WHERE name = 'x'",
        "SHOW DATABASES",
        "SHOW CREATE TABLE t",
        "SHOW DICTIONARIES",
        "SHOW SETTINGS",
        "SHOW PROCESSLIST",
        "SHOW CLUSTERS",
        "SHOW ENGINES",
        "SHOW FUNCTIONS",
        "SHOW GRANTS",
        "SHOW GRANTS FOR u",
        "SHOW USERS",
        "SHOW ROLES",
        "SHOW FULL TABLES",
        "DESCRIBE TABLE t",
        "DESC t",
        "DESCRIBE TABLE db.t",
        "DESCRIBE ( SELECT 1 )",
        "DESCRIBE TABLE numbers(10)",
        "EXPLAIN AST SELECT 1",
        "EXPLAIN SYNTAX SELECT 1",
        "EXPLAIN PLAN SELECT 1",
        "EXPLAIN PIPELINE SELECT 1",
        "EXPLAIN ESTIMATE SELECT 1",
        "EXPLAIN PLAN header = 1 SELECT 1",
    ],
    # ── 会话 / 系统 / 权限 ──────────────────────────────────────────
    "session": [
        "SET a = 1",
        "SET a = 1, b = 'x'",
        "SET GLOBAL a = 1",
        "SET a = DEFAULT",
        "SET a = true",
        "USE db",
        "SYSTEM FLUSH LOGS",
        "SYSTEM RELOAD DICTIONARIES",
        "SYSTEM STOP MERGES t",
        "KILL QUERY WHERE query_id = 'x'",
        "KILL MUTATION WHERE mutation_id = 'x' SYNC",
        "EXISTS TABLE t",
        "EXISTS TABLE db.t",
        "CHECK TABLE t",
        "GRANT SELECT ON db.* TO u",
        "GRANT SELECT, INSERT ON db.t TO u WITH GRANT OPTION",
        "REVOKE SELECT ON db.t FROM u",
    ],
    # ── 注释 / 空白 / 引号标识符 ────────────────────────────────────
    "lexical_noise": [
        "SELECT 1 -- trailing comment",
        "SELECT /* inline */ 1",
        "SELECT 1; ",
        "-- leading\nSELECT 1",
        "SELECT 1 AS `select`",
        "SELECT `a b` FROM `t t`",
    ],
}


def _corpus_params() -> list:
    out = []
    for feature, stmts in CORPUS.items():
        for sql in stmts:
            out.append(pytest.param(sql, id=f"{feature}-{_sid(sql)}"))
    return out


CORPUS_STATEMENTS = [sql for stmts in CORPUS.values() for sql in stmts]


def test_corpus_is_big_enough():
    """语料库得够大 —— 单条语句的覆盖率假象是解析器测试最常见的坑。"""
    assert len(CORPUS_STATEMENTS) >= 60, len(CORPUS_STATEMENTS)
    assert len(set(CORPUS_STATEMENTS)) == len(CORPUS_STATEMENTS), "语料里有重复语句"


@pytest.mark.parametrize("sql", _corpus_params())
def test_corpus_roundtrip_fixpoint(sql: str):
    """大语料:解析成功 + ``to_sql`` 是文本不动点。"""
    p1 = parse(sql).to_sql()
    assert isinstance(p1, str)
    p2 = parse(p1).to_sql()
    assert p1 == p2, f"回打不是不动点:\n  sql = {sql!r}\n  p1  = {p1!r}\n  p2  = {p2!r}"


@pytest.mark.parametrize("sql", _corpus_params())
def test_corpus_children_are_nodes(sql: str):
    """每个 AST 节点的 ``children()`` 必须只返回 ``Node``(遍历/改写的前提)。"""
    node = parse(sql)

    def walk(n: A.Node) -> int:
        for c in n.children():
            assert isinstance(c, A.Node), f"{type(n).__name__}.children() 返回了 {c!r}"
            walk(c)
        return 1

    assert walk(node) >= 1


# ══════════════════════════════════════════════════════════════════════════
# 2. AST 字段断言(抽样)—— 光"能解析"不够,还得树长得对
# ══════════════════════════════════════════════════════════════════════════


def _first_select(sql: str) -> A.Node:
    q = parse(sql)
    assert isinstance(q, A.SelectQuery)
    return q.selects[0]


def test_literal_nodes():
    assert isinstance(_first_select("SELECT 1"), A.Literal)
    assert _first_select("SELECT 1").value == 1
    assert _first_select("SELECT 1.5").value == 1.5
    assert _first_select("SELECT -1").value == -1          # 负号被折叠进字面量
    assert _first_select("SELECT .5").value == 0.5
    assert _first_select("SELECT 0x1f").value == 31
    assert _first_select("SELECT 'a'").value == "a"
    assert _first_select("SELECT 'a'").type_name == "String"
    assert _first_select("SELECT true").value is True
    assert _first_select("SELECT false").value is False
    assert _first_select("SELECT NULL").value is None


def test_identifier_and_binary_op_nodes():
    ident = _first_select("SELECT a FROM t")
    assert isinstance(ident, A.Identifier)
    assert ident.name == "a"

    plus = _first_select("SELECT a + 1")
    assert isinstance(plus, A.BinaryOp)
    assert plus.op == "+"
    assert isinstance(plus.left, A.Identifier)
    assert isinstance(plus.right, A.Literal)

    conj = _first_select("SELECT a AND b OR c")
    assert isinstance(conj, A.BinaryOp) and conj.op == "OR"     # OR 优先级最低

    notop = _first_select("SELECT NOT a")
    assert isinstance(notop, A.UnaryOp) and notop.op == "NOT"


def test_alias_and_star_nodes():
    alias = _first_select("SELECT a AS b")
    assert isinstance(alias, A.AliasExpr) and alias.alias == "b"

    star = _first_select("SELECT * FROM t")
    assert isinstance(star, A.StarExpr)

    qualified = _first_select("SELECT t.* FROM t")
    assert isinstance(qualified, A.StarExpr) and qualified.qualified == "t"

    except_star = _first_select("SELECT * EXCEPT (a, b) FROM t")
    assert except_star.except_columns == ["a", "b"]

    replace_star = _first_select("SELECT * REPLACE (a + 1 AS a) FROM t")
    assert replace_star.replace_columns[0][1] == "a"
    assert isinstance(replace_star.replace_columns[0][0], A.BinaryOp)


def test_case_and_cast_nodes():
    case = _first_select("SELECT CASE a WHEN 1 THEN 'x' ELSE 'y' END")
    assert isinstance(case, A.CaseExpr)
    assert isinstance(case.operand, A.Identifier)
    assert len(case.whens) == 1
    assert isinstance(case.else_expr, A.Literal)

    searched = _first_select("SELECT CASE WHEN 1 THEN 2 END")
    assert searched.operand is None
    assert searched.else_expr is None

    cast = _first_select("SELECT CAST(a AS UInt32)")
    assert isinstance(cast, A.CastExpr) and cast.type_name == "UInt32"
    # ``a::UInt32`` 是同一棵树的语法糖
    assert _first_select("SELECT a::UInt32").to_sql() == cast.to_sql()


def test_composite_literal_nodes():
    arr = _first_select("SELECT [1, 2, 3]")
    assert isinstance(arr, A.ArrayLiteral) and len(arr.items) == 3

    tup = _first_select("SELECT (1, 2)")
    assert isinstance(tup, A.TupleLiteral) and len(tup.items) == 2
    assert isinstance(_first_select("SELECT (1)"), A.Literal), "单元素括号不是元组"

    # ``{'a': 1}`` 一度被查询参数 ``{name:Type}`` 的词法抢先吃掉,现已修好(见第 6 节);
    # 这里既测语法,也测节点本身。
    mp = _first_select("SELECT {'a': 1, 'b': 2}")
    assert isinstance(mp, A.MapLiteral) and len(mp.pairs) == 2
    built = A.MapLiteral(pairs=[(A.Literal(value="a"), A.Literal(value=1))])
    assert built.to_sql() == "{'a': 1}"
    assert len(built.children()) == 2

    lam = _first_select("SELECT x -> x + 1")
    assert isinstance(lam, A.Lambda)
    assert lam.params == ["x"]
    assert isinstance(lam.body, A.BinaryOp)

    lam2 = _first_select("SELECT (x, y) -> x + y")
    assert lam2.params == ["x", "y"]


def test_index_in_between_like_isnull_nodes():
    idx = _first_select("SELECT arr[1]")
    assert isinstance(idx, A.IndexExpr)
    assert isinstance(idx.index, A.Literal) and idx.index.value == 1

    elem = _first_select("SELECT x.1")
    assert isinstance(elem, A.IndexExpr) and elem.index.value == 1
    assert isinstance(elem.base, A.Identifier)

    inx = _first_select("SELECT a IN (1, 2)")
    assert isinstance(inx, A.InExpr) and inx.negated is False
    assert isinstance(inx.right, A.TupleLiteral)

    nin = _first_select("SELECT a NOT IN (1, 2)")
    assert nin.negated is True

    bw = _first_select("SELECT a BETWEEN 1 AND 10")
    assert isinstance(bw, A.BetweenExpr)
    assert isinstance(bw.low, A.Literal) and isinstance(bw.high, A.Literal)

    nbw = _first_select("SELECT a NOT BETWEEN 1 AND 10")
    assert nbw.negated is True

    ilike = _first_select("SELECT a ILIKE '%x%'")
    assert isinstance(ilike, A.LikeExpr) and ilike.case_insensitive is True
    assert ilike.is_regexp is False

    regexp = _first_select("SELECT a NOT REGEXP '^x'")
    assert regexp.is_regexp is True and regexp.negated is True

    is_null = _first_select("SELECT a IS NOT NULL")
    assert isinstance(is_null, A.IsNullExpr) and is_null.negated is True


def test_function_call_params_vs_args():
    """``quantile(0.9)(x)`` —— 参数(params)与实参(args)是两回事。"""
    call = _first_select("SELECT quantile(0.9)(x)")
    assert isinstance(call, A.FunctionCall)
    assert call.name == "quantile"
    assert [p.value for p in call.params] == [0.9]
    assert [a.name for a in call.args] == ["x"]
    assert len(call.children()) == 2


def test_interval_and_desugared_function_names():
    iv = _first_select("SELECT INTERVAL 1 DAY")
    assert isinstance(iv, A.IntervalExpr)
    assert iv.kind == "DAY" and iv.amount.value == 1
    # 复数单位被规范化成单数
    assert _first_select("SELECT INTERVAL 3 MONTHS").kind == "MONTH"

    # EXTRACT / SUBSTRING / TRIM / EXISTS 都在解析期降级成普通函数
    assert _first_select("SELECT EXTRACT(DAY FROM d)").name == "toDay"
    assert _first_select("SELECT SUBSTRING(s FROM 1 FOR 2)").name == "substring"
    assert _first_select("SELECT TRIM(BOTH 'x' FROM s)").name == "trimBoth"
    assert _first_select("SELECT TRIM(LEADING 'x' FROM s)").name == "trimLeft"
    assert _first_select("SELECT TRIM(TRAILING 'x' FROM s)").name == "trimRight"
    assert _first_select("SELECT EXISTS (SELECT 1 FROM t)").name == "exists"


def test_parameter_and_subquery_expression():
    param = _first_select("SELECT {p:UInt32}")
    assert isinstance(param, A.ParameterExpr)
    assert param.name == "p" and param.type_name == "UInt32"

    sub = _first_select("SELECT (SELECT 1)")
    assert isinstance(sub, A.SubqueryExpr)
    assert isinstance(sub.query, A.SelectQuery)


def test_table_reference_fields():
    ref = parse("SELECT * FROM db.t FINAL AS x").from_
    assert isinstance(ref, A.TableReference)
    assert ref.database == "db" and ref.table == "t"
    assert ref.final is True and ref.alias == "x"
    assert ref.qualified_name() == "db.t"

    sampled = parse("SELECT * FROM t SAMPLE 0.1").from_
    assert isinstance(sampled.sample, A.Literal) and sampled.sample.value == 0.1

    tfn = parse("SELECT * FROM numbers(10)").from_
    assert isinstance(tfn.table_function, A.TableFunctionCall)
    assert tfn.table_function.name == "numbers"
    assert tfn.table_function.args[0].value == 10

    subq = parse("SELECT * FROM (SELECT 1) AS x").from_
    assert isinstance(subq, A.SubqueryRef)
    assert subq.alias == "x" and isinstance(subq.query, A.SelectQuery)


def test_join_node_fields():
    j = parse("SELECT * FROM a ANY LEFT JOIN b ON a.x = b.x").from_
    assert isinstance(j, A.JoinExpr)
    assert j.kind == "LEFT" and j.strictness == "ANY"

    using = parse("SELECT * FROM a JOIN b USING (x, y)").from_
    assert using.using_columns == ["x", "y"]
    assert using.on_expr is None

    cross = parse("SELECT * FROM a, b").from_
    assert cross.kind == "CROSS"

    glob = parse("SELECT * FROM a GLOBAL JOIN b ON a.x = b.x").from_
    assert glob.global_ is True


def test_select_query_clause_fields():
    q = parse(
        "WITH 1 AS x SELECT DISTINCT a FROM t PREWHERE p > 0 WHERE b < 1 "
        "GROUP BY a HAVING count() > 1 ORDER BY a DESC LIMIT 5 SETTINGS max_threads = 4"
    )
    assert isinstance(q, A.SelectQuery)
    assert q.distinct is True
    assert q.with_exprs[0].name == "x" and q.with_exprs[0].is_subquery is False
    assert isinstance(q.prewhere, A.BinaryOp)
    assert isinstance(q.where, A.BinaryOp)
    assert isinstance(q.group_by, A.GroupByClause)
    assert isinstance(q.having, A.BinaryOp)
    assert q.order_by[0].direction == "DESC"
    assert q.limit.value == 5
    assert q.settings == [("max_threads", q.settings[0][1])]
    assert q.settings[0][0] == "max_threads"
    assert q.settings[0][1].value == 4


def test_with_clause_subquery_form():
    """``WITH (SELECT …) AS m`` —— 标量子查询形态(与具名 CTE 不是一回事)。"""
    q = parse("WITH (SELECT max(x) FROM t) AS m SELECT m")
    we = q.with_exprs[0]
    assert we.name == "m"
    assert we.is_subquery is False
    assert isinstance(we.expr, A.SubqueryExpr)


def test_group_by_modifier_flags():
    assert parse("SELECT * FROM t GROUP BY a WITH ROLLUP").group_by.with_rollup is True
    assert parse("SELECT * FROM t GROUP BY a WITH CUBE").group_by.with_cube is True
    assert parse("SELECT * FROM t GROUP BY a WITH TOTALS").group_by.with_totals is True

    sets = parse("SELECT * FROM t GROUP BY GROUPING SETS ((a, b), ())").group_by
    assert len(sets.grouping_sets) == 2
    assert len(sets.grouping_sets[0]) == 2
    assert sets.grouping_sets[1] == []

    all_groups = parse("SELECT * FROM t GROUP BY ALL").group_by
    assert isinstance(all_groups.elements[0], A.Identifier)
    assert all_groups.elements[0].name == "__ALL__"


def test_order_by_element_fields():
    el = parse("SELECT * FROM t ORDER BY a DESC NULLS FIRST").order_by[0]
    assert isinstance(el, A.OrderByElement)
    assert el.direction == "DESC" and el.nulls == "FIRST"

    col = parse("SELECT * FROM t ORDER BY a COLLATE 'tr'").order_by[0]
    assert col.collate == "tr"
    assert col.direction == "ASC"     # 缺省方向被补全

    fill = parse("SELECT * FROM t ORDER BY a WITH FILL").order_by[0]
    assert fill.with_fill is True     # 解析下来了,但 to_sql 不会打出去(见第 5 节)


def test_limit_and_limit_by_fields():
    q = parse("SELECT * FROM t LIMIT 10 OFFSET 5")
    assert q.limit.value == 10 and q.offset.value == 5
    assert q.limit_by is None

    mysql_style = parse("SELECT * FROM t LIMIT 5, 10")     # LIMIT offset, count
    assert mysql_style.limit.value == 10 and mysql_style.offset.value == 5

    lb = parse("SELECT * FROM t LIMIT 3 BY a").limit_by
    assert isinstance(lb, A.LimitByClause)
    assert lb.limit.value == 3
    assert lb.elements[0].name == "a"
    assert parse("SELECT * FROM t LIMIT 3 BY a").limit is None   # 已挪进 limit_by


def test_union_fields():
    q = parse("SELECT 1 UNION ALL SELECT 2")
    assert q.union_distinct is False
    assert len(q.unions) == 1
    assert q.unions[0].selects[0].value == 2

    d = parse("SELECT 1 UNION DISTINCT SELECT 2")
    assert d.union_distinct is True

    # 裸 UNION 被规范化成 UNION ALL
    assert parse("SELECT 1 UNION SELECT 2").union_distinct is False


def test_array_join_fields():
    aj = parse("SELECT * FROM t ARRAY JOIN arr AS a").array_joins[0]
    assert isinstance(aj, A.ArrayJoinExpr)
    assert aj.alias == "a" and aj.left is False
    assert aj.expr.name == "arr"


def test_window_nodes():
    wf = _first_select("SELECT row_number() OVER (PARTITION BY a ORDER BY b) FROM t")
    assert isinstance(wf, A.WindowFunction)
    assert isinstance(wf.function, A.FunctionCall)
    assert isinstance(wf.window, A.WindowSpec)
    assert wf.window.partition_by[0].name == "a"
    assert wf.window.order_by[0].direction == "ASC"

    framed = _first_select("SELECT sum(x) OVER (ORDER BY k RANGE UNBOUNDED PRECEDING) FROM t")
    frame = framed.window.frame
    assert isinstance(frame, A.WindowFrame)
    assert frame.type == "RANGE"
    assert frame.start == "UNBOUNDED_PRECEDING"
    assert frame.end == "CURRENT"

    between = _first_select(
        "SELECT sum(x) OVER (ORDER BY k ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) FROM t"
    ).window.frame
    assert between.type == "ROWS" and between.start == "PRECEDING"
    assert between.start_offset.value == 2

    named = parse("SELECT * FROM t WINDOW w AS (PARTITION BY a)")
    assert named.windows[0][0] == "w"
    assert named.windows[0][1].partition_by[0].name == "a"


def test_insert_query_fields():
    q = parse("INSERT INTO t (a, b) VALUES (1, 2), (3, 4)")
    assert isinstance(q, A.InsertQuery)
    assert q.table == "t" and q.columns == ["a", "b"]
    assert len(q.values) == 2
    assert q.values[0][0].value == 1 and q.values[1][1].value == 4
    assert q.select is None

    sel = parse("INSERT INTO t SELECT * FROM u")
    assert isinstance(sel.select, A.SelectQuery)

    fmt = parse("INSERT INTO t FORMAT CSV")
    assert fmt.format == "CSV"

    tfn = parse("INSERT INTO FUNCTION file('x.csv') VALUES (1)")
    assert tfn.table_function.name == "file"
    assert tfn.table == ""


def test_create_table_query_fields():
    q = parse("CREATE TABLE IF NOT EXISTS db.t (a UInt32) ENGINE = MergeTree ORDER BY a")
    assert isinstance(q, A.CreateTableQuery)
    assert q.if_not_exists is True
    assert q.database == "db" and q.table == "t"
    assert q.engine == "MergeTree" and q.engine_params == []
    assert len(q.order_by) == 1
    assert q.get_column("a").type_name == "UInt32"
    assert q.get_column("nope") is None

    params = parse("CREATE TABLE t (a UInt32) ENGINE = Distributed('c', 'd', 't')")
    assert params.engine == "Distributed"
    assert params.engine_params == ["c", "d", "t"]

    temporary = parse("CREATE TEMPORARY TABLE t (a UInt32)")
    assert temporary.temporary is True and temporary.engine == ""

    cluster = parse("CREATE TABLE t ON CLUSTER c (a UInt32) ENGINE = Memory")
    assert cluster.cluster == "c"

    uuid = parse("CREATE TABLE t UUID '123e4567-e89b-12d3-a456-426614174000' (a UInt32) ENGINE = Memory")
    assert uuid.uuid == "123e4567-e89b-12d3-a456-426614174000"


def test_create_table_column_modifiers():
    q = parse(
        "CREATE TABLE t (a UInt32 DEFAULT 1, b String MATERIALIZED x, "
        "c String ALIAS x, d UInt32 EPHEMERAL 1, e UInt32 NOT NULL, "
        "f String COMMENT 'hi', g UInt32 CODEC(ZSTD), x String) ENGINE = Memory"
    )
    by_name = {c.name: c for c in q.columns}
    assert by_name["a"].default_kind == "DEFAULT"
    assert by_name["a"].default_expr.value == 1
    assert by_name["b"].default_kind == "MATERIALIZED"
    assert by_name["c"].default_kind == "ALIAS"
    assert by_name["d"].default_kind == "EPHEMERAL"
    assert by_name["e"].not_null is True
    assert by_name["f"].comment == "hi"
    assert by_name["g"].codec == "ZSTD"

    ttl_col = parse("CREATE TABLE t (d DateTime TTL d + INTERVAL 1 DAY) ENGINE = Memory")
    assert isinstance(ttl_col.columns[0].ttl, A.BinaryOp)


def test_create_table_secondary_objects():
    idx = parse("CREATE TABLE t (a UInt32, INDEX i a TYPE minmax GRANULARITY 1) ENGINE = MergeTree ORDER BY a")
    assert idx.indexes[0].name == "i"
    assert idx.indexes[0].type == "minmax"
    assert idx.indexes[0].granularity == 1

    con = parse("CREATE TABLE t (a UInt32, CONSTRAINT c CHECK a > 0) ENGINE = Memory")
    assert con.constraints[0].name == "c"
    assert con.constraints[0].kind == "CHECK"

    prj = parse(
        "CREATE TABLE t (a UInt32, PROJECTION p (SELECT a, sum(a) GROUP BY a)) "
        "ENGINE = MergeTree ORDER BY a"
    )
    assert isinstance(prj.projections[0].query, A.SelectQuery)


def test_create_table_storage_clauses():
    q = parse(
        "CREATE TABLE t (a UInt32) ENGINE = MergeTree PARTITION BY a ORDER BY a "
        "PRIMARY KEY a SAMPLE BY a TTL a + INTERVAL 1 DAY SETTINGS index_granularity = 8192"
    )
    assert q.partition_by.name == "a"
    assert len(q.order_by) == 1 and q.order_by[0].name == "a"
    assert len(q.primary_key) == 1 and q.primary_key[0].name == "a"
    assert q.sample_by.name == "a"
    assert len(q.ttl) == 1 and isinstance(q.ttl[0], A.TTLDefinition)
    assert q.settings == [("index_granularity", 8192)]


def test_create_table_as_forms():
    as_sel = parse("CREATE TABLE t ENGINE = Memory AS SELECT 1")
    assert isinstance(as_sel.as_select, A.SelectQuery) and as_sel.as_table is None

    as_tbl = parse("CREATE TABLE t AS db.other")
    assert isinstance(as_tbl.as_table, A.TableReference)
    assert as_tbl.as_table.database == "db" and as_tbl.as_table.table == "other"


def test_create_other_object_fields():
    db = parse("CREATE DATABASE IF NOT EXISTS db ENGINE = Atomic")
    assert isinstance(db, A.CreateDatabaseQuery)
    assert db.name == "db" and db.if_not_exists is True and db.engine == "Atomic"

    view = parse("CREATE VIEW v AS SELECT 1")
    assert isinstance(view, A.CreateViewQuery)
    assert view.materialized is False and isinstance(view.select, A.SelectQuery)

    mv = parse("CREATE MATERIALIZED VIEW mv TO t AS SELECT 1")
    assert mv.materialized is True and mv.to_table.table == "t"

    fn = parse("CREATE FUNCTION f AS (x) -> x * 2")
    assert isinstance(fn, A.CreateFunctionQuery)
    assert isinstance(fn.body, A.Lambda)

    dic = parse(
        "CREATE DICTIONARY d (id UInt64, name String DEFAULT 'x') PRIMARY KEY id "
        "SOURCE(CLICKHOUSE(HOST 'x' PORT 9000)) LAYOUT(FLAT) LIFETIME(300)"
    )
    assert isinstance(dic, A.CreateDictionaryQuery)
    assert dic.attributes[0][0] == "id"
    assert dic.primary_key == ["id"]
    assert dic.source == "CLICKHOUSE"
    assert dic.layout == "FLAT"
    assert dic.lifetime == "300"


def test_drop_query_fields():
    q = parse("DROP TABLE IF EXISTS db.t SYNC")
    assert isinstance(q, A.DropQuery)
    assert q.kind == "TABLE" and q.if_exists is True and q.sync is True
    assert q.names[0].database == "db" and q.names[0].table == "t"

    assert parse("DROP DATABASE db").kind == "DATABASE"
    assert parse("DROP DICTIONARY d").kind == "DICTIONARY"
    assert parse("DROP VIEW v").kind == "VIEW"
    assert parse("DROP FUNCTION f").kind == "FUNCTION"
    assert parse("DROP USER u").kind == "USER"
    assert parse("DROP ROLE r").kind == "ROLE"
    assert len(parse("DROP TABLE a, b").names) == 2


def test_alter_query_fields():
    add = parse("ALTER TABLE t ADD COLUMN a UInt32")
    assert isinstance(add, A.AlterQuery)
    assert add.table == "t"
    assert add.commands[0].kind == "ADD_COLUMN"
    assert add.commands[0].column.name == "a"

    upd = parse("ALTER TABLE t UPDATE a = 1 WHERE b > 0")
    assert upd.commands[0].kind == "UPDATE"
    assert upd.commands[0].update_assignments[0][0] == "a"
    assert isinstance(upd.commands[0].predicate, A.BinaryOp)

    assert parse("ALTER TABLE t DELETE WHERE a > 0").commands[0].kind == "DELETE"
    assert parse("ALTER TABLE t DROP PARTITION 'x'").commands[0].kind == "DROP_PARTITION"
    assert parse("ALTER TABLE t MODIFY SETTING x = 4").commands[0].setting_name == "x"
    assert parse("ALTER TABLE t RESET SETTING x").commands[0].kind == "RESET_SETTING"
    assert parse("ALTER TABLE t MODIFY ORDER BY (a)").commands[0].kind == "MODIFY_ORDER_BY"
    assert parse("ALTER TABLE t FREEZE").commands[0].kind == "FREEZE"
    assert parse("ALTER TABLE t RENAME COLUMN a TO b").commands[0].new_name == "b"
    assert len(parse("ALTER TABLE t ADD COLUMN a UInt32, DROP COLUMN b").commands) == 2


def test_rename_truncate_optimize_fields():
    rn = parse("RENAME TABLE a TO b, c TO d")
    assert isinstance(rn, A.RenameQuery)
    assert len(rn.pairs) == 2 and rn.pairs[0][0].table == "a" and rn.pairs[0][1].table == "b"

    tr = parse("TRUNCATE TABLE IF EXISTS t")
    assert isinstance(tr, A.TruncateQuery) and tr.if_exists is True and tr.table == "t"

    op = parse("OPTIMIZE TABLE t PARTITION 'x' FINAL DEDUPLICATE")
    assert isinstance(op, A.OptimizeQuery)
    assert op.partition.value == "x" and op.final is True and op.deduplicate is True


def test_introspection_query_fields():
    show = parse("SHOW FULL TABLES FROM db")
    assert isinstance(show, A.ShowQuery)
    assert show.kind == "TABLES" and show.full is True and show.database == "db"

    assert parse("SHOW CREATE TABLE t").kind == "CREATE"
    assert parse("SHOW DICTIONARIES").kind == "DICTIONARIES"
    assert parse("SHOW PROCESSLIST").kind == "PROCESSLIST"
    assert parse("SHOW TABLES LIKE 'x%'").like == "x%"
    assert isinstance(parse("SHOW TABLES WHERE name = 'x'").where, A.BinaryOp)

    desc = parse("DESC db.t")
    assert isinstance(desc, A.DescribeQuery)
    assert desc.database == "db" and desc.table == "t"

    sub = parse("DESCRIBE ( SELECT 1 )")
    assert isinstance(sub.subquery, A.SelectQuery)

    tfn = parse("DESCRIBE TABLE numbers(10)")
    assert tfn.table_function.name == "numbers"

    ex = parse("EXPLAIN AST SELECT 1")
    assert isinstance(ex, A.ExplainQuery)
    assert ex.kind == "AST" and isinstance(ex.statement, A.SelectQuery)

    ex2 = parse("EXPLAIN PLAN header = 1 SELECT 1")
    assert ex2.kind == "PLAN"
    assert ex2.settings[0][0] == "header" and ex2.settings[0][1].value == 1


def test_session_query_fields():
    st = parse("SET GLOBAL a = 1, b = DEFAULT")
    assert isinstance(st, A.SetQuery)
    assert st.scope == "GLOBAL"
    assert st.items == [("a", 1), ("b", None)]

    assert parse("USE db").database == "db"

    sysq = parse("SYSTEM FLUSH LOGS")
    assert isinstance(sysq, A.SystemQuery) and sysq.command == "FLUSH LOGS"

    kill = parse("KILL MUTATION WHERE mutation_id = 'x' SYNC")
    assert isinstance(kill, A.KillQuery)
    assert kill.kind == "MUTATION" and kill.sync is True

    assert isinstance(parse("EXISTS TABLE db.t"), A.ExistsQuery)
    assert parse("EXISTS TABLE db.t").database == "db"

    assert isinstance(parse("CHECK TABLE t"), A.DescribeQuery)

    grant = parse("GRANT SELECT, INSERT ON db.t TO u")
    assert isinstance(grant, A.GrantQuery)
    assert grant.privileges == ["SELECT", "INSERT"]
    assert grant.on == "db.t" and grant.to == ["u"] and grant.revoke is False

    revoke = parse("REVOKE SELECT ON db.t FROM u")
    assert revoke.revoke is True and revoke.to == ["u"]

    user = parse("CREATE USER u")
    assert isinstance(user, A.CreateUserQuery)
    assert user.kind == "USER" and user.name == "u"
    assert parse("CREATE ROLE r").kind == "ROLE"


def test_is_aggregating_heuristic():
    assert parse("SELECT count() FROM t").is_aggregating() is True
    assert parse("SELECT a FROM t GROUP BY a").is_aggregating() is True
    assert parse("SELECT a FROM t").is_aggregating() is False


def test_format_ast_shows_node_class_names():
    text = format_ast(parse("SELECT a + 1 FROM t"))
    for name in ("SelectQuery", "BinaryOp", "TableReference", "Identifier", "Literal"):
        assert name in text, f"{name} 没出现在 EXPLAIN AST 输出里:\n{text}"


def test_format_ast_handles_none():
    assert format_ast(None) == "(empty)"


def test_node_base_class_is_abstract():
    """基类的 ``to_sql``/``children`` 会抛 NOT_IMPLEMENTED(48),不是静默返回空。"""
    base = A.Node()
    for call in (base.to_sql, base.children):
        with pytest.raises(MiniChError) as ei:
            call()
        assert ei.value.code == ErrorCode.NOT_IMPLEMENTED


def test_identifier_quoting():
    assert A.Identifier(name="x").to_sql() == "x"
    assert A.Identifier(name="x", quoted=True).to_sql() == "`x`"
    # 名字里的反引号要转义
    assert A.Identifier(name="a`b", quoted=True).to_sql() == "`a\\`b`"


def test_quoted_identifier_roundtrip():
    q = parse("SELECT `a b` FROM `t t`")
    assert q.selects[0].name == "a b"
    assert q.selects[0].to_sql() == "`a b`"
    assert q.from_.to_sql() == "`t t`"


# ══════════════════════════════════════════════════════════════════════════
# 3. 词法分析:歧义消解的"教科书案例"
# ══════════════════════════════════════════════════════════════════════════


def _kinds(sql: str) -> list:
    return [(t.kind, t.value) for t in tokenize(sql) if t.kind != TokenKind.EOF]


def test_leading_dot_float_needs_a_safe_left_neighbour():
    """``.5`` 是浮点数,``t.1`` 不是 —— 词法器必须看**前一个字符**才能消歧。"""
    # 前一个字符是空格 → ``.5`` 是一个 NUMBER 0.5
    assert _kinds("SELECT .5") == [("ident", "SELECT"), ("number", 0.5)]

    # 前一个字符是标识符字符 → ``.`` 是标点,``1`` 是独立的 NUMBER
    assert _kinds("t.1") == [("ident", "t"), ("punct", "."), ("number", 1)]

    # 前一个字符是右括号 → 同样不当作前导点小数
    assert _kinds("(a).5") == [
        ("punct", "("), ("ident", "a"), ("punct", ")"), ("punct", "."), ("number", 5),
    ]


def test_leading_dot_float_at_parser_level():
    assert _first_select("SELECT .5").value == 0.5
    elem = _first_select("SELECT t.1")
    assert isinstance(elem, A.IndexExpr)          # 元组元素访问,不是浮点数
    assert elem.index.value == 1


def test_hex_literals():
    assert _kinds("0x1f") == [("number", 31)]
    assert _kinds("0XFF") == [("number", 255)]
    with pytest.raises(MiniChError) as ei:
        tokenize("0x")
    assert ei.value.code == SYNTAX_ERROR


def test_scientific_and_decimal_numbers():
    assert _kinds("1e3") == [("number", 1000.0)]
    assert _kinds("12.5e-2") == [("number", 0.125)]
    assert _kinds("10") == [("number", 10)]
    assert isinstance(_kinds("10")[0][1], int), "没有小数点的数字必须保持 int"


def test_qualified_identifier_is_one_node():
    """``a.b`` 在表达式层是**一个**限定标识符(为了 JOIN 的 ON 能按名字归属)。"""
    node = parse_expression_text("a.b")
    assert isinstance(node, A.Identifier)
    assert node.name == "a.b"
    assert "(" not in node.to_sql(), "两段限定名不该降级成函数调用"

    # 表格别名.列 —— 同一个东西
    assert _first_select("SELECT e.user FROM t AS e").name == "e.user"

    # 三段及以上才是元组字段访问(链式 tupleElement)
    deep = parse_expression_text("a.b.c")
    assert isinstance(deep, A.FunctionCall)
    assert deep.name == "tupleElement"
    assert deep.to_sql() == "tupleElement(tupleElement(a, 'b'), 'c')"


def test_expression_text_entrypoint():
    assert parse_expression_text("1 + 2").to_sql() == "1 + 2"
    assert isinstance(parse_expression_text("x -> x"), A.Lambda)
    # 表达式解析也不允许有剩余 token
    with pytest.raises(MiniChError) as ei:
        parse_expression_text("1 2")
    assert ei.value.code == SYNTAX_ERROR


def test_token_helpers():
    tok = Token(TokenKind.IDENT, "select", 0)
    assert tok.upper == "SELECT"
    assert tok.is_keyword("SELECT", "FROM") is True
    assert tok.is_keyword("FROM") is False

    punct = Token(TokenKind.PUNCT, ">=", 3)
    assert punct.is_punct(">", ">=") is True
    assert punct.is_punct("=") is False
    assert punct.upper == ">=", "标点的 upper 就是它自己"

    number = Token(TokenKind.NUMBER, 1, 0)
    assert number.upper == "", "非字符串值的 upper 是空串"
    assert number.is_keyword("SELECT") is False, "NUMBER 不是关键字"


def test_token_positions_and_lines():
    toks = tokenize("  SELECT\n  a")
    assert toks[0].pos == 2 and toks[0].line == 1 and toks[0].col == 3
    # ``col`` 是"在**本行**里的第几个字符"(从 1 开始),所以第 2 行的第一个
    # token 应当是 3(前面两个空格),而不是 11(它在整个输入里的绝对偏移)。
    # 11 这个值曾经是真的 —— 行起点被算成了切片内部的相对位置。
    assert toks[1].pos == 11 and toks[1].line == 2 and toks[1].col == 3
    assert toks[-1].kind == TokenKind.EOF


def test_comments_and_whitespace_are_dropped():
    assert _kinds("SELECT 1 -- x\n+ 2") == [
        ("ident", "SELECT"), ("number", 1), ("punct", "+"), ("number", 2)
    ]
    assert _kinds("SELECT /* c */ 1") == [("ident", "SELECT"), ("number", 1)]
    assert _kinds("# hash comment\n1") == [("number", 1)]


def test_string_escapes():
    assert _kinds("'a\\nb'") == [("string", "a\nb")]
    assert _kinds("'it\\'s'") == [("string", "it's")]
    assert _kinds("'\\x41'") == [("string", "A")]


def test_quoted_identifier_token_flag():
    (tok,) = [t for t in tokenize("`a b`") if t.kind != TokenKind.EOF]
    assert tok.kind == TokenKind.IDENT and tok.quoted is True and tok.value == "a b"


def test_query_parameter_token():
    assert _kinds("{p:UInt32}") == [("param", ("p", "UInt32"))]


def test_keywords_set_is_not_a_token_kind():
    """关键字**不是**独立 token 类型 —— 这是有意为之(关键字可当列名)。"""
    assert "SELECT" in KEYWORDS and "FROM" in KEYWORDS
    assert _kinds("key") == [("ident", "key")]
    # 关键字裸用当列名是合法的
    assert parse("SELECT key, table, range FROM t").selects[0].name == "key"


def test_parser_exposes_max_depth():
    """深度上限必须**低于** CPython 递归上限所能承受的层数。

    每深入一层表达式要穿过约 12 个 Python 帧(``_parse_or`` 那条链),
    而 CPython 默认只给 1000 帧 —— 所以上限设在 60 而不是 200。
    这一条断言的是**这个关系**,不是一个魔数:上限乘以每层的帧数
    必须明显小于 1000,否则守卫永远够不着(见
    ``test_depth_guard_fires_before_python_recursion_error``)。
    """
    limit = Parser("SELECT 1").max_depth
    assert 0 < limit * 12 < 1000


def test_parser_class_is_usable_directly():
    p = Parser("SELECT 1")
    node = p.parse_statement()
    assert isinstance(node, A.SelectQuery)
    assert p.at_end() is True
    assert p.peek().kind == TokenKind.EOF


# ══════════════════════════════════════════════════════════════════════════
# 4. 畸形 SQL:错误码 + 消息里的位置
# ══════════════════════════════════════════════════════════════════════════

MALFORMED = [
    "SELECT",
    "SELECT * FROM",
    "SELECT (1",
    "SELECT 1 +",
    "SELECT * FROM a JOIN b",
    "SELECT )",
    "SELECT * FROM t WHERE",
    "SELECT 1 =",
    "SELECT 1 FROM t GROUP",
    "SELECT * FROM t ORDER BY",
    "SELECT * FROM t LIMIT",
    "SELECT 1 2",
    "SELECT 1 garbage extra",
    "SELECT 1; SELECT 2",
    "UPDATE t SET a = 1",
    "SELECT * FROM t WHERE a >",
    "SELECT 1 FROM t GROUP BY",
    "SELECT 1 FROM t HAVING",
]


@pytest.mark.parametrize("sql", MALFORMED, ids=[_sid(s) for s in MALFORMED])
def test_malformed_sql_raises_syntax_error_with_position(sql: str):
    with pytest.raises(MiniChError) as ei:
        parse(sql)
    e = ei.value
    assert e.code == SYNTAX_ERROR
    assert e.name == "SYNTAX_ERROR"
    assert "position" in str(e)
    assert _POSITION_RE.search(str(e)), str(e)
    assert str(e).startswith(f"Code: {SYNTAX_ERROR}. DB::Exception: ")


def test_trailing_single_word_becomes_an_implicit_alias_not_an_error():
    """``SELECT 1 garbage`` 不算错误 —— 它被当成隐式别名。"""
    assert parse("SELECT 1 garbage").to_sql() == "SELECT 1 AS garbage"
    assert parse("SELECT a b FROM t").to_sql() == "SELECT a AS b FROM `t`"


def test_syntax_error_has_no_position_attribute():
    """位置只在消息文本里 —— 没有 ``e.pos``/``e.position`` 这类属性。"""
    with pytest.raises(MiniChError) as ei:
        parse("SELECT * FROM")
    e = ei.value
    assert not hasattr(e, "pos")
    assert not hasattr(e, "position")
    assert e.code == SYNTAX_ERROR
    assert e.query_id is None


@pytest.mark.parametrize("sql", ["", ";", ";;", "   ", "\n\t", "/* c */", "-- x", "# x"])
def test_empty_query(sql: str):
    with pytest.raises(MiniChError) as ei:
        parse(sql)
    assert ei.value.code == EMPTY_QUERY
    assert ei.value.name == "EMPTY_QUERY"


def test_unterminated_string_literal():
    for sql in ["SELECT 'abc", 'SELECT "abc']:
        with pytest.raises(MiniChError) as ei:
            parse(sql)
        assert ei.value.code == CANNOT_PARSE_QUOTED_STRING, sql
        assert ei.value.name == "CANNOT_PARSE_QUOTED_STRING"


def test_dangling_escape_sequence_raises_code_29():
    """悬空的转义序列(``'a\\``)抛 ``CANNOT_PARSE_ESCAPE_SEQUENCE``(29)。

    (这条曾经失败:lexer 引用了当时还不存在的 ``ErrorCode.CANNOT_PARSE_ESCAPE_SEQUENCE``,
    于是抛的是 ``AttributeError``。实现补齐该错误码后已修复。)
    """
    with pytest.raises(MiniChError) as ei:
        tokenize("'a\\")
    assert ei.value.code == CANNOT_PARSE_ESCAPE_SEQUENCE


def test_escape_sequence_error_code_is_defined_on_errorcode_class():
    """该错误码既在名字表里、也是 ``ErrorCode`` 的类属性,且两处编号一致。"""
    assert CANNOT_PARSE_ESCAPE_SEQUENCE == 29
    assert error_code("CANNOT_PARSE_ESCAPE_SEQUENCE") == 29
    assert ErrorCode.CANNOT_PARSE_ESCAPE_SEQUENCE == 29


def test_unterminated_quoted_identifier():
    with pytest.raises(MiniChError) as ei:
        tokenize("SELECT `abc")
    assert ei.value.code == SYNTAX_ERROR


def test_unterminated_block_comment():
    with pytest.raises(MiniChError) as ei:
        tokenize("SELECT 1 /* oops")
    assert ei.value.code == SYNTAX_ERROR
    assert "Unterminated block comment" in ei.value.message


def test_unexpected_character():
    with pytest.raises(MiniChError) as ei:
        tokenize("SELECT \x01")
    assert ei.value.code == SYNTAX_ERROR
    assert "Unexpected character" in ei.value.message


def test_error_string_matches_real_clickhouse_shape():
    with pytest.raises(MiniChError) as ei:
        parse("SELECT * FROM")
    s = str(ei.value)
    assert re.match(r"^Code: 61\. DB::Exception: .+\. \(SYNTAX_ERROR\)$", s), s


def test_error_can_carry_query_id():
    e = MiniChError("boom", code=SYNTAX_ERROR).with_query_id("q-1")
    assert e.query_id == "q-1"
    assert "query_id: q-1" in str(e)


def test_error_code_helpers():
    assert error_code("SYNTAX_ERROR") == SYNTAX_ERROR
    assert error_code("NO_SUCH_ERROR_NAME") == ErrorCode.LOGICAL_ERROR_IN_MINICH


def test_query_is_too_large_code_is_124():
    """深度守卫用的错误码 —— ``ErrorCode`` 类与错误表两边都要有它。

    (曾经 ``ErrorCode`` 类里没有这个属性,守卫一触发就抛 AttributeError;
    该缺陷已修,见第 6 节。)
    """
    assert QUERY_IS_TOO_LARGE == 124
    assert error_code("QUERY_IS_TOO_LARGE") == 124


# ══════════════════════════════════════════════════════════════════════════
# 5. 已知有损的 to_sql —— 明确断言"丢了什么",不是笼统的 round-trip
# ══════════════════════════════════════════════════════════════════════════
#
# 下面这些都不是 bug(至少不是我们要修的 bug),而是**有意/已知**的信息损失。
# 它们仍然是不动点(p1 == p2),所以也可以放进语料库;这里刻意单列,是为了
# 把"丢失了什么"写清楚。


def test_lossy_named_window_drops_over():
    """具名窗口引用 ``OVER w`` 回打时丢了 ``OVER``,而且**不是**不动点。

    ``row_number() w`` 再解析时,``w`` 会被当成隐式别名 —— 二次回打变成 ``AS w``。
    这是本套件里唯一一个已知"二次回打还会变"的形态。
    """
    p1, p2 = fixpoint("SELECT row_number() OVER w FROM t WINDOW w AS (PARTITION BY a)")
    assert p1 == "SELECT row_number() w FROM `t` WINDOW w AS (PARTITION BY a)"
    assert "OVER" not in p1
    assert p2 == "SELECT row_number() AS w FROM `t` WINDOW w AS (PARTITION BY a)"
    assert p1 != p2, "具名窗口是已知的非不动点"


def test_lossy_with_fill_is_dropped():
    p1, p2 = fixpoint("SELECT * FROM t ORDER BY x WITH FILL")
    assert p1 == "SELECT * FROM `t` ORDER BY x ASC"
    assert "FILL" not in p1
    assert p1 == p2, "WITH FILL 只是被丢掉,回打本身仍是稳定的"


def test_lossy_into_outfile_is_dropped():
    p1, _ = fixpoint("SELECT 1 INTO OUTFILE 'o.tsv' FORMAT TSV")
    assert p1 == "SELECT 1 FORMAT TSV"
    assert "OUTFILE" not in p1


def test_lossy_insert_settings_are_dropped():
    p1, _ = fixpoint("INSERT INTO t SETTINGS async_insert = 1 VALUES (1)")
    assert p1 == "INSERT INTO `t` VALUES (1)"


def test_lossy_group_by_all_becomes_magic_identifier():
    p1, _ = fixpoint("SELECT * FROM t GROUP BY ALL")
    assert p1 == "SELECT * FROM `t` GROUP BY __ALL__"


def test_lossy_extract_substring_trim_desugar_to_functions():
    assert parse("SELECT EXTRACT(DAY FROM d)").to_sql() == "SELECT toDay(d)"
    assert parse("SELECT SUBSTRING(s FROM 1 FOR 2)").to_sql() == "SELECT substring(s, 1, 2)"
    assert parse("SELECT TRIM(BOTH 'x' FROM s)").to_sql() == "SELECT trimBoth(s, 'x')"
    assert parse("SELECT EXISTS (SELECT 1 FROM t)").to_sql() == "SELECT exists((SELECT 1 FROM `t`))"


def test_lossy_array_index_normalizes_to_dot_form():
    assert parse("SELECT arr[1]").to_sql() == "SELECT arr.1"
    assert parse("SELECT [1,2][1]").to_sql() == "SELECT [1, 2].1"


def test_lossy_tables_are_always_backquoted():
    assert parse("SELECT 1 FROM db.t").to_sql() == "SELECT 1 FROM `db`.`t`"
    assert parse("SELECT 1 FROM t").to_sql() == "SELECT 1 FROM `t`"


def test_lossy_limit_comma_form_is_normalized():
    assert parse("SELECT * FROM t LIMIT 5, 10").to_sql() == "SELECT * FROM `t` LIMIT 10 OFFSET 5"


def test_lossy_bare_union_becomes_union_all():
    assert parse("SELECT 1 UNION SELECT 2").to_sql() == "SELECT 1 UNION ALL SELECT 2"


def test_lossy_cast_forms_normalize():
    assert parse("SELECT CAST(a, 'UInt8')").to_sql() == "SELECT CAST(a AS UInt8)"
    assert parse("SELECT a::UInt8").to_sql() == "SELECT CAST(a AS UInt8)"


def test_lossy_implicit_alias_becomes_explicit():
    assert parse("SELECT a b FROM t").to_sql() == "SELECT a AS b FROM `t`"


def test_lossy_plural_interval_unit_is_singularized():
    assert parse("SELECT INTERVAL 3 MONTHS").to_sql() == "SELECT INTERVAL 3 MONTH"


def test_lossy_single_param_lambda_drops_parens():
    assert parse("SELECT (x) -> x + 1").to_sql() == "SELECT x -> x + 1"


def test_lossy_join_strictness_is_not_printed():
    """``ANY``/``ASOF``/``SEMI``/``ANTI`` 这些严格性**根本不会**回打到 SQL 里。"""
    assert parse("SELECT * FROM a ANY JOIN b ON a.x = b.x").to_sql() == \
        "SELECT * FROM `a` JOIN `b` ON a.x = b.x"
    assert parse("SELECT * FROM a ASOF JOIN b ON a.x = b.x").to_sql() == \
        "SELECT * FROM `a` JOIN `b` ON a.x = b.x"
    assert parse("SELECT * FROM a SEMI JOIN b ON a.x = b.x").to_sql() == \
        "SELECT * FROM `a` JOIN `b` ON a.x = b.x"
    assert parse("SELECT * FROM a ANTI JOIN b ON a.x = b.x").to_sql() == \
        "SELECT * FROM `a` JOIN `b` ON a.x = b.x"
    # ANY LEFT JOIN 只剩 LEFT
    assert parse("SELECT * FROM a ANY LEFT JOIN b ON a.x = b.x").to_sql() == \
        "SELECT * FROM `a` LEFT JOIN `b` ON a.x = b.x"


def test_lossy_global_join_gets_a_double_space():
    """``GLOBAL JOIN`` 回打成 ``GLOBAL  JOIN``(两个空格)—— 一个回打小瑕疵,但稳定。"""
    p1, p2 = fixpoint("SELECT * FROM a GLOBAL JOIN b ON a.x = b.x")
    assert p1 == "SELECT * FROM `a` GLOBAL  JOIN `b` ON a.x = b.x"
    assert p1 == p2


def test_lossy_settings_values_are_python_reprs():
    """``SET`` 的值是原样的 Python 值 —— 字符串丢引号、DEFAULT 变 None、true 变 True。"""
    assert parse("SET a = 'x', b = DEFAULT, c = true").to_sql() == "SET a = x, b = None, c = True"


def test_lossy_show_describe_forms():
    assert parse("SHOW CREATE TABLE t").to_sql() == "SHOW CREATE `t`"
    assert parse("CHECK TABLE t").to_sql() == "DESCRIBE TABLE `t`"
    assert parse("SHOW GRANTS FOR u").to_sql() == "SHOW GRANTS"


def test_lossy_optimize_deduplicate_by_is_dropped():
    assert parse("OPTIMIZE TABLE t FINAL DEDUPLICATE BY a, b").to_sql() == \
        "OPTIMIZE TABLE `t` FINAL DEDUPLICATE"


def test_lossy_explain_settings_are_dropped():
    assert parse("EXPLAIN PLAN header = 1 SELECT 1").to_sql() == "EXPLAIN PLAN SELECT 1"


def test_lossy_grant_option_is_dropped():
    assert parse("GRANT SELECT ON db.t TO u WITH GRANT OPTION").to_sql() == \
        "GRANT SELECT ON db.t TO u"


def test_lossy_materialized_view_populate_becomes_an_alias():
    """``POPULATE`` 被 SELECT 列表当成了隐式别名 —— 回打成 ``AS POPULATE``。"""
    assert parse("CREATE MATERIALIZED VIEW mv TO t AS SELECT 1 POPULATE").to_sql() == \
        "CREATE MATERIALIZED VIEW `mv` TO `t` AS SELECT 1 AS POPULATE"


def test_lossy_create_function_drops_param_parens():
    assert parse("CREATE FUNCTION f AS (x) -> x * 2").to_sql() == "CREATE FUNCTION f AS x -> x * 2"


def test_lossy_create_user_clauses_are_raw_token_values():
    assert parse("CREATE USER IF NOT EXISTS u IDENTIFIED BY 'p'").to_sql() == \
        "CREATE USER IF NOT EXISTS u IDENTIFIED BY p"


def test_lossy_create_or_replace_and_temporary_drop_flags():
    assert parse("CREATE OR REPLACE TABLE t (a UInt32) ENGINE = Memory").to_sql().startswith(
        "CREATE TABLE `t`"
    )
    assert parse("DROP TEMPORARY TABLE t").to_sql() == "DROP TABLE `t`"


def test_lossy_system_command_upper_cases_and_swallows_table():
    """``SYSTEM STOP MERGES t`` —— 表名被并进 command 并全部大写成 ``T``。"""
    p1, p2 = fixpoint("SYSTEM STOP MERGES t")
    assert p1 == "SYSTEM STOP MERGES T"
    assert p1 == p2


def test_lossy_alter_add_column_after_is_dropped():
    assert parse("ALTER TABLE t ADD COLUMN IF NOT EXISTS a UInt32 AFTER b").to_sql() == \
        "ALTER TABLE `t` ADD COLUMN `a` UInt32"


def test_lossy_dictionary_source_params_are_flattened():
    p1 = parse(
        "CREATE DICTIONARY d (id UInt64) PRIMARY KEY id "
        "SOURCE(CLICKHOUSE(HOST 'x' PORT 9000)) LAYOUT(FLAT) LIFETIME(300)"
    ).to_sql()
    assert "SOURCE(CLICKHOUSE(HOST, x, PORT, 9000))" in p1


def test_lossy_alias_backticks_are_dropped():
    """别名是个裸字符串 —— ``AS `a b``` 会回打成 ``AS a b``(见第 7 节)。"""
    assert parse("SELECT 1 AS `a b`").to_sql() == "SELECT 1 AS a b"
    assert parse("SELECT 1 AS `select`").to_sql() == "SELECT 1 AS select"


# ══════════════════════════════════════════════════════════════════════════
# 6. 回归:曾经解析不了的写法(**现已修复**)
# ══════════════════════════════════════════════════════════════════════════
#
# 这一节里每一条都曾经是**真实的缺陷**:写作期间有一路独立的代码审计
# 逐条挖出来、钉成 `xfail()` 摆在这里,然后被修掉了。
#
# 现在它们变成了**正向断言** —— 把这 11 条留在文件里,是因为它们各自
# 代表一类**很容易再犯**的错误。每条的注释都写了"原来错在哪、为什么
# 会写成那样",这比测试本身有价值。
#
# 共同点:它们全是"**写法合法、语义清楚,但解析器/词法器拒绝或误解**"。
# 这类缺陷不会让任何已有测试变红 —— 它们只在**读者照着文档写代码**时
# 才暴露。所以只能靠"拿着真实 ClickHouse 的语法清单逐条比对"来发现。


def test_count_star_is_count_of_rows():
    """``count(*)`` —— ``*`` 在计数函数里**不是通配符**,只是惯用写法。

    原来错在:函数实参列表直接走 `parse_expression()`,于是 `*` 变成一个
    ``StarExpr`` 节点,而求值器对它的处理是"必须提前展开成具体列" ——
    报出一句和实际原因毫无关系的 ``'*' must be expanded into concrete
    columns``。**``SELECT count(*) FROM t`` 是最常见的 SQL 之一**,
    它跑不通这件事不该被任何测试放过。
    """
    node = parse("SELECT count(*) FROM t")
    fn = node.selects[0]
    assert fn.name.lower() == "count"
    assert fn.args == []


def test_count_distinct_is_the_distinct_combinator():
    """``count(DISTINCT x)`` 是 ``countDistinct(x)`` 的语法糖。

    原来错在:实参列表不接受 ``DISTINCT`` 关键字 —— 它被当成一个标识符
    去解析,然后撞上 ``)`` 报 "Unexpected token"。修法是在解析期就把它
    换算成 ``-Distinct`` 组合器(真实 ClickHouse 也是这么做的),
    这样聚合函数的实现里不必到处写 ``if distinct:``。
    """
    node = parse("SELECT count(DISTINCT x) FROM t")
    assert node.selects[0].name == "countDistinct"


def test_paste_join_needs_no_condition():
    """``PASTE JOIN`` 是按位置拼的,本来就没有连接条件。"""
    node = parse("SELECT * FROM a PASTE JOIN b")
    assert node.from_.kind == "PASTE"


def test_limit_with_both_offset_and_by():
    """``LIMIT n OFFSET m BY expr`` —— OFFSET 可以出现在 BY 之前。"""
    node = parse("SELECT * FROM t ORDER BY a LIMIT 3 OFFSET 1 BY b")
    lb = node.limit_by
    assert lb is not None
    assert lb.limit.value == 3 and lb.offset.value == 1
    assert lb.elements[0].name == "b"


@pytest.mark.parametrize(
    "sql,left",
    [
        ("SELECT * FROM t ARRAY JOIN arr", False),
        ("SELECT * FROM t LEFT ARRAY JOIN arr", True),
        ("SELECT * FROM t ARRAY LEFT JOIN arr", True),
    ],
)
def test_array_join_accepts_both_keyword_orders(sql, left):
    """``LEFT ARRAY JOIN`` 是**标准写法**,``ARRAY LEFT JOIN`` 是它的历史顺序。

    原来只认后者的顺序 —— 于是读者最可能敲的那一种过不了。
    两边的语义完全相同(保留空数组所在的行)。
    """
    node = parse(sql)
    assert node.array_joins[0].left is left


def test_named_cte_with_parenthesized_subquery():
    """``WITH name AS (SELECT …)`` —— 判据是"名字后面跟 **AS**"。

    原来错在:形态判断写成了"名字后面跟左括号"(``peek(1)`` 是不是 ``(``)。
    而真实输入里 ``AS`` 夹在中间,于是这个分支永远进不去,
    语句回落成"表达式形态的 WITH"再报 ``Expected alias`` ——
    一个把读者指向**错误方向**的消息。
    """
    node = parse("WITH x AS (SELECT 1 AS v) SELECT v FROM x")
    assert node.with_exprs[0].is_subquery is True
    assert node.with_exprs[0].name == "x"


def test_alter_add_constraint_consumes_name_and_check():
    """``ALTER TABLE ... ADD CONSTRAINT c CHECK expr`` 要把名字与表达式都吃掉。"""
    node = parse("ALTER TABLE t ADD CONSTRAINT c CHECK a > 0")
    cmd = node.commands[0]
    assert cmd.kind == "ADD_CONSTRAINT"
    assert cmd.name == "c"
    assert cmd.predicate is not None


def test_depth_guard_fires_before_python_recursion_error():
    """深度守卫必须在 Python 的递归上限**之前**触发。

    原来的错法很典型:**守卫写了,但够不着**。``max_depth`` 设成 200,
    而每深入一层表达式要穿过 ``_parse_or → _parse_and → … → _parse_primary``
    约 12 个 Python 帧 —— 200 × 12 = 2400 帧,远超 CPython 默认的 1000。
    于是读者拿到的是一句 ``maximum recursion depth exceeded``,
    而我们精心准备的 ``QUERY_IS_TOO_LARGE`` 从来没被抛出过。
    """
    with pytest.raises(MiniChError) as ei:
        parse("SELECT " + "(" * 400 + "1" + ")" * 400)
    assert ei.value.code == ErrorCode.QUERY_IS_TOO_LARGE
    assert "depth" in ei.value.message.lower()


def test_leading_dot_float_at_the_very_start():
    """``SELECT .5`` —— 输入**开头**的前导点小数。

    原来错在:消歧的判据是"前一个字符不是标识符字符",而输入开头
    "没有前一个字符"被写成了空串 —— Python 里**空字符串是任何字符串的子串**,
    ``"" in _IDENT_CHARS`` 恒为真,于是 ``.5`` 被判成"跟在标识符后面",
    降级成一个裸的 ``.`` 标点。
    """
    node = parse("SELECT .5")
    assert node.selects[0].value == 0.5


def test_column_number_is_absolute_after_a_newline():
    """跨行报错时,位置信息里的行号要对。

    这一条只保证"位置信息存在且行号正确" —— 列号本身在**错误消息**
    里没有单独暴露(消息里给的是绝对 position)。真正的列号断言在
    ``test_token_positions_and_lines``。
    """
    with pytest.raises(MiniChError) as ei:
        parse("SELECT 1\nFROM t\nWHERE )")
    assert "line 3" in ei.value.message


def test_map_literal_is_not_swallowed_by_query_parameter_lexing():
    """``{'a': 1}`` 是 Map 字面量,不是查询参数 ``{name:Type}``。

    原来错在:词法层只要"花括号里有冒号"就当查询参数吞掉整块 ——
    于是 ``SELECT {'a': 1}`` 里的 ``'a'`` 变成了"参数名"。
    正确判据是**冒号前面那一截得像一个裸标识符**。
    """
    node = parse("SELECT {'a': 1}")
    m = node.selects[0]
    assert m.pairs[0][0].value == "a"
    assert m.pairs[0][1].value == 1
    # 而真正的查询参数仍然要认出
    param = parse("SELECT {p:UInt8}").selects[0]
    assert param.name == "p" and param.type_name == "UInt8"


# ══════════════════════════════════════════════════════════════════════════
# 7. 仍然坏着:to_sql 打出来的文本**自己解析不了**
# ══════════════════════════════════════════════════════════════════════════
#
# 第 5 节讲的"有损"顶多是丢信息,回打出来的东西**还是合法 SQL**;
# 这一节更糟:回打出的文本压根不是 SQL —— ``parse(p1)`` 直接报 SYNTAX_ERROR,
# 连"不动点"都无从谈起(这正是为什么第 1 节的语料库**不能**靠遍历
# 语句种类来写,只能用一份人工核对过的白名单)。
#
# 根因高度集中:``AlterCommand.to_sql`` / ``ShowQuery.to_sql`` 用的是
# "一长串 if kind == ..." 的写法,漏了分支时最后一行是 ``return self.kind`` ——
# 于是把**内部的枚举名**当 SQL 打了出来(``MATERIALIZE_INDEX``、
# ``MODIFY_SAMPLE_BY`` …)。这些名字里带下划线,再解析时连一个关键字都匹配不上。

_STILL_BROKEN_ROUNDTRIP = [
    # (原语句, 回打出的(非法)文本)
    ("ALTER TABLE t MATERIALIZE INDEX i", "ALTER TABLE `t` MATERIALIZE_INDEX"),
    ("ALTER TABLE t MATERIALIZE COLUMN a", "ALTER TABLE `t` MATERIALIZE_COLUMN"),
    ("ALTER TABLE t MATERIALIZE PROJECTION p", "ALTER TABLE `t` MATERIALIZE_PROJECTION"),
    ("ALTER TABLE t DROP PROJECTION p", "ALTER TABLE `t` DROP_PROJECTION"),
    ("ALTER TABLE t MODIFY SAMPLE BY a", "ALTER TABLE `t` MODIFY_SAMPLE_BY"),
    ("ALTER TABLE t REMOVE TTL", "ALTER TABLE `t` REMOVE_TTL"),
    ("ALTER TABLE t START MERGES", "ALTER TABLE `t` START_MERGES"),
    ("ALTER TABLE t STOP MERGES", "ALTER TABLE `t` STOP_MERGES"),
    # 下面这几条是"信息丢了一半":PROJECTION/FETCH/MOVE 后面的东西没了
    ("ALTER TABLE t ADD PROJECTION p (SELECT a GROUP BY a)", "ALTER TABLE `t` ADD_PROJECTION"),
    ("ALTER TABLE t ADD CONSTRAINT c CHECK a > 0", "ALTER TABLE `t` ADD_CONSTRAINT"),
    ("ALTER TABLE t MOVE PARTITION 'x' TO DISK 'd'", "ALTER TABLE `t` MOVE PARTITION 'x'"),
    ("ALTER TABLE t FETCH PARTITION 'x' FROM '/z'", "ALTER TABLE `t` FETCH"),
    # SHOW COLUMNS 认得 FROM,回打时却把 FROM 弄丢了
    ("SHOW COLUMNS FROM t", "SHOW COLUMNS `t`"),
    # 别名是裸字符串,反引号被丢掉 —— 于是 ``AS a b`` 变成了"别名 + 多余 token"
    ("SELECT 1 AS `a b`", "SELECT 1 AS a b"),
]


@pytest.mark.parametrize(
    "sql, expected",
    _STILL_BROKEN_ROUNDTRIP,
    ids=[_sid(s) for s, _ in _STILL_BROKEN_ROUNDTRIP],
)
def test_broken_roundtrip_current_text(sql: str, expected: str):
    """先把"当前丢成什么样"钉住 —— 这条断言**是绿的**(记录现状,不是缺陷证明)。"""
    assert parse(sql).to_sql() == expected


@pytest.mark.xfail(
    raises=MiniChError,
    reason="AlterCommand/ShowQuery 的 to_sql 缺分支时会把内部枚举名原样打出"
    "(如 MATERIALIZE_INDEX),回打文本无法再次解析 —— 回打链在这里是断的,不是只丢信息",
)
@pytest.mark.parametrize(
    "sql",
    [s for s, _ in _STILL_BROKEN_ROUNDTRIP],
    ids=[_sid(s) for s, _ in _STILL_BROKEN_ROUNDTRIP],
)
def test_broken_roundtrip_is_not_a_fixpoint(sql: str):
    """期望:``parse(parse(sql).to_sql())`` 能跑通。现状:报 SYNTAX_ERROR。"""
    parse(parse(sql).to_sql())


# ══════════════════════════════════════════════════════════════════════════
# 8. 回归:FORMAT 之后的 payload 与排序键方向(**现已修复**)
# ══════════════════════════════════════════════════════════════════════════


# ── 8.1 ``FORMAT`` 之后是数据,不是 SQL ───────────────────────────────────


def test_insert_format_payload_with_backslash_is_taken_verbatim():
    """``INSERT … FORMAT TSV`` 之后的数据里出现反斜杠,必须原样取走。

    .. warning::

       这条以前直接报 ``Code:61 Unexpected character '\\\\'`` ——
       根因是 ``Parser.__init__`` 把**整条 SQL(含 FORMAT 之后的数据)**
       一次交给词法器切完,于是数据里有一个词法器不认识的字符,
       在"取 payload"那一步**之前**就炸了。而 TSV/CSV writer 对任何含
       ``\\`` 的值恰恰就是这么写的(``a\\b`` → ``a\\\\b``)——
       自己写出去的文本自己读不回来。

       修法是让解析器**按需**切 token:一遇到 ``FORMAT <name>`` 就停手,
       把剩下的原文按偏移切片交给格式解析器(真实 ClickHouse 的
       ``TSVRowInputFormat`` 同样不经过 SQL 词法器)。
    """
    node = parse("INSERT INTO t FORMAT TSV\na\\b\n")
    assert node.format == "TSV"
    assert node.data == "a\\b\n"


@pytest.mark.parametrize(
    "payload",
    [
        "a\\b\n",            # 反斜杠(TSV/CSV 转义后的形态)
        "\\\n",              # 一个孤零零的反斜杠
        "it's \"q\"\n",      # 引号
        "1\t2;\n",           # 分号:在 payload 里不是语句分隔符
        "x -- not a comment\n",
        "/* not a comment */\n",
        "a\x01b\n",          # 词法器根本不认识的字符
        "",                  # 没有数据
    ],
    ids=["backslash", "lone_backslash", "quotes", "semicolon",
         "dashdash", "blockcomment", "ctrl-char", "empty"],
)
def test_insert_format_payload_is_never_tokenized(payload: str):
    """payload 原封不动(一个字符都不许被词法器/注释规则吃掉)。"""
    node = parse("INSERT INTO t FORMAT CSV\n" + payload)
    assert node.data == payload


def test_insert_format_tsv_output_can_be_read_back_again(memory_ctx):
    """``SELECT … FORMAT TSV`` 的输出要能再喂回 ``INSERT … FORMAT TSV``。

    这是上面那条缺陷的**端到端形状**:把含反斜杠的值写出去(TSV 会转义成
    ``\\\\``),再把这段文本当成 SQL 执行 —— 以前第二步报
    ``Unexpected character '\\'``。
    """
    ctx = memory_ctx
    ctx.execute("CREATE TABLE t (a String) ENGINE = Memory")
    ctx.execute("INSERT INTO t VALUES ('a\\\\b')")   # SQL 里的 'a\\b' → 值 a\b
    text = ctx.execute("SELECT a FROM t FORMAT TSV").text()
    assert text == "a\\\\b\n"

    ctx.execute("CREATE TABLE t2 (a String) ENGINE = Memory")
    ctx.execute("INSERT INTO t2 FORMAT TSV\n" + text)
    assert [tuple(r) for r in ctx.execute("SELECT a FROM t2").rows()] == [("a\\b",)]


def test_lazy_tokenization_does_not_swallow_real_syntax_errors():
    """惰性切 token **不能**把真正的错误吞掉 —— 只有 payload 那一段例外。

    这两条走的是同一条路径(``peek`` 时才切),区别在于
    "FORMAT 之后的文本"被明确标记成了数据,别的地方照旧要报错。
    """
    with pytest.raises(MiniChError) as ei:
        parse("SELECT 1 2")
    assert ei.value.code == SYNTAX_ERROR
    with pytest.raises(MiniChError):          # \x01 在词法层就被拒
        parse("SELECT 1\x01")
    with pytest.raises(MiniChError):          # 只有 FORMAT 之后才豁免
        parse("INSERT INTO t VALUES (1, \x01)")


# ── 8.2 ``CREATE TABLE … ORDER BY … DESC`` ──────────────────────────────


def test_create_table_order_by_desc_parses():
    """``ORDER BY k DESC``(不带括号)必须能解析,方向要留下来。

    以前这一支用的是"表达式列表"解析器,它不认识 ``ASC``/``DESC`` ——
    于是报 ``Unexpected trailing token 'DESC'``,引擎里那套
    ``sorting_key_reverse``/``_Descending`` 支持**永远走不到**(死代码)。
    真实 ClickHouse 两种写法都接受:括号只是分组,不改变"这是几个排序键"。
    """
    node = parse("CREATE TABLE t (k UInt32) ENGINE = MergeTree ORDER BY k DESC")
    assert [getattr(e, "direction", "ASC") for e in node.order_by] == ["DESC"]
    assert node.order_by[0].name == "k"

    node = parse("CREATE TABLE t (k1 UInt32, k2 UInt32) ENGINE = MergeTree "
                 "ORDER BY (k1 ASC, k2 DESC)")
    assert [e.direction for e in node.order_by] == ["ASC", "DESC"]


def test_create_table_order_by_desc_is_a_text_fixpoint():
    """回打要带上方向,而且回打文本自己还能再解析(不动点)。"""
    p1, p2 = fixpoint("CREATE TABLE t (k UInt32) ENGINE = MergeTree ORDER BY k DESC")
    assert "ORDER BY (k DESC)" in p1, p1
    assert p1 == p2


def test_create_table_default_direction_is_unchanged():
    """没写 ``DESC`` 的排序键,回打结果与以前**逐字一样**(不许凭空多个 ASC)。"""
    a = parse("CREATE TABLE t (k UInt32) ENGINE = MergeTree ORDER BY k").to_sql()
    b = parse("CREATE TABLE t (k UInt32) ENGINE = MergeTree ORDER BY (k)").to_sql()
    assert a == b
    assert "DESC" not in a and "ASC" not in a


def test_create_table_order_by_desc_reaches_the_engine(disk_ctx):
    """方向要一路走到引擎的 ``sorting_key_reverse``,并真的改变 part 里的行序。"""
    ctx = disk_ctx
    ctx.execute("CREATE TABLE t (k UInt32, v String) "
                "ENGINE = MergeTree ORDER BY k DESC")
    engine = ctx.catalog.get_table(ctx.database, "t").engine
    assert engine.sorting_key_reverse == [True]

    ctx.execute("INSERT INTO t VALUES (3, 'c'), (1, 'a'), (2, 'b')")
    assert [r[0] for r in ctx.execute("SELECT k FROM t").rows()] == [3, 2, 1]
    assert "k DESC" in ctx.execute("SHOW CREATE TABLE t").rows()[0][0]
