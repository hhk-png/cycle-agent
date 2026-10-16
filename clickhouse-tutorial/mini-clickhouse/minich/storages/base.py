"""存储引擎接口 —— ``IStorage``。

表引擎是 ClickHouse 最有辨识度的设计。理解它的关键是一句话:

    **引擎不是"数据类型",而是"这张表的读写语义"**。

.. code-block:: text

   CREATE TABLE t (...) ENGINE = MergeTree ORDER BY x
                                  ^^^^^^^^^^^^^^^^^^^^^^^^^
                                  这一行决定了:
                                    * 数据存在哪里(内存 / 磁盘 / 远端)
                                    * 怎么读(全扫 / 索引 / 预聚合)
                                    * 能不能改(追加 / 覆盖 / 只读)
                                    * 后台做什么(merge / TTL / 复制 / fetch)
                                    * 支不支持主键、分区、排序键

所以同一个 ``SELECT`` 在不同引擎上的**执行计划完全不同**,而
优化器必须知道这件事 —— 这就是为什么 ``IStorage`` 有一个
:meth:`IStorage.read` 而不是"通用的表扫描"。

mini-clickhouse 实现的引擎族(完整清单见 README 的能力矩阵):

===========================  ==========================================================
引擎                          语义 / 用途
===========================  ==========================================================
``MergeTree`` 及其家族         **核心**。写追加、后台 merge、稀疏索引、分区
``Memory``                    数据在内存,重启即失,**没有**索引
``Log`` / ``TinyLog`` /       最简单的磁盘引擎:一个文件一列,追加写
``StripeLog``                 没有索引、不能并行读
``Null``                      丢弃一切写入,读取永远为空(计时压测用)
``Set``                       只存主键的集合(用于 ``IN`` 的半连接优化)
``Join``                      内存里的哈希表(用于 ``JOIN`` 的预构建)
``Buffer``                    在内存里攒批,定期刷入目标表
``Merge``                     把多张同构表"合并"成一张虚拟表
``File`` / ``URL``           从文件/网络读,写入即覆盖
``Values``                    把 ``VALUES`` 子句当成表
``View`` / ``MaterializedView`` 视图
``Distributed``               分片代理(第 21 章)
===========================  ==========================================================

.. note::

   **引擎的选择在 ClickHouse 里比在 MySQL 里重要一个量级。**
   MySQL 里换引擎主要是"要不要事务";ClickHouse 里换引擎意味着
   "能不能按时间范围裁剪分区""能不能去重""能不能做增量聚合"——
   这些直接决定了你的查询快十倍还是慢十倍。第 04 章整章讲这个。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..catalog import StorageID, Table
from ..errors import ErrorCode, MiniChError, raise_error
from ..types import DataType

__all__ = [
    "IStorage",
    "ReadContext",
    "WriteContext",
    "StorageFactory",
    "register_storage_engine",
    "create_storage",
    "storage_engine_exists",
    "registered_storage_engines",
    "StorageFeatures",
]


@dataclass
class StorageFeatures:
    """一个引擎"支持什么"的**声明**。

    这个结构存在的价值是让优化器**不必用 isinstance 判断引擎类型**。
    真实 ClickHouse 里对应的是一堆虚函数
    (``supportsPrimaryKey``、``supportsSamplingByParallelReadingMerged``…),
    我们用一张小表代替 —— 语义一样,读起来更直观。

    第 22 章会展示:一个优化规则如果误判了引擎能力
    (比如对 ``Log`` 表下推主键条件),会产生**错误结果**而不是慢查询。
    """

    primary_key: bool = False
    """支持稀疏主键索引(能把 ``WHERE`` 转成 key range)"""

    partitions: bool = False
    """支持分区(``PARTITION BY``),能按分区裁剪"""

    sorting_key: bool = False
    """支持排序键,数据在 part 内有序"""

    parallel_read: bool = True
    """支持多线程并行读"""

    skip_indexes: bool = False
    """支持数据跳过索引(skip index)"""

    ttl: bool = False
    """支持 TTL(按时间删除/移动/重压缩)"""

    mutations: bool = False
    """支持 ``ALTER ... UPDATE/DELETE``"""

    merges: bool = False
    """有后台 merge"""

    replication: bool = False
    """支持复制(``Replicated*``)"""

    append_only: bool = False
    """只允许追加,不允许改已有数据(``Log`` 家族)"""

    sampling: bool = False
    """支持 ``SAMPLE`` 子句"""

    projections: bool = False
    """支持投影"""

    def describe(self) -> str:
        """``system.table_engines``/``SHOW ENGINES`` 里的那一行。"""
        flags = []
        if self.primary_key:
            flags.append("primary_key")
        if self.partitions:
            flags.append("partitions")
        if self.sorting_key:
            flags.append("sorting_key")
        if self.replication:
            flags.append("replication")
        if self.mutations:
            flags.append("mutations")
        if self.ttl:
            flags.append("ttl")
        return ", ".join(flags) if flags else "-"


@dataclass
class ReadContext:
    """一次读取的**全部输入** —— 引擎能看到的所有信息。

    .. code-block:: text

       InterpreterSelectQuery
            │  ① 列裁剪:只保留 SELECT/WHERE/ORDER BY 真正用到的列
            │  ② 主键分析:把 WHERE 变成 key range(引擎相关)
            │  ③ 分区裁剪:算出一个分区集合
            ▼
       ReadContext(columns=[...], key_analysis=..., limit=..., final=False)
            │
            ▼
       IStorage.read()  →  Iterator[Chunk]

    真实 ClickHouse 传给 ``IStorage::read`` 的是一整棵
    ``QueryPlan`` 与 ``SelectQueryInfo``(里面装着 PreparedSets、
    KeyCondition、分区表达式、采样等)。mini-clickhouse 把这些
    压缩成一个 dataclass —— 信息量少一些,但结构对得上。
    """

    ctx: Any = None
    #: 要读的列名(None = 全部)。**列裁剪在此生效** ——
    #: MergeTree 会据此决定读哪些 ``.bin`` 文件。
    columns: Optional[List[str]] = None
    #: 引擎自己产出的"主键分析结果"(MergeTree 是 KeyCondition,其它引擎为 None)
    key_analysis: Any = None
    #: 分区裁剪结果(分区 ID 的集合;None = 不裁剪)
    partition_ids: Optional[set] = None
    #: ``LIMIT`` 下推 —— 引擎可以提前停止读取
    limit: Optional[int] = None
    #: ``FROM t FINAL``
    final: bool = False
    #: ``SAMPLE`` 子句
    sample: Any = None
    #: **查询的过滤条件**(AST)。存储引擎靠它做分区裁剪 / 主键裁剪 /
    #: 跳数索引裁剪 —— 这是"索引为什么有用"在 SQL 路径上的唯一入口。
    #: 真实 ClickHouse 传的是 ``SelectQueryInfo``(里面装着已经分析好的
    #: ``KeyCondition``),我们传原始 AST,由引擎自己分析。
    where: Any = None
    prewhere: Any = None
    #: 查询级设置(覆盖会话设置)
    settings: Any = None
    #: 查询 ID / 统计信息的挂载点
    query_info: Any = None

    def wanted_columns(self, available: Sequence[str]) -> List[str]:
        """把"想要的列"与"表里有的列"求交(保持表定义里的顺序)。

        这一步看着多余,其实是**列裁剪的最后一道闸门**:
        ``SELECT * FROM t`` 展开后可能请求了一个 ``ALIAS`` 列,
        而 ALIAS 列不落盘 —— 引擎不该去找它的文件。
        """
        if self.columns is None:
            return list(available)
        wanted = set(self.columns)
        return [c for c in available if c in wanted]


class WriteContext:
    """一次写入的上下文。"""

    __slots__ = ("ctx", "query_id", "settings")

    def __init__(self, ctx: Any = None, query_id: str = "", settings: Any = None) -> None:
        self.ctx = ctx
        self.query_id = query_id
        self.settings = settings


class IStorage(ABC):
    """存储引擎基类。

    子类至少要实现 :meth:`read`、:meth:`write` 和 :attr:`features`。
    其它方法有合理默认(不支持就抛 ``NOT_IMPLEMENTED``)——
    这一点与真实 ClickHouse 一致:``IStorage`` 里有几十个虚函数,
    大多数引擎只覆盖其中一小部分,其余的默认实现直接抛
    ``NOT_IMPLEMENTED``。
    """

    #: 引擎名 —— ``system.tables.engine`` 显示的就是它
    engine_name: str = "Unknown"

    #: 能力声明(子类覆盖)
    features: StorageFeatures = StorageFeatures()

    def __init__(
        self,
        table: Table,
        storage_id: StorageID,
        context: Any = None,
        engine_args: Optional[Sequence[Any]] = None,
    ) -> None:
        self.table = table
        self.storage_id = storage_id
        self.context = context
        self.engine_args: List[Any] = list(engine_args or [])
        self._lock = threading.RLock()
        self._shutdown = False

    # ── 身份 ────────────────────────────────────────────────────────────

    def get_storage_id(self) -> StorageID:
        return self.storage_id

    def get_name(self) -> str:
        """引擎的完整名字 —— 带参数,例如 ``MergeTree`` / ``Distributed('c', 'd', 't')``。"""
        if self.engine_args:
            params = ", ".join(_quote_engine_arg(a) for a in self.engine_args)
            return f"{self.engine_name}({params})"
        return self.engine_name

    def full_table_name(self) -> str:
        return self.storage_id.full_name()

    # ── 读写(必须实现)──────────────────────────────────────────────────

    @abstractmethod
    def read(self, ctx: Any, **kwargs: Any) -> Iterator[Chunk]:
        """读整表 —— **必须**产出 ``Chunk``,而不是 Block。

        .. warning::

           产出的每个 chunk 的列**必须**与 :meth:`get_columns`
           (或 :meth:`ReadContext.columns` 裁剪后的子集)**同序同名同类型**。
           引擎层最容易出的 bug 就是这里:某个 chunk 的列顺序变了,
           下游按位置取列就整整齐齐地错位 —— 而且**不报错**,
           只是结果全错。第 10 章有一个真实例子。
        """

    @abstractmethod
    def write(self, ctx: Any, block: Block) -> None:
        """写入一个 Block(通常来自 ``INSERT``)。

        真实 ClickHouse 的写路径是异步的:``INSERT`` 先写
        "临时 part"(``tmp_insert_*``),写满或提交时再原子改名。
        这样**读取方永远看不到半个 part**。mini-clickhouse 在
        MergeTree 里实现了同样的两阶段提交(见第 05 章)。
        """

    # ── 元信息(有默认实现)────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        """表结构(空列)。默认从 :class:`minich.catalog.Table` 取。"""
        return [c.to_cwtn() for c in self.table.columns]

    def row_count(self) -> int:
        """总行数 —— 用于 ``SELECT count()`` 的**平凡优化**。

        .. note::

           ClickHouse 的 ``optimize_trivial_count_query``(默认开)会把
           没有 ``WHERE`` 的 ``count()`` 变成"读元数据里的行数",
           于是 ``SELECT count() FROM huge_table`` 是**毫秒级**的。
           这就是 :meth:`row_count` 存在的意义 —— 它是一个
           **可以走捷径的接口**,而不是"扫一遍数出来"。
        """
        return 0

    def size_in_bytes(self) -> int:
        """磁盘上的字节数。"""
        return 0

    def part_count(self) -> int:
        return 0

    def get_partition_key_description(self) -> str:
        return ""

    def get_sorting_key_description(self) -> str:
        return ""

    def get_primary_key_description(self) -> str:
        return ""

    # ── 生命周期 ────────────────────────────────────────────────────────

    def startup(self) -> None:
        """引擎开始工作(启动后台线程/加载 part)。"""

    def shutdown(self) -> None:
        """引擎停止(``DROP TABLE`` 或进程退出)。

        **必须幂等** —— 真实 ClickHouse 里 ``shutdown`` 被调用两次
        是常态(显式 DROP + 析构)。
        """
        self._shutdown = True

    def is_shutdown(self) -> bool:
        return self._shutdown

    def flush(self) -> None:
        """把内存里的缓冲落到"持久"位置(对 Memory 引擎是 no-op)。"""

    # ── 变更(可选)─────────────────────────────────────────────────────

    def truncate(self) -> None:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Engine {self.engine_name} does not support TRUNCATE",
        )

    def alter_add_column(self, column: Any, after: Optional[str] = None) -> None:
        self.table.columns.append(column)
        if after is not None:
            self._reorder_column(column.name, after)

    def _reorder_column(self, name: str, after: str) -> None:
        """把 ``name`` 挪到 ``after`` 后面(``ALTER ... ADD COLUMN ... AFTER x``)。

        .. note::

           真实 ClickHouse 里列顺序体现在 ``columns.txt`` 里,
           而**已有 part 的物理列顺序不变**(新列只在读的时候补默认值)。
           所以"改列顺序"是**元数据操作**,不重写数据 ——
           这也是为什么 ``ALTER TABLE ... MODIFY COLUMN`` 改顺序几乎瞬间完成。
        """
        cols = self.table.columns
        idx = next((i for i, c in enumerate(cols) if c.name == name), -1)
        if idx < 0:
            return
        col = cols.pop(idx)
        if after == "__first__":
            cols.insert(0, col)
            return
        target = next((i for i, c in enumerate(cols) if c.name == after), -1)
        cols.insert(target + 1 if target >= 0 else len(cols), col)

    def alter_drop_column(self, name: str) -> None:
        self.table.columns = [c for c in self.table.columns if c.name != name]

    def alter_modify_column(self, name: str, new_type: DataType, default_expr: Any = None) -> None:
        col = self.table.get_column(name)
        if col is None:
            raise_error(ErrorCode.NO_SUCH_COLUMN_IN_TABLE, f"Column {name} does not exist")
        col.type = new_type
        if default_expr is not None:
            col.default_expr_sql = default_expr

    def alter_rename_column(self, old: str, new: str) -> None:
        col = self.table.get_column(old)
        if col is None:
            raise_error(ErrorCode.NO_SUCH_COLUMN_IN_TABLE, f"Column {old} does not exist")
        if self.table.get_column(new) is not None:
            raise_error(ErrorCode.DUPLICATE_COLUMN, f"Column {new} already exists")
        col.name = new

    def mutate(self, command: Any) -> str:
        """``ALTER ... UPDATE/DELETE`` —— 返回 mutation id。"""
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Engine {self.engine_name} does not support mutations",
        )

    def optimize(self, final: bool = False, deduplicate: bool = False,
                 partition: Any = None) -> None:
        """``OPTIMIZE TABLE`` —— 强制合并。"""

    def check(self) -> str:
        """``CHECK TABLE`` —— 返回诊断文本(空字符串表示没问题)。"""
        return ""

    def describe_stats(self) -> Dict[str, Any]:
        """引擎自己的统计(给 ``system.parts``、``EXPLAIN`` 用)。"""
        return {}

    # ── 查询分析(引擎相关)─────────────────────────────────────────────

    def analyze_where(self, expr: Any, ctx: Any) -> Any:
        """把 ``WHERE`` 表达式分析成"引擎能用的裁剪信息"。

        MergeTree 会把它变成 :class:`KeyCondition`(主键区间)+
        分区裁剪;``Memory`` 返回 ``None``(它没有索引可言)。
        优化器据此决定"这个条件下能不能跳过一些 granule"。

        默认实现返回 ``None`` —— "我没有能力从 WHERE 里榨出任何东西"。
        """
        return None

    def supports_where_pushdown(self) -> bool:
        return False


def _quote_engine_arg(arg: Any) -> str:
    if isinstance(arg, str) and not arg.replace("_", "").isalnum():
        return f"'{arg}'"
    return str(arg)


# ══════════════════════════════════════════════════════════════════════════
# 引擎注册表
# ══════════════════════════════════════════════════════════════════════════

#: 引擎名 → 工厂。工厂签名 ``(table, storage_id, context, engine_args, params) -> IStorage``
StorageFactory = Callable[..., IStorage]

_REGISTRY: Dict[str, StorageFactory] = {}

#: 引擎名(小写) → (是否 deprecated, 说明)。``system.table_engines`` 用。
_ENGINE_INFO: Dict[str, Tuple[bool, str]] = {}

#: 小写键 → **注册时的原始大小写**。展示用(``MergeTree`` 而不是 ``mergetree``)。
_DISPLAY_NAMES: Dict[str, str] = {}


def register_storage_engine(
    name: str,
    factory: StorageFactory,
    *,
    deprecated: bool = False,
    description: str = "",
    aliases: Sequence[str] = (),
) -> None:
    """注册一个表引擎。名字是**大小写不敏感**的(查表用小写键),
    但**展示**时用注册时的原始大小写 —— ``system.table_engines`` 与
    ``SHOW ENGINES`` 里必须显示 ``MergeTree`` 而不是 ``mergetree``。
    """
    key = name.lower()
    _REGISTRY[key] = factory
    _ENGINE_INFO[key] = (deprecated, description)
    _DISPLAY_NAMES[key] = name
    for alias in aliases:
        _REGISTRY[alias.lower()] = factory
        _ENGINE_INFO.setdefault(alias.lower(), (deprecated, description))
        _DISPLAY_NAMES.setdefault(alias.lower(), alias)


def storage_engine_exists(name: str) -> bool:
    return name.lower() in _REGISTRY


def registered_storage_engines() -> List[Tuple[str, bool, str]]:
    """``(引擎名, 是否弃用, 说明)`` —— ``system.table_engines`` 的数据源。

    返回的是**注册时的原始大小写**(``MergeTree``,不是 ``mergetree``)。
    """
    return sorted(
        ((_DISPLAY_NAMES.get(k, k), v[0], v[1]) for k, v in _ENGINE_INFO.items()),
        key=lambda t: t[0],
    )


def create_storage(
    table: Table,
    engine_name: str,
    engine_args: Optional[Sequence[Any]] = None,
    context: Any = None,
    params: Optional[Dict[str, Any]] = None,
    storage_id: Optional[StorageID] = None,
) -> IStorage:
    """按引擎名建一个存储实例 —— ``CREATE TABLE`` 的最后一步。

    ``params`` 是"引擎相关的建表参数",例如 MergeTree 的
    ``order_by`` / ``partition_by`` / ``primary_key`` / ``settings``。
    真实 ClickHouse 通过 ``EngineFactory`` + ``Arguments``(一个
    ``ASTs`` 向量)传递同样的信息。
    """
    key = engine_name.lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise_error(
            ErrorCode.UNKNOWN_STORAGE,
            f"Unknown table engine {engine_name}. "
            f"Known engines: {', '.join(sorted({k for k in _REGISTRY}))}",
        )
    return factory(
        table,
        storage_id if storage_id is not None else table.storage_id,
        context,
        list(engine_args or []),
        dict(params or {}),
    )


def storage_engine_display_name(engine_name: str) -> str:
    """把用户写的引擎名规范成标准大小写(``mergetree`` → ``MergeTree``)。

    真实 ClickHouse 也这么做:``CREATE TABLE ... ENGINE = mergetree``
    能建出来,但 ``system.tables.engine`` 显示的是 ``MergeTree``。
    引擎名的大小写不敏感是 SQL 层的约定,而引擎自己有一个规范名。
    """
    return _DISPLAY_NAMES.get(engine_name.lower(), engine_name)
