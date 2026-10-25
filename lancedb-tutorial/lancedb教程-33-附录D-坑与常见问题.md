# 附录 D　坑与常见问题

这份清单里的每一条都写清楚三件事：**现象**（你会看到什么）、
**原因**（为什么是这样）、**处理**（怎么办）。

有一条筛选标准贯穿始终：**优先收录"不报错但结果是错的"的那类问题。**
会抛异常的错误其实不可怕——它当场就告诉你出问题了。真正消耗时间的是
"查询跑通了、结果返回了、只是排序是错的"这种。

除非特别标注，下面所有引用的报错文本、返回值、警告措辞都是在本机
`lancedb 0.40.0` + Python 3.14.6 + pyarrow 26.0.0 上**实际跑出来的**。
标了"文档口径"的地方表示它出自官方文档而本机未能复现；
标了"未经确认"的地方表示我没有验证过。

每条末尾给一行**复现**命令。涉及真实 LanceDB 的条目共用下面这个骨架
（在临时目录里跑，每次从干净状态开始），后文出现的 `db` / `t` / `rows`
都指它；带 `mini_lancedb.` 前缀的条目要在本教程仓库根目录跑：

```python
import lancedb, numpy as np
rng = np.random.default_rng(0)
db = lancedb.connect("./repro-db")
rows = [{"id": i, "vector": rng.random(8).astype("float32"),
         "txt": f"item-{i:06d}", "cat": "a" if i % 2 else "b"} for i in range(100)]
t = db.create_table("t", data=rows)
```

每条标题末尾带一个后果标记。**"静默错误"和"会报错"不是一回事**，
这个区分本身就是这份清单的价值所在：

- **【静默】** 不报错，但结果是错的、或数据少了（最危险）。
- **【报错】** 当场抛异常或拒绝执行。
- **【崩溃】** 进程级退出或死锁，Python 层拿不到干净的异常。
- **【文档偏离】** 文档/类型注解说的和源码实际行为不一致。
- **【行为差异】** 行为与直觉不同，但既不算错也不算崩，属于"要知道"。

**这五个标记描述的是"出了什么事"。出了问题之后该怎么办——**
这个异常属于哪一族、该不该重试、哪些失败连异常都不抛——
**在附录 K 里**，它和这份清单是同一个问题的两个轴，互补而不是覆盖。
附录 K 还实测了一个这里没记的现象：**`add()` 少给一列会静默写 `null`
（零异常、零警告）**，以及并发写冲突通常**不会**变成异常
（8 个线程写 321 行、零报错），所以**"没有报错"在这里不构成正确性证据**。

条目索引（编号 → 主题）：

**A. 建表与 `mode` 参数**
1 `create_table` 没有 `append` 模式 · 2 `add` 不认 `create` · 3 不能传单个字典 · 4 不能传逐行生成器 · 5 向量列宽度不一致

**B. 返回值与"文档说 A、实际是 B"**
6 `optimize()` 返回 `None` · 7 `cleanup_old_versions` 没有 `dry_run` · 8 缺 `pylance` 时 `ImportError` · 9 `index_stats` 返回 `None` · 10 `add` 的 docstring 说谎 · 11 `Table` 上没有 `query()` / `filter()`

**C. 分数列与查询语义**
12 FTS 返回 `_score` · 13 `.select()` 不去 `_distance` · 14 `prefilter=False` 少返回行 · 15 `where` 默认值不一致 · 16 `fast_search()` 空结果 · 17 `merge_insert` 重排行 · 18 `merge_insert` 缺列写 null · 19 主键不是唯一性约束 · 20 `ordering_field_name` 被静默忽略

**D. 索引**
21 `IvfFlat(distance_type='hamming')` panic · 22 `ef_construction` 默认 300 · 23 `num_indexed_rows=0` · 24 索引不覆盖新数据 · 25 `size_bytes` 可能是 `None`

**E. 弃用清单**
26 五个接口发弃用警告 · 27 `optimize(retrain=True)` · 28 `data_storage_version` / `enable_v2_manifest_paths` · 29 `open_table(index_cache_size=...)` · 30 `create_fts_index` 旧参数报错 · 31 遗留 Tantivy 索引 · 32 `create_fts_index` 不支持多列

**F. 并发与进程**
33 `fork` 不兼容 · 34 老 CPU 上 `import lancedb` 崩 · 35 并发写者过多 · 36 多进程各自 `optimize()` · 37 注册表不跨进程

**G. 多向量与多模态**
38 没有 `MultivectorQuery` · 39 多向量只支持 cosine · 40 多向量暴力扫描是乘积级 · 41 `blob_mode` 在远程表不可用 · 42 忘了给 blob 列打标记 · 43 跨模态的错答案

**H. 本教程实现自身的坑**
44 `parse_type` 不解析嵌套 · 45 多向量列要存成 `binary` · 46 `Dataset.create_index` 默认是向量索引 · 47 `_relevance_score` 量纲因重排器而异 · 48 内置 `hash()` 跨进程对不上 · 49 单个字符串被当可迭代 · 50 空列表转 Arrow 丢 schema · 51 `to_arrow` 后的 blob 列不是 `bytes`

**I. 命名与导入**
52 重排器类名大小写 · 53 教程类名不保证对应

**J. 文档与源码不一致**
54 清理保留期 7 天 vs 两周 · 55 Blob 文档页用 V1

条目与正文各章的对应关系（反查用）：

| 章 | 相关条目 |
|---|---|
| 第 2 章 | 1、2、3、4、10 |
| 第 5 章 | 5、50 |
| 第 7 章 | 28 |
| 第 8 章 | 1、35 |
| 第 9 章 | 17、18 |
| 第 12 章 | 9、16、23、24、25 |
| 第 15 章 | 22 |
| 第 16 章 | 21 |
| 第 17 章 | 12、20、30、31、32 |
| 第 18 章 | 14 |
| 第 19 章 | 11、13、14、15 |
| 第 20 章 | 12、13、47、52 |
| 第 21 章 | 38、39、40、41、42、43、45、51、55 |
| 第 22 章 | 37、43、48、49 |
| 第 23 章 | 6、7、8、24、26、27、36、54 |
| 第 24 章 | 29 |
| 第 25 章 | 5、17、18、19、33、34、35、36、37 |
| 第 28 章 | 28 |
| 附录 A | 52、53 |
| 附录 C | 44、45、46、53 |

## A. 建表与 `mode` 参数

### 1. `create_table` 没有 `append` 模式　【报错】

**现象**：`db.create_table("t", data=rows, mode="append")` 直接抛错。

```
ValueError: mode must be either 'create' or 'overwrite'
```

**原因**：`create_table` 的 `mode` 只接受 `"create"` 和 `"overwrite"`
两个值。类型定义是 `CreateMode = Literal["create", "overwrite"]`。
它根本没有"追加"这个语义——追加是 `add()` 的事。

**处理**：想往已有表里加数据用 `table.add(rows)`，它的默认 `mode`
就是 `"append"`。想覆盖用 `mode="overwrite"`。

**复现**：`db.create_table("t", data=[{"a": 1}], mode="append")`

### 2. `add` 只接受 `append` / `overwrite`，不认 `create`　【报错】

**现象**：`table.add(rows, mode="create")` 抛错，而且报错文本和上一条
不一样。

```
ValueError: Invalid mode: create
```

**原因**：`AddMode = Literal["append", "overwrite"]`。和 `create_table`
是**两套互不相通**的取值集合。注意这个校验不在 Python 层做，
是透传给 Rust 之后才报的，所以错误文本的措辞和 `create_table` 那条
不一致（一个说 "must be either"，一个说 "Invalid mode:"）。

**处理**：记住"建表用 create/overwrite，加数据用 append/overwrite"。
两组值的交集只有 `overwrite`，那是唯一一个能同时出现在两处的值。

**复现**：`t.add([{"a": 1}], mode="create")`

### 3. `create_table` 不能传单个字典　【报错】

**现象**：

```
ValueError: Cannot create or add rows from a single dictionary. Use a list of dictionaries instead.
```

**原因**：源码里有一步显式检查 `isinstance(data, dict)`。设计上的理由是
"一行的数据"和"一批数据"必须在类型上能区分开——否则
`{"a": 1}` 到底是一行还是一列（列名 a、唯一值 1）就有了歧义。

**处理**：包成列表 `[{"a": 1}]`。哪怕只有一行也要是列表。

**复现**：`db.create_table("t", data={"a": 1})`

### 4. `create_table` 不能直接传"逐行 yield"的生成器　【报错】

**现象**：`db.create_table("t", data=gen_rows())` 抛出**和上一条完全一样**的
错误，尽管生成器本身没问题。

```
ValueError: Cannot create or add rows from a single dictionary. Use a list of dictionaries instead.
```

**原因**：生成器走的是另一个分支 `_iterator_to_reader`，它的语义是
**"迭代出 batch"，不是"迭代出行"**。它做的第一件事是
`next(data)` 取第一个 batch，然后把它当成"一批数据"处理。
如果你 yield 出来的是一个 dict，那个 dict 就被当成了一批数据 →
落到 `isinstance(data, dict)` 的检查上 → 报同一条错。

**处理**：生成器要 **yield batch**，不要 yield row：

```python
def gen_batches():
    yield [{"id": i, "vector": [...]} for i in range(1000)]   # 一批
    yield [{"id": i, "vector": [...]} for i in range(1000, 2000)]
```

或者干脆先 `list(gen_rows())` 再传。我们实测 yield list 是可以的：

```
生成器 yield list（正确写法）: ok, rows = 3
```

另外，多个 batch 的 schema 必须一致，否则会报
`Input iterator yielded a batch with schema that does not match
the schema of other batches.`

**复现**：`db.create_table("t", data=iter([{"a": 1}]))`

### 5. 向量列宽度不一致会报 `variable length vectors`　【报错】

**现象**：

```
ValueError: Vector column 'vector' has variable length vectors.
Set on_bad_vectors='drop' to remove them, set on_bad_vectors='fill'
and fill_value=<value> to replace them, or set on_bad_vectors='null'
to replace them with null.
```

**原因**：`fixed_size_list` 要求每一行的长度都一样。只要有一行长度不同，
整列就不是定长的了。最常见的触发方式不是"你写了错的向量"，
而是**混了 None**——`pa.array([[...], None, [...]])` 推出来的类型是
变长 list，因为 null 元素让长度信息丢失了。

**处理**：三个选项。`on_bad_vectors="drop"` 丢掉坏行（**危险**：
你会静默少数据）；`"fill"` 配 `fill_value=0.0` 填零（**危险**：
零向量在 cosine 距离下和所有东西都"正交"，会变成一条永远
不该被召回的行）；`"null"` 替换成 null（最保守）。
但更正确的做法是**在写入之前校验**：把长度不对的行挑出来单独处理，
不要让它们进到写入路径。

**复现**：`db.create_table("t2", data=[{"vector": [0.1, 0.2]}, {"vector": [0.1]}])`

## B. 返回值与"文档说 A、实际是 B"

### 6. `optimize()` 返回 `None`，不是 `OptimizeStats`　【文档偏离】

**现象**：

```python
>>> r = t.optimize()
optimize() -> <class 'NoneType'> None
```

**原因**：**源码里的 `optimize()` 没有 `return` 语句**，执行完
`LOOP.run(...)` 就结束了。但类型存根 `_lancedb.pyi` 声明的是
`-> OptimizeStats`，文档也跟着写。所以这是一个
"类型注解说谎"的问题。

**处理**：不要写 `stats = table.optimize(); stats.compaction`——
第一行就拿到 `None`，第二行直接 `AttributeError`。
想要统计数字就去 `list_versions()` 看版本元数据，
或者用 `index_stats(name)` 看索引状态。

**复现**：`python -X utf8 -c "import lancedb; d=lancedb.connect('./d'); print(type(d.create_table('t', data=[{'a':1}]).optimize()))"`

### 7. `cleanup_old_versions` 没有 `dry_run`　【报错】

**现象**：

```
TypeError: LanceTable.cleanup_old_versions() got an unexpected keyword argument 'dry_run'
```

真实签名只有：

```python
cleanup_old_versions(older_than: Optional[timedelta] = None, *,
                     delete_unverified: bool = False) -> CleanupStats
```

**原因**：`dry_run` 是别家数据库（以及我们教学实现）的习惯，
但真实 LanceDB 从来没有这个参数。

**处理**：想"先看看会删多少"，就在一份**副本**上跑，或者靠
`list_versions()` 自己算出可删的版本。另外注意
`delete_unverified=True` 不是"预览"的意思，它是"删除年龄不足
7 天的孤儿文件"——**默认 False 是有安全含义的**。

**复现**：`python -X utf8 -c "import inspect; from lancedb.table import LanceTable; print(inspect.signature(LanceTable.cleanup_old_versions))"`

### 8. `cleanup_old_versions` 和 `compact_files` 在没有 `pylance` 时直接 ImportError　【报错】

**现象**：调用它们抛

```
ImportError: The lance library is required to use this function.
Please install with `pip install pylance`.
```

**原因**：这两个方法内部调 `self.to_lance()`，把调用转发给
Python 侧的 `lance` 包。只装了 `lancedb`（Rust 扩展）是不够的。

**处理**：`pip install pylance`。或者改用 `table.optimize()`——
它是纯 Rust 路径，不需要额外的 Python 包。

**复现**：`t.compact_files()`（本机只装了 `lancedb`、没装 `pylance` 时）

### 9. `index_stats("不存在的索引")` 返回 `None`，不抛异常　【行为差异】

**现象**：

```python
>>> t.index_stats("nope")
None
```

**原因**：返回类型就是 `Optional[IndexStatistics]`，docstring 明写
"Returns None if the index does not exist."

**处理**：**不要**写 `try/except IndexNotFoundError`——那段 except
永远不会触发，而你的代码会在下一行因为 `None.num_indexed_rows`
挂掉。正确写法是判空：

```python
stats = table.index_stats("vector_idx")
if stats is None:
    rebuild_index()          # 而不是 raise
```

**复现**：`print(t.index_stats("nope"))`

### 10. `add()` 的类型注解和 docstring 不一致　【文档偏离】

**现象**：`table.add(rows)` 返回的是一个 `AddResult` 对象，
但 docstring 写的是 "Returns: int — The number of vectors in the table."

**原因**：返回值从 `int` 改成结果对象之后，docstring 没跟着改。
这是纯粹的文档债。

**处理**：不要写 `n = table.add(rows); print(n + 1)`。
要么用 `table.count_rows()` 拿行数，要么按 `AddResult` 的字段读。

**复现**：`python -X utf8 -c "from lancedb.table import LanceTable; print(LanceTable.add.__doc__)"`

### 11. `Table` 上没有 `query()` 和 `filter()`　【行为差异】

**现象**：同步的 `Table` 对象上 `hasattr(t, "query")` 是 `False`，
`t.filter(...)` 也不存在。

**原因**：`query()` 是 **`AsyncTable`** 才有的方法；
`analyze_plan()` / `explain_plan()` 则挂在查询构建器上，
不挂在表上。

**处理**：同步用 `t.search(...)`（它同时支持向量和 FTS，
通过 `query_type=` 区分）。要看计划就用
`t.search(...).limit(5).analyze_plan()`。

**复现**：`print(hasattr(t, "query"), hasattr(t, "filter"))`

## C. 分数列与查询语义

### 12. FTS 返回 `_score`，不是 `_distance`　【行为差异】

**现象**：全文检索的返回列是 `['_score', 'id', 'text', 'vector']`，
每行形如 `{'_score': 0.1784704327583313, ...}`。

**原因**：两路检索用不同的分数列，这是刻意的——
`_distance` 是距离（越小越好），`_score` 是 BM25（越大越好）。
执行计划里 FTS 分支的投影就是 `_score@1 as _score`。

**处理**：任何"混合两路结果"的代码都必须先**统一方向**。
直接把两列相加是静默错的（详见第 20 章 20.2 节）。

**复现**：`print(t.search("item", query_type="fts").limit(1).to_arrow().column_names)`

### 13. `.select()` 不会去掉 `_distance`　【行为差异】

**现象**：

```python
>>> t.search([0.0] * 8).select(["id"]).limit(3).to_list()
[{'id': 2, '_distance': ...}, ...]        # 返回列: ['_distance', 'id']
```

而且控制台上会多出一行警告：

```
Deprecation warning, this behavior will change in the future. This search
specified output columns but did not include `_distance`.  Currently the
`_distance` column will be included.  In the future it will not.  Call
`disable_scoring_autoprojection` to adopt the future behavior and avoid
this warning
```

**原因**：`_distance` 不是普通列，是 Lance 执行层**自动附加**的分数列。
`select()` 只管用户列，不管它。当前版本会把它一起带出来，
但官方已经明确说**未来不会**。

**处理**：如果你的下游代码依赖 `_distance` 一定存在，现在就显式
把它写进 `select(["id", "_distance"])`，不要依赖自动附加。
警告里提到的 `disable_scoring_autoprojection` 在**同步的向量查询
构建器上不存在**（我们实测 `hasattr(t.search([0.0] * 8),
"disable_scoring_autoprojection")` 是 `False`），它挂在别处
（**具体位置未经确认**）。所以最稳的做法就是显式列出你要的列。

**复现**：`print(t.search([0.0] * 8).select(["id"]).limit(3).to_list()[0])`

### 14. `prefilter=False` 会返回少于 `limit` 的行数　【行为差异】

**现象**：

```python
t.search([0.0] * 8).where("id >= 90", prefilter=False).limit(5)   # 1 行
t.search([0.0] * 8).where("id >= 90", prefilter=True ).limit(5)   # 5 行
```

同一个查询、同一个谓词，只改一个开关，结果从 5 行变成 1 行
（谓词选择性再高一点就会直接是 0 行）。

**原因**：`prefilter=False` 是 postfilter：先用 ANN 取回 `limit` 个
最近邻，**再**用谓词筛。如果那 `limit` 个候选一个都不满足谓词，
结果就是空的。官方 docstring 的原话是 "it can cause the query to
return fewer than `limit` results (or even no results) if none of
the nearest results match the filter."

**处理**：这不是 bug，是"先排序后过滤"的必然结果。默认应该是
prefilter。只在谓词选择性极低（比如"排除已删除的 1%"）时才用
postfilter，因为那时 prefilter 会把候选池压得太小、反而丢召回。

**复现**：`print(len(t.search([0.0] * 8).where("id >= 90", prefilter=False).limit(5).to_list()))`

### 15. `where(..., prefilter=...)` 的默认值在两个类里不一样　【行为差异】

**现象**：`LanceVectorQueryBuilder.where()` 的默认值不是 `True`，
而是 `None`（一个哨兵值），并且它的 docstring 里标着
"This feature is **EXPERIMENTAL** and may be removed and modified
without warning in the future."

**原因**：基类和向量查询构建器是两处独立定义的，默认值没有对齐。

**处理**：**永远显式写 `prefilter=`**，不要依赖默认值。
在一个标着 EXPERIMENTAL 的参数上依赖默认值，等于把生产行为
交给一次小版本升级来决定。

**复现**：`python -X utf8 -c "import inspect; from lancedb.query import LanceVectorQueryBuilder as B; print(inspect.signature(B.where))"`

### 16. `fast_search()` 在无索引的表上返回空结果　【静默】

**现象**：

```python
t.search([0.0] * 8).fast_search().to_list()      # 0 行
t.search([0.0] * 8).to_list()                    # 10 行
```

**原因**：`fast_search()` 的语义是"**跳过对未索引数据的暴力扫描**"。
如果表根本就没有索引，那么"未索引数据"就是**全部数据**，
结果自然是空的。docstring 写得很清楚：它 "will miss any data
that has not been indexed"。

**处理**：在确认索引已经覆盖了全部数据（`index_stats` 的
`num_unindexed_rows == 0`）之后再开 `fast_search()`。
它最常见的翻车场景是"刚写完一批新数据、还没 optimize 就开了
fast_search"——那批新数据会**静默消失**，不报错、不警告。

**复现**：`print(len(t.search([0.0] * 8).fast_search().to_list()))`（无索引表上为 0）

### 17. `merge_insert` 会改变行的顺序　【行为差异】

**现象**：合并前后比较整表，行的物理顺序变了。

**原因**：`merge_insert` 的语义是"在一个事务里删旧插新"，
被更新的行会以新行的形式落到新的 fragment 里。
官方 docstring 明写 "the data may appear to be reordered as part
of this operation."

**处理**：不要写任何依赖行序的逻辑。需要稳定顺序就在查询里
`order_by`，或者在 schema 里存一个显式的序号列。

**复现**：`t.merge_insert("id").when_matched_update_all().execute(rows[:5]); print(t.to_arrow()["id"].to_pylist())`

### 18. `merge_insert` 缺列会写 null，非空列会让整批失败　【报错】

**现象**：upsert 的数据只带了一部分列，写入时报错说某列不能为 null。

**原因**：官方文档的说法是 "when `merge_insert` creates a new row,
any missing columns are written as `null`. If a missing column is
non-nullable in your schema, the insert will fail."

**处理**：要么 upsert 时带上完整的列，要么把列都声明成可空。
特别注意：**只更新一部分列**的场景（比如"只更新摘要列"）
天然会触发这个问题，因为它走的是"更新已有行"而不是"新建行"
——如果它恰好要新建一行，缺的列就成了 null。

**复现**：`t.merge_insert("id").when_not_matched_insert_all().execute([{"id": 999}])`（缺非空列时）

### 19. 主键不是唯一性约束　【静默】

**现象**：连续两次 `table.add()` 写进两条 `doc_id` 相同的行，
系统不报错。

**原因**：官方文档明写 "Primary keys in LanceDB are metadata used by
operations such as `merge_insert`; they are not enforced as uniqueness
constraints on ordinary writes."

**处理**：去重是你自己的责任，靠 `merge_insert` 而不是靠普通
`add` 来维持。如果你的管道里有任何一条路径用了 `add` 写主键列，
那张表就可能已经有重复行了——这件事值得单独写一个校验任务去查。

**复现**：`t.add([{"id": 1, "vector": rows[0]["vector"]}]); print(t.count_rows("id = 1"))`

### 20. `order_by` 之外的 FTS 排序参数被静默忽略　【静默】

**现象**：`search(..., ordering_field_name="popularity")` 不报错，
但排序完全没有按 popularity。

**原因**：源码里那个参数被收下之后直接丢弃：
`_ = ordering_field_name`，附一句注释说
"Deprecated compatibility parameter. Native FTS ordering is now
configured through `order_by()`."

**处理**：用 `order_by()`。任何"传了参数但不报错、行为又没变"的
情况都值得去源码里确认一下——这类静默忽略比报错危险得多。

**复现**：`t.search("item", query_type="fts", ordering_field_name="id").limit(3)`

## D. 索引

### 21. `IvfFlat(distance_type='hamming')` 会让 Rust 层 panic　【崩溃】

**现象**：建索引时进程级的 panic，Python 侧只看到一个笼统的异常：

```
thread 'lancedb-tokio-worker' panicked at
C:\Users\runneradmin\.cargo\registry\src\index.crates.io-...\lance-index-13.0.0\src\vector\kmeans.rs:578:21:
KMeans::find_partitions: hamming is not supported
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

RustPanic: rust future panicked: unknown error
```

**原因**：Python 侧的 `IvfFlat` 类型注解里**允许** `"hamming"`
（它是唯一一个允许这个值的 config 类），但 Rust 侧的 kmeans 训练
路径不支持它，直接 `panic!`。而且二进制里还有另一条字符串
`hamming distance only supports UInt8 vectors`——就算绕过了
kmeans 那一步，非 UInt8 的向量也会被拒。

**处理**：**不要用 hamming。** 而且没有替代品——我们查过 0.40.0 的
`lancedb.index`，它暴露的 config 只有 `IvfFlat`、`IvfPq`、`IvfSq`、
`IvfRq`、`IvfHnsw*`、`Hnsw*`、`FTS`、`BTree`、`Bitmap`、`LabelList`
——**没有任何二进制量化的 config 类**。所以"用 hamming 做二进制向量"
这条路在 Python API 上目前是走不通的。文档里
"hamming is supported for IVF_FLAT indexes only" 这句话
和实际实现不一致。

顺带一条：`RustPanic` 这种异常类型**不应该被当作可重试错误**。
它意味着 Rust 侧的状态可能已经损坏，重试只会再崩一次。

**复现**：`t.create_index("vector", config=lancedb.index.IvfFlat(distance_type="hamming"))`

### 22. `ef_construction` 的默认值：文档说 150，源码说 300　【文档偏离】

**现象**：按文档的"Start at 150"去调参，发现默认值本来就是 300。

**原因**：`HnswPq` / `HnswSq` / `HnswFlat` 三个 config 类的字段默认值
**都是 300**，docstring 也写 "default 300"；而官方文档的性能调优
页面写的是 "Start at 150"。我们实测 `HnswSq().ef_construction` 得到
**300**。

**处理**：以源码为准（300）。文档里的 150 是"建议的起点"，
不是默认值——但文档没有把这个区别写清楚。

**复现**：`python -X utf8 -c "import lancedb.index as i; print(i.HnswSq().ef_construction)"`

### 23. `index_stats` 在小数据集上可能显示 `num_indexed_rows=0`　【行为差异】

**现象**：IVF_PQ 索引明明建好了（`create_index` 没报错），
`index_stats` 却显示 `num_indexed_rows=0, size_bytes=0`，
查询也还是慢。

**原因**：索引已经创建但**还没有覆盖任何行**——通常是因为建索引时
数据量太小，训练出来的分区结构对这批数据没有实际作用，
或者建索引时表里还没有数据。

**处理**：把 `num_indexed_rows` 和表的总行数对比。
两者差得多就说明索引没起作用。**"索引建成功了"和"索引在生效"
是两件不同的事**，只看 `create_index` 没报错是不够的。

（我们自己的实测里，300 行、32 维、`IvfPq(num_partitions=1,
num_sub_vectors=8)` 的索引是正常覆盖的：
`num_indexed_rows=300, num_unindexed_rows=0`。所以这条的触发条件
更可能是"建索引时表是空的"或"索引建完之后又被大量删除"，
**具体的触发阈值未经确认**。）

**复现**：`print(t.index_stats("idx").num_indexed_rows, t.count_rows())`（先建好 `idx`）

### 24. 索引不会自动覆盖新写入的数据　【行为差异】

**现象**：写入一批新数据之后查询变慢，但功能正常。

**原因**：索引记录的是"它建的时候覆盖了哪些行"。之后 `add` 进来的行
不在索引里，查询时走暴力扫描兜底——所以**搜得到，只是慢**。

**处理**：把 `index_stats(name).num_unindexed_rows` 作为常驻监控，
在它涨起来的时候跑 `optimize()`（它会做增量索引更新）。
官方建议 "schedule it on a cadence that keeps `num_unindexed_rows`
close to zero"。

**复现**：`t.add(rows); print(t.index_stats("idx").num_unindexed_rows)`

### 25. `index_stats` 的 `size_bytes` 可能是 `None`　【行为差异】

**现象**：`stats.size_bytes` 是 `None` 而不是一个整数。

**处理**：做数值比较前先判空。要从"索引占多少空间"推出磁盘用量，
不要依赖这个字段，去看 `list_versions()` 的 `total_files_size`。

**复现**：`print(t.index_stats("idx").size_bytes)`

## E. 弃用清单（照着改，别等它删）

### 26. 五个方法/函数会发弃用警告，附精确文本　【行为差异】

**现象**：调用这些接口时多出警告。实测到的原文：

```
create_scalar_index is deprecated as of 0.25.0.
    Use create_index() with config=BTree()/Bitmap()/LabelList() instead.

create_fts_index is deprecated as of 0.25.0.
    Use create_index() with config=FTS() instead.

compact_files is deprecated as of 0.21.0. Use `Table.optimize` instead.

cleanup_old_versions is deprecated as of 0.21.0. Use `Table.optimize` instead.

table_names() is deprecated, use list_tables() instead
```

**原因**：索引创建被统一到单一的 `create_index(column, config=...)`
入口；维护操作被统一到 `optimize()`。这是一次**接口收敛**，
不是功能变化。

**处理**：对应关系是 `create_scalar_index` → `create_index` +
`BTree()`/`Bitmap()`/`LabelList()`；`create_fts_index` →
`create_index` + `FTS()`；`compact_files` 和 `cleanup_old_versions`
→ `optimize()`；`table_names()` → `list_tables()`。

注意 `table_names` 那条是**手写的 `warnings.warn`**（不是
`deprecation` 库），所以我们实测看到它被打印了**两次**——
这本身是个小瑕疵，但不要因为"看到两遍"就以为调用了两次。

**复现**：`t.create_fts_index("txt")`（看弃用警告）

### 27. `optimize(retrain=True)` 被弃用但静默接受　【静默】

**现象**：传 `retrain=True` 会发一条 `DeprecationWarning`
（"The 'retrain' parameter is deprecated and will be removed in a
future version."），但不报错，也不做任何额外的事。

**原因**：docstring 写 "This parameter is no longer used and is
deprecated."

**处理**：不要传它。如果依赖它来决定"什么时候重建索引"，
把那个判断挪到 `index_stats` 的 `num_unindexed_rows` 上。

**复现**：`t.optimize(retrain=True)`

### 28. `create_table` 的 `data_storage_version` / `enable_v2_manifest_paths` 已弃用　【行为差异】

**现象**：这两个参数会发 `DeprecationWarning`，提示改用
`database_options`。

**原因**：它们被从表级挪到了连接级（因为它们是数据集级别的属性，
放在每次建表时传是重复的）。

**处理**：在 `lancedb.connect(...)` 或建表时通过 `database_options`
传。`enable_v2_manifest_paths` 在对象存储上特别重要——
官方说 "opening a table with many versions is dominated by
listing cost"，V2 manifest 路径就是为降低这个成本设计的。

**复现**：`db.create_table("x", data=rows, data_storage_version="stable")`

### 29. `open_table(index_cache_size=...)` 已弃用　【行为差异】

**现象**：警告提示改用 session 级别的缓存配置。

**处理**：建一个 `Session`，把缓存大小配在它上面，
再把 session 传给 `lancedb.connect()`。

**复现**：`db.open_table("t", index_cache_size=1024)`

### 30. `create_fts_index` 的三个旧参数现在**直接报错**　【报错】

**现象**：传 `use_tantivy=True`、`ordering_field_names=...` 或非默认的
`writer_heap_size=...` 会抛 `ValueError`，不是警告。

```
ValueError: Tantivy-based FTS has been removed. Remove use_tantivy
and recreate the index with native FTS.
```

**原因**：FTS 的底层实现从 Tantivy 换成了原生实现，
那三个参数只对旧实现有意义。这三个是"从警告升级成错误"的典型。

**处理**：把它们从代码里删掉。如果表上还有旧的 Tantivy 索引，
也要先删掉重建，否则会碰到下一条。

**复现**：`t.create_fts_index("txt", use_tantivy=True)`

### 31. 表上还留着旧 Tantivy 索引时，操作会直接抛错　【报错】

**现象**：报错说在某条路径上检测到 "Legacy Tantivy FTS index
detected"，并要求删除旧索引再重建。

**处理**：删掉旧索引、用 `create_index(config=FTS())` 重建。
这类"遗留索引导致升级失败"的问题在版本升级时很常见，
升级前先把索引清一遍比升级后排查便宜。

**复现**：`t.create_index("txt", config=lancedb.index.FTS())`（表上还留着旧 Tantivy 索引时）

### 32. `create_fts_index` 不支持多列　【报错】

**现象**：传一个 `field_names` 列表（而不是单个字符串）报

```
ValueError: Native FTS indexes can only be created on a single field
at a time. To search over multiple text fields, create a separate FTS
index for each field.
```

**原因**：原生 FTS 是一列一个索引。旧实现支持合并索引，
新实现不支持了。

**处理**：按列各建一个。查询时可以用 `fts_columns=[...]`
指定要在哪几列上搜。

**复现**：`t.create_fts_index(["txt", "cat"])`

## F. 并发与进程

### 33. `fork` 和多线程的 Lance 不兼容　【崩溃】

**现象**：用 `multiprocessing` 的默认 start method（Linux 上是 `fork`）
起子进程，子进程在访问表时死锁或崩溃。

**原因**：`fork` 只复制调用线程，其它线程持有的锁在子进程里
永久处于锁定状态。Lance 内部是多线程的，所以这是必然的。

**处理**：显式 `mp.get_context("spawn")`。POSIX 上 `forkserver`
也可以（比 `spawn` 启动便宜）；**Windows 上只有 `spawn`**，
没有别的选择。PyTorch DataLoader 的 `num_workers > 0`
同样要显式指定 start method。

**复现**：Linux 上 `ctx = mp.get_context("fork"); ctx.Process(target=lambda: t.count_rows()).start()`

### 34. 老 x86_64 CPU 上 `import lancedb` 直接崩　【崩溃】

**现象**：进程启动瞬间就死，日志里什么都没有（SIGILL）。

**原因**：默认 wheel 按 `x86-64-haswell` 基线编译，要求
**AVX2 + FMA + F16C**。没有 AVX2 的 CPU（Intel Sandy Bridge /
Ivy Bridge / Westmere，AMD Bulldozer / Piledriver / Steamroller）
上执行到这些指令就收到 `Illegal instruction`。

**处理**：换成 `lancedb-compat`：

```bash
pip uninstall lancedb && pip install lancedb-compat
```

它的编译基线是 `x86-64-v2`，并用运行时 SIMD 分发在支持的机器上
照样用上 AVX2/AVX-512。两个包**共用同一个 `lancedb/` 命名空间、
安装时冲突**，所以必须先卸载再装。

排查提示：在容器里看到"进程秒退、没有任何 Python traceback"，
要把 CPU 指令集列进怀疑对象，而不是只查内存和权限。

**复现**：`python -X utf8 -c "import lancedb"`（无 AVX2 的 CPU 上收到 SIGILL）

### 35. 太多并发写者会导致写入失败　【报错】

**现象**：高峰期写入偶发失败，错误信息里提到太多并发写者。

**原因**：提交重试的次数是有限的。官方 FAQ 的说法是
"too many concurrent writers can lead to failing writes as there is
a limited number of times a writer retries a commit."

**处理**：在应用层再包一层带抖动的指数退避重试。关键是
**把"并发写者过多"这个错误当成可重试错误**——把它当成致命错误
直接让任务失败，是很多管道高峰期丢数据的直接原因。
另外，重试要用**抖动**（jitter）：多个 worker 同时线性退避
会让它们永远撞在同一时刻。

**复现**：多个进程同时 `t.add(rows)`

### 36. 多个进程各自 `optimize()` 会互相冲突　【报错】

**现象**：多个 worker 都跑压实，提交频繁失败，而且压实的收益
互相抵消。

**原因**：压实是"**重排已有 fragment**"这个动作，两个进程同时做
就是在互相使绊子——一方的提交会让另一方的快照失效。
（相对地，`add` 和"仅插入的 merge_insert"只创建新 fragment，
不会冲突，可以放心并行。）

**处理**：**压实和清理必须由单一所有者来做**——一个定时任务、
一个 sidecar 进程。不要每个 worker 各自做。官方在讲大规模回填时
也建议"考虑在回填期间禁用自动压实，避免冲突"。

**复现**：多个进程同时 `t.optimize()`

### 37. 嵌入函数和注册表不跨进程　【报错】

**现象**：`spawn` 出来的子进程里，从表 metadata 还原 embedding
函数时报"未注册的函数名"。

**原因**：注册表是**进程内**单例。用装饰器注册的函数、
用 `set_var` 设的密钥变量，都不会跨进程保留。

**处理**：把注册动作写进一个模块并在子进程入口处重新执行
（`spawn` 会重新 import 主模块，所以写在模块顶层就够了）。
更好的做法是**不要依赖注册表的隐式状态**：把"用哪个模型"
当成显式配置，在每个进程里重建。

**复现**：`mp.get_context("spawn")` 起的子进程里 `db.open_table("t_with_embedding")`

## G. 多向量与多模态

### 38. 真实 Python 包里没有 `MultivectorQuery`　【报错】

**现象**：

```
ImportError: cannot import name 'MultivectorQuery' from 'lancedb.query'
```

**原因**：多向量查询的入口就是普通的 `search()`，区别只是传进去的是
一个**矩阵**而不是一个向量：

```python
q_multi = np.random.random(size=(2, 256))
tbl.search(q_multi).limit(5).to_pandas()
```

`MultivectorQuery` 是本教程教学实现里的类。真实的 schema 类型是
`lancedb.pydantic.MultiVector`，那在 pydantic 模块下，不在 query 模块下。
（这是 0.40.0 的实测结果，**其它版本未经确认**。）

**处理**：传矩阵就行。`Reranker.rerank_multivector(vector_results,
query, deduplicate=False)` 是真实存在的融合入口，
注意它的 `deduplicate` 默认是 `False`，而且**要求输入带 `_rowid` 列**，
否则报 `ValueError`。

**复现**：`python -X utf8 -c "from lancedb.query import MultivectorQuery"`

### 39. 多向量索引只支持 cosine　【报错】

**现象**：给多向量列建 l2 或 dot 的索引失败，或者查询被拒。

**原因**：官方文档的原话是 "Multivector indexing currently requires
`distance_type=\"cosine\"`." 实测用 `metric="l2"` 建索引会报：

```
RuntimeError: lance error: LanceError(Index): Build Vector Index:
multivector type supports only cosine distance
```

**处理**：建索引时 `metric="cosine"`（实测同表换 cosine 就能建成）。
非 cosine 的查询要显式 `bypass_vector_index()`。好消息是这条错误是
`RuntimeError` 而不是 panic，可以正常捕获和分类。
（文档只点名了 **IVF_PQ** 一种索引类型；HNSW 系列和 IVF_FLAT
是否支持多向量**未经确认**。）

**复现**：`t.create_index("vector", metric="l2")`（多向量列上）

### 40. 多向量表的暴力扫描代价是乘积级的　【崩溃】

**现象**：没有索引的多向量表，查询长时间不返回（OSS 里不报错，
就是卡住）。

**原因**：暴力扫描要"每个查询向量 × 每一行的每个向量"两两比较，
代价是 `行数 × 每行向量数`，两个维度同时涨。

**处理**：多向量表建索引的时机要比单向量表早得多。官方还提醒
云端对多向量列的暴力 KNN 行数阈值大约**比单向量列低 10 倍**，
所以会比你预期的更早开始被拒。

**复现**：无索引的多向量表上 `t.search(q_multi).limit(5)`

### 41. `blob_mode` 在远程表和向量查询上不可用　【报错】

**现象**：`to_pandas(blob_mode="bytes")` 在远程表上抛
`NotImplementedError`；在向量 / FTS / 混合 / 有序查询上也不工作。

**原因**：`"bytes"` 和 `"descriptions"` 需要文件系统的支撑；
而"非扫描类查询"的结果物化不走同一条路径。官方原话是
"Vector, FTS, hybrid, and ordered queries can't materialize blob
columns through `to_pandas()`; omit blob columns from the projection
for those query shapes."

**处理**：向量查询时把 blob 列从投影里排除，拿到行之后再按需
`take_blobs(indices=[...])` 单独取。这也正是 `blob_mode="lazy"`
想要你采取的工作方式——先拿描述符，再决定读不读。

**复现**：`t.to_pandas(blob_mode="bytes")`（远程表或向量查询上）

### 42. 忘了给 blob 列打标记　【静默】

**现象**：`large_binary` 列里的数据读出来是普通 bytes，
lazy / streaming 的 blob API 用不上。

**原因**：Blob V1 要求在列上挂元数据
`metadata={"lance-encoding:blob": "true"}` 才会启用 Blob API。
（Blob V2 是一等公民，不需要这个标记，但官方文档页的部分例子仍用
V1 写法。两者的关系是清楚的：在 2.2 及以上的数据集上，dataset writer
接受打了 V1 标记的输入并把它转写成 Blob v2，只有更底层的 file writer
会拒绝它。）

**处理**：建 schema 时把标记挂上。另外注意 Blob V2 的推荐容量：
**图片小于 1MB、视频片段小于 5MB**，超过就要考虑 Dedicated
或 External 那一档。

**复现**：建 `large_binary` 列但不挂 `{"lance-encoding:blob": "true"}` 元数据

### 43. 跨模态检索"不报错的错答案"　【静默】

**现象**：图文检索能跑，但结果看起来是随机的。

**原因**：入库用了一个模型（比如 OpenCLIP ViT-B/32 laion2b_s34b_b79k，
512 维 cosine 归一化），查询用了另一个（比如 SigLIP2，768 维）。
两个模型的输出**不在同一个向量空间里**，算出来的距离没有意义。
维度碰巧一样的情况更危险——连报错都不会有。

**处理**：入库和查询必须用**同一个模型的同一个 checkpoint**。
把"模型 ID + 权重版本"写进表的 schema metadata，查询时读出来校验。
另外距离度量也要和训练时一致：CLIP 系是 cosine 归一化的，
就用 cosine，不要图省事用 l2。

**复现**：入库用 CLIP、查询用 SigLIP2，再 `t.search(vec).limit(5)`

## H. 本教程实现自身的坑

下面几条是 `mini_lancedb` 为了教学而**刻意**留下的限制或简化，
不是真实 LanceDB 的问题。照着写的时候要分开看。

### 44. `parse_type` 只吃字面量，`[N]` 这种占位符会报错　【报错】

**现象**：

```python
>>> parse_type("fixed_size_list<fixed_size_list<float16>[128]>[N]")
ValueError: invalid literal for int() with base 10: 'N'
```

**原因**：`N` 是文档里的**占位符**，不是合法的大小——把 N 原样敲进代码，
解析器算不出整数。嵌套本身没问题：解析走的是**跳过嵌套尖括号的扫描**
（`_strip_outer`），`"fixed_size_list<fixed_size_list<float16>[128]>[3]"`、
`"fixed_size_list<list<float16>>[3]"`、`"struct<x: fixed_size_list<float16>[4]>"`
都能正确解析并往返。写入路径（`FixedSizeListArray.from_pylist`）也是
递归的，子类型是 `FixedSizeList` 时会继续往下构造，所以嵌套列表能写能读。

**处理**：把 `[N]` 换成具体数字，例如
`parse_type("fixed_size_list<fixed_size_list<float16>[128]>[3]")`，
或者用 `FixedSizeList(FixedSizeList(Float16(), 128), 3)` 直接构造。
真实 LanceDB 的 schema 由 pyarrow 构造，不受这条影响。

**复现**：`python -X utf8 -c "import mini_lancedb; mini_lancedb.parse_type('fixed_size_list<fixed_size_list<float16>[128]>[N]')"`（把 `N` 换成数字就不报了）

### 45. 多向量列要按 `binary` 布局存，别当定长向量列用　【报错】

**现象**：往一个声明成 `fixed_size_list<fixed_size_list<float16>[16]>[3]`
的列里塞**平铺**的一行（`[0.0]*16`）会抛 `TypeMismatchError`，报
`列 'tokens' 类型无法归并`；按嵌套列表写（`[[0.0]*16]*3`）能写进去，
但物理布局和 ColBERT 重排路径读的不是同一套。

**原因**：`FixedSizeListArray.from_pylist` 现在走的是通用构造器
（`array_from_pylist(type.child, flat)`），嵌套子类型本身能被解析、
也能写入。真正对不上的地方在**物理布局**：多向量列在真实 Lance 里按
fp16 扁平字节流存成一个 `binary` 列。

**处理**：按真实 Lance 的物理布局把 token 矩阵存成 `binary` 列
（fp16 扁平字节流）。`ColBERTReranker.tokens_of` 有专门的
bytes 解码路径，解的就是这个布局。另外 `is_vector_type()` 对嵌套类型
返回 `False`，可以拿它当"这是多向量列不是单向量列"的判别器。

**复现**：用平铺值写嵌套 `fixed_size_list` 列，或按 `binary` 布局写多向量列

### 46. `Dataset.create_index` 的默认类型是向量索引　【报错】

**现象**：对一个**非向量列**直接调 `Dataset.create_index("txt")` 会抛
`NotSupportedError`，报

```
mini-LanceDB 未实现该能力: 标量索引类型 'IVF_PQ'；
支持的类型：BITMAP, BTREE, FTS, LABEL_LIST, NGRAM, ZONEMAP
```

**原因**：`Dataset.create_index` 的 `index_type` 默认值是 `'IVF_PQ'`
（向量索引）。标量列不认这个类型，于是报"能力未实现"，
而不是"列的类型不匹配"。

**处理**：显式给标量类型：`Dataset.create_index("txt", index_type="BTREE")`，
或 `"FTS"` / `"NGRAM"` / `"ZONEMAP"` / `"BITMAP"` / `"LABEL_LIST"`。
表层等价写法是 `table.create_index("txt", config="FTS")`。
真实 LanceDB 用 `table.create_index(column, config=FTS())`。

**复现**：在 `Dataset` 上对 `txt` 列直接 `create_index("txt")`（默认 `index_type="IVF_PQ"`）

### 47. `_relevance_score` 的量纲因重排器而异　【行为差异】

**现象**：同一个查询，RRF 的分数是 0.03、线性加权是 0.89、
cross encoder 是 0.72。想拿它做阈值判断时不知道该定多少。

**原因**：`_relevance_score` 的契约**只有方向**（越大越相关），
**没有量纲**。RRF 落在 `(0, 1/k]`，cross encoder 落在 `(0, 1)`，
线性加权落在 `[0, 1]`。

**处理**：**永远不要在换了重排器之后沿用同一个阈值。**
需要阈值语义就用线性加权（它的分数铺满 `[0, 1]`，有绝对含义），
RRF 的分数只有相对含义、不能跨查询比较绝对值。

**复现**：同一查询分别用 `RRFReranker` / `LinearCombinationReranker` / `CrossEncoderReranker` 重排，比 `_relevance_score`

### 48. 用内置 `hash()` 做索引会让查询和写入对不上　【静默】

**现象**：离线建好的索引，在**另一个进程**里查询时完全对不上；
同一个进程内一切正常。

**原因**：Python 对 `str` 的哈希带**每进程随机盐**（`PYTHONHASHSEED`）。
同一个字符串在两个进程里得到的桶号不同——于是"离线写入的向量"
和"另一个进程里编码的查询向量"落在完全不同的桶里，
而且是**静默地对不上**。

**处理**：用 `hashlib.blake2b` 这类**跨进程稳定**的哈希。
本教程的实现全部走 `stable_hash()`。测试里有一条专门跨进程
比对字节的用例盯这件事。

**复现**：两个进程各自 `python -X utf8 -c "print(hash('hello'))"`

### 49. 单个字符串被当成可迭代对象　【静默】

**现象**：接口层如果不做特判，把单个字符串 `"hello"` 直接喂进嵌入函数，
会得到 5 个"单字符向量"拼起来的结果——`str` 在 Python 里是可迭代的，
逐字符迭代完全合法。

**原因**：Python 的 `str` 是可迭代的，逐字符迭代完全合法、不报错，
只是结果是错的。这是 embedding 库里最常见的一类 bug。

**处理**：接口层要做 `isinstance(texts, str)` 的特判，包装成
`[texts]`。本教程的 `sanitize_input()` 就是干这个的。
任何"接受 str 或 list[str]"的接口都要有这个判断。

**复现**：`python -X utf8 -c "from mini_lancedb.embedding import HashingEmbeddingFunction as F; print(F().sanitize_input('hello'))"`

### 50. 空列表转 Arrow 表会丢掉 schema　【静默】

**现象**：`pa.Table.from_pylist([])` 不报错，返回的是一张**零列零行**的表
——你想保留的列名和类型**全丢了**（更老版本的 pyarrow 会直接抛
`ArrowInvalid`，现在这一版改成静默返回空表）。

**原因**：Arrow 的表必须有 schema，而空列表里没有任何类型信息，
它只能给你一张什么都没有的表。

**处理**：显式给 schema（`pa.Table.from_pylist([], schema=...)`），
或者干脆返回一张"零列零行"的表（`pa.table({})`）。本教程的
`rows_to_table([])` 走的是后者。这个坑在"查询返回 0 行"的路径上
一定会遇到，测试要专门覆盖。

**复现**：`python -X utf8 -c "import pyarrow as pa; print(pa.Table.from_pylist([]))"`

### 51. `to_arrow` 之后的 blob 列不是 `bytes`　【行为差异】

**现象**：从 LanceDB 读出来的 blob 列，类型不是 `bytes`。

**原因**：`blob_mode="lazy"`（默认）返回的是惰性的 `BlobFile` 对象，
不是 `bytes`。这个设计是为了避免"取一行元数据却把所有大对象
都拉回来"。

**处理**：明确你要什么。只要描述符就 `blob_mode="descriptions"`，
要立刻拿到字节就 `"bytes"`，要惰性访问就默认。

**复现**：`print(t.to_arrow()["blob"].type)`

## I. 命名与导入：那些"看起来对但 import 不到"的名字

### 52. 重排器类名的大小写：`OpenaiReranker` / `ColbertReranker`　【报错】

**现象**：按常识写 `OpenAIReranker` 或 `ColBERTReranker`，`ImportError`。

**原因**：真实的类名只把**首字母**大写，缩写里的后续字母是小写的。
我们实测：

```
ColBERTReranker 存在? False
OpenAIReranker  存在? False
```

而 `from lancedb.rerankers import ColbertReranker, OpenaiReranker`
是可以成功的。文件名同样是 `colbert.py` / `openai.py`（全小写）。

**处理**：按官方拼写写。`lancedb.rerankers` 里一共 13 个导出：
`AnswerdotaiRerankers`、`CohereReranker`、`ColbertReranker`、
`CrossEncoderReranker`、`JinaReranker`、`LinearCombinationReranker`、
`MRRReranker`、`OpenaiReranker`、`RRFReranker`、`Reranker`、
`TypeSafeReranker`、`VoyageAIReranker`、`WatsonxReranker`。
注意这里面**三种风格混在一起**：`ColbertReranker` 和
`OpenaiReranker` 是"首字母大写"，`RRFReranker` 和 `MRRReranker`
是全大写缩写连写，`TypeSafeReranker` 是驼峰。没有统一规律，
只能查。

**通用建议**：这类"名字猜错了"的问题不要靠记忆，直接
`[n for n in dir(lancedb.rerankers) if n.endswith(("Reranker", "Rerankers"))]`
列一遍。注意 `endswith` 得带两个后缀：`AnswerdotaiRerankers` 以
`Rerankers` 结尾，只写 `"Reranker"` 会把它漏掉，数出 12 而不是 13。
我们整份附录里的名字都是这么核对的。

**复现**：`python -X utf8 -c "from lancedb.rerankers import OpenAIReranker"`

### 53. 本教程的类名和真实类名**不保证**一一对应　【报错】

**现象**：把本教程里的类名原样拿去 import 真实的 `lancedb`，报错。

**原因**：教程实现为了突出语义，在几处刻意换了名字。
最典型的是 `MultivectorQuery`——真实 Python 包里**没有这个类**
（`ImportError: cannot import name 'MultivectorQuery' from
'lancedb.query'`），它是本教程用来演示"查询侧展开"这条路线
的教学类。另外 `LinearCombinationReranker` 在我们的实现里有
`normalize` 参数，真实的没有。

**处理**：把教程当**概念**读，把 API 调用当成"这些能力存在"
的证据，而不是可复制的代码。需要精确签名的时候，用
`inspect.signature(Cls.__init__)` 当场查一遍，或者去翻
`site-packages/lancedb/` 的源码——那比文档可靠。

**复现**：`python -X utf8 -c "from lancedb.query import MultivectorQuery"`

## J. 文档与源码不一致的地方（汇总）

这一节把分散在各处的"文档说一套、源码是另一套"集中起来。
遇到这类问题**以源码为准**——文档有版本滞后，源码没有。

### 54. 清理保留期：文档说 7 天，源码说两周　【文档偏离】

**现象**：不知道 `cleanup_old_versions()` 在不传 `older_than` 时
到底保留多久。

**原因**：同一个库里的两处说法不一致。`optimize()` 的 docstring
写的是 "cleanup_older_than: timedelta, optional **default 7 days**"；
而 `cleanup_old_versions()` 的 docstring 写的是
"older_than: timedelta, default None — The minimum age of the
version to delete. **If None, then this defaults to two weeks.**"
我们实测 `inspect.signature` 确认了 Python 层的默认值确实是
`older_than=None`，但 Python 层**没有硬编码**任何天数常量——
它把 `None` 直接透传给 Rust，所以"两周"这个语义是 Rust 侧决定的，
**真正生效的默认值未经确认**。

**处理**：**不要赌默认值。** 显式传你要的窗口：

```python
table.optimize(cleanup_older_than=timedelta(days=1))
```

保留多久完全由你的回滚需求决定。写一条"承诺可回滚到 24 小时前"
的 SLO，然后把窗口写成 1 天，这样行为和文档哪个版本对上都不影响你。

（同一类不一致还有一条，见第 22 条：`ef_construction` 文档说
"Start at 150"、源码默认 300。）

**复现**：同第 7 条：`inspect.signature(LanceTable.cleanup_old_versions)` 与 `LanceTable.optimize.__doc__` 对照

### 55. Blob 的文档页用的是 V1 机制　【文档偏离】

**现象**：按官方 Blob 文档页的写法给列加
`metadata={"lance-encoding:blob": "true"}`，能用，
但和 Blob V2 博客里描述的 API 对不上。

**原因**：Blob 有 V1 和 V2 两套。V1 是"在 `large_binary` 列上打一个
元数据标记"，V2 是"blob 是一等公民，按大小分四档存储、
四种语义共用一套描述符和 API"。文档页 `/tables/multimodal`
目前写的仍是 V1；博客 *Lance Blob V2* 讲的是 V2。
**两者之间的迁移边界和兼容性是明确的**：2.2+ 的数据集上 dataset writer
接受 V1 标记的输入并转写成 Blob v2，两种编码能混存，老数据集带
`data_storage_version="2.2"` 追加即用，重写只是可选优化。

**处理**：先确认你装的那一版支持哪一套，再用对应的 API。
写新代码优先按 V2 的设计（`blob_array` / `take_blobs` /
`data_storage_version="2.2"`）。V1 的写法在 2.2+ 上不会被拒绝，
但会被悄悄转写成 Blob v2。
另外 `to_pandas(blob_mode=...)` 这个参数是 LanceDB 这一层的新
接口，和 `lance` 包的 `BlobFile` API 是两套东西，别混用。

**复现**：按文档页给列挂 `lance-encoding:blob` 元数据，再 `t.to_pandas(blob_mode="lazy")`

## 小结

这份清单可以压成四句话。

**凡是"不报错但结果是错的"，都要当成高优先级 bug 处理。**
方向搞反的分数融合、`fast_search` 静默漏掉未索引数据、
被静默忽略的 `ordering_field_name`、被当成手工数据而不回填的部分向量
——这些都不会在日志里留下任何痕迹。

**凡是"文档说 A、实际是 B"，以源码为准。**
`optimize()` 返回 `None`、`ef_construction` 默认 300、
清理默认窗口的两处说法不一致，都是这种。文档有版本滞后，
源码没有。

**凡是"参数被接受了但行为没变"，去源码里找 `_ =`。**
静默忽略比报错危险得多，因为它给你一种"我配置生效了"的错觉。

**凡是"进程层面的事"，单独排查。**
`fork`、AVX2、注册表不跨进程——这三类问题的现象都不是
Python 异常，而是崩溃、超时、"什么都没有"。
