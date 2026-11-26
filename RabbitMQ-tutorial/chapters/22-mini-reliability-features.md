# 第 22 章 Mini-RabbitMQ 可靠性特性实现

> 本章定位：逐项拆解 TTL、优先级、溢出、死信、delivery-limit、publisher confirms、事务和持久化恢复。每项都给出代码位置、测试和与真实 RabbitMQ 的差异。

## 消息模型

`mini_rabbitmq/message.py` 的 `Message`：

- body（bytes）；
- properties（AMQP Basic 属性）；
- exchange/routing_key；
- redelivered；
- message_id/timestamp 默认值。

`Delivery`：

- message；
- delivery_tag；
- queue；
- owner；
- consumer_tag；
- auto_ack；
- redelivered；
- queued_seq/expires_at；
- delivered_at。

为什么把 queued_seq 放进 Delivery？nack/requeue 时需要恢复原优先级和顺序；仅靠 message_id 无法区分同 ID 的重复消息。真实 RabbitMQ 内部也有类似的队列序号/消息引用。

## TTL

`Queue._compute_expiry`：

```python
ttls = []
if message.ttl_ms is not None: ttls.append(message.ttl_ms)
if self.message_ttl is not None: ttls.append(int(self.message_ttl))
return now + min(ttls) / 1000.0 if ttls else None
```

每次 enqueue/get/dispatch 调 `_expire_locked`，扫描 ready 堆，过期消息进入死信路径（如果配置 DLX）。

差异：

- RabbitMQ 通常在队头检查 TTL；Mini 扫描所有 ready 消息；
- Mini 的时间精度是 Python 时钟，测试用毫秒级 sleep；
- 快照保存的是绝对 `expires_at`，恢复时直接复用；只有缺失该字段时才按当前时间重新计算。它不会按消息的原始时间戳重算，因此“恢复后剩余 TTL”与重启前一致（除非快照里没有 `expires_at`）。

测试：

- `test_message_ttl_expires`；
- `test_queue_message_ttl`；
- `test_zero_ttl_dead_letters_immediately`；
- `test_expired_on_requeue`。

## 优先级

`_priority_of` 把消息优先级 clamp 到 0～255，再与队列 `x-max-priority` 取 min。堆键：

```python
(-priority, seq, expires_at, message)
```

- 高优先级先出；
- 同优先级按 seq FIFO；
- 过期时间不影响排序，但会在弹出前被扫描淘汰。

声明时校验 `x-max-priority` 必须在 0～255；Mini 的 quorum 模型拒绝 `x-max-priority`（真实 RabbitMQ 的 quorum 在 4.0–4.2 支持 normal/high 两档调度，4.3+ 支持 32 级严格优先级且同样不使用 classic 的 `x-max-priority`；本实现未模拟这些调度器）。

测试：

- `test_priority_order_and_clamp`；
- `test_priority_fifo_within_same_priority`；
- `test_invalid_max_priority_rejected`；
- `test_quorum_queue_validation`。

## 长度/字节溢出

`_would_exceed_locked` 检查条数和字节数。`_handle_overflow_locked` 按 `x-overflow`：

- `drop-head`：弹出堆顶（最高优先级/最旧序号），必要时死信，再尝试放入新消息；
- `reject-publish`：抛 `PublishRejected`；
- `reject-publish-dlx`：先死信新消息，再拒绝发布。

注意 `drop-head` 在优先级队列中弹的是“堆顶”，即下一个会被消费的消息，不一定是“最旧”。这与“从队头丢弃”的直观语义一致，但业务上要知道高优先级消息可能被丢。

测试：

- `test_overflow_drop_head`；
- `test_overflow_reject_publish`；
- `test_overflow_reject_publish_dlx`；
- `test_max_length_bytes`。

## 死信与 x-death

`Queue._dead_letter` 调 `Broker.dead_letter`，后者转给 `VHost.dead_letter`：

1. 如果没有 DLX，直接返回（消息丢弃）；
2. 检查 death hops，达到 32 时丢弃（是 `>= 32`，不是“超过 32”）；
3. `Message.add_death` 添加 x-death/first-death；
4. 计算新的 routing key（显式覆盖或沿用原文）；
5. 清空原 expiration（避免立即过期）；
6. 通过 DLX 重新发布。

x-death 结构：

```python
{
    "count": 1,
    "reason": "rejected",
    "queue": "work",
    "exchange": "",
    "routing-keys": ["work"],
    "time": 1700000000,
}
```

测试：

- `test_nack_without_requeue_dead_letters`；
- `test_message_ttl_expires`；
- `test_overflow_reject_publish_dlx`；
- `test_delivery_limit_dead_letters`。

差异：

- 真实 RabbitMQ 对死信 TTL/属性的保留规则更细；
- Mini 主动清空 expiration，可能与真实行为不同；
- 死信重发没有 publisher confirm 语义；
- DLX 不可路由时消息丢弃（只打日志）。

## delivery-limit

Queue 维护 `_delivery_counts[seq]`：

- 每次投递 +1；
- nack requeue 时若计数达到 `x-delivery-limit`，死信 reason=`delivery_limit`；
- ack 或最终丢弃时清理计数。

测试曾暴露一个真实 bug：早期实现成功 requeue 时清空了计数，导致 delivery-limit 永远不触发；修复后保留计数。这个案例说明“测试通过不等于语义正确”，需要针对边界写测试。

## Publisher confirms

服务端在 `ChannelState` 维护 `next_publish_seq`：

- `basic.publish` 时序号 +1；
- 消息被接受后发 `basic.ack(seq)`；
- 队列满 reject-publish 时发 `basic.nack(seq)`；
- mandatory 不可路由时先发 `basic.return`，再 ack。

`_publish_now` 的核心：

```python
try:
    result = broker.publish_message(...)
except PublishRejected:
    if channel.confirm:
        send_confirm(seq, ack=False)
    return
for returned in result.returned:
    send basic.return
if channel.confirm:
    send_confirm(seq, ack=True)
```

注意：Mini 的 confirm 发生在内存入队后，没有 fsync/Quorum 复制。它表达协议语义，不表达生产持久化保证。Mini 目前对每条 publish 单独发 `basic.ack(seq)`，不实现 `multiple=true` 的批量聚合，也没有“confirm 超时后重发”的客户端状态机；真实客户端必须同时处理单条/批量 ack、乱序 confirm、nack 和超时未知。

测试：

- `test_publisher_confirms_success`；
- `test_publisher_confirm_nack_on_reject_publish`；
- `test_mandatory_return_with_confirm`。

## 事务

`ChannelState.tx` 模式下：

- publish 存入 `tx_publishes`；
- ack/nack 存入 `tx_acks`；
- commit 时先应用 ack/nack，再逐条 publish；
- rollback 清空缓冲。

限制：

- Mini 的事务不是数据库事务，没有 XA；
- 与 confirms 互斥；
- commit 不是原子的（如果中途失败，前面已应用）；
- 没有磁盘日志/回滚段。

测试：

- `test_transactions_commit_and_rollback`；
- `test_transactions_and_confirms_are_mutually_exclusive`。

## 持久化与恢复

`SnapshotStore` 原子写：

```text
mkstemp -> json.dump -> flush -> fsync(optional) -> os.replace
```

恢复：

- 读取 JSON；
- 恢复 users/permissions；
- 逐个 vhost：policies → exchanges → queues → bindings；
- queue 的 ready + unacked 都恢复为 ready（unacked 标记 redelivered）；
- 只恢复 durable 队列/交换机和 persistent 消息。

测试：

- `test_durable_topology_and_persistent_messages_recover`；
- `test_acked_message_does_not_recover`；
- `test_unacked_message_recovers_as_ready`；
- `test_policy_and_users_persist`；
- `test_transient_objects_do_not_persist`；
- `test_corrupt_snapshot_raises`。

差异：

- 没有 WAL/段文件；
- 全量快照可能丢最近变更；
- 没有 Quorum 复制；
- 恢复顺序/策略参数可能与真实 RabbitMQ 不同。

## 告警与流控

`Broker.set_alarm`：

- 设置 memory/disk 告警；
- 通知监听器；
- publish 抛 `ResourceError`；
- `AMQPServer` 向连接发送 `connection.blocked`/`unblocked`。

测试：

- `test_alarm_blocks_publish`；
- `test_alarm_listener`；
- 服务端的 blocked 通知未做完整端到端测试（客户端只记录回调）。

这不是完整 flow control，只是状态模拟。

## 可靠性边界

| 特性 | Mini | 真实 RabbitMQ |
|---|---|---|
| TTL | 全量扫描 | 队头语义/定时器 |
| 优先级 | classic 堆；quorum 拒绝 x-max-priority | classic 多级；quorum 4.0–4.2 两档、4.3+ 32 级 |
| 溢出 | 三种策略 | 三种策略，细节更多 |
| 死信 | x-death/循环限制 | 更完整属性/路由 |
| delivery-limit | 计数 | quorum 等 |
| confirm | 内存接受 | persistent+durable 且 Quorum 多数派后才确认；classic 不保证 confirm 前 fsync，transient 可更早 |
| 事务 | 缓冲提交 | 更复杂但非 XA |
| 持久化 | JSON 快照 | 段文件/WAL/Quorum |
| 流控 | 告警抛错 | blocked/credit |

这些差异不是“实现偷懒”，而是教学范围选择。第 25 章会解释生产化需要补哪些模块。

## 可靠性特性的状态机

### 消息生命周期

```text
READY
  -> DELIVERED (unacked)
       -> ACKED (removed)
       -> NACK_REQUEUE (ready, redelivered)
       -> NACK_DEAD (DLX)
       -> EXPIRED (DLX/drop)
  -> DROPPED (overflow/drop-head)
```

### 消费者生命周期

```text
REGISTERED (suspended)
  -> ACTIVE
       -> CANCELLED
       -> SUSPENDED (channel flow)
       -> INACTIVE (single active consumer standby)
  -> REMOVED (channel/connection close)
```

### 死信生命周期

```text
REJECTED/EXPIRED/MAXLEN/DELIVERY_LIMIT
  -> add x-death
  -> DLX route
       -> queue (ready)
       -> unroutable (drop/return)
       -> death hop limit (drop)
```

把这些状态机画出来，测试就能按“状态 × 事件”覆盖，而不是只测 happy path。

## 边界条件清单

| 特性 | 边界 | Mini 行为 |
|---|---|---|
| TTL | 0 / 负数 / 队头阻塞 | 0 立即过期；负数未设置；全量扫描 |
| 优先级 | 0 / 255 / 超出 / 同优先级 | clamp + FIFO；quorum 拒绝 x-max-priority |
| 溢出 | 空队列 / 恰好等于上限 / 字节上限 | drop-head/reject-publish/dlx |
| ack | 未知 tag / 重复 ack / multiple 越界 | 抛错/抛 `PreconditionFailed`（不幂等）/按 <= tag |
| prefetch | 0（无限）/ 1 / 大于队列 | 0 不限；1 严格 |
| 死信 | 无 DLX / DLX 不可路由 / 循环 | 丢弃/记录/跳数限制 |
| 持久化 | 损坏文件 / 空文件 / 版本不匹配 | 损坏/空文件抛错；版本字段当前未校验，不能指望它做兼容迁移 |
| 事务 | 中途失败 / 与 confirm 混用 | 非原子/拒绝 |

这些边界是“测试是否真的在测语义”的试金石。只测“正常发一条收一条”，无法发现 delivery-limit 计数被清零这类 bug。

## 失败注入

Mini 的测试可以直接注入：

- 队列满 → reject-publish nack；
- 消费者抛异常 → 消息保持未确认；
- channel 关闭 → 未确认重投；
- 快照损坏 → 启动失败；
- 告警 → publish 抛 ResourceError；
- 死信循环 → 跳数限制。

真实系统还要注入：

- 网络延迟/丢包/分区；
- 磁盘满/IO 错误；
- 进程 kill -9；
- leader 切换；
- 证书过期；
- 下游超时。

故障注入不是“额外工作”，而是验证可靠性声明是否成立的唯一方式。

## 本章小结

Mini 的可靠性实现覆盖了 TTL、优先级、溢出、死信、delivery-limit、confirm、事务和快照恢复，并用 219 项测试中的相关用例验证。关键不变量包括“过期不进 ready、requeue 保留计数、ack 释放映射、persistent 恢复、transient 不恢复”。同时要清楚它没有生产级持久化/复制/流控。

下一章拆线协议与服务端/客户端实现，解释握手、内容帧、confirm 和消费投递如何在真实 TCP 上工作。

---

[上一章：第 21 章 Mini-RabbitMQ 核心 Broker 实现](21-mini-core-broker.md) | [返回目录](../README.md) | [下一章：第 23 章 Mini-RabbitMQ 线协议与客户端实现](23-mini-wire-protocol.md)
