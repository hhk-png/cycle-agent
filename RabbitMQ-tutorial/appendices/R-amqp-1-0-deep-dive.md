# 附录 R AMQP 1.0 协议深度手册

AMQP 1.0 在 RabbitMQ 4.0 起是核心协议，但很多团队对它的理解仍停留在“另一个端口”或“换了个客户端库”。这会掩盖一个事实：**AMQP 1.0 与 AMQP 0-9-1 是两套不同的模型**。0-9-1 的核心是 exchange/queue/channel；1.0 的核心是 connection/session/link、credit 和 settlement。确认、流控、路由、属性映射都不是一一对应。

本附录补上第 03 章和第 14 章省略的线级细节：帧、类型系统、performative、SASL、link credit、transfer/settlement、消息 section、source/target 与过滤器，以及它们和 RabbitMQ 0-9-1 语义的映射边界。字段名以 AMQP 1.0 规范为准；不同 broker 对可选字段和扩展的支持不同，落地前以目标版本文档和实际抓包为准。

## 为什么单独用一篇讲 1.0

三个现实原因：

1. **它已经是核心协议**。4.0 起 RabbitMQ 默认启用 AMQP 1.0，旧插件变成兼容壳；新系统不必再把它当实验能力。
2. **它解决的是跨厂商互通**。Azure Service Bus、ActiveMQ Artemis、Qpid、Solace 等都能说 1.0；但“都能说”不等于“语义相同”。
3. **它的失败模式不同**。0-9-1 的 confirm/ack 直觉，直接套到 1.0 的 settlement 上，很容易写出“以为至少一次、实际最多一次”的消费者。

学 1.0 的目标不是背字段，而是能回答：

- 这条消息什么时候算“被 broker 接受”？
- credit 和 ack 是什么关系？
- `accepted`、`rejected`、`released`、`modified` 分别对应什么业务动作？
- 为什么消费者不发 disposition，发送方会一直重发或停住？
- 0-9-1 的 `basic.return`、DLX、TTL 在 1.0 里怎么表达？

## 分层与帧

AMQP 1.0 的分层是：

```text
connection
  └── session
        ├── sender link   ── transfer ──>  target
        └── receiver link <── transfer ──  source
```

- **connection**：TCP/TLS 上的会话容器，负责 `open`/`close`、帧大小、channel 数、心跳（idle-time-out）；
- **session**：双向通信上下文，负责窗口流控、`begin`/`end`、下一个 outgoing id；
- **link**：单向消息流，有 source/target、credit、settlement 模式、投递状态；
- **transfer**：承载消息的 performative，可以分片（`more`）；
- **disposition**：接收方对 transfer 的结算/结果。

帧格式：

```text
0                   1                   2                   3
+-------------------+-------------------+-------------------+
|                     size (4 bytes, BE)                    |
+---------+---------+---------------------------------------+
|  DOFF   |  type   |            channel (2 bytes)          |
+---------+---------+---------------------------------------+
|                     body (performative + payload)        |
+-----------------------------------------------------------+
```

- `size`：整个帧的字节数，包含 8 字节帧头和 body，**不包含** size 字段自身；
- `DOFF`：data offset，以 4 字节为单位，从帧起始到 body 的偏移；最小值为 2（8 字节）；
- `type`：`0` 表示 AMQP 帧，`1` 表示 SASL 帧；
- `channel`：会话通道；连接级 performative 和 SASL 使用 channel 0；
- body：performative（described type）+ 可选 payload（transfer 的消息 section）。

`open` 里协商 `max-frame-size` 和 `channel-max`；`begin` 里建立 session 的窗口；`attach` 里建立 link 并声明 source/target、settle 模式、初始 delivery-count。一次典型的发送链路：

```text
client                                  broker
  | ---------- open -------------------> |
  | <--------- open -------------------- |
  | ---------- begin ------------------> |
  | <--------- begin ------------------- |
  | ---------- attach (sender) --------> |
  | <--------- attach (receiver) ------- |
  | <--------- flow (credit) ----------- |
  | ---------- transfer ----------------> |
  | <--------- disposition ------------- |
  | ---------- detach -----------------> |
  | ---------- end --------------------> |
  | ---------- close ------------------> |
```

注意方向：`attach` 的 sender/receiver 是**链路端点角色**，不是“客户端/服务端”。客户端可以创建 sender link 来发送，也可以创建 receiver link 来接收；broker 对两个方向都可能是 sender 或 receiver。

## 类型系统

AMQP 1.0 自带一套紧凑的二进制类型系统。每个值以 1 字节 format code 开头，后跟定长或变长数据。常用类型：

| 类型 | format code | 说明 |
|---|---|---|
| null | `0x40` | 空值 |
| boolean | `0x41` / `0x42` / `0x56` | true / false / 1 字节布尔 |
| ubyte / byte | `0x50` / `0x51` | 无符号/有符号 8 位 |
| ushort / short | `0x60` / `0x61` | 16 位 |
| uint / int | `0x70` / `0x71` | 32 位 |
| ulong / long | `0x80` / `0x81` | 64 位 |
| float / double | `0x72` / `0x82` | 32/64 位浮点 |
| decimal32/64/128 | `0x74` / `0x84` / `0x94` | 十进制 |
| char | `0x73` | Unicode 字符 |
| timestamp | `0x83` | 毫秒时间戳 |
| uuid | `0x98` | 16 字节 UUID |
| binary | `0xa0` / `0xb0` | vbin8 / vbin32 |
| string | `0xa1` / `0xb1` | str8 / str32，UTF-8 |
| symbol | `0xa3` / `0xb3` | sym8 / sym32，ASCII |
| list | `0x45` / `0xc0` / `0xd0` | list0 / list8 / list32 |
| map | `0xc1` / `0xd1` | map8 / map32 |
| array | `0xe0` / `0xf0` | array8 / array32 |
| described | `0x00` | 描述类型：descriptor + value |

两个关键概念：

- **constructor**：format code 本身决定后续字节宽度。例如 `0xa1` 后跟 1 字节长度，再跟 UTF-8 字节；`0xb1` 后跟 4 字节长度。
- **described type**：用 `0x00` 开头，后跟 descriptor 和 value，用来表达“这个值是什么语义类型”。AMQP 1.0 的 performative 和消息 section 都是 described type。

例如 `properties` section 的编码开头是：

```text
00 53 73 ...
```

`0x00` 表示 described type，`0x53` 是 smallulong（1 字节 ulong），`0x73` 是 `amqp:properties` 的 descriptor。后面才是 properties 的字段列表。很多手写解析器在这里出错：把 `0x53` 当成普通整数，或者忘记 descriptor 本身也可能是 symbol/ulong 的不同编码。

数组（array）有额外规则：array 的元素共享同一个 constructor，后续元素只写值，不重复写 format code。解析 array 时不能假设每个元素都以 format code 开头。

## Performative

AMQP 1.0 用 performative 表达协议动作。常用 descriptor：

| descriptor | performative | 作用域 | 方向 |
|---|---|---|---|
| `0x10` | `open` | connection | 双方 |
| `0x11` | `begin` | session | 双方 |
| `0x12` | `attach` | link | 双方 |
| `0x13` | `flow` | session/link | 双方 |
| `0x14` | `transfer` | link | sender -> receiver |
| `0x15` | `disposition` | link | receiver -> sender |
| `0x16` | `detach` | link | 双方 |
| `0x17` | `end` | session | 双方 |
| `0x18` | `close` | connection | 双方 |

SASL 层有独立的 performative：

| descriptor | performative | 作用 |
|---|---|---|
| `0x40` | `sasl-mechanisms` | broker 公布支持的 SASL 机制 |
| `0x41` | `sasl-init` | 客户端选择机制并发送初始响应 |
| `0x42` | `sasl-challenge` | broker 发起挑战 |
| `0x43` | `sasl-response` | 客户端回应挑战 |
| `0x44` | `sasl-outcome` | 认证结果 |

常用字段（只列排障最常看的）：

- `open`：`container-id`、`hostname`、`max-frame-size`、`channel-max`、`idle-time-out`；
- `begin`：`remote-channel`、`next-outgoing-id`、`incoming-window`、`outgoing-window`、`handle-max`；
- `attach`：`name`、`handle`、`role`、`snd-settle-mode`、`rcv-settle-mode`、`source`、`target`、`initial-delivery-count`、`max-message-size`；
- `flow`：`next-incoming-id`、`incoming-window`、`next-outgoing-id`、`outgoing-window`、`handle`、`delivery-count`、`link-credit`、`available`、`drain`、`echo`；
- `transfer`：`handle`、`delivery-id`、`delivery-tag`、`message-format`、`settled`、`more`、`rcv-settle-mode`、`state`、`aborted`、`batchable`；
- `disposition`：`role`、`first`、`last`、`settled`、`state`、`batchable`；
- `detach`：`handle`、`closed`、`error`；
- `end` / `close`：`error`。

错误作用域：

- **connection error**：`close` + `error`，整条连接关闭；
- **session error**：`end` + `error`，该 session 关闭；
- **link error**：`detach` + `error`，该 link 关闭。

常见 error condition 包括 `amqp:internal-error`、`amqp:not-found`、`amqp:unauthorized-access`、`amqp:resource-limit-exceeded`、`amqp:link:detach-forced` 等。condition 是 symbol，description 是人类可读文本；客户端应记录两者，而不是只打印“link detached”。

## SASL 与安全层

AMQP 1.0 把认证放在 SASL 层，和 AMQP 帧分开：

```text
TCP/TLS
  -> SASL 握手（type=1, channel=0）
  -> AMQP open（type=0, channel=0）
```

流程：

```text
broker -> sasl-mechanisms(mechanisms)
client -> sasl-init(mechanism, initial-response, hostname)
broker -> sasl-challenge(challenge)     # 可选，可能多轮
client -> sasl-response(response)
broker -> sasl-outcome(code, additional-data)
```

常见机制：

- `PLAIN`：初始响应通常是 `\0username\0password`；
- `EXTERNAL`：依赖 TLS 客户端证书/外部身份，初始响应可能为空；
- `ANONYMOUS`：匿名，取决于 broker 是否启用。

注意：

- `open` 里没有用户名/密码字段；认证发生在 SASL 层；
- 机制是 broker 公布的，不是客户端想用就能用；
- TLS 只提供传输加密，不等于认证；mTLS 的证书映射仍需 broker 配置；
- 认证失败通常以 `sasl-outcome` 或连接关闭体现，不一定有业务层错误码。

排障时先确认：客户端和服务端是否都支持该机制、TLS 是否终止在正确位置、证书主题/用户名映射是否正确、是否有代理剥离了 SASL 帧。

## Link credit 与流控

AMQP 1.0 的流控分两层：

1. **session window**：控制某个 session 上未结算的 transfer 数量；
2. **link credit**：控制某个 link 上接收方还能接收多少条消息。

`flow` 的关键字段：

- `delivery-count`：发送方已经在该 link 上发送的 transfer 数；
- `link-credit`：接收方授予的额度；
- `available`：发送方估计的可发送消息数；
- `drain`：接收方要求发送方把当前 credit 用完并回报；
- `echo`：要求对方立即回一个 `flow`。

发送方每发一个 transfer，`delivery-count` 增加，可用 credit 减少；credit 为 0 时必须停。接收方处理完消息后，可以发新的 `flow` 补充 credit。**credit 不是 ack**：它只表示“你可以再发几条”，不表示“上一条已经成功处理”。结算由 `disposition` 表达。

常见 bug：

- 客户端只发一次 `flow`，消费几条后 credit 耗尽，表现为“消费者还连着但不再收消息”；
- 接收方把 credit 补得过大，导致内存积压；
- 发送方忽略 `drain`，接收方等不到 drain 完成；
- 把 session window 和 link credit 混为一谈，调大一个以为解决了另一个；
- 多线程共享 link，delivery-count/credit 竞争。

一个健康的状态是：credit 随消费进度补充，`delivery-count` 单调增长，未结算 transfer 数量有上限，session window 不长期为 0。

## Transfer 与 settlement

`transfer` 是消息传输的核心。关键字段：

- `handle`：link 的本地句柄；
- `delivery-id`：link 内单调递增的投递 ID；
- `delivery-tag`：发送方定义的二进制标签，接收方在 disposition 里引用；
- `message-format`：消息格式，通常为 0；
- `settled`：发送方是否认为已结算；
- `more`：是否还有后续 transfer 片段；
- `rcv-settle-mode`：接收方的结算模式；
- `aborted`：中止本次多帧传输；
- `state`：发送方看到的投递状态。

结算模式：

- `snd-settle-mode=0`（unsettled）：发送方等待接收方 disposition；
- `snd-settle-mode=1`（settled）：发送方发送后即认为结算；
- `rcv-settle-mode=0`（first）：接收方收到 transfer 后立即结算；
- `rcv-settle-mode=1`（second）：接收方处理完再结算。

`disposition` 的 `state` 是 outcome：

| outcome | descriptor | 语义 | 常见业务映射 |
|---|---|---|---|
| `accepted` | `0x24` | 接收方接受 | ack/成功 |
| `rejected` | `0x25` | 拒绝，通常不可重试 | reject/死信 |
| `released` | `0x26` | 释放，未处理，可重投 | nack/requeue |
| `modified` | `0x27` | 修改后重投/延迟 | 重试、延迟、带注解重投 |

`modified` 可以带：

- `delivery-failed`：本次投递失败；
- `undeliverable-here`：当前消费者无法处理；
- `message-annotations`：附加/覆盖注解，可用于重试计数、延迟提示等。

这套模型比 0-9-1 的 ack/nack/reject 更细，但也更容易误用：

- 收到消息就发 `accepted`，业务还没处理完，等于 at-most-once；
- 处理失败一律 `rejected`，可能把可重试的临时故障变成死信；
- 不区分 `released` 和 `modified`，重试策略无法表达；
- 发送方在 unsettled 模式下不处理 disposition，重发/卡住；
- 接收方批量 disposition 的 `first`/`last` 范围写错，误结算其他消息。

**AMQP 1.0 本身不保证 exactly-once**。settlement 只表达“这条投递的结局”，不自动让业务副作用幂等。跨系统 exactly-once 仍需要事务/幂等/去重表/对账。

## 消息格式

AMQP 1.0 的消息由多个 section 组成，顺序固定：

```text
header
delivery-annotations
message-annotations
properties
application-properties
body (data / amqp-value / amqp-sequence)
footer
```

常用 section 的 descriptor：

| section | descriptor | 内容 |
|---|---|---|
| header | `0x70` | durable、priority、ttl、first-acquirer、delivery-count |
| delivery-annotations | `0x71` | 传输层注解（symbol -> any） |
| message-annotations | `0x72` | 消息注解（symbol -> any） |
| properties | `0x73` | message-id、subject、reply-to、correlation-id 等 |
| application-properties | `0x74` | 应用属性（string -> simple type） |
| data | `0x75` | 二进制 body |
| amqp-sequence | `0x76` | AMQP list body |
| amqp-value | `0x77` | 单个 AMQP 值 body |
| footer | `0x78` | 页脚注解 |

`properties` 的关键字段：

- `message-id`：ulong/uuid/binary/string；
- `user-id`：binary；
- `to`：string；
- `subject`：string；
- `reply-to`：string；
- `correlation-id`：message-id 类型；
- `content-type` / `content-encoding`：symbol；
- `absolute-expiry-time` / `creation-time`：timestamp；
- `group-id` / `group-sequence` / `reply-to-group-id`：分组/顺序。

`header` 的关键字段：

- `durable`：是否持久化；
- `priority`：0～9；
- `ttl`：毫秒；
- `first-acquirer`：是否首次获取；
- `delivery-count`：投递次数。

`delivery-annotations` 和 `message-annotations` 都是 map，但语义不同：前者偏传输层（broker/路由/投递），后者偏消息层（业务/应用）。不同 broker 对注解键的命名空间有自己的约定，不要假设跨厂商一致。

body 三选一：

- `data`：一段二进制，最常用；
- `amqp-value`：一个 AMQP 值（字符串、map、list 等）；
- `amqp-sequence`：一个 AMQP list。

一条消息可以有多个 body section 吗？规范允许 data section 出现多次，接收方按顺序拼接；但很多客户端只处理单个 data。跨厂商时，优先用单个 `data` + JSON/Protobuf，避免依赖多 section 行为。

## Source、target 与过滤器

`attach` 里的 `source` 和 `target` 描述链路两端的地址和能力。

`source` 常用字段：

- `address`：消息来源地址；
- `durable`：来源是否持久；
- `expiry-policy`：过期策略；
- `timeout`：超时；
- `dynamic`：是否请求动态地址；
- `distribution-mode`：分发模式；
- `filter`：过滤器集合；
- `default-outcome` / `outcomes`：支持的结果；
- `capabilities`：能力声明。

`target` 常用字段：

- `address`：目标地址；
- `durable`、`expiry-policy`、`timeout`；
- `dynamic`、`dynamic-node-properties`；
- `capabilities`。

过滤器（filter）是一个 map：键是 filter 类型 symbol，值是 filter 值。常见类型包括 SQL/JMS selector 一类，以及 broker 自定义的过滤器（例如 Stream 的过滤）。**过滤器不是跨厂商标准语义**：一个 broker 支持的 filter key，另一个 broker 未必支持；attach 时如果 filter 不被支持，可能直接 detach 或忽略。

地址语义由 broker 定义。AMQP 1.0 规范只规定 `address` 是字符串，不规定它必须映射到 queue 还是 exchange。RabbitMQ 通常把 target 映射到 exchange/queue、source 映射到 queue/stream，但具体地址形式和 routing key 传递方式属于实现约定。迁移时不要假设“0-9-1 的 exchange + routing key 可以逐字段替换成 1.0 的 address”。

## RabbitMQ 的 1.0 映射

RabbitMQ 4.0 起把 AMQP 1.0 作为核心协议。对使用者最重要的映射点：

| 0-9-1 概念 | 1.0 近似表达 | 注意 |
|---|---|---|
| connection | connection | 1.0 有 `open` 的 container-id、idle-time-out |
| channel | session | session 有窗口流控，不是 channel 的简单别名 |
| exchange + routing key | target address + 实现约定的路由信息 | 不是规范级一一对应 |
| queue | source/target address | 地址语义由 RabbitMQ 定义 |
| publisher confirm | settlement/disposition | 确认粒度/失败类型不同 |
| consumer ack | disposition `accepted` | 还有 released/modified 等更细状态 |
| nack/requeue | `released` / `modified` | 语义要按业务重试策略选 |
| prefetch | link credit | credit 是“可发送额度”，不是未 ack 上限 |
| mandatory return | 无完全等价物 | 用 outcome/地址校验/死信等组合 |
| DLX | 死信/注解/重投策略 | 映射取决于版本和队列类型 |
| tx.select/commit | 事务/结算模型不同 | 不要假设等价 |
| global QoS | 无直接等价物 | 用 credit/应用层限流 |

版本相关能力：

- 4.0：AMQP 1.0 默认启用，旧插件 no-op；
- 4.2：AMQP 1.0 Direct Reply-To，RPC 场景不必自建回复队列；
- 4.4：AMQP 1.0 连接唯一性（按 container ID 在集群范围去重），以及更完整的 JMS topic 语义（商业版能力，需按版本核对）。

迁移到 1.0 的检查清单：

- [ ] 目标版本默认启用 1.0，客户端库支持所需能力；
- [ ] 地址映射（exchange/queue/stream）在预发环境验证；
- [ ] routing key/属性映射有契约测试；
- [ ] publisher 的 settlement 模式与业务可靠性匹配；
- [ ] consumer 的 outcome 处理覆盖 accepted/rejected/released/modified；
- [ ] 重试/死信/延迟语义在 1.0 下重新验证；
- [ ] 监控能看到 1.0 连接、link、credit、settlement；
- [ ] 认证（SASL/TLS/OAuth）与 0-9-1 一致；
- [ ] 回滚路径明确（协议切换不是仅改端口）。

## 与 0-9-1 的逐项对照

| 维度 | AMQP 0-9-1 | AMQP 1.0 | 迁移风险 |
|---|---|---|---|
| 多路复用 | channel | session | channel 错误/关闭语义不同 |
| 路由 | exchange + routing key | source/target address + filter | 地址语义由 broker 定义 |
| 流控 | prefetch（未 ack 上限） | link credit + session window | credit 不是 ack |
| 发布确认 | publisher confirm | settlement/disposition | 失败类型更细 |
| 消费确认 | ack/nack/reject | accepted/rejected/released/modified | 重试语义要重新设计 |
| 持久化 | delivery-mode + durable queue | header.durable + broker 语义 | 不能只看 header |
| 事务 | tx class | 事务/结算模型不同 | 不要假设等价 |
| 错误 | channel/connection exception | link/session/connection error | 作用域和错误码不同 |
| 消息属性 | Basic properties + headers | properties + annotations + application-properties | 字段映射不一一对应 |
| 消息体 | body 字节 | data/amqp-value/amqp-sequence | 多 section 行为要验证 |

一个常见的迁移错误是：把 0-9-1 的“手动 ack + prefetch”直接翻译成“收到 transfer 就 accepted + 大 credit”。前者在业务处理完后 ack，后者在收到时就结算，可靠性从至少一次退化成最多一次。正确做法是：处理成功后发 `accepted`，可重试失败发 `released`/`modified`，不可重试失败发 `rejected`，并让 credit 与处理能力匹配。

## 抓包与调试

AMQP 1.0 的排障顺序：

1. **TCP/TLS 是否建立**：端口、证书、SNI、代理；
2. **SASL 是否成功**：机制、初始响应、outcome；
3. **open 是否成功**：container-id、max-frame-size、channel-max、idle-time-out；
4. **begin/attach 是否成功**：session window、source/target、filter、capabilities；
5. **credit 是否授予**：flow 的 link-credit、delivery-count、available；
6. **transfer 是否发出/收到**：handle、delivery-id、more、settled；
7. **disposition 是否返回**：first/last、state、settled；
8. **detach/end/close 的 error condition**：谁先关闭、为什么。

Wireshark 的 AMQP 1.0 解析器可以显示 performative 和字段；如果版本不支持，退化为看 TCP 流和 broker 日志。抓包时注意 TLS 会加密；不要为了抓包在生产关闭 TLS。

常见症状：

| 症状 | 可能原因 | 排查 |
|---|---|---|
| 连上但不发消息 | credit=0、attach 失败、filter 不匹配 | 看 flow/attach 的 error |
| 发了几条就停 | credit 耗尽、session window=0 | 看 delivery-count/window |
| 重复消息 | unsettled 重发、消费端未幂等 | 看 settle mode/disposition |
| 消息“丢了” | 收到即 accepted、发送方 settled | 看业务处理与 settlement 时序 |
| link 被 detach | 地址不存在、无权限、资源限制 | 看 detach.error.condition |
| 认证失败 | SASL 机制/TLS/凭据 | 看 sasl-outcome 和 broker 日志 |
| 消息太大 | max-message-size/frame size | 看 open/attach 协商值 |
| 顺序错乱 | 多 link/多 session/并发消费 | 用单 link/单消费者/分区 |

## Mini 的边界与练习

Mini-RabbitMQ 只实现 AMQP 0-9-1 的子集，不支持 AMQP 1.0。这不是遗漏，而是范围选择：1.0 的类型系统、performative、credit/settlement 足够再写一个教学 broker。

如果要在 Mini 上练习 1.0，建议按以下顺序：

1. 实现帧头和 `open`/`close` 的编解码；
2. 实现类型系统的常用 constructor（null/boolean/uint/ulong/string/symbol/list/map/described）；
3. 实现 `begin`/`end` 和 session window；
4. 实现 `attach`/`detach` 与 source/target；
5. 实现 `flow` 的 credit 和 `transfer` 的 delivery-id；
6. 实现 `disposition` 的 accepted/released；
7. 用一个内存队列模拟 source/target，验证 credit 耗尽和补充；
8. 用原始帧测试覆盖“settle before process”“credit leak”“aborted transfer”等边界。

做完这些，你会更清楚为什么 1.0 的客户端库比 0-9-1 复杂：它不是“多几个字段”，而是多了一套流控和结算状态机。

## 小结

AMQP 1.0 的核心是 connection/session/link、类型系统、performative、credit 和 settlement。帧由 size/DOFF/type/channel 组成；消息由 header/annotations/properties/body 等 section 组成；发送受 link credit 和 session window 双重约束；接收方用 disposition 表达 accepted/rejected/released/modified。它与 0-9-1 不是版本关系，路由、确认、属性映射都不能假设一一对应。

RabbitMQ 4.0 起 1.0 是核心协议，4.2 增加 Direct Reply-To，4.4 增加连接唯一性等能力。迁移时先验证地址映射、settlement 语义、重试/死信和认证，再用抓包和契约测试兜底。把 1.0 当成“另一个客户端库”会低估迁移成本；把它当成一套新的消息状态机，才是正确的起点。互操作实验见[附录 W](W-4x-feature-labs.md)，版本差异核查见[附录 X](X-fact-check-and-sources.md)。

---

[返回目录](../README.md) | [上一章：附录 Q RabbitMQ 内部架构与源码阅读地图](Q-broker-internals-source-map.md) | [下一章：附录 S 消息架构模式与反模式目录](S-messaging-patterns-antipatterns.md)
