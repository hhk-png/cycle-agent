# 第 25 章　生产实践

前面几章讲的都是"能不能做"。这一章讲"做起来会在哪里疼"。

一个检索原型和一套生产系统之间的差距，通常不体现在算法上，
而体现在这样一些地方：你写进去的第 4 批数据里混了一行脏的，
第 10001 毫秒时合并插入突然报错，两个 worker 同时压实把对方
的提交打回，某个进程 `import lancedb` 直接崩掉，
以及三个月后磁盘上躺着 800 个版本没人敢删。

这一章按一条完整的 RAG 数据管道走一遍，每一站说清楚三件事：
这一步该怎么做、这一步**最常踩的坑是什么**、以及怎么用指标
看出它已经出问题了。

## 25.1 一条完整的 RAG 数据管道

管道是七步。下面每一节给出一个可以跑的骨架，并且标出哪些地方
是"这一步真正的难点"。

### 25.1.1 采集

采集这一步在 LanceDB 语境下的特殊之处是：**它决定了你的主键是
什么。** 后面所有步骤——去重、增量更新、合并插入——都建立在这
一个决定上。

选择主键的规则只有一条：**它必须是内容自身的一个稳定函数，
不能依赖"第几次采集"或"采集到的顺序"。** 常见的正确选择是
源文档的稳定 ID（数据库主键、URL 规范化之后的哈希、
文件的 SHA-256）。常见错误选择是自增序号、采集时间戳、
列表下标——这些东西在"重新采一遍"时会全部错位，导致增量更新
变成全量重复插入。

```python
import hashlib, json

def stable_id(source: str, raw: bytes) -> str:
    """内容寻址的稳定 ID：源标识 + 内容哈希。

    两个好处：同一个源的两份不同内容拿到不同 ID（能检测到更新），
    同一个内容无论采多少次都是同一个 ID（天然去重）。
    """
    return hashlib.sha256(f"{source}\x1f".encode("utf-8") + raw).hexdigest()
```

如果源是一个会改动的文档（比如 wiki 页面），ID 应该是
"源 URL 的规范化形式"而不是"内容的哈希"——否则每次改一个错别字
都会变成一条新记录，而你想表达的其实是"这一条更新了"。
这两种语义对应 `merge_insert` 的两种用法，25.2 节展开。

### 25.1.2 切分：策略与重叠

切分（chunking）是整条管道里**最影响检索质量**的一步，
也是最容易被敷衍的一步。

三个参数要有意识地选，不要用默认值蒙。

**块大小。** 它同时受两件事约束：embedding 模型的有效上下文
（超过之后尾部信息被稀释甚至截断），和"回答一个问题需要多少
上下文"。实践中 256 到 512 token 是一个常见区间。太小的问题是
**指代丢失**（"它"、"上面说的那个"失去了先行词）；太大的问题是
**向量被稀释**（一段 2000 字的文本里只有 50 字相关，那 50 字
在向量里的权重是 2.5%）。

**重叠。** 相邻块之间留一段重叠，是为了防止"关键句子正好被切在
边界上"。经验值是块大小的 10%–20%。重叠的代价是存储和嵌入
算力按比例增加——20% 重叠意味着多 20% 的 token 要编码。

**切分边界。** 按固定 token 数硬切是最差的做法。优先级从高到低
是：文档结构（标题、列表项、表格行）> 段落 > 句子 > 固定长度。
结构化切分的好处是**每一块在语义上自足**，这直接决定了
embedding 的质量。

```python
def chunk_by_structure(text: str, *, target_tokens: int = 400,
                       max_tokens: int = 512, overlap: int = 64) -> list[str]:
    """按段落聚合，超长段落按句子切，相邻块之间留 overlap 个 token 的重叠。

    这个函数刻意不用任何 tokenizer 依赖：它按字符数近似 token 数
    （中文约 1 字符 ≈ 0.7 token，英文约 4 字符 ≈ 1 token）。
    生产上应该换成真实 tokenizer 的计数，因为"512 token 的块"
    在不同模型里的字符数差别可以到两三倍。
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf, buf_len = [], [], 0

    def flush():
        nonlocal buf, buf_len
        if buf:
            chunks.append("\n\n".join(buf))
            # 保留尾部 overlap：下一块从头带上这一段
            tail = chunks[-1][-overlap * 2:] if overlap else ""
            buf = [tail] if tail else []
            buf_len = len(tail) // 2

    for para in paragraphs:
        p_len = max(1, len(para) // 2)          # 粗略的 token 估算
        if p_len > max_tokens:
            # 超长段落：先 flush，再按句子切
            flush()
            for sentence in _split_sentences(para):
                s_len = max(1, len(sentence) // 2)
                if buf_len + s_len > target_tokens:
                    flush()
                buf.append(sentence)
                buf_len += s_len
            continue
        if buf_len + p_len > target_tokens:
            flush()
        buf.append(para)
        buf_len += p_len

    if buf:
        chunks.append("\n\n".join(buf))
    return [c for c in chunks if c.strip()]


def _split_sentences(text: str) -> list[str]:
    """按句末标点切句。生产上这里应该换成真正的分句库，
    因为中文的省略号、引号收尾、以及英文的缩写点都会骗过朴素的
    正则切分。"""
    ...
```

有两个工程细节值得单独提。

第一，**给每一块存上它的位置**（文档 ID、块序号、字符区间）。
检索到一块之后，你几乎总要做两件事：把它的前后邻居一起取出来
喂给 LLM（"扩展上下文"），以及在 UI 上高亮原文位置。没有位置
信息这两件事都做不了。

第二，**块的元数据里要带上来源的层级信息**（标题路径、
页码、章节名）。这既是为了过滤（"只在这一章里搜"），
也是为了在 prompt 里给 LLM 提供上下文。

### 25.1.3 嵌入：批处理、失败重试、缓存

嵌入这一步的两个特征决定了它的工程形态：它是**最贵的一步**
（要么花钱调 API，要么花 GPU 时间），也是**最容易中途失败的一步**
（一次批量 1000 条、跑几分钟、网络抖一下就前功尽弃）。

**批处理。** API 侧的批量大小通常受两个限制：单次请求的条数上限
和单次请求的 token 总数上限。稳妥的做法是按 token 总数动态攒批，
而不是固定条数——一条 5000 token 的文本和一条 10 token 的文本
放进同一个"100 条一批"里，前者会把整批撑爆。

**失败重试。** 指数退避加抖动。我们的 embedding 接口把这做成了
内建方法，而不是留给每个调用方自己写：

```python
from mini_lancedb.embedding import retry_with_exponential_backoff

vectors = retry_with_exponential_backoff(
    fn.compute_source_vectors,
    max_retries=7,        # 延迟序列 0.5, 1, 2, 4, 8, 8, 8 秒，累计约 31 秒
    base_delay=0.5,
    max_delay=8.0,
)(batch_texts)
```

`max_retries=0` 表示不重试、直接透传异常——这一项是有意提供的，
因为**给本地确定性函数套重试只会掩盖 bug**。远端 API 要重试，
本地哈希函数重试等于把崩溃变成慢崩溃。

**缓存。** 这是最容易被跳过、收益最大的一步。缓存的键是
`(模型 ID, 内容哈希)`，不是 `(模型 ID, 文档 ID)`——
因为同一段文本可能在多个文档里重复出现（模板、免责声明、版权声明），
按内容缓存能省掉一大块。

```python
import hashlib, os, sqlite3, numpy as np

class EmbeddingCache:
    """基于(模型, 内容哈希)的嵌入缓存。

    用 SQLite 而不是一个 dict：因为它是**跨进程、跨运行**持久的。
    一次全量重建要跑几个小时，中途进程崩了不能让已经算好的
    几十万条白算。dict 缓存在进程退出时就没了。
    """
    def __init__(self, path: str, model_id: str):
        self.conn = sqlite3.connect(path)
        self.model_id = model_id
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS emb "
            "(key TEXT PRIMARY KEY, dim INTEGER, vec BLOB)")

    def _key(self, text: str) -> str:
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{self.model_id}:{h}"

    def get_many(self, texts: list[str]) -> tuple[list, list]:
        """返回 (命中的向量, 未命中的下标)。"""
        found, missing = {}, []
        for i, t in enumerate(texts):
            row = self.conn.execute(
                "SELECT dim, vec FROM emb WHERE key = ?", (self._key(t),)).fetchone()
            if row is None:
                missing.append(i)
            else:
                found[i] = np.frombuffer(row[1], dtype=np.float32).reshape(row[0])
        return found, missing

    def put_many(self, texts, vectors) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO emb (key, dim, vec) VALUES (?, ?, ?)",
            [(self._key(t), len(v), np.asarray(v, dtype=np.float32).tobytes())
             for t, v in zip(texts, vectors)])
        self.conn.commit()
```

**校验。** 一个必须做的检查：`ndims()` 返回的维度和实际产出的
向量宽度必须一致。写小了会截断，写大了会补 0，两种都是**静默的
数据损坏**——向量还在、索引还能建、查询还能跑，只是结果是错的。
我们的 `embedding_matrix()` 会逐条比对宽度并在不一致时报错，
这个检查不要省。

### 25.1.4 写入：批量大小与 upsert

写入这一步的核心参数是**批量大小**，它的两边都有代价。

批量太小（比如一行一次 `add`）会把表打成一堆极小的 fragment，
每次查询都要打开几百个文件、读几百份元数据。批量太大则会在
一个批次失败时丢掉更多的进度，而且单个 fragment 过大会让
后续的压实和索引重建更笨重。

一个实用的起点是**每批 1000 到 10000 行，或者按 10–100 MB 的
字节量攒批**。

去重和更新用 `merge_insert`，而**不是**"先删后加"：

```python
(
    table.merge_insert("doc_id")                 # join 列
         .when_matched_update_all()              # 已存在 -> 更新
         .when_not_matched_insert_all()          # 不存在 -> 插入
         .execute(new_rows)
)
```

这套组合就是标准的 upsert。真实返回值（我们实测的形态）：

```
MergeResult(version=4, num_updated_rows=4000, num_inserted_rows=0,
            num_deleted_rows=0, num_attempts=1, num_rows=4000)
```

六个字段：新版本号、更新行数、插入行数、删除行数、提交重试次数、
处理的总行数。**`num_attempts` 这个字段在生产上很值钱**——
它大于 1 说明发生了提交冲突，是并发压力的早期信号。

几个必须知道的行为：

`where=` 里引用两侧的列要用前缀。**真实 LanceDB 的语义**是：`target.`
指表里已有的行、`source.` 指传进来的行，所以"只在源数据更新的时间戳
更晚时才覆盖"写成 `where="target.updated_at < source.updated_at"`。

**但这条在迷你实现里不成立，别照抄。** 本实现的
`when_matched_update_all(where=...)` **把 `where` 参数直接丢掉**——
传了和不传的效果逐行相同，不报错、不警告。实测：表里两行是
`(doc_id=1, updated_at=10)` 和 `(doc_id=2, updated_at=10)`，用上面那个
`where` 去 upsert `(doc_id=1, updated_at=5, text="stale")` 和
`(doc_id=2, updated_at=99, text="fresh")`，两次调用（带 `where` / 不带
`where`）都返回 `updated=2`，而且 `doc_id=1` 被改成了 `updated_at=5`
那一行——正是这个 `where` 本该拦住的"旧数据覆盖新数据"。`target.` /
`source.` 这两个前缀也因此没有任何意义。

所以在这一份实现上做"按时间戳取舍"的 upsert，只能把判断放到应用层：
先读出目标行的 `updated_at`、和源数据逐行比一遍、**把该更新的行过滤出来
之后**再交给 `merge_insert`。另外，被丢掉的只有
`when_matched_update_all` 的 `where`——`when_matched_delete(where=...)`
的参数是会被解析并生效的。

当 `merge_insert` 新建一行时，**源数据里缺的列会被写成 null**。
如果那个列在 schema 里是非空的，**插入会直接失败**。所以
upsert 的数据里最好带上完整的列，或者把所有列都声明成可空。

主键在 LanceDB 里是**元数据**，用于 `merge_insert` 这类操作，
它**不是**普通写入时的唯一性约束。也就是说，两次普通 `add`
完全可以写进两条 `doc_id` 相同的行。约束是你自己靠 upsert
维持的，不是数据库替你保证的。

最后一条很容易在测试里吓人一跳：**这些操作可能改变行的顺序**。
真实文档的 docstring 里写着 "the data may appear to be reordered
as part of this operation"。所以任何依赖行序的外部逻辑都不要写。

### 25.1.5 索引：什么时候建、建什么

**建什么。** 向量列建 ANN 索引（`IvfPq` 是最常用的通用选择；
数据量小、要求精确时用 `IvfFlat`）。要过滤的标量列建
`BTree` / `Bitmap` / `LabelList`。要全文检索的文本列建 `FTS`。
`merge_insert` 的 join 列**应该**建一个标量索引，
而且**建了还要显式打开 `use_index(True)`**——默认是关的，
不打开的话 join 依然是全量扫键列，"建了索引"和"用上索引"是两件事
（附录 D 有这条，第 9 章 §9.7 有完整说明）。
按官方文档的说法，未索引行数超过 10000 时 `merge_insert` 会直接报错，
但**这条限制本机没复现出来**（见下面的"一条本地核实的说明"）。
所以准确的说法是：**建索引是官方文档给出的硬性要求，也是实测有效的
性能手段；"不建就一定报错"这一条只在服务端部署上成立。**
两条理由都指向同一个动作，但证据强度不一样——别把它们混成一句"必须"。

```python
from lancedb.index import IvfPq, FTS, BTree

table.create_index("vector", config=IvfPq(distance_type="cosine",
                                          num_partitions=256,
                                          num_sub_vectors=32))
table.create_index("text", config=FTS())
table.create_index("doc_id", config=BTree())
```

（`create_scalar_index` 和 `create_fts_index` 这两个旧接口仍然
能用，但会发弃用警告，完整清单在附录 A 的 A.3 节。）

**什么时候建。** 这里有一个反直觉的答案：**索引不是在"数据写完
之后"建的，而是在"数据量到了该怎么查"的临界点建的。**
对向量索引，粗规则是**大约 10 万行/向量以上**才值得建（第 12 章
§12.5、第 24 章 §24.3 有完整讨论）——更少的时候暴力扫描还在零点
几毫秒量级，收益很薄；而且这个门槛会随维度升高、延迟预算收紧而
下降。对多向量列（第 21 章），门槛要低得多。

对 `merge_insert` 的 join 列，门槛是**另一回事**：它不是一个"值不值得
建"的性能建议，而是一条**硬性的安全上限**——**未被索引覆盖的行数**
超过 10000 时，`merge_insert` 会直接报错（25.2 节展开）。注意这两个
数字说的不是一件事：前者是"要不要建索引"的经验阈值，按**总行数**算；
后者是"未索引行数"的硬上限，按**总行数减去已索引行数**算。把
"join 列必须建标量索引"读成"表里只能有 10000 行"，就把它读错了。

索引建好之后**不会自动覆盖新写入的数据**。这一点用 `index_stats`
就能看出来：

```python
stats = table.index_stats("vector_idx")
stats.num_indexed_rows, stats.num_unindexed_rows
# 真实库返回的是 IndexStatistics 对象：属性访问可用，也支持 stats["num_unindexed_rows"]。
# 本实现的 index_stats() 返回的是**普通 dict**，属性访问会 AttributeError，
# 只能写 stats["num_unindexed_rows"]——附录 A 说两者"形状一致"，
# 指的字段名一致，不是访问方式一致。
```

数据写入之后 `num_unindexed_rows` 会涨，而那些行**仍然搜得到**
（走暴力扫描兜底），所以你不会立刻发现问题——只是查询慢慢变慢。
把 `num_unindexed_rows` 作为常驻监控项，是为了让这件事在
变慢之前就被看见。

### 25.1.6 查询：prefilter 还是 postfilter

`where` 的默认行为是 **prefilter**：先按谓词筛出候选，再在候选里
做向量检索。`prefilter=False` 是 postfilter：先用 ANN 取回 k 个
最近邻，再用谓词把它们筛一遍。

这两个语义的差别不是性能，是**正确性**。实测：

```python
t.search([50.0, 0.0]).where("id = 50",  prefilter=True).limit(5)   # 1 行
t.search([0.0, 0.0]).where("id >= 90", prefilter=False).limit(5)  # 0 行
```

第二个查询返回了 **0 行，尽管它要的是 5 行**。原因很直白：
它先让 ANN 取回 5 个候选，再用 `id >= 90` 去筛，一个都没剩下。
这不是 bug，是"先排序后过滤"这个顺序的必然结果。真实文档的原话是
「it can cause the query to return fewer than `limit` results
(or even no results) if none of the nearest results match the filter.」
两条路径在查询计划上分别落在哪一层，第 19 章 §19.5 讲全了；
这里只讲**生产里怎么设**。

**性能上的选择规则，一句话：命中率低用 prefilter，命中率高用
postfilter。** 这条规则极容易记反（"选择性"这个词也常被正反混用），
所以把两面并排摆出来对照：

**谓词命中率低（比如只命中 1% 的行）→ 用 prefilter。** 反向操作
（postfilter）会让 ANN 先在全集里取 k 个候选、再筛掉 99%，只要谓词
够苛刻，返回的行数就少到没法用；prefilter 则把向量检索的候选集直接
缩小 100 倍，ANN 的工作量降两个数量级，召回率也不掉。典型场景：
"排除已删除的行""只要某一个租户的数据"。

**谓词命中率高（比如命中 90% 的行）→ 用 postfilter（`prefilter=False`）。**
这时 prefilter 只是白付一次过滤开销——把一个 90% 大小的掩码交给索引、
索引内部还要在掩码上过滤一遍，省下的距离计算抵不过多付的开销；
反过来 postfilter 拿回 100 个候选里还有 90 个有效，筛掉 10 个再补
几个完全可以接受。典型场景："排除一小撮下架商品""状态不是
archived 的行"。

**不确定的时候用 prefilter。** 它在两种命中率下都**不会返回错的
结果**（只是命中率高的时候慢一点），而 postfilter 少的行数是静默的
——你只会看到"召回率怎么这么差"，看不到任何报错。

### 25.1.7 维护：optimize 的触发时机

写入不会自动整理碎片。删掉的行还在 fragment 里占着位置，
小的 fragment 会越积越多，索引会慢慢落后于数据。
`optimize()` 一次做三件事，官方文档的模型是 PostgreSQL 的 VACUUM：
**压实**（把小的文件合并成大的）、**淘汰**（删掉数据集的旧版本）、
**索引**（把新数据并进已有索引）。

**什么时候在调度器里排它。** 触发规则本身——"累计 10 万行变更、
或者 20 次修改操作，以先到者为准"，以及它背后那两个计数器各自防的是
什么——第 23 章 §23.4 与 §23.6 是权威出处（本实现把它落成了
`should_optimize()`），这里只说运维上的排法：把它放进一个**独立的
定时任务**、每小时或每天跑一次，跑之前先看第 23 章 §23.4 那四个信号；
**绝不要在写入路径上顺手调它**——它是分钟级的重操作（要读 + 重写
大量数据、可能触发索引重建），塞进写路径会把写入延迟直接拖垮。

```python
import datetime as dt

table.optimize()                                      # 默认保留 7 天
table.optimize(cleanup_older_than=dt.timedelta(days=1))   # 更早回收空间
```

有两个坑必须知道。

第一，**`optimize()` 返回 `None`**。类型存根声明它返回
`OptimizeStats`，但 Python 公开 API 里没有 `return` 语句——我们实测
`t.optimize()` 得到的就是 `type(r) is NoneType`。所以不要写
`stats = table.optimize(); stats.compaction`。（返回值的这桩不一致，
第 23 章 §23.6 讲得更细。）想要统计只能退回 `compact_files()`
（返回 `CompactionStats`）和 `cleanup_old_versions()`（返回
`CleanupStats`），而这两个在 0.21.0 就被标为弃用，且**依赖 Python
侧的 `pylance`**，没装就直接 `ImportError`（我们实测到了这条）。

第二，**压实本身不释放磁盘空间**。官方警告说，压实只是把小的
fragment 合并成大的，旧的版本文件要等淘汰时才删；所以压实期间
磁盘占用会**先涨后落**。真正回收空间的是 prune 那一步，
而它的窗口由保留策略决定。

## 25.2 merge_insert 的硬限制：10000 行

这是生产上最容易踩的坑之一，因为它是一个**硬性的**、
会在数据量涨到某个点之后突然出现的错误。

官方文档给出的完整错误文本是：

```
Bad request: Merge insert cannot be performed because the number of
unindexed rows exceeds the maximum of 10000
```

触发条件是：**表里未被索引覆盖的行数超过 10000 行**，
而 `merge_insert` 的 join 列上没有可用的标量索引。
文档给的处置办法只有一句：**确认 join 列上的标量索引是最新的**，
然后重试。

为什么会这样？因为 `merge_insert` 的语义是"把输入数据和目标表在
你给的那个键上做 join"。没有索引的时候，这个 join 只能退化成
**全列扫描**。官方对代价的说明是：这个命令在输入数据和目标表之间、
按你提供的键做 join，**这要求扫描那一整列**，对大表可能很贵；
要加速就得在 join 列上建一个标量索引，这样 LanceDB 不必扫描全表
就能找到匹配。

所以这 10000 行的阈值和"全列扫描有多贵"是同一件事的两面。
文档在性能那页也重申了一遍：`merge_insert` 显著慢于 `add`，
因为 `add` 只是追加新 fragment，而 merge 要扫描已有数据找匹配、
然后在一个事务里删旧插新。"先建标量索引"这句话在官方文档里
出现了不止一次。

正确的写法因此是：

```python
table.create_index("doc_id", config=BTree())    # 先建，再 merge
(table.merge_insert("doc_id")
      .when_matched_update_all()
      .when_not_matched_insert_all()
      .execute(new_rows))
```

而且索引要**保持最新**。批量写入之后如果不跑 `optimize()`，
新写的行会变成"未索引行"，累积到 10000 就重新触发这个错误。
所以生产上的做法是把 `create_index` / `optimize` 和每日的
增量写入排在一起，并且把 `num_unindexed_rows` 报警。

**一条本地核实的说明。** 这个错误我们**没有能在本机复现**。
在 0.40.0 的本地连接上，用 15000 行、join 列无索引的表跑
`merge_insert`，操作**成功了**：

```
rows: 15000
--- merge_insert 在 join 列无索引 + 未索引行 > 10000 ---
  成功（没有报错，行数 -> 17000）
--- 建标量索引后重试 ---
  成功; result = MergeResult(version=4, num_updated_rows=4000,
        num_inserted_rows=0, num_deleted_rows=0, num_attempts=1, num_rows=4000)
```

而且 `unindexed rows exceeds` 这个字符串**在安装包的二进制和
Rust 扩展里都搜不到**。结合文档把它描述成一个 **HTTP 400** 错误，
最合理的解释是：这是一道**服务端（Cloud / Enterprise）的护栏**，
不在本地 OSS 的连接路径上。文档在 Enterprise 那页也提到
"platform-level guardrails, such as ... safety checks around
operations like `merge_insert` when too many rows are unindexed"。
所以：**错误文本、10000 这个阈值、以及"必须给 join 列建标量索引"
这条处置办法，都来自官方文档；本地 OSS 0.40.0 上没有复现出这个
报错。** 在实践中仍然要按它来设计，因为你无法保证未来不切到
服务端部署。

## 25.3 向量自动生成的真实语义

如果表带了 embedding 定义（schema 里有 embedding metadata），
LanceDB 会在写入时自动计算向量。但**触发条件比大多数人以为的
严格得多**。

官方文档把触发条件说得很死：如果输入里已经带上了向量列，自动编码
只在**该列整批缺失、或者整批全为 null** 时才跑；**部分提供**的向量
会被当成手工数据，LanceDB 原样保留，而不是"只把缺的那几行补上"。

拆成三句话就是：

**整批都没有向量列** → 自动编码。**整批的向量列全是 null** →
自动编码。**只要有任何一行提供了非空向量** → 整批被当成
"手工数据"，**既不回填缺的行，也不报错**。

最后那句是本节的要害。**部分提供向量是一个静默的坑**：
你以为缺的那些行会被补上，实际上它们被原样留下（要么是 null、
要么是零向量），而系统不会给你任何提示。等你去查询的时候，
那些行表现出的行为是"永远排最后"或者"完全搜不到"，
而你不会想到问题出在几个月前的一次批量写入上。

我们自己实测了一遍这个规则，结论比文档还要更直白一些。
在 0.40.0 上用 pydantic schema（`Vector(n)`）走一遍：

```
1) 不给 vector 列:
    {'text': 'hello', 'vector': [1.0, 1.0, 0.0, 0.0]}
    {'text': 'world!', 'vector': [2.0, 1.0, 0.0, 0.0]}

2) 一行给向量 + 一行为 None: RuntimeError: Arrow error: C Data interface
   error: Invalid: Vector column 'vector' has variable length vectors.
   Set on_bad_vectors='drop' to remove them, ...

3) 整批全 None: RuntimeError: ... Invalid null value. ...

4) 整批都手工给向量:
    {'text': 'hello', 'vector': [9.0, 9.0, 9.0, 9.0]}
    {'text': 'world', 'vector': [8.0, 8.0, 8.0, 8.0]}
```

也就是说，在这条路径上：不给列 → 自动编码（符合文档）；
全给 → 原样保留（符合文档）；而**部分给**和**全 None** 这两种
情况根本走不到"静默不回填"那一步，**在 pydantic 这一层就直接
报错了**（`has variable length vectors` / `Invalid null value`）。
`add()` 也是同样的报错。

所以工程结论很清楚：**不要让"哪一行有向量、哪一行没有"变成
一个运行时才知道的状态。** 要么整批都不带向量列、交给表去算，
要么整批都带向量、自己算。混合供给在这套 API 下要么报错、
要么（在别的代码路径上）静默地不回填，两种都不好。

（附带一条：文档还提到 `merge_insert` 的行为略有不同——
"Embeddings are only auto-generated when that source field is
present in the incoming data, and the vector field is empty"。
措辞比 embedding 那页的"entirely null for the batch"更宽松，
这个不一致**未经确认**。）

## 25.4 并发写

### 25.4.1 对象存储上的写是"乐观"的

Lance 的提交模型是**不可变数据 + 一次条件写**。写者做完所有
昂贵的活（写数据文件、建索引文件）之后，最后只做一个很小的
动作：写入一份新的版本 manifest。这份 manifest 的写入是
**条件写**——"如果这个版本号还不存在，就写进去"。

于是冲突的粒度极小，而且出现的时机**很晚**：两个写者可以同时
读数据、同时算、同时写数据文件，只在最后提交那一瞬间才可能撞车。
撞车时失败的一方**什么都不用回滚**（数据文件本来就不用删，
它们没被任何 manifest 引用），只要重新读最新的 manifest、
重新算一次新 manifest、再提交一次就行。这就是"乐观"的含义：
不预先加锁，只在提交时检测冲突并重试。

删掉重试的复杂度之后，剩下的就只有一件事：**确保你用的对象存储
支持条件写。** 官方在存储页的说法是"S3 and S3 Express now support
atomic writes natively, so LanceDB handles concurrent writers against
the same table out-of-the-box — no external commit coordinator is
required."

（"optimistic concurrency"这个说法在官方文档里**找不到**，
概念是被"atomic writes / 不需要外部提交协调器 / 写者会重试提交"
这些措辞表达的。）

上面讲的都是**写者之间**怎么不打架。但"不可变快照"还有另一半，
是**读者侧**的：**你读的是你手里那份快照，不是别人刚提交的那个版本。**
控制这件事的旋钮是 `read_consistency_interval`。不设（默认）时官方
文档的说法是 "no automatic cross-process refresh checks"，也就是
**表不会自动从别的写入者那里刷新**，想看最新版本得显式
`checkout_latest`；设成 `timedelta(0)` 是强一致，设成 30–60 秒是官方
推荐的弱一致。文档同时提醒 "**Stronger consistency is not free**"——
间隔越短，每次读都要多付一次刷新检查的成本，延迟和费用都会上去。

这条参数在第 6 章 §6.6 讲存储选型时就展开过（它决定了"两个读者看到
不同版本"是不是可接受），第 8 章又用它解释过快照隔离的边界——那里
描述的形态相当于"永远不自动刷新"。放到生产里的读法是：**"写完立刻
读得到"不是默认行为，而是一个要显式买的属性。** 如果管道里有
"写一批 → 立刻按主键回读校验"这种步骤，要么把
`read_consistency_interval` 调到需要的档次，要么在回读前
`checkout_latest`；不处理的话回读拿到的可能是写之前那份快照，
而且它**不报错**，只是安静地给你旧数据——校验步骤会"通过"得毫无意义。

### 25.4.2 冲突矩阵

并不是所有操作都会互相冲突。这个矩阵值得记住，因为它决定了哪些
后台任务可以并行跑。下面把每种操作和"会不会跟别的写冲突、为什么"
排在一起：

| 操作 | 与其它写冲突？ | 原因 |
|---|---|---|
| `search` / `to_arrow` 等读操作 | 否 | 读的是不可变快照 |
| `add()` | 否 | 只追加新 fragment |
| `merge_insert` 的**仅插入**形态 | 否 | 只创建新 fragment，不改已有行 |
| 给表**加新列** | 否 | 只改 schema，不重写 fragment |
| 带更新的 `merge_insert` | 是 | 改已有行，要"删旧插新" |
| `delete()` | 是 | 改已有 fragment（写删除文件） |
| `compact_files()` / `optimize()` | 是 | 重排 fragment，会让别人的快照失效 |

（"加新列"指只改 schema 的那种；如果加列还要回填数据，它就是一个
普通的写操作，按写操作算。）

矩阵的结论有两面。**好的一面**：多个进程各写各的新数据、
互不干扰，这是最常见的并发形态，可以放心并行。**坏的一面**：
**多个进程各自跑 `optimize()` 会互相打架**，因为压实是"重排
已有 fragment"这个动作，两个进程同时做就是在互相使绊子。
真文档在讲大规模回填时明确写了一句"考虑在大规模回填期间
禁用自动压实，避免冲突"——同一个道理。

所以生产上的规矩是：**压实和清理必须由单一所有者来做**
（一个定时任务、一个 sidecar 进程），不能每个 worker 各自做。

### 25.4.3 为什么多进程写需要重试

乐观提交的重试次数是有限的。官方 FAQ 的说法是"For writes, we
support concurrent writing, though too many concurrent writers can
lead to failing writes as there is a limited number of times a writer
retries a commit."

"有限次数"具体是多少，官方给的一套参考值是：

```
GENEVA_COMMIT_MAX_RETRIES            12   指数退避 1/2/4/8/16/16/... 秒，约 2.5 分钟
GENEVA_VERSION_CONFLICT_MAX_RETRIES  10   版本冲突的重试上限
GENEVA_RETRY_LANCE_ATTEMPTS           7   I/O 重试（含 "Too many concurrent writers"）
```

（这是 Geneva 这套框架的默认值，未必等于 OSS 的默认值——
**未经确认**。但数量级是有参考价值的。）

**这三个数和正文里另外两处不是一回事，放在一起看才不会被绕晕。**
第 8.2 节说的是**真实 Lance 那个 dataset 层**的提交重试默认值：
**20 次、总时限约 60 秒**；第 8 章还提到本实现的 `Dataset.write`
只重试 **3 次**。所以同一件事在书里有三个数字：3（本实现）、
20 / 约 60 秒（Lance 的 dataset 层）、12 / 约 2.5 分钟（Geneva 框架）。
**它们不是互相矛盾的三个说法，而是三个不同层各自的默认值**——
最里层是存储引擎的提交循环，外面是云端的作业框架。
读任何一个"重试 N 次"的说法之前，先问清楚 N 是哪一层的，
这和 §25.4.3 开头"重试次数要点明是谁的默认值"是同一个要求。
选型时按**最外层**那个数给应用层留余量：框架层能等 2.5 分钟，
不意味着你的 API 请求也该等那么久。

**再往前一步的问题是"哪些错误才值得重试"**，那一层在附录 K：
实测下来 `lancedb` 的异常类型几乎给不出可用信号（十个探针里八个是
`ValueError`），所以可重试性只能按"归责方"判断——调用方的错不重试、
环境的错必重试、并发的错带上限重试、能力缺失的错降级不重试。
**这一节给的是"重试几次"，附录 K 给的是"什么样的错才轮得到这个次数"。**

2.5 分钟听起来很久，但如果你的重试策略是"睡 1 秒再来"，
在写者密集的时候很容易在窗口之外失败。**应用层要自己再加一层
重试**，而且要用带抖动的指数退避——多个 worker 同时线性重试
会让它们永远撞在同一时刻。

还有一个更容易被忽略的地方：**"Too many concurrent writers"
这个错误是可重试的**，不是致命错误。把它当成不可恢复的错误
直接让任务失败，是很多管道在高峰期丢数据的直接原因。

## 25.5 进程模型：fork、spawn 和 AVX2

这一节的每一条都会让进程直接死掉，而且死法都很不直观。

### 25.5.1 fork 和 Lance 不兼容

Lance 内部是多线程的。而 `fork` 只复制调用线程，复制出来的
子进程里那些"存在于其它线程中的锁"会永久处于锁定状态——
一旦子进程碰到它，就是一个死锁或崩溃。这不是 Lance 特有的问题，
是多线程进程 fork 的通用问题。

官方文档在三处地方说了同一件事，措辞一次比一次直接。性能那页是
"When using Python with multiprocessing, use `spawn` rather than
`fork`. LanceDB is multi-threaded internally, and `fork` plus a
multi-threaded process is unsafe."；FAQ 那页是"If you use Python's
multiprocessing, you should probably not use `fork` as Lance is
multi-threaded internally and `fork` and multi-threaded Python do not
work well together."；查询优化那页最短："Multiprocessing with `fork`
is not supported. You should use `spawn` instead."

所以：

```python
import multiprocessing as mp

if __name__ == "__main__":
    ctx = mp.get_context("spawn")        # 显式指定，不要依赖平台默认值
    with ctx.Pool(processes=8) as pool:
        pool.map(worker, tasks)
```

在 Linux 上还可以考虑 `forkserver`：它比 `spawn` 启动便宜
（不用重新 import 一遍所有模块），但仍避开 fork 的线程问题。
文档的推荐是："Prefer the `forkserver` start method when using
multiple workers. LanceDB uses internal threads, so the default
`fork` method is unsafe; `forkserver` avoids that while being cheaper
to start than `spawn`"。**`forkserver` 只在 POSIX 上可用；
Windows 上只有 `spawn`，没有别的选择。**

在 PyTorch DataLoader 这类会自己起进程的地方，`num_workers > 0`
就等于在用 multiprocessing，所以同样要显式指定 start method。

### 25.5.2 没有 AVX2 的 x86_64 CPU 上 import 就会崩

这一条最容易被误诊，因为它看起来像一个"安装坏了"的问题，
而不是"CPU 不支持"。

默认的 `lancedb` wheel 是按 **`x86-64-haswell`** 这个基线编译的，
要求 **AVX2 + FMA + F16C**。在没有 AVX2 的老 x86_64 CPU 上——
文档点名的是 Intel Sandy Bridge、Ivy Bridge、Westmere，
AMD Bulldozer、Piledriver、Steamroller——**`import lancedb`
直接以 `Illegal instruction` 崩掉**。

注意这个错误的形态：它不是 ImportError，是进程收到 SIGILL
直接终止。在一个容器里看到"进程启动就没了、日志里什么都没有"，
这是要排查的可能性之一。

解药是换一个 wheel：

```bash
pip uninstall lancedb && pip install lancedb-compat
```

`lancedb-compat` 暴露同一套 API（`import lancedb` 照旧能写），
编译基线是 **`x86-64-v2`**，并用**运行时 SIMD 分发**在有 AVX2 /
FMA / AVX-512 的机器上照样用上这些指令。两个包共用同一个
`lancedb/` 命名空间，**安装时会冲突**，所以必须二选一。

### 25.5.3 一个顺带的进程间问题：EmbeddingFunction 不跨进程

我们的 embedding 注册表是**进程内**的单例：用
`register_embedding_function` 注册的函数、用 `set_var` 设的变量，
都不会跨进程保留。所以 `spawn` 出来的子进程里，
"从表 metadata 还原 embedding 函数"这件事会失败——
因为注册表里没有那个名字。

处理办法是把注册动作写进一个模块，在子进程的入口处重新执行
（`spawn` 会重新 import 主模块，所以写在模块顶层就够了），
或者干脆把注册表当成配置的一部分显式重建。

## 25.6 版本与保留策略

版本机制本身（manifest 怎么命名、时间旅行为什么零成本、`latest_version_hint.json`
这个缓存是干什么的）在第 7 章；这一节只讲**保留策略**——运维上该留多久、
哪些版本删不掉、删的时候会踩什么。

### 25.6.1 留多少个版本

官方文档**没有给推荐的具体数字**，只给了两个时间窗口，而这两个
窗口本身还不一致：

`optimize()` 的 `cleanup_older_than` 文档说默认 **7 天**；
`cleanup_old_versions` 的 docstring 说 `older_than=None` 时
"defaults to **two weeks**"。同一个库里两处说法不同
（我们的实测是 `inspect.signature` 确认了 `older_than=None`
这个默认值本身，但"两周"这个语义在 Python 层没有硬编码，
是透传给 Rust 的——**真正生效的默认值未经确认**）。

结论是：**不要赌默认值，显式传参数。** 保留多久完全取决于你的
回滚需求。写一条"我们承诺可以回滚到 24 小时前"的 SLO，
然后把 `cleanup_older_than=timedelta(days=1)` 写死。

### 25.6.2 版本不是免费的

这是前面第 7 章讲过的那个观察，官方文档里有对应的原话："Each
version contains metadata and just the new/updated data in your
transaction. So if you have 100 versions, they aren't 100 duplicates of
the same data. However, they do have 100x the metadata overhead of a
single version, which can result in slower queries."

**100 个版本不是 100 份数据，但有 100 倍的元数据开销。**
而元数据是每次查询都要读的东西。在对象存储上这件事还会更严重
一层：打开一张有很多版本的表，代价主要花在 LIST 上
（"opening a table with many versions is dominated by listing cost"）。

所以"版本多会让查询变慢"是一条**真实的、可测量的代价**，
不是理论担忧。

### 25.6.3 标签是版本的保护伞

标签（tag）和版本的关系是**保护**：打了标签的版本在清理时会被
跳过。官方原话是"Tagged versions are exempt from cleanup. A version
with a tag pointing at it is retained regardless of age, and its files
are not removed by `optimize()`. To make a tagged version eligible for
pruning, delete the tag first."

于是标签有两种用法，都应该用起来。

**一个是给机器读的锚点**：把"某次发布"对应的版本打上标签，
回滚时就 checkout 那个标签，而不是去记版本号。

```python
table.tags.create("release-2026-10-01")     # 默认指向当前版本
table.checkout("release-2026-10-01")        # 回滚到它
```

**另一个是给 GC 的护栏**：你想保留一个"三个月前刚上线时"的
基线版本用来做回归对比，就给它打个标签。它会一直在，
不管清理窗口是 7 天还是两周。

要小心的是**反向的坑**：标签会**阻止**空间回收。一个被忘掉的
标签会让整个历史数据集一直留在盘上。所以标签要有生命周期管理，
不要只创建不删除。

### 25.6.4 清理的时间窗口

清理有一个安全窗口，是为了保护**在途的读者**。第 7 章讲过这个
道理：一个跑了 20 分钟的大查询可能正握着老版本的 manifest 在读，
这时把它的数据文件删掉，它就会读到半个表。

所以 `delete_unverified` 的默认值是有意义的：它的文档说
"Because they may be part of an in-progress transaction, files newer
than 7 days old are not deleted by default"。把它设成 `True`
意味着"我保证没有别的进程在读这张表"——**这是一个非常强的假设**，
在生产环境里基本等于"我打算把表弄坏"。

## 25.7 备份、迁移与监控

### 25.7.1 备份与迁移

备份策略取决于你的存储。**在对象存储上，整个数据集就是一堆
不可变的文件**，所以备份在理论上可以是"复制前缀"——但更划算的
做法是用存储层自己的版本化 / 跨区域复制，因为 Lance 的数据
本来就是只追加的，复制成本被增量地摊销掉了。

迁移有一个和"版本保留"直接相关的好处：**因为版本是 manifest，
迁移整张表 = 复制所有数据文件 + 最后写一份 manifest**。
你不需要停写很久，只需要在切换的瞬间做一次短暂的写冻结。
迁移本身不会影响老版本的可读性——只要文件都在。

上面那句是结论，但它没说清**"复制哪些文件"**。下面这份**迁移步骤清单**
把这句落成可以照做的事——"迁移"在这套格式里的定义很窄：**目标端要能
独立打开这张表**，而不是"能读到数据就行"。（只把当前版本的数据复制
过去、在新位置重建一份 manifest，是一条更省的路；代价是历史版本和标签
都留在源端，时间旅行在老位置有、在新位置没有。）

**第一步，冻结写，并且判定它成立。** 判定条件必须可观测，不能靠
"这个点应该没人写了吧"。先确认没有写者在跑——压实、回填、`optimize()`
都算写者，§25.4.2 那张冲突矩阵里"会冲突"的几种就是它们；然后
**读一次 `list_versions()` 记下最大版本号 V，隔一会儿再读一次，
还是 V 才算冻结成立**。之后再有任何写入，目标端复制的就是一个
**移动过的靶子**，源和目标会永久分叉，而且分叉点没有任何记录。

**第二步，复制这六个目录，而且以"整个前缀"为单位。** 一个 dataset
目录的完整清单是（第 7 章 §7.3 把 `latest_version_hint.json` 单独讲成
一档"刻意的缓存"，第 8 章讲事务时说过一句"事务文件是日志不是状态"，
两处说的是同一件事的两半）：

```
data/            不可变的数据文件（*.lance），全部版本的数据都在这里
_versions/       各版本的 manifest + latest_version_hint.json
_transactions/   事务记录（{read_version}-{uuid}.txn），"我基于 v 做了什么"
_deletions/      删除文件（{frag}-{read_version}-{uuid}.bin）
_indices/        向量 / 标量 / 全文索引文件
_refs/tags/      标签（v1.0.json 这种），只是指向版本号的小文件
```

六项里**只有 `_versions/` 是"少一行都打不开"**——它就是表本身；
其余五项缺了是"能打开但不对"：缺 `data/` 读不出数据、缺 `_deletions/`
会让删掉的行复活、缺 `_indices/` 查询静默退化成暴力扫描、缺
`_refs/tags/` 是标签指向不存在的版本。所以最常见的迁移事故就是
**只复制了 `data/`**——数据看起来都在，但表和索引都不对，而且打开时
不一定报错。

**第三步，在目标端做一次端到端校验，三件事都要过。**

一是**打开 + 真查一次**：`open_table()` 之后再跑一个真实查询，
返回行数和源端一致才算过（只 `open` 不查是不够的，缺 `_indices/`
要查了才暴露）。二是**对版本号**：目标端 `list_versions()` 的最大
版本号要等于第一步记下的 V。三是**checkout 一个老版本再查一次**
（`table.checkout(V - k)`）：这一步验的是"历史版本用到的文件也一起
复制过来了"——只复制"最新版本引用到的那些文件"是一种很自然的省事
做法，但它会让时间旅行**静默失效**：老 manifest 还在，它指向的文件
却不在目标端，要等到有人真的回滚时才炸。

把这三步写成脚本（或者一个迁移后必跑的检查任务），比写在 runbook 里
更可靠：**三步全是读操作**，代价很小，但它们挡住的是"迁移完看起来
一切正常、直到第一次出事"那一类。

### 25.7.2 监控：看哪些指标

**对象存储层的指标。** LanceDB OSS 会导出内部指标，可以接到
任何 OpenTelemetry 后端（`pip install "lancedb[otel]" opentelemetry-sdk`，
启动时调一次 `instrument_lancedb_metrics(provider)`）。
它给的五个指标是：

```
lance_object_store_requests_total                 计数器，按 operation / base 打标
lance_object_store_request_duration_seconds       直方图，请求延迟
lance_object_store_bytes_transferred_total        计数器，读写字节数
lance_object_store_retryable_responses_total      计数器，可重试错误（限流/瞬时故障）
lance_object_store_in_flight_requests             仪表，当前在途请求数
```

官方建议盯四个信号。把它们和"看什么、意味着什么"排在一起：

| 信号 | 怎么看 | 说明 |
|---|---|---|
| 按 operation 分组的请求速率 | `rate(...[1m])` | 读/写压力的分布；它的绝对值本身**就是账单** |
| 尾部延迟 | 直方图分位数 | 平均值会掩盖长尾，只看均值会漏掉最慢的那批请求 |
| 可重试响应的增长 | `..._retryable_responses_total` 的速率 | 官方口径：typically means you are being throttled and should back off or shard writes |
| 在途请求数 | `..._in_flight_requests` | 官方口径：a growing in-flight gauge without a matching rise in throughput indicates queueing |

这几个指标既是性能指标也是**成本指标**——对象存储按请求数计费，
所以"请求速率"的绝对值直接对应账单。

**表健康层的指标。** 这一层要自己从表的元数据里取。
`list_versions()` 返回的元数据字典里有一组现成的计数：

```
total_rows / total_data_file_rows / total_data_files
total_deletion_file_rows / total_deletion_files / total_files_size
total_fragments
```

从这些计数可以直接推出该报警的几个量：

| 指标 | 取法 | 报警含义 |
|---|---|---|
| fragment 数 | `list_versions()` 的 `total_fragments` | 应随时间**稳定**而非单调增长；持续增长说明压实没跑或跑不动（文档口径：Many small fragments build up as you write, slowing down queries） |
| 删除行占比 | `total_deletion_file_rows / total_rows` | 物理存在但不返回的空间；删得越多越该勤压实 |
| 未索引行数 | `index_stats(name).num_unindexed_rows` | 官方建议保持在接近 0（This will be zero when indexes are fully up-to-date）；它也是 25.2 节那个 10000 行错误的**先行指标** |
| manifest 大小 / 版本数 | `list_versions()` 的元数据 | 对应"查询开始前要读多少元数据"；无写入活动时版本数突然翻倍 = 某个后台任务在空转提交 |
| 每查询的 I/O 次数 | `analyze_plan()` 的分阶段耗时 | 用来分清"查询变慢"是 I/O 多了还是算力多了（官方口径：returns the execution plan with per-stage timings, so you can see where the query actually spent its time） |

顺带说明一个边界：**"删除比例"和"manifest 大小"并没有被官方列为
受监控的指标**（本教程在官方文档里找不到这样的表述），它们是直接从
`list_versions()` 的元数据里算出来的**推断量**。`table.stats()` 虽然
存在，但没有文档页说明它的返回结构（**未经确认**）。

最后补一条现在缺的东西：**上面全是指标（metrics），没有链路（traces）。**
OpenTelemetry 的三件套里，OSS 官方只桥接了指标；分布式的**追踪**
要靠客户端侧的自动埋点——社区有 Traceloop 系的
`opentelemetry-instrumentation-lancedb` / `@traceai/lancedb`
做调用链埋点（第 26 章 §26.12）。两者的分工是：指标回答"系统整体
健不健康"、用来报警；追踪回答"**这一个个案**的时间花在哪一跳上"、
用来归因。所以生产上两样都要接：指标进监控看板，追踪进 APM。

### 25.7.3 成本模型

三块成本，结构完全不同，要分开优化。（多模态场景的成本结构见
第 21 章 §21.5、和别家向量库的成本对比见第 27 章；这一节给的是
把三块成本放在**同一个总账**里看的视角。）

**存储 + 请求次数。** 对象存储的计价通常是"存储容量 + 请求数 +
出口流量"三项。多模态场景里**请求次数往往比容量更贵**：
一张 4 MB 的图读 1000 次是 1000 次 GET，和它的体积无关。
所以缓存命中率的收益要按请求数折算，不是按字节数。

这里有一个必须知道的事实：**LanceDB OSS 没有内建缓存。**
官方原话是"LanceDB OSS has no built-in cache. Every read repeats
the same object-store round trip and pays the same latency penalty."
（**没有** 官方的本地磁盘缓存层或 `~/.cache/lance` 这类路径——
这一点我查过，是"未找到"。）

官方给的对比是：OSS 从对象存储读的延迟是 **500–1000 ms**，
Enterprise 靠分布式 NVMe 缓存（按一致性哈希分片、LRU 淘汰，
"Cache misses fall back to the object store, fill the local shard,
and serve future reads faster"）压到 **50–200 ms**。

所以在 OSS 上，**你要自己在前端做缓存**。最有效的做法是
"把热数据的向量和缩略图放进应用层的内存缓存"，
命中时完全不碰对象存储。

**嵌入算力。** 这一块的形态取决于你用的是远端 API 还是本地模型。
官方对 OpenAI / Gemini 的 UDF 明确提示"make API calls that incur
per-token costs ... Each row processed results in one or more API
requests billed to your account"；对 Sentence Transformers 则是
"run locally on your workers — there are no API calls and no
per-token costs. This makes them a good fit for large-scale
embedding jobs where cost is a concern."

两种模式的成本曲线完全不同：远端 API 是**随行数线性增长**的
持续成本，本地模型是**一次性的 GPU 投入 + 电费**。
一次全量重建（比如换模型）在这种结构下是远端方案最贵的时刻，
而它恰好是本地方案最擅长的时刻。这也解释了为什么"换 embedding
模型"在生产上是一个需要排期的重大决策，而不是改一行配置。

**本地 SSD。** 本地 NVMe 很贵，但它的收益是按"请求次数 ×
延迟"算的。适合放在本地的是**小、热、随机访问**的东西：
向量索引文件、FTS 的倒排表、缩略图。不适合的是大的冷门 blob——
那会挤爆 SSD 而没有收益。

## 25.8 安全、治理与运营

前面七节讲的是"管道怎么跑通"。这一节补上另外几件在生产里绕不开、
却在别的章节只被点名的事。

### 25.8.1 安全与授权：OSS 上要自己建，而且别建错层

先说缺口：**开源版没有 RBAC / SSO / 审计的现成实现。** 官方维护者
2025-02-28 在讨论区的原话是「We haven't looked into any RBAC like
system yet」（第 28 章 §28.6、第 29 章 §29.6.8 都引了这条）。
企业版有 RBAC / SSO / 审计，但那是商业能力。所以要在 OSS 上做访问
控制，现实里只有两条路：**用文件系统 / 对象存储的 IAM**，
或者**自己写一层**。

本教程用手写一层演示了这个形状（`mini_lancedb/permissions.py`）：
七种表级权限（`select` / `insert` / `update` / `delete` / `create` /
`alter` / `drop`），加一张**权限的隐含关系表**——`ALTER` 隐含
`SELECT` + `INSERT`、`DROP` 隐含 `CREATE`、而 `CREATE` **不**隐含
`DROP`。把隐含关系显式写出来，是因为"角色名"式的约定一旦组合起来
就藏提权漏洞，那张表里每一条都值得单独想一遍。

但这个模块**自己声明了它不构成安全边界**：所有检查都发生在 Python
进程内部，任何人只要 `import mini_lancedb`、直接对 store 调
`put` / `get`，就能绕过 `AccessController`。**把权限检查写在客户端里，
等于把门锁装在门外面。** 真实系统的检查必须落在数据所在的那条边界上：
对象存储的 IAM policy、服务端的鉴权中间件、或者企业版的 Namespace API。
这条是本节的结论，不是免责声明。

审计同理：OSS 没有审计日志，所以"谁在什么时候读了哪张表"这类问题，
要么在对象存储的访问日志里找（前提是所有读都经过它），要么自己在
应用层记。

### 25.8.2 静态加密与密钥管理

本教程**没有找到** LanceDB 自己提供静态加密（encryption at rest）的
文档页——这一条标为"未找到"，不是"不存在"。实践中静态加密通常落在
**存储层**：S3 的 SSE-S3 / SSE-KMS、EBS 的卷加密、GCS 的 CMEK。
也就是说"给 LanceDB 加密"这件事，做起来基本是"给存放 `.lance` 文件的
那个桶/卷开加密"，再配好 KMS 的密钥轮换与访问策略；传输中加密由对象
存储端点和 SDK 负责，不在这一层。

有一个具体后果值得提前想清楚：**密钥丢了，数据文件是完整但你打不开的。**
所以备份策略里要把"密钥的备份与托管"单列一项，别只备份数据文件。

### 25.8.3 多租户：namespace 是目录，不是所有权

表模型极其扁平（`<root>/<name>.lance`），所以多租户下第一个撞上的
问题是**表名冲突**：两个租户都想叫 `documents`。官方为此提供了
Namespace API（企业版 / 云端，可嵌套，如 `["tenant_a", "project_x"]`），
远端有真正的元数据服务保证隔离。

本教程把这一层做成了一个**本地模型**（`mini_lancedb/namespace.py`），
并明确承认两个不足：**物理表是扁平的**（同一张物理表不能落在两个
命名空间里）、**没有并发控制**（manifest 的读改写不是原子的）。
它演示的核心是这个反直觉的区分：**命名空间是"目录"，不是"所有权"。**
删命名空间、从命名空间摘表，**都不会删任何一张物理表**——表还在根
目录下、`table_names()` 照样看得到。把"目录"和"数据"混在一起，就是
"删了个命名空间结果数据全没了"这类事故的来源。

所以 OSS 上的多租户，现实做法是三选一：**namespace 隔离 + 操作系统层
权限**、**每个租户一份数据目录**、或者干脆上企业版。第三方说 OSS
"不适合多租户高并发后端"（第 29 章 §29.6.6，未经确认），但即使不看
第三方，光是"没有服务端、没有集中鉴权"这两条也足够说明它不是为这个
场景设计的（第 1 章讲嵌入式与服务化的取舍时也点了这一条）。

### 25.8.4 灾难恢复：RTO / RPO 与恢复演练

**版本是 manifest，所以"回滚一个版本"本身很便宜**：`checkout` 一个老
版本是零成本的（第 7 章 §7.4）。但"回滚"和"灾难恢复"是两件事，后者
要先定两个数：

**RPO（能丢多少数据）** 取决于写入是否有回执。如果写是"PUT 数据文件
+ 提交 manifest"，那只要 manifest 提交成功，已提交的数据就不会丢，
RPO 就是 0；落在"半提交"中间态的文件是孤儿，本来也不该算数（§25.4.1）。

**RTO（多久能恢复）** 取决于备份形态。对象存储上的备份是"复制前缀 /
跨区域复制"，恢复 = 把前缀指回去；因为 Lance 的数据文件是只追加的，
每次增量复制很便宜。**迁移整张表 = 复制所有数据文件 + 最后写一份
manifest**（§25.7.1），这同时也是恢复的路径。

**光有 RTO / RPO 不等于能恢复——要演练。** 演练清单至少三条：
一，**从备份真的打开一张表并跑一次查询**（不是"文件数对得上"，而是
"检索结果对得上"）；二，**验一次 `checkout` 老版本**（证明保留窗口内
的版本都还在）；三，**测一次"提交中崩溃"**——在写入过程中 kill 掉
进程，看下一次打开表是不是干净状态（半提交的孤儿文件不该影响读）。
三条里最容易漏掉的是第三条。

### 25.8.5 容量规划

容量规划要算的不是"数据集多大"，而是四项之和：**数据文件 + 索引文件
+ 版本历史 + 压实峰值**。

**数据文件**按列存估（第 14 章 §14.1 那张表：每条向量 `维数 × 4` 字节，
再乘上编码/压缩的收益）。

**索引文件**要单列：IVF_FLAT / IVF_SQ / IVF_PQ 差别很大，HNSW 最吃
内存（第 29 章 §29.6.4）。而且索引的**内存**预算和**磁盘**预算是两笔
账——热的那部分要常驻内存，否则每次查询都要回表（第 24 章 §24.8）。

**版本历史**按保留窗口估：100 个版本不是 100 份数据，但有 100 倍的
元数据开销（§25.6.2）。

**压实峰值**是最容易漏的一项：压实要让新旧片段共存一段时间，峰值占用
可能接近两倍（第 23 章 §23.6）。容量吃紧时，正确的顺序是**先
`cleanup_old_versions` 腾出空间、再压实**。

### 25.8.6 嵌入 API 的背压与限流

嵌入这一步是整条管道里最容易把外部依赖打挂的地方：远端 API 有**限流**
（每分钟多少请求、多少 token），而限流响应本身是**可重试**的。
三件事要做对。

**指数退避加抖动。** §25.1.3 的 `retry_with_exponential_backoff` 是
骨架；抖动是必需的——多个 worker 同时线性退避会让它们永远撞在同一
时刻。

**按 token 数攒批，而不是按条数。** 一条 5000 token 的文本会把整个
"100 条一批"撑爆，而一批被限流，牵连的是同一批里其它 99 条。

**把限流当信号，不当故障。** 对象存储那层有
`..._retryable_responses_total` 这个指标（§25.7.2）；嵌入 API 这边要
自己记"每分钟被限流几次"。持续被限流说明要么退避不够、要么该分片写入
——官方对存储侧的原话是 back off or shard writes，嵌入侧同理。

最后一条纪律：**给本地确定性函数套重试只会掩盖 bug**（§25.1.3）。
重试只该套在远端调用上。

### 25.8.7 schema 演进与迁移

Lance 的 schema 演进是"加新文件"而不是"改旧文件"：加列不重写历史数据
（第 21 章 §21.4 里"给 1000 万行加一列 13 ms"就是这个机制的极端例子）。
所以**加列廉价、改类型/删列是语义上的**——老片段里那一列的数据还在，
只是不再被读。

运维上要盯的是**演进的方向和顺序**：先加列（旧读者忽略它）、再回填、
然后切读写路径，最后才考虑清理。`ALTER` 之所以在权限模型里被单列为
一种高风险权限（§25.8.1），正是因为它会**静默改变已有数据的解释方式**
——改列类型丢了数据你看得见，改错了类型你看不见。跨库迁移的完整路径
（重摄取、重索引，以及"换 embedding 不用迁数据"这条优势）在第 26 章
§26.7。

### 25.8.8 检索质量的评测 harness 进 CI

召回率只在"改了索引参数 / 换了模型 / 升了 SDK"之后掉，而这些改动
**在功能测试里全是绿的**——检索质量没有断言，没人会发现它掉了。
所以要把一个**评测 harness** 放进 CI：固定一批查询、固定 ground truth
（第 24 章 §24.8 讲了 ground truth 怎么造），每次改动都跑一遍
recall@k，**低于基线就 fail**。

几个要点。**查询集要从真实日志里采样**，别用开发者手写的十条——
手写查询会把"我测过的那几条"当成全局。**基线要写进仓库**，否则
"上次是多少"只能靠人记。**断言的是确定性指标**：召回率、候选集大小、
读取字节数可以断言；延迟是挂钟时间，别写进 `assert`（附录 E §E.6
专门列了哪些数字能当断言、哪些不能），延迟只做量级监控。

本教程自己就是这么做的，可以当模板：`tests/` 里的用例给同一份数据
**对拍**（附录 E §E.4 的配对清单），`check_docs.py` 则把"正文里写下
的断言"和"仓库里的事实"对一遍——检索质量评测无非是把这套
"对拍 + 卡基线"的思路从代码搬到质量指标上。

### 25.8.9 损坏与半提交的处置手册

存储引擎会遇到的坏状态分两类，处置方式完全不同。

**半提交（可自愈）。** 一次写事务在"数据文件已 PUT、manifest 未提交"
之间失败，留下孤儿文件。**这类状态不用修**：孤儿不被任何 manifest
引用，读路径根本看不见它，重试一次写就能覆盖（§25.4.1）。唯一的副
作用是占空间，回收它要么等保留窗口、要么用 `delete_unverified=True`
（§25.6.4，危险）。**告警阈值**：孤儿文件的**总字节数**占比超过一个
百分点、或者它随写入次数单调增长，说明有个写入方在反复半途失败——
那是应用层的问题，不是数据层的问题。

**真损坏（不可自愈）。** 文件被截断、footer 魔数不对、页表越界——
本实现的读路径会抛 `CorruptedFileError`（`mini_lancedb/errors.py`）。
真损坏的处置顺序是：一，**立刻停写**这张表（继续写只会让损坏面扩大）；
二，**从最近的备份恢复**（§25.8.4 的演练清单在这里兑现）；三，
**保留现场**（损坏文件不要删），它是唯一的根因证据。**告警阈值**：
任何一次 `CorruptedFileError` 都该当 P0 处理——它几乎不可能是"偶发"，
只可能是磁盘、网络、或写入方三者之一出了系统性问题。

两类状态的区别，是判断"要不要叫人"的关键：**半提交是正常的、可自愈的；
真损坏是不正常的、且几乎一定会扩散。**

### 25.8.10 PII 与数据治理

最后一条，也是最容易在架构期被忽略、上线后被合规部门追着问的一条。
LanceDB 的数据治理有两个特殊性。

**删除要"真的删"，得靠压实。** 删除是软删除（写一个删除文件），数据
文件里那些行还在。所以"用户要求删除我的数据"（GDPR 的
right-to-erasure）在 LanceDB 上**不是一次 `delete()` 能了结的**——
必须 **delete + 压实 + 清理旧版本**，否则被删的行仍然躺在历史片段里
（第 23 章 §23.1 的"幽灵行"）。而且压实要真的压到那些旧片段上，
光删不压是假删除。

**版本历史是合规的敌人。** 时间旅行意味着"三个版本之前的那份数据"
还在盘上。保留窗口（§25.6.1）越长，合规风险越大。所以 PII 表要单独定
一个**更短的保留窗口**，并定期核对"已删除的数据是不是真的不在任何
存活版本里"。

**租户隔离和 PII 是同一件事的两面。** 如果多个租户的数据在同一张表里
靠一个 `tenant_id` 列区分，那么任何一个漏掉 `where` 的查询都是跨租户
泄露。把租户边界放在**表 / 命名空间**这一层（§25.8.3），比放在"每一行
的一个列"上安全得多——前者的默认是隔离，后者的默认是泄露。

## 25.9 小结

**管道的七步是采集、切分、嵌入、写入、索引、查询、维护。**
真正决定成败的是三个决定：主键必须是内容的稳定函数（否则增量
更新变成重复插入）；切分边界要按文档结构而不是固定长度
（否则向量被稀释）；嵌入要有跨进程的持久缓存（否则重建一次
崩一次就重来一次）。

**`merge_insert` 在未索引行超过 10000 时会报错**，
`join` 列上必须有标量索引而且要保持最新。这条错误文本来自
官方文档，**在本机 OSS 0.40.0 上未能复现**（15000 行无索引
反而成功了），结合它是 HTTP 400 这一点，判断为服务端护栏。

**向量自动生成的触发条件是"整批"级的**：向量列不存在、
或者整批全为 null 才会自动编码。部分提供向量会被当成手工数据，
**不回填也不报错**。我们实测的那条路径上，"部分提供"和"全 null"
直接报 `has variable length vectors` / `Invalid null value`。
所以别让"哪一行有向量"变成运行时才知道的状态。

**写是乐观的**：只有在提交 manifest 那一瞬间才可能冲突，
失败方不需要回滚、只需要重试。但**压实和清理必须由单一所有者
做**，多个进程各自 `optimize()` 会互相使绊子。

**进程模型有两条硬约束**：`fork` 和多线程的 Lance 不兼容，
multiprocessing 必须用 `spawn`（POSIX 上可以用 `forkserver`，
Windows 上只有 `spawn`）；没有 AVX2 的老 x86_64 CPU 上
`import lancedb` 会以 `Illegal instruction` 崩掉，要换成
`lancedb-compat`。

**版本不是免费的**：100 个版本有 100 倍的元数据开销，
而元数据每次查询都要读。标签是版本的保护伞，也是忘记清理时
空间一直涨的原因。**别赌默认保留窗口，显式传时间**（机制见第 7 章）。

**`optimize()` 返回 `None`**，尽管类型存根说 `OptimizeStats`；
它的触发规则是"新增或修改 10 万行以上，或者超过 20 次写操作"。
这两件事的完整讨论在第 23 章 §23.6，运维上只关心"什么时候排它、
别塞进写路径"。

**OSS 没有内建缓存**，每次读都要付一次对象存储往返。
在 OSS 上做生产部署，缓存必须自己在应用层做。

**prefilter / postfilter 的选择规则是"命中率低用 prefilter、
命中率高用 postfilter"**（§25.1.6），不确定时用 prefilter。
这条规则容易记反，也容易和"选择性"的措辞混起来。

**安全、加密、多租户、灾难恢复、容量、PII 这些"外围"能力，
OSS 基本都不提供现成的**（§25.8）：权限检查千万别写在客户端里
（那不是安全边界），多租户靠 namespace + 存储层权限拼出来，
静态加密交给存储层和 KMS，而删除要"真的删"必须走压实。
这些能力要么自己建，要么上企业版。

附录 D 把这一路踩过的坑按"现象 / 原因 / 处理"列了一遍，
包括那些"不报错但结果是错的"的静默陷阱。

下一章换个视角：从单个系统的运维，转到**它周围的整个生态**——
和各种框架、工具、语言 SDK 集成起来，哪些是真集成、哪些只是名字撞了。
