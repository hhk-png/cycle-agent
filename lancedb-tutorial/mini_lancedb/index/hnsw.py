"""HNSW（Hierarchical Navigable Small World）—— 分层可导航小世界图。

HNSW 是 2016 年提出、至今仍是"内存图索引"的事实标准。它的想法可以
一句话说清：**把跳表的思想搬到近邻图上**。

* 每个点被随机分配一个"最高层" ``level``，分配方式是 ``floor(-ln(U) · ml)``，
  ``ml = 1/ln(M)``。这是一个几何分布，所以第 ``l`` 层的点数期望是
  ``n · M^-l`` —— 层数越高点越少，形成"上疏下密"的结构；
* 查询从最高层的入口点开始：**贪心**地往离查询更近的邻居走，走到
  局部最优就下降一层，重复；
* 在最底层用 ``ef`` 大小的候选堆做**束搜索**，返回最近的 k 个。

``ml = 1/ln(M)`` 这个常数不是拍脑袋的：它让每层的点数期望正好是上一层
的 ``1/M``，而每层每个点最多连 ``M`` 条边 —— 两件事乘起来，一层的搜索
代价与点数无关，于是层数变成 ``O(log n)``，总查询复杂度 ``O(log n)``。

工程上真正决定召回率的是两个参数，方向恰好相反：

* ``ef_construction``：建图时每个新点要找多少个候选邻居。它决定图的
  **连通质量**，只影响构建时间和索引大小；
* ``ef``：查询时的候选堆大小。它必须 ≥ k，越大越准越慢。**这是查询期
  唯一的精度旋钮**，和 IVF 的 ``nprobes`` 地位相同。

还有一个容易被忽略的细节是**邻居选择启发式**（论文 Algorithm 4）：
不能简单地"连最近的 M 个"，否则所有点都会连到同一个中心，图会退化成一
颗星。启发式要求新邻居 c 在"到已选邻居的距离"上不能比"到查询点的距离"
更近，也就是只保留那些**方向上有差异**的邻居。这一步叫 diversity pruning，
是 HNSW 在聚簇数据上比朴素 k-NN 图好得多的主要原因。

本模块同时提供两个层次的东西：:class:`HnswGraph` 是可以脱离索引单独使用
的图结构（可整库建，也可在 IVF 的每个分区内部建 —— 后者就是
``IVF_HNSW_*``），:class:`HnswIndex` 是把它包装成 ``VectorIndex`` 的适配器。
"""

from __future__ import annotations

import heapq
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ..errors import IndexBuildError
from ._common import (
    DistanceKernel,
    as_mask,
    as_matrix,
    as_row_ids,
    check_k,
    check_queries,
    prepare_matrix,
    resolve_metric,
    save_arrays,
    total_nbytes,
)
from .base import (
    IndexMetadata,
    IndexStore,
    SearchResult,
    VectorIndex,
    require_positive,
)
from .metric import Metric, get_metric

__all__ = ["HnswGraph", "HnswIndex", "build_partition_graphs"]


def _empty_links() -> np.ndarray:
    return np.zeros(0, dtype=np.int32)


class HnswGraph:
    """一张 HNSW 图。点用 **0..n-1 的局部下标**寻址。

    局部下标这个设计是为了让同一份代码既能给整库建图，也能给 IVF 的
    单个分区建图（分区里的点重新编号成 0..m-1，用
    ``partition_members[local_id]`` 映射回全局位置）。
    """

    def __init__(
        self,
        *,
        dim: int = 0,
        m: int = 16,
        ef_construction: int = 100,
        ef: int = 50,
        metric: Any = "l2",
        seed: int = 0,
    ) -> None:
        metric_obj = metric if isinstance(metric, Metric) else get_metric(str(metric))
        self.metric: Metric = metric_obj
        self.dim: int = int(dim)
        #: 每个点在 1 层及以上最多连的边数
        self.m: int = max(2, int(m))
        self.ef_construction: int = max(self.m, int(ef_construction))
        self.ef: int = max(1, int(ef))
        #: ``ml = 1/ln(M)``：层高随机分布的尺度参数
        self.ml: float = 1.0 / math.log(self.m)
        #: 第 0 层允许连更多边（论文用 2M），因为最底层承担了几乎全部搜索
        self.m_max: int = self.m
        self.m_max0: int = 2 * self.m
        self.entry_point: int = -1
        self.max_level: int = -1
        self.levels: np.ndarray = np.zeros(0, dtype=np.int32)
        self.links: List[List[np.ndarray]] = []
        self.num_nodes: int = 0
        self.vectors: np.ndarray = np.zeros((0, max(1, self.dim)), dtype=np.float32)
        self.kernel: DistanceKernel = DistanceKernel(self.vectors, metric_obj)
        self.rng: np.random.Generator = np.random.default_rng(int(seed))
        #: 建图统计
        self.distance_evaluations: int = 0
        self.insertions: int = 0

    # -- 层高 -------------------------------------------------------------
    def _random_level(self) -> int:
        """``floor(-ln(U) · ml)``。用指数分布抽样，直接得到几何分布。"""

        u = float(self.rng.random())
        return int(-math.log(max(u, 1e-12)) * self.ml)

    # -- 距离 -------------------------------------------------------------
    def _dist(self, query: np.ndarray, ids: Any) -> np.ndarray:
        """``query`` **必须已经过 kernel 预处理**（建图时取的就是库里的行）。"""

        d = self.kernel.to_positions(query, np.asarray(ids, dtype=np.int64))
        self.distance_evaluations += int(np.size(d))
        return d

    # -- 建图 -------------------------------------------------------------
    def build(self, data: np.ndarray) -> "HnswGraph":
        """逐点插入，构出整张图。``O(n · M · ef_construction)`` 次距离计算。"""

        x = prepare_matrix(as_matrix(data, what="HNSW 数据"), self.metric)
        self.vectors = x
        self.dim = int(x.shape[1])
        self.kernel = DistanceKernel(x, self.metric)
        n = int(x.shape[0])
        self.num_nodes = n
        self.levels = np.array([self._random_level() for _ in range(n)], dtype=np.int32)
        top = int(self.levels.max()) if n else 0
        self.links = [[_empty_links() for _ in range(n)] for _ in range(top + 1)]
        self.entry_point = -1
        self.max_level = -1
        for i in range(n):
            self._insert(i)
        return self

    def append(self, data: np.ndarray) -> None:
        """增量插入 —— HNSW 的"图结构天然支持逐点写入"是它对 IVF 的优势。

        注意新点的局部下标被追加在末尾，所以调用方必须保证"同一分区内的
        新点排在旧点之后"（:meth:`IvfBase._rebuild_csr` 的 stable 排序保证了
        这一点）。
        """

        x = prepare_matrix(as_matrix(data, what="HNSW 追加数据"), self.metric)
        if x.shape[1] != self.dim:
            raise IndexBuildError(
                "追加数据的维度与图不一致",
                appended_dim=int(x.shape[1]),
                graph_dim=self.dim,
            )
        if self.num_nodes == 0:
            self.build(x)
            return
        start = self.num_nodes
        self.vectors = np.concatenate([self.vectors, x], axis=0)
        self.kernel.set_matrix(self.vectors)
        self.num_nodes += x.shape[0]
        new_levels = np.array(
            [self._random_level() for _ in range(x.shape[0])], dtype=np.int32
        )
        self.levels = np.concatenate([self.levels, new_levels])
        top = int(self.levels.max())
        while len(self.links) <= top:
            self.links.append([_empty_links() for _ in range(self.num_nodes)])
        for lvl in range(len(self.links)):
            gap = self.num_nodes - len(self.links[lvl])
            if gap > 0:
                self.links[lvl].extend([_empty_links() for _ in range(gap)])
        for j in range(start, self.num_nodes):
            self._insert(j)

    def _ensure_level(self, level: int) -> None:
        while len(self.links) <= level:
            self.links.append([_empty_links() for _ in range(self.num_nodes)])

    def _insert(self, node: int) -> None:
        level = int(self.levels[node])
        q = self.vectors[node]
        if self.entry_point < 0:
            self._ensure_level(level)
            self.entry_point = node
            self.max_level = level
            self.insertions += 1
            return
        ep = self.entry_point
        # 阶段 1：在高出目标层的部分做"每层只走一步"的贪心下降
        for lc in range(self.max_level, level, -1):
            ep = self._greedy(q, ep, lc)
        # 阶段 2：从 min(level, max_level) 一路往下，每层做一次束搜索建边
        eps = [ep]
        for lc in range(min(level, self.max_level), -1, -1):
            W = self._search_layer(q, eps, self.ef_construction, lc)
            limit = self.m_max0 if lc == 0 else self.m_max
            picked = self._select_neighbors(W, limit)
            self.links[lc][node] = np.asarray(picked, dtype=np.int32)
            for nb in picked:
                self._add_link(lc, int(nb), node, limit)
            if W:
                eps = [w[1] for w in W]
        if level > self.max_level:
            self._ensure_level(level)
            self.entry_point = node
            self.max_level = level
        self.insertions += 1

    def _greedy(self, q: np.ndarray, ep: int, level: int) -> int:
        """第 ``level`` 层上的贪心下降：只要邻居里有更近的就走。"""

        cur = int(ep)
        cur_d = float(self._dist(q, [cur])[0])
        improved = True
        while improved:
            improved = False
            neigh = self.links[level][cur]
            if neigh.size == 0:
                break
            d = self._dist(q, neigh)
            j = int(np.argmin(d))
            if float(d[j]) < cur_d:
                cur = int(neigh[j])
                cur_d = float(d[j])
                improved = True
        return cur

    def _search_layer(
        self,
        q: np.ndarray,
        entry_points: Sequence[int],
        ef: int,
        level: int,
        keep: Optional[np.ndarray] = None,
    ) -> List[Tuple[float, int]]:
        """第 ``level`` 层的束搜索，返回按距离升序的 ``[(dist, id), ...]``。

        ``keep`` 是"这一行还能不能被返回"的谓词（删除行 / prefilter）。
        关键设计：**被过滤的点照样参与图的遍历**，只是不进结果堆。
        如果连遍历都跳过它们，图会被切断，召回率会崩掉 —— 这是
        "图索引上的过滤"和"倒排索引上的过滤"最大的实现差异。
        """

        ep = np.asarray(sorted({int(e) for e in entry_points}), dtype=np.int64)
        visited = set(int(e) for e in ep)
        d0 = self._dist(q, ep)
        # candidates：小顶堆（越近越先扩展）；results：大顶堆（存负距离）
        candidates: List[Tuple[float, int]] = []
        results: List[Tuple[float, int]] = []
        for dd, ii in zip(d0, ep):
            node = int(ii)
            heapq.heappush(candidates, (float(dd), node))
            if keep is None or bool(keep[node]):
                heapq.heappush(results, (-float(dd), node))
        while candidates:
            cd, ci = heapq.heappop(candidates)
            if len(results) >= ef and cd > -results[0][0]:
                break  # 最近的候选都比结果堆里最差的还远 → 不可能再有更好的
            neigh = self.links[level][ci]
            if neigh.size == 0:
                continue
            fresh = [int(j) for j in neigh if int(j) not in visited]
            if not fresh:
                continue
            visited.update(fresh)
            dd = self._dist(q, np.asarray(fresh, dtype=np.int64))
            worst = -results[0][0] if results else float("inf")
            for j, dj in zip(fresh, dd):
                dj = float(dj)
                if len(results) < ef or dj < worst:
                    heapq.heappush(candidates, (dj, j))
                    if keep is None or bool(keep[j]):
                        heapq.heappush(results, (-dj, j))
                        if len(results) > ef:
                            heapq.heappop(results)
                        worst = -results[0][0] if results else float("inf")
        return sorted((-d, i) for d, i in results)

    # -- 邻居选择启发式 ---------------------------------------------------
    def _select_neighbors(
        self, W: Sequence[Tuple[float, int]], limit: int
    ) -> List[int]:
        """论文 Algorithm 4：多样性剪枝。

        对候选按距离升序处理，只有当"候选 c 到每个已选邻居的距离都 ≥ c 到
        查询点的距离"时才保留 c。直觉：如果某个已选邻居 w 比查询点还靠近 c，
        那么走 c → w 就能到查询点附近，c 这条边是冗余的；而如果 c 在别的
        方向上，它就是一条有价值的"长边"，能避免搜索掉进局部最优。

        这里用 ``kernel.pairwise`` 一次算出候选两两距离 —— 否则每个候选
        都要发一次小距离调用，在 Python 里常数会爆炸。
        """

        if len(W) <= limit:
            return [i for _, i in W]
        ids = np.asarray([i for _, i in W], dtype=np.int64)
        dists = np.asarray([d for d, _ in W], dtype=np.float32)
        pair = self.kernel.pairwise(ids)
        selected: List[int] = []
        for a in range(ids.shape[0]):
            if len(selected) >= limit:
                break
            ok = True
            for b in selected:
                if pair[a, b] < dists[a]:
                    ok = False
                    break
            if ok:
                selected.append(a)
        return [int(ids[a]) for a in selected]

    def _add_link(self, level: int, src: int, dst: int, limit: int) -> None:
        """加一条 ``src → dst`` 的边；超限时对 ``src`` 的邻居表重新剪枝。"""

        cur = self.links[level][src]
        cand = np.append(cur, np.int32(dst))
        if cand.size > limit:
            d = self._dist(self.vectors[src], cand)
            order = np.argsort(d, kind="stable")
            W = [(float(d[o]), int(cand[o])) for o in order]
            cand = np.asarray(self._select_neighbors(W, limit), dtype=np.int32)
        self.links[level][src] = cand

    # -- 查询 -------------------------------------------------------------
    def search(
        self,
        query: Any,
        k: int,
        *,
        ef: Optional[int] = None,
        keep: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """返回 ``(局部下标, 距离)``，长度 ≤ k，按距离升序。"""

        k = int(k)
        if self.entry_point < 0 or self.num_nodes == 0 or k <= 0:
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float32)
        ef = int(ef) if ef is not None else self.ef
        ef = max(ef, k)  # ef < k 时结果堆装不下 k 个，直接取不到
        q = self.kernel.prepare_query(query)
        ep = self.entry_point
        for lc in range(self.max_level, 0, -1):
            ep = self._greedy(q, ep, lc)
        W = self._search_layer(q, [ep], ef, 0, keep=keep)
        W = W[:k]
        ids = np.asarray([i for _, i in W], dtype=np.int64)
        ds = np.asarray([d for d, _ in W], dtype=np.float32)
        return ids, ds

    # -- 持久化 -----------------------------------------------------------
    def save_arrays(self, prefix: str = "hnsw_") -> Dict[str, np.ndarray]:
        """把图结构拍平成一组数组。``prefix`` 让 IVF 的每分区图能共存一个目录。"""

        arrays: Dict[str, np.ndarray] = {
            f"{prefix}meta.npy": np.array(
                [
                    self.entry_point,
                    self.max_level,
                    self.num_nodes,
                    self.m,
                    self.ef_construction,
                    self.ef,
                ],
                dtype=np.int64,
            ),
            f"{prefix}levels.npy": self.levels.astype(np.int32),
        }
        for level, lvl in enumerate(self.links):
            offs, flat = _pack_level(lvl)
            arrays[f"{prefix}links_{level}_offsets.npy"] = offs
            arrays[f"{prefix}links_{level}_neighbors.npy"] = flat
        return arrays

    @classmethod
    def from_arrays(
        cls,
        arrays: Mapping[str, np.ndarray],
        *,
        vectors: np.ndarray,
        metric: Any,
        prefix: str = "hnsw_",
    ) -> "HnswGraph":
        v = np.ascontiguousarray(np.asarray(vectors, dtype=np.float32))
        meta = np.asarray(arrays[f"{prefix}meta.npy"], dtype=np.int64)
        graph = cls(
            dim=int(v.shape[1]),
            m=int(meta[3]),
            ef_construction=int(meta[4]),
            ef=int(meta[5]),
            metric=metric,
        )
        graph.vectors = v
        graph.kernel = DistanceKernel(v, graph.metric)
        graph.levels = np.asarray(arrays[f"{prefix}levels.npy"], dtype=np.int32)
        graph.num_nodes = int(meta[2])
        graph.entry_point = int(meta[0])
        graph.max_level = int(meta[1])
        graph.links = []
        level = 0
        while f"{prefix}links_{level}_offsets.npy" in arrays:
            offs = np.asarray(arrays[f"{prefix}links_{level}_offsets.npy"], dtype=np.int64)
            flat = np.asarray(arrays[f"{prefix}links_{level}_neighbors.npy"], dtype=np.int32)
            n = int(offs.shape[0]) - 1
            graph.links.append([flat[int(offs[i]) : int(offs[i + 1])] for i in range(n)])
            level += 1
        return graph

    # -- 观测 -------------------------------------------------------------
    @property
    def num_levels(self) -> int:
        return len(self.links)

    def link_counts(self) -> np.ndarray:
        if not self.links:
            return np.zeros(0, dtype=np.int64)
        return np.asarray([int(a.size) for a in self.links[0]], dtype=np.int64)

    def stats(self) -> Dict[str, Any]:
        counts = self.link_counts()
        arrays = self.save_arrays()
        return {
            "num_nodes": int(self.num_nodes),
            "num_levels": int(self.num_levels),
            "m": int(self.m),
            "m_max0": int(self.m_max0),
            "ml": float(self.ml),
            "max_level": int(self.max_level),
            "ef_construction": int(self.ef_construction),
            "avg_degree_level0": float(counts.mean()) if counts.size else 0.0,
            "max_degree_level0": int(counts.max()) if counts.size else 0,
            "graph_bytes": total_nbytes(arrays),
            "distance_evaluations": int(self.distance_evaluations),
        }


def _pack_level(lvl: Sequence[np.ndarray]) -> Tuple[np.ndarray, np.ndarray]:
    """把一层的邻居表拍成 CSR（offsets + 扁平数组），方便落盘。"""

    n = len(lvl)
    offs = np.zeros(n + 1, dtype=np.int64)
    for i, arr in enumerate(lvl):
        offs[i + 1] = offs[i] + int(arr.size)
    if offs[-1] == 0:
        return offs, np.zeros(0, dtype=np.int32)
    flat = np.concatenate([np.asarray(a, dtype=np.int32) for a in lvl])
    return offs, flat.astype(np.int32)


def build_partition_graphs(
    prepared_vectors: np.ndarray,
    members: Sequence[np.ndarray],
    *,
    m: int,
    ef_construction: int,
    metric: Metric,
    seed: int = 0,
) -> List[HnswGraph]:
    """给 IVF 的每个分区各建一张 HNSW 图 —— 这就是 ``IVF_HNSW_*`` 的结构。

    为什么要"每分区一张图"而不是"整库一张图 + 分区过滤"：整库图的问题
    是每次查询都要在图里找出属于被探分区的点，等于把图搜索退化成了
    "图搜索 + 过滤"，代价随过滤比例上升。分区内建图则把"探nprobes个桶"
    的粗量化收益完整保留下来：每张图只有 ``n/P`` 个点，层数也更浅。

    代价是图的总边数变多（分区边界上没有边），以及每个分区都要单独
    训练一次结构 —— 小分区（几百个点）的图质量通常不如整库大图，
    这是 ``IVF_HNSW_*`` 召回率略低于纯 HNSW 的原因。
    """

    graphs: List[HnswGraph] = []
    dim = int(prepared_vectors.shape[1]) if prepared_vectors.ndim == 2 else 0
    for p, mem in enumerate(members):
        graph = HnswGraph(
            dim=dim,
            m=m,
            ef_construction=ef_construction,
            metric=metric,
            seed=int(seed) + p,
        )
        if len(mem):
            graph.build(np.ascontiguousarray(prepared_vectors[np.asarray(mem, dtype=np.int64)]))
        graphs.append(graph)
    return graphs


class HnswIndex(VectorIndex):
    """``HNSW``：整库一张图，没有粗量化。"""

    type_name = "HNSW"

    def __init__(self, metadata: IndexMetadata, metric: Any) -> None:
        super().__init__(metadata, metric)
        self.graph: HnswGraph = HnswGraph(dim=0, metric=metric)

    # -- 属性 -------------------------------------------------------------
    @property
    def vectors(self) -> np.ndarray:
        return self.graph.vectors

    @property
    def dim(self) -> int:
        return int(self.graph.dim)

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(
        cls,
        data: np.ndarray,
        row_ids: np.ndarray,
        *,
        metadata: IndexMetadata,
    ) -> "HnswIndex":
        metric = resolve_metric(metadata)
        matrix = prepare_matrix(as_matrix(data), metric)
        ids = as_row_ids(row_ids, matrix.shape[0])
        params = dict(metadata.params)
        m = require_positive(params, "m", 16)
        ef_construction = require_positive(params, "ef_construction", 100)
        seed = int(params.get("seed", 0))
        index = cls(metadata, metric)
        index.graph = HnswGraph(
            dim=int(matrix.shape[1]),
            m=m,
            ef_construction=ef_construction,
            metric=metric,
            seed=seed,
        )
        index.graph.build(matrix)
        index.row_ids = ids
        metadata.num_indexed_rows = int(matrix.shape[0])
        metadata.details.update(
            {
                "metric_type": metric.name,
                "dim": int(matrix.shape[1]),
                "m": int(index.graph.m),
                "ef_construction": int(index.graph.ef_construction),
                "num_levels": int(index.graph.num_levels),
                "max_level": int(index.graph.max_level),
            }
        )
        return index

    # -- 查询 -------------------------------------------------------------
    def search(
        self,
        queries: np.ndarray,
        k: int,
        *,
        mask: Optional[np.ndarray] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> SearchResult:
        p = dict(params or {})
        x = check_queries(queries, self.dim)
        k = check_k(k, self.num_indexed_rows)
        # ef 必须 ≥ k：结果堆装不下 k 个候选时，图搜索根本返回不出 k 条。
        ef = max(int(p.get("ef", self.graph.ef)), k)
        keep: Optional[np.ndarray] = as_mask(mask, self.num_indexed_rows)
        out_d = np.full((x.shape[0], k), np.inf, dtype=np.float32)
        out_i = np.full((x.shape[0], k), -1, dtype=np.int64)
        for i in range(x.shape[0]):
            local, dist = self.graph.search(x[i], k, ef=ef, keep=keep)
            if local.size == 0:
                continue
            out_d[i, : local.size] = dist
            out_i[i, : local.size] = self.row_ids[local].astype(np.int64)
        return SearchResult(out_d, out_i, approximate=True, stats={"ef": int(ef)})

    # -- 维护 -------------------------------------------------------------
    def append(self, data: np.ndarray, row_ids: np.ndarray) -> None:
        """HNSW 支持真正的增量插入：新点直接插进已有图，不需要重训。

        这是它相对 IVF 系列最大的工程优势 —— IVF 追加时新点只能挂到
        **旧**质心上，桶的分布会逐渐偏离数据分布。
        """

        matrix = prepare_matrix(as_matrix(data), self.metric)
        ids = as_row_ids(row_ids, matrix.shape[0])
        self.graph.append(matrix)
        self.row_ids = np.concatenate([self.row_ids, ids])
        self.metadata.num_indexed_rows = int(self.row_ids.shape[0])

    def remap(self, old_to_new: np.ndarray) -> None:
        """删除节点在 HNSW 图里是件麻烦事：删一个点要修补它所有邻居的边。

        真实 LanceDB 的做法是让索引失效后重建。这里同样选择重建 ——
        对小规模教学数据这是唯一不会引入细微 bug 的做法。
        """

        old = np.asarray(old_to_new, dtype=np.int64)
        keep = old[self.row_ids.astype(np.int64)] >= 0
        kept = self.graph.vectors[keep]
        self.row_ids = old[self.row_ids.astype(np.int64)[keep]].astype(np.uint64)
        params = dict(self.metadata.params)
        graph = HnswGraph(
            dim=self.dim,
            m=int(params.get("m", self.graph.m)),
            ef_construction=int(params.get("ef_construction", self.graph.ef_construction)),
            ef=self.graph.ef,
            metric=self.metric,
            seed=int(params.get("seed", 0)),
        )
        if kept.shape[0]:
            graph.build(np.ascontiguousarray(kept))
        self.graph = graph
        self.metadata.num_indexed_rows = int(self.row_ids.shape[0])

    # -- 持久化 -----------------------------------------------------------
    def _persisted_arrays(self) -> Dict[str, np.ndarray]:
        arrays = {"hnsw_vectors.npy": self.graph.vectors, "row_ids.npy": self.row_ids}
        arrays.update(self.graph.save_arrays())
        return arrays

    def save(self, store: IndexStore) -> List[str]:
        return save_arrays(store, self._persisted_arrays())

    @classmethod
    def load(cls, store: IndexStore, metadata: IndexMetadata) -> "HnswIndex":
        metrics = resolve_metric(metadata)
        index = cls(metadata, metrics)
        arrays = {name: store.read_numpy(name) for name in store.list()}
        vectors = np.ascontiguousarray(arrays["hnsw_vectors.npy"], dtype=np.float32)
        index.row_ids = np.asarray(arrays["row_ids.npy"], dtype=np.uint64)
        index.graph = HnswGraph.from_arrays(arrays, vectors=vectors, metric=metrics)
        metadata.num_indexed_rows = int(index.row_ids.shape[0])
        return index

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        arrays = self._persisted_arrays()
        info = {
            "index_type": self.type_name,
            "num_indexed_rows": int(self.num_indexed_rows),
            "size_bytes": total_nbytes(arrays),
            "memory_bytes": int(self.graph.vectors.nbytes) + int(
                self.graph.stats()["graph_bytes"]
            ),
            "dim": self.dim,
            "metric_type": self.metric.name,
            "vectors_kept": True,
            "persisted_files": sorted(arrays),
        }
        info.update(self.graph.stats())
        info["compression_ratio"] = 1.0
        return info

    def __repr__(self) -> str:  # pragma: no cover
        return f"HnswIndex(rows={self.num_indexed_rows}, levels={self.graph.num_levels})"
