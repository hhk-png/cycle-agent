# 第33章 从 MiniFlink 到生产 Flink

## 33.1 已经对齐的部分

MiniFlink 和 Flink 在以下概念上一致：

- DataStream/KeyedStream/WindowedStream 的链式 API；
- 事件时间、watermark、多输入最小值；
- 滚动/滑动/会话/全局窗口；
- 键控状态、TTL、事件/处理时间定时器；
- 侧输出、广播状态、CEP 序列；
- source offset 与状态同快照；
- checkpoint 存储与恢复；
- 窗口/连接器/指标的基本形状。

这些是理解 Flink 的“骨架”。有了它们，再看 Flink 的 Java 类名，不会迷失在 API 细节中。

```text
MiniFlink 能回答：
- keyBy 之后状态为什么是 per-key？
- watermark 为什么要取最小？
- 窗口什么时候清理？
- checkpoint 为什么必须包含 offset？
- 恢复后为什么会重放？
```

## 33.2 还差什么

### 分布式执行

MiniFlink 是单进程任务图，Flink 是 JM/TM、slot、网络栈、反压、region failover。差异包括：

- 真正的 subtask 分布与资源调度；
- 网络缓冲、序列化、credit-based 流控；
- 算子链与 slot sharing；
- 任务失败检测、重启、region failover；
- JobManager HA。

### 类型与序列化

MiniFlink 直接传 Python 对象；Flink 有 TypeInformation、POJO、Kryo、Avro/Protobuf、schema 演进。生产上状态兼容性、跨语言、性能都依赖这一层。

### 检查点协议

MiniFlink 是微批同步快照；Flink 是屏障对齐/非对齐分布式快照，checkpoint 期间继续处理。Flink 还要处理：

- checkpoint 并发、超时、失败容忍；
- 增量 RocksDB 快照；
- savepoint 兼容性；
- 端到端 2PC；
- 2.x 的异步状态/ForSt。

### SQL 引擎

MiniFlink 是 SQLite 有界表；Flink 是 Calcite 解析、逻辑/物理计划、优化器、changelog、窗口 TVF、UDF、Catalog、SQL Gateway。两者复杂度不在一个量级。

### 连接器生态

MiniFlink 只有集合/文件/内存 Kafka/SQLite；Flink 有 Kafka、Pulsar、CDC、湖仓、JDBC、Elasticsearch、云服务，且每个连接器都有自己的并行度、offset、事务、schema 细节。

### 运维

MiniFlink 有指标抽象与内存 reporter，但没有外部 reporter/历史存储、日志体系、REST、K8s Operator、安全、多租户、容量管理。生产平台的价值很大一部分在这里。

## 33.3 类名映射

| MiniFlink | Flink |
|---|---|
| `StreamExecutionEnvironment` | 同名 |
| `DataStream` | 同名 |
| `KeyedStream` | 同名 |
| `WindowedStream` | 同名 |
| `WindowAssigner` | `WindowAssigner` |
| `Trigger` | `Trigger` |
| `Evictor` | `Evictor` |
| `AggregateFunction` | `AggregateFunction` |
| `KeyedProcessFunction` | 同名 |
| `TimerService` | 同名 |
| `ValueState` | 同名 |
| `StateTtlConfig` | 同名 |
| `CheckpointStorage` | `CheckpointStorage` |
| `CheckpointCoordinator` | 同名 |
| `RestartStrategy` | `RestartStrategy` |
| `SourceOperator` | `Source` / `SourceReader` |
| `SinkOperator` | `Sink` / `SinkWriter` |
| `TableEnvironment` | 同名 |
| `Pattern` | `Pattern` |
| `CepOperator` | `CepOperator` |
| `LocalCluster` | `MiniCluster` |

## 33.4 如何继续读 Flink 源码

建议路线：

1. **API 层**：`DataStream`、`KeyedStream`、`WindowedStream`、`ProcessFunction`；
2. **图**：`StreamGraph`、`JobGraph`、`ExecutionGraph`；
3. **运行时**：`Task`、`StreamTask`、`StreamOperator`、`Mailbox`；
4. **网络**：`NetworkBufferPool`、`CreditBasedPartitionRequestClientHandler`；
5. **状态**：`KeyedStateBackend`、`RocksDBKeyedStateBackend`、`StateSnapshot`；
6. **checkpoint**：`CheckpointCoordinator`、`Barrier`、`SnapshotStrategy`；
7. **SQL**：`TableEnvironment`、`Planner`、`Optimizer`、`ExecNode`；
8. **连接器**：`Source`、`Sink`、`SplitEnumerator`、`SinkWriter`。

读源码时带着问题：

- 这个类对应 MiniFlink 的哪个模块？
- 状态存在哪里？谁负责快照？
- 失败后谁触发恢复？
- 时间/watermark 如何传播？
- 并发/线程模型是什么？

## 33.5 生产就绪清单

从 MiniFlink 走向生产，需要补齐：

- [ ] 分布式资源调度与 HA；
- [ ] 类型系统与 schema 演进；
- [ ] 屏障/非对齐 checkpoint；
- [ ] 增量快照与 savepoint；
- [ ] 端到端 2PC/幂等 sink；
- [ ] 反压与网络调优；
- [ ] 指标/日志/trace；
- [ ] 安全与多租户；
- [ ] K8s Operator/GitOps；
- [ ] 容量规划与成本；
- [ ] 故障演练与 runbook。

## 33.6 练习路线

1. **扩展 MiniFlink**：
   - 加 key group/rescale；
   - 加 barrier 式 checkpoint；
   - 加 watermark idleness；
   - 加两阶段提交 sink；
   - 加窗口 TVF。

2. **对照 Flink**：
   - 用 Flink 官方示例跑同样的逻辑；
   - 对比执行计划、状态大小、checkpoint；
   - 用 `EXPLAIN` 看 SQL 计划。

3. **读源码**：
   - 选一个模块（如 window/checkpoint），画类图；
   - 找关键方法，写注释；
   - 用测试验证理解。

4. **做压测**：
   - 构造倾斜数据；
   - 观察反压/状态/checkpoint；
   - 调整并行度/内存/参数。

## 33.7 一个对照实验

MiniFlink：

```python
env.from_collection(events, watermark_strategy=strategy)
   .key_by(lambda e: e["user"])
   .window(TumblingEventTimeWindows.of(Duration.of_minutes(5)))
   .aggregate(SumAggregate("amount"))
   .print()
```

Flink：

```java
env.fromSource(kafkaSource, watermarkStrategy, "events")
   .keyBy(Event::user)
   .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
   .aggregate(new SumAmount())
   .sinkTo(kafkaSink);
```

同样的语义，不同的运行时：

- MiniFlink 在单进程里轮转；
- Flink 在分布式集群里调度；
- 两者的核心语义对齐，但 MiniFlink 缺 idleness、watermark alignment、rescale、barrier 对齐等生产机制，不能把“结果相同”当成“语义完全一致”。

## 33.8 能力差距矩阵

| 能力 | MiniFlink | 生产 Flink | 补齐难度 |
|---|---|---|---|
| 事件时间/watermark | 有（简化） | 完整 | 中 |
| 窗口/触发器 | 子集 | 完整 | 中 |
| 键控状态/TTL | 有 | 完整 | 低 |
| 算子状态 | 类存在，未接入运行时 | 完整 | 中 |
| 广播状态 | 有 | 完整 | 低 |
| 屏障 checkpoint | 无（微批） | 有 | 高 |
| 非对齐 checkpoint | 无 | 有 | 高 |
| 增量快照 | 无 | 有 | 高 |
| savepoint | 部分（手动快照） | 完整 | 高 |
| rescale | 无 | 有 | 高 |
| 事务 sink | 无 | 有 | 高 |
| 类型/schema | 无 | 完整 | 高 |
| SQL 优化器 | 无（SQLite） | Calcite | 很高 |
| 网络/反压 | 无 | 完整 | 很高 |
| HA/运维/安全 | 无 | 完整 | 很高 |

## 33.9 从教学到生产的迁移计划

如果要把一个 MiniFlink 原型迁到 Flink：

1. **固化语义**：把窗口/watermark/状态逻辑写成测试用例；
2. **选 API**：能用 SQL 就用 SQL，复杂状态用 DataStream；
3. **定 uid**：所有有状态算子设置稳定 uid；
4. **选状态后端**：按状态大小/恢复时间选 RocksDB/HashMap；
5. **设计 EOS**：source offset + sink 2PC/幂等；
6. **做容量**：按峰值/状态/checkpoint 估算；
7. **做故障演练**：kill TM、checkpoint 失败、扩缩容；
8. **接监控**：指标/日志/trace/告警；
9. **上线**：灰度 + 回滚 + 对账。

```text
原型验证（MiniFlink） → 语义测试 → Flink 实现 → 压测/故障注入 → 灰度
```

## 33.10 如何讲清楚你的项目

面试/评审时，用这条线：

```text
业务问题 → SLO → 架构 → 时间/状态设计 → 一致性 → 容量/成本 → 故障/运维 → 结果
```

不要只列技术栈。面试官更关心：

- 你为什么选 Flink，而不是批/其他引擎？
- watermark/状态怎么设计？
- 出过什么故障，怎么定位/修复？
- 成本和收益怎么量化？

## 本章小结

MiniFlink 是“理解 Flink 的脚手架”，不是“Flink 的替代品”。它把时间、状态、窗口、checkpoint 的核心语义压缩到可运行的代码，帮助你建立直觉；生产系统还需要分布式、类型、事务、运维和安全。

下一章进入 Flink 2.x 深潜：DataStream API V2、ForSt、物化表、PTF 与 1.x→2.x 迁移；之后再谈实时 AI、正确性工程和生态选型。
