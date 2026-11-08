# 第 30 章 · 从 MiniKueue 到生产控制器：工程化迁移手册

第 13～15 章讲了 MiniKueue 的设计、实现和差距。本章回答最后一个工程问题：**如果要把这套调度核心变成真正的 Kubernetes 控制器，具体要做什么、按什么顺序做、每一步如何验证。**

本章的代码是 controller-runtime/kubebuilder 项目的骨架，用于说明结构和契约；它需要在一个真实的 Go 模块中编译。MiniKueue 本身仍然是可运行、可测试的参考实现。

> 版本提示：controller-runtime、client-go、Kueue API 的字段会随版本变化。示例中的 import 路径和接口以你使用的版本为准。

## 30.1 迁移的目标与非目标

### 目标

把一个"内存模拟器"变成"能接管真实 Job 的控制器"：

```text
用户 Job/JobSet
    │
    ▼
[Job 集成控制器] ──创建──► Workload CRD
    │                         │
    │ 暂停 Job                 ▼
    │                    [QueueManager + Cache]
    │                         │
    │                    [Scheduler]
    │                         │
    │◄── Admitted ────────────┘
    │
    ▼
kube-scheduler 放置 Pod
```

### 非目标

- 不重新实现 kube-scheduler；
- 不复制 Kueue 的全部功能（先做核心闭环）；
- 不追求第一版就 HA/多集群/DRA；
- 不在没有测试的情况下直接接生产流量。

## 30.2 组件映射：从 MiniKueue 到 controller-runtime

| MiniKueue | 生产实现 | 迁移要点 |
|---|---|---|
| `internal/api` structs | CRD types + kubebuilder markers | 字段、默认值、校验、deepcopy |
| `internal/store` | informer cache + client | 索引、list/watch、冲突重试 |
| `internal/cache` | 进程内 scheduler cache | 从 informer 事件重建，快照读 |
| `internal/scheduler` | 纯决策包 | 可原样保留，替换类型/Quantity |
| `Engine.Tick` | workqueue + reconcile + 调度循环 | 事件驱动，不是固定 tick |
| `Engine.Submit` | Job 集成创建 Workload | ownerRef、幂等、暂停 |
| `Engine.Finish` | Workload/Job 状态同步 | 条件、完成判定 |
| `Engine.ApproveCheck` | 外部控制器写 status | 只写自己的字段 |
| `api.Validate*` | webhook | 默认值、校验、不可变字段 |
| `Engine.ResourceTransformations` | Configuration `resources.transformations` | 启动时加载；用 Quantity；不可变/热更新策略 |
| `Engine.Provisioning` | ProvisioningRequest 控制器 | 外部对象、云扩容、retryStrategy、PodSetUpdates |
| `Engine.PreemptionConfigs` | `PreemptionConfig` CRD + 注解 | v1alpha1、feature gate、webhook 校验 |
| `Engine.FairSharingPreemption` | Configuration `fairSharing.preemptionStrategies` | 与 AlmostLCA/目标 CQ 排序一起实现 |
| `Engine.Backoff` | Configuration/Provisioning retryStrategy | 真实时钟、抖动、最大次数 |
| `metrics.Metrics` | Prometheus registry | 标签、直方图、基数 |
| `scenario` | envtest/集成测试 | 真实对象、真实 API server |

**最重要的迁移原则**：把"决策"和"提交"继续分开。MiniKueue 的 `TryAdmit` 不修改 Cache，这个性质在生产中同样重要；controller-runtime 的并发和重试会让"边算边改"变得非常危险。

## 30.3 项目骨架

用 kubebuilder 初始化：

```bash
kubebuilder init --domain example.com --repo example.com/kueue-lite
kubebuilder create api --group kueue --version v1beta1 --kind Workload
kubebuilder create api --group kueue --version v1beta1 --kind ClusterQueue
kubebuilder create api --group kueue --version v1beta1 --kind LocalQueue
kubebuilder create api --group kueue --version v1beta1 --kind ResourceFlavor
```

推荐目录：

```text
kueue-lite/
├── api/v1beta1/              # CRD types, deepcopy, webhook
├── cmd/controller/           # manager 入口
├── internal/
│   ├── cache/                # 调度账本
│   ├── queue/                # QueueManager
│   ├── scheduler/            # 纯决策
│   ├── controller/
│   │   ├── workload/         # Workload reconciler
│   │   ├── queue/            # CQ/LQ reconcilers
│   │   └── job/              # Job 集成
│   ├── admissioncheck/       # 外部检查控制器
│   └── metrics/              # 指标
├── config/                   # CRD/RBAC/webhook manifests
└── test/                     # envtest/integration
```

不要把所有逻辑塞进一个 `main.go`。Kueue 本身的分层（`pkg/cache/queue`、`pkg/cache/scheduler`、`pkg/scheduler`、`pkg/controller`）就是可借鉴的边界。

## 30.4 CRD 设计：从 struct 到 API

MiniKueue 的 `Workload` 可以直接映射成 CRD，但要做几件事：

### 30.4.1 资源量换成 Quantity

MiniKueue 用 `int64`（millicore/MiB）换取确定性。生产应使用 `resource.Quantity`：

```go
type ResourceList map[corev1.ResourceName]resource.Quantity
```

迁移步骤：

1. 先写一组表驱动测试，覆盖 `1000m`、`1Gi`、`1.5`、`0`、负值；
2. 用 `Quantity.MilliValue()`/`Value()` 或 `Cmp` 做比较；
3. 处理单位换算和四舍五入；
4. 保留一个 `FormatQuantity` 用于日志/CLI；
5. 最后再替换类型，不要一边改类型一边改算法。

### 30.4.2 默认值与校验

用 kubebuilder marker 和 webhook：

```go
// +kubebuilder:validation:Enum=StrictFIFO;BestEffortFIFO
// +kubebuilder:default=BestEffortFIFO
QueueingStrategy QueueingStrategy `json:"queueingStrategy,omitempty"`
```

需要显式处理的语义：

- `namespaceSelector` 的 `nil` vs `{}`；
- `borrowingLimit`/`lendingLimit` 的 `nil` vs `0`；
- `queueingStrategy`/`stopPolicy` 的默认值；
- `concurrentAdmissionPolicy` 创建后不可变；
- 已准入 Workload 的 `podSets` 不可变（或只能通过受支持路径更新）；
- 资源组/Flavor 的唯一性；
- Cohort 父链无环。

### 30.4.3 条件与 status subresource

用 `status` subresource 写条件，避免和 spec 更新冲突：

```go
// +kubebuilder:subresource:status
type Workload struct {
    metav1.TypeMeta   `json:",inline"`
    metav1.ObjectMeta `json:"metadata,omitempty"`
    Spec              WorkloadSpec   `json:"spec,omitempty"`
    Status            WorkloadStatus `json:"status,omitempty"`
}
```

条件用 `metav1.Condition`，带 `ObservedGeneration`：

```go
meta.SetStatusCondition(&w.Status.Conditions, metav1.Condition{
    Type:               "QuotaReserved",
    Status:             metav1.ConditionTrue,
    Reason:             "Reserved",
    Message:            "quota reserved",
    ObservedGeneration: w.Generation,
    LastTransitionTime: metav1.Now(),
})
```

### 30.4.4 字段所有权

Kueue 与 Job 集成共同读写 Workload。生产要明确：

- Job 集成创建/更新 `spec.podSets`、`spec.queueName`、`spec.priority`；
- Kueue 写 `status.conditions`、`status.admission`、`status.admissionChecks`、`status.requeueState`；
- 集成写 `status.reclaimablePods`（或通过受支持路径）；
- 不要用整对象 `Update` 互相覆盖；用 SSA 或只更新自己的字段。

## 30.5 Manager 装配

`cmd/controller/main.go` 的主干：

```go
func main() {
    scheme := runtime.NewScheme()
    utilruntime.Must(clientgoscheme.AddToScheme(scheme))
    utilruntime.Must(kueuev1beta1.AddToScheme(scheme))

    mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
        Scheme:                 scheme,
        LeaderElection:         true,
        LeaderElectionID:       "kueue-lite.example.com",
        Metrics:                server.Options{BindAddress: ":8443"},
        HealthProbeBindAddress: ":8081",
    })
    if err != nil {
        setupLog.Error(err, "unable to start manager")
        os.Exit(1)
    }

    cache := schedulercache.New()
    queues := queue.NewManager()
    sched := scheduler.New(cache, queues, mgr.GetClient(), mgr.GetEventRecorderFor("kueue-lite"))

    if err := (&workload.Reconciler{
        Client:    mgr.GetClient(),
        Cache:     cache,
        Queues:    queues,
        Scheduler: sched,
    }).SetupWithManager(mgr); err != nil {
        setupLog.Error(err, "unable to create controller", "controller", "Workload")
        os.Exit(1)
    }

    if err := mgr.Add(sched); err != nil { // 实现 Runnable，启动调度循环
        setupLog.Error(err, "unable to add scheduler")
        os.Exit(1)
    }

    if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
        setupLog.Error(err, "problem running manager")
        os.Exit(1)
    }
}
```

关键点：

- **只有 leader 写**：leader election 保证同一时刻一个 manager 执行 reconcile/调度；
- **Scheduler 是 Runnable**：它不应该在每个 Workload reconcile 里跑，而是被事件唤醒后做一次快照调度；
- **Cache/QueueManager 是进程内状态**：从 informer 事件重建，不持久化；
- **指标/健康检查/webhook 分开端口**，便于监控和探针。

## 30.6 调度循环：从 Tick 到事件驱动

MiniKueue 的 `Tick()` 是固定顺序的协调循环。生产版应改成"事件驱动 + 去抖 + 单次快照"：

```text
Workload/Queue/Cohort/Flavor 事件
        │
        ▼
QueueManager.AddOrUpdate / Requeue
        │
        ▼
Scheduler 被唤醒（合并短时间内的多次事件）
        │
        ▼
snapshot := cache.Snapshot()
heads    := queues.Heads(ctx)
entries  := nominate(heads, snapshot)
for each entry:
    processEntry(entry, snapshot)   // 决策，不写 Cache
        │
        ▼
提交：更新 Workload status/驱逐 victim/通知集成
        │
        ▼
informer 事件回流 → Cache 更新
```

### 30.6.1 为什么不要在每个 reconcile 里跑完整调度

- 同一个事件会触发多次 reconcile；
- 每次全量扫描会放大 CPU；
- 并发 reconcile 可能基于不同快照做冲突决策；
- 调度应该是"批处理"而不是"每对象一次"。

Kueue 的做法是 QueueManager 维护 heads/backlog，Scheduler 作为独立循环消费；这比"每个 Workload 自己尝试准入"更容易保证一致性。

### 30.6.2 快照与提交的竞态

生产必须处理：

- 决策时 Workload 可能被用户停用；
- CQ 可能被改小；
- 另一个控制器可能改了 status；
- API server 可能返回 409。

提交前重新校验关键条件（active、queue、generation、admission 仍为空），失败则放弃本次决策并重新入队。MiniKueue 用单锁避免了这个问题，但生产不能依赖一把大锁。

### 30.6.3 把 MiniKueue 的 scheduler 包搬过来

`internal/scheduler` 的 `TryAdmit`/`findAssignment`/`findPreemption` 是纯决策逻辑，可以保留。需要改的只有：

- `api.Workload` → CRD 类型；
- `ResourceList` → `resource.Quantity`；
- `Cache` → 生产 cache 的 Snapshot 接口；
- `View` → 从 informer 构建的只读视图；
- 日志/指标/事件。

建议先写一个 adapter 层，把 CRD 转成 MiniKueue 的内部类型，跑通测试，再逐步去掉 adapter。这样可以在迁移期间保持决策逻辑可对拍。

## 30.7 Workload Reconciler

Workload reconciler 是生命周期的核心。推荐显式状态机：

```text
observe(w):
  if w.DeletionTimestamp != nil:
      release cache; remove finalizer
  if !w.Spec.Active:
      evict if admitted; ensure not requeued
  if w.Status.Admission == nil:
      ensure in queue
  else if conditions[QuotaReserved] == True && conditions[Admitted] != True:
      evaluate admission checks
  else if conditions[Admitted] == True:
      ensure pods ready / execution time
  if finished:
      release cache; mark Finished
```

### 30.7.1 幂等

- 用 `w.UID` 或确定性 key 去重；
- `Cache.Add` 重复 key 要返回可识别错误，而不是重复扣费；
- 驱逐/完成要可重入；
- 状态更新用 patch/SSA，避免覆盖别人的字段；
- 重复事件不能重复创建 ProvisioningRequest/副本。

### 30.7.2 Finalizer

Kueue 给 Workload、CQ、Flavor、AdmissionCheck、Topology 加 finalizer。生产要保证：

- 删除时先释放 Cache/外部资源；
- 控制器不可用时不要强制删 CRD；
- 升级/卸载顺序正确（第 3、10 章）。

### 30.7.3 状态时间戳

- `LastTransitionTime` 只在状态变化时更新；
- `ObservedGeneration` 记录看到的是哪一代 spec；
- 不要用墙钟时间做调度决策（用事件/逻辑时间）；
- 监控/测试要注意时钟偏移。

## 30.8 Job 集成

Job 集成是"Workload ↔ 用户对象"的桥。最小实现：

```go
func (r *JobReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var job batchv1.Job
    if err := r.Get(ctx, req.NamespacedName, &job); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }
    if job.Labels["kueue.x-k8s.io/queue-name"] == "" {
        return ctrl.Result{}, nil // 不托管
    }

    wl, err := r.ensureWorkload(ctx, &job) // 确定性名称 + ownerRef
    if err != nil {
        return ctrl.Result{}, err
    }

    if !isAdmitted(wl) {
        return ctrl.Result{}, r.ensureSuspended(ctx, &job, true)
    }
    if isEvicted(wl) {
        if err := r.ensureSuspended(ctx, &job, true); err != nil {
            return ctrl.Result{}, err
        }
        return ctrl.Result{}, r.cleanupPods(ctx, &job)
    }
    return ctrl.Result{}, r.ensureSuspended(ctx, &job, false)
}
```

要点：

- **确定性名称**：`<job-name>` 或 `<job-uid>`，避免重复 Workload；
- **ownerReference**：Job 删除时 Workload 被回收；
- **暂停/放行**：只让集成管理 `spec.suspend`；
- **PodSetUpdates**：把 Flavor 的 nodeSelector/tolerations/TAS assignment 应用到 Pod 模板；
- **reclaimablePods**：作业缩容时报告，不要直接改已准入的 `podSpecs`；
- **完成判定**：Job 完成/失败后写 Workload `Finished`，释放配额；
- **驱逐**：监听 `Evicted`，暂停并清理 Pod，等待重排队。

### 30.8.1 多 PodSet 与 gang

每个角色一个 PodSet，整体准入。不要先放行 head 再等 worker；否则会出现"head 跑着、worker 永远 Pending"的半启动。若框架不支持整体暂停，用 scheduling gate 或 webhook 兜底。

### 30.8.2 长期运行对象

Deployment/StatefulSet/RayCluster 没有天然"完成"。必须定义：

- 什么算 Finished？
- 缩容/滚动更新如何释放配额？
- 旧 Pod 和新 Pod 是否同时计入？
- 副本数变化如何报告？

否则 Workload 永远不 Finished，配额永远不释放。

## 30.9 AdmissionCheck 控制器

外部 AdmissionCheck 控制器的最小骨架：

```go
func (r *CheckReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var wl kueuev1beta1.Workload
    if err := r.Get(ctx, req.NamespacedName, &wl); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }
    if wl.Status.Admission == nil {
        return ctrl.Result{}, nil // 还没预留配额
    }
    for i := range wl.Status.AdmissionChecks {
        cs := &wl.Status.AdmissionChecks[i]
        if cs.Name != r.CheckName || cs.State != kueuev1beta1.CheckPending {
            continue
        }
        ready, retryAfter, err := r.evaluate(ctx, &wl)
        switch {
        case err != nil && retryAfter > 0:
            cs.State = kueuev1beta1.CheckRetry
            cs.Message = err.Error()
            cs.RequeueAfterSeconds = retryAfter
            cs.RetryCount++
        case err != nil:
            cs.State = kueuev1beta1.CheckRejected
            cs.Message = err.Error()
        case ready:
            cs.State = kueuev1beta1.CheckReady
            cs.Message = "capacity available"
        default:
            return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
        }
        cs.LastTransitionTime = metav1.Now()
        if err := r.Status().Update(ctx, &wl); err != nil {
            return ctrl.Result{}, err
        }
    }
    return ctrl.Result{}, nil
}
```

关键语义：

- **只写自己的 check**：不要覆盖其他检查或 Kueue 的字段；
- **Retry 释放配额**：Kueue 看到 Retry 会释放预留并退避；
- **Rejected 停用**：永久失败，不要反复重试；
- **requeueAfterSeconds**：避免忙循环；
- **幂等**：重复 reconcile 不能重复创建外部资源；
- **外部资源清理**：Workload 完成/删除时清理 ProvisioningRequest/副本。

### 30.9.1 一个可测试的状态机

把检查逻辑写成纯函数：

```go
type Decision struct {
    State        CheckState
    Message      string
    RequeueAfter time.Duration
}

func evaluate(ctx context.Context, wl *Workload, client ExternalClient) Decision
```

这样可以用 fake client 做表驱动测试，不需要真实云。MiniKueue 的 `ApproveCheck`/`ApproveCheckRetry` 就是这种思路的简化版。

## 30.10 Webhook

Webhook 是 API 边界上的最后一道防线。至少实现：

- **Defaulting**：`queueingStrategy`、`stopPolicy`、`namespaceSelector` 等默认值；
- **Validation**：
  - 资源组/Flavor 唯一；
  - 每个 coveredResource 在每个 Flavor 上有 nominal（哪怕 0）；
  - borrow/lend 非负；
  - Cohort 父链无环；
  - `concurrentAdmissionPolicy` 约束；
  - `podSets` 名称唯一、资源非负、`pods` 保留资源；
- **Immutability**：
  - 已准入 Workload 的 `podSets`；
  - `concurrentAdmissionPolicy`；
  - 某些字段的创建后修改。

Webhook 不能替代 controller 逻辑，但它能阻止明显非法的对象进入系统。升级时要同时验证 CRD schema 和 webhook 版本。

## 30.11 测试策略

### 30.11.1 单元测试

- scheduler/cache/queue 纯函数；
- 表驱动边界：nil/empty selector、0/null limit、负值、重复 key；
- 不变量：配额、拓扑、生命周期。

### 30.11.2 envtest

用真实 API server + etcd 跑 CRD/webhook/controller：

```go
testEnv := &envtest.Environment{
    CRDDirectoryPaths: []string{filepath.Join("..", "..", "config", "crd", "bases")},
    ErrorIfCRDPathMissing: true,
}
cfg, err := testEnv.Start()
```

覆盖：

- 创建 Job → Workload → Admitted → Pod 创建；
- 配额不足 → Pending；
- 抢占 → 驱逐 → 重排队；
- AdmissionCheck Retry/Rejected；
- 控制器重启/缓存重建；
- finalizer 清理。

### 30.11.3 集成测试

- 真实 Job/JobSet/RayJob/Kubeflow；
- 真实 kube-scheduler（kind）；
- TAS/DRA/ProvisioningRequest（需要额外环境）；
- 多副本/leader election。

### 30.11.4 故障与性能

- 停 webhook/控制器；
- API 冲突/429/watch 断流；
- informer 延迟；
- 大规模 CQ/Workload 基准；
- 内存/队列深度/指标基数。

第 27 章的分层方法可以直接复用：单元 → 不变量 → 场景 → 差分 → 故障注入 → race → 性能。

## 30.12 上线路线

不要一次性替换生产 Kueue。推荐：

```text
阶段 0：影子
  ├── 部署新控制器，只处理测试 namespace
  ├── 不接管真实 Job
  └── 对拍 MiniKueue/真实 Kueue 的决策

阶段 1：单租户试点
  ├── 一个 LocalQueue/CQ
  ├── 只接新作业
  └── 保留旧机制兜底

阶段 2：多租户
  ├── Cohort/借用/抢占
  ├── 监控/告警/runbook
  └── 配额合同

阶段 3：高级能力
  ├── TAS/DRA/MultiKueue
  ├── ProvisioningRequest
  └── 性能/HA 压测
```

每个阶段都要有退出条件和回滚方案。升级/迁移不要和配额调整同时做。

## 30.13 迁移检查清单

### API/CRD

- [ ] 字段/默认值/校验与目标 Kueue 版本对齐；
- [ ] `namespaceSelector` 的 nil/empty 语义正确；
- [ ] borrow/lend 的 null/0 语义正确；
- [ ] status subresource 和条件时间戳正确；
- [ ] 不可变字段有 webhook 保护；
- [ ] conversion/round-trip 通过（如果多版本）。

### 控制器

- [ ] 只有 leader 写；
- [ ] 调度循环是事件驱动 + 快照；
- [ ] 决策与提交分离；
- [ ] 提交前重新校验；
- [ ] Cache 从 informer 重建；
- [ ] 重复事件幂等；
- [ ] finalizer 正确清理。

### Job 集成

- [ ] 确定性 Workload 名称 + ownerRef；
- [ ] Admitted 前不创建可调度 Pod；
- [ ] 多 PodSet 整体暂停/放行；
- [ ] Evicted 后暂停/清理/重排队；
- [ ] 完成/失败同步；
- [ ] Flavor/TAS 注入正确；
- [ ] `reclaimablePods` 正确报告。

### 测试/运维

- [ ] 单元/不变量/envtest/集成测试；
- [ ] 控制器重启/leader 切换演练；
- [ ] webhook 证书/可用性监控；
- [ ] 指标/告警/runbook；
- [ ] 容量模型/配额评审；
- [ ] 回滚清单和演练。

## 30.14 本章小结

- 迁移的核心不是"重写调度算法"，而是把 MiniKueue 的纯决策核心接到 Kubernetes 的对象、事件和并发模型上。
- CRD/status/webhook/字段所有权是 API 层的主要工作；Quantity、默认值、不可变字段要单独验证。
- 调度循环要从固定 Tick 改成事件驱动 + 快照 + 提交前校验。
- Job 集成的核心不变量是"Admitted 前不创建可调度 Pod"，多 PodSet 必须整体处理。
- AdmissionCheck 控制器要只写自己的字段、区分 Retry/Rejected、幂等、清理外部资源。
- 测试要覆盖单元、不变量、envtest、集成、故障和性能；上线要分阶段、可回滚。
- MiniKueue 仍然是理解语义和验证算法的好工具；生产控制器是另一层工程，不要混为一谈。

---

[← 上一章：排队论与容量工程](29-queueing-capacity-engineering.md) | [返回目录](../README.md) | [下一章：kueuectl 与自动化运维 →](31-kueuectl-operations.md)
