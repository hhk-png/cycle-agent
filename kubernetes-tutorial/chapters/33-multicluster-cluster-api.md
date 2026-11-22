# 第 33 章 多集群、集群生命周期与虚拟集群

> 单集群有天然上限：故障域、合规边界、团队规模、版本升级、GPU 资源池。多集群不是“多装几套 Kubernetes”，而是把集群当成可声明、可替换、可编排的基础设施单元。本章讨论为什么要多集群、如何用 Cluster API 管理生命周期、如何做多集群调度与网络，以及虚拟集群的适用边界。

## 33.1 为什么多集群

多集群的驱动因素通常不是技术，而是约束：

| 驱动 | 说明 |
|---|---|
| 故障隔离 | 一个集群的控制面/升级/事故不影响全部业务 |
| 合规与数据驻留 | 数据必须留在特定区域/云/账户 |
| 团队自治 | 团队拥有独立集群，减少相互阻塞 |
| 环境隔离 | dev/staging/prod 物理隔离 |
| 规模 | 单集群 etcd/API Server/网络有实际上限 |
| 版本与变更 | 分批升级，降低爆炸半径 |
| 边缘 | 大量小站点，网络不稳定 |
| GPU/专用硬件 | 按区域/机型划分资源池 |
| 并购/混合云 | 不同云、不同技术栈共存 |

但多集群有明确代价：

- **运维面翻倍**：升级、证书、监控、备份、策略都要乘以集群数。
- **网络复杂**：跨集群服务发现、DNS、策略、延迟。
- **身份分散**：每个集群一套 RBAC/证书；需要统一身份。
- **成本上升**：控制面、闲置容量、跨集群流量。
- **一致性难**：版本偏斜、配置漂移、策略不一致。

因此，多集群的前提是**自动化**：如果集群还靠手工安装，集群数一多就会失控。

## 33.2 多集群拓扑模式

### 独立集群（Silo）

每个集群独立管理，互不依赖：

```text
Cluster A（团队 A，region us-east）
Cluster B（团队 B，region eu-west）
Cluster C（prod，region ap-southeast）
```

优点：简单、故障隔离强。
缺点：重复建设、难以统一策略、利用率低。

适合：强合规、强隔离、团队完全自治。

### 中心-辐射（Hub-Spoke）

一个管理集群管理多个工作负载集群：

```text
              ┌──────────────┐
              │ Management   │
              │ Cluster API  │
              │ GitOps/Policy│
              └──────┬───────┘
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   Workload A   Workload B   Workload C
```

优点：统一生命周期、策略、可观测。
缺点：管理集群成为关键依赖；网络连通性要求高。

### 联邦（Federation）

多个集群共享调度与策略，应用可以跨集群分布：

- Karmada：PropagationPolicy 把资源分发到成员集群。
- Clusterpedia：跨集群资源检索。
- 多集群 Service/Ingress。

优点：跨集群弹性、故障转移。
缺点：一致性、网络、数据复制复杂；不是所有应用都适合。

### 服务网格（Mesh）

多个集群通过服务网格互联，应用感知不到集群边界：

- Cilium Cluster Mesh。
- Istio multi-cluster（primary-remote、multi-primary）。
- Submariner/Skupper。

优点：跨集群服务发现、mTLS、流量管理。
缺点：网络/身份/版本复杂度高。

实际架构常常组合：Hub-Spoke 管生命周期，Mesh 管跨集群流量，Karmada 管特定批处理/推理负载。

## 33.3 Cluster API：把集群变成 Kubernetes 对象

Cluster API（CAPI）用声明式 API 管理集群生命周期：

```text
Management Cluster
├── Cluster                 # 一个工作负载集群
├── Machine                 # 一台机器（节点）
├── MachineSet              # 一组机器
├── MachineDeployment       # 滚动升级的机器组
├── KubeadmControlPlane     # 控制面
├── InfrastructureCluster   # 云/裸金属基础设施（provider 实现）
└── BootstrapConfig         # kubeadm 配置
```

一个简化示例：

```yaml
apiVersion: cluster.x-k8s.io/v1beta2
kind: Cluster
metadata:
  name: prod-east
  namespace: fleet
spec:
  clusterNetwork:
    pods:
      cidrBlocks: ["10.244.0.0/16"]
    services:
      cidrBlocks: ["10.96.0.0/12"]
  controlPlaneRef:
    apiVersion: controlplane.cluster.x-k8s.io/v1beta2
    kind: KubeadmControlPlane
    name: prod-east-control-plane
  infrastructureRef:
    apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
    kind: AWSCluster
    name: prod-east
---
apiVersion: cluster.x-k8s.io/v1beta2
kind: MachineDeployment
metadata:
  name: prod-east-workers
  namespace: fleet
spec:
  clusterName: prod-east
  replicas: 6
  selector:
    matchLabels:
      cluster.x-k8s.io/cluster-name: prod-east
  template:
    spec:
      clusterName: prod-east
      version: v1.37.0
      bootstrap:
        configRef:
          apiVersion: bootstrap.cluster.x-k8s.io/v1beta2
          kind: KubeadmConfigTemplate
          name: prod-east-workers
      infrastructureRef:
        apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
        kind: AWSMachineTemplate
        name: prod-east-workers
```

> **版本注意**：Cluster API 的 core contract 已从 `v1beta1` 迁到 `v1beta2`，旧版本在 2026 年进入移除窗口；`clusterctl upgrade apply --contract v1beta2` 可迁移管理集群。provider（AWSCluster/AWSMachineTemplate 等）的 CRD 版本独立演进，上面的 `v1beta1` 只是示例，必须以实际 provider 的 served 版本为准。升级前用 `kubectl api-resources | grep cluster.x-k8s.io` 核对。

### 控制器如何工作

1. `Cluster` 控制器协调基础设施与控制面。
2. `KubeadmControlPlane` 创建/升级控制面 Machine。
3. `MachineDeployment` 管理 `MachineSet`，`MachineSet` 管理 `Machine`。
4. `Machine` 控制器调用基础设施 provider 创建 VM/裸金属，调用 bootstrap provider 生成 cloud-init/kubeadm 配置。
5. 节点加入集群，CAPI 更新状态。
6. 升级时 `MachineDeployment` 滚动替换 Machine，遵循 maxSurge/maxUnavailable。

### Pivot

引导流程通常是：

```text
临时 bootstrap 集群 → 创建管理集群 → 把对象“pivot”到管理集群 → 删除 bootstrap
```

`clusterctl move` 负责迁移对象。生产环境要确保管理集群本身高可用、有备份。

### ClusterClass

ClusterClass 把集群模板化：

- 定义控制面/工作节点的模板、版本、基础设施参数。
- 用户创建 Cluster 时只填少量参数。
- 升级 ClusterClass 可以批量滚动更新集群。

这相当于“集群的 Helm Chart”，但由控制器持续调谐。

### 机器健康与修复

- `MachineHealthCheck` 监控节点条件，不健康时删除 Machine，由 MachineSet 重建。
- 修复要防止“误杀”：设置足够长的超时、排除控制面、与 PDB/维护窗口配合。
- 节点启动慢的云环境要留足 grace period。

## 33.4 集群生命周期

一个集群从创建到删除要经历：

```text
规划 → 创建 → 引导 → 加入节点 → 安装附加组件 → 验收
  → 日常运维 → 升级 → 扩缩容 → 故障修复 → 退役/删除
```

### 创建

- 网络 CIDR 规划：Pod/Service CIDR 不能与集群外重叠。
- 控制面规模：3/5 节点 etcd，跨 AZ。
- 节点池：按工作负载/GPU/架构划分。
- 身份：OIDC、证书、IAM/Workload Identity。
- 附加组件：CNI、CSI、CoreDNS、metrics-server、策略、可观测。

### 升级

- 先管理集群，再工作负载集群。
- 控制面 → 节点池 → 附加组件。
- 版本偏斜遵守 Kubernetes 支持窗口。
- 用 CAPI 的滚动升级 + PDB + 维护窗口。
- 升级前备份 etcd/关键 PV，升级后跑验收。

### 扩缩容

- 控制面扩容要谨慎：etcd 成员变更影响 quorum。
- 工作节点用 MachineDeployment/节点池自动伸缩。
- GPU/专用节点池独立伸缩，避免把普通负载调度到昂贵节点。

### 删除

- 先确认数据备份、DNS/流量切走、依赖方已迁移。
- 删除顺序：工作负载 → 附加组件 → 节点 → 控制面 → 基础设施。
- 防止误删：finalizer、删除保护、双人审批。

## 33.5 Karmada：多集群调度与故障转移

Karmada 在多个集群之上提供 Kubernetes 风格的 API：

- **Karmada 控制面**：API Server、controller-manager、scheduler。
- **成员集群**：注册到 Karmada，运行工作负载。
- **PropagationPolicy**：把哪些资源分发到哪些集群。
- **OverridePolicy**：按集群覆盖镜像/副本/配置。
- **ResourceBinding/Work**：分发状态。

示例：

```yaml
apiVersion: policy.karmada.io/v1alpha1
kind: PropagationPolicy
metadata:
  name: payments
spec:
  resourceSelectors:
    - apiVersion: apps/v1
      kind: Deployment
      name: payments
  placement:
    clusterAffinity:
      clusterNames: ["east", "west"]
    spreadConstraints:
      - spreadByField: cluster
        maxGroups: 2
        minGroups: 1
```

故障转移：

- 集群不可用时，Karmada 可以把副本迁移到健康集群（取决于副本策略与数据）。
- 无状态服务容易；有状态服务需要数据复制/切换方案。
- 多集群 Service/Ingress 需要 DNS/GSLB 或 Karmada 的 MultiClusterService。

Karmada 的边界：

- 它不是“跨集群的 etcd”；成员集群仍是独立控制面。
- 网络/存储/数据一致性仍需单独设计。
- 版本兼容、CRD 分发、策略冲突要治理。

### 33.5.1 舰队 API：ClusterSet、Work 与策略分发

多集群管理有三层抽象，容易混淆：

1. **集群生命周期**：创建/升级/删除集群（Cluster API）。
2. **工作负载分发**：把对象放到哪些集群（Karmada PropagationPolicy、Argo CD ApplicationSet、Fleet）。
3. **运行时连通**：跨集群服务发现/网络（Cluster Mesh、Submariner、MCS）。

常见 API/项目：

- **ClusterSet**：把一组集群作为一个逻辑组；CAPI 生态里有 `ClusterSet` 相关提案/实现，Rancher Fleet 也用类似概念。
- **Work API**：Karmada 的 `Work` 对象表示“要在成员集群执行的资源集合”；它是分发状态的底层表示，不建议用户直接写。
- **Fleet**：Rancher Fleet 用 GitOps 管理大量集群，按 cluster selector 部署 Bundle。
- **ApplicationSet**：Argo CD 用生成器按集群/目录批量生成 Application。

选型时先问：

- 你要的是“创建集群”还是“分发应用”？
- 应用是否需要跨集群服务发现/数据复制？
- 策略由 Git 驱动，还是由中心控制面 API 驱动？
- 故障转移是 DNS 层、流量层还是数据层？

一个常见的分层：

```text
Git（应用/策略） → Argo CD/Fleet（分发） → 成员集群（执行）
CAPI（集群生命周期） → 基础设施 provider（VM/网络）
Karmada（跨集群调度/故障转移） → 成员集群
```

不要用 Karmada 替代 GitOps，也不要用 GitOps 替代集群生命周期管理；它们解决不同层的问题。

### 33.5.2 Cluster API 的生产细节

**Provider 矩阵**

- 基础设施 provider：AWS、Azure、GCP、vSphere、OpenStack、Metal3（裸金属）。
- 控制面 provider：KubeadmControlPlane（最常见）、Talos、k3s。
- Bootstrap provider：KubeadmConfig、TalosConfig。

选择 provider 时看：

- 是否支持你需要的 OS/网络/存储。
- 升级/修复路径是否成熟。
- 是否支持 MachinePool（托管节点组）而不是逐 Machine。
- 与云厂商的 IAM/网络集成。

**管理集群的高可用**

管理集群本身是关键基础设施：

- 3 个控制面 + etcd 备份。
- 与工作负载集群分开，避免“管理集群挂了，所有集群无法修复”。
- 限制谁能创建/删除 Cluster/Machine。
- 对 CAPI CRD 做备份/恢复演练。

**MachineHealthCheck 的误杀风险**

- 节点启动慢（GPU/大内存）时，超时太短会反复重建。
- 控制面 Machine 通常不纳入自动修复，需人工确认。
- 网络分区时，管理集群可能误判节点不健康；要有隔离/审批。
- 修复动作要可审计，避免循环重建。

**升级**

- Cluster API 升级分两层：管理集群的 CAPI 组件升级；工作负载集群的 Kubernetes 版本升级。
- 先升级 provider/CRD，再升级 ClusterClass/MachineDeployment。
- 遵守 Kubernetes version skew；控制面先于节点。
- 升级失败要能暂停/回滚（MachineDeployment 的 rollout 策略）。

## 33.6 多集群网络

跨集群通信的常见方案：

### Cilium Cluster Mesh

- 每个集群有唯一 cluster ID/name。
- 通过 etcd 或 Kubernetes API 同步身份与端点。
- 支持跨集群 Service（global service）、NetworkPolicy、mTLS。
- 要求 Pod CIDR 不重叠、集群间网络可达。

### Submariner

- Broker 协调集群信息。
- Gateway 节点建立 IPsec/WireGuard 隧道。
- 跨集群 Service 发现（Lighthouse DNS）。
- 适合混合云/不同网络环境。

### MCS API（Multi-Cluster Services）

Kubernetes 社区标准：

```yaml
apiVersion: multicluster.x-k8s.io/v1alpha1
kind: ServiceExport
metadata:
  name: payments
---
apiVersion: multicluster.x-k8s.io/v1alpha1
kind: ServiceImport
metadata:
  name: payments
```

`ServiceExport` 声明“这个 Service 要暴露给其他集群”，`ServiceImport` 在消费集群生成聚合 DNS/ClusterIP。具体实现由 Cilium/Submariner/云厂商提供。

### 网络设计要点

- **CIDR 不重叠**：Pod/Service/节点网段全局规划。
- **延迟**：跨地域调用要接受 RTT；不要把强一致数据库随意跨域。
- **带宽/出口费**：跨集群流量可能很贵。
- **故障域**：隧道/gateway 本身要冗余。
- **策略**：跨集群 NetworkPolicy、mTLS、身份要统一。

## 33.7 多集群身份、策略与可观测

### 身份

- 统一 OIDC/SSO 作为用户身份源。
- 工作负载身份用 SPIFFE/SPIRE、云 Workload Identity、cert-manager。
- 每个集群的 ServiceAccount 默认不跨集群；需要显式联邦/信任。
- 证书/密钥轮转要有全局视图。

### 策略

- Kyverno/OPA Gatekeeper 策略通过 GitOps 分发到所有集群。
- 策略要有版本、例外、审计；避免“一个集群一个样”。
- 准入策略、网络策略、Pod Security、资源配额要一起治理。

### 可观测

- Prometheus 远程写/Thanos/Mimir 聚合多集群指标。
- 日志用 Loki/Elasticsearch/云日志，带 cluster/region 标签。
- 追踪用 OpenTelemetry Collector，统一资源属性。
- 事件/告警要带集群上下文，避免“同名服务分不清”。
- 成本按集群/租户/团队分摊。

## 33.8 虚拟集群

虚拟集群在共享的物理集群上提供“看起来独立”的控制面：

### vCluster

- 每个 vCluster 运行自己的 API Server（和控制面组件）在 Pod 中。
- Syncer 把 Pod/Service 等资源同步到宿主集群。
- 租户看到独立集群，实际工作负载默认跑在共享节点上。

**部署模式决定隔离强度**（vCluster 0.27+）：

| 模式 | 数据面 | 隔离 | 适用 |
|---|---|---|---|
| 共享节点（默认） | 工作负载跑在宿主节点 | API/RBAC/CRD 隔离；内核/节点共享 | 开发/测试、CI |
| Private Nodes | 租户专属节点 + 自己的 CNI/CSI | 节点级隔离，接近独立集群 | 生产多租户、合规 |
| Auto Nodes | 按需为租户创建节点 | 介于两者之间 | 弹性租户 |
| Standalone | 不依赖宿主集群，独立控制面/数据面 | 最强 | 完全独立但想复用运维模式 |

优点：

- 创建快、成本低（共享模式）。
- 租户隔离 API/RBAC/CRD。
- 适合开发/测试、CI、多租户平台。

缺点：

- **共享节点模式**下，容器逃逸、节点故障、资源争抢仍跨租户；Private Nodes/Standalone 才解决数据面隔离。
- 网络/存储/CRD 兼容性有边界。
- Syncer 是额外故障点。

### Kamaji

- 控制面组件（API Server、etcd、controller-manager、scheduler）作为 Pod 运行在管理集群。
- 工作节点加入独立控制面。
- 比 vCluster 更接近“真集群”，但控制面托管。

适用：需要独立控制面但不想为每个集群付三台控制面机器。

### 选择

| 方案 | 隔离 | 成本 | 适用 |
|---|---|---|---|
| 独立集群 | 强 | 高 | 生产、合规 |
| vCluster Private Nodes/Standalone | 中强到强 | 中 | 生产多租户、强隔离 |
| vCluster 共享节点 | 中（仅控制面） | 低 | 开发/测试、CI |
| Kamaji | 中强 | 中 | 托管控制面、多租户 |
| 命名空间 | 弱 | 最低 | 受信团队 |

虚拟集群不是“免费的生产隔离”。共享节点模式下，容器逃逸、节点故障、资源争抢仍然跨租户；需要数据面隔离时选 Private Nodes/Standalone，并重新评估成本与运维。

## 33.9 灾备与故障转移

多集群灾备的三种模式：

- **备份恢复**：主集群故障，在新集群恢复 etcd/PV/清单。RTO 长，成本低。
- **Active-Passive**：备用集群预热，DNS/流量切换。RTO 中等。
- **Active-Active**：多集群同时服务，数据复制/分片。RTO 短，复杂度高。

关键问题：

1. **数据**：数据库/对象存储/消息队列如何复制？跨集群一致性？
2. **流量**：DNS/GSLB/Anycast/服务网格如何切换？TTL 多长？
3. **脑裂**：网络分区时两边都认为自己是主，如何仲裁？
4. **状态**：PV、CRD、Secret、镜像、策略是否在备用集群可用？
5. **演练**：没有演练的灾备等于没有。

建议从“备份恢复”开始，逐步演进到“预热备用”，不要一开始就追求 active-active。

## 33.10 MiniK8s 的边界

MiniK8s 是单进程集群，不实现多集群联邦。但可以：

- 在同一进程内创建多个 `MiniCluster` 实例，模拟多集群的 API 隔离。
- 用 `ApiClient` 对不同集群执行相同清单，模拟“分发”。
- 用 `scripts/verify_all.py` 的隔离模式理解“每个集群独立状态”。

不实现：Cluster API、Karmada、跨集群网络、虚拟集群、全局身份、灾备编排。这些需要在真实环境验证。

## 33.11 生产清单

- [ ] 多集群的必要性有明确驱动（隔离/合规/规模/边缘），不是为了“架构先进”。
- [ ] 集群创建/升级/删除自动化，禁止手工长期维护。
- [ ] Cluster API/托管服务的管理集群自身高可用、有备份。
- [ ] Pod/Service CIDR 全局规划，不重叠。
- [ ] 跨集群网络方案明确，延迟/带宽/出口费已评估。
- [ ] 统一身份、策略、可观测、成本标签。
- [ ] 虚拟集群的隔离边界已告知租户。
- [ ] 灾备模式与 RTO/RPO 明确，至少演练过一次。
- [ ] 集群退役有数据迁移/流量切换/删除保护流程。
- [ ] 版本偏斜在支持窗口内，升级分批进行。

## 本章小结

- 多集群由故障隔离、合规、团队自治、规模和边缘驱动，代价是运维面与网络复杂度。
- Cluster API 把集群生命周期变成 Kubernetes 对象；管理集群本身也要高可用。
- Karmada 提供多集群分发与故障转移，但不解决数据一致性。
- 跨集群网络需要全局 CIDR、服务发现、策略和身份；MCS API 是标准化方向。
- 虚拟集群降低多租户成本，但共享内核意味着隔离弱于独立集群。
- 灾备从备份恢复开始，逐步演进，必须演练。

## 延伸阅读

- [第 28 章 高可用与灾难恢复](28-ha-disaster-recovery.md)
- [第 32 章 GitOps 与渐进式交付](32-gitops-progressive-delivery.md)
- [第 27 章 可观测性](27-observability.md)
- [第 30 章 性能与规模化](30-performance-scale.md)
- [第 43 章 商业分析 I：市场格局与价值链](43-business-market.md)
