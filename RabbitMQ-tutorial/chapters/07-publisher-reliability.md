# 第 07 章 发布可靠性：confirms、mandatory return 与事务

> 本章定位：回答一个看似简单但经常答错的问题——“这条消息到底发成功了没有”。答案是分层的：路由成功、broker 接受、持久化/复制成功、业务消费成功，是四个不同事件。

## 发布路径的四个阶段

一条消息从生产者到磁盘/多数派，至少经历：

1. 客户端序列化并通过 TCP 发出 METHOD + HEADER + BODY；
2. broker 解析、认证、路由到目标队列；
3. 队列接受消息；classic 可能写入磁盘，quorum 可能复制到多数派；
4. broker 向生产者返回确认（如果启用了 confirm）。

每个阶段都可能失败：

- 连接断开：消息可能到、可能没到；
- 交换机不存在：channel 404；
- 不可路由：mandatory 时 return，否则丢弃；
- 队列满：reject-publish 时 nack；
- 节点崩溃：未持久化/未复制的消息丢失；
- confirm 超时：结果未知。

“发布成功”必须明确指哪一层。业务上通常需要“broker 已持久化接受”，而不是“TCP send 成功”。

## Publisher Confirms

在 channel 上开启（pika 用 `confirm_delivery()`；Mini 客户端的方法名是 `confirm_select()`，两者不要混用）：

```python
channel.confirm_delivery()
```

之后每个 publish 会分配一个从 1 开始的单调序号。broker 用 `basic.ack` 或 `basic.nack` 回应，支持 `multiple` 标志表示“截至该序号的消息都已处理”。

```text
publish seq=1
publish seq=2
publish seq=3

broker -> basic.ack(delivery_tag=3, multiple=true)
```

含义是 1、2、3 都被接受。若队列满：

```text
broker -> basic.nack(delivery_tag=2, multiple=false)
```

生产者需要维护“未确认集合”，不能只等最后一条。真实客户端库通常提供同步等待或异步回调：

- 同步：批量发布后 `wait_for_confirms`；
- 异步：注册 confirm 回调，按序号推进；
- 批量：攒 N 条或 T 毫秒后统一等待。

每发一条就同步等待是性能反模式：

```python
# 反例：吞吐被 RTT 限制
for body in bodies:
    channel.basic_publish(...)
    channel.wait_for_confirms()
```

更合理：

```python
for body in bodies:
    channel.basic_publish(...)
channel.wait_for_confirms()
```

Mini 的 `AMQPClient` 在 `publish` 时递增本地序号，服务端在消息被接受后发送 `basic.ack`；客户端在后台读线程记录 ack/nack，`wait_for_confirms` 等待未确认集合清空。测试覆盖成功、队列满 nack 和 mandatory return。

### Confirm 窗口、批处理与背压

confirm 的性能取决于“同时在途的未确认消息数”，也就是窗口。窗口太小，吞吐被 RTT 限制；窗口太大，内存、重发范围和结果未知的代价都会上升。

```text
窗口 = 1：
  publish -> wait ack -> publish -> wait ack
  吞吐 ≈ 1 / RTT

窗口 = N：
  publish × N -> ack × N（可乱序/批量）
  吞吐 ≈ N / RTT（受 broker/磁盘/网络上限约束）
```

生产上常见的三种模式：

1. **同步批量**：发 N 条后 `wait_for_confirms()`，实现简单，延迟较高；
2. **异步回调**：注册 confirm 回调，按序号维护未确认集合，吞吐高但代码复杂；
3. **流式窗口**：维护最多 N 条在途，收到 ack 就补发，兼顾吞吐和内存。

一个异步窗口的伪代码：

```text
inflight = {}
next_seq = 1

def publish(msg):
    while len(inflight) >= window:
        wait_for_any_confirm()
    seq = channel.publish(msg)   # 返回本地序号
    inflight[seq] = msg

def on_confirm(seq, multiple, ack):
    if multiple:
        for s in list(inflight):
            if s <= seq:
                finish(s, ack)
    else:
        finish(seq, ack)
```

关键点：

- confirm 可能乱序到达；`multiple=true` 表示“截至该序号”；
- nack 要重发或进入失败队列，不能静默丢弃；
- 超时的 confirm 是“结果未知”，不是“失败”；
- 重发必须幂等，否则会重复；
- 关闭连接前要等待未确认集合清空，或明确接受“结果未知”。

窗口大小不是越大越好：它受 broker 内存、客户端内存、磁盘 fsync、Quorum 复制和网络 RTT 共同约束。用压测找拐点，而不是照抄数字。

## mandatory return 不是确认

`mandatory=True` 表示“如果无法路由，请把消息退回”。退回通过 `basic.return` 发送，包含 reply code（通常 312 NO_ROUTE）、文本、exchange 和 routing key，随后是完整消息内容。

关键区别：

- **confirm**：broker 是否接受了消息；
- **return**：消息是否找到了至少一个目标队列。

一条 mandatory 消息不可路由时，典型顺序是：

```text
broker -> basic.return(...)
broker -> basic.ack(seq)
```

消息被“接受”了，但没有进入任何队列，随后被退回。生产者必须处理 return，否则仍会丢消息。

非 mandatory 的不可路由消息通常被静默丢弃。若业务不能接受，应该：

1. 开启 mandatory 并处理 return；
2. 或为交换机配置 alternate exchange；
3. 或在发布前确保拓扑存在并持续监控路由失败。

Mini 的 `_publish_now` 先发送 `basic.return`，再发送 confirm；`tests/test_wire_server.py::test_mandatory_return_with_confirm` 验证顺序和内容。

### 生产代码：同时处理 return 与 confirm

`mandatory=True` 只保证“不可路由时退回”，不保证“已持久化”。生产代码要同时处理 return 和 confirm：

```python
import pika

connection = pika.BlockingConnection(params)
channel = connection.channel()
channel.confirm_delivery()

returns = []

def on_return(channel, method, properties, body):
    returns.append({
        "reply_code": method.reply_code,
        "exchange": method.exchange,
        "routing_key": method.routing_key,
        "message_id": properties.message_id,
        "body": body,
    })
    # 记录指标/告警/进入未路由审计队列

channel.add_on_return_callback(on_return)

try:
    channel.basic_publish(
        exchange="orders",
        routing_key="order.created",
        body=payload,
        mandatory=True,
        properties=pika.BasicProperties(delivery_mode=2, message_id=order_id),
    )
    # confirm_delivery 模式下，publish 返回/异常语义依客户端版本；
    # 关键是不要把“没有异常”当成“已路由且已持久化”
except pika.exceptions.UnroutableError as exc:
    # 部分客户端会在 confirm + mandatory 时抛 UnroutableError
    handle_unroutable(exc.messages)
```

判断“发布成功”的正确顺序：

1. 消息被 broker 接受（confirm ack）；
2. 消息至少路由到一个队列（没有 return / 或 return 已处理）；
3. 持久化/复制满足业务要求（durable + persistent + quorum/confirm 语义）；
4. 消费端处理成功（ack + 业务幂等）。

少任何一层，都不能说“业务成功”。

## 持久化链条

一条已经路由进队列的消息要在“broker 重启后仍存在”，需要：

1. 目标队列 durable；
2. 消息 `delivery_mode=2`；
3. 消息被写到持久化存储（classic）或复制到 Quorum 多数派；
4. 发布者在 confirm 返回后再认为成功。

交换机 durable 影响的是*拓扑*和*后续发布*：交换机本身重启后消失，绑定和路由关系也没了，后续消息会发布失败；但一条已经进入 durable 队列的 transient 消息，不会因为“交换机是 transient”而在队列里丢。把交换机 durable 与消息持久化分开理解，才能定位“重启后队列还在、但新消息发不进来”的问题。

任何一环缺失都可能丢：

| 缺失项 | 后果 |
|---|---|
| 交换机非 durable | 重启后交换机和绑定消失，后续发布失败；已入队消息不受直接影响 |
| 队列非 durable | 重启后队列和消息消失 |
| 消息非 persistent | 队列存在但消息可能丢失 |
| confirm 未开 | 不知道 broker 是否落盘/复制 |
| quorum 未形成多数派 | 消息可能只存在于 leader，故障后丢失 |

Transient 消息不是“一定丢”，而是“不保证恢复”。在内存充足且节点不重启时，它工作得很好；一旦故障，恢复语义不同。不要把 transient 用于订单、支付等关键事件。

Mini 的持久化是 JSON 快照：durable 交换机/队列、persistent 消息、用户和策略会被写入；transient 对象不写。`tests/test_persistence.py` 验证 durable 恢复、transient 丢弃、ack 后不恢复、未确认 persistent 消息恢复为 ready。这个模型是教学用，不是 RabbitMQ 的 WAL/Quorum 日志。

## 事务：更强语义，更差吞吐

AMQP 事务在 channel 上：

```text
tx.select
basic.publish ...
basic.ack ...
tx.commit    # 原子应用
tx.rollback  # 丢弃发布和确认操作
```

它能保证“这一批 publish 和 ack 一起生效/一起不生效”，但不提供跨系统 XA，也不把消息发布和数据库事务绑定。事务的代价很高：

- broker 需要缓冲操作；
- commit 通常是同步的；
- 吞吐显著低于 confirms；
- 与 publisher confirms 不能在同一 channel 混用。

生产上绝大多数发布可靠性用 confirms，不用事务。只有在需要“批量发布与确认的原子性”且吞吐不敏感时才考虑事务。Mini 的服务端实现了 tx.select/commit/rollback，客户端测试覆盖提交与回滚；仍建议把 confirms 作为默认路径。

## 结果未知：最难的失败

网络在 `basic.publish` 发出后、confirm 返回前断开，生产者不知道消息是否被接受。这不是“失败”，而是“未知”。处理策略：

1. **幂等发布**：消息携带业务唯一键，消费端去重；
2. **重发**：接受至少一次，消费端保证幂等；
3. **查询/对账**：通过业务表或事件存储确认；
4. **Outbox**：把消息写入业务数据库的 outbox 表，由独立发布器读取并发送，避免“数据库提交了但消息没发”；
5. **CDC**：从数据库变更日志派生事件，减少双写。

不要假设“重发一定安全”。重复消息可能导致重复扣款、重复发货、重复积分。发布可靠性和消费幂等必须一起设计。

## Outbox 模式

典型 Outbox：

```text
BEGIN
  INSERT INTO orders (...)
  INSERT INTO outbox (aggregate_id, event_type, payload, status)
COMMIT

publisher loop:
  SELECT * FROM outbox WHERE status='pending' FOR UPDATE SKIP LOCKED
  publish to RabbitMQ with publisher confirm
  mark status='sent'
```

一个简化的 PostgreSQL 例子：

```sql
CREATE TABLE outbox (
  id           BIGSERIAL PRIMARY KEY,
  aggregate_id TEXT NOT NULL,
  event_type   TEXT NOT NULL,
  payload      JSONB NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at      TIMESTAMPTZ
);

CREATE INDEX outbox_pending_idx ON outbox (status, id);

-- 业务事务
BEGIN;
INSERT INTO orders (id, status) VALUES ($1, 'created');
INSERT INTO outbox (aggregate_id, event_type, payload)
VALUES ($1, 'order.created', $2);
COMMIT;
```

发布器：

```sql
SELECT id, aggregate_id, event_type, payload
FROM outbox
WHERE status = 'pending'
ORDER BY id
LIMIT 100
FOR UPDATE SKIP LOCKED;
```

```text
for row in rows:
    publish with confirm + mandatory
    if confirmed and routed:
        UPDATE outbox SET status='sent', sent_at=now() WHERE id=row.id
    else:
        leave pending / record attempt
```

要点：

- 业务数据和 outbox 在同一数据库事务中，解决双写；
- `FOR UPDATE SKIP LOCKED` 支持多发布器并行；
- 发布器至少一次投递，消费端仍需幂等；
- 发布成功后标记 sent，失败保留 pending；
- outbox 表要归档/清理，避免无限增长；
- 按 aggregate_id 控制顺序（同一聚合单线程/单分区）；
- confirm 超时不要立即删除，重试并依赖幂等。

Outbox 不是 RabbitMQ 专属，但它是“数据库 + 消息系统”组合最常用的可靠性模式。第 19 章会展开完整一致性设计，`examples/19_outbox_idempotency.py` 提供了一个可运行的 SQLite + Mini-RabbitMQ 版本。顺序约束见[第 31 章](31-ordering-partitions.md)。

## 发布性能实践

- 复用连接和 channel；
- 批量 publish，批量等待 confirm；
- 对非关键消息考虑异步 confirm；
- 控制消息大小，避免单条几十 MB；
- 不要在每个请求里声明拓扑；
- 监控 confirm 延迟和 nack 率；
- 对 quorum 队列，理解 fsync/复制带来的延迟；
- 对 mandatory 消息，处理 return 而不是只打日志；
- 对高频小消息，考虑批量/压缩，但不要破坏幂等键。

一个常见错误是把 `basic_publish` 返回当成成功。多数客户端库的 publish 是异步写入 socket，返回只代表“已交给库”，不代表 broker 接受。必须等 confirm。

## Mini 的发布链路

| 阶段 | 代码 |
|---|---|
| 解析 publish 方法 | `mini_rabbitmq/server.py::_basic_publish` |
| 拼装内容 | `_handle_content_header` / `_handle_content_body` / `_finish_publish` |
| 路由与入队 | `mini_rabbitmq/vhost.py::publish` |
| confirm/return | `mini_rabbitmq/server.py::_publish_now` |
| 客户端等待 | `mini_rabbitmq/client.py::wait_for_confirms` |
| 测试 | `tests/test_wire_server.py`、`tests/test_broker.py` |

Mini 在消息入队后立即发送 confirm，不做磁盘 fsync/Quorum 复制；因此它的 confirm 语义是“broker 内存接受”，不是生产级持久化确认。教程明确标注这一点，避免读者把教学实现当成可靠性保证。

## 4.x 发布侧注意事项

- `max_message_size` 默认 16 MiB，大消息会失败；
- Quorum 默认 `delivery-limit=20`；4.3+ 的 `nack`/`released` 主要增加 acquired-count，不消耗 delivery-limit，`reject`/delivery-failed 才消耗。不要把所有重试都当成会触发 delivery-limit；
- global QoS 默认被拒绝，不要依赖；
- 4.0 起 AMQP 1.0 是核心协议，确认/属性映射要重新验证；
- 升级后 confirm 延迟可能因 Quorum/Khepri/磁盘变化而改变，要重新压测。

## 发布可靠性检查清单

- [ ] 交换机、队列 durable，消息 persistent；
- [ ] 启用 publisher confirms，并处理 ack/nack；
- [ ] mandatory 消息处理 basic.return；
- [ ] 不可路由消息有 alternate exchange 或审计；
- [ ] confirm 超时视为未知，不盲目认为成功；
- [ ] 消费端幂等，唯一键可追踪；
- [ ] 关键链路使用 outbox/CDC，避免双写；
- [ ] 监控 confirm 延迟、nack 率、return 数；
- [ ] 明确事务与 confirms 不混用；
- [ ] 压测确认持久化与复制延迟。

## 本章小结

发布可靠性是分层契约：路由成功靠 mandatory/return 和 alternate exchange，broker 接受靠 publisher confirm，持久化靠 durable/persistent/Quorum，业务成功靠消费幂等。事务提供更强的原子性但吞吐较差，通常 confirms 是默认选择。网络断开后的“结果未知”只能靠幂等、outbox 和对账解决。

下一章转向消费侧：ack/nack、prefetch、公平分发、重投和消费者生命周期。发布成功只是开始，消费语义才决定系统是否真的可靠。

---

[上一章：第 06 章 队列语义：FIFO、优先级、TTL 与长度限制](06-queues-ttl-priority.md) | [返回目录](../README.md) | [下一章：第 08 章 消费可靠性：ack、nack、prefetch 与公平分发](08-consumer-reliability.md)
