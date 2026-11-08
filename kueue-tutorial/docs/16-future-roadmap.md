# 第 16 章 · 现状与未来方向

> 本章讨论的是方向和趋势，不是版本承诺。具体特性是否进入 alpha/beta/stable，以 Kueue release notes、KEP 和官方文档为准。

## 16.1 当前状态：从"队列"走向"平台调度层"

Kueue 已经从一个"Job 排队控制器"发展成 Kubernetes 上 AI/ML 批处理的基础调度层：

- `Workload` 成为统一的作业抽象；
- `ClusterQueue`/`LocalQueue`/`Cohort` 成为配额和共享的标准模型；
- `ResourceFlavor` 把异构资源纳入配额；
- `AdmissionCheck` 把云扩容、多集群、拓扑等能力接入准入；
- `MultiKueue` 把队列扩展到多集群；
- TAS/DRA 开始处理"卡在哪、怎么连"的问题。

从 API 演进看：

- v1beta1 → v1beta2；
- Cohort 从字符串引用变成一等 CRD，并支持层级；
- 新能力通过 feature gate 渐进发布。

这说明 Kueue 的定位在扩大：它不只是"排队"，而是 **Kubernetes 上的作业级资源治理与调度前置层**。

### 16.1.1 2026 年时间点的版本快照

截至 2026 年 11 月，稳定线是 **v0.20.x**（最新 patch 为 v0.20.5）；v0.21 只有 `v0.21.0-devel`，尚未 GA。以下以 v0.20 稳定线为主线，涉及 devel 的地方会单独标注。几个标志性变化：

- **Admission Fair Sharing 成为 beta 默认开启**（v0.15 起），v0.20 引入 `AdmissionFairSharingAnchorAtQuotaReservation`（beta 默认开），把计量锚点从 Admitted 提前到 QuotaReserved。
- **TAS 成熟为 beta 默认开启**，热替换、partial slice、multi-layer topology 等 gate 陆续 beta。
- **DRA 在 Kubernetes 1.34 GA**（`resource.k8s.io/v1`，默认开启）；Kueue 侧两条路径（ResourceClaimTemplate / 扩展资源）进入 beta，counter/capacity 计费仍依赖 Kubernetes 侧 gate；1.37 的 `DRAWorkloadResourceClaims` 进入 beta，允许 PodGroup 共享 claim 生命周期。
- **DQO 进入 alpha**（v0.20），有独立的 CapacityProvider/DynamicQuotaOrchestrator API；第一个里程碑是"容量发现 + 按 nominal 比例分发到 effectiveQuotas"。
- **Concurrent Admission 进入 alpha**（v0.18），支持 Parent/Variant Workload 与 Flavor 迁移。
- **Configurable Preemption 进入 alpha**（v0.20），PreemptionConfig CRD 可声明触发条件和候选选择器。
- **TAS 继续成熟**（v0.20）：`podset-topology-spreading`（alpha）、TAS+DRA 设备可行性、`TASReplaceMultipleFailedNodes` 增量热替换、mutable `topology.spec.levels`。
- **Elastic Workloads 扩展**（v0.20）：Partial Admission 的 `minCount` 可以为 0；`ElasticJobsViaWorkloadSlices` 支持 ProvisioningRequest 增量置备；Ray 集成增强。
- **DRA 细化**（v0.20）：`KueueDRADeviceFeasibility`、`KueueDRAIntegrationPrioritizedList`、DeviceTaintRule、`adminAccess` 请求免计费、`DRAResourcesUnresolved`。
- **Fair Sharing 继续演进**：`FairSharingRefill`（alpha）、`AdmissionFairSharingAnchorAtQuotaReservation`（beta）。
- **v1beta2 成为主线 API**；v1beta1 在 v0.20 起不再 served（升级前必须迁移）。

这些变化说明：Kueue 正在从"队列控制器"变成"多租户、多集群、拓扑/设备感知的资源治理层"。

## 16.2 近期/活跃方向

### 16.2.1 分层 Cohort 与公平分享

层级 Cohort 让组织可以表达：

```text
公司
├── 研究组织
│   ├── 团队 A
│   └── 团队 B
└── 生产组织
    ├── 团队 C
    └── 团队 D
```

挑战：

- 配额在树上如何聚合/借用/出借；
- 公平分享如何在层级上比较（AlmostLCA）；
- 抢占如何避免跨层循环；
- 运维如何解释"为什么这个团队拿不到资源"。

Kueue 已经在推进层级 Cohort 和 AlmostLCA 的公平算法。未来的重点可能是：

- 更完整的层级公平证明；
- 更直观的份额指标；
- 与 Admission Fair Sharing 的结合；
- 更丰富的权重/预留策略。

### 16.2.2 Dynamic Resource Allocation（DRA）

DRA 是 Kubernetes 设备管理的方向。Kueue 已经在做：

- DeviceClass 到逻辑资源的映射；
- ResourceClaimTemplate/扩展资源两条路径；
- 按设备数/counter/capacity 计费；
- TAS 与 DRA 的设备可行性检查；
- 设备污点。

未来可能的方向：

- 更完整的 DRA 特性覆盖（binding conditions、device taints、partitionable devices）；
- 与拓扑/网络拓扑的更深集成；
- 更准确的"配额已预留但设备未分配" gap 处理；
- 多设备类别/共享设备的公平。

### 16.2.3 Topology-Aware Scheduling

TAS 从"尽量同 rack"发展到：

- 多层级拓扑约束；
- PodSet group；
- slice（把一个 PodSet 切成多个拓扑域）；
- 跨 Workload 打散；
- 节点故障热替换；
- DRA 设备可行性。

2026 路线图里的 TAS 方向包括：

- **资源变换**：让拓扑感知与资源变换/设备计费组合；
- **弹性作业**：扩缩容时重新计算拓扑放置；
- **新污点节点的驱逐**：节点被标记为不可用后，主动把 Workload 从该域迁走；
- **增量热替换**：`TASReplaceMultipleFailedNodes` 减少多节点故障时的全量重排。

未来挑战：

- 大规模拓扑的容量计算性能；
- 与 Cluster Autoscaler 的协作；
- 节点故障/回收时的重新放置；
- 拓扑碎片整理（defragmentation）；
- 与 gang scheduling 的关系；
- `podset-topology-spreading` 的份额语义与多租户公平。

### 16.2.4 MultiKueue 与多集群

MultiKueue 已经支持：

- manager/worker；
- AllAtOnce/Incremental/External dispatcher；
- 跨集群状态同步；
- ClusterProfile/Secret kubeconfig；
- `MultiKueueOrchestratedPreemption`（KEP-8303）：用 preemption gate 协调"一次只让一个 worker 尝试抢占"；v0.20 起 Beta 默认开启，`SingleClusterPreemptionTimeout` 默认 5 分钟并可通过 Configuration 配置。

2026 路线图里的 MultiKueue 重点包括：

- **弹性 Ray 作业/自动扩缩**：让 RayJob/RayCluster 在 MultiKueue 下支持弹性与跨集群扩缩；
- **Workload 级准入约束与偏好感知分发**：不是所有 Workload 都可以去所有 worker；
- **跨集群移动时的服务可用性**：长运行服务在 worker 切换时的连续性；
- **worker 集群日志检索**：从 manager 统一取 worker 日志；
- **防止抢占在多个 worker 同时启动**：与 orchestrated preemption 相关；
- 更智能的集群选择（成本、碳排、数据 locality、拓扑）；
- 跨集群公平与配额；
- manager 自身作为 worker（当前限制）；
- 与集群联邦/多集群控制面集成。

### 16.2.5 动态配额编排（DQO）

DQO 让配额来自 CapacityProvider 的实际容量，而不是静态数字。这适合：

- 云上弹性容量；
- 多集群 manager 超卖；
- 预留/抢占式实例混合；
- 容量随价格/供应变化。

挑战：

- 容量下降时如何回收（当前不会自动驱逐）；
- effectiveQuotas 的清理和冲突；
- provider 的可靠性/一致性；
- 与公平分享/抢占的交互。

### 16.2.6 Concurrent Admission

Concurrent Admission 让 Workload 在多个 Flavor 上并行追求准入，并迁移到更优 Flavor。它适合：

- reservation/spot/on-demand 混合；
- 多 zone 同构 Flavor；
- 先跑起来、再迁移的场景。

挑战：

- Variant 数量 × CQ 数量的性能；
- 迁移的 disruption；
- 异构 PodSet 不支持；
- 与 AdmissionCheck/拓扑的交互。

### 16.2.7 Configurable Preemption

`PreemptionConfig` 让管理员用声明式规则定义抢占：

- 拓扑碎片整理；
- "hero" 关键作业；
- 自定义标签/优先级规则；
- 跨队列抢占。

风险：

- 规则对称 → 级联抢占；
- `AnyClusterQueue` → 误伤；
- 难以推理/测试。

未来可能的发展：

- 更强的静态分析/校验；
- 抢占模拟/干跑；
- 与 Fair Sharing 的统一；
- 更丰富的选择器/触发条件。

### 16.2.8 Elastic Workload 与动态回收

方向：

- 作业运行时调整副本数；
- 释放不再需要的配额；
- 与 JobSet/Kubeflow/Ray 的弹性集成；
- 更准确的账本同步；
- 与 TAS/DRA 的交互。

### 16.2.9 可观测性与运维

方向：

- 更丰富的队列/公平指标；
- 更好的 pending 原因解释；
- 事件/条件的一致性；
- 与 OpenTelemetry 的集成；
- 大规模集群的性能优化；
- 更完善的 dashboard/runbook。

### 16.2.10 可验证性与形式化方法

调度器的正确性越来越难靠"跑几个例子"证明。未来的方向包括：

- **不变量即测试**：把配额、拓扑、生命周期不变量直接写成随机化属性测试；
- **差分测试**：同一组清单同时喂给真实 Kueue 和模拟器，比较准入决策而不是时间戳；
- **模型检查**：用 TLA+/Alloy 验证"预留不会超额""抢占不会循环""迁移必然终止"等性质；
- **故障注入/混沌**：webhook 不可用、informer 延迟、API 冲突、worker 失联；
- **真实 trace 回放**：用生产 Workload 的到达/完成序列做回归，而不是随机数；
- **mutation testing**：故意注入缺陷，检查测试能否杀死突变。

这些方法不会替代真实集群测试，但能把"绿测试"变成"有证据的正确性"。第 27 章会给出 MiniKueue 上的一套可执行流程。

### 16.2.11 Workload-Aware Scheduling（WAS）

Workload-Aware Scheduling（WAS）已经不再是"可能的形态"，而是有了明确的时间线：

- Kubernetes 1.36 在树内引入 Workload/PodGroup；
- Kubernetes 1.37（2026-08）把 Workload/PodGroup 推进到 Beta，API 为 `scheduling.k8s.io/v1beta1`；`GangScheduling` 与 `WorkloadAwarePreemption` 合并为 `GenericWorkload` gate（Beta，默认关闭）；
- `CompositePodGroup`（gang of gangs）为 Alpha（`scheduling.k8s.io/v1alpha3`）；
- Kueue 侧已有 `pkg/cache/scheduler/was` 与集成 RFC #12385，讨论"Kueue 创建 PodGroup → PodGroup 驱动 PodSets → 弃用标签式 pod group"的分阶段迁移。

职责边界因此变成：

- Kueue 继续做配额/准入与作业语义的唯一事实来源；
- kube-scheduler 以 PodGroup 为单位做 gang 放置和作业级抢占；
- 平台必须显式对齐"配额单位"与"gang 单位"，并处理双轨期的版本 skew。

第 38 章会完整展开这条线。平台设计时要避免把"当前 Kueue 的 TAS 实现细节"或"标签式 pod group"写死成长期接口。

### 16.2.12 Hero Workload、抢占成本与队列治理

路线图里还有几类"治理型"能力：

- **Hero workloads**：为少数关键作业提供更强的准入/保护语义，避免它们被普通队列规则卡住；
- **preemption-cost-aware candidate selection**：选择 victim 时考虑重启成本、checkpoint 代价、已运行时长，而不是只看优先级；
- **workload-level admission constraints**：某些 Workload 只能去某些集群/队列；
- **partial preemption of serving workloads**：长期目标，当前不是稳定能力。

这些能力共同说明：Kueue 正在从"按优先级排队"走向"按业务价值/成本/风险做准入"。

### 16.2.13 API v1 毕业

Kueue 的长期目标是 API 毕业到 `v1`。**截至 2026 年 11 月尚未毕业**：v1beta2 仍是唯一主线，v1beta1 从 v0.20 起不再 served；上游 issue #3476 仍在跟踪。这意味着：

- 字段会继续收敛，alpha 字段可能被重新设计；
- v1beta2 的迁移路径要提前规划；
- 平台不要依赖未稳定的 alpha 字段做核心契约；
- 升级测试要覆盖 CRD conversion 和 webhook 兼容。

## 16.3 Kubernetes 生态趋势

### 16.3.1 Gang Scheduling / Pod Group

Kubernetes 社区长期在讨论 Pod Group/Gang Scheduling。Kueue 的 Workload 已经提供了作业级 gang 语义，但最终的 Pod 放置仍由调度器决定。未来可能出现：

- 调度器原生 gang scheduling；
- Kueue 与调度器的更紧密协作；
- 作业级原子放置的标准化 API。

### 16.3.2 DRA 成为一等公民

DRA 会把 GPU/FPGA/网卡等设备的管理标准化。Kueue 需要：

- 理解 DeviceClass/ResourceClaim；
- 在配额层计费；
- 在拓扑层检查可行性；
- 与设备污点/健康状态交互。

### 16.3.3 多集群/联邦

AI 训练和推理越来越跨集群/跨云。MultiKueue 是一个方向，但还会遇到：

- 数据 locality；
- 网络带宽/成本；
- 集群间身份/信任；
- 统一配额/公平；
- 故障隔离。

### 16.3.4 成本与碳排

未来调度可能不仅看"有没有资源"，还看：

- 每 GPU 小时成本；
- spot 中断概率；
- 碳排/能耗；
- 数据传输成本；
- SLA/截止时间。

Kueue 的 Flavor/AdmissionCheck 架构可以接入这些信号，但需要新的策略/指标。

## 16.4 AI/ML 负载趋势

### 16.4.1 更大的模型

- 单作业需要更多 GPU/TPU；
- 对拓扑/带宽更敏感；
- gang scheduling 和 all-or-nothing 更重要；
- 抢占的代价更高（checkpoint/restart）。

### 16.4.2 异构加速器

- NVIDIA/AMD/TPU/自研芯片；
- 不同型号/显存/互联；
- DRA/Flavor 需要表达更多维度；
- 配额不能只按"卡数"。

### 16.4.3 推理与训练混合

- 推理服务需要长期占用/低延迟；
- 训练需要批量/可抢占；
- Kueue 需要区分 SLA/优先级/抢占策略；
- Deployment/StatefulSet/LeaderWorkerSet 集成更重要。

### 16.4.4 弹性与多租户

- 多团队共享 GPU 池；
- 借用/回收/公平；
- 预算/成本分摊；
- 安全隔离。

## 16.5 对平台设计者的建议

1. **把配额当产品**：nominal/borrowing/lending 是平台和租户的合同，要有文档、评审、变更流程。
2. **先做可观测性**：没有 pending 原因、份额、借用指标，就无法调优。
3. **不要过度分层**：Cohort 树太深会增加公平/排障复杂度。
4. **优先保证 gang/all-or-nothing**：AI 作业最怕半启动。
5. **预留物理 gap**：逻辑配额 ≠ 物理容量；用 TAS/DRA/ProvisioningRequest/WaitForPodsReady 兜底。
6. **谨慎使用抢占**：尤其是 `Any` 和可配置抢占；要有非对称规则和监控。
7. **渐进式接入**：先用 LocalQueue/ClusterQueue 管理新作业，再迁移存量；用 `manageJobsWithoutQueueName` 要谨慎。
8. **版本固定/升级测试**：Kueue 迭代快，feature gate/CRD 会变。

## 16.6 对贡献者的建议

- 从文档/测试/小 bug 开始；
- 理解 Kueue 的调度语义和 Cache/Scheduler 分层；
- 新特性需要 KEP/设计讨论；
- 性能/规模问题需要基准和真实负载；
- 多集群/TAS/DRA 需要跨项目知识；
- 关注 Kubernetes SIG Scheduling 的讨论。

## 16.7 如何跟踪 Kueue 的变化

不要靠博客/二手文章判断版本能力。推荐的信息源和顺序：

1. **release notes**：每个版本的破坏性变更、gate 默认值、CRD schema。
2. **KEP**：设计动机、权衡、未实现部分。KEP 比 API reference 更能解释"为什么"。
3. **API reference**：字段、默认值、必填/可选。
4. **源码**：`pkg/cache/queue`、`pkg/cache/scheduler`、`pkg/scheduler` 是决策主干。
5. **issue/PR**：已知问题、性能回归、下一版计划。

一个实用做法：在 staging 集群固定版本，把 release notes 里的 gate 变化做成 diff，再决定是否升级。不要直接追 main。

## 16.8 未来不是一条直线

需要保持谨慎：

- 不是所有 alpha 特性都会进入 stable；
- API 可能变化；
- 多集群/动态配额/可配置抢占的复杂度很高；
- 生产采用要看社区成熟度、供应商支持、自身运维能力。

## 16.9 本章小结

- Kueue 正从"Job 排队"扩展到"作业级资源治理与调度前置层"。
- 2026 年时间点：Admission Fair Sharing/TAS/DRA/MultiKueue 进入 beta 默认，DQO/Concurrent Admission/Configurable Preemption 仍是 alpha；v1beta2 成为主线，v1beta1 在 v0.20 起不再 served。
- 活跃方向：层级 Cohort/公平、DRA、TAS（spreading/资源变换/弹性）、MultiKueue（弹性 Ray/长运行服务/日志）、DQO、Concurrent Admission、Configurable Preemption、弹性作业、WAS，以及可验证性/形式化方法。
- 治理型能力在增加：hero workloads、preemption cost、workload-level admission constraints；长期目标是 API v1。
- Kubernetes 生态的 gang scheduling、DRA、多集群、成本/碳排会影响 Kueue 的演进。
- AI/ML 负载趋向更大、更异构、更混合、更弹性。
- 平台设计者应优先可观测性、gang、物理 gap、谨慎抢占和渐进接入。
- 跟踪变化靠 release notes + KEP + API reference + 源码，不靠二手文章。
- 未来方向是趋势判断，不是版本承诺。

---

[← 上一章：与真实 Kueue 的差距](15-gap-to-real-kueue.md) | [返回目录](../README.md) | [下一章：Kueue 控制器内部原理 →](17-kueue-internals.md)
