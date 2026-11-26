# 附录 F 术语表

## A

**ACK（确认）**：消费者告诉 broker“消息已处理”的动作。未 ack 的消息在 channel/connection 关闭后重投。

**Acquired count（获取计数）**：4.3+ Quorum 队列统计消息被重新获取/退回次数的计数；`basic.nack`/AMQP `released` 主要增加它，不消耗 `delivery-limit`。

**Alternate Exchange（备用交换机）**：交换机无法路由消息时转发的备用目标。

**AMQP 0-9-1**：RabbitMQ 的核心二进制协议，使用 connection/channel/exchange/queue 模型。

**AMQP 1.0**：另一个消息协议，使用 link/credit/settlement 模型，与 0-9-1 不兼容。

**Auto-delete queue**：最后一个消费者取消后删除的队列（曾有过消费者）。

**Auto-delete exchange**：最后一个绑定移除后删除的交换机。

## B

**Basic**：AMQP 中包含 publish/consume/ack 等方法的类（class 60）。

**Binding**：交换机到队列/交换机的路由规则，可带 routing key 和 arguments。

**Blocked connection**：broker 因内存/磁盘告警暂停发布者的连接状态。

**Broker**：接收、路由、存储、投递消息的中间件进程。

## C

**Causal order（因果序）**：只保证有因果依赖的消息有序，而不是所有消息全序；通常需要版本向量、依赖标记或业务状态机。

**Channel（信道）**：连接上的逻辑多路复用单元，承载大多数操作和确认状态。

**Chaos engineering（混沌工程）**：用受控故障实验验证系统稳态、恢复和告警的工程实践。

**Classic Queue**：单节点经典队列，支持优先级，不跨节点复制。

**CQv2（Classic Queue v2）**：4.x 唯一保留的 classic 存储实现；4.4 起的共享存储 v2 使用 `.sqs` 文件。

**Confirm（publisher confirm）**：broker 告知发布者消息已被接受（并按配置持久化/复制）。

**Consistent hash exchange（一致性哈希交换机）**：按哈希环把同一 routing key 稳定映射到队列的插件交换机，增删绑定会迁移部分 key。

**Consumer**：从队列接收消息的客户端实体。

**Consumer tag**：消费者在 channel 内的标识。

**consumer_timeout**：消费者在配置时间内未 ack 时的超时保护；4.3 起可按队列/消费者配置，触发后关闭消费者/channel 并重投未确认消息。

**Content header**：消息属性帧，含 body-size 和 Basic 属性。

**Connection**：TCP/TLS 连接，承载认证、vhost、心跳和多个 channel。

**Correlation ID**：请求/响应关联标识，常用于 RPC。

**Credit**：流控额度，AMQP 1.0/Stream 中常见；0-9-1 用 prefetch/blocked 等机制。

## D

**Dead Letter Exchange（DLX）**：接收死信的交换机。

**Dead letter**：被拒绝、过期、超长、超过投递次数等无法正常处理的消息。

**Delayed retry（原生延迟重试）**：4.3+ Quorum 队列在队列内把失败消息放置一段时间再重投，参数为 `x-delayed-retry-*`；不是通用定时投递。

**Delivery count（投递计数）**：Quorum 队列统计“投递失败”次数的计数；`basic.reject`/`modified delivery-failed` 增加它并消耗 `delivery-limit`。

**Delivery tag**：投递确认编号，AMQP 中属于 channel。

**Durable**：队列/交换机元数据在 broker 重启后保留。

**Direct exchange**：按 routing key 精确匹配的交换机。

## E

**Envelope**：事件元数据外壳（id/type/source/time/trace 等），与业务 `data` 分离，便于统一治理。

**Event contract（事件契约）**：事件的语义、结构、标识、兼容性、所有权和生命周期的组合，不只是 JSON 字段。

**Exchange（交换机）**：根据绑定路由消息，不存储消息。

**Exchange-to-exchange binding（E2E）**：交换机之间的绑定，形成路由层级。

**Exactly-once**：端到端恰好一次；通常需要业务幂等/事务，不是 broker 单独保证。

## F

**Fanout exchange**：忽略 routing key，广播到所有绑定队列。

**Feature flag**：RabbitMQ 跨版本的元数据/行为兼容开关；升级前启用、回滚前确认不可逆。

**Field table**：AMQP 的键值表类型，用于 headers/arguments。

**Federation**：跨 broker 的交换机/队列复制机制（插件）。

**FIFO**：先进先出。RabbitMQ 只在单队列、单消费者、无重投、无优先级/溢出重排的窄条件下近似成立；多消费者、requeue、DLX 和跨 broker 复制都会削弱它。

**Flow control**：broker/客户端限制流入的机制。

## H

**Headers exchange**：按消息 headers 匹配绑定参数。

**Heartbeat**：空闲连接上的空帧，用于检测半开连接。

## I

**Idempotency（幂等）**：同一操作执行多次结果一致。

**Idempotency key（幂等键）**：用于识别重复请求/消息的业务键或 message_id，常与唯一约束/去重表配合。

**Inbox**：消费端落库/去重模式。

## K

**Khepri**：基于 Raft 的元数据存储；4.2 起新集群默认，4.3 起是唯一支持的元数据存储（Mnesia 移除）。

## L

**Lazy queue**：历史队列模式，减少内存占用；新版本行为/可用性随版本变化。

**Link credit**：AMQP 1.0 中接收方授予发送方的发送额度；credit 不是 ack。

**Longstr**：AMQP 变长字符串类型。

## M

**Mandatory**：发布时若不可路由，要求 broker 退回消息。

**max_message_size**：整条消息 body 的上限；4.0 起默认 16 MiB，与 `frame_max`（单帧大小）不同。

**Message TTL**：消息存活时间（毫秒）。

**Modulus hash exchange（`x-modulus-hash`）**：4.3+ 内置交换机，按 routing key 哈希后对绑定目标数取模；拓扑固定时稳定，增删目标会重排几乎所有 key。

**MPL 2.0**：RabbitMQ 使用的 Mozilla Public License 2.0。

## N

**Nack**：否定确认，可 requeue 或死信。

**No-route**：消息没有匹配的目标队列。

## O

**Osiris**：RabbitMQ Streams 使用的追加日志库，负责段文件、读取和 retention。

**Outbox**：业务数据与待发消息同事务落库，再由独立发布器发送的模式。

## P

**Persistent message**：`delivery_mode=2` 的消息，配合 durable 队列/持久化存储。

**Partition key（分区键）**：决定消息进入哪个 Stream 分区/哈希队列的业务键；同一 key 固定进入同一分区，是“每键序”的前提。

**Per-key ordering（每键序）**：同一业务实体（订单/账户/设备）内有序、不同实体可并行的顺序模型；RabbitMQ 通常用分区键 + 单消费者/按 key 串行化实现。

**Policy**：按正则匹配资源、动态覆盖队列/交换机参数的机制。

**Prefetch**：每个消费者最多未确认消息数。

**Publisher confirm**：发布确认机制。

**Publish/Subscribe**：发布订阅模式，常用 fanout/topic。

## Q

**Quorum Queue**：基于 Raft 的复制队列，要求多数派持久化。

**Quorum priority**：4.0–4.2 为 normal/high 两档（约 2:1 调度）；4.3+ 为 32 级严格优先级（0–31，总是启用，`x-max-priority` 被忽略）。

**Quorum**：多数派，`floor(N/2)+1`。

## R

**Ra**：Erlang 的 Raft 库，Quorum Queue 的复制/选举/日志基础。

**RabbitMQ**：基于 Erlang/OTP 的开源消息 broker。

**Raft**：共识算法，用于 Quorum Queue 的复制/选举/日志。

**Redelivered**：消息被重新投递的标记。

**Resequencer（重排序器）**：应用层按 `(key, seq)` 缓冲乱序消息，等缺口补齐后再按序交付；需要处理缺号超时、缓冲上限和重复。

**Retention**：Stream 保留消息的时间/大小策略。

**Routing key**：发布时用于路由的字符串。

## S

**Schema registry**：集中管理事件 schema 及其版本/兼容性的服务或仓库，RabbitMQ 侧常需自建或结合 Avro/Protobuf/JSON Schema。

**Settlement**：AMQP 1.0 中接收方对 transfer 的结算/结果，常见状态为 accepted/rejected/released/modified。

**Shovel**：从源队列拉取并转发到目标交换机/队列的插件/工具。

**Single Active Consumer（SAC）**：队列同时只有一个 active 消费者。

**Stream**：追加日志队列类型，消费者按 offset 读取，读不删除。

**Stream initial offset（`x-stream-initial-offset`）**：4.4 创建 Stream 时的队列参数，设置第一条消息的 offset；不是消费者起点。

**Stream offset（`x-stream-offset`）**：消费者的读取起点，可取 `first`/`last`/`next`/数值；与 `x-stream-initial-offset` 不是一回事。

**Super Stream**：分区 Stream 集合，按 key 哈希分区。

**Snapshot**：某一时刻的状态副本；Mini 用 JSON 快照持久化。

**.sqs 文件**：4.4 classic queue 共享存储 v2 的文件扩展名；节点写入后不能降级到 4.3.x 或更早。

## T

**Topic exchange**：支持 `*`/`#` 通配的交换机。

**Total order（全序）**：所有消息共享同一个顺序；通常靠单队列/单分区 + 单消费者实现，吞吐和可用性受限。

**Toxiproxy**：在应用与 broker 之间注入延迟、断连、带宽限制等故障的代理工具。

**Transaction（tx）**：AMQP channel 上的发布/确认批量提交，非 XA。

**TTL**：Time To Live，消息/队列存活时间。

## U

**Unacked**：已投递但未确认的消息。

## V

**Virtual host（vhost）**：RabbitMQ 的命名空间/权限边界。

## W

**WAL**：Write-Ahead Log，预写日志；Mini 未实现。

## X

**x-death**：死信消息 headers 中的死亡记录数组。

**x-delayed-retry 参数**：4.3+ Quorum 队列的原生延迟重试参数（`x-delayed-retry-type`/`-min`/`-max`）；类型可为 `failed`/`returned`/`all`，延迟按线性退避。

**x-max-priority**：classic 队列的多级优先级参数；Quorum 队列忽略它（4.3+ 的 32 级优先级总是启用）。

**x-opt-delivery-time**：4.4 Quorum 队列的生产者定时投递 header/annotation，Unix epoch 毫秒、long 类型；classic 队列不支持。

**x-overflow**：队列满时的溢出策略（drop-head/reject-publish/reject-publish-dlx）。

**x-stream-initial-offset**：见 Stream initial offset；创建 Stream 时的首条消息 offset。

**x-stream-offset**：消费者读取 Stream 的起点；见 Stream offset。

## 其他

**At-least-once**：至少一次，可能重复。

**At-most-once**：最多一次，可能丢。

**SLA/SLO/SLI**：服务等级协议/目标/指标。

**TCO**：总拥有成本。

**Backpressure**：下游处理不过来时的反向压力。

**Coordinated omission**：压测中因等待而漏记延迟的偏差。

**Poison message**：反复处理失败的消息。

**Parking lot**：无法自动处理的消息的人工/离线存放队列。

**Schema Registry**：事件 schema 的注册/兼容性管理。

**CloudEvents**：事件元数据标准。

**AsyncAPI**：异步 API/事件契约描述规范。

**OpenTelemetry**：可观测性标准/工具集。

---

[返回目录](../README.md) | [上一章：附录 E 故障排查手册](E-troubleshooting.md) | [下一章：附录 G 代码地图与测试矩阵](G-code-map-test-matrix.md)
