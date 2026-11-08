# 第 15 章 · 与真实 Kueue 的差距

## 15.1 为什么要做差距矩阵

MiniKueue 的目标是"可运行的语义模型"，不是"Kueue 的替代品"。明确差距有三个好处：

1. 避免把教学实现误用于生产；
2. 知道哪些语义已经验证，哪些只是文档描述；
3. 为从模拟器迁移到真实控制器提供路线图。

## 15.2 覆盖矩阵

| 能力 | 真实 Kueue | MiniKueue | 差距/说明 |
|---|---|---|---|
| Workload/PodSet 资源汇总 | 完整 | 完整 | 包括 `pods` 自动计数 |
| LocalQueue/ClusterQueue | 完整 CRD | 内存对象 | 无 API server/CRD/webhook |
| ResourceFlavor | nodeLabels/tolerations/taints/topology | 名称 + topologyName | 不注入 Pod，不影响真实节点 |
| 多资源组/多 Flavor | 完整 | 完整 | 按列表顺序 first-fit |
| nominal/borrowing/lending | 完整 | CQ 级完整 + Cohort 级简化 | Cohort borrowingLimit 按成员正借用求和；Cohort lendingLimit 只做逐成员收紧，不是子树总量上限 |
| 扁平/层级 Cohort | 完整 | 配额聚合 + 隐式 Cohort | 共享池按根树扁平聚合（兄弟子树可见）；Fair Sharing 未用 AlmostLCA |
| TAS | beta，节点/设备容量推导 | TAS-lite，显式 domain 容量 + slice/打散 | 无热替换、节点/非 TAS Pod 推导、多层 slice、DRA 可行性；group 视为硬约束，slice 固定最空优先。跨层账本已用 `Scheduler.TopologyUsage` 对齐 |
| DQO | alpha，CapacityProvider/effectiveQuotas | DQO-lite，按 nominal 比例分配 | 无容量下降驱逐/状态同步 |
| Cohort 共享 nominal | 完整 | 扁平聚合近似 | 共享池按根树汇总，兄弟子树的共享池对目标 CQ 可见 |
| namespaceSelector | 完整 LabelSelector | matchLabels + matchExpressions | MiniKueue 的 namespace 标签需显式注册；真实 K8s 的 `kubernetes.io/metadata.name` 由控制面自动设置 |
| StrictFIFO/BestEffortFIFO | 完整 | 完整 | — |
| StopPolicy | CQ + LocalQueue | CQ + LocalQueue | 叠加语义完整 |
| Admission Gate | 注解 + 外部控制器 | `AdmissionGates` + `ClearAdmissionGate` | 可演示配额前阻塞；无 gate 控制器、逐 gate 状态或自动清除 |
| Resource transformations | 支持 | lite | `Replace`/`Retain`、`multiplyBy`（资源名或固定整数）、`outputs`；无 Configuration API/webhook；与 DRA 映射是两条路径 |
| PVC/卷拓扑等调度约束 | 由调度器/TAS 处理 | 未实现 | 无节点/卷模型 |
| 优先级 | Pod + WorkloadPriorityClass | 数字 + PriorityClass | 不解析 Pod priority |
| Classic 抢占 | 完整 | 简化 | 无 flavorFungibility、四条目标启发式、完整预抢占 |
| 回收 | 完整 | 简化 | 已有 Fair Sharing 抢占 lite；无目标 CQ 排序、预抢占与 AlmostLCA |
| Fair Sharing | 加权 DRF + AlmostLCA + Cohort 权重 | CQ 级借用式加权 DRS + Cohort 权重 + AFS-lite + Fair Sharing 抢占 lite | 无 AlmostLCA、采样窗口/精确锚点、目标 CQ 排序与预抢占；lendable 已按 nominal 总和（受 lendingLimit 截断）实现 |
| SchedulingStats | 完整 | reason + underlyingCause 计数 | 无指标去重 |
| AdmissionCheck Retry | requeueAfterSeconds + retryCount + 退避策略 | requeueAfterSeconds + retryCount + `BackoffConfig` | 无可配置 `retryStrategy`/最大重试次数 |
| Visibility | Visibility API | `Engine.QueueStatus` + CLI `-query`/`-get`/`-describe` | 无 HTTP API、无 APF |
| Configurable Preemption | alpha | lite | trigger/scope/priority/numericLabels/labelSelector/clusterQueueSelector；无 preemptor selector/规则优先级 |
| AdmissionCheck | Pending/Ready/Retry/Rejected | Pending/Ready/Rejected + Retry | 无真实控制器；ProvisioningRequest 由 lite 配置模拟；MultiKueue 由独立 lite 包模拟 |
| ProvisioningRequest | 完整 | lite | 预留期间置备、失败重试、延迟后 Ready；无云扩容、ProvisioningRequestConfig、PodSetUpdates |
| MultiKueue | beta | MultiKueue-lite | 多 Engine、AllAtOnce/Incremental、选中与清理、Down + WorkerLostTimeout 失联切换；无 External dispatcher、跨集群凭据/网络/状态同步/重复执行语义 |
| DRA | beta/alpha | DRA-lite | DeviceClass → 逻辑资源计费；无真实设备分配、Counter/Capacity、设备污点 |
| Concurrent Admission | alpha | Concurrent-lite | 单 Workload 向更优 Flavor 迁移；无 Parent/Variant/并行准入 |
| Elastic/Dynamic Reclaim | 部分 | 已实现简化版 | `ReportReclaimable`，无真实集成层 |
| All-or-nothing/PodsReady | 完整 | 已实现简化版 | `WaitForPodsReady` + `MarkPodsReady`，无真实 Pod 状态 |
| 作业集成 | 多种框架 | 直接 Workload | 无 Job 控制器 |
| 纳管边界 | 默认 LocalQueue、默认 WorkloadPriorityClass、`managedJobsNamespaceSelector`、VAP | 未实现 | MiniKueue 的 `Submit` 要求显式 `QueueName`；真实 Kueue 的纳管策略见第 8.5 节 |
| Webhook/CRD | 完整 | 内存校验 | 无 admission webhook |
| 持久化 | etcd | 无 | 进程退出即丢失 |
| HA/leader election | 支持 | 无 | 单进程 |
| Visibility API | 支持 | 简化 | `QueueStatus` + CLI；无 HTTP API、APF、分页 |
| kueuectl | 完整 CLI | 参数级模拟 | `-query`/`-get`/`-describe`/`-stop`/`-resume`；无 `create`/`list` 子命令与资源补全 |
| 指标 | 大量 | 子集 | 无直方图/完整标签 |
| 真实 Pod/节点 | 是 | 否 | 无 kube-scheduler |
| 性能 | 生产级 | 教学级 | 单锁、全量扫描 |

## 15.3 语义差异详解

### 15.3.1 Flavor 分配

真实 Kueue 会考虑：

- `flavorFungibility`（是否先试下一个 Flavor，还是先借用/抢占）；
- 是否存在 nominal 内的 Flavor；
- 是否需要预抢占；
- 资源组的组合；
- 可能对每个 Flavor 模拟抢占。

MiniKueue：

- 先按顺序尝试所有 Flavor 的直接 fit；
- 都不行才统一做抢占；
- 没有 `flavorFungibility`。

这会导致某些边界场景下的选择不同，例如"本地 nominal 不足时，是先换 Flavor 还是先抢同 CQ"。

### 15.3.2 Fair Sharing

真实 Kueue：

- 加权 DRF；
- Cohort 树的 AlmostLCA；
- 只对可借用资源；
- Fair Sharing 抢占策略；
- 历史用量（Admission Fair Sharing）。

MiniKueue：

- 每个 Flavor/Resource 的 `borrowed = max(0, usage - effectiveNominal)`；
- `lendable` 取同 Cohort 树其他 CQ 的 nominal 总和（受 lendingLimit 截断）+ 共享池；
- 取最大 `borrowed/lendable`，除以 CQ 权重 × 路径上各 Cohort 权重（`fairShareWeight`）；
- 只对借用计分，nominal 内 share 为 0；
- 按升序处理 CQ；
- AFS-lite 另外维护 LocalQueue 历史用量、半衰期和 Entry Penalty；
- Fair Sharing 抢占 lite：按两种 strategy 计算 preemptor new share / target old/new share，候选取同 Cohort、低优先级、正 share 的 Workload；
- 没有 AlmostLCA、采样窗口/精确锚点、目标 CQ 排序和预抢占。

因此 MiniKueue 的"公平"在方向上与真实 Kueue 一致，但仍不能用于生产调参。

### 15.3.3 抢占

真实 Kueue：

- Classic Preemption 的完整 target 选择启发式；
- Fair Sharing 抢占；
- Configurable Preemption；
- 预抢占/flavorFungibility；
- 最小 victim 集合；
- 更复杂的排序/去重。

MiniKueue：

- Classic：同 CQ/Cohort 候选人，借用者优先、低优先级、最近准入；
- Configurable：`PreemptionConfig` 的 trigger/scope/priority/numericLabels/selectors 与 Classic 候选合并；
- Fair Sharing：两种 strategy 的 share 条件 + 同 Cohort 候选；
- 三者都使用"逐个移除 + 反向最小化"；
- 同优先级用创建时间做全序。

MiniKueue 的抢占是"能工作"的简化版，但没有覆盖真实 Kueue 的全部策略组合（preemptor selector、目标 CQ 排序、预抢占、flavorFungibility）。

### 15.3.4 Cohort 级配额

真实 Kueue 的 Cohort 可以定义：

- `resourceGroups`/`nominalQuota`；
- borrowingLimit/lendingLimit；
- fairSharing；
- 层级。

MiniKueue 支持共享 nominal、层级聚合、隐式 Cohort，以及简化版的 Cohort 级 borrowing/lending limit。borrowingLimit 按子树内每个成员的正借用求和，不会用空闲成员抵消借用者；lendingLimit 只是逐成员收紧，子树有 N 个成员时仍可能出借接近 N × limit。共享池按根树扁平聚合，兄弟子树的共享池对目标 CQ 可见。它不支持 AlmostLCA，也没有逐层精确的出借账本。CQ 级的 borrowing/lending 是完整的。

### 15.3.5 AdmissionCheck

真实 Kueue：

- 外部控制器；
- ProvisioningRequest 重试/生成新 request；
- MultiKueue 跨集群；
- PodSetUpdates；
- 与 Job 集成协作。

MiniKueue：

- 内存状态；
- 手动/自动 Ready；
- Rejected 停用；
- Retry 释放 + 重排队，`requeueAfterSeconds` 与 `BackoffConfig` 两种退避；
- `ProvisioningConfig` 模拟预留期间的置备、失败重试与延迟 Ready。

### 15.3.6 生命周期

真实 Kueue：

- Conditions 是主状态；
- `requeueState` 指数退避；
- `schedulingStats`；
- `reclaimablePods`；
- `PodsReady`/All-or-nothing；
- `maximumExecutionTimeSeconds`；
- `active` 的完整语义；
- Workload 由 Job 集成管理。

MiniKueue：

- State + Conditions；
- 默认固定 2 tick 重排队，可用 `BackoffConfig` 改成指数退避；
- 有简化的 `reclaimablePods`、`PodsReady`、`schedulingStats`、`requeueAfterSeconds`/`retryCount`，但没有真实 Pod 状态与 `retryStrategy` CRD；
- 最大执行时间 1 tick=1s，并用 `ExecutionTicks` 跨准入周期累计；
- 直接 Submit Workload。

## 15.4 工程化差距

### 15.4.1 持久化与一致性

真实 Kueue 用 etcd + API server：

- 乐观并发（resourceVersion）；
- watch 事件；
- status subresource；
- finalizer；
- 多副本 leader election。

MiniKueue 用内存 map：

- 无持久化；
- 无冲突检测；
- 无 watch；
- 进程退出即丢失。

### 15.4.2 并发

真实 Kueue 用 controller-runtime 的 workqueue：

- 每个 key 串行 reconcile；
- 不同 key 可并发；
- 限速/重试；
- informer cache。

MiniKueue 用一把 Engine 锁串行化所有写操作。简单、确定，但不是生产并发模型。

### 15.4.3 可观测性

真实 Kueue：

- Prometheus 指标；
- Events；
- Conditions；
- 日志；
- Visibility API；
- pprof（可选）。

MiniKueue：

- 子集指标；
- 内存事件；
- Conditions；
- CLI 输出。

### 15.4.4 安全

真实 Kueue：

- CRD + webhook；
- RBAC；
- finalizer；
- TLS 证书；
- MultiKueue 凭据。

MiniKueue：

- 无 API server；
- 无 RBAC；
- 无 webhook；
- 无 TLS；
- 无凭据。

## 15.5 从 MiniKueue 到真实控制器

### 15.5.1 替换 API

把 `internal/api` 替换成 CRD 类型：

- `Workload`、`ClusterQueue`、`LocalQueue`、`Cohort`、`ResourceFlavor`、`AdmissionCheck`、`Topology`。
- 用 kubebuilder/controller-gen 生成 deepcopy、CRD、client。
- 用 status subresource 更新条件。

### 15.5.2 替换 Store

用 controller-runtime 的 informer cache：

- 注册 informer；
- 用 field/label indexer 加速查询；
- 用 client 更新对象；
- 用 workqueue 驱动 reconcile。

### 15.5.3 保留 Scheduler/Cache

`internal/scheduler` 和 `internal/cache` 的逻辑可以保留，但需要：

- 从 CRD 对象构建 View；
- 处理 resourceVersion/并发；
- 用真实的 ResourceList（Kubernetes Quantity）替换整数；
- 支持 DRA/TAS 扩展。

### 15.5.4 集成层

实现 job controller：

- 创建/更新 Workload；
- 暂停/放行 Job；
- 同步状态；
- 处理驱逐/完成；
- 注入 Flavor 的 nodeSelector/tolerations。

### 15.5.5 Webhook

实现：

- 默认值；
- 校验（资源组唯一、Flavor 唯一、borrow/lend 非负、namespaceSelector 语义）；
- 不可变字段；
- 状态同步。

### 15.5.6 测试

- 单元测试：scheduler/cache。
- envtest：CRD + webhook + controller。
- 集成测试：真实 Job/JobSet/RayJob。
- 性能测试：大规模 CQ/Workload。
- 混沌测试：节点故障、apiserver 抖动、控制器重启。

## 15.6 常见"看起来一样但不一样"的点

1. **`namespaceSelector: {}` vs 不写**：真实 Kueue 中前者匹配所有，后者不匹配任何。MiniKueue 已对齐，且支持 matchExpressions。
2. **`borrowingLimit`/`lendingLimit` 的 null vs 0**：null 不是 0。MiniKueue 已对齐。
3. **`withinClusterQueue` 的值**：真实是 `LowerOrNewerEqualPriority`，不是 `LowerOrEqual`。MiniKueue 已对齐字符串。
4. **`reclaimWithinCohort: Any`**：可以抢占任意优先级，风险高。MiniKueue 支持。
5. **Fair Sharing 的份额**：真实 Kueue 用借用比例 + 权重 + 层级 AlmostLCA；MiniKueue 已实现借用式 DRS 和 CQ/Cohort 权重，但仍是全树聚合，没有 AlmostLCA。
6. **`pods` 资源**：是自动计算的，不是 Pod requests。MiniKueue 已对齐。
7. **Flavor 注入**：真实 Kueue 会改 Pod 模板；MiniKueue 不会。这是"逻辑 vs 物理"的关键差距。
8. **AdmissionCheck 的 Retry**：真实有可配置退避和 ProvisioningRequest 生命周期；MiniKueue 有 requeueAfterSeconds/retryCount 和 `BackoffConfig` 指数退避，但没有最大重试次数策略。
9. **Workload 是 Condition 驱动**：MiniKueue 有 State 字段，只是教学简化。
10. **Cohort 是 CRD 还是字符串**：`cohortName` 是分组名，显式 Cohort 对象才有共享池/层级；MiniKueue 支持两种形态的简化版。
11. **LocalQueue stopPolicy 的叠加**：真实 Kueue 的 CQ/LQ 策略会叠加；MiniKueue 已对齐。
12. **SchedulingStats 的 underlyingCause**：真实 Kueue 有更细的原因；MiniKueue 只覆盖几种。
13. **AdmissionCheck Retry 的 retryCount**：MiniKueue 在释放并重新预留后仍保留 retryCount；早期实现会在 `AdmissionChecks=nil` 时把它清零。
14. **TAS 的 group/Preferred**：MiniKueue 把任何命名 group 都当作硬性共享约束，即使成员写 Preferred 也不会降级；真实语义可能允许 preferred 回退。
15. **TAS 的 slice 选域**：MiniKueue 的 slice 固定按"最空 domain 优先"选择，不受 `Spreading` 影响；`Spreading` 只控制整个 PodSet 的选域。
16. **DQO 漏报资源**：MiniKueue 现在把 provider 已报告 Flavor 但未报告的资源视为 0；provider 完全没报告的 Flavor 才回退到静态 nominal。

## 15.7 什么时候可以直接用真实 Kueue

- 需要生产级队列/配额/公平/抢占；
- 需要 Job/JobSet/Kubeflow/Ray 集成；
- 需要多集群、TAS、DRA、ProvisioningRequest；
- 需要 Webhook/CRD/HA/持久化；
- 需要完整的可观测性和社区支持。

## 15.8 什么时候 MiniKueue 仍然有用

- 教学和面试准备；
- 在改配额/权重前做快速模拟；
- 验证一个调度算法想法；
- 给非 Kubernetes 环境演示 Kueue 语义；
- 作为真实控制器的算法原型；
- 写论文/博客时做可重复实验。

## 15.9 本章小结

- MiniKueue 覆盖了核心配额/队列/抢占/AdmissionCheck 语义，并补齐了 DRA-lite、AFS-lite、Concurrent-lite、MultiKueue-lite、admission gate、Cohort 级 limit、隐式 Cohort、TAS slice/打散；但它仍不是生产 Kueue。
- 最大差距在：Kubernetes 集成、真实 Pod/Job/设备分配、AlmostLCA/逐层 Cohort 账本、采样窗口与精确计量锚点、生产级 ProvisioningRequest/Configurable Preemption/资源变换、持久化/HA/Webhook、完整可观测性。注意其中不少能力已有 lite 版本（见矩阵），差距是"生产完整语义"而不是"完全没有"。
- 从模拟器迁移到真实控制器需要替换 API/Store、实现集成层/Webhook、补齐测试。
- 有些语义细节（selector、borrow/lend null、preemption 字符串、pods、LQ stopPolicy、隐式 Cohort、retryCount 持久化、ExecutionTicks 累计）已经对齐，容易踩的坑要特别注意。
- 仍属于教学近似、不要当生产语义的：共享池按根树扁平聚合、Cohort lendingLimit 逐成员收紧、TAS group/Preferred 与 slice 选域、DQO 漏报资源的处理。
- 第 27 章会把这些"绿测试掩盖的缺陷"和对应的验证方法整理成可复用的审计流程。
- 真实生产请使用官方 Kueue，并阅读目标版本的文档。
- 要把 MiniKueue 的调度核心真正移植到 Kubernetes，按第 30 章的 CRD → manager → 调度循环 → Job 集成 → webhook → 测试 → 上线顺序推进；不要跳过 envtest 和提交前校验。

---

[← 上一章：MiniKueue 实现与验证](14-mini-kueue-walkthrough.md) | [返回目录](../README.md) | [下一章：现状与未来方向 →](16-future-roadmap.md)
