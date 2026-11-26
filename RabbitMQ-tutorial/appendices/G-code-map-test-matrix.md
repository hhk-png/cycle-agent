# 附录 G 代码地图与测试矩阵

## 包结构

| 文件 | 职责 | 关键入口 |
|---|---|---|
| `mini_rabbitmq/broker.py` | 门面、用户/vhost、投递标签、告警、持久化 | `publish`, `get`, `ack`, `add_consumer`, `close_owner` |
| `mini_rabbitmq/vhost.py` | 拓扑、绑定、路由、策略、死信 | `declare_queue`, `bind_queue`, `publish`, `dead_letter` |
| `mini_rabbitmq/exchange.py` | 交换机类型与匹配 | `route`, `topic_matches`, `ConsistentHashExchange` |
| `mini_rabbitmq/queue.py` | 队列、优先级、TTL、确认、消费者 | `enqueue`, `dispatch`, `ack`, `nack` |
| `mini_rabbitmq/message.py` | 消息/投递 | `Message`, `Delivery`, `add_death` |
| `mini_rabbitmq/policy.py` | policy 合并/映射 | `PolicySet`, `Policy` |
| `mini_rabbitmq/auth.py` | 密码哈希/ACL | `AuthStore`, `hash_password`, `Permission` |
| `mini_rabbitmq/metrics.py` | 指标 | `Metrics` |
| `mini_rabbitmq/storage.py` | 快照 | `SnapshotStore` |
| `mini_rabbitmq/protocol.py` | AMQP 编解码 | `Writer`, `Reader`, `encode_content_header` |
| `mini_rabbitmq/server.py` | TCP 服务端/握手 | `AMQPServer`, `ConnectionHandler`, `ChannelState` |
| `mini_rabbitmq/client.py` | 同步客户端 | `AMQPClient`, `WireDelivery` |
| `mini_rabbitmq/management.py` | HTTP 管理/指标 | `ManagementServer` |
| `mini_rabbitmq/streams.py` | Stream/SuperStream 模型 | `Stream`, `SuperStream` |
| `mini_rabbitmq/cluster.py` | Raft 模拟 | `RaftCluster`, `QuorumQueue` |
| `mini_rabbitmq/links.py` | Shovel/Federation | `Shovel`, `FederationLink` |
| `mini_rabbitmq/cli.py` | CLI | `serve`, `diagnose` |
| `mini_rabbitmq/__main__.py` | `python -m` 入口 | `main` |

## 一次 publish 的调用链

```text
AMQPServer/ConnectionHandler._frame_loop
  -> _handle_channel_method
  -> _basic_class / _basic_publish
  -> _handle_content_header / _handle_content_body
  -> _finish_publish
  -> Broker.publish_message
  -> VHost.publish
  -> Exchange.route / VHost._route_exchange
  -> Queue.enqueue
  -> Queue.dispatch
  -> ConnectionHandler._deliver_to_client
```

## 一次 ack 的调用链

```text
ConnectionHandler._basic_ack
  -> Broker.ack
  -> Queue.ack
  -> Queue.dispatch
  -> ConnectionHandler._deliver_to_client
```

## 测试矩阵

| 文件 | 收集数 | 主要覆盖 |
|---|---:|---|
| `tests/test_auth.py` | 13 | 哈希、认证、权限、序列化 |
| `tests/test_broker.py` | 33 | 默认交换机、AE、E2E、vhost、策略、告警、所有权 |
| `tests/test_cluster.py` | 13 | 选举、复制、提交、故障转移、日志冲突 |
| `tests/test_exchange.py` | 28 | 四类交换机、topic 边界、一致性哈希 |
| `tests/test_links.py` | 9 | Shovel/Federation、防环、失败重排 |
| `tests/test_management.py` | 12 | HTTP 认证/API/策略/指标/权限 |
| `tests/test_persistence.py` | 9 | 快照恢复/ack/用户/策略/损坏 |
| `tests/test_protocol.py` | 19 | 帧、字段表、content header、错误 |
| `tests/test_queue.py` | 32 | 优先级、TTL、溢出、ack、prefetch、DLX |
| `tests/test_streams.py` | 15 | offset、retention、订阅、SuperStream |
| `tests/test_ordering.py` | 8 | FIFO 边界、requeue、优先级、分区、单活消费者 |
| `tests/test_wire_server.py` | 28 | 握手、发布/消费、confirm、tx、ACL、大消息 |
| **合计** | **219** | |

`check_docs.py` 实际运行 `pytest --collect-only` 验证 219；`verify_all.py` 验证 219 passed 和 22 示例。

## 按特性查测试

| 特性 | 测试文件 |
|---|---|
| topic `*`/`#` | `tests/test_exchange.py` |
| headers all/any | `tests/test_exchange.py` |
| 一致性哈希 | `tests/test_exchange.py` |
| 默认交换机 | `tests/test_broker.py`、`tests/test_wire_server.py` |
| Alternate exchange | `tests/test_broker.py` |
| E2E 绑定/防环 | `tests/test_broker.py`、`tests/test_wire_server.py` |
| vhost 隔离 | `tests/test_broker.py` |
| 队列优先级 | `tests/test_queue.py` |
| TTL/过期 | `tests/test_queue.py` |
| 溢出策略 | `tests/test_queue.py` |
| 死信/x-death | `tests/test_queue.py`、`tests/test_broker.py` |
| delivery-limit | `tests/test_queue.py` |
| prefetch/公平分发 | `tests/test_queue.py` |
| 单活/排他消费者 | `tests/test_queue.py`、`tests/test_ordering.py` |
| 顺序/分区/重排序 | `tests/test_ordering.py`、`tests/test_streams.py` |
| publisher confirms | `tests/test_wire_server.py` |
| 事务 | `tests/test_wire_server.py` |
| 快照恢复 | `tests/test_persistence.py` |
| 密码/ACL | `tests/test_auth.py`、`tests/test_wire_server.py` |
| 管理 API | `tests/test_management.py` |
| Streams | `tests/test_streams.py` |
| Raft | `tests/test_cluster.py` |
| Shovel/Federation | `tests/test_links.py` |
| 协议编解码 | `tests/test_protocol.py` |
| 原始握手/大消息 | `tests/test_wire_server.py` |

## 运行单个测试

```bash
python -m pytest -q tests/test_queue.py
python -m pytest -q tests/test_wire_server.py::test_raw_handshake_connection_start
python -m pytest -q -k "dead_letter or confirm"
```

## 代码不变量

1. ready 堆与 unacked 表不重复计数；
2. 未确认消息在 channel 关闭后重投；
3. ack 必须释放 broker tag 映射；
4. requeue 保留优先级/序号并标记 redelivered；
5. TTL 过期消息不进入投递；
6. 死信循环有跳数限制；
7. 独占队列随 owner 删除；
8. 快照只保留 durable/persistent；
9. 协议帧 METHOD/HEADER/BODY 不交错；
10. confirm 只在消息被接受后发送；
11. 同优先级消息按入队序号投递，requeue 消息带 redelivered 标记；
12. Super Stream 的同一 key 固定映射到同一分区，分区内按 append 顺序读取。

## 扩展代码的建议

- 新交换机类型：继承 `Exchange`，实现 `binding_matches`/`route`，注册到 `ExchangeTypeRegistry`；
- 新队列策略：在 `policy.py` 映射，Queue 的 `_refresh_settings` 读取；
- 新协议方法：`protocol.py` 常量 + `server.py` 分发 + `client.py` 封装 + 原始帧测试；
- 新管理端点：`management.py` 路由 + 权限检查 + 测试；
- 新持久化后端：实现 `SnapshotStore` 接口或替换 Broker 存储；
- 新复制：参考 `cluster.py`，但生产需真实网络/持久化/成员变更。

## 已完成 vs 未完成

**已完成**：AMQP 子集、四类交换机、一致性哈希、E2E/AE、vhost/ACL、队列语义、TTL/优先级/溢出、DLX/x-death、prefetch/ack/nack、confirms/tx、快照、管理 API/Prometheus、Streams 模型、Raft 模拟、Shovel/Federation。

**未完成**：完整 AMQP 方法集/官方互操作、TLS 测试、OAuth/LDAP、多进程集群、真实 Raft、Stream 协议、MQTT/STOMP/AMQP 1.0、生产级性能/备份/审计。

## 代码阅读路线

1. `message.py` → 数据结构；
2. `exchange.py` → 路由；
3. `queue.py` → 队列/消费者；
4. `vhost.py` → 拓扑/死信；
5. `broker.py` → 门面/所有权/持久化；
6. `protocol.py` → 字节；
7. `server.py` → 协议状态机；
8. `client.py` → 对端；
9. `management.py` / `cluster.py` / `links.py` → 外围能力；
10. `tests/` → 语义证据。

读完 Mini 后，如果要对照真实 RabbitMQ 的 reader/channel/queue、Ra/Osiris/Khepri 和插件体系，继续看[附录 Q](Q-broker-internals-source-map.md)。

## 术语一致性

- “消息”通常指 AMQP message；
- “投递”指 Delivery/消费尝试；
- “确认”指 consumer ack 或 publisher confirm，需看上下文；
- “死信”指进入 DLX 的消息；
- “快照”专指 Mini 的 JSON 持久化，不指 RabbitMQ 的 Quorum snapshot。

---

[返回目录](../README.md) | [上一章：附录 F 术语表](F-glossary.md) | [下一章：附录 H 实验课与生产上线检查清单](H-labs-production-checklist.md)
