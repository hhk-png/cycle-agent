# 第 08 章 消费可靠性：ack、nack、prefetch 与公平分发

> 本章定位：消费侧决定“消息是否真的被处理”。发布 confirm 只证明 broker 接受，消费者 ack 才表示业务处理完成；两者之间是一个可能重复、可能乱序、可能毒消息的世界。

## Push 与 Pull

RabbitMQ 提供两种消费方式：

- **push（basic.consume）**：broker 主动投递，适合持续消费、低延迟；
- **pull（basic.get）**：客户端按需拉取，适合批处理、测试、低频任务。

Push 是主流。它需要 prefetch 控制未确认消息数量，否则 broker 会把大量消息推给一个消费者。Pull 看似简单，但每次 get 都是一次 RPC，吞吐低，而且没有“等待新消息”的语义；用 get 实现轮询通常不如 consume。

Mini 两者都支持：`Queue.dispatch` 对应 push，`Queue.get` 对应 pull。`examples/01` 用 get，`examples/12` 用 consume。

## Delivery tag 与 ack

每次投递都会分配一个 delivery tag。它是 **channel 级**的，不是全局，也不是消息 ID。消费者用：

```text
basic.ack(delivery_tag, multiple=false)
```

告诉 broker“这条处理完了”。`multiple=true` 表示“截至该 tag 的所有未确认消息都确认”。例如收到 1、2、3，发送 `ack(3, multiple=true)` 等价于确认 1、2、3。

注意：

- delivery tag 只在当前 channel 有效；
- channel 关闭后，未确认消息会被重投；
- 同一个消息重新投递会得到新的 delivery tag；
- 不能用 delivery tag 做业务幂等键，应该用 message_id/业务 ID。

Mini 的 `Broker._deliveries` 维护全局 tag → (queue, delivery) 映射，`Queue._unacked` 维护每个队列的未确认表；ack 会释放映射并唤醒下一轮 dispatch。`tests/test_queue.py` 覆盖单条、multiple、未知 tag 和重复 ack。

## nack、reject 与 requeue

两种否定操作，外加一个容易混淆的取消操作：

| 方法 | 能力 | 典型用途 |
|---|---|---|
| basic.reject | 单条，requeue 布尔 | 简单拒绝 |
| basic.nack | 单条/批量，requeue 布尔 | RabbitMQ 扩展，支持 multiple |
| basic.cancel | 取消消费者 | 停止消费；**不是**否定 ack，不会 requeue 在途消息 |

`requeue=true` 会把消息放回队列，标记 redelivered，并可能立即再次投递给其他消费者。`requeue=false` 会丢弃消息；如果队列配置了 DLX，则进入死信路径。

最常见的错误是“处理失败就 requeue”。如果消息本身有 bug（毒消息），它会立刻回到队列并被再次投递，形成无限循环，CPU 和日志被打爆。正确策略：

1. 区分可重试错误（网络抖动、下游 503）和不可重试错误（数据格式非法）；
2. 可重试错误使用延迟队列/退避重试，限制次数；
3. 不可重试错误进入 DLX/parking lot；
4. 达到最大重试次数后人工处理；
5. 记录 message_id、失败原因、重试次数。

Mini 的 `x-delivery-limit` 在 nack requeue 时检查投递次数，超过后死信，测试见 `test_delivery_limit_dead_letters`。真实 RabbitMQ 的 delivery-limit 主要与 quorum queue 关联，classic 行为需核对版本。

4.3 起 Quorum 还把“投递失败”和“退回”分开计数：`basic.reject`/`modified delivery-failed` 增加 delivery-count，消耗 delivery-limit；`basic.nack`/AMQP `released` 主要增加 acquired-count，不消耗 delivery-limit。也就是说，“无限 nack requeue”在 4.3+ 不一定会被 delivery-limit 拦住。实验见[附录 W](../appendices/W-4x-feature-labs.md)的实验 2，版本语义见[附录 I](../appendices/I-version-migration.md)。

## Prefetch：流控的核心

`basic.qos(prefetch_count=N)` 限制每个消费者最多 N 条未确认消息。它解决两个问题：

- 防止一个消费者囤积大量消息；
- 让多个消费者更公平地分担负载。

```python
channel.basic_qos(prefetch_count=20)
channel.basic_consume(queue="jobs", on_message_callback=handle, auto_ack=False)
```

Prefetch 取值需要匹配处理模式：

- 处理快、下游延迟低：可以大一些（几十到几百）；
- 处理慢、下游易过载：小一些（1～10）；
- 需要严格公平：prefetch=1；
- 批量处理：适度大，配合批量 ack。

Prefetch 太小会降低吞吐（频繁等待），太大则失去流控意义。它不是“越大越好”，也不是“1 最可靠”。

### global 标志

AMQP 的 `basic.qos` 有一个 `global` 标志。RabbitMQ 对它的解释与 AMQP 规范原文不同：

- `global=false`（大多数客户端默认）：**每个消费者**各自最多 N 条未确认；
- `global=true`：**整条 channel** 共享最多 N 条未确认。

信道级预取需要跨队列协调，性能更差；RabbitMQ 4.0 将其标记弃用，4.3 起默认拒绝（Quorum/Stream 会以 channel error 回应）。生产代码应只使用 **per-consumer prefetch**，不要依赖 global 语义。Mini 明确拒绝 `global=true`（返回 NOT_IMPLEMENTED 并关闭 channel），这是诚实的边界，而不是偷偷忽略。

### prefetch-size

`prefetch_size` 按字节限制，而不是条数。RabbitMQ 对它的支持有限，很多客户端默认 0（不限制）。实际流控主要靠 prefetch_count 和消费者处理速度。

## 公平分发与消费者优先级

没有 prefetch 时，broker 可能把一个队列的消息轮询给多个消费者，但每个消费者都在自己的缓冲区里囤积。prefetch 让“未完成工作量”可见，从而实现更公平的分配。

消费者优先级通过 `x-priority` 参数设置（basic.consume arguments）。高优先级消费者优先收到消息；这适合“主消费者 + 备用消费者”或“快机器优先”。但优先级不等于负载均衡：

- 高优先级消费者可能过载；
- 低优先级消费者可能饥饿；
- 预取中的消息不会因为优先级变化而重新分配。

Mini 在 `Queue._next_consumer_locked` 中按优先级排序并轮询，测试覆盖高优先级先收到第一条、低优先级收到第二条。

## auto_ack 的风险

`auto_ack=true` 表示 broker 投递后立即认为消息已确认，不等业务处理。风险：

- 消费者处理到一半崩溃，消息永久丢失；
- 业务异常无法 requeue；
- 过载时无法通过未确认数感知积压。

它适合：

- 可丢失的日志/指标；
- 消费逻辑极短且幂等；
- 明确接受 at-most-once 的场景。

绝大多数业务队列应使用手动 ack。不要因为“代码简单”而关闭 ack。

## 消费者生命周期、取消与超时

消费者可能因以下原因消失：

- 客户端主动 cancel；
- channel 关闭；
- connection 关闭；
- 队列删除；
- broker 节点故障/升级；
- 管理操作（policy、权限变更）；
- `consumer_timeout` 触发。

关闭时：

- 未确认消息会重新入队（channel/connection 关闭）；
- 消费者 tag 失效；
- auto-delete 队列可能被删除；
- exclusive queue 可能随连接删除。

### consumer_timeout

RabbitMQ 有一个容易被忽略的保护：消费者如果在 `consumer_timeout` 内没有 ack，channel 会被关闭。常见默认是 30 分钟（1800000 ms），可通过配置调整；4.3 起可按队列/消费者配置。

**作用范围**：4.3+ 只有 Quorum Queue 会评估 consumer timeout；classic queue 和 stream 不会因为“处理太久没 ack”而触发它。不要把这条保护当成所有队列类型的通用兜底。

它解决的是“消费者拿到消息后永远不 ack，队列以为它还在处理”的问题。触发后：

- channel 关闭，未确认消息重投；
- 消费者需要重连/重建；
- 业务可能重复处理；
- 日志里通常能看到 `consumer_timeout` 相关错误。

实践：

- 长任务要么拆小、要么把进度写入业务状态，不能依赖“每次 ack 前延长超时”这种不存在的机制；
- 不要把 `consumer_timeout` 当成消息 TTL；
- 消费者要能处理 channel 被关闭并恢复；
- 监控 channel 关闭原因，区分网络错误和 ack 超时；
- 如果业务确实需要更长处理时间，显式调大超时并评估重投范围。

### 消费者取消通知

broker 可能主动取消消费者：队列被删除、节点下线、权限变更、Quorum leader 切换等。AMQP 用 `basic.cancel` 通知客户端。客户端应：

- 注册 cancel 回调，记录原因；
- 不要把“没有消息”误判为“队列空闲”；
- 重建消费者前先确认队列/拓扑仍存在；
- 避免在 cancel 回调里做阻塞操作；
- 监控消费者数量与预期副本数的差异。

Mini 在队列删除/连接关闭时移除消费者；真实客户端库通常暴露 `on_cancel`/`consumer cancellation` 事件，具体 API 依库而定。

### 优雅关闭

```python
import signal
import threading

class RetryableError(Exception):
    """下游 503、网络抖动等可重试错误。"""

stop = threading.Event()

def handle_signal(signum, frame):
    stop.set()

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

def consume_loop(channel, queue):
    def on_message(ch, method, properties, body):
        if stop.is_set():
            ch.stop_consuming()
            return
        try:
            process(body)
            ch.basic_ack(method.delivery_tag)
        except RetryableError:
            # 可重试：退回队列，交给重试拓扑/退避；生产环境通常发布到 retry 队列
            ch.basic_nack(method.delivery_tag, requeue=True)
        except Exception:
            # 不可重试：进入 DLX/parking lot，避免毒消息循环
            ch.basic_nack(method.delivery_tag, requeue=False)

    channel.basic_qos(prefetch_count=20)
    channel.basic_consume(queue=queue, on_message_callback=on_message)
    channel.start_consuming()
    channel.close()
    channel.connection.close()
```

关闭顺序：

1. 停止接收新任务（取消消费者/停止拉取）；
2. 等待在途任务完成并 ack；
3. 关闭 channel；
4. 关闭 connection；
5. 对未完成任务做补偿/重放。

如果直接 `kill -9`，broker 会在心跳超时后重投未确认消息，造成重复。优雅关闭的目标不是“绝不重复”，而是“把重复范围控制到可接受”。

## 长任务与心跳

消费者处理一条消息耗时很长时，要注意：

- 不要在事件循环线程里做阻塞 IO，否则心跳无法发送；
- 长时间无 ack 不等于连接断开，但可能触发客户端超时；
- 如果处理时间超过心跳超时，连接可能被判定死亡；
- 把长任务拆分、异步化，或使用独立 worker 进程；
- 对超长任务设计任务状态和幂等恢复。

RabbitMQ 不会因为“消息处理很久”自动重投；只有连接/channel 断开、Quorum 的 `consumer_timeout` 触发或显式 nack 才会。但客户端库可能因为心跳/网络问题断开，因此长任务应可恢复。

## 批量 ack 的权衡

批量 ack（multiple=true）能减少协议往返，但失败时重投范围更大：

```text
收到 1..100，处理到 80 时崩溃
如果只 ack 了 1..70，80..100 会重投
如果 ack(100, multiple=true) 但 80 未完成，消息丢失
```

原则：**只 ack 已经完成的消息**。可以在每 N 条后 ack 到当前完成位置，但不能提前 ack。批量 ack 适合可重放、幂等的批处理；不适合有严格副作用且不幂等的业务。

## 消费者并发模型与下游容量

常见模型：

- **单进程多线程**：每线程一个 channel，prefetch 小；
- **多进程**：每进程一个 connection，水平扩容；
- **异步事件循环**：单 channel 串行，CPU 密集任务丢线程池；
- **Kubernetes worker**：按队列深度/HPA 扩容，注意缩容时的优雅关闭。

不要多个线程共享一个 channel 做 RPC。不要在一个 callback 里执行无限阻塞操作。

消费者数量不是越多越好。吞吐的上限通常在下游：

```text
所需并发 ≈ 到达率 × 平均处理时间 / 目标利用率
```

例子：2,000 msg/s、平均处理 50 ms、目标利用率 70%，需要约 143 个并发处理单元。如果数据库连接池只有 50，增加消费者只会让它们在连接池上排队，延迟更高。

设计顺序：

1. 测下游容量（数据库连接、API 限流、CPU）；
2. 定单消费者并发（线程/协程/进程）；
3. 定 prefetch（未确认窗口）；
4. 定消费者副本数；
5. 用队列分片/分区突破单队列上限；
6. 压测故障与恢复。

不要用“增加消费者”掩盖下游瓶颈，也不要用“减小 prefetch”掩盖处理逻辑的阻塞。

## Mini 的消费实现

`Queue.dispatch` 的循环：

1. 加锁，过期扫描；
2. 选择可投递消费者（active、未 suspended、未达 prefetch）；
3. 弹出消息，生成 Delivery，写入 unacked；
4. 释放锁后调用 callback；
5. callback 抛异常时记录日志，消息保持未确认（手动 ack）；
6. ack/nack 后再次 dispatch。

这个设计避免了“持有队列锁做网络 IO”，但也带来重入复杂性：callback 里 ack 会再次触发 dispatch。Mini 用 `_dispatching` 和 `_dispatch_pending` 防止递归风暴。对应代码在 `mini_rabbitmq/queue.py`。

测试覆盖：

- prefetch 限制；
- 双消费者轮询；
- 消费者优先级；
- 单活消费者；
- exclusive consumer；
- auto_ack；
- channel/connection 关闭重投；
- 事务中的 ack 延迟。

## 消费可靠性检查清单

- [ ] 手动 ack，处理成功后再确认；
- [ ] prefetch 与处理能力匹配；
- [ ] 区分可重试/不可重试错误；
- [ ] 毒消息进入 DLQ，不无限 requeue；
- [ ] 消费幂等，使用业务唯一键；
- [ ] 优雅关闭，等待在途任务；
- [ ] 监控 ready、unacked、consumer 数、ack 速率；
- [ ] 长任务不阻塞心跳/事件循环；
- [ ] 批量 ack 不提前确认；
- [ ] 明确 at-least-once 语义，不假设 exactly-once。

## 本章小结

消费可靠性由 ack、prefetch、失败策略和生命周期共同决定。Push 消费是主流，delivery tag 是 channel 级确认凭据，nack/requeue 不能滥用，prefetch 是流控核心，auto_ack 只适合可丢失场景。连接关闭会重投未确认消息，因此消费者必须幂等。

下一章把失败处理系统化：死信交换机、`x-death`、TTL 延迟重试、退避、最大重试次数和 parking lot。

---

[上一章：第 07 章 发布可靠性：confirms、mandatory return 与事务](07-publisher-reliability.md) | [返回目录](../README.md) | [下一章：第 09 章 死信、延迟与重试架构](09-dead-letter-delay-retry.md)
