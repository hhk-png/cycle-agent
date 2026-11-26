# 附录 H Flink 源码阅读与二次开发地图

> 版本基线：Flink 2.3。源码包名/类名会随版本演进；本附录给的是“按问题找代码”的路线，不是逐行注释。读源码前先跑通第 28～33 章的 MiniFlink，建立运行时直觉。

## H.1 仓库结构

Flink 主仓库是一个多模块 Maven 工程。和流处理最相关的模块：

```text
flink-core                 基础类型、配置、序列化、内存抽象
flink-runtime              调度、ExecutionGraph、checkpoint、网络、状态后端接口
flink-streaming-java       DataStream API、StreamTask、算子、watermark、窗口
flink-clients              客户端、提交、REST
flink-table                 Table API/SQL、planner、runtime、codegen
flink-connectors           文件、Kafka 等连接器（部分已独立仓库）
flink-state-backends       RocksDB/ForSt 等状态后端
flink-kubernetes-operator  独立仓库
flink-cdc                  独立仓库
flink-agents               独立子项目
flink-ml                   机器学习（视版本）
```

注意：Kafka connector、CDC、Kubernetes Operator、Agents 可能是独立仓库/独立版本节奏。不要在主仓库里找所有东西。

## H.2 从 API 到执行图

### 用户 API

```text
StreamExecutionEnvironment
  └── DataStream / KeyedStream / WindowedStream
        └── Transformation（OneInput/TwoInput/Source/Sink）
```

关键类：

- `org.apache.flink.streaming.api.environment.StreamExecutionEnvironment`
- `org.apache.flink.streaming.api.datastream.DataStream`
- `org.apache.flink.api.dag.Transformation`
- `org.apache.flink.streaming.api.operators.StreamOperator`

`DataStream` 的每次转换都会追加一个 `Transformation`；`env.execute()` 才触发图构建。

### StreamGraph

```text
Transformation DAG
  → StreamGraphGenerator
  → StreamGraph（逻辑拓扑）
  → StreamingJobGraphGenerator
  → JobGraph（算子链、分区、slot sharing）
```

关键类：

- `org.apache.flink.streaming.api.graph.StreamGraphGenerator`
- `org.apache.flink.streaming.api.graph.StreamGraph`
- `org.apache.flink.streaming.api.graph.StreamingJobGraphGenerator`
- `org.apache.flink.runtime.jobgraph.JobGraph`

读这段代码可以回答：

- 算子链是怎么合并的？
- `keyBy` 生成什么分区器？
- slot sharing group 怎么确定？
- uid 是怎么生成的？

### ExecutionGraph 与调度

```text
JobGraph
  → ExecutionGraph
  → ExecutionJobVertex / ExecutionVertex / Execution
  → Scheduler（Default/Adaptive）
  → SlotPool / ResourceManager
```

关键类：

- `org.apache.flink.runtime.executiongraph.ExecutionGraph`
- `org.apache.flink.runtime.executiongraph.ExecutionJobVertex`
- `org.apache.flink.runtime.executiongraph.ExecutionVertex`
- `org.apache.flink.runtime.scheduler.SchedulerBase`
- `org.apache.flink.runtime.scheduler.DefaultScheduler`
- `org.apache.flink.runtime.scheduler.adaptive.AdaptiveScheduler`
- `org.apache.flink.runtime.scheduler.adaptivebatch.AdaptiveBatchScheduler`
- `org.apache.flink.runtime.scheduler.adaptivebatch.DefaultSpeculativeExecutionHandler`
- `org.apache.flink.runtime.scheduler.slowtaskdetector.ExecutionTimeBasedSlowTaskDetector`
- `org.apache.flink.runtime.io.network.partition.hybrid.*`（hybrid shuffle）
- `org.apache.flink.runtime.jobmaster.slotpool.SlotPool`

读这段可以回答：

- 失败后为什么重启整个 region/整个作业？
- slot 怎么申请/释放？
- 自适应调度器怎么扩缩容？
- 并行度变化后状态怎么重分配？
- 慢 task 什么时候触发 speculative attempt？blocklist 怎么生效？
- hybrid shuffle 的分区在内存/磁盘/远程层之间怎么流转？

## H.3 状态与 checkpoint

### 状态后端

```text
StateBackend
  ├── HashMapStateBackend
  ├── EmbeddedRocksDBStateBackend
  └── ForStStateBackend（实验）
```

关键类：

- `org.apache.flink.runtime.state.StateBackend`
- `org.apache.flink.runtime.state.AbstractKeyedStateBackend`
- `org.apache.flink.runtime.state.KeyedStateBackend`
- `org.apache.flink.runtime.state.KeyGroupRange`
- `org.apache.flink.state.rocksdb.EmbeddedRocksDBStateBackend`
- `org.apache.flink.runtime.state.storage.FileSystemCheckpointStorage`

读这段可以回答：

- keyed state 怎么按 key group 分区？
- RocksDB 的列族/序列化怎么组织？
- 增量 checkpoint 的 SST 文件怎么跟踪？
- TTL 在哪个层次实现？

### Checkpoint 协议

```text
CheckpointCoordinator
  → 触发 source barrier
  → CheckpointBarrierHandler（对齐/非对齐）
  → StreamTask 快照
  → 确认/完成
  → CheckpointStorage 持久化
```

关键类：

- `org.apache.flink.runtime.checkpoint.CheckpointCoordinator`
- `org.apache.flink.runtime.checkpoint.CheckpointBarrier`
- `org.apache.flink.runtime.checkpoint.CheckpointBarrierHandler`
- `org.apache.flink.runtime.io.network.partition.consumer.CheckpointedInputGate`
- `org.apache.flink.runtime.state.SnapshotStrategy`
- `org.apache.flink.runtime.state.CheckpointStorage`

读这段可以回答：

- 对齐期间数据缓存在哪？
- 非对齐 checkpoint 怎么把 in-flight 数据写入快照？
- checkpoint 完成通知怎么触发 sink 提交？
- 恢复时状态怎么分配到 subtask？

### 定时器与 watermark

关键类：

- `org.apache.flink.streaming.api.operators.InternalTimerService`
- `org.apache.flink.streaming.api.operators.InternalTimer`
- `org.apache.flink.streaming.runtime.watermarkstatus.StatusWatermarkValve`
- `org.apache.flink.streaming.api.watermark.Watermark`
- `org.apache.flink.streaming.runtime.operators.windowing.WindowOperator`

读这段可以回答：

- 多输入 watermark 为什么取最小？
- idle 分区怎么被排除？
- 事件时间定时器什么时候触发？
- 窗口状态什么时候清理？

## H.4 运行时与网络

关键类：

- `org.apache.flink.streaming.runtime.tasks.StreamTask`
- `org.apache.flink.streaming.runtime.tasks.mailbox.MailboxProcessor`
- `org.apache.flink.streaming.runtime.io.StreamTaskInput`
- `org.apache.flink.streaming.runtime.io.RecordWriter`
- `org.apache.flink.runtime.io.network.partition.consumer.InputGate`
- `org.apache.flink.runtime.io.network.buffer.BufferPool`
- `org.apache.flink.runtime.io.network.netty.NettyClient/NettyServer`

读这段可以回答：

- 为什么算子线程是 mailbox 模型？
- credit-based 流控怎么工作？
- 反压指标从哪里来？
- buffer timeout/debloating 怎么影响延迟？

## H.5 Table/SQL

```text
SQL text
  → Calcite parser
  → SqlNode / RelNode
  → Flink logical plan
  → optimizer rules
  → physical plan（StreamPhysicalRel / BatchPhysicalRel）
  → ExecNode
  → CodeGenerator / operator
```

关键类/包：

- `org.apache.flink.table.planner.delegation.PlannerBase`
- `org.apache.flink.table.planner.plan.nodes.FlinkRelNode`
- `org.apache.flink.table.planner.plan.nodes.logical.*`
- `org.apache.flink.table.planner.plan.nodes.physical.stream.*`
- `org.apache.flink.table.planner.optimizer.*`
- `org.apache.flink.table.runtime.operators.*`
- `org.apache.flink.table.api.*`
- CTAS/RTAS：`SqlCreateTableAs`（parser）→ `CreateTableASOperation`（api）→ `TableEnvironmentImpl` 的 atomic/non-atomic 分支 → `StagedTable` / `SupportsStaging`（catalog/connector）
- SQL Gateway/Client 的语句解析与提交：`org.apache.flink.table.gateway.*`、`org.apache.flink.table.client.*`

读这段可以回答：

- 窗口 TVF 怎么变成算子？
- 聚合为什么拆成 local/global？
- join 怎么选策略？
- changelog 的 RowKind 怎么传播？
- `EXPLAIN` 的计划节点对应哪个算子？

调试 SQL 的第一工具是：

```sql
EXPLAIN SELECT ...;
EXPLAIN CHANGELOG SELECT ...;
```

## H.6 Source/Sink V2

Source：

```text
Source
  ├── SplitEnumerator（发现/分配分片）
  └── SourceReader（读取/水位线/offset）
```

关键类：

- `org.apache.flink.api.connector.source.Source`
- `org.apache.flink.api.connector.source.SplitEnumerator`
- `org.apache.flink.api.connector.source.SourceReader`
- `org.apache.flink.connector.kafka.source.KafkaSource`

Sink V2：

- `org.apache.flink.api.connector.sink2.Sink`
- `org.apache.flink.api.connector.sink2.SinkWriter`
- `org.apache.flink.connector.kafka.sink.KafkaSink`

读这段可以回答：

- 分区发现/分片分配怎么做？
- offset 怎么进 checkpoint？
- 2PC sink 怎么提交/回滚？
- watermark 在 source 侧怎么生成？

## H.7 DataStream API V2

包：

- `org.apache.flink.datastream.api.stream.DataStream`
- `org.apache.flink.datastream.api.stream.KeyedPartitionStream`
- `org.apache.flink.datastream.api.stream.NonKeyedPartitionStream`
- `org.apache.flink.datastream.api.stream.GlobalStream`
- `org.apache.flink.datastream.api.stream.BroadcastStream`
- `org.apache.flink.datastream.api.function.*`
- `org.apache.flink.datastream.api.state.*`

实现模块：`flink-datastream`。

阅读顺序：

1. `DataStream`/`Partitioning` 的接口；
2. ProcessFunction 类型层次；
3. StateDeclaration/StateManager；
4. 实现层的 `ProcessOperator`；
5. 与 V1 的 `StreamOperator` 的差异。

## H.8 按问题找代码

| 问题 | 入口 |
|---|---|
| 窗口不触发 | `WindowOperator`、`StatusWatermarkValve`、`InternalTimerService` |
| checkpoint 超时 | `CheckpointCoordinator`、`CheckpointBarrierHandler`、`SnapshotStrategy` |
| 状态太大 | `KeyedStateBackend`、`RocksDBKeyedStateBackend`、TTL |
| 反压 | `InputGate`、`BufferPool`、`RecordWriter`、credit-based 流控 |
| 扩缩容失败 | `KeyGroupRange`、`StateAssignmentOperation`、`ExecutionGraph` |
| SQL 计划不对 | `PlannerBase`、optimizer rules、`EXPLAIN` |
| 连接器 offset 不对 | `SourceReader`、`SplitEnumerator`、checkpoint |
| K8s 部署问题 | Flink Kubernetes Operator 仓库 |

## H.9 调试技巧

- 开发期关闭算子链，便于火焰图定位：`pipeline.operator-chaining: false`；
- 用 `jstack`/`jcmd` 看线程阻塞；
- 用 async-profiler 看 CPU/alloc/lock；
- 用 `EXPLAIN` 看 SQL 计划；
- 用 MiniCluster/TestHarness 做最小复现；
- 用 `flink list`/REST 看 checkpoint、反压、watermark；
- 对比“同一输入”的 MiniFlink 与 Flink 输出；
- 保留失败现场的日志、指标、线程栈、checkpoint 元数据。

## H.10 二次开发的风险

- **内部 API 不稳定**：`org.apache.flink.runtime.*` 很多不是公共 API，升级易碎；
- **shaded 依赖**：Flink 对 Guava/Jackson/Netty 等做了 shade，连接器依赖冲突常见；
- **序列化兼容**：自定义 serializer 要考虑 schema 演进和 savepoint；
- **状态布局**：自定义 operator 的状态一旦上线，改动要考虑恢复；
- **测试成本**：MiniCluster 测试比单元测试慢，但更接近真实；
- **许可/供应链**：连接器/依赖的许可证和安全扫描。

建议：

- 优先用公共 API（`@Public`/`@PublicEvolving`）；
- 把内部 API 封装在少量适配层；
- 给自定义 operator 固定 uid；
- 写恢复测试（savepoint → restore）；
- 记录版本矩阵和兼容性。

## H.11 MiniFlink 的映射

| Flink 源码 | MiniFlink |
|---|---|
| `StreamExecutionEnvironment` | `StreamExecutionEnvironment` |
| `StreamGraph/JobGraph` | `JobGraph`/`Node` |
| `ExecutionGraph/ExecutionVertex` | `RuntimeTask` |
| `CheckpointCoordinator` | `CheckpointCoordinator` |
| `KeyedStateBackend` | `KeyedStateBackend` |
| `WindowOperator` | `WindowOperator` |
| `StatusWatermarkValve` | `_recompute_watermark` |
| `StreamTask` | `RuntimeTask` 调度循环 |
| Table planner | SQLite 子集 |

MiniFlink 是“去掉分布式/网络/类型系统后的骨架”。读 Flink 源码时，先找对应的骨架，再看分布式细节。

## H.12 练习

1. 画出一个 `source → map → keyBy → window → sink` 作业的 StreamGraph/JobGraph/ExecutionGraph；
2. 找到 `keyBy` 生成分区器的代码；
3. 找到 checkpoint barrier 从 source 到 sink 的路径；
4. 找到窗口状态清理的触发点；
5. 用 `EXPLAIN` 看一个窗口聚合的物理计划；
6. 给 MiniFlink 加一个与 Flink 对应的指标，观察差异；
7. 读一个 FLIP，写出“设计动机/API/状态/兼容性”四段笔记。
