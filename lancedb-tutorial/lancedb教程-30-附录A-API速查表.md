# 附录 A　API 速查表

这份附录是拿来"查"的，不是拿来"读"的。它分两半：

**上半是真实 LanceDB 的 API。** 里面每一个签名都不是从文档里抄的，
而是用 `inspect.signature()` 从本机安装的 `lancedb 0.40.0`
（Python 3.14.6 / pyarrow 26.0.0）上当场取下来的。
文档和实现对不上的地方，这里一律以**实现**为准，并且在"坑"那一列点出来。

**下半是本教程 `mini_lancedb` 的 API。** 它存在的目的是让读者在两个
代码库之间来回走的时候不用重新建立心智模型；最后是一份
"哪里和真实库不一致"的对照清单——它比上半部分更重要，
因为它明确告诉你**哪些习惯不能带过去**。

正文里分散的"为什么"不会在这里重复。遇到"为什么 `optimize()` 返回 `None`
却没人管"这类问题，去附录 D（`lancedb教程-33-附录D-坑与常见问题.md`）看完整解释。

一份速查表的价值取决于它的时效性。这一份对应的是 `lancedb 0.40.0`；
换一个版本，先跑一遍下面的核对命令再看这张表：

```bash
python -X utf8 -c "import inspect, lancedb; print(inspect.signature(lancedb.connect))"
python -X utf8 -c "import inspect; from lancedb.table import LanceTable; print(inspect.signature(LanceTable.add))"
python -X utf8 -c "import lancedb.rerankers as r; print([n for n in dir(r) if n.endswith(('Reranker','Rerankers'))])"
python -X utf8 -c "import lancedb.index as i; print([n for n in dir(i) if n[0].isupper() and isinstance(getattr(i,n), type) and getattr(i,n).__module__ == 'lancedb.index'])"
```

第二条 `endswith` 必须带两个后缀：`AnswerdotaiRerankers` 以 `Rerankers` 结尾，
只写 `Reranker` 会把它漏掉，数出 12 而不是 13。第三条如果只按
`n[0].isupper()` 过滤会数出 21，多出来的是 `IndexConfig`（它的
`__module__` 是 `lancedb._lancedb`）和几个 typing / 子模块名字
（`List` / `Literal` / `Optional` / `BaseTokenizerType` / `DocumentGranularity`）；
补上 `getattr(i, n).__module__ == 'lancedb.index'` 才只剩真正的索引配置类，
数出 15 个。

## 附录里的表在哪

下面的内容里嵌着若干张表，按"查什么"索引一下；`A.N` 指对应小节。

- A.1　连接的其他入口（`connect_async` / `connect_catalog` …）
- A.2　连接对象的方法
- A.3　`LanceTable` 的分组方法表：写数据、读数据、模式演进、版本与时间旅行、索引、维护、其它（共 7 张）
- A.4　查询构建器的公共方法、向量查询专有方法（2 张）
- A.5　向量索引配置对象、标量/文本索引配置对象（2 张）
- A.6　重排器与默认模型
- A.7　全文查询 AST
- A.8　`merge_insert` 构建器
- A.10　各接口的返回值类型
- A.11　`mini_lancedb` 的 `LanceDBConnection` 方法、`LanceTable` 实例方法（2 张）
- A.12　`mini_lancedb` 的查询构建器类
- A.15　`optimize.py` 的维护函数
- A.16　`mini_lancedb` 的 `ObjectStore` 实现
- A.17　命名空间与权限的类
- A.18　嵌入函数与重排器注册表

## A.1 连接

`lancedb.connect()` 是唯一的同步入口。完整签名：

```
connect(uri=None, *, api_key=None, region='us-east-1', host_override=None,
        sql_host_override=None, read_consistency_interval=None,
        request_thread_pool=None, client_config=None, storage_options=None,
        session=None, manifest_enabled=False, namespace_client_impl=None,
        namespace_client_properties=None,
        namespace_client_pushdown_operations=None, **kwargs)
```

`uri` 是**位置参数**，其余全是关键字参数。返回值在本地场景下是
`LanceDBConnection`（继承 `DBConnection`），在 Cloud 场景下是另一个子类。

支持的 URI 形态有七种。本地目录（`"./data/my_db"`、`"~/.lancedb"`）
一个目录就是一个库，传 `pathlib.Path` 也可以；`"file:///abs/path"` 是同一个
东西的显式 scheme 写法（`file://` 前缀会被剥掉）；`"memory://"` 是内存库，
进程退出即丢，最后一个连接或表句柄关掉时表就被丢弃。
对象存储有三种：`"s3://bucket/prefix"`（配 `storage_options`）、
`"az://container/path"`（Azure Blob，`account_name` / `account_key`
直接进 `storage_options`）、`"gs://bucket/prefix"`（Google Cloud Storage——
这一条**未经确认**，本机 0.40.0 的 Python 层没有引用 `gs://`）。
最后是 LanceDB Cloud 的 `"db://<database>"`，它**必须给 `api_key`**，
否则 `ValueError`。其余每一条都能在本机的 `db.py` / `__init__.py` 里
找到对应的分支或文档字符串。

`api_key` 也可以通过环境变量 `LANCEDB_API_KEY` 给。注意
docstring 里写着 OAuth 只被 `connect_async` 支持——同步连接上 Cloud
只能走 API key。

除了 `connect()`，模块里还有另外五个入口，语义各不相同：

| 函数 | 返回 | 用途 |
|---|---|---|
| `connect_async(uri, ...)` | `AsyncConnection` | 同上，协程版；`uri` 是必填位置参数 |
| `connect_catalog(endpoint, *, api_key=None, ...)` | `Catalog` | 只连 catalog 服务，不连数据 |
| `connect_catalog_async(endpoint, ...)` | `AsyncCatalog` | 同上，异步 |
| `connect_namespace(namespace_client_impl, namespace_client_properties, ...)` | `LanceNamespaceDBConnection` | 走外部 namespace（`"dir"` / `"rest"`） |
| `connect_namespace_async(...)` | `AsyncLanceNamespaceDBConnection` | 同上，异步 |

`read_consistency_interval` 值得单独说：它只在**读**上生效，
写永远是强一致的。传 `timedelta(0)` 是"每次读都去对象存储确认版本"，
代价是每次读都多一次往返；传 `None`（默认）是"根本不检查"。

### `storage_options` 里那些不在签名里的东西

`storage_options` 是一个自由字典，但有几个键有特殊语义，会改变表的
**物理布局**而不是存储凭证。三个键值得记：`new_table_enable_stable_row_ids="true"`
开启稳定行 ID（压实、更新、删除、merge 之后行 ID 不变）；
`new_table_data_storage_version="stable"`（或 `"2.1"` 这类具体版本号）
指定新表的 Lance 数据存储版本；`new_table_enable_v2_manifest_paths="true"`
让新表用 V2 manifest 路径，降低对象存储上的 listing 成本。
这三项在旧 API 里是 `create_table(...)` 的参数，现在都已弃用
（见 A.3 的弃用清单）。

### `Session`：缓存配置的载体

0.40.0 把索引缓存和元数据缓存的配置从 `open_table(index_cache_size=...)`
挪到了 `Session` 上：

```
Session(index_cache_size_bytes=None, metadata_cache_size_bytes=None)
Session.default()                 # 静态方法，拿默认配置
session.size_bytes                # 属性：当前占用
session.approx_num_items          # 属性：当前条目数
```

建好之后传给 `lancedb.connect(..., session=sess)`。同一个 session
可以给多个连接共用，缓存状态随之共享——这正是它被设计出来的理由。

## A.2 连接对象的方法

下面这张表覆盖"你天天会用"的那部分。`DBConnection` 上还有一大批
namespace / 视图 / 密钥 / 远程 SQL 相关的方法
（`create_namespace`、`describe_secret`、`execute_query`、`create_view`、
`list_materialized_views` 等），它们只在 namespace 或 Cloud 连接上
真的可用，本地连接调用会抛 `NotImplementedError`。

| 方法 | 签名（节选自实测） | 说明 |
|---|---|---|
| `table_names` | `(page_token=None, limit=10, *, namespace_path=None)` | **已弃用**，用 `list_tables()` |
| `list_tables` | `(namespace_path=None, page_token=None, limit=None)` | 返回 pydantic 模型，**不是列表**，要取 `.tables` |
| `open_table` | `(name, *, namespace_path=None, storage_options=None, index_cache_size=None, branch=None, version=None)` | 打开已有表；`index_cache_size` 已弃用 |
| `create_table` | `(name, data=None, schema=None, mode='create', exist_ok=False, on_bad_vectors='error', fill_value=0.0, embedding_functions=None, *, namespace_path=None, storage_options=None, data_storage_version=None, enable_v2_manifest_paths=None)` | `mode` 只接受 `'create'` / `'overwrite'` |
| `drop_table` | `(name, namespace_path=None)` | 不支持软删除，直接删 |
| `drop_all_tables` | `(namespace_path=None)` | 测试里清场用 |
| `rename_table` | `(cur_name, new_name, cur_namespace_path=None, new_namespace_path=None)` | — |
| `table_exists` | `(table_id: List[str])` | **namespace 专有**；本地连接上直接 `NotImplementedError` |
| `drop_database` | `()` | 整个库删掉 |
| `uri` | 属性 | 连接时用的 URI |

`list_tables()` 的返回值容易踩：它是 `ListTablesResponse`，
有三个字段——`context` / `tables`（列表）/ `page_token`。直接
`for name in db.list_tables()` **不会报错**：这个 pydantic 模型本身可迭代，
迭代出来的是 `(字段名, 值)` 元组，于是你拿到的是
`[('context', None), ('tables', ['t']), ('page_token', None)]` 而不是表名。
这种"跑通了、但拿到的是字段名"的静默错比 `TypeError` 危险。
要表名得写 `db.list_tables().tables`。

```python
db.list_tables()            # ListTablesResponse(context=None, tables=['t'], page_token=None)
list(db.list_tables())      # [('context', None), ('tables', ['t']), ('page_token', None)]
db.list_tables().tables     # ['t']
db.table_names()            # ['t']  —— 但会打印两遍弃用警告
```

## A.3 `LanceTable`：表的全部公开方法

表的实现类是 `lancedb.table.LanceTable`（继承抽象基类 `Table`）。
下面按用途分组列出**全部**公开方法。签名按实测原文抄，只做了换行。

### 写数据

这张表列的是写入路径的四个方法。看的时候留意 `mode` 的取值集合——
它和 `db.create_table` 的取值**不是同一套**（见 A.3 末尾"类方法"那一段）。

| 方法 | 签名 | 语义 |
|---|---|---|
| `add` | `(data, mode='append', on_bad_vectors='error', fill_value=0.0, progress=None, write_parallelism=None, allow_external_blob_outside_bases=False)` | 追加/覆盖。`mode` **只接受 `'append'` / `'overwrite'`**；返回 `AddResult(version=...)` |
| `update` | `(where=None, values=None, *, values_sql=None)` | 有 `where` 就是更新匹配行；两者都不给则更新所有行 |
| `delete` | `(where)` | `where` 是必填；返回 `DeleteResult(num_deleted_rows=..., version=...)` |
| `merge_insert` | `(on)` | 返回 `LanceMergeInsertBuilder`，见 A.8 |

`add` 的坑在于"向量列缺失"：如果表带嵌入函数、而 `data` 里没有向量列，
它会**自动算好向量再写**。这条语义的细节在第 25.3 节。

### 读数据

这张表里最容易记错的是返回类型：`to_polars` 给的是 `LazyFrame`，
不是 `DataFrame`；`search` 本身不返回数据，返回的是查询构建器。

| 方法 | 签名 | 语义 |
|---|---|---|
| `to_arrow` | `()` | 整表转 `pa.Table` |
| `to_pandas` | `(blob_mode='lazy', **kwargs)` | `blob_mode` 取 `'lazy'` / `'bytes'` / `'descriptions'` |
| `to_polars` | `(batch_size=None)` | 返回 **`LazyFrame`**，不是 `DataFrame` |
| `head` | `(n=5)` | 前 n 行 |
| `count_rows` | `(filter=None)` | 带过滤条件的计数 |
| `schema` | 属性 | `pa.Schema` |
| `search` | `(query=None, vector_column_name=None, query_type='auto', ordering_field_name=None, fts_columns=None)` | 查询入口，见 A.4 |
| `take_offsets` | `(offsets: list[int])` | 按物理 offset 取行，返回 take 构建器 |
| `take_row_ids` | `(row_ids: list[int])` | 按稳定行 ID 取行 |

### 模式演进

这张表的五个方法都是纯元数据操作。注意 `alter_columns` **不能改类型**，
只能改名和改可空性——改类型是数据变换，另走一条路。

| 方法 | 签名 | 语义 |
|---|---|---|
| `add_columns` | `(transforms=None, *, computed=None)` | `computed={"新列": "SQL 表达式"}` 或给一个 `FunctionApplication` |
| `alter_columns` | `(*alterations: Iterable[dict])` | 改名 / 改可空性，**不能改类型** |
| `drop_columns` | `(columns: Iterable[str])` | — |
| `update_field_metadata` | `(*updates: dict)` | 写字段级 metadata |
| `replace_field_metadata` | `(field_name, new_metadata)` | 整体替换某字段的 metadata |

### 版本与时间旅行

这张表里 `checkout` 和 `restore` 长得像、后果完全不同，
看下一段那两个词的区别。

| 方法 | 签名 | 语义 |
|---|---|---|
| `version` | 属性 | 当前版本号 |
| `list_versions` | `()` | `List[dict]`，每条一个版本 |
| `checkout` | `(version)` | **原地**改这个表句柄指向的版本 |
| `checkout_latest` | `()` | 回到最新 |
| `restore` | `(version=None)` | **原地**把库恢复成某版本，产生一个新版本 |
| `tags` | 属性 | 标签列表 |
| `branches` | 属性 | 分支列表 |
| `current_branch` | `()` | 当前分支名 |
| `uses_v2_manifest_paths` | `()` | — |
| `migrate_v2_manifest_paths` | `()` | 迁移 manifest 布局 |

`checkout` 和 `restore` 的区别值得记牢：`checkout` 只影响**你这个句柄**
读哪个版本，别的人不受影响；`restore` 是**库级别的原地操作**，
会让所有人都跳到那个版本。前者是调试工具，后者是事故恢复工具。

### 索引

这张表最该记住的是一句话：`index_stats(name)` 在索引不存在时
**返回 `None` 而不是抛异常**，别写 `try/except`。

| 方法 | 签名 | 语义 |
|---|---|---|
| `create_index` | `(metric='l2', num_partitions=None, num_sub_vectors=None, vector_column_name='vector', replace=True, accelerator=None, index_cache_size=None, num_bits=8, index_type='IVF_PQ', max_iterations=50, sample_rate=256, m=20, ef_construction=300, *, config=None, wait_timeout=None, name=None, train=True, target_partition_size=None)` | 新老两套参数混在一个签名里，见 A.5 |
| `create_index_async` | `(column, *, config, replace=None, wait_timeout=None, name=None, train=True)` | 返回 `Job` |
| `list_indices` | `()` | 返回 `IndexConfig` 的可迭代对象 |
| `index_stats` | `(index_name)` | 返回 `IndexStatistics`，**索引不存在时返回 `None`** |
| `drop_index` | `(name)` | — |
| `wait_for_index` | `(index_names, timeout=timedelta(seconds=300))` | 等索引建完 |
| `prewarm_index` | `(name)` | 把索引预热进缓存 |
| `prewarm_data` | `(columns=None)` | 预热数据页 |
| `create_scalar_index` | `(column, *, replace=True, index_type='BTREE', name=None)` | **已弃用**（0.25.0），改用 `create_index(config=BTree()/Bitmap()/LabelList())` |
| `create_fts_index` | `(field_names, *, ordering_field_names=None, replace=False, writer_heap_size=1073741824, use_tantivy=False, tokenizer_name=None, with_position=False, base_tokenizer='simple', language='English', max_token_length=40, lower_case=True, stem=True, remove_stop_words=True, custom_stop_words=None, ascii_folding=True, ngram_min_length=3, ngram_max_length=3, prefix_only=False, block_size=128, document_granularity=DocumentGranularity.ROW, name=None)` | **已弃用**（0.25.0），改用 `create_index(config=FTS(...))`；其中 `use_tantivy` / `ordering_field_names` / 非默认 `writer_heap_size` 现在**直接报错** |

### 维护

这张表里 `optimize()` 的返回值是个坑：它**返回 `None`**，
尽管存在一个 `OptimizeStats` 类（见 A.10 末段）。

| 方法 | 签名 | 语义 |
|---|---|---|
| `optimize` | `(*, cleanup_older_than=None, delete_unverified=False, retrain=False)` | 压实 + 清理 + 索引维护三合一；**返回 `None`**；`retrain` 已弃用但静默接受 |
| `compact_files` | `(*args, **kwargs)` | **已弃用**（0.21.0），改用 `optimize()`；无 `pylance` 时直接 `ImportError` |
| `cleanup_old_versions` | `(older_than=None, *, delete_unverified=False)` | **已弃用**（0.21.0）；**没有 `dry_run`**；无 `pylance` 时 `ImportError` |
| `stats` | `()` | 返回的是**普通 `dict`**（`type(t.stats())` 实测是 `dict`，不是 `TableStatistics` 实例），键是 `total_bytes` / `num_rows` / `num_indices` / `fragment_stats` |
| `to_lance` | `(**kwargs)` | 拿到底层 `lance.LanceDataset`，需要装了 `pylance` |

### 其它

这张表是杂项：元信息属性、blob 取数、分词器查询、主键声明。

| 方法 | 签名 | 语义 |
|---|---|---|
| `name` / `uri` / `id` / `namespace` | 属性 | 元信息 |
| `embedding_functions` | 属性 | 表上注册的嵌入函数 |
| `blob_columns` | `()` | 哪些列是 blob v2 列 |
| `fetch_blobs` | `(column, row_ids)` | 按行取 blob 字节 |
| `fetch_blob_files` | `(column, row_ids)` | 取 `BlobFile`（懒加载句柄） |
| `fetch_blob_ranges` | `(column, requests)` | 按 `(row_id, start, end)` 取片段 |
| `tokenize` | `(query, *, column=None, index_name=None)` | 用表上配的分词器切词，见 A.9 |
| `infer_tokenizer_configs` | `(tokenizer_name)` 静态 | 从名字推出一份 `FTS` 配置 |
| `refresh_column` | `(column)` | 重算列，配合 `add_columns(computed=...)` |
| `set_unenforced_primary_key` | `(columns)` | 只是**声明**，不是唯一性约束 |

### 类方法（不是实例方法）

`LanceTable` 上还有一个类方法 `create(...)`，它才是 `db.create_table` 的
底层实现。它的 `mode` 和 `db.create_table` 是**同一套**，没有多出来的值：

```
LanceTable.create(db, name, data=None, schema=None, mode='create', ...)
# 签名注解是 CreateMode = Literal['create', 'overwrite']
```

它的 docstring 白纸黑字写着 mode 可取 `"create"` / `"overwrite"` / `"append"`，
但**实现不认 append**：实测 `LanceTable.create(db, "t", data, mode="append")`
抛 `ValueError: Invalid mode append`。`lancedb/types.py` 里的真身是
`CreateMode = Literal["create", "overwrite"]`——docstring 声称接受 append，
实际两边都只有 `create` / `overwrite`，"append" 只是文档没删干净。
想追加仍然只能用 `table.add()`。这正是本附录开头那条"以**实现**为准"的
典型样本：这一处恰好是照抄了文档才会写错的。

### 弃用清单（照着改）

以下九个接口或参数已被标记弃用，各自的替代物是：`table_names()` →
`list_tables()`；`create_scalar_index`（0.25.0）→
`create_index(config=BTree()/Bitmap()/LabelList())`；`create_fts_index`（0.25.0）
→ `create_index(config=FTS())`；`compact_files`（0.21.0）→ `optimize()`；
`cleanup_old_versions`（0.21.0）→ `optimize(cleanup_older_than=...)`；
`create_table(data_storage_version=...)` 和
`create_table(enable_v2_manifest_paths=...)` → 连接级的 `database_options`；
`open_table(index_cache_size=...)` → `Session(index_cache_size_bytes=...)`；
`optimize(retrain=...)` 则**没有替代物**，那个参数已经不起作用。

前五条的警告文本逐字如下（实测）：

```
create_scalar_index is deprecated as of 0.25.0.
    Use create_index() with config=BTree()/Bitmap()/LabelList() instead.
create_fts_index is deprecated as of 0.25.0.
    Use create_index() with config=FTS() instead.
compact_files is deprecated as of 0.21.0. Use `Table.optimize` instead.
cleanup_old_versions is deprecated as of 0.21.0. Use `Table.optimize` instead.
table_names() is deprecated, use list_tables() instead
```

## A.4 查询构建器

`table.search(...)` 返回的**不是一个类**，而是四个构建器之一，
由 `query_type` 决定。`query` 是列表 / ndarray / 图像，给
`LanceVectorQueryBuilder`；`query_type='fts'`，或者 `query` 是字符串且
表上没有嵌入函数，给 `LanceFtsQueryBuilder`；`query_type='hybrid'` 给
`LanceHybridQueryBuilder`；`query` 为 `None`（纯扫描）给
`LanceEmptyQueryBuilder`。

四个构建器共有一批方法，另外各自有一批专有的。公共部分：

| 方法 | 语义 |
|---|---|
| `.limit(n)` / `.offset(n)` | 截断 |
| `.select(cols)` | 投影下推；**不会**去掉 `_distance` / `_score` |
| `.where(pred, prefilter=True)` | 过滤；`prefilter=False` 是 postfilter |
| `.rerank(reranker)` | 接一个重排器，见 A.6 |
| `.text(s)` | 给混合查询补上全文那一路 |
| `.vector(v)` | 给混合查询补上向量那一路 |
| `.with_row_id(bool)` / `.with_row_address(bool)` | 附带行 ID / 行地址 |
| `.fragment_ids([...])` | 只扫指定片段 |
| `.order_by([ColumnOrdering(...)])` | 显式排序 |
| `.use_lsm(bool)` | 走 LSM 写路径 |
| `.explain_plan(verbose=False)` | 打印计划 |
| `.analyze_plan(distributed_metrics='aggregate')` | 打印计划 + 运行时指标 |
| `.to_list()` / `.to_arrow()` / `.to_pandas()` / `.to_polars()` / `.to_pydantic(Model)` / `.to_batches()` / `.to_df()` | 执行 |

专有部分：

**向量查询**（`LanceVectorQueryBuilder`）才有的一批：

| 方法 | 签名 | 语义 |
|---|---|---|
| `.distance_type` | `(distance_type: Literal['l2','cosine','dot'])` | 距离度量；**这是首选名** |
| `.metric` | `(metric: Literal['l2','cosine','dot'])` | `distance_type` 的别名，docstring 明说"may be deprecated in the future" |
| `.nprobes` | `(nprobes: int)` | 探测多少个 IVF 分区；它**同时**把最小和最大探针数设成同一个值 |
| `.minimum_nprobes` | `(int)` | 自适应探针的下界 |
| `.maximum_nprobes` | `(int)` | 自适应探针的上界 |
| `.ef` | `(ef: int)` | HNSW 的搜索束宽 |
| `.refine_factor` | `(int)` | 多取 `refine_factor × k` 个候选，重排后返回前 k |
| `.fast_search` | `()` | **是一个方法**，见下 |
| `.bypass_vector_index` | `()` | 跳过索引做暴力检索（可用来算 ground truth） |
| `.distance_range` | `(lower_bound=None, upper_bound=None)` | 只保留距离落在 `[lower, upper)` **半开区间**内的行 |

`distance_type` 和 `metric` 的关系容易记反：**`distance_type` 是正名，
`metric` 是被标记为将来可能弃用的别名。** 两者当前行为完全一样，
但新代码应该写 `distance_type`。同名的概念在建索引侧
（`IvfPq(distance_type=...)`）也只有一个名字，那里没有 `metric`。

**全文查询**（`LanceFtsQueryBuilder`）专有：`.phrase_query(bool=True)`、
`.fast_search()`。

**混合查询**（`LanceHybridQueryBuilder`）同时有向量那批和
`.phrase_query()`，并且 `.rerank` 的签名不一样：

```
LanceHybridQueryBuilder.rerank(reranker=RRFReranker(), normalize='score')
```

也就是说混合路径**默认就带一个 RRF 重排器**，而向量路径的 `.rerank(reranker)`
是必填参数。

### 三件必须记住的事

**第一，`fast_search` 是方法，不是 `search()` 的参数。**
`search()` 的签名里根本没有 `fast_search`。正确写法是链式调用：

```python
tbl.search(q).fast_search().limit(10)      # 对
tbl.search(q, fast_search=True)            # 错：TypeError
```

它的 docstring 逐字是 "Skip a flat search of unindexed data.
This will improve search performance but search results will not
include unindexed data."——**它跳过的是"对未索引数据的那一次暴力扫描"**，
不是"跳过重排"。代价是**未索引的行会被静默跳过**：
在"只有一部分数据建了索引"的表上表现为"结果凭空变少"，
而且不报错。附录 D 第 16 条记了现场。

**第二，没有 `.postfilter()` 这个方法。**
postfilter 是通过 `where` 的参数表达的：

```python
tbl.search(q).where("cat = 'rare'", prefilter=True)     # 先过滤后检索
tbl.search(q).where("cat = 'rare'", prefilter=False)    # 先检索后过滤
```

而且这两个类的**默认值不一样**：`LanceQueryBuilder.where` 和
`LanceFtsQueryBuilder.where` 默认 `prefilter=True`，而
`LanceVectorQueryBuilder.where` 的默认值是 `None`（等价于沿用内部默认）。
写代码时显式传，别赌默认值。

**第三，`nprobes` / `metric` / `refine_factor` 不在基类 `LanceQueryBuilder` 上。**
`hasattr(LanceQueryBuilder, 'nprobes')` 是 `False`。如果你的代码把查询
对象按基类类型持有，这些方法在静态检查里是找不到的——运行时能不能调，
取决于它实际是哪个子类。

## A.5 索引配置对象

0.40.0 推荐的建索引方式是 `create_index(column, config=<配置对象>)`。
配置对象全部住在 `lancedb.index` 里，实测导出 15 个。下面两张表要给的是
**参数（含默认值）**，看的时候注意参数名是 `distance_type` 不是 `metric`：

**向量索引：**

| 类 | 参数（含默认值） |
|---|---|
| `IvfPq` | `(distance_type='l2', num_partitions=None, num_sub_vectors=None, num_bits=8, max_iterations=50, sample_rate=256, target_partition_size=None, accelerator=None)` |
| `IvfSq` | `(distance_type='l2', num_partitions=None, max_iterations=50, sample_rate=256, target_partition_size=None, accelerator=None)` |
| `IvfFlat` | `(distance_type='l2', num_partitions=None, max_iterations=50, sample_rate=256, target_partition_size=None, accelerator=None)` |
| `IvfRq` | `(distance_type='l2', num_partitions=None, num_bits=1, max_iterations=50, sample_rate=256, target_partition_size=None, accelerator=None)` |
| `IvfHnswPq` | `(distance_type='l2', num_partitions=None, num_sub_vectors=None, num_bits=8, max_iterations=50, sample_rate=256, m=20, ef_construction=300, target_partition_size=None, accelerator=None)` |
| `IvfHnswSq` | `(distance_type='l2', num_partitions=None, max_iterations=50, sample_rate=256, m=20, ef_construction=300, target_partition_size=None, accelerator=None)` |
| `IvfHnswFlat` | `(distance_type='l2', num_partitions=None, max_iterations=50, sample_rate=256, m=20, ef_construction=300, target_partition_size=None)` |
| `HnswPq` | `(distance_type='l2', num_partitions=None, num_sub_vectors=None, num_bits=8, max_iterations=50, sample_rate=256, m=20, ef_construction=300, target_partition_size=None, accelerator=None)` |
| `HnswSq` | `(distance_type='l2', num_partitions=None, max_iterations=50, sample_rate=256, m=20, ef_construction=300, target_partition_size=None, accelerator=None)` |
| `HnswFlat` | `(distance_type='l2', num_partitions=None, max_iterations=50, sample_rate=256, m=20, ef_construction=300, target_partition_size=None)` |

**标量/文本索引：**

这张表里除 `FTS` 外都是无参数的占位类，`FTS` 的参数最多：

| 类 | 参数 |
|---|---|
| `BTree` | `()` |
| `Bitmap` | `()` |
| `LabelList` | `()` |
| `Fm` | `()` |
| `FTS` | `(with_position=False, base_tokenizer='simple', language='English', max_token_length=40, lower_case=True, stem=True, remove_stop_words=True, ascii_folding=True, ngram_min_length=3, ngram_max_length=3, prefix_only=False, block_size=128, custom_stop_words=None, memory_limit=None, num_workers=None, document_granularity=DocumentGranularity.ROW)` |

几点值得单独指出：

**参数名是 `distance_type`，不是 `metric`。** 查询侧叫 `metric`，
建索引侧叫 `distance_type`。这两个名字在同一个库里并存，是历史包袱。

**`IvfFlat` 的 `distance_type` 是四个值的 `Literal`（多了 `'hamming'`）**，
其它所有向量索引只有 `l2` / `cosine` / `dot` 三个。而且
`IvfFlat(distance_type='hamming')` 在 0.40.0 上会让 Rust 层直接 panic
而不是报一个 Python 异常——附录 D 第 21 条记了现场。

**`ef_construction` 的默认值是 300，不是 150。** 文档里写
"Start at 150"，源码默认 300。以源码为准。

**`FTS` 里 `base_tokenizer` 的取值**是
`'simple' | 'raw' | 'whitespace' | 'ngram' | 'icu' | 'icu/split'`。

### 老式 `create_index` 参数

`create_index` 的签名同时保留了老式散装参数
（`metric=`、`index_type='IVF_PQ'`、`num_partitions=`、`num_sub_vectors=`…），
和新的 `config=`。两者混用的时候 `config` 优先。`index_type` 能取的值是：

```
'IVF_FLAT' | 'IVF_SQ' | 'IVF_PQ' | 'IVF_RQ'
| 'IVF_HNSW_SQ' | 'IVF_HNSW_PQ' | 'IVF_HNSW_FLAT'
```

新式配置里还多**三个** `HnswFlat` / `HnswPq` / `HnswSq`（不带 IVF 前缀），
它们对应"纯图索引"，在老式 `index_type` 里没有对应写法。

**注意这两个列表不一样，而且是故意的。** 上面这个 `index_type=` 能取
七个值、**含** `'IVF_FLAT'`；而 `lancedb.types` 里的 `IndexType`
这个 Literal 只有六个向量值、**不含** `'IVF_FLAT'`（本机实测两边的取值
都在 §A.7 抄了下来）。所以"`IVF_FLAT` 合不合法"这个问题**没有唯一答案**——
取决于你在问参数还是问类型别名。这类"同名不同域"的分叉在
一个 API 速查表里必须点破，否则读者会拿一处去推翻另一处。

## A.6 重排器

`lancedb.rerankers` 实测导出 13 个名字。**大小写是坑**：缩写只首字母大写。

| 类 | 默认模型 / 关键参数 |
|---|---|
| `RRFReranker` | `(K=60, return_score='relevance')` |
| `MRRReranker` | `(weight_vector=0.5, weight_fts=0.5, return_score='relevance')` |
| `LinearCombinationReranker` | `(weight=0.7, fill=1.0, return_score='relevance')` |
| `ColbertReranker` | `(model_name='colbert-ir/colbertv2.0', column='text', return_score='relevance', **kwargs)` |
| `CrossEncoderReranker` | `(model_name='cross-encoder/ms-marco-TinyBERT-L-6', column='text', device=None, return_score='relevance', trust_remote_code=True, batch_size=32, **kwargs)` |
| `OpenaiReranker` | `(model_name='gpt-4-turbo-preview', column='text', return_score='relevance', api_key=None)` |
| `CohereReranker` | `(model_name='rerank-english-v3.0', column='text', top_n=None, return_score='relevance', api_key=None)` |
| `JinaReranker` | `(model_name='jina-reranker-v2-base-multilingual', column='text', top_n=None, return_score='relevance', api_key=None)` |
| `VoyageAIReranker` | `(model_name, column='text', top_n=None, return_score='relevance', api_key=None, truncation=True)` |
| `AnswerdotaiRerankers` | `(model_type='colbert', model_name='answerdotai/answerai-colbert-small-v1', column='text', return_score='relevance', **kwargs)` |
| `WatsonxReranker` | `(model_name='cross-encoder/ms-marco-minilm-l-12-v2', column='text', top_n=None, return_score='relevance', api_key=None, project_id=None, space_id=None, url=None, truncate_input_tokens=None)` |
| `TypeSafeReranker` | `(model_name='jev-latest', column='text', instructions=None, criteria=None, return_score='relevance', api_key=None, max_concurrency=8, batch_size=1)` |
| `Reranker` | `(return_score='relevance')`，抽象基类 |

**`OpenaiReranker` 不是 `OpenAIReranker`，`ColbertReranker` 不是
`ColBERTReranker`。** 实测这两个"看起来更规范"的拼写都不存在。
`RRFReranker` / `MRRReranker` 反而是全大写缩写连写，`TypeSafeReranker`
是驼峰——三种风格并存，没有规律，只能查。

`AnswerdotaiRerankers` 有个容易以为是笔误的地方：类名末尾**带 s**，
它是复数形式。

`return_score` 的取值决定输出列里是多一个 `_relevance_score`
还是保留原分数，量纲随重排器而变（附录 D 第 47 条）。

## A.7 枚举与字面量类型

0.40.0 把大部分"枚举"做成了**类型别名 `Literal`**，而不是 `enum.Enum`
子类。定义都在 `lancedb/types.py` 里，逐字如下：
`QueryType = 'vector' | 'fts' | 'hybrid' | 'auto'`；
`DistanceType = 'l2' | 'cosine' | 'dot'`；`DistanceTypeWithHamming`
在上面基础上多一个 `'hamming'`；`OnBadVectorsType = 'error' | 'drop' | 'fill' | 'null'`；
`AddMode = 'append' | 'overwrite'`；`CreateMode = 'create' | 'overwrite'`；
`BlobMode = 'lazy' | 'bytes' | 'descriptions'`；
`ScalarIndexType = 'BTREE' | 'BITMAP' | 'LABEL_LIST'`；
`VectorIndexType = 'IVF_FLAT' | 'IVF_SQ' | 'IVF_PQ' | 'IVF_HNSW_SQ' | 'IVF_HNSW_PQ' | 'IVF_HNSW_FLAT' | 'IVF_RQ'`；
`IndexType` 不是 `VectorIndexType` 的超集：实测它的取值是
`'IVF_PQ' | 'IVF_HNSW_PQ' | 'IVF_HNSW_SQ' | 'IVF_HNSW_FLAT' | 'IVF_SQ' | 'IVF_RQ'`
——向量那部分**少一个 `'IVF_FLAT'`**（`VectorIndexType` 里有，`IndexType` 里没有），
另外再加上标量/文本的 `'FTS'` / `'BTREE'` / `'BITMAP'` / `'LABEL_LIST'`；
`BuiltinTokenizerType = 'simple' | 'raw' | 'whitespace' | 'ngram' | 'icu' | 'icu/split'`。

**"距离度量枚举"是不存在的。** 没有 `DistanceType` 这个类可以
`import` 去 `isinstance` 判断——它只是一个 `Literal`。运行时的对照物是
`lancedb/table.py` 里的模块级常量 `KNOWN_METRICS = {"l2", "cosine", "dot", "hamming"}`。
类型检查器认识 `Literal`，运行时只认识字符串，别指望 `AttributeError`
帮你兜底：写错度量名往往是在更深的地方才炸。

四个**真的是 `enum.Enum` 子类**的，只在查询侧出现：`FullTextQueryType`
取 `match` / `match_phrase` / `boost` / `multi_match` / `boolean`；
`FullTextOperator` 取 `AND` / `OR`；`Occur` 取
`SHOULD` / `MUST` / `MUST_NOT`；`DocumentGranularity` 取
`ROW` / `LIST_ELEMENT`。

### 全文查询 AST

结构化 FTS 查询用这五个类搭出来（都在 `lancedb.query` 里），
传给 `search(...)` 或 `.text(...)`：

| 类 | 签名 |
|---|---|
| `MatchQuery` | `(query, column, *, boost=1.0, fuzziness=0, max_expansions=50, operator=FullTextOperator.OR, prefix_length=0, document_granularity=None)` |
| `PhraseQuery` | `(query, column, *, slop=0, document_granularity=None)` |
| `BoostQuery` | `(positive, negative, *, negative_boost=0.5)` |
| `MultiMatchQuery` | `(query, columns, *, boosts=None, operator=FullTextOperator.OR)` |
| `BooleanQuery` | `(queries: list[tuple[Occur, FullTextQuery]])` |

`DocumentGranularity.LIST_ELEMENT` 是"把列表列的每个元素当成一篇文档"，
多向量/多标签检索里用得到（第 21 章）。

## A.8 `merge_insert`

`table.merge_insert(on)` 返回一个 `LanceMergeInsertBuilder`，链式配置
三类行的处理方式，最后 `.execute(new_data)`。全部方法：

| 方法 | 签名 | 语义 |
|---|---|---|
| `when_matched_update_all` | `(*, where=None)` | 源表和目标表都有的行：更新。`where` 可以只更新其中一部分 |
| `when_not_matched_insert_all` | `()` | 只在源表有的行：插入 |
| `when_not_matched_by_source_delete` | `(condition=None)` | 只在目标表有的行：删除 |
| `use_index` | `(use_index: bool)` | 用索引做匹配连接 |
| `use_lsm` | `(enable: bool)` | 走 LSM 写路径 |
| `validate_single_shard` | `(validate_single_shard: bool)` | 校验单分片 |
| `execute` | `(new_data, on_bad_vectors='error', fill_value=0.0, timeout=None)` | 返回 `MergeResult` |

`on` 可以是一个列名，也可以是一组列名——传多个就是**复合键**：
源行只有**在所有这些列上都一致**时才会更新目标行。

两个必须记住的语义（第 25.2 节展开）：

**它会重排行。** 被更新的行是"删掉再在末尾重新插入"的，所以
merge 之后按写入顺序读出来的行序会变。依赖行序的代码会在这一步炸。

**它没有 10000 行以上的现实可用性。** 大批 merge 需要分批，
而批与批之间没有事务隔离。

## A.9 `lancedb.tokenize()`

模块级函数，用**显式指定**的分词器切一个查询串，**不需要表上有 FTS 索引**：

```
tokenize(query, *, base_tokenizer='simple', language='English',
         max_token_length=40, lower_case=True, stem=True,
         remove_stop_words=True, custom_stop_words=None,
         ascii_folding=True, ngram_min_length=3, ngram_max_length=3,
         prefix_only=False) -> Iterable[FtsToken]
```

参数集和 `FTS` 配置对象对齐。`FtsToken` 只有两个字段：

```
FtsToken.text      # str
FtsToken.position  # int
```

表级版本是 `LanceTable.tokenize(query, *, column=None, index_name=None)`，
它用的是**表上那个索引实际配置的**分词器——排查"为什么这个查询检索不到"
时，用它来看查询串实际被切成了什么，比猜有用得多。

```python
[t.text for t in tbl.tokenize("Running quickly!")]
```

## A.10 返回值类型速查

同一个"看起来只是写一行数据"的动作，返回值类型是五花八门的。
拿不准的时候看这张表。

| 返回类型 | 字段 |
|---|---|
| `AddResult` | `version` |
| `UpdateResult` | `rows_updated`、`version` |
| `DeleteResult` | `num_deleted_rows`、`version` |
| `MergeResult` | `version`、`num_updated_rows`、`num_inserted_rows`、`num_deleted_rows`、`num_attempts`、`num_rows` |
| `AddColumnsResult` / `AlterColumnsResult` / `DropColumnsResult` / `UpdateFieldMetadataResult` | `version` |
| `RefreshColumnResult` | `rows_filled`、`version` |
| `CompactionStats` | `fragments_removed`、`fragments_added`、`files_removed`、`files_added` |
| `CleanupStats` | `bytes_removed`、`old_versions` |
| `RemovalStats` | `bytes_removed`、`old_versions_removed` |
| `OptimizeStats` | `compaction`（一个 `CompactionStats`）、`prune`（一个 `RemovalStats`） |
| `TableStatistics` | `total_bytes`、`num_rows`、`num_indices`、`fragment_stats` |
| `IndexStatistics` | `num_indexed_rows`、`num_unindexed_rows`、`index_type`、`distance_type`、`num_indices` |
| `IndexConfig` | `name`、`index_type`、`columns`、`index_uuid`、`type_url`、`created_at`、`num_indexed_rows`、`num_unindexed_rows`、`size_bytes`、`num_segments`、`index_version`、`index_details` |

**注意 `optimize()` 不在上表里**——它的返回类型是 `None`。
`OptimizeStats` 只在类型存根里存在（运行时 import 不到，见下），
而且就算拿得到它，`optimize()` 也不返回它。
这是本附录里最容易写出 `AttributeError` 的一条。

**上表里的类名大部分不能从顶层 `lancedb` import。** 实测
`from lancedb import OptimizeStats` / `CompactionStats` / `CleanupStats` /
`AddColumnsResult` / `AlterColumnsResult` / `DropColumnsResult` /
`UpdateFieldMetadataResult` 一律 `ImportError`。其中后四个还能从
`lancedb._lancedb` 里取到；`OptimizeStats` / `CompactionStats` /
`CleanupStats` 则只写在类型存根 `lancedb/_lancedb.pyi` 里，运行时根本不存在
（`TableStatistics` / `IndexStatistics` 反过来只在 `lancedb.table` 里）。
这张表给的是"类型形状"，别拿这些名字去写 `from lancedb import ...`。

`IndexStatistics` 有个为了向后兼容留的后门：它实现了 `__getitem__`，
所以 `stats["num_indexed_rows"]` 和 `stats.num_indexed_rows` 都能用。

另外注意 `IndexConfig.size_bytes` 的类型是 `Optional[int]`——
刚建完的索引可能拿不到大小，实测会返回 `None`（附录 D 第 25 条）。

---

## 下半：本教程的 `mini_lancedb`

下半存在的理由只有一个：**让你在两个库之间来回走的时候不用重建心智模型。**
接口形状是刻意抄的——包括那些奇怪的地方（`create_table` 没有 `append` 模式、
`fast_search` 是方法不是关键字参数）。所以下面只列"长什么样"，
不重复正文里的"为什么"。

模块地图、各模块的关键决策、以及写它时踩过的坑，在附录 C
（`lancedb教程-32-附录C-迷你实现代码导读.md`）里，这里不重复。

### A.11 连接与表

```python
import mini_lancedb as ldb

db = ldb.connect(uri=None, *, api_key=None, region=None,
                 storage_options=None, **kwargs) -> LanceDBConnection
ldb.tokenize(text, **kwargs) -> list
Table = LanceTable                      # 别名，两个名字都能用
```

分词函数是**模块级**的，挂在包上而不是连接对象上：写
`ldb.tokenize(...)`，不是 `db.tokenize(...)`。`LanceDBConnection`
上根本没有这个方法（实测 `hasattr(db, "tokenize")` 是 `False`）。
真实的 `lancedb` 同样是模块级函数 `lancedb.tokenize(...)`，
所以这一点两边是一致的，别在连接对象上找它。

`connect` 的 `api_key` / `region` / `storage_options` 是**接受但忽略**的——
留着它们只是为了让真实 LanceDB 的调用代码能原样跑起来。URI 支持
`None`（内存）、本地路径、以及模拟的 `s3://` / `gs://` / `az://`。

`LanceDBConnection` 的公开方法如下。这张表里最后三行是工具方法
（命名空间视图、I/O 记账开关），其余是表的增删查改：

| 方法 | 签名 |
|---|---|
| `table_names` | `(limit=None, **kwargs) -> List[str]` |
| `create_table` | `(name, data=None, schema=None, mode='create', exist_ok=False, embedding_functions=None, on_bad_vectors='error', fill_value=0.0, **kwargs)` |
| `open_table` | `(name, **kwargs)` |
| `drop_table` | `(name, ignore_missing=False, **kwargs)` |
| `rename_table` | `(cur_name, new_name, **kwargs)` |
| `table_exists` | `(name) -> bool` |
| `create_namespace` / `list_namespaces` / `describe_namespace` / `drop_namespace` | 见 `mini_lancedb/namespace.py` |
| `namespaces` / `instrument()` / `reset_io()` | 命名空间视图、I/O 记账开关 |

注意迷你实现里**没有 `list_tables()`**——只有 `table_names()`，
而且它没有 `page_token`。`db[...]`、`in db`、`len(db)`、`for db` 这些
鸭子类型接口都实现了。

`LanceTable`（构造签名以类方法为准）：

```python
LanceTable.create(db, name, data=None, schema=None, mode='create',
                  exist_ok=False, on_bad_vectors='error', fill_value=0.0,
                  embedding_functions=None, **kwargs) -> LanceTable
LanceTable.open(db, name, **kwargs) -> LanceTable
```

实例方法（签名按实测原文）：

| 方法 | 签名 | 与真实库的关系 |
|---|---|---|
| `add` | `(data, mode='append', on_bad_vectors='error', fill_value=0.0, **kwargs) -> AddResult` | 一致 |
| `delete` | `(where) -> DeleteResult` | 一致 |
| `update` | `(where=None, values=None, *, values_sql=None) -> UpdateResult` | 一致 |
| `merge_insert` | `(on) -> MergeInsertBuilder` | 一致 |
| `search` | `(query=None, vector_column_name=None, query_type='vector', fts_columns=None, **kwargs) -> Query` | **默认值不同**（真实是 `'auto'`） |
| `create_index` | `(column, config=None, name=None, replace=True, wait_timeout=None, **kwargs)` | **`config` 收字符串** |
| `drop_index` | `(name) -> None` | 一致 |
| `list_indices` | `() -> List[IndexMetadata]` | 字段名一致 |
| `index_stats` | `(name) -> Optional[Dict[str, Any]]` | **返回类型不同**：真实是 `IndexStatistics` 对象（属性访问可用、也支持 `stats["num_unindexed_rows"]`），这里只返回**普通 dict**，属性访问会 `AttributeError` |
| `stats` | `() -> TableStatistics` | **返回类型不同**：真实返回普通 `dict`，这里返回 `TableStatistics` 数据类（多了几个字段） |
| `count_rows` | `(filter=None) -> int` | 一致 |
| `head` | `(n=5)` | 一致 |
| `to_arrow` / `to_pandas(**kwargs)` / `to_polars(batch_size=None)` / `to_list()` | — | 一致 |
| `list_versions` | `() -> List[VersionInfo]` | 一致 |
| `checkout` / `restore` / `rollback_to` | `(version)` | `rollback_to` 是额外的 |
| `optimize` | `(*, cleanup_older_than=None, delete_unverified=False, retrain=False, **kw)` | **返回值不同** |
| `cleanup_old_versions` | `(older_than=None, *, delete_unverified=False, dry_run=False)` | **多一个 `dry_run`** |
| `compact_files` | `(*args, **kwargs)` | 无弃用警告 |
| `add_columns` | `(new_columns=None, *, computed=None, **kwargs)` | 参数名不同（真实叫 `transforms`） |
| `drop_columns` / `alter_columns` | `(columns)` / `(*alterations, **kwargs)` | 一致 |
| `to_lance` | `(**kwargs) -> Dataset` | 返回的是内建 `Dataset`，不是 `lance.LanceDataset` |
| `rename_table` / `drop` | — | 一致 |
| `tags` / `name` / `uri` / `schema` / `version` | 属性 | 没有 `branches`，只有 `tags` |

`TableTags` 上有 `.create(name, version=None)`、`.list()`、`.delete(name)`、
`.get_version(name)`、`.update(name, version=None)`、`.get(name)`。

### A.12 查询构建器

`table.search(...)` 按 `query_type` 分派到四个类，四个类共享一个 `Query` 基类。
和真实库最大的形状差异是：**`Query` 基类上就有向量那批方法**。

| 类 | 构造签名 |
|---|---|
| `Query` | `(table, *, vector_column_name=None)` |
| `VectorQuery` | `(table, vector, *, vector_column_name=None)` |
| `FTSQuery` | `(table, query, *, fts_columns=None)` |
| `HybridQuery` | `(table, vector, text=None, *, vector_column_name=None, fts_columns=None)` |

工厂函数是 `create_query(table, query, query_type='vector', *, vector_column_name=None, fts_columns=None)`，
模块常量 `DEFAULT_LIMIT = 10`。

链式方法（`Query` 基类上的全量）：

```
.limit(n)  .offset(n)  .select(columns)  .where(where, prefilter=True)
.postfilter()  .fast_search()  .bypass_vector_index()  .with_row_id(bool=True)
.rerank(reranker)  .nprobes(n)  .refine_factor(int)  .ef(int)  .distance_type(str)
.metric(...)
```

输出方法：`to_list` / `to_arrow` / `to_pandas` / `to_polars` / `to_pydict` /
`to_batch` / `to_batches(batch_size=None)` / `to_df`。
计划方法：`explain(verbose=False)` / `explain_plan(verbose=False)` /
`analyze_plan()` / `output_schema`。

子类额外的：`VectorQuery.distance_range(lower_bound=None, upper_bound=None)`
和 `VectorQuery.multivector()`（后者直接抛 `NotSupportedError`，
它演示的是路线而不是能力）；`FTSQuery.phrase(enabled=True)` /
`.phrase_query(...)` / `.columns(columns)` / `.boost(boost, column=None)`；
`HybridQuery.text()` / `.phrase()` / `.rerank(reranker=None, *, normalize='score')`。

**`postfilter()` 在这里是一个真方法**，在真实库里没有（要用
`where(..., prefilter=False)`）。这是刻意的：教学实现要把两条语义
都放在台面上，好让测试能断言"postfilter 返回少于 k 行"。
`where` 的默认 `prefilter=True` 和真实库一致。

扫描查询（`query=None`）的默认 limit 是 **`None`（全都要）**，
这个差异是实测真实库得到的：`tbl.search().to_arrow()` 返回全表，
而 `tbl.search(vec).to_arrow()` 只返回 10 行。

### A.13 索引

`create_index` 的 `config` 参数**收三种形状**（这是为了让教程里
不用记那么多类而做的放宽）：字符串（`config='IVF_PQ'`）、字符串加
散装参数（`config='IVF_PQ', num_partitions=16`）、或者一个配置对象。
真实库只认最后一种。

数据集层（`Dataset.create_index`）的签名是：

```python
Dataset.create_index(column, *, index_type='IVF_PQ', name=None,
                     metric='l2', replace=True, **params) -> IndexMetadata
```

`index_type` 支持的名字就是两张注册表的键。`VECTOR_INDEX_TYPES` 里
`FLAT`→`FlatIndex`、`IVF_FLAT`→`IvfIndex`、`IVF_SQ`→`IvfSqIndex`、
`IVF_PQ`→`IvfPqIndex`、`HNSW`→`HnswIndex`、`IVF_HNSW_SQ`→`IvfHnswSqIndex`、
`IVF_HNSW_PQ`→`IvfHnswPqIndex`、`BQ`→`BqIndex`；`SCALAR_INDEX_TYPES` 里
`BTREE`→`BTreeIndex`、`BITMAP`→`BitmapIndex`、`LABEL_LIST`→`LabelListIndex`、
`NGRAM`→`NGramIndex`、`ZONEMAP`→`ZoneMapIndex`、`FTS`→`InvertedIndex`。

两点和真实库对不上，注意别按真实库的名字猜：

**迷你实现没有 `IVF_RQ`。** 真实库的 `VectorIndexType` 里有它，
这里没有——`IVF_RQ` 是随机量化，教学实现只到 SQ / PQ / BQ 三档。

**`FTS` 在 `SCALAR_INDEX_TYPES` 里，不在单独一张文本表里。**
换句话说倒排索引在迷你实现中和 BTREE 并列，走同一个
"标量索引"接口；真实库的 `ScalarIndexType` 则只有
`'BTREE' | 'BITMAP' | 'LABEL_LIST'` 三个值，全文索引根本不走那条路。

`NGRAM` 和 `ZONEMAP` 也是迷你实现**可显式创建**、
而真实库没有对应 `index_type` 字符串的两个类型。第 18.4、18.5 节
解释了为什么它们仍然值得实现：它们是"候选集"和"剪枝"这两种
非索引式加速的代表，不实现就讲不清楚"索引不一定非要是索引"。

查询接口是 `vector_index_types()` / `scalar_index_types()`，
两者返回"类型名 → 实现类名"的字典。

每个索引类都实现同一组方法，这是刻意的约束：

```
build / search / append / remap / save / load / stats / size_bytes / num_indexed_rows
```

### A.14 数据集层

```python
from mini_lancedb.dataset import open_dataset, create_dataset, WriteParams, Dataset

open_dataset(store, uri, **kw) -> Dataset
create_dataset(store, uri, data, *, mode='create', schema=None) -> Dataset
WriteParams(max_rows_per_file=1048576, max_rows_per_group=1048576,
            max_bytes_per_file=96636764160, target_values_per_page=8192,
            compression='none', batch_size=1024)
```

`max_rows_per_group` 和 `max_bytes_per_file` 的默认值（每组 100 万行、
单文件 90 GiB）刻意和真实 Lance 对齐，因为"默认值差一个量级"会让
所有关于文件切分的实验结论都失效。而 `target_values_per_page=8192`
是**故意和对齐相反**的：真实 Lance 按字节切页（默认目标页大小 8 MiB，
见第 3 章 §3.6），这里按值的个数切，好处是页大小可预测、实验可复现。
第 3 章里"把页从 8192 个值改成 512 个值"那个实验就是拧这个参数。

`Dataset` 的常用方法：`write(batches, *, mode='append', params=None, max_retries=3)`、
`delete(predicate)`、`update(...)`、`merge_insert(on)`、
`add_columns` / `drop_columns` / `alter_columns`、
`checkout` / `restore` / `list_versions`、
`create_tag` / `list_tags` / `delete_tag` / `tag_version`、
`count_rows` / `row_address(logical_row)` / `read_fragment(...)` / `take_rows(...)` /
`scanner(**kwargs)` / `to_batches` / `to_arrow` / `to_pylist` / `head(n=5, columns=None)`、
`create_index` / `drop_index` / `list_indices` / `load_index`、
`compact(...)` / `cleanup_old_versions(...)` / `optimize(...)` / `stats()`。

属性：`version` / `schema` / `fragments` / `indices` / `num_rows` /
`physical_rows` / `num_deleted_rows` / `vector_columns`。

`MergeInsertBuilder` 有 `when_matched_update_all(*, where=None)`、
`when_matched_delete(*, where=None)`、`when_not_matched_insert_all()`、
`when_not_matched_by_source_delete(...)`、`has_matched_action`、`execute(data)`。
注意它多一个 `when_matched_delete`——那是为了把附录 C 里
"不带 `where` 时静默什么都不做"这个坑做成可观测的。

`Scanner` 是这一层最重要的可观测入口：

```python
Scanner(dataset, *, columns=None, filter=None, limit=None, batch_size=None,
        row_range=None, use_indices=True, use_zone_maps=True,
        late_materialization=True, with_row_id=False)
```

每个开关都有一个 `disable_*` / `with_*` 的链式版本，用来做
"关掉一层下推再量一次"的对照实验。输出有 `to_batches` / `to_pylist` /
`to_arrow` / `count_rows` / `explain(analyze=False)` / `build_plan` / `analyze`。

配套的 `ScanStats` 是整份教程里出现频率最高的一个结构：

```
fragments_total / fragments_scanned / fragments_pruned_by_stats / fragments_pruned_by_index
rows_scanned / rows_after_filter / bytes_read / range_reads / pages_read
pages_skipped / index_lookups / elapsed_ms / late_materialization_saved_rows
```

外加 `.summary()` 和 `.report()` 两个格式化输出。

### A.15 维护与统计

`mini_lancedb/optimize.py` 里的四个维护函数（这张表给的是签名，
默认值在下一段单独说）：

| 函数 | 签名 |
|---|---|
| `compact_files` | `(target, *, target_rows_per_fragment=None, materialize_deletions=True, min_rows_per_fragment=None, max_fragments=None, **kw) -> CompactionStats` |
| `cleanup_old_versions` | `(target, *, older_than=None, delete_unverified=False, dry_run=False, keep_versions=1, **kw) -> CleanupStats` |
| `optimize` | `(table_or_dataset, *, cleanup_older_than=None, delete_unverified=False, retrain=False, **kw) -> OptimizeStats` |
| `should_optimize` | `(dataset, *, row_change_threshold=..., operation_threshold=...) -> Dict` |

`should_optimize(dataset, *, row_change_threshold=100000, operation_threshold=20)`
返回一个决策字典，用来回答"现在该不该跑维护"。

**这里有一个会咬人的命名冲突：`mini_lancedb.optimize` 是一个函数，
不是那三个函数的模块。** 包级 `__init__` 导出了 `optimize` 这个函数，
把同名的子模块盖住了。所以下面这两行的结果完全不同：

```python
from mini_lancedb import optimize             # 拿到的是函数
from mini_lancedb.optimize import compact_files  # 这才进到模块里
```

拿不准的时候用 `importlib.import_module("mini_lancedb.optimize")`，
它返回的一定是模块。

`stats.py` 里的 `collect_table_statistics(...)` 返回一个 `TableStatistics`，
字段比真实库多（`uri` / `version` / `physical_rows` / `num_deleted_rows` /
`bytes_per_row` / `bytes_per_column` / `null_counts` / `num_versions` 等），
外加 `.deleted_rows_ratio` / `.report()` / `.to_dict()`。
多出来的字段是有意的：真实库的 `TableStatistics` 只有四个字段，
而正文里那些"元数据占了多少字节"的结论需要一个更细的载体。

### A.16 存储与格式

`mini_lancedb/storage/base.py` 的 `ObjectStore` 抽象定义了
`get` / `get_range` / `put` / `put_if_not_exists` / `delete` / `head` /
`list` / `exists` / `size` / `get_text` / `put_text` / `delete_prefix` /
`child_paths` / `join`。三个实现，这张表列的是各自用途——注意 `S3LikeStore`
比另外两个额外提供 `rename` / `conditional_commit` / `supports_conditional_put`
几个对象存储专有的方法：

| 实现 | 用途 |
|---|---|
| `InMemoryStore` | 测试与示例的默认后端 |
| `LocalFileStore(uri, *, create=True)` | 真落盘 |
| `S3LikeStore(uri='s3://demo', *, list_delay_writes=0)` | 模拟对象存储，额外提供 `rename` / `conditional_commit` / `supports_conditional_put` |

`InstrumentedStore(inner)` 是**所有 I/O 数字的来源**：它包住任意
后端，提供 `events()` / `reset_events()` / `io_report(top=12)`。
第 3、10、19、24 章里每一个"读了多少字节"的数字都是从它出来的。

`mini_lancedb/format/` 导出的是文件层：`LanceFileWriter` / `LanceFileReader` /
`inspect_lance_file` / `write_lance_file` / `read_lance_file`，
`Manifest` 及其读写函数，`Transaction` / `Operation` / `commit_manifest` /
`can_rebase` / `CONFLICT_MATRIX`，以及删除文件的
`DeletionFile` / `write_deletion_file` / `read_deletion_file` / `deletion_mask`。

异常体系不在这一节，单独拆到了 A.19——它有 18 个公开类，
`MiniLanceError` 是根。

### A.17 命名空间与权限

这两块在正文里只讲了个大概（挂在第 25 章），接口在
`mini_lancedb/namespace.py` 和 `mini_lancedb/permissions.py` 里。

`NamespaceClient(store, *, self_uri='', connection=None, manifest_path='_namespaces.json')`
是命名空间的门面：`create_namespace(namespace_path, mode=None, properties=None)`、
`drop_namespace(namespace_path, mode=None)`、`list_namespaces(namespace_path=None)`、
`get(namespace_path=None)`、`exists(namespace_path=None)`、`describe(namespace_path=None)`，
外加一组直通表的方法 `create_table` / `open_table` / `drop_table` / `list_tables`。
`Namespace` 是一个 dataclass，字段为 `path` / `properties` / `children` /
`tables` / `created_at`，另有 `name` / `full_path` 两个属性与
`from_json` / `to_json`。

权限侧，`Permission` 是一个枚举，取值 `SELECT` / `INSERT` / `UPDATE` /
`DELETE` / `ALTER` / `DROP` / `CREATE`。这张表列的是权限模型的四个类，
注意 `PermissionStore` 只管存储、`AccessController` 只管判定，职责是分开的：

| 类 | 职责 |
|---|---|
| `PermissionStore(store=None, *, path='_permissions.json')` | 授权记录持久化：`grant` / `revoke` / `get` / `effective` / `all_grants` / `save` / `clear` |
| `TablePermissions(principal, table='*', permissions=..., ...)` | 一条授权记录：`add` / `remove` / `allows` / `to_json` |
| `AccessController(store=None, *, enabled=True, superusers=None)` | 判定与过滤：`check` / `allows` / `effective` / `grant` / `revoke` / `filter_tables` / `with_tables_filtered` |
| `PermissionDenied` | 越权时抛的异常 |

`PermissionDenied` 和 `NamespaceNotFoundError` **都继承 `MiniLanceError`**
（实测 MRO 是 `PermissionDenied → MiniLanceError → Exception`），
但它们**不在 `errors.py` 的 `__all__` 里**——一个定义在
`mini_lancedb/permissions.py`、一个定义在 `mini_lancedb/namespace.py`，
各自跟着自己那一层走。所以 A.19 那棵树上**找不到这两个名字**，
它列的是 `errors.py` 导出的那 18 个。**"属于同一个异常体系"和
"列在同一张清单里"是两件事**——按 A.19 去 `except` 全部
`MiniLanceError` 能接住它们，按 A.19 去数名字会数漏。

### A.18 嵌入函数与重排器注册表

迷你实现顶层导出了嵌入函数的注册表 API：`get_registry() -> EmbeddingFunctionRegistry`、
装饰器 `register_embedding_function(alias=None, *, builtin=False)`、
`registered_embedding_functions() -> list`，以及类
`EmbeddingFunctionRegistry`。注册表是**进程内单例**——
`EmbeddingFunctionRegistry.get_instance()` 拿到的永远是同一个实例；
`register` / `get` / `get_function` / `names` / `functions` / `set_var` /
`get_var` 是它的常用方法，`parse_functions` / `get_table_metadata` 负责把
函数配置和表元数据互相转换（"表上用哪个模型"就靠它落盘再还原）。

实测 `registered_embedding_functions()` 返回
`['bow', 'char-ngram', 'cohere', 'fake', 'gemini', 'hashing', 'huggingface', 'jina', 'ollama', 'openai', 'sentence-transformers']`，
其中 `builtin_function_names()` 给出本机自带的四个：
`hashing` / `char-ngram` / `fake` / `bow`。

重排器注册表更小：`reranker_names() -> list` 返回
`['colbert', 'cross_encoder', 'linear', 'none', 'rrf']`；
`get_reranker(name=None, **kwargs)` 按名字取实例（不传名字拿到
`NoneReranker`）；`describe_rerankers()` 返回一份带中文说明的清单，
每条包含分数来源、是否免归一化、额外成本、默认参数、典型场景和失败模式。

### A.19 异常体系

`mini_lancedb/errors.py` 的 `__all__` 有 18 个公开名字，根异常是
`MiniLanceError`，其余都是它的直接或间接子类。分层如下：

- `MiniLanceError`——根。
  - `SchemaError`——schema 层面的问题。
  - `TypeMismatchError`——类型转换被拒（例如 `alter_columns` 改向量维度）。
  - `DataValidationError`——数据校验失败。
  - `StoreError`——存储层错误。
  - `CorruptedFileError`——文件损坏。
  - `NotSupportedError`——"真实库有、这里没有"的能力。
  - `TableNotFoundError` / `TableAlreadyExistsError`——表级。
  - `VersionNotFoundError` / `TagNotFoundError`——版本与标签。
  - `CommitConflict`——提交冲突（条件写失败）。
  - `QueryError`——查询层面的基类。
    - `ExpressionSyntaxError`——表达式语法错误。
    - `PlanError`——查询规划错误。
  - `IndexErrorBase`——索引层面的基类。
    - `IndexBuildError`——建索引失败。
    - `IndexNotFoundError`——索引不存在。

这些错误有一大批是**刻意**的：`alter_columns` 改向量维度抛
`TypeMismatchError`，而不是静默把 500 行 × 8 维当 1000 行 × 4 维读出来；
`NotSupportedError` 专门承接"真实库有这个能力、迷你实现没有"的场合
（例如对非向量列用默认的 `IVF_PQ` 建索引）。模块里另有两个辅助函数
`format_error_chain(exc)` 和 `first_of(...)`，用来把嵌套错误拼成可读的一串。

**这 18 个不是异常体系的全部。** `PermissionDenied`（在
`mini_lancedb/permissions.py`）和 `NamespaceNotFoundError`
（在 `mini_lancedb/namespace.py`）同样继承 `MiniLanceError`，
但**不在这个 `__all__` 里**——它们跟着自己的模块走。
**要写"接住所有迷你实现异常"的代码，`except MiniLanceError` 是对的，
按这份清单去枚举名字就会漏两个。** A.17 讲这两处时用的是同一个原则。

### A.20 哪里和真实库不一致

这一节是整份附录里最该先看的一段。下面按"接口形状"和"语义与物理布局"
两组，逐条说真实 LanceDB 的行为、迷你实现的行为，以及影响。

**接口形状上的差异。** 建索引时，真实库只收配置对象
（`create_index(config=IvfPq(num_partitions=16))`），迷你实现还接受字符串
（`config='IVF_PQ'`，或 `config='IVF_PQ', num_partitions=16`），所以字符串
写法搬不过去；真实库的 `create_index()` 立即返回、要 `wait_for_index()` 才等，
迷你实现是同步构建（`wait_timeout` 被接受但忽略），批量写入脚本里会出现
"索引还没建完就查"。过滤上，真实库没有 `postfilter()` 方法（用
`where(..., prefilter=False)`），迷你实现有，方法名搬不过去；`search()` 的
默认 `query_type` 真实库是 `'auto'`、迷你实现是 `'vector'`，字符串查询在
迷你实现里不会自动走 FTS。连接对象上，真实库有 `list_tables()` 且
`table_names()` 已弃用，迷你实现只有 `table_names()`，这条没有影响。
维护上，真实库的 `cleanup_old_versions` **没有** `dry_run`，迷你实现有
（教学补的）；真实库的 `optimize()` 返回 `None`，迷你实现**刻意返回一个
真统计对象**，拿返回值会被真实库打脸。模式演进上，真实库是
`add_columns(transforms=...)`、迷你实现是 `add_columns(new_columns=...)`，
关键字名不同；`tokenize()` 真实库返回 `FtsToken` 流、配置走散装关键字，
迷你实现返回 `list[Token]`、配置走 `TokenizerConfig`，返回值形状不同。
此外真实库有 `branches`（可写 ref），迷你实现只有 `tags`；真实库部分方法
带弃用警告、迷你实现不带，迁移时才会发现；真实库**没有** `MultivectorQuery`，
迷你实现有（教学类，原样 import 会失败）；`LinearCombinationReranker` 在
迷你实现里多一个 `normalize` 参数；`RRFReranker` 真实库参数名是大写
`K=60`，迷你实现主参数叫 `k`、`K` 作为只读属性保留；`to_lance()` 真实库
返回 `lance.LanceDataset`，迷你实现返回内建的 `Dataset`。

**统计接口上有两个方向相反的差异，容易被一句"形状一致"糊过去，
单独列出来。** `index_stats` 真实库返回 `IndexStatistics` 对象
（属性访问可用、也支持 `stats["num_unindexed_rows"]`），迷你实现只返回
**普通 dict**——按属性的写法会 `AttributeError`。`stats` 恰好反过来：
真实库返回普通 `dict`，迷你实现返回 `TableStatistics` 数据类（多了几个字段）。
**同一个"统计"两个字，一边是对象一边是字典，而且恰好错开。**
写跨两边的代码时，`index_stats` 用下标、`stats` 用属性，
这两个写法互换都会炸。第 25.1 节实测撞到过前一条。

**语义与物理布局上的差异。** 列元数据真实 Lance 用 FlatBuffers、迷你实现
用 JSON，元数据体积大一个量级（正文里量化过）；有效性位图真实 Lance 编进
页里、迷你实现放在列级，于是"只读某一页的 null 位图"做不到。行标识上，
真实 Lance 用稳定的 64 位行地址 `(fragment_id << 32) | offset`，迷你实现
用逻辑行号 `0..count_rows-1`，压实 / 删除后编号会移动；页大小真实 Lance 按
字节（默认目标 8 MiB），迷你实现默认每页 8192 个值，页的粒度不同。
索引实现上，真实 Lance 的 BTREE 是磁盘分页的（`page_data` + `page_lookup`），
迷你实现所有节点在内存里做真分裂，形状更接近教科书 B+ 树；NGRAM 真实
Lance 只存 gram → 行号、验证时回表，迷你实现多存一份原值，索引体积翻倍；
向量索引真实 Lance 不存原始向量、精确重排时回表，迷你实现索引自包含一份
向量副本，索引更大但没有独立数据层；压实后真实 Lance 索引失效就重建，
迷你实现会先尝试 `remap` 搬行号，搬不动才失效降级。分词上，CJK 真实
Lance 用 jieba / lindera 做真分词，迷你实现用字符二元组（bigram），中文
检索质量不可比；高压缩比那一档真实 Lance 是 Zstd、迷你实现用 zlib 代表；
数据文件真实 Lance 有多套后缀 / 命名规则，迷你实现用单一的自描述命名。

**还有一条不在上面的清单里，但性质比它们都严重，必须单独强调：NaN 的语义。**
它不是"实现细节不同"，而是**同一个查询会给出不同的行集**。
`NaN` 在真实库的 Arrow 比较里是"最大的值"（`NaN = NaN`
为 true、`NaN > 1.0` 也为 true），在我们这里是"未知"（任何一边是 NaN ⇒
结果为 NULL）。从迷你实现迁移到真实库时，任何涉及缺失值的过滤条件都要
重新验证一遍。

建索引配置要换成配置对象、索引构建是异步的、部分方法带弃用警告——这三处
正是附录 C 的 C.5 点名的"切换成本"。**两节是互补的、不是谁覆盖谁**：
C.5 从"迁移该怎么做"的角度写，列了六条必须知道的差异；
这一节从"哪里不一样"的角度写，覆盖面更广但每条更短。
做迁移前两节都要看，别只看一节就当自己知道全部差异。

除了上面这些，`mini_lancedb/` 的源码里还散布着九十多处
"这里和真实库不一样"的行内注释——覆盖到编码选择启发式、
删除文件编码、manifest 命名、LFU 缓存策略这些更细的地方。
需要逐条核对的时候，用这个命令把它们一次捞出来：

```bash
grep -rn "真实 Lance" mini_lancedb/ --include=*.py
```
