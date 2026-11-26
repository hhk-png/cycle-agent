# 第 15 章 控制器与 Webhook

## 15.1 控制循环

Volcano 的 controller manager 遵循 Kubernetes 的 reconcile 模式：

```text
watch/informer → 队列 → reconcile(key) → 读当前状态 → 计算期望 → 写回
```

控制器必须满足：

- **幂等**：同一个 key 处理多次，结果一致；
- **可重入**：失败后能从任意状态继续；
- **不阻塞**：不做长耗时同步操作；
- **有退避**：失败重试带指数退避。

## 15.2 控制器清单

| 控制器 | 输入 | 输出 |
|---|---|---|
| Job controller | Volcano Job | Pod、PodGroup、Job status |
| PodGroup controller | Pod、PodGroup | PodGroup phase/counts |
| Queue controller | Queue、PodGroup | Queue status/state |
| CronJob controller | CronJob | Job |
| JobFlow controller | JobFlow、JobTemplate | Job、JobFlow status |
| HyperNode controller | HyperNode、Node | HyperNode status/topology（v1.16 可独立部署） |
| NamespaceQueue controller | NamespaceQueue、Queue | NamespaceQueue status/authorization（v1.16，Alpha） |

### 15.2.1 Job controller

职责：

1. 为 Job 创建 PodGroup（如果不存在）；
2. 为每个 task replica 创建 Pod；
3. 监听 Pod 状态，更新 Job 的 running/succeeded/failed；
4. 根据 `policies` 执行重启/终止/完成；
5. 超过 `maxRetry` 时标记 Failed；
6. 清理/保留完成的 Job（取决于配置）。

它不做：

- 节点选择；
- 队列份额计算；
- 抢占。

### 15.2.2 PodGroup controller

职责：

- 根据 Pod 状态更新 PodGroup 的 `running/succeeded/failed`；
- 维护 phase（Pending/Inqueue/Running/Completed/Unknown）；
- 处理 `minMember` 与 Pod 数量的关系。

PodGroup 是调度器的输入，控制器要保证它及时反映 Pod 状态。

### 15.2.3 Queue controller

职责：

- 消费指向 Queue 的 `Command`（open/close）并维护 `status.state`；
- 处理 Open → Closing → Closed 的排空；
- 更新 allocated/deserved 等状态；
- 拒绝非法状态变更。

用户通过 `vcctl queue operate` 创建 Command，控制器更新 `status.state`。Queue 没有 `spec.state`，也不要手改 status。

### 15.2.4 HyperNode controller

v1.16 把 HyperNode controller 拆成可独立部署的组件：

- 模式：`controller-manager`（默认，随主控制器）、`standalone`、`disabled`；
- 独立二进制 `vc-hypernode-controller-manager`；
- 让其他调度器也能消费 HyperNode API。

这为“Volcano 只提供拓扑 API，调度交给别的系统”打开了门。

### 15.2.5 CronJob / JobFlow controller

- CronJob：按 cron 表达式创建 Job，处理并发策略与历史；
- JobFlow：按 DAG 依赖创建 Job，聚合状态；
- 它们都依赖 Job controller 的最终状态。

### 15.2.6 NamespaceQueue controller（v1.16）

职责：

- 校验 NamespaceQueue 的父链（`cluster/<name>` / 同 namespace / 默认 `cluster/default`）；
- 维护 `Ready` 与 `Authorized` condition；
- 当集群 Queue 的 `allowedNamespaces` 变化时，重新计算授权；
- 在父队列删除/关闭时，把子树标记为不可用。

排障要点：

- 作业提交失败时，先看 NamespaceQueue 的 conditions，而不是只看 Queue；
- `Authorized=False` 通常是 `allowedNamespaces` 没包含该 namespace；
- `Ready=False` 通常是父队列不存在、成环或 feature gate 未开。

## 15.3 Webhook

### 15.3.1 端点

Volcano admission 的端点（v1.16 量级）：

```text
/jobs/mutate
/jobs/validate
/podgroups/validate
/queues/mutate
/queues/validate
/hypernodes/validate
/cronjobs/validate
/namespacequeues/validate
```

Mutating 负责默认值：

- Job 的 `schedulerName`、默认 queue；
- Queue 的 state 默认 Open；
- 其他 CRD 的默认字段。

Validating 负责合法性：

- `minAvailable` 范围；
- task 名唯一；
- Queue 状态只能是 Open/Closed；
- HyperNode 的 selector 合法性；
- CronJob 的 schedule 语法。

### 15.3.2 证书

`volcano-admission-init` 生成证书，挂载到 webhook 容器。证书过期会导致：

- 创建/更新 CRD 失败；
- 错误信息通常是 `failed calling webhook` 或 `x509: certificate has expired`。

检查：

```bash
kubectl -n volcano-system get secret
kubectl -n volcano-system logs deploy/volcano-admission
```

### 15.3.3 failurePolicy

Webhook 的 `failurePolicy` 决定 admission 不可用时的行为：

- `Fail`：拒绝请求（安全，但可能阻断所有提交）；
- `Ignore`：放行（可用，但可能写入非法对象）。

生产上要明确选择，并监控 webhook 可用性。Volcano 的默认值随安装方式变化，升级时检查。

### 15.3.4 安全

- CVE-2026-44247：webhook 对无界 body 的处理导致 OOM；修复在 v1.12.4、v1.13.3、v1.14.2 及后续 patch 线。
- webhook 证书私钥要保护；
- 限制 admission 的 RBAC；
- 不要让 webhook 访问不必要的 API。

## 15.4 控制器与调度器的边界

| 场景 | 控制器 | 调度器 |
|---|---|---|
| Job 创建 Pod | ✅ | ❌ |
| Pod 绑定节点 | ❌ | ✅ |
| Pod 失败后重建 | ✅ | ❌ |
| PodGroup phase | ✅ | 读/写 Inqueue |
| Queue allocated | 读 | 计算/写回 |
| 抢占驱逐 | ❌ | ✅（通过删除/标记） |
| 资源预留 | 部分 | reservation 插件 |

一个常见误解：以为“调度器会重启失败的 Pod”。不会。调度器只负责放置；重启是 Job controller 根据策略做的。

## 15.5 故障排查

### 15.5.1 Job 不创建 Pod

- Job controller 是否运行；
- webhook 是否拒绝；
- Job 是否卡在 finalizer；
- 队列是否存在。

```bash
kubectl -n volcano-system logs deploy/volcano-controllers | grep <job>
kubectl get vcjob <job> -o yaml | grep -A5 status
```

### 15.5.2 PodGroup 不更新

- PodGroup controller 是否运行；
- Pod 是否有正确的 owner/label；
- 是否有多个控制器争抢同一 PodGroup。

### 15.5.3 Queue 卡在 Closing

- 仍有 PodGroup 未完成；
- 终止/删除残留作业；
- 检查 Queue controller 日志。

### 15.5.4 Webhook 拒绝所有请求

- 证书过期；
- service/endpoint 不通；
- failurePolicy=Fail 且 admission 副本为 0。

## 15.6 高可用

- controller manager 通常多副本 + leader election；
- scheduler 也可以多副本，但同一时刻只有一个 leader；
- admission 多副本 + Service；
- 升级时先升 CRD，再升控制器，最后升调度器（或按 release notes 顺序）。

HA 的细节在第 19 章展开。

## 15.7 MiniVolcano 的控制器

MiniVolcano 的 `controller.Manager` 模拟了：

- Pod 物化；
- Allocated → Running → Succeeded/Failed 的时间推进；
- `PodFailed/PodEvicted/TaskCompleted` 策略；
- RestartTask/RestartJob/Abort/Terminate/Complete；
- Job/PodGroup/Queue 状态回写；
- CronJob/JobFlow 的创建；
- 故障注入（按 tick）。

与真实控制器的差异：

- 没有 informer/工作队列/leader election；
- 没有 finalizer/owner reference；
- 没有 Webhook；
- 没有 API Server 的乐观并发；
- 时间是离散 tick，不是真实时钟。

这些差异让 Mini 更适合讲语义，而不是讲运维。

## 15.8 写一个最小控制器：从 Reconcile 到事件

前面讲的是“控制器应该做什么”。这一节给一个可落地的最小骨架，帮助你把第 15.2 节的职责表变成代码。代码是示意，API 签名以你使用的 controller-runtime/Volcano 版本为准。

### 15.8.1 Reconcile 骨架

一个 Volcano Job 控制器的最小循环：

```go
func (r *JobReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var job batchv1alpha1.Job
    if err := r.Get(ctx, req.NamespacedName, &job); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }

    // 1) 删除路径：先清子资源，再摘 finalizer
    if job.DeletionTimestamp != nil {
        if controllerutil.ContainsFinalizer(&job, jobFinalizer) {
            if err := r.deleteOwned(ctx, &job); err != nil {
                return ctrl.Result{}, err
            }
            controllerutil.RemoveFinalizer(&job, jobFinalizer)
            if err := r.Update(ctx, &job); err != nil {
                return ctrl.Result{}, err
            }
        }
        return ctrl.Result{}, nil
    }

    // 2) 注册 finalizer（只写一次）
    if !controllerutil.ContainsFinalizer(&job, jobFinalizer) {
        controllerutil.AddFinalizer(&job, jobFinalizer)
        if err := r.Update(ctx, &job); err != nil {
            return ctrl.Result{}, err
        }
    }

    // 3) 确保 PodGroup
    pg, err := r.ensurePodGroup(ctx, &job)
    if err != nil {
        return ctrl.Result{}, err
    }

    // 4) 确保 Pod（ownerReference 指向 Job）
    if err := r.ensurePods(ctx, &job, pg); err != nil {
        return ctrl.Result{}, err
    }

    // 5) 根据 Pod 状态推进 Job 状态与策略
    if err := r.syncStatus(ctx, &job); err != nil {
        return ctrl.Result{}, err
    }

    // 6) 终态清理或 requeue
    if job.Status.Phase.Terminal() {
        return ctrl.Result{}, nil
    }
    return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
}
```

六个步骤对应六个必须回答的问题：

| 步骤 | 问题 | 常见错误 |
|---|---|---|
| 删除路径 | 子资源谁清理？ | 只删 Job，Pod/PodGroup 泄漏 |
| finalizer | 删除过程中如何防止重建？ | 先摘 finalizer 再清理 |
| PodGroup | 调度器看到的对象从哪来？ | 每次 Reconcile 重复创建 |
| Pod | 谁拥有 Pod？ | 没有 ownerReference，Job 删了 Pod 还在 |
| 状态 | 状态从哪算？ | 用 Job 自己的 status 递归推导 |
| requeue | 什么时候再来看？ | 无条件 `Requeue: true` 打爆 API Server |

### 15.8.2 幂等、finalizer 与事件

控制器必须假设“同一个 key 会被处理很多次”。三个具体做法：

1. **Create 前先 Get**：`ensurePodGroup`/`ensurePods` 都先查存在性，已存在就跳过。
2. **用 ownerReference 而不是手工记账**：Pod 的 `metadata.ownerReferences` 指向 Job，Kubernetes GC 会处理级联删除；finalizer 只处理需要“优雅清理”的部分（如释放外部资源）。
3. **事件只发一次**：状态迁移时用 `r.Recorder.Eventf(&job, corev1.EventTypeNormal, "Inqueue", ...)`；不要在每次 Reconcile 都发同样的 Normal 事件，否则事件表会被刷屏。

一个幂等性自检：

```text
连续调用 Reconcile 两次，第二次不应该产生任何写操作。
用 fake client 的 Actions() 断言：第二次只有 Get/List，没有 Create/Update。
```

### 15.8.3 Webhook 处理函数

Volcano 的 admission 是标准的 mutating/validating webhook。一个最小校验器：

```go
func (v *JobValidator) ValidateCreate(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
    job, ok := obj.(*batchv1alpha1.Job)
    if !ok {
        return nil, fmt.Errorf("expected Job, got %T", obj)
    }
    if job.Spec.SchedulerName != "volcano" {
        return nil, field.Invalid(field.NewPath("spec", "schedulerName"),
            job.Spec.SchedulerName, "must be volcano")
    }
    total := int32(0)
    for _, t := range job.Spec.Tasks {
        total += t.Replicas
    }
    if job.Spec.MinAvailable > total {
        return nil, field.Invalid(field.NewPath("spec", "minAvailable"),
            job.Spec.MinAvailable, fmt.Sprintf("must be <= total replicas (%d)", total))
    }
    return nil, nil
}
```

默认值注入（mutating）要遵守两条规则：

- **只填未设置的字段**：用户显式写的 `queue: ""` 与省略 `queue` 在 Go 结构里可能都是空串，需要用指针或 annotation 区分。
- **默认值必须可解释**：`schedulerName=volcano`、`queue=default`、`minAvailable=Σ task 有效 minima` 是 Volcano 的三个关键默认值。关掉 webhook 后这些不会自动补，作业会表现为“不调度”。

### 15.8.4 用 envtest 验证

```go
func TestJobReconcileCreatesPodGroup(t *testing.T) {
    // envtest 启动真实 API Server + etcd
    // 创建 Job，调用 Reconcile，断言 PodGroup 存在且 ownerReference 正确
    // 再调用一次 Reconcile，断言没有重复创建
}
```

测试至少覆盖：

1. 首次 Reconcile 创建 PodGroup + Pod；
2. 第二次 Reconcile 幂等；
3. 删除 Job 时子资源被清理；
4. Pod 失败后按 policy 重建；
5. webhook 拒绝非法 `minAvailable`。

MiniVolcano 的 `Manager.Sync` 是这些测试的“无 API Server 版本”：它用显式调用代替 informer，用内存 cache 代替 API Server，用 tick 代替时钟。读第 22.6 节时，把 `Sync` 想成“手工触发的 Reconcile”即可。

### 15.8.5 上线检查

- [ ] 控制器有 leader election，且副本数 ≥ 2。
- [ ] 所有 Create 路径都幂等。
- [ ] finalizer 有对应的清理逻辑，不会永久卡住删除。
- [ ] ownerReference 正确，GC 能级联删除。
- [ ] webhook 有证书轮换与 failurePolicy 说明。
- [ ] 事件有去重/限流。
- [ ] Reconcile 有超时与退避，不阻塞工作队列。
- [ ] 指标覆盖 reconcile 次数、错误、耗时。

## 15.9 并发、finalizer 与事件风暴

控制器在单测里通常是“创建一个对象、调用一次 Reconcile”。生产里是“多个 informer 同时推送、工作队列并发消费、对象随时被用户改”。这一节列出三个最容易踩的坑。

### 15.9.1 并发与陈旧缓存

控制器读的是 informer 本地缓存，不是 API Server 的实时状态。两个后果：

1. **读到旧对象**：你刚更新了 Job，下一次 Reconcile 可能仍看到旧版本；不要用“读到的 status”做自增计数，尽量用期望状态重新计算。
2. **写冲突**：两个 worker 同时更新同一个对象，后写的会得到 `Conflict`；要重新 Get、重算、重试，而不是盲目覆盖。

推荐模式：

```text
Get（缓存）
  → 计算期望状态
  → 有变化才 Update/Patch
  → Conflict 时重新入队（rate limited）
```

不要用 `resourceVersion` 手工比较；让 API Server 的乐观并发做冲突检测。

### 15.9.2 finalizer 的顺序

finalizer 用来在对象删除前清理外部资源，但顺序错了会让对象永久卡在 `Terminating`：

| 错误 | 后果 | 正确做法 |
|---|---|---|
| 先摘 finalizer，再清理子资源 | 对象已删，子资源泄漏 | 先清理，确认成功后再摘 |
| 清理失败仍摘 finalizer | 外部资源泄漏 | 返回 error，重新入队 |
| 在 finalizer 里做无限重试 | 对象永远删不掉 | 设置超时/告警，允许人工介入 |
| 每个 Reconcile 都 AddFinalizer | 重复写、事件风暴 | 只在不存在时添加 |

一个删除路径的骨架：

```go
if job.DeletionTimestamp != nil {
    if controllerutil.ContainsFinalizer(&job, finalizer) {
        if err := r.cleanupOwned(ctx, &job); err != nil {
            return ctrl.Result{}, err // 保留 finalizer，等待重试
        }
        controllerutil.RemoveFinalizer(&job, finalizer)
        if err := r.Update(ctx, &job); err != nil {
            return ctrl.Result{}, err
        }
    }
    return ctrl.Result{}, nil
}
```

### 15.9.3 事件风暴

Kubernetes Event 是有限资源。控制器每次 Reconcile 都发一条 Normal 事件，会：

- 刷满 `kubectl get events`，真正的问题被淹没；
- 增加 API Server 写压力；
- 触发基于事件的告警误报。

三个做法：

1. **只在状态迁移时发事件**：比较 old/new phase，变化才发。
2. **用 EventRecorder 的聚合**：相同 reason/message 由 client-go 聚合计数。
3. **对高频路径用日志/指标，不用事件**：例如每次 allocate 的细节写日志，不要写 Event。

### 15.9.4 状态更新的粒度

| 写法 | 风险 | 建议 |
|---|---|---|
| 整个对象 `Update` | 覆盖别人刚写的字段 | 用 `Patch` 或 status subresource |
| 每次 Reconcile 写 status | 写放大、冲突 | 只有变化才写 |
| 在 status 里放派生计数 | 与 Pod 实际状态不一致 | 以 Pod 为准，status 只做缓存 |
| 用 annotation 存内部状态 | 用户可改、无法校验 | 用 status 或独立 CRD |

### 15.9.5 并发测试

单测覆盖“顺序执行”，并发问题要靠：

- `go test -race`（如果 CGO 可用）；
- envtest 中并发创建/删除对象；
- 注入 `Conflict` 错误，验证重试路径；
- 用 fake client 的 `Actions()` 断言没有重复写。

一个最小检查：

```text
连续 Reconcile 两次
  → 第二次只有 Get/List，没有 Create/Update
删除 Job
  → 子资源被清理，finalizer 被摘除，对象最终消失
```

## 15.10 本章小结

- 控制器负责“把状态推向期望”，调度器负责“放哪”。
- Job/PodGroup/Queue/HyperNode/Cron/JobFlow 各有控制器。
- Webhook 负责默认值与合法性，证书和 failurePolicy 是常见故障点。
- 控制器必须幂等、可重入、有退避。
- MiniVolcano 用 Manager 复刻了核心状态机，但没有 K8s 控制面的复杂性。

下一章讲可观测性与运维：如何知道调度器“为什么这样决策”。
