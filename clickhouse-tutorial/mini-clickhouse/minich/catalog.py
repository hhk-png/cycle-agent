"""数据库目录 —— ``DatabaseCatalog``、``Database``、``Table``。

真实 ClickHouse 的这一层在 ``src/Interpreters/DatabaseCatalog.cpp``,
它做的事情比"用一个 dict 存表"多得多,而且每一条都有明确的理由:

.. code-block:: text

   职责                        为什么需要
   ─────────────────────────  ────────────────────────────────────────────
   表名 → IStorage 的映射       基础
   **UUID 索引**               重命名/移动表时,正在跑的查询仍然靠 UUID
                               找到那张表(名字变了不影响在飞的查询)
   生命周期管理(引用计数)       `DROP TABLE` 之后表不能立刻析构 ——
                               还有查询在用;真实 CH 用 `TableLockHolder`
   重命名锁(RENAME)            `RENAME TABLE` 与查询之间的互斥
   临时表/外部表                `CREATE TEMPORARY TABLE` 与会话绑定
   元数据持久化                 `metadata/<db>/<table>.sql` 落盘 + 启动时回放
   ─────────────────────────  ────────────────────────────────────────────

mini-clickhouse 实现前四条里的前三条(简化版),**不做**元数据落盘 ——
目录状态活在内存里,进程结束就没了(数据文件本身可以落盘,见
:mod:`minich.storages.merge_tree`)。这个取舍写在 README 的能力矩阵里。

.. note::

   **为什么要有 UUID?** 看这个真实场景:一条查询跑了 10 分钟,
   期间有人执行了 ``RENAME TABLE a TO b`` 然后又建了一张新的 ``a``。
   如果查询靠**名字**解析表,它后半段就会读到另一张表的数据 ——
   结果是一个"两半来自不同表"的垃圾。ClickHouse 的做法是:
   查询开始时把表解析成 UUID,之后一直用 UUID 查,
   重命名只是改"名字 → UUID"的映射。
   第 06 章有一个例子演示这个场景在 mini-clickhouse 里的行为。
"""

from __future__ import annotations

import threading
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .block import Block, ColumnWithTypeAndName
from .errors import ErrorCode, MiniChError, raise_error
from .types import DataType, get_data_type

__all__ = [
    "StorageID",
    "ColumnDefinition",
    "Table",
    "Database",
    "DatabaseCatalog",
]


@dataclass(frozen=True)
class StorageID:
    """一张表的**稳定标识** —— ``(database, table, uuid)``。

    ``uuid`` 是建表时生成的 128 位随机数。它有三个用处:

    1. 重命名/移动表时,在飞的查询靠它继续找到同一张表;
    2. 复制表时,各副本通过 UUID 确认"我们说的是同一张逻辑表";
    3. 磁盘上 ``data/<db>/<table>/`` 目录名里会带上 UUID 的一部分,
       这样删表重建不会撞上残留的旧数据。
    """

    database: str
    table: str
    uuid: str = ""

    def __str__(self) -> str:
        return f"{self.database}.{self.table}"

    def full_name(self) -> str:
        return f"{self.database}.{self.table}"

    def with_database(self, database: str) -> "StorageID":
        return StorageID(database, self.table, self.uuid)

    def with_table(self, table: str) -> "StorageID":
        return StorageID(self.database, table, self.uuid)


@dataclass
class ColumnDefinition:
    """列定义(存储层的,不是语法树的)。

    与 :class:`minich.sql.ast_.ColumnDefinition` 的区别:那个带的是
    **AST 表达式**,这个带的是**已解析的类型**与默认值。真实 ClickHouse
    的对应物是 ``ColumnDescription``(只有 name/type/default_desc),
    默认值表达式单独存在 ``defaults`` 里。

    三种默认值的语义差别(与真实 ClickHouse 完全一致):

    * ``DEFAULT`` —— 插入时该列**被省略**才用表达式;
    * ``MATERIALIZED`` —— 该列**永远**由表达式算,插入它直接报错;
    * ``ALIAS`` —— **不落盘**,查询时替换成表达式(列级宏)。
    """

    name: str
    type: DataType
    default_kind: Optional[str] = None      # DEFAULT / MATERIALIZED / ALIAS / EPHEMERAL
    default_expr_sql: Optional[str] = None  # 原始 SQL 文本(延迟解析)
    codec: Optional[str] = None
    ttl_sql: Optional[str] = None
    comment: Optional[str] = None

    def has_default(self) -> bool:
        return self.default_kind == "DEFAULT"

    def is_materialized(self) -> bool:
        return self.default_kind == "MATERIALIZED"

    def is_alias(self) -> bool:
        return self.default_kind == "ALIAS"

    def is_ephemeral(self) -> bool:
        return self.default_kind == "EPHEMERAL"

    def is_stored(self) -> bool:
        """是否真的**落盘**。

        .. warning::

           **``MATERIALIZED`` 列是落盘的。** 它的含义是"这一列的值
           永远由表达式算出、不允许用户显式插入",但算出来的值**会
           写进 part**。不落盘的只有两种:

           * ``ALIAS`` —— 查询时替换成表达式,part 里没有它;
           * ``EPHEMERAL`` —— 只作为 ``DEFAULT`` 的中间值,查询时也看不见。

           把 ``MATERIALIZED`` 当成不落盘是一个**会算错列裁剪**的 bug:
           读表时会以为不需要读它,于是 ``SELECT *`` 少一列。
        """
        return self.default_kind not in ("ALIAS", "EPHEMERAL")

    def to_cwtn(self) -> ColumnWithTypeAndName:
        """转成"空列 + 类型",用于构造 Block 的 header。"""
        return ColumnWithTypeAndName(self.name, self.type, self.type.create_column())

    def describe_row(self) -> dict:
        """``DESCRIBE TABLE`` 的一行。"""
        default_type = ""
        default_expression = ""
        if self.default_kind:
            default_type = self.default_kind
            default_expression = self.default_expr_sql or ""
        return {
            "name": self.name,
            "type": self.type.get_name(),
            "default_type": default_type,
            "default_expression": default_expression,
            "comment": self.comment or "",
            "codec_expression": self.codec or "",
            "ttl_expression": self.ttl_sql or "",
        }


class Table:
    """一张表 —— **元数据 + 存储引擎**的组合。

    真实 ClickHouse 里 ``StorageMergeTree`` 自己就继承了 ``IStorage``,
    "表"这个概念是引擎实例本身。mini-clickhouse 多包一层 :class:`Table`,
    把"元数据"(UUID、列定义、COMMENT)与"存储"(part 管理)分开 ——
    这样 ``system.tables`` 之类的元数据查询不需要碰存储引擎的内部状态。
    """

    def __init__(
        self,
        storage_id: StorageID,
        columns: Sequence[ColumnDefinition],
        engine: Any = None,
        metadata_sql: str = "",
        comment: str = "",
    ) -> None:
        self.storage_id = storage_id
        self.columns: List[ColumnDefinition] = list(columns)
        #: 存储引擎实例(``minich.storages`` 里的某个 ``IStorage``)
        self.engine = engine
        self.metadata_sql = metadata_sql
        self.comment = comment
        #: 建表时的**引擎参数**(order_by / partition_by / primary_key / TTL /
        #: SETTINGS…)。``CREATE TABLE t2 AS t1`` 与 ``SHOW CREATE TABLE``
        #: 都要用它 —— 没有它,克隆出来的表会丢掉排序键与分区键。
        self.engine_params: Dict[str, Any] = {}
        self.create_time = _now_string()

    # ── 元数据访问 ──────────────────────────────────────────────────────

    @property
    def database(self) -> str:
        return self.storage_id.database

    @property
    def name(self) -> str:
        return self.storage_id.table

    def full_name(self) -> str:
        return f"{self.database}.{self.name}"

    def get_column(self, name: str) -> Optional[ColumnDefinition]:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    def get_column_names(self) -> List[str]:
        return [c.name for c in self.columns]

    def stored_column_names(self) -> List[str]:
        """真正落盘的列 —— ``SELECT *`` 返回的是**全部**列(含 ALIAS)。"""
        return [c.name for c in self.columns if c.is_stored()]

    def has_column(self, name: str) -> bool:
        return self.get_column(name) is not None

    def header(self) -> Block:
        """返回一个**零行**的 Block —— 表的结构。

        这是引擎里最常用的一个东西:任何一次读取都从
        ``table.header()`` 开始,再按需要裁剪列。
        """
        return Block([c.to_cwtn() for c in self.columns])

    def describe_rows(self) -> List[dict]:
        return [c.describe_row() for c in self.columns]

    # ── 读写代理 ────────────────────────────────────────────────────────

    def read(self, ctx: Any, **kwargs: Any) -> Any:
        if self.engine is None:
            raise_error(
                ErrorCode.TABLE_IS_READ_ONLY,
                f"Table {self.full_name()} has no storage engine",
            )
        return self.engine.read(ctx, **kwargs)

    def write(self, ctx: Any, block: Block) -> None:
        if self.engine is None:
            raise_error(ErrorCode.TABLE_IS_READ_ONLY, f"Table {self.full_name()} is read-only")
        self.engine.write(ctx, block)

    def row_count(self) -> int:
        return self.engine.row_count() if self.engine is not None else 0

    def size_in_bytes(self) -> int:
        return self.engine.size_in_bytes() if self.engine is not None else 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Table {self.full_name()} engine={type(self.engine).__name__}>"


def _now_string() -> str:
    import datetime as _dt

    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """一个数据库 —— 表的命名空间。

    真实 ClickHouse 的数据库有引擎之分(``Atomic`` / ``Ordinary`` /
    ``Replicated`` / ``Lazy`` / ``SQLite`` / ``MySQL`` …),
    差别在于**元数据怎么存**与**DDL 怎么同步**:

    * ``Atomic``(默认,20.10 起):每张表一个 ``uuid``,DDL 原子生效,
      支持 `RENAME TABLE ... EXCHANGE` 与无锁并发读;
    * ``Ordinary``:老引擎,表名就是目录名,DDL 与查询互相加锁。

    mini-clickhouse 只有 ``Atomic`` 的语义(因为我们本来就有 UUID),
    但会在 ``system.databases`` 里如实报告引擎名。
    """

    def __init__(self, name: str, engine: str = "Atomic", comment: str = "") -> None:
        self.name = name
        self.engine = engine
        self.comment = comment
        self.tables: Dict[str, Table] = {}
        self.dictionaries: Dict[str, Any] = {}
        self.create_time = _now_string()

    def add_table(self, table: Table, if_not_exists: bool = False) -> None:
        if table.name in self.tables:
            if if_not_exists:
                return
            raise_error(
                ErrorCode.TABLE_ALREADY_EXISTS,
                f"Table {self.name}.{table.name} already exists",
            )
        self.tables[table.name] = table

    def get_table(self, name: str) -> Optional[Table]:
        return self.tables.get(name)

    def remove_table(self, name: str) -> Optional[Table]:
        return self.tables.pop(name, None)

    def table_names(self) -> List[str]:
        return sorted(self.tables)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Database {self.name} engine={self.engine} tables={len(self.tables)}>"


class DatabaseCatalog:
    """全局目录 —— 进程内唯一(真实 ClickHouse 也是单例)。

    **线程安全**:真实 ClickHouse 用 ``RWLock`` 保护"表名 → 表"的映射
    (读多写少,所以是读写锁)。mini-clickhouse 用一把普通
    ``threading.RLock`` —— 性能差一些,但语义正确,
    而且免去了读写锁最经典的 bug(写锁里再去拿读锁导致死锁)。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.databases: Dict[str, Database] = {}
        self._by_uuid: Dict[str, Table] = {}
        self.default_database = "default"
        #: 临时表按"会话 ID"分组 —— 真实 ClickHouse 里临时表绑定到 session
        self._temporary: Dict[str, Dict[str, Table]] = {}
        self.create_database("default")
        self.create_database("system")

    # ── 数据库 ──────────────────────────────────────────────────────────

    def create_database(
        self, name: str, engine: str = "Atomic", if_not_exists: bool = False,
        comment: str = "",
    ) -> Database:
        with self._lock:
            db = self.databases.get(name)
            if db is not None:
                if if_not_exists:
                    return db
                raise_error(ErrorCode.DATABASE_ALREADY_EXISTS, f"Database {name} already exists")
            db = Database(name, engine, comment)
            self.databases[name] = db
            return db

    def get_database(self, name: str) -> Database:
        with self._lock:
            db = self.databases.get(name)
            if db is None:
                raise_error(ErrorCode.UNKNOWN_DATABASE, f"Database {name} does not exist")
            return db

    def try_get_database(self, name: str) -> Optional[Database]:
        with self._lock:
            return self.databases.get(name)

    def has_database(self, name: str) -> bool:
        with self._lock:
            return name in self.databases

    def drop_database(self, name: str, if_exists: bool = False) -> None:
        with self._lock:
            db = self.databases.get(name)
            if db is None:
                if if_exists:
                    return
                raise_error(ErrorCode.UNKNOWN_DATABASE, f"Database {name} does not exist")
            if name in ("default", "system"):
                raise_error(
                    ErrorCode.UNKNOWN_DATABASE,
                    f"Cannot drop the system database {name}",
                )
            if db.tables:
                raise_error(
                    ErrorCode.UNKNOWN_DATABASE,
                    f"Database {name} is not empty ({len(db.tables)} tables)",
                )
            del self.databases[name]

    def database_names(self) -> List[str]:
        with self._lock:
            return sorted(self.databases)

    # ── 表 ──────────────────────────────────────────────────────────────

    def create_table(
        self,
        database: str,
        table: Table,
        if_not_exists: bool = False,
    ) -> Table:
        with self._lock:
            db = self.databases.get(database)
            if db is None:
                raise_error(ErrorCode.UNKNOWN_DATABASE, f"Database {database} does not exist")
            existing = db.get_table(table.name)
            if existing is not None:
                if if_not_exists:
                    return existing
                raise_error(
                    ErrorCode.TABLE_ALREADY_EXISTS,
                    f"Table {database}.{table.name} already exists",
                )
            table.storage_id = StorageID(database, table.name, table.storage_id.uuid or new_uuid())
            db.add_table(table)
            self._by_uuid[table.storage_id.uuid] = table
            return table

    def get_table(self, database: str, table: str) -> Table:
        with self._lock:
            db = self.databases.get(database)
            if db is None:
                raise_error(ErrorCode.UNKNOWN_DATABASE, f"Database {database} does not exist")
            t = db.get_table(table)
            if t is None:
                raise_error(
                    ErrorCode.UNKNOWN_TABLE,
                    f"Table {database}.{table} does not exist",
                )
            return t

    def try_get_table(self, database: str, table: str) -> Optional[Table]:
        with self._lock:
            db = self.databases.get(database)
            if db is None:
                return None
            return db.get_table(table)

    def get_table_by_uuid(self, table_uuid: str) -> Optional[Table]:
        with self._lock:
            return self._by_uuid.get(table_uuid)

    def has_table(self, database: str, table: str) -> bool:
        return self.try_get_table(database, table) is not None

    def drop_table(self, database: str, table: str, if_exists: bool = False) -> Optional[Table]:
        with self._lock:
            db = self.databases.get(database)
            if db is None:
                if if_exists:
                    return None
                raise_error(ErrorCode.UNKNOWN_DATABASE, f"Database {database} does not exist")
            t = db.remove_table(table)
            if t is None:
                if if_exists:
                    return None
                raise_error(ErrorCode.UNKNOWN_TABLE, f"Table {database}.{table} does not exist")
            self._by_uuid.pop(t.storage_id.uuid, None)
            if t.engine is not None:
                # 真实 ClickHouse 的 DROP 是"标记 + 后台清理",因为可能有
                # 在飞的查询还在读它的 part。这里直接 shutdown,
                # 但把差异写在 storage 的 docstring 里。
                try:
                    t.engine.shutdown()
                except Exception:  # pragma: no cover - 清理失败不该掩盖 DROP 本身
                    pass
            return t

    def rename_table(self, database: str, old: str, new_database: str, new: str) -> None:
        """重命名/移动表 —— **只改名字映射,UUID 与数据不动**。

        这正是"在飞的查询不受影响"的机制:已经有引用的查询拿着 UUID,
        ``get_table_by_uuid`` 仍然能拿到表;新查询用新名字。
        """
        with self._lock:
            src_db = self.databases.get(database)
            dst_db = self.databases.get(new_database)
            if src_db is None or dst_db is None:
                raise_error(ErrorCode.UNKNOWN_DATABASE, "Source or destination database missing")
            t = src_db.remove_table(old)
            if t is None:
                raise_error(ErrorCode.UNKNOWN_TABLE, f"Table {database}.{old} does not exist")
            if dst_db.get_table(new) is not None:
                src_db.add_table(t)
                raise_error(
                    ErrorCode.TABLE_ALREADY_EXISTS,
                    f"Table {new_database}.{new} already exists",
                )
            t.storage_id = StorageID(new_database, new, t.storage_id.uuid)
            dst_db.add_table(t)

    def exchange_tables(
        self, db1: str, t1: str, db2: str, t2: str
    ) -> None:
        """``EXCHANGE TABLES`` —— 原子交换两张表(Atomic 数据库特有的能力)。

        这个操作在"重建物化视图/回滚上线"时极有用:新表数据灌好之后
        一次交换就生效,中间没有任何"表不存在"的窗口。
        """
        with self._lock:
            d1 = self.databases.get(db1)
            d2 = self.databases.get(db2)
            if d1 is None or d2 is None:
                raise_error(ErrorCode.UNKNOWN_DATABASE, "Database does not exist")
            a = d1.get_table(t1)
            b = d2.get_table(t2)
            if a is None or b is None:
                raise_error(ErrorCode.UNKNOWN_TABLE, "Table does not exist")
            d1.tables[t1], d2.tables[t2] = b, a
            a.storage_id = StorageID(db2, t2, a.storage_id.uuid)
            b.storage_id = StorageID(db1, t1, b.storage_id.uuid)

    def all_tables(self) -> List[Table]:
        with self._lock:
            out: List[Table] = []
            for db in self.databases.values():
                out.extend(db.tables.values())
            return out

    def tables_of(self, database: str) -> List[Table]:
        with self._lock:
            db = self.databases.get(database)
            return list(db.tables.values()) if db else []

    # ── 临时表 ──────────────────────────────────────────────────────────

    def create_temporary_table(self, session_id: str, table: Table) -> Table:
        """临时表按**会话**隔离 —— 不同客户端可以各有一个 ``tmp``。"""
        with self._lock:
            bucket = self._temporary.setdefault(session_id, {})
            table.storage_id = StorageID("_temporary", table.name, new_uuid())
            bucket[table.name] = table
            return table

    def get_temporary_table(self, session_id: str, name: str) -> Optional[Table]:
        with self._lock:
            return self._temporary.get(session_id, {}).get(name)

    def drop_temporary_table(self, session_id: str, name: str) -> None:
        with self._lock:
            self._temporary.get(session_id, {}).pop(name, None)

    def temporary_tables(self, session_id: str) -> List[Table]:
        with self._lock:
            return list(self._temporary.get(session_id, {}).values())

    def drop_session(self, session_id: str) -> None:
        with self._lock:
            self._temporary.pop(session_id, None)

    # ── 统计 ────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "databases": len(self.databases),
                "tables": sum(len(db.tables) for db in self.databases.values()),
                "temporary_tables": sum(len(b) for b in self._temporary.values()),
            }

    def __repr__(self) -> str:  # pragma: no cover
        s = self.stats()
        return f"<DatabaseCatalog databases={s['databases']} tables={s['tables']}>"


def new_uuid() -> str:
    """生成一个 128 位 UUID 的**无连字符**十六进制形式。

    真实 ClickHouse 的 ``StorageID::createRandomUUID`` 用的是
    伪随机数(不是密码学安全的),因为我们只需要"不撞"。
    mini-clickhouse 用 ``uuid4`` —— 更好,也更慢,但我们不敏感。
    """
    return _uuid.uuid4().hex
