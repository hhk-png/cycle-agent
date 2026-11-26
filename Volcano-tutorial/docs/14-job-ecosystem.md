# 第 14 章 作业生态与插件

## 14.1 三种“作业”层次

Volcano 生态里有三层作业抽象：

1. **Volcano Job**：原生的批作业，带 tasks/minAvailable/policies/plugins。
2. **框架作业**：Ray、PyTorch、TensorFlow、MPI、Spark、LWS 等，由各自 operator 管理。
3. **工作流**：JobFlow/JobTemplate、Argo Workflows、Kubeflow Pipelines 等。

Volcano 不要求所有作业都改写成 Volcano Job。它通过两种方式接收外部作业：

- **PodGroup + schedulerName=volcano**：任何能创建 Pod 的控制器都能接入；
- **框架 operator 的 Volcano 集成**：operator 自动创建 PodGroup 并设置 schedulerName。

## 14.2 JobFlow 与 JobTemplate

### 14.2.1 JobTemplate

JobTemplate 是可复用的 Volcano JobSpec：

```yaml
apiVersion: flow.volcano.sh/v1alpha1
kind: JobTemplate
metadata:
  name: prep
  namespace: default
spec:
  schedulerName: volcano
  queue: team-b
  minAvailable: 1
  tasks:
    - name: prep
      replicas: 1
      template: { ... }
```

它没有自己的调度语义，只是被 JobFlow 引用。

### 14.2.2 JobFlow

```yaml
apiVersion: flow.volcano.sh/v1alpha1
kind: JobFlow
metadata:
  name: ml-pipeline
  namespace: default
spec:
  jobRetainPolicy: retain
  flows:
    - name: prep
    - name: train
      dependsOn:
        targets: ["prep"]
```

语义：

- `flows[].name` 是 DAG 节点名，也是引用的 JobTemplate 名；
- 创建出的 Job 名为 `<jobflow>-<flow>`；
- `dependsOn.targets` 指定上游；
- `jobRetainPolicy: retain|delete` 控制 JobFlow 成功后是否保留 Job；
- 状态：Pending → Running → Succeed/Failed/Terminating。

限制（v1.10–v1.16 仍有讨论）：

- 控制器主要检查上游 Job 是否 Completed；
- DAG 循环校验、模板存在性校验在不同版本有差异；
- `probe` 等高级字段可能未完全实现；`patch` 在较新版本支持（PR #4411），老版本要确认；
- 多个 JobFlow 共享同一 JobTemplate 时，状态聚合要小心。

### 14.2.3 与 Argo/Kubeflow 的区别

JobFlow 是“Job 级 DAG”，不是通用工作流引擎：

- 没有复杂条件分支、循环、人工审批；
- 没有容器级步骤；
- 适合“预处理 → 训练 → 评估”这类线性/小 DAG。

复杂流水线仍然用 Argo/Kubeflow，把 Volcano 当作底层调度器。

### 14.2.4 作业内 task 依赖：比 JobFlow 更轻的 DAG

一个 Volcano Job 内部也可以表达依赖，用 `tasks[].dependsOn`：

```yaml
spec:
  minAvailable: 1        # 不要包含尚未创建的下游副本
  tasks:
    - name: prep
      replicas: 1
    - name: train
      replicas: 8
      dependsOn:
        name: ["prep"]
        iteration: all
```

它和 JobFlow 的边界：

| | task dependsOn | JobFlow |
|---|---|---|
| 作用域 | 单个 Job 内 | 多个 Job 之间 |
| 对象 | `batch.volcano.sh/v1alpha1 Job` | `flow.volcano.sh/v1alpha1 JobFlow` + JobTemplate |
| 依赖条件 | 上游 task 的 Ready 数 ≥ 其 `minAvailable` | 上游 Job 是否 Completed |
| 调度影响 | 只延迟下游 Pod 的创建 | 创建新的 Job |
| 适合 | 预处理 → 训练这类同作业角色链 | 跨作业、跨队列、跨团队的流水线 |

task 依赖与**严格 gang 叠加会死锁**：下游 Pod 在上游 Ready 前不创建，而 gang 的 `JobValid` 要求 `minAvailable` 个有效 Pod；如果 `minAvailable` 包含下游副本，作业永远进不了 Inqueue。用 dependsOn 时把 `minAvailable` 保持在第一阶段可满足的值（如 1），或把依赖拆成多个 Job 用 JobFlow 编排。依赖图的环会被 webhook 拒绝。字段语义与排障见 4.2.4。

### 14.2.5 JobFlow 的 patch 覆盖

较新版本的 JobFlow 允许在每个 flow 上用 `patch` 覆盖引用的 JobTemplate，而不必为每个变体复制一份模板：

```yaml
apiVersion: flow.volcano.sh/v1alpha1
kind: JobFlow
metadata:
  name: ml-pipeline
spec:
  flows:
    - name: prep
    - name: train
      dependsOn:
        targets: ["prep"]
      patch:
        spec:
          queue: team-a
          minAvailable: 4
          tasks:
            - name: train
              replicas: 4
```

语义与坑：

- `patch` 只覆盖显式写出的字段，其余继承 JobTemplate；
- 合并策略按层级（Job、Task、Container）不同，容器通常按 `name` 匹配；
- 不同版本对 `patch` 的支持程度不同（PR #4411 之后逐步完善），升级前先在测试集群验证；
- 覆盖 `queue`/`priorityClassName` 时，要确认租户有权限，否则会被 admission 拒绝；
- `patch` 不会改变 JobTemplate 本身，多个 JobFlow 共享模板时仍然会互相影响状态聚合（Issue #4089）。

如果只是改镜像 tag 或副本数，`patch` 比复制模板更易维护；如果要表达复杂的条件分支，仍然应该用 Argo/Kubeflow。

## 14.3 Cron VolcanoJob

v1.13 引入 `batch.volcano.sh/v1alpha1 CronJob`：

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: CronJob
metadata:
  name: nightly-eval
spec:
  schedule: "0 2 * * *"
  timeZone: Asia/Shanghai
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 300
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      schedulerName: volcano
      queue: team-b
      minAvailable: 1
      tasks:
        - name: eval
          replicas: 1
          template: { ... }
```

与 Kubernetes CronJob 的区别：

- `jobTemplate.spec` 是 **Volcano JobSpec**，不是 Pod template；
- 生成的资源是 `vcjob`，不是 `batch/v1 Job`；
- 并发策略、历史限制语义类似，但字段名以 Volcano CRD 为准。

## 14.4 框架集成

### 14.4.1 Ray

v1.13 起 Volcano 原生支持 Ray：

- Job 里用 `ray` 作业插件；
- 自动配置 head/worker；
- 适合 Ray Train/Ray Serve 的 gang 启动。

KubeRay 也支持把 RayCluster 的 Pod 交给 Volcano 调度（通过 schedulerName + PodGroup）。具体方式取决于 KubeRay 版本。

### 14.4.2 LeaderWorkerSet（LWS）

LWS 用于多机推理/大模型 serving。v1.13 起 LWS v0.7 原生集成 Volcano：

- 自动创建 PodGroup；
- gang 调度；
- 适合同一推理服务的多副本一起起。

### 14.4.3 Kubeflow / Training Operator

Kubeflow Training Operator 的 PyTorchJob/TFJob 可以通过：

- `schedulerName: volcano`；
- 自动/手动创建 PodGroup；

接入 Volcano。不同 operator 版本的字段不同，升级时要验证。

### 14.4.4 Spark

Spark on Kubernetes 支持指定 scheduler：

```properties
spark.kubernetes.scheduler.name=volcano
```

配合 PodGroup 注解，Spark driver/executor 可以获得 gang 调度。注意 Spark 的动态资源分配与 Volcano 的 gang 语义可能冲突（executor 按需创建）。

### 14.4.5 MindSpore / PaddlePaddle / MPI

Volcano 社区长期维护这些框架的示例：

- MindSpore：多 worker + scheduler；
- PaddlePaddle：PS/worker；
- MPI：mpimaster/mpiworker；
- TensorFlow：ps/worker/chief。

它们的共同点是：用 task 表达角色，用作业插件注入环境，用 minAvailable 做 gang。

### 14.4.6 Grove / KubeRay 与多级 gang（v1.16）

v1.16 增强了与 Grove、KubeRay 的多级 gang 集成。这里的“多级”指：

```text
一个推理服务
  ├── 多个副本组（每组是一个 gang）
  │     ├── leader
  │     └── workers
  └── 组之间可能需要拓扑反亲和（group-topology-affinity）
```

与单个 Volcano Job 的 minAvailable 不同，多级 gang 要求“组内 all-or-nothing”和“组间协调”同时成立。实现方式通常是：

- 每个组映射为一个 PodGroup；
- 上层控制器（Grove/KubeRay）负责组间依赖与副本数；
- Volcano 负责每个 PodGroup 的 gang 放置；
- 用 `group-topology-affinity` 把不同组分散到不同故障域。

这类集成仍在快速演进，生产采用前要确认 Grove/KubeRay 与 Volcano 的版本矩阵，以及“组间失败如何传播”的语义。

## 14.5 非 Volcano 工作负载的接入

任何控制器创建的 Pod 都可以接入：

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata:
  name: web-gang
spec:
  minMember: 3
  minResources:
    cpu: "3"
  queue: team-b
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  template:
    metadata:
      annotations:
        scheduling.k8s.io/group-name: web-gang
    spec:
      schedulerName: volcano
```

要点：

- Pod 的 `schedulerName` 必须是 `volcano`；
- Pod 注解 `scheduling.k8s.io/group-name` 指向 PodGroup；
- PodGroup 的 `minMember`/`minResources` 要与工作负载匹配；
- Deployment 的滚动更新会创建新 Pod，gang 语义可能导致更新卡住（新 Pod 等旧 Pod 释放），要评估。

## 14.6 作业插件生态

Volcano Job 的作业插件：

| 插件 | 版本 | 用途 |
|---|---|---|
| `env` | 早期 | 注入 task index 等环境变量 |
| `svc` | 早期 | headless Service + DNS |
| `ssh` | 早期 | 免密 SSH |
| `pytorch` | 早期 | PyTorch 分布式环境 |
| `tensorflow` | 早期 | TF_CONFIG |
| `mpi` | 早期 | MPI hostfile/launcher |
| `ray` | v1.13 | Ray 集群 |
| `hcclrank` | v1.13 | 昇腾 HCCL rank |

插件参数以 Job 的 `spec.plugins` 传入。不同版本的参数可能变化，升级时看 release notes。

## 14.7 生态选择的建议

| 需求 | 建议 |
|---|---|
| 简单批处理 | Volcano Job 或 Kubernetes Job + Volcano gang |
| 分布式训练 | 框架 operator（PyTorch/TF/MindSpore）+ Volcano |
| 多机推理 | LWS + Volcano |
| Ray 生态 | Ray + Volcano 作业插件或 KubeRay |
| 复杂 DAG | Argo/Kubeflow + Volcano 作为底层调度 |
| 定时任务 | Cron VolcanoJob |
| 小规模线性流水线 | JobFlow + JobTemplate |

不要为了“统一”把所有工作负载都塞进 Volcano Job。框架 operator 通常更懂自己的生命周期；Volcano 只需要负责调度。

LLM 预训练、微调、推理与 Agent 的具体作业形状、算子/拓扑约束和容量模型，见第 34 章；本节只讲生态接入方式。

## 14.8 MiniVolcano 的实现

MiniVolcano 模拟了：

- `CronJob`：以 tick 为周期，支持 Allow/Forbid/Replace、历史清理；
- `JobFlow`：步骤内联 JobSpec，按依赖创建，检查上游 Completed；
- 作业插件：未模拟（属于控制面注入，不影响调度语义）。

场景：

- `cron`：每 2 tick 创建一次，历史限制 2；
- `jobflow`：prep → train → eval 顺序执行。

差异：

- 真实 JobFlow 引用 JobTemplate，Mini 内联；
- 真实 CronJob 用 cron 表达式和时区，Mini 用 tick；
- 真实框架 operator 的复杂生命周期未模拟。

## 14.9 Spark 与 Ray 的接入细节

框架集成不是“把 `schedulerName` 改成 volcano”就结束了。Spark 和 Ray 都有自己的资源模型，和 gang 语义会发生冲突。这一节把两个最常见的集成拆开。

### 14.9.1 Spark：动态资源分配与 gang 的冲突

Spark on Kubernetes 的默认行为是：

```text
driver 启动
  → 按需申请 executor
  → 任务完成后释放 executor
  → 动态扩缩
```

Volcano 的 gang 语义是“一组 Pod 同时启动”。两者冲突点：

| 冲突 | 表现 | 缓解 |
|---|---|---|
| executor 按需创建 | PodGroup 的 `minMember` 无法预先确定 | 固定 executor 数，或关闭动态分配 |
| executor 分批到达 | gang 永远凑不齐，driver 空等 | 用 `minAvailable` 小于预期 executor 数，或不用 gang |
| driver 先于 executor | driver 占着资源等 executor | 把 driver 与 executor 放在同一个 PodGroup，或给 driver 单独队列 |
| 动态缩容 | 释放的 executor 让队列账本波动 | 监控 `Shuffled`/`Evicted`，避免和重调度叠加 |

一个可用的起点：

```properties
spark.kubernetes.scheduler.name=volcano
spark.dynamicAllocation.enabled=false
spark.executor.instances=8
```

然后为 driver + executor 创建对应的 PodGroup（或使用 Spark operator 的 Volcano 集成，版本相关），把 `minMember` 设为 9。确认 operator 版本支持 PodGroup 模板后再上生产。

如果必须保留动态分配，建议：

- 不要对 Spark 作业启用严格 gang；
- 用队列配额和优先级治理，而不是 all-or-nothing；
- 把 driver 放到独立队列，避免 executor 波动影响 driver；
- 监控“driver Running 但 executor 长期不足”的作业。

### 14.9.2 KubeRay：RayCluster 接入 Volcano

Ray 的 head/worker 是长期运行的进程组，比 Spark 更适合 gang。接入步骤（字段名以 KubeRay/Volcano 版本为准）：

```text
1. 确认 KubeRay 版本支持自定义 schedulerName
2. 在 headGroupSpec/workerGroupSpec 的 Pod 模板里设置 schedulerName: volcano
3. 为 RayCluster 创建 PodGroup，或用 operator 的 Volcano 集成自动创建
4. 给 head/worker Pod 加 scheduling.k8s.io/group-name 注解（若未自动）
5. 用 minMember 表达“head + N 个 worker 一起启动”
6. 验证 head 的 GCS 地址、worker 的 join 顺序
```

示意（只展示关键字段）：

```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: ray-train
spec:
  headGroupSpec:
    template:
      metadata:
        annotations:
          scheduling.k8s.io/group-name: ray-train
      spec:
        schedulerName: volcano
        containers:
          - name: ray-head
            image: rayproject/ray:2.40.0
  workerGroupSpecs:
    - groupName: workers
      replicas: 4
      template:
        metadata:
          annotations:
            scheduling.k8s.io/group-name: ray-train
        spec:
          schedulerName: volcano
          containers:
            - name: ray-worker
              image: rayproject/ray:2.40.0
```

注意：

- PodGroup 的 `minMember` 要覆盖 head + worker 总数；
- head 必须先就绪，worker 才能 join；gang 只保证“一起绑定”，不保证“启动顺序”；
- Ray 的自动扩缩（autoscaler）会改变 worker 数，和固定 `minMember` 冲突；要么关闭自动扩缩，要么接受“部分 Running”；
- 如果使用 Volcano 的 `ray` 作业插件（Volcano Job 形态），则不需要手写 KubeRay 的 PodGroup；两种形态不要混用。

### 14.9.3 集成矩阵

| 框架 | 推荐接入方式 | gang 适配 | 主要风险 |
|---|---|---|---|
| PyTorch/TF/MindSpore | 框架 operator + PodGroup | 好 | operator 版本字段差异 |
| MPI | Volcano Job + mpi 插件 | 好 | SSH/launcher/共享内存 |
| Ray | KubeRay + PodGroup 或 Volcano Job + ray 插件 | 好 | head/worker join 顺序 |
| Spark | Spark operator + PodGroup | 一般 | 动态分配与 gang 冲突 |
| LWS | LWS + Volcano 集成 | 好 | 多级 gang/组间拓扑 |
| Argo/Kubeflow Pipelines | 作为上层工作流，Pod 用 volcano | 取决于步骤 | 步骤级 gang 与 DAG 的边界 |

一个通用原则：**框架负责生命周期，Volcano 负责放置**。不要让 Volcano 去理解 Spark 的 stage 或 Ray 的 task；也不要让框架去猜节点拓扑。

## 14.10 本章小结

- Volcano 通过 Volcano Job、PodGroup、框架 operator 三种方式接收作业。
- JobFlow/JobTemplate 提供轻量 Job 级 DAG；CronJob 提供定时作业。
- LWS/Ray/Kubeflow/Spark 等通过 schedulerName + PodGroup 或专用插件接入。
- 多级 gang（Grove/KubeRay）把“组内 gang”和“组间协调”分开，是推理平台的重要模式。
- 非 Volcano 工作负载也能用 PodGroup 获得 gang，但要注意滚动更新。
- MiniVolcano 模拟了 Cron 与 JobFlow 的核心语义。

下一章进入控制面：控制器与 Webhook 如何把对象状态推向期望状态。
