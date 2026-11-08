# 第 14 章 · MiniKueue 实现与验证

## 14.1 快速开始

```bash
cd mini-kueue

# 编译/测试
go test ./...

# 列出场景
go run ./cmd/minikueue -list

# 运行一个场景
go run ./cmd/minikueue -scenario basic -ticks 4
```

项目只使用 Go 标准库，没有外部依赖。`go.mod` 里的 module 名是 `example.com/minikueue`。

## 14.2 代码结构

```text
mini-kueue/
├── cmd/minikueue/main.go
├── internal/
│   ├── api/
│   │   ├── types.go          # 对象、资源量、状态、条件
│   │   └── validate.go       # CQ/Workload/LocalQueue 校验
│   ├── store/store.go        # 内存 API server
│   ├── cache/cache.go        # 配额账本
│   ├── queue/order.go        # 排序/过滤/公平顺序
│   ├── scheduler/scheduler.go# Flavor/配额/抢占
│   ├── scheduler/explain.go  # 诊断：逐资源 FitCheck/拓扑/抢占证据
│   ├── controller/engine.go  # Tick/生命周期
│   ├── controller/explain.go # 控制器层阻塞 + Engine.Explain
│   ├── metrics/metrics.go    # 指标
│   ├── multikueue/           # Manager/Worker 多集群分发
│   └── scenario/scenarios.go # 可复现场景
├── go.mod
├── Makefile
└── README.md
```

## 14.3 从一次 `Tick` 看控制面

`Engine.Tick()` 的顺序：

```text
clock++
events = nil
decayAFS                   # Admission Fair Sharing 历史用量衰减
recomputeEffectiveQuotas   # CapacityProvider → effective nominal
rebuildScheduler
ensureCache
applyStopPolicies
reapDeactivated
expireExecution
checkPodsReady             # WaitForPodsReady
migrateConcurrentAdmissions# 向更优 Flavor 迁移
admitReadyReservations
schedule                   # AFS 打开时 LQ 内按历史用量排序
admitReadyReservations
updateGauges
```

### 14.3.1 rebuildScheduler

每次 tick 从 Store 读取最新的 CQ/Cohort/PriorityClass，以及 Flavor、Topology、TopologyDomain 和有效配额，构造 `scheduler.View`。这样测试里修改 StopPolicy、增加 CQ 或调整拓扑后，不需要手动重建。

### 14.3.2 ensureCache

如果 Store 中有 `QuotaReserved`/`Admitted` 但 Cache 里没有的 Workload（例如重新加载），补进去。这保证 Cache 和 Store 的账本一致。

### 14.3.3 applyStopPolicies

- `Hold`：取消 QuotaReserved 的预留；
- `HoldAndDrain`：驱逐所有已预留/已准入的 Workload。

### 14.3.4 schedule

1. 收集所有 CQ，按 `FairShare` 升序；
2. 对每个 CQ，按 `BestEffortFIFO`/`StrictFIFO` 取 pending；
3. 调用 `Scheduler.TryAdmit`；
4. 如果有 victim，先 `evict`；
5. 提交预留，必要时立即准入。

### 14.3.5 为什么先决策后提交

`TryAdmit` 只计算，不修改 Cache。控制器：

```text
decision := TryAdmit(w, cq)
if decision.Admission == nil: pending
else:
   for victim in decision.Victims: evict(victim)
   reserve(w, decision.Admission)
```

如果 `TryAdmit` 直接改 Cache，失败回滚会很复杂；分开后，决策是可测试的纯逻辑。

## 14.4 关键代码走读

### 14.4.1 资源汇总

`api.Workload.TotalUsage()`：

```go
func (w *Workload) TotalUsage() ResourceList {
    total := ResourceList{}
    for _, ps := range w.Spec.PodSets {
        count := w.EffectiveCount(ps)                 // Count - ReclaimablePods
        for name, value := range w.PodSetPerPodUsage(ps) { // DRA + 资源变换
            total[name] += value * int64(count)
        }
        total[Pods] += int64(count)
    }
    return total
}
```

它自动把 `pods` 计入。如果 CQ 覆盖了 `pods`，就会限制 Pod 数；没覆盖则忽略。

注意这里不是直接读 `ps.Requests`：`PodSetPerPodUsage` 会先把 DRA 设备映射成逻辑资源（与扩展资源取 `max`），再应用提交时冻结的资源变换。回收 Pod 通过 `EffectiveCount` 从计数中扣除。这正是"配额账本与 Pod 原始请求解耦"的关键。

### 14.4.2 Flavor 分配

`findAssignment` 递归遍历资源组：

```go
walk = func(groupIndex int) bool {
    if groupIndex == len(cq.Spec.ResourceGroups) {
        if !requireTopology { topology = nil; return true }
        planned, ok := s.planTopology(w, cq, assignments, removed)
        if !ok { return false } // 回溯到下一个 Flavor
        topology = planned
        return true
    }
    group := cq.Spec.ResourceGroups[groupIndex]
    requested := ...
    if requested.IsZero() { return walk(groupIndex + 1) }
    for _, fq := range group.Flavors {
        if s.canFitFlavor(cq, fq, requested, removed) {
            assignments = append(assignments, ...)
            if walk(groupIndex + 1) { return true }
            assignments = assignments[:len(assignments)-1]
        }
    }
    return false
}
```

这保证：

- 同组资源使用同一个 Flavor；
- 不同组可以不同；
- 按列表顺序优先。

### 14.4.3 TAS 放置

`planTopology` 在 Flavor 分配完成后运行：

```go
planned, ok := s.planTopology(w, cq, assignments, removed)
if !ok { return false } // 回溯到下一个 Flavor
```

它把带拓扑注解的 PodSet 按 group 归并，然后：

- Required/Preferred：在目标 level 的 domain 中找能容纳整个 group 的 domain；
- Unconstrained：按 domain 顺序铺开 Pod；
- 每次放置都更新本地 usage 副本，避免同一 Workload 的多个 PodSet 超用同一 domain。

domain 容量通过 `topologyCapacity` 聚合叶子域得到。例如 rack 层会把该 rack 下所有 host 的 GPU 加起来；这正是一个 4 卡作业能跨两台 2 卡 host 占满一个 rack 的原因。

### 14.4.4 有效配额

`Engine.recomputeEffectiveQuotas` 把 `CapacityProvider.Capacity` 按静态 nominal 比例分给 CQ，写入 `effective[cq][flavor]`。Scheduler 的 `nominal()` 优先读它，因此：

- `canFitFlavor` 的 nominal 检查；
- Cohort 总量；
- lendable；
- FairShare 的 borrowed/lendable

都会自动使用有效配额。这个设计让 DQO-lite 不需要改动调度算法。

### 14.4.5 就绪与回收

`checkPodsReady` 在每 tick 扫描已准入但未就绪的 Workload，超时则驱逐并重排队；重试上限只统计 `PodsReadyTimeout` 这一个原因，不会被抢占计数污染。`ReportReclaimable` 则从 `FlavorAssignment` 中减去回收 Pod 的资源，同时收缩 `TopologyAssignments`（从尾部 assignment 减少 PodCount，归零的删除），并重新写 Cache。

两者都体现了"账本必须和实际状态一致"：前者防止逻辑上已准入、物理上永远起不来的作业占住配额；后者防止作业缩容后仍按旧请求计费，也防止 TAS domain 被已回收的 Pod 永久占用。

### 14.4.5.1 `Cache.Refresh`：准入后刷新状态，不动账本

Workload 先在 `QuotaReserved` 状态被加入 Cache，随后 `Admitted` 才被设置。如果 Cache 一直保存准入前的快照，抢占/诊断会读到 `AdmittedAt=0`、旧条件等过期字段。

`Cache.Refresh` 只替换缓存里的 Workload 快照，**保留 usage 和 sequence**：

```go
updated := w.Clone()
updated.Status.Sequence = existing.Status.Sequence
c.workloads[key] = updated
```

Controller 在 `admitIfReady` 和 Concurrent 迁移后调用它。这个修复对应第 27 章的"Cache 保存准入前快照"缺陷；回归测试是 `TestRefreshUpdatesStatusWithoutChangingUsageOrSequence`。

### 14.4.6 配额检查

`canFitFlavor` 的核心：

```go
cur := Cache.FlavorUsageExcluding([]string{cq.Name}, fq.Name, r, removed)
newUsage := cur + req
nominal := s.nominal(cq.Name, fq.Name, r) // 优先使用 DQO effective quota

if root == "" {
    if newUsage > nominal { return false }
    continue
}

cohortTotal := Cache.FlavorUsageExcluding(members, fq.Name, r, removed)
cohortNominal := s.cohortNominal(root, fq.Name, r)
if cohortTotal+req > cohortNominal { return false }
if newUsage <= nominal { continue }

borrow := newUsage - nominal
if limit, ok := fq.BorrowingLimit[r]; ok && borrow > limit { return false }
lendable := Cache.LendableExcluding(...) + cohortSharedFree(...)
if borrow > lendable { return false }
```

这段代码是第 5 章公式的可执行版本。

### 14.4.7 抢占

`findPreemption`：

1. 收集候选人；
2. 逐个移除，直到 `findAssignment` 成功；
3. 反向最小化；
4. 返回 Admission + Victims。

控制器拿到 Victims 后先驱逐，再提交。

### 14.4.8 AdmissionCheck

`evaluateChecks`：

- `Ready`：继续；
- `Rejected`：驱逐并停用；
- `Retry`：释放预留，重排队；
- `Pending`：自动检查或等待。

### 14.4.9 指标

`Engine.updateGauges` 统计 Pending/Reserved/Admitted/Finished；`Metrics` 记录累计计数。

### 14.4.10 namespaceSelector

`Engine.namespaceMatches` 直接调用 `LabelSelector.Matches`：

```go
return cq.Spec.NamespaceSelector.Matches(e.Store.NamespaceLabels(namespace))
```

支持：

- `MatchLabels` 的键值相等；
- `MatchExpressions` 的 `In`/`NotIn`/`Exists`/`DoesNotExist`；
- nil selector 不匹配任何 namespace，空 selector 匹配所有。

### 14.4.11 LocalQueue StopPolicy

`applyStopPolicies` 不再只遍历 CQ，而是对每个已预留/已准入的 Workload 计算"最严格策略"：

```text
policy = CQ.StopPolicy
if LQ.StopPolicy == HoldAndDrain: policy = HoldAndDrain
if LQ.StopPolicy == Hold and policy == None: policy = Hold
```

`schedule` 会跳过 stopPolicy 非 None 的 LocalQueue 的 pending Workload，但不影响同 CQ 下其他 LQ。

### 14.4.12 SchedulingStats

`evict` 增加 `underlyingCause` 参数，`recordEviction` 按 `(reason, cause)` 累加。`PodsReadyTimeout` 的重试使用 `WaitForStart`，达到重试上限后的自动停用使用 `Deactivated` + `RequeuingLimitExceeded`。

### 14.4.13 Visibility

`QueueStatus` 从 Store 里按"LQ 指向目标 CQ 或 admission 指向目标 CQ"筛选 Workload，再按 State 分类。pending 原因取自 `QuotaReserved` 条件；MiniKueue 额外维护一个 `Inadmissible` 条件方便 CLI/测试，但真实 Kueue 用的是 `QuotaReserved=False` 的 `Reason=Inadmissible`。CLI 的 `-query` 直接打印这个结构。

### 14.4.14 DRA-lite

`Workload.TotalUsage` 遍历 `PodSet.Devices`，通过 `Spec.DeviceClassMappings` 把 DeviceClass 折算成逻辑资源。同一资源同时出现在 `Requests` 和设备请求时取 `max`，不是相加。`Engine.Submit` 在创建时把 CQ 的映射复制到 Workload，避免调度和回收阶段依赖可变的 CQ spec。

### 14.4.15 TAS slice 与打散

`planTopology` 的 group 结构新增 `sliceSize` 和 `spreading`：

- slice：单个 PodSet 按 `SliceSize` 切片，每片独立找 domain，产生多条 `TopologyAssignment`；
- spreading：`orderedDomains` 按 domain 当前用量升序，而非按名字 first-fit；
- 两者都复用 `topologyCapacity` 的层级聚合和 `Scheduler.TopologyUsage` 的跨层已用量。

### 14.4.16 Admission Fair Sharing-lite

`Engine` 维护 `afsUsage[lqKey]`。`Submit` 加 Entry Penalty，`reserve` 加加权用量，`decayAFS` 每 tick 乘 `0.5^(1/halfLife)`。`schedule` 在 CQ 打开 `AdmissionScopeUsageBasedAdmissionSharing` 时调用 `queue.SortByScore`，先按 `history/weight` 升序，再按优先级/创建时间。

### 14.4.17 Concurrent Admission-lite

`migrateConcurrentAdmissions` 在每 tick 调度前运行：用 `DirectAdmission` 排除自身后重新计算 Flavor，如果得到更靠前的 Flavor 就释放旧预留并重新预留。`admissionFlavorIndex` 把 Flavor 名映射到资源组中的顺序，`migrationAllowed` 检查 `LastAcceptableFlavorName`。

### 14.4.18 Admission gate

`schedule` 遇到 `WorkloadSpec.AdmissionGates` 非空时，写 `AdmissionGated=True` 并跳过，不占 Cache。`ClearAdmissionGate` 移除指定 gate，全部清除后恢复 `QuotaReserved=False/Pending`，下一次调度即可参与准入。

### 14.4.19 MultiKueue-lite

`internal/multikueue` 用多个独立 `Engine` 作为 worker。`Manager.Tick` 先检查已选中的 worker 是否失联，再提名健康 worker（AllAtOnce 全部，Incremental 按 `StepSize` 扩张），给每个健康 worker 提交副本、调用 `Tick`，第一个 Admitted 的 worker 成为 `Selected`，其余健康副本用 `Engine.DeleteWorkload` 清理。`Worker.Down` + `Config.WorkerLostTimeout` 模拟失联超时切换；失联 worker 的副本保留到恢复后再清理。`Snapshot` 给 CLI 提供 worker 状态（含 `Down`）。

### 14.4.20 Resource transformations

`Engine.SetResourceTransformations` 校验并保存列表；`Submit` 把列表复制到 `Workload.Spec.ResourceTransformations`；`Workload.PodSetPerPodUsage` 在合并 DRA 设备后调用 `ApplyResourceTransformations`。所有规则读原始 requests，`Replace` 删除 input，`Retain` 保留，`outputs` 空 + `Replace` 表示忽略。

### 14.4.21 Configurable Preemption-lite

`Engine.SetPreemptionConfig` 保存并校验 `api.PreemptionConfig`；`rebuildScheduler` 把它挂到 Scheduler。`preemptionCandidates` 在 Classic 候选之后追加 `configurableCandidates`：按 trigger/scope/priority/numericLabels/selectors 过滤，去重后统一排序。trigger 的判定依赖 `findAssignmentWithoutTopology`：quota 能放下但拓扑放不下时，才是 `QuotaFeasibleAndInsufficientTopology`。

### 14.4.22 Fair Sharing 抢占-lite

`Engine.SetFairSharingPreemption` 开启策略；`findFairSharingPreemption` 收集同 Cohort、低优先级、正 share 的 victim，逐个测试 `findAssignment`，用 `fairShareValue` 计算 preemptor new share 和 target old/new share，再按两种 strategy 判断，最后反向最小化。

### 14.4.23 ProvisioningRequest-lite / Backoff

`Engine.Provisioning` 驱动 `evaluateChecks` 的 Pending 分支：前 `Failures` 次进入 Retry 并释放预留，之后等待 `DelayTicks` 再置 Ready。`Engine.Backoff` 通过 `requeueDelay` 为抢占/驱逐和未显式指定延迟的 Retry 提供指数退避。CLI 的 `-get`/`-describe`/`-stop`/`-resume` 直接复用 Engine 的只读查询和生命周期方法。

## 14.5 场景详解

### 14.5.1 basic

```bash
go run ./cmd/minikueue -scenario basic -ticks 4
```

关键输出：

```text
--- tick 1 ---
  [QuotaReserved] default/job-a: reserved {cpu=1, memory=512Mi, pods=1} on basic
  [Admitted] default/job-a: workload admitted
  [QuotaReserved] default/job-b: reserved {cpu=1, memory=512Mi, pods=1} on basic
  [Admitted] default/job-b: workload admitted
  default  job-c  Pending
  usage: basic={cpu:2,memory:1Gi}
--- tick 2 ---
>> operator: job-a finished
--- tick 3 ---
  [QuotaReserved] default/job-c: ...
  [Admitted] default/job-c: ...
```

发生了什么：

- CQ 2 CPU/2Gi；
- job-a、job-b 各 1 CPU/512Mi → 准入；
- job-c 放不下 → Pending；
- job-a 完成释放 → job-c 准入。

### 14.5.2 cohort-borrow

```text
team-a: nominal 4
team-b: nominal 4
cohort: shared
```

- team-a 提交 6 CPU → 借 2，准入；
- team-b 提交 4 CPU → Cohort 总量 10 > 8，Pending。

这演示了"借用会挤占 Cohort 总量"。

### 14.5.3 preemption

- low（2 CPU）先准入；
- high（1 CPU，高优先级）提交；
- high 触发同 CQ 抢占；
- low 被驱逐，带 `Evicted=Preempted`；
- high 准入；
- low 在 `RequeueAt` 之后可重排队。

### 14.5.4 reclaim

- team-a 2 CPU、team-b 6 CPU（借 2）；
- team-a 再提交 2 CPU；
- Cohort 总量会超；
- team-a 的 `reclaimWithinCohort: LowerPriority`；
- team-b 正在借用且优先级低；
- team-b 被驱逐，team-a 准入。

### 14.5.5 admission-check

- tick 1：QuotaReserved，`manual-check` Pending；
- 操作员 `ApproveCheck`；
- tick 2：Admitted。

这演示了两阶段准入。

### 14.5.6 strict-fifo / best-effort-fifo

- CQ 2 CPU；
- big（3 CPU）在前，small（1 CPU）在后；
- StrictFIFO：big 阻塞，small 也 Pending；
- BestEffortFIFO：跳过 big，small 准入。

### 14.5.7 tas

拓扑：

```text
rack-a: h1(2 GPU), h2(2 GPU)
rack-b: h3(2 GPU), h4(2 GPU)
```

三个 4 卡作业，`Required` 同一个 rack：

- job-a 占满 rack-a（跨 h1/h2）；
- job-b 占满 rack-b；
- job-c Pending——CQ 的 12 卡配额还剩 4 卡，但没有任何 rack 还有 4 卡连续容量。

这演示了"配额够 ≠ 拓扑放得下"。

### 14.5.8 pods-ready

- 两个作业同时准入，`WaitForPodsReady.Timeout=3`；
- 操作员只把 `ready-job` 标记为就绪；
- 第 4 个 tick，`stuck-job` 超时被驱逐并进入重排队；
- 它的配额被释放，但退避期内不会立即重新准入。

这演示了 all-or-nothing 的安全网。

### 14.5.9 dynamic-reclaim

- `elastic-job` 2 个 Pod、共 4 CPU，占满 CQ；
- `waiting-job` 需要 2 CPU，Pending；
- 操作员报告 `elastic-job` 回收 1 个 Pod；
- Cache 立即降到 2 CPU，下一个 tick `waiting-job` 准入。

这演示了动态回收如何提高利用率，以及"回收是单向的"。

### 14.5.10 dynamic-quota

- CapacityProvider 报告 6000m CPU；
- 两个 CQ 的静态 nominal 都是 4000m；
- Engine 按比例算出各 3000m 有效配额；
- `too-big` 请求 4000m → Pending；
- `fits` 请求 3000m → Admitted。

这演示了 DQO-lite 如何让配额跟随实际容量。

### 14.5.11 label-selector

- CQ selector 要求 `environment=production` 且 `team in (blue, green)`；
- `blue` 满足两个条件 → Admitted；
- `red` 团队不匹配 → Inadmissible；
- `green` 环境不是 production → Inadmissible。

这演示 matchExpressions 与 matchLabels 的组合，以及"结构性不可准入"和"配额不足"的区别。

```bash
go run ./cmd/minikueue -scenario label-selector -ticks 2 -query selective
```

### 14.5.12 localqueue-hold

- 两个 LQ 共享一个 CQ：`open` 和 `held`；
- `held` 的 stopPolicy=Hold；
- tick 1：`open-job` 准入，`held-job` 保持 Pending；
- 操作员把 `held` 改回 None；
- tick 2：`held-job` 准入。

这演示 LQ 级 stopPolicy 只影响自己的 Workload，不会阻塞同 CQ 的其他租户。

### 14.5.13 retry-backoff

- CQ 配置 manual AdmissionCheck；
- tick 1：QuotaReserved，check Pending；
- 操作员以 `requeueAfterSeconds=2` 进入 Retry；
- tick 2：释放配额，`Status.RequeueAt=3`；
- tick 3：重新预留；操作员批准 check；
- tick 4：`Admitted`。

这演示 Retry 的退避语义：不是立即重试，也不是永久失败。

### 14.5.14 dra

- CQ 覆盖 `nvidia.com/gpu`，配额 6；`gpu-a100` 映射到 `nvidia.com/gpu`；
- 两个 Job 各 4 个 Pod、每 Pod 1 个设备；
- tick 1：`dra-job-a` 计入 4 卡并 Admitted，`dra-job-b` 因 4+4>6 Pending。

这演示"设备进入配额账本"，以及"Pod 模板里没有写扩展资源，也仍然会被计费"。

### 14.5.15 tas-slice

- 4 个 host，每个 4 卡；
- `one-pod` 落在 h1；
- `spread-two` 带 `Spreading`，选择当前最空的 h2；
- `slice-four` 每 2 个 Pod 一片，落在 h3/h4。

这演示 slice 会把一个 PodSet 拆成多条 `TopologyAssignment`，以及打散不按名字 first-fit。注意 MiniKueue 的 slice 内部固定按"最空 domain 优先"选域，`Spreading` 控制的是整个 PodSet 的选域；两者不要混为一谈。

### 14.5.16 admission-fair-sharing

- `heavy` 先跑一个 2 CPU 作业并完成，留下历史用量；
- 之后 `heavy` 和 `light` 同时提交 2 CPU 作业，CQ 只放得下一个；
- `light` 历史用量低，先准入；`heavy-next` Pending。

这演示 AFS 只看历史，不看"谁先提交"。

### 14.5.17 concurrent-admission

- `reserved` Flavor 初始 0 配额，`spot` 有 4 CPU；
- tick 1：Job 先落在 `spot`；
- 操作员把 `reserved` 提到 2 CPU；
- tick 2：控制器重新计算，发现 `reserved` 可行，迁移到更优 Flavor。

这演示"先跑起来、再迁移"，以及只向更优 Flavor 迁移的防抖不变量。

### 14.5.18 admission-gate

- `gated-job` 有两个 gate：`security-scan`、`license-check`；
- tick 1：`AdmissionGated=True`，不占配额；
- 操作员清除两个 gate；
- tick 2：正常预留并准入。

这演示 gate 在配额之前，AdmissionCheck 在配额之后。

### 14.5.19 hierarchical-limits

- `org-a` 的 Cohort 级 `borrowingLimit` 是 2 CPU；
- `within-limit` 请求 6 CPU（借 2）→ Admitted；
- `over-limit` 请求 7 CPU（需要更多借用）→ Pending，即使 `org-b` 还有空闲。

这演示 Cohort 级 limit 是子树总量上限，不是单 CQ limit。

### 14.5.20 implicit-cohort

- 两个 CQ 都写 `cohortName: implicit-shared`，但没有 Cohort 对象；
- A 请求 6 CPU（nominal 4）→ 借 2，Admitted。

这演示隐式 Cohort 的借用，不需要额外对象。

### 14.5.21 multikueue

- worker-1 配额 1 CPU，worker-2 配额 4 CPU；
- 2 CPU 的 Workload 同时复制到两个 worker；
- worker-2 先 Admitted，worker-1 的副本被删除。

这演示 MultiKueue 的提名、单一选中和失败副本清理。`Manager.Nominated` 会记录本轮提名的 worker，对应真实 Kueue 的 `status.nominatedClusterNames`；早期实现只做了分发、没有把提名结果暴露出来，属于"行为对但不可观测"的缺陷。

### 14.5.22 cohort-shared-pool

- 显式 Cohort `shared-pool` 定义自己的 CPU nominal 4；
- 两个成员 CQ 的 nominal 都是 0；
- 第一个 3 CPU 作业从共享池借到 3，Admitted；
- 第二个 3 CPU 作业使 Cohort 总量超过 4，Pending。

这演示"显式 Cohort 的 nominal 是额外共享池"：它不是把成员 CQ 的 nominal 再汇总一遍，而是成员可以直接借用的独立额度。成员 CQ 仍必须为资源显式写 `nominalQuota: 0`，否则不具备借用资格。

### 14.5.23 configurable-preemption

- CQ 的 `spec.preemption` 全为 `Never`；
- `PreemptionConfig` 规则：`InsufficientQuota` + `WithinClusterQueue` + `priority < preemptor` + `workload-type=batch`；
- tick 2：`batch-job` 被驱逐，`hero-job` 准入。

这演示配置规则可以在 Classic 全关时单独授权抢占。如果去掉 victim 的 `workload-type=batch` 标签，规则不匹配，作业会保持 Pending。

### 14.5.24 fair-sharing-preemption

- `team-a` 名义 4 CPU、实际使用 6 CPU（借 2）；
- `team-b` 名义 4 CPU、空闲，提交 4 CPU 作业；
- Classic Preemption 全关，只启用 `LessThanOrEqualToFinalShare`；
- tick 2：`team-a/borrower` 被驱逐，`team-b/reclaimer` 准入。

这演示"份额"而不是"优先级"驱动的回收：preemptor 准入后 share 为 0，target 被驱逐后 share 也为 0，满足 `<=` 条件。

### 14.5.25 provisioning-request

- CQ 带 `provisioning-check`，Engine 配置 `DelayTicks=2, Failures=1`；
- tick 1：预留后第一次置备失败，释放配额并重排队到 tick 3；
- tick 3：重新预留，等待置备；
- tick 5：检查 Ready，Workload Admitted。

这演示"预留期间置备、失败不丢重试计数"的 ProvisioningRequest 主干。

### 14.5.26 resource-transform

- 三个 Pod 各请求 `gpu=2, gpumem=1024, gpucores=20`；
- 变换后每个 Pod 计入 `total-gpumem=2048, total-gpucores=40`，`gpu` 被忽略；
- CQ 只有 4096/80，因此前两个 Admitted、第三个 Pending。

这演示配额账本看到的是变换后的资源，而不是 Pod 原始请求。

### 14.5.27 multikueue-failover

- worker-1 初始 `Down`，worker-2 被提名并 Admitted；
- tick 2：worker-2 失联，`WorkerUnavailable`（1/1 ticks），保持选中；
- tick 3：超过 `WorkerLostTimeout`，`WorkerLost`，重新分发到 worker-1；
- tick 4：worker-2 恢复，清理其陈旧副本；
- tick 5：保持 worker-1 选中。

这演示 MultiKueue 的失联超时、重新分发和恢复后清理；对应测试 `TestDownWorkerIsNotNominated`、`TestFailoverAfterWorkerLostTimeout`、`TestNoAvailableWorker`。

## 14.6 测试策略

### 14.6.1 单元测试

- `api`：资源格式化、TotalUsage（含 reclaimable）、校验、条件、LabelSelector（nil/空/MatchLabels/MatchExpressions/非法 operator）、Topology/Domain/CapacityProvider 校验。
- `cache`：Add/Remove、usage、excluding、lendable、TopologyUsage。
- `queue`：排序、过滤、公平顺序。
- `scheduler`：Flavor fallback、Cohort 借用、borrow/lend limit、同 CQ 抢占、回收、借用时抢占、公平权重/借用 DRS、多资源组、层级 Cohort、TAS（Required/Preferred/Unconstrained/Group）、有效配额。
- `store`：克隆隔离、引用校验、Cohort 环检测、Topology/Domain/Provider 存取、隐式 Cohort 允许。
- `metrics`：计数器（含 PodsReady 超时、回收 Pod）和 exposition。
- `multikueue`：AllAtOnce 选中与清理、Incremental 扩张、重复 tick 不重复创建副本、Nominated 提名列表、Down worker 跳过、失联超时切换、恢复后陈旧副本清理。
- 新增能力测试：DRA 计费/重复计费保护/回收释放设备配额、admission gate、AFS 排序、Concurrent 迁移、Cohort 级 limit、TAS slice/spreading、隐式 Cohort、SortByScore。
- 回归测试（第一轮对抗性审计新增）：retryCount 跨重新预留持久化、TAS 回收同时释放拓扑用量、WaitForPodsReady 重试上限只统计超时、Cohort borrowingLimit 不被空闲兄弟掩盖、Admission 后 Cache 状态刷新、最大执行时间跨抢占累计、StrictFIFO 不被 inadmissible 队头阻塞、Concurrent 迁移尊重 StopPolicy、拓扑模式/组校验、DQO 漏报资源按 0 计、LabelSelector 缺失键语义。
- 回归测试（第二轮对抗性审计新增）：TAS 跨层级双计（rack 占满后 hostname 不能再放）、回收释放 `pods`、Store 克隆隔离、`UpdateWorkload` 保留 UID/spec、DRA 映射到空/未覆盖资源被拒绝、设备映射到 `pods` 不重复计费、MultiKueue 重名 worker 清理、未知 dispatcher 不广播、`Selected` 副本被删后重新分发、Concurrent 迁移保留 AdmissionCheck/PodsReady、evict 保留 retryCount、空资源名、Topology 更新拒绝不兼容 levels。
- 实现补全测试：HAMi 资源变换、Retain/固定倍率、倍率缺失、拒绝 `pods`/重复 input、PreemptionConfig 校验、五种 scope + priority/numericLabels/labelSelector/clusterQueueSelector 匹配、quota 可行但拓扑不可行的 trigger 判定、Cohort 权重、Configurable 抢占、Fair Sharing 抢占、Provisioning 时间线、Backoff 覆盖固定延迟。

### 14.6.2 控制器集成测试

- 基本生命周期；
- StrictFIFO/BestEffortFIFO；
- 抢占 + 重排队；
- AdmissionCheck 两阶段/拒绝/重试/自动；
- Hold/HoldAndDrain；
- Deactivate；
- 最大执行时间；
- namespaceSelector 的 matchLabels 与 matchExpressions；
- LocalQueue stopPolicy 的隔离效果；
- AdmissionCheck Retry 的 requeueAfterSeconds/retryCount；
- WaitForPodsReady 超时与 MarkPodsReady；
- 动态回收与超额回收拒绝；
- DQO-lite 有效配额；
- TAS 场景；
- SchedulingStats 与 QueueStatus；
- ProvisioningRequest-lite 的 Retry/Ready 时间线；
- 指数退避对抢占 requeue 的影响；
- Configurable Preemption 授权驱逐；
- Fair Sharing 抢占收回借用；
- 资源变换后的 TotalUsage；
- CLI `-get`/`-describe`/`-stop`/`-resume` 手工冒烟（`cmd` 包没有自动化测试）；
- 指标。

### 14.6.3 场景测试

`TestAllScenariosRunAndPreserveQuotaInvariants`：

- 运行所有 26 个引擎场景（`scenario.Names()`），每个 tick 检查配额与拓扑不变量；
- 2 个 MultiKueue 演示不在这个测试里，由 `internal/multikueue` 的单元测试和 `TestMultiKueueFailoverScenario` 单独覆盖。

`TestRandomizedStressPreservesQuotaInvariants`：

- 3 个 CQ 共享 Cohort；
- 80 个随机 Workload；
- 40 个 tick；
- 随机完成；
- 每 tick 检查：
  - usage 非负；
  - 无 Cohort 时 usage ≤ nominal；
  - 显式 borrowingLimit 不被突破；
  - Cohort 总量 ≤ 总 nominal；
  - Cache 与 Store 状态一致。

### 14.6.4 不变量

测试检查的核心不变量：

```text
1. 任何 Flavor/Resource 的 usage >= 0
2. 无 Cohort 的 CQ：usage <= effective nominal
3. 显式 borrowingLimit：usage <= nominal + limit
4. Cohort 树：总 usage <= 总 nominal（含共享池）
5. Cohort 级 borrowingLimit：子树内每个成员的正借用之和 <= limit（不能用空闲成员抵消）
6. TAS：每个 topology/level/domain 的 usage <= 聚合容量
7. TAS assignment 覆盖的 Pod 数必须等于 PodSet 的有效 Pod 数（回收/抢占后仍然成立）
8. Admission.Usage 必须等于 Workload 的 TotalUsage（回收/迁移后仍然成立）
9. QuotaReserved/Admitted 的 Workload 必须在 Cache 中
10. Cache 中的 Workload 必须是 QuotaReserved/Admitted
```

这些不变量比"某个场景输出对不对"更重要，因为它们能在随机操作下抓出账本泄漏/超额。

## 14.7 实际验证结果

在 Windows 11、Go 1.27.0、Intel Core Ultra 9 285H 上执行：

```bash
cd mini-kueue
gofmt -l .                 # 无输出
go vet ./...               # 通过
go test ./... -count=1     # 全部通过
```

测试数量：

```text
146 个顶层测试
+ 26 个场景子测试
（另有 scheduler selector 12 个、multikueue 4 个子测试；
  按 `--- PASS` 全量统计会更多）
```

覆盖率（`go test ./... -cover`）：

```text
internal/api         52.6%
internal/cache       71.4%
internal/controller  80.8%
internal/metrics    100.0%
internal/multikueue  90.9%
internal/queue       82.5%
internal/scenario    94.1%
internal/scheduler   73.7%
internal/store       59.4%
```

说明：

- `cmd` 没有单元测试，覆盖率为 0；
- `store` 的很多代码被集成测试间接执行，但 `go test -cover` 只统计本包测试；本轮 `store` 提升来自克隆隔离、引用校验和 Topology 更新测试；
- `api` 的覆盖率下降是因为新增了多个 `Clone` 方法，测试主要覆盖校验路径；
- 覆盖率不是目标，关键是不变量和场景测试。

基准（同一台机器上的采样，不保证复现同样数字；关注数量级和 allocs）：

```text
BenchmarkTryAdmit-16    91039    13916 ns/op    880 B/op    8 allocs/op
BenchmarkTryAdmit-16    95763    30348 ns/op    880 B/op    8 allocs/op
BenchmarkTryAdmit-16    35900    34021 ns/op    880 B/op    8 allocs/op
```

条件：1 CQ、500 个已准入 Workload、不触发抢占。只用于理解算法成本；高负载时会出现更高离群值。新增的 Configurable/Fair Sharing 逻辑只在直接准入失败后执行，不改变直接准入路径的分配次数。

完整记录见 [VERIFICATION.md](../VERIFICATION.md)。

## 14.8 如何加一个场景

在 `internal/scenario/scenarios.go` 中：

```go
func MyScenario() *controller.Engine {
    e := New()
    MustCQ(e, SimpleCQ("my-cq", "default", api.RL("cpu", 4000)))
    MustLQ(e, "default", "q", "my-cq")
    MustNamespace(e, "default", nil)
    MustSubmit(e, Workload("default", "job", "q", 0, 1, api.RL("cpu", 2000)))
    return e
}
```

然后在 `Names()` 和 `Run()` 中注册。为新场景加一个测试，至少检查：

- 初始状态；
- Tick 后状态；
- 配额/借用量；
- 驱逐/条件（如果有）。

## 14.9 如何加一个抢占测试

```go
func TestMyPreemption(t *testing.T) {
    cq := testCQ("cq", 2000)
    cq.Spec.Preemption.WithinClusterQueue = api.PreemptLower
    s := New(cache.New(), NewView([]*api.ClusterQueue{cq}, nil))

    low := testWorkload("low", 0, 2000, nil)
    commit(t, s.Cache, low, s.TryAdmit(low, cq))

    high := testWorkload("high", 10, 1000, nil)
    d := s.TryAdmit(high, cq)
    if d.Admission == nil || len(d.Victims) != 1 {
        t.Fatalf("expected one victim, got %+v", d)
    }
}
```

测试应覆盖：

- 允许/禁止的优先级；
- borrowing/lending limit；
- 最小 victim 集合；
- 同优先级 tie-break；
- 失败时 Cache 不变。

## 14.10 调试技巧

### 打开详细输出

```bash
go run ./cmd/minikueue -scenario reclaim -ticks 6
```

### 只跑某个测试

```bash
go test ./internal/controller -run TestAdmissionCheck -v
```

### 打开 race（需要 cgo）

```bash
CGO_ENABLED=1 go test -race ./...
```

Windows 上如果没有 C 编译器，`-race` 会失败；这是环境限制，不是代码问题。

### 加日志

在 `Engine` 的 `event` 或 `reserve`/`evict` 中加 `fmt.Printf`，或者扩展 `Event` 类型。不要在生产 Kueue 控制器里随意加高频日志。

## 14.11 本章小结

- MiniKueue 用 Store/Cache/Scheduler/Controller 分层，把"对象、账本、决策、编排"分开。
- `Tick` 是确定性协调循环，先决策后提交，失败可回滚。
- 场景覆盖 FIFO、借用、抢占、回收、AdmissionCheck、DRA、TAS slice、AFS、Concurrent、gate、Cohort limit、隐式 Cohort、MultiKueue 与失联切换。
- 测试包括单元、集成、随机不变量；具体数量见 [VERIFICATION.md](../VERIFICATION.md)。
- 覆盖率不是目标；不变量和端到端场景更重要。
- 可以按同样的模式加场景、加测试、加扩展（ProvisioningRequest、资源变换、Configurable Preemption）。

---

[← 上一章：MiniKueue 设计](13-mini-kueue-design.md) | [返回目录](../README.md) | [下一章：与真实 Kueue 的差距 →](15-gap-to-real-kueue.md)
