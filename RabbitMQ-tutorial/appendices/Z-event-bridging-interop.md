# 附录 Z 事件互操作与桥接工程：RabbitMQ ↔ Kafka ↔ CDC ↔ 云队列

> 本附录定位：把“把消息从 A 搬到 B”当成一个有状态、会失败、需要契约的工程问题。它不重复第 13 章的 Federation/Shovel 配置，也不重复[附录 D](D-comparison.md)的产品对比，而是回答一个更常见的问题：当 RabbitMQ 必须和 Kafka、数据库 CDC、云队列或另一种协议共存时，怎样搬消息才不会丢、不会重复、不会把顺序和语义搬坏。

## 为什么“桥”不是一根管子

很多人把桥接想成下面这样：

```text
RabbitMQ queue ──读一条、写一条、删一条──> Kafka topic
```

这条线里至少藏着五个状态：

1. 源端读到了哪条消息（delivery tag / offset / receipt handle）；
2. 消息是否已经成功写入目标（produce 是否被确认）；
3. 目标端的写入是否可重复（幂等键是什么）；
4. 失败后从哪里重试（checkpoint）；
5. 无法处理的消息去哪里（DLQ / parking lot）。

只要桥进程在“目标已写、源未确认”之间崩溃，就一定会产生重复；在“源已确认、目标未写”之间崩溃，就一定会丢消息。桥的可靠性不是由某个客户端参数决定的，而是由**写入目标、确认源端、推进 checkpoint 的先后顺序**决定的。

先记住三条不变量：

- **不变量 1：目标端没有持久化成功，绝不 ack/delete 源消息。**
- **不变量 2：目标端的 offset/checkpoint 不能在发布确认之前提交。**
- **不变量 3：跨系统没有免费的 exactly-once；用稳定事件 ID + 幂等写入把“至少一次”收敛成业务上的“恰好一次效果”。**

本附录配套一个可运行示例：`examples/20_bridge_interop.py`。它用 Mini-RabbitMQ 作为源、用一个分区追加日志模拟 Kafka，注入“写完目标、ack 前崩溃”，并证明重投不会产生第二条记录。读完后建议先跑它，再看后面的失败矩阵。

```bash
cd RabbitMQ-tutorial
python examples/20_bridge_interop.py
```

期望输出类似：

```text
injected crash: bridge died after target write, before ack
appended=6 duplicates=1 acked=6 checkpoint=evt-6
order-1 order: order.created -> order.paid -> order.shipped
OK 20_bridge_interop
```

## 先做语义映射，再选工具

桥接的第一份交付物不是代码，而是一张语义映射表。下面这些维度只要有一项没写清，后面就会在故障时暴露。

### 投递与确认

| 系统 | 常规语义 | 确认单位 | 重复来源 |
|---|---|---|---|
| RabbitMQ | 至少一次 | 每条消息的 delivery tag（可 multiple） | ack 前崩溃、连接断开、confirm 超时重发 |
| Kafka | 至少一次（默认） | consumer group 的 offset commit | 处理完、commit 前崩溃；再均衡 |
| SQS 标准队列 | 至少一次 | receipt handle + delete | 可见性超时、delete 失败 |
| SQS FIFO | 去重窗口内恰好一次处理 | receipt handle + delete | 窗口外重发、重复消息体 |
| AMQP 1.0 | 由 settlement 决定 | link 上的 delivery | 未 settle、link 恢复 |
| MQTT | QoS 0/1/2 | broker 与客户端的 PUBACK/PUBREC | QoS 1 重复；QoS 2 只保证链路内 |

关键不是“谁支持 exactly-once”，而是“重复发生在哪一层、由谁吸收”。RabbitMQ 的 publisher confirm 只证明 broker 接受了消息，不证明消费者处理成功；Kafka 的 offset commit 只证明消费进度，不证明业务副作用只发生一次。跨系统的 exactly-once 通常要落到目标端的唯一约束、upsert 或去重表。

### 顺序的作用域

| 系统 | 顺序保证的边界 | 破坏顺序的常见操作 |
|---|---|---|
| RabbitMQ classic | 单队列、单消费者、无 requeue 时近似 FIFO | 多消费者、requeue、优先级、死信重入 |
| RabbitMQ quorum | 单队列内按提交顺序投递 | leader 切换、重投、多消费者 |
| Kafka | 单 partition 内 | 换 key、增加 partition、并发处理 |
| SQS FIFO | 单 message group 内 | 多 group 并发、重试 |
| SQS 标准 | 不保证 | 并发、可见性超时 |

桥接时最常见的错误是“源端按 A 有序，目标端按 B 分区”。例如 RabbitMQ 里同一订单的消息在同一队列，桥却按随机 key 写入 Kafka，顺序立刻消失。正确做法是先定义**顺序键**（通常是 `aggregate_id`），再让源端队列、桥的并发模型和目标端分区都围绕这个键对齐。

### 保留、重放与删除

RabbitMQ 的普通队列在 ack 后删除消息；Kafka 按时间/大小保留，消费者可以重置 offset 重放；SQS 有保留期（默认 4 天，可配置到 14 天）；RabbitMQ Streams 按 retention 保留。这个差异决定了“桥能否回放”：

- 源端是 RabbitMQ 普通队列：一旦 ack，消息就没了；桥不能靠“重新读队列”回放，必须有原始归档或 outbox。
- 源端是 Kafka：只要 retention 内，桥可以重置 offset 重放；但目标端必须幂等，否则会重复写入。
- 源端是 SQS：消息删除后不可回放；DLQ 和归档是唯一补救。

所以“桥接方案”必须回答：**重放的数据源在哪里？重放的幂等键是什么？重放会不会影响在线消费者？**

### 流控与背压

RabbitMQ 用 prefetch/QoS、connection blocked、内存/磁盘告警来施加背压；Kafka 用 `max.poll.records`、消费 lag、暂停/恢复分区；SQS 用可见性超时和批量大小。桥接器如果只知道“读得快”，就会把源端读空、把目标端打爆，最后在内存里堆出一个不受监控的缓冲区。

一个可靠的桥必须能**暂停源端读取**：RabbitMQ 用 `basic.qos` + 停止 `basic.get`/暂停 consumer；Kafka 用 `pause()`；SQS 用降低并发和延长等待。目标端持续失败时，正确动作是停止 ack、让源端积压并告警，而不是无限重试或把消息丢进内存队列。

### 元数据与契约

跨系统时，消息体通常不是问题，属性才是。至少要映射：

| 语义 | RabbitMQ | Kafka | 建议 |
|---|---|---|---|
| 事件 ID | `message_id` | record key/header | 生成稳定 UUID，写入目标记录 |
| 事件类型 | `type` / routing key | header / topic | 显式字段，不依赖 topic 名 |
| 关联 ID | `correlation_id` | header | 贯穿请求链路 |
| 内容类型 | `content_type` | header | `application/json` / `application/avro` |
| 时间 | `timestamp` | record timestamp | 区分“发生时间”和“写入时间” |
| 租户 | header | header/key | 用于隔离、分区、审计 |
| 失败原因 | `x-death` | DLQ header | 保留原始错误和重试次数 |

建议使用 CloudEvents 作为跨系统信封（id、source、type、time、datacontenttype、data），但不要把 CloudEvents 当成可靠性机制：它只统一元数据，不提供确认、顺序或幂等。事件契约、schema 兼容和事件目录见[附录 O](O-event-contracts-governance.md)。

## 五种桥接拓扑

| 拓扑 | 状态在哪里 | 顺序 | 重复处理 | 适合 | 不适合 |
|---|---|---|---|---|---|
| 应用层 outbox + 桥 | 业务库 outbox 表 | 按 aggregate | 目标幂等 | 业务事件、可审计 | 大量非业务数据 |
| Shovel | 源队列 + 目标 broker | 通常按队列 | 至少一次 | 迁移、跨集群搬运 | 复杂转换、跨协议 |
| Federation | 上游交换机/队列 | 不保证全局 | 至少一次 | 多区域订阅、松耦合 | 强顺序、全量镜像 |
| CDC | 数据库日志/connector offset | 按主键/分区 | 至少一次 + 快照重复 | 少改应用、捕获变更 | 业务语义强的事件 |
| 连接器/托管桥 | connector offset | 取决于连接器 | 取决于连接器 | 标准源汇、低运维 | 特殊路由/转换 |

### 拓扑 A：应用层 outbox + 桥

业务事务同时写业务表和 outbox 表，独立发布器读 outbox，发布到 RabbitMQ 或 Kafka。它的优点是事件由业务代码显式定义，语义清楚、可审计；缺点是应用要多一张表和一个发布器。第 07、19 章和 `examples/19_outbox_idempotency.py` 已经给出闭环。

桥接场景下，outbox 常作为“源端适配器”：应用不直接依赖 Kafka，只写自己的数据库；桥把 outbox 搬到目标系统。此时 outbox 的 `id` 就是稳定事件 ID，目标端按它去重。

### 拓扑 B：Shovel

Shovel 把一个 broker 的队列搬到另一个 broker 的交换机/队列，适合迁移、跨集群汇聚、边缘到中心。它不理解业务 schema，也不做字段转换；源端和目标端的确认由插件协调，但崩溃窗口仍可能产生重复。用 Shovel 时要监控：

- 源队列积压和 Shovel 状态；
- 目标端不可达时的重连/退避；
- 重复消息对目标消费者的影响；
- 是否需要 TLS、独立的 Shovel 用户和 vhost。

Shovel 的配置和动态迁移见第 13 章。不要把 Shovel 当成“跨协议网关”：它主要搬 AMQP 消息，不负责把 AMQP 属性翻译成 Kafka 记录。

### 拓扑 C：Federation

Federation 让下游 broker 以“上游订阅”的方式接收交换机/队列的消息，适合多区域、多团队松耦合。它不是全量镜像，也不保证跨上游的全局顺序；上游不可用时，下游只能拿到恢复后的消息。Federation 的 policy、upstream set 和失败模式见第 13 章。

### 拓扑 D：CDC

CDC 从数据库事务日志派生变更事件（Debezium 等），不需要业务代码双写。它适合“数据库已经是事实源、希望把变更同步到消息系统”的场景。代价是：

- 事件形状由数据库 schema 决定，容易泄露内部表结构；
- 初始快照会与增量流重复；
- 删除、DDL、事务边界、tombstone 需要专门处理；
- connector offset、复制槽/WAL 保留、重放和权限是新的运维面。

CDC 的事件通常是“行变更”，不是“业务事件”。如果下游需要 `order.created` 这种语义，最好在 CDC 之后加一层转换/聚合，或者直接用 outbox。

### 拓扑 E：连接器/托管桥

Kafka Connect、云厂商的 SQS/SNS 连接器、托管 EventBridge 管道等把“读源、写目标、提交 offset”产品化。优点是运维少、offset 管理成熟；限制是转换能力、顺序控制、错误处理和成本。选型时要确认：

- 连接器的投递语义和 offset 提交时机；
- 是否支持 exactly-once，还是仅至少一次；
- 失败记录去哪里，能否重放；
- schema 转换/映射能力；
- 版本升级和供应商锁定。

## 方向 A：RabbitMQ → Kafka

典型用途：把业务事件归档到数据湖、供流处理/分析使用、长期保留和重放。

```text
producer -> exchange: orders -> queue: bridge.orders -> bridge -> Kafka topic: orders
                                                              key = aggregate_id
```

### 状态机

```text
get(source)
  -> transform
  -> produce(topic, key, value, headers)
  -> wait for delivery report / flush
  -> ack(source delivery_tag)
  -> advance checkpoint
```

顺序不能反过来。先 ack 再 produce，会在 produce 失败时丢消息；produce 后不等待确认就 ack，会在 broker 缓冲区丢失时丢消息。

### Kafka producer 配置

```python
from confluent_kafka import Producer

producer = Producer(
    {
        "bootstrap.servers": "kafka-1:9092,kafka-2:9092",
        "enable.idempotence": True,
        "acks": "all",
        "max.in.flight.requests.per.connection": 5,
        "compression.type": "zstd",
        "linger.ms": 5,
    }
)
```

`enable.idempotence=True` 让同一个 producer 会话内的重试不产生重复；它不能覆盖“produce 成功、进程崩溃、重启后重新 produce”的窗口。跨重启的重复仍要靠目标端按事件 ID 去重，或者使用 Kafka 事务并让下游读已提交。

### 崩溃矩阵

| 崩溃点 | 源端状态 | 目标端状态 | 恢复动作 | 结果 |
|---|---|---|---|---|
| produce 前 | 未 ack，会重投 | 无记录 | 重新读、重新 produce | 正常 |
| produce 后、flush 前 | 未 ack | 可能已写入，也可能没有 | 重投、重新 produce | 可能重复，靠事件 ID 去重 |
| flush 后、ack 前 | 未 ack | 已写入 | 重投、目标去重 | 业务恰好一次效果 |
| ack 后、checkpoint 前 | 已 ack | 已写入 | checkpoint 从目标/源端重建 | 不重复消费；checkpoint 可能滞后 |
| 目标不可用 | 未 ack | 无写入 | 暂停读取、退避、告警 | 源端积压，不丢 |

注意最后一行：目标不可用时，桥应该停止 ack 并让源端积压。把消息放进桥进程的内存队列再 ack，等于把 RabbitMQ 的持久化换成了一个更脆弱的缓冲区。

### 背压与批量

- 用 `basic.qos(prefetch_count=N)` 限制未确认消息；
- 用 producer 的队列长度/`flush()` 控制内存；
- 批量 produce 可以提升吞吐，但必须在整批确认后再 ack 对应的源消息；
- 目标端延迟升高时，降低并发/批量，而不是加大超时。

### 重放

Kafka 的 retention 决定能重放多久；RabbitMQ 普通队列 ack 后不能重放。因此：

- 需要“从源头重建”时，保留原始事件归档（对象存储/数据湖）或使用 outbox 表；
- 重放到 Kafka 时，用同一个事件 ID 和幂等 upsert；
- 重放要与在线流量隔离（独立 topic/consumer group/限速）。

## 方向 B：Kafka → RabbitMQ

典型用途：Kafka 里的领域事件需要驱动命令/任务，利用 RabbitMQ 的路由、优先级、单条 ack、DLQ 和 RPC。

```text
Kafka topic: orders (partition by order_id)
  -> bridge consumer group
  -> exchange: commands (topic)
  -> queue: inventory.reserve (quorum)
```

### 状态机

```text
poll()
  -> transform
  -> publish to RabbitMQ (mandatory + publisher confirm)
  -> if confirmed: commit offset
  -> else: retry / park / stop
```

### 崩溃矩阵

| 崩溃点 | Kafka offset | RabbitMQ | 恢复动作 | 结果 |
|---|---|---|---|---|
| publish 前 | 未提交 | 无消息 | 重新 poll | 正常 |
| publish 后、commit 前 | 未提交 | 已写入 | 重新 poll、重新 publish | 重复，消费端幂等 |
| commit 后 | 已提交 | 已写入 | 继续下一条 | 正常 |
| publish 被 nack/不可路由 | 未提交 | 未写入/退回 | 重试或进 DLQ | 不丢，但可能阻塞分区 |

`mandatory=true` 只能发现“不可路由”，不能替代 publisher confirm；确认成功也不代表消费者处理成功。Kafka offset 的提交时机必须与 RabbitMQ 的发布确认对齐。

### 顺序与并发

Kafka 只保证单 partition 内有序。桥要保证同一 key 的命令按序到达 RabbitMQ，需要：

- 一个 partition 同时只由一个桥实例处理；
- 同一 key 路由到同一队列；
- 该队列由单消费者或单活消费者处理；
- 重试不绕过后续消息（或显式允许乱序）。

Rebalance 是最容易破坏顺序的时刻：旧 consumer 可能还有在途 publish，新 consumer 已开始 poll。处理方式是先停止 poll、等待在途 publish 确认、再提交 offset/让出分区；无法等待时，下游必须用版本号/状态机处理乱序。

### 毒消息

Kafka 的消息不能像 RabbitMQ 那样 `nack(requeue=false)` 直接进 DLX。桥要自己实现：

1. 记录尝试次数（header/状态表）；
2. 超过上限后发布到 RabbitMQ DLQ 或 Kafka DLQ topic；
3. 提交 offset，避免阻塞整个 partition；
4. 保留原始消息、错误栈、桥版本和时间。

也可以选择“不提交 offset，阻塞分区”，但这会把一条毒消息变成整个分区的可用性事故。多数业务更适合“有限重试 + parking + 告警”。

## 方向 C：CDC → RabbitMQ

Debezium 风格的事件通常长这样：

```json
{
  "before": null,
  "after": {"id": 42, "status": "created", "customer_id": "c-7"},
  "source": {
    "connector": "postgresql",
    "db": "orders",
    "table": "orders",
    "lsn": 123456,
    "ts_ms": 1730000000000
  },
  "op": "c",
  "ts_ms": 1730000000000
}
```

把它搬到 RabbitMQ 时，要处理四件事：

1. **路由**：按表/聚合根选择 exchange 和 routing key，例如 `orders.order.created`；
2. **顺序**：同一主键的变更按日志顺序路由到同一队列；
3. **重复**：连接器重启、快照与增量重叠都会重复；用 `(connector, lsn)` 或业务事件 ID 去重；
4. **删除**：`op=d` 和 tombstone 要有明确的业务语义，不能把“删除”当成“更新为空”。

CDC 与 outbox 的取舍：

| 维度 | Outbox | CDC |
|---|---|---|
| 应用改动 | 需要写 outbox 表/发布器 | 通常不需要改业务代码 |
| 事件语义 | 业务显式定义 | 数据库行变更，偏底层 |
| 事务一致性 | 与业务同事务 | 由数据库日志保证 |
| Schema 泄漏 | 可控 | 容易暴露内部表 |
| 运维 | 表清理、发布器 | 复制槽/WAL、connector、schema |
| 适合 | 领域事件、跨服务契约 | 数据同步、缓存失效、审计 |

CDC 的运维细节见[附录 O](O-event-contracts-governance.md)的事件契约部分和[附录 U](U-disaster-recovery-continuity.md)的恢复设计；不要让 CDC 直接成为全公司的业务事件总线，除非你有能力治理 schema 和语义。

## 方向 D：云队列互操作

云队列的语义和 AMQP 差异很大，桥接时最容易踩的是“可见性超时”和“删除”。

| 维度 | RabbitMQ | SQS | SNS/EventBridge | Pub/Sub |
|---|---|---|---|---|
| 确认 | ack/nack | delete + visibility timeout | 订阅确认/重试策略 | ack deadline |
| 重复 | 至少一次 | 标准队列至少一次 | 至少一次 | 至少一次 |
| 顺序 | 队列内 | FIFO message group | 通常不保证 | 通常不保证 |
| 保留 | 队列内直到 ack | 默认 4 天，可到 14 天 | 取决于目标 | 默认 7 天 |
| 大小上限 | 受 `max_message_size` 限制 | 256 KB | 256 KB | 10 MB |
| 路由 | exchange/binding | 队列名 | 事件规则 | topic/subscription |

### 桥接 worker 的状态机

```text
receive(SQS)
  -> transform
  -> publish to RabbitMQ + confirm
  -> delete(SQS receipt handle)
```

如果 delete 失败，SQS 会在可见性超时后重投，产生重复；如果 publish 成功但 delete 前崩溃，也会重复。目标端必须按事件 ID 幂等。反过来，RabbitMQ → SQS 时，SQS 的 `SendMessage` 成功后再 ack 源消息；不要因为 SQS 返回 200 就认为消费者已经处理。

### 顺序与并发

- SQS FIFO 用 `MessageGroupId` 保证组内顺序；桥的并发要按 group 串行；
- 标准队列没有顺序，不要在上层假设；
- SNS/EventBridge 到多个目标时，各目标独立重试，顺序更不可依赖；
- 云队列的 DLQ 需要单独配置 redrive，且 redrive 不是“重放历史”的通用工具。

### 成本与身份

跨云桥接还要算：请求费、数据传出费、NAT/专线、跨区域复制、日志和 DLQ 存储。身份用 IAM role/Workload Identity，不要把长期 access key 写进桥配置；网络优先私网端点。

## 方向 E：协议桥（AMQP 1.0 / MQTT / STOMP / JMS）

协议桥比消息系统桥更容易“看起来能连上，语义却不对”。常见映射：

| 概念 | AMQP 0-9-1 | AMQP 1.0 | MQTT | STOMP | JMS |
|---|---|---|---|---|---|
| 目标 | exchange + routing key | node/address | topic | destination | queue/topic |
| 确认 | basic.ack | settlement | PUBACK/PUBREC | ACK/NACK | acknowledge |
| 顺序 | 队列内 | link 内 | 通常按客户端 | 通常按连接 | session 内 |
| 持久 | delivery_mode=2 | durable header | QoS/retained | header | persistent |
| 过期 | TTL | absolute expiry | expiry interval | header | expiration |
| 事务 | tx class | transaction | 无原生 | 有限 | transacted session |

几个高频坑：

- MQTT 的 `/` 与 AMQP 的 `.` 不是同一种分隔符，通配符语义也不同；
- MQTT 的 retained message 在 AMQP 没有直接等价物；
- AMQP 1.0 的 settlement 是 link 级语义，不能简单映射成 0-9-1 的 delivery tag；
- JMS 的 durable subscription、事务 session、消息选择器在 AMQP 侧需要显式建模；
- QoS 2 只保证客户端与 broker 之间的链路，不保证端到端业务只执行一次。

优先使用 broker 原生支持或成熟网关；不要为了“统一协议”手写帧解析。跨协议测试要用官方客户端，并覆盖属性丢失、重复、断开重连和不可路由。

## 桥接器的可靠性状态机

把桥写成一个显式状态机，比写成一串回调更容易验证：

```text
        ┌─────────┐
        │ receive │
        └────┬────┘
             ▼
        ┌─────────┐   失败/不可转换
        │transform│──────────────> DLQ / parking
        └────┬────┘
             ▼
        ┌─────────┐   失败/超时
        │  write  │──────────────> retry with backoff
        └────┬────┘
             ▼
        ┌─────────┐
        │ confirm │
        └────┬────┘
             ▼
        ┌─────────┐
        │ ack/src │
        └────┬────┘
             ▼
        ┌─────────┐
        │checkpoint│
        └─────────┘
```

### 幂等键怎么选

| 键 | 稳定性 | 适用 | 风险 |
|---|---|---|---|
| 源端消息 ID | 高 | RabbitMQ `message_id`、Kafka record key | 生产者可能不设置/重复使用 |
| 源端位置 | 高 | Kafka `(topic, partition, offset)`、CDC `(lsn)` | 重放/重建后位置变化 |
| 业务键 + 版本 | 高 | 订单 ID + 状态版本 | 需要业务定义版本 |
| 内容哈希 | 中 | 无 ID 的旧系统 | 同内容不同事件会被误判 |
| 随机 UUID | 低 | 不推荐 | 每次重试都变，无法去重 |

去重表要设置保留期，至少覆盖最大重试/重放窗口；高吞吐场景按 key 分区，避免单表成为瓶颈。第 19 章的 inbox/幂等表设计可以直接复用。

### 并发与顺序

- 按顺序键把消息分给固定 worker/partition；
- 同一键同时只有一个在途消息；
- 失败重试不阻塞其他键（除非业务要求全局顺序）；
- rebalance/扩容时先排空在途消息，再切换；
- 批量写入时，整批成功后再 ack，部分失败要能定位到具体消息。

### 毒消息与重放

桥的 DLQ 要保留：原始消息体、源端元数据、转换后消息、错误类型/栈、尝试次数、桥版本、时间。重放工具要支持按时间/键/错误类型筛选、限速、幂等、暂停和回滚。没有重放工具的 DLQ 只是一个垃圾桶。

## 可观测性与 SLO

桥接器至少暴露以下指标：

| 指标 | 含义 | 告警示例 |
|---|---|---|
| source lag | 源端未处理消息数/最老消息年龄 | 持续 10 分钟上升 |
| checkpoint age | 上次成功 checkpoint 的时间 | 超过 5 分钟 |
| target latency p99 | 写入目标耗时 | 超过 SLO |
| error rate | 转换/写入失败比例 | 5 分钟 > 1% |
| duplicate rate | 目标端去重命中比例 | 突增 10 倍 |
| DLQ rate | 进入 parking 的速率 | 连续 3 个窗口 > 0 |
| rebalance count | Kafka consumer 再均衡次数 | 频繁抖动 |
| inflight | 已读未确认消息数 | 接近 prefetch/内存上限 |

追踪要把 `traceparent` 从源端 header 传到目标端 header，并为 receive/transform/write/ack 各打一个 span。事件 ID 要贯穿日志、指标和 DLQ，否则排障时无法把一条消息从源端追到目标端。

对账是最后一道防线：按时间窗口比较源端发布数、桥处理数、目标写入数、业务表数；抽样比对 payload 哈希；差异要能定位到具体事件 ID。

## 迁移 playbook

| 阶段 | 动作 | 退出标准 |
|---|---|---|
| 1. 盘点 | 列出交换机/队列/topic/消费者/owner/流量/保留期 | 拓扑和依赖图完整 |
| 2. 契约 | 定义事件 ID、schema、顺序键、DLQ 语义 | 生产/消费双方签字 |
| 3. 影子 | 双写/双读，目标端只观察不切换 | 差异率在阈值内 |
| 4. 灰度 | 按租户/流量比例切换，保留回滚开关 | SLO 达标，无数据丢失 |
| 5. 切换 | 停止旧路径，清理双写 | 对账连续 N 天一致 |
| 6. 退役 | 删除旧拓扑/权限/监控 | 无消费者、无告警 |

RabbitMQ 与 Kafka 的概念映射：

| RabbitMQ | Kafka | 注意 |
|---|---|---|
| exchange | topic | 路由语义不同，不能 1:1 照搬 |
| queue | consumer group + partition | 队列是竞争消费，partition 是并行单位 |
| routing key | record key | key 决定分区和顺序 |
| binding | subscription/filter | Kafka 主要靠消费端过滤 |
| DLX | DLQ topic | 重试/重放工具不同 |
| publisher confirm | acks=all | 都不等于消费成功 |
| consumer ack | offset commit | 提交时机决定重复窗口 |
| prefetch | max.poll.records | 都影响在途量和内存 |
| quorum queue | replication | 复制单位和故障模型不同 |

迁移时不要只复制拓扑，要重新设计语义：原来的“一个大队列 + 多消费者”在 Kafka 里可能需要按 key 分区；原来的“topic 通配路由”在 Kafka 里可能需要多个 topic 或消费端过滤。

## 反模式

1. **先 ack 再写目标**：目标失败就丢消息。
2. **produce 后不等待确认就 commit offset**：Kafka 缓冲区/网络失败会丢。
3. **桥没有幂等键**：每次重试都产生新记录。
4. **把 CDC 当业务事件总线**：schema 泄漏、语义不清、删除/快照难处理。
5. **把 Kafka 当任务队列**：单条 ack、优先级、延迟、RPC 都很别扭。
6. **把 RabbitMQ 当日志**：ack 即删除，重放要靠 Streams/归档。
7. **无限重试**：毒消息阻塞分区/队列，拖垮整个桥。
8. **一个桥服务所有域**：故障域、权限、schema、扩缩容全部耦合。
9. **没有顺序键**：源端有序、目标端随机分区。
10. **只监控桥进程存活**：不看 lag、checkpoint age、DLQ 和重复率。

## 检查清单

### 设计

- [ ] 明确源端/目标端的投递语义、顺序边界、保留期；
- [ ] 定义稳定事件 ID、顺序键、租户键；
- [ ] 画出崩溃矩阵，逐点写恢复动作；
- [ ] 确定 DLQ/parking 的 owner、保留期、重放工具；
- [ ] 明确“桥不可用时”是积压、降级还是拒绝。

### 实现

- [ ] 目标写入成功后才 ack/delete；
- [ ] 发布确认后才 commit offset；
- [ ] 目标端幂等（唯一键/upsert/去重表）；
- [ ] 有界重试 + 退避 + 最大次数；
- [ ] 支持暂停/恢复源端读取；
- [ ] 批量写入的整批确认与部分失败处理；
- [ ] 优雅关闭：停止读取、排空在途、提交 checkpoint。

### 运维

- [ ] source lag、checkpoint age、error rate、DLQ rate 告警；
- [ ] 追踪贯穿源端/桥/目标端；
- [ ] 对账任务和差异处理流程；
- [ ] 重放/回填限速，不影响在线流量；
- [ ] 桥版本、配置、schema 纳入版本控制；
- [ ] 定期做目标不可用/桥崩溃/重复消息演练。

### 安全

- [ ] 源端/目标端最小权限；
- [ ] TLS/私网端点；
- [ ] 凭据轮换，不写死在配置；
- [ ] 多租户隔离与配额；
- [ ] 审计谁在何时重放/删除了消息。

## 小结

桥接的核心不是“连上”，而是“在崩溃、重复、乱序和目标不可用时仍然保持业务语义”。先做语义映射，再选拓扑；把桥写成“读 → 转换 → 写目标 → 确认 → ack/commit → checkpoint”的状态机；用稳定事件 ID 和目标端幂等吸收至少一次；用 lag、checkpoint age、DLQ 和重复率证明它真的在工作。

RabbitMQ 在这个组合里通常承担“业务命令/任务/路由/确认”的角色，Kafka 承担“保留/重放/流处理”，CDC 负责“数据库变更”，云队列负责“托管与弹性”。它们不是互相替代，而是各自在自己的语义边界内工作。`examples/20_bridge_interop.py` 展示了最小闭环；把它扩展到真实 Kafka/SQS 时，先补上本附录的崩溃矩阵和检查清单，再谈吞吐。

相关阅读：[附录 D](D-comparison.md) 的产品对比、[附录 O](O-event-contracts-governance.md) 的事件契约、[附录 P](P-application-testing-chaos.md) 的故障注入、[附录 R](R-amqp-1-0-deep-dive.md) 的 AMQP 1.0 细节、[附录 V](V-reference-architectures.md) 的企业集成案例、[第 13 章](../chapters/13-federation-shovel.md) 的 Federation/Shovel、[第 19 章](../chapters/19-end-to-end-reliability.md) 的 outbox 与幂等。

---

[返回目录](../README.md) | [上一章：附录 Y 消息系统合规与数据治理](Y-compliance-data-governance.md)
