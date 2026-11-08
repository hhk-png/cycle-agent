# 第 4 章 · 准入生命周期

## 4.1 为什么是两阶段

一个作业"能开始"需要满足两类条件：

1. **逻辑配额足够**：ClusterQueue/Cohort 的额度能容纳它。
2. **外部条件满足**：云上真的有节点、目标集群接受它、拓扑能放下、许可证可用。

这两类条件的检查成本和失败语义不同：

- 配额检查是 Kueue 内部的、快速的、可回滚的；
- 外部检查可能很慢、会失败、需要重试、可能产生副作用（例如创建云资源）。

因此 Kueue 把准入拆成：

```text
Phase 1: Quota Reservation
  检查 CQ/Cohort/Flavor 配额
  预留资源，写 status.admission
  条件：QuotaReserved=True

Phase 2: Admission Checks
  执行 ClusterQueue 配置的所有 AdmissionCheck
  全部 Ready 后：Admitted=True
  Job 被放行
```

这个设计是 Kueue 能同时支持"简单队列"和"云扩容/多集群/拓扑"的关键。

## 4.2 完整时序

以 batch/Job 为例：

```text
用户创建 Job（带 queue-name 标签）
        │
        ▼
Job 集成控制器
  1. 创建 Workload
  2. 暂停 Job（suspend=true）
        │
        ▼
Kueue QueueManager
  3. 把 Workload 放进 LocalQueue → ClusterQueue 的待处理集合
        │
        ▼
Kueue Scheduler
  4. 读取 Cache 与 CQ 配置
  5. 选择 Flavor、检查配额/Cohort
  6. 预留配额，写 status.admission
  7. 条件 QuotaReserved=True
        │
        ├── 没有 AdmissionCheck ──────────────┐
        │                                      │
        ▼                                      │
AdmissionCheck 控制器                          │
  8. 执行检查（扩容/多集群/...）                │
  9. 写 status.admissionChecks                  │
        │                                      │
        ▼                                      │
Kueue                                          │
 10. 所有检查 Ready                             │
 11. 条件 Admitted=True ◄──────────────────────┘
        │
        ▼
Job 集成控制器
 12. 取消 Job 的 suspend
        │
        ▼
Job 控制器创建 Pod
        │
        ▼
kube-scheduler 放置 Pod
```

注意第 7 步之后，配额已经被预留。即使 Workload 还没 Admitted，其他作业也不能使用这部分配额。这是"预留"和"准入"分离的核心。

## 4.3 条件与状态

Kueue 用 Conditions 表达生命周期。常见条件：

| 条件 | 含义 |
|---|---|
| `QuotaReserved` | 逻辑配额已预留 |
| `Admitted` | 所有 AdmissionCheck 通过，作业可以运行 |
| `Evicted` | Workload 被驱逐，附带原因 |
| `Preempted` | 被抢占的细分原因 |
| `Finished` | Workload 完成 |
| `QuotaReserved=False` + `Reason=Inadmissible` | 结构性不可准入（例如 namespace 不匹配）；真实 Kueue 里这不是独立条件类型，MiniKueue 另写合成条件 `Inadmissible=True` 便于查询 |
| `PodsReady` | 所有 Pod 已就绪（启用 WaitForPodsReady 时） |

一个被抢占的 Workload 可能同时有：

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

`Evicted` 回答"发生了什么"，`Preempted` 回答"为什么/在哪里"。

### 状态机

```text
                 ┌──────────────┐
                 │   Pending    │
                 └──────┬───────┘
                        │ quota fits
                        ▼
                 ┌──────────────┐
        ┌───────►│ QuotaReserved│
        │        └──────┬───────┘
        │               │ checks Ready
        │               ▼
        │        ┌──────────────┐
        │        │   Admitted   │
        │        └──────┬───────┘
        │               │ finish
        │               ▼
        │        ┌──────────────┐
        │        │   Finished   │
        │        └──────────────┘
        │
        │ evict / preempt / reject / deactivate / stop
        └───────────────────────────────
```

`Pending` 不是 Kueue API 里的一个字段，而是"没有 QuotaReserved 且没有 Finished"的观察状态。实际判断要看 Conditions。

## 4.4 配额预留做了什么

配额预留至少包含：

1. **选择 ClusterQueue**：通过 LocalQueue 找到。
2. **选择 Flavor**：对每个资源组，按 Flavor 顺序找第一个能容纳的。
3. **计算用量**：所有 PodSet 的 `request × count`，加上自动计算的 `pods`。
4. **检查 CQ 配额**：不超过 `nominalQuota`。
5. **检查 Cohort**：不超过 Cohort 总量；借用不超过 `borrowingLimit`；出借不超过 `lendingLimit`。
6. **写 `status.admission`**：记录 CQ、PodSet 的 Flavor 分配、总用量。
7. **更新 Cache**：把这部分用量计入全局缓存。

`status.admission` 的一个简化示例：

```yaml
status:
  admission:
    clusterQueue: team-a-cq
    podSetAssignments:
    - name: worker
      flavors:
        cpu: a100
        memory: a100
        nvidia.com/gpu: a100
      resourceUsage:
        cpu: "32"
        memory: 128Gi
        nvidia.com/gpu: "8"
```

注意 `flavors` 是 `map[ResourceName]FlavorReference`（资源到 Flavor 的映射），不是 list；`resourceUsage` 是汇总后的资源量。真实 API 的结构会随版本变化，但思想一致：**记录这次预留用了哪个 Flavor、多少资源**，后续释放、抢占、重算都依赖它。

### 4.4.1 Admission Gate：比配额更早的一道门

AdmissionCheck 在 `QuotaReserved=True` 之后执行；**Admission Gate 在配额预留之前**执行。真实 Kueue 通过 Job 注解 `kueue.x-k8s.io/admission-gated-by` 表达，Workload 会继承这个注解。

```text
Job 带 admission-gated-by
        │
        ▼
Workload 创建 ── gate 未清除 ──► QuotaReserved=False（不占配额）
        │
        │ 外部控制器移除 gate
        ▼
QueueManager 放回可调度集合 ──► 正常配额流程
```

关键语义：

- gate 不清除，Workload 连配额都拿不到，所以不会出现"审批没通过却占着配额"；
- gate 只能在创建时添加，之后只能移除或缩短，不能追加；
- 多个 gate 用逗号分隔，各自独立清除；
- 事件 `AdmissionGated` / `AdmissionGateCleared` 可用于审计；
- 它和 AdmissionCheck 可以叠加，但不要让同一个外部系统同时做两件事。

MiniKueue 用 `WorkloadSpec.AdmissionGates` 模拟，`Engine.ClearAdmissionGate` 清除，场景 `admission-gate` 演示了"先阻塞、后放行"。它不会在 gate 期间占用 Cache。

## 4.5 AdmissionCheck 详解

### 4.5.1 定义

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: AdmissionCheck
metadata:
  name: capacity-check
spec:
  controllerName: example.com/capacity-controller
  parameters:
    apiGroup: example.com
    kind: CapacityCheckConfig
    name: default
```

`controllerName` 标识哪个控制器负责处理这个检查。Kueue 本身只负责：

- 在配额预留后创建/更新 Workload 的 `status.admissionChecks`；
- 等待所有检查变成 `Ready`；
- 处理 `Retry` / `Rejected`。

真正的检查逻辑由外部控制器实现。

### 4.5.1.1 Active 条件与控制器契约

`AdmissionCheck` 还有一个容易漏掉的 `status.conditions`：控制器必须把它标记为 `Active=True`。如果某个 AdmissionCheck 没有 Active，所有引用它的 ClusterQueue 会变成 `Inactive`，队列不再准入。

排查：

```bash
kubectl get admissioncheck capacity-check -o yaml
kubectl get clusterqueue <cq> -o yaml   # 看 Active 条件与 message
```

控制器的最小契约：

| 谁写 | 字段/对象 | 说明 |
|---|---|---|
| 检查控制器 | `AdmissionCheck.status.conditions[Active]` | 控制器健康、能处理该 Check 时置 True |
| 检查控制器 | `Workload.status.admissionChecks[name].state` | Pending/Ready/Retry/Rejected |
| 检查控制器 | `.message`、`.requeueAfterSeconds`、`.podSetUpdates` | 原因、退避、Pod 模板增量 |
| Kueue | `.retryCount`、`.lastTransitionTime` | 由 Kueue 维护，控制器不要手写 |
| 两者 | `status` 的字段所有权 | 用 SSA 或只改自己负责的条目，不要重写整个列表 |

`podSetUpdates` 是**增量**修改，只允许补 `labels`、`annotations`、`nodeSelector`、`tolerations`；多个检查不能写同一个 key。它和 `ProvisioningRequestConfig.spec.podSetUpdates` 不是同一个东西：前者是检查控制器对单个 Workload 的建议，后者是 ProvisioningRequest 的模板合并策略。

### 4.5.2 四种状态

| 状态 | 语义 | Kueue 的行为 |
|---|---|---|
| `Pending` | 还在处理 | 保持 QuotaReserved，等待 |
| `Ready` | 通过 | 继续等其他检查 |
| `Retry` | 暂时失败 | 释放配额，重排队，退避重试 |
| `Rejected` | 永久失败 | 驱逐并停用 Workload |

`Retry` 与 `Rejected` 的区别很重要：

- `Retry` 用于"现在没有云容量，等一会儿再试"；
- `Rejected` 用于"这个作业永远不可能通过，例如镜像非法、许可证被拒"。

`Retry` 不是简单地把状态改一下。检查控制器还应该设置：

```yaml
status:
  admissionChecks:   # v1beta2 字段名；旧文档里的 admissionCheckStates 是历史拼写
  - name: capacity-check
    state: Retry
    message: "no capacity in zone-a"
    requeueAfterSeconds: 300
    retryCount: 2
```

- `requeueAfterSeconds` 控制退避；不填时 Kueue 立即重试，容易形成忙循环。ProvisioningRequest 等内置检查会用 `retryCount` + backoff 主动填这个字段；
- `retryCount` 由 Kueue 在每次进入 Retry 时递增，可用于观测和上限判断；它属于 Workload 的检查状态，**在释放配额并重新预留后仍然保留**，不能因为 `status.admissionChecks` 被重建就归零；
- `Rejected` 会驱逐并停用 Workload，用户需要把 `spec.active` 改回 true 才能重新排队。

### 4.5.3 配置方式

v1beta2 使用统一入口 `admissionChecksStrategy.admissionChecks`：

**所有 Workload 都执行（`onFlavors` 留空）：**

```yaml
spec:
  admissionChecksStrategy:
    admissionChecks:
    - name: capacity-check
```

**按 Flavor 选择：**

```yaml
spec:
  admissionChecksStrategy:
    admissionChecks:
    - name: capacity-check
      onFlavors: ["a100-spot"]
    - name: global-check
```

`onFlavors` 为空表示所有 Flavor 都执行。v1beta2 **推荐** `admissionChecksStrategy`，但 CQ 顶层的 `spec.admissionChecks` 字符串列表仍然存在；两者互斥，只能选一个。从 v1beta1 迁移时，如果不需要 `onFlavors`，保留顶层列表也能工作，但带策略的清单应改用 `admissionChecksStrategy`。

### 4.5.4 ProvisioningRequest

ProvisioningRequest 是最常见的 AdmissionCheck 之一，用于和 Cluster Autoscaler/云容量控制器协作：

1. Workload 预留配额；
2. ProvisioningRequest 控制器创建一个 ProvisioningRequest，请求扩容；
3. 云控制器扩容成功后，检查变 Ready；
4. Workload 被 Admitted，Pod 创建，落到新节点上。

如果扩容失败：

- 临时失败 → `Retry`，释放配额，退避后重新尝试；
- 永久失败 → `Rejected`，停用 Workload。

ProvisioningRequest 还支持 PodSet 更新，例如给 Pod 加 annotation，让 Cluster Autoscaler 识别它应该使用哪个 provisioning request。

### 4.5.5 MultiKueue

MultiKueue 的 AdmissionCheck 控制器会把 Workload 复制到多个 worker 集群。哪个 worker 先准入，就在哪个集群运行；其他副本被清理。对 manager 集群来说，这只是一个 AdmissionCheck。

## 4.6 驱逐、重排队与退避

### 4.6.1 为什么会驱逐

- 被更高优先级作业抢占；
- Cohort 配额被原主回收；
- AdmissionCheck `Retry`/`Rejected`；
- `spec.active=false`；
- StopPolicy `HoldAndDrain`；
- `PodsReady` 超时（启用 WaitForPodsReady）；
- 超过 `maximumExecutionTimeSeconds`。

### 4.6.2 重排队

被抢占或临时失败的 Workload 通常会重新排队。Kueue 在 `status.requeueState` 记录：

```yaml
status:
  requeueState:
    count: 5
    requeueAt: "2026-02-11T04:51:03Z"
```

`count` 是退避重排队的次数，`requeueAt` 是下次可重排队的时间。重排队不是立即的，避免抢占风暴。

### 4.6.3 指数退避

`PodsReadyTimeout` 等场景会使用指数退避。配置里可以设置最大重试次数；超过后 Workload 被停用（`active=false`），需要用户重新激活。

MiniKueue 用 `BackoffConfig` 模拟这一层：

```go
e.Backoff = &controller.BackoffConfig{
    BaseTicks:  1,
    MaxTicks:   60,
    Multiplier: 2,
}
```

`requeueDelay(attempt) = min(MaxTicks, BaseTicks × Multiplier^attempt)`。它用于抢占/驱逐的重新排队，以及 AdmissionCheck Retry 没有显式 `requeueAfterSeconds` 的场景；显式延迟仍然优先。场景 `retry-backoff` 演示了 fix-delay 路径，`TestExponentialBackoffOverridesFixedPreemptionDelay` 验证 Backoff 覆盖固定 2 tick 延迟。

### 4.6.4 驱逐的审计信息

`status.schedulingStats.evictions` 记录按 `reason`/`underlyingCause` 分组的驱逐次数：

```yaml
status:
  schedulingStats:
    evictions:
    - reason: Preempted
      underlyingCause: ""
      count: 2
    - reason: PodsReadyTimeout
      underlyingCause: WaitForStart
      count: 1
```

这些信息也用于指标 `kueue_evicted_workloads_once_total`，帮助区分"抢占"和"超时"。

## 4.7 All-or-nothing 与 WaitForPodsReady

Kueue 的配额准入保证"逻辑上放得下"，但不保证 Pod 一定能被 `kube-scheduler` 放下去。原因包括：

- 节点资源碎片；
- 节点亲和性/污点；
- 卷/网络/GPU 设备问题；
- DRA 设备分配失败。

`WaitForPodsReady` 提供一个安全网：

- 作业被准入后，如果在 `timeout` 内没有全部 Pod Ready，Kueue 驱逐 Workload；
- 可以配置 `recoveryTimeout`，等待节点恢复；
- 驱逐后按指数退避重排队，避免反复占用配额；
- 如果配置了最大重试次数，应该按**同一原因**（PodsReadyTimeout）的连续次数计数，不能复用被抢占等其它驱逐共享的计数器。

All-or-nothing 与 ready Pods 的语义：

- 一个作业的所有 Pod 要么都准备好，要么整体被驱逐；
- 不会出现"一半 Pod 跑着、一半永远 Pending"的长期状态。

配置示例（Kueue Configuration）：

```yaml
waitForPodsReady:
  timeout: 30m
  recoveryTimeout: 5m
  blockAdmission: false
  unscheduledTimeout: 10m
```

三个 timeout 的区别：

| 字段 | 从什么时候开始算 | 解决什么问题 |
|---|---|---|
| `timeout` | Workload 被 Admitted 后 | Pod 一直没 Ready（镜像拉取失败、启动慢） |
| `unscheduledTimeout` | Admitted 后到所有 Pod 被调度 | 调度器放不下（碎片、亲和性、设备） |
| `recoveryTimeout` | 已运行 Workload 失去 Ready 后 | 节点故障后等待替代 Pod |

`blockAdmission: true` 会让 Kueue 一次只准入一个 Workload，等它的 Pod 全部 Ready 后再准入下一个。这是最严格的防死锁模式，但吞吐会明显下降；适合小规模、强 gang 的训练集群，不适合高吞吐批处理。

Kueue 的 all-or-nothing 不是单一机制，而是四层互补：

1. **配额准入**：所有 PodSet 的配额一起预留，不会只准入一部分；
2. **TAS**：在准入时检查拓扑/节点容量，避免"配额够、节点放不下"；
3. **ProvisioningRequest**：在自动扩缩集群里等物理容量就绪；
4. **waitForPodsReady**：最后的超时安全网。

把它们当成"逐层收紧"的防线，而不是四选一。第 9 章会分别展开。

## 4.8 StopPolicy

`stopPolicy` 是 ClusterQueue/LocalQueue 上的"急停开关"。

| 值 | 新预留 | 已预留未准入 | 已准入 |
|---|---|---|---|
| `None` | 允许 | 正常 | 继续运行 |
| `Hold` | 停止 | 取消预留 | 继续运行到完成 |
| `HoldAndDrain` | 停止 | 取消预留 | 驱逐 |

使用场景：

- 维护窗口；
- 配额迁移；
- 发现某个团队异常提交，先止血；
- 多集群切换。

注意：`Hold` 不是"暂停队列"，它仍然允许已准入的作业继续消耗资源。要真正清空，用 `HoldAndDrain`。

## 4.9 动态回收（Dynamic Reclaim）

有些作业的实际需求会小于初始请求，例如：

- 分布式训练先启动 8 个 worker，后来只需要 4 个；
- 批处理作业的分片数动态减少。

Job 集成可以通过 `status.reclaimablePods` 告诉 Kueue：

```yaml
status:
  reclaimablePods:
  - name: worker
    count: 4
```

Kueue 会释放这部分配额，让其他作业使用。`count` 只能增加，不能减少；一旦释放，不能"要回来"。

这是 Kueue 提高利用率的重要机制，但需要 Job 集成正确实现。

## 4.10 最大执行时间

```yaml
spec:
  maximumExecutionTimeSeconds: 86400
```

Workload 累计处于 Admitted 的时间超过阈值后，Kueue 会自动停用（`active=false`）。跨多次 Admit/Evict 周期的时间会累计；**删除并重建 Workload（新 UID）才会清零**。手动 deactivate 后再 activate 是否清零，取决于目标版本的 `accumulatedPastExecutionTimeSeconds` 语义，升级/运维脚本不要假设一定清零；MiniKueue 在 `Submit` 新 incarnation 时清零。

这适合防止"永远跑不完的作业"占住配额。三个实现细节：

- 计时是**累计**的，不是单次运行时间；一个作业被抢占/重排队多次，累计时间仍然增长。实现上要区分"当前准入周期的起点"和"历史累计时间"，不能只存一个 `AdmittedAt`。
- 停用后不会自动重排队；需要用户或上层系统把 `spec.active` 改回 true。
- 对 Job 可以通过 `kueue.x-k8s.io/max-exec-time-seconds` 标签设置，不用手写 Workload。

MiniKueue 用 `AdmittedAt` 记录当前准入周期起点、`ExecutionTicks` 累计历史周期；抢占、迁移和重排队只结束当前周期，不清零累计值。重新 `Submit` 会开启一个新的 Workload 生命周期并清零。真实 Kueue 的"重新激活后清零"也要通过一次显式的激活/重建操作完成，而不是每次重新准入自动清零。

它和 `WaitForPodsReady` 的分工：前者限制"运行太久"，后者限制"一直起不来"。

## 4.11 MiniKueue 中的映射

MiniKueue 实现了本章的大部分核心语义：

| 真实 Kueue | MiniKueue |
|---|---|
| 两阶段准入 | `QuotaReserved` → `Admitted`，支持 AdmissionCheck |
| AdmissionCheck 状态 | `Pending`/`Ready`/`Retry`/`Rejected`；Retry 支持 `RequeueAfterSeconds` 与 `RetryCount`，且 retryCount 跨重新预留保留 |
| 驱逐/重排队 | `Evicted` 条件、`RequeueAt` 延迟、`SchedulingStats` 按原因计数 |
| StopPolicy | ClusterQueue 与 LocalQueue 都支持 `Hold`/`HoldAndDrain` |
| namespaceSelector | 支持 `matchLabels` 与 `matchExpressions`（In/NotIn/Exists/DoesNotExist） |
| `active=false` | `Deactivate` |
| 最大执行时间 | 1 tick = 1 秒；`AdmittedAt` + `ExecutionTicks` 跨准入周期累计 |
| All-or-nothing/PodsReady | `WaitForPodsReady`：超时驱逐 + 退避重排队，`MarkPodsReady` 报告就绪 |
| 动态回收 | `ReportReclaimable`：释放未使用 Pod 的配额，`Count - ReclaimablePods` 参与记账 |
| Visibility | `Engine.QueueStatus` + CLI `-query`，按状态/原因分类 |
| Admission Gate | `WorkloadSpec.AdmissionGates` + `ClearAdmissionGate`，配额预留前阻塞 |
| Concurrent Admission | Flavor 迁移：先在靠后的 Flavor 准入，更优 Flavor 可行后重新预留 |
| 真实 Pod/Job | 用内存对象模拟；就绪/回收由操作员显式调用 |

MiniKueue 的协调循环在 `internal/controller/engine.go` 的 `Tick()` 中：

```text
Tick:
  clock++
  decay AFS history            # Admission Fair Sharing 半衰期
  recompute effective quotas   # CapacityProvider → effective nominal
  rebuild scheduler view
  ensure cache
  apply stop policies
  reap deactivated
  expire execution
  check pods ready             # WaitForPodsReady timeout
  migrate concurrent admissions# 更优 Flavor 可行时迁移
  admit ready reservations
  schedule
  admit ready reservations
  update gauges
```

第 13、14 章会逐段讲。

## 4.12 本章小结

- Kueue 采用"配额预留 + AdmissionCheck"两阶段准入。
- 配额预留后资源就被锁住，即使还没 Admitted。
- AdmissionCheck 的 `Retry` 和 `Rejected` 语义不同：前者释放配额重试，后者停用 Workload。
- 驱逐后通过 `requeueState` 做退避重排队。
- StopPolicy、WaitForPodsReady、动态回收和最大执行时间是运维与稳定性的重要工具。
- Admission Gate 在配额预留之前，AdmissionCheck 在预留之后；Concurrent Admission 允许运行中迁移到更优 Flavor。
- MiniKueue 覆盖了核心生命周期，并实现了 `WaitForPodsReady`、`reclaimablePods`、admission gate 和 concurrent admission-lite；真实 Pod/Job 仍由内存对象模拟。

---

[← 上一章：安装与快速上手](03-install-and-quickstart.md) | [返回目录](../README.md) | [下一章：配额、Flavor 与 Cohort →](05-quota-cohorts.md)
