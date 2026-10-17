# 第 17 章　全文检索与 BM25

一个向量库为什么非得自己带一套全文检索？答案不是"功能齐全显得厉害"，
而是一个具体的、每天都在发生的失败。

## 17.1 一个向量模型必然答错的查询

考虑一张设备台账表，六行，每行一句话：

```
The NVIDIA A100 has 40 GB of HBM2 memory.
We bought 8x H100 SXM5 GPUs last quarter.
升级到 CUDA 12.4 之后 kernels 不再报错。
错误码 E1001 表示 partition 校验失败。
The RTX 4090 is a consumer card, not a datacenter part.
Our billing SKU is DB-RAM-0032-GB per month.
```

拿这六句话去做 embedding，向量空间里 `A100`、`H100`、`E1001`、`4090`
全都是"一串字母加数字"，落在几乎同一个邻域。你检索 `A100`，
最像的是 `H100` 那句和 `4090` 那句 —— 因为它们共享"显卡型号"这个语义方向。
用户要的是**精确的那一行**，模型给的是"语义上像的一堆"。型号、错误码、
SKU、GitHub issue 号、函数名、股票代码、身份证号，全都属于这一类：
**它们没有语义，只有字面**，而字面正是向量模型唯一不擅长的东西。
把 `40 GB` 和 `8x` 的差别说出来，得靠倒排索引。

我们用本教程的 `mini_lancedb` 在这张表上实测一遍：

```
'A100'             -> [(0, 1.6777)]   查询词项 ['a100']
'H100'             -> [(1, 1.5836)]   查询词项 ['h100']
'CUDA'             -> [(2, 1.3556)]   查询词项 ['cuda']
'E1001'            -> [(3, 1.4995)]   查询词项 ['e1001']
'DB-RAM-0032-GB'   -> [(5, 5.5009)]   查询词项 ['db', 'ram', '0032', 'gb']
'4090'             -> [(4, 1.6777)]   查询词项 ['4090']
```

每一问都命中唯一的那一行，分数是 BM25 的 `_score`（越大越相关，见 17.8）。
注意最后一行：`DB-RAM-0032-GB` 被切成四个词项，四个同时命中的文档拿了 5.5 分
—— 这个"总分 = 各词项贡献之和"的结构是 BM25 的核心，17.3 会把它拆开。

这就是 LanceDB 同时提供 `_distance`（向量）和 `_score`（全文）两条路的原因。
两条路各自有明确的能力边界，谁都不该假装自己能覆盖对方。

## 17.2 倒排索引：从"一段文本"到"一张查得到的表"

正排是 `文档 → 词项`，倒排把它翻过来存 `词项 → 文档`：

```
term        -> [(doc, tf, positions), ...]
"quick"     -> [(0, 2, [1, 5]), (7, 1, [3])]
"brown"     -> [(0, 1, [2])]
```

于是"哪些文档含 quick"从"扫全表"变成"查一次字典"。全文检索能做到亚线性，
秘密全在这里，没有别的魔法。

构建过程是这样一条流水线：先把每篇文档过一遍分词器得到词项流，再把词项流
按 `词项 → (文档, 词频, 位置表)` 聚合，最后把聚合结果摊平成几条平行数组。
本教程的 `InvertedIndex._build_from()` 就是这么写的：先在 Python 侧用
`Dict[str, Dict[int, Tuple[int, List[int]]]]` 聚合，再一次性摊平，
避免 numpy 数组反复扩容。

摊平之后的物理布局刻意对齐真实 Lance 的 `invert.lance`：

- `terms`：**排好序**的词项表。排序不是为了查找（查找走哈希），
  而是为了让"按词项前缀扫描""合并两个 partition 的字典"能顺序做；
- `doc_offsets`：长度 `num_terms + 1` 的前缀和数组，`[doc_offsets[t], doc_offsets[t+1])`
  就是词项 `t` 的 postings 区间。一个词项一段**连续内存** —— 遍历时全是顺序访问，
  这是倒排索引快的第二个原因（第一个是字典查找）；
- `postings_doc` / `postings_tf`：摊平的 `(文档号, 词频)` 两条平行数组；
- `pos_offsets` + `positions`：位置压成一段字节流。位置在文档内是递增的，
  所以先做**差分**（第一个存绝对值，后面存与前一个的差），差分值绝大多数很小，
  再用 varint（LEB128）编码，一个字节就能装下大多数。

### 位置里存的到底是什么

位置 = 词项在**基础分词之后、各过滤器之前**的序号。它有两个用途，都不是"显示"：

**第一，短语查询。** 查询 `"quick brown"` 要求文档里这两个词项的位置差为
**查询里的偏移差**。这条要求在 17.7 会用一个真实反例展开。

**第二，邻近度打分。** 两个词项出现的位置越近，通常越相关。本实现没做
proximity scoring，但位置数据在，做起来只是加一个打分项。

位置的空间代价是可以量出来的。8000 篇各 30 词的合成语料、词表 2000：

```
with_position=True : size=4,180,810 B  postings=238,244  pos_data=240,000 B  (1.00 B/position)
with_position=False: size=3,940,810 B  postings=238,244
```

注意 `pos_data` 里每个 varint 对应的是一个**位置**（一次 token 出现），
不是一条 posting：这份语料一共 8,000 × 30 = **240,000 个位置**，位置流
也正好 240,000 字节，即 **1.00 字节/位置** —— 因为差分后绝大多数值落在
0..127，正好一个 varint 字节。总位数除以位置数才是位置的真实单价，
拿它去除 `postings`（238,244 = 不同的"词项—文档"对数，比位置数少）
会算出偏大的 1.01。这是"位置很便宜"的一面。

但真实 Lance 文档给的是另一面：官方格式规范里写着，**`with_position: true` 时
建 FTS 索引的临时磁盘空间"很容易需要原列的 10 倍以上"**，不开位置时通常
小于原列、总共一般不到 2 倍（来源：Lance 格式规范的 FTS 章节；这类"构建期
临时空间"的口径和一般文件布局一样，见第 3 章）。为什么差这么多：开了位置，每篇文档的每个词项都要带上一个
位置列表，构建期的中间缓冲、merge 期的重写、压缩块的边界对齐全都要按
"最坏情况"分配。所以索引体积不是唯一成本，**构建期的峰值空间**才是大头。

这也解释了官方在性能页里点名的那个坑：**`with_position=True` 和
`remove_stop_words=False` 都会显著膨胀索引体积与构建时间，只在真的要
短语查询时才开。**

**量一下构建成本。** 在 10 万行、内容形如 `item-NNNNNN-abcdef` 的文本列上，
本实现的 FTS 索引构建耗时 4,302.6 毫秒、落盘 8,500,747 字节（约为原始列的
3.86 倍）——这个数字和第 18 章 §18.6 的成本表用的是同一份数据、同一次
测量，可以横向对照。（真实 Lance 用 Rust、把分词和 postings 编码做进
原生代码，会快一个数量级；但"分词占掉构建时间的大头、且有峰值内存"
这个结构性事实不变。）

### 真实 Lance 的四个文件

真实 Lance 的 FTS 索引是四个逻辑文件（文件布局的通用规则见第 3 章；下面
这些是 Lance 格式规范里 FTS 部分列出的具体文件）：

`tokens.lance` 是词项字典（`_token` / `_token_id`）；`docs.lance` 存文档元数据，
只有 `_rowid` 和 `_num_tokens` 两列 —— `_num_tokens` 就是 BM25 里的 `|d|`；
`invert.lance` 是每个词项的压缩 posting list，除了 `_posting` 还带
`_max_score`（该词项的最大分，WAND 剪枝用，17.10 展开）、`_length`（含该词项的文档数，即 `df`）
和 `_compressed_position`（短语查询用的位置表，可空）；
`metadata.lance` 是索引元数据与 tokenizer 配置。

我们在本机装了真实的 `lancedb` 0.40.0，建一个 FTS 索引，落盘确实是这四个
（带 partition 前缀）：

```
metadata.lance        898 B
part_0_docs.lance     671 B
part_0_invert.lance  2589 B
part_0_tokens.lance   731 B
```

`part_0_` 前缀来自分区：FTS 索引可以切成多个 partition，每个 partition
一套自己的文件，`metadata.lance` 列出所有 partition ID。查询时**每个 partition
都要搜再合并排名**，所以 partition 越少查询通常越快（每多一个 partition
就多一次字典查找和 posting 扫描）。实现自己的教学版时不需要分区，但要知道
"为什么真实实现的文件名前面有个 `part_0_`"。

顺带一个实测：没有 FTS 索引时用 `query_type="fts"` 搜索，真实 LanceDB 直接报错，
不静默退化：

```
ValueError: Invalid input, Cannot perform full text search unless an INVERTED
index has been created on at least one column
```

本实现里对应的行为是找不到 FTS 索引就报 `IndexNotFoundError`。**"查不到"和
"没索引"必须是两个不同的信号** —— 搜索引擎最糟糕的失败模式就是把配置错误
伪装成"没有结果"。

### 增量更新与删除：幽灵 posting

倒排索引不像向量索引那样能"就地改一个 posting"。它的 postings 是一段段
连续内存（后面还拖着位置字节流），**在中间插一行或删一行都会打乱之后所有
偏移**。所以真实引擎用两种办法绕开：

**删除——打墓碑（tombstone）。** 删一行时不动物理 postings，只在别处记
一个"这一行已删除"的掩码；命中的 posting 照样读出来，返回前按掩码过滤掉。
这和 15.8 讲 HNSW 删除时的 `keep` 掩码是同一个套路，代价也一样：**被删的行
变成"幽灵 posting"，占着索引体积、拖慢扫描，还会混进 `df` 和 `avgdl`**，
把统计量带偏。所以删除多了必须 compact。

**更新——先删后插，或者 merge。** 真实 LanceDB 在持续写入时会建一个小的
**delta（补丁）索引**：新写入的行先进补丁，查询时主索引和补丁一起搜、再
合并排名（合并策略见第 23 章）。补丁大到一定程度就把它和主索引合并成一份。
本实现没有 delta 索引，只支持全量重建——**压实之后索引直接标记失效**
（见第 18 章 §18.6 最后一条）。

两种办法的共同点是：**倒排索引的"增量"本质上都是"追加 + 事后合并"，
而不是"原地修改"。** 接受这一点，"为什么 LanceDB 要在后台补建索引、
为什么 `num_unindexed_rows` 会长期不为零"就都顺了。

### 分区：每个 partition 有自己的 df 和 avgdl

前面说过 FTS 索引可以切成多个 partition（文件名的 `part_0_` 前缀），
查询时每个 partition 都要搜再合并。这里有个**打分正确性的关键**必须点破：
**每个 partition 是独立建起来的，所以它只能持有自己那一份的 `df` 和
`avgdl`（本地统计）；查询时各分区用本地统计分别打分，再合并排名。**

为什么不先合成全局统计再打分？因为全局 `avgdl` 要先知道所有分区的文档总长，
而分区设计的初衷恰恰是"每个分区独立、能并行建、能独立增删"——两者是冲突的。
代价是**同一个文档在不同分区划分下会拿到不同的 BM25 分数**：`df`、`avgdl`
都是分区的本地量，而 `idf(df) = ln(1 + (N-df+0.5)/(df+0.5))` 是 `df` 和
`N` 的联合函数——一个 `N` 大、一个 `N` 小的两个分区，同一个词算出的 `idf`
不可比，**直接跨分区比较分数就会系统性地抬高或压低某一边**。

所以合并排名只有两条正确路径：要么**把各分区的本地统计汇总成全局统计再
重算分数**（多一次聚合，但要保证可比）；要么**只用排名、不用分值**——
RRF 这一类融合天然免疫"两边分数量纲不同"（第 20 章会讲）。**"不同分区、
不同索引的分数不可直接相加"是混合检索里反复踩的坑**：真实场景里 FTS 分区
之间、以及向量 + FTS 之间，都要先把分数归一化到同一个空间再合并。

## 17.3 BM25 的完整推导

BM25 的公式，Lance / Lucene / tantivy 用的是同一版：

```
score(q, d) = Σ_t  IDF(t) · tf·(k1+1) / (tf + k1·(1 - b + b·|d|/avgdl))
IDF(t)      = ln(1 + (N - df + 0.5) / (df + 0.5))
```

真实 Lance 的 `lance-index/src/scalar/inverted/scorer.rs` 里常量就是这个：

```rust
pub const K1: f32 = 1.2;
pub const B:  f32 = 0.75;
pub fn idf(token_docs: usize, num_docs: usize) -> f32 {
    ((num_docs - token_docs as f32 + 0.5) / (token_docs as f32 + 0.5) + 1.0).ln()
}
```

三个部分各有各要解决的问题。

### TF 饱和：`k1` 在控制什么

朴素 TF-IDF 里 `tf` 直接乘上去。后果是：把关键词在文档里重复十遍，
文档就排到十倍前面 —— 而它并不比只提一遍的文档相关十倍。BM25 用一个
**饱和曲线**取代线性：

```
tf · (k1+1) / (tf + k1 · norm)
```

`tf` 从 1 涨到 2，得分涨得很快；从 50 涨到 51，几乎没差。这个上界
（`tf → ∞` 时趋近 `(k1+1)/norm`）就是"重复不能无限加分"的数学形式。

`k1` 控制饱和速度。`k1 → 0` 时曲线退化成"只看有没有"（词频完全不起作用）；
`k1` 越大越接近线性。实测（词干化关、停用词关、`b=0` 排除长度干扰，
两篇文档，一篇 1 个 apple、一篇 10 个 apple）：

```
k1=1e-09 b=0.0:  {0: 0.182322, 1: 0.182322}    ← 两篇完全同分：tf 不起作用
k1=1.2   b=0.0:  {0: 0.182322, 1: 0.358132}    ← tf 从 1 到 10，分数只涨 1.96 倍
```

第二行是饱和的直接证据：**tf 涨了 10 倍，分数只涨了不到 2 倍。**

### 文档长度归一化：`b` 在控制什么

长文档天然更容易命中任意一个词 —— 它包含的词多。不修正的话，检索结果会被
长篇大论霸占。BM25 按"比平均长度长多少"打折：

```
norm = 1 - b + b · |d| / avgdl
```

`b=0` 完全不归一化（适合长度本来就该随意的情况，比如日志：一条 500 行的
堆栈和一条 1 行的健康检查不该被同等看待）；`b=1` 完全归一化（适合长度均匀的
正文）。实测（两篇文档：一篇 `apple`，一篇 `apple` 加 20 个噪声词）：

```
b=0.0 :  {0: 0.182322, 1: 0.182322}    ← 长短一样分
b=0.75:  {0: 0.290275, 1: 0.132897}    ← 长文档被扣掉一半以上
```

这三个断言（`k1→0` 时 tf 失效、`b=0` 时长度失效、`b>0` 时长文档被扣分）
比"分数等于某个数"更能说明 BM25 在干什么 —— 它们在 `tests/test_fts.py`
里都是独立的测试用例。

### IDF：为什么是 `ln(1 + ...)`

经典 RSJ 形式的 `ln((N - df + 0.5) / (df + 0.5))` 有个著名的缺陷：当 `df`
逼近 `N` 时分子变成负数，于是**一个几乎出现在所有文档里的词会获得负分** ——
"文档里多一个词反而降分"。Lucene 4.0 加了那个 `1 +` 就是为了把 IDF 压成恒正。
本实现照抄了这一版，测试里显式钉住了 `idf(N) > 0`。

IDF 单调递减于 `df`：越稀有的词越值钱。实测三者（N=3）：`idf(1)=0.980829`、
`idf(2)=0.470004`、`idf(3)=` 一个很小的正数。这也顺便解释了"为什么要去停用词"：
停用词的 `df ≈ N`，IDF 几乎为 0，留着它们对排序毫无贡献，只会撑大 postings。

### 用一个 3 文档语料手算一个分数

语料（词干化关、停用词关，好算），`k1=1.2`、`b=0.75`：

```
d0 = "apple banana banana"   |d0| = 3
d1 = "apple cherry"          |d1| = 2
d2 = "banana"                |d2| = 1
N = 3,  avgdl = 2
```

文档频率：`df(apple)=2`、`df(banana)=2`、`df(cherry)=1`。于是

```
idf(apple) = idf(banana) = ln(1 + (3-2+0.5)/(2+0.5)) = ln(1.6)  = 0.47000362924573563
idf(cherry)              = ln(1 + (3-1+0.5)/(1+0.5)) = ln(8/3)  = 0.98082925301172630
```

查询 `apple banana`，逐篇算。

`d0`（`|d|=3`，`norm = 0.25 + 0.75·(3/2) = 1.375`，分母 `tf + 1.65`）：

```
apple  tf=1 -> 1·2.2 / (1+1.65) = 2.2/2.65   = 0.8301886792452831  × idf = 0.39019169220400696
banana tf=2 -> 2·2.2 / (2+1.65) = 4.4/3.65   = 1.2054794520547945  × idf = 0.56657971744691430
                                                          合计 d0 = 0.95677140965092125
```

`d1`（`|d|=2`，`norm = 1.0`，分母 `tf + 1.2`）：只有 `apple`，`tf=1`，
`2.2/2.2 = 1.0`，乘 `idf = 0.47000362924573563`。**d1 合计 = 0.47000362924573563**。

`d2`（`|d|=1`，`norm = 0.25 + 0.75·0.5 = 0.625`，分母 `tf + 0.75`）：只有 `banana`，
`tf=1`，`2.2/1.75 = 1.2571428571428571`，乘 `idf = 0.59086170533749631`。
**d2 合计 = 0.59086170533749631**。

跑代码对照：

```
doc_lengths: [3, 2, 1] avgdl: 2.0
idf(2)= 0.47000362924573563  idf(1)= 0.9808292530117263
   doc0 score=0.95677140965092122
   doc2 score=0.59086170533749627
   doc1 score=0.47000362924573563
```

三篇的分数逐位对上手算值（`tests/test_fts.py::test_bm25_hand_computed_score_on_tiny_corpus`
要求 1e-6 内一致）。

这里有个值得停一下的地方：排序是 `d0 (0.957) > d2 (0.591) > d1 (0.470)`，
而 `d1` 和 `d2` 都只有一个词命中、两个命中词的 `df` 都是 2、`tf` 都是 1。
**唯一的不同是文档长度**：`|d2|=1` 远短于 `avgdl=2`，归一化因子
`norm = 0.25 + 0.75·0.5 = 0.625`，而 `|d1|=2` 恰好等于平均值，
`norm = 1.0`。分母越小分数越高，于是短文档赢。**"短文档更值钱"不是 bug，
是长度归一化的设计意图。**

同一套机制也解释了一个常见困惑：为什么 `A100` 这种词在只有一行提到它的
表里能给出 1.68 的高分？把 17.1 那张表的 6 篇文档拿来看
（停用词关在管线里但影响已计入长度）：

```
doc_lengths = [6, 7, 10, 8, 6, 8]   avgdl = 7.5
df(A100) = 1  ->  idf = ln(1 + (6-1+0.5)/1.5) = ln(4.66667) = 1.540445
doc0 的 |d| = 6  ->  norm = 0.25 + 0.75·(6/7.5) = 0.85
                     w = 1·2.2 / (1 + 1.2·0.85) = 2.2/2.02 = 1.089109
                     score = 1.540445 · 1.089109 = 1.677712    ← 实测 1.6777
```

`explain("A100", 0)` 的输出逐项对得上。所以"1.68 算高"是因为
**IDF 的地板本来就高**（`df=1`、`N=6` 时 idf 已经是 1.54），
再乘上一个略大于 1 的 tf 权重。

### explain：把分数拆开看

BM25 的分数是**加性**的，所以可以逐词项拆开。实测 `explain("apple banana", 0)`：

```
{'term': 'apple',  'in_doc': True, 'tf': 1, 'df': 2, 'idf': 0.47000362924573563, 'norm': 1.375, 'score': 0.39019169220400696}
{'term': 'banana', 'in_doc': True, 'tf': 2, 'df': 2, 'idf': 0.47000362924573563, 'norm': 1.375, 'score': 0.5665797174469143}
total: 0.9567714096509212
```

这是排查"为什么这篇排前面"唯一靠谱的工具：IDF 异常（词太常见）、
tf 异常（刷词）、norm 异常（文档太长/太短）一眼可见。

## 17.4 分词器管线：七个阶段，每个都能单独拨

**全文检索的第一性问题是：查询串和文档串必须被同一套规则切成同一批词项。**
只要两边对不上，倒排索引就永远查不到 —— 而且这种错误是静默的：
索引建得出来、查得动、只是结果为空。所以真实 LanceDB 把一个
可配置的 tokenizer 配置**写进索引元数据**，本实现也是
（`metadata.params["tokenizer"]`，`load()` 时读回来）。

管线是一条**基础分词器 + 一串过滤器**的链：

```
原始文本 ──► [ascii 折叠] ──► 基础分词 ──► 小写化 ──► 去停用词 ──► 词干化 ──► 长度过滤 ──► 词项流
                                  ▲
                    base_tokenizer: simple / whitespace / ngram / raw
```

`analyze()` 会把每一阶段的词项流都留下来，这是调 FTS 的第一件工具：

```
>>> print(analyze("The RUNNING dogs").render())
  原始文本     : The RUNNING dogs
  ascii 折叠 : The RUNNING dogs
  基础分词     : 'The' 'RUNNING' 'dogs'
  小写化      : 'the' 'running' 'dogs'
  去停用词     : 'running' 'dogs'
  词干化      : 'run' 'dog'
  长度过滤     : 'run' 'dog'
  最终词项     : run@1[4,11), dog@2[12,16)
```

每一行都能单独拨。实测每个开关（文本 `The RUNNING Dogs 跑得快`，
为隔离效果，不相关的开关一律关掉）：

```
lower_case=False : ['The', 'RUNNING', 'Dogs', '跑得', '得快']
lower_case=True  : ['the', 'running', 'dogs', '跑得', '得快']
stem=False       : ['running', 'dogs', ...]
stem=True        : ['run', 'dog', ...]
stop=False       : ['the', 'running', 'dogs', ...]
stop=True        : ['running', 'dogs', ...]
max_token_length=5: ['the', 'dogs', '跑得', '得快']     ← 'running' 被整个丢掉
with_position=False: [None, None, None]                 ← 位置变成 None
```

`max_token_length` 值得单独说一句：本实现（跟 tantivy 的 `RemoveLongFilter`）
是**丢弃**超长词，不是**截断**。Lucene 的 `StandardTokenizer.maxTokenLength`
是截断到 40 字符。差别是真实的：截断会让两个不同的长词变成同一个词项（假匹配），
丢弃只会降低召回。假匹配比漏召严重得多。实测 `max_token_length=5` 下
`"averyverylongtoken ok"` 得到 `['ok']` —— 长词整个消失，而不是变成 `avery`。

四种基础分词器的差别实测（文本 `hello, world! 你好世界`）：

```
base=simple      : ['hello', 'world', '你好', '好世', '世界']
base=whitespace  : ['hello,', 'world!', '你好世界']
base=raw         : ['hello, world! 你好世界']
base=ngram(2,3)  : ['ab', 'abc', 'bc']            ← 文本 "abc"
```

`whitespace` 把标点留在词里（`hello,` 是一个词项），这是它和 `simple` 的
关键差别，也是"改 tokenizer 等于换索引"的典型例子。`raw` 把整段当一个词项，
配上 `max_token_length` 就是个真实的坑：`raw` + `max_token_length=5` 时
`"hello world"` 会被整个丢掉，索引里一个词项都没有。

组合起来还有一批反直觉的坑：`raw` + 长度过滤会让长文本直接消失；
`with_position=False` 会让短语查询彻底失效（本实现在这种情况下
**显式报 `QueryError`**，而不是给一个没有位置依据的近似答案 ——
静默给错答案比报错糟糕得多）。

### 本实现的默认值：两处不同，另有若干能力没实现

真实 LanceDB 的 Python `tokenize()` 签名（本机 0.40.0 实测）默认值是：
`base_tokenizer='simple'`、`language='English'`、`max_token_length=40`、
`lower_case=True`、`stem=True`、`remove_stop_words=True`、`ascii_folding=True`、
`ngram_min_length=3`、`ngram_max_length=3`、`prefix_only=False`。

和本实现比，差别分**两类**：一类是**两个默认值的取值不同**，另一类是
**若干能力干脆没实现**。先看默认值，有两处，都是刻意的：

**`ascii_folding` 本实现默认关，真实默认开。** 关掉它的时候 `Café` 是词项
`café`，开了之后是 `cafe` —— 后者才能和用户敲的 `cafe` 对上。

**`with_position` 本实现默认开，真实建索引时默认关。** 真实默认关是因为
位置很贵（见 17.2），而大多数场景不做短语查询。本实现默认开，
是因为教学里"短语查询为什么需要位置"是要亲手跑一遍的，不想让读者先踩
"为什么我的短语查询报错"。

另一类是**能力缺口**，不是默认值差异：本实现**没有** `prefix_only`
（真实 LanceDB 的 ngram tokenizer 支持"只索引前缀 n-gram"），也没有
`icu` / `jieba/*` / `lindera/*` 这些需要模型文件或预编译词典的 base
tokenizer —— 17.6 会讲 CJK 用什么替代。这些不是"默认关掉了"，而是
"这条代码路径不存在"，调到它只会报错。

### `custom_stop_words` 是"取代"而不是"追加"

本实现（对齐真实语义）里传 `custom_stop_words` 会**完全取代**内置表。
想追加得自己拼 `sorted(ENGLISH_STOP_WORDS | {"foo", "bar"})`。

这一条**两份实现的实测是一致的**（都传 `custom_stop_words=["zebra"]`、
文本 `the zebra runs`）：

```
真实 lancedb 0.40.0 : tokenize("the zebra runs", custom_stop_words=["zebra"]) -> [the, run]
本实现              : TokenizerConfig(custom_stop_words=["zebra"])            -> [the, run]
（两边都不传 custom_stop_words 时都是 [zebra, run] —— 内置表照常生效）
```

关键在 `the`：它是内置英文停用词，本该被过滤，但传了 `custom_stop_words`
之后它**留下了** —— 说明内置表被整张替换掉了，而不是"在内置表之上再追加
`zebra`"。真实 `lancedb` 0.40.0 的 `index.FTS` docstring 也把语义写死成
同一件事："Custom words **replace** the built-in language stop words and
only take effect when `remove_stop_words` is True"。

**要小心的是网上流传的另一套说法。** 二手资料里常把 `custom_stop_words`
描述成 "**Extra** stop words to drop in addition to the language defaults"，
读起来像"追加"。这句话在当前官方文档里**找不到**，而且和上面的实测
**相反**。这类"文档措辞（还是以讹传讹的措辞）vs 实测行为"的分歧，
唯一可靠的判据是**拿手上的版本实测一遍**。它属于一整类接口差异，
**应当作为备选收进附录 D 的 B 类**（B 类正是按"文档措辞 vs 实测行为"
分组的清单）—— 这里只是指个去处，不代表附录里已经有这一条。

### ASCII 折叠为什么要记字符映射

折叠会改变字符串长度：`ß` 一个字符变成 `ss` 两个字符。如果不记录
"折叠后的第 j 个字符来自原文的哪一段"，算出来的字节偏移就全错 ——
而这正是"在原文里高亮命中片段"时唯一不能错的东西。实测：

```
ascii_folding=False -> [('café', 0, 5), ('naïve', 6, 12), ('straße', 13, 20), ('ærø', 21, 26)]
ascii_folding=True  -> [('cafe', 0, 5), ('naive', 6, 12), ('strasse', 13, 20), ('aero', 21, 26)]
```

注意 `('straße', 13, 20)`：词项文本是折叠后的 `strasse`（7 个 ASCII 字符），
但 `[start, end)` 仍然是**原文**里的 13..20（7 个字节的 UTF-8 原文）。
两者长度不同、指向不同，正因为实现里存了那张映射才能同时对。

折叠本身用 Unicode 的标准套路：NFD 分解（`é` → `e` + U+0301）再丢掉
组合记号。好处是不用维护映射表就能覆盖整个拉丁字母系统；
坏处是对 `ß` / `ø` / `æ` 这些"没有分解形式"的字符无效，所以要额外补一张小表。
真实 Lucene 的 `ASCIIFoldingFilter` 有一张覆盖西里尔、希腊、数学符号的完整表，
本实现只挑了最常见的一批 —— 缺口是显式的，不是隐藏的。

## 17.5 Porter 词干化：规则型算法的祖师爷

词干化的目的很直白：让 `running` 和 `run` 落到同一个词项上，提高召回。
Porter 1980 的算法是这条路线的祖师爷，一行数据库都不需要，纯靠对英文
构词法的观察。本实现的 `PorterStemmer` 是经典实现的完整复刻（步骤 1a ~ 5b）。

核心概念只有三个，都围绕"**别把短词削得面目全非**"：

`m` 是词干里"元音-辅音"交替的对数（VC 序列的个数）。`tr` 是 `m=0`，
`tree` 是 `m=1`，`trouble` 是 `m=2`。贴 `e`、去重辅音这些规则都以
"`m > 1`（词够长）"为门槛。`vowel_in_stem` 是"词干里还有没有元音" ——
`-ed`/`-ing` 只在词干含元音时才删，否则会把 `sing` 削成 `s`。
`cvc` 是"结尾是不是辅-元-辅且末辅音不是 `w/x/y`"，这类词（`hop`、`run`）
要补回一个 `e`，即 `hop → hopping` 而不是 `hop → hoping`。

### 五组规则

**1a 复数。** `-sses → -ss`（`caresses → caress`）、`-ies → -i`（`ponies → poni`）、
`-s`（`cats → cat`）；遇到 `-ss` 结尾（`caress`）不动手，否则会把 `class`
削成 `clas`。三条分支刻意按最长匹配优先排列。

**1b 过去式/进行时。** `-eed` 只在 `m > 0` 时去掉一个 `e`（`agreed → agre`），
否则保留（`feed → feed`）—— 这是为了不破坏 `feed`/`bleed` 这类原形词。
`-ed`/`-ing` 在词干含元音时删掉，然后按结尾补回 `-ate`/`-ble`/`-ize`；
双写末辅音（`hopping`）去一个，但 `l/s/z` 例外（`roll`、`press`、`fizz`）。

**1c 词干含元音的 `-y` → `-i`**（`happy → happi`）。注意 `sky` 不动 ——
它的词干 `sk` 没有元音。

**2 / 3 派生后缀映射**，条件是 `m > 0`：`-ational → -ate`、`-tional → -tion`、
`-izer → -ize`、`-fulness → -ful`、`-ness → ""`、`-alize → -al` ……

**4 删后缀**，条件是 `m > 1`（词得够长才敢删）：`-al`、`-ance`、`-ment`、
`-ent`、`-ous`、`-ive` … 其中 `-ion` 只有在前一个字符是 `s` 或 `t` 时才处理
（`adoption` 可以，`lion` 不行）。

**5a 删结尾 `e`**（`m > 1`，或 `m == 1` 且结尾不是 cvc）；**5b 删双写 `l`**。

### 十几个真实词对

跑一遍本实现（官方测试集里的代表样本）：

```
running     -> run       dogs        -> dog       easily      -> easili
relational  -> relat     conditional -> condit    ponies      -> poni
caresses    -> caress    cats        -> cat       agreed      -> agre
feed        -> feed      plastered   -> plaster   motoring    -> motor
happy       -> happi     sky         -> sky       hopping     -> hop
hoping      -> hope      controlling -> control   generalizations -> gener
rational    -> ration    national    -> nation    triplicate  -> triplic
formative   -> form      sensibility -> sensibl   electrical  -> electr
callousness -> callous
```

`easily → easili` 和 `happy → happi` 看着丑，但**丑得一致**才是重点：
只要查询侧和索引侧用同一套规则，词项就永远是同一个。
`hopping → hop` 和 `hoping → hope` 必须落到不同词干，否则会把"跳"和"希望"
混起来 —— 这条在测试里是独立的用例。

### 两个已知缺陷

**第一，过度词干化。** `generalizations → gener` 把语义信息砍掉太多了，
`university` 和 `universe` 都会掉到 `univers`。规则型词干化不区分词性、
不看语境，只能靠"后缀像什么"来判断。

**第二，完全不处理不规则动词。** `went` / `gone` / `go` 三个词项永远对不上，
`was` / `were` / `be` 也是。要解决只能上词形还原（lemmatization），
那需要词典和词性标注 —— 那是另一个量级的东西。

### 一个容易踩的性质：Porter 不是幂等函数

对**已经**是词干的词再跑一遍，绝大多数不动（`run → run`、`relat → relat`）。
但有少数会继续缩：

```
agre -> agr     callous -> callou    decis -> deci
defens -> defen  ceas -> cea
```

原因不是 bug：step 4 / step 5 的删除只以 `m` 为准 —— 某个词干完全可能
"第一轮 m 还不够、第二轮刚好越过阈值"。官方 Porter 实现（Martin Porter
的 C 版）行为完全相同，本实现也把它当**特性**钉进测试
（`PORTER_SECOND_ROUND`）。

好消息是它**收敛**：迭代到不动点用不了几轮。这条性质比"幂等"更弱、
却更真实：只要它成立，"索引时多算一轮"就不会发散，词干化仍然是可用的
规范化手段。

## 17.6 CJK 怎么办：真实实现和教学实现的岔路

中文、日文、韩文没有空格分词。真实 LanceDB 的 `base_tokenizer` 支持
`jieba/*`（中文，需要下载模型到 Lance home 的 `jieba/` 目录）、
`lindera/*`（日文 `lindera/ipadic`、韩文 `lindera/ko-dic`，需要编译好的
词典）、以及 `icu`（ICU4X 的分词，**不需要外部模型文件**）。
需要模型文件的 tokenizer 从 Lance 的语言模型目录加载，
可以用 `LANCE_LANGUAGE_MODEL_HOME` 指到别处。

本机实测一个反直觉的默认行为：**真实 LanceDB 默认的 `simple` tokenizer
完全不分中文**：

```
>>> lancedb.tokenize("中国人民银行")
[FtsToken(text="中国人民银行", position=0)]
```

整个连续段是**一个词项**。这意味着"建了 FTS 索引的中文表，用默认配置搜
`人民` 是搜不到的"（除非碰巧整段字符串就是 `人民`）。中文场景必须显式选
分词器：`ngram` 或 `icu`，或者上 `jieba`。用 `ngram`（默认 3-gram）的效果：

```
>>> lancedb.tokenize("中国人民银行", base_tokenizer="ngram", stem=False, remove_stop_words=False)
[('中国人', 0), ('国人民', 0), ('人民银', 0), ('民银行', 0)]
```

注意所有 n-gram 的 position 都是 **0** —— Tantivy 的 `NgramTokenizer`
给同一位置的多个 gram 发相同的位置增量，这样它们之间的关系是"或"。
这也意味着基于 ngram 的短语查询**不能**靠位置来判相邻。

教学实现带不起词典，于是退化成 **CJK 连续段的字符二元组**：
`"中国人民银行"` → `中国 / 国人 / 人民 / 民银 / 银行`。这是中文检索的经典
做法（Lucene 的 CJKAnalyzer 也是这个思路），实测：

```
'中国人民银行' -> [('中国', 0), ('国人', 1), ('人民', 2), ('民银', 3), ('银行', 4)]
'你好，世界'   -> [('你好', 0), ('世界', 1)]      ← 全角逗号不并进 CJK 段
'a中b'        -> [('a', 0), ('中', 1), ('b', 2)]  ← 单字段就是一个单字词项
'上海银行'     -> [('上海', 0), ('海银', 1), ('银行', 2)]
```

为什么是二元组而不是单字：单字索引会让"中国"匹配到所有含"中"或"国"的文档，
IDF 直接失效（几乎每篇中文文档都含"的""是""在"）。二元组保留了局部顺序信息，
精度高得多。CJK 标点（U+3000..U+303F）和全角形式（U+FF00..U+FFEF）
被刻意排除在 CJK 段之外 —— 否则 `你好，世界` 会产生 `好，` / `，世`
这类纯噪声的二元组。

### 这个近似能做到什么、做不到什么

**能做到：** 任意中文子串的召回。`银行` 能召回"中国人民银行"和"上海银行"。

**做不到：** 词边界概念。`中国人民银行` 被切成五个二元组，其中 `民银`
在真实语料里几乎不出现，是个纯噪声词项 —— 它撑大索引但不提高精度。

**做不到：** 词典外的新词。二元组不需要词典，所以"造词"不是问题；
但反过来，`人工智能` 和 `智能人工` 共享一个二元组 `智能`，OR 语义下会互串。
精确匹配多字词**必须用短语查询**（见 17.7）。

**代价：** 索引膨胀。一段 n 个字的中文正文会产出 n-1 个二元组词项，
而英文同样长度的文本大概只有 n/6 个词项。真实 Lance 文档的建议也是
"中文用 ngram 或 icu 得到确定性的 shingle"——
Lance 格式规范的 FTS 章节里还提到 MinHash-LSH 索引对 CJK 文本
同样要求用 `ngram` 或 `icu`，因为这两个是确定性的。

实测中文检索的两条路（三篇文档）：查询 `中国人民银行` 和 `上海银行`
各自命中唯一的一篇；但查询 `中国 人民 银行`（中间有空格 = OR 语义）
会命中全部三篇 —— 包括只含"中国人民"的那篇。**中文检索里短语查询
不是"高级功能"，是基本功能。**

## 17.7 查询语法

本实现的查询串解析器支持一小组规则：空白是分隔符，`"..."` 是短语，
`(` / `)` 分组，`+` / `-` 在**词首**是出现方式标记，裸词
`and` / `or` / `not`（大小写不敏感）是布尔运算符。

**相邻 = OR** —— 这是搜索引擎的默认（DuckDuckGo、Google 都是），
想要求全部命中必须写 `AND` 或 `+`。`AND` 会把它两侧的 should 子句
**提升**为 must，所以 `(a b) AND c` 是"a、b、c 都得有"，和 Lucene 的经典行为一致。

实测在一个四篇文档的语料上（`the quick brown fox` / `quick brown dogs` /
`slow green turtle` / `quick the brown fox`）：

```
'turtle'             rows=[2]
'quick'              rows=[0, 1, 3]
'turtle OR quick'    rows=[0, 1, 2, 3]
'+quick -turtle'     rows=[0, 1, 3]
'quick AND fox'      rows=[0, 3]
'"quick brown"'      rows=[0, 1]
'"quick the brown"'  rows=[3]
'+quick brown'       rows=[0, 1, 3]
'NOT turtle'         rows=[0, 1, 3]
```

两个反直觉之处值得点出来。

**`should` 的语义随 `must` 是否存在而变。** `+quick brown` 只要求文档含
`quick`，`brown` 是纯粹的加分项 —— 命中更好、不命中也能返回。所以
`rows=[0,1,3]`（三篇都含 quick），而不是"含 quick 且含 brown"。这个规则
是布尔检索里最反直觉的一条，但它是**对的**：`+沙发 -皮质` 要的是
"含沙发的都给我，皮质沙发滚"，如果 should 也强制命中，就没法表达"可选加分"了。

**纯否定查询。** 本实现里 `-foo` 返回"全集减去 foo"。Lucene 在这种
"没有正向子句"的情况下返回**空集**（它认为没有起点）。我们选了更符合直觉的
前者，但这是个**有意的偏差** —— 真实系统里 `-foo` 这种查询请自己补一个
`+something`。

### 短语查询靠位置差，不靠"相邻"

`PhraseQuery` 要求的不是"两个词项位置相邻"，而是"它们的**位置差**等于
查询里的偏移差"。实测：

```
原文 'the quick brown'  -> [('quick', 1), ('brown', 2)]   位置差 1
原文 'quick the brown'  -> [('quick', 0), ('brown', 2)]   位置差 2
原文 'quick brown the'  -> [('quick', 0), ('brown', 1)]   位置差 1
```

查询 `"quick brown"` 的偏移差是 1，所以只有第一和第三种原文命中；
`"quick the brown"` 的偏移差是 2，只命中第二种。测试里把这条钉死了：
`test_phrase_query_respects_position_offsets` 断言文档 3（`quick the brown fox`）
**不**被 `"quick brown"` 命中。

中文检索的强证据也在测试里：`中国人民银行发布报告` / `上海银行也发布了报告` /
`中国人民很高兴` 三篇，用**整串查询** `中国人民银行`（会被切成五个二元组）
只命中第一篇，因为二元组的位置相邻恰好等价于原文相邻：

```
'中国人民银行'      -> [0]
'上海银行'          -> [1]
'中国 人民 银行'    -> [0, 1, 2]     ← OR 语义，全捞进来了
'"中国人民银行"'    -> [0]
```

所以中文的"精确匹配多字词"唯一正确的写法是短语查询（或者把查询串
不加空格地整段传进来，让它退化成短语）。

### 真实 LanceDB 的查询形态不一样

真实 LanceDB 的 Python API **不接受**在搜索串里写 `AND` / `OR` ——
官方文档明确写着 "Lance-based FTS doesn't support queries using boolean
operators `OR`, `AND` in the search string"。布尔/加权要用查询类
（`BooleanQuery` / `BoostQuery`），短语用 `PhraseQuery`，模糊用
`MatchQuery(..., fuzziness=n)`，多字段用 `MultiMatchQuery`。
官方说这是"Lance 原生实现"的结果，老的 Tantivy 选项（`use_tantivy`）
已经不再被索引创建 API 接受。

本实现把查询串解析和内部分数求值分成两层：`parse_query()` 产出
`BooleanQuery` / `TermQuery` / `PhraseQuery` 的树，`_eval()` 负责求值。
这不是照抄真实 API，而是为了让"查询语法"和"布尔语义"能分开讲 ——
真实 API 里这两层也是分开的（字符串形态 vs `FullTextQuery` 对象）。

### 对象 API：查询串只是语法糖

上面那些查询串底层就是一棵查询树。本实现把 `MatchQuery` / `BooleanQuery` /
`TermQuery` / `PhraseQuery` 都暴露出来，可以直接构造——尤其当查询不是
"用户敲进来的"，而是从别的系统拼装出来的时候：

```
>>> from mini_lancedb.index.inverted import BooleanQuery, TermQuery
>>> q = BooleanQuery(must=[TermQuery("quick")], must_not=[TermQuery("turtle")])
>>> idx.search_ranked(q, k=10)     # 语料：the quick brown fox / quick brown dogs /
                                   #       slow green turtle / quick the brown fox
(array([0, 1, 3]), array([0.3567, 0.3567, 0.3567]))
```

真实 LanceDB 的对象体系更全，而且参数不止"模糊度"一个。本机 0.40.0 实测
`inspect.signature`，两个最常用的长这样：

```
PhraseQuery(query, column, *, slop=0)
MatchQuery(query, column, *, boost=1.0, fuzziness=0, max_expansions=50,
           operator=FullTextOperator.OR, prefix_length=0)
```

- **`PhraseQuery(..., slop=n)`** 里的 `slop` 是"短语中间允许空出多少个
  位置"（docstring 原话：the maximum number of intervening positions
  permitted in the phrase）。`slop=0` 要求两个词项相邻；`slop=1` 就允许
  中间夹一个别的词，于是查询 `"quick brown"` 也能命中 `quick red brown`。
  这是**用可控的误召回换鲁棒**：`slop` 越大越能兜住"用户漏打了一个词"，
  代价是误匹配变多。本实现的 `PhraseQuery` 只有 `slop=0` 这一种：位置差
  必须严格等于查询里的偏移差。
- **`MatchQuery(..., operator=OR)`** 控制的是"查询串被切成多个词项之后怎么
  组合"：默认 `OR`（命中任意一个就算），改 `operator=FullTextOperator.AND`
  就是**全部词项都要命中**。注意这里的 `And`/`Or` 是**对象参数**，不是查询
  串里写的布尔词 —— 字符串里写 `AND` 会被拒绝（上一节刚讲）。这正是对象
  API 相对字符串 API 多出来的表达能力。本实现的布尔语义由 `parse_query()`
  从查询串里解析（`a AND b`），没有"给整条 `MatchQuery` 挂一个 operator"
  的入口。

`BooleanQuery` / `MultiMatchQuery`（多列）以及标量侧的一整套也都有。
**`BoostQuery` 是字符串 API 给不了的能力**——"标题命中比正文命中更重要"
这类权重只能靠它表达（`MatchQuery` 自己那个 `boost` 只能给**整条**查询
加权，给不了布尔树里的某一个分支）。本实现没有做加权，所以也就不提供
`BoostQuery`：**没实现的东西就不该有入口，否则用户会以为它在生效。**

### 本实现没有、真实引擎有的三类查询

**模糊查询（fuzzy / 编辑距离）。** 真实 LanceDB 的 `MatchQuery(..., fuzziness=n)`
支持"Levenshtein 距离 ≤ n"的匹配，底层是自动机（Lucene 的 `FuzzyQuery` 用
Levenshtein automaton，tantivy 同理）。本实现完全没有——查错一个字母就是
查不到。**要不要上模糊，取决于你是不是在替用户兜打字错误**：term 精确匹配
对"型号、错误码"这类"没有语义、只有字面"的查询就是对的（见 17.1），
对"用户随手敲的自然语言关键词"才需要模糊。

**前缀与通配查询。** `foo*`（前缀）能直接用**排好序**的词项表做一次范围
扫描——`bisect` 定位到 `foo` 的起点，一路取到第一个不以 `foo` 开头的词项
为止，再把这些词项的 postings 求并。**这恰好是词项表要排序的第二个用途**
（第一个是 17.2 说的"合并两个字典"）。任意通配（`f*o`）复杂得多，真实引擎
要么退化成"遍历词项表筛前缀"，要么直接拒绝。本实现两者都没做。

**多列 / 混合检索。** 本实现是一列一个 FTS 索引；跨列搜索、以及"向量 +
全文"的混合检索，归第 20 章。真实 LanceDB 的原生 FTS **一列只能建一个
索引**，多列要分别建再在查询层融合（附录 D 第 32 条记了这个限制：
`Native FTS indexes can only be created on a single field at a time`）。

**真实 LanceDB 的短语查询需要两个开关。** 官方文档说得很硬：
"To search for a phrase, the index must be created with `with_position=True`
**and** `remove_stop_words=False`"。为什么第二个开关也要？因为在同一个语料上实测：

```
remove_stop_words=True : PhraseQuery('quick brown')     -> [0, 1]
                         PhraseQuery('the quick brown') -> [0, 1]   ← 假匹配！
remove_stop_words=False: PhraseQuery('quick brown')     -> [0, 1]
                         PhraseQuery('the quick brown') -> [0]      ← 正确
```

（语料：`the quick brown fox` / `quick brown dogs` / `quick the brown fox`，
建索引时 `with_position=True`。）

`remove_stop_words=True` 时，查询串里的 `the` 被停用词表吃掉，短语
`"the quick brown"` 静默退化成 `"quick brown"`，于是把 `quick brown dogs`
也捞了进来。**这就是"短语查询悄悄多匹配出文档"，是比索引大几个字节
严重得多的 bug。**

值得强调的是：**这不是哪一个实现做错了，而是"位置有没有空档"和
"停用词有没有被删"这两件事共同决定的后果。** 本实现和真实 LanceDB
在这个语料上逐问一致（两边都实测过）：

```
                 stop=True            stop=False
"quick brown"    [0, 1]   (真 lance: [0, 1])   [0, 1]
"the quick brown"[0, 1]   (真 lance: [0, 1])   [0]      ← 只有关掉停用词才对
"quick the brown"[3]      (真 lance: [3])      [3]
```

两边之所以一致，是因为"位置差"才是短语查询的唯一依据，而两边的**位置差**
算法等价：过滤只**删**元素、不改序号，于是 `quick the brown` 在两边都
保留"差 2"这个事实，`"quick brown"`（差 1）就永远匹配不上它。
两边的**绝对**位置可以不同 —— 真实 LanceDB 会把开头的停用词"归一化"掉
（实测 `tokenize("the quick brown")` 得到 `quick@0, brown@1`），
本实现保留它们在基础流里的原始序号（`quick@1, brown@2`）——
但只要位置差对，短语查询的结果就对。

这条经验可以带走去写自己的引擎：**只要"位置 = 基础词项流里的序号、
过滤器只删元素不改序号"这一条守住，短语查询的语义就对了。**
`tests/test_fts.py::test_position_increments_leave_gaps_for_removed_tokens`
就是钉这一条的。

## 17.8 打分方向：整个 FTS 里最容易搞错的一件事

**向量检索的 `_distance` 越小越好（L2 距离、cosine 距离）。
全文检索的 `_score` 越大越好（BM25 相似度）。**

这不是记性问题，是**混合检索（hybrid search）里的头号 bug 来源**：
两路结果合并时，向量那一路要升序、FTS 这一路要降序；想做加权融合
必须先把两边的分数归一化到同一个区间，否则一个 0.3 的距离和一个 12.7 的
BM25 分数相加，等于只保留了 BM25。

真实 LanceDB 在结果里给的是两个不同的列名：`_distance` 和 `_score`。
本机实测一次真实 FTS 查询：

```
>>> t.search("brown", query_type="fts").limit(10).to_list()
[{'id': 1, ..., '_score': 0.378813},
 {'id': 0, ..., '_score': 0.336981},
 {'id': 3, ..., '_score': 0.336981}]
```

（语料同上，建索引时 `with_position=True, remove_stop_words=False`，
所以每篇的 token 数是 `the quick brown fox`=4、`quick brown dogs`=3、
`slow green turtle`=3、`quick the brown fox`=4，`N=4`、`avgdl=3.5`，
`df(brown)=3`。）

这三个值用手算的公式能对上，精确到小数点后五位：

```
idf(brown) = ln(1 + (4-3+0.5)/(3+0.5)) = ln(1.428571) = 0.356675
doc1(|d|=3): norm = 0.25+0.75·(3/3.5) = 0.892857
             w = 1·2.2 / (1+1.2·0.892857) = 2.2/2.071429 = 1.062069
             score = 0.356675 · 1.062069 = 0.378813     ← 与实测 0.378813 一致
doc0(|d|=4): norm = 0.25+0.75·(4/3.5) = 1.107143
             w = 2.2 / (1+1.2·1.107143) = 2.2/2.328571 = 0.944785
             score = 0.356675 · 0.944785 = 0.336981     ← 与实测 0.336981 一致
```

doc0 和 doc3 分数相同（都是 4 个 token、`brown` 的 tf 都是 1）；
doc1 短一个 token，所以同样的 tf 拿了更高的分数。**真实 LanceDB 的
`_score` 就是本实现 `InvertedIndex` 算的那条公式**，逐位对得上。

本实现保持同样的约定：`InvertedIndex.search_ranked()` 返回
**按 `_score` 降序**的 `(行号, 分数)`。测试里有一条显式的方向断言：

```python
def test_bm25_scores_are_descending_not_ascending():
    idx = make_index(BM25_DOCS, config=BM25_CONFIG)
    _, scores = idx.search_ranked("apple banana")
    assert list(scores) == sorted(scores, reverse=True)
    rows, _ = idx.search_ranked("apple banana")
    assert int(rows[0]) == 0    # 两词都命中的文档排第一
```

另一件相关的事：**并列分数的名次必须可复现。** 本实现用
`np.lexsort((row_ids, -scores))` —— 分数相同时按行号升序。不这么做的话，
同样的查询在不同机器/不同 numpy 版本下顺序不同，测试会间歇性失败。

## 17.9 一个端到端的实测：词干化到底换来了什么

这一节把所有零件装起来。语料三行，同一件事的三种说法：

```
running fast in the park
she runs every morning
the dog is sleeping
```

查询 `run` / `running` / `runs`，分别在 `stem=True` 和 `stem=False` 的索引上：

```
stem=True  q='run'      -> rows [0, 1]
stem=True  q='running'  -> rows [0, 1]
stem=True  q='runs'     -> rows [0, 1]
stem=False q='run'      -> rows []         ← 一个都不命中
stem=False q='running'  -> rows [0]
stem=False q='runs'     -> rows [1]
```

这张表就是词干化存在的全部理由。**开启词干化之后，`run` / `running` / `runs`
三种写法互相召回，召回率从 1/3 变成 3/3**；代价是精度：`university` 和
`universe` 会互串（都掉到 `univers`）。这是一个必须由业务决定的取舍，
不是一个"更好的默认值"。

同一节的另一个实测是索引体积（就用上面这**三**行的语料，默认配置）：

```
with_position=True : 300 B   pos_data = 8 B
with_position=False: 292 B
```

**8 个位置用了 8 字节的位置流——正好 1 字节/位置**，因为这三篇文档里
所有位置差分都落在 0..127。再对照 17.2 那份 8000 篇语料（1 字节/位置），
规律一致：**varint + 差分对"位置"这种递增小整数是近乎无损的**，
真实引擎再叠一层按块 bitpacking 还能更省。

最后，一个把"索引必须自描述"落地的细节：索引建完之后，实际生效的
tokenizer 配置会被回填进元数据：

```
stats()['tokenizer'] == 'simple+lower+stop+stem(English)+pos'
```

换一套 tokenizer 去查老索引，等于拿另一把钥匙开锁 —— 而且打不开时是
静默的（结果是空集）。把配置写进索引元数据，才能让"打开的索引知道自己
当初是怎么切的"。

### 怎么评 FTS 的 precision / recall

向量那几章一直在说 recall@k，FTS 这边得先把"相关"定义清楚。**对精确术语
查询，"相关集"是有明确定义的：包含该词项的全部文档**——就是这个词项的
postings 集合。于是：

**召回率 = 返回的相关文档数 / 相关文档总数**（分母就是该词项的 `df`）；
**精确率 = 返回的相关文档数 / 返回总数**。关键推论是：**如果返回全部命中
（`k ≥ df`），term 查询的召回率恒为 1**——BM25 会把所有命中都算出来，
只是给它们排序。**一旦截断到 `top-k`，问题就从"能不能找回"变成了"排得
对不对"**，所以 FTS 评测真正看的是排序质量：**nDCG、MAP、P@k**（它们惩罚
"相关的没排在前面"），而不是 recall。

而真正会让 FTS 召回率掉下来的，全都发生在**分析链**上，不在打分公式里：
查询侧和索引侧分词不一致（17.4）、词干化把两个不同的词并成一个（17.5 的
`university` / `universe`）、CJK 走二元组导致"精确多字词"必须改用短语查询
（17.6）。**FTS 的召回瓶颈在分词，不在 BM25。**

## 17.10 排名剪枝：WAND 与 block-max WAND

前面所有实测都有一个共同的前提：**命中集合不大、而且我们把它全算出来了。**
本实现的 `_eval()` 返回一个长度等于文档数的稠密分数数组，再整体排序取
top-k。这在几万篇文档上没问题，但到亿级文档时，"给每一个命中分配一个
float"本身就放不下了——**WAND 存在的全部理由，就是让你不必把命中集合
物化出来。**

### 上界剪枝：为什么能安全地跳过

BM25 的分数是**加性**的：一个文档对 `q = t₁ AND t₂ AND …` 的分数，等于
它命中的每个词项贡献之和。于是"一个词项最多能给一个文档加多少分"是可以
**预先算出来的上界**——对词项 `t`，它的贡献最大是 `idf(t)·(k1+1)`
（`tf → ∞` 时 `tf·(k1+1)/(tf + k1·norm)` 的极限）。这个数就是真实 Lance 的
`invert.lance` 里那个 **`_max_score`** 字段（17.2 提过）：**每个词项存一个
自己的最大可能贡献。**

有了每个词项的上界，就能给一个**候选文档**算一个"分数天花板"：把还没累加
的每个词项的上界全加进去。设当前已经攒了 `k` 个结果、其中最差的是第 `k` 名
分数 `θ`。如果某个候选文档的天花板 `upper` 还不到 `θ`，那它**无论后面的
词项怎么凑，都不可能挤进前 `k`——可以安全跳过，一个距离都不算。**
这就是 WAND（Weak AND）的核心，也是 `_max_score` 唯一的作用。

### 把姿势做对：按上界排序的游标

光有上界还不够，得知道**从哪个文档开始跳**。WAND 的做法是：

1. 给每个词项一条 postings 游标，游标按**文档号升序**前进（postings 本来
   就按文档号排好，见 17.2）；
2. 把游标按**各自词项的 `_max_score`** 排成一个小顶堆——**上界最小的词项
   在最前面**；
3. 逐个弹出最小的，把它的上界累加进"当前候选文档"的天花板；一旦累加值
   超过第 `k` 名的分数 `θ`，就停下来——说明这个候选**有可能**进前 `k`，
   把它真正评估一次；否则所有游标一起跳到下一个文档号。

直觉是：**先把"贡献上限最小"的词项排除掉。** 如果一个上界就很低的词项都还
没把候选的天花板顶到 `θ`，这个候选根本不值得算。

### block-max WAND：给 postings 分段，各自记上界

WAND 有个弱点：它只用**整个词项**的一个上界，而同一个词项在很多文档里
可能贡献很低（`tf=1`）。block-max WAND（BMW）把它做细：**把每个词项的
postings list 切成固定大小的块（真实 LanceDB 的默认是 128 篇一档），每块
单独算一个上界**（这一块里贡献的最大值），查询时按**块级上界**跳。这样
"开头几篇 `tf` 都很低"的块能整块跳过，剪枝比全局上界锐利得多。代价是
postings 里多一张块偏移表加每块一个 float——**"索引大一点、查询快很多"
的又一处经典取舍。** 真实 Lance 走的就是这条路：`rust/lance-index/src/scalar/inverted/wand.rs`
里既有 `BlockMaxWindow`，也有按块取上界的 `block_max_score()`，块大小是常量
`BLOCK_SIZE`——**块级 BMW 是实现了的**。

**本实现则完全没有 WAND，也就没有什么"词项级上界"可谈。** `mini_lancedb`
的 `InvertedIndex` 自己带着"刻意不做"的声明（`index/inverted.py` 的
docstring 里，WAND / BlockMax-WAND 与"delta 索引合并""跳表"并列写在
"本实现不做"那一组）。全仓库能搜到的 `max_score` 只有一个地方 ——
`search_ranked()` 写进 `last_search_stats["max_score"]` 的那个数，
它是**上一次查询**得分数组的最大值，是个查询期统计量，跟"词项在索引里
预先存一个可能贡献的上界"是两回事，**不要把它当成 WAND 的 `_max_score`**。
真实 Lance 才有那个字段（17.2 讲的 `invert.lance`）。本实现的 postings
布局（`doc_offsets` + 平坦数组）其实够放一张块表，只是没做；
`_eval()` 依然把稠密分数数组整体排序取 top-k。

出处：WAND 出自 Broder et al., CIKM 2003（*Efficient Query Evaluation using a
Two-Level Retrieval Process*）；BMW 的块级细化出自 Ding & Suel, SIGIR 2011
（*Faster Top-k Document Retrieval Using Block-Max Indexes*）。

### 它和标量侧的剪枝是同一件事

WAND 的"用上界跳过文档"，和 zone map 的"用 min/max 跳过整块"（第 18 章
§18.5）、以及倒排求交时的"跳过没有交集的区段"，本质是同一个动作：
**在动手算之前，先用一个便宜的上界证明"这里不可能有答案"。** 区别只在
上界从哪来——zone map 上界来自列的 `min`/`max`，WAND 上界来自词项的
`_max_score`。

### 建索引时真正能拧的两个旋钮

前面 17.4 讲的都是**分词器**参数；17.10 这一路讲的块和粒度，在真实
LanceDB 里是 `lancedb.index.FTS(...)` 上的**另两个索引级参数**，本章
一直没点过名。本机 0.40.0 的默认签名（`inspect.signature` 实测）：

```
FTS(with_position=False, base_tokenizer='simple', language='English',
    max_token_length=40, lower_case=True, stem=True, remove_stop_words=True,
    ascii_folding=True, ngram_min_length=3, ngram_max_length=3,
    prefix_only=False, block_size=128, custom_stop_words=None,
    memory_limit=None, num_workers=None,
    document_granularity=DocumentGranularity.ROW)
```

- **`block_size: int = 128`** —— 就是上面 BMW 里"一个压缩 postings 块装多少
  篇文档"。**默认 128**（不是 64）。docstring 写明"Supported values are
  128 and 256. A value of 256 uses the experimental FTS V3 format and may
  introduce breaking changes"——所以它**不是可以随便填的连续参数**，
  128 是稳定档、256 切到实验性的 V3 索引格式，别的值没有定义。
  **这一个数字直接把真实 Lance 的"块级上界"粒度钉死了**（就是上面 BMW
  讲的那个）：块越大，块表越省，但每块的上界越钝、剪枝越弱；块越小则
  反过来。默认取 128 就是这笔"索引大小 vs 剪枝锐度"交易的落脚点——
  而它和"换 V3 格式"绑在一起，说明连官方都没打算把它当常规调参旋钮。
- **`document_granularity: DocumentGranularity = DocumentGranularity.ROW`**
  —— 决定"什么算一篇文档"。默认 `ROW` 是"一行 = 一篇文档"；改成
  `LIST_ELEMENT` 会把**被索引字段路径上最深的那个 list 的每个元素当成
  独立的一篇文档**（`tags = ['red','blue']` 的一行会拆成两篇分别进倒排），
  并且在查询结果里多返回一个 **`_doc_index`** 列，指明命中的是这一行
  list 里的第几个元素（一行可能同时命中好几个）。用 `MatchQuery` /
  `PhraseQuery` 时也有一个同名参数可以显式指定；如果同一列两种粒度都
  建了索引，查询侧**必须**指定，否则无法推断用哪一份。

  这是"粒度是索引级、不是查询级"的直接后果：postings 里存的"文档号"
  到底是**行号**还是 **(行号, 元素下标)**，一旦写进物理布局就改不了，
  所以只能在建索引时定。本实现是一行一篇文档，没有这个参数、也没有
  `_doc_index`——`tags` 那种 list 列的成员检索在本实现里走的是 18.3 的
  LABEL_LIST，不是 FTS。

（`memory_limit` / `num_workers` 也在签名里，但它们只管**构建期**的
内存与并行，不进索引元数据、不影响查询语义，不属于这一节的两类旋钮。）

## 17.11 小结

**向量模型不擅长字面。** 型号、错误码、SKU、注释、代码符号，这些"没有语义、
只有字面"的东西必须靠倒排索引。倒排的全部秘密是"词项 → 文档"的一次字典查找。

**位置存的是词项在基础词项流里的序号。** 它唯一的用途是短语查询
（位置差 = 偏移差）和邻近度打分。它很便宜（差分 + varint ≈ 1 字节/位置），
但**构建期的临时空间**很贵，真实 Lance 文档说开了位置"很容易需要原列的
10 倍以上"。

**BM25 的三件事：TF 饱和（`k1` 控制，默认 1.2）、长度归一化（`b` 控制，
默认 0.75）、恒正 IDF（`ln(1 + ...)` 是 Lucene 4.0 的修正）。**
分数是**加性**的，所以可以逐词项拆开解释。

**分词器和索引是一个整体。** 查询侧和索引侧必须用同一套配置，
所以配置要写进索引元数据。本实现的默认值和真实 LanceDB 有两处不同
（`ascii_folding` 默认关、`with_position` 默认开）；`custom_stop_words`
的"取代"语义两边则**一致**——网上流传的"额外追加"说法与实测不符。
用之前都先在手上版本实测一遍。

**Porter 是规则型词干化的祖师爷**，五个步骤、纯靠英文后缀的观察。
它过度词干化（`generalizations → gener`）、不处理不规则动词
（`went`/`gone` 永远对不上）、且**不幂等**（但收敛）。

**CJK 是真实的岔路。** 真实 LanceDB 默认的 `simple` tokenizer 把整个中文段
当**一个词项**，中文场景必须显式用 `ngram` / `icu` / `jieba`；
教学实现退化成字符二元组，效果远好于按字切，但**精确匹配多字词必须用
短语查询**。

**`_score` 越大越好，`_distance` 越小越好。** 这是混合检索里最容易
出错的地方，也是 LanceDB 分别给两个列名的原因。另一个同源的坑是
**跨分区 / 跨索引的分数量纲不同**：每个 partition 只持有本地 `df`/`avgdl`，
分数不可直接比，要么合成全局统计、要么只用排名融合（17.2）。

**WAND 是"不必物化命中集合"的算法。** 它给每个词项存一个
`_max_score`（`idf·(k1+1)`），按上界从小到大推游标；候选文档的分数天花板
一旦够不到第 `k` 名，就整段跳过。block-max WAND 再把它做细到"每个 postings
块一个上界"（真实 Lance 的块大小就是 `FTS(block_size=128)` 的默认 128）。
它和 zone map 是同一个思想：**用一个便宜的上界，先证明"这里不可能有答案"。**
本实现**没有实现 WAND**，`_max_score` 也不存在 —— 只有真实 Lance 有它。

下一章换了完全不同的目标：向量索引允许漏，标量索引**不允许有误差**。
这个差别决定了标量索引的接口不是"返回最近的前 k 个"，而是
"返回一个行号集合的表达式"。
