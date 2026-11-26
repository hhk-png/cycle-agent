# 附录 I RabbitMQ 4.x 版本演进、破坏性变更与升级迁移

本附录回答一个在真实项目中比“怎么发消息”更容易出事的问题：**从旧版本升到 RabbitMQ 4.x，到底有哪些行为变了，怎么安全迁移**。版本细节会继续演进，本附录以 RabbitMQ 4.x 的公开文档和发布说明为基线，给出可执行的判断框架与检查清单；具体补丁版本的行为请以目标版本的 release notes 为准。

## 为什么单独用一篇附录讲版本

RabbitMQ 3.x 到 4.x 不是一次“小版本升级”。它同时改变了：

- 经典队列的存储实现（CQv1 → CQv2）；
- 高可用队列的默认选择（镜像队列 → Quorum Queue）；
- 元数据存储（Mnesia → Khepri）；
- 核心协议（AMQP 1.0 从插件变成核心协议）；
- 一批长期弃用项的最终处置（lazy 模式、transient 非排他队列、global QoS）；
- 默认消息大小上限（128 MiB → 16 MiB）；
- Quorum Queue 的投递限制与优先级语义。

这些变化不会在编译期报错，却会在升级后以 406、消息被拒、消费者卡住、队列不可用的形式出现。升级前必须把“版本行为”当成一次架构变更来管理。

## 版本时间线（概念级）

| 版本 | 关键变化 | 升级含义 |
|---|---|---|
| 3.8 | Quorum Queue 可用、feature flags 机制引入 | 老集群常见起点 |
| 3.9 | 镜像队列开始弃用、Streams 引入 | 应开始规划去镜像 |
| 3.10 | `reject-publish-dlx`、更多 Quorum 能力 | 溢出策略可更细 |
| 3.11 | 性能与 Streams 改进 | 仍以 3.x 语义为主 |
| 3.12 | classic queue v2 可用、lazy 模式被忽略 | 队列内存行为改变 |
| 3.13 | 4.0 前的最后一个 3.x 系列、AMQP 1.0 插件成熟 | **升级 4.0 的必经跳板** |
| 4.0 | 镜像队列移除、CQv1 移除、AMQP 1.0 核心化、默认 16 MiB、Quorum 两档优先级、delivery-limit 默认 20 | 破坏性最大的一步 |
| 4.1 | Khepri 可显式启用、AMQP 1.0 over WebSocket 等 | 新能力，默认仍是 Mnesia |
| 4.2 | 新集群默认 Khepri、`khepri_db` feature flag 稳定、Stream SQL 过滤、AMQP 1.0 Direct Reply-To | 新部署的元数据默认值改变 |
| 4.3 | **Khepri 成为唯一元数据存储（Mnesia 移除）**、Quorum 32 级严格优先级、原生延迟重试、队列压缩、按消费者超时、内置 `x-modulus-hash`、global QoS/transient 非排他队列/CQv1 拒绝 | 当前主流生产基线；升级路径收紧 |
| 4.4 | Quorum 原生 `x-opt-delivery-time` 定时投递、原生 STOMP、AMQP 1.0 连接唯一性、CQv2 共享存储 v2（`.sqs`）、Erlang 29 | 2026-10 发布；有单向升级门 |

不要把这张表当成“必须逐版本升级”的命令，也不要假设可以任意跳级。RabbitMQ 官方给的是**路径矩阵**，不是单一的相邻链。以 4.4 为例，受支持的路径包括：

- 4.3.x → 4.4.x；
- 4.2.x → 4.4.x；
- 3.13.x → 4.2.x → 4.4.x；
- 4.0.x → 4.2.x → 4.4.x。

不支持的典型跳法是 3.13 → 4.4 直达。4.3 只支持从 4.2.x 原地升级；4.3.7+ 是 4.3 与 4.4 元数据互操作的条件（尤其同时升级 Erlang/OTP 28/29 时），不是所有 4.4 升级的通用硬前提。即使路径受支持，跳过 feature flag 步骤、跳过预发演练仍然高风险；关键业务更推荐“新建目标集群 + 蓝绿迁移”，而不是一路原地升级。

## 4.0 的破坏性变更逐项拆解

### 1. 经典镜像队列被移除

3.x 的 `ha-mode`、`ha-params`、`ha-sync-mode`、`ha-sync-batch-size`、`ha-promote-on-shutdown`、`ha-promote-on-failure` 等 policy 在 4.0 中不再生效。Classic queue 变成单副本队列：可以从任意节点发布/消费，但队列数据只属于一个节点。

影响：

- 原本依赖镜像实现“节点故障不丢消息”的拓扑，升级后不再有跨节点副本；
- `ha-mode` policy 不会报错，而是被忽略，容易造成“以为还有副本”的错觉；
- 需要在升级前把关键队列迁移到 Quorum Queue 或 Stream。

排查方法：

```bash
rabbitmqctl list_queues name policy effective_policy_definition mirror_pids
rabbitmqctl list_policies -p /app
```

如果 `effective_policy_definition` 里还有 `ha-mode`，或者 `mirror_pids` 非空，就说明这个队列仍依赖旧语义。

### 2. CQv1 存储被移除

4.0 只保留 classic queue v2。配置 `classic_queue.default_version = 1` 会导致节点无法启动。升级前要确认没有显式固定 v1 的配置；升级后队列的磁盘布局、内存占用和恢复行为都会变化。

这不是“换个存储引擎”这么简单：CQv2 更依赖消费速率来决定内存中的消息量，积压时的磁盘 IO 模式也不同。升级后要重新做一次积压/恢复压测。

### 3. Lazy 模式被忽略

从 3.12 起，`x-queue-mode` 就被忽略；classic queue 默认行为已经接近 lazy：只在小窗口内保留消息在内存，其余写盘。4.x 中继续传 `x-queue-mode` 不会生效。

迁移建议：

- 从 policy 和队列参数中删除 `queue-mode`；
- 不要再用“lazy queue”作为容量规划的假设；
- 用 `x-max-in-memory-length` / `x-max-in-memory-bytes`（若目标版本支持）或队列类型选择来控制内存。

### 4. Transient 非排他队列被弃用/拒绝

“非 durable 且非 exclusive”的 classic queue 在 3.x 已被标记弃用；4.x 逐步收紧，4.3 起默认拒绝（可通过 `deprecated_features.permit.transient_nonexcl_queues = true` 临时放开，但不建议长期依赖）。

替代方案：

- 需要长期存在：用 durable queue；
- 需要随连接销毁：用 exclusive queue；
- 需要自动清理：用 `x-expires`（队列 TTL）或 auto-delete；
- 临时 RPC 回复队列：exclusive + auto-delete。

很多老客户端（包括某些 Celery/Kombu 配置）会声明 transient 非排他队列，升级后会在声明阶段收到 channel error。升级前要用真实客户端跑一遍拓扑声明。

### 5. Global QoS 被弃用并最终默认拒绝

RabbitMQ 对 `basic.qos` 的 `global` 标志有自己的解释：`global=false` 是**每消费者**预取，`global=true` 是**信道级**共享预取。它不是 AMQP 规范原义，且信道级预取需要跨队列协调，性能更差。

4.0 将 global QoS 标记为弃用；4.3 起默认拒绝，Quorum/Stream 队列会以 channel error 回应。正确做法是：

- 只用 per-consumer prefetch（`global=false`）；
- 如果确实需要“整条信道最多 N 条未确认”，在应用层做信号量/令牌桶；
- 升级前搜索客户端代码中的 `basic_qos(..., global_=True)` / `basicQos(n, true)`。

### 6. 默认最大消息大小从 128 MiB 降到 16 MiB

4.0 的 `max_message_size` 默认值是 16 MiB。超过上限的发布会失败，而不是被分片成“无限大消息”。这不是 `frame_max`：`frame_max` 控制单帧大小（常见 128 KiB），`max_message_size` 控制整条消息的 body 上限。

影响：

- 把图片、PDF、大 JSON 直接塞进消息的系统会在升级后突然失败；
- 客户端可能只看到 connection/channel close，而不是清晰的业务错误；
- 需要改配置或改架构（对象存储 + 引用）。

```ini
# rabbitmq.conf
max_message_size = 33554432   # 32 MiB，按需设置
```

更推荐的做法是把大文件放对象存储，消息只传 URI、校验和、版本号。

### 7. AMQP 1.0 成为核心协议

4.0 起 AMQP 1.0 默认启用，`rabbitmq_amqp1_0` 插件变成 no-op（只为兼容旧部署脚本）。AMQP 1.0 客户端直接以 exchange 为 target、queue 为 source，不再经过 0-9-1 代理。

影响：

- 新系统可以认真考虑 AMQP 1.0，而不是把它当“实验插件”；
- 0-9-1 与 1.0 的确认、属性、死信语义仍需映射，不能假设等价；
- 升级前如果脚本里 enable/disable 该插件，行为不变，但不要再依赖它控制协议开关。

### 8. Quorum Queue 的默认与语义变化

4.0 的关键变化：

- 新声明的 Quorum Queue 默认 `delivery-limit = 20`；超过限制的消息会被丢弃或死信；
- Quorum Queue 支持**两档优先级**：消息 priority 0–4 为 normal，>4 为 high；不需要 `x-max-priority`；高:普通约 2:1 的比例投递，避免 normal 饥饿；
- Quorum Queue 仍不支持 classic 的 `x-max-priority` 多级优先级语义；
- 仍要求 durable、非 exclusive。

这意味着“Quorum 不支持优先级”这句话在 4.x 已经不准确。准确说法要分版本：

| 版本 | Quorum 优先级模型 | 参数/行为 |
|---|---|---|
| 4.0–4.2 | normal/high 两档，2:1 加权调度 | 无需 `x-max-priority`；无优先级视为 4（normal）；避免 normal 饥饿 |
| 4.3+ | 32 级严格优先级（0–31） | 总是启用；`x-max-priority` 只属于 classic，Quorum 会忽略/拒绝；无优先级视为 4；**没有反饥饿插队**，持续高优先级会压住低优先级 |

4.3 的严格优先级是“更高优先级先投递”，不是 4.0 的“按比例混合”。它解决了两档模型表达力不足的问题，但也把饥饿风险交还给了业务：如果你的高优先级流量持续不断，低优先级消息可能长期得不到处理。需要公平性时，仍应拆队列、限流或使用独立的低优先级消费者。重投/return 的消息不保留原优先级，会按返回顺序重新入队，这一点在“重试不能改变优先级语义”的设计里必须考虑。

### 9. Feature flags 成为升级闸门

RabbitMQ 用 feature flags 管理跨版本的元数据/行为兼容。升级前必须在 3.13 上启用所有可用 flag：

```bash
rabbitmqctl list_feature_flags
rabbitmqctl enable_feature_flag all
```

注意事项：

- flag 一旦启用通常不能回退；
- 升级顺序错误可能导致节点无法加入集群；
- 回滚到旧版本前必须确认没有启用旧版本不认识的 flag；
- 4.2 的 `khepri_db` flag 是稳定 flag，`enable_feature_flag all` 会启用它。

### 10. Khepri 从“新集群默认”到“唯一存储”

- 4.0：Khepri 可用，但默认仍是 Mnesia；
- 4.1：通过 `khepri_db` feature flag 显式启用；
- 4.2：**新部署**默认 Khepri；从旧版本升级的集群保留原存储，需要显式启用 flag 才迁移；
- 4.3：**Khepri 是唯一支持的元数据存储，Mnesia 被完全移除**；升级前必须在 4.2 上启用 `khepri_db` 并完成迁移，迁移不可逆；
- 4.4：元数据互操作只保证 4.3.7+ 与 4.4.x；从更早的 4.3 补丁升级时，先到 4.3.7。

Khepri 基于 Raft，和 Quorum Queue/Stream 使用同一套共识思路，官方给出的收益包括更快的大量 vhost/队列启动、更好的恢复能力。但迁移不是无风险的：

- 升级前备份 definitions 和节点数据；
- 先在预发集群启用并演练重启/滚动升级；
- 大集群（十万级 classic queue）要验证性能回归；
- 不要把“新集群默认 Khepri”误当成“老集群已经迁移”；
- 4.3 起元数据写入也要求集群多数派在线：少数派节点无法声明交换机/队列，这会把“部分节点可用”从“还能读”变成“不能改拓扑”；
- `cluster_partition_handling` 等 Mnesia 时代的多数派配置在 4.3 中会被接受但**不生效**，故障语义统一由 Raft 决定，升级后应从配置里清理，避免误以为还在控制分区行为。

## 4.3/4.4 带来的架构级变化

4.0 是“把旧世界拆掉”，4.3/4.4 更像“把过去必须靠插件或外部系统实现的能力收回内核”。对架构师来说，以下变化会直接影响拓扑设计。

### 1. Quorum 原生延迟重试（4.3）

4.3 的 Quorum Queue 可以在队列内部把失败消息“放置”一段时间再重投，不需要 TTL 桶，也不需要重写消息：

```text
x-delayed-retry-type: disabled | all | returned | failed
x-delayed-retry-min:  30000    # 毫秒
x-delayed-retry-max: 120000    # 毫秒
```

延迟按线性退避计算：`min(min_delay × delivery_count, max_delay)`。例如 min=30s、max=120s，重试间隔为 30s、60s、90s、120s 封顶。它只处理“被拒绝/被退回”的消息，不是通用定时投递：

- `failed`：只处理 delivery-count 增加的消息，例如 `basic.reject`、AMQP `rejected`、`modified` 且 `delivery-failed=true`；
- `returned`：只处理 acquired-count 增加的消息，例如 `basic.nack`、AMQP `released`；
- `all`：覆盖所有退回消息，不论计数是否增加。

4.3 同时把这两个计数分开：nack/released 主要增加 acquired-count，不消耗 `delivery-limit`；只有 failed 路径才消耗。因此“反复 nack requeue 一定会触发 delivery-limit”在 4.3+ 不成立。精确语义以目标版本官方文档为准，实验见[附录 W](W-4x-feature-labs.md)。AMQP 1.0 还可以在 `modified` outcome 里用 `x-opt-delivery-time` 覆盖下一次投递时间，适合尊重下游 `Retry-After`。

这条能力把第 09 章的重试拓扑从“必须自建 TTL 桶”变成“可以在队列内声明退避策略”。但它不替代业务重试状态机：最大次数、毒消息隔离、parking lot、审计仍然要在应用层设计。

### 2. Quorum 原生定时投递（4.4）

4.4 起，Quorum Queue 支持按消息级时间戳延迟可见：

- AMQP 0-9-1：消息 header `x-opt-delivery-time`，值为 Unix epoch **毫秒**，类型必须是 long；
- AMQP 1.0：作为 message annotation 传递；
- 消息会立即路由/入队，但在指定时间之前不可被消费；
- **classic queue 不支持**：同样的 header 发到 classic 队列会立即投递；4.3 及更早版本也不识别。

这与 4.3 的“失败后延迟重试”不同：4.4 是通用的“到点才投递”，可以用于预约任务、延迟通知、超时补偿。注意：老插件的 `x-delay` 在 4.4 的 Quorum Queue 上**不会生效且不会报错**，消息会立即投递；字符串类型的 `x-opt-delivery-time` 也会静默失效。从延迟插件迁移时这是最危险的静默行为变化，实验与反例见[附录 W](W-4x-feature-labs.md)。

### 3. 延迟交换机插件的终局

`rabbitmq-delayed-message-exchange` 依赖 Mnesia 存储延迟消息。Mnesia 在 4.3 被移除后，该插件无法在 4.3+ 工作，官方仓库已归档、不再维护。它还只支持单副本存储，队列/节点故障时延迟消息的可用性有限。

因此：

- 3.x/4.0–4.2 的存量系统可以继续用，但应规划迁移；
- 4.3+ 应使用 Quorum 原生延迟重试（4.3）或 `x-opt-delivery-time`（4.4）；
- 需要跨重启、超大积压、精确调度的场景，仍应使用外部调度服务或 Tanzu 商业版的 delayed queue；
- 不要在新系统里继续把 `x-delay` 当作跨版本可移植能力。

### 4. 队列压缩、优先级与超时（4.3）

- **Quorum 队列压缩**：Raft 段中部分失效的数据会被压缩，减少“队列很短但磁盘占用很高”的问题，降低磁盘告警和重启恢复成本；
- **32 级严格优先级**：见上一节，表达力增强但饥饿风险回归；
- **按消费者超时**：`consumer_timeout` 从信道级迁移到 Quorum 队列/消费者级，可通过队列参数、policy 或消费者参数配置，统一覆盖 AMQP 0-9-1、AMQP 1.0 和 MQTT；默认仍是 30 分钟。超时会关闭消费者而不是无限挂起，避免“消费者在线但永不 ack”拖住队列；
- **内置 `x-modulus-hash` 交换机**：按 routing key 哈希取模分发到绑定队列，拓扑不变时路由稳定，适合“按订单 ID 分区 + 每分区单活消费者”的顺序方案；扩容/缩容会重排几乎所有 key，这一点不如一致性哈希插件平滑。

### 5. 4.4 的运维单向门

4.4 引入 classic queue 共享存储 v2（文件扩展名 `.sqs`）。节点一旦写入 v2 文件，就**不能降级到 4.3.x 或更早版本**。同时：

- AMQP 0-9-1 的 `x-amqp-1.0-properties` 等桥接 header 不再被解析，3.13 及更早写入的跨协议消息可能丢属性；
- Stream 的 `x-basic-delivery-mode`、`x-basic-priority`、`x-basic-expiration` 注解不再被解析；
- OIDC discovery 校验更严格（issuer/HTTPS），Cloud Foundry UAA 等需要临时放宽；
- `GET /api/auth/hash_password/{password}` 改为 `POST /api/auth/hash_password`，依赖该接口的自动化会失败；
- 原生 STOMP 不再经过内部 AMQP 0-9-1 客户端，连接数/进程模型变化，监控面板要重新校准。

这些不是“补丁级”变化。升级到 4.4 前要把回滚边界写清楚：应用回滚容易，数据格式回滚很难。

### 6. 版本能力对照

| 能力 | 3.13 | 4.0–4.2 | 4.3 | 4.4 |
|---|---|---|---|---|
| 经典镜像队列 | 弃用 | 移除 | 移除 | 移除 |
| Khepri | 实验 | 可用/默认（4.2） | **唯一** | 唯一 |
| Quorum 优先级 | 无 | 两档（2:1） | 32 级严格 | 32 级严格 |
| 延迟重试 | TTL/DLX/插件 | TTL/DLX/插件 | **原生** | 原生 |
| 定时投递 | TTL/DLX/插件 | TTL/DLX/插件 | TTL/DLX | **`x-opt-delivery-time`** |
| 延迟交换机插件 | 可用 | 可用 | **不可用** | 不可用 |
| global QoS | 可用（不推荐） | 弃用 | 拒绝 | 拒绝 |
| transient 非排他队列 | 弃用 | 弃用 | 拒绝 | 拒绝 |
| 最大消息 | 128 MiB | 16 MiB | 16 MiB | 16 MiB |

这张表是“做架构决策时的版本坐标”，不是逐条 API 文档。具体补丁版本可能调整默认值或参数名，落地前用目标版本的管理 API 和 `rabbitmqctl list_queues` 实测。

## 升级前的检查清单

### 版本与兼容性

- [ ] 目标版本与 Erlang/OTP 版本匹配；
- [ ] 所有插件都有目标版本兼容版本；
- [ ] 客户端库支持目标协议/确认语义；
- [ ] 云厂商/托管服务支持目标版本与所需插件；
- [ ] 阅读目标版本 release notes 和升级指南。

### 拓扑与配置

- [ ] 导出 definitions 并纳入版本控制；
- [ ] 搜索并移除 `ha-mode` 等镜像 policy；
- [ ] 搜索并移除 `x-queue-mode`；
- [ ] 找出 transient 非排他队列并改造；
- [ ] 找出 `global=true` 的 QoS 调用；
- [ ] 检查 `max_message_size` 与业务最大消息；
- [ ] 检查 Quorum 队列的 `delivery-limit` 与 DLX；
- [ ] 检查 `classic_queue.default_version` 等已移除配置。

### 集群与数据

- [ ] 3.13 上启用所有 feature flags；
- [ ] 集群健康、无分区、无告警；
- [ ] 每节点磁盘余量足够完成恢复/重建；
- [ ] 备份 definitions、证书、配置、必要的消息归档；
- [ ] 预发环境完整演练升级与回滚；
- [ ] 准备降级边界（哪些 flag 不可回退）。

### 客户端与业务

- [ ] 客户端重连/拓扑恢复逻辑在升级窗口可用；
- [ ] 消费者能处理 channel error 并重建；
- [ ] 关键业务有幂等/对账；
- [ ] 有升级期间的发布/消费降级方案；
- [ ] 监控能覆盖升级后的新指标名/行为变化。

## 三种迁移路径

### 路径 A：原地滚动升级

适合：拓扑简单、没有镜像队列依赖、客户端兼容、可接受短时间节点重启。

步骤（以最保守的 3.13 → 4.0 → 4.1 → 4.2 → 4.3 → 4.4 为例；如果官方矩阵允许 4.2 → 4.4，可省去 4.3 的原地一跳，但 4.2 上的 Khepri 迁移不能省）：

1. 在 3.13 最新补丁上启用全部 feature flags；
2. 升级到 4.0 的最新补丁：逐节点 stop_app → 升级软件 → start_app，每步等待集群健康；
3. 依次完成 4.1、4.2 的原地升级；在 4.2 上启用 `khepri_db` 并等待元数据迁移完成；
4. 升级到 4.3.7+，确认 Khepri 工作正常、无 Mnesia 残留依赖；
5. 升级到 4.4.x，观察 CQv2 `.sqs` 文件生成与性能；
6. 全部节点升级后再升级客户端/插件；
7. 保留回滚窗口，但注意 feature flag 和 Khepri 迁移不可逆。

这不是“多此一举”：4.3 只支持从 4.2.x 升级，4.4 的元数据互操作只保证 4.3.7+。但也不要把它误读成“任何跨版本都禁止”：4.2 → 4.4 是官方支持的路径，可以少一次原地升级；真正不支持的是 3.13 → 4.4 这类矩阵外的直达。feature flag 和 Khepri 迁移仍然不能跳。

风险：升级过程中 Quorum 队列会经历 leader 切换；发布者会看到 confirm 超时；消费者会重连。必须能在业务层处理“结果未知”。4.2→4.3 的 Khepri 迁移还要求集群多数派在线，迁移窗口内不要做节点维护。

### 路径 B：蓝绿迁移（推荐给关键业务）

适合：不能接受原地升级风险、需要同时验证新旧版本、需要迁移队列类型。

步骤：

1. 新建目标版本集群，导入 definitions（可裁剪/改造）；
2. 用 Federation 或 Shovel 从旧集群复制/转发到新集群；
3. 生产者双写或影子流量；
4. 消费者双读/比对结果；
5. 按业务域逐步切换生产者；
6. 排空旧队列，观察对账；
7. 下线旧集群。

优点：可回滚、可对比、可分批。代价：双倍资源、重复消息、需要幂等和对账。

### 路径 C：队列类型迁移（classic/镜像 → quorum/stream）

这不是“改一个参数”。队列类型不能原地修改，必须：

1. 声明新类型队列；
2. 用 Shovel/Federation 或应用双写把消息搬到新队列；
3. 切换消费者；
4. 验证顺序、重复、DLX、TTL、优先级语义；
5. 删除旧队列。

注意：

- Quorum 的优先级模型与 classic 不同：4.0–4.2 只有两档，4.3+ 有 32 级但不使用 classic 的 `x-max-priority` 参数；依赖 0–255 精细优先级的业务要重新设计；
- Quorum 的 `delivery-limit` 默认 20，可能比旧队列更早死信；
- Quorum 的 TTL、死信、重投顺序与 classic 不完全一致，迁移前要用真实消息验证 `x-death` 和重试拓扑；
- Stream 不是“更快的队列”，消费者模型要重新设计；
- 迁移期间的重复消息必须由消费端幂等消化。

## 升级后的验证清单

- [ ] `rabbitmq-diagnostics -q ping`、`cluster_status` 正常；
- [ ] 无 partitions、无 alarms；
- [ ] 每个 Quorum 队列有 leader、副本数符合预期；
- [ ] 关键队列的 publish/consume/confirm 延迟在 SLO 内；
- [ ] DLQ 没有异常增长；
- [ ] 客户端连接数/channel 数没有异常翻倍；
- [ ] feature flags 状态符合预期；
- [ ] 元数据存储符合目标版本预期（4.2 及以前可能是 Mnesia；4.3+ 必须是 Khepri）；
- [ ] 4.3+ 节点不再引用 Mnesia，旧插件已替换；
- [ ] 4.4 节点已生成 `.sqs` 文件，且团队知道不能降级到 4.3；
- [ ] 延迟消息路径已从 `x-delay` 迁移到 4.3 延迟重试或 4.4 `x-opt-delivery-time`；
- [ ] 备份/监控/告警仍然工作；
- [ ] 回滚边界和降级方案仍可执行。

## 常见升级故障

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 节点启动失败 | `classic_queue.default_version=1` 等已移除配置 | 删除配置，恢复备份 |
| 队列声明 406 | 客户端仍声明 transient 非排他队列/旧参数 | 改客户端或临时放开 deprecated feature |
| 发布被拒 | 消息超过 16 MiB 默认上限 | 提高 `max_message_size` 或改对象存储 |
| 消费者收不到消息 | Quorum `delivery-limit` 提前死信、优先级语义变化 | 检查 DLQ/x-death、调整 limit |
| 写入变慢 | Quorum 复制/磁盘/网络、Khepri 迁移 | 查 leader/副本/磁盘/网络 |
| 集群无法加入 | feature flags 不一致、节点名/DNS/cookie | 对齐 flag、检查网络与节点身份 |
| 插件报错 | 插件未适配 4.x；延迟插件依赖 Mnesia | 升级/替换插件，查兼容矩阵；4.3+ 改用原生延迟 |
| 延迟消息立即投递 | 仍用 `x-delay`，而 4.3+ 插件已不可用/4.4 Quorum 不识别 | 改用 `x-delayed-retry-*` 或 `x-opt-delivery-time` |
| 4.3 启动失败/元数据异常 | 未在 4.2 完成 `khepri_db` 迁移 | 回退到 4.2 完成迁移，或从备份重建 |
| 4.4 无法降级 | CQv2 `.sqs` 文件格式单向 | 在预发验证回滚边界，必要时重建集群 |
| OIDC 登录失败（4.4） | issuer/HTTPS 校验更严格 | 修正 discovery 配置，或临时放宽校验 |
| hash_password 自动化失败（4.4） | GET 改为 POST | 更新脚本/客户端 |
| 回滚失败 | 已启用旧版本不认识的 feature flag / Khepri 迁移不可逆 | 在预发验证回滚边界，必要时重建集群 |

## 一份可执行的升级 Runbook

```text
T-14 天
  - 盘点版本、插件、客户端、拓扑、policy、队列类型
  - 导出 definitions，扫描 ha-mode / x-queue-mode / transient / global QoS
  - 扫描延迟插件、`x-delay`、Mnesia 依赖插件、global QoS 调用
  - 建立预发集群，导入真实拓扑影子流量

T-7 天
  - 在 3.13 预发启用全部 feature flags，按 4.0 → 4.1 → 4.2 演练
  - 在 4.2 启用 khepri_db，验证迁移时间与多数派要求
  - 升级到 4.3.7+，压测 Quorum confirm/消费/积压恢复/压缩
  - 验证 4.4 的 `.sqs` 文件、降级边界、OIDC/API 兼容
  - 准备回滚与降级方案（明确哪些步骤不可逆）

T-1 天
  - 生产启用 feature flags（如尚未启用）
  - 备份 definitions/配置/证书
  - 确认监控、告警、on-call、变更窗口

T 日
  - 按 4.0 → 4.1 → 4.2 → 4.3 → 4.4 逐版本、逐节点升级
  - 每步等待健康再继续；Khepri 迁移时保持多数派在线
  - 观察 confirm p99、积压、DLQ、连接数、分区、插件错误
  - 每步记录时间戳与指标

T+1 天
  - 全量验证业务链路与对账
  - 观察 24 小时，再关闭回滚窗口
  - 复盘并更新 runbook
```

## 与 Mini-RabbitMQ 的关系

Mini 不实现 feature flags、Khepri、CQv2 段文件或真实 Raft，因此不能用来验证升级行为。它能做的是：

- 用 `x-queue-type=quorum` 演示“类型不同、语义不同”；
- 用快照恢复演示“元数据与消息的恢复边界”；
- 用 `tests/test_queue.py::test_quorum_queue_validation` 演示 Quorum 对 durable/exclusive 的约束。

真实升级必须在真实 RabbitMQ 的预发集群上做。把 Mini 的“能启动”当成升级验证，是典型的假绿。

## 进一步阅读（版本事实核对）

- Quorum Queues（4.3）：<https://www.rabbitmq.com/docs/4.3/quorum-queues>
- RabbitMQ 4.3 Highlights：<https://www.rabbitmq.com/blog/2026/04/23/rabbitmq-4.3-release>
- RabbitMQ 4.4 Highlights：<https://blog.rabbitmq.com/blog/2026/10/20/rabbitmq-4.4-release>
- RabbitMQ 4.0 Quorum Queue 新特性：<https://www.rabbitmq.com/blog/2024/08/28/quorum-queues-in-4.0>
- AMQP 1.0（4.0 核心协议）：<https://www.rabbitmq.com/docs/amqp>
- Khepri 启用指南（4.2）：<https://www.rabbitmq.com/docs/4.2/metadata-store/how-to-enable-khepri>
- 4.3 元数据存储（Khepri only）：<https://www.rabbitmq.com/docs/4.3/metadata-store>
- Consumer Prefetch / global QoS：<https://www.rabbitmq.com/docs/consumer-prefetch>
- 4.0 弃用公告：<https://www.rabbitmq.com/blog/2021/08/21/4.0-deprecation-announcements>
- Classic Queues（CQv2/lazy）：<https://www.rabbitmq.com/docs/classic-queues>
- 延迟插件归档说明：<https://github.com/rabbitmq/rabbitmq-delayed-message-exchange>

版本事实会继续演进；升级前以目标版本的 release notes、升级指南和实际预发集群为准。

## 小结

RabbitMQ 4.x 的升级风险主要来自“静默行为变化”，而不是安装失败：镜像队列、CQv1、lazy 模式、transient 队列、global QoS、16 MiB 默认上限、Quorum 优先级与 delivery-limit、AMQP 1.0 核心化、Khepri 默认值与 4.3 的 Mnesia 移除、4.3/4.4 的原生延迟能力，任何一项都可能改变生产行为。安全升级的核心是：按官方路径矩阵升级，先在 3.13 启用 feature flags，在 4.2 完成 Khepri 迁移，扫描并改造弃用项，用预发集群演练，关键业务走蓝绿/双跑，升级后按清单验证。版本会继续演进，方法论不会；具体事实的核查流程、证据分级与官方资料地图见[附录 X](X-fact-check-and-sources.md)，4.3/4.4 的正反实验见[附录 W](W-4x-feature-labs.md)。

---

[返回目录](../README.md) | [上一章：附录 H 实验课与生产上线检查清单](H-labs-production-checklist.md) | [下一章：附录 J 多语言客户端工程手册](J-polyglot-client-cookbook.md)
