# 第 9 章 · 高级能力

本章覆盖 Kueue 在"配额与队列"之外的扩展能力。这些能力大多通过 feature gate 和额外 CRD 提供，成熟度不同。生产使用前请核对目标版本的 feature state。

## 9.1 Topology-Aware Scheduling（TAS）

TAS 目前是 beta 特性（自 v0.14 起），通常默认开启；关闭时需要同时处理依赖它的 TAS feature gate。具体状态以目标版本为准。

### 9.1.1 问题

AI/ML 训练经常需要 Pod 间高带宽通信。两台机器在同一个 rack 内的带宽，通常远高于跨 rack/跨 block。Kueue 的默认配额只回答"有没有足够的卡"，不回答"这些卡是否在合适的物理拓扑内"。

TAS 要解决：

- 一个 8 卡作业是否能在同一个 rack 内放下？
- 如果放不下，是否可以退而求其次跨 rack？
- 作业运行中节点故障，能否只替换坏节点，而不是整体重调度？

### 9.1.2 Topology 对象

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Topology
metadata:
  name: gpu-topology
spec:
  levels:
  - nodeLabel: cloud.provider.com/topology-block
  - nodeLabel: cloud.provider.com/topology-rack
  - nodeLabel: kubernetes.io/hostname
```

`levels` 从高到低（越往后越细）。节点用这些 label 标识自己在树中的位置。

API 约束：

- 最多 16 层；
- 每个 `nodeLabel` 必须唯一；
- `kubernetes.io/hostname` 只能出现在最低层；
- 只有 hostname 是最低层时 `spec.levels` 才可变；否则修改会被拒绝；
- 新增层前必须先把 label 打到所有相关节点，否则已有 Workload 的放置会失效。

这些约束是"配置后 TAS 全挂/队列 Inactive"的高频原因。

### 9.1.3 关联 Flavor

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: tas-a100
spec:
  nodeLabels:
    node.kubernetes.io/instance-type: a100
  topologyName: gpu-topology
```

只有带 `topologyName` 的 Flavor 才支持 TAS。

### 9.1.4 用户注解

在 Pod 模板上加注解：

```yaml
metadata:
  annotations:
    kueue.x-k8s.io/podset-required-topology: cloud.provider.com/topology-rack
```

含义：这个 PodSet 的所有 Pod 必须放在同一个 rack 内。

其他注解：

| 注解 | 含义 |
|---|---|
| `podset-required-topology` | 必须放在指定层级的同一个 domain |
| `podset-preferred-topology` | 优先放在同一 domain；放不下时逐级放宽 |
| `podset-unconstrained-topology` | 启用 TAS，但不要求同一 domain |
| `podset-group-name` | 多个 PodSet 共享同一个拓扑 domain |
| `podset-slice-required-topology` / `podset-slice-size` | 把 PodSet 切片，每片在一个 domain 内 |
| `podset-slice-required-topology-constraints` | 多层切片约束（alpha） |
| `podset-topology-spreading` | 跨 Workload 打散（alpha） |

几个精确语义：

- `required`：所有 Pod 必须落在指定 level 的**同一个** domain；找不到就整体 Pending，不会降级。
- `preferred`：先尝试同一 domain；放不下时从指定 level 逐级向上放宽，最终可以退化为无拓扑约束。
- `unconstrained`：启用 TAS，但不要求同 domain；按容量铺开。
- `group-name`：多个 PodSet 必须共享同一个 domain；必须与 required 或 preferred 搭配。MiniKueue 的校验会拒绝 `group-name + unconstrained`，因为"共享 domain"与"不要求同一 domain"自相矛盾。
- 注解里的 level 必须存在于 Flavor 的 `Topology.spec.levels`；不在的话该 PodSet/Workload 会 Inadmissible/Pending，Flavor 本身不受影响。
- `slice-required-topology` 与 `slice-size` 必须成对使用；`slice-required-topology-constraints` 与它们互斥。

### 9.1.5 容量计算

TAS 需要知道每个拓扑 domain 的可用容量。它从节点状态推导，而不是像 MiniKueue 那样显式写死：

```text
domain 可用容量
  = Σ 节点 allocatable
  - 已准入 TAS Workload 的预留
  - 非 TAS Pod 的占用（DaemonSet、Deployment、系统组件）
  - 已绑定但未被 TAS reservation 覆盖的非终止 Pod
  - 不健康/不可调度节点的容量
```

最后一条由 `TASResidualPodAccounting` 控制。该 gate 截至 v0.20 稳定线仍是 **alpha、默认关闭**（在 v0.19/v0.20 线引入，具体 patch 以 release notes 为准）；0.21-devel 中可能有阶段变化，升级前以 release notes 为准。关闭它时只做 reservation-only 记账，非 TAS Pod 仍占用物理容量，但已绑定未覆盖的 TAS Pod 可能被重复计算。

### 9.1.6 隐式 TAS 与混合 Flavor

TAS 有两种使用方式：

1. **TAS-only ClusterQueue**：CQ 引用的**所有** ResourceFlavor 都设置了 `spec.topologyName`。此时未加 TAS 注解的 PodSet 也隐式使用 TAS，做 unconstrained 放置（最细层级，`LeastFreeCapacity` 策略）。
2. **混合 Flavor CQ**：CQ 里只要有一个被引用的 ResourceFlavor（即使 `nominalQuota: 0`）没有 `spec.topologyName`，隐式 TAS 就关闭：
   - 未注解的 PodSet 只能分到非 TAS Flavor；
   - 显式注解的 PodSet 只能分到 TAS Flavor；
   - 不能把一个 PodSet 的资源拆到 TAS/非 TAS 两种 Flavor。

这带来一个容易踩的坑：**不要**把 `cpu` 放在非 TAS resourceGroup、`nvidia.com/gpu` 放在 TAS resourceGroup。一个同时请求两者的 Pod 既不能走 TAS 也不能走非 TAS，永远无法准入。资源组应该把同一 Pod 需要的资源放在一起。

### 9.1.7 节点故障与热替换

如果最低层是 hostname，TAS 会为 Pod 固定节点。节点故障时，一组 feature gate 决定行为：

| Gate | 状态 | 作用 |
|---|---|---|
| `TASFailedNodeReplacement` | beta 默认开 | 为故障节点找替代，避免整体重调度 |
| `TASReplaceNodeOnPodTermination` | beta 默认开 | Pod 终止时立即触发替换 |
| `TASFailedNodeReplacementFailFast` | beta 默认开 | 只尝试一次替换，失败就整体驱逐 |
| `TASReplaceNodeOnNodeTaints` | beta 默认开 | 节点出现调度污点时触发替换 |
| `TASReplaceNodeDueToNotReadyOverFixedTime` | 已弃用 | 旧版"NotReady 超时"触发 |

这些 gate 有交互矩阵，不要只关一个。典型行为：

- 单节点故障：只替换坏节点上的 Pod；
- 多个节点故障且找不到替代：整体驱逐 Workload；
- `FailFast` 开启：不无限重试，失败就重排队；
- 节点污点：`NoExecute` 会驱逐 Pod；`NoSchedule` 不会驱逐已运行 Pod，是否替换取决于 `TASReplaceNodeOnPodTermination`。

### 9.1.8 更高级的 TAS 能力

| 能力 | Gate | 状态 | 说明 |
|---|---|---|---|
| Balanced placement | `TASBalancedPlacement` | alpha 默认关 | 先找最优 domain 集合，再尽量均匀分布 Pod |
| Multi-layer topology | `TASMultiLayerTopology` | beta 默认开 | 最多 3 层 slice 约束，内层 slice 必须整除外层 |
| Topology spreading | `TASTopologySpreading` | alpha 默认关 | 跨 Workload 打散，类似 PodTopologySpread |
| Partial slices | `TASPartialSlices` | beta 默认开（v0.20） | 尾部不足一个 slice 的 Pod 也能放置 |
| Grouped PodSet slicing | `TASGroupedPodSetSlicing` | beta（按版本核对） | leader/worker 分组 PodSet 的 slice 共置 |
| 全层级节点可行性 | `TASNodeFeasibilityForAllLevels` | beta 默认开 | 每一层都检查节点/设备可行性 |
| 拓扑树缓存 | `TASCacheTopologyTree` | beta 默认开 | 缓存 Topology tree，降低大规模下的重建成本 |
| Profile 混合 | `TASProfileMixed` | alpha | 同一 CQ 内混合 TAS/非 TAS Profile |
| Flavor 扫描进度 | `FlavorFungibilityPreserveScanProgress` | beta 默认开（v0.20） | admission/eviction 后保留 Flavor 扫描进度，避免饥饿/抖动 |
| Partial slices | `TASPartialSlices` | beta 默认开 | slice 不整除时，尾部 Pod 组成一个 partial slice |
| DRA device feasibility | `KueueDRADeviceFeasibility` | alpha 默认关 | 放置时检查节点是否真的能分配 DRA 设备 |

多层的例子：

```yaml
annotations:
  kueue.x-k8s.io/podset-slice-required-topology-constraints: |
    - topologyLevel: cloud.provider.com/topology-block
      sliceSize: 32
    - topologyLevel: cloud.provider.com/topology-rack
      sliceSize: 8
```

含义：整个 PodSet 先按 block 切成 32 Pod 的片，每片再按 rack 切成 8 Pod 的子片。内层 `sliceSize` 必须整除外层。

Partial slice 的例子：10 个 Pod、`sliceSize: 4`，会切成 4+4+2，最后 2 个 Pod 组成一个 partial slice，仍然放在同一个 domain 内。它按实际 Pod 数计费，不会虚占 4 个 Pod 的容量。

### 9.1.9 TAS 与 DRA

DRA 设备不在 Pod 的普通 requests 里，TAS 默认看不到设备。`KueueDRADeviceFeasibility` 会在 TAS 放置时检查节点是否能分配 DRA 设备，避免"配额够、节点没设备"的假象。

它只检查"一个 Pod 能否在某个节点上分配设备"，不覆盖：

- 一个节点多 Pod 的设备竞争；
- 抢占时设备释放；
- 所有 Kubernetes DRA 特性（如 AdminAccess、部分 binding conditions）；
- 分配耗时——每个候选节点都要试一次分配，CEL selector 慢会拖慢整个调度周期。

### 9.1.10 MiniKueue 中的 TAS

MiniKueue 实现了一个结构化的 TAS-lite：

- `api.Topology` 定义层级（如 rack/hostname），`api.TopologyDomain` 定义叶子容量；
- `PodSetTopology` 支持 `Required`、`Preferred`、`Unconstrained`、`Group`；
- `PodSetTopology.SliceSize` 把一个 Required PodSet 切成若干片，每片放进一个 domain；`Spreading` 选择当前用量最少的 domain；
- `scheduler.planTopology` 在 Flavor 分配成功后做拓扑放置；
- `Scheduler.TopologyUsage` 按 Workload 的 `TopologyAssignments` 聚合 domain 用量，并把粗层分配下钻、细层分配上卷，保证 rack/hostname 不双计；
- 抢占模拟会把 victim 从拓扑用量中排除；
- 第 19 章的扩展实验继续实现热替换和节点健康模型。

它实现了 partial slice：`count=10`、`SliceSize=4` 会切成 4+4+2，最后一片按实际 2 个 Pod 计费，不虚占 4 个 Pod 的容量。它没有实现真实 TAS 的节点/非 TAS Pod 容量推导、隐式 TAS、混合 Flavor 语义、多层 slice 约束和 DRA 设备可行性；这些在第 15 章列为差距。另外，MiniKueue 的 slice 固定按"最空 domain 优先"选域，`Spreading` 只控制整个 PodSet 的选域；Unconstrained 默认按 domain 名字 first-fit，而不是真实 TAS 的 LeastFreeCapacity/Balanced。`go run ./cmd/minikueue -scenario tas` 演示 4 卡作业跨两台 host 占满一个 rack；`-scenario tas-slice` 演示 slice + 打散。

## 9.2 Dynamic Resource Allocation（DRA）

### 9.2.1 背景

Kubernetes DRA 用 `ResourceClaim`/`DeviceClass` 描述 GPU、FPGA、网卡等设备，比扩展资源更灵活。Kueue 需要在配额层面对 DRA 设备计数。

### 9.2.2 两条路径

**路径一：ResourceClaimTemplate**

Pod 显式引用 `ResourceClaimTemplate`。Kueue 读取 `deviceClassName`，通过 Configuration 的 `deviceClassMappings` 映射到逻辑资源名，按 `count` 计费。

```yaml
# Kueue Configuration
resources:
  deviceClassMappings:
  - name: nvidia.com/gpu
    deviceClassNames: ["gpu-a100", "gpu-h100"]
    sources:
    - counter:
        name: gpu-memory
        driver: nvidia.com/gpu
        deviceSelector:
          cel:
            expression: device.attributes["nvidia.com"].memory > 40
```

**路径二：扩展资源**

Pod 仍然写 `nvidia.com/gpu: 1`，但底层由 DRA 提供。Kubernetes 的 `DeviceClass.extendedResourceName` 让 kube-scheduler 自动创建 ResourceClaim。Kueue 自动发现这个映射，不需要 `deviceClassMappings`；如果同时配了映射，会以映射的逻辑名为准，并合并计费，避免重复。

两条路径不要对同一个 DeviceClass 同时配置，否则容易出现"看起来只请求了一次、账本扣了两次"。

### 9.2.3 配额计费模式

| 模式 | 计费依据 | 适用 |
|---|---|---|
| 设备数（默认） | `count` | 独占整卡 |
| Counter | 设备 `consumesCounters` 之和 | 分区设备（MIG、MPS） |
| Capacity | `capacity.requests` | 共享设备（按显存/算力切分） |

Counter 和 Capacity 不能在同一个 DeviceClass 的同一 mapping 里混用。Capacity 模式适合"多个 Workload 共享一张卡的不同显存"；Counter 模式适合"一张卡被切成多个固定 partition"。

### 9.2.4 版本与 feature gate

- DRA 需要 Kubernetes 1.34+ 的 `resource.k8s.io/v1`。
- Kueue 0.18 起把 DRA gate 改名为 `KueueDRAIntegration` / `KueueDRAIntegrationExtendedResource`，避免和 Kubernetes 同名 gate 冲突。
- 扩展资源路径自 v0.19 起 beta 默认开启。
- partitionable devices 需要 Kubernetes 1.35+ 和 `DRAPartitionableDevices`。
- consumable capacity 需要 Kubernetes 1.36+ 和 `DRAConsumableCapacity`，Kueue 侧是 `KueueDRAIntegrationConsumableCapacity`（0.21-devel 计划 beta；v0.20 稳定线的默认值需按 release notes 核对，不要假设默认开启）。
- 具体 gate 名和默认值以版本为准。

### 9.2.5 调度 gap 与限制

Kueue 只能保证"配额上允许"，不能保证 kube-scheduler 一定能分配到具体设备。`KueueDRADeviceFeasibility` 可以在 TAS 放置时补上节点设备可行性检查，但它不是万能的。

DRA 侧的已知限制：

- 只支持 `ResourceClaimTemplate` 引用，不支持 Pod 直接引用 `ResourceClaim`；
- 只支持 `ExactCount` 分配模式，`All` 模式不支持；
- `adminAccess: true` 的设备请求在 v0.20 起按**零配额**计费（管理员访问不计入租户配额）；

### 9.2.6 一个可运行的骨架

`examples/kueue/dra.yaml` 把 DeviceClass、ResourceClaimTemplate、ClusterQueue、LocalQueue 和 Job 串起来；映射片段在 `examples/config/dra-device-mapping.yaml`。注意配额账本只认逻辑资源名：

```yaml
# Configuration（不是 ClusterQueue 字段）
resources:
  deviceClassMappings:
  - name: nvidia.com/gpu
    deviceClassNames: ["gpu.example.com"]
---
# ClusterQueue
resourceGroups:
- coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
  flavors:
  - name: dra-flavor
    resources:
    - name: nvidia.com/gpu
      nominalQuota: 4
```

没有 DRA driver 时只能观察对象结构；MiniKueue 用 `go run ./cmd/minikueue -scenario dra -ticks 2` 演示同一套"DeviceClass → 逻辑资源"计费，不分配真实设备。
- `DeviceTaintRule` 在 v0.20 起支持，由 `KueueDRAIntegrationDeviceTaints` + `KueueDRADeviceFeasibility` 控制；
- 设备 `constraints`（MatchAttribute）仍有限制，按目标版本 API reference 核对；
- `firstAvailable` 请求的计费有边界情况；
- 没有设备可行性检查时，TAS 可能把 Pod 放到没有设备的节点；
- 抢占时设备释放不是即时/精确的。

排障顺序：先看 Kueue 是否计费、再看 TAS 是否放行、最后看 kube-scheduler/DRA driver 是否分配成功。这三层失败看起来都像"Pod Pending"。

### 9.2.6 MiniKueue 中的 DRA-lite

MiniKueue 不实现真实设备分配，只实现"设备请求 → 逻辑配额资源"的计费映射：

```go
type DeviceRequest struct {
    DeviceClass string
    Count       int32
}
```

- `ClusterQueueSpec.DeviceClassMappings` 把 `gpu-a100` 映射到 `nvidia.com/gpu`；
- `Workload.TotalUsage` 把 `PodSet.Devices` 按 `count × pod count` 计入逻辑资源；
- 同一资源如果既写在 `Requests` 又通过设备请求，取两者较大值，避免重复计费；
- Submit 时会把 CQ 的映射复制到 Workload，保证后续账本不依赖 CQ 的后续修改；
- 场景 `dra` 演示：配额 6 卡，两个各请求 4 卡的 Job，第二个 Pending。

它不读取 `ResourceClaimTemplate`、不做设备分配、不检查设备健康/污点，也不支持 Counter/Capacity 计费模式。这些是第 15 章列出的差距。

## 9.3 ProvisioningRequest

### 9.3.1 作用

ProvisioningRequest 是一个 AdmissionCheck 控制器，用于：

- 请求 Cluster Autoscaler/云容量控制器扩容；
- 等新节点 Ready；
- 再允许 Workload 运行。

它把"逻辑配额"和"物理容量"连接起来。

### 9.3.2 流程

```text
Workload QuotaReserved
  → ProvisioningRequest 创建
  → 云控制器扩容
  → 节点 Ready
  → AdmissionCheck Ready
  → Workload Admitted
  → Pod 创建
```

### 9.3.3 Provisioning class

ProvisioningRequest 的行为取决于 `provisioningClassName`：

| Class | 行为 |
|---|---|
| `check-capacity.autoscaling.x-k8s.io` | 只检查现有容量；没有就失败，不扩容 |
| `best-effort-atomic-scale-up.autoscaling.x-k8s.io` | 请求扩容；能否原子扩容取决于云厂商 |
| 厂商自定义 class | 由驱动/厂商实现，能力差异大 |

"best effort" 意味着它不保证严格 gang：云厂商可能只扩出部分节点。真正的 all-or-nothing 仍要靠配额 + TAS + waitForPodsReady。

### 9.3.4 失败与重试

- 临时失败（云容量不足）：`Retry`，释放配额，退避重试。
- 永久失败：`Rejected`，停用 Workload。
- 每次重试创建新的 ProvisioningRequest，名字通常是 `<workload>-<check>-<attempt>`。
- 退避由 `ProvisioningRequestConfig.spec.retryStrategy` 控制：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ProvisioningRequestConfig
metadata:
  name: prov-config
spec:
  provisioningClassName: best-effort-atomic-scale-up.autoscaling.x-k8s.io
  managedResources:
  - nvidia.com/gpu
  retryStrategy:
    backoffLimitCount: 5
    backoffBaseSeconds: 60
    backoffMaxSeconds: 1800
```

默认值：`backoffLimitCount=3`、`backoffBaseSeconds=60`、`backoffMaxSeconds=1800`；`retryStrategy: null` 等价于全部用默认。`backoffLimitCount` 用完后再失败，Workload 会被停用，需要人工重新激活。生产上要监控 `retryCount` 和 ProvisioningRequest 的失败率。

`ProvisioningRequestConfig.spec.podSetMergePolicy` 控制多个 PodSet 如何合并成扩容请求：

| 值 | 语义 |
|---|---|
| `IdenticalPodTemplates` | 只有 Pod 模板完全相同的 PodSet 才合并 |
| `IdenticalWorkloadSchedulingRequirements` | 调度需求（资源、亲和、拓扑等）相同的 PodSet 合并 |

字段名以目标版本 API reference 为准。

### 9.3.5 PodSet 更新

ProvisioningRequest 可以通过 `podSetUpdates` 修改 Pod 模板，例如加 annotation 让 Cluster Autoscaler 知道使用哪个 request。

注意区分两个 `podSetUpdates`：

- `AdmissionCheckState.podSetUpdates`：检查控制器对单个 Workload 的增量建议；
- `ProvisioningRequestConfig.spec.podSetUpdates`：ProvisioningRequest 的模板合并策略/注入规则。

两者都可能注入 labels/annotations/nodeSelector/tolerations，但所有权和生效时机不同。

### 9.3.6 MiniKueue 的 ProvisioningRequest-lite

MiniKueue 用 `controller.ProvisioningConfig` 模拟一个 ProvisioningRequest 控制器：

```go
e.Provisioning = &controller.ProvisioningConfig{
    CheckName:  "provisioning-check",
    DelayTicks: 2, // 扩容/置备需要的时间，也是重试退避
    Failures:   1, // 前 N 次尝试以 Retry 结束
}
```

行为：

1. Workload 预留配额后，AdmissionCheck 进入 `Pending`，配额保持占用；
2. 若 `retryCount < Failures`，检查转为 `Retry`，释放预留，按 `DelayTicks` 重排队；
3. 重新预留后，若重试次数已用完，等待 `DelayTicks` 再把检查置为 `Ready`；
4. 全部检查 Ready 后 Admitted；
5. 重试计数跨预留保留，事件 `ProvisioningRetry` / `ProvisioningReady` 可观测。

运行：

```bash
go run ./cmd/minikueue -scenario provisioning-request -ticks 5
```

预期时间线：tick 1 预留后第一次尝试失败；tick 2 退避；tick 3 重新预留；tick 4 等待置备；tick 5 Admitted。

它没有真实的 ProvisioningRequest 对象、云扩容、`ProvisioningRequestConfig.retryStrategy`、`backoffLimitCount` 或 `podSetUpdates`。指数退避由 `Engine.Backoff` 单独模拟：

```go
e.Backoff = &controller.BackoffConfig{BaseTicks: 1, MaxTicks: 60, Multiplier: 2}
```

`Backoff` 影响抢占/驱逐以及未显式指定延迟的 AdmissionCheck Retry；显式的 `requeueAfterSeconds` 仍然优先。

## 9.4 MultiKueue

### 9.4.1 架构

```text
Manager Cluster
  ├── Kueue + MultiKueue AdmissionCheck
  ├── 连接多个 Worker Cluster
  └── 维护 Workload 的调度状态

Worker Cluster 1..N
  ├── 独立 Kueue
  └── 实际运行 Job
```

### 9.4.2 流程

1. 用户在 manager 提交 Job；
2. manager 的 Workload 获得 QuotaReservation；
3. MultiKueue 根据 dispatcher 选择 worker；
4. 在 worker 创建 Workload/Job；
5. 某个 worker 准入后，manager 清理其他副本；
6. manager 同步状态；
7. 完成后清理。

### 9.4.3 Dispatcher

- **AllAtOnce**（默认）：复制到所有 worker，谁先准入用谁。
- **Incremental**：分批提名，默认每批最多 3 个，5 分钟没有准入就扩大一批。集群顺序按 `MultiKueueConfig.spec.clusters`；`MultiKueueIncrementalDispatcherRespectConfigOrder`（v0.19 beta 默认开）保证这个顺序，关掉后按字母序。`stepSize` 需要开启 `MultiKueueIncrementalDispatcherConfig` gate 后配置。
- **External**：由外部控制器写 `status.nominatedClusterNames`。需要 `WorkloadRequestUseMergePatch` gate 或使用 `kueue-admission` field manager，否则 SSA 字段所有权会阻止外部写入。

Incremental 的 `stepSize: 1` 配置示例：

```yaml
multiKueue:
  incrementalDispatcherConfig:
    stepSize: 1
```

这样会先只提名第一个（最便宜的 on-prem）集群，5 分钟没准入再提名下一个。

### 9.4.4 状态字段

- `status.nominatedClusterNames`：正在考虑的 worker；
- `status.clusterName`：最终选中的 worker，设置后不可变。

### 9.4.5 安全

worker 的 kubeconfig 来源：

| 来源 | 推荐度 | 说明 |
|---|---|---|
| `ClusterProfile` | 生产推荐 | 联邦凭据发现 |
| `Secret` | 生产推荐 | kubeconfig 存 Secret |
| `Path` | 仅开发 | 控制器 Pod 文件系统路径，风险高 |

`MultiKueueKubeConfigPathValidation` 限制 `Path` 只能指向固定前缀，防止路径穿越。

### 9.4.6 限制与调优

- manager 不能同时作为自己的 worker（角色共享不支持）；但可以在 manager 上用专用 CQ 跑普通 Job。
- 某些没有 `managedBy` 支持的 Job 类型，manager 上的状态可能不准确。
- 配额规划要合理：manager 的 quota 应约等于所有 worker 的总可用量。太低浪费 worker，太高会提名一批注定放不下的 Workload。
- `multiKueue.workerLostTimeout` 决定 worker 失联多久后清理；太短会误杀网络抖动，太长会占着 manager 配额。
- `multiKueue.gcInterval` 控制已完成后远端对象的清理频率。
- 跨集群状态同步有延迟，监控 `multikueue_workloads_dispatched_total` 和远端 Workload 的 age。
- 每个 worker 的 Kueue 版本/配置最好一致；至少 CRD 兼容。

### 9.4.7 MiniKueue 中的 MultiKueue-lite

`mini-kueue/internal/multikueue` 用多个独立 `Engine` 模拟 worker：

```text
Manager
  ├── Workload（唯一源对象）
  ├── Workers []Worker{Name, Engine, Workload, Down}
  ├── Config{Dispatcher, StepSize, WorkerLostTimeout}
  ├── Nominated / Selected
  └── Tick:
        selected 检查 → 失联超时 → 清理陈旧副本
        → nominate（跳过 Down）→ Submit 副本
        → Tick 健康 worker → 第一个 Admitted 胜出
        → 清理其他健康 worker 的副本
```

行为：

- `AllAtOnce`：第一轮复制到所有健康 worker；
- `Incremental`：第 N 轮提名前 `N × StepSize` 个健康 worker，没选中就继续扩大；
- 选中后其他健康副本通过 `Engine.DeleteWorkload` 删除；
- 重复 tick 不会重复创建副本；
- `Down` worker 不参与提名/不被 tick；选中 worker 失联超过 `WorkerLostTimeout` 后重新分发；
- 失联 worker 恢复后，其陈旧副本被清理；
- 场景 `multikueue` 用 worker-1（1 CPU）和 worker-2（4 CPU）演示第一个 worker 放不下、第二个胜出；
- 场景 `multikueue-failover` 演示选中 worker 失联、超时切换和恢复清理。

它不实现真实的跨集群 API、kubeconfig/Secret、网络分区、连接退避、状态同步延迟、重复执行语义和 manager 自身故障。它只保留"提名、单一选中、失联切换、失败清理"的主干。

## 9.5 Concurrent Admission

### 9.5.1 问题

一个 Workload 可能首选 reservation Flavor，但当前只有 spot 可用。传统 Kueue 会先在 spot 上准入，之后即使 reservation 空出来，也不会自动迁移。

Concurrent Admission 允许：

- Workload 同时在多个 Flavor 上尝试准入；
- 先在某个 Flavor 上运行；
- 更优 Flavor 可用时迁移。

### 9.5.2 Parent/Variant Workload

- 原始 Workload 变成 **Parent**；
- 每个 Flavor 创建一个 **Variant**；
- Variant 独立准入；
- Parent 聚合状态，Job 集成只关心 Parent。

### 9.5.3 迁移策略

```yaml
spec:
  concurrentAdmissionPolicy:
    migration:
      mode: TryPreferredFlavors
      constraints:
        lastAcceptableFlavorName: reservation
```

- `TryPreferredFlavors`：只支持的模式；
- `lastAcceptableFlavorName`：限制最多迁移到哪个 Flavor。

### 9.5.4 约束

- 需要 `ConcurrentAdmission` feature gate；
- 只能用于 `BestEffortFIFO`；
- 只能有一个 resource group；
- 最多 32 个 Flavor；
- `concurrentAdmissionPolicy` 创建后不可变；
- 每个 Variant 只能用一个 Flavor；
- 异构 PodSet（例如 CPU head + GPU worker）不支持；
- 只为"能容忍中断"的 Workload 开启：迁移会驱逐/重建 Pod，训练任务要考虑 checkpoint。

### 9.5.5 性能

Kueue 会为 CQ 的**每个** Flavor 创建一个 Variant，即使该 Flavor 明显放不下。假设：

```text
1000 个 CQ × 32 个 Flavor = 32000 个 Variant Workload
```

控制器、调度器和 apiserver 的负载都会成倍增加。官方文档明确要求在数千 CQ 的规模下做真实峰值压测。不要把它当成"免费的重试"。

### 9.5.6 什么时候值得用

- spot 先跑起来、reservation 空出后迁移；
- 多 zone 同构 Flavor，先到先得；
- 准入检查很慢，想并行跑多个 Flavor 的检查。

不值得用的场景：严格 gang、异构 PodSet、StrictFIFO 顺序、对驱逐零容忍。

### 9.5.7 MiniKueue 中的 Concurrent Admission-lite

MiniKueue 不创建 Parent/Variant，而是在一个 Workload 上做"只向更优 Flavor 迁移"：

1. 正常准入时按 Flavor 顺序选第一个可行的（例如 reserved 没配额，落到 spot）；
2. 每个 tick 在调度前检查已准入/已预留的 Workload；
3. 用 `DirectAdmission` 排除自身后重新计算，如果更靠前的 Flavor 现在可行，且 `LastAcceptableFlavorName` 允许，就释放旧预留并重新预留到更优 Flavor；
4. 只允许迁移到更小索引，因此不会在 Flavor 之间来回抖动；
5. 迁移不重置 `AdmittedAt`，累计最大执行时间不会被"刷新"。

场景 `concurrent-admission`：reserved 初始 0 配额，Job 先跑在 spot；操作员把 reserved 提到 2 CPU 后，下一 tick 自动迁移到 reserved。

它没有 Variant 的并行准入、Parent 状态聚合、AdmissionCheck 重新执行策略和迁移中断成本模型；这些是第 15 章的差距。

## 9.6 Dynamic Quota Orchestration（DQO）

> 前置：`CapacityProvider`/`DynamicQuotaOrchestrator` 是 alpha API，默认安装清单不包含它们。先按 3.2.1 安装 alpha CRD，并开启 `DynamicQuotaOrchestration` gate，否则 apply 会报 `no matches for kind`。

### 9.6.1 目标

让配额来自"实际可用容量"，而不是管理员手写的固定数字。

### 9.6.2 对象

```yaml
apiVersion: kueue.x-k8s.io/v1alpha1
kind: CapacityProvider
metadata:
  name: cloud-gpu
spec:
  controllerName: example.com/capacity
  orchestratedFlavors: ["a100"]
status:
  capacity:
    flavors:
    - name: a100
      resources:
      - name: nvidia.com/gpu
        quantity: "64"
  conditions:
  - type: CapacitySynchronized
    status: "True"
    observedGeneration: 1
```

```yaml
apiVersion: kueue.x-k8s.io/v1alpha1
kind: DynamicQuotaOrchestrator
metadata:
  name: gpu-orchestrator
spec:
  capacityDiscovery:
    providers:
    - name: cloud-gpu
      effectiveCapacityMultiplier: "0.95"
  capacityDistribution:
    subtreeRootQuotaRef:
      kind: Cohort
      name: root
```

### 9.6.3 两阶段

1. **Discovery**：对每个 provider，把报告容量乘以 `effectiveCapacityMultiplier`，按 flavor/resource 汇总到 `status.effectiveCapacity`。
2. **Distribution**：对子树中每个 flavor/resource，按各 CQ/Cohort 的 nominal 比例切分；向下取整（CPU 到 1m，其他到 1），余数按最大余数分配，平局给 UID 更小的对象。

几个精确规则：

- **扁平分配**：子树中每一层 CQ 和 Cohort 共享同一个池；Cohort 分到的份额不会再分给子节点。
- **不编排的 Flavor**：继续用 spec。
- **provider 没报告的资源**：effectiveQuota = 0。
- **所有 CQ nominal 都是 0**：effectiveQuota = 0。
- **borrowingLimit/lendingLimit 从 spec 复制**；CQ 的 lendingLimit 会被 effective nominal 截断。
- **不处理 resource transformation/DRA mapping**；调度器仍按原逻辑应用。

### 9.6.4 条件与冲突

| 条件 | Reason | 含义 |
|---|---|---|
| `CapacitySynchronized` | `Synchronized` | provider 状态有效 |
| | `SourceUnavailable` | 数据源不可用 |
| | `InvalidCapacity` | 容量非法 |
| | `Misconfigured` | 配置错误 |
| `EffectiveCapacityComputed` | `Computed` / `ProviderNotReady` / `Misconfigured` / `AggregationFailed` | Discovery 结果 |
| `Distributed` | `QuotasDistributed` / `EffectiveCapacityNotComputed` / `ConflictingDynamicQuotaOrchestrator` / `EffectiveQuotasConflict` / `Misconfigured` | Distribution 结果 |

冲突规则：两个 orchestrator 管理重叠子树时，根更高的赢；同根时创建更早的赢；再平局时 UID 更小的赢。赢家即使分发失败也保持优先，其他 orchestrator 不会接管。

### 9.6.5 限制

- alpha；
- 容量下降不会自动驱逐已准入 Workload；
- `status.effectiveQuotas` 可能残留：删除 orchestrator、改根、分发失败时，旧值会继续生效，直到手工清理；
- provider 必须报告子树里配置的所有资源；漏报会变成 0；
- 每个 orchestrator 最多 8 个 provider；每个 provider 最多 64 个 flavor；每个 flavor 最多 64 个资源；effectiveCapacity 最多 128 个 flavor；
- 只影响 DQO 管理的 Flavor/资源。

MiniKueue 的 DQO-lite 实现了 Discovery + Distribution 的核心：`CapacityProvider` 报告容量，Engine 每个 tick 按静态 nominal 比例分配给 CQ，调度器使用 `EffectiveQuotas`。它没有实现真实 DQO 的 provider 状态、余数分配规则、冲突优先级、容量下降驱逐和有效配额清理。`go run ./cmd/minikueue -scenario dynamic-quota` 可以观察 6000 的容量如何在两个 nominal 4000 的 CQ 之间变成 3000/3000。

## 9.7 Elastic Workload 与动态回收

Kueue 支持 Job 集成报告 `status.reclaimablePods`，释放不再需要的配额。更进一步的 elastic workload 能力可以让作业在运行时调整副本数，并同步配额账本。具体 API 和 feature gate 以版本为准。

两条不同的路径：

- **Partial Admission**：`PodSet.minCount` + `kueue.x-k8s.io/job-min-parallelism`，启动时用较少副本先跑；由 `PartialAdmission` gate 控制。v0.20 起 `minCount` 可以为 0（先准入、零副本等待）。配额按**实际准入的副本数**计费，后续扩容需要重新走准入/补账，不是"先按 count 占住"。
- **Elastic Workload Slices**：`kueue.x-k8s.io/elastic-job: "true"`，运行时扩容创建新 slice、缩容原地更新；由 `ElasticJobsViaWorkloadSlices` 控制。部分副本扩容是 `ElasticJobsViaWorkloadSlicesWithPartialReplicaScaleUp`（alpha）；与 ProvisioningRequest 联动是 `ElasticJobsViaWorkloadSlicesForProvisioningRequests`。

两者互斥，不能同时启用。详细的状态机、注解和集成边界见[第 36 章](36-inference-elastic-workloads.md)。

设计要点：

- 配额账本必须和实际 Pod 数一致；Partial Admission 的"已准入副本数"是账本口径，不是 `spec.podSets[].count`；
- 释放容易，重新要回难；扩容时可能因为 Cohort 已被别人借走而卡住；
- 需要 Job 控制器和 Kueue 的紧密配合；
- 不能让用户随意改已准入 Workload 的 requests；
- 扩容/缩容期间新旧副本可能同时占配额，容量模型要覆盖峰值。

## 9.8 Admission Fair Sharing

Admission Fair Sharing 关注 LocalQueue 的历史用量，让长期占用少的租户在排队时获得优先。它和基于当前 DRS 的 Fair Sharing 互补。

适用场景：

- 多租户共享集群；
- 某些租户偶尔爆发，但长期占用低；
- 希望惩罚长期占用的"大户"。

## 9.9 Visibility API 与 KueueViz

### 9.9.1 Visibility API

Kueue 提供 Visibility API，让用户查询：

- 某个 ClusterQueue 的 pending Workload；
- 某个 LocalQueue 的 pending Workload；
- 队列状态和资源使用。

它适合做租户门户、排障工具和自动化。对大规模查询要配置 API Priority and Fairness（APF），避免打爆 apiserver。Kueue 提供 `enableVisibilityAPF` 之类的开关来保护这条查询路径，具体字段以版本为准。

一个典型查询：

```bash
kubectl get --raw \
  "/apis/visibility.kueue.x-k8s.io/v1beta1/clusterqueues/team-a-cq/pendingworkloads" \
  | jq .
```

### 9.9.2 KueueViz

KueueViz 是浏览器仪表盘，展示队列、Workload、资源使用和进度。安装方式：

```bash
helm upgrade kueue oci://registry.k8s.io/kueue/charts/kueue \
  --namespace kueue-system \
  --set enableKueueViz=true
```

开发环境可以 port-forward：

```bash
kubectl -n kueue-system port-forward svc/kueue-kueueviz-frontend 8080:8080
kubectl -n kueue-system port-forward svc/kueue-kueueviz-backend 8081:8081
```

生产建议走 Ingress，并配置 CORS/认证。KueueViz 是**展示层**，不是配额决策层；它挂了不影响 Kueue 准入。

### 9.9.3 自建查询的注意点

- 不要每次查询都 list 全量 Workload；用 label/field selector 或 Visibility API；
- 查询结果可能很大，分页/限流；
- 指标里的租户标签会放大基数，和自定义 dashboard 一起设计；
- 只读账号也要 RBAC；不要把 cluster-admin 的 kubeconfig 给门户。

## 9.10 Workload PriorityClass

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata:
  name: critical
value: 1000
description: "critical batch jobs"
```

与 Pod PriorityClass 的区别：

- Pod PriorityClass 影响 `kube-scheduler`；
- WorkloadPriorityClass 只影响 Kueue 排队/抢占。

适合"队列里优先，但不应该在节点上抢占系统 Pod"的场景。

### 9.10.1 端到端用法

Job 通过标签选择：

```yaml
metadata:
  labels:
    kueue.x-k8s.io/queue-name: queue
    kueue.x-k8s.io/priority-class: critical
```

Kueue 创建 Workload 时把它转换成 v1beta2 的引用：

```yaml
spec:
  priorityClassRef:
    group: kueue.x-k8s.io
    kind: WorkloadPriorityClass
    name: critical
```

几个边界：

- **缺失的类不会回退**。引用的 WorkloadPriorityClass 不存在时，Workload 创建失败并产生 `WorkloadPriorityClassNotFound` 事件；不要指望它退回 Pod PriorityClass。
- **默认化**（v0.20 beta，`WorkloadPriorityClassDefaulting`）：集群里存在名为 `default` 的 WorkloadPriorityClass 时，未打标签的 Job 会自动补上 `priority-class: default`。这是集中治理默认优先级的手段，但也意味着"没打标签"不再等于"优先级 0"。
- **Pod priority 独立**。WorkloadPriorityClass 不写 Pod 的 `priorityClassName`；需要节点级抢占时，仍要单独设置 Pod PriorityClass。
- **标签位置随对象类型不同**。Deployment 的 `queue-name` 要打在 Pod 模板上；JobSet/RayJob 通常打在外层 `metadata.labels`。以第 8.5 节的表和目标版本的集成为准。

## 9.11 能力成熟度与版本矩阵

以下是 2026 年主线版本的典型状态，具体以目标 release 为准：

| 能力 | 状态 | 默认 | 主要 gate |
|---|---|---|---|
| TAS | beta | 开 | `TopologyAwareScheduling` + `TAS*` |
| DRA | beta | 开 | `KueueDRAIntegration` |
| DRA 扩展资源 | beta | 开（v0.19+） | `KueueDRAIntegrationExtendedResource` |
| DRA device feasibility | alpha | 关 | `KueueDRADeviceFeasibility` |
| DRA device taints | alpha | 关 | `KueueDRAIntegrationDeviceTaints` |
| DRA prioritized list | alpha | 关 | `KueueDRAIntegrationPrioritizedList` |
| DRA partitionable devices | alpha | 关 | `KueueDRAIntegrationPartitionableDevices` + K8s `DRAPartitionableDevices` |
| DRA consumable capacity | 0.21-devel 计划 beta | 按版本核对 | `KueueDRAIntegrationConsumableCapacity` + K8s `DRAConsumableCapacity` |
| ProvisioningRequest | 稳定 | 开 | 无 |
| MultiKueue | beta | 开 | `MultiKueue` |
| MultiKueue 编排式抢占 | beta | 开（v0.20） | `MultiKueueOrchestratedPreemption` |
| Concurrent Admission | alpha | 关 | `ConcurrentAdmission` |
| DQO | alpha | 关 | `DynamicQuotaOrchestration` |
| Admission Fair Sharing | beta | 开（v0.15+） | `AdmissionFairSharing` |
| AFS 计量锚点 | beta | 开 | `AdmissionFairSharingAnchorAtQuotaReservation` |
| Fair Sharing 抢占 | 稳定 | 由配置开启 | 配置 `fairSharing.preemptionStrategies`（不是 gate） |
| Fair Sharing refill | alpha | 关 | `FairSharingRefill` |
| Configurable Preemption | alpha | 关 | `ConfigurablePreemptions` |
| WaitForPodsReady | 稳定 | 由配置开启 | 无 |
| Partial Admission | beta | 按版本核对 | `PartialAdmission` |
| Elastic Workload Slices | beta | 按版本核对 | `ElasticJobsViaWorkloadSlices` |
| Elastic Slices 部分副本扩容 | alpha | 关 | `ElasticJobsViaWorkloadSlicesWithPartialReplicaScaleUp` |
| TAS partial slices | beta | 开（v0.20） | `TASPartialSlices` |
| TAS 跨 Workload 打散 | alpha | 关 | `TASTopologySpreading` |
| TAS 全层级节点可行性 | beta | 开 | `TASNodeFeasibilityForAllLevels` |
| TAS 拓扑树缓存 | beta | 开 | `TASCacheTopologyTree` |
| TAS grouped PodSet slicing | beta | 按版本核对 | `TASGroupedPodSetSlicing` |
| TAS 多层拓扑 | beta | 开 | `TASMultiLayerTopology` |
| TAS 均衡放置 | alpha | 关 | `TASBalancedPlacement` |
| TAS 残留 Pod 记账 | alpha | 关 | `TASResidualPodAccounting` |
| Hierarchical Cohort / Cohort 对象 | 稳定 | 开 | 无 |
| Cohort 内优先级排序 | beta | 按版本核对 | `PrioritySortingWithinCohort` |
| 默认 LocalQueue | 稳定（v0.17+） | 开 | `LocalQueueDefaulting` |
| 默认 WorkloadPriorityClass | beta | 开（v0.20） | `WorkloadPriorityClassDefaulting` |
| namespace 纳管 selector 总是生效 | GA 锁定（v0.19+） | 开 | `ManagedJobsNamespaceSelectorAlwaysRespected` |
| 单次 reconcile 高并发更新 | beta | 按版本核对 | `HighMaxParallelismWithinReconcile` |
| Flavor 扫描进度保留 | beta | 开（v0.20） | `FlavorFungibilityPreserveScanProgress` |

一个实用原则：**默认开的能力仍要读 release notes**；"beta 默认开"不等于"语义不变"。

MiniKueue 对这些能力的覆盖是：

| 能力 | MiniKueue | 说明 |
|---|---|---|
| TAS | lite | Required/Preferred/Unconstrained/Group + slice + spreading；无节点/非 TAS Pod 推导 |
| DRA | lite | DeviceClass → 逻辑资源计费；无真实设备分配 |
| ProvisioningRequest | lite | `ProvisioningConfig`：预留期间置备、失败重试、延迟后 Ready；无云扩容 |
| MultiKueue | lite | 多 Engine、AllAtOnce/Incremental、选中与清理 |
| Concurrent Admission | lite | 单 Workload 的 Flavor 迁移；无 Parent/Variant |
| DQO | lite | CapacityProvider → effective nominal |
| Admission Fair Sharing | lite | 历史用量、衰减、Entry Penalty、LocalQueue 权重 |
| Fair Sharing 抢占 | lite | 两种 strategy + 同 Cohort 候选；无 AlmostLCA/目标 CQ 排序 |
| Configurable Preemption | lite | trigger/scope/priority/numericLabels/selectors；无 preemptor selector |
| Resource Transformations | lite | Replace/Retain + multiplyBy + outputs；见第 5.10 节 |
| Visibility | 简化 | `QueueStatus` + CLI `-query`/`-get`/`-describe`，无 HTTP API/APF |

## 9.12 本章小结

- TAS 把物理拓扑纳入准入，解决高带宽通信和碎片问题；它有 required/preferred/unconstrained/group/slice/spreading 等多种模式。MiniKueue 已实现这些模式的 lite 版本。
- TAS 的容量来自节点、已准入 TAS Workload、非 TAS Pod 和残留 Pod 的推导，不是手写数字；隐式 TAS 和混合 Flavor 有严格语义。
- DRA 让 Kueue 能对新型设备做配额管理，但配额与设备分配之间仍有 gap；两条路径不要重复配置。MiniKueue 实现 DeviceClass 计费映射。
- ProvisioningRequest 把逻辑配额和云扩容连起来，retryStrategy 控制退避；MiniKueue 用 `ProvisioningConfig` 实现了"预留期间置备 + 失败重试 + 延迟后 Ready"的 lite 版本。
- MultiKueue 把队列扩展到多集群，dispatcher 顺序和 manager 配额规划是关键；MiniKueue 有多 Engine lite 版。
- Concurrent Admission 允许 Workload 在多个 Flavor 上并行追求准入并迁移，代价是 Variant 数量成倍增加；MiniKueue 用单 Workload 迁移模拟主干。
- DQO 让配额随实际容量动态变化，alpha 下要特别注意容量下降不驱逐和 effectiveQuotas 残留。
- Visibility API 与 KueueViz 是展示/查询层，不参与准入决策。
- 这些能力大多仍是 alpha/beta，生产使用要核对版本、gate 依赖和限制。
- TAS 的 domain 容量推导、assignment 到 Pod 约束的转换、DRA 设备分配和 ProvisioningRequest 扩容，属于"物理落地"层；第 28 章把这些链路拆成可诊断的分层。

---

[← 上一章：作业集成](08-workload-integrations.md) | [返回目录](../README.md) | [下一章：运维与可观测性 →](10-operations.md)
