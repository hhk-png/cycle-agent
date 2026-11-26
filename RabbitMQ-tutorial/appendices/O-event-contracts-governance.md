# 附录 O 事件契约、Schema 治理与事件目录

## 为什么“消息格式”不等于“契约”

很多团队在文档里写一句“消息是 JSON”，就认为契约已经定义完毕。真正的事件契约至少包含六件事：

1. **语义**：这个事件表示什么事实，什么时候产生，是否可重复；
2. **结构**：字段名、类型、必填/可选、枚举、单位、时区；
3. **标识**：事件 ID、业务键、关联 ID、因果 ID；
4. **兼容性**：哪些变更向后兼容，哪些必须升版本；
5. **所有权**：谁生产、谁消费、谁批准变更、谁 on-call；
6. **生命周期**：何时引入、何时弃用、保留多久、如何迁移。

只定义第 2 项，消费者仍然会在第一次加字段、改类型或改语义时崩溃。契约治理的目标不是“写一份漂亮的 schema”，而是让**变更可预测、可验证、可回滚**。

## 事件、命令与文档：先分清契约类型

| 类型 | 语义 | 典型命名 | 契约重点 | 投递模型 |
|---|---|---|---|---|
| 事件 | 已经发生的事实 | `order.created` | 事实不可变、可重复、可重放 | 交换机广播/主题订阅 |
| 命令 | 请执行一个动作 | `inventory.reserve` | 目标明确、可能失败、需要回执 | 队列 + 竞争消费者 |
| 文档/状态 | 某个实体的当前快照 | `order.snapshot` | 版本、覆盖语义、冲突解决 | 队列/Stream/API |

事件是“过去时”，命令是“祈使句”。把命令命名为事件（例如 `order.create`）会让消费者误以为“事情已经完成”，从而忽略失败和重试。命名不是风格问题，而是契约语义的一部分。

## 命名规范

一个可执行的命名约定比“大家自觉”更有用：

```text
<domain>.<entity>.<past-tense-verb>[.<version>]
```

示例：

```text
order.order.created
payment.payment.captured
inventory.stock.reserved
```

实践建议：

- 事件类型用**过去时**，命令用**祈使式**；
- 领域/实体用小写点分，避免空格、斜杠、中文；
- 版本优先放在 envelope 的 `schema_version`，不要放进 routing key（否则所有绑定都要改）；
- routing key 可以与事件类型一致，也可以做更粗的聚合（`order.#`）；
- 事件类型一旦发布就视为公开 API，改名等于破坏性变更；
- 不要用自增数字当事件类型（`event.123`），消费者无法从名字理解语义。

RabbitMQ 的 topic 交换机把 `.` 当分隔符、`*` 匹配一段、`#` 匹配零到多段。命名规范要同时考虑路由：

```text
orders (topic)
  order.created       -> inventory.order-created
  order.#             -> audit.all-orders
  payment.*.captured  -> finance.captures
```

如果事件类型里包含业务 ID 或时间戳，绑定会爆炸式增长，routing key 也会失去可枚举性。把高基数信息放进 headers 或 payload，而不是 routing key。

## Envelope：把“事件元数据”与“业务载荷”分开

推荐把所有事件共有的字段放进 envelope，业务字段放进 `data`：

```json
{
  "specversion": "1.0",
  "id": "evt-01J8Z6K3V9",
  "source": "order-service",
  "type": "order.created",
  "subject": "orders/order-100",
  "time": "2026-11-26T10:15:30Z",
  "datacontenttype": "application/json",
  "dataschema": "https://schemas.example.com/order/created/2.1.json",
  "tenant_id": "tenant-42",
  "correlation_id": "req-7f3c",
  "causation_id": "evt-01J8Z5...",
  "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  "data": {
    "order_id": "order-100",
    "amount": {"currency": "CNY", "minor_units": 19900},
    "created_at": "2026-11-26T10:15:29Z"
  }
}
```

字段说明：

| 字段 | 作用 | 注意 |
|---|---|---|
| `id` | 事件唯一 ID | 全局唯一，重放时不变 |
| `type` | 事件类型 | 与 routing key/目录一致 |
| `source` | 生产者标识 | 服务名/实例域，不含敏感信息 |
| `subject` | 实体标识 | 便于按业务键过滤/分区 |
| `time` | 事件发生时间 | UTC、ISO 8601；不要用消费时间 |
| `dataschema` | schema 位置/版本 | 可解析、可缓存、可审计 |
| `correlation_id` | 一次业务请求的关联 | 跨服务追踪 |
| `causation_id` | 直接触发本事件的事件 | 构建因果链 |
| `traceparent` | W3C Trace Context | 与 OpenTelemetry 对齐 |
| `tenant_id` | 租户 | 多租户隔离/过滤/计费 |
| `data` | 业务载荷 | 只放事实，不放“请处理” |

不要把所有字段都塞进 AMQP headers：headers 适合路由/过滤/追踪，不适合承载大 payload。RabbitMQ 的 headers 交换机可以按 headers 匹配，但 headers 过多会增加内存和诊断成本。

## CloudEvents 映射

CloudEvents 是 envelope 的一种标准，不是投递协议。映射到 RabbitMQ 时：

| CloudEvents | AMQP 0-9-1 位置 | 说明 |
|---|---|---|
| `id` | `message_id` 或 payload | 建议同时保留在 payload |
| `type` | routing key / `type` 属性 / payload | 三者要一致 |
| `source` | `app_id` 或 payload | 便于审计 |
| `subject` | routing key / payload | 高基数时不要放 routing key |
| `time` | `timestamp` 属性 / payload | 秒级属性 vs 毫秒 payload |
| `datacontenttype` | `content_type` | `application/json` |
| `dataschema` | payload/header | 不要依赖客户端自动传递 |
| `traceparent` | headers | W3C 标准 |

CloudEvents 解决“元数据怎么统一”，不解决“消息是否重复、是否有序、是否丢失”。用了 CloudEvents 仍然需要幂等、DLQ、对账和 schema 兼容性。

## Schema 选型：JSON Schema、Avro、Protobuf

| 方案 | 优点 | 代价 | 适用 |
|---|---|---|---|
| JSON Schema | 人类可读、工具多、与 HTTP 生态一致 | 体积大、运行时校验成本、弱类型 | 业务事件、跨语言、调试友好 |
| Avro | 紧凑、schema 演进规则成熟、适合大数据 | 需要 registry、二进制不可读 | 数据管道、Kafka 生态 |
| Protobuf | 紧凑、强类型、代码生成成熟 | 需要 `.proto` 管理、默认值语义 | 高吞吐、多语言、gRPC 生态 |
| 无 schema 的 JSON | 上手快 | 变更不可控、测试困难 | 只适合原型/内部临时消息 |

RabbitMQ 不强制 schema。你可以：

- 在 CI 中用 JSON Schema 校验生产者输出；
- 把 schema 存进 Git/对象存储/自建 registry；
- 在 `dataschema` 中放可解析的版本 URL；
- 在消费者启动时做兼容性检查（而不是等第一条消息失败）。

一个最小 JSON Schema：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://schemas.example.com/order/created/2.1.json",
  "type": "object",
  "required": ["id", "type", "source", "time", "data"],
  "properties": {
    "id": {"type": "string", "minLength": 1},
    "type": {"const": "order.created"},
    "source": {"type": "string"},
    "time": {"type": "string", "format": "date-time"},
    "data": {
      "type": "object",
      "required": ["order_id", "amount"],
      "properties": {
        "order_id": {"type": "string"},
        "amount": {
          "type": "object",
          "required": ["currency", "minor_units"],
          "properties": {
            "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
            "minor_units": {"type": "integer", "minimum": 0}
          },
          "additionalProperties": false
        }
      },
      "additionalProperties": true
    }
  },
  "additionalProperties": true
}
```

注意 `additionalProperties: true` 与 `false` 的取舍：生产者侧严格、消费者侧宽松，通常更利于演进。消费者应忽略未知字段，而不是因为多了一个字段就拒绝消息。

## 兼容性规则

把变更分成“安全”“需过渡”“必须升版本”三类：

| 变更 | 兼容性 | 做法 |
|---|---|---|
| 新增可选字段 | 向后兼容 | 消费者忽略未知字段；生产者默认值明确 |
| 新增必填字段 | 破坏性 | 新版本事件/双写过渡 |
| 删除可选字段 | 可能破坏 | 先弃用、观察消费方、再删除 |
| 删除必填字段 | 破坏性 | 新版本 |
| 改字段类型 | 破坏性 | 新字段/新版本，避免隐式转换 |
| 改字段语义/单位 | 破坏性 | 新字段名/新版本；不要“同名不同义” |
| 改枚举值 | 视消费者 | 消费者必须容忍未知枚举；否则升版本 |
| 重命名字段 | 破坏性 | 新旧并存过渡 |
| 调整嵌套结构 | 视情况 | 用兼容性测试验证 |

“向后兼容”常被误解。至少区分：

- **向后兼容**：新生产者 + 旧消费者可以工作；
- **向前兼容**：旧生产者 + 新消费者可以工作；
- **完全兼容**：两个方向都可以。

事件驱动系统通常要求“新生产者 + 旧消费者”先成立，因为消费者升级总是更慢。发布顺序应是：先升级消费者（容忍新字段/新版本），再升级生产者。

## 版本策略

三种常见策略：

1. **信封版本**：`schema_version: "2.1"`，事件类型不变；消费者按版本分支。
2. **类型版本**：`order.created.v2`；routing key/绑定可能变化，迁移成本高。
3. **并行事件**：新旧事件同时发布一段时间，消费者逐步迁移。

推荐默认用信封版本，只有在语义发生根本变化时才用类型版本。不要把版本放进每个字段名（`amount_v2`），那会让 schema 变成历史包袱。

版本的生命周期要写清楚：

```text
v1: 2025-01 引入
v1: 2026-06 标记 deprecated（仍发布）
v2: 2026-06 引入，双写
v2: 2026-12 停止发布 v1
v2: 2027-03 删除 v1 schema/消费者分支
```

没有“删除日期”的弃用公告，等于没有弃用。

## AsyncAPI：契约先行的载体

AsyncAPI 可以把通道、消息、schema、绑定写在一起。一个最小示例：

```yaml
asyncapi: 3.0.0
info:
  title: Order Events
  version: 2.1.0
channels:
  orderCreated:
    address: order.created
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
      name: OrderCreated
      contentType: application/json
      payload:
        $ref: '#/components/schemas/OrderCreatedPayload'
  schemas:
    OrderCreatedPayload:
      type: object
      required: [id, type, source, time, data]
      properties:
        id: {type: string}
        type: {const: order.created}
        source: {type: string}
        time: {type: string, format: date-time}
        data:
          type: object
          required: [order_id, amount]
          properties:
            order_id: {type: string}
            amount:
              type: object
              required: [currency, minor_units]
              properties:
                currency: {type: string}
                minor_units: {type: integer}
```

AsyncAPI 的价值在于：

- 代码/文档/测试共享同一份契约；
- CI 可以检测破坏性变更；
- 事件目录可以自动生成；
- 跨团队评审有具体对象。

但它不是运行时保证。发布时仍要校验 schema，消费时仍要容错。

## 契约测试

契约测试分三层：

### 生产者侧

- 用 schema 校验每条测试消息；
- 用固定样例覆盖必填/可选/边界值；
- 检查 `id` 唯一、`time` 可解析、`type` 与 routing key 一致；
- 检查不泄露 PII/密钥；
- 检查消息大小在 `max_message_size` 以内。

### 消费者侧

- 用“历史消息样本”回放，确保新消费者能读旧事件；
- 用“未来兼容样本”（多字段/未知枚举）确保忽略未知字段；
- 用损坏消息验证 DLQ/错误处理；
- 用重复/乱序样本验证幂等/版本号。

### 跨团队

- 生产者变更前，消费者在 CI 跑契约测试；
- 破坏性变更需要消费者签字/迁移计划；
- 契约测试失败阻塞合并，而不是“上线后再说”。

一个最小的消费者容错测试矩阵：

| 输入 | 期望 |
|---|---|
| 未知字段 | 忽略并成功 |
| 缺失可选字段 | 使用默认值 |
| 缺失必填字段 | 拒绝/进 DLQ，错误可诊断 |
| 未知枚举值 | 记录并跳过/降级，不崩溃 |
| 旧 schema 版本 | 按兼容逻辑处理 |
| 超大消息 | 发布前拒绝，不进入 broker |
| 重复 message_id | 幂等处理，副作用一次 |

## 事件目录

事件目录不是“Excel 清单”，而是可查询的契约索引。至少包含：

| 字段 | 示例 |
|---|---|
| 事件类型 | `order.created` |
| 版本 | `2.1` |
| 所有者 | `order-platform` |
| 生产者 | `order-service` |
| 消费者 | `inventory`, `audit`, `notification` |
| schema | 仓库路径/URL |
| 示例 | 一条脱敏样例 |
| 兼容策略 | backward |
| 保留/重放 | Stream 30 天 / 队列不保留 |
| SLO | p99 2s |
| 弃用日期 | `2027-03-01` |
| 数据分级 | internal / PII |

目录要和代码同源：从 AsyncAPI/schema 生成，或在 CI 中校验目录与代码一致。手工维护的目录通常三个月后过期。

## 治理流程

一个可落地的最小流程：

```text
1. 提案：新事件/变更 -> 填模板（语义、schema、owner、消费者）
2. 评审：生产/消费者/安全/平台团队
3. 实现：schema + 生产者 + 消费者 + 测试
4. 预发：契约测试 + 回放 + 监控
5. 发布：先消费者后生产者；破坏性变更双写
6. 观测：生产/消费/延迟/DLQ/重复率
7. 弃用：公告 + 迁移窗口 + 删除
```

治理不是“审批越多越好”。关键是：

- 每个事件有 owner；
- 每个变更有人类可读的兼容性判断；
- 每个破坏性变更都有迁移路径；
- 每个季度清理无主/无消费者事件。

## 安全与隐私

事件契约也是安全边界：

- **数据分级**：public/internal/confidential/PII；不同级别用不同 vhost/加密/保留策略；
- **最小化**：不要因为“以后可能用”就把身份证/手机号/地址塞进事件；
- **脱敏**：日志、追踪、DLQ 中避免完整 PII；
- **加密**：传输 TLS；静态加密依赖存储/磁盘；敏感字段可应用层加密；
- **访问控制**：vhost + 用户权限 + topic authorization；不要让所有服务都能读所有事件；
- **保留**：PII 的保留期要可配置、可删除；
- **审计**：谁发布/订阅/重放了敏感事件。

不要把密钥、token、完整信用卡号放进 headers：headers 会出现在管理 UI、日志、追踪和 DLQ 中。

## 多租户

多租户事件系统有三种隔离级别：

| 级别 | 做法 | 隔离强度 | 成本 |
|---|---|---|---|
| vhost 隔离 | 每租户独立 vhost/用户/配额 | 强 | 运维对象多 |
| 共享拓扑 + tenant_id | 同一交换机/队列，payload/header 带租户 | 弱-中 | 便宜，需应用过滤 |
| 混合 | 大租户独立 vhost，小租户共享 | 中-强 | 复杂度高 |

共享拓扑时，消费者必须校验 `tenant_id`，不能只依赖 routing key。topic authorization 可以限制租户只能绑定自己的模式，但不要把它当成唯一隔离手段。噪声邻居问题用配额、独立 vhost、独立集群分层解决。

## RabbitMQ 拓扑映射

把契约落到 RabbitMQ：

```text
exchange: orders (topic, durable)
  binding: order.created -> queue inventory.order-created
  binding: order.#       -> queue audit.all-orders
  binding: order.created -> queue notification.order-created

exchange: orders.dlx (fanout, durable)
  binding -> queue orders.parking
```

选择队列类型：

- 任务/命令、需要 per-message ack：Quorum Queue；
- 事件广播、每消费者独立进度：每服务一个队列；
- 需要重放/多消费者独立 offset：Stream/Super Stream；
- 低延迟/临时/优先级：Classic Queue（接受其复制/持久化边界）。

消息大小：

- 4.0 起默认 `max_message_size` 为 16 MiB；
- 大文件放对象存储，事件只传 URI、校验和、版本；
- 契约里明确“这是引用还是内联”，避免消费者误以为 body 就是全量数据。

## 反模式

1. **God Event**：一个 `order.updated` 包含所有字段和所有变更类型，消费者无法判断发生了什么；拆成具体事实事件。
2. **事件当 RPC**：事件里带 `reply_to`、期望生产者等待结果；事件应异步、可重复。
3. **无 schema 的 JSON**：字段靠口头约定，三个月后没人知道 `status=3` 是什么意思。
4. **版本放 routing key**：`order.created.v2` 导致所有绑定/消费者迁移；优先信封版本。
5. **同名不同义**：`amount` 从元改成分，消费者静默算错；新字段名/新版本。
6. **消费者严格拒绝未知字段**：每次生产者加字段都造成故障；消费者应宽容。
7. **把 PII 写进 headers/日志**：管理 UI、追踪、DLQ 全量泄露。
8. **无 owner 的事件**：没人敢删、没人能改、没人 on-call。
9. **契约测试只在生产者侧**：消费者读旧事件/未知字段仍然崩溃。
10. **把 CloudEvents 当可靠性方案**：envelope 标准化不等于幂等/顺序/不丢。

## 契约评审清单

- [ ] 事件名是过去时，语义单一；
- [ ] 有 owner、生产者、消费者、SLO；
- [ ] 有 schema、示例、版本、兼容策略；
- [ ] 必填/可选/枚举/单位/时区明确；
- [ ] 有 `id`、`time`、`correlation_id`、`tenant_id`（如适用）；
- [ ] 消费者容忍未知字段/未知枚举；
- [ ] 破坏性变更已有迁移/双写/弃用计划；
- [ ] 大消息有引用方案，不超过 `max_message_size`；
- [ ] PII/密钥/敏感数据已分级、脱敏、限制保留；
- [ ] 契约测试在 CI 中运行；
- [ ] 事件目录已更新；
- [ ] DLQ/重放/对账路径已设计。

## 小结

事件契约不是一份 JSON 示例，而是语义、结构、标识、兼容性、所有权和生命周期的组合。先把事件/命令/文档分清，再用稳定命名和 envelope 统一元数据；schema 选型服从团队与生态，兼容性规则服从“消费者升级更慢”的现实；契约测试、事件目录和治理流程让变更可预测。RabbitMQ 提供路由、确认、DLQ、Stream 等机制，但契约的正确性只能由生产者和消费者共同保证。

下一篇附录把视角从“消息长什么样”转到“怎么证明它在故障下仍然正确”：应用层测试、集成测试、故障注入和混沌实验。

---

[返回目录](../README.md) | [上一章：附录 N Erlang/OTP 运行时、进程模型与调优](N-erlang-runtime.md) | [下一章：附录 P RabbitMQ 应用测试与混沌工程手册](P-application-testing-chaos.md)
