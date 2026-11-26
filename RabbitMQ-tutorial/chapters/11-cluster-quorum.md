# 第 11 章 集群、Quorum Queue 与故障转移

> 本章定位：从单机 broker 走向分布式。重点不是背“三节点集群”命令，而是理解 Erlang 集群、Raft 多数派、网络分区和队列类型之间的可靠性契约。

## 集群不是自动高可用

RabbitMQ 集群把多个节点组成一个逻辑 broker：

- 节点之间共享元数据（用户、vhost、交换机、绑定、策略等）；
- 连接可以连到任意节点；
- 队列可以位于某个节点（classic）或被复制（quorum/stream）；
- 管理 UI 能看到整个集群。

但“有集群”不等于“消息不丢”。如果队列是 classic 且只在一个节点，节点故障时该队列不可用/可能丢失。高可用取决于队列类型、副本数、持久化、确认和故障处理策略。

## Erlang 分布式基础

RabbitMQ 用 Erlang/OTP 构建，节点之间通过 Erlang distribution 通信。关键概念：

- **node name**：形如 `rabbit@host1`，主机名解析必须稳定；
- **Erlang cookie**：集群节点共享的认证密钥，泄露等于集群被接管；
- **端口**：4369（epmd）、25672（Erlang distribution）等需要互通；
- **元数据存储**：历史上是 Mnesia；4.0 起 Khepri 可用，4.1 通过 `khepri_db` feature flag 显式启用，4.2 起新部署默认 Khepri，**4.3 起 Khepri 是唯一支持的存储、Mnesia 被移除**。从 4.2 升级到 4.3 前必须启用 `khepri_db` 并完成迁移，且迁移不可逆；4.3+ 元数据写入要求集群多数派在线，少数派节点仍能连但无法声明新拓扑。

集群不是“把 IP 加进去”这么简单。节点名、DNS、防火墙、时钟、cookie、版本、feature flags 都必须一致。容器环境中尤其要固定 hostname 和持久化节点数据，否则重启后节点身份可能变化。

## 加入集群

典型流程（具体命令以版本为准）：

```bash
# 在 node1 上
rabbitmqctl stop_app
rabbitmqctl reset
rabbitmqctl start_app

# 在 node2 上
rabbitmqctl stop_app
rabbitmqctl reset
rabbitmqctl join_cluster rabbit@node1
rabbitmqctl start_app
```

检查：

```bash
rabbitmqctl cluster_status
rabbitmq-diagnostics status
rabbitmq-queues quorum_status <queue>
```

不要把已在生产承载数据的节点随意 `reset`；它会清空节点元数据。加入/移除节点是控制面高风险操作，必须有备份、变更窗口和回滚计划。

## Classic Queue 在集群中的位置

Classic queue 的队列数据属于某一个节点（队列 master/owner）。集群提供：

- 元数据复制；
- 连接路由（客户端连到其他节点也能操作队列）；
- 管理视图；
- 某些故障转移能力。

但不提供队列内容的跨节点复制。节点故障时：

- 该节点上的 classic 队列不可用；
- durable 队列可能在该节点恢复后重新出现；
- 非持久消息可能丢失；
- 依赖该队列的绑定/消费者需要恢复。

经典镜像队列（classic mirrored queues）在 RabbitMQ 4.x 已不再是可用/推荐方案，历史上也被认为在分区和性能上有明显问题。现代高可用应使用 Quorum Queue 或 Stream。

## Quorum Queue 与 Raft

Quorum Queue 是复制队列，基于 Raft 共识：

- 每个队列有一个 Raft group；
- 一个 leader 处理写入和读取协调；
- follower 复制日志；
- 写入需要多数派持久化确认；
- leader 故障时选举新 leader；
- 支持 delivery-limit、dead-letter、TTL、max-length 等；
- 4.0–4.2 支持 normal/high 两档优先级；4.3+ 支持 32 级严格优先级（0–31），但都不使用 classic 的 `x-max-priority` 参数；

多数派公式：

```text
quorum = floor(N / 2) + 1
```

| 节点数 | 多数派 | 可容忍故障 |
|---|---|---|
| 1 | 1 | 0 |
| 3 | 2 | 1 |
| 5 | 3 | 2 |
| 7 | 4 | 3 |

3 节点是最常见的折中；5 节点提高容错但写入延迟和成本更高。

### 写入路径

```text
publisher -> leader append log entry
          -> replicate to followers
          -> majority fsync/ack
          -> commit index advance
          -> publisher confirm
          -> consumers see committed message
```

因此 quorum 的 confirm 是“多数派已持久化”，比 classic 的“单节点接受”更强。代价是网络往返、磁盘 fsync 和 leader 负载。

### 选举与故障转移

leader 失联后，follower 在 election timeout 后发起选举，获得多数票成为新 leader。客户端会看到短暂的不可用/重连；未确认的投递可能重投。Quorum Queue 的 delivery-limit 可以限制毒消息反复投递。

注意：

- 选举期间写入可能失败；
- 少数派节点无法形成 quorum，不能写入；
- 旧 leader 恢复后会被同步/截断未提交日志；
- 消费者需要重连并重新声明拓扑；
- 发布者必须处理 confirm 超时和重试。

Mini 的 `mini_rabbitmq/cluster.py` 在单进程内模拟 Raft：投票、任期、日志复制、多数派提交、leader 故障、节点追赶。它不实现真实网络 RPC、快照安装、成员变更和持久化日志，测试见 `tests/test_cluster.py`，示例见 `examples/15_quorum_failover.py`。它的价值是解释语义，不是提供生产复制。

## 网络分区

分布式系统最危险的不是节点宕机，而是网络分区：节点都活着，但彼此看不到。这里必须区分两个时代：

**4.2 及以前（Mnesia 元数据）**：可以配置 `cluster_partition_handling`：

- `pause_minority`：少数派暂停，等待恢复；
- `autoheal`：分区恢复后自动合并；
- `ignore`：忽略分区（高风险，可能导致不一致）。

**4.3+（Khepri only）**：`cluster_partition_handling` 仍可能被接受，但**不再生效**；元数据和 Quorum 队列都走 Raft/多数派语义。不要再按旧 playbook 调这个键，也不要在升级后以为“已经配了 pause_minority 就安全”。

分区期间：

- 不同分区可能各自认为自己是集群；
- Quorum 队列只有在多数派一侧可写，少数派侧拒绝写入；
- classic 队列可能出现脑裂/不一致；
- 管理 UI 可能显示“partitions”；
- 客户端可能连到错误的一侧。

监控必须包括 `partitions` 指标和节点间网络质量。跨机架/跨 AZ 部署要评估延迟和分区概率，不要为了“高可用”把 quorum 放到高延迟链路。

## Quorum Queue 的限制

- 不支持 `x-max-priority`；
- 队列必须 durable、非 exclusive；
- 资源开销高于 classic；
- 大量小队列/大量消息时内存和磁盘压力更大；
- 某些插件/参数不兼容；
- 跨地域复制延迟高；
- 需要稳定的集群和时钟/DNS。

不要把“所有队列都换成 quorum”当成最佳实践。低价值、短生命周期、临时 RPC 回复队列用 classic 更合适。根据数据重要性和故障成本分层。

Quorum 的版本差异会直接影响集群运维：4.3+ 的 Khepri 让元数据写入也依赖多数派，4.4 的 CQv2 `.sqs` 是不可降级的数据格式单向门，4.3 的 32 级优先级和 4.4 的 `x-opt-delivery-time` 又会改变队列行为。迁移前按[附录 W](../appendices/W-4x-feature-labs.md)在一次性三节点集群验证，按[附录 X](../appendices/X-fact-check-and-sources.md)记录版本指纹与证据；不要用单节点实验推断集群语义。

## Stream 与集群

Stream 是复制日志，支持多消费者独立 offset、保留策略和高吞吐。它适合事件日志/遥测，不是“每条消息 ack 后删除”的队列。集群中的 stream 副本、leader、segment 和 retention 需要单独规划。第 12 章展开。

## Kubernetes 部署

推荐使用 RabbitMQ Cluster Operator：

- 管理 StatefulSet、Service、PVC；
- 支持 TLS、镜像、资源限制；
- 提供 `RabbitmqCluster` CRD；
- 处理滚动升级和节点发现。

关键配置：

- 稳定的 hostname/DNS；
- 每副本独立 PVC；
- 足够的 CPU/内存/磁盘；
- 反亲和性，避免所有节点在同一物理机；
- 网络策略开放集群端口；
- 管理 UI 不暴露公网；
- 备份/恢复方案；
- 监控 quorum/partition/leader。

不要用裸 Deployment + 共享空目录运行有状态 RabbitMQ；节点身份、磁盘和网络稳定性都会出问题。

## 故障演练

至少演练：

1. 杀死一个 quorum follower：写入应继续；
2. 杀死 leader：选举后恢复，confirm 超时/重试；
3. 杀死两个节点：确认多数派丢失时写入失败；
4. 网络分区：观察两侧行为、告警和恢复；
5. 磁盘满：发布阻塞、告警、恢复后释放；
6. 滚动升级：客户端重连、消费者恢复；
7. 备份恢复：从 definitions + 数据恢复拓扑和消息；
8. 队列/交换机误删：是否有审计和恢复路径。

演练要在预发环境做，并记录 RTO/RPO。不要在生产第一次验证故障转移。

## Quorum 的 leader locator、rebalance 与成员变更

### leader locator

Quorum 队列的 leader 放在哪个节点，会影响写入热点的分布。策略通常包括：

- `balanced`：尽量把 leader 均匀分布到节点；
- `client-local`：尽量让 leader 靠近客户端（降低延迟，但可能集中）；
- `least-leaders`：选择 leader 最少的节点。

策略通过 `queue-leader-locator` 配置；不同版本/云厂商支持不同。选择时要权衡：

- `client-local` 降低单客户端延迟，但多个客户端可能把 leader 集中到同一节点；
- `balanced` 更均匀，但跨节点写入延迟可能更高；
- 无论如何，都要监控每节点的 leader 数量/CPU/磁盘。

### rebalance

当节点增减或 leader 分布不均时，需要把 Quorum 队列的 leader 迁移到其他节点。命令要带队列类型：

```bash
rabbitmq-queues rebalance quorum   # 只重平衡 Quorum
rabbitmq-queues rebalance stream   # 只重平衡 Stream
rabbitmq-queues rebalance all
```

不要只写 `rabbitmq-queues rebalance`，CLI 会要求你明确范围。

注意：

- rebalance 会产生额外网络/磁盘开销；
- 迁移期间可能有短暂不可用/confirm 延迟；
- 不要在高负载/故障恢复期间做全量 rebalance；
- 先看 `rabbitmq-queues quorum_status` 和每节点 leader 数。

### 成员变更

Quorum 的副本集合可以变更（增加/减少节点），但这是 Raft 成员变更，不是“改个配置”。4.x 的主要控制点：

- `x-quorum-initial-group-size`：声明队列时的初始副本数，默认 3；
- `rabbitmq-queues add_member` / `grow` / `shrink`：增加/扩容/缩容副本；
- 新成员通常先以 non-voter 追赶，再提升为 voter；
- 成员变更需要多数派同意，期间可用性/延迟可能波动；
- 不要频繁变更，也不要在故障恢复期间做；
- 成员变更/快照安装是 Mini 未实现的部分（`cluster.py` 只模拟投票/复制/提交）。

## Raft 日志、快照与恢复

Quorum 的 Raft 日志不是无限保留：

- 日志增长到阈值会触发快照（snapshot）；
- 快照包含状态机在某个 index 的完整状态；
- 落后的 follower 可以通过日志追赶，日志不够时安装快照；
- 快照/日志都落盘，节点重启后恢复；
- 日志/快照损坏可能需要重建副本。

运维含义：

- 监控 Raft 日志/快照大小和增长；
- 磁盘 IO 延迟直接影响写入/选举；
- 节点长期离线后重新加入，可能需要大量传输；
- 不要手工删除日志/快照文件；
- 备份要理解“快照 + 日志”的一致性边界。

Mini 的 `cluster.py` 模拟了 append/replicate/commit/选举/追赶/冲突截断，但没有真实快照安装、成员变更、网络 RPC 和持久化日志。它适合理解语义，不适合验证生产恢复。

## 分区策略怎么选（仅对 ≤4.2 有意义）

下表只适用于 Mnesia 时代的 4.2 及以前；4.3+ 不要用 `cluster_partition_handling` 做决策。

| 策略 | 行为 | 适用 | 风险 |
|---|---|---|---|
| `pause_minority` | 少数派暂停，等待恢复 | 多数派可自愈的集群 | 少数派不可用；恢复需时间 |
| `autoheal` | 分区恢复后自动合并 | 可接受自动决策 | 可能丢/重复，需业务对账 |
| `ignore` | 忽略分区，两侧继续 | 几乎不推荐 | 脑裂、数据不一致 |

选择原则：

- 跨机架/同低延迟网络：≤4.2 常用 `pause_minority`；
- 跨地域：不要组一个大集群，用 Federation/Shovel；
- 4.3+：把精力放在 Quorum 多数派、节点间网络、`partitions` 监控和业务对账上；
- 关键业务：无论哪个版本，都要业务幂等 + 对账；
- 必须显式演练，不要依赖默认值。

## 3 节点还是 5 节点

| 节点数 | 多数派 | 容忍故障 | 写延迟/成本 | 适用 |
|---:|---:|---:|---|---|
| 1 | 1 | 0 | 最低 | 开发/可重建 |
| 3 | 2 | 1 | 中 | 生产基线 |
| 5 | 3 | 2 | 较高 | 关键业务/高可用 |
| 7 | 4 | 3 | 高 | 极少必要 |

更多节点不等于更好：

- 每次写入需要更多副本确认，延迟上升；
- 运维/升级/网络复杂度上升；
- 跨 AZ 流量成本上升；
- 多数派故障的爆炸半径更大。

3 节点是大多数团队的折中；5 节点用于“不能接受单节点故障导致写入不可用”的关键场景。无论几个节点，都要演练“杀 leader/杀多数派/网络分区”。

## 本章小结

RabbitMQ 集群共享元数据，但队列的可用性取决于队列类型。Classic 队列不跨节点复制；Quorum Queue 用 Raft 实现多数派持久化和 leader 故障转移；Stream 是复制日志。网络分区、Erlang cookie、节点名、Kubernetes 存储和版本兼容都是运维重点。

下一章进入 Streams：追加日志、offset、retention、Super Streams 分区，以及它和传统队列的本质区别。

---

[上一章：第 10 章 持久化、恢复与流控](10-persistence-flow-control.md) | [返回目录](../README.md) | [下一章：第 12 章 Streams 与 Super Streams](12-streams-super-streams.md)
