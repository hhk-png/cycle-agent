# 第 23 章 · 附录

## 23.1 术语表

| 术语 | 含义 |
|---|---|
| Workload | Kueue 的准入单位，包含一个或多个 PodSet |
| PodSet | 一组同构 Pod，包含 count 和 Pod 模板 |
| LocalQueue | namespace 级队列入口，指向 ClusterQueue |
| ClusterQueue | 集群级配额池 |
| Cohort | 一组可互相借用配额的 ClusterQueue，可层级嵌套 |
| ResourceFlavor | 一类异构资源，通过 nodeLabels/tolerations 关联节点 |
| Topology | 节点拓扑层级描述，用于 TAS |
| AdmissionCheck | 配额预留后的外部准入检查 |
| QuotaReserved | 逻辑配额已预留的条件 |
| Admitted | 所有检查通过，Workload 可运行 |
| Preemption | 驱逐已准入 Workload 以腾出配额 |
| Reclaim | 本 CQ 收回被其他 CQ 借走的 nominal |
| Borrowing | 使用同 Cohort 其他 CQ 的空闲配额 |
| Lending | 把自己的空闲配额借给同 Cohort 的其他 CQ |
| nominalQuota | CQ 在某个 Flavor/Resource 上的保底配额 |
| borrowingLimit | CQ 最多能借多少 |
| lendingLimit | CQ 最多能借出多少 |
| DRS | Dominant Resource Share，最紧资源的使用率 |
| AlmostLCA | 层级 Cohort 公平比较中使用的"近似最低公共祖先" |
| TAS | Topology-Aware Scheduling |
| TopologyDomain | 拓扑树中的叶子域，带可调度容量 |
| DRA | Dynamic Resource Allocation |
| DQO | Dynamic Quota Orchestration |
| CapacityProvider | 向 DQO 报告实际容量的对象 |
| effectiveQuotas | DQO 计算出的生效配额，调度器优先使用 |
| MultiKueue | 多集群作业分发 |
| ProvisioningRequest | 云扩容 AdmissionCheck |
| All-or-nothing | 作业的所有 Pod 要么都启动，要么都不启动 |
| WaitForPodsReady | 准入后等待 Pod 全部就绪，超时则驱逐的安全网 |
| reclaimablePods | Job 集成报告不再需要的 Pod，用于动态回收配额 |
| Admission Fair Sharing | 基于 LocalQueue 历史用量的准入公平 |
| Entry Penalty | AFS 的入场惩罚；真实 Kueue 由公式自动计算，MiniKueue 暴露为可调参数 |
| 显式 Cohort | 创建了同名 Cohort 对象，有共享池/层级/权重 |
| 隐式 Cohort | 只靠 cohortName 分组，只有借用能力 |
| Parent/Variant Workload | Concurrent Admission 的父对象与各 Flavor 尝试副本 |
| Configurable Preemption | 用 PreemptionConfig 声明式定义抢占规则 |
| deviceClassMappings | DRA DeviceClass 到逻辑配额资源的映射 |
| consumesCounters | 分区设备按 counter 计费的字段 |
| Resource Transformation | 把输入资源按比例映射成配额资源 |
| Admission Gate | 配额预留之前的外部闸门，gate 未清除时不占配额 |
| 默认 LocalQueue | 名字为 `default` 的 LocalQueue；mutating webhook 给未声明队列的新 Job 自动补标签 |
| 默认 WorkloadPriorityClass | 名字为 `default` 的 WorkloadPriorityClass；未声明优先级的被管理作业自动继承 |
| 纳管边界 | 哪些 namespace/对象会被 Kueue 接管；由 queue-name、默认队列、namespace selector、VAP 等共同决定 |
| ValidatingAdmissionPolicy | Kubernetes CEL 准入策略；可用于拒绝未声明 queue-name 的作业 |
| Showback / Chargeback | 成本分摊；Showback 只展示，Chargeback 进入账单 |
| envtest | 用真实 kube-apiserver/etcd 跑控制器集成测试的测试环境 |
| Slice | 把一个 PodSet 切成若干片，每片放进一个拓扑 domain |
| Topology Spreading | 跨 Workload/domain 打散放置 |
| Concurrent Admission | 一个 Workload 在多个 Flavor 上尝试并迁移 |
| MultiKueue-lite | MiniKueue 的多 Engine manager/worker 模拟 |
| Visibility API | 查询 pending/队列状态的只读 API |
| KueueViz | Kueue 的浏览器仪表盘 |
| Witness / Certificate | 准入诊断中的"可行见证"（完整 Flavor 分配）与"不可行证书"（首个失败约束） |
| WAS / PodGroup | Kubernetes 原生 Workload-Aware Scheduling 与 gang 放置对象（`scheduling.k8s.io`） |
| `GenericWorkload` | Kubernetes 1.37 中合并 Gang/WorkloadAwarePreemption 的 Beta gate，默认关闭 |

## 23.2 API 字段速查

### Workload

| 字段 | 说明 |
|---|---|
| `spec.active` | 是否允许排队/运行 |
| `spec.queueName` | LocalQueue 名称 |
| `spec.priority` | 队列/抢占优先级 |
| `spec.priorityClassRef` | 真实 Kueue v1beta2 的 WorkloadPriorityClass 引用；MiniKueue 内部用 `priorityClassName` |
| `spec.podSets[]` | PodSet 列表（含 `requests`、`devices`、`topology`） |
| `spec.admissionGates` | MiniKueue 的 admission gate 列表 |
| `spec.resourceTransformations` | MiniKueue 的资源变换列表（提交时冻结） |
| `spec.maximumExecutionTimeSeconds` | 最大执行时间 |
| `spec.waitForPodsReady` | 工作负载级 WaitForPodsReady（alpha） |
| `status.conditions[]` | QuotaReserved/Admitted/Evicted/Preempted/Finished/AdmissionGated/PodsReady；Inadmissible 是 QuotaReserved=False 的 Reason |
| `status.admission` | ClusterQueue、Flavor 分配、用量、Topology 分配 |
| `status.admissionChecks[]` | AdmissionCheck 状态（含 requeueAfterSeconds/retryCount） |
| `status.requeueState` | 重排队次数/时间 |
| `status.schedulingStats` | 按 reason/underlyingCause 的驱逐统计 |
| `status.reclaimablePods[]` | 可回收的 Pod 数 |
| `status.nominatedClusterNames[]` | MultiKueue 提名的 worker |
| `status.clusterName` | MultiKueue 最终选中的 worker |

### LocalQueue

| 字段 | 说明 |
|---|---|
| `spec.clusterQueue` | 目标 ClusterQueue |
| `spec.stopPolicy` | 队列级停止策略（与 CQ stopPolicy 叠加） |
| `spec.fairSharing.weight` | Admission Fair Sharing 的队列权重 |
| `status.pendingWorkloads` | 未准入数 |
| `status.reservingWorkloads` | 已预留未完成数 |
| `status.admittedWorkloads` | 已准入未完成数 |
| `status.fairSharing` | AFS 历史用量/加权份额 |

### ClusterQueue

| 字段 | 说明 |
|---|---|
| `spec.namespaceSelector` | 允许的 namespace；null 不允许，`{}` 全部 |
| `spec.resourceGroups[]` | 资源组 |
| `spec.cohortName` | Cohort 名称 |
| `spec.queueingStrategy` | StrictFIFO/BestEffortFIFO |
| `spec.preemption.withinClusterQueue` | Never/LowerPriority/LowerOrNewerEqualPriority |
| `spec.preemption.reclaimWithinCohort` | Never/LowerPriority/Any |
| `spec.preemption.borrowWithinCohort` | policy/maxPriorityThreshold |
| `spec.preemptionConfigName` | 引用 alpha PreemptionConfig（MiniKueue 字段） |
| `metadata.labels` | clusterQueueSelector 匹配用 |
| `spec.admissionChecksStrategy.admissionChecks[]` | AdmissionCheck 配置（v1beta2 已移除顶层 `spec.admissionChecks`） |
| `spec.stopPolicy` | None/Hold/HoldAndDrain |
| `spec.fairSharing.weight` | 公平权重 |
| `spec.flavorFungibility` | Flavor 切换策略 |
| `spec.admissionScope` | Admission Fair Sharing 模式 |
| `spec.concurrentAdmissionPolicy` | 并发准入（alpha） |
| `spec.deviceClassMappings` | DRA DeviceClass → 逻辑资源（**MiniKueue 教学字段**；真实 Kueue 在 Configuration `resources.deviceClassMappings`） |
| `status.effectiveQuotas` | DQO 生效配额（alpha） |
| `status.fairSharing.weightedShare` | 当前加权份额 |

### ResourceGroup / FlavorQuota

| 字段 | 说明 |
|---|---|
| `coveredResources[]` | 该组覆盖的资源 |
| `flavors[].name` | ResourceFlavor 名称 |
| `flavors[].resources[].name` | 资源名 |
| `flavors[].resources[].nominalQuota` | 保底配额 |
| `flavors[].resources[].borrowingLimit` | 借入上限；null = 不限 |
| `flavors[].resources[].lendingLimit` | 出借上限；null = 全部 nominal |

### Cohort

| 字段 | 说明 |
|---|---|
| `spec.parentName` | 父 Cohort |
| `spec.resourceGroups[]` | 共享额度 |
| `spec.fairSharing.weight` | 公平权重 |

### ResourceFlavor

| 字段 | 说明 |
|---|---|
| `spec.nodeLabels` | 节点标签 |
| `spec.tolerations` | 注入 Pod 的 toleration |
| `spec.nodeTaints` | 限制使用的污点 |
| `spec.topologyName` | TAS 拓扑 |

### PodSet / PodSetTopology（MiniKueue）

| 字段 | 说明 |
|---|---|
| `podSets[].devices[]` | DRA-lite 设备请求：`DeviceClass` + `Count` |
| `podSets[].topology.mode` | Required/Preferred/Unconstrained |
| `podSets[].topology.level` | 拓扑层级 |
| `podSets[].topology.group` | 多个 PodSet 共享 domain |
| `podSets[].topology.sliceSize` | 每片 Pod 数；仅 Required |
| `podSets[].topology.spreading` | 优先选择最空 domain |

### AdmissionCheck

| 字段 | 说明 |
|---|---|
| `spec.controllerName` | 处理控制器 |
| `spec.parameters` | 参数引用 |

### Topology

| 字段 | 说明 |
|---|---|
| `spec.levels[]` | 拓扑层级（nodeLabel） |

### PreemptionConfig（alpha）

| 字段 | 说明 |
|---|---|
| `spec.rules[].name` | 规则名 |
| `spec.rules[].activationPolicy.trigger` | Always/InsufficientQuota/QuotaFeasibleAndInsufficientTopology |
| `spec.rules[].candidateSelectors[].scope` | WithinLocalQueue/WithinClusterQueue/WithinParentCohort/WithinCohortTree/AnyClusterQueue |
| `spec.rules[].candidateSelectors[].priority` | `mode` Base/Boosted + `comparison` |
| `spec.rules[].candidateSelectors[].numericLabels[]` | `key`、`comparison`、`fallbackValue`、`minValue`、`maxValue` |
| `spec.rules[].candidateSelectors[].labelSelector` | 按 victim 标签过滤 |
| `spec.rules[].candidateSelectors[].clusterQueueSelector` | 按 victim CQ 标签过滤 |

CQ 通过注解 `kueue.x-k8s.io/preemption-config-name` 引用；MiniKueue 用 `spec.preemptionConfigName`。

### ResourceTransformation（MiniKueue）

| 字段 | 说明 |
|---|---|
| `Input` | 输入资源 |
| `Strategy` | Replace/Retain |
| `MultiplyBy` | 资源名或固定整数 |
| `Outputs` | 每单位 input 的产出 |

### 控制器配置（MiniKueue）

| 配置 | 说明 |
|---|---|
| `Engine.Provisioning` | `CheckName`/`DelayTicks`/`Failures`，模拟 ProvisioningRequest |
| `Engine.Backoff` | `BaseTicks`/`MaxTicks`/`Multiplier`，指数退避 |
| `Engine.FairSharingPreemption` | `LessThanOrEqualToFinalShare`/`LessThanInitialShare` |
| `Engine.ResourceTransformations` | 资源变换列表 |
| `Engine.PreemptionConfigs` | Configurable Preemption 规则 |

## 23.2.1 API 组

| API 组 | 用途 |
|---|---|
| `kueue.x-k8s.io/v1beta2` | Workload、LocalQueue、ClusterQueue、Cohort、ResourceFlavor、AdmissionCheck、Topology、ProvisioningRequestConfig |
| `config.kueue.x-k8s.io/v1beta2` | Kueue 控制器配置 |
| `kueue.x-k8s.io/v1alpha1` | 部分 alpha 资源，例如 PreemptionConfig、CapacityProvider、DynamicQuotaOrchestrator |
| `visibility.kueue.x-k8s.io` | Visibility API（按版本/安装情况） |

具体 kind/version 以目标版本的 API reference 为准。

### 23.2.2 常用 feature gate 速查

| Gate | 典型状态 | 作用 |
|---|---|---|
| `TopologyAwareScheduling` | beta 默认开 | TAS 总开关 |
| `TASFailedNodeReplacement` | beta 默认开 | TAS 节点热替换 |
| `TASReplaceNodeOnPodTermination` | beta 默认开 | Pod 终止时替换 |
| `TASFailedNodeReplacementFailFast` | beta 默认开 | 替换失败快速放弃 |
| `TASReplaceNodeOnNodeTaints` | beta 默认开 | 污点触发替换 |
| `TASBalancedPlacement` | alpha 默认关 | 均衡放置 |
| `TASMultiLayerTopology` | beta 默认开 | 多层 slice |
| `TASTopologySpreading` | alpha 默认关 | 跨 Workload 打散 |
| `TASPartialSlices` | beta 默认开 | 尾部 partial slice |
| `KueueDRAIntegration` | beta 默认开 | DRA 总开关 |
| `KueueDRAIntegrationExtendedResource` | beta 默认开 | 扩展资源路径 |
| `KueueDRAIntegrationConsumableCapacity` | 0.21-devel 计划 beta | 共享设备容量计费；v0.20 默认值按 release notes 核对 |
| `KueueDRADeviceFeasibility` | alpha 默认关 | TAS 设备可行性 |
| `KueueDRAIntegrationDeviceTaints` | alpha 默认关 | 设备污点 |
| `MultiKueue` | beta 默认开 | 多集群 |
| `ConcurrentAdmission` | alpha 默认关 | 并发准入 |
| `DynamicQuotaOrchestration` | alpha 默认关 | DQO |
| `AdmissionFairSharing` | beta 默认开 | AFS |
| `AdmissionFairSharingAnchorAtQuotaReservation` | beta 默认开 | AFS 计量锚点 |
| `ConfigurablePreemptions` | alpha 默认关 | PreemptionConfig |
| `WorkloadRequestUseMergePatch` | alpha 默认关 | Workload 用 Merge Patch |
| `MultiKueueKubeConfigPathValidation` | beta 默认开 | 限制 kubeconfig 路径 |
| `LocalQueueDefaulting` | stable（v0.17+） | 名字为 `default` 的 LocalQueue 自动补 queue-name 标签 |
| `WorkloadPriorityClassDefaulting` | beta 默认开（v0.20） | 名字为 `default` 的 WorkloadPriorityClass 自动补优先级标签 |
| `ManagedJobsNamespaceSelectorAlwaysRespected` | GA 锁定（v0.19+） | `managedJobsNamespaceSelector` 总是限制 reconcile |
| `HighMaxParallelismWithinReconcile` | beta（v0.20） | 单次 reconcile 的并发更新上限 8→32 |
| `PrioritySortingWithinCohort` | beta | Cohort 内优先级排序 |
| `TASResidualPodAccounting` | alpha 默认关 | 残留 Pod 记账 |
| `PartialAdmission` | beta | 部分准入；默认值按版本核对 |
| `ElasticJobsViaWorkloadSlices` | beta | 弹性 Workload Slices |
| `ElasticJobsViaWorkloadSlicesWithPartialReplicaScaleUp` | alpha 默认关 | 部分副本扩容策略 |
| `MultiKueueOrchestratedPreemption` | beta 默认开 | 编排式抢占（KEP-8303） |
| `MultiKueueRemoteSpecSync` | beta | manager→worker spec 同步 |
| `GenericWorkload`（Kubernetes） | Beta 默认关 | 原生 WAS/gang；K8s 1.37 |

gate 名称和默认值随版本变化；升级前以 release notes 为准。不要只关一个 gate 而不管依赖链。

### 23.2.3 Configuration 常用字段

Kueue 控制器通过 `config.kueue.x-k8s.io/v1beta2` 的 `Configuration` 配置。下表是排障和评审时最常看的字段，完整 schema 以目标版本的 `Configuration` API reference 为准。

| 区块 | 字段 | 作用 |
|---|---|---|
| （顶层） | `manageJobsWithoutQueueName` | 是否管理无 queue-name 的对象；**不会**自动选队列 |
| | `managedJobsNamespaceSelector` | 只 reconcile 匹配的 namespace；v0.19+ 总是生效 |
| | `featureGates` | alpha/beta gate 开关（v1beta2 是顶层 `map[string]bool`，不能和 `--feature-gates` 同时用） |
| （顶层） | `leaderElection` | 选主配置（`leaderElect`、`resourceName`） |
| | `metrics.bindAddress`、`metrics.customLabels` | 指标地址与自定义标签 |
| | `webhook.port` | webhook 监听端口（证书管理见 `internalCertManagement`） |
| | `health.healthProbeBindAddress` | 健康探针 |
| | `controller.groupKindConcurrency` | 各 kind 的 reconcile 并发 |
| | `clientConnection.qps`、`burst` | 控制器到 API server 的限速 |
| `integrations` | `frameworks` | 启用的作业框架 |
| | `labelKeysToCopy` | 复制到 Workload 的标签 |
| `queueVisibility` | `updateIntervalSeconds`、`clusterQueues.maxCount` | Visibility 快照频率/保留数 |
| `waitForPodsReady` | `timeout`、`recoveryTimeout`、`unscheduledTimeout` | 就绪/恢复/未调度超时 |
| | `blockAdmission`、`requeuingBackoffLimitCount` | 阻塞式准入与重排队上限 |
| `fairSharing` | `preemptionStrategies` | 公平抢占策略组合 |
| `admissionFairSharing` | `usageHalfLifeTime`、`usageSamplingInterval`、`resourceWeights` | AFS 半衰期/采样/权重 |
| `multiKueue` | `gcInterval`、`workerLostTimeout`、`dispatcherName`、`incrementalDispatcherConfig` | 多集群清理/失联/分发 |
| `resources` | `transformations`、`deviceClassMappings` | 资源变换与 DRA 映射 |
| `internalCertManagement` | `enable`、`webhookCertName`、`webhookServiceName` | 内置证书管理 |

两个常见误区：

1. `manageJobsWithoutQueueName: true` 只负责"接管"，**不会**给作业挑默认队列；没有 LocalQueue 的 Workload 仍然无法准入。默认队列请用名字为 `default` 的 LocalQueue + `LocalQueueDefaulting`。
2. `managedJobsNamespaceSelector` 自 v0.19 起总是限制 reconcile，不只是 `manageJobsWithoutQueueName: true` 时生效。启用后，所有被管理的 namespace 都必须带匹配标签，否则作业会静默不受管理。

### 23.2.4 条件、原因与事件

**Workload 常见条件：**

| 条件 | True 的含义 | 常见 Reason |
|---|---|---|
| `QuotaReserved` | 逻辑配额已预留 | `Reserved`、`Pending`、`InsufficientQuota`、`Inadmissible` |
| `Admitted` | 所有 AdmissionCheck 通过 | `Admitted`、`AdmissionChecks`、`Finished` |
| `Evicted` | 曾被驱逐 | `Preempted`、`PodsReadyTimeout`、`AdmissionCheck`、`Stopped`、`Deactivated`、`MaximumExecutionTime` |
| `Preempted` | 被抢占的细分原因 | `InClusterQueue`、`InCohort` |
| `Finished` | 作业完成，配额释放 | `Completed` |
| `AdmissionGated` | 外部 gate 未清除 | `AdmissionGated` |
| `PodsReady` | 所有 Pod 就绪 | `PodsReady`、`WaitForStart` |

**常见事件：**

| 事件 | 触发 |
|---|---|
| `QuotaReserved` / `Admitted` | 准入阶段 |
| `Evicted` / `Preempted` | 驱逐/抢占 |
| `AdmissionGated` / `AdmissionGateCleared` | gate 生命周期 |
| `ProvisioningRetry` / `ProvisioningReady` | ProvisioningRequest 置备 |
| `Migrated` | Concurrent Admission 迁移 |
| `PodsReadyTimeout` | WaitForPodsReady 超时 |
| `Reclaimed` | 动态回收 |
| `AdmissionCheckRejected` | AdmissionCheck 永久失败，Workload 被停用 |

事件是审计和排障的辅助证据；最终判断仍以 `status.conditions` 为准。

### 23.2.5 常见错误信息与处理

| 错误/现象 | 常见原因 | 处理 |
|---|---|---|
| `resourceGroups` / `coveredResources` 校验失败 | CQ schema 不合法 | 补资源组、coveredResources、每个 Flavor 的 nominal |
| `flavor ... does not define nominal quota` | 某 Flavor 缺少资源配额 | 显式写 `nominalQuota: 0` 或实际值 |
| `namespaceSelector` 不匹配 | CQ selector 与 Namespace 标签不符 | 改标签/selector；注意 nil 与 `{}` |
| `QuotaReserved=False, Reason=Inadmissible`（message/LimitRange） | Pod 请求不满足 LimitRange | 检查 namespace 的 LimitRange 和 requests/limits |
| `QuotaReserved=False, Reason=InsufficientQuota` | 配额/Cohort 总量/借用上限不足 | 按第 5 章公式逐层检查 |
| `QuotaReserved=True, Admitted=False` | AdmissionCheck 未 Ready | 看 `status.admissionChecks` 和外部控制器 |
| `AdmissionGated=True` | 外部 gate 未清除 | 找 gate owner，不要加配额 |
| Pod `FailedScheduling: didn't match node affinity/selector` | Flavor 注入/节点标签不一致 | 对比 Flavor、Pod、Node 标签 |
| Pod `untolerated taint` | 用户没容忍 `nodeTaints` | 在 Pod 模板加 toleration |
| `ResourceClaim` 未分配 | DRA driver/设备问题 | 看 DeviceClass、ResourceSlice、driver 日志 |
| Workload 卡在 `Terminating` | finalizer 未清理 | 先恢复控制器，不要 force delete CRD |

### 23.2.6 MultiKueue、弹性与扩展对象速查

| 对象/字段 | 作用 | 关键点 |
|---|---|---|
| `MultiKueueCluster.spec.clusterSource.kubeConfig` | worker 连接 | Secret 在 `kueue-system`，key=`kubeconfig` |
| `MultiKueueCluster.spec.clusterSource.clusterProfileRef` | ClusterProfile 连接 | 需要 `MultiKueueClusterProfile` gate |
| `MultiKueueConfig.spec.clusters` | worker 集合/顺序 | 影响 AllAtOnce/Incremental 顺序 |
| `AdmissionCheck.spec.controllerName` | 控制器所有权 | MultiKueue 用 `kueue.x-k8s.io/multikueue` |
| `Workload.status.nominatedClusterNames` | 本轮提名 | 可观测 |
| `Workload.status.clusterName` | 最终选中 | 设置后不可变 |
| `PodSet.minCount` | Partial Admission | 只允许一个 PodSet；`minCount ≤ count` |
| `kueue.x-k8s.io/job-min-parallelism` | Job 的最低并行度 | 需要 `PartialAdmission` gate |
| `kueue.x-k8s.io/elastic-job` | 启用 Elastic Slices | 与 Partial Admission 互斥 |
| `kueue.x-k8s.io/workload-slice-replacement-for` | 新 slice 替换旧 slice | 扩容路径 |
| `kueue.x-k8s.io/podset-required-topology` | TAS 必选层级 | spreading 的前置 |
| `kueue.x-k8s.io/podset-topology-spreading` | 跨 Workload 打散 | alpha `TASTopologySpreading` |
| `AdmissionCheckState.state` | 检查状态 | Pending/Ready/Retry/Rejected |
| `AdmissionCheckState.requeueAfterSeconds` | Retry 退避 | 优先于已废弃的 `retryDelayMinutes` |

## 23.3 常用 kubectl / kueuectl 命令

```bash
# kueuectl
kubectl kueue version
kubectl kueue list workload -A
kubectl kueue list localqueue -n default
kubectl kueue list clusterqueue
kubectl kueue stop clusterqueue <name>
kubectl kueue resume clusterqueue <name>
kubectl kueue stop localqueue <name> -n <ns>
kubectl kueue resume localqueue <name> -n <ns>

# 队列
kubectl get clusterqueues -o wide
kubectl describe clusterqueue <name>
kubectl get localqueues -A -o wide
kubectl get queues -n <ns>

# Workload
kubectl get workloads -A
kubectl get workload <name> -n <ns> -o wide
kubectl describe workload <name> -n <ns>
kubectl get workload <name> -n <ns> -o yaml

# 作业
kubectl get jobs -A
kubectl describe job <name> -n <ns>
kubectl get pods -n <ns> -o wide

# 事件
kubectl get events -A --sort-by=.lastTimestamp
kubectl describe <resource> <name> -n <ns>

# 控制器
kubectl get pods -n kueue-system
kubectl logs -n kueue-system deploy/kueue-controller-manager -f

# Flavor/Check/Topology
kubectl get resourceflavors
kubectl get admissionchecks
kubectl get topologies

# MultiKueue
kubectl get multikueueclusters
kubectl get multikueueconfigs
kubectl get workload <name> -n <ns> \
  -o jsonpath='{.status.nominatedClusterNames}{"\n"}{.status.clusterName}{"\n"}'
kubectl get workloads -A -o custom-columns=\
NAME:.metadata.name,CLUSTER:.status.clusterName,NOMINATED:.status.nominatedClusterNames

# 指标（如果已配置抓取）
kubectl port-forward -n kueue-system svc/kueue-controller-manager-metrics-service 8443:8443
curl -k https://localhost:8443/metrics
```

## 23.4 最小清单

`examples/kueue/quick-start-setup.yaml`：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: default-flavor
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: cluster-queue
spec:
  namespaceSelector: {}
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: default-flavor
      resources:
      - name: cpu
        nominalQuota: 9
      - name: memory
        nominalQuota: 9Gi
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: default
  name: user-queue
spec:
  clusterQueue: cluster-queue
```

`examples/kueue/quick-start-job.yaml`：

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: sample-job
  namespace: default
  labels:
    kueue.x-k8s.io/queue-name: user-queue
spec:
  parallelism: 3
  completions: 3
  template:
    spec:
      containers:
      - name: dummy-job
        image: registry.k8s.io/e2e-test-images/agnhost:2.53
        command: ["/bin/sh"]
        args: ["-c", "sleep 30"]
        resources:
          requests:
            cpu: "1"
            memory: "200Mi"
      restartPolicy: Never
```

## 23.5 指标速查

| 指标 | 类型 | 用途 |
|---|---|---|
| `kueue_pending_workloads` | gauge | pending 数 |
| `kueue_admitted_active_workloads` | gauge | 活跃准入数 |
| `kueue_reserving_active_workloads` | gauge | 已预留未准入 |
| `kueue_admitted_workloads_total` | counter | 累计准入 |
| `kueue_quota_reserved_workloads_total` | counter | 累计预留 |
| `kueue_evicted_workloads_total` | counter | 累计驱逐 |
| `kueue_evicted_workloads_once_total` | counter | 按原因去重驱逐 |
| `kueue_preempted_workloads_total` | counter | 累计抢占 |
| `kueue_cluster_queue_resource_usage` | gauge | CQ 资源使用 |
| `kueue_cluster_queue_nominal_quota` | gauge | CQ nominal |
| `kueue_cluster_queue_weighted_share` | gauge | 加权份额 |
| `kueue_admission_wait_time_seconds` | histogram | 排队时间 |
| `kueue_quota_reserved_wait_time_seconds` | histogram | 到预留的等待 |
| `kueue_ready_wait_time_seconds` | histogram | 到 Pod Ready 的等待 |
| `kueue_admission_checks_wait_time_seconds` | histogram | 检查等待 |
| `kueue_local_queue_admission_fair_sharing_usage` | gauge | AFS 历史用量 |
| `kueue_local_queue_pending_workloads` | gauge | LQ pending 数 |
| `kueue_cohort_weighted_share` | gauge | Cohort 加权份额 |
| `kueue_unadmitted_workloads` | gauge | 未准入数 |
| `kueue_build_info` | gauge | 版本 |

## 23.6 调试检查清单

### CQ 不 Active

- [ ] ResourceFlavor 存在？
- [ ] AdmissionCheck 存在且 Active？
- [ ] Cohort 存在？
- [ ] 资源组/Flavor schema 合法？
- [ ] webhook 正常？
- [ ] namespaceSelector 是否过窄？

### Workload Pending

- [ ] Job 有 queue-name 标签？
- [ ] LocalQueue 存在且指向正确？
- [ ] CQ 配额足够？
- [ ] Cohort 总量足够？
- [ ] borrowingLimit/lendingLimit 是否卡住？
- [ ] 是否被 StopPolicy/Inactive？
- [ ] 是否需要抢占但策略禁止？
- [ ] 是否 namespaceSelector 不匹配？
- [ ] 是否 Flavor 不存在？
- [ ] 是否 TAS/DRA 约束？
- [ ] 是否 DQO 把 effectiveQuotas 调低了？
- [ ] 是否 WaitForPodsReady 超时后仍在退避？

### QuotaReserved 但未 Admitted

- [ ] AdmissionCheck 状态？
- [ ] ProvisioningRequest 是否创建？
- [ ] 云容量是否可用？
- [ ] 外部控制器日志？
- [ ] 是否 Retry/Rejected？

### 作业运行但 Pod Pending

- [ ] Kueue 层已放行？
- [ ] nodeSelector/toleration 是否匹配？
- [ ] 节点资源碎片？
- [ ] 卷/网络/GPU 设备？
- [ ] kube-scheduler 事件？
- [ ] TAS/DRA 分配失败？

### 抢占异常

- [ ] 优先级配置？
- [ ] withinClusterQueue？
- [ ] reclaimWithinCohort？
- [ ] borrowWithinCohort？
- [ ] Fair Sharing 策略？
- [ ] 是否有对称规则/级联？
- [ ] 驱逐率/重排队退避？

## 23.7 练习

### 练习 1：手算配额

Cohort：A nominal 8，B nominal 4，总 12。A 使用 6，B 使用 4。A 再提交 3：

- 新 A 用量 9，借 1；
- Cohort 总量 6+4+3=13 > 12；
- 结果？

<details>
<summary>答案</summary>
直接失败。除非 A 能回收 B 的借用（B 使用 4 = nominal 4，不算借用；A 的 reclaim 可能不适用），或者 Cohort 有额外共享额度。A 需要等 B 释放或调整配额。
</details>

### 练习 2：borrowingLimit

A nominal 10，B nominal 10，B 的 borrowingLimit=2。A 使用 0，B 使用 10。B 提交 3：

- B 新用量 13，借 3；
- Cohort 总量 13 ≤ 20；
- 结果？

<details>
<summary>答案</summary>
失败：借 3 > borrowingLimit 2。
</details>

### 练习 3：lendingLimit

A nominal 10，lendingLimit=3；B nominal 10。A 使用 0，B 使用 8。B 提交 5：

- B 新用量 13，借 3；
- Cohort 总量 13 ≤ 20；
- 结果？

<details>
<summary>答案</summary>
成功：借 3 ≤ lendingLimit 3，且 Cohort 总量足够。
</details>

### 练习 4：StrictFIFO

CQ 2 CPU。big（3 CPU）先到，small（1 CPU）后到。StrictFIFO 下谁会运行？

<details>
<summary>答案</summary>
都不会。big 放不下，阻塞队头；small 不能插队。
</details>

### 练习 5：抢占

CQ 2 CPU，`withinClusterQueue: LowerPriority`。low（2 CPU, priority 0）已准入，high（1 CPU, priority 10）提交。结果？

<details>
<summary>答案</summary>
high 抢占 low，low 被驱逐并重排队，high 准入。
</details>

### 练习 6：回收

Cohort A/B 各 4。A 使用 2，B 使用 6（借 2）。A 提交 2，`reclaimWithinCohort: LowerPriority`，B 优先级更低。结果？

<details>
<summary>答案</summary>
A 新用量 4（在 nominal 内），Cohort 总量 2+6+2=10 > 8；回收 B 的借用，驱逐 B，A 准入。
</details>

### 练习 7：TAS 手算

两个 rack，每个 rack 两台 host，每台 host 2 GPU。一个 4 GPU 作业要求 `podset-required-topology: rack`。它能否准入？如果每台 host 只有 1 GPU 呢？

<details>
<summary>答案</summary>
第一个可以：rack 聚合容量是 4 GPU，作业跨两台 host 占满一个 rack。第二个不行：rack 聚合容量只有 2 GPU，Required 模式找不到能容纳 4 GPU 的 domain；如果改成 Preferred，则退化为无拓扑约束（但物理上仍可能放不下）。
</details>

### 练习 8：WaitForPodsReady

CQ 2 CPU，两个 1 CPU 作业同时准入，`Timeout=3`，`MaxRetries=0`。其中一个在第 2 tick 报告 ready，另一个一直不报告。第 4 tick 会发生什么？

<details>
<summary>答案</summary>
未就绪的作业在第 4 tick 满足 `clock - AdmittedAt >= 3`，被驱逐并进入重排队；已就绪的作业保持 Admitted。被驱逐作业的配额释放，但退避期内不会立即重新准入。
</details>

### 练习 9：动态回收

CQ 4 CPU，作业 A 有 2 个 Pod、每 Pod 2 CPU，作业 B 需要 2 CPU。A 准入后 B Pending。报告 A 回收 1 个 Pod 后，Cache 和 B 的状态如何变化？

<details>
<summary>答案</summary>
A 的有效 Pod 数变为 1，Cache 从 4 CPU 降到 2 CPU；下一个 tick B 可准入，总用量回到 4 CPU。回收是单向的，不能把 1 个 Pod 再要回来。
</details>

### 练习 10：DQO-lite

CapacityProvider 报告 6000m CPU，两个 CQ 的静态 nominal 都是 4000m。它们的有效配额各是多少？如果 provider 降到 4000m 呢？

<details>
<summary>答案</summary>
各 3000m（按 1:1 比例分配）。降到 4000m 时各 2000m。已经准入的作业不会被驱逐；只有新的准入使用新的有效配额。
</details>

### 练习 11：MiniKueue 扩展

给 MiniKueue 加一个"最多 Pod 数"测试：CQ 覆盖 `pods`，Workload 有 3 个 Pod，配额 2。预测结果并写测试。

<details>
<summary>提示</summary>
`TotalUsage` 会自动加 `pods`；如果 CQ 的 `coveredResources` 包含 `pods`，`canFitFlavor` 会检查。测试可参考 `TestCohortBorrowingAndCapacity`。
</details>

### 练习 12：selector 语义

CQ 的 selector 是：

```yaml
matchLabels:
  environment: production
matchExpressions:
- key: team
  operator: In
  values: [blue, green]
```

namespace 的标签是 `environment=production, team=red`。结果？

<details>
<summary>答案</summary>
不匹配：`team=red` 不在 In 的 values 里。Workload 进入 Pending，`QuotaReserved=False`、`Reason=Inadmissible`（message 指向 NamespaceSelector）；加配额无效，必须改标签或 selector。
</details>

### 练习 13：LocalQueue Hold

CQ 有 `open` 和 `held` 两个 LQ，`held` 的 stopPolicy=Hold。一个 Workload 通过 `held` 提交，另一个通过 `open` 提交。谁会运行？

<details>
<summary>答案</summary>
`open` 的 Workload 可以准入；`held` 的 Pending。LQ stopPolicy 只影响自己的 Workload，不阻塞同 CQ 的其他 LQ。
</details>

### 练习 14：Retry 退避

AdmissionCheck 在 tick 1 进入 Retry，`requeueAfterSeconds=300`。Kueue 什么时候重新尝试？

<details>
<summary>答案</summary>
在 `lastTransitionTime + 300s` 之后。如果检查控制器不设置 `requeueAfterSeconds`，Kueue 会立即重试；这容易造成忙循环，所以检查控制器应自己计算退避并写入该字段（ProvisioningRequest 用 `retryCount` + backoff 就是这么做的）。
</details>

### 练习 15：SchedulingStats

一个 Workload 被抢占 2 次，又因 PodsReadyTimeout 1 次。`status.schedulingStats` 长什么样？

<details>
<summary>答案</summary>
```yaml
status:
  schedulingStats:
    evictions:
    - reason: Preempted
      underlyingCause: ""
      count: 2
    - reason: PodsReadyTimeout
      underlyingCause: WaitForStart
      count: 1
```
指标 `kueue_evicted_workloads_once_total` 按 `(reason, underlyingCause)` 去重，不是按次数。
</details>

### 练习 16：DQO 分配

CapacityProvider 报告 10 CPU，两个 CQ 的 nominal 是 6 和 4。它们各拿多少？如果 provider 报告 3 CPU 呢？

<details>
<summary>答案</summary>
10 CPU 按 6:4 分，各 6 和 4；3 CPU 各 1.8 和 1.2（实际按最小单位取整/余数分配，以版本规则为准）。
</details>

### 练习 17：TAS partial slice

10 个 Pod，`sliceSize: 4`，每个 host 2 卡。会怎么切？最后一个 partial slice 能不能跨 host？

<details>
<summary>答案</summary>
切成 4+4+2。每个 slice（包括 2 个 Pod 的 partial slice）必须在一个 topology domain 内；2 个 Pod 需要 2 卡，可以放在一个 host。partial slice 不能跨 host。
</details>

### 练习 18：DRA 重复计费

一个 PodSet 有 4 个 Pod，每个 Pod 同时写 `nvidia.com/gpu: 2` 和一个映射到 `nvidia.com/gpu` 的设备请求（`Count: 1`）。`TotalUsage` 里 GPU 是多少？

<details>
<summary>答案</summary>
每个 Pod 取 `max(2, 1) = 2`，4 个 Pod 共 8。不是 `(2+1)×4 = 12`。
</details>

### 练习 19：Cohort 级 borrowingLimit

`org-a` 的 Cohort borrowingLimit 是 2 CPU，其下只有一个 CQ nominal 4。CQ 当前用量 6（借 2）。再提交一个 1 CPU 作业，能否准入？

<details>
<summary>答案</summary>
不能。新用量 7，总借用 3；子树已有借用 2，新增借用 1，2+1>2，超过 Cohort 级上限。即使总 Cohort 还有空闲也拒绝。
</details>

### 练习 20：AFS 排序

两个 LocalQueue 在同一 CQ：`heavy` 历史用量 10，`light` 历史用量 1，权重都是 1。CQ 只能再放一个作业。谁先被尝试？

<details>
<summary>答案</summary>
`light`。AFS 按 `history/weight` 升序，1 < 10。AFS 只影响排队顺序，不改变配额。
</details>

### 练习 21：Concurrent Admission 迁移

Workload 当前在 Flavor 索引 1（spot），索引 0（reserved）配额从 0 提到足够。下一 tick 会发生什么？会迁移回去吗？

<details>
<summary>答案</summary>
控制器用排除自身的视图重新计算，发现索引 0 可行，于是释放 spot 预留并重新预留到 reserved。只允许向更小索引迁移，因此不会再迁回 spot。
</details>

### 练习 22：TAS slice

`count=10`、`SliceSize=4`，每个 host 4 卡。会切成几片？每片多少 Pod？

<details>
<summary>答案</summary>
4+4+2，共 3 片。最后 2 个 Pod 是 partial slice，仍然必须在一个 domain 内。
</details>

### 练习 23：retryCount 的持久化

AdmissionCheck 在 tick 1 进入 Retry，`requeueAfterSeconds=1`。tick 2 释放配额并重新预留后，`status.admissionChecks[].retryCount` 应该是多少？如果实现把 `AdmissionChecks` 清空后重建，会发生什么？

<details>
<summary>答案</summary>
仍然是 1；tick 3 再次进入 Retry 时应变成 2。如果重建时把 retryCount 归零，每次 Retry 都会显示 1，观测和重试上限判断都会失真。
</details>

### 练习 24：TAS 回收的拓扑账本

一个 4 Pod 的 TAS 作业在 hostname 层占满 h1（4 卡），报告回收 2 个 Pod。`FlavorAssignment` 和 `TopologyAssignment` 应分别怎么变？只改前者会怎样？

<details>
<summary>答案</summary>
配额用量从 4 卡降到 2 卡；TopologyAssignment 的 PodCount 从 4 降到 2、Resources 按每 Pod 请求缩减（如果有多条 slice assignment，从尾部开始缩）。只改 FlavorAssignment 会让 h1 仍显示 4 卡占用，后续 TAS 作业可能误判"拓扑没空间"。
</details>

### 练习 25：最大执行时间的累计

作业 A 的 `maximumExecutionTimeSeconds=2`，tick 1 准入，tick 2 被高优先级作业抢占，tick 4 重新准入。tick 4 结束时累计执行时间是多少？tick 5 会怎样？

<details>
<summary>答案</summary>
tick 2 时累计 1 tick；tick 4 重新准入后当前周期从 0 开始，但历史累计仍是 1。tick 5 结束时累计 1+1=2，达到上限，作业被停用。如果每次重新准入都清零，它会一直跑下去。
</details>

### 练习 26：Cohort 借用上限的空闲兄弟

`org-a` 下有两个 CQ：a1 nominal 4 空闲，a2 nominal 4 已用 7（借 3）。`org-a` 的 Cohort borrowingLimit=2，a2 再请求 1。按"子树总用量 - 总 nominal"净额计算和按"每个成员正借用求和"计算，结果有什么不同？

<details>
<summary>答案</summary>
净额法：总用量 7、总 nominal 8，借用为 0，误判可以再借。逐成员法：a2 已借 3，新增 1 后为 4，超过 limit 2，拒绝。后者才是子树上限的正确语义。
</details>

### 练习 27：资源变换

Pod 请求 `example.com/gpu=4, gpumem=512, gpucores=16`，变换为：

```yaml
- input: example.com/gpumem
  strategy: Replace
  multiplyBy: example.com/gpu
  outputs: {example.com/total-gpumem: "1"}
- input: example.com/gpucores
  strategy: Replace
  multiplyBy: example.com/gpu
  outputs: {example.com/total-gpucores: "1"}
- input: example.com/gpu
  strategy: Replace
  outputs: {}
```

配额账本看到什么？如果 `multiplyBy` 写成固定整数 `2`，结果有什么不同？

<details>
<summary>答案</summary>
第一组：total-gpumem=512×4=2048，total-gpucores=16×4=64，gpu 被忽略。固定整数 2 时倍率与 Pod 请求的 gpu 无关：total-gpumem=512×2=1024，total-gpucores=16×2=32；gpu 仍被忽略。
</details>

### 练习 28：Configurable Preemption 的 scope

一个 CQ 的 `PreemptionConfig` 规则 scope 是 `WithinParentCohort`。victim 在同一个 CQ、同一个父 Cohort 的兄弟 CQ、或者根 Cohort 下另一个子树，分别是否匹配？

<details>
<summary>答案</summary>
同一个 CQ 不匹配（它是"父 Cohort 内"，通常指兄弟/上级范围，MiniKueue 显式排除自身 CQ）；同一个父 Cohort 的兄弟 CQ 匹配；另一个子树不匹配。MiniKueue 用根 Cohort 近似父 Cohort，因此"同根但不同父"也会被匹配，这是已知简化。
</details>

### 练习 29：Fair Sharing 抢占

Cohort 内 A 借用了 4 CPU，B 在 nominal 内。B 提交的作业让 Cohort 超限。驱逐 A 的一个 Workload 后：

```text
preemptorNewShare = 0
targetOldShare    = 0.8
targetNewShare    = 0
```

两种 strategy 分别是否允许？

<details>
<summary>答案</summary>
`LessThanOrEqualToFinalShare`: 0 <= 0，允许。`LessThanInitialShare`: 0 < 0.8，允许。两种都允许。关键区别是：Final 比较驱逐后的 target share，Initial 比较驱逐前的 target share；如果 targetNew 仍为 0.5，Final 的 0 <= 0.5 仍然允许，但 Initial 的 0 < 0.8 也允许——真正会拒绝的情形是 preemptorNew 大于 targetNew（Final）或不小于 targetOld（Initial）。
</details>

### 练习 30：ProvisioningRequest 时间线

`DelayTicks=3, Failures=2`，tick 1 首次预留。第二次重试的 RequeueAt 是多少？第几次预留后才可能 Ready？

<details>
<summary>答案</summary>
tick 1 第一次失败，RequeueAt=1+3=4；tick 4 重新预留，retryCount=1 < 2，第二次失败，RequeueAt=4+3=7；tick 7 第三次预留，retryCount=2，等待 3 tick，tick 10 Ready。即第三次预留后才可能 Ready。
</details>

### 练习 31：MultiKueue 失联超时

`WorkerLostTimeout=2`。tick 10 选中 worker-b；tick 11、12 worker-b 不可达；tick 13 仍不可达。哪些 tick 会产生 `WorkerUnavailable`，哪个 tick 会 `WorkerLost`？

<details>
<summary>答案</summary>
tick 11：1/2，WorkerUnavailable；tick 12：2/2，WorkerUnavailable；tick 13：3/2 超过阈值，WorkerLost 并重新分发。注意计数器从选中 worker 第一次被观察到不可达开始，`<= timeout` 的 tick 都只是等待。
</details>

### 练习 32：Partial Admission 的二分

Workload 请求 10 个 Pod，`minCount=4`，CQ 当前只能放下 7 个。Flavor 分配器应该在哪个区间二分？最终 `status.admission.podSetAssignments[*].count` 是多少？

<details>
<summary>答案</summary>
在 `[4, 10]` 内二分，找最大可放置值；当前能放 7 个，所以结果是 7。注意二分的是"副本数"而不是资源量，且只允许一个 PodSet 使用 minCount。
</details>

### 练习 33：Partial Admission 与 Elastic Slices

一个 Job 同时设置了 `kueue.x-k8s.io/job-min-parallelism` 和 `kueue.x-k8s.io/elastic-job: "true"`。会发生什么？

<details>
<summary>答案</summary>
Webhook 会拒绝这种组合：Partial Admission 与 Elastic Workload Slices 互斥。应二选一。
</details>

### 练习 34：TAS spreading 的份额

`maxShareAllowingPlacement=0.45`，`enforcementMode=Preferred`。已有两个 zone 各放了 45% 的副本。第三个副本应该放在哪里？如果模式是 Required 呢？

<details>
<summary>答案</summary>
Preferred：前两个 zone 已接近上限，第三个副本优先放第三个 zone；如果没有第三个 zone，允许回退到已有 zone。Required：超过 45% 的 zone 被排除，如果没有可用域，副本可能无法放置。`0.45` 大约对应 3 个域（`ceil(1/0.45)=3`）。
</details>

### 练习 35：Deployment 的逐 Pod 准入

一个 Deployment 扩容到 100 副本，Kueue 队列只能再容纳 60 个 Pod。会发生什么？Deployment 会不会整体 Pending？

<details>
<summary>答案</summary>
不会整体 Pending。Deployment 通过 Pod 集成逐 Pod 管理：60 个 Pod 被准入并放行，另外 40 个被 scheduling gate 挡住，处于 Pending。缩容时删除的 Pod 会释放对应配额。滚动更新时新旧 Pod 会同时计入配额，所以 `maxSurge` 要预留空间。
</details>

### 练习 36：AdmissionCheck 的状态迁移

一个检查从 Pending 变成 Retry，再变成 Ready。配额会发生什么？如果变成 Rejected 呢？

<details>
<summary>答案</summary>
Pending → Retry：Kueue 释放配额，按退避重新排队，之后重新预留；Retry → Ready：因为 Retry 已经释放了配额，Ready 之后必须重新预留成功，全部检查 Ready 才能 Admitted，不存在"保留当前预留"的捷径。Pending → Rejected：Workload 被停用，不会自动重试。Retry 是"暂时不行"，Rejected 是"永远不行"。
</details>

## 23.8 参考资料

### 官方

- Kueue 文档：<https://kueue.sigs.k8s.io/>
- Kueue 快速开始：<https://kueue.sigs.k8s.io/docs/getting-started/quick-start/>
- Kueue 安装：<https://kueue.sigs.k8s.io/docs/installation/>
- Kueue v1beta2 API：<https://kueue.sigs.k8s.io/docs/reference/kueue.v1beta2/>
- Kueue 指标：<https://kueue.sigs.k8s.io/docs/reference/metrics/>
- Kueue GitHub：<https://github.com/kubernetes-sigs/kueue>
- Kueue KEPs：<https://github.com/kubernetes-sigs/kueue/tree/main/keps>
- Kueue WAS 集成 RFC：<https://github.com/kubernetes-sigs/kueue/issues/12385>
- Kueue v0.20 release notes：<https://github.com/kubernetes-sigs/kueue/releases/tag/v0.20.0>

### Kubernetes

- Scheduling：<https://kubernetes.io/docs/concepts/scheduling-eviction/>
- Resource Quotas：<https://kubernetes.io/docs/concepts/policy/resource-quotas/>
- Priority and Preemption：<https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/>
- DRA：<https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/>
- Pod Group / Gang Scheduling（KEP）：<https://github.com/kubernetes/enhancements>
- Kubernetes 1.37 Workload-Aware Scheduling：<https://kubernetes.io/blog/2026/09/08/kubernetes-v1-37-advancing-workload-aware-scheduling/>

### 生态

- JobSet：<https://jobset.sigs.k8s.io/>
- KubeRay：<https://docs.ray.io/en/latest/cluster/kubernetes/index.html>
- Kubeflow Training Operator：<https://www.kubeflow.org/docs/components/training/>
- LeaderWorkerSet：<https://github.com/kubernetes-sigs/lws>
- Cluster Autoscaler：<https://github.com/kubernetes/autoscaler>

### 本教程

- MiniKueue 代码：`../mini-kueue/`
- MiniKueue README：`../mini-kueue/README.md`
- 验证记录：`../VERIFICATION.md`
- 示例清单：`../examples/`
- 容量模拟器：`../scripts/capacity_model.py`（第 29 章）
- Configuration 静态检查：`../scripts/check_kueue_config.py`（第 3.6 节）
- 纳管边界/JobSet/DRA 示例：`../examples/kueue/enforce-management.yaml`、`jobset.yaml`、`dra.yaml`
- Configuration 片段：`../examples/config/`
- 物理落地诊断：第 28 章
- 生产控制器迁移：第 30 章
- kueuectl 与自动化运维：第 31 章
- 高可用与灾备：第 32 章
- 实现补全：第 33 章
- MultiKueue 多集群深入：第 34 章
- 扩展开发（AdmissionCheck/Job 集成/client-go）：第 35 章
- 推理、弹性与混合负载：第 36 章
- 调度决策可解释性与 What-if：第 37 章
- 原生 WAS 与 gang scheduling：第 38 章
- MultiKueue/扩展/推理示例：`../examples/kueue/multikueue.yaml`、`custom-admission-check.yaml`、`inference-workloads.yaml`

## 23.9 版本说明

- 本教程以 `kueue.x-k8s.io/v1beta2` 为主线；截至 2026-11 稳定线是 v0.20.x（v0.20.5），v0.21 仅有 devel。
- 涉及 alpha/beta 的能力已标注；默认值/feature gate 可能随版本变化。Kubernetes 1.37 的原生 WAS/PodGroup 是 Beta（`GenericWorkload` 默认关闭），见第 38 章。
- 安装命令中的版本号是示例，请替换为实际 release。
- 如果字段/行为与你的版本不一致，以官方 API reference 和 release notes 为准。
- 从 v1beta1 迁移到 v1beta2 的字段映射和升级流程见[第 24 章](24-api-migration-upgrade.md)。

## 23.10 MiniKueue 速查

```bash
cd mini-kueue

go test ./... -count=1
go run ./cmd/minikueue -list
go run ./cmd/minikueue -scenario basic -ticks 4
go run ./cmd/minikueue -scenario tas -ticks 2
go run ./cmd/minikueue -scenario pods-ready -ticks 5
go run ./cmd/minikueue -scenario dynamic-reclaim -ticks 4
go run ./cmd/minikueue -scenario dynamic-quota -ticks 2
go run ./cmd/minikueue -scenario label-selector -ticks 2 -query selective
go run ./cmd/minikueue -scenario localqueue-hold -ticks 3
go run ./cmd/minikueue -scenario retry-backoff -ticks 4 -query retry
go run ./cmd/minikueue -scenario dra -ticks 2
go run ./cmd/minikueue -scenario tas-slice -ticks 1
go run ./cmd/minikueue -scenario admission-fair-sharing -ticks 2
go run ./cmd/minikueue -scenario concurrent-admission -ticks 2
go run ./cmd/minikueue -scenario admission-gate -ticks 2
go run ./cmd/minikueue -scenario hierarchical-limits -ticks 1
go run ./cmd/minikueue -scenario implicit-cohort -ticks 1
go run ./cmd/minikueue -scenario cohort-shared-pool -ticks 1
go run ./cmd/minikueue -scenario configurable-preemption -ticks 2
go run ./cmd/minikueue -scenario fair-sharing-preemption -ticks 2
go run ./cmd/minikueue -scenario provisioning-request -ticks 5
go run ./cmd/minikueue -scenario resource-transform -ticks 2
go run ./cmd/minikueue -scenario multikueue -ticks 2
go run ./cmd/minikueue -scenario multikueue-failover -ticks 5

# 查询与操作
go run ./cmd/minikueue -scenario basic -ticks 2 -query basic
go run ./cmd/minikueue -scenario basic -ticks 1 -get workloads
go run ./cmd/minikueue -scenario basic -ticks 1 -describe default/job-a
go run ./cmd/minikueue -scenario basic -ticks 2 -stop default/job-b
go run ./cmd/minikueue -scenario basic -ticks 2 -resume default/job-b
go run ./cmd/minikueue -scenario basic -ticks 1 -explain default/job-c
go run ./cmd/minikueue -scenario basic -ticks 1 -what-if "basic/default/cpu=3000"
```

26 个引擎场景 + 2 个 MultiKueue 演示（含 failover）：

| 场景 | 演示 |
|---|---|
| `basic` | FIFO、配额耗尽、完成释放 |
| `cohort-borrow` | Cohort 借用 |
| `preemption` | 同 CQ 抢占 |
| `reclaim` | 原主回收 |
| `admission-check` | 两阶段准入 |
| `strict-fifo` | 队头阻塞 |
| `best-effort-fifo` | 跳过队头 |
| `tas` | 4 卡作业占满 rack |
| `pods-ready` | WaitForPodsReady 超时 |
| `dynamic-reclaim` | 回收 Pod |
| `dynamic-quota` | DQO-lite |
| `label-selector` | matchLabels + matchExpressions |
| `localqueue-hold` | LQ stopPolicy 隔离 |
| `retry-backoff` | AdmissionCheck Retry 退避 |
| `dra` | DeviceClass → 逻辑资源计费 |
| `tas-slice` | TAS slice + 打散 |
| `admission-fair-sharing` | AFS 历史用量排序 |
| `concurrent-admission` | Flavor 迁移 |
| `admission-gate` | 配额前外部闸门 |
| `hierarchical-limits` | Cohort 级 borrowing limit |
| `implicit-cohort` | 隐式 Cohort 借用 |
| `cohort-shared-pool` | 显式 Cohort 的共享 nominal 池 |
| `configurable-preemption` | PreemptionConfig 规则授权抢占 |
| `fair-sharing-preemption` | Fair Sharing 收回借出配额 |
| `provisioning-request` | 预留期间置备、失败重试、延迟 Ready |
| `resource-transform` | vGPU 请求折算为 total memory/core |
| `multikueue` | Manager/worker 提名、选中、清理 |
| `multikueue-failover` | 选中 worker 失联、超时切换、恢复后清理 |

MiniKueue 的指标：

```text
minikueue_admission_cycles_total
minikueue_admitted_workloads_total
minikueue_finished_workloads_total
minikueue_evicted_workloads_total
minikueue_preempted_workloads_total
minikueue_quota_reserved_workloads_total
minikueue_admission_check_waits_total
minikueue_pods_ready_timeouts_total
minikueue_reclaimed_pods_total
minikueue_pending_workloads
minikueue_reserving_active_workloads
minikueue_admitted_active_workloads
minikueue_finished_workloads
```

MiniKueue 的 API 类型（内存对象，不是 CRD）：

```text
Workload / PodSet / WorkloadStatus
  - Conditions / Admission / AdmissionChecks
  - RequeueAt / SchedulingStats / ReclaimablePods
  - AdmittedAt / ExecutionTicks（当前准入周期起点 + 历史累计执行时间）
  - AdmissionGates / DeviceClassMappings / Devices
LocalQueue（含 StopPolicy、FairSharing）
ClusterQueue / Cohort
  - ResourceGroups / Flavors / FairSharing
  - NamespaceSelector（LabelSelector + matchExpressions）
  - AdmissionScope / ConcurrentAdmission / DeviceClassMappings
  - PreemptionConfigName / Labels
  - Cohort 级 BorrowingLimit/LendingLimit
ResourceFlavor / Topology / TopologyDomain
AdmissionCheck / WorkloadPriorityClass
CapacityProvider
ResourceTransformation / PreemptionConfig / ProvisioningConfig / BackoffConfig
Topology slice/spreading（PodSetTopology.SliceSize/Spreading）
```

## 23.11 结语

Kueue 的价值不只是"排队"，而是把 **作业级资源治理** 变成 Kubernetes 的一等能力：

- 用 Workload 表达作业；
- 用 Queue/Cohort 表达配额与共享；
- 用 Flavor 表达异构资源；
- 用 AdmissionCheck 接入外部世界；
- 用 Fair Sharing/Preemption 处理竞争；
- 用 MultiKueue/TAS/DRA 处理多集群与物理约束。

MiniKueue 把这些语义压缩成一套可运行、可测试的代码。希望它能帮助你：

- 建立直觉；
- 验证想法；
- 读懂真实 Kueue；
- 在自己的平台上做出更好的队列/配额设计。

---

[← 上一章：真实集群实验手册](22-hands-on-labs.md) | [返回目录](../README.md) | [下一章：API 迁移与版本升级 →](24-api-migration-upgrade.md)
