# 附录 T Kubernetes 生产部署与 Operator 实战

> 本附录定位：把“在 Kubernetes 上跑 RabbitMQ”从一条 `kubectl apply` 命令，展开成一套可运维的生产方案。重点不是背 CRD 字段，而是理解有状态 broker 与 Kubernetes 调度、存储、网络、升级模型之间的摩擦，以及 Operator 到底替你做了什么、没做什么。

## 为什么不能只用裸 StatefulSet

RabbitMQ 是有状态、需要稳定身份、依赖 Erlang 分布式、对磁盘和网络延迟敏感的中间件。把它塞进一个手写 StatefulSet 通常会在这些地方翻车：

- **节点身份不稳定**：Erlang 节点名依赖稳定 hostname；Pod 重建后如果名字变了，集群成员关系、数据目录和 cookie 都会错位。
- **PVC 与调度不匹配**：zonal 云盘绑定到某个可用区，Pod 被调度到另一个可用区就挂不上盘。
- **滚动升级没有顺序**：RabbitMQ 大版本升级要求逐节点、等健康、检查 feature flags；普通 Deployment 的滚动策略不理解 Quorum leader。
- **TLS/证书/密钥轮换**：需要把 Secret 挂载、权限、监听器配置、客户端信任链一起协调。
- **拓扑漂移**：交换机、队列、策略由人手工创建，环境之间不可复现。
- **备份和恢复没有编排**：PVC 快照、definitions 导出、恢复顺序、DNS 切换都需要流程。

RabbitMQ 官方提供两个 Operator，分别解决“集群生命周期”和“拓扑声明”两类问题：

| Operator | 负责 | 典型 CRD |
|---|---|---|
| Cluster Operator | 创建/升级/扩缩 RabbitMQ 集群，管理 StatefulSet、Service、PVC、TLS、监控 | `RabbitmqCluster` |
| Messaging Topology Operator | 用 Kubernetes 声明式 API 管理 vhost、用户、权限、交换机、队列、绑定、策略、Federation/Shovel | `Vhost`、`User`、`Permission`、`Exchange`、`Queue`、`Binding`、`Policy`、`Federation`、`Shovel` |

两者可以独立安装。只装 Cluster Operator 也能跑，但拓扑仍要手工或脚本管理；只装 Topology Operator 则需要一个已存在的集群引用。

## 版本与安装基线

以下命令只示意安装方式，实际版本号应以官方 release 页面为准：

```bash
# Cluster Operator
kubectl apply -f https://github.com/rabbitmq/cluster-operator/releases/latest/download/cluster-operator.yml

# Messaging Topology Operator
kubectl apply -f https://github.com/rabbitmq/messaging-topology-operator/releases/latest/download/messaging-topology-operator.yaml
```

安装后先确认：

```bash
kubectl get crd | grep rabbitmq.com
kubectl -n rabbitmq-system get pods
kubectl api-resources --api-group=rabbitmq.com
```

生产环境不要把 `latest` 当成版本策略。Operator 与 RabbitMQ 镜像、Kubernetes 版本、CRD schema 之间都有兼容矩阵；升级 Operator 前要读 release notes，并在预发集群验证 CRD 变更。

## 一个偏生产化的 RabbitmqCluster

下面是一个三节点、跨可用区、带资源边界和持久化的示例。字段名以目标 Operator 版本为准，重点是设计意图：

```yaml
apiVersion: rabbitmq.com/v1beta1
kind: RabbitmqCluster
metadata:
  name: orders-mq
  namespace: messaging
spec:
  replicas: 3

  image: rabbitmq:4.4-management
  persistence:
    storageClassName: fast-ssd
    storage: 200Gi

  resources:
    requests:
      cpu: "2"
      memory: 8Gi
    limits:
      cpu: "4"
      memory: 8Gi

  rabbitmq:
    additionalConfig: |
      vm_memory_high_watermark.relative = 0.6
      disk_free_limit.absolute = 20GB
      default_queue_type = quorum
      consumer_timeout = 1800000
      max_message_size = 16777216
    additionalPlugins:
      - rabbitmq_prometheus
      - rabbitmq_management

  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              app.kubernetes.io/name: orders-mq
          topologyKey: topology.kubernetes.io/zone

  terminationGracePeriodSeconds: 120
```

这段 YAML 里每个字段都在回答一个运维问题：

- `replicas: 3`：Quorum 队列能容忍 1 个节点故障；5 节点才容忍 2 个。副本数不是越多越好，Raft 写入要多数派确认。
- `persistence`：没有持久卷，Pod 重建后队列和消息一起消失；`storageClassName` 决定 IOPS 和延迟。
- `requests/limits`：Erlang 调度器、GC、TLS 都吃 CPU；内存 limit 太低会触发 OOMKill，太高会让 `vm_memory_high_watermark` 失去保护意义。
- `podAntiAffinity`：三个副本落在同一节点或同一可用区，等于没有高可用。
- `terminationGracePeriodSeconds`：给队列排空、连接关闭、Raft 交接留时间。
- `default_queue_type = quorum`：新队列默认复制，但老队列不会自动迁移；这是一条“新集群默认值”，不是迁移工具。
- 没有写 `cluster_partition_handling`：4.3 起 Khepri 是唯一元数据存储，Mnesia 时代的分区配置即使被接受也不再控制分区行为。不要从旧集群复制这类配置，先读目标版本的迁移指南。

不要把示例直接复制到生产。`storage`、CPU、内存、水位、`consumer_timeout` 都要按业务压测和故障演练校准。

## 存储：PVC 是最容易低估的部分

RabbitMQ 的磁盘行为与普通无状态服务不同：

- Quorum 队列写 Raft 日志，confirm 延迟受 fsync 和网络影响；
- Classic Queue v2 使用段文件，积压时磁盘增长快；
- Stream 按 retention 保留数据，磁盘占用与消费进度无关；
- 内存告警、磁盘告警会阻塞发布，而不是“慢慢变慢”；
- 节点重启要重放日志/恢复索引，恢复时间与数据量相关。

Kubernetes 存储选型要点：

| 维度 | 建议 | 原因 |
|---|---|---|
| 介质 | 本地 NVMe 或高 IOPS 云盘 | fsync 延迟直接进入 confirm 尾延迟 |
| 访问模式 | `ReadWriteOnce` | 每个副本独占自己的卷 |
| 绑定模式 | zonal 盘用 `WaitForFirstConsumer` | 避免 Pod 调度到无盘可用区 |
| 容量 | 按峰值积压 × 副本因子 × 安全系数 | 磁盘告警会阻塞发布 |
| 快照 | 只作为节点级备份的一部分 | 快照不保证跨节点消息一致 |

一个常见的容量公式：

```text
单节点磁盘 ≈ 峰值消息率 × 可接受积压时长 × 平均消息大小
           × 写放大系数 × 安全系数
```

Quorum 的写放大来自日志、索引、快照和压缩残留；不同版本、不同消息大小差异很大，必须用真实负载压测。不要用“平均消息大小”掩盖大消息长尾。

PVC 的生命周期也要明确：

- StatefulSet 删除后 PVC 是否保留？默认保留，但有人会写清理脚本；
- 缩容时被移除节点的 PVC 要不要归档？
- 恢复演练是否使用独立 namespace，避免覆盖生产卷？
- 快照是否加密、跨账号/跨区域复制？

## 调度：反亲和、PDB 与拓扑分布

三个副本“存在”不等于“高可用”。要保证它们分散在故障域：

```yaml
spec:
  affinity:
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 100
          podAffinityTerm:
            labelSelector:
              matchLabels:
                app.kubernetes.io/name: orders-mq
            topologyKey: topology.kubernetes.io/zone
```

`required` 能强制分散，但集群资源不足时会导致 Pod 无法调度；`preferred` 更灵活，但可能退化成同区部署。关键业务建议配合 `topologySpreadConstraints` 和足够的节点池。

PodDisruptionBudget 防止运维操作一次性干掉多数派：

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: orders-mq-pdb
  namespace: messaging
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app.kubernetes.io/name: orders-mq
```

还要注意：

- **节点维护**：`kubectl drain` 会驱逐 Pod；PDB 只能阻止“自愿中断”，不能阻止节点故障。
- **集群自动缩容**：不要缩掉承载 PVC 的节点，除非确认卷可以迁移。
- **资源碎片**：三副本 × 8Gi 内存 + 系统预留，节点池要留出故障转移空间。
- **单节点多副本**：反亲和配置错误时，三个 Pod 可能在同一节点，节点故障直接失去多数派。

## 网络：稳定 DNS 比 Service 更重要

RabbitMQ 节点之间通过 Erlang distribution 通信，依赖稳定主机名。Cluster Operator 会创建：

- 客户端 Service：`<name>`；
- 无头 Service：`<name>-server`，供 StatefulSet 稳定 DNS；
- 每个 Pod 的稳定域名，如 `<name>-server-0.<name>-server.<namespace>.svc.cluster.local`。

排查集群问题时，先确认：

```bash
kubectl -n messaging get svc
kubectl -n messaging get endpoints
kubectl -n messaging exec orders-mq-server-0 -- hostname -f
kubectl -n messaging exec orders-mq-server-0 -- rabbitmq-diagnostics cluster_status
```

常见网络问题：

- NetworkPolicy 只放行客户端端口，忘了 4369/25672 等节点间端口；
- Service 用了 `ClusterIP` 但客户端需要长连接，滚动升级时连接被切断；
- Ingress/LoadBalancer 没有配置空闲超时，长连接被中间设备静默断开；
- DNS 缓存导致故障转移后客户端仍连旧 IP；
- 跨命名空间引用 Secret/Cluster 时权限不足。

客户端侧要配合：

- 连接工厂设置合理的 heartbeat（不要依赖默认值）；
- 自动重连 + 退避 + 抖动；
- 拓扑恢复（重新声明 exchange/queue/binding）；
- confirm/return 在重连后重新建立；
- 消费者重新注册，并处理重复投递。

## TLS 与证书轮换

生产至少要有：

- 客户端到 broker 的 TLS；
- 管理面/监控的 TLS；
- 节点间通信的 TLS（可选但推荐）；
- 证书轮换流程；
- 私钥不落盘到镜像/日志。

Cluster Operator 的 TLS 字段通常包括 `secretName`、`caSecretName`、`disableNonTLSListeners` 等。一个示意：

```yaml
spec:
  tls:
    secretName: orders-mq-tls
    caSecretName: orders-mq-ca
    disableNonTLSListeners: true
```

配合 cert-manager 时，要让证书 SAN 覆盖：

- 客户端 Service DNS；
- 每个 Pod 的稳定 DNS；
- 外部域名（如果通过 LoadBalancer/Ingress 暴露）。

轮换时最容易忽略的是**客户端信任链**和**节点间证书**。只换服务端证书、不更新客户端 truststore，会在证书过期当天变成全站故障。轮换演练要覆盖：

1. 新证书签发；
2. 滚动重启/热加载；
3. 新旧证书并存窗口；
4. 客户端信任更新；
5. 旧证书吊销；
6. 监控告警确认。

## 拓扑即代码：Topology Operator

手工在管理 UI 点出来的拓扑，无法复现、无法评审、无法回滚。Topology Operator 把 RabbitMQ 资源映射成 Kubernetes CRD：

```yaml
apiVersion: rabbitmq.com/v1beta1
kind: Vhost
metadata:
  name: orders
  namespace: messaging
spec:
  name: orders
  rabbitmqClusterReference:
    name: orders-mq
---
apiVersion: rabbitmq.com/v1beta1
kind: User
metadata:
  name: orders-app
  namespace: messaging
spec:
  tags:
    - management
  importCredentialsSecret:
    name: orders-app-credentials
  rabbitmqClusterReference:
    name: orders-mq
---
apiVersion: rabbitmq.com/v1beta1
kind: Permission
metadata:
  name: orders-app-permission
  namespace: messaging
spec:
  userReference:
    name: orders-app
  vhost: orders
  permissions:
    configure: ".*"
    write: ".*"
    read: ".*"
  rabbitmqClusterReference:
    name: orders-mq
```

交换机、队列、绑定、策略同理：

```yaml
apiVersion: rabbitmq.com/v1beta1
kind: Exchange
metadata:
  name: orders-exchange
  namespace: messaging
spec:
  name: orders
  type: topic
  durable: true
  autoDelete: false
  vhost: orders
  rabbitmqClusterReference:
    name: orders-mq
---
apiVersion: rabbitmq.com/v1beta1
kind: Queue
metadata:
  name: inventory-order-created
  namespace: messaging
spec:
  name: inventory.order-created
  durable: true
  autoDelete: false
  vhost: orders
  arguments:
    x-queue-type: quorum
    x-dead-letter-exchange: orders.dlx
  rabbitmqClusterReference:
    name: orders-mq
---
apiVersion: rabbitmq.com/v1beta1
kind: Binding
metadata:
  name: inventory-order-created-binding
  namespace: messaging
spec:
  source: orders
  destination: inventory.order-created
  destinationType: queue
  routingKey: order.created
  vhost: orders
  rabbitmqClusterReference:
    name: orders-mq
```

Topology Operator 的关键约束：

- 引用的 Secret 通常要打标签 `rabbitmq.com/topology-operator: "true"`；
- 自动生成的用户凭据 Secret 名称通常是 `<user-name>-user-credentials`；
- 如果直接在 `additionalConfig` 里设置 `default_user`/`default_pass`，Topology Operator 可能拿到 401；应使用生成的凭据 Secret；
- 自定义域名/TLS 时可能需要 `rabbitmq.com/operator-connection-uri` 注解；
- CRD 删除行为要明确：删除 `Queue` CR 是否会删除 broker 上的队列？生产环境建议先确认 ownerReference/finalizer 策略，避免误删。

把拓扑放进 Git 之后，流程应该是：

```text
修改 CR -> PR 评审 -> CI schema 校验/策略检查 -> Argo CD/Flux 同步
        -> Topology Operator reconcile -> broker 拓扑变更
        -> 监控/契约测试验证
```

不要让“GitOps”变成“自动把错误拓扑推到生产”。CI 至少要做：

- CRD schema 校验；
- 命名规范/owner 标签检查；
- 禁止 `autoDelete`/exclusive 的误用；
- 队列类型与业务等级匹配；
- DLX/长度限制/TTL 必填项；
- 变更影响面（删除队列/交换机）人工审批。

## 升级与扩缩容

### 升级

Kubernetes 上的升级有两层：Operator 升级和 RabbitMQ 镜像升级。

推荐顺序：

1. 读 Operator 与 RabbitMQ 的 release notes/升级指南；
2. 在预发集群演练，包含 Quorum leader 切换、客户端重连、监控告警；
3. 备份 definitions、配置、证书、关键消息；
4. 升级 Operator（CRD 可能变更）；
5. 升级 RabbitMQ 镜像，观察 `RabbitmqCluster` 状态条件；
6. 逐节点确认健康、Quorum 成员、积压、confirm 延迟；
7. 保留回滚窗口。

```bash
kubectl -n messaging get rabbitmqcluster orders-mq -o yaml
kubectl -n messaging describe rabbitmqcluster orders-mq
kubectl -n messaging get pods -w
kubectl -n messaging exec orders-mq-server-0 -- rabbitmq-diagnostics status
kubectl -n messaging exec orders-mq-server-0 -- rabbitmq-queues quorum_status inventory.order-created
```

不要走官方路径矩阵之外的跳级（例如 3.13 直达 4.4）。4.2 → 4.3 前必须完成 `khepri_db` 迁移；4.4 的 CQv2 `.sqs` 文件是单向门。Operator 能帮你滚动 Pod，但不能替你做 feature flag 和拓扑兼容检查。

### 扩缩容

- 加节点：新 Pod 加入集群后，Quorum 队列不会自动把副本搬到新节点；需要 `rabbitmq-queues rebalance` 或重建队列。
- 减节点：先迁移/删除该节点上的 Quorum 成员，再缩容；直接删 Pod 可能触发 leader 选举和短暂不可用。
- Stream：扩缩容涉及分区/协调器，不能按普通队列处理。
- 缩容前确认 PVC 保留策略和备份。

## 监控与告警

推荐组合：

- `rabbitmq_prometheus` 插件暴露 `/metrics`；
- Prometheus Operator 的 `ServiceMonitor`/`PodMonitor` 采集；
- Grafana 面板看队列深度、发布/消费速率、confirm 延迟、未确认消息、Quorum 状态、磁盘/内存；
- Alertmanager 按严重级别路由到 on-call。

关键告警不要只看“节点活着”：

| 告警 | 触发条件示例 | 含义 |
|---|---|---|
| 队列积压持续增长 | ready 消息 15 分钟单调上升 | 消费者不足/卡住 |
| 消费者为 0 | 关键队列 consumers=0 且 ready>0 | 消费端掉线 |
| 未确认消息高 | unacked 持续超过阈值 | 处理慢/ack 丢失 |
| Quorum 无 leader | leader 缺失/成员不足 | 写入不可用 |
| 磁盘水位 | free < 阈值 | 即将阻塞发布 |
| 内存告警 | memory alarm | 发布被阻塞 |
| confirm 延迟 | p99 超 SLO | 磁盘/网络/过载 |
| 证书到期 | < 30 天 | 未来故障 |

Kubernetes 事件也要采集：Pod OOMKilled、PVC 挂载失败、调度失败、Operator reconcile 错误。

## 备份与恢复

Cluster Operator 不提供“消息一键备份”。Kubernetes 上的备份要分层：

1. **拓扑/配置**：definitions 导出、CR YAML、`additionalConfig`、插件列表；
2. **密钥/证书**：Secret、CA、客户端信任链；
3. **节点数据**：PVC 快照（一致性取决于停机/存储语义）；
4. **消息**：上游事件日志、对象存储归档、Shovel 到备份集群、消费者归档；
5. **集群元数据**：Khepri/Quorum 状态随节点数据。

恢复顺序：

```text
基础设施/网络/存储 -> Secret/证书 -> 节点数据 -> 启动集群
-> 校验集群健康 -> 导入 definitions -> 校验拓扑/权限
-> 恢复/重放消息 -> 客户端切流 -> 对账
```

PVC 快照不等于消息备份。对运行中的 Quorum 队列做单卷快照，可能拿到滞后的 follower 日志；恢复后消息计数、消费位点、死信都可能不一致。必须做恢复演练，并验证：

- 交换机/队列/绑定数量；
- 用户/权限/vhost；
- 关键队列的消息计数与抽样内容；
- 消费者能否重连；
- DLQ/重试链路；
- 监控是否恢复。

## 多集群与多区域

不要把跨地域节点塞进一个 Erlang 集群。跨区域网络延迟和分区会让 Raft 多数派、心跳、元数据写入变得不可预测。更现实的模式：

- **同区域多 AZ**：一个集群，Quorum 副本跨 AZ；
- **跨区域 active-passive**：主集群 + 温备集群，Federation/Shovel 或应用双写；
- **跨区域 active-active**：按业务域分区，Federation 做事件扩散，应用层处理重复/冲突；
- **边缘**：本地 broker 缓冲，Federation/Shovel 上行。

Kubernetes 层面还要考虑：

- 多集群 DNS/GSLB；
- 跨集群证书与信任；
- 网络出口/入口策略；
- 数据驻留合规；
- 跨区域流量成本；
- 故障切换演练。

## 常见故障与排查

| 症状 | 常见根因 | 排查 |
|---|---|---|
| Pod 一直 Pending | 资源不足/反亲和太严/PVC 绑不上 | `kubectl describe pod`、StorageClass、节点池 |
| Pod 启动但集群不形成 | DNS/端口/Erlang cookie/节点名不一致 | `cluster_status`、NetworkPolicy、hostname |
| 滚动升级卡住 | PDB、Quorum 健康、镜像拉取、终止宽限期 | `describe`、事件、`quorum_status` |
| 队列不可用 | 多数派丢失/leader 选举/磁盘告警 | `quorum_status`、`rabbitmq-diagnostics` |
| 客户端连不上 | Service/端口/TLS/认证/vhost | `rabbitmq-diagnostics check_port_connectivity` |
| 拓扑 CR 不生效 | Secret 标签/权限/Cluster 引用/Operator 日志 | `kubectl describe queue`、Operator logs |
| 磁盘满 | 积压/Stream retention/日志/快照 | `rabbitmq-diagnostics check_local_alarms`、`rabbitmqctl status`、PVC 使用率 |
| OOMKilled | limit 太低/大消息/积压 | 内存水位、消息大小、Pod events |

排查时先区分“Kubernetes 层”和“RabbitMQ 层”：Pod 没起来、卷没挂上、Service 没端点，是 Kubernetes 问题；Pod Running 但队列不可用，才进入 broker 诊断。

## 生产检查清单

- [ ] 使用 Cluster Operator，而不是手写 StatefulSet；
- [ ] 3/5 副本跨节点、跨 AZ，PDB 配置正确；
- [ ] PVC 使用高 IOPS 介质，容量按峰值积压规划；
- [ ] CPU/内存 requests/limits 经过压测，水位合理；
- [ ] 节点间端口、客户端端口、监控端口有 NetworkPolicy；
- [ ] TLS 覆盖客户端/管理面/节点间，证书轮换演练过；
- [ ] 拓扑用 CR/GitOps 管理，删除操作有审批；
- [ ] 升级按官方路径矩阵，feature flags/Khepri/CQv2 检查过；
- [ ] Prometheus/Grafana/Alertmanager 覆盖队列、Quorum、磁盘、证书；
- [ ] 备份包含 definitions、Secret、PVC 快照、消息归档；
- [ ] 恢复演练在独立 namespace 完成，记录 RTO/RPO；
- [ ] 多集群/多区域切换有 runbook 和回切方案；
- [ ] 客户端重连、拓扑恢复、幂等、confirm/return 已验证。

## 与 Mini-RabbitMQ 的映射

Mini-RabbitMQ 不实现 Kubernetes Operator，但它的模块边界能帮助你理解 Operator 管理的对象：

| Kubernetes/Operator 对象 | Mini 中的对应 |
|---|---|
| `RabbitmqCluster` | `mini_rabbitmq/broker.py` + `cluster.py` 的进程内集群 |
| `Vhost`/`User`/`Permission` | `vhost.py`、`auth.py` |
| `Exchange`/`Queue`/`Binding` | `exchange.py`、`queue.py`、`vhost.py` |
| `Policy` | `policy.py` |
| `Federation`/`Shovel` | `links.py` |
| 管理面/指标 | `management.py`、`metrics.py` |

练习建议：

1. 用 Mini 的 `definitions` 快照模拟“拓扑即代码”；
2. 写一个脚本对比两个 vhost 的拓扑差异，模拟 GitOps drift；
3. 用 `cluster.py` 的投票/日志复制观察多数派丢失后的行为；
4. 把 Mini 的 CLI 参数映射成一份 `RabbitmqCluster` YAML 草稿，检查哪些字段在 Mini 中没有对应物。

## 小结

Kubernetes 上的 RabbitMQ 不是“把镜像跑起来”，而是把有状态 broker 的身份、存储、网络、升级、备份和多集群策略产品化。Cluster Operator 负责集群生命周期，Topology Operator 负责拓扑声明；它们能减少手工操作，但不能替代容量规划、故障演练和业务幂等。生产方案的核心仍然是：跨故障域副本、可恢复的备份、可验证的升级、可观测的告警，以及能承受重复/乱序的客户端。

---

[返回目录](../README.md) | [上一章：附录 S 消息架构模式与反模式目录](S-messaging-patterns-antipatterns.md) | [下一章：附录 U 灾备、多地域与业务连续性](U-disaster-recovery-continuity.md)
