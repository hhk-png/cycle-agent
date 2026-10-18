# 第 10 章 RayJob：提交模式、生命周期与清理

RayJob 把“建 Ray 集群 + 提交作业 + 清理资源”封装成一个 CR。它适合训练、批推理、数据预处理这类一次性任务。

## 10.1 一个最小 RayJob

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: hello-rayjob
spec:
  entrypoint: python -c "import ray; print(ray.__version__)"
  submissionMode: K8sJobMode
  shutdownAfterJobFinishes: true
  ttlSecondsAfterFinished: 300
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
        replicas: 1
        minReplicas: 0
        maxReplicas: 4
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

生命周期：

```text
New -> Initializing -> Running -> Complete/Failed
                         |            |
                         +-> Retrying +-> Suspended
```

## 10.2 四种 SubmissionMode

| 模式 | 谁提交作业 | 网络要求 | 适用场景 |
|---|---|---|---|
| `K8sJobMode` | operator 创建 Kubernetes Job，Job 的 Pod 运行 `ray job submit` | submitter Pod 能访问 Dashboard | 默认；隔离 submitter；可重试 |
| `HTTPMode` | operator 直接调用 Dashboard HTTP API | operator 能访问 Dashboard | 网络简单、无 K8s Job 开销 |
| `SidecarMode` | head Pod 里的 sidecar 提交 | 不需要外部客户端 | 网络隔离严格；避免 operator→head 连通性 |
| `InteractiveMode` | 用户自己提交，operator 只等待 `spec.jobId` | 用户负责 | Notebook/交互式调试 |

四种模式的逐步时序（谁发 HTTP、谁轮询、卡住时看哪里）见[附录 J.2–J.5](appendix-j-sequence-atlas.md)。

### 10.2.1 K8sJobMode

- 创建 `<rayjob>-submitter` Kubernetes Job；
- Job 的 Pod 镜像默认与 Ray 镜像一致；
- 命令大致是 `ray job submit --address=http://<dashboard> --submission-id=<jobId> -- <entrypoint>`；
- `submitterConfig.backoffLimit` 控制 submitter 重试；
- submitter Job 通常保留用于日志/审计，随 RayJob 删除或清理策略回收；不要假设作业一结束它就消失（不同版本/策略可能不同）。

优点：

- submitter 与 head 隔离；
- 可以利用 Kubernetes Job 的重试/超时；
- 适合生产批任务。

缺点：

- 需要 submitter Pod 能解析/访问 head Dashboard；
- 多一个 Pod 的启动开销。

### 10.2.2 HTTPMode

operator 直接 POST 到 Dashboard 的 `/api/jobs/`：

- 不需要 submitter Pod；
- 依赖 operator 与 head 的网络连通；
- 失败重试由 operator 控制；
- 适合 operator 与 Ray 集群同网络的环境。

### 10.2.3 SidecarMode（v1.5+）

operator 在 head Pod 注入 `ray-job-submitter` sidecar：

- sidecar 在 head 本地提交，天然能访问 Dashboard；
- 不需要 operator→head 的网络；
- 不支持 `clusterSelector`、`submitterPodTemplate`、`submitterConfig`；
- head 的 restartPolicy 必须是 `Never`；
- v1.7 的 `SidecarSubmitterRestart` gate 支持容器级重启（K8s 1.35+，Ray 2.54+）。

`SidecarSubmitterRestart` 的细节值得单独记住，因为它是“容器级重启策略”这个 Kubernetes 新原语在 KubeRay 里的第一个落地点：

- submitter 容器设置 `restartPolicy: OnFailure`，与 head Pod 的 `restartPolicy: Never` 相互独立；
- submitter 非零退出时只重启这一个容器，`ray-head` 不重启、正在运行的 Ray job 不受影响；
- 重启后先查询 Ray job 状态：job 还在跑就重新 attach 日志流，而不是重复提交（避免副作用执行两次）；
- 重启次数受 `submitterConfig.backoffLimit`（默认 2）约束，SidecarMode 下用户不能覆盖；
- **head Pod 所在的 kubelet 也必须是 1.35+**；控制面 1.35、节点 1.34 时字段会被静默忽略，RayJob 可能在 submitter 退出 30 秒后被误判为 `Failed`；
- entrypoint 自身的非零退出不会触发容器重启，它仍然走 RayJob 的失败路径。

版本偏差的排查与更多容器级重启语义见[第 58.3 节](58-kubernetes-primitives.md)。

### 10.2.4 InteractiveMode

- operator 不提交；
- 用户用 `ray job submit` 或 Dashboard 提交；
- 用户把 job ID 写进 `spec.jobId`；
- operator 等待并跟踪状态。

适合：

- Jupyter/Notebook；
- 调试；
- 需要人工控制提交时机。

## 10.3 关键字段

| 字段 | 说明 |
|---|---|
| `entrypoint` | 要执行的命令 |
| `runtimeEnvYAML` | Ray runtime env（pip、env_vars、working_dir 等） |
| `jobId` | 指定 job ID；不填则自动生成 |
| `metadata` | 传给作业的键值 |
| `rayClusterSpec` | 集群模板 |
| `clusterSelector` | 复用已有 RayCluster |
| `submissionMode` | 四种模式 |
| `submitterPodTemplate` | 自定义 submitter Pod |
| `submitterConfig.backoffLimit` | submitter 重试次数（默认 2；与 RayJob 级 `backoffLimit` 不同） |
| `shutdownAfterJobFinishes` | 完成后立即删除 RayCluster |
| `ttlSecondsAfterFinished` | `shutdownAfterJobFinishes` 的延迟：`shutdownTime = endTime + TTL`。在 v1.7 默认配置下它删的是 RayCluster + submitter，不是 RayJob CR；RayJob CR 的删除由 `DELETE_RAYJOB_CR_AFTER_JOB_FINISHES`（operator 环境变量，默认 false）或 `deletionStrategy` 控制 |
| `activeDeadlineSeconds` | 总运行时限 |
| `preRunningDeadlineSeconds` | 从初始化到 Running 的时限 |
| `backoffLimit` | 失败后重试次数（每次新建集群） |
| `suspend` | 暂停/恢复 |
| `deletionStrategy` | 细粒度清理规则 |
| `entrypointResources` | JSON 字符串，为 entrypoint 预留自定义资源，如 `'{"Custom1": 2}'` |
| `entrypointNumCpus` / `entrypointNumGpus` | 为 entrypoint 预留 CPU/GPU |
| `managedBy` | 交给 Kueue |

## 10.4 状态机

### 10.4.1 JobStatus

| 值 | 含义 |
|---|---|
| 空 | 尚未提交 |
| `PENDING` | 已提交，等待资源 |
| `RUNNING` | 运行中 |
| `SUCCEEDED` | 成功 |
| `FAILED` | 失败 |
| `STOPPED` | 被停止 |

### 10.4.2 JobDeploymentStatus

| 值 | 含义 |
|---|---|
| 空 | New |
| `Initializing` | 创建集群/等待就绪（内部还会区分等 Dashboard、等 submitter 等阶段，但不作为对外枚举） |
| `Running` | 作业已提交 |
| `Waiting` | InteractiveMode 等待 jobId |
| `Complete` | 完成 |
| `Failed` | 失败 |
| `ValidationFailed` | spec 非法 |
| `Suspending` / `Suspended` | 暂停中/已暂停 |
| `Retrying` | 准备重试 |

排查“卡在 Initializing”时，`jobDeploymentStatus` 只能告诉你还在初始化；具体停在哪一步要看 **Events、RayCluster status、submitter Job/Pod 状态和 operator 日志**。不要凭旧版本文档里的内部状态名（例如 `WaitForDashboard`）去 grep，它们不是 v1.7.1 的公开枚举。

`JobStatus` 是 Ray 作业状态，`JobDeploymentStatus` 是 KubeRay 的部署状态。两者不要混淆。

## 10.5 失败、重试与超时

### 10.5.1 backoffLimit

- 失败后，若 `failed <= backoffLimit`，进入 `Retrying`；
- 删除旧集群/submitter，重新初始化；
- 每次重试创建新 RayCluster；
- `DeadlineExceeded` / `JobStatusCheckTimeoutExceeded` 不重试。

### 10.5.2 activeDeadlineSeconds

从 `status.startTime` 开始计时；超时：

- 停止 Ray job；
- `JobDeploymentStatus=Failed`；
- `reason=DeadlineExceeded`；
- 不重试。

注意：超时路径可能留下 `status.jobStatus` 不是 `FAILED` 的状态（例如作业从未真正提交/停止）。如果 deletion rules 只匹配 `jobStatus: FAILED`，RayCluster 可能不会被清理（上游 issue #5184）。建议同时加一条 `jobDeploymentStatus: Failed` 的兜底规则：

```yaml
deletionStrategy:
  deletionRules:
    - policy: DeleteCluster
      condition:
        jobStatus: FAILED
    - policy: DeleteCluster
      condition:
        jobDeploymentStatus: Failed
```

另外，`DeleteWorkers` 在 `enableInTreeAutoscaling: true` 时会被拒绝（autoscaler 拥有 worker 副本），需要改用 `DeleteCluster` 或先关 autoscaling。

### 10.5.3 preRunningDeadlineSeconds

从初始化开始到 Running 的时限；用于清理卡在 Initializing/Waiting 的作业。

### 10.5.4 submitter 重试与 RayJob 重试的区别

| 层 | 字段 | 行为 |
|---|---|---|
| submitter Job | `submitterConfig.backoffLimit`（默认 2） | Kubernetes Job 重启 submitter Pod，作业 ID 不变 |
| RayJob | `backoffLimit`（默认 0） | 失败后删除旧集群，创建新集群重跑 entrypoint |
| Kubernetes Job | `spec.backoffLimit` | submitter Pod 的失败重试 |

不要用 `backoffLimit` 同时表达两层重试；否则一次网络抖动可能触发多次新集群创建。

### 10.5.5 submitter 失败

K8sJobMode/SidecarMode 下，如果 submitter 完成但 Ray job 不存在：

- `Failed`；
- `reason=AppFailed`；
- `message=Submitter completed but Ray job not found in RayCluster`。

## 10.6 清理策略

### 10.6.1 旧式字段

```yaml
spec:
  shutdownAfterJobFinishes: true
  ttlSecondsAfterFinished: 600
```

- `shutdownAfterJobFinishes=true`：作业进入终态后删除 RayCluster/资源；
- `ttlSecondsAfterFinished>0`：不是“删除 RayJob CR”的 TTL，而是把 shutdown 推迟到 `endTime + TTL`；`0` 表示终态后立即 shutdown；
- TTL 必须与 shutdown 同时使用（校验会拒绝单独设置 TTL）；
- **RayJob CR 本身**是否在完成后删除，由 operator 的环境配置（如 `DELETE_RAYJOB_CR_AFTER_JOB_FINISHES`）控制，不由这个字段控制。

> 这是最容易误解的一组字段。记住：**`ttlSecondsAfterFinished` 延迟的是集群/资源清理，不是 RayJob CR 的生命周期**。需要保留 RayJob 记录做审计时，除了 TTL，还要确认 operator 的 CR 删除策略。

### 10.6.2 DeletionStrategy（v1.5 alpha，v1.6 beta 默认开启）

规则模式：

```yaml
spec:
  deletionStrategy:
    deletionRules:
      - policy: DeleteWorkers
        condition:
          jobStatus: FAILED
          ttlSeconds: 100
      - policy: DeleteCluster
        condition:
          jobStatus: FAILED
          ttlSeconds: 600
      - policy: DeleteCluster
        condition:
          jobStatus: SUCCEEDED
          ttlSeconds: 0
      - policy: DeleteSelf
        condition:
          jobDeploymentStatus: Failed
          ttlSeconds: 3600
```

策略：

| 策略 | 行为 |
|---|---|
| `DeleteCluster` | 删除 RayCluster |
| `DeleteWorkers` | 只删除 worker，保留 head 便于调试 |
| `DeleteSelf` | 删除 RayJob（及其子资源） |
| `DeleteNone` | 不删除 |

规则模式与 `shutdownAfterJobFinishes`、legacy `onSuccess/onFailure` 互斥。TTL 从进入对应终态开始计时。

> `deletionStrategy.onSuccess`/`onFailure` 是旧式字段，v1 API 为兼容保留，但新配置应优先使用 `deletionRules`；规则模式表达力更强，且与 `shutdownAfterJobFinishes` 的互斥关系更清晰。

### 10.6.3 Kueue

`managedBy: kueue.x-k8s.io/multikueue` 时，KubeRay 不调和，由 Kueue 管理跨集群调度。

## 10.7 复用已有集群：clusterSelector

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: reuse-job
spec:
  entrypoint: python batch.py
  clusterSelector:
    ray.io/cluster: shared-cluster
  submissionMode: HTTPMode
```

规则：

- `ray.io/cluster` 必须存在；
- 不能用 `suspend`；
- 不能用 `SidecarMode`；
- `backoffLimit` 必须为 0；v1.7.1 的 `ValidateRayJobSpec` 会直接拒绝“`clusterSelector` + 正数 `backoffLimit`”的组合（上游 issue #4516 已修复）。仍建议用 admission policy 在 CI/入口层提前拦截；
- 不能同时设置 `rayClusterSpec`（二选一）；
- `deletionStrategy` 不能包含 `DeleteCluster`/`DeleteWorkers` 规则——共享集群不属于这个 RayJob；`DeleteSelf`/`DeleteNone` 仍可用；
- 不要设置 `shutdownAfterJobFinishes`/`ttlSecondsAfterFinished` 去删除共享集群；
- 作业结束后不删除共享集群。

适合：

- 多作业共享一个长期 RayCluster；
- 避免每个作业都拉镜像/建集群；
- 需要自己管理集群容量和隔离。

## 10.8 runtimeEnv

```yaml
spec:
  runtimeEnvYAML: |
    pip:
      - requests==2.32.3
      - pandas==2.2.3
    env_vars:
      DATA_PATH: s3://bucket/data
    working_dir: s3://bucket/code.zip
```

注意：

- runtime env 在每个节点上安装/下载，启动慢；
- 生产建议预装依赖到镜像；
- `working_dir` 大文件会拖慢启动；
- 敏感信息用 Secret/环境变量注入，不要写进 YAML。

## 10.9 日志与调试

```bash
kubectl get rayjob hello-rayjob -o yaml
kubectl describe rayjob hello-rayjob
kubectl logs job/hello-rayjob-submitter
kubectl exec <head> -- ray job logs <jobId>
```

状态字段：

- `status.jobId`
- `status.rayClusterName`
- `status.dashboardURL`
- `status.rayJobInfo.startTime/endTime`
- `status.succeeded/failed`
- `status.reason/message`

## 10.10 生产建议

- 明确 `activeDeadlineSeconds` 和 `preRunningDeadlineSeconds`；
- 默认 `shutdownAfterJobFinishes=true`，除非需要保留集群调试；
- 用 deletion rules 区分成功/失败保留时间；
- 失败重试要幂等（entrypoint 可重入）；
- 用 `clusterSelector` 复用集群时，自己做队列/配额；
- submitter 镜像与 Ray 镜像版本一致；
- 监控 `kuberay_job_deployment_status` / `kuberay_job_info`。

## 10.11 MiniKubeRay 的实现

`minikuberay/controllers/rayjob.py`：

- 状态机：New/Initializing/Waiting/Running/Complete/Failed/Suspending/Suspended/Retrying；
- 四种 submission mode（submitter Job/Pod 与 env 会被建模，失败会置 `SubmissionFailed`；但真正的提交仍由模拟 Runtime 完成，不执行 `ray job submit`）；
- `backoffLimit`、deadline、TTL、deletion rules；
- clusterSelector；
- submitter Job/Pod；
- `entrypointNumCpus`/`entrypointNumGpus`/`entrypointResources` 的校验与转发（写入模拟作业的 metadata，对应 Ray Job API 的 `entrypoint_num_cpus` 等字段）；
- finalizer 停止作业；
- status/conditions。

测试：`tests/test_rayjob.py` 覆盖成功、失败、重试、超时、挂起、TTL、规则清理、四种模式、selector、finalizer。

### 10.11.1 实验：一次跑完四种提交模式

`examples/rayjob_modes.py` 为每种模式创建一个独立 RayJob 和 RayCluster，用 `ManualClock` 推进到终态，并打印状态迁移：

```bash
python examples/rayjob_modes.py
```

关键输出：

```text
K8sJobMode
  Initializing ...
  Running .../PENDING
  Running .../RUNNING
  Complete .../SUCCEEDED
...
RayClusters=4 submitterJobs=1

OK: all four submission modes reached Complete
```

四个观察点：

- **只有 K8sJobMode 创建了 Kubernetes submitter Job**（`submitterJobs=1`）；
- SidecarMode 的 submitter 在 head Pod 里，不会多一个 Job；
- InteractiveMode 先到 `Waiting`，只有用户把 `spec.jobId` 写回后才进入 `Running`；
- HTTPMode/K8sJobMode/SidecarMode 的作业 ID 由控制器生成，InteractiveMode 的 ID 由用户提供。

把 `--fast` 之类的随机因素去掉正是这个实验的目的：状态机差异应该是确定的、可断言的，而不是靠日志“感觉”。

## 10.12 Ray Job Submission API 速查

四种提交模式最终都落到 Dashboard 的 Job API。理解这几个端点，能自己复现 KubeRay 的行为，也能在 submitter 失败时手工排查。

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/jobs/` | 提交作业 |
| GET | `/api/jobs/` | 列出作业 |
| GET | `/api/jobs/{job_id}` | 作业状态 |
| GET | `/api/jobs/{job_id}/logs` | 作业日志 |
| POST | `/api/jobs/{job_id}/stop` | 停止作业 |
| GET | `/api/jobs/{job_id}/logs?tail=N` | 最近 N 行 |

提交 body 的常见字段：

```json
{
  "entrypoint": "python train.py",
  "submission_id": "train-2026-10-18-001",
  "runtime_env": {
    "working_dir": "s3://bucket/code.zip",
    "env_vars": {"DATA_PATH": "s3://bucket/data"}
  },
  "metadata": {"team": "ml", "run": "42"}
}
```

关键语义：

- **`submission_id` 是幂等键**：重复 POST 同一个 ID 不会创建第二个作业；
- `entrypoint` 是 shell 命令，不是 Python 函数；
- `runtime_env` 在每个节点准备依赖，启动慢；
- 作业状态最终由 driver/JobSupervisor 决定；
- `stop` 停止 driver/actor，不保证外部副作用回滚。

KubeRay 的 `HTTPMode` 直接调用这些端点；`K8sJobMode`/`SidecarMode` 通过 `ray job submit` 间接调用；`InteractiveMode` 让用户自己调用。排查时可以用：

```bash
kubectl port-forward svc/<cluster>-head-svc 8265:8265
curl -s http://127.0.0.1:8265/api/jobs/ | jq
curl -s http://127.0.0.1:8265/api/jobs/<job-id> | jq
curl -s http://127.0.0.1:8265/api/jobs/<job-id>/logs?tail=100
```

> API 字段随 Ray 版本变化；以 `ray job submit --help` 和 Dashboard OpenAPI 为准。不要在生产脚本里依赖未文档化的内部字段。

## 10.13 幂等设计清单

RayJob 的 `backoffLimit` 会创建新集群重跑 entrypoint。只要 entrypoint 不是幂等的，重试就会制造重复副作用。

### 10.13.1 副作用的分类

| 副作用 | 风险 | 做法 |
|---|---|---|
| 写对象存储 | 重复文件/重复行 | 输出到 `run_id` 路径；原子 rename；分区覆盖 |
| 写数据库 | 重复插入 | 唯一键/幂等 upsert；事务 |
| 调用外部 API | 重复扣费/重复任务 | 幂等键；去重表；先查后写 |
| 发消息 | 重复消费 | 消息 ID + 消费端去重 |
| 改模型/配置 | 半成品对外可见 | 版本化路径；先写临时再切换 |
| checkpoint | 覆盖可恢复状态 | 先写临时，再原子替换；保留 N 份 |

### 10.13.2 推荐的 entrypoint 结构

```python
import os
import ray

# RAY_JOB_SUBMISSION_ID 注入在 submitter Pod/容器上，Ray 在运行 entrypoint
# 前会清理内部环境变量，不能假设 entrypoint 里还能读到它。
run_id = os.environ.get("RUN_ID") or ray.get_runtime_context().get_job_id()
output = f"s3://bucket/output/run_id={run_id}"

# 1. 读取输入（幂等）
# 2. 计算
# 3. 写临时路径
# 4. 校验（行数/校验和）
# 5. 原子提交（rename/partition overwrite）
# 6. 写 _SUCCESS 标记
```

### 10.13.3 重试与 checkpoint

- 训练：从最近 checkpoint 恢复，而不是从头开始；
- ETL：按分区/分片幂等重跑；
- Tune：trial 结果按 trial ID 去重；
- 外部提交：用 `submission_id`/业务键去重。

### 10.13.4 不要用“重试”掩盖不确定性

如果失败原因是“结果不确定”（例如请求超时但服务端可能已执行），重试前必须先查询状态，而不是直接再发一次。幂等键的意义就在于此。

## 10.14 小结

- RayJob = 集群模板 + 提交方式 + 清理策略。
- 四种模式解决不同网络/隔离需求；K8sJobMode 是默认，SidecarMode 适合严格网络。
- JobStatus 与 JobDeploymentStatus 是两套状态，不要混用。
- backoffLimit 只对可重试失败生效；deadline 是硬终止。
- deletion rules 比单个 TTL 更灵活，但规则之间互斥/顺序要看清。
- 复用集群时，容量和隔离由你自己负责。

下一章 RayService：把 Ray Serve 变成可升级的在线服务。

→ [第 11 章 RayService](11-rayservice.md)
