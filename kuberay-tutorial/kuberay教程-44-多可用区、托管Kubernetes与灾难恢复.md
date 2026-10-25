# 第 44 章 多可用区、托管 Kubernetes 与灾难恢复

前 43 章默认了一个隐含前提：集群的控制面、节点、存储和网络都在同一个故障域里。生产环境很少这么简单。RayCluster 可能跨三个可用区，head 和 GPU worker 分属不同节点池，对象存储和镜像仓库在集群外，operator 自己也需要高可用。本章把这些约束放在一起，回答三个问题：

1. **当一个可用区整体不可用时，哪些对象还能工作？**
2. **KubeRay 的哪些字段能帮助放置，哪些只是“看起来能帮忙”？**
3. **在 EKS/GKE/AKS 这类托管 Kubernetes 上，哪些差异会改变设计？**

这一章不重复第 6 章的 Pod 模板、第 13 章的 GCS FT、第 31 章的升级流程和第 41 章的控制面规模，而是把它们放进“故障域”这个统一框架里。

## 44.1 为什么把“单可用区”当作默认风险

一个 Ray 集群通常包含这些依赖：

- operator 与 Kubernetes 控制面；
- head Pod（GCS、Dashboard、autoscaler）；
- worker Pod；
- head Service、Serve Service、Gateway/Ingress；
- Redis（如果启用 GCS FT）或 PVC（RocksDB 后端）；
- 对象存储、镜像仓库、包索引、模型仓库；
- 节点池、GPU device plugin、网络插件、DNS。

它们中任何一个与 head 同处一个可用区，都可能把“单点故障”升级成“整集群故障”。常见的连锁反应：

```text
可用区 A 故障
  -> head Pod 与 Redis 同时不可用
  -> worker 无法重连 GCS
  -> RayService Serve 代理全部失联
  -> 新 Pod 因 GPU 节点池只在 A 区而 Pending
  -> operator 能创建 Pod，但调度器无容量可放
  -> 业务侧看到 503，且恢复时间取决于云厂商的容量
```

注意最后两步：**控制面能工作，不等于数据面能恢复**。KubeRay 能把 Pod 重建出来，但如果没有可调度的节点、可挂载的存储、可达的 GCS，重建只会停在 Pending 或 CrashLoopBackOff。

### 44.1.1 高可用与灾难恢复不是一回事

| 目标 | 问题 | 典型手段 | 失败时 |
|---|---|---|---|
| HA（高可用） | 单节点/单进程故障时继续服务 | 多副本、PDB、GCS FT、重试 | 秒级到分钟级抖动 |
| DR（灾难恢复） | 整个故障域丢失后恢复业务 | 备份、跨区/跨集群、DNS 切换 | 分钟级到小时级中断 |
| 业务连续性 | 对用户承诺的 SLO | 多活、降级、排队、重试 | 取决于架构 |

KubeRay 本身主要解决 HA 的一部分：重建 Pod、重建 head、切换 Serve 流量。**它不提供跨集群复制，也不提供跨区存储复制**。DR 必须由平台层设计。

## 44.2 故障域与恢复目标

### 44.2.1 故障域模型

| 故障域 | 典型故障 | 影响 | 可用的缓解 |
|---|---|---|---|
| 容器/进程 | OOM、崩溃、死锁 | 单个容器重启 | liveness/readiness、重启策略 |
| Pod | 节点压力、驱逐、镜像问题 | 单 Pod 重建 | 控制器、PDB、优先级 |
| 节点 | 宕机、内核panic、GPU XID | 节点上所有 Pod | 反亲和、多副本、节点池 |
| 机架/交换机 | 断电、TOR 故障 | 一组节点 | 机架感知、跨机架副本 |
| 可用区 | 电力/网络/控制面故障 | 整个 AZ | 跨 AZ 放置、GCS FT、DR |
| 区域 | 云厂商区域故障 | 多 AZ | 多区域 DR |
| 依赖 | Redis/对象存储/DNS/镜像仓库 | 全局或局部 | 多副本、缓存、降级 |

“跨可用区”不是免费的：延迟、带宽、跨区流量费、存储挂载约束都会增加。设计时要先确定**哪些组件必须跨区，哪些可以在单区内做到 N+1**。

### 44.2.2 先写 RTO/RPO

| 对象 | RPO | RTO | 说明 |
|---|---|---|---|
| RayCluster（开发/实验） | 可丢失 | 小时级 | 重建即可 |
| RayCluster（共享生产池） | 分钟级 | 15–30 分钟 | 依赖节点容量和镜像缓存 |
| RayJob（批处理） | 取决于 checkpoint | 重跑时间 | 作业必须幂等 |
| RayService（在线） | 0（模型/配置在 Git） | 1–5 分钟 | 依赖备用集群或新集群容量 |
| History Server 数据 | 分钟级 | 小时级 | 对象存储跨区复制 |
| operator/CRD | 0（Git） | 分钟级 | Helm/Argo CD 恢复 |

没有 RTO/RPO 的“高可用”无法验收。第 31 章给出了升级/迁移视角的检查表；本章补充故障域视角。

## 44.3 控制面：operator 自身的多可用区

### 44.3.1 operator 的 HA 配置

operator 是控制面的写入者。它挂掉不会让已有 Ray Pod 立刻停止，但新的调和、扩缩容、升级、清理都会停。生产建议至少两个副本 + leader election：

```yaml
# kuberay-operator-ha-values.yaml
replicas: 2
leaderElectionEnabled: true
resources:
  requests: {cpu: "1", memory: 1Gi}
  limits: {cpu: "4", memory: 4Gi}
affinity:
  podAntiAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - topologyKey: topology.kubernetes.io/zone
        labelSelector:
          matchLabels:
            app.kubernetes.io/name: kuberay-operator
```

要点：

- **required 反亲和**保证两个副本不在同一个可用区；只有两个可用区时也能工作，但第三个副本会 Pending。副本数不要超过可用区数。
- 只有 leader 调和；follower 待命。leader 所在区故障后，选主需要 `leaseDuration`/`renewDeadline`/`retryPeriod` 的时间，通常是秒级到十几秒。
- operator 的 ServiceAccount/RBAC 必须能在新 leader 上立即使用；不要依赖本地文件。
- 如果 API server 本身不可用，operator 的 HA 也救不了；这时只能等控制面恢复。

### 44.3.2 operator 的 PDB 与优先级

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: kuberay-operator
  namespace: kuberay-system
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: kuberay-operator
```

PDB 只约束**自愿驱逐**（drain、节点升级），不约束节点宕机或可用区故障。它的价值是防止集群升级时把两个 operator 同时赶走。

给 operator 一个较高的 PriorityClass，可以降低它在节点压力下被优先驱逐的概率：

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: platform-critical
value: 100000
globalDefault: false
description: "Platform controllers and Ray heads"
```

不要让业务 RayCluster 使用 `system-cluster-critical`；那会与 Kubernetes 自身组件竞争。

### 44.3.3 控制面的其他单点

- **API server 端点**：托管 Kubernetes 通常提供多 AZ 的 API server；自建集群要确保 kube-apiserver/etcd 的拓扑。
- **admission webhook**：如果平台有 Kyverno/Gatekeeper/OPA，webhook 的 failurePolicy 可能阻塞所有 Ray CR 创建；跨 AZ 部署或设置合理的超时/失败策略。
- **CRD 存储**：etcd 的备份与恢复属于集群级 DR，不在 KubeRay 范围内。
- **镜像仓库/包索引**：operator 启动时才拉镜像；离线环境要预加载到每个可用区的节点。

## 44.4 RayCluster 的跨可用区放置

### 44.4.1 head 放在哪里

head 只有一个 Pod（除非未来 active-passive head 落地），因此“跨可用区”对 head 没有意义。能做的是：

- 不要把 head 固定到某一个容易故障的节点或 Spot 节点；
- 尽量让 head 与 Redis/存储在同一低延迟区域，但不要与全部 worker 同区；
- 给 head 更高的优先级，避免被普通批任务挤掉；
- 用 node affinity/selector 选择“稳定型”节点池，而不是 GPU 抢占型节点池。

```yaml
headGroupSpec:
  template:
    spec:
      nodeSelector:
        node.kubernetes.io/lifecycle: normal
      priorityClassName: platform-critical
      containers:
        - name: ray
          image: rayproject/ray:2.59.0
```

`node.kubernetes.io/lifecycle` 是常见的节点标签约定（不同平台可能不同）；上线前用 `kubectl get nodes --show-labels` 确认。没有这个标签时，nodeSelector 会让 head 永远 Pending。

### 44.4.2 worker 的拓扑打散

worker 是跨 AZ 放置的主角。Kubernetes 的 `topologySpreadConstraints` 按 `topologyKey` 打散 Pod：

```yaml
workerGroupSpecs:
  - groupName: cpu-workers
    replicas: 6
    minReplicas: 3
    maxReplicas: 12
    template:
      spec:
        topologySpreadConstraints:
          - maxSkew: 1
            topologyKey: topology.kubernetes.io/zone
            whenUnsatisfiable: DoNotSchedule
            minDomains: 3
            labelSelector:
              matchLabels:
                ray.io/cluster: raycluster-multiaz
                ray.io/group: cpu-workers
        containers:
          - name: ray
            image: rayproject/ray:2.59.0
            resources:
              requests: {cpu: "2", memory: 8Gi}
```

逐项解释：

| 字段 | 作用 | 常见错误 |
|---|---|---|
| `maxSkew` | 任意两个拓扑域中匹配 Pod 数之差的上限 | 设 0 或过小，导致无法调度 |
| `topologyKey` | 故障域标签，常用 `topology.kubernetes.io/zone` | 用了节点名却想表达 AZ |
| `whenUnsatisfiable` | `DoNotSchedule`（硬约束）或 `ScheduleAnyway`（软约束） | 生产直接硬约束，容量不足时全组 Pending |
| `minDomains` | 至少覆盖多少个域（1.28 beta/default-on，1.30 GA） | 在只有两个 AZ 的集群里写 3 |
| `labelSelector` | 只统计同组 Pod | 选择器过宽，把 head/其他组算进来 |
| `nodeAffinityPolicy`/`nodeTaintsPolicy` | 控制哪些节点参与计算 | 不设置时默认行为可能与预期不同 |

选择建议：

- **开发/弹性池**：`ScheduleAnyway`，优先均衡，不阻塞。
- **生产共享池**：`DoNotSchedule` + 足够的 minReplicas，保证故障域分散；同时接受“容量不足时 Pending”的代价。
- **GPU 池**：先按机型/可用区划分节点池，再在池内打散；不要用一个 selector 跨所有 GPU 型号。

KubeRay 会在创建 Pod 时打上 `ray.io/cluster`、`ray.io/group` 等标签，因此上面的 selector 可以直接使用。不要手写与 KubeRay 命名规则冲突的标签。

### 44.4.3 Pod 反亲和：避免同节点/同机架

`topologySpreadConstraints` 解决“域间均衡”，`podAntiAffinity` 解决“不要靠太近”：

```yaml
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels:
              ray.io/cluster: raycluster-multiaz
              ray.io/group: cpu-workers
```

注意：

- `required` 反亲和会让副本数超过节点数时永久 Pending；通常用 `preferred`。
- 多主机（`numOfHosts > 1`）的同一 replica 反而需要**靠近**：同一机架/同一交换机，减少跨机通信。用 `podAffinity` 按 `ray.io/worker-group-replica-name` 聚拢，而不是反亲和。
- head 与 worker 之间通常不需要反亲和；head 需要和所有 worker 通信，放在同区反而降低延迟。

### 44.4.4 PDB：保护自愿中断，不保护可用区故障

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: raycluster-multiaz-workers
  namespace: default
spec:
  minAvailable: 2
  selector:
    matchLabels:
      ray.io/cluster: raycluster-multiaz
      ray.io/node-type: worker
```

PDB 的作用：

- 节点 drain/升级时，驱逐 API 会检查 PDB；
- 不会阻止节点宕机、AZ 故障、OOM、kubelet 直接杀 Pod；
- 不会让调度器“预留”故障域容量。

因此 PDB 必须和 `topologySpreadConstraints`、节点池容量、GCS FT 一起用，不能单独作为高可用方案。

### 44.4.5 多主机组的放置

多主机 worker（TPU 切片、超大模型）一个 replica 包含多个 Pod，这些 Pod 之间有高带宽/低延迟通信。放置原则：

1. **同一 replica 尽量同机架/同 AZ**：用 `podAffinity` 按 replica 名聚合；
2. **不同 replica 之间打散**：用 `topologySpreadConstraints` 按 `ray.io/worker-group-replica-name` 或 replica index 分散；
3. **不要用 `DoNotSchedule` 的 hostname 反亲和**：一个 replica 有 8 个 host 时，可能没有 8 个节点满足；
4. **确认节点池的拓扑**：TPU/IB 机型通常有 compact placement 或拓扑标签，按云厂商文档使用。

### 44.4.6 放置之后仍会失败的地方

调度成功只代表 Pod 有节点。以下问题仍会发生：

- 节点上的 GPU 驱动/device plugin 未就绪；
- 镜像拉取慢或仓库跨区不可达；
- `/dev/shm` 太小，Ray 对象存储启动失败；
- head 的 GCS 状态在故障后不一致，worker 重连失败；
- CNI/NetworkPolicy 阻断 worker 到 head 的端口。

这些属于第 6、13、35、40 章的内容。跨 AZ 设计不能只看调度器。

## 44.5 GCS FT 与存储的跨可用区真相

### 44.5.1 Redis 后端

GCS FT（Redis）把 GCS 的元数据写到外部 Redis，head 重建后可以从 Redis 恢复。它能不能扛住可用区故障，取决于 Redis 的部署：

| Redis 部署 | 跨 AZ 能力 | 注意 |
|---|---|---|
| 单实例 | 无 | Redis 所在区故障即失去 FT |
| 主从 + Sentinel | 有，取决于选举和网络 | 需要奇数个 Sentinel、跨 AZ 部署、客户端支持 Sentinel |
| Redis Cluster | 有，分片+副本 | 需要至少 3 主 3 从跨 AZ；运维复杂 |
| 托管 Redis（多 AZ） | 取决于服务等级 | 确认 SLA、故障转移时间、连接串 |

KubeRay 只负责注入 Redis 地址和启用 Ray 的 FT；**Redis 自身的高可用是平台责任**。常见错误是把 Redis 和 head 放在同一个 namespace/同一个区，然后宣称“已经 GCS FT”。

还要注意：

- `externalStorageNamespace` 要按集群/租户隔离，避免多个 RayCluster 写同一个 key 空间；
- Redis 的延迟直接影响 GCS 写入；跨区延迟要纳入容量评估；
- Redis 密码/证书轮换要同步更新 Secret；
- 删除 RayCluster 时的 cleanup finalizer 需要能访问 Redis；Redis 不可达会导致删除卡住（第 13、42 章）。

### 44.5.2 RocksDB 后端

RocksDB 后端把 GCS 元数据放在 head 的 PVC 上，不依赖外部 Redis。它的边界：

- **PVC 是区级资源**：EBS/PD/Azure Disk 通常只能在创建它的可用区挂载；节点/区故障后，新 head 在另一个区无法挂载。
- **单写者**：同一 PVC 不能同时被两个 head 挂载（ReadWriteOnce）。
- **适合**：head 进程崩溃、同节点/同区重建；不适合整区灾难恢复。
- **备份**：如果 RocksDB 目录需要跨区恢复，必须做快照/复制，且恢复时保证一致性。

因此，RocksDB 不是“去掉 Redis 就自动跨 AZ”的方案。它减少的是外部依赖，不是故障域。

### 44.5.3 选择矩阵

| 方案 | head 崩溃 | 节点故障 | 单 AZ 故障 | 运维成本 | 适用 |
|---|---|---|---|---|---|
| 无 FT | 重启/重建，元数据丢失 | 重建 | 重建 | 低 | 开发、可重跑作业 |
| Redis FT | 自动恢复 | 自动恢复（若 Redis 可达） | 取决于 Redis 拓扑 | 中高 | 共享生产池 |
| RocksDB FT | 自动恢复（同区） | 取决于 PVC | 通常不可用 | 中 | 单区、无 Redis 环境 |
| 跨集群 DR | 切换 | 切换 | 切换 | 高 | 关键在线服务 |

第 13 章给出了 GCS FT 的配置与恢复时序；本节只强调故障域边界。

## 44.6 存储：区级 PV 与跨区对象存储

### 44.6.1 区级 PV 的约束

大多数块存储是区级的：

- AWS EBS：卷在某个 AZ，跨 AZ 不能直接挂载；
- GCP Persistent Disk：zonal/regional 两种，regional 有复制但仍有拓扑；
- Azure Disk：zonal，跨区需要复制/快照。

Kubernetes 的 CSI topology 会把节点和 PV 的区标签匹配；Pod 调度到错误的区时，会看到 `volume node affinity conflict`。这不是 KubeRay 的 bug，而是存储拓扑的正常行为。

设计建议：

- **无状态数据**放对象存储，不放 PV；
- **checkpoint/模型**写对象存储，PV 只做本地缓存；
- **RocksDB PVC** 绑定到稳定节点池/区，并接受区故障时不可用；
- **需要跨区恢复的数据**用对象存储跨区复制、数据库备份或应用层双写。

### 44.6.2 对象存储是跨区设计的锚点

Ray 生态的大多数持久数据应该放在对象存储：

- 训练数据、checkpoint、Tune 结果；
- Ray Train 的 `storage_path`；
- History Server 的事件/日志；
- 模型权重、tokenizer、LoRA adapter；
- 作业代码（`working_dir`）和依赖包。

对象存储通常自带跨区/跨区域复制。把它作为“唯一事实来源”，Ray Pod 就可以随时重建。

### 44.6.3 备份什么

| 对象 | 方式 | 频率 | 恢复验证 |
|---|---|---|---|
| Ray CR（RayCluster/RayJob/RayService/RayCronJob） | Git + Velero | 每次变更 | 在空集群 apply |
| CRD/operator/Helm values | Git + chart 仓库 | 每次升级 | 全新安装 |
| Secret/证书 | Sealed Secrets/External Secrets + 备份 | 轮换时 | 轮换演练 |
| PVC（RocksDB/缓存） | CSI snapshot | 按 RPO | 挂载到新 Pod |
| 对象存储 | 版本化 + 跨区复制 | 持续 | 随机抽样恢复 |
| History Server | 对象存储生命周期 | 持续 | 查询已删除集群 |
| 镜像 | 镜像仓库跨区复制 | 每次构建 | 从备区拉取 |

备份的验收标准不是“备份任务成功”，而是“在空环境恢复后，RayCluster 能 Ready、RayJob 能重跑、RayService 能返回 200”。

## 44.7 网络：跨 AZ 的延迟、带宽与费用

### 44.7.1 延迟与带宽

同区延迟通常在亚毫秒级，跨区通常在同一区域内 0.5–2 ms 量级（具体取决于云厂商和可用区距离）。对 Ray 的影响：

- GCS 元数据写入/Raylet 心跳：跨区延迟会放大调度延迟；
- 对象传输：大对象跨区会占用带宽并产生费用；
- Serve 请求：head/proxy 与 worker 跨区会增加 TTFT；
- NCCL/集合通信：跨区带宽通常远低于同区，训练应尽量同区/同机架。

原则：

- **head 与 worker 不必强制同区**，但高频通信的 worker 组尽量同区；
- **多主机训练组同区/同机架**；
- **Serve 的 proxy 与副本尽量同区**，或使用拓扑感知路由；
- **大对象走对象存储/共享存储**，不要反复跨区传输。

### 44.7.2 出口与 NAT

每个可用区通常有独立的 NAT Gateway/出口。AZ 故障时：

- 该区 Pod 的互联网出口中断；
- 跨区 NAT 切换需要时间；
- 镜像拉取、包下载、对象存储访问可能失败。

缓解：

- 镜像预拉取/缓存到每个区；
- 使用 VPC Endpoint/Private Link 访问对象存储；
- 依赖包走内部镜像源；
- 作业设计为可重试，避免把网络抖动当永久失败。

### 44.7.3 Service、DNS 与拓扑感知

- head Service 的 EndpointSlice 会包含 head Pod 所在节点；head 重建后端点自动更新。
- 跨区访问 Service 时，kube-proxy/CNI 可能优先本地端点；Ray 的 GCS 地址应使用稳定的 Service DNS，而不是 Pod IP。
- DNS 的 ndots/search domain 会影响解析路径；第 40 章给出了排障命令。
- 如果使用 Gateway/Ingress，确认其数据面跨 AZ 高可用；单区 Gateway 会成为新的单点。

## 44.8 托管 Kubernetes：EKS、GKE、AKS 的差异

托管 Kubernetes 把控制面交给云厂商，但**数据面、节点池、存储、网络、身份仍然由你负责**。KubeRay 的 CRD/控制器行为基本一致，差异主要在：

| 维度 | EKS | GKE | AKS |
|---|---|---|---|
| 节点弹性 | Cluster Autoscaler、Karpenter | Node Auto-Provisioning、Cluster Autoscaler | Cluster Autoscaler、node pool autoscaling |
| GPU | GPU Operator/device plugin、EKS 优化 AMI | GPU Operator/device plugin、GKE GPU 驱动 | GPU Operator/device plugin |
| 身份 | IRSA / EKS Pod Identity | Workload Identity | Microsoft Entra Workload ID |
| 块存储 | EBS CSI、zonal | PD CSI、zonal/regional | Azure Disk CSI、zonal |
| 对象存储 | S3 + VPC Endpoint | GCS + Private Service Connect | Azure Blob + Private Endpoint |
| 网络 | VPC CNI、Security Groups for Pods | VPC-native、Dataplane V2 | Azure CNI/Overlay |
| 专用加速 | Trainium/Inferentia、EFA | TPU、compact placement | ND/NC 系列、InfiniBand |
| 控制面 SLA | 多 AZ 托管 | 多 AZ 托管（视模式） | 多 AZ 托管（视层级） |

### 44.8.1 EKS

- **Karpenter** 按 Pod 的资源形状直接选实例，比 Cluster Autoscaler 更快；但要在 NodePool 里限制 AZ/机型，避免把 GPU worker 放到没有 GPU 驱动的节点。
- **IRSA/Pod Identity** 让 Ray Pod 访问 S3，不需要把 AK/SK 写进 Secret；注意 ServiceAccount 的信任策略。
- **VPC CNI** 的 ENI/IP 配额会影响大规模 Pod 密度；节点池要预留 IP。
- **EBS CSI** 的卷是 zonal；RocksDB PVC 不要跨区迁移。
- **Spot** 适合 worker，不适合 head/Redis；用 `node.kubernetes.io/lifecycle` 或 Karpenter 的 capacity type 标签区分。

### 44.8.2 GKE

- **Autopilot** 会自动管理节点和 GPU 驱动，但对特权容器、hostPath、DaemonSet 有更多限制；先确认 Ray/GPU Operator 的兼容性。
- **NAP** 可以按 Pod 自动建节点池；为 GPU 设置资源上限和机型白名单。
- **Workload Identity** 访问 GCS；**GCS FUSE** 可以作为共享文件系统，但不要把高频小文件 IO 放在 FUSE 上。
- **TPU** 需要拓扑标签（如 `cloud.google.com/gke-tpu-topology`）和 multi-host；放置策略与普通 GPU 不同。
- **compact placement** 用于高性能训练；确认节点池的拓扑策略。

### 44.8.3 AKS

- **节点池**按 GPU 型号/可用区划分；系统节点池与用户节点池分离。
- **Azure CNI** 的 IP 规划要提前做；大规模 Pod 需要足够的子网。
- **Workload Identity** 访问 Blob/Key Vault；不要让 Pod 使用节点身份。
- **Azure Disk** 是 zonal；跨区恢复用快照/Blob。
- **可用区**支持取决于区域和 VM 系列；不是所有 GPU 机型都能跨三个区。

### 44.8.4 托管控制面的“隐藏单点”

- **Webhook/策略引擎**：如果 Kyverno/Gatekeeper 只部署在一个区，控制面故障时可能阻塞所有 CR 写入。
- **Ingress/Gateway 数据面**：很多默认安装是单副本/单区；需要单独做多 AZ。
- **metrics-server/Prometheus**：监控单点不影响业务，但会影响故障判断。
- **镜像仓库/包索引**：跨区复制和缓存是恢复速度的关键。

## 44.9 容量、成本与 AZ 失衡

### 44.9.1 按故障域预留容量

假设业务需要 12 个 worker，分布在 3 个 AZ。为了在单 AZ 故障时仍保留 8 个 worker：

```text
每区正常容量 = 12 / 3 = 4
单区故障后剩余 = 8
若要求故障后至少 8 个，则每区至少 4 个，且另外两区不能已经被占满
```

如果节点池按区独立扩缩，故障后另外两区需要能扩到 8 个；这要求：

- 节点池的 max size 足够；
- 镜像/驱动已缓存；
- 云厂商在该区有 GPU 容量；
- 配额（vCPU/GPU）足够。

公式化：

```text
每区最小容量 >= ceil(总需求 / AZ 数) + 故障余量
故障后可用 >= 总需求 - 单区容量
```

注意“平均分布”不等于“故障后够用”。12 个 worker 平均分到 3 区，单区故障只剩 8 个；如果业务需要 10 个，就必须额外预留。

### 44.9.2 成本项

| 成本 | 来源 | 优化 |
|---|---|---|
| 跨 AZ 流量 | Ray 对象传输、Serve 请求、GCS 心跳 | 同区放置、批量传输、压缩 |
| 闲置 headroom | 为故障预留的节点 | 用低优先级池/Spot 承担可中断负载 |
| 跨区复制 | 对象存储、镜像仓库 | 生命周期策略、只复制关键数据 |
| NAT/出口 | 跨区/公网访问 | VPC Endpoint、私有链接 |
| 多控制面 | 多集群/多 operator | 分片、共享 operator、按租户隔离 |

### 44.9.3 Spot 与 AZ 失衡

Spot 价格/库存按 AZ 波动，容易出现“某个区全是 Spot、另一个区没有容量”。做法：

- head/Redis/operator 用按需或稳定节点池；
- worker 用多机型、多 AZ 的 Spot 池；
- autoscaler 的节点池优先级按 AZ 动态调整（如果云厂商支持）；
- 监控每区可调度 GPU 数和 Pending Pod 的区分布。

## 44.10 灾难恢复模式

### 44.10.1 四种模式

| 模式 | RTO | RPO | 成本 | 适用 |
|---|---|---|---|---|
| 备份/恢复 | 小时级 | 上次备份 | 低 | 开发、可重跑作业 |
| Pilot Light | 30–60 分钟 | 分钟级 | 中 | 关键但可短暂中断 |
| Warm Standby | 5–15 分钟 | 秒/分钟级 | 高 | 在线服务 |
| Active-Active | 秒级 | 近零 | 很高 | 多区域、强合规 |

Ray 的特殊性：

- **GCS 是有状态的**，不适合简单的 active-active 双写；
- **RayService** 可以通过 Gateway/DNS 做双集群切换，但两个集群的 Serve 配置/模型要一致；
- **RayJob** 更适合“失败后重跑”，前提是幂等 + checkpoint；
- **RayCluster** 是长期资源，DR 更多是“重建”而不是“迁移”。

### 44.10.2 恢复顺序

```text
1. 基础设施：网络、节点池、存储、镜像仓库
2. 控制面：Kubernetes、CRD、operator、RBAC、Secret
3. 平台配置：Helm values、NetworkPolicy、Gateway/Ingress、策略
4. 数据：对象存储、PVC 快照、Redis
5. Ray 资源：RayCluster -> RayService/RayJob/RayCronJob
6. 流量：DNS/Gateway 切换、健康检查
7. 验证：Ray status、Serve 200、Job 成功、指标恢复
```

顺序不能反：没有 CRD 就 apply CR 会失败；没有 Secret 就拉不到镜像；没有对象存储就恢复不了 checkpoint。

### 44.10.3 切换与回切

- **DNS**：降低 TTL，切换后观察；不要依赖客户端缓存。
- **Gateway/HTTPRoute**：改 backendRefs 权重；确认健康检查通过再切 100%。
- **RayService**：备用集群的 `serveConfigV2` 必须与主集群一致；切流前先跑 `/health` 和一条真实推理。
- **RayJob**：确认上次运行的 jobId/输出路径，避免重复写；用幂等键。
- **回切**：不要在主集群刚恢复时立刻切回；先观察容量、GCS、Redis、队列。

### 44.10.4 一个最小 DR 演练脚本

```bash
# 1. 记录基线
kubectl get rayclusters,rayjobs,rayservices -A -o wide
kubectl get pods -A -l ray.io/is-ray-node=true -o wide
curl -sS http://<serve-endpoint>/health

# 2. 模拟单区故障：cordon + drain 该区节点（真实故障不要 drain，直接隔离网络/关机）
kubectl get nodes -l topology.kubernetes.io/zone=<zone> -o name | xargs -r kubectl cordon
kubectl get nodes -l topology.kubernetes.io/zone=<zone> -o name | xargs -r kubectl drain --ignore-daemonsets --delete-emptydir-data

# 3. 观察
kubectl get pods -A -o wide | grep -E "Pending|CrashLoop|Terminating"
kubectl get events -A --sort-by=.lastTimestamp | tail -30

# 4. 恢复
kubectl get nodes -l topology.kubernetes.io/zone=<zone> -o name | xargs -r kubectl uncordon
```

真实故障演练应与云厂商的故障注入服务（如 AWS Fault Injection Simulator）配合，不要在共享生产集群上直接关机。

## 44.11 反模式

| 反模式 | 问题 | 正确做法 |
|---|---|---|
| operator 单副本 | 节点故障后无人调和 | 2 副本 + leader election + 反亲和 |
| head 固定在 Spot/单区 | 节点回收即集群失联 | 稳定节点池 + 优先级 |
| 只配 PDB | 以为能扛 AZ 故障 | PDB 只保护自愿驱逐；配合拓扑/DR |
| `DoNotSchedule` + 不足容量 | 整组 Pending | 先保证节点池容量，或改 `ScheduleAnyway` |
| Redis 与 head 同区 | GCS FT 形同虚设 | Redis 跨 AZ 主从/Cluster |
| RocksDB 当 DR | PVC 跨区不可挂载 | 同区 FT + 对象存储备份 |
| worker 跨区大对象传输 | 延迟/费用爆炸 | 对象存储/同区放置 |
| 只备份 CR | 恢复时缺 Secret/CRD/镜像 | 备份完整依赖链 |
| 多集群 active-active 双写 GCS | 元数据冲突 | 单写 + 切换，或业务层分片 |
| 不演练 | 真故障时不知道 RTO | 每季度一次单区故障演练 |

## 44.12 检查表

上线前逐项确认：

- [ ] operator ≥2 副本、leader election、跨 AZ 反亲和、PDB；
- [ ] head 不在 Spot/易故障节点池，优先级高于普通批任务；
- [ ] worker 组配置 `topologySpreadConstraints`，并确认节点池每区容量；
- [ ] `minDomains`/`maxSkew`/`whenUnsatisfiable` 与 AZ 数量匹配；
- [ ] 多主机组同 replica 聚拢、不同 replica 打散；
- [ ] GCS FT 的 Redis 跨 AZ，且清理 finalizer 可访问 Redis；
- [ ] RocksDB PVC 的区级约束已记录，不当作跨区 DR；
- [ ] 对象存储/镜像仓库跨区复制，VPC Endpoint 可用；
- [ ] head/worker/operator 的 PDB 与 PriorityClass 已设置；
- [ ] 备份包含 CR、CRD、Helm values、Secret、PVC、对象存储；
- [ ] RTO/RPO 已写进 SLO，并做过恢复演练；
- [ ] 单区故障演练记录：Pending 数、恢复时间、数据丢失、人工步骤。

完整的跨可用区样例见 `manifests/samples/ray-cluster-multiaz.yaml`；把它当成模板，按你的云厂商标签/节点池修改。

## 44.13 小结

- 跨可用区设计的第一步是写 RTO/RPO，而不是堆副本。
- operator 自身要 HA；PDB 只保护自愿驱逐，不保护 AZ 故障。
- head 是单点，跨 AZ 对它没有意义；能做的是稳定节点池、优先级和 GCS FT。
- worker 用 topology spread/反亲和打散；多主机组反而要聚拢。
- Redis 的高可用是平台责任；RocksDB 的 PVC 是区级的，不是 DR。
- 对象存储是跨区恢复的锚点；备份要覆盖完整依赖链。
- 托管 Kubernetes 的差异主要在节点弹性、身份、存储和网络，CRD/控制器行为一致。
- 单区故障演练是唯一能证明 RTO 的方法；没有演练的 HA 只是配置。

← [第 43 章 从 Issue 到 Release](kuberay教程-43-从Issue到Release-KubeRay贡献、API治理与发布工程.md)

下一部分从“平台怎么部署”转向“数据与模型怎么跑好”：Ray Data 的执行引擎、弹性训练、LLM 推理内部机制。

→ [第 45 章 Ray Data 执行引擎深潜](kuberay教程-45-Ray-Data执行引擎深潜-流式调度、背压、Shuffle与对象存储.md)
