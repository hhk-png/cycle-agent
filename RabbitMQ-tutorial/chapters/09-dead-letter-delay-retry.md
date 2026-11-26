# 第 09 章 死信、延迟与重试架构

> 本章定位：失败不是异常路径，而是消息系统的常态。死信交换机（DLX）负责“把失败消息隔离出来”，TTL/延迟机制负责“等一会儿再试”，两者组合才能构建可控的重试架构。

## 死信是什么

消息在以下情况会变成“死信”：

- 消费者 basic.reject/nack 且 `requeue=false`；
- 消息 TTL 过期；
- 队列达到 max-length/max-length-bytes 被丢弃；
- Quorum Queue 达到 delivery-limit；
- 其他 broker 定义的拒绝原因。

如果队列配置了：

```text
x-dead-letter-exchange: orders.dlx
x-dead-letter-routing-key: failed   # 可选
```

死信会被重新发布到指定交换机，routing key 默认沿用原消息的 routing key（除非显式覆盖）。注意：**死信是一次新的发布**，会经过交换机的正常路由；如果 DLX 没有匹配队列，消息仍可能丢失。

## x-death 记录

RabbitMQ 会在死信消息的 headers 中加入 `x-death` 数组，每个元素包含：

```json
{
  "count": 1,
  "reason": "rejected",
  "queue": "orders",
  "exchange": "orders.exchange",
  "routing-keys": ["order.created"],
  "time": 1700000000
}
```

常见 reason：

| reason | 触发 |
|---|---|
| rejected | reject/nack 且 requeue=false |
| expired | 消息 TTL 到期 |
| maxlen | 队列长度/字节超限 |
| delivery_limit | 投递次数达到上限 |

还有 `x-first-death-reason`、`x-first-death-queue`、`x-first-death-exchange`，记录第一次死亡的信息。这些字段是排查“消息为什么进了 DLQ”的关键。

Mini 的 `Message.add_death` 按这个形状维护记录，`VHost.dead_letter` 负责重新路由，并限制最多 32 次死亡跳转，防止拓扑环导致无限循环。测试覆盖 rejected、expired、maxlen、delivery_limit 和 `x-death` 内容。

## 死信不是回收站

很多团队把 DLQ 当成“失败消息随便堆着”的地方。正确做法是：

1. DLQ 有明确的所有者、保留期和告警；
2. 记录失败上下文（原队列、原因、重试次数、异常 ID）；
3. 提供查询和重放工具；
4. 重放前修复根因、确认幂等；
5. 对无法处理的消息有归档/人工审批流程；
6. 不要让 DLQ 再次无限死信。

重放工具要注意：

- 重放会再次投递，消费端必须幂等；
- 重放顺序可能与原顺序不同；
- 重放时 original exchange/routing key 可能需要覆盖；
- 大量重放会冲击下游，需要限速；
- 重放后原 DLQ 消息要确认，避免重复。

## 延迟重试的实现谱系

延迟重试没有唯一正确答案。按“依赖什么组件、在哪个版本可用、精度和规模如何”分成六类，选择时要先看目标版本，再看云厂商限制。

### 方案 A：TTL 队列 + DLX（跨版本可用）

为每种延迟建一个“等待队列”：

```text
work queue --reject--> retry.5s (TTL=5000, DLX=work exchange) --> work queue
```

消息失败后进入 retry.5s，5 秒后过期，死信回原交换机，重新投递。优点是只用核心功能、所有版本和托管服务都支持；缺点是需要为每个延迟级别建队列，且不同 TTL 的消息在同一队列可能队头阻塞。

### 方案 B：多级延迟桶（跨版本可用）

建立 1s、5s、30s、5min、30min 等固定桶，失败消息按退避级别进入对应桶。避免单队列内混合 TTL，便于容量和监控。代价是延迟精度受桶大小限制，且桶数量会放大拓扑复杂度。

### 方案 C：Quorum 原生延迟重试（4.3+）

4.3 起 Quorum Queue 可以在队列内部把失败消息放置一段时间再重投，不需要 TTL 桶，也不需要重写消息：

```text
x-delayed-retry-type: disabled | all | returned | failed
x-delayed-retry-min:  30000
x-delayed-retry-max: 120000
```

延迟按线性退避计算：`min(min_delay × delivery_count, max_delay)`。它处理的是“被拒绝/被退回”的消息，不是通用定时任务；配置可通过队列参数或 policy。优点是复制、持久化、无消息重写；缺点是版本/云厂商支持不一，且不替代业务重试状态机。

### 方案 D：Quorum 原生定时投递（4.4+）

4.4 起 Quorum Queue 支持消息级定时可见：

- AMQP 0-9-1：long 类型 header `x-opt-delivery-time`，Unix epoch 毫秒；
- AMQP 1.0：message annotation；
- 消息立即入队，但在指定时间前不可消费。

它适合预约任务、延迟通知、超时补偿。注意 `x-delay` 在 4.4 的 Quorum 上不会生效也不会报错，消息会立即投递；从旧插件迁移时必须显式改写 header。

### 方案 E：延迟交换机插件（历史方案，3.x/4.0–4.2）

社区插件 `rabbitmq_delayed_message_exchange` 提供 `x-delayed-message` 类型，消息 header `x-delay` 指定毫秒延迟。它依赖 Mnesia 存储延迟消息；Mnesia 在 4.3 被移除后，插件已归档、无法在 4.3+ 运行。存量系统可以继续用，但新系统不应再选它。

### 方案 F：外部调度

把重试任务写入数据库/Redis/调度服务，由调度器到点重新发布。它适合：

- 需要精确延迟和持久化审计；
- 重试次数很多、延迟很长；
- 需要跨系统协调、跨 broker 迁移；
- 需要人工干预和可视化排队。

代价是引入另一个组件和一致性边界。不要为了一个 5 秒重试就上复杂调度系统。

### 怎么选

| 约束 | 首选 | 说明 |
|---|---|---|
| 必须跨 3.x/4.x/托管服务 | A/B | 只用核心 TTL/DLX，行为最可移植 |
| 4.3+、失败重试 | C | 队列内退避，复制与持久化由 Quorum 负责 |
| 4.4+、预约/定时可见 | D | 消息级 `x-opt-delivery-time`，注意 `x-delay` 不生效 |
| 已有 3.x 延迟插件 | E 迁移到 C/D 或 A/B | 4.3+ 插件不可用，且迁移是静默行为变化 |
| 长延迟、精确调度、审计 | F | 外部调度是真相源，broker 只负责投递 |
| 云厂商锁定 | A/B 或 F | 先确认托管版本是否支持 C/D |

无论选哪种，退避、抖动、最大次数、毒消息隔离、幂等和对账都不能省。延迟只是“什么时候再试”，不是“失败了怎么办”。

想亲手确认 4.3 的 `x-delayed-retry-*` 与 4.4 的 `x-opt-delivery-time` 到底怎么行为，按[附录 W](../appendices/W-4x-feature-labs.md)的实验 3/4 做：前者是队列内失败退避，后者是生产者指定绝对时间；`x-delay`、字符串类型、classic 队列都是静默失效路径，必须正反都测。

## 退避与最大重试

“失败就 5 秒后重试”会在下游故障时形成重试风暴。推荐指数退避 + 抖动：

```text
delay = min(max_delay, base * 2 ** attempt) * random(0.8, 1.2)
```

例如 base=1s、max=5min：

```text
attempt 0 -> ~1s
attempt 1 -> ~2s
attempt 2 -> ~4s
...
attempt 8 -> ~256s
attempt 9 -> 300s（封顶）
```

重试次数必须有限。达到上限后：

- 进入 parking lot 队列，人工处理；
- 记录完整失败上下文；
- 触发告警；
- 不要继续自动重试。

重试次数建议存在自定义业务 header（如 `retry-count`，避免与 broker 保留的 `x-` 语义混淆）或业务表。不要依赖 `x-death.count` 做重试计数：3.13/4.0 起 broker 不再在 republish 时累加客户端提供的 `x-death`，`basic.nack(requeue=true)` 也不会创建 `x-death`。也不要把重试状态只存在消费者内存；消费者重启会丢失。

## 毒消息与 parking lot

毒消息是“无论重试多少次都会失败”的消息。识别信号：

- 同一 message_id 反复进入 DLQ；
- 处理异常是数据格式/业务规则错误，而非暂时故障；
- 重试次数达到上限；
- 单条消息消耗异常资源。

处理：

1. 区分可重试/不可重试异常；
2. 不可重试直接进 parking lot，不参与自动重试；
3. parking lot 有独立消费者/人工审批；
4. 修复后按 message_id 重放；
5. 防止同一条消息阻塞整个队列。

## 顺序与重试的冲突

重试会破坏顺序：

```text
消息 A（失败，进入 5s 重试）
消息 B（成功）
消息 C（成功）
5s 后 A 重新投递
```

如果业务要求同一订单的消息严格有序，重试时不能简单回到队尾。可选：

- 按订单 ID 分区到单队列单消费者；
- 失败时暂停该分区消费，直到 A 成功/死信；
- 用状态机 + 版本号，允许乱序到达但拒绝旧版本；
- 把重试消息放回队头（RabbitMQ classic 的 requeue 接近队头，但不保证严格）。

大多数业务不需要全局严格顺序，只需要“同一实体”的顺序。设计分区键比追求全局 FIFO 更现实。

## Mini 中的死信与延迟构件

Mini 已实现：

- `x-dead-letter-exchange` / `x-dead-letter-routing-key`；
- `x-death` 与 first-death headers；
- 死信循环跳数限制；
- delivery-limit；
- TTL 过期触发死信；
- reject/nack 触发死信；
- 长度溢出触发死信。

Mini 没有实现延迟交换机、Quorum 原生延迟重试或 `x-opt-delivery-time` 定时投递。可以用 TTL + DLX 组合出延迟队列，但没有后台定时器精确扫描；维护线程会周期性调用 `Queue.expire_ready`，通过 `Broker.start_maintenance` 启动。这个边界是刻意的：延迟语义一旦涉及复制、时钟和崩溃恢复，就不是“加一个 sleep”能正确模拟的。`examples/08_dead_letter.py` 演示拒绝进入 DLQ，`examples/18_order_pipeline.py` 演示消费失败进入失败队列。

真实 RabbitMQ 的死信发布也受 publisher confirm 影响：死信消息的重新发布是 broker 内部行为，业务发布者通常已经收到原始消息的 confirm；不要以为“进了 DLX 就一定可恢复”，DLX 自身也可能不可路由或队列满。

## 一个可运维的重试拓扑

```text
exchange: orders (topic)
  -> queue orders.work

# 消费者处理失败时显式重投，而不是只 nack：
consumer:
  retry_count = headers.get("retry-count", 0) + 1
  if retry_count <= 3:
      publish to exchange "orders.retry", routing_key "retry.5s/1m/10m"
      headers["retry-count"] = retry_count
      ack original
  else:
      publish to exchange "orders.retry", routing_key "parking"
      ack original

exchange: orders.retry (direct)
  binding retry.5s  -> queue orders.retry.5s
  binding retry.1m  -> queue orders.retry.1m
  binding retry.10m -> queue orders.retry.10m
  binding parking   -> queue orders.parking

queue orders.retry.5s
  x-message-ttl: 5000
  x-dead-letter-exchange: orders
  x-dead-letter-routing-key: order.created

queue orders.parking
  # 无 TTL，人工处理
```

为什么要在消费者里显式重投，而不是只给 work 队列配 DLX？因为 **DLX 的 routing key 是队列级固定值**，broker 在死信时不会根据重试次数挑不同的 key。如果 work 队列统一 `x-dead-letter-routing-key: retry`，四个桶都绑 `retry` 就会同时收到一份；如果绑不同 key，又没有任何一条能匹配。要让消息按次数进入不同延迟桶，必须由消费者选择 routing key 重新发布，或者给每个延迟级别拆独立的 work 队列。

这个拓扑完全使用核心功能，缺点是队列数量增加、延迟精度受 TTL 语义影响，而且重投不是原子的（发布成功、ack 前崩溃会重复）。消费端幂等仍然必需。

## 监控与告警

必须监控：

- DLQ 深度和增长速度；
- 重试队列深度；
- 重试次数分布；
- parking lot 数量；
- 死信原因分布（rejected/expired/maxlen/delivery_limit）；
- 重放速率和失败率；
- 单条 message_id 的重复次数。

告警不要只看“DLQ > 0”，否则轻微失败也会吵；可以设置“DLQ 增长速度”“持续非空时长”“parking 数量”等复合条件。

## DLX 拓扑的五个陷阱

### 陷阱 1：DLX 不可路由

死信是重新发布到 DLX，走正常路由。如果 DLX 没有匹配队列，消息会被丢弃（或按 mandatory/return 处理，取决于 broker 内部行为）。上线前必须验证 DLX 的绑定。

### 陷阱 2：DLX 自身也满/被删

DLX 队列也会触发 max-length、TTL、节点故障。如果 DLQ 没有监控和容量规划，失败消息会“二次丢失”。

### 陷阱 3：死信循环

```text
queue A (DLX=B) -> queue B (DLX=A) -> ...
```

A 和 B 互相死信会形成环。RabbitMQ 会检测**纯 TTL 过期**导致的自动循环并丢弃消息，但显式 `reject`/`nack` 触发的循环可能一直继续；RabbitMQ 没有可配置的通用“死信跳数限制”。32 跳是 Mini 的 `MAX_DEATH_HOPS` 教学保护，不是真实 broker 的行为。生产拓扑必须自己设计终止条件（最大次数、parking lot、人工开关）。

### 陷阱 4：x-death 被覆盖/裁剪

`x-death` 是数组，记录每次死亡；但不同版本/协议对它的处理不同。4.0 起 RabbitMQ 不再解释客户端在重新发布时提供的 `x-` 头（AMQP 0-9-1 和 1.0 都受影响）；AMQP 1.0 使用 `x-opt-deaths` 注解。不要把 `x-death` 当成唯一的审计来源，重要失败要写业务表。

### 陷阱 5：TTL + DLX 的队头阻塞

TTL 队列只在消息到达队头时检查过期（classic 语义）。如果队头是一条 TTL 很长的消息，后面的短 TTL 消息即使过期也不会及时死信。用固定延迟桶（5s/1m/10m）而不是在一个队列里混多种 TTL。

## 从延迟插件迁移：一次静默行为变化的复盘

延迟插件的迁移风险不在“代码改不动”，而在“行为变了但不报错”：

```text
情况 A：旧拓扑仍在声明 x-delayed-message 交换机
  4.3+ 插件不可用 → 声明失败（unknown exchange type / channel error）
  结果：生产者连不上或声明失败，通常是显式错误

情况 B：生产者把 x-delay header 发给普通 Quorum 队列
  4.4 Quorum 不认识 x-delay → header 被忽略
  结果：消息立即投递；业务以为“60 秒后重试”，实际“立刻重试”
```

两种情况的失败方式不同：A 大声报错，B 静默失效。升级前要同时扫描“交换机类型”和“消息 header”，否则只改拓扑仍会踩 B。

迁移步骤：

1. **盘点**：搜索交换机类型 `x-delayed-message`、消息 header `x-delay`、客户端 `setDelay`/`delay` 参数；
2. **选替代**：失败重试走 4.3 的 `x-delayed-retry-*`；预约/定时走 4.4 的 `x-opt-delivery-time`；跨版本走 TTL 桶或外部调度；
3. **改拓扑**：延迟插件是交换机类型，替代方案通常是队列参数或消息 header，不能只改一个字段；
4. **改消息**：把 `x-delay` 转成 `x-opt-delivery-time`（绝对时间，毫秒）或重试状态机的 `next_retry_at`；
5. **双跑验证**：对比旧插件与新方案的投递时间、顺序、重复、DLQ；
6. **删除旧交换机**：确认没有生产者后再删，避免“删了才发现还有流量”。

如果团队暂时不能升级，至少要在升级检查清单里把“延迟插件”标成阻塞项，而不是等升级后由业务发现消息提前到达。

## 重试设计的三个反模式

1. **无上限重试**：`requeue=true` 或 TTL 循环没有最大次数，毒消息会永久占用队列；Quorum 的 delivery-limit 是最后一道闸，但不应该作为唯一闸门。
2. **固定间隔重试**：下游故障时所有失败消息同一时刻回涌，形成重试风暴；至少要有指数退避 + 抖动。
3. **把延迟当调度**：TTL/DLX 的精度受队头检查和 broker 调度影响，不适合“必须在 10:00:00 执行”的业务；精确调度用外部系统。

## 重试状态机

一个可运维的重试不应该只靠 `x-death.count`，而应该有显式状态：

```text
PENDING
  -> PROCESSING
  -> SUCCEEDED
  -> RETRY_WAIT (attempt++)
       -> PROCESSING
       -> FAILED_PERMANENT
       -> PARKED (attempt >= max)
```

状态字段建议：

- `message_id` / 业务唯一键；
- `attempt`；
- `first_failed_at`；
- `last_error` / 错误分类；
- `next_retry_at`；
- `owner` / 处理实例。

状态存哪里？

- 消息 header：简单，但消费者重启/重放时容易丢；
- 数据库表：可查询、可审计、可人工干预；
- 两者结合：header 做快速路由，数据库做真相源。

## Parking lot 工具设计

Parking lot 不是“再建一个队列就完了”。它需要：

1. **查询**：按时间/业务键/错误类型筛选；
2. **重放**：单条/批量、限速、 dry-run；
3. **修复**：重放前允许修正 payload/header；
4. **审计**：谁在什么时候重放了什么；
5. **权限**：重放是高危操作，要审批；
6. **幂等**：重放不产生重复副作用；
7. **保留期**：避免无限增长；
8. **告警**：parking 数量/增长/年龄。

一个最小重放流程：

```text
parking -> 选择消息 -> 校验/修复 -> 预览目标 exchange/routing key
        -> 限速发布 -> 等待 confirm -> 标记已重放 -> 观察下游
```

不要让重放工具直接“全量清空 parking 再发回去”；那会把一次故障变成第二次故障。

## 本章小结

死信交换机把失败消息从主流程隔离出来，`x-death` 提供诊断上下文。延迟重试可以用 TTL+DLX、Quorum 原生延迟重试（4.3+）、`x-opt-delivery-time`（4.4+）或外部调度实现；延迟交换机插件已随 Mnesia 移除而退出历史舞台。关键是退避、抖动、最大次数、毒消息隔离和幂等。重试会破坏顺序，顺序要求应按实体分区设计。

下一章从失败处理转到存储与流控：持久化到底写什么、内存/磁盘告警如何阻塞发布、classic 与 quorum 的存储差异，以及 Mini 的 JSON 快照为什么不是生产 WAL。

---

[上一章：第 08 章 消费可靠性：ack、nack、prefetch 与公平分发](08-consumer-reliability.md) | [返回目录](../README.md) | [下一章：第 10 章 持久化、恢复与流控](10-persistence-flow-control.md)
