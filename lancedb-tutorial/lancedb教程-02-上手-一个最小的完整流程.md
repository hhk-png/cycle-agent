# 第 2 章　上手：一个最小的完整流程

这一章用一个具体的例子把 LanceDB 的主要能力过一遍：
建表、写数据、建索引、三种检索、增删、版本管理、维护。
每一段代码都真的在我们这台机器上跑过，输出是实测结果。

本章用的是这份教程自带的迷你实现 `mini_lancedb`。
它的 API 是照着真实 `lancedb` 0.40.0 的形状设计的，
所以把 `import mini_lancedb as ldb` 换成 `import lancedb as ldb`
基本就能跑真实版本——差异会在每一节里点明。

## 2.1 连接

```python
import numpy as np
import mini_lancedb as ldb

db = ldb.connect("./my_db")     # 一个目录就是一个"数据库"
```

连接对象不持有任何后台进程、连接池或守护线程。它只是"一个目录的句柄"。
`connect()` 不做任何 I/O——第一次真正读盘是在 `open_table()` 或
`create_table()` 的时候。这一点和"客户端连服务器"的数据库完全不同，
也是"无服务器"这个说法的字面含义。

URI 的形态决定存储后端：本地路径走文件系统，
`memory://` 走内存（测试用），`s3://` / `gs://` / `az://` 走对象存储。
在真实 LanceDB 里，对象存储这条路需要装对应的 extras 并配好凭证。

## 2.2 建表：数据从列表开始

```python
docs = [
    ("Why vector search matters",     "semantic retrieval over embeddings"),
    ("Columnar file formats",         "Parquet versus Lance layout"),
    ("Approximate nearest neighbour", "IVF PQ and HNSW tradeoffs"),
    ("Versioning and time travel",    "immutable files and a version manifest"),
    ("Full text search",              "BM25 scoring over an inverted index"),
    ("Hybrid retrieval",              "BM25 plus embeddings and reranking"),
]
rng = np.random.default_rng(42)
rows = [
    {
        "id": i,
        "title": t,
        "text": body,
        # 真实场景这里是嵌入模型的输出，这里用固定种子的随机向量代替
        "vector": rng.standard_normal(64).astype("float32").tolist(),
    }
    for i, (t, body) in enumerate(docs)
]

tbl = db.create_table("notes", data=rows)
```

`create_table` 会做三件事：从数据推断 Arrow schema、把数据写成一个
不可变的数据文件、提交版本 1 的 manifest。它返回一个 `LanceTable`：

```
LanceTable(name='notes', version=2, rows=6, columns=[id, title, text, vector])
```

注意 `version=2`——因为 `create_table` 内部是"先建空表提交版本 1，
再把数据写成版本 2"。这不是实现细节，而是**所有写操作都是提交一个新版本**
这个设计原则的直接体现。真实 LanceDB 的 `create_table` 也会留下类似的痕迹。

向量列的类型是推断出来的：上面那行列表里的 64 个浮点数会被识别成
`fixed_size_list<float64>[64]`。**注意是 float64，不是 float32**——
哪怕你在上游 `.astype("float32")` 过，`tolist()` 会把它们变回 Python
的 `float`，而推断规则是"只要出现浮点就上 float64"（不去猜你要哪个精度）。
想要 float32 得显式建 schema 指定，这是第 5 章 §5.6 的主题。

**向量不是一种特殊的列类型**，它就是一个定长浮点列表——
这个设计带来的后果，第 5 章会展开讲。

`create_table` 的 `mode` 只有两个取值：`create`（默认，表已存在就报错）
和 `overwrite`（覆盖）。**没有 `append`**——追加是 `add()` 的事。
真实 LanceDB 也是这个语义，传第三个值会得到一个
`ValueError: mode must be either 'create' or 'overwrite'`。

**schema 也可以显式给。** 上面这份表是从数据推断出来的，但生产里更常见的是
先把 schema 写死再灌数据：`db.create_table("notes", schema=..., data=rows)`，
`schema` 可以是 `pyarrow.Schema`、本实现的 `Schema`，或者一串 `Field`。
推断建表在真实系统里有个具体的风险——列类型由第一批数据决定，之后想改就
必须走 schema 演进；显式 schema 还顺手解决了"空表没有列结构可推"这件事
（`create_table(name, schema=sch)` 出来就是 version 1、rows 0 的空表）。
类型系统本身是第 5 章的内容，这里只记住入口。

## 2.3 建索引

建索引之前先想清楚要查什么。这个表里有两种查询需求：
按语义找（向量）和按关键词找（全文）。

```python
tbl.create_index("text", config="FTS", name="fts_idx")
tbl.create_index("vector", config="IVF_PQ", name="v_idx",
                 num_partitions=2, num_sub_vectors=8)
```

两个细节值得注意。

**全文索引是必须的。** 不建 FTS 索引就做全文检索，真实 LanceDB 会直接报
`ValueError: ... unless an INVERTED index has been created`；
我们的实现报 `没有可用的全文索引`，并附上建索引的提示。
这不是"没优化"，而是"没这个能力"——倒排索引不做，全文检索根本无从谈起。

这里正好放一个最小的错误处理。所有异常都继承自 `MiniLanceError`，
抛出的对象里带一组结构化上下文（`hint` 就是修复动作），所以这样写就够了：

```python
try:
    tbl.search("hello", query_type="fts").limit(3).to_list()
except ldb.MiniLanceError as exc:
    print(f"{type(exc).__name__}: {exc}")
    # IndexNotFoundError: 没有可用的全文索引，无法做全文检索
    #   (columns=['id', 'title', 'text', 'vector'], hint="先建索引：table.create_index('text', config='FTS')")
```

一个容易踩的点：查询是**惰性**的，`search(...)` 本身不碰数据，错误要到
`to_list()`（或 `to_arrow()` / `to_pandas()`）真正执行时才抛出。把 `try`
只包在 `search(...)` 那一句上，是这类 API 最常见的空转写法——什么都拦不到。
后面章节反复强调的"不报错但结果错"是另一类问题：这里会抛异常的都还算好，
真正麻烦的是那些静默返回错误结果的路径。

**建索引在真实 LanceDB 里有两个入口，别把它们的语义搞混。**
`create_index()` 是**阻塞的**：它在本地表上建完（或者等到
`wait_timeout` 用尽）才返回。本机 0.40.0 实测，3000 行 32 维的
`IvfPq` 建了 **0.15 秒**，返回 `None`，紧接着 `list_indices()`
就能看到它覆盖了全部 3000 行。真正异步的是另一个方法：

```python
job = tbl.create_index_async("v", config=IvfPq(num_partitions=4, num_sub_vectors=8))
# 0.001 秒返回一个 Job 对象
job.wait()          # 想等就等；也可以轮询 job.state / job.events
```

所以"`create_index()` 是异步的、调完立刻返回、要再调 `wait_for_index()`"
这个流传很广的说法**不对**：那个行为属于 `create_index_async()`。
第 12 章 §12.6 会把这条实测和三档 `wait_timeout` 的行为一起展开。

这件事为什么值得在快速上手里就点破：对大数据集，建一个 IVF_PQ 可能要
几十分钟，**如果以为 `create_index()` 会立刻返回，你会把"阻塞了几十分钟"
误当成"卡死了"**；反过来，如果以为 `create_index_async()` 建完了，
后续查询就会静默地退化成暴力检索。我们的迷你实现只有同步的那一档，
但保留了 `index_stats()` 里的 `num_unindexed_rows` 字段——
它是"索引还没覆盖多少行"的度量，也是判断"后台建完了没有"的依据。

还有一条和版本有关的副作用：**建索引也是提交一个新版本**。上面两次
`create_index` 把表从 v2 推到了 v4，所以 §2.6 那个打在 v4 上的 tag 才能自洽。

## 2.4 三种检索

**向量检索。** 拿一个查询向量找最近的几行：

```python
tbl.search(rows[2]["vector"]).limit(3).to_list()
```

返回的是一个**字典列表**（不是元组列表），每行带上该行的所有列
外加一个 `_distance`：

```
[{'id': 2, 'title': 'Approximate nearest neighbour', '_distance': 1.546, ...},
 {'id': 1, 'title': 'Columnar file formats',          '_distance': 74.873, ...},
 {'id': 4, 'title': 'Full text search',               '_distance': 90.759, ...}]
```

（上面的 `...` 是省略掉的 `text` 和 `vector` 两列——`to_list()` 默认把
所有列都带回来，只想看标题要显式 `.select(["title", "_distance"])`。）

第一条的距离是 1.546，第二条直接跳到 74.873——因为查询向量就是第 3 行的向量，
所以它自己距离接近 0（这里是 1.5，因为经过 PQ 量化，返回的是**近似**距离）。
**记住这一点**：索引建好之后 `_distance` 不再是精确距离。
真实 LanceDB 的文档有一张专门的表讲这件事，
第 12、19 章会展开。想要精确距离可以调 `.bypass_vector_index()`。

**全文检索。** 先建索引再查：

```python
tbl.search("inverted index", query_type="fts").limit(3).to_list()
# -> ['Full text search']
```

注意返回的分数列叫 `_score` 而不是 `_distance`，而且**越大越好**
（BM25 的语义）。方向和向量检索相反，这是混合检索里最容易出错的地方。

**混合检索。** 两路一起跑、再用重排器融合：

```python
tbl.search(rows[5]["vector"], query_type="hybrid") \
   .text("reranking embeddings").limit(3).to_list()
# -> ['Hybrid retrieval', 'Columnar file formats', 'Why vector search matters']
```

注意 `search()` 的第一个参数是**向量**，检索词通过 `.text()` 单独传——
这和真实 LanceDB 的接口一致。默认的重排器是 RRF（倒数排名融合），
它只用排名不用分数，所以不需要把两路的分数归一化到同一个尺度。

**纯标量查询。** 不传查询向量就是一次普通的过滤扫描：

```python
tbl.search().where("id >= 3").limit(10).to_list()
```

这条路径完全不碰向量索引，走的是第 10 章讲的列扫描 + 谓词下推。

**投影与分页。** `limit()` 上面已经用过，和它同层的是 `select()` 与 `offset()`：

```python
tbl.search().select(["id", "title"]).limit(2).offset(2).to_list()
```

`select()` 是**投影下推**：只要不选向量列，那一列的数据页根本不会被读——
第 3 章会把省下多少字节实测出来。`offset()` 跳过前 N 条结果用来翻页，
注意它的语义是"先取回、再丢掉"，和真正的键集分页不是一回事。

**结果落地。** 上面三类检索都以 `to_list()` 收尾，但它不是唯一的出口。
真实 LanceDB 的出口有六个，**分工一半在"落成什么类型"、一半在"要不要一次拿完"**：
`to_list()` 出 Python 字典列表；`to_arrow()` 出 `pyarrow.Table`；
`to_pandas()` / `to_polars()` 出 DataFrame（没装就报 `ImportError`
并给出安装命令，**不静默退回 pandas**）；`to_batches()` 出
`pyarrow.RecordBatchReader`——**这个是流式的、可以边拉边处理**，
还能传 `batch_size=` 控制分段粒度（附录 I.5 量过这个参数）；
`to_pydantic(Model)` 出模型实例列表（第 5.7 节）。

**"要不要一次拿完"这一半更值得记**：`to_arrow()` 会等结果全部到齐，
`to_batches()` 不会——它给你一个游标，迭代到哪算到哪。
结果集很大、或者你想早停的时候，这个区别就是峰值内存的区别。
第六个 `to_df()` **已经弃用**（`to_df is unsupported as of 0.4.0`），
用 `to_pandas()`。

除了 `to_list()`，其余几个都需要装了 `pyarrow`（`to_pandas()` 还额外要
pandas、`to_polars()` 要 polars）——迷你实现的硬依赖只有 numpy，
Arrow 是**可选的**。整表导出走 `tbl.to_arrow()` / `tbl.to_pandas()`，
那是**不带 limit** 的全量导出。

## 2.5 增、删、改

```python
tbl.add([{"id": 6, "title": "Streaming ingestion",
          "text": "batch writes and compaction",
          "vector": rng.standard_normal(64).astype("float32").tolist()}])
tbl.delete("id = 0")
```

（脚本里在 `add` 之前还有一行 `tbl.tags.create("v0.1")`，给当时的当前版本
v4 打了个 tag；它属于版本与时间旅行，留到 §2.6 讲。）

`add` 的 `mode` 只有 `append`（默认）和 `overwrite`。
`overwrite` 的语义值得强调：它**不删任何数据文件**，
只是让新版本指向另一批片段。老版本仍然可读，空间由 GC 回收。
`add` 只覆盖"追加"和"整表覆盖"两种；要按主键做"有则更新、无则插入"，
得用 `merge_insert`——那是第 9 章的主题。

`delete("id = 0")` 是纯元数据操作——它不为被删的行写任何数据，
只写一个删除文件记录"片段 X 的第 0 行没了"。
所以删 1 行和删 100 万行的成本几乎一样。代价是数据文件里那一行还在，
读的时候要过滤掉，这就是第 9 章要讲的"幽灵行"问题。

删除之后：

```
rows 6 version 6
```

原本 6 行，加了 1 行变成 7 行，删了 1 行回到 6 行，版本号到了 6。

## 2.6 版本与时间旅行

tag 是在 §2.5 那两步增删**之前**打的，所以它指向 v4，而不是删除之后的版本：

```python
tbl.tags.create("v0.1")
...
tbl.checkout("v0.1")
```

`checkout` 是**就地**把表切到某个历史版本（不是返回一个新对象）：

```
checkout 到 v0.1 之后： version 4 rows 6
restore 之后：          version 7 rows 6
```

版本号自洽：`v0.1` 打在 v4 上，而 v5、v6 是它之后的 `add` 和 `delete`，
所以 `checkout("v0.1")` 回到的是还没被增删过的那一版，`rows 6`。
这也顺便说明 tag 记的是"打 tag 那一刻的版本"，不会跟着后续写入走。

`restore()` 不是"回退指针"：它把 v0.1 的内容**作为一个新版本**（v7）
提交回来。所以 v4、v5、v6 都还在，回滚本身也可以被回滚。

真实 LanceDB 的 `checkout(version)` 语义相同——这也是为什么它有一个单独的
`restore()`。如果你不确定当前表被 checkout 到哪了，看 `version` 属性。

## 2.7 维护

```python
stats = tbl.optimize()
```

`optimize()` 的名义职责有三件：压实小片段、清理旧版本、重建失效的索引。
**但默认只做第一件。**我们的实现签名是
`optimize(*, cleanup_older_than=None, delete_unverified=False, retrain=False)`：
两个 `None` / `False` 的默认值意味着后两件都是**显式选项**，
不传就不做。所以上面那次调用跑完，报告里只有【压实】一段——
这不是报告漏打，是真没做。

真实 LanceDB 的 `optimize()` **返回 `None`**（尽管类型标注说是
`OptimizeStats`，实测是 `None`）；我们返回一个真正有内容的
`OptimizeStats` 对象，因为教程里需要看到"到底做了什么"。

官方把 `optimize()` 比作数据库的 `VACUUM`，并给了一个触发启发式：
累计 10 万行变更、或者 20 次修改操作，以先到者为准。
它不该在写入路径上调用——第 23 章会讲为什么以及代价有多大。

## 2.8 从头到尾的完整脚本

上面的片段拼起来是一个可以直接跑的脚本，放在
`examples/01_quickstart.py`。跑法是：

```
cd lancedb-tutorial
python -X utf8 examples/01_quickstart.py
```

在 Windows 上加 `-X utf8` 是必要的：控制台默认是 GBK 编码，
而 `explain()` 之类的输出里有中文，不加这个参数会直接抛
`UnicodeEncodeError`。这不是库的问题，是 Windows 控制台的默认编码问题。

## 2.9 真实 LanceDB 的对照

把这份教程的迷你实现换成真实库，改动很小：

```python
import lancedb
from lancedb.index import FTS, IvfPq

db = lancedb.connect("./my_db")
tbl = db.create_table("notes", data=rows)
tbl.create_index("text", config=FTS())
tbl.create_index("vector", config=IvfPq(num_partitions=2, num_sub_vectors=8))
tbl.search(vec).limit(3).to_list()
```

三处差异要知道：

**索引配置必须是对象，字符串会直接报错。** 真实 LanceDB 0.40.0 的新签名是
`create_index(column, config=IvfPq(distance_type="l2"))`，
旧的 `create_index("l2", vector_column_name=...)` 签名已经废弃。
本机实测，传字符串的结果是：

```python
tbl.create_index("text", config="FTS")
# TypeError: config must be an instance of IvfSq, IvfPq, IvfRq, HnswPq,
#            HnswSq, BTree, Bitmap, LabelList, ...
```

报错里那份"合法类型"清单本身就值得读一遍，因为**它的顺序没有任何规律**：
实测的完整原文是

```
TypeError: config must be an instance of IvfSq, IvfPq, IvfRq, HnswPq, HnswSq,
           BTree, Bitmap, LabelList, Fm, or FTS, but got <class 'str'>
```

不是字母序（`IvfSq` 排在 `IvfPq` 前面），也不是分类别（向量索引和
标量索引混在一起、`FTS` 被扔在最后），而且前十项里**
`IvfFlat` 根本不在列**——尽管 `create_index(index_type=...)`
这个老参数是接受 `'IVF_FLAT'` 的（附录 A §A.5 和 §A.7 把这两个
不同的"合法清单"分开放了）。所以想确认自己该用哪个类，
别按这个报错的顺序找，去查 §A.5。

我们的实现接受字符串（`"FTS"` / `"IVF_PQ"`）也接受对象，
是为了少一层概念——但**这意味着同一段代码在两边不通用**：
本实现里跑得通的 `config="FTS"`，换到真实库上会直接 `TypeError`。

**几个方法带弃用警告。** `create_scalar_index`、`create_fts_index`、
`compact_files`、`cleanup_old_versions`、`table_names` 在 0.40.0 里
都会发 `DeprecationWarning`——它们正在被新接口取代。

**`cleanup_old_versions` 没有 `dry_run` 参数。** 真实版本传它会直接
`TypeError`。我们的实现提供了它，纯粹是为了教学方便。

## 2.10 小结

这一章建立的直觉是：**LanceDB 的每一个写操作都是"提交一个新版本"。**
建表是、追加是、删除是、更新是、建索引也是。
这是后面所有章节的共同前提。

下一章开始往下钻，从"文件里到底有什么"讲起。
