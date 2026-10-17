"""索引框架：元数据、存储抽象、向量索引与标量索引的接口。

LanceDB 的索引体系有两棵树，它们的设计目标完全不同：

**向量索引（ANN）** 是"大概率的近似"：它允许漏掉真正最近邻，换取
亚线性的查询时间。所以它的接口里必须显式携带"召回率旋钮"
（``nprobes`` / ``ef`` / ``refine_factor``），而且要能回答
"这个结果是不是精确的"。

**标量索引**是"精确的集合运算"：``WHERE a = 1 AND b > 5`` 不允许有
误差。所以它的接口不是"返回最近的前 k 个"，而是返回一个
**行号集合的表达式**（``ScalarIndexExpr``），由查询规划器再和
其它条件做交并补。

这个差别直接决定了查询规划器的形态：向量索引的输入是"候选集掩码"
（先过滤再搜，即 prefilter），标量索引的输出是"行号集合"。
本模块把这两条路径的接口都定下来。

另外三个必须处理好的工程问题，接口里都留了位置：

* **增量索引（delta index）** —— 表在持续写入，不能每次写都重建索引
  （重建一个亿级 IVF_PQ 要几十分钟）。做法是主索引 + 若干"补丁索引"，
  查询时一起搜、结果合并；
* **压实后重映射（remap）** —— compaction 会把小片段合并成大片段，
  行号全变了。索引要么跟着改行号，要么标记失效；
* **删除后的空洞** —— 删除的行还在索引里，查询时靠 deletion file
  过滤掉（``mask`` 参数）。
"""

from __future__ import annotations

import abc
import json
import time
import uuid
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

import numpy as np

from ..arrays import Array
from ..errors import IndexBuildError, IndexNotFoundError, MiniLanceError, NotSupportedError
from ..storage.base import ObjectStore
from .metric import Metric, get_metric

__all__ = [
    "IndexMetadata",
    "IndexStore",
    "DictIndexStore",
    "ObjectStoreIndexStore",
    "VectorIndex",
    "ScalarIndex",
    "ScalarIndexExpr",
    "ExactMatch",
    "RangeQuery",
    "IsIn",
    "And",
    "Or",
    "Not",
    "SearchResult",
    "DistanceType",
    "INDEX_TYPE_ALIASES",
    "normalize_index_type",
]

#: 真实 LanceDB 里索引类型的正式名字。
#: ``IVF_PQ`` / ``IVF_SQ`` / ``IVF_HNSW_SQ`` / ``IVF_HNSW_PQ`` / ``IVF_FLAT``
#: 是向量索引；``FTS`` / ``BTREE`` / ``BITMAP`` / ``LABEL_LIST`` / ``NGRAM`` /
#: ``ZONEMAP`` 是标量索引。
INDEX_TYPE_ALIASES = {
    "ivf_pq": "IVF_PQ",
    "ivfpq": "IVF_PQ",
    "ivf_sq": "IVF_SQ",
    "ivfsq": "IVF_SQ",
    "ivf_hnsw_sq": "IVF_HNSW_SQ",
    "ivfhnswsq": "IVF_HNSW_SQ",
    "ivf_hnsw_pq": "IVF_HNSW_PQ",
    "ivf_hnsw_flat": "IVF_HNSW_FLAT",
    "ivf_flat": "IVF_FLAT",
    "flat": "FLAT",
    "brute_force": "FLAT",
    "hnsw": "HNSW",
    "fts": "FTS",
    "inverted": "FTS",
    "btree": "BTREE",
    "bitmap": "BITMAP",
    "label_list": "LABEL_LIST",
    "ngram": "NGRAM",
    "zonemap": "ZONEMAP",
    "zone_map": "ZONEMAP",
}


#: 向量索引的类型名集合。和 ``index/__init__.py`` 里的
#: ``VECTOR_INDEX_TYPES`` 一致，但放在这里是为了让 ``IndexMetadata.is_vector``
#: 不必反向导入整个索引包（那会成环）。
VECTOR_INDEX_NAMES = frozenset(
    {
        "FLAT",
        "IVF_FLAT",
        "IVF_SQ",
        "IVF_PQ",
        "IVF_RQ",
        "IVF_HNSW_FLAT",
        "IVF_HNSW_SQ",
        "IVF_HNSW_PQ",
        "HNSW",
        "BQ",
    }
)


def normalize_index_type(name: str) -> str:
    key = str(name).lower().strip().replace("-", "_")
    return INDEX_TYPE_ALIASES.get(key, str(name).upper())


class DistanceType:
    """真实 LanceDB 在索引元数据里用的字段名是 ``distance_type``。"""

    L2 = "l2"
    COSINE = "cosine"
    DOT = "dot"
    HAMMING = "hamming"


# ---------------------------------------------------------------------------
# 索引元数据
# ---------------------------------------------------------------------------


@dataclass
class IndexMetadata:
    """一份索引的自描述信息。

    它被写进表的 manifest（不是写在索引自己的文件里），所以
    "打开一张表"时只读 manifest 就知道有哪些索引、覆盖哪些列、
    参数是什么、索引了多少行 —— 不需要扫描索引文件本身。
    """

    name: str
    index_type: str
    columns: List[str]
    #: 建索引时的数据集版本号
    version: int = 0
    #: 用户传入的参数（num_partitions / num_sub_vectors / m / ef_construction ...）
    params: Dict[str, Any] = dc_field(default_factory=dict)
    #: 索引覆盖的行数（不等于表的总行数 —— 索引建立之后新写入的行不在其中）
    num_indexed_rows: int = 0
    size_bytes: int = 0
    #: 运行时才知道的细节（实际分区数、量化误差、训练耗时……）
    details: Dict[str, Any] = dc_field(default_factory=dict)
    #: 增量索引覆盖的版本号列表，空表示这是一个"全量"索引
    delta_versions: List[int] = dc_field(default_factory=list)
    #: 索引文件在 ``_indices/<name>/`` 下的相对路径
    files: List[str] = dc_field(default_factory=list)
    created_at: float = dc_field(default_factory=time.time)
    #: 索引是否对当前表数据仍然有效（压实/改 schema 之后可能失效）
    valid: bool = True

    def __post_init__(self) -> None:
        self.index_type = normalize_index_type(self.index_type)
        self.columns = list(self.columns)

    # -- 便利属性 ---------------------------------------------------------
    @property
    def distance_type(self) -> Optional[str]:
        return self.params.get("distance_type") or self.details.get("metric_type")

    @property
    def is_delta(self) -> bool:
        return bool(self.delta_versions)

    @property
    def is_vector(self) -> bool:
        """按名字判断这是不是向量索引。

        **不要写成"以 IVF 开头就算向量索引"**：那样会漏掉 ``FLAT``、
        ``HNSW`` 和 ``BQ``，而漏掉的后果很隐蔽 —— 分类错了会走错
        工厂函数，最后报一个"标量索引里没有这个类型"的错误，
        让人完全摸不着头脑。用一个显式集合更安全。
        """

        return self.index_type in VECTOR_INDEX_NAMES

    @property
    def is_scalar(self) -> bool:
        return not self.is_vector

    def covers(self, version: int) -> bool:
        return version == self.version or version in self.delta_versions

    # -- 序列化 -----------------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "index_type": self.index_type,
            "columns": list(self.columns),
            "version": self.version,
            "params": _jsonable(self.params),
            "num_indexed_rows": self.num_indexed_rows,
            "size_bytes": self.size_bytes,
            "details": _jsonable(self.details),
            "delta_versions": list(self.delta_versions),
            "files": list(self.files),
            "created_at": self.created_at,
            "valid": self.valid,
        }

    @staticmethod
    def from_json(obj: Mapping[str, Any]) -> "IndexMetadata":
        return IndexMetadata(
            name=obj["name"],
            index_type=obj["index_type"],
            columns=list(obj.get("columns") or []),
            version=int(obj.get("version", 0)),
            params=dict(obj.get("params") or {}),
            num_indexed_rows=int(obj.get("num_indexed_rows", 0)),
            size_bytes=int(obj.get("size_bytes", 0)),
            details=dict(obj.get("details") or {}),
            delta_versions=[int(v) for v in (obj.get("delta_versions") or [])],
            files=list(obj.get("files") or []),
            created_at=float(obj.get("created_at", time.time())),
            valid=bool(obj.get("valid", True)),
        )

    def describe(self) -> str:
        extra = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return (
            f"{self.name}: {self.index_type}({', '.join(self.columns)}) "
            f"v{self.version} rows={self.num_indexed_rows} "
            f"{self.size_bytes} B{(' ' + extra) if extra else ''}"
        )


# ---------------------------------------------------------------------------
# 索引存储
# ---------------------------------------------------------------------------


class IndexStore(abc.ABC):
    """索引文件读写的抽象。

    真实 LanceDB 把索引存成 ``_indices/<uuid>/`` 下的一堆 ``.lance`` 文件
    （BTREE 是 ``page_data.lance`` + ``page_lookup.lance``；
    FTS 是 ``metadata/docs/invert/tokens`` 四个文件）。
    这里保持"一个索引 = 一个目录 + 若干命名文件"的结构，
    只是文件内容用我们自己的二进制/JSON，而不是真正的 Lance 文件。
    """

    @abc.abstractmethod
    def write(self, name: str, data: bytes) -> None: ...

    @abc.abstractmethod
    def read(self, name: str) -> bytes: ...

    @abc.abstractmethod
    def exists(self, name: str) -> bool: ...

    @abc.abstractmethod
    def list(self) -> List[str]: ...

    # -- 便利方法 ---------------------------------------------------------
    def write_json(self, name: str, obj: Any) -> None:
        self.write(name, json.dumps(_jsonable(obj), ensure_ascii=False).encode("utf-8"))

    def read_json(self, name: str) -> Any:
        return json.loads(self.read(name).decode("utf-8"))

    def write_numpy(self, name: str, arr: np.ndarray) -> None:
        arr = np.ascontiguousarray(arr)
        header = json.dumps(
            {"dtype": str(arr.dtype), "shape": list(arr.shape)}
        ).encode("utf-8")
        self.write(name, len(header).to_bytes(4, "little") + header + arr.tobytes())

    def read_numpy(self, name: str) -> np.ndarray:
        raw = self.read(name)
        hlen = int.from_bytes(raw[:4], "little")
        meta = json.loads(raw[4 : 4 + hlen].decode("utf-8"))
        return np.frombuffer(raw[4 + hlen :], dtype=np.dtype(meta["dtype"])).reshape(
            meta["shape"]
        ).copy()

    def total_bytes(self) -> int:
        return sum(len(self.read(f)) for f in self.list())


class DictIndexStore(IndexStore):
    """内存实现，测试默认用它。"""

    def __init__(self) -> None:
        self._files: Dict[str, bytes] = {}

    def write(self, name: str, data: bytes) -> None:
        self._files[name] = bytes(data)

    def read(self, name: str) -> bytes:
        try:
            return self._files[name]
        except KeyError:
            raise IndexNotFoundError(f"索引文件不存在: {name}") from None

    def exists(self, name: str) -> bool:
        return name in self._files

    def list(self) -> List[str]:
        return sorted(self._files)

    def snapshot(self) -> Dict[str, bytes]:
        return dict(self._files)


class ObjectStoreIndexStore(IndexStore):
    """把索引落到对象存储的 ``prefix`` 下。"""

    def __init__(self, store: ObjectStore, prefix: str) -> None:
        self.store = store
        self.prefix = prefix.rstrip("/")

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}"

    def write(self, name: str, data: bytes) -> None:
        self.store.put(self._key(name), data)

    def read(self, name: str) -> bytes:
        return self.store.get(self._key(name))

    def exists(self, name: str) -> bool:
        return self.store.exists(self._key(name))

    def list(self) -> List[str]:
        base = self.prefix + "/"
        return sorted(m.path[len(base) :] for m in self.store.list(base))


# ---------------------------------------------------------------------------
# 查询结果
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """一次索引查询的原始输出：``(距离, 行号)``，两者形状都是 ``(q, k)``。"""

    distances: np.ndarray
    row_ids: np.ndarray
    #: 距离是精确算出来的还是近似估计的（PQ 的 ADC 距离就是估计值）
    approximate: bool = True
    #: 索引内部统计，用于 explain / analyze
    stats: Dict[str, Any] = dc_field(default_factory=dict)

    @property
    def num_queries(self) -> int:
        return self.distances.shape[0]

    @property
    def k(self) -> int:
        return self.distances.shape[1]

    def top_for(self, qi: int, k: int) -> Tuple[np.ndarray, np.ndarray]:
        return self.distances[qi, :k], self.row_ids[qi, :k]

    def merge(self, other: "SearchResult", k: int) -> "SearchResult":
        """和另一份结果合并去重（主索引 + delta 索引就要这一步）。"""

        q = self.num_queries
        out_d = np.full((q, k), np.inf, dtype=np.float32)
        out_i = np.full((q, k), -1, dtype=np.int64)
        for i in range(q):
            cand: Dict[int, float] = {}
            for d, r in zip(self.distances[i], self.row_ids[i]):
                if r >= 0:
                    cand[int(r)] = min(cand.get(int(r), np.inf), float(d))
            for d, r in zip(other.distances[i], other.row_ids[i]):
                if r >= 0:
                    cand[int(r)] = min(cand.get(int(r), np.inf), float(d))
            items = sorted(cand.items(), key=lambda kv: kv[1])[:k]
            for j, (r, d) in enumerate(items):
                out_i[i, j] = r
                out_d[i, j] = d
        return SearchResult(out_d, out_i, self.approximate or other.approximate)


def empty_result(num_queries: int, k: int) -> SearchResult:
    return SearchResult(
        np.full((num_queries, k), np.inf, dtype=np.float32),
        np.full((num_queries, k), -1, dtype=np.int64),
    )


# ---------------------------------------------------------------------------
# 向量索引接口
# ---------------------------------------------------------------------------


class VectorIndex(abc.ABC):
    """近似最近邻索引的接口。

    生命周期固定为四步，顺序不能变：

    ``build`` → ``search`` … → （可选 ``append`` 做增量） →
    （可选 ``remap`` 做压实） → ``save`` / ``load``

    刻意把 ``build`` 做成类方法而不是构造函数：索引需要先"训练"
    （kmeans 求质心、PQ 求码本），训练失败必须能明确报错，
    而不是构造出一个半成品对象。
    """

    #: 索引类型名，写进 ``IndexMetadata.index_type``
    type_name: str = ""

    def __init__(self, metadata: IndexMetadata, metric: Metric) -> None:
        self.metadata = metadata
        self.metric = metric
        #: 被索引行的行号（不是行内容）。ANN 返回的是这个数组的下标，
        #: 通过它映射回"表的第几行"。
        self.row_ids: np.ndarray = np.zeros(0, dtype=np.uint64)

    # -- 构建 -------------------------------------------------------------
    @classmethod
    @abc.abstractmethod
    def build(
        cls,
        data: np.ndarray,
        row_ids: np.ndarray,
        *,
        metadata: IndexMetadata,
    ) -> "VectorIndex":
        """``data`` 是 ``(n, d)`` 的 float32 矩阵，``row_ids`` 是对应的行号。"""

    # -- 查询 -------------------------------------------------------------
    @abc.abstractmethod
    def search(
        self,
        queries: np.ndarray,
        k: int,
        *,
        mask: Optional[np.ndarray] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> SearchResult:
        """``mask`` 是长度 ``n`` 的布尔数组（True 表示"这一行还在，
        可以被返回"）。删除行过滤、prefilter 语义都走这个参数。"""

    # -- 维护 -------------------------------------------------------------
    def append(self, data: np.ndarray, row_ids: np.ndarray) -> None:
        raise NotSupportedError(
            f"{self.type_name} 不支持增量追加",
            hint="重建索引，或使用支持 delta 的组合索引",
        )

    def remap(self, old_to_new: np.ndarray) -> None:
        """压实之后重映射行号。

        ``old_to_new`` 是长度 = 旧行数的数组，``-1`` 表示这一行被删掉了。
        """

        keep = old_to_new[self.row_ids] >= 0
        self.row_ids = old_to_new[self.row_ids[keep]]
        for attr in ("vectors",):
            if hasattr(self, attr):
                setattr(self, attr, getattr(self, attr)[keep])

    # -- 持久化 -----------------------------------------------------------
    def save(self, store: IndexStore) -> List[str]:
        """写索引文件，返回写出的文件名列表。"""

        raise NotSupportedError(f"{self.type_name} 未实现 save")

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "VectorIndex":
        raise NotSupportedError(f"{metadata.index_type} 未实现 load")

    # -- 观测 -------------------------------------------------------------
    @abc.abstractmethod
    def stats(self) -> Dict[str, Any]:
        """给 ``index_stats()`` 用的信息：类型、分区数、内存占用、召回率线索……"""

    @property
    def size_bytes(self) -> int:
        info = self.stats()
        return int(info.get("size_bytes", 0))

    @property
    def num_indexed_rows(self) -> int:
        return int(len(self.row_ids))

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}({self.num_indexed_rows} rows)"


# ---------------------------------------------------------------------------
# 标量索引接口
# ---------------------------------------------------------------------------


class ScalarIndexExpr:
    """标量索引能回答的"问题形式"。

    真实 Lance 的 ``ScalarIndex`` trait 就是长这样的：
    ``fn search(&self, query: &dyn AnyQuery) -> Result<Option<ScalarIndexExpr>>`` ——
    返回 ``None`` 表示"这个问题我答不了，请退化成全表扫描"。
    """

    pass


@dataclass
class ExactMatch(ScalarIndexExpr):
    """精确命中的行号集合。"""

    row_ids: np.ndarray


@dataclass
class RangeQuery(ScalarIndexExpr):
    """一个闭/开区间。BTREE 索引最擅长这个。"""

    start: Any
    end: Any
    inclusive_start: bool = True
    inclusive_end: bool = True


@dataclass
class IsIn(ScalarIndexExpr):
    """``col IN (...)``。BITMAP 索引最擅长这个。"""

    values: List[Any]


@dataclass
class And(ScalarIndexExpr):
    children: List[ScalarIndexExpr]


@dataclass
class Or(ScalarIndexExpr):
    children: List[ScalarIndexExpr]


@dataclass
class Not(ScalarIndexExpr):
    child: ScalarIndexExpr


class ScalarIndex(abc.ABC):
    """标量索引接口。"""

    type_name: str = ""

    def __init__(self, metadata: IndexMetadata) -> None:
        self.metadata = metadata
        self.row_ids: np.ndarray = np.zeros(0, dtype=np.uint64)

    @classmethod
    @abc.abstractmethod
    def build(
        cls, values: Array, row_ids: np.ndarray, *, metadata: IndexMetadata
    ) -> "ScalarIndex":
        """``values`` 是列式数组（不是 Python 列表）—— 标量索引要按列建。"""

    @abc.abstractmethod
    def search(self, query: ScalarIndexExpr) -> Optional[ScalarIndexExpr]:
        """把查询翻译成"行号集合表达式"；答不了就返回 ``None``。"""

    def resolve(self, expr: ScalarIndexExpr, num_rows: int) -> Optional[np.ndarray]:
        """把 ``ScalarIndexExpr`` 求值成布尔掩码。"""

        if isinstance(expr, ExactMatch):
            m = np.zeros(num_rows, dtype=bool)
            ok = expr.row_ids[(expr.row_ids >= 0) & (expr.row_ids < num_rows)]
            m[ok.astype(np.int64)] = True
            return m
        if isinstance(expr, And):
            parts = [self.resolve(c, num_rows) for c in expr.children]
            if any(p is None for p in parts):
                return None
            out = np.ones(num_rows, dtype=bool)
            for p in parts:
                out &= p  # type: ignore[operator]
            return out
        if isinstance(expr, Or):
            parts = [self.resolve(c, num_rows) for c in expr.children]
            if any(p is None for p in parts):
                return None
            out = np.zeros(num_rows, dtype=bool)
            for p in parts:
                out |= p  # type: ignore[operator]
            return out
        if isinstance(expr, Not):
            inner = self.resolve(expr.child, num_rows)
            return None if inner is None else ~inner
        return None

    def save(self, store: IndexStore) -> List[str]:
        raise NotSupportedError(f"{self.type_name} 未实现 save")

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "ScalarIndex":
        raise NotSupportedError(f"{metadata.index_type} 未实现 load")

    @abc.abstractmethod
    def stats(self) -> Dict[str, Any]:
        ...

    @property
    def size_bytes(self) -> int:
        return int(self.stats().get("size_bytes", 0))

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}({len(self.row_ids)} rows)"


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def require_positive(params: Mapping[str, Any], key: str, default: Optional[int] = None) -> int:
    v = params.get(key, default)
    if v is None:
        raise IndexBuildError(f"缺少必填索引参数 {key}", params=dict(params))
    v = int(v)
    if v <= 0:
        raise IndexBuildError(f"索引参数 {key} 必须为正", value=v)
    return v


def check_divisible(total: int, parts: int, *, what: str = "维度") -> int:
    """PQ 要求维度能被子向量个数整除，否则每个子空间宽度不一，码本没法建。"""

    if parts <= 0 or total % parts != 0:
        raise IndexBuildError(
            f"{what} {total} 不能被 {parts} 整除",
            hint="num_sub_vectors 必须整除向量维度",
        )
    return total // parts


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def recall_against_bruteforce(
    index: VectorIndex, data: np.ndarray, row_ids: np.ndarray, queries: np.ndarray,
    k: int, *, metric_name: str, mask: Optional[np.ndarray] = None,
    params: Optional[Mapping[str, Any]] = None,
) -> float:
    """算召回率 —— 评测索引好不好用只有这一个硬指标。

    做法：拿索引的结果和暴力检索的结果求交集，再除以 k。
    ``recall@k = |ANN_k ∩ BF_k| / k``。
    """

    from .metric import pairwise_distances, top_k

    ann = index.search(queries, k, mask=mask, params=params)
    hits = 0
    total = 0
    for i in range(ann.num_queries):
        got = {int(r) for r in ann.row_ids[i] if r >= 0}
        d = pairwise_distances(data, queries[i : i + 1], metric_name)[0]
        if mask is not None:
            d = np.where(mask, d, np.inf)
        _, truth = top_k(d, k)
        total += len(truth)
        hits += len(got & {int(row_ids[j]) for j in truth})
    return hits / max(1, total)
