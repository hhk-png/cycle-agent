# 第 13 章 · MiniKueue 设计

## 13.1 目标与非目标

### 目标

`mini-kueue` 是一个**可运行的 Kueue 控制面模拟器**，用来：

- 把 Kueue 的核心语义变成可执行代码；
- 演示队列、配额、Cohort 借用、公平排序、抢占、回收、AdmissionCheck；
- 提供可重复的实验和测试；
- 展示工程化的分层、接口、错误处理和验证方法；
- 作为把算法移植到真实 controller-runtime 项目的参考。

### 非目标

- 不依赖 Kubernetes API；
- 不创建真实 Pod/Job；
- 不替代 kube-scheduler；
- 不实现全部 Kueue 特性；
- 不追求生产性能；
- 不实现 CRD/webhook/informer/leader election。

这个边界让代码可以在没有集群的环境中运行和测试，同时保留最值得学习的控制面逻辑。

## 13.2 需求

### 功能需求

1. 定义 ClusterQueue、Cohort、LocalQueue、Workload、ResourceFlavor、AdmissionCheck、WorkloadPriorityClass。
2. 支持多资源组、多 Flavor、每 Flavor 的 nominal/borrowing/lending。
3. 支持扁平/层级 Cohort 的配额聚合。
4. 支持 BestEffortFIFO/StrictFIFO。
5. 支持优先级。
6. 支持同 CQ 抢占、Cohort 回收、借用时抢占。
7. 支持 AdmissionCheck 两阶段准入。
8. 支持 StopPolicy、deactivate、最大执行时间。
9. 支持 TAS-lite：Topology/TopologyDomain、Required/Preferred/Unconstrained/Group。
10. 支持 WaitForPodsReady 与动态回收（reclaimable pods）。
11. 支持 DQO-lite：CapacityProvider → effective nominal。
12. 支持基础指标。
13. 提供 CLI 场景。
14. 支持 DRA-lite：DeviceClass 映射到逻辑资源，重复计费保护。
15. 支持 Admission Fair Sharing-lite：历史用量、半衰期、Entry Penalty、LocalQueue 权重。
16. 支持 Concurrent Admission-lite：Flavor 迁移。
17. 支持 MultiKueue-lite：manager/worker、AllAtOnce/Incremental、选中与清理。
18. 支持 admission gate（配额预留前）。
19. 支持 Cohort 级 borrowing/lending limit 与隐式 Cohort。
20. 支持 TAS slice 与跨 Workload 打散。
21. 支持资源变换：Replace/Retain、multiplyBy、outputs、与 DRA 去重。
22. 支持 ProvisioningRequest-lite：预留期间置备、失败 Retry、延迟 Ready。
23. 支持 Configurable Preemption-lite：trigger/scope/priority/numericLabels/selectors。
24. 支持 Fair Sharing 抢占-lite：两种 strategy 与 Cohort 权重。
25. 支持指数退避与 `kueuectl` 风格的 CLI 查询/操作。

### 非功能需求

1. **确定性**：相同输入/顺序产生相同输出，便于测试。
2. **可测试**：调度算法与控制器解耦，纯函数优先。
3. **可观测**：事件、状态、指标。
4. **可回滚**：抢占决策先模拟，再提交；失败不留下半成品。
5. **可扩展**：Topology/DRA/MultiKueue 可以通过接口扩展。

## 13.3 分层架构

```text
┌──────────────────────────────────────────────┐
│ cmd/minikueue                                │  CLI
├──────────────────────────────────────────────┤
│ internal/scenario                            │  可复现场景
├──────────────────────────────────────────────┤
│ internal/multikueue                          │  Manager/Worker 分发
├──────────────────────────────────────────────┤
│ internal/controller                          │  协调循环、生命周期
│   Tick / Submit / Finish / Deactivate        │
├───────────────┬───────────────┬──────────────┤
│ internal/queue│ internal/     │ internal/    │
│ 排序/过滤      │ scheduler     │ metrics      │
│               │ Flavor/配额/  │              │
│               │ TAS/抢占      │              │
├───────────────┴───────────────┴──────────────┤
│ internal/cache                               │  配额/拓扑账本
├──────────────────────────────────────────────┤
│ internal/store                               │  内存 API server
├──────────────────────────────────────────────┤
│ internal/api                                 │  类型/校验/工具
└──────────────────────────────────────────────┘
```

Store 里除了队列对象，还保存 `ResourceFlavor`、`Topology`/`TopologyDomain`、`CapacityProvider` 和 `WorkloadPriorityClass`。Scheduler 的 `View` 是这些配置的不可变快照。

### 为什么这样分层

- **api** 只依赖标准库，定义对象和不变式。
- **store** 是唯一的对象存储；控制器不直接持有全局 map。
- **cache** 是唯一的配额账本；调度器只读，控制器提交。
- **scheduler** 是纯决策层：给定 Workload + CQ + Cache，返回 Decision，不修改 Cache。
- **controller** 负责编排：排序、调用调度器、提交驱逐/预留、更新状态。
- **scenario/CLI** 不碰内部细节，只通过公开接口构造和观察。

这种分层让：

- 调度算法可以单独单元测试；
- 控制器可以集成测试；
- 场景可以端到端测试；
- 未来替换 store 为 Kubernetes client 时，scheduler 不用大改。

## 13.4 数据模型

### 13.4.1 资源量

```go
type ResourceList map[ResourceName]int64
```

单位：

- CPU：millicore；
- Memory：MiB；
- 其他：整数。

所有计算用整数，避免浮点误差。展示时再格式化（`1.5` CPU、`2Gi`）。

### 13.4.2 Workload

```go
type Workload struct {
    Namespace string
    Name      string
    UID       string
    Labels    map[string]string // Configurable Preemption 的 label/numeric selector
    Spec      WorkloadSpec
    Status    WorkloadStatus
}
```

`Spec.PodSets` 是资源计算的基础；`Status.Admission` 记录 Flavor 分配和总用量；`Status.Conditions` 表达生命周期。`Status.AdmittedAt` 只表示当前准入周期的起点，`Status.ExecutionTicks` 累计此前所有准入周期的 tick 数，因此 `maximumExecutionTimeSeconds` 是跨抢占/重排队累计的，而不是每次重新准入就重置。

### 13.4.3 ClusterQueue

```go
type ClusterQueueSpec struct {
    NamespaceSelector   *LabelSelector   // nil = 不匹配；空 = 匹配所有
    ResourceGroups      []ResourceGroup
    CohortName          string
    QueueingStrategy    QueueingStrategy
    Preemption          PreemptionPolicy
    AdmissionChecks     []string
    StopPolicy          StopPolicy
    FairSharing         *FairSharing
    AdmissionScope      AdmissionScope      // AFS 开关
    ConcurrentAdmission *ConcurrentAdmissionPolicy
    DeviceClassMappings map[string]ResourceName // DRA-lite
    CapacityProvider    string
    PreemptionConfigName string                // Configurable Preemption
}

type LabelSelector struct {
    MatchLabels      map[string]string
    MatchExpressions []LabelSelectorRequirement // In/NotIn/Exists/DoesNotExist
}
```

`LabelSelector` 对齐 Kubernetes 的语义：nil selector 不匹配任何 namespace，空 selector 匹配所有 namespace。`LocalQueue` 增加 `StopPolicy`，与 ClusterQueue 的 stopPolicy 叠加生效。

`ResourceGroup.Flavors` 里每个 Flavor 有：

```go
type FlavorQuota struct {
    Name           string
    Nominal        ResourceList
    BorrowingLimit ResourceList
    LendingLimit   ResourceList
}
```

### 13.4.4 Cohort

```go
type Cohort struct {
    Name           string
    ParentName     string
    ResourceGroups []ResourceGroup
    FairSharing    *FairSharing
}
```

`ParentName` 支持树；`ResourceGroups` 是 Cohort 级共享池；`FlavorQuota.BorrowingLimit/LendingLimit` 在 Cohort 上表示子树级上限。

新增的对象字段：

```go
type PodSet struct {
    Name     string
    Count    int32
    Requests ResourceList
    Topology *PodSetTopology
    Devices  []DeviceRequest // DRA-lite
}

type PodSetTopology struct {
    Mode      TopologyMode
    Level     string
    Group     string
    SliceSize int32  // TAS slice
    Spreading bool   // 跨 Workload 打散
}

type WorkloadSpec struct {
    // ...
    PriorityClassName       string
    MaximumExecutionSeconds int64
    DeviceClassMappings     map[string]ResourceName
    AdmissionGates          []string
    ResourceTransformations []ResourceTransformation // 提交时冻结
}

type LocalQueue struct {
    // ...
    FairSharing *FairSharing // AFS 权重
}
```

### 13.4.5 Cache

```go
type Cache struct {
    usage     map[string]map[string]api.ResourceList // cq -> flavor -> resources
    workloads map[string]*api.Workload                // key -> admitted/reserved
    sequence  int64
}
```

Cache 是"谁现在占了多少"的唯一真相。调度器的所有 fit 检查都基于它。

### 13.4.6 Topology 与 TopologyDomain

```go
type Topology struct {
    Name   string
    Levels []string   // 从粗到细，例如 rack, hostname
}

type TopologyDomain struct {
    Name     string
    Topology string
    Values   []string       // 每个 level 一个值
    Capacity ResourceList   // 这个叶子域的可调度容量
}
```

真实 Kueue 从 Node 和已运行 Pod 推导 domain 容量；MiniKueue 把容量显式写在叶子域上，换取确定性和可测试性。`Values` 的前缀决定上层 domain：`["rack-a","h1"]` 在 rack 层的 domain key 是 `rack-a`，在 hostname 层是 `rack-a/h1`。

`Admission.TopologyAssignments` 记录每个 PodSet 落在哪个 topology/level/domain、多少个 Pod、占多少资源。`Scheduler.TopologyUsage` 扫描这些 assignment，并在不同 level 之间做换算后计算物理用量。

### 13.4.7 CapacityProvider 与有效配额

```go
type CapacityProvider struct {
    Name     string
    Capacity map[string]ResourceList // flavor -> resources
}
```

`ClusterQueueSpec.CapacityProvider` 引用一个 provider。Engine 每个 tick 调 `recomputeEffectiveQuotas`，按静态 nominal 比例把 provider 容量分给 CQ，写入 `effective` 并传给 Scheduler View。Scheduler 的 `nominal()` 优先返回有效配额，因此配额检查、Cohort 聚合、lendable 和公平份额都自动使用它。

## 13.5 生命周期状态机

MiniKueue 用 `WorkloadState`：

```text
Pending
  │ reserve
  ▼
QuotaReserved
  │ checks ready
  ▼
Admitted
  │ Finish
  ▼
Finished
```

驱逐/停用会回到 `Pending` 或 `Active=false`。

与真实 Kueue 的差异：

- 真实 Kueue 没有单一的 `state` 字段，而是 Conditions；
- MiniKueue 用 `State` 简化 CLI/测试；
- 同时保留了 Conditions，便于对照。

## 13.6 配额算法

### 13.6.1 直接准入

```text
TryAdmit(w, cq):
  usage = w.TotalUsage()
  if findAssignment(w, cq, nil) succeeds:
      return Admission
  else if preemption enabled:
      return findPreemption(...)
  else:
      return insufficient quota
```

`findAssignment` 对每个资源组递归选择 Flavor：

```text
walk(groupIndex):
  if all groups assigned: success
  requested = usage for group.CoveredResources
  if requested is zero: skip group
  for each flavor in group.Flavors:
      if canFitFlavor(cq, flavor, requested, removed):
          assign
          if walk(groupIndex+1): success
          unassign
  fail
```

### 13.6.2 canFitFlavor

对每个请求资源：

```text
cur = CQ 在该 Flavor 上的 usage（可减去被移除的 victim）
new = cur + req
nominal = flavor.Nominal[r]

if no cohort:
    require new <= nominal

if cohort:
    require cohortTotal + req <= cohortNominal
    if new <= nominal: ok
    borrow = new - nominal
    if borrowingLimit exists and borrow > limit: fail
    lendable = sum(other CQ unused nominal, capped by lendingLimit)
             + free cohort shared quota
    if borrow > lendable: fail
```

这个算法是 MiniKueue 的核心。它比真实 Kueue 简化，但保留了：

- nominal 保底；
- Cohort 总量边界；
- borrowingLimit；
- lendingLimit；
- 共享池；
- 层级 Cohort 聚合。

### 13.6.3 层级 Cohort

`rootCohort` 沿 `ParentName` 向上；`cohortMembers` 收集同一根下的所有 CQ；`cohortNominal` 汇总 CQ nominal + 所有 Cohort 的共享 nominal。

这允许跨分支借用：

```text
root
├── org-a (CQ A)
└── org-b (CQ B)
```

A 可以借 B 的空闲 nominal，只要 root 总量允许。

MiniKueue 实现了简化版的 Cohort 级 borrowing/lending limit：

```text
for each cohort on path(cq):
    if cohort.borrowingLimit exists:
        used = subtree usage - subtree CQ nominal
        incremental = totalBorrowAfter - currentBorrow
        require used + incremental <= limit

lending capacity of peer = min(CQ lendingLimit, all ancestor cohort lendingLimits)
```

borrowingLimit 是"子树内每个成员的正借用之和"，不会用空闲成员的未用量去抵消另一个成员的借用；lendingLimit 则只是逐成员收紧，不是子树总量上限：子树有 N 个成员时，实际可出借量仍可能接近 N × limit。共享池也按根树扁平聚合，兄弟子树的共享池对目标 CQ 可见。这些都不是真实 Kueue 的逐层借出账本，但能复现"组织级限额"的方向。隐式 Cohort（没有 Cohort 对象）按扁平组处理，仍可借用。

### 13.6.4 DRA-lite

`Workload.TotalUsage` 在汇总普通 requests 后，遍历 `PodSet.Devices`：

```text
deviceUsage[resource] += device.Count × effectivePodCount
total[resource] += max(requestUsage[resource], deviceUsage[resource])
```

取 `max` 而不是相加，避免同一资源既以扩展资源又以设备请求出现时重复计费。`Engine.Submit` 把 CQ 的 `DeviceClassMappings` 复制到 Workload，保证后续调度和回收不依赖 CQ 的后续修改。

### 13.6.5 Admission Fair Sharing-lite

Engine 为每个 LocalQueue 维护一个 `float64` 历史用量：

```text
submit:  history[lq] += entryPenalty
admit:   history[lq] += usage.WeightedSum(resourceWeights)
tick:    history[lq] *= 0.5^(1/halfLifeTicks)
score:   history[lq] / lq.fairSharing.weight
```

当 CQ 的 `admissionScope` 打开时，pending Workload 先按 score 升序，再按优先级/创建时间排序。它不是硬配额，只改变排队顺序。

### 13.6.6 Concurrent Admission-lite

每个 tick 在调度前：

```text
for each admitted/reserved workload in a concurrent CQ:
    current = flavor index of its admission
    if current <= 0: continue
    admission = DirectAdmission(workload, cq, exclude=self)
    next = flavor index of admission
    if next < current and allowed by lastAcceptableFlavorName:
        release old reservation
        reserve on next flavor
```

只允许向更小索引迁移，避免 A↔B 抖动；迁移会结束当前准入周期并开始新周期（`AdmittedAt` 更新），累计执行时间通过 `ExecutionTicks` 保留。

### 13.6.7 TAS slice 与打散

- `SliceSize > 0` 且 Required：把 PodSet 切成 `ceil(count/sliceSize)` 片，每片找能容纳的 domain，记录多条 `TopologyAssignment`；
- `Spreading`：候选 domain 按当前用量升序（用量相同按名字），选择最空的；否则按名字 first-fit；
- slice 与 group/Preferred 互斥，校验会拒绝；
- 抢占时 `removed` 集合会同时从配额和拓扑用量中排除 victim。

### 13.6.8 Resource transformations

`Engine.SetResourceTransformations` 安装集群级变换列表；`Submit` 把列表复制进 `Workload.Spec.ResourceTransformations` 并冻结。`Workload.PodSetPerPodUsage` 的顺序是：

```text
requests
  → 合并 DRA DeviceClass 映射（取 max，避免双计）
  → 应用资源变换（所有规则读原始 requests）
  → 返回 per-pod usage
```

变换类型：

```go
type ResourceTransformation struct {
    Input      ResourceName
    Strategy   TransformStrategy // Retain / Replace
    MultiplyBy string            // 资源名或固定整数
    Outputs    ResourceList      // 每单位 input 的产出
}
```

`Replace` 删除 input，`Retain` 保留；`outputs` 为空 + `Replace` 表示忽略该资源。设计取舍：把配置冻结在 Workload 上，保证已准入 Workload 的账本不会因后续改配置而变化；这与真实 Kueue 在 Pod 创建时换算、后续以 Workload 请求为准的方向一致。

## 13.7 抢占算法

### 13.7.1 候选人

- 同 CQ：满足 `withinClusterQueue` 的优先级条件；
- Cohort：满足：
  - 同一根 Cohort；
  - victim CQ 正在借用；
  - `reclaimWithinCohort` 或 `borrowWithinCohort` 允许；
  - 优先级条件。

### 13.7.2 排序

1. 借用者优先；
2. 优先级低者优先；
3. 最近准入者优先；
4. 稳定的 key tie-break。

### 13.7.3 选择与最小化

```text
for victim in candidates:
    removed.add(victim)
    if findAssignment(w, cq, removed) succeeds:
        break
if no assignment: fail

for victim in reverse(selected):
    removed.remove(victim)
    if findAssignment(w, cq, removed) succeeds:
        selected.remove(victim)
    else:
        removed.add(victim)
```

### 13.7.4 防循环

同优先级时用"更老的赢"：

```go
func olderWins(victim, incoming *Workload) bool {
    if victim.CreationTimestamp != incoming.CreationTimestamp {
        return victim.CreationTimestamp > incoming.CreationTimestamp
    }
    return victim.Key() > incoming.Key()
}
```

这给同优先级一个全序，避免 A 抢 B、B 又抢 A。

### 13.7.5 TAS 放置

TAS 不是独立的调度器，而是配额分配后的"第二层可行性检查"：

```text
findAssignment:
  walk resource groups:
    choose flavor
    when all groups assigned:
      planTopology(w, cq, assignments, removed)
      if topology fits: success
      else: backtrack to next flavor
```

`planTopology` 的算法：

1. 把带 `PodSetTopology` 的 PodSet 按 `Group` 分组；
2. 为每个 PodSet 找到覆盖其资源的 Flavor，再通过 `ResourceFlavor.topologyName` 找到 Topology；
3. 解析 level（空则用最细层）；
4. 对 Required/Preferred：在目标 level 的 domain 中找一个能容纳整个 PodSet（或 group）的 domain；
5. 对 Unconstrained：按排序后的 domain 逐个铺开 Pod；
6. 更新工作副本中的 domain 用量，供后续 PodSet 使用；
7. Required 失败则整体失败；Preferred 失败则退化为无拓扑约束。

domain 用量来自 Cache 中已准入 Workload 的 `TopologyAssignments`，因此释放/抢占会自动反映。真实 TAS 还要考虑节点 allocatable、非 TAS Pod、设备可行性和碎片整理，MiniKueue 只保留"容量 + 约束"的骨架。

### 13.7.6 Configurable Preemption-lite

`Scheduler.PreemptionConfigs[name]` 保存规则，CQ 通过 `spec.preemptionConfigName` 引用。候选收集分两路：

```text
classicCandidates = 按 withinClusterQueue / reclaimWithinCohort / borrowWithinCohort 过滤
configCandidates  = 对每条 rule：
                      if rule.trigger matches（Always 或本次失败类型）:
                        for selector in rule.candidateSelectors:
                          for admitted workload:
                            if scopeMatch && priorityMatch && numericLabelsMatch &&
                               labelSelectorMatch && clusterQueueSelectorMatch:
                              append
candidates = dedupe(classicCandidates + configCandidates)
sort(candidates)
```

失败类型由 `TryAdmit` 判定：先跑一个跳过拓扑的 quota 分配，若它成功而完整分配失败，trigger 为 `QuotaFeasibleAndInsufficientTopology`，否则为 `InsufficientQuota`。这个判定把"配额够但拓扑放不下"和"配额本身不够"分开，是 Configurable Preemption 里少数必须显式建模的语义。

排序后仍然走 13.7.3 的"逐个移除 + 反向最小化"，所以配置规则只改变**谁可以当 victim**，不改变提交决策的结构。这个分层让新增规则不会破坏配额/拓扑账本。

### 13.7.7 Fair Sharing 抢占-lite

在 Classic 和 Configurable 都失败后，若 `FairSharingPreemption` 非空：

1. 收集同一根 Cohort、优先级不高于 preemptor、`FairShare > 0` 的已准入 Workload；
2. 逐个加入 victim 集合并调用 `findAssignment`；
3. 对每个可行集合计算：
   - `preemptorNewShare = fairShareValue(preemptorCQ, admission, removed)`；
   - `targetOldShare = FairShare(victimCQ)`；
   - `targetNewShare = fairShareValue(victimCQ, nil, removed)`；
4. 任一启用策略满足即接受：
   - `LessThanOrEqualToFinalShare`: `preemptorNewShare <= targetNewShare`；
   - `LessThanInitialShare`: `preemptorNewShare < targetOldShare`；
5. 反向最小化 victim 集合。

`fairShareValue` 复用 `FairShare` 的 borrowed/lendable 公式，但支持"假设准入"和"排除集合"，并沿 CQ → 根路径乘上 Cohort 权重。它的语义是"借用式 DRS"，不是真实 Kueue 的完整加权 DRF + AlmostLCA。

## 13.8 AdmissionCheck

MiniKueue 的检查模型：

- CQ 上列出的 `AdmissionChecks` 在配额预留后进入 `Pending`；
- `evaluateChecks` 每个 tick 检查：
  - `Ready`：继续；
  - `Rejected`：驱逐并停用；
  - `Retry`：释放预留，重排队；
  - `Pending`：如果 `AutoApproveChecks` 或控制器名为 `auto`，变 `Ready`；否则等待。
- 全部 Ready 后 `Admitted`。

CLI 的 `admission-check` 场景演示：

1. tick 1：QuotaReserved；
2. 操作员 `ApproveCheck`；
3. tick 2：Admitted。

真实 Kueue 的 ProvisioningRequest/MultiKueue 是外部控制器，MiniKueue 用内存状态模拟。

## 13.9 控制器循环

`Tick()` 的顺序：

```text
1. clock++
2. 清空事件
3. decayAFS（历史用量半衰期）
4. recomputeEffectiveQuotas（CapacityProvider → effective nominal）
5. 重建 scheduler view（读取最新 CQ/Cohort/Flavor/Topology/PriorityClass）
6. ensureCache（把 store 中已预留/已准入的对象补进 Cache）
7. applyStopPolicies（Hold/HoldAndDrain）
8. reapDeactivated（active=false）
9. expireExecution（最大执行时间）
10. checkPodsReady（WaitForPodsReady 超时）
11. migrateConcurrentAdmissions（更优 Flavor 可行时迁移）
12. admitReadyReservations（已有的 QuotaReserved）
13. schedule（按公平份额顺序处理 CQ；AFS 打开时 LQ 内按历史用量）
14. admitReadyReservations（新预留的，自动检查可能已 Ready）
15. updateGauges
```

为什么 `admitReadyReservations` 出现两次：

- 第一次处理上一 tick 的预留；
- 第二次处理本 tick 新预留且自动检查通过的，避免多等一 tick。

### 13.9.1 WaitForPodsReady

Engine 的可选配置：

```go
type WaitForPodsReadyConfig struct {
    Timeout    int64
    MaxRetries int32
}
```

启用后：

- `reserve` 把 `PodsReady` 置为 false，并写 `PodsReady=False/WaitForStart`；
- `MarkPodsReady` 由操作员/集成层调用，写 true；
- `checkPodsReady` 在每 tick 检查 `clock - AdmittedAt >= Timeout`，超时则驱逐并重排队；
- `MaxRetries > 0` 时，按 `schedulingStats` 中 `PodsReadyTimeout` 的次数判断，连续超时达到上限后停用 Workload。这里刻意不复用 `Requeues`：抢占也会增加 `Requeues`，混用会让"被抢占过一次 + 一次就绪超时"的作业被误停用。

这模拟了 Kueue 的 all-or-nothing 安全网：逻辑配额足够不等于 Pod 一定能全部启动。

### 13.9.2 动态回收

`ReportReclaimable(ns, name, podSet, count)`：

1. 校验 `count <= Count - 已回收`；
2. 更新 `Status.ReclaimablePods`；
3. 如果 Workload 正在占用 Cache，先从 Cache 移除；
4. `applyReclaim` 从对应的 FlavorAssignment 中减去 `per-pod request × count`；
5. 同步收缩 `TopologyAssignments`：从尾部 assignment 开始减少 `PodCount` 和资源，PodCount 归零的 assignment 直接删除；
6. 重新计算 `Admission.Usage`，再把 Workload 加回 Cache。

第 5 步很关键：配额账本和拓扑账本是两套独立的账本。只减 FlavorAssignment 而不减 TopologyAssignment，会让 domain 一直被旧 Pod 占用，后续 TAS 作业看到"配额够、拓扑没空间"的假象。`TotalUsage` 使用 `Count - ReclaimablePods` 作为有效 Pod 数。回收是单向的：Kueue 不会把已经释放的配额"要回来"。

### 13.9.3 DQO-lite

`recomputeEffectiveQuotas` 在每次 Tick 开始时：

1. 遍历所有 CapacityProvider；
2. 对每个 flavor/resource，收集引用该 provider 的 CQ；
3. 按静态 nominal 比例分配 provider 报告的容量；provider 已报告某个 Flavor 但漏报的资源按 0 处理，只有完全没被 provider 报告的 Flavor 才回退静态 nominal；
4. 把结果写入 `Engine.effective`，再通过 Scheduler View 生效。

它不修改 CQ spec，也不持久化有效配额；容量变化只影响后续调度，不会驱逐已准入 Workload。这是对 DQO alpha 的有意简化。

### 13.9.4 AdmissionCheck Retry

`AdmissionCheckState` 增加两个字段：

```go
type AdmissionCheckState struct {
    Name                string
    State               string // Pending/Ready/Retry/Rejected
    Message             string
    RequeueAfterSeconds int32
    RetryCount          int32
    LastTransitionTick  int64
}
```

`ApproveCheckRetry` 进入 Retry 时递增 `RetryCount`；`evaluateChecks` 释放预留并设置 `RequeueAt = LastTransitionTick + RequeueAfterSeconds`。没有设置延迟时立即重试，除非配置了 `BackoffConfig`：

```go
type BackoffConfig struct {
    BaseTicks  int64
    MaxTicks   int64
    Multiplier float64
}
```

`requeueDelay(attempt)` 返回 `min(MaxTicks, BaseTicks × Multiplier^attempt)`；它同时用于抢占/驱逐和未显式指定延迟的 Retry。显式的 `requeueAfterSeconds` 仍然优先。

`ProvisioningConfig` 在 `evaluateChecks` 的 Pending 分支里模拟 ProvisioningRequest：

```text
if retryCount < Failures:
    SetAdmissionCheckRetry(delay)
    releaseReservation(requeueAt = clock + delay)
else if clock - reservationTick < delay:
    keep Pending
else:
    Ready
```

重试计数跨预留保留，Retry 路径不会重置它。这个实现对齐了 `requeueAfterSeconds`/`retryCount` 的主要语义，但没有真实 `ProvisioningRequestConfig.retryStrategy` 的 `backoffLimitCount` 和 `podSetUpdates`。

### 13.9.5 SchedulingStats

每次 `evict` 都会按 `(reason, underlyingCause)` 更新 `Status.SchedulingStats`。例如：

```text
Preempted/""                          count=1
PodsReadyTimeout/WaitForStart         count=2
Deactivated/RequeuingLimitExceeded    count=1
```

这让测试可以断言"为什么被驱逐"，而不是只看 `Evicted` 条件。

### 13.9.6 Visibility

`Engine.QueueStatus(cq)` 返回 pending/reserving/admitted/finished 分类、usage/nominal 和 pending 原因分布。它不修改状态，用于 CLI `-query` 和测试；对应真实 Kueue 的 Visibility API。

## 13.10 并发模型

- `Engine` 用一把 mutex 串行化 Tick/Submit/Finish/Deactivate/ApproveCheck。
- `Store` 和 `Cache` 各自有锁，支持并发读。
- Scheduler 本身无锁，依赖传入的 View/Cache。
- 测试可以并发调用只读方法；写操作由 Engine 串行化。

这不是高性能设计，但：

- 避免了复杂的锁顺序；
- 保证 Tick 的原子性；
- 让测试可重复。

真实控制器会用 workqueue + 单 key 串行 reconcile + informer cache，而不是一把大锁。

## 13.11 错误处理与回滚

- `TryAdmit` 不修改 Cache；失败不会留下半成品。
- 控制器先驱逐 victim，再 `Cache.Add` 新 Workload。
- 如果 `Cache.Add` 失败（重复 key），回滚 Workload 状态并记录事件。
- 动态回收先保存旧 `Admission` 和 `ReclaimablePods`；若 `applyReclaim` 失败，恢复两者再重新加回 Cache，避免"状态说回收了、账本没回收"的漂移。
- 抢占 victim 来自 Cache 快照；`evict` 会按 key 重新读取 Store 的最新对象，避免用准入前的快照覆盖 `AdmittedAt`、`ExecutionTicks` 和条件。
- 多数 `Store.UpdateWorkload` 调用点忽略返回值（`_ =`），因为对象来自 Store 且引用已校验；会改变契约的路径（如 `Activate`）返回错误。内存实现中失败很少发生，但这不等同于"失败会 panic"。
- 场景使用 `Must*` 辅助函数，构造失败直接 panic，避免"静默跑错实验"。

### 13.11.1 跨层拓扑账本

TAS 的 `TopologyAssignment` 记录在注解指定的 level，但物理容量在所有 level 上是同一份。`Scheduler.TopologyUsage` 负责两种转换：

- **粗层分配 → 细层**：把 rack 级预留按容量比例下钻到 hostname 子域；
- **细层分配 → 粗层**：把 hostname 级预留上卷到 rack 父域。

`planTopology` 在同一次决策中通过 `addPlacement` 同步更新所有 level 的 working 账本，因此一个 Workload 内"PodSet A 用 rack、PodSet B 用 hostname"也不会双计。这个修复对应第 27.10.6 节的第一类缺陷。

### 13.11.2 对象隔离与引用校验

- Store 的所有 spec 类型（CQ/LQ/Cohort/Flavor/Topology/Domain/Provider/Check/PriorityClass）读写都做深拷贝；
- `UpdateWorkload` 保留 UID 和 spec，只允许修改 `spec.active` 与 `spec.admissionGates`；新的生命周期走 `UpsertWorkload`；
- `UpsertWorkload`/`UpdateWorkload` 都校验 LQ/CQ/priority class/DRA 映射引用；
- Topology 更新如果让已有叶子域不兼容，直接拒绝并回滚，不静默删域。

这些约束让"Get 出来改一改"不会再意外改到 store，也让测试和真实 API server 的语义更接近。

## 13.12 扩展点

### 13.12.1 Topology（已实现，仍可扩展）

`planTopology` 已实现 Required/Preferred/Unconstrained/Group、slice、spreading 和跨层账本。仍可扩展：

- 从 Node/Device 自动推导 domain 容量（当前显式 `TopologyDomain`）；
- 节点故障热替换；
- 多层 slice 与 DRA 设备可行性；
- 更贴近真实 Kueue 的 packing/defragmentation 启发式。

### 13.12.2 DRA（已实现 lite，仍可扩展）

`DeviceRequest` 已把 DeviceClass 映射成逻辑资源，并与扩展资源取 max 避免双计。仍可扩展：

- 解析 `ResourceClaimTemplate`；
- 真实设备分配、健康/污点、Counter/Capacity；
- `adminAccess` 免计费；
- 与 TAS 的设备约束联动。

### 13.12.3 MultiKueue（已实现 lite，仍可扩展）

`internal/multikueue` 已实现 manager/worker、AllAtOnce/Incremental、提名/选中/清理、`Down` + `WorkerLostTimeout` 失联切换。仍可扩展：

- External dispatcher；
- 跨集群凭据/网络/状态同步；
- 编排式抢占（一次只让一个 worker 尝试）；
- 重复执行与恢复语义。

### 13.12.4 真实控制器

把 `scheduler`/`cache` 包移植到 controller-runtime：

- 用 CRD 替换 `api`；
- 用 informer 替换 `store`；
- 用 workqueue 驱动 `Tick`；
- 用 webhook 做校验；
- 用 status subresource 更新条件；
- 用 client-go 管理 Job/Pod。

## 13.13 设计取舍

| 取舍 | 选择 | 原因 |
|---|---|---|
| 是否依赖 Kubernetes | 不依赖 | 可在无集群环境运行/测试 |
| 时间模型 | 离散 tick | 确定性、易测试 |
| 状态表示 | State + Conditions | 简化 CLI，同时保留可观测性 |
| 抢占提交 | 先决策后提交 | 可回滚、易测试 |
| 公平算法 | 借用量加权 DRS | 对齐真实语义，同时避免 AlmostLCA 的复杂度 |
| TAS | 显式叶子域容量 + slice/spreading | 不依赖 Node/Device 模型，可确定性测试 |
| DRA | DeviceClass → 逻辑资源计费 | 不模拟真实设备分配，只验证配额账本 |
| AFS | 历史用量 + 半衰期 + Entry Penalty | 覆盖排序主干，不做采样窗口/精确锚点 |
| Concurrent | 单 Workload 向更优 Flavor 迁移 | 不创建 Variant，避免对象数量爆炸 |
| MultiKueue | 多 Engine + 单 Workload 分发 + Down/超时切换 | 不模拟跨集群网络/凭据/重复执行，保留提名、选中、失联与清理 |
| Cohort limit | 子树借用/出借的保守近似 | 可演示组织级上限，不做逐层精确账本 |
| DQO | provider → 按 nominal 比例分配 | 覆盖 discovery/distribution 主干，不做容量下降驱逐 |
| 就绪/回收 | 操作员显式调用 | 不模拟真实 kubelet/Pod 状态机 |
| Cohort | 支持树/共享池/隐式 Cohort/Cohort 级 limit | 共享池按根树扁平聚合，兄弟子树的共享池也可见；不做 AlmostLCA/逐层账本 |
| namespaceSelector | 完整 matchLabels/matchExpressions | 对齐 Kubernetes LabelSelector |
| 状态统计 | State + Conditions + SchedulingStats | 兼顾 CLI、测试与真实 API 对照 |
| 重试 | `requeueAfterSeconds` + `BackoffConfig` + `WaitForPodsReady.MaxRetries` | 已实现可配置指数退避；WaitForPodsReady 有最大重试次数，AdmissionCheck/抢占还没有统一的全局重试上限 |
| 持久化 | 无 | 模拟器；真实控制器用 etcd |
| 并发 | 单 Engine 锁 | 简单、可重复 |

## 13.14 本章小结

- MiniKueue 的分层是 api → store → cache → scheduler → controller → scenario → CLI。
- 调度器是纯决策层，Cache 是唯一账本，控制器负责提交/回滚。
- 配额算法覆盖 nominal/borrow/lend/cohort/shared/hierarchy，并支持 DQO-lite 的有效配额。
- 抢占算法覆盖同 CQ、回收、借用时抢占，并做最小化；TAS 用量参与抢占模拟。
- TAS-lite 覆盖 Required/Preferred/Unconstrained/Group、层级 domain 聚合与跨层物理账本（粗层下钻/细层上卷）。
- AdmissionCheck、StopPolicy（CQ + LQ）、deactivate、最大执行时间、WaitForPodsReady、动态回收都有实现。
- namespaceSelector 支持 matchLabels + matchExpressions；SchedulingStats 和 Visibility 让排障更接近真实 Kueue。
- AdmissionCheck Retry 支持 requeueAfterSeconds/retryCount。
- 新增 DRA-lite、AFS-lite、Concurrent Admission-lite、MultiKueue-lite、admission gate、Cohort 级 limit、隐式 Cohort、显式 Cohort 共享池、TAS slice/打散。
- 设计取舍优先保证确定性、可测试性和可理解性。

---

[← 上一章：性能与规模](12-performance-scale.md) | [返回目录](../README.md) | [下一章：MiniKueue 实现与验证 →](14-mini-kueue-walkthrough.md)
