# 第 8 章 · 作业集成

## 8.1 为什么需要集成层

Kueue 的准入对象是 `Workload`，但用户提交的是 `Job`、`JobSet`、`RayJob`、`MPIJob`、`Pod` 等。中间需要一个集成层：

```text
用户对象（Job/RayJob/...）
        │
        │ 1. 创建 Workload
        │ 2. 暂停用户对象
        ▼
     Workload ──► Kueue 准入
        │
        │ 3. 监听 Admitted
        ▼
用户对象 ──► 取消暂停 ──► 创建 Pod
```

集成层要解决：

- 如何把用户对象的 Pod 模板转换成 Workload 的 PodSet；
- 如何在准入前阻止 Pod 创建；
- 如何在准入后放行；
- 如何把 Workload 的驱逐同步回用户对象；
- 如何报告完成、重试、失败；
- 如何处理框架特有的语义（gang、角色、副本数）。

## 8.2 最基础的集成：batch/v1 Job

Job 有原生 `spec.suspend` 字段，集成最简单：

1. 读取 Job 的 `spec.template`；
2. 创建 Workload，`podSets` 通常只有一个，`count = parallelism`；
3. 设置 Job `spec.suspend=true`；
4. 等 Workload `Admitted=True`；
5. 设置 `spec.suspend=false`；
6. Job 控制器创建 Pod。

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: train
  namespace: team-a
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  parallelism: 8
  completions: 8
  suspend: true          # 通常由 Kueue 集成设置/管理
  template:
    spec:
      containers:
      - name: worker
        image: example/trainer
        resources:
          requests:
            cpu: "4"
            nvidia.com/gpu: "1"
```

用户通常不需要手动写 `suspend: true`；集成会在创建 Workload 后处理。不要和 Kueue 争夺这个字段。

## 8.3 多 PodSet：JobSet、MPIJob、RayJob

一个作业可能有多种角色：

```text
RayJob:
  head: 1 pod, 2 CPU
  worker: 4 pods, 1 GPU each

MPIJob:
  launcher: 1 pod
  worker: 8 pods

JobSet:
  replicatedJobs:
  - name: workers
    replicas: 4
    template: ...
  - name: coordinator
    replicas: 1
    template: ...
```

这些框架的集成会把每个角色映射成一个 PodSet。以 `examples/kueue/jobset.yaml` 为例：

```yaml
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: jobset-train
  labels:
    kueue.x-k8s.io/queue-name: user-queue
spec:
  replicatedJobs:
  - name: driver
    replicas: 1
    template:
      spec:
        parallelism: 1
        completions: 1
        template:
          spec:
            containers:
            - name: driver
              resources: {requests: {cpu: "2", memory: 2Gi}}
  - name: worker
    replicas: 4
    template:
      spec:
        parallelism: 1
        completions: 1
        template:
          spec:
            containers:
            - name: worker
              resources: {requests: {cpu: "1", memory: 1Gi, nvidia.com/gpu: "1"}}
```

集成层生成的 Workload 会近似长这样（省略模板细节）：

```yaml
spec:
  queueName: user-queue
  podSets:
  - name: driver
    count: 1
    template: {...}
  - name: worker
    count: 4
    template: {...}
---
# 准入后：
status:
  admission:
    podSetAssignments:
    - name: driver
      count: 1
      resourceUsage: {cpu: "2", memory: 2Gi, pods: "1"}
    - name: worker
      count: 4
      resourceUsage: {cpu: "4", memory: 4Gi, nvidia.com/gpu: "4", pods: "4"}
```

字段名以目标版本为准，但映射关系是稳定的：`replicatedJobs[].replicas` → PodSet `count`，每个 role 的 Pod 模板 → PodSet 模板。Kueue 汇总所有 PodSet 的资源，作为一个整体准入。这保证的是**配额层面的 all-or-nothing**：要么整个 Workload 拿到配额，要么都不拿；它**不保证物理 gang 放置**（所有 Pod 同时被 kube-scheduler 接受）。物理 gang 需要额外机制，见第 28、38 章。

## 8.4 支持的作业类型

Kueue 的集成覆盖了主流批处理/AI 框架，常见包括：

| 类型 | 说明 |
|---|---|
| `batch/v1.Job` | 最基础的批处理 |
| `JobSet` | 多 Job 编排，适合分布式训练 |
| Kubeflow Training Operator | TFJob、PyTorchJob、MPIJob、XGBoostJob、JAXJob 等 |
| KubeRay | RayJob、RayCluster、RayService |
| AppWrapper | 复杂应用封装 |
| Deployment | 长期运行的服务/推理；实际按 Pod 集成逐 Pod 管理 |
| StatefulSet | 有状态工作负载 |
| LeaderWorkerSet | 多节点推理/训练 |
| Pod | 裸 Pod |
| TrainJob | Kubeflow Trainer 的新 API |
| 自定义外部框架 | 通过 external framework 接入 |

具体支持矩阵以目标版本的文档为准。不同框架的集成成熟度不同，升级前要确认。

### 8.4.1 集成契约

不管框架是什么，一个合格的 Kueue 集成要满足这份契约：

| 责任 | 要求 |
|---|---|
| 发现 | 识别带 `kueue.x-k8s.io/queue-name` 标签（或 `manageJobsWithoutQueueName` 下的）对象 |
| 转换 | 把 Pod 模板按角色映射成 PodSet，资源请求按第 2 章规则汇总 |
| 暂停 | 在 Workload 未 Admitted 前，确保不创建 Pod |
| 放行 | Admitted 后取消暂停/移除 scheduling gate |
| 驱逐 | Evicted 后暂停并清理 Pod，等待重排队 |
| 完成 | Job 完成/失败后把 Workload 标为 Finished |
| 账本 | 资源变化通过 `reclaimablePods` 或受支持的更新路径报告 |
| 注入 | 把 Flavor 的 nodeSelector/tolerations 应用到真实 Pod |
| 幂等 | 重复 reconcile 不产生重复 Workload/Pod |

任何一条缺失，都会导致"Kueue 的账本"和"实际集群"不一致。

### 8.4.2 几类框架的差异

| 框架 | 暂停机制 | PodSet 映射 | 常见坑 |
|---|---|---|---|
| batch/Job | `spec.suspend` | 一个 PodSet，count=parallelism | 用户手改 suspend 与集成冲突 |
| JobSet | 顶层 `spec.suspend` | 每个 replicatedJob 一个 PodSet | 部分暂停导致半启动 |
| Kubeflow Training | 框架的 suspend/副本控制 | 每个 role 一个 PodSet | role 名称/副本数变化 |
| KubeRay | RayCluster/RayJob 的 suspend | head + worker groups | head 与 worker 资源差异大 |
| LeaderWorkerSet | 副本/组控制 | leader + worker | group 语义与 gang |
| Pod | scheduling gate | 单 PodSet | gate 移除时机 |
| Deployment | Pod 模板注入队列标签 + scheduling gate | 逐 Pod，不是一个 Workload | 扩缩容/滚动更新期间的新旧 Pod 配额 |
| StatefulSet | Plain Pod Group（v0.20 不支持扩缩） | 一个 PodSet/组 | 不要用 `replicas=0` 暂停 |

Deployment/StatefulSet 这类长期运行对象没有天然的"完成"概念，Kueue 的配额释放依赖集成判定副本缩容/删除。特别是 Deployment：Kueue 不会把它当作一个整体 Workload，而是逐 Pod 管理。把它们纳入 Kueue 前，先想清楚"什么算 Finished"，以及扩缩容/滚动更新期间新旧 Pod 如何计费。详见[第 36 章](36-inference-elastic-workloads.md)。

## 8.5 队列名标签与纳管边界

Kueue 默认只接管"显式声明了队列"的对象。`kueue.x-k8s.io/queue-name` 既是路由信息，也是纳管开关：

```yaml
metadata:
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
```

- 标签值必须是同 namespace 的 LocalQueue 名称。
- 没有这个标签的 Job 默认完全不受 Kueue 管理，会直接交给 Job 控制器和 `kube-scheduler`。
- 标签位置随对象类型不同，打错位置是最常见的"看起来接入了其实没接入"。

| 对象 | 位置 | 说明 |
|---|---|---|
| Job / JobSet / RayJob / Pod | 外层 `metadata.labels` | 集成读取对象标签 |
| Deployment | **Pod 模板** `spec.template.metadata.labels` | 逐 Pod 管理，不是外层 Deployment |
| StatefulSet | Pod 模板/集成约定 | 以目标版本 Plain Pod Group 文档为准 |
| 自定义 Job | 框架对象或 Pod 模板 | 取决于你的 GenericJob 实现读哪里 |

"没有标签就不管"适合渐进接入，但它不是治理策略：用户忘写标签就能绕过配额。平台通常需要在"默认给一个队列""直接拒绝""按 namespace 纳管"之间做选择。Kueue 提供五种互补机制，它们的执行时机不同，混用前要先画清楚顺序。

### 8.5.1 五种纳管机制

#### 机制一：显式 queue-name（基线）

最可控的方式：要求每个作业显式声明 LocalQueue。Kueue 只校验标签格式和引用关系；标签值指向不存在的 LocalQueue 时，Workload 会进入 `Inadmissible`，不会静默降级。

#### 机制二：默认 LocalQueue（LocalQueueDefaulting）

在 namespace 里创建一个**名字必须叫 `default`** 的 LocalQueue：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: team-a
  name: default
spec:
  clusterQueue: team-a-cq
```

Kueue 的 mutating webhook 会给同 namespace 内、**还没有** queue-name 标签的新 Job 自动加上 `kueue.x-k8s.io/queue-name: default`。

要点：

- `LocalQueueDefaulting` 自 v0.12 beta、v0.17 stable（默认开启）；版本行为以 release notes 为准。
- 只对"新建"对象生效；已经存在的 Job 不会被补标签。
- 不会替你创建 LocalQueue；`default` 必须指向一个存在的 ClusterQueue。
- 它解决的是"忘了写标签"，不是"强制所有作业必须走 Kueue"。
- 因为标签在 mutating 阶段就补上了，后续 `manageJobsWithoutQueueName` 看到的已经是"有标签"的 Job。

#### 机制三：默认 WorkloadPriorityClass（WorkloadPriorityClassDefaulting）

创建一个名字必须叫 `default` 的 WorkloadPriorityClass：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata:
  name: default
value: 100
description: "Default queueing priority"
```

没有 `kueue.x-k8s.io/priority-class` 标签的被管理作业会自动获得 `kueue.x-k8s.io/priority-class: default`。`WorkloadPriorityClassDefaulting` 在 v0.20 为 beta、默认开启。

注意：**如果集群里本来就有 `default` 这个 WorkloadPriorityClass，所有未显式指定优先级的作业都会继承它**，这是行为变更。升级前先审计集群里是否已有同名对象；需要保留旧行为时，给关键作业显式设置优先级标签。

#### 机制四：manageJobsWithoutQueueName + managedJobsNamespaceSelector

```yaml
apiVersion: config.kueue.x-k8s.io/v1beta2
kind: Configuration
manageJobsWithoutQueueName: true
managedJobsNamespaceSelector:
  matchLabels:
    managed-by-kueue: "true"
```

语义：

- `manageJobsWithoutQueueName: true` 让没有 queue-name 的对象也被 Kueue 管理（创建 Workload、暂停 Job）。但**它不会自动选队列**；没有 LocalQueue 的 Workload 仍然无法准入。把它当成"强制纳管"，不要当成"默认队列"。
- `managedJobsNamespaceSelector` 是更高层的闸门：只有匹配的 namespace 才会被 reconcile。自 v0.19 起该 selector **总是生效**（对应 gate 已 GA 并锁定），不再只在 `manageJobsWithoutQueueName: true` 时生效。
- 因此启用 selector 后，**所有** Kueue 管理的 namespace（包括有 queue-name 的）都必须打上匹配标签；否则作业会"静默不受管理"。
- 生产上建议显式排除 `kube-system`、`kueue-system` 等系统 namespace，并给 namespace 标签加准入策略，防止租户自己改标签绕过纳管。

#### 机制五：ValidatingAdmissionPolicy（VAP）

如果目标是"在 API 层直接拒绝未声明队列的作业"，用 CEL VAP 比 `manageJobsWithoutQueueName` 更直接。Kubernetes 1.30+ 提供 `admissionregistration.k8s.io/v1`。

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: require-kueue-queue
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
    - apiGroups: ["batch"]
      apiVersions: ["v1"]
      operations: ["CREATE", "UPDATE"]
      resources: ["jobs"]
    - apiGroups: ["jobset.x-k8s.io"]
      apiVersions: ["v1alpha2"]
      operations: ["CREATE", "UPDATE"]
      resources: ["jobsets"]
  matchConditions:
  - name: exclude-jobset-owned
    expression: "!(has(object.metadata.ownerReferences) && object.metadata.ownerReferences.exists(o, o.apiVersion=='jobset.x-k8s.io/v1alpha2' && o.kind=='JobSet' && o.controller))"
  validations:
  - expression: "has(object.metadata.labels) && 'kueue.x-k8s.io/queue-name' in object.metadata.labels && object.metadata.labels['kueue.x-k8s.io/queue-name'] != ''"
    message: "必须设置 kueue.x-k8s.io/queue-name 标签"
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: require-kueue-queue
spec:
  policyName: require-kueue-queue
  validationActions: ["Deny"]
  matchResources:
    namespaceSelector:
      matchLabels:
        kueue-managed: "true"
```

要点：

- VAP 在 mutating webhook 之后执行，所以"默认 LocalQueue 先补标签、VAP 再校验"可以组合；如果不想让默认队列兜底，就不要创建 `default` LQ。
- JobSet 会创建子 Job；子 Job 没有 queue-name 标签是正常的，必须用 `matchConditions` 排除 owner 为 JobSet 的 Job，否则 JobSet 永远建不起来。
- 把 VAP 绑定到 namespace 标签，而不是全局 Deny；先在小范围用 `Warn`/`Audit` 观察，再切 `Deny`。
- `apiVersions`/`resources` 要按你的 JobSet 版本调整；不要照抄旧版本。
- VAP 只校验"有没有标签"，不校验 LocalQueue 是否存在；后者由 Kueue 的 webhook/控制器报 `Inadmissible`。

### 8.5.2 五种机制怎么选

| 目标 | 推荐组合 | 说明 |
|---|---|---|
| 渐进接入，允许未纳管作业直接运行 | 显式标签 / 默认 LocalQueue | 默认 LQ 降低用户心智负担 |
| 强制所有作业必须走 Kueue | VAP(Deny) + 显式标签 | 拒绝比"静默不纳管"更安全 |
| 想给未声明队列的作业一个兜底队列 | 默认 LocalQueue | 只对新对象生效 |
| 想统一默认排队优先级 | 默认 WorkloadPriorityClass | 先审计是否已有同名对象 |
| 按 namespace 灰度纳管 | `managedJobsNamespaceSelector` | 标签治理是前提 |
| 想接管未声明队列的作业，但暂时不拒绝 | `manageJobsWithoutQueueName` | 没有 LQ 仍然无法准入 |

推荐顺序：先用 VAP 的 `Warn`/`Audit` 观察一段时间，再创建默认 LocalQueue 或要求显式标签，最后切 `Deny`。不要把 `manageJobsWithoutQueueName: true` 当成"默认队列"使用。

### 8.5.3 常见失败模式

| 现象 | 根因 | 处理 |
|---|---|---|
| 新 Job 没有 Workload | namespace 未打 `managed-by-kueue` 标签，或被 selector 排除 | 检查 namespace 标签和 `managedJobsNamespaceSelector` |
| 默认 LocalQueue 不生效 | Job 在 LQ 创建之前已存在；或 LQ 不叫 `default` | 重新创建 Job；确认名字/namespace |
| Job 被 VAP 拒绝 | 没标签；JobSet 子 Job 未排除 | 加标签；补 `exclude-jobset-owned` |
| 有标签但仍 Pending | LQ 指向不存在的 CQ / CQ Inactive / namespaceSelector 不匹配 | 看 Workload 的 `Inadmissible` 原因 |
| 优先级突然变化 | 集群里新增了名为 `default` 的 WorkloadPriorityClass | 审计默认 WPC；显式设置标签 |
| Deployment 不生效 | 标签打在外层而不是 Pod 模板 | 改 `spec.template.metadata.labels` |

### 8.5.4 Admission gating

Kueue 支持通过注解把某个 Job 的"能否开始准入"交给外部控制器：

```yaml
metadata:
  annotations:
    kueue.x-k8s.io/admission-gated-by: example.com/my-gate,example.com/second-gate
```

语义：

- 注解可以写在 Job/JobSet/Pod/TrainJob 等被管理对象上，Job 集成会把它复制到 Workload；
- 只要注解存在，Workload 就不会被准入（`QuotaReserved=False`）；
- 外部控制器完成自己的检查后，移除自己那一项（或整个注解），Kueue 恢复正常准入；
- 注解只能在创建时添加，之后只能移除/缩短，不能追加；
- gate 名称要符合 RFC 1123 subdomain / RFC 3986 的格式；
- 事件：`AdmissionGated` / `AdmissionGateCleared`。

它和 AdmissionCheck 的区别：

- **admission gate 在配额预留之前**：Workload 连配额都拿不到；
- **AdmissionCheck 在配额预留之后、Admitted 之前**：配额已经被锁住；
- 两者可以叠加，但不要让同一个外部系统同时做两件事，否则排障会非常绕。

典型用途：安全扫描、审批流、许可证检查、外部调度器"开闸"。

### 8.5.5 标签复制

Configuration 的 `integrations.labelKeysToCopy` 可以把 Job/Pod 的标签复制到 Workload：

```yaml
integrations:
  labelKeysToCopy:
  - "team.example.com/cost-center"
  - "team.example.com/project"
```

用途：成本分摊、审计、按项目查 Workload。Kueue 不会复制自己的内部标签（`multikueue-origin`、`concurrent-admission-parent`、`job-uid`），即使你列出来也会在启动日志里提示。

## 8.6 集成配置

Kueue 的 Configuration 中配置启用的框架：

```yaml
apiVersion: config.kueue.x-k8s.io/v1beta2
kind: Configuration
integrations:
  frameworks:
  - "batch/job"
  - "jobset.x-k8s.io/jobset"
  - "kubeflow.org/mpijob"
  - "kubeflow.org/pytorchjob"
  - "ray.io/rayjob"
  - "ray.io/raycluster"
  - "apps/deployment"
  - "apps/statefulset"
  - "leaderworkerset.x-k8s.io/leaderworkerset"
  - "pod"
```

框架名以你的版本为准。启用太多集成会增加控制器 watch 的对象数量和内存占用；只启用实际用到的。

## 8.7 暂停与放行的几种机制

不同对象的能力不同：

- **Job**：`spec.suspend`。
- **JobSet**：顶层 `spec.suspend`；不是每个 replicatedJob 各有 suspend。
- **Kubeflow/Ray**：通过框架的 suspend/暂停字段或副本控制。
- **Pod**：通常通过 scheduling gate 或 webhook 阻止调度，准入后移除。
- **Deployment**：没有统一的 suspend 字段；webhook 把队列标签注入 Pod 模板，Pod 集成逐 Pod 用 scheduling gate 管理。
- **StatefulSet**：用 Plain Pod Group 管理成员；v0.20 不支持扩缩，不要用 `replicas=0` 作为暂停机制。

无论机制如何，核心不变量是：

> Workload 未 Admitted 之前，真实 Pod 不应该被创建并开始消耗资源。

## 8.8 完成、失败与重试

集成层还要把用户对象的状态同步回 Kueue：

- 成功完成 → Workload `Finished`；
- 失败 → 根据策略重试或标记失败；
- 被驱逐 → 用户对象被暂停/删除 Pod，Workload 重排队；
- Pod 无法启动 → 可能触发 `WaitForPodsReady` 超时。

Kueue 不直接管理 Job 的重试逻辑；这仍由 Job/框架控制器负责。Kueue 只管理"是否允许运行"和"配额"。

## 8.9 自定义集成

如果你有一个自研作业框架，可以按以下步骤接入。

### 8.9.1 定义 Workload

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Workload
metadata:
  name: myjob-abc
  namespace: team-a
  ownerReferences:
  - apiVersion: example.com/v1
    kind: MyJob
    name: myjob
    uid: ...
    controller: true
spec:
  active: true
  queueName: team-a-queue
  podSets:
  - name: main
    count: 4
    template:
      spec:
        containers:
        - name: main
          image: example/myjob
          resources:
            requests:
              cpu: "2"
              memory: 4Gi
```

### 8.9.2 暂停真实作业

在 Workload 创建后，把你的 MyJob 设为暂停，或确保它不会创建 Pod。

### 8.9.3 监听准入

Watch Workload 的 `Admitted` 条件：

```text
Admitted=True  → 放行 MyJob
Evicted=True   → 暂停/清理 MyJob 的 Pod，等待重排队
Finished=True  → 清理 Workload
```

### 8.9.4 处理 Flavor 注入

Kueue 可能给 Pod 模板注入 nodeSelector/tolerations（基于 ResourceFlavor）。你的集成需要确保这些变更应用到真实 Pod 模板。

### 8.9.5 处理资源变化

如果作业的实际 Pod 数变化，需要更新 `spec.podSets[*].count` 或报告 `reclaimablePods`。随意修改已准入 Workload 的请求会破坏配额账本，必须通过 Kueue 支持的接口。

### 8.9.6 最小集成伪代码

```go
func reconcileMyJob(job *MyJob) error {
    // 注意：不要在这里因为 job.Spec.Suspend == true 就直接 return。
    // 集成自己也会把 Job 设为 suspended；提前返回会让 Workload 永远不被
    // 创建/更新，Job 就永远等不到准入。要区分"用户主动暂停"和"集成暂停"。
    if job.Annotations["myjob.example.com/user-suspended"] == "true" {
        return nil
    }
    wl := buildWorkload(job)       // 幂等创建/更新
    if err := ensureWorkload(wl); err != nil {
        return err
    }
    if !admitted(wl) {
        setSuspend(job, true, "kueue-pending") // 记录暂停原因
        return updateJob(job)
    }
    if evicted(wl) {
        setSuspend(job, true, "kueue-evicted")
        cleanupPods(job)
        return updateJob(job)
    }
    setSuspend(job, false, "")
    return updateJob(job)
}
```

真实实现要处理：

- ownerReference 和垃圾回收；
- Workload 已存在时的幂等更新；
- 并发 reconcile；
- 条件的时间戳/generation；
- 作业完成后删除 Workload；
- 状态同步。

### 8.9.7 推荐的 reconcile 状态机

把集成逻辑写成显式状态机，比一堆 if 更可靠：

```text
观察 MyJob + Workload
  │
  ├── Workload 不存在
  │     → 构造 Workload（确定性名称 + ownerReference）
  │     → 暂停 MyJob
  │     → 创建 Workload
  │
  ├── Workload 存在，但 spec 与 MyJob 不一致
  │     → 只更新允许变化的字段（count/priority）
  │     → 不要改已准入的资源请求
  │
  ├── QuotaReserved=False / Admitted=False
  │     → 确保 MyJob 暂停
  │     → 等待
  │
  ├── Admitted=True
  │     → 注入 nodeSelector/tolerations
  │     → 取消暂停
  │
  ├── Evicted=True
  │     → 暂停、清理 Pod
  │     → 等待重排队
  │
  └── MyJob 完成/失败
        → 标记 Workload Finished（或由 Kueue 观察 Pod 状态）
```

关键不变量：**在 Workload 未 Admitted 之前，MyJob 不能创建 Pod。** 所有分支都要围绕这条不变量。

## 8.10 集成层的常见坑

### 1. 重复创建 Workload

必须用确定性名称或 owner reference，避免每次 reconcile 都创建新 Workload。

### 2. 暂停不彻底

如果只暂停 Job 的一部分，Pod 仍可能被创建，导致 Kueue 的配额账本和实际资源不一致。

### 3. 修改已准入 Workload 的资源

Kueue 的 Cache 已经按准入时的用量记账。随意改大请求会导致超额；改小则需要走 `reclaimablePods`。

### 4. 忽略 Flavor 注入

如果集成没有把 nodeSelector/tolerations 应用到真实 Pod，Pod 可能落到错误的节点，甚至无法调度。

### 5. 不处理驱逐

被抢占后如果集成不暂停/清理，Job 会继续跑，违背 Kueue 的配额决策。

### 6. 不支持 gang

多 PodSet 作业必须整体准入。如果集成只创建部分 Pod，会破坏 all-or-nothing 语义。

## 8.11 MiniKueue 的“集成”

MiniKueue 没有真实的 Job 控制器，它直接用 Workload 模拟：

- 场景中的 `Workload` 就是用户提交的对象；
- `Engine.Submit` 相当于集成层创建 Workload 并暂停 Job；
- `Admitted` 后没有真实 Pod 创建，只有状态变化；
- `Finish` 模拟 Job 完成。

这让它可以把注意力放在队列和配额算法上，而不是 Kubernetes 控制器细节。第 15 章会说明如何把 MiniKueue 的调度核心嵌入一个真实的 controller-runtime 项目。

## 8.12 本章小结

- 集成层负责把用户作业转换成 Workload，并在准入前暂停、准入后放行。
- 一个合格集成要满足"发现/转换/暂停/放行/驱逐/完成/账本/注入/幂等"九条契约。
- batch/Job 用 `spec.suspend`；其他框架用各自的暂停/门控机制；长期运行对象要定义"什么算完成"。
- 多 PodSet 作业要整体准入，保证配额层面的 all-or-nothing；物理 gang 放置是另一层。
- 集成必须处理完成、失败、驱逐、Flavor 注入和资源变化。
- 纳管边界有五种机制：显式标签、默认 LocalQueue、默认 WorkloadPriorityClass、`manageJobsWithoutQueueName` + namespace selector、ValidatingAdmissionPolicy；它们解决不同问题，不能互相替代。
- admission gate 与 AdmissionCheck 是两道不同的门，不要混用。
- 自定义集成的核心是：幂等创建 Workload、正确暂停/放行、监听条件、维护账本一致。
- MiniKueue 用 Workload 直接模拟，省略了真实 Job 控制器。
- 集成层放行 Pod 之后，真正的节点放置、TAS 注入、DRA 分配和 ProvisioningRequest 扩容见第 28 章；从模拟器迁移到真实 controller-runtime 的工程步骤见第 30 章。

---

[← 上一章：抢占与回收](07-preemption-reclaim.md) | [返回目录](../README.md) | [下一章：高级能力 →](09-advanced-features.md)
