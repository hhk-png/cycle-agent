"""mini-LanceDB 的向量索引层。

## 索引类型速查：什么时候用哪一个

索引选择不是"哪个准用哪个"，而是**四个资源互相挤压**：内存、建索引时间、
查询延迟、召回率。下面按"你手上最紧的资源"来选：

===========================  ==================================================
索引类型                      选它的理由 / 代价
===========================  ==================================================
``FLAT``                     不解释的基准。n 小于几十万、或者带 filter 后候选
                             很少时，它比任何 ANN 都快。**没有近似误差**。
``IVF_FLAT``                 只砍候选集、不量化。内存和 FLAT 一样大，但查询
                             快了 nprobes/P 倍。是"量化误差有多少"的对照组。
``IVF_SQ``                   4 倍压缩（8 bit/维），精度损失极小。**默认首选**：
                             它把"少读数据"和"几乎不丢精度"同时拿到了。
``IVF_PQ``                   M 字节/向量的高压缩（16~64 倍）。内存装不下时才
                             值得上，因为量化误差比 SQ 大一档；务必配
                             ``refine_factor`` 做精排。
``HNSW``                     不要粗量化、要极高召回率时用。内存最大（图 + 向量），
                             建索引最慢，但 ``ef`` 拉高后召回率能到 99%+，
                             而且**支持真正的增量插入**。
``IVF_HNSW_SQ``              "OSS 生产档"：分区 + 分区内图 + 8 bit 量化。
                             召回率接近纯 HNSW，内存小得多，是多数场景的终点。
``IVF_HNSW_PQ``              数据大到必须极限压缩（如 1 亿 × 768 维）时的选择，
                             用``IVF_HNSW_SQ`` 一半的内存换几个点召回率。
``BQ``                       1 bit/维，内存小 32 倍、距离用 popcount。召回率低
                             （典型 0.5~0.8），**必须**配 ``refine_factor`` 精排。
                             适合"内存红线卡死、且能容忍重排"的场景。
===========================  ==================================================

## 本模块还导出什么

* :data:`VECTOR_INDEX_TYPES` —— 类型名 → 类的注册表；
* :func:`create_vector_index` —— 按类型名分发的工厂（表层的
  ``create_index(..., config=...)`` 最终调的就是它）；
* 量化器 :class:`ScalarQuantizer` / :class:`ProductQuantizer` /
  :class:`BinaryQuantizer` 与图结构 :class:`HnswGraph` —— 它们可以脱离
  索引单独用（教程里就是这么演示"量化到底损失了多少"的）。

## 一条实验方法论

调 ANN 索引永远是"先隔离误差源，再调参"：

1. 用 ``IVF_FLAT`` 量出**粗量化的天花板**（这个 ``nprobes`` 下最多能到多少）；
2. 加上量化器（SQ / PQ）再看掉多少 —— 那是**量化误差**；
3. 最后用 ``refine_factor`` 精排，看能补回多少 —— 补不回来的部分才是
   真正的召回率损失。

跳过第 1 步直接调参，会在两个误差源之间反复横跳。
"""

from __future__ import annotations

from typing import Dict, Type

import numpy as np

from ..errors import NotSupportedError
from .base import (
    INDEX_TYPE_ALIASES,
    DictIndexStore,
    IndexMetadata,
    IndexStore,
    ObjectStoreIndexStore,
    SearchResult,
    VectorIndex,
    normalize_index_type,
    recall_against_bruteforce,
)
from .bq import BITS_PER_WORD, BinaryQuantizer, BqIndex, pack_signs, unpack_signs
from .flat import FlatIndex
from .hnsw import HnswGraph, HnswIndex, build_partition_graphs
from .ivf import IvfBase, IvfIndex
from .ivf_hnsw import IvfHnswPqIndex, IvfHnswSqIndex
from .ivf_pq import IvfPqIndex
from .metric import (
    METRICS,
    Metric,
    get_metric,
    hamming_distance,
    normalize,
    pairwise_distances,
    top_k,
)
from .pq import SUPPORTED_NUM_BITS, ProductQuantizer, kmeans
from .sq import SUPPORTED_SQ_BITS, IvfSqIndex, QuantizationError, ScalarQuantizer

__all__ = [
    # -- 索引类型 ---------------------------------------------------------
    "VectorIndex",
    "IvfBase",
    "FlatIndex",
    "IvfIndex",
    "IvfSqIndex",
    "IvfPqIndex",
    "HnswIndex",
    "IvfHnswSqIndex",
    "IvfHnswPqIndex",
    "BqIndex",
    "BQ",
    "VECTOR_INDEX_TYPES",
    "create_vector_index",
    "vector_index_types",
    # -- 量化器与图 -------------------------------------------------------
    "ScalarQuantizer",
    "ProductQuantizer",
    "BinaryQuantizer",
    "QuantizationError",
    "HnswGraph",
    "build_partition_graphs",
    "kmeans",
    "pack_signs",
    "unpack_signs",
    "BITS_PER_WORD",
    "SUPPORTED_NUM_BITS",
    "SUPPORTED_SQ_BITS",
    # -- 元数据 / 存储 ----------------------------------------------------
    "IndexMetadata",
    "IndexStore",
    "DictIndexStore",
    "ObjectStoreIndexStore",
    "SearchResult",
    "INDEX_TYPE_ALIASES",
    "normalize_index_type",
    # -- 度量与评测 -------------------------------------------------------
    "Metric",
    "METRICS",
    "get_metric",
    "pairwise_distances",
    "hamming_distance",
    "normalize",
    "top_k",
    "recall_against_bruteforce",
]

#: 向量索引类型注册表。键是 ``normalize_index_type`` 规范化之后的名字。
VECTOR_INDEX_TYPES: Dict[str, Type[VectorIndex]] = {
    "FLAT": FlatIndex,
    "IVF_FLAT": IvfIndex,
    "IVF_SQ": IvfSqIndex,
    "IVF_PQ": IvfPqIndex,
    "HNSW": HnswIndex,
    "IVF_HNSW_SQ": IvfHnswSqIndex,
    "IVF_HNSW_PQ": IvfHnswPqIndex,
    "BQ": BqIndex,
}

#: 类型名 ``BQ`` 同时也是它的类名缩写 —— 教程里写 ``BQ.build(...)`` 更顺手。
BQ: Type[BqIndex] = BqIndex

#: 只在本层处理的别名（``base.INDEX_TYPE_ALIASES`` 里没有的）。
_EXTRA_ALIASES = {
    "BINARY_QUANTIZATION": "BQ",
    "BQ_FLAT": "BQ",
    "BINARY": "BQ",
    "IVF_HNSW": "IVF_HNSW_SQ",  # 只说 IVF_HNSW 时按默认量化档处理
    "HNSW_PQ": "IVF_HNSW_PQ",
}


def create_vector_index(
    index_type: str,
    data: np.ndarray,
    row_ids: np.ndarray,
    metadata: IndexMetadata,
) -> VectorIndex:
    """按类型名建索引 —— 表层 API 与内部实现之间唯一的耦合点。

    ``data`` 是 ``(n, d)`` 的 float32 矩阵，``row_ids`` 是每行在表里的行号。
    参数（``num_partitions`` / ``num_sub_vectors`` / ``m`` / ``ef_construction`` …）
    全部从 ``metadata.params`` 里读 —— 因为它们是**索引元数据的一部分**，
    要跟着 manifest 一起持久化，重建索引时必须能原样复现。
    """

    name = normalize_index_type(index_type)
    name = _EXTRA_ALIASES.get(name, name)
    try:
        cls = VECTOR_INDEX_TYPES[name]
    except KeyError:
        raise NotSupportedError(
            f"向量索引类型 {index_type!r}",
            hint=f"支持的类型：{', '.join(sorted(VECTOR_INDEX_TYPES))}",
        ) from None
    return cls.build(data, row_ids, metadata=metadata)


def vector_index_types() -> Dict[str, str]:
    """类型名 → 类名。给 ``index_stats()`` / 文档生成用的小表。"""

    return {name: cls.__name__ for name, cls in sorted(VECTOR_INDEX_TYPES.items())}


# ---------------------------------------------------------------------------
# 标量索引注册表
# ---------------------------------------------------------------------------

from .btree import BTreeIndex  # noqa: E402
from .bitmap import BitmapIndex  # noqa: E402
from .label_list import LabelListIndex  # noqa: E402
from .ngram import NGramIndex  # noqa: E402
from .zonemap import ZoneMapIndex  # noqa: E402
from .inverted import InvertedIndex  # noqa: E402

#: 类型名 → 类。**顺序即"自动选型"的优先级**：
#: 一个字符串列到底该建 FTS 还是 NGRAM 还是 BITMAP，
#: 取决于它会被怎么查（全文？子串？等值？）——
#: 所以上层必须显式指定，这里的顺序只用于"没指定时"的兜底。
SCALAR_INDEX_TYPES: Dict[str, Type[ScalarIndex]] = {
    "BTREE": BTreeIndex,
    "BITMAP": BitmapIndex,
    "LABEL_LIST": LabelListIndex,
    "NGRAM": NGramIndex,
    "ZONEMAP": ZoneMapIndex,
    "FTS": InvertedIndex,
}


def create_scalar_index(
    index_type: str,
    values: Any,
    row_ids: np.ndarray,
    metadata: IndexMetadata,
) -> ScalarIndex:
    """建标量索引。

    和向量索引最大的区别在参数：向量索引要的是 ``(n, d)`` 的浮点矩阵，
    标量索引要的是**一个列式数组**（``Array``）——因为标量索引
    的内部结构（B 树、位图、倒排表）都是围绕"列的取值分布"建的，
    而不是围绕"每一行的值"。
    """

    name = normalize_index_type(index_type)
    name = _EXTRA_ALIASES.get(name, name)
    try:
        cls = SCALAR_INDEX_TYPES[name]
    except KeyError:
        raise NotSupportedError(
            f"标量索引类型 {index_type!r}",
            hint=f"支持的类型：{', '.join(sorted(SCALAR_INDEX_TYPES))}",
        ) from None
    return cls.build(values, row_ids, metadata=metadata)


def load_index(metadata: IndexMetadata, store: IndexStore) -> Any:
    """从磁盘加载索引（向量和标量都走这里）。

    索引的"类型名"写在 manifest 的元数据里，所以加载时先按名字
    找到类，再把文件交给它自己解析 —— **加载逻辑属于索引本身，
    不属于 dataset**。这样新增一种索引类型时，dataset 层一行都不用改。
    """

    if metadata.is_vector:
        cls = VECTOR_INDEX_TYPES.get(metadata.index_type)
        if cls is None:
            raise NotSupportedError(
                f"向量索引类型 {metadata.index_type!r}",
                hint=f"支持的类型：{', '.join(sorted(VECTOR_INDEX_TYPES))}",
            )
        return cls.load(store, metadata)
    cls = SCALAR_INDEX_TYPES.get(metadata.index_type)
    if cls is None:
        raise NotSupportedError(
            f"标量索引类型 {metadata.index_type!r}",
            hint=f"支持的类型：{', '.join(sorted(SCALAR_INDEX_TYPES))}",
        )
    return cls.load(store, metadata)


def scalar_index_types() -> Dict[str, str]:
    return {name: cls.__name__ for name, cls in sorted(SCALAR_INDEX_TYPES.items())}


__all__ += [
    "SCALAR_INDEX_TYPES",
    "create_scalar_index",
    "load_index",
    "scalar_index_types",
    "BTreeIndex",
    "BitmapIndex",
    "LabelListIndex",
    "NGramIndex",
    "ZoneMapIndex",
    "InvertedIndex",
    "ScalarIndex",
]
