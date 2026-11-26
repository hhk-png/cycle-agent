# 第 20 章 Mini-RabbitMQ 架构与工程决策

> 本章定位：从“使用 RabbitMQ”切换到“实现一个 broker”。先看整体分层、线程模型、数据流和非目标，再进入各模块代码。目标是可读、可测、可解释，而不是复制生产实现。

## 目标与非目标

### 目标

- 用真实 TCP/AMQP 0-9-1 帧完成握手、发布、消费、确认；
- 覆盖 RabbitMQ 的核心语义：交换机、队列、绑定、vhost、ack、prefetch、DLX、TTL、优先级、confirms、事务；
- 提供管理 HTTP API 和 Prometheus 指标；
- 用测试证明关键不变量；
- 代码结构清晰，能对应到教程章节；
- 零第三方依赖，Python 3.10+ 可运行。

### 非目标

- 生产级吞吐/延迟；
- 完整 AMQP 方法集/官方互操作认证；
- 磁盘段文件、零拷贝、credit 流控；
- 真实 Raft 网络复制/成员变更；
- Stream 协议、MQTT、STOMP、AMQP 1.0；
- TLS 自动化测试、OAuth、审计；
- 高可用/备份/升级。

明确非目标让代码可以“为教学优化”，而不是在错误方向上堆复杂度。

## 分层

```text
┌──────────────────────────────────────────────────────────┐
│ CLI / examples / tests                                   │
├──────────────────────────────────────────────────────────┤
│ Management HTTP API        │ AMQP TCP server              │
│ (management.py)            │ (server.py)                  │
├──────────────────────────────────────────────────────────┤
│ Client (client.py)         │ Protocol codec (protocol.py)│
├──────────────────────────────────────────────────────────┤
│ Broker facade (broker.py)                                │
│  users, vhosts, delivery tags, alarms, storage, metrics  │
├──────────────────────────────────────────────────────────┤
│ VHost (vhost.py)                                         │
│  exchanges, queues, bindings, policies, dead-lettering   │
├──────────────────────────────────────────────────────────┤
│ Exchange (exchange.py)     │ Queue (queue.py)             │
├──────────────────────────────────────────────────────────┤
│ Message (message.py)       │ Storage (storage.py)         │
├──────────────────────────────────────────────────────────┤
│ Auth (auth.py) │ Policy (policy.py) │ Metrics (metrics.py)│
└──────────────────────────────────────────────────────────┘

Adjacent models:
  streams.py (Stream/SuperStream)
  cluster.py (Raft simulation)
  links.py (Shovel/Federation)
```

依赖方向总体向下，协议层不依赖业务实现细节，业务层不直接读写 socket。`server.py` 是适配器：把 AMQP 方法翻译成 Broker 调用；`client.py` 是测试/示例用的对端。

## 线程模型

- **AMQP 服务端**：每个 TCP 连接一个读线程；心跳线程可选；消息投递可能发生在发布线程、消费线程或维护线程。
- **发送锁**：每个连接一把 `RLock`，保证同一信道的 METHOD/HEADER/BODY 不交错。
- **队列锁**：每个 Queue 一把 `RLock`，保护堆、未确认表、消费者表。
- **分发锁**：Queue 单独的 `_dispatch_lock`，避免 callback 重入导致递归风暴。
- **Broker 锁**：保护投递标签映射、vhost 列表、告警监听器。
- **维护线程**：定期过期消息/队列、清理自动删除交换机。

锁顺序原则：不要在持有队列锁时做阻塞网络 IO；callback 在锁外/分发锁内执行；Broker 锁只做短临界区。代码中仍有可改进处（例如 callback 内的 dispatch 重入），但已用测试覆盖常见路径。

## 发布数据流

```text
ConnectionHandler._frame_loop
  -> _handle_channel_method
  -> _basic_publish
  -> _handle_content_header
  -> _handle_content_body
  -> _finish_publish
  -> Broker.publish_message
  -> VHost.publish
  -> Exchange.route / _route_exchange
  -> Queue.enqueue
  -> Queue.dispatch
  -> ConnectionHandler._deliver_to_client
  -> basic.deliver + header + body
```

关键点：

- `basic.publish` 方法先到，消息正文随后分帧；
- 路由和入队后才发 confirm；
- mandatory 不可路由时先发 `basic.return`；
- 队列满 reject-publish 时发 `basic.nack`；
- 事务模式下 publish 暂存，commit 时批量执行。

## 消费数据流

```text
basic.consume
  -> ChannelState 创建 ConsumerHandle（suspended=True）
  -> 发送 consume-ok
  -> 解除 suspended，Queue.dispatch
  -> ConsumerHandle.callback
  -> ConnectionHandler._deliver_to_client
  -> 客户端 callback
  -> basic.ack/nack
  -> Broker.ack/nack
  -> Queue.ack/nack
  -> 再次 dispatch
```

为什么 consume-ok 要先发？消费者可能在收到 ok 前就处理 deliver，导致客户端状态未就绪。Mini 用 suspended 标志确保顺序。

## 投递标签与所有权

Broker 维护全局递增的 delivery tag，并映射到 `(Queue, Delivery)`。`Delivery` 记录 owner（channel）、consumer tag、是否 auto_ack、原始队列序号、过期时间。

这解决了：

- 多队列/多 channel 的 tag 唯一性；
- ack 时找到对应队列；
- channel 关闭时按 owner 重投；
- 管理视图统计未确认数。

真实 AMQP 的 delivery tag 是 channel 级，不同 channel 可以重复。Mini 用全局 tag 简化实现，但对客户端仍表现为单调递增；这属于实现差异，不影响教程语义演示。第 23 章会从客户端视角再次对照；附录 G 的代码不变量也记录了它。

## 队列内部

`Queue` 用堆保存 ready 消息：

```text
(-priority, seq, expires_at, message)
```

- 负优先级让高优先级先出；
- seq 保证同优先级 FIFO；
- expires_at 用于过期扫描；
- 未确认消息放在 `_unacked`，不占 ready 堆；
- 消费者表支持 prefetch、优先级、单活、exclusive；
- 统计字段用于管理和调试。

## 路由

`VHost._route_exchange` 递归处理 E2E 绑定：

- visited 防环；
- 最大深度；
- destination 去重；
- 备用交换机；
- 内部交换机禁止直接发布；
- 默认交换机特殊处理。

真实 RabbitMQ 的路由表更复杂（按 binding 索引、并行匹配、策略），Mini 用线性扫描，适合学习，不适合大绑定表性能。

## 持久化

Mini 用 JSON 快照：

- durable 交换机/队列；
- persistent 消息（包括未确认？是的，快照会包含未确认，恢复为 ready）；
- 用户/权限/策略；
- 原子替换、可选 fsync。

这不是生产实现，但展示了：

- 哪些状态需要持久化；
- 恢复顺序（先策略，再拓扑，再消息，再绑定）；
- ack 与快照的时序；
- 崩溃窗口的语义。

## 管理面

`ManagementServer` 使用标准库 HTTP server：

- Basic Auth；
- `/api/overview`、queues、exchanges、bindings、policies、nodes；
- 健康检查；
- Prometheus 文本；
- 少量写操作（purge/delete/policy/publish）。

它是控制面，不是数据面；没有分页、RBAC 细粒度、审计、HTTPS。

## 测试策略

- 纯单元：exchange 路由、topic 匹配、字段表、auth、policy；
- 队列语义：优先级、TTL、溢出、ack、prefetch、DLX；
- Broker：拓扑、vhost、AE、E2E、告警、持久化；
- 协议：帧/内容头/字段表；
- 端到端：TCP 握手、发布/消费、confirms、事务、ACL；
- 管理：HTTP + Prometheus；
- 模型：Streams、Raft、Shovel/Federation。

测试数字：219 项，`check_docs.py` 会实际收集并核对。第 25 章给出分类和盲区。

## 设计取舍

| 决策 | 选择 | 原因 | 代价 |
|---|---|---|---|
| 语言 | Python | 可读、零依赖、测试快 | GIL/性能 |
| 存储 | JSON 快照 | 易调试 | 全量写、非生产 |
| 复制 | 单进程 Raft 模拟 | 可测试语义 | 无网络/持久化 |
| 路由 | 线性扫描 | 简单 | 大绑定表慢 |
| delivery tag | 全局递增 | 简化映射 | 与 AMQP 语义有差异 |
| Streams | 独立模型 | 聚焦语义 | 未接入 wire |
| 管理面 | 标准库 HTTP | 零依赖 | 功能有限 |
| 协议 | 子集 | 可维护 | 无官方互操作认证 |

这些取舍在第 25 章会逐项对照真实 RabbitMQ，说明“如果要生产化，下一步做什么”。

## 模块依赖与错误处理

依赖方向可以写成：

```text
protocol.py      (无业务依赖)
    ↑
server.py        (协议状态 + 适配)
    ↑
broker.py        (门面/所有权/持久化)
    ↑
vhost.py         (拓扑/路由/死信)
    ↑
exchange.py  queue.py  message.py  policy.py
    ↑
auth.py  metrics.py  storage.py
```

几条约束：

- `protocol.py` 不 import `broker`/`queue`；
- `queue.py` 不直接写 socket，只通过 callback；
- `vhost.py` 不处理 AMQP 方法，只表达路由/拓扑；
- `server.py` 把协议异常翻译成 reply-code，不让业务异常穿透读线程；
- `management.py` 只读/改 Broker/VHost，不直接操作队列内部结构。

错误分层：

| 层 | 异常 | 处理 |
|---|---|---|
| 协议编解码 | `FrameError`/`SyntaxError_` | connection close（501/502） |
| 信道操作 | `NotFound`/`AccessRefused`/`PreconditionFailed` | channel close（404/403/406） |
| 发布 | `PublishRejected`/`ResourceError` | confirm nack / channel close 506；`connection.blocked` 是告警通知，不是异常类型 |
| 业务 | 普通异常 | 记录日志，转成 channel/connection 错误 |

这种分层让测试可以只测“路由匹配”而不启 socket，也可以只测“握手”而不关心队列。

## 并发与锁模型的再审视

锁的清单和顺序原则已在上面的“线程模型”给出，这里只记录实现里仍然存在的取舍：

- callback 内 ack 会再次触发 dispatch，虽然有 `_dispatch_pending` 防递归，但延迟/公平性仍可优化；
- 全局 delivery tag 简化了映射，但与 AMQP 的 channel 级语义有差异；
- 管理 API 与数据面共享 GIL，大队列查询会拖慢数据面；
- 持久化快照在状态变更时触发，可能放大写放大；
- 死信/重投路径可能跨越“源队列锁 → vhost → 目标队列锁”，锁顺序并不像理想模型那样单向。

这些取舍在第 25 章统一讨论验证边界和生产化路线。

## 扩展点

如果要在 Mini 上做实验，推荐的扩展点：

| 目标 | 扩展点 |
|---|---|
| 新交换机类型 | `exchange.py::ExchangeTypeRegistry` + `binding_matches` |
| 新队列参数 | `queue.py::_refresh_settings` + `policy.py` 映射 |
| 新协议方法 | `protocol.py` 常量 + `server.py` 分发 + `client.py` 封装 |
| 新管理端点 | `management.py` 路由 + 权限 + 测试 |
| 新存储后端 | 实现 `SnapshotStore` 接口 |
| 新复制 | 参考 `cluster.py`，但要补网络/持久化/成员变更 |
| Stream 接入 wire | `server.py` 增加 stream queue 类型 + Stream 协议 |

扩展时先写测试，再改实现；否则很容易破坏“未确认消息不丢/ack 释放映射”等不变量。

Mini 的模块划分是真实 broker 的简化投影。真实 RabbitMQ 里，连接由 reader/writer 协作、每个 channel 是有状态进程；元数据存储分阶段演进：4.0/4.1 默认 Mnesia，4.2 新集群默认 Khepri，4.3+ 才是 Khepri only。Quorum Queue 用 Ra，Stream 用 Osiris；这些模块的职责、消息调用链和与 Mini 的逐项映射见[附录 Q](../appendices/Q-broker-internals-source-map.md)。

## 本章小结

Mini-RabbitMQ 是分层、可测试的教学 broker：协议层/服务端适配器/Broker 门面/VHost/Exchange/Queue/存储/管理面。它用真实 AMQP 帧和线程模型演示发布、消费、确认、死信、持久化；用单进程模拟 Raft 和 Streams；用测试证明关键不变量。它不是生产实现，但结构足够接近，能作为理解 RabbitMQ 内部原理的脚手架。

下一章逐段拆解核心 Broker、VHost 和 Queue 的实现。

---

[上一章：第 19 章 端到端可靠性与数据一致性](19-end-to-end-reliability.md) | [返回目录](../README.md) | [下一章：第 21 章 Mini-RabbitMQ 核心 Broker 实现](21-mini-core-broker.md)
