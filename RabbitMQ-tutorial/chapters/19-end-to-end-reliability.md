# 第 19 章 端到端可靠性与数据一致性

> 本章定位：把 broker 的确认、持久化、重试和业务数据库放在同一条链路上。消息系统能提供“至少一次”，但业务正确性只能由幂等、事务边界和对账保证。

## 三种投递语义

| 语义 | 含义 | 实现代价 | 典型 |
|---|---|---|---|
| at-most-once | 最多一次，可能丢 | 低 | 可丢日志、指标 |
| at-least-once | 至少一次，可能重复 | 中 | 订单、支付、任务（配合幂等） |
| exactly-once | 恰好一次 | 极高/通常不可能端到端 | 特定流处理/事务系统 |

RabbitMQ 的常规消费是 at-least-once：消息在 ack 前崩溃会重投；网络断开时 confirm 结果未知；重试会重复。Broker 不能替业务判断“这次扣款是否已经发生”。所谓 exactly-once 通常是“在某个受控边界内 + 幂等 + 事务”的组合，而不是一个开关。

## 双写问题

业务代码常见写法：

```text
BEGIN
  INSERT order
COMMIT
publish order.created
```

如果数据库提交成功、发布失败，订单存在但下游不知道；如果发布成功、数据库回滚，下游收到不存在的订单。这就是双写不一致。

解决方案：

1. **Outbox**：业务数据和 outbox 记录同事务提交，独立发布器发送；
2. **CDC**：从数据库变更日志派生事件；
3. **本地消息表 + 定时补偿**；
4. **业务对账**：定期核对数据库和消息/下游状态。

Outbox 的发布器要使用 publisher confirm，并记录发送状态。失败重试；重复由消费端幂等消化。

## 幂等消费者

幂等不是“消息有 message_id 就行”，而是“同一业务操作执行多次，结果一致”。实现方式：

- **唯一约束**：数据库对业务键加唯一索引，重复插入失败但视为成功；
- **去重表**：记录已处理的 message_id/业务键，带过期清理；
- **状态机**：只允许合法状态迁移，重复迁移无副作用；
- **版本号/乐观锁**：旧版本事件被忽略；
- **幂等 API**：下游接口接受 idempotency key。

示例：

```text
处理订单支付事件:
  BEGIN
    INSERT INTO processed_messages(message_id) VALUES (?)  -- 唯一键
    UPDATE orders SET status='paid' WHERE id=? AND status='pending'
  COMMIT
  如果唯一键冲突 -> 已处理，直接 ack
```

去重表要设置保留期，避免无限增长。保留期要覆盖最大重试/重放窗口。

## Inbox 模式

Inbox 是消费端的对称模式：

```text
消息 -> 写入 inbox 表（唯一键 message_id）
     -> 业务事务处理
     -> 标记已处理
```

它和去重表类似，但更强调“先落库、后处理”，适合需要审计和重放的场景。Inbox 也可能成为瓶颈，需分区/清理。

## 顺序与并发

RabbitMQ 的队列顺序只在窄条件下成立。端到端顺序需要：

- 按实体 ID 分区（同一订单进同一队列/分区）；
- 单消费者或每分区单消费者；
- 消费失败时不打乱后续消息；
- 版本号/状态机处理乱序；
- 跨服务用业务版本而非到达顺序。

如果使用多个队列/消费者，全局顺序基本不可能。业务应定义“哪些实体需要顺序”，而不是要求所有消息有序。

## Saga 与补偿

分布式事务不适合用消息队列“模拟 XA”。更现实的是 Saga：

- 每个服务本地事务 + 发事件；
- 失败时执行补偿动作；
- 补偿也必须幂等；
- 需要状态机和超时；
- 最终一致，不是即时一致。

例子：

```text
创建订单 -> 扣库存 -> 支付 -> 发货
若支付失败 -> 释放库存 -> 取消订单
```

每一步都可能重复/乱序，补偿要能处理“已经补偿过”的情况。

## RPC 模式

RabbitMQ 可以实现请求/响应：

- 请求消息带 `reply_to` 和 `correlation_id`；
- 服务端消费后发布回复；
- 客户端用临时 exclusive/auto-delete 队列接收；
- 设置超时和重试。

注意：

- RPC 是同步语义，容易把消息系统退化成慢 RPC；
- 超时后请求可能已执行，重试要幂等；
- 回复队列生命周期要管理；
- 大量 RPC 会放大连接/队列开销；
- 更好的替代是异步事件 + 状态查询。

Mini 的 `examples/10_rpc.py` 用 reply_to/correlation_id 演示最小模式。

## 对账与重放

无论设计多好，都需要对账：

- 业务表与事件流计数；
- 订单状态与下游状态；
- 消息生产/消费计数；
- DLQ 与失败记录；
- 定时抽样/全量校验。

重放工具要：

- 按时间/业务键筛选；
- 限速；
- 幂等；
- 记录重放批次；
- 可暂停/回滚；
- 不影响在线流量。

## 一致性矩阵

| 场景 | 推荐 |
|---|---|
| 订单创建 | Outbox + confirm + 幂等消费 |
| 支付回调 | 幂等键 + 状态机 + 对账 |
| 通知/短信 | at-most-once 或至少一次 + 去重 |
| 审计日志 | 至少一次 + 唯一事件 ID |
| 实时指标 | 可丢/可聚合，at-most-once |
| 跨区域复制 | 至少一次 + 去重 + 冲突策略 |

没有一种模式适合所有场景。先定义“重复/丢失/乱序”的业务代价，再选语义。

## 故障演练

- 发布后、confirm 前断网；
- 消费后、ack 前 kill 进程；
- 数据库提交后、outbox 标记前崩溃；
- 重复消息/乱序消息注入；
- DLQ 满/不可路由；
- 节点 leader 切换；
- 下游超时/限流；
- 重放大量历史消息。

每次演练都要验证：消息是否重复、业务是否重复扣款、监控是否告警、对账是否能发现差异。

## Mini 的端到端边界

Mini 提供：

- publisher confirm 的协议路径；
- ack/nack/requeue；
- 持久化快照；
- DLX/delivery-limit；
- 管理/指标。

Mini 不提供：

- 业务数据库事务；
- outbox/inbox；
- 幂等表；
- 跨系统对账；
- 真实网络复制。

所以 Mini 的示例只能证明“消息系统语义”，不能证明业务一致性。业务一致性要在应用层实现并用集成测试/故障注入验证。

## Outbox 的落地细节

第 7 章已经给了 Outbox 的 SQL 和发布器骨架；这里不重复建表，只补三个容易出错的点：多发布器的顺序、attempts/backoff、以及示例与真实 confirm 的差距。

### 表结构与索引

在第 7 章的表上，生产版通常再加 `attempts`、`last_error`、`next_attempt_at` 和 `headers`：

```sql
ALTER TABLE outbox ADD COLUMN attempts INT NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN last_error TEXT;
ALTER TABLE outbox ADD COLUMN next_attempt_at TIMESTAMPTZ;
```

索引除了 `(status, id)`，还要有 `(aggregate_id, id)`，用于按聚合回放和排查顺序。

### 发布器并发

```sql
SELECT id, aggregate_id, event_type, payload, headers
FROM outbox
WHERE status = 'pending'
  AND (next_attempt_at IS NULL OR next_attempt_at <= now())
ORDER BY id
LIMIT 100
FOR UPDATE SKIP LOCKED;
```

关键点：

- `SKIP LOCKED` 让多个发布器**在不同 aggregate 之间**并行，但不保证同一 aggregate 的顺序；两个发布器可能各自领到同一 aggregate 的两行，并发发布后顺序颠倒。
- 要保证每聚合顺序，按 `aggregate_id` 哈希到固定发布器，或对 `aggregate_id` 加咨询锁/用单发布器处理该聚合。
- 发布成功（confirm + 路由）后标记 sent；失败保留 pending，递增 attempts、记录 last_error、设置 next_attempt_at。
- 超过最大 attempts 进入人工/告警，不要无限重试。

### 常见坑

- 业务事务和 outbox 不在同一数据库/事务 → 双写问题仍在；
- 发布器直接 `DELETE` 而不是标记 sent → 无法审计/重放；
- 不处理 confirm 超时 → 重复或丢失；
- 多发布器并发导致同一 aggregate 乱序；
- outbox 表无限增长；
- 消费端不幂等。

## 一个可运行的 Outbox + 幂等消费者

前面的表结构和规则需要一个能跑的闭环来证明。`examples/19_outbox_idempotency.py` 用 SQLite 模拟业务库，用 Mini-RabbitMQ 模拟 broker，把四个动作放在同一条链路上：

```text
create_order:
  BEGIN
    INSERT orders
    INSERT outbox(status='pending')
  COMMIT

publish_outbox:
  SELECT pending FOR each
    publish(message_id='outbox-<id>')
    if routed: UPDATE outbox SET status='sent'

consume:
  BEGIN
    INSERT processed(message_id)   -- 唯一键，幂等屏障
    INSERT inventory(order_id)     -- 业务副作用
  COMMIT
  ack
```

示例故意注入两种真实故障：

1. **发布者崩溃在“发布成功、标记 sent 之前”**：重启后重新发布同一 `message_id`，产生重复消息；
2. **消费者崩溃在“业务副作用之后、ack 之前”**：broker 重投同一条消息，产生重复投递。

运行：

```bash
python examples/19_outbox_idempotency.py
```

输出应类似：

```text
deliveries observed: 3
inventory rows: 1
processed ids: 1
outbox sent: 1
OK 19_outbox_idempotency
```

三次投递、一次库存写入，说明幂等键吸收了重复。这个例子能证明：

- 业务状态与 outbox 行在同一事务提交，避免双写；
- 只有 `routed` 后才标记 sent，失败可重试；
- 重复消息由 `processed` 唯一键挡住，副作用只发生一次；
- ack 前崩溃会触发重投，但不会重复扣库存。

**重要边界**：Mini 的 `PublishResult` 只有 `routed/queues/returned`，没有 publisher confirm。示例用 `routed` 演示“发布→标记”的链路，但 `routed` 只证明至少进入一个队列，不证明持久化/复制，也不代表真实 confirm。生产实现必须等 confirm ack，并把 confirm 超时当作“结果未知”，不能因为 `routed=True` 就标记 sent。

它不能证明真实 RabbitMQ 的持久化/复制、跨进程并发、数据库隔离级别和网络分区；这些需要[附录 P](../appendices/P-application-testing-chaos.md)的集成测试与故障注入来补。示例的价值是把“至少一次 + 幂等”从口号变成可观察的计数。

## Saga 状态机

Saga 的核心是“每个本地事务 + 补偿”，不是“消息队列保证原子性”。

```text
OrderSaga
  state: CREATED
  -> reserve_inventory   (command)
  -> INVENTORY_RESERVED  (event)
  -> charge_payment      (command)
  -> PAYMENT_CAPTURED    (event)
  -> ship                (command)
  -> COMPLETED

失败分支：
  payment_failed -> release_inventory -> CANCELED
  ship_failed    -> refund_payment -> release_inventory -> CANCELED
```

实现要点：

- 状态机持久化（数据库）；
- 每个命令/事件带 saga_id、step、version；
- 重复消息按 (saga_id, step) 幂等；
- 补偿也必须幂等；
- 超时/人工介入有入口；
- 状态转换有审计。

不要用“消息顺序”保证 Saga 正确；用状态机 + 版本号 + 幂等。Saga、Outbox、Claim Check、路由单、停尸房等模式如何组合，以及每种模式的失败路径和测试清单，见[附录 S](../appendices/S-messaging-patterns-antipatterns.md)。

## 幂等表设计

```sql
CREATE TABLE processed_messages (
  message_id   TEXT PRIMARY KEY,
  consumer     TEXT NOT NULL,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  result       JSONB
);

-- 保留期清理
DELETE FROM processed_messages
WHERE processed_at < now() - interval '30 days';
```

变体：

- 业务唯一键（订单 ID + 事件类型）而不是 message_id；
- 复合唯一约束；
- 状态机版本；
- 下游 API 的 idempotency key。

保留期要覆盖：

- 最大重试窗口；
- 最大重放窗口；
- 备份恢复后的重放窗口；
- 合规要求。

去重表本身也会成为瓶颈/热点；按 hash 分区、批量清理、异步写入。

## Schema 演进与事件版本

事件 schema 会变。兼容策略：

| 变更 | 兼容性 | 做法 |
|---|---|---|
| 新增可选字段 | 向后兼容 | 消费者忽略未知字段 |
| 删除字段 | 破坏性 | 先弃用/双写，再删除 |
| 改类型 | 破坏性 | 新字段/新版本 |
| 改语义 | 破坏性 | 新事件类型/版本 |
| 重命名 | 破坏性 | 新旧并存过渡 |

建议：

- 事件带 `schema_version`；
- 用 JSON Schema/Avro/Protobuf 定义；
- 消费者容忍未知字段；
- 生产者/消费者契约测试；
- 版本路由（routing key/header）；
- 保留旧版本消费能力至少一个发布周期。

命名规范、envelope 设计、CloudEvents/AsyncAPI 映射、兼容性规则、契约测试和事件目录的完整方法，见[附录 O](../appendices/O-event-contracts-governance.md)。

## 对账系统

对账不是“出事后查一下”，而是持续运行的系统：

```text
数据源 A（业务库）  ─┐
数据源 B（消息/下游）─┼─> 对账任务 -> 差异表 -> 告警/修复
数据源 C（财务/审计）─┘
```

对账维度：

- 计数（生产/消费/成功/失败）；
- 业务状态（订单/支付/库存）；
- 金额/库存数量；
- 消息 ID/业务键；
- 时间窗口/分区。

对账频率：

- 实时（关键金额）；
- 小时/日（批量）；
- 全量（定期）。

差异处理：

- 自动修复（幂等重放）；
- 人工审核；
- 记录根因；
- 更新监控/测试。

## 本章小结

端到端可靠性 = 至少一次投递 + 幂等消费 + 事务边界（outbox/inbox）+ 顺序设计 + 对账/重放。Broker 不能替代业务一致性；exactly-once 不是配置项。双写要用 outbox/CDC，重复要用唯一键/状态机，乱序要用分区/版本号，失败要有 DLQ 和人工兜底。`examples/19_outbox_idempotency.py` 把这条链路跑成了一个可验证的闭环；事件契约见[附录 O](../appendices/O-event-contracts-governance.md)，应用层测试与故障注入见[附录 P](../appendices/P-application-testing-chaos.md)。当这套可靠性设计需要随拓扑/schema 演进时，按[第 30 章](30-change-engineering.md)的 expand-contract 流程变更，而不是直接改生产队列。

下一章开始拆解 Mini-RabbitMQ 的工程结构，把前面所有概念映射到代码模块和设计决策。

---

[上一章：第 18 章 性能工程：从连接数到持久化](18-performance-engineering.md) | [返回目录](../README.md) | [下一章：第 20 章 Mini-RabbitMQ 架构与工程决策](20-mini-architecture.md)
