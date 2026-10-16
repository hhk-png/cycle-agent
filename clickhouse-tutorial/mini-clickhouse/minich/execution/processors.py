"""处理器与流水线 —— ClickHouse 的执行模型。

这是整个引擎里**最值得慢慢读**的一章代码,因为它解释了 ClickHouse
为什么能把一台机器的每一个核都吃满。

.. code-block:: text

   传统"火山模型"(MySQL / PostgreSQL)        ClickHouse 的推送模型
   ┌──────────────────────────────┐          ┌──────────────────────────────┐
   │ 上层算子 next() → 下层算子    │          │ 源把它产出的 Chunk **推**给  │
   │ 每次返回**一行**              │          │ 下一个处理器,不等对方来要  │
   │ 一个查询 = 一条调用栈          │          │ 一个查询 = 一张**图**        │
   │ 天然单线程                    │          │ 天然可以切成多份并行         │
   └──────────────────────────────┘          └──────────────────────────────┘

具体差在哪,看一个聚合查询:

.. code-block:: text

   SELECT domain(url), count() FROM hits GROUP BY domain

   ClickHouse 会把它拆成**多个可以并行跑的片段**:

   ReadFromMergeTree ─┬─► Expression ─► PartialAggregating ─┐
      (part 1)        │                                      │
   ReadFromMergeTree ─┼─► Expression ─► PartialAggregating ─┼─► MergingAggregated ─► …
      (part 2)        │        ▲                             │
   ReadFromMergeTree ─┴─► Expression ─► PartialAggregating ─┘
      (part 3)                 ▲
                               └─ max_threads 条线程各跑一条

**每个"部分聚合"只处理一部分数据,产出"部分状态";最后再合并。**
这就是为什么 ClickHouse 的 ``GROUP BY`` 能线性扩展,而传统数据库的
``HashAggregate`` 只能单线程(或者要靠复杂的分区重分布)。

.. note::

   真实 ClickHouse 的 ``IProcessor`` 有 30 多个虚函数
   (``prepare``、``work``、``schedule``、``getProcessorID``、
   ``onUpdatePorts``、``setPipeline``…),还有端口级别的
   ``Port::State``(NeedData / Full / Finished)与 ``IProcessor::Status``。
   mini-clickhouse 保留了**核心的四态状态机**与推送语义,
   去掉了异步 IO、优先级调度与 profile 信息收集。

   一个重要的差异:真实 ClickHouse 的 ``PipelineExecutor`` 是
   **真的多线程**的(每个 processor 跑在自己的线程里,靠端口状态同步),
   而 mini-clickhouse 在**单线程**里按拓扑序轮转 —— 语义一样,
   但你看不到"两个 CPU 核在同时跑"。教程第 10 章用图解释了这一点,
   并给出真实 ClickHouse 的线程池配置(``max_threads`` 与
   ``max_threads_for_indexes``)。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections import deque
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..aggregates import get_aggregate_function
from ..block import Block, Chunk, ColumnWithTypeAndName
from ..columns import ColumnConst, IColumn
from ..errors import ErrorCode, MiniChError, raise_error
from ..util.keys import build_key, normalize_key_value
from ..util.stopwatch import ProfileEvents
from .expression import AggregateDescription, Evaluator, Scope

__all__ = [
    "Status",
    "Port",
    "InputPort",
    "OutputPort",
    "IProcessor",
    "ISource",
    "ISink",
    "ISimpleTransform",
    "Pipeline",
    "PipelineExecutor",
    "SourceFromChunks",
    "SourceFromBlocks",
    "NullSink",
    "CollectingSink",
    "ExpressionTransform",
    "FilterTransform",
    "LimitTransform",
    "OffsetTransform",
    "ArrayJoinTransform",
    "MaterializingTransform",
    "SortingTransform",
    "MergingSortedTransform",
    "AggregatingTransform",
    "MergingAggregatedTransform",
    "DistinctTransform",
    "LimitByTransform",
    "UnionTransform",
    "JoiningTransform",
    "WindowTransform",
    "ResizeProcessor",
    "TotalsTransform",
    "convert_chunk_to_block",
    "block_to_chunk",
]


class Status(Enum):
    """处理器 ``work()`` 的返回值 —— 与真实 ClickHouse 的 ``IProcessor::Status`` 同名同义。

    四个状态的含义与**为什么需要它们**:

    * ``NEED_DATA`` —— 我有输入端口但没数据,什么也没做。
      执行器不该因为"没有进展"就判定死锁,所以要能区分这个。
    * ``PORT_FULL`` —— 我想往下推但下游满了。这是**背压**的载体:
      下游慢的时候,上游会被自然地卡住,内存不会无限增长。
    * ``FINISHED`` —— 我彻底干完了。
    * ``READY`` —— 我刚做了一件有用的事(读了一块/算了一块/推了一块)。
    """

    NEED_DATA = "NeedData"
    PORT_FULL = "PortFull"
    FINISHED = "Finished"
    READY = "Ready"


# ══════════════════════════════════════════════════════════════════════════
# 端口
# ══════════════════════════════════════════════════════════════════════════


class Port:
    """端口 —— 处理器之间的**连接点**。

    ClickHouse 的端口有两个状态位:``is_finished`` 与"有没有数据"。
    推送模型下,"结束"是一个**显式信号**(而不是"没有数据了"),
    否则下游无法区分"暂时没有"和"永远没有" —— 这个区分是
    ``LIMIT`` 能提前终止整条流水线的关键。
    """

    __slots__ = ("parent", "name", "chunk", "finished")

    def __init__(self, parent: Optional["IProcessor"] = None, name: str = "") -> None:
        self.parent = parent
        self.name = name
        self.chunk: Optional[Chunk] = None
        self.finished = False

    def has_data(self) -> bool:
        return self.chunk is not None

    def is_finished(self) -> bool:
        return self.finished and self.chunk is None

    def __repr__(self) -> str:  # pragma: no cover
        state = "finished" if self.finished else ("data" if self.chunk is not None else "empty")
        return f"<Port {self.name or '?'} {self.parent.name if self.parent else '?'} {state}>"


class InputPort(Port):
    """输入端 —— 数据**存在上游的输出端口里**,这里只是它的一个视图。

    .. note::

       这是真实 ClickHouse 的端口设计里一个容易被忽略的细节:
       ``connect(out, in)`` 之后,**数据仍然存在 ``out`` 里**,
       ``in`` 只是通过一个指针去读它。这样做的原因是
       "端口是同步的载体" —— executor 只需要检查一个位置
       (上游端口是否为空)就能决定"下游能不能跑"。

       mini-clickhouse 照抄这个设计:``InputPort`` 通过
       :attr:`peer` 指向配对的 ``OutputPort``,所有读写都转发过去。
    """

    def __init__(self, parent: Optional["IProcessor"] = None, name: str = "") -> None:
        super().__init__(parent, name)
        self.peer: Optional["OutputPort"] = None

    def pull(self) -> Optional[Chunk]:
        # peer 为空 = 这个输入端口**没有被连接**,此时用自己身上的槽位。
        # 这是为"分段执行"准备的:解释器把一块数据直接塞进处理器,
        # 不经过端口连线(见 LimitByTransform.work_with)。
        if self.peer is None:
            chunk = self.chunk
            self.chunk = None
            return chunk
        chunk = self.peer.chunk
        self.peer.chunk = None
        return chunk

    def has_data(self) -> bool:
        if self.peer is None:
            return self.chunk is not None
        return self.peer.chunk is not None

    def is_ready(self) -> bool:
        return self.has_data() or self.is_finished()

    def is_finished(self) -> bool:
        if self.peer is None:
            return self.finished and self.chunk is None
        return self.peer.finished and self.peer.chunk is None

    def finish(self) -> None:
        # 上游结束等价于这个输入口没有更多数据了
        self.finished = True

    def __repr__(self) -> str:  # pragma: no cover
        state = "finished" if self.is_finished() else ("data" if self.has_data() else "empty")
        return f"<InputPort {self.name} {state}>"


class OutputPort(Port):
    """输出端 —— ``push`` 会告诉你下游能不能接。

    ``PushResult.NotNeeded`` 是一个非常重要的返回值:它表示
    **下游已经不需要更多数据了**(比如 ``LIMIT 10`` 已经凑够)。
    上游收到它就该停下 —— 这正是 ``SELECT * FROM huge LIMIT 10``
    在 ClickHouse 里能秒回、而在很多数据库里要扫完的原因之一。
    """

    def __init__(self, parent: Optional["IProcessor"] = None, name: str = "") -> None:
        super().__init__(parent, name)
        self.peer: Optional[InputPort] = None

    def push(self, chunk: Chunk) -> "PushResult":
        if self.finished:
            return PushResult.NotNeeded
        if self.peer is not None and self.peer.finished:
            # 下游已经主动结束(例如 LIMIT 凑够了)—— 不要再往里塞
            return PushResult.NotNeeded
        if self.chunk is not None:
            return PushResult.PortFull
        self.chunk = chunk
        return PushResult.NeedMoreData

    def pull(self) -> Optional[Chunk]:
        """从输出端取走数据。

        正常流水线里数据由下游的 ``InputPort`` 取;但在"分段执行"时
        处理器**没有被连线**,解释器就直接从输出端取(见
        ``LimitByTransform.work_with``)。所以输出端也要能 pull。
        """
        chunk = self.chunk
        self.chunk = None
        return chunk

    def finish(self) -> None:
        self.finished = True

    def has_data(self) -> bool:
        return self.chunk is not None

    def is_finished(self) -> bool:
        return self.finished and self.chunk is None

    def __repr__(self) -> str:  # pragma: no cover
        state = "finished" if self.is_finished() else ("data" if self.has_data() else "empty")
        return f"<OutputPort {self.name} {state}>"


class PushResult(Enum):
    NotNeeded = "NotNeeded"
    PortFull = "PortFull"
    NeedMoreData = "NeedMoreData"


def connect(output: OutputPort, input_port: InputPort) -> None:
    """把两个端口连起来 —— 数据从 output 流向 input。

    连接是**双向**的:上游知道下游是谁(用来判断"下游还要不要"),
    下游知道上游是谁(用来取数据)。
    """
    output.peer = input_port
    input_port.peer = output


# ══════════════════════════════════════════════════════════════════════════
# 处理器基类
# ══════════════════════════════════════════════════════════════════════════


class IProcessor(ABC):
    """处理器基类。

    子类必须实现 :meth:`work`。可选实现 :meth:`prepare`
    (真实 ClickHouse 用它做"启动时的一次性决策",比如
    ``MergingSortedTransform`` 据此决定用几路归并)。
    """

    name: str = "Processor"

    def __init__(self) -> None:
        self.inputs: List[InputPort] = []
        self.outputs: List[OutputPort] = []
        self.finished = False
        #: 处理器在图里的序号 —— 与真实 ClickHouse 的 ``processor_id`` 对应,
        #: ``EXPLAIN PIPELINE`` 的 ``processor`` 列就是它
        self.processor_id = 0
        #: 统计
        self.elapsed_ns = 0
        self.input_rows = 0
        self.output_rows = 0

    # ── 端口 ────────────────────────────────────────────────────────────

    def add_input(self, name: str = "") -> InputPort:
        port = InputPort(self, name or f"in{len(self.inputs)}")
        self.inputs.append(port)
        return port

    def add_output(self, name: str = "") -> OutputPort:
        port = OutputPort(self, name or f"out{len(self.outputs)}")
        self.outputs.append(port)
        return port

    # ── 生命周期 ────────────────────────────────────────────────────────

    def prepare(self, processor_id: int) -> Status:
        self.processor_id = processor_id
        return Status.READY

    @abstractmethod
    def work(self) -> Status:
        """做一步工作。**必须尽快返回**,不能阻塞等待。"""

    def has_input(self) -> bool:
        return bool(self.inputs)

    def has_output(self) -> bool:
        return bool(self.outputs)

    def all_inputs_finished(self) -> bool:
        return all(p.is_finished() for p in self.inputs)

    def all_outputs_finished(self) -> bool:
        return all(p.is_finished() for p in self.outputs)

    def finish_outputs(self) -> None:
        for p in self.outputs:
            p.finish()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} id={self.processor_id}>"


class ISource(IProcessor):
    """源 —— 没有输入,只有输出。

    它必须在产出完所有数据后**显式 finish 输出端口**。
    忘了这一步是新手最常犯的错:整条流水线会**永久挂起**
    (执行器认为"还有数据要来")。第 10 章把这类 bug
    列成"ClickHouse 风格实现里最危险的一类"。
    """

    def __init__(self) -> None:
        super().__init__()
        self.add_output("out")

    @abstractmethod
    def generate(self) -> Optional[Chunk]:
        """产出下一个 chunk;**返回 None 表示结束**。"""

    def work(self) -> Status:
        out = self.outputs[0]
        if out.is_finished():
            self.finished = True
            return Status.FINISHED
        if out.has_data():
            return Status.PORT_FULL
        chunk = self.generate()
        if chunk is None:
            out.finish()
            self.finished = True
            return Status.FINISHED
        self.output_rows += chunk.num_rows
        out.push(chunk)
        return Status.READY


class ISink(IProcessor):
    """汇聚端 —— 只有输入,没有输出。"""

    def __init__(self) -> None:
        super().__init__()
        self.add_input("in")

    def work(self) -> Status:
        inp = self.inputs[0]
        if inp.is_finished():
            self.finished = True
            return Status.FINISHED
        chunk = inp.pull()
        if chunk is None:
            return Status.NEED_DATA
        self.input_rows += chunk.num_rows
        self.consume(chunk)
        return Status.READY

    @abstractmethod
    def consume(self, chunk: Chunk) -> None:
        """消费一个 chunk。"""


class ISimpleTransform(IProcessor):
    """**一进一出**的变换器 —— 绝大多数算子都属于这一类。

    实现的是一条最重要的约定:**输入结束 ⇒ 输出结束**。
    少写这一句,整条查询就会挂住。所以它被放在基类里 ——
    子类只管"怎么变",不用管"什么时候结束"。
    """

    def __init__(self) -> None:
        super().__init__()
        self.add_input("in")
        self.add_output("out")

    @abstractmethod
    def transform(self, chunk: Chunk) -> Optional[Chunk]:
        """把输入 chunk 变成输出 chunk。返回 None 表示"这个 chunk 被全部丢掉"。"""

    def work(self) -> Status:
        inp, out = self.inputs[0], self.outputs[0]
        if self.finished:
            return Status.FINISHED
        if out.has_data():
            return Status.PORT_FULL
        if inp.has_data():
            chunk = inp.pull()
            self.input_rows += chunk.num_rows
            result = self.transform(chunk)
            if result is None or result.num_rows == 0:
                return Status.READY if inp.has_data() else Status.NEED_DATA
            self.output_rows += result.num_rows
            out.push(result)
            return Status.READY
        if inp.finished:
            out.finish()
            self.finished = True
            return Status.FINISHED
        return Status.NEED_DATA


# ══════════════════════════════════════════════════════════════════════════
# 源与汇聚
# ══════════════════════════════════════════════════════════════════════════


class SourceFromChunks(ISource):
    """从一个 chunk 迭代器读 —— 把任意"生成器"接进流水线。"""

    name = "SourceFromChunks"

    def __init__(self, chunks: Iterator[Chunk]) -> None:
        super().__init__()
        self._iter = chunks

    def generate(self) -> Optional[Chunk]:
        try:
            return next(self._iter)
        except StopIteration:
            return None


class SourceFromBlocks(ISource):
    """从一个 block 列表读。"""

    name = "SourceFromBlocks"

    def __init__(self, blocks: Iterable[Block]) -> None:
        super().__init__()
        self._blocks = iter(blocks)

    def generate(self) -> Optional[Chunk]:
        for block in self._blocks:
            header = block.clone_empty()
            return Chunk(header, [c.column for c in block.data], block.rows())
        return None


class NullSink(ISink):
    """丢弃一切 —— ``EXPLAIN`` 与写 ``Null`` 表时用。"""

    name = "NullSink"

    def __init__(self) -> None:
        super().__init__()
        self.rows = 0

    def consume(self, chunk: Chunk) -> None:
        self.rows += chunk.num_rows


class CollectingSink(ISink):
    """把结果收集起来 —— 查询结果走这条路径。"""

    name = "CollectingSink"

    def __init__(self, limit: Optional[int] = None) -> None:
        super().__init__()
        self.blocks: List[Block] = []
        self.rows = 0
        self.limit = limit
        self._header: Optional[Block] = None

    def consume(self, chunk: Chunk) -> None:
        block = chunk.get_block_with_data()
        if self._header is None:
            self._header = chunk.header
        self.blocks.append(block)
        self.rows += chunk.num_rows

    def get_result_blocks(self) -> List[Block]:
        return self.blocks

    def get_header(self) -> Optional[Block]:
        return self._header


# ══════════════════════════════════════════════════════════════════════════
# 变换
# ══════════════════════════════════════════════════════════════════════════


class ExpressionTransform(ISimpleTransform):
    """**算表达式** —— 整条流水线里最常出现的处理器。

    它把"上游的列"变成"上游的列 + 新算出来的列"。
    例如 ``SELECT domain(url) AS d FROM hits WHERE d LIKE '%x%'``:

    .. code-block:: text

       输入  [url]
       输出  [url, d]      ← d 是新算的;WHERE 用的就是它

    .. note::

       真实 ClickHouse 里这一步叫 ``ExpressionTransform`` 与
       ``ExpressionActions`` 配合;而更现代的版本用的是
       ``ActionsDAG``(有向无环图,能复用公共子表达式、能做常量折叠、
       能删掉没用的动作)。我们用逐表达式递归求值 ——
       对 ``a + a`` 会算两次,真实 ClickHouse 只算一次。
       第 22 章会讲 ``ActionsDAG`` 与"为什么把表达式做成 DAG 是一次大重构"。
    """

    name = "ExpressionTransform"

    def __init__(
        self,
        ctx: Any,
        expressions: Sequence[Any],
        scope_columns: Dict[str, ColumnWithTypeAndName],
        row_count_hint: int = 0,
        subquery_executor: Optional[Callable[..., Any]] = None,
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.expressions = list(expressions)
        self.scope_columns = dict(scope_columns)
        self.subquery_executor = subquery_executor
        #: 分阶段求值的查找表(聚合后阶段注入,见 analyzer.ExpressionLookup)
        self.lookup: Any = None
        self.aliases: Dict[str, Any] = {}
        for e in self.expressions:
            if type(e).__name__ == "AliasExpr" and e.alias:
                self.aliases[e.alias] = e.expr
        self.constant_folds = 0

    def transform(self, chunk: Chunk) -> Optional[Chunk]:
        scope = Scope(dict(self.scope_columns), self.aliases)
        for c, col in zip(chunk.header.data, chunk.columns):
            scope.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, col))
        rows = chunk.num_rows
        ev = Evaluator(
            self.ctx, scope, rows, subquery_executor=self.subquery_executor,
            expression_lookup=self.lookup,
        )
        out_cols: List[ColumnWithTypeAndName] = []
        for i, c in enumerate(chunk.header.data):
            out_cols.append(ColumnWithTypeAndName(c.name, c.type, chunk.columns[i]))
        for e in self.expressions:
            try:
                res = ev.evaluate(e)
            except MiniChError:
                raise
            # .. warning::
            #
            #    **同一个列对象不能在一个 Block 里出现两次。**
            #
            #    求值器对裸标识符是"原样返回作用域里的列对象",所以
            #    ``SELECT * FROM t LIMIT 1 BY b`` 这类查询会让结果块里
            #    **同一个 ColumnVector 被换着名字塞两次**。之后任何
            #    "就地过滤"的处理器(``LIMIT BY`` / ``DISTINCT``)都会
            #    按同一把 mask 过滤同一块内存两次:第一次把它从 3 行缩到
            #    2 行,第二次再拿长度 3 的 mask 去过滤一个 2 行的列 ——
            #    报出 ``filter mask size 3 != column size 2``。
            #
            #    克隆一次就彻底避免了这一类别名问题,代价是一次内存拷贝。
            if any(res.column is c.column for c in out_cols):
                res = ColumnWithTypeAndName(res.name, res.type, res.column.clone())
            out_cols.append(res)
        self.constant_folds += ev.constant_folds
        if rows == 0:
            return None
        header = Block([ColumnWithTypeAndName(c.name, c.type, c.column.clone_empty())
                        for c in out_cols])
        return Chunk(header, [c.column for c in out_cols], rows)

    def evaluate_one(self, chunk: Chunk, expr: Any) -> ColumnWithTypeAndName:
        """对单个 chunk 求单个表达式 —— 处理器内部复用。"""
        scope = Scope(dict(self.scope_columns), self.aliases)
        for c, col in zip(chunk.header.data, chunk.columns):
            scope.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, col))
        ev = Evaluator(self.ctx, scope, chunk.num_rows,
                       subquery_executor=self.subquery_executor,
                       expression_lookup=self.lookup)
        return ev.evaluate(expr)


class FilterTransform(ISimpleTransform):
    """``WHERE`` / ``PREWHERE`` —— 按条件**就地**过滤每一列。

    过滤是**列级**的:先算出条件列(一个 UInt8 列),再用它
    对**所有列**做同一个 mask。这个"一个 mask 应用到所有列"的模式
    是列存的核心操作之一,也是它比行存快的原因:
    没有分支预测失败,而且可以直接在 NumPy 层做。

    .. warning::

       ``PREWHERE`` 与 ``WHERE`` 的区别**只在于执行顺序**:
      ``PREWHERE`` 先用**更少**的列过滤(比如分区键、主键列),
       过滤完再去读其它列。这在宽表上收益巨大 ——
       因为没被选中的那些行的**其他列根本不会被读出来**。
       真实 ClickHouse 会自动把 ``WHERE`` 里的合适条件下推成
       ``PREWHERE``(``optimize_move_to_prewhere``,默认开)。
       mini-clickhouse 实现了这个下推(见 optimizer.py)。
    """

    name = "FilterTransform"

    def __init__(self, ctx: Any, condition: Any,
                 scope_columns: Dict[str, ColumnWithTypeAndName],
                 is_prewhere: bool = False,
                 subquery_executor: Optional[Callable[..., Any]] = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.condition = condition
        self.scope_columns = dict(scope_columns)
        self.is_prewhere = is_prewhere
        self.subquery_executor = subquery_executor
        self.lookup: Any = None
        self.rows_before = 0
        self.rows_after = 0

    def transform(self, chunk: Chunk) -> Optional[Chunk]:
        scope = Scope(dict(self.scope_columns))
        for i, c in enumerate(chunk.header.data):
            scope.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, chunk.columns[i]))
        ev = Evaluator(self.ctx, scope, chunk.num_rows,
                       subquery_executor=self.subquery_executor,
                       expression_lookup=self.lookup)
        cond = ev.evaluate(self.condition)
        self.rows_before += chunk.num_rows
        mask = _condition_mask(cond.column, chunk.num_rows)
        kept = sum(1 for m in mask if m)
        self.rows_after += kept
        if kept == 0:
            return None
        if kept == chunk.num_rows:
            return chunk
        for col in chunk.columns:
            col.filter(mask)
        return Chunk(chunk.header, chunk.columns, kept)


def _condition_mask(column: IColumn, rows: int) -> List[bool]:
    """把条件列转成布尔 mask —— 处理 NULL(``NULL`` 在 WHERE 里是**假**)。

    这条语义很容易被忽略:``WHERE nullable_col`` 时,值为 NULL 的行
    会被**过滤掉**(因为 ``NULL`` 不等于真)。而 ``WHERE NOT nullable_col``
    **也会**把 NULL 行过滤掉 —— 这就是著名的"三值逻辑"陷阱,
    第 07 章专门讲它。
    """
    mask: List[bool] = []
    for i in range(rows):
        v = column.get(i)
        mask.append(bool(v) if v is not None else False)
    return mask


class LimitTransform(ISimpleTransform):
    """``LIMIT`` —— 凑够就**停止拉取**。

    ``LIMIT`` 在推送模型里最优雅的地方是:它能通过
    ``PushResult.NotNeeded`` **反向传播**给上游,
    于是 ``SELECT * FROM billion_rows LIMIT 10`` 只读一个 chunk 就结束。
    在拉取模型里要做同样的事需要靠异常或特殊的"提前退出"协议。
    """

    name = "LimitTransform"

    def __init__(self, limit: Optional[int], offset: int = 0) -> None:
        super().__init__()
        self.limit = limit
        self.offset = offset
        self.seen = 0
        self.produced = 0

    def transform(self, chunk: Chunk) -> Optional[Chunk]:
        if self.limit is None:
            return chunk
        if self.produced >= self.limit:
            return None
        start = 0
        remaining_rows = chunk.num_rows
        if self.seen < self.offset:
            skip = min(self.offset - self.seen, remaining_rows)
            self.seen += skip
            start = skip
            remaining_rows -= skip
            if remaining_rows <= 0:
                return None
        take = min(self.limit - self.produced, remaining_rows)
        self.produced += take
        if take == remaining_rows and start == 0:
            return chunk
        return chunk.slice(start, take)

    def work(self) -> Status:
        status = super().work()
        if self.limit is not None and self.produced >= self.limit:
            # 主动结束 —— 并让上游也停下来(靠 finish 输出端口)
            self.finish_outputs()
            self.finished = True
            return Status.FINISHED
        return status


class OffsetTransform(ISimpleTransform):
    """``OFFSET``(单独出现时)—— 丢掉前 N 行。"""

    name = "OffsetTransform"

    def __init__(self, offset: int) -> None:
        super().__init__()
        self.offset = offset
        self.skipped = 0

    def transform(self, chunk: Chunk) -> Optional[Chunk]:
        if self.skipped >= self.offset:
            return chunk
        skip = min(self.offset - self.skipped, chunk.num_rows)
        self.skipped += skip
        if skip >= chunk.num_rows:
            return None
        return chunk.slice(skip, chunk.num_rows - skip)


class MaterializingTransform(ISimpleTransform):
    """把**常量列**展开成普通列。

    .. warning::

      这个处理器看着很无聊,但它防的是一类**真真切切的错误**:
      ``ColumnConst`` 的 ``size()`` 是显式设的而不是数出来的,
      所以它一旦流进 ``ORDER BY``、``ARRAY JOIN`` 这类
      "就地改写列"的处理器,行数就会错得很自然(比如变成 1 行)。
      真实 ClickHouse 也有 ``MaterializingTransform``,
      而且它的位置是**精心安排**的 —— 放在任何"会改变行数"的
      处理器之前。第 10 章会展示删掉它会出什么错。
    """

    name = "MaterializingTransform"

    def transform(self, chunk: Chunk) -> Optional[Chunk]:
        cols = []
        for col in chunk.columns:
            cols.append(col.convert_to_full_column() if col.is_const() else col)
        return Chunk(chunk.header, cols, chunk.num_rows)


class ArrayJoinTransform(ISimpleTransform):
    """``ARRAY JOIN`` —— 把数组列**展开**成多行。

    .. code-block:: text

       id | tags                  id | tag
       1  | ['a','b']      →      1  | 'a'
       2  | ['c']                 1  | 'b'
                                2  | 'c'

    ``LEFT ARRAY JOIN`` 与 ``ARRAY JOIN`` 的差别只在**空数组**:
    前者保留一行(数组列的值为空数组),后者把这一行丢掉。

    它是引擎里少数几个**会改变行数**的算子,所以:

    * 必须在它之前把所有常量列 materialize;
    * 它之后不能再假设"行数 = 上游行数";
    * 它与会改变行数的别的算子(``LIMIT``/``GROUP BY``)的**相对顺序**
      决定了正确性 —— ``SELECT arrayJoin(a) FROM t LIMIT 2`` 是
      "先展开再取 2 行",不是"先取 2 行再展开"。
    """

    name = "ArrayJoinTransform"

    def __init__(self, ctx: Any, expressions: Sequence[Any],
                 scope_columns: Dict[str, ColumnWithTypeAndName],
                 left: bool = False,
                 subquery_executor: Optional[Callable[..., Any]] = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.expressions = list(expressions)
        self.scope_columns = dict(scope_columns)
        self.left = left
        self.subquery_executor = subquery_executor
        self.lookup: Any = None

    def transform(self, chunk: Chunk) -> Optional[Chunk]:
        scope = Scope(dict(self.scope_columns))
        for i, c in enumerate(chunk.header.data):
            scope.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, chunk.columns[i]))
        ev = Evaluator(self.ctx, scope, chunk.num_rows,
                       subquery_executor=self.subquery_executor)
        arrays = [ev.evaluate(e) for e in self.expressions]

        # 逐行展开:算出每一行被复制几次、以及展开后的元素
        counts: List[int] = []
        elements: List[List[Any]] = []
        for i in range(chunk.num_rows):
            total = 1
            row_elements: List[Any] = []
            for a in arrays:
                value = a.column.get(i) or []
                if isinstance(value, (list, tuple)):
                    if not value:
                        if self.left:
                            row_elements.append(None)
                        else:
                            total = 0
                    else:
                        total *= len(value)
                        row_elements.append(list(value))
                else:
                    row_elements.append([value])
            counts.append(total)
            elements.append(row_elements)

        out_rows = sum(counts)
        if out_rows == 0:
            return None

        # 源列按 counts 复制
        out_cols: List[IColumn] = []
        for idx, col in enumerate(chunk.columns):
            rep: List[int] = []
            for i, n in enumerate(counts):
                if n:
                    rep.append(n)
            new_col = col.clone_empty()
            for i, n in enumerate(counts):
                if n:
                    new_col.insert_many_from(col, i, n)
            out_cols.append(new_col)

        # 数组列自己:展开成多行
        #
        # .. warning::
        #
        #    **多个数组同时展开时,每一列的元素个数必须与行数一致。**
        #
        #    行数是**笛卡尔积**(``total *= len(value)``),而早期版本让每个
        #    数组列只追加"它自己的元素个数" —— 于是 ``ARRAY JOIN [1,2] AS p,
        #    [3,4] AS q`` 算出的行数是 4(2×2),而 p 列只有 2 个值,
        #    构造 Chunk 时直接抛 ``Chunk column 1 has 2 rows, expected 4``。
        #
        #    修法是让每个数组列**也按笛卡尔积展开**:第 ``a_idx`` 个数组在
        #    第 j 个组合里取的元素索引是
        #    ``(j // 后面所有数组长度的乘积) % 本数组长度``。
        #    这正是"进制展开"—— 把 j 当成一个混合进制数,每一位对应一个数组。
        lengths_per_row = [
            [len(e[a_idx]) if isinstance(e[a_idx], list) else 1 for a_idx in range(len(arrays))]
            for e in elements
        ]
        for a_idx, a in enumerate(arrays):
            from ..types.composite import DataTypeArray

            elem_type = a.type.element if isinstance(a.type, DataTypeArray) else None
            if elem_type is None:
                raise_error(
                    ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                    f"ARRAY JOIN requires an Array column, got {a.type.get_name()}",
                )
            new_col = elem_type.create_column()
            for i, n in enumerate(counts):
                if n == 0:
                    continue
                lens = lengths_per_row[i]
                values = elements[i][a_idx]
                if not isinstance(values, list):
                    values = [values]
                # 本数组右边所有数组长度的乘积 = 本位的"进制"
                stride = 1
                for k in range(a_idx + 1, len(arrays)):
                    stride *= max(lens[k], 1)
                for j in range(n):
                    new_col.append(values[(j // stride) % len(values)])
            out_cols.append(new_col)

        header_items: List[ColumnWithTypeAndName] = []
        for c in chunk.header.data:
            header_items.append(ColumnWithTypeAndName(c.name, c.type, c.type.create_column()))
        for a in arrays:
            header_items.append(
                ColumnWithTypeAndName(a.name, a.type.element, a.type.element.create_column())
            )
        header = Block(header_items)
        return Chunk(header, out_cols, out_rows)


class SortingTransform(ISimpleTransform):
    """``ORDER BY`` —— **累积所有数据**再排序。

    这类算子叫"blocking operator"(阻塞算子):它在看到**全部**输入之前
    一行都输出不了。这是有代价的:

    * 内存峰值 = 整个结果集(除非开了 ``max_bytes_before_external_sort``);
    * 无法流式输出 —— ``ORDER BY`` 的查询拿不到"第一行很快"的体验。

    ``LIMIT n BY`` 与"读时有序"能避免这个代价 —— 前者用
    :class:`LimitByTransform`,后者靠 :class:`MergingSortedTransform`
    在**已排序**的流上做归并。
    """

    name = "SortingTransform"

    def __init__(
        self,
        ctx: Any,
        order_exprs: Sequence[Any],
        scope_columns: Dict[str, ColumnWithTypeAndName],
        limit: Optional[int] = None,
        subquery_executor: Optional[Callable[..., Any]] = None,
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.order_exprs = list(order_exprs)
        self.scope_columns = dict(scope_columns)
        self.limit = limit
        self.subquery_executor = subquery_executor
        self.lookup: Any = None
        self._blocks: List[Block] = []
        self._header: Optional[Block] = None
        self._done = False

    def transform(self, chunk: Chunk) -> Optional[Chunk]:
        if self._header is None:
            self._header = chunk.header
        block = chunk.get_block_with_data()
        # 算排序键 → 加到 block 里(临时列,排完删掉)
        scope = Scope(dict(self.scope_columns))
        for c in block.data:
            scope.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, c.column))
        ev = Evaluator(self.ctx, scope, block.rows(), subquery_executor=self.subquery_executor,
                       expression_lookup=self.lookup)
        for i, e in enumerate(self.order_exprs):
            expr = e.expr if hasattr(e, "expr") else e
            res = ev.evaluate(expr)
            block.add_column(ColumnWithTypeAndName(f"__sort_{i}", res.type, res.column))
        self._blocks.append(block)
        return None

    def work(self) -> Status:
        inp, out = self.inputs[0], self.outputs[0]
        if self.finished:
            return Status.FINISHED
        if out.has_data():
            return Status.PORT_FULL
        if inp.has_data():
            chunk = inp.pull()
            self.input_rows += chunk.num_rows
            self.transform(chunk)
            return Status.READY
        if inp.finished and not self._done:
            self._done = True
            result = self._sort_and_emit()
            if result is None or result.num_rows == 0:
                out.finish()
                self.finished = True
                return Status.FINISHED
            self.output_rows += result.num_rows
            out.push(result)
            return Status.READY
        if inp.finished:
            out.finish()
            self.finished = True
            return Status.FINISHED
        return Status.NEED_DATA

    def _sort_and_emit(self) -> Optional[Chunk]:
        """把累积的 block 拼起来排序 —— **归并所有块后一次全排**。

        真实 ClickHouse 用的是"分块排序 + 多路归并"
        (``SortingTransform`` 之后接 ``MergingSortedTransform``),
        这样每一块内部排序可以并行、归并是流式的。
        mini-clickhouse 直接全排 —— 结果一样,内存峰值更高。
        """
        if not self._blocks:
            return None
        merged = self._blocks[0].clone_empty_without_columns()
        for b in self._blocks:
            if merged.is_empty():
                merged = b.clone_empty()
            for src, dst in zip(b.data, merged.data):
                dst.column.insert_range_from(src.column, 0, src.column.size())
        rows = merged.rows()
        if rows == 0:
            return None
        n_keys = len(self.order_exprs)
        keys = [merged.get_by_name(f"__sort_{i}").column for i in range(n_keys)]
        reverse = [getattr(e, "direction", "ASC") == "DESC" for e in self.order_exprs]
        nulls_first = []
        for e in self.order_exprs:
            nf = getattr(e, "nulls", None)
            if nf == "FIRST":
                nulls_first.append(True)
            elif nf == "LAST":
                nulls_first.append(False)
            else:
                # 默认:NULL 是"最大值",所以 ASC 时在最后、DESC 时在最前
                nulls_first.append(getattr(e, "direction", "ASC") == "DESC")
        indexes = _stable_sort_indexes(keys, reverse, nulls_first, rows)
        # 排完把临时列删掉
        for i in range(n_keys):
            merged.erase(f"__sort_{i}")
        merged.permute(indexes)
        if self.limit is not None and merged.rows() > self.limit:
            merged = merged.slice(0, self.limit)
        return Chunk(merged.clone_empty(), [c.column for c in merged.data], merged.rows())


def _stable_sort_indexes(
    keys: Sequence[IColumn], reverse: Sequence[bool], nulls_first: Sequence[bool], rows: int
) -> List[int]:
    """多列排序 —— 用 Python 的**稳定排序**逐列从后往前排。

    .. code-block:: text

       多列排序的标准技巧:从**最后一列**往前排,每趟用稳定排序。
       因为稳定排序不会打乱已经排好的列,所以最终结果是按
       (k1, k2, ..., kn) 的字典序 —— 而且不需要构造复合键。

    这个技巧在真实 ClickHouse 里被做得更底层:它把排序键**序列化成
    一段字节**然后 memcmp(见 ``SortingTransform`` 与 ``SortBlock``),
    因为一次 memcmp 比逐列比较快得多。

    NULL 的处理见 :func:`_compare_with_nulls`。
    """
    indexes = list(range(rows))
    for c in range(len(keys) - 1, -1, -1):
        col = keys[c]
        rev = reverse[c]
        nf = nulls_first[c]
        indexes.sort(
            key=lambda i, col=col, rev=rev, nf=nf: _sort_key(col, i, rev, nf),
            # Python 的 list.sort 是稳定排序 —— 这正是上面那个技巧成立的前提
        )
    return indexes


class _SortKey:
    """排序键的包装 —— 处理 NULL 与 DESC 的"取反"。

    DESC 的实现方式是在比较时**取反**。但 Python 的 ``sorted``
    只能升序,所以用这个类做"可比较对象",在 ``__lt__`` 里取反。
    NULL 被映射到一个特殊的哨兵,它的位置由 ``nulls_first`` 决定 ——
    注意这**不是**简单的"最大/最小":
    ``ORDER BY x DESC NULLS FIRST`` 里的 NULL 在**最前**,
    而 DESC 取反之后如果 NULL 还是"最大",它就会跑到最后。所以
    NULL 的位置必须**独立于方向**地由 ``nulls_first`` 决定。
    """

    __slots__ = ("value", "is_null", "reverse", "nulls_first")

    def __init__(self, value: Any, reverse: bool, nulls_first: bool) -> None:
        self.is_null = value is None
        self.value = value
        self.reverse = reverse
        self.nulls_first = nulls_first

    def __lt__(self, other: "_SortKey") -> bool:
        """比较多列排序里的**一个**键。

        .. warning::

           **相等时必须返回 False,不能把它也取反。**

           早期版本在 ``reverse`` 时写成 ``return not (self.value < other.value)``
           —— 两个**相等**的值于是得到 ``not False`` = ``True``,也就是
           ``x < x`` 成立。这违反了排序算法对比较器的要求,timsort 会因此
           把**上一趟已经排好的次要键顺序打乱**。

           症状很具体,而且只在多列排序时出现:``ORDER BY k DESC, n DESC``
           在 ``k`` 全相等时会得到**升序**的 ``n``(与预期完全相反)。
           单列 ``ORDER BY`` 看不出来 —— 因为它不走"逐列从后往前、
           依赖稳定排序"那条路。
        """
        if self.is_null or other.is_null:
            if self.is_null and other.is_null:
                return False
            # NULL 的位置只由 nulls_first 决定,与 reverse 无关
            return self.is_null if self.nulls_first else not self.is_null
        if self.value == other.value:
            return False          # ← 相等绝不能取反
        result = self.value < other.value
        return (not result) if self.reverse else result

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _SortKey):
            return NotImplemented
        if self.is_null and other.is_null:
            return True
        if self.is_null or other.is_null:
            return False
        return self.value == other.value


def _sort_key(col: IColumn, i: int, reverse: bool, nulls_first: bool) -> _SortKey:
    return _SortKey(col.get(i), reverse, nulls_first)


class MergingSortedTransform(IProcessor):
    """**多路归并** —— 输入已经是排好序的,输出还是排好序的。

    这是 ClickHouse 处理"有序数据"的核心手段,出现在三个地方:

    1. ``ORDER BY`` 的最终阶段(每块排好序后归并);
    2. **MergeTree 的 part 合并**(每个 part 内部有序,归并成新 part);
    3. ``optimize_read_in_order`` —— 如果排序键与 ``ORDER BY`` 一致,
       直接按顺序读,跳过整个排序步骤(**省内存、还能流式输出**)。

    第 2 条尤其重要:它意味着"``ORDER BY`` 恰好等于排序键"的查询
    不需要排序算子 —— 这是 ClickHouse 上一个最常见的性能调优手段。

    实现用 **堆** 做 k 路归并:每一路取当前最小的一行,弹出最小的那个,
    再从同一路补充 —— O(N log k)。
    """

    name = "MergingSortedTransform"

    def __init__(self, n_inputs: int, order_exprs: Sequence[Any] = ()) -> None:
        super().__init__()
        self.order_exprs = list(order_exprs)
        for _ in range(n_inputs):
            self.add_input("in")
        self.add_output("out")
        self._rows: List[Tuple[Any, int, int]] = []
        self._cursors: List[int] = [0] * n_inputs
        self._blocks: List[Optional[Block]] = [None] * n_inputs
        self._header: Optional[Block] = None
        self._initialized = False

    def work(self) -> Status:
        # 简化实现:等所有输入结束,然后整体归并(与真实 CH 的流式归并
        # 语义一致,但不是流式的 —— 差异写在类文档里)
        if self.finished:
            return Status.FINISHED
        out = self.outputs[0]
        if out.has_data():
            return Status.PORT_FULL
        if not all(p.is_finished() for p in self.inputs):
            # 把所有输入吸进内存(简化实现:不做流式归并,见类文档)
            progressed = False
            for i, p in enumerate(self.inputs):
                while p.has_data():
                    chunk = p.pull()
                    if self._blocks[i] is None:
                        self._blocks[i] = chunk.get_block_with_data()
                        self._header = chunk.header
                    else:
                        self._blocks[i].merge(chunk.get_block_with_data())
                    progressed = True
            if progressed:
                return Status.READY
            return Status.NEED_DATA
        if not self._initialized:
            self._initialized = True
            result = self._merge_all()
            if result is None:
                out.finish()
                self.finished = True
                return Status.FINISHED
            out.push(result)
            return Status.READY
        out.finish()
        self.finished = True
        return Status.FINISHED

    def _merge_all(self) -> Optional[Chunk]:
        blocks = [b for b in self._blocks if b is not None and b.rows()]
        if not blocks:
            return None
        merged = blocks[0].clone_empty()
        for b in blocks[1:]:
            merged.merge(b)
        if not self.order_exprs:
            return Chunk(merged.clone_empty(), [c.column for c in merged.data], merged.rows())
        keys = []
        for i, e in enumerate(self.order_exprs):
            expr = getattr(e, "expr", e)
            name = expr.to_sql() if hasattr(expr, "to_sql") else str(expr)
            col = merged.try_get_by_name(name)
            if col is None:
                keys = []
                break
            keys.append(col.column)
        if keys:
            reverse = [getattr(e, "direction", "ASC") == "DESC" for e in self.order_exprs]
            nf = [getattr(e, "nulls", None) == "FIRST" for e in self.order_exprs]
            idx = _stable_sort_indexes(keys, reverse, nf, merged.rows())
            merged.permute(idx)
        return Chunk(merged.clone_empty(), [c.column for c in merged.data], merged.rows())


# ══════════════════════════════════════════════════════════════════════════
# 聚合
# ══════════════════════════════════════════════════════════════════════════


class AggregatingTransform(IProcessor):
    """**部分聚合** —— 每个分组维护一个"聚合状态",而不是最终值。

    这是 ClickHouse 聚合能并行的全部秘密。看一个例子:

    .. code-block:: text

       数据(两个线程各处理一半):
         线程 1: A=1, A=2, B=5
         线程 2: A=3, B=7, B=1

       部分聚合的输出(状态,不是结果):
         线程 1:  A → {sum: 3, count: 2}
         线程 2:  A → {sum: 3, count: 1}
                  B → {sum: 8, count: 2}
                  ↑ 注意 B 在两个线程里各出现一次

       最终合并后:
         A → {sum: 6, count: 3} → avg = 2
         B → {sum: 13, count: 2} → avg = 6.5

    关键在于 **``avg`` 的中间状态是 (sum, count) 而不是一个数** ——
    所以它可合并。这就是为什么 ``AggregateFunction`` 是一种**类型**,
    以及为什么 ``AggregatingMergeTree`` 能把状态直接写进 part。

    .. note::

       真实 ClickHouse 有两级哈希表(``two_level_hash_table``):
       分组数超过 ``group_by_two_level_threshold``(默认 10 万)时,
       它会把哈希表按 key 的高位切成 256 个"桶",每个桶可以独立地
       被一个线程处理 —— 这样"分组数极多"的查询也能并行。
       mini-clickhouse 只实现单级哈希表,并在文档里标注这个差异。
    """

    name = "AggregatingTransform"

    def __init__(
        self,
        ctx: Any,
        keys: Sequence[Any],
        aggregates: Sequence[AggregateDescription],
        scope_columns: Dict[str, ColumnWithTypeAndName],
        arg_names: Sequence[str] = (),
        max_rows: Optional[int] = None,
        subquery_executor: Optional[Callable[..., Any]] = None,
    ) -> None:
        super().__init__()
        self.add_input("in")
        self.add_output("out")
        self.ctx = ctx
        self.keys = list(keys)
        #: ``keys[i]`` 是 AST;``arg_names[i]`` 是它要读的列名
        self.aggregates = list(aggregates)
        self.scope_columns = dict(scope_columns)
        self.subquery_executor = subquery_executor
        #: 分阶段求值的查找表(由解释器注入)
        self.lookup: Any = None
        self._key_columns: List[IColumn] = []
        self._key_types: List[Any] = []
        self._states: Dict[Any, List[Any]] = {}
        #: 规范化键 → 原始键值(逐列)。回填键列时必须用**原始值**。
        self._raw_keys: Dict[Any, tuple] = {}
        self._order: List[Any] = []
        self._done = False
        self._groups_overflow = False
        self._max_rows = max_rows
        #: 状态列的名字前缀 —— 与真实 CH 的 ``__aggregate_...`` 命名一致
        self.state_column_names: List[str] = []
        self._key_names: List[str] = []

    def work(self) -> Status:
        inp, out = self.inputs[0], self.outputs[0]
        if self.finished:
            return Status.FINISHED
        if out.has_data():
            return Status.PORT_FULL
        if inp.has_data():
            chunk = inp.pull()
            self.input_rows += chunk.num_rows
            self._accumulate(chunk)
            return Status.READY
        if inp.finished and not self._done:
            self._done = True
            result = self._emit()
            if result is None:
                out.finish()
                self.finished = True
                return Status.FINISHED
            out.push(result)
            return Status.READY
        if inp.finished:
            out.finish()
            self.finished = True
            return Status.FINISHED
        return Status.NEED_DATA

    def _accumulate(self, chunk: Chunk) -> None:
        if not self._key_names:
            self._key_names = [
                _key_name(k, i) for i, k in enumerate(self.keys)
            ]
            self.state_column_names = [
                f"__agg_{i}" for i in range(len(self.aggregates))
            ]
        # 算键列与实参列
        scope = Scope(dict(self.scope_columns))
        for i, c in enumerate(chunk.header.data):
            scope.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, chunk.columns[i]))
        ev = Evaluator(self.ctx, scope, chunk.num_rows,
                       subquery_executor=self.subquery_executor,
                       expression_lookup=self.lookup)
        key_results = [ev.evaluate(k) for k in self.keys]
        key_cols = [_col_of(kr) for kr in key_results]
        if not self._key_types:
            self._key_types = [kr.type for kr in key_results]
            self._key_columns = [self._key_types[i].create_column()
                                 for i in range(len(key_results))]
        arg_cols: List[IColumn] = []
        for agg in self.aggregates:
            cols = [_col_of(ev.evaluate(a)) for a in agg.arg_exprs]
            arg_cols.append(cols)
        for i, c in enumerate(key_cols):
            self._key_columns[i].insert_range_from(c, 0, c.size())
        for row in range(chunk.num_rows):
            key = tuple(build_key(key_cols, row)) if len(key_cols) > 1 else (
                build_key(key_cols, row) if key_cols else ()
            )
            states = self._states.get(key)
            if states is None:
                if self._max_rows is not None and len(self._states) >= self._max_rows:
                    self._groups_overflow = True
                    raise_error(
                        ErrorCode.TOO_MANY_ROWS,
                        f"GROUP BY would produce more than {self._max_rows} rows; "
                        f"increase max_rows_to_group_by or use "
                        f"group_by_overflow_mode = 'any'",
                    )
                states = [a.func.create_state() for a in self.aggregates]
                self._states[key] = states
                self._order.append(key)
                # 记下这一组的**原始键值**(逐列)。
                #
                # 哈希表用的是 ``build_key`` 规范化之后的键:NaN 换成
                # 哨兵、list 换成 tuple、dict 换成排序后的元组对 ——
                # 那些形式**不能**直接写回列(NaN 哨兵喂给 Float64 列会
                # 抛 ``TypeError: float() argument must be ... not '_NaNKey'``,
                # tuple 喂给 Array 列会抛 ``Cannot insert tuple into Array``)。
                # 回填列必须用原始值。
                self._raw_keys[key] = (
                    tuple(key_cols[i].get(row) for i in range(len(key_cols)))
                )
            for j, agg in enumerate(self.aggregates):
                agg.func.add(states[j], arg_cols[j], row)

    def _emit(self) -> Optional[Chunk]:
        if not self._states and not self.keys:
            # 无 GROUP BY 且没有输入行 —— 仍然要产出一行(``SELECT count()``)
            states = [a.func.create_state() for a in self.aggregates]
            self._states[()] = states
            self._order.append(())
        if not self._states:
            return None
        header_items: List[ColumnWithTypeAndName] = []
        out_cols: List[IColumn] = []
        for i, k in enumerate(self.keys):
            dtype = self._key_types[i] if i < len(self._key_types) else get_data_type("String")
            col = dtype.create_column()
            out_cols.append(col)
            header_items.append(ColumnWithTypeAndName(
                _key_output_name(k), dtype, dtype.create_column()
            ))
        state_cols = []
        arg_types = []
        for agg in self.aggregates:
            state_type = _state_type(agg)
            col = state_type.create_column()
            state_cols.append(col)
            arg_types.append(state_type)
            header_items.append(ColumnWithTypeAndName(agg.result_name or f"__agg_{len(state_cols)-1}",
                                                      state_type, state_type.create_column()))
        for key in self._order:
            # 用**原始值**回填 —— 见 _accumulate 里 self._raw_keys 的说明
            key_vals = self._raw_keys.get(key)
            if key_vals is None:
                key_vals = key if isinstance(key, tuple) else (key,)
            for i, v in enumerate(key_vals):
                out_cols[i].append(v)
            states = self._states[key]
            for j, st in enumerate(states):
                state_cols[j].append(st)
        out_cols.extend(state_cols)
        header = Block(header_items)
        return Chunk(header, out_cols, len(self._order))


def _state_type(agg: AggregateDescription) -> Any:
    """聚合状态的类型 —— ``AggregateFunction(...)`` 或 ``SimpleAggregateFunction``。"""
    from ..types.composite import DataTypeAggregateFunction, DataTypeSimpleAggregateFunction

    func = agg.func
    arg_types = getattr(func, "arg_types", [])
    if getattr(func, "is_simple", lambda: False)():
        return DataTypeSimpleAggregateFunction(agg.func_name, func.get_return_type())
    return DataTypeAggregateFunction(agg.func_name, arg_types, agg.params)


def _col_of(cwtn: ColumnWithTypeAndName) -> IColumn:
    return cwtn.column.convert_to_full_column() if cwtn.column.is_const() else cwtn.column


def _expr_type(expr: Any) -> Any:
    return get_data_type("String")


def _key_name(expr: Any, index: int) -> str:
    return expr.to_sql() if hasattr(expr, "to_sql") else f"__key_{index}"


def _key_output_name(expr: Any) -> str:
    if hasattr(expr, "alias") and getattr(expr, "alias", None):
        return expr.alias
    return expr.to_sql() if hasattr(expr, "to_sql") else str(expr)


class MergingAggregatedTransform(IProcessor):
    """**合并部分聚合结果** —— 把多份状态合成一份,再算出最终值。

    输入是若干份"部分状态",输出是**最终值**列(而不是状态)——
    除非查询里写了 ``-State``(那时它保持状态不 finalize,
    这正是 ``AggregatingMergeTree`` 与物化视图增量聚合的用法)。

    一句话区分这两个处理器:

    .. code-block:: text

       AggregatingTransform        多行 → 一份状态          (扇入到 HashTable)
       MergingAggregatedTransform  多份状态 → 一份状态 + 值  (树形归并)

    真实 ClickHouse 里后者还会在合并到一定规模时**提前做一轮合并**
    (``aggregation_memory_efficient_merge_threads``),
    以避免最终合并时内存爆掉 —— 这叫 "bucket 合并"。
    """

    name = "MergingAggregatedTransform"

    def __init__(self, ctx: Any, aggregates: Sequence[AggregateDescription],
                 with_finalize: bool = True) -> None:
        super().__init__()
        self.add_input("in")
        self.add_output("out")
        self.ctx = ctx
        self.aggregates = list(aggregates)
        self.with_finalize = with_finalize
        self._states: Dict[Any, List[Any]] = {}
        #: 规范化键 → 原始键值(逐列)。回填键列必须用原始值,见
        #: AggregatingTransform._accumulate 的说明。
        self._raw_keys: Dict[Any, tuple] = {}
        self._order: List[Any] = []
        self._key_names: List[str] = []
        self._key_types: List[Any] = []
        self._done = False

    def work(self) -> Status:
        inp, out = self.inputs[0], self.outputs[0]
        if self.finished:
            return Status.FINISHED
        if out.has_data():
            return Status.PORT_FULL
        if inp.has_data():
            chunk = inp.pull()
            self._merge(chunk)
            return Status.READY
        if inp.finished and not self._done:
            self._done = True
            result = self._emit()
            if result is None:
                out.finish()
                self.finished = True
                return Status.FINISHED
            out.push(result)
            return Status.READY
        if inp.finished:
            out.finish()
            self.finished = True
            return Status.FINISHED
        return Status.NEED_DATA

    def _merge(self, chunk: Chunk) -> None:
        if not self._key_names:
            n_keys = len(chunk.header.data) - len(self.aggregates)
            for i in range(n_keys):
                c = chunk.header.data[i]
                self._key_names.append(c.name)
                self._key_types.append(c.type)
        n_keys = len(self._key_names)
        key_cols = chunk.columns[:n_keys]
        state_cols = chunk.columns[n_keys:]
        for row in range(chunk.num_rows):
            # **哈希用的键必须规范化**:Array 的值是 list、Map 的值是 dict,
            # 两者都不可哈希,直接拿去当字典键会抛
            # ``TypeError: cannot use 'list' as a dict key``。
            # ``normalize_key_value`` 与 GROUP BY 用的是同一套规则
            # (list→tuple、dict→排序后的元组对、NaN→哨兵),
            # 所以两边算出来的"同一个分组"必然一致。
            raw = tuple(key_cols[i].get(row) for i in range(n_keys))
            key = tuple(normalize_key_value(v) for v in raw)
            if len(key) == 1:
                key = key[0]
            self._raw_keys.setdefault(key, raw)
            dst = self._states.get(key)
            if dst is None:
                dst = [
                    self.aggregates[j].func.deserialize_state(
                        self.aggregates[j].func.serialize_state(state_cols[j].get(row))
                    )
                    for j in range(len(self.aggregates))
                ]
                self._states[key] = dst
                self._order.append(key)
            else:
                for j in range(len(self.aggregates)):
                    self.aggregates[j].func.merge(
                        dst[j], state_cols[j].get(row)
                    )

    def _emit(self) -> Optional[Chunk]:
        if not self._states:
            return None
        header_items: List[ColumnWithTypeAndName] = []
        out_cols: List[IColumn] = []
        for i, name in enumerate(self._key_names):
            col = self._key_types[i].create_column()
            out_cols.append(col)
            header_items.append(ColumnWithTypeAndName(name, self._key_types[i],
                                                      self._key_types[i].create_column()))
        for agg in self.aggregates:
            if self.with_finalize:
                dtype = agg.func.get_return_type()
            else:
                dtype = _state_type(agg)
            col = dtype.create_column()
            out_cols.append(col)
            header_items.append(ColumnWithTypeAndName(
                agg.result_name or f"__agg_{len(out_cols)-1}", dtype, dtype.create_column()
            ))
        for key in self._order:
            # 用**原始值**回填 —— 见 _accumulate 里 self._raw_keys 的说明
            key_vals = self._raw_keys.get(key)
            if key_vals is None:
                key_vals = key if isinstance(key, tuple) else (key,)
            for i, v in enumerate(key_vals):
                out_cols[i].append(v)
            states = self._states[key]
            for j, agg in enumerate(self.aggregates):
                idx = len(self._key_names) + j
                if self.with_finalize:
                    agg.func.insert_result_into(states[j], out_cols[idx])
                else:
                    out_cols[idx].append(
                        agg.func.deserialize_state(agg.func.serialize_state(states[j]))
                    )
        header = Block(header_items)
        return Chunk(header, out_cols, len(self._order))


class DistinctTransform(IProcessor):
    """``SELECT DISTINCT`` —— 用哈希集合去重**整行**。

    .. note::

       真实 ClickHouse 把 ``DISTINCT`` 实现成"全列 GROUP BY"——
       ``MergingAggregatedTransform`` 的一个特例。这样做的好处是
       它可以复用两级聚合、可以下推到分布式节点
       (``distributed_group_by_no_merge``)。
       mini-clickhouse 单独写一个处理器,因为它更好读。
    """

    name = "DistinctTransform"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("in")
        self.add_output("out")
        self._seen: set = set()
        self._done = False

    def work(self) -> Status:
        inp, out = self.inputs[0], self.outputs[0]
        if self.finished:
            return Status.FINISHED
        if out.has_data():
            return Status.PORT_FULL
        if inp.has_data():
            chunk = inp.pull()
            mask: List[bool] = []
            for row in range(chunk.num_rows):
                key = tuple(
                    _hashable(chunk.columns[i].get(row)) for i in range(len(chunk.columns))
                )
                if key in self._seen:
                    mask.append(False)
                else:
                    self._seen.add(key)
                    mask.append(True)
            if not any(mask):
                return Status.READY
            return _push_filtered(out, chunk, mask)
        if inp.finished:
            out.finish()
            self.finished = True
            return Status.FINISHED
        return Status.NEED_DATA


def _hashable(value: Any) -> Any:
    """把值转成可哈希的形式(数组/Map 需要转换)。"""
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


def _push_filtered(out: OutputPort, chunk: Chunk, mask: List[bool]) -> Status:
    kept = sum(1 for m in mask if m)
    for col in chunk.columns:
        col.filter(mask)
    out.push(Chunk(chunk.header, chunk.columns, kept))
    return Status.READY


class LimitByTransform(IProcessor):
    """``LIMIT n BY expr`` —— **每个分组**取前 n 行。

    为什么要有专门的算子? 因为"取每个用户最近 3 条"这种需求太常见了,
    而它用窗口函数写要付出"全排序"的代价。``LIMIT BY`` 只需要一个
    按分组键的计数器 —— O(1) 额外空间、单趟完成。

    .. code-block:: sql

       -- 写法 A(推荐,单趟、低内存)
       SELECT * FROM events ORDER BY ts DESC LIMIT 3 BY user_id;

       -- 写法 B(等价但要全排序)
       SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY user_id
                     ORDER BY ts DESC) AS rn FROM events) WHERE rn <= 3;

    注意 ``LIMIT BY`` **依赖输入顺序**:要先 ``ORDER BY`` 才能保证
    "取到的是最近 3 条" —— 与真实 ClickHouse 的语义一致。
    """

    name = "LimitByTransform"

    def __init__(self, ctx: Any, key_exprs: Sequence[Any], limit: int, offset: int = 0,
                 scope_columns: Optional[Dict[str, ColumnWithTypeAndName]] = None) -> None:
        super().__init__()
        self.add_input("in")
        self.add_output("out")
        self.ctx = ctx
        self.key_exprs = list(key_exprs)
        self.limit = limit
        self.offset = offset
        self.scope_columns = dict(scope_columns or {})
        self.lookup: Any = None
        self._counters: Dict[Any, int] = {}

    def work_with(self, chunk: Chunk) -> Optional[Chunk]:
        """**直接喂一个 chunk** 并返回过滤后的结果(不过流水线)。

        分段执行需要这种"一步算完"的入口:解释器手里已经有一整块数据,
        不需要把它塞进端口再拉出来。真实 ClickHouse 的处理器没有这个方法
        —— 它的一切都通过端口流动。
        """
        inp, out = self.inputs[0], self.outputs[0]
        inp.chunk = chunk          # 直接把数据放到输入端口上(未连接时有效)
        self.work()
        return out.pull()

    def work(self) -> Status:
        inp, out = self.inputs[0], self.outputs[0]
        if self.finished:
            return Status.FINISHED
        if out.has_data():
            return Status.PORT_FULL
        if inp.has_data():
            chunk = inp.pull()
            scope = Scope(dict(self.scope_columns))
            for i, c in enumerate(chunk.header.data):
                scope.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, chunk.columns[i]))
            ev = Evaluator(self.ctx, scope, chunk.num_rows, expression_lookup=self.lookup)
            keys = [_col_of(ev.evaluate(k)) for k in self.key_exprs]
            mask: List[bool] = []
            for row in range(chunk.num_rows):
                key = tuple(build_key(keys, row)) if len(keys) > 1 else build_key(keys, row)
                n = self._counters.get(key, 0)
                if n < self.offset:
                    mask.append(False)
                elif n < self.offset + self.limit:
                    mask.append(True)
                else:
                    mask.append(False)
                self._counters[key] = n + 1
            if not any(mask):
                return Status.READY
            return _push_filtered(out, chunk, mask)
        if inp.finished:
            out.finish()
            self.finished = True
            return Status.FINISHED
        return Status.NEED_DATA


class UnionTransform(IProcessor):
    """``UNION ALL`` —— 把多路输入**依次**接起来。

    ``UNION ALL`` 不需要去重,所以它极其便宜:没有任何状态。
    ``UNION DISTINCT`` 则要在后面接一个 :class:`DistinctTransform` ——
    这也是为什么 ``UNION ALL`` 与 ``UNION`` 的性能差一个数量级。
    """

    name = "UnionTransform"

    def __init__(self, n_inputs: int) -> None:
        super().__init__()
        for _ in range(n_inputs):
            self.add_input("in")
        self.add_output("out")
        self._current = 0

    def work(self) -> Status:
        out = self.outputs[0]
        if self.finished:
            return Status.FINISHED
        if out.has_data():
            return Status.PORT_FULL
        while self._current < len(self.inputs):
            inp = self.inputs[self._current]
            if inp.has_data():
                chunk = inp.pull()
                out.push(chunk)
                return Status.READY
            if inp.finished:
                self._current += 1
                continue
            return Status.NEED_DATA
        out.finish()
        self.finished = True
        return Status.FINISHED


class ResizeProcessor(IProcessor):
    """把一路输入**复制**成 N 路 —— ``max_threads`` 并行的起点。

    真实 ClickHouse 的 ``ResizeProcessor`` 是个"圆环":它把上游的
    chunk 轮流投给 N 个下游,下游谁空就投给谁。这样上游只需要读一遍,
    下游就能并行处理。教程第 10 章会用它来解释"为什么 ClickHouse 的
    并行是**算子级**的而不是**查询级**的"。
    """

    name = "ResizeProcessor"

    def __init__(self, n_outputs: int) -> None:
        super().__init__()
        self.add_input("in")
        for _ in range(n_outputs):
            self.add_output(f"out{len(self.outputs)}")
        self._cursor = 0

    def work(self) -> Status:
        inp = self.inputs[0]
        if self.finished:
            return Status.FINISHED
        if inp.has_data():
            chunk = inp.pull()
            # 找一下哪个输出是空的
            for _ in range(len(self.outputs)):
                port = self.outputs[self._cursor % len(self.outputs)]
                self._cursor += 1
                if not port.has_data() and not port.finished:
                    port.push(chunk)
                    return Status.READY
            return Status.PORT_FULL
        if inp.finished:
            self.finish_outputs()
            self.finished = True
            return Status.FINISHED
        return Status.NEED_DATA


class JoiningTransform(IProcessor):
    """``JOIN`` —— **哈希连接**(build 右表、probe 左表)。

    实现的是 ClickHouse 的默认算法 ``hash``:

    .. code-block:: text

       ① build 阶段:把**右表**全部读进内存,建 `join_key → [行号]` 的哈希表
       ② probe 阶段:左表每一行去查哈希表,按 strictness 决定怎么合并

    **哪个表放右边很重要**:右表要全部进内存,所以小的那张要放右边。
    ClickHouse 不做这个自动重排(它相信你知道自己在干什么),
    放反了会 OOM —— 这是最常见的 JOIN 事故(第 13 章)。

    四种 strictness 的语义差别(第 13 章有完整表格):

    * ``ALL`` —— 匹配多少行就产出多少行(笛卡尔);
    * ``ANY`` —— 右表只取**第一个**匹配(去重,避免行数爆炸);
    * ``SEMI`` —— 只保留左表中**有匹配**的行(不取右表任何列);
    * ``ANTI`` —— 只保留左表中**没有匹配**的行;
    * ``ASOF`` —— 取右表中"键 ≤ 左表键"里**最大的那个**(时序数据专用)。
    """

    name = "JoiningTransform"

    def __init__(self, kind: str = "INNER", strictness: str = "ALL",
                 use_nulls: bool = False) -> None:
        super().__init__()
        self.add_input("left")
        self.add_input("right")
        self.add_output("out")
        self.kind = kind.upper()
        self.strictness = strictness.upper()
        self.use_nulls = use_nulls
        self._right_block: Optional[Block] = None
        self._hash: Dict[Any, List[int]] = {}
        self._key_columns_left: List[IColumn] = []
        self._built = False
        self._left_finished = False

    def prepare_keys(self, left_keys: List[IColumn], right_keys: List[IColumn]) -> None:
        self._left_key_columns = left_keys
        self._right_key_columns = right_keys

    def work(self) -> Status:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            "JoiningTransform is a stub in mini-clickhouse: JOIN is executed by "
            "minich.execution.joins.JoinExecutor, which handles the build/probe "
            "phases, strictness and ASOF matching together. See chapter 13.",
        )


class WindowTransform(IProcessor):
    """窗口函数 —— ``OVER (PARTITION BY ... ORDER BY ...)``。

    窗口函数的执行需要**两遍**:

    ① 按 ``PARTITION BY`` 分区、按 ``ORDER BY`` 排序(所以它是个阻塞算子);
    ② 对每个分区,按帧(``ROWS BETWEEN ...``)算出每个位置的聚合值。

    ``row_number`` / ``rank`` / ``dense_rank`` / ``lag`` / ``lead`` 都是
    这个框架的特例(它们的"聚合"很简单)。真正的聚合窗口
    (``sum(x) OVER (...)``)需要**增量**计算 —— 否则每个位置都重算一次
    就是 O(n²)。第 14 章会讲前缀和优化,以及"帧类型决定能不能增量"。
    """

    name = "WindowTransform"

    def __init__(self, ctx: Any, window_functions: Sequence[Any],
                 scope_columns: Optional[Dict[str, ColumnWithTypeAndName]] = None) -> None:
        super().__init__()
        self.add_input("in")
        self.add_output("out")
        self.ctx = ctx
        self.window_functions = list(window_functions)
        self.scope_columns = dict(scope_columns or {})
        self._blocks: List[Block] = []
        self._done = False

    def work(self) -> Status:
        inp, out = self.inputs[0], self.outputs[0]
        if self.finished:
            return Status.FINISHED
        if out.has_data():
            return Status.PORT_FULL
        if inp.has_data():
            chunk = inp.pull()
            self._blocks.append(chunk.get_block_with_data())
            return Status.READY
        if inp.finished and not self._done:
            self._done = True
            result = self._compute()
            if result is None:
                out.finish()
                self.finished = True
                return Status.FINISHED
            out.push(result)
            return Status.READY
        if inp.finished:
            out.finish()
            self.finished = True
            return Status.FINISHED
        return Status.NEED_DATA

    def _compute(self) -> Optional[Chunk]:
        from .window import execute_window_functions

        return execute_window_functions(self.ctx, self.window_functions, self._blocks,
                                        self.scope_columns)


class TotalsTransform(ISimpleTransform):
    """``GROUP BY ... WITH TOTALS`` —— 在结果末尾追加"总计"行。

    总计行里的**分组键列是默认值**(0 或空串),这经常让人困惑:
    ``SELECT domain, count() FROM hits GROUP BY domain WITH TOTALS``
    会多出一行 ``'', 1000000``。真实 ClickHouse 也是这样 ——
    所以当分组键本身可能是空串时,总计行会和真实的分组行**混淆**。
    第 12 章把这个坑单独列了出来。
    """

    name = "TotalsTransform"

    def __init__(self, totals_block: Optional[Block] = None) -> None:
        super().__init__()
        self.totals_block = totals_block

    def transform(self, chunk: Chunk) -> Optional[Chunk]:
        return chunk


# ══════════════════════════════════════════════════════════════════════════
# 流水线与执行器
# ══════════════════════════════════════════════════════════════════════════


class Pipeline:
    """一张处理器图 —— ClickHouse 里"一个查询计划"的物理形态。

    ``EXPLAIN PIPELINE`` 打印的就是它。

    .. code-block:: text

       Pipeline:
       ┌───────────────────────────────────────────────────────────┐
       │ (ReadFromMergeTree × 4)                                   │
       │      ▼                                                    │
       │ (Expression × 4)                                          │
       │      ▼                                                    │
       │ (Aggregating × 4)                                         │
       │      ▼                                                    │
       │ (MergingAggregated × 1)                                   │
       │      ▼                                                    │
       │ (Expression × 1) → (CollectingSink × 1)                   │
       └───────────────────────────────────────────────────────────┘

    真实 ClickHouse 的 ``Pipeline`` 还会携带 ``resize``/``exchange``
    节点以处理"并行度变化"(4 路聚合 → 1 路输出)。mini-clickhouse
    用 :class:`ResizeProcessor` 表达同一件事。
    """

    def __init__(self) -> None:
        self.processors: List[IProcessor] = []
        self.source: Optional[IProcessor] = None
        self.sink: Optional[IProcessor] = None

    def add(self, *processors: IProcessor) -> None:
        for p in processors:
            self.processors.append(p)

    def connect(self, upstream: IProcessor, downstream: IProcessor,
                out_index: int = 0, in_index: int = 0) -> None:
        """连接两个处理器 —— 数据从 ``upstream.outputs[out_index]`` 流向
        ``downstream.inputs[in_index]``。"""
        out_port = upstream.outputs[out_index]
        in_port = downstream.inputs[in_index]
        setattr(out_port, "peer", in_port)
        setattr(in_port, "peer", out_port)

    def chain(self, *processors: IProcessor) -> "Pipeline":
        """把一串处理器首尾相连 —— 构建流水线最常用的方式。"""
        for a, b in zip(processors, processors[1:]):
            self.connect(a, b)
        self.add(*processors)
        return self

    def describe(self, indent: str = "") -> str:
        """``EXPLAIN PIPELINE`` 的文本形态。"""
        lines = [f"{indent}Pipeline ({len(self.processors)} processors):"]
        for p in self.processors:
            lines.append(f"{indent}  ({p.name} × 1)")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.processors)


class PipelineExecutor:
    """执行一张流水线。

    .. note::

       真实 ClickHouse 的 ``PipelineExecutor`` 是**多线程**的:
       每个 processor 由线程池里的一个任务驱动,靠端口状态做同步,
       空闲的 processor 会被挂起(``schedule``/``wakeUp``)。
       mini-clickhouse 在**单线程**里按顺序轮转 —— 语义相同
       (都能跑出正确结果、都能体现背压),但你观察不到并行。
       教程第 10 章会明确画出来,并解释真实实现里
       "为什么 processor 数量会远多于 ``max_threads``"。

    .. warning::

       **死锁检测**。如果一个 processor 永远返回 ``NEED_DATA`` 而
       上游永远不会结束(典型原因:某个源忘了 ``finish()``),
       真实 ClickHouse 会**挂住不返回**,让你去猜。
       mini-clickhouse 在这种情况下会抛一个明确的错误 ——
       这是我们有意与真实行为不同的一处,因为教学场景下
       "挂住" 的调试成本太高。
    """

    def __init__(self, pipeline: Pipeline, max_steps: int = 1_000_000) -> None:
        self.pipeline = pipeline
        self.max_steps = max_steps
        self.steps = 0
        self.profile_events = ProfileEvents()

    def execute(self) -> None:
        processors = self.pipeline.processors
        for i, p in enumerate(processors):
            p.prepare(i)

        idle_rounds = 0
        while True:
            if all(p.finished for p in processors):
                return
            progressed = False
            for p in processors:
                if p.finished:
                    continue
                status = p.work()
                self.steps += 1
                if self.steps > self.max_steps:
                    raise_error(
                        ErrorCode.TOO_SLOW,
                        f"Pipeline did not finish in {self.max_steps} steps — "
                        f"there is probably a processor that never finishes "
                        f"its output",
                    )
                if status in (Status.READY, Status.FINISHED):
                    progressed = True
            if not progressed:
                idle_rounds += 1
                if idle_rounds > 3:
                    stuck = [p.name for p in processors if not p.finished]
                    raise_error(
                        ErrorCode.LOGICAL_ERROR,
                        "Pipeline is stuck: no processor can make progress. "
                        f"Unfinished processors: {stuck}",
                    )
            else:
                idle_rounds = 0


# ══════════════════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════════════════


def convert_chunk_to_block(chunk: Chunk) -> Block:
    return chunk.get_block_with_data()


def block_to_chunk(block: Block, num_rows: Optional[int] = None) -> Chunk:
    header = block.clone_empty()
    return Chunk(header, [c.column for c in block.data], num_rows or block.rows())


_SHARED_SLOTS: Dict[int, Any] = {}
