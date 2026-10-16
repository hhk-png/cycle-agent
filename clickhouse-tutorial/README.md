# ClickHouse 完全教程

从列存的物理布局讲到集群运维,每一层都配一个**可运行、可验证**的简化实现。
`mini-clickhouse/` 是纯 Python 写的列式 OLAP 引擎(不依赖 ClickHouse 本身),
教程里凡是涉及"内部是怎么做的"的地方,都能在它里面找到对应代码并跑起来。

把这份教程和只讲语法的文档区分开的,是三条纪律:

1. **每一层都讲到能解释性能现象的深度。** 不是"ClickHouse 很快",而是
   一个 part 里数据怎么排、主键索引为什么是稀疏的、granule 为什么是 8192 行、
   哪些 `WHERE` 能用上索引哪些用不上。学会之后你能自己解释"为什么这条查询
   扫了 10 亿行而那条只扫了 1 万行"。
2. **每个机制都有一个能跑的对照实现。** 觉得"稀疏主键索引"抽象?
   `examples/02_mergetree_parts.py` 会把 part 目录、granule 目录、
   `primary.idx` 的实际字节数打给你看。
3. **事实与判断分开,不确定的明确标注。** 所有"真实 ClickHouse 的行为"
   以 **25.3** 为基准;凡是写不出可靠来源的,正文里写"**未确认**"而不是
   编一个像样的答案。

---

## 这份教程给谁看

**如果你已经会写 SQL,但没碰过 ClickHouse**:从第 01 章开始,按第 01 → 02 →
04 → 06 → 07 → 12 章的次序读下去(第 03 章讲类型语义,跳过它不会卡住——
等碰到类型问题时再回头读)。

**如果你从 MySQL / PostgreSQL 迁过来**:主线读 **第 37 章(附录 E:MySQL 与
PostgreSQL 迁移 playbook)**——它把"先改哪些心态、类型怎么换成等价的、哪些子句
语义不同、哪些同名函数不等价"按迁移的顺序串成一条可执行的流程。跟着它走会顺路
读到第 02 章 §02.8(五个心态转变)、第 03 章(类型语义的差异最多)、第 15 章
(JOIN 要小心)、第 18 章(更新是异步的)、第 28 章(反模式清单)。搬过来之后要
搭什么,接着读第 38 章(附录 F:端到端实战)。

**如果你要调优一个已经在跑的集群**:第 05 章(存储结构)和第 11 章(索引)
是根,第 24 章(执行计划)是工具,第 28 章(反模式)是清单,
第 27 章(系统表)是眼睛。

---

## 章节导航

| 章 | 标题 | 一句话 |
|---|---|---|
| 00 | [前言与导读](clickhouse教程-00-前言与导读.md) | 教程定位、四条阅读路径、结构地图、事实标注规则 |
| 01 | [认识 ClickHouse](clickhouse教程-01-认识ClickHouse.md) | 是什么、为什么快、生态全景、2026 年现状、什么时候不该用 |
| 02 | [快速上手](clickhouse教程-02-快速上手.md) | 跑起来的三种方式、第一个 MergeTree 表、五个心态转变 |
| 03 | [数据类型体系](clickhouse教程-03-数据类型体系.md) | 整数回绕、`Decimal`、`String` 不校验编码、`Nullable` 与 `LowCardinality` 的隐藏成本、时区与夏令时、半结构化(`JSON` / `Variant` / `Dynamic`)与摄入侧写法 |
| 04 | [表引擎总览与选择](clickhouse教程-04-表引擎总览与选择.md) | 五个引擎族的取舍、按工作负载选引擎 |
| 05 | [MergeTree 存储结构](clickhouse教程-05-MergeTree存储结构.md) | **part / granule / mark / 稀疏主键索引的物理布局** |
| 06 | [表结构与 DDL](clickhouse教程-06-表结构与DDL.md) | 排序键是最重要的一个决定、三种默认值语义 |
| 07 | [SQL 语法全貌:SELECT](clickhouse教程-07-SQL语法全貌：SELECT.md) | 子句求值顺序、别名作用域、`WITH` 的三种形态(含递归 CTE) |
| 08 | [数据写入与导入](clickhouse教程-08-数据写入与导入.md) | 写入五步、临时 part 与原子改名、part 太多的根因 |
| 09 | [DDL、ALTER 与运维语句](clickhouse教程-09-DDL、ALTER与运维语句.md) | 元数据操作 vs 数据重写、mutation 为什么慢 |
| 10 | [查询执行引擎](clickhouse教程-10-查询执行引擎.md) | **推送模型、processor 四态、两阶段聚合、并行来自哪里**、泳道之下的 IO 层(并发读门槛 / 预取 / 异步读) |
| 11 | [索引与数据跳过](clickhouse教程-11-索引与数据跳过.md) | 稀疏主键、五种跳数索引、投影 |
| 12 | [聚合与 GROUP BY](clickhouse教程-12-聚合与GROUP-BY.md) | **聚合状态、组合器、`uniq` 是近似的**、位图(去重之外还要做集合运算时) |
| 13 | [物化视图与增量聚合](clickhouse教程-13-物化视图与增量聚合.md) | 插入触发器、`-State`/`-Merge`、MV 链、可刷新视图、坑清单 |
| 14 | [数据建模与表设计](clickhouse教程-14-数据建模与表设计.md) | **排序键怎么选、分区为了什么、分层建模与模型演进** |
| 15 | [JOIN 全解](clickhouse教程-15-JOIN全解.md) | 方向 × 严格度、ASOF、`join_use_nulls` 的坑、用字典替代 |
| 16 | [排序、窗口函数与 LIMIT BY](clickhouse教程-16-排序、窗口函数与LIMIT-BY.md) | NULL 的位置、`ROWS` vs `RANGE`、`lagInFrame` |
| 17 | [函数库全景](clickhouse教程-17-函数库全景.md) | 按族索引 + 每族最常踩的坑,末尾附**从 MySQL / PostgreSQL 迁过来时同名不等价的那批函数** |
| 18 | [分区、合并、Mutation 与 TTL](clickhouse教程-18-分区、合并、Mutation与TTL.md) | 后台合并、part 闸门、TTL 与冷热分层 |
| 19 | [数据格式与序列化](clickhouse教程-19-数据格式与序列化.md) | 可往返的判据、CSV 为什么不行、Native 不跨版本 |
| 20 | [压缩与编码](clickhouse教程-20-压缩与编码.md) | Delta / DoubleDelta / Gorilla / T64 / FPC、怎么选 |
| 21 | [字典](clickhouse教程-21-字典.md) | LAYOUT 的取舍、惰性刷新、加载失败是静默的 |
| 22 | [复制与 Keeper](clickhouse教程-22-复制与Keeper.md) | 复制日志、插入去重、quorum、leader 选举 |
| 23 | [分片与分布式查询](clickhouse教程-23-分片与分布式查询.md) | `Distributed` 的写读路径、`GLOBAL JOIN` 为什么必需 |
| 24 | [查询优化与执行计划](clickhouse教程-24-查询优化与执行计划.md) | 六种 `EXPLAIN` 的分工、按症状诊断慢查询 |
| 25 | [并发控制、资源管理与配额](clickhouse教程-25-并发控制、资源管理与配额.md) | 内存、溢出、四类限制、配额、**内存是怎么被追踪的**(RSS 与 `MemoryTracking` 为什么对不上) |
| 26 | [权限与安全](clickhouse教程-26-权限与安全.md) | 五种对象、行策略、加固清单 |
| 27 | [可观测性与 system 表](clickhouse教程-27-可观测性与system表.md) | 每张表回答什么问题、按症状查 |
| 28 | [性能调优与反模式](clickhouse教程-28-性能调优与反模式.md) | 十条反模式、调优顺序、验证方法学、**把性能结论固化成回归测试** |
| 29 | [生产部署与运维](clickhouse教程-29-生产部署与运维.md) | 容量规划、备份恢复、升级、事故清单、**启动与崩溃恢复**、对象存储的本地缓存(filesystem cache) |
| 30 | [集成与生态](clickhouse教程-30-集成与生态.md) | 数据怎么进来、怎么出去、怎么被消费(含 Arrow Flight/ADBC、四种湖表格式、原生协议与驱动选型) |
| 31 | [从零实现简化版 ClickHouse](clickhouse教程-31-从零实现简化版ClickHouse.md) | mini-clickhouse 的十个关键设计,以及**读真实 C++ 源码的入口顺序** |
| 32 | [ClickHouse 现状与未来方向](clickhouse教程-32-ClickHouse现状与未来方向.md) | 用三节把事实 / 路线 / 判断分开写,另有竞争格局与批评两节 |
| 33 | [附录 A:SQL 与函数速查](clickhouse教程-33-附录A-SQL与函数速查.md) | 语句、函数、设置、类型、格式、引擎速查表 |
| 34 | [附录 B:mini-clickhouse 工程手册](clickhouse教程-34-附录B-mini-clickhouse工程手册.md) | 目录结构、能力矩阵、API 参考、扩展指南、练习 |
| 35 | [附录 C:术语表](clickhouse教程-35-附录C-术语表.md) | 约 150 条术语 + 最容易混的十组概念 |
| 36 | [附录 D:FAQ 与排错](clickhouse教程-36-附录D-FAQ与排错.md) | 按症状组织的 72 个问答 |
| 37 | [附录 E:MySQL 与 PostgreSQL 迁移 playbook](clickhouse教程-37-附录E-MySQL与PostgreSQL迁移playbook.md) | 按迁移时间线串起来的可执行流程:盘点 → 类型映射 → 重写 schema → 搬数据 → 改查询 → 验证 → 切换与回滚 |
| 38 | [附录 F:端到端实战](clickhouse教程-38-附录F-端到端实战-可观测性平台.md) | 用 ClickHouse 搭日志/指标/追踪平台,**含全书唯一的结语** |

**如果只读三章**:第 05 章(存储结构)、第 12 章(聚合)、第 28 章(反模式)。

**按问题找章节**:

| 你的问题 | 去哪 |
|---|---|
| 「为什么这条查询这么慢」 | 第 24 章(读 `EXPLAIN`)、第 28 章(反模式)、第 10 章 §10.11(IO 层)、附录 D |
| 「为什么 `WHERE` 没走索引」 | 第 11 章 §11.2(排序键前缀)、第 05 章 §05.6 |
| 「`GROUP BY` 把内存打爆了」 | 第 12 章 §12.4、第 25 章 §25.2 |
| 「`JOIN` 结果比预期多 / 少」 | 第 15 章 §15.2(严格度)、§15.6(`join_use_nulls`) |
| 「`UPDATE`/`DELETE` 没生效」 | 第 09 章 §09.3、第 18 章 §18.8(mutation 是异步的) |
| 「`TOO_MANY_PARTS` 报错」 | 第 08 章 §08.4、第 18 章 §18.6 |
| 「磁盘涨得比数据快」 | 第 20 章(CODEC)、第 18 章 §18.10(TTL)与 §18.13(冷热分层) |
| 「数据放上 S3 之后就查不动了」 | 第 29 章 §29.14(对象存储的本地缓存)、§29.13(多盘与分层存储) |
| 「副本延迟一直涨」 | 第 22 章 §22.8、第 27 章 §27.10 |
| 「怎么把 Kafka 接进来」 | 第 30 章 §30.2(必须配物化视图) |
| 「维度补全用 `JOIN` 还是字典」 | 第 21 章 §21.2、第 15 章 §15.11 |
| 「物化视图为什么不回填历史数据」 | 第 13 章 §13.1、§13.7 |
| 「汇总表该存结果还是存状态」 | 第 13 章 §13.4、第 12 章 §12.10 |
| 「这张表的排序键该怎么选」 | 第 14 章 §14.3、第 06 章 §06.3 |
| 「要不要为报表提前预聚合」 | 第 14 章 §14.7、§14.8、第 13 章 |
| 「怎么把现有报表迁过来」 | 第 02 章 §02.8、第 07 章(子句语义差异)、第 17 章 §17.22(同名不等价的函数) |
| 「整个库怎么从 MySQL / PostgreSQL 搬过来」 | **第 37 章(附录 E:MySQL 与 PostgreSQL 迁移 playbook)**(按顺序串起来的迁移流程);搬完要搭什么见第 38 章(附录 F) |
| 「某个函数/设置在 25.3 里的默认值是多少」 | 附录 A;⚠️ 查不到就写"未确认",别猜 |
| 「想读源码但不知道从哪进」 | 第 31 章(配套实现的十个关键设计;§31.15 另给了**真实 C++ 源码**的入口顺序)、第 34 章 |

---

## 配套实现:mini-clickhouse

一个用纯 Python 写的列式 OLAP 引擎,把 ClickHouse 的**核心机制**实现了一遍:

```python
import minich

ctx = minich.Context(data_path="./data")     # 有磁盘;省略则纯内存
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
    SELECT url, count() AS c, round(avg(v), 2) AS avg_v
    FROM hits GROUP BY url ORDER BY c DESC LIMIT 5
""").text())

print(ctx.execute("SELECT name, rows, bytes_on_disk FROM system.parts WHERE active").rows())
ctx.shutdown()
```

**实现了什么**:完整的类型系统(56 个类型族)与列实现、二进制序列化、
10 种压缩编码实现(9 个能在 `CODEC(...)` 里直接写名:`NONE`/`LZ4`/`LZ4HC`/`ZSTD`/`Delta`/`DoubleDelta`/`Gorilla`/`T64`/`FPC`,外加内部用来装链的 `Multiple`;其中 Delta/DoubleDelta/Gorilla/T64/FPC 是真实算法)、
24 种数据格式、658 个标量函数、65 个聚合函数(含组合器与真实的 HyperLogLog)、
完整的 SQL 解析器(词法 + 递归下降 + 回溯)、推送模型的执行引擎(20 多种 processor)、
哈希连接与窗口函数、23 个表引擎、MergeTree 的 part/granule/mark/稀疏主键索引/
五种跳数索引/k 路归并/引擎家族行折叠/mutation/TTL、
**物化视图(插入触发器:`TO` 目标表、`POPULATE` 回填、源表 INSERT 触发 push)**、
43 张 `system` 表、
外部字典(多种 LAYOUT)、Keeper(znode 树 + 会话 + 监听)与三副本复制协调、
访问控制、以及一个简化协议的客户端/服务端与 CLI。

**明确不做**:真正的多线程流水线、代价模型、`Distributed` 分片、对象存储、
投影、窗口帧的 `RANGE` 语义(被当成 `ROWS`)、与官方库字节兼容的 LZ4/ZSTD、
网络认证、`OPTIMIZE` 之外的后台调度器。完整清单见
[`mini-clickhouse/README.md`](mini-clickhouse/README.md) 的能力矩阵。

### 快速开始

```bash
cd mini-clickhouse
python -m pytest tests/                        # 全部测试
for f in examples/*.py; do python "$f"; done    # 全部示例
python -m minich demo                           # 一个自包含的演示
python -m minich local -q "SELECT count() FROM numbers(1000000)"
python -m minich client                         # 交互式 REPL
cd .. && python mini-clickhouse/tools/check_docs.py   # 文档结构自检
```

**只需要 Python ≥ 3.10 与 NumPy**;不需要 GPU、不需要装 ClickHouse、
不需要联网。

---

## ✅ 验证状态

*(这一节由仓库里的命令产出,不是声明。运行上面"快速开始"里的命令即可复现;
"一次完整的验证"那三条同样是可复现的。)*

| 项 | 结果 |
|---|---|
| 测试 | `python -m pytest tests/` —— `2813 passed, 83 skipped, 49 xfailed` |
| 示例 | `for f in examples/*.py; do python "$f"; done` —— `14/14 exit 0` |
| 文档结构自检 | `python mini-clickhouse/tools/check_docs.py` —— 20 项检查全通过 |
| 依赖 | Python ≥ 3.10 + NumPy;无其它第三方依赖 |

**文档结构自检**检查的就是这份 README 与 39 篇正文里那些**容易被改动弄坏**的
一致性:章号连续、代码围栏闭合、跨章引用不悬空、表格列数一致、
**自述数字唯一**("测试数"在整个仓库里只能有一个值)、
**自指计数**("后面还有 N 章"必须等于真实章号差)、
**结语只出现在一章**,
以及五组"规模数字必须等于当场数出来的值"——注册了多少个函数/引擎/系统表、
`minich/` 有多少行代码(逐包、逐文件)、`tests/` 有多少个测试文件与测试函数、
测试展开后到底收集到多少条、以及检查脚本自己有多少行。
最后一类针对的是一类很特别的错误:它们**由"项目在长大"这个动作本身弄坏**。
你没有改任何错的东西,只是加了一个函数、多写了一个测试,旁边所有引用规模
数字的句子就全部过期了 —— 这也是这份教程里唯一一类**必须**由脚本拦下的错误,
因为人眼在"我只是加了个函数"的时候不会去重读二十处数字。
这一轮就抓到了它的一个真实案例:B.5.1 的测试矩阵停在 23 个文件 / 891 个函数上,
而磁盘上已经是 25 个文件 / 923 个 —— 连 README 里那句"测试通过数"也一起过期了。
**文档自己宣称"数字是数出来的、不是声明",就得让机器每次真的数一遍。**
二十项检查里只有一项要调用外部命令(第 20 项跑一次
`pytest --collect-only`,约 1 秒),原因见附录 B 的 B.5.3。

**关于事实的诚实说明**:`mini-clickhouse` 在本机**完整验证**;
而教程里对**真实 ClickHouse** 的行为描述基于官方文档与对 25.3 的核对,
**本机没有安装 ClickHouse**,因此那些示例属"文档级准确"而非"本机跑过"。
正文中凡是不能确认的,都写了"未确认"。

---

## 关于事实与时效性

- 写作时间:**2026 年 10 月**;对标版本 **ClickHouse 25.3**;Python ≥ 3.10。
- 标注规则:无标注 = 官方文档级事实;`(25.x 起)` 这类 = 有版本依据;
  **未确认** = 检索不到可靠来源、或来源之间冲突。
- **版本节奏(已核实)**:ClickHouse 每年发两条 LTS,分别在 **3 月**与 **8 月**
  (版本号形如 `YY.3` / `YY.8`),同时支持两条、每条至少 **12 个月**;
  此外每月还有 stable 版,但只有**最近三个** stable 会拿到补丁。
  `25.3` 发布于 2025-03-20,官方支持到 2026-03-20 —— **也就是说本教程对标的
  这个版本已经过了上游支持窗口**,把它当"长期生产基线"之前请先确认升级路径。
- **查默认值的三个自查入口**,按可靠性排序:`system.settings`(会话/查询设置)、
  `system.merge_tree_settings`(表级设置)、以及 **`system.settings_changes`**
  —— 最后这张官方表专门记录"某个设置的默认值在哪个版本变过"(列为
  `type` / `version` / `changes`),是判断"这条默认值在我这一版到底是什么"最可靠的
  手段。注意它的两个局限:只覆盖 `compatibility` 机制引入之后的改动,且
  **不覆盖服务端配置**(`config.xml` 那一类) —— 本教程里仍标"未确认"的条目,
  大多正是掉在这两条缝里。
- 第 32 章用「确定的事实 / 官方路线与在研方向 / 作者的判断」三个层次
  把可信度分开写,并另立版本与升级策略、Cloud 与 OSS 的功能分界、
  半结构化与 AI 相邻能力、检索能力的补齐、湖仓与开放表格式、
  部署形态的扩散、怎么跟踪演化、选型决策清单、竞争格局与批评争议等节;
  **不要把判断当事实引用**。
- ClickHouse 迭代很快(每月一个 release)。**本教程的任何具体默认值,
  都以你所用版本的 `system.settings` / `system.merge_tree_settings` 为准。**

---

## 目录结构

```
clickhouse-tutorial/
├── README.md                      # 本文件
├── clickhouse教程-00..38-*.md      # 39 篇正文(含 6 个附录 A–F)
└── mini-clickhouse/               # 配套实现
    ├── minich/                    # 引擎源码
    ├── tests/                     # 测试
    ├── examples/                  # 可运行示例
    ├── tools/check_docs.py        # ⭐ 文档结构自检(20 项,带退出码)
    ├── README.md                  # 工程说明与能力矩阵
    ├── conftest.py
    └── pyproject.toml
```

**一次完整的验证 = 三条命令**:

```bash
cd mini-clickhouse
python -m pytest tests/                        # ① 全部测试通过
for f in examples/*.py; do python "$f"; done    # ② 全部示例 exit=0
cd .. && python mini-clickhouse/tools/check_docs.py   # ③ 20 项结构检查通过
```
