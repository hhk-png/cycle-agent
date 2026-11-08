# 第 7 章 · 抢占与回收

## 7.1 术语

| 术语 | 含义 |
|---|---|
| Preemptor | 正在申请资源的 Workload |
| Preemptee | 被驱逐的 Workload |
| Target ClusterQueue | preemptee 所在的 ClusterQueue |
| Preempting ClusterQueue | preemptor 所在的 ClusterQueue |
| Reclaim | 本 CQ 收回被其他 CQ 借走的 nominal 配额 |
| Borrow | 本 CQ 借用 Cohort 内其他 CQ 的空闲配额 |

抢占和回收经常被混为一谈。可以这样区分：

- **抢占**：为了给某个 Workload 腾地方，驱逐别的 Workload。
- **回收**：本 CQ 的 nominal 被借走了，现在自己要用，要求借入方归还。

回收通常通过"抢占借入方的 Workload"来实现，所以两者在代码路径上很接近。

## 7.2 什么时候允许抢占

Kueue 的 Classic Preemption 允许在以下情况发生：

1. **同一个 ClusterQueue 内**：pending Workload 放不下，且存在优先级更低的已准入 Workload。
2. **Cohort 内回收**：pending Workload 在本 CQ 的 nominal 范围内，但配额被其他 CQ 借走；那些借用者正在超过自己的 nominal。
3. **Cohort 内借用时抢占**：pending Workload 自己也需要借用，配置了 `borrowWithinCohort`，允许抢占 Cohort 内的低优先级 Workload。

另外，Fair Sharing 开启时，还会为了"更公平的份额"进行抢占。

Classic Preemption 的资格判定可以更精确地写成三条：

```text
一个 pending Workload 可以发起 Classic Preemption，当且仅当：
  (a) 它的请求低于该 Flavor 的 nominal quota；或
  (b) borrowWithinCohort 已启用；或
  (c) withinClusterQueue 已启用，且同 CQ 存在候选人。
```

注意 (a) 的措辞是"低于 nominal"，不是"低于 nominal + borrowingLimit"。如果 Workload 本身就要借很多，只有 (b)/(c) 能救它。这也是 `reclaimWithinCohort` 与 `borrowWithinCohort` 经常被配错的地方。

## 7.3 配置字段

```yaml
spec:
  preemption:
    withinClusterQueue: LowerPriority
    reclaimWithinCohort: LowerPriority
    borrowWithinCohort:
      policy: LowerPriority
      maxPriorityThreshold: 100
```

### `withinClusterQueue`

| 值 | 含义 |
|---|---|
| `Never` | 不抢占同 CQ 的 Workload |
| `LowerPriority` | 只抢占优先级更低的 |
| `LowerOrNewerEqualPriority` | 抢占更低优先级，或同优先级但更新（更晚创建/准入）的 |

注意真实 API 的值是 `LowerOrNewerEqualPriority`，不是简单的 `LowerOrEqual`。它的语义是"同优先级时，新作业让位给老作业"，避免同优先级互相抢占形成循环。

### `reclaimWithinCohort`

| 值 | 含义 |
|---|---|
| `Never`（默认） | 不回收 |
| `LowerPriority` | 只抢占优先级更低的借用者 |
| `Any` | 不管优先级，都可以抢占借用者 |

`Any` 很强，适合关键业务队列，但可能导致低优先级作业被反复驱逐。

### `borrowWithinCohort`

| 字段 | 含义 |
|---|---|
| `policy` | `Never`（默认）或 `LowerPriority` |
| `maxPriorityThreshold` | 只允许抢占优先级 ≤ 该阈值的 Workload；不填则不限 |

它只适用于 Classic Preemption，不能和 Fair Sharing 混用。

## 7.4 Classic Preemption 的候选人与目标

### 7.4.1 候选人

候选人来自：

- 同 CQ 的已准入 Workload，满足 `withinClusterQueue`；
- Cohort 内其他 CQ 的已准入 Workload，满足：
  - 该 CQ 正在借用（usage 超过 nominal，至少在一个双方都需要的资源上）；
  - 满足 `reclaimWithinCohort` 或 `borrowWithinCohort` 的优先级条件。

### 7.4.2 排序

候选人通常按以下顺序排序：

1. 来自借用队列的优先（更该被回收）；
2. 优先级低的优先；
3. 最近才准入的优先（更"新"的作业更容易被牺牲）；
4. 稳定的 tie-breaker。

### 7.4.3 选择目标

Kueue 不是简单地"抢一个最大的"，而是按四条启发式依次尝试：

1. **全在目标 CQ**：如果候选人都属于同一个目标 CQ，贪心地加候选，允许目标 CQ 的用量超过 nominal，直到 borrowingLimit；
2. **borrowWithinCohort**：如果启用，按 `maxPriorityThreshold` 过滤后贪心加候选，允许 preemptor 借用；
3. **目标 CQ 未超 nominal**：如果目标 CQ 当前用量低于 nominal，贪心加候选，但**不允许借入**；
4. **只抢 preempting CQ 自己的**：如果前面都没放下，就只从 preemptor 自己的 CQ 里选候选，允许借用。

最后再做最小化：从后往前遍历 target，如果把某个 victim 加回去 preemptor 仍然放得下，就把它从 victim 集合移除。这样能减少驱逐数量，降低对集群的扰动。

这四条顺序解释了一个常见现象：**为什么明明 Cohort 里有低优先级作业，preemptor 却先抢了自己 CQ 的作业？** 因为前两条启发式优先，只有它们失败后才会走到第 3/4 条。

## 7.5 一个完整的抢占例子

CQ 配额 2 CPU，`withinClusterQueue: LowerPriority`：

```text
已准入：
  low   priority 0   CPU 2

Pending：
  high  priority 10  CPU 1
```

流程：

1. high 直接申请 → 2+1 > 2，失败；
2. 找同 CQ 候选人 → low 优先级 0 < 10，符合；
3. 模拟移除 low → 0+1 ≤ 2，可放下；
4. 驱逐 low；
5. 准入 high。

结果：

- high 运行；
- low 进入 Pending，带 `Evicted=Preempted` 条件；
- low 在退避时间后重新排队；
- 如果 high 一直运行，low 可能再次 pending；
- 如果 high 完成，low 可以重新准入。

MiniKueue 的 `preemption` 场景就是这个流程。

## 7.6 回收例子

Cohort：A、B 各 nominal 4，总 8。

```text
已准入：
  A-steady   2 CPU
  B-borrower 6 CPU   （B nominal 4，借 2）

Pending：
  A-owner    2 CPU
```

流程：

1. A 当前 2，再要 2 → 新用量 4，仍在 nominal 内；
2. 但 Cohort 总量 2+6+2=10 > 8，直接失败；
3. A 的 `reclaimWithinCohort: LowerPriority`；
4. B 正在借用（6 > 4），且 B-borrower 优先级低于 A-owner；
5. 模拟移除 B-borrower → A 可以放下；
6. 驱逐 B-borrower，准入 A-owner；
7. 结果 A 使用 4，B 0，Cohort 总量 4。

MiniKueue 的 `reclaim` 场景演示了这个过程。

注意：如果 A-owner 自己也需要借用（例如 A 当前 3，再要 3，新用量 6 > nominal 4），那么仅仅 `reclaimWithinCohort` 不够，还要看 `borrowWithinCohort`。这是第 5 章"借用 vs 回收"的边界。

## 7.7 抢占后的重排队

被抢占的 Workload 通常不会立刻重新竞争，否则会形成"抢占—重排队—再抢占"的抖动。Kueue 使用：

- `status.requeueState.count`：退避次数；
- `status.requeueState.requeueAt`：下次可重排队时间；
- 指数退避；
- 最大重试次数（超过后停用）。

MiniKueue 默认用 `RequeueAt = clock + 2` 模拟；配置 `BackoffConfig` 后改用可配置指数退避（`BaseTicks`/`MaxTicks`），见 4.6.3 与 33.3.4。它还没有"最大重试次数后停用"的策略。

### 防抖动设计

一个健壮的抢占实现需要：

- 同优先级不互相抢占（或用创建时间做全序）；
- 被抢占者有冷却时间；
- 抢占决策基于一致的快照；
- 抢占后重新校验，避免"抢了还不够"；
- 最小化 victim 集合；
- 记录原因，便于审计。

MiniKueue 的 `preemptible` 对同优先级使用"更老的赢"作为 tie-break，保证不会 A 抢 B、B 又抢 A。

## 7.8 Fair Sharing 抢占

Fair Sharing 开启后，抢占不再只看优先级，还要看份额。它与 `withinClusterQueue`、`reclaimWithinCohort` 配合使用，但**不能和 `borrowWithinCohort` 混用**：

- `reclaimWithinCohort: LowerPriority` + Fair Sharing：既看优先级，也看策略；
- `reclaimWithinCohort: Any` + Fair Sharing：不再看优先级，只看策略；
- `borrowWithinCohort` 只属于 Classic Preemption，配置了 Fair Sharing 时不要用它。

Kueue 支持两种策略：

### `LessThanOrEqualToFinalShare`

```text
preemptor 准入后的 share ≤ victim 被驱逐后的 share
```

### `LessThanInitialShare`

```text
preemptor 准入后的 share < victim 被驱逐前的 share
```

通常组合使用：

```yaml
fairSharing:
  preemptionStrategies:
  - LessThanOrEqualToFinalShare
  - LessThanInitialShare
```

算法概览：

```text
FindFairPreemptionTargets(X, W)
  for each strategy:
    while W 放不下 and 还有候选人:
      选择 share 最高的 CQ Y
      for each admitted workload U in Y:
        if U 满足 strategy:
          加入 targets
  // 最小化 targets
  从后往前尝试移除 target，若 W 仍能放下则移除
```

关键性质：如果 A 抢占了 B，A 准入后的 share 严格小于 B 被驱逐前的 share，因此 B 之后不能反过来抢 A。这避免了抢占循环。

### 7.8.1 MiniKueue 的 Fair Sharing 抢占

MiniKueue 通过 `Engine.SetFairSharingPreemption([]string{...})` 启用策略。实现路径：

1. `TryAdmit` 先尝试直接准入，再尝试 Classic/Configurable 抢占；
2. 如果仍失败，`findFairSharingPreemption` 收集同一 Cohort 树内、优先级不高于 preemptor、且当前 `FairShare > 0` 的已准入 Workload；
3. 按"低优先级、较晚准入、名字"排序后逐个加入 victim 集合；
4. 每次加入后重新计算 `findAssignment`，并要求：
   - `LessThanOrEqualToFinalShare`：`preemptorNewShare <= targetNewShare`；
   - `LessThanInitialShare`：`preemptorNewShare < targetOldShare`；
5. 找到可行集合后反向最小化，和 Classic Preemption 使用同一套最小 victim 逻辑。

`fairShareValue` 是 `FairShare` 的带"假设准入 + 排除 victim"版本：额外用量按 admission 的 FlavorAssignment 记到对应 Flavor，排除集合会同时影响 usage、lendable 和共享池空闲量。Cohort 权重会沿 CQ → 根路径相乘。

运行：

```bash
go run ./cmd/minikueue -scenario fair-sharing-preemption -ticks 2
```

场景中 `team-a` 名义配额 4 CPU、实际使用 6 CPU；空闲的 `team-b` 提交 4 CPU 作业。Classic Preemption 全关，只有 Fair Sharing 策略允许 `team-b` 收回被借走的容量。

与真实 Kueue 的差距：MiniKueue 的 share 是"借用式 DRS"而不是完整加权 DRF；候选按优先级而不是按目标 CQ 的 share 最高者选取；没有 AlmostLCA、采样窗口、目标 CQ 排序和预抢占。策略语义方向正确，但不能用于生产调参。

## 7.9 可配置抢占（Configurable Preemption）

从 v0.20 起，Kueue 引入 alpha 的 `PreemptionConfig`（`kueue.x-k8s.io/v1alpha1`），由 `ConfigurablePreemptions` gate 控制，允许声明式地定义：

> 前置：alpha CRD 默认不安装；先按 3.2.1 安装 alpha CRD 并开启 gate，否则 apply 会报 `no matches for kind`。

- **触发条件**：`Always`、`InsufficientQuota`、`QuotaFeasibleAndInsufficientTopology`；
- **候选人选择器**：
  - 作用域：`WithinLocalQueue`、`WithinClusterQueue`、`WithinParentCohort`、`WithinCohortTree`、`AnyClusterQueue`；
  - 优先级比较；
  - PriorityClass 选择；
  - 数字标签；
  - label selector / clusterQueueSelector；
- **preemptor 选择器**：限制哪些 incoming Workload 可以触发规则。

示例：

```yaml
apiVersion: kueue.x-k8s.io/v1alpha1
kind: PreemptionConfig
metadata:
  name: topology-defrag
spec:
  rules:
  - name: evict-smaller-jobs
    activationPolicy:
      trigger: QuotaFeasibleAndInsufficientTopology
    candidateSelectors:
    - scope: WithinCohortTree
      priority:
        mode: Base
        comparison: LessThanOrEqual
      numericLabels:
      - key: example.com/node-count
        comparison: LessThan
        fallbackValue: 0
```

通过 ClusterQueue 注解引用：

```yaml
metadata:
  annotations:
    kueue.x-k8s.io/preemption-config-name: topology-defrag
```

风险：

- 规则如果对称，会导致 A 抢 B、B 抢 A 的级联抢占；
- `AnyClusterQueue` 作用域很强，容易误伤；
- 需要严格的非对称设计（例如只允许高优先级队列抢低优先级，或只允许大作业抢小作业）。

### 7.9.1 MiniKueue 的 Configurable Preemption-lite

MiniKueue 在 `api.PreemptionConfig` 中实现了规则的**核心语义子集**：

- `Engine.SetPreemptionConfig(*api.PreemptionConfig)` 做与 CRD webhook 同类的校验；
- ClusterQueue 通过 `spec.preemptionConfigName` 引用规则（真实 Kueue alpha 用 `kueue.x-k8s.io/preemption-config-name` 注解）；
- 支持的触发条件：`Always`、`InsufficientQuota`、`QuotaFeasibleAndInsufficientTopology`；
- 支持的作用域：`WithinLocalQueue`、`WithinClusterQueue`、`WithinParentCohort`、`WithinCohortTree`、`AnyClusterQueue`；
- 支持 `priority`（`Base`/`Boosted` + 比较符）、`numericLabels`（键、比较符、fallback/min/max）、`labelSelector`、`clusterQueueSelector`；
- 规则候选与 Classic 候选合并、去重、统一排序，再走"逐个移除 + 反向最小化"；
- `QuotaFeasibleAndInsufficientTopology` 的判定方式是：先跑一个跳过拓扑的 quota 分配，若成功而完整分配失败，就认为失败来自拓扑。

运行：

```bash
go run ./cmd/minikueue -scenario configurable-preemption -ticks 2
```

场景中 CQ 的 `spec.preemption` 全为 `Never`，唯一允许抢占的是 `PreemptionConfig` 里"优先级更低 + `workload-type=batch`"的规则。

限制：

- 没有 preemptor selector、规则优先级/互斥、`priorityClassSelector` 和 `labelKeysToCopy`；
- `Boosted` 用固定的 +1000 近似，不是真实按 numeric label 计算的 boosted priority；
- `WithinParentCohort` 用根 Cohort 近似"父 Cohort"，没有逐层 LCA；
- 没有真实 CRD/webhook/annotation 解析，字段是 MiniKueue 的内存类型。

对称规则仍可能导致 A↔B 的级联抢占，生产规则必须显式设计成非对称。

## 7.10 抢占的观测

Workload 条件：

```yaml
status:
  conditions:
  - type: Evicted
    status: "True"
    reason: Preempted
    message: "Preempted to accommodate ..."
  - type: Preempted
    status: "True"
    reason: InClusterQueue
```

指标：

- `kueue_preempted_workloads_total`
- `kueue_evicted_workloads_total`
- `kueue_evicted_workloads_once_total`
- `kueue_admission_cycle_preemption_skips`

审计：

- 谁抢了谁；
- 在哪个 CQ/Cohort；
- 原因是什么；
- 抢了几次。

## 7.11 MiniKueue 的实现

核心函数：

```text
TryAdmit
  ├── findAssignment            直接尝试 Flavor + 配额
  └── findPreemption
        ├── preemptionCandidates
        │     ├── 同 CQ：withinClusterQueue
        │     └── Cohort：reclaim / borrow
        ├── 排序
        ├── 逐个移除 victim，直到 findAssignment 成功
        └── 反向最小化 victim 集合
```

关键细节：

1. `findPreemption` 不改 Cache，只返回 victim 列表；控制器先驱逐 victim，再提交 preemption 的预留。这样失败时不会留下半成品。
2. 同优先级通过 `olderWins` 做全序，避免循环。
3. Cohort 候选人必须是"正在借用"的 CQ。
4. 只有 `reclaimWithinCohort` 允许时，才回收；如果 preemptor 自己也要借用，则要求 `borrowWithinCohort`。
5. 最小化 victim 集合。
6. TAS 预留同样参与抢占模拟：`removed` 中的 victim 会从 `Scheduler.TopologyUsage` 中排除，因此"抢走一个占满 rack 的作业"能正确腾出拓扑容量。
7. 每次驱逐都会写入 `status.schedulingStats`，按 `(reason, underlyingCause)` 计数；抢占的 cause 为空。

一个简化后的伪代码：

```go
candidates := eligibleVictims(...)
removed := map[string]bool{}
for _, victim := range candidates {
    removed[victim.Key()] = true
    if admission, ok := findAssignment(w, cq, removed); ok {
        break
    }
}
// 反向移除不必要的 victim
for i := len(selected)-1; i >= 0; i-- {
    delete(removed, selected[i].Key())
    if _, ok := findAssignment(w, cq, removed); ok {
        selected = append(selected[:i], selected[i+1:]...)
    } else {
        removed[selected[i].Key()] = true
    }
}
```

完整代码见 `mini-kueue/internal/scheduler/scheduler.go`。

## 7.12 本章小结

- 抢占解决"配额被占满，但高优先级/原主需要资源"的问题。
- Classic Preemption 的资格由"低于 nominal / borrowWithinCohort / withinClusterQueue"三者之一触发。
- 目标选择按四条启发式依次尝试，最后反向最小化 victim 集合。
- `reclaimWithinCohort`、`borrowWithinCohort`、`withinClusterQueue` 的默认值都是"不抢占"，需要显式开启。
- 回收是抢占的一种特殊场景：原主收回被借走的 nominal。
- 抢占后要退避重排队，避免抖动。
- Fair Sharing 抢占受份额策略约束，防止循环；不能与 `borrowWithinCohort` 混用。
- Configurable Preemption 提供更灵活但更危险的规则，需要非对称设计。
- MiniKueue 实现了 Classic 的简化版，并用创建时间做同优先级 tie-break。

---

[← 上一章：排队、调度与公平分享](06-scheduling-and-fairness.md) | [返回目录](../README.md) | [下一章：作业集成 →](08-workload-integrations.md)
