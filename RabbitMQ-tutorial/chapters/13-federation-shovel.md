# 第 13 章 Federation、Shovel 与多活拓扑

> 本章定位：当消息需要跨 broker、跨机房、跨云时，单集群不够用。Federation 与 Shovel 是 RabbitMQ 的两个“跨 broker 连接器”，但它们解决的问题、可靠性和拓扑代价不同。本章给出配置骨架、失败模式和多活冲突处理。

## 为什么不直接组一个大集群

RabbitMQ 集群适合低延迟、同一网络内的节点。跨地域组集群会遇到：

- Erlang distribution 对延迟敏感；
- Raft/Quorum 写入需要多数派往返；
- 网络分区概率高；
- 节点名/DNS/时钟/防火墙复杂；
- 故障域变大，一个区域故障可能拖垮全局。

因此跨地域通常采用“多个独立集群 + 连接器”的联邦式架构。每个区域本地读写，异步复制/转发到其他区域。代价是最终一致、重复消息、冲突处理和运维复杂度。

先做一个选择：

```text
同一机房、要求低延迟和强一致复制？
├── 是 → 单集群 + Quorum Queue
└── 否
    ├── 目标只是搬运/迁移/汇聚队列？
    │   └── 是 → Shovel
    ├── 需要按交换机/队列做跨集群订阅？
    │   └── 是 → Federation
    ├── 目标是 Kafka/SQS/CDC 等非 RabbitMQ 系统？
    │   └── 是 → 应用层桥接（附录 Z），不要硬套 Shovel/Federation
    └── 需要业务级冲突解决？
        └── 是 → 分区/双写/对账 + 消息连接器只做传输
```

## Shovel

Shovel 是“从源队列拉取消息，转发到目标交换机/队列”的插件。它像一个内建的消费者 + 发布者：

```text
source queue -> Shovel -> destination exchange/queue
```

特点：

- 源可以是 queue，目标可以是 exchange 或 queue；
- 支持 AMQP URI、TLS、重连；
- 成功转发后再 ack 源消息；
- 失败可以 requeue；
- 支持静态配置和动态参数；
- 适合迁移、跨集群搬运、灾备复制。

Shovel 的可靠性是“至少一次”：如果转发成功但 ack 源消息前崩溃，消息会被再次转发。目标端必须幂等，或使用去重表。

### 静态配置

静态 Shovel 放在 `advanced.config`（Erlang terms），适合随节点启动：

```erlang
[
  {rabbitmq_shovel, [
    {shovels, [
      {orders_to_eu, [
        {source, [
          {protocol, amqp091},
          {uris, ["amqp://shovel:***@rabbit-a/%2f"]},
          {queue, "orders"}
        ]},
        {destination, [
          {protocol, amqp091},
          {uris, ["amqps://shovel:***@rabbit-b/%2f"]},
          {exchange, "orders"},
          {routing_key, "order.created"}
        ]},
        {ack_mode, on_confirm},
        {prefetch_count, 100},
        {reconnect_delay, 5}
      ]}
    ]}
  ]}
].
```

### 动态配置

动态 Shovel 通过管理 API 的 runtime parameter 创建，可在运行时增删：

```bash
curl -u admin:secret -X PUT \
  -H 'content-type: application/json' \
  http://rabbit-a:15672/api/parameters/shovel/%2F/orders_to_eu \
  -d '{
    "value": {
      "src-protocol": "amqp091",
      "src-uri": "amqp://rabbit-a/%2f",
      "src-queue": "orders",
      "dest-protocol": "amqp091",
      "dest-uri": "amqps://rabbit-b/%2f",
      "dest-exchange": "orders",
      "dest-exchange-key": "order.created",
      "ack-mode": "on-confirm",
      "src-prefetch-count": 100,
      "src-delete-after": "never",
      "reconnect-delay": 5
    }
  }'
```

关键参数：

| 参数 | 含义 | 建议 |
|---|---|---|
| `ack-mode` | `on-confirm` / `on-publish` / `no-ack` | 生产用 `on-confirm` |
| `src-prefetch-count` | 源端在途消息数 | 按网络 RTT 和目标吞吐调（部分文档/旧配置写作 `prefetch-count`） |
| `reconnect-delay` | 断线重连间隔 | 5–30 秒，避免风暴 |
| `src-delete-after` | 转发后删除源消息 | `never` / `queue-length` / 整数；迁移场景先用 `never` 观察 |
| `src-queue` / `dest-queue` | 源/目标队列 | 目标 exchange 更灵活 |

`ack-mode` 很重要：

- `on-confirm`：目标 confirm 后再 ack 源，最安全；
- `on-publish`：发布到目标 socket 后就 ack，可能丢；
- `no-ack`：源消息立即 ack，风险最高。

### 典型场景

- 把旧集群队列迁移到新集群；
- 把边缘站点消息汇聚到中心；
- 把关键事件复制到灾备集群；
- 在不改变生产拓扑的情况下做数据导出。

### 失败模式与监控

| 症状 | 可能原因 | 检查 |
|---|---|---|
| 源积压持续增长 | Shovel 吞吐不足/目标慢 | 转发速率、网络 RTT、prefetch |
| 目标重复 | 转发成功但源 ack 前崩溃 | 目标幂等键、去重表、`ack-mode` |
| 目标不可路由 | exchange/binding 不存在 | 目标拓扑、mandatory/return、Shovel 日志 |
| 连接反复断开 | TLS/认证/网络/URI 编码 | 证书、用户权限、vhost `%2F` |
| 消息环路 | origin 头被丢弃/拓扑成环 | `x-shovel-origin`、max_hops、拓扑图 |
| 顺序变化 | 多 Shovel/多消费者并发 | 按 key 分区、单 Shovel、[第 31 章](31-ordering-partitions.md) |

监控至少要有：Shovel 状态（running/terminated）、转发速率、失败/重连次数、源队列深度、目标 confirm 延迟。管理 API 的 `/api/shovels` 和 Prometheus 指标都要纳入告警。

Mini 的 `Shovel` 在 `mini_rabbitmq/links.py` 中实现：从源 broker get 消息，发布到目标 broker，成功 ack，失败 nack/requeue；用 `x-shovel-origin` 记录跳数防止环路。测试在 `tests/test_links.py`，示例在 `examples/16_shovel_federation.py`。

## Federation

Federation 是“交换机/队列级别的跨 broker 订阅”：

- **exchange federation**：上游交换机的消息复制到下游交换机，适合多活/跨区域复制；
- **queue federation**：下游队列按需从上游“借”消息，只有本地消费者需要、且上游有富余时才拉取；它不是无条件复制，也不是多活双写；
- 通过 upstream（AMQP URI）和 policy 配置；
- 下游按需连接上游，支持断线重连；
- 真实插件用内部 `x-received-from` 头和 `max-hops`（默认 1）防止环路；`x-federation-origin` 是 Mini 的教学 header，不是官方 Federation 的公共字段。

### upstream set 与 policy

Federation 的配置由两部分组成：

1. **upstream**：上游 broker 的 AMQP URI、TLS、认证、重连参数；
2. **policy**：匹配哪些 exchange/queue，用哪个 upstream set。

```bash
# 定义一个 upstream
rabbitmqctl set_parameter federation-upstream eu \
  '{"uri":"amqps://fed:***@rabbit-eu/%2f","expires":3600000}'

# 把匹配 ^orders 的交换机联邦到 eu
rabbitmqctl set_policy --apply-to exchanges fed-orders "^orders" \
  '{"federation-upstream-set":"eu"}'
```

要点：

- 一个 policy 可以引用多个 upstream（集合），实现多上游复制；
- `apply-to` 可以是 exchanges 或 queues；
- policy 可设置 `max-hops`，默认 1：超过跳数的消息不再转发，防止 A→B→C→A 环路；
- 上游 URI 里的 vhost 要 URL 编码（`/` → `%2F`）；
- 认证用户只需最小权限（读上游 exchange/queue、写下游）；
- TLS/证书/网络策略要打通；
- 监控 upstream 状态、复制速率、lag、错误。

Federation 是“下游按需连接上游”的模型，断线重连由插件处理；但它是异步复制，不提供同步确认/强一致。

### 与 Shovel 的区别

| 维度 | Shovel | Federation |
|---|---|---|
| 粒度 | 队列到交换机/队列 | 交换机/队列到交换机/队列 |
| 配置 | 静态/动态参数 | upstream + policy |
| 拓扑 | 显式搬运 | 逻辑订阅/复制 |
| 典型用途 | 迁移、灾备、汇聚 | 多活、地理分布式 |
| 环路防护 | 需要设计 | 内建 `x-received-from` + `max-hops`（默认 1） |
| 运维 | 连接/队列可见 | upstream 状态/策略 |
| 顺序 | 单 Shovel 近似 FIFO | 多 upstream/多消费者更弱 |

Federation 不是“集群复制”。它不保证强一致，不参与 Raft，不提供跨地域的同步确认。消息复制是异步的，源端 confirm 不代表下游已收到。

### Federation 的失败模式

- **upstream 不可达**：下游继续本地消费，但复制 lag 增长；恢复后追赶可能产生重复；
- **policy 匹配错误**：以为复制了，实际没有；用管理 API 检查 link 状态和绑定；
- **多 upstream 环**：A→B、B→A 同时配置；`x-received-from`/`max-hops` 能防环，但会丢弃消息并留下日志；
- **认证/TLS 失败**：连接状态 down，业务无感但灾备已失效；
- **顺序弱化**：不同 upstream/队列的复制速率不同，跨区域顺序不可依赖。

## 多活拓扑

### Active-Passive

主区域处理流量，灾备区域通过 Shovel/Federation 接收副本；故障时切换。优点是冲突少；缺点是灾备资源闲置、切换需要 DNS/客户端配合。

### Active-Active

两个区域都处理流量，互相复制。优点是本地低延迟、资源利用率高；缺点是：

- 同一实体的消息可能在两个区域被处理；
- 需要全局幂等键/去重；
- 顺序难以保证；
- 冲突解决复杂；
- 环路和重复放大。

### 地理分区

按用户/租户分区，每个区域只处理自己的数据，跨区复制用于分析/备份。冲突最少，但需要路由层把用户定向到正确区域。

选择拓扑时，先明确一致性要求。如果业务不能接受重复扣款，active-active 的自动复制需要非常强的幂等和对账，不要只靠消息中间件。

## 环路与重复

跨 broker 复制最容易出的问题是环路：

```text
A -> B -> C -> A
```

防护手段：

- origin header 链（Federation 内建，Shovel 可自定义）；
- 最大跳数；
- 不把下游消息再转发回上游；
- 拓扑白名单；
- 监控重复 message_id。

即使没有环路，网络重试也会产生重复。消费端必须：

- 使用业务唯一键去重；
- 维护幂等表/状态机；
- 对重复消息返回相同结果；
- 记录重复率。

## 顺序

跨 broker 异步复制通常不保证全局顺序：

- 不同连接/队列的并发；
- 重试导致后发先至；
- 多区域同时写入；
- 消费者并发。

如果业务需要顺序，按实体 ID 分区到单队列/单消费者，并在目标端保持同一分区键。跨区域顺序通常需要版本号/状态机，而不是依赖 broker 的 FIFO。完整的顺序模型、分区键设计和重排序方案见[第 31 章](31-ordering-partitions.md)。

## 安全与运维

跨 broker 连接要：

- 使用 TLS（AMQPS）；
- 独立最小权限用户；
- 限制源/目标 vhost；
- 保护 Erlang cookie/证书；
- 监控连接状态和延迟；
- 设置重连退避；
- 对跨公网链路做带宽/加密评估；
- 记录消息来源和审计。

Federation/Shovel 的连接会出现在管理 API 中；需要监控 up/down、速率、积压、错误。跨区域链路的延迟和丢包会直接影响复制 lag。复制 lag 的估算：

```text
lag ≈ 源端未确认消息数 / 有效转发速率
有效转发速率 ≈ min(Shovel/Federation 吞吐, 目标 confirm 吞吐, 网络带宽)
```

如果源端持续生产 2,000 msg/s、目标只能 confirm 1,500 msg/s，lag 会以 500 msg/s 增长；此时加 Shovel 实例可能提高并发，但目标 confirm 仍是瓶颈。先定位瓶颈，再扩容。

## Mini 的实现边界

Mini 的 Shovel 是教学实现：

- 单进程、无网络、无 TLS；
- 手动 `run_once`，没有后台循环；
- 目标不可路由时 requeue；
- origin 链防环；
- 没有动态配置/管理 API。

Mini 的 FederationLink 监听源 broker 的 publish 事件，把匹配消息复制到目标 broker：

- 支持 routing key 正则；
- 使用 `x-federation-origin` 防环；
- 没有 upstream 连接/断线重连；
- 不是 RabbitMQ 插件的完整实现。

这些边界在第 25 章统一说明。真实生产请使用 RabbitMQ 官方 federation/shovel 插件，并按其文档配置。

## 迁移与蓝绿 playbook

用 Federation/Shovel 做 RabbitMQ 版本或集群迁移：

```text
阶段 0：发现
  - 拓扑/队列/绑定/策略/权限/插件/客户端
  - 消息速率/大小/积压/保留
  - 依赖的 vhost/证书/网络

阶段 1：目标设计
  - 新集群版本/队列类型/命名
  - 安全/监控/备份
  - 迁移期间的双写/双读方案

阶段 2：复制
  - 建目标拓扑
  - Federation/Shovel 单向复制
  - 观察 lag/重复/错误

阶段 3：双跑
  - 生产者双写或影子流量
  - 消费者双读/比对
  - 限速/回滚开关

阶段 4：切换
  - 低峰窗口
  - 停旧生产者/排空旧队列
  - 切 DNS/配置
  - 验证生产/消费/延迟

阶段 5：退役
  - 观察期/对账
  - 删除旧拓扑/用户
  - 归档配置/数据
  - 复盘
```

风险：

- 拓扑参数不一致导致 406；
- 客户端不兼容新版本；
- 重复/顺序变化；
- 复制 lag 导致切换时丢消息；
- 回滚路径未演练。

## 多活冲突解决

Active-Active 的难点不是“复制消息”，而是“同一实体在两个区域被并发修改”。

| 策略 | 说明 | 适用 |
|---|---|---|
| 最后写入获胜（LWW） | 用时间戳/版本号覆盖 | 低冲突、可丢更新 |
| 版本向量 | 检测并发修改 | 需要冲突检测 |
| 业务分区 | 每个实体只在一个区域写 | 最容易 |
| 单向复制 | 主区域写，备区域读 | 灾备 |
| 冲突队列 | 冲突进人工/自动解决 | 金融/订单 |

工程建议：

- 能分区就分区（按用户/租户/地域），避免真正的多写；
- 必须多写时，事件带版本号/因果信息；
- 消费端状态机拒绝旧版本；
- 冲突有审计/人工兜底；
- 不要假设 RabbitMQ 的 FIFO 能解决跨区域顺序。

## 本章小结

Shovel 适合队列级搬运/迁移/灾备，Federation 适合交换机/队列级的跨 broker 订阅。两者都是异步、至少一次，需处理重复、顺序、环路和安全。多活拓扑的核心不是“复制消息”，而是业务幂等、分区键、冲突解决和切换演练。

需要强调的是：Shovel 和 Federation 是 **broker 之间的 AMQP 搬运/订阅机制**，不是通用的跨系统桥。它们不理解 Kafka 的 partition/offset、SQS 的 visibility timeout、CDC 的 LSN，也不负责 schema 转换。当目标不是另一个 RabbitMQ 时，应把它当成一个有状态桥接器来设计；语义映射、崩溃矩阵和 checkpoint 设计见[附录 Z](../appendices/Z-event-bridging-interop.md)。

下一章横向看协议生态：AMQP 1.0、MQTT、STOMP、WebSocket、HTTP 和 Stream 协议，以及不同客户端库的行为差异。

---

[上一章：第 12 章 Streams 与 Super Streams](12-streams-super-streams.md) | [返回目录](../README.md) | [下一章：第 14 章 协议生态与多语言客户端](14-protocol-ecosystem-clients.md)
