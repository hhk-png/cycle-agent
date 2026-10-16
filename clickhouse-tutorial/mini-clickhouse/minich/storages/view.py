"""视图引擎 —— 普通视图 ``View`` 与物化视图 ``MaterializedView``。

两者只有一字之差,语义却**完全不同**。这是 ClickHouse 里最需要牢记的一对
概念,所以把它们的差别放在一张表里:

.. code-block:: text

                        View(普通视图)              MaterializedView(物化视图)
   ───────────────────  ─────────────────────────  ──────────────────────────────
   数据                 **不存**(只是个宏)         存(有一张真实的目标表)
   查询时               每次都**重跑子查询**         只读已物化的结果
   写入时               什么都不发生                **插入触发器**:往源表插数据,
                                                    自动把新行加工后写进目标表
   能不能 POPULATE      无关(它不存数据)          ``POPULATE`` 时才回填**已有**数据
   典型用途             简化/复用查询逻辑           预聚合、实时 ETL、过滤转发

普通视图
========

.. code-block:: text

   CREATE VIEW v AS SELECT a, b+1 AS c FROM t;
   SELECT * FROM v WHERE c > 10;
   -- 等价于 SELECT a, b+1 AS c FROM t WHERE b+1 > 10;  ——每次重跑

引擎**不**解析、**不**执行 ``SELECT`` —— 那是解释器的活。所以本引擎只保留
一个回调 ``subquery_executor``(解释器注入),``read`` 时把子查询表达式与
当前上下文交给它。回调没注入时,``read`` 抛 ``NOT_IMPLEMENTED`` 并给出
清楚的说明 —— 因为"没有解释器,视图就只是一段 AST"。

物化视图
========

.. code-block:: text

   CREATE MATERIALIZED VIEW mv TO daily ENGINE = SummingMergeTree
   AS SELECT toDate(ts) AS d, count() AS n FROM raw GROUP BY d;

   INSERT INTO raw ...         -- ← 只有 **这一句之后** 插入的行才会被 mv 看到
   -- mv 像一个触发器:raw 的每次 INSERT,都触发一次这个聚合,写进 daily

**这是一个"插入触发器",不是一个"刷新任务"。** 三条必须记住的后果:

1. **视图创建之前插入的行,永远不会被看到** —— 除非建视图时写了 ``POPULATE``
   (一次性回填)。这就是"我建了 MV 怎么数据对不上"的头号根因;
2. ``POPULATE`` 有个经典竞态:回填期间插入的行**可能**漏掉或**可能**重复,
   所以真实 ClickHouse 的文档建议在低峰期建、或干脆用 ``TO`` + 手动回填;
3. ``TO db.dest`` 形式的物化视图把结果写进一张**已存在的**目标表,
   于是"恰好一次"成为可能(源表写成功后,目标表的写入也是确定性的),
   这也是它被用来做**增量聚合**的原因。

.. note::

   真实 ClickHouse 的 ``StorageMaterializedView`` 与 ``StorageView``
   都在 ``src/Storages/`` 下;物化视图还有一个"内部表"(当没写 ``TO`` 时)
   和一个后台"清理线程"(``DROP`` 时清内部表)。mini-clickhouse 保留
   ``push`` / ``populate`` 这两个**语义核心**接口,把线程与内部表的细节
   省略掉,并在报告里标出。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterator, List, Optional

from ..block import Block, Chunk, ColumnWithTypeAndName, chunk_from_block
from ..errors import ErrorCode, raise_error
from ._common import (
    _align_block,
    _check_chunk,
    _requested_columns,
    _stored_columns,
)
from .base import IStorage, StorageFeatures, register_storage_engine

__all__ = ["StorageView", "StorageMaterializedView"]


def _as_block(chunk_or_block: Any) -> Block:
    """视图回调可能产出 Block,也可能产出 Chunk —— 统一成 Block。"""
    if isinstance(chunk_or_block, Chunk):
        return chunk_or_block.get_block_with_data()
    return chunk_or_block


# ══════════════════════════════════════════════════════════════════════════
# 普通视图
# ══════════════════════════════════════════════════════════════════════════


class StorageView(IStorage):
    """普通(非物化)视图 —— 见模块文档。"""

    engine_name = "View"

    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        #: 子查询 AST —— 由解释器在建视图时挂上。
        self.select: Any = self.params.get("select")
        #: 子查询执行回调:``executor(select_ast, ctx) -> Iterator[Block]``。
        self.subquery_executor: Optional[Callable[[Any, Any], Iterator[Block]]] = \
            self.params.get("subquery_executor")

    def attach_select(self, select: Any) -> None:
        """挂上子查询 AST(解释器调用)。"""
        self.select = select

    def set_subquery_executor(self, executor: Callable[[Any, Any], Iterator[Block]]) -> None:
        """注入子查询执行器(解释器调用) —— 这是视图唯一的信息来源。"""
        self.subquery_executor = executor

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        return _stored_columns(self.table)

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        if self.subquery_executor is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"View {self.full_table_name()} cannot be read: no subquery executor "
                f"is attached. A plain view is not materialized — the interpreter "
                f"must call set_subquery_executor() to run its stored SELECT.",
            )
        expected = _requested_columns(ctx, self.get_columns())
        for block in self.subquery_executor(self.select, ctx):
            block = _as_block(block)
            if expected:
                # 视图列可能比表定义少/多 —— 按名字对齐。
                columns = []
                for cwt in expected:
                    src = block.try_get_by_name(cwt.name)
                    if src is None:
                        column = cwt.type.create_column()
                        column.append_many_default(block.rows())
                    else:
                        column = src.column
                    columns.append(column)
                hdr = [c.clone_empty() for c in expected]
                chunk = Chunk(Block(hdr), columns, columns[0].size() if columns else 0)
                _check_chunk(chunk, expected)
                yield chunk
            else:
                yield chunk_from_block(block)

    def write(self, ctx: Any, block: Block) -> None:
        raise_error(
            ErrorCode.TABLE_IS_READ_ONLY,
            f"Table {self.full_table_name()} is read-only (it is a view)",
        )

    def row_count(self) -> Optional[int]:
        """视图**没有**可以走元数据的行数 —— 返回 ``None`` 表示"我不知道"。

        返回 ``0`` 会是一个**静默的错误答案**:``SELECT count() FROM v``
        会被平凡 count 优化接走(它只问 ``row_count()``,不读数据),
        于是空 / 非空视图都报 0。``None`` 让那条捷径**放弃**,
        交回给解释器老老实实跑子查询(见
        ``InterpreterSelectQuery._try_trivial_count`` 对 ``None`` 的处理)。
        这与真实 ClickHouse 里 ``StorageView::totalRows`` 不提供行数一致。
        """
        return None

    def shutdown(self) -> None:
        self._shutdown = True


# ══════════════════════════════════════════════════════════════════════════
# 物化视图
# ══════════════════════════════════════════════════════════════════════════


class StorageMaterializedView(IStorage):
    """物化视图 —— 一个**插入触发器**(见模块文档)。"""

    engine_name = "MaterializedView"

    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        self.select: Any = self.params.get("select")
        self.subquery_executor: Optional[Callable[[Any, Any], Iterator[Block]]] = \
            self.params.get("subquery_executor")
        #: ``TO db.t`` 的目标表名(没有 TO 时用视图自己的内部表)。
        self.to_database: Optional[str] = self.params.get("to_database")
        self.to_table: Optional[str] = self.params.get("to_table")
        #: ``TO`` 目标(Table 对象,惰性解析 / 显式注入)。
        self._target: Any = self.params.get("target")
        #: **源表** —— ``SELECT … FROM src`` 里的那张 src。解释器据此把
        #: "往 src 插入"路由到本视图的 :meth:`push`(这就是"插入触发器"
        #: 能被找到的方式,真实 ClickHouse 是登记在源表的读路径上)。
        self.source_database: Optional[str] = self.params.get("source_database")
        self.source_table: Optional[str] = self.params.get("source_table")
        #: 没有 TO 时的内部数据(视图自己的内部表)。
        #:
        #: .. warning::
        #:
        #:    真实 ClickHouse 会给没写 ``TO`` 的物化视图**建一张内部表**,
        #:    引擎取自 ``CREATE MATERIALIZED VIEW … ENGINE = …``,于是它
        #:    与普通表一样落盘、一样能 ``SHOW CREATE`` 出来。
        #:    mini-clickhouse **省略了这张内部表**:``_owned`` 只是一个
        #:    **进程内的内存缓冲**,``ENGINE =`` 子句被忽略,重启即丢。
        #:    所以"没有 TO 的物化视图"在本实现里只能当**同进程内的缓存**
        #:    用 —— 需要可查询、可持久化的落地表时,**必须写 ``TO``**。
        self._owned: List[Block] = []
        #: ``POPULATE`` 是一次性的 —— 用这个标记保证幂等。
        self._populated = False

    # ── 挂载(解释器调用)─────────────────────────────────────────────

    def attach_select(self, select: Any) -> None:
        self.select = select

    def set_subquery_executor(self, executor: Callable[[Any, Any], Iterator[Block]]) -> None:
        self.subquery_executor = executor

    def set_target(self, table: Any) -> None:
        """指定 ``TO`` 目标表(解释器解析完 ``TO db.t`` 后调用)。"""
        self._target = table

    def set_source(self, database: Optional[str], table: Optional[str]) -> None:
        """登记**源表**(解释器建视图时调用)。

        这一行信息就是"插入触发器"的注册表项:解释器在 ``INSERT`` 之后
        遍历目录里的物化视图,能对上的就让它们 :meth:`push`。
        """
        self.source_database = database
        self.source_table = table

    def matches_source(self, table: Any) -> bool:
        """``table`` 是不是本视图的源表 —— 解释器 ``INSERT`` 时用它筛选。

        源表名由 ``SELECT … FROM src`` 里的 ``src`` 决定(解释器在
        :meth:`Context.execute` 的 ``CREATE MATERIALIZED VIEW`` 路径上
        通过 :meth:`set_source` 登记)。没登记源表(例如 ``FROM`` 写成
        JOIN/子查询)的视图**永远不会**被 INSERT 触发 —— 这是本实现的
        能力边界,不是静默的宽容。
        """
        if self.source_table is None:
            return False
        if self.source_database is not None:
            return table.database == self.source_database and table.name == self.source_table
        return table.name == self.source_table

    def target_table(self) -> Any:
        if self._target is not None:
            return self._target
        catalog = getattr(self.context, "catalog", None)
        if catalog is None or self.to_database is None or self.to_table is None:
            return None
        self._target = catalog.try_get_table(self.to_database, self.to_table)
        return self._target

    def has_inner_table(self) -> bool:
        return self.target_table() is None

    # ── 元信息 ──────────────────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        target = self.target_table()
        if target is not None:
            return [c.to_cwtn() for c in target.columns if c.is_stored()]
        return _stored_columns(self.table)

    def get_name(self) -> str:
        if self.to_database and self.to_table:
            return f"MaterializedView -> {self.to_database}.{self.to_table}"
        return "MaterializedView"

    def is_populated(self) -> bool:
        return self._populated

    def describe_stats(self) -> Dict[str, Any]:
        return {
            "to_database": self.to_database or "",
            "to_table": self.to_table or "",
            "source": (
                f"{self.source_database}.{self.source_table}"
                if self.source_table is not None else ""
            ),
            "populated": self._populated,
            "owned_blocks": len(self._owned),
        }

    # ── 插入触发器 ──────────────────────────────────────────────────────

    def push(self, ctx: Any = None, block: Any = None) -> None:
        """把源表新插入的一批行**推送**进目标表 —— 解释器的 INSERT 会调它。

        这就是"物化视图是插入触发器"的字面落地:源表 ``write`` 成功后,
        解释器把同一个 block(通常先经过视图的 SELECT 加工)交给 ``push``。

        为了对接不同的调用风格,这个方法同时接受 ``push(block)`` 与
        ``push(ctx, block)`` 两种写法。
        """
        if block is None:
            block, ctx = ctx, self.context
        block = _as_block(block)
        if block.rows() == 0:
            return
        target = self.target_table()
        if target is None or target.engine is None:
            # 没有 TO —— 落到视图自己的"内部表"(这里就是一个内存缓冲)。
            self._owned.append(block.clone_with_columns())
            return
        stored = [c for c in target.columns if c.is_stored()]
        aligned = _align_block(block, stored, strict=False,
                              table_name=target.full_name())
        target.engine.write(ctx, aligned)

    def populate(
        self,
        ctx: Any = None,
        reader: Optional[Callable[[Any], Iterator[Any]]] = None,
    ) -> int:
        """``POPULATE`` —— 一次性地把**已有数据**回填进目标表。

        返回回填的行数。**幂等**:已经回填过就直接返回 0(真实 CH 里
        ``POPULATE`` 是建视图时的一个开关,不会重复触发)。

        ``reader`` 省略时用注入的 ``subquery_executor`` 跑视图自己的 SELECT。
        """
        if self._populated:
            return 0
        if reader is None:
            if self.subquery_executor is None:
                raise_error(
                    ErrorCode.NOT_IMPLEMENTED,
                    f"MaterializedView {self.full_table_name()} cannot POPULATE: no "
                    f"subquery executor is attached to read the pre-existing rows.",
                )
            reader = lambda c: self.subquery_executor(self.select, c)  # noqa: E731
        total = 0
        for item in reader(ctx):
            block = _as_block(item)
            self.push(ctx, block)
            total += block.rows()
        self._populated = True
        return total

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        """读物化视图 = 读它的**目标表**(有 ``TO`` 时);没有 ``TO`` 就读内部数据。"""
        target = self.target_table()
        if target is not None and target.engine is not None:
            yield from target.engine.read(ctx)
            return
        expected = _requested_columns(ctx, self.get_columns())
        for block in self._owned:
            if not expected:
                yield chunk_from_block(block)
                continue
            columns = []
            for cwt in expected:
                src = block.try_get_by_name(cwt.name)
                if src is None:
                    column = cwt.type.create_column()
                    column.append_many_default(block.rows())
                else:
                    column = src.column
                columns.append(column)
            yield Chunk(Block([c.clone_empty() for c in expected]), columns,
                        columns[0].size() if columns else 0)

    def write(self, ctx: Any, block: Block) -> None:
        raise_error(
            ErrorCode.TABLE_IS_READ_ONLY,
            f"Cannot INSERT into materialized view {self.full_table_name()} directly; "
            f"insert into its source table instead (a materialized view is an insert "
            f"trigger, not a writable table)",
        )

    def row_count(self) -> int:
        target = self.target_table()
        if target is not None:
            return target.row_count()
        return sum(b.rows() for b in self._owned)

    def shutdown(self) -> None:
        self._shutdown = True


# ── 注册 ────────────────────────────────────────────────────────────────


def _view_factory(table, storage_id, context, engine_args, params) -> StorageView:
    return StorageView(table, storage_id, context, engine_args, params)


def _mv_factory(table, storage_id, context, engine_args, params) -> StorageMaterializedView:
    return StorageMaterializedView(table, storage_id, context, engine_args, params)


register_storage_engine(
    "View",
    _view_factory,
    description="普通视图:不存数据,查询时重跑子查询(需要解释器注入执行器)。",
    aliases=("VIEW",),
)
register_storage_engine(
    "MaterializedView",
    _mv_factory,
    description="物化视图:插入触发器,把源表的新行加工后写入目标表(支持 POPULATE)。",
    aliases=("MATERIALIZEDVIEW",),
)
