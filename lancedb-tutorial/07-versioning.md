# 第 7 章　版本 manifest 与时间旅行

如果只能从 Lance 的设计里保留一个思想，我会保留这个：
**一个数据集里唯一"活的"东西是版本 manifest，其它一切都是不可变的。**

数据文件写完就不再改，删除文件写完就不再改，索引文件写完就不再改。
"表的当前状态"完全由 manifest 描述：哪些片段组成这个版本、
每个片段有哪些数据文件、哪些行被删了、有哪些索引。

这一章讲清楚这个设计能带来什么、代价是什么，以及它在磁盘上长什么样。

## 7.1 目录布局

一个 dataset 目录的完整形态是这样的：

```
mytable.lance/
  _versions/
    18446744073709551614.manifest      <- 版本 1（注意文件名是倒着的，见下）
    18446744073709551613.manifest      <- 版本 2
    18446744073709551612.manifest      <- 版本 3
    latest_version_hint.json           <- {"version": 3}
  _transactions/
    1-8f3c2a9d4e5b6c7d.txn             <- "我基于 v1 做了 append"
  _refs/tags/
    v1.0.json                          <- {"version": 3, "manifest_size": 1263}
  data/
    3f9a8b....lance                    <- 不可变的数据文件
  _deletions/
    7-2-9c1e....bin                    <- 删除文件
  _indices/
    vector_idx/index.idx
    fts_idx/meta.lance
    btree_idx/page_data.lance
```

每个目录都有明确的所有者：

`_versions/` 是唯一的权威。manifest 文件的**存在性**就是"这个版本已提交"的证明，
内容里的版本号必须和文件名一致（我们的 `read_manifest()` 会校验这一点，
不一致直接报 `CorruptedFileError`）。

`_transactions/` 是操作日志。它记录"意图"而不是"状态"：
**谁、在什么时候、基于哪个版本、做了什么操作** —— 对应 `Transaction` 上的
`writer` / `timestamp` / `read_version` / `operation`（操作的细节在 `payload` 里）。
它**不权威**：就算丢了或被覆盖也不影响正确性，所以可以用更便宜的方式写
（普通 `put` 而不是条件写）。文件名是 `{read_version}-{uuid}.txn`，
天然不冲突。**它没有任何 GC**：本实现里事务文件只增不减，
`cleanup_old_versions` 也不碰它（它只清 manifest、数据文件和删除文件），
所以一个提交 N 次的表会在 `_transactions/` 下留 N 个文件。
真实 Lance 同样把事务文件排除在 manifest 的引用图之外，靠保留窗口成批清理。

`_refs/` 是标签和分支。它们的内容小到可以忽略：就是一个版本号。

`data/`、`_deletions/`、`_indices/` 是所有不可变数据的存放地。

## 7.2 文件名为什么要"倒着数"

真实 Lance 把版本 v 的 manifest 命名为：

```
_versions/{u64::MAX - v:020}.manifest
```

也就是 `18446744073709551615 - v`，补零到 20 位。于是 v=1 是
`...614.manifest`，v=2 是 `...613.manifest`——**数字越大，文件名越小**。

这不是行为艺术，是为了让一次 `LIST` 就能拿到"最近的 N 个版本"。

对象存储的 LIST 是按 key 字典序返回的。如果文件名是正序（`1.manifest`、
`2.manifest`、……、`10.manifest`），要取最新版本就得把整个目录列出来再排序——
版本多的时候（一个大表可能有几千个版本），这一次 LIST 的返回量就是几千条元数据。
倒序命名之后，"列出最近 10 个版本"只需要读 LIST 结果的前 10 条，
**返回量从 O(所有版本) 降到 O(N)**，而且不需要读任何一个文件的内容。

我们的 `manifest_filename()` 实现了同一套规则，`list_manifest_versions()`
也就顺理成章地不需要排序——字典序本身就是时间倒序。

## 7.3 `latest_version_hint.json`：一个刻意的缓存

每次打开表都去 LIST 一遍太贵，所以 Lance 额外维护一个小文件
`latest_version_hint.json`，内容就一句 `{"version": 3}`。

**它是缓存，不是权威。** 这个区分很关键，因为对象存储上的 LIST
是最终一致的：你刚写进去的 manifest 可能还没出现在 LIST 结果里。
如果拿 hint 当权威，就会读到过期的版本。

我们的 `try_read_latest()` 把两者的关系处理得很明确：

```python
hint = read_latest_hint(store)
if hint is not None and store.exists(manifest_path(hint)):
    return read_manifest(store, hint)      # 快路径：1 次 GET
versions = list_manifest_versions(store)   # 兜底：LIST
...
```

也就是"**先信 hint，但要用 manifest 是否存在来验证它**"。
如果 hint 指向的 manifest 不存在（比如 GC 删掉了），就走 LIST 兜底，
并顺手把 hint 修正回来。

真实 Lance 在 hint 存在时的做法更进一步：读到 hint 里的版本 v 之后，
对 v+1、v+2……依次发 HEAD 探测，直到第一个 `NotFound` 为止 ——
这样比完整 LIST 一次便宜得多。如果 hint 与实际差得太远
（版本号跨度超过 `MAX_HINT_PROBE_GAP = 1000`），就放弃探测、退回完整列目录。

## 7.4 时间旅行是零成本的

这是"不可变 + manifest"最直接的回报。`checkout(v)` 的实现只有一行：

```python
return Dataset.open(self.store, self.uri, version=version)
```

它做的事就是读另一个 manifest 文件。数据文件全都还在，
所以读 v3 和读 v7 走的是**完全相同的代码路径**，
只是加载的 manifest 不同。

我们实测一个真实的例子。建表、写 500 行、删掉 `id < 100`：

```
v1    2026-10-17 00:29:05  rows=0    fragments=0  deleted=0
v2    2026-10-17 00:29:05  rows=500  fragments=1  deleted=0
v3    2026-10-17 00:29:05  rows=400  fragments=1  deleted=100
```

现在打一个标签 `v1.0` 指向 v3，然后 checkout：

```python
ds.create_tag("v1.0")            # TagInfo(version=3, manifest_size=1263)
old = ds.checkout("v1.0")
old.num_rows                     # 400
```

注意 `manifest_size` 是 1263 字节。**一个版本的"全部状态"就是 1.2 KB。**
100 个版本就是 126 KB 的元数据——数据不重复，但元数据是 100 份。

这个观察很重要，因为它是"版本不是免费的"的全部原因：
**100 个版本不是 100 份数据，但有 100 倍的元数据开销。**
每次查询都要读一份 manifest，manifest 越大、查询越慢。
官方文档里也点明了这一点：版本多会让查询变慢。

## 7.5 标签、分支与它们的差异

标签（tag）就是 `_refs/tags/{name}.json`：

```json
{"version": 3, "manifest_size": 1263, "created_at": 1792168145.74}
```

它的作用有三层。

**第一是给人看的锚点。** "把生产环境固定在 `v1.0`" 比 "固定在版本 37" 可读得多。

**第二是给 GC 的保护。** `cleanup_old_versions` 在决定删哪些版本时，
会先看有没有标签引用它。

**这一条曾经是错的，而它是整本书里代价最高的一个 bug**，
所以值得完整讲一遍——包括为什么测试全绿还是漏了它。

当时的实现把"哪些版本要留"算完之后，才回头去看标签：

```python
keep_set = set(versions[:keep_versions]) | {v for v in versions if v not in removable}
live_files = set()                      # <- 从 keep_set 推出"哪些数据文件还活着"
for v in sorted(keep_set):
    ...
referenced_tags = {int(t["version"]) for t in self.list_tags().values()}   # <- 太晚了
```

后果不是"标签不起作用"，而是**比不起作用更坏的一种**：

- 标签指向的版本，它的 **manifest 被保住了**（后一行会把它从 `removable`
  里摘掉），所以清理报告里会大大方方地写着"保留的版本"；
- 但它的**数据文件和删除文件已经被当成垃圾回收掉了**——因为
  `live_files` 是在看标签之前算的，压根没把那批文件算进去。

于是：清理成功返回，没有任何一步报错。失败发生在几天之后，
有人想回到那个快照的时候：

```
t.checkout("release-1")
# StoreError: 对象不存在
#   (path='t.lance/_deletions/5325787448008566032-2-621f1f35e99b48ae.bin')
```

**这是"标签被当成发布保险、而它保不住自己的数据"的完整形状。**

正确的顺序只有一种：**先算 `keep_set`（含被标签保护的版本），再由它推出
`live_files`，最后才回收。**顺序一换，语义就反了。修法是把这个 bug
的根因直接写进代码注释里，免得以后有人"顺手优化"又把顺序调回去。

修好之后，带标签的 v3 会在报告里出现**两次**，而且两次都一致：

```
removable_versions: [2, 1]      # v3 不在可删列表里
kept_versions: [4, 3]           # 而它出现在保留列表里
```

**这份报告曾经是自相矛盾的，值得记一笔。** 在修那个 `keep_set` 顺序 bug
之前，`kept_versions` 是在标签过滤**之前**算的，于是同一份报告会告诉你
"v3 不可删"，却又不把它列进"保留下来的版本"。那时候判断一个版本安不安全
只能看它**不在** `removable_versions` 里，看 `kept_versions` 反而会被误导。
现在两列都从同一个 `keep_set` 派生（`mini_lancedb/dataset.py` 里
`kept_versions` 就是 `sorted(keep_set, reverse=True)`），
所以**两列互为对照**：一个版本如果被标签保护，它必然同时出现在
`kept_versions` 里、且不出现在 `removable_versions` 里。
报告里能出现"保护了数据却不在保留列表里"这种事，本身就是个信号——
它说明有两条计算路径在各自算各自的。

顺带说一句怎么自查：把 `removable_versions` 和 `kept_versions` 并起来，
应该正好是 `list_versions()` 的全部版本号，不多不少。上面这份报告里
`{2,1} ∪ {4,3} = {1,2,3,4}`，和版本列表对得上。如果并起来少了一个号，
那就是又有一条版本掉在两条路径之间了。

**为什么测试没抓住它？** 因为原来的用例群是分开的：
有测标签的（`test_checkout_by_tag`），有测清理的
（`test_cleanup_actually_removes_versions`），
但**没有一条把两者串起来**——没有任何一条用例做过
"打标签 → 覆写 → 清理 → 按标签 checkout"。

这正好是附录 E 讲的"第三种假绿：覆盖不到关键分支"的一个实例，
只不过它不是某个函数的分支没覆盖，而是**两个功能之间的交互没人走过**。
修完之后补了回归用例
`test_cleanup_old_versions_keeps_tagged_versions_readable`，
它断言的就是上面那条 checkout。

**第三是分支的起点**，这一点下面单独讲。

标签的 API 一共六个方法（不是三个）：`Table.tags.create(name, version=None)`
（不传 `version` 就打在当前版本上）、`list()`（返回 `{name: {...}}`）、
`delete(name)`、`update(name, version)`、`get(name)`、`get_version(name)`。
删除一个标签只是删掉那个小 JSON，不影响它指向的版本本身。

`update()` 值得单独说一句，因为它和下一段要讲的"分支只能靠新提交移动"
正好相反：**`tags.update()` 在实现上是"先 delete 再 create"**，
也就是一次**就地移动指针**。它的注释里自己写了这条路径存在竞态窗口——
在删掉旧标签和建好新标签之间，有读者会看不到这个标签。
这不是实现偷懒，而是"引用就地可变"这个设计必须付的代价。
分支为什么不做成就地移动，对照着看就清楚了。

**分支（branch）：可写的引用。** `main` 是保留的默认分支名，
其余名字你可以自己建。从一个版本分出去之后，分支上有自己的可写历史，
可以独立提交新版本，主线看不到。访问方式是带 `branch=` 打开一个表句柄
（Python 侧 `open_table(..., branch=...)`，v0.34.0-beta.0 起支持；
Rust 是 `create_branch`；JS 客户端把这一切收在 `table.branches` 下，有
`create` / `checkout` / `delete` / `list` / `diff` / `cherryPick`）。
三条性质值得记住：**分支名与版本号一起构成全局标识**；
**建分支、删分支本身不产生新版本**（只有"写"才产生）；
`open_table(branch=..., version=...)` 同时给分支和版本号，拿到的是一个
**只读、钉死在那个版本**的句柄。

必须划清一条边界：**开源版的分支不会自动和主线合并。** 官方文档写得很明白，
分支之间不做 reconcile —— 它是一支独立的历史，不是"待合并的变更集"。
`diff` / `cherryPick`（以及企业版那套更完整的 diff/merge）能提供一部分比对能力，
但"自动合并"这件事不存在；这些方法的可用性在不同语言绑定和版本里也不一致，
用到时以官方文档为准。把 Lance 的分支当成 Git 分支用，会失望。

**本实现只做了标签，没做分支。** 存储层其实是认得 `_refs/branches/` 的：
`resolve_ref()` 会按"标签名 → 分支名"的顺序解析，`list_refs(kind="branch")`
也能列出分支；但**没有任何创建/删除分支的 API** —— `Table` 的注释写着
"真实 LanceDB 还有 branches（可写的 ref），本实现只做 tags"。
分支目录与标签目录的结构一模一样（都是 `{name}.json` 装一个版本号），
这本身是个设计信号：**分支和标签在存储层是同一种东西，区别只在于
你会不会往上面提交。** Git 也是这么干的（分支就是一个可移动的引用）；
Lance 这里的差别是"移动"只能靠新的提交，不能就地改指针。

## 7.6 为什么不"就地改"

一个自然的疑问：为什么不做增量 manifest，只记录"这一版改了什么"？

**第一，读放大。** 增量 manifest 要读 N 个文件才能拼出当前状态。
一个跑了三年的表可能积累几千个增量文件，打开表就要发几千次请求。
全量快照的话，永远是"读一个文件"。

**第二，快照隔离是免费的。** 读者拿到一份 manifest 之后，
无论写者怎么折腾，它看到的世界都不会变。如果用增量日志，
读者就得跟踪"我读到哪个位置了"，还要处理"我读的时候有人在写"。

**第三，实现简单得多。** 没有"合并增量""压缩日志""检测日志断链"这些事。
代价是每次提交都要写一份完整状态——而这份状态只有几百字节到几 MB，
在"一次提交本来就要写几 MB 数据文件"的背景下，完全可以接受。

这也解释了为什么 manifest 必须是**紧凑二进制格式**（真实 Lance 用 protobuf）：
它每次提交都要重写、每次查询都要读，是整条路径上最热的元数据。
我们的迷你实现用 JSON，纯粹是为了教程能把它打印出来看——
代价在第 3 章量化过：同样的结构，元数据体积大一个量级。

## 7.7 覆写（overwrite）和 GC：空间什么时候回收

有一个反直觉的事实：**`overwrite` 不删任何数据。**
它只是让新版本指向另一批片段，老片段文件原封不动地留在 `data/` 下。
所以：

```
ds.write(batch, mode="overwrite")   # 表内容变了
# 但磁盘上的老数据文件一个都没少
```

这是刻意的：只要老文件还在，任何历史版本就都还能读。
代价是磁盘占用会随版本数增长，所以必须有 GC。

`cleanup_old_versions` 是那个 GC。它的策略不是"删掉除最新版之外的所有版本"
——那太危险了，因为**可能有读者正在读老版本**。
一个跑了 20 分钟的大查询可能正握着 v3 的 manifest 在读，
这时把 v3 的文件删掉，它就会读到半个表。

所以真实策略是"**保留最近 N 个版本 + 只删早于 X 分钟前的版本**"。
两个条件都是为了给在途读者留出安全窗口。

**本实现另外给了一个真实 Lance 没有的东西：`dry_run`。**
真实 Lance 的 `cleanup_old_versions` **不接受** `dry_run` 参数
（附录 D 第 7 条记了这条报错），本实现的两层都接受它，
但**接受的方式不一样，这是最容易踩的地方**：

面向用户的 `Table.cleanup_old_versions` 签名是
`(older_than=None, *, delete_unverified=False, dry_run=False)`，
`dry_run` 是**一个真正的入参**，返回的是一个 `CleanupStats` 对象：

```
>>> t.cleanup_old_versions(dry_run=True)
CleanupStats(old_versions_removed=0, old_versions_removable=2, files_removed=3,
             bytes_removed=1825, kept_versions=[3], dry_run=True, cleaned=False, reason='')
```

底层的 `Dataset.cleanup_old_versions` 签名是
`(*, older_than=None, keep_versions=1, delete_unverified=False)`——
**没有 `dry_run` 这个参数**，它改用 `delete_unverified` 当"真的删"的开关，
默认 `False`（也就是默认只看不删），返回一个字典，
字典里**带一个 `dry_run` 字段**：

```
>>> ds.cleanup_old_versions()
{'cleaned': False, 'dry_run': True, 'removable_versions': [...],
 'removable_files': 6, 'bytes_reclaimable': 5305, 'kept_versions': [...]}
```

**上面三份报告的数字互不相同（`3 / 1825`、`6 / 5305`、`2 / 约 1.4 KB`），
这不是笔误。** 它们来自三个不同的表状态：第一份是 `Table` 层包了一层之后
的统计（口径是"版本 + 数据文件"），另两份是 `Dataset` 层的原始统计，
而其中 `6 / 5305` 那份是在**覆写之后**（多了一批没有版本引用的数据文件）
拍的，`2 / 约 1.4 KB` 那份才是 §7.4 那个只有三个版本的干净场景。
**能对上的只有同一场景内的数字**，跨场景去凑总数会得出"清理逻辑算错了"
这种假结论——对比字节数之前，先确认两次跑的是不是同一个表状态。

所以同一个名字 `dry_run` 在上层是入参、在下层是返回值字段；
`cleanup_old_versions(dry_run=True)` 打在 `Table` 上能用、打在 `Dataset`
上会直接 `TypeError`。**这不是"两处细节"，是两层 API 的语义分叉**——
写维护脚本时先确认自己拿的是哪一层。

**更要小心的是 `delete_unverified` 这个名字在两层间也不一致。**
真实 Lance 的 `cleanup_old_versions` 确实有这个参数，但语义和这里
**毫无关系**：在真实库里它管的是"要不要顺手删掉那些**任何 manifest
都没引用**的孤儿文件"，是个默认关闭的危险开关（打开它意味着放弃了
"万一有半提交产物还能人工捞回来"的余地）。本实现的
`Dataset` 层借用了同一个名字，却把它当成"关掉模拟、真的执行删除"的开关。
两个含义都属于"清理"这个大类，所以在代码 review 里极难被发现——
**如果这段代码以后要迁到真实库上，`delete_unverified=True`
会从"真的删"变成"连孤儿一起删"，删除范围比你以为的大。**
迁移时这一行必须重看。

默认关闭时看到的报告长这样：

```
cleaned: False
dry_run: True
removable_versions: [2, 1]
removable_files: 2
bytes_reclaimable: 约 1.4 KB        # 逐次运行会浮动几个字节，见下
kept_versions: [3]
```

这份报告就是 §7.4 那张 `v1 / v2 / v3` 的表跑出来的，两份数字对得上：
`{2, 1} ∪ {3}` 恰好是全部三个版本，和 §7.5 末尾那条自查规则一致。

**`bytes_reclaimable` 这里刻意没写精确值。** 它统计的是被回收的
manifest 文件大小，而 manifest 里带写入时间戳（浮点数的字符串长度
会变），所以同机连跑三次拿到的是 1441 / 1443 / 1442——
**它不是确定量**。这一条和第 23 章那个"压实前后字节数"是相反的：
那里只算数据文件、逐位可复现，所以可以写精确值；
这里算的是含时间戳的元数据，就必须写"约"。**"字节数"三个字本身
不保证确定性，要看那批字节里有没有时间戳。**

这份报告里只有 2 个文件、约 1.4 KB —— 因为我们只有一个 500 行的片段。
真实场景里这个数字会是几百 GB。

这一节讲的是"什么时候可以删"。至于什么时候**该**跑 GC、以及它和压实
怎么排期，属于维护的话题，第 23.5 节有完整的窗口设定和调用示例。

## 7.8 行号：稳定 id 的故事

版本管理还有一个隐藏的难点：**行号在压实之后会变。**

假设片段 A 有 1000 行、其中删了 900 行。压实之后，这 100 行会被
搬到新片段里，行号全变。任何"我记下了第 42 行"的外部系统就失效了。

Lance 的解法是 **stable row id**：不把行号当成"表里的第几行"，
而是当成 `(fragment_id << 32) | offset_in_fragment` 这样一个 64 位地址。
片段一旦建成就永不变，所以这个地址**永远指向同一行数据**——
哪怕这一行后来被压实搬到了别的片段，也能通过"行血脉"（row lineage）
一路映射到新位置。

我们的迷你实现里实现了这个地址编码（`Dataset.row_address()`），
但索引里用的是逻辑行号，压实之后一律把索引标记失效：

```python
clone.valid = False
clone.details["invalidated_by_compaction"] = True
```

为什么不跟着做重映射？因为完整实现需要一套"行血脉"（row lineage）结构：
每压实一次，就要记录"旧片段 X 的第 i 行现在在片段 Y 的第 j 行"，
而且这个映射要能链式传递 —— 真实 Lance 用 `DeletionFile` + `RowIdLineage`
做这件事。它与主键无关：开启它是表上的一个独立开关（stable row ids）。
**没开这个开关的表，`_rowid` 就等于行地址本身**，压实之后照样会变，
也就和本实现一样只剩下"重建索引"一条路（见第 9 章）。

**代价是真实的：** 一个亿级向量的 IVF_PQ 索引重建要几十分钟。
所以生产环境里，压实和重建索引是要排期的后台任务，
不能在写入路径上顺手做。这也解释了 LanceDB 的 `optimize()`
为什么要设计成一个"手动触发的 VACUUM"而不是自动的。
压实怎么改行号、索引为什么失效、重建该排在什么时候，第 23 章有完整流程。

## 7.9 manifest 里到底有什么

第 6 章末尾答应过要展开 manifest 的内容，这一节把它摊开。
先看数据结构 —— `mini_lancedb/format/manifest.py` 的 `Manifest`
一共只有九个字段：

按"这个字段回答什么问题"把它们分成四组，比按声明顺序更有用。

**第一组是身份：`version` 和 `transaction_id`。** `version` 是版本号
（int），**必须与文件名一致**——`read_manifest()` 会校验，不一致直接报
`CorruptedFileError`，这是全文最便宜的一道完整性检查。`transaction_id`
（str）指向 `_transactions/` 里的那个 `.txn`，它是"这次提交的原文"，
manifest 是它的结果。

**第二组是内容：`schema` 和 `fragments`。** `schema` 是全表的 schema——
字段名、类型、可空性、`field_id`、字段元数据，第 5 章讲的那一切都住在这里。
`fragments` 是 `Fragment` 的列表，**表的"数据"全在这里**：
manifest 本身不装一行数据，它装的是"数据在哪"。

**第三组是索引：`indices`，一个 `IndexMetadata` 列表。**
每一条记录"有哪些索引、覆盖哪些列、参数是什么、覆盖多少行、是否有效"。
"覆盖多少行"就是 `num_indexed_rows` / `num_unindexed_rows` 的来源
（第 12.6 节），所以这一项不是装饰，是监控指标的落点。

**第四组是溯源：`parent_version`、`timestamp`、`metadata`、`writer`。**
`parent_version`（int 或 null）是上一版号，**本实现只写不读**，下面单独讲。
`timestamp`（float，秒）是提交时刻，`find_version_by_timestamp()` 读它，
但那个函数**没有公开入口**（§7.10）。`metadata` 是 `str → str` 的自由键值，
`restore()` 会往里写 `restored_from`。`writer`（str）记"谁写的"，
多个写者时它是排查"是谁在写"的第一手线索。

**`parent_version` 要单独说清楚，因为它的名字在许诺一件实现没做的事。**
真实 Lance 的 manifest 里有父版本号，它支撑的是"版本链"这类能力——
顺着 `parent_version` 往回爬，就能重建出一条提交历史，也能校验
"我手上这个 v7 到底是不是从 v6 接上来的"。本实现**照抄了这个字段，
但没有任何代码读它**：写入时填上（`manifest.py` 的序列化路径），
读回来时解析出来，然后就没有下文了。`read_manifest()` 只校验
`version == 文件名`，不校验 `parent_version` 指向的版本是否真的存在、
是否真的是它的前驱。

后果是：**你不能用这个字段做完整性校验，也不能用它遍历历史。**
想遍历历史就老老实实 1..N 逐个 `read_manifest`，想校验链就自己比
`version` 的连续性。把这个"有字段、没语义"的现状写进表里，是因为
它是个很典型的坑：读代码的人看到 `parent_version` 会以为有版本链，
写代码的人照着这个名字去实现"链校验"就会实现出一个永远返回 True 的函数。

序列化时还会多出一个 `stats` 块（`num_rows` / `physical_rows` /
`num_deleted_rows` / `num_fragments` / `size_bytes`）。它是**从 fragments
算出来的自检统计**，读回来时并不权威 —— `from_json()` 直接忽略它，
`Manifest.describe()` 打印的这几个数每次都是重算的。

`fragments` 里每个 `Fragment` 装五样东西：`id`（随机 u64，多个写者
不会撞）、`files`（片段内按"列组"拆出的 `.lance` 文件列表）、
`physical_rows`、`deletion_file`（软删除的偏移集合，见第 9 章），
以及一个自由 `metadata`。每个 `DataFile` 自己带 `path`、`fields`
（这个文件装哪些 `field_id`，分配规则见第 5.4.1 节）、`num_rows`、
`column_names`、`size_bytes` 和 `column_stats`
（min/max/null_count —— 片段级剪枝靠它零 I/O）。

`indices` 里每条 `IndexMetadata` 的字段不少：`name`、`index_type`、
`columns`、建索引时的 `version`、`params`、`num_indexed_rows`、
`size_bytes`、`details`、`delta_versions`、`files`（`_indices/<name>/`
下的相对路径）、`created_at`、`valid`。两点值得注意：
**索引的元数据写在 manifest 里，不写在索引自己的文件里**，
所以打开一张表只读 manifest 就知道有哪些索引、覆盖哪些列；
而 `valid` 正是压实或改 schema 之后被置成 `False` 的那个标志
（第 7.8 节），重建索引会把新的一条换进去（第 23 章）。

删除文件的位置其实也在 manifest 里 —— 每个 fragment 的 `deletion_file`。
它自己的二进制格式、以及"按行地址做差分"的编码，见第 9 章。

**和真实 Lance 的差别。** 真实 Lance 的 manifest 是 protobuf，
字段比这九个多（还包含数据存储版本、以及行血脉/删除相关的一组结构），
这里不逐条对照。本实现只保留"读一张表必需"的那些项、用 JSON 序列化
（体积与解析速度的代价第 3 章量化过）。**这份清单以本实现的 `Manifest`
定义为准，真实 protobuf 的字段名不保证一一对应。**

## 7.10 版本的三个读法和一个写回

这一节把散在上面几节里的版本 API 收拢一下。它们没有一条是"再讲一遍
时间旅行"，而是各自回答一个前面没答的问题。

**第一个读法是 `list_versions()`：版本列表长什么样。** §7.4 那张
`v1 / v2 / v3` 的表就是它打印出来的，但当时没有点明。本机实测它的
每一条有七个字段：

```
v1    2026-10-17 03:09:07  -   rows=0    fragments=0  deleted=0
v2    2026-10-17 03:09:07  -   rows=50   fragments=1  deleted=0
v3    2026-10-17 03:09:07  -   rows=1    fragments=1  deleted=0

VersionInfo 字段：version / timestamp / operation / transaction_id /
                 num_rows / num_fragments / num_deleted_rows
```

**注意那个 `operation` 字段在本实现里是空字符串**——列位留在那儿，
没有值，而下面是它在真实库上的对照。

**"真实库会填它"这句话是我先写错、再改掉的，值得记下来，
因为错的方式很典型。** 原来的说法是"真实 LanceDB 的 `list_versions()`
会填 `operation`（`Overwrite` / `Append` / `Delete` 之类）"。
本机 0.40.0 实测，`list_versions()` 返回的每一条只有三个键：

```
{'version': 1, 'timestamp': datetime(...), 'metadata': {...}}
```

**没有 `operation` 这个键**，而且整个安装包里搜不到它。
真实库那第三个键是 **`metadata`**——一个装着计数器的字典
（`total_rows`、`total_data_file_rows`、`total_files_size` 之类，
第 25 章 §25.7.2 讲表健康指标时用的就是它）。

我为什么会写错：**"有版本列表 ⇒ 应该能看出每版做了什么"这个推断太顺了**，
顺到我没有去跑一遍。而这两个库在这件事上其实**殊途同归**——
真实库给的是 `metadata` 里的计数，本实现给的是一个空着的 `operation`
列位，**两边都没有"这一版是 append 还是 overwrite"这个信息**。
所以正确的结论不是"真实库有、本实现没有"，而是：
**从版本列表里读操作类型，两边都读不到**；想知道就去看
`_transactions/` 里的记录（§8.2）。

`num_deleted_rows` 值得单独留意：**它让"删除债"是可观测的**。
把最后一行的 `deleted` 和 `rows` 一比，就知道这张表累积了多少没落地的删除，
而这正是第 23 章决定"该不该压实"的第一个信号。版本列表是拿到它的
最便宜途径——不需要扫任何数据。

**第二个读法是"按时间点找版本"，它揭示了一个典型的位置错误。**
`find_version_by_timestamp(store, before)` 返回"`before` 这个时刻之前
最新的那个版本"，本机实测：

```
find_version_by_timestamp(ds.store, t0)      -> 2     # t0 是写第二版之前
find_version_by_timestamp(ds.store, now)     -> 3
find_version_by_timestamp(ds.store, 0.0)     -> None  # 没有任何版本那么老
```

前两行是它该有的行为，第三行是边界：**没有符合条件的版本时返回
`None`，不抛异常。** 这符合"查询无结果"的惯例，但调用方得自己接住。

真正值得注意的是它的第一个参数：**它要的是一个"以表为根"的 store，
不是连接的 store。** 用 `db.store`（连接级）调用它，本机实测会
**返回 `None` 而不是报错**——因为版本文件挂在 `t.lance/_versions/`
下面，连接级的 store 在根上找 `_versions/` 什么也找不到，
于是遍历出空列表、返回 `None`。这和整个教程里反复出现的
"静默空结果"是同一个形状：**参数类型对、语义错，不报错，返回空。**
`Dataset.store` 才是对的入口。

还有一层要说明：**这个函数在本实现里是死代码。**
它在 `mini_lancedb/format/manifest.py` 里定义、文档串齐全，
`Table` 和 `Dataset` 上**都没有暴露它**，仓库里也没有任何测试或
示例调用它。（真实 LanceDB 的 `restore` 只接受版本号或标签，
"按时间点恢复"是上层自己维护的映射——这条注释本身就是实现者
写下的理由。）所以上面那三行输出是我直接调模块级函数得到的，
不是在某个公开 API 上量的。**"能跑"和"有入口"是两件事。**

**第三个读法是 `restore()`，它是唯一会写回的操作。**
`checkout(v)` 是"切过去看"，`restore(v)` 是"把表退回去，并且提交一版"。
本机实测从 v3 退回 v1：

```
restore(1) 之后：版本 = [1, 2, 3, 4]，行数 = 0
manifest.metadata = {'restored_from': '1'}
```

三件事要看清楚：

**它是新提交一个版本，不是删除历史。** 版本号从 3 涨到 **4**，
v2、v3 都还在，仍然可以 checkout。所以 restore **不会**回收空间，
它只是把最新版的内容换成了老版的内容。

**老版本被记在了 `metadata` 里**（`restored_from: '1'`）。
这是"谁把表退回去了、退到了哪一版"的唯一痕迹，做审计时靠它。

**两层的返回值不一样：`Dataset.restore()` 返回新版本号（int），
`Table.restore()` 返回 `None`。** 和 `optimize()` 同一类分叉，
但方向相反——这里**底层给的信息比上层多**。
想记"我退回去之后是新版本几"，就得拿 `Dataset` 那一层。

## 7.11 小结

**一个数据集只有一份可变状态：版本 manifest。** 它是全量快照，
每次提交整体重写，每次查询整体读取。

**`u64::MAX - version` 的命名让"取最近 N 个版本"从 O(全部) 降到 O(N)。**

**`latest_version_hint.json` 是缓存，权威永远是 manifest 文件的存在性。**

**时间旅行零成本：** 读老版本就是读另一个 manifest，代码路径完全一样。

**版本不是免费的：** 数据不重复，但元数据是 N 份，而元数据每次查询都要读。

**GC 必须给在途读者留窗口：** 保留最近 N 个 + 只删早于 X 分钟的。

**压实会改行号**，索引要么跟着重映射（需要行血脉），要么重建。

下一章讲"多个写者同时提交会怎样"——也就是这套设计的并发一侧。
