# 第1章 流计算与 Flink 全景

## 1.1 流处理的第一性原理

批处理把数据看成一个**已经结束的集合**：数据到齐、可以排序、可以重跑、可以等。流处理把数据看成一条**仍在生长的序列**：没有终点，记录按到达顺序出现，迟到、重复、乱序和失败都是常态。

这个差别带来的不是“延迟从小时降到秒”，而是五个必须正面回答的问题：

1. **完整性**：什么时候可以认为一段时间的数据“到齐了”？
2. **正确性**：乱序、迟到、重复到达时，结果以谁为准？
3. **一致性**：作业失败后，状态和外部系统如何回到同一个逻辑时刻？
4. **成本**：为了低延迟和正确性，要付出多少内存、磁盘、网络和人力？
5. **可运维性**：如何知道它算得对、跑得动、扩得开、停得下？

批处理用“等数据到齐”回避了第 1、2 题，用“整批重算”回避了第 3 题；流处理没有这些退路，所以必须把时间、状态和容错做成运行时的一等公民。Flink 的核心价值，正是把这三件事工程化。

可以用一个不等式概括流式系统的设计空间：

```text
结果确定性  ≤  f(等待时间, 状态保留时间, 重放能力)
```

等待越久、状态留得越久、源端越可重放，结果越确定；反过来，延迟越低、状态越省，就越依赖近似和补偿。所有“实时且绝对准确”的宣传，最终都要落回这个不等式。

## 1.2 有界与无界：同一套语义的两种边界

Flink 把输入分成两类：

- **有界流（bounded）**：输入有明确的结束点，例如一个 Hive 分区、一批 S3 文件；
- **无界流（unbounded）**：输入持续产生，例如 Kafka、CDC、点击流、传感器。

关键点是：**有界流可以看作无界流在一个时刻结束的特例**。因此 Flink 用同一套算子、同一套 SQL、同一套状态和 checkpoint 机制处理两者，只在执行模式上做优化：批模式可以选择阻塞 shuffle、排序聚合、更激进的调度；流模式则必须处理 watermark、乱序和持续状态。

这也解释了“流批一体”的真正含义：不是把批当流硬跑，而是**同一份逻辑计划，根据输入边界选择不同的物理执行策略**。第 18 章会展开这一点。

## 1.3 Flink 的分层能力地图

从下往上，Flink 可以分成五层：

```text
┌──────────────────────────────────────────────────────────────┐
│ 应用层   DataStream API │ Table API/SQL │ CEP │ Agents/ML    │
├──────────────────────────────────────────────────────────────┤
│ 逻辑层   动态表 / 算子 DAG / 窗口 / Join / 状态描述           │
├──────────────────────────────────────────────────────────────┤
│ 运行时   JobManager / TaskManager / Slot / 网络栈 / 调度      │
├──────────────────────────────────────────────────────────────┤
│ 状态层   Keyed State / Operator State / StateBackend / 快照   │
├──────────────────────────────────────────────────────────────┤
│ 部署层   Standalone / YARN / Kubernetes / Operator / 云服务   │
└──────────────────────────────────────────────────────────────┘
```

- **DataStream API**：最底层也最灵活，适合需要精细控制时间、状态、定时器和网络行为的场景。
- **Table API / SQL**：面向关系语义，用动态表（dynamic table）描述流上的持续查询，适合实时数仓、ETL 和分析。
- **CEP**：复杂事件处理，用模式匹配表达“事件序列”，适合风控、设备告警、用户行为。
- **Stateful Functions / Agents**：把有状态实体或 AI Agent 作为一等公民，强调事件驱动的长期记忆和外部调用。需要提醒的是，StateFun 子项目在 3.x 之后基本停更，2026 年社区已通过 FLIP-569 推进归档；新项目应优先评估 Flink Agents、PTF 或普通的有状态算子，而不是把 StateFun 当作新系统的基础。
- **运行时与状态层**：上述 API 最终都编译成算子图，共享同一套调度、网络、状态和容错机制。

学习 Flink 最容易犯的错误，是只学最上层 API，遇到“窗口不触发”“checkpoint 超时”“恢复后重复写”时没有下层模型可依靠。本书的结构刻意从运行时往下扎，再回到 API。

## 1.4 典型场景与反模式

### 适合 Flink 的场景

| 场景 | 关键需求 | 典型链路 |
|---|---|---|
| 实时数仓 | 事件时间、乱序、 exactly-once 入湖 | CDC → Flink → Paimon/Hudi/Iceberg → OLAP |
| 实时风控 | 低延迟、复杂序列、状态查询 | 交易流 + 行为流 → CEP/规则 → 拦截 |
| 实时特征 | 窗口聚合、点查、低延迟 | 埋点 → Flink → Redis/Feature Store |
| 实时告警 | 多流 join、去重、抑制 | 指标/日志 → Flink → 告警平台 |
| 数据同步 | CDC、schema 演进、幂等 | Binlog → Flink CDC → 数仓/湖 |
| 事件驱动 | 状态机、定时器、外部调用 | 订单事件 → ProcessFunction → 下游服务 |

### 不应该强行使用 Flink 的场景

- **OLTP 点查**：Flink 不是数据库，不能替代 Redis/MySQL 的低延迟读写；
- **长期消息存储**：Kafka/Pulsar 才是消息总线，Flink 是有算力无存储的引擎；
- **超低延迟硬实时**（微秒级）：JVM、网络栈和 checkpoint 都会带来毫秒级开销，应考虑专用流引擎或边缘计算；
- **一个巨大作业承载所有业务**：升级、扩缩容和故障的爆炸半径会失控，应按领域拆成多个作业；
- **没有 watermark 语义却要求“绝对正确”**：无界流上不存在不等待的绝对完整，只能定义可接受的迟到边界。

理解边界比记住 API 更重要。一个架构师的价值，往往体现在“知道什么不该用 Flink”。

## 1.5 一个作业的完整生命周期

下面这条链路会贯穿全书。假设我们要做“订单实时风控”：

```text
1. 编写        DataStream/SQL 描述 source → 转换 → sink
2. 编译        StreamGraph → JobGraph → ExecutionGraph
3. 提交        Client 把 JobGraph 交给 JobManager
4. 调度        Scheduler 分配 Slot，部署 subtask
5. 运行        Source 读数据，算子处理，watermark 推进
6. 快照        周期性 checkpoint，状态写到持久存储
7. 故障        TaskManager/网络/算子异常，JobManager 检测
8. 恢复        从最近成功 checkpoint 重建状态，source 回拨 offset
9. 升级        savepoint → 改逻辑/并行度 → 从 savepoint 恢复
10. 下线       stop-with-savepoint / cancel，清理资源
```

每一步都可能出错，也都对应一组配置和指标：

- 编译期：算子链、并行度、序列化器、状态描述；
- 调度期：Slot 数量、资源规格、亲和性；
- 运行期：watermark、反压、GC、状态大小；
- 快照期：checkpoint 间隔、超时、存储带宽；
- 恢复期：重启策略、本地恢复、状态兼容性；
- 升级期：UID、savepoint 兼容、状态迁移。

本书后面每一章，基本都在拆解这条链路中的一段。

## 1.6 版本演进：从 1.x 到 2.x

Flink 1.x 的主线是“真正的流式运行时 + 事件时间 + 有状态算子 + 分布式快照”。它把流处理从微批中解放出来，并在 1.13～1.20 期间完善了 DataStream API、Table/SQL、Kafka exactly-once、Kubernetes Operator 和细粒度资源管理。

Flink 2.x 则围绕“云原生、流批一体、湖仓、Data + AI”继续演进。几个值得记住的节点：

- **Flink 1.20（2024-08）**：物化表（FLIP-435）进入可用阶段，用 `FRESHNESS` 把“持续刷新/全量刷新”统一成一条 DDL。
- **Flink 2.0（2025-03）**：默认 Java 17（最低仍支持 Java 11）；移除大量 1.x 废弃 API（`SourceFunction`、`SinkFunction` V1、DataSet、Scala API、per-job 部署模式、`run-application` 子命令等）；引入**分离式状态管理**（disaggregated state，ForSt）和异步状态访问；DataStream API V2 与 `QUALIFY` 进入可用/实验阶段。
- **Flink 2.1（2025-07-31）**：面向 Data + AI，加入 `ML_PREDICT`、Process Table Functions（PTF）、`VARIANT`/`PARSE_JSON`、Delta Join、StreamingMultiJoin 等。
- **Flink 2.2（2025-12-04）**：增强物化表（`FRESHNESS` 可选、`DISTRIBUTED BY`）、`VECTOR_SEARCH`、SinkUpsertMaterializer V2、Delta Join 与连接器框架；RocksDB 版本变化可能影响状态二进制。Kryo 在 2.0 已从 2.24 升级到 5.x（FLIP-317），2.2 沿用，1.x 状态升级前要专门验证。
- **Flink 2.3（2026-06-25）**：`FROM_CHANGELOG`/`TO_CHANGELOG`、物化表 DDL 完整化、`ON CONFLICT`、原生 S3 文件系统（实验）、watermark alignment 修复、应用级生命周期管理。`START_MODE` 由 FLIP-557 推进，社区目标 2.4，2.3 开源语法中尚不包含（厂商方言已有）。

需要特别强调两点：

1. **1.x 与 2.x 的状态不保证兼容**。跨大版本恢复通常需要 State Processor API 翻译、从源端重放，或在 1.20 上先做 savepoint 迁移。升级前必须在副本上做恢复演练。
2. **ForSt 仍是实验特性**。截至 2.3，生产大状态仍以 RocksDB 为主；ForSt 不支持 canonical savepoint、全量快照、changelog 和文件合并 checkpoint。不要因为“2.0 新特性”就贸然替换生产状态后端。

本书正文以 **Flink 2.3 的 API 与运行模型**为基线，同时保留 1.x 中仍然广泛使用的概念，并在涉及版本差异时明确标注。2.x 的架构变化、迁移路线和实验特性边界，见第 34 章；实时 AI 见第 35 章。

## 1.7 一个最小作业，两种写法

Java DataStream 的典型写法（2.x 推荐使用 Source/Sink V2）：

```java
StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
env.enableCheckpointing(10_000);
env.setParallelism(4);

DataStream<Order> orders = env
    .fromSource(
        KafkaSource.<Order>builder()
            .setBootstrapServers("kafka:9092")
            .setTopics("orders")
            .setGroupId("risk")
            .setStartingOffsets(OffsetsInitializer.committedOffsets())
            .setValueOnlyDeserializer(new OrderDeserializer())
            .build(),
        WatermarkStrategy
            .<Order>forBoundedOutOfOrderness(Duration.ofSeconds(5))
            .withTimestampAssigner((order, ts) -> order.eventTime())
            .withIdleness(Duration.ofSeconds(30)),
        "orders");

orders
    .keyBy(Order::getUserId)
    .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
    .allowedLateness(Duration.ofMinutes(1))
    .aggregate(new OrderAmountAggregate())
    .sinkTo(kafkaSink);

env.execute("order-risk");
```

同样的逻辑用 SQL 表达：

```sql
CREATE TABLE orders (
  user_id STRING,
  order_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH ('connector' = 'kafka', ...);

SELECT
  user_id,
  window_start,
  window_end,
  SUM(amount) AS total_amount
FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES))
GROUP BY user_id, window_start, window_end;
```

把它翻译成 MiniFlink（本书第 28 章之后会逐层拆开）：

```python
from miniflink import StreamExecutionEnvironment, TumblingEventTimeWindows, Duration

env = StreamExecutionEnvironment(parallelism=2)
env.enable_checkpointing(10_000)

(
    env.from_collection(orders, watermark_strategy=strategy)
    .key_by(lambda order: order["user_id"])
    .window(TumblingEventTimeWindows.of(Duration.of_minutes(5)))
    .allowed_lateness(Duration.of_minutes(1))
    .aggregate(SumAggregate("amount"))
    .print()
)
env.execute("order-risk")
```

三种写法共享同一组概念：source、watermark、keyBy、窗口、聚合、sink、checkpoint。API 只是表皮，运行时语义才是骨架。

## 1.8 本书的结构与阅读路线

全书分八部分：

1. **建立全景**：运行时、DataStream、时间语义；
2. **窗口、状态与容错**：Flink 最难也最有价值的部分；
3. **数据集成**：连接器、Kafka exactly-once、维表、CDC 与湖仓；
4. **Table API 与 SQL**：动态表、窗口 TVF、优化器、UDF、流批一体；
5. **高级特性与调优**：双流 join、CEP、内存网络、监控、安全；
6. **工程与运维**：测试、Kubernetes、容量成本、故障排查；
7. **MiniFlink 实现**：从零构建一个可运行的教学引擎；
8. **前沿、实战与商业**：2.x 深潜、实时 AI、正确性工程、生态选型、端到端案例、市场与 ROI。

其中第八部分可以拆成两条线：

- **前沿线**：第 34 章（DataStream V2/ForSt/物化表）、第 35 章（实时 Data + AI）、第 36 章（正确性工程）、第 37 章（生态选型与未来）；
- **实战/商业线**：第 38 章（端到端风控平台）、第 39～41 章（商业分析）。

正文之外有 16 个附录：A～H 是 API、配置、MiniFlink 边界、术语、面试、参考、SQL 速查和源码地图；I 是 PyFlink 工程手册；J 是性能工程与压测手册；K 是 Table 连接器与 Catalog 开发手册；L 是状态迁移、Savepoint 兼容与 State Processor API 手册；M 是 REST API 与作业运维自动化手册；N 是实时数仓建模手册；O 是 Flink SQL 生产模式库；P 是作业交付与变更管理手册。

六条主线可以独立阅读：

- **时间线**：第 4、5、6、15、19、20 章；
- **状态线**：第 7、8、9、12、19、21 章；
- **工程线**：第 10、11、13、22～27 章；
- **实现线**：第 28～33 章；
- **前沿线**：第 34～37 章；
- **性能线**：第 21、26、27 章 + 附录 J。

MiniFlink 的实现线是第 28～33 章，建议在读完第 9 章后再看，否则容易陷入“实现细节很多，但不知道生产语义是什么”。

## 1.9 MiniFlink 的位置

MiniFlink 是一个只依赖 Python 标准库的单进程教学实现。它不复刻 Flink 的分布式运行时，而是把以下机制压缩到可读、可运行的代码里：

- DataStream/KeyedStream/WindowedStream 的链式 API；
- 事件时间、watermark、窗口分配、触发、迟到与侧输出；
- 键控状态、TTL、事件/处理时间定时器；
- 微批一致快照、source offset 恢复；
- 内存 Kafka、文件、SQLite 连接器；
- Table API/SQL 子集、CEP、广播状态、指标与 CLI。

它的价值不是“替代 Flink”，而是把抽象变成可调试的对象：当生产 Flink 的窗口不触发时，你可以在 MiniFlink 里打断点，看 watermark 如何被多输入取最小值、窗口状态如何被清理。附录 C 会明确列出它与生产 Flink 的差异。

## 1.10 动手环境与实验约定

本书有两种代码，阅读时要分清：

- **Java/SQL 片段**：描述 Flink 2.3 的生产 API 与语义，用于理解和落地，不要求逐字编译；
- **MiniFlink（Python）**：仓库内可运行的实现，测试和示例会实际执行。

推荐环境：

```text
JDK 17（Flink 2.x 默认；最低 11，具体以发行版为准）
Apache Flink 2.3（本地集群/容器）
Python 3.10+（MiniFlink 在 3.14 上验证；PyFlink 的支持范围更窄，见附录 I）
pytest 9.x
```

MiniFlink 不依赖 Java、Kafka 或 Flink 发行版，适合先建立语义直觉：

```bash
cd flink-tutorial
python -m pip install -r requirements.txt

python examples/01_word_count.py
python -m pytest -q
python check_docs.py
```

生产语义的实验则需要真实组件。最小组合是：

```text
Kafka（或 Redpanda） + Flink 本地/standalone 集群 + 对象存储（本地可用 MinIO）
```

如果暂时没有集群，也可以只读代码、跑 MiniFlink，并把 Java 片段当作“生产对照”。**不要因为本地没有 Kafka，就跳过第 10～13 章的状态与一致性设计**；这些章节的结论不依赖具体集群。

三个实验约定：

1. **先固定版本**：Flink、连接器、JDK、Python 的版本写进实验记录，否则结论不可复现；
2. **先跑反例**：迟到、乱序、重复、空输入、故障恢复，比正常路径更能暴露语义问题；
3. **区分语义验证与性能验证**：MiniFlink 能验证前者，不能验证后者（附录 J）。

## 本章小结

流处理的核心不是“逐条处理”这个动作，而是**在无界、乱序、会失败的环境里，持续维护正确状态**。Flink 用事件时间、watermark、有状态算子和分布式快照把这套问题工程化；2.x 则在此基础上走向分离式状态、流批一体和 Data + AI。

进入细节之前，记住三句话：

> 流式系统的正确性，最终取决于“什么时候认为数据到齐了”和“失败后状态从哪里恢复”。
>
> 延迟、正确性和成本构成一个三角，任何设计都在其中取舍。
>
> API 会变，运行时语义不会。先建立运行时地图，再学 API。

下一章从 JobManager、TaskManager、Slot 和作业图开始，建立 Flink 的运行时地图。
