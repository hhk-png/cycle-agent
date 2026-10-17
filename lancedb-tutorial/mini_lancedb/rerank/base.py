"""重排器（reranker）的公共地基：分数方向、行表示、结果整形。

**为什么需要重排器。**
一次"混合检索"会同时跑两路召回，而这两路返回的分数量纲完全不同：

* 向量检索返回 ``_distance`` —— **越小越好**。cosine 距离落在 ``[0, 2]``，
  l2 是平方距离、上不封顶，内积是取负的内积、可以是任意实数；
* 全文检索（FTS / BM25）返回 ``_score`` —— **越大越好**。BM25 是无界的
  正数，典型值 0~30，但它的绝对值依赖于**语料统计**（文档长度、词频、
  语料规模），换一批数据就整体平移。

把这两列数字直接加权相加是新手最常见的错误：它等于在一个坐标系里
把"米"和"摄氏度"相加。所以在混合检索里，**重排器的第一件事不是排序，
而是把两路分数放到同一把尺子上**。放尺子的办法有三类，正好对应本包
的三个真实现：

* :class:`~mini_lancedb.rerank.rrf.RRFReranker` —— **只用名次**，不用分数。
  对分数怎么定义完全免疫（"scale-free"），代价是丢掉"第 1 名比第 2 名
  好多少"这个信息；
* :class:`~mini_lancedb.rerank.linear.LinearCombinationReranker` —— **先归一化
  再线性加权**。保留了幅度信息，代价是把"异常值怎么办、缺失值怎么填"
  这两个坑暴露给了使用者；
* :class:`~mini_lancedb.rerank.cross_encoder.CrossEncoderReranker` ——
  **不用召回的分数**，把 ``(query, doc)`` 成对送进模型重新打分。理论上最好，
  代价是 O(N) 次前向计算。

**关于行（row）的表示。**
真实 LanceDB 在 Python 侧用 ``pa.Table``（RecordBatch）在内部传递结果，
Arrow 的列式布局对"按列取分数"很友好。但教学实现里 ``RecordBatch``
的字段访问语法会把"分数方向"这个重点淹没掉，所以我们把内部表示
统一成 **list[dict]**（dict 的顺序稳定、字段可缺省、天生能表达
"这一行没有 FTS 分"）。同时提供 :func:`as_rows` / :func:`rows_to_table`
两个适配器：调用方仍然可以传 ``pa.Table`` / 任何带 ``to_pylist()`` 的对象
/ pandas DataFrame 进来，也可以把结果换回 Arrow 表。

**刻意不做的事：** 不依赖 pandas。pandas 只是"一堆 dict 的另一种写法"，
而 dict 已经够用了；依赖越少，"三行代码跑通一个混合检索"的教程就越好写。
"""

from __future__ import annotations

import abc
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..errors import NotSupportedError, QueryError

__all__ = [
    "Row",
    "DISTANCE_COL",
    "SCORE_COL",
    "RELEVANCE_COL",
    "ROWID_COL",
    "SCORE_SCALES",
    "PositionalId",
    "as_rows",
    "rows_to_table",
    "resolve_ids",
    "sort_rows_by_relevance",
    "Reranker",
    "NoneReranker",
]

#: 一行查询结果的类型别名。**必须**是 Mapping，因为重排器会按字段名取值。
Row = Dict[str, Any]

#: 向量检索的分数列：距离，**越小越好**。
DISTANCE_COL = "_distance"
#: 全文检索的分数列：BM25，**越大越好**。
SCORE_COL = "_score"
#: 重排器输出的统一分数列：相关性，**越大越好**。不同重排器的量纲
#: 不一样（RRF 落在 ``(0, 1/k]``，cross encoder 落在 ``(0, 1)``，
#: linear 落在 ``[0, 1]``），但**方向**永远一致 —— 这是它的全部契约。
RELEVANCE_COL = "_relevance_score"
#: 行标识列，和真实 LanceDB 一致。
ROWID_COL = "_rowid"

#: 三把"尺子"的方向表。混合检索里任何一处方向搞反，结果都是静默错的：
#: 不会报错，只会把最不相关的结果排在第一位。
SCORE_SCALES: Dict[str, Dict[str, Any]] = {
    DISTANCE_COL: {
        "higher_is_better": False,
        "range": "[0, 2]（cosine）/ [0, ∞)（l2 平方距离）/ (-∞, ∞)（内积取负）",
        "produced_by": "向量检索（ANN）",
        "note": "l2 是平方距离、刻意不开方：排序只需要单调",
    },
    SCORE_COL: {
        "higher_is_better": True,
        "range": "[0, ∞)，典型 0~30",
        "produced_by": "全文检索（BM25 / FTS）",
        "note": "绝对值依赖语料统计，换语料就整体平移",
    },
    RELEVANCE_COL: {
        "higher_is_better": True,
        "range": "重排器自定义，但方向统一",
        "produced_by": "本包的重排器",
        "note": "重排器的唯一硬契约：越大越相关",
    },
}


class PositionalId:
    """没有 id 列时用的占位行标识。

    刻意做成一个**独立类型**而不是直接用整数下标：如果直接拿 ``3`` 当 id，
    它可能和真正的 ``_rowid = 3`` 撞在一起，去重时就会静默吞掉一行。
    这类 bug 在测试里很难发现（结果只是少了一行），所以宁可多写 10 行代码。
    """

    __slots__ = ("index",)

    def __init__(self, index: int) -> None:
        self.index = int(index)

    def __repr__(self) -> str:  # pragma: no cover - 只为调试可读
        return f"<pos#{self.index}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PositionalId) and other.index == self.index

    def __hash__(self) -> int:
        # 混入一个字符串前缀，保证 hash 空间和整数 id 尽量不重叠
        return hash(("__mini_lancedb_pos__", self.index))


# ---------------------------------------------------------------------------
# 行 / 表格的适配器
# ---------------------------------------------------------------------------


def as_rows(results: Any) -> List[Row]:
    """把外部结果统一成 ``list[dict]``。

    接受：``None``、单个 dict、``list[dict]``、任何带 ``to_pylist()`` 的对象
    （``pa.Table`` / ``pa.RecordBatch``）、任何带 ``to_dict("records")`` 的对象
    （pandas DataFrame）。返回的是**新的 dict**，不会改到调用方的对象。
    """

    if results is None:
        return []
    if isinstance(results, Mapping):
        return [dict(results)]
    if isinstance(results, (list, tuple)):
        out: List[Row] = []
        for i, item in enumerate(results):
            if not isinstance(item, Mapping):
                raise QueryError(
                    "结果列表的元素必须是 dict（或 Mapping）",
                    index=i,
                    got=type(item).__name__,
                    hint="用 as_rows() 先转换，或传入 pa.Table",
                )
            out.append(dict(item))
        return out

    to_pylist = getattr(results, "to_pylist", None)
    if callable(to_pylist):
        return [dict(r) for r in to_pylist()]

    # pandas DataFrame：to_dict("records") 就是 list[dict]
    to_dict = getattr(results, "to_dict", None)
    if callable(to_dict):
        try:
            return [dict(r) for r in to_dict("records")]
        except (TypeError, ValueError) as exc:  # pragma: no cover - 防御
            raise QueryError(
                "无法把该对象转成 list[dict]",
                got=type(results).__name__,
                reason=str(exc),
            ) from exc

    raise QueryError(
        "不认识的结果类型",
        got=type(results).__name__,
        hint="支持 list[dict] / pa.Table / pa.RecordBatch / pandas.DataFrame",
    )


def rows_to_table(rows: Any) -> Any:
    """把 ``list[dict]`` 换回 ``pa.Table``（给"我要接回真实 LanceDB"的场景用）。

    空列表会被换成"零列零行"的表 —— ``pa.Table.from_pylist([])`` 会因为
    **推不出 schema** 而直接抛错，这是个很多人踩过的坑。
    """

    import pyarrow as pa  # 只有这条路需要 pyarrow

    data = as_rows(rows)
    if not data:
        return pa.table({})
    return pa.Table.from_pylist(data)


def resolve_ids(rows: Sequence[Row], id_column: str = ROWID_COL) -> List[Any]:
    """给每一行取一个**可用于跨列表去重**的标识。

    查找顺序：``id_column`` → ``_rowid`` → ``_id`` → ``id``。
    某个候选列必须**在每一行里都存在且非 None** 才会被采用 ——
    部分行有、部分行没有的情况如果硬用，就会退化成一堆 None 互相去重。

    全都不可用时退回 :class:`PositionalId`。这时**跨列表去重是失效的**
    （同一篇文档在两路召回里的位置 id 不同），这是刻意的：我们宁可让
    结果多几行，也不愿意把两篇不同的文档合并成一行。
    """

    rows = list(rows)
    if not rows:
        return []
    for key in (id_column, ROWID_COL, "_id", "id"):
        if not key:
            continue
        if not all(isinstance(r, Mapping) and key in r and r[key] is not None for r in rows):
            continue
        ids = [r[key] for r in rows]
        try:
            for value in ids:
                hash(value)
        except TypeError:
            # id 是 list/dict 这类不可哈希的东西，没法当 dict 的 key
            continue
        return ids
    return [PositionalId(i) for i in range(len(rows))]


def sort_rows_by_relevance(
    rows: Sequence[Row], *, key: str = RELEVANCE_COL, reverse: bool = True
) -> List[Row]:
    """按分数排序：**稳定排序**，分数相同保持输入顺序。

    稳定性在这里不是洁癖。两路召回的融合结果里，"名次相同"是常态
    （RRF 完全可能给两篇文档同样的分数）。如果排序不稳定，同一个查询
    跑两次的顺序可能不一样，测试就没法写、线上也没法复现问题。

    ``None``（没有分数，例如 cross encoder 只重排了 top-k 之后剩下的尾巴）
    一律排到最后，且它们之间保持输入顺序。
    """

    def sort_key(item: Tuple[int, Row]) -> Tuple[int, float, int]:
        index, row = item
        value = row.get(key)
        if value is None:
            return (1, 0.0, index)
        number = float(value)
        return (0, -number if reverse else number, index)

    return [row for _, row in sorted(enumerate(rows), key=sort_key)]


# ---------------------------------------------------------------------------
# 重排器接口
# ---------------------------------------------------------------------------


class Reranker(abc.ABC):
    """重排器的抽象基类。

    接口形状刻意贴近真实 LanceDB 的 ``lancedb.rerankers.Reranker``：
    ``rerank_vector`` / ``rerank_fts`` / ``rerank_hybrid`` / ``rerank_multivector``
    四个入口 + ``merge_results`` / ``_deduplicate`` 两个工具函数。
    唯一区别是**载荷类型**：真实 LanceDB 传 ``pa.Table``，
    我们传 ``list[dict]``（见模块开头的说明）。

    子类只需要实现 :meth:`rerank_hybrid`（两路融合），
    加上可选的 :meth:`score_rows`（给单个列表打分）。
    """

    def __init__(
        self,
        return_score: str = "relevance",
        *,
        id_column: str = ROWID_COL,
    ) -> None:
        if return_score not in ("relevance", "all"):
            raise QueryError(
                "return_score 只能是 'relevance' 或 'all'",
                value=return_score,
                hint="'relevance' 只留 _relevance_score；'all' 还保留 _distance / _score 便于调试",
            )
        #: ``"relevance"`` 或 ``"all"``。真实 LanceDB 里这个字段叫 ``score``。
        self.score = return_score
        self.return_score = return_score
        #: 去重时用哪一列作为行标识。
        self.id_column = id_column

    def __str__(self) -> str:  # pragma: no cover - 只为打印好看
        return type(self).__name__

    # -- 公共入口 ---------------------------------------------------------
    def rerank(
        self,
        query: Optional[str],
        results: Any,
        *,
        k: Optional[int] = None,
        **kwargs: Any,
    ) -> List[Row]:
        """统一入口，按**输入结构的形状**自动分派。

        * ``results`` 是"一个结果列表"（``list[dict]`` / ``pa.Table``）
          → :meth:`rerank_vector`；
        * ``results`` 是"两个结果列表"（``[向量结果, FTS结果]``）
          → :meth:`rerank_hybrid`；
        * ``results`` 是"N 个结果列表"（N > 2）→ :meth:`rerank_multivector`。

        ``k`` 是**最终保留多少行**，和 :class:`RRFReranker` 的常数 ``k``
        是两个不同的东西（那个是 ``self.k``），这里刻意不做合并 ——
        合并只会让"为什么少了 3 行"这种问题查半天。
        """

        lists = _split_lists(results)
        if not lists:
            return []
        if len(lists) == 1:
            return self.rerank_vector(query, lists[0], k=k, **kwargs)
        if len(lists) == 2:
            return self.rerank_hybrid(query, lists[0], lists[1], k=k, **kwargs)
        return self.rerank_multivector(query, lists, k=k, **kwargs)

    def rerank_vector(
        self, query: Optional[str], vector_results: Any, *, k: Optional[int] = None, **_: Any
    ) -> List[Row]:
        """重排**单路**结果（向量检索，或者任何已经排好序的列表）。

        默认实现 = 调 :meth:`score_rows` 重新打分 + 稳定排序。
        注意很多真实系统里这一步是**可选**的：如果输入本来就是按
        ``_distance`` 升序排好的，而重排器又想保留原有顺序
        （比如 RRF），那"重排"就等于给每一行贴上名次分数。
        """

        rows = as_rows(vector_results)
        return self._finalize(self._score_and_rank(query, rows), k)

    def rerank_fts(
        self, query: Optional[str], fts_results: Any, *, k: Optional[int] = None, **kwargs: Any
    ) -> List[Row]:
        """重排单路 FTS 结果。默认和 :meth:`rerank_vector` 同一条路径 ——
        单列表重排时"这行来自哪一路"已经不重要了，重要的是分数方向。"""

        return self.rerank_vector(query, fts_results, k=k, **kwargs)

    @abc.abstractmethod
    def rerank_hybrid(
        self,
        query: Optional[str],
        vector_results: Any,
        fts_results: Any,
        *,
        k: Optional[int] = None,
        **kwargs: Any,
    ) -> List[Row]:
        """融合"向量路 + FTS 路"两路结果。**这是唯一必须实现的方法。**"""

    def rerank_multivector(
        self,
        query: Optional[str],
        vector_results: Any,
        *,
        k: Optional[int] = None,
        deduplicate: bool = True,
        **kwargs: Any,
    ) -> List[Row]:
        """融合 **N 路**结果（N 路向量、或者多向量查询展开出来的 N 个 token 查询）。

        默认不支持。能在"只有名次、没有可比分数"的前提下融合 N 路的方法
        本质只有 RRF 一类，所以让 :class:`RRFReranker` 自己重写。
        """

        raise NotSupportedError(
            f"{type(self).__name__} 不支持多路融合",
            hint="用 RRFReranker（只需要名次，天然支持任意路数）",
        )

    # -- 子类要重写的钩子 -------------------------------------------------
    def score_rows(self, query: Optional[str], rows: Sequence[Row]) -> List[float]:
        """给一批行打分，**越大越相关**。

        默认不支持：像 RRF 这样的融合器只定义了"多路之间"的合并规则，
        没有定义"单行"的分数；而 cross encoder 这类又只定义了单行分数。
        与其在基类里给一个"看起来能用、其实语义模糊"的默认实现，
        不如显式报错。
        """

        raise NotSupportedError(
            f"{type(self).__name__} 没有实现单列表打分",
            hint="请调用 rerank_hybrid()，它才是融合器的入口",
        )

    # -- 工具 -------------------------------------------------------------
    def merge_results(
        self, vector_results: Any, fts_results: Any, *, deduplicate: bool = True
    ) -> List[Row]:
        """朴素合并：拼接两路结果 + 去掉重复行。

        **保留先出现的那一份**（向量路在前），这就是真实 LanceDB
        ``merge_results`` 的行为，也是它自己的注释里提醒的那件事：
        这个函数不看分数，所以它只适合"不依赖分数大小"的重排器（RRF 就是）。

        ``return_score == "all"`` 时我们还会把两路**同 id 行的互补字段**
        合并进来：向量路的行补上 ``_score``，FTS 路的行补上 ``_distance``，
        否则调试时看到的永远是一堆 None。
        """

        rows = as_rows(vector_results) + as_rows(fts_results)
        if not deduplicate:
            return rows
        return self._deduplicate(rows)

    def _deduplicate(self, rows: Sequence[Row]) -> List[Row]:
        """按 :func:`resolve_ids` 的结果去重，保留第一次出现的那一行。

        同 id 再次出现时，把**对方独有**的字段补进来（这正是"向量命中、
        FTS 也命中"的文档该有的样子：两个分数都在）。
        """

        rows = list(rows)
        if not rows:
            return []
        ids = resolve_ids(rows, self.id_column)
        out: List[Row] = []
        seen: Dict[Any, Row] = {}
        for row_id, row in zip(ids, rows):
            existing = seen.get(row_id)
            if existing is None:
                merged = dict(row)
                seen[row_id] = merged
                out.append(merged)
                continue
            for key, value in row.items():
                if key not in existing or existing[key] is None:
                    existing[key] = value
        return out

    # -- 内部 -------------------------------------------------------------
    def _score_and_rank(
        self, query: Optional[str], rows: Sequence[Row], *, add_score: bool = True
    ) -> List[Row]:
        """调用 :meth:`score_rows`，把结果写到 ``_relevance_score`` 上并排序。"""

        rows = list(rows)
        if not rows:
            return []
        if add_score:
            scores = list(self.score_rows(query, rows))
            if len(scores) != len(rows):  # pragma: no cover - 子类契约保护
                raise QueryError(
                    "score_rows 返回的分数个数和行数不一致",
                    expected=len(rows),
                    got=len(scores),
                )
            rows = [dict(r) for r in rows]
            for row, value in zip(rows, scores):
                row[RELEVANCE_COL] = float(value)
        return sort_rows_by_relevance(rows)

    def _finalize(self, rows: Sequence[Row], k: Optional[int] = None) -> List[Row]:
        """统一收尾：按 ``return_score`` 整形 + 截断到 k 行。"""

        if k is not None:
            k = int(k)
            if k < 0:
                raise QueryError("k 必须是非负整数", k=k)
        out = [dict(r) for r in rows]
        if self.return_score == "relevance":
            for row in out:
                row.pop(DISTANCE_COL, None)
                row.pop(SCORE_COL, None)
        else:
            # "all" 模式下把缺的分数列补成 None：真实 LanceDB 也是这么做的，
            # 否则 from_pylist 会因为 schema 不齐而报错。
            for row in out:
                row.setdefault(DISTANCE_COL, None)
                row.setdefault(SCORE_COL, None)
        return out if k is None else out[:k]


class NoneReranker(Reranker):
    """不做任何重排：只把两路**按原顺序拼起来**、去重、发一个占位分数。

    它存在的意义是给"我不想融合，只想看看两路各自召回了什么"提供一个
    合法的对象（``get_reranker("none")``），而不是让调用方到处写
    ``if reranker is not None``。分数用的是"离原始量纲最近的那个"：
    向量路用 ``1 / (1 + _distance)``，FTS 路用 ``_score`` 本身。
    """

    def score_rows(self, query: Optional[str], rows: Sequence[Row]) -> List[float]:
        out: List[float] = []
        for row in rows:
            if row.get(DISTANCE_COL) is not None:
                out.append(1.0 / (1.0 + float(row[DISTANCE_COL])))
            elif row.get(SCORE_COL) is not None:
                out.append(float(row[SCORE_COL]))
            else:
                out.append(0.0)
        return out

    def rerank_hybrid(
        self,
        query: Optional[str],
        vector_results: Any,
        fts_results: Any,
        *,
        k: Optional[int] = None,
        **_: Any,
    ) -> List[Row]:
        merged = self.merge_results(vector_results, fts_results)
        if not merged:
            return []
        scores = self.score_rows(query, merged)
        for row, value in zip(merged, scores):
            row[RELEVANCE_COL] = float(value)
        # 刻意**不排序**：这个重排器的语义就是"别动我的顺序"
        return self._finalize(merged, k)


def _split_lists(results: Any) -> List[List[Row]]:
    """把 ``rerank()`` 的输入拆成"若干个结果列表"。"""

    if results is None:
        return []
    if isinstance(results, (list, tuple)):
        if len(results) == 0:
            return []
        first = results[0]
        if isinstance(first, Mapping):
            return [as_rows(results)]
        return [as_rows(item) for item in results]
    if isinstance(results, Mapping):
        return [as_rows(results)]
    return [as_rows(results)]
