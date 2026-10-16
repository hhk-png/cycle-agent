# mini-clickhouse

一个**可读、可运行、可验证**的列式 OLAP 数据库教学实现 —— 用纯 Python
把 ClickHouse 的核心机制实现了一遍。

它不是 ClickHouse 的绑定、也不是它的简化版客户端,而是**另写一个引擎**:
自己实现类型系统、列存、序列化、压缩、SQL 解析、执行引擎、存储引擎、
`system` 表、字典、Keeper 与复制协调。目的是让读者能把教程里每一句
"ClickHouse 是这么做的"**落到一段能读、能改、能跑出结果的代码上**。

```python
import minich

ctx = minich.Context(data_path="./data")
ctx.execute("""
    CREATE TABLE hits (id UInt64, url String, ts DateTime, v UInt32)
    ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY (ts, id)
""")
ctx.execute("""
    INSERT INTO hits
    SELECT number, concat('/p/', toString(number % 10)),
           toDateTime('2024-01-01 00:00:00') + number, toUInt32(number % 100)
    FROM numbers(200000)
""")
print(ctx.execute("""
    SELECT url, count() AS c, round(avg(v), 2) AS v
    FROM hits GROUP BY url ORDER BY c DESC LIMIT 5
""").text())
ctx.shutdown()
```

---

## 这个实现有什么用 / 没什么用

**有用**:它是**唯一能让你在没有 ClickHouse 的机器上,把"列存怎么存、
索引怎么裁、聚合怎么并行、mutation 为什么要等"跑出来看**的东西。
教程第 31 章按十个关键设计走读它;第 34 章是完整的工程手册。

**没用**:它**不快**,而且**永远也不会快**。

- Python 的对象开销让"向量化"退化成了"整列循环" —— 我们的列是
  NumPy 数组,但每一次函数调用都要穿过 Python 的解释器;
- 流水线是**单线程**的:真实的 `PipelineExecutor` 把每个 processor 派到
  线程池上,mini-clickhouse 在单线程里按拓扑序轮转。**语义相同,并行度不同**;
- 阻塞算子(聚合、排序、JOIN)会把中间结果**物化成 Block** 再起下一段流水线,
  而真实 ClickHouse 全程只让 `Chunk` 在图上流动。

所以:**不要拿它的耗时去和真实 ClickHouse 比**,也不要用它得出任何性能结论。
它能证明的是"机制是这样运作的",不是"这样运作有多快"。

---

## 实现了什么

先说规模,再说取舍。

| 部件 | 数量 | 说明 |
|---|---|---|
| 类型族 | 56 | 数值 / 字符串 / 日期时间 / 枚举 / UUID / IP / `Decimal` / `Array` / `Tuple` / `Map` / `Nullable` / `LowCardinality` / `Nested` / `AggregateFunction` / `Variant` / `Dynamic` / `JSON` |
| 标量函数 | 658 | 含别名;算术、比较、条件、字符串、正则、日期时间、数组、高阶、哈希、编码、JSON、URL/IP、类型转换 |
| 聚合函数 | 65 | 含 `uniq`(**真实的 HyperLogLog**)、`quantile`、`groupArray`、`topK`、统计族;以及 `-If`/`-Array`/`-State`/`-Merge`/`-Distinct`/`-OrNull`/`-OrDefault`/`-Resample`/`-ForEach` 组合器与它们的叠加 |
| 表引擎 | 23 | `MergeTree` 全家族(含 Replacing/Summing/Aggregating/Collapsing/VersionedCollapsing/Replicated)、Log 三兄弟、`Memory`/`Null`/`Set`/`Join`/`Buffer`/`Merge`/`File`/`URL`/`Values`、`View`/`MaterializedView` |
| 表函数 | 30 | `numbers`/`zeros`/`input`/`values`/`file`/`url`/`format`/`generateRandom`/`merge`/`view`/`null` 已实现;`s3`/`mysql`/`hdfs` 等注册了名字并抛 `NOT_IMPLEMENTED` |
| 数据格式 | 24 | TSV/CSV 家族、JSON 家族(6 种)、`Values`、`Pretty` 家族、`Vertical`、`Markdown`、`SQLInsert`、`RowBinary`、`Native`;**可往返的 16 种** |
| 压缩编码 | 10 | `NONE`/`LZ4`/`LZ4HC`/`ZSTD`/`Delta`/`DoubleDelta`/`Gorilla`/`T64`/`FPC`,外加链式容器 `Multiple`(`Brotli`/`Snappy` **未实现**) |
| 设置 | 124 | 类型、默认值、范围、层级、别名都按真实结构建模 |
| `system` 表 | 43 | 结构、注册表、指标、日志、访问控制四大类 |
| processor | 20+ | 源、汇聚、表达式、过滤、聚合(两阶段)、排序、归并、去重、`LIMIT BY`、`ARRAY JOIN`、窗口、扇出扇入 |

再加上:完整 SQL 解析器(词法 + 递归下降 + **有界回溯**,支持 `WITH`/`JOIN`/
子查询/窗口/`UNION`/`ARRAY JOIN`/全部 DDL)、推送模型的执行引擎、
哈希连接与四种严格度、窗口函数的排名/偏移/聚合三族、MergeTree 的
part/granule/mark 布局与**稀疏主键索引**、五种跳数索引、k 路归并、
引擎家族的行折叠、mutation 与 TTL、外部字典(7 种 LAYOUT)、
Keeper(znode 树 + 会话 + 一次性监听)、三副本复制协调、
访问控制(用户/角色/授权/行策略/配额)、以及一个简化协议的
客户端/服务端与 CLI。

---

## 明确不做

读懂这份清单比读懂上面那份更重要 —— **它决定了哪些结论你不能从这里得到**。

**执行层**:真正的多线程流水线(单线程轮转,语义同、并行度不同)、
基于代价的优化器(只有确定性改写规则)、`ActionsDAG`(表达式逐棵递归求值,
公共子表达式会算两次)、真正的流式输出(阻塞算子会物化)。

**存储层**:`Distributed` 分片、对象存储(S3/HDFS)、投影、`SharedMergeTree`、
真正的冷热分层(有 TTL 与 volume 的建模,但没有多盘调度)、
`OPTIMIZE` 之外的后台调度器(有后台合并线程池,但没有后台 TTL/fetch 调度)、
并发写同一张表。

**语义上的刻意简化**(每一条都在代码里有 `.. warning::`):
- 窗口帧的 **`RANGE` 被当成 `ROWS`** —— 有并列排序键时会与真实 ClickHouse 不一致;
- `cityHash64` 是 `siphash64` 的别名,输出与真实 ClickHouse **不同**;
- `LZ4`/`ZSTD` 是自己写的同类算法,**与官方库字节不兼容**;
- `Native` 格式**不与真实 ClickHouse 互通**(少了 BlockInfo 与版本协商);
- `join_use_nulls` 默认与真实一致都是 0(填类型默认值),但 `ASOF` 只按等值键
  取最后一个匹配,不解析 `ON` 里的不等值条件;
- `uniq` 用了带小基数修正的 HLL,所以在小基数上**比真实 ClickHouse 更准**
  (那句"两个不同值返回 3"在这里不会复现);
- 部分默认值标了"未确认",因为查不到可靠来源 —— 它们**不会被编造**。

**安全**:没有网络认证,`Context.user` 被无条件信任,默认**不强制鉴权**。
这是一个教学实现,不要拿它处理你不能泄露的数据。

---

## 快速开始

```bash
cd mini-clickhouse

# ① 全部测试
python -m pytest tests/

# ② 全部示例(每个都自证有输出)
for f in examples/*.py; do python "$f"; done

# ③ 文档结构自检(20 项检查,带退出码)
cd .. && python mini-clickhouse/tools/check_docs.py
```

单独试一个功能:

```bash
python -m minich demo                                     # 自包含演示
python -m minich local -q "SELECT count() FROM numbers(1000000)"
python -m minich local -q "SELECT number, number*number FROM numbers(5) FORMAT PrettyCompact"
python -m minich benchmark -q "SELECT count() FROM numbers(100000)" --iterations 5
python -m minich client                                   # 交互式 REPL
python -m minich server --port 9000                       # 起服务端
python -m minich format --in TSV --out JSONEachRow data.tsv
```

**依赖**:Python ≥ 3.10 与 NumPy。没有别的。

---

## ✅ 验证状态

*(这一节里的每一条都能用上面的命令复现。)*

| 项 | 结果 |
|---|---|
| 测试 | `python -m pytest tests/` —— `2813 passed, 83 skipped, 49 xfailed` |
| 示例 | `for f in examples/*.py; do python "$f"; done` —— `14/14 exit 0` |
| 文档结构自检 | `python mini-clickhouse/tools/check_docs.py` 20 项全通过 |
| 规模 | `minich/` 约 8.5 万行 Python |
| 依赖 | Python ≥ 3.10 + NumPy;无其它第三方依赖 |
| 验证环境 | Windows 11 + Python 3.14.6 + NumPy 2.5.1(无需 GPU) |

**诚实说明**:mini-clickhouse 在本机**完整验证**。教程里对**真实 ClickHouse**
的行为描述基于官方文档与对 25.3 的核对,但本机**没有安装 ClickHouse** ——
那些示例属"文档级准确"而非"本机跑过",正文中已逐处标注来源,
不能确认的写"未确认"。

---

## 代码审计与已知缺陷

上面那三条命令**全绿**,不等于没有 bug。本项目做过一轮**独立审计** —— 它的任务不是"跑一遍验证",而是"想办法证明这份实现是错的" —— 结果在测试全绿的情况下找出了**若干真实缺陷**,每一个都有可直接粘贴运行的最小复现。这些缺陷的成因、影响,以及"它会影响你哪一个判断",已经归类整理在**教程第 34 章(附录 B)的 §B.6「已知取舍与陷阱清单」**里:那一节按"会给出错误结果 / 会显得没生效 / 结构性缺失 / 语义细节差异 / 工具链小坑"五类逐条列出,想知道**哪些结论不能从这份实现里得到**,读那一节即可。这里不再重复叙述。

与**跑本项目**直接相关、因此留在这里的只有一件:审计之后,一类错误被固化成了**结构性回归测试**。"引用了不存在的错误码"早先出现过五次,而且每次都要到运行时才抛 `AttributeError`;现在 `tests/test_regressions.py` 里有一条断言会把整个仓库扫一遍,任何被引用的 `ErrorCode.X` 都必须在对应类上存在。它不需要你预先想到每一条错误路径 —— 只要你跑测试,它就会替你拦住这类回归。

---

## 与真实 ClickHouse 的对应关系

这份实现的目录结构和真实 ClickHouse 的 `src/` 是**一一对应**的,
这样读者在两边跳转时不需要重新建立心智模型:

| mini-clickhouse | 真实 ClickHouse(`src/`) | 职责 |
|---|---|---|
| `errors.py` | `Common/ErrorCodes.cpp` | 错误码与异常 |
| `types/` | `DataTypes/` | 类型系统 |
| `columns/` | `Columns/` | 列的物化 |
| `block.py` | `Core/Block.h` / `Chunk.h` | 块与表头 |
| `serialization.py` | `DataTypes/Serializations/` | 二进制序列化 |
| `compression.py` | `Compression/` | 压缩编码 |
| `formats/` | `Formats/` | 输入输出格式 |
| `sql/` | `Parsers/` | 词法、语法、AST |
| `execution/` | `Processors/` + `Interpreters/` | 执行引擎与解释器 |
| `functions/` | `Functions/` | 标量函数 |
| `aggregates/` | `AggregateFunctions/` | 聚合函数与组合器 |
| `storages/` | `Storages/` | 表引擎 |
| `system/` | `Storages/System/` | `system` 表 |
| `dictionaries.py` | `Dictionaries/` | 外部字典 |
| `keeper.py` | `Coordination/` | Keeper(ZooKeeper 协议) |
| `replication.py` | `Storages/StorageReplicatedMergeTree*` | 副本协调 |
| `access.py` | `Access/` | 权限 |
| `settings.py` | `Core/Settings.cpp` | 设置系统 |
| `client.py` / `protocol.py` | `Client/` | 客户端与服务端 |
| `catalog.py` | `Interpreters/DatabaseCatalog.cpp` | 目录与生命周期 |

更细的对应关系与"为什么这一层要单独存在"写在每个包的 `__init__.py` 里;
完整的能力矩阵与扩展指南见教程第 34 章(附录 B)。
