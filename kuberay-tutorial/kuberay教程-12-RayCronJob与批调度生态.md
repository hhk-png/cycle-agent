# 第 12 章 RayCronJob 与批调度生态

RayJob 解决“跑一次”，RayCronJob 解决“按计划反复跑”。本章先讲 RayCronJob 的真实 API（比 Kubernetes CronJob 简单很多），再讲调度语义、重入窗口、并发控制和可观测性，最后把 KubeRay 与批调度器的集成放到一张图里。

## 12.1 RayCronJob 是什么

RayCronJob 是 v1.6 引入的 alpha CRD（feature gate `RayCronJob` 默认关闭）：

- 按 cron 表达式创建 RayJob；
- 子 RayJob 由 ownerReference 管理；
- 支持时区；
- 支持 suspend；
- 不直接管理 RayCluster，集群由子 RayJob 创建。

最小示例：

```yaml
apiVersion: ray.io/v1
kind: RayCronJob
metadata:
  name: nightly
spec:
  schedule: "0 2 * * *"
  timeZone: UTC
  jobTemplate:
    entrypoint: python /home/ray/jobs/nightly.py
    shutdownAfterJobFinishes: true
    ttlSecondsAfterFinished: 600
    rayClusterSpec:
      rayVersion: "2.59.0"
      headGroupSpec:
        rayStartParams: {}
        template:
          spec:
            restartPolicy: Never
            containers:
              - name: ray
                image: rayproject/ray:2.59.0
                resources:
                  requests:
                    cpu: "1"
                    memory: 2Gi
      workerGroupSpecs:
        - groupName: workers
          replicas: 2
          minReplicas: 0
          maxReplicas: 8
          rayStartParams: {}
          template:
            spec:
              restartPolicy: Never
              containers:
                - name: ray
                  image: rayproject/ray:2.59.0
                  resources:
                    requests:
                      cpu: "2"
                      memory: 4Gi
```

## 12.2 字段（v1.7.1）

| 字段 | 说明 |
|---|---|
| `schedule` | 五段 cron：分钟 小时 日 月 周 |
| `timeZone` | IANA 时区；为空时用 operator 本地时区；不能写空字符串 |
| `suspend` | 暂停调度；不影响已创建的子 RayJob |
| `jobTemplate` | 一个完整的 RayJobSpec 模板 |

v1.7.1 的 RayCronJob **没有**：

- `concurrencyPolicy`（Allow/Forbid/Replace）；
- `successfulJobsHistoryLimit` / `failedJobsHistoryLimit`；
- `startingDeadlineSeconds`。

注意 `activeDeadlineSeconds` 不是 RayCronJob 的顶层字段，但 `jobTemplate` 是一个完整的 `RayJobSpec`，所以可以写在 `jobTemplate.activeDeadlineSeconds` 里限制单个子作业的运行时长。

这与 Kubernetes `batch/v1 CronJob` 不同。不要照搬 K8s CronJob 的字段。字段少不代表语义简单：并发、补跑、清理、可观测性都要由平台自己补齐，后面几节逐项展开。

## 12.3 调度语义

KubeRay 的实现：

1. 校验 cron；
2. 如果 suspend，直接返回；
3. 计算下一次调度时间：
   - 第一次从 `creationTimestamp` 开始；
   - 之后从 `status.lastScheduleTime` 开始；
4. 时间未到 -> requeue；
5. 到点 -> 创建子 RayJob；
6. 子 RayJob 名字：v1.7.1 使用确定性的 `<cronjob>-<scheduledTime.Unix()/60>`（`getRayJobName`/`getTimeHashInMinutes`）；MiniKubeRay 与之对齐；
7. 更新 `status.lastScheduleTime`；
8. 计算下一次时间并 requeue。

### 12.3.1 命名的幂等性

v1.7.1 的子作业名由**计划时刻**决定，同一 tick 重复 create 会命中 `AlreadyExists`，不会创建两个不同名字的子作业。这借鉴了 Kubernetes `batch/v1` CronJob 的命名方式。

版本差异很重要：

- **v1.6.x** 使用 `<cronjob>-<rand(5)>` 随机后缀；create 与 status 写回不是原子的，重入窗口可能为同一 tick 创建两个子作业。issue #4849 记录的就是 v1.6.1 上的这个 bug（约 1/15 触发）。
- **v1.7.1** 已改为确定性命名（上游源码注释明确写了 “so a repeated Create for the same tick collides with AlreadyExists”）。

即便如此，仍要注意：

- `status.lastScheduleTime` 的写回与 create 不是原子的；确定性名字把“重复创建”降级为“AlreadyExists”，但补跑/错过调度的语义仍要监控；
- 用标签 `ray.io/cronjob-name=<parent>` 查询某次计划的所有子作业；
- 业务侧仍应按业务日期分区/唯一约束做幂等，不要把控制面命名当成唯一防线。

MiniKubeRay 使用确定性名字，与 v1.7.1 对齐；如果你维护的是 v1.6.x，不能假设同样的幂等性。

### 12.3.2 时区

- `timeZone: Asia/Shanghai` 表示按上海时间解释 cron；
- 不要写 `TZ=Asia/Shanghai 0 2 * * *`，校验会拒绝；
- Windows 上的 Python 可能没有 IANA 时区数据库；MiniKubeRay 提供了常见时区的固定偏移回退（见第 22 章）；
- 时区只影响“何时触发”，不改变子 RayJob 内的 `TZ` 环境变量。如果作业逻辑依赖本地时间，要在容器里显式设置；
- 夏令时地区（如 `America/New_York`）在切换日会出现“同一本地时间出现两次”或“跳过一小时”。对账任务应使用 UTC，或在作业内部按业务日期去重。

### 12.3.3 错过调度与补跑

**上游 KubeRay 会补跑，但按调和节奏推进**：控制器用 `schedule.Next(lastScheduleTime)` 计算下一次计划时刻；如果它已经过去，就把 `RequeueAfter` 设为 0，下一轮立即再算一次。因此停机期间错过的周期不会一次性全部创建，而是**每次调和补一个 tick**，直到追上当前时间。

MiniKubeRay 的教学实现会在一次调和里把错过的周期全部补跑（第 12.7 节的实验展示 3 次补跑），用于演示“补跑 + 幂等/突发并发”的失败模式。两者都会补跑，差别是**补跑是逐 tick 还是一次性批量**；不要把 MiniKubeRay 的批量补跑当成上游的节奏。

判断调度健康时看：

| 量 | 含义 | 期望 |
|---|---|---|
| `now - lastScheduleTime` | 落后多少个调度周期 | 不超过业务允许的延迟 |
| 子 RayJob 数 | 是否有重复/遗漏 | 与业务的对账口径一致 |
| 子 RayJob 的创建时间/标签 | 追溯计划时刻 | 上游写 `ray.io/cronjob-name` 与 `ray.io/cronjob-scheduled-timestamp`（RFC3339） |

如果业务需要补跑（例如“停机后补上每天的日报”），不要依赖 RayCronJob 的默认行为；用外部工作流按业务日期枚举，或在 entrypoint 里做幂等重放。如果业务不能接受补跑，则要监控 `lastScheduleTime` 并在恢复后人工触发。

### 12.3.4 cron 表达式语义

RayCronJob 使用五段 cron，没有“秒”字段：

```text
┌───────────── 分钟 (0-59)
│ ┌─────────── 小时 (0-23)
│ │ ┌───────── 日 (1-31)
│ │ │ ┌─────── 月 (1-12)
│ │ │ │ ┌───── 周 (0-6，0=周日)
│ │ │ │ │
* * * * *
```

常见写法：

| 表达式 | 含义 |
|---|---|
| `0 2 * * *` | 每天 02:00 |
| `*/15 * * * *` | 每 15 分钟 |
| `0 9 * * 1-5` | 工作日 09:00 |
| `0 0 1 * *` | 每月 1 日 00:00 |
| `30 4 * * 0` | 每周日 04:30 |

容易踩的坑：

- **日和周同时受限**：标准 cron 中“日”和“周”是 OR 还是 AND，不同实现不一致。不要写 `0 0 1 * 1` 这种“既是 1 号又是周一”的表达式；用两个 RayCronJob 或在上层判断。
- **月末**：`0 0 31 * *` 在没有 31 日的月份不会触发，也不会自动顺延到月末。
- **时区与 UTC**：跨时区团队统一用 UTC 最省事；本地时区只用于“人类作息”的任务。
- **没有秒级调度**：需要秒级就用 Ray Job/工作流，不要用 RayCronJob。
- **校验失败不会自动修复**：非法 cron/时区只会产生事件/状态，不会创建子作业；要监控 `lastScheduleTime` 是否停滞。

### 12.3.5 控制器伪代码与重入窗口

把 RayCronJob 的调和写成伪代码，重入窗口就很清楚：

```text
reconcile(cronjob):
    if deleted: return                 # 子 RayJob 交给 ownerReference GC
    if suspend: return
    last = status.lastScheduleTime or creationTimestamp
    next = cron.next(after=last, tz=timeZone)
    if next > now: return              # 还没到点
    name = childName(next)             # v1.7.1：<cron>-<unix/60>；v1.6.x：<cron>-<rand(5)>
    if not exists(RayJob, name):
        create(RayJob, name, spec=jobTemplate)
    status.lastScheduleTime = next
    requeue()
```

三个关键点：

1. **v1.7.1 的名字由计划时刻决定**：同一 `next` 稳定，重复 create 得到 AlreadyExists，天然降低重复风险。v1.6.x 的随机后缀没有这个保护。
2. **status 写回与 create 不是原子的**。控制器必须能容忍“同一计划时刻被处理两次”。v1.6.x 的 issue #4849 就是重入窗口导致重复子作业的实例；v1.7.1 用确定性命名把后果降级为 AlreadyExists。
3. **requeue 不能依赖 watch 子 RayJob**。子作业的进度由 RayJob 控制器负责，RayCronJob 只关心下一次计划时刻。

## 12.4 子 RayJob 的生命周期与可观测性

RayCronJob 只创建 RayJob；后续由 RayJob 控制器负责：

- 建集群；
- 提交作业；
- 清理；
- 重试。

因此：

- RayJob 的 `shutdownAfterJobFinishes`、TTL、deletion rules 仍然生效；
- 子 RayJob 失败不会自动阻止下一次调度（除非你自己加 concurrency 控制）；
- 历史子 RayJob 默认保留，除非 RayJob 自己被删除（ownerReference GC）。

### 12.4.1 标签与注解：把子作业和计划时刻对上

MiniKubeRay 创建子 RayJob 时会写：

| 键 | 值 | 用途 |
|---|---|---|
| `ray.io/cronjob-name` | 父 RayCronJob 名 | 查询某个 cron 的所有子作业 |
| `ray.io/cronjob-scheduled-timestamp`（注解） | 本次计划时刻（ISO8601） | 补跑去重、审计、对账 |

`ray.io/cronjob-scheduled-timestamp` 是上游 v1.7 就存在的注解（RFC3339），子 RayJob 创建时写入；它适合回答“哪个计划时刻跑的”。但不要把它当作唯一对账口径：补跑/重试仍可能产生多个子作业，需要结合 `ray.io/cronjob-name` 与作业内容判断。否则你无法回答：

- 昨天 02:00 的任务到底跑了没有？
- 这个 RayJob 是哪次计划触发的？
- 补跑有没有重复？

### 12.4.2 没有历史限制怎么办

Kubernetes CronJob 有 `successfulJobsHistoryLimit`/`failedJobsHistoryLimit`；RayCronJob v1.7 没有。可选做法：

| 做法 | 优点 | 代价 |
|---|---|---|
| 子 RayJob 设 `ttlSecondsAfterFinished` | 自动清理 RayCluster + submitter | 需要 `shutdownAfterJobFinishes` 配合；**默认不删 RayJob CR** |
| 用 deletion rules 分成功/失败保留 | 失败多留、成功快删 | 规则复杂，需要测试 |
| 外部定时清理脚本 | 灵活 | 多一个组件，可能误删 |
| 归档到 History/对象存储后删除 | 可审计 | 需要额外流水线 |

不要无限保留子 RayJob：每个 RayJob 的 status、事件和子对象都会增加 etcd 与控制面负担（第 41 章）。

### 12.4.3 与 RayJob 清理策略的交互

```yaml
jobTemplate:
  shutdownAfterJobFinishes: true
  ttlSecondsAfterFinished: 600
```

- 作业结束 -> 删除 RayCluster；
- 600 秒后 -> 删除 RayCluster + submitter（v1.7 默认）；**RayJob CR 本身仍然保留**，除非 operator 设置了 `DELETE_RAYJOB_CR_AFTER_JOB_FINISHES=true`，或 `deletionStrategy` 明确包含 `DeleteSelf`；
- RayCronJob 本身保留，`lastScheduleTime` 不受影响。

如果看到子 RayJob 越积越多，先确认这两层语义，不要误以为 `ttlSecondsAfterFinished` 会删 CR。

如果同时设置了 deletion rules，要确认规则不依赖已被删除的集群；RayJob 的 `DeleteCluster` 与 `DeleteSelf` 的顺序由控制器处理，但自定义规则仍应遵守“先集群、后 CR”的直觉。

## 12.5 并发控制的四种实现方式

RayCronJob 没有 `concurrencyPolicy`，所以“同一时间只跑一个”必须在别处实现。按侵入性从低到高：

### 12.5.1 作业内幂等 + 分布式锁

entrypoint 先抢一个带 TTL 的锁（Redis/etcd/数据库），拿不到就退出 0 或等待。适合：

- 任务本身可重入；
- 你能接受“后到的任务直接跳过”；
- 不想引入新的调度组件。

缺点：锁的 TTL 要覆盖最长运行时间；进程被杀后锁要能过期。

### 12.5.2 业务日期分区

把输出写到 `date=<业务日期>` 分区，已存在 `_SUCCESS` 就跳过。适合：

- 日报/离线 ETL；
- 重跑天然幂等；
- 补跑和并发都只会产生一份结果。

这是最推荐的做法：把并发问题转成幂等问题，而不是调度问题。

### 12.5.3 Kueue / 队列准入

用 Kueue 的 ClusterQueue 配额限制同时运行的作业数。注意 v1.7.1 的 `jobTemplate` 是裸 `RayJobSpec`，没有 metadata 字段，因此**不能**在 RayCronJob 里直接给子 RayJob 打 `kueue.x-k8s.io/queue-name`；需要在上层（工作流/准入）或 RayCluster 模板上做队列控制，或等带 `RayJobTemplateSpec.metadata` 的版本。适合：

- 多租户平台；
- 需要配额、抢占、公平共享；
- 同时管理 RayJob/RayCronJob 和其他批任务。

Kueue 的 `managedBy: kueue.x-k8s.io/multikueue` 时，KubeRay 不调和该 RayJob，由 Kueue 接管（第 29 章）。

### 12.5.4 外部工作流（Argo/Airflow）

让工作流系统负责“同一时间只跑一个”和依赖关系，RayCronJob 只作为执行器或干脆不用。适合：

- 任务之间有 DAG 依赖；
- 需要重试策略、告警、审计；
- 已经有工作流平台。

### 12.5.5 选择建议

| 需求 | 首选 |
|---|---|
| 单任务、可重入 | 业务日期分区 |
| 需要跨任务互斥 | 分布式锁 |
| 多租户配额/抢占 | Kueue |
| DAG/跨系统编排 | Argo/Airflow |

## 12.6 监控与告警

RayCronJob 只有 `lastScheduleTime` 一个关键状态，但这一个字段能覆盖大部分故障：

KubeRay 不导出 `kuberay_raycronjob_last_schedule_timestamp_seconds` 这类指标；它的 collector 覆盖 RayCluster、RayJob 和 RayService（`kuberay_service_*`）。RayCronJob 目前没有专门的指标，所以下面的 PromQL 不能直接用。用脚本/CR scrape 计算：

```bash
kubectl get raycronjobs -A -o json | jq -r '
  .items[] | [.metadata.namespace, .metadata.name,
  (.status.lastScheduleTime // "never")] | @tsv'
```

建议告警：

| 告警 | 条件 | 说明 |
|---|---|---|
| 调度停滞 | `lastScheduleTime` 超过 1.5–2 个周期未更新 | operator 挂了、cron 非法、suspend 忘关 |
| 子作业连续失败 | 同一 cron 的最近 N 个子 RayJob 都 Failed | 代码/依赖/配额问题 |
| 补跑风暴 | 短时间内创建大量子 RayJob | operator 停机后恢复，需要限流 |
| 子作业堆积 | 未终止的子 RayJob 数超过阈值 | 并发失控或清理失效 |
| 运行时长异常 | 子作业 Running 超过预期 | 死锁、数据倾斜、资源不足 |

日志/事件侧要记录：`CreatedRayJob`、`FailedToCreateRayJob`、`InvalidRayCronJobSpec`、`SuspendedRayCronJob`。这些事件比“看 Pod”更早暴露问题。

## 12.7 实验：确定性地观察调度、时区与 suspend

`examples/cronjob_lab.py` 用 `ManualClock` 把时间变成可断言的输入：

```bash
python examples/cronjob_lab.py
```

输出（节选）：

```text
nightly (0 2 * * *, UTC)
  clock=2026-01-01T03:00:00+00:00 children=['nightly-29453880']
  duplicate reconciles: 1 -> 1 child(ren)

suspended for 24h: children=1
after resume + 48h: children=4

morning-cn (0 9 * * *, Asia/Shanghai)
  clock=2026-01-05T02:00:00+00:00 children=['morning-cn-29459580']

OK: cron scheduling, time zone, suspend and idempotency behaved as specified
```

实验验证了四件事：

1. 到点才创建，同一分钟重复调和不会重复创建；
2. suspend 停止调度但不删除已有子作业；
3. 恢复后补跑多个错过的周期（这里是 3 个；MiniKubeRay 一次调和批量补完，上游按调和逐 tick 补）；
4. `Asia/Shanghai 09:00` 对应 `01:00 UTC`。

把脚本里的 `step` 改小或把 `schedule` 改成 `*/5 * * * *`，可以观察更细的调度节奏。确定性时钟意味着输出可以进 CI，而不是“看起来跑了”。

## 12.8 与 Kubernetes CronJob 的对比

| 维度 | Kubernetes CronJob | RayCronJob |
|---|---|---|
| 调度对象 | Pod/Job | RayJob |
| 并发策略 | `concurrencyPolicy` | 无（v1.7） |
| 历史限制 | 有 | 无 |
| 时区 | `timeZone`（1.27+） | `timeZone` |
| 失败处理 | Job backoff | RayJob backoff |
| 资源 | Pod 模板 | RayCluster 模板 |
| 补跑语义 | `startingDeadlineSeconds` 控制 | 由控制器按 lastScheduleTime 推进 |
| 状态 | `.status.active/lastScheduleTime` | `.status.lastScheduleTime`；暂停用 `spec.suspend`（`.status.suspended` 是 MiniKubeRay 扩展） |

如果你需要“同一时间只跑一个”，目前要在应用层或上层工作流（Argo Workflows、Airflow、Kueue）实现。

## 12.9 批调度生态（速览）

KubeRay 的 Pod 默认由 kube-scheduler 调度。gang scheduling/队列/配额需要外部调度器或 Kueue。完整集成见[第 29 章](kuberay教程-29-生态集成-队列、调度与工作流.md)，这里只列与 RayCronJob/RayJob 相关的结论：

| 组件 | 与 KubeRay 的接口 | 关键点 |
|---|---|---|
| Volcano | `--batch-scheduler=volcano`，创建 PodGroup | 队列用 `volcano.sh/queue-name` 标签 |
| YuniKorn | `--batch-scheduler=yunikorn` | 层次队列，不需要 PodGroup |
| scheduler-plugins | `--batch-scheduler=scheduler-plugins` | coscheduling PodGroup |
| Kueue | 本地队列标签（LocalQueue/ClusterQueue）用于准入/配额；`managedBy: kueue.x-k8s.io/multikueue` 是另一套 MultiKueue 机制 | 不要把两者混为一谈 |
| Kubernetes WAS | `KubernetesWAS` gate | v1.7 alpha，与 `--batch-scheduler` 互斥 |

对 RayCronJob 的额外影响：

- 定时任务可能在同一时间重叠；上游没有 `concurrencyPolicy`，需要 Kueue/上层工作流控制并发；
- 子 RayJob 继承 `jobTemplate`；v1.7.1 的 `jobTemplate` 没有 metadata，队列标签不能直接写在子 RayJob 上（见 12.5.3）；
- 批调度器看的是 PodGroup 的 `minMember`，定时任务的最坏并发要算进队列配额；
- 补跑会产生“突发并发”，队列配额要按最坏情况预留，否则补跑会把在线任务挤掉。

## 12.10 选择建议

| 需求 | 建议 |
|---|---|
| 只需要定时跑 | RayCronJob |
| 需要队列/配额 | Kueue |
| 需要 gang scheduling（Volcano 生态） | Volcano |
| 需要 YuniKorn 队列 | YuniKorn |
| 需要 K8s 原生 coscheduling | scheduler-plugins |
| 需要跨集群 | Kueue MultiKueue |
| 需要 DAG/依赖 | Argo Workflows/Airflow |

详细的 ClusterQueue/PodGroup/Argo/Airflow 配置见[第 29 章](kuberay教程-29-生态集成-队列、调度与工作流.md)。

## 12.11 MiniKubeRay 的实现

`minikuberay/controllers/raycronjob.py`：

- feature gate `RayCronJob`；
- cron 解析（5 段、名称、范围、步长）；
- timeZone（UTC/常见时区回退）；
- 确定性子 RayJob 名字 `<cron>-<unix 分钟>`（与 v1.7.1 对齐；v1.6.x 使用随机后缀，见 12.3.1）；
- ownerReference；
- `lastScheduleTime`；
- suspend/resume；
- 标签 `ray.io/cronjob-name` 与注解 `ray.io/cronjob-scheduled-timestamp`（上游）；MiniKubeRay 同样写入；
- 事件。

测试与实验：

- `tests/test_raycronjob.py` 覆盖调度、时区、suspend、重复 tick、feature gate；
- `tests/test_examples.py::test_cronjob_lab_example` 把 `examples/cronjob_lab.py` 的断言纳入 pytest；
- `examples/cronjob_lab.py` 可单独运行，输出可对照。

## 12.12 小结

- RayCronJob 是“定时创建 RayJob”，不是 Kubernetes CronJob 的完整替代。
- v1.7 只有 schedule/timeZone/suspend/jobTemplate；并发/历史限制要上层实现。
- v1.7.1 的子作业名由计划时刻决定，重复 tick 会命中 AlreadyExists；v1.6.x 使用随机后缀（issue #4849）。升级前确认版本。
- 即便如此，`lastScheduleTime` 与 create 仍非原子，补跑行为要监控，不要假设“只补一次”。
- 并发问题优先用“业务日期分区 + 幂等”解决，再考虑锁、Kueue、工作流。
- `lastScheduleTime` 是最重要的健康信号；子作业失败不会自动阻止下一次调度。
- 批调度器解决 gang scheduling、队列、配额、抢占；Kueue 是多租户平台的首选。
- 选择批调度器要看现有生态，不要同时开多个。

下一章高可用与容错：head 挂了怎么办，GCS FT 如何工作，RayService 如何做 HA。

→ [第 13 章 高可用、容错与 GCS FT](kuberay教程-13-高可用、容错与GCS-FT.md)
