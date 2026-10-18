# 第 29 章 生态集成：队列、调度与工作流

KubeRay 负责 Ray 集群的生命周期，但“谁可以先跑”“一组 Pod 能不能一起调度”“训练完怎么触发下游”通常由别的系统回答。本章把常见的四类集成讲清楚：批调度器（gang scheduling/队列）、作业队列（Kueue）、工作流编排（Argo/Airflow/Kubeflow/Flyte）、GitOps 与可观测性。

## 29.1 为什么需要批调度器

Kubernetes 默认调度器是**逐个 Pod** 调度的。对 Ray 来说这有三个问题：

1. **gang scheduling**：一个 Ray 集群需要 head + N 个 worker 同时起来才能工作。如果只调度了部分 worker，剩下的可能永远等不到资源，已调度的资源也被占住。
2. **队列/配额**：多租户下需要“团队 A 最多 32 张 GPU”“团队 B 优先级更高”。
3. **抢占/公平**：高优先级作业可以抢占低优先级资源。

KubeRay 的 Pod 默认由 `kube-scheduler` 调度。要获得 gang scheduling/队列，需要：

- 在 operator 启动参数选择批调度器；
- 或在上层用 Kueue 做准入/配额；
- 或使用 Kubernetes 原生的 Workload API（WAS，alpha）。

## 29.2 Volcano

### 29.2.1 组成

- **Queue**：资源队列，设置 capability；
- **PodGroup**：一组 Pod 的调度单元，`minMember` 是 gang 的最小成员数；
- **Job**：Volcano 自己的批作业（KubeRay 不用它，只用 PodGroup）；
- **Scheduler**：volcano-scheduler。

### 29.2.2 KubeRay 集成

operator 启动参数：

```bash
--batch-scheduler=volcano
```

KubeRay 会：

- 为每个 RayCluster 创建 PodGroup；
- 把 Pod 的 `schedulerName` 设为 `volcano`；
- 从 RayCluster 的标签读取队列名：`volcano.sh/queue-name`（KubeRay 的 Volcano 插件常量 `QueueNameLabelKey`）。

RayCluster 示例：

```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: train
  labels:
    volcano.sh/queue-name: team-a
spec:
  rayVersion: "2.59.0"
  headGroupSpec:
    template:
      spec:
        containers:
          - name: ray
            image: rayproject/ray:2.59.0
            resources:
              requests: {cpu: "4", memory: 16Gi}
  workerGroupSpecs:
    - groupName: gpu
      replicas: 8
      minReplicas: 8
      maxReplicas: 8
      template:
        spec:
          containers:
            - name: ray
              image: rayproject/ray:2.59.0-gpu
              resources:
                requests: {cpu: "8", memory: 64Gi, nvidia.com/gpu: "1"}
```

PodGroup 的 `minMember` 应该覆盖 head + 所有 worker（如果希望整个集群原子调度）。如果只要求 worker gang，可以调整 `minMember`，但要理解 head 先行的语义。

### 29.2.3 队列与配额

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: team-a
spec:
  weight: 1
  capability:
    cpu: "200"
    memory: 800Gi
    nvidia.com/gpu: "32"
```

- `weight` 影响公平共享；
- `capability` 是队列上限；
- 超出配额的 PodGroup 会 Pending；
- 监控 `volcano_queue_*` 指标。

### 29.2.4 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| PodGroup Pending | 队列配额不足/资源碎片 | 看 Queue/PodGroup 事件 |
| 部分 Pod 运行、部分 Pending | `minMember` 太小/调度器未启用 | 检查 PodGroup 与 schedulerName |
| 作业无法抢占 | 优先级/队列权重 | 配置 PriorityClass/Queue |
| 扩容后仍 Pending | 节点池不足 | Cluster Autoscaler/Karpenter |

## 29.3 Apache YuniKorn

YuniKorn 提供层次队列、资源配额、gang scheduling。KubeRay 集成：

```bash
--batch-scheduler=yunikorn
```

- Pod 的 `schedulerName` 设为 `yunikorn`；
- 队列通过标签/注解指定，例如 `yunikorn.apache.org/queue: root.team-a`；
- YuniKorn 使用自己的应用/队列模型，不依赖 Volcano PodGroup。

适合已经有 YuniKorn 集群、需要层次队列和混合负载（Spark/Flink/Ray）的组织。

## 29.4 scheduler-plugins（Coscheduling）

Kubernetes SIG Scheduling 的 scheduler-plugins 提供 coscheduling/gang scheduling：

```bash
--batch-scheduler=scheduler-plugins
```

- 创建 `scheduling.x-k8s.io/v1alpha1 PodGroup`；
- Pod 的 `schedulerName` 设为 `scheduler-plugins-scheduler`；
- 需要部署 scheduler-plugins 的 scheduler 及其配置。

优点：贴近 Kubernetes 原生；缺点：功能/生态不如 Volcano/Kueue 完整。

## 29.5 Kubernetes WAS（Workload-Aware Scheduling）

v1.7 alpha 的 `KubernetesWAS` 使用 in-tree 的 `scheduling.k8s.io` Workload/PodGroup API：

- feature gate `KubernetesWAS`；
- 与 `--batch-scheduler` 互斥；
- 需要集群启用 GenericWorkload；
- 是未来 gang scheduling 标准化的方向。

生产建议：先在测试集群验证 API 可用性和调度器行为，不要把 alpha 作为唯一方案。

## 29.5.1 KAI Scheduler（v1.7.1 新增支持）

KubeRay v1.7.1 通过 `--batch-scheduler=kai-scheduler` 注册 KAI Scheduler 插件。它与其他批调度器的关键差异：

- **插件不做 PodGroup 创建/更新**：KAI Scheduler 自带 PodGrouper，在运行时创建 PodGroup；
- KubeRay 只给子资源打上 `schedulerName=kai-scheduler` 和队列标签；
- 队列标签是 `kai.scheduler/queue`（不是 Volcano 的 `volcano.sh/queue-name`）；
- **不支持 `RayJob` 的 `K8sJobMode`**：submitter Pod 在 RayCluster 就绪后才创建，KAI 的 gang 调度无法覆盖这个两阶段模式；插件会直接返回错误。RayCluster、以及非 K8sJobMode 的 RayJob 可用。

```yaml
metadata:
  labels:
    kai.scheduler/queue: team-a
```

启用前确认：

- KAI Scheduler 已安装，且其 PodGrouper 的配置与队列一致；
- operator 启动参数是 `--batch-scheduler=kai-scheduler`；
- 不要把 KAI 的队列标签和 Volcano/YuniKorn 的标签混用；
- 对 K8sJobMode 的 RayJob，改用 HTTPMode/SidecarMode，或换 Volcano/YuniKorn。

## 29.6 Kueue

Kueue 是 Kubernetes 原生的作业队列/配额系统，和批调度器的定位不同：

- 批调度器决定 **Pod 怎么放**；
- Kueue 决定 **作业什么时候被准入**（quota、队列、优先级、抢占）。

### 29.6.1 核心对象

| 对象 | 作用 |
|---|---|
| `ResourceFlavor` | 节点 flavor（GPU 型号/可用区） |
| `ClusterQueue` | 集群级配额与借用策略 |
| `LocalQueue` | namespace 内的队列入口 |
| `Workload` | Kueue 对作业的抽象 |
| `AdmissionCheck` | 外部准入检查（如 MultiKueue） |

### 29.6.2 RayJob 集成

RayJob 加队列标签：

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: train
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  entrypoint: python train.py
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
```

Kueue 会：

1. 创建 Workload；
2. 根据 ClusterQueue 配额决定是否准入；
3. 准入后允许 RayJob 创建集群；
4. 作业完成后释放配额。

注意两种模式不要混用：

| 模式 | 配置 | 谁调和 |
|---|---|---|
| Kueue 本地准入 | `kueue.x-k8s.io/queue-name` 标签 | KubeRay operator 正常调和 |
| MultiKueue | `spec.managedBy: kueue.x-k8s.io/multikueue` | Kueue 决定在哪个集群运行；KubeRay 跳过 |

本地准入不需要 `managedBy`；MultiKueue 也不应再依赖本地队列标签做准入。

### 29.6.3 RayCluster 与 managedBy

KubeRay 支持把 RayCluster 交给外部控制器：

```yaml
spec:
  managedBy: kueue.x-k8s.io/multikueue
```

此时 KubeRay operator **不调和**该对象；由 Kueue MultiKueue 决定在哪个集群运行。注意：

- `managedBy` 不可变；
- 只支持 `ray.io/kuberay-operator` 和 `kueue.x-k8s.io/multikueue` 两个值；
- 不是所有 KubeRay 功能在 MultiKueue 下都可用；
- 需要 Kueue 的 MultiKueue controller 和对应 CRD。

### 29.6.4 配额与抢占

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: team-a
spec:
  namespaceSelector: {}
  resourceGroups:
    - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
      flavors:
        - name: a100
          resources:
            - name: cpu
              nominalQuota: 200
            - name: memory
              nominalQuota: 800Gi
            - name: nvidia.com/gpu
              nominalQuota: 32
  preemption:
    withinClusterQueue: LowerPriority
```

- `nominalQuota` 是保证配额；
- `borrowingLimit` 允许借用空闲配额；
- `preemption` 控制队列内/队列间抢占；
- 监控 `kueue_pending_workloads`、`kueue_quota_reserved`。

### 29.6.5 选择 Kueue 还是 Volcano

| 需求 | 选择 |
|---|---|
| 队列/配额/公平/抢占 | Kueue |
| Pod 级 gang scheduling | Volcano/YuniKorn/scheduler-plugins |
| 两者都要 | Kueue 做准入 + Volcano 做 gang（需要验证兼容性） |
| 跨集群 | Kueue MultiKueue |

不要同时开多个 gang 调度器；它们会争抢 `schedulerName`。

## 29.7 Argo Workflows

Argo Workflows 适合把多个 RayJob 串成 DAG：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: ray-pipeline-
spec:
  entrypoint: pipeline
  templates:
    - name: pipeline
      dag:
        tasks:
          - name: prepare
            template: submit-rayjob
            arguments:
              parameters: [{name: job, value: prepare}]
          - name: train
            dependencies: [prepare]
            template: submit-rayjob
            arguments:
              parameters: [{name: job, value: train}]
    - name: submit-rayjob
      inputs:
        parameters: [{name: job}]
      resource:
        action: create
        manifest: |
          apiVersion: ray.io/v1
          kind: RayJob
          metadata:
            generateName: "{{inputs.parameters.job}}-"
          spec:
            entrypoint: python /app/{{inputs.parameters.job}}.py
            shutdownAfterJobFinishes: true
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
                          requests: {cpu: "1", memory: 2Gi}
```

注意：

- Argo 的 `resource` 模板只负责创建对象，不会等待 RayJob 完成；需要 `wait`/`suspend` 或自定义 controller；
- 更常见的是用 Argo 的 `http`/`container` 模板调用 Ray Job API，或写一个小的 RayJob 等待器；
- 每个步骤的 RayJob 要独立、幂等；
- 产物通过对象存储/S3 传递，不要依赖 Pod 本地盘。

## 29.8 Airflow

Airflow 的 `KubernetesPodOperator` 可以：

- 直接提交 `ray job submit` 到一个长期 RayCluster；
- 或创建 RayJob CR（需要 `KubernetesHook`/自定义 operator）。

推荐模式：

```text
Airflow DAG -> KubernetesPodOperator (ray job submit) -> 长期 RayCluster
```

或：

```text
Airflow DAG -> 创建 RayJob CR -> 轮询 status -> 下游任务
```

注意：

- Airflow worker 到 Ray Dashboard 的网络；
- 作业 ID/幂等；
- 超时/重试；
- 资源池隔离（用不同的 RayCluster）。

## 29.9 Kubeflow / Flyte

- **Kubeflow**：KFP pipeline 可以把 RayJob 作为一个 component；需要 Ray 的 KFP 集成或自定义 component。
- **Flyte**：Flyte 的 Ray plugin 可以直接创建 Ray 集群/作业；适合已有 Flyte 的平台。
- 共同点：上层工作流负责 DAG/缓存/重试，KubeRay 负责 Ray 运行时。

不要让两层都做重试：明确“谁负责重试”，否则失败会放大。

## 29.10 GitOps

### 29.10.1 目录结构

```text
platform/
├── base/
│   ├── operator/
│   ├── crds/
│   └── rbac/
├── tenants/
│   ├── team-a/
│   │   ├── raycluster.yaml
│   │   ├── rayjob.yaml
│   │   └── kustomization.yaml
│   └── team-b/
└── overlays/
    ├── staging/
    └── production/
```

### 29.10.2 Argo CD/Flux 注意

- CRD 升级单独管理（Helm 不升级 `crds/`）；
- 用 sync wave/health check 保证 operator 先于 CR；
- 不要把 Secret 明文放 Git（Sealed Secrets/External Secrets/SOPS）；
- 对 `managedBy` 的对象排除自动同步；
- 漂移检测要忽略 operator 写的 status/默认值；
- 多集群用 ApplicationSet/Flux Kustomization。

## 29.11 可观测性栈

```text
Prometheus Operator
  ├── ServiceMonitor: kuberay-operator
  ├── PodMonitor: Ray head/worker
  └── rules: RayCluster/RayJob/RayService
Grafana
  ├── KubeRay operator dashboard
  ├── Ray dashboard
  └── GPU/DCGM dashboard
Loki/Tempo
  ├── operator logs
  ├── Ray logs
  └── Serve traces
```

示例：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: kuberay-operator
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: kuberay-operator
  endpoints:
    - port: http
      interval: 30s
---
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: ray-pods
  namespace: monitoring
spec:
  selector:
    matchLabels:
      ray.io/is-ray-node: "yes"
  podMetricsEndpoints:
    - port: metrics
      interval: 30s
```

注意：

- Ray 容器需要暴露 metrics 端口（8080）；
- 多租户时 PodMonitor 的 namespaceSelector 要覆盖租户；
- 指标 cardinality 可能很高（task/actor），设置 relabel/drop；
- 日志用 Loki/Fluent Bit，追踪用 OTel Collector；
- 指标/日志/追踪的分工见第 8 章。

## 29.12 集成选择矩阵

| 需求 | 推荐 |
|---|---|
| 单租户、简单批作业 | 原生 kube-scheduler |
| gang scheduling | Volcano / scheduler-plugins / KAI Scheduler |
| 层次队列（已有 YuniKorn） | YuniKorn |
| 配额/公平/抢占 | Kueue |
| 跨集群 | Kueue MultiKueue |
| DAG 编排 | Argo Workflows / Flyte / Airflow |
| GitOps | Argo CD / Flux |
| 监控 | Prometheus Operator + Grafana |

## 29.13 练习

1. 部署 Volcano，创建一个 Queue，提交一个 4 worker 的 RayCluster，观察 PodGroup。
2. 把 Queue capability 调小，观察 Pending 事件。
3. 部署 Kueue，创建 ClusterQueue/LocalQueue，提交 RayJob，观察 Workload 准入。
4. 用 Argo Workflows 串两个 RayJob，处理等待/失败。
5. 用 Argo CD 管理一个租户目录，验证 CRD 升级流程。
6. 对比 Kueue 与 Volcano 的指标，判断瓶颈在准入还是调度。

## 29.14 队列容量规划

### 29.14.1 从作业形状到队列配额

对每个队列，统计：

```text
并发作业数 × 单作业最坏形状（head + workers）
+ 预留/突发
= 队列需要的资源上限
```

例：

- 单作业：head 4 CPU + 8 worker × (8 CPU + 1 GPU)；
- 同时 2 个作业；
- 队列至少需要 `2 × (4 + 64) = 136 CPU`、`2 × 8 = 16 GPU`。

如果配额只按平均形状算，大作业会永远排队。

### 29.14.2 gang scheduling 的“最小可运行形状”

```text
PodGroup.minMember = head + 所有 worker
```

- 小于实际 Pod 数：部分 Pod 运行，资源被占住；
- 大于队列可容纳：永远无法准入；
- 扩缩容后要更新 `minMember`。

### 29.14.3 借用与抢占

- `nominalQuota` 是保证，不是上限；
- `borrowingLimit` 允许借用空闲配额，但要防止“借了不还”；
- 抢占会杀死低优先级作业，必须有 checkpoint/重试；
- 监控 `kueue_pending_workloads`、`kueue_quota_reserved`。

### 29.14.4 碎片与形状

- GPU 碎片（8 个 1-GPU 作业占满，8-GPU 作业无法调度）；
- 用节点池按形状隔离；
- 大作业优先/预留整机；
- 监控“最大可调度形状”，不只是总空闲。

### 29.14.5 容量评审问题

1. 单作业最大形状是多少？
2. 同时最多跑几个？
3. 队列配额能否容纳最坏组合？
4. 借用的上限/归还策略？
5. 抢占是否会杀死关键作业？
6. 节点池上限是否 ≥ 队列配额？

## 29.15 小结

- 批调度器解决 Pod 级 gang scheduling；Kueue 解决作业级准入/配额。
- KubeRay 通过 `--batch-scheduler` 和 `managedBy` 与它们集成。
- Argo/Airflow/Kubeflow/Flyte 负责 DAG；不要让两层重复重试。
- GitOps 要把 CRD、operator、租户 CR 分开管理。
- 选择集成要看现有生态，不要同时开多个调度器。

下一章进入 KubeRay 源码：controller-runtime、关键函数、测试与贡献流程。

→ [第 30 章 KubeRay 源码导读与二次开发](30-source-code-tour.md)
