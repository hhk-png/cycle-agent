# 第8章 Checkpoint、Savepoint 与 Exactly-once

## 8.1 目标：一致的状态切面

流作业一直在处理数据。如果直接逐个算子复制状态，会出现：

```text
时间 →
source: 已发 1..100
算子 A: 处理到 80
算子 B: 处理到 60
```

复制下来的状态对应不同输入位置，恢复后要么丢数据，要么重复。Checkpoint 的目标是找到一个**全局一致的切面**：所有算子的状态都对应“处理完同一批输入”的那一刻。

```text
正确切面：source 100 / A 100 / B 100  （同一逻辑时刻）
错误切面：source 100 / A 80  / B 60   （各自为政）
```

Flink 用基于屏障（barrier）的分布式快照实现这一点，思想来自 Chandy-Lamport 算法。

## 8.2 屏障如何流动

JobManager 的 CheckpointCoordinator 周期性地让 source 注入一个编号为 `n` 的屏障：

```text
source ──记录──记录──[barrier n]──记录──>
          │
          └─ 算子收到 barrier 时，把当前状态快照到存储
             然后把 barrier 继续向下游转发
```

关键规则：

- 屏障像普通记录一样在通道中流动，保证“屏障之前的记录”一定先被处理；
- 单输入算子收到屏障，立即快照并转发；
- 多输入算子要等所有输入的屏障都到齐（**对齐**），再快照；
- 对齐期间，先到屏障的输入后面的记录会被缓冲，不能处理；
- 所有算子完成快照并确认后，CheckpointCoordinator 标记该 checkpoint 完成。

```text
input 1: --a--b--[B]--c--d-->
input 2: --x------y--[B]--z-->
                    ↑
             对齐后统一快照，c/d 缓冲
```

### 为什么需要对齐

假设算子 B 有两条输入：

```text
input 1: 1, 2, [B], 5, 6
input 2: 10, 20, 30, [B], 60
```

如果 B 收到 input 1 的屏障就立刻快照，但 input 2 还有 30 没处理，快照里 input 1 的状态已经包含 1、2，input 2 的状态只包含 10、20，恢复后 30 会被重复处理或丢失。对齐保证“屏障之前的所有输入都已处理”。

### 对齐的代价

在反压场景下，先到屏障的通道会被阻塞，后到屏障的通道还在慢慢消费。对齐时间可能很长，导致：

- checkpoint 超时；
- 屏障后的数据在缓冲中堆积，内存/磁盘压力上升；
- 吞吐下降。

这就是非对齐 checkpoint 要解决的问题。

## 8.3 对齐 vs 非对齐

### 对齐 checkpoint

优点：状态一致、实现成熟、快照体积小。
缺点：反压时对齐慢，可能超时。

### 非对齐 checkpoint（Unaligned Checkpoint）

收到第一个屏障时，不等待其他输入，而是把**当前所有通道中未处理的数据**连同状态一起写入快照，然后立即转发屏障。

```text
input 1: --a--b--[B]--c--d-->
input 2: --x------y--z--[B]-->

非对齐：收到 input 1 的 B 时，把 input 2 中 x/y/z 的 in-flight 数据一起快照
```

优点：跳过对齐等待，反压时也能快速完成 checkpoint。
代价：

- 快照包含 in-flight 数据，体积更大；
- 恢复时要重放这些数据；
- 对 source/sink 和状态后端有额外要求；
- 不是所有版本/连接器都支持。

配置：

```yaml
execution.checkpointing.unaligned.enabled: true
execution.checkpointing.aligned-checkpoint-timeout: 30s
```

选择原则：**先解决反压；非对齐是缓解手段，不是反压的解药**。如果反压来自下游 sink 或外部系统，非对齐只会把问题搬到恢复阶段。

### Changelog State Backend

另一种减少 checkpoint 开销的思路是 changelog：状态变更先写本地 changelog，checkpoint 只上传 changelog + 基础快照，恢复时重放 changelog。它适合写多、状态大的场景，但会增加本地磁盘和恢复复杂度。

```yaml
state.backend.changelog.enabled: true
state.backend.changelog.storage: filesystem
```

注意：changelog 与 ForSt 的兼容性有限，选型时要核对版本矩阵。

## 8.4 Exactly-once 的边界

Flink 的 checkpoint 能保证**作业内部状态**的 exactly-once。端到端 exactly-once 还需要：

1. source 可重放，且 offset 进入 checkpoint；
2. sink 支持事务或幂等写入；
3. checkpoint 完成与 sink 提交原子协调；
4. 外部系统支持 read_committed / 幂等键。

```text
source offset ─┐
operator state ├─ checkpoint n ──> 完成通知 ──> sink 提交事务 n
sink 事务状态  ┘
```

Kafka 的经典组合：

- KafkaSource 把 offset 存进 checkpoint；
- KafkaSink 使用两阶段提交（2PC）；
- checkpoint 完成时提交事务；
- 下游消费者设置 `isolation.level=read_committed`。

```java
KafkaSink<Order> sink = KafkaSink.<Order>builder()
    .setBootstrapServers("kafka:9092")
    .setRecordSerializer(new OrderSerializationSchema("orders-sink"))
    .setDeliveryGuarantee(DeliveryGuarantee.EXACTLY_ONCE)
    .setTransactionalIdPrefix("orders-sink-")
    .setProperty("transaction.timeout.ms", "900000")
    .build();
```

如果 sink 是普通 JDBC、Elasticsearch 或 HTTP 接口，通常只能做到 at-least-once，除非业务键幂等或自己实现事务表。

### “Exactly-once”到底保证什么

它保证的是**从 source 到 sink 的逻辑结果**在故障恢复后不重不漏，前提是：

- 外部系统配合事务/幂等；
- 用户逻辑是确定性的；
- 没有副作用绕过 checkpoint（例如在 `map` 里直接写数据库）。

如果用户在算子中直接调用外部 API，Flink 无法回滚这个副作用。这类操作必须放到支持事务的 sink，或设计成幂等。

## 8.5 关键配置

```yaml
execution.checkpointing.interval: 30s
execution.checkpointing.timeout: 10min
execution.checkpointing.min-pause: 10s
execution.checkpointing.max-concurrent-checkpoints: 1
execution.checkpointing.mode: EXACTLY_ONCE
execution.checkpointing.tolerable-failed-checkpoints: 3
execution.checkpointing.unaligned.enabled: false
state.checkpoints.dir: s3://bucket/checkpoints
state.savepoints.dir: s3://bucket/savepoints
state.backend.incremental: true
```

经验：

- 间隔太小 → 频繁快照，影响吞吐；
- 间隔太大 → 故障恢复时间长，丢数据窗口大；
- timeout 要覆盖大状态上传时间；
- `max-concurrent-checkpoints` 通常为 1，避免快照互相竞争；
- 失败阈值用于容忍偶发失败，但不能掩盖系统性问题；
- checkpoint 目录必须是高可靠、低延迟的共享存储；
- `min-pause` 防止 checkpoint 无缝衔接，给作业留出处理时间。

一个常用的起点：

```text
checkpoint 间隔 = 1～5 分钟（状态 < 10 GB）
               = 5～15 分钟（状态 10～100 GB）
               = 按 SLO 和恢复时间反推（状态 > 100 GB）
```

## 8.6 Savepoint

Savepoint 是**手动触发、格式稳定、用于运维**的 checkpoint：

```bash
flink savepoint <job-id> s3://bucket/savepoints
flink stop --savepointPath s3://bucket/savepoints <job-id>
flink run -s s3://bucket/savepoints/savepoint-xxx job.jar
```

区别：

| 维度 | Checkpoint | Savepoint |
|---|---|---|
| 触发 | 周期自动 | 手动/运维 |
| 生命周期 | 可被新 checkpoint 覆盖 | 长期保留 |
| 格式 | 可随版本优化 | 强调兼容性 |
| 用途 | 故障恢复 | 升级、迁移、扩缩容 |

改作业拓扑、升级 Flink、调整并行度前，先做 savepoint。恢复时要注意：

- 算子 uid 必须稳定；
- 状态 schema 必须兼容；
- 并行度变化需要 key group 重分配；
- 带 operator state 的算子受重分配模式限制（例如 `UnionListState` 的语义与普通 `ListState` 不同）；`union` 本身是无状态算子，不是“不能 rescale”的典型；
- 新算子如果没有默认状态，需要 `--allowNonRestoredState`（慎用）。

### Savepoint 的目录结构

```text
savepoint-<job-id>-<timestamp>/
├── _metadata
├── <uid>-<operator>/...
└── shared/...
```

`_metadata` 记录了算子 uid、状态句柄、并行度和 checkpoint 格式版本。不要手工编辑；需要修改时用 State Processor API。离线读写 savepoint、改 key、改并行度、修复坏状态的完整方法见[附录 L](../appendix/L-state-migration-spi.md)。

### 兼容性检查

升级前至少做三件事：

1. 在副本集群上用 savepoint 恢复新版本；
2. 对比关键状态大小和算子 uid；
3. 跑一批回放数据，核对输出。

## 8.7 Checkpoint 失败的常见原因

- 存储不可用、权限错误、S3 限流；
- 状态太大，超时；
- 反压导致对齐时间过长；
- 某个 subtask 持续失败；
- 序列化异常；
- TaskManager 磁盘满（RocksDB 本地目录）；
- checkpoint 并发过多；
- 网络抖动导致确认丢失；
- 用户函数在 snapshot 期间阻塞。

排查顺序：

1. 看 Checkpoint 页面：duration、size、alignment、failure reason；
2. 看反压和 GC；
3. 看磁盘和对象存储指标；
4. 看是否有 subtask 反复失败；
5. 最后才考虑调大 timeout 或换非对齐。

不要一上来就调大 timeout。那通常只是把失败推迟，没有解决根因。

## 8.8 2.x 的变化

Flink 2.0 的分离式状态管理（ForSt）试图改变 checkpoint 的成本结构：

- 状态持续写到远程 DFS，checkpoint 只需记录一个逻辑切面；
- 本地盘作为缓存，恢复时不必全量下载；
- 异步状态访问减少同步阻塞。

但 2.3 的 ForSt 仍是实验特性，且与 1.x 状态不保证兼容。生产上：

- 大状态仍优先 RocksDB；
- 升级 2.x 前必须做 savepoint 恢复演练；
- 不要假设“2.0 的 checkpoint 一定更快”，要看具体状态后端和存储。

## 8.9 MiniFlink 的实现

MiniFlink 不实现屏障，而是在微批边界做**同步一致快照**：

1. 每个 source 读取最多 `micro_batch_records` 条；
2. 调度器排空所有通道；
3. 所有 task 处于静止状态；
4. 依次快照 source offset、算子状态、计时器、sink 状态；
5. 写入内存或文件。

```python
env.enable_checkpointing(10_000, micro_batch_records=50)
execution = env.create_execution()
execution.run(max_rounds=1)
checkpoint = execution.checkpoint()
restored = env.create_execution().restore(checkpoint)
restored.run()
```

这保证了“快照对应同一批输入”，但会暂停处理，不适合生产。它帮助读者理解 checkpoint 的本质，而不是替代 Flink 的分布式快照。

MiniFlink 的 `CheckpointCoordinator` 还实现了：

- checkpoint id 递增；
- 存储后端抽象（内存/文件）；
- 完成/失败统计；
- 恢复时校验任务 id 是否齐全。

## 8.10 Checkpoint 的生命周期与诊断

理解 checkpoint 的最好方式，是把它拆成一条有明确阶段的流水线。任何一个阶段变慢，最终都表现为“checkpoint 超时”，但根因和处置完全不同。

```text
1. 触发        JM 的 CheckpointCoordinator 按间隔发起 checkpoint n
2. 注入        source 在数据流中插入 barrier n（可能先做一次对齐/快照准备）
3. 对齐        多输入算子等待所有通道的 barrier n 到齐（非对齐则跳过）
4. 本地快照    task 线程冻结状态视图，生成状态句柄（RocksDB 为 SST 引用）
5. 持久化      异步上传/复制到 checkpoint 存储（全量或增量）
6. 确认        所有 task 回报成功，Coordinator 汇总
7. 完成        写入 _metadata，标记 checkpoint n 为 completed
8. 通知        sink 的 Committer 收到完成通知，提交事务
```

### 每个阶段在做什么

- **触发**：间隔太短会让 checkpoint 互相竞争；`min-pause` 用来给处理留出时间。
- **注入**：source 必须把 offset/分片位置纳入同一快照，否则恢复后无法对齐输入。
- **对齐**：只有多输入算子需要；单输入算子收到 barrier 即可快照。反压时对齐可能非常慢。
- **本地快照**：并不是“把状态复制一份”，而是生成一个可持久化的视图。RocksDB 增量快照主要记录新产生的 SST 文件；HashMap 则要遍历堆内对象。
- **持久化**：对象存储的带宽、限流、权限、DNS 都可能在这里出问题。
- **确认/完成**：coordinator 只认“所有 task 都确认”的 checkpoint；未完成的 checkpoint 不能用于恢复。
- **通知**：Kafka/文件/湖仓 sink 的两阶段提交在这里发生；提交失败会重试，可能造成部分 sink 已提交、部分未提交的短暂不一致。

### 全量、增量与分离式状态

| 方式 | 快照内容 | 优点 | 代价 |
|---|---|---|---|
| 全量 | 全部状态 | 简单、恢复直接 | 上传量大、随状态线性变慢 |
| RocksDB 增量 | 新增/变化的 SST + 共享文件引用 | 上传量小 | 恢复要读更多文件，依赖 compaction 状态 |
| ForSt | 远程状态 + 逻辑切面 | 快照轻、恢复快 | 2.3 仍实验，限制多 |

增量 checkpoint 不是“免费”：它把成本从“每次上传全量”转移到“恢复时读取大量共享文件”和“本地 compaction”。状态越大、更新越随机，增量快照的共享文件越多，恢复越可能成为瓶颈。

### 非对齐 checkpoint 的适用边界

非对齐 checkpoint 在收到第一个 barrier 时，把通道中的 in-flight 数据一起写入快照，从而跳过对齐等待。它适合：

- 反压严重、对齐时间远大于快照时间；
- 状态后端/连接器支持 channel state；
- 能接受更大的快照和恢复时的重放。

它不适合：

- 反压来自 sink 或外部系统（非对齐只是把问题搬到恢复阶段）；
- 连接器不支持 in-flight 数据恢复；
- 快照存储带宽有限（in-flight 数据会放大上传量）。

### 诊断：从指标到根因

先看 Checkpoint 页面和指标：

```text
lastCheckpointDuration        总时长
lastCheckpointSize            快照大小
lastCheckpointAlignmentTime   对齐耗时
numberOfFailedCheckpoints     失败次数
```

不同版本还会暴露 full size、processed/persisted data、restore timestamp 等指标；以所用版本的指标列表为准。

再把 duration 拆开：

```text
duration ≈ 对齐时间 + 本地快照时间 + 上传时间 + 确认/重试时间
```

| 现象 | 更像是 | 先做什么 |
|---|---|---|
| alignment 很大，duration 大 | 反压/慢输入 | 找反压根因；评估非对齐 |
| alignment 小，snapshot 大 | 状态大/序列化慢 | 减状态、TTL、增量、换后端 |
| snapshot 小，upload 慢 | 存储带宽/限流/权限 | 看对象存储指标、换存储/分区 |
| 固定某个 subtask 失败 | 倾斜/磁盘满/OOM | 看该 subtask 的状态、磁盘、GC |
| 失败率随流量上升 | 资源竞争 | 看 CPU/网络/checkpoint 并发 |
| 恢复后很快再次失败 | 状态不兼容/外部依赖 | 看恢复日志、sink 事务、连接器 |

### 一个诊断案例

现象：状态 200 GB，checkpoint 间隔 5 分钟，duration 从 2 分钟涨到 25 分钟，频繁超时。

拆解：

```text
alignment ≈ 1 min        → 有反压，但不是主因
snapshot ≈ 3 min         → 状态遍历/序列化
upload ≈ 20 min          → 200 GB 全量上传，带宽约 170 MB/s
```

结论：全量快照 + 对象存储带宽是主因，反压是促成因素。处置顺序：

1. 开启 RocksDB 增量 checkpoint，把上传量降到变化量；
2. 给状态设 TTL/减少窗口重叠，降低基线和变化率；
3. 检查 compaction 是否跟得上，避免“增量快照引用大量小文件”；
4. 评估本地备份/本地恢复，缩短 RTO；
5. 如果反压仍严重，再评估非对齐；
6. 最后才考虑调大 timeout/间隔——那只是把失败推迟。

### 2.3 的恢复期 checkpoint

大状态作业在恢复期间，通常要先把 in-flight 通道数据消费完才能做下一次 checkpoint，这段窗口可能很长。FLIP-547 引入“恢复期 checkpoint”，默认关闭，需要同时打开：

```yaml
execution.checkpointing.unaligned.recover-output-on-downstream.enabled: true
execution.checkpointing.unaligned.during-recovery.enabled: true
```

它缩短了“恢复期间再次故障”的暴露窗口，代价是恢复流量和快照流量同时争抢资源。只对非对齐 checkpoint 路径有意义；普通对齐 checkpoint 不需要。

### 诊断清单

- [ ] 先看失败原因和失败 subtask，不要先调 timeout；
- [ ] 把 duration 拆成对齐/快照/上传/确认；
- [ ] 区分状态大、存储慢、反压、倾斜、磁盘满；
- [ ] 增量快照是否开启？恢复时间是否测过？
- [ ] checkpoint 存储是否与业务流量争抢网络？
- [ ] sink 提交是否拖慢完成通知？
- [ ] 恢复期 checkpoint 是否需要在 2.3 上评估？

## 8.11 生产检查清单

- [ ] checkpoint 间隔与状态大小、恢复时间匹配；
- [ ] checkpoint 存储是高可靠、低延迟的共享存储；
- [ ] `max-concurrent-checkpoints=1`，`min-pause` 合理；
- [ ] 大状态开启增量 checkpoint；
- [ ] 反压严重时评估非对齐 checkpoint；
- [ ] source offset 进入 checkpoint，sink 使用 2PC 或幂等；
- [ ] savepoint 目录长期保留，定期做恢复演练；
- [ ] 监控 checkpoint duration、size、alignment、失败率；
- [ ] 能把 duration 拆成对齐/快照/上传/确认四段；
- [ ] 升级/扩缩容前做 savepoint 兼容性测试。

## 8.12 Checkpoint 存储的内部结构与保留策略

前面讲的是“checkpoint 怎么发生”，这一节讲“checkpoint 落到哪里、保留多久、恢复时选哪一个”。很多“checkpoint 成功但恢复失败”“存储费用暴涨”“旧快照被误删”的问题，都出在这一层。

### 8.12.1 两种 CheckpointStorage

Flink 用 `CheckpointStorage` 抽象快照的持久化位置：

| 实现 | 状态放哪 | 适用 | 风险 |
|---|---|---|---|
| `JobManagerCheckpointStorage` | JobManager 堆内存 | 本地测试、极小状态 | JM OOM、重启即丢 |
| `FileSystemCheckpointStorage` | 文件系统/对象存储 | 生产 | 存储带宽、权限、一致性 |

配置 `state.checkpoints.dir` 后通常使用文件系统实现：

```yaml
state.checkpoints.dir: s3://bucket/flink/checkpoints
state.savepoints.dir: s3://bucket/flink/savepoints
```

没有配置目录时，Flink 可能退化为 JM 堆存储。它只适合本地实验：状态稍大就会把 JobManager 打爆，而且 JM 重启后快照消失。生产必须用共享的持久存储（HDFS、S3、OSS、GCS、PVC），并确保 JobManager 和所有 TaskManager 都能访问。

### 8.12.2 目录结构

一次 checkpoint 的典型布局：

```text
s3://bucket/flink/checkpoints/
└── <job-id>/
    ├── chk-1/
    │   ├── _metadata
    │   ├── <task-id>/...          # task-owned 状态
    │   └── shared/...             # 可被多个 checkpoint 共享的状态文件
    ├── chk-2/
    │   ├── _metadata
    │   ├── <task-id>/...
    │   └── shared/...
    └── ...
```

Savepoint 的布局类似，但目录名通常是 `savepoint-<job-id>-<timestamp>/`，并且由用户显式创建/删除。

`_metadata` 记录：

- checkpoint id、类型（CHECKPOINT/SAVEPOINT/FULL_CHECKPOINT）、时间戳；
- 每个算子的状态句柄、状态大小、keyed/operator state 的元数据；
- 并行度、maxParallelism、状态后端信息；
- 恢复时需要的 master state（例如 checkpoint 协调器状态）；
- 外部路径（externalized checkpoint/savepoint）。

不要手工编辑 `_metadata`。需要修改状态时用 State Processor API（附录 L），需要清理时用 Flink CLI 或生命周期规则。

### 8.12.3 全量、增量与共享文件

| 模式 | 写入内容 | 恢复 | 存储成本 |
|---|---|---|---|
| 全量 | 每个 checkpoint 一份完整状态 | 直接读 | 随保留数线性增长 |
| RocksDB 增量 | 新增/变化的 SST + 对旧文件的引用 | 读多个共享文件 | 低于全量，但共享文件要保留 |
| ForSt | 远程状态的逻辑切面 | 依赖远程存储 | 2.3 仍实验 |

增量 checkpoint 的关键是**共享文件引用计数**：

- 新 checkpoint 只上传变化的 SST；
- 旧 checkpoint 仍引用未变化的文件；
- 清理旧 checkpoint 时，不能删除仍被其他 checkpoint 引用的共享文件；
- 如果清理逻辑/存储生命周期规则误删共享文件，恢复会失败。

对象存储的生命周期规则（例如“30 天后删除”）如果按对象最后修改时间删除，可能删掉仍被引用的共享文件。生产上要么让 Flink 自己管理保留，要么把生命周期规则设得比 `state.checkpoints.num-retained` 更保守，并做恢复演练。

### 8.12.4 保留策略

```yaml
# 保留最近多少个 completed checkpoint
state.checkpoints.num-retained: 3

# 取消作业时是否保留外部化 checkpoint
execution.checkpointing.externalized-checkpoint-retention: RETAIN_ON_CANCELLATION
```

保留策略要同时考虑：

- **RPO**：保留太少，最近 checkpoint 损坏时没有退路；
- **存储成本**：全量快照 × 保留数可能非常贵；
- **恢复选择**：不是所有 checkpoint 都能恢复（可能状态不兼容/文件缺失）；
- **合规**：savepoint 可能包含敏感数据，保留期要符合数据治理要求。

`CompletedCheckpointStore` 负责记录“哪些 checkpoint 已完成”：

- 默认实现把完成记录放在 checkpoint 存储中，JM 重启后可恢复；
- 嵌入式实现把记录放在状态后端中，适合特定部署；
- 无论哪种，**未完成的 checkpoint 不能用于恢复**。

### 8.12.5 本地备份与本地恢复

```yaml
# 写 checkpoint 时在 TM 本地留一份备份
execution.checkpointing.local-backup.enabled: true

# 恢复时优先用本地备份
execution.state-recovery.from-local: true
```

本地备份能显著缩短大状态作业的恢复时间，但：

- 依赖 task 被调度回同一节点/同一本地盘；
- Kubernetes 上 Pod 漂移会让本地备份失效；
- 本地盘容量要预留，备份不是“免费的副本”；
- 远程 checkpoint 仍是最终真相，本地备份只是加速。

### 8.12.6 恢复时选哪个快照

恢复来源有三类：

1. **最近完成的 checkpoint**：自动恢复的默认行为；
2. **指定 checkpoint/savepoint**：`flink run -s <path>` 或 `execution.savepoint.path`；
3. **State Processor API 生成的新 savepoint**：离线修复/迁移后使用（附录 L）。

选择时要看：

- checkpoint 是否 completed；
- 状态后端/序列化器是否兼容；
- maxParallelism 是否匹配；
- 文件是否完整（共享文件、权限、对象存储一致性）；
- 是否有更新的 savepoint 可用。

“最新”不等于“最合适”。升级/迁移时，通常选一个显式创建、经过校验的 savepoint，而不是自动恢复时抓到的最近 checkpoint。

### 8.12.7 存储选型与成本

| 存储 | 优点 | 注意 |
|---|---|---|
| HDFS | 成熟、顺序写快 | 运维成本、NameNode 压力 |
| S3/OSS/GCS | 弹性、免运维 | 请求费用、带宽、一致性/权限 |
| PVC | 低延迟、可控 | 容量/可用区、Pod 漂移 |
| 本地盘 | 最快 | 不持久，只能做本地备份 |

成本不只是“每 GB 单价”：

- PUT/GET 请求数（增量快照会产生大量小文件）；
- 跨区/跨云流量；
- 保留多份全量快照；
- 恢复时的下载带宽；
- 生命周期规则误删导致的重跑成本。

大状态作业要把“checkpoint 存储”当成一个独立的容量项做预算，而不是附属于计算资源。第 26 章的成本模型会把这部分拆开。

### 8.12.8 检查清单

- [ ] `state.checkpoints.dir` 指向共享持久存储，不是 JM 本地盘；
- [ ] `state.savepoints.dir` 独立配置并长期保留；
- [ ] 保留数与存储成本、RPO 匹配；
- [ ] 增量快照的共享文件不会被生命周期规则误删；
- [ ] 恢复演练覆盖“最近 checkpoint”和“指定 savepoint”两条路径；
- [ ] 本地备份/本地恢复的节点亲和与容量已评估；
- [ ] 对象存储的权限、限流、跨区延迟有监控；
- [ ] 不手工编辑 `_metadata`，状态修改走 State Processor API。

## 本章小结

Checkpoint 是 Flink 状态一致性的基石：屏障划出全局切面，状态后端负责快照，source/sink 决定端到端语义。生产上要区分“作业内部 exactly-once”和“端到端 exactly-once”，不要被名词误导。

下一章讨论失败之后如何恢复：重启策略、局部恢复、扩缩容、状态演进，以及恢复过程中最容易踩的坑。
