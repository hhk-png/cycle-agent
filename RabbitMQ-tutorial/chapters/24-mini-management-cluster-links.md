# 第 24 章 Mini-RabbitMQ 管理面、集群模拟与互联

> 本章定位：数据面之外，一个 broker 还需要管理、监控、分布式语义和跨 broker 连接。本章拆解 Mini 的 Management API、Prometheus 指标、Streams 模型、Raft 模拟和 Shovel/Federation，并明确它们与真实 RabbitMQ 的差距。

## 管理面在整体架构中的位置

Mini 的数据面是 `broker.py` → `vhost.py` → `exchange.py` / `queue.py`：发布、路由、入队、投递、确认。管理面不参与消息路径，它只做三件事：

1. **观察**：连接、信道、队列、交换机、绑定、指标、健康检查；
2. **控制**：声明/删除/清空队列、发布测试消息、管理 policy；
3. **诊断**：节点信息、告警状态、Prometheus 文本。

这种分层很重要。管理面查的是内存中的实时对象，不是另一份副本；好处是没有同步延迟，坏处是查询会和数据面争抢 GIL，而且没有分页/缓存。生产 RabbitMQ 的 management 插件也是“读 broker 状态”，但它有独立的采样、缓存、分页和权限模型。

## Management HTTP API

`mini_rabbitmq/management.py` 用标准库 `ThreadingHTTPServer` 起一个独立端口（CLI 默认 15673）。每个请求经过：

```text
HTTP request
  -> Basic Auth（AuthStore）
  -> guest loopback 检查
  -> _route(method, path, user)
  -> 资源级 ACL（configure/write/read）
  -> JSON / Prometheus 响应
```

核心代码是 `ManagementServer._route` 和 `_queue_route` / `_exchange_route` / `_policy_route`。它们直接调用 `broker` 的门面方法，不复制业务逻辑。

### 主要端点

| 方法 | 路径 | 作用 | 权限 |
|---|---|---|---|
| GET | `/api/overview` | 节点、对象总数、队列总数、监听器 | 登录即可 |
| GET | `/api/whoami` | 当前用户/tags | 登录即可 |
| GET | `/api/connections` | 连接列表 | 登录即可 |
| GET | `/api/channels` | 信道列表 | 登录即可 |
| GET | `/api/vhosts` | 有权限的 vhost | 按用户 |
| GET | `/api/nodes` | 节点信息 | 登录即可 |
| GET | `/api/queues` | 队列列表/详情 | read |
| POST | `/api/queues/{vhost}/{name}/purge` | 清空队列 | configure |
| DELETE | `/api/queues/{vhost}/{name}` | 删除队列 | configure |
| GET | `/api/exchanges` | 交换机列表/详情 | read |
| POST | `/api/exchanges/{vhost}/{name}/publish` | 管理面发布 | write |
| GET | `/api/bindings` | 绑定列表 | read |
| GET/PUT/DELETE | `/api/policies` | 策略管理 | configure |
| GET | `/api/health/checks/alarms` | 告警健康检查 | 登录即可 |
| GET | `/metrics` | Prometheus 文本 | 登录即可 |

这些路径是 RabbitMQ Management HTTP API 的子集，字段名尽量贴近官方，便于把 Mini 的实验迁移到真实集群。

### 一次请求长什么样

```bash
# 概览
curl -s -u guest:guest http://127.0.0.1:15673/api/overview

# 队列列表（vhost 默认为 /）
curl -s -u guest:guest http://127.0.0.1:15673/api/queues

# 清空队列
curl -s -u guest:guest -X POST \
  http://127.0.0.1:15673/api/queues/%2F/orders/purge

# 管理面发布（JSON body）
curl -s -u guest:guest -X POST \
  -H 'content-type: application/json' \
  -d '{"routing_key":"order.created","payload":"hello","payload_encoding":"string"}' \
  http://127.0.0.1:15673/api/exchanges/%2F/orders/publish
```

注意：

- URL 中的 vhost `/` 编码为 `%2F`；
- 删除成功返回 `204`，没有 JSON body；
- policy 创建返回 `201`；
- 请求体非法返回 `400`，对象不存在返回 `404`，方法不支持返回 `405`；
- 认证失败返回 `401`，ACL 拒绝返回 `403`；
- 管理 API 是控制面，不要拿它做高频消息通道。

### 认证与授权

- Basic Auth 复用 `AuthStore`，密码哈希与 AMQP 登录一致；
- `guest` 只允许 loopback，避免默认用户从网络登录；
- 读队列/交换机需要 `read`，声明/删除/purge 需要 `configure`，发布需要 `write`；
- 权限检查按 vhost 做，`_vhost_allowed` / `_authorized` 是统一入口；
- 没有 RBAC 角色、审计日志、HTTPS、CORS、分页和排序。

测试：`tests/test_management.py` 覆盖认证、overview、队列/交换机/绑定、策略、发布、健康检查、指标、vhost 权限和 404。示例：`examples/13_management_api.py`。

## Prometheus 指标

Mini 的 `Metrics` 是零依赖的扁平计数器/仪表：

```text
messages_published_total
messages_routed_total
messages_delivered_total
messages_acked_total
messages_nacked_total
messages_redelivered_total
publisher_confirms_total
publisher_nacks_total
publish_rejected_total
connections_total
channel_errors_total
connections
queues
exchanges
consumers
messages_ready
messages_unacknowledged
alarm_active
```

`render_prometheus` 会为每个 key 输出 `# TYPE` 和值，并支持调用方拼接 label：

```text
mini_rabbitmq_messages_published_total 42
mini_rabbitmq_messages_ready{queue="orders",vhost="/"} 7
```

实现要点：

- key 里的 `-`、`.`、`/`、`:` 会被替换成 `_`（`_name`）；
- label 由调用方拼进 key，`render_prometheus` 只负责输出，不做校验；
- gauge 在 `broker.refresh_gauges()` 时更新；
- `/metrics` 额外输出每个队列的 ready/unacked/consumers label。

**高基数风险**：如果每租户/每队列都打 label，Prometheus 的 series 数量会爆炸。生产要限制 label 集合，把租户 ID 放到聚合维度或日志，而不是每个队列一个 label。

## Streams 模型

`mini_rabbitmq/streams.py` 用内存列表演示追加日志：

| 类 | 作用 |
|---|---|
| `StreamEntry` | offset、timestamp、body、headers、properties |
| `Stream` | append 分配 offset、按年龄/大小 trim、按 offset 读取 |
| `StreamConsumer` | 独立 offset、read/seek |
| `SuperStream` | 按 routing key SHA-256 哈希分区 |
| `SuperStream.append` | 选分区并 append |

关键语义：

- `append` 分配单调 offset，读取不删除；
- `max_age_seconds` / `max_length_bytes` 在 append 时触发 trim；
- `subscribe(start="first"/"last"/"next"/数字)` 决定消费者起点；
- `SuperStream` 只保证分区内有序，跨分区没有全局顺序；
- 消费者 offset 不持久化，进程重启会丢进度。

限制：

- 内存模型，无磁盘段文件、零拷贝、chunk；
- retention 只在 append 时触发，没有后台清理线程；
- 无消费者组协调/offset 提交；
- 无复制、无 Stream 协议；
- 未接入 AMQP 服务端（核心 Queue 拒绝 stream 类型）。

测试：`tests/test_streams.py`；示例：`examples/14_streams.py`。真实 RabbitMQ 的 Stream 还会涉及段文件、chunk、leader/replica、过滤和 Super Stream 分区发现，见[第 12 章](12-streams-super-streams.md)和[附录 Q](../appendices/Q-broker-internals-source-map.md)。

## Raft 模拟

`mini_rabbitmq/cluster.py` 用单进程对象模拟 Raft 的关键状态：

```text
RaftNode:  term, voted_for, log, commit_index, state, leader
RaftCluster: elect, append, replicate, append_entries, fail, recover
QuorumQueue: publish -> 提交 offset; messages() -> applied
```

### 选举

`RaftCluster.elect(preferred)`：

1. 选择一个 alive 节点（优先 preferred，否则日志最新的节点）；
2. term +1，投自己；
3. 向其他 alive 节点 `request_vote`；
4. 获得多数派（`floor(N/2)+1`）即成为 leader；
5. 初始化 `next_index` / `match_index`，发送心跳。

### 复制与提交

`append(command)`：

1. leader 追加本地日志；
2. 对每个 follower 从 `next_index` 复制；
3. 处理 `prev_log_index` / `prev_log_term` 不匹配，回退重试；
4. 多数派成功即 commit；
5. 应用状态机；
6. 向 follower 传播 commit index。

`fail(node_id)` / `recover(node_id)` 模拟节点故障和追赶；测试覆盖无 leader 不能写、选举、多数派提交、单节点故障仍可写、多数派丢失不可写、leader 故障转移、日志冲突截断和 QuorumQueue 故障转移。

### 与真实 Raft 的差距

Mini 没有实现：

- 网络 RPC、超时、重试、乱序/重复 RPC；
- 持久化日志、快照安装、日志压缩；
- 成员变更、预投票、领导租约、线性一致读；
- 分区/脑裂处理、客户端会话；
- 真实 Quorum Queue 的段文件、确认、流控和恢复。

因此它只能解释“多数派/选举/日志”的语义，不能证明生产 Quorum 的正确性。真实实现见 Erlang `ra` 库和 RabbitMQ 的 Quorum Queue 代码，[附录 Q](../appendices/Q-broker-internals-source-map.md)给了源码入口。

## Shovel 与 Federation

### Shovel

`mini_rabbitmq/links.py::Shovel` 是“从源队列 get，转发到目标交换机/队列”的最小模型：

1. `source_broker.get(src_queue, auto_ack=False)`；
2. 读取 `x-shovel-origin` 链；
3. 达到 `max_hops` 或重复 origin → 丢弃并 ack（防环）；
4. 发布到目标，带新的 origin；
5. 成功 ack 源消息，失败 `nack(requeue=True)`；
6. 统计 forwarded/failed/loop_dropped。

它是“至少一次”：目标成功但源 ack 前崩溃会重复。测试覆盖转发、属性保留、无路由 requeue、防环和 `max_messages`。

### Federation

先分清生产语义：Shovel 是一个**配置好的队列拉取器**（源队列 → 目标 exchange/queue）；Federation 是**交换机/上游事件复制**（exchange federation 复制消息，queue federation 是下游按需从上游拉取）。Mini 的 FederationLink 模拟前者的事件复制面：

`FederationLink` 不主动拉取，而是监听源 broker 的 publish 事件：

1. `start()` 注册 publish listener；
2. 匹配 vhost/exchange/routing key 正则；
3. 读取 `x-federation-origin`（Mini 的教学 header；真实 Federation 用内部 `x-received-from` + `max-hops`）；
4. 复制到目标 exchange；
5. `stop()` 移除监听。

它模拟“下游按需订阅上游”的语义，但没有 upstream 连接、断线重连、队列、TLS、动态配置和管理 API。测试覆盖转发、非匹配、origin 防环和 stop。

## 设计取舍：为什么管理面直接读内存

Mini 的 Management 直接读 broker 对象，不做分页/缓存：

- **优点**：简单、实时、少一层状态同步；
- **缺点**：大队列/大量对象时序列化慢，且与数据面共享 GIL；
- **生产做法**：独立采样/缓存、分页/过滤、限流、读写分离、审计写操作、只读账号与运维账号分离、不暴露公网。

管理写操作（purge/delete/policy/publish）没有审计/审批。生产必须补：谁在什么时候删了什么、为什么、能否回滚。这不是“管理 UI 的细节”，而是变更安全的一部分。

## 与真实 RabbitMQ 的映射

| Mini | RabbitMQ | 差距 |
|---|---|---|
| Management HTTP 子集 | `rabbitmq_management` 插件 | 分页/缓存/HTTPS/RBAC/审计 |
| Prometheus 文本 | `rabbitmq_prometheus` 插件 | 指标语义、label、聚合 |
| Stream 内存模型 | Streams + Stream 协议 | 段文件/复制/过滤/消费者组 |
| Raft 单进程模拟 | Quorum Queue 的 `ra` | 网络/持久化/成员变更/快照 |
| Shovel 手动 `run_once` | `rabbitmq_shovel` 插件 | 后台循环/重连/TLS/动态参数 |
| Federation publish listener | `rabbitmq_federation` 插件 | upstream/断线重连/策略 |

这些映射帮助理解概念，但不要假设实现细节一致。特别是管理 API 的字段、错误码和权限模型，版本间会变化；生产脚本要以目标版本文档为准。

## 能证明什么，不能证明什么

**能证明：**

- 管理 API 的认证、ACL、路由和 JSON 形状；
- Prometheus 文本格式和指标更新路径；
- Stream 的 offset/retention/分区语义；
- Raft 的多数派/选举/日志冲突/故障转移直觉；
- Shovel/Federation 的至少一次、防环和失败重排。

**不能证明：**

- 真实 management 插件的分页/缓存/HTTPS/RBAC；
- 真实 Stream 的段文件、复制和协议；
- 真实 Quorum 的网络分区、快照、成员变更和线性一致读；
- 真实 Shovel/Federation 的重连、TLS、动态策略和跨版本兼容；
- 生产级审计、配额、限流和多租户隔离。

这些边界在第 25 章统一说明。真实生产请使用 RabbitMQ 官方插件和发行版，不要把 Mini 的管理面直接暴露到网络。

## 本章小结

Mini 的管理面提供 overview/queues/exchanges/bindings/policies/health/metrics；Streams 模型演示 offset/retention/分区；Raft 模拟演示选举/复制/提交/故障转移；Shovel/Federation 演示跨 broker 转发与防环。它们共同说明：数据面之外，管理、监控、分布式共识和互联各有自己的状态机与失败模式。

下一章统一讨论测试、验证方法、219 项测试的分布、可发现的 bug 类型和覆盖盲区。

---

[上一章：第 23 章 Mini-RabbitMQ 线协议与客户端实现](23-mini-wire-protocol.md) | [返回目录](../README.md) | [下一章：第 25 章 测试、验证与实现边界](25-testing-verification-limitations.md)
