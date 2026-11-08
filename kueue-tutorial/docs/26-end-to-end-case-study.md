# 第 26 章 · 端到端案例：从资源池到多租户 GPU 平台

前面 25 章分别讲了对象、语义、算法、实现、运维和模式。本章把它们串成一个完整案例：**一个 96 卡 GPU 集群，如何从"先到先得"演进成多租户、可借用、可回收、可观测的 Kueue 平台。**

案例中的数字和 YAML 是可执行的起点，不是唯一答案。重点不是照抄，而是理解每一步的取舍、验证和回滚。

## 26.1 背景

一家公司的 AI 平台有 96 张 A100，节点为 12 台 8 卡机。四个使用方：

| 团队 | 负载特征 | 目标 |
|---|---|---|
| 推荐 | 大量 1～4 卡短训练，延迟敏感 | 快速拿到小配额，不被大作业阻塞 |
| 大模型 | 少量 32 卡、运行数天 | 保底容量，能借空闲卡，尽量不被打断 |
| 研究 | 实验性、可中断、优先级低 | 用空闲容量，随时可被回收 |
| 推理 | 长期在线，4 卡一组 | 稳定、低延迟、不可被训练抢占 |

当前做法：

- 每个 namespace 一个 `ResourceQuota`；
- 训练 Job 用 `PriorityClass`；
- 没有队列，先到先得；
- 大作业经常只启动一部分 Pod；
- 空闲 GPU 经常一边闲置、一边有人排队。

目标是：

1. 作业级原子准入，避免半启动；
2. 团队保底 + 空闲借用；
3. 推理不被训练抢占，训练可被高优先级回收；
4. 等待原因可解释；
5. 容量变化可运营、可回滚。

## 26.2 需求与非目标

### 功能需求

- 四个团队各有 LocalQueue；
- 推荐/大模型/研究位于同一棵 root Cohort 树，设计上允许在 limit 内共享；跨子树借用/回收的方向要在 staging 验证；
- 推理是独立 ClusterQueue（不加入 Cohort），不参与借用；
- spot 与 on-demand 两类 Flavor；
- 大模型作业支持 TAS（同 rack）；
- 研究作业可被回收，推荐短作业优先；
- 推理长期运行，需要明确的配额释放语义；
- 预留云上突发集群的 MultiKueue 接口。

### 非目标

- 不用 Kueue 做安全隔离（RBAC/NetworkPolicy 独立）；
- 不用 Kueue 做成本结算（成本系统读标签）；
- 不在第一阶段上 DQO/Concurrent Admission 等 alpha 能力；
- 不替换 kube-scheduler。

## 26.3 容量模型

### 26.3.1 物理容量

```text
12 台 × 8 卡 = 96 卡
系统/驱动/监控预留：4 卡
可调度：92 卡
安全余量 8%（约 7 卡）
可分配逻辑配额：85 卡
```

### 26.3.2 配额分配

```text
推理 ClusterQueue（独立，不加入 Cohort）
  cq-inference       nominal 16

共享 Cohort（root -> org-research/org-product）
  cq-recommend       nominal 24   borrowingLimit 8   lendingLimit 8
  cq-llm             nominal 32   borrowingLimit 16  lendingLimit 4
  cq-research        nominal 13   borrowingLimit 13  lendingLimit 13

合计 nominal = 16 + 24 + 32 + 13 = 85
```

注意：这里的 nominal 是每个 CQ **在所有 Flavor 上的合计**。下面的 YAML 会把它拆成 spot/on-demand 两个 Flavor；拆完之后每个 CQ 的 GPU 合计必须回到 24/32/13/16，否则容量模型和清单就对不上。CPU/内存按 12 台节点的可分配量配比：清单中四个 CQ 的 CPU 合计约 992 核、内存合计约 7.75 TiB，低于物理 allocatable，余量留给系统组件和碎片。

最坏情况：

- 推理满 16；
- 共享 Cohort 内三个队列都满载时，总量仍是 69；
- 共享 Cohort 可借的空闲来自同 Cohort 其他队列的未用 nominal；
- 物理上仍有 92 - 85 = 7 卡余量，用于碎片和故障恢复。

### 26.3.3 spot 与 on-demand

```text
Flavor a100-spot      nominal 为主，成本低，可中断
Flavor a100-ondemand  保底，稳定
```

策略：

- 推荐/研究优先 spot；
- 大模型优先 on-demand，spot 作为溢出；
- 推理只用独立的 `a100-inference` 稳定池（不加入共享 Cohort）；
- `flavorFungibility` 先找 nominal 内，再考虑借用/抢占；
- spot 中断由 checkpoint + 重试处理。

## 26.4 对象拓扑

```text
Cohort root
├── Cohort org-product
│   ├── cq-recommend
│   └── cq-llm
└── Cohort org-research
    └── cq-research

Cohort inference（独立）

LocalQueue:
  team-recommend/queue    -> cq-recommend
  team-llm/queue          -> cq-llm
  team-research/queue     -> cq-research
  team-inference/queue    -> cq-inference

ResourceFlavor:
  a100-spot        -> gpu-topology
  a100-ondemand    -> gpu-topology
  a100-inference   -> gpu-topology

Topology:
  gpu-topology: [zone, hostname]

PriorityClass / WorkloadPriorityClass:
  inference-critical
  llm-high
  recommend-standard
  research-best-effort
```

## 26.5 完整清单

### 26.5.1 Flavor

TAS 需要先有 Topology，并让参与 TAS 的 Flavor 引用它。下面假设节点带 `topology.kubernetes.io/zone` 和 `kubernetes.io/hostname` 标签：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Topology
metadata:
  name: gpu-topology
spec:
  levels:
  - nodeLabel: topology.kubernetes.io/zone
  - nodeLabel: kubernetes.io/hostname
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: a100-spot
spec:
  nodeLabels:
    node.kubernetes.io/instance-type: a100-spot
  topologyName: gpu-topology
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: a100-ondemand
spec:
  nodeLabels:
    node.kubernetes.io/instance-type: a100-ondemand
  topologyName: gpu-topology
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: a100-inference
spec:
  nodeLabels:
    node.kubernetes.io/instance-type: a100-inference
  topologyName: gpu-topology
```

> 如果集群节点没有 zone/hostname 标签，TAS 放置会失败。先确认标签再 apply；不上 TAS 的集群可以删掉 `topologyName` 和 Job 上的 topology 注解。

### 26.5.2 Cohort 树

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: root
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: org-product
spec:
  parentName: root
  fairSharing:
    weight: "2"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: org-research
spec:
  parentName: root
  fairSharing:
    weight: "1"
```

注意：推荐和大模型在 `org-product` 子树，研究在 `org-research` 子树，**不是三个 CQ 直接共享一个扁平 Cohort**。跨子树借用/回收是否发生，取决于 root 层级的共享、各 CQ 的 `borrowingLimit`/`reclaimWithinCohort` 和 Fair Sharing；下面的数字按"root 树内可借用"设计，但上线前要在 staging 用真实对象验证跨子树方向。

### 26.5.3 推理队列（独立）

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: cq-inference
spec:
  namespaceSelector:
    matchLabels:
      team: inference
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: a100-inference
      resources:
      - {name: cpu, nominalQuota: 256}
      - {name: memory, nominalQuota: 2Ti}
      - {name: nvidia.com/gpu, nominalQuota: 16}
```

推理 CQ 不加入共享 Cohort，避免训练借走推理卡。它自身也不配置抢占。

### 26.5.4 推荐队列

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: cq-recommend
spec:
  cohortName: org-product
  namespaceSelector:
    matchLabels:
      team: recommend
  queueingStrategy: BestEffortFIFO
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: a100-spot
      resources:
      - {name: cpu, nominalQuota: 192, borrowingLimit: 64, lendingLimit: 64}
      - {name: memory, nominalQuota: 1.5Ti, borrowingLimit: 512Gi, lendingLimit: 512Gi}
      - {name: nvidia.com/gpu, nominalQuota: 20, borrowingLimit: 8, lendingLimit: 8}
    - name: a100-ondemand
      resources:
      - {name: cpu, nominalQuota: 64, borrowingLimit: 0, lendingLimit: 0}
      - {name: memory, nominalQuota: 512Gi, borrowingLimit: 0, lendingLimit: 0}
      - {name: nvidia.com/gpu, nominalQuota: 4, borrowingLimit: 0, lendingLimit: 0}
  preemption:
    withinClusterQueue: LowerPriority
    reclaimWithinCohort: LowerPriority
```

### 26.5.5 大模型队列

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: cq-llm
spec:
  cohortName: org-product
  namespaceSelector:
    matchLabels:
      team: llm
  queueingStrategy: StrictFIFO
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: a100-ondemand
      resources:
      - {name: cpu, nominalQuota: 256, borrowingLimit: 128, lendingLimit: 32}
      - {name: memory, nominalQuota: 2Ti, borrowingLimit: 1Ti, lendingLimit: 256Gi}
      - {name: nvidia.com/gpu, nominalQuota: 28, borrowingLimit: 16, lendingLimit: 4}
    - name: a100-spot
      resources:
      - {name: cpu, nominalQuota: 128, borrowingLimit: 0, lendingLimit: 0}
      - {name: memory, nominalQuota: 1Ti, borrowingLimit: 0, lendingLimit: 0}
      - {name: nvidia.com/gpu, nominalQuota: 4, borrowingLimit: 0, lendingLimit: 0}
  preemption:
    withinClusterQueue: LowerPriority
    reclaimWithinCohort: LowerPriority
```

大模型用 `StrictFIFO` 保护"先提交的大作业"，避免小作业不断插队。代价是队头阻塞，需要配合监控。

注意容量口径：cq-llm 的 GPU 合计是 32（ondemand 28 + spot 4），但**单个 Workload 只能选一个 Flavor**。32 卡作业走 ondemand 时是"28 卡 nominal + 最多借 16 卡"；这个作业需要借 4 卡，Cohort 没有空闲时它会 Pending。spot 的 4 卡是另一个独立池，不能和 ondemand 拼进同一个 Workload。如果业务要求 32 卡硬保底，应把 ondemand 的 `nominalQuota` 改成 32，并相应下调其他队列。

### 26.5.6 研究队列

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: cq-research
spec:
  cohortName: org-research
  namespaceSelector:
    matchLabels:
      team: research
  queueingStrategy: BestEffortFIFO
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: a100-spot
      resources:
      - {name: cpu, nominalQuota: 96, borrowingLimit: 96, lendingLimit: 96}
      - {name: memory, nominalQuota: 768Gi, borrowingLimit: 768Gi, lendingLimit: 768Gi}
      - {name: nvidia.com/gpu, nominalQuota: 13, borrowingLimit: 13, lendingLimit: 13}
  preemption:
    reclaimWithinCohort: Never
```

研究队列没有 `withinClusterQueue` 抢占，避免实验互相驱逐；跨子树回收是否发生，取决于产品队列的 `reclaimWithinCohort` 是否覆盖、优先级关系和 root 层级共享，不能想当然。本例在 staging 用 `kueue_cluster_queue_resource_usage` 和 Evicted 事件验证方向。

### 26.5.7 LocalQueue 与 namespace

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: team-recommend
  labels:
    team: recommend
---
apiVersion: v1
kind: Namespace
metadata:
  name: team-llm
  labels:
    team: llm
---
apiVersion: v1
kind: Namespace
metadata:
  name: team-research
  labels:
    team: research
---
apiVersion: v1
kind: Namespace
metadata:
  name: team-inference
  labels:
    team: inference
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: team-recommend
  name: queue
spec:
  clusterQueue: cq-recommend
  fairSharing:
    weight: "2"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: team-llm
  name: queue
spec:
  clusterQueue: cq-llm
  fairSharing:
    weight: "1"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: team-research
  name: queue
spec:
  clusterQueue: cq-research
  fairSharing:
    weight: "1"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: team-inference
  name: queue
spec:
  clusterQueue: cq-inference
```

### 26.5.8 优先级

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata:
  name: inference-critical
value: 10000
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata:
  name: llm-high
value: 1000
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata:
  name: recommend-standard
value: 100
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata:
  name: research-best-effort
value: 0
```

Job 通过标签 `kueue.x-k8s.io/priority-class` 选择。Pod 的 `PriorityClass` 单独设置，用于 kube-scheduler；两者不要混为一谈。

### 26.5.9 清单落地顺序与可执行性验收

把上面的片段合并成一个文件后，按依赖顺序 apply，并先做 server-side dry-run：

```bash
kubectl apply --dry-run=server -f platform.yaml
kubectl apply -f platform.yaml

# 验收：namespace/标签、Flavor/Topology、CQ/LQ 是否都就绪
kubectl get namespaces -l team
kubectl get resourceflavors,topologies
kubectl get cohorts
kubectl get clusterqueues -o wide
kubectl get localqueues -A
```

如果 `cq-inference` 的 `namespaceSelector` 是 `team=inference`，必须确认 `team-inference` namespace 真的带这个标签；否则该队列永远匹配不到 Workload。TAS 作业还要确认节点有 `topology.kubernetes.io/zone`/`kubernetes.io/hostname` 标签。

## 26.6 训练作业示例

### 26.6.1 推荐短作业

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: recommend-short
  namespace: team-recommend
  labels:
    kueue.x-k8s.io/queue-name: queue
    kueue.x-k8s.io/priority-class: recommend-standard
spec:
  parallelism: 2
  completions: 2
  template:
    spec:
      containers:
      - name: train
        image: example/recommend:latest
        resources:
          requests:
            cpu: "8"
            memory: 32Gi
            nvidia.com/gpu: "1"
      restartPolicy: Never
```

### 26.6.2 大模型 32 卡作业 + TAS

```yaml
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: llm-train
  namespace: team-llm
  labels:
    kueue.x-k8s.io/queue-name: queue
    kueue.x-k8s.io/priority-class: llm-high
spec:
  replicatedJobs:
  - name: workers
    replicas: 4
    template:
      spec:
        parallelism: 1
        completions: 1
        template:
          metadata:
            annotations:
              kueue.x-k8s.io/podset-required-topology: topology.kubernetes.io/zone
          spec:
            containers:
            - name: worker
              image: example/llm:latest
              resources:
                requests:
                  cpu: "32"
                  memory: 256Gi
                  nvidia.com/gpu: "8"
            restartPolicy: Never
```

这里用 zone 作为拓扑层，实际可以是 rack/block。TAS 需要 Flavor 配置 `topologyName`，节点带对应标签。

### 26.6.3 研究可抢占作业

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: research-sweep
  namespace: team-research
  labels:
    kueue.x-k8s.io/queue-name: queue
    kueue.x-k8s.io/priority-class: research-best-effort
spec:
  parallelism: 1
  completions: 1
  template:
    spec:
      containers:
      - name: sweep
        image: example/research:latest
        resources:
          requests:
            cpu: "16"
            nvidia.com/gpu: "2"
      restartPolicy: Never
```

研究作业必须能 checkpoint/重试；被回收是预期行为，不是故障。

## 26.7 上线路线

### 阶段 0：影子

- 安装 Kueue，但只让新作业带 queue-name；
- 保留 ResourceQuota 作为兜底；
- 观察 Workload 创建、配额、Pending 原因；
- 不开启抢占/Fair Sharing。

退出条件：

- 连续两周没有准入异常；
- Pending 原因可解释；
- 控制器资源稳定。

### 阶段 1：试点

- 推荐 + 研究两个团队先接入；
- 建立共享 Cohort 和借用；
- 开启 `reclaimWithinCohort: LowerPriority`；
- 验证借用/回收、暂停/放行、完成释放。

退出条件：

- 借用和回收符合容量模型；
- 没有抢占风暴；
- 租户能看懂 `kubectl get workload`。

### 阶段 2：规模化

- 大模型接入，StrictFIFO + TAS；
- 推理是独立 ClusterQueue（不加入 Cohort）；
- 开启 Fair Sharing / AFS；
- 接入告警、仪表盘、成本标签；
- 迁移存量作业。

退出条件：

- 容量模型与物理容量对齐；
- SLO 达标；
- 变更可回滚。

### 阶段 3：高级能力

- MultiKueue 云上突发；
- DRA（如果使用新型设备）；
- DQO（容量随云供应变化）；
- Configurable Preemption（如果确有非对称需求）。

每个高级能力都要单独评估，不要和高风险配额调整同时上线。

## 26.8 SLO 与告警

### 26.8.1 SLO

| SLO | 指标 | 目标 |
|---|---|---|
| 准入延迟 | 创建到 QuotaReserved | P99 < 30s |
| 推荐队列等待 | Pending 到 Admitted | P99 < 10m |
| 大模型等待 | Pending 到 Admitted | 按周窗口统计 |
| 推理可用性 | 可用副本比例 | 99.9% |
| 驱逐率 | 每小时被抢占作业数 | 低于基线 |
| Pending 可解释 | 有明确 Reason 的比例 | > 99% |

### 26.8.2 告警

```promql
# 推荐队列 pending 持续增长
sum by (cluster_queue) (kueue_pending_workloads{cluster_queue="cq-recommend"}) > 20
for: 30m

# 共享 Cohort 长期满载
kueue_cohort_subtree_resource_reservations
  / kueue_cohort_subtree_quota > 0.95
for: 1h

# 驱逐率异常
sum by (reason) (rate(kueue_evicted_workloads_total[10m])) > 2

# AdmissionCheck 等待过长
histogram_quantile(0.99,
  sum by (le) (rate(kueue_admission_checks_wait_time_seconds_bucket[10m]))
) > 600

# 控制器重启
increase(kube_pod_container_status_restarts_total{namespace="kueue-system"}[1h]) > 0
```

阈值要按基线调整；不要直接抄。

## 26.9 事故复盘

### 26.9.1 借用风暴

**现象**：研究子树借走大量 root 层容量，大模型提交后触发回收，研究作业反复被驱逐。

**根因**：

- 研究队列 `borrowingLimit` 设得过大；
- 大模型 `reclaimWithinCohort: LowerPriority`；
- 研究作业退避时间太短。

**修复**：

- 给研究子树/root 设置更紧的 Cohort 级 borrowingLimit，并收紧研究 CQ 的 borrowingLimit；
- 提高研究作业的退避/重试上限；
- 监控每次抢占的 victim 数；
- 关键大模型用 nominal 保底，不依赖回收。

### 26.9.2 AFS 冷启动

**现象**：新建 LocalQueue 的作业总是优先，老队列被压后。

**根因**：AFS 历史用量为 0，采样窗口/衰减让新队列快速获得低份额；真实 Kueue 的 entry penalty 由公式自动计算，不能直接调大，租户仍可通过新建 LocalQueue 刷低份额。

**修复**：

- 调整 AFS 采样窗口/半衰期，并限制每个团队可创建的 LocalQueue 数量；
- 限制 LocalQueue 创建权限；
- 用 Namespace 标签/团队维度绑定历史，而不是只看 LQ 名；
- 观察 `kueue_local_queue_admission_fair_sharing_usage`。

### 26.9.3 TAS 碎片

**现象**：GPU 总数够，但 32 卡作业一直 Pending。

**根因**：空闲卡分散在多个 rack，`required` 拓扑找不到完整 domain。

**修复**：

- 用 `preferred` 允许降级；
- 调整作业大小/拓扑层级；
- 用 TAS 打散/碎片整理策略；
- 监控每个 domain 的空闲容量，而不是只看总量。

### 26.9.4 DRA 重复计费

**现象**：Pod 只请求 1 卡，Workload 却扣 2 卡。

**根因**：同一资源既写了扩展资源，又走了 DeviceClass 映射，实现把两者相加。

**修复**：

- 取 `max` 而不是求和；
- 只保留一条映射路径；
- 排障时同时看 `requests`、`resourceClaims` 和 Kueue 的 admission 用量。

### 26.9.5 Concurrent 迁移抖动

**现象**：作业在 spot 和 reserved 之间反复迁移。

**根因**：迁移条件写成"不同就迁"，没有"严格更优"的偏序。

**修复**：

- 只允许迁移到更靠前的 Flavor；
- 迁移后重新校验；
- 记录迁移次数，超过阈值告警；
- 对不可中断作业关闭 Concurrent。

### 26.9.6 MultiKueue 过度提名

**现象**：manager 提名大量 worker，但作业长期不运行。

**根因**：manager 配额远大于 worker 总容量；Incremental step 太大。

**修复**：

- manager nominal ≈ worker 总可用量；
- 调小 stepSize/扩大间隔；
- 监控 `nominatedClusterNames` 和远端 Pending；
- worker 失联要及时清理。

### 26.9.7 Admission gate 卡住

**现象**：Workload 一直 `AdmissionGated`，但没有配额问题。

**根因**：外部控制器失败，没有清除 gate；或 gate 名称写错。

**修复**：

- 监控 `AdmissionGated` 的 Workload 数和年龄；
- 给 gate 设置超时/告警；
- 明确 gate 的 owner 和清除条件；
- 不要把 gate 当 AdmissionCheck 用。

## 26.10 成本与标签

在 Job/namespace 上打标签，复制到 Workload：

```yaml
metadata:
  labels:
    platform.example.com/tenant: team-llm
    platform.example.com/cost-center: ai-001
    platform.example.com/environment: prod
```

Kueue Configuration：

```yaml
integrations:
  labelKeysToCopy:
  - platform.example.com/tenant
  - platform.example.com/cost-center
  - platform.example.com/environment
```

用途：

- 成本分摊；
- 按团队查 Workload；
- 审计和容量回顾；
- 告警路由。

Kueue 不做财务结算；它只提供可关联的标签。

## 26.11 演进路线

### 近期

- 稳定共享 Cohort 的借用/回收；
- 完善 TAS 与 spot 中断处理；
- 建立配额合同和变更流程；
- 接入成本标签和仪表盘。

### 中期

- MultiKueue 云上突发；
- AFS 参数调优；
- DRA（如果引入新设备）；
- 更细的团队层级和权重。

### 长期

- DQO 让配额跟随实际容量；
- Configurable Preemption 做碎片整理；
- 训练/推理统一调度与 SLA；
- 多集群公平与成本/碳排感知。

## 26.12 经验清单

1. **先有容量模型，再有 YAML。** nominal 总和、借用上限、安全余量要能解释。
2. **保底配额给足，借用处理峰谷。** 不要把 nominal 压到极低、靠借用提高利用率。
3. **推理和训练分开。** 长期运行对象要有独立的 ClusterQueue/配额和明确的释放语义；是否加入 Cohort 取决于你是否愿意让它参与借用。
4. **抢占是最后手段。** 优先用 nominal、权重、队列策略解决，不要一上来 `Any`。
5. **AFS 要防冷启动。** 采样窗口/半衰期、LocalQueue 创建权限、团队维度绑定（entry penalty 自动计算，不可直接调）。
6. **TAS 要看 domain 空闲，不只看总卡数。**
7. **DRA/资源变换只保留一条计费路径。** 避免重复扣费。
8. **Concurrent/MultiKueue 都要求幂等和终止条件。** 否则会抖动或重复运行。
9. **每个新能力都要有指标、告警、runbook 和回滚。**
10. **升级和配额调整不要同时做。**

## 26.13 本章小结

- 一个可运营的平台 = 容量模型 + 对象拓扑 + 配额合同 + 准入/借用/抢占策略 + 可观测性 + 变更流程。
- 案例用 96 卡集群演示了推理独立、产品/研究共享、spot/on-demand 分层、TAS 和优先级。
- 上线分影子、试点、规模化、高级能力四阶段，每阶段有退出条件。
- 事故复盘覆盖借用风暴、AFS 冷启动、TAS 碎片、DRA 重复计费、Concurrent 抖动、MultiKueue 过度提名和 gate 卡住。
- 成本标签、SLO、告警和回滚是平台的一部分，不是"以后再加"。
- 最重要的是：让"为什么这个作业没开始"可解释；这是 Kueue 相对"先到先得"的核心价值。
- 案例里的 85 卡 logical quota 是否合理，可以用第 29 章的容量模拟器做敏感性分析；"Admitted 但 Pod 起不来"则按第 28 章的分层决策树排查。

---

[← 上一章：生态集成实战](25-integration-cookbook.md) | [返回目录](../README.md) | [下一章：验证工程 →](27-verification-engineering.md)
