"""查询构建器 —— 把"用户想要的检索"变成"一次可执行的计划"。

这个模块是用户接触 LanceDB 的第一层 API::

    tbl.search(vector).limit(5).where("id > 100").select(["id", "text"]).to_list()
    tbl.search("机器 学习", query_type="fts").limit(3).phrase().to_list()
    tbl.search(vector).text("hello").rerank("rrf").to_list()

它比 :mod:`mini_lancedb.scanner` 高一层。分层的原因是**两类查询的
"计划"根本不是一回事**：

* **纯扫描**（``SELECT ... WHERE ... LIMIT``）的计划核心是"少读字节" ——
  列投影下推、zone map 剪枝、延迟物化，全部服务于此。这是
  :class:`~mini_lancedb.scanner.Scanner` 的主场。
* **向量检索**的计划核心是"少算距离" —— 先用索引把候选集缩小到
  ``nprobes`` 个分区的量级，再精算。它的性能瓶颈是**算力**，不是带宽。
* **全文检索**的计划核心是"少扫文档" —— 倒排表直接把查询词映射到
  文档集合，再按 BM25 排序。它的性能瓶颈是**postings 的长度**。

硬把三者塞进一个执行器，结果会是一个到处是 ``if`` 的巨型函数。
所以这里刻意分成两条路径，只在**结果整形**（``_distance`` / ``_score`` /
``_relevance_score`` 三列的方向和命名）上强制统一 —— 那才是用户真正
感知到的契约。

**三个分数列的方向，是这个模块最容易出错的地方**（见
:data:`mini_lancedb.rerank.base.SCORE_SCALES`）：

.. list-table::
   :header-rows: 1

   * - 列名
     - 何时出现
     - 方向
   * - ``_distance``
     - 向量检索
     - **越小越好**（升序）
   * - ``_score``
     - 全文检索
     - **越大越好**（降序）
   * - ``_relevance_score``
     - 混合检索（重排器输出）
     - **越大越好**

**prefilter / postfilter 是这里最容易被误解的语义。** 真实 LanceDB 的
文档明确写过：``prefilter=False`` 时**可能返回少于 k 行**。原因很直白 ——
它先让 ANN 取回 k 个候选，再用谓词去筛，筛掉的就没了。这不是 bug，
是"先排序后过滤"这个顺序的必然结果；很多人第一次遇到会以为索引坏了。
本模块把两种语义都实现出来并让差异可断言，因为只有亲手看到
"同一个查询、只改一个开关，结果从 10 行变成 3 行"，才算真的理解了它。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from .arrays import Array, PrimitiveArray, array_from_pylist
from .errors import IndexNotFoundError, NotSupportedError, QueryError
from .record import RecordBatch
from .rerank import (
    DISTANCE_COL,
    RELEVANCE_COL,
    ROWID_COL,
    SCORE_COL,
    RRFReranker,
    Reranker,
    get_reranker,
)
from .schema import Field, Schema
from .types import FLOAT32, FLOAT64, UINT64

__all__ = [
    "Query",
    "VectorQuery",
    "FTSQuery",
    "HybridQuery",
    "create_query",
    "DEFAULT_LIMIT",
]

#: 向量 / 全文 / 混合检索的默认 ``limit``。
#: **扫描查询的默认是 ``None``（全都要）**，这个差异是实测真实
#: LanceDB 得到的：``tbl.search().to_arrow()`` 返回全表，
#: 而 ``tbl.search(vec).to_arrow()`` 只返回 10 行。
DEFAULT_LIMIT = 10

#: 认得的 ``query_type`` 取值。
_QUERY_TYPES = ("vector", "fts", "hybrid", "auto")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _as_sql(where: Any) -> str:
    """把 ``where`` 参数统一成 SQL 字符串。

    真实 LanceDB 同时接受 ``str`` 和 ``Expr``（``col("id") > 100``）。
    本实现只认字符串，但**认得 ``to_sql()``**，所以任何长得像表达式的
    对象都能塞进来 —— 这样上层不用关心用户传的是哪一种。
    """

    if where is None:
        return "true"
    if isinstance(where, str):
        return where
    to_sql = getattr(where, "to_sql", None)
    if callable(to_sql):
        return str(to_sql())
    raise QueryError(
        "where 必须是 SQL 字符串或带 to_sql() 的表达式对象",
        got=type(where).__name__,
    )


def _as_column_list(columns: Any) -> Optional[List[str]]:
    if columns is None:
        return None
    if isinstance(columns, str):
        return [columns]
    if isinstance(columns, Mapping):  # 真实 API 也接受 dict（列别名）
        return [str(k) for k in columns.keys()]
    return [str(c) for c in columns]


def _empty_batch(schema: Schema) -> RecordBatch:
    return RecordBatch.empty(schema)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class Query:
    """查询构建器的基类。

    它是**不可变 + 链式**的：每个设置方法都返回 ``self``（就地改），
    但**执行方法不消费查询对象** —— 同一个 ``Query`` 可以反复
    ``to_list()``，也可以在中间改一个参数再跑一次。真实 LanceDB 的
    构建器是 Rust 侧的 ``Query`` 对象，同样是"可重复执行"的语义。

    子类只改两件事：

    1. ``_default_limit``（扫描是 ``None``，检索是 10）；
    2. :meth:`_execute` 用哪条路径。
    """

    #: 这条查询的类别，用于 ``explain`` 与结果整形。
    _kind = "scan"
    _default_limit: Optional[int] = None

    def __init__(self, table: Any, *, vector_column_name: Optional[str] = None) -> None:
        self._table = table
        self._vector_column_name = vector_column_name

        self._limit: Optional[int] = self._default_limit
        self._offset: int = 0
        self._columns: Optional[List[str]] = None
        self._where: Optional[str] = None
        #: ``True`` = 先过滤再检索（谓词下推进索引）；``False`` = 先检索再过滤
        self._prefilter: bool = True
        #: ``fast_search()`` 打开后：**没有可用索引就返回空结果**
        self._fast_search: bool = False
        self._bypass_index: bool = False
        self._with_row_id: bool = False
        self._reranker: Optional[Reranker] = None
        self._rerank_query: Optional[str] = None
        #: 向量检索调参（扫描查询上设了也不报错，只是不起作用）
        self._nprobes: Optional[int] = None
        self._refine_factor: Optional[int] = None
        self._ef: Optional[int] = None
        self._metric: Optional[str] = None

        #: 最近一次执行的实测数字（``analyze_plan`` 从这里取）
        self._measured: Dict[str, Any] = {}
        #: 最近一次执行的计划树（已经渲染成行）
        self._plan_lines: List[str] = []

    # -- 数据集 -----------------------------------------------------------
    @property
    def _dataset(self) -> Any:
        """**每次执行时**才去取 dataset。

        这一点很重要：``query = tbl.search(v).limit(5)`` 之后用户可能
        又 ``tbl.add(...)`` 或 ``tbl.checkout(2)``，构建器必须看到
        **最新**的表状态，而不是构造时的快照。缓存 dataset 会让
        "先建查询后写数据"这种用法静默查到旧版本。
        """

        return self._table._dataset

    # -- 链式设置 ---------------------------------------------------------
    def limit(self, limit: Optional[int]) -> "Query":
        """最多返回多少行。``None`` 表示不限制。"""

        if limit is not None:
            limit = int(limit)
            if limit < 0:
                raise QueryError("limit 必须非负", limit=limit)
        self._limit = limit
        return self

    def offset(self, offset: int) -> "Query":
        """跳过前 n 行。

        对向量检索来说 ``offset`` 很贵：它**不能**让索引少算距离，
        只能让最终少返回几行。真实 LanceDB 也把它标成"有性能代价"。
        """

        offset = int(offset)
        if offset < 0:
            raise QueryError("offset 必须非负", offset=offset)
        self._offset = offset
        return self

    def select(self, columns: Union[Sequence[str], Mapping[str, Any], str]) -> "Query":
        """投影。

        **注意向量检索时 ``_distance`` 永远保留**，哪怕没写进 ``columns``
        —— 去掉它等于让结果无法解释"为什么这行排第一"。真实 LanceDB
        同样是这个行为（``.select(["id"])`` 返回 ``id`` + ``_distance``）。
        """

        self._columns = _as_column_list(columns)
        return self

    def where(self, where: Any, prefilter: bool = True) -> "Query":
        """过滤条件。

        Parameters
        ----------
        prefilter :
            * ``True``（默认）—— 谓词**先**算出行掩码，交给向量索引，
              索引只在掩码允许的行里挑最近邻。结果是"最多 k 行，
              且全部满足谓词"；
            * ``False`` —— 索引**先**取回 ``k * max(1, refine_factor)``
              个候选，再拿谓词去筛。于是**可能少于 k 行**，甚至 0 行。

            什么时候该关掉？当谓词命中的行很多（比如 90%）时，
            prefilter 会让索引"在很小的候选池里挑"，反而丢召回；
            此时 postfilter 更划算。反过来，谓词很挑剔（命中 1%）时
            prefilter 能把 ANN 的工作量降两个数量级。
        """

        self._where = _as_sql(where)
        self._prefilter = bool(prefilter)
        return self

    def postfilter(self) -> "Query":
        """把已经设过的 ``where`` 切成"先检索后过滤"。

        等价于 ``self.where(self._where, prefilter=False)``。这个 API 在
        真实 LanceDB 早期版本存在，现在推荐直接写 ``prefilter=False``；
        保留它是为了让老代码能直接跑。
        """

        self._prefilter = False
        return self

    def fast_search(self) -> "Query":
        """**只用索引**。没有可用索引 → 返回空结果集。

        ⚠️ 这是个真正的陷阱，真实 LanceDB 就是这个行为：它不会报错，
        不会回退到暴力扫描，而是**安静地返回 0 行**。设计意图是
        "生产查询不允许因为索引丢了就退化成全表扫描"，但代价是
        索引没建好时你会看到"查不到东西"而不是"报错"。

        所以：**只要用了 ``fast_search()``，就必须有测试断言
        "索引存在且返回行数 > 0"**，否则一次误删索引就能让线上
        所有查询静默返回空。
        """

        self._fast_search = True
        return self

    def bypass_vector_index(self) -> "Query":
        """绕开向量索引，对全表做暴力扫描。

        **结果和索引检索一样准，但不一定和它一样** —— 索引是近似的，
        暴力是精确的。调试"索引召回率是不是有问题"时，
        把它和索引的结果对比是最直接的手段。
        """

        self._bypass_index = True
        return self

    def with_row_id(self, with_row_id: bool = True) -> "Query":
        """在结果里加一列 ``_rowid``。

        **本实现（与真实 LanceDB 的差异）**：这里给的是**逻辑行号**
        （0..count_rows-1），而真实 LanceDB 给的是稳定的 64 位行地址
        ``(fragment_id << 32) | offset``。逻辑行号在压实或删除之后
        会变，所以**不要把它持久化**；教程里用它是因为它一眼能看懂，
        而且和 :meth:`mini_lancedb.dataset.Dataset.row_address` 可互相换算。
        """

        self._with_row_id = bool(with_row_id)
        return self

    def rerank(self, reranker: Union[Reranker, str, None]) -> "Query":
        """设置重排器。

        可以传名字（``"rrf"`` / ``"linear"`` / ``"cross_encoder"`` /
        ``"colbert"`` / ``"none"``）或一个 :class:`Reranker` 实例。
        单路查询（纯向量、纯 FTS）上设它也是合法的：会走
        ``rerank_vector`` / ``rerank_fts``，用来给结果换一把尺子
        （比如"用 cross encoder 精排 top 20"）。
        """

        self._reranker = get_reranker(reranker) if not isinstance(reranker, Reranker) else reranker
        return self

    # -- 索引调参 ---------------------------------------------------------
    def nprobes(self, n: int) -> "Query":
        """探多少个 IVF 分区。

        ``nprobes=1`` 最快、召回最差；``nprobes=num_partitions``
        退化成精确扫描（此时 ANN 不再"近似"）。这是**唯一一个
        能直接拿召回率换延迟**的旋钮，也是性能章节的主角。
        """

        self._nprobes = int(n)
        return self

    def refine_factor(self, refine_factor: int) -> "Query":
        """精排倍数。

        取回 ``k * refine_factor`` 个候选，再用**完整精度**重算距离
        并截回 k 个。它对付的是量化误差（PQ / SQ 把向量压过，
        距离是估计值）。代价是候选多了 ``refine_factor`` 倍。

        在 postfilter 路径上它还有一个副作用：候选池越大，
        谓词筛完剩下的行越多 —— 这正是 postfilter 掉行数的解药。
        """

        self._refine_factor = int(refine_factor)
        return self

    def ef(self, ef: int) -> "Query":
        """HNSW 的搜索宽度（动态候选表大小）。

        ``ef`` 越大召回越高、越慢；它必须 ≥ ``k``，否则图表搜索
        连 k 个邻居都攒不齐。真实 LanceDB 在 IVF_HNSW_* 索引上
        用它，纯 IVF 索引上设了无效。
        """

        self._ef = int(ef)
        return self

    def distance_type(self, distance_type: str) -> "Query":
        """查询侧的距离度量（``"l2"`` / ``"cosine"`` / ``"dot"``）。

        **它应该和建索引时用的度量一致。** 不一致时不会报错，
        只会静默变差：索引用 l2 建、查询用 cosine 算，那么
        "索引挑出来的候选"和"重排后的顺序"就是两套标准。
        """

        self._metric = str(distance_type)
        return self

    # 「和链式 API 对齐的别名」：真实 LanceDB 里向量查询上的名字叫 ``metric``
    metric = distance_type

    # -- 执行 -------------------------------------------------------------
    def _pipeline(self) -> "_SearchPipeline":
        return _SearchPipeline(self)

    def to_batch(self) -> RecordBatch:
        """执行并返回一个 :class:`~mini_lancedb.record.RecordBatch`。"""

        return self._pipeline().run()

    def to_list(self) -> List[Dict[str, Any]]:
        return self.to_batch().to_pylist()

    def to_arrow(self) -> Any:
        return self.to_batch().to_arrow()

    def to_pydict(self) -> Dict[str, List[Any]]:
        return self.to_batch().to_pydict()

    def to_pandas(self, **kwargs: Any) -> Any:
        return self.to_batch().to_pandas()

    def to_polars(self) -> Any:
        pl = _import_polars()
        return pl.from_arrow(self.to_arrow())

    def to_batches(self, batch_size: Optional[int] = None) -> Any:
        """流式读取：返回 ``pyarrow.RecordBatchReader``。

        和 :meth:`to_arrow` 的区别不在结果，在于**内存**：一个
        千万行的结果集 ``to_arrow()`` 会在内存里拼出一个完整表，
        而 ``to_batches()`` 是一批一批地吐。真实 LanceDB 的
        ``to_batches()`` 直接对应 Lance 的流式读取器。
        """

        import pyarrow as pa

        batch = self.to_batch()
        size = int(batch_size) if batch_size else 4096
        pieces = batch.split_by_rows(size) if batch.num_rows else []
        table = batch.to_arrow()
        # ``mini RecordBatch.to_arrow()`` 给的是 ``pa.Table``；而
        # ``RecordBatchReader`` 要的是 ``pa.RecordBatch``。少了这一步
        # 会在 **read_all() 时**报 "Could not unwrap RecordBatch"，
        # 而不是在构造时 —— 这种"延迟报错"最难查。
        arrow_batches = [p for piece in pieces for p in piece.to_arrow().to_batches()]
        return pa.RecordBatchReader.from_batches(table.schema, arrow_batches)

    def to_df(self) -> Any:
        """真实 LanceDB 的老名字，等价于 :meth:`to_pandas`。"""

        return self.to_pandas()

    # -- 解释 -------------------------------------------------------------
    def explain(self, verbose: bool = False) -> str:
        """打印计划树（**不执行查询**，除了算剪枝信息时的一点点读）。"""

        lines = self._render_plan(analyze=False, verbose=verbose)
        return "\n".join(lines)

    # 真实 LanceDB 的名字
    def explain_plan(self, verbose: Optional[bool] = False) -> str:
        return self.explain(verbose=bool(verbose))

    def analyze_plan(self) -> str:
        """**执行查询**并打印计划 + 实测 I/O。

        和 ``explain()`` 的差别就是"测量"与"估算"的差别：
        ``explain()`` 只能告诉你"计划用什么算子"，``analyze_plan()``
        才能告诉你"这个算子实际读了多少字节、扫了多少行"。
        性能调优必须看后者 —— 计划的形状对不代表下推真的生效了。
        """

        self.to_batch()  # 真跑一遍，把数字填进 self._measured
        lines = self._render_plan(analyze=True, verbose=True)
        return "\n".join(lines)

    def output_schema(self) -> Any:
        return self.to_batch().schema.to_arrow()

    # -- 内部：计划渲染 ---------------------------------------------------
    def _render_plan(self, *, analyze: bool, verbose: bool) -> List[str]:
        """基类（纯扫描）也有计划 —— 只是节点少一些。

        让基类直接实现（而不是留抽象方法）是因为 ``search()`` 无参时
        拿到的就是基类实例，它是**最常用的一条查询**；留成抽象方法
        会让 ``tbl.search().explain()`` 抛 ``NotImplementedError``，
        而那个异常完全不该出现在一个合法调用上。
        """

        return _SearchPipeline(self).render_plan(analyze=analyze, verbose=verbose)

    # -- 内部：结果整形 ---------------------------------------------------
    def _score_columns(self) -> List[str]:
        kind = self._kind
        if kind == "vector":
            return [DISTANCE_COL]
        if kind == "fts":
            return [SCORE_COL]
        if kind == "hybrid":
            return [RELEVANCE_COL]
        return []

    def _projection(self, schema: Schema) -> List[str]:
        """输出里除了分数列之外的列。"""

        cols = list(self._columns) if self._columns else list(schema.names)
        return [c for c in cols if c not in (DISTANCE_COL, SCORE_COL, RELEVANCE_COL)]

    def _output_schema(self, schema: Schema) -> Schema:
        fields: List[Field] = []
        for name in self._projection(schema):
            if name in schema:
                fields.append(schema[name])
        for name in self._score_columns():
            if name == DISTANCE_COL:
                fields.append(Field(name, FLOAT32, nullable=True))
            elif name == SCORE_COL:
                fields.append(Field(name, FLOAT64, nullable=True))
            else:
                fields.append(Field(name, FLOAT32, nullable=True))
        if self._with_row_id:
            fields.append(Field(ROWID_COL, UINT64, nullable=False))
        return Schema(fields)

    def __repr__(self) -> str:  # pragma: no cover - 只为调试
        bits = [f"kind={self._kind}"]
        if self._limit is not None:
            bits.append(f"limit={self._limit}")
        if self._where:
            bits.append(f"where={self._where!r}")
        return f"<{type(self).__name__} {' '.join(bits)}>"


def _import_polars() -> Any:
    try:
        import polars  # type: ignore
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise ImportError(
            "需要 polars 才能用 to_polars()；安装：pip install polars"
        ) from exc
    return polars


# ---------------------------------------------------------------------------
# VectorQuery
# ---------------------------------------------------------------------------


class VectorQuery(Query):
    """最近邻检索。

    ``search(vector)`` 造出来的就是它。核心问题是"再多算一点还是多读一点"：
    ANN 索引让你少算距离，代价是**可能漏掉真正最近的那几个**；
    ``nprobes`` / ``refine_factor`` / ``ef`` 三个旋钮就是在这条曲线上挪位置。
    """

    _kind = "vector"
    _default_limit = DEFAULT_LIMIT

    def __init__(
        self,
        table: Any,
        vector: Any,
        *,
        vector_column_name: Optional[str] = None,
    ) -> None:
        super().__init__(table, vector_column_name=vector_column_name)
        self._vector = vector
        self._distance_range: Optional[Tuple[Optional[float], Optional[float]]] = None

    # -- 链式 -------------------------------------------------------------
    def vector(self, vector: Any) -> "VectorQuery":
        self._vector = vector
        return self

    def distance_range(
        self,
        lower_bound: Optional[float] = None,
        upper_bound: Optional[float] = None,
    ) -> "VectorQuery":
        """只保留距离落在 ``[lower, upper)`` 的行。

        它的价值在于**把"相似度阈值"变成一个可下推的过滤条件**：
        RAG 里"只把这些文档当上下文"本质上就是这个区间。
        注意边界语义按 **"下闭上开"**：向量之间的距离可以精确等于 0
        （同一行），闭区间下界才合理；上界开区间则避免
        "距离恰好等于阈值"时结果的抖动。
        """

        lo = None if lower_bound is None else float(lower_bound)
        hi = None if upper_bound is None else float(upper_bound)
        if lo is not None and hi is not None and lo > hi:
            raise QueryError("distance_range 下界不能大于上界", lower=lo, upper=hi)
        self._distance_range = (lo, hi)
        return self

    def multivector(self, *args: Any, **kwargs: Any) -> "VectorQuery":
        """多向量（ColBERT 式 late interaction）查询 —— **本实现未提供**。

        这里刻意抛异常而不是"悄悄退化成单向量"：多向量检索的语义是
        "逐 token 召回再取 max/求和"，和单向量的结果完全不同，
        静默退化会让用户拿到一份看起来正常、其实错的结果。

        想用多向量，请走重排器那条路：把 ``ColBERTReranker``
        接到 ``.rerank(...)`` 上（它实现了 token 级 MaxSim）。
        """

        raise NotSupportedError(
            "多向量（multivector）检索",
            hint="用 mini_lancedb.rerank.ColBERTReranker 走 .rerank(...) 路径，"
            "它实现了 token 级 MaxSim；真正的多向量索引本实现没有做",
        )

    # -- 计划 -------------------------------------------------------------
    def _render_plan(self, *, analyze: bool, verbose: bool) -> List[str]:
        return _SearchPipeline(self).render_plan(analyze=analyze, verbose=verbose)


# ---------------------------------------------------------------------------
# FTSQuery
# ---------------------------------------------------------------------------


class FTSQuery(Query):
    """全文检索（BM25）。

    和向量检索最本质的差别：**它读的是词，不是数字**。于是"分词配置"
    成了查询契约的一部分 —— 索引用哪套分词建的，查询就必须用同一套，
    否则 ``running`` 和 ``run`` 会被当成两个词，查不出来还不报错。
    所以 :class:`~mini_lancedb.index.inverted.InvertedIndex` 把
    tokenizer 配置写进了自己的元数据，查询时从索引里取回来用。
    """

    _kind = "fts"
    _default_limit = DEFAULT_LIMIT

    def __init__(
        self,
        table: Any,
        query: str,
        *,
        fts_columns: Optional[Union[str, Sequence[str]]] = None,
    ) -> None:
        super().__init__(table)
        if not isinstance(query, str):
            raise QueryError("FTS 查询必须是字符串", got=type(query).__name__)
        self._fts_query = query
        self._fts_columns = _as_column_list(fts_columns)
        self._phrase = False
        self._boost: Dict[str, float] = {}

    # -- 链式 -------------------------------------------------------------
    def phrase(self, enabled: bool = True) -> "FTSQuery":
        """短语查询：要求这些词**按顺序相邻**出现。

        代价是必须建索引时开了 ``with_position=True``（默认开）——
        没存位置信息的索引只能退化成"这些词的 AND"，这是**静默的
        精度损失**，不会有任何报错。
        """

        self._phrase = bool(enabled)
        return self

    # 真实 LanceDB 现在的名字
    def phrase_query(self, phrase_query: bool = True) -> "FTSQuery":
        return self.phrase(phrase_query)

    def columns(self, columns: Union[str, Sequence[str]]) -> "FTSQuery":
        """只在指定的文本列上检索。

        多列时**要求每一列都有自己的 FTS 索引**（LanceDB 的索引是按列建的）。
        指定了没有索引的列不会报错，只会说"这个要求没有索引能满足"。
        """

        self._fts_columns = _as_column_list(columns)
        return self

    def boost(self, boost: float, column: Optional[str] = None) -> "FTSQuery":
        """给某一列的分一个乘数。

        **本实现与真实 LanceDB 的差异**：真实 LanceDB 的 ``boost``
        是给融合器（``LinearCombinationReranker``）的一路权重，作用在
        **重排阶段**；这里把它作用在 **BM25 原始分**上（``_score *= boost``）。
        两种做法在单列检索时结果一致，多列时本实现的融合是"分数加权和"
        而不是"名次加权和"，所以**多列 + boost 的绝对顺序会不一样**。
        只用一列时请放心使用。
        """

        key = column
        if key is None:
            cols = self._fts_columns
            if not cols:
                # 没指定列 → 由执行期决定；用 ``None`` 这个哨兵键记着，
                # 执行时再展开成"所有命中的列"。
                key = None
            else:
                key = cols[0]
        self._boost[key] = float(boost)
        return self

    # -- 计划 -------------------------------------------------------------
    def _render_plan(self, *, analyze: bool, verbose: bool) -> List[str]:
        return _SearchPipeline(self).render_plan(analyze=analyze, verbose=verbose)


# ---------------------------------------------------------------------------
# HybridQuery
# ---------------------------------------------------------------------------


class HybridQuery(Query):
    """向量 + 全文两路召回，交给重排器融合。

    **为什么不能"把两个分数加起来"。** 向量路的 ``_distance`` 落在
    ``[0, 2]``（cosine），FTS 路的 ``_score`` 是 BM25、典型 0~30 且
    依赖语料统计。直接相加等于把"米"和"摄氏度"相加。所以默认重排器
    是 :class:`RRFReranker` —— 它**只看名次，不看分数**，对量纲免疫。

    真实 LanceDB 的 ``HybridQuery`` 默认也是 RRF。
    """

    _kind = "hybrid"
    _default_limit = DEFAULT_LIMIT

    def __init__(
        self,
        table: Any,
        vector: Any,
        text: Optional[str] = None,
        *,
        vector_column_name: Optional[str] = None,
        fts_columns: Optional[Union[str, Sequence[str]]] = None,
    ) -> None:
        super().__init__(table, vector_column_name=vector_column_name)
        self._vector = vector
        self._text = text
        self._fts_columns = _as_column_list(fts_columns)
        self._reranker = RRFReranker()
        self._phrase = False

    # -- 链式 -------------------------------------------------------------
    def text(self, text: str) -> "HybridQuery":
        self._text = text
        return self

    def phrase(self, enabled: bool = True) -> "HybridQuery":
        self._phrase = bool(enabled)
        return self

    def phrase_query(self, phrase_query: bool = True) -> "HybridQuery":
        return self.phrase(phrase_query)

    def rerank(  # type: ignore[override]
        self,
        reranker: Union[Reranker, str, None] = None,
        *,
        normalize: str = "score",
    ) -> "HybridQuery":
        """设置融合用的重排器（默认 RRF）。

        ``normalize`` 是为了和真实 LanceDB 的签名对齐而保留的参数：
        它只在 ``linear`` 这类"看分数"的重排器上有意义，本实现把它
        透传成 ``normalize=`` 关键字（``LinearCombinationReranker``
        接受 ``"minmax"`` / ``"rank"`` / ``"none"``）。
        """

        if reranker is None:
            return self
        if normalize and not isinstance(reranker, Reranker):
            try:
                self._reranker = get_reranker(reranker, normalize=normalize)
                return self
            except Exception:
                pass
        self._reranker = (
            reranker if isinstance(reranker, Reranker) else get_reranker(reranker)
        )
        return self

    # -- 计划 -------------------------------------------------------------
    def _render_plan(self, *, analyze: bool, verbose: bool) -> List[str]:
        return _SearchPipeline(self).render_plan(analyze=analyze, verbose=verbose)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def create_query(
    table: Any,
    query: Any,
    query_type: str = "vector",
    *,
    vector_column_name: Optional[str] = None,
    fts_columns: Optional[Union[str, Sequence[str]]] = None,
) -> Query:
    """按 ``query_type`` 造一个合适的查询对象。

    ``query_type`` 取值：

    * ``"vector"`` —— ``query`` 是向量（或能被 embedding 函数编码的文本）；
    * ``"fts"`` —— ``query`` 是检索词；
    * ``"hybrid"`` —— ``query`` 是向量，另需在查询上 ``.text(...)``；
    * ``"auto"`` —— 按 ``query`` 的类型猜：``str`` → fts，数字序列 → vector。
      **真实 LanceDB 的默认值就是 ``"auto"``**，本实现把它也支持了，
      但 :meth:`LanceTable.search` 的默认值是 ``"vector"``（见那里的说明）。
    """

    if query_type not in _QUERY_TYPES:
        raise QueryError(
            f"不认识的 query_type {query_type!r}",
            supported=list(_QUERY_TYPES),
        )
    if query is None:
        if query_type == "vector":
            # 真实 LanceDB 的 ``search()`` 无参调用就是一个普通扫描
            return Query(table)
        raise QueryError(f"query_type={query_type!r} 时必须给出 query")
    if query_type == "auto":
        query_type = "fts" if isinstance(query, str) else "vector"
    if query_type == "fts":
        return FTSQuery(table, query, fts_columns=fts_columns)
    if query_type == "hybrid":
        # 真实 LanceDB 的写法是 ``tbl.search(向量, query_type="hybrid").text("关键词")``：
        # **query 参数是向量**，检索词由 ``.text()`` 给。这么设计是因为
        # 向量那一侧是"必填的召回路径"，文本那一侧才是可选的补充。
        if isinstance(query, str):
            raise QueryError(
                "hybrid 查询的 query 参数是**向量**，检索词请用 .text() 传",
                hint="tbl.search(vec, query_type='hybrid').text('关键词').rerank('rrf')",
            )
        return HybridQuery(
            table, query, None,
            vector_column_name=vector_column_name, fts_columns=fts_columns,
        )
    return VectorQuery(table, query, vector_column_name=vector_column_name)


# ---------------------------------------------------------------------------
# 执行流水线
# ---------------------------------------------------------------------------


class _SearchPipeline:
    """真正干活的执行器。

    一次执行的四个阶段，顺序固定：

    1. **解析** —— 向量列是哪一列？度量是什么？谓词掩码算不算？
    2. **召回** —— 走索引还是暴力；prefilter 还是 postfilter；
    3. **排序 / 截断** —— 距离升序、分数降序，套 ``distance_range``、``offset``；
    4. **物化 + 整形** —— 把行号回表成列，挂上 ``_distance`` / ``_score`` / ``_rowid``。

    第 4 步是**唯一一处三种查询共用**的代码（:meth:`_assemble`）。
    放在同一个地方的理由就是开头说的：分数列的方向和命名是用户可见的
    契约，让它在三处各写一遍迟早会有一处写反。
    """

    def __init__(self, query: Query) -> None:
        self.q = query
        self.ds = query._dataset
        #: 实测数字，供 ``analyze_plan`` 使用
        self.measured: Dict[str, Any] = {}
        #: 计划节点（步骤字符串），供 ``explain`` 使用
        self.nodes: List[str] = []
        self.notes: List[str] = []
        #: 索引元数据（有索引时）
        self.index_meta: Any = None

    # -- 入口 -------------------------------------------------------------
    def run(self) -> RecordBatch:
        kind = self.q._kind
        if kind == "vector":
            batch = self._run_vector()
        elif kind == "fts":
            batch = self._run_fts()
        elif kind == "hybrid":
            batch = self._run_hybrid()
        else:
            batch = self._run_scan()
        self.q._measured = self.measured
        return batch

    # ------------------------------------------------------------------
    # 纯扫描
    # ------------------------------------------------------------------
    def _run_scan(self) -> RecordBatch:
        q = self.q
        self.nodes = ["LanceRead: 片段级列存扫描"]
        limit = None if q._limit is None else q._limit + q._offset
        scanner = self.ds.scanner(
            columns=q._columns,
            filter=None if not q._where or q._where == "true" else q._where,
            limit=limit,
            with_row_id=q._with_row_id,
        )
        batch = scanner.to_batch()
        self.measured["scan"] = scanner.stats.summary()
        self.nodes.append(
            "ProjectionPushdown: 只读 " + ", ".join(sorted(set(scanner._needed_columns())))
        )
        if scanner.filter_sql:
            self.nodes.append(f"FilterExec: {scanner.filter_sql}")
        if q._with_row_id and ROWID_COL not in batch:
            batch = batch.add_rowid(0)
        return _apply_offset(batch, q._offset, q._limit)

    # ------------------------------------------------------------------
    # 向量检索
    # ------------------------------------------------------------------
    def _resolve_vector_column(self) -> str:
        q = self.q
        schema = self.ds.schema
        col = q._vector_column_name
        if col is None:
            col = schema.default_vector_column()
        if col is None:
            raise QueryError(
                "无法确定用哪个向量列",
                available=schema.vector_columns,
                hint="表里有多个向量列，请显式传 vector_column_name=...",
            )
        if col not in schema:
            raise QueryError(
                f"向量列不存在: {col!r}",
                available=schema.names,
            )
        if not schema[col].is_vector:
            raise QueryError(
                f"{col!r} 不是向量列",
                type=str(schema[col].type),
                hint="向量列必须是 FixedSizeList<float> 之类的定长数值列",
            )
        return col

    def _query_vector(self, column: str) -> np.ndarray:
        """把 ``query`` 参数变成一维 float32 向量。

        两条路：给的就是向量 → 直接用；给的是字符串 → 找这个列上注册的
        embedding 函数把文本编码成向量。后者就是教程里
        ``tbl.search("一段话")`` 能直接跑的原因。
        """

        q = self.q
        raw = q._vector
        if isinstance(raw, str):
            fn = self._embedding_function_for(column)
            if fn is None:
                raise QueryError(
                    "查询是文本，但这一列没有注册 embedding 函数",
                    column=column,
                    hint="要么直接给向量，要么在 create_table 时注册 embedding_functions",
                )
            vec = np.asarray(fn.query_vector(raw), dtype=np.float32).reshape(-1)
            self.notes.append(f"EmbeddingQuery: {type(fn).__name__}.query_vector(文本)")
            return vec
        if hasattr(raw, "to_numpy"):  # numpy / 任何类数组
            raw = raw.to_numpy()
        arr = np.asarray(raw, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            raise QueryError("查询向量为空")
        return arr

    def _embedding_function_for(self, column: str) -> Any:
        table = self.q._table
        getter = getattr(table, "_embedding_function_for", None)
        if getter is None:
            return None
        return getter(column)

    def _find_vector_index(self, column: str) -> Optional[Any]:
        """找覆盖 ``column`` 且仍然有效的向量索引。

        ``valid=False`` 的索引一律跳过 —— 压实会把行号全打乱，
        继续用它等于拿旧地址去查新表格，可能返回**别的行**。
        宁可退化成暴力扫描（慢但正确）。
        """

        best = None
        for meta in self.ds.indices:
            if not meta.is_vector or not meta.valid:
                continue
            if column not in meta.columns:
                continue
            # 优先覆盖行数最多的那个
            if best is None or meta.num_indexed_rows > best.num_indexed_rows:
                best = meta
        if best is None:
            return None
        self.index_meta = best
        try:
            return self.ds.load_index(best.name)
        except Exception:  # pragma: no cover - 索引文件缺失时退化
            self.notes.append(f"索引 {best.name} 加载失败，退化为暴力扫描")
            self.index_meta = None
            return None

    def _read_vector_matrix(self, column: str) -> np.ndarray:
        """把整列向量读成一个 ``(n, d)`` 矩阵（逻辑行序）。"""

        batch = self.ds.scanner(columns=[column]).to_batch()
        if batch.num_rows == 0:
            dim = self.ds.schema[column].vector_dim or 0
            return np.zeros((0, dim), dtype=np.float32)
        return np.asarray(batch.to_numpy(column), dtype=np.float32)

    def _brute_force(
        self,
        column: str,
        vector: np.ndarray,
        k: int,
        mask: Optional[np.ndarray],
        metric: str,
    ) -> Tuple[np.ndarray, np.ndarray]:
        from .index.metric import pairwise_distances, top_k

        matrix = self._read_vector_matrix(column)
        if matrix.shape[0] == 0:
            return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.int64)
        if matrix.shape[1] != vector.size:
            raise QueryError(
                "查询向量维度与表不符",
                expected=int(matrix.shape[1]),
                got=int(vector.size),
            )
        distances = pairwise_distances(matrix, vector[None, :], metric)[0]
        if mask is not None:
            if mask.shape[0] != distances.shape[0]:
                raise QueryError("谓词掩码长度与表不符")
            distances = np.where(mask, distances, np.inf)
        self.measured["brute_force"] = {
            "rows_scored": int(matrix.shape[0]),
            "dim": int(matrix.shape[1]),
            "metric": metric,
        }
        values, indices = top_k(distances, k)
        return (
            np.asarray(values, dtype=np.float32),
            np.asarray(indices, dtype=np.int64),
        )

    def _run_vector(self) -> RecordBatch:
        q = self.q
        column = self._resolve_vector_column()
        vector = self._query_vector(column)

        dim = self.ds.schema[column].vector_dim
        if dim is not None and vector.size != dim:
            raise QueryError(
                "查询向量维度与表不符",
                expected=int(dim),
                got=int(vector.size),
                column=column,
            )

        # ``limit=None`` 表示"全都要"（和扫描查询一致）。这里不能退回
        # ``DEFAULT_LIMIT`` —— 那样 ``limit(None)`` 就和"什么都不写"没法
        # 区分了，而它是明确写出来"我确实要全部"的意图。
        k = q._limit if q._limit is not None else max(1, self.ds.num_rows)
        if k <= 0:
            return _empty_batch(q._output_schema(self.ds.schema))
        # 候选数要含 offset：否则 ``limit(3).offset(3)`` 会先取 3 个、
        # 再丢掉 3 个，返回空 —— 一个只在同时用 limit 和 offset 时才现形的 bug。
        fetch = k + q._offset

        # ---- 1) 谓词掩码（prefilter 才算）----
        pre_mask: Optional[np.ndarray] = None
        if q._where and q._where != "true" and q._prefilter:
            pre_mask = self.ds._predicate_mask(q._where)
            self.nodes.append(f"FilterExec: {q._where}   # prefilter=True")

        # ---- 2) 选索引 ----
        index = None
        if not q._bypass_index:
            index = self._find_vector_index(column)
        if q._fast_search and index is None:
            # ⚠️ 真实 LanceDB 的行为：静默返回空结果，不报错、不回退
            self.notes.append(
                "fast_search() 已打开但**没有可用的向量索引** → 返回空结果"
                "（这是真实 LanceDB 的行为，不会报错，也不会退化成暴力扫描）"
            )
            self.measured["fast_search_empty"] = True
            batch = _empty_batch(q._output_schema(self.ds.schema))
            self.nodes.append("FastSearch: 无可用索引 → 空结果（EmptyExec）")
            return batch

        metric = q._metric or (
            (index.metadata.distance_type if index is not None else None) or "l2"
        )
        refine = max(1, int(q._refine_factor or 1))
        # postfilter：多取一些候选，给过滤留余量。默认 refine=1 → 只取 fetch 个，
        # 于是"筛完可能少于 k 行"，这正是要复现的语义。
        candidates = fetch if q._prefilter else max(fetch, fetch * refine)

        params: Dict[str, Any] = {}
        if q._nprobes is not None:
            params["nprobes"] = q._nprobes
        if q._ef is not None:
            params["ef"] = q._ef
        if q._refine_factor is not None:
            params["refine_factor"] = q._refine_factor

        # ---- 3) 召回 ----
        if index is not None:
            result = index.search(
                vector[None, :], candidates, mask=pre_mask, params=params or None
            )
            distances = np.asarray(result.distances[0], dtype=np.float32)
            row_ids = np.asarray(result.row_ids[0], dtype=np.int64)
            self.measured["index"] = dict(result.stats)
            self.measured["index_approximate"] = bool(result.approximate)
            self.nodes.append(
                f"KNNVectorDistance: metric={metric}"
            )
            self.nodes.append(
                f"ANNSubIndex: {self.index_meta.name} "
                f"({self.index_meta.index_type}, "
                f"nprobes={params.get('nprobes', '(默认)')})"
            )
            self.nodes.append(
                f"ANNIvfPartition: {self.index_meta.name} "
                f"({self.index_meta.index_type}, "
                f"partitions={self.index_meta.details.get('num_partitions', '?')}, "
                f"indexed_rows={self.index_meta.num_indexed_rows})"
            )
        else:
            distances, row_ids = self._brute_force(column, vector, candidates, pre_mask, metric)
            self.nodes.append(f"KNNVectorDistance: metric={metric}")
            self.nodes.append("FlatSearch: 暴力扫描（无可用向量索引）")

        if pre_mask is not None and self.index_meta is not None:
            self.nodes.append(
                f"ScalarIndexQuery: 谓词掩码下推给索引 "
                f"({int(pre_mask.sum())} 行候选)"
            )

        # ---- 4) postfilter ----
        if q._where and q._where != "true" and not q._prefilter and row_ids.size:
            pmask = self.ds._predicate_mask(q._where)
            keep = np.zeros(row_ids.shape[0], dtype=bool)
            valid = (row_ids >= 0) & (row_ids < pmask.shape[0])
            keep[valid] = pmask[row_ids[valid]]
            self.measured["postfilter_dropped"] = int((~keep).sum())
            distances, row_ids = distances[keep], row_ids[keep]
            self.nodes.append(
                f"FilterExec: {q._where}   # postfilter=True，"
                f"候选 {int(keep.size)} 行 → 剩 {int(keep.sum())} 行"
            )

        # ---- 5) 丢弃空槽 + distance_range + 排序 ----
        valid = row_ids >= 0
        distances, row_ids = distances[valid], row_ids[valid]
        if q._distance_range is not None:
            lo, hi = q._distance_range
            sel = np.ones(row_ids.shape[0], dtype=bool)
            if lo is not None:
                sel &= distances >= lo
            if hi is not None:
                sel &= distances < hi
            self.measured["distance_range_kept"] = int(sel.sum())
            distances, row_ids = distances[sel], row_ids[sel]
            self.nodes.append(
                f"DistanceRangeFilter: [{lo}, {hi})"
            )

        order = np.argsort(distances, kind="stable")[:fetch]
        distances, row_ids = distances[order], row_ids[order]
        self.nodes.append(f"TopK: limit={k}, offset={q._offset}  (按 _distance 升序)")

        if self.q._columns is None:
            self.nodes.append("LateMaterialization: 先定行号，再回表取投影列")

        row_ids = row_ids[q._offset :]
        distances = distances[q._offset :]
        return self._assemble(row_ids, distances=distances)

    # ------------------------------------------------------------------
    # 全文检索
    # ------------------------------------------------------------------
    def _find_fts_indices(self, columns: Optional[Sequence[str]]) -> List[Any]:
        out: List[Any] = []
        for meta in self.ds.indices:
            if meta.index_type != "FTS" or not meta.valid:
                continue
            if columns:
                if not any(c in meta.columns for c in columns):
                    continue
            out.append(meta)
        return out

    def _run_fts(self) -> RecordBatch:
        q = self.q
        metas = self._find_fts_indices(q._fts_columns)
        if not metas:
            raise IndexNotFoundError(
                "没有可用的全文索引，无法做全文检索",
                columns=q._fts_columns or self.ds.schema.names,
                hint="先建索引：table.create_index('text', config='FTS')",
            )

        k = q._limit if q._limit is not None else DEFAULT_LIMIT
        # 倒排索引的 ``k`` 是"从 BM25 排行榜上取前几个"，取完就没法再往回补。
        # 所以要把 offset 一起算进去，否则 ``limit(1).offset(1)`` 永远返回空。
        fetch = k + q._offset
        pre_mask: Optional[np.ndarray] = None
        if q._where and q._where != "true" and q._prefilter:
            pre_mask = self.ds._predicate_mask(q._where)

        text = f'"{q._fts_query}"' if q._phrase else q._fts_query
        if q._phrase:
            self.nodes.append(f"PhraseQuery: {q._fts_query!r}")

        per_route: List[Tuple[Any, np.ndarray, np.ndarray]] = []
        for meta in metas:
            index = self.ds.load_index(meta.name)
            query_obj = index.to_boolean_query(text)
            row_ids, scores = index.search_ranked(query_obj, fetch, mask=pre_mask)
            self.measured[f"fts:{meta.name}"] = dict(getattr(index, "last_search_stats", {}))
            boost = q._boost.get(meta.columns[0] if meta.columns else None)
            if boost is None:
                boost = q._boost.get(None)
            if boost is not None:
                scores = scores * float(boost)
                self.nodes.append(f"BoostQuery: {meta.columns} × {boost}")
            per_route.append((meta, np.asarray(row_ids, dtype=np.int64), np.asarray(scores, dtype=np.float64)))
            self.nodes.append(
                f"ScalarIndexQuery: query=[{text}]@{meta.name}"
                f"({meta.index_type}) on {meta.columns}"
            )

        if len(per_route) == 1:
            _, row_ids, scores = per_route[0]
        else:
            # 多列 → 没有"可比的分数"可言，用名次融合（RRF），
            # 这和多列 FTS 在真实 LanceDB 里的推荐做法一致。
            row_ids, scores = _rrf_merge_routes(per_route)
            self.nodes.append(f"RerankExec: RRF 融合 {len(per_route)} 条列路由")

        if q._where and q._where != "true" and not q._prefilter and row_ids.size:
            pmask = self.ds._predicate_mask(q._where)
            keep = np.zeros(row_ids.shape[0], dtype=bool)
            valid = (row_ids >= 0) & (row_ids < pmask.shape[0])
            keep[valid] = pmask[row_ids[valid]]
            self.measured["postfilter_dropped"] = int((~keep).sum())
            row_ids, scores = row_ids[keep], scores[keep]
            self.nodes.append(f"FilterExec: {q._where}   # postfilter=True")

        valid = row_ids >= 0
        row_ids, scores = row_ids[valid], scores[valid]
        # search_ranked 已经按 _score 降序；融合路径也保证了这个顺序。
        # 这里再排一次是为了让 offset 的语义在任何路径下都一致。
        order = np.argsort(-scores, kind="stable")[: k + q._offset]
        row_ids, scores = row_ids[order], scores[order]
        self.nodes.append(f"TopK: limit={k}, offset={q._offset}  (按 _score 降序)")
        row_ids, scores = row_ids[q._offset :][:k], scores[q._offset :][:k]
        return self._assemble(row_ids, scores=scores)

    # ------------------------------------------------------------------
    # 混合检索
    # ------------------------------------------------------------------
    def _run_hybrid(self) -> RecordBatch:
        q = self.q
        if not q._text:
            raise QueryError(
                "hybrid 查询缺少检索词",
                hint="tbl.search(向量, query_type='hybrid').text('关键词')",
            )
        column = self._resolve_vector_column()
        vector = self._query_vector(column)
        k = q._limit if q._limit is not None else DEFAULT_LIMIT
        if k <= 0:
            return _empty_batch(q._output_schema(self.ds.schema))

        projection = q._projection(self.ds.schema)

        # 两路各自取 k 个候选。**注意这里没有过度采样**：SQL 路径
        # （DuckDB 集成）上有一个 oversample_factor 负责"融合前每路多取"，
        # Python 侧没有这个旋钮，本实现也没有实现这一步。
        # 后果是"在 A 路排第 11、在 B 路排第 1"的候选进不了融合池，
        # 第 20 章 §20.9 有这一段的完整说明。
        vq = VectorQuery(self.q._table, vector, vector_column_name=column)
        vq._limit = k
        vq._where = q._where
        vq._prefilter = q._prefilter
        vq._nprobes = q._nprobes
        vq._refine_factor = q._refine_factor
        vq._ef = q._ef
        vq._metric = q._metric
        vp = _SearchPipeline(vq)
        vp.ds = self.ds
        v_dist, v_rows = vp._vector_candidates(column, vector, k)

        fq = FTSQuery(self.q._table, q._text, fts_columns=q._fts_columns)
        fq._limit = k
        fq._where = q._where
        fq._prefilter = q._prefilter
        fq._phrase = q._phrase
        fp = _SearchPipeline(fq)
        fp.ds = self.ds
        f_rows, f_scores = fp._fts_candidates(k)

        self.measured["hybrid_vector"] = dict(vp.measured)
        self.measured["hybrid_fts"] = dict(fp.measured)
        self.nodes.extend(vp.nodes)
        self.nodes.extend(fp.nodes)

        # 把两路结果物化成 list[dict]（重排器要的是行，不是列存批次）
        v_rows_list = self._rows_for_rerank(v_rows, projection, distances=v_dist)
        f_rows_list = self._rows_for_rerank(f_rows, projection, scores=f_scores)

        reranker = q._reranker or RRFReranker()
        fused = reranker.rerank_hybrid(q._text, v_rows_list, f_rows_list, k=k)
        self.measured["leaderboard"] = {
            "reranker": type(reranker).__name__,
            "vector_rows": len(v_rows_list),
            "fts_rows": len(f_rows_list),
            "fused_rows": len(fused),
        }
        self.nodes.append(
            f"RerankExec: {type(reranker).__name__} 融合 "
            f"{len(v_rows_list)} + {len(f_rows_list)} 行 → {len(fused)} 行"
        )

        keep_cols = list(projection)
        if q._with_row_id:
            keep_cols.append(ROWID_COL)
        keep_cols.append(RELEVANCE_COL)
        if getattr(reranker, "return_score", "relevance") == "all":
            keep_cols.extend([DISTANCE_COL, SCORE_COL])
        if not fused:
            return _empty_batch(q._output_schema(self.ds.schema))
        out_rows = [{c: row.get(c) for c in keep_cols} for row in fused]
        out_rows = out_rows[q._offset :]
        return self._rows_to_batch(out_rows, self.ds.schema, sort_by=RELEVANCE_COL)

    def _vector_candidates(
        self, column: str, vector: np.ndarray, k: int
    ) -> Tuple[np.ndarray, np.ndarray]:
        """只做召回，不物化 —— 给 hybrid 用。"""

        q = self.q
        pre_mask = None
        if q._where and q._where != "true" and q._prefilter:
            pre_mask = self.ds._predicate_mask(q._where)
        index = None if q._bypass_index else self._find_vector_index(column)
        metric = q._metric or (
            (index.metadata.distance_type if index is not None else None) or "l2"
        )
        refine = max(1, int(q._refine_factor or 1))
        fetch = k + q._offset
        candidates = fetch if q._prefilter else max(fetch, fetch * refine)
        params: Dict[str, Any] = {}
        if q._nprobes is not None:
            params["nprobes"] = q._nprobes
        if q._ef is not None:
            params["ef"] = q._ef
        if index is not None:
            res = index.search(vector[None, :], candidates, mask=pre_mask, params=params or None)
            self.measured["index"] = dict(res.stats)
            d = np.asarray(res.distances[0], dtype=np.float32)
            r = np.asarray(res.row_ids[0], dtype=np.int64)
        else:
            d, r = self._brute_force(column, vector, candidates, pre_mask, metric)
        if q._where and q._where != "true" and not q._prefilter and r.size:
            pmask = self.ds._predicate_mask(q._where)
            keep = np.zeros(r.shape[0], dtype=bool)
            valid = (r >= 0) & (r < pmask.shape[0])
            keep[valid] = pmask[r[valid]]
            d, r = d[keep], r[keep]
        valid = r >= 0
        d, r = d[valid], r[valid]
        order = np.argsort(d, kind="stable")[:k]
        return d[order], r[order]

    def _fts_candidates(self, k: int) -> Tuple[np.ndarray, np.ndarray]:
        q = self.q
        metas = self._find_fts_indices(q._fts_columns)
        if not metas:
            raise IndexNotFoundError(
                "混合检索需要一条全文索引，但表上一条都没有",
                hint="先建索引：table.create_index('text', config='FTS')",
            )
        pre_mask = None
        if q._where and q._where != "true" and q._prefilter:
            pre_mask = self.ds._predicate_mask(q._where)
        text = f'"{q._fts_query}"' if q._phrase else q._fts_query
        routes = []
        for meta in metas:
            index = self.ds.load_index(meta.name)
            r, s = index.search_ranked(index.to_boolean_query(text), k, mask=pre_mask)
            routes.append((meta, np.asarray(r, dtype=np.int64), np.asarray(s, dtype=np.float64)))
        if len(routes) == 1:
            _, r, s = routes[0]
        else:
            r, s = _rrf_merge_routes(routes)
        if q._where and q._where != "true" and not q._prefilter and r.size:
            pmask = self.ds._predicate_mask(q._where)
            keep = np.zeros(r.shape[0], dtype=bool)
            valid = (r >= 0) & (r < pmask.shape[0])
            keep[valid] = pmask[r[valid]]
            r, s = r[keep], s[keep]
        valid = r >= 0
        r, s = r[valid], s[valid]
        order = np.argsort(-s, kind="stable")[:k]
        return r[order], s[order]

    def _rows_for_rerank(
        self,
        row_ids: np.ndarray,
        projection: Sequence[str],
        *,
        distances: Optional[np.ndarray] = None,
        scores: Optional[np.ndarray] = None,
    ) -> List[Dict[str, Any]]:
        """把一批行号物化成重排器能吃的 ``list[dict]``，并带上 ``_rowid``。

        ``_rowid`` 是必须的：重排器靠它做跨路去重。没有它，
        ``resolve_ids`` 会退回 ``PositionalId``，于是"同一篇文档在两路里
        位置不同"就会被当成两篇 —— 结果里出现重复行，而且顺序也乱。
        """

        if row_ids.size == 0:
            return []
        batch = self.ds.take_rows([int(r) for r in row_ids], columns=list(projection))
        rows = batch.to_pylist()
        for i, row in enumerate(rows):
            row[ROWID_COL] = int(row_ids[i])
            if distances is not None:
                row[DISTANCE_COL] = float(distances[i])
            if scores is not None:
                row[SCORE_COL] = float(scores[i])
        return rows

    # ------------------------------------------------------------------
    # 结果物化 + 整形（三种查询共用的唯一一段）
    # ------------------------------------------------------------------
    def _assemble(
        self,
        row_ids: np.ndarray,
        *,
        distances: Optional[np.ndarray] = None,
        scores: Optional[np.ndarray] = None,
    ) -> RecordBatch:
        schema = self.ds.schema
        q = self.q
        projection = q._projection(schema)
        out_schema = q._output_schema(schema)

        if row_ids.size == 0:
            return _empty_batch(out_schema)

        batch = self.ds.take_rows([int(r) for r in row_ids], columns=projection)
        if distances is not None:
            batch = batch.with_column(
                DISTANCE_COL, PrimitiveArray(FLOAT32, np.asarray(distances, dtype=np.float32))
            )
        if scores is not None:
            batch = batch.with_column(
                SCORE_COL, PrimitiveArray(FLOAT64, np.asarray(scores, dtype=np.float64))
            )
        if q._with_row_id:
            batch = batch.with_column(
                ROWID_COL, PrimitiveArray(UINT64, np.asarray(row_ids, dtype=np.uint64))
            )
        # 只保留用户要的列 + 分数列（顺序也要一致）
        return batch.select([f.name for f in out_schema.fields])

    def _rows_to_batch(
        self, rows: Sequence[Mapping[str, Any]], schema: Schema, *, sort_by: str
    ) -> RecordBatch:
        """``list[dict]`` → ``RecordBatch``，用于重排器输出的重新整形。"""

        if not rows:
            return _empty_batch(self.q._output_schema(schema))
        out_schema = self.q._output_schema(schema)
        names = [f.name for f in out_schema.fields]
        ordered = sorted(
            rows,
            key=lambda r: (
                -(float(r[sort_by]) if r.get(sort_by) is not None else float("-inf"))
            ),
        )
        cols: Dict[str, Array] = {}
        for name in names:
            field = out_schema[name]
            values = [r.get(name) for r in ordered]
            cols[name] = array_from_pylist(field.type, values)
        return RecordBatch(out_schema, cols)

    # ------------------------------------------------------------------
    # 计划渲染
    # ------------------------------------------------------------------
    def render_plan(self, *, analyze: bool, verbose: bool) -> List[str]:
        """渲染计划树。

        **不执行查询**（除非 ``analyze=True``）。要拿到"真实"的计划，
        就必须真的跑一遍 —— 索引有没有被用上、谓词有没有下推，
        只有执行期才知道。所以 ``analyze_plan()`` 会先跑再渲染。
        """

        q = self.q
        if analyze:
            # 先跑一遍把实测数字填进 measured / nodes
            self.run()
        else:
            self._dry_plan()

        schema = self.ds.schema
        out_schema = q._output_schema(schema)
        head = "ProjectionExec: expr=[" + ", ".join(
            f"{f.name}@{i}" for i, f in enumerate(out_schema.fields)
        ) + "]"
        lines = [head]
        indent = "  "
        lines.append(
            f"{indent}LanceRead: uri={self.ds.uri}, version={self.ds.version}, "
            f"num_fragments={len(self.ds.fragments)}, "
            f"projection=[{', '.join(q._projection(schema))}]"
        )
        for node in self.nodes:
            lines.append(f"{indent * 2}{node}")
        for note in self.notes:
            lines.append(f"{indent * 2}! {note}")
        if verbose and self.measured:
            lines.append("")
            # 只用 ASCII：Windows 控制台默认 GBK，打印制表符会直接抛
            # UnicodeEncodeError，把一次正常的 analyze 变成一次崩溃。
            lines.append("--- 实测 I/O ---")
            for key, value in self.measured.items():
                lines.append(f"{key}: {value}")
        return lines

    def _dry_plan(self) -> None:
        """不执行、只按"应该发生什么"推一份计划。"""

        q = self.q
        ds = self.ds
        if q._kind == "scan":
            self.nodes = ["LanceRead: 片段级列存扫描"]
            if q._where and q._where != "true":
                self.nodes.append(f"FilterExec: {q._where}")
                # 有谓词才谈得上"用索引回答谓词"。无谓词时印一行
                # ``query=[?]`` 是纯噪声，还会让人以为索引被用上了。
                for meta in ds.indices:
                    if meta.is_scalar and meta.valid:
                        self.nodes.append(
                            f"ScalarIndexQuery: query=[{q._where}]"
                            f"@{meta.name}({meta.index_type})"
                        )
            self.nodes.append("LateMaterialization: 先算掩码，再取投影列")
            if q._limit is not None:
                self.nodes.append(f"LimitExec: {q._limit}")
            return
        if q._kind == "vector":
            if q._where and q._where != "true":
                self.nodes.append(
                    f"FilterExec: {q._where}   "
                    f"# {'prefilter' if q._prefilter else 'postfilter'}"
                )
            if q._fast_search:
                self.nodes.append("FastSearch: 无索引则返回空结果")
            if q._bypass_index:
                self.nodes.append(
                    "BypassVectorIndex: 强制暴力扫描（结果精确，代价是全表扫）"
                )
            elif any(m.is_vector and m.valid for m in ds.indices):
                for meta in ds.indices:
                    if meta.is_vector and meta.valid:
                        self.nodes.append(f"ANNSubIndex: {meta.name} ({meta.index_type})")
                        self.nodes.append(
                            f"ANNIvfPartition: {meta.name} ({meta.index_type})"
                        )
                        break
            else:
                self.nodes.append("FlatSearch: 无可用向量索引，将暴力扫描")
            self.nodes.append("KNNVectorDistance")
            self.nodes.append(f"TopK: limit={q._limit}")
            self.nodes.append("LateMaterialization: 先定行号，再回表取投影列")
            return
        if q._kind == "fts":
            found = [
                m for m in ds.indices if m.index_type == "FTS" and m.valid
            ]
            for meta in found:
                self.nodes.append(
                    f"ScalarIndexQuery: query=[{q._fts_query}]@{meta.name}(FTS)"
                )
            if not found:
                self.nodes.append("! 表上没有可用的 FTS 索引 → 执行时会报错")
            self.nodes.append("BM25Score: 按 _score 降序")
            return
        # hybrid
        self.nodes.append("VectorSearch: 向量路召回")
        self.nodes.append("FullTextSearch: 全文路召回")
        self.nodes.append(
            f"RerankExec: {type(q._reranker).__name__ if q._reranker else 'RRFReranker'}"
        )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _apply_offset(batch: RecordBatch, offset: int, limit: Optional[int]) -> RecordBatch:
    if offset:
        batch = batch.slice(offset)
    if limit is not None:
        batch = batch.limit(limit)
    return batch


def _rrf_merge_routes(
    routes: Sequence[Tuple[Any, np.ndarray, np.ndarray]], *, k: int = 60
) -> Tuple[np.ndarray, np.ndarray]:
    """把若干条"某一列"的检索结果按**名次**融合成一条。

    多列 FTS 没有"可比的分数"：BM25 的绝对值依赖该列的语料统计，
    两列的 ``_score`` 放在一起比大小是没有意义的（长文本列天然分低）。
    所以只能看名次 —— 这正是 RRF 的适用场景。

    返回的 ``scores`` 是 RRF 分数（``Σ 1/(rank + k)``），
    **方向是"越大越好"**，和 ``_score`` 一致，所以可以直接当
    ``_score`` 用。
    """

    fused: Dict[int, float] = {}
    for _meta, row_ids, _scores in routes:
        for rank, row_id in enumerate(row_ids, start=1):
            rid = int(row_id)
            if rid < 0:
                continue
            fused[rid] = fused.get(rid, 0.0) + 1.0 / (rank + k)
    if not fused:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64)
    items = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
    ids = np.asarray([rid for rid, _ in items], dtype=np.int64)
    scores = np.asarray([s for _, s in items], dtype=np.float64)
    return ids, scores
