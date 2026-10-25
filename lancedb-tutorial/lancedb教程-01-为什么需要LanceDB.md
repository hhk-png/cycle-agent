# 第 1 章　为什么需要 LanceDB

这一章回答一个问题：**向量检索这件事，明明已经有 FAISS、Milvus、Qdrant、
pgvector 了，为什么还要有 LanceDB？**

答案不是"它更快"。LanceDB 在绝对延迟上打不过内存型的 Qdrant，在亿级单表的
高 QPS 服务化上也打不过 Milvus 集群。它的位置在别处：**把"向量检索"和
"多模态列存 + 版本管理"放进同一个文件和同一个进程里**。要讲清楚这件事，
得先看清这个领域的形状。

## 1.1 向量检索的三条技术路线

把市面上所有做相似度检索的系统摊开，会自然地分成三类。分类的依据不是
"谁写得更好"，而是**它把自己放在系统的哪一层**。

**第一条：专用 ANN 库（FAISS、hnswlib、usearch、ScaNN 内核）。**
这类东西是**算法内核**，不是数据库。FAISS 的官方自述是
"a library for efficient similarity search and clustering of dense vectors"，
C++ 实现加一层 numpy 包装，唯一必需依赖是 BLAS。
它只接受 `float32` 矩阵，**不支持字符串 id**，不存任何标量字段，
没有事务、没有版本、没有并发控制。FAISS 的 `IndexFlat*` 就是一个
`ntotal × code_size` 字节的裸数组；`IndexIVF*` 是 k-means 分桶；
`IndexHNSWFlat` 是在 Flat 之上叠一张图，而且**不支持删点**——
官方原文的理由是 "This would destroy the graph structure"。

它的边界很清楚：**它是一个可以嵌进你自己数据库的引擎**，不是数据库本身。
拿它做标量过滤，你得自己另建一套存储把 id 喂给 `IDSelector`；
持久化只有整索引导出的 `write_index` / `read_index`，
没有快照链、没有按时间戳读。用 FAISS 的代价，是这些你都要自己造。

**第二条：服务化的向量数据库（Milvus/Zilliz、Qdrant、Weaviate、Pinecone、
Vespa、Elasticsearch/OpenSearch）。**
这类系统把"检索"做成了一个**独立进程或独立集群**，对外是 HTTP/gRPC。
它们买到的东西是实打实的：多租户、集中鉴权、连接池、在线滚动升级、
水平扩展、成熟的过滤下推。

代价同样实打实。第一是**一个 7×24 常驻集群**，而它的成本与你的向量条数
基本无关、与"索引必须常驻内存"强相关。Qdrant 的索引就是 HNSW，官方文档
写得很直白：默认 `m: 16`、`ef_construct: 100`；Weaviate 官方表格给的是
1M 向量节点约 2–12 GB、1 亿向量节点 200–1200 GB，并直言
"memory requirements … can quickly become a bottleneck"。
Milvus 官方拿 1B SIFT 举例：`IVF_FLAT` 索引约 470 GB，换成 `IVF_SQ8`
仍要 140 GB。第二是**运维半径**：Milvus 的最小可用集群是 etcd + 对象存储 +
WAL 三件套；Qdrant 自托管要自己管副本和 shard 迁移。
第三是**形态被锁死**：服务端形态意味着你做不出一个"双击就能跑的桌面应用"。

**第三条：嵌入式（LanceDB OSS、Chroma Local、DuckDB VSS）。**
进程内的库，`pip install` 之后在自己进程里跑。没有空转成本，
交付形态自由，可以塞进 CLI、桌面应用、边缘函数。

代价是服务端那边买到的能力这边都没有：没有连接池、没有多租户、
没有集中式鉴权、没有在线升级，客户端进程崩了就是崩了。
更麻烦的是**扩展上界明确**：LanceDB OSS 的单进程和你的应用抢同一个
CPU 池，而且**没有内置缓存**——官方原话是
"LanceDB OSS has no built-in cache. Every read repeats the same object-store
round trip and pays the same latency penalty."

三者的能力边界，差别集中在五件事上。

**交付形态与 CRUD。** 专用 ANN 库和服务化数据库的形态正好相反：前者是进程内库、
CRUD 和持久化都要自己造，后者是独立服务或托管云，这些能力自带。
嵌入式检索库（LanceDB OSS、Chroma Local、DuckDB VSS）跟专用库一样是进程内库，
但 CRUD 与持久化是自带的——存的不再是一个裸索引，而是一张表。

**标量过滤。** 专用 ANN 库没有标量过滤的概念，过滤靠调用方把候选 id 集合传进来；
服务化数据库和嵌入式库都把它当一等能力，区别只在预过滤策略的成熟度。

**版本、原始字节与列式分析。** 这三项是分水岭。服务化系统绝大多数既没有版本与
时间旅行，也不能存原始字节，更不能做列式分析；LanceDB 三样都有——分支 + tag +
checkout、blob 列（小到几十 KB 的内联进主文件、大到 GB 级的拆进边车文件，
四档自动挑）、同一张表既能 ANN 又能 OLAP 扫描。专用 ANN 库则三样全无。

（这里我第一版写的是"blob 列内联"，**只说对了一半**。内联只是四档里最小的
那一档，而真正让 blob 区别于"一个大字段"的恰恰是**外置**——把 20 MB 的
图片从主文件里挪走，行里只留偏移。§1.3 讲"超大对象"时会展开成四档，
第 3 章 §3.5 和第 21 章给它完整版本。）

**运维负担与并发写。** 专用库和嵌入式库都没有运维负担，但也都没有内建的并发控制；
LanceDB 的模型是"单写者 + 多读者"，在对象存储上靠原生原子写把并发写变得可用。
服务化系统自带多租户与并发控制，代价是一整套必须常驻的集群。

**规模上界。** 专用库的上界取决于你自己在它外面搭了什么；服务化系统的上界就是
集群规模；LanceDB OSS 的官方口径是单节点百万向量级，再往上要走 Enterprise。

## 1.2 LanceDB 的定位：把"无服务器向量数据库"这个标签扔掉

写教程的第一件事，是纠正一个流行了三年、现在**已经过时**的说法。

2023 年 5 月 5 日，LanceDB 在 YC 的发布帖标题是
"LanceDB 🍢 - serverless, low-latency vector database for AI applications"，
tagline 是"the easy-to-use and developer-friendly alternative to Pinecone"。
那是它最初的形象：一个便宜的、本地的、嵌进去的 Pinecone 替代品。

到 2026 年，这个说法已经不成立了。官方文档的首页第一句现在是：

```
Multimodal lakehouse for AI.
```

docs.lancedb.com 上有一节专门澄清这件事，措辞是
"Despite its name, LanceDB is not a 'database' in the traditional sense.
It is a **Multimodal Lakehouse** built on Lance tables plus a catalog
abstraction."。另一处更细："LanceDB exposes a catalog-level abstraction over
the Lance table format, via a *namespace spec*."

所以正确的分层链是这样的（这一章之后的所有内容，都挂在这条链上）：

```
Lance 文件格式 (file format)     列存 + blobs + 快速随机访问
        ↓
Lance 表格式 (table format)      manifest / fragments / 删除文件 / 索引
        ↓
LanceDB 目录层 (catalog layer)   namespace spec、表名解析
        ↓
LanceDB 查询引擎                 建在 Lance 之上（DataFusion 系）
        ↓
SDK 层                           Python / TypeScript / Rust / Java(REST)
```

**这五层里支持度落差最大的是最上面的 SDK 层，选型时也最容易被忽略。**
Python 是能力最全的一层——本教程从头到尾对照的 `lancedb 0.40.0` 就是它，
建表、索引、重排器、embedding 函数一应俱全。TypeScript/JS 与 Rust 覆盖了核心 API
（建表、检索、过滤、混合检索），但高级特性有缺口：第 26 章点出了一处具体的落差，
TypeScript 与 Rust SDK 只暴露通用的 `Reranker` 接口，想在 TS 或 Rust 里用 Cohere、
ColBERT 这类具体重排器，得自己实现。Rust 是原生层，Python 与 TypeScript 的包里
都打包了 Rust 编译出的原生模块，所以三者的版本号是各自独立发布的（见第 28 章）。
Java 侧没有随包发布的原生 SDK，接入方式是走 REST 的 catalog（namespace spec），
这也是它在上面那条链里被标成 `Java(REST)` 的原因——功能面最窄，也最依赖服务端形态。

Lance 和 LanceDB 的关系，官方 FAQ 里有一条专门回答，值得整句记住：

Lance 是面向多模态 AI 的现代湖仓格式，适合做搜索引擎、特征库，
以及需要高性能 IO 与 shuffle 的大规模 ML 训练任务的底座；
LanceDB 则是建在 Lance 之上的多模态湖仓，利用底层的存储格式构造
高效的**磁盘索引**，支撑从 RAG 到问答机器人到推荐系统的语义检索。

**"serverless" 这个词现在只活在 vectordb-recipes 仓库的 README 里**
（"a free, open-source, serverless vectorDB that requires no setup"），
当前 docs.lancedb.com 的正文**不用它**来定义产品形态。
GitHub 上的 README 至今还写着 "Cloud and Enterprise"。
**以下是一组截至 2026-10-17 的观察**：`lancedb.com/cloud` 返回 404、
`/pricing` 不是定价页而是"联系我们"、docs.lancedb.com 的 `llms.txt`
索引里**没有任何 Cloud 页面**。**目前存在的形态只有两个：OSS（嵌入式）与
Enterprise（私有云 / BYOC）**。任何"LanceDB Cloud GA"的说法都是过期情报。

这个转向有一个可验证的时间锚点：2025-06-24 官方博客宣布 $30M Series A，
标题就是 "…to Build the Multimodal Lakehouse"。公司的法定实体名是
**Eto Labs, Inc.**（dba LanceDB），2022 年 YC W22 批次。
Lance 格式仓库搬过两次家——`eto-ai/lance` → `lancedb/lance` →
**`lance-format/lance`**（2025-11-18 宣布）——**网上流传的教程链接大部分
因此 404**，找源码时别找错组织。

**许可与商业形态也是两套，必须分开看。** LanceDB OSS 是 **Apache-2.0**——
无企业闭源目录、无双许可，这是它和 Weaviate（`wl/` 目录另行授权）、
VectorChord（AGPLv3 / ELv2 双许可）在许可条款上最实在的差别。Enterprise
是另一套闭源授权，交付形态是私有云 / BYOC，官网对外只有"联系销售"这一个
入口，公开的只有托管的 TCO 对比（见 §1.4）。还有一层容易忽略的分裂：
**格式和产品是两套治理。** Lance 格式在 2025-11-18 之后走社区的
Contributors → Maintainers → PMC，对标 Apache 的 "The Apache Way"；
而 LanceDB OSS 至今由公司主导，同一篇公告的原文写得很直白，
"it doesn't, at least not yet"。所以"该把谁当长期依赖"这个问题，
答案取决于你依赖的是格式还是这个产品。

## 1.3 为什么不直接用 Parquet

这是所有 LanceDB 介绍里最常被一句"为 AI 优化"带过去的问题。
官方 FAQ 给了四条理由，这一节只记它们的**定位层面**——每条理由背后的
字节级机制放在第 3 章，那里才讲得清。

**第一条，随机访问的代价。** Parquet 为分析型的全表扫描设计，
读取单位是一整块；而向量检索是**点查**——ANN 返回一批行号，
你要把那批行的原始列取回来。官方的说法是
"Our benchmarks show that Lance is up to **1000x faster than
Parquet for random access**"。在 Lance v2.2 的基准里，这个差距被具体化为
一件事：**在 LAION-10M 上随机取 1,000 张图，Parquet 要 889 秒，
Lance v2.2 是个位数秒**（官方口径，2026-04-07）。

**第二条，嵌套数据的表达力。** 一条多模态数据可能是
"视频帧 + 每帧的多个检测框 + 每个框的向量 + 框的标签列表"。
Parquet 能表达它，但读取时很难只取其中一个子列。
Lance 的做法是把嵌套**结构化解开**成若干平列，
每列独立编码、独立压缩、独立读取——这一层的细节同样在第 3 章。

**第三条，超大对象。** 一张 4K 图、一段 30 秒音频、一个 PDF——
行里塞一个 20 MB 的值，列里所有行的读取都会被它拖累。
Lance 把 blob 外置成独立 buffer，行里只留偏移和长度。
Lance 2.2 的 Blob V2 更进一步，给了四档存储语义
（Inline / Packed / Dedicated / External），按负载自动适配。

**第四条，格式的可演进性。** 字段要有稳定 id，因为数据文件要能在
列增删之后仍然可读；元数据要能被快速解析，因为它每次查询都要读；
统计信息要能和数据分离，这样剪枝才能零 I/O。
官方把这条叫做 **zero-copy data evolution**：加一列派生特征时，
"Only new data is written; expensive existing data (like images/videos)
remain untouched"。同一份基准里逐项的数字是：**FineWeb 一千万行加一列，
Lance 13 ms、Parquet 520 秒**——这一对差的是**四个数量级**；
**跨全部数据集平均下来是约 61 倍**（第 21 章 §21.4 有这张表的完整数据）。
两个数别混用：61 倍是跨数据集的聚合值，不是 FineWeb 这一项的比值。

但这四条理由是**单向**的，不能读成"Lance 全面碾压 Parquet"。
同一份 v2.2 基准里官方自己写了反向结果：**在本地 NVMe 上
`scan_project` 比 Parquet 慢 40%**，理由是本地字典解码的 CPU 开销。
原文承认 "Parquet remains highly competitive" for narrow scalar scans
on local storage。把各项并排看，胜负分明但不对称：Lance 赢在
**随机点查少量行**（最多 1000x）、**取 blob**（约 75x）、
**加列 / 改 schema 回填**（约 61x）、**存储体积**（v2.2 比 Parquet 小 52%）
和**摄入吞吐**（快约 70%）；Parquet 只在**本地窄列扫描（scan_project）**
这一项上领先，快 40%；而**高选择率过滤扫描**（本地）Lance 快 2.8x，
**低选择率过滤扫描**（本地）打平。

一句话概括：**Parquet 为"扫全表"优化，Lance 为"点查 + 多模态 + 频繁演进"优化。**

还有一组数据不测向量检索，但测的正是 LanceDB 与 S3 Vectors 共同所在的
那一层——**对象存储上表格式的提交与加载**。官方的基准
（EC2 c7i.48xlarge、us-east-1b、同区 S3 桶、10,000 次顺序 append commit；
厂商口径，2026-07）测出来的形状是这样的：10,000 次提交的平均延迟，
**Lance 140 ms 对 Delta Lake 534 ms 对 Iceberg 457 ms**——
同一批机器、同一个区域、同一个负载，差异全在表格式怎么提交。

更值得记的是另外两列，因为它们不是延迟：**平均活跃元数据体积
Lance 0.78 MiB 对 Iceberg 42+ MiB**（差五十倍以上），
以及**200 个并发写入者时的失败率 Lance 16% 对 Iceberg 88% / 89–94%**。

最后那一列是**同机、同负载、同区域下的并发提交失败率**——
它不是性能而是可用性，也是"元数据跳数越少越好"这句话最具体的形状。
（第 29 章 §29.6.7 从"这个赛道没有第三方基准"的角度再讲了一次这组数字，
并说明了为什么厂商自测的这组数据即便口径偏厂商、仍然值得看。）

## 1.4 "无服务器"到底意味着什么，以及它的代价

把"embedded / 无服务端 / 对象存储原生"拆开，其实是**三件互相独立的事**，
各自算账。

**第一件：嵌入式，省掉一个 7×24 常驻进程。**
买到的是成本曲线的形状不同——常驻集群的成本与"索引常驻内存"强相关，
嵌入式库没有空转成本，进程不在就没有账单。以及在 287K 条这个量级上，
官方对比给的是 OpenSearch 约 $125/月 vs LanceDB 约 $37/月；
到 1 亿条是 $3,333/月 vs $779/月（厂商口径，2026-06-15，
us-east-1 on-demand，两侧都把图片放 S3，只算稳态查询基础设施；
该博文自身有内部不一致，1M 那一格出现了两个数，引用时给量级而非精确值）。
以及交付形态自由：可以塞进桌面应用、CLI、WASM 边缘函数。

代价是**没有服务端就没有服务端的一切**：没有连接池、没有多租户、
没有集中式鉴权、没有在线滚动升级；并发写要自己面对。
Lance 表是"单写者 + 多读者"模型（每次写入产生新版本 manifest）。
官方文档说 **S3 与 S3 Express 现在支持原生原子写，所以同一张表上的并发写者
开箱可用、不需要外部 commit 协调器**；但同一份 FAQ 又留了一句
"TOO MANY CONCURRENT WRITERS can lead to failing writes as there is a
LIMITED NUMBER OF TIMES A WRITER RETRIES A COMMIT"。
**非 S3 后端上的并发行为本轮未确认。**

**第二件：对象存储原生，指的是"唯一真源"在对象存储上。**
计算节点可以随时被替换、扩缩、销毁，数据与索引工件都在 S3/GCS/Azure Blob。
买到的是可以 scale to zero（官方对比表原文是 "Can scale to zero —
Yes, queryable from cold S3"）、计算与存储分开演进、
备份就是"已经在那儿"。

**第三件：计算存储分离，代价全部落在延迟和 I/O 账单上。**
官方在 OSS vs Enterprise 的对照表里把这两组数字放在一起。
这张表把同一套 Lance 列存格式在两种部署下的四个维度拉齐对比，
读的时候重点看**对象存储上的延迟**与**吞吐**两行——它们决定了这套架构的
能力边界：

| 维度 | LanceDB OSS | LanceDB Enterprise |
|---|---|---|
| 部署模式 | 单进程 | 分布式集群 |
| **对象存储上的延迟** | **500–1000 ms** | **50–200 ms** |
| 吞吐 | 10–50 QPS | 最高 10,000 QPS |
| 缓存 | 无 | 分布式 NVMe 缓存 |
| 索引与 compaction | 手工 | 平台托管 |
| 数据格式 | 同一套 Lance 列存格式，**两边自由迁移、无需转换** |

这组数字要读懂三件事。第一，**500–1000 ms 是"对象存储上的冷读"**，
不是向量检索本身慢——它主要花在加载索引分片和取原始列上。
第二，**Enterprise 的 50–200 ms 是靠 SSD 缓存买来的**，
不是靠更快的算法。第三，**OSS 的 10–50 QPS 是单进程的上界**，
和向量条数关系不大，和"一次查询要发多少个对象存储请求"关系很大。
**这同一组数字在第 6 章会从存储层角度再讲一次**——那里会补上五档存储
介质的延迟阶梯和"读一致性窗口"这件事，本章只把它当作定位章的账记下来。

对象存储的 I/O 账单也是真实的：S3 Standard 是 $0.0004/1000 次 GET。
官方估算 10 QPS 下一个月约 26M 次 GET ≈ **$10.40/月，
且随 QPS 线性增长、与语料规模无关**。
另外官方自己在 1,000,000 IOPS 那篇里坦承：
"when searches are cold, the latency is most likely going to be dominated
by the index loading time"。

**对象存储的 list 语义**是一个容易被忽略的坑。
Lance v1 的 manifest 路径命名会让"打开一个有大量版本的表"的耗时
被 listing 主导；官方为此加了 `new_table_enable_v2_manifest_paths`
（要求 LanceDB ≥ 0.10.0 才能读），并把它的作用直接写成
"matters most on object stores, where **opening a table with many versions
is dominated by listing cost**"。同时每个版本都带一份元数据，
官方提醒 **"they do have 100x the metadata overhead of a single version,
which can result in slower queries"**——所以版本不是免费的，
promotion 需要 `optimize()` 与 cleanup 来配合。

## 1.5 什么时候该用它，什么时候不该用

把上面所有约束条件摆齐，判断就变得机械了。

**该用 LanceDB 的场景，四个条件同时成立的时候：**
既要做向量检索、又要在同一张表上做列式分析（OLAP 扫描 / 聚合 / 导出训练集）；
要存原始图片、视频、音频字节而不只是向量和元数据；
要"复现某次实验用的是哪一版数据"这种版本或分支语义；
并且不想为它跑一个常驻服务。

这四条同时成立时，本章涉及的十几个同类系统里目前只有 LanceDB 能全中
（这是本教程的归纳，不是厂商口径）。

**不该用的场景，同样清晰：**

- **要内存级绝对延迟（个位数毫秒 p99）、且数据能全部装进内存** ——
  对象存储原生的架构在这个方向上没有优势，Qdrant / Redis 这类全内存
  系统是更直接的选择。
- **要开源自托管的十亿级单表集群** —— LanceDB OSS 的官方上限是
  "comfortably handle millions of vectors on a single node"，
  亿级以上推荐 Enterprise。想开源跑十亿级得看 Milvus / Qdrant / Weaviate。
- **过滤是核心负载，而且过滤条件复杂、选择性低** ——
  filtered-ANN 是 Qdrant（filterable HNSW + ACORN）与 Weaviate
  （sweeping / ACORN / PathSeer）的主场；LanceDB 官方自己承认
  "In filtered workloads, HNSW-backed IVF indexes … can show
  **higher latency variance**"。
- **只想要一个 ANN 内核、其余自己造** —— FAISS 是 MIT、算法最全、零成本。
- **已经在跑 Elasticsearch / OpenSearch 集群** —— 复用现有集群的 kNN，
  比再引入一个格式划算。
- **需要"完全不想碰运维"的托管体验** —— Pinecone / Zilliz Cloud 有免费档，
  LanceDB 的托管形态目前是"联系销售"。

**"已经在重度使用 Postgres"这一类没写进上面的清单，但要单独说。**
pgvector 是 Postgres 扩展，直接挂在 Postgres 存储引擎上（四种向量类型
`vector` / `halfvec` / `bit` / `sparsevec`，索引只有 IVFFlat 与 HNSW），
于是事务、JOIN、备份、权限、连接池全是你已有的那一套，许可（PostgreSQL
License）也最宽松。它换来的是三个明确的缺口：**过滤默认是 postfilter**
（近似索引下过滤在索引扫描之后才应用，第 27 章会引文档原文说明这是它最
著名的坑，0.8.0 起的 `hnsw.iterative_scan` 是可选项而不是默认行为）；
**没有列存、不存原始二进制**，本章算的那笔多模态账在它这里根本算不了；
**没有向量级的版本与时间旅行**。取舍因此很干净：负载以"向量 + 关系查询 +
事务"为主、又不用存原始字节时，pgvector 往往比再引入一个系统划算；一旦
"同表存图/音视频 + 做列式分析 + 按版本复现"这三件事里有两件成立，
LanceDB 的定位优势才真正兑现。

## 1.6 走一遍完整场景：500 万条视频片段按文字找片段

抽象讲完了，用一个具体场景把每一层的成本算出来。这个场景的设定是：

- 素材是 500 万条视频片段（clips），平均 12 秒，720p，H.264 约 2 Mbps，
  **单条 3.00 MB**，视频字节总量 **15.0 TB**；
- 每条片段用多模态模型算一个 **768 维 float32** 的向量（3072 字节），
  **向量总量 15.36 GB**；
- 每条片段还有约 1 KB 的结构化元数据（来源、时间、时长、分辨率、
  检测到的物体标签列表等），**总计约 5.1 GB**；
- 需求是"用户输入一句中文，返回 20 个最相关的片段并播放"。

**第一步，建模——一张表还是三张表。**
朴素做法是三张表：对象存储放视频、向量库放向量、关系库放元数据，
查询时三路对账。LanceDB 的做法是**一张表**：

```
clips.lance
  clip_id       int64
  src_uri       string          # 原始出处
  duration_ms   int32
  tags          list<string>    # 可以直接建 LABEL_LIST 标量索引
  clip_bytes    large_binary    # 3 MB 的原始视频片段，blob 列
  thumb_bytes   large_binary    # 120 KB 缩略图，另一个 blob 列
  caption       string          # 自动生成的描述，可以建 FTS 索引
  vec           list<float32>(768)
```

关键在 `clip_bytes` 与 `vec` 同表。查向量时不需要读 `clip_bytes`——
列存格式让"取 768 维向量"这一步只碰向量列；要播放时再按行号惰性读
blob 列。这一条把"向量库 + 对象存储 + 特征库"三件套压成了一件事。

**第二步，写入的成本。** 500 万条片段要先解码、抽帧、过模型。
按每片段抽 8 帧、40M 帧过一遍 CLIP 类模型、单卡约 1000 帧/秒估算，
就是 **约 11 GPU 小时**的编码成本（这是按公开的模型吞吐量做的量级推算，
不是实测）。写入本身反而是最便宜的一环：Lance 的摄入吞吐在官方基准里
比 Parquet 快约 70%，且加列回填是零拷贝的——以后想换一个 embedding 模型，
`add_columns` 加一列即可，**15 TB 的视频字节一个字节都不用重写**。

**第三步，索引的内存。** 5M × 768 维，各档索引的常驻内存可以直接算出来。
不压缩的 `IVF_FLAT` 每向量 3072 字节，5M 条就是 15.36 GB；
`IVF_SQ`（8 bit/维）把每向量降到 768 字节，即 3.84 GB；
`IVF_HNSW_SQ` 在 SQ 之上再叠一层 HNSW 图，每向量约 960 字节加图开销，
合计约 4.80 GB；`IVF_PQ` 压得最狠，`num_sub_vectors=48` 时每向量 48 字节
（5M 条 0.24 GB），`num_sub_vectors=96` 时是 96 字节（0.48 GB）。

这组数字解释了为什么量化不是可选项：**不压缩的话，光索引就装不进
一台普通机器**。`IVF_PQ` 把内存压到 0.5 GB 级别，代价是 `_distance`
变成估计值——所以它必须配 `refine_factor`（机制见第 12 章，实测数字见第 14 章）。
**"5M 落在 SQ 完全够、PQ 属于过度压缩的区间"是本章的归纳，不是厂商口径**；
在这个量级上，`IVF_SQ` 的 3.84 GB 是最省心的选择。

**第四步，查询的三段延迟。**
一次文字检索会被拆成三段，各自的量级完全不同：

1. **文字 → 向量**：调一次多模态模型的文本编码器，局域网内约 5–20 ms，
   走公网 API 则 50–200 ms。这一段和 LanceDB 无关，但它是整条链上
   第一个不可忽略的常数。
2. **向量 → 行号**：ANN 检索。索引分片在内存/SSD 上时是毫秒级；
   冷读到对象存储时按官方 OSS 口径是 **500–1000 ms**，
   这是整条链上最大的方差来源。
3. **行号 → 原始字节**：取回 20 条结果对应的 `clip_bytes` / `thumb_bytes`。

第三段是**所有人都会算错的一段**。它在"向量库"的心智模型里根本不存在，
但在多模态场景里它是成本的大头。按"20 条结果全取 3 MB 完整片段"算，
每次查询要搬 60 MB：QPS=1 时是 60 MB/s，QPS=10 时 600 MB/s（4.8 Gbit/s），
QPS=50 时 3000 MB/s（24 Gbit/s）。换成"3 条完整片段 + 17 条 120 KB 缩略图"，
每次查询降到约 11 MB：QPS=1 时 11 MB/s，QPS=10 时 110 MB/s，QPS=50 时 552 MB/s。

按 S3 出流量 $0.09/GB 粗算，第一种策略在 10 QPS 下是
**约 $140,000/月**，第二种是**约 $25,754/月**。
而把 15 TB 视频存在 S3 Standard 上，**存储费只有约 $345/月**。

换句话讲：**这个系统的成本 99% 不在存储、也不在向量检索，
而在"把原始字节搬出去"这一层**。所以多模态检索的架构决策，
第一个要回答的不是"用哪个索引"，而是"返回给前端的是原片还是缩略图"。
`thumb_bytes` 单独建一列的理由就在这里——它把每查询的字节数从
60 MB 压到 11 MB，成本降 5 倍，而且用户看到的搜索结果页几乎无损。

**第五步，把版本语义用起来。**
一年后模型换代，要换 embedding。传统做法是重建整个向量库、
期间服务不可用。LanceDB 的做法是开一个分支：

```
main
 └── embed-v2      # 从 main fork 出来，加一列 vec_v2，只回填向量
```

在固定评测集上比两个分支的召回，赢的那个 merge 回 main。
**原始视频字节从头到尾没有被复制过一次。**
这正是"多模态湖仓"这个定位落到工程上最实在的一条收益。

**这套账的结论。**
在这个场景里，LanceDB 的优势不体现在"每次查询快 3 毫秒"，
而体现在三件事上：一张表替代三套系统；换模型零拷贝；
计算存储分离让 15 TB 的静态数据和按 QPS 走的算力账单分开。
它的劣势同样清楚：**冷查询延迟被对象存储的 p99 支配**，
所以这个系统必须配一层本地 SSD 缓存或 read replica，
而这些在 OSS 里都要你自己搭。

---

第 2 章先跑一遍完整流程：连接、建表、写数据、建索引、三种检索、增删、
版本管理与维护，用一个真实可跑的脚本把上面这套心智模型落到代码上。
第 3 章再进入磁盘布局，讲 Lance 文件格式怎么摆——因为后面所有关于索引、
删除、版本、零拷贝演进的结论，都是那一层的直接推论。
