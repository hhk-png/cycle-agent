# 第 21 章 Mini-RabbitMQ 核心 Broker 实现

> 本章定位：沿着一次 publish/get/ack 的路径读代码。重点看数据结构、锁、所有权和不变量，而不是逐行解释语法。

## Broker 门面

`mini_rabbitmq/broker.py` 的 `Broker` 是顶层门面，负责：

- 用户与 vhost；
- 全局 delivery tag；
- 发布/拉取/确认/消费者 API；
- 告警；
- 持久化快照；
- 指标与维护线程。

初始化大致是：

```python
broker = Broker(
    data_file=None,
    auto_checkpoint=True,
    fsync=False,
    auth=default_auth(),
    node_name="mini@localhost",
)
```

关键字段：

| 字段 | 作用 |
|---|---|
| `vhosts` | vhost 名 → VHost |
| `_delivery_seq` | 全局投递标签 |
| `_deliveries` | tag → (Queue, Delivery) |
| `_consumer_seq` | 自动 consumer tag |
| `_alarms` | 内存/磁盘告警 |
| `store` | SnapshotStore/NullStore |
| `metrics` | 计数器/仪表 |

`Broker` 不直接处理 socket；网络层调用它的门面方法。这样可以用内存模式做单元测试，也可以加 TCP 做端到端测试。

## 发布入口

```python
def publish(self, exchange, routing_key, body, *, properties=None, headers=None,
            mandatory=False, vhost="/"):
    if self._alarms:
        raise ResourceError("publisher blocked by resource alarm")
    props = dict(properties or {})
    if headers is not None:
        merged = dict(props.get("headers") or {})
        merged.update(headers)
        props["headers"] = merged
    message = Message(body=body, properties=props, exchange=exchange,
                      routing_key=routing_key)
    return self.publish_message(message, mandatory=mandatory, vhost=vhost)
```

`publish_message` 做三件事：

1. 检查告警；
2. 调 `VHost.publish` 路由/入队；
3. 更新指标、通知 publish listener、刷新 gauge。

它不处理 publisher confirm 序号；那是 channel 层状态，由 `server.py` 负责。这个分层很重要：Broker 只表达“消息被接受/拒绝”，不关心客户端是否等待确认。

## VHost 声明与路由

`VHost` 持有交换机、队列、策略：

```python
self.default_exchange = Exchange("", durable=True, arguments={})
self.exchanges = {"": self.default_exchange}
self.queues = {}
self.policies = PolicySet()
```

### 声明交换机

`declare_exchange` 检查：

- 名称不能为空（默认交换机由 broker 创建）；
- `amq.` 前缀保留；
- 已存在时属性必须一致；
- passive 只查询；
- durable + auto-delete 不允许；
- 通过 registry 创建类型；
- 合并 policy 参数；
- 记录 base arguments。

### 声明队列

`declare_queue` 检查：

- 空名生成 `amq.gen-*`；
- 保留前缀；
- passive 查询；
- 已存在的 durable/exclusive/auto-delete/arguments 一致；
- exclusive + durable 不允许；
- 合并 policy；
- 创建 Queue 并登记。

Mini 对已存在队列的等价性比较使用 `base_arguments`，而不是 policy 生效后的参数；这样 policy 变更不会导致“声明冲突”。真实 RabbitMQ 的比较规则更细。

### 绑定

`bind_queue` 调 exchange.add_binding；`bind_exchange` 建立 E2E 绑定；unbind 删除。auto-delete exchange 在最后一个绑定移除后由 Broker 清理。

## 路由算法

`VHost.publish` 分两种情况：

### 默认交换机

```python
if message.exchange == "":
    queue = self.queues.get(message.routing_key)
    if queue is None:
        return self._maybe_return(message, mandatory)
    queue.enqueue(message)
    return PublishResult(routed=1, queues=[queue.name])
```

默认交换机不做绑定查找，直接按队列名路由。这与 RabbitMQ 的隐式绑定语义等价，但实现更简单。

### 显式交换机

```python
exchange = self.get_exchange(message.exchange)
if exchange.internal:
    raise AccessRefused(...)
destinations = []
visited = set()
routed = self._route_exchange(exchange, message.routing_key, message,
                              visited, destinations, depth=0)
for queue_name in destinations:
    self.queues[queue_name].enqueue(message)
```

`_route_exchange`：

1. 若 exchange 已访问，返回 0（防环）；
2. 深度超过 32 抛错；
3. `exchange.route` 得到匹配绑定；
4. 若没有绑定，尝试 alternate exchange；
5. 对 queue 目标去重；对 exchange 目标递归；
6. 返回匹配计数。

一个细节：Mini 对同一消息的多个绑定指向同一队列时去重。这不是教学取舍，而是 AMQP 0-9-1 的语义：一条消息即使匹配同一队列的多条绑定，队列也只应收到一份（规范要求“至多一次”）。真实 RabbitMQ 同样按队列去重；如果业务需要两份，必须绑定两个不同队列或发布两条消息。Mini 的去重是正确的，测试也覆盖了多绑定场景。

## 入队

`Queue.enqueue` 的路径：

1. 更新 last_used；
2. `_expire_locked` 清理过期消息；
3. 检查 length/bytes 上限；
4. 若溢出，按 drop-head/reject-publish 处理；
5. 计算 expires_at（消息 TTL 与队列 TTL 取小）；
6. TTL=0 立即死信；
7. 推入堆，更新字节数；
8. 统计 published。

入队不直接 dispatch；VHost 在路由完成后统一调用 `queue.dispatch()`，避免每条消息触发多次。

## 消费者与 dispatch

`Queue.add_consumer` 创建 `ConsumerHandle`，支持：

- auto_ack；
- prefetch_count；
- exclusive；
- priority；
- owner（channel）；
- suspended（用于 consume-ok 顺序）；
- active（单活消费者）。

`dispatch` 的核心循环：

```python
while True:
    with lock:
        expire
        if no message: break
        consumer = select_consumer()
        if none: break
        pop message
        delivery = issue_delivery(...)
        if not auto_ack:
            unacked[tag] = delivery
    callback(message, delivery)
```

为什么 callback 在锁外？避免消费者处理慢时阻塞发布/ack。为什么需要 `_dispatch_lock` 和 pending？因为 callback 内可能调用 `ack`，ack 会再次触发 dispatch；不防重入会递归。

消费者选择：

- 按 `(-priority, created_at, tag)` 排序；
- 跳过 cancelled/inactive/suspended；
- 检查 prefetch 未确认数；
- 轮询游标实现公平。

Mini 的 prefetch 计数按 consumer_tag 统计 unacked；真实 RabbitMQ 还可能考虑 channel-wide QoS/credit。

## 投递标签与 ack

`Broker.issue_delivery`：

```python
with lock:
    _delivery_seq += 1
    delivery.delivery_tag = _delivery_seq
    if not delivery.auto_ack:
        _deliveries[tag] = (queue, delivery)
```

`Broker.ack`：

- 根据 tag 找到 queue/delivery；
- 验证 owner；
- 对 multiple 收集 <= tag 的投递；
- 调 `Queue.ack`；
- 更新指标、保存快照、刷新 gauge。

`Queue.ack` 从 `_unacked` 移除，释放 broker 映射，统计 acked，然后再次 dispatch。nack 类似，但可选择 requeue 或死信。

一个边界：Mini 的 delivery tag 全局递增，而 AMQP 规范是 channel 级。教程保留了“全局 tag 对客户端仍单调”的表象，用于简化跨队列映射；真实客户端不要依赖跨 channel 的 tag 关系。

## 消费者关闭与资源所有权

`Broker.close_owner(owner)`：

1. 遍历所有 vhost/queue；
2. 移除 owner 的消费者；
3. 如果队列 exclusive 且 owner 匹配，删除队列；
4. 否则 requeue owner 的未确认消息；
5. 若队列 auto-delete 且无消费者，删除；
6. 清理 auto-delete exchange。

测试：

- `test_channel_owner_close_requeues_unacked`；
- `test_exclusive_queue_deleted_when_owner_closes`；
- `test_connection_close_deletes_exclusive_queue`；
- `test_auto_delete_queue_after_cancel`。

这些测试模拟真实客户端的关闭路径，而不是只测单个函数。

## 策略

`PolicySet` 按 priority 排序、正则匹配 queue/exchange，把 `max-length` 等 policy key 映射成 `x-*` 参数。声明队列时合并，已有队列通过 `reconfigure` 更新。

限制：

- 不支持所有 policy 类型；
- exchange policy 主要影响新声明；
- 参数变更后某些设置不可变（如 max-priority）；
- 真实 RabbitMQ 的 operator policy/parameter 更复杂。

## 持久化

`Broker.snapshot_state` 收集：

- users/permissions；
- 每个 vhost 的 durable exchanges/queues/bindings/policies；
- persistent messages。

`VHost.snapshot` 只保留 durable 对象；`Queue.snapshot` 只保留 persistent 消息（包括未确认）。`restore` 按顺序恢复 policy → exchange → queue → binding。

`note_state_change` 在拓扑变更/persistent 发布/ack 后触发保存；可关闭自动 checkpoint 并显式 flush。

## 关键代码走读：publish/get/ack

### publish

```text
Broker.publish
  -> Message(body, properties, exchange, routing_key)
  -> Broker.publish_message
       -> 检查 alarm
       -> VHost.publish
            -> 默认交换机：按队列名直接入队
            -> 显式交换机：_route_exchange 递归/去重/AE
            -> Queue.enqueue
                 -> TTL/长度/溢出
                 -> heap push
       -> metrics/listener/state change
```

关键不变量：

- 入队前完成 TTL/溢出检查；
- 死信是重新发布，不是直接写另一个队列；
- 发布返回后，消息要么在 ready，要么在 DLQ/被拒绝；
- confirm 由 server 层发，Broker 不关心客户端是否等待。

### get

```text
Broker.get
  -> Queue.get
       -> expire
       -> 无消息 -> None
       -> 有消息 -> issue_delivery
       -> 非 auto_ack -> unacked[tag]=delivery
```

`basic.get` 是同步 RPC，吞吐低；Mini 用它演示最小消费路径，`examples/01` 就是 get。

### ack

```text
Broker.ack(tag, multiple)
  -> 查 _deliveries
  -> 验证 owner
  -> 收集 <= tag 的 delivery（multiple）
  -> Queue.ack
       -> 从 _unacked 移除
       -> stats.acked++
       -> 再次 dispatch
  -> 清理 broker 映射
  -> 保存快照
```

不变量：

- ack 必须释放 broker 映射，否则内存泄漏；
- 重复 ack 会抛 `PreconditionFailed`（测试覆盖），不是幂等操作；客户端不要在重连后重放旧 tag；
- 未知 tag 抛错；
- channel 关闭时未确认消息重投，而不是丢失。

## 锁顺序与死锁

Mini 的锁顺序建议：

```text
Broker lock -> Queue lock -> (释放后再 callback)
```

不要：

- 持有 Queue lock 调用 callback；
- 持有 Queue lock 发布到另一个队列（可能触发另一把 Queue lock）；
- 在 callback 里递归 dispatch 而不防重入；
- 同时持有两个 Queue lock。

一个真实风险是“死信回调”：`Queue.enqueue` 在持有源队列锁时调用 `_dead_letter` → `Broker.dead_letter` → `VHost.publish` → 目标 `Queue.enqueue`，实际形成“源 Queue lock → vhost/Broker 锁 → 目标 Queue lock”的链路。如果存在反向死信（B 也死信回 A），就可能 ABBA 死锁。Mini 当前**没有**在锁外异步死信，测试也没有覆盖这种反向路径；这是已知的实现风险，而不是已经解决的纪律。生产化改造应把死信目标入队放到源锁释放之后（或使用有界队列 + 单独线程），并加并发死信测试。读者扩展时不要把当前实现当成锁安全的范本。

## 可改进点

| 问题 | 现状 | 改进方向 |
|---|---|---|
| 全局 delivery tag | 简化映射 | 改为 channel 级 tag + 每 channel 映射 |
| 线性路由 | 简单 | 绑定索引/前缀树 |
| JSON 快照 | 易调试 | WAL/段文件/增量 |
| 单进程 Raft | 可测试 | 网络 RPC/持久化日志/成员变更 |
| 管理面共享 GIL | 简单 | 独立采样/缓存/分页 |
| callback 重入 | 有防护 | 异步队列/批量投递 |
| 无 credit 流控 | alarm 抛错 | credit/blocked/背压 |

这些不是“作业”，而是理解生产实现为什么复杂的入口。

## 本章小结

核心路径是：Broker 门面 → VHost 路由 → Queue 入队/分发 → delivery tag/ack → 快照。关键不变量是“未确认消息不丢、ack 后释放、channel 关闭重投、独占队列随 owner 删除”。Mini 用线性扫描、全局 tag、JSON 快照换取可读性；这些取舍已在代码和文档标注。

下一章专门拆可靠性特性：TTL、优先级、溢出、死信、delivery-limit、confirms 和事务。

---

[上一章：第 20 章 Mini-RabbitMQ 架构与工程决策](20-mini-architecture.md) | [返回目录](../README.md) | [下一章：第 22 章 Mini-RabbitMQ 可靠性特性实现](22-mini-reliability-features.md)
