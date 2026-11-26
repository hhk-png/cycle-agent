# 第 14 章 协议生态与多语言客户端

> 本章定位：RabbitMQ 不是“一个协议”。AMQP 0-9-1 是核心，但 AMQP 1.0、MQTT、STOMP、WebSocket、HTTP 和 Stream 协议各有语义。客户端库的并发模型、确认方式和重连行为，往往比协议本身更影响生产稳定性。

## 协议全景

| 协议 | 默认端口 | 模型 | 典型用途 | 插件/说明 |
|---|---|---|---|---|
| AMQP 0-9-1 | 5672 / 5671 TLS | 交换机/队列/信道 | 业务消息、任务、RPC | 核心 |
| AMQP 1.0 | 5672 / 5671 TLS | session/link/credit/settlement | 跨厂商消息、Azure 生态 | 核心（4.0+ 默认启用） |
| MQTT | 1883 / 8883 TLS | topic、QoS 0/1 | IoT、移动端 | 插件 |
| STOMP | 61613 | 文本帧、SEND/SUBSCRIBE | 轻量 Web/脚本 | 插件 |
| Web STOMP | 15674 | STOMP over WebSocket | 浏览器 | 插件 |
| Web MQTT | 15675 | MQTT over WebSocket | 浏览器/IoT | 插件 |
| Stream | 5552 / 5551 TLS | 追加日志、offset | 高吞吐事件流 | Stream 插件/核心 |
| HTTP Management | 15672 | REST/JSON | 运维控制面 | 管理插件 |
| Prometheus | 15692 | 文本指标 | 监控 | Prometheus 插件 |

同一台 broker 可以同时监听这些协议，但“能连上”不等于“语义等价”。一个 MQTT 客户端发布的 QoS 1 消息，和 AMQP 0-9-1 的 publisher confirm、AMQP 1.0 的 settlement，不是同一件事；跨协议桥接时要把确认、重试、死信和属性映射重新设计。

```text
                    ┌──────────────────────────────┐
AMQP 0-9-1  ───────▶│                              │
AMQP 1.0    ───────▶│          RabbitMQ            │
MQTT        ───────▶│  exchange / queue / stream   │
STOMP       ───────▶│  vhost / auth / policy       │
Stream      ───────▶│                              │
                    └──────────────────────────────┘
```

不要把管理 HTTP API 当消息数据面。它用于运维、审计、查询，不适合高频发布/消费；管理 API 的写操作也通常需要更高权限。

## AMQP 1.0：不同模型，不是版本号

AMQP 0-9-1 与 AMQP 1.0 不是版本关系，而是两个不同的协议。0-9-1 的核心是 exchange/queue/channel；1.0 的核心是 connection/session/link/transfer：

```text
connection
  └── session
        ├── sender link  ──transfer──>  broker target (exchange/queue)
        └── receiver link <──transfer──  broker source (queue)
```

关键机制：

- **credit**：接收方授予发送方“还能发多少条”的额度；发送方耗尽 credit 必须等待；
- **settlement**：接收方对 transfer 做 accepted/rejected/released 等 disposition；
- **link**：单向消息流，可独立配置 source/target、过滤器、可靠性；
- **session**：多路复用和流控的上下文；
- **source/target address**：由 broker 定义地址语义，RabbitMQ 把 target 映射到 exchange、source 映射到 queue。

与 0-9-1 的差异：

| 0-9-1 | 1.0 | 注意 |
|---|---|---|
| exchange/queue | target/source address | 地址语义由 broker 定义 |
| channel | session | 多路复用模型不同 |
| basic.ack | disposition/settlement | 确认粒度/状态不同 |
| prefetch | link credit | 流控模型不同 |
| confirm.select | sender settle mode | 不是一一对应 |
| headers | application-properties/message-annotations | 属性映射不一一对应 |

版本变化：

- 3.x：AMQP 1.0 通过 `rabbitmq_amqp1_0` 插件提供；
- 4.0：AMQP 1.0 成为默认启用的核心协议，旧插件变成 no-op；
- 4.2：AMQP 1.0 支持 Direct Reply-To，RPC 模式不再必须自建回复队列；
- 4.4：支持 AMQP 1.0 连接唯一性（sole connection，按 container ID 在集群范围去重）和更完整的 JMS topic 语义（商业版）。

使用前确认：

- 目标版本是否默认启用（4.0+ 是）；
- 客户端库支持目标特性（publisher confirms 映射、dead-letter 映射、Direct Reply-To）；
- 管理/监控能看到 1.0 连接；
- 认证/TLS 配置；
- 消息属性映射规则。

如果团队已有 0-9-1 代码，迁移到 1.0 不是“换个端口”，而是客户端模型和语义的重新设计。不要假设“confirm 就是 settlement”“prefetch 就是 credit”；确认的粒度、失败语义、重投行为都要重新验证。帧布局、类型系统、SASL、link credit、transfer/settlement 和 RabbitMQ 地址映射的细节见[附录 R](../appendices/R-amqp-1-0-deep-dive.md)；互操作实验见[附录 W](../appendices/W-4x-feature-labs.md)，版本差异的事实核查方法见[附录 X](../appendices/X-fact-check-and-sources.md)。

## MQTT：为受限设备设计，不是 AMQP 的简化版

MQTT 的模型是 topic 层级 + QoS：

- QoS 0：最多一次；
- QoS 1：至少一次；
- **RabbitMQ 的 MQTT 插件只支持 QoS 0 和 QoS 1，不支持 QoS 2**。客户端可以请求 QoS 2 订阅，但 broker 最多授予 QoS 1；发布 QoS 2 会失败，也不会交换 PUBREC/PUBREL/PUBCOMP；
- retained message、last will、session。

RabbitMQ 的 MQTT 插件把 MQTT topic 映射到 exchange/queue。适合：

- 大量 IoT 设备；
- 低带宽/不稳定网络；
- 设备影子/命令下发；
- 移动端推送。

注意：

- MQTT 的 topic 分隔符 `/` 与 AMQP routing key 的 `.` 不同；桥接时要映射；
- 不要把 QoS 2 写进契约：RabbitMQ 插件只到 QoS 1，“恰好一次”需要业务幂等；
- 插件版本、共享订阅、retained 行为要核对；
- 大量长连接对 broker 的文件描述符/内存是挑战；
- MQTT 的持久会话会为每个客户端维护队列，客户端数量大时元数据成本高。

### MQTT 5 与共享订阅

MQTT 5 带来的常用能力：

- **消息属性/用户属性**：更丰富的元数据；
- **会话过期/消息过期**：更细的生命周期；
- **原因码/返回码**：更明确的错误；
- **主题别名**：减少重复 topic 的带宽。

**共享订阅（shared subscription）是 MQTT 5 的能力，但 RabbitMQ 的 MQTT 插件目前不支持**，不能把它当成竞争消费者的替代；需要竞争消费时，仍应使用 AMQP 队列或多个独立订阅 + 业务侧负载分配。

这些能力增强了 MQTT 的工程可用性，但不改变 RabbitMQ 的底线：QoS 2 不支持，跨协议“恰好一次”仍要靠业务幂等。retained 消息的保留范围、session 过期后的离线消息行为，要以目标版本的 MQTT 插件文档为准。

## STOMP / Web STOMP

STOMP 是简单的文本协议，帧形如：

```text
SEND
destination:/queue/orders
content-type:application/json

{"id":1}
^@
```

常见 destination：

- `/queue/<name>`：点对点队列；
- `/topic/<name>`：发布/订阅；
- `/exchange/<exchange>/<routing-key>`：显式指定交换机和路由键。

优点：

- 易调试、易实现；
- 浏览器可通过 WebSocket 接入；
- 适合脚本/轻量集成。

缺点：

- 语义比 AMQP 简单；
- 确认/事务/路由能力有限；
- 性能不如二进制协议；
- 需要额外的 destination 映射。

4.4 起 STOMP 是**原生实现**，不再经过内部 AMQP 0-9-1 客户端。这会减少 Erlang 进程和内存开销，但也意味着连接/进程指标、错误日志、资源占用会与旧版本不同，监控面板要重新校准。

Web STOMP 适合“浏览器直接订阅事件”的场景，但要注意认证、CORS、TLS、消息大小和连接数。浏览器端没有可靠的“优雅关闭”，服务端要用心跳和会话超时清理僵尸连接。

## Stream 协议

Stream 协议面向高吞吐日志读取：

- 二进制协议，独立端口（常见 5552，TLS 5551）；
- 支持 offset、consumer group、credit、过滤；
- 与 AMQP 0-9-1 的 stream queue 访问方式不同；
- 客户端库包括 Java、Go、Python 等（以官方为准）。

如果只是用 AMQP 0-9-1 消费 stream 队列，功能可能受限；需要 consumer group、offset 提交、过滤、高吞吐时，使用 Stream 协议客户端。不要假设两种方式的 offset/确认语义完全一致，详见第 12 章。

## 客户端库的行为差异

同样的 AMQP 0-9-1，不同库的默认行为可能不同：

| 库 | 语言 | 并发模型 | 注意 |
|---|---|---|---|
| pika | Python | 阻塞/可选异步 | `BlockingConnection` 非线程安全；自动重连需自己实现 |
| aio-pika | Python | asyncio | RobustConnection/Channel；注意事件循环阻塞 |
| Spring AMQP | Java | 线程池 | 容器并发、ack 模式、重试拦截器 |
| rabbitmq-java-client | Java | 连接/信道 | 手动管理 confirm/consumer |
| .NET Client | C# | 异步 | `IChannel`/`IConnection` 生命周期 |
| amqplib | Node.js | 回调/Promise | channel 不是线程安全，注意 backpressure |
| amqp091-go | Go | goroutine | 连接复用、confirm 模式 |

选择客户端时看：

- 是否支持 publisher confirms；
- 自动重连是否恢复消费者/拓扑；
- 是否支持异步 confirm；
- 线程/协程安全模型；
- 背压/流控；
- 可观测性（指标/日志）；
- 维护活跃度与版本兼容。

客户端库的“自动重连”通常只恢复 TCP 连接，不恢复业务状态。重连后要重新声明拓扑、恢复消费者、恢复 prefetch、处理未确认消息；这些必须在应用层显式设计。

## 连接与通道的最佳实践（跨语言）

1. 连接长期复用，进程级；
2. channel 按用途隔离，避免并发 RPC；
3. 开启心跳，处理 blocked；
4. 声明拓扑幂等；
5. 发布者开启 confirms；
6. 消费者手动 ack + prefetch；
7. 优雅关闭；
8. 重连后重建 channel/consumer；
9. 消费者幂等；
10. 监控连接/channel/consumer 数。

自动重连库常见的坑：

- 重连后队列/交换机没重新声明；
- 消费者恢复但 prefetch 没恢复；
- confirm 未确认集合丢失；
- 重连风暴（所有客户端同时重连）；
- 旧 channel 对象仍被业务代码持有。

重连策略应包含指数退避 + 抖动，并限制最大重试。不要用固定 1 秒无限重连，broker 恢复瞬间会被打爆。

## 消息属性映射与协议桥接

不同协议对消息属性的表达不同：

| 属性 | 0-9-1 | 1.0 | MQTT | STOMP |
|---|---|---|---|---|
| 消息 ID | message-id | message-id | packet id/属性 | header |
| 关联 ID | correlation-id | correlation-id | user property | header |
| 内容类型 | content-type | content-type | content type | header |
| 持久化 | delivery-mode | durable/header | QoS/retain | header |
| 路由 | exchange + routing key | address | topic | destination |
| 确认 | ack/nack | disposition | QoS/puback | ACK |

跨协议桥接时，先定义“哪些属性是语义的一部分”。建议：

- 把业务必需字段放进 body/headers 的明确 schema，而不是依赖协议特有字段；
- 确认语义不要“降级”而不自知（例如 MQTT QoS 0 桥到 0-9-1 后仍可能丢）；
- 死信/重试在目标端重新设计；
- 记录来源协议/版本，便于排障；
- 做跨协议契约测试。

一个常见错误是把 `delivery_mode=2` 当成“跨协议持久化”。MQTT 的 QoS、STOMP 的 header、AMQP 1.0 的 durable header 各有自己的语义；桥接层必须显式转换，不能指望 broker 自动推导。

## Mini 的协议范围

Mini-RabbitMQ 只实现 AMQP 0-9-1 的一个子集：

- 连接/信道/交换机/队列/Basic/Confirm/Tx；
- 不支持 AMQP 1.0、MQTT、STOMP、WebSocket、Stream 协议；
- 不支持官方客户端互操作认证；
- 管理 API 是简化子集。

它的价值是让你能读、改、测协议核心，而不是替代官方客户端。第 23 章会逐段讲解握手、帧和客户端 RPC。

## 协议选型：先看语义，再看端口

| 场景 | 建议 | 原因 |
|---|---|---|
| 业务命令、任务、RPC | AMQP 0-9-1 | 路由/确认/死信语义最完整，生态成熟 |
| 跨厂商、云消息互通 | AMQP 1.0 | 标准化程度高，4.0+ 核心支持 |
| IoT、弱网、海量设备 | MQTT | 轻量、长连接、QoS 0/1 足够 |
| 浏览器/脚本轻量接入 | Web STOMP / Web MQTT | 不用装 AMQP 客户端 |
| 高吞吐事件日志、重放 | Stream 协议 | offset/consumer group/retention |
| 运维自动化 | HTTP Management API | 控制面，不是数据面 |

协议越多，运维面越大：认证、TLS、监控、限流、升级都要按协议分别验证。不要因为“RabbitMQ 支持”就同时引入五种协议；每引入一种，都要有对应的客户端规范、故障演练和 on-call 知识。

## 跨 broker 桥接：协议之外的状态机

协议支持解决的是“能连上”，桥接解决的是“崩溃后语义还对”。当 RabbitMQ 需要和 Kafka、数据库 CDC、SQS/SNS 或另一种协议共存时，真正的难点是确认顺序、checkpoint、幂等键和顺序键，而不是端口和客户端库。把“读源 → 转换 → 写目标 → 确认 → ack/commit”写成显式状态机，并用稳定事件 ID 让目标端去重，通常比换一个“支持更多协议”的 broker 更有效。完整的语义映射、五种桥接拓扑、崩溃矩阵和迁移 playbook 见[附录 Z](../appendices/Z-event-bridging-interop.md)，可运行的最小闭环见 `examples/20_bridge_interop.py`。

## 本章小结

RabbitMQ 的协议生态很宽：AMQP 0-9-1 是核心，AMQP 1.0 是不同模型，MQTT/STOMP/WebSocket 面向特定接入，Stream 协议面向日志。RabbitMQ 的 MQTT 插件只支持 QoS 0/1；AMQP 1.0 在 4.0 核心化、4.2 支持 Direct Reply-To、4.4 支持连接唯一性；STOMP 在 4.4 原生化。客户端库的并发、确认、重连和属性映射决定生产行为。选型时先确认语义和运维能力，再看协议名称。

下一章进入安全：认证、授权、TLS、OAuth、审计和最小权限，以及 Mini 的 ACL 实现与缺口。

---

[上一章：第 13 章 Federation、Shovel 与多活拓扑](13-federation-shovel.md) | [返回目录](../README.md) | [下一章：第 15 章 安全：认证、授权、TLS 与审计](15-security.md)
