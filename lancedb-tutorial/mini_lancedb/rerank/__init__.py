"""重排器包：把多路召回的分数放到同一把尺子上，再决定最终顺序。

四种重排器对应四种工程取舍（详见各模块的模块级文档）：

.. list-table::
   :header-rows: 1

   * - 名字
     - 用什么打分
     - 成本
     - 什么时候选它
   * - ``"rrf"``
     - **只用名次**
     - 几乎为零
     - 默认选择。两路分数不可比、或者有 3 路以上召回时
   * - ``"linear"``
     - 归一化后的分数
     - 几乎为零
     - 你**相信**两路的分数幅度是有意义的
   * - ``"cross_encoder"``
     - ``(query, doc)`` 成对过模型
     - O(N) 次前向
     - 精排 top 20~100，效果上限最高
   * - ``"colbert"``
     - token 级 MaxSim
     - O(|Q|×|D|) + 100 倍存储
     - 长文档 / 多模态，需要"局部对齐"

``"none"`` 不是重排器而是一个**空对象**：它只把两路拼起来去重，
让你不用在调用处写 ``if reranker is not None``。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Type

from ..errors import QueryError
from .base import (
    DISTANCE_COL,
    RELEVANCE_COL,
    ROWID_COL,
    SCORE_COL,
    SCORE_SCALES,
    NoneReranker,
    PositionalId,
    Reranker,
    Row,
    as_rows,
    resolve_ids,
    rows_to_table,
    sort_rows_by_relevance,
)
from .colbert import (
    MULTIVECTOR_STORAGE_SCHEMA,
    ColBERTReranker,
    HashingTokenEmbedder,
    MultivectorQuery,
    maxsim,
    maxsim_matrix,
)
from .cross_encoder import (
    CrossEncoderModel,
    CrossEncoderReranker,
    HashCrossEncoder,
    SentenceTransformersCrossEncoder,
)
from .linear import (
    NORMALIZE_MODES,
    LinearCombinationReranker,
    average_ranks,
    normalize_scores,
)
from .rrf import RRFReranker, rrf_score

__all__ = [
    # 基础设施
    "Reranker",
    "Row",
    "as_rows",
    "rows_to_table",
    "resolve_ids",
    "sort_rows_by_relevance",
    "PositionalId",
    "DISTANCE_COL",
    "SCORE_COL",
    "RELEVANCE_COL",
    "ROWID_COL",
    "SCORE_SCALES",
    # 四个重排器
    "RRFReranker",
    "LinearCombinationReranker",
    "CrossEncoderReranker",
    "ColBERTReranker",
    "NoneReranker",
    # 模型 / 工具
    "rrf_score",
    "normalize_scores",
    "average_ranks",
    "NORMALIZE_MODES",
    "CrossEncoderModel",
    "HashCrossEncoder",
    "SentenceTransformersCrossEncoder",
    "maxsim",
    "maxsim_matrix",
    "HashingTokenEmbedder",
    "MultivectorQuery",
    "MULTIVECTOR_STORAGE_SCHEMA",
    "RERANKERS",
    "get_reranker",
    "reranker_names",
    "describe_rerankers",
]

#: 名字 → 类的注册表。和真实 LanceDB 的用法对齐：
#: ``get_reranker("rrf", k=60)`` 拿到的就是一个可以直接 ``.rerank(...)`` 的对象。
RERANKERS: Dict[str, Type[Reranker]] = {
    "rrf": RRFReranker,
    "linear": LinearCombinationReranker,
    "cross_encoder": CrossEncoderReranker,
    "colbert": ColBERTReranker,
    "none": NoneReranker,
}

#: 别名，让官方文档里的写法也能用。
_ALIASES: Dict[str, str] = {
    "reciprocal_rank_fusion": "rrf",
    "reciprocal-rank-fusion": "rrf",
    "linear_combination": "linear",
    "linear-combination": "linear",
    "cross-encoder": "cross_encoder",
    "crossencoder": "cross_encoder",
    "hash": "cross_encoder",
    "late_interaction": "colbert",
    "late-interaction": "colbert",
    "multivector": "colbert",
    "noop": "none",
    "no_rerank": "none",
    "null": "none",
}


def reranker_names() -> list:
    """给教程用的"有哪些重排器"列表。"""

    return sorted(RERANKERS)


def get_reranker(name: Optional[str] = None, **kwargs: Any) -> Reranker:
    """按名字建一个重排器。

    Parameters
    ----------
    name :
        ``"rrf"`` / ``"linear"`` / ``"cross_encoder"`` / ``"colbert"`` / ``"none"``。
        传 ``None`` 表示"不做重排"，等价于 ``"none"``。
        也可以直接传一个 :class:`Reranker` 实例（原样返回），
        这样调用方不必关心"用户传的是名字还是对象"。
    **kwargs :
        透传给对应重排器的构造参数，例如
        ``get_reranker("rrf", k=60, weights=[1.0, 0.5])``。

    Examples
    --------
    >>> r = get_reranker("rrf")
    >>> rows = [{"_rowid": 1, "_distance": 0.1}, {"_rowid": 2, "_distance": 0.2}]
    >>> [row["_rowid"] for row in r.rerank_vector("q", rows)]
    [1, 2]
    """

    if isinstance(name, Reranker):
        if kwargs:
            raise QueryError("传入 Reranker 实例时不能再给构造参数", kwargs=sorted(kwargs))
        return name
    key = "none" if name is None else str(name).lower().strip().replace(" ", "_")
    key = _ALIASES.get(key, key)
    try:
        cls = RERANKERS[key]
    except KeyError:
        raise QueryError(
            f"不认识的重排器 {name!r}",
            supported=sorted(RERANKERS),
            aliases=sorted(_ALIASES),
        ) from None
    return cls(**kwargs)


def describe_rerankers() -> list:
    """给教程用的对比表（列表形式，方便直接塞进 DataFrame 或 markdown 表）。"""

    return [
        {
            "name": "rrf",
            "分数来源": "名次（rank）",
            "免归一化": True,
            "保留幅度信息": False,
            "额外成本": "≈0",
            "默认参数": "k=60",
            "典型场景": "默认选择；≥3 路召回；分数不可比",
            "失败模式": "无视「第一名领先多少」；两路质量悬殊时需要 weights",
        },
        {
            "name": "linear",
            "分数来源": "归一化后的原始分数",
            "免归一化": False,
            "保留幅度信息": True,
            "额外成本": "≈0",
            "默认参数": "weight=0.7, normalize='minmax', fill=0.0",
            "典型场景": "相信分数幅度；两路召回质量接近",
            "失败模式": "minmax 对离群值敏感；rank 丢幅度；none 要求尺度自洽",
        },
        {
            "name": "cross_encoder",
            "分数来源": "(query, doc) 成对过模型",
            "免归一化": True,
            "保留幅度信息": True,
            "额外成本": "O(N) 次 Transformer 前向",
            "默认参数": "top_k=100",
            "典型场景": "精排 top 20~100，效果上限最高",
            "失败模式": "贵；N 一大就不可用；离线只有玩具模型",
        },
        {
            "name": "colbert",
            "分数来源": "token 级 MaxSim",
            "免归一化": True,
            "保留幅度信息": True,
            "额外成本": "O(|Q|×|D|) 计算 + ~100 倍存储",
            "默认参数": "top_k=100",
            "典型场景": "长文档、多模态（ColPali）、需要局部对齐",
            "失败模式": "回表 IO 大；查询侧展开的 max 只是近似 max",
        },
        {
            "name": "none",
            "分数来源": "不重排，只拼接去重",
            "免归一化": True,
            "保留幅度信息": True,
            "额外成本": "≈0",
            "默认参数": "-",
            "典型场景": "调试「两路各自召回了什么」",
            "失败模式": "没有融合，顺序完全取决于拼接顺序",
        },
    ]
