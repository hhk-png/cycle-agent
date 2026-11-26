# 第 06 章 队列语义：FIFO、优先级、TTL 与长度限制

> 本章定位：队列不是“消息数组”。它是一个带容量、过期、优先级、消费者、确认和死信规则的有限状态机。理解这些规则，才能预测积压时的行为。

## 队列的基本属性

声明队列时，最关键的参数是：

| 参数 | 含义 | 注意 |
|---|---|---|
| name | 队列名，空串表示由 broker 生成 | 生成名通常以 `amq.gen-` 开头 |
| durable | broker 重启后队列元数据是否保留 | 不等于消息持久化 |
| exclusive | 只允许声明连接使用，连接关闭删除 | 通常用于临时回复队列 |
| auto-delete | 最后一个消费者取消后是否删除 | 历史上至少有过消费者 |
| arguments | `x-*` 扩展参数 | 类型、TTL、长度、死信等 |

常见组合：

```text
durable=true, exclusive=false, auto-delete=false   # 长期业务队列
durable=false, exclusive=true, auto-delete=true    # 临时 RPC 回复队列
durable=true, exclusive=false, auto-delete=true    # 临时但可恢复的队列（少见）
durable=false, exclusive=false, auto-delete=false  # 临时队列，重启即消失
```

不要声明 `exclusive=true` 又 `durable=true`：真实 RabbitMQ 会接受这个组合，但它是无意义的——独占队列随声明连接删除，durable 不能让它活过连接；Mini 为了强调语义会直接拒绝。生产代码应把这两个标志视为互斥意图。

## FIFO 的边界

经典队列在“同一优先级、同一投递路径、没有重投”的前提下近似 FIFO。以下情况会打破严格顺序：

- 消息被 nack/requeue 后重新入队，可能出现在队头附近；
- 多个消费者并发处理，完成顺序不等于入队顺序；
- 优先级队列会先投高优先级；
- Quorum Queue 在 leader 切换、重试时可能改变可见顺序；
- 生产者多线程发布本身没有全局顺序。

因此“RabbitMQ 保证 FIFO”只在很窄的条件下成立。如果业务要求严格顺序，需要：

1. 单一队列、单一消费者（或按分区键分片后每片单消费者）；
2. 不使用优先级和 requeue；
3. 生产者串行化或按 key 分区；
4. 消费失败时不要重排到队尾，而是暂停/死信/人工处理；
5. 明确“顺序”是全局还是每个实体。

很多团队以为“一个队列 + 多个消费者”仍然有序，这是错误的：消费者 A 处理慢、B 处理快，完成顺序必然乱。

## 优先级队列

优先级通过 `x-max-priority` 开启，消息用 `priority` 属性设置，取值通常 0～255，数值越大越先投递。声明：

```python
broker.declare_queue("tasks", arguments={"x-max-priority": 10})
broker.publish("", "tasks", b"low", properties={"priority": 1})
broker.publish("", "tasks", b"urgent", properties={"priority": 9})
```

行为要点：

- classic queue 通过 `x-max-priority` 支持 0–255 多级优先级；
- Quorum Queue 的优先级模型分版本：**4.0–4.2** 是 normal/high 两档（priority 0–4 为 normal，大于 4 为 high，按“高:普通约 2:1”调度，避免 normal 饥饿）；**4.3+** 是 32 级严格优先级（0–31），总是启用，`x-max-priority` 只属于 classic、Quorum 会忽略/拒绝，无优先级按 4 处理；
- 4.3 的严格优先级没有反饥饿插队，持续高优先级流量可能让低优先级长期得不到处理；4.0–4.2 的两档模型则用 2:1 比例保证 normal 有进展；
- 重投（requeue）的消息在 Quorum 中不保留原优先级，可能改变后续投递顺序；`basic.return` 是退回给发布者，不是 requeue，不参与这里讨论；
- 优先级会带来每个优先级一个内部队列/额外内存开销，范围越大成本越高；
- 低优先级消息可能长期饥饿；
- 预取中的消息已经离开 ready 队列，提升优先级不会抢占已投递消息；
- classic 队列的消息 requeue 后按原优先级重新入队，可能插到低优先级消息前面；Quorum 不保留原优先级，见上一条。

Mini 用堆实现优先级：`(-priority, seq, expires_at, message)`。负号让高优先级成为最小堆顶，`seq` 保证同优先级 FIFO。`tests/test_queue.py` 覆盖排序、clamp、同优先级 FIFO 和非法优先级。

优先级在 4.0–4.2 与 4.3+ 的 Quorum 上行为不同：前者只有 normal/high 两档，后者是 32 级严格优先级且 `x-max-priority` 被忽略。想知道某个版本到底怎么排序，不要只读结论；按[附录 W](../appendices/W-4x-feature-labs.md)的实验 1 在一次性集群上发 `0/4/9/31` 四条消息，记录实际消费顺序和版本指纹。

## TTL：两种时钟

RabbitMQ 的 TTL 分两类：

1. **消息 TTL**：`x-message-ttl`（队列级，毫秒）或消息属性 `expiration`（每消息，毫秒字符串）。取两者较小值。
2. **队列 TTL**：`x-expires`（毫秒），队列在无消费者且长时间未使用时过期删除。

消息 TTL 的经典语义是：**消息到达队头时检查是否过期**。如果队头消息未过期，后面的消息即使已经超过 TTL，也可能继续等待。这是为了防止每个消息都做定时器。实践含义：

- 不能把 TTL 当成精确的定时投递；
- 大量不同 TTL 的消息可能造成队头阻塞；
- 需要精确定时/延迟投递时，4.4+ 的 Quorum 可用 `x-opt-delivery-time`，4.3+ 的失败重试可用原生 `x-delayed-retry-*`；跨版本/跨云仍可用 TTL + DLX 的分层队列或外部调度服务。延迟交换机插件依赖 Mnesia，在 4.3+ 已不可用，不要作为新系统方案。

Mini 为了可测试性，在每次 get/dispatch 时扫描所有 ready 消息并淘汰过期项，这与 RabbitMQ 的“队头过期”略有差异。教程在代码注释和第 25 章明确标注这个偏差。

TTL 为 0 表示消息“一入队就过期”；但如果消费者立即可用，它仍可能被直接投递。只有未能立即投递时，才会在队头检查中过期并进入 DLX。不要用 TTL=0 模拟“一定不投递”。`tests/test_queue.py::test_zero_ttl_dead_letters_immediately` 覆盖 Mini 的无消费者场景。

## 长度与字节上限

两个独立的上限：

- `x-max-length`：最大消息条数；
- `x-max-length-bytes`：最大消息字节数（按 RabbitMQ 的存储口径，不是仅 body）。

溢出策略 `x-overflow`：

| 值 | 行为 |
|---|---|
| `drop-head` | 删除队头消息，腾出空间（默认） |
| `reject-publish` | 拒绝新消息，publisher confirm 返回 nack |
| `reject-publish-dlx` | 先把新消息死信，再拒绝发布 |

`reject-publish` 与 publisher confirms 配合才有明确反馈；如果没有 confirm，发布者可能不知道消息被拒绝。`drop-head` 会静默丢弃旧消息，必须配合 DLX 或监控，否则会丢业务数据。

Mini 的 `Queue._handle_overflow_locked` 实现了三种策略，并用 `tests/test_queue.py` 覆盖 drop-head、reject-publish、reject-publish-dlx 和字节上限。注意 Mini 的字节估算是 body + 属性 JSON，真实 RabbitMQ 的计数口径不同；容量规划不能直接套用。

## 队列类型

RabbitMQ 4.x 主要队列形态：

### Classic Queue

传统队列，单节点存储。RabbitMQ 4.x 默认使用 classic queue v2 的存储实现，改进了内存和磁盘行为。classic 队列不支持跨节点复制；节点故障时，未复制消息可能丢失。它适合：

- 低延迟、单节点可接受风险的场景；
- 大量临时队列；
- 0–255 多级优先级队列（Quorum 在 4.0–4.2 只有两档，4.3+ 是 32 级但没有 classic 的 `x-max-priority` 参数）；
- 与旧版本/插件兼容性要求高的场景。

### Quorum Queue

基于 Raft 的复制队列，要求 durable、非 exclusive，通常至少 3 节点。它提供多数派持久化、leader 故障转移、delivery-limit 等能力，适合关键业务。限制包括：

- 优先级模型与 classic 不同：4.0–4.2 为 normal/high 两档，4.3+ 为 32 级严格优先级，均不使用 classic 的 `x-max-priority`；
- 资源开销高于 classic；
- 某些参数/插件不兼容；
- 需要正确的集群和分区处理。

### Stream

追加日志，消息保留、消费者维护 offset、不按 ack 删除。适合高吞吐、可重放、多消费者独立读取的场景。Stream 的语义与队列不同，第 12 章展开。

选择队列类型时，先问“丢失一条消息的代价”和“是否需要重放”，再谈吞吐。不要默认所有队列都用 quorum；大量低价值临时队列用 quorum 会浪费资源。

## 单活消费者（Single Active Consumer）

设置 `x-single-active-consumer=true` 后，同一队列同时只有一个消费者处于 active 状态，其他消费者待命。active 消费者取消或断开后，broker 选择下一个。它适合：

- 需要严格单实例处理的定时任务；
- 避免多消费者并发导致的状态竞争；
- 主备式消费。

注意：

- 它不保证全局唯一（多个队列仍可并行）；
- 待命消费者不接收消息，但仍占用连接资源；
- 故障切换需要时间，切换期间消息可能积压；
- 业务仍需幂等，因为切换可能发生在消息处理中。

Mini 在 `Queue.add_consumer` 和 `remove_consumer` 中实现 active 标志与接管，测试见 `tests/test_queue.py::test_single_active_consumer`。

## 队列 TTL、自动删除与排他

`x-expires` 统计“没有消费者且未被使用”的时间。它适合自动清理临时队列，但要注意：

- 有消费者时不会过期；
- 声明/绑定/拉取是否刷新“使用时间”依版本和参数而定；
- 队列过期删除会丢弃消息；
- 不要用它替代业务层面的数据保留策略。

auto-delete 与 exclusive 的区别：

- auto-delete 在最后一个消费者取消后触发；
- exclusive 绑定到声明连接，连接关闭即删除；
- 两者可以组合，但语义叠加，容易让其他连接“找不到队列”。

Mini 的 `Queue.expired_by_inactivity` 和 `Broker.maintenance` 提供后台过期；`tests/test_broker.py` 覆盖 auto-delete exchange，`tests/test_queue.py` 覆盖队列不活跃判断。

## 队列容量与积压

队列不是无限缓冲。积压会带来：

- 内存上涨，触发 paging/磁盘换入换出；
- 磁盘占用，触发磁盘告警；
- 消费者延迟增加；
- 发布被阻塞；
- 恢复时大量重复投递。

容量估算至少考虑：

```text
所需容量 ≈ 峰值到达率 × 可接受积压时长 × 平均消息大小 × 副本因子
```

例如 5,000 msg/s、允许积压 10 分钟、平均 2 KB、3 副本：

```text
5000 × 600 × 2KB × 3 ≈ 18 GB
```

这还没算索引、段文件、内存开销和突发系数。实际应留 2～3 倍余量，并用真实消息采样校准。

## Mini 的队列实现

`mini_rabbitmq/queue.py` 把队列拆成：

- 堆：优先级、序号、过期时间、消息；
- 未确认表：delivery tag → Delivery；
- 消费者表：consumer tag → ConsumerHandle；
- 统计：published/delivered/acked/redelivered/dead_lettered 等；
- 死信回调：通过 broker 交给 vhost 路由。

关键不变量：

1. 未 ack 的消息不在 ready 堆中，但计入 unacknowledged；
2. ack 后必须释放 broker 的 delivery tag 映射；
3. requeue 必须标记 redelivered，并保留原优先级；
4. 过期消息不能再次投递；
5. 消费者取消不等于未确认消息立即重投，channel 关闭才统一处理。

这些不变量在 `tests/test_queue.py` 中有对应测试。阅读代码时建议先看 `enqueue`、`dispatch`、`ack`、`nack` 四个入口。

## 队列类型决策树

不要把“队列类型”当成部署细节。它决定了复制、顺序、重放、优先级和资源成本。

```text
需要按 offset 重放历史/多消费者独立读取吗？
├── 是 → Stream / Super Stream
└── 否
    ├── 消息不能丢，节点故障要自动切换吗？
    │   ├── 是 → Quorum Queue
    │   └── 否
    │       ├── 需要 0–255 多级优先级/大量临时队列/极低延迟？
    │       │   ├── 是 → Classic Queue
    │       │   └── 否 → Classic（默认）或 Quorum（按重要性）
    └── 队列是临时的（RPC 回复、连接级）？
        ├── 是 → exclusive + auto-delete（classic）
        └── 否 → durable classic/quorum
```

补充判断：

- 关键业务、不能接受节点故障丢消息 → Quorum；
- 低价值、短生命周期、大量临时队列 → Classic；
- 日志/遥测/事件溯源、需要重放 → Stream；
- 需要 classic 的 0–255 级优先级参数 → Classic；Quorum 4.0–4.2 只有两档，4.3+ 有 32 级但不用 `x-max-priority`；
- 需要跨地域复制 → 不要用 Quorum 跨地域，考虑 Federation/Shovel；
- 队列数量巨大但消息价值低 → 先评估元数据/内存成本，再决定是否 Quorum。

## RabbitMQ 4.x 的队列行为变化

写队列代码时，以下 4.x 行为必须知道（详见[附录 I](../appendices/I-version-migration.md)）：

| 变化 | 影响 |
|---|---|
| Classic queue v1 移除，只保留 v2 | 存储/内存/恢复行为变化；`classic_queue.default_version=1` 会导致启动失败 |
| Lazy 模式被忽略 | `x-queue-mode` 不再生效；classic 默认已接近 lazy |
| 镜像队列移除 | `ha-mode` 等 policy 不再提供副本；关键队列要迁 Quorum/Stream |
| Transient 非排他队列被弃用/拒绝 | 老客户端可能声明失败；改用 durable 或 exclusive |
| Quorum 默认 `delivery-limit=20` | 4.3+ 的 `basic.nack`/`released` 主要增加 acquired-count，不消耗 delivery-limit；`reject`/`delivery-failed` 才消耗。不要把所有重试都当成会触发 delivery-limit |
| Quorum 优先级模型演进 | 4.0–4.2 两档（2:1）；4.3+ 32 级严格优先级，且不使用 `x-max-priority` |
| 默认 `max_message_size=16 MiB` | 大消息发布会失败 |
| Global QoS 弃用/默认拒绝 | 只用 per-consumer prefetch |

这些变化不会在编译期报错，升级前必须扫描拓扑和客户端声明。

## 消息大小与 max_message_size

`max_message_size` 是整条消息 body 的上限，和 `frame_max`（单帧大小）不是一回事：

```text
frame_max = 131072          # 单帧最多约 128 KiB，消息会自动拆成多个 BODY 帧
max_message_size = 16777216 # 整条消息最多 16 MiB（4.0 默认）
```

实践：

- 超过 `max_message_size` 的发布会失败，客户端可能只看到连接/信道关闭；
- 大消息会放大内存、磁盘、Quorum 复制和网络成本；
- 图片/视频/大文档放对象存储，消息只传引用；
- 如果确实需要大消息，显式调大上限，并重新做容量/延迟压测；
- 不要用“拆成多条消息”逃避大小限制，除非业务能处理分片/重组和部分失败。

## 用 Little 定律校验积压估算

除了“峰值 × 积压时长 × 消息大小”，还可以用排队论做 sanity check：

```text
L = λ × W
L = 队列中的平均消息数
λ = 平均到达率（msg/s）
W = 平均停留时间（s）
```

例子：到达 2,000 msg/s，平均停留 30 s，则平均积压约 60,000 条。如果消息平均 2 KB，约 120 MB（未计副本/索引）。当 W 从 30 s 涨到 300 s，积压和存储会线性放大 10 倍。

这能解释为什么“消费者只慢了一点”会迅速变成磁盘告警：排队系统的停留时间对利用率非常敏感。利用率接近 100% 时，W 会非线性上升。容量规划要留出消费者余量，而不是按“平均处理时间刚好等于到达间隔”设计。

## 本章小结

队列是有限状态机：durable/exclusive/auto-delete 决定生命周期，FIFO 只在窄条件下成立，优先级会牺牲公平性，TTL 有队头检查语义，长度上限需要明确溢出策略，队列类型决定复制与重放能力。积压不是“等消费者追上来”这么简单，它会触发内存、磁盘和流控连锁反应。

下一章聚焦发布侧可靠性：publisher confirms、mandatory return、事务和持久化链条，解释“消息到底什么时候算发成功”。

---

[上一章：第 05 章 交换机、绑定与路由算法](05-exchanges-routing.md) | [返回目录](../README.md) | [下一章：第 07 章 发布可靠性：confirms、mandatory return 与事务](07-publisher-reliability.md)
