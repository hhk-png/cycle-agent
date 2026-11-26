# 第 04 章 连接、信道与虚拟主机

> 本章定位：把“连接/信道/vhost”从名词变成可操作的资源模型。重点不是 API 怎么调，而是生命周期、隔离、权限和故障边界。

## 连接是昂贵资源

一条 AMQP 连接包含 TCP 三次握手、TLS 握手（可选）、SASL 认证、vhost 选择、参数协商、心跳线程和 broker 侧进程/内存资源。它适合长期复用，不适合每次发布创建一条。

常见错误：

```python
# 反例：每条消息建一次连接
for message in messages:
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.basic_publish(...)
    connection.close()
```

这样做会把吞吐压到极低，并制造大量 TIME_WAIT、认证日志和 broker 连接抖动。正确模式是应用级连接池或长连接：

```python
connection = pika.BlockingConnection(params)
channel = connection.channel()
for message in messages:
    channel.basic_publish(...)
connection.close()
```

多线程环境下，pika 的 `BlockingConnection` 和它创建的 channel 都不是线程安全的；安全模式是**每个线程一条独立连接，并在该线程内创建和使用**，而不是多个线程共享连接、各开一个 channel。需要跨线程共享时，用连接池把连接借给线程，或改用 aio-pika 等异步客户端的事件循环模型。aio-pika 的 `RobustConnection` 和 `RobustChannel` 提供自动重连，但自动重连不等于业务自动恢复：交换机/队列声明、消费者恢复、未确认消息重放都需要显式设计。

## 信道是逻辑多路复用

一个连接可以开多个 channel，每个 channel 有独立的：

- 方法序列与 RPC 响应；
- consumer tag 命名空间；
- delivery tag 和未确认消息集合；
- QoS/prefetch；
- publisher confirm 序号；
- 事务状态。

因此不要把 channel 当成“线程安全的全局对象”。多个线程同时在一个 channel 上发 RPC，响应会串线。推荐：

- 每线程一条独立连接（pika 的 `BlockingConnection`/channel 不是线程安全的）；
- 或每类操作一个连接/channel（发布、消费、管理），不跨线程共享；
- 或使用异步客户端的单 channel 串行事件循环，避免跨线程调用。

channel 数量不是越多越好。每个 channel 在 broker 侧有状态和内存开销；几万条 channel 可能拖垮节点。合理规模通常是“每进程几十到几百”，取决于并发模型，而不是“每消息一个”。

## 连接生命周期

一个健康的连接至少经历：

```text
CONNECTING -> OPEN -> RUNNING -> CLOSING -> CLOSED
                    |  ^
                    v  |
                 BLOCKED
```

- **OPEN/RUNNING**：可发布、消费、声明拓扑。
- **BLOCKED**：broker 触发内存/磁盘告警时，对发布者发送 `connection.blocked`；连接仍可消费，但发布被阻塞或拒绝。
- **CLOSING**：客户端或服务端发送 `connection.close`，等待 `close-ok`。
- **CLOSED**：TCP 关闭，所有 channel、消费者、未确认投递都需要清理。

客户端必须处理：

1. 连接被服务端强制关闭（升级、节点下线、认证过期）；
2. 心跳超时；
3. 网络闪断；
4. TLS 证书过期；
5. broker 告警导致的 blocked；
6. 关闭过程中的在途消息。

上线时建议把“优雅关闭”写成代码：停止接受新消息、等待在途 ack、取消消费者、关闭 channel、关闭连接。不要依赖进程退出时操作系统替你清理；未确认消息会被 broker 重投，可能造成重复处理。

## vhost 是权限与命名空间边界

vhost 把交换机、队列、绑定、策略和权限隔离开。两个 vhost 可以有同名队列，互不影响。它适合：

- 多租户 SaaS；
- 生产/预发/开发共享集群；
- 不同业务域隔离；
- 按环境限制权限。

vhost 不是安全隔离的全部。同一 vhost 内的用户仍可能通过队列绑定读取别人的消息；网络层还需要 TLS、防火墙和最小权限。RabbitMQ 的权限分为：

| 权限 | 控制什么 |
|---|---|
| configure | 声明/删除交换机、队列、绑定、策略等拓扑 |
| write | 向交换机发布消息 |
| read | 从队列消费/拉取消息 |

三个都是正则，匹配资源名。常见配置：

```text
configure: ^order\.
write:     ^order\.
read:      ^order\.
```

这意味着用户只能操作名字以 `order.` 开头的资源。注意默认交换机的权限检查使用的是名字 `amq.default`，不是空字符串；如果 write 正则不匹配 `amq.default`，向默认交换机发布会被拒绝。需要默认交换机时，把 write 正则写成类似 `^(order|amq\.default)`。

## 资源所有权与关闭顺序

AMQP 的资源有明确的所有者：

- **exclusive queue**：属于声明它的连接；连接关闭时自动删除；
- **auto-delete queue**：最后一个消费者取消后删除（前提是曾经有过消费者）；
- **exclusive consumer**：队列上只允许一个消费者；
- **consumer tag**：属于 channel，channel 关闭时消费者消失；
- **delivery tag**：属于 channel，未确认消息在 channel 关闭时重投。

Mini-RabbitMQ 用 `owner` 字段模拟这种关系：队列声明时记录连接，消费者记录 channel，投递记录 channel。`Broker.close_owner` 会取消消费者、重排队未确认消息、删除独占队列。对应测试在 `tests/test_broker.py` 和 `tests/test_wire_server.py`。

一个真实事故模式是：连接 A 声明 exclusive queue，连接 B 尝试消费，得到 `RESOURCE_LOCKED`；或者连接 A 断开时队列被删除，依赖它的其他连接收到取消通知。独占队列的生命周期是**连接级**，不是 channel 级：同一连接内的其他 channel 可以使用，channel 关闭不会删除队列，连接关闭才会。正确做法是把独占队列的声明和消费放在同一连接内，并明确“连接断开 = 队列消失”的语义。

## channel 错误 vs connection 错误

| 场景 | 结果 | 客户端应对 |
|---|---|---|
| 队列不存在 | channel close 404 | 修正队列名/重建 channel |
| 无权限 | channel close 403 | 修正 ACL/不要重试同一操作 |
| 参数不一致 | channel close 406 | 对齐声明参数 |
| 帧格式错误 | connection close 501 | 记录、断开、检查客户端库 |
| 协议语法错误 | connection close 502 | 断开，通常是实现 bug |
| 连接被管理员关闭 | connection close 320 | 重连，检查升级/下线事件 |

不要把 channel 错误当成连接错误处理。一个消费者队列名错，不应触发整个进程重连。相反，connection 错误意味着所有 channel 都失效，必须整体重建。

## blocked 连接与流控

RabbitMQ 在内存或磁盘达到高水位时会阻塞发布连接，并发送 `connection.blocked`。客户端应把它当成背压信号：暂停新发布、限制本地缓冲、监控 blocked 的连接数和持续时间，而不是无限重试。Mini-RabbitMQ 用 `Broker.set_alarm` 模拟这个状态：告警期间 `publish` 抛 `ResourceError`，服务端向连接发送 `connection.blocked`/`unblocked`；真实 RabbitMQ 更多是“阻塞”而不是立即报错。

具体的客户端回调与背压代码见本章末尾的“blocked 连接的客户端处理”。

## 连接与信道的可观测性

管理 API 暴露每个连接的用户、vhost、peer、channel 数、收发计数和状态；每个 channel 暴露消费者数、prefetch、confirm/tx 状态。关键指标：

- 连接建立/关闭速率；
- 每连接 channel 数；
- 连接存活时间；
- blocked 连接数；
- channel 错误率；
- 消费者数量与队列数量的比例。

如果连接数持续上涨，先查客户端是否泄漏；如果 channel 数暴涨，查是否每请求建 channel；如果 blocked 时间长，查内存、磁盘和队列积压。

## 一个推荐的客户端资源模型

```text
Process
├── Connection (one per process or per worker)
│   ├── Channel "publish"     -> publisher confirms, batch
│   ├── Channel "consume-A"   -> QoS 20, manual ack
│   └── Channel "consume-B"   -> QoS 50, manual ack
└── Reconnect supervisor      -> rebuild channels/consumers on failure
```

原则：

1. 连接长期复用，但要有重连监督；
2. channel 按用途隔离，避免 RPC 响应串线；
3. 消费者恢复要幂等，不能假设消息只投一次；
4. 关闭顺序与打开顺序相反；
5. 把 vhost、用户、权限纳入配置管理，而不是散落在代码里。

## 重连状态机：自动重连不等于自动恢复

客户端库的“自动重连”通常只恢复 TCP/AMQP 连接，不等于恢复业务。一个完整的状态机至少要覆盖：

```text
DISCONNECTED
  -> CONNECTING
  -> AUTHENTICATING
  -> OPENING_VHOST
  -> RESTORING_CHANNELS
  -> DECLARING_TOPOLOGY
  -> RESTORING_CONSUMERS
  -> READY
       |  (connection lost / channel error)
       v
  BACKOFF -> CONNECTING
```

每一层的失败处理不同：

| 阶段 | 失败 | 处理 |
|---|---|---|
| TCP/TLS | 网络、证书 | 退避重连；证书错误不要无限重试 |
| AUTH | 密码/token 过期 | 刷新凭据；告警；不要用固定密码重试风暴 |
| vhost | 权限/vhost 不存在 | 修复配置；重试无意义 |
| channel | 队列不存在/参数不一致 | 修正拓扑；关闭 channel，不要重连整条连接 |
| consumer | 队列被删/被独占 | 重建消费者；区分永久/暂时错误 |
| publish | confirm 超时 | 视为“结果未知”，重发 + 幂等 |

重连后的恢复清单：

- [ ] 重新打开 channel；
- [ ] 重新声明 exchange/queue/binding（或确认平台已保证）；
- [ ] 重新设置 QoS/prefetch；
- [ ] 重新注册 consumer，并处理 consume-ok 之前的投递；
- [ ] 重置 confirm 未确认集合（旧连接的 confirm 不会再来）；
- [ ] 重发“结果未知”的消息（依赖幂等）；
- [ ] 记录重连次数、耗时、最后错误。

一个常见事故是：自动重连成功、连接显示绿色，但消费者没有恢复，队列持续积压。监控必须看“消费者数量/消费速率”，不能只看连接状态。

## channel 泄漏与容量排查

channel 泄漏的典型信号：

- 连接数稳定，但 channel 数持续上涨；
- 管理 API 中大量 channel 没有 consumer、长期 idle；
- broker 内存上涨，GC/调度变慢；
- 客户端每请求 `channel()` 但没有 `close()`。

排查命令：

```bash
rabbitmqctl list_connections name user vhost channels state
rabbitmqctl list_channels connection number consumer_count messages_unacknowledged
rabbitmqctl list_consumers -p /app
```

客户端侧要检查：

- 是否在 `finally`/`defer`/`using` 中关闭 channel；
- 是否把 channel 缓存在全局但从不清理；
- 是否每次请求创建连接池之外的 channel；
- 是否在重连后重复创建 channel。

经验值：每进程几十到几百个 channel 通常可控；几万个 channel 会显著增加 broker 内存和调度开销。不要用“channel 很轻”作为每请求建 channel 的理由。

## vhost 多租户设计

vhost 是最常用的隔离单元，但它不是“多租户银弹”。设计时要区分：

| 隔离需求 | 推荐 | 说明 |
|---|---|---|
| 环境隔离（dev/staging/prod） | 独立 vhost 或独立集群 | 生产建议独立集群 |
| 业务域隔离 | 独立 vhost | 权限、命名、配额清晰 |
| 单租户内多应用 | 同 vhost + 命名规范/ACL | 避免 vhost 数量爆炸 |
| 强安全隔离 | 独立集群/独立账号 | vhost 不隔离网络与资源 |
| 按租户计费/配额 | 独立 vhost + 监控/限制 | vhost 本身不提供硬配额 |

vhost 的代价：

- 每个 vhost 有独立的交换机/队列/绑定/策略；
- 大量 vhost 会增加元数据和启动/恢复时间；
- 跨 vhost 不能直接绑定，需要 Shovel/Federation 或应用层转发；
- 权限正则要按 vhost 维护，容易漂移。

多租户的常见反模式是“每个租户一个永久队列 + 一个 vhost”，租户增长后元数据爆炸。更现实的做法是：按业务域分 vhost，租户用 routing key/header 或独立队列 + TTL/清理策略，配合配额和监控。

## blocked 连接的客户端处理

broker 触发内存/磁盘告警时会阻塞发布连接。客户端应该把 blocked 当成“背压信号”，而不是“网络错误”：

```python
import threading
import time

import pika

# 省略认证/主机配置；实际项目从环境变量或密钥管理读取
params = pika.ConnectionParameters(host="localhost", port=5672)

blocked = threading.Event()

def on_blocked(connection, method):
    blocked.set()
    # 暂停生产者、记录指标、触发告警

def on_unblocked(connection, method):
    blocked.clear()
    # 缓慢放量，不要瞬间打满

connection = pika.BlockingConnection(params)
connection.add_on_connection_blocked_callback(on_blocked)
connection.add_on_connection_unblocked_callback(on_unblocked)
channel = connection.channel()

def publish_with_backpressure(body):
    while blocked.is_set():
        time.sleep(0.5)
    channel.basic_publish(exchange="orders", routing_key="order.created", body=body)
```

要点：

- blocked 期间不要无限重试发布，否则会加重 broker 负担；
- 本地缓冲要有上限和落盘策略，否则只是把内存问题转移到应用；
- 关键消息要 confirm + 幂等；
- 监控 blocked 的连接数、持续时间和恢复时间；
- 真实 RabbitMQ 的行为可能是“阻塞”而不是立即报错，客户端库的暴露方式不同，要以目标库文档为准。

## 本章小结

连接是昂贵的长生命周期资源，信道是轻量的逻辑多路复用单元，vhost 是拓扑与权限的命名空间。理解三者的所有权和关闭语义，才能避免连接泄漏、消费者丢失、未确认消息重投和权限越界。blocked 连接是 broker 保护自己的手段，客户端必须主动响应。

下一章进入交换机与绑定，解释 direct、fanout、topic、headers 的路由算法，以及备用交换机、交换机到交换机绑定和一致性哈希插件。

---

[上一章：第 03 章 AMQP 0-9-1 协议深潜](03-amqp-0-9-1-protocol.md) | [返回目录](../README.md) | [下一章：第 05 章 交换机、绑定与路由算法](05-exchanges-routing.md)
