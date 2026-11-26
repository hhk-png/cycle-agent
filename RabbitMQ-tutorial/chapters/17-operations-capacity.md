# 第 17 章 运维、部署与容量规划

> 本章定位：把 RabbitMQ 当作需要生命周期管理的生产系统，而不是“装好就不管”的基础设施。覆盖部署拓扑、资源规划、备份、升级、故障演练和 SLO。

## 部署拓扑

| 拓扑 | 可用性 | 成本 | 适用 |
|---|---|---|---|
| 单节点 | 低 | 低 | 开发、测试、可重建的内部系统 |
| 3 节点集群 | 中高 | 中 | 生产基线，Quorum 容忍 1 节点故障 |
| 5 节点集群 | 高 | 高 | 关键业务，容忍 2 节点故障 |
| 多集群 + Federation/Shovel | 跨地域 | 很高 | 多活、灾备、数据驻留 |
| 托管服务 | 取决于 SLA | 可变 | 团队不想运维 Erlang/集群 |

单节点不是“不能生产”，而是必须明确接受 RPO/RTO。对于可以重建的任务队列，单节点 + 快速重建可能足够；对于订单/支付，至少 3 节点 Quorum。

## 资源规划

### CPU

- 连接/channel 管理、TLS、消息路由消耗 CPU；
- Quorum 的 Raft 复制也消耗 CPU/网络；
- 大量小消息放大每消息开销；
- TLS 握手和加解密需要额外 CPU。

### 内存

- 消息、索引、连接、channel、插件；
- 内存高水位触发 blocked；
- 分页会降低性能；
- Erlang 运行时/GC 开销；
- 预留操作系统和监控空间。

### 磁盘

- 持久化消息、段文件、索引、日志；
- Quorum 日志/快照；
- 备份空间；
- 磁盘水位阈值；
- IOPS/fsync 延迟比容量更关键。

### 网络

- 客户端连接；
- 节点间复制；
- Federation/Shovel；
- 管理/监控；
- TLS 开销；
- 跨 AZ/region 延迟。

一个粗略容量公式：

```text
存储 = 峰值消息率 × 可接受积压时长 × 平均消息大小 × 副本因子 × 安全系数
```

示例（仅示范方法，不是基准）：

```text
峰值 2,000 msg/s
积压容忍 30 分钟 = 1800 s
平均 4 KB（按 1 KB = 1000 B 计）
副本 3
安全系数 2

2,000 × 1,800 × 4,000 B × 3 × 2 = 86.4 GB
```

如果按 1 KiB = 1024 B 计，同样输入是 2,000 × 1,800 × 4,096 × 3 × 2 ≈ 88.5 GB（十进制 GB），约 82.4 GiB。写容量文档时必须注明单位约定，否则 82.8/86.4/88.5 这类差异会让评审对不上账。

还要加索引、日志、DLQ、备份和操作系统空间。实际用生产采样校准，不要用“平均值”掩盖大消息长尾。

## 节点规格建议（量级）

这些是架构讨论用的量级，不是 RabbitMQ 官方基准：

| 场景 | CPU | 内存 | 磁盘 |
|---|---|---|---|
| 开发/学习 | 2 vCPU | 4 GB | 20 GB |
| 中小生产 | 4～8 vCPU | 16～32 GB | SSD 100～500 GB |
| 高吞吐 quorum | 8～16 vCPU | 32～64 GB | 本地 SSD/NVMe，独立数据盘 |
| 跨区域灾备 | 与主集群相近 | 与主集群相近 | 取决于保留 |

真实规格取决于消息大小、确认模式、持久化、插件、TLS、消费者数量和 SLA。用 PerfTest/生产回放压测，不要照抄表格。

## Kubernetes 部署

使用 RabbitMQ Cluster Operator：

- `RabbitmqCluster` CRD 管理 StatefulSet/Service/PVC；
- 每副本独立 PVC；
- 稳定网络标识；
- 支持 TLS/资源/亲和性；
- 管理升级和扩缩容。

最小可用配置只需要 `replicas`、资源、`persistence` 和必要的 `additionalConfig`。完整的生产化示例（反亲和、PVC、消息大小上限）见本章后面的“Operator 的关键字段”，不要把示例当模板直接复制。Cluster Operator 与 Messaging Topology Operator 的分工、CRD 示例、存储/调度/TLS/GitOps/备份/升级细节，见[附录 T](../appendices/T-kubernetes-operator-production.md)。

注意：

- memory limit 与 Erlang 堆/页缓存关系要留余量；
- 不要所有副本在同一节点；
- 网络策略限制端口；
- 管理 UI 通过 ingress + 认证保护；
- 升级前检查 quorum/feature flags；
- 4.3 起 Khepri 是唯一元数据存储，`cluster_partition_handling` 等 Mnesia 时代的分区配置会被接受但**不生效**，应从 `additionalConfig` 中删除，避免误以为还能控制分区行为；
- 备份 PVC 不等于消息备份。

## 配置与拓扑管理

把以下内容纳入版本控制/配置管理：

- vhost、用户、权限；
- 交换机、队列、绑定；
- policy、operator policy；
- 参数（Federation upstream、Shovel）；
- TLS/证书；
- 资源限制；
- 插件列表。

RabbitMQ 支持 definitions 导出/导入：

```bash
rabbitmqctl export_definitions /backup/definitions.json
rabbitmqctl import_definitions /backup/definitions.json
```

definitions 包含拓扑/用户/权限/策略等元数据，不包含队列中的消息。导入前确认版本兼容、密码哈希/凭据处理、是否覆盖现有对象。

## 备份与恢复

分层备份：

1. **拓扑**：definitions 定期导出；
2. **消息**：Quorum/Classic 的底层存储快照，或通过 Shovel/Federation/归档复制；
3. **配置**：K8s manifests、TLS、插件、策略；
4. **审计/日志**：按合规保留；
5. **密钥**：独立加密保存。

恢复演练要回答：

- RPO 是多少（能丢多少消息/时间）？
- RTO 是多少（多久恢复）？
- 恢复后消费者如何重放/对账？
- DLQ/积压如何处理？
- 拓扑版本与代码版本是否匹配？

没有演练的备份等于没有备份。

## 升级

RabbitMQ 升级涉及：

- Erlang/OTP 版本兼容；
- 节点版本兼容；
- feature flags；
- 插件兼容；
- Quorum 队列滚动重启；
- 客户端协议兼容；
- 管理 API/监控指标变化。

滚动升级原则：

1. 阅读目标版本 release notes/升级指南；
2. 备份 definitions/配置；
3. 预发演练；
4. 一次升级一个节点，等待集群健康；
5. 监控 quorum leader/分区/积压；
6. 升级客户端库；
7. 保留回滚窗口；
8. 观察 confirm 延迟、错误率、DLQ。

不要在生产首次验证插件兼容性，也不要假设“相邻版本才支持、跨一个版本一定不行”。按官方升级路径矩阵走：到 4.4 可从 4.3.x 或 4.2.x 直接升；从 3.13.x/4.0.x 要先到 4.2.x，再从 4.2.x 到 4.4.x。4.2 上必须启用 `khepri_db` 并完成迁移（4.3 起 Mnesia 被移除，迁移不可逆）；4.3.7+ 是 4.3↔4.4 元数据互操作条件，CQv2 `.sqs` 文件是单向门。细节见[附录 I](../appendices/I-version-migration.md)。

## 灾难恢复

灾难场景：

- 单节点故障；
- 机架/可用区故障；
- 区域故障；
- 磁盘损坏；
- 元数据损坏；
- 误删队列/交换机；
- 勒索软件/权限滥用。

应对：

- 多 AZ 集群；
- 跨区域 Federation/Shovel；
- 上游事件日志/对象存储归档；
- 不可变备份；
- 最小权限和审计；
- 定期恢复演练；
- 业务对账/重放工具。

跨区域 active-active 的冲突处理必须业务设计，不能只靠 broker。RPO/RTO、故障域、备份对象、Federation/Shovel/应用双写的取舍、切流与回切、演练验收，见[附录 U](../appendices/U-disaster-recovery-continuity.md)。

## SLO 与 SLI

消息系统常见 SLI：

- 发布成功率（confirm ack / 总发布）；
- 发布延迟 p99；
- 消费延迟（端到端）；
- 队列积压恢复时间；
- DLQ 比率；
- 可用性（连接/发布/消费）；
- 消息重复率；
- 数据丢失事件数（目标应为 0）。

SLO 示例（不是通用标准）：

```text
99.9% 的发布在 100ms 内收到 confirm
99% 的订单事件在 2s 内被消费
DLQ 增长在 5 分钟内告警
单节点故障后 60s 内恢复消费
```

SLO 要可测量、可告警、有 owner。不要用“系统要稳定”这种无法验证的目标。

## 日常运维清单

| 周期 | 检查项 |
|---|---|
| 每日 | 队列深度、DLQ 增长、告警状态、节点健康、磁盘水位、证书到期 |
| 每周 | 连接/channel 趋势、消费者 lag、策略/拓扑变更审计、备份成功 |
| 每月 | 容量评估、权限复核、恢复抽检、无用队列清理、Khepri 元数据健康 |
| 每季度 | 故障演练、升级评估、证书/密钥轮换、TCO 复盘 |
| 每年 | 架构评审、DR 演练、供应商/许可评估、版本支持窗口核对 |

清单的价值在于“有人负责、有阈值、有记录”。只列项目不设 owner 和告警阈值，等于没有运维。

## 队列卫生

- 删除未使用队列；
- 合并重复队列；
- 限制 auto-delete/exclusive 队列数量；
- 设置 max-length/DLX 防止无限增长；
- 监控队列数量增长；
- 避免每租户一个永久队列（除非有清理策略）；
- 对临时 RPC 回复队列使用 exclusive/auto-delete。

队列数量过多会增加元数据、内存和管理开销。一个集群几千个队列可能正常，几万个要评估。

## 滚动升级 Runbook

RabbitMQ 升级不是“换镜像重启”。以 3.13 → 4.4 为例：

```text
T-14：盘点版本/插件/客户端/拓扑/policy
     扫描 ha-mode、x-queue-mode、transient、global QoS
     扫描延迟插件、x-delay、Mnesia 依赖插件
     预发集群演练升级与回滚

T-7： 在 3.13 启用全部 feature flags
     按 4.0 -> 4.1 -> 4.2 演练；在 4.2 启用 khepri_db
     压测 Quorum confirm/消费/积压恢复/压缩
     验证客户端重连/幂等/监控

T-1： 备份 definitions/配置/证书
     确认监控/告警/on-call/变更窗口
     明确 Khepri 迁移与 .sqs 的单向门

T：   按 4.0 -> 4.1 -> 4.2 -> 4.3 -> 4.4 逐版本、逐节点升级
     每步等待集群健康、Quorum leader、无分区
     Khepri 迁移期间保持多数派在线
     观察 confirm p99、积压、DLQ、连接数、插件错误

T+1： 全量验证业务链路与对账
     观察 24h，再关闭回滚窗口
```

细节和破坏性变更见[附录 I](../appendices/I-version-migration.md)。三条铁律：

1. 不要跳官方路径矩阵不支持的路径（例如 3.13 直接到 4.4）；
2. 不要在未启用 feature flags 的情况下升级；
3. 不要在生产第一次验证插件/客户端兼容性。

## Operator 的关键字段

RabbitMQ Cluster Operator 把 StatefulSet/PVC/Service/TLS/升级做成 CRD。一个偏生产化的 `RabbitmqCluster` 片段：

```yaml
apiVersion: rabbitmq.com/v1beta1
kind: RabbitmqCluster
metadata:
  name: rabbitmq
spec:
  replicas: 3
  resources:
    requests:
      cpu: "2"
      memory: 4Gi
    limits:
      cpu: "4"
      memory: 4Gi
  persistence:
    storageClassName: fast-ssd
    storage: 100Gi
  rabbitmq:
    additionalConfig: |
      vm_memory_high_watermark.relative = 0.6
      max_message_size = 16777216
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              app.kubernetes.io/name: rabbitmq
          topologyKey: kubernetes.io/hostname
```

要点：

- 每副本独立 PVC；
- 反亲和性避免同物理机；
- 内存 limit 要给 Erlang/页缓存留余量；
- 管理 UI 通过 ingress + 认证保护；
- 网络策略只开放必要端口；
- 升级前检查 feature flags/Quorum/Khepri 状态；
- 4.3+ 不再使用 `cluster_partition_handling`，Raft 统一处理多数派与恢复；
- PVC 快照不等于消息备份。

## 容量规划案例

假设：

```text
峰值发布 3,000 msg/s
峰值消费 2,500 msg/s（消费滞后）
允许积压 20 分钟 = 1,200 s
平均消息 3 KB，p99 30 KB
Quorum 3 副本
安全系数 2
```

存储估算（按平均）：

```text
(3000 - 2500) × 1200 × 3KB × 3 × 2 ≈ 10.8 GB
```

如果按“所有消息都积压 20 分钟”保守估算：

```text
3000 × 1200 × 3KB × 3 × 2 ≈ 64.8 GB
```

还要加：

- 索引/段文件/日志/快照；
- DLQ/重放/备份；
- 操作系统/监控；
- 大消息长尾（p99 30 KB 不是上限）；
- 磁盘水位余量（长期 < 60%）。

消费者估算：

```text
所需并发 ≈ 2500 × 平均处理时间 / 0.7
```

如果平均处理 40 ms，约 143 并发。下游数据库连接池/API 限流往往先成为瓶颈。

## 成本优化：先降无效工作，再谈单价

消息系统的账单通常不是“CPU 单价”决定的，而是这些因素叠加：

1. **队列/拓扑膨胀**：每个未使用队列都有元数据、监控和恢复成本；几万个空闲队列会拖慢启动和滚动升级；
2. **副本因子过高**：所有队列都 Quorum，会把写入、磁盘和网络放大 3 倍；低价值/临时队列用 classic 更合理；
3. **消息太大**：大消息放大内存、磁盘、复制、备份和跨 AZ 流量；图片/文档放对象存储，消息只传引用；
4. **保留期过长**：Stream retention、DLQ、备份保留都会持续占盘；保留期要由业务/合规需求决定，不是“越大越安全”；
5. **连接/channel 泄漏**：空闲连接和 channel 占内存/FD，还会让监控和重连风暴更严重；
6. **跨 AZ/跨区域流量**：Quorum 跨 AZ、Federation/Shovel 复制、备份异地存放都会产生流量成本；
7. **过度预留**：按峰值 3 倍常年预留，利用率却很低；应按基线 + 弹性/突发评估。

优化顺序：

```text
先删无用队列/绑定/交换机，再谈压缩
先按数据重要性分层队列类型，再谈副本
先控制消息大小/保留期，再谈磁盘
先修连接/channel 泄漏，再谈扩容
最后才谈预留/托管折扣
```

成本要和可靠性一起算：把 DLQ 保留期从 30 天砍到 3 天，可能省盘，但会削弱故障复盘和合规证据；把所有 Quorum 降成 classic，可能省资源，但会把节点故障变成数据丢失。用[附录 Y](../appendices/Y-compliance-data-governance.md)确认保留/删除要求，用[附录 L](../appendices/L-performance-benchmark-playbook.md)量化不同方案的资源代价，再把 TCO 放回第 27～29 章的商业框架。

## 多租户运维

多租户不只是 vhost：

| 维度 | 手段 | 注意 |
|---|---|---|
| 命名/权限 | vhost + 正则 ACL | 正则过宽会越权 |
| 配额 | 队列/连接/消息限制 | 版本/插件支持不同 |
| 监控 | 按 vhost/队列标签 | 高基数标签会拖垮 Prometheus |
| 计费 | 消息量/存储/连接 | 需要准确计量 |
| 隔离 | 独立集群/网络 | vhost 不隔离资源 |
| 升级 | 分批/蓝绿 | 租户影响面不同 |

常见反模式：

- 每租户一个 vhost + 永久队列，租户增长后元数据爆炸；
- 共享用户 + 通配权限；
- 监控标签用租户 ID 导致高基数；
- 没有配额，单个租户打满集群。

## 配额与资源限制

多租户/多团队共享集群时，光靠“约定”不够，要有硬限制：

```bash
# vhost 级限制（示例字段，具体依版本）
rabbitmqctl set_vhost_limits -p /app '{"max-connections": 200, "max-queues": 2000}'

# 用户级连接限制
rabbitmqctl set_user_limits app '{"max-connections": 50}'

# 查看
rabbitmqctl list_vhost_limits -p /app
rabbitmqctl list_user_limits app
```

常见限制维度：

| 维度 | 手段 | 注意 |
|---|---|---|
| 连接数 | vhost/user limits、网络层 | 防止单租户打满 FD |
| 队列数 | vhost limits | 防止元数据爆炸 |
| 消息大小 | `max_message_size` | 4.0 默认 16 MiB |
| 帧/信道 | `frame_max`/`channel_max` | 协商值，影响内存/吞吐 |
| 消费超时 | `consumer_timeout` | 防止永不 ack；4.3+ 仅 Quorum 评估，classic/stream 不触发 |
| 磁盘/内存 | 水位告警 | 全局保护，不是租户级 |
| 吞吐 | 客户端限流/配额 | broker 本身不做精细限速 |

注意：

- 限制触发时客户端可能只看到连接/channel 关闭；
- 限制值要留余量，避免正常峰值被误杀；
- 监控“接近限制”的比例，而不是只看是否触发；
- 配额不是安全隔离；强隔离仍需独立集群/vhost/网络。

## 本章小结

运维 RabbitMQ 需要部署拓扑、资源规划、配置管理、备份、升级、DR、SLO 和日常卫生。3 节点 Quorum 是常见生产基线，但不是所有队列都该 quorum。容量规划用峰值、积压时长、消息大小和副本因子估算，再用真实压测校准。备份必须包含拓扑和消息，并定期演练恢复。拓扑、schema 与消费者的日常变更如何做到可回滚、零停机，见[第 30 章](30-change-engineering.md)。

下一章进入性能工程：确认批处理、prefetch、并发、持久化、Quorum 延迟和基准测试方法。

---

[上一章：第 16 章 可观测性：指标、日志、追踪与 Firehose](16-observability.md) | [返回目录](../README.md) | [下一章：第 18 章 性能工程：从连接数到持久化](18-performance-engineering.md)
