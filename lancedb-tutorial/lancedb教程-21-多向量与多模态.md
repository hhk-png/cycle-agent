# 第 21 章　多向量与多模态

多向量列在 Lance 里的类型写作 `fixed_size_list<fixed_size_list<float16>[128]>[N]`
——比普通向量列多出一层嵌套。这一章把那个"多出来的一层"讲透，
然后顺着它走到另一个方向上：如果一行的内容根本不是文字，
而是一张图、一段音频、一个视频呢？

## 21.1 单向量表示的信息瓶颈

一个双塔编码器把一段文本映射成一个固定长度的向量。这个动作里藏着
一个不可协商的取舍：**输出维度是常数，输入长度是变量。**

一段 2000 字的文档，按中文约 1.5 字/token 估，约 1300 个 token。
模型要把它压成 768 个数。这 768 个数要同时承担三件事：**这一段大概
在讲什么**（主题）、**这段里有哪些具体信息**（细节）、**这些信息是
怎么措辞的**（用词）。三件事抢同一块固定带宽，必然有人出局。

出局的通常是细节。因为训练目标（对比学习）优化的是"这段文本和它
对应的正样本互相靠近"，而一段 2000 字的文档可能被用来回答 50 个
不同的问题——**任何单一的向量表示都只能是这 50 个问题的一个折中**。
折中的结果就是：主题还在，细节糊了。

这不是"模型不够好"，是维度不够。而维度和长度之间的差距会随着
文档变长而放大：768 个数覆盖 2000 字，和覆盖 200 字，
密度差了 10 倍。

### 文档级检索比段落级难

把这个问题放到 RAG 的语境里，它的形状就变了。

"找出用哪几段话回答这个问题"（段落级检索）和"找出用哪几篇文档
回答这个问题"（文档级检索）不是同一个难度的问题。前者要的是
**局部对齐**——查询里的这个词对应这段话里的那句话；后者要的是
**全局主题匹配**。

单向量做不了局部对齐，因为它在编码的时候就丢掉了位置结构：
`文档的前半部分在讲 A、后半部分在讲 B` 这件事，压进一个向量之后
只剩"它讲了 A 和 B"。于是用户问 A 的细节时，一篇"70% 讲 B、
30% 讲 A"的文档和一篇"100% 讲 A"的文档，向量相似度可能很接近。

解法的方向很自然：**别压。让表示的容量随长度线性增长。**
一个 256 token 的段落留 256 个向量，一个 512 token 的留 512 个。
这就是多向量（multi-vector / late interaction）的出发点——
ColBERT 那一套，MaxSim 的公式与手算见本节最后两个小节。

### "多向量"不是"多个向量列"，是一行里存一个向量的集合

这里有一个概念上的坑，值得单独点出来。

一张表里**可以**有多个向量列。比如同时存 `title_vector` 和
`body_vector`，用不同的 embedding 函数编码不同的源列。这是
"多个向量列"，它是 schema 层面的事，每列各自独立地建索引、各自
独立地查。

多向量是另一回事：**一行的一个列里，存着一个长度可变的向量的集合。**

```
row 1  ┌ text: "向量检索擅长语义..." ┐
       └ mv:   [[0.1, -0.2, ...],    ← token 0 的向量
                [0.3,  0.1, ...],    ← token 1 的向量
                ...                  ← 共 n 个，n 随行而变
               ]
```

一行 = 一个段落，一个段落 = n 个 token 向量。**这一行的"向量维度"
是变化的**（n 不同），所以它不能参与普通的 ANN 索引——
这就是为什么多向量需要一条专门的检索路径，也是为什么它的类型
必须多一层嵌套。

两者的工程含义完全不同。"多个向量列"增加的是**列数**，
存储代价是线性的（多一列就多一个向量）；"多向量"增加的是
**每行的向量数**，存储代价随段落长度线性增长——倍数怎么算见本节
末尾的「存储代价」小节：**一行有 n 个 token，就是 n 倍**。

### 多向量怎么打分：MaxSim 与晚交互

双塔检索把一篇文档压成一个向量，然后在查询向量和它之间算一次相似度，
一个数就是全部结论。ColBERT（Khattab & Zaharia, 2020）换掉了这一步：
**每个 token 各留一个向量**，再用 late interaction 打分。

```
score(q, d) = Σ_i  max_j  (q_i · d_j)
              ↑ 每个查询 token   ↑ 文档所有 token 里最像的那个
```

逐项读。内层 `max_j` 问的是"文档里哪个 token 最像这个查询 token"——
也就是**定位**：它回答的是"这个词落在段落的哪一段上"，而不是
"整体像不像"。外层 Σ 要求查询的**每个** token 都在文档里找到归宿，
找不到就贡献 0，所以**漏掉一个查询词要扣分**。

整个式子**先算相似度、后求和**，这就是 "late interaction" 的含义：
交互发生在双方编码完成之后，不像 cross encoder 那样在 Transformer
内部逐层交互。由此得到那个值钱的性质——**文档侧的 token 向量可以离线算、
可以建索引**，查询时只需要编码查询。这是它比 cross encoder 便宜的
全部原因：cross encoder 的分数依赖 query，每次查询都得对每个候选做一次
完整前向（第 20.6 节），而多向量的文档侧成本是一次性的。

手算一遍。查询 2 个 token `q = [[1,0],[0,1]]`，文档 3 个 token
`d = [[1,0],[1,1],[0,1]]`：

```
相似度矩阵 q @ d.T =    每行取 max -> [1.0, 1.0]
[[1. 1. 0.]            求和 = 2.0
 [0. 1. 1.]]
```

第二个查询 token `[0,1]` 在文档里找到了 `[0,1]`（第 3 个 token，
相似度 1.0），第一个找到了 `[1,0]`（第 1 个，相似度 1.0）。
把文档换成只有 `[1,0]` 一个 token，第二个 token 的贡献就变成 0，
分数掉到 1.0——**这就是"漏词扣分"的具体形态**。
这也是为什么 MaxSim 天然适合"查询里有一两个专有名词"的场景：
那个词找不到对应就会拉低整条分数，而不像单一向量那样被平均掉。

### 存储代价：段落有多少 token，就贵多少倍

同维度、同数据类型下，这个倍数是可以精确算出来的，不需要估：
一篇文档在单向量方案里占 **1 个向量**，在多向量方案里占 **n 个向量**
（n = 段落 token 数），其余因素完全相同（同一个维度 `D`、同一种精度）。
所以**倍数就等于 n**，与维度无关。

三个可核对的点。第一，128 维 float16 的单向量是 `128 × 2 = 256 B`；
一篇 256 token 的段落是 `256 × 128 × 2 = 65536 B`，正好 **256 倍**。
第二，我们实测的小例子是 4 个段落、共 52 个 token（平均 13 个 token
一个中文短句），128 维 fp16：多向量 13312 B 对单向量 1024 B（4 行 × 256 B）
是 **13.0 倍**——和"平均 13 个 token"对上：

```
多向量 13312 B vs 单向量 1024 B = 13.0x
若 256 token 的文档: 65536 B = 单向量(256 B) 的 256x
```

第三，真实场景里一个段落的 token 数通常就在几百这一档
（ColBERT 系的编码器输入上限本来就是几百个 token），
所以真实场景的倍数落在几百倍。

所以常见的那个说法——"多向量比单向量贵 100 倍"——只在特定段落
长度下成立，**准确的说法是"贵 n 倍，n 是段落 token 数"**，
区间从十几倍到几百倍。
真实系统用两招压这个数字：向量量化到 1~2 bit（ColBERTv2 的
residual compression），以及"去掉标点、停用词对应的 token"——
后者的效果直接体现在 n 上。

这个倍数会一路传导下去：`n` 倍存储、`n` 倍的磁盘读、以及
"每行要和多少个向量做比较"这个计算量（见 21.2 节里那条
"多向量表比单向量表更需要建索引"的提醒）。

## 21.2 LanceDB 的多向量列：类型与索引

真实的 Arrow 类型（0.40.0 实测）长这样：

```
vector: list<item: fixed_size_list<item: float>[256]>
```

外层是变长的 `list`（这一行有多少个 token），内层是定长的
`fixed_size_list`（每个 token 的向量维度必须固定）。官方文档里
构造它的写法是：

```python
import pyarrow as pa

schema = pa.schema([
    pa.field("id", pa.int64()),
    # float16, float32, float64 都支持
    pa.field("vector", pa.list_(pa.list_(pa.float32(), 256))),
])
data = [{"id": i, "vector": np.random.random(size=(2, 256)).tolist()}
        for i in range(1024)]
tbl = db.create_table("multivector_example", data=data, schema=schema)

# 0.40.0 的推荐写法：列名在前，索引参数走 config=
from lancedb.index import IvfPq
tbl.create_index("vector", config=IvfPq(distance_type="cosine"))

# 旧写法还能用，但会打弃用警告（原文见下）
tbl.create_index(metric="cosine", vector_column_name="vector")
```

上面**最后一行**那种 `create_index(metric=..., vector_column_name=...)`
的写法**在 0.40.0 已经弃用**，实测会打：

```
DeprecationWarning: The create_index() API with metric/num_partitions
parameters is deprecated and will be removed in a future version. Please
migrate to the new unified API:
  # New (recommended):
  table.create_index('my_vector', config=IvfPq(distance_type='l2'))
```

本书别处（第 24 章 §24.3、第 25 章 §25.1.5）用的都是 `config=` 这一套，
这一章原来示范的是旧写法——现在两种都给出，照抄时选 `config=` 那种。

我们实测这段能跑通：1024 行、每行 2 个 256 维向量，索引建成，
`tbl.search(q).limit(5)` 返回 5 行。Pydantic 那边有对应类型：

```python
from lancedb.pydantic import MultiVector
vector: MultiVector(func.ndims()) = func.VectorField()
```

### 索引的硬约束

多向量索引目前**只支持 cosine**。官方文档的原话是"Multivector
indexing currently requires `distance_type=\"cosine\"`. Use
`bypass_vector_index()` for non-`cosine` queries on a multivector
column."

这不是建议，是硬约束。我们实测用 `metric="l2"` 建多向量索引会被直接
拒绝（**在本机 0.40.0 的 Python 包上实测**）：

```
RuntimeError: lance error: LanceError(Index): Build Vector Index:
multivector type supports only cosine distance
```

同一张表把度量换成 cosine（新旧两种 `create_index` 写法都行）就建成了。
**这条错误在本机 0.40.0 的
Python 包上实测是 `RuntimeError`（而不是 Rust 侧的 panic），
所以它是可以捕获、可以按条件分支处理的——但你没有任何别的选择，
只能改用 cosine。**（把它写成 `RuntimeError` 是本机观察，
不保证其它版本、其它语言绑定下也是同一种异常类型。）

要建索引就是标准的 IVF_PQ。**官方文档只点名了 IVF_PQ，但"只点名"
不等于"只有它支持"**——我们按本节那种多向量列实测过：1024 行、每行 3 个
64 维向量、`distance_type="cosine"`，`IvfFlat` / `HnswFlat` / `IvfPq`
**三种都能建**，建完 `index_stats()` 都报 `num_indexed_rows=1024`。

```python
import numpy as np, pyarrow as pa, lancedb
from lancedb.index import IvfFlat, HnswFlat, IvfPq

db = lancedb.connect("./mv_db")
schema = pa.schema([pa.field("id", pa.int64()),
                    pa.field("vector", pa.list_(pa.list_(pa.float32(), 64)))])
rng = np.random.default_rng(0)
data = [{"id": i, "vector": rng.random(size=(3, 64)).tolist()}
        for i in range(1024)]
for cfg in (IvfFlat(distance_type="cosine"),
            HnswFlat(distance_type="cosine"),
            IvfPq(distance_type="cosine")):
    tbl = db.create_table(f"mv_{type(cfg).__name__}", data=data, schema=schema)
    tbl.create_index("vector", config=cfg)      # 三种都通过
```

**真正的硬约束只在距离度量上**：把 `distance_type` 换成 `"l2"`，
上面这三种索引都会被同一条错误拒绝（三种都实测过，错误原文见本节
上半部分）。所以"多向量只能配 cosine"这句话约束的是**度量**，不是
**索引类型**——"能不能建"和"能不能用 l2 查"是两个问题，别把它们
混成一条禁令。

上面提到的 `bypass_vector_index()` 是这条硬约束的出口，它在计划里
长什么样值得看一眼——这是"我要精确结果"这句话在执行层的落地。
本实现的 `explain()` 会给出：

```
BypassVectorIndex: 强制暴力扫描（结果精确，代价是全表扫）
KNNVectorDistance
TopK: limit=5
```

`ANNSubIndex` 和 `ANNIvfPartition` 两个节点整条消失，因为索引根本没被
查。`analyze_plan()` 则在实测 I/O 里补上它真正的代价：

```
brute_force: {'rows_scored': 4000, 'dim': 32, 'metric': 'l2'}
```

**这一档的全部代价就是"参与距离计算的行数"，它等于所有片段行数之和**
——所以它同时也是算 ground truth 的标准办法（第 12 章）。
有一处本实现的不一致要说明：同一件事在 `explain()` 里叫
`BypassVectorIndex`、在 `analyze_plan()` 里叫 `FlatSearch`
（"无可用向量索引"），两个入口两个名字。真实 LanceDB 只有
`explain_plan()` 这一条路径，不会有这个分叉。

还有一条更值得记住的提醒：**多向量表比单向量表更需要建索引。**
暴力扫描的代价是"行数 × 每行的向量数"，两个维度同时在涨。官方文档
多向量检索那一页写明了这一点：一次暴力扫描必须把每个查询向量
和**每一行的每一个**文档向量都比一遍，所以成本同时随行数和
每行向量数增长；而在 LanceDB OSS 里查询不会被拦下来，它只是照跑，
于是一张没建索引的大多向量表能把进程卡住很久。

换个说法：OSS 里它**不拦你**，只是卡住。云端还有一道更严的暴力 KNN
安全阈值——同一页文档写的是：多向量列的行数阈值大约比单向量列**低 10 倍**
（低一个数量级），所以一张在小规模上"单向量可以不建索引"的表，
换成多向量列就可能被云端直接拒绝，报的是"vector search would use
brute-force KNN"。企业版没有这道限制，但那不是"可以省掉索引"的理由，
只是"卡住的地方换成了别的"。

### 查询侧与存储侧：两条路线的取舍

多向量检索在实现上有两条路，取舍很清楚。

**路线一：查询侧展开。** 把查询也切成 token，每个 token 各自发起
一次向量检索，拿到各自的 top-k，再合并。优点是可以**直接复用
现成的单向量 ANN 索引**，不需要为多向量做任何索引层面的改造。
缺点是每个 token 的检索都是近似的，MaxSim 里的 `max_j` 变成了
"在召回集合里挑最大"而**不是全局最大**，召回率有天花板。

**路线二：存全量 token 矩阵，检索后算 MaxSim。** 先做一次粗召回，
把候选行的 token 矩阵取回来，老老实实算 MaxSim。优点是打分精确，
缺点是回表要拉回 **n 倍**的数据量（n = 段落 token 数，见 21.1 节），
**IO 是瓶颈**。

这里有一个必须澄清的 API 事实。**真实 LanceDB 的 Python 包里
没有 `MultivectorQuery` 这个类**——`from lancedb.query import
MultivectorQuery` 会 ImportError（0.40.0 实测）。真实的多向量查询
入口就是普通的 `search()`，区别只在于传进去的是一个矩阵：

```python
q_multi = np.random.random(size=(2, 256))       # 2 个 query token
results_multi = tbl.search(q_multi).limit(5).to_pandas()
```

`mini_lancedb/rerank/colbert.py` 里的 `MultivectorQuery` 是**我们的
教学实现**，它走的是路线一（查询侧展开），因为这条路人人都能
在自己现有的单向量索引上跑起来。它同时提供了 `aggregate="sum"`
和 `aggregate="max"` 两种合并方式：前者对应 MaxSim 的外层 Σ，
后者对应"只要有一个 token 强命中就够"。

**但它在迷你实现上没有一条"直接传矩阵"的路。**
本实现的 `VectorQuery.multivector()` 会**显式抛 `NotSupportedError`**
（`mini_lancedb/query.py`），而不是静默退化成单向量——
理由写在它的文档串里：多向量的语义是"逐 token 召回再 max/求和"，
和单向量结果完全不同，静默退化会让用户拿到一份看起来正常、
其实错的结果。所以在迷你实现里，唯一能跑通的多向量路径是

```python
table.search("文本").rerank(ColBERTReranker(...))
```

也就是**走重排器**（它实现了 token 级 MaxSim），而不是走查询。
上面那段 `tbl.search(q_multi)` 说的是**真实 LanceDB**，
在本实现上会直接报错——这两边的入口不一样，是最容易照着抄错的一处。

实测一个 8 token 的查询（`GPT-4o-mini 上下文窗口`）打 4 个段落：

```
token 数: 8,  维度: 128,  存储开销: 8 个 token × 128 维 = 2.0 KB (float16)，单向量只需要 0.25 KB
rowid=2 rel=7.9999 命中token数=8   「GPT-4o-mini 的上下文窗口是 128k」
rowid=3 rel=4.7621 命中token数=8   「BM25 靠词频和逆文档频率打分」
rowid=1 rel=4.3372 命中token数=8   「向量检索擅长语义但不擅长精确词」
rowid=4 rel=4.2324 命中token数=8   「跨模态检索要求同一个语义空间」
```

正确的那一段拿到 7.9999，几乎满分（8 个 token 每个都强命中）。
**但这个 7.9999 不是"近似程度"的读数，别拿它和下面精确版的 8.0000
去划"近似 vs 精确"的界。** 它的来源是打分公式加浮点量化：这一路把
每个 token 的 `_distance` 折成 `1 / (1 + _distance)` 再求和，8 项
每一项的上限是 1；而段落向量是按 fp16 存进磁盘的，读回来每个 token
与 fp32 查询向量的余弦距离约 `6.7e-6`，于是每一项是 0.9999933、
8 项和 7.9999463——四舍五入 7.9999。把同一批段落改用 float32 存，
这个和是 7.9999996（即 8.0000）；精确 MaxSim 那边加的是**原始点积**
而不是 `1/(1+d)`，8 个点积里有的略大于 1，和为 8.0000057，也是 8.0000。
**两个数差的只是小数点后第四位，而量化恰好就贡献了这一位。**
但后面三名挤得很近（4.76 / 4.34 / 4.23）——这才是查询侧展开的
典型形态：**头部可靠，尾部靠噪音区分**。原因是每一路召回都是近似的，
`max_j` 只能在每个 token 各自的 top-k 里取最大值，
"没有真正命中的 token"也能蹭到一点分数。所以**粗召回阶段的名次可以信，
头部之后的名次必须靠精确 MaxSim 重排才有分辨力**——精确版的结果见
本节的最后一个例子。

真实 API 侧，路线二对应的是 `Reranker.rerank_multivector`：

```python
Reranker.rerank_multivector(vector_results, query, deduplicate=False)
```

注意真实签名里 `deduplicate` 的默认值是 `False`，而 `query` 是
第二个位置参数。`RRFReranker.rerank_multivector` 会要求输入带
`_rowid` 列，否则报 `ValueError`。

生产上两条路通常串起来用：查询侧展开做粗召回，再用精确 MaxSim
对 top 100 重排。本教程实现的对应类叫 **`ColBERTReranker`**（大写 B）。
**真实 LanceDB 的类名是 `ColbertReranker`（小写 b）**——这是全书少数
几个"两种拼写在同时用"的地方，查文档和写 import 时要各按各的那一个来
（附录 A 记过实测：`ColBERTReranker` 在真实库里不存在）。真实库的签名是：

```python
ColbertReranker(model_name='colbert-ir/colbertv2.0', column='text',
                return_score='relevance', **kwargs)
```

`ColBERTReranker` 的 `tokens_of` 支持三种输入形态：`(n_tokens, dim)`
的 numpy 数组、嵌套 list、以及 **bytes**。最后一种是真实 Lance 的磁盘
布局——多向量是按定长列表扁平存进 buffer 的，所以从磁盘读回来就是一串
`float16` 字节。我们刻意保留了这条解码路径。有一个必须知道的边界：
`tokens_of` 从字节长度反推形状时**假设内层宽度是 128**，
换成别的维度要先自己确认，否则 reshape 出来的矩阵是错的——
而它不会报错，只会让分数偏低。

### 一个能跑的段落级例子

先纠正一个容易想当然的印象：**嵌套定长列表在 `mini_lancedb` 里是支持的**，
不需要绕路。类型系统能表达它：

```python
>>> FixedSizeList(FixedSizeList(Float16(), 128), 3).name
'fixed_size_list<fixed_size_list<float16>[128]>[3]'
```

解析和写入两条路都通。`parse_type` 用的是**跳过嵌套尖括号的扫描**
（`_strip_outer`），不是 `partition(">")`——后者会把内层的 `>` 当成
外层的收尾，正是它要避开的坑；所以
`fixed_size_list<fixed_size_list<float16>[128]>[3]`、
`fixed_size_list<list<float16>>[3]`、
`struct<x: fixed_size_list<float16>[4]>` 都能解析并原样构造出类型。
唯一会抛 `ValueError` 的，是把文档里的占位符 `[N]` 原样敲进代码
（消息是 `invalid literal for int() with base 10: 'N'`），那和嵌套无关。
写入路径的 `FixedSizeListArray.from_pylist` 也是递归的：它调用通用的
`array_from_pylist(type.child, flat)`，刻意**不假定子列是定长标量**，
子类型是 `FixedSizeList` 时会继续往下构造。`tests/test_regressions.py`
里有一条 `test_parse_type_handles_nested_and_parameterised_types`
专门钉着这两条，防止它们退化。（`is_vector_type` 对嵌套类型返回
`False` 这一点仍然成立——正好可以当"这是多向量列不是单向量列"的判别器。）

那例子为什么还是选 `binary`？因为这是一次**刻意的选择**，不是被迫的。
真实 Lance 的多向量列在磁盘上就是按定长列表**扁平**存进 buffer 的，
读回来是一串 fp16 字节——上面提过，`tokens_of` 专门保留了一条 bytes
解码路径，解的就是这种布局。把 token 矩阵写成 `binary` 列，例子就跑在
真实的磁盘布局上：既能直接量出"这一列占了多少字节"这笔存储账，也能
顺带演示那条解码路径。而多向量列每行的向量数可变，本来也建不了普通的
ANN 索引（见前面的索引硬约束），它无论如何都不该被当成定长向量列用。
整段可以跑：

```python
import numpy as np
from mini_lancedb.types import parse_type
from mini_lancedb.schema import Schema, Field
from mini_lancedb.storage.base import InMemoryStore
from mini_lancedb.dataset import Dataset
from mini_lancedb.record import RecordBatch
from mini_lancedb.rerank import ColBERTReranker, HashingTokenEmbedder

emb = HashingTokenEmbedder(dim=128)
PARAS = [(1, "向量检索擅长语义但不擅长精确词"),
         (2, "GPT-4o-mini 的上下文窗口是 128k"),
         (3, "BM25 靠词频和逆文档频率打分"),
         (4, "跨模态检索要求同一个语义空间")]

schema = Schema([Field("id", parse_type("int64")),
                 Field("text", parse_type("utf8")),
                 Field("mv_bytes", parse_type("binary"))])
rows = [{"id": rid, "text": t,
         "mv_bytes": emb.encode_tokens(t).astype(np.float16).tobytes()}
        for rid, t in PARAS]

ds = Dataset.create(InMemoryStore(), "memory://mv", schema)
ds.write([RecordBatch.from_pylist(rows, schema)])
```

真实输出：

```
写入 -> version 2
binary 列读回类型: bytes 长度: 3840
还原成 token 矩阵: (15, 128) float16
```

3840 = 15 token × 128 维 × 2 B——这是读回**一行**得到的：第一个段落
（"向量检索擅长语义但不擅长精确词"）正好 15 个 token。4 个段落的
token 数分别是 15 / 11 / 12 / 14，合计 52，也就是这一列总共
`52 × 128 × 2 = 13,312` 字节，而同样的 4 行用单向量列只需要
`4 × 128 × 2 = 1,024` 字节。**13.0 倍，就是"平均每行 13 个 token"**
——这就是多向量的存储代价在磁盘上的样子。
还原出来之后，MaxSim 精排：

```
rowid=2 MaxSim=8.0000  「GPT-4o-mini 的上下文窗口是 128k」
rowid=3 MaxSim=1.9662  「BM25 靠词频和逆文档频率打分」
rowid=1 MaxSim=1.2291  「向量检索擅长语义但不擅长精确词」
rowid=4 MaxSim=0.8709  「跨模态检索要求同一个语义空间」
```

查询是 `GPT-4o-mini 上下文窗口`，8 个 token。命中段落拿到 8.0000，
也就是**这个查询的每一个 token 都在这个段落里找到了归宿**——
这正是 MaxSim 外层 Σ 的语义。同一批段落上，查询侧展开（近似）给出
7.9999、4.76 / 4.34 / 4.23，精确 MaxSim 给出 8.0000、1.97 / 1.23 / 0.87：
**第一名的名次和量级两边一致，后面几名只有精确版拉开了差距。**
这就是"粗召回靠近似、尾部排序靠精确"的实测依据。

读第一列时注意上面那条提醒：**7.9999 对 8.0000 不是近似和精确的分界**
（两条路的打分公式不同，量化又各占掉小数点后第四位，见本节上半部分），
真正的分界要在**后面三名**上看——4.7621 / 4.3372 / 4.2324 对
1.9662 / 1.2291 / 0.8709，差距从"挤在一起"变成"拉开一个量级"。

## 21.3 图像、音频、视频：怎么把非文本放进同一张表

文本之外的东西，LanceDB 给出的建模方式非常朴素：
**原始字节放一个 blob 列，向量放一个向量列，其余放元数据列，
全部在同一行里。**

官方文档在讲多模态建模时，把这四种东西直接并列写了进去——图像、
音频、视频、PDF 文件都是**原生**支持的，做法是"把原始字节存进一个
binary 列，和你的向量、元数据放在一起"。它给的理由也很直接：
这样能**把原始素材和它们的嵌入留在同一个库里**，省掉一整套
数据基础设施。

这么做的好处是**版本一致性免费拿到**：向量和它对应的原始文件在
同一行、同一个 fragment、同一个版本 manifest 里。你不必担心
"向量库更新了但 CDN 上的图片还是老的"——因为它俩本来就是同一个
版本里的两个列。这是这一节里最值钱的一条，21.5 节讲
"向量与原始文件的版本一致性"时还会展开。

一个典型的 schema 长这样：

```python
schema = pa.schema([
    pa.field("id", pa.int64()),
    pa.field("image", pa.large_binary(),
             metadata={"lance-encoding:blob": "true"}),   # 启用 Blob API
    pa.field("caption", pa.utf8()),
    pa.field("image_emb", pa.list_(pa.float32(), 512)),    # OpenCLIP ViT-B/32
    pa.field("thumbnail", pa.large_binary(),
             metadata={"lance-encoding:blob": "true"}),
])
```

列上还能挂描述元数据，让表自解释：`lancedb:description`
（例如 `"CLIP embedding of the product image"`）和
`lancedb:tag:model`（例如 `"clip"`）。

注意 `lance-encoding:blob` 是 **Blob V1** 的机制——一个挂在
`large_binary` 列上的元数据标记，用来告诉 Lance"这一列按 blob 处理"。
下一节讲的 Blob V2 是更彻底的一版，那里 blob 是一等公民，
不再需要在列上打标记。这两种机制在文档里同时存在（第 3 章讲 Lance 的
blob 存储时也记过这个不一致），**所以照着官方页面的例子写代码之前，
先确认那一页讲的是 V1 还是 V2**——不过两种格式的关系是**单向兼容**的：
在 2.2 及以上的数据集上，**dataset writer 接受打了 V1 标记的 binary 输入、
并把它转写成 Blob v2**，会直接拒绝的只是更底层的 file writer
（21.4 节最后讲迁移时展开）。

### 模型与维度：几个真实的坐标

**CLIP 系。** 最常用的是 OpenCLIP 的 `ViT-B-32`，配
`laion2b_s34b_b79k` 权重，输出 **512 维、cosine 归一化**。
LanceDB 的 `open-clip` embedding 注册表里的默认参数就是
`name="ViT-B-32"`、`pretrained="laion2b_s34b_b79k"`、
`normalize=True`、`batch_size=64`。512 维是个很好的起点：
便宜、够用、生态最全。

**SigLIP 系。** SigLIP2 不是"一个 768 维的模型"，而是一个**尺寸谱**：
ViT-B 768 维、L 约 1024 维、So400m 1152 维、g（1B）1536 维。
**所以"SigLIP2 是 768 维"只在 base 那一档成立**——引用维数时
必须连 checkpoint 一起引用（`google/siglip2-so400m-patch14-224` 是
1152 维，而它是检索场景里更常用的那一个）。比 CLIP 新，通常在
零样本分类和图文检索上更好，代价是维度和算力都上去了。

**ColPali / ColQwen 系。** 这是**多向量**的多模态模型：
一张图被切成 patch，每个 patch 一个向量，然后用 MaxSim 对齐。
它是"把 21.1 节那套搬到图像上"的直接结果。官方集成里
`colpali` 的默认模型是 `Metric-AI/ColQwen2.5-3b-multilingual-v1.0`，
另外还有 `vidore/colpali-v1.3`、`vidore/colqwen2-v1.0`、
`vidore/colSmol-256M` 这一系列。文档的说法是"ColPali produces
multiple embedding vectors per input (multi-vector), enabling more
nuanced similarity matching between text queries and image documents."
它的存储代价可以直接套 21.1 节那条规则算：**一个 item 切出多少
patch，就贵多少倍**（同维度、同精度下）。按"一页切出约 1000 个 patch"
这个设定算——这也是一页 A4 在这种 32×32 级别的 patch 网格下的量级——
用 ColPali 表示一页的向量占用就是"每页一个向量"方案的**约 1000 倍**，
即三位数倍。和前面 ColBERT 那个"段落有多少 token 就贵多少倍"
是同一条规律，只是这里的 n 是 patch 数。所以具体倍数不必记，
**记住"倍数 = 向量个数"，然后用你自己的切法去算**。

**能一次覆盖多模态的。** ImageBind（`imagebind_huge`）把文本、图像、
音频、视频甚至热成像、深度、IMU 都放进一个空间。`jina-clip-v1`
和 `voyage-multimodal-3.5` 是 API 侧的多模态 embedder。

选型上有一个粗糙但好用的判断：**只做图文检索用 CLIP/SigLIP，
要做"查询里的短语定位到图上的哪一块"（比如"这句话写在发票的哪一行"）
用 ColPali 类。** 前者的存储和延迟是单向量的量级，后者要走多向量
那条路径——贵 n 倍（n = patch 数），而且索引同样只支持 cosine
（这一点和文本多向量一样）。

### 音频、视频、PDF 各自要挑什么模型

**音频**。代码路径和图片完全一样：把 `.wav` / `.mp3` 读成 bytes
放进 blob 列，区别只在 embedder。这里要分清两件不同的事。
**音频相似度检索**（"找出和这段音频相似的片段"）只需要音频侧的
向量，官方 recipes 仓库里的音频检索示例用的是 PANNs
（`panns_inference` 的 AudioTagging）出向量。**用文本查音频**则要求音频和文本在同一个空间里，
这需要一个对比训练过的音频-文本模型，主流选择是 LAION 的 CLAP
（`laion_clap`）——把音频和文本编码进同一空间之后，"文本查音频"
就退化成一次普通的 ANN，和下面讲的"成对嵌入"是同一件事。
要注意的是**官方 embedding 注册表里目前没有专门的音频模型**
（多模态那一段只有 OpenCLIP / SigLIP / Jina / ImageBind / ColPali
这五种，完整名单和它们的模态见第 22 章 §22.4），
音频向量得自己在写入路径上算好再塞进表里。用 ImageBind 也是一个
选择：它顺带把音频和文本放进了同一个空间。

**视频**。这里没有一条"用某个模型就行"的答案，要先决定
**一行是什么**。三种切法各有各的模型：

**一行 = 一帧。** 用 CLIP/SigLIP 逐帧出图向量，一行一帧。
检索粒度最细，但一小时的视频会变成十万行，元数据开销和片段数
都上去了（第 23 章那套权衡会立刻找上门）。

**一行 = 一个片段。** 按镜头切换或固定时长切段，每段取关键帧的
向量（或对帧向量做平均/池化）。这是最能落地的一档：
行数与"人能理解的单位"对齐，检索到之后再按时间戳取那一小段视频。

**一行 = 一段文本描述。** 先用视觉模型给每个片段生成 caption，
再把 caption 交给文本 embedding。它的好处是**查询侧不需要
多模态模型**（纯文本检索），代价是多了一层生成的失真。

视频的容量经验值是官方给的：**图片小于 1 MB、视频片段小于 5 MB**，
超过这个量级就该考虑 Blob V2 的 Dedicated / External 那一档（见下）。
注意这个经验值是"blob 大小"的建议，和"一行存多少"是两件事——
一段 30 秒的 1080p 视频动辄几十 MB，所以"一行 = 一个片段"
通常意味着必须走 Dedicated 档。

**PDF 与文档解析**。官方文档把 PDF 和图像并列，但这两条路的技术
含量完全不同，值得单独说。

**先抽文本，再当纯文本处理。** 这是最省的一条，用 PDF 解析器
（`pypdf`、`pdfplumber`、`unstructured` 之类）把文字抽出来，
切段、编码、走正常的多向量或单向量流程。它的问题是**版式信息全丢**：
表格、多栏排版、印章、手写批注、图里的文字都抽不出来，
而恰恰是这些内容最需要检索。

**整页当图，用 ColPali 类模型。** 这一条路线把"页面图像"直接喂给
多向量的视觉模型，查询是一个文本问题，对齐由 MaxSim 完成。
它的价值在于**不需要 OCR 也不需要对版式建模**——模型的 patch
注意力自己会落到相关的那一块上（这就是"短语定位到哪一块"
在文档场景里的具体用处）。代价就是上面算过的那笔账：每页约 1000 个
patch，存储和索引成本都是三位数倍。

所以文档检索的现实选择是分层的：**正文文本走抽文本那条便宜路，
关键的表格页/扫描件走整页图像那条贵路，两张表或同一张表的两行
分开存**。想两条都吃掉、又不想付两份存储的，只能回到
"整页图像 + 多向量"这一条。

### "同一个语义空间"为什么是前提

跨模态检索能成立的**唯一**原因是：图像和文本被编码进了同一个
向量空间。这件事不是自动的——它需要模态对齐训练。

所以有一条铁律：**查库时用的模型，必须和入库时用的是同一个。**
不是"同一种"，是同一个 checkpoint。用 OpenCLIP ViT-B/32 laion2b
入库、用 SigLIP2 查库，结果是"不报错的错答案"：向量还在、ANN
还跑得动、距离还算得出来，只是距离的数值毫无意义。

官方文档没有用"同一个语义空间"这个措辞（我查过，这个短语在文档
和博客里都找不到）。它表达同一件事的方式是"入库和查询用同一个模型"
和"同一行里的成对嵌入"。TextVQA 那个数据集的文档把成对嵌入写得很直白：
同一行里并排放 `image_emb` 和 `question_emb` 两个 512 维
（OpenCLIP ViT-B/32、cosine 归一化）的向量，于是**跨模态的
"文本→图像"检索就变成了一次带索引的查询**——"用一个文本问题去查图片"
只是"用 question 的编码结果去查 `image_emb` 这一列"，一次普通的 ANN。

还有一条配套的规矩：**距离度量也必须和模型训练时的一致。**
官方在向量检索那页挂了一个警告："Ensure you always use the same
distance metric that your embedding model was trained with."
CLIP 系都是 cosine 归一化的，就该用 cosine，不要图省事用 l2。

### 怎么评跨模态检索：文本查图算不算"召回"

文本查图的评测比同模态检索多几个坑。

**第一，你得有跨模态的标注。** 同模态检索的标注是"查询 → 相关文档"，
跨模态的是"文本查询 → 相关图片集合"。公开基准里最常用的形态是
MS-COCO / Flickr30K 那种"一张图配五条 caption"：把 caption 当查询、
它对应的那张图当正例，其余图当负例。评测集必须是**这个形状**，
拿只有图向量和文本向量的库自己给自己打分是没有意义的。

**第二，候选集大小必须写清楚。** Recall@k 高度依赖"从多少张里捞"。
同一个模型在 1000 张里算 Recall@10，和在一百万张里算，
是两个完全不同的数字（后者通常掉得很厉害）。所以任何"我们的
跨文本查图召回率是 0.9x"的说法，**没有写候选集规模就等于没写**。
工程上更诚实的做法是固定一个候选集（比如随机抽 10 万张图），
在这个集合上报告 Recall@1 / Recall@5 / Recall@10，
并在报告里把这个集合的大小和来源写进同一句话。

**第三，两个方向要分开评。** 文本→图（T→I）和图→文本（I→T）
的召回率不对称，这是 CLIP 系模型的常态。只报一个方向会掩盖问题：
T→I 好而 I→T 差，往往说明文本侧的编码分布比图像侧更"平"。

**第四，别用余弦相似度本身当指标。** 相似度的绝对值随模型标定
（temperature）变化，跨模型不可比；只有名次类的指标（Recall@k、mAP）
才可比。这和 20.3 节讲融合时"只看名次对尺度免疫"是同一条道理。

## 21.4 Blob：从"一个大字段"到一等公民

Blob 支持是 Lance 2.2 的一件事，官方博客的标题就是
*Lance Blob V2: Making Multimodal Data a First-Class Citizen in the
Lakehouse*（LanceDB 官方博客）。这里的"一等公民"不是营销词，
它可以拆成四条具体的存储语义。

**按大小自动选择存储方式**，阈值是明确的：

小于 **64 KB** 走 *Inline*，直接放在主数据文件里。**64 KB 到
4 MB** 走 *Packed*，若干 blob 打包进共享的 `.blob` 边车文件
（每个边车文件最大 **1 GiB**）。大于 **4 MB** 走 *Dedicated*，
每个 blob 一个独立的 `.blob` 文件。第四种是 *External*，
你自己给 URI，Lance 只存引用。

**四种语义共用一套磁盘描述结构**，所以读取代码不用分支：

```
kind      uint8   0=Inline / 1=Packed / 2=Dedicated / 3=External
position  uint64
size      uint64
blob_id   uint32
blob_uri  string
```

文件命名是 `data/{data_file_key}/{reversed_blob_id}.blob`——
注意 `reversed` 不是装饰：它和 7.2 节讲"文件名为什么要倒着数"
用的是同一个手法，把递增的序号反转，让**字典序顺序与写入顺序相反**。
这样对象存储的一次 `LIST` 拿到前缀，就等于拿到最近写入的那批条目，
不必把整个目录列出来再排序。后面每一节讲"只碰需要的那部分"时，
你会在不同层次上反复看到这同一种设计。

**API 也是一套**：`take_blobs()`、`BlobFile.read()`、`BlobFile.seek()`。
写入侧用 `lance.blob_array(values)`，指定存储版本用
`data_storage_version="2.2"`；读回用 `ds.take_blobs("blob", indices=[0,1,2])`；
外部引用用 `Blob.from_uri(external_uri, position=1024, size=4096)`。

在 LanceDB 这一层，blob 的读法通过 `to_pandas(blob_mode=...)` 控制，
三种模式：`"lazy"`（默认，返回惰性的 `BlobFile` 对象，不急着把
payload 拉回来）、`"bytes"`（立刻物化成 `bytes`）、
`"descriptions"`（只返回描述符，不返回数据本身）。
`"bytes"` 和 `"descriptions"` 需要文件系统支撑，内存表不支持；
远程表会抛 `NotImplementedError`。还有一条限制很容易踩：
**向量、全文、混合、有序这四类查询不能通过 `to_pandas()` 物化
blob 列**，投影里必须把 blob 列排除掉。

### External 那一档：URI、凭证，以及"密钥过期了怎么办"

前三档（Inline / Packed / Dedicated）的选择权在 Lance 手里，按大小
自动分；**只有 External 是你要做决定的**：字节不搬进来，`blob_uri`
里存一个指向外部的绝对 URI（`s3://bucket/key` 那一类的形状），
Lance 只存引用。

这里有一个必须先想清楚的机制：**Lance 不在 blob 里存凭证。**
读一个 External blob 时，它拿 `blob_uri` 的 scheme 去对象存储注册表
里找对应的 store，用**注册表里的凭证**把对象取回来。由这一条推出
两条实践结论。

第一，**不要把签名 URL（presigned URL / SAS）写进 `blob_uri`。**
签名 URL 的本质是"把凭证塞进了 URL 的查询串"，一旦写进 lance 文件，
它就**固化成数据的一部分**：密钥一过期，那一行就永久读不出来，
而且报出来的是对象存储的 403、不是 Lance 的错误，排查要绕一圈。
更糟的是签名里带着真凭证，会跟着数据一起被备份、被共享、被
`to_arrow()` 导出到别处。正确做法是存普通 URI，凭证交给读取侧
（环境变量、实例角色、注册表）。

第二，**External 的另一半好处是访问控制能分层**：桶上的权限策略、
生命周期规则、CDN 都还生效，因为字节确实还在那里。Lance 侧还有一条
安全默认值——外部 blob 的 URI **默认必须落在已注册的、非 dataset
根目录的 base path 之下**，想引用之外的路径要显式打开
`allow_external_blob_outside_bases=True`。这个开关不是多余的：
它挡的是"用一个 blob 引用去读数据集目录之外的任意对象"这类越权。

所以"密钥过期怎么处理"的答案是分层的：**能用长期凭证就用长期凭证**
（IAM role / 实例角色 / 注册表里的服务账号），过期这件事就消失了；
必须用短期凭证时，刷新发生在**读取侧的进程里**（SDK 的刷新链、
启动时取一次），**不进数据文件**；只有"给第三方一个限时下载链接"
才轮到签名 URL，而那种 URL 应该**在应用层现签现用**，
不落库、不进版本。

### `lance-encoding:blob` 的老数据怎么办

方向和直觉相反：**这两种格式不是互斥的，而是单向兼容**，而且
`data_storage_version` 的默认值在数据集创建时就固定下来、**不能原地修改**
（不改默认值不影响"新写的一批用哪种物理编码"）。规则两句就说完：
`data_storage_version` 是 0.1 / 2.0 / 2.1 的数据集，V1 标记可读可写、
Blob V2 不支持；声明为 2.2 及以上的数据集，**V1 标记被当作输入接受，
由 dataset writer 转写成 Blob v2**，Blob v2 也支持读写——会直接拒绝
V1 标记的只是更底层的 file writer，走 dataset writer 的正常写入碰不到它。

所以老数据**不必重写**。在一个 2.0 / 2.1 的老数据集上，直接带着
`data_storage_version="2.2"` 追加，就开始往这张表里写 Blob v2 文件了。
官方页面的原话是两者都收——"Both legacy blob-marked binary inputs and
`blob_field` / `blob_array` inputs are accepted. **No separate upgrade
operation or historical-payload rewrite is required.**"同一张表里
两种物理编码**可以混着存**，读取端按描述符里的 `kind` 分别解释。

那什么时候才需要重写？答案是"想把历史 payload 一次性换成 V2 编码、
不再保留老编码"的时候——这是**可选的优化**，不是迁移的必经之路。
官方页面的说法是重写成新数据集"is optional for 2.0/2.1 tables"，
只有 file format **0.1** 的表才**必须**重写才能进入 2.x 家族。
重写的形状是：先用 `blob_handling="all_binary"` 把老列读成原始
字节，再按 Blob V2 的 schema 写到一个新建的 2.2 数据集里。

```python
legacy = lance.dataset("./legacy_blob_dataset")
raw = legacy.scanner(columns=["id", "video"], blob_handling="all_binary").to_table()

new_schema = pa.schema([
    pa.field("id", pa.int64()),
    blob_field("video"),          # Blob V2
])
rewritten = pa.table(
    {"id": raw.column("id"),
     "video": blob_array(raw.column("video").to_pylist())},
    schema=new_schema,
)
lance.write_dataset(rewritten, "./blob_v22_dataset", data_storage_version="2.2")
```

这段代码有一个**不能照搬到生产**的地方：`blob_handling="all_binary"`
加 `to_pylist()` 会把二进制全部物化进内存。几百万张 4 MB 的图会直接把
内存打爆——而这恰好是 Blob V2 要解决的问题。生产上的做法是**分块重写**：
一边流式读老数据集的一个批次、一边写进新数据集，每批提交一个新版本。
代价是重写期间两份数据同时占着存储。既然重写本身是可选的，更省事的
做法是先按上面说的原地混存，再让后台压实把混着的那一列逐步收敛到
Blob v2 文件——压实只重写被选中的片段，它不改变默认文件版本，
也不是开启 Blob v2 写入的前提。

### 官方的量化数字

下面这些数字出自 LanceDB 官方博客
*Lance Format v2.2 Benchmarks: Half the Storage, None of the Slowdown*
（作者 Xuanwo，2026 年 4 月 6 日，lancedb.com/blog）。**它们是厂商
自己的口径**，我把它标出来，引用时请连同出处一起引用。

存储体积，FineWeb 1000 万行：Parquet **32,534 MiB**，
Lance v2.0 **32,821 MiB**（比 Parquet 还大一点），
Lance v2.2 **15,631 MiB**——比 Parquet **小 52%**。
同一篇博客里 LAION-10M 的 20 万行子集是"基本持平"（5995 MiB 对
5995 MiB），OpenVid 100 万行小 37%（1027 → 649 MiB），
LeRobot PushT 2.5 万行小 28%（1.2 → 0.86 MiB）。

放到 TiB 量级，博客给出的 S3 数字是：FineWeb-1B 从
**3.32 TiB 降到 1.62 TiB**（降 51%），OpenVid-1M 从 0.85 GiB
降到 0.36 GiB（降 58%）。

随机 blob 读取是最能说明问题的那组。**1000 次随机 blob 读取**：

```
Lance v2.2   ~2 秒（几何平均）
Parquet      ~150 秒
```

博客的说法是"Lance v2.2 比 Parquet 快 75 倍"，并单独给了
LAION-10M 上的数字："Parquet 取 1000 张随机图片用了 889 秒，
Lance v2.2 在个位数秒内完成同一份工作。"全量规模（S3 上的
LAION-10M-Full，2000 万行、589 GiB）的延迟是：v2.0 总耗时
151,138 ms、p50 149.6 ms、p95 265.0 ms；v2.2 总耗时 139,706 ms、
p50 124.1 ms、p95 294.4 ms。

给 1000 万行加一列：**Lance 13 ms，Parquet 520 秒**（博客原话：
"Lance adds a new column in 13 ms, while Parquet takes 520 seconds
to rewrite the dataset"）；跨全部数据集是 61 倍。

为什么 Parquet 在这两个场景上这么慢，值得想一下。Parquet 的
列是"整列一起重写"的——加一列意味着重写整个数据集，所以是 520 秒。
随机读 1000 个 blob 慢是因为 Parquet 把 blob 的定位信息（offset）
和 payload 一起编码在行列结构里，随机访问要解一大堆不需要的
页头和字典——**它从来没有为"随机点读一个大对象"设计过**。
Lance 把这两件事都做成了"只碰需要的那部分"。

## 21.5 多模态检索的工程问题

前面都是"能不能做"，这一节讲"做起来会疼在哪里"。三个问题，
每一个都有非平凡的答案。

### 缩略图与全分辨率的分层存储

一个商品图库可能有 500 万张图，每条记录有 4K 原图、800px 展示图、
128px 缩略图三个版本。如果都把原图塞进同一条记录，检索时的
I/O 会因为"其实只要缩略图"而浪费几十倍。

分层存储的标准做法是：**把不同分辨率的字节放在不同的 blob 列
（或不同表）里，检索阶段只投影缩略图那一列，真正需要原图时再
按需 `take_blobs`。** `blob_mode="lazy"` 和 `"descriptions"` 就是
为这件事设计的——你可以先拿到描述符（offset/size），决定要不要
真的读。缩略图能放进 Inline 那一档（小于 64 KB），
意味着它可以跟着主数据文件的顺序读一起被拉回来，几乎没有额外
的随机 I/O。

### 向量与原始文件的版本一致性

这是"把字节和向量放在同一张表"最大的回报，值得说清楚它解决了
什么。

如果图片放在对象存储的一个桶里、向量放在 LanceDB 里，那么
"更新一张图片"就是一个**分布式事务**：要么先传图片再更新向量
（中间窗口里，向量指向了一张已经不存在的图），要么先更新向量
再传图片（中间窗口里，最坏情况下新图配旧向量）。两个窗口都会
在高峰期的并发更新里真实发生。

放在同一行里，这两件事是**同一个版本 manifest 的两个列**。
写一次 = 一个版本 = 图片和向量同时可见。时间旅行把整行一起回退，
不存在"向量回退了但图片没回退"这种状态。

还有一个直接的下游好处：**去重**。图片的 SHA-256 放在一个标量列上，
配一个 BTREE 或 BITMAP 索引，上传前的去重就是一个索引查询。同一张图
被不同文档引用时，可以只存一份字节、多个引用行。

### 成本：多模态特有的两块

存储、请求次数、嵌入算力这三块成本的结构在第 25.7.3 节（成本模型），
那里讲的是通用的（也适用于纯文本）。这里只补两块**多模态特有**的。

**出流量（egress）。** 这是多模态独有的一大笔：文本检索返回的是
几百字节的字符串，图像检索返回的是几百 KB 到几 MB 的字节。
对象存储的出口流量按 GB 计费，"检索一次返回 20 张原图"这样的接口
就是 20 × 4 MB = 80 MB 的出口。所以多模态系统里**默认返回缩略图、
按需取原图**不只是延迟优化，它同时是成本控制手段——
上面讲的分层存储（缩略图一列、原图一列）在这里才真正兑现价值：
检索路径只碰缩略图列，出口流量就从几 MB 掉到几十 KB。
**把"哪些分辨率会被真的读"当成一个成本参数来设计**，
是多模态特有的功课。

**缩略图生成。** 分层存储的前提是缩略图真的存在，而生成它是一次
**写入路径上的 CPU 开销**（解码、缩放、重编码，一张 4K 图几十毫秒），
并且和嵌入算力一样必须按 SHA 缓存，否则重复上传会把 CPU 打满。
它还有一个容易被忽略的正确性要求：**缩略图和原图必须来自同一次
解码**，否则"图换了、缩略图没换"这种不一致会直接出现在检索结果里。
把两者放进同一行的同一批写入，这个问题就自动消失了——
这正是上一节讲的版本一致性的一个具体用处。

## 21.6 小结

**单向量的信息瓶颈是维度不够，不是模型不行。** 一段 2000 字的
文档压成一个 768 维向量，主题还在、细节糊了。多向量让表示的容量
随长度线性增长。

**"多向量"不是"多个向量列"。** 前者是一行的一个列里存一个变长的
向量集合（`list<fixed_size_list<float>[D]>`），后者是 schema 里
多几个定长向量列。前者需要专门的检索路径；后者只是多几个独立的向量。

**多向量的打分是 token 级 MaxSim**：每个查询 token 在文档所有 token
里取最相似的那个，再求和。先算相似度、后求和（late interaction），
所以文档侧向量可以离线算、可以建索引——这是它比 cross encoder 便宜的
全部原因。代价是存储**贵 n 倍，n 就是段落 token 数或图像 patch 数**：
128 维 fp16 下 256 token 的段落是 256 倍，中文短段落（平均 13 个 token）
实测 13.0 倍，ColPali 每页约 1000 个 patch 就是三位数倍。
"100 倍"只在特定段落长度下成立，别当常数用。

**多向量索引目前只支持 cosine**，标准做法是 IVF_PQ。多向量表比
单向量表更需要早点建索引，因为暴力扫描的代价是"行数 × 每行向量数"，
而且云端的暴力 KNN 行数阈值比单向量列低约一个数量级。

**真实 LanceDB 的 Python 包里没有 `MultivectorQuery` 类**（0.40.0
实测）。多向量查询的入口就是 `search(矩阵)`。查询侧展开和
"存全量 token 矩阵 + 后算 MaxSim"是两条互补的路线，生产上串着用：
前者头部可靠，尾部要靠后者才有分辨力。

**多模态的建模方式是"blob 列 + 向量列 + 元数据列"放在同一行**，
回报是版本一致性免费。跨模态检索成立的唯一前提是入库和查询
用同一个模型的同一个 checkpoint，距离度量也要和训练时一致。
模型要连尺寸一起引用：SigLIP2 从 768 维到 1536 维有四个档。
音频要文本查音频就用 CLAP（官方注册表里没有音频模型）；
视频先决定"一行是什么"（帧 / 片段 / caption）；
PDF 是"抽文本走便宜路、关键页整页图像走 ColPali"的分层。

**Blob 在 Lance 2.2 是一等公民**，按大小分四档（64 KB / 4 MB
两个阈值），四种语义共用一套描述符和一套 API。V1 的
`lance-encoding:blob` 标记和文件格式 2.2 并不互斥：2.2+ 上 dataset
writer 会把打了 V1 标记的输入转写成 Blob v2，两种编码可以混存，
老数据集带 `data_storage_version="2.2"` 追加即用——重写只是可选的优化，
只有 0.1 格式的表才必须重写。
External 档下 Lance 不存凭证，签名 URL 写进 `blob_uri` 就是把它
固化成数据的一部分，凭证过期那一行就永久读不出来。官方基准给出的
数字是 FineWeb 1000 万行比 Parquet 小 52%、1000 次随机 blob 读取
约 2 秒对约 150 秒、给 1000 万行加一列 13 ms 对 520 秒——
**这些都是厂商口径，出处是 LanceDB 官方博客 2026 年 4 月 6 日那篇
v2.2 基准报告。**

第 22 章往前走一步：前面所有章节都假设"向量已经有了"，下一章补上
前面那一步——向量是怎么来的，以及"表里的向量到底是哪个模型产生的"
这条决定长期可维护性的元信息。
