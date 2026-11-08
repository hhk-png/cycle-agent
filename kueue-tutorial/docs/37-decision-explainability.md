# 第 37 章 · 调度决策的可解释性与 What-if 工程

前 36 章回答了"Kueue 怎么工作"。这一章回答一个运维现场更常见的问题：**这个作业为什么还没开始？**

条件、事件和指标能告诉你"现在是什么状态"，但通常不能告诉你"是哪一条约束挡住了它""要改什么才会好""改了以后会不会更糟"。本章把调度决策拆成可检查的证据链，并在 MiniKueue 中实现两个可运行的工具：

- `-explain`：输出一次准入尝试的完整诊断，包括每个 flavor、每个资源、每一层配额约束的精确数字；
- `-what-if`：在同一确定性场景上做配额反事实，比较"改之前/改之后"的 Workload 状态。

> 这一章不是"再讲一遍排障清单"。第 10、31 章讲的是**怎么查**；本章讲的是**怎么让调度器自己把理由说清楚**，以及如何验证这些理由没有撒谎。

## 37.1 为什么 "Pending" 不是一个答案

一个 Workload 处于 `Pending`，可能的直接原因至少有十几种：

| 层面 | 典型原因 | 现有证据 |
|---|---|---|
| 对象引用 | LocalQueue 不存在、ClusterQueue 不存在 | condition `Inadmissible` |
| 租户边界 | `namespaceSelector` 不匹配 | condition `Inadmissible` |
| 生命周期 | Workload 被 deactivate、队列 `stopPolicy` | `active=false`、CQ/LQ 状态 |
| 外部闸门 | AdmissionGate 未清除 | condition `AdmissionGated` |
| 配额 | CQ nominal 不足、Cohort 总量不足 | condition `InsufficientQuota` |
| 借用 | `borrowingLimit`、Cohort 级 limit、无可借资源 | 只有笼统的 reason |
| 拓扑 | 配额够，但 TAS domain 放不下 | 往往只看到 `InsufficientQuota` |
| 抢占 | 没有候选人、候选人不满足策略、最小 victim 集不存在 | 事件里只有"被抢占" |
| 准入检查 | AdmissionCheck Pending/Retry/Rejected | `status.admissionChecks` |
| 调度器之后 | Pod 已创建但 kube-scheduler 放不下 | Pod 事件，不在 Kueue 里 |

`status.conditions` 的 `reason` 是**粗粒度分类**，不是**不可行证书**。它能区分"配额不足"和"结构性不可准入"，但无法回答：

- 是 CPU 不够还是 GPU 不够？
- 是 CQ nominal 用完，还是 Cohort 总量用完？
- 如果换一个 flavor 呢？
- 如果借 500m 就能过，为什么借不到？
- 配额明明够，为什么 TAS 放不下？
- 抢占为什么没发生？是没人可抢，还是抢了也不够？

可解释性工程的目标，就是把这些问题的答案变成**结构化、可测试、与真实决策同源**的数据。

## 37.2 一个足够精确的决策模型

把一次准入写成谓词：

```text
Admit(w, cq, S) =
    Active(w)
  ∧ QueueExists(w)
  ∧ SelectorMatches(cq, w.namespace)
  ∧ GatesClear(w)
  ∧ StopPolicyAllows(cq, w.queue)
  ∧ ∃ A. QuotaFit(w, cq, A, S)
  ∧ TopologyFit(w, A, S)
  ∧ (DirectFit ∨ ∃ V ⊆ S. PreemptFit(w, cq, V, S))
```

其中：

- `S` 是当前调度器看到的状态（缓存账本、拓扑账本、配置快照）；
- `A` 是一个 **flavor 分配见证（witness）**：每个资源组选一个 flavor，并给出用量；
- `V` 是一个 **victim 集合**；
- `QuotaFit` 是逐资源、逐层的配额不等式；
- `TopologyFit` 是 TAS 的放置可行性。

这个模型有两个重要性质：

1. **见证可验证**。如果 `Admit` 为真，`A`（以及可选的 `V`）就是证据；任何人都能用同一份账本复算。
2. **不可行证明不唯一**。`Admit` 为假时，可能有多条约束同时失败；诊断工具要给出**第一条/最紧的一条**，而不是假装给出了唯一证明。

> 重要区分：`QuotaFit` 为真但 `TopologyFit` 为假，和 `QuotaFit` 为假，是两类完全不同的问题。前者要改拓扑/打散/作业形状，后者要改配额/借用/抢占。把两者混成 `InsufficientQuota` 是排障时间被浪费的主要原因。

### 37.2.1 单调性的错觉

一个自然的假设是："加配额一定不会让结果更差。" 在真实调度器里，这个假设不成立：

- 加配额可能改变 CQ 的准入顺序，让一个更晚的作业先拿到资源；
- 加配额可能改变 fair-share 份额，从而改变抢占目标；
- 加 CPU 但不加 GPU，可能让一个作业从"放不下"变成"能过配额检查但拓扑更碎"；
- 改变一个 Flavor 的 nominal 可能改变 Flavor 分配顺序，触发不同的借用路径。

所以 What-if 的结论必须写成"在这个确定性场景、这次 tick 序列下的反事实"，不能写成"生产上一定会更好"。本章最后一节会把这一点展开。

## 37.3 MiniKueue 的诊断架构

诊断代码分成三层，刻意不复制调度逻辑：

```text
scheduler.Explain(w, cq)
  ├── evaluateFlavor(...)        # 与 canFitFlavor 共用同一实现
  ├── findAssignmentWithoutTopology(...)
  ├── findAssignment(...)
  ├── explainTopology(...)
  └── preemptionCandidates / findPreemption / findFairSharingPreemption

controller.(*Engine).Explain(ns, name)
  ├── 解析 LocalQueue -> ClusterQueue
  ├── 调用 scheduler.Explain
  └── 叠加控制器层阻塞：inactive / gate / stopPolicy / AdmissionCheck

cmd/minikueue -explain / -what-if
  └── 只负责渲染和反事实回放，不做业务判断
```

### 37.3.1 单一事实来源

最关键的设计决策是：**诊断和准入共用 `evaluateFlavor`**。

`internal/scheduler/scheduler.go` 里的 `canFitFlavor` 现在只有一行：

```go
func (s *Scheduler) canFitFlavor(cq *api.ClusterQueue, fq api.FlavorQuota, requested api.ResourceList, removed map[string]bool) bool {
	return s.evaluateFlavor(cq, fq, requested, removed).ok
}
```

真正的配额检查在 `internal/scheduler/explain.go` 的 `evaluateFlavor` 里，返回：

```go
type flavorVerdict struct {
	ok      bool
	checks  []FitCheck
	failure string
}
```

这样做的原因很实际：如果诊断另写一套 `if`，它迟早会和准入逻辑分叉，然后给出"看起来很有道理但和实际决策无关"的解释。测试里有一条 `TestExplainMatchesAdmissionDecision` 专门守住这个不变量。

### 37.3.2 只读与稳定顺序

`Explain` 不修改 Cache、不写 Store，可以在 tick 之间随时调用。所有资源按名称排序，flavor 按 spec 顺序输出，保证：

- 同一状态多次运行输出一致；
- 测试可以做精确断言；
- diff 两个时间点的报告有意义。

### 37.3.3 诊断不是"重新调度"

`Explain` 不会触发驱逐、不会推进 tick、不会改变 `Sequence`。它只是把 `TryAdmit` 会经过的检查**再走一遍并记录**。这也是它必须与 `TryAdmit` 共用底层函数的原因：一旦它开始"模拟一个不同的调度器"，它就不再是诊断，而是另一个产品。

## 37.4 逐层诊断算法

### 37.4.1 控制器层：先排除"根本没轮到调度"

调度器只回答"能不能放下"。以下情况即使配额充足也不会被调度，必须先由 `Engine.Explain` 覆盖：

```go
switch {
case !w.Spec.Active:
    out.Reason = "Inactive"
case len(w.Spec.AdmissionGates) > 0:
    out.Reason = "AdmissionGated"
case cq.Spec.StopPolicy != "" && cq.Spec.StopPolicy != api.StopNone:
    out.Reason = "ClusterQueueStopped"
case lq.StopPolicy != "" && lq.StopPolicy != api.StopNone:
    out.Reason = "LocalQueueStopped"
case w.Status.State == api.StateAdmitted:
    out.Reason = "Admitted"
case w.Status.State == api.StateQuotaReserved:
    out.Reason = "AdmissionCheck" + check.State
}
```

顺序很重要：一个被 gate 卡住的 Workload，报告"配额不足"会把人带到完全错误的方向。控制器层阻塞优先于调度层结论。

### 37.4.2 Flavor 层：逐资源记录不等式

`evaluateFlavor` 对每个被覆盖的资源 `r` 计算：

```text
cur       = 当前 CQ 在 flavor 上的使用量
nominal   = CQ 的 nominal（可能是 DQO 的 effective quota）
newUsage  = cur + request
```

然后按顺序检查：

1. **CQ nominal**：`newUsage > nominal` 直接失败；
2. **Cohort 总量**：`cohortUsage + request > cohortNominal` 失败；
3. **CQ borrowingLimit**：`borrow = newUsage - nominal` 超过 `flavor.borrowingLimit[r]` 失败；
4. **Cohort 级 borrowingLimit**：沿 CQ 到根的每个显式 Cohort，检查"子树已有借用 + 本次增量借用"是否超过该层上限；
5. **可借资源**：`borrow > lendable` 失败。这里的 `lendable` 是**当前可借**：同 Cohort 其他 CQ 的空闲 nominal（受 `lendingLimit` 截断）加上共享池空闲。它和第 6 章 Fair Sharing 的 DRS 分母不是同一个量——后者用 nominal 总和，不随 peer 当前用量变化。

每一步都会写入一个 `FitCheck`：

```go
type FitCheck struct {
	Resource  api.ResourceName
	Requested int64
	Current   int64
	Nominal   int64
	NewUsage  int64

	CohortUsage   int64
	CohortNominal int64

	Borrow            int64
	Lendable          int64
	BorrowingLimit    int64
	HasBorrowingLimit bool
	...
	BlockedBy string
	Detail    string
}
```

`BlockedBy` 是机器可读的原因（`ClusterQueueNominal`、`CohortNominal`、`BorrowingLimit`、`CohortBorrowingLimit`、`LendableQuota`），`Detail` 是给人看的算式。两者都要有：前者用于聚合/告警，后者用于现场解释。

### 37.4.3 组合层：单个 Flavor 可行 ≠ 整体可行

一个资源组里每个 flavor 都可能"单独可行"，但多个资源组组合起来仍可能失败。`Explain` 因此额外调用：

```go
_, quotaFeasible := s.findAssignmentWithoutTopology(w, cq, removed)
_, directFeasible := s.findAssignment(w, cq, removed)
```

- `quotaFeasible=false`：配额账本层面就过不了；
- `quotaFeasible=true && directFeasible=false`：配额够，但 TAS 放置失败；
- `directFeasible=true`：存在完整见证，`Admission` 字段就是它。

这里的 `removed` 包含 Workload 自己：

```go
removed := map[string]bool{w.Key(): true}
```

否则一个已经准入的 Workload 会把自己的占用算进"新请求"，诊断出"自己挤掉了自己"的假象。

### 37.4.4 拓扑层：把"碎片"变成可读的数字

`explainTopology` 对 ClusterQueue 引用的每个 Topology，取叶子层级，列出每个 domain 的：

- `capacity`：域的可调度容量；
- `usage`：当前 TAS 账本（跨层分配后）；
- `free = capacity - usage`。

它复用 `TopologyUsage` 的跨层分发逻辑，所以 rack 级预留会在 hostname 级可见。这样"配额够但放不下"就能落到具体 domain：

```text
topology gpu-topology levels=[rack hostname]
  rack-a/h1  capacity={cpu:8,nvidia.com/gpu:2,pods:8} usage={...} free={cpu:6,pods:6}
  ...
```

注意 `free` 里不显示 0 值资源。GPU free 为空，表示这个域已经没有空闲 GPU；这比一句"拓扑不足"有用得多。

### 37.4.5 抢占层：先分类，再找人，再最小化

抢占诊断严格复刻 `TryAdmit` 的顺序：

1. **分类 trigger**：配额完全不 fit → `InsufficientQuota`；配额 fit 但拓扑不 fit → `QuotaFeasibleAndInsufficientTopology`；
2. 如果抢占被禁用，直接给出 `preemption is disabled`；
3. 否则列出候选人（`preemptionCandidates`）；
4. 尝试经典抢占 `findPreemption`，它会逐个加入 victim 并做反向最小化；
5. 再尝试 Fair Sharing 抢占；
6. 记录最终 victim 集合，或说明"有候选人但没有可行最小集"。

这一步的产物不是"抢占失败"四个字，而是：

```text
preemption: enabled=true trigger=InsufficientQuota candidates=1
  candidates: team-b/b-borrower
  victims:    team-b/b-borrower
  a minimal victim set makes the assignment feasible
```

或者：

```text
preemption: enabled=true trigger=InsufficientQuota candidates=3
  candidates: a, b, c
  candidates exist, but no minimal victim set makes the workload fit
```

后者说明"抢了也不够"或"策略不允许抢其中任何一个"，两种情况的下一步完全不同。

### 37.4.6 复杂度

单次 `Explain` 的复杂度：

- Flavor 检查：`O(G × F × R)`，G 资源组、F flavor、R 资源数；
- 组合搜索：最坏 `O(F^G)`，与 `TryAdmit` 相同；MiniKueue 的资源组通常很小；
- 拓扑：`O(domains × R)`；
- 抢占：候选人 `C`，每个候选集都要重跑 `findAssignment`，最坏 `O(C² × F^G)`；这也是为什么生产环境不要把 `Explain` 放进每个 reconcile 的热路径。

诊断是**按需**操作：CLI、排障脚本、采样日志。不要在每个 tick 对所有 Pending Workload 全量调用。

## 37.5 实战：四种典型报告

以下输出都来自仓库中的可运行场景。命令中的 `-ticks` 决定诊断时点。

### 37.5.1 CQ nominal 用完

```bash
cd mini-kueue
go run ./cmd/minikueue -scenario basic -ticks 1 -explain default/job-c
```

```text
== explain default/job-c ==
  queue:        q -> basic
  state:        Pending  active=true  requeues=0  requeueAt=0  evictions=0
  requested:    {cpu:1,memory:512Mi,pods:1}
  verdict:      InsufficientQuota - no flavor assignment fits the quota ledger
  feasibility:  direct=false quota-only=false
  resource group 0: resources=[cpu memory] requested={cpu:1,memory:512Mi}
    flavor default          NO (blocked by ClusterQueueNominal)
      cpu      current=2000 nominal=2000 request=1000 new=3000  -> ClusterQueueNominal: CQ usage 2000 + request 1000 > nominal 2000
      memory   current=1024 nominal=2048 request=512 new=1536
  topology:     none referenced by the flavors
  preemption:   enabled=false trigger=InsufficientQuota candidates=0
    preemption is disabled for this ClusterQueue
  conditions:
    - QuotaReserved=False reason=InsufficientQuota message="insufficient quota; preemption is disabled"
```

读法：

- CPU 是瓶颈；memory 还有 512Mi 余量，即使 CPU 过了，memory 也不是问题；
- `ClusterQueueNominal` 说明不是 Cohort 总量问题，也不是借用限制；
- 抢占被禁用，所以"加优先级"不会有用；要么加 quota，要么开启 `withinClusterQueue`。

### 37.5.2 Cohort 总量用完

```bash
go run ./cmd/minikueue -scenario cohort-borrow -ticks 1 -explain team-b/b-normal
```

```text
  verdict:      InsufficientQuota - no flavor assignment fits the quota ledger
  feasibility:  direct=false quota-only=false
    flavor default          NO (blocked by CohortNominal)
      cpu      current=0 nominal=4000 request=4000 new=4000  -> CohortNominal: cohort usage 6000 + request 4000 > cohort nominal 8000
```

关键点：team-b 自己的 nominal 是 4000，请求也是 4000，**CQ 层完全放得下**；挡住它的是 Cohort 的总量 8000。此时：

- 调大 team-b 的 `borrowingLimit` 没用；
- 调大 team-b 的 nominal 也没用（Cohort 总量不变）；
- 需要缩小 team-a 的借用、加 Cohort 总 nominal（如果模型允许），或等 team-a 完成。

如果只看到 `InsufficientQuota`，这些区分都做不到。

### 37.5.3 配额够但拓扑放不下

```bash
go run ./cmd/minikueue -scenario tas -ticks 1 -explain default/job-c
```

```text
  verdict:      TopologyInfeasible - quota fits, but no TAS placement satisfies the topology constraints
  feasibility:  direct=false quota-only=true
    flavor tas-flavor       YES
      cpu      current=8000 nominal=32000 request=4000 new=12000
      nvidia.com/gpu current=8 nominal=12 request=4 new=12
      pods     current=8 nominal=40 request=4 new=12
  topology gpu-topology levels=[rack hostname]
    rack-a/h1  capacity={cpu:8,nvidia.com/gpu:2,pods:8} usage={...} free={cpu:6,pods:6}
    ...
```

`quota-only=true` 是决定性的：不要再去加配额。每个 domain 的 GPU free 都是 0，作业要求 4 个 Pod 落在同一个 rack，失败是物理放置问题。下一步应该是打散/换 topology/等释放/改 slice，而不是调 nominal。

### 37.5.4 可抢占

```bash
go run ./cmd/minikueue -scenario reclaim -ticks 0 -explain team-a/a-owner
```

```text
  verdict:      Preemptable - admission requires preemption
  feasibility:  direct=false quota-only=false
    flavor default          NO (blocked by CohortNominal)
      cpu      current=2000 nominal=4000 request=2000 new=4000  -> CohortNominal: cohort usage 8000 + request 2000 > cohort nominal 8000
  preemption:   enabled=true trigger=InsufficientQuota candidates=1
    candidates: team-b/b-borrower
    victims:    team-b/b-borrower
```

这份报告同时回答了三件事：谁挡的（Cohort 总量）、谁可以被抢（b-borrower）、抢了是否够（minimal victim set 可行）。

## 37.6 What-if：把"如果"变成可复现实验

`-explain` 解释当前状态；`-what-if` 回答"改一个配额会怎样"。

### 37.6.1 语义

```bash
go run ./cmd/minikueue -scenario basic -ticks 1 -what-if "basic/default/cpu=3000"
```

```text
== what-if: basic/default/cpu=3000 (ticks=1) ==
  WORKLOAD                 BEFORE         AFTER
  default/job-a            Admitted       Admitted
  default/job-b            Admitted       Admitted
  default/job-c            Pending        Admitted
  summary: pending 1 -> 0, admitted 2 -> 3
```

实现上，它做了四件事：

1. 用同一个 `scenario.Run(name)` 构造两个**独立**的 Engine；
2. 只对第二个 Engine 应用 `clusterqueue/flavor/resource=value` 覆盖；
3. 对两个 Engine 施加相同的 tick 次数；
4. 对比每个 Workload 的状态。

覆盖只改 `FlavorQuota.Nominal`，不动 Cohort limit、抢占策略、优先级和历史用量。这是故意的：**一次只改一个变量**，否则无法归因。

### 37.6.2 为什么必须从 tick 0 重放

不能在"当前状态"上直接加配额再跑一轮，因为：

- 当前状态已经包含了旧配额下的准入顺序；
- Cache 里已经有旧的 reservation；
- AFS 历史、Sequence、拓扑账本都已经被旧决策污染。

从 tick 0 重放，才能保证 before/after 的差异只来自配额覆盖，而不是执行历史。代价是：它不能直接告诉你"生产集群现在改配额会怎样"，只能告诉你"在这个场景的完整轨迹里，改配额会怎样"。

### 37.6.3 反事实的三个陷阱

**陷阱一：非单调。**

"加配额"不保证"更多作业被准入"。例如 CQ A 和 CQ B 共享 Cohort，A 加 nominal 后可能更早借走资源，B 反而更晚。报告里的 `pending 1 -> 0` 是这个场景的结论，不是普遍规律。

**陷阱二：时间点敏感。**

`-ticks 1` 和 `-ticks 5` 的结论可能不同。短窗口看"谁先被准入"，长窗口看"谁最终完成"。做容量评审时要固定窗口，并同时报告两个窗口。

**陷阱三：只改一个数不等于只改一个语义。**

把 CPU nominal 从 2000 改成 3000，可能同时改变：

- 该 CQ 的 borrow 量（变少）；
- 同 Cohort 其他 CQ 的 lendable（变多）；
- Fair Sharing 的份额（分母变化）；
- 抢占目标（borrow 状态变化）。

所以 What-if 的输出必须带上**完整的配额覆盖表达式**和**tick 数**，否则结论不可复现。

## 37.7 把诊断接到生产 Kueue 上

真实 Kueue 没有 `Explain` API，也没有稳定的调度插件接口。能用的证据源是：

| 证据源 | 能回答 | 不能回答 |
|---|---|---|
| `status.conditions` | 粗粒度原因、时间线 | 具体资源/约束 |
| Events | 发生了什么（准入、驱逐、检查） | 为什么没发生 |
| `status.schedulingStats` | 驱逐原因计数 | 当前阻塞点 |
| 指标 | 趋势、聚合 | 单个 Workload |
| 控制器日志（V-level） | 调度尝试、flavor 选择 | 稳定结构化字段 |
| Visibility API | 队列/Workload 快照 | 决策路径 |

实践中的组合：

1. **条件 + 事件**做第一层分诊（第 10、31 章）；
2. 打开 Kueue 的调度日志，按 Workload 名过滤，拿到 flavor/借用尝试；
3. 用 `kubectl get workload -o yaml` 的 `status` 复算账本；
4. 对"配额够但起不来"的案例，转到第 28 章的分层诊断（Kueue → Job → kube-scheduler → kubelet → DRA/autoscaler）。

如果要在自己的平台里做类似 `Explain` 的能力，建议：

- **不要复制调度逻辑**。从上游 scheduler 包导出只读快照/诊断接口，或者至少在同一个函数上挂 hook；
- **给每条诊断一个稳定 code**（像 `ClusterQueueNominal`），消息可以变，code 不要变；
- **记录输入快照的版本/时间**，否则事后无法复现；
- **区分 witness 和 certificate**：能准入时给出完整 assignment；不能准入时给出第一个失败约束，不要伪造"唯一原因"。

### 37.7.1 一个结构化日志 schema

```json
{
  "ts": "2026-11-08T12:00:00Z",
  "workload": "team-a/train-42",
  "clusterQueue": "team-a",
  "tick": 17,
  "verdict": "InsufficientQuota",
  "stage": "flavor",
  "flavor": "a100",
  "resource": "nvidia.com/gpu",
  "blockedBy": "CohortNominal",
  "requested": 32,
  "current": 96,
  "nominal": 64,
  "cohortUsage": 128,
  "cohortNominal": 128,
  "preemption": {"enabled": true, "candidates": 2, "feasible": false}
}
```

有了稳定 schema，才能做聚合（"本周 60% 的 Pending 是 CohortNominal"）、告警（"某队列连续 30 分钟被同一约束阻塞"）和回归（"升级后诊断 code 分布是否变化"）。

## 37.8 诊断的正确性怎么验证

诊断工具最危险的失败模式不是崩溃，而是**自信地给出错误原因**。测试要覆盖三类性质：

1. **与决策一致**：`Explain.Admission != nil` 当且仅当 `TryAdmit.Admission != nil`（`TestExplainMatchesAdmissionDecision`）；
2. **数字可复算**：`FitCheck` 的 `current/nominal/request` 必须能重新算出 `BlockedBy`（`TestExplainReportsClusterQueueNominal`、`TestExplainBorrowCheckDetails`）；
3. **不把控制器问题误报成配额问题**：gate/stopPolicy/AdmissionCheck 必须覆盖调度器结论（`TestEngineExplainControllerBlockers`）。

还可以加两类性质测试：

- **变形**：只改 Workload 名字/namespace 标签（不影响语义），诊断 code 不变；
- **差分**：对同一场景，`Explain` 的 `DirectFeasible` 必须等于"把该 Workload 从 Store 删除后重新 Tick 是否会被准入"。

MiniKueue 当前实现了前三条；后两条留给第 19 章的扩展实验。

## 37.9 局限

- **只解释单个 Workload 的当前尝试**。它不解释"为什么另一个 Workload 先被准入"；那需要队列顺序/公平份额的诊断，属于另一个维度。
- **不做跨 tick 因果**。AFS 历史、退避、抢占后的重排队都可能让"这一 tick 的结果"与"长期结果"不同。
- **拓扑诊断是容量视角**，不模拟 kube-scheduler 的 Pod 亲和/污点/卷拓扑；真实集群仍要回到第 28 章。
- **What-if 只支持 nominal 覆盖**。优先级、权重、borrowingLimit、Cohort 结构、作业形状的变更没有 CLI 入口；可以在 `scenario` 层扩展。
- **不建议在生产热路径调用**。抢占诊断最坏是 `O(C² × F^G)`（C 候选人、F flavor、G 资源组），按需使用。
- **持久化的 Condition 仍然是粗粒度的**。MiniKueue 在"配额够但拓扑放不下"时仍写 `QuotaReserved=False/Reason=InsufficientQuota`；`-explain` 能区分 `QuotaInsufficient` 与 `TopologyInsufficient`，但这个区分只在 CLI 报告里，不体现在 Condition 上。告警/自动化如果要区分，必须消费 explain 输出，或自己扩展一个 `TopologyInfeasible` reason。

## 37.10 练习

1. 给 `-what-if` 增加 `priority=namespace/name=value` 覆盖，观察"提高优先级"是否真的能减少 Pending。
2. 增加一个 `-explain-all`，对某 CQ 的所有 Pending Workload 输出诊断，并按 `BlockedBy` 聚合。
3. 为 `TestExplainMatchesAdmissionDecision` 增加随机化：随机生成 CQ/Cohort/Workload，断言诊断与决策一致。
4. 把 `FitCheck` 序列化成 Prometheus 标签，设计一个"按阻塞原因分组的 Pending 时长"指标。
5. 构造一个"加配额反而让某个作业更晚"的场景，用 `-what-if` 复现，并解释是哪一个顺序/借用变化导致的。

## 37.11 本章小结

- `Pending` 是状态，不是原因；可解释性要把状态变成**结构化证据链**。
- 决策模型 = 谓词 + 见证 + 不可行证书；配额可行与拓扑可行必须分开报告。
- 诊断必须与准入**共用同一实现**（`evaluateFlavor`），否则迟早分叉。
- 控制器层阻塞（inactive/gate/stopPolicy/AdmissionCheck）优先于调度层结论。
- `-explain` 给出逐资源、逐 flavor、逐约束的精确数字；`-what-if` 用确定性重放做单变量反事实。
- 反事实不是预测：非单调、时间点敏感、一次覆盖可能改变多个语义。
- 生产 Kueue 没有现成的 Explain API；用 conditions/events/日志/Visibility 组合，并给诊断 code 建立稳定 schema。

---

[← 上一章：推理、弹性与混合负载](36-inference-elastic-workloads.md) | [返回目录](../README.md) | [下一章：原生 WAS 与 gang scheduling →](38-native-was-gang-scheduling.md)
