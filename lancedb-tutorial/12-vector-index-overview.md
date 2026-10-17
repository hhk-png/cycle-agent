# 第 12 章　向量索引总览

前面讲了文件怎么摆、页里装什么、版本怎么管。从这一章开始进入查询路径。
第一站是向量索引：**它决定了你的召回率上限、内存下限和延迟方差。**

这一章不讲某一种索引的算法细节（那是后面几章的事），而是把全局摆清楚：
什么时候该建索引、LanceDB 到底提供哪几种、它们的取舍边界在哪、
以及一个所有人都会踩的坑——`_distance` 在量化索引上**不是真实距离**。

## 12.1 为什么需要 ANN：把"暴力检索到底有多慢"算清楚

暴力检索（brute force / flat scan）的复杂度是 `O(n·d)`：每个查询向量
要和库里每一条向量算一次距离。这个复杂度的完整推导在第 13 章 §13.1
（单核 10 GFLOP/s、100 万 × 768 维一次查询 154 毫秒），本节不重复，
只补上那里没有的**实测**。

我在本机（Windows 11，16 个逻辑 CPU，numpy 2.5.1 + 其自带的 BLAS）
测了 cosine 相似度下的暴力 top-10 检索。100,000 × 768 维（0.31 GB）
在 16 核 BLAS 上每条查询 **5.47 ms**；涨到 1,000,000 × 768 维（3.07 GB）
单线程 **230–251 ms**、16 核 **25.83 ms**——到这个规模，单查询 230 ms
已经不可接受，多线程 26 ms 也把整机所有核占满。反过来，规模降下来就
完全不是问题：**2 万行 64 维的暴力检索只要零点几毫秒/查询**
（`examples/04_index_benchmark.py` 的基线，本机两次运行 0.502 / 0.680 ms），
而且它的召回率是 1.00——**没有任何近似索引能在"又快又准"这两项上同时赢过它**。
说得更准一点：最激进档位的 ANN 确实更快（`IVF_FLAT nprobes=1` 约 0.14 ms），
但代价是召回掉到 0.85；`IVF_PQ nprobes=1` 只要 0.13 ms，召回却只有 0.42
（第 24 章 §24.3 有完整的旋钮对照）。所以准确的说法不是"暴力检索比任何索引都快"，
而是**在这个规模上，它是延迟-召回曲线里最划算的那个点**——
这就是"小表不要建索引"的物理依据。

最值得注意的不是毫秒数，而是**带宽**。1M × 768 维是 3.07 GB，单线程
跑 230 ms 意味着 13 GB/s——这已经是 DDR4 单通道到双通道之间的水平，
**说明暴力检索是内存带宽瓶颈，不是浮点算力瓶颈**。算力侧对应的数字是
6.1–6.7 GFLOP/s，远低于现代核心的峰值（单核 AVX-512 FMA 理论上能到
几十 GFLOP/s）。**你加更多的 ALU 没有用，瓶颈在把 3 GB 向量从内存
搬进 CPU 的路上。**（单线程与多线程数字是同一台机器、同一份数据、
只改 `OMP_NUM_THREADS` / `OPENBLAS_NUM_THREADS` 测的；100K 那一行的
单线程值按 1M 行的加速比外推，标为推算。"带宽" = 向量总量 ÷ 一批
10 条查询的总耗时，1M 单线程那一档是单条查询的数字。）

ANN（Approximate Nearest Neighbor）的目标不是"更准"，而是**用一点精度
换掉一个数量级的扫描量**：把 `O(n·d)` 变成 `O(候选数·d)`，候选数由索引
结构决定，通常是 `n` 的千分之一到万分之一。官方由此给出的口径是"几十万条
以内暴力检索通常够用、超过约 10 万就该建索引"，数字与出处见 §12.5。

## 12.2 ANN 的三大家族

所有 ANN 算法都可以归到三类里。理解这三类的取舍，
比记住具体索引名字有用得多。

**量化系：用一个更短的编码近似向量。**
把 768 维 float32（3072 字节）压成几十到几百字节，距离计算在这个
短编码上做。压缩比越高，距离估计的误差越大。
代表是 PQ（乘积量化，切成 `M` 个子空间、每个子空间独立 k-means）、
SQ（标量量化，每个分量独立量化）、BQ（二值量化，1 bit/维，
距离用 popcount）、以及 RaBitQ（每个向量只量化到"大约 1 bit/维"）。
量化本身**不减少候选数**——它只让每次距离计算变便宜。
所以量化通常和"分区"配合使用：IVF 先砍候选集，量化再砍每条候选的字节数。

**图系：把近邻关系预先连成一张图。**
HNSW 是事实标准：每个点被随机分配一个"最高层"，层数越高点越少，
查询从顶层贪心下降、在底层用 `ef` 大小的候选堆做束搜索。
它的优势是**召回率极高**（`ef` 拉高可以到 99%+）且
**支持真正的增量插入**；代价是内存大（图 + 向量）、建图慢。
同族还有 DiskANN / Vamana（为 SSD 优化）、SPFresh（质心索引，
对象存储友好，Turbopuffer 用的是这一系）。**这一族要区分"磁盘常驻"和
"内存驻留"**：DiskANN 把图放在 SSD 上、内存里只留压缩后的导航结构，
查询时按需读盘（内存映射 mmap 是常见实现手段），适合"向量装不进内存"
的场景。**LanceDB 没有这一类索引。** 它没有"图放盘上、按需读页"的查询路径——
索引文件在参与查询之前要被加载成一个内存里的结构，磁盘只是持久化的落点。
想省内存只能靠量化压小每条向量，不能靠"把索引留在盘上"。
所以把 LanceDB 和 Milvus 放在一起比时，"Milvus 有 `DISKANN`"
是一条真实的产品差异（第 27 章 §27.4）。

**但"加载"不等于"每次查询都重来一遍"，这一点要说清楚。**
加载过的索引会进一个**有容量上限的进程内缓存**
（老接口是 `index_cache_size`，0.40.0 上已弃用，改用
`Session(index_cache_size_bytes=...)`，见附录 A），
所以稳态下它是"常驻内存 + 有上限"，不是"每查一次读一遍盘"。
本实现有同一层缓存，但**没有上限**——第 10 章 §10.7 用
`examples/verify/ch10_index_cache.py` 把冷/热两次的读取量并排量了出来，
差值恰好等于索引文件的合计大小。
这个区别在选型上有实际后果：**索引总量超过缓存上限时，
LanceDB 会开始在索引之间换入换出，而 DiskANN 那一类不受这个约束。**
"装不下"这件事在两边表现不同——一边是内存不够，一边是随机读变多。

**哈希系：用哈希把相似向量映射到同一个桶。**
LSH 是这一系的代表，理论上漂亮（有概率保证），但在高维、
高召回目标下需要的哈希表数量会爆炸，实际性能被前两系碾压，
在主流向量库里基本退场。

三大家族的取舍可以压成一句话：**量化系减少的是"每条候选的字节数"，
图系和哈希系减少的是"参与的候选数"。** 内存上量化系最小（可以压到
原始向量的 1/32），图系最大（图结构加上完整向量），哈希系居中。
召回上限反过来：图系最高（HNSW 拉高 `ef` 可以到 99%+），量化系要靠
精确重排才追得上，哈希系最低。增量插入上量化系和 HNSW 都很容易
（前者只要编码，后者本来就是为增量设计的），建索引成本却相反——
量化系要跑 k-means 训练，图系建图慢但没有训练步，哈希系最低。
最后一条是过滤友好度：量化系和标量索引配合简单，图系会被过滤
"打断"（第 15 章展开），哈希系同样不友好。代表算法分别是
PQ / SQ / BQ / RaBitQ、HNSW / DiskANN / SPFresh、LSH。

LanceDB 走的是**量化系为主、图系作为子结构**的路线，
下面一节就是这条路线在 API 上的具体形状。

## 12.3 LanceDB 实际提供哪些向量索引

官方文档与 SDK 的 `Literal` 类型给出的完整清单是七种
（`lancedb.table.LanceTable.create_index` 的 `index_type` 参数逐字）：

```
'IVF_FLAT'  'IVF_SQ'  'IVF_PQ'  'IVF_RQ'
'IVF_HNSW_SQ'  'IVF_HNSW_PQ'  'IVF_HNSW_FLAT'
```

官方给的选型思路是按"优先级 → 索引 + 压缩比"排，**压缩比是它最实在的
一个卖点**：要最大压缩选 `IVF_RQ`（约 1/32 原始体积，Python 配置类
`IvfRq`）；小维度（≤256）下要更高精度选 `IVF_PQ`（压缩比按
`num_sub_vectors` 落在 1/64 到 1/16 之间，`IvfPq`）；召回率与延迟的
最佳平衡是 `IVF_HNSW_SQ`（体积略大于 1/4 原始，`IvfHnswSq`）；要最高
召回、干脆不量化就上 `IVF_HNSW_FLAT`（体积约等于原始向量加图开销，
`IvfHnswFlat`）；二进制向量配 hamming 距离用 `IVF_FLAT`（不压缩，
`IvfFlat`）。

**这里必须先做一次压缩比口径校准，否则这段话自己就矛盾了**：同一段里
`IVF_RQ` 被说成"最大压缩 1/32"，而 `IVF_PQ` 又落在"1/64 到 1/16"——
按数字 PQ 最低能压到 1/64，比 RQ 还狠，"最大压缩选 RQ"就站不住了。
根源是**那句"IVF_RQ 最大压缩"的参照系是 `IVF_SQ` / `IVF_HNSW_SQ`
（1/4 量级），不是 `IVF_PQ`**：SQ 系每维一字节（1/4），RQ（1 bit）每维一
bit（1/32），在"标量量化这一支"里 RQ 确实压得最狠。PQ 是另一条支线，它的
压缩比是 `4d / num_sub_vectors`（8 bit），由 `num_sub_vectors` 直接决定——
取到 `d/8` 时是 32 倍（1/32），取得更大就更小，所以它能超过 1/32。
**把三条支线的口径并排写清楚**：SQ 系 1/4 量级、RQ（1 bit）1/32、
PQ 由 `num_sub_vectors` 定（可以到 1/64 甚至更小，见 §14.2）。
"RQ 最大压缩"只在 SQ 系内部成立，跨到 PQ 系不成立。
（下面 §12.7 那组热态内存比值，用的也是"原始 float32 体积 = 1"这个
共同口径。）

**这里有一个几乎所有人都搞错的事实：HNSW 在 LanceDB 里不是顶层索引类型。**
官方文档的措辞是 "In LanceDB, **HNSW is not exposed as a top-level vector
index**. Instead, it's available as a substructure which further indexes the
selected vectors inside each IVF partition."

我在 0.40.0 上直接验证了这一点：

```
index_type='HNSW'      -> ValueError: Unknown index type HNSW
index_type='IVF_HNSW'  -> ValueError: Unknown index type IVF_HNSW
index_type='IVF_RQ'    -> OK, list_indices 里显示 index_type='IvfRq'
```

注意第二个：连 `IVF_HNSW` 这个不带量化后缀的字符串都被拒绝，
必须写成 `IVF_HNSW_SQ` / `IVF_HNSW_PQ` / `IVF_HNSW_FLAT` 之一。
所以**任何写"LanceDB 支持独立 HNSW 索引"的教程都是错的**。
想在 LanceDB 里用图索引，就得接受"IVF 分区 + 分区内建图"这个结构——
它换来的是分区级的并行与更好的可扩展性，代价是比"整个数据集一张图"
多了一层粗量化的误差来源。

另一条硬约束：**`IVF_RQ` 要求向量维度能被 8 整除。**
实测在一个 100 维的表上建 `IVF_RQ`，报错原文是：

```
ValueError: Invalid input, vector dimension must be divisible by 8 for IVF_RQ
```

256 维、768 维（都是 8 的倍数）没有问题。这一条在选型阶段就要检查——
如果你的 embedding 是 384 维（8 的倍数，OK）或 100 维（不行），
选型结论完全不同。

各类型适合的规模，可以按内存和精度排成一条谱。**10 万以下不建索引、
直接暴力**（理由见 §12.5）；**10 万到 100 万用 `IVF_SQ` 或 `IVF_PQ`**
——SQ 只占 1/4 内存且精度损失极小，PQ 用来压到 1/16 以下；**100 万到
1000 万用 `IVF_HNSW_SQ`**，召回率接近纯 HNSW，内存比它小一个量级；
**超过 1000 万（比如 1 亿 × 768 维）只能用 `IVF_HNSW_PQ` 或 `IVF_RQ`**
——内存装不下时只剩极限压缩一条路，而且**必须**配 `refine_factor`；
**高维（1536 / 3072 维）优先 `IVF_RQ`**，RaBitQ 在高维下比 PQ 更有
优势，且 `num_bits` 可调。

在 mini 实现（`mini_lancedb/index/__init__.py`）里，这条谱被写成一句
更本质的话：**索引选择不是"哪个准用哪个"，而是四个资源互相挤压——
内存、建索引时间、查询延迟、召回率。** 按"你手上最紧的资源"来选，
比按数据量来选更接近实际决策。

不管选哪一种，索引文件最后都落在表的 `_indices/` 目录下，按索引名分子目录：
mini 实现写的是 `_indices/<名字>/index.idx` 加一个 `auxiliary.idx`，
真实 LanceDB 是 `_indices/<uuid>/` 下的一组 `.lance` 文件——索引本身
就是用普通的 Lance 文件存的，所以它是"可移植、可管理"的，不需要为索引
单独设计一套存储。完整的目录布局见第 7 章 §7.1，
真实版本里能碰到的那几个存储版本参数在第 28 章 §28.2.1。

## 12.4 建索引的 API 与默认值

0.40.0 的 `create_index` 实际签名（用 `inspect.signature` 抓的，不是抄文档）：

```python
create_index(
    self,
    metric: str = 'l2',
    num_partitions: Optional[int] = None,
    num_sub_vectors: Optional[int] = None,
    vector_column_name: str = 'vector',
    replace: bool = True,
    accelerator: Optional[str] = None,
    index_cache_size: Optional[int] = None,
    num_bits: int = 8,
    index_type: Literal['IVF_FLAT', 'IVF_SQ', 'IVF_PQ', 'IVF_RQ',
                        'IVF_HNSW_SQ', 'IVF_HNSW_PQ', 'IVF_HNSW_FLAT'] = 'IVF_PQ',
    max_iterations: int = 50,
    sample_rate: int = 256,
    m: int = 20,
    ef_construction: int = 300,
    *,
    config: Optional[IndexConfigType] = None,
    wait_timeout: Optional[timedelta] = None,
    name: Optional[str] = None,
    train: bool = True,
    target_partition_size: Optional[int] = None,
)
```

这套签名同时支持两代 API。**新写法是把列名放第一个位置参数、
参数装进 config 对象**：

```python
import lancedb.index as li
tbl.create_index("vec", config=li.IvfPq(distance_type="l2", num_partitions=16,
                                        num_sub_vectors=32))
```

旧写法（`metric=` 放第一个位置参数 + 一堆裸参数）仍然能用，
但会走 legacy 分支。实测新写法的 config 类字段如下：

```
IvfPq       : distance_type, num_partitions, num_sub_vectors, num_bits,
              max_iterations, sample_rate, target_partition_size, accelerator
IvfRq       : distance_type, num_partitions, num_bits, max_iterations,
              sample_rate, target_partition_size, accelerator
IvfSq       : distance_type, num_partitions, max_iterations, sample_rate,
              target_partition_size, accelerator
IvfFlat     : distance_type, num_partitions, max_iterations, sample_rate,
              target_partition_size, accelerator
IvfHnswSq   : distance_type, num_partitions, max_iterations, sample_rate,
              m, ef_construction, target_partition_size, accelerator
IvfHnswPq   : distance_type, num_partitions, num_sub_vectors, num_bits,
              max_iterations, sample_rate, m, ef_construction,
              target_partition_size, accelerator
IvfHnswFlat : distance_type, num_partitions, max_iterations, sample_rate,
              m, ef_construction, target_partition_size
```

七个 config 类一一对上 §12.3 那七个 `index_type`（注意类名是 `IvfHnswSq` /
`IvfHnswPq` / `IvfHnswFlat`，但打印出来的类对象叫 `HnswSq` / `HnswPq` /
`HnswFlat`，别被 `inspect` 的结果绕晕）。两个差异点值得单独记：
**`IvfFlat` 是七个里唯一接受 `hamming` 的 `distance_type`**——它的取值是
`Literal['l2', 'cosine', 'dot', 'hamming']`，其余六个只有
`Literal['l2', 'cosine', 'dot']`（实测 `inspect.signature` 的注解，
本机 `lancedb 0.40.0`），这正是 §12.3 那条"二进制向量配 hamming 用
`IVF_FLAT`"在 API 上的落点；而 **`IvfHnswFlat` 没有 `accelerator` 字段**
（其余六个都有），也就是它不能走 GPU 建索引那条路。

有两个没写在签名里、却又到处都是的语义要先钉死。

**`distance_type`（旧写法叫 `metric`）决定"距离怎么算"，只有四个合法
取值。** `l2` 是**平方**欧氏距离——刻意不开方，排序只需要单调，开方是
纯浪费，而且真实 LanceDB 返回的 `_distance` 给的也是平方值（很多人
第一次看到会以为算错了）。`cosine` 是 `1 - cos(a, b)`：写入时就把
cosine 列归一化、查询时只归一化查询向量，归一化之后 `l2² = 2(1 - cos)`，
所以对归一化向量用 l2 索引查 cosine 结果是一样的。`dot` 是 `1 - a·b`
（**不是取负**）：这样"完全同向**且模长合适**"的向量距离接近 0，
和 l2/cosine 的值域大致对齐；排序结果和取负一致，但绝对值不一样，任何拿
`_distance` 做阈值判定的代码都必须按这个定义。那个"且模长合适"的限定
不能省（`mini_lancedb/index/metric.py` 的注释原话就是这八个字）：`a·b`
随模长线性放大，两个模长都很大的同向向量，`1 - a·b` 是一个很大的负数，
根本不是"接近 0"——所以 `dot` 的距离只在模长受控时才落在 `[0, 2]` 那
一带，脱离了模长前提就拿它当阈值用会静默地错。`hamming` 只用于二值向量，比较方式是
`popcount(xor)`，内存省 32 倍、速度通常快 5~10 倍，代价是召回率要靠
"多探分区 + 浮点重排"补回来。贯穿全章的约定是 **`_distance` 一律
"越小越好"**，所以内积这种"越大越好"的量必须先翻成距离再排序——
这个符号错误在只有 l2 的场景里看不出来，一切到 dot 就会静默地错
（定义在 `mini_lancedb/index/metric.py`）。

**`accelerator` 是 GPU 加速建索引的开关**：`"cuda"`（Linux）或
`"mps"`（Apple Silicon），打开后 k-means 训练交给 PyTorch、Rust 核心
负责把结果序列化成索引。这条功能不新（2023 年就有），官方自报的加速是
20~26 倍（厂商口径，Linux 上 CPU 323 秒 vs GPU 12.5 秒）；开源版里它
要手动开，**只有"自动 GPU 索引"是企业版独占**。注意这里没有"GPU 索引
类型"这回事——GPU 只是把**训练**搬上显卡，产物还是上面七种 IVF 索引
之一；别把 Milvus 的 `GPU_CAGRA`（NVIDIA cuVS 贡献的一等索引类型）
混进来（第 27 章 §27.4）。cuVS 在 LanceDB 里的确切状态在第 29 章
§29.3.1 有交代，目前是 commit-pinned 的 beta，不是能直接投产的路径。

几个容易搞错的参数语义：

`num_sub_vectors` **只对 `IVF_PQ` / `IVF_HNSW_PQ` 生效**（后者的"PQ"
指的就是分区内的乘积量化），默认是 `dimension // 16`；
维度能被 8 整除但不能被 16 整除时退到 `dimension // 8`
（768 维 → 48，128 维 → 8，96 维 → 6，24 维 → 3）。**两者都不整除时
会退到一个很不直观的值**（实测 100 维 → `1`，同时打一条 warning），
所以用非典型维度时**显式写这个参数**，别依赖默认。它同时决定压缩率和精度：
`num_sub_vectors=96` 意味着每条向量存 96 字节，
768 维 float32 的 3072 字节压到 96 字节，压缩 32 倍。

`num_bits` **只对 `IVF_PQ` / `IVF_HNSW_PQ`（默认 8）和 `IVF_RQ` 生效**。
对 `IVF_FLAT` / `IVF_SQ` / `IVF_HNSW_FLAT` /
`IVF_HNSW_SQ` 传它是**没有效果的**——这四个类型自带固定的量化方式。

RaBitQ 的位宽取值范围是 1~9：`1` 是纯符号位，多出来的位是额外的幅度信息
（机制见第 16 章 §16.6）。**要记的结论只有一条：Python SDK 的 `IvfRq()`
默认 `num_bits=1`**——用户不写这个参数时拿到的是 1 bit/维，不是官方博文里
那个 5-bit 索引；要复现博文里那组数字，必须显式传 `num_bits=5`。
（为什么 Rust 层的常量是另一个值、SDK 又为什么覆盖它，§16.6 一处讲全，
这里不重复。）

`sample_rate` 默认 `256`，是 k-means 每个分区的训练样本数；
`max_iterations` 默认 `50`。这两个是**训练质量**的旋钮，调大更准更慢。

`m` 默认 `20`、`ef_construction` 默认 `300`，只对 IVF-HNSW 系生效。
注意**官方文档说 `ef_construction` "Start at 150"，而 SDK 默认值是 300**——
两者不一致，引用时要说清是哪一边。

`target_partition_size` 是 `num_partitions` 的替代旋钮（按"每分区多少行"
反推分区数）；官方明确 **`num_partitions` 优先于它**。
两者都不给时，分区数由行数和索引类型自动计算。
**官方文档里没有出现 `num_partitions ≈ sqrt(N)` 这类启发式（未确认）。**
mini 实现里用的确实是 `sqrt(n)` 上限 256 的经验值，
但那是我们自己的实现选择，不要当成 LanceDB 的行为来写。

`train` 默认 `True`，是这套签名里最容易被忽略、又最该解释的一个布尔值。
**`train=False` 的语义是"只把这份配置登记成一个索引、不真的去训练它"。**
我在本机 0.40.0 上验证过（2000 行 × 8 维、`IvfFlat`、`train=False`）：
它**立即返回、不报错**，`list_indices()` 里能看到这个 `IvfFlat` 索引，
但 `index_stats()` 显示 `num_indexed_rows=0, num_unindexed_rows=2000`、
`size_bytes=0`——配置挂上去了，训练一次没跑。这正好接上 §12.6 那套
"异步构建"的语义：在远端 / Enterprise 表上，可以先 `train=False` 把索引
登记住，训练交给后台的刷新（或者之后显式触发），所以它不是"调试用的
跳过训练开关"，而是"先登记、后训练"这个两段式的入口。在本地表上很少用到，
但读签名时要知道它是这个意思。

最后一条容易踩的坑：**`create_scalar_index` 已经废弃。**
在 0.40.0 上调用它会得到：

```
DeprecatedWarning: create_scalar_index is deprecated as of 0.25.0.
Use create_index() with config=BTree()/Bitmap()/LabelList() instead.
```

顺带一个有用的能力：**同一列上可以挂多个索引。**
`replace=False` + 显式 `name=` 就能做到，实测结果：

```
[(name='my_sq',  index_type='IvfSq'), (name='vec_idx', index_type='IvfHnswSq')]
```

默认索引名是 `<列名>_idx`（列叫 `vec` 就是 `vec_idx`）。

**既然能挂多个，就必须知道怎么摘掉一个。** 建索引讲了三章，
删索引一句话：`drop_index(name)`，**按名字删，没有按列删的接口**。
实测这三个动作连起来看一遍：

```python
t.create_index("id", config=BTree())
t.list_indices()          # [(name='id_idx', index_type='BTree')]
t.drop_index("id_idx")
t.list_indices()          # []
```

三件事值得记。**第一，删不存在的索引会报错，不会静默成功**——
实测是 `RuntimeError: lance error: Lance index not found: name=nope`。
这和 `index_stats()` 对不存在的索引**返回 `None` 而不抛**是相反的
（附录 A 的 A.11 记了这个不对称），所以两个接口的调用姿势不能互相套用：
删之前先 `list_indices()` 对一遍名字，比 `try/except` 干净。

**第二，`drop_index` 只改元数据，不回收空间。** 索引文件从 manifest 里
摘掉之后，数据文件还在盘上，要等 `cleanup_old_versions` 的保留窗口
过去才会被 GC 掉（第 23.5 节）。**"删了索引磁盘就空了"是个假直觉**，
重索引频繁的场景里这会积成一笔看得见的存储账。

**第三，这条报错顺带把底下的 Rust crate 版本露出来了**——
错误信息里写着 `lance-13.0.0`。这就是第 0.6 节说的"三种版本号"
里 Rust crate 那一种，它和 `lancedb 0.40.0` 是两个独立演化的号。
排查真实问题时，**报错串里的 crate 路径和行号**往往比 Python
这一层的 traceback 更有定位价值。

## 12.5 什么时候该建索引：约 10 万向量，不是 256

关于"什么时候建索引"，流传最广的一条说法是
"**少于 256 行不用建索引**"。这条说法**在官方文档里找不到**，
数字 256 出现在完全另一组语境里：

- `sample_rate` 的默认值是 256（k-means 每分区的训练样本数）；
- GPU 索引示例里用 `num_partitions=256` / `num_sub_vectors=96`；
- LangChain 集成的 `create_index()` 里 `num_partitions` 默认 256；
- 实验性 FTS V3 的 `block_size` 可以是 256（**这个参数的默认值是 128**，
  256 只是它支持的另一个取值、用来切到 V3 布局，见第 28 章 §28.2.1）。

也就是说 256 是**几个不同参数的默认值或候选值**被误传成了"建索引的门槛"。
官方口径是 **~100K**：

- "For datasets up to a few hundred thousand vectors, LanceDB's highly
  efficient kNN (brute-force) retrieval of nearest neighbors is often
  sufficient."
- "That works fine below ~100K vectors, but past that you should build a
  dedicated vector index."
- "An ANN index is usually not needed for datasets ~100K vectors.
  For large-scale (>1M) or higher dimension vectors, it is beneficial
  to create an ANN index."

那么"LanceDB 内部是不是真的在小表上自动退化为暴力检索、并且阈值就是
256"？**这一点未确认**——文档没有描述这种自动退化，
我也没有在 0.40.0 上找到对应行为。

有一个例外要单独记：**多向量（multivector / late-interaction）场景要更早
建索引。** 官方原文是 "Multivector brute-force scales with
rows × vectors-per-row, so build the index at **much smaller dataset sizes**
than you would for single-vector search."。
一行挂 100 个向量的话，2 万行就等价于 200 万条单向量，
门槛要按这个折算。而且**多向量列对索引类型有硬约束**：目前只支持 cosine
距离、标准做法是 `IVF_PQ`，用 l2 建多向量索引会被直接拒绝（HNSW 系列和
`IVF_FLAT` 是否支持，文档没有说明，标为未经确认）。这些细节、以及
"多向量不是多个向量列"的区分，都在第 21 章 §21.1。阈值这一项也和普通表
不同：文档说多向量列的行数阈值大约比单向量列**低 10 倍**，所以它要更早
建索引是双重原因——暴力成本更高，官方给的安全线也更低。

## 12.6 索引构建：文档说异步，本地实测是同步的

这一节的标题原来写的是"索引构建是异步的"，**那句话对文档成立、
对本机实测不成立**，所以改掉了。下面把两边并排摆出来，
**读者要按自己用的是哪条路径去选相信哪一边**——这正是全书
反复在做的区分。

官方文档的说法很清楚：

- `create_index()` **立即返回**，向量索引在后台异步构建；
- 要等构建完成，可以传 `wait_timeout`，或者之后调
  `wait_for_index(...)`；
- **建索引之后追加进来的行不在索引里**，直到手动刷新（OSS）
  或自动刷新（Enterprise）；
- 这些行仍然能被普通检索查到，走的是**更慢的兜底路径**（暴力）；
- `fast_search()` **跳过兜底路径，只搜已索引的数据**。

**但在本机的 0.40.0 本地表路径上，实测 `create_index()` 是同步的。**
我用 6 万行 × 128 维的表、`IVF_PQ` 64 分区，即使显式传
`wait_timeout=timedelta(seconds=0)`，调用仍然阻塞了 **6.39 秒**，
返回后 `index_stats` 立刻显示 `num_indexed_rows=60000,
num_unindexed_rows=0`。另外两次实测：2 万行 × 256 维 IVF_PQ 用 7.18 秒，
6 万行 × 128 维 IVF_HNSW_SQ 用 8.19 秒，都是返回即完成。

所以可以这么说：**"异步构建"这套语义描述的是远端 / Enterprise 表；
在本机 0.40.0 的本地表上，`create_index()` 表现为同步调用。**
（关于远端表的行为本轮未实际验证，标为未经确认。）

`index_stats()` 是监控索引覆盖率的入口，实测输出长这样：

```python
>>> tbl.index_stats("vec_idx")
IndexStatistics(num_indexed_rows=20000, num_unindexed_rows=0,
                index_type='IVF_PQ', distance_type='l2', num_indices=1)
```

`num_unindexed_rows` 就是"还没进索引的行数"。持续写入的表上，
这个数会一直不为零；官方的建议是
"For tables with continuous ingest, schedule it on a cadence that keeps
`num_unindexed_rows` close to zero."。

`num_unindexed_rows` 之外还有一层更容易忽略的有效性：**索引是挂在某个
版本上的，旧版本上它算不算数要单独判断。** 索引本身不可变，
它记的是"建索引那一刻的行"，所以两类操作会让它失效——
一是**追加**（新行不在索引里，就是上面说的 `num_unindexed_rows`），
二是**删除 / 压实**（行号被重映射，索引里记的逻辑行号不再成立，
第 9 章 §9.4 的幽灵行和 `IndexError` 就是这条路的后果）。真实 Lance 用
行地址（`(fragment_id << 32) | offset`）加删除文件过滤来扛住删除，
压实之后则要么重映射、要么把索引标记失效并重建（第 7 章 §7.8）。
对时间旅行要格外小心：**checkout 到一个旧版本时，那个版本上不存在
当年还没建的索引**，查询会退回暴力路径——这不是 bug，是"索引跟着
版本走"的必然结果。

**`fast_search()` 的陷阱值得单独演示一遍。** 我在 5000 行的表上建好索引后，
追加一行"和查询向量完全相同"的数据（`id=999999`），然后对比：

```
普通 search     : ids=[999999, 429, 4734]  _distance=[0.0, 177.2164, 202.0356]
fast_search()   : ids=[429, 4734, 1776]    _distance=[155.1182, 178.3393, 182.1742]
```

那一行**在 `fast_search()` 下彻底消失了**——它不在索引里，而
`fast_search()` 只搜索引。更危险的是它**不报错、不警告、不返回任何信号**，
只是安静地少一条。同样一行代码在刷新索引前后返回不同结果，
这是生产事故的标准配方。

所以 `fast_search()` 的正确用法只有一种：**你明确知道索引是最新的
（`num_unindexed_rows == 0`），并且愿意用"少结果"换掉那一段暴力兜底延迟。**
mini 实现里对这一点写了一句很直白的话：只要用了 `fast_search()`，
就必须有测试断言它返回空结果或返回索引内结果——
因为它失败的方式是"安静地正确不了"。

## 12.7 召回率与延迟的权衡：四个旋钮

调参这件事可以归到四个旋钮上。前两个是"扫描范围"，后两个是"打分精度"。

**旋钮一：`nprobes`（IVF 系列）。** 查询时探几个分区。
官方默认值是 **20**，这一点可以从执行计划里直接读到——
我跑 `analyze_plan()` 时输出里有：

```
ANNIvfPartition: minimum_nprobes=20, maximum_nprobes=Some(20)
```

`nprobes` 直接决定扫描量：分区数 `P` 时，扫描量约为 `nprobes/P · n`。
官方的调参建议是"**默认别动它**"：LanceDB 会自动调，
只有在召回率不达标、或者你有明确的性能余量时才手动覆盖。
另外有 `minimum_nprobes` / `maximum_nprobes` 的组合用法：
**带 `where(...)` 过滤时，LanceDB 先扫 `minimum_nprobes` 个分区，
结果不够再放大到 `maximum_nprobes`**；想固定住就两个设成一样。

**旋钮二：`ef`（IVF-HNSW 系列）。**
官方建议"**先保持默认 `nprobes`，优先调 `ef`**"，
起点约 `1.5 × k`，最高到 `10 × k`。

**旋钮三与四：`refine_factor` 与 `approx_mode`。**
`refine_factor` 的机械含义是"索引先取回 `k × refine_factor` 个候选，
再对这些候选的完整向量（不是量化编码）重算距离并重排"——它是本章最
容易被误解的参数，完整的语义（为什么"放大 k + 精确重排"两步缺一不可、
`Some(0)` 会被显式拒绝）放在第 13 章 §13.5 展开，这里只钉一个结论：
**四种查询模式下结果里的 `_distance` 语义不同。**

无索引、或者 `.bypass_vector_index()` 时是精确 kNN（100% 召回），
`_distance` 是完整向量上的真实距离。有量化索引但不设 `refine_factor` 时
结果是近似近邻，`_distance` 是**索引表示上的距离**——平坦索引下精确，
量化索引下是近似值。设了 `refine_factor(>1)` 时既扩候选集、又用完整向量
重算距离。这条语义链是本章最有价值的一处：**只有在量化索引上显式设了
`refine_factor > 1`，`_distance` 才是真实距离**，任何依赖距离阈值的产品
逻辑都必须走这条路。

**这里有一个我们自己的实现和真实 Lance 分道扬镳的点，值得单独说。**
真实 Lance 把 `refine_factor` 建模成 `Option<u32>`：`Some(1)` 是合法的，
语义是"取回 `k × 1 = k` 个候选，再对这 k 个用完整向量精排"——
候选集不变，但距离被换成真实值。本实现没有照搬这一档，
精排的门槛写成了 `refine > 1`，于是 **`refine_factor=1` 在本实现里是
空操作**，输出和完全不设这个参数逐位相同。

三处索引里的判断**并不完全一样**，写代码的人要按各自的形状去看：

```python
mini_lancedb/index/ivf.py:300       if refine > 1 and not self.exact_candidates:
mini_lancedb/index/ivf_hnsw.py:164  if refine > 1:
mini_lancedb/index/bq.py:242        if refine > 1:
```

`ivf.py` 多出来的那个 `not self.exact_candidates` 不是笔误：
`IVF_FLAT` 的桶内本来就是精确距离（没有量化），**对它做"精排"
是同一次计算做两遍**，所以被显式短路掉了——§13.5 里
"`refine_factor` 对 `IVF_FLAT` 没有意义"说的就是这件事。
也就是说 `refine_factor=1` 在 `IVF_FLAT` 上是**双重空操作**
（门槛没过 + 本来就不需要），而在量化索引上是单纯没过门槛。
结论一样（都是空操作），成因不同——**这两件事在排查时指向不同的代码**。

写 `> 1` 而不是 `>= 1` 的动机很朴素：`k × 1` 并没有"放大"任何东西，
把这一档做成静默的重排会让人误以为"设了 1 就有精确距离了"。
但代价是——**照着真实 Lance 的文档写 `refine_factor=1` 的代码，
在本实现上不会报错，只会安静地拿不到精确距离。**
用本实现的时候，要么不设（接受估计距离），要么直接写 ≥ 2。

下面这份输出由 `examples/verify/ch12_refine_factor.py` 产生
（20,000 行 × 256 维随机向量、`IVF_PQ` 16 分区 32 子向量、
查询向量**就是库里的第 42 行**，所以真值里 `id=42` 必须是 `0.0`）。
脚本的随机种子写死成 0，逐位可复现：

```
暴力真值          : [(42, 0.0000), (213, 359.7981), (4622, 362.8711), (14219, 366.0667), (15510, 370.5474)]
默认 (nprobes=20) : [(42, 71.4431), (6829, 317.0914), (4153, 318.3803), (5336, 320.7949), (9097, 321.9358)]
refine_factor(1)  : [(42, 71.4431), (6829, 317.0914), (4153, 318.3803), (5336, 320.7949), (9097, 321.9358)]
refine_factor(20) : [(42, 0.0000), (15510, 370.5474), (14428, 371.1369), (4692, 371.8015), (8720, 372.1078)]
nprobes(1)        : [(42, 71.4431), (5682, 323.0404), (4980, 333.6306), (9355, 335.9933), (3322, 336.5832)]
```

从这份输出里能读出四件事：

1. **默认模式下，`_distance` 是量化编码上的估计值。** 完美的精确匹配
   显示的是 `71.4431` 而不是 `0.0000`。任何拿 `_distance` 做阈值判定
   （比如"距离小于 X 才算命中"）的代码，在有量化索引时**判断依据是错的**。
2. **`refine_factor(1)` 那一行是空的。** 它和默认行**逐字符相同**——
   脚本里专门打了一行 `refine_factor(1) 与默认逐位相同吗: True` 来钉这件事。
   如果你在别处读到"设 1 就能拿到精确距离"，那说的是真实 LanceDB，
   不是这里这个实现。
3. **`refine_factor(20)` 才是真的动手了。** 候选从 5 条（`k=5`）扩到
   100 条，再用完整向量重排，于是 `id=42` 变回 `0.0000`，
   它旁边那几条也从"看起来差不多"变成了真值里真实存在的邻居
   （`15510` 就在真值第 5 位）。按 `recall@5` 算，默认 0.2、
   `refine_factor(20)` 是 0.4——**翻倍，但仍然远不是 1.0**，
   因为扩候选只能修正"桶内排序"，修正不了"近邻根本不在探的桶里"。
4. **`nprobes` 的作用方向完全不同，而且这个方向很反直觉。**
   把 `nprobes` 压到 1（16 个分区里只探 1 个）时，精确匹配的那一条
   （`id=42`）**仍然稳稳在结果里**——因为查询向量自己就落在它被分配
   的那个分区里。可 `recall@5` 掉到 0.2，也就是说真值 top-5 里
   除了它自己，另外 4 条**一条都没回来**；它们散布在没被探到的 15 个
   桶里。这正好说明 IVF 的误差有两个来源，而这一组对照把它们分开了：
   默认行的 0.2 是**量化误差**（16 个分区、`nprobes=20`，其实已经把
   所有桶都探了一遍，等于全表扫描，但仍然只有 0.2——错的不是"漏看了
   哪些桶"，是"桶内距离算得不准"）；`nprobes(1)` 的 0.2 则是纯粹的
   **漏桶**。前者的解药是 `refine_factor`，后者的解药才是 `nprobes`——
   这也是为什么第 24 章的调参流程要求先固定一个、再动另一个。

官方给的 `refine_factor` 推荐区间是 **5–50**，
并附了一个可核查的实测口径：在 **GIST-1M** 数据集上，
用 **~50 probes + `refine_factor=50`** 可以在多数机器上做到
**>0.95 召回、延迟 < 10 ms**。

`approx_mode` 是 `IVF_RQ` 独有的**查询时**开关，**改它不需要重建索引**：

| 取值 | 行为 |
|---|---|
| `fast` | 偏向低延迟，召回可能下降 |
| `normal` | 默认的延迟/召回平衡 |
| `accurate` | 偏向高召回，延迟可能上升 |

**但它在 Python API 上是够不着的，这一点本机实测过，必须说清。**
`inspect.signature(IvfRq)` 上只有
`distance_type / num_partitions / num_bits / max_iterations /
sample_rate / target_partition_size / accelerator` 这几个参数，
**没有 `approx_mode`**；而且整个 `lancedb` 安装包的 `.py` 里
搜不到这个名字（只出现在编译好的扩展里）。所以这个开关存在于
Rust / pylance 那一层，**本教程瞄准的 0.40.0 Python 接口上没有它的入口**。
上面的表格和下面的数字因此只能当"官方说它有这三档"来读，
**不能当"你可以这么调"**——想用它得走 Python 之外的路径。
（对照一下第 12 章 §12.6 那条 `create_index()` 同步/异步的实测：
那里能给结论是因为本机能跑；这里跑不了，所以只到"文档说有"为止。）

官方 2026-07 的 RaBitQ 博文给了一个 **5-bit** 索引在三档下的实测
（单核、top-k=10、`nprobes=24`、不做全精度 refine）：
`fast` **81.4% 召回 / 3.2 ms / 约 315 QPS**，
`normal` **96.1% / 3.8 ms / 约 263**，
`accurate` **96.6% / 4.5 ms / 约 222**。
**这里必须说清口径：那是 5-bit 索引。** 而 Python SDK 的 `IvfRq()`
默认给的其实是 **1 bit/维**（`inspect.signature(IvfRq)` → `num_bits: int = 1`，
本机 `lancedb 0.40.0`），所以不显式传参时你拿到的不是博文里那个 5-bit 索引，
想复现上面这组数字得显式传 5。这个"两层默认值"的完整来历见第 16 章 §16.6，
这里只用一句带过。

位宽为什么重要：三档 `approx_mode` 的差别只在 `num_bits > 1` 时存在——
把位宽降到 1（纯符号位）时 `normal` 和 `fast`
会走同一条一比特打分路径，**因为 fast 路径本身就是那个一比特路径**，
`accurate` 也没有额外的幅度信息可用。换句话说，"三档可调"这个能力
是跟着多比特位宽一起来的。

顺带一组同源的对比数字，说明"量化方式选对"比"参数调好"更重要：
同一篇博文里，单核、`top-k=10`、`nprobes=24`、不做 refine 的条件下，

| 索引 | recall@10 | 平均延迟 | p99 | QPS/核 |
|---|---|---|---|---|
| `IVF_PQ`（不 refine） | 74.8% | 12.8 ms | 14.7 ms | 78.0 |
| `IVF_RQ` 3-bit | 93.5% | 3.8 ms | 4.7 ms | **261.0** |
| `IVF_RQ` 5-bit | **96.2%** | 4.6 ms | 5.6 ms | 218.1 |
| `IVF_RQ` 7-bit | 96.8% | 5.0 ms | 5.9 ms | 200.7 |

（厂商口径，2026-07-10。官方自标"QPS 为每核 QPS，不是整机吞吐"，
且 `approx_mode` 那组 QPS 是由 `1000/平均延迟` 反推的相对值——
按表里的延迟反推，3.2 ms 应该是 312.5 而不是 315、3.8 ms 是 263.2
而不是 261.0，**这些零头对不上**，说明原始延迟表格是有小数的、
这里抄的是四舍五入后的值。不影响量级，但别拿这几列做回归。）

**还有一处要提醒：这张表的 3-bit / 5-bit / 7-bit 三种位宽，
和官方文档给的 `num_bits` 取值集合对不上。** 文档里列的是
**1 / 2 / 4 / 8**，没有 3、5、7，并且说明 `num_bits >= 2` 会启用
一套新的磁盘布局。这篇博文用的位宽可能是它自己那版的实验配置，
也可能是在描述布局内部的参数而不是用户可见的 `num_bits`——
**本机没有复现过这组数字**，也不打算把"5-bit 是一个合法配置"
当成结论传下去。表留在这里，是因为它把"位宽越大、延迟越高、
召回越高"这条趋势展示得很清楚；**具体数值只当趋势读**。

还有一组热态内存的数字，它解释了 RaBitQ 想解决的问题：
`IVF_PQ` 不 refine 时约占原始向量的 **1/32**，
5-bit `IVF_RQ` 走一比特快路径也是 **1/32**，
但 5-bit 多比特打分涨到 **5/32**，而 **`IVF_PQ` + refine 是 33/32**——
**比原始向量还大。** "PQ + refine"这套组合的内存收益在重排这一步被吃掉了。

**先说清这组分数的口径，否则数字会被误读。** 这里的"1"是**原始 float32
向量的体积**，也就是每维 32 bit——所以 1/32 是"每维 1 bit"、5/32 是
"每维 5 bit"，都是**按位宽直接算出来的**，与维度无关。只有 `IVF_PQ` 那一项
要落到具体配置：`4d / num_sub_vectors = 32` 反推出 `num_sub_vectors = d/8`
（8 bit），768 维时就是 `num_sub_vectors=96`——正是 §12.4 里"3072 字节压到
96 字节"那个配置。33/32 是"1（保留原始向量用于 refine）+ 1/32（PQ 码）"，
所以它比原始向量还大不是笔误。官方的博文没有逐一标注维度和
`num_sub_vectors`，这里按 `1/32 = d/8` 这条关系还原口径；比值的**量级**
（SQ 系 1/4、RQ 1-bit 1/32、5-bit 5/32）不依赖这些细节。

## 12.8 一套可复用的调参方法

ANN 调参最容易犯的错，是在两个误差源之间反复横跳：
调了 `nprobes` 发现召回涨了，又去调 `refine_factor`，
然后不知道涨的到底来自哪一边。mini 实现里给了一条实验方法论，
值得照抄：

1. **先用 `IVF_FLAT` 量出"粗量化的天花板"。**
   它不压缩，所以这个 `nprobes` 下能达到的召回率就是分区裁剪造成的
   全部损失。这一步是基线。
2. **换上量化器（SQ / PQ / RQ）再看掉多少。** 掉下来的部分才是**量化误差**。
3. **最后用 `refine_factor` 精排，看能补回多少。** 补不回来的部分，
   才是这套索引组合真正的召回率损失。

跳过第 1 步直接调参，你就分不清"是桶没探够"还是"编码太粗"——
而这两件事的解法完全相反：前者加 `nprobes`（扩大扫描量），
后者加 `num_sub_vectors` 或换 `num_bits`（重建索引）。

走这套流程的前提是**召回率算得对**，而召回率的 ground truth 只有一个来源：
**精确暴力检索。** 对每条查询先用 `.bypass_vector_index()`（无索引的表
或这个开关）算出真正的 top-k 集合，再数 ANN 结果命中了几个、除以 k，
`recall@k = |ANN_k ∩ BF_k| / k`。这一步的暗礁在于方向：真值的取法必须和
查询侧一致——`_distance` 是"越小越好"，所以暴力侧取的是最小优先的 top-k
（`mini_lancedb/index/metric.py` 里 `top_k` 的默认分支）；
如果把内积当"越大越好"来取真值，方向就反了。mini 实现把这一整套固定成
`recall_against_bruteforce()`（`mini_lancedb/index/base.py` 导出），
省得每次手写都漏掉一边。评测里所有"recall@k"的数字都来自它。

配套要有几个不变量当测试断言，否则调参会在"召回率涨了但结果是错的"
这类问题上耗掉几天：

- 无索引 / `bypass_vector_index()` 的结果必须是真值，任何 ANN 结果
  都拿它当基准算召回率；
- `refine_factor` 调到足够大时，召回率必须单调逼近真值——
  如果调大到 200 召回率反而掉，说明 `nprobes` 是瓶颈，候选池里
  根本不存在正确的那些行，再重排也没用；
- `_distance` 只有在量化索引上设了 `refine_factor > 1` 时才是真实距离，
  任何依赖距离阈值的产品逻辑都必须走这条路。**门槛要写成 `> 1` 而不是
  `>= 1`**：真实 Lance 接受 `Some(1)`（候选集不变、只换算法），
  本实现把它当空操作（§12.7 讲过分歧在哪）。

## 12.9 小结

把这一章压缩成六句话：

**暴力检索的门槛是内存带宽，不是算力。** 100 万 × 768 维在单线程上要
230 ms/查询，16 核 BLAS 也要 26 ms，而带宽利用率只有 13 GB/s。

**官方建议的建索引门槛是约 10 万向量**，不是流传的 256——
256 是 `sample_rate` 的默认值、若干集成里 `num_partitions` 的默认值，
也是 FTS `block_size` 的一个候选值（那个参数的默认值其实是 128），
跟"建索引门槛"没有关系。

**LanceDB 只有 IVF 系索引，HNSW 不是顶层类型**，它只能作为 IVF 分区
内部的子结构出现（`IVF_HNSW_SQ` / `IVF_HNSW_PQ` / `IVF_HNSW_FLAT`）。

**七种索引类型的取舍，本质上是内存换召回率**：
从 `IVF_FLAT`（不压缩、最准）到 `IVF_RQ`（1/32、需重排）是一条连续的谱。

**建索引后的新数据不在索引里。** `num_unindexed_rows` 是唯一可信的
监控指标；`fast_search()` 会**静默**跳过这些行，这是它的设计意图，
也是它最大的风险。

**量化索引上的 `_distance` 不是真实距离。** 实测里一个精确匹配的
距离是 `71.4431` 而不是 `0.0`（§12.7 那份输出）。想要真实距离就把
`refine_factor` 设到 **≥ 2**——注意不是设成 `1`：本实现的门槛是
`> 1`，`refine_factor(1)` 的输出和默认行逐字符相同，是个空操作。
真实 Lance 接受 `1`（语义是"候选集不变、只换算法"），
照着官方文档写 `refine_factor(1)` 的代码在这里**不会报错，
只会安静地拿不到精确距离**。想要更高召回就用 5–50 的 `refine_factor`。

后面几章会逐个拆开这些索引：第 13 章的 IVF 分区与重排、第 14 章的
PQ 子空间分解与 ADC 查表、第 15 章的 HNSW 分层图与 diversity pruning、
第 16 章的 SQ / BQ 与 RaBitQ。**RaBitQ 就是 LanceDB 的 `IVF_RQ`**，
它讲的是"多比特幅度"在高维下为什么比 PQ 更划算（第 16 章 §16.6）。
需要说明的是，**除了 RaBitQ，另一族常被并提的 RVQ（残差向量量化）
不在本教程范围内**——它没有出现在 LanceDB 的索引类型里，这里不展开。
