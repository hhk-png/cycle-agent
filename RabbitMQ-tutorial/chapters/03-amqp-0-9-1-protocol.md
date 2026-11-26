# 第 03 章 AMQP 0-9-1 协议深潜

> 本章定位：从字节层理解 RabbitMQ 的“母语”。看完你应该能解释一次 publish 为什么由三类帧组成、`basic.ack` 属于哪个信道、字段表如何编码，以及 Mini-RabbitMQ 的协议实现为什么这样分层。

## 协议栈与版本

AMQP 0-9-1 是 RabbitMQ 的核心协议，运行在 TCP 之上。TLS 是可选的传输层（AMQPS，默认 5671）。它不基于 HTTP，也不是 JSON 协议；每一个方法都有二进制布局。

一个容易混淆的点：**AMQP 0-9-1 与 AMQP 1.0 不是版本关系，而是两个不同的协议**。0-9-1 使用“类 + 方法 + 帧”的模型，1.0 使用 performative、link、credit 和丰富的类型系统。RabbitMQ 3.x 通过插件支持 AMQP 1.0；4.0 起 AMQP 1.0 是默认启用的核心协议，旧插件变成 no-op。两者不能假设线格式兼容。教程的实现只覆盖 0-9-1 子集。

AMQP 0-9-1 的一帧由三部分组成：

```text
+--------+---------+----------------+----------------+-----------+
| type   | channel | payload size   | payload        | frame-end |
| 1 byte | 2 bytes | 4 bytes (BE)   | N bytes        | 0xCE      |
+--------+---------+----------------+----------------+-----------+
```

总帧大小 = 8 字节开销 + payload。`frame-max` 限制的是总帧大小，而不是仅 payload。默认值常见为 131072 字节，可通过服务端配置协商。

帧类型只有四种核心值：

| 值 | 名称 | 用途 |
|---|---|---|
| 1 | METHOD | 连接、信道、交换机、队列、Basic 等方法 |
| 2 | HEADER | 内容头：类 ID、body 大小、Basic 属性 |
| 3 | BODY | 消息正文分片 |
| 8 | HEARTBEAT | 心跳，channel 必须为 0 |

## 握手：从 TCP 到 vhost

客户端连接后第一件事不是发方法，而是发送 8 字节协议头：

```text
41 4D 51 50 00 00 09 01
A  M  Q  P  0  0  9  1
```

服务端校验后，发送 `connection.start`，其中包含：

- 协议版本（0, 9）；
- server properties 字段表，例如 `product`、`version`、capabilities；
- 支持的认证机制，通常是 `PLAIN`；
- locale 列表。

客户端回复 `connection.start-ok`，携带客户端属性、机制名、SASL 响应和 locale。`PLAIN` 的响应是：

```text
\0username\0password
```

注意第一个字节是空授权身份（authzid），不是用户名的开始。很多手写客户端在这里把用户名和密码拼错，导致服务端解析出空用户。

服务端随后发 `connection.tune`，提出 `channel-max`、`frame-max`、`heartbeat`。客户端用 `connection.tune-ok` 回应协商值，再发 `connection.open` 指定 vhost。服务端检查 vhost 和权限后发 `connection.open-ok`，连接进入可用状态。

这三个参数的含义不同：

- `channel-max`：单连接允许的最大 channel 数，常见默认 2047；0 表示不限制（由实现决定）。客户端不能协商出比服务端更大的值；超过上限再 `channel.open` 会触发连接级错误。
- `frame-max`：单帧总大小（含 8 字节帧开销）；0 表示不限制，实际由双方协商。
- `heartbeat`：心跳间隔秒数；0 表示禁用。

超过 `channel-max` 不是“自动扩容”，而是协议错误。大量短连接/高并发客户端要把 channel 上限和文件描述符一起规划；更常见的问题是 channel 泄漏，而不是上限本身。

```text
client                                server
  | ---- protocol header ------------> |
  | <--- connection.start ------------ |
  | ---- connection.start-ok --------> |
  | <--- connection.tune ------------- |
  | ---- connection.tune-ok ----------> |
  | ---- connection.open -------------> |
  | <--- connection.open-ok ----------- |
```

Mini-RabbitMQ 在 `mini_rabbitmq/server.py` 中实现了这条握手，在 `mini_rabbitmq/client.py` 中实现了对应的客户端。原始字节握手由 `tests/test_wire_server.py` 的 `test_raw_handshake_connection_start` 独立校验，避免“服务端和客户端共享同一个 bug 所以测试通过”。

## 方法编号与类

AMQP 方法用 `(class-id, method-id)` 唯一标识。常用类：

| class | 名称 | 代表方法 |
|---|---|---|
| 10 | connection | start、tune、open、close、blocked |
| 20 | channel | open、flow、close |
| 40 | exchange | declare、delete、bind、unbind |
| 50 | queue | declare、bind、purge、delete、unbind |
| 60 | basic | qos、consume、publish、deliver、get、ack、nack、reject |
| 85 | confirm | select（publisher confirms） |
| 90 | tx | select、commit、rollback |

方法 payload 的前 4 字节固定是 `class-id`（2 字节）和 `method-id`（2 字节），之后是字段。字段按规范顺序出现，位字段会被打包进一个或多个字节，且 **LSB first**。

例如 `queue.declare` 的字段顺序是：

1. reserved short（占位，必须为 0）；
2. queue shortstr；
3. passive、durable、exclusive、auto-delete、no-wait 五个 bit；
4. arguments 字段表。

如果只看文档里的签名，很容易把 bit 字段当成五个独立字节。实际编码时它们共用一个字节的低 5 位。Mini 的 `Writer.bits` 和 `Reader.bits` 就是为这个细节准备的。

## 字段表（field table）

AMQP 的 headers 和 arguments 使用字段表。每个表先是 4 字节总长度，然后重复“短字符串键 + 1 字节类型 + 值”。常见类型：

| 类型码 | 含义 | 线格式 |
|---|---|---|
| `t` | boolean | 1 字节 |
| `b` / `B` | 有符号/无符号 8 位 | 1 字节 |
| `U` / `u` | 有符号/无符号 16 位 | 2 字节 |
| `I` / `i` | 有符号/无符号 32 位 | 4 字节 |
| `L` / `l` | 有符号/无符号 64 位 | 8 字节 |
| `f` / `d` | float / double | 4/8 字节 |
| `D` | decimal | scale + int32 |
| `S` | long string | 4 字节长度 + UTF-8 |
| `x` | byte array | 4 字节长度 + 原始字节 |
| `A` | array | 4 字节长度 + 带类型元素 |
| `T` | timestamp | 8 字节无符号 |
| `F` | nested table | 递归字段表 |
| `V` | void | 无值 |

字段表有两个陷阱：

- 表键是 shortstr（最长 255 字节），不是 longstr；
- `s` 是 short string（1 字节长度，最长 255），`S` 是 long string（4 字节长度）；RabbitMQ 客户端主要用 `S`，手写编码器不要把两者混用。

Mini-RabbitMQ 的 `protocol.py` 支持上述主要类型，并用 `tests/test_protocol.py` 验证布尔、整数、浮点、Decimal、字符串、字节数组、嵌套表、数组、时间戳和空值。

## 内容帧：一条消息如何拆开

`basic.publish` 只包含路由元数据，不包含正文。真正的消息由三类帧组成：

```text
METHOD  basic.publish(exchange, routing_key, mandatory, immediate)
HEADER  class=60, weight=0, body-size=N, property-flags, properties
BODY    chunk 1
BODY    chunk 2
...
```

注意：`immediate` 标志自 RabbitMQ 3.0 起不再支持，传 `true` 会得到 `NOT_IMPLEMENTED`；Mini 的服务端也会拒绝。实际可用的路由控制是 `mandatory`、alternate exchange 和 DLX。

HEADER 的布局是：

```text
class-id (2) | weight (2) | body-size (8) | property-flags (2) | properties...
```

Basic 属性标志位从高位开始：

| bit | 属性 |
|---|---|
| 15 | content-type |
| 14 | content-encoding |
| 13 | headers |
| 12 | delivery-mode |
| 11 | priority |
| 10 | correlation-id |
| 9 | reply-to |
| 8 | expiration |
| 7 | message-id |
| 6 | timestamp |
| 5 | type |
| 4 | user-id |
| 3 | app-id |
| 2 | cluster-id |

属性只按标志位出现，未设置的属性不占字节。`delivery_mode=2` 表示持久化消息，但它只在 durable 队列、持久化存储/Quorum 复制路径上才有完整意义；单独的 delivery-mode 不会让非持久队列“变持久”。

消息正文可以拆成多个 BODY 帧，顺序不能交错到同一信道的其他内容中。不同信道之间可以交错，这是 AMQP 多路复用的基础。

Mini 的 `encode_content_header` / `decode_content_header` 实现了属性编解码，`server.py` 在 `_handle_content_header` 和 `_handle_content_body` 中拼装完整消息。`tests/test_protocol.py` 对全部属性和空 body 做了往返测试。

## 发布、返回与确认

发布路径上有三个容易混淆的机制：

1. **mandatory**：消息不可路由时，broker 用 `basic.return` 把消息退回给发布者。它是“路由失败通知”，不是持久化确认。
2. **publisher confirm**：broker 用 `basic.ack` / `basic.nack` 告诉发布者“这条消息已被接受（并按配置持久化/复制）”。它是发布可靠性的核心。
3. **transaction**：`tx.select/commit/rollback` 把发布和确认操作打包。它提供原子性语义，但吞吐通常远低于 confirms。

一个常见错误是“只开 mandatory，不开 confirm”。此时消息成功路由后，如果队列所在节点崩溃且消息未持久化，发布者不会收到任何错误。另一个错误是“开了 confirm，却把 `wait_for_confirms` 放在每一条消息之后”，吞吐会退化成同步 RPC。正确做法是批量发布后统一等待，或使用异步 confirm 回调。

Mini 的 `ChannelState` 维护 `next_publish_seq`，在 publish 时分配序号，在消息被接受后发送 `basic.ack`，队列满导致拒绝时发送 `basic.nack`，不可路由且 mandatory 时先发 `basic.return` 再发 confirm。

## 错误模型：信道异常与连接异常

AMQP 把错误分成两类：

- **channel exception**：关闭当前 channel，连接和其他 channel 仍可用。典型：队列不存在（404）、无权限（403）、precondition failed（406）、资源锁定（405）。
- **connection exception**：关闭整条连接。典型：帧格式错误（501）、协议语法错误（502）、unexpected frame（505）、connection forced（320）。

这个区分对客户端设计至关重要。一个消费者把队列名拼错，不应该导致整个进程所有消费者断开；但收到非法帧说明协议状态已经不可信，必须断开连接。

Mini 的 `RabbitMQError` 携带 reply-code，网络层根据异常类型决定关闭 channel 还是 connection。错误码表见附录 A。

## 心跳与连接存活

心跳是 AMQP 的空 frame，用来在没有业务流量时检测半开连接。它不会替代 TCP keepalive，也不保证应用层处理能力。常见配置是 30～60 秒；过小会增加无谓流量，过大则故障发现变慢。

协商时的具体规则依客户端和 broker 版本略有差异。RabbitMQ 常见行为是双方建议值中取较小值，0 表示禁用。Mini 采用客户端 `tune-ok` 的心跳值，并提供简单的双向心跳线程；这只覆盖教学场景，不宣称覆盖所有边界条件。

心跳丢失后，客户端应认为连接已失效，重建连接、channel、消费者和未确认消息。这也是“消费者必须幂等”的原因之一：断开时 broker 会重新投递未确认消息。

## Mini-RabbitMQ 的协议层结构

| 文件 | 职责 |
|---|---|
| `mini_rabbitmq/protocol.py` | 帧、字段表、内容头、方法编解码 |
| `mini_rabbitmq/server.py` | 握手、方法分发、信道状态、投递 |
| `mini_rabbitmq/client.py` | 客户端握手、RPC 等待、异步投递、confirms |
| `tests/test_protocol.py` | 纯编解码测试，不依赖网络 |
| `tests/test_wire_server.py` | 原始握手、发布/消费、事务、ACL 的端到端测试 |

一个重要的工程原则是：**编解码层不依赖业务层**。`protocol.py` 不知道队列是什么，只负责字节；`server.py` 不知道消息如何持久化，只调用 broker 门面。这样可以用单元测试覆盖位布局，用集成测试覆盖状态机，而不是把所有问题混在 socket 循环里。

## AMQP 1.0、MQTT 与其他协议

RabbitMQ 支持多种协议，但每种协议的能力不同：

- **AMQP 1.0**：更现代的类型系统、link flow、settlement；4.0 起是核心协议，与 0-9-1 不是同一模型。
- **MQTT**：发布/订阅、QoS 0/1（RabbitMQ 插件不支持 QoS 2）、主题层级；通过 MQTT 插件接入，适合 IoT。
- **STOMP**：文本帧、简单的 SEND/SUBSCRIBE；适合轻量集成。
- **Web STOMP / Web MQTT**：通过 WebSocket 暴露。
- **Stream 协议**：面向 Streams 的二进制协议，端口常见 5552。
- **HTTP Management API**：运维控制面，不是消息数据面。

不要因为“RabbitMQ 支持多协议”就混用。一个系统内的消息语义、确认方式和错误处理应尽量统一；跨协议桥接要考虑消息属性、确认语义和路由模型的差异。

## frame_max、max_message_size 与消息大小

很多“消息太大发不出去”的问题，其实是三个不同限制混在一起：

| 限制 | 控制什么 | 默认/常见值 | 超限表现 |
|---|---|---|---|
| `frame_max` | 单帧总大小（含 8 字节帧开销） | 常见 131072 字节 | 帧错误/连接关闭 |
| `max_message_size` | 整条消息 body 上限 | 4.0 起默认 16 MiB（3.13 为 128 MiB） | 发布被拒 |
| 内存/磁盘水位 | broker 资源保护 | 依配置 | blocked/分页 |

`frame_max` 只决定一条消息要拆成多少个 BODY 帧；拆帧是正常的，不改变消息语义。`max_message_size` 才是“这条消息能不能发”的硬上限。4.0 把默认值从 128 MiB 降到 16 MiB，很多直接塞图片/PDF 的系统会在升级后突然失败。

```ini
# rabbitmq.conf
frame_max = 131072
max_message_size = 16777216
```

实践建议：

- 不要把“能发 16 MiB”当成“应该发 16 MiB”；大消息会放大内存、磁盘、复制和网络成本；
- 图片/视频/大文档放对象存储，消息只传 URI、校验和、版本；
- 如果必须发大消息，先确认 `max_message_size`、客户端分帧、消费者内存和 Quorum 复制成本；
- 监控发布失败原因，不要把“消息被拒”误判成网络问题。

## 属性 flags 的 continuation 位

Basic 属性用 16 位 flags 表示哪些属性存在。第 0 位（最低位）是 **continuation bit**：如果还有后续 flags 字，就置位并再读 2 字节。当前 Basic 属性只用了 14 个位，通常不需要 continuation，但解码器必须正确处理，否则会把后续属性字当成属性值，导致错位。

Mini 的 `decode_content_header` 在读取 flags 后检查 `flags & 1`，必要时继续读；`tests/test_protocol.py` 覆盖属性往返和空 body。手写客户端时最常见的 bug 就是“只读一个 flags 字”或“把 continuation 位当成某个属性”。

## 用 Wireshark 看一次 publish

Wireshark 自带 AMQP 0-9-1 解析器（端口 5672 会被自动识别）。抓一次最小发布：

```text
过滤器：tcp.port == 5672
观察顺序：
  AMQPProtocolHeader (AMQP\x00\x00\x09\x01)
  connection.start / start-ok / tune / tune-ok / open / open-ok
  channel.open / open-ok
  exchange.declare / declare-ok
  queue.declare / declare-ok
  queue.bind / bind-ok
  basic.publish
  content-header (body size, properties)
  content-body
  basic.ack（如果开启 confirm）
```

抓包的价值：

- 确认 `basic.publish` 之后确实跟着 header/body；
- 看 confirm 的 delivery tag 和 multiple 标志；
- 看 `basic.return` 是否真的到达客户端；
- 排查“客户端说发了、broker 说没收到”的争议。

注意 TLS（5671）下抓包看到的是密文；需要在客户端/服务端导出密钥或使用测试证书，生产不要为了抓包关闭 TLS。

## AMQP 1.0 的线格式差异

AMQP 1.0 不是“0-9-1 加版本号”。它的核心概念是：

- **connection**：TCP/TLS 上的会话容器；
- **session**：双向通信上下文；
- **link**：单向消息流（sender/receiver）；
- **transfer**：消息传输 performative；
- **credit**：接收方授予发送方的额度；
- **settlement**：确认/结算状态。

与 0-9-1 的映射难点：

| 0-9-1 | 1.0 | 注意 |
|---|---|---|
| exchange/queue | target/source address | 地址语义由 broker 定义 |
| channel | session | 多路复用模型不同 |
| basic.ack | disposition/settlement | 确认粒度/状态不同 |
| prefetch | link credit | 流控模型不同 |
| headers | application-properties/message-annotations | 属性映射不一一对应 |

如果团队要从 0-9-1 迁到 1.0，不要假设“换端口/换库”就行；确认、路由、死信、属性映射都要重新验证。第 14 章和[附录 J](../appendices/J-polyglot-client-cookbook.md)会继续展开，线格式、类型系统、performative、credit 与 settlement 的逐字段说明见[附录 R](../appendices/R-amqp-1-0-deep-dive.md)。

## 本章小结

AMQP 0-9-1 的核心是帧、方法、字段表和内容帧。握手完成协议版本、认证、参数协商和 vhost 选择；channel 承载大部分操作；消息由 METHOD + HEADER + BODY 组成；发布可靠性依赖 mandatory、confirm 和持久化语义。错误分为 channel 级和 connection 级，二者对应用的影响完全不同。

Mini-RabbitMQ 用 `protocol.py` 把字节布局独立出来，并用原始帧测试和端到端测试双重校验。下一章讨论连接、信道和 vhost 的工程使用方式：如何避免连接泄漏、channel 争用、权限越界和关闭风暴。

---

[上一章：第 02 章 安装、拓扑与第一条消息](02-install-and-first-message.md) | [返回目录](../README.md) | [下一章：第 04 章 连接、信道与虚拟主机](04-connections-channels-vhosts.md)
