"""mini-LanceDB —— 一个从零实现的教学版 LanceDB。

**这个库的取舍，一句话说清**：真实 LanceDB 的每一行代码都在
"省一次 I/O" 和 "省一次计算" 之间做选择；本实现保留了这些选择的
**形状**（同样的分层、同样的文件格式概念、同样的 API），
但允许自己在常数因子上慢一点、在内存上奢侈一点，
换来"每一个决策都能在一屏代码里看完"。

所以：**不要拿它当生产依赖**。要拿它当"把 LanceDB 拆开看"的显微镜。

各公开名字对应的教程章节
========================

.. list-table::
   :header-rows: 1

   * - 名字
     - 章节
     - 它回答什么问题
   * - :func:`connect` / :class:`LanceDBConnection`
     - 第 2 章《上手：一个最小的完整流程》
     - 一个 uri 上的多张表；为什么"目录即表"
   * - :class:`LanceTable` / ``Table``
     - 第 2、9 章
     - 用户 API 的形状：写入、查询、演进、维护
   * - :class:`~mini_lancedb.query.Query` /
       :class:`~mini_lancedb.query.VectorQuery` /
       :class:`~mini_lancedb.query.FTSQuery` /
       :class:`~mini_lancedb.query.HybridQuery`
     - 第 19、20 章
     - prefilter / postfilter、``_distance`` / ``_score`` 的方向
   * - :class:`~mini_lancedb.scanner.Scanner`
     - 第 10、19 章
     - 列投影、zone map、延迟物化，以及它们各自省下了什么
   * - :class:`~mini_lancedb.dataset.Dataset` / :class:`WriteParams`
     - 第 8、9 章
     - manifest、事务、时间旅行，以及删除为什么只是改元数据
   * - :class:`~mini_lancedb.fragment.Fragment` / :class:`DataFile`
     - 第 7 章
     - 片段、数据文件，以及稳定行地址
   * - :class:`~mini_lancedb.optimize.OptimizeStats`
     - 第 23 章
     - 什么时候该 optimize、它的代价在哪
   * - :class:`~mini_lancedb.stats.TableStatistics`
     - 第 23、24 章
     - "这张表健康吗"的体检报告
   * - :func:`tokenize` / ``mini_lancedb.fts``
     - 第 17 章
     - 分词是写入和查询共享的契约
   * - ``mini_lancedb.rerank``
     - 第 20、21 章
     - 两路分数的量纲问题，以及 RRF 为什么只需要名次
   * - ``mini_lancedb.embedding``
     - 第 22 章
     - 表的自描述：向量是哪来的
   * - :func:`func` / :class:`EmbeddingFunctionRegistration`
     - 第 22 章
     - 为什么推荐在 schema 里声明而不是传一堆函数
   * - :class:`~mini_lancedb.namespace.NamespaceClient`
     - 第 25、28 章
     - 多租户；命名空间和"目录即表"是两层
   * - :class:`~mini_lancedb.permissions.AccessController`
     - 第 25 章
     - 为什么客户端权限检查不构成安全边界
   * - ``mini_lancedb.storage``
     - 第 6 章
     - 七种方法撑起一个存储抽象；条件写是唯一的原子原语
   * - :class:`~mini_lancedb.storage.base.InstrumentedStore`
     - 第 6、10 章
     - 所有性能数字的可复现来源
   * - ``mini_lancedb.index``
     - 第 12–18 章
     - 向量索引与标量索引的公共接口、注册表与度量
   * - ``mini_lancedb.errors``
     - 附录 A、附录 D
     - 一个根异常 + 按出错阶段分层

快速上手
========

.. code-block:: python

    import mini_lancedb as mldb

    db = mldb.connect("memory://")
    tbl = db.create_table("docs", [
        {"id": 1, "text": "向量检索", "vector": [1.0, 0.0]},
        {"id": 2, "text": "全文检索", "vector": [0.0, 1.0]},
    ])
    print(tbl)
    print(tbl.search([1.0, 0.0]).limit(1).to_list())
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# 连接 / 表
# ---------------------------------------------------------------------------
from .connection import (
    EmbeddingFunctionRegistration,
    LanceDBConnection,
    SourceField,
    VectorField,
    connect,
    func,
)
from .table import (
    AddColumnsResult,
    AddResult,
    AlterColumnsResult,
    DropColumnsResult,
    LanceTable,
    Table,
    TableTags,
)

# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
from .query import (
    DEFAULT_LIMIT,
    FTSQuery,
    HybridQuery,
    Query,
    VectorQuery,
    create_query,
)
from .scanner import Scanner, ScanStats

# ---------------------------------------------------------------------------
# 数据集 / 写入
# ---------------------------------------------------------------------------
from .dataset import (
    DeleteResult,
    Dataset,
    MergeInsertBuilder,
    MergeResult,
    TagInfo,
    UpdateResult,
    VersionInfo,
    WriteParams,
    create_dataset,
    open_dataset,
)

# ---------------------------------------------------------------------------
# 维护 / 统计
# ---------------------------------------------------------------------------
from .optimize import (
    OPTIMIZE_OPERATION_THRESHOLD,
    OPTIMIZE_ROW_CHANGE_THRESHOLD,
    CleanupStats,
    CompactionStats,
    OptimizeStats,
    cleanup_old_versions,
    compact_files,
    optimize,
    should_optimize,
)
from .stats import (
    FragmentStats,
    TableStatistics,
    collect_table_statistics,
    index_stats,
)

# ---------------------------------------------------------------------------
# 命名空间 / 权限
# ---------------------------------------------------------------------------
from .namespace import Namespace, NamespaceClient, NamespaceNotFoundError
from .permissions import (
    AccessController,
    Permission,
    PermissionDenied,
    PermissionStore,
    TablePermissions,
    with_tables_filtered,
)

# ---------------------------------------------------------------------------
# schema / 类型
# ---------------------------------------------------------------------------
from .schema import Field, Schema, merge_schemas
from .types import (
    BINARY,
    BOOL,
    DATE32,
    DATE64,
    FLOAT16,
    FLOAT32,
    FLOAT64,
    INT8,
    INT16,
    INT32,
    INT64,
    UINT8,
    UINT16,
    UINT32,
    UINT64,
    UTF8,
    DataType,
    FixedSizeList,
    List,
    Struct,
    Timestamp,
    infer_schema,
    parse_type,
)

# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------
from .index.base import (
    DistanceType,
    IndexMetadata,
    ScalarIndex,
    SearchResult,
    VectorIndex,
    empty_result,
    recall_against_bruteforce,
)

# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
from .storage.base import (
    InMemoryStore,
    InstrumentedStore,
    LocalFileStore,
    ObjectStore,
    S3LikeStore,
)

# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------
from .errors import (
    CommitConflict,
    CorruptedFileError,
    DataValidationError,
    ExpressionSyntaxError,
    IndexBuildError,
    IndexErrorBase,
    IndexNotFoundError,
    MiniLanceError,
    NotSupportedError,
    PlanError,
    QueryError,
    SchemaError,
    StoreError,
    TableAlreadyExistsError,
    TableNotFoundError,
    TagNotFoundError,
    TypeMismatchError,
    VersionNotFoundError,
    format_error_chain,
)

# ---------------------------------------------------------------------------
# 向量化 / 重排注册表
# ---------------------------------------------------------------------------
from .embedding import (
    EmbeddingFunction,
    EmbeddingFunctionConfig,
    EmbeddingFunctionRegistry,
    TextEmbeddingFunction,
    get_registry,
    register_embedding_function,
    registered_embedding_functions,
)
from .rerank import (
    ColBERTReranker,
    CrossEncoderReranker,
    LinearCombinationReranker,
    NoneReranker,
    RRFReranker,
    Reranker,
    describe_rerankers,
    get_reranker,
    reranker_names,
)


def tokenize(text: str, **kwargs: Any) -> list:
    """分词 —— 对齐真实 ``lancedb.tokenize``。

    它**委托给** :func:`mini_lancedb.fts.tokenize`，而不是重新实现一遍。
    理由不是省代码，是**一致性**：分词管线同时被写入路径（建索引）和
    查询路径（检索）使用，两边的配置一旦分叉，就会出现
    "索引建好了、查得动、就是查不出来"这种最难排查的 bug。
    让它们共用同一个函数，是在代码结构上把这条契约写死。

    和真实 LanceDB 的差异：真实的 ``tokenize`` 返回 ``FtsToken`` 对象流，
    并且有一整套 ``base_tokenizer`` / ``language`` / ``stem`` 选项
    （对应 Tantivy 的 tokenizer 配置）。本实现返回
    :class:`~mini_lancedb.fts.tokenizer.Token` 列表，
    配置通过 ``config=TokenizerConfig(...)`` 传。

    Args:
        text: 待分词的文本。
        **kwargs: 透传给 :class:`~mini_lancedb.fts.TokenizerConfig`
            的字段（如 ``lower_case=False`` / ``stem=False``）。

    Returns:
        ``list[Token]``，每个 ``Token`` 带 ``text`` / ``position`` /
        ``start`` / ``end``（字节偏移）。
    """

    from .fts import TokenizerConfig
    from .fts import tokenize as _tokenize

    config = TokenizerConfig(**kwargs) if kwargs else None
    return _tokenize(text, config)


__all__ = [
    "__version__",
    # 连接 / 表
    "connect",
    "LanceDBConnection",
    "LanceTable",
    "Table",
    "TableTags",
    "AddResult",
    "AddColumnsResult",
    "AlterColumnsResult",
    "DropColumnsResult",
    "func",
    "SourceField",
    "VectorField",
    "EmbeddingFunctionRegistration",
    # 查询
    "Query",
    "VectorQuery",
    "FTSQuery",
    "HybridQuery",
    "create_query",
    "DEFAULT_LIMIT",
    "Scanner",
    "ScanStats",
    # 数据集 / 写入
    "Dataset",
    "WriteParams",
    "MergeResult",
    "MergeInsertBuilder",
    "DeleteResult",
    "UpdateResult",
    "VersionInfo",
    "TagInfo",
    "open_dataset",
    "create_dataset",
    # 维护 / 统计
    "OptimizeStats",
    "CompactionStats",
    "CleanupStats",
    "optimize",
    "compact_files",
    "cleanup_old_versions",
    "should_optimize",
    "OPTIMIZE_ROW_CHANGE_THRESHOLD",
    "OPTIMIZE_OPERATION_THRESHOLD",
    "TableStatistics",
    "FragmentStats",
    "collect_table_statistics",
    "index_stats",
    # 命名空间 / 权限
    "Namespace",
    "NamespaceClient",
    "NamespaceNotFoundError",
    "Permission",
    "PermissionDenied",
    "PermissionStore",
    "TablePermissions",
    "AccessController",
    "with_tables_filtered",
    # schema / 类型
    "Schema",
    "Field",
    "merge_schemas",
    "DataType",
    "FixedSizeList",
    "List",
    "Struct",
    "Timestamp",
    "UTF8",
    "BOOL",
    "INT8",
    "INT16",
    "INT32",
    "INT64",
    "UINT8",
    "UINT16",
    "UINT32",
    "UINT64",
    "FLOAT16",
    "FLOAT32",
    "FLOAT64",
    "BINARY",
    "DATE32",
    "DATE64",
    "infer_schema",
    "parse_type",
    # 索引
    "IndexMetadata",
    "DistanceType",
    "VectorIndex",
    "ScalarIndex",
    "SearchResult",
    "empty_result",
    "recall_against_bruteforce",
    # 存储
    "ObjectStore",
    "InMemoryStore",
    "LocalFileStore",
    "S3LikeStore",
    "InstrumentedStore",
    # 错误
    "MiniLanceError",
    "SchemaError",
    "TypeMismatchError",
    "DataValidationError",
    "QueryError",
    "PlanError",
    "ExpressionSyntaxError",
    "IndexErrorBase",
    "IndexNotFoundError",
    "IndexBuildError",
    "TableNotFoundError",
    "TableAlreadyExistsError",
    "VersionNotFoundError",
    "TagNotFoundError",
    "CommitConflict",
    "StoreError",
    "CorruptedFileError",
    "NotSupportedError",
    "format_error_chain",
    # 向量化 / 重排
    "EmbeddingFunction",
    "EmbeddingFunctionConfig",
    "EmbeddingFunctionRegistry",
    "TextEmbeddingFunction",
    "get_registry",
    "register_embedding_function",
    "registered_embedding_functions",
    "Reranker",
    "RRFReranker",
    "LinearCombinationReranker",
    "CrossEncoderReranker",
    "ColBERTReranker",
    "NoneReranker",
    "get_reranker",
    "reranker_names",
    "describe_rerankers",
    # 工具
    "tokenize",
]
