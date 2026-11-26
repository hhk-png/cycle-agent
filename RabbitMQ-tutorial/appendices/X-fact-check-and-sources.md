# 附录 X 事实核查、版本验证与官方资料地图

> 本附录定位：给读者一套“怎么确认 RabbitMQ 在目标版本上到底怎么行为”的方法。它不是官方文档的替代品，而是把“版本事实”从博客、记忆和二手结论里拉回到可复核的证据链。教程结构由 `check_docs.py` 校验，但**结构正确不等于事实正确**；版本相关结论必须按本附录的流程核对。

## 为什么“文档看起来对”不等于“事实对”

一份技术教程最常见的三类事实错误：

1. **版本漂移**：结论在 3.13 成立，4.0 已改变，但文章没有标注版本；
2. **二手转述**：博客把“Quorum 不支持优先级”当成永久事实，忽略了 4.0 的两档和 4.3 的 32 级；
3. **环境差异**：单节点、无 TLS、默认参数下的实验，被外推成“生产结论”。

RabbitMQ 4.x 的变化尤其密集：镜像队列移除、CQv1 移除、lazy 模式失效、transient 非排他队列收紧、global QoS 拒绝、默认消息上限变化、AMQP 1.0 核心化、Khepri 唯一化、Quorum 优先级演进、原生延迟能力、CQv2 `.sqs` 单向门。任何一条写错，都可能让读者在生产升级时踩坑。

本附录给出的不是“标准答案列表”，而是**核查答案的方法**。具体事实仍以目标版本的官方文档、源码和实测为准。

## 事实分级：什么算证据

| 级别 | 来源 | 可靠性 | 适用 |
|---|---|---|---|
| L0 | AMQP 规范、RabbitMQ 官方协议文档 | 最高（但可能滞后于实现） | 线格式、方法/字段、语义定义 |
| L1 | 目标版本的官方文档（versioned docs） | 高 | 参数、默认值、版本能力、运维步骤 |
| L2 | 对应 tag 的源码、release notes、PR | 高（实现真相） | 文档含糊、行为边界、bug 判断 |
| L3 | 一次性集群实测 | 高（限环境） | 确认行为、时序、错误码、客户端差异 |
| L4 | 社区博客、问答、第三方总结 | 低（假设来源） | 发现问题、生成假设，不作为最终证据 |

一条可复核的结论至少要有 **L1 + L3**：官方文档说明“应该怎样”，实测确认“这个版本确实这样”。涉及实现细节或文档冲突时，再加 **L2**。只有 L4 的结论，不应写进生产决策。

## 版本坐标矩阵

下表是“做架构决策时的版本坐标”，不是逐条 API 文档。每一行都给出验证入口；具体补丁版本可能调整默认值，落地前用目标版本实测。

| 事实 | 3.13 | 4.0–4.2 | 4.3 | 4.4 | 怎么验证 |
|---|---|---|---|---|---|
| 经典镜像队列 | 弃用 | 移除 | 移除 | 移除 | `rabbitmqctl list_policies`、`list_queues mirror_pids` |
| CQv1 存储 | 可选 | 移除 | 移除 | 移除 | `classic_queue.default_version=1` 是否导致启动失败 |
| Lazy 模式 | 3.12 起忽略 | 忽略 | 忽略 | 忽略 | 设 `x-queue-mode` 后看内存/磁盘行为 |
| Transient 非排他队列 | 弃用 | 弃用 | 默认拒绝 | 默认拒绝 | 声明非 durable、非 exclusive 队列是否 406 |
| Global QoS | 可用（不推荐） | 弃用 | 默认拒绝 | 默认拒绝 | `basic_qos(global=True)` 是否 channel error |
| 默认最大消息 | 128 MiB | 16 MiB | 16 MiB | 16 MiB | 查 `max_message_size`，发大消息看错误 |
| AMQP 1.0 | 插件 | 核心 | 核心 | 核心 | 插件是否 no-op；1.0 客户端能否直连 |
| Khepri | 实验 | 4.2 新集群默认 | 唯一 | 唯一 | `list_feature_flags`、`status`、管理 overview |
| Quorum 优先级 | 无 | 两档（0–4 normal，>4 high，约 2:1） | 32 级（0–31，总是启用） | 32 级 | 发不同 priority，观察消费顺序 |
| Quorum delivery-limit | 无/可配 | 默认 20 | 默认 20；区分 acquired/delivery 计数 | 同 4.3 | 查队列参数；reject vs nack 对比 |
| 原生延迟重试 | 无 | 无 | `x-delayed-retry-*` | 同 4.3 | 声明参数，观察重投间隔 |
| 定时投递 | 无 | 无 | 无 | `x-opt-delivery-time`（long，epoch ms，Quorum only；classic 立即投递） | 发未来时间，立即 get 应为空 |
| 延迟交换机插件 | 可用 | 可用 | 不可用 | 不可用 | 插件列表、启动日志、`x-delay` 行为 |
| STOMP | 经内部 AMQP | 同左 | 同左 | 原生实现 | 连接/进程指标、日志变化 |
| CQv2 `.sqs` | 无 | 无 | 无 | 有 | 数据目录查找 `.sqs`；降级测试 |
| `x-modulus-hash` | 无 | 无 | 内置 | 内置 | 声明交换机类型，观察路由 |
| 按消费者 `consumer_timeout` | 信道级 | 信道级 | 队列/消费者级 | 同 4.3 | 查队列参数/policy，观察超时关闭 |
| Stream SQL 过滤 | 无 | 4.2+ | 有 | 有 | 用 Stream 客户端发过滤表达式 |
| `x-stream-initial-offset` | 无 | 无 | 无 | 队列创建参数 | 建空 Stream，看第一条 offset |

使用这张表时注意：

- “版本”是大版本坐标，补丁版本可能有差异；
- 云托管（Amazon MQ、CloudAMQP 等）可能滞后，先确认厂商版本；
- “移除/拒绝”可能表现为 406、channel error、启动失败或静默忽略，必须实测具体表现；
- 不要用一张表替代 release notes，尤其是升级路径和 feature flags。

## 本地验证配方

### 1. 固定版本、隔离环境

```bash
docker run --rm -d --name rmq-43 \
  -p 5673:5672 -p 15673:15672 \
  -e RABBITMQ_DEFAULT_USER=app \
  -e RABBITMQ_DEFAULT_PASS=change-me \
  rabbitmq:4.3-management
```

每个实验一个容器/端口/数据目录，避免“上次的队列还在”。不要在共享集群上验证破坏性行为。

### 2. 记录版本指纹

```bash
docker exec rmq-43 rabbitmqctl version
docker exec rmq-43 rabbitmq-diagnostics status
docker exec rmq-43 rabbitmqctl list_feature_flags
docker exec rmq-43 rabbitmqctl list_plugins
docker exec rmq-43 rabbitmqctl list_queues name type arguments
```

### 3. 用最小探针验证行为

不要用“跑一遍业务系统”做事实核查。写最小脚本，只验证一个行为：

```python
import pika

connection = pika.BlockingConnection(pika.ConnectionParameters(
    host="localhost", port=5673,
    credentials=pika.PlainCredentials("app", "change-me"),
))
channel = connection.channel()
channel.queue_declare("probe", durable=True, arguments={"x-queue-type": "quorum"})
# 只改变一个变量：priority / header / nack 方式 / 队列类型
connection.close()
```

### 4. 正反都测

一个好的事实核查至少包含：

- 正向：参数正确时行为符合预期；
- 反向：参数错误/缺失/类型不对时，是否报错，还是静默失效；
- 边界：空队列、单条消息、达到上限、跨版本；
- 环境：单节点 vs 3 节点、TLS vs 明文、classic vs quorum。

例如验证 4.4 `x-opt-delivery-time`，只测“延迟生效”不够；还要测字符串类型、classic 队列、4.3 集群、`x-delay`，因为这些都是静默失效路径。

### 5. 记录时间与时序容差

延迟、选举、超时类实验不要用“精确毫秒”断言。记录：

- 发布/投递时间戳；
- 期望区间（例如 2s ± 500ms）；
- 重复次数；
- 客户端/网络/容器时钟偏差。

### 6. 对照源码/发布说明

当文档与行为冲突时：

1. 查目标版本的 release notes 和升级指南；
2. 查对应 tag 的源码/PR（源码地图见[附录 Q](Q-broker-internals-source-map.md)）；
3. 在一次性集群复现；
4. 记录“文档说 X、实测 Y、版本 Z”，不要只留一个结论。

## 官方资料地图

| 问题 | 首选来源 | 说明 |
|---|---|---|
| 版本变更、破坏性变更 | 官方 release notes、升级指南、版本 highlights | 先看“移除/弃用/默认值” |
| 参数、默认值、队列类型 | 目标版本的 versioned docs | 不要看无版本号的旧页面 |
| AMQP 0-9-1 线格式 | AMQP 规范、官方协议页 | 帧/方法/字段/错误码 |
| AMQP 1.0 | 官方 AMQP 页、OASIS 规范、[附录 R](R-amqp-1-0-deep-dive.md) | performative/credit/settlement |
| 源码实现 | GitHub 对应 tag、`deps/` 目录 | 文档含糊时的最终依据 |
| 管理 API | 官方 management 文档、`/api` 实际响应 | 字段随版本变化 |
| 指标 | 官方 monitoring/prometheus 文档 | 指标名/标签会变 |
| 安全 | 官方 security 页、GitHub security advisories | 漏洞/公告/支持版本 |
| Kubernetes | Cluster Operator/Topology Operator 仓库与文档 | CRD/版本矩阵 |
| 客户端行为 | 各客户端仓库/文档 | 自动重连、confirm、线程模型 |
| 云托管 | 厂商版本文档/功能矩阵 | 版本滞后、插件限制 |
| 性能 | PerfTest 仓库、官方基准文章 | 只作方法参考，不作容量承诺 |

常用入口：

- RabbitMQ 文档：<https://www.rabbitmq.com/docs>
- 版本化文档：<https://www.rabbitmq.com/docs/4.4/quick-start>（把 `4.4` 换成目标版本）
- 发布说明：<https://github.com/rabbitmq/rabbitmq-server/releases>
- 源码仓库：<https://github.com/rabbitmq/rabbitmq-server>
- 管理 API：<https://www.rabbitmq.com/docs/management>
- Quorum Queues：<https://www.rabbitmq.com/docs/quorum-queues>
- Streams：<https://www.rabbitmq.com/docs/streams>
- Feature flags：<https://www.rabbitmq.com/docs/feature-flags>
- 元数据存储：<https://www.rabbitmq.com/docs/metadata-store>
- AMQP 1.0：<https://www.rabbitmq.com/docs/amqp>
- Consumer prefetch：<https://www.rabbitmq.com/docs/consumer-prefetch>
- TLS：<https://www.rabbitmq.com/docs/ssl>
- OAuth 2.0：<https://www.rabbitmq.com/docs/oauth2>
- Definitions：<https://www.rabbitmq.com/docs/definitions>
- Cluster Operator：<https://github.com/rabbitmq/cluster-operator>
- Topology Operator：<https://github.com/rabbitmq/messaging-topology-operator>
- AMQP 0-9-1 规范：<https://www.amqp.org/specification/0-9-1/amqp-org-download>
- AMQP 1.0 规范：<https://www.amqp.org/specification/1.0/amqp-org-download>

## 常见过时结论清单

下面这些说法在中文技术文章里仍然常见，但在 4.x 上需要加版本限定或直接修正。

| 过时说法 | 更准确的说法 |
|---|---|
| “Quorum 不支持优先级” | 4.0–4.2 有 normal/high 两档；4.3+ 有 32 级严格优先级。 |
| “Quorum 要设置 `x-max-priority` 才有优先级” | 错。Quorum 优先级总是启用；`x-max-priority` 只属于 classic，Quorum 忽略它。 |
| “用镜像队列做高可用” | 4.0 起镜像队列移除；关键队列用 Quorum/Stream。 |
| “lazy queue 可以省内存” | 3.12 起 `x-queue-mode` 被忽略；classic 默认已接近 lazy。 |
| “延迟交换机插件可以继续用” | 插件依赖 Mnesia；4.3 起 Mnesia 移除，插件归档不可用。 |
| “消息默认上限 128 MiB” | 4.0 起默认 16 MiB；`frame_max` 是另一回事。 |
| “global QoS 可以限制整条信道” | 4.0 弃用、4.3 默认拒绝；只用 per-consumer prefetch。 |
| “Khepri 在 4.3 还是可选” | 4.3 起 Khepri 是唯一元数据存储。 |
| “AMQP 1.0 要装插件” | 4.0 起是核心协议，旧插件 no-op。 |
| “`x-delay` 在 4.4 Quorum 上还能延迟” | 不会；4.4 用 `x-opt-delivery-time`，`x-delay` 静默失效。 |
| “所有队列都应该上 Quorum” | 低价值/临时/大量小队列用 classic 更合适；按丢失代价分层。 |
| “`x-stream-initial-offset` 是消费者起点” | 它是创建 Stream 时的首条消息 offset；消费者起点用 `x-stream-offset`。 |

这张表的作用不是让你背结论，而是提醒：**看到一句“RabbitMQ 就是这样”时，先问版本、队列类型和证据来源。**

## 一份事实核查工作流

```text
1. 写清 claim
   一句话，包含版本/队列类型/前提

2. 选来源
   L1 官方 versioned docs + L2 release notes/source

3. 设计最小实验
   单变量、可重复、正反路径

4. 记录
   版本指纹 + 配置 + 预期 + 实测 + 反例 + 日期

5. 结论
   加版本限定；写清未验证项

6. 复核
   下次升级/半年后重跑；云厂商版本变化时重跑
```

### 核查卡片模板

```yaml
claim: "Quorum 在 4.3+ 有 32 级严格优先级，x-max-priority 被忽略"
version_scope: "RabbitMQ 4.3.x / 4.4.x, quorum queue"
sources:
  - type: official_docs
    url: "https://www.rabbitmq.com/docs/quorum-queues"
  - type: release_notes
    url: "https://github.com/rabbitmq/rabbitmq-server/releases"
experiment:
  queue_type: quorum
  priorities: [0, 4, 9, 31]
  client: pika
expected: "[31, 9, 4, 0]"
observed: "[31, 9, 4, 0]"
negative_cases:
  - "x-max-priority=10 不改变行为"
  - "4.2 镜像上 high/normal 两档"
unverified:
  - "云托管 4.3 是否完全一致"
owner: platform-team
review_by: "2027-05-01"
```

## 文档与实现冲突时怎么办

优先顺序不是“谁听起来更权威”，而是：

1. **目标版本的行为**：实测 + 源码；
2. **目标版本的官方文档**：确认参数/默认值/升级路径；
3. **release notes / PR**：解释“为什么变”；
4. **旧版本文档/博客**：只作历史背景。

如果官方文档与实测冲突：

- 先确认版本、队列类型、参数、插件、客户端是否一致；
- 再查 release notes/源码，看是否是文档滞后或已知 bug；
- 记录冲突，不要静默选择对自己有利的一方；
- 必要时向官方仓库提 issue，并保留最小复现。

## 云托管与版本滞后

托管服务的版本通常滞后于开源版本，且可能限制插件/参数。核查时要额外确认：

- 厂商支持的 RabbitMQ 版本与补丁；
- 是否支持 Quorum 优先级、原生延迟、Khepri、Stream 过滤；
- 是否允许修改 `max_message_size`、`consumer_timeout`、policy；
- 管理 API/指标是否与开源一致；
- 跨区域复制/备份是否由厂商提供，RPO/RTO 是多少。

不要用“开源 4.4 支持”直接推导“托管服务支持”。把厂商版本矩阵纳入事实核查卡片。

## Mini-RabbitMQ 的核查边界

Mini 是教学实现，不是 RabbitMQ 的行为证据：

- 它不实现 4.3/4.4 的优先级调度、原生延迟、Khepri、CQv2、Stream 协议；
- 它的 confirm 是“内存接受”，不是持久化/多数派确认；
- 它的 Raft 是单进程模拟，不含网络 RPC/成员变更/快照安装；
- 它的 JSON 快照不是 WAL/段文件。

Mini 能证明的是“语义可以被写成代码、被测试表达”；真实版本行为必须用真实 RabbitMQ 验证。Mini 与真实实现的逐项映射见[附录 G](G-code-map-test-matrix.md)和[附录 Q](Q-broker-internals-source-map.md)。

## 引用规范

写内部文档/方案时，建议每条版本结论都带：

```text
[版本] [队列类型] [前提] 结论
来源：官方文档 URL / release notes / 源码 tag
实测：日期、版本、配置、结果
未验证：云厂商、补丁版本、规模
```

反面例子：

```text
RabbitMQ 支持延迟消息。
```

正面例子：

```text
RabbitMQ 4.4 的 quorum queue 支持生产者指定 x-opt-delivery-time
（Unix epoch 毫秒，long 类型）；classic queue 不支持，会立即投递。
来源：官方 4.4 文档；实测 4.4.x 单节点；云托管未验证。
```

## 核查检查清单

- [ ] 结论带版本、队列类型和前提；
- [ ] 至少有一个 L1 官方来源；
- [ ] 关键行为有 L3 实测；
- [ ] 正反路径都测了；
- [ ] 记录了版本指纹和实验日期；
- [ ] 标注了未验证项（云厂商、补丁版本、规模）；
- [ ] 没有把单节点结论外推到集群；
- [ ] 没有把 Mini 行为当成真实 RabbitMQ 行为；
- [ ] 过时结论已加版本限定或删除；
- [ ] 半年/升级后安排复核。

## 小结

事实核查不是“多查几个网页”，而是建立一条可复核的证据链：写清 claim、锁定版本、找官方来源、做最小实验、记录正反路径、标注未验证项。RabbitMQ 4.x 的变化密集，任何“永久结论”都可能在下个版本失效。结构校验（`check_docs.py`）保证文档不腐烂；本附录保证**版本事实不被二手转述带偏**。动手实验见[附录 W](W-4x-feature-labs.md)，版本迁移清单见[附录 I](I-version-migration.md)，源码定位见[附录 Q](Q-broker-internals-source-map.md)。

---

[返回目录](../README.md) | [上一章：附录 W RabbitMQ 4.3/4.4 新特性实验手册](W-4x-feature-labs.md) | [下一章：附录 Y 消息系统合规与数据治理](Y-compliance-data-governance.md)
