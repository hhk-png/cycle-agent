# 附录 D RabbitMQ 与 Kafka、Pulsar、NATS、SQS 对比

本附录给出架构/语义层面的对比，不比较具体版本的跑分。吞吐/延迟取决于配置、硬件、持久化、消息大小和客户端。

## 总览

| 系统 | 类型 | 核心抽象 | 强项 | 弱项 |
|---|---|---|---|---|
| RabbitMQ | 消息 broker | exchange/queue/binding | 路由、任务、确认、协议生态 | 日志重放/超大规模流 |
| Kafka | 分布式日志 | topic/partition/offset | 高吞吐、重放、流生态 | 复杂路由/任务/RPC |
| Pulsar | 日志/队列平台 | topic/partition/subscription | 存储计算分离、多租户 | 运维复杂、生态 |
| NATS | 轻量消息 | subject/queue group/JetStream | 低延迟、简单、云原生 | 企业集成/复杂路由 |
| SQS | 云队列 | queue | 免运维、弹性、按量 | 路由/协议/可移植性 |
| SNS | 云 pub/sub | topic/subscription | 广播、与 AWS 集成 | 队列语义弱 |
| Azure Service Bus | 云消息 | queue/topic/subscription | 企业集成、会话/事务 | 云绑定 |
| Google Pub/Sub | 云消息 | topic/subscription | 全球托管、弹性 | 队列控制弱 |
| ActiveMQ Artemis | 消息 broker | queue/topic | JMS/AMQP、企业集成 | 生态/云托管 |
| IBM MQ | 商业消息 | queue/topic | 金融级、企业支持 | 成本/锁定 |

## RabbitMQ vs Kafka

### 模型

- RabbitMQ：消息被路由到队列，消费者确认后删除；重点是“任务/事件被处理”。
- Kafka：消息追加到分区日志，消费者按 offset 读取；重点是“事件被保留/重放”。

### 路由

RabbitMQ 支持 direct/fanout/topic/headers、E2E、AE；Kafka 主要按 topic/partition/key，消费端过滤。复杂多租户路由 RabbitMQ 更自然。

### 确认

RabbitMQ 支持 per-message ack、prefetch、nack/requeue；Kafka 用 offset commit，重复范围取决于提交频率。任务型语义 RabbitMQ 更直接。

### 吞吐/保留

Kafka 在顺序写、批量、分区并行、长期保留上更强；RabbitMQ Quorum 更高可靠但写延迟更高，Streams 补充日志能力但生态不同。

### 选型

- 任务/订单/支付/RPC/企业集成 → RabbitMQ；
- 日志/遥测/事件溯源/流处理/数据管道 → Kafka；
- 两者并用很常见：RabbitMQ 做业务命令/任务，Kafka 做数据/事件日志。

## RabbitMQ vs Pulsar

Pulsar 的存储计算分离、多租户、分层存储吸引大规模平台；运维复杂度高。RabbitMQ 更成熟的路由/协议/管理，适合业务消息。若组织已有 Pulsar 平台，不必为任务队列再引入 RabbitMQ；反之亦然。

## RabbitMQ vs NATS

NATS 轻量、低延迟、subject 路由、JetStream 持久化；适合微服务/边缘/控制面。RabbitMQ 的交换机/队列/确认/管理/插件更企业化。NATS 的队列组与 RabbitMQ 竞争消费者类似，但复杂路由/死信/管理生态不同。

## RabbitMQ vs SQS/SNS

SQS：

- 标准队列（至少一次）和 FIFO 队列；
- visibility timeout、长轮询、DLQ；
- 免运维、按量；
- 限制：路由简单、无 topic 通配、协议/插件有限、跨云可移植性弱。

SNS：

- pub/sub 广播到 SQS/Lambda/HTTP；
- 与 AWS 生态深集成；
- 不是通用队列。

RabbitMQ 的优势是路由/协议/自建/跨云；SQS 的优势是零运维/弹性/成本。很多团队在 AWS 内用 SQS，跨云/混合云用 RabbitMQ。

## RabbitMQ vs Azure Service Bus / Pub/Sub

Service Bus 提供队列/topic/subscription、会话、事务、死信，企业集成强；Pub/Sub 全球托管、弹性。两者都绑定云生态。RabbitMQ 的可移植性/插件/AMQP 生态是差异点。

## 场景决策表

| 需求 | 首选 | 说明 |
|---|---|---|
| 订单/支付任务 | RabbitMQ Quorum | 路由/确认/DLX/重试 |
| 日志/指标管道 | Kafka/Pulsar | 高吞吐/保留/重放 |
| 简单云任务 | SQS | 免运维 |
| 广播到多系统 | SNS/EventBridge/RabbitMQ fanout | 取决于云 |
| IoT | MQTT 平台/EMQX | 设备协议/规模 |
| 微服务控制面 | NATS | 低延迟/轻量 |
| 企业 JMS 集成 | ActiveMQ/IBM MQ/RabbitMQ | 协议/事务/支持 |
| 事件溯源 | Kafka | 日志/重放 |
| AI 推理任务 | RabbitMQ/SQS | 任务/重试/优先级 |
| 多租户 SaaS | RabbitMQ vhost/云队列 | 隔离/配额 |

## 常见误区

1. **“Kafka 吞吐高，所以所有消息都用 Kafka”**：任务/RPC/复杂路由会很别扭。
2. **“RabbitMQ 不能高吞吐”**：配置得当可以很高，只是与 Kafka 的规模/模式不同。
3. **“SQS 便宜所以不用 RabbitMQ”**：忽略路由/迁移/跨云/运维能力。
4. **“云托管=不用运维”**：容量、拓扑、客户端、监控仍需团队。
5. **“队列=日志”**：语义不同，重放/保留/确认都不一样。
6. **“多协议=可以随便混”**：属性/确认/路由映射会丢语义。

## 评分模板

用 1～5 分、按权重评分：

```text
维度（权重）:
  任务/路由 25%
  可靠/复制 20%
  吞吐/重放 15%
  运维 15%
  生态 15%
  成本 10%
```

不要用别人的权重。让业务/平台/安全/财务一起定义，并记录评分理由。

## 结论

RabbitMQ 不是“最快的消息系统”，而是“复杂路由 + 可靠任务 + 企业集成”的强手。Kafka/Pulsar 是日志/流平台，NATS 是轻量消息，SQS/SNS 是云队列。正确架构常常是组合，而不是单选。

---

[返回目录](../README.md) | [上一章：附录 C 配置、参数与功能矩阵](C-configuration-feature-matrix.md) | [下一章：附录 E 故障排查手册](E-troubleshooting.md)
