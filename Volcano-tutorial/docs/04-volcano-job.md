# 第 4 章 VolcanoJob 深入

## 4.1 Job 是用户视角，PodGroup 是调度视角

一个 Volcano Job 提交后，控制器会做三件事：

1. 为 Job 创建一个 PodGroup，名字是 `<job-name>-<job-uid>`（兼容旧版本时会回退到 `<job-name>`）；
2. 为每个 task 的每个 replica 创建一个 Pod；
3. 持续根据 Pod 状态推进 Job 状态。

因此一个 Job 的“副本数”不是 `spec.replicas`，而是：

```text
总 Pod 数 = Σ tasks[i].replicas
minAvailable = gang 要求的最小同时可运行数
```

`minAvailable` 省略或写成 0 时，mutating webhook 会用所有 task 的 `minAvailable`（未设则 replicas）之和补齐——CRD 字段是 int32，无法区分“省略”和“显式 0”。因此 **0 不是 upstream 的“关闭 gang”开关**；要弱化 all-or-nothing，用 `minAvailable: 1`，或从调度器配置中移除 gang 插件。MiniVolcano 用额外的 `MinAvailableSet` 位区分两者，那是教学扩展，不是上游 API。

## 4.2 字段全景

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: example
  namespace: default
spec:
  schedulerName: volcano        # 通常显式写；省略时 admission 补默认调度器
  queue: team-a                 # 必须存在且 Open
  priorityClassName: high-priority
  minAvailable: 4           # 作业级 gang 门槛
  minSuccess: 4             # 可选：成功数达到 4 才判 Completed，低于它判 Failed
  maxRetry: 3               # 作业级重建次数
  plugins:
    ssh: []
    svc: ["--publish-not-ready-addresses=true"]
    pytorch: ["--master=master", "--worker=worker"]
  volumes:                  # 可选：作业级卷，挂到每个 Pod 的业务容器
    - mountPath: /data
      volumeClaimName: training-data
  policies:
    - event: PodEvicted
      action: RestartJob
    - event: PodFailed
      action: RestartJob
  tasks:
    - name: master
      replicas: 1
      minAvailable: 1        # 任务级门槛，写入 PodGroup.minTaskMember
      maxRetry: 3            # 任务级容器重启上限
      policies:
        - event: TaskCompleted
          action: CompleteJob
      template:
        metadata:
          labels: {role: master}
        spec:
          restartPolicy: OnFailure
          containers:
            - name: main
              image: pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime
              command: ["python", "train.py"]
              resources:
                requests:
                  cpu: "2"
                  memory: 8Gi
                  nvidia.com/gpu: "1"
    - name: worker
      replicas: 3
      minAvailable: 3
      template: { ... }
```

### 4.2.1 字段语义表

| 字段 | 类型 | 作用 | 注意 |
|---|---|---|---|
| `schedulerName` | string | 指定调度器 | 通常为 `volcano`；省略时 admission 补默认，显式 `default-scheduler` 则交给 kube-scheduler |
| `queue` | string | 队列 | 不存在或非 Open 会被拒绝/挂起 |
| `priorityClassName` | string | 引用 `scheduling.k8s.io/v1 PriorityClass` | 值越大优先级越高 |
| `minAvailable` | int32 | 作业级 gang 最小副本数 | 省略/0 都会被 webhook 补成 Σ task 的有效 minAvailable（未设则 replicas）；0 不是关闭 gang |
| `minSuccess` | *int32 | 成功副本数的完成/失败门槛 | 达到即提前 Completed；全部结束后仍不足则 Failed |
| `maxRetry` | int32 | 作业级重试次数 | 达到后 Job 进入 Failed |
| `volumes[]` | []VolumeSpec | 作业级卷，挂到每个 Pod 的业务容器 | `mountPath` 必填；`volumeClaimName` 引用已有 PVC |
| `tasks[].replicas` | int32 | 角色副本数 | 必须 ≥ 0 |
| `tasks[].minAvailable` | *int32 | 任务级 gang 门槛 | 省略时默认 replicas；若 `PartitionPolicy.MinPartitions > 0` 则默认 `MinPartitions × PartitionSize`。由控制器写入 `minTaskMember` |
| `tasks[].maxRetry` | int32 | 任务级容器重启上限 | 默认 3；`-1` 表示不限制 |
| `tasks[].dependsOn` | object | 任务依赖 | `name[]` + `iteration: any\|all`；只控制 Pod 创建时机 |
| `tasks[].topologyPolicy` | string | 任务 NUMA 策略 | `none`/`best-effort`/`restricted`/`single-numa-node` |
| `tasks[].template` | PodTemplateSpec | Pod 模板 | 与普通 Pod 基本一致 |
| `tasks[].policies` | []LifecyclePolicy | 任务级事件策略 | 优先于 Job 级策略 |
| `policies` | []LifecyclePolicy | 作业级事件策略 | 任务没有匹配策略时使用 |
| `plugins` | map[string][]string | 作业插件 | 与调度器插件不是一套；见 4.3 与 33.6 |
| `networkTopology` | object | 拓扑约束（v1.13+） | hard/soft + highestTierAllowed/Name |
| `ttlSecondsAfterFinished` | int32 | 完成后自动清理的秒数 | 0=立即；不设=永久保留 |

### 4.2.2 生命周期策略

用户可在 `policies[].event` 中使用的事件（来自 `bus.volcano.sh/v1alpha1`）：

| Event | 触发时机 |
|---|---|
| `*` | 任意事件（兜底） |
| `PodPending` | Pod 进入 Pending |
| `PodRunning` | Pod 进入 Running |
| `PodFailed` | Pod 失败 |
| `PodEvicted` | Pod 被驱逐 |
| `Unknown` | 作业状态不可判定（例如部分 Pod 长期无法调度） |
| `TaskCompleted` | 某个 task 的副本全部成功 |
| `TaskFailed` | task 非预期结束 |

`OutOfSync`/`CommandIssued`/`JobUpdated` 是内部事件，不应写进策略。

`action` 可取（v1.16 的 `policyActionMap` 允许值）：

| Action | 语义 |
|---|---|
| `AbortJob` | 中止作业（进入 Aborting → Aborted） |
| `RestartJob` | 重启整个作业（所有 Pod 重建） |
| `RestartTask` | 只重启当前 task 的失败 Pod |
| `RestartPod` | 只重启当前 Pod |
| `RestartPartition` | 重启当前 partition |
| `TerminateJob` | 终止整个作业（进入 Terminating → Terminated） |
| `CompleteJob` | 标记作业完成 |
| `ResumeJob` | 恢复被中止的作业 |

典型组合：

```yaml
policies:
  - event: PodEvicted
    action: RestartJob
  - event: PodFailed
    action: RestartJob
```

分布式训练通常选择 `RestartJob`：一个 worker 失败后，其他 worker 的梯度已经不一致，继续跑没有意义，全部重来更安全。

推理/离线批处理可能选择 `RestartTask`：单个副本失败不影响其他副本。

### 4.2.3 与 Kubernetes restartPolicy 的区别

- `restartPolicy: OnFailure` 是 **Pod 内容器**级别的重启；
- `policies` 是 **Volcano Job** 级别的重建/终止决策；
- `maxRetry` 限制的是作业级重启次数。

三者叠在一起时，排障要分清是哪一层在动作。

### 4.2.4 任务级门槛：minAvailable、dependsOn 与 maxRetry

作业级 `minAvailable` 只回答“整个作业至少起几个”。真实训练作业往往还有角色约束：参数服务器（ps）必须恰好 1 个，worker 可以先起 4 个、再扩到 8 个。Volcano 用 **task 级 `minAvailable`** 表达这种约束，并在内部转成 **PodGroup 的 `minTaskMember`**。

数据流如下：

```text
Job.spec.tasks[i].minAvailable
   │ webhook 默认值：nil → replicas
   │（PartitionPolicy.MinPartitions > 0 时 → MinPartitions × PartitionSize）
   ▼
Job controller 写入 PodGroup.spec.minTaskMember[task] = n
   ▼
gang 插件在 JobValid / JobReady / JobPipelined 中按角色检查
```

一个可直接提交的例子见 `examples/jobs/vcjob-task-gang.yaml`：

```yaml
spec:
  minAvailable: 5          # 1 (master) + 4 (worker)
  tasks:
    - name: master
      replicas: 1
      minAvailable: 1
    - name: worker
      replicas: 8
      minAvailable: 4
```

它允许 9 个 Pod 中先启动 5 个（1 个 master + 4 个 worker），而不是等到 8 个 worker 全齐。控制器生成的 PodGroup 会包含：

```yaml
spec:
  minMember: 5
  minTaskMember:
    master: 1
    worker: 4
```

**最重要的边界：角色门槛只在作业门槛不低于角色门槛之和时生效。**

```text
job.minAvailable >= Σ task.minAvailable   → 逐角色检查
job.minAvailable <  Σ task.minAvailable   → 只检查总量，角色门槛被跳过
```

这是上游 `CheckTaskValid/CheckTaskReady/CheckTaskPipelined` 的实际逻辑。上面例子把 `minAvailable` 改成 4，就会退化成“任意 4 个 Pod 就绪即可”，可能出现 1 个 master + 3 个 worker 的布局：worker 不足 4 个，却已经判定 gang 满足。配置时把作业门槛写成角色门槛之和（或更大），否则逐角色约束是静默失效的。

手动创建 PodGroup 调度裸 Pod 时，也可以直接写 `spec.minTaskMember`；但要保证 `spec.minMember >= Σ minTaskMember`，且 Pod 上有正确的 `volcano.sh/task-spec` 标签（或同名注解），否则角色检查会被跳过或计数为 0。

#### dependsOn：任务之间的 DAG

`tasks[].dependsOn` 控制 **Pod 何时被创建**，不是调度器的 gang 约束：

- `name` 列出上游 task；
- `iteration: all`（默认语义）要求所有上游 task 都达到门槛；
- `iteration: any` 在列出多个上游时，任意一个达到门槛即可；
- “达到门槛”指：上游 Pod `Succeeded`，或 `Running` 且所有容器 `Ready`，数量不少于上游 task 的 `minAvailable`。

```yaml
spec:
  minAvailable: 1          # 关键：不要包含尚未创建的下游副本
  tasks:
    - name: prep
      replicas: 1
    - name: train
      replicas: 8
      dependsOn:
        name: ["prep"]
        iteration: all
```

**dependsOn 与严格 gang 叠加会死锁。** 下游 Pod 在上游 Ready 前不会被创建；而 gang 的 `JobValid`/`ValidTaskNum` 要求 `minAvailable` 个有效 Pod。如果 `minAvailable` 包含下游副本，调度器看到的 Pod 数永远不够，作业不会 Inqueue，上游也就永远跑不起来。规则：

- 用 dependsOn 时，把 `job.minAvailable` 设成**第一阶段就能满足**的值（如 1），或关闭 gang；
- 需要“阶段之间都 gang”的编排，用 JobFlow（每个 Job 各自 gang），不要在一个 Job 里用 dependsOn 表达。

webhook 会对依赖图做拓扑排序，环会被拒绝，错误信息包含 `doesn't form a directed acyclic graph(DAG)`。依赖只影响创建时机：下游 Pod 还没创建时，调度器看不到它，也不会为它做 gang 预留。

#### task 级 maxRetry 与 job 级 maxRetry

两个 `maxRetry` 不是一回事：

| 层级 | 统计对象 | 默认 | 触发后的行为 |
|---|---|---|---|
| `tasks[].maxRetry` | 该 task 各 Pod 内容器的 `RestartCount` 之和 | 3（webhook 注入） | 达到上限后该 task 被视为失败 |
| `spec.maxRetry` | 作业级重建次数 | 3（CRD 默认） | 达到上限后 Job `Failed` |

task 级 `maxRetry` 约束的是 **kubelet 重启容器**的次数；job 级 `maxRetry` 约束的是 **Volcano 重建 Pod/作业**的次数。排障时先看 Pod 的 `restartCount`，再看 Job 的 `status.retryCount`，不要混为一谈。`maxRetry: -1` 在 task 级表示不限制。

#### minSuccess：独立的成功副本门槛

`spec.minSuccess` 是**提前完成阈值**，不是对 `minAvailable` 的替代：

- 运行中：`succeeded >= minSuccess` 立即把 Job 置为 `Completed`，并终止仍在运行的副本——即使此时 `succeeded < minAvailable`；
- 如果没有提前触发、所有 Pod 都已终止：成功需要同时满足 `succeeded >= minSuccess` **且** `succeeded >= minAvailable`；只满足前者会 `Failed`。

所以 `minSuccess < minAvailable` 是危险配置：运行中可能在少于 `minAvailable` 时提前完成；若先失败再走到终止，又可能 `Failed`。通常让 `minSuccess >= minAvailable`，或干脆不设。它适合“至少 N 个分片成功就算成功”的批处理；对训练作业通常不设，避免部分 worker 成功就提前结束。

#### volumes：作业级共享卷

`spec.volumes[]` 会被注入到每个 Pod 的业务容器（`spec.containers`，不含 initContainer）：

```yaml
spec:
  volumes:
    - mountPath: /data
      volumeClaimName: training-data
    - mountPath: /checkpoints
      volumeClaim:
        accessModes: ["ReadWriteMany"]
        resources:
          requests:
            storage: 1Ti
```

`volumeClaimName` 引用已存在的 PVC；不存在时 Job 会保持 Pending，并给出 `pvc ... is not found`。只写 `volumeClaim` 时，Job controller 会生成名字、创建 PVC 并回填。多个 task 共享同一个 PVC 时，注意访问模式（`ReadWriteMany` 还是 `ReadWriteOnce`）和挂载点冲突。

#### 排障入口

```bash
# 看控制器是否把角色门槛写进了 PodGroup
# PodGroup 名是 <job>-<uid>，不是 Job 名；先按 owner 找到它
kubectl -n <ns> get podgroup -o custom-columns=NAME:.metadata.name,JOB:.metadata.ownerReferences[0].name | grep <job-name>
kubectl get podgroup <podgroup-name> -o jsonpath='{.spec.minTaskMember}'

# 看某个 task 为什么还没创建
kubectl describe vcjob <job-name> | sed -n '/Events/,$p'

# 看容器重启次数与 task 级 maxRetry
kubectl get pod -l volcano.sh/job-name=<job-name> \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.containerStatuses[*].restartCount}{"\n"}{end}'
```

如果 `minTaskMember` 为空，先确认 webhook 是否正常（默认值由它补齐）；如果 PodGroup 有值但作业仍部分启动，检查 `job.minAvailable` 是否小于角色门槛之和。

## 4.3 作业插件

作业插件在 Job controller 创建 Pod 时介入，负责注入环境变量、创建 Service、生成 SSH 密钥等。它们不是调度器插件。

| 插件 | 作用 | 常见参数 |
|---|---|---|
| `env` | 注入 `VC_TASK_INDEX`、`VK_TASK_INDEX` 等 | 无 |
| `svc` | 创建 headless Service，提供 DNS 发现 | `--publish-not-ready-addresses=true` |
| `ssh` | 生成 RSA key Secret，挂载免密 SSH | 无 |
| `pytorch` | 注入 `MASTER_ADDR`、`RANK`、`WORLD_SIZE` | `--master=master --worker=worker` |
| `tensorflow` | 注入 `TF_CONFIG` | 无 |
| `mpi` | 生成 hostfile/launcher 环境 | 视版本 |
| `ray` | 配置 Ray head/worker | v1.13+ |
| `hcclrank` | 为昇腾 HCCL 分配 rank | v1.13+ |

示例：

```yaml
plugins:
  ssh: []
  svc: []
  pytorch: ["--master=master", "--worker=worker"]
```

### 4.3.1 PyTorch 插件做了什么

以 master/worker 两个 task 为例：

- `svc` 创建 headless Service，master Pod 有稳定 DNS；
- `pytorch` 把 master 地址写入 `MASTER_ADDR`，按 task/序号写入 `RANK`、`WORLD_SIZE`；
- `ssh` 让 Pod 之间可以免密登录（MPI/自定义 launcher 需要）。

如果你的训练脚本自己解析环境变量，确认变量名与插件版本一致。不同版本的 PyTorch 插件参数可能变化。

### 4.3.2 作业插件与调度器插件的混淆

| | 作业插件 | 调度器插件 |
|---|---|---|
| 配置位置 | `Job.spec.plugins` | `volcano-scheduler-configmap` 的 tiers |
| 例子 | `ssh`、`svc`、`pytorch` | `gang`、`drf`、`proportion` |
| 作用阶段 | Pod 创建/注入 | 调度决策 |
| 失败表现 | Pod 起不来/环境变量缺失 | 作业 Pending/不公平 |

把 `pytorch` 写进调度器 tiers，或把 `gang` 写进 `Job.spec.plugins`，都不会按预期工作。

## 4.4 作业状态与条件

Job 的 `status` 包含：

- `state.phase`：Pending/Aborting/Aborted/Running/Restarting/Completing/Completed/Terminating/Terminated/Failed；`Inqueue` 是 PodGroup 的 phase，不是 Job phase；
- `pending/running/succeeded/failed/terminating/unknown`：各阶段 Pod 计数；
- `taskStatusCount`：按 task 分组的 Pod 状态计数；
- `retryCount`：重试次数；
- `conditions`：状态迁移的原因与时间；
- `minAvailable`、`queue` 等运行期信息。

查看：

```bash
kubectl get vcjob example -o yaml
kubectl describe vcjob example
```

一个常见现象：Job 显示 `Running`，但有 Pod 处于 Pending。这通常是因为 `minAvailable < 总副本数`，作业已经满足 gang 门槛，剩余副本在等资源。这不是故障，而是弹性语义。

## 4.5 一个完整的 PyTorch 例子

`examples/jobs/vcjob-pytorch.yaml` 定义 master×1、worker×2：

- `minAvailable: 3`：三个 Pod 必须一起启动；
- `pytorch` 插件注入分布式环境；
- `svc` 提供 DNS；
- `ssh` 提供免密；
- `PodEvicted -> RestartJob`：被抢占后整体重启，避免部分 worker 继续跑。

提交后：

```bash
kubectl apply -f examples/jobs/vcjob-pytorch.yaml
kubectl get vcjob pytorch-dist -w
kubectl get pods -l volcano.sh/job-name=pytorch-dist -o wide
kubectl logs pytorch-dist-master-0 | head
```

如果 master 起来了但 worker 一直 Pending，先看 Queue 的 capability 是否够 3 张卡，再看节点 GPU 是否被其他队列占用。

更大规模的 LLM 预训练/微调作业、并行策略与世界大小、checkpoint/弹性、推理与 Agent 的作业形状，见第 34 章；本节只讲 Volcano Job 的通用字段。

## 4.6 作业完成与失败

### 4.6.1 正常完成

所有 Pod 成功退出后，Job 进入 `Completed`。如果配置了 `TaskCompleted -> CompleteJob`，某个 task 完成即可提前完成整个作业（适合“参数服务器 + 提前停止”的场景）。

### 4.6.2 失败

- Pod 失败且策略为 `RestartJob`：`retryCount` 增加，所有 Pod 重建；超过 `maxRetry` 后 Job `Failed`。
- 策略为 `AbortJob/TerminateJob`：立即终止。
- 策略为 `RestartTask`：只重建失败 task 的 Pod。

### 4.6.3 驱逐

Pod 被抢占/回收时产生 `PodEvicted` 事件。默认行为取决于策略；分布式训练建议显式配置 `RestartJob`，否则可能出现“部分 worker 被驱逐、其余 worker 空转”。

### 4.6.4 TTL 清理

`ttlSecondsAfterFinished` 让 Job 在完成/失败后自动被 GC：

```yaml
spec:
  ttlSecondsAfterFinished: 600   # 完成 10 分钟后清理
```

语义与坑：

- 不设置 = 永久保留；设为 0 = 完成后立即清理（适合一次性任务）；
- 清理的是 Job 及其 Pod/PodGroup，排障期间不要设太小；
- 与 `jobRetainPolicy`（JobFlow）不是一回事：后者控制 JobFlow 的子 Job 是否保留；
- 生产建议保留 1–7 天，配合日志/指标归档，避免“作业删了，问题无法复盘”。

### 4.6.5 作业完成与失败的证据链

排障时不要只看 `status.phase`，按下面顺序取证据：

```text
1. vcjob.status.conditions：最近一次状态迁移的 reason/message
2. podgroup.status.phase：调度器视角（Inqueue/Running）
3. pod.status.phase + nodeName：是否绑定、是否 Running
4. events：Scheduled/Evicted/RestartJob/Completed
5. scheduler/controller 日志：具体拒绝原因
```

如果 Job 是 `Failed` 但 Pod 都是 `Succeeded`，检查 `TaskCompleted` 策略是否被误触发；如果 Job 是 `Running` 但 Pod 全 Pending，检查 `minAvailable` 是否小于总副本（弹性语义）。

## 4.7 常见陷阱

1. **`minAvailable` 大于总副本数**：admission 拒绝。
2. **`minAvailable` 等于总副本数但集群永远凑不齐**：作业永远 Pending，且会占住队列配额。
3. **没有配置 `PodEvicted` 策略**：被抢占后作业可能卡在部分运行。
4. **task 名重复**：Job 校验失败。
5. **容器没有 requests**：调度器按 0 资源处理，可能被过度放置。
6. **只写 limits 不写 requests**：Kubernetes 会把 requests 默认成 limits（对 CPU/内存），但扩展资源必须显式。
7. **Pod 模板与 Job 层的 `schedulerName` 不一致**：模板非空时以模板为准，Job 层只在模板为空时注入；两层都写 `volcano` 最稳。
8. **Job 名过长**：Pod 名是 `<job>-<task>-<index>`，可能超过 63 字符。

## 4.8 MiniVolcano 的对应实现

MiniVolcano 的 `api.Job` 保留了：

- `tasks`、`replicas`、`requests`、`duration`；
- `minAvailable`（含 `MinAvailableSet` 区分“显式 0”与“未设置”）；
- task 级 `minAvailable`（`EffectiveMinAvailable`）与由它生成的 `PodGroup.MinTaskMember`；
- `minSuccess`（提前完成阈值）；
- `maxRetry`、`policies`、`queue`、`priority`；
- 作业插件没有模拟（svc/ssh/pytorch 是控制面注入，超出调度器范围）。

MiniVolcano 的 Job controller 实现了：

- Pod 物化（所有 task 一次性创建；没有 `dependsOn`）；
- `PodFailed/PodEvicted/TaskCompleted` 策略；
- `RestartTask/RestartJob/AbortJob/TerminateJob/CompleteJob`；
- `minSuccess` 提前完成，并把尚未启动的副本标记为终止；
- `minTaskMember` 的角色就绪判定参与 Job 的 Running 状态；
- PodGroup/Queue 状态回写。

这里有一个**刻意的语义差异**：Mini 的 `minAvailable: 0` 表示“关闭 gang”（教学扩展），上游 webhook 会把显式 0 也补成 Σ task 有效 minima，无法用 0 关闭 gang。角色门槛的生效条件则与上游一致：`job.minAvailable < Σ task.minAvailable` 时，角色检查被跳过；示例见 4.2.4。

第 22 章会逐段走读这些代码。

## 4.9 Pod 的命名、标签、owner 与清理

Volcano Job 最终要落到一组 Pod 上。理解这些 Pod 的“身份信息”，是做日志检索、故障定位和清理策略的前提。

### 4.9.1 命名规则

Pod 名通常是：

```text
<job-name>-<task-name>-<index>
```

例如 `pytorch-dist-worker-2`。这意味着：

- task 名必须在 Job 内唯一，否则 Pod 名冲突；
- 名字长度受 Kubernetes 63 字符限制，长 Job 名 + 长 task 名会创建失败；
- index 从 0 开始，重启后通常复用同一个名字，而不是生成随机后缀。

如果 Job 名接近上限，优先缩短 task 名，或在平台侧对 Job 名做长度校验。

### 4.9.2 标签与注解

Volcano 会给 Pod 打上一组用于关联的标签/注解（具体键以版本为准，常见的是）：

| 键 | 作用 |
|---|---|
| `volcano.sh/job-name` | 按 Job 查 Pod |
| `volcano.sh/task-spec` | 任务名/角色（调度器用它做 `minTaskMember` 归组） |
| `volcano.sh/task-index` | 任务内副本序号 |
| `volcano.sh/job-namespace` | 跨 namespace 关联时使用 |
| `volcano.sh/queue-name` | 队列归属 |
| `volcano.sh/job-forwarding` | 多集群转发标记（`extendClusters`） |
| `scheduling.k8s.io/group-name`（注解） | 指向 PodGroup，调度器据此归组 |

常用检索：

```bash
kubectl get pods -l volcano.sh/job-name=pytorch-dist -o wide
kubectl logs -l volcano.sh/job-name=pytorch-dist,volcano.sh/task-spec=worker --tail=100
kubectl get pod pytorch-dist-worker-0 -o jsonpath='{.metadata.labels}'
```

注意：标签是控制器写入的，用户不要手工改；改了之后 Pod 可能脱离 Job 的管理，或被 PodGroup 统计漏掉。

### 4.9.3 ownerReference 与级联删除

Job controller 创建 Pod 和 PodGroup 时，会设置 `ownerReferences` 指向 Job。因此：

- 删除 Job 时，Kubernetes GC 会级联删除 Pod/PodGroup；
- 直接删除 Pod，Job 会按策略重建（取决于 `policies` 和 `maxRetry`）；
- 直接删除 PodGroup 是危险操作：Job 仍在，但调度器看不到组，Pod 可能永久 Pending。

清理顺序应该是“先 Job，后残留对象”，不要反向操作。

### 4.9.4 完成后的保留与清理

| 机制 | 作用范围 | 注意 |
|---|---|---|
| `ttlSecondsAfterFinished` | Job 及其子资源 | 0 = 立即清理；不设 = 永久保留 |
| `jobRetainPolicy`（JobFlow） | JobFlow 的子 Job | 与 TTL 是两套逻辑 |
| 手工 `kubectl delete vcjob` | Job 及 owner 链 | 不会自动删除外部 PVC/Secret |
| 作业插件创建的 Secret/Service | 由插件决定 | 升级/卸载时要单独审计 |

生产建议：

- 训练 Job 至少保留到日志/指标归档完成；
- 失败 Job 的保留时间长于成功 Job，便于复盘；
- 对 Job 插件创建的 SSH Secret、Service 做定期清理；
- 不要把 `ttlSecondsAfterFinished` 设成 0 之后再去查“为什么日志没了”。

## 4.10 本章小结

- Job 是用户对象，PodGroup 是调度对象；`minAvailable` 决定 gang 语义。
- task 级 `minAvailable` 转成 `minTaskMember`，只有 `job.minAvailable ≥ Σ task.minAvailable` 时才按角色检查；`dependsOn` 只控制 Pod 创建时机。
- `minSuccess` 是独立的成功副本门槛，`volumes` 是作业级共享卷。
- `policies` 管作业生命周期，`plugins` 管 Pod 注入，二者与调度器插件不同。
- 分布式训练通常用 `RestartJob` + `maxRetry`。
- 大部分“作业不跑”的问题，都能通过 Job/PodGroup/Pod 三层状态定位。

下一章进入 Queue：Volcano 资源治理的核心。
