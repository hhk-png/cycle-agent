# 附录 G　运行时面：异步、作业与写路径

## G.1 这一篇补的是哪一层

附录 F 讲的是"用户可见的派生表"。这一篇往下走一层，
讲**运行支撑**：一个请求从提交到完成的路上，
除了"读数据、算距离"之外还会经过哪些东西。

这些东西正文里几乎没提，原因和附录 F 一样——它们不属于任何单独一章。
异步接口横跨第 19 章（查询执行）和第 25 章（生产）；
写路径分片属于第 8 章（事务）和第 9 章（upsert）；
克隆压在版本管理上（第 7 章）；预热是索引缓存的一部分（第 24 章）；
而"哪些能力只在云端有"这条边界，是第 25 到 29 章一直在暗示
但从来没有正面列过的一张清单。

这一篇还有第二个目的：**把"说出来有的"和"实际能跑的"分开。**
本教程反复强调"文档说 A、实际是 B"这类发现，
而运行时面这一层恰好是这种落差最密集的地方——
一堆 API 在签名上存在、在文档里有、在本地一调就 `NotImplementedError`。

验证环境照旧：`lancedb 0.40.0` + Python 3.14.6 + pyarrow 26.0.0，
**没有装 `pylance`**。下面每一条都标了是本机实跑还是引用文档。

## G.2 异步接口：一个是真的异步，一个只是同名

0.40 有一整套 `await` 风格的接口：

```python
db = await lancedb.connect_async(uri)
t  = await db.create_table("t", rows)
q  = await t.search([1.0, 0.0])
res = await q.limit(1).to_arrow()
```

实测能跑通，连接返回 `AsyncConnection`，表返回 `AsyncTable`。

**但"异步版就是同步版加个 `async`"这个直觉是错的**，
把两者的签名并排摆出来看一眼就清楚：

```
同步 connect:  api_key client_config host_override manifest_enabled
               namespace_client_impl namespace_client_properties
               namespace_client_pushdown_operations read_consistency_interval
               region request_thread_pool session sql_host_override
               storage_options uri

异步 connect:  api_key client_config host_override manifest_enabled
               namespace_client_properties oauth_config
               read_consistency_interval region session
               sql_host_override storage_options uri
```

`region` / `host_override` / `sql_host_override` 这几个"云端参数"
**同步版一样有**，不是异步版的增量。真正的差别在另一个方向：
**异步版少了三个**——`namespace_client_impl`、
`namespace_client_pushdown_operations`、`request_thread_pool`，
另外多了一个 `oauth_config`。

所以准确的说法是：**两套连接参数是各自演化的，没有包含关系。**
写代码时别按"同步版的参数异步版肯定有"去传，要按异步版的签名对一遍。
这和第 25.5 节讲的进程模型是同一个教训的两种形状：
接口长得像，不等于能力一样。

用的时候有一个坑，实测撞到过：

```python
t.search([1.0, 0.0]).limit(1)     # AttributeError: 'coroutine' object has no attribute 'limit'
await t.search([1.0, 0.0])        # 先 await，拿回来的才是查询构建器
```

也就是 **`search()` 本身是异步的，不是它的终结方法异步**。
这和直觉相反：多数库的设计是"构建同步、执行异步"，
这里是"构建也要 await"。所以 `.limit()` 要写在 `await` 之后。

第二个坑更重要：**`AsyncTable` 和 `Table` 的方法名基本相同，
但参数和返回类型不保证一致。**
它们不是同一个类的两种包装，而是两套并行实现。
跨着用的时候（比如照抄同步版的调用去写异步版），
要按异步版的签名重新对一遍。

## G.3 作业句柄：长任务不该阻塞你

索引构建是个长任务。同步接口会让你等，异步接口给你一个句柄：

```python
job = await t.create_index_async("v", config=lancedb.index.IvfFlat())
print(job)          # <AsyncJob object>，一个句柄
await job.wait()    # 等到它跑完；**不是 await job()**
```

**这里有一个很容易踩的语法坑：`AsyncJob` 本身不能 `await`。**

```python
>>> await job
TypeError: 'AsyncJob' object can't be awaited
```

它是个**普通的对象**，不是 awaitable。"等它完成"要显式调 `wait()`。
这个错误信息其实还算友好，但如果你的代码是照着
"异步接口就是 `await` 它"的直觉写的，会在第一次运行时就撞上。

`AsyncJob` 上真正能用的东西是一组属性和方法：
`wait()` / `refresh()` / `cancel()` / `result` / `state` / `id` /
`job_type` / `events` / `failure` / `creation_ms` / `spec`。

实测本地建索引走一遍：

```python
job = await t.create_index_async("v", config=IvfFlat())
job.id, job.job_type        # (None, None)  —— 还没 refresh 过
await job.wait()            # -> None
job.state                   # 'finished'
job.result                  # None
```

三个细节值得注意。

**第一，`wait()` 的返回值是 `None`，不是"结果"。**
文档对这种作业的说法是"unit-result jobs resolve to `None`"——
建索引这类操作的终态就是"做完了"，没有返回值。
所以别写 `result = await job.wait()` 然后去用 `result`。

**第二，`job.id` 和 `job.job_type` 在 `refresh()` 之前是 `None`。**
句柄是懒加载的，要拿到服务端的状态得先 `refresh()`。
在本地连接上它们可能一直是 `None`——因为**没有服务端注册表**，
也就没有服务端分配的作业 id。这和下面 `list_jobs` 不可用是同一件事的两面。

**第三，`status` 是一个方法，不是属性。**
`job.status` 拿到的是个 bound method，别当成字符串去比较。

`Job` / `AsyncJob` 的语义是"一个可能还在跑的操作的句柄"。
文档写得很准确：**句柄创建的时候，操作可能已经完成了**。
所以你也不该假设 `wait()` 之前它一定在跑——这个抽象有意地把
"已经完成"和"还要等一会儿"统一成同一个形状。

连接上还有一套作业管理接口：`list_jobs` / `open_job` /
`cancel_job` / `pause_job` / `resume_job`。
**本地连接上全部不可用。**

实测 `db.list_jobs()`：

```
NotImplementedError: LanceDBError: not supported: list_jobs is not supported by this database
```

这是个合理的划分：本地进程里的任务，它的生命周期就是你的进程，
不需要一套作业注册表来管理。作业管理是有服务端才成立的抽象。

但要注意它带来的一个后果：**"异步"和"能在本地跑"是两个独立的维度。**
`create_index_async` 本地能跑（它只是一个不阻塞的构建），
`list_jobs` 本地不能跑（它需要服务端的注册表）。
把这两个概念混在一起，就会写出本地跑不通的代码。

## G.4 LSM 写路径：能跑，但姿势很讲究

这是本附录里"文档与实际差距"最密集的一块——不是因为它不能用，
而是因为**能用的前提条件一条都没写在文档的显眼位置**。

第 29.1.3 节把 MemWAL 放在"未来方向"里讲。
实际上 0.40 已经把它做成了一个**可配置的写路径**，
入口是一族 `LsmWriteSpec`：

```python
from lancedb import LsmWriteSpec

LsmWriteSpec.bucket("id", 8)     # 按 unenforced primary key 列做哈希分桶
LsmWriteSpec.identity("id")      # 按某一列的原值分片
LsmWriteSpec.unsharded()         # 不分片，所有写入进同一个 shard
```

三个构造器的语义差别，在两两之间最清楚：

**`bucket` 是哈希分桶**，分片数你指定（上面是 8），
同一个主键永远落在同一个桶里。

**`identity` 是按值分片**，片数由数据决定。它的文档给了一条硬约束，
值得原样记住：那个列**必须是主键的确定性函数**——
"每一行只要主键相同，就永远产生同一个 `column` 值，
否则这个键的 upsert 可能落到不同分片，于是一个陈旧的版本可能胜出。"
最后半句是重点：**分片错了不会报错，只会让旧数据赢。**

**`unsharded` 是不分片**，所有 `merge_insert` 进同一个 shard。
它的价值在于"先别引入并发复杂度"，是调优的起点。

装配和读取都是表上的方法：

```python
t.set_lsm_write_spec(LsmWriteSpec.bucket("id", 8))
t.get_lsm_write_spec()
```

这里实测到一个**没写在文档里但很合理的行为**：
装上去之后再读回来，`maintained_indexes` 被自动填上了表上已有的索引名。

```python
set 前： LsmWriteSpec.bucket(column="id", num_buckets=4, maintained_indexes=None, ...)
set 后： LsmWriteSpec.bucket(column="id", num_buckets=4, maintained_indexes=["v_idx"], ...)
```

对照官方那句"A fresh spec maintains every index the MemWAL..."就很清楚了：
**新装的 spec 默认接管表上所有索引的维护**。不想让它接管，
就用 `.with_maintained_indexes([...])` 显式指定。
还有一个 `.with_writer_config_defaults({...})` 用来调写入器参数。

现在说落差。**落差不在"能不能跑"，而在"跑起来之后你会得到什么"——
这一点必须先把对照组立起来，否则很容易把两件事的责任搞反。**

先看对照组：一张普通的表，什么都不配，`merge_insert` 是好好的。

```python
t = db.create_table("t", rows, schema=S)          # 什么都不加
t.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(P)
# MergeResult(version=2, num_updated_rows=1, num_inserted_rows=1,
#             num_deleted_rows=0, num_attempts=1, num_rows=2)
```

**版本号、更新数、插入数、重试次数，全都是有意义的。**记住这个形状，
因为下面每配一样东西，这个返回值都会变一次。

**第一道坎：装了 LSM spec 就必须有 unenforced primary key。**

```
RuntimeError: merge_insert: table has a MemWAL index but no unenforced primary key
```

那就去设一个 `t.set_unenforced_primary_key("id")`。**但它会先失败一次**，
哪怕你在建表时明确写了 `nullable=False`：

```
RuntimeError: lance error: LanceError(Schema): Primary key column and all its
ancestors must not be nullable: Field(id=0, name=id, type=int64)
```

**这里有一条顺序上的硬约束，它决定了整个配方能不能跑通：
`alter_columns` 必须在 `set_lsm_write_spec` 之前做。**

如果顺序反了（先装 spec、再改非空），会得到另一条报错：

```
ValueError: Invalid input, cannot make a column non-nullable on a table with a
MemWAL attached: the check runs against the base table, and a write admitted
into the WAL while it runs is not there to be checked. Drop the MemWAL first.
```

中间那半句是**理由**，值得读两遍：非空检查是对着**基表**跑的，
而在它跑的同时被收进 WAL 的那次写入不在基表里，检查看不到它。
**换句话说，这条限制不是懒得实现，是"检查会漏掉并发写入"这件事实在
逼着它必须禁止。**报错最后也把出路说出来了：要么先
`unset_lsm_write_spec()` 把 MemWAL 摘掉，要么一开始就把顺序排对。

它不是可以绕开的小毛病——它意味着"给一张已经在用 LSM 的表补一个主键"
是做不到的，必须停机摘掉再装回去。

所以配 LSM 的完整顺序只有一种：

```python
t = db.create_table("t", rows, schema=S)
t.alter_columns({"path": "id", "nullable": False})   # 1. 先把列改成非空
t.set_unenforced_primary_key("id")                   # 2. 再设主键
t.set_lsm_write_spec(LsmWriteSpec.bucket("id", 4))   # 3. 最后装 MemWAL
```

顺序里的第 1 步和第 2 步之间也有讲究——**先 alter 再设 PK**，
因为设 PK 时就会检查非空。这三步是串行的，没有交换律。

下面说第 2 步为什么会失败：

```python
t = db.create_table("t", rows, schema=pa.schema([pa.field("id", pa.int64(), nullable=False), ...]))
t.schema.field("id").nullable   # -> True
```

**`create_table` 把 `nullable` 抹掉了**——建出来的表里所有字段都是可空的。
所以"我在 schema 里声明了非空"这句话在 LanceDB 里**不成立**。
这就是上面那条三步顺序里第 1 步必须存在的理由：
第 2 步（设主键）会当场检查非空，而建表时写的那句声明早就没了。

`alter_columns` 自己也会返回一个版本号
（`AlterColumnsResult(version=2)`），因为改 schema 就是一次提交。

**第二道坎，也是最反直觉的一条：设了 unenforced PK 之后，
不用 LSM 的那条普通路径反而坏了。**

实测把上面每一步单独拆开跑，结果是这样一张表：

| 表上的配置 | 普通 `merge_insert` |
|---|---|
| 只有表本身 | 正常，`version=2, updated=1, inserted=1, attempts=1` |
| `+ alter_columns(nullable=False)` | 正常，`version=3, updated=1, inserted=1, attempts=1` |
| `+ alter` + `+ set_unenforced_primary_key` | **`RuntimeError`：非空校验失败** |
| `+ alter` + `+ PK` + LSM `unsharded()` | 能跑，但**计数器全零** |
| `+ alter` + `+ PK` + LSM `bucket(id, 4)` | 能跑，**计数器全零** |
| `+ alter` + `+ PK` + LSM `bucket(id, 1)` | 能跑，**计数器全零** |
| `+ alter` + `+ PK` + LSM `bucket(id, 8)` | **`ValueError`：一次调用只能打一个分片** |

读法：**`set_unenforced_primary_key` 这一下，本身就改变了 `merge_insert`
走哪条路径。**设了它、又不装 LSM spec，就会撞上那个非空校验——
明明 schema 里 `nullable=False`，`t.schema` 也说是 `False`，它照样报错。
所以"设了 PK 就万事大吉"是错的，**PK 和 LSM spec 是一套的，
要么都设，要么都不设**。

**第三道坎：走 LSM 之后，`MergeResult` 的计数器全归零。**

```python
MergeResult(version=0, num_updated_rows=0, num_inserted_rows=0,
            num_deleted_rows=0, num_attempts=0, num_rows=2)
```

数据是对的（`id=3` 被改成 30.0，`id=9` 插了进来），但**`version=0`，
四个计数器全是 0**，唯一非零的是 `num_rows`。

这是整节最需要记住的一条：**计数器归零是 LSM 路径特有的行为，
不是"LanceDB 的 merge 都这样"。**对照组那张表已经证明了普通路径
会给你有意义的数字。所以如果你的代码用 `num_inserted_rows` 做监控，
**它在你把 LSM 打开的那一刻会静默失效**——不报错、不警告，
只是一夜之间所有写入指标变成 0，而数据其实写进去了。

顺带一个副作用：返回的行序也变了（原本 `[1,2,3,4]`，
现在是 `[3,9,1,2,4]`）。这和附录 D 里"`merge_insert` 会改变行的顺序"
是同一条，只是在 LSM 下更明显。

**第四道坎：一次 `merge_insert` 只能打一个分片。**

`bucket(id, 8)` 那张表报的是：

```
ValueError: Invalid input, merge_insert: input row 0 hashes to bucket 3 but
row 1 hashes to bucket 7; each merge_insert call must target a single bucket
(pre-shard the input, or set validate_single_shard(false) to route by the
first row only)
```

也就是说，分片不是"引擎内部帮你拆"的——**调用方得自己按分片把输入分组**，
每组一次调用。报错原文给的两条出路也是这个意思：要么自己预分片，
要么把 `validate_single_shard` 关掉（关掉之后**只按第一行的分片路由**，
其余行被塞进同一个分片——官方显然不建议默认这么做）。

而 `bucket(id, 8)` 在**两次单行**调用下会撞上另一条：

```
ValueError: Invalid input, merge_insert: a shard writer is already open for
shard 1d8d46d4-1ffd-5f6e-9aea-466d5ea76fdb but this input routes to shard
8bbfeae7-9aba-5077-b693-c16562a7058b; call close_lsm_writers before writing
to a different shard
```

**写完一个分片要 `close_lsm_writers()` 才能写下一个。**实测调一次
`close_lsm_writers()` 之后再重试，同一行就写进去了。
这不是可选的清理动作，是**协议的一部分**——它揭示了 LSM 写路径的真实形状：
**写入器是按分片常驻的**，你要么持续写同一个分片，要么显式收尾。

**第五道坎：维护动作在本地全部不可用。**

```
flush_lsm      -> NotImplementedError: not supported: flush_lsm is not supported on this table type
checkpoint_lsm -> NotImplementedError: not supported: flush_lsm is not supported on this table type
compact_lsm    -> NotImplementedError: not supported: compact_lsm is not supported on this table type
get_lsm_stats  -> NotImplementedError: not supported: get_lsm_stats is not supported on this table type
```

注意 `checkpoint_lsm` 报的是 **`flush_lsm`** 的名字——复制粘贴的痕迹，
但它恰好说明这两条走的是同一个没有实现的入口。
`close_lsm_writers()` 返回 `None`（能调，而且是必须调的）；
`unset_lsm_write_spec()` 也能用，调完之后 `get_lsm_write_spec()` 回到 `None`。

所以这一节的结论要改一个说法：

**LSM 的配置面在本地完整可用；写入路径能跑，但要用对姿势
（PK 和 spec 一起设、按分片分组、分片之间 `close_lsm_writers()`）；
维护面（flush / checkpoint / compact / stats）全部只在服务端存在。**

第 29.1.3 节把它当"未来方向"，从"能不能配置"的角度确实过时了。
但**把它当成"本地可用的写入加速"也是不对的**——
在维护面缺席的情况下，你写进去的分片没有正常的手段去合并和观测。
这一条更准确的定位是：**配置已经就位，运维还没有。**

## G.5 `take_row_ids` 和 `take_offsets`：两种"第几行"

这是两个容易被当成同义词的方法，实测差别很大。

准备一张 5 行的表（id 1 到 5），**删掉 id=2**，然后分别取前四个。
注意两者返回的都是 `LanceTakeQueryBuilder`，要 `.to_arrow()` 才拿到行：

```python
t.take_row_ids([0, 1, 2, 3]).to_arrow()     # -> id 1, 3, 4      （3 行）
t.take_offsets([0, 1, 2, 3]).to_arrow()     # -> id 1, 3, 4, 5   （4 行）
```

差别来自两者数的不是同一样东西：

**`row_id` 数的是物理存储位置，包括已经被删除的行。**
删掉的那一行仍然占着它的 row_id（这里是 1），
所以按 `[0,1,2,3]` 去取，row_id=1 取不到东西，只回来 3 行。

**`offset` 数的是当前版本里的存活行。**
删掉一行之后，剩下的 4 行重新排成 offset 0 到 3，
所以 `[0,1,2,3]` 正好取满 4 行。

这个区别在什么场景下会咬人？**分页。**
如果你把 `offset` 当成稳定标识存进外部系统，
那么下一次有人在前面删了一行，你存的 offset 就整体错位一格。
`row_id` 相对好一些，但也**不是稳定标识**——
官方对它的原话是"row ids are not stable and are relative to the current
version of the table"，压实和更新都会让它变。

**真正稳定的那个东西是稳定行 id**（附录 F 讲的
`new_table_enable_stable_row_ids`），而这两个接口都不用"稳定行 id"这个语义。

所以把它们当"随机访问"用是安全的，当"引用"用是不安全的。

## G.6 克隆：共享数据文件，独立的 manifest

`clone_table` 的签名是：

```python
clone_table(target_table_name, source_uri, *,
            target_namespace_path=None, source_version=None,
            source_tag=None, is_shallow=True)
```

注意第一个参数是**目标**，第二个才是**源**——
顺序和直觉相反，很容易写反。

它的语义值得完整说一遍，因为它是第 7 章那套"不可变数据文件 + manifest"
最直接的产物：**浅克隆建出一张新表，它和源表共享底层数据文件，
但有自己的 manifest。**

这个设计的意思是：**克隆几乎不花存储**。数据文件是共享的
（因为它不可变，共享是安全的），复制的是那份几百字节到几 KB 的 manifest。
两张表从这一刻开始**各自独立演进**——你往克隆里加数据、删数据，
源表毫发无损，反过来也一样。

`is_shallow=True` 是默认值。传 `False` 会做深拷贝，
把数据文件也复制一份，代价立刻上去了。

另外两个参数是版本那一侧的钩子，正好对应第 7 章的两个概念：
**`source_version` 按版本号克隆，`source_tag` 按标签克隆。**
这意味着"克隆"这个动作本身就是一次时间旅行——
你可以从一张表三个月前的某个版本拉出一个分支来做实验。

这里必须报告一个**本机跑不通**的事实，因为它很容易浪费你半小时：

```python
db.clone_table("clone1", "src")
# RuntimeError: lance error: Dataset at path .../src was not found:
#               Not found: .../src/_versions
```

它把 `source_uri` 当成**相对当前工作目录的文件系统路径**来解析，
而不是当成连接里的表名。实测验证的方式是换个目录再跑：
把 cwd 切到数据库目录（也就是 `src.lance` 所在的那一层）之后，
传 `"src.lance"` 就成功了。

也就是说，**`clone_table` 在本地连接上，`source_uri` 要写成
"相对 cwd 的、带 `.lance` 后缀的目录名"，而不是表名。**
这个行为和"表名优先"的直觉不符，本地用之前先试一次。

还有一条**容易误判成上面那个问题的报错**。实测同样的 cwd 下：

```
目标名是新的 + "./src.lance"   -> OK
目标名是新的 + "src.lance"     -> OK
目标名重复用（比如再克隆一次到 "c1"）
    -> RuntimeError: lance error: Encountered internal error. Please file a
       bug report ...: Clone operation should not enter build_manifest.
```

**`"./"` 前缀本身没有问题**——从这个对照能看出来，只要目标名是新的，
两种写法都能成功。真正触发那条"内部错误"的是**目标表名已经存在**：
引擎撞上冲突之后没走冲突处理，而是掉进了一条不该走到的构建路径。

所以 `clone_table` 的正确用法要多加一条纪律：
**每次克隆用一个没被占用过的目标名**；目标名冲突时报的不是
"表已存在"那种可读的错误，而是一条让你去报 bug 的内部错误。

## G.7 预热：把冷启动挪到前面

`prewarm_index(name)` 的语义是"提前告诉引擎这个索引马上要用，
把它读进内存"。实测它在本地**能跑**：对一张有 `tag_idx` 的表调用，
返回 `None`，不抛异常。索引名对不上的时候报错很直接：

```
RuntimeError: lance error: Lance index not found: name=nope
```

`prewarm_data(columns=None)` 在本地**不能跑**：

```
NotImplementedError: LanceDBError: not supported: prewarm_data is currently only supported on remote tables.
```

这里有一个值得注意的不对称：**索引预热本地能跑，数据预热本地不能跑。**
这和 G.3 节刚讲的 `create_index_async` / `list_jobs` 是同一类划分——
判断"某个调优手段在本地能不能用"，不能按"它听起来像不像本地的事"来猜。

按第 24 章那套调优框架来看，预热的定位很明确：它**不改变查询的
算法复杂度**，只改变第一次查询要付多少 I/O。所以它属于"把尖峰摊平"
这一类手段，和缓存大小、`nprobes` 那些真正改变复杂度的旋钮不是一个层级。

不过要说明一个覆盖缺口：**第 24 章本身没有讲预热**，
它的 §24.8"还没量到的几个旋钮"里也没有这一条。
所以上面这个定位是**用第 24 章的框架推出来的**，
不是那一章已有的结论——它在本章第一次出现。

衡量预热有没有用，要看的指标是**冷启动的首查延迟**，
不是平均延迟——平均延迟会被后续的缓存命中稀释掉。
而本教程**没有量过预热到底省了多少**（本地能调 `prewarm_index`，
但本机没有一套能区分冷热缓存的测量装置）。这条属于"接口确认过、
收益没量过"，用之前请在自己的数据上测一次。

## G.8 `serialize()`：连接是可以序列化的

这是一个小功能，但它解决第 25.5 节那个进程模型问题：

```python
print(db.serialize())
```

实测输出：

```json
{"connection_type": "local",
 "uri": "C:\\...\\demo",
 "storage_options": null,
 "manifest_enabled": false,
 "namespace_client_properties": null,
 "read_consistency_interval_seconds": null}
```

它是一个 JSON 字符串，描述了"要重建这个连接需要什么"：
类型、uri、存储选项、命名空间属性、读一致性间隔。

**它的用途是把连接配置传给另一个进程。**
第 25.5 节讲了 fork 和 Lance 不兼容、讲了 `EmbeddingFunction`
不跨进程——那些问题的共同形状是"子进程需要重建父进程的上下文"。
`serialize()` 给出的就是这个上下文里**可以被序列化的那一部分**。

注意"可以被序列化的那一部分"这个限定：它只包含连接层面的配置，
**不包含注册表**（嵌入函数、重排器都注册在进程内存里）、
也不包含任何缓存状态。所以它能解决配置传递，解决不了注册表传递——
后者在第 25.5 节里已经说了，要在子进程里重新注册。

## G.9 一张清单：哪些是开源版没有的

这一节把散落在各处的 `NotImplementedError` 集中起来。
下面每一条都是本机在**本地连接**上实测的结果，不是从文档推断的——
**唯一的例外是 `create_function` 那一条**，它引用的是 docstring 原文
（本地确实也抛 `NotImplementedError`，但"服务端打包 OCI 镜像"
这半句是从文档来的，本机无从验证）。`connect_catalog` 那一条
只引用官方的适用范围说明，不是报错实测——本地连它的失败方式
其实和"云端专属"无关（先撞上的是 URL 校验）。

报错文本这里都做了压缩，**完整原文都带
`NotImplementedError: LanceDBError: not supported: ` 这个前缀**，
别按截断后的字符串去 `except` 匹配。

**连接层的名字解析与注册表**

`create_view` / `list_views` / `describe_view` / `drop_view`：
`View operations are not supported by this database`。
（附录 F 讲过：物化视图本地能跑，普通视图不能。）

`rename_table`：`rename_table is not supported in LanceDB OSS`。

`create_secret` / `list_secrets` / `describe_secret`：同名报错。
密钥是为了让表能访问外部 URI 上的数据（第 21.4 节讲 blob 的 External 档时
会遇到这个需求），它需要一个服务端来保管。

**SQL**

`execute_query` / `describe_query`：
`SQL is not supported for this connection type`。
`SqlQuery` 的类型文档第一句就是"handles to SQL queries running on a
**remote** database"。所以 LanceDB 的 SQL 是远端专属的能力。

**用户定义函数**

`create_function` 的文档写得很明确：
"Build and register a scalar Python UDF... The server builds the OCI image
and registers the completed artifact. **Local connections raise
`NotImplementedError`.**
"——注意"the server builds the OCI image"这半句：
它的实现方式是**把你的函数打成容器镜像**再注册。
`lancedb.udf` 装饰器 + 一个 `UdfDefinition` 是配套的入口。

**作业管理**

`list_jobs` / `cancel_job` / `pause_job` / `resume_job` / `open_job`：
同名报错。

**Catalog**

`connect_catalog` 连的是"an HTTP(S) server's root catalog"，
文档原话："Local filesystem and object-store catalogs are not supported."

**一个容易误判的**

`namespace_client()` 在本地抛的**不是** `NotImplementedError`，而是：

```
ValueError: Failed to construct namespace impl lance.namespace.DirectoryNamespace:
No module named 'lance'
```

原因是 **`pylance` 没装**。这条和第 0 章开头那个提醒是同一件事：
本机的验证环境缺 `pylance`，所以凡是走它那一层的功能都会以各种
不同的错误形状失败，而不是统一地报"不支持"。
`list_namespaces()` 反而能返回（空列表）。

**这张清单的正确用法**不是"知道哪些不能用"，
而是**在选型阶段就把它们从你的方案里划掉**。
如果你的架构里有一步依赖本地视图、本地 UDF 或者本地密钥管理，
那它就不是"以后再优化"的问题，而是**现在就不成立**。

## G.10 小结

这一篇的结论压成八条。

**异步是一套并行实现，不是同步版的包装。**
`search()` 本身要 `await`（不是它的终结方法异步）；`AsyncJob`
**不能直接 `await`**，要写 `await job.wait()`；`AsyncTable` 和 `Table`
的签名不保证一致。照着同步版抄、或者按"异步就是 await 它"的直觉写，
都会在第一次运行时就出错。

**"异步"和"本地能跑"是两个独立的维度。**
`create_index_async` 本地能跑，`list_jobs` 不能——
判断某个能力能不能用，要一条一条试，不能按感觉归类。

**LSM 写路径能跑，但姿势很讲究，而且计数器会静默归零。**
配置面（`LsmWriteSpec` 三种分片）完整可用；写入要用对姿势——
四步的顺序是**固定的**：`alter_columns(nullable=False)` →
`set_unenforced_primary_key` → `set_lsm_write_spec` → 写入。
**`alter_columns` 必须在装 MemWAL 之前做**，否则报
"cannot make a column non-nullable on a table with a MemWAL attached...
Drop the MemWAL first"——也就是**没法给一张已经在用 LSM 的表补主键**。
另外：只设主键不装 spec 会让普通 `merge_insert` 反而报非空校验错
（两者要一起用）；一次调用只能打一个分片；分片之间要
`close_lsm_writers()`；维护面全部只在服务端。

**最要命的一条：计数器归零是 LSM 特有的。**
同一张表不配 LSM 时，`MergeResult` 给的是
`version=2, updated=1, inserted=1, attempts=1`；一旦开了 LSM，
就变成 `version=0` 加四个 0。**它不报错，只是在你把开关打开的那一刻
让所有写入指标归零**——数据是写进去了，监控看到的是零。

**`create_table` 会抹掉 `nullable`。**
"我在 schema 里声明了非空"这句话在 LanceDB 里不成立，
实测 `t.schema.field("id").nullable` 是 `True`。
依赖非空的机制（比如 unenforced primary key）必须先 `alter_columns`。

**`row_id` 和 `offset` 数的不是同一样东西。**
前者含已删除的行，后者只数存活行。两者都不稳定，
不能当持久引用；能当引用的只有附录 F 讲的稳定行 id。

**云端与开源版的边界要正面列出来。**
视图、密钥、UDF、SQL、作业管理、catalog 在本地全部不可用，
而且失败的形状不统一（多数是 `NotImplementedError`，
`namespace_client` 却是因为缺 `pylance` 而抛 `ValueError`）。
选型时把这一层划清楚，比性能调优重要得多。

**本地 `clone_table` 的 `source_uri` 是"相对 cwd 的路径"，不是表名。**
要写成带 `.lance` 后缀的目录名。另外**目标表名必须是新的**——
重名时报的是一条"请去提 bug"的内部错误，不是"表已存在"。
