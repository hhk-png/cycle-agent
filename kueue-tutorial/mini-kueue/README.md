# MiniKueue

一个不依赖 Kubernetes 的 Kueue 控制面模拟器。它保留队列、配额、Cohort 借用、公平排序、抢占、回收、两阶段准入、TAS（含 slice/打散）、WaitForPodsReady、动态回收、DQO-lite、DRA-lite、资源变换、ProvisioningRequest-lite、Configurable Preemption-lite、Fair Sharing 抢占-lite、Admission Fair Sharing-lite、Concurrent Admission-lite、MultiKueue-lite（含失联超时切换）、admission gate、Cohort 级 limit、隐式 Cohort、matchExpressions selector、LocalQueue stopPolicy、SchedulingStats、AdmissionCheck Retry/指数退避和 Visibility 查询的核心语义，用内存对象和离散 `Tick` 替代 API server/etcd/真实 Pod。

> 这是教学实现，不是生产 Kueue 的替代品。生产请使用 <https://github.com/kubernetes-sigs/kueue>。

## 快速开始

```bash
go test ./...
go run ./cmd/minikueue -list
go run ./cmd/minikueue -scenario basic -ticks 4
go run ./cmd/minikueue -scenario tas -ticks 2
go run ./cmd/minikueue -scenario basic -ticks 2 -get workloads
go run ./cmd/minikueue -scenario basic -ticks 2 -describe default/job-a
go run ./cmd/minikueue -scenario basic -ticks 2 -stop default/job-b
go run ./cmd/minikueue -scenario basic -ticks 1 -explain default/job-c
go run ./cmd/minikueue -scenario basic -ticks 1 -what-if "basic/default/cpu=3000"
```

环境：Go 1.24+（本机使用 Go 1.27 验证）。没有第三方依赖。

## 场景

| 场景 | 演示内容 |
|---|---|
| `basic` | FIFO 准入、配额耗尽、完成后释放 |
| `cohort-borrow` | ClusterQueue 借用 Cohort 空闲配额 |
| `preemption` | 高优先级抢占同 CQ 低优先级 |
| `reclaim` | 原主收回被借走的 nominal |
| `admission-check` | 配额预留 + 手动 AdmissionCheck |
| `strict-fifo` | 队头阻塞 |
| `best-effort-fifo` | 跳过放不下的队头 |
| `tas` | 4 卡作业跨 host 占满一个 rack |
| `pods-ready` | WaitForPodsReady 超时驱逐 |
| `dynamic-reclaim` | 作业回收 Pod 并释放配额 |
| `dynamic-quota` | CapacityProvider 缩小有效配额 |
| `label-selector` | matchLabels + matchExpressions 的 namespaceSelector |
| `localqueue-hold` | LocalQueue stopPolicy 只阻塞自己的 Workload |
| `retry-backoff` | AdmissionCheck Retry 的 requeueAfterSeconds 退避 |
| `dra` | DRA DeviceClass → 逻辑资源计费 |
| `resource-transform` | 资源变换：vGPU → total memory/core |
| `tas-slice` | TAS slice 与打散 |
| `configurable-preemption` | PreemptionConfig 规则授权抢占 |
| `fair-sharing-preemption` | Fair Sharing 按份额收回借用 |
| `provisioning-request` | 预留期间置备、失败重试、延迟 Ready |
| `admission-fair-sharing` | AFS 历史用量排序 |
| `concurrent-admission` | spot → reserved 的 Flavor 迁移 |
| `admission-gate` | 配额前外部闸门 |
| `hierarchical-limits` | Cohort 级 borrowing limit |
| `implicit-cohort` | 隐式 Cohort 借用 |
| `cohort-shared-pool` | 显式 Cohort 用自己的 nominal 作为共享池 |
| `multikueue` | Manager/worker 提名、选中、清理 |
| `multikueue-failover` | 选中 worker 失联、超时切换、恢复后清理 |

示例：

```text
$ go run ./cmd/minikueue -scenario tas -ticks 1

== MiniKueue / tas ==
Topology-aware placement packs a 4-GPU job into one rack
...
--- tick 1 ---
  [QuotaReserved] default/job-a: reserved {cpu=4, nvidia.com/gpu=4, pods=4} on tas
  [Admitted] default/job-a: workload admitted
  [QuotaReserved] default/job-b: reserved {cpu=4, nvidia.com/gpu=4, pods=4} on tas
  [Admitted] default/job-b: workload admitted
  default  job-a  Admitted  tas[g0/tas-flavor,tas:rack/rack-a]
  default  job-b  Admitted  tas[g0/tas-flavor,tas:rack/rack-b]
  default  job-c  Pending
  usage: tas={cpu:8,nvidia.com/gpu:8,pods:8}
```

## 架构

```text
cmd/minikueue
  └── internal/scenario
        └── internal/controller
              ├── internal/queue
              ├── internal/scheduler
              │     └── internal/cache
              ├── internal/store
              └── internal/metrics
```

- `api`：Kubernetes 风格的对象、资源量、条件、校验、Topology/CapacityProvider。
- `store`：内存 API server，对象以 namespace/name 为 key；保存 CQ/LQ/Cohort/Flavor/Topology/Domain/Provider。
- `cache`：配额与拓扑账本，记录 CQ/Flavor/Resource 的预留量和 TAS domain 用量。
- `queue`：优先级/创建时间排序，StrictFIFO/BestEffortFIFO 过滤，公平顺序。
- `scheduler`：Flavor 分配、Cohort 借用、借用式加权 DRS、TAS 放置、抢占/回收。
- `controller`：Tick 协调、两阶段准入、驱逐、重排队、StopPolicy、WaitForPodsReady、动态回收、有效配额、指标。
- `scenario`：可复现场景。
- `metrics`：Prometheus 文本子集。

## 语义覆盖

已实现：

- Workload/PodSet 资源汇总（含自动 `pods`、reclaimable pods）；
- LocalQueue/ClusterQueue/ResourceFlavor/Cohort/AdmissionCheck/PriorityClass；
- 多资源组、多 Flavor、first-fit；
- nominal/borrowing/lending（CQ 级）；
- 扁平/层级 Cohort 配额聚合；
- BestEffortFIFO/StrictFIFO；
- 同 CQ 抢占、Cohort 回收、借用时抢占；
- 最小 victim 集合、同优先级创建时间 tie-break；
- 借用式加权 DRS 公平排序；
- 两阶段准入、AdmissionCheck Pending/Ready/Rejected、Auto 检查；
- StopPolicy Hold/HoldAndDrain；
- 停用、最大执行时间、简单重排队；
- namespaceSelector（null vs `{}`，matchLabels + matchExpressions）；
- LocalQueue stopPolicy（与 CQ stopPolicy 叠加）；
- TAS-lite：Topology/TopologyDomain、Required/Preferred/Unconstrained/Group、slice、打散；
- WaitForPodsReady + MarkPodsReady；
- 动态回收（ReportReclaimable）；
- DQO-lite：CapacityProvider → effective nominal；
- DRA-lite：DeviceClass → 逻辑资源、重复计费保护；
- 资源变换：Replace/Retain、multiplyBy（资源名/固定整数）、outputs、与 DRA 去重；
- ProvisioningRequest-lite：预留期间置备、失败 Retry、延迟 Ready、retryCount 跨预留保留；
- 调度诊断：`-explain` 逐资源/逐 Flavor 输出 CQ nominal、Cohort 总量、borrow/lend、拓扑与抢占证据；`-what-if` 做单变量配额反事实；
- Configurable Preemption-lite：trigger/scope/priority/numericLabels/labelSelector/clusterQueueSelector；
- Fair Sharing 抢占-lite：LessThanOrEqualToFinalShare / LessThanInitialShare；
- 指数退避：BackoffConfig（base/max/multiplier）；
- Admission Fair Sharing-lite：历史用量、半衰期、Entry Penalty、LocalQueue 权重、Cohort 权重；
- Concurrent Admission-lite：更优 Flavor 迁移；
- MultiKueue-lite：AllAtOnce/Incremental、选中与清理、`Down` + `WorkerLostTimeout` 失联切换；
- Admission gate、Cohort 级 borrowing/lending limit、隐式 Cohort；
- SchedulingStats（reason + underlyingCause）；
- AdmissionCheck Retry（requeueAfterSeconds + retryCount；retryCount 跨重新预留保留）；
- 最大执行时间跨抢占/重排队累计（AdmittedAt + ExecutionTicks）；
- TAS 跨层物理账本：粗层分配下钻、细层分配上卷，rack/hostname 不双计（`Scheduler.TopologyUsage`）；
- Store spec 深拷贝、`UpdateWorkload` 保留 UID/spec（只允许 active/admissionGates）、写入时校验 LQ/CQ/priority/DRA 引用；
- DRA 映射必须指向非空、非 `pods`、被 CQ 覆盖的资源；
- Concurrent 迁移保留 AdmissionCheck Ready 和 PodsReady，不把已准入作业降级；
- 驱逐（抢占/排空/超时）保留 AdmissionCheck retryCount；
- 动态回收同时释放配额和 TAS 拓扑用量；
- DQO 漏报资源按 0 计，未编排 Flavor 才回退静态 nominal；
- MultiKueue 提名列表（Nominated）可观测；
- Visibility（QueueStatus + CLI `-query`）；
- 指标/事件。

未实现（详见教程第 15 章）：

- 真实 Kubernetes CRD/API server/webhook/informer；
- 真实 Job/Pod/节点调度；
- Fair Sharing 的 AlmostLCA、目标 CQ 排序、预抢占；Cohort 权重已有；
- 层级 Cohort 共享池的子树隔离与逐层出借账本（当前按根树扁平聚合，Cohort lendingLimit 逐成员收紧）；
- TAS 的节点热替换/节点与设备容量推导/多层 slice；group 视为硬约束，slice 固定最空优先；
- 资源变换的 Quantity/小数、Configuration 热更新、CQ 覆盖校验；
- ProvisioningRequest 的真实对象、云扩容、ProvisioningRequestConfig、PodSetUpdates；
- Configurable Preemption 的 preemptor selector、规则优先级、逐层 ParentCohort、CRD；
- DRA 的真实设备分配、MultiKueue 的跨集群网络/凭据/重复执行语义、Concurrent 的 Parent/Variant；
- `retryStrategy` 的最大重试次数、真实 scheduling gates；
- 持久化/HA/leader election/Visibility HTTP API；
- 完整 metrics/labels。

## 验证

```bash
gofmt -l .
go vet ./...
go test ./... -count=1
go test ./... -cover
go test ./internal/scheduler -bench BenchmarkTryAdmit -benchmem -run '^$'
```

在本机（Windows 11、Go 1.27.0、Intel Core Ultra 9 285H）的结果：

- 测试数量、覆盖率和基准以 [../VERIFICATION.md](../VERIFICATION.md) 为准；
- 所有 26 个引擎场景 + 2 个 MultiKueue 演示可运行；`-count=10`、`-shuffle=on` 和随机不变量测试均通过。

这些数字只代表教学实现，不代表生产 Kueue 性能。

## 设计说明

更详细的算法、状态机、取舍和代码走读见教程：

- [第 13 章 · MiniKueue 设计](../docs/13-mini-kueue-design.md)
- [第 14 章 · MiniKueue 实现与验证](../docs/14-mini-kueue-walkthrough.md)
- [第 15 章 · 与真实 Kueue 的差距](../docs/15-gap-to-real-kueue.md)
- [第 19 章 · MiniKueue 扩展实验室](../docs/19-mini-kueue-extension-lab.md)
- [第 27 章 · 验证工程](../docs/27-verification-engineering.md)
