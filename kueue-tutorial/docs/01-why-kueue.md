# 第 1 章 · 为什么需要 Kueue

## 1.1 一个真实的平台困境

假设你管理一个 64 卡的 GPU 集群，有三个团队：

- 推荐团队：每天提交几十个短训练任务，单任务 1～4 卡，延迟敏感。
- 大模型团队：每周提交几个 32 卡、运行数天的任务，吞吐敏感。
- 研究团队：不定期提交大量实验，优先级低，可以等。

如果只使用 Kubernetes 原生能力，常见做法是：

1. 给每个 namespace 配 `ResourceQuota`；
2. 给训练任务加 `PriorityClass`；
3. 用 `kube-scheduler` 的优先级和抢占让高优先级 Pod 挤掉低优先级 Pod；
4. 用某个 Job 控制器管理重试和并行度。

这套组合能跑，但会出现几个很难受的问题：

- **配额是 Pod 级的，不是作业级的。** 一个 32 卡作业可能先启动 20 个 Pod、占住 20 卡，然后因为剩下的 12 卡被别的作业拿走而永久 pending。集群看起来"用满了"，实际没有任何作业能完成。
- **抢占发生在 Pod 级。** 高优先级作业可能只抢到一部分 Pod，低优先级作业被拆得七零八落，双方都无法收敛。
- **没有真正的队列。** `ResourceQuota` 只做"不超过"的硬限制，不提供排队、公平、借用、回收。
- **没有作业级公平。** 两个团队都提交 100 个作业时，谁先抢到 Pod 谁赢；长期看，先提交的团队会占住资源。
- **没有作业级配额预留。** 调度器看到的是节点上的空闲资源，而不是"这个作业已经获得 32 卡的承诺"。
- **多集群/弹性容量无法统一排队。** 本地集群满了，作业不会自动考虑到云端或另一个集群排队。

Kueue 要解决的正是这些问题：**在 Pod 创建之前，以 Workload 为单位做队列、配额、公平和抢占决策。**

## 1.2 Kueue 的定位

Kueue 是 Kubernetes SIG Scheduling 下的项目。它的核心定位可以概括为三句话：

1. **它是作业级排队与配额管理器，不是节点调度器。**
2. **它在 Pod 被创建之前做准入（admission）决策。**
3. **它通过 Job 控制器/集成层来暂停和放行真实作业。**

```text
用户提交 Job / JobSet / RayJob / MPIJob ...
        │
        ▼
┌─────────────────────────────┐
│ Job 集成控制器               │  创建 Workload，暂停真实 Job
│ (Kueue integration)         │
└──────────────┬──────────────┘
               │ Workload
               ▼
┌─────────────────────────────┐
│ Kueue 控制面                 │
│  QueueManager → Cache       │
│  Scheduler → Admission      │
│  Preemption / Fair Sharing  │
└──────────────┬──────────────┘
               │ Admitted
               ▼
┌─────────────────────────────┐
│ Job 集成控制器               │  放行 Job，创建 Pod
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ kube-scheduler              │  决定 Pod 落在哪个 Node
└─────────────────────────────┘
```

这个分工非常重要：

- Kueue 关心"**这个作业现在有没有资格开始**"。
- `kube-scheduler` 关心"**这个 Pod 应该放在哪台机器**"。
- 两者之间通过 Pod 的创建/暂停和 ResourceFlavor 注入的 nodeSelector/toleration 协作。

Kueue 不直接操作 Job 对象。它管理 `Workload`，由集成控制器把 Workload 的准入结果同步回 Job。这样同一套队列语义可以覆盖多种作业框架。

### 1.2.1 控制面组件

从实现角度看，Kueue 由几块组成：

- **kueue-controller-manager**：核心控制器进程，包含 QueueManager、Cache、Scheduler、Workload/Queue/AdmissionCheck 控制器、Webhook、指标和 leader election。
- **CRD**：Workload、LocalQueue、ClusterQueue、Cohort、ResourceFlavor、AdmissionCheck、Topology、ProvisioningRequestConfig 等。
- **集成控制器**：把 Job/JobSet/Kubeflow/Ray/Pod 等对象转换成 Workload，并负责暂停/放行。
- **AdmissionCheck 控制器**：ProvisioningRequest、MultiKueue，以及自定义检查控制器。
- **Visibility API**：查询 pending Workload/队列状态。
- **配置与 feature gate**：控制集成、公平策略、实验特性。

这些组件共同构成"作业级调度前置层"，而不是单个调度器插件。

## 1.3 如果不用 Kueue，会怎样

### ResourceQuota 不够

`ResourceQuota` 是 namespace 级的硬上限。它能阻止超额创建，但不能：

- 让作业排队；
- 在团队之间借还配额；
- 做加权公平；
- 在作业粒度抢占；
- 保证一个作业的所有 Pod 要么都启动、要么都不启动；
- 表达"这批 GPU 是 spot，那批是 on-demand"。

### Kubernetes 优先级/抢占不够

`kube-scheduler` 的抢占以 Pod 为单位。它适合"关键 Pod 要尽快运行"的场景，但不适合"一整个 32 卡训练作业要原子地获得资源"。此外，Pod 级抢占不理解：

- ClusterQueue 的 nominal quota；
- Cohort 的借用/出借；
- 作业的公平份额；
- 作业完成后配额如何回到队列。

### 自己写一个 Job 控制器不够

你当然可以自己实现"暂停/恢复 + 配额检查 + 队列"，但这会很快遇到：

- 多资源、多 Flavor、多资源组的组合；
- 抢占的死锁与抖动；
- 公平分享的长期收敛；
- 多租户、可观测性、API 校验；
- 与 JobSet、Kubeflow、Ray 等生态的兼容。

Kueue 把这些做成了一套可复用的 API 和控制器。

## 1.4 Kueue 的核心设计原则

### 原则一：Workload 是准入单位

一个 Workload 可以包含多个 PodSet，例如：

- driver：1 个 Pod，2 CPU；
- worker：8 个 Pod，每个 1 GPU；
- parameter server：2 个 Pod，每个 4 CPU。

Kueue 把这些 PodSet 的资源需求汇总，作为一个整体做配额判断。只有整个 Workload 能放下时，才会准入。

### 原则二：两阶段准入

Kueue 的准入不是"一下说 yes"，而是：

1. **Quota Reservation**：逻辑配额足够，预留。
2. **Admission Checks**：可选的外部检查，例如云容量是否真的可用、多集群是否已选中、拓扑是否可放置。

只有两个阶段都通过，Workload 才变成 `Admitted`，Job 才被放行。

这个设计把"逻辑配额"和"物理/外部条件"解耦。云上扩容、多集群调度、拓扑感知都可以作为 AdmissionCheck 插入，而不污染核心配额算法。

### 原则三：队列是分层的

- `LocalQueue`：namespace 级入口，租户看到的是它。
- `ClusterQueue`：集群级配额池。
- `Cohort`：一组 ClusterQueue，可以互相借用空闲配额。

这形成"租户入口 → 配额池 → 配额共享组"的层次。

### 原则四：Flavor 表达异构资源

同样是 GPU，A100、H100、L4、spot、on-demand 的代价和能力不同。Kueue 用 `ResourceFlavor` 表达这些差异，并在准入时给每个资源组分配一个 Flavor。

### 原则五：默认不干扰未托管的作业

Kueue 通过 `kueue.x-k8s.io/queue-name` 标签识别要托管的 Job。没有这个标签的 Job 默认不受影响（除非显式开启 `manageJobsWithoutQueueName`）。这让它可以渐进式接入现有集群。

### 原则六：控制面可观测、可调试

Kueue 暴露大量指标、条件（Conditions）、事件和 Visibility API。队列为什么 pending、为什么被抢占、为什么 CQ inactive，都有迹可循。

## 1.5 与相邻项目的对比

| 项目 | 主要层次 | 是否替代 kube-scheduler | 典型能力 |
|---|---|---|---|
| Kueue | 作业级队列/配额 | 否 | Workload 准入、Cohort 借用、公平分享、抢占、AdmissionCheck、多集群 |
| Volcano | 批处理调度 | 部分场景下替代/扩展 | gang scheduling、队列、DRF、拓扑 |
| YuniKorn | 资源调度器 | 可作为调度器 | 层级队列、公平、抢占 |
| Slurm | HPC 调度器 | 独立体系 | 作业队列、分区、预留、拓扑 |
| Kubernetes ResourceQuota | namespace 配额 | 否 | 硬上限，无队列/公平/借用 |
| Kubernetes PriorityClass | Pod 优先级 | 是调度器输入 | Pod 级优先级与抢占 |

Kueue 的差异化在于：

- **Kubernetes 原生 API**：CRD + controller，不替换调度器。
- **作业级语义**：Workload 而非 Pod 是单位。
- **生态集成广**：Job、JobSet、Kubeflow、Ray、MPI、AppWrapper、LeaderWorkerSet 等。
- **可组合的准入**：AdmissionCheck 把云扩容、多集群、拓扑等能力接进来。
- **多集群**：MultiKueue 在 manager/worker 架构下分发作业。

### 1.5.1 选型建议：Kueue、Volcano、YuniKorn 怎么选

三者经常被放在一起比较，但它们的切入点不同：

| 你的主要问题 | 优先考虑 | 原因 |
|---|---|---|
| 作业级配额、借用、公平、抢占，且继续用 kube-scheduler | Kueue | 不替换调度器，CRD 化配额，生态集成广 |
| 需要 gang scheduling、DRF、队列，且愿意使用专用调度器 | Volcano | 调度器内置 gang/队列，适合批处理集群 |
| 需要多层级队列、跨框架统一调度、Hadoop/YARN 迁移 | YuniKorn | 分层队列和资源调度是核心能力 |
| 已有 Slurm 集群，需要 Kubernetes 化 | Slurm + Kueue 组合或迁移评估 | Slurm 的 HPC 语义与 Kubernetes 不同，不能简单替换 |
| 只需要 namespace 硬上限 | ResourceQuota | 不需要引入新的控制面 |

一个常见的组合是：**Kueue 负责"作业何时开始 + 配额给谁"，kube-scheduler 负责"Pod 放哪"，Volcano/其他调度器只在需要 gang 或特殊放置时作为 Pod 调度器使用**。但要注意，Kueue 的 Workload 级 all-or-nothing 与调度器的 gang scheduling 解决的是不同层次的问题：前者保证配额一起预留，后者保证 Pod 一起放置。两者可以互补，也可能产生语义重叠，落地前要明确边界。

## 1.6 Kueue 不做什么

理解边界同样重要：

- **不做节点级放置。** Pod 最终落在哪个 Node 仍由 `kube-scheduler`（或你配置的调度器）决定。
- **不保证物理资源一定可用。** 逻辑配额足够不代表节点上真的有卡；DRA、TAS、ProvisioningRequest 和 `WaitForPodsReady` 用来缩小这个 gap。
- **不是安全边界。** namespace、RBAC、NetworkPolicy、Pod Security 仍由 Kubernetes 负责。
- **不是通用的工作流引擎。** 它管理排队和准入，不编排任务依赖（DAG）；工作流由 Argo Workflows、JobSet 等负责。
- **不自动替你写 Job 控制器。** 对自定义框架，需要集成层创建 Workload 并响应准入结果。

### 1.6.1 五个"不该用 Kueue"的信号

Kueue 不是所有集群的默认答案。出现以下信号时，先停下来评估：

1. **只有单个团队、单类作业、资源长期充足。** 引入 LocalQueue/ClusterQueue/Cohort 只会增加一层间接。
2. **主要需求是 namespace 硬隔离和预算。** ResourceQuota + 成本系统更直接；Kueue 的配额是逻辑配额，不是账单。
3. **作业必须跨节点强 gang 放置，且不能容忍任何半启动。** Kueue 的配额预留只能保证逻辑上放得下；物理放置仍要靠 TAS、调度器 gang 或 ProvisioningRequest 兜底。
4. **团队没有 Kubernetes 控制器运维能力。** Kueue 引入 CRD、webhook、finalizer、leader election 和新的排障面；没有 owner 的平台不建议上线。
5. **想用 Kueue 替代安全边界。** 它不能替代 RBAC、NetworkPolicy、Pod Security、镜像签名和节点隔离。

反过来，以下场景通常值得引入：多团队共享 GPU、作业大小差异大、需要借用/回收、需要作业级公平、需要把云扩容/多集群/拓扑接入准入、需要统一 Job/JobSet/Ray/Kubeflow 的排队语义。

## 1.7 一次请求的完整路径

把前六节的概念串起来，一个 Job 从提交到运行大致经过十条链路：

```text
1. 用户提交 Job（带 kueue.x-k8s.io/queue-name 标签）
2. Job 集成控制器创建 Workload，并暂停 Job
3. Workload 进入 LocalQueue，QueueManager 把它放进对应 ClusterQueue 的 heads/backlog
4. Scheduler 取 Cache 快照，按优先级/创建时间/公平份额排序
5. FlavorAssigner 为每个资源组选择 ResourceFlavor，检查 nominal/borrow/lend/cohort
6. 如果放不下，Preemptor 选择 victim 并模拟移除；成功后提交驱逐
7. 配额预留成功，Workload 写 QuotaReserved=True 与 status.admission
8. AdmissionCheck 控制器执行外部检查；全部 Ready 后写 Admitted=True
9. Job 集成取消暂停，Job 控制器创建 Pod
10. kube-scheduler 把 Pod 放到节点；TAS/DRA 可能在准入阶段已经约束了放置
```

这条链路里有两个容易混淆的"完成"：

- **QuotaReserved**：逻辑配额已经锁定，其他 Workload 不能再用。
- **Admitted**：所有外部条件也满足，Job 才被真正放行。

理解这两个阶段，是读懂后续所有章节的基础。

> 链路第 8～10 步之后还有一层"物理落地"：Job 集成取消暂停、Pod 创建、kube-scheduler 绑定、DRA 分配设备、kubelet 启动容器。Kueue 的 `Admitted` 只保证配额到手，不保证 Pod 一定能放下。第 28 章会把这一层单独拆开。

## 1.8 版本与 API 演进

Kueue 的 API 经历过多次演进：

- 早期使用 `v1beta1`；
- 新版本以 `v1beta2` 为主；
- `cohortName` 仍然是一个字符串引用：所有同名的 ClusterQueue 属于同一个扁平 Cohort；
- 需要 Cohort 级共享配额、层级结构、独立权重时，再创建 `Cohort` 对象；`Cohort` 的 `spec.parentName` 让它成为树节点；
- 新特性通过 feature gate 逐步进入 alpha/beta/stable。

这里有一个常见误解：**`cohortName` 不"指向"某个 Cohort 对象，它只是分组名**。只有当你创建了同名 `Cohort` 对象时，这棵组才有共享池、层级和独立权重。扁平借用可以在没有 Cohort 对象的情况下工作，但可运维性和能力都会受限。

写这套教程时，建议遵循两条规则：

1. **生产清单固定 API 版本**，不要混用 `v1beta1` 和 `v1beta2` 的字段。
2. **升级前读 release notes**，尤其是 CRD schema、默认值和 feature gate 的变化。

本教程的示例以 `kueue.x-k8s.io/v1beta2` 为主；涉及 alpha/beta 能力时会明确标注。截至 2026 年 11 月，Kueue 稳定线是 v0.20.x（最新 patch v0.20.5），v0.21 仅有 devel：TAS 是 beta 且默认开启，Admission Fair Sharing 是 beta 且默认开启，DQO/Concurrent Admission/Configurable Preemption 仍是 alpha；v1beta1 从 v0.20 起不再 served。具体以你部署的 release 为准。

## 1.9 本章小结

- Kubernetes 原生调度器解决"Pod 放哪"，Kueue 解决"作业何时开始、配额给谁"。
- Kueue 以 Workload 为准入单位，采用"配额预留 + AdmissionCheck"两阶段准入。
- LocalQueue / ClusterQueue / Cohort / ResourceFlavor 构成队列、配额、共享和异构资源模型。
- Kueue 不替代 `kube-scheduler`，也不替代安全边界和工作流引擎。
- 下一章会把核心对象逐个拆开，建立完整的对象关系图。

---

[返回目录](../README.md) | [下一章：核心对象与资源模型 →](02-core-objects.md)
