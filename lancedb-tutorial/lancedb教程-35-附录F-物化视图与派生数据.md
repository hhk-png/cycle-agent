# 附录 F　物化视图与派生数据

## F.1 这一篇补的是哪一层

正文从第 3 章到第 11 章讲的是"数据怎么落盘、怎么改"，
第 12 到第 19 章讲的是"怎么找得快"，
第 23 到第 25 章讲的是"怎么活下去"。

这三段里有一个位置始终是空的：**派生结果**。

所谓派生结果，指的是"内容由另一张表算出来、但物理上自己存一份"的表。
全文检索的倒排索引算一种（第 17 章），向量索引也算一种（第 12 到 16 章），
但它们是**引擎内部的加速结构**，用户看不见，也不该直接查。

还有另一种派生结果，它是**用户可见、可被当作表来查的**：
把清洗过的行挑出来存一份、把长文本切段之后存一份、
把去重之后的向量存一份。这一层在 0.40 里叫**物化视图（materialized view）**。

这一篇把它讲清楚，顺带把三个同源的机制一起讲完：计算列、字段元数据、
以及和它配套的稳定行 id。它们共同回答同一个问题——
**"这份派生数据，什么时候重算、谁来重算、重算的代价是多少"。**

正文里没有它们的篇幅，不是因为它们不重要，而是因为它们全部落在
"用户可见的表"这一层，和第 7 章（版本）、第 8 章（事务）、
第 23 章（维护）是交叉的。放在任何单独一章里都会显得突兀，
所以收在这里。

关于验证环境，先说一句要紧的：环境是 `lancedb 0.40.0` + Python 3.14.6
+ pyarrow 26.0.0，**本机没有装 `pylance`**（就是第 0 章说的那个坑），
而物化视图走的是 `lancedb` 打包的原生模块，**不需要 `pylance`**，所以能跑通。
这一点很关键：它意味着物化视图不是"云端专属"，本地文件系统上就能用。

**本篇的数字分两类，读的时候要分清。**绝大多数是**本机实跑**得到的，
凡是标了"实测"的都是；但有两处是**引用官方文档**——F.7 里
`vector_dedup` 的去重策略（逐句拆的那一段），
以及 F.11 里引用的 OSS 限制说明。它们不是从本机跑出来的，
所以按"引用"读：**策略描述可能和实现的真实行为有出入**，
而这一层本机没有可用的条件去逐条验证（需要一个覆盖全部片段的
合格索引，以及构造出边界样例）。

## F.2 硬前提：稳定行 id 不是可选项

第一个实测结果就是一条硬门槛。

建一张普通的表，然后直接建物化视图：

```python
import lancedb, pyarrow as pa
db = lancedb.connect("./demo")
schema = pa.schema([
    pa.field("id", pa.int64(), nullable=False),
    pa.field("tag", pa.string(), nullable=True),
])
db.create_table("plain", [{"id": 1, "tag": "a"}], schema=schema)
db.create_materialized_view("mv", "plain", where="id > 3")
```

报错是：

```
ValueError: Invalid input, materialized views require stable row ids on the
source table; create 'plain' with storage option
new_table_enable_stable_row_ids=true
```

这条报错本身就是最好的文档：它把**要求**（源表必须有稳定行 id）、
**时机**（建表时）、**参数名**（`new_table_enable_stable_row_ids`）
三件事一次说全了。按它改：

```python
db.create_table(
    "src",
    rows,
    schema=schema,
    storage_options={"new_table_enable_stable_row_ids": "true"},
)
```

之后物化视图就能建了。

现在回头看第 7.8 节和第 29.1.4 节，会发现它们对稳定行 id 的判断需要一处修正。

第 7.8 节说的是"没开这个开关的表，`_rowid` 就等于行地址本身"，
这没错。第 29.1.4 节把它归入"未来方向"，理由是迁移成本高、
索引侧还是实验性的，所以"当成现在就能开的东西不是"。

**这个结论对"为了索引"这个用途是对的，但漏掉了一个反例：
物化视图今天就已经强制要求它。**

也就是说，如果你的表要建物化视图，
`new_table_enable_stable_row_ids=true` 不是"要不要付这个成本"的选择题，
而是**唯一的一条路**。第 29.1.4 节列的迁移代价（删索引、停写、不重试）
仍然全部成立，只是"它是不是未来"这个判断，在物化视图这个场景下
已经有了确定答案。

这里有个值得记住的规律：**一个底层机制从"实验"走到"能用"，
最快的路径往往是先被某个上层功能强制依赖。**
稳定行 id 就是这样被拉进主线的。

## F.3 定义、落盘形态与那个隐藏列

建好之后，先看它到底存了什么：

```python
mv = db.create_materialized_view("mv1", "src", select=["id", "tag"], where="id > 3")
print(mv.definition)
print(mv.table.to_arrow().to_pylist())
```

输出：

```
MaterializedViewDefinition(query='SELECT id, tag FROM src WHERE id > 3')
[{'id': 4, 'tag': 'even', '__source_row_id': 3},
 {'id': 5, 'tag': 'odd',  '__source_row_id': 4},
 {'id': 6, 'tag': 'even', '__source_row_id': 5}]
```

三件事值得注意。

**第一，定义被规范化成了一条 SQL 字符串。**
你写的是 Python 关键字参数（`select=`、`where=`、`limit=`），
落盘的是 `SELECT ... FROM ... WHERE ...`。
参数到 SQL 的映射是直接拼接，所以 `limit=3` 会变成 `LIMIT 3`：

```python
db.create_materialized_view("mvlim", "src", select=["id"], where="id > 1", limit=3)
# MaterializedViewDefinition(query='SELECT id FROM src WHERE id > 1 LIMIT 3')
```

一个过滤条件都不给会怎样？它会退化成全量拷贝，
`MaterializedViewDefinition(query='SELECT id, v, tag FROM src')`，
行数和源表一样。这不算错，但要知道自己付的是全量的存储成本。

**第二，多出来一列 `__source_row_id`。**
它的值是**源表里的稳定行地址**：源表第 4 行给出 `__source_row_id = 3`，
也就是**从 0 开始数的物理偏移**。这正是第 7.8 节讲的
`(fragment_id << 32) | offset` 那一套东西落到用户可见层的样子。

这一列是整件事的关键。有了它，刷新的时候才能回答
"这一行在源表里现在还是第几行、有没有被搬走、有没有被删掉"。
没有它，每次刷新就只能全量重算——那样物化视图就只是个缓存，不是个结构。

**第三，定义本身只是一段字符串，没有类型检查。**
`MaterializedViewDefinition` 这个 dataclass 只有一个字段 `query`，
它不会在你建视图的时候告诉你说"`tag` 这列不存在"。
错误要等到真正执行（或者刷新）的时候才爆出来。
这是这一层目前比较粗的地方，用它的时候别指望早期报错。

## F.4 刷新：三种模式，和一个会骗人的计数器

物化视图最容易被人误解的地方是**它不会自己变**。

实测：建完视图之后往源表里加两行，源表 8 行，视图还是 3 行。
必须显式调 `refresh()`：

```python
r = mv.refresh()
print(r)
```

```
RefreshMaterializedViewResult(mode=incremental, rows_written=2,
                              source_version=2, version=5)
```

**`mode=incremental`，只写了 2 行。**这是物化视图和"重建一张表"
最本质的区别：它靠 `__source_row_id` 做差分，只补变化的那些行。

刷新一共会给出三种模式，实测都撞到了：

**`no_op`** —— 源表没变，什么都不做。而且注意它**不产生新版本**：
连着刷两次，两次都是
`mode=no_op, rows_written=0, source_version=1, version=3`，
版本号停在 3 不动。这个设计是对的，但它意味着
"刷了一次"和"版本号变了"不是一回事，别拿版本号当刷新计数用。

**`incremental`** —— 有变化，做增量。

**`rebuild`** —— 全量重算。它**不是**"改动大到一定程度自动降级"，
试过把 100 行**全部**更新一遍（`rows_written=100`）以及把源表**全部删空**，
两次返回的都还是 `incremental`。**它由调用方显式指定**：

```python
>>> db.open_materialized_view("mv").refresh(full=True)
RefreshMaterializedViewResult(mode=rebuild, rows_written=4,
                              source_version=1, version=5)
```

还有一条**自动**走到 `rebuild` 的路径：用 `with_no_data=True`
声明出来的视图，第一次 `refresh()` 就是全量——

```python
>>> db.create_materialized_view("mv2", "src", with_no_data=True)
>>> db.open_materialized_view("mv2").refresh()
RefreshMaterializedViewResult(mode=rebuild, rows_written=4,
                              source_version=1, version=3)
```

这在逻辑上说得通：声明式建的视图还没有任何已填充的行，
也就没有"增量"可言，只能全量算一次。

**模式名要写对。**这一条值得单独强调，因为按 `mode` 分支是
本节自己教的判断口径：名字写成 `full` 的代码会在真机上永远走不到那个分支，
而它不会报错——只是"全量刷新"这条路静默失效。
以 `RefreshMaterializedViewResult` 实际给出的字符串为准：只有
`no_op` / `incremental` / `rebuild` 三种。

现在说那个会骗人的计数器。

源表里删掉一行（`t.delete("id = 2")`），再刷新：

```
RefreshMaterializedViewResult(mode=incremental, rows_written=0,
                              source_version=2, version=5)
```

**`rows_written=0`，但删除确实传播过去了**——视图里的 `id=2` 不见了。

原因很直白：删除不写新行，它只是让某一行不再出现，
所以"写了多少行"这个计数器对删除天然是 0。
但它同时又报了 `mode=incremental`，说明引擎知道有事情发生。

**所以 `rows_written` 不能当"这次刷新有没有干活"的判断依据**，
它只统计**新增和重写**的行数。判断有没有活干，要看 `mode`。

对照着看源表里的**更新**：

```python
t.update(where="id = 3", values={"tag": "CHANGED"})
db.open_materialized_view("mvall").refresh()
# RefreshMaterializedViewResult(mode=incremental, rows_written=1,
#                               source_version=3, version=7)
```

`rows_written=1`，改过的行被重写了。三个计数器合起来看，
"新增算 1、更新算 1、删除算 0"，这个口径要记住。

## F.5 生命周期与"先声明、后填充"

物化视图的日常操作只有四个方法，都挂在连接上：

```python
db.create_materialized_view(...)     # 建
db.open_materialized_view("mv1")     # 打开
db.list_materialized_views()         # 列
db.drop_materialized_view("mv2")     # 删
```

实测 `list` 返回名字的列表，`drop` 之后名字就没了。
`open` 拿回来的对象和 `create` 返回的是同一个类型，都有
`.name` / `.definition` / `.table` / `.refresh()`。

还剩下一个参数没用过：`with_no_data=True`。

它的语义是**只声明、不填充**——建完立刻查，`count_rows()` 是 0。
然后调一次 `refresh()` 才会把数据算出来。

这个模式解决的是一个很实际的问题：**冷启动**。
如果视图很大，`create_materialized_view` 会阻塞到算完为止。
声明模式让你先把结构（和它的名字）占住，把填充推迟到你自己的调度里去。

它和 `create_materialized_view_async` 是**两种不同的异步化思路**，
而且差别比名字看起来大：

`create_materialized_view_async` 是**真不等**——调用立刻返回一个作业句柄，
此刻表**还不存在**：

```python
>>> job = db.create_materialized_view_async("mvasync", "src")
>>> job
Job(not refreshed)
>>> db.open_materialized_view("mvasync")
ValueError: Table 'mvasync' was not found
>>> job.wait()          # 等它跑完（注意 Job 用 wait，不能直接 await）
>>> db.open_materialized_view("mvasync").table.count_rows()
4
```

`with_no_data=True` 是**先建一个空壳、再同步填充**：调用返回时表已经
存在且可查（只是 0 行），填充的时机完全由你决定。

选哪个取决于你要的是什么：要"立刻能引用这个名字"就用 `with_no_data`；
要"立刻把控制权还给我、管它建没建完"就用 `_async`。

## F.6 物化视图是一张真表

这是最容易低估的一点。**物化视图本身就是一个 Lance 表**，
有自己的目录、自己的 manifest、自己的版本链。

实测一张视图的版本链。做法是：源表 4 行，建一个 `where="id > 1"` 的视图，
然后往源表加一行、刷一次、再刷一次（这次是空转）。全程版本链是：

```
建视图之后        v1(rows 0)  v2(rows 3)  v3(rows 3)
源表 add 之后     （不变，视图不自动跟随）
第一次 refresh     v4(rows 4)  v5(rows 4)
第二次 refresh     （不变，no_op 不产生版本）
```

读法很清楚：

- **v1 是声明**（`total_rows = 0`）——带数据的 `create` 也要先声明再填，
  所以它**占三个版本**，不是两个；
- v2 把数据填进去，v3 是紧跟着的一次收尾提交；
- 源表 `add` 之后视图**纹丝不动**；
- 一次 `refresh` 占**两个**版本（v4 写入、v5 收尾）；
- 第二次 `refresh` 返回 `mode=no_op`，**一个版本都不占**。

把这些数字记下来的价值在于：**物化视图的版本开销是常数级的、
而且和"刷了多少行"无关。**只补了 1 行也要占 2 个版本。
所以高频刷新的视图会以两倍于刷新区间的速度堆出版本，
第 23 章的压实和保留策略对它同样必要——而它的版本链是**独立于源表**的，
清源表不管用。

这个事实有几个直接后果，都值得单独说：

**它可以被时间旅行。**第 7 章讲的那套东西全部适用——
`checkout` 到旧版本、按标签读、对比两个版本的差异，
对物化视图和对普通表一模一样。

**它也会被 `cleanup_old_versions` 影响。**第 23 章讲的保留策略
同样适用。视图的版本链和源表的版本链是**两条独立的链**，
清理的时候要分别考虑，不能只盯源表。

**它的存储是真实成本。**一个"只选两列"的视图不是零成本的视图：
数据文件是实实在在写出来的，和普通表走同一套片段机制。
所以第 23 章那套写入欠债模型（小片段变多、读放大上升、
需要压实）对它**同样适用**，而且因为它的版本链独立，
清理和压实要**当做两张表分别排期**。

这一条要说清证据强度：**上面的版本链只给了行数，没给文件数和字节数**，
所以"压实对它同样必要"是从同构性**推**出来的，不是量出来的。
要真下结论，得在自己的数据上数一遍视图目录下的片段数——
那是个确定性、可复现的量，比行数更能说明问题。

## F.7 vector_dedup：把索引、版本和视图绑在一起

物化视图的 `source` 不只能是表名，还可以是一个**表函数**。
0.40 里现成的只有一个，但它恰好把前面十几章的东西全串起来了。

`vector_dedup` 的签名是：

```python
vector_dedup(source, *, column, distance_threshold, dataset_version=None)
```

它的作用是**按向量距离去重**：把互相靠得太近的向量只留一个。

最值得读的是它的默认策略，官方文档写得很细，值得逐句拆：

- **保留快照里最小的那个未分配行 id**，只删掉它的**直接邻居**；
- 一个 A-B-C 的链，如果 A 和 C 之间没有边，那么 A 和 C 都留着——
  也就是说它**不做传递性合并**，只做局部去重；
- **没有配对的行走掉删除**，包括向量是 null 的行；
- **只比较同一个片段、同一个 IVF 分区内的行**；
- 量化距离用的是**重建出来的索引向量**，不是原始向量；
- **索引必须覆盖所选快照**。

最后一条是硬要求，实测撞到过：

```python
db.create_materialized_view("dedup", vector_dedup("t", column="v", distance_threshold=0.01))
```

```
RuntimeError: Error during planning: vector index on column 'v' does not
cover all fragments; optimize the index first
```

报错给的动作很明确：先 `optimize()` 把索引覆盖到所有片段。
这条依赖关系是本附录里最值得记的一条工程事实——
**一个物化视图能不能建，取决于一个向量索引的状态**。

把这句话展开，就是第 12 到 18 章和第 7、23、29 章的交叉点：

分区内的比较（第 13 章）、用重建向量算距离（第 14 章的 ADC）、
索引覆盖片段才有效（第 18.6 节讲的主索引 + delta）、
以及"先压实再建索引"的顺序（第 23 章）。

`dataset_version` 参数则是版本那一侧的钩子：
`None` 表示"建的时候抓当前版本"，传一个正整数表示
"复现某个历史快照"。这就是第 7 章的时间旅行，
只不过用在了建视图这一个动作上。

一个使用上的限制：**`vector_dedup` 不能和 `select` / `where` / `limit` 一起用。**
它输出的是源表原始的行和列。要做进一步变换，得先建这个视图，
再对视图这张表下查询。

## F.8 计算列：`add_columns` 的两种语义

物化视图是"整表级"的派生。还有一个"列级"的派生，就是计算列。

`add_columns` 的表面签名是
`add_columns(transforms=None, *, computed=None)`，
两个参数长得像，语义差得很远。

**`transforms` 是立即求值**：

```python
t.add_columns({"dbl": "id * 2"})
# AddColumnsResult(version=2)
```

结果是每一行立刻算好，`dbl` 分别是 2、4、6。

**`computed=` 是声明式、延迟求值**：

```python
t.add_columns(computed={"sq": "dbl * dbl"})
# AddColumnsResult(version=3)
```

建完之后去看数据，**`sq` 整列都是 `None`**。要等到调了
`refresh_column` 才会填上。

还有两个细节必须记住。

**第一个：`transforms` 吃的是 SQL 字符串，不是表达式对象。**
本机试过传 `Expr`（就是后面附录 G 要讲的那个类型安全表达式），
报错是 `TypeError: argument 'definitions': 'Expr' object is not an
instance of 'str'`。列定义的这一层还没有跟上表达式构建器那一层。

**第二个，也是更重要的：写入不自动维护计算列。**
往表里 `add` 一行新数据，那一行的 `dbl` 和 `sq` **全是 `None`**。
而且此时对 `sq` 调 `refresh_column` 返回 `rows_filled=0`——
因为 `sq` 的输入 `dbl` 本身还是空，引擎没有东西可算。

也就是说：**计算列的"自动维护"只覆盖它自己能推出的依赖链，
推不出来的部分要你自己按顺序补。**先补 `dbl`，再补 `sq`。

顺带一提，本机跑 `add_columns` 的时候，Rust 侧会打出一条警告：

```
WARN lance_table::transaction::manifest_build] Building manifest with
DataReplacement operation. This operation is not stable yet, please use
with caution.
```

计算列的落盘走的是 `DataReplacement` 这个 manifest 操作，
官方自己标注了"还不稳定"。用它是可以的，但要知道脚下这块地还没夯实。

## F.9 `refresh_column`：补算与重算

上一节的 `refresh_column` 值得单独看，因为它同时干两件事。

它的文档描述是"填充一个计算列里没填的行，并重算那些输入变了的行"。
两件事是分开的：

**补算**指"有行没有值，把它算出来"。声明一个 `computed` 列之后
第一次刷新就是补算。
实测 `refresh_column("sq")` 返回
`RefreshColumnResult(rows_filled=3, version=5)`——填了 3 行。

**重算**指"行的值有了，但它的输入变了，重算一遍"。

返回值只有 `rows_filled` 和 `version` 两个字段，
**没有任何字段告诉你"重算了多少行"**。所以如果你改了一个底层列，
再去刷上层计算列，`rows_filled` 可能是个小数字，
但实际的工作量要看它重算了多少行——而那个数字它不报。

和 F.4 节的 `rows_written` 是同一类问题：
**这个家族里的计数器，都只统计它们各自关心的那一部分，
不要把它们当成通用的"干了多少活"指标。**

## F.10 字段元数据：`update_field_metadata`

派生数据讲完了，还剩一个"标注"的需求：
给一个字段挂上说明，让下游知道这列是什么、谁生成的。

第 5 章讲过 schema 里有字段元数据这一层。0.40 把它做成了可写的：

```python
t.update_field_metadata({"path": "txt", "metadata": {"lancedb:description": "正文"}})
# UpdateFieldMetadataResult(version=2)
```

三个细节：

**`path` 是点分路径**，可以指向嵌套字段里的一层（文档给的例子是
`"a.b.c"`），不只是顶层列。

**值传 `None` 就是删掉这个键**。实测给一个存在的键传 `None`
之后再读，那个键就没了。所以"改"和"删"共用同一个入口。

**它是一次提交**，返回值里带着新的版本号。
这很合理——元数据是 schema 的一部分，schema 是 manifest 的一部分，
改 manifest 就是写一个新版本。它便宜（不动数据文件），
但它不是"免费"的（每次都新增一个版本，第 25.6 节讲的版本保留策略要算上它）。

顺带说一个已弃用的近亲：`replace_field_metadata(field_name, new_metadata)`
是**整体替换**语义，0.33.1 起弃用，改用 `update_field_metadata`。
两者的区别是"替换整个字典"和"按键增删改"，新接口更精确。

## F.11 视图不是物化视图，而且在 OSS 里没有

名字很像，是两件不同的事。

**视图（view）** 只存定义，不存数据，查的时候现算。
**物化视图** 存数据，需要刷新。

连接上有完整的一套视图 API：`create_view` / `drop_view` /
`list_views` / `describe_view`。

**但在开源版里它们全部不可用。**四个方法逐条实测，报错文本完全相同
（下面是压缩过的写法，完整原文前面还带 `LanceDBError: not supported: ` 前缀）：

```
create_view   -> NotImplementedError: ... View operations are not supported by this database
drop_view     -> NotImplementedError: ... View operations are not supported by this database
list_views    -> NotImplementedError: ... View operations are not supported by this database
describe_view -> NotImplementedError: ... View operations are not supported by this database
```

`drop_view` 值得单独列出来，因为它是最容易被漏掉的一个：
这个名字看起来像"本地清理"，很容易让人以为它总能用。
四个一起不可用，说明这一整层都要靠服务端。

这和物化视图形成了有意思的对照：**物化视图（更重的那个）本地能跑，
普通视图（更轻的那个）本地不能跑。**

原因不难猜：物化视图是"一次算好、存成一张真表"，
不需要查询期的元数据服务，本地文件系统扛得住。
普通视图要在查询期做名字解析和定义展开，
那需要一个在线的 catalog——也就是附录 G 会讲的云端那一层。

所以看到"LanceDB 支持视图"这种说法时，先问一句：
**是物化视图还是普通视图，是在 OSS 还是在 Cloud。**
这两问不澄清，"支持视图"这四个字是没有信息量的。

## F.12 迷你实现为什么没有这一层

`mini_lancedb/` 里没有物化视图，也没有计算列和字段元数据的写接口。
这不是遗漏，是一个有意的边界，理由有三条。

**第一，物化视图的硬前提我们在索引那一侧就砍掉了。**
第 7.8 节说过，本实现在压实之后把索引一律标记失效
（`clone.valid = False`），走的是"重建"而不是"重映射"这条路。
没有行血脉（row lineage），就生不出稳定的 `__source_row_id`，
物化视图的增量刷新也就无从谈起。这是一个**连锁的取舍**：
省掉了行血脉，就等于同时放弃了增量物化视图。

**第二，字段元数据的行为在附录 A 里已经列了。**
`Schema` / `Field` 都带 `metadata`，读是有的，写没有——
因为写它要新增一个提交路径，而它对教学主线没有增量价值。

**第三，计算列在迷你实现里可以直接用表达式求值模拟。**
第 11 章的表达式引擎本身就能对每一行求值，
`add_columns` 的那点语法糖可以用三行代码在应用层做掉。
"框架替你维护"这件事才是计算列的真正价值，而那需要后台任务调度，
不在本实现的范围里。

所以本附录的正确读法是：**它是正文的"下一层"，
而不是正文里那些结论的反例。**前面讲的取舍一条都没被推翻，
只是真实产品在那些取舍之上又盖了一层。

## F.13 小结

这一篇的结论可以压成五条。

**物化视图有一个硬门槛：源表必须开稳定行 id。**
这不是可选项，报错原文会直接把 `new_table_enable_stable_row_ids=true`
这个参数名告诉你。这也修正了第 29.1.4 节"稳定行 id 还是未来"的判断——
至少对物化视图这个用途，它已经是今天必须付的成本。

**刷新有三种模式，名字是 `no_op` / `incremental` / `rebuild`。**
`incremental` 只写变化的行；`no_op` 什么都不做**并且不产生新版本**；
`rebuild` 全量重算，由 `refresh(full=True)` 显式触发，
声明式建的视图第一次刷新也会走它。
**按 `mode` 分支的代码要用这三个字符串**——写成 `full` 不会报错，
只是那条分支永远走不到。
另外 `rows_written` 只统计新增和重写，**删除永远是 0**——
判断有没有活干要看 `mode`，不能看计数器。

**物化视图是一张真表。**
它有自己的版本链（建一次占三个版本，刷一次占两个，空转不占），
能被时间旅行，也会被清理策略和压实影响。存储不是免费的。

**`vector_dedup` 暴露了一条硬依赖：视图能不能建，取决于索引的状态。**
"索引必须覆盖所有片段"这个报错，把第 13、14、18、23 章串成了一条线。

**普通视图和物化视图是两回事，而且前者在 OSS 里没有。**
物化视图本地能跑，普通视图不能——这恰好说明了它们对
基础设施的要求差在哪。

这一篇讲的全是"用户可见的派生表"。下一层是"用户看不见的运行支撑"：
异步接口、作业句柄、写路径分片、缓存预热，
以及一条贯穿始终的边界——**哪些能力只在 Cloud/Enterprise 里有**。
那部分收在附录 G。
