# 附录 B:mini-clickhouse 工程手册

第 31 章把 mini-clickhouse 当成一个"可以读的故事"讲了一遍——为什么这么设计、和真实 ClickHouse 差在哪。这份附录解决的问题完全不同:**当你想查"这个实现里 X 是怎么做的"时,翻这里。** 它由六部分组成:真实的文件树(带行数)、逐功能的能力矩阵、照源码抄下来的 API 签名、三个"怎么加一个新东西"的步骤模板、验证手段,以及一份可扫读的陷阱清单,最后是九个练习。

**所有数字都来自一次统一的快照**(`minich/` 下 123 个 `.py` 文件、86 242行;快照时刻 2026-10-16)。这个实现还在演进——我在写这一章的过程中,它就长了好几轮(从 80 450 行到 85 061 行)。所以数字会变,结论不会。要重新数一遍:

```bash
cd mini-clickhouse/minich
find . -name '*.py' -not -path '*__pycache__*' | xargs wc -l | tail -1     # 总行数
find . -name '*.py' -not -path '*__pycache__*' | wc -l                     # 文件数
cd .. && python -c "
from minich.functions.base import registered_functions
from minich.aggregates.base import registered_aggregate_functions
from minich.storages.base import registered_storage_engines
from minich.formats.base import registered_formats
from minich.types.base import registered_type_names
from minich.storages.table_functions import registered_table_functions
from minich.settings import registered_settings
from minich.system import system_table_names
print('functions  ', len(registered_functions()))
print('aggregates ', len(registered_aggregate_functions()))
print('engines    ', len(registered_storage_engines()))
print('formats    ', len(registered_formats()))
print('types      ', len(registered_type_names()))
print('table funcs', len(registered_table_functions()))
print('settings   ', len(registered_settings()))
print('system tbls', len(system_table_names()))
"
```

## B.1 目录结构

下面的文件树是**真实的**——每一行都是 `find` + `wc -l` 的输出,不是示意。行数写在文件名后面。

```text
minich/                       16 个文件  16 993 行   包级入口、注册表、以及"不属于任何子包"的核心设施
```

```text
      116  __init__.py                 包级导出;TARGET_CLICKHOUSE_VERSION = "25.3"
        41  __main__.py                  python -m minich 的入口
      331  errors.py                    ErrorCode 枚举 + MiniChError + raise_error()
      546  block.py                     ColumnWithTypeAndName / Block / Chunk
      557  catalog.py                   数据库与表的目录(DatabaseCatalog / Table / StorageID)
  1 369  access.py                    用户、角色、权限、行策略、配额
  1 109  client.py                    客户端库(Client / MinichServer 的会话侧)
      978  cli.py                       clickhouse-client / -local / -benchmark 的迷你版
  2 100  compression.py               编解码器:None/LZ4/LZ4HC/ZSTD/Delta/DoubleDelta/Gorilla/T64/FPC/Multiple
  2 374  dictionaries.py              外部字典:源 / 布局 / 生命周期 / dictGet
  2 415  keeper.py                    迷你 Keeper(ZooKeeper 的对应物)—— 复制的地基
      338  literals.py                  文本字面量 → 值(类型感知的解析)
      656  protocol.py                  原生协议(消息序列 + Native 块编解码)
  2 364  replication.py               ReplicatedMergeTree 的协调状态机
      780  serialization.py             ISerialization 及 12 个具体实现
      919  settings.py                  124 条设置的注册表:类型 / 默认值 / 约束 / 级别
```

```text
minich/sql/                    4 个文件   5 115 行    SQL 前端(词法 → 语法 → AST)
        53  __init__.py
  1 936  ast_.py                     AST 节点定义 + to_sql() + format_ast()
      412  lexer.py                    文本 → Token(注释/空白在此丢弃,位置保留)
  2 714  parser.py                   递归下降 + 有界回溯,Token → AST
```

```text
minich/execution/             10 个文件  10 037 行    从 AST 到结果
        59  __init__.py
      699  context.py                  Context(会话)/ QueryResult(结果)/ QueryInfo
      840  analyzer.py                 表达式分析:聚合前 / 聚合 / 聚合后三段
      250  explain.py                  六种 EXPLAIN 的输出
  1 623  expression.py               Evaluator + Scope:AST 表达式 → IColumn
  2 871  interpreter.py              解释器工厂 + 每条语句的编排(执行层最大的文件)
      656  joins.py                    哈希连接、strictness、ASOF
      318  optimizer.py                四条确定性改写规则(见 B.6:已被解释器调用)
  2 094  processors.py               IProcessor / Port / Status 四态 + 各类处理器
      627  window.py                    窗口函数(阻塞算子)
```

```text
minich/storages/              18 个文件   5 277 行    表引擎
        83  __init__.py                  "导入即注册"的汇总入口
      524  base.py                     IStorage / StorageFeatures / ReadContext / 引擎注册表
        42  disk.py                     对 merge_tree.disk 的再导出(抽象层级归位)
      507  _common.py                  多引擎共用的排序/去重/统计小工具
      259  file.py                     File       文件本身就是表(读时解析、写时重写整个文件)
      295  memory.py                   Memory     数据全在内存
      516  log.py                      Log / TinyLog / StripeLog   最简磁盘引擎
      107  null.py                     Null       吞掉写入
      175  set.py                      Set        只存第一列去重值
      257  join.py                     Join       内存哈希表(joinGet 用)
      399  buffer.py                   Buffer     内存攒批再落盘
      182  merge.py                    Merge      多表合一(只读)
      241  url.py                      URL        远端 HTTP 文件即表
      161  values.py                   Values     字面量即表
      408  view.py                     View / MaterializedView
      138  numbers.py                  numbers() 表函数
      130  system_numbers.py           system.numbers / numbers_mt
      853  table_functions.py          30 个表函数的注册与实现
```

```text
minich/storages/merge_tree/   13 个文件  12 997 行    唯一"真正的"存储引擎
      265  __init__.py                 引擎注册 + 学习顺序说明
      433  disk.py                     IDisk / DiskLocal:原子写、move、临时目录
      568  marks.py                    granule / mark / 固定与自适应粒度
  2 116  part.py                     part 的磁盘布局与读写(这个子包里最厚的两个文件之一)
  1 375  key_condition.py            WHERE → key range(稀疏索引二分)
  1 554  skip_index.py               跳过索引:minmax / set / bloom_filter / ngrambf_v1 / tokenbf_v1
      242  merge_policy.py             该合并哪几个 part(SimpleMergeSelector 三条规则)
      992  merger.py                   k 路归并 + 引擎家族的折叠语义
      403  mutations.py                ALTER UPDATE/DELETE 的执行模型
      475  ttl.py                      TTL:DELETE / TO DISK / RECOMPRESS
  3 072  merge_tree.py               MergeTreeData 本体(全项目最大的一个文件)
      501  engines.py                  八个引擎成员 + 注册
    1 001  expression.py               存储层自用的小求值器(分区裁剪与单调函数)
```

```text
minich/types/                  4 个文件   3 521 行    类型(元数据)
      687  __init__.py                  类型工厂:字符串 → DataType
      451  base.py                      DataType 抽象基类 + register_data_type()
  1 244  scalars.py                   UInt/Int/Float/Decimal/Date/String/Enum/UUID…
  1 139  composite.py                 Array/Tuple/Map/Nullable/LowCardinality/AggregateFunction…
```

```text
minich/columns/                4 个文件   2 430 行    列的物化(存储)
        58  __init__.py
      825  base.py                      IColumn / ColumnVector / ColumnBigInt / ColumnUUID
      385  string.py                    ColumnString / ColumnFixedString
  1 162  composite.py                 Nullable/Array/Tuple/Map/LowCardinality/Sparse/Const/AggregateFunction
```

```text
minich/functions/             19 个文件  15 522 行    标量函数库(全项目最大的包)
        94  __init__.py                  "导入即注册"的汇总入口(没有对外 API)
      536  base.py                      FunctionBase + register_function() + NULL 传播
  1 880  arithmetic.py                算术、位运算、类型提升规则(最大的函数模块)
  1 412  string.py                    字符串(含 length 与 lengthUTF8 的区别)
  1 457  datetime.py                  日期时间与时区
  1 604  type_funcs.py                类型转换(toXxx / reinterpretAsXxx / CAST / toString)
  1 194  array.py                     数组函数(1-based 下标)
  1 104  misc.py                      杂项(now / version / bar / neighbor)
      923  math_funcs.py                数学
      726  json_funcs.py                visitParam* 与 JSONExtract* 两族
      709  comparison.py                比较、ifNull/nullIf、greatest/least
      668  url_ip.py                    URL 与 IP
      637  rounding.py                  取整与舍入
      606  hashing.py                   哈希(sipHash / xxHash / MD5 …)
      515  tuple_map.py                 元组与 Map
      495  conditional.py               惰性的 if/multiIf、coalesce
      465  higher_order.py              高阶函数(arrayMap 等,收 lambda)
      282  logical.py                   三值逻辑 and/or/not
      215  encoding.py                  hex / base64 / bin 编解码
```

```text
minich/aggregates/            13 个文件   4 818 行    聚合函数与组合器
        79  __init__.py
      835  base.py                      IAggregateFunction + register_aggregate() + 组合器解析
      726  combinators.py               9 个组合器后缀的实现
      391  sum.py                      sum / sumKahan / sumWithOverflow / sumMap
      148  count.py                    count / countDistinct
      139  average.py                  avg / avgWeighted
      125  minmax.py                   min / max
      210  any.py                      any / anyLast / anyHeavy
      457  uniq.py                     uniq / uniqExact / uniqCombined / uniqHLL12 / uniqUpTo
      269  quantile.py                 quantile / quantiles / quantileExact / quantileTDigest / …
      130  argminmax.py                argMin / argMax
      413  group_array.py              groupArray / groupArrayLast / groupArrayInsertAt / groupArraySample
      896  statistics.py               stddev / var / corr / covar / skew / kurt / topK / maxIntersections
```

```text
minich/formats/                7 个文件   3 868 行    格式(FORMAT 子句)
      468  __init__.py                  注册表 + get_input_format / get_output_format
      752  base.py                      IInputFormat / IOutputFormat 接口
      712  binary_formats.py            RowBinary* / Native
      769  json_formats.py              JSON / JSONCompact / JSON*EachRow
      509  pretty_formats.py            Pretty* / Vertical / Markdown / SQLInsert / Null
      474  text_formats.py              TSV* / CSV*
      184  values_format.py             Values
```

```text
minich/system/                 8 个文件   4 316 行    system 库(43 张表)
      120  __init__.py                  四个对外接口 + "哪些是真数据"的说明
      485  base.py                      ISystemTable / SimpleSystemTable / 注册表
  1 202  system_tables.py             注册表 & 配置族(functions / settings / formats / clusters / 权限…)
  1 228  tables.py                    结构 & 元数据族(databases / tables / columns / parts / part_log…)
      538  logs_tables.py               运行时日志族(query_log / processes / text_log / errors / crash_log…)
      370  metrics_tables.py            计数器与 gauge(metrics / events / asynchronous_metrics)
      268  replication_tables.py        replicas / replication_queue / zookeeper…
      105  helpers.py                   辅助表(one / numbers / numbers_mt)
```

```text
minich/util/                   7 个文件   1 348 行    通用工具(不依赖任何上层)
        90  __init__.py
      272  hashing.py                   SipHash / xxHash / CRC(真实算法)
      150  keys.py                      分组键、排序键描述(KeyDescription)
      240  strings.py                   TSV/CSV/SQL 转义、标识符引号、可读大小
      280  stopwatch.py                 Stopwatch / ProfileEvents / MetricsRegistry
      223  logger.py                    system.text_log 的写入端
        93  json_text.py                 JSON 编解码(含 NaN/Inf 的 ClickHouse 语义)
```

配套文件(不在 `minich/` 包内):

```text
README.md                 项目说明 + 能力清单 + "验证状态"一节
pyproject.toml            打包元数据;pytest 配置(testpaths=["tests"])
conftest.py               让 import minich 在任何工作目录下都能工作
tools/check_docs.py       教程正文的 20 项结构自检(见 B.5.3)
tests/                    18 个 test_*.py(见 B.5.1)
examples/                 14 个自包含示例脚本(见 B.5.2)
```

包外的**源文件**只有这些(`__pycache__/`、`.pytest_cache/` 这类由工具生成的
缓存目录不计)。没有遗留的临时目录、没有空的占位文件、没有"以后再说"的
脚本 —— 一个教学项目最容易被这类东西拖脏,而它们的存在本身就在削弱
"这份代码是工程化写的"这个说法。

## B.2 能力矩阵

状态的含义:**已实现** = 有可用实现且能跑;**部分实现** = 能跑但有明确的范围限制或已知差异;**未实现** = 调用会抛 `NOT_IMPLEMENTED`(或根本没有入口)。第三列回答"如果实现了,在哪;如果没实现,为什么"。

### B.2.1 数据模型:类型、列、序列化

| 功能 | 状态 | 位置 / 原因 |
| --- | --- | --- |
| 类型注册表与类型工厂 | 已实现 | `types/__init__.py` 的 `get_data_type` / `try_get_data_type`;`registered_type_names()` 返回 56 个名字 |
| 数值类型(Int/UInt 8..256、Float32/64、Decimal) | 已实现 | `types/scalars.py`;`Int128/256`、`UInt128/256` 由 `ColumnBigInt` 承载 |
| 布尔 / 日期时间(Date、Date32、DateTime、DateTime64) | 已实现 | `types/scalars.py`;时区支持在 `functions/datetime.py` |
| 字符串(FixedString、Enum8/16、UUID、IPv4、IPv6) | 已实现 | `types/scalars.py` + `columns/string.py` |
| 复合类型(Array、Tuple、Map、Nested、Nullable、LowCardinality) | 已实现 | `types/composite.py` + `columns/composite.py` |
| `Variant` / `Dynamic` / `JSON` / `Object` | 部分实现 | 名字已注册,`allow_experimental_variant_type` 等设置默认 1;解析与基本读写可用,但**不完整**——真实 25.3 的 `JSON` 有多路径与类型推断,这里没有 |
| `AggregateFunction` / `SimpleAggregateFunction` | 已实现 | `types/composite.py` + `columns/composite.py` 的 `ColumnAggregateFunction` |
| `Nothing` 类型 | 已实现 | 供 `NULL` 字面量与空数组元素用 |
| `IColumn` 接口与 `ColumnVector` | 已实现 | `columns/base.py`;`ColumnVector` 用 `np.ndarray` + 容量倍增(照 `PaddedPODArray`) |
| 编码列(`LowCardinality`、`Sparse`、`Const`) | 已实现 | `columns/composite.py`——注意这里只有**列级**编码,没有对应的序列化级实现 |
| `ISerialization` 及 12 个具体实现 | 已实现 | `serialization.py`;Number / BigInt / UUID / FixedString / String / Nullable / Array / Tuple / Map / LowCardinality / AggregateFunction / Nothing |
| 同一类型多种序列化(`SerializationInfo`) | 未实现 | 真实 ClickHouse 在 part 元数据里记录"这一列用了哪种序列化";这里一个类型只对应一种序列化,写在 `serialization.py` 的 note 里 |
| `serialize_binary_bulk_*` / `create_bulk_state` | 部分实现 | 接口与 `SerializeBinaryBulkState` 都在,但多数类型不返回 bulk state(返回 `None` 走通用路径) |

### B.2.2 压缩与编码

| 功能 | 状态 | 位置 / 原因 |
| --- | --- | --- |
| `NONE` / `LZ4` / `LZ4HC` | 部分实现 | `compression.py` 的 `CompressionCodecNone` / `CompressionCodecLZ4` / `CompressionCodecLZ4HC`;**自研 LZ77,格式与真实 lz4 不兼容**(多了 6 字节魔数 `MCHLZ1` + 4 字节原始长度) |
| `ZSTD(level)` | 部分实现 | `CompressionCodecZSTD`;自研 LZ77 + 自适应算术编码,魔数 `MCHZS1`,**与 libzstd 不兼容** |
| `Delta(N)` / `DoubleDelta` / `Gorilla` / `T64` / `FPC` | 已实现 | 这五个是**真算法**;但 `Delta` **没有照抄**真实 ClickHouse 的字节布局——它是在**逐字节平面**上做一阶差分,与上游"在整数值上做"同思想、算法是真的,但**字节不兼容官方库**,属一处**已知偏差**(`compression.py` 模块 docstring 明说"不要期待与官方库字节兼容") |
| `CODEC(a, b, …)` 列级声明与链式校验 | 已实现(有一处刻意简化) | 建表时写在列上的 CODEC 会被**真正应用**:写入路径共用 `make_write_context` 一处解析列定义,所以 INSERT、后台合并、mutation 重写三条路径都带上它。链会经 `parse_codec_chain` → `_validate_chain` **校验**("专用必须跟通用"),因此 `CODEC(Delta)` 这类写法会被**拒绝**(`ILLEGAL_TYPE_OF_ARGUMENT`,Code 24),与真实 ClickHouse 一致。**简化之处**:磁盘上一个压缩块只带**一个** method 字节,表达不了整条链,所以实际压缩时取的是**链尾那个通用编解码器**——`CODEC(Delta, ZSTD(3))` 会用 ZSTD 压,**跳过 Delta 预变换**。即"压缩真的发生了、且发生了但用的是链尾算法",不是"声明被忽略" |
| `CompressedWriteBuffer` / `CompressedReadBuffer` | 已实现 | `compression.py`;每个压缩块带头部 |
| 方法字节与真实约定一致(0x02/0x82/0x83/0x84/0x85/0x86/0x87/0x90) | 已实现 | `CompressionMethodByte`——字节值与算法**不对应于**真实库的字节流 |
| `benchmark_codecs()` | 已实现 | `compression.py` 末尾,用四类合成列(单调时间戳/随机 UInt64/低基数串)对比各编码器 |

### B.2.3 格式

共 **24 个**注册格式。可读可写的是 TSV 族(3)、CSV 族(2)、`Values`、JSON 族(6)、RowBinary 族(3)、`Native`;只可写的是 `Pretty` 族(4)、`Vertical`、`Markdown`、`SQLInsert`、`Null`。注册表在 `formats/__init__.py`(`registered_formats()` 返回 `(名字, 可读, 可写, 说明)`)。

| 功能 | 状态 | 位置 / 原因 |
| --- | --- | --- |
| TSV / TSVWithNames / TSVWithNamesAndTypes | 已实现 | `formats/text_formats.py`;`NULL` 是 `\N`,可无损往返 |
| CSV / CSVWithNames | 已实现 | 同上;RFC-4180 引号规则 |
| Values | 已实现 | `formats/values_format.py` |
| JSON / JSONCompact / JSON*EachRow(6 种) | 已实现 | `formats/json_formats.py` |
| RowBinary / RowBinaryWithNames / RowBinaryWithNamesAndTypes | 已实现 | `formats/binary_formats.py` |
| Native | 部分实现 | `formats/binary_formats.py`;**与真实 Native 不兼容**:不写 `BlockInfo` 的 5 个字节、没有 custom-serialization 标志字节、每列多一个长度字段——读到真实 Native 文件会解析出垃圾 |
| Pretty / PrettyCompact / PrettyCompactMonoBlock / PrettySpace / Vertical / Markdown / SQLInsert / Null | 已实现(只写) | `formats/pretty_formats.py` |
| 输入格式的"猜格式" | 未实现 | 真实 ClickHouse 在 `FORMAT` 缺失时能按扩展名/内容推断;这里必须显式写 |

### B.2.4 函数与聚合函数

| 功能 | 状态 | 位置 / 原因 |
| --- | --- | --- |
| 标量函数 | 已实现 | `functions/` 19 个模块;`registered_functions()` 返回 **658** 个名字(含别名) |
| 标量函数的向量化 | 部分实现 | `FunctionBase.execute` 是向量化入口,默认实现**逐行回退**;数值/比较/逻辑/取整等模块覆盖了 `execute` 走 NumPy,其余多数是逐行 |
| 高阶函数(arrayMap 等,收 lambda) | 已实现 | `functions/higher_order.py` |
| 聚合函数(基础名) | 已实现 | `registered_aggregate_functions()` 返回 **65** 个基础名 |
| 聚合组合器 | 已实现 | 9 个后缀:`Merge`、`State`、`Resample`、`OrNull`、`OrDefault`、`Distinct`、`Array`、`ForEach`、`If`;实现在 `aggregates/combinators.py`,解析在 `base.py` 的 `split_combinators`。一个 `sum` 因此派生出十几个可用名 |
| `quantile` 族 | 已实现 | `aggregates/quantile.py`;`quantileExact` / `quantileTDigest` / `quantileTiming` 等 |
| `uniq` 族与 HyperLogLog | 已实现 | `aggregates/uniq.py`;`uniqCombined` / `uniqHLL12` / `uniqExact` / `uniqUpTo` |
| 统计类聚合(stddev / var / corr / covar / skew / kurt / topK / maxIntersections) | 部分实现 | `aggregates/statistics.py`;其中有一部分 `create_state` 会抛 `NOT_IMPLEMENTED_IN_MINICH`(见 B.6) |
| `-Resample` 组合器 | 部分实现 | 后缀在册,但 `Resample` 需要"按时间粒度重采样"的框架,实现只覆盖部分形态 |
| `sumMap` / `groupArrayInsertAt` 等冷门函数 | 已实现 | 各自的模块 |
| `AggregatingMergeTree` 用的状态序列化 | 已实现 | `IAggregateFunction.serialize_state` / `deserialize_state` + `SerializationAggregateFunction` |
| 聚合溢出写盘 / 两级哈希表 | 未实现 | `processors.py` 的 `_groups_overflow` 只抛 `TOO_MANY_ROWS`;真实 ClickHouse 会切 256 个桶并可选落盘 |

### B.2.5 SQL 语法与执行引擎

| 功能 | 状态 | 位置 / 原因 |
| --- | --- | --- |
| 词法 / 语法 / AST | 已实现 | `sql/lexer.py`(412 行)、`sql/parser.py`(2 696 行,递归下降 + 有界回溯)、`sql/ast_.py`(1 936 行) |
| `SELECT`(WHERE / PREWHERE / GROUP BY / HAVING / ORDER BY / LIMIT / OFFSET / DISTINCT / LIMIT BY / ARRAY JOIN / 子查询 / UNION ALL) | 已实现 | `execution/interpreter.py` 的 `InterpreterSelectQuery` |
| `INSERT`(VALUES / SELECT / FORMAT) | 已实现 | `InterpreterInsertQuery`;`INSERT INTO FUNCTION` 抛 `NOT_IMPLEMENTED` |
| `CREATE TABLE` / `DROP` / `ALTER` / `OPTIMIZE` / `RENAME` / `TRUNCATE` | 已实现 | `interpreter.py` 中对应的 `IInterpreter` 子类(见 `ast_type` 表) |
| `SHOW` / `DESCRIBE` / `EXISTS` / `USE` / `SET` / `SYSTEM` / `KILL` / `GRANT` | 部分实现 | 都有解释器;`SHOW` 只覆盖部分 `kind`,`CREATE USER` / `DROP USER` / `GRANT` / `CREATE QUOTA` 等权限 DDL 已经可用(见第 26 章),只有少数未接线的语句才抛 `NOT_IMPLEMENTED` |
| `EXPLAIN` 六种形态 | 部分实现 | `execution/explain.py` 支持 AST / SYNTAX / PLAN / PIPELINE / ESTIMATE / QUERY TREE;`SYNTAX` 会因为"优化器不改 AST"而输出与输入相同,并显式打印说明 |
| 处理器与四态状态机 | 已实现 | `execution/processors.py`:`Status` 的四个字面量与真实一致;`OutputPort.push()` 返回 `PushResult`(`NotNeeded` / `PortFull` / `NeedMoreData`) |
| 流水线执行 | 部分实现 | **分段执行**:阻塞算子物化中间 Block,单线程顺序驱动;`EXPLAIN PIPELINE` 会打印 `max_threads = 0` |
| 逻辑计划(`QueryPlan`)这一层 | 未实现 | 真实 ClickHouse 的 `EXPLAIN PLAN` 读的是 `QueryPlan` 树;这里打印的是"分段执行"的步骤列表 |
| 优化器规则 | 部分实现 | `execution/optimizer.py` 实现四条规则,`optimize_select()` **已被调用**;其中只有 `optimize_trivial_count_query` 真正改变了读的字节数。`limit_push_down` 会把 LIMIT 算进优化结果,但那个值没有继续传到存储层;`move_to_prewhere` / `optimize_read_in_order` 连触发条件都取不到(找不到排序键列名),一次都不会命中(见 B.6) |
| 三次裁剪(分区 / 主键 granule / 列) | 部分实现 | 机制全在存储层,输入是 `ReadContext.where` / `prewhere`(分区走 `_prune_partitions`,主键 granule 走 `_mark_ranges_for_part` + `key_condition.py`,列走 `_plan_columns`)。`ReadContext` 上的 `columns` / `key_analysis` / `partition_ids` / `limit` 是"调用方已经算好了就填进来"的透传位,空着不代表裁剪失效。**生效状态随快照变化**,一次实测的汇总见 §B.6.2;量法见第 31 章 §31.5 |
| 谓词下推 / PREWHERE 自动提升 | 部分实现 | 解释器把 `WHERE` / `PREWHERE` 原样放进 `ReadContext.where` / `prewhere` 交给存储层——这一步是整个"索引为什么有用"链条的入口,没有它三条裁剪一条也不生效。优化器里那条 `move_to_prewhere` 规则反而是不生效的(见 B.6) |
| 代价模型(CBO) | 未实现 | 顶层 docstring 明确写了这是刻意的选择:规则式更可解释、不需要统计信息 |
| 子查询(标量 / IN / FROM 子查询) | 已实现 | `expression.py` 的 `eval_subquery` + 解释器注入的 `subquery_executor` |

### B.2.6 JOIN、窗口、排序与去重

| 功能 | 状态 | 位置 / 原因 |
| --- | --- | --- |
| 哈希连接 | 已实现 | `execution/joins.py`;`kind` 支持 `INNER` / `LEFT` / `RIGHT` / `FULL` / `CROSS`,`strictness` 支持 `ALL` / `ANY` / `ASOF` / `SEMI` / `ANTI` |
| 默认 strictness | 已实现 | **`ALL`**(`join_default_strictness = 'ALL'`)——与"ClickHouse 默认 ANY"的流行说法不同,`joins.py` 的 docstring 专门澄清了这一点 |
| 非等值连接(只有 `ON a > b` 这类条件) | 未实现 | 会抛 `NOT_IMPLEMENTED`,原因写在 `joins.py`:**没有等值条件就没有哈希键** |
| `GLOBAL JOIN` / 分布式 JOIN | 未实现 | 没有分布式层 |
| 窗口函数 | 已实现 | `execution/window.py`;`row_number` / `rank` / `dense_rank` / `percent_rank` / `cume_dist` / `ntile` / `lag` / `lead` / `lagInFrame` / `leadInFrame` / `first_value` / `last_value` / `nth_value` 等 |
| 窗口帧 `ROWS` | 已实现 | 三种常见形态(累计 / 滑动 / 整分区) |
| 窗口帧 `RANGE` | 部分实现 | **被当成 `ROWS` 处理**——有并列值时结果与真实 ClickHouse 不同 |
| 窗口的可增量帧优化、已有序则跳过排序 | 未实现 | 一律全排序 + 逐位置扫 |
| `ORDER BY`(多列、`ASC/DESC`、`NULLS FIRST/LAST`) | 已实现 | `interpreter.py` 的排序阶段 |
| `DISTINCT` | 已实现 | 整行去重;`DISTINCT ON (列)` 未实现 |
| `LIMIT BY` | 已实现 | 解释器支持 |
| `LIMIT n OFFSET m` | 已实现 | 有 `LimitTransform` / `OffsetTransform`;`LIMIT` 下推到存储层的规则写在 `optimizer.py` 但未接线 |

### B.2.7 存储引擎

`registered_storage_engines()` 返回 **23** 个引擎。注意这份清单里**没有 `Distributed`**。

| 引擎 | 状态 | 说明 |
| --- | --- | --- |
| `MergeTree` | 已实现 | 核心引擎。追加写 + 后台合并 + 稀疏索引 + 分区 + 跳数索引 + TTL + mutation |
| `ReplacingMergeTree([ver])` | 已实现 | 同键只留 version 最大的那行;`ver` 省略时留最后写入的 |
| `SummingMergeTree([cols])` | 已实现 | 同键把数值列相加 |
| `AggregatingMergeTree()` | 已实现 | 同键把 `AggregateFunction` 的**状态** merge 起来 |
| `CollapsingMergeTree(sign)` | 已实现 | 按 `sign` 配对抵消 |
| `VersionedCollapsingMergeTree(sign, ver)` | 已实现 | 同上,但按 version 配对 |
| `GraphiteMergeTree(...)` | 部分实现 | 引擎已注册且能建表,但 **rollup 规则不生效**——引擎说明里直接写着 "registered but NOT applied in mini-clickhouse" |
| `ReplicatedMergeTree` | 已实现 | 通过 Keeper 协调;`ReplicatedReplacingMergeTree` 是保留的别名 |
| `Memory` | 已实现 | 无索引,重启即失 |
| `Log` / `TinyLog` / `StripeLog` | 已实现 | 最简磁盘引擎,只允许追加 |
| `Null` | 已实现 | 吞掉一切写入 |
| `Set` | 已实现 | 只存第一列的去重值(`IN` 半连接用) |
| `Join` | 已实现 | 内存哈希表,供 `joinGet()` 与预建 JOIN 右表用 |
| `Buffer` | 已实现 | 内存攒批,达到阈值后刷入目标表 |
| `Merge` | 已实现 | 把同库下匹配正则的多张同构表当成一张只读表 |
| `View` / `MaterializedView` | 已实现 | 普通视图查询时重跑子查询;物化视图是**插入触发器**(支持 `POPULATE`),不是定时刷新 |
| `File` / `URL` / `Values` | 已实现 | 文件/网络/字面量即表 |
| `Distributed` | 未实现 | 23 个引擎里没有它。分片的**钩子**在(`ctx.clusters`、`ctx.remote_executor`、表函数 `cluster` / `remote`),但**没有任何代码去填它们** |
| 存储特性声明(`StorageFeatures`) | 已实现 | `storages/base.py`;12 个布尔位(primary_key / partitions / sorting_key / parallel_read / skip_indexes / ttl / mutations / merges / replication / append_only / sampling / projections),让优化器不必用 `isinstance` 判断引擎类型 |
| 投影(PROJECTION) | 未实现 | 子句能解析、声明会被存进 `MergeTreeData.projections`,但**读路径完全不看它**;`StorageFeatures.projections = True` 只是个声明位 |

### B.2.8 MergeTree 的各个部件

这是本项目的重心。`storages/merge_tree/` 一个子包 12 762 行,占存储层代码的 70.7%。

| 部件 | 状态 | 位置 / 差异 |
| --- | --- | --- |
| 磁盘抽象 `IDisk` | 部分实现 | `merge_tree/disk.py`(经 `storages/disk.py` 再导出)。只有 `DiskLocal`;`DiskS3` / `DiskHDFS` 只存在于 docstring 的示意图里 |
| `atomic_write` / `move` | 已实现 | `DiskLocal.atomic_write` = 写 `tmp_<随机>` + `os.replace`;`move` 落到 `os.replace` |
| part 名解析与生成 | 已实现 | `format_part_name` / `parse_part_name`;只认 `<pid>_<min>_<max>_<level>` 四段式,不处理 `tmp_` 前缀 / `detached` / `.tmp` 后缀等变体 |
| part 写入(两阶段提交) | 已实现 | `write_part()`:排序 → 切 granule → 逐列序列化压缩 → 建索引 → 写进 `tmp_insert_<随机>` → `disk.move` 原子改名 |
| part 元数据文件 | 已实现 | `columns.txt` / `count.txt` / `partition.dat` / `primary.idx` / `checksums.txt` / `serialization.json` / `uuid.txt`,有 TTL 时写 `ttl.txt`。**不写** `defaults.mrk` |
| 崩溃恢复 | 已实现 | `_load_existing_parts()` 启动时删掉 `tmp` 开头的残留目录(对应 `clearOldTemporaryDirectories`) |
| 校验码 | 部分实现 | 写 size + **CRC-32(zlib)**;真实 ClickHouse 用 CRC-32C |
| `marks`(granule / mark / `IndexGranularity`) | 已实现 | `marks.py`;默认 `index_granularity = 8192`、`index_granularity_bytes = 10 MiB`,与真实一致 |
| `.mrk2` 三元组 | 部分实现 | 格式是 `(offset_in_compressed_file, offset_in_decompressed_block, rows_in_granule)`;但因为**一个 granule 一个压缩块**,第二项**恒为 0**。`mrk3` 的编解码函数也在,但写入用的是 `mrk2` |
| `primary.idx`(稀疏主键索引) | 已实现 | **列优先**存放每个 granule 的第一个键值 |
| `minmax_<col>.idx` | 已实现 | 只为分区键列与主键列写 |
| 跳数索引 | 部分实现 | `skip_index.py` 注册 **5** 种:`minmax` / `set(N)` / `bloom_filter` / `ngrambf_v1` / `tokenbf_v1`。与真实 ClickHouse 的**最大差异是建索引的时机**:真实在 merge 时才建,这里**写入时就建**(为演示直观)。真实额外还有全文检索索引(`text`/`inverted`)与实验性的 `vector_similarity`,这里都没有。注意 `hypothesis` 不是"真实有而这里没有"——它曾经是实验类型,已被上游移除(第 11 章 §11.9) |
| `may_be_true` 不变量 | 已实现 | 允许**错报真(false positive)**,绝不允许**漏报**(false negative)——对多列(元组)的 minmax 保守放行 |
| `KeyCondition`(WHERE → key range) | 已实现 | `key_condition.py`(1 375 行)。需要 `where` 非空;我最后一次量到它**在 SELECT 路径上已生效**(实测汇总见 §B.6.2) |
| 分区裁剪 | 已实现 | `_prune_partitions`(从 `ReadContext.where` 里抠分区键的比较,`rc.partition_ids` 非空时优先用它)。实测:69 个分区的表上查两个月只读了 **3** 个 part |
| 分区操作 | 已实现 | `DROP PARTITION` / `DETACH PARTITION`(挪到 `detached/`)/ `ATTACH PARTITION`(挪回来)/ `REPLACE PARTITION`(未实现,是 mini 的缺口) |
| 合并(k 路归并) | 已实现 | `merger.py`;最小堆 O(N log k),内存 O(k) |
| 合并策略 | 已实现 | `merge_policy.py` 的 `SimpleMergeSelector` 三条规则:段连续、段内最大 ≤ 最小 × `max_size_ratio`(默认 3)、总量 ≤ 150 GiB |
| 引擎家族的折叠语义 | 已实现 | 八个 `MergeTree` 成员(MergeTree / Replacing / Summing / Aggregating / Collapsing / VersionedCollapsing / Graphite / Replicated)**共用全部代码**,差别只有 `MergeTreeMergeConfig.family` 一个字段 |
| 后台合并线程 | 部分实现 | 单个 `threading.Thread`(名字 `MergeTree-merge-<表名>`),`start_merges()` / `stop_merges()` / `wait_for_merges()`;**不是**真实那样 `background_pool_size`(默认 16)的线程池,也没有并发合并 |
| 旧 part 的延迟删除 | 未实现 | `old_parts_lifetime`(默认 480)**只是声明,没有任何地方读它**;被取代的 part 立刻从磁盘删除,同时元数据对象进 `obsolete_parts` 字典,**只增不减** |
| mutation(`ALTER UPDATE/DELETE`) | 已实现 | `mutations.py`;异步、不原地改、在 part 参与合并时顺手应用;`system.mutations` / `system.part_log` 有数据 |
| mutation 的后台推进 | 部分实现 | 靠合并线程顺带推进;真实 ClickHouse 有独立的 mutation 池 |
| TTL(DELETE / TO DISK / RECOMPRESS / 列级) | 已实现 | `ttl.py`;`ttl.txt` 在写 part 时算好,以后判定是"读一个 JSON 比一次大小"。`ttl_only_drop_parts` 支持 |
| 约束(`CONSTRAINT ... CHECK`) | 部分实现 / 有缺陷 | 只在解析层实现;**任何带 CHECK 约束的表一 `INSERT` 就抛 `TypeError`**,根本用不了(根因见 B.6.2) |
| `FINAL` | 已实现 | 读取时归并(有额外开销) |
| `SAMPLE` | 未实现 | `StorageFeatures.sampling` 默认 `False` |

### B.2.9 分区、字典、复制、Keeper、权限、system 表、设置、客户端

| 功能 | 状态 | 位置 / 原因 |
| --- | --- | --- |
| `PARTITION BY` 与分区过滤 | 已实现 | 分区键值序列化成 part 目录名的第一段(`202401_1_1_0`);`format_partition_id` / `_compute_partition_value` |
| `DROP` / `DETACH` / `ATTACH PARTITION` | 已实现 | `merge_tree.py` 的 `drop_partition` / `detach_partition` / `attach_partition` |
| `REPLACE PARTITION` | 未实现 | 在 `merge_tree.py` 与 `interpreter.py` 里都找不到实现 |
| 外部字典:源 | 部分实现 | `dictionaries.py`;**可用**:`FILE` / `EXECUTABLE` / `HTTP` / `TABLE` / `IN-PROCESS` / `CLICKHOUSE`(**仅限本机**,远程会抛 `NOT_IMPLEMENTED`)。`MYSQL` / `POSTGRESQL` / `ODBC` / `JDBC` / `MONGODB` 等只保留名字,调用即报精确的 `NOT_IMPLEMENTED` |
| 外部字典:布局 | 已实现 | `flat` / `hashed` / `hashed_array` / `sparse_hashed` / `range_hashed` / `complex_key_hashed` / `direct` / `complex_key_direct` / `cache` |
| 外部字典:生命周期 | 已实现 | `LIFETIME(MIN/MAX)` |
| `dictGet` / `dictHas` 等 | 已实现 | `dictionaries.py` 末尾注册的一组函数 |
| 复制引擎 | 已实现 | `replication.py`(2 364 行);`ReplicatedMergeTree` 通过 Keeper 的 `/log` 队列协调 |
| 复制的仿真台 | 已实现 | `LocalCluster`——在**一个进程里**跑 N 个 Replica,共享一个 KeeperServer;支持 `kill_replica` / `leader` 重选 / `assert_converged` |
| 跨进程的真实复制 | 未实现 | 没有网络协议上的副本会话;`LocalCluster` 是本进程内的仿真,进程重启会重建全部内存状态 |
| Keeper | 已实现 | `keeper.py`(2 415 行);完整的 ZNode 树、会话、watch、`CreateMode`、错误码族(与 ZooKeeper 对齐) |
| Keeper 的多数派/持久化 | 部分实现 | 是**单进程内**的一致性服务,没有 Raft 选举与磁盘日志 |
| 用户 / 角色 / 权限 / 行策略 / 配额 | 部分实现 | `access.py`(1 369 行);注册表齐全(B.1 里 system 表的 `users` / `roles` / `grants` / `quotas` / `row_policies` / `settings_profiles` / `current_roles` 都有数据来源),但**默认不强制**——`Context.user` 是一个被完全信任的字符串,没有握手、密码校验、会话令牌 |
| system 表 | 已实现 | **43 张**。`system/__init__.py` 把它们分成三类:真实有数据、结构对但来源可选、结构对但**故意恒空**(如 `query_thread_log` / `crash_log` / `contributors`)——最后一类不伪造数据 |
| `system.parts` | 已实现 | `tables.py`;数据全部来自存储引擎自己的 part 清单,`Memory`/`Log`/`Null` 等没有 part 的引擎在这里一行都不出现 |
| 设置 | 已实现 | **124 条**,每条带类型、默认值、`min`/`max` 约束、级别(Regular/Important/Obsolete)、是否只读、别名;`SET max_threads = 'abc'` 会按类型报错 |
| 设置的层级覆盖 | 已实现 | 会话级 + 查询级(`Context.clone()` 派生一份独立 settings);`settings.apply_dict(..., scope="query")` |
| 客户端库 | 部分实现 | `client.py`(1 109 行)+ `protocol.py`(656 行)+ `cli.py`(978 行);**端到端会话已验证**(起服务端 → `Client.connect()` → 查询/建表/流式插入/`ping` 全部通过),但报文字节布局与真实 ClickHouse 不兼容(见 B.6) |
| 表函数 | 部分实现 | **30 个**名字;可用的是 `numbers` / `zeros` / `null` / `file` / `url` / `format` / `values` / `view` / `input` / `generateRandom` / `merge` / `system.numbers`;`dictionary` / `executable` / `jdbc` / `mysql` / `odbc` / `postgresql` / `sqlite` / `hdfs` / `s3` / `s3Cluster` / `getClientHTTPHeader` / `timeSeries*` 注册了名字但抛精确的 `NOT_IMPLEMENTED`,并在消息里说明原因 |
| 分片代理表函数 | 部分实现 | `cluster` / `clusterAllReplicas` / `remote` 会把请求委托给 `ctx.remote_executor`——**但没有任何代码去设置它** |
| HTTP 接口 | 未实现 | 只有 TCP 上的自定义协议,没有 HTTP 端点 |
| 分布式 `DDL` / `ON CLUSTER` | 未实现 | 没有集群拓扑 |

## B.3 API 参考

签名照源码抄写(省掉 `self`,默认值保留)。**只列你会真正调用的部分**;内部辅助方法在第 31 章 §31.15 给的路径里回查。

### B.3.1 `Context` —— 会话(`execution/context.py`)

```python
class Context:
    def __init__(self, data_path: Optional[str] = None, database: str = "default",
                 user: str = "default", settings: Any = None,
                 catalog: Optional[DatabaseCatalog] = None) -> None
    def clone(self, **overrides: Any) -> "Context"
    def with_settings(self, settings: Any) -> "Context"
    def set_current_database(self, database: str) -> None
    def new_query_id(self) -> str
    def execute(self, sql: str, settings: Optional[Dict[str, Any]] = None) -> QueryResult
    def execute_many(self, *statements: str) -> List[QueryResult]
    def show(self, sql: str, format_name: str = "PrettyCompact") -> str
    def print(self, sql: str, format_name: str = "PrettyCompact") -> None
    def create_table(self, name: str, columns_sql: str, engine_sql: str = "Memory",
                     database: Optional[str] = None) -> Table
    def insert(self, table: str, rows: Sequence[Sequence[Any]],
               columns: Optional[Sequence[str]] = None) -> QueryResult
    def insert_block(self, table: str, block: Block) -> None
    def shutdown(self) -> None
    def uptime(self) -> int
    def stats(self) -> Dict[str, Any]
```

| 成员 | 用途 |
| --- | --- |
| `__init__` | 建一个"服务端"。`data_path=None` 表示纯内存(引擎自行降级);给了路径则 MergeTree 的 part 落盘 |
| `execute(sql, settings=None)` | **对外主入口**。`settings` 是"每条查询附带的设置",优先级高于会话级 |
| `execute_many(*statements)` | 依次执行;任一条失败即抛出,**前面的不会回滚** |
| `show(sql, format_name)` / `print(...)` | 执行并直接返回/打印文本——写实验脚本时最顺手 |
| `create_table(name, columns_sql, engine_sql, database=None)` | 一句话建表;`engine_sql` 以 `mergetree` 开头且没写 `ORDER BY` 时自动补 `ORDER BY tuple()` |
| `insert(table, rows, columns=None)` | 按行插入,内部做"值 → SQL 字面量 → 解析回来"的往返。**不是高性能路径**,别用它插一百万行 |
| `insert_block(table, block)` | 直接写一个 `Block`,绕过 SQL 解析——**这才是快路径** |
| `clone(**overrides)` | 派生一个共享目录/统计、但**有独立 settings** 的子上下文;查询级设置覆盖就是这么实现的 |
| `shutdown()` | 关闭所有引擎(停后台线程、flush buffer)。**幂等**,可以重复调用 |

`Context` 上还有几个直接对应 system 表的公开字段:`profile_events`(`system.events`)、`metrics`(`system.metrics`)、`query_log`(`system.query_log`)、`processes`(`system.processes`)、`catalog`、`disk`、`settings`、`keeper`、`clusters`、`remote_executor`、`dictionaries`、`access`。

### B.3.2 `QueryResult` —— 结果(`execution/context.py`)

```python
class QueryResult:
    def __init__(self, ctx: "Context", query_id: str, header: Optional[Block] = None,
                 format_name: str = "PrettyCompact", info: Optional[QueryInfo] = None,
                 source: Optional[Iterator[Block]] = None) -> None
    # 消费
    def stream(self) -> Iterator[Block]
    def blocks(self) -> List[Block]
    def rows(self) -> List[Tuple[Any, ...]]
    def first_row(self) -> Optional[Tuple[Any, ...]]
    def row_count(self) -> int
    def scalar(self) -> Any
    def to_block(self) -> Block
    def column_names(self) -> List[str]
    def column_types(self) -> List[str]
    def text(self, format_name: Optional[str] = None) -> str
    def as_dataframe(self) -> Any
    # 诊断
    def profile_events(self) -> Dict[str, int]
    def elapsed_seconds(self) -> float
```

| 成员 | 用途 |
| --- | --- |
| `stream()` | **流式**逐个 Block 产出(会消费底层迭代器);适合大结果集 |
| `blocks()` | 把结果**全部物化**;两条路径共用同一份缓存,所以"先 stream 再 rows"不会重跑查询 |
| `rows()` | 拉平成 `[(v1, v2), ...]`——**测试里最常用的形式** |
| `scalar()` | 单行单列结果的便捷取值(`SELECT count() ...`) |
| `text(format_name=None)` | 按格式渲染成文本;默认用查询里写的 `FORMAT`,没有就用 `PrettyCompact`(与 `clickhouse-client` 一致) |
| `column_names()` / `column_types()` | 优先取 `header`,没有才去物化结果 |
| `as_dataframe()` | 转 pandas `DataFrame`;没装 pandas 会抛明确的 `NOT_IMPLEMENTED` |
| `profile_events()` / `elapsed_seconds()` | 诊断信息,来自 `QueryInfo` |

注意:这些**都是方法而不是属性**,写 `r.row_count` 会拿到绑定方法本身。`QueryResult` 还实现了 `__iter__` 与 `__len__`,所以 `for row in result:` 与 `len(result)` 都能用。

### B.3.3 `Evaluator` 与 `Scope`(`execution/expression.py`)

```python
class Evaluator:
    def __init__(self, ctx: Any, scope: Scope, rows: int = 1,
                 aggregate_result: Optional[Dict[int, str]] = None,
                 subquery_executor: Optional[Callable[[A.SelectQuery], "Any"]] = None,
                 lambda_scope: Optional[Scope] = None,
                 expression_lookup: Optional[Any] = None) -> None
    def evaluate(self, node: A.Node) -> ColumnWithTypeAndName
    def evaluate_all(self, nodes: Sequence[A.Node]) -> List[ColumnWithTypeAndName]
    def call_function(self, name: str, args: Sequence[ColumnWithTypeAndName],
                      node: Optional[A.Node] = None, params: Sequence[Any] = ()) -> ColumnWithTypeAndName
```

| 参数 | 用途 |
| --- | --- |
| `rows` | 当前 block 的行数;常量列(如 `ColumnConst`)要撑到这个行数 |
| `aggregate_result` | 聚合索引 → 状态列名;聚合后的求值用 |
| `subquery_executor` | 标量子查询的执行入口,由解释器注入 |
| `expression_lookup` | **分阶段求值的粘合剂**:聚合后的阶段里 `sum(x)` 不再重算,而是来这张"表达式文本 → 已算好的列"的表里取 |
| `lambda_scope` | lambda 形参的作用域 |

硬规则(写在类 docstring 里):**任何从这个类流出去的列,`size()` 必须等于 `rows()`**。常量列如果忘了 `resize(rows)`,会在后续 `filter` 里长度对不上——报错还算好,怕的是长度刚好相等而结果全错。

```python
class Scope:
    def __init__(self, columns: Optional[Dict[str, ColumnWithTypeAndName]] = None,
                 aliases: Optional[Dict[str, A.Node]] = None,
                 parent: Optional["Scope"] = None,
                 lambda_params: Optional[Set[str]] = None) -> None
    def child(self, **kwargs: Any) -> "Scope"
    def resolve_column(self, name: str) -> Optional[ColumnWithTypeAndName]
    def resolve_alias(self, name: str) -> Optional[A.Node]
    def is_lambda_param(self, name: str) -> bool
    def add_column(self, name: str, col: ColumnWithTypeAndName) -> None
    def add_alias(self, name: str, node: A.Node) -> None
    def flatten(self) -> Dict[str, ColumnWithTypeAndName]
    def available_names(self) -> List[str]
```

`Scope` 是一条**链**:lambda 形参(最内层)→ SELECT 别名(中间)→ 表列(最外层)。查找从内往外走,**内层优先**。

### B.3.4 `IStorage` 与相关结构(`storages/base.py`)

```python
class IStorage(ABC):
    engine_name: str = "Unknown"
    features: StorageFeatures = StorageFeatures()

    def __init__(self, table: Table, storage_id: StorageID, context: Any = None,
                 engine_args: Optional[Sequence[Any]] = None) -> None
    # 身份
    def get_storage_id(self) -> StorageID
    def get_name(self) -> str
    def full_table_name(self) -> str
    # 读写(必须实现)
    @abstractmethod
    def read(self, ctx: Any, **kwargs: Any) -> Iterator[Chunk]
    @abstractmethod
    def write(self, ctx: Any, block: Block) -> None
    # 结构
    def get_columns(self) -> List[ColumnWithTypeAndName]
    def row_count(self) -> int
    def size_in_bytes(self) -> int
    def part_count(self) -> int
    def get_partition_key_description(self) -> str
    def get_sorting_key_description(self) -> str
    def get_primary_key_description(self) -> str
    # 生命周期
    def startup(self) -> None
    def shutdown(self) -> None
    def is_shutdown(self) -> bool
    def flush(self) -> None
    def truncate(self) -> None
    # DDL
    def alter_add_column(self, column: Any, after: Optional[str] = None) -> None
    def alter_drop_column(self, name: str) -> None
    def alter_modify_column(self, name: str, new_type: DataType, default_expr: Any = None) -> None
    def alter_rename_column(self, old: str, new: str) -> None
    def mutate(self, command: Any) -> str
    def optimize(self, final: bool = False, deduplicate: bool = False, **kwargs: Any) -> Any
    def check(self) -> str
    def describe_stats(self) -> Dict[str, Any]
    def analyze_where(self, expr: Any, ctx: Any) -> Any
    def supports_where_pushdown(self) -> bool
```

`read()` 有一条容易踩的约定(写在它的 docstring 里):产出的每个 chunk 的列**必须**与 `get_columns()`(或 `ReadContext.columns` 裁剪后的子集)**同序同名同类型**。引擎层最容易出的 bug 就是这里——某个 chunk 的列顺序变了,下游按位置取列就整整齐齐地错位,而且**不报错**,只是结果全错。

配套的两个数据结构:

```python
@dataclass
class ReadContext:
    ctx: Any = None
    # ── 执行器填的:真正决定"读多少" ──
    where: Any = None                            # 查询的过滤条件(AST),引擎靠它做三次裁剪
    prewhere: Any = None                         # 同上,来自 PREWHERE
    settings: Any = None                         # 查询级设置(use_skip_indexes 这类开关)
    final: bool = False                          # FROM t FINAL
    sample: Any = None                           # SAMPLE 子句
    # ── "调用方已经算好了就填进来"的透传位(None = 引擎自己从 where 算) ──
    columns: Optional[List[str]] = None          # 列裁剪:要读哪些列
    key_analysis: Any = None                     # 已经算好的主键分析结果
    partition_ids: Optional[set] = None          # 已经算好的分区集合
    limit: Optional[int] = None                  # LIMIT 下推(引擎据此提前停)
    query_info: Any = None

    def wanted_columns(self, available: Sequence[str]) -> List[str]

@dataclass
class StorageFeatures:
    primary_key: bool = False
    partitions: bool = False
    sorting_key: bool = False
    parallel_read: bool = True
    skip_indexes: bool = False
    ttl: bool = False
    mutations: bool = False
    merges: bool = False
    replication: bool = False
    append_only: bool = False
    sampling: bool = False
    projections: bool = False

    def describe(self) -> str
```

引擎注册与工厂(`storages/base.py` 末尾):

```python
StorageFactory = Callable[..., IStorage]

def register_storage_engine(name: str, factory: StorageFactory, *,
                            deprecated: bool = False, description: str = "",
                            aliases: Sequence[str] = ()) -> None
def storage_engine_exists(name: str) -> bool
def registered_storage_engines() -> List[Tuple[str, bool, str]]
def create_storage(table: Table, engine_name: str,
                   engine_args: Optional[Sequence[Any]] = None,
                   context: Any = None, params: Optional[Dict[str, Any]] = None,
                   storage_id: Optional[StorageID] = None) -> IStorage
def storage_engine_display_name(engine_name: str) -> str
```

引擎名**大小写不敏感**(查表用小写键),但**展示**时用注册时的原始大小写:`CREATE TABLE ... ENGINE = mergetree` 能建出来,而 `system.tables.engine` 显示的是 `MergeTree`。`params` 是"引擎相关的建表参数",例如 MergeTree 的 `order_by` / `partition_by` / `primary_key` / `settings`。

### B.3.5 `ISerialization`(`serialization.py`)

```python
class ISerialization(ABC):
    @abstractmethod
    def serialize_binary(self, column: IColumn, out: bytearray, start: int, count: int) -> None
    @abstractmethod
    def deserialize_binary(self, data: bytes, num_rows: int, pos: int = 0) -> tuple
    def serialize_text(self, value: Any, settings: Any = None) -> str
    def deserialize_text(self, text: str, settings: Any = None) -> Any
    def serialize_binary_bulk_prefix(self, ...) -> Optional["SerializeBinaryBulkState"]
    def serialize_binary_bulk_suffix(self, ...) -> None
    def create_bulk_state(self) -> Optional["SerializeBinaryBulkState"]
    def has_fixed_size(self) -> bool
    def fixed_size(self) -> int

def get_serialization(dtype: DataType) -> ISerialization
def serialize_binary(dtype: DataType, column: IColumn) -> bytes
def deserialize_binary(dtype: DataType, data: bytes, num_rows: int) -> IColumn
def write_var_uint(out: bytearray, value: int) -> None
def read_var_uint(data: bytes, pos: int = 0) -> tuple
```

四个贯穿全代码库的设计约束(模块 docstring):按**列**序列化;序列化是**流式**且可只读一部分;变长结构用**偏移数组**而不是分隔符;格式由**类型**决定而不是由数据决定。

### B.3.6 `ICompressionCodec`(`compression.py`)

```python
class ICompressionCodec(ABC):
    @abstractmethod
    def do_compress(self, data: bytes) -> bytes
    @abstractmethod
    def decompress(self, data: bytes, uncompressed_size: int) -> bytes
    def compress(self, data: bytes) -> bytes          # 带外层的公共逻辑
    def is_generic(self) -> bool
    def byte_size_after_compress(self, data: bytes) -> int

class CompressionCodecNone(ICompressionCodec)
class CompressionCodecLZ4(_CompressionCodecLZ4Base)
class CompressionCodecLZ4HC(_CompressionCodecLZ4Base)
class CompressionCodecZSTD(ICompressionCodec)
class CompressionCodecDelta(ICompressionCodec)
class CompressionCodecDoubleDelta(ICompressionCodec)
class CompressionCodecGorilla(ICompressionCodec)
class CompressionCodecT64(ICompressionCodec)
class CompressionCodecFPC(ICompressionCodec)
class CompressionCodecMultiple(ICompressionCodec)

class CompressionCodecFactory:
    @staticmethod
    def get(name: str, params: Sequence[int] = ()) -> ICompressionCodec
    @staticmethod
    def parse_codec_chain(text: str) -> List[ICompressionCodec]

class CompressedWriteBuffer: ...
class CompressedReadBuffer: ...
def benchmark_codecs(n: int = 100_000, seed: int = 0,
                     check_roundtrip: bool = False) -> List[Tuple[str, str, int, float, float]]
```

`benchmark_codecs` 在一组代表性列(单调时间戳 / 随机 `UInt64` / 低基数串 / 普通串)上跑遍所有 codec,返回 `(列名, codec 名, 原始字节数, 压缩率, 耗时毫秒)`。注意**压缩率 = 原始 / 压缩后**,越大越好。

`is_generic()` 是链式校验的判据:**一个非通用的编解码器后面必须跟一个通用编解码器**,否则数据既没变小又读不回来——真实 ClickHouse 因此会拒绝单独写 `CODEC(Delta)`。这条校验**已经接线**:写入路径解析列上声明的 CODEC 时会走 `parse_codec_chain` → `_validate_chain`,所以 `CREATE TABLE ... CODEC(Delta)` 会像真实 ClickHouse 那样被拒绝,抛的是 `ILLEGAL_TYPE_OF_ARGUMENT`(**Code 24**),不是 49。

真正需要记住的简化在别处:**一个压缩块只带一个 method 字节**,表达不了整条链,所以链里最终生效的是**链尾那个通用编解码器**。也就是说 `CODEC(Delta, ZSTD(3))` 会**真的用 ZSTD 压**(字节流里 method = ZSTD),但**跳过 Delta 预变换**——"专用编解码器先把数据变规整、再交给通用编解码器"这条收益拿不到。单写一个通用编解码器(`CODEC(ZSTD(3))`)则与真实 ClickHouse 语义一致(算法本身仍是自研的,格式不兼容,见 B.2.9)。

### B.3.7 `IAggregateFunction`(`aggregates/base.py`)

```python
class IAggregateFunction:
    def __init__(self, name: str, arg_types: Sequence[DataType],
                 params: Sequence[Any] = ()) -> None
    def get_return_type(self) -> DataType
    def create_state(self) -> Any
    def clone_state(self, state: Any) -> Any
    def add(self, state: Any, columns: Sequence[Any], row: int) -> None
    def merge(self, dst_state: Any, src_state: Any) -> None
    def insert_result_into(self, state: Any, column: Any) -> None
    def serialize_state(self, state: Any) -> bytes
    def deserialize_state(self, data: bytes) -> Any
    def is_state_combinator(self) -> bool
    def requires_arg_columns(self) -> bool

def register_aggregate(name: str, cls: Optional[type] = None, *,
                       aliases: Sequence[str] = (), description: str = "",
                       factory: Optional[Callable[[str, Sequence[Any], Sequence[DataType]],
                                                  IAggregateFunction]] = None) -> Any
def register_combinator(suffix: str, handler: Callable[..., IAggregateFunction]) -> None
def split_combinators(name: str) -> Tuple[str, Optional[str], bool, bool]
def combinator_suffixes() -> List[str]
def get_aggregate_function(name: str, params: Optional[Sequence[Any]] = None,
                           arg_types: Optional[Sequence[DataType]] = None) -> IAggregateFunction
def registered_aggregate_functions() -> List[Tuple[str, str]]
def aggregate_function_exists(name: str) -> bool
```

`cls` 形态要求构造器签名是 `(name, arg_types, params)`;`factory` 形态收到 `(canonical_name, params, arg_types)`。**`add` 只处理一行** —— 逐个 `row` 调用,这是接口最简单也最慢的一处。

### B.3.8 `IProcessor`(`execution/processors.py`)

```python
class Status(Enum):
    NEED_DATA = "NeedData"
    PORT_FULL = "PortFull"
    FINISHED = "Finished"
    READY = "Ready"

class PushResult(Enum):
    NotNeeded = "NotNeeded"
    PortFull = "PortFull"
    NeedMoreData = "NeedMoreData"

class IProcessor(ABC):
    def add_input(self, name: str = "") -> InputPort
    def add_output(self, name: str = "") -> OutputPort
    def prepare(self, processor_id: int) -> Status
    def work(self) -> Status
    def has_input(self) -> bool
    def has_output(self) -> bool
    def all_inputs_finished(self) -> bool
    def all_outputs_finished(self) -> bool
    def finish_outputs(self) -> None

class ISource(IProcessor):
    def generate(self) -> Optional[Chunk]
    def work(self) -> Status

class ISink(IProcessor):
    def work(self) -> Status
    def consume(self, chunk: Chunk) -> None

class ISimpleTransform(IProcessor):
    def transform(self, chunk: Chunk) -> Optional[Chunk]
    def work(self) -> Status

class InputPort(Port):
    def pull(self) -> Optional[Chunk]
    def is_ready(self) -> bool
    def finish(self) -> None

class OutputPort(Port):
    def push(self, chunk: Chunk) -> "PushResult"
    def pull(self) -> Optional[Chunk]
    def finish(self) -> None

def connect(output: OutputPort, input_port: InputPort) -> None
```

`connect()` 是**双向**的:上游知道下游是谁(用来判断"下游还要不要"),下游知道上游是谁(用来取数据)。端口有两个状态位:`is_finished` 与"有没有数据"——推送模型下"结束"必须是**显式信号**,否则下游无法区分"暂时没有"和"永远没有"。

## B.4 扩展指南

三个模板,按"改动范围从小到大"排列。每一步都指出**该改哪个文件、要注册什么、怎么自测**。

### B.4.1 加一个标量函数

以"加一个 `isPalindromicUTF8(s)`"为例(纯虚构,便于套模板)。

**第一步:选模块。** 函数按语义分到 `minich/functions/` 下的某个文件。字符串类放 `string.py`,类型转换放 `type_funcs.py`,新的领域(比如"加一组地理位置函数")就新建一个模块。前缀和下标的命名要与现有风格一致——这决定了 `system.functions` 里的分类看起来是否整齐。

**第二步:写类。** 继承 `FunctionBase`,实现 `get_return_type` 和 `execute_row`(**或者** `execute`)。

```python
class FunctionIsPalindromicUTF8(FunctionBase):
    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type("UInt8")

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = values[0]
        if s is None:
            return None
        return 1 if s == s[::-1] else 0
```

两条硬约束,都写在 `FunctionBase` 的 docstring 里:

- `get_return_type` **必须是纯函数,且不能依赖行的值**——它只在准备阶段被调用一次,违反这条会导致"类型与实际数据不符",下游崩溃。
- `execute_row` 只处理一行;只实现它就拿到基类的向量化入口 `execute` 的**逐行回退实现**。这条路能跑但慢。想快就覆盖 `execute`,一次处理整列(见 `functions/encoding.py` 的 `_EncodingFunction` 或 `functions/math_funcs.py` 的写法)。

另外三个可选钩子要在需要时覆盖:`propagate_nulls`(默认 `True`:任一参数为 NULL 结果就是 NULL;`isNull` / `coalesce` / `ifNull` 这类置 `False` 并自己处理 NULL)、`deterministic`(`now` / `rand` 这类非确定性函数必须置 `False`——否则常量折叠会把整条查询里所有 `now()` 变成同一个时刻)、`return_type_cannot_be_nullable`。

**第三步:注册。** 在**模块末尾**调用 `register_function`(注册是 import 的副作用,必须发生在模块导入时):

```python
register_function(
    "isPalindromicUTF8",
    FunctionIsPalindromicUTF8,
    description="判断 UTF-8 字符串是否为回文(按字节比较)",
)
```

`register_function(name, cls=None, *, aliases=(), description="", factory=None, case_insensitive=True)` 有三种用法:当装饰器(`@register_function("plus", aliases=["add"])`)、直接传类、或传 `factory=`。函数名在注册表里统一小写存放,查询时也小写化再查——**大小写不敏感**。

**第四步:自测。**

```bash
cd mini-clickhouse
python -c "
import minich as ch
ctx = ch.Context()
print(ctx.execute(\"SELECT isPalindromicUTF8('abcba')\").scalar())     # 1
print(ctx.execute(\"SELECT isPalindromicUTF8('abc')\").scalar())       # 0
print(ctx.execute(\"SELECT ispalindromicutf8(NULL)\").scalar())        # None
"
python -c "
from minich.functions.base import function_exists, registered_functions
assert function_exists('ISPALINDROMICUTF8')
assert any(n == 'ispalindromicutf8' for n, _, _ in registered_functions())
print('registered OK')
"
```

`system.functions` 里也会立刻出现新函数——把 `SELECT * FROM system.functions WHERE name = 'isPalindromicUTF8'` 当作第二个自测点。

### B.4.2 加一个聚合函数

以"加一个 `product(x)`(连乘)"为例。

**第一步:选模块。** `minich/aggregates/`。已有的归类很清楚:`sum.py` 放求和族,`uniq.py` 放去种族,新领域可以新建模块(如 `product.py`)。

**第二步:写类。** 继承 `IAggregateFunction`,实现**四个必需方法**:

```python
class AggregateFunctionProduct(IAggregateFunction):
    def get_return_type(self) -> DataType:
        return get_data_type("Float64")

    def create_state(self) -> Any:
        return [1.0, 0]          # (积, 计数) —— 一个"可合并"的状态

    def add(self, state, columns, row) -> None:
        v = columns[0].get(row)
        if v is None:
            return
        state[0] *= v
        state[1] += 1

    def insert_result_into(self, state, column) -> None:
        column.append(float("nan") if state[1] == 0 else state[0])

    # 要让 AggregatingMergeTree / -State / -Merge 能用,还要这两个:
    def merge(self, dst_state, src_state) -> None:
        dst_state[0] *= src_state[0]
        dst_state[1] += src_state[1]

    def serialize_state(self, state) -> bytes:
        w = ByteWriter(); w.f64(state[0]); w.f64(state[1]); return w.bytes()

    def deserialize_state(self, data) -> Any:
        r = ByteReader(data); return [r.f64(), r.f64()]
```

先想清楚的一件事是**状态是什么**,而且它必须**可合并**——这正是第 31 章 §31.9 讲的 `avg` 那个例子。`avg` 的状态是 `(和, 计数)`;`uniq` 是 HyperLogLog 的桶数组;`quantile` 是 TDigest 草图。`product` 的状态是 `(积, 计数)`,其中计数存在的理由与 `avg` 相同:空集合要能返回 `nan` 而不是 1。

同理,`get_return_type` **只看 `arg_types` / `params`,不看数据**——准备阶段只调用一次。

**第三步:注册。** 模块末尾:

```python
register_aggregate("product", AggregateFunctionProduct,
                   description="连乘:状态是 (积, 计数),空集返回 nan")
```

签名是 `register_aggregate(name, cls=None, *, aliases=(), description="", factory=None)`。`cls` 形态要求构造器签名是 `(name, arg_types, params)`;`factory` 形态收到 `(canonical_name, params, arg_types)`,适合"同一个类派生出多个名字"的情况(`median` 就是这样派生的)。

**组合器不用你操心**:注册完之后 `productIf` / `productArray` / `productState` / `productMerge` / `productDistinct` / `productOrNull` / `productOrDefault` / `productForEach` 会自动可解析——它们在 `get_aggregate_function()` 内部按后缀剥掉再由 `combinators.py` 包装。

**第四步:自测。**

```bash
cd mini-clickhouse
python -c "
import minich as ch
ctx = ch.Context()
print(ctx.execute('SELECT product(x) FROM values(\\'x UInt64\\', (2), (3), (4))').scalar())   # 24.0
print(ctx.execute('SELECT productIf(x, x > 3) FROM values(\\'x UInt64\\', (2), (3), (4))').scalar())
print(ctx.execute('SELECT productState(x) FROM values(\\'x UInt8\\', (2), (3))').text())
"
```

再补一个**往返自测**(这是最容易漏、也最容易悄悄出错的一步):

```python
from minich.aggregates import get_aggregate_function
from minich.types import get_data_type
f = get_aggregate_function("product", [], [get_data_type("UInt64")])
s = f.create_state(); f.add(s, [ [2, 3, 4] ], 1)     # 简化示意
blob = f.serialize_state(s)
assert f.deserialize_state(blob) == s                 # 状态往返必须精确
```

### B.4.3 加一个表引擎

以"加一个 `Echo` 引擎"(写入时把 block 打印出来,读取永远为空——调试用)为例。

**第一步:新建模块。** `minich/storages/echo.py`。每个引擎一个文件是这个项目的惯例。

**第二步:写类。** 继承 `IStorage`,设 `engine_name`,声明 `features`,实现 `read` / `write`:

```python
class StorageEcho(IStorage):
    engine_name = "Echo"
    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: dict = dict(params or {})

    def get_columns(self):
        return _stored_columns(self.table)

    def read(self, ctx, **kwargs):
        return iter(())          # 永远读不出东西

    def write(self, ctx, block: Block) -> None:
        print(block.dump_structure())
```

`IStorage` 的其余方法都有合理默认(**不支持就抛 `NOT_IMPLEMENTED`**),所以最小实现只需要这两个。`read()` 有一条必须遵守的约定:产出的每个 `Chunk` 的列要与 `get_columns()`(或 `ReadContext.columns` 裁剪后的子集)**同序同名同类型**——顺序错了不会报错,只会让结果静默错位。

`features` 的位要**诚实地**声明。第 24 章会展示:一个优化规则如果误判了引擎能力(比如对没有主键的引擎下推主键条件),产生的不是慢查询,而是**错误结果**。

**第三步:注册。** 文件末尾:

```python
def _factory(table, storage_id, context, engine_args, params) -> StorageEcho:
    return StorageEcho(table, storage_id, context, engine_args, params)

register_storage_engine(
    "Echo",
    _factory,
    description="把写入的 block 结构打印出来(调试用);读取永远为空。",
)
```

工厂的签名必须能接收 `(table, storage_id, context, engine_args, params)`——`create_storage()` 就是这么调它的。

**第四步:把模块挂进包的导入链。** 这一步漏了引擎就"注册不上",而且**不报错**——`CREATE TABLE ... ENGINE = Echo` 只会说"未知引擎":

```python
# minich/storages/__init__.py
from . import echo  # noqa: F401
```

**第五步:自测。**

```bash
cd mini-clickhouse
python -c "
import minich as ch
from minich.storages.base import storage_engine_exists, storage_engine_display_name
print(storage_engine_exists('echo'), storage_engine_exists('ECHO'))   # True True
print(storage_engine_display_name('echo'))                            # Echo
ctx = ch.Context()
ctx.execute('CREATE TABLE e (a UInt64) ENGINE = Echo')
ctx.execute('INSERT INTO e VALUES (1), (2)')      # 应当打印 block 结构
print(ctx.execute('SELECT count() FROM e').scalar())                  # 0
print(ctx.execute(\"SELECT engine FROM system.tables WHERE name = 'e'\").scalar())  # Echo
"
```

`system.table_engines` 会自动多出一行——那是第三个自测点。

## B.5 验证矩阵

这个项目有三条**可复现**的验证命令,它们就是 `mini-clickhouse/README.md` 的"快速开始"里写的那三条:

```bash
cd mini-clickhouse
python -m pytest tests/                                   # ① 全部测试
for f in examples/*.py; do python "$f"; done                  # ② 全部示例
cd .. && python mini-clickhouse/tools/check_docs.py           # ③ 文档结构自检
```

下面每一节的数据都是我在同一次快照上跑出来的。

### B.5.1 测试

`tests/` 下有 **25 个** `test_*.py` 文件加一个 `conftest.py`。按 `test_` 函数计数共 **923 个**;参数化展开后 pytest 实际收集 **2 945 条**(其中 2813 条通过):

| 测试文件 | `test_` 函数数 | 行数 |
| --- | --- | --- |
| `test_sql_parser.py` | 119 | 1 892 |
| `test_execution.py` | 98 | 1 236 |
| `test_keeper_replication.py` | 70 | 1 283 |
| `test_columns.py` | 67 | 685 |
| `test_types.py` | 67 | 756 |
| `test_storages.py` | 62 | 1 018 |
| `test_mergetree.py` | 46 | 946 |
| `test_execution_aggregates.py` | 37 | 384 |
| `test_block.py` | 36 | 386 |
| `test_formats.py` | 35 | 701 |
| `test_dictionaries_access.py` | 30 | 700 |
| `test_functions.py` | 29 | 661 |
| `test_system_tables.py` | 29 | 332 |
| `test_aggregates.py` | 29 | 641 |
| `test_compression.py` | 26 | 306 |
| `test_serialization.py` | 22 | 303 |
| `test_regressions.py` | 21 | 394 |
| `test_cli.py` | 20 | 236 |
| `test_regressions_nullable_storage.py` | 17 | 330 |
| `test_regressions_hash_sql.py` | 16 | 342 |
| `test_regressions_audit2.py` | 15 | 290 |
| `test_regressions_joins.py` | 11 | 256 |
| `test_regressions_pruning.py` | 9 | 332 |
| `test_regressions_codec_family.py` | 7 | 263 |
| `test_regressions_window.py` | 5 | 206 |
| **合计** | **923** | **14 879** |

跑一遍的实测结果:

```text
2813 passed, 83 skipped, 49 xfailed
```

读这行结果时要注意三点:

1. **83 条 skip** 不是失败,是"条件不满足就跳过"(典型是平台相关的路径/磁盘行为)。
2. **49 条 xfail 是"有意标出的已知差异"**——`tests/test_regressions.py` 里的写法是把"真实 ClickHouse 支持、mini-clickhouse 不支持"的东西写成 `@pytest.mark.xfail`,于是差异变成了**可执行、可统计**的清单,而不是散落在文档里的文字。
3. **没有 xpass,也没有 warning**。xpass 为 0 是好事但也是信号:"原本以为没实现的,现在实现了"会以 xpass 的形式冒出来,一旦出现就说明该摘掉那个标记了。warning 为 0 则说明 `pyproject.toml` 里的 ini 选项在当前 pytest 版本下全部合法——早先版本曾因为写了这个 pytest 不认识的键而冒出一条 `Unknown config option` 提醒,它不影响结果,但属于"配置写了不一定生效"的典型例子,值得留意。

顺带一句方法论:**上表里的"函数数/行数"是数出来的,不是估的**——`grep -c '^def test_'` 和 `wc -l`。这也解释了为什么它们会和早先版本的数字对不上:实现长大了。凡是在本章看到与别处不一致的规模数字,以**当场数出来的**为准。

### B.5.2 示例

`examples/` 下有 **14 个**脚本,共 **1 713 行**。每个都是自包含的(从 `import minich as ch` 开始,自己建表、插数据、打印),执行时**不依赖任何前置状态**。实测 14 个全部退出码 0。

| 示例 | 主题 |
| --- | --- |
| `01_hello_clickhouse.py` | 最小可用:建表、插入、查询 |
| `02_mergetree_parts.py` | part 在磁盘上长什么样 |
| `03_auto_merge.py` | 后台合并与 `level` |
| `04_compression.py` | 编解码器与压缩率 |
| `05_formats.py` | `FORMAT` 的输入与输出 |
| `06_functions.py` | 函数库巡礼 |
| `07_aggregation.py` | 聚合与组合器 |
| `08_joins.py` | JOIN 的方向与 strictness |
| `09_window_functions.py` | 窗口函数 |
| `10_replacing_mergetree.py` | 去重引擎 |
| `11_mutations_and_ttl.py` | mutation 与 TTL |
| `12_keeper_and_replication.py` | Keeper 与复制 |
| `13_system_tables.py` | system 表 |
| `14_end_to_end.py` | 端到端串起来 |

### B.5.3 文档结构自检

`tools/check_docs.py`(1000 行)是这本书自己的检查脚本,对 `clickhouse-tutorial/` 下**全部正文 + 两个 README** 扫描。它现在有 **20 项**检查,每一项都对应一类**真实发生过**的错误:

| 检查项 | 拦什么 |
| --- | --- |
| 1. 章号连续性 | 00..N 无跳号、无重号 |
| 2. 代码围栏闭合 | ``` 成对出现——少一个反引号会让后面整段排版崩掉 |
| 3. 跨章引用越界 | "第 N 章"里的 N 必须存在 |
| 4. §小节引用悬空 | "§N.M" 必须在该章里存在(附录用 §B.M 形式) |
| 5. 本地链接可达 | markdown 相对链接指向的文件必须存在 |
| 6. 表格列数一致 | 每行单元格数 == 表头(忽略转义竖线) |
| 7. 自述数字一致性 | 全书里"测试数 / 示例数"这类**总量**说法只能有一个值 |
| 8. 自指计数一致 | "(往后)还有 N 章"必须等于真实章号差 |
| 9. 结语位置一致 | "结语"标题只许出现在一章,且所有指向它的说法都指向那一章 |
| 10. 小节编号连续 | 每章 `## N.1`、`## N.2`… 必须从 1 开始、无跳号(附录用字母前缀) |
| 11. 实现规模与注册表一致 | "N 个表引擎" / "N 个表函数" / "N 张 system 表" 这类说法必须等于从 `registered_*()` 里**实际数出来**的数字 |
| 12. 实现行数/文件数一致 | "N 个 `.py`、M 行"这类**总量**必须等于当场 `find \| wc -l` 数出来的数字;同一项还管**单个模块**的行数——正文里 "`merger.py`(992 行)" 这种写法,数字也得等于当场数出来的 |
| 13. 测试结果行一致 | 全书所有 `N passed, M skipped, K xfailed` 必须**完全相同** |
| 14. 附录 B 文件树与磁盘一致 | B.1 里每个文件名后面的行数必须等于当场 `wc -l`;还要求不漏列、不多列任何一个文件 |
| 15. 自述检查项数一致 | 文档里"N 项检查"的 N 必须等于本脚本真实执行的项数(脚本数自己) |
| 16. 关键设计计数一致 | "十个关键设计"这类说法必须等于第 31 章真实的小节数 |
| 17. 子包行数一致 | "`minich/storages/merge_tree/` 12 997 行"这类**子包小计**必须等于当场数出来的值(第 12 项只管总量,刻意放过子包) |
| 18. 自述脚本行数一致 | "`tools/check_docs.py`(N 行)"必须等于本脚本真实行数(第 12、14、17 项的取证范围都止于 `minich/`,它掉进了缝里) |
| 19. `tests/` 规模矩阵一致 | B.5.1 那张"测试文件 / 函数数 / 行数"表必须逐行等于磁盘:文件都在、"`def test_`"数对得上、合计正确,且不漏列、不多列任何一个测试文件 |
| 20. 收集条数一致 | "展开后实际收集 N 条"必须等于当场 `pytest --collect-only` 数出来的条数,且 `passed + skipped + xfailed` 必须等于同一个数 |

第 11–20 项是同一种病反复发作,成因非常典型:这本书的多个章节是**并行**写的,各自读到的是不同时刻的实现快照——于是先写的章节说"注册了 14 个引擎",后写的说"23 个";附录 B 的正文说 `minich/` 有 85 061 行,同一章的"小结"却停留在一个更小的旧值上;两份 README 里写的测试通过数也早就不是实测值了。**三处写的时候都是对的**,只是写完之后实现又长了一大截。这类错误不是"有人写错了",而是"项目在长大"这个动作本身引发的 —— 你什么都没有改错,只是加了一个函数、多跑了一次测试,旁边所有引用规模数字的句子就全过期了。**所以要交给机器拦,不能靠人眼。** 第 8、9 两项是同一个形状(新增一章会让所有"后面还有几章""结语在哪"的说法过期),它们一起构成了这份脚本真正的价值。

第 12 项还示范了一次"检查本身也要防误报":第一版把所有 `N 行` 都当成总量来比,结果把**子包小计**(`minich/sql/` 那一行的合计)和**单文件行数**(`part.py` 那种括号里的值)全报成了错误,18 条里 17 条是误报。改成只认 **"N 个 `.py` … M 行"** 这个总量句式之后才可用。**一个总在误报的检查,很快就会被所有人忽略** —— 这和第 6 项当初踩的坑是同一个。

不过第 12 项收紧判据是有代价的:子包小计从此**没人管**了,于是这一轮真的漏了一次——附录 B 写"`storages/merge_tree/` 12 142 行、占存储层 69%",而磁盘上已经是 12 762 行。这就是第 17 项的来历:子包路径后面紧跟的行数,必须等于当场 `wc -l` 数出来的值。

第 18 项是同一句话的第二次收获。第 17 项补上了子包,但第 12、14、17 三项的取证范围**都止于 `minich/`** —— 检查脚本自己在 `tools/` 下,不在任何一项的视野里。于是 B.5.3 这段一直把这个脚本的行数写成 **704**,而磁盘上已经是 **801**:脚本每年都跑绿,这个数字每年都是错的,因为**没有任何一项检查会读到它**。规律已经很清楚:**每一项检查都是"为了不误报"而收窄了范围的,而收窄掉的那部分就没人管**。所以每加一项检查,都值得再问一句"它把什么排除在外了"——第 12 项排除了子包(→ 第 17 项),第 12、14、17 项一起排除了 `tools/`(→ 第 18 项)。

第 19、20 两项是同一句话的**第三次**收获,也是这一轮里最难看的一次。第 18 项补上了 `tools/`,但第 12、14、17、18 四项的取证范围**没有一项伸进 `tests/`** —— 于是 B.5.1 那张表一直停在"23 个文件、891 个测试函数"上,而磁盘上早就是 **25 个文件、923 个**。多出来的两个文件是 `test_regressions_nullable_storage.py` 与 `test_regressions_audit2.py`,一共 32 个测试函数、展开后 34 条用例。它们不是偷偷加进来的:它们被写出来、跑绿、留在仓库里,而**同时写在两份 README 与附录 B 里的那个"2779 条通过"从头到尾没有变过** —— 因为没有任何一项检查会去数测试。

这件事最值得记的地方在于:那个数字就印在 README 的"验证状态"一节里,旁边还写着一句"**这一节由仓库里的命令产出,不是声明**"。它当时已经是一句声明了。一份反复强调"数字必须当场数出来"的文档,把自己最重要的那个数字抄在了四个地方然后忘了更新 —— 这不是态度问题,是**结构**问题:第 13 项只保证那几处写的**互相一致**,而当它们一起过期时,它照样全绿。**一致不等于正确。** 第 13 项守着的是"有没有人只改了一处",它从来没有能力回答"改对了没有"。

所以第 19、20 两项分别补上这条缝的两半。第 19 项是静态的:把 B.5.1 那张表逐行拿去和磁盘比,用的是和第 12、14 项同一套做法(`wc -l` 数行数、`grep -c 'def test_'` 数函数)。第 20 项则**真的调用一次外部命令**——它是全部二十项里唯一这么做的一项,因为"参数化展开后有多少条"是**参数化的产物**:源码里只有 923 个 `def test_`,展开后却是 2 945 条,静态读文件无论如何读不出来。数不出来的东西,只能让它自己报。代价是约 1 秒(只收集、不执行任何用例),换来的是 B.5.1 的"收集 N 条"与那句 `N passed, M skipped, K xfailed` 再也不会各自过期 —— 因为脚本现在会拿 `passed + skipped + xfailed` 去对收集到的条数。

顺带一个反例式的教训:第 20 项最自然的实现是给 pytest 加 `-p no:cacheprovider`(为了不留缓存、让检查无副作用)。但这一加,`pyproject.toml` 里的 `cache_dir` 就变成了"当前 pytest 不认识的 ini 选项",凭空冒出一条 `PytestConfigWarning` —— 而 B.5.1 明确断言测试过程"没有 warning"。**检查工具自己会制造它要检查的现象**,和第 6 项那次误报(把 `` `a\|b` `` 当成表格分隔符)是同一个坑。

第 14 项也是被真实事故逼出来的:第 12 项只管"总量",而 B.1 是一棵**逐文件**的树,每行都带一个行数。总量对得上、单个文件对不上完全可能 —— 当时的情况是 `part.py`(自述 1 881、当时实际 2 018)、`key_condition.py`(1 207 / 1 339)、`ttl.py`(396 / 459)这一批都还是上一轮快照的值,而它们正是读者判断"该读哪个文件"的依据。所以这棵树现在也逐行核对。

第 15 项则把同一条原则对准了**这份清单自己**:每加一项检查,所有"共 N 项"的说法就过期了 —— 这一轮里它就发生了七次(11 → 13 → 15 → 17 → 18 → 19 → 20)。解法不是每次手工同步,而是让脚本数自己:实现段落都写成 `# ── N. 标题`,把标题里的编号加起来就是真实项数。

脚本的行为约定值得一提:检查失败时退出码 1,找不到正文时退出码 2,全通过退出码 0——所以它能直接接进 CI。输出里的"表格列数"检查还专门处理了一个曾经的误报:markdown 里 `` `a\|b` `` 是**字面竖线**而不是分隔符,脚本用 `re.split(r'(?<!\\)\|')` 切分,宁可漏报也不误报。

### B.5.4 验证环境与边界

`README.md` 的"验证状态"一节写明了:验证环境是 Windows 11 + Python 3.14.6 + NumPy 2.5.1,依赖只有 Python ≥ 3.10 与 NumPy,**没有别的第三方依赖**。

同一节里还有一句"诚实说明"值得原样记住:**mini-clickhouse 在本机完整验证,教程里对真实 ClickHouse 的行为描述基于官方文档与对 25.3 的核对,但本机没有安装 ClickHouse——那些示例属于"文档级准确"而非"本机跑过"。** 这份附录里的所有数字都属于前者;凡是关于**真实** ClickHouse 的性能与行为数字,请以正文里标注了来源的那些为准,本章不提供。

## B.6 已知取舍与陷阱清单

这一节把 `minich/` 里最需要知道的部分汇总成一份可扫读的清单。每一条都写清"**会影响什么判断**"——这才是它的用处。

先说清**规模口径**,因为这类数字最容易各说各话。下面四个数都是当场用 `grep` 数出来的,命令原样附上,你可以自己复现(在 `mini-clickhouse/` 下执行):

```bash
grep -rn "\.\. warning::" --include='*.py' minich/ | wc -l                          # 274
grep -rn "NOT_IMPLEMENTED" --include='*.py' minich/ | wc -l                        # 170
grep -rn "ErrorCode.NOT_IMPLEMENTED," --include='*.py' minich/ | wc -l             # 121
grep -rn "ErrorCode.NOT_IMPLEMENTED_IN_MINICH," --include='*.py' minich/ | wc -l   # 12
```

读法:**274 个 `.. warning::` 文档块**;**170 行**提到 `NOT_IMPLEMENTED`(这个数把 docstring、注释、错误消息里的提及全算进去了,所以它**不是**"未实现的功能数");其中**真正作为 `raise_error(...)` 第一个参数**出现的、也就是抛出点,是 `ErrorCode.NOT_IMPLEMENTED,` 的 **121 处**加上 `ErrorCode.NOT_IMPLEMENTED_IN_MINICH,` 的 **12 处**。它们不相等,是因为"提到"和"抛出"本来就是两回事——以后引用这些数字时请连口径一起引。

### B.6.1 会给出错误结果(不是慢,是错)

这一类最危险,因为它们**不报错**。

- **`cityHash64` 是 `sipHash64` 的别名。** 真实实现是特定版本(1.0.2)的 CityHash,mini-clickhouse 直接返回 SipHash64——分布质量一样好,但**输出值完全不同**。跨系统比对哈希值时会得到不同的分片/去重结果。需要与真实 ClickHouse 对齐请改用 `sipHash64` 或 `xxHash64`(这两个是逐位正确的)。`functions/hashing.py` 的模块 docstring 里明确说明这是**故意的**取舍。
- **window 帧的 `RANGE` 被当成 `ROWS` 处理。** 两者只在**有并列值**时不同:`ORDER BY k` 里有两行 `k = 5` 时,`ROWS ... CURRENT ROW` 给这两行不同的累计值,`RANGE` 给相同的。在去重排序键上做窗口聚合会得到与真实 ClickHouse 不同的结果。差异写在 `window.py` 的 `_frame_bounds` 注释里。
- **窗口函数要进 `ORDER BY`,得用它的别名。** 别名写法现在是支持的:`SELECT b, row_number() OVER (PARTITION BY b ORDER BY a) AS rn FROM t ORDER BY b, rn` 能跑——解释器在分析期会把 `ORDER BY` 里的别名展开回那个窗口节点,再整体替换成 `WindowTransform` 算出来的列。但**裸写**窗口函数本身仍然会抛 `Window function ... must be evaluated by the WindowTransform, not the expression evaluator`(实测:`SELECT k, sum(v) OVER (ORDER BY k) AS s FROM w ORDER BY k, sum(v) OVER (ORDER BY k)`)。写查询时给窗口函数起个名字就行。
- **`VALUES` 与 `SELECT` 两种插入的对齐规则不同。** `_align_block` 只在 VALUES 形态下按名字取列(`block.try_get_by_name(name)`,有显式列清单时按列清单);`INSERT ... SELECT` / `INSERT ... FORMAT` 走 `positional=True`,按**位置**对齐(与真实 ClickHouse 一致)。所以同一条语句从 VALUES 改写成 SELECT 时,"列名对不上"的后果会从**静默填默认值**变成**按位置硬贴**。跨形态搬 SQL 时留意。
- **Native 格式与真实 ClickHouse 不兼容。** 不写 `BlockInfo` 的 5 个字节、没有 custom-serialization 标志字节、每列多一个长度字段——**读到真实 Native 文件会解析出一堆垃圾而不是报错**。
- **`LZ4` / `ZSTD` 是自研实现,不兼容。** 输出带 6 字节魔数(`MCHLZ1` / `MCHZS1`)+ 4 字节原始长度,真实 LZ4 块没有这个头。喂给 lz4 / libzstd 一定报错,反过来一定被魔数挡住。**不要拿它做跨系统交换。**

### B.6.2 会显得"没生效"(设计在,接线没到位)

这一类会让你**误判这个实现的能力**——你会读到很完整的代码,却量不出效果。

- **"机制写好了"不等于"机制生效了",而且这件事会变。** 三次裁剪的机制全在存储层(`_prune_partitions`、`_mark_ranges_for_part`、`_plan_columns`、`key_condition.py`),但生效与否取决于解释器有没有把 `where` / `prewhere` 交给 `ReadContext`(`ReadContext` 上那几个 `columns` / `key_analysis` / `partition_ids` / `limit` 只是"调用方已经算好了就填进来"的透传位,空着**不代表**裁剪失效)。我最后一次量到的状态是:**平凡 count 生效、主键 granule 裁剪生效**(`WHERE id = 100000` 在 20 万行上读 1 个 mark、16 KB),**`LIMIT` 下推与列裁剪不生效**(`SELECT id FROM w` 与 `SELECT * FROM w` 的 `ReadCompressedBytes` 完全相同)。**判断方法固定,结论会变**——用 `ctx.profile_events` 的差值量,脚本见第 31 章 §31.5。(顺带一个读数上的坑:`ProfileEvents.snapshot()` 返回的字典**只含非零项**,所以直接 `a['SelectedMarks'] - b['SelectedMarks']` 可能抛 `KeyError`;要么用 `ProfileEvents.delta(after, before)`,要么两边都写 `.get(k, 0)`。)
- **四条优化规则的效果是分层的,而且只有一条真的在起作用。** `optimize_select()` **是被调用的**(`interpreter.py` 的 `_try_trivial_count()` 与 `_run_optimizer()`),但四条规则里:
  - `optimize_trivial_count_query` **生效**——它让 `SELECT count() FROM t` 一个数据块都不读,直接看 part 的 `count.txt`;而且它会正确地对 `LIMIT` / `OFFSET` / `LIMIT BY` / `FINAL` / `ARRAY JOIN` / `SAMPLE` 失效(否则会给出**静默的错误行数**,不是慢)。
  - `optimize_move_to_prewhere` **不生效**:它要靠"表的排序键列名 / 分区键列名"判断哪些条件可以提到 `PREWHERE`,而取列名的辅助函数 `_sorting_key_columns()` / `_partition_key_columns()` 读的是引擎上的 `order_by_sql` / `sorting_key_sql` / `partition_by_sql` 属性——实测这几个属性在任何引擎上都不存在,于是 `key_columns` 是空集,规则提前返回、一次也不会命中。所以不要指望"条件被自动提到 PREWHERE"(`WHERE` 仍然会被整体交给存储层,索引裁剪照常生效,只是没有这一步拆分)。
  - `limit_push_down` **算出来了但没接线**:它把 LIMIT 记进 `self.optimization.limit`,可是解释器构造 `ReadContext` 时没有把这个值填进 `read_ctx.limit`,于是实测 `LIMIT 10` 仍然读完所有 mark。
  - `optimize_read_in_order` **同样取不到排序键列名**,一次也不会命中;而且即使命中,它的结果也只是被记录,没有任何地方读它来决定"跳过排序"。
  另外注意:**排序键上的等值裁剪不是优化器规则**——它走的是存储层自己的 `KeyCondition`,所以那一条即使优化器完全不帮忙也会生效。(`self.optimization` 目前**只写不读**:`explain.py` 并不打印它,尽管 `_run_optimizer` 的 docstring 是那么写的。)
- **投影解析了但不使用。** `PROJECTION` 子句能解析、声明会被存进 `MergeTreeData.projections`,`StorageFeatures.projections = True` 也只是声明位;读路径完全不看它。`optimize_use_projections` 这条设置同样没有代码读。
- **`old_parts_lifetime`(默认 480)只是声明。** 没有任何地方读它;被取代的 part **立刻**从磁盘删除,而元数据对象进 `obsolete_parts` 字典且**只增不减**。真实 ClickHouse 的延迟删除正是"磁盘占用比数据量大"最常见的原因——在这个实现里观察不到那个现象。
- **`EXPLAIN SYNTAX` 的输出与输入相同。** 因为优化器改的是执行计划而不是语法树。脚本会**自己把这件事打印出来**,不要把它当作"没有可优化空间"的证据。
- **`SELECT 别名.*` 在连接上展开出**整张连接**的列,而不是那个别名自己的列。** 根因不在连接层(`joins.py`),而在分析器的星号展开:`expand_stars` 判断"某列属不属于 `t`"靠的是**列名前缀**(`name.startswith(t + ".")`),可连接结果的表头里**左表的列是裸名**(`k`、`x`),只有右表的同名列才被加上别名前缀(`b.k`)。于是 `SELECT a.* FROM A a JOIN B b ON ...` 时 `prefixed` 是空集,代码走了"**找不到就退回全部列**"那条兜底分支,把 4 列全吐出来。这一条**仍未被修复**(测试里以 `strict` 的 `xfail` 钉住),修它需要在分析器侧建立"列 → 来源表"的映射——单靠名字前缀是做不到的,因为真实 ClickHouse 保留左表裸名、`SELECT *` 也不该带前缀。
- **`UInt64` 列上的负数字面量会让主键裁剪"多跳",而内存引擎不跳,两者对不上。** `WHERE k > -1`(k 是 `UInt64` 列)在 `MergeTree` 上会**返回 0 行**,而同样数据的 `Memory` 引擎返回全表。它的成因是"负数字面量在归一化到无符号时变成了满值",而裁剪路径据此判成"没有任何 granule 能匹配"。**这一条仍未被修复**,而且**真实 ClickHouse 在这类写法上应当报错还是回绕,本书未能确认**——所以这里只陈述"两个引擎不一致"这个可复现的事实,不宣称哪一边是对的。要写这种谓词,请显式写 `k > toUInt64(0)`,不要依赖负数字面量的隐式转换。
- **两种插入形态的对齐规则不同**(`VALUES` 按名字、`SELECT` 按位置):见上一节。
- **`CONSTRAINT ... CHECK` 接错了线,带约束的表根本插不进去。** 约束在解析期被包成一个 `ConstraintDefinition` 对象,但写入路径 `MergeTreeData.write` 的约束检查(`_check_constraints`)却按 `(name, kind, expr)` **三元组**去解包 `self.constraints`(即 `for name, kind, expr in self.constraints`),两种形状对不上。后果:实测 `CREATE TABLE cc (a Int32, CONSTRAINT pos CHECK a > 0) ENGINE = MergeTree ORDER BY a` 建表成功,但一条 `INSERT INTO cc VALUES (5)` 立刻抛 `TypeError: cannot unpack non-iterable ConstraintDefinition object`——**任何带 CHECK 约束的表一插入就崩,彻底不可用**(能力矩阵 B.2.8 已标为"部分实现 / 有缺陷")。想用它,得先把写入路径的解包对齐到解析期产出的对象形状。

### B.6.3 结构性缺失(拿不到那个数字)

- **单线程 + 分段执行。** `EXPLAIN PIPELINE` 会打印 `max_threads = 0`,每个算子都是 `× 1`;阻塞算子(聚合、排序、JOIN、窗口)会把中间结果物化成 Block,所以内存峰值高于真实 ClickHouse,也看不到"边算边出"。**不能拿它量并行加速,也不能量首行延迟。**
- **没有代价模型。** 只有规则式优化,而且四条规则里只有"平凡 count"真正改变了读数,其余三条要么取不到触发条件、要么算出了值却没人用。"为什么选了这条计划"这类问题必须用真实 ClickHouse 回答。
- **没有 `Distributed` 引擎。** 23 个引擎里没有它;`ctx.clusters` 与 `ctx.remote_executor` 这两个钩子**没有任何代码去填**。`cluster` / `clusterAllReplicas` / `remote` 表函数会去找 `remote_executor`,所以实际上不可用。
- **没有 S3 / HDFS。** `IDisk` 只有 `DiskLocal`;`s3` / `s3Cluster` / `hdfs` 表函数都抛精确的 `NOT_IMPLEMENTED`。
- **只有 `DiskLocal` 一个磁盘实现。** `disk.py` 里画的 `DiskS3` / `DiskHDFS` 是**示意图,不是代码**。
- **没有聚合溢出、没有两级哈希表。** 分组数超过 `max_rows_to_group_by` 直接抛 `TOO_MANY_ROWS`;真实 ClickHouse 会切 256 个桶、可并行、可落盘。`group_by_overflow_mode` 的其他取值没有实现。
- **`SAMPLE` 未实现。** `StorageFeatures.sampling` 默认 `False`。
- **没有 HTTP 接口与 `ON CLUSTER`。** 只有 TCP 上的自定义协议。
- **`REPLACE PARTITION` 未实现。** `DROP` / `DETACH` / `ATTACH PARTITION` 有。
- **`EXPLAIN` 的 `PLAN` 不是 `QueryPlan`。** 它打印的是"分段执行"的步骤列表,不是真实 ClickHouse 那棵带 `Indexes` 明细的计划树。

### B.6.4 语义上的细节差异(小,但会对不上账)

- **校验码是 CRC-32(zlib),不是 CRC-32C。**
- **跳数索引在写入时就建**,真实 ClickHouse 是 **merge 时**才建。所以这里的 `system.parts` 一插入就能看到 `skp_idx_*` 文件。
- **一个 granule 一个压缩块。** 于是 `.mrk2` 三元组里的 `offset_in_decompressed_block` **恒为 0**——读代码时会以为这个字段没用,那是因为压缩块边界与 granule 边界对齐了。
- **`.mrk2` 而不是 `.mrk3`。** `mrk3` 的编解码函数在,但写入用的是 `mrk2`。
- **跳数索引只注册了 5 种**:`minmax` / `set(N)` / `bloom_filter` / `ngrambf_v1` / `tokenbf_v1`。真实额外还有全文检索索引(`text`/`inverted`)与实验性的 `vector_similarity`。多列(元组)的 `minmax` **保守放行**(永远说"可能有")。
- **`system.parts` 里,没有 part 概念的引擎一行都不出现。** `Memory` / `Log` / `Null` 等在这里查不到——这是"降级为空"而不是报错,因为"没有 part"本身是个合法答案。
- **`MATERIALIZED` 列不能被显式写入。** `_align_block` 会拦。
- **`a = NULL` 会直接报错,不是返回空集。** 实测 `SELECT count() FROM n WHERE a = NULL`(`a` 是 `Nullable(UInt64)`)抛 `Code: 24 ... Illegal types of arguments for function equals: UInt64, Nothing`。判断空值请用 `IS NULL` / `isNull()`。真实 ClickHouse 25.3 在这一点上的确切行为我**未确认**——不要用这一段去推断真实行为。
- **`avg` 空分组返回 `nan`**,不是 0 也不是 NULL;要 NULL 得用 `avgOrNull`。
- **`MD5` 返回 `FixedString(16)` 原始字节**,不是 32 字符十六进制串——这一点**与真实 ClickHouse 一致**。要注意 `hex` 的输出是**大写**(附录 A 的 `hex` 条目写明了这一点),所以 `hex(MD5(''))` 得到的是 `'D41D8CD98F00B204E9800998ECF8427E'`(全大写);若要与习惯小写的十六进制串比对,请两边都先 `lower()`。
- **`join_use_nulls` 默认 0**,与真实 ClickHouse 一致(未匹配的列填类型默认值而不是 NULL)。`join_default_strictness` 默认 `ALL`——`joins.py` 的 docstring 专门澄清了"ClickHouse 默认 ANY"这个流行说法。
- **哈希表不落盘、JOIN 右表全量进内存。** `_build_source` 会把 `FROM` 物化成一个 `Block`,JOIN 的右表也一样。
- **Keeper 是单节点、无 Raft。** `multi` **不是原子的**(真实 ZooKeeper 的是);watch 是**一次性**的;真实 CH 在 `/blocks/<hash>` 节点里还会存更多信息。
- **复制是进程内仿真。** `LocalCluster` 在一个进程里跑 N 个 Replica 共享一个 `KeeperServer`,进程重启会重建全部内存状态——**没有跨进程的副本会话**。
- **权限默认不强制。** `Context.user` 是一个被完全信任的字符串:没有握手、没有密码校验、没有会话令牌。`access.py` 开头的 warning 说得很直白:这是教学实现,不要拿它处理不能泄露的数据。
- **`MATERIALIZED VIEW` 是插入触发器**,不是定时刷新;支持 `POPULATE`。
- **`GraphiteMergeTree` 的 rollup 规则不生效**——引擎能注册能建表,但说明里写着 "registered but NOT applied"。

### B.6.5 工具链上的小坑

- **`pyproject.toml` 里刻意没有写 `timeout`。** 那个选项来自 `pytest-timeout` 插件,而依赖里没有它——写了只会得到一条 `Unknown config option: timeout`,`pyproject.toml` 里有一段注释专门说明这一点。(顺带更正一处旧说法:`cache_dir` **是** pytest 认可的 ini 选项,实测跑测试时 warnings summary 是空的。真正"写了不生效"的是 `timeout`。)
- **`import minich as ch; ch.Context()`** 能用了(包级导出了 `__version__` / `TARGET_CLICKHOUSE_VERSION` / `Context` / `Client` / `QueryResult` / `MiniChError`)。如果你读到的版本里没有,等价写法是 `from minich.execution.context import Context`。
- **引擎模块必须挂进 `storages/__init__.py` 的导入链。** 漏了不会报错,只会让 `CREATE TABLE ... ENGINE = X` 说"未知引擎"。
- **注册是 import 的副作用。** `minich/functions/`、`minich/aggregates/`、`minich/storages/`、`minich/system/` 四个包都是"导入即注册"。所以只 `from minich.functions.string import ...` 而不 `import minich.functions` 时,注册表可能是空的。
- **`Context.shutdown()` 必须幂等,而且应该调用。** 后台合并线程是 daemon 线程,不关会持续吃 CPU 并干扰下一个用例。
- **`QueryResult` 的方法是方法不是属性。** `r.row_count` 拿到的是绑定方法,要写 `r.row_count()`。

## B.7 九个练习

九个练习按难度排列,每个都以"**能跑出可见结果**"为标准设计——不是"请你思考一下",而是"改完之后这条命令的输出会变"。前四个是热身,后五个是需要真正动执行路径的改造。

### B.7.1 给 `system.parts` 加一列(易)

**目标。** 在 `system.parts` 里多一列 `bytes_per_mark`,值 = `bytes_on_disk / marks`(marks 为 0 时给 0)。这是排查"小 mark 过多导致 IO 放大"时真正会用到的派生列。

**改哪里。** `minich/system/tables.py` 的 `SystemParts`:

1. 在 `columns` 列表末尾加 `("bytes_per_mark", "UInt64")`;
2. 在 `_part_row()` 返回的元组**同样的位置**补一个值。

**可见结果。** `SELECT name, rows, marks, bytes_on_disk, bytes_per_mark FROM system.parts WHERE active` 会多出一列,而且这一列参与了真实的计算。

**验收。**

```bash
python -c "
import tempfile, minich as ch
ctx = ch.Context(data_path=tempfile.mkdtemp())
ctx.execute('CREATE TABLE p (id UInt64) ENGINE = MergeTree ORDER BY id')
ctx.execute('INSERT INTO p SELECT number FROM numbers(20000)')
print(ctx.execute('SELECT name, rows, marks, bytes_on_disk, bytes_per_mark FROM system.parts WHERE active').text())
"
```

**注意。** 元组的位置必须和列定义一致——错位不会报错,只会让所有列的值整体平移一位。这正是 `columns` 与行元组"两处必须同步"这种设计的典型风险,也是真实 ClickHouse 用结构化描述符而不是位置元组的原因之一。

### B.7.2 补一个缺失的标量函数(易)

**目标。** 实现一个真实 ClickHouse 有、mini-clickhouse 没有的函数。用注册表查一下就知道缺什么——实测这几个是缺的:`toDecimalString`、`URLHash`、`randomString`。

```bash
python -c "
from minich.functions.base import function_exists
for n in ['toDecimalString', 'URLHash', 'randomString', 'isFinite', 'dateDiff']:
    print(('有  ' if function_exists(n) else '缺  ') + n)
"
```

**改哪里。** 按语义选模块:`toDecimalString` → `functions/type_funcs.py`,`URLHash` → `functions/url_ip.py`,`randomString` → `functions/misc.py`(注意它是**非确定性**函数,要把 `deterministic = False`,否则常量折叠会把整条查询里的调用折叠成同一个值)。

**可见结果。** `SELECT toDecimalString(toDecimal64(1.5, 2))` 返回 `'1.50'`。

**验收。**

```bash
python -c "
import minich as ch
ctx = ch.Context()
print(ctx.execute(\"SELECT toDecimalString(toDecimal64(1.5, 2))\").scalar())   # 1.50
print(ctx.execute('SELECT name, description FROM system.functions WHERE name = \\'toDecimalString\\'').text())
"
```

第二条命令是重点——它证明新函数**进了注册表**,而不只是"这个模块里的一个类"。

### B.7.3 把一个逐行函数向量化,并量出差距(易→中)

**目标。** 挑一个只实现了 `execute_row` 的函数(基类会给你一个逐行回退的 `execute`),覆盖它、改成整列一次算完,然后**用秒数证明它变快了**。

**改哪里。** 比如 `functions/rounding.py` 里的某个取整函数。参考 `functions/encoding.py` 的 `_EncodingFunction.execute` 或 `math_funcs.py` 的写法:先 `col = args[0].column; if col.is_const(): col = col.convert_to_full_column()`,然后一次遍历整列。

**可见结果。** 同样的查询在改造前后耗时不同。**这是唯一一个"性能数字合法"的练习**——它量的是同一个实现内部的相对变化,不涉及与真实 ClickHouse 的比较。

**验收。**

```bash
python -c "
import time, minich as ch
ctx = ch.Context()
q = 'SELECT sum(round(number / 7)) FROM numbers(2000000)'
t0 = time.time(); ctx.execute(q); print('%.2f s' % (time.time() - t0))
"
```

**注意。** 别指望"向量化"一定变快——如果 NumPy 调用本身的构造开销大于收益(小数组、复杂类型),逐行反而更快。**先量再改**,不要按信仰优化。第 17 章有一个 200 万行的量化例子。

### B.7.4 让 `EXPLAIN PIPELINE` 画出真实连线(中)

**目标。** 现在的 `EXPLAIN PIPELINE` 已经会打印每段的算子**数量**(`(ReadFromStorage × 1)`),但那还是一个"分段列表"。把它改成**真实的对象级视图**:每个 processor 一个编号,并打印它与谁相连。

**改哪里。** `minich/execution/explain.py` 的 `_explain_pipeline`。信息源是 `processors.py` 里的端口对象——`OutputPort.peer` 指向下游的 `InputPort`,`InputPort.parent` / `OutputPort.parent` 指回各自的 `IProcessor`。所以只要遍历处理器、按 `id()` 排个序、把 `类型名 #编号 → 类型名 #编号` 打出来就行。

**可见结果。** 输出从"三段列表"变成"一张带编号的图"。

**验收。**

```bash
python -c "
import tempfile, minich as ch
ctx = ch.Context(data_path=tempfile.mkdtemp())
ctx.execute('CREATE TABLE t (a UInt64, b String) ENGINE = MergeTree ORDER BY a')
ctx.execute('INSERT INTO t SELECT number, toString(number) FROM numbers(1000)')
print(ctx.execute('EXPLAIN PIPELINE SELECT b, count() FROM t WHERE a > 10 GROUP BY b').text())
"
```

**为什么这个练习值得做。** 它逼你回答一个真实 ClickHouse 用户天天问的问题:"这条查询最终跑成了几个算子、谁接谁?"第 10 章展示了真实 ClickHouse 的输出形态(同一算子出现多份、有 `Resize` 节点),做完这个练习你就能对着那张图说清"我们的差异具体在哪几个节点上"。

### B.7.5 让 `LIMIT` 真的下推到存储层(中)

**目标。** 把 `LIMIT 10` 传进 `ReadContext.limit`,让存储层读够 10 行就停,而不是读完 20 万行再截断。

**改哪里。**

1. `interpreter.py` 的 `_read_storage_table`:构造 `ReadContext` 时把 `limit=` 填上。取值的来源有两个——查询里直接写的 `LIMIT`,以及 `optimizer.optimize_select()` 已经算好并存在 `self.optimization.limit` 里的结果(后者更"正确",因为它会判断能不能下推)。
2. 存储层**已经支持**了:`MergeTreeData.read()` 里有 `if rc.limit is not None and produced >= rc.limit: return`。所以你只需要把值送过去。

**可见结果。** `SELECT id FROM k LIMIT 10` 的 `SelectedRows` / `SelectedMarks` 大幅下降(改之前的完整基线见 §B.6.2)。

**验收。**

```bash
python -c "
import tempfile, minich as ch
ctx = ch.Context(data_path=tempfile.mkdtemp())
ctx.execute('CREATE TABLE k (id UInt64) ENGINE = MergeTree ORDER BY id')
ctx.execute('INSERT INTO k SELECT number FROM numbers(200000)')
b = ctx.profile_events.snapshot(); ctx.execute('SELECT id FROM k LIMIT 10')
a = ctx.profile_events.snapshot()
print({k: a.get(k, 0) - b.get(k, 0) for k in ('SelectedMarks','SelectedRows')})
"
```

(两边的 `.get(k, 0)` 不能省:`snapshot()` 返回的字典**只含非零项**,直接下标取值会抛 `KeyError`——"读之前"那一份快照里根本没有 `SelectedMarks` 这个键。)

改之前的状态见 §B.6.2;改之后应当只剩一两个 mark。

**注意。** `LIMIT` 下推**只在没有 `ORDER BY` / `GROUP BY` / `WHERE` 时**是安全的——否则"前 10 行"不是用户要的那 10 行。这就是为什么真实 ClickHouse 用的是优化器规则而不是无条件下推;也是为什么这个练习要求你先去看 `optimizer.py` 里 `limit_push_down` 那一段的判断条件,再决定把什么值送下去。

### B.7.6 加一种跳数索引并验证摘要字节(中)

**目标。** 在 `skip_index.py` 里加第六种跳数索引类型,比如 `minmax_string_len`(记每个粒度组里字符串长度的 min/max),然后验证它**写进了 part**、摘要**能被反序列化回来**。

**改哪里。**

1. 在 `skip_index.py` 里写一个继承 `IMergeTreeIndex` 的类,设 `index_type = "minmax_string_len"`,实现 `build(column) -> bytes` 与 `deserialize(data) -> IndexGranule`;
2. 在类里实现 `may_be_true(cond) -> bool`——**必须遵守那条不变量:允许错报真,绝不允许漏报**。拿不准就返回 `True`;
3. 在 `_INDEX_TYPES` 字典里登记:`"minmax_string_len": MinMaxStringLenIndex`。

**可见结果。** `CREATE TABLE ... INDEX idx_url url TYPE minmax_string_len GRANULARITY 1` 能建出来,`system.parts` 对应的目录里出现 `skp_idx_idx_url.idx`,而且 `EXPLAIN` 的输出里能看到这个索引名。

**验收。**

```bash
python -c "
import tempfile, os, minich as ch
d = tempfile.mkdtemp()
ctx = ch.Context(data_path=d)
ctx.execute('CREATE TABLE s (id UInt64, url String, INDEX iu url TYPE minmax_string_len GRANULARITY 1) ENGINE = MergeTree ORDER BY id')
ctx.execute(\"INSERT INTO s SELECT number, repeat('x', number % 20) FROM numbers(20000)\")
# 找出 part 目录里的 skp_idx 文件
for root, dirs, files in os.walk(d):
    for f in files:
        if f.startswith('skp_idx'):
            print(os.path.join(root, f), os.path.getsize(os.path.join(root, f)))
"
```

**这个练习的真正价值**不在"多一种索引",而在它会逼你想清楚一个反直觉的接口设计:`build()` 收到的**只有一个粒度组的数据**(行数 = 一个 granule),它必须自己把这个粒度组压成一段不可再分的摘要字节;而 `may_be_true()` 拿到的是**一个条件**(`IndexCondition`,带 `op` 与 `value`),它要回答的是"这个粒度组里**可能**有满足该行的行吗"。这两件事之间没有任何共享状态——这是跳过索引能写在高层的根本原因。

### B.7.7 把列裁剪接进 SELECT 路径(难)

**目标。** 让 `SELECT id FROM wide_table` 只读 `id` 那一列的 `.bin`,而不是读全部 20 列。

**改哪里。** `interpreter.py` 的 `_read_storage_table`。现在是这样:

```python
read_ctx = ReadContext(
    ctx=self.ctx,
    columns=None,
    settings=self.ctx.settings,
)
read_ctx.where = self._pending_where
read_ctx.prewhere = self._pending_prewhere
self.read_context = read_ctx
```

需要的列在拿到分析结果之后才算得出来,所以你面对的是一个**真正的结构问题**:要么在读之前先做一次"轻量分析"(只解析表达式里出现的标识符)拿到列清单,要么改成两次读取(第一次拿表头、第二次带列清单)。**第二种更简单,但会让源被读两遍**——要留意 `SelectedRows` 会不会因此翻倍,这个副作用曾经出现过(在写这一章的期间它被修掉了),所以改完一定要量一下。想更彻底一点,可以顺便把"探查读 + 流水线读"合并成一次。

**可见结果。** `ReadCompressedBytes` 随列宽**线性下降**。这是 §B.6.2 那组实验的正面版本。

**验收。**

```bash
python -c "
import tempfile, minich as ch
ctx = ch.Context(data_path=tempfile.mkdtemp())
cols = ', '.join('c%d String' % i for i in range(20))
ctx.execute('CREATE TABLE w (id UInt64, %s) ENGINE = MergeTree ORDER BY id' % cols)
ctx.execute('INSERT INTO w SELECT number, %s FROM numbers(30000)' % ', '.join([\"repeat('x',100)\"]*20))
for q in ['SELECT id FROM w', 'SELECT * FROM w']:
    b = ctx.profile_events.snapshot(); ctx.execute(q); a = ctx.profile_events.snapshot()
    print(q, a.get('ReadCompressedBytes', 0) - b.get('ReadCompressedBytes', 0))
"
```

(同样地,`.get(k, 0)` 不能省——`snapshot()` 只含非零项。实测这两条现在各读 **630 000** 字节。)

改之前两条查询的字节数**相同**;改之后 `SELECT id FROM w` 应当只有 `SELECT * FROM w` 的几十分之一。

### B.7.8 给 JOIN 接上内存保护(难)

**目标。** 现在的 JOIN 会把右表**整份读进内存**(`_materialize_join` 先把右表物化成一个 `Block`),而且**完全不检查** `max_bytes_in_join` / `max_rows_in_join`。`joins.py` 的模块 docstring 自己把这一条写成了已知缺口。这个练习就是把它补上。

**改哪里。**

1. `minich/execution/interpreter.py` 的 `_materialize_join`:在把右表做成哈希表之前,先按 `self.ctx.settings` 取两个上限:`max_rows_in_join`(默认 0 = 不限)、`max_bytes_in_join`(默认 0 = 不限)。两者都为 0 时保持现状(默认行为不能变)。
2. 超限时的行为由 `join_overflow_mode`(默认 `throw`)决定:先只实现 `throw`——抛一个明确的错误,消息里带上"实际多少行 / 上限多少"和"该怎么调"。这是最快的可见结果。
3. 想再进一步:`join_overflow_mode = 'break'` 表示"保留已经建好的那一部分"(结果是近似的,真实 ClickHouse 允许这么用),`'any'` 表示"随便留一部分"。**先把 `throw` 做对**,因为它是默认值,也是唯一不会给出错误结果的模式。

**可见结果。** 一个"小表 JOIN 大表"的查询在 `max_rows_in_join` 设小之后**报错**,而且是带着数字的明确报错;不设的时候照常跑完。

**验收。**

```bash
python -c "
import minich as ch
ctx = ch.Context()
ctx.execute('CREATE TABLE l (a UInt64) ENGINE = Memory')
ctx.execute('CREATE TABLE r (a UInt64, b UInt64) ENGINE = Memory')
ctx.execute('INSERT INTO l SELECT number FROM numbers(1000)')
ctx.execute('INSERT INTO r SELECT number, number FROM numbers(100000)')
q = 'SELECT count() FROM l INNER JOIN r ON l.a = r.a'
print('默认:', ctx.execute(q).scalar())                                  # 1000,不限行数
try:
    ctx.execute(q, settings={'max_rows_in_join': 100})
    print('没报错 —— 说明保护还没接上')
except Exception as e:
    print('超限报错:', str(e)[:120])
"
```

**为什么这条练习值得做。** 因为它把一个**默认不变的设置**变成了真实的行为约束。真实 ClickHouse 里 `max_bytes_in_join` 是防"一个 JOIN 把内存吃光"的最后一道闸,而它的默认值**也是 0(不限)**——也就是说这个保护需要运维显式打开。你在 mini-clickhouse 上把这条通路走一遍,就会理解"为什么生产环境必须显式设它",而不是等到 OOM 那天。

**做完之后顺手验证一个"不该变好"的查询。** 把 `max_rows_in_join` 设得足够大(比如 1000000),同一个查询应当**照常返回 1000**。如果它也报错了,说明你的判断条件写成了 `>=` 或者把默认值当成了上限。

### B.7.9 修正窗口帧的 `RANGE` 语义(难)

**目标。** 让 `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` 在有**并列值**时给出与 `ROWS` 不同的结果:`RANGE` 把所有并列值当成"同时到达",因而它们得到**相同**的累计值。

**改哪里。** `minich/execution/window.py` 的 `_frame_bounds`(差异就写在它的注释里)。核心工作是:当帧类型是 `RANGE` 时,帧的右边界不是"当前行号",而是"**最后一个与当前行 `ORDER BY` 键相等的行**"。所以你需要按 `ORDER BY` 键做一次分组,把每个位置映射到它所在的"并列组"的末尾位置。

**可见结果。** 造一个有两行 `k = 5` 的数据集,比较同一列上两种帧的输出。

**验收。**

```bash
python -c "
import minich as ch
ctx = ch.Context()
ctx.execute('CREATE TABLE w (k UInt64, v UInt64) ENGINE = Memory')
ctx.execute('INSERT INTO w VALUES (1,10),(5,20),(5,30),(9,40)')
rows = \"SELECT k, sum(v) OVER (ORDER BY k %s) AS s FROM w ORDER BY k\"
print('ROWS :', ctx.execute(rows % 'ROWS  BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW').rows())
print('RANGE:', ctx.execute(rows % 'RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW').rows())
"
```

现在两者**给出完全相同的答案** `[(1,10), (5,30), (5,60), (9,100)]`——`RANGE` 被当成 `ROWS` 了。改对之后 `RANGE` 那一行应当是 `[(1,10), (5,60), (5,60), (9,100)]`:`k=5` 的两行因为并列而拿到**相同**的累计值 60。

（这里 `AS s` 只是给结果列起个名字。真正的坑在 `ORDER BY` 那一侧:引用窗口函数的**别名**没问题(`ORDER BY k, s` 能跑),但**裸写**窗口函数本身会报 `Window function ... must be evaluated by the WindowTransform`——`ORDER BY k, sum(v) OVER (ORDER BY k)` 实测如此。验收脚本的 `ORDER BY` 只写 `k`,正好绕开了这一点。）

**为什么放最后一个。** 因为它要求你先想清楚一个容易被"实现细节"淹没的问题:**帧到底定义在"行"上还是定义在"排序键的值"上?** `ROWS` 是前者,`RANGE` 是后者。想清楚这一点,代码只有十几行;想不清楚,就会写出一个在无并列值时"看起来对"的实现——而绝大多数测试数据恰好都是无并列值的。

## B.8 小结

- **这份附录解决"查"的问题**,不解决"懂"的问题。懂的过程在第 31 章:为什么这么设计、和真实 ClickHouse 差在哪。
- **目录结构**(B.1):`minich/` 下 123 个 `.py` 文件、86 242行,分成 12 个包。包结构与真实 ClickHouse 的 `src/` 刻意对齐,对照表在第 31 章 §31.2。
- **能力矩阵**(B.2)按九个维度展开:数据模型、压缩编码、格式、函数与聚合、SQL 与执行、JOIN/窗口/排序、存储引擎、MergeTree 各部件、周边设施(分区/字典/复制/Keeper/权限/system 表/设置/客户端)。**凡是"部分实现"的,第三列都写清了边界在哪。**
- **API 参考**(B.3)给了八组照源码抄下来的签名:`Context`、`QueryResult`、`Evaluator`/`Scope`、`IStorage`(含 `ReadContext` / `StorageFeatures` / 注册表)、`ISerialization`、`ICompressionCodec`、`IAggregateFunction`、`IProcessor`。
- **扩展指南**(B.4)三个模板:加标量函数(四步:选模块 → 写类 → 注册 → 自测)、加聚合函数(第一步就想清楚"状态是什么,而且它必须可合并")、加表引擎(五步,其中**第四步"挂进导入链"最容易漏,而且漏了不报错**)。
- **验证矩阵**(B.5):`pytest`(923 个测试函数、展开后 2813 条通过)、`examples/`(14 个脚本全部执行成功)、`tools/check_docs.py`(20 项结构自检)。**这三条命令就是这本书的"可复现性"的全部内容**,没有第四个来源。
- **陷阱清单**(B.6)按"危害方式"分组:会给出**错误结果**的、会显得**没生效**的、**结构性缺失**的、**语义细节**的、**工具链**的。前三组里的每一条都写清了"会影响什么判断"。
- **九个练习**(B.7)从"给 `system.parts` 加一列"到"修正 `RANGE` 帧语义"。它们不是玩具:B.7.5(下推 `LIMIT`)、B.7.7(接上列裁剪)、B.7.8(给 JOIN 加内存保护)改的正是这份实现当下**最值钱的几处缺口**,做完之后用 `profile_events` 能直接量出收益。**每个练习的验收命令都给全了,照抄即可。**


