# 第34章 Flink 2.x 深潜：DataStream API V2、ForSt 与流批统一

前置知识：第 4、7、8 章（时间、状态、checkpoint）、第 14 章（Table/SQL）、第 18 章（流批一体）。本章可以独立阅读，但涉及运行时语义时会回引这些章节。

前 33 章讲的是“Flink 怎么工作”。这一章换一个视角：**Flink 2.x 为什么要这样改，哪些改动会影响你的架构，哪些还只是实验**。

Flink 2.0 不是一次普通的版本升级。它同时动了四件事：用户 API、状态存储、SQL 抽象和运行时调度。很多团队在 1.x 上积累的 savepoint、连接器、调优参数和运维脚本，到了 2.x 需要重新验证。把 2.x 当成“1.20 加几个 SQL 函数”会踩坑；把它当成“换了一个引擎”又过度恐慌。正确的姿势是：**分清稳定语义、实验特性和已移除 API，按依赖顺序迁移**。

本章的前置阅读是第 1、2、3、7、8、14 章；如果你只关心迁移，可以直接跳到 34.8。

## 34.1 1.x 留下的四个结构性约束

Flink 1.x 非常成功，但它的成功也留下了结构性债务。2.x 的大部分设计，都能对应到下面四个约束之一。

### 约束一：API 的历史包袱

1.x 同时存在多套 Source/Sink 接口：

- `SourceFunction` / `SinkFunction`（V1）；
- FLIP-27 的 `Source`；
- Sink V1 / Sink V2；
- DataSet API 与 DataStream API 两套批处理抽象；
- Scala API 与 Java API 两套类型系统。

这些接口的生命周期、并行度、checkpoint 语义并不完全一致。连接器作者要为多套接口维护适配层，用户在不同 API 之间迁移时也会遇到“同样的逻辑，状态却不兼容”的问题。

Flink 2.0 的选择是**做减法**：移除 `SourceFunction`、`SinkFunction` V1、DataSet API、Scala API 等一批废弃接口，把 Source/Sink 统一到 FLIP-27 与 Sink V2。代价是升级时必须改代码；收益是运行时和连接器生态可以围绕一套契约收敛。

### 约束二：状态与计算耦合

1.x 的主流状态后端是 HashMap 和 RocksDB：

- HashMap 把状态放在 JVM 堆里，状态大时 GC 和 checkpoint 都会痛；
- RocksDB 把状态放在本地磁盘，依赖本地 SSD，compaction 会争抢 IO，扩缩容要下载/重分配大量文件。

这套模型假设“状态跟着计算走”。当状态达到几百 GB 甚至 TB、当云原生环境要求快速扩缩容时，本地盘就成了瓶颈。Flink 2.0 的**分离式状态管理**（disaggregated state）和 **ForSt** 后端，就是把状态的主存储移到分布式文件系统，本地只做缓存。第 7、8 章讲过状态后端；34.4 会讲 ForSt 的边界。

### 约束三：流批两套栈

1.x 的 DataSet API 和 DataStream API 是两套算子、两套优化器、两套容错路径。Table/SQL 虽然统一了逻辑计划，但底层仍要针对有界/无界输入选择不同的物理执行策略。

2.x 的方向是：**一套 API、一套 SQL、根据输入边界选择执行模式**。DataSet API 被移除，批处理通过 DataStream 的 BATCH 执行模式或 SQL 的批模式完成。第 18 章讲过流批一体的运行时差异；本章补充 2.x 在调度和 SQL 抽象上的新东西。

### 约束四：Data + AI 的表达力

1.x 的 UDF 主要面向“一行进、一行出”或“一行进、多行出”的标量/表函数。它能调用模型，但很难表达：

- 有状态的、跨多行的事件处理；
- 同时读写 changelog；
- 在一个算子内注册定时器、维护会话；
- 多表输入、多表输出；
- 模型调用、向量检索、工具调用等 AI 原语。

2.1 引入的 **Process Table Function（PTF）**、`ML_PREDICT`、`VARIANT`，2.2 的 `VECTOR_SEARCH`，以及 Flink Agents 子项目，都是在补这块表达力。第 35 章会专门讲实时 AI。

## 34.2 2.x 版本路线：截至 2026 年 11 月

先给一张时间线。它不是“功能大全”，而是帮你判断某个特性属于“稳定可用”“实验”还是“还没来”。

| 版本 | 发布时间 | 主题 | 代表性变化 |
|---|---|---|---|
| 2.0.0 | 2025-03 | 新运行时基线 | Java 17 默认；移除 DataSet/Scala/SourceFunction/SinkFunction V1；`config.yaml`；ForSt 实验；DataStream API V2 实验；异步状态访问（物化表已在 1.20 引入，2.x 继续扩展） |
| 2.1.0 | 2025-07-31 | Data + AI | PTF；`ML_PREDICT` 与 Model DDL；`VARIANT`/`PARSE_JSON`；Delta Join；StreamingMultiJoin；异步 lookup join 增强；Sink 复用；Smile 计划序列化 |
| 2.2.0 | 2025-12-04 | 实时 AI 与湖仓 | `VECTOR_SEARCH`；`ML_PREDICT` Table API；物化表 `FRESHNESS` 可选；`SinkUpsertMaterializer V2`；Delta Join 增强；平衡调度；PyFlink 异步 |
| 2.3.0 | 2026-06-25 | 流批统一与运维 | `FROM_CHANGELOG`/`TO_CHANGELOG`；物化表 DDL 完整化；`ON CONFLICT`；原生 S3 文件系统（实验）；自适应分区选择；恢复期 checkpoint；应用级生命周期（`START_MODE` 见 FLIP-557，目标 2.4） |

版本基线要记三句话：

1. **Java 17 是默认，Java 11 是最低；Java 21 的支持口径在 2.3 仍有文档差异**（release notes 已称支持，兼容性页仍可能标 experimental，见 FLIP-623 的说明）。不要假设 1.x 的 Java 8 环境能直接升级，落地前以所用发行版的兼容性页为准。
2. **2.x 不保证与 1.x 的状态二进制兼容**。跨大版本恢复通常需要 State Processor API、从源端重放，或先在 1.20 做迁移演练。
3. **实验特性不要进核心链路**。ForSt、DataStream API V2、原生 S3、部分 AI 函数在 2.3 仍标注实验；生产选型要准备回退路径。

> 版本号和发布日期会继续变化。做架构决策时，以所用发行版的 release notes、FLIP 和兼容性矩阵为准，不要只看二手文章。

## 34.3 DataStream API V2：把“算子内部”藏起来

### 为什么需要 V2

经典 DataStream API 暴露了太多运行时细节：

- 用户可以 `disableChaining()`、`startNewChain()`、设置 slot sharing group；
- 窗口、join、CEP 等高层算子依赖内部的状态布局；
- source/sink 的并行度、事件时间、checkpoint 语义分散在多个接口；
- 用户代码可能直接依赖 mailbox、barrier 对齐等内部行为。

这些细节让调优很灵活，也让 API 很难演进。**DataStream API V2（FLIP-408 伞形 FLIP）** 的目标是重新划一条边界：用户只描述“怎么处理记录”，运行时保留“怎么调度、怎么对齐、怎么快照”的自由。

### 五个原语

V2 把 API 收敛到五个基础原语：

1. **DataStream**：有类型的流；
2. **Partitioning**：数据如何分区；
3. **ProcessFunction**：如何处理记录；
4. **State**：如何声明和访问状态；
5. **Watermark / Timer**：如何处理事件时间与定时器。

高层函数（map、flatMap、reduce、窗口）是建立在这些原语上的语法糖，而不是另一套运行时。

### 流类型与分区

V2 的流类型比 V1 更显式：

```text
GlobalStream             全局单分区
NonKeyedPartitionStream  非键控、可分区
KeyedPartitionStream     按键分区
BroadcastStream          广播到所有下游
```

分区方式也收敛为四种：

| 分区 | 语义 | 典型用途 |
|---|---|---|
| `keyBy` | 按 key 哈希 | 键控状态、聚合 |
| `shuffle` | 轮询/随机 | 打散、负载均衡 |
| `global` | 全部到一个分区 | 全局汇总、单例 sink |
| `broadcast` | 复制到所有分区 | 规则、维表 |

这比 V1 的 `rebalance/rescale/global/broadcast` 更接近“逻辑分区”而不是“物理策略”。物理上怎么连、要不要走网络，由运行时决定。

### ProcessFunction 是唯一的处理入口

V2 的原则是：**所有处理逻辑都通过 ProcessFunction 表达**。一个最小的 V2 作业长这样（示意，具体签名以所用版本文档为准）：

```java
ExecutionEnvironment env = ExecutionEnvironment.getExecutionEnvironment();

env.fromSource(kafkaSource, WatermarkStrategy.noWatermarks(), "orders")
   .process(new OneInputStreamProcessFunction<Order, Order>() {
       @Override
       public void processRecord(Order order, Collector<Order> out, Context ctx) {
           if (order.amount() > 0) {
               out.collect(order);
           }
       }
   })
   .keyBy(Order::userId)
   .process(new RiskProcessFunction())
   .toSink(sink);

env.execute("risk-v2");
```

注意几个变化：

- 环境类叫 `ExecutionEnvironment`，不是 `StreamExecutionEnvironment`；
- `processRecord(value, out, ctx)` 的参数顺序与 V1 的 `processElement(value, ctx, out)` 不同；
- source 只支持 FLIP-27 的 `Source`；
- 没有 `window()` 这类高层算子，窗口要用 ProcessFunction + 定时器/状态组合，或使用后续提供的高层扩展。

ProcessFunction 的生命周期是：

```text
open → processRecord* → endInput → close
```

双输入、广播、双输出分别有对应的 ProcessFunction 类型。这个分类比 V1 的 `CoProcessFunction`/`BroadcastProcessFunction` 更系统，也让运行时可以针对不同输入形状做优化。

### 状态声明：把“状态布局”变成契约

V1 通过 `ValueStateDescriptor` 在运行时取状态。V2 引入 **StateDeclaration**（FLIP-433），把状态声明前置：

```java
public class RiskProcessFunction
        extends KeyedProcessFunction<String, Order, Alert> {

    private static final ValueStateDeclaration<Long> COUNT =
        StateDeclarations.valueState("count", TypeDescriptors.LONG);

    @Override
    public Set<StateDeclaration<?>> usesStates() {
        return Set.of(COUNT);
    }

    @Override
    public void processRecord(
            Order order, Collector<Alert> out, PartitionedContext ctx) {
        ValueState<Long> count =
            ctx.getStateManager().getState(COUNT).orElseThrow();
        long next = (count.value() == null ? 0 : count.value()) + 1;
        count.update(next);
        if (next > 10) {
            out.collect(new Alert(order.userId(), "too-many-orders"));
        }
    }
}
```

状态声明还带有**重分配模式**：

| 模式 | 含义 | 适用 |
|---|---|---|
| `NONE` | 不允许重分配 | 与 subtask 强绑定 |
| `REDISTRIBUTABLE` | 可以按 key 重分配 | 大多数键控状态 |
| `IDENTICAL` | 每个 subtask 必须拿到相同状态 | 广播/全局配置 |

这解决了 V1 里“状态能不能 rescale”只靠文档和经验判断的问题：**状态的重分配能力成为类型的一部分**。

### 广义 watermark 与处理时间

V2 把 watermark 抽象成“广义 watermark”：

- 可以声明 watermark 的生成方式；
- 可以选择“窥视（PEEK）”或“消费（POLL）”输入 watermark；
- 空闲、对齐等策略由运行时统一处理；
- 处理时间定时器通过 `ProcessingTimeManager` 注册，不再和事件时间定时器混在一个接口里。

这比 V1 的 `WatermarkStrategy` + `TimerService` 更干净，但也意味着 V1 的很多调优手法在 V2 里要重新学。

### V1 与 V2 的取舍

| 维度 | V1 | V2 |
|---|---|---|
| 成熟度 | 生产主力 | 实验 |
| 高层算子 | 丰富（window/join/CEP） | 以 ProcessFunction 为主 |
| 状态声明 | 运行时 descriptor | 声明式 + 重分配模式 |
| 运行时细节 | 暴露较多 | 刻意隐藏 |
| 迁移成本 | — | 高，不建议重写生产作业 |
| 适用 | 现有生产、复杂算子 | 新项目预研、平台内部 |

**结论**：截至 2.3，V2 还不是生产默认选项。新项目可以关注，但不要把核心链路押在实验 API 上。真正值得现在做的是：**把用户逻辑从算子内部 API 中解耦**，这样未来迁移 V2 时只需要替换“壳”。

### MiniFlink 的映射

MiniFlink 的 `DataStream` 也是“转换图 + 末端构图”，和 V2 的“处理逻辑即图”在思想上接近。但它没有：

- 显式的流类型层次；
- StateDeclaration 与重分配模式；
- 广义 watermark；
- 真正的异步状态访问。

你可以在 MiniFlink 里做一个练习：把 `KeyedProcessFunction` 的状态访问改成“先声明、再通过 manager 获取”的形状，体会 V2 为什么要这样设计。

## 34.4 ForSt：把状态从本地盘搬到 DFS

### 问题：本地状态的三座大山

RocksDB 状态后端在 1.x 很成功，但它有三个绕不开的成本：

1. **本地盘绑定**：状态存在哪台机器，计算就得去哪台机器；扩缩容要搬状态。
2. **compaction 抖动**：写入放大、读放大、后台 compaction 会争抢 CPU/IO，和 checkpoint 互相影响。
3. **快照成本**：全量/增量 checkpoint 仍要扫描本地状态并上传；状态越大，快照越慢。

ForSt（“For Streaming”）的思路是：**把 DFS 作为状态的主存储，本地盘只做缓存**。状态写入先落本地缓存/日志，再异步持久化到 DFS；checkpoint 不再需要把全部状态重新上传，而是记录一个逻辑切面。

```text
计算 Task ──读写──> 本地缓存（内存/SSD）
                       │
                       │ 异步 flush / 日志
                       ▼
                  DFS（S3/HDFS/OSS）
                       │
                       └── checkpoint 记录逻辑切面
```

### 它带来的好处

- **快照更轻**：不需要每次上传全量状态；
- **恢复更快**：本地缓存命中时不必全量下载；
- **扩缩容更快**：状态与计算解耦，重分配不必搬大量文件；
- **更适合云原生**：计算节点可以无状态化，本地盘只做 cache。

官方在 2.0 的 Nexmark 测试中给出的量级是：在 1 GB cache 下，I/O 密集型查询的逐查询吞吐大约落在本地状态的 75%～120%，小状态查询开销不超过约 10%（VLDB 2025 的 ForSt 论文 §6.2 给出的是逐查询分布，平均差异更小）。这些数字依赖 workload、cache 命中率和存储带宽，不能直接套到你的业务。

### 异步状态访问：ForSt 的另一半

只把状态放到远程 DFS 还不够。如果每次 `state.value()` 都同步等网络，算子线程会被远程延迟卡住。Flink 2.0 的异步状态访问（FLIP-424/425/426/427 一组工作）要解决的就是这个问题：

- 状态访问返回异步句柄/`CompletableFuture` 形状，算子可以在等待期间处理其他记录；
- 运行时可以合并/批量远程读，减少往返；
- 异步执行模型允许一定程度的乱序处理，但仍要保持 watermark、定时器和 key 内语义；
- SQL 侧通过 `table.exec.async-state.enabled` 让部分有状态算子使用异步状态。

示意（具体 API 以版本文档为准）：

```java
// 不是逐字可编译的 API，只表达“先发起、后消费”的形状
CompletableFuture<Long> count = asyncState.value();
count.thenAccept(value -> {
    // 在回调/后续处理中消费状态
});
```

它带来的收益：

- 远程状态延迟被并发/批量化摊薄；
- 大状态算子不必为每次访问阻塞；
- 更适合 ForSt 的“远程主存储”模型。

代价：

- in-flight 记录变多，内存/checkpoint 压力上升；
- 乱序处理让调试和正确性验证更难；
- 并非所有算子/连接器都支持；
- 异步状态与事件时间定时器的交互需要仔细测试。

工程建议：

1. 先在非核心作业上开启，观察 P99、checkpoint 大小、恢复时间；
2. 限制并发/批量大小，避免把远程存储打爆；
3. 给异步路径加超时/降级；
4. 用对账/回放验证“异步”没有改变业务结果。

### 代价与边界

ForSt 不是“免费的性能提升”。截至 2.3，公开资料仍把它标为实验特性，并列出这些限制：

- 不支持 canonical savepoint；
- 不支持全量快照；
- 不支持 changelog state backend；
- 不支持文件合并 checkpoint；
- 并非所有 SQL 算子都完整支持异步状态访问；
- 远程访问有额外延迟，cache 未命中时更明显；
- 孤儿文件清理仍是 best-effort。

因此，**不要因为“2.0 新特性”就把生产大状态切到 ForSt**。正确的评估方式是：

| 问题 | 如果答案是“是” | 如果答案是“否” |
|---|---|---|
| 状态是否达到数百 GB～TB？ | ForSt 值得 POC | 先优化 RocksDB |
| 是否需要频繁扩缩容/快速恢复？ | ForSt 有吸引力 | 本地状态足够 |
| 是否强依赖 canonical savepoint？ | 暂不适合 ForSt | 可评估 |
| 是否能接受实验特性？ | 可小流量试点 | 继续 RocksDB |
| 是否有跨版本恢复要求？ | 先做兼容性演练 | 可评估 |

### 配置与迁移

配置入口通常是：

```yaml
state.backend.type: forst
state.checkpoints.dir: s3://bucket/flink/checkpoints
# 其余 cache/IO 参数以所用版本官方文档为准
```

迁移步骤：

1. 在副本集群用相同状态做一次 restore 演练；
2. 对比 checkpoint 时长、恢复时长、cache 命中率、P99 延迟；
3. 用小流量作业双跑，比较输出；
4. 准备回退到 RocksDB 的 savepoint 路径；
5. 确认监控能覆盖远程存储错误、cache 未命中、孤儿文件。

### MiniFlink 的映射

MiniFlink 的状态是进程内字典，checkpoint 是全量深拷贝。它演示的是“状态可快照、可恢复”，但没有“远程主存储 + 本地 cache”的分离。你可以在 MiniFlink 里加一层 `StateStore` 接口，模拟“远程写成功才算持久化”的语义，理解 ForSt 的失败模型。

## 34.5 物化表：把“新鲜度”变成一等公民

### 从“流作业”到“持续维护的表”

传统实时链路里，用户要自己决定：

- 用流式 SQL 还是批式 SQL；
- 多久刷新一次；
- 全量覆盖还是增量更新；
- 失败后从哪重跑。

物化表（Materialized Table）把这些决策收敛成一条 DDL：**声明一张表、一个新鲜度目标、一个查询**，由 planner 选择 CONTINUOUS（流式增量）或 FULL（批式全量）刷新。

```sql
-- Flink 2.3 开源语法：列/主键 + FRESHNESS；START_MODE 尚不在 2.3 中
CREATE MATERIALIZED TABLE user_revenue (
  user_id STRING,
  total DECIMAL(18, 2),
  PRIMARY KEY (user_id) NOT ENFORCED
)
FRESHNESS = INTERVAL '1' MINUTE
AS
SELECT user_id, SUM(amount) AS total
FROM orders
GROUP BY user_id;
```

几个关键点：

- 物化表由 **Flink 1.20（FLIP-435）** 引入；2.0 继承，2.2 让 `FRESHNESS` 可选并加入 `DISTRIBUTED BY`，2.3 补齐更完整的 `CREATE/ALTER` DDL。`ON CONFLICT` 是同一版本为 upsert sink 引入的 DML 冲突策略，不是物化表 DDL 的子句（见下文）。
- `FRESHNESS` 是**目标新鲜度**，不是硬 SLA；planner 会据此选择刷新模式，但资源不足、源端延迟、故障都会影响实际新鲜度。
- `CONTINUOUS` 模式仍然是一个流作业，消耗流资源；`FULL` 模式需要调度器和全量覆盖能力。
- `START_MODE` 是 **FLIP-557** 提出的扩展，控制首次启动/重建时的读取位置，常见取值包括 `FROM_BEGINNING`、`FROM_NOW`、`FROM_TIMESTAMP(...)`，以及 `RESUME_OR_*` 变体；社区目标版本是 **2.4**，2.3 开源语法中没有这个子句，Confluent 等厂商方言可能已提供。下面讨论的是它的设计意图与迁移影响，不要直接照抄到 2.3 作业。

### `START_MODE` 的工程意义

没有 `START_MODE` 时，物化表重建最怕两件事：

- 从最早 offset 重跑，回放大量历史，资源被打满；
- 从最新 offset 开始，历史数据缺失，结果不对。

`START_MODE` 把“从哪开始”变成显式契约：

| 取值 | 语义 | 适用 |
|---|---|---|
| `FROM_BEGINNING` | 从源端最早位置读 | 首次初始化、需要完整历史 |
| `FROM_NOW` | 从当前最新位置读 | 只关心增量 |
| `FROM_TIMESTAMP(ts)` | 从指定时间读 | 补某个时间段 |
| `RESUME_OR_FROM_*` | 优先恢复上次进度，否则按后缀策略 | `CREATE OR ALTER` 的幂等重建 |

生产上要把 `START_MODE` 和源端保留期、下游可接受的回填窗口一起设计。**能从 savepoint 恢复的，不要用全量重跑**。

### `ON CONFLICT`：upsert sink 的显式选择

2.3 引入 `ON CONFLICT`（FLIP-558），针对 upsert sink：

```sql
INSERT INTO user_revenue_sink
SELECT user_id, total FROM user_revenue
ON CONFLICT DO DEDUPLICATE;
```

三种策略：

- `DO NOTHING`：保留第一条，忽略后续冲突；
- `DO ERROR`：遇到主键冲突直接失败；
- `DO DEDUPLICATE`：保留最后一条（按处理顺序/watermark 语义）。

它解决的问题是：当查询产生的 upsert key 与 sink 的主键不一致时，旧版本会隐式插入 `SinkUpsertMaterializer`，行为不透明且可能状态爆炸。2.3 改为**计划期显式要求选择策略**，否则直接报错。`SinkUpsertMaterializer V2` 和 `ADAPTIVE` 策略则试图在乱序 changelog 下控制状态和性能。

工程建议：

- 明确 sink 的主键和查询的 upsert key 是否一致；
- 不一致时优先在 SQL 里消除乱序（去重、窗口、主键对齐），而不是依赖 materializer；
- 监控 materializer 的状态大小；
- 不要把 `DO DEDUPLICATE` 当成“自动修正数据质量”的银弹。

### 物化表适合谁

适合：

- 指标表/宽表，下游只关心“足够新”；
- 流批一体开发，不想维护两套 SQL；
- 湖仓上的物化视图；
- 平台化产品，把刷新策略交给 planner。

不适合：

- 需要严格逐条语义的实时风控；
- 需要精确控制算子拓扑和状态布局的场景；
- 源端不支持增量读、只能全量覆盖的场景；
- 对 `FRESHNESS` 有硬 SLA 承诺的场景（它只是目标）。

## 34.6 PTF、Changelog 算子与 SQL 表达力

### Process Table Function（PTF）

PTF 是 2.1 引入的“SQL 里的有状态算子”。它比普通 UDF 多出：

- 多表输入、多表输出；
- 按 `PARTITION BY` 的 set 语义；
- 事件时间/处理时间定时器；
- 托管状态；
- changelog 输入/输出。

一个示意（具体注解/签名以官方 PTF 文档为准）：

```sql
SELECT *
FROM my_timeout_ptf(
  input => TABLE orders PARTITION BY user_id,
  on_time => DESCRIPTOR(order_time),
  timeout_seconds => 600
);
```

PTF 的价值是把“ProcessFunction 能做的事”搬进 SQL：

- 会话超时；
- 有状态序列检测；
- 自定义窗口；
- 多流对齐；
- 调用外部服务（配合异步/重试）。

代价是：

- 调试比普通 SQL 难；
- 状态布局和 TTL 要自己设计；
- 不同版本的 API/注解仍在演进；
- 不是所有 connector/format 都支持 changelog 输入输出。

### `FROM_CHANGELOG` / `TO_CHANGELOG`

2.3 把 DataStream 的 `fromChangelogStream`/`toChangelogStream` 带进了 SQL（FLIP-564），但 **2.3 只覆盖基础用法**：`PARTITION BY`、`invalid_op_handling`、`consume_full_deletes`（FROM_CHANGELOG）/`produces_full_deletes`（TO_CHANGELOG）等增强在后续版本才逐步就绪。2.3 上不要照搬更高版本文档里的完整参数。

```sql
-- 把带 op 列的追加流变成动态表（2.3 基础用法）
SELECT *
FROM FROM_CHANGELOG(
  input => TABLE cdc_raw,
  op => DESCRIPTOR(op),
  op_mapping => MAP['c', 'INSERT', 'r', 'INSERT',
                     'ub', 'UPDATE_BEFORE', 'ua', 'UPDATE_AFTER',
                     'd', 'DELETE']
);

-- 把 retract/upsert 流转回追加流
SELECT *
FROM TO_CHANGELOG(input => TABLE user_revenue);
```

典型用途：

- 自定义 CDC 格式，不想写专用 connector；
- 把 upsert 结果写到只接受 append 的下游；
- 在 SQL 层做 changelog 归一化；
- 调试 changelog 语义。

注意：

- `op_mapping` 写错不会报语义错误，只会静默产生错误的表；
- 2.3 不支持 `PARTITION BY`、`invalid_op_handling`、`consume_full_deletes`/`produces_full_deletes` 的完整组合；后续版本的 `invalid_op_handling` 取值为 `FAIL`/`LOG`/`SKIP`，不是 `error_handling`；
- 下游必须能处理 `+I/-U/+U/-D` 或约定的 op 列；
- 升级到支持增强参数的版本前，先用 `EXPLAIN` 和一组脏 op 数据验证行为。

### `VARIANT`、`QUALIFY` 与 Delta Join

- `VARIANT`：半结构化 JSON 类型，保留字段类型信息；配合 `PARSE_JSON`/`TRY_PARSE_JSON`，适合 schema 频繁变化的埋点/日志。
- `QUALIFY`（2.0）：在窗口函数之后过滤，减少子查询嵌套。
- **Delta Join**：多条同源流按相同 key 做 join 时，用外部索引表的双向点查替代两侧完整 join 状态，本地只保留缓存与键队列；适合 CDC 宽表和多 topic 同源场景。它依赖 Fluss/Paimon 等支持点查/增量读的存储，代价是外部存储的延迟和可用性。不要把它和“维表 lookup join”混为一谈。
- **StreamingMultiJoin**：多流 join 时消除中间状态，适合星型/雪花型实时宽表，但仍需关注 shuffle 和状态。

这些特性在 2.1～2.3 逐步成熟，但都有版本/连接器前提。选型时先问：**我的存储支不支持？我的 SQL 能不能改？我的状态预算够不够？**

## 34.7 运行时与运维增强

2.x 的运行时改动不如 API 显眼，但对生产影响很大：

| 能力 | 版本 | 作用 | 注意 |
|---|---|---|---|
| 自适应分区选择 | 2.3 | 根据数据分布选择分区策略 | 通过 `taskmanager.network.adaptive-partitioner.enabled` 开启 |
| 自适应调度器扩缩容历史 | 2.3 | Web UI 查看 rescale 历史 | 便于复盘 |
| 恢复期 checkpoint | 2.3 | 恢复过程中也能做 checkpoint | 缩短二次故障窗口，但增加恢复期负载 |
| 应用级生命周期 | 2.3 | 集群 → 应用 → 作业的层级管理 | 多作业平台更清晰 |
| 原生 S3 文件系统 | 2.3 | 不依赖 Hadoop，AWS SDK v2 | 实验；新配置命名空间 |
| OTel gRPC exporter | 2.3 | 更好的 OpenTelemetry 导出 | 仍需关注指标基数 |
| PyFlink 异步 | 2.2 | Python 异步 lookup/外部调用 | 受 GIL 和线程池影响 |

“恢复期 checkpoint”对应 FLIP-547，默认关闭，需要同时打开两个开关（第二个依赖第一个）：

```yaml
execution.checkpointing.unaligned.recover-output-on-downstream.enabled: true
execution.checkpointing.unaligned.during-recovery.enabled: true
```

它解决的是非对齐 checkpoint 恢复期间“必须先把 in-flight 通道数据消费完才能做下一次 checkpoint”的问题。大状态作业反复扩缩容时，这个窗口可能长达数小时；开启后可以边恢复边快照，缩短二次故障的暴露时间。代价是恢复期同时承担恢复流量和快照流量，且只对非对齐 checkpoint 路径有意义，普通的对齐 checkpoint 不需要它。

这些能力的共同方向是：**让 Flink 更像云原生平台，而不是一个需要手工调参的 JVM 集群**。但每一项都有代价：自适应调度会增加决策复杂度，恢复期 checkpoint 会增加资源竞争，原生 S3 仍是实验。生产启用前要做压测和回滚演练。

## 34.8 1.x → 2.x 迁移手册

### 第一步：盘点

先列一张表，不要直接改代码：

| 类别 | 要检查什么 |
|---|---|
| API | `SourceFunction`、`SinkFunction` V1、DataSet、Scala API、`open(Configuration)` |
| 状态 | 状态后端、savepoint 格式、Kryo/POJO、State Processor API |
| 配置 | `flink-conf.yaml` → `config.yaml`；`Time` → `Duration`；StateBackend getter/setter |
| 连接器 | 是否支持 2.x；是否依赖已移除的 V1 接口 |
| SQL | planner 版本、物化表、changelog、连接器 DDL |
| 运行时 | Java 版本、内存模型、网络缓冲、调度器 |
| 运维 | 镜像、Operator 版本、监控指标、告警规则 |

### 第二步：编译与替换

常见替换：

```text
SourceFunction        → FLIP-27 Source
SinkFunction V1       → Sink V2
DataSet API           → DataStream BATCH 模式 / Table SQL
Scala API             → Java API / Python API
open(Configuration)   → open(OpenContext)
org.apache.flink.api.common.time.Time → java.time.Duration
flink-conf.yaml       → config.yaml
```

不要用“兼容层”掩盖问题。兼容层可能让代码编译通过，但状态/序列化/连接器行为仍然不兼容。

一张更细的迁移矩阵：

| 1.x | 2.x | 影响 |
|---|---|---|
| `SourceFunction`/`RichSourceFunction` | FLIP-27 `Source`/`SourceReader` | 自定义 source 要重写 |
| `SinkFunction` V1 | Sink V2 | 自定义 sink 要重写 |
| `DataSet` API | DataStream BATCH / Table SQL | 批作业要迁移 |
| Scala API | Java/Python API | Scala 作业要重写 |
| `open(Configuration)` | `open(OpenContext)` | 编译错误 |
| `org.apache.flink.api.common.time.Time` | `java.time.Duration` | 编译错误 |
| `flink-conf.yaml` | `config.yaml` | 配置迁移 |
| `state.backend.local-recovery` | `execution.checkpointing.local-backup.enabled` + `execution.state-recovery.from-local` | 本地恢复语义拆分 |
| DataSet State Processor API | DataStream `SavepointReader`/`SavepointWriter` | 离线状态工具要重写 |
| 1.x savepoint | 2.x savepoint | 不保证二进制兼容，需演练 |

状态兼容要单独评估：

- 1.x 的 Kryo/POJO/RocksDB 格式与 2.x 不一定兼容；
- POJO 集合字段、类名/包名变化会放大风险；
- SQL 作业的状态兼容性也受 planner/算子 uid 影响；
- 跨大版本恢复优先用 State Processor API 或源端重放，不要假设 `flink run -s` 一定成功。

### 第三步：配置迁移

```bash
./bin/migrate-config-file.sh flink-conf.yaml config.yaml
```

迁移后逐项核对：

- 状态后端类型；
- checkpoint/savepoint 目录；
- HA 配置；
- 网络内存；
- 调度器；
- 指标 reporter。

配置项改名不会自动帮你验证语义。比如 `state.backend` 的写法、`execution.checkpointing.*` 的默认值、`taskmanager.memory.*` 的分配，都可能在迁移后悄悄变化。

### 第四步：状态恢复演练

1. 在 1.x 上做一次完整 savepoint；
2. 在 2.x 副本集群用 `flink run -s` 恢复；
3. 对比算子 uid、状态大小、关键输出；
4. 如果失败，评估：
   - State Processor API 离线迁移；
   - 从源端重放；
   - 先在 1.20 上做中间格式转换；
   - 重新初始化状态（仅适用于可重算场景）。

**不要在生产集群直接试。** 状态不兼容的失败往往不是“起不来”，而是“起来了但结果不对”。

### 第五步：双跑与切流

- 影子流量：新旧作业同时消费，比较输出；
- 抽样对账：按事件 id/时间窗口核对；
- 灰度：1% → 10% → 50% → 100%；
- 回滚：旧镜像 + 旧 savepoint，确认能在 10 分钟内回退；
- 观察：checkpoint 时长、状态大小、P99、反压、GC。

### 第六步：清理

- 移除废弃依赖和兼容层；
- 更新监控指标名称；
- 更新 runbook；
- 记录版本矩阵和已知限制；
- 把“实验特性”列入技术债清单。

## 34.9 MiniFlink 的映射与练习

MiniFlink 没有实现 2.x 的 API 和 ForSt，但它可以帮你理解“为什么”：

| 2.x 概念 | MiniFlink 的近似物 | 差距 |
|---|---|---|
| DataStream V2 的 ProcessFunction | `ProcessFunction` / `KeyedProcessFunction` | 无显式流类型/状态声明 |
| StateDeclaration | `ValueStateDescriptor` | 无重分配模式 |
| ForSt | 进程内状态 + 全量快照 | 无远程主存储/异步 flush |
| 物化表 | `TableEnvironment` 的有界 SQL | 无 CONTINUOUS/FULL 刷新 |
| PTF | `ProcessFunction` + Table 子集 | 无 changelog/定时器 SQL 集成 |
| FROM/TO_CHANGELOG | 无 | 不支持 changelog |

练习：

1. 给 MiniFlink 的 `ValueStateDescriptor` 加一个 `redistribution_mode` 字段，模拟 V2 的状态声明；
2. 把 `KeyedStateBackend` 抽成接口，分别实现“内存”和“文件”两种，模拟 ForSt 的远程/本地分层；
3. 给 Table 层加一个 `FRESHNESS` 元数据（只记录，不执行），理解物化表的声明式设计；
4. 用 `ProcessFunction` 实现一个简化 PTF：多表输入、按键分区、定时输出。

## 34.10 2.x 连接器与生态迁移清单

Flink 2.0 移除了 `SourceFunction`、`SinkFunction` V1、DataSet 和 Scala API。很多团队把注意力放在用户代码上，却忽略了**连接器也是 API 的一部分**：连接器的版本、状态格式和提交协议，决定了迁移能否真正上线。

### 先分清四类“版本”

| 版本 | 例子 | 不匹配的后果 |
|---|---|---|
| Flink 版本 | 2.3.0 | 编译/运行时 API 不兼容 |
| 连接器版本 | Kafka connector 4.x | 类找不到、行为变化 |
| 格式/序列化版本 | Avro/Protobuf/JSON schema | 反序列化失败、字段错位 |
| 外部系统版本 | Kafka 3.x/4.x、Paimon/Iceberg | 协议/事务/元数据不兼容 |

“Flink 2.3 + 最新 connector”不一定可用；要以连接器的兼容矩阵为准。SQL connector 尤其容易混用：`flink-sql-connector-kafka` 的版本与 Flink 小版本、Kafka 客户端版本都相关。

### 迁移检查矩阵

| 类别 | 要检查 | 典型问题 |
|---|---|---|
| Source | 是否 FLIP-27 `Source`；是否保存 offset/分片状态 | 旧 `SourceFunction` 无法编译 |
| Sink | 是否 Sink V2；是否支持 2PC/幂等 | 旧 `SinkFunction` 被移除 |
| State | 连接器状态格式是否跨版本兼容 | offset 状态读不出、恢复失败 |
| SQL | connector option 是否改名/移除 | 作业启动即报未知参数 |
| 序列化 | Avro/Protobuf/JSON schema 是否兼容 | 字段缺失/类型变化 |
| 安全 | TLS/SASL/凭证注入方式 | 认证失败、密钥泄漏 |
| 指标 | 指标名/标签是否变化 | 看板空、告警失效 |
| 部署 | Java 17、镜像、Operator 版本 | 启动失败、类冲突 |

### 状态兼容比代码兼容更难

连接器的状态也进入 savepoint：

- Kafka source 的 offset/分区分配；
- 文件 source 的已读文件/位置；
- 事务 sink 的 transaction id/committer 状态；
- CDC source 的 binlog 位点/schema 历史。

升级连接器时，要单独验证：

1. 旧连接器 savepoint → 新连接器 restore；
2. offset/位点是否连续；
3. 事务是否悬挂/重复提交；
4. 分区发现/扩缩容后状态是否重分配；
5. 下游是否读到重复/丢失。

不要假设“用户状态兼容”就等于“连接器状态兼容”。

### 迁移顺序

1. **盘点**：列出所有 source/sink、连接器版本、SQL DDL、UDF、格式；
2. **编译**：替换已移除 API，先让代码在 2.x 编译；
3. **单连接器验证**：在副本集群跑最小 source→sink，验证 offset/事务；
4. **状态演练**：旧 savepoint → 新代码 restore，核对输出；
5. **SQL 计划 diff**：connector 参数、changelog 模式、sink 能力；
6. **配置迁移**：`flink-conf.yaml` → `config.yaml`，逐项核对；
7. **灰度**：影子流量/小比例，观察 checkpoint、lag、重复；
8. **回滚**：旧镜像 + 旧 savepoint，确认可回退。

### 常见坑

- **只看编译**：编译通过但运行时类冲突（shaded 依赖）；
- **混用 connector JAR**：SQL 作业的 JAR 版本与集群不一致；
- **忽略事务超时**：新连接器默认值变化，慢 checkpoint 反复失败；
- **忽略分区发现**：新分区从 earliest/latest 开始，结果重复/缺失；
- **忽略 schema 演进**：CDC DDL 在下游失败；
- **忽略指标改名**：升级后监控空白；
- **忽略 Operator 版本**：Flink 2.x 与 Operator 的支持矩阵独立。

### 一个最小验收清单

- [ ] 所有连接器都有明确的 Flink/外部系统兼容版本；
- [ ] 旧 savepoint 能在新连接器上恢复；
- [ ] source offset/位点连续，sink 无重复/悬挂事务；
- [ ] SQL DDL 的参数在 2.x 全部合法；
- [ ] 序列化格式与 schema 演进策略验证过；
- [ ] 指标/日志/告警在新版本仍可用；
- [ ] 有灰度与回滚路径；
- [ ] 记录版本矩阵与已知限制。

## 34.11 VARIANT、Delta Join 与 StreamingMultiJoin 的工程细节

34.6 末尾只给了这三个特性一句话。它们恰好是 2.1～2.3 最容易“听说很厉害、落地踩坑”的部分，值得单独展开。

### 34.11.1 VARIANT 与 PARSE_JSON

`VARIANT` 是半结构化类型（FLIP-521），用来存“结构不固定、但需要保留类型信息”的 JSON。与 `STRING`/`ROW`/`MAP` 的差别：

| 类型 | 结构 | 类型信息 | 适合 |
|---|---|---|---|
| `STRING` | 任意文本 | 无 | 原样存储/透传 |
| `ROW` | 固定字段 | 编译期确定 | schema 稳定的嵌套数据 |
| `MAP<STRING, ...>` | 键值 | value 类型统一 | 同构字典 |
| `VARIANT` | 任意嵌套 | 保留（标量/数组/对象） | 埋点、日志、频繁演进的 JSON |

解析与取值：

```sql
CREATE VIEW parsed_events AS
SELECT id, PARSE_JSON(payload) AS v
FROM raw_events;

SELECT
  id,
  VARIANT_VALUE(v, '$.user.id' RETURNING BIGINT) AS user_id,
  VARIANT_VALUE(v, '$.amount' RETURNING DECIMAL(18, 2)) AS amount,
  VARIANT_QUERY(v, '$.items' RETURNING ARRAY<STRING>) AS items,
  TO_JSON(v) AS normalized_json
FROM parsed_events;
```

要点：

- `PARSE_JSON` 遇到非法 JSON 会报错；脏数据场景用 `TRY_PARSE_JSON`，返回 `NULL`；
- `PARSE_JSON(json[, allow_duplicate_keys])` 默认不允许重复 key，需要“后者覆盖”时显式开启；
- `VARIANT_VALUE` 取标量，`VARIANT_QUERY` 取数组/对象等更复杂的值；两者都支持 `ON EMPTY`/`ON ERROR` 策略；
- 支持 `v['key']`、`v[index]` 等下标访问；
- `VARIANT` 不能随意 cast 成其他类型，要通过取值函数；
- 解析有 CPU 成本，不要对同一列反复 `PARSE_JSON`；用计算列/视图物化一次；
- Parquet 等格式对 `VARIANT` 的支持随版本演进，落地前验证读写链路。

VARIANT 不是“免 schema”的银弹：它适合“结构经常变、查询只取少数字段”的场景；如果字段稳定、需要大量聚合，还是应该解析成强类型列。

### 34.11.2 Delta Join

Delta Join（FLIP-486）解决的是“两条 append 流按同一 key 长期 join，状态无限增长”的问题。它不再为两侧各维护一份完整状态，而是把 join 变成对**外部索引表**的双向点查：

```text
流 A ──┐
       ├─► Delta Join ──► 结果
流 B ──┘        │
                └─ 点查外部索引（Fluss/Paimon 等）
```

配置：

```yaml
table.optimizer.delta-join.strategy: AUTO      # AUTO / FORCE / NONE
# cache 是 2.2+ 能力；2.1 只有 join 本体
table.exec.delta-join.cache-enabled: true
table.exec.delta-join.left.cache-size: 10000
table.exec.delta-join.right.cache-size: 10000
```

前提与限制（2.1 基线）：

- source 必须是 append-only，并且有单一前缀/索引键；
- 外部存储支持按 key 点查/增量读（Fluss 是典型实现）；
- sink 必须能“忽略删除”（宽表构建场景）；
- 计划里不能有其他有状态算子（聚合、TopN 等）；
- 只适用于受支持的 join 类型/键模式，`FORCE` 在不满足时会直接失败；
- 代价从“状态内存”转移到“外部存储延迟/可用性/QPS”。

它不是“更快的 regular join”，而是**用外部索引换状态**。外部存储的 P99、限流和故障会直接进入作业延迟；选型时要把这部分算进 SLO。

### 34.11.3 StreamingMultiJoin

StreamingMultiJoin（FLIP-516）把 N 条流的链式二元 join 合并成一个多输入算子，消除中间 join 的状态：

```text
t1 JOIN t2 JOIN t3 JOIN t4
        │
        ├─ 传统： (t1⋈t2) ⋈ t3 ⋈ t4，每层一份状态
        └─ MultiJoin：一个算子同时处理 t1..t4，中间状态为 0
```

配置与提示：

```yaml
table.optimizer.multi-join.enabled: true
table.optimizer.multi-join.max-tables: 10
```

```sql
SELECT /*+ MULTI_JOIN(t1, t2, t3) */ ...
FROM t1 JOIN t2 ON t1.k = t2.k
        JOIN t3 ON t2.k = t3.k;
```

限制：

- 只支持 streaming；
- 目前主要支持 INNER/LEFT，RIGHT/FULL 还不完整；
- 需要至少一个共享 join key（传递性也算），否则会拆成多个 MultiJoin；
- 仍然有 shuffle 和输出放大的成本，只是省掉了中间状态；
- 计划是否真正应用要用 `EXPLAIN` 确认，不要只看配置。

官方给出的收益量级是“状态小 3～1000 倍、吞吐高 2～100 倍”，但这是在 record amplification 明显的管道上；如果你的 join 本来就很窄、状态很小，收益有限。

### 34.11.4 选型决策表

| 场景 | 首选 | 状态 | 前提 |
|---|---|---|---|
| 两条流按时间窗口关联 | Interval Join | 窗口内状态 | 有时间字段、可定义区间 |
| 流 + 维表点查 | Lookup Join | 缓存/外部表 | 维表可点查 |
| 流 + 版本表（汇率/价格） | Temporal Join | 版本状态 | 有版本时间/主键 |
| 两条 append 流长期关联 | Regular Join + TTL | 两侧状态 | 能接受状态增长/回撤 |
| 多条同源流按同 key 构宽表 | Delta Join | 外部索引 | append-only、外部可点查、sink 忽略删除 |
| 星型/雪花多流 join | StreamingMultiJoin | 无中间状态 | 共享 key、INNER/LEFT、streaming |
| 需要更新/删除/复杂状态 | Regular Join | 完整状态 | 不要用 Delta/MultiJoin 硬套 |

三条落地建议：

1. 先用 `EXPLAIN` 确认优化器真的选了 Delta/MultiJoin；
2. 用真实数据量压测“状态节省”和“外部存储 QPS/延迟”的净收益；
3. 给外部索引表做容量、限流、故障预案，否则省下的状态会变成新的可用性风险。

## 本章小结

Flink 2.x 的主线不是“更多 API”，而是**重新划分用户与运行时的边界**：

- DataStream API V2 把算子内部藏起来，用 ProcessFunction/StateDeclaration/广义 watermark 重新表达；
- ForSt 把状态从本地盘搬到 DFS，用远程主存储换取快照、恢复和扩缩容的弹性；
- 物化表把“新鲜度”和“启动模式”变成声明式契约；
- PTF/changelog 算子把 SQL 的表达力推向有状态、多表、AI 场景；
- 迁移的关键不是改 API，而是**验证状态、配置、连接器和端到端语义**。

源码阅读路线见附录 H；SQL 语法/函数速查见附录 G。

下一章进入实时 Data + AI：当 `ML_PREDICT`、`VECTOR_SEARCH` 和 Flink Agents 进入流作业，延迟、成本、幂等和可观测性会发生什么变化。
