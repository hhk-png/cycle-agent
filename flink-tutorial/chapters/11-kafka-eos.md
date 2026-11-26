# 第11章 Kafka 端到端 Exactly-once

## 11.1 “端到端”到底包含什么

很多文章说“Flink + Kafka 可以 exactly-once”，但这句话省略了三个前提：

1. Source 的消费 offset 必须进入 checkpoint；
2. Sink 的写入必须和 checkpoint 完成绑定；
3. 下游消费者必须只读已提交数据。

缺任何一条，端到端语义都会退化。

```text
KafkaSource ──> Flink 算子 ──> KafkaSink
   offset          state          transaction
      │              │                │
      └──── checkpoint n 一致切面 ────┘
                       │
                       ▼
              checkpoint 完成 → 提交事务 n
```

三个组件必须在同一个一致性协议里，而不是各自“支持 exactly-once”。

## 11.2 KafkaSource 的 offset

KafkaSource 把每个分区的 offset 作为 operator state 保存。checkpoint 时：

```text
分区 0 已处理 offset 1000
分区 1 已处理 offset 2500
        ↓ 写入 checkpoint
恢复时从 1000 / 2500 继续
```

如果作业失败，Flink 回滚到最近完成的 checkpoint，source 从该 offset 重放。由于 offset 和算子状态在同一快照中，内部状态与输入位置一致。

注意：

- 未完成的 checkpoint 不能用于恢复；
- offset 提交到 Kafka（consumer group）和 checkpoint 是两件事；
- `setStartingOffsets` 决定首次启动位置，恢复时以 checkpoint 为准；
- 分区发现可能导致新分区从 earliest/latest 开始，要明确配置；
- watermark 不在 Kafka 中，恢复后由 source 重新生成。

```java
KafkaSource<Order> source = KafkaSource.<Order>builder()
    .setBootstrapServers("kafka:9092")
    .setTopics("orders")
    .setGroupId("risk-job")
    .setStartingOffsets(OffsetsInitializer.committedOffsets(OffsetResetStrategy.EARLIEST))
    .setProperty("partition.discovery.interval.ms", "60000")
    .setValueOnlyDeserializer(new OrderDeserializationSchema())
    .build();
```

`committedOffsets` 只影响“没有 checkpoint 时”的首次启动；有 checkpoint 时，Flink 用 checkpoint 里的 offset。不要把两者混为一谈。

### offset 提交与 consumer group

KafkaSource 可以选择把 offset 提交回 Kafka（`enable.auto.commit` 或 connector 的 offset commit 配置）。这有两个用途：

- 方便其他消费者/监控看到进度；
- 在没有 checkpoint 的作业中做粗略恢复。

但它**不能替代 checkpoint**：offset 提交是异步的，可能落后或超前于算子状态。真正的恢复依据是 checkpoint。

## 11.3 KafkaSink 的两阶段提交

KafkaSink 使用 Kafka 事务实现 2PC：

```text
checkpoint 开始
  → sink 开始/继续事务，写入 Kafka（未提交）
checkpoint 完成
  → sink 提交事务
checkpoint 失败
  → sink 中止事务
```

更细的时间线：

```text
1. CheckpointCoordinator 注入 barrier
2. SinkWriter 收到 barrier，flush 当前批次
3. SinkWriter 返回 committable（transaction id + checkpoint id）
4. 所有 task 确认快照
5. Checkpoint 标记完成
6. Committer 收到完成通知，提交事务
7. 下游 read_committed 消费者可见
```

如果第 6 步之前失败，事务不会被提交；Kafka 会在 `transaction.timeout.ms` 后自动 abort。恢复后从上一个 checkpoint 重放，新事务继续。

配置示例：

```java
KafkaSink<Order> sink = KafkaSink.<Order>builder()
    .setBootstrapServers("kafka:9092")
    .setRecordSerializer(new OrderSerializer("orders-out"))
    .setDeliveryGuarantee(DeliveryGuarantee.EXACTLY_ONCE)
    .setTransactionalIdPrefix("orders-job-")
    .setProperty("transaction.timeout.ms", "900000")
    .build();
```

关键参数：

- `transactional.id` 前缀必须每个作业唯一，否则不同作业会互相 abort；
- `transaction.timeout.ms` 必须大于 checkpoint 间隔 + 最大 checkpoint 时长，否则事务可能超时；
- Kafka broker 的 `transaction.max.timeout.ms` 要足够大；
- 下游消费者设置 `isolation.level=read_committed`，否则会读到未提交数据；
- 每个 sink subtask 需要独立事务 id，Flink 会自动加后缀；
- 事务 id 在恢复后要能复用，否则旧事务悬挂。

### 事务超时的计算

```text
transaction.timeout.ms > checkpoint.interval + checkpoint.timeout + 缓冲
```

例如 checkpoint 间隔 5 分钟、超时 10 分钟，事务超时至少要 20 分钟。否则一次慢 checkpoint 就会让事务过期，作业反复失败。

## 11.4 故障时发生什么

假设 checkpoint n 正在做，sink 已写入部分消息：

- **checkpoint n 成功**：事务提交，消息对 read_committed 消费者可见；
- **checkpoint n 失败**：事务中止，消息不可见；作业恢复到 checkpoint n-1，重新处理；
- **作业在提交前崩溃**：Kafka 事务超时后自动 abort；
- **checkpoint 已完成、但提交确认丢失**：恢复后 Flink 会重新提交同一个 committable；事务 id 与 checkpoint 绑定，Kafka 对同一事务的重复提交是幂等的，不会产生两份可见数据。前提是下游 `read_committed`，且事务 id 在恢复后保持一致；
- **sink 提交成功但下游没读**：消息仍在 Kafka，下游恢复后可读；
- **下游读了未提交数据**：如果 `read_uncommitted`，会看到稍后被 abort 的消息，造成“脏读”。

### 一个容易误解的点

Flink 的 2PC 不是“所有 sink 同时提交”的分布式事务。它是：

- 每个 sink subtask 独立事务；
- checkpoint 完成是提交的协调点；
- 提交失败会重试，但可能部分成功。

因此，下游必须能处理“部分事务已提交、部分未提交”的短暂不一致。最终一致依赖 checkpoint 完成后的重试和 Kafka 事务语义。

## 11.5 常见错误配置

| 错误 | 后果 |
|---|---|
| 下游用 `read_uncommitted` | 读到未提交/已中止消息 |
| `transaction.timeout.ms` 小于 checkpoint 间隔 | 事务超时，作业反复失败 |
| 多个作业共用 transactionalIdPrefix | 事务互相干扰 |
| checkpoint 间隔过长 | 恢复时间长，事务长时间未提交 |
| checkpoint 间隔过短 | 频繁事务提交，吞吐下降 |
| sink 并行度改变但事务 id 不复用 | 旧事务悬挂，需手工清理 |
| 使用 at-least-once sink 却宣称 EOS | 重复写入 |
| 没有开启 checkpoint | 事务 sink 无法提交协调 |
| 下游消费者组 offset 提交覆盖 checkpoint | 恢复位置错误 |

## 11.6 SQL 中的 Kafka EOS

Flink SQL Kafka connector 通过 table option 配置：

```sql
CREATE TABLE sink_orders (
  order_id STRING,
  amount DECIMAL(10, 2)
) WITH (
  'connector' = 'kafka',
  'topic' = 'orders-out',
  'properties.bootstrap.servers' = 'kafka:9092',
  'format' = 'json',
  'sink.delivery-guarantee' = 'exactly-once',
  'sink.transactional-id-prefix' = 'orders-sql-'
);
```

同时确保作业开启 checkpoint。没有 checkpoint，事务 sink 无法完成提交协调。

SQL 作业还要注意：

- `table.exec.sink.not-null-enforcer` 等配置可能改变写入行为；
- 结果表的 changelog 模式（append/upsert/retract）要与 sink 能力匹配；
- Kafka sink 的分区键、序列化格式要与下游约定。

## 11.7 性能与运维

- 事务提交有开销，checkpoint 间隔不能太短；
- 每个 sink subtask 一个事务，并行度高时事务数多；
- Kafka 事务会占用 broker 资源，需要监控；
- 失败后会留下 `__transaction_state`，超时自动清理；
- 监控 checkpoint 时长、事务提交失败、consumer lag；
- 升级作业前先 savepoint，确保事务 id 和 offset 可恢复。

### 关键指标

- `checkpointDuration`：checkpoint 总时长；
- `checkpointAlignmentTime`：对齐时间；
- `transactionCommitTime`：事务提交耗时；
- `recordsWritten` / `bytesWritten`：写入量；
- `consumerLag`：下游消费滞后；
- `numAbortedTransactions`：abort 次数（如果可采集）。

## 11.8 端到端验证

不要只看配置。用一个可观测的测试：

1. 写入 100 万条带唯一 id 的消息；
2. 作业写 Kafka，下游用 read_committed 消费；
3. 在处理到一半时 kill TaskManager；
4. 恢复后统计下游唯一 id 数和总数；
5. 期望：唯一 id = 100 万，总数 = 100 万（无重复无丢失）。

```python
# 校验脚本示意
seen = set()
duplicates = 0
with consumer:
    for msg in consumer:
        if msg.value["id"] in seen:
            duplicates += 1
        seen.add(msg.value["id"])
assert len(seen) == 1_000_000
assert duplicates == 0
```

如果 sink 是数据库，改用唯一主键 + upsert，或事务表，重复执行验证幂等。

### 故障注入矩阵

| 注入点 | 期望 |
|---|---|
| checkpoint 前 kill TM | 回滚到上一 checkpoint，无重复 |
| checkpoint 中 kill TM | 事务 abort，重放 |
| checkpoint 完成后、提交前 kill | 事务可能已提交，下游去重/read_committed |
| 提交后 kill | 数据可见，offset 从 checkpoint 恢复 |
| 下游读时 kill | 下游自己的 offset 决定重复范围 |

## 11.9 MiniFlink 的边界

MiniFlink 的 `KafkaSink` 只是把消息追加到内存列表，`KafkaSource` 把 offset 放进快照：

```python
source = KafkaSource(bus, "in", "g", 0, 1)
source.next_element()
snapshot = source.snapshot()
```

这演示了“offset 与状态同快照”的思想，但没有事务、read_committed、consumer group 协调。`KafkaSink` 明确不是 exactly-once sink。生产语义必须使用真正的 Flink Kafka connector。

## 11.10 事务 fencing、幂等与多 sink 边界

Kafka 的 exactly-once 不是“打开一个开关”，而是一套生产者、broker、消费者和 Flink checkpoint 共同遵守的协议。最容易出错的地方，往往不是正常路径，而是**旧 producer 没死透、新 producer 已接管**的灰色地带。

### transactional.id 与 fencing

Kafka 用 `transactional.id` 标识一个事务生产者。同一个 `transactional.id` 同时只允许一个活跃 producer；新 producer 初始化时，broker 会：

1. 递增该 transactional.id 的 epoch；
2. **fence** 掉旧 producer（旧 producer 后续的写入/提交会失败）；
3. abort 旧 producer 未完成的事务。

Flink 的 `KafkaSink` 默认会为每个 sink subtask、每个 checkpoint 生成稳定的 transactional id（通常是前缀 + subtask + checkpoint id 的组合，具体格式以版本文档为准）。恢复时，新 task 用同一个 id 重新初始化，从而 fence 掉可能还活着的旧 task。

高并行度 + 短 checkpoint 间隔会让 transaction id 数量快速膨胀。2.x 的 Kafka connector 提供 `sink.transaction-naming-strategy=POOLING`（FLIP-511）：每个 subtask 复用一个小 ID 池，而不是为每个 checkpoint 生成新 ID；默认仍是 `INCREMENTING`。切换前要验证恢复时的事务 fence、下游可见性和 broker 版本（POOLING 依赖 Kafka 3.0+ 的事务列表 API）。

这带来两个工程要求：

- `transactionalIdPrefix` 必须在作业内唯一，且跨重启稳定；不同作业共用会导致互相 fence；
- 旧 producer 被 fence 是**预期行为**，不是“Kafka 坏了”。看到 `ProducerFencedException`/`OutOfOrderSequenceException` 时，正确处置是关闭 producer、重建，而不是无限重试同一个实例。

### 事务超时不是“越大越好”

```text
transaction.timeout.ms
  > checkpoint 间隔 + 最大 checkpoint 时长 + 缓冲
  <= broker 的 transaction.max.timeout.ms
```

- 太小：一次慢 checkpoint 就会让事务过期，作业反复失败；
- 太大：故障后未完成事务长时间不清理，`read_committed` 消费者被阻塞在 LSO（last stable offset）之后；
- broker 侧的上限（常见默认 15 分钟）必须覆盖客户端设置，否则 producer 初始化/提交会失败。

### 幂等 producer ≠ exactly-once

Kafka 的幂等 producer 通过 producer id + sequence number 去重，能避免**同一个 producer 会话内**的重试重复。但它不解决：

- 进程重启后新 producer 的重复；
- 跨 checkpoint 的事务可见性；
- 下游 `read_uncommitted` 读到未提交/已中止数据。

端到端 exactly-once 仍然需要：source offset 进 checkpoint、sink 事务与 checkpoint 完成绑定、下游 `read_committed`。

### 提交确认丢失时会发生什么

一个常见场景：

```text
checkpoint n 完成
  → Committer 调用 commitTransaction
  → broker 已提交
  → 确认在网络上丢失 / task 崩溃
  → 恢复后 Flink 重新提交同一个 committable
```

Kafka 对同一事务的重复提交是幂等的：事务已经提交，重复提交不会产生第二份可见数据。前提是：

- transactional id 在恢复后保持一致；
- 下游只读已提交数据；
- 没有另一个 producer 用同一个 id 把事务 abort 掉。

如果提交前就崩溃，事务不会被提交，broker 会在超时后 abort；恢复后从上一个 checkpoint 重放，新事务继续。

### 多 sink 的原子性边界

一个 Flink 作业同时写 Kafka 和湖仓时，checkpoint 完成会通知**每个** sink 的 Committer。各 sink 独立提交：

```text
checkpoint n 完成
  ├── Kafka sink 提交成功
  └── 湖仓 sink 提交失败/超时
```

这不是跨系统的分布式事务。结果是：Kafka 已可见、湖仓未可见，直到重试成功。工程上要：

- 让下游能容忍短暂不一致；
- 用业务主键/对账修复；
- 监控每个 sink 的提交延迟和失败；
- 不要把“两个 sink 都支持 EOS”误认为“两个 sink 原子提交”。

### 消费端 offset 提交的陷阱

KafkaSource 可以把 offset 提交回 consumer group，用于监控或其他消费者。但**恢复依据必须是 checkpoint 里的 offset**，不是 group offset：

- group offset 是异步提交的，可能落后/超前于算子状态；
- 手工 `commitSync` 可能覆盖 checkpoint 语义；
- 多个作业共用 group 会互相干扰。

### 故障注入矩阵（补充）

| 注入点 | 期望 | 观察 |
|---|---|---|
| checkpoint 完成前 kill TM | 事务未提交，恢复后重放 | 下游无重复 |
| 提交中 kill Committer | 可能已提交；恢复后重提 | 下游仍无重复 |
| 旧 producer 悬挂后新实例启动 | 旧实例被 fence | 旧实例报 fatal，不重试 |
| broker 事务超时 | 事务 abort | 下游 `read_committed` 不读脏数据 |
| 多 sink 之一提交失败 | 部分可见 | 对账/重试后收敛 |
| 下游 `read_uncommitted` | 可能读到 abort 数据 | 改为 `read_committed` |

### 验证方法

不要只看配置。用一个可重复的测试：

1. 写入带唯一 id 的事件；
2. 作业写 Kafka，下游 `read_committed` 消费；
3. 在 checkpoint 前、中、后分别 kill TM；
4. 恢复后统计唯一 id 数、总数、重复数；
5. 再测“提交成功但确认丢失”的场景（代理层丢包/超时）；
6. 多 sink 场景分别校验每个下游。

只有故障注入能证明“看起来 exactly-once”不是运气。

## 本章小结

Kafka 端到端 exactly-once = **source offset 进 checkpoint + sink 事务 + checkpoint 完成提交 + 下游 read_committed**。四个条件缺一不可。

配置之外，还要用故障注入验证，否则“看起来 exactly-once”只是幸运。下一章讨论维表 join 和 Async I/O，这是实时宽表最常见的性能瓶颈之一。
