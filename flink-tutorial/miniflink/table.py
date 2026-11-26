"""Table API 与 SQL 的教学实现。

MiniFlink 的 Table 层是“有界表”实现：数据先物化为行集合，再借助标准库
``sqlite3`` 执行关系代数。这足以演示 schema、投影、过滤、聚合、join、排序与
窗口列，但不具备 Flink Table API 的流式增量计算、changelog 和优化器。

真实 Flink 中，Table API 与 DataStream API 共享同一个运行时，SQL 会经过
Calcite 校验、逻辑计划、物理计划与算子融合。这里刻意把边界说清楚：MiniFlink
的 SQL 不是 Flink SQL 兼容层。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .api import DataStream, StreamExecutionEnvironment
from .core import Duration


@dataclass(frozen=True)
class Field:
    name: str
    type_name: str = "STRING"
    nullable: bool = True


@dataclass
class Schema:
    fields: list[Field]
    rowtime: str | None = None

    @classmethod
    def from_names(cls, names: list[str]) -> "Schema":
        return cls([Field(name, "STRING") for name in names])

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> "Schema":
        if not rows:
            return cls([])
        expected = set(rows[0])
        for index, row in enumerate(rows[1:], start=2):
            extra = set(row) - expected
            if extra:
                # 不能按首行 schema 静默丢掉后面的列；Flink 会拒绝
                # schema 不一致的输入。缺字段可以按 NULL 处理，多字段必须报错。
                raise ValueError(
                    f"第 {index} 行包含首行没有的字段: {sorted(extra)}；"
                    "Table 需要统一 schema"
                )
        fields: list[Field] = []
        for name in rows[0]:
            # 必须看整列，而不是第一个非空值：一列里既有数字又有字符串时，
            # 按第一个值推断会让 SQLite 用数值/文本亲和性做出错误比较。
            values = [row.get(name) for row in rows if row.get(name) is not None]
            fields.append(Field(name, _infer_column_type(values)))
        return cls(fields)

    @property
    def names(self) -> list[str]:
        return [field.name for field in self.fields]

    def index_of(self, name: str) -> int:
        for index, field in enumerate(self.fields):
            if field.name == name:
                return index
        raise KeyError(name)

    def to_sqlite_ddl(self, table_name: str) -> str:
        columns = []
        for field in self.fields:
            sqlite_type = {
                "INT": "INTEGER",
                "BIGINT": "INTEGER",
                "DOUBLE": "REAL",
                "BOOLEAN": "INTEGER",
                "TIMESTAMP": "INTEGER",
                "BYTES": "BLOB",
            }.get(field.type_name, "TEXT")
            columns.append(f'"{field.name}" {sqlite_type}')
        return f'CREATE TABLE "{table_name}" ({", ".join(columns)})'


@dataclass
class Row:
    values: tuple[Any, ...]

    def __getitem__(self, index: int) -> Any:
        return self.values[index]

    def __iter__(self):
        return iter(self.values)

    def as_dict(self, schema: Schema) -> dict[str, Any]:
        return dict(zip(schema.names, self.values))


class Table:
    def __init__(self, rows: list[dict[str, Any]], schema: Schema, name: str | None = None) -> None:
        if not schema.fields:
            # SQLite 不接受零列表；空输入又无法推断 schema 时，应在建表
            # 阶段就给出明确错误，而不是等到 SQL 执行时报语法错误。
            raise ValueError(
                "Table 至少需要一列；空输入/无 schema 的数据流请显式提供 schema"
            )
        self.rows = rows
        self.schema = schema
        self.name = name

    # ---- 关系操作 ----

    def select(self, *expressions: str) -> "Table":
        if not expressions:
            raise ValueError("select 至少需要一个表达式")
        return self._query(f"SELECT {', '.join(expressions)} FROM __table")

    def where(self, condition: str) -> "Table":
        return self._query(f"SELECT * FROM __table WHERE {condition}")

    def filter(self, condition: str) -> "Table":
        return self.where(condition)

    def group_by(self, *columns: str) -> "GroupedTable":
        return GroupedTable(self, list(columns))

    def order_by(self, *expressions: str) -> "Table":
        return self._query(f"SELECT * FROM __table ORDER BY {', '.join(expressions)}")

    def limit(self, count: int) -> "Table":
        if count < 0:
            raise ValueError("limit 不能为负数")
        return self._query(f"SELECT * FROM __table LIMIT {int(count)}")

    def join(
        self,
        other: "Table",
        condition: str,
        *,
        how: str = "INNER",
    ) -> "Table":
        how = how.upper()
        if how not in {"INNER", "LEFT", "RIGHT", "FULL"}:
            raise ValueError("join 类型必须是 INNER/LEFT/RIGHT/FULL")
        connection = self._connection("__left")
        try:
            other._load(connection, "__right")
            resolved_condition = condition
            if self.name:
                resolved_condition = re.sub(
                    rf"\b{re.escape(self.name)}\s*\.",
                    '"__left".',
                    resolved_condition,
                )
            if other.name:
                resolved_condition = re.sub(
                    rf"\b{re.escape(other.name)}\s*\.",
                    '"__right".',
                    resolved_condition,
                )
            cursor = connection.execute(
                f'SELECT "__left".*, "__right".* FROM "__left" '
                f'{how} JOIN "__right" ON {resolved_condition}'
            )
            rows, schema = _fetch_rows(cursor)
            return Table(rows, schema)
        finally:
            connection.close()

    def window_tumble(
        self, time_column: str, size: Duration | int, offset: int = 0
    ) -> "Table":
        """给每行补上 window_start/window_end，供后续分组。"""
        size_ms = size.millis if isinstance(size, Duration) else int(size)
        if size_ms <= 0:
            raise ValueError("窗口大小必须大于 0")
        if time_column not in self.schema.names:
            raise KeyError(time_column)
        rows = []
        for row in self.rows:
            ts = row[time_column]
            start = ts - ((ts - offset) % size_ms)
            enriched = dict(row)
            enriched["window_start"] = start
            enriched["window_end"] = start + size_ms
            rows.append(enriched)
        schema = Schema(
            self.schema.fields + [Field("window_start", "TIMESTAMP"), Field("window_end", "TIMESTAMP")]
        )
        return Table(rows, schema)

    def window_tumble_start(
        self, time_column: str, size: Duration | int, offset: int = 0
    ) -> "Table":
        return self.window_tumble(time_column, size, offset)

    # ---- 输出 ----

    def to_data_stream(self, env: StreamExecutionEnvironment) -> DataStream:
        return env.from_collection([dict(row) for row in self.rows], name="table-source")

    def execute(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]

    def collect(self) -> list[dict[str, Any]]:
        return self.execute()

    def print_schema(self) -> str:
        return "\n".join(
            f"{field.name}: {field.type_name}{'' if field.nullable else ' NOT NULL'}"
            for field in self.schema.fields
        )

    def __iter__(self):
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    # ---- 内部 ----

    def _connection(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        self._load(connection, name)
        return connection

    def _load(self, connection: sqlite3.Connection, name: str) -> None:
        connection.execute(self.schema.to_sqlite_ddl(name))
        if not self.rows:
            return
        placeholders = ", ".join("?" for _ in self.schema.fields)
        columns = ", ".join(f'"{field.name}"' for field in self.schema.fields)
        values = [
            tuple(_sqlite_value(row.get(field.name)) for field in self.schema.fields)
            for row in self.rows
        ]
        connection.executemany(
            f'INSERT INTO "{name}" ({columns}) VALUES ({placeholders})', values
        )

    def _query(self, sql: str) -> "Table":
        connection = self._connection("__table")
        try:
            cursor = connection.execute(sql)
            rows, schema = _fetch_rows(cursor)
            return Table(rows, schema)
        finally:
            connection.close()


class GroupedTable:
    def __init__(self, table: Table, keys: list[str]) -> None:
        if not keys:
            raise ValueError("group_by 至少需要一个字段")
        self.table = table
        self.keys = keys

    def aggregate(self, **aggregations: str) -> Table:
        """例如 ``aggregate(total="SUM(amount)", cnt="COUNT(*)")``。"""
        if not aggregations:
            raise ValueError("aggregate 至少需要一个聚合表达式")
        projections = [f'"{key}"' for key in self.keys]
        projections += [f"{expr} AS \"{alias}\"" for alias, expr in aggregations.items()]
        sql = (
            f"SELECT {', '.join(projections)} FROM __table "
            f"GROUP BY {', '.join('\"' + key + '\"' for key in self.keys)}"
        )
        connection = self.table._connection("__table")
        try:
            cursor = connection.execute(sql)
            rows, schema = _fetch_rows(cursor)
            return Table(rows, schema)
        finally:
            connection.close()


class TableEnvironment:
    def __init__(self) -> None:
        self._tables: dict[str, Table] = {}

    @classmethod
    def create(cls, env: StreamExecutionEnvironment | None = None) -> "TableEnvironment":
        return cls()

    def from_elements(
        self,
        rows: Iterable[dict[str, Any]],
        schema: Schema | None = None,
        *,
        name: str | None = None,
    ) -> Table:
        materialized = [dict(row) for row in rows]
        resolved = schema or Schema.from_rows(materialized)
        table = Table(materialized, resolved, name)
        if name:
            self.register_table(name, table)
        return table

    def from_data_stream(
        self,
        stream: DataStream,
        schema: Schema | None = None,
        *,
        name: str | None = None,
    ) -> Table:
        # 教学实现会触发一次有界执行；生产 Flink 中 Table 与 DataStream 可以
        # 互相转换而不打断流式拓扑。
        values = stream.execute_and_collect()
        rows = [value if isinstance(value, dict) else {"f0": value} for value in values]
        table = Table(rows, schema or Schema.from_rows(rows), name)
        if name:
            self.register_table(name, table)
        return table

    def register_table(self, name: str, table: Table) -> None:
        if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("表名必须是合法标识符")
        self._tables[name] = table
        table.name = name

    def list_tables(self) -> list[str]:
        return sorted(self._tables)

    def sql(self, query: str) -> Table:
        query = query.strip().rstrip(";")
        if not query:
            raise ValueError("SQL 不能为空")
        connection = sqlite3.connect(":memory:")
        try:
            for name, table in self._tables.items():
                table._load(connection, name)
            cursor = connection.execute(_translate_sql(query))
            rows, schema = _fetch_rows(cursor)
            return Table(rows, schema)
        except sqlite3.Error as exc:
            raise ValueError(f"SQL 执行失败: {exc}") from exc
        finally:
            connection.close()

    def execute_sql(self, query: str) -> Table:
        """执行查询或 MiniFlink 支持的 DDL。

        支持的 DDL 子集：

        - ``CREATE [OR REPLACE] TABLE name AS SELECT ...``
        - ``CREATE [OR REPLACE] VIEW name AS SELECT ...``
        - ``CREATE TABLE/VIEW IF NOT EXISTS name AS SELECT ...``

        与 Flink 的 CTAS 不同，MiniFlink 没有 Catalog 和外部存储，DDL 会把
        查询结果物化成内存表（VIEW 也物化）。它只用于演示“建表 + 写入”
        的形状，不覆盖 schema 演进、原子建表和持续写入语义。
        """

        statement = query.strip().rstrip(";")
        match = re.fullmatch(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?(TABLE|VIEW)\s+"
            r"(?:IF\s+NOT\s+EXISTS\s+)?"
            r"([A-Za-z_][A-Za-z0-9_]*)\s+AS\s+(.+)",
            statement,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            name = match.group(2)
            if name in self._tables:
                # IF NOT EXISTS 与 OR REPLACE 都允许重复执行；没有这两个修饰符时
                # 与 Flink 一样报错，避免悄悄覆盖已有表。
                modifiers = statement[: match.start(2)].upper()
                if "IF NOT EXISTS" in modifiers:
                    return self._tables[name]
                if "OR REPLACE" not in modifiers:
                    raise ValueError(f"表已存在: {name}")
            table = self.sql(match.group(3).strip())
            self.register_table(name, table)
            return table
        if re.match(r"(?is)^CREATE\s+(TABLE|VIEW)\b", statement):
            raise ValueError(
                "MiniFlink 仅支持不带显式 schema/WITH 的 CREATE TABLE/VIEW ... AS SELECT；"
                "带列定义、WITH、主键或水位的 DDL 属于 Flink SQL 的生产能力"
            )
        return self.sql(statement)

    def explain_sql(self, query: str) -> str:
        connection = sqlite3.connect(":memory:")
        try:
            for name, table in self._tables.items():
                table._load(connection, name)
            rows = connection.execute("EXPLAIN QUERY PLAN " + _translate_sql(query)).fetchall()
            return "\n".join(" | ".join(str(part) for part in row) for row in rows)
        finally:
            connection.close()


def _tumble_start_expr(column: str, size: int) -> str:
    """SQLite 的整数除法向零截断；负数时间戳需要显式做 floor。

    显式 CAST 成 INTEGER，避免 REAL 列触发浮点除法而返回事件时间本身。
    """
    value = f"CAST({column} AS INTEGER)"
    return (
        f"(CASE WHEN {value} < 0 "
        f"THEN (({value} - {size} + 1) / {size}) "
        f"ELSE ({value} / {size}) END * {size})"
    )


def _translate_sql(query: str) -> str:
    """把少量 Flink 风格函数映射到 SQLite 表达式。"""

    def tumble_start(match: re.Match[str]) -> str:
        column, size = match.group(1).strip(), int(match.group(2))
        return _tumble_start_expr(column, size)

    query = re.sub(
        r"TUMBLE_START\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*(\d+)\s*\)",
        tumble_start,
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"TUMBLE_END\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*(\d+)\s*\)",
        lambda m: f"({_tumble_start_expr(m.group(1), int(m.group(2)))} + {m.group(2)})",
        query,
        flags=re.IGNORECASE,
    )
    return query


def _fetch_rows(cursor: sqlite3.Cursor) -> tuple[list[dict[str, Any]], Schema]:
    raw_names = [description[0] for description in cursor.description or []]
    # SQL 的 JOIN ... SELECT * 可能返回同名列。dict 会静默覆盖，因此显式
    # 生成唯一列名（id, id_1, ...），保证不丢数据。
    names: list[str] = []
    used: set[str] = set()
    for raw_name in raw_names:
        if raw_name not in used:
            used.add(raw_name)
            names.append(raw_name)
            continue
        suffix = 1
        candidate = f"{raw_name}_{suffix}"
        # 原始列里可能已经存在 id_1；必须继续递增，不能仅按原始列的出现次数
        # 生成后缀，否则仍会撞名并被 dict 静默覆盖。
        while candidate in used:
            suffix += 1
            candidate = f"{raw_name}_{suffix}"
        used.add(candidate)
        names.append(candidate)
    raw_rows = cursor.fetchall()
    rows = [dict(zip(names, values)) for values in raw_rows]
    if rows:
        schema = Schema.from_rows(rows)
    else:
        schema = Schema([Field(name, "STRING") for name in names])
    return rows, schema


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list, tuple)):
        return str(value)
    return value


def _infer_column_type(values: list[Any]) -> str:
    """按整列推断类型；混合列退化为 STRING，避免数值/文本比较反转。"""
    if not values:
        return "STRING"
    if all(isinstance(value, bool) for value in values):
        return "BOOLEAN"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return "BIGINT"
    if all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in values
    ):
        return "DOUBLE"
    if all(isinstance(value, bytes) for value in values):
        return "BYTES"
    return "STRING"
