# 第 33 章 · 实现补全：资源变换、ProvisioningRequest、可配置抢占与公平抢占

> 这一章把第 15 章差距矩阵里最影响"能不能覆盖 Kueue 语义"的四块补上，并解释实现取舍。所有内容都在 `mini-kueue/` 中可运行、可测试。

## 33.1 为什么单独讲"补全"

第 13～15 章已经讲清 MiniKueue 的主干：队列、配额、Cohort、Flavor、抢占、回收、TAS、DRA、AFS、Concurrent、MultiKueue。但差距矩阵里有四类能力会直接影响"配额账本看到的资源"和"谁可以抢谁"：

1. **资源变换**：Pod 请求的资源不一定就是配额资源；
2. **ProvisioningRequest**：准入检查可以持续很多轮，期间配额必须保持；
3. **Configurable Preemption**：抢占规则可以声明式定义，而不只是四个固定字段；
4. **Fair Sharing 抢占**：份额可以成为抢占的判据，而不只是优先级。

如果这四块缺失，教程对"Kueue 全貌"的覆盖会在最关键的调度决策处断掉。本章补全它们，并保持三个工程原则：

- **纯决策 + 提交分离**：Scheduler 只算，Controller 提交；
- **配置冻结**：Workload 提交时冻结影响账本的配置；
- **可观测**：每个新行为都有事件、条件或 CLI 查询。

## 33.2 资源变换

### 33.2.1 问题

Pod 可能请求：

```text
example.com/gpu: 2          # vGPU 数量
example.com/gpumem: 1024    # 每个 vGPU 的显存
example.com/gpucores: 20    # 每个 vGPU 的算力
```

但平台想按"总显存/总算力"配额，而不是按 vGPU 数量。直接按原始请求计费会导致：

- vGPU 数量相同的作业，显存/算力可能差很多；
- MIG/切分卡的资源无法映射到物理卡；
- 配额合同无法用统一单位表达。

资源变换在"配额账本"这一层做归一化：

```text
total-gpumem  = gpumem × gpu
total-gpucores = gpucores × gpu
gpu            = 忽略
```

### 33.2.2 数据模型

```go
type TransformStrategy string

const (
    TransformRetain  TransformStrategy = "Retain"
    TransformReplace TransformStrategy = "Replace"
)

type ResourceTransformation struct {
    Input      ResourceName
    Strategy   TransformStrategy
    MultiplyBy string
    Outputs    ResourceList
}
```

语义：

| 字段 | 说明 |
|---|---|
| `Input` | 被变换的输入资源 |
| `Strategy` | `Replace` 删除 input；`Retain` 保留 input |
| `MultiplyBy` | 资源名（取 Pod 请求量）或固定整数（教学扩展） |
| `Outputs` | 每单位 input 产生的输出资源 |

`Replace` + 空 `Outputs` 是官方语义里的"忽略该资源"。

### 33.2.3 算法

`ApplyResourceTransformations` 的关键是**所有规则读原始 requests**：

```go
original := perPod.Clone()
out := original.Clone()
for _, t := range transformations {
    input := original.Get(t.Input)
    if input <= 0 { continue }
    multiplier := 1
    if t.MultiplyBy != "" {
        if fixed, ok := parseFixedMultiplier(t.MultiplyBy); ok {
            multiplier = fixed
        } else if v, ok := original[ResourceName(t.MultiplyBy)]; ok {
            multiplier = v
        } else {
            multiplier = 0
        }
    }
    amount := input * multiplier
    if t.Strategy == "" || t.Strategy == TransformReplace {
        delete(out, t.Input)
    }
    for r, perUnit := range t.Outputs {
        out[r] += amount * perUnit
    }
}
```

为什么读 `original` 而不是 `out`：

- 防止 A 的输出成为 B 的输入，产生链式/环状效果；
- 让规则顺序不影响结果（除了同一资源的累加）；
- 与"从 PodSpec 一次性换算"的直觉一致。

### 33.2.4 与 DRA 的顺序

`Workload.PodSetPerPodUsage` 的顺序是：

```text
Requests
  → 合并 Devices（DeviceClass → 逻辑资源；同一资源取 max）
  → ApplyResourceTransformations
  → per-pod usage
```

为什么 DRA 先：

- 设备请求需要先映射成逻辑资源，资源变换才能看到它；
- 同一资源既出现在 `Requests` 又出现在 `Devices` 时，先取 max 防止双计；
- 变换只处理"账本资源"，不负责设备分配。

### 33.2.5 配置冻结

`Engine.Submit` 把 `Engine.ResourceTransformations` 复制到 `Workload.Spec.ResourceTransformations`。之后：

- 改 Engine 配置不影响已提交 Workload；
- `Store.UpdateWorkload` 保留 Spec，所以状态更新不会丢失变换；
- 重新 `Submit` 是一次新 incarnation，会重新复制当前配置。

这是"账本可解释"的关键：给定一个 Workload，永远能回答"它按什么规则计费"。

### 33.2.6 测试与场景

```bash
go run ./cmd/minikueue -scenario resource-transform -ticks 2
```

```text
[QuotaReserved] default/vgpu-a: reserved {total-gpucores=40, total-gpumem=2048, pods=1}
[QuotaReserved] default/vgpu-b: reserved {total-gpucores=40, total-gpumem=2048, pods=1}
vgpu-c Pending
```

单元测试：

- `TestResourceTransformationsMatchHamiExample`：HAMi 风格的 2 倍 vGPU；
- `TestResourceTransformationsRetainAndFixedMultiplier`：Retain 保留 input；
- `TestResourceTransformationMissingMultiplierContributesZero`：倍率资源缺失时输出 0；
- `TestValidateResourceTransformationsRejectsReservedName`：拒绝 `pods`/重复 input。

### 33.2.7 边界

- `multiplyBy` 为负数/非整数非资源名：校验不会崩，但倍率为 0 或解析失败；生产配置要在 webhook 里严格校验；
- `outputs` 与 input 同名 + Replace：先删后加，净效果是"替换"；不建议这样写；
- 变换后的资源必须被 CQ 覆盖，否则调度器看不到它；MiniKueue 不强制校验这一点，生产要在配置评审时检查；
- 小数/单位后缀（`1.5`、`500m`）不支持；MiniKueue 用整数，生产用 `resource.Quantity`。

## 33.3 ProvisioningRequest-lite 与退避

### 33.3.1 状态机

```text
QuotaReserved
  → AdmissionCheck Pending
      ├── retryCount < Failures
      │     → Retry（释放预留，RequeueAt = clock + Delay）
      │     → 重新调度 → 重新预留
      └── retryCount >= Failures
            → 等待 DelayTicks
            → Ready
            → Admitted
```

关键点：

- Retry **释放配额**，否则"等容量"会永远占着配额；
- RetryCount 跨预留保留，否则每次重新预留都从 0 开始，会无限重试；
- 成功路径需要一个"置备计时器"，否则检查会在预留的同一 tick 立即 Ready；
- 计时器用 `AdmissionCheckState.LastTransitionTick`，在 `reserveWith` 时设为 `clock`。

### 33.3.2 代码位置

```text
internal/controller/engine.go
  type ProvisioningConfig
  reserveWith()       // 新检查写入 LastTransitionTick
  evaluateChecks()    // Pending 分支驱动 Retry/Ready
  releaseReservation() // 释放配额、保留 retryCount
```

### 33.3.3 时间线

场景 `provisioning-request` 配置 `DelayTicks=2, Failures=1`：

| Tick | 事件 | 状态 |
|---|---|---|
| 1 | 预留 → 第一次置备失败 → Retry | Pending，RequeueAt=3 |
| 2 | 退避 | Pending |
| 3 | 重新预留 | QuotaReserved |
| 4 | 置备中 | QuotaReserved |
| 5 | Ready → Admitted | Admitted |

### 33.3.4 指数退避

`BackoffConfig`：

```go
type BackoffConfig struct {
    BaseTicks  int64
    MaxTicks   int64
    Multiplier float64
}
```

```text
delay(attempt) = min(MaxTicks, BaseTicks × Multiplier^attempt)
```

使用点：

- `evict(..., requeue=true, delay)`：配置了 Backoff 时覆盖固定延迟；
- `releaseReservation(..., requeueAt)`：当 reason 是 `AdmissionCheckRetry` 且没有显式延迟时，用 Backoff 计算并递增 `Requeues`。

显式 `requeueAfterSeconds` 仍然优先，因为外部控制器可能知道更准确的退避。

### 33.3.5 测试

- `TestProvisioningRequestLiteRetriesThenAdmits`：完整 5 tick 时间线；
- `TestExponentialBackoffOverridesFixedPreemptionDelay`：抢占 victim 的 RequeueAt 从 4 变成 5；
- `TestRetryCountPersistsAcrossReservations`：Retry 后重新预留计数不丢。

### 33.3.6 与真实 ProvisioningRequest 的差距

- 没有 `ProvisioningRequest` 对象和 attempt 后缀；
- 没有 `ProvisioningRequestConfig.retryStrategy` 的 `backoffLimitCount`；
- 没有云 autoscaler/节点 Ready 的真实信号；
- 没有 `podSetUpdates`；
- 没有 `check-capacity`/`best-effort-atomic-scale-up` 的差异。

这些在文档和示例中保留为"生产版要做"的清单，避免把 lite 当生产实现。

## 33.4 Configurable Preemption-lite

### 33.4.1 为什么要补

Classic Preemption 只有四个字段：

```text
withinClusterQueue
reclaimWithinCohort
borrowWithinCohort
```

它们能表达"同队列低优先级可以被抢""Cohort 内借用者可以被收回"，但表达不了：

- 只允许某个标签的 Workload 被抢；
- 只允许跨某个 CQ 选择 victim；
- 只在"配额够但拓扑放不下"时触发；
- 按 numeric label（例如节点数）选择 victim。

Configurable Preemption 把这些变成声明式规则。

### 33.4.2 数据模型

```go
type PreemptionConfig struct {
    Name  string
    Rules []PreemptionRule
}

type PreemptionRule struct {
    Name               string
    Trigger            PreemptionTrigger
    CandidateSelectors []PreemptionCandidateSelector
}

type PreemptionCandidateSelector struct {
    Scope                SelectorScope
    Priority             *PriorityConstraint
    NumericLabels        []NumericLabelConstraint
    LabelSelector        *LabelSelector
    ClusterQueueSelector *LabelSelector
}
```

### 33.4.3 trigger

| Trigger | 含义 | MiniKueue 判定 |
|---|---|---|
| `Always` | 每次尝试抢占都适用 | 直接匹配 |
| `InsufficientQuota` | 配额本身不够 | `findAssignmentWithoutTopology` 也失败 |
| `QuotaFeasibleAndInsufficientTopology` | 配额够但拓扑放不下 | 跳过拓扑的分配成功，完整分配失败 |

这个区分很重要：如果配额够、只是 TAS 放不下，用"配额不足"的规则去抢可能选错 victim。

### 33.4.4 scope

| Scope | 含义 | MiniKueue |
|---|---|---|
| `WithinLocalQueue` | 同一个 LocalQueue | namespace + queueName 相同 |
| `WithinClusterQueue` | 同一个 CQ | admission.clusterQueue 相同 |
| `WithinParentCohort` | 父 Cohort 内 | 用根 Cohort 近似，排除自身 CQ |
| `WithinCohortTree` | 整个 Cohort 树 | 根 Cohort 相同 |
| `AnyClusterQueue` | 所有 CQ | 不限制 |

`AnyClusterQueue` 很强，生产要配合 `clusterQueueSelector`/priority 使用。

### 33.4.5 selector matching

`selectorMatches` 的顺序：

```text
scope → priority → numericLabels → labelSelector → clusterQueueSelector
```

- priority：victim priority 与 incoming priority 比较；`Boosted` 给 incoming +1000（教学近似）；
- numericLabels：victim 标签值 vs incoming 同名标签值；缺失时用 `FallbackValue`；再做 min/max；
- labelSelector：victim 的 Workload 标签；
- clusterQueueSelector：victim 所在 CQ 的 labels。

### 33.4.6 候选合并与最小化

```text
classic = 按 spec.preemption 收集
config  = 按规则收集
candidates = dedupe(classic + config)
sort(candidates)
for victim in candidates:
    removed.add(victim)
    if findAssignment succeeds:
        break
minimize(reverse(selected))
```

配置规则只改变"谁可以当 victim"，不改变提交决策结构。这样新规则不会破坏配额/拓扑账本。

### 33.4.7 测试与场景

```bash
go run ./cmd/minikueue -scenario configurable-preemption -ticks 2
```

```text
[Evicted] default/batch-job: preempted to accommodate default/hero-job
[QuotaReserved] default/hero-job
[Admitted] default/hero-job
```

- `TestConfigurablePreemptionAuthorizesEviction`：Classic 全关时规则生效；
- `TestValidatePreemptionConfig`：trigger/scope/comparison/numeric label 校验。

### 33.4.8 风险

- 对称规则 → A↔B 级联抢占；
- `AnyClusterQueue` → 误伤关键业务；
- numeric label 缺失 → fallback 让规则意外匹配；
- 与 Classic 叠加 → 两套规则都生效，victim 可能超出预期。

生产评审要画出"谁可以抢谁"的有向图，并检查是否存在环。

## 33.5 Fair Sharing 抢占-lite

### 33.5.1 两条规则

```text
LessThanOrEqualToFinalShare:
    preemptorNewShare <= targetNewShare

LessThanInitialShare:
    preemptorNewShare < targetOldShare
```

- `targetOldShare`：victim CQ 在驱逐前的 share；
- `targetNewShare`：victim CQ 在驱逐后的 share；
- `preemptorNewShare`：preemptor 假设准入后的 share。

### 33.5.2 候选

```text
同一根 Cohort
优先级不高于 preemptor
FairShare(victimCQ) > 0
```

第三个条件确保 target 确实在借用；份额为 0 的 CQ 不应该被公平抢占（它已经在 nominal 内）。

### 33.5.3 share 计算

`fairShareValue` 复用 `FairShare` 的公式：

```text
borrowed = max(0, usage - nominal)
lendable = 同 Cohort 其他 CQ 的 nominal 总和（受 lendingLimit 截断）+ 共享池
share    = max over flavor/resource (borrowed / lendable) / weight
```

区别：

- `usage` 可以加"假设 admission"的用量；
- `lendable` 只看 nominal/limit，不随当前 usage 变化；`removed` 只影响 usage 和共享池空闲；
- `weight` 沿 CQ → 根路径乘 Cohort 权重。

### 33.5.4 场景

```bash
go run ./cmd/minikueue -scenario fair-sharing-preemption -ticks 2
```

- `team-a` 名义 4 CPU，使用 6 CPU（借 2）；
- `team-b` 名义 4 CPU，空闲，提交 4 CPU；
- `team-b` 不能直接放下（Cohort 总量 10 > 8）；
- 驱逐 `team-a/borrower` 后：
  - `preemptorNewShare = 0`（team-b 在 nominal 内）；
  - `targetNewShare = 0`（team-a 回到 nominal 内）；
  - `0 <= 0`，满足 FinalShare；
- 准入 `team-b/reclaimer`。

测试：`TestFairSharingPreemptionReclaimsBorrowedQuota`。

### 33.5.5 差距

- 不是完整加权 DRF；
- 没有 AlmostLCA；
- 候选取"低优先级、最近准入"，不是"目标 CQ share 最高"；
- 没有预抢占（preemption while admitting）、没有 flavorFungibility；
- 没有采样窗口/精确锚点。

## 33.6 CLI 与可观测性补全

新增参数：

| 参数 | 作用 | 对应真实操作 |
|---|---|---|
| `-get workloads` | 表格列出 Workload | `kueuectl list workload` |
| `-get clusterqueues` | CQ/cohort/strategy/preemption/usage | `kueuectl list clusterqueue` |
| `-get localqueues` | LQ → CQ、stopPolicy | `kueuectl list localqueue` |
| `-get cohorts/flavors/topologies` | 配置视图 | `kubectl get` |
| `-describe ns/name` | conditions/checks/admission/evictions | `kubectl describe workload` |
| `-stop ns/name` | Deactivate | `kueuectl stop workload` |
| `-resume ns/name` | Activate | `kueuectl resume workload` |
| `-query <cq>` | Visibility 快照 | Visibility API |

这些参数让 MiniKueue 能在没有 Kubernetes 的环境里练习第 31 章的排障流程。

## 33.7 工程原则复盘

### 33.7.1 纯决策

`Scheduler.TryAdmit` 仍然不修改 Cache。新增的 Configurable/Fair Sharing 抢占只返回 `Victims`，由 Controller 统一 `evict` + `reserve`。这样：

- 决策可单测；
- 回滚简单；
- 不会出现"决策成功但提交失败"的半状态。

### 33.7.2 配置冻结

- 资源变换在 Submit 冻结到 Workload；
- PreemptionConfig 在 Scheduler 重建时挂载，规则变更下一 tick 生效；
- ProvisioningConfig/Backoff 是 Engine 级，测试和场景显式设置。

### 33.7.3 两套账本

新增功能仍然要同时维护：

- 配额账本（Cache/Admission.Usage）；
- 拓扑账本（TopologyAssignments/TopologyUsage）；
- 生命周期账本（Conditions/AdmissionChecks/RequeueAt）；
- 历史账本（AFS usage）。

第 27 章的审计方法在这里同样适用：断言"撤销路径"（Retry/驱逐/回收）而不仅是"前进路径"。

### 33.7.4 测试金字塔

| 层 | 新增覆盖 |
|---|---|
| 单元 | 变换数学、PreemptionConfig 校验、share 公式 |
| 控制器 | Provisioning 时间线、Backoff、抢占 victim |
| 场景 | 4 个新场景可运行 |
| 随机不变量 | 现有随机测试继续覆盖配额/TAS/Admission 一致性 |
| CLI | `-get`/`-describe`/`-stop`/`-resume` 冒烟 |

## 33.8 验证命令

```bash
cd mini-kueue
gofmt -l .
go vet ./...
go test ./... -count=1
go test ./... -cover

go run ./cmd/minikueue -scenario resource-transform -ticks 2
go run ./cmd/minikueue -scenario provisioning-request -ticks 5
go run ./cmd/minikueue -scenario configurable-preemption -ticks 2
go run ./cmd/minikueue -scenario fair-sharing-preemption -ticks 2

cd ..
python scripts/verify_docs.py
bash scripts/verify_all.sh
```

实际数字以 [VERIFICATION.md](../VERIFICATION.md) 为准；本章不重复易过期的计数。

## 33.9 仍缺什么

补全后仍然保留的差距：

| 能力 | 仍缺 |
|---|---|
| 资源变换 | Quantity/小数、Configuration 对象、webhook 默认化、CQ 覆盖校验 |
| ProvisioningRequest | 真实对象、云扩容、retryStrategy、podSetUpdates |
| Configurable Preemption | preemptor selector、规则优先级、逐层 ParentCohort、CRD |
| Fair Sharing 抢占 | AlmostLCA、目标 CQ 排序、预抢占、采样窗口 |
| 其他 | DRA 设备分配、TAS 热替换、MultiKueue 网络/凭据、HA/持久化、Visibility HTTP API |

这些不是"文档没写"，而是模拟器有意不做的部分。第 30 章给出把它们推向生产控制器的路径。

## 33.10 本章小结

- 资源变换让配额账本与 Pod 原始请求解耦；关键是读原始请求、冻结配置、与 DRA 去重。
- ProvisioningRequest-lite 用"预留期间置备 + Retry 释放配额 + 延迟 Ready"覆盖主干；退避由 BackoffConfig 统一。
- Configurable Preemption 把"谁可以当 victim"变成声明式规则；trigger 要区分配额不足与拓扑不足。
- Fair Sharing 抢占用 share 而不是优先级做判据；两种 strategy 的等号/严格小于差异要明确。
- 新增能力仍然遵守纯决策、配置冻结、两套账本和测试金字塔。
- 差距矩阵没有被"假装填满"：每一项都标注了 lite 与生产版的边界。

---

[← 上一章：高可用、灾备与一致性](32-high-availability-dr.md) | [返回目录](../README.md) | [下一章：MultiKueue 多集群调度深入 →](34-multikueue-deep-dive.md)
