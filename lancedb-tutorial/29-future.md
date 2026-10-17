# 第 29 章　未来方向

这一章讲"往前看"。它和前面所有章节的性质不同：前面的内容都能在本地跑出来验证，
这一章的内容大部分只能引用文档和规范，而且**很多"未来"其实已经写进了规范、
只是还没有实现**。

所以本章对每一条都标注它处在哪个阶段：
**已经能用**、**规范已写明但没落地**、**公司愿景/营销**、还是**推测**。
不做这个区分的话，"路线图"就会变成一份愿望清单。

**先把一件事说清楚：官方没有公开的路线图页面。** `docs.lancedb.com` 上没有
`roadmap` 这一页（本章取证时确认），官方 README 给的指针是
「Check out the GitHub Issues if you would like to work on the features that are
planned for the future.」—— 也就是说，**官方口径的"计划中的功能"在 GitHub Issues
里，不在文档站里**。本章的推断因此来自四处：格式规范（`lance.org` 的
`docs/src/format/`）、`lance-format/lance` 与 `lancedb/lancedb` 两个仓库的 release
notes 与 issue、官方博客与月度 newsletter、以及 `llms.txt` 索引里的产品文档。
**除此之外看到的"2026 路线图"多半是第三方拼的**（例如把"Lance 2.0
AI-Compression""移动端 GPU/NPU 加速""Hybrid Search 2.0"写成既定计划），
这类内容本章一律不采信。

## 29.1 规范里已经写明、但还不是主线的

Lance 的规范仓库里有一批文档，标题上就写着 `Experimental` 或
`proposed on-disk format`。这些是最实在的"未来" —— 有格式定义、有 feature flag、
有取舍分析，只是还没有成为默认路径。

### 29.1.1 Fragment Metadata Tree

我们从官方规范原文取一段（`docs/src/format/table/fragment_metadata.md`）。
规范给这一页挂的警告是：这套布局「is unstable. Readers and writers may change
it without keeping compatibility with earlier unstable revisions. Flat manifests
remain the default.」，并且明确「**This page specifies a proposed on-disk format
for storing fragment state. Support for creating or reading tree tables is not yet
available in released Lance versions.**」；启用它需要 feature flag 4096
（`FLAG_FRAGMENT_TREE`）。

它要解决的问题在规范里也写明了：「In the flat format, each Version Manifest
carries the complete fragment list. Committing a change and opening a dataset both
process that whole list, even when the change touches only a few fragments.」

这是第 7 章那个"manifest 是全量快照"设计在大表上的直接后果：
一个跑了三年的表可能有几万个片段，于是**每次提交、每次打开表都要处理整个片段列表**。
片段元数据树把片段记录放进按 fragment ID 索引的不可变 Lance 叶子文件里，
"已验证的改动可以留在叶子上方作为 mutation 累积，直到工作量够了再重写"。

注意这个设计的双重性：它确实解决了大表问题，但它**把第 7 章
"读一个文件就知道全表状态"的简单性换掉了一部分**。
规范自己也在"At a glance"里列出了更复杂的读写路径。

一个必须提醒的边界：**13.0.0 的 release notes 里已经列了
"fragment metadata tree"，但规范页仍然写着"已发布版本尚不支持
创建或读取 tree table"**。两处说法不一致，**以规范页为准更安全**，
状态标记为"进行中/仅规范"，而不是"已发布"。

### 29.1.2 Data Overlay Files

当前 Lance 有三条"不改基础数据文件"的路径：**删除文件**（删整行）、
**Data Evolution**（加列或重写整列）、以及 **overlay**（改单个单元）。
第三条是最新的，规范对它的定位是
"给一个 fragment 内的部分 `(row offset, field)` 单元赋新值，
**而不重写基础数据文件**"。

它的设计细节里有两条很值得记：

**优先级的规则是显式的。** 更新的 `committed_version` 赢；
commit 版本相同时，按 `DataFragment.overlays` 里的位置，**后面的更新**。
而**删除赢过 overlay** —— 一个被删掉的行上的 overlay 是"死的"，
不管 commit 顺序如何。

**不支持它的 reader 必须拒绝整个表，而不是忽略。**
规范的理由写得很清楚：静默忽略 overlay 会返回陈旧的 base 值，
"这是正确性 bug 而非体验降级"。这条判据可以推广成一条通用原则：
**在存储格式里加一种"覆盖既有值"的机制时，必须让不认识的 reader 硬失败。**

同时它有一个诚实的现状：**写入端不完整。**
密实的（矩形的）overlay 可以用现有的等长 writer 写出来，
但**稀疏的 overlay 需要一个能输出不等长列的 writer，而当前的 v2 writer 做不到**，
在那之前只能把稀疏更新表达成"一个事务里的多个密实 overlay"。

### 29.1.3 MemWAL

这是所有实验特性里规模最大的一个。规范开头交代它的架构是
「a **Log-Structured-Merge (LSM) tree architecture** for Lance tables, enabling
high-performance streaming write workloads while maintaining indexed read
performance for key workloads including scan, point lookup, vector search and
full-text search」。

几个核心概念（都取自规范原文）：

**shard 是水平写入扩展的单位**，任一时刻恰好有一个活跃的 writer epoch。
对主键表有一条硬约束：**同一个主键的所有行必须落在同一个 shard**，
否则 shard 之间的异步压实顺序会让旧行覆盖新行。
（规范顺带说明了为什么主键是"unenforced"的：它只用做查找和 last-write-wins，
不做唯一性校验；追加式的表可以完全没有主键。）

**generation 定义新鲜度顺序。** 基准表是 generation 0，SSTable 的 generation 越大越新，
**活跃 MemTable 比任何已发布的 SSTable 都新**，MemTable 内部行位置越大越新。
MemTable 本身**没有** generation。

**WAL 是每个 shard 一份的 Arrow IPC 流文件**，路径形如
`_mem_wal/{shard_id}/wal/{bit_reversed_position}.arrow`。
文件名用**位反转的 64 位二进制** —— 目的是"把顺序位置分散到对象存储的 key 空间里"，
避免顺序写入把请求集中到同一个分区上被限流。这个技巧和第 3 章讲的
"数据文件用二进制前缀而不是 hex 前缀"是同一个思路：**在对象存储上，
key 的分布是一项要主动设计的东西。**

**SSTable 就是一个标准 Lance dataset**，写成单个 fragment，
并且带着基准表的 field ID。注意它**不是按主键排序的**（和经典的 LSM SSTable 不同），
随机访问靠一个 BTree 主键侧车索引，点查靠 Bloom filter 跳过。
主键的 last-write-wins 靠 fragment 0 上的删除向量实现，删除则表现为
带内部 `_tombstone` 列的行。

**shard manifest 才是 shard 本地可变状态的权威**，写成一个不可变的 protobuf，
路径同样用位反转的版本号；基准表的 manifest 里只放一个名为 `__lance_mem_wal`
的系统索引条目，存配置和全局进度。

还有一条容易被忽略但很重要的运维警告：规范说
"**删除 WAL 文件会削弱 writer fencing**" —— 老的 writer 之所以能发现
自己被隔离了，靠的是"它在下一个 WAL 位置上和别人撞车"。
把 WAL 删了，这个检测机制就失效了。

在官方文档层面需要标注一件事：**"LSM" 这个词只出现在格式规范里，
没有出现在 `docs.lancedb.com` 的产品文档正文中。**
所以"LanceDB 是一个 LSM 存储"这种表述，严格说超出了产品文档的措辞范围 ——
但也确实有规范的原文支撑。

**但"未来方向"这个归类要打个补丁：0.40 已经给了它一个可配置的入口。**
这一节原来把 MemWAL 整个放在"还不是主线"里，**在"能不能配"这个意义上
已经过时了**——0.40 的 Python 面暴露了 `LsmWriteSpec`
（`bucket("id", 8)` 做哈希分桶、`identity("id")` 按值分片、
`unsharded()` 不分片）和 `Table.set_lsm_write_spec()` /
`close_lsm_writers()` / `checkpoint_lsm()`，另外还有一个
`LsmWriteSpec` 可以注册到表上。附录 G §G.4 有这一层的完整清单，
包括本机实测出来的一条硬限制：**一次 `merge_insert` 只能打一个分片**，
`bucket(id, 8)` 的表上会直接抛
`ValueError: input row 0 hashes to bucket 3 but ...`。

所以准确的读法是两句话，缺一不可：**它是一个可配置、可运行的写路径
（这部分是"现在"），同时它没有公开的运维基准、没有状态声明、
本轮也没有量过压实的吞吐和恢复行为（这部分是"未来"）。**
把它写成"纯未来方向"会让读者错过一个能上手的接口；
把它当生产选项又会越过那些没被回答的问题。

**关于运维就绪度，规范给不出答案。** MemWAL 目前只有格式定义，
**没有公开的运维基准**（吞吐、压实延迟、回收策略这类数字都找不到，**未经确认**），
也没有任何"什么时候可以上生产"的状态声明。对照 29.1.1 的 Fragment Metadata Tree ——
那一页明确写了"已发布版本尚不支持创建或读取 tree table"，而 MemWAL 连这样一句
声明都没有。正确的读法是：**MemWAL 属于"接口已发布、运维经验为零"的特性**，
把它写进容量规划是不负责任的。

### 29.1.4 Row lineage 与 stable row ids

第 7 章讲过 stable row id 的动机：压实之后行号会变，
所以需要一个 `(fragment_id << 32) | offset` 之外的、真正稳定的逻辑 id。

现状需要分清**两件事**：

**迁移机制已经有了。** `lance` crate 12.0.0 加了
`Dataset.migrate_to_stable_row_ids` 迁移方法；13.0.0 加了隐藏的和溢出的
row lineage 列。Python 侧通过
`storage_options["new_table_enable_stable_row_ids"]` 开启，默认 `false`。

**但迁移的条件很苛刻，而且不是自动的。** 规范里列了前置条件：
普通的写入即使带了 `enable_stable_row_ids=true` **也不会触发迁移**，
必须显式调用迁移方法；迁移前要停掉所有索引构建、
**删掉所有二级索引**、并让写入者静默；迁移本身是一次原子的 merge commit，
而且**冲突时不重试**。迁移会给**每一个物理位置**（包括已删除的行）分配 id。

**索引侧仍然是实验的。** 官方索引文档里对 "Stable Row ID for Index" 的原话是
"This feature is currently experimental. Performance evaluation is ongoing to
determine when the tradeoff is worthwhile."

取舍也很清楚：好处是压实后不需要重映射；代价是"查询时要多做一次
stable row id → 物理 row address 的转换"。而 2026-09 的社区 sync 里
"stable row id 的生产级重新设计"仍然是一个**开放议题**。

所以对这一条的正确描述是：**能用了，但要付迁移成本（删索引、停写），
而且索引侧还没到生产级。** 把它当成"未来会更好"的东西是合适的，
当成"现在就能开"的东西不是。

机制层面（stable row id 的动机、与压实改行号的关系）在第 7 章，
维护视角（压实何时触发行号变化、该多久做一次）在第 23 章，
本节只保留"未来方向"这一侧的判断，不重复那边的机制。

### 29.1.5 一批新的标量索引

官方的索引列表里有这么几种（我们把它们和第 4 章的编码放在一起看会很顺）：

**FM-Index（精确子串）—— 注意：这一个已经能用了，不是未来方向。**
本机实测 `t.create_index("txt", config=lancedb.index.Fm())` **建索引成功**，
`index_stats` 返回 `IndexStatistics(num_indexed_rows=50, num_unindexed_rows=0,
index_type='FM', ...)`，`list_indices()` 里出现 `index_type='Fm'`。
所以它应该和下面三种分开读：下面三种是"规范里写了、路由图还没落地"，
FM 是"今天就能建"。

它的原理是基于 Burrows-Wheeler 变换的压缩子串索引，
支持任意子串、前缀、后缀和正则。规范里记了两个实现细节：
`\x00` 保留作全局 BWT 终止符、`\xFF` 作文档分隔符，输入会被重映射到空格里。
还有一个结构性限制：**BWT 索引不能被拼接**，所以 `merge_segments`
必须重新读源数据 —— 这在高频写入的表上是有成本的。
它的定位和 NGRAM 的区别（精确 vs 候选集）在第 18.9 节。

**Bloom Filter** —— 按 zone 组织的 SBBF（Split Block Bloom Filter），
近似（无假阴性、有假阳性），但 `IS NULL` 是精确的（靠维护的 null 位图）。

**R-Tree（geo）** —— 规范明确说它是"**静态、不可变的 2D 空间索引**"，
用打包构建 + Hilbert 曲线排序。代价是**空间索引不能增量追加**，
每次变更都要重建。它加速 `St_Intersects` / `St_Contains` / `St_Within` 等谓词，
语义是 `AtMost`。

**JSON 索引** —— 这一条最容易误解。它本质上是一个**包装器**：
把 JSONB 在某个路径上解码出来，合成一个 Arrow 数组，交给一个普通标量索引处理。
后果是**整列必须选一个统一的 Arrow 类型**（Boolean / Int64 / Float64 / Utf8），
记录在 `target_data_type` 里。规范自己给了很重的警告：
"这些细节没有记录目标索引的格式版本，所以它们**不是该索引的通用、
无损描述，绝不能当成那样读**"。

**MinHash-LSH（实验性）** —— 用来回答"**哪些行是这一行的近似副本**"，
基于估计的 Jaccard 相似度。规范的措辞很硬：
"message 和文件**可能不兼容地改变而不需要单独投票，届时索引必须重建**
（**数据永不重写**）；**如果稳定化投票没通过，这个特性会被移除**。"
规范的规范文本里还带了 Version 0 的参考实现和已知答案向量。

把这一批放在一起看，有个共同的模式：**Lance 在给"非结构化数据上的标量谓词"
补索引**（子串、相似度、空间、JSON 路径）。
第 1 章讲过 Lance 的核心卖点是"向量 + 标量 + 全文在同一个表里"，
这批索引就是把"标量"这一侧的覆盖面补全。

### 29.1.6 Fuzzy 检索：这条已经发布了，别当未来

需要纠正一个常见的归类：**模糊检索是已经能用（已发布）的功能，不是实验特性。**

LanceDB 支持 `MatchQuery("learnin", column="tags", fuzziness=1)`，
`fuzziness` 是 Levenshtein 距离，默认 0，
**不指定时会按词长自动选**：长度 ≤ 2 用 0、≤ 5 用 1、> 5 用 2；
`max_expansions` 默认 50。它支持对 `pa.list_(pa.string())` 这类
字符串数组列建 FTS 索引并做模糊/短语查询。

同一处还有一条限制值得记进坑列表：
**基于 Lance 的 FTS 不支持在查询串里用布尔运算符 `OR` / `AND`**，
要组合条件得用 `BooleanQuery` 这类结构化查询对象。

### 29.1.7 Multi-base layout（多基路径）

这是一条"从真实客户需求长出来"的格式特性，来源可以查得很清楚：
Uber 提的需求是"**把一个 Lance 数据集分布到多个 S3 桶上以扩展吞吐，
同时保留相对路径的可移植性**"。

设计上的关键是引入了一个"**location base**"概念：manifest 里带一个
`base_paths` 列表，每个文件引用其中一个 base。官方博客给的效果描述是
"**把一个有 1000 万文件的数据集分布到 5 个桶上，需要改的是 5 个字符串，
而不是 1000 万个**"。

它解锁的场景包括热数据分层（把热数据放到 Lustre / Weka 这类并行文件系统上）、
多地域分发、按 base 的定向容灾切换，以及作为 shallow clone / 分支 / 标签的基础。

### 29.1.8 Lance Namespace 与 catalog 的路线图

第 26 章 §26.6 把 catalog 层归到"格式层互操作"里，这里补它的路线图状态。
**Lance Namespace 自成一版**（第 28 章 §28.2 讲过的第四套版本号，本机装到的是
**0.13.0**），目标是让 Lance 表出现在**任何 catalog** 里。官方的支持列表
（`lance.org` 的 Namespace Client Spec / Supported Catalogs，2026-10-17 抓取）里，
**有专门实现**的是 AWS Glue、Apache Hive MetaStore（V2/V3）、
Apache Iceberg REST Catalog、Apache Polaris、Unity Catalog，
实现都在 `lance-format/lance-namespace-impls` 里；
**Google BigLake / Dataproc 与 Microsoft OneLake 是经各自兼容端点接进来的** ——
BigLake 走它的 Iceberg REST Catalog、Dataproc 走它的 Hive MetaStore、
OneLake 走 Unity Catalog 或 Iceberg REST 端点，Lance 侧都没有专门实现
（OneLake 那一页官方明说 "does not provide a dedicated OneLake namespace
implementation"）；**Apache Gravitino 也在这份列表里，但原生接入是 Gravitino
从 1.1.0 起自己那侧提供的，Lance 仓库里并没有它的实现**。

**路线图里明确的下一步是 Arrow Flight gRPC**：官方说"为了提供完整的 catalog
spec 体验，Lance 的目标是用 Arrow Flight gRPC 作为主要标准"，
以符合"Arrow-native lakehouse format"这个定位。所以现状是**REST 已经能跑、
Flight 还在路上**（Flight SQL 的具体时间表未公开，**未经确认**）。

**要记住的边界**：这条路线图管的是"Lance 出现在别人的 catalog 里"，
不是"LanceDB 变成一个元数据服务"—— 开源版没有常驻的 catalog 进程，
catalog 能力靠 namespace 规范加各家实现拼出来。

## 29.2 "Lance 作为 AI 数据的 Iceberg"

这一节讲愿景。做区分的价值在于：**这个愿景有一部分是可验证的工程事实，
另一部分是定位话术。**

### 29.2.1 可验证的部分：层数

技术上的差异是可以数出来的。Iceberg 覆盖**两层**——表格式和 catalog 规范，
文件格式用 Parquet。**Lance 覆盖三层** —— 它同时是文件格式、表格式和 catalog 规范。
官方博客里的原话是"**Lance spans three layers of the stack, because it's
simultaneously a file format, table format and a catalog spec.**"

在元数据这一层，两者的形状也不同：Lance 是"**每个表版本一个 manifest 文件**"
的单层结构，Iceberg 是"metadata → manifest list → manifest"的三层结构。
行标识也不同：Lance 是 64 位的 `(fragment_id, offset)`，
Iceberg 是"文件路径 + 位置"的元组。

官方对 Iceberg 的批评列了四条，其中三条是技术性的（元数据多层跳转、
缺少对图片/视频/音频/向量的原生类型支持、缺少低延迟随机访问），
第四条是"缺少高效的列追加"。
最后一条有个很直观的例子：**一个 100 GB 的表，要加一列带 1% 数据的列，
在 Iceberg 上是全表重写**（因为 Parquet 把整个 row group 存在一起），
**在 Lance 上是零拷贝操作**。

### 29.2.2 不能夸大的部分

有几条必须按实际状态写。

**"Lance 是 AI 数据的 Iceberg" 这个短语没有在官方文本里逐字出现过 —— 未经确认。**
叙事是官方的一手（2025-04-08 那篇博客明确说两者是"互补而非竞争"），
但不要把一句概括当成引文。

**Catalog 集成的清单是具体可查的**：`lance.org` 的支持列表（2026-10-17 抓取）里，
有专门实现的是 AWS Glue、Apache Hive MetaStore（V2/V3）、
**Apache Iceberg REST Catalog**、Apache Polaris、Unity Catalog、Apache Gravitino
（最后这一家的接入由 Gravitino 侧提供）；
Google BigLake / Dataproc 与 Microsoft OneLake 走的是各自的 Iceberg REST /
Hive / Unity 兼容端点，不是 Lance 侧的专门实现。
**Lakekeeper 在 lance.org 的规范导航和 `docs.lancedb.com` 里都找不到 —— 未经确认**，
不要在文档里写它。

**未来标准是 Arrow Flight gRPC**。官方说"为了提供完整的 catalog spec 体验，
Lance 的目标是用 Arrow Flight gRPC 作为主要标准"，
以符合"**Arrow-native lakehouse format**"这个定位。

**Lance 目前打不赢的地方官方也写了**：它的数据组织方式
"**doesn't fit well with traditional OLAP-style compute engines**"；
计算引擎集成"**still emerging (primarily Spark and Ray at present)**"，
Flink / StarRocks / Trino 社区有兴趣但官方措辞是 "upcoming / in their nascent stages"。

所以准确的表述是：**Lance 的目标不是取代 Iceberg，而是占据"Iceberg 覆盖不到的
那部分 AI 数据工作负载"，并以 catalog 层互操作的方式共存。**
把"Lance 要取代 Iceberg"当成事实是不准确的。

### 29.2.3 两个容易被漏掉的生态项

**`lance-graph`。** 它是 Lance 治理公告里举的"低门槛孵化通道"例子（由 Uber 发起），
把图结构与 Lance 存储结合，服务"图 + 向量"这类负载。它目前是**子项目**，
不进主线的四套版本号体系。提它的意义是：**看到 `lance-graph` 别以为它是
LanceDB 的内建能力** —— 它和主线的距离，比"Experimental"标记的那些特性还远。

**Iceberg v3 互操作。** 官方对 Iceberg 的定位是"互补而非竞争"（29.2.1 讲过），
catalog 层的互操作（Iceberg REST Catalog）已经能跑。但**"Lance 与 Iceberg v3
规范在文件 / 表格式层怎么协同"没有公开的时间表**（**未经确认**）。
把它当成"迟早会有"是推测，不是事实。

## 29.3 加速方向

这一节要先把一个流行误解纠正掉。

### 29.3.1 GPU 索引不是"未来"，它是 2023 年的功能

**GPU 加速建索引早在 2023-11-02 就发布了。** 官方博客的原话是
"**In the most recent release of the Python package of LanceDB (v0.3.3),
backed by Lance (v0.8.10), you can now use either CUDA or MPS by simply
specifying the `accelerator` parameter when calling `create_index`**"。
当时的实现方式是"用 PyTorch 训练 IVF 的聚类，把 kmeans 质心交给 Rust 核心做索引序列化"，
官方给的基准是 IVF_4096 / 100 万 × 1536 维：
Linux 上 CPU 323 秒 vs GPU 12.5 秒，MacBook Pro 上 397 秒 vs 21 秒，
**大约 19–26 倍加速**（厂商口径；两个例子各自是 323/12.5 ≈ 25.8 倍与
397/21 ≈ 18.9 倍，所以区间下沿取 19 而不是 20）。

**只有"自动 GPU 索引"是企业版独占的。** 开源版里 GPU 索引是手动的：
显式指定 `IVF_PQ`、同步 SDK、需要 PyTorch > 2.0、
`accelerator="cuda"`（Linux）或 `"mps"`（Apple Silicon）。
企业版的说法是"a few billion vectors under 4 hours"（厂商口径）。

至于 **NVIDIA cuVS** 这条线，状态必须写准。它唯一的官方载体是
`lance-format/lance-cuvs` 这个孵化中的仓库，包名是 **`pylance-cuvs`**。
它的 README 里写明了三条硬约束和一条定位：

- **只支持 Linux，只支持 Python 3.12+，依赖 CUDA 12 运行时和 `libcuvs-cu12==26.2.0`**；
- 要求把 lance **钉死在一个具体的 commit** 上
  （`6112a34bfe38618f07c099217dc3d89fd39ca6bb`）；
- "**Published beta releases are wheel-only. Source distributions are not supported.**"；
- 定位只有三件事：用 cuVS 训练 IVF_PQ、构建分区内的产物、把产物交回 Lance，
  原文是 "**It does not create or register a Lance index on your behalf.**"

**结论：cuVS 目前在 commit-pinned 的 beta 状态，不是一个能直接投产的产品路径。**
而且它**不在任何官方路线图里** —— 只存在于那个孵化仓库。
我们也没有找到 LanceDB 与 NVIDIA 联署的 cuVS 发布博客（**未经确认**）。
所以教程里**不应该**写成"LanceDB 用 cuVS 建索引"。

### 29.3.2 分布式建索引：官方明确说它不提供调度器

这是本节最值得逐字引用的一段官方原文。`lance.org` 的分布式建索引文档里有一条
Warning，写的是「Lance exposes public APIs that can be integrated into an external
distributed index build workflow, but **Lance itself does not provide a full
distributed scheduler or end-to-end orchestration layer.**」；
紧接着还有一句更直接的：「**Lance does not provide a distributed scheduler.
The caller is responsible for launching workers and driving the overall
workflow.**」

模型是这样的：worker 调 `create_index_uncommitted(..., fragment_ids=...)`
把自己的索引段写到 `indices/<segment_uuid>/`，
调用方再决定是 `commit_existing_index_segments(...)` 还是
`merge_existing_index_segments(...)`。

两个实现层面的限制值得记：

**不是所有索引都能合并。** `merge_existing_index_segments`
目前只支持 vector / inverted / bitmap / BTree / zone map，
其它标量索引家族还不能被合并成更大的物理段。

**合并的粒度是"段"，段的数量直接决定元数据大小。**
`lance-ray` 提供了 `num_segments` 参数，这个旋钮的取舍在"元数据体积"和
"并行度"之间。

真实世界的效果是有的（厂商/客户口径）：字节跳动火山引擎的案例里，
原本单机 IVFPQ 训练要 500 GB+ 内存、跑多天，改成跨 Lance fragment 的分布式训练之后
"**把 7 天的模型训练流水线压到 1 天，GPU 利用率 95%**"；
二进制副本压实"**在 500 万行表上把压实时间从 418 秒降到 15 秒**"。
Exa 的案例是"用 Lance 和 Ray Data 索引数千亿网页"。

**所以这一节的准确表述是**：Lance 提供了可被编排的 API，
但**调度器要你自己写**。如果你的项目文档里写着"用 LanceDB 做分布式建索引"，
那个"分布式"是你要自己搭的部分。

### 29.3.3 浏览器 / 边缘

这一条要标注得特别清楚，因为官方定位和可验证产物之间有明显的落差。

官方的 FAQ 定位是："**LanceDB is among the few embedded vector DBs out there
that we believe can unlock a whole new class of LLM-powered applications in the
browser or via edge functions**"。

但我们**找不到一个可核实的产品化 WASM 构建**：
`@lancedb/lancedb` 的 README 里检索不到 `wasm` / `WebAssembly` / 浏览器运行时的表述。
所以现状是：**官方把"浏览器/边缘"当成定位，但当前文档和 README 里没有承诺
一个浏览器 WASM 构建 —— 未经确认。**

"嵌入式 / 边缘"这个说法在**当前**是成立的，但它指的是另一件事：
**进程内的原生库**（Rust 核心，不需要服务器、不需要 Docker，
数据落在本地盘或对象存储上）。这一条是确凿的，
`lancedb` 的 wheel 会把 235 MB 的 `_lancedb.pyd` 直接装进你的 Python 环境，
这就是"嵌入式"的字面证据。

边缘生态里有一个确实存在的东西：**LeRobot 的原生集成**
（可以直接从对象存储上训练机器人策略），有独立的文档页。

### 29.3.4 I/O 层的一条前沿

顺带记一条不属于以上三类的加速方向：2026-01-29 的官方博客
《One Million IOPS》记录了"把调度器重写 + `io_uring` 结合之后达到
**150 万 IOPS、磁盘跑满**"。但**作者自己在文末写了 "Not Yet Ready!" ——
"写这篇文章时这些改动还没有合并"**。而且那个基准把 `nprobes` 从 20 降到 1
（召回率很差）来换取 IOPS。

这类数字的正确用法是"知道方向在哪"，而不是"预期这个性能"。

### 29.3.5 向量索引的下一代：IVF_RQ / RaBitQ 往后走

这是路线图里最实在的一条，因为它有公开的改进记录。当前的下一代索引是
**IVF_RQ**（用 RaBitQ 量化，1 bit/维起步，`num_bits` 可调 1/2/4/8）。
它的卖点是**省掉 PQ 昂贵的码本训练**、建索引更快、更新更容易，
在高维、同等存储预算下召回不低于 IVF_PQ。官方博客在 2026-07-10 的
《RaBitQ Gets Faster》里给出的改进是**更高的召回、更低的 p99 延迟，
以及一个查询时的调节旋钮 `approx_mode`** —— `approx_mode` 让"精度 / 延迟"的取舍
可以在**查询时**决定，而不是建索引时锁死，这在负载波动大的场景里是个有用的能力。

一条关于兼容性的提醒（**厂商口径**）：**`num_bits ≥ 2` 的多比特索引用的是更新过的
落盘布局，老版本读不了**，只有在重写（compaction / optimize / remap）时才会自动升级。
所以"升级 SDK 就自动吃到新索引"是不成立的，多比特索引的迁移是**一次重写**。
更细的索引参数与量化取舍在第 12 章，本节只标方向与版本状态。

### 29.3.6 GPU 查询：只有索引侧，没有查询侧

需要把口径说准：**GPU 加速的是"建索引"，不是"查询"。**
29.3.1 讲的 GPU 建索引（含 cuVS）都落在训练 / 构建阶段；
**本教程没有找到公开的查询侧 GPU 路径** —— `search()` 没有 `accelerator=`
之类的参数，cuVS 那条线的定位也只到"构建分区内的产物、交回 Lance"为止
（29.3.1 引过的 `It does not create or register a Lance index on your behalf.`
同样只覆盖建索引）。所以查询侧的可调项仍然是 IVF 的 `nprobes`、
RaBitQ 的 `approx_mode` 与缓存策略（第 6 章、第 24 章）。
**这是一条负结论（官方未见公开的查询侧 GPU 计划，未经确认），写进选型文档时要标清楚。**

## 29.4 全文检索的演进，以及那个容易混淆的 "FTS V3"

第 28 章已经点过一次，这里展开。

**FTS 的格式版本和文件格式版本是两套独立的编号**，而且这里还叠了第三个基准 ——
**`lance` 库（crate）的版本**。三套必须分清，否则数字会打架。

**posting 格式这一层。** 当前默认是 **posting format v2**（随 Lance 库 9.0.0 一起可用），
而 `block_size=256` 可以 opt-in 到「**experimental FTS V3 layout**」。
文档同时说明：code analyzer 与 `block_size=256` **需要 format v3**，
`document_granularity="list_element"` 也要求 v3 reader 能力。
**这里的 "v3" 是 posting 布局的 v3，不是文件格式的 v3** ——
第 28 章 §28.3 专门拆过这个歧义，本节只讲方向、那边讲版本号。

**crate 版本这一层。** 下面这些性能里程碑里写的 `v11` / `v12` / `v13`
说的是 **`lance` crate 的大版本**（13.0.0 对应 2026-10-07，见第 28 章 §28.1），
和 posting 格式版本**没有任何对应关系**。把两套数字写进同一句是这一节最容易犯的错：
`v11` 不是"posting v11"，`v3`（`lance` 3.0.0）也和"FTS V3"毫不相干。

所以当有人问"该用 FTS V2 还是 V3"，正确的理解是：
**这不是文件格式的选择，是倒排索引 posting 编码布局的选择**，
而且 V3 目前是实验性的（postings 编码会变，未来可能 breaking）。

性能侧的进展（厂商口径，均为 `lance` crate 版本口径）：
v2 时代"bulk MAXSCORE search for top-k disjunctions"、
官方宣称"7x FTS performance improvement"、冷读"up to 8× faster"；
crate v11 加了 MAXSCORE，候选探测"**up to 53.6x**"的削减；
v12 加了 "deferred phrase confirmation with score bounds"、宽 AND 查询的批量求交；
v13（2026-10-07）加了 BM25F 的 `combined_fields` 查询。

中文支持有一条具体的实现路径：JSON 的 FTS 用 `path,type,value` 三元组分词，
中文可以配 `jieba`；`base_tokenizer` 支持 `jieba/*` 和 `lindera/*` 这些配置。

## 29.5 多模态湖仓要解决的问题清单

这一节不带判断，只把"多模态湖仓"这个说法背后的**具体工程问题**列出来，
因为"多模态"这个词太容易被当成营销词，而它对应的问题是很实在的。

**问题一：跨模态联合检索。** 一份数据里同时有图片、文本、结构化元数据，
一次查询要同时用上向量相似度、全文相关性和标量过滤。
官方的表述是"hybrid-query-native, offering vector, full-text, and SQL capabilities"，
并且把"传统数据湖是模块化的但**碎片化的**，
要求团队为每一种查询拼一套系统"当成核心痛点。
从工程角度看这就是一句很朴素的要求：**不要为每种查询维护一套存储和一套索引。**

**问题二：blob 的一等公民地位。** 一个"只用指针"的方案（表里存 URL 指向 S3 上的原图）
有两个具体代价：访问控制要分层（一份数据两套权限），
以及"取单个数据项时额外的 I/O 和网络开销"。
Lance 的答案是 Blob V2 —— 它把 blob 按大小分成四档（`Inline` / `Packed` /
`Dedicated` / `External`），描述符带 `kind` / `position` / `size` / `blob_id` /
`blob_uri` 这些字段。**这四档的阈值、字段定义与取舍在第 21 章 §21.4
（多模态章是它的主场），本节不重复。**

**问题三：训练数据管道。** 这一块对应的产品是 Geneva（企业版），
官方文档的说法是"**The `geneva` package uses Python User Defined Functions (UDFs)
to define features as columns in a Lance dataset.**"；
流传更广的那句 "Write Python like you normally would. Keep your functions pure."
**本轮未能从官方文档核实**。
具体拆成三类 UDF：逐行的标量 UDF、用 Apache Arrow 做批处理的高性能 UDF、
以及**有状态 UDF**；底下跑在 Ray + KubeRay 上。

**问题四：特征回填。** 这是"多模态"最容易低估的一块。
重跑一个 embedding 模型意味着给几百万行重算向量，
而官方文档给这类操作的措辞是 **Incremental refresh**（"refreshes incrementally"）：
可以只回填满足条件的行，并在新数据到达时刷新物化视图。
API 上的体现是 `tbl.backfill_async("embedding", batch_size=10, commit_granularity=2)`
这类带**部分提交**语义的调用 —— 注意 `commit_granularity` 这个参数，
它意味着回填过程中会提交多个版本，中途可见。

**问题五：时序 + 向量。** 机器人数据的典型形态是
"把每一个时间步存成一行（像素、本体感觉、物理量、指令），
在摄入时加 DINOv2 embedding，然后用一次混合检索代替后置过滤"，
官方给的样例规模是 **32,212 个时间步**一行一个时间步
（该数字**未能从官方文档核实**，可能来自博客或营销页）。
这个场景对"一次查询同时用向量和标量"的要求最直接 ——
后置过滤（先取 top-k 再按时间窗过滤）在时序数据上很容易返回空结果。

**问题六：视频与多模态模型的路线图。** 这一条目前处在"有方向、没细节"的阶段。
已经能用的是 **Blob V2 装原始视频字节 + 多向量列（ColPali / ColQwen）做
late interaction + 跨模态 embedding（ImageBind / OpenCLIP，第 26 章 §26.4）**，
也就是说"把视频存进去、用帧向量或文本查"这条链路是通的。
**但"原生的视频向量索引 / 视频解码 / 帧采样"没有公开的官方计划**（**未经确认**）——
现在的做法是在应用侧抽帧、算 embedding，再把结果写进表。
把它写成"LanceDB 支持视频检索"，会掩盖掉"视频理解是你自己的活"这一层。

**这些问题的共同点**：它们都不是"更好的向量索引"能解决的，
而是"向量、标量、全文、blob 必须在同一个事务边界和同一个 schema 下"。
这也解释了为什么 LanceDB 选择从文件格式往上做，
而不是在 Parquet 上叠一层 —— 第 3 章讲过的"容器没有类型系统"、
"FSL 在结构编码阶段被展平"这些设计，都是为这一节的问题在做准备。

## 29.6 已知的批评与风险

这一节是本章最重要的部分。前面讲的都是"会变好"，
但选型决策要靠的是"**现在哪里不好**"。

下面每一条都给出可查的证据。**凡是第三方转述、原始基准未公开的，都标注 未经确认。**

### 29.6.1 锁定效应：要分清"数据不锁"和"运行时不锁"

企业版文档里有一句"**No vendor lock-in; data moves freely between editions**"，
它在**数据格式开放**这个意义上是成立的。但"锁定"有两层，第二层才是真的。
**第一层（数据）** —— 检索范围内没有发现独立第三方实现的 Lance reader / writer
（未经确认，属于"没找到"而不是"不存在"；Iceberg 则有 Spark / Flink / Trino /
Snowflake / BigQuery 各自的实现），Lance 的互操作真正发生在 catalog 层
（Namespace / REST），不是文件层。**第二层（运行时）** —— 治理文章原话是
「**The short answer: it doesn't, at least not yet**」「**by far the most
contributions coming from LanceDB Inc.**」，PMC 20 席里公司员工占 9 席；
再叠加"采用 Lance 就要同时接受 file / table / index / catalog / namespace 五层规范"
与企业版独占能力（分布式检索/建索引、自动 GPU 索引、Geneva、SQL），
结论是：**数据你可以带走，但把系统跑起来的能力带不走。**

治理与公司这一层的完整事实（Eto Labs、YC W22、两次仓库搬家、PMC 席位）
都在第 28 章 §28.6，本节不再重复。

### 29.6.2 生态成熟度：和 Parquet/Iceberg 的对比要谨慎

**能成熟使用的计算引擎集成只有 Spark 和 Ray**，其余是
"upcoming / in their nascent stages"（官方措辞）。
社区体量按 2026-10-17 GitHub 仓库页实时抓取的数字（与第 27 章 §27.1 同一口径）：
`lancedb/lancedb` **11,684** stars，对比 Chroma **29,511**、Qdrant **35,101** ——
体量上是明显的挑战者，直接体现为更少的集成、教程与 Stack Overflow 答案。

**厂商自己的口径也不一致**：FAQ 页写"up to **1000x** faster than Parquet
for random access"，首页写"**100×**"。差一个数量级。
这类不一致本身就是一个信号：看到任何单个倍数的时候，去查它的口径和场景。

还有一个更实的数据点：**官方承认 OSS 的容量上限**是
"can comfortably handle millions of vectors on a single node"，
到了"hundreds of millions of vectors or terabytes"官方就推荐企业版了。

### 29.6.3 依赖矩阵：更新频繁、平台在收缩、编译不了也调试不了

这一节和第 28 章 §28.7.3（版本 churn）、§28.7.4（平台矩阵收缩）几乎逐点对应，
数字、日期与来源都在那边，本章不重复。这里只留一条那边没展开的差异：
**"用 Rust 写的所以我能调试"这句话有官方依据地不成立。**
官方说 Rust 公共 API "仍在实验状态"，`lance-arrow` "不预期会 stable、建议 fork"，
而 `fork` 在多线程进程里本来就不安全（详第 25 章 §25.5）；再加上 Python 侧
只有 235 MB 的 `.pyd`，栈追踪会断在原生边界上。**这不是能力问题，是要提前接受的成本。**

其余的版本节奏、wheel 矩阵、`5.0.0` 从未发布、13.0.0 的 9 条 breaking changes、
Python ≥ 3.10 / glibc ≥ 2.28 / 没有 Intel Mac wheel / 没有 sdist 等，
见第 28 章 §28.7.3 与 §28.7.4。

### 29.6.4 建索引的内存成本

这一条有一个官方承认的完整案例。用户在 **4100 万向量、178 GB 数据集、32 GB 内存**
上建 `IVF_HNSW_PQ` 索引时 OOM，而 `IVF_PQ` 没问题
（issue `lancedb/lancedb#1500`，2024-08-03）。

维护者的回应值得逐字看：「This is somewhat expected. **HNSW is a very memory
intensive algorithm.** This is part of why IVF_PQ is the default.」
另一位维护者承认："Admittedly, our indexing path of this new index type is
less optimized. We should definitely make it more memory efficient."

机制层面的解释是："**we got to keep vectors of at least one partition in memory
during indexing. OOM means that the memory can't serve some large partition**"，
所以调大 `num_partitions` 有帮助（代价是查询时要更大的 `nprobes`）。

还有两条相关的运行时陷阱：

**没有索引的行在索引更新期间会退化成暴力扫描**；
`.fast_search()`（一个链式方法，**不是** `search()` 的关键字参数 ——
`search(emb, fast_search=True)` 会直接 `TypeError`）只搜已索引的数据，
在无索引的表上返回**空结果**而不是报错；
而 `wait_for_index(...)` 在持续写入的场景下"**可能超时并且永不返回**"。

**所以容量规划里"建索引要多少内存"这一项必须单独预算**，
而且它的经验规则是"**单个分区要能装进内存**"，
不是"总内存大于数据集"。

### 29.6.5 对象存储的一致性与资源泄漏

**一致性是可配置的，而且默认是"不刷新"。** `read_consistency_interval`
不设（默认）时，官方文档说"no automatic cross-process refresh checks"，
表不会自动从别的写入者那里刷新；要强一致得设 `timedelta(0)`，
官方推荐的弱读间隔是 30–60 秒。写始终是强一致的。
推论：**多个读者看到不同版本在 LanceDB 里是设计行为。**
另外**没有任何官方的按 provider 一致性保证**（Azure Blob / GCS / MinIO）—— 未经确认。

**有一个真实的内存泄漏，开了 14 个月才关。**
issue `lancedb/lancedb#2468`（2025-06-26 开）：通过 S3 直连时，
约 2 GB 数据（200 万行）消耗了 16 GB+ 内存。
维护者给根因是"每次重开 http 连接都重新加载 SSL 证书"，
而"要显式把 session 对象传下去，这在我们的绑定里做不到"。
**这个 issue 到 2026-08-26 才被 #3805 关闭** —— 从开单到关闭正好 14 个月。
缓解手段是用 POSIX 挂载代替 S3 直连。

**还有一个仍然能复现的文件描述符泄漏。**
issue `lance-format/lance#9934`（2026-10-13 开，仍开着）：
**每张表每跑一次 `optimize()` 就泄漏一个文件描述符，永不释放** ——
缓存淘汰、空闲、`gc.collect()`、`connection.close()` 都不释放，
最终会撞到 `Too many open files`。
报告者给的数据是：每轮 +1.97 个（两个索引都在时）；
7 张表共享一个 `Session` 时每轮 +7 个，**80 轮后到 607 个**。
`Session(index_cache_size_bytes=...)` 只是"改变斜率不改变方向"。
报告者说在 `0.30.2 / 0.32.0 / 0.33.0 / 0.34.0 / 0.40.0` 上都能复现，
并明确说"**0.40.0 每轮漏 2.07，略差于 0.34.0 的 1.97，不是新引入的回归**"。
（这条是第三方用户提交、自己报告的数字，**未经独立复现**。）

**长跑的服务里，fd 和内存都要监控。** 这不是"注意一下"级别的建议，
上面两个数字说明它们可能几小时到几天就撞上限。

### 29.6.6 多进程写并发

这一节的三个限制在第 25 章已经是主场，这里只做指针。
**并发写者太多会失败** —— 官方原话是「we support concurrent writing, though
**TOO MANY CONCURRENT WRITERS** can lead to failing writes as there is a
**LIMITED NUMBER OF TIMES A WRITER RETRIES A COMMIT**」（重试默认 20 次），
见第 25 章 §25.4；**`fork` 不安全**（多线程进程 + fork，必须用 `spawn`），
见第 25 章 §25.5；**`merge_insert` 未索引行上限 10,000**、
且"significantly slower than `add()`"，见第 25 章 §25.2。
第三方（未经确认）还有"多进程并发访问有局限、不适合多租户高并发后端"的说法，
但上面三条官方限制已经足够说明：**不要把多进程并发写当成这个系统的强项。**

### 29.6.7 没有第三方基准

这一条对做技术选型的人最重要，但完整版在第 27 章 §27.25（对比章是它的主场）：
**`lancedb` 在 VectorDBBench 的 README 里出现 0 次、也不在 ANN-Benchmarks 里**，
所以网上绝大多数"LanceDB vs Qdrant vs Milvus"的对比表要么是厂商自测、要么没有出处。
本节只补一条那边没展开的提醒：**厂商手里最硬的一份数据不是向量 QPS，是元数据操作的**
（厂商口径，2026-07）—— 10,000 次 S3 提交的平均延迟 Lance 140 ms
vs Delta 534 ms vs Iceberg 457 ms、活跃元数据体积 0.78 MiB vs 42+ MiB、
200 个并发写入者时失败率 16% vs 88% / 89–94%。它测的正是第 6、7 章讲的那些机制
（单层 manifest、指针提交、条件写），差异是结构性的、不容易靠调参抹平，
但仍然是**厂商口径**，应该这么标注。

### 29.6.8 社区的真实反馈

这一条要给出处，因为它是本章唯一"用户第一人称"的材料。

在 Hacker News 一个关于 Lance 表格式的讨论里（story 46933779，
我们用 HN 的 Algolia API 逐条核对过原文），用户 `fcanesin` 在 2026-02-10 写道：
「**My experience trying LanceDB has been abysmal.** It worked great on dev
and small testing environments but as soon we tried production workloads it
would get extremely slow. We shifted to PostgreSQL + pgvector and had
absolutely no issues, even if it is not "engineered for multimodal data".」
——原话紧接着还有一句保留意见，不该省：「**Maybe we were doing something
wrong but we did put effort in trying to make it work - it is this hard to
get it performant?**」；
两天后（2026-02-12）补充：「Inserts become increasingly slow. Became **>10sec
for a chat completion insert after 10_000 entries** on k8s Longhorn atop NVMe.」

**同一个帖子里有利的反证**，用户 `nickpadge`（2026-02-10）——
这条比前面那条更有信息量，因为它把"性能问题"拆成了三个具体的归因：
「I've used LanceDB for a few projects in production now and it's worked out
reasonably well. **The docs quality is spotty, and the lack of parity between
the async and sync python API is frustrating**, but otherwise it's been great.
The only performance issues I've had have been A) **not rebuilding indexes on
an appropriate cadence**, B) **not filtering the search space enough for queries
which bypass the index**, or C) **running search against millions of vectors on
an object storage and expecting millisecond latency**.」

这三条归因和本教程前面讲的内容是一一对应的：
A 对应第 7 章讲的"压实会改行号、索引要重建"，
B 对应"没有索引的行退化成暴力扫描"，
C 对应第 6 章讲的"OSS 没有内建缓存、冷查询结构性就是几百毫秒"。
**所以这位用户说的"用得还行"并不是"没有成本"，
而是"知道代价在哪、并且按代价去用"。**

以及**厂商的公开回复**（LanceDB 员工账号 `laminarflow027`，2026-02-10）：
「Great, thanks for the feedback! **I work at LanceDB and will take these points
into account (esp. the docs).**」

这几条摆在一起才是完整的信息：有一位用户在生产上失败了并迁走，
有一位用得还行但明确指出代价在哪里，厂商公开认领了文档问题。
**只引用第一条会失之偏颇，只引用第二条会掩盖风险。**

还有一条和"能不能自己绕开"有关的官方数据点：
2025-02-28 维护者在讨论区说 "We haven't looked into any RBAC like system yet"。
也就是说**如果你需要细粒度的权限，得自己建**，
而这件事在"能不能上生产"的评估里常常被低估。

### 29.6.9 一张风险检查清单

把上面所有条目压成一份可以直接放进立项文档的清单：

**先验证平台**：Python ≥ 3.10、glibc ≥ 2.28、CPU 有 AVX2
（否则 `import lancedb` 直接 `Illegal instruction`）、目标平台有 wheel
（Intel Mac 没有）。

**再验证格式**：生产表**写显式文件格式版本**，不要用 `stable` 别名；
绝不用 `next` / 2.3；跨版本混跑前读 `Compatibility Caveats`
（28.4 那个 FSL 全 null 的案例在老 reader 上是 **panic**）。

**再验证内存与 fd**：建索引的内存按"单个分区"预算，不是"总内存 > 数据集"；
长跑进程要监控 fd（`optimize()` 每次泄漏一个，issue 仍开着）和内存
（S3 直连泄漏开了 14 个月才关）。

**再验证并发模型**：用 `spawn` 不用 `fork`；写入者数量要克制
（重试上限 20）；`merge_insert` 未索引行上限 10,000；
多进程并发写不是强项。

**最后验证能力边界**：分布式检索、分布式建索引、
企业版训练缓存这三件事在官方文档里还写着 "**Coming soon**" ——
所以**不要在架构图里画上它们**。
分布式建索引的调度器要你自己写（官方 Warning 原文如此）。
自动 GPU 索引、SQL 分析、Geneva 是企业版。
RBAC / SSO / 审计要自己建。

**并且记住**：任何"LanceDB vs X"的性能对比，如果没有 VectorDBBench
或 ANN-Benchmarks 的支撑，都要按厂商口径处理。

## 29.7 小结

**要区分四种"未来"。** 已经能用（fuzzy 检索、multi-base、Blob V2、row lineage 的迁移）；
规范已写明但没落地（Fragment Metadata Tree、Data Overlay Files、MemWAL）；
公司愿景（多模态湖仓、"Lance 作为 AI 数据的 Iceberg"）；
以及推测。混在一起讲，"路线图"就失去信息量了。

**规范里那些实验特性写得比大多数产品的正式功能还细。**
Fragment Metadata Tree 有 feature flag（4096）和"已发布版本尚不支持"的明文声明；
MemWAL 有 shard / generation / SSTable / shard manifest 的完整定义，
连 WAL 文件名的位反转都解释了为什么；
Data Overlay Files 连"不支持它的 reader 必须拒绝而不是忽略"都写进去了。
**读规范是了解这个项目走向最靠谱的方式，比读博客准。**

**"Lance 覆盖三层而 Iceberg 覆盖两层"是这条愿景里最实质的部分。**
catalog 层的互操作（Iceberg REST / Unity / Polaris / Glue / HMS）是真实存在的，
"取代 Iceberg"不是。

**官方没有路线图页面，这是本章方法的起点。** 可靠的推断来源只有四处：
格式规范、两个仓库的 release notes / issue、官方博客与 newsletter、
以及 `llms.txt` 里的产品文档；第三方拼的"2026 路线图"一律不采信。
按这个口径，本章能确认的"方向"是：向量索引的下一代是 **IVF_RQ / RaBitQ**
（`approx_mode` 做查询时调参、多比特布局要重写才能升级）、catalog 层的下一步是
**Arrow Flight gRPC**、GPU 只在建索引侧而**查询侧没有公开的 GPU 路径**。

**GPU 索引不是未来，是 2023 年的功能**；只有"自动 GPU 索引"是企业版。
cuVS 是 commit-pinned 的 beta，**不替你建索引**，不在任何官方路线图里。

**分布式建索引的调度器要自己写** —— 这是官方 Warning 的原话。
三项企业版核心能力在文档里仍写着 "Coming soon"。

**FTS V3 是索引格式不是文件格式**，而且它现在是实验性的 opt-in
（`block_size=256`）。

**风险清单要写进立项文档**：平台在收缩（4 个 wheel、no Intel Mac、
manylinux_2_28、no sdist）、版本churn（10→13 只用 61 天、13.0.0 有 9 条
breaking changes）、建索引内存按"单分区可容纳"预算、
`optimize()` 泄漏 fd（issue 仍开着）、S3 直连内存泄漏开单 14 个月、
`fork` 不安全、`merge_insert` 未索引行上限 1 万、
企业版独占且"数据不锁但运行时会锁"。

**最后一条判断标准**：这个生态**没有第三方基准**（VectorDBBench 0 次、
不在 ANN-Benchmarks 里），所以任何性能对比都要标口径。
厂商手里最硬的证据是元数据操作的对比（10,000 次 S3 提交的延迟和失败率），
因为那测的正是这个项目在设计上真正与众不同的地方。

到这里，这个教程的三条线就完整了：
**格式怎么摆字节**（第 3–4 章）、**类型与存储怎么组织**（第 5–6 章）、
**版本与未来怎么演进**（第 7、28、29 章）。
剩下的章节都是把这三条线用在具体问题上：检索、索引、更新、
多模态、运维。它们能跑，是因为下面这三层是清楚的。
