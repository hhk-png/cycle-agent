# 附录 V 生产参考架构案例集

> 本附录定位：把前面章节的协议、路由、可靠性、集群、运维知识组合成可讨论的架构。案例不是“照抄模板”，而是展示同一组积木在不同业务约束下如何取舍。每个案例都给出拓扑、可靠性链、失败模式、指标和测试重点。

## 怎么读这些案例

一个消息架构要先回答六个问题：

1. **语义**：这是命令、事件还是流？失败时业务能接受重复、丢失还是乱序？
2. **所有权**：谁生产、谁消费、谁是拓扑 owner？
3. **可靠性**：需要 confirm/ack/持久化/Quorum 到什么级别？
4. **顺序**：哪些实体必须有序？按什么键分区？
5. **生命周期**：消息保留多久？失败后重试/死信/人工处理？
6. **运维**：容量、监控、升级、灾备、成本。

下面的案例都按这六问展开。数字只用于说明量级，不是基准。

## 案例 1：电商订单与支付

### 业务语义

- `order.created` 是事件：库存、支付、通知、审计都要知道；
- `inventory.reserve` 是命令：只能一个库存服务实例处理；
- 支付结果既要可靠，又不能重复扣款；
- 大促峰值是平时的 10～50 倍，允许短暂延迟，不允许丢单。

### 拓扑

```text
order-service
  └─ publish -> exchange: orders (topic, durable)
       ├─ order.created  -> queue: inventory.order-created (quorum)
       ├─ order.created  -> queue: payment.order-created (quorum)
       ├─ order.created  -> queue: notification.order-created (classic)
       └─ order.#        -> queue: audit.all-orders (quorum/stream)

inventory-service
  └─ publish -> exchange: inventory (topic)
       ├─ inventory.reserved -> queue: order.inventory-reserved
       └─ inventory.rejected -> queue: order.inventory-rejected

payment-service
  └─ publish -> exchange: payments (topic)
       ├─ payment.captured -> queue: order.payment-captured
       └─ payment.failed   -> queue: order.payment-failed

所有业务队列:
  x-dead-letter-exchange: orders.dlx
  x-delivery-limit: 5
```

### 可靠性链

- 订单库与 outbox 同事务提交；发布器读 outbox，使用 publisher confirm；
- `mandatory=true`，处理不可路由消息；
- 关键队列 Quorum + persistent + 手动 ack + prefetch；
- 消费端按 `order_id` 幂等，状态机只允许合法迁移；
- 失败进入 DLX，按退避重试，超过上限进 parking lot；
- 支付回调用 idempotency key，和订单状态机对账；
- 大促前压测 Quorum confirm 尾延迟和积压恢复。

可运行的最小闭环见 `examples/18_order_pipeline.py` 与 `examples/19_outbox_idempotency.py`。

### 失败模式

| 失败 | 影响 | 处理 |
|---|---|---|
| 发布成功、confirm 丢失 | outbox 重发，重复消息 | 消费端幂等 |
| 库存服务重复消费 | 重复扣减 | 唯一约束/状态机 |
| 支付回调乱序 | 状态回退 | 版本号/状态机 |
| DLX 满/不可路由 | 失败消息丢失 | DLX 监控 + alternate exchange |
| Quorum leader 切换 | 短暂不可写 | 客户端重连/重试 |
| 大促积压 | 消费延迟 | 扩容消费者/限流/降级 |

### 指标与测试

- 发布 confirm 成功率/p99；
- 各队列 ready/unacked/consumer 数；
- 订单到支付的端到端延迟；
- DLQ 增长率；
- 重复消息率、幂等冲突数；
- 故障注入：ack 前崩溃、confirm 前断网、Quorum leader kill、DLX 不可用。

## 案例 2：金融交易与对账

### 业务语义

- 交易指令不能丢，重复必须被识别；
- 审计要求完整、可追溯、不可篡改；
- 对账是日终/实时的硬要求；
- 监管要求数据驻留、加密、访问审计。

### 拓扑

```text
trading-gateway
  └─ publish -> exchange: trades (direct/topic, durable)
       ├─ trade.new     -> queue: risk.trade-new (quorum)
       ├─ trade.new     -> queue: ledger.trade-new (quorum)
       └─ trade.cancel  -> queue: risk.trade-cancel (quorum)

risk-service -> exchange: risk.results
       ├─ risk.approved -> queue: oms.approved
       └─ risk.rejected -> queue: oms.rejected

ledger-service -> exchange: ledger.events
       └─ ledger.posted -> queue: reconciliation.ledger-posted

audit:
  exchange: audit (fanout/topic)
  queue: audit.immutable (quorum + 长保留 + 归档)
```

### 可靠性链

- TLS + mTLS，最小权限 vhost/用户，管理面隔离；
- Quorum 3/5 副本，跨 AZ；
- publisher confirm + mandatory + persistent；
- 消费端手动 ack，prefetch 小，处理时间可预测；
- 所有消息带业务唯一键、交易 ID、版本号；
- 账本/风控/对账三方独立落库，日终对账；
- 灾备：温备集群 + definitions/消息归档 + 定期切换演练；
- 审计日志独立存储，防篡改/只追加。

### 失败模式

| 失败 | 影响 | 处理 |
|---|---|---|
| 重复交易指令 | 重复下单/扣款 | 业务唯一键 + 幂等 |
| 风控与账本状态不一致 | 资金风险 | 对账 + 状态机 |
| Quorum 多数派丢失 | 不可写 | 恢复节点/切换 |
| 证书过期 | 全站不可连 | 轮换演练/告警 |
| 审计消息丢失 | 合规风险 | 独立归档/confirm |
| 跨区域切换重复 | 重复入账 | 全局交易 ID + 对账 |

### 指标与测试

- 交易指令端到端延迟；
- confirm p99、Quorum 复制延迟；
- 未确认消息、DLQ、对账差异；
- 证书到期、TLS 握手失败；
- 故障注入：节点/ AZ 故障、证书过期、重复指令、乱序、磁盘满；
- 灾备切换与回切演练，记录 RPO/RTO。

## 案例 3：SaaS 多租户

### 业务语义

- 每租户隔离（至少逻辑隔离），配额/限流；
- 租户 onboarding/offboarding 要可自动化；
- 大租户不能拖垮小租户；
- 计费/审计需要按租户统计。

### 两种隔离模型

| 模型 | 拓扑 | 优点 | 缺点 |
|---|---|---|---|
| vhost per tenant | 每租户一个 vhost + 用户/权限 | 强隔离、权限清晰、易删除 | 元数据/连接开销大，跨租户统计难 |
| shared vhost + 前缀 | 队列/交换机名带租户前缀 | 资源利用率高、运维简单 | 隔离弱，误配置影响面大 |

混合模型：大客户 vhost 隔离，中小客户共享 vhost + 命名规范/配额。

```text
shared vhost: saas
  exchange: tenant.<tenant_id>.events
  queue:    tenant.<tenant_id>.jobs
  policy:   tenant-<tenant_id>-quota
    max-length / max-length-bytes / TTL / DLX

large tenant:
  vhost: tenant-acme
  dedicated user + permissions + cluster?
```

### 可靠性链

- 租户级用户/权限，禁止跨租户 configure/write/read；
- 每租户队列有 max-length/TTL/DLX，防止无限积压；
- 消费者按租户隔离或按租户分区，避免 noisy neighbor；
- 指标带 tenant 标签（注意基数）；
- onboarding 自动化：创建 vhost/用户/权限/拓扑/配额/监控；
- offboarding：停止生产、排空、归档、删除、审计。

### 失败模式

| 失败 | 影响 | 处理 |
|---|---|---|
| 单租户积压 | 磁盘/内存被占满 | 配额 + 隔离 + 告警 |
| 权限过大 | 跨租户访问 | 最小权限 + 审计 |
| 命名冲突 | 拓扑覆盖 | 命名规范 + CI 校验 |
| 指标基数爆炸 | 监控不可用 | 租户标签采样/聚合 |
| 删除租户误删共享资源 | 其他租户受影响 | 资源 owner/命名空间隔离 |

### 指标与测试

- 每租户队列深度、发布/消费速率、DLQ；
- 配额触发次数；
- 租户创建/删除耗时与失败率；
- 越权访问测试；
- 单租户压测不影响其他租户。

## 案例 4：IoT 与边缘

### 业务语义

- 海量设备、长连接、弱网、离线缓冲；
- 设备到云的上行遥测、云到设备的命令；
- 设备身份/证书、主题权限；
- 边缘站点可能断网，需要本地缓冲/聚合。

### 拓扑

```text
device --MQTT/TLS--> edge broker --Federation/Shovel--> regional broker
                          |                                  |
                          └─ local queue (offline buffer)    ├─ telemetry stream
                                                             ├─ command queues
                                                             └─ alerting
```

RabbitMQ 的 MQTT 插件把主题映射到 exchange/queue；QoS 1 对应至少一次，设备端要幂等。共享订阅可用于多消费者分摊。

### 可靠性链

- 设备证书/用户名密码 + 主题 ACL；
- QoS 1 + persistent session（按客户端库/插件能力）；
- 边缘本地持久化/缓冲，断网不丢；
- 上行消息带设备 ID/时间戳/序列号，云端去重/乱序处理；
- 命令下行带过期时间，避免过期命令执行；
- 边缘到云端用 Federation/Shovel，至少一次；
- 设备影子/最后状态存数据库，不依赖队列保留。

### 失败模式

| 失败 | 影响 | 处理 |
|---|---|---|
| 设备断网 | 数据延迟 | 本地缓冲 + 重连 |
| 重复上行 | 重复计费/告警 | 设备序列号 + 幂等 |
| 命令过期 | 错误执行 | TTL/时间戳校验 |
| 边缘磁盘满 | 缓冲丢失 | 容量告警 + 降采样 |
| 主题权限错误 | 跨设备访问 | ACL + 证书绑定 |

### 指标与测试

- 在线设备数、连接抖动、重连率；
- 上行/下行消息速率、端到端延迟；
- 边缘缓冲深度、断网恢复时间；
- 重复/乱序率；
- 故障注入：断网、证书过期、设备重连风暴、边缘磁盘满。

## 案例 5：AI/GPU 任务队列

### 业务语义

- 推理/批处理任务异步执行，GPU worker 消费；
- 任务有优先级、超时、重试、取消；
- 大模型输入/输出可能很大，消息只传引用；
- GPU 昂贵，要避免空转/重复计算。

### 拓扑

```text
api -> exchange: inference (direct/topic)
         ├─ inference.realtime -> queue: gpu.realtime (quorum, high priority)
         ├─ inference.batch    -> queue: gpu.batch (quorum)
         └─ inference.low      -> queue: gpu.low (classic)

gpu-worker:
  prefetch=1
  manual ack
  heartbeat/consumer_timeout 匹配任务时长
  publish result -> exchange: results
  large payload -> object storage + claim check

results:
  queue: api.results.<request_id> (临时/自动删除)
  or callback/webhook with idempotency
```

### 可靠性链

- 任务消息带 `request_id`、优先级、死线、重试次数；
- 大输入/输出走对象存储，消息带 URI/校验和（Claim Check）；
- prefetch=1，避免 GPU worker 囤积任务；
- `consumer_timeout` 要大于最长任务，或用异步心跳/进度续期；
- 任务幂等：同一 `request_id` 重复执行返回缓存结果；
- 失败重试有上限，毒任务进 parking lot；
- 优先级不要依赖无限多级；Quorum 优先级能力按版本确认；
- GPU 队列按模型/租户隔离，避免大任务饿死小任务。

### 失败模式

| 失败 | 影响 | 处理 |
|---|---|---|
| worker 处理中崩溃 | 任务重投 | 幂等 + 重试上限 |
| 任务超时 | consumer_timeout 关闭 channel | 调整超时/心跳/拆分任务 |
| 大消息 | 内存/网络压力 | Claim Check + 对象存储 |
| 优先级反转 | 低优先级饿死 | 配额/分队列/公平调度 |
| 重复推理 | GPU 浪费 | 结果缓存 + 幂等键 |
| 队列积压 | 延迟上升 | 自动扩缩 + 限流 |

### 指标与测试

- 队列等待时间、任务执行时间、GPU 利用率；
- 重试率、超时率、重复执行率；
- 大消息比例、对象存储失败率；
- 故障注入：worker kill、任务超时、对象存储不可用、优先级饥饿。

## 案例 6：企业集成与遗留系统

### 业务语义

- 连接 IBM MQ/ActiveMQ/JMS/文件/API；
- 协议转换、格式转换、路由；
- 事务/确认/幂等要求因系统而异；
- 迁移期间新旧系统并行。

### 拓扑

```text
legacy system --AMQP 1.0/JMS/Shovel--> RabbitMQ --> modern services
                                   ├─ transformation queue
                                   ├─ enrichment queue
                                   └─ audit/archive

external partner --AMQP 1.0/TLS--> exchange: partners
                                   ├─ partner.<id>.orders
                                   └─ partner.<id>.events
```

### 可靠性链

- 协议桥接要明确消息属性/确认语义映射（AMQP 1.0 settlement、JMS ack）；
- 转换服务幂等，保留原始消息/错误；
- 迁移期双写/双跑，按业务键对账；
- 伙伴接入用独立 vhost/用户/证书，配额隔离；
- Shovel 用于迁移/桥接，监控积压和重复；
- 旧系统不能确认时，不要让消息无限重投。

### 失败模式

| 失败 | 影响 | 处理 |
|---|---|---|
| 协议映射丢失属性 | 业务字段缺失 | 契约测试 + 原始消息保留 |
| 转换失败 | 消息卡住 | DLQ + 人工修复 |
| 双写不一致 | 新旧系统差异 | 对账 + 切换窗口 |
| 伙伴流量突增 | 影响其他租户 | 独立 vhost/配额 |
| 旧系统不可用 | 积压 | 缓冲 + 限流 + 告警 |

### 指标与测试

- 桥接延迟、转换失败率、积压；
- 新旧系统对账差异；
- 协议兼容性测试（AMQP 0-9-1/1.0/JMS）；
- 故障注入：旧系统中止、转换异常、伙伴断连。

## 案例 7：事件驱动微服务与事件平台

### 业务语义

- 多个服务通过事件协作，事件是长期契约；
- 事件目录、schema、owner、SLO；
- 消费者独立升级，允许重复/乱序；
- 需要重放/审计/数据管道。

### 拓扑

```text
producer services -> exchange: domain.events (topic)
  ├─ order.created   -> queue: billing.order-created
  ├─ order.created   -> queue: analytics.order-events (stream)
  ├─ payment.captured-> queue: order.payment-captured
  └─ #               -> queue: audit.all-events (stream/archiv)

schema registry / event catalog
  - event name/version/owner
  - JSON Schema/Avro/Protobuf
  - compatibility rules
  - sample payloads
```

### 可靠性链

- 事件带 `event_id`、`occurred_at`、`schema_version`、`correlation_id`、`causation_id`；
- 生产者 outbox + confirm；
- 消费者幂等/去重/状态机；
- 事件契约测试 + 兼容性检查；
- 重放工具按时间/业务键限速；
- Stream 用于分析/审计重放，队列用于任务；
- 事件目录和 owner 治理，定期清理无主事件。

### 失败模式

| 失败 | 影响 | 处理 |
|---|---|---|
| schema 破坏性变更 | 消费者崩溃 | 兼容性规则 + 版本路由 |
| 事件重复 | 重复副作用 | 幂等键 |
| 事件乱序 | 状态错误 | 版本号/分区 |
| 无主事件 | 长期无人维护 | 事件目录 + owner |
| 重放压垮下游 | 故障扩散 | 限速 + 隔离 |

### 指标与测试

- 事件生产/消费速率、端到端延迟；
- schema 兼容性检查通过率；
- 重放批次/进度；
- 消费者 lag、DLQ；
- 契约测试、重放演练、乱序/重复注入。

## 反模式速查

| 反模式 | 症状 | 修正 |
|---|---|---|
| 一个队列所有人消费 | 任务被错误的人处理 | 按语义拆队列/路由 |
| 把事件当命令 | 多个消费者重复执行 | 命令用竞争消费者，事件用订阅 |
| 无限重试 | 毒消息打满队列 | delivery-limit + parking lot |
| 队列无长度限制 | 磁盘满、发布阻塞 | max-length/TTL + 告警 |
| 全局顺序 | 单消费者瓶颈 | 按实体分区，只保证局部顺序 |
| 每条消息建连接 | 连接/端口耗尽 | 复用连接/channel |
| 只用 auto_ack | 崩溃丢消息 | 手动 ack + 幂等 |
| 把 broker 当数据库 | 查询/聚合困难 | 落库/OLAP，消息只做传输 |
| 把 Kafka 当任务队列 | 重复消费/无单条 ack | 用 RabbitMQ/SQS 类队列 |
| 把 RabbitMQ 当日志 | 保留/重放成本高 | Stream/Kafka |
| 没有 DLQ | 失败消息消失 | DLX + 监控 + 人工 |
| 没有对账 | 重复/丢失不可见 | 计数/业务状态对账 |

## 架构选择矩阵

| 需求 | 首选 | 备选 | 注意 |
|---|---|---|---|
| 复杂路由/多消费者 | RabbitMQ topic/headers | Kafka + 多 topic | 绑定数量/匹配成本 |
| 单条确认/任务分发 | RabbitMQ/SQS | ActiveMQ | prefetch/ack/重试 |
| 高吞吐日志/重放 | Kafka/Pulsar | RabbitMQ Streams | 保留/分区/顺序 |
| 请求/响应 | RabbitMQ RPC | HTTP/gRPC | 超时/幂等/连接开销 |
| 延迟/重试 | RabbitMQ Quorum 原生延迟 | 外部调度 | 版本/顺序/成本 |
| 多租户 | vhost/命名空间 | 独立集群 | 隔离/配额/指标基数 |
| 跨区域 | Federation/Shovel/应用双写 | 云托管 | 重复/顺序/RPO |
| 事件治理 | 契约 + 目录 + schema | 自建平台 | owner/兼容性 |

## 小结

参考架构的价值不是给你一套可复制的 YAML，而是展示“业务语义 → 拓扑 → 可靠性链 → 失败模式 → 指标/测试”的推导过程。电商、金融、SaaS、IoT、AI、企业集成和事件平台的积木相同，取舍不同：队列类型、确认、顺序、隔离、重试、保留和灾备都由业务约束决定。先把失败模式和验收标准写清楚，再选交换机、队列和集群形态，架构才不会沦为“消息队列用法大全”。

---

[返回目录](../README.md) | [上一章：附录 U 灾备、多地域与业务连续性](U-disaster-recovery-continuity.md) | [下一章：附录 W RabbitMQ 4.3/4.4 新特性实验手册](W-4x-feature-labs.md)
