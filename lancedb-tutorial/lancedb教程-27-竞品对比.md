# 第 27 章　竞品对比

这一章的立场是：**向量检索已经不是一个"选哪家数据库"的问题，
而是一个部署形态问题。**

同样 2000 万条 768 维向量，你可以塞进一个进程内库、一个 Kubernetes 集群、
一个 Postgres 扩展，或者纯对象存储上的一个文件格式。
**形态决定了你的账单曲线、你的运维负担，以及你能不能在同一个系统里
同时做向量检索和列式分析。**

所以这一章不做"谁最快"的排行。每个竞品都按同一组轴拆开，
最后给一张汇总表和一段不替 LanceDB 说好话的结论。

**写作日期 2026-10-17。** 所有实时数据（GitHub 星数等）均标注
`as of 2026-10-17`；厂商自报的性能一律标为**厂商口径**；
本教程的推断标为**推测**；没能核实的写**未经确认**。

## 27.1 先把评估轴钉死

不同厂商对同一件事用的词不一样，先定义清楚，后面才不用反复解释。

**部署模式**：`embedded`（进程内库）/ `server`（独立服务端）/
`managed cloud`（厂商托管）/ `object-storage-native`
（元数据与数据都以对象存储为唯一真源）。
它决定你是否需要一个 7×24 常驻集群——而常驻集群的成本与向量条数
**基本无关**，与"索引必须常驻内存"这件事强相关。

**存储底座**：内存 / 本地盘 / 对象存储原生。

**索引类型**：ANN 索引族（IVF / HNSW / DiskANN(Vamana) / ScaNN / SPANN /
SPFresh）与量化（PQ / SQ / BQ / RaBitQ / TurboQuant）。
它决定建索引要多少内存、查询能压到多低延迟、增量更新的代价。

**过滤能力**：`postfilter`（先 ANN 取 top-k 再过滤，**可能返回不足 k 条**）/
`prefilter`（先算命中集合再在集合内做 ANN）/
`filtered-ANN`（把过滤条件编进图结构本身，如 filter-aware HNSW）。
生产负载里绝大多数查询带 filter，**postfilter 在低选择性过滤下会静默
返回空结果——这是最常见的线上事故**。

**版本与时间旅行**：表级不可变快照链、按版本号/时间戳读取、
branch / tag / checkout。要区分两件事：**备份是灾备能力，版本是数据模型能力**。
前者恢复慢、粒度粗、不可查询；后者可以 checkout 到任意历史版本继续读。

**多模态支持**：能不能把原始图片/视频/音频字节与向量放进**同一个存储单元**，
以及能不能对非向量列做列式扫描。

**写在并发下的表现**：多写者、长事务、持续摄入时的行为。

**许可与成本模型**：SPDX 名 + 限制条款（copyleft 与否、是否允许"作为服务提供"）
+ 计费维度。

**社区体量**：它不是技术指标，但它直接体现为 Stack Overflow 答案更少、
第三方教程更少、招人更难。

`as of 2026-10-17` 的星数对照（GitHub 仓库页实时抓取）：
Milvus **46,401**、Qdrant **35,101**、Chroma **29,511**、
pgvector **23,386**、Weaviate **16,887**、
**lancedb/lancedb 11,684**、Vespa 7,129、**lance-format/lance 7,162**。
**LanceDB 在这个赛道上体量上是明显的挑战者，不是领先者。**

## 27.2 基线：LanceDB / Lance 自己的九轴

后面每一节都拿这些事实做参照，所以先把 LanceDB 自己按同一组轴钉一遍。

**部署模式**是 **embedded（OSS）+ BYOC / 托管（Enterprise）**，对象存储是唯一真源；
**存储底座**是对象存储原生，**没有必须存在的第三组件**（不需要 etcd / WAL / 控制库）。
**索引类型**包括 `IVF`（None/PQ/SQ/RQ）、`IVF_HNSW`（None/SQ/PQ）、`FTS`（BM25），
以及 `BTree` / `Bitmap` / `LabelList` / `FM` 这些标量索引。
**过滤以 prefilter 为默认**，可以 `prefilter=False` 切回 postfilter。

**版本与时间旅行是版本树 + tags + branches + checkout / restore**；
**多模态与列存**是 blob 内联 + 完整列存，同一张表既能 ANN 又能 OLAP 扫描。
**并发写**是单写者 + 多读者，S3 原生原子写，不需要外部 commit 协调器。
**许可**是 Apache-2.0，无企业闭源目录、无双许可。
**规模**上 OSS 的舒适区是官方 FAQ 原话的"can comfortably handle **millions of
vectors** on a single node"；再往上（"hundreds of millions of vectors or
terabytes"，官方措辞）官方就导向企业版了（来源与完整引文见第 29.6.2 节）。
**注意单位是"向量"不是"行"**——这两个数不能互换：
一行可以携带多个向量（第 21 章的多向量列），也可以完全不携带向量，
所以"行数"在这里不是个可比的量纲。

## 27.3 FAISS（Meta）

**一句话定位：它是 ANN 内核，不是数据库。**

FAISS 是 C++ 实现 + Python/numpy 包装，唯一必需依赖是 BLAS，
GPU 可选（CUDA / ROCm，也支持以 NVIDIA cuVS 作后端）。
它的架构以 `Index` 为中心：`IndexFlat*` 把向量平铺存储，
`IndexIVF*` 用 k-means 切成 `nlist` 个倒排桶、查询探 `nprobe` 个桶，
`IndexHNSW` 是在 Flat 之上叠图。

**它只支持 embedded**，没有服务端、没有 CRUD 语义、没有用户与多租户；
分布式要自己拼（官方 FAQ 给的是"分机训练-加载-合并"或 `IndexShards`）。

索引类型（精确名）：`IndexFlatL2` / `IndexFlatIP`、`IndexHNSWFlat`、
`IndexIVFFlat`、`IndexIVFScalarQuantizer`、`IndexIVFPQ`、`IndexIVFPQR`、
`IndexScalarQuantizer`、`IndexPQ`、`IndexLSH`；
较新的工厂串里还有 `Vamana`（经 Intel SVS 集成，DiskANN 式图）、
`RaBitQ` / `RaBitQ4`、`LeanVec4x4/8x8`、`ITQ` 等。
注意 **FAISS 本体没有名叫 DiskANN 的类**，磁盘化要自己接第三方实现。

过滤靠 `IDSelector` 家族（`IDSelectorRange` / `IDSelectorArray` /
`IDSelectorBatch` / `IDSelectorBitmap` 及 And/Or/Not 组合），
通过 `SearchParametersIVF.sel` 传入，**只在 IVF 系列里生效**。
本质是"调用方先算好 id 集合"的 prefilter：**没有倒排/位图加速的
filtered-HNSW，也没有 scalar index 概念**——FAISS 不存任何标量字段。

**版本与时间旅行：无。** 只有整索引导出的 `write_index` / `read_index`，
没有 MVCC、没有快照链。
**多模态与列存：都不支持。** 只接受 `float32` 矩阵，
**不支持字符串 id**，没有 payload/JSON/列式布局。
**许可证：MIT**——比 LanceDB 还宽松。

FAISS 与 LanceDB 不构成替代关系：LanceDB 的引擎内部用的就是
IVF / PQ / RaBitQ 这一族算法。两者真正的差别是**FAISS 止步于内核**，
而 LanceDB 把列存、版本、标量索引、多模态一起做进来了。
如果只是要一个算法内核自己造数据库，FAISS 仍然是正确答案，
因为它是 MIT、算法最全、零成本。

## 27.4 Milvus / Zilliz Cloud

**一句话定位：云原生分布式向量数据库，功能最全、运维也最重。**

Milvus 是四层架构、存储与计算完全分离：Access Layer（无状态 Proxy）、
Coordinator（单 active，管 DDL/DCL/TSO/负载均衡）、Worker Nodes、
Storage（etcd 存元数据、MinIO/S3/Azure Blob 存对象存储、
WAL 用 Kafka/Pulsar 或自研的 "zero-disk" 设计）。
核心是 Go，**向量执行引擎 Knowhere 是 C++ 写、经 CGO 被 Go 调用**，
官方称 Knowhere 是 "refactoring the codebase of Faiss"。

**它属于 object-storage-native，但对象存储只是"多组件之一下"而不是唯一真源**
——etcd 与 WAL 同样是必需件。这正是它和 LanceDB 最本质的架构差别。

索引类型（精确名）非常全：浮点向量 `FLAT`、`IVF_FLAT`、`IVF_SQ8`、
`IVF_PQ`、`IVF_RABITQ`、`HNSW`、`HNSW_SQ`、`HNSW_PQ`、`HNSW_PRQ`、
`DISKANN`、`SCANN`、`AISAQ`；二值向量 `BIN_FLAT`、`BIN_IVF_FLAT`；
稀疏向量 `SPARSE_INVERTED_INDEX`。GPU 侧有 `GPU_CAGRA`（NVIDIA 贡献）、
`GPU_IVF_FLAT`、`GPU_IVF_PQ`。`DISKANN` 是官方磁盘方案：
Vamana 图 + PQ codes 常驻内存，用 `pq_code_budget_gb_ratio` 调内存/精度。

过滤走 **prefilter**：谓词被解析成 AST → 物理计划 → **在每个 segment
生成 bitset**，再作为向量搜索参数收窄范围；低选择性过滤靠
**iterative filtering** 兜底（迭代返回候选逐个过滤直到凑满 topK，
代价是量大时变慢）。标量索引分 `AUTOINDEX` 与 Tantivy 驱动的
inverted indexing。**它的 HNSW 不是 filter-aware 图**——
走的是"bitset 预过滤 + 图搜索"，这与 Qdrant/Weaviate 的路线不同。

**版本与时间旅行：算半个。** Milvus 给每条实体打 timestamp，
暴露 `guarantee_timestamp` / `service_timestamp` / `travel_timestamp`，
可以查询某个时间点的数据视图。**但这是时间戳式历史读，
不是 Lance 那种可 branch、可 checkout 的版本树**；
能追溯多远还受 compaction / GC 影响（未经确认）。

许可证 **Apache-2.0**，无附加条款。治理上项目在 LF AI & Data 基金会下，
Zilliz 是主要贡献者。**"Milvus 换过 License"这个说法本轮未确认，
不要凭印象写。**

优点：组件最全（稀疏向量 + scalar index + GPU + DiskANN + 时间旅行）、
K8s 生态成熟、集成面最广。
缺点：**运维最重**（etcd + 对象存储 + WAL 三件套）；
全内存 HNSW 时内存开销大——官方自己拿 1B SIFT 举例，
`IVF_FLAT` 索引约 470 GB，`IVF_SQ8` 仍需 140 GB；
官方也承认对象存储"高访问延迟且按请求数计费"是成本痛点。

在开源自托管十亿级集群这个需求上，Milvus 的开源版**没有**
LanceDB OSS 那种单机百万级的天花板，这是它明确赢的一项。

## 27.5 Qdrant

**一句话定位：Rust 写的 client-server 向量库，过滤能力是这个领域的标杆。**

部署形态是 client-server，暴露 HTTP(6333) 与 gRPC(6334)；
数据分 collections → shard → segment 三级。官方明确 segment 数影响性能
（"Fewer segments create larger segments with better search throughput"）。
**没有 API 层面的嵌入形态**（Rust crate 可嵌入，但主线是 server）。
官方列四种部署：Managed Cloud、Hybrid Cloud、Private Cloud、开源自托管。

**索引类型只有 HNSW**（默认 `m: 16`、`ef_construct: 100`，
查询时 `ef` 默认等于 `ef_construct`），外加稀疏向量倒排索引
（`sparse_vectors`，可设 `memory: pinned|cached|cold`）。
**没有 IVF / DiskANN / ScaNN 这类可选索引族**——
它是"HNSW + 量化 + 磁盘分层"的路线，而不是"多索引可选"。
量化四种：Scalar Quantization（float32→uint8，约 4×）、
Binary Quantization（1/1.5/2 bit，最高约 32×）、
Product Quantization（最高约 64×）、以及 TurboQuant（先随机旋转再量化，
1.5/2/4-bit）。另有 GPU 加速的 HNSW 建索引。

**过滤是它的招牌，机制分三层**：① **filterable HNSW**——
"extends the HNSW graph with additional edges based on indexed payload
values"，把过滤条件编进图；**payload indexes 必须在灌数据前建**，
否则要重建 HNSW 才能吃到 filter-aware 边。② **ACORN**（v1.16.0 起）——
多路过滤组合时图会断连，ACORN 在图遍历时连"邻居的邻居"一起看。
③ **full-scan 回退**——`full_scan_threshold_kb`（默认 10000，
注释里 1 KB ≈ 1 个 256 维向量）：谓词估计命中量低于阈值时，
查询规划器**直接全扫描而不走 HNSW**。还可以开 strict mode +
`unindexed_filtering_retrieve=false` **直接拒绝**在未索引字段上过滤的查询。
payload index 会**精确估计 filter cardinality 供规划器选策略**。

**版本与时间旅行：无。** 提供的是一次性 snapshot（tar 包，
用于归档/复制/灾备），没有按版本号/时间点读取的接口，没有 branch 语义。
**多模态与列存：无列存**（推测），载荷是任意 JSON，
支持 named vectors、multivectors / late interaction（ColBERT 式）、稀疏向量，
进而支持 hybrid。**许可证 Apache-2.0。**

缺点：索引选择单一（超大规模/低内存场景没有 Milvus DISKANN 那种选项）；
payload index 必须先建后灌；filter-aware 边会增加索引延迟与图体积，
需要按字段 `enable_hnsw: false` 手动关掉；自托管下副本与 shard 迁移要手动。

在"过滤是核心负载、条件复杂、选择性低"这个方向上，
Qdrant 与 Weaviate 是这个领域做得最深的两家。

## 27.6 Weaviate

**一句话定位：Go 写的向量优先数据库，过滤策略最多，但有一部分不是真开源。**

每个 shard 内含三个互不依赖的组件：**对象存储、倒排索引存储、向量索引**。
对象存储与倒排存储自 v1.5.0 起用 **LSM-Tree**（memtable → 落盘 sorted segment，
读时用 **Bloom filter** 跳过无关 segment，后台持续 merge）；
**向量索引不使用 segmentation**——官方原文是
"Weaviate keeps the vector index as large as possible within a shard.
HNSW indexes cannot be merged efficiently."
分布式上，元数据用 **Raft**（管 collection 定义与 shard/tenant 状态），
**数据复制是无主的 leaderless（Dynamo 式）**，靠可调一致性
（`ALL` / `QUORUM` / `ONE`）平衡；节点发现用 gossip。

索引类型：`HNSW`（**默认**）、`flat`、`dynamic`（v1.25 起实验性，
对象数超阈值默认 **10,000** 时由 flat **单向**升级到 HNSW）、
`HFresh`（v1.36 新增，基于 **SPFresh**：向量切成 cluster/posting list，
**HNSW 只索引质心并常驻内存**，posting list 落盘；质心用 8-bit RQ、
posting 用 1-bit RQ；**只支持 cosine 与 l2-squared，不支持 dot product**）。
量化有 RQ（推荐）、PQ、BQ、SQ。

**过滤有三条路**：倒排索引生成 **allow-list**（uint64 id 列表）交给 HNSW，
图照常走边、只在入结果集时检查 allow-list。`filterStrategy` 三选一：
`sweeping`（逐节点判过滤）、**`acorn`（默认，多跳评估候选邻域 +
随机 seed 符合过滤的入口点）**、`pathseer`（**v1.40 新增**，来自 **SIGMOD 2026
收录的论文《PathSeer: Adaptive Neighbor Handling for Efficient Filtered ANN
Search》**，ACM DOI 10.1145/3802098；逐节点自适应地在"先算距离"与"先判谓词"
两种策略间切换，多数基准里最快。**但 ACORN 仍是默认**，而且在"过滤条件与最近邻
负相关"的一类过滤上 ACORN 反而快得多 —— 它不是无条件的赢家；
**从 v1.40 降级前必须改回 acorn / sweeping**）。
另有 **Flat-Search Cutoff**：过滤太严（约 <15% 命中）时自动退化为
brute-force 只扫子集。索引结构是 `indexFilterable`（**Roaring Bitmap**）
与 `indexRangeFilters`（int/number/date 的 range 索引，**仅对新属性可用**）。

**版本与时间旅行：无。** 有 Backups（原生对接 S3/GCS/Azure、跨 provider 恢复、
增量备份）与 tenant 状态机（`ACTIVE` / `INACTIVE` / `OFFLOADED`），
没有按时间点/版本号读取的接口。
**多模态与列存：无列存**（推测）。多租户是"租户 = shard"，
官方称单节点可轻松承载 5 万+ active shard。

**许可证要写对，不能只写 BSD-3-Clause。** 仓库根 LICENSE 写明
"Source code in this repository is variously licensed under the
BSD-3-Clause License, or the Weaviate License"：**`wl/` 目录之外的代码是
BSD-3-Clause，`wl/` 目录及子目录"all rights reserved，
only available under a separate enterprise license"**，
且"The BSD license does not grant the right to use these features or
circumvent any license key"。**这意味着部分能力不是真开源**——
这是它与 Qdrant/Milvus 纯 Apache-2.0 的实质差别，也是做闭源 SaaS 时要查的第一件事。

缺点：`wl/` 企业目录的许可问题；默认 HNSW 全内存代价高（官方表格：
1M 向量节点约 2–12 GB、边约 200 MB；1 亿向量节点 200–1200 GB，
文档直言 "memory requirements … can quickly become a bottleneck"）；
`dynamic` 与 `HFresh` 都较新/实验。

## 27.7 Chroma

**一句话定位：原型阶段最省事的嵌入式向量库，但它的强项在单机，不在规模。**

2024 年重写后核心为 Rust（workspace 含 `rust/index`、`rust/worker`、
`rust/storage`、`rust/sqlite`、`rust/wal3`，依赖 arrow / parquet /
object_store / aws-sdk-s3）。分布式版由五组件构成：
Gateway、Log（WAL）、Query Executor、Compactor、System Database。
官方口径是尽量把工作委托给 SQLite 和云对象存储这类成熟子系统。

部署三档：**Local（嵌入式库）**、Single-Node（官方称适合 < 1000 万条记录）、
Distributed + Chroma Cloud（serverless）。
索引上，Single-Node 用 **HNSW**（`ef_construction` 默认 100、
`ef_search` 默认 100、`max_neighbors` 默认 16）；
**Distributed / Cloud 用 SPANN**（簇内二级索引；`search_nprobe` 的默认值随版本与
自适应逻辑变化 —— 上游代码里按集合规模取 24 / 32 / 64 一档，
文档里"默认 64、上限 128"这个说法本轮未能核实），
且**SPANN 参数当前不接受自定义、设了会被忽略**。

数据模型是 `id + embedding + metadata + document`。
内建多种倒排/FTS 索引（`StringInvertedIndexConfig`、
`IntInvertedIndexConfig`、`FloatInvertedIndexConfig`、
`BoolInvertedIndexConfig`、`FtsIndexConfig`、`SparseVectorIndexConfig`），
元数据过滤默认对全部元数据启用；`where(...)` 可与向量、全文、稀疏检索组合，
官方支持 RRF 做 hybrid。

**版本与时间旅行：无通用时间旅行**；但有 **Collection Forking**
（copy-on-write 分叉，**仅 Chroma Cloud**，单机存储引擎不支持）。
**底层虽用 Arrow/Parquet 做块存储，但对外不暴露列存查询**（推测）——
这正是"有列存"与"是列存"的差别。**许可证 Apache-2.0。**

Chroma 与 LanceDB 在"嵌入式"这一轴上确实重叠，但能力的重心不同：
Chroma 的重心是"最简单地把向量存起来搜出来"，LanceDB 的重心是
"在同一个文件里同时管向量、原始字节、列式分析与版本"。

## 27.8 Pinecone

**一句话定位：纯托管、闭源、零运维的代表。它把"运维负担"这一项做到了零。**

架构是两层：资源层级（organization → project → index → namespace → record）
+ serverless 基础设施。请求经 API gateway 路由到全局 **control plane**
或区域 **data plane**。所有向量数据存分布式对象存储，
每个 namespace 的数据组织为不可变文件 **slabs**。
写入先记 LSN 到 request log 并立即返回 `200 OK`，再进 memtable 后台落盘；
读时先查 memtable 再查 slab 缓存（内存/本地 SSD）。
**没有自托管选项。**

索引类型**专有，不公开算法**。新式是 **document index**
（schema 里声明 `dense_vector`、`sparse_vector`、`full_text_search` 字段，
单索引同时服务 BM25 全文、语义、稀疏检索）；旧式是 vector index。
全文用 **BM25 + Lucene 查询语法**（官方文档如此描述；算法实现本身未公开）。
过滤是元数据过滤 + 文档 schema 的 `$match_phrase` / `$match_all` / `$match_any`，
官方称 query executor 对 slab **先排除不匹配记录再找最佳匹配**，即过滤下推
（**厂商口径**，是否 filter-aware 未公开）。

**版本与时间旅行：无**（Backups 不可查询 + namespace alias）。
**多模态与列存：无**，不存原始二进制。
**许可证：闭源 SaaS**（具体条款未经确认）。

优点：无服务端化最彻底，不需要任何运维；有免费档让"先试后买"的路径最短。
缺点：不可自托管（这是与 LanceDB 最根本的差别，不是性能差别）；
算法不透明；无版本/无列存/不存原始字节。

## 27.9 pgvector 系：pgvector / pgvecto.rs / VectorChord

这一组要一起看，因为它们**不是三家独立产品，而是一条演进线**。

**pgvector** 是 Postgres 扩展，C 编写，直接挂在 Postgres 存储引擎上。
向量类型有 `vector`（单精度，≤2000 维）、`halfvec`（半精度，≤4000 维）、
`bit`（≤64000 维）、`sparsevec`（≤1000 非零元）。最新 0.8.7（2026-10-01）；
**0.8.0（2024-10-30）是分水岭**，加入**迭代索引扫描**并改进过滤时的代价估算。
索引只有 `IVFFlat` 与 `HNSW`，距离算子含 L2/IP/Cosine/L1/Hamming/Jaccard。

**过滤是 pgvector 最著名的坑。** 文档原文：近似索引下过滤在索引扫描
**之后**应用；若条件命中 10% 行、HNSW 默认 `ef_search=40`，
平均只有 4 行匹配——**即历史上是 postfilter**。
0.8.0 起可开 `hnsw.iterative_scan`（`strict_order` / `relaxed_order`）
自动多扫索引直到凑够结果或达 `hnsw.max_scan_tuples`；
也可以用 partial index（带 `WHERE` 建索引）做预过滤。
**注意迭代扫描是"可选项"而不是默认行为——升级了不等于问题消失。**

**版本与时间旅行：无向量级能力**（继承 Postgres 的 MVCC 与 PITR
是集群级能力，不是"查询历史版本的索引"）。
**多模态与列存：行存 OLTP 存储，不含列存，不存原始二进制。**
**许可证：PostgreSQL License**，与 MIT 同级宽松。
**23,386 stars**（as of 2026-10-17），是这一组里生态最大的。

**pgvecto.rs** 是 Rust + pgrx 的重写，索引的存储与内存独立于 Postgres
存储引擎管理，支持到 65535 维，**索引与数据的 WAL 支持标为
"working in progress"**。**官方已劝退**：README 顶部声明这是旧实现，
建议用户迁移到 VectorChord。

**VectorChord** 是 pgvecto.rs 的继任者，**依赖 pgvector**、复用其
`vector` 表示与数据类型（"Fully compatible with pgvector data types and
syntax"）。新索引类型 `vchordrq`，底层用 **RaBitQ 压缩 + 自主 rerank**，
支持 RaBitQ4（4-bit）与 RaBitQ8（8-bit）原生向量类型
（官方称 RaBitQ8 <1% 召回损失，**厂商口径**）。
**VectorChord-bm25** 是独立扩展，在 PG 内实现 Block-WeakAnd 的 BM25 排序。
过滤提供 Prefilter / Prefetch / Rerank in Table / Similarity Filter 等页。
官方宣称 100M × 768 可跑在单台 AWS i4i.xlarge（$247/mo）、
100M 向量 20 分钟建索引（**均为厂商口径，未独立验证**）。

**VectorChord 的许可证是重点，容易写错：双许可 AGPLv3 或
Elastic License v2（ELv2），不是 Apache-2.0。**
LICENSE 文件标题即 "Dual License Notice"，正文列出 GNU AGPL v3 与
ELv2；VectorChord-bm25 同样是 AGPLv3/ELv2 双许可。
ELv2 明确限制"把软件作为服务提供"——**对闭源 SaaS 集成是实质性许可收紧**。
（"VectorChord 早期是 Apache-2.0、何时切换"未经确认；
可确认的是前身 pgvecto.rs 为 Apache-2.0。）

选择建议：已经在重度使用 Postgres、要事务 + JOIN + 备份一起时，
pgvector 系是最自然的选择。但**默认 postfilter 这一条要在选型时确认清楚**。

## 27.10 Vespa

**一句话定位：真正的搜索引擎，把关键词、结构化过滤、向量与 ML 推理
放在一次查询里——能力最强，学习曲线也最陡。**

Java + C++ 混合：无状态 Java container clusters（托管 Searchers、
Document Processors 等自定义组件）+ 零或多个 content clusters
（搜索核心是 **Proton**，以原生进程 `vespa-proton-bin` 运行）。
部署为自托管或 Vespa Cloud，另有 **Vespa Cloud Enclave**——
让 Vespa Cloud 应用跑在租户自己的云账号内（BYOC 语义）。**无嵌入式形态。**

索引类型：**HNSW**（tensor 字段加 `index { hnsw { max-links-per-node: 16;
neighbors-to-explore-at-insert: 100 } }`，官方称是改良版）+
**BM25**（`enable-bm25`，Okapi BM25；官方已把 nativeRank 归为遗留算法，
只有"BM25 has largely replaced nativeRank"这类定性说法、没有公开的倍数）+
**张量多阶段排序**（multi-phase ranking，可接 ONNX / TensorFlow /
XGBoost / LightGBM 模型）。

过滤上 ANN 官方支持 **prefilter 与 postfilter 两种**，
以及 beam exploration、距离计算前过滤（"Acorn 1"），并提供调参工具；
`nearestNeighbor` 算子可与 YQL 的过滤/查询词任意组合。

**版本与时间旅行：无向量级时间旅行。** 文档强调原子部署的
application package（配置+代码+模型自包含），但那是**部署**版本化，
不是**数据**快照。
**多模态：张量支持 double/float/bfloat16/int8/单 bit，
mixed tensor（"map of vectors"）多向量索引用于文档多 chunk/多模态**；
支持 parent-child 关系；实时 CRUD、可变更 HNSW 图。**非列存数据库。**
**许可证 Apache-2.0。**

优点：单查询内融合关键词 + 结构化过滤 + 向量 + ML 推理（YQL）；
生产级分布式与自动 re-balance/failover；许可宽松；实时索引。
缺点：概念与学习曲线陡（application package / schema / rank-profile）；
需自运维容错集群（或付费 Cloud）；**无 serverless、无嵌入式**；
对"只想存向量搜相似"的轻量场景过重。

## 27.11 Elasticsearch / OpenSearch

**一句话定位：如果已经有 ES/OS 集群，kNN 是"顺手加上去"的能力；
但许可证与内存模型是两个长期包袱。**

**Elasticsearch** 基于 Apache Lucene，向量存于 `dense_vector`（稠密）、
`sparse_vector`、`semantic_text`（托管语义字段）。
索引级 `index.mode: vectordb_document` 可一键套用向量库调优默认值。

索引默认用 Lucene 实现的 **HNSW**，`flat` 做暴力精确 kNN。
`index_options.type` 取值非常全：`hnsw` / `int8_hnsw` / `int4_hnsw` /
`bbq_hnsw` / `flat` / `int8_flat` / `int4_flat` / `bbq_flat` / `bbq_disk`。
量化比率：int8 约 4×、int4 约 8×、**BBQ（1 bit/维）约 32×**。
float 向量 ≥384 维默认 `bbq_hnsw`、<384 维默认 `int8_hnsw`
（这是 **9.1 起**的行为 —— `bbq_hnsw` 到 9.1 才被设为该维度的默认值，别当成 9.0 就有）；
**Elastic Stack 9.0 起 float 稠密向量一律默认 `int8_hnsw`**；
**`bbq_disk` 需 Enterprise 订阅**。量化保留原始 float 重打分，
盘占用额外增约 25%(int8) / 12.5%(int4) / 3.1%(bbq)。
HNSW 参数 `m` 默认 16、`ef_construction` 默认 100。

**过滤有一条极易搞错的规则**：**只有把 filter 放进 `knn` 子句
（`knn` 顶层选项、`knn` 查询、knn retriever 的 `filter`）才是过滤式 ANN**
——过滤发生在近似检索过程中并保证凑够 k 条；
**放在 `knn` 子句外的 bool filter 是后过滤**，可能远少于 k 条。
HNSW 下**选择性越强的过滤反而越慢**，因为要探索更多图。

**许可证是本章最复杂的一家**：默认发行版始终是 ELv2；
源码自 7.11 起双许可 SSPL 1.0 + ELv2；
**2024 年（8.16 GA 前）追加 OSI 认可的 AGPLv3**，
形成 **ELv2 / SSPL1.0 / AGPLv3 三选一**；更早（≤7.10）为 Apache-2.0；
**客户端库仍是 Apache-2.0。**

**OpenSearch** 是 Elasticsearch 7.10.2 的 Apache-2.0 分支，
向量由 knn 插件提供，字段类型 `knn_vector`。
method 有 `hnsw` / `ivf` / `flat` / `disk_ann`；
engine 有 `faiss` / `lucene` / `nmslib`（**已弃用**）/ `jvector`。
**Lucene engine** 定位 <1000 万向量、高延迟高质量；
**Faiss** 支持 HNSW + IVF（IVF 需训练，`nlist`/`nprobes`），
带 `pq` / `sq` encoder（**维度需为 8 的倍数**）；
**JVector** 是纯 Java 的 DiskANN 型 `disk_ann`，并发写、增量合并、
原生 PQ、无需 JNI。官方内存估算 **HNSW ≈ 1.1×(4×dim + 8×m) 字节/向量**。

过滤上，Lucene 与 Faiss 引擎会**自动**在"精确 kNN + 预过滤"与
"近似 + 改良后过滤"间选择；Faiss 由
`knn.advanced.filtered_exact_search_threshold` 决定何时回退精确。
**3.1 引入 ACORN 优化**，**仅在过滤掉 ≤60% 邻居时启用**。
官方对比表把 Lucene/JVector 的过滤归为 "Filter during search"、
Faiss 归为 "Post-filter"。**许可证 Apache-2.0**——
这是它在许可上碾压 Elasticsearch 的地方。

两家的共同短板：**无对象存储原生、无分支/时间旅行、HNSW 常驻内存**。
OpenSearch 额外还有"向量能力由插件提供、版本间行为差异大"的问题。

## 27.12 Turbopuffer

**一句话定位：与 LanceDB 最像的架构对手——同样 object-storage-native、
同样支持 branching；但它闭源、纯 SaaS、且拿掉了列存与原始字节。**

架构上**所有持久态只存对象存储（S3），计算节点无状态**，
写入直接以对象存储为 WAL、组提交（每 namespace 每秒 1 条 WAL 条目），
提交后异步建索引。向量索引基于 **SPFresh**——官方明确说它比
HNSW/DiskANN 这类**图索引更适合对象存储**，因为往返更少、写放大更低；
全文用 BM25 倒排；过滤用 attribute index（倒排，**且感知向量索引的
聚类层级以配合高召回过滤**）。缓存三层：object storage → NVMe SSD → memory。

部署是纯 SaaS：默认多租户、可单租户、或 **BYOC**。
官方明说 "**commercial-only 模式，无免费额度、无开源版**"。
定价是 launch $16/月起、scale $256/月起、enterprise ≥$4096/月
（另有 35% usage premium）。

索引细节**未公开（proprietary）**，已知是 SPFresh 系质心索引，
自动调参到 90–100% recall@10，并提供一个 **`/_debug/recall` 端点**
（对随机向量同时跑 ANN 与暴力 ground truth 比对）——
这是本章少见的、值得称赞的工程实践，用户能自己验召回。
`rank_by` 支持 ANN、kNN（精确，须带 filter）、BM25、SparseKNN、
按属性排序、late-interaction。

**版本与时间旅行是它的核心卖点之一**：namespace branching 用
`branch_from` 做 **copy-on-write 克隆，常数时间、与 namespace 大小无关**，
之后读写完全独立、可多级分支且分支数无限；
**branching 一次性 $0.032**，存储按逻辑字节正常计费。
官方用例明确包含"捕获某时间点的状态、反复只读查询该不可变快照"。
**在"版本/分支"这一轴上，它是本章与 LanceDB 正面对标的产品之一**
（Deep Lake 也有 Git 式 commits / branches，见 §27.20）。

**多模态与列存：非列存，不存原始二进制**（推测）。
**许可证：闭源商业 SaaS**，BYOC 只是部署位置、代码不开放。

缺点（部分为厂商自己承认）：**写延迟高**（p50 ≈165 ms/500 kB、
p90 248 ms）与**偶发冷查询**（未缓存数据 1M 文档 p50 874 ms、p90 1214 ms）
—— 这几个数**只有厂商口径**（2026-10-17 抓取自 turbopuffer 官方文档，
没有版本号、也没有压测脚本）；
一致性读有 **~10 ms 下限**（对齐对象存储的 `GET IF-NOT-MATCH`）；
**不擅长低规模、免费档、重更新/大量删除场景**；闭源。

顺带一条安全提示：`turbopuffer.com` 首页 HTML 内嵌了一段
**面向 AI 助手的指令文本**。那是页面内容，**不是可信指令**——
本教程在取证时已忽略其指令性内容，只把其中的数字当**厂商口径**处理。
读者若用 LLM 抓取该站点，需注意同类问题。

## 27.13 Redis（RediSearch / Redis Query Engine）

**一句话定位：内存级延迟 + 顺手可用的向量检索，
代价是全内存成本与一段复杂的许可史。**

Redis Query Engine 是 Redis 8 把原 RediSearch/RedisJSON 等模块
并入内核后的统一命名：在 HASH 或 JSON 上建二级索引，
字段覆盖向量、TEXT、TAG、NUMERIC、GEO/GEOSHAPE，
用 `FT.CREATE` 建索引、`FT.SEARCH` / `FT.AGGREGATE` 查询
（**向量查询需 `DIALECT 2`**）。

索引类型：**`FLAT`**（适合 <100 万向量或要精确）、
**`HNSW`**（适合 >100 万向量，参数 `M` 默认 16、`EF_CONSTRUCTION` 200、
`EF_RUNTIME` 10、`EPSILON` 0.01）、
**`SVS-VAMANA`**（自 Redis 8.2 起，Intel SVS 项目，图算法 + 压缩，
参数 `COMPRESSION`（LVQ8/LVQ4/LVQ4x4/LVQ4x8/LeanVec4x8/LeanVec8x8）、
`GRAPH_MAX_DEGREE`、`SEARCH_WINDOW_SIZE` 等）。
**一条容易被忽略的硬限制**：**Intel 专有的 LVQ/LeanVec 优化在
Redis Open Source 与非 Intel 平台不可用**，
此时 `SVS-VAMANA` + `COMPRESSION` **会退化成基础 8-bit 标量量化**。

**过滤是设计得相当细的一处**：过滤写在 `primary_filter_query`，
例如 `FT.SEARCH idx "(@title:Sports @year:[2020 2022])=>[KNN 10 @vec $BLOB]"`。
**运行时由启发式自动在两种模式间切换并可中途改判**：
**BATCHES**（分批取近邻、边取边过滤，批大小按 top_k 与过滤命中率动态调整）
与 **ADHOC_BF**（对通过过滤的文档暴力打分，**此模式下结果始终精确**）；
可用 `HYBRID_POLICY`（BATCHES|ADHOC_BF）与 `BATCH_SIZE` 覆盖。
**"自动在 prefilter 与 postfilter 之间切换并可中途改判"
是本章见到的第三类过滤设计**（前两类是 Qdrant/Weaviate 的 filter-aware 图、
Milvus 的 bitset 预过滤）。

**版本与时间旅行：仅有 RDB/AOF 持久化**，未见 MVCC/分支/时间旅行（未确认）。
**多模态：无原生多模态 embedding**，靠 RedisVL/客户端侧生成向量；
存储为内存 HASH/JSON，**非列存**。

**许可证已核实，别写错**：Redis Open Source ≤7.2 = BSD-3-Clause；
**7.4–7.8**（Redis Community Edition）= RSALv2 或 SSPLv1；
**≥8.0.0 = RSALv2 / SSPLv1 / AGPLv3 三选一**。
优点：内存级低延迟、部署简单、向量+结构化过滤+全文同库、
三许可中可选 OSI 认可的 AGPLv3。
缺点：全内存成本高、无对象存储原生/分层存储、无原生多模态与时间旅行、
SVS-VAMANA 高级压缩绑 Intel 硬件、许可史复杂
（7.4 的 RSALv2/SSPLv1 曾引发 Valkey fork 等社区反弹）。

## 27.14 MongoDB Atlas Vector Search

**一句话定位：把向量检索做进文档数据库，代价是 SSPL 与"没有嵌入式形态"。**

**向量检索不是 `mongod` 的内建能力**，而是由独立引擎 **`mongot`**
承担——`mongot` 基于 **Apache Lucene** 构建；`mongod` 通过 **gRPC**
把 `$search` / `$vectorSearch` 代理给 `mongot`，
取回文档 ID 后再自行完成剩余聚合管道。
`mongot` 以 **change stream 异步复制**源数据变更，
索引构建与维护**在事务提交窗口之外**进行，不影响既有事务负载；
代价是索引与源数据之间为**最终一致**。
分片集群中每个分片各持异步本地索引，
`mongos` 做 scatter-gather 并按 `$searchScore` 降序 merge-sort。

索引定义形如 `{"type":"vector","path":…,"numDimensions":…,
"similarity":"euclidean|cosine|dotProduct",
"quantization":"none|scalar|binary","indexingMethod":"hnsw"}`；
`indexingMethod` 默认 **HNSW**，也支持 **flat**；
另有 `{"type":"filter","path":…}` 用于预过滤字段。向量维度上限 **8192**。
**自动量化**：scalar 量化把向量 RAM 成本降到约 **1/3.75**，
binary 量化降到 **1/24**（不是 1/32——HNSW 图结构本身不压缩）；
检索时用二进制近似挑候选，再用落盘的全精度向量重打分。
官方建议向量数**超过 10 万**时启用量化。

**过滤是 prefilter**：过滤字段写进索引定义，查询时在 `$vectorSearch`
阶段内部用 MQL match 表达式过滤，使不匹配的向量**根本不进入比较**。

**版本与时间旅行：官方文档给出的向量检索能力中
未见面向向量索引的版本快照或时间旅行查询语义（未确认）**。
**多模态与列存：BSON 文档模型，非列存**；**不存储原始二进制负载**——
图像/视频字节需另存对象存储，靠应用自行解析路径。

**许可证：服务端（`mongod`/`mongos`）自 2018-10-16 起采用 SSPL v1**；
`mongot` 于 2026-01-15 以 **SSPL** 开放预览。
**SSPL 未经 OSI 认证，与 MIT/Apache 生态不兼容——
这是选型时必须点明的迁移门槛。**

优点：与既有 MongoDB 业务数据同库同模型，过滤字段就是业务字段；
Atlas 全托管省运维；量化 + 重打分兼顾内存与召回。
缺点：**无嵌入式/本地优先形态**（这是与 LanceDB 最根本的部署差异）；
索引与数据最终一致；SSPL 商业使用受限；向量索引无时间旅行；
二进制负载需外置。

## 27.15 Amazon S3 Vectors（AWS）

**一句话定位：AWS 把向量做成了对象存储的一个存储类型。
这是与 LanceDB 最直接的正面竞争——也是最容易被误读的一个。**

AWS 自述为"**第一个原生支持存储与查询向量的云对象存储**"，
定位是 "AI-ready storage" 而非内存向量数据库；
数据落入新的存储类型 **vector bucket**，桶内用 **vector index** 组织，
通过专用 API（`PutVectors` / `GetVectors` / `QueryVectors` /
`DeleteVectors` / `ListVectors`）读写，**无需预置任何基础设施**。

**发布时间已核实**：re:Invent 2025 期间 **2025-12-02 GA**，
预览版为 2025 年 7 月；GA 相比预览版规模放大 **40 倍**、
区域从 5 个扩到 **14 个**。GA 硬指标：**单索引最多 20 亿向量**、
单桶最多 1 万个索引（理论可达 20 万亿向量）、
**高频查询 ~100ms 或更低**、非常规查询 **<1 秒**、
流式单向量写入 **1,000 向量/秒**、**单次查询最多返回 100 条**（GA 时口径）、
每向量最多 50 个元数据键。

**它明确定位为"大型、长期、非常规查询"的向量数据，而非高吞吐场景**
——高 QPS 低延迟 AWS 建议继续用 Amazon OpenSearch Service，
两者构成**分层（tiered）策略**。这是一个罕见的、官方主动承认自己
不适合高 QPS 的例子。

**索引类型：专有实现，未公开算法**（AWS 未披露是否为 HNSW/IVF 或图结构，
**未确认**）。可观察到的区分是**索引模式**：
`ENHANCED` 与 `CLASSIC`——2026-09-30 及之后创建的 vector bucket
默认 `ENHANCED`，之前的为 `CLASSIC`。

**过滤上两种模式语义不同，这是最容易踩的坑。**
**`ENHANCED` 为 prefilter**——先定位满足元数据条件的向量，
再仅在这些向量上做相似度检索，官方称"即使过滤条件只命中极小比例
向量也能保证高召回"；**`CLASSIC` 则把检索与过滤并行评估（tandem）**，
因此在小命中集下**可能返回少于 top-K 条结果**。
`ENHANCED` 下单次过滤最多 **100 个约束**。

**版本与时间旅行：文档中未见面向向量索引的版本或时间旅行 API（未确认）。**
**多模态与列存：非列存，只存向量 + 元数据，不存原始二进制。**
限制：可过滤元数据 ≤ 2 KB、总元数据 ≤ 40 KB、键 ≤ 50 个、
维度 1–4,096、单次 `QueryVectors` 的 top-K 上限 10,000
（官方限制页，2026-10-17 抓取 —— 这个上限比前面 GA 时说的"100 条"晚、也更大）。
多模态场景下原始字节仍留在普通 S3 桶，
S3 Vectors 只负责"指向它们的向量"——**这正是 LanceDB 宣传的对照点**：
向量、元数据、原始 JPEG 内联在同一个 Lance 文件里。

计费上有一条对小请求很不友好的设计：查询按"每索引向量数 ×
平均向量大小（含 key 与可过滤元数据）"分三档计"数据量处理费"，
另按返回 GB 计费，**每条结果至少按 256 字节计**；
**PUT 每次最低按 128KB 计**。

优点：对象存储级弹性与耐久性（S3 的 11 个 9）、真正零运维、
**相比专用向量库声称总成本可降最多 90%（厂商口径）**；
`ENHANCED` 下 prefilter 保证小命中集召回。
缺点：**不面向高 QPS**、延迟承诺（~100ms / <1s）远高于内存型索引、
**算法不透明、无法自托管、无版本/时间旅行、无列存与列裁剪**、
与 AWS 深度绑定、计费维度多且对小请求不利。

## 27.16 DuckDB VSS

**一句话定位：进程内 + 列存 + SQL，形态上离 LanceDB OSS 最近；
但它的 HNSW 还是实验性的。**

DuckDB 的 VSS 是**进程内嵌入式扩展**，官方定位为
"experimental extension"，用 DuckDB 的定长 `ARRAY` 类型承载向量，
只新增一个索引类型 **HNSW**。实现**基于 `usearch` 库**
（便携 C++11、无外部依赖的 HNSW 实现），DuckDB 坦承目前只用了
usearch 的一部分功能。

默认超参：**`ef_construction=128`、`ef_search=64`、
`M=16`、`M0=2*M`**，且 `ef_search` 可在运行时用
`SET hnsw_ef_search` **按连接**覆盖。支持三种度量：
**`l2sq`→`array_distance`（默认）**、`cosine`→`array_cosine_distance`、
`ip`→`array_negative_inner_product`。索引是否命中可用 `EXPLAIN` 里的
**`HNSW_INDEX_SCAN`** 节点确认。

**过滤：无 prefilter 接口。** 文档明确索引"只用于加速形如
`ORDER BY array_distance(...) LIMIT n` 的查询"以及 `min_by(col, arg, n)`。
即带 `WHERE` 的查询里，HNSW 扫描与标量过滤是**两个独立阶段**，
过滤作用在 HNSW 取出的结果之上——**语义上等价于 postfilter**
（**推测**：文档未给出低选择率过滤下的召回行为说明）。

**版本与时间旅行：DuckDB 自身不提供 Lance 式的时间旅行查询（未确认）。**
**但 DuckDB 有官方的 `lance` 核心扩展**，可直接
`SELECT * FROM 'path/to/dataset.lance'`、`COPY … TO … (FORMAT lance)`、
`ATTACH 'dir' AS ns (TYPE lance)`，并可对 `s3://` 路径配 `TYPE lance` secret。
**"版本化/时间旅行"是通过 Lance 格式获得的，而非 VSS 扩展自身——
这一点必须说清楚。**

**多模态与列存：完整的列存/OLAP 引擎**——任意 SQL、
Parquet/CSV/JSON/Iceberg/Delta 互操作、与 pandas/Arrow/Polars 零拷贝。
但 **VSS 只支持 32 位 `FLOAT`**，`DOUBLE[]` 不被索引。
**许可证：DuckDB 本体 MIT，VSS 扩展同为 MIT，`usearch` 亦为宽松许可。**

缺点（**官方自己明列**）：索引**必须整体放进 RAM**，
且**不计入 `memory_limit`**（容易 OOM）；**默认只能在内存库建 HNSW 索引**，
持久化需开实验开关 `hnsw_enable_experimental_persistence`，
而该开关**未实现 WAL 恢复**，崩溃可能丢数据或索引损坏；
**删除只是标记，索引会变陈旧**，需 `PRAGMA hnsw_compact_index` 或重建；
只支持 `FLOAT`；`vss_join` / `vss_match` 宏**不走 HNSW 索引**，是纯暴力。

一个有意思的观察：**DuckDB VSS 与 LanceDB 形态最接近
（嵌入式 + 列存），却被 LanceDB 的对比着陆页漏掉了**——
`/lp/vs-*` 那一组里有 pinecone / elastic / opensearch / milvus-zilliz /
chroma / qdrant / mongodb / turbopuffer / pgvector / weaviate / redis / faiss，
**没有 Vespa、Marqo、DuckDB VSS 的专属页面**。
需要补充的是，**本章新增的几家同样不在那份名单里**：
Azure AI Search、Vertex AI Vector Search、Oracle AI Vector Search、Deep Lake
都没有 `/lp/vs-*` 页面（§27.17–§27.20）。所以"LanceDB 官方把谁当对手"
这份名单本身就是有偏的 —— 它偏向"开源自托管 / 小而美"那一边，
对云厂商的托管服务和最像它的多模态竞品反而没有正面回应。
这可以有两种读法："看不上"或者"没顾上"，留给读者判断。

（顺带一条警告：**Marqo 的开源版已被官方宣告废弃**，2026 年不建议新项目采用。
这家本章单列了一节（§27.22），因为它在很多"向量库排行"里仍被当成活着的开源项目。）

## 27.17 Azure AI Search

**一句话定位：主流云厂商里最完整的托管向量检索，但它是服务，不是库。**

形态上只有 **managed cloud** 一种 —— 没有嵌入式形态、不能自托管，
这是它与 LanceDB 最根本的差别（和 Pinecone 一样，属于"部署形态级"而非"性能级"）。
索引只有两类：**HNSW**（近似）与 **exhaustiveKnn**（精确暴力）。
HNSW 的参数是 `m`（4–10）、`efConstruction`（100–1000）、`efSearch`（100–1000）
与 `metric`（cosine / dotProduct / euclidean / hamming）；
**exhaustiveKnn 不占向量索引配额、数据在查询时动态加载**，
适合 <100 万向量（官方只说它适合"小型到中型数据集"；
"实用在 <5 万"是本教程的经验判断，**未经确认**），也可以对 HNSW 字段在查询时用
`exhaustive: true` 临时切精确——但反过来不行。

**过滤是这一家最容易搞混的地方。** 参数是 `vectorFilterMode`，
取值 `preFilter` 与 `postFilter`：**2023-10-15 前后创建的索引默认 `preFilter`**
（官方文档的原话是 "after approximately October 15, 2023"），
更早创建的默认 `postFilter`（要吃到 preFilter 得重建索引）。
preFilter 在 HNSW 遍历中扩展图、直到凑够 k 个满足条件的候选，
**保证 k 条（若存在）**，官方明说它"almost always slower than postfilter"、
在大数据集上慢几个数量级；命中率极低时官方反而建议 `exhaustive: true`。
这条"默认值随时间改过"的历史，是选型时必须核对自己索引创建时间的原因。

**版本与时间旅行：无。** **多模态与列存：不存原始二进制**，图片字节要另存
Blob 存储；有 integrated vectorization（用 Azure OpenAI 在摄入时算嵌入），
多向量（complex 字段）可做父文档多 chunk。**许可：闭源 Azure 服务**，
按服务计费（具体档位未经确认）。

优点：微软生态内一键接入、与企业身份 / 网络天然一体、
`preFilter` 的召回有保证、有免费层可先试。
缺点：**不可自托管、不可嵌入**；preFilter 慢（换来召回）；无版本 / 无列存 /
不存原始字节；算法与实现对用户不透明。
**什么时候该选它**：整条技术栈已经在 Azure 上、要的是"托管服务 + 企业合规"，
而不是"一个能放进进程的文件格式"。
**你自己该验证什么**：你的索引是哪天创建的（决定 `vectorFilterMode` 默认值），
以及低选择性过滤下 preFilter 的延迟是否可接受。

## 27.18 Google Cloud：Vertex AI Vector Search / AlloyDB / Spanner

**一句话定位：Google 把向量能力铺满了整条产品线，但三个产品的定位完全不同。**

**Vertex AI Vector Search**（原 Matching Engine）是**纯托管的向量检索服务**，
底层是 Google 自研的 **ScaNN**。它是两层结构：先建 index、再部署到
index endpoint 才能查；支持流式更新。适合**纯向量检索、>1000 万向量、低延迟**
的场景，官方口径里它是"向量是唯一需求"时的首选，而不是 AlloyDB。

**AlloyDB** 是 Postgres 兼容的托管库，向量能力由两部分拼成：
定制的 `vector`（pgvector 的 PG 版）加 `alloydb_scann`（ScaNN 索引）。
索引可选 HNSW / IVFFlat / **ScaNN**；`google_ml_integration` 扩展能在 SQL 里
直接调 `embedding('text-embedding-005', ...)` 就地算嵌入。
厂商口径（**未独立验证**）：比标准 PostgreSQL 快 10×、建索引快 10×、
向量查询快 4×、十亿级向量 <25ms。另有 AlloyDB Omni 可自托管（本地 / 边缘 / 多云）。

**Spanner** 走的是另一条路：**用列存引擎做精确 KNN**（官方说可到万亿级），
AlloyDB / Spanner 都能挂 ScaNN 索引。Vertex AI RAG Engine 的
RagManagedDb 后端就是 Spanner —— 零配置但**不透出 SQL**。

**版本与时间旅行：都没有"查询历史向量索引"的语义。** Spanner 有版本保留与
时间戳读（继承自它的多版本模型），那是数据库级能力、不是"checkout 到某个索引版本"。
**多模态与列存**：AlloyDB / Spanner 是行存 OLTP 引擎（Spanner 有列存引擎做分析），
**都不存原始二进制**。**许可：闭源托管**（AlloyDB Omni 可自托管，条款另计）。

优点：和 GCP 数据栈深度集成、ScaNN 是久经考验的算法、AlloyDB 能"SQL + 向量"同库。
缺点：**没有嵌入式形态**；三个产品各有各的接入方式（服务 / 扩展 / 列存引擎），
选错一个就要重做；无版本 / 无列式多模态。
**什么时候该选它**：已经在 GCP 上，要么只用托管向量服务（Vertex），
要么要"Postgres + 向量"（AlloyDB），要么要"Spanner 规模 + 精确 KNN"（Spanner）。
**你自己该验证什么**：你的负载是"纯检索"还是"带事务/JOIN" —— 这决定选
Vertex 还是 AlloyDB；以及 ScaNN 索引的规模上限在你这个维度上的真实表现。

## 27.19 Oracle AI Vector Search

**一句话定位：把向量做进企业级关系库的"收敛数据库"路线，
赢在事务与 SQL 一体，输在没有嵌入式与对象存储原生。**

Oracle Database **23ai** 引入了一等的 `VECTOR` 数据类型
（需要 `COMPATIBLE` 参数 ≥ 23.4.0），向量和业务数据在同一张表、同一个事务里。
索引分两类：**`INMEMORY NEIGHBOR GRAPH`**（HNSW，
参数 `NEIGHBORS` / `EFCONSTRUCTION` / `EFSEARCH`）与
**`NEIGHBOR PARTITIONS`**（IVF，内存可预测）。建索引时用 `TARGET ACCURACY`
或直接调低层参数，距离度量支持 `EUCLIDEAN` / `COSINE_SIMILARITY` / `HAMMING` 等。

**过滤分三档并显式可选**：prefilter、in-filter、postfilter ——
这家把"过滤发生在哪个阶段"做成了可选项而不是隐藏默认值，比 pgvector 早期的
默认 postfilter 要透明。

**混合检索**是它与本教程其他竞品差得最开的地方：用 Oracle Text 的 `CONTAINS()`
做关键词、向量做语义，由 `DBMS_HYBRID_VECTOR.SEARCH` 融合，
融合方法有相对分数融合（RSF）与倒数排名融合（RRF）；
`HYBRID VECTOR INDEX` 与 `DBMS_HYBRID_VECTOR.SEARCH` 是同一批引入的 ——
都随 **23ai 的 Release Update 23.6（2024-10）** 到来，不是 26ai 新增。
SQL 里还有 `VECTOR_DISTANCE()` /
`TO_VECTOR()` / `VECTOR_EMBEDDING()` 与一批简写度量函数。

**版本与时间旅行：向量索引没有快照链**，但整个库继承 Oracle 的 Flashback
（数据库级时间点读，是另一回事）。**多模态与列存：行存为主**
（有 In-Memory Column Store），**不存原始二进制**。**许可：闭源商业数据库**。

优点：与事务 / SQL / 安全（VPD、审计）完全一体，混合检索的融合方法是一等公民，
企业已有 Oracle 时迁移成本低。
缺点：**没有嵌入式形态、对象存储不原生、无列存、不存原始字节**；
向量索引参数与调优绑定 Oracle 版本；许可与成本门槛高。
**什么时候该选它**：组织已经重度依赖 Oracle、且要求"向量与业务数据同事务"。
**你自己该验证什么**：`COMPATIBLE` 是否已到 23.4.0、以及 RSF / RRF 两种融合
在你的评测集上谁更好 —— 这两个都得在你的库上实测。

## 27.20 Deep Lake（Activeloop）

**一句话定位：本章里最接近 LanceDB 的多模态列存竞品 ——
同样是"数据集格式 + 版本学 + 对象存储"，但它没有公开规范，也没有完整列存。**

Deep Lake（Python 包 `deeplake`）既是**嵌入式库**又是托管平台（Activeloop），
数据自包含在对象桶里（S3 / GCS / Azure / 本地），可以流式喂给
PyTorch / TensorFlow，也能当 LLM 的向量库用。它最接近 LanceDB 的地方是
**版本学**：Git 式的 commits（data lineage）、branches、time-travel ——
这是本章除了 LanceDB 与 Turbopuffer 之外**第三家**在"版本 / 分支"轴上真下功夫的。
它的存储格式是自有的 **Tensor Storage Format (TSF)**：分块压缩数组，
支持变长（ragged）张量、`htype`（如 image / class_label）、样本级压缩（JPEG）
与 chunk 级压缩（LZ4），对外是 NumPy 式接口。

**但两处关键差别决定了它替代不了 LanceDB。**
第一，**格式是私有的、没有 Lance 那样的公开规范与 PMC**
（本轮没查到它的公开规范文本、也没查到独立的第三方 reader，**未经确认**）——
ICEBERG 式"多家能各自实现一个 reader"这件事在这里不成立，
版本学是产品能力而不是生态契约。第二，**它不是 SQL / OLAP 引擎**：
chunked tensor 接口能高效流式读训练数据，但"读向量列不碰图片列、
同一份数据既服务 ANN 又服务列式分析"这类能力它没有。
**许可：未经确认**（历史记录里是 MPL-2.0，但本轮未在仓库 LICENSE 中核实）。

优点：多模态 / 训练数据的版本学做得早、与 PyTorch 流式训练贴合、
有托管平台省运维。缺点：格式私有、无列式 OLAP、无公开规范与治理。
**什么时候该选它**：核心需求是"多模态数据集 + Git 式版本学 + 训练流"，
而不需要关系型 SQL 与列式分析。**你自己该验证什么**：许可条款、
TSF 规范是否公开可复现，以及无服务化检索下冷查询的延迟。

## 27.21 sqlite-vec / sqlite-vss

**一句话定位：本章最"嵌入式"的对手。这一章恰恰推崇嵌入式，
所以不能因为 sqlite-vec 默认不做 ANN 就跳过它 —— 它在"零依赖、任何平台"上赢。**

**`sqlite-vec`**（作者 asg017）是一个**纯 C 的 SQLite 扩展**，无外部依赖，
可跑在 Linux / macOS / Windows、浏览器 WASM、树莓派上。
它用 **`vec0` 虚表**存向量，支持 `float[N]` / `int8[N]` / `bit[N]` 三种类型，
查询走 `MATCH` 算子、**必须带 `LIMIT`（或 `k = ?`）**，
支持 `rowid IN (...)` 预过滤以及 metadata / 辅助 / 分区键列。
它默认走**暴力 KNN**：按固定大小 chunk 扫描、用 SIMD（x86 的 AVX、
ARM 的 NEON）加速距离计算，再用双指针合并结果 —— 官方说这换来的是
"没有需要维护或调参的复杂索引结构"，并建议 >100 万 chunk 时另寻 ANN 方案。
**从 v0.1.10-alpha.1（2026-03-31）起它也带了 ANN 索引**
（`rescore` / `ivf` / `DiskANN`），但要靠 `SQLITE_VEC_EXPERIMENTAL_IVF_ENABLE`
这类开关显式打开，默认不开、也还不算稳定。
**许可是 Apache-2.0**，但项目是 pre-v1，会有 breaking change。

**`sqlite-vss`** 是它的前身、基于 Faiss，**作者已在 2024-08 宣告弃用**
（原话是"sqlite-vec is an improvement in nearly every single way"），
**仓库自 2024-05 起就没再更新，但并未归档**
（GitHub 上 `archived: false`），新项目不要再用。

**版本与时间旅行：无**（继承 SQLite 的事务，但没有表快照链）。
**多模态与列存：无列存、不存原始二进制**（BLOB 可存，但没有 blob 的一等公民语义）。

优点：零依赖、体积极小、可嵌进 SQLite 已存在的任何地方（包括 WASM）、
许可干净、检索是精确的。缺点：**默认没有 ANN 索引**（规模上界就是暴力扫描；
v0.1.10-alpha 起的 IVF / DiskANN 仍是实验性的）、
无版本 / 无列存 / 不存原始字节、pre-v1。
**什么时候该选它**：数据本来就在（或适合放进）一个 SQLite 文件、
量在几万到几十万、要求零外部依赖与最小部署足迹。
**你自己该验证什么**：在你的目标平台上是否真能加载扩展（尤其是 WASM / 移动端），
以及暴力扫描在你的数据量下的 P99 延迟。
**与 LanceDB 的关系**：两者都"嵌入式、零服务"，但底座相反 ——
SQLite 是**行存 + 暴力**，LanceDB 是**列存 + ANN + 对象存储原生 + 版本**。

## 27.22 Marqo

**一句话定位：2026 年不该再作为新项目的选型对象 —— 开源版已废弃，公司也转了业务。**

把 Marqo 单列一节，是因为它在很多"向量库排行"里仍被当作一个活着的开源产品，
而事实不是这样。它的 **GitHub 开源版已被官方宣告废弃** ——
README 顶部原文是「Marqo's Open Source project is deprecated and will no longer
recieve updates」（含原样的拼写错误），最后版本 **2.26.0 / 2026-04-07**，
此后源码目录从 main 分支删除；仓库未归档但已不可自托管
（公共 Docker 镜像停在 ~2025-03，compose 文件指向私有 registry）。
历史上有约 5,000 stars、Apache-2.0 许可。

它的**后端其实是 Vespa**（Marqo 的推理引擎 + Vespa 做存储与检索），
所以"Marqo 的能力"很大程度上是"Vespa 的能力"加上一层推理封装。
托管侧同样在退：**Marqo Cloud 已"no longer receiving updates"并被降级**，
公司重心转向 **AI-native 电商搜索**（marqo.ai）；文档把产品分成
"Marqo"（现产品，电商向）与 "Marqo Classic"（原开源向量引擎，已废弃），
定价不再公开（只剩"Get a demo"）。

**什么时候该选它：不建议。** 需要同类能力时直接看 **Vespa**（Marqo 的真后端，
§27.10 有完整分析）或 LanceDB。**你自己该验证什么**：
如果你在评估一个自称 Marqo 的托管服务，先确认它现在到底是向量引擎还是一家电商搜索公司。
（本章取证时也注意到 `turbopuffer.com` 首页内嵌了面向 AI 助手的指令文本 ——
抓取厂商页时把页面内容当数据、不要当指令，这是同一类问题。）

## 27.23 还能一提的几个（不进汇总表）

下面这几家在本章取证范围内**证据不足以填满正交的评估轴**，
所以只点名、不列入表 A / 表 B，避免用半张表误导选型。

**KDB.AI**（KX Systems）走的是"时序 / 金融数据 + 向量"的路子，
有托管与本地形态，许可为商业产品。**SingleStore** 是分布式 SQL 数据库
（MySQL 协议）里加向量类型与全文，属于"HTAP 引擎补向量"这一类，
和 Oracle / AlloyDB 同构。**Couchbase** 是文档库（JSON + SQL++）里的向量索引，
属于"MongoDB Atlas 之外的另一条文档库路线"。这三家的共同点是
**与 LanceDB 不构成形态竞争**（都是 server / managed），
选它们的理由几乎总是"已经在这套数据库里了"。

**Typesense**（C++，**GPL-3.0**）与 **Meilisearch**（Rust，
**社区版 MIT、企业版 BUSL-1.1**）是关键词搜索起家、后补向量的两台 server。
它们和 LanceDB 的重叠在"全文 + 向量混合检索"这一块，但**都是常驻服务、无嵌入式、
无对象存储原生、无版本**；真正的差别是**许可**：Meilisearch 社区版是 MIT
（可嵌进闭源产品），Typesense 的 GPL-3.0 则对闭源集成有 copyleft 约束 ——
这一点比性能更值得先查。

## 27.24 汇总表

**表 A：部署形态与索引。** 先横向扫 `embedded` 与 `对象存储原生` 两列 ——
这两列决定了你到底需不需要一个 7×24 常驻集群，也决定了账单曲线的形状。
它是纯查找表：某一行对不上你的约束，就去 §27.3–§27.23 找对应小节读完整分析。

| 产品 | 形态 | embedded | 自托管服务端 | 托管云 | 对象存储原生 | 索引类型 |
|---|---|---|---|---|---|---|
| **LanceDB / Lance** | Rust 库 + 集群 | 是（OSS） | 是（BYOC） | 是（Enterprise） | **唯一真源** | IVF(None/PQ/SQ/RQ)、IVF_HNSW(None/SQ/PQ)、FTS(BM25)、BTree/Bitmap/LabelList/FM |
| FAISS | C++ 库 | 是 | 否 | 否 | 否 | Flat、HNSW、IVF*、PQ/RQ/SQ、Vamana(SVS)、LSH |
| Milvus | Go + C++ Knowhere | 否（Lite 未确认） | 是（重） | 是 | 是（但需 etcd + WAL） | FLAT、IVF_*、IVF_RABITQ、HNSW*、DISKANN、SCANN、AISAQ、BIN_*、SPARSE_INVERTED_INDEX、GPU_CAGRA |
| Qdrant | Rust server | 否 | 是 | 是 | 否 | **仅 HNSW** + 稀疏倒排 + 四种量化 |
| Weaviate | Go server | 实验性 | 是 | 是 | 否 | HNSW(默认)、flat、dynamic、HFresh(SPFresh) + RQ/PQ/BQ/SQ |
| Chroma | Rust 核心 | 是（Local） | 是 | 是 | 是（分布式模式） | HNSW(单机)、SPANN(Cloud，参数不可调) |
| Pinecone | 闭源 | 否 | 否 | **仅此一种** | 是（slabs） | 专有；document index 融合 dense/sparse/BM25 |
| pgvector | C（PG 扩展） | 随 PG | 是 | 随 PG 托管商 | 否 | IVFFlat、HNSW |
| VectorChord | Rust（PG 扩展） | 随 PG | 是 | 随 PG | 否 | vchordrq(RaBitQ + rerank)；bm25 独立扩展 |
| Vespa | Java + C++ | 否 | 是 | 是 | 否 | HNSW(改良)、BM25、张量多阶段排序 |
| Elasticsearch | Java（Lucene） | 否 | 是 | 是 | 否 | Lucene HNSW、flat、int8/int4/BBQ 量化、`bbq_disk`(企业版) |
| OpenSearch | Java（Lucene 分支） | 否 | 是 | 是 | 否 | hnsw/ivf/flat/disk_ann × faiss/lucene/jvector |
| Turbopuffer | 闭源 | 否 | 否 | 是（+BYOC） | **唯一真源** | 专有 SPFresh 系 + BM25 + SparseKNN + late-interaction |
| Redis | C | 随 Redis | 是 | 是 | 否 | FLAT、HNSW、SVS-VAMANA(8.2+，高级压缩绑 Intel) |
| MongoDB Atlas VS | `mongot`(Lucene) | 否 | SSPL 预览 | 是 | 否 | Lucene HNSW 或 flat + scalar/binary 量化 |
| **Amazon S3 Vectors** | 闭源托管 | 否 | 否 | **仅此一种** | **唯一真源** | 专有，未公开；分 ENHANCED / CLASSIC 两种模式 |
| DuckDB VSS | C++ 扩展 | 是 | 否 | 否 | 经 Lance 扩展间接 | 仅 HNSW（基于 usearch） |
| Azure AI Search | 托管服务 | 否 | 否 | **仅此一种** | 否 | HNSW、exhaustiveKnn（精确） |
| Vertex AI Vector Search | 托管服务 | 否 | 否 | **仅此一种** | 否 | ScaNN（tree-AH 等）；index → endpoint 两层 |
| AlloyDB | Postgres 兼容托管 | 否（Omni 可自托管） | 是（Omni） | 是 | 否 | HNSW、IVFFlat、ScaNN |
| Spanner | 分布式 SQL 托管 | 否 | 否 | 是 | 否 | 列存引擎精确 KNN + ScaNN 索引 |
| Oracle AI Vector Search | Oracle DB 内建 | 否 | 是 | 是 | 否 | HNSW（INMEMORY NEIGHBOR GRAPH）、IVF（NEIGHBOR PARTITIONS） |
| Deep Lake | Python 库 + 托管平台 | 是 | 否 | 是（Activeloop） | **是** | chunked tensor 存储（TSF）；无公开 ANN 族细分 |
| sqlite-vec | C 扩展 | 是 | 否 | 否 | 否 | 默认暴力 KNN（`vec0`，SIMD 加速）；v0.1.10-alpha 起有实验性 IVF / DiskANN |
| Marqo | 已废弃（后端 Vespa） | 否 | 曾可（已不可） | 是（已降级） | 否 | 后端 Vespa：HNSW + BM25 + 张量多阶段排序 |

**表 B：过滤、版本、多模态、许可。** 这张表用来核对四件最容易在选型时被忽略的事：
过滤发生在哪个阶段、有没有真正的版本能力、原始字节存不存、许可能不能闭源集成。

| 产品 | 过滤默认语义 | 版本 / 时间旅行 | 原始字节 + 列存 | 许可证 |
|---|---|---|---|---|
| **LanceDB / Lance** | **prefilter（默认）** | 版本树 + tags + branches + checkout | **blob 内联 + 完整列存** | **Apache-2.0** |
| FAISS | prefilter（仅 IVF，靠 IDSelector 传 id 集合） | 无 | 否 / 否 | MIT |
| Milvus | prefilter（segment 级 bitset）+ 迭代过滤兜底 | 时间戳式历史读（非版本树） | 否 / 否 | Apache-2.0 |
| Qdrant | **filtered-ANN**（filterable HNSW + ACORN + full-scan 回退） | 无（仅 snapshot） | 否 / 否 | Apache-2.0 |
| Weaviate | **filtered-ANN**（sweeping / ACORN 默认 / PathSeer + <15% 退暴力） | 无（仅 Backups） | 否 / 否 | **BSD-3-Clause + `wl/` 企业闭源** |
| Chroma | 倒排索引过滤（元数据默认全建索引） | Collection Forking（仅 Cloud） | 否 / 内部用 Arrow/Parquet | Apache-2.0 |
| Pinecone | 过滤下推（厂商口径，实现未公开） | 无 | 否 / 否 | 闭源 SaaS |
| pgvector | **postfilter（默认）**；0.8.0+ 可选 iterative_scan | 无 | 否 / 否 | PostgreSQL License |
| VectorChord | Prefilter / Prefetch / Rerank 可配 | 无 | 否 / 否 | **AGPLv3 或 ELv2 双许可** |
| Vespa | prefilter 与 postfilter 皆可，+ Acorn 1 | 无（部署包版本 ≠ 数据版本） | 否 / 否 | Apache-2.0 |
| Elasticsearch | **只有 filter 写进 `knn` 子句才是过滤式 ANN** | 无（searchable snapshots + PIT） | 否 / 否 | **ELv2 / SSPL1.0 / AGPLv3 三选一** |
| OpenSearch | **自动**在"精确+预过滤"与"近似+改良后过滤"间切换；3.1+ ACORN | 无（snapshot/restore） | 否 / 否 | Apache-2.0 |
| Turbopuffer | attribute index 感知向量聚类层级 | **namespace branching（COW，$0.032/次）** | 否 / 否 | 闭源 SaaS |
| Redis | **运行时启发式在 BATCHES 与 ADHOC_BF 间自动切换** | 无（RDB/AOF） | 否 / 否 | **≤7.2 BSD-3；7.4–7.8 RSALv2/SSPLv1；≥8.0 三选一** |
| MongoDB Atlas VS | **prefilter**（过滤字段写进索引定义） | 无（未确认） | 否 / 否 | **SSPL v1** |
| Amazon S3 Vectors | **ENHANCED=prefilter；CLASSIC=tandem（可能少于 top-K）** | 无（未确认） | 否 / 否 | 专有 AWS 服务 |
| DuckDB VSS | **postfilter（推测）** | 自身无（经 Lance 扩展获得） | 否 / **完整 OLAP 列存** | MIT |
| Azure AI Search | **`vectorFilterMode`：2023-10-15 后默认 preFilter，之前 postFilter** | 无 | 否 / 否 | 闭源 Azure 服务 |
| Vertex AI Vector Search | 未公开（专有，未确认） | 无 | 否 / 否 | 专有 GCP 服务 |
| AlloyDB | 继承 pgvector 语义 + ScaNN 索引（默认未确认） | 无（PG MVCC / PITR 是库级） | 否 / 否 | 专有 GCP 服务（Omni 另计） |
| Spanner | 精确 KNN 走索引 + ScaNN（未确认） | 无（版本保留 ≠ 索引快照） | 否 / 部分列存引擎 | 专有 GCP 服务 |
| Oracle AI Vector Search | **显式三档 pre / in / postfilter 可选择** | 无（Flashback 是库级） | 否 / 否 | 闭源商业 |
| Deep Lake | 未确认 | **Git 式 commits / branches / time-travel** | **是（chunk 存原始字节）/ 无 OLAP 列存** | 未经确认（历史为 MPL-2.0） |
| sqlite-vec | `rowid IN (...)` 预过滤（默认暴力检索，无 ANN 阶段） | 无 | 否 / 否 | Apache-2.0 |
| Marqo | 由后端 Vespa 决定（见 27.10） | 无 | 否 / 否 | Apache-2.0（项目已废弃） |

**表 B 最值得盯的三列。**
第一列，**过滤默认语义**——postfilter 在低选择性过滤下会静默返回不足
k 条，这是线上事故的高发区，pgvector 与 DuckDB VSS 都在这条线上。
第二列，**版本 / 时间旅行**——这一章 25 个对象里真正有版本树 / 分支的
只有 **LanceDB、Turbopuffer 与 Deep Lake**（后者是数据集版本学、不是表快照链），
Milvus 只能算半个。
第三列，**许可证**。这一列不适合用一句"谁宽松谁不宽松"概括，
因为同一家产品常常是多许可或分版本变化的——**逐个产品的小节里都单独标了**，
做闭源 SaaS 集成前请照着那一节查，别照本章的汇总印象查。几个真实的坑：
Elasticsearch 是 ELv2 / SSPL / AGPLv3 三选一，
VectorChord 是 AGPLv3 / ELv2 双许可（§27.9，它和 pgvector 的许可完全不同），
MongoDB Atlas 与 Redis 的向量能力各自受其主产品的许可约束。
反面也同样容易搞错：**宽松许可不是一个口径**——
pgvector 是 PostgreSQL License、Chroma / Marqo / Qdrant / Milvus 是 Apache-2.0，
它们对闭源集成的约束基本相同，但署名与专利条款不同。

## 27.25 基准数据：哪些能横比，哪些不能

先把规矩立下来：**向量检索的基准是这个领域最容易骗人的部分。**
同一个产品，换个维度、换个 recall 目标、换个 top-K、换个并发数，
QPS 可以差 5 倍以上。**看到只有 QPS 没有 recall 与条件的数字，直接忽略。**

**VectorDBBench（Zilliz 出品，这个领域事实上的横比标准）**
提供可复现工作负载（插入、向量检索、带过滤检索、2026-06 加入的 FTS），
核心指标是 QPS / P99 延迟 / Recall 三元组，对云服务额外给性价比报告。
覆盖 SIFT、GIST、Cohere wikipedia 嵌入、C4 上的 OpenAI 嵌入，
以及 2026-09 起的多模态语料。覆盖后端含 Milvus/Zilliz Cloud、Qdrant、
Pinecone、Weaviate、Elastic、pgvector 系、Redis、MemoryDB、Chroma、
CockroachDB、OpenSearch、MongoDB、TiDB、Vespa、OceanBase、Hologres 等。

**关键事实：LanceDB 不在这个列表里。**
VectorDBBench 的 README 里 **`lancedb` 出现 0 次**；
S3 Vectors 与 DuckDB 同样不在。
**这意味着"LanceDB vs Milvus 谁快"这类问题没有官方的第三方横比数据。**

**ANN-Benchmarks（学术口径，唯一严格可横比的）** 衡量
recall vs Queries-per-second，收录超过 35 个算法
（hnswlib、hnsw(faiss)、faiss-ivf、scann、annoy、vamana(diskann)、
pgvector、qdrant、weaviate、Milvus(Knowhere)、redisearch、opensearchknn、
luceneknn、NGT-*、pynndescent、bruteforce-blas 等），
**最新结果更新于 2025 年 4 月**，维护强度低。
**它的算法列表里同样没有 LanceDB、S3 Vectors、DuckDB VSS。**
而且它评的是**算法/库**，不是**数据库**——不测过滤、不测并发、
不测成本、不测运维。**把它的图当成"选数据库"的依据是误用。**

LanceDB 自家的基准（**全部为厂商口径**）里，有几组值得单独引：

**（a）RaBitQ / IVF_RQ 单核检索**（2026-07，单核、top-k=10、
`nprobes=24`、不做 refine）：见第 12 章那张表，
`IVF_PQ` 74.8% recall / 12.8 ms，`IVF_RQ` 5-bit 96.2% / 4.6 ms。

**（b）100 亿向量分布式检索**（2026-04）：**100 亿条 × 1536 维 float32**，
IVF + RaBitQ，切成 10 个各 10 亿行的 segment 分布在 10 个节点，
每查询探查 20 个 partition、返回 top-100；
端到端 **p50 18 ms / p95 20 ms / p99 21 ms**；分布式索引用 10 个 worker
比单节点快约 5×。**这是 Enterprise 的数字，不是 OSS 的。**

**（c）10B 规模的吞吐**（2026-10）：10B × 768 维索引上，`nprobes=20` 时
32 并发下 951 QPS，提到 256 并发升到 1,066 QPS，中位响应 18.05 ms、
p99 21.61 ms。同篇给的 10M × 768 维对比：5-bit RaBitQ
**1,614 QPS @ 96.2% recall**；PQ96 **1,647 QPS @ 68.6%**；
PQ384 **1,047 QPS @ 91.5%**；SQ **789 QPS @ 95.4%**；
1-bit RQ **1,695 QPS @ 75.0%**。
注意 **(a) 的 QPS 是"每核"、(c) 的是整机，两者不能混用。**

**（d）OpenSearch vs LanceDB 成本对比**（2026-06，COCO 2017 + SigLIP
1152 维嵌入，两侧均用 8-bit 标量量化）：

| 规模 | OpenSearch | LanceDB | 比值 |
|---|---|---|---|
| 28.7 万文档（~46 GB 图像） | ~$125/月 | ~$37/月 | 3.4× |
| 100 万文档（~160 GB） | ~$254/月 | ~$65/月 | 3.9× |
| 1000 万文档（~1.6 TB） | ~$1,020/月 | ~$148/月 | 6.9× |
| 1 亿文档（~16 TB） | ~$3,333/月 | ~$779/月 | 4.3× |

**这是成本模型推算，不是实测吞吐基准**，而且该博文自身有内部不一致
（同一场景给出 ~$37/月 与 $0.04/hr ≈ $26/月 两个数）。
**引用时给量级，不要给精确值。**

**（e）Lance 格式 v2.2 基准**（2026-04）：blob 随机读取比 Parquet 快 75×、
新增列回填比 Parquet 快约 61×、存储比 Parquet 小 52%，
但**在本地 NVMe 上 `scan_project` 比 Parquet 慢 40%**——
**厂商自己在文里承认了这个反向结果。**

**（f）对象存储上的表格式元数据基准**（厂商口径，2026-07）：见第 1 章 §1.3，
同机同负载、Lance vs Delta Lake vs Iceberg，是真正的 apples-to-apples
（但不是向量检索）。（日期只写到月：这份数字同时出现在厂商的月度通讯
与一篇独立博文里，两处的发布日期差了几天，**写到月比编一个精确日子更诚实**。）

**（g）LanceDB Enterprise 官方基准页**：向量检索
**P50 25 ms / P90 26 ms / P99 35 ms**；全文检索 P50 26 ms / P99 42 ms；
带元数据过滤 P50 30 ms / P90 39 ms / P99 50 ms（选择性高的过滤）、
P50 65 ms / P90 76 ms / P99 100 ms（选择性低的过滤）。
数据集是 `dbpedia-entities-openai-1M`（1,000,000 × 1536 维）
与合成数据集（15,000,000 × 256 维），**均注明 "warmed-up cache"（热缓存）**。
官方自己加了免责声明："should be read as representative benchmark results
for a specific test setup, not as universal guarantees"。
这是少见的、给出完整延迟分位数与数据集规格的厂商基准。

**最后一条，也是最容易被忽略的：过滤选择率在几乎所有公开材料里都缺失。**
S3 Vectors 只说 `ENHANCED` 下小命中集保高召回、
`CLASSIC` 下可能返回不足 top-K；MongoDB 的 prefilter 与 DuckDB VSS 的
（推测的）postfilter 语义在低选择率下差异会非常大。
**如果你要选型，唯一可靠的办法是拿自己的数据、自己的过滤条件、
自己的召回目标跑一遍。**

## 27.26 诚实的结论

### LanceDB 明确赢的地方

**第一，嵌入式 + 无服务端是形态级差异，不是性能差异。**
这一章 25 个对象里，真正"进程内 + 零运维 + 无厂商依赖"的只有
LanceDB OSS、FAISS、DuckDB VSS、Chroma Local、sqlite-vec 五个；
而"进程内 + 能持久化 + 带版本 + 带标量索引 + 能放对象存储"的只有
LanceDB 一个。

**第二，对象存储原生是"数据模型级"而非"部署选项级"。**
Turbopuffer 与 S3 Vectors 也主打对象存储，但 LanceDB 的原生程度更深：
数据、索引工件、manifest 都在对象存储，
且**没有必须存在的第三组件**（不需要 etcd / WAL / 控制库）。

**第三，多模态 + 列存在同一个格式里。**
官方 FAQ 最能说明问题："Lance's multimodal nature allows you to store
the raw data, metadata and the embeddings all at once, unlike other
solutions that typically store just the embeddings and metadata"。
落到工程上是：读向量列不碰图片列、blob 惰性读、
一份数据同时服务 ANN 与 OLAP。**这一条把"向量库 + 对象存储 + 特征库"
三件套压成一件，是它最难被复制的能力。**

**第四，版本与分支是表格式内建能力。**
`list_versions` / `checkout` / `restore` / `tags` / `branches` 全线可用。
在这一章里，**把分支式实验做进表格式本身**没有第二家——
Turbopuffer 与 Deep Lake 也有版本/分叉语义（§27.12、§27.20），
但它们要么是数据库层的命名空间、要么是数据集层的版本学，
不是"同一张表的快照链上开分支"这个形状。

**第五，成本曲线形状更好（在低 QPS + 大数据场景）。**
差异来源是结构性的：OpenSearch 的算力随**内存里索引大小**阶梯式上涨，
LanceDB 的算力随 **QPS** 走。

**第六，许可最干净。** Apache-2.0，无 `wl/` 式企业闭源目录、
无 AGPL/ELv2 双许可、无 SSPL。
**如果你要做闭源 SaaS 或嵌进商业产品，这一条直接刷掉若干候选。**

### LanceDB 明确输的地方

**第一，生态成熟度与社区体量是硬伤。**
11,684 stars vs Milvus 46,401、Qdrant 35,101、Chroma 29,511、
pgvector 23,386。差 2–4 倍不是"略少一点"，
它直接体现为答案更少、教程更少、招人更难。

**第二，Lance 格式在更广阔生态里的读者还很少。**
任何一个引擎、任何一种语言都有 Parquet reader；
Lance reader 主要由 Lance 团队自己维护。
官方给的可对接生态是 PyTorch / Ray / Spark / Trino / DuckDB / Polars
——这个列表在变长，但它远不是"万物皆可读 Parquet"那种地位。
**这是本章最被低估的迁移成本（推测）。**

**第三，单机规模上界低。** OSS 的舒适区是"单节点百万向量"，
亿级以上推荐 Enterprise。**你要么接受百万级天花板，要么进商业版。**
对照地，Milvus / Qdrant / Weaviate 的开源自托管**没有 LanceDB OSS 这种
"官方明写的百万级天花板"**；但要加一条限定：**这并不等于它们在单机上都能
轻松跑十亿级** —— Qdrant、Weaviate 在很多负载上实际也是"单节点扩容 + 分片"，
只是它们没有像 LanceDB 那样把上限写成一句官方建议。
**"没有公开天花板"和"没有天花板"是两回事。**

**第四，索引构建的内存与时间特征对它不利。**
LanceDB 的索引是 IVF 系，建索引需要 k-means 训练
（`sample_rate` 默认 256、`max_iterations` 默认 50），
`num_partitions` / `target_partition_size` / `num_sub_vectors` 需要人工调。
相比之下 HNSW 系（Qdrant / Weaviate / pgvector）无需训练步、
**可以空表建索引**。"建索引要不要调参"是运维体验上的真实差距。

**第五，过滤负载下 HNSW 系索引的延迟方差更大。**
这是 **LanceDB 官方自己写的**："In filtered workloads,
HNSW-backed IVF indexes such as `IVF_HNSW_FLAT` and `IVF_HNSW_SQ`
can show higher latency variance"。
而 filtered-ANN 恰恰是 Qdrant 与 Weaviate 的主场。

**第六，托管区域选项更少、没有免费试用档。**
Enterprise 支持 AWS/GCP/Azure、managed 或 BYOC，
但**具体 region 列表、SLA、价格页面本轮未取到**，标**未经确认**。
`lancedb.com/pricing` 实测**根本不是一个定价页**：
它以 200 结尾，但最终落在了 `/contact`——是一次重定向，不是渲染问题
（`curl -L` 的 `url_effective` 是 `https://www.lancedb.com/contact`）。
这一节原来写的是"返回 200 但正文由前端渲染，curl 拿不到内容"，
**那个诊断是错的**：拿不到内容不是因为前端渲染，是因为它压根没有价格内容。
两者看起来都是"curl 拿不到"，但含义完全不同——前者意味着
"内容在，只是取不到"，后者意味着"**这页不存在**"。
对"有没有公开定价"这个问题，答案是后者，而这本身就是一条选型信息。
对比 Pinecone / Zilliz / Qdrant Cloud 都有公开的区域与计费页面。
而且 LanceDB 的托管形态是"联系销售"，
**没有免费托管的公共 playground**——这让"先试后买"的路径变长。

**第七，横向扩展是"商业版能力 + 部分仍在路上"。**
分布式索引与查询执行在 Enterprise 中已交付
（10B 规模的分段索引 + fan-out 查询有公开结果），
但 Enterprise 概览页上 **"Distributed search"、"Distributed indexing and
compaction"、"Enterprise training cache" 三项仍挂着 `Coming soon` 徽标**。
读法是：这些是对已有能力的增量增强，不是从零开始。
但"哪一档现在可用"在公开文档里说不清楚，选型时值得直接向厂商确认。

### 一个决策顺序（以及什么时候该选别人）

与其问"谁最快"，不如按下面的顺序自问 —— 每一步先排除掉一批。

**第一步：你要的是"进程内的库"还是"常驻的服务"？**
如果答案必须是"库"（不能起服务、不能有常驻进程），候选立刻缩到
LanceDB OSS、FAISS、DuckDB VSS、Chroma Local、sqlite-vec 五个，
其余全部出局，无论它们性能多好。这一步把"形态"和"性能"分开，
也是本章开头那句话的落地。

**第二步：数据要有版本 / 分支吗？** 要，那么这一章里只剩 LanceDB、
Turbopuffer、Deep Lake 三家（Milvus 只能算半个）。

**第三步：原始字节（图片 / 视频）要不要和向量放在一起？** 要，
那么排掉所有"只存向量 + 元数据"的对象（Pinecone、S3 Vectors、
MongoDB Atlas、Azure AI Search 等），剩 LanceDB 与 Deep Lake 最接近。

**第四步：要不要完整列式 OLAP？** 要，Deep Lake 出局，只剩 LanceDB
（或走"向量库 + DuckDB"的两段式）。

**四个条件同时成立 —— 既要向量、又要列存分析、又要原始字节、又要版本复现、
又不想跑常驻服务 —— 这一章覆盖的对象里目前只有 LanceDB 一个能全中。
这是它唯一真正无可替代的位置，也是它最应该被评估的位置。**

反过来，某一步答"否"时别人就更合适，把它们列出来：

**已经在重度使用 Postgres、要事务 + JOIN + 备份一起** → pgvector / VectorChord
（与 SQL / 事务天然一体；注意两者许可不同：pgvector 是 PostgreSQL License，
VectorChord 是 AGPLv3 / ELv2 双许可，见 §27.9）。**需要开源自托管的十亿级集群** →
Milvus / Qdrant / Weaviate（开源版没有 LanceDB OSS 那种官方明写的单机百万级上限，
但"没有公开上限"不等于"单机随便上十亿"）。**过滤是核心负载（多字段组合、低选择性）**
→ Qdrant、Weaviate（filter-aware 图 + cardinality 估算 + 多策略回退是真本事）。
**完全不想碰运维** → Pinecone / Zilliz Cloud / 各家托管（无服务端化最彻底）。
**只是需要一个 ANN 内核、自己造数据库** → FAISS（MIT、算法最全、零成本）。
**在 DuckDB 里做小规模向量检索** → DuckDB VSS（与 OLAP 同进程，零额外基础设施）。
**已经有一套 Elasticsearch / OpenSearch** → 它的 kNN（复用现有集群与运维）。
**技术栈在 Azure / GCP / Oracle 上、要托管服务** → 各家的向量检索
（§27.17–§27.19）。**数据本来就在 SQLite 里、量不大、要零依赖** → sqlite-vec。
**要闭源 SaaS、必须避开 copyleft / SSPL** → LanceDB、Qdrant、OpenSearch、
FAISS、sqlite-vec（许可干净的白名单很短）。

（以上归纳为本教程的判断，非厂商口径。）

### 每个竞品：你该自己验证什么

前面每一节的结论都建立在本轮取证的事实上，但**每一家的关键假设都该你在自己的
数据上复核一遍**。下面这份清单是本章的"验收动作"，一行一家。

**FAISS**：你的规模下 `nprobe` 与召回的关系，以及没有过滤加速时 prefilter 的代价。
**Milvus**：三件套（etcd + 对象存储 + WAL）的真实运维成本，以及时间戳式历史读能追溯多远。
**Qdrant**：payload index 是否在灌数据前建好，以及 full-scan 回退的触发点。
**Weaviate**：`wl/` 企业功能在你场景里是否必需，以及 filter-aware 边的索引延迟代价。
**Chroma**：单机 HNSW 的上界（官方称 <1000 万），以及 Cloud 的 SPANN 参数不可调是否致命。
**Pinecone**：不可自托管是否可接受，以及过滤下推的真实召回（实现未公开）。
**pgvector**：`hnsw.iterative_scan` 是否真开了（升级 ≠ 默认开），以及低选择性过滤的召回。
**VectorChord**：AGPLv3 / ELv2 双许可对你的商业模型意味着什么。
**Vespa**：application package / schema / rank-profile 的学习成本，以及你需不需要 ML 推理。
**Elasticsearch**：filter 是否写进了 `knn` 子句（这决定是不是过滤式 ANN），以及许可选哪支。
**OpenSearch**：knn 插件的版本间行为差异，以及引擎选 lucene 还是 faiss / jvector。
**Turbopuffer**：写延迟与冷查询的 p90 是否可接受，以及 branching 的成本模型。
**Redis**：SVS-VAMANA 的高级压缩在你的 CPU 上是否退化（非 Intel 平台会变基础 SQ）。
**MongoDB Atlas**：最终一致窗口能否接受，以及 SSPL 的合规边界。
**Amazon S3 Vectors**：`ENHANCED` 还是 `CLASSIC`（决定过滤语义），以及每结果 256 字节起计的费用。
**DuckDB VSS**：索引是否整体进内存且不计入 `memory_limit`（这是 OOM 的来源）。
**Azure AI Search**：索引创建日期（决定 `vectorFilterMode` 默认），以及 preFilter 的延迟。
**Vertex AI Vector Search / AlloyDB / Spanner**：负载是纯检索还是带事务，据此选哪一个。
**Oracle AI Vector Search**：`COMPATIBLE` 是否 ≥ 23.4.0，以及 RSF / RRF 哪个在评测集上更好。
**Deep Lake**：许可条款与 TSF 规范是否公开可复现，以及冷查询延迟。
**sqlite-vec**：目标平台能否加载扩展，以及暴力扫描在你数据量下的 P99。
**Marqo**：它现在到底是向量引擎还是电商搜索公司（答案决定要不要继续看）。

对比到此为止。这一章给的是"现在该怎么选"，但选型的另一半是"这套技术会往哪走、
版本会不会变脸"—— 下一章（第 28 章）讲三层版本号、格式兼容事故与升级策略，
那是把这里的判断落到长期项目上的前提。
