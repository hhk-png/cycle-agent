# 第 10 章 DaemonSet、Job 与 CronJob

## 10.1 DaemonSet：每节点一个

DaemonSet 保证符合条件的节点上都运行一个 Pod，典型用途：

- 日志采集（Fluent Bit、Filebeat）。
- 监控 agent（node-exporter、Datadog agent）。
- CNI/CSI 插件。
- 安全 agent、缓存、本地负载均衡。

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: node-agent
  namespace: kube-system
spec:
  selector:
    matchLabels: {app: node-agent}
  template:
    metadata:
      labels: {app: node-agent}
    spec:
      tolerations:
        - operator: Exists
      containers:
        - name: agent
          image: node-agent:1.0
          resources:
            requests: {cpu: 20m, memory: 24Mi}
```

### 调度行为

从 Kubernetes 1.12 起，DaemonSet 控制器**不再直接写 `spec.nodeName`**。它为每个目标节点创建一个带 `nodeAffinity`（绑定到该节点）的 Pod，由默认调度器完成绑定。调度器仍然执行资源、污点、端口等检查；如果调度器不可用，DaemonSet Pod 会保持 Pending。旧文档中“DaemonSet 绕过调度器”的说法只适用于很老的版本。

控制器根据以下条件决定哪些节点需要 Pod：

- nodeSelector / nodeAffinity。
- 污点与容忍。
- 节点是否 Ready、是否 unschedulable。DaemonSet Pod 默认容忍 `node.kubernetes.io/unschedulable`，因此 `kubectl cordon` 后 Pod 仍保留在节点上（这与普通 Deployment Pod 不同）。
- 资源与端口（由调度器最终判定）。

常见做法：

- 容忍所有污点：`tolerations: [{operator: Exists}]`，确保控制面/专用节点也能采集。
- 但要注意资源开销和安全性；不是所有 agent 都该跑在所有节点。
- 使用 `nodeSelector` 限定 GPU/边缘节点。

### 更新

```yaml
updateStrategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 1
```

默认 `maxUnavailable: 1`、`maxSurge: 0`，即逐个节点先删后建；`maxSurge` 可让新 Pod 先起来再删旧 Pod，代价是节点上短时出现两个实例。也可以 `OnDelete` 手动控制。

### 资源与驱逐

DaemonSet Pod 通常设置较低 requests、较高优先级：

```yaml
priorityClassName: system-node-critical
```

避免节点压力时先被驱逐，导致监控/网络中断。但优先级过高也可能挤占业务资源，需要权衡。

MiniK8s 的 `DaemonSetController` 为每个符合条件的节点创建一个 Pod，并在模板变化时逐个重建。它检查 nodeSelector、unschedulable 和 NoSchedule/NoExecute 污点；但为了简化，仍直接写 `spec.nodeName` 绑定，而不是像真实实现那样用 nodeAffinity 交给调度器。测试 `test_daemonset_one_pod_per_node`、`test_daemonset_respects_node_taints`。

## 10.2 Job：运行到完成

Job 管理“成功完成 N 次”的 Pod：

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: batch-etl
spec:
  completions: 3
  parallelism: 2
  backoffLimit: 2
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: etl
          image: etl:2.1
          command: ["python", "-m", "etl"]
```

字段：

- `completions`：需要成功完成的 Pod 数。
- `parallelism`：同时运行的 Pod 数上限。
- `backoffLimit`：失败重试次数（失败 Pod 数上限）。
- `activeDeadlineSeconds`：整个 Job 的超时。
- `ttlSecondsAfterFinished`：完成后保留多久再删除。
- `suspend`：暂停创建 Pod。
- `completionMode: Indexed`：每个 Pod 获得 0..completions-1 的索引。
- `podFailurePolicy`：按失败原因决定重试/终止（1.26 beta，1.31 GA）。设置 `podFailurePolicy` 后，`podReplacementPolicy` 默认变为 `Failed`，且不能再使用 `TerminatingOrFailed`。
- `successPolicy`：部分成功即完成（1.33 GA；更早版本为 alpha/beta，字段/语义以目标版本为准）。

默认值：

| 字段 | 默认 | 说明 |
|---|---|---|
| `completions` | 1 | 需要成功的 Pod 数 |
| `parallelism` | 1 | 并发 Pod 数 |
| `backoffLimit` | 6 | 失败 Pod 数上限 |
| `completionMode` | `NonIndexed` | `Indexed` 给每个 Pod 唯一索引 |
| `suspend` | false | 暂停创建 |
| `ttlSecondsAfterFinished` | 不设置 | 不设置则永久保留 |
| `activeDeadlineSeconds` | 不设置 | 建议对可能无限重试的任务设置 |

`podFailurePolicy` 示例（只对特定退出码重试，其余立即失败）：

```yaml
spec:
  podFailurePolicy:
    rules:
      - action: Retry
        onExitCodes:
          containerName: etl
          operator: In
          values: [1, 2]
      - action: FailJob
        onExitCodes:
          containerName: etl
          operator: In
          values: [42]
```

### restartPolicy

Job Pod 只能是 `Never` 或 `OnFailure`：

- `Never`：容器失败，Pod 进入 Failed；Job 的 `backoffLimit` 按**失败 Pod 数**计数，并创建新 Pod 重试。
- `OnFailure`：容器失败，kubelet 在同一 Pod 内重启；Pod 不会立即变成 Failed，因此 `backoffLimit` 可能迟迟不触发。对可能无限崩溃的任务，用 `activeDeadlineSeconds` 设硬上界，或改用 `Never` + `podFailurePolicy` 精确控制。

`backoffLimit` 统计的是 Pod 级失败，不是容器重启次数。这是 `OnFailure` 最容易踩的坑：容器可能已经重启几十次，Job 仍然显示 0 个 failed。

选择：

- 任务可以原地重试、启动成本低：OnFailure。
- 需要保留每次失败现场/日志：Never。

### 完成与失败

Job 完成条件：

```yaml
status:
  succeeded: 3
  conditions:
    - type: Complete
      status: "True"
```

失败：

```yaml
status:
  failed: 3
  conditions:
    - type: Failed
      status: "True"
      reason: BackoffLimitExceeded
```

**默认策略下**，Job 不会因为部分失败就失败；只有超过 backoffLimit 或 activeDeadlineSeconds 才失败。配置 `podFailurePolicy` 后，`FailJob` 动作可以立即失败；Indexed Job 的 `maxFailedIndexes` 超限也会失败。已成功的 Pod 不会因为其他 Pod 失败而回滚。

### TTL 清理

```yaml
spec:
  ttlSecondsAfterFinished: 3600
```

完成后 1 小时删除 Job 及其 Pod（级联）。没有 TTL 的 Job 会永久保留，堆积 etcd 对象和日志。

### Indexed Job

```yaml
spec:
  completionMode: Indexed
  completions: 4
  parallelism: 4
```

每个 Pod 获得：

```text
JOB_COMPLETION_INDEX=0..3
```

适合分片处理：每个索引处理一部分数据。注意索引不保证与特定节点/存储绑定。

### 索引级失败控制

默认 `backoffLimit` 是“整个 Job 的失败 Pod 总数”。对分片任务，这会让一个坏分片耗尽所有重试，其他分片无法继续。较新版本提供：

- `backoffLimitPerIndex`：每个索引独立的重试次数。
- `maxFailedIndexes`：允许失败的分片数上限；超过则整个 Job 失败。

```yaml
spec:
  completionMode: Indexed
  completions: 100
  parallelism: 20
  backoffLimitPerIndex: 3
  maxFailedIndexes: 5
```

含义：每个分片最多失败 3 次；最多允许 5 个分片最终失败；超过则 Job 失败。使用前确认目标版本支持。

### podReplacementPolicy

Job 的 Pod 失败后，控制器可以：

- `TerminatingOrFailed`（默认）：等旧 Pod 终止后创建新 Pod。
- `Failed`：Pod 进入 Failed 就创建替代，不必等终止完成。

后者能加快重试，但会短暂增加同时存在的 Pod 数（旧 Pod 可能还在清理）。对资源紧张/有 finalizer 的任务要评估。

### JobSet：一组 Job 的编排

原生 Job 只管理一个模板。分布式训练/数据处理常需要：

- 多个角色（driver/worker/ps）。
- 一组 Job 同时启动/完成。
- 统一的失败/重试/暂停语义。

JobSet 用 `JobSet` CRD 管理一组 Job，配合 Kueue 做队列准入。它不替代 gang scheduling，但把“多 Job 生命周期”变成声明式对象。

### Job 的暂停、驱逐与原地重启

- `suspend: true` 会删除运行中的 Pod（受 `suspend` 语义影响），恢复后重建；不适合不能中断的任务。
- 节点驱逐会删除 Pod，Job 控制器按策略重建；索引任务可能重复执行同一索引，应用要幂等。
- `restartPolicy: OnFailure` 是容器级重启，不换 Pod；`Never` 是 Pod 级失败，换 Pod。选择取决于是否需要保留现场。

## 10.3 CronJob：定时任务

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: nightly-report
spec:
  schedule: "0 2 * * *"
  timeZone: "Asia/Shanghai"
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 300
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: report
              image: report:1.0
```

### 调度语义

- 五字段 cron：分 时 日 月 周。
- `timeZone`（1.27+ stable）：默认使用 controller manager 的时区（通常是 UTC）。
- `startingDeadlineSeconds`：错过的调度最多延迟多久；超过则跳过。
- `concurrencyPolicy`：
  - `Allow`：允许并发。
  - `Forbid`：上一次没结束就跳过本次。
  - `Replace`：取消上一次，启动新的。
- `suspend`：暂停调度。
- 历史限制：保留成功/失败 Job 数量。

### 10.3.1 夏令时、时区与错过调度

`timeZone` 指定 IANA 时区后，CronJob 按该时区的本地时间计算下一次触发。但夏令时（DST）切换会制造两个边界：

- **春季跳变**：本地时间从 02:00 直接到 03:00，落在 02:30 的调度可能当天不触发。
- **秋季回拨**：02:30 出现两次，调度可能重复触发或被去重。

工程建议：

1. 对账务、报表等“每天一次”的任务，用 UTC 或固定 offset 时区，避免 DST 歧义。
2. 任务必须幂等；用业务日期/唯一键去重，不要依赖“只触发一次”。
3. 观察 `status.lastScheduleTime`、`status.lastSuccessfulTime` 与 Job 事件；错过调度会体现在时间差和 `FailedNeedsStart` 类事件中。
4. `startingDeadlineSeconds` 要覆盖控制面短暂不可用的窗口；设得太小会直接跳过，太大则可能在恢复后补跑一批。
5. 超过控制器允许的连续错过次数（约 100 次）后，控制器会停止补跑并记录事件；不要把它当成“无限补跑”。

### CronJob 的触发保证

CronJob 不是精确定时器，也不是“恰好一次”执行器：

- 触发时间是近似的；控制器被阻塞、控制面不可用、节点繁忙都会造成延迟。调度间隔以一个最小时间窗口为单位（旧版 v1 控制器约 10 秒轮询；现代 CronJob v2 控制器使用 workqueue，但语义仍是近似）。
- “至少触发一次”有前提：`startingDeadlineSeconds` 足够大（或不设置）、`concurrencyPolicy: Allow`（或上一次已结束）、且没有超过控制器能处理的错过次数。超过 100 次错过调度时，控制器会记录事件并跳过，不会补跑全部。
- `Forbid` 会跳过本应触发的时间点；`Replace` 会删除正在跑的 Job，可能导致任务被中断。
- 任务必须**幂等**：重试、补跑、并发、节点重启都可能让同一业务动作执行多次。用唯一键/事务/去重表保证重复执行安全。

因此正确的期望是“在可接受的延迟内至少跑一次，且重复执行不会造成错误”，而不是“每天 02:00:00 精确执行且只执行一次”。

### 常见坑

- 任务执行时间超过调度间隔，Allow 导致堆积。
- Forbid 下任务一直卡住，后续所有调度被跳过。
- 时区搞错，凌晨任务跑在错误时间。
- 没有 TTL/历史限制，Job 和 Pod 堆积。
- 任务不幂等，重试导致重复写入。
- 集群控制面停机期间错过调度，恢复后行为取决于 startingDeadlineSeconds。

## 10.4 Job/CronJob 与控制器

Job 控制器创建 Pod，观察 Pod phase：

- Succeeded → succeeded++。
- Failed → failed++，必要时创建新 Pod。
- 达到 completions → Complete。
- 超过 backoffLimit → Failed。

CronJob 控制器根据 schedule 创建 Job，并设置 ownerReference；Job 再创建 Pod。

MiniK8s 的实现：

- `JobController`：创建 Pod、统计 succeeded/failed、Complete/Failed 条件、TTL 删除。
- `CronJobController`：可注入时钟，支持 Allow/Forbid/Replace、历史清理。
- 测试用假时钟避免等待真实分钟。

## 10.5 批处理资源与队列

原生 Job 适合简单批处理。大规模批处理需要：

- **队列与公平共享**：Kueue、Volcano、YuniKorn。
- ** gang scheduling**：一组 Pod 同时调度，避免死锁。
- **优先级与抢占**：高优先级作业插队。
- **配额与借还**：团队配额、弹性借还。
- **拓扑感知**：MPI/GPU 任务需要同机架/NVLink。
- **多队列**：研发、生产、离线共享集群。

第 21 章展开。

## 10.6 MiniK8s 实操

```bash
python -m mini_k8s.cli apply -f examples/05-jobs.yaml
python -m mini_k8s.cli get jobs
python -m mini_k8s.cli get pods
python -m mini_k8s.cli logs <job-pod>
```

观察 Job：

- `completions: 3, parallelism: 2`：最多 2 个 Pod，总共成功 3 个。
- Pod 注解 `mini-k8s.io/simulate.run-ticks: "2"` 让容器在 2 个 tick 后成功退出。

测试：

- `test_job_completes`
- `test_job_retries_failed_pods`
- `test_cronjob_schedule_with_fake_clock`

## 10.7 设计建议

- 批处理任务必须幂等；重试是常态。
- 设置 `activeDeadlineSeconds`，防止卡死任务永久占用资源。
- 设置 `ttlSecondsAfterFinished`，控制对象和日志留存。
- 大任务拆分成分片，使用 Indexed Job 或工作队列。
- 监控 Job 的 `Failed`、`BackoffLimitExceeded`、CronJob 的 `lastScheduleTime`。
- 对时间敏感任务，使用外部调度器或 Kueue，而不是只依赖 CronJob。

## 本章小结

- DaemonSet 负责节点级 agent，资源与容忍要谨慎设计。
- Job 管理“运行到完成”，completions/parallelism/backoff 决定重试与并发。
- CronJob 是分钟级调度器，不保证精确时间，必须处理错过的调度和并发。
- 大规模批处理需要队列、gang scheduling 和配额管理。

## 延伸阅读

- [第 21 章 批处理与队列](21-batch-and-queues.md)
- [第 26 章 多租户与资源治理](26-multitenancy-governance.md)
- [第 36 章 调度器与控制器实现](36-scheduler-controllers.md)
