# 第18章 流批一体

## 18.1 统一的 API，不同的执行模式

Flink 的“流批一体”不是把批处理伪装成流处理，而是：

- 同一套 DataStream/Table API；
- 同一套 SQL；
- 同一个运行时；
- 根据输入是否有界、执行模式，选择流式或批式执行策略。

```java
env.setRuntimeMode(RuntimeExecutionMode.BATCH);
```

或：

```yaml
execution.runtime-mode: BATCH
```

三种模式：

- `STREAMING`：无界/有界都按流处理；
- `BATCH`：有界输入，阻塞 shuffle，优化吞吐；
- `AUTOMATIC`：根据 source 是否有界自动选择（部分版本支持）。

```text
同一份 SQL
   ├─ STREAMING：逐条/微批，状态常驻，checkpoint
   └─ BATCH：阻塞 shuffle，排序/合并，失败重跑
```

## 18.2 批模式的不同

批模式下：

- 输入必须有界；
- shuffle 可以落盘、排序、合并；
- 算子可以等待所有输入到齐再计算；
- checkpoint 通常不开启（失败重跑整个作业）；
- 可以使用更激进的优化（join 重排、动态分区裁剪）；
- 延迟以分钟/小时计，吞吐优先。

流模式：

- 输入无界；
- shuffle 持续进行；
- 状态和 checkpoint 是一等公民；
- 延迟秒级/毫秒级；
- 不能假设“所有输入到齐”。

### 阻塞 shuffle

批模式的 shuffle 可以把中间结果写到磁盘，按分区排序后一次性交给下游：

```text
map → shuffle write（落盘/分区/排序）→ shuffle read → reduce
```

好处：

- 下游可以顺序读，减少随机 IO；
- 可以在 shuffle 阶段做合并/压缩；
- 失败可以只重算部分分区。

代价：

- 延迟高（必须等上游写完）；
- 需要本地磁盘；
- 对倾斜敏感。

批模式的容错与输出提交是一个独立问题：失败后是重跑整个 stage、复用 shuffle，还是从 checkpoint 恢复，取决于作业时长、输出语义和代价。18.6 会展开。

## 18.3 自适应批调度

Flink 1.15+ 引入自适应批调度器（Adaptive Batch Scheduler），根据每个 stage 的实际数据量决定并行度：

```yaml
# 批作业默认就是 AdaptiveBatch；只有被显式改成别的调度器时才需要写
jobmanager.scheduler: AdaptiveBatch
execution.batch.adaptive.auto-parallelism.enabled: true
execution.batch.adaptive.auto-parallelism.max-parallelism: 128
```

工作方式：

1. 先跑上游 stage，产出 shuffle 数据；
2. 统计每个分区的数据量；
3. 动态决定下游 stage 的并行度；
4. 避免“并行度拍脑袋”导致的资源浪费或倾斜。

适合：

- 数据量波动大的批作业；
- 多 stage 的复杂 ETL；
- 无法提前准确估算并行度的场景。

限制：

- 需要等待上游完成，增加延迟；
- 对流模式不适用；
- 某些算子/连接器可能不支持。

stage、shuffle 与批算子层面的细节见 18.9；这里先建立“按数据量调并行度”的概念。

## 18.4 何时用批模式

- 历史数据回填；
- 离线报表/对账；
- 大规模 ETL；
- 复杂多表 join，流式状态成本过高；
- 算法训练样本生成；
- 数据质量校验。

何时用流模式：

- 实时监控/告警；
- 实时数仓明细；
- 事件驱动业务；
- 需要持续更新结果；
- 需要秒级/毫秒级延迟。

```text
延迟要求 < 秒级 → 流
延迟要求 分钟/小时 → 批
需要持续更新 → 流
只需最终结果 → 批
状态成本高、可重跑 → 批
```

很多团队采用“Lambda 架构”：流式链路保证低延迟，批式链路每天修正。Flink 的流批一体让两条链路共享 SQL 和元数据，但**不消除**架构上的双链路成本。

### Kappa 架构

Kappa 架构只用流式链路，历史数据通过重放 Kafka/湖表来重算。它适合：

- 消息保留时间足够长；
- 重放成本可接受；
- 团队不想维护两套链路。

Flink 的流批一体让 Kappa 更可行：同一套 SQL 既能实时跑，也能对历史数据重放。但重放大规模历史仍然昂贵，很多团队最终回到“流 + 批修正”的混合模式。

## 18.5 有界 source

批模式要求 source 能报告“有界”。文件 source、数据库 snapshot、批 Kafka（指定结束 offset）都可以。无界 Kafka topic 在批模式下需要显式设置结束条件，否则作业不会结束。

Table 环境中，`scan.bounded.mode`（`latest-offset`/`group-offsets`/`timestamp`/`specific-offsets`）决定“读到哪结束”，`scan.bounded.timestamp-millis`/`scan.bounded.specific-offsets` 提供对应的结束位点；`scan.startup.mode` 决定起点。Kafka 连接器没有 `scan.stop.timestamp` 这个键。

```sql
CREATE TABLE orders (
  ...
) WITH (
  'connector' = 'kafka',
  'scan.startup.mode' = 'earliest-offset',
  'scan.bounded.mode' = 'latest-offset'   -- 读到当前最新即结束
);
```

### 混合 source

Hybrid Source 可以先读历史（有界），再切换到实时（无界）：

```java
HybridSource<Event> source = HybridSource.builder(historySource)
    .addSource(kafkaSource)
    .build();
```

它在“回填 + 实时”场景很有用，但切换边界必须精确，否则会重复/遗漏。

## 18.6 批模式的恢复、输出提交与幂等边界

批模式没有“持续 checkpoint 兜底”的默认假设，故障恢复更像传统数据仓库作业：**重算 + 覆盖**。但“重算”并不等于“免费”，也不自动等于“结果正确”。

### 18.6.1 三种恢复路径

```text
失败
├─ 重跑整个作业：从 source 重新读，丢弃所有中间结果
├─ 复用已持久化的 shuffle：只重算失败 stage 及其下游
└─ 从 checkpoint 恢复：保留已完成 stage 的状态（长批作业）
```

选择依据：

| 路径 | 适合 | 代价 |
|---|---|---|
| 整作业重跑 | 短作业、source 便宜、输出可覆盖 | 浪费已完成工作 |
| 复用 shuffle | 多 stage、上游昂贵 | 依赖 shuffle 持久化与清理 |
| checkpoint 恢复 | 数小时级、迭代/机器学习、外部副作用少 | 状态存储、快照开销、语义约束 |

Flink 的批执行可以开启 checkpoint，但“有 checkpoint”不代表端到端 exactly-once；仍要 source 可重放、sink 提交协议正确。

### 18.6.2 shuffle 数据不是永久真相

阻塞 shuffle 会把中间结果写到本地磁盘/远程存储。失败后能否复用，取决于：

- shuffle 文件是否还在；
- 上游 stage 是否被标记为可重算；
- 作业是否换了并行度；
- 集群是否换了节点。

不要把“shuffle 落盘”当成持久化存储。它通常有清理策略，节点故障也可能丢失；真正的恢复依据仍是 source + 可重算的 DAG。

### 18.6.3 输出提交语义

批模式常见的 sink 语义：

| sink | 幂等方式 | 注意 |
|---|---|---|
| 文件/对象存储 | 覆盖分区、临时目录 + 原子 rename | 对象存储 rename 非原子，需提交协议 |
| 湖仓表 | 快照/事务提交、`INSERT OVERWRITE` | 并发写同一分区要协调 |
| JDBC | 主键 upsert、事务 | 批量提交失败可能部分成功 |
| Kafka | 幂等 producer/事务，或业务去重 | append-only 重跑会重复 |

```sql
-- 批模式覆盖写分区：最常见的幂等模式
INSERT OVERWRITE my_table PARTITION (dt = '2026-11-26')
SELECT ...;
```

覆盖写要求：

- 分区边界明确，不能“算到哪写到哪”；
- 下游能接受提交前的短暂不可见；
- 并发写同一分区要加锁/协调；
- 失败重跑不能把不完整分区暴露给查询。

如果 sink 只支持 append（例如普通 Kafka），重跑必然产生重复；要么用幂等键 + 下游去重，要么改成事务/覆盖写。

### 18.6.4 外部副作用

批作业也会调用外部系统：

- 写邮件/告警；
- 调模型/第三方 API；
- 更新缓存/搜索索引；
- 触发下游调度。

这些副作用不随 Flink 作业回滚。重跑时要么幂等（request_id/业务主键），要么把副作用挪到单独的“提交后”步骤，不要在 `map()` 里边算边发。

### 18.6.5 批模式恢复检查清单

- [ ] 失败后是整作业重跑、复用 shuffle，还是 checkpoint 恢复？
- [ ] source 是否可重放？重放成本是否可接受？
- [ ] sink 是覆盖写、upsert 还是 append？重跑会不会重复？
- [ ] 分区提交是否原子？失败时会不会暴露半成品？
- [ ] 外部副作用是否幂等？能否挪到提交后？
- [ ] 并行度变化后 shuffle 是否还能复用？
- [ ] 恢复时间是否在 SLO 内？

## 18.7 性能差异

流模式：

- 逐条/微批处理；
- 状态访问频繁；
- 网络常驻；
- checkpoint 开销。

批模式：

- 阻塞 shuffle，减少重复计算；
- 可以使用排序/合并；
- 内存/磁盘调度更激进；
- 没有 checkpoint 常驻开销。

同一个 SQL 在两种模式下可能生成完全不同的物理计划。不要假设“流模式能跑，批模式一定更快”；要用实际数据压测。

### 一个对比

```text
流模式：source → 持续聚合 → 每秒输出 → checkpoint 每 30s
批模式：source → 全量扫描 → 排序聚合 → 一次输出 → 失败重跑
```

对于“每天全量重算”的报表，批模式通常快一个数量级；对于“持续更新”的指标，流模式是唯一选择。

## 18.8 物化表：流批一体的新抽象

物化表把“新鲜度”作为配置，让 planner 在 CONTINUOUS（持续增量）和 FULL（全量刷新）之间选择。它减少“为同一指标维护流批两套 SQL”的成本，但没有消除底层资源成本：刷新调度、状态/存储、失败重试、数据质量和权限仍要平台管理。

DDL 形态见[第 14 章 14.8 节](14-table-sql.md#148-物化表materialized-table)，`ON CONFLICT`、刷新成本和迁移策略的完整讨论见[第 34 章 34.5 节](34-flink2-deep-dive.md#345-物化表把新鲜度变成一等公民)；`START_MODE` 是 FLIP-557 的 2.4 路线/厂商方言，2.3 开源语法没有，这里不重复。

## 18.9 批执行的内核：shuffle、stage 与自适应调度

流批一体不是“把流模式的算子换个开关”。批模式会重新组织执行图：stage 之间可以阻塞、shuffle 可以落盘、并行度可以按数据量动态决定。理解这些差异，才能解释“为什么同一个 SQL 在批模式快一个数量级，或者反而更慢”。

### 18.9.1 stage 与 shuffle 边界

```text
source → map/filter → [blocking shuffle] → aggregate → [blocking shuffle] → sink
         stage 0                         stage 1                     stage 2
```

- **pipelined exchange**：上游边生产边下游消费，延迟低，适合流和部分批算子；
- **blocking exchange**：上游写完，下游才能开始；可以排序/合并/落盘，吞吐高；
- **stage**：被 blocking exchange 切开的调度/恢复单元；失败可以只重算一个 stage。

Flink 的批 shuffle 模式可以配置：

```yaml
execution.batch-shuffle-mode: ALL_EXCHANGES_BLOCKING
# 可选值/默认值随版本变化，常见还有 ALL_EXCHANGES_PIPELINED、
# ALL_EXCHANGES_HYBRID_FULL、ALL_EXCHANGES_HYBRID_SELECTIVE；
# 以所用版本文档为准
# 注意：POINTWISE_EDGES_PIPELINED 属于 table.exec.shuffle-mode 的旧词汇，
# 不是 execution.batch-shuffle-mode 的合法值
```

全阻塞适合大吞吐 ETL；全流水线适合低延迟/小数据量。不要把“批模式”等同于“所有边都阻塞”，优化器会按算子需求选择。

### 18.9.2 批算子为什么不一样

批模式可以选择流模式不能用的物理算子：

| 逻辑操作 | 流模式常见 | 批模式可选 |
|---|---|---|
| join | hash join + 状态 | sort-merge join、hash join、broadcast join |
| 聚合 | 增量聚合 + 状态 | hash aggregate、sort aggregate、local/global |
| 去重 | 状态 + TTL | sort-based dedup、hash-based dedup |
| TopN | 状态 + 更新流 | 排序 + limit |
| 输出 | 持续 changelog | 一次性覆盖/提交 |

批模式可以做**阻塞式**的全局排序、分区裁剪、join 重排；流模式必须假设输入无界，不能等“所有数据到齐”。

### 18.9.3 AdaptiveBatch 调度

AdaptiveBatch 的核心是“先跑上游，拿到真实 shuffle 大小，再决定下游并行度”：

```text
stage 0 完成
  → 统计每个 shuffle 分区大小
  → 计算 stage 1 的并行度（受 min/max 限制）
  → 部署 stage 1
```

```yaml
jobmanager.scheduler: AdaptiveBatch
execution.batch.adaptive.auto-parallelism.enabled: true
execution.batch.adaptive.auto-parallelism.max-parallelism: 128
```

它解决的是“并行度拍脑袋”：

- 数据量小 → 少开 task，减少调度/小文件开销；
- 数据量大 → 多开 task，避免单 task 扫全量；
- 分区倾斜 → 仍需要额外的倾斜处理，调度器不会自动拆分热点 key。

限制：

- 必须等上游 stage 完成，增加首字节延迟；
- 对流模式不适用；
- 某些 source/算子不支持动态并行度；
- 并行度变化会影响 shuffle 文件布局和恢复。

### 18.9.4 批模式的资源与恢复

批作业通常比流作业更“吃”本地磁盘和网络：

- shuffle 落盘；
- 排序/哈希表 spill；
- 大 stage 的并发 task；
- 失败重算的重复 IO。

恢复策略要和 stage 设计一起看：

- stage 越小，失败重算范围越小，但调度开销越大；
- shuffle 持久化能减少重算，但增加存储和清理成本；
- 长批作业可以开 checkpoint，但要确认 source/sink 语义；
- 动态并行度让“恢复后 stage 布局”与首次执行不同，压测要覆盖。

### 18.9.5 批模式调优顺序

1. **减少数据量**：分区裁剪、谓词下推、列裁剪；
2. **减少 shuffle**：预聚合、broadcast、join 重排；
3. **处理倾斜**：salting、两阶段聚合、拆分热点；
4. **调 stage 并行度**：AdaptiveBatch + 真实 shuffle 统计；
5. **调内存/磁盘**：managed memory、spill、本地盘；
6. **调 shuffle 模式**：阻塞 vs 流水线；
7. **最后调资源**：加 CPU/磁盘/带宽。

批模式的瓶颈经常是“某个 stage 的 shuffle 倾斜”，而不是总 CPU 不够。先用 Web UI 看每个 stage 的 input/output records、shuffle bytes、spill 和 task 时长分布，再决定加不加资源。针对长尾 task 的推测执行和针对磁盘瓶颈的混合 shuffle，见 18.12。

## 18.10 流批一体的工程实践

- SQL/表定义共享，执行模式作为配置；
- 回填任务和实时任务使用同一套 UDF，避免口径不一致；
- 批任务输出覆盖分区，流任务输出 upsert；
- 监控两条链路的结果差异；
- 用同一套数据质量规则；
- 明确哪些表支持 changelog，哪些只支持 append；
- 为流/批分别设置资源队列，避免互相挤占；
- 记录每条链路的版本和血缘。

## 18.11 MiniFlink 的定位

MiniFlink 是**有界流**教学运行时：source 读完就发 `Watermark(+INF)` 和 `EndOfStream`，没有单独的批模式。它的执行模型更接近“有限流”，可以用来观察窗口和 checkpoint，但不具备 Flink 批模式的阻塞 shuffle、优化器和任务重跑。`set_runtime_mode(BATCH)` 目前只记录配置，不改变执行语义；这个差异在附录 C 中列为明确限制。

## 18.12 批作业的尾部延迟：推测执行与混合 shuffle

批作业的完成时间由**最慢的 task** 决定，而不是平均 task。数据倾斜、坏节点、磁盘抖动都会让少数 task 拖住整个 stage。Flink 为此提供两类手段：推测执行（用重复计算换尾部延迟）和混合 shuffle（用分层存储换磁盘/网络弹性）。

### 18.12.1 推测执行只适用于批模式

```yaml
execution.batch.speculative.enabled: true
execution.batch.speculative.max-concurrent-executions: 2
execution.batch.speculative.block-slow-node-duration: 1min
```

工作方式：

1. AdaptiveBatch 调度器持续观察同一 stage 内各 task 的执行进度；
2. 慢任务检测器把“执行时间 / 已处理数据量”显著高于中位数的 task 标记为慢任务；
3. 为慢 task 启动一个 speculative attempt，与原 attempt 并行跑；
4. 任一 attempt 先完成，就取消其余 attempt，并把慢节点短暂加入 blocklist。

限制与代价：

- **只支持批作业**，且要求 AdaptiveBatch 调度器；流作业不适用；
- **sink 默认不参与推测**，需要实现 `SupportsConcurrentExecutionAttempts`；带自定义 `SourceEvent` 的 source 需要实现 `SupportsHandleExecutionAttemptSourceEvent`，否则推测 attempt 的事件会导致作业失败；committer 算子不参与；
- 会**重复计算、重复写 shuffle**，甚至重复产生副作用；
- 因此 sink 必须幂等或事务化，否则同一批数据可能写两次；
- `max-concurrent-executions` 不是越大越好：并行尝试会争抢 slot、磁盘和网络，反而拖慢正常 task；
- 慢节点 blocklist 只影响后续调度，不会迁移已经在跑的 task。

判断是否值得开：先看 stage 内 task 时长的 P99/P50 比值和失败/重试记录。如果长尾来自数据倾斜，推测执行只是把倾斜再算一遍，应该先做 salting/两阶段聚合；如果长尾来自个别坏节点或磁盘抖动，推测执行更有效。

### 18.12.2 混合 shuffle：内存、本地盘与远程层

批 shuffle 可以把中间数据放在不同层级：

```yaml
execution.batch-shuffle-mode: ALL_EXCHANGES_HYBRID_FULL
# 常见取值还有 ALL_EXCHANGES_BLOCKING、ALL_EXCHANGES_PIPELINED、
# ALL_EXCHANGES_HYBRID_SELECTIVE
```

混合 shuffle 有两档落盘策略，不是简单的“内存优先”：

- `ALL_EXCHANGES_HYBRID_SELECTIVE`：只持久化下游未及时消费的数据，落盘量小，但失败时可能需要重启上游重算；
- `ALL_EXCHANGES_HYBRID_FULL`：持久化所有数据，失败后可直接重读完整中间结果，代价是更多磁盘/IO。

它同时允许下游在上游尚未完成时开始消费（类似流水线），又保留了阻塞 shuffle“上下游不必同时运行”的资源优势。shuffle 数据可以卸载到文件系统或远程 shuffle 服务：

```yaml
# 方式一：远程文件系统（OSS/HDFS/S3 等）
taskmanager.network.hybrid-shuffle.remote.path: s3://bucket/flink-shuffle

# 方式二：外部 remote tier（例如 Celeborn）
taskmanager.network.hybrid-shuffle.external-remote-tier-factory.class: <Celeborn 工厂类>
celeborn.master.endpoints: celeborn-master:9097
```

Apache Celeborn 是常见的远程 shuffle 层（FLIP-459，Flink 1.20+）。Flink 2.0 已移除旧的 hybrid shuffle legacy mode，2.x 只保留新实现。

注意两条 2.3 仍存在的限制：

- hybrid shuffle 仍是**实验特性**，不支持 slot sharing；每个 task 需要独占 slot，显式指定 slot sharing 会报错；
- 开启动态图/自动并行度时，AdaptiveBatch 会等上游完成才决定下游并行度，hybrid 实际退化为 blocking（`ALL_PRODUCERS_FINISHED`）。

推测执行与 hybrid shuffle 同时开启时，分区消费约束默认是 `ONLY_FINISHED_PRODUCERS`：比 blocking 多一些流水线机会，但也更容易产生额外的 speculative attempt 和更高的 failover 成本。要退回 blocking 行为，可把 `jobmanager.partition.hybrid.partition-data-consume-constraint` 设为 `ALL_PRODUCERS_FINISHED`；`UNFINISHED_PRODUCERS` 在推测执行下不支持。

适用场景：

- 大规模批作业，本地磁盘成为瓶颈；
- 节点磁盘容量/IO 不一致，shuffle 落盘导致长尾；
- 计算与存储分离，希望 shuffle 数据不绑定在 TaskManager 本地；
- 多租户集群，需要隔离 shuffle 的磁盘/网络占用。

代价：

- 多一次序列化/反序列化和远程传输；
- 远程 shuffle 服务本身需要高可用、容量规划和清理策略；
- 故障域变大：remote tier 故障会影响所有依赖它的作业；
- 运维复杂度上升，必须有压测和回退到本地 shuffle 的方案。

### 18.12.3 联合调优顺序

1. 先消除数据倾斜；
2. 再看 stage 的 shuffle bytes、spill、task 时长分布；
3. 如果长尾来自坏节点，开推测执行并确保 sink 幂等；
4. 如果本地磁盘是瓶颈，评估混合 shuffle/远程 tier；
5. 最后才调 `max-concurrent-executions`、内存和并行度。

推测执行和混合 shuffle 都是“用资源换时间/稳定性”的手段。它们不能修复错误的 join 策略、缺失的预聚合或严重的 key 倾斜；在这些问题存在时开启，只会让成本更高。

## 本章小结

流批一体的价值在于统一 API、SQL 和元数据，而不是让流和批变成同一件事。选择模式要看输入是否有界、延迟要求、状态成本和容错方式。

下一章进入双流 Join，这是流式 SQL 中最容易状态膨胀的部分。
