# 第 12 章 Streams 与 Super Streams

> 本章定位：RabbitMQ 不只有“队列”。Stream 是追加日志，语义接近 Kafka 分区；理解 offset、retention、消费者组和 Super Streams，才能在队列与日志之间做正确选择。

## 从队列到日志

传统队列的心智模型是：

```text
消息进入 -> 被一个消费者取走 -> ack 后删除
```

Stream 的心智模型是：

```text
消息追加到日志尾部 -> 多个消费者按自己的 offset 读取
日志按时间/大小保留 -> 读不删除
```

这带来根本差异：

| 维度 | Queue | Stream |
|---|---|---|
| 消费模型 | 竞争消费者，一条消息通常给一个消费者 | 多消费者独立读取，各自 offset |
| 删除 | ack 后删除 | 按 retention 删除，不按 ack |
| 重放 | 困难/不支持 | 通过 offset 重放 |
| 顺序 | 队列内近似 FIFO | 分区内严格追加顺序 |
| 背压 | prefetch/ack | 消费者读取速度/offset |
| 适用 | 任务、RPC、业务解耦 | 事件日志、遥测、数据管道 |

Stream 不是“更快的队列”，而是不同的数据抽象。把任务队列改成 Stream，可能导致同一任务被多个消费者重复执行；把事件日志塞进队列，则无法重放历史。

## Stream 的核心概念

- **offset**：消息在日志中的位置，单调递增；
- **first offset / last offset**：当前保留范围；
- **retention**：按时间（`x-max-age`）或大小（`x-max-length-bytes`）删除旧数据；
- **segment**：日志分段文件（`x-stream-max-segment-size-bytes`），便于滚动删除；
- **consumer**：按 offset 读取，读取不删除；AMQP 0-9-1 访问时仍需 ack（ack 不删除消息），Stream 协议则用 offset tracking/commit；
- **consumer group / super stream**：把分区分配给多个消费者。

RabbitMQ Stream 可以通过专用 Stream 协议（常见端口 5552）访问，也可以在 AMQP 0-9-1 中以 `x-queue-type=stream` 的队列形式使用，但语义受客户端/版本限制。不同访问方式的 offset 提交、QoS 和确认行为要查对应文档。

## Retention

Retention 决定“历史保留多久”：

```text
x-max-age: 7D
x-max-length-bytes: 100000000000
x-stream-max-segment-size-bytes: 100000000
```

`x-stream-max-segment-size-bytes` 要小于 retention 目标：如果段永远不滚动，按大小/时间的删除就没有可回收的粒度。段太小会增加文件/索引开销，段太大导致删除粒度粗、磁盘占用高。

常见策略：

- 时间：保留 7 天/30 天；
- 大小：保留最近 100 GB；
- 两者同时存在时，先触发者生效。

Retention 与消费者进度无关。即使所有消费者都读完了，日志仍保留到过期；即使消费者落后，旧数据也可能被删除，导致它无法从最早 offset 读取。监控 consumer lag 和 first offset 很重要。

## 消费者与 offset

Stream 消费者通常维护：

- 当前读取 offset；
- 已提交 offset（用于故障恢复）；
- 消费者组/分区分配。

与队列 ack 不同，读取一条消息不会删除它。消费者崩溃后，可以从上次提交的 offset 继续，可能重复读取未提交的部分。因此：

- 消费端仍需幂等；
- offset 提交频率影响重复范围；
- 多消费者可以独立重放，不会互相竞争；
- 需要监控 lag = last_offset - consumer_offset。

## Super Streams

单个 Stream 的分区能力有限。Super Stream 把多个 Stream 作为分区，按 routing key 哈希选择分区：

```text
super stream: orders
  partition 0: orders-0
  partition 1: orders-1
  partition 2: orders-2
```

生产者按订单 ID/租户 ID 哈希到分区；同一 key 的消息进入同一分区，从而保证该 key 的局部顺序。消费者组把分区分配给多个消费者：

```text
consumer group "billing"
  consumer A -> partition 0
  consumer B -> partition 1
  consumer C -> partition 2
```

扩容时分区重新分配，可能引起短暂重复/延迟。分区数通常在创建时固定，变更需要迁移。

## 与 Kafka 的相似与不同

相似：

- 追加日志、offset、retention、分区顺序；
- 多消费者独立读取；
- 适合事件流/数据管道。

不同：

- RabbitMQ Stream 是 RabbitMQ 的一部分，复用其认证、vhost、管理面；
- Kafka 生态有 Connect、Streams、KSQL、Schema Registry 等更完整的数据平台；
- RabbitMQ 的强项仍是灵活路由和队列语义，Stream 是补充而非替代 Kafka；
- 运维模型不同：Erlang/RabbitMQ vs JVM/Kafka；
- 云厂商支持不同。

选型时不要只看“都支持 offset”。要问：是否需要 RabbitMQ 的路由/队列混合、现有运维能力、生态工具、吞吐规模、保留策略和团队经验。

## Stream 的适用场景

- 遥测/指标/日志的高吞吐摄入；
- 事件溯源/审计日志；
- 多个独立消费者重放同一事件；
- 实时分析管道；
- 需要大 retention 但不需要 per-message ack 的场景。

不适合：

- 任务分发（需要每条任务只被一个 worker 处理并 ack）；
- 低延迟 RPC；
- 需要优先级/死信重试的复杂队列语义；
- 消息量小、团队不熟悉日志模型。

## Mini 的 Stream 模型

`mini_rabbitmq/streams.py` 实现：

- `Stream.append`：分配 offset、写入内存、按大小/年龄 trim；
- `Stream.read`：按 offset 读取，不删除；
- `Stream.subscribe`：first/last/next 起始位置，独立 offset；
- `SuperStream`：按 routing key 哈希分区；
- `stats`：first/last/next offset、字节数、追加/trim 计数。

测试在 `tests/test_streams.py`，示例在 `examples/14_streams.py`。Mini 的限制：

- 没有磁盘段文件/Stream 协议；
- retention 只在 append 时触发；
- 没有消费者组协调/offset 提交；
- 没有复制；
- 没有与 AMQP 服务端集成（核心 Queue 明确拒绝 `x-queue-type=stream`）。

教程在 README 和第 25 章标注这些边界，避免把教学模型当成生产 Streams。

## 一个 Stream 拓扑示例

```text
super stream: telemetry (3 partitions)
  producer key = device_id
  retention = 24h or 50GB

consumer group "dashboard": 3 consumers, each assigned a partition
consumer group "archive": 1 consumer, reads all partitions from earliest
consumer group "alerting": 2 consumers, reads latest
```

同一 device 的数据在同一分区，dashboard 可以按设备顺序处理；archive 独立重放全量；alerting 只看新数据。它们互不竞争，也不会删除彼此的数据。

## 运维指标

- 每个 partition 的 first/last offset；
- consumer lag；
- retention 删除速率；
- segment 数量/大小；
- 磁盘使用；
- 生产者吞吐/错误；
- 消费者 rebalance 次数；
- 分区热点（某个 partition 过大）。

分区键设计直接决定负载均衡。若所有消息都用同一个 key，所有流量会进一个分区；若 key 分布均匀，才能水平扩展。

## Stream 的存储、段与 retention 细节

Stream 在磁盘上按段（segment）组织：

```text
stream
├── segment 0  (oldest, may be deleted by retention)
├── segment 1
├── segment 2  (active, appended)
└── index/offset metadata
```

关键点：

- 消息追加到活跃段，段满后滚动；
- retention 按时间或大小删除旧段（通常以段为单位，不是逐条删除）；
- 因此实际保留量可能略大于配置阈值；
- 消费者按 offset 读取，读取不删除；
- 段文件/索引的 IO 模式与队列不同，顺序写/读更友好；
- 节点故障/副本恢复依赖日志/段复制。

`x-max-age`、`x-stream-max-segment-size-bytes` 等参数决定保留和段大小。段太小会增加文件/索引开销，段太大导致删除粒度粗、磁盘占用高。用真实消息速率/保留目标校准。

## Stream filtering 与去重

Stream 支持按过滤值减少消费者读取量。4.2 起还支持 SQL 过滤表达式，但它是 **AMQP 1.0 访问方式**的能力，Stream 协议和 AMQP 0-9-1 不使用这套 SQL 过滤（以目标版本文档为准）：

```text
producer: 设置 x-stream-filter-value（如 tenant=eu）
consumer: 订阅时指定 filter（如 tenant=eu）
4.2+ AMQP 1.0: SQL 过滤表达式可按消息属性/值做更复杂的筛选
```

价值：

- 多租户/多类型事件共享一个 stream，消费者只读自己关心的；
- 降低网络/客户端 CPU；
- 不改变日志保留，只是读取过滤。

边界：

- 过滤不是安全边界；消费者仍可能读到其他值（取决于实现/权限）；
- 过滤值基数过大会增加索引/内存；
- 不是所有客户端/协议都支持；
- 4.4 新增 `x-stream-initial-offset` 队列参数，设置 Stream 第一条消息的 offset（创建时固定，非零值可能需要 4.4 feature flag）；它不等于消费者起点，消费者起点仍由 `x-stream-offset`（first/last/next/数值）决定，升级后要分别检查这两处配置。过滤与 offset 的实验见[附录 W](../appendices/W-4x-feature-labs.md)。

### 发布端去重：producer name + publishing ID

Stream 插件支持**发布端去重**，但它不是“端到端只处理一次”：

- 生产者为每次发布分配一个 publishing ID；
- broker 按 **producer name + publishing ID** 记录最高 ID，重复 ID 会被静默丢弃但仍返回确认；
- ID 必须是**从 0 开始、严格递增**的整数（允许跳号），不能用 UUID；乱序/多线程发布会导致误过滤；
- 生产者重启后要向 broker 查询最后发布的 ID，从下一个 ID 继续；
- 去重状态在 chunk trailer 中，消费者看不到 ID，也不参与消费端去重。

正确理解：

```text
发布端去重：同一 producer name 重复发送同一个 publishing ID -> broker 只保留一条
不等于：不同生产者/不同 ID 的重复消息会被去掉
不等于：消费者不会重复处理（重平衡、offset 回退仍会重复）
```

因此消费端仍要幂等；发布端去重解决的是“生产者重试导致的重复写入日志”，不是“业务只执行一次”。如果使用去重，producer name 必须稳定且全局唯一（不要每个 Pod 随机生成），否则去重表会被打散，效果归零。

## Super Stream 的分区、rebalance 与顺序

Super Stream 是“多个分区 Stream 的逻辑集合”：

```text
super stream orders
  orders-0
  orders-1
  orders-2
```

生产者按 key 哈希到分区；同一 key 进同一分区，保证该 key 的局部顺序。

消费者组：

- 每个分区分配给组内一个消费者；
- 消费者数 > 分区数时，多余消费者空闲；
- 消费者数 < 分区数时，一个消费者处理多个分区；
- 扩容/缩容触发 rebalance，可能短暂重复/延迟；
- 分区数通常创建时固定，变更需要迁移/重建。

设计建议：

- 分区数 ≥ 预期最大消费者数（留余量）；
- 分区键选择高基数、分布均匀的业务键；
- 避免热点键（一个大客户占满一个分区）；
- 监控每分区 offset/lag/大小；
- rebalance 期间消费端幂等。

## Stream 协议 vs AMQP 访问

同一个 Stream 可以有两种访问方式：

| 方式 | 端口 | 能力 | 限制 |
|---|---|---|---|
| Stream 协议 | 常见 5552 | offset、consumer group、credit、过滤、高性能 | 需要专用客户端 |
| AMQP 0-9-1（stream queue） | 5672 | 基础消费/确认 | 能力/语义受客户端限制 |

如果只需要“从 stream 读消息”，AMQP 可能够用；如果需要 consumer group、offset 提交、过滤、高吞吐，使用 Stream 协议客户端。不要假设两种方式的 offset/确认语义完全一致。

## Kafka 对比的工程维度

除了“都支持 offset”，还要比较：

| 维度 | RabbitMQ Streams | Kafka |
|---|---|---|
| 运维 | 复用 RabbitMQ/Erlang/管理面 | JVM/KRaft/Connect 生态 |
| 协议 | Stream 协议 + AMQP | Kafka 协议 |
| 生态 | 与 RabbitMQ 路由/队列混合 | Connect/Streams/Schema Registry 更成熟 |
| 多租户 | vhost/权限 | topic/ACL/quota |
| 保留 | 时间/大小/段 | 时间/大小/compaction |
| 事务 | 有限 | 事务/幂等生产者更完整 |
| 适用 | 已有 RabbitMQ、需要重放 | 大规模数据平台/流处理 |

选型不是“谁更强”，而是“现有运维体系 + 生态 + 负载特征”。很多团队两者并用：RabbitMQ 做业务命令/事件，Kafka 做数据管道。

## 本章小结

Stream 是追加日志：offset 定位、retention 保留、消费者独立读取、读不删除。Super Streams 通过分区提供扩展和局部顺序。它适合事件日志/遥测/重放，不适合任务分发和复杂队列语义。Mini 实现了语义模型，但没有实现生产级 Stream 协议、磁盘段和复制。

下一章讨论 Federation 与 Shovel：当消息需要跨 broker、跨机房、跨云时，如何复制/转发，以及如何防止环路和重复。

---

[上一章：第 11 章 集群、Quorum Queue 与故障转移](11-cluster-quorum.md) | [返回目录](../README.md) | [下一章：第 13 章 Federation、Shovel 与多活拓扑](13-federation-shovel.md)
