"""解释器 —— 一条 SQL 从 AST 到结果的**编排**。

每个语句类型有一个解释器。它们的共同约定是:

.. code-block:: text

   get_interpreter(ctx, ast_node)  →  IInterpreter
   interpreter.run()              →  QueryResult

这就是真实 ClickHouse 的 ``InterpreterFactory::get``
(``src/Interpreters/InterpreterFactory.cpp``)。它是一张
"AST 节点类型 → 解释器类"的表,查不到就报 ``UNKNOWN_STATEMENT``。

``SELECT`` 的处理流程(也是本文件的主体):

.. code-block:: text

   ① 解析 FROM        → 源(表 / 子查询 / 表函数 / JOIN)+ 源 header
   ② 列裁剪           → 只读用得到的列(下推给存储引擎)
   ③ 表达式分析        → 分出聚合前 / 聚合 / 聚合后三段(analyzer.py)
   ④ 建流水线          → processor 图(processors.py)
   ⑤ 执行             → PipelineExecutor
   ⑥ 包成 QueryResult  → 交给格式层输出

.. note::

   **mini-clickhouse 的流水线是"分段执行"的**:聚合、排序、JOIN
   这些**阻塞算子**会把中间结果物化成一个 Block,然后再起一段新的
   流水线。真实 ClickHouse 把它们串成**一整张图**,数据块在图上
   流动,内存里同时只有 ``max_block_size`` 行数量级的数据。

   结果是:我们的内存峰值更高,而且看不到"边算边出"的效果
   (真实 ClickHouse 的 ``SELECT`` 能在扫完第一个 granule 之后
   就开始往客户端推行)。第 10 章用 ``EXPLAIN PIPELINE`` 的对比
   说明这个差异,第 18 章给出内存占用的量化。
"""

from __future__ import annotations

import inspect
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..aggregates import get_aggregate_function
from ..block import Block, Chunk, ColumnWithTypeAndName
from ..catalog import ColumnDefinition, StorageID, Table, new_uuid
from ..errors import ErrorCode, MiniChError, raise_error
from ..sql import ast_ as A
from ..storages.base import ReadContext, create_storage, storage_engine_exists
from ..types import DataType, get_data_type
from ..util.logger import get_logger
from .analyzer import AggregateInfo, ExpressionAnalyzer, ExpressionLookup, QueryAnalysis
from .context import Context, QueryInfo, QueryResult, QueryStatus
from .expression import Evaluator, Scope
from .joins import JoinSettings, execute_join
from .processors import (
    AggregateDescription,
    AggregatingTransform,
    ArrayJoinTransform,
    CollectingSink,
    DistinctTransform,
    ExpressionTransform,
    FilterTransform,
    ISimpleTransform,
    LimitByTransform,
    LimitTransform,
    MergingAggregatedTransform,
    OffsetTransform,
    Pipeline,
    PipelineExecutor,
    SortingTransform,
    SourceFromBlocks,
    SourceFromChunks,
    WindowTransform,
    block_to_chunk,
)

__all__ = ["execute_query", "get_interpreter", "IInterpreter"]

_log = get_logger("Query")


def execute_query(ctx: Context, sql: str) -> QueryResult:
    """**对外的主入口** —— 解析 + 解释 + 记录。

    这个函数承担了真实 ClickHouse 里 ``executeQuery`` 的一系列职责:
    生成 query_id、登记到 ``system.processes``、执行、把结果写进
    ``system.query_log``(不管成功还是失败)。

    .. code-block:: python

       ctx = Context()
       ctx.execute("SELECT 1").scalar()      # → 1
    """
    from ..sql import parse as _parse

    node = _parse(sql)
    info = QueryInfo(ctx.new_query_id(), sql, ctx.user, ctx.database)
    info.query_kind = type(node).__name__.replace("Query", "").replace("Select", "Select")
    if isinstance(node, A.SelectQuery) and node.format:
        info.format = node.format

    ctx.processes[info.query_id] = info
    ctx.metrics.inc("Query")
    ctx.profile_events.increment("Query")
    before = ctx.profile_events.snapshot()
    start = time.perf_counter()

    try:
        interpreter = get_interpreter(ctx, node)
        result = interpreter.run()
    except MiniChError as exc:
        info.status = QueryStatus.ExceptionWhileProcessing
        info.exception = f"Code: {exc.code}. DB::Exception: {exc.message}. ({exc.name})"
        info.finish_time = None
        info.elapsed = time.perf_counter() - start
        info.profile_events = _delta(ctx, before)
        ctx.profile_events.increment("FailedQuery")
        _finish(ctx, info)
        raise exc
    finally:
        ctx.processes.pop(info.query_id, None)

    info.status = QueryStatus.QueryFinish
    info.elapsed = time.perf_counter() - start
    info.profile_events = _delta(ctx, before)
    info.settings = dict(ctx.settings.changed())
    result.info = info
    _finish(ctx, info)
    return result


def _delta(ctx: Context, before: Dict[str, int]) -> Dict[str, int]:
    """``system.query_log.ProfileEvents`` 的**增量**。

    .. warning::

       ``system.events`` 里的计数器是**累积**的(从进程启动算起),
       而 ``system.query_log.ProfileEvents`` 是**这条查询**的增量。
       两者的区别就是"取不取差"。

       早期版本只做了"过滤出发生变化的键"、**没有真的相减** ——
       于是 query_log 里的数字一路累加(第一次查询 200000、第二次
       400000、第三次 600000)。它看着像数字、格式也对,但你**没法
       拿两条查询的 ProfileEvents 做前后对比**,而那正是这张表最
       主要的用法。
    """
    after = ctx.profile_events.snapshot()
    out: Dict[str, int] = {}
    for k, v in after.items():
        d = v - before.get(k, 0)
        if d:
            out[k] = d
    return out


def _finish(ctx: Context, info: QueryInfo) -> None:
    """把一条查询写进 ``system.query_log``。"""
    ctx.query_log.append(info)
    # 真实 ClickHouse 的 query_log 也有上限(
    # ``max_query_log_size``);我们不限制,但把这一点写出来。
    if len(ctx.query_log) > 10000:  # pragma: no cover - 只是防内存无界
        del ctx.query_log[: len(ctx.query_log) - 10000]


# ══════════════════════════════════════════════════════════════════════════
# 解释器基类与工厂
# ══════════════════════════════════════════════════════════════════════════


class IInterpreter:
    """解释器基类。"""

    #: 该解释器处理的 AST 类型(工厂据此分派)
    ast_type: Any = A.Node

    def __init__(self, ctx: Context, node: A.Node) -> None:
        self.ctx = ctx
        self.node = node

    def run(self) -> QueryResult:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"{type(self).__name__} does not implement run()",
        )

    # ── 便捷构造 ────────────────────────────────────────────────────────

    def _single_block_result(self, block: Block, format_name: str = "PrettyCompact") -> QueryResult:
        """把一个 Block 包成 QueryResult(大多数 DDL/SHOW 都返回一张小表)。"""
        return QueryResult(
            self.ctx,
            self.ctx.new_query_id(),
            block.clone_empty(),
            format_name,
            QueryInfo(self.ctx.new_query_id(), ""),
            iter([block]),
        )

    def _rows_result(
        self, columns: Sequence[Tuple[str, str]], rows: Iterable[Sequence[Any]],
        format_name: str = "PrettyCompact",
    ) -> QueryResult:
        """按 ``(列名, 类型名)`` 的声明造一个结果集 —— SHOW/DESCRIBE 用。"""
        header = Block([
            ColumnWithTypeAndName(name, get_data_type(tname))
            for name, tname in columns
        ])
        data = header.clone_empty()
        for row in rows:
            if len(row) != len(columns):
                raise_error(
                    ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                    f"Row has {len(row)} values but {len(columns)} columns are declared",
                )
            for (_, tname), value, col in zip(columns, row, data.data):
                try:
                    col.column.append(get_data_type(tname).normalize(value))
                except MiniChError:
                    col.column.append(value)
        return self._single_block_result(data, format_name)


#: AST 类型 → 解释器类(在文件末尾填充,避免前向引用)
_FACTORY: Dict[type, type] = {}


def get_interpreter(ctx: Context, node: A.Node) -> IInterpreter:
    """按 AST 类型建解释器 —— ``InterpreterFactory::get`` 的对应物。"""
    cls = _FACTORY.get(type(node))
    if cls is None:
        for ast_type, interpreter_cls in _FACTORY.items():
            if isinstance(node, ast_type):
                cls = interpreter_cls
                break
    if cls is None:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"No interpreter registered for {type(node).__name__}",
        )
    return cls(ctx, node)  # type: ignore[call-arg]


def register_interpreter(ast_type: type, interpreter_cls: type) -> None:
    _FACTORY[ast_type] = interpreter_cls


# ══════════════════════════════════════════════════════════════════════════
# SELECT
# ══════════════════════════════════════════════════════════════════════════


class InterpreterSelectQuery(IInterpreter):
    """``SELECT`` 的解释器 —— 本文件里最长的一个。"""

    ast_type = A.SelectQuery

    def __init__(self, ctx: Context, node: A.SelectQuery,
                 outer_scope: Optional[Scope] = None,
                 source_override: Optional[Tuple[Table, Block]] = None) -> None:
        super().__init__(ctx, node)
        self.query: A.SelectQuery = node
        self.outer_scope = outer_scope
        #: ``(table, block)`` —— **把 ``FROM table`` 换成这个 block**。
        #:
        #: 唯一的使用者是物化视图的插入触发器:源表刚插入的那一个 block
        #: 必须被喂给 MV 的 ``SELECT``,而**不是**让 SELECT 去读源表的
        #: 全量数据(那会把历史数据反复统计一遍)。真实 ClickHouse 是在
        #: 读路径的源上插一个 MaterializedView 节点,这里等价地替换源。
        self.source_override = source_override
        #: 源表(如果有)—— 用于走存储引擎的裁剪路径
        self.source_table: Optional[Table] = None
        self.read_context: Optional[ReadContext] = None
        self.analysis: Optional[QueryAnalysis] = None
        #: 本次查询的优化结果(``EXPLAIN`` 会打印它生效了哪些规则)
        self.optimization: Optional[Any] = None
        #: 读取之前先从语法树里取出来的过滤条件(交给存储引擎做裁剪)
        self._pending_where: Optional[A.Node] = None
        self._pending_prewhere: Optional[A.Node] = None

    # ── 主流程 ──────────────────────────────────────────────────────────

    def run(self) -> QueryResult:
        query = self.query
        start = time.perf_counter()
        try:
            result = self._run_inner(query)
        finally:
            _ = time.perf_counter() - start
        return result

    #: ``SELECT count() FROM t``(没有 WHERE/GROUP BY/HAVING)可以直接
    #: 用 part 元数据回答,不必扫数据。真实 ClickHouse 由
    #: ``optimize_trivial_count_query``(默认开)做这件事 ——
    #: 这是 ``count()`` 能是**毫秒级**的唯一原因。
    def _try_trivial_count(self, query: A.SelectQuery,
                           table: Optional[Table] = None) -> Optional[int]:
        """``SELECT count() FROM t``(无 ``WHERE``/``GROUP BY``)—— 走 part 元数据。

        .. note::

           这条捷径**由优化器决定要不要走**(:mod:`minich.execution.optimizer`
           的 ``trivial_count_query`` 规则),而不是解释器自己判断 ——
           这样"优化器做了什么"和"查询计划里体现了什么"就是同一件事。
           一条只能靠 `EXPLAIN` 才能观察到的优化规则,和没有这条规则
           没有区别。
        """
        from .optimizer import optimize_select

        if self.source_override is not None:
            # 源被替换成了"刚插入的那个 block"(物化视图触发路径)。
            # 真表的 ``row_count()`` 与这次要处理的行数**毫无关系** ——
            # 拿它回答 ``count()`` 会给出源表的历史总行数,而不是这批新行
            # 的聚合结果。所以这条捷径必须关掉。
            return None
        table = table or self._source_table(query)
        if table is None or table.engine is None:
            return None
        result = optimize_select(self.ctx, query, table)
        self.optimization = result
        if not result.trivial_count:
            return None
        rows = table.row_count()
        if rows is None:  # 引擎不提供行数 → 只能老实扫
            return None
        return rows

    def _source_table(self, query: A.SelectQuery) -> Optional[Table]:
        """查询的源表(没有 FROM、或者是 system 表/表函数时返回 None)。"""
        if not isinstance(query.from_, A.TableReference) or query.from_.table_function:
            return None
        database = query.from_.database or self.ctx.database
        if database == "system":
            return None
        return self.ctx.catalog.try_get_table(database, query.from_.table)

    def _run_inner(self, query: A.SelectQuery) -> QueryResult:
        # ① UNION ALL —— 每个分支各跑一次,然后拼接
        if query.unions:
            blocks = list(self._execute_branch(query))
            for branch in query.unions:
                blocks.extend(self._execute_branch(branch))
            if query.union_distinct:
                blocks = _apply_distinct(blocks)
            if blocks:
                # **列名取第一个分支的**(与真实 ClickHouse 一致),
                # 并把后续分支的列重命名过来。
                #
                # 不这么做的后果不是"少了个名字",而是**同一个结果集里
                # 每一块的列名都不一样**:``SELECT 1 AS a UNION ALL SELECT 2 AS b``
                # 输出成 ``{"a":1}`` 和 ``{"b":2}`` 两行 —— 下游按名字取值
                # 的代码会在第二行拿到 None。JSON 这类"逐行带列名"的格式
                # 会把这个错误直接暴露出来。
                names = blocks[0].get_names()
                fixed: List[Block] = []
                for b in blocks:
                    if b.get_names() == names:
                        fixed.append(b)
                        continue
                    data = [
                        ColumnWithTypeAndName(names[i] if i < len(names) else c.name,
                                              c.type, c.column)
                        for i, c in enumerate(b.data)
                    ]
                    fixed.append(Block(data, b.info.clone()))
                blocks = fixed
            header = blocks[0].clone_empty() if blocks else Block()
            return self._make_result(header, blocks, query.format)

        # ①.5 平凡 count —— **在读任何数据之前**就回答掉
        trivial = self._try_trivial_count(query)
        if trivial is not None:
            from ..types import get_data_type

            block = Block([ColumnWithTypeAndName("count()", get_data_type("UInt64"))])
            block.get_by_position(0).column.append(trivial)
            return self._make_result(block.clone_empty(), [block], query.format)

        # ①.9 查询级 ``SETTINGS`` 子句
        #
        # ``SELECT … SETTINGS use_skip_indexes = 0`` 里的设置**优先级最高**
        # ——它在语义上属于"这一条查询",而不是会话。真实 ClickHouse 的
        # 优先级链是 查询级 > 剖面 > 会话 > 服务端默认。
        #
        # 漏掉它的后果很具体:教程里让人用 `SETTINGS use_skip_indexes = 0`
        # 对照证明"跳数索引真的省了读",而开关**不生效** ——
        # 那两条查询会给出完全一样的读数,读者会得出"索引没用"的错误结论。
        self._apply_query_settings(query.settings)

        # ② 优化器:在读取之前跑一遍规则(下推条件、记下 LIMIT)
        self._run_optimizer(query)

        # ②.5 把过滤条件交给存储引擎(见 _read_storage_table 的说明)
        self._pending_where = query.where
        self._pending_prewhere = query.prewhere

        # ③ FROM:拿到源 chunks 与 header
        chunks_iter, header = self._build_source(query)

        # ③ 分析(需要在有 header 之后,因为要展开 ``*``)
        analysis = ExpressionAnalyzer(self.ctx, query, header).analyze()
        self.analysis = analysis

        # ④ 建流水线并执行
        blocks = self._execute_pipeline(query, header, chunks_iter, analysis)
        return self._make_result(
            self._result_header(analysis, blocks, header), blocks, query.format
        )

    def _make_result(self, header: Block, blocks: List[Block],
                     format_name: Optional[str]) -> QueryResult:
        return QueryResult(
            self.ctx,
            self.ctx.new_query_id(),
            header.clone_empty() if header is not None else Block(),
            format_name or "PrettyCompact",
            None,
            iter(blocks),
        )

    def _result_header(self, analysis: QueryAnalysis, blocks: List[Block],
                       source_header: Optional[Block] = None) -> Block:
        """结果集的**表头**(零行)。

        客户端要靠它渲染空表的列名与列类型,所以即使一行都没有,
        类型也必须是对的。

        .. warning::

           早期版本在"没有数据"时把**每一列**都标成 ``String`` ——
           注释还写着"要给出正确的类型"。后果不是"少了个类型",而是
           **一条本来正确的查询报了一个假错**::

               SELECT sum(x) FROM (SELECT 1 AS x WHERE 0);
               --> Code: 24. sum requires a numeric argument, got String.

           内层查询一行都没有,于是 ``x`` 被标成 String,
           聚合器据此拒绝了它。**类型应该由表达式本身决定,与有没有
           数据无关** —— 所以这里改为"在一个零行的源块上把 SELECT
           表达式求一遍类型"。
        """
        if blocks:
            return blocks[0].clone_empty()
        names = analysis.output_names or ["result"]
        types = self._output_types(analysis, source_header)
        cols: List[ColumnWithTypeAndName] = []
        for i, n in enumerate(names):
            dtype = types[i] if i < len(types) and types[i] is not None                 else get_data_type("String")
            cols.append(ColumnWithTypeAndName(n, dtype))
        return Block(cols)

    def _output_types(self, analysis: QueryAnalysis,
                      source_header: Optional[Block]) -> List[Optional[DataType]]:
        """在**零行**的源块上求一遍 SELECT 表达式,只为拿到类型。

        为什么不直接看 ``blocks[0]``? 因为这里是"结果集为空"的那条路 ——
        没有 blocks 可看。类型推导不需要数据:``Evaluator`` 求类型时
        只关心列的 ``type``,而零行的列同样带着类型。
        """
        cols: List[ColumnWithTypeAndName] = []
        if source_header is not None:
            for c in source_header.data:
                cols.append(ColumnWithTypeAndName(c.name, c.type, c.type.create_column()))
        scope = Scope(_block_scope(Block(cols)) if cols else {})
        ev = Evaluator(self.ctx, scope, 0, subquery_executor=self._subquery_executor)
        out: List[Optional[DataType]] = []
        for expr in analysis.select_exprs:
            try:
                out.append(ev.evaluate(expr).type)
            except MiniChError:
                out.append(None)
        return out

    # ── ① 源 ────────────────────────────────────────────────────────────

    def _apply_query_settings(self, settings_list: Sequence[Tuple[str, A.Node]]) -> None:
        """把 ``SELECT … SETTINGS k = v`` 应用到这次查询的上下文上。"""
        if not settings_list:
            return
        if self.ctx is not None:
            self.ctx = self.ctx.with_settings(self.ctx.settings.clone())
        for name, value_node in settings_list:
            try:
                ev = Evaluator(self.ctx, Scope(), 1)
                value = ev.evaluate(value_node).column.get(0)
            except MiniChError:
                continue
            try:
                self.ctx.settings.set(name, value, scope="query")
            except MiniChError:
                # 未知设置名不该让查询失败 —— 真实 ClickHouse 会报
                # UNKNOWN_SETTING,而我们选择宽容(教学实现里更常见的是
                # 名字写错的笔误,静默忽略比中断查询更不容易劝退)。
                pass

    def _run_optimizer(self, query: A.SelectQuery,
                       source_columns: Optional[Sequence[str]] = None) -> None:
        """在**读取之前**跑一遍优化规则。

        规则做的事分两类:

        * **改变要读什么** —— ``move_to_prewhere`` 把只依赖排序键/分区键的
          条件下推成 ``PREWHERE``(读取阶段就能少解压);
        * **改变要读多少** —— ``limit_push_down`` 把 ``LIMIT`` 传给存储引擎,
          让它提前停止。

        真实 ClickHouse 把这些规则编译进 ``QueryPlanOptimizers`` 的
        pass 序列里,``EXPLAIN PLAN`` 能看到每一步的痕迹。
        我们把结果记在 ``self.optimization`` 上,``EXPLAIN`` 会打印出来。
        """
        from .optimizer import optimize_select

        table = self._source_table(query)
        if table is None:
            return
        self.optimization = optimize_select(self.ctx, query, table, source_columns)

    def _build_source(self, query: A.SelectQuery) -> Tuple[Iterator[Chunk], Block]:
        """解析 ``FROM`` 并把源做成一个 chunk 迭代器 + header。

        .. note::

           **真实 ClickHouse 在这里做的是"构建 QueryPlan"**,
           而 mini-clickhouse 直接产出一个 chunk 迭代器。
           代价是:我们把 JOIN、子查询这类**阻塞操作**在源阶段就
           物化了(整个右表进内存)。收益是这一层读起来非常直白。
        """
        if self.source_override is not None and self._override_applies(query):
            table, block = self.source_override
            self.source_table = table
            return iter([block_to_chunk(block)]), block.clone_empty()

        if query.from_ is None:
            # 没有 FROM:``SELECT 1`` —— 造一个"恰好一行"的源
            header = Block([ColumnWithTypeAndName("dummy", get_data_type("UInt8"))])
            block = header.clone_empty()
            block.get_by_name("dummy").column.append(0)
            return iter([block_to_chunk(block)]), header

        block = self._materialize_from(query.from_)
        return iter([block_to_chunk(block)]), block.clone_empty()

    def _override_applies(self, query: A.SelectQuery) -> bool:
        """``FROM`` 是不是就是那张被替换的表 —— 只有它是时才动源。

        ``FROM`` 写成 JOIN / 子查询时**不做**替换,老老实实去读真表
        (那条路的结果会多算历史数据 —— 所以物化视图的源表只支持
        简单的 ``FROM src``,见 ``_mv_source``)。
        """
        if self.source_override is None:
            return False
        ref = query.from_
        if not isinstance(ref, A.TableReference) or ref.table_function is not None:
            return False
        table = self.source_override[0]
        database = ref.database or self.ctx.database
        return database == table.database and ref.table == table.name

    def _materialize_from(self, node: A.Node) -> Block:
        """把 ``FROM`` 子句物化成一个 Block。"""
        if isinstance(node, A.TableReference):
            # ``FROM numbers(10)`` 解析出来的是一个**带表函数的 TableReference**
            # (而不是单独的 TableFunctionCall 节点)—— 因为 SQL 里
            # "表名(参数)" 与 "表名" 是同一个语法位置。
            # 漏掉这个分支的症状是 ``Table default. does not exist``:
            # 表名是空串,于是报了一个"名字为空的表不存在"的错误。
            if node.table_function is not None:
                return self._read_table_function(node.table_function)
            return self._read_table(node)
        if isinstance(node, A.SubqueryRef):
            inner = InterpreterSelectQuery(self.ctx, node.query, self.outer_scope)
            sub = inner.run()
            block = sub.to_block()
            if node.alias:
                block = _prefix_block(block, node.alias)
            return block
        if isinstance(node, A.TableFunctionCall):
            return self._read_table_function(node)
        if isinstance(node, A.JoinExpr):
            return self._materialize_join(node)
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Unsupported FROM clause: {type(node).__name__}",
        )

    def _read_table(self, ref: A.TableReference) -> Block:
        """读一张表 —— **列裁剪与主键/分区裁剪都在这里下推**。"""
        database = ref.database or self.ctx.database
        if database == "system" or (ref.database is None and _is_system_table(self.ctx, ref.table)):
            return self._read_system_table(ref, database)
        table = self.ctx.catalog.get_table(database, ref.table)
        self.source_table = table
        return self._read_storage_table(table, ref)

    def _read_storage_table(self, table: Table, ref: A.TableReference) -> Block:
        engine = table.engine
        header = table.header()

        # **把 WHERE / PREWHERE 交给存储引擎。**
        #
        # 这一步是整个"索引为什么有用"链条的起点。存储层拿到条件之后才能:
        #   ① 用分区键裁掉整个分区;
        #   ② 用稀疏主键索引把读取范围缩到几段 mark;
        #   ③ 用跳数索引跳过不符合条件的 granule。
        #
        # 漏掉它会发生什么:引擎拿不到条件 → 每次都全表扫 →
        # **所有裁剪都只在直连引擎 API 时才生效**,而读者从 SQL 这一侧
        # 永远看不到收益。那时 `SelectedMarks` 这类指标也不会动,
        # 于是"怎么证明索引生效"这件事变成了不可能。
        #
        # 真实 ClickHouse 传的是 ``SelectQueryInfo``(里面装着
        # ``KeyCondition``、分区表达式、PreparedSets 等一整套)。
        # 我们传的是 AST 本身,由引擎自己去分析。
        read_ctx = ReadContext(
            ctx=self.ctx,
            columns=None,
            settings=self.ctx.settings,
        )
        read_ctx.where = self._pending_where
        read_ctx.prewhere = self._pending_prewhere
        self.read_context = read_ctx

        if engine is None:
            raise_error(
                ErrorCode.TABLE_IS_READ_ONLY,
                f"Table {table.full_name()} has no storage engine",
            )
        chunks = _call_read(engine, self.ctx, read_ctx, final=ref.final)
        blocks: List[Block] = []
        for ch in chunks:
            b = ch.get_block_with_data()
            if b.rows():
                blocks.append(b)
        # .. note::
        #
        #    **不要在这里再累加一次 ``SelectedRows``。** 引擎自己报的是
        #    "裁剪之后真正读出来的行数",那才是有意义的那个数;解释器再
        #    加一遍会把计数翻倍,于是"索引省了多少读"这件事在指标里
        #    看不出来(反而看起来更差了)。
        if not blocks:
            return header
        out = blocks[0].clone_empty()
        for b in blocks:
            out.merge(b)
        # ``FINAL`` 的"同键折叠"**已经由存储引擎在读路径上完成**了:
        # 上面通过 ``_call_read(..., final=ref.final)`` 把 ``ref.final`` 交给了
        # 引擎(MergeTree 家族在 ``read(final=True)`` 里按**排序键 + 家族语义**
        # 折叠,见 ``MergeTree._final_merge_blocks``)。
        #
        # 这里**绝不能**再按"整行"去重一遍 —— 那不属于任何引擎的 FINAL 语义:
        # 普通 MergeTree 的 FINAL 是 **no-op**,逐列完全相同的重复行必须原样
        # 保留;再折一次会把它们静默合成一行。折叠逻辑只有一份,在引擎里。
        return out

    def _read_system_table(self, ref: A.TableReference, database: str) -> Block:
        """读 ``system.*``。

        用**流式**接口(``get_system_table_chunks``)而不是"一次给出整张表":
        ``system.numbers`` 是无限表,``system.processes`` 会随查询变化 ——
        它们都不适合被物化成一个 Block。这与真实 ClickHouse 把
        system 表实现成普通 ``IStorage``(同样有 ``read`` 流)是一致的。
        """
        from ..system import get_system_table_chunks

        out: Optional[Block] = None
        for chunk in get_system_table_chunks(self.ctx, ref.table):
            b = chunk.get_block_with_data()
            if out is None:
                out = b.clone_empty()
            if b.rows():
                out.merge(b)
        if out is None:
            from ..system import get_system_table_header

            header = get_system_table_header(ref.table)
            return header.clone_empty()
        return out

    def _read_table_function(self, node: A.TableFunctionCall) -> Block:
        from ..storages.table_functions import get_table_function

        fn = get_table_function(node.name)
        args = [self._eval_constant_arg(a) for a in node.args]
        header = Block(fn.get_columns(args))
        out = header.clone_empty()
        for ch in fn.execute(args, self.ctx):
            b = ch.get_block_with_data()
            if b.rows():
                if out.is_empty():
                    out = b.clone_empty()
                out.merge(b)
        return out

    def _eval_constant_arg(self, node: A.Node) -> Any:
        ev = Evaluator(self.ctx, Scope(), 1)
        return ev.evaluate(node).column.get(0)

    def _materialize_join(self, join: A.JoinExpr) -> Block:
        left_block = self._materialize_from(join.left)
        right_block = self._materialize_from(join.right)
        left_scope = Scope(_block_scope(left_block))
        right_scope = Scope(_block_scope(right_block))
        settings = JoinSettings(
            kind=join.kind,
            strictness=join.strictness,
            use_nulls=bool(self.ctx.settings.try_get("join_use_nulls", 0)),
        )
        return execute_join(self.ctx, left_block, right_block, join,
                            left_scope, right_scope, settings)

    # ── ④ 流水线 ────────────────────────────────────────────────────────

    def _execute_pipeline(
        self, query: A.SelectQuery, header: Block,
        chunks: Iterator[Chunk], analysis: QueryAnalysis,
    ) -> List[Block]:
        scope_columns = _block_scope(header)

        # ── 把源 chunk 流物化成 Block 列表 ──────────────────────────────
        # 源接口是 **Chunk 流**(与真实 ClickHouse 一致:存储引擎产出 chunk),
        # 但 mini-clickhouse 的分段执行需要在阶段之间传递 Block,
        # 所以这里做一次转换。真实 ClickHouse 全程只传 chunk,
        # 不物化 —— 这正是它内存峰值低的原因(见模块开头的说明)。
        blocks: List[Block] = []
        for chunk in chunks:
            b = chunk.get_block_with_data()
            if b.rows():
                blocks.append(b)

        # ── ARRAY JOIN(会改变行数,最先做) ──────────────────────────────
        if query.array_joins:
            blocks = self._apply_array_join(query, blocks, scope_columns)

        # ── PREWHERE / WHERE ────────────────────────────────────────────
        for cond, is_prewhere in ((analysis.prewhere, True), (analysis.where, False)):
            if cond is None:
                continue
            blocks = self._apply_filter(cond, blocks, scope_columns, is_prewhere)

        # **不能在这里提前返回。** 空输入对聚合查询是**合法的**:
        # ``SELECT count() FROM empty_table`` 的结果是"一行 0",不是零行 ——
        # 这是 SQL 标准语义,也是很多人写"有没有数据"判断时的依赖。
        # 早期版本在这里 ``return blocks``(空列表),于是所有"空表上的
        # 无 GROUP BY 聚合"都返回空结果集,而不是一行默认值。
        if not blocks and not analysis.has_aggregation:
            return blocks

        # ── 聚合 ────────────────────────────────────────────────────────
        if analysis.has_aggregation:
            blocks, agg_descs = self._apply_aggregation(query, blocks, scope_columns, analysis)
            lookup = self._build_post_lookup(analysis, blocks, agg_descs)
            # 聚合后的作用域**只有**键列与聚合结果列 —— 源列不再可见。
            # 这正是 "Column `x` is not under aggregate function and not in
            # GROUP BY" 这条错误的来源:引用源列时在作用域里找不到它。
            post_scope = _block_scope(blocks[0]) if blocks else {}
            scope_columns = post_scope
        else:
            lookup = None

        # ── 窗口函数(**必须在 SELECT 之前算**) ─────────────────────────
        # 窗口函数的实参取自原始列,而它的结果又要被 SELECT 列表引用 ——
        # 所以顺序只能是"先算窗口,再把 SELECT 里的窗口节点换成列引用"。
        # 真实 ClickHouse 也是这个顺序(WindowTransform 在
        # ExpressionTransform(projection) 之前)。
        if analysis.window_names:
            blocks = self._apply_windows(analysis, blocks, scope_columns)
            _substitute_window_nodes(analysis, blocks)

        # ── SELECT 表达式 ────────────────────────────────────────────────
        #
        # 注意:**投影放到最后**。``ORDER BY`` 允许引用不在 ``SELECT``
        # 里的列(``SELECT user FROM t ORDER BY ts`` 是合法的),
        # 所以"算出来的列"必须活到 ORDER BY 之后才能丢。
        # 真实 ClickHouse 也是这么排的:最后一步才做 projection,
        # 把中间的辅助列丢掉。
        pre_columns = blocks[0].columns() if blocks else 0
        blocks = self._apply_expressions(analysis.select_exprs, blocks, scope_columns, lookup)

        # ── DISTINCT(**只看 SELECT 的那几列**) ──────────────────────────
        if analysis.distinct:
            blocks = _apply_distinct(blocks, pre_columns)

        # ── ORDER BY ────────────────────────────────────────────────────
        if analysis.order_by:
            blocks = self._apply_order_by(analysis, blocks, scope_columns, lookup,
                                          query.limit)

        # ── LIMIT BY(**必须在 ORDER BY 之后**) ───────────────────────────
        # 真实 ClickHouse 的子句执行顺序是
        # ``… → ORDER BY → LIMIT BY → LIMIT/OFFSET``。
        #
        # 这个顺序**不是可有可无的**:``LIMIT BY`` 的语义是"每个分组取前 N 行",
        # 而"前"是按**输入顺序**定义的。只有先排好序,``ORDER BY ts DESC
        # LIMIT 3 BY user`` 才真的是"每个用户最近 3 条";反过来先做 LIMIT BY,
        # 取到的就是"每组任意 3 条,再把这 3 条排序" —— 一个**静默的错误结果**
        # (行数对、内容错、不报错)。
        #
        # 这个 bug 在实现时确实出现过,是写第 07 章的审计者读代码时发现的 ——
        # 它属于"只靠跑测试很难发现"的那一类:测试通常只断言计数。
        if query.limit_by is not None:
            blocks = self._apply_limit_by(query.limit_by, blocks, scope_columns, lookup)

        # ── OFFSET / LIMIT ──────────────────────────────────────────────
        if query.offset is not None or query.limit is not None:
            blocks = self._apply_limit(query, blocks)

        # ── 最终投影:只留 SELECT 要的列,并按 SELECT 的顺序命名 ──────────
        return self._apply_projection(analysis, blocks, pre_columns)

    # ── 各阶段 ──────────────────────────────────────────────────────────

    def _apply_array_join(self, query: A.SelectQuery, blocks: List[Block],
                          scope_columns: Dict[str, ColumnWithTypeAndName]) -> List[Block]:
        aliases = [j.alias for j in query.array_joins]
        out: List[Block] = []
        transform = ArrayJoinTransform(
            self.ctx,
            [j.expr for j in query.array_joins],
            scope_columns,
            left=any(j.left for j in query.array_joins),
            subquery_executor=self._subquery_executor,
        )
        for b in blocks:
            ch = block_to_chunk(b)
            result = transform.transform(ch)
            if result is None or not result.num_rows:
                continue
            block = result.get_block_with_data()
            # ``ARRAY JOIN arr AS x`` —— 展开出来的列必须叫 ``x``,
            # 否则后续 ``SELECT x`` 找不到它(列名会退化成被求值表达式
            # 的文本,例如 ``[1, 2]``,那不是一个合法标识符)。
            if any(aliases):
                data = block.data[: len(block.data) - len(aliases)]
                for alias, col in zip(aliases, block.data[len(block.data) - len(aliases):]):
                    data.append(ColumnWithTypeAndName(alias or col.name, col.type, col.column))
                block = Block(data)
            out.append(block)
        self.ctx.profile_events.increment("ArrayJoinRows", sum(b.rows() for b in out))
        return out

    def _apply_filter(self, cond: A.Node, blocks: List[Block],
                      scope_columns: Dict[str, ColumnWithTypeAndName],
                      is_prewhere: bool) -> List[Block]:
        transform = FilterTransform(self.ctx, cond, scope_columns, is_prewhere,
                                    subquery_executor=self._subquery_executor)
        out: List[Block] = []
        for b in blocks:
            ch = block_to_chunk(b)
            result = transform.transform(ch)
            if result is not None and result.num_rows:
                out.append(result.get_block_with_data())
        filtered = transform.rows_before - transform.rows_after
        if filtered:
            self.ctx.profile_events.increment("FilteredRows", filtered)
        return out

    def _apply_expressions(
        self, exprs: Sequence[A.Node], blocks: List[Block],
        scope_columns: Dict[str, ColumnWithTypeAndName], lookup: Optional[ExpressionLookup],
        project: bool = False,
        output_names: Optional[Sequence[str]] = None,
    ) -> List[Block]:
        """算一批表达式。

        .. code-block:: text

           project=False(SELECT 之前/中间阶段):
               输入 [url, ts]  →  输出 [url, ts, domain(url), toDate(ts)]
                                    ↑ 原列**保留** —— 后面的 ORDER BY/WHERE 还要用

           project=True(SELECT 的最后一步):
               输入 [url, ts, domain(url), toDate(ts)]
                     →  输出 [domain(url), toDate(ts)]
                          ↑ **只留** SELECT 要的那些,并按 SELECT 的顺序重命名

        这一步在真实 ClickHouse 里叫 ``ExpressionTransform`` 之后的
        **Projection**,由 ``InterpreterSelectQuery::executeFetchColumns``
        与最后的 ``executeProjection`` 完成。少了它,``SELECT 1`` 会把
        源表的所有列都带出来 —— 这正是本节实现时踩到的第一个 bug:

            ``SELECT 1 + 1 AS x`` 的输出是 ``0\\t2``(两列:
            一个来自无 FROM 时的哑列,一个才是 ``x``)。

        **为什么可以按位置取而不按名字匹配?** 因为 ``ISimpleTransform``
        的约定是"先原样保留输入列,再按顺序追加新列"。所以第
        ``len(input_columns)`` 列之后的,就是这次算出来的 ——
        比按 ``to_sql()`` 文本匹配名字可靠得多(文本匹配在
        ``a + b`` vs ``plus(a, b)`` 这种地方会失效)。
        """
        if not exprs:
            return blocks
        transform = ExpressionTransform(self.ctx, exprs, scope_columns,
                                        subquery_executor=self._subquery_executor)
        transform.lookup = lookup
        out: List[Block] = []
        for b in blocks:
            input_cols = b.columns()
            ch = block_to_chunk(b)
            result = transform.transform(ch)
            if result is None or result.num_rows == 0:
                continue
            produced = result.get_block_with_data()
            if project:
                produced = _project_block(produced, input_cols, output_names)
            out.append(produced)
        return out

    def _apply_projection(self, analysis: QueryAnalysis, blocks: List[Block],
                          pre_columns: int) -> List[Block]:
        """只保留 SELECT 列表对应的列,并按 SELECT 的顺序命名。"""
        return [
            _project_block(b, pre_columns, analysis.output_names)
            for b in blocks if b.rows()
        ]

    def _apply_aggregation(
        self, query: A.SelectQuery, blocks: List[Block],
        scope_columns: Dict[str, ColumnWithTypeAndName], analysis: QueryAnalysis,
    ) -> List[Block]:
        """**两阶段聚合** —— 部分聚合 + 合并。"""
        from .processors import AggregateDescription

        # ① 先把所有聚合函数的**参数表达式**收集起来并物化成列。
        #
        #    顺序很重要:必须**先**物化,**再**构造聚合函数 ——
        #    因为聚合函数需要真实的参数类型才能推出返回类型。
        arg_exprs: List[A.Node] = []
        for info in analysis.aggregates:
            arg_exprs.extend(info.arg_exprs)
        if arg_exprs:
            materialized = self._apply_expressions(arg_exprs, blocks, scope_columns, None)
        else:
            materialized = blocks

        # ② 参数的真实类型:优先从物化结果里读(最可靠 —— 那就是引擎
        #    实际会喂给聚合器的列),没有数据时退化成"按源列推导"。
        arg_types = self._aggregate_arg_types(
            analysis, materialized, arg_exprs, scope_columns
        )

        # ③ 按真实类型构造聚合实现。
        #
        #    ⚠️ 早期版本这里传的是 ``[get_data_type("UInt8")] * len(args)``
        #    这种**占位类型**。后果是 ``min(status)``(status 是 String)
        #    会推出 ``UInt8`` 的返回类型,于是把一个字符串往 uint8 列里写
        #    → ``OverflowError: ... out of bounds for uint8``。
        #    占位类型在"只有 sum/count"的测试里看不出问题,所以很能藏。
        agg_descs: List[AggregateDescription] = []
        cursor = 0
        for info in analysis.aggregates:
            n = len(info.arg_exprs)
            types_i = arg_types[cursor : cursor + n]
            cursor += n
            try:
                func = get_aggregate_function(info.node.name, info.params, types_i)
            except MiniChError as exc:
                raise_error(
                    exc.code,
                    f"Cannot resolve aggregate function {info.node.name}"
                    f"({', '.join(t.get_name() for t in types_i)}): {exc.message}",
                )
            agg_descs.append(AggregateDescription(
                func_name=info.base_name,
                func=func,
                arg_exprs=list(info.arg_exprs),
                params=info.params,
                argument_names=[a.to_sql() for a in info.arg_exprs],
                combinator=info.combinator,
                result_name=info.state_name,
                is_state=info.is_state,
                is_merge=info.is_merge,
            ))
        # 同样**不能提前返回**:空输入下 ``count()`` 仍要产出一行 0。

        def run_aggregation(keys: Sequence[A.Node]) -> Optional[Block]:
            """对给定的键集合跑一遍"部分聚合 + 归并"(不 finalize)。

            抽成函数是为了支持 ``WITH ROLLUP``/``WITH CUBE`` —— 它们要对
            键集合的**各个子集**分别聚合一遍。真实 ClickHouse 也是这么做的
            (它把 ROLLUP/CUBE 展开成一组 ``GROUPING SETS``,每个集合一次聚合)。
            """
            partial = AggregatingTransform(
                self.ctx,
                keys,
                agg_descs,
                scope_columns,
                max_rows=_int_setting(self.ctx, "max_rows_to_group_by", 0) or None,
                subquery_executor=self._subquery_executor,
            )
            # ① 部分聚合:把每个输入块喂进去,累积"部分状态"。
            #    注意这里**没有**把状态写成一个 Block 再读回来 ——
            #    真实 ClickHouse 会把部分状态序列化后发给归并节点
            #    (即使在本机,也是流经端口)。mini-clickhouse 直接复用
            #    同一个 Python 对象,省掉了序列化开销。
            for b in materialized:
                partial._accumulate(block_to_chunk(b))
            # ② **即使一行输入都没有,也要调 _emit()** —— 因为
            #    ``SELECT count() FROM empty_table`` 必须是**一行 0**,
            #    而不是零行。没有 GROUP BY 的聚合在空输入上仍然有一行结果,
            #    这是 SQL 的标准语义(也是很多人写"有没有数据"判断时的依赖)。
            states = partial._emit()
            if states is None or states.num_rows == 0:
                return None
            # ③ 归并:把部分状态合成最终状态(不 finalize —— finalize 在
            #    "聚合后求值"那一步做,因为只有那里才知道用户要的是
            #    最终值还是状态本身(``-State``))。
            merging = MergingAggregatedTransform(self.ctx, agg_descs, with_finalize=False)
            merging._merge(states)
            out = merging._emit()
            if out is None:
                return None
            self.ctx.profile_events.increment("AggregatedRows", out.num_rows)
            return out.get_block_with_data()

        grouping_sets = _rollup_or_cube_sets(query, len(analysis.key_exprs))

        if grouping_sets is None:
            block = run_aggregation(analysis.key_exprs)
            if block is None:
                return [], agg_descs
            # ── ``GROUP BY … WITH TOTALS``:在末尾追加一行"总计" ──────────
            #
            # 总计行的语义(与真实 ClickHouse 一致,而且**这一点经常让人意外**):
            #
            #   * 它是对**全部行**再聚合一次的结果;
            #   * 它的**分组键列填的是该类型的默认值**(``0`` / 空串),
            #     不是 NULL、也不是最后一组的键。
            #
            # 第 2 条正是坑所在:如果分组键本身可能是默认值(``GROUP BY status``
            # 而 status 有 ``''``),总计行就会和真实的分组行**长得一样**,
            # 分不出来。真实 ClickHouse 也是这样,所以"用 WITH TOTALS 时
            # 别指望能从键上分辨总计行"是一条要写进文档的注意事项。
            if analysis.with_totals and block.rows():
                block = _append_totals_row(block, analysis, agg_descs)
            return [block], agg_descs

        # ── ``WITH ROLLUP`` / ``WITH CUBE``:对每个键子集各聚合一遍 ─────────
        if analysis.with_totals:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                "WITH ROLLUP/WITH CUBE combined with WITH TOTALS "
                "is not implemented in mini-clickhouse",
            )
        base = run_aggregation(analysis.key_exprs)
        if base is None:
            return [], agg_descs
        n_keys = len(analysis.key_exprs)
        out_blocks: List[Block] = []
        for subset in grouping_sets:
            if len(subset) == n_keys:
                out_blocks.append(base)
                continue
            keys = [analysis.key_exprs[i] for i in subset]
            sub = run_aggregation(keys)
            if sub is None:
                continue
            out_blocks.append(_pad_grouping_block(sub, subset, base))
        # 所有子集块结构一致,合成**一个** block —— 下游的"聚合后查找表"
        # 会把若干 block 先 merge 再建列,分行返回会让查找表列长对不上。
        merged = out_blocks[0]
        for b in out_blocks[1:]:
            merged.merge(b)
        return [merged], agg_descs

    def _aggregate_arg_types(
        self,
        analysis: QueryAnalysis,
        materialized: List[Block],
        arg_exprs: Sequence[A.Node],
        scope_columns: Dict[str, ColumnWithTypeAndName],
    ) -> List[DataType]:
        """推导聚合函数的**真实参数类型**。

        两条路径:

        * **有数据** —— 直接从物化后的 block 尾部把那些参数列的类型读出来。
          这是最可靠的一条:它就是引擎真正会喂给聚合器的列。
        * **没有数据**(空表) —— 退回"静态推导":在源列的作用域上对每个
          参数表达式求一次**类型**(不取值)。这条路径同样必要,因为
          ``SELECT min(s) FROM empty`` 也必须推出正确的结果类型,
          否则结果集的列类型会是错的。
        """
        if arg_exprs and materialized:
            tail = materialized[0].data
            if len(tail) >= len(arg_exprs):
                return [c.type for c in tail[len(tail) - len(arg_exprs):]]
        if not arg_exprs:
            return []
        scope = Scope(dict(scope_columns))
        # 用一个"零行但没有列"的 evaluator 只求类型
        from .expression import Evaluator as _Ev

        out: List[DataType] = []
        for expr in arg_exprs:
            try:
                ev = _Ev(self.ctx, scope, 0)
                out.append(ev.evaluate(expr).type)
            except MiniChError:
                # 实在推不出来时保守地用 UInt8 —— 但这只可能发生在
                # 表达式本身有问题的查询里,后续阶段仍会报出真正的错误。
                out.append(get_data_type("UInt8"))
        return out

    def _empty_aggregated_block(self, analysis: QueryAnalysis,
                                descs: Sequence[Any]) -> Block:
        """没有输入行时也要给一个"空但结构正确"的聚合结果。"""
        cols: List[ColumnWithTypeAndName] = []
        for i, k in enumerate(analysis.key_exprs):
            ev = Evaluator(self.ctx, Scope(), 1)
            try:
                dtype = ev.evaluate(k).type
            except MiniChError:
                dtype = get_data_type("String")
            cols.append(ColumnWithTypeAndName(analysis.key_names[i], dtype,
                                              dtype.create_column()))
        for d in descs:
            from .processors import _state_type

            st = _state_type(d)
            cols.append(ColumnWithTypeAndName(d.result_name, st, st.create_column()))
        return Block(cols)

    def _build_post_lookup(self, analysis: QueryAnalysis, blocks: List[Block],
                           agg_descs: Sequence[AggregateDescription]) -> ExpressionLookup:
        """构建"聚合后阶段"的表达式查找表。

        聚合后的 block 里只有两样东西:GROUP BY 的键列、以及
        各个聚合的**状态列**。所以这张表要做两件事:

        1. 键表达式(``toDate(ts)``)→ 键列;
        2. 聚合调用(``sum(x)``)→ **最终化后的值列**。

        第 2 条是这里最有意思的地方:我们把状态列"finalize"成一个
        普通列(Name 就是聚合调用的 SQL 文本),于是聚合后的表达式
        (``sum(x)/count()``、``HAVING sum(x) > 10``)就能像普通
        表达式一样求值 —— 这也是真实 ClickHouse 里
        ``finalizeAggregation`` 那个函数名想表达的意思。
        """
        lookup = ExpressionLookup()
        if not blocks:
            return lookup
        header = blocks[0].clone_empty()
        merged = blocks[0].clone_empty()
        for b in blocks:
            if merged.is_empty():
                merged = b.clone_empty()
            merged.merge(b)
        rows = merged.rows()

        for i, key_expr in enumerate(analysis.key_exprs):
            if i >= len(merged.data):
                break
            c = merged.data[i]
            lookup.add_sql(key_expr.to_sql() if hasattr(key_expr, "to_sql") else str(key_expr),
                           ColumnWithTypeAndName(c.name, c.type, c.column))
            lookup.add_sql(analysis.key_names[i],
                           ColumnWithTypeAndName(c.name, c.type, c.column))

        for j, info in enumerate(analysis.aggregates):
            idx = len(analysis.key_exprs) + j
            if idx >= len(merged.data):
                break
            state_col = merged.data[idx]
            # **复用**聚合阶段已经构造好的实现 —— 它带着真实的
            # arg_types,所以 get_return_type() 能算对。
            # 拿空参数重新构造一个会得到 "requires an argument" 之类的错。
            func = agg_descs[j].func if j < len(agg_descs) else None
            if info.is_state or func is None:
                # ``-State``:结果就是状态本身
                lookup.add_sql(info.output_name, state_col)
                continue
            # finalize:状态列 → 最终值列
            out_type = func.get_return_type()
            value_col = out_type.create_column()
            states = state_col.column
            for r in range(rows):
                func.insert_result_into(states.get(r) if hasattr(states, "get") else None,
                                        value_col)
            lookup.add_sql(info.output_name,
                           ColumnWithTypeAndName(info.output_name, out_type, value_col))
        return lookup

    def _apply_windows(self, analysis: QueryAnalysis, blocks: List[Block],
                       scope_columns: Dict[str, ColumnWithTypeAndName]) -> List[Block]:
        if not blocks:
            return blocks
        transform = WindowTransform(self.ctx, analysis.window_names, scope_columns)
        for b in blocks:
            transform._blocks.append(b)
        result = transform._compute()
        if result is None or result.num_rows == 0:
            return []
        return [result.get_block_with_data()]

    def _apply_limit_by(self, clause: A.LimitByClause, blocks: List[Block],
                        scope_columns: Dict[str, ColumnWithTypeAndName],
                        lookup: Optional[ExpressionLookup]) -> List[Block]:
        limit = _const_int(self.ctx, clause.limit)
        offset = _const_int(self.ctx, clause.offset) or 0
        transform = LimitByTransform(self.ctx, clause.elements, limit, offset, scope_columns)
        transform.lookup = lookup
        out: List[Block] = []
        for b in blocks:
            status = transform.work_with(block_to_chunk(b))
            if status is not None and status.num_rows:
                out.append(status.get_block_with_data())
        return out

    def _apply_order_by(self, analysis: QueryAnalysis, blocks: List[Block],
                        scope_columns: Dict[str, ColumnWithTypeAndName],
                        lookup: Optional[ExpressionLookup],
                        limit: Optional[A.Node]) -> List[Block]:
        if not blocks:
            return blocks
        merged = blocks[0].clone_empty()
        for b in blocks:
            if merged.is_empty():
                merged = b.clone_empty()
            merged.merge(b)
        rows = merged.rows()
        if rows == 0:
            return []

        scope = Scope(dict(scope_columns))
        for c in merged.data:
            scope.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, c.column))
        ev = Evaluator(self.ctx, scope, rows, subquery_executor=self._subquery_executor,
                       expression_lookup=lookup)
        sort_keys: List[Any] = []
        for e in analysis.order_by:
            res = ev.evaluate(e.expr)
            sort_keys.append(res.column.convert_to_full_column()
                             if res.column.is_const() else res.column)
        from .processors import _stable_sort_indexes

        reverse = [e.direction == "DESC" for e in analysis.order_by]
        nulls_first = []
        for e in analysis.order_by:
            if e.nulls == "FIRST":
                nulls_first.append(True)
            elif e.nulls == "LAST":
                nulls_first.append(False)
            else:
                nulls_first.append(e.direction == "DESC")
        indexes = _stable_sort_indexes(sort_keys, reverse, nulls_first, rows)
        merged.permute(indexes)
        self.ctx.profile_events.increment("SortedRows", rows)
        return [merged]

    def _apply_limit(self, query: A.SelectQuery, blocks: List[Block]) -> List[Block]:
        offset = _const_int(self.ctx, query.offset) or 0
        limit = _const_int(self.ctx, query.limit)
        out: List[Block] = []
        skipped = 0
        taken = 0
        for b in blocks:
            rows = b.rows()
            start = 0
            if skipped < offset:
                start = min(offset - skipped, rows)
                skipped += start
            remaining = rows - start
            if remaining <= 0:
                continue
            take = remaining
            if limit is not None:
                take = min(limit - taken, remaining)
                if take <= 0:
                    break
            taken += take
            out.append(b.slice(start, take) if (start or take != rows) else b)
            if limit is not None and taken >= limit:
                break
        return out

    # ── 子查询 ──────────────────────────────────────────────────────────

    def _execute_branch(self, query: A.SelectQuery) -> List[Block]:
        """执行 ``UNION`` 的一个分支。

        每个分支是**独立的一条查询**(它有自己的 WITH/FROM/WHERE),
        所以这里直接递归地用一个新的解释器跑它。真实 ClickHouse 会把
        UNION 分解成多个 QueryPlan 再合并,但语义完全一样。
        """
        # **必须清掉 unions** —— 否则分支又会执行自己的分支,无限递归。
        # 真实 ClickHouse 在解析期就把 UNION 拆成多个独立查询
        # (``UnionQuery``),每个分支都是干净的 SelectQuery。
        import copy as _copy

        branch = _copy.copy(query)
        branch.unions = []
        # 源替换(物化视图触发)要跟着分支走 —— 否则 ``SELECT … FROM src
        # UNION ALL SELECT … FROM src`` 这样的物化视图会把整张源表
        # 读进来重算一遍,而不是只看刚插入的那一批行。
        sub = InterpreterSelectQuery(self.ctx, branch, self.outer_scope,
                                     self.source_override)
        result = sub.run()
        return result.blocks()

    def _subquery_executor(self, query: A.SelectQuery, header_only: bool = False):
        """标量子查询的执行入口 —— 交给求值器注入。

        真实 ClickHouse 支持**相关子查询**吗? **不支持**
        (这是它一个有名的限制 —— 相关子查询会报
        ``NOT_IMPLEMENTED``)。所以这里的实现不需要传入外层作用域,
        与真实行为一致。
        """
        interp = InterpreterSelectQuery(self.ctx, query)
        result = interp.run()
        if header_only:
            header = result.header
            cols = header.data if header is not None else []
            dtype = cols[0].type if cols else get_data_type("String")
            return _SingleColumn(dtype.create_column(), dtype)
        block = result.to_block()
        if block.columns() != 1:
            raise_error(
                ErrorCode.ILLEGAL_COLUMN,
                f"A scalar subquery must return exactly one column, got "
                f"{block.columns()}",
            )
        c = block.data[0]
        return _SingleColumn(c.column, c.type)


class _SingleColumn:
    """标量子查询的结果包装 —— 只是一个 (column, type) 对。"""

    __slots__ = ("column", "type")

    def __init__(self, column: Any, dtype: Any) -> None:
        self.column = column
        self.type = dtype


# ══════════════════════════════════════════════════════════════════════════
# INSERT
# ══════════════════════════════════════════════════════════════════════════


class InterpreterInsertQuery(IInterpreter):
    """``INSERT`` 的解释器。

    支持三种来源:

    * ``VALUES`` —— 逐行字面量(最慢,但最方便);
    * ``SELECT`` —— 查询结果直接写(最快,是推荐的批量导入方式);
    * ``FORMAT`` —— 由格式解析器读一段文本(第 17 章)。

    .. note::

       真实 ClickHouse 的 INSERT 是**两阶段**的:先写临时 part
       (``tmp_insert_*``),写满或提交时**原子改名**成正式 part。
       这个设计的收益是"**读的人永远看不到半个 part**" ——
       这是它敢在写入的同时提供一致性读取的底气。
       见第 05 章。
    """

    ast_type = A.InsertQuery

    def run(self) -> QueryResult:
        node: A.InsertQuery = self.node
        database = node.database or self.ctx.database

        if node.table_function is not None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                "INSERT INTO FUNCTION is not implemented in mini-clickhouse",
            )

        table = self.ctx.catalog.get_table(database, node.table)
        if node.settings:
            self._apply_query_settings(node.settings)

        if node.values:
            block = self._build_values_block(table, node)
        elif node.select is not None:
            sub = InterpreterSelectQuery(self.ctx, node.select)
            block = sub.run().to_block()
        elif node.format is not None:
            block = self._read_format(table, node, node.data or "")
        else:
            raise_error(
                ErrorCode.SYNTAX_ERROR,
                "INSERT requires VALUES, SELECT or FORMAT",
            )

        # ``INSERT ... SELECT`` 与 ``INSERT ... FORMAT`` 按**位置**对齐列。
        # 这一点是 SQL 语义,不是实现细节:
        #     INSERT INTO t SELECT number, concat(...) FROM numbers(10)
        # 里那两个表达式的**名字**是 ``number`` 与 ``concat(...)``,
        # 与表的列名毫无关系 —— 按名字对齐会让每一列都落空,
        # 于是整张表被填成默认值(而且**不报错**)。
        # 只有 ``VALUES`` 那条路是用括号里的列名显式指定的,才按名字对齐。
        positional = node.select is not None or node.format is not None
        block = self._align_block(table, block, node.columns, positional=positional)
        table.write(self.ctx, block)
        self.ctx.profile_events.increment("InsertedRows", block.rows())
        self.ctx.profile_events.increment("InsertedBytes", _block_bytes(block))
        self._trigger_materialized_views(table, block)
        return self._rows_result([], [])

    def _trigger_materialized_views(self, table: Table, block: Block) -> None:
        """往源表插入一批行之后,把它们喂给挂在这张表上的每条物化视图。

        真实的语义(也是"物化视图 ≠ 定时任务"的全部含义):

        * **只喂刚插入的这个 block**,不喂历史数据 —— 所以插入之前就存在
          的行永远不会被这条 MV 看到(除非建视图时写了 ``POPULATE``);
        * MV 把它 ``SELECT`` 加工后的结果写进 ``TO`` 的目标表;
        * 目标表的写入是**确定性**的,与源表写入同一批数据 —— 这就是
          "恰好一次"和"增量聚合"能成立的原因。

        实现上做**一轮**传播:MV 的结果直接写目标的引擎
        (:meth:`StorageMaterializedView.push`),**不**再经过 ``INSERT``
        解释器 —— 于是"MV1 → 表 T → MV2(T 上的 MV)"这种**链式**视图
        只触发最前面那一环:MV2 不会跟着动。

        .. warning::

           **这是 mini-clickhouse 的简化,不是真实 ClickHouse 的行为。**

           真实 ClickHouse **支持级联物化视图**(官方文档有专门一节
           "Cascading materialized views"):只要把上一环的 ``TO`` 目标表
           写成下一环的源表,``INSERT`` 就会沿链条一级级传下去 ——
           多级 rollup(原始 → 小时 → 天 → 月)就是这么搭的,中间层还要
           配合 ``sumMergeState`` / ``countMergeState`` 这类 ``-MergeState``
           组合子,把子状态并成父状态。

           本实现**不做**级联:链条上第二环及之后**不会**被触发。需要多级
           rollup 时,只能把每一环当成独立的一步显式去做。原因见
           ``test_materialized_view_only_fires_one_round_of_propagation``。

        触发点是**解释器的 INSERT**,不是存储引擎的 ``write``。所以像
        ``Buffer`` 引擎到点刷盘、``Merge`` 这类由引擎自己发起的写,
        都不会触发物化视图 —— 与真实 ClickHouse 一致(那些写也没有
        一条"插入的 block"可以喂进去)。
        """
        from ..storages.view import StorageMaterializedView

        if block.rows() == 0:
            return
        for candidate in list(self.ctx.catalog.all_tables()):
            engine = candidate.engine
            if not isinstance(engine, StorageMaterializedView):
                continue
            if not engine.matches_source(table):
                continue
            out = self._run_mv_select(engine, table, block)
            if out.rows():
                engine.push(self.ctx, out)

    def _run_mv_select(self, engine: Any, source_table: Table, block: Block) -> Block:
        """在被替换成"刚插入 block"的源上,跑一遍物化视图的 ``SELECT``。"""
        select = engine.select
        if select is None:
            return Block([])
        interp = InterpreterSelectQuery(self.ctx, select,
                                        source_override=(source_table, block))
        return interp.run().to_block()

    def _apply_query_settings(self, settings: Sequence[Tuple[str, A.Node]]) -> None:
        for name, value_node in settings:
            ev = Evaluator(self.ctx, Scope(), 1)
            try:
                value = ev.evaluate(value_node).column.get(0)
            except MiniChError:
                continue
            try:
                self.ctx.settings.set(name, value, scope="query")
            except MiniChError:
                pass

    def _build_values_block(self, table: Table, node: A.InsertQuery) -> Block:
        """把 ``VALUES (...)`` 变成 Block —— 逐行解析 + 按列类型转换。"""
        names = node.columns or table.stored_column_names()
        types = []
        for n in names:
            col = table.get_column(n)
            if col is None:
                raise_error(
                    ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                    f"Table {table.full_name()} has no column {n}",
                )
            types.append(col.type)
        cols = [t.create_column() for t in types]
        ev = Evaluator(self.ctx, Scope(), 1)
        for row in node.values:
            if len(row) != len(names):
                raise_error(
                    ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                    f"Expected {len(names)} values for columns ({', '.join(names)}), "
                    f"got {len(row)}",
                )
            for expr, dtype, col in zip(row, types, cols):
                value = ev.evaluate(expr).column.get(0)
                col.append(_coerce(dtype, value))
        block = Block([
            ColumnWithTypeAndName(n, t, c) for n, t, c in zip(names, types, cols)
        ])
        return block

    def _read_format(self, table: Table, node: A.InsertQuery, data: str) -> Block:
        from ..formats import get_input_format

        names = node.columns or table.stored_column_names()
        cols: List[ColumnWithTypeAndName] = []
        for n in names:
            col = table.get_column(n)
            if col is None:
                raise_error(ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                            f"Table {table.full_name()} has no column {n}")
            cols.append(ColumnWithTypeAndName(n, col.type, col.type.create_column()))
        header = Block(cols)
        fmt = get_input_format(node.format)
        out = header.clone_empty()
        for block in fmt.read(data, header, self.ctx.settings):
            if out.is_empty():
                out = block.clone_empty()
            out.merge(block)
        return out

    def _align_block(self, table: Table, block: Block, columns: Sequence[str],
                     positional: bool = False) -> Block:
        """把输入 Block 对齐到**表结构**:

        * 按名字(或位置)映射输入的列;
        * 缺失的列用默认值/``DEFAULT`` 表达式补上;
        * ``MATERIALIZED`` 列不允许被显式写入;
        * 顺序按表的列顺序排好。
        """
        stored = table.stored_column_names()
        #: 输入块的列名(按位置对齐时才用得上)
        target_names = list(columns) if columns else None
        out = Block()
        for pos, col_def in enumerate(table.columns):
            name = col_def.name
            if positional:
                # ``INSERT ... SELECT``:第 i 个表列 ← 第 i 个输入列
                idx = (target_names.index(name) if target_names and name in target_names
                       else (pos if not target_names else -1))
                source = block.get_by_position(idx) if 0 <= idx < block.columns() else None
            else:
                source = block.try_get_by_name(name)
                if source is None and target_names and name in target_names:
                    idx = target_names.index(name)
                    if idx < block.columns():
                        source = block.get_by_position(idx)
            if source is None:
                # 缺列填什么?
                #   * ``MATERIALIZED`` 列 —— 由表达式算;
                #   * 有 ``DEFAULT`` 表达式 —— **也用表达式算**,而不是类型零值。
                #     ``INSERT INTO t (a) VALUES (1)`` 在一张有
                #     ``b UInt8 DEFAULT 7`` 的表上必须把 ``b`` 写成 **7**;
                #     按零值填充会静默写进 0(与 ``SELECT`` 时的语义不一致)。
                #   * 其余 —— 类型零值(``_eval_default`` 内部会兜底成零值)。
                if col_def.is_materialized() or col_def.has_default():
                    value_block = self._eval_default(table, col_def, block)
                    out.add_column(ColumnWithTypeAndName(name, col_def.type, value_block))
                    continue
                out.add_column(ColumnWithTypeAndName(
                    name, col_def.type, _fill_default(col_def.type, block.rows())
                ))
                continue
            if col_def.is_materialized():
                raise_error(
                    ErrorCode.CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN,
                    f"Cannot insert into MATERIALIZED column {name} of table "
                    f"{table.full_name()}",
                )
            converted = _convert_column(source, col_def.type)
            out.add_column(ColumnWithTypeAndName(name, col_def.type, converted))
        return out

    def _eval_default(self, table: Table, col_def: ColumnDefinition, block: Block) -> Any:
        return _eval_default_column(self.ctx, col_def, block)


# ══════════════════════════════════════════════════════════════════════════
# DDL
# ══════════════════════════════════════════════════════════════════════════


class InterpreterCreateQuery(IInterpreter):
    """``CREATE`` —— 表、库、视图、物化视图、字典、函数。"""

    ast_type = A.CreateTableQuery

    def run(self) -> QueryResult:
        node = self.node
        if isinstance(node, A.CreateDatabaseQuery):
            self.ctx.catalog.create_database(
                node.name, node.engine or "Atomic",
                if_not_exists=node.if_not_exists, comment=node.comment or "",
            )
            _log.information(f"Created database {node.name}")
            return self._rows_result([], [])
        if isinstance(node, A.CreateViewQuery):
            return self._create_view(node)
        if isinstance(node, A.CreateDictionaryQuery):
            return self._create_dictionary(node)
        if isinstance(node, A.CreateFunctionQuery):
            return self._create_function(node)
        if isinstance(node, A.CreateUserQuery):
            return self._create_access(node)
        return self._create_table(node)

    # ── 建表 ────────────────────────────────────────────────────────────

    def _create_table(self, node: A.CreateTableQuery) -> QueryResult:
        database = node.database or self.ctx.database
        self.ctx.catalog.get_database(database)

        columns, engine_name, params = self._resolve_definition(node)

        storage_id = StorageID(database, node.table, new_uuid())
        table = Table(storage_id, columns, None, node.to_sql(), node.comment or "")
        table = self.ctx.catalog.create_table(database, table, node.if_not_exists)

        if not storage_engine_exists(engine_name):
            raise_error(
                ErrorCode.UNKNOWN_STORAGE,
                f"Unknown table engine {engine_name}",
            )
        #: 把"建表参数"留在表上 —— ``CREATE TABLE t2 AS t1`` 与
        #: ``SHOW CREATE TABLE`` 都要用它。真实 ClickHouse 是**重放**
        #: 建表时的元数据 SQL(``metadata/<db>/<table>.sql``),
        #: 语义等价但更保真(连列注释、CODEC 都原样带回)。
        table.engine_params = params
        engine = create_storage(table, engine_name, node.engine_params,
                                context=self.ctx, params=params, storage_id=storage_id)
        table.engine = engine
        engine.startup()

        if node.as_select is not None:
            sub = InterpreterSelectQuery(self.ctx, node.as_select)
            block = sub.run().to_block()
            aligned = InterpreterInsertQuery(
                self.ctx, A.InsertQuery(database=database, table=node.table)
            )._align_block(table, block, None)
            table.write(self.ctx, aligned)
        return self._rows_result([], [])

    def _resolve_definition(self, node: A.CreateTableQuery):
        """把 AST 的列定义解析成存储层的 :class:`ColumnDefinition` 列表。"""
        columns: List[ColumnDefinition] = []
        if node.as_table is not None:
            src_db = node.as_table.database or self.ctx.database
            src = self.ctx.catalog.get_table(src_db, node.as_table.table)
            columns = [
                ColumnDefinition(name=c.name, type=c.type, default_kind=c.default_kind,
                                 default_expr_sql=c.default_expr_sql, codec=c.codec,
                                 ttl_sql=c.ttl_sql, comment=c.comment)
                for c in src.columns
            ]
            engine_name = node.engine or src.engine.engine_name
            # **引擎参数也要继承** —— 否则 ``CREATE TABLE t2 AS t1``
            # 会得到一张"列一样但排序键/分区键全丢"的表。
            # 真实 ClickHouse 是重放 ``t1`` 的元数据 SQL,所以连
            # SETTINGS 一起带走;我们存的参数就够用了。
            inherited = dict(getattr(src, "engine_params", None) or {})
            if node.order_by:
                inherited["order_by"] = node.order_by
            if node.partition_by is not None:
                inherited["partition_by"] = node.partition_by
            if node.primary_key:
                inherited["primary_key"] = node.primary_key
            if node.sample_by is not None:
                inherited["sample_by"] = node.sample_by
            if node.ttl:
                inherited["ttl"] = node.ttl
            if node.settings:
                merged = dict(inherited.get("settings") or {})
                merged.update(dict(node.settings))
                inherited["settings"] = merged
            return columns, engine_name, inherited

        for c in node.columns:
            dtype = get_data_type(c.type_name)
            columns.append(ColumnDefinition(
                name=c.name,
                type=dtype,
                default_kind=c.default_kind,
                default_expr_sql=c.default_expr.to_sql() if c.default_expr is not None else None,
                codec=c.codec,
                ttl_sql=c.ttl.to_sql() if c.ttl is not None else None,
                comment=c.comment,
            ))
        if node.as_select is not None and not node.columns:
            # ``CREATE TABLE t ENGINE = X AS SELECT ...`` —— 列由 SELECT 推出
            sub = InterpreterSelectQuery(self.ctx, node.as_select)
            result = sub.run()
            header = result.header
            if header is not None:
                columns = [ColumnDefinition(name=c.name, type=c.type) for c in header.data]

        engine_name = node.engine
        if not engine_name:
            raise_error(
                ErrorCode.SYNTAX_ERROR,
                "CREATE TABLE requires an ENGINE clause "
                "(e.g. ENGINE = MergeTree ORDER BY ...)",
            )
        params = {
            "order_by": node.order_by,
            "partition_by": node.partition_by,
            "primary_key": node.primary_key,
            "sample_by": node.sample_by,
            "ttl": node.ttl,
            "settings": dict(node.settings),
            "indexes": [_make_index_description(ix) for ix in node.indexes],
            "constraints": node.constraints,
            "projections": node.projections,
            "columns_ast": node.columns,
        }
        return columns, engine_name, params

    def _create_view(self, node: A.CreateViewQuery) -> QueryResult:
        from ..storages.view import StorageMaterializedView, StorageView

        database = node.database or self.ctx.database
        self.ctx.catalog.get_database(database)
        if node.select is None:
            raise_error(ErrorCode.SYNTAX_ERROR, "CREATE VIEW requires AS SELECT")
        sub = InterpreterSelectQuery(self.ctx, node.select)
        header = sub.run().header
        if header is None:
            header = Block()
        # **不要写成 ``header or Block()``。** ``Block.__len__`` 返回**行数**,
        # 于是任何零行的 block 都是"假"的 —— 视图的表头恰好永远是零行,
        # 那句写法会把整个表头丢掉:视图表变成**零列**的,
        # ``DESCRIBE v`` 空、``INSERT INTO v`` 在到达引擎的
        # ``TABLE_IS_READ_ONLY`` 之前就先报 INCORRECT_NUMBER_OF_COLUMNS。
        columns = [ColumnDefinition(name=c.name, type=c.type) for c in header.data]
        storage_id = StorageID(database, node.name, new_uuid())
        table = Table(storage_id, columns, None, node.to_sql(), "")
        table = self.ctx.catalog.create_table(database, table, node.if_not_exists)

        # **两条构造路径必须是同一条。**
        #
        # 注册工厂(:mod:`minich.storages.view` 末尾的 ``_view_factory`` /
        # ``_mv_factory``)是把所有东西塞进一个 ``params`` 字典再交给构造器;
        # 早期版本在这里改用关键字参数(``target_table=`` / ``query=``),
        # 于是"工厂能建的视图,``CREATE VIEW`` 建不了" —— 直接 TypeError。
        # 引擎的参数一律走 ``params``,构造函数只认这一条路。
        if node.materialized:
            to_database = to_table = None
            target = None
            if node.to_table is not None:
                to_database = node.to_table.database or self.ctx.database
                to_table = node.to_table.table
                target = self.ctx.catalog.get_table(to_database, to_table)
            source_database, source_table = self._mv_source(node.select)
            engine = StorageMaterializedView(table, storage_id, self.ctx, params={
                "select": node.select,
                "subquery_executor": self._run_subquery,
                "to_database": to_database,
                "to_table": to_table,
                "target": target,
                "source_database": source_database,
                "source_table": source_table,
            })
        else:
            engine = StorageView(table, storage_id, self.ctx, params={
                "select": node.select,
                "subquery_executor": self._run_subquery,
            })
        table.engine = engine
        engine.startup()
        if node.populate and node.materialized:
            table.engine.populate()
        return self._rows_result([], [])

    def _mv_source(self, select: A.SelectQuery) -> Tuple[Optional[str], Optional[str]]:
        """物化视图的**源表** —— ``SELECT … FROM src`` 里那第一张表。

        真实 ClickHouse 是**倒过来**登记的:它把 MV 挂到 ``SELECT`` 引用的
        **每一张**源表上(所以 ``JOIN`` 两个表的 MV 在两边插入时都会触发)。
        mini-clickhouse 只认 ``FROM`` 位置上的那张表 —— ``FROM`` 是 JOIN
        或子查询时返回 ``(None, None)``,那条 MV **只**能靠 ``POPULATE``
        回填,永远不响应 INSERT。这是能力边界,写在这里而不是让它静默失效。
        """
        ref = select.from_
        if not isinstance(ref, A.TableReference) or ref.table_function is not None:
            return None, None
        return (ref.database or self.ctx.database), ref.table

    def _run_subquery(self, query: A.SelectQuery, ctx: Any = None) -> Iterator[Block]:
        """视图的回调 —— 执行子查询,产出 Block 迭代器。

        签名 ``(select, ctx)`` 是 :mod:`minich.storages.view` 里两个视图
        引擎约定好的调用形式(``ctx`` 目前用不到:子查询总是在建视图时
        那个上下文里跑)。**普通视图的 ``read`` 与物化视图的 ``populate``
        都从这里取数据** —— 少挂一次(旧版只在普通视图分支里挂)的后果
        就是 ``POPULATE`` 报 "no subquery executor is attached"。
        """
        result = InterpreterSelectQuery(self.ctx, query).run()
        yield result.to_block()

    def _create_dictionary(self, node: A.CreateDictionaryQuery) -> QueryResult:
        from ..dictionaries import create_dictionary

        create_dictionary(self.ctx, node)
        return self._rows_result([], [])

    def _create_function(self, node: A.CreateFunctionQuery) -> QueryResult:
        from ..functions.base import get_function, register_function
        from ..functions.base import FunctionBase

        body = node.body
        if not isinstance(body, A.Lambda):
            raise_error(
                ErrorCode.SYNTAX_ERROR,
                "CREATE FUNCTION requires a lambda body, e.g. (x) -> x + 1",
            )
        captured_ctx = self.ctx

        class _UserDefinedFunction(FunctionBase):
            name = node.name

            def __init__(self) -> None:
                self.params = []
                self._body = body

            def get_return_type(self, args):
                scope = Scope()
                for pname, t in zip(self._body.params, args):
                    scope.add_column(pname, ColumnWithTypeAndName(pname, t, t.create_column()))
                from ..execution.expression import Evaluator as _Ev

                return _Ev(captured_ctx, scope, 1).evaluate(self._body.body).type

            def execute_row(self, values, context=None):
                scope = Scope()
                for pname, v in zip(self._body.params, values):
                    t = _infer_value_type(v)
                    col = t.create_column()
                    col.append(t.normalize(v))
                    scope.add_column(pname, ColumnWithTypeAndName(pname, t, col))
                from ..execution.expression import Evaluator as _Ev

                return _Ev(captured_ctx, scope, 1).evaluate(self._body.body).column.get(0)

        _UserDefinedFunction.__name__ = f"UserDefined_{node.name}"
        register_function(node.name, _UserDefinedFunction)
        return self._rows_result([], [])

    def _create_access(self, node: A.CreateUserQuery) -> QueryResult:
        from ..access import get_access_control

        get_access_control(self.ctx).create(node)
        return self._rows_result([], [])


class InterpreterDropQuery(IInterpreter):
    ast_type = A.DropQuery

    def run(self) -> QueryResult:
        node: A.DropQuery = self.node
        for ref in node.names:
            database = ref.database or self.ctx.database
            if node.kind == "DATABASE":
                self.ctx.catalog.drop_database(ref.table, if_exists=node.if_exists)
            elif node.kind == "DICTIONARY":
                from ..dictionaries import drop_dictionary

                drop_dictionary(self.ctx, database, ref.table, node.if_exists)
            elif node.kind == "FUNCTION":
                from ..functions.base import _REGISTRY

                _REGISTRY.pop(ref.table.lower(), None)
            elif node.kind in ("USER", "ROLE"):
                from ..access import get_access_control

                get_access_control(self.ctx).drop(node.kind, ref.table, node.if_exists)
            else:
                self.ctx.catalog.drop_table(database, ref.table, if_exists=node.if_exists)
        return self._rows_result([], [])


class InterpreterAlterQuery(IInterpreter):
    """``ALTER TABLE`` —— 大部分子命令都转发给存储引擎。

    .. note::

       ``ALTER`` 在 ClickHouse 里是**异步**的(除了 ``ALTER ... ATTACH``
       之类的元数据操作):``ALTER TABLE ... UPDATE/DELETE`` 会创建一个
       **mutation**,由后台 merge 逐步应用到各个 part 上。
       所以 ``ALTER`` 返回之后**数据可能还没改完** ——
       想看进度要查 ``system.mutations``。
       这是 ClickHouse 与"事务型数据库"最不像的地方之一。
    """

    ast_type = A.AlterQuery

    def run(self) -> QueryResult:
        node: A.AlterQuery = self.node
        database = node.database or self.ctx.database
        table = self.ctx.catalog.get_table(database, node.table)
        engine = table.engine
        if engine is None:
            raise_error(ErrorCode.TABLE_IS_READ_ONLY, f"{table.full_name()} has no engine")
        for cmd in node.commands:
            self._apply(table, engine, cmd)
        return self._rows_result([], [])

    def _apply(self, table: Table, engine: Any, cmd: A.AlterCommand) -> None:
        k = cmd.kind
        if k == "ADD_COLUMN":
            col = ColumnDefinition(
                name=cmd.column.name,
                type=get_data_type(cmd.column.type_name),
                default_kind=cmd.column.default_kind,
                default_expr_sql=cmd.column.default_expr.to_sql()
                if cmd.column.default_expr is not None else None,
                codec=cmd.column.codec,
                comment=cmd.column.comment,
            )
            engine.alter_add_column(col, cmd.setting_name)
            self._backfill_new_column(table, engine, col)
            return
        if k == "DROP_COLUMN":
            if table.get_column(cmd.name) is None:
                raise_error(ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                            f"Column {cmd.name} does not exist")
            engine.alter_drop_column(cmd.name)
            return
        if k == "MODIFY_COLUMN":
            engine.alter_modify_column(
                cmd.column.name,
                get_data_type(cmd.column.type_name),
                cmd.column.default_expr.to_sql() if cmd.column.default_expr is not None else None,
            )
            return
        if k == "RENAME_COLUMN":
            engine.alter_rename_column(cmd.name, cmd.new_name)
            return
        if k == "COMMENT_COLUMN":
            col = table.get_column(cmd.name)
            if col is not None:
                col.comment = str(cmd.setting_value)
            return
        if k in ("UPDATE", "DELETE"):
            from ..storages.merge_tree.mutations import MutationCommand

            # ``MutationCommand`` 收的是 **AST 谓词**(不是 SQL 文本)——
            # 因为 mutation 要在**后台 merge 时**对每个 part 求值,
            # 而那时只有一个 Block、没有解析器。
            mutation = MutationCommand(
                kind="UPDATE" if k == "UPDATE" else "DELETE",
                predicate=cmd.predicate,
                assignments=list(cmd.update_assignments),
                text=cmd.to_sql(),
                affected_columns=[name for name, _ in cmd.update_assignments],
            )
            engine.mutate(mutation)
            return
        if k == "DROP_PARTITION":
            engine.drop_partition(_literal_value(cmd.partition))
            return
        if k == "FREEZE":
            # ``ALTER TABLE … FREEZE [PARTITION p]`` —— 引擎侧是真的实现了的
            # (拷贝 part 到 ``shadow/<backup>/``,真实 ClickHouse 用硬链接),
            # 所以这里把它接上。早期版本引擎方法存在、但 SQL 层拒绝 ——
            # 那种"文档写了、代码也有、只有入口没接"的缺口最容易被误读成
            # "这个功能没实现"。
            backup = cmd.setting_name or None
            n = engine.freeze(_literal_value(cmd.partition) if cmd.partition is not None
                              else None, backup)
            _log.information(f"Freeze created a shadow backup of {n} part(s)")
            return
        if k in ("DROP_PART", "DETACH_PARTITION", "ATTACH_PARTITION", "MOVE_PARTITION"):
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"ALTER {k} is not implemented in mini-clickhouse "
                f"(DROP PARTITION / FREEZE 是可用的)",
            )
        if k == "MODIFY_TTL":
            engine.modify_ttl(cmd.ttl)
            return
        if k == "MODIFY_SETTING":
            engine.modify_setting(cmd.setting_name, _literal_value(cmd.setting_value))
            return
        if k == "MODIFY_ORDER_BY":
            engine.modify_order_by(cmd.expr)
            return
        if k in ("ADD_INDEX", "DROP_INDEX", "MATERIALIZE_INDEX"):
            # 跳数索引只支持在建表时用 ``INDEX`` 子句声明。
            # 真实 ClickHouse 支持 ``ALTER TABLE ... ADD INDEX``(它走的是
            # mutation 那条重写路径),mini-clickhouse 没有实现。
            # **必须显式报 NOT_IMPLEMENTED** —— 早期版本直接调
            # ``engine.alter_index(cmd)``,而引擎上没有这个方法,
            # 读者拿到的是 ``AttributeError: 'StorageMergeTree' object has
            # no attribute 'alter_index'``:一个既没说是谁的错、
            # 也没说该怎么办的消息。
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"ALTER {k} is not implemented in mini-clickhouse; "
                f"declare skip indexes in CREATE TABLE with an INDEX clause "
                f"(真实 ClickHouse 支持 ALTER ... ADD INDEX)",
            )
        if k == "RESET_SETTING":
            engine.modify_setting(cmd.setting_name, None)
            return
        if k in ("START_MERGES", "STOP_MERGES"):
            if k == "START_MERGES":
                engine.start_merges()
            else:
                engine.stop_merges()
            return
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"ALTER command {k} is not implemented in mini-clickhouse",
        )

    # ── ADD COLUMN 的 DEFAULT 回填 ──────────────────────────────────────

    def _backfill_new_column(self, table: Table, engine: Any,
                             col: ColumnDefinition) -> None:
        """``ADD COLUMN <c> ... DEFAULT <expr>``:把**已有行**的这一列按 DEFAULT 回填。

        为什么需要它:

        * ``MergeTree`` 家族在读路径上给"老数据里缺失的列"现补默认值
          (``MergeTreePart.missing_columns`` → ``missing_column_default``),
          所以 ``ADD COLUMN`` 是**纯元数据**操作 —— 这里的回填必须跳过它;
        * ``Memory`` / ``Log`` 家族没有这套机制,读出来的是**类型零值**,于是::

              ALTER TABLE t ADD COLUMN b UInt8 DEFAULT 7
              SELECT b FROM t   -- MergeTree → 7,Memory → 0(同一语义两个结果)

        这里统一成"按 DEFAULT 表达式回填":读出老数据、重算这一列、整表写回
        (与真实 ClickHouse 对 Memory 表做 mutation 重写的做法一致)。
        """
        if not (col.is_stored() and col.has_default()):
            return
        # 引擎自己会在读路径上补默认值 —— 委托给它,别在这里重写数据。
        if callable(getattr(engine, "_missing_column_default", None)):
            return
        block = self._read_all_rows(engine)
        if block.rows() == 0:
            return
        filled = _eval_default_column(self.ctx, col, block)
        if block.try_get_by_name(col.name) is not None:
            block.erase(col.name)
        block.add_column(ColumnWithTypeAndName(col.name, col.type, filled))
        engine.truncate()
        table.write(self.ctx, block)

    def _read_all_rows(self, engine: Any) -> Block:
        """把整表读成一个带数据的 Block(``ALTER`` 回填用)—— 不做列裁剪。"""
        read_ctx = ReadContext(ctx=self.ctx, columns=None, settings=self.ctx.settings)
        out: Optional[Block] = None
        for ch in _call_read(engine, self.ctx, read_ctx, final=False):
            b = ch.get_block_with_data()
            if b.rows() == 0:
                continue
            if out is None:
                out = b.clone_empty()
            out.merge(b)
        return out if out is not None else Block()


class InterpreterOptimizeQuery(IInterpreter):
    ast_type = A.OptimizeQuery

    def run(self) -> QueryResult:
        node: A.OptimizeQuery = self.node
        database = node.database or self.ctx.database
        table = self.ctx.catalog.get_table(database, node.table)
        if table.engine is not None:
            table.engine.optimize(final=node.final, deduplicate=node.deduplicate,
                                  partition=_literal_value(node.partition))
        return self._rows_result([], [])


class InterpreterRenameQuery(IInterpreter):
    ast_type = A.RenameQuery

    def run(self) -> QueryResult:
        node: A.RenameQuery = self.node
        for src, dst in node.pairs:
            sdb = src.database or self.ctx.database
            ddb = dst.database or sdb
            self.ctx.catalog.rename_table(sdb, src.table, ddb, dst.table)
        return self._rows_result([], [])


class InterpreterTruncateQuery(IInterpreter):
    ast_type = A.TruncateQuery

    def run(self) -> QueryResult:
        node: A.TruncateQuery = self.node
        database = node.database or self.ctx.database
        table = self.ctx.catalog.try_get_table(database, node.table)
        if table is None:
            if node.if_exists:
                return self._rows_result([], [])
            raise_error(ErrorCode.UNKNOWN_TABLE,
                        f"Table {database}.{node.table} does not exist")
        if table.engine is not None:
            table.engine.truncate()
        return self._rows_result([], [])


class InterpreterShowQuery(IInterpreter):
    """``SHOW ...`` —— 全部实现成对 ``system`` 表的查询。"""

    ast_type = A.ShowQuery

    def run(self) -> QueryResult:
        node: A.ShowQuery = self.node
        kind = node.kind
        if kind == "TABLES":
            return self._show_tables(node)
        if kind == "DATABASES":
            return self._show_databases(node)
        if kind == "CREATE":
            return self._show_create(node)
        if kind == "COLUMNS":
            return self._show_columns(node)
        if kind == "SETTINGS":
            return self._show_settings(node)
        if kind == "FUNCTIONS":
            return self._show_functions(node)
        if kind == "PROCESSLIST":
            return self._query_system("processes", node)
        if kind == "CLUSTERS":
            return self._query_system("clusters", node)
        if kind == "ENGINES":
            return self._query_system("table_engines", node)
        if kind == "DICTIONARIES":
            return self._query_system("dictionaries", node)
        if kind == "GRANTS":
            from ..access import get_access_control

            # ``SHOW GRANTS FOR u`` 里的目标用户被解析进 ``node.name``。
            # 早期版本硬编码成"当前用户",于是 ``SHOW GRANTS FOR u``
            # 会返回当前用户的权限 —— 一个**看起来成功、其实答非所问**的错。
            target = node.name or self.ctx.user
            ac = get_access_control(self.ctx)
            lines = ac.show_grants(target)
            header = f"GRANTS FOR {target}"
            return self._rows_result(
                [(header, "String")], ((line,) for line in lines)
            )
        if kind in ("USERS", "ROLES", "QUOTAS", "ACCESS"):
            return self._query_system(kind.lower(), node)
        if kind == "MERGES":
            return self._query_system("merges", node)
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"SHOW {kind} is not implemented")

    def _show_tables(self, node: A.ShowQuery) -> QueryResult:
        database = node.database or self.ctx.database
        names = []
        db = self.ctx.catalog.try_get_database(database)
        if db is not None:
            names = db.table_names()
        if node.like:
            names = [n for n in names if _like_match(node.like, n)]
        return self._rows_result(
            [("name", "String")], ((n,) for n in names)
        )

    def _show_databases(self, node: A.ShowQuery) -> QueryResult:
        names = self.ctx.catalog.database_names()
        if node.like:
            names = [n for n in names if _like_match(node.like, n)]
        return self._rows_result([("name", "String")], ((n,) for n in names))

    def _show_columns(self, node: A.ShowQuery) -> QueryResult:
        database = node.database or self.ctx.database
        table = self.ctx.catalog.get_table(database, node.table)
        rows = [
            (c.name, c.type.get_name(), c.default_kind or "", c.comment or "")
            for c in table.columns
        ]
        return self._rows_result(
            [("name", "String"), ("type", "String"), ("default_type", "String"),
             ("comment", "String")],
            rows,
        )

    def _show_create(self, node: A.ShowQuery) -> QueryResult:
        database = node.database or self.ctx.database
        table = self.ctx.catalog.get_table(database, node.table)
        sql = table.metadata_sql or _render_create(table)
        return self._rows_result([("statement", "String")], [(sql,)])

    def _show_settings(self, node: A.ShowQuery) -> QueryResult:
        rows = [
            (r[0], r[1], r[2], r[3], r[7]) for r in self.ctx.settings.as_rows()
        ]
        if node.changed:
            changed = self.ctx.settings.changed()
            rows = [r for r in rows if r[0] in changed]
        return self._rows_result(
            [("name", "String"), ("value", "String"), ("changed", "UInt8"),
             ("description", "String"), ("default", "String")],
            [(a, str(b), c, d, str(e)) for a, b, c, d, e in rows],
        )

    def _show_functions(self, node: A.ShowQuery) -> QueryResult:
        from ..functions.base import registered_functions

        name = node.like
        rows = [(n,) for n, _, _ in registered_functions()
                if not name or _like_match(name, n)]
        return self._rows_result([("name", "String")], rows)

    def _query_system(self, table_name: str, node: A.ShowQuery) -> QueryResult:
        from ..sql import parse as _parse  # 延迟导入:避免前端 ↔ 执行层的循环

        sql = f"SELECT * FROM system.{table_name}"
        return InterpreterSelectQuery(self.ctx, _parse(sql)).run()


class InterpreterDescribeQuery(IInterpreter):
    ast_type = A.DescribeQuery

    def run(self) -> QueryResult:
        node: A.DescribeQuery = self.node
        if node.subquery is not None:
            result = InterpreterSelectQuery(self.ctx, node.subquery).run()
            # 同样不能写 ``header or Block()``:``Block.__len__`` 是**行数**,
            # ``DESCRIBE (SELECT 1 AS x WHERE 0)`` 的表头恰好零行,
            # 会被判成"假"而整块丢掉 —— 输出变成空的。
            header = result.header
            if header is None:
                header = Block()
            rows = [(c.name, c.type.get_name()) for c in header.data]
        elif node.table_function is not None:
            from ..storages.table_functions import get_table_function

            fn = get_table_function(node.table_function.name)
            args = [_literal_value(a) for a in node.table_function.args]
            rows = [(c.name, c.type.get_name()) for c in fn.get_columns(args)]
        else:
            database = node.database or self.ctx.database
            table = self.ctx.catalog.get_table(database, node.table)
            described = table.describe_rows()
            # ``DESCRIBE TABLE`` 在真实 ClickHouse 里输出**七列**:
            #   name / type / default_type / default_expression /
            #   comment / codec_expression / ttl_expression
            # 早期版本只输出了前两列 —— 于是读者按教程去查
            # ``default_type`` 时会发现"没有这一列",而文档里明明写着有。
            # 元数据一直都在 ``catalog.describe_row()`` 里,只是没接到输出上。
            return self._rows_result(
                [
                    ("name", "String"),
                    ("type", "String"),
                    ("default_type", "String"),
                    ("default_expression", "String"),
                    ("comment", "String"),
                    ("codec_expression", "String"),
                    ("ttl_expression", "String"),
                ],
                [
                    (
                        r["name"], r["type"], r["default_type"], r["default_expression"],
                        r["comment"], r["codec_expression"], r["ttl_expression"],
                    )
                    for r in described
                ],
            )
        return self._rows_result([("name", "String"), ("type", "String")], rows)


class InterpreterExplainQuery(IInterpreter):
    """``EXPLAIN`` —— 见 :mod:`minich.execution.explain`。"""

    ast_type = A.ExplainQuery

    def run(self) -> QueryResult:
        from .explain import explain_query

        rows = explain_query(self.ctx, self.node)
        return self._rows_result([("explain", "String")], ((r,) for r in rows))


class InterpreterSetQuery(IInterpreter):
    ast_type = A.SetQuery

    def run(self) -> QueryResult:
        node: A.SetQuery = self.node
        scope = {"GLOBAL": "server", "SESSION": "session", "LOCAL": "query"}.get(
            node.scope, "session"
        )
        for name, value in node.items:
            if value is None:
                self.ctx.settings.reset(name) if hasattr(self.ctx.settings, "reset") \
                    else self.ctx.settings.set(name, None)
                continue
            self.ctx.settings.set(name, value, scope=scope)
        return self._rows_result([], [])


class InterpreterUseQuery(IInterpreter):
    ast_type = A.UseQuery

    def run(self) -> QueryResult:
        self.ctx.set_current_database(self.node.database)
        return self._rows_result([], [])


class InterpreterExistsQuery(IInterpreter):
    ast_type = A.ExistsQuery

    def run(self) -> QueryResult:
        node: A.ExistsQuery = self.node
        database = node.database or self.ctx.database
        exists = self.ctx.catalog.has_table(database, node.table)
        return self._rows_result([("result", "UInt8")], [(1 if exists else 0,)])


class InterpreterSystemQuery(IInterpreter):
    ast_type = A.SystemQuery

    def run(self) -> QueryResult:
        node: A.SystemQuery = self.node
        command = node.command.upper()
        if command.startswith("FLUSH LOGS"):
            return self._rows_result([], [])
        if command.startswith("RELOAD DICTIONAR"):
            from ..dictionaries import reload_dictionaries

            reload_dictionaries(self.ctx, node.table)
            return self._rows_result([], [])
        if command.startswith("STOP MERGES"):
            self._for_engine(node, "stop_merges")
            return self._rows_result([], [])
        if command.startswith("START MERGES"):
            self._for_engine(node, "start_merges")
            return self._rows_result([], [])
        if command.startswith("SYNC REPLICA") or command.startswith("RESTART REPLICA"):
            return self._rows_result([], [])
        if command.startswith("DROP"):
            return self._rows_result([], [])
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"SYSTEM {node.command} is not implemented in mini-clickhouse",
        )

    def _for_engine(self, node: A.SystemQuery, method: str) -> None:
        if node.table:
            database = node.database or self.ctx.database
            table = self.ctx.catalog.get_table(database, node.table)
            if table.engine is not None:
                getattr(table.engine, method)()
            return
        for table in self.ctx.catalog.all_tables():
            if table.engine is not None and hasattr(table.engine, method):
                getattr(table.engine, method)()


class InterpreterKillQuery(IInterpreter):
    ast_type = A.KillQuery

    def run(self) -> QueryResult:
        # mini-clickhouse 单线程执行,没有"取消别的查询"这回事。
        # 真实 ClickHouse 会设置一个原子标志,让执行线程在下一次检查时退出。
        return self._rows_result([("kill_status", "String")], [])


class InterpreterGrantQuery(IInterpreter):
    ast_type = A.GrantQuery

    def run(self) -> QueryResult:
        from ..access import get_access_control

        get_access_control(self.ctx).apply_grant(self.node)
        return self._rows_result([], [])


# ══════════════════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════════════════


def _register_all() -> None:
    register_interpreter(A.SelectQuery, InterpreterSelectQuery)
    register_interpreter(A.InsertQuery, InterpreterInsertQuery)
    register_interpreter(A.CreateTableQuery, InterpreterCreateQuery)
    register_interpreter(A.CreateDatabaseQuery, InterpreterCreateQuery)
    register_interpreter(A.CreateViewQuery, InterpreterCreateQuery)
    register_interpreter(A.CreateDictionaryQuery, InterpreterCreateQuery)
    register_interpreter(A.CreateFunctionQuery, InterpreterCreateQuery)
    register_interpreter(A.CreateUserQuery, InterpreterCreateQuery)
    register_interpreter(A.DropQuery, InterpreterDropQuery)
    register_interpreter(A.AlterQuery, InterpreterAlterQuery)
    register_interpreter(A.OptimizeQuery, InterpreterOptimizeQuery)
    register_interpreter(A.RenameQuery, InterpreterRenameQuery)
    register_interpreter(A.TruncateQuery, InterpreterTruncateQuery)
    register_interpreter(A.ShowQuery, InterpreterShowQuery)
    register_interpreter(A.DescribeQuery, InterpreterDescribeQuery)
    register_interpreter(A.ExplainQuery, InterpreterExplainQuery)
    register_interpreter(A.SetQuery, InterpreterSetQuery)
    register_interpreter(A.UseQuery, InterpreterUseQuery)
    register_interpreter(A.ExistsQuery, InterpreterExistsQuery)
    register_interpreter(A.SystemQuery, InterpreterSystemQuery)
    register_interpreter(A.KillQuery, InterpreterKillQuery)
    register_interpreter(A.GrantQuery, InterpreterGrantQuery)


_register_all()


# ══════════════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════════════


def _call_read(engine: Any, ctx: Any, read_ctx: ReadContext, final: bool = False):
    """调用存储引擎的 ``read`` —— 兼容几种可能的签名。

    真实 ClickHouse 的接口是固定的 ``read(QueryPlan &, SelectQueryInfo &, ...)``,
    不会有这种兼容问题。我们这么做是因为 :class:`IStorage` 一旦被
    多个引擎实现,签名的小差异就会暴露出来 —— 与其让调用点猜,
    不如用 ``inspect`` 明确地适配一次,并把差异集中在**一个**地方。
    """
    read_ctx.final = final
    sig = inspect.signature(engine.read)
    params = list(sig.parameters)
    if "read_ctx" in params:
        return engine.read(ctx, read_ctx)
    if "ctx" in params and len(params) == 2:
        return engine.read(ctx)
    if len(params) == 1:
        return engine.read(ctx)
    # 默认:按关键字传
    return engine.read(ctx, columns=read_ctx.columns, key_analysis=read_ctx.key_analysis,
                       partition_ids=read_ctx.partition_ids, limit=read_ctx.limit,
                       final=final, sample=read_ctx.sample, settings=read_ctx.settings)


def _drive(transform: Any, chunk: Chunk) -> Optional[Block]:
    """把一个 chunk 喂给聚合处理器,榨出它的输出。

    这是"分段执行"的具体体现:我们把聚合拆成
    "逐个 chunk 累计状态" + "最后产出一次",而不是让它成为一个
    真正的流式处理器。**语义完全相同**,但内存峰值不同(见模块文档)。
    """
    transform._accumulate(chunk) if hasattr(transform, "_accumulate") else None
    return None


def _drive_aggregating(transform: Any, block: Block) -> Optional[Block]:
    """把"状态 Block"喂给合并处理器,拿到最终 Block。"""
    chunk = block_to_chunk(block.clone_with_columns())
    if hasattr(transform, "_merge"):
        transform._merge(chunk)
    result = transform._emit() if hasattr(transform, "_emit") else None
    if result is None:
        return None
    return result.get_block_with_data()


def _substitute_window_nodes(analysis: QueryAnalysis, blocks: List[Block]) -> None:
    """把 SELECT/ORDER BY/HAVING 里的窗口函数节点**就地替换**成列引用。

    窗口函数算完之后,它的结果已经是 block 里的一列(名字是
    ``__window_N``)。但表达式树上仍然写着 ``row_number() OVER (...)`` ——
    求值器遇到它会直接报错("必须由 WindowTransform 求值")。

    所以要把它换掉。真实 ClickHouse 也在**分析期**做等价的事:
    它把 ``ASTFunction`` 的窗口计算改写成一个"读窗口结果列"的引用。
    区别是我们**事后**替换(因为要等到知道结果列名),真实实现
    **事前**规划(所以它能在计划里看到窗口节点)。

    用 ``id()`` 做匹配的依据:窗口节点对象在 ``analysis.window_names``
    与表达式树里是**同一个对象**(分析期从同一棵树里收集出来的)。
    """
    mapping = {id(wf): name for wf, name in analysis.window_names}
    # **根节点也要能替换** —— 见 :func:`_replace_window_in`。
    for i, node in enumerate(analysis.select_exprs):
        analysis.select_exprs[i] = _replace_window_in(node, mapping)
    if analysis.having is not None:
        analysis.having = _replace_window_in(analysis.having, mapping)
    for e in analysis.order_by:
        e.expr = _replace_window_in(e.expr, mapping)


def _replace_window_in(node: Optional[A.Node], mapping: Dict[int, str]) -> Optional[A.Node]:
    """递归地把树里的窗口节点替换成 ``Identifier`` —— **含根节点本身**。

    ``SELECT v, row_number() OVER (...) AS rn ... ORDER BY rn`` 是最常见的
    写法:``ORDER BY`` 引用的是窗口函数的**别名**。别名在分析期被展开回
    那个 ``WindowFunction`` 节点,于是 ``order_by[i].expr`` **本身就是**
    窗口节点(裸写窗口函数时 ``select_exprs[i]`` 同理)。

    早先的实现只对**子节点**做替换(``_map_children``),根节点被漏掉 ——
    求值器随后撞上 ``WindowFunction`` 并报
    "must be evaluated by the WindowTransform (NOT_IMPLEMENTED)"。
    这里先判根、再递归子节点,覆盖两种写法。
    """
    if node is None:
        return None
    if isinstance(node, A.WindowFunction) and id(node) in mapping:
        return A.Identifier(name=mapping[id(node)], pos=node.pos)
    from .analyzer import _map_children

    _map_children(node, lambda c: _rewrite_window_child(c, mapping))
    return node


def _rewrite_window_child(node: A.Node, mapping: Dict[int, str]) -> A.Node:
    if isinstance(node, A.WindowFunction) and id(node) in mapping:
        return A.Identifier(name=mapping[id(node)], pos=node.pos)
    from .analyzer import _map_children

    _map_children(node, lambda c: _rewrite_window_child(c, mapping))
    return node


def _project_block(block: Block, pre_columns: int,
                   output_names: Optional[Sequence[str]]) -> Block:
    """取 ``block`` 里第 ``pre_columns`` 列之后的部分,并重命名。

    ``pre_columns`` 之后的列就是"这次算出来的表达式",顺序与
    ``select_exprs`` 一一对应。名字换成用户看到的名字
    (别名优先,否则用表达式文本)—— ``PrettyCompact`` 的表头、
    ``JSONEachRow`` 的键名都由它决定。
    """
    if pre_columns >= block.columns():
        return block
    tail = block.data[pre_columns:]
    names = list(output_names) if output_names else [c.name for c in tail]
    if len(names) != len(tail):
        names = [c.name for c in tail]
    out = Block()
    for name, c in zip(names, tail):
        out.add_column(ColumnWithTypeAndName(name, c.type, c.column))
    return out


def _make_index_description(ast_index: A.IndexDefinition) -> Any:
    """把语法树的 ``INDEX`` 定义转成引擎认识的索引描述。

    .. code-block:: text

       AST 层(语法)                    存储层(建索引要用)
       IndexDefinition                  IndexDescription
         name: str                        name: str
         expr: Node                       expr: Node          ← 同一个 AST,直接传
         type: "minmax"                   index_type: "minmax"  ← 名字不同
         type_params: [0.01]              params: [0.01]
         granularity: 4                   granularity: 4
                                          data_type: None     ← 引擎自己算

    **为什么要转这一层?** 因为语法树属于 SQL 前端,而索引是存储层的
    概念 —— 两者共用一个类会让前端依赖存储层(反过来的依赖才正常)。
    真实 ClickHouse 也是分开的:``ASTIndexDeclaration`` 在
    ``InterpreterCreateQuery`` 里被翻译成 ``IndexDescription``。

    ⚠️ 早期版本**直接把 AST 对象塞给引擎**,于是引擎去读
    ``d.data_type`` 时抛 ``AttributeError: 'IndexDefinition' object has
    no attribute 'data_type'`` —— 表现是"建带跳数索引的表直接失败"。
    """
    try:
        from ..storages.merge_tree.skip_index import IndexDescription

        return IndexDescription(
            name=ast_index.name,
            expr_sql=ast_index.expr.to_sql() if ast_index.expr is not None else "",
            expr=ast_index.expr,
            index_type=ast_index.type,
            params=list(ast_index.type_params or []),
            granularity=int(ast_index.granularity or 1),
            data_type=None,
        )
    except ImportError:  # pragma: no cover - 引擎未装载时的降级
        class _Fallback:
            def __init__(self) -> None:
                self.name = ast_index.name
                self.expr_sql = ast_index.expr.to_sql() if ast_index.expr is not None else ""
                self.expr = ast_index.expr
                self.index_type = ast_index.type
                self.params = list(ast_index.type_params or [])
                self.granularity = int(ast_index.granularity or 1)
                self.data_type = None

        return _Fallback()


def _block_scope(block: Optional[Block]) -> Dict[str, ColumnWithTypeAndName]:
    if block is None:
        return {}
    return {
        c.name: ColumnWithTypeAndName(c.name, c.type, c.column)
        for c in block.data
    }


def _prefix_block(block: Block, alias: str) -> Block:
    """给子查询的列加上别名前缀 —— ``SELECT s.x FROM (SELECT 1 AS x) AS s``。"""
    out = Block(info=block.info.clone())
    for c in block.data:
        out.add_column(ColumnWithTypeAndName(f"{alias}.{c.name}", c.type, c.column))
    return out


def _apply_distinct(blocks: List[Block], start: int = 0) -> List[Block]:
    """``DISTINCT`` —— **只按第 ``start`` 列之后的那些列去重**。

    ``SELECT DISTINCT user FROM t ORDER BY ts`` 里,``ts`` 是被 ORDER BY
    需要的辅助列。如果按**全部**列去重,``ts`` 不同就会让同一个 user
    出现多次 —— 结果静默变多。所以去重必须限定在"用户选中的那几列"上。

    真实 ClickHouse 的 ``DistinctTransform`` 同样只拿 SELECT 的列做键。
    """
    if not blocks:
        return []
    merged = blocks[0].clone_empty()
    for b in blocks:
        if merged.is_empty():
            b0 = b.clone_empty()
            for c in b0.data[:start]:
                merged.add_column(c)
        merged.merge(b)
    seen: set = set()
    mask: List[bool] = []
    key_cols = merged.data[start:]
    for row in range(merged.rows()):
        key = tuple(_hashable_value(c.column.get(row)) for c in key_cols)
        if key in seen:
            mask.append(False)
        else:
            seen.add(key)
            mask.append(True)
    merged.filter(mask)
    return [merged] if merged.rows() else []


def _hashable_value(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable_value(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable_value(v)) for k, v in value.items()))
    return value


def _block_bytes(block: Block) -> int:
    return sum(c.column.byte_size() for c in block.data)


def _coerce(dtype: DataType, value: Any) -> Any:
    """按列类型把用户给的值规范化,失败就**抛错**。

    .. warning::

       早期版本写成 ``except MiniChError: return value`` —— 把正确的
       类型错误**吞掉、返回原值**。后果是静默写入非法数据:

       * ``INSERT INTO t(Enum8('a'=1)) VALUES ('5')`` 会写进一个越界的
         枚举值 5(而不是报 ``UNKNOWN_ELEMENT_OF_ENUM``);
       * ``INSERT INTO u8 VALUES ('999')`` 会被 NumPy 回绕成 231;
       * 有些类型则会漏出**裸的 ``ValueError``**(不是 ``MiniChError``),
         于是调用方按错误码处理的逻辑全部失效。

       "报错"和"写进去一个错的值"之间,前者永远更便宜。
    """
    if value is None and not dtype.is_nullable():
        # ``INSERT INTO t(x Int32) VALUES (NULL)`` —— 真实 ClickHouse 抛
        # Code 70 ``CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN``。不做这个前置判断
        # 的话,``dtype.normalize(None)`` 会漏出**裸的 ``TypeError``**
        # (``int(None)``),调用方按错误码处理的逻辑全部失效。
        raise_error(
            ErrorCode.CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN,
            f"Cannot convert NULL value to non-Nullable type {dtype.get_name()}",
        )
    return dtype.normalize(value)


def _convert_column(source: ColumnWithTypeAndName, target: DataType) -> Any:
    """把一列转成目标类型 —— 走 ``CAST`` 函数(与真实 ClickHouse 一致)。"""
    if source.type.get_name() == target.get_name():
        return source.column
    from ..functions.base import execute_with_null_propagation, get_function

    try:
        fn = get_function("cast", [target.get_name()], [source.type])
        col, _ = execute_with_null_propagation(None or fn, [source], source.column.size(), None)
        return col
    except MiniChError:
        return source.column


def _fill_default(dtype: DataType, rows: int) -> Any:
    col = dtype.create_column()
    col.append_many_default(rows)
    return col


def _eval_default_column(ctx: Any, col_def: ColumnDefinition, block: Block) -> Any:
    """按列的 ``DEFAULT`` / ``MATERIALIZED`` 表达式算出一整列(共 ``block.rows()`` 行)。

    ``INSERT`` 缺列、``ALTER ADD COLUMN`` 回填都走**同一份**实现 ——
    两个入口用不同逻辑是"同一语义两种结果"的经典来源。
    解析/求值失败时退回**类型零值**(与真实 ClickHouse 的容错一致)。
    """
    if not col_def.default_expr_sql:
        return _fill_default(col_def.type, block.rows())
    from ..sql import parse_expression_text

    try:
        expr = parse_expression_text(col_def.default_expr_sql)
    except MiniChError:
        return _fill_default(col_def.type, block.rows())
    scope = Scope(_block_scope(block))
    ev = Evaluator(ctx, scope, block.rows())
    try:
        col = ev.evaluate(expr)
        return _convert_column(col, col_def.type)
    except MiniChError:
        return _fill_default(col_def.type, block.rows())


def _infer_value_type(value: Any) -> DataType:
    from .expression import infer_literal_type

    return infer_literal_type(value)


def _literal_value(node: Optional[A.Node]) -> Any:
    if node is None:
        return None
    if isinstance(node, A.Literal):
        return node.value
    if isinstance(node, A.Identifier):
        return node.name
    return node.to_sql() if hasattr(node, "to_sql") else None


def _const_int(ctx: Context, node: Optional[A.Node]) -> Optional[int]:
    if node is None:
        return None
    if isinstance(node, A.Literal) and isinstance(node.value, int):
        return node.value
    try:
        ev = Evaluator(ctx, Scope(), 1)
        v = ev.evaluate(node).column.get(0)
        return int(v) if v is not None else None
    except MiniChError:
        return None


def _int_setting(ctx: Context, name: str, default: int) -> int:
    try:
        return int(ctx.settings.try_get(name, default))
    except Exception:
        return default


def _like_match(pattern: str, value: str) -> bool:
    """把 SQL 的 ``LIKE`` 模式转成正则 —— ``SHOW TABLES LIKE 'a%'`` 用。"""
    import re

    regex = "^" + re.escape(pattern).replace("%", ".*").replace("_", ".") + "$"
    return re.match(regex, value, re.IGNORECASE) is not None


def _render_create(table: Table) -> str:
    cols = ", ".join(f"`{c.name}` {c.type.get_name()}" for c in table.columns)
    return f"CREATE TABLE {table.full_name()} ({cols}) ENGINE = ..."


def _is_system_table(ctx: Context, name: str) -> bool:
    """判断一个**没写库名前缀**的表名是不是 system 表。

    ``FROM system.tables`` 走的是显式前缀那条路;但
    ``SELECT * FROM numbers(10)`` 这种没写库名的名字要先在这里筛一下,
    否则会被当成当前库里的普通表,报 ``UNKNOWN_TABLE``。
    """
    try:
        from ..system import is_system_table
    except ImportError:  # pragma: no cover - system 包未装载时的降级
        return False
    return is_system_table(name)


def _rollup_or_cube_sets(query: A.SelectQuery, n: int) -> Optional[List[List[int]]]:
    """把 ``GROUP BY … WITH ROLLUP/WITH CUBE`` 展开成一组**键下标子集**。

    * 没有 ROLLUP/CUBE → ``None``(调用方走原来的单次聚合路径);
    * ``WITH ROLLUP`` → 前缀 ``[0..n-1]``、``[0..n-2]``、…、``[]``
      (逐级"去掉最后一个键",最后是全集的总计行);
    * ``WITH CUBE`` → **所有** ``2^n`` 个子集。

    子集里的数字是 ``analysis.key_exprs`` 的**下标**。返回顺序即真实
    ClickHouse 的输出顺序:先完整的组,再按"势"从大到小,势相同按下标升序
    (``CUBE(a,b)`` → ``(a,b) (a) (b) ()``)。
    """
    gb = query.group_by
    if gb is None or not (gb.with_rollup or gb.with_cube):
        return None
    if gb.with_rollup:
        return [list(range(k)) for k in range(n, -1, -1)]
    subsets: List[List[int]] = [
        [i for i in range(n) if mask & (1 << i)] for mask in range(1 << n)
    ]
    subsets.sort(key=lambda s: (-len(s), s))
    return subsets


def _pad_grouping_block(sub: Block, subset: Sequence[int], base: Block) -> Block:
    """把某个键子集的聚合块补齐成与 ``base`` 相同的列结构。

    子集块的前 ``len(subset)`` 列是子集键列,其余是聚合(状态)列。缺掉的键列
    按真实 ClickHouse 的规则填**类型默认值**(``0`` / 空串 / ``''``);
    列名与类型取自 ``base`` 里同下标的键列,保证各块结构一致 —— 这样下游的
    表达式求值 / ``ORDER BY`` / 投影才会把所有子集的行当成同一个结果集。
    """
    n_aggs = len(sub.data) - len(subset)
    n_keys = len(base.data) - n_aggs
    rows = sub.rows()
    cols: List[ColumnWithTypeAndName] = []
    k = 0
    for i in range(n_keys):
        if i in subset:
            cols.append(sub.data[k])
            k += 1
            continue
        ref = base.data[i]
        col = ref.type.create_column()
        for _ in range(rows):
            col.append_default()
        cols.append(ColumnWithTypeAndName(ref.name, ref.type, col))
    cols.extend(sub.data[k:])
    return Block(cols, sub.info.clone())


def _append_totals_row(block: Block, analysis: QueryAnalysis,
                       agg_descs: Sequence[AggregateDescription]) -> Block:
    """给 ``WITH TOTALS`` 的结果追加一行总计。

    做法:把**所有分组的状态**再合并一次,得到"全局那一份状态",
    然后拼成一行 —— 键列填类型默认值、状态列填合并结果。

    为什么在**状态层**合并而不是"把结果行再加一遍"? 因为结果行是
    已经 finalize 过的(``avg`` 变成了一个平均值),再加一遍是错的;
    而状态是可合并的(``avg`` 的状态是 ``(和, 计数)``)。
    这正是"两阶段聚合"这套设计的价值在 WITH TOTALS 上的体现 ——
    真实 ClickHouse 也是这么算总计行的(它用的是同一个聚合器的
    ``merge`` 路径)。
    """
    n_keys = len(analysis.key_exprs)
    if block.columns() <= n_keys:
        return block
    totals: List[ColumnWithTypeAndName] = []
    for i in range(n_keys):
        c = block.data[i]
        col = c.type.create_column()
        col.append_default()
        totals.append(ColumnWithTypeAndName(c.name, c.type, col))
    for j, agg in enumerate(agg_descs):
        src = block.data[n_keys + j]
        # 第一组的状态**克隆**一份当累加器,再从**第二组**开始合并 ——
        # 从 0 开始合并会把第一组算两遍(``sum`` 的值翻倍、``count`` 多一半),
        # 而且不报错:总计行看起来很正常,只是数字不对。
        acc = agg.func.clone_state(src.column.get(0))
        for r in range(1, block.rows()):
            agg.func.merge(acc, src.column.get(r))
        col = src.type.create_column()
        col.append(acc)
        totals.append(ColumnWithTypeAndName(src.name, src.type, col))
    # 直接构造 Block,不要走 ``clone_empty() + add_column()`` ——
    # 后者会在"0 行块上追加 1 行列"时报 SIZES_OF_COLUMNS_DOESNT_MATCH
    # (clone_empty 产出的块行数是 0,而这一行的每一列都是 1 行)。
    out = Block(totals, block.info.clone())
    block.merge(out)
    return block
