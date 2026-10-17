# 第 22 章　嵌入函数

前面二十章讲的都是"向量已经有了之后怎么办"。这一章补上前面那一步：
**向量是怎么来的**，以及 LanceDB 在这件事上做了哪些约定。

这一章有一个必须提前说清楚的现实：**嵌入函数是 LanceDB 里变动最频繁、
文档与实现最容易对不上的地方。** 截至 2026-10，在 docs.lancedb.com 的
API 参考索引里检索不到 `embedding_functions=` 这个参数；
但它在第三方教程里到处都是，而且在 `lancedb` 0.40.0 的
`create_table` 签名里**依然存在**（附录 A 的 `create_table` 条目里
就列着它，且不带弃用标记）。

所以这一章要把三条线分开写，别混在一起。

**官方的当前推荐路径**是 schema 级的声明——用
`func.SourceField()` / `func.VectorField()` 说明"哪一列的向量由哪一列
算出来"，见 22.6 节。

**真实库**在 0.40.0 上仍然接受参数式入口 `embedding_functions=[...]`，
只是文档不再主推它。

**本教程的迷你实现**两条路都支持，把 schema 声明当推荐路径、把参数式
入口当兼容路径（`mini_lancedb/connection.py` 里的
`EmbeddingFunctionRegistration` 就是这个"视图"）。

## 22.1 嵌入函数到底解决什么问题

一个向量检索系统里，嵌入模型的调用点有三处：写入时把文档转成向量、
查询时把查询串转成向量、以及"表里存的向量是哪个模型产生的"这个元信息。

第三件事最容易被忽略，但它决定了系统能不能长期维护。如果没人记得
那张表里的向量是 `text-embedding-3-small` 还是 `bge-small-zh-v1.5` 生成的，
那么换模型的那一刻就会**静默地毁掉检索质量**——新旧向量不在同一个语义空间里，
距离计算毫无意义，而且不会报任何错。

所以 LanceDB 的做法是：**把嵌入函数的标识写进表的 schema metadata。**
打开表的时候先读这个标识，就知道该用哪个模型去生成查询向量。
这个机制在真实 LanceDB 里叫 `EmbeddingFunctionRegistry`，
本教程的迷你实现里也复刻了它：

```python
from mini_lancedb.embedding import get_registry

registry = get_registry()
registry.names()
# ['bow', 'char-ngram', 'cohere', 'fake', 'gemini', 'hashing',
#  'huggingface', 'jina', 'ollama', 'openai', 'sentence-transformers']
```

注册表里的每个函数都有一个稳定的 **source 字符串**，它就是写进
schema metadata 的那个键。**这个字符串一旦发布就永远不能改**——
改了等于"老表里的向量突然没人认得"。

## 22.2 嵌入函数的接口：形状、工厂与输入规整

### 接口形状

真实 LanceDB 的嵌入函数是一组类，每个类回答三个问题：
维度是多少、怎么算文档向量、怎么算查询向量。
最后一条经常和第一条不同——很多检索模型（比如 BGE 系列）
给查询和文档设计了两套不同的前缀，混用会掉点。

迷你实现里保留了这套形状：

```python
class EmbeddingFunction:
    def ndims(self) -> int: ...
    def compute_source_vectors(self, texts: list[str]) -> list[list[float]]: ...
    def compute_query_vectors(self, queries: list[str]) -> list[list[float]]: ...
```

拆成两个方法还有一个实际好处：查询向量经常需要缓存
（同一个查询串被反复搜），而文档向量只需要算一次。

### 工厂：`create()` 而不是直接 `Cls(...)`

光有一个类不够，还需要一个**从配置重建实例**的入口——因为"打开表"
拿到的是一份 JSON，不是一活着的对象。迷你实现和真实 LanceDB 都用
`create(**kwargs)` 这个类方法来承担它（`mini_lancedb/embedding/base.py`）：

```python
@classmethod
def create(cls, **kwargs):
    for key in cls.sensitive_keys():          # 敏感参数必须走 $var:
        v = kwargs.get(key)
        if isinstance(v, str) and v and not v.startswith("$var:"):
            raise QueryError(f"敏感参数 {key!r} 不允许硬编码", hint=...)
    inst = cls(**resolve_variables(kwargs))   # 解析 $var: 占位符
    inst._original_args = dict(kwargs)        # 记下**原样**传进来的参数
    return inst
```

两条设计都在里面。第一条是**把密钥挡在代码之外**：
`api_key="sk-..."` 这种写法直接抛错（`sensitive_keys()` 默认返回 `[]`，
provider 里只有 `OpenAIEmbeddings` / `CohereEmbeddings` / `JinaEmbeddings`
/ `GeminiEmbeddings` 这四个把它写成 `["api_key"]`），必须写
`$var:OPENAI_API_KEY`、运行时再注入。第二条更细——它记下来的是
**用户原样传进来的那份参数**，不是解析后的值，所以
`safe_model_dump()` 写进表 metadata 的会是 `"$var:OPENAI_API_KEY"`
这个占位符，而不是明文密钥。这套机制值得单独一节，见 §22.7。

### `sanitize_input()`：一个高频的静默 bug

接口收的是"一批文本"，但调用方手上常常是**单个字符串**。
如果实现里直接 `for t in texts`，那么 `compute("hello")` 会得到
`["h", "e", "l", "l", "o"]`——**5 个字符各自的向量，而不是 1 个
"hello" 的向量**，而且不报错、不警告。这个 bug 在 embedding 库里出现
的频率高得离谱，所以迷你实现把"规整"单独做成一个方法：

```
>>> HashingEmbeddingFunction(dim=64).sanitize_input("hello")
['hello']                                  # 单个字符串 -> 恰好一条
>>> HashingEmbeddingFunction(dim=64).sanitize_input(["a", "b"])
['a', 'b']                                 # 列表原样
>>> HashingEmbeddingFunction(dim=64).sanitize_input(123)
QueryError: 无法把输入规整成文本列表 (got='int', hint='支持 str / list[str] / np.ndarray / pa.Array')
```

注意它对**列表元素**统一 `str(...)`，`np.ndarray` / `pa.Array` 先
`to_pylist()` 再转字符串。这是"宁可放宽、不要静默错"的取舍：
把数字转成 `"123"` 至少是确定的，把字符串拆成字符却是灾难。

### `embedding_matrix()`：维度对不上时唯一会报错的地方

`ndims()` 写错（类里写 64、实际产出 128 维）在别处基本都是静默的，
堆成矩阵的时候才会被抓：

```
>>> class Bad(HashingEmbeddingFunction):
...     def ndims(self): return 99
>>> Bad(dim=64).embedding_matrix(["hello"])
QueryError: 向量宽度和 ndims() 不一致 (expected=99, function='Bad', got=64)
```

`embedding_matrix()` 做的是"编码 → 逐条比对宽度 → 堆成 `(n, ndims)`"，
宽度不符就抛。**这就是 `ndims()` 写错唯一会被当场抓住的地方**
（另一处是 `VectorQuery.vectors()` 对**查询**向量做的同一种检查）。
第 25 章 §25.1.3 把它列为嵌入这一步"不能省"的校验，理由就在这里。

## 22.3 离线可用的嵌入函数：为什么必须有

一份教程如果所有例子都依赖 API key，那它就没有可复现性。
迷你实现里带了四个**完全离线、确定性**的嵌入函数，
它们不是"假装有语义"的随机数，而是真的能给出有用的相似度：

**`hashing`（哈希技巧）。** 把词哈希到固定维度的桶里，
做次线性 TF 加权再 L2 归一化。这是"没有模型时"的标准退路：
它抓不住同义词，但能抓住词形重叠，在专有名词、型号、代码符号上
反而比神经模型更可靠。

实测它在"相关 vs 不相关"的对比上有明确的区分度。用
`tests/test_embedding.py` 里那 8 组夹具（查询 / 词面重叠 / 对抗性不相关）
跑一遍，`examples/verify/ch22_embedding_functions.py` 给出：

```
夹具: tests/test_embedding.py::RELATED_PAIRS（8 组）

函数              维数           相关 [min, max]            不相关 [min, max]        最小间隔
hashing        128        [0.4330, 0.7385]          [0.0000, 0.2835]      0.3658
char-ngram     256        [0.4523, 0.7031]         [-0.1291, 0.3120]      0.2644
fake           128       [-0.1163, 0.1269]         [-0.1193, 0.1905]     -0.1978
bow              9        [0.0000, 1.0000]          [0.0000, 0.5774]      0.0000
```

**这一段我改过一次，记下来免得以后又抄错。** 原来这里写的是
"相关落在 0.5774 到 0.6860、不相关落在 0.2041 到 0.2835、间隔
0.3732 到 0.4082，四组"。这几个数字**确实是从真实输出里抄的，
但抄的是子集**：0.5774 是第 3 组（`python programming language`）
那一行的相关值，0.6860 是第 6 组（`检索 增强 生成`）那一行，
它们既不是最小值也不是最大值；"四组"则把 8 组记成了 4 组。
**相似度是确定性的量，所以这不是"抖动"，是真错**——同机重跑
逐位相同，范围就该是 `[0.4330, 0.7385]`。

那 8 组里的"不相关"文档是**刻意挑的对抗样本**——它们和查询共享
功能词（`models`、`index`、`language`），所以间隔不是"非零 vs 零"
那种廉价结果。注意 `hashing` 的不相关最小值是 **0.0000**，
落在第 5 组和第 7、8 组上，意味着那些对抗样本没共享任何词面。

`fake`（种子高斯）那一行是这份表里最有价值的**反例**：它的相关和不相关
区间几乎完全重叠，最小间隔是**负的**（−0.1978）。它就是
"随机向量做检索"的定量反面教材——区间宽度和 `hashing` 差不多，
但**区间位置**说明它没有任何区分度。（这里原来写的是"第 22.1 节说的"
——**那个引用是错的**，§22.1 讲的是嵌入函数的身份与注册表，
没讲过随机向量；这条结论只在本节有。）

`bow` 的最小间隔是 0.0000，而且它**不是因为"好"**。这一行的成因
比看上去绕，值得拆开：整个夹具里只有第 1 组产出了非零向量，
其余 7 组两边全是零向量。**但"零向量"的原因不是"词表里没有那个词"**——
第 7 组的相关文档是 `神经网络 训练 需要 大量 标注 数据`，里面明明有
`训练` 和 `数据` 两个词表词，可它仍然是零向量。真正的成因是
**分词器把 CJK 逐字切开**：`tokenize("训练 数据")` 得到的是
`['训', '练', '数', '据']`，而词表里存的是 `训练` / `数据`
这两个**双字词**——两边永远对不上。也就是说，
`tests/test_embedding.py` 里 `VOCAB` 的那几个中文词项在这份夹具上
**全是死条目**，一个都命中不了。（夹具注释里写 `RICH_TEXT`
"包含全部词表词"，对中文那半句同样不成立。）
**"零向量"和"没这个词"是两个原因、两种修法**，这一行正好是前者。

**`char-ngram`（字符 3-gram 哈希）。** 设计上它是"中文/日文的首选兜底"，
因为它根本不需要分词："机器学习"的 3-gram 是
`<机`、`机器`、`器学`、`学习`、`习>`，而"机器学习算法"共享前四个。

**但在同一份夹具上实测下来，它的整体区分度不如 hashing——而且
不是"每一对都更差"那种整齐的弱。** 同一张表里的两行：

```
hashing     相关 [0.4330, 0.7385]     不相关 [0.0000, 0.2835]     最小间隔 0.3658
char-ngram  相关 [0.4523, 0.7031]     不相关 [-0.1291, 0.3120]     最小间隔 0.2644
```

三个值得注意的地方：

**它的相关区间上界更低、下界更高**——`[0.4523, 0.7031]` 比
`hashing` 的 `[0.4330, 0.7385]` 更窄，弹性更小。

**它的不相关区间会下探到负数**（−0.1291，来自第 6 组"检索 增强 生成"）。
这不影响排序（每一组的"相关 − 不相关"仍然是正的，最小 0.2644），
所以它不会让排名反过来。但它说明**"不相关 ⇒ 相似度低"这个直觉
在 char-ngram 上不成立**：一个完全无关的文档可以拿到负分。

值得把这一条和它的反面分开记——我第一版在这里写的是反面：
"如果代码里写了 `if sim > 0: 认为相关`，就会漏掉一条真正相关的"。
**那是错的**：这份夹具里 8 组的**相关**相似度最小也有 +0.4523，
全部为正，`sim > 0` 一条都不会漏。真正的风险在另一侧——
**不相关的对里有一组是正的（+0.3120，第 3 组）**，
所以 `if sim > 0: 认为相关` 会把一个无关文档判成相关。
阈值型判断在这类打分上的失效方向是"**误收**"而不是"漏掉"，
写阈值的时候要按这个方向去防。

**最小间隔 0.2644 对 0.3658，差了约 28%**，这才是"弱"的准确度量。
我原来在这里写过"三组全部更低"，那是不对的：逐对看，
第 1 组（`machine learning models`）char-ngram 是 **+0.7031**，
比 hashing 的 **+0.6124** 更高。**平均更好、最差的那一对更差**，
和"全面更差"是两回事——压缩成一个形容词就把这条信息丢了。

原因是 `HashingEmbeddingFunction` 在分词时**已经对 CJK 逐字（unigram）切开**，
它早就吃到了"不需要分词"这个红利，而 char-ngram 还要额外付出
向量更稠密、语义粒度更粗的代价。（走字符二元组的是另一条线——
全文检索的 tokenizer，和这里的嵌入分词不是同一件事。）

这个反直觉的结果值得记住：**"哪个兜底嵌入更好"取决于你的分词器，
不能只看名字。** 换成对中文一窍不通的空白符分词器，char-ngram 会立刻反超。
这类"理论上更好、实测更差"的结论只能靠量，不能靠推。

**`bow`（精确词袋）。** 在一个固定词表上给出精确的词频向量。
它的价值在于**完全可手算**——写测试的时候，
你可以把期望的相似度在纸上算出来，和代码对照。

**`fake`（定种子高斯）。** 用来做性能测试。
它的向量之间**没有任何语义结构**，所以用它评判召回率是没有意义的。
我们在文档里明确写了这一点，因为"用随机向量测召回率然后得出
'这个索引召回率很低'的结论"是一个真实出现过的误判。

## 22.4 在线模型：接口留好，调用留给你

真实世界里常用的是 OpenAI、Cohere、Jina、Sentence-Transformers、
HuggingFace、Ollama、Gemini 这些；VoyageAI 也是真实世界的常客，
但迷你实现里**没有**它的类。对前面那七个，迷你实现提供了**只写接口、
不做调用**的类（`mini_lancedb/embedding/providers.py` 的 `PROVIDERS`
正好是这七个）：

```python
from mini_lancedb.embedding.providers import describe_providers
describe_providers()
# [{'name': 'openai', 'package': 'openai', 'typical_model': 'text-embedding-3-small',
#   'dims': 1536, 'needs_api_key': True, 'runs_locally': False, ...},
#  {'name': 'sentence-transformers', 'package': 'sentence-transformers',
#   'typical_model': 'all-MiniLM-L6-v2', 'dims': 384,
#   'needs_api_key': False, 'runs_locally': True, ...}, ...]
```

每个 provider 都实现了完整接口、写清了真实模型名和维度，
但被真正调用时会抛 `NotSupportedError` 并给出安装提示。
这样做的理由很实际：**一份教程不应该在读者机器上偷偷发网络请求，
更不应该因为没有 key 就整个跑不起来。**

**但这七个和真实注册表不是一回事，数量别往一起凑。** 0.40.0 的
`lancedb.embeddings.get_registry()` 实测注册了 **17 个**名字，按字母序是：

```
bedrock-text   cohere        colbert       colpali      gemini-text
gte-text       huggingface   imagebind     instructor   jina
ollama         open-clip     openai        sentence-transformers
siglip         voyageai      watsonx
```

按**模态**把这 17 个分三堆：

- **多模态**（能吃图，部分能吃音视频）：`open-clip`、`siglip`、`jina`、
  `imagebind`、`colpali`——正好就是第 21 章 §21.3 逐个讲过的那五个
  （ColPali 还同时是多向量，接 §21.1）；
- **文本多向量**：`colbert`（输出的是 token 矩阵而不是一个向量，
  就是 §21.1 那套 late interaction 现成的注册表实现）；
- **纯文本**：其余 11 个——`openai`、`cohere`、`gemini-text`、
  `sentence-transformers`、`huggingface`、`ollama`、`voyageai`、
  `watsonx`、`instructor`、`gte-text`、`bedrock-text`。

对照着看两边的差：迷你实现的七个桩是"**常见在线 provider 的接口形状**"，
不是"真实注册表的全部"。真实那边多出来的，一是多模态的那几个
（第 21 章要用到），二是 `voyageai` / `instructor` / `bedrock-text` /
`gte-text` / `watsonx` 这几个纯文本 provider（顺带一提，`gte-text`
的 docstring 第一行写着 "Deprecated: GTE embeddings should be used
through sentence-transformers"——注册表里有名字，不等于推荐用它）。

选模型的几条实用准则：

**维度不是越高越好。** 1536 维和 384 维在检索质量上的差距，
往往小于"换一个更适合中文的模型"带来的差距。
而维度直接决定内存和索引大小——第 14 章 §14.1 那张内存表算过
（1 亿 × 768 维是 307 GB、10 亿 × 768 维是 3072 GB），
10 亿 × 1536 维正好是它的两倍，即 6 TB。

**一定要看模型是不是"检索专用"。** 把生成式模型的
embedding 输出拿来做检索，效果通常明显差于专门的
双塔检索模型。这是最常见的一个坑。

**中文一定要用中文模型。** `text-embedding-3-small` 在中文上能用，
但 `BAAI/bge-small-zh-v1.5` 这类中文专用模型通常更好，
而且后者可以本地跑（不花钱、不联网、没有延迟）。

**维度截断（Matryoshka）。** 部分新模型支持"只取前 N 维"，
前 512 维就能保留大部分信息。这在成本敏感的场合很有用，
但要注意：**截断后的向量和原模型的其他维度不兼容**，
混用会出错。

**各家的上下文 / 词元上限不一样，而且都远小于生成式模型的窗口。**
按厂商文档的口径（截至 2026，具体值以各家文档为准）：
OpenAI 的 `text-embedding-3-*` 单条上限约 **8191 token**，
Cohere 的 `embed-v3` 是 **512**，Jina 的 v3 是 **8192**，
`BAAI/bge-small-zh-v1.5` 是 **512**。要点有两个：
一是**嵌入接口的长度上限常常小于"模型号称的上下文长度"**；
二是**同一个"512 token 的块"在不同模型里的字符数可以差两三倍**
（第 25 章 §25.1.2 讲切分时算过这笔账）。所以切分器用的 token 计数
必须和你**实际要调用的那个模型**对齐，不能拿一个通用估算糊过去。

**嵌入调用要能批量，也要能异步。** 接口层最好同时准备三种形态：
`compute_source_vectors(texts)` 天然是批量的；
远端 API 还需要**异步**（asyncio）以便并发发请求而不阻塞；
两者合起来就是"异步批量"——按 token 总数动态攒批、并发发若干批、
每批自带超时与重试（第 25 章 §25.1.3 给了重试和跨进程缓存的骨架）。
本教程的 provider 桩只固定了同步的 `compute_source_vectors` /
`compute_query_vectors` 两个方法，异步形态留给接入方按自己的
并发模型补。

**换模型不用重新入库。** 这是 LanceDB 相对传统向量库的一个结构性
优势：`add_columns` 加一列新向量、回填、把查询切到新列，
原始数据一个字节都不用重写。完整的迁移路径——包括"从 `main` fork 出
两个分支各挂一个模型、在固定评测集上比、只把赢的合并回来"——见
第 26 章 §26.7。

**多模态嵌入是"多向量列"，不是"更大的一列"。** CLIP / ColPali
这类模型的输入是图（或图 + 文本），向量语义和纯文本模型不在一处，
所以它们的注册要落到**独立的向量列**上：同一张表可以有
`image_vector` 和 `text_vector` 两列，各由不同模型生成。
多向量列在查询时怎么合（ColBERT 式后期交互、还是加权求和）见
第 21 章；把新模态接进 registry 的写法见第 26 章。

## 22.5 自动向量化：一个静默的陷阱

真实 LanceDB 支持"声明式"的自动向量化：在 schema 里声明"这一列由
哪个模型生成"，写入时只给文本、向量由库自己算。它的触发条件是
**整批级**的——向量列不存在、或者整批全为 null 才算；只要有一行
提供了非空向量，整批就被当成"手工数据"，**不回填、不报错、也不提示**。

这条语义踩下去之后的现场、官方原文，以及我们自己对它的实测
（"部分提供"和"整批全 null"在 pydantic 那一层就直接报
`has variable length vectors` / `Invalid null value`），都在
第 25 章 §25.3。结论一句话：**不要依赖自动向量化**——在应用层
显式算好向量再写进去，多写三行代码，换的是"向量何时、由哪个模型
算的"这件事完全可控。

## 22.6 一个完整例子

把嵌入、写入、检索串起来，并且**把"这些向量是谁生成的"记进表里**。
这段代码在 `examples/05_embedding_search.py` 里，不依赖任何网络。

```python
import numpy as np
import mini_lancedb as ldb
from mini_lancedb.embedding import HashingEmbeddingFunction
from mini_lancedb.embedding.base import EmbeddingFunctionConfig

embedder = HashingEmbeddingFunction(dim=64)
docs = [
    "Lance 文件格式用 40 字节的 footer 定位全部元数据",
    "IVF_PQ 把向量切成子空间分别量化，用查表算距离",
    "删除不重写数据文件，只写一个记录行号的删除文件",
]
vectors = embedder.compute_source_vectors(docs)

db = ldb.connect("./demo_db")
table = db.create_table(
    "notes",
    data=[
        {"id": i, "text": t, "vector": np.asarray(v, dtype="float32").tolist()}
        for i, (t, v) in enumerate(zip(docs, vectors))
    ],
    embedding_functions=[
        EmbeddingFunctionConfig(
            source_column="text", vector_column="vector", function=embedder
        )
    ],
)
```

打开表之后，schema 的元数据里就带着这条信息：

```
metadata keys: ['embedding_functions']
[{"source_column": "text", "vector_column": "vector",
  "name": "hashing", "ndims": 64}]
```

关掉进程、重新打开这张表，这条元数据还在——这是重点。
**把产生向量的函数名和维度写进表的元数据**，半年后有人接手时，
不用猜也知道该用哪个模型重算查询向量。

**但这个"知道"在本实现里只到"注册名 + 维度"这一层，构造参数是丢的。**
`mini_lancedb/connection.py` 的 `_embedding_metadata()` 落盘时写的就是
上面那四个键——**没有 `model` 这一项**；而重开表时
`mini_lancedb/table.py` 走的是 `embedding/base.py` 的 `parse_functions()`：

```python
function=cls.create(**entry.get("model") or {})   # 没有 "model" 键就用空 dict
```

于是**恢复出来的是"注册名对应的默认构造函数"**，不是当初那个实例。
端到端实测一遍：`HashingEmbeddingFunction(dim=64)` 建表
（metadata 里明明写着 `"ndims": 64`）→ 关连接 → 重新 `open_table`
→ 按 metadata 恢复：

```
建表时 ndims(): 64
恢复出来 ndims(): 128        ← HashingEmbeddingFunction 的默认 dim 是 128
```

**存进去是 64，恢复出来是 128。** 后果在**查询那一刻**才炸，而且报的
是维度校验：

```
>>> reopened = db.open_table("notes")      # 重新打开
>>> reopened.search("量化").limit(2)
QueryError: 查询向量维度与表不符 (column='vector', expected=64, got=128)
```

`expected=64` 是表里那一列的宽度（这一段的数据写进去是
`fixed_size_list<float64>[64]`），`got=128` 是恢复出来的默认维度。
**这一次是报错、不是静默错**，但报出来的位置离病根很远——metadata 里
明明写着 `"ndims": 64`，而恢复路径根本不看这个键。所以"表知道自己用了
哪个模型"这句话，在本实现里的准确版本是：**表知道模型的注册名和维度，
不知道它的构造参数**。真实 LanceDB 的 metadata 里带 `model`
（构造参数字典，形状见 `embedding/base.py` 模块开头那段注释，例如
`"model": {"name": "all-MiniLM-L6-v2"}`），所以它能真的重建出等价
编码器——**这也是本实现和真实库在这一步上唯一的实质差别，别把它当成
"两种实现都一样"**。

**所以打开一张别人建的表之后，要自己断言一次。** 这段检查值得抄进
任何"表不是自己建的"启动路径：

```python
import json, mini_lancedb as ldb
from mini_lancedb.embedding import get_registry

db = ldb.connect("./demo_db")
table = db.open_table("notes")
entry = json.loads(table._ds.schema.metadata["embedding_functions"])[0]
# 这一步就是 parse_functions() 干的事
fn = get_registry().get(entry["name"]).create(**(entry.get("model") or {}))
assert fn.ndims() == entry["ndims"], (
    f"恢复出 {fn.ndims()} 维，metadata 里记的是 {entry['ndims']} 维 —— "
    "构造参数没有随 metadata 落盘"
)
```

**真实 LanceDB 里等价的检查是对着 schema 里向量列的宽度做**——
`func.ndims()` 必须等于建表时声明的那一列宽度，也就是 §25.1.3 那句
"写小了会截断、写大了会补 0，两种都是静默的数据损坏"要防的东西。
至于**编码阶段**的 `ndims()` 写错，本实现里唯一会当场报错的地方是
§22.2 的 `embedding_matrix()`。

注意上面写成 `embedding_functions=[...]` 是**本教程实现的兼容入口，
不是推荐路径**。真实库的当前推荐做法是在 schema 里用
`func.SourceField()` / `func.VectorField()` 声明"哪一列的向量由哪一列
算出来"。这个声明在真实库里长这样（`lancedb` 0.40.0，已核对签名）：

```python
from lancedb.embeddings import get_registry
from lancedb.pydantic import LanceModel, Vector

func = get_registry().get("sentence-transformers").create()

class Doc(LanceModel):
    text: str = func.SourceField()                     # 源列
    vector: Vector(func.ndims()) = func.VectorField()  # 向量列

table = db.create_table("docs", schema=Doc)
```

（注意 `func.ndims()` 在真实库里会**真的加载模型**来量维数，所以这段
代码要求装上对应 SDK；没有 SDK 时 `create()` 之后的 `ndims()` 会直接
报错——这正是本教程把远端/本地模型做成"接口桩"的原因。）

迷你实现里的等价形状是把 `mldb.func.SourceField()` /
`mldb.func.VectorField()` 当作字段标记放进 schema，配对规则写在
`mini_lancedb/connection.py` 的 `EmbeddingFunctionRegistration` 里。
两条路殊途同归：**`SourceField` / `VectorField` 是声明，
`EmbeddingFunctionConfig` 是落盘后的形态，前者能推导出后者。**
迷你实现把"能被推导多少"写得很保守——同一个 schema 里出现多于
一个 source 列时直接放弃配对，因为"猜错源列"的后果是拿错文本算向量，
而向量错是**静默错**（查得到、就是不准），比报错难查得多。

## 22.7 敏感参数与 `$var:` 密钥机制

§22.4 讲 provider 时留了一个尾巴没讲：那几个要 API key 的 provider
（`mini_lancedb/embedding/providers.py` 里的 `OpenAIEmbeddings` /
`CohereEmbeddings` / `JinaEmbeddings` / `GeminiEmbeddings`）**照直写
参数是跑不起来的**——拦下你的不是 SDK 没装，是一套强制的密钥机制。
这一节把它讲完，因为**照着 §22.4 写 `api_key="sk-..."` 会立刻撞报错**。

### 明文密钥由接口拦下，不靠自觉

这四个类的 `sensitive_keys()` 都返回 `["api_key"]`，而 `create()` 在
构造之前先扫一遍这些键：**值是字符串、非空、又不以 `$var:` 开头，
就直接抛错**。实测：

```
>>> OpenAIEmbeddings.create(name="text-embedding-3-small", api_key="sk-hardcoded")
QueryError: 敏感参数 'api_key' 不允许硬编码
            (hint="改成 '$var:API_KEY'，再用 get_registry().set_var('API_KEY', ...) 在运行时注入")
```

（hint 里那个 `API_KEY` 是 `参数名.upper()` 拼出来的示意，不代表变量名
必须等于参数名；按 `$var:OPENAI_API_KEY` 这样的约定命名更清楚。）

正确的写法是两步：**代码 / notebook / 配置文件里只写占位符，真值在
运行时注入。**

```python
import os
from mini_lancedb.embedding import get_registry
from mini_lancedb.embedding.providers import OpenAIEmbeddings

get_registry().set_var("OPENAI_API_KEY", os.environ["OPENAI_API_KEY"])
fn = OpenAIEmbeddings.create(name="text-embedding-3-small",
                             api_key="$var:OPENAI_API_KEY")
fn.api_key            # 'sk-...' —— 实例上拿到的是解析后的真值
```

`$var:NAME:default` 这种三段的写法还支持**默认值**（变量没设就用
default），适合"不影响正确性"的配置，比如
`SentenceTransformerEmbeddings.create(device="$var:DEVICE:cpu")` 在没设
`DEVICE` 时拿到 `device="cpu"`。变量没设、又没给默认值就抛：

```
QueryError: 变量 'NOT_SET_ANYWHERE' 未设置
            (hint='用 get_registry().set_var(name, value) 设置；变量不跨进程保留', key='api_key')
```

### 安全收益：进文件的是占位符，不是密钥

这套机制真正值钱的地方在**序列化**那一步。`create()` 把**用户原样传
进来的参数**记在 `_original_args` 上，而 `safe_model_dump()` 返回的是
那一份、不是解析后的值：

```python
>>> fn.safe_model_dump()
{'name': 'text-embedding-3-small', 'api_key': '$var:OPENAI_API_KEY'}
```

于是注册表那条序列化路径（`EmbeddingFunctionConfig.to_dict()` →
`EmbeddingFunctionRegistry.get_table_metadata()`）产出的 metadata 是：

```
[{"name": "openai",
  "model": {"name": "text-embedding-3-small", "api_key": "$var:OPENAI_API_KEY"},
  "source_column": "text", "vector_column": "vector"}]
```

**明文密钥不在里面**——实测按字符串搜过 `sk-live-do-not-commit`，搜不到；
进去的是占位符。这个区别很大：表 metadata 会跟着 manifest 进对象存储、
进备份、进 `to_arrow()` 的导出、也进 git（如果你把表目录提交了）。
把密钥挡在这一步之外，等于把泄密面从"整条数据管道"缩回"运行时的
环境变量"。

一处本实现的边界要说清楚：上面这段是**注册表那条路径**的输出
（`get_table_metadata()`）。真正建表时 `mini_lancedb/connection.py` 的
`_embedding_metadata()` 写的是**另一种更短的记录**——
`{"source_column", "vector_column", "name", "ndims"}`，连 `model` 都没有。
所以本实现里**占位符确实不会被解析成明文写进文件，但构造参数连同占位符
一起丢了**（就是 §22.6 讲的那个缺陷）。真实 LanceDB 写的是带 `model`
的完整形状——那也正是 `$var:` 的收益能真正落到磁盘上的原因。

还有一条配套的规矩：**变量不跨进程保留。** `set_var()` 只写进当前进程
那份注册表，进程一换就没了。所以从 metadata 恢复一个带
`$var:OPENAI_API_KEY` 的 provider 时，只要这个进程没设过该变量，抛的
就是上面那条"变量未设置"——这是设计如此，不是 bug：密钥必须由每个
进程自己注入（环境变量、密钥管理服务、启动时取一次）。第 25 章
§25.5.3 讲"EmbeddingFunction 不跨进程"时说的也是这件事的另一半。

## 22.8 小结

**嵌入函数的标识必须写进表的 schema metadata**，
否则换模型会静默毁掉检索质量。

**接口要区分"算文档向量"和"算查询向量"**，
很多检索模型对两者的前缀要求不同。

**离线可用的嵌入函数不是玩具。** 哈希技巧和字符 n-gram
在没有模型时是能用的退路，而且它们让整份教程可复现。

**自动向量化有条件触发**：只有向量列缺失或整列为 NULL 时才算，
部分提供会被静默当成手工数据。别依赖它。

**选模型看三件事**：是不是检索专用、语言匹配不匹配、维度带来的成本。

下一章讲这些向量写进表**之后**怎么维护——小片段、幽灵行、失效索引
与版本膨胀这四种债，以及 `optimize()` 把它们还清的方式。
