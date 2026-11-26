# 第 16 章 可观测性：指标、日志、追踪与 Firehose

> 本章定位：消息系统的故障往往不是“服务挂了”，而是“队列在涨、消费者在假活、确认在变慢”。可观测性要能回答：消息在哪、为什么不动、下一小时会不会失控。

## 四类信号

| 信号 | 回答的问题 | 典型来源 |
|---|---|---|
| Metrics | 趋势、容量、告警 | Prometheus、管理 API、系统指标 |
| Logs | 具体事件、错误上下文 | broker 日志、客户端日志、审计 |
| Traces | 一条消息跨服务路径 | OpenTelemetry/厂商 APM |
| Events | 拓扑/管理变更 | 管理事件、审计、Firehose |

不要只建一个 Grafana 面板就称为“可观测”。至少要有：队列深度趋势、消费速率、确认延迟、连接/channel 数、资源水位、DLQ 数量和错误日志关联。

## RabbitMQ 的关键指标

### 队列

- `messages_ready`：可投递但未被取走；
- `messages_unacknowledged`：已投递未确认；
- `messages`：两者之和；
- `consumers`：消费者数量；
- `message_bytes`/存储大小；
- publish/deliver/ack/redeliver 速率；
- consumer utilisation（如有）。

判断“队列是否健康”不能只看 ready。ready 高 + unacked 高 + consumer=0，说明没有消费者；ready 高 + unacked 高 + consumer>0，可能是消费者卡住/下游慢；ready 低 + unacked 高，可能是 prefetch 太大或处理超时。

### 连接与信道

- 连接数、channel 数；
- blocked 连接数；
- 连接建立/关闭速率；
- 每连接 channel 数；
- consumer 数；
- 认证失败/权限拒绝。

连接数暴涨通常是客户端泄漏；channel 数暴涨可能是每请求建 channel；blocked 持续说明内存/磁盘告警。

### 资源

- 内存使用/告警；
- 磁盘剩余/告警；
- 文件描述符；
- Erlang 进程/调度器；
- GC/内存碎片；
- 网络带宽/延迟；
- 磁盘 IOPS/fsync 延迟。

### Quorum/集群

- 每个 quorum queue 的 leader/follower；
- commit index/lag；
- 选举次数；
- 分区（partitions）；
- 节点存活；
- Raft 日志/快照大小。

### Stream

- first/last offset 与当前日志尾部；
- 每个 consumer 的 committed offset 与 lag（尾 offset − consumer offset）；
- segment 数量/大小、retention 触发与磁盘占用；
- leader/replica 状态、复制 lag；
- consumer group 成员变化/rebalance；
- 过滤值基数与过滤后读取量。

Stream 的“积压”不是 `messages_ready`，而是 offset lag；用队列指标看 Stream 会得到错误的健康判断。

### 发布/消费语义

- publisher confirm 延迟；
- nack/return 数；
- redelivery 率；
- DLQ 增长；
- 消费失败率；
- 端到端延迟（带时间戳）。

## Prometheus 与管理 API

RabbitMQ 的 Prometheus 插件通常暴露在 15692（具体路径/端口以版本为准）。管理 API 在 15672，提供 JSON 视图，适合排障和低频采集。生产监控优先用 Prometheus 指标，避免高频轮询管理 API。

常见指标族（名称以实际插件为准）：

```text
rabbitmq_connections
rabbitmq_channels
rabbitmq_queues
rabbitmq_queue_messages_ready
rabbitmq_queue_messages_unacknowledged
rabbitmq_queue_consumers
rabbitmq_global_messages_received_total
rabbitmq_global_messages_acknowledged_total
rabbitmq_alarms_memory_used_watermark
rabbitmq_alarms_free_disk_space_watermark
```

不要硬编码指标名到告警里而不做版本验证；插件/版本升级可能改变命名或标签。

## 告警设计

差告警：

```text
queue messages > 1000
```

它会在正常批量任务时误报，也会在大队列业务中永远触发。更好的告警：

- ready 持续增长超过 N 分钟，且消费速率低于生产速率；
- ready > 阈值且 consumers = 0；
- unacked 持续高且 ack 速率下降；
- DLQ 增长率/非空时长；
- blocked 连接持续 > 1 分钟；
- 内存/磁盘接近水位；
- quorum 无 leader/副本不足；
- confirm p99 超过 SLO；
- 节点间分区。

使用多条件、持续时间和业务基线，避免“单点阈值”。

## SLO、错误预算与告警分级

没有 SLO 的监控面板，最后都会变成“所有图都看，所有告警都吵”。先把“多好算好”写成可测量的 SLI/SLO：

| SLI | 定义 | 示例 SLO |
|---|---|---|
| 发布成功率 | confirm ack / 总发布 | 99.9% / 5 分钟 |
| 发布延迟 | publish 到 confirm 的 p99 | p99 < 100 ms |
| 消费延迟 | 事件产生到业务处理完成 | p99 < 2 s |
| 积压恢复 | 峰值后回到基线的时间 | < 10 分钟 |
| DLQ 比率 | 死信 / 总消费 | < 0.01% |
| 重复率 | 重复副作用 / 总处理 | 由业务定义 |

有了 SLO，就能算**错误预算**：例如 30 天 99.9% 可用，允许约 43 分钟不可用。告警不应按“某个队列 > 0”触发，而应按“预算消耗速度”分级：

- **P1/ page**：多窗口高燃烧率（例如 1 小时和 5 分钟窗口都远超预算），立即叫人；
- **P2/ ticket**：低燃烧率但持续消耗，工作时间处理；
- **P3/ dashboard**：趋势异常，不直接叫人，进入容量/治理评审。

一个概念性的多窗口燃烧率规则：

```text
长期窗口：1h 燃烧率 > 14.4（约 2 天耗尽 30 天预算）→ page
短期窗口：5m 燃烧率 > 14.4 且 1h 也异常 → page
慢燃烧：6h 燃烧率 > 6 → ticket
```

告警要绑定 runbook、owner 和“恢复验证”。没有错误预算的团队，容易在“告警太少漏故障”和“告警太多没人看”之间反复；SLO 是把两者拉回可讨论的量化基线。容量/SLO 的运维视角见第 17 章。

## 日志

RabbitMQ 日志包含：

- 启动/关闭；
- 连接/认证失败；
- 资源告警；
- 队列/插件错误；
- 集群/分区事件；
- 管理操作（取决于配置）。

客户端日志应记录：

- 连接/channel 重建；
- publish confirm 超时/nack/return；
- 消费失败与重试；
- 消息 message_id/correlation_id；
- 不记录正文/密码。

日志级别生产通常 INFO，排障时临时 DEBUG。不要把 DEBUG 长期打开，日志量和敏感信息风险都会上升。

## 追踪

消息追踪有两种层次：

1. **broker 内部追踪**：Firehose tracer、tracing 插件，能看到 publish/deliver/ack 事件；
2. **分布式追踪**：在消息 headers 中传播 traceparent/tracestate，消费者创建 span，形成跨服务链路。

OpenTelemetry 生态可用，但 RabbitMQ 的插件/客户端支持随版本和语言不同。设计时：

- 在发布时注入 trace context；
- 消费时提取并关联；
- 记录 message_id、correlation_id、queue、routing key；
- 采样，避免高吞吐下追踪成本失控；
- 不要把敏感数据写入 span。

Firehose 适合短期排障，不适合长期全量开启；它会产生大量事件和开销。

## 端到端延迟

仅看 broker 指标无法知道“用户等了多久”。建议在消息中带：

- `published_at`（生产者时钟）；
- `message_id`；
- `trace_id`；
- 业务实体 ID。

消费者计算 `now - published_at`，上报端到端延迟分布。注意跨机器时钟偏差；可以用 NTP/云时钟同步，或记录生产者/消费者时间戳分别分析。

## Mini 的可观测性

Mini 提供：

- `Metrics` 计数器/仪表；
- `/api/overview`、`/api/queues`、`/api/exchanges`、`/api/nodes`、`/api/health/checks/alarms`；
- `/metrics` Prometheus 文本；
- 队列统计（ready/unacked/consumers/delivered/acked/redelivered/dead_lettered）；
- 连接/信道管理视图；
- 告警状态。

相关代码：`mini_rabbitmq/metrics.py`、`mini_rabbitmq/management.py`、`mini_rabbitmq/server.py`。测试 `tests/test_management.py`，示例 `examples/13_management_api.py`。

Mini 的限制：

- 指标粒度粗；
- 没有历史存储/告警规则；
- 没有分布式追踪；
- 没有 Firehose；
- 管理 API 只读 + 少量写操作；
- 没有真正的多节点指标聚合。

## 一个最小监控面板

按行组织：

1. **流量**：publish rate、deliver rate、ack rate、redeliver rate；
2. **积压**：ready、unacked、DLQ、每队列 Top N；
3. **消费者**：consumer 数、prefetch、ack 延迟、失败率；
4. **资源**：内存、磁盘、FD、CPU、网络；
5. **集群**：节点存活、quorum leader、分区、选举；
6. **发布可靠**：confirm p99、nack、return、超时；
7. **业务**：订单创建/支付成功率、端到端延迟。

每个面板都要有“正常基线”和“异常时怎么做”的链接，否则只是漂亮图表。

## 排障顺序

当“消息不动”时：

1. 队列有消费者吗？
2. 消费者连接/channel 是否活着？
3. unacked 是否达到 prefetch 上限？
4. 消费者是否卡在下游/死锁？
5. 队列是否被 policy 限制/排他？
6. broker 是否内存/磁盘告警？
7. quorum 是否有 leader/多数派？
8. 消息是否被 DLX/return/丢弃？
9. 路由键/绑定是否变化？
10. 客户端是否在重连风暴？

先看队列和消费者，再看资源，最后看拓扑。不要一上来重启 broker。

## PromQL 查询示例

以下查询基于常见的 Prometheus 指标命名，实际标签/名称以目标版本插件为准：

```text
# 每队列 ready 消息数（Top 10）
topk(10, rabbitmq_queue_messages_ready)

# 每队列消费速率（5 分钟）
sum by (queue) (rate(rabbitmq_queue_messages_delivered_total[5m]))

# 发布速率与确认速率
sum(rate(rabbitmq_global_messages_received_total[5m]))
sum(rate(rabbitmq_global_messages_acknowledged_total[5m]))

# 未确认消息持续高
max_over_time(rabbitmq_queue_messages_unacknowledged[10m])

# ready 是 gauge：看当前值或 delta，不要对它用 rate()
sum by (queue) (rabbitmq_queue_messages_ready) > 10000
sum by (queue) (delta(rabbitmq_queue_messages_ready[10m])) > 1000

# 告警状态
rabbitmq_alarms_memory_used_watermark
rabbitmq_alarms_free_disk_space_watermark

# 分区节点
rabbitmq_partitions > 0
```

告警规则示例（概念）：

```text
- alert: QueueBacklogGrowing
  expr: |
    sum by (queue) (delta(rabbitmq_queue_messages_ready[10m])) > 1000
  for: 15m

- alert: QueueBacklogHigh
  expr: |
    sum by (queue) (rabbitmq_queue_messages_ready) > 10000
  for: 10m
```

`rabbitmq_queue_messages_ready` 是 gauge，不能套 `rate()`；`rabbitmq_global_messages_received_total` 没有 `queue` 标签，也不能直接和每队列的 counter 做向量匹配。需要全局发布/确认对比时，用两个 global counter；需要每队列积压时，用 ready 的当前值或 `delta`。

不要直接抄指标名；升级插件/版本后先验证 `metric_relabel_configs` 和标签。

## 版本变化对监控的影响

升级 RabbitMQ 时，指标名和维度也会变。以下是 4.x 中容易踩的几处：

- **4.3 Quorum 32 级优先级**：管理 UI 会按优先级显示消息计数；旧面板如果只聚合 `messages_ready`，会看不到“高优先级把低优先级压住”的结构性问题；
- **4.3 按消费者超时**：消费者超时从信道级迁移到队列/消费者级，但 4.3+ 只有 Quorum Queue 会评估它，classic/stream 不会因“处理太久没 ack”触发；`consumer_timeout` 相关指标和关闭原因要按队列类型核对；
- **4.3 Khepri only**：元数据健康、Raft 日志、快照指标取代了部分 Mnesia 指标；`cluster_partition_handling` 不再生效，分区告警要基于 Raft 状态而不是旧配置；
- **4.4 原生 STOMP**：STOMP 不再经过内部 AMQP 客户端，连接/进程/channel 指标的数量和含义会变化，旧阈值可能误报；
- **4.4 CQv2 `.sqs`**：classic 队列的磁盘/段指标会变化，容量面板要重新基线。

升级后的第一周，应该把新旧版本的同一业务负载指标并排对比，而不是直接沿用旧告警阈值。

## Grafana 面板结构

一个可用的 RabbitMQ 面板按“症状 → 原因 → 资源”组织：

1. **顶部 SLO**：发布成功率、confirm p99、端到端 p99、DLQ 增长；
2. **队列**：Top N ready/unacked、消费速率、消费者数、redelivery；
3. **连接**：连接/channel 数、blocked、认证失败、重连；
4. **集群**：节点存活、partitions、Quorum leader/副本、Raft 日志；
5. **资源**：CPU、内存、磁盘剩余/IOPS、网络、FD；
6. **业务**：订单/支付成功率、重放/对账状态。

每个面板要有：

- 正常基线（阈值线）；
- 告警链接/runbook；
- 时间范围与采样说明；
- 下钻链接（队列详情/日志/追踪）。

不要堆 50 个图；先保证“出问题时能在 1 分钟内定位到队列/消费者/资源”。

## OpenTelemetry 接入

消息链路的追踪要解决“生产者的 span 如何传到消费者”：

```text
producer:
  span publish
  inject traceparent/tracestate into message headers

broker:
  (可选) 记录 publish/deliver 事件

consumer:
  extract traceparent from headers
  start span consume (parent = producer trace)
  ack/nack 记录为 span event
```

要点：

- 在消息 headers 中传播 `traceparent`，不要放在 body；
- 消费者创建的 span 要与生产者同 trace；
- 记录 message_id/correlation_id/queue/routing key；
- 采样率与吞吐匹配，避免追踪成本超过业务；
- 不要把 PII/消息正文写入 span；
- broker 侧追踪（Firehose/tracing 插件）与客户端追踪互补。

## Firehose 的使用与成本

Firehose 可以把 broker 内部的 publish/deliver/ack 等事件流式导出，适合短期排障/审计。但它：

- 产生大量事件；
- 增加 CPU/网络/存储开销；
- 不适合长期全量开启；
- 需要消费者/过滤/采样；
- 可能包含敏感元数据。

使用建议：

- 只在排障窗口开启；
- 用 tracer 过滤关心的 vhost/队列；
- 限制速率/采样；
- 明确保留期；
- 与日志/指标/trace 关联，而不是单独看。

## 告警 Runbook 模板

每条告警都应该有 runbook：

```text
告警名：
含义：
影响：
立即检查：
  1.
  2.
  3.
可能原因：
  - 
处理步骤：
  - 
升级路径：
  - 
恢复验证：
  - 
相关面板/日志/追踪：
```

没有 runbook 的告警，通常会在凌晨变成“重启试试”。

## 本章小结

可观测性要覆盖队列、连接、资源、集群和发布/消费语义。告警应基于趋势、持续时间和业务基线，而不是单一阈值。日志、指标、追踪和审计互补；Firehose 适合短期排障，不应长期全量开启。Mini 提供了最小管理面和指标，足以练习排障，但不具备生产监控体系的完整性。

下一章进入运维与容量：部署拓扑、备份、升级、故障演练、SLO 和容量公式。

---

[上一章：第 15 章 安全：认证、授权、TLS 与审计](15-security.md) | [返回目录](../README.md) | [下一章：第 17 章 运维、部署与容量规划](17-operations-capacity.md)
