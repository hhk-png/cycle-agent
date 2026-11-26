# 第 25 章 事件驱动：CloudEvents、AsyncAPI 与消息契约

## 25.1 事件是“已经发生的事实”

事件（Event）不是命令（Command），也不是查询（Query）：

- **命令**：请做某事，例如 `CancelOrder`；
- **查询**：请告诉我某事，例如 `GetOrder`；
- **事件**：某事已经发生，例如 `OrderCancelled`。

事件的关键特征是**过去式、不可变、可被多个消费者独立消费**。

```json
{
  "type": "com.example.order.cancelled",
  "source": "/orders/ord_123",
  "subject": "ord_123",
  "time": "2026-11-26T10:15:30Z",
  "data": {
    "orderId": "ord_123",
    "reason": "customer_requested",
    "cancelledAt": "2026-11-26T10:15:30Z"
  }
}
```

### 事件与命令的混淆

最常见的错误是把命令当事件广播：

```text
# 错误：事件里说“请退款”
{
  "type": "order.cancelled",
  "data": { "action": "refund" }
}

# 正确：事件说“订单已取消”，退款由消费者自行决定
{
  "type": "order.cancelled",
  "data": { "orderId": "ord_123", "reason": "customer_requested" }
}
```

事件生产者不应告诉消费者“做什么”，只应告诉“发生了什么”。否则消费者与生产者的业务逻辑耦合。

## 25.2 事件驱动架构的价值

- 解耦生产者与消费者；
- 异步削峰；
- 多消费者；
- 可回放；
- 审计；
- 跨服务最终一致；
- 实时分析；
- 故障隔离。

代价：

- 最终一致；
- 重复投递；
- 顺序问题；
- 调试困难；
- Schema 演进；
- 消费者治理；
- 可观测性复杂；
- 事件风暴；
- 数据所有权模糊。

### 什么时候适合事件驱动

- 多个下游需要感知同一事实；
- 生产者不应知道消费者；
- 可以接受异步；
- 需要回放/审计；
- 需要削峰。

### 什么时候不适合

- 需要立即返回结果；
- 需要强一致读；
- 团队没有幂等、去重、死信能力；
- 业务简单，同步调用足够；
- 事件量小，维护成本高于收益。

## 25.3 事件设计原则

### 1. 以事实命名

```text
OrderCreated
OrderPaid
OrderCancelled
PaymentRefunded
InventoryReserved
```

不要用：

```text
UpdateOrder
OrderChanged
RowUpdated
```

后者暴露实现细节，消费者无法理解业务含义。

### 2. 事件应自包含

消费者不应为了理解事件而回调生产者。事件应包含：

- 事件 ID；
- 类型；
- 版本；
- 发生时间；
- 聚合 ID；
- 最小必要业务数据；
- 租户/组织 ID；
- 关联 ID；
- 生产者标识；
- schema URL。

### 3. 薄事件 vs 胖事件

- **薄事件**：只含 ID 和类型，消费者需查询详情；
- **胖事件**：包含完整快照，消费者无需回调。

| 类型 | 优点 | 缺点 |
|---|---|---|
| 薄 | 小、避免泄露、消费者可获取最新状态 | 需要额外查询、耦合 API |
| 胖 | 自包含、消费简单 | 大、可能过期、泄露风险 |

**建议**：包含消费者完成任务所需的最小字段；对于需要完整状态的场景，提供快照 API。

### 4. 事件不可变

事件发布后不应修改。修正应通过新事件（如 `OrderCorrected`）表达。

### 5. 版本化

```json
{
  "type": "com.example.order.paid",
  "specversion": "1.0",
  "datacontenttype": "application/json",
  "dataschema": "https://schemas.example.com/order/paid/v2"
}
```

### 6. 事件应可重放

- 消费者能从历史事件重建状态；
- 事件包含足够信息；
- 事件顺序可恢复；
- 重放不影响生产。

### 7. 事件应有明确所有权

- 谁生产；
- 谁维护 schema；
- 谁负责兼容性；
- 谁处理消费者问题。

## 25.4 CloudEvents

CloudEvents 是 CNCF 的事件信封标准：

```json
{
  "specversion": "1.0",
  "id": "evt_01J9Z6...",
  "source": "https://api.example.com/orders",
  "type": "com.example.order.paid",
  "subject": "ord_123",
  "time": "2026-11-26T10:15:30Z",
  "datacontenttype": "application/json",
  "dataschema": "https://schemas.example.com/order/paid/v1",
  "data": {
    "orderId": "ord_123",
    "amount": 12900,
    "currency": "CNY"
  }
}
```

优点：

- 统一信封；
- 跨协议（HTTP、Kafka、MQTT、AMQP）；
- 标准化元数据；
- 易于路由与追踪；
- 与供应商解耦。

### 必填字段

- `specversion`；
- `id`；
- `source`；
- `type`。

### 常用扩展

- `subject`；
- `time`；
- `datacontenttype`；
- `dataschema`；
- `traceparent`；
- `partitionkey`。

### CloudEvents 的绑定

- HTTP；
- Kafka；
- MQTT；
- AMQP；
- NATS。

绑定定义了如何在特定协议中编码事件。

## 25.5 AsyncAPI

AsyncAPI 描述事件驱动 API：

```yaml
asyncapi: 3.0.0
info:
  title: Order Events
  version: 1.0.0
channels:
  orderCreated:
    address: orders.created
    messages:
      orderCreated:
        $ref: '#/components/messages/OrderCreated'
operations:
  publishOrderCreated:
    action: send
    channel:
      $ref: '#/channels/orderCreated'
components:
  messages:
    OrderCreated:
      payload:
        type: object
        required: [orderId, customerId, total]
        properties:
          orderId: { type: string }
          customerId: { type: string }
          total:
            $ref: '#/components/schemas/Money'
  schemas:
    Money:
      type: object
      required: [value, currency]
      properties:
        value:
          type: integer
          description: 最小货币单位，例如分
        currency:
          type: string
          pattern: '^[A-Z]{3}$'
          description: ISO 4217 币种
```

AsyncAPI 可以生成：

- 文档；
- 代码；
- mock；
- schema 校验；
- 契约测试。

### AsyncAPI 的价值

- 事件契约可版本化；
- 生产者/消费者可独立开发；
- 工具可校验兼容性；
- 文档可自动生成；
- 与 OpenAPI 类似的治理。

## 25.6 消息系统

### Kafka

- 分布式日志；
- 分区与顺序；
- 消费者组；
- 可回放；
- 高吞吐；
- 持久化；
- 生态成熟。

适合：事件流、日志、分析、事件溯源。

#### 日志语义

Kafka 不是“消息队列”，而是分区日志。理解下面几个概念，才能写出正确的事件契约：

- **分区是顺序单位**：同一分区内按 offset 有序，跨分区无序；分区键决定事件去哪个分区，通常用聚合 ID（`order_id`、`tenant_id`）。
- **offset 是消费者位置**：消费者组各自维护 offset；lag = 最新 offset - 已提交 offset；lag 持续增长说明消费能力不足。
- **保留 vs 压缩**：普通主题按时间/大小保留；压缩主题（compacted）按 key 保留最新值，并用 tombstone 表示删除，适合“状态快照”类主题（如用户资料、配置）。
- **重平衡**：消费者加入/离开会触发分区重新分配；重平衡期间可能重复消费，消费者必须幂等。
- **持久性**：`acks=all` + `min.insync.replicas` 决定写入需要多少副本确认；它影响延迟与丢数据风险，是契约的一部分。
- **事务与 exactly-once**：Kafka 事务可以保证“读-处理-写”在 Kafka 内部原子，但外部副作用（数据库、HTTP 调用）仍需要幂等；不要把它理解成端到端 exactly-once。
- **分层存储**：冷数据下沉到对象存储，降低保留成本，但回放延迟上升；重放窗口是契约的一部分。

事件契约要写清：主题名、分区键、保留期、压缩策略、最小同步副本、schema 版本、消费者 lag SLO。

### Pulsar

- 多租户；
- 分层存储；
- 队列与流统一；
- geo-replication；
- 计算存储分离。

### NATS / JetStream

- 轻量；
- 低延迟；
- 请求—响应与流；
- 适合云原生；
- 运维简单。

### RabbitMQ

- 灵活路由；
- 队列；
- 成熟生态；
- 适合任务队列；
- 消息确认；
- 死信队列。

### 云服务

- AWS SNS/SQS/EventBridge；
- GCP Pub/Sub；
- Azure Service Bus/Event Grid。

选择取决于：

- 吞吐；
- 顺序；
- 保留；
- 回放；
- 多租户；
- 运维能力；
- 成本；
- 生态。

### 消息系统对比

| 系统 | 吞吐 | 顺序 | 回放 | 运维 | 典型 |
|---|---|---|---|---|---|
| Kafka | 极高 | 分区内 | 是 | 高 | 事件流 |
| Pulsar | 高 | 分区内 | 是 | 高 | 多租户流 |
| NATS | 高 | 有限 | JetStream | 低 | 云原生 |
| RabbitMQ | 中 | 队列内 | 有限 | 中 | 任务队列 |
| SQS | 中 | 有限 | 否 | 低 | 简单队列 |

## 25.7 投递语义

- **至多一次**：可能丢失，不重复；
- **至少一次**：不丢失，可能重复；
- **恰好一次**：通常只在特定边界内成立（如 Kafka 事务 + 幂等生产者 + 事务消费者）。

现实中的“恰好一次”往往是“至少一次 + 幂等消费”。

### 消费者幂等

```text
if event.id already processed:
    ack and skip
else:
    process in transaction with event.id
    ack
```

需要：

- 去重表；
- 事务性写入；
- TTL/清理；
- 处理并发重复；
- 幂等键；
- 唯一约束。

### 去重表设计

```sql
CREATE TABLE processed_events (
  consumer_id TEXT NOT NULL,
  event_id    TEXT NOT NULL,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer_id, event_id)
);
```

`consumer_id` 用于区分不同消费者组，避免互相干扰。

## 25.8 顺序

- Kafka 只保证分区内顺序；
- 相同聚合 ID 应路由到同一分区；
- 跨分区无序；
- 消费者并行处理会打乱顺序；
- 重试可能造成乱序。

如果业务需要顺序：

- 按聚合 ID 分区；
- 单消费者处理；
- 版本号/序列号；
- 拒绝旧版本事件；
- 处理乱序缓冲；
- 使用事件流而非 Webhook。

### 顺序与并发的权衡

- 单分区单消费者：有序但吞吐低；
- 多分区：吞吐高但无序；
- 按聚合分区：同一聚合有序，不同聚合并行。

大多数业务只需要“同一聚合有序”，不需要全局有序。

## 25.9 Schema 演进

事件 schema 演进比 API 更严格，因为消费者可能长期不升级。

规则：

- 新增可选字段；
- 不删除/重命名字段；
- 不改变类型；
- 枚举新增值需消费者宽容；
- 使用 schema registry；
- 兼容性策略（backward/forward/full）；
- 版本化事件类型；
- 不要改变字段语义。

```text
com.example.order.paid.v1
com.example.order.paid.v2
```

### 兼容性类型

- **Backward**：新 schema 能读旧数据；
- **Forward**：旧 schema 能读新数据；
- **Full**：双向兼容。

事件通常要求 full 兼容，因为消费者可能滞后。

### Schema Registry

- 集中管理 schema；
- 版本；
- 兼容性检查；
- 生产者/消费者注册；
- 序列化格式（Avro/Protobuf/JSON Schema）；
- 演进策略。

### 事件也是数据契约

事件流一旦被多个下游消费，它就不只是“服务间通知”，而是一份数据契约。除了 schema，还要约定：

| 要素 | 问题 |
|---|---|
| 所有权 | 谁生产、谁维护 schema、谁负责兼容性 |
| 语义 | 事件字段的业务含义、金额单位、时间归属 |
| 新鲜度 | 端到端延迟、数据截止时间、迟到事件窗口 |
| 质量 | 空值率、重复率、顺序保证、丢失告警 |
| 生命周期 | 保留期、重放窗口、废弃计划、迁移路径 |
| 消费者 | 谁在消费、消费 lag SLO、失败联系人 |

事件目录（event catalog）应记录：

```yaml
event: com.example.order.paid
version: 2
owner: orders-team
topic: orders.paid.v2
partitionKey: order_id
retention: 30d
compatibility: full
freshness: p95 < 5s
consumers:
  - billing
  - analytics
  - notification
```

没有目录的事件系统，半年后没人知道哪个主题还有人用、哪个字段能删。数据契约与指标口径的完整方法见[附录 AB](../appendices/ab-data-contracts-metrics.md)。

## 25.10 Outbox、Saga 与事件溯源

### Outbox

业务表与事件表同一事务写入，异步发布：

```text
DB transaction:
  insert order
  insert outbox event
→ publisher reads outbox
→ publish to broker
→ mark sent
```

解决“写库成功、发消息失败”。

### Saga

长事务通过一系列本地事务与补偿完成：

```text
CreateOrder → ReserveInventory → ChargePayment → ConfirmOrder
                  ↓ fail            ↓ fail
              ReleaseInventory   RefundPayment
```

需要：

- 每步幂等；
- 补偿幂等；
- 超时；
- 人工介入；
- 状态查询；
- 补偿失败处理。

### 事件溯源

存储事件而不是当前状态：

```text
OrderCreated → OrderPaid → OrderShipped
```

优点：

- 完整审计；
- 可回放；
- 可重建读模型；
- 天然事件驱动。

缺点：

- 复杂；
- 事件版本管理；
- 快照；
- 查询需要投影；
- 隐私删除困难；
- 事件 schema 演进难；
- 存储增长。

### 三者对比

| 模式 | 解决的问题 | 复杂度 | 适用 |
|---|---|---|---|
| Outbox | 写库与发消息原子 | 中 | 大多数事件发布 |
| Saga | 跨服务长事务 | 高 | 订单/支付流程 |
| 事件溯源 | 状态即事件序列 | 很高 | 审计/金融/复杂领域 |

## 25.11 死信与重试

- 失败重试；
- 指数退避；
- 最大重试次数；
- 死信队列（DLQ）；
- 告警；
- 人工重放；
- poison message 处理；
- 重试预算；
- 避免无限重试。

### 死信处理

```text
1. 消息进入 DLQ
2. 告警
3. 人工检查
4. 修复后重放
5. 记录原因
```

DLQ 不应成为“垃圾场”，需要定期清理和复盘。

## 25.12 可观测性

- 事件生产/消费速率；
- 端到端延迟；
- 消费者 lag；
- 失败率；
- 重试次数；
- DLQ 数量；
- schema 版本分布；
- trace 关联；
- 每租户事件量；
- 事件大小分布。

### 消费者 lag

Lag = 最新偏移量 - 消费者偏移量。持续增长说明：

- 消费者太慢；
- 分区不均；
- 下游故障；
- 需要扩容。

## 25.13 本章小结

- 事件表达已发生的事实，不是命令。
- 事件应自包含、不可变、可版本化。
- CloudEvents 提供标准信封，AsyncAPI 提供契约描述。
- 投递通常是至少一次，消费者必须幂等。
- 顺序只在分区内保证。
- Schema 演进必须严格。
- 事件流是数据契约：除 schema 外还要有 owner、新鲜度、质量、保留期与消费者目录。
- Outbox/Saga/事件溯源解决不同的一致性问题。
- 死信、重试、可观测性是生产必备。
- 跨区域事件的顺序、一致性与故障转移，见[附录 Z](../appendices/z-distributed-consistency-multi-region.md)；数据契约与指标口径见[附录 AB](../appendices/ab-data-contracts-metrics.md)。
- 事件 schema 的序列化格式、兼容性规则与 Schema Registry 落地，见[附录 AE](../appendices/ae-serialization-schema-evolution.md)。

## 设计练习

为“电商订单系统”设计事件：

- 订单创建、支付、发货、取消、退款；
- 库存预占与释放；
- 通知服务；
- 数据分析；
- 对账系统。

写出事件目录、CloudEvents 信封、分区键、幂等消费和 DLQ 策略。然后回答：

1. 订单支付事件应该包含哪些字段？
2. 库存服务如何避免重复扣减？
3. 消费者 lag 增长时如何处理？
4. 事件 schema 新增字段，旧消费者如何兼容？

---

上一章：[GraphQL：灵活查询与治理成本](24-graphql.md)  
下一章：[Webhook 工程：签名、重试、去重与可观测](26-webhooks.md)
