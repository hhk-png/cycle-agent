# 第 17 章 · Kueue 控制器内部原理

前 16 章主要从使用者的角度讲 Kueue。本章换一个视角：**如果打开 Kueue 的源码，它到底是怎么运转的？** 理解内部结构之后，你才能解释一些看似奇怪的行为，例如"为什么队列已经空了调度器还不准入"、"为什么改了一个字段要等下一次快照"、"为什么控制器重启后 pending 作业没有立刻恢复"。

> 版本提示：Kueue 迭代很快，源码目录和函数名会变化。本章描述的是 v0.20.x 稳定线（截至 2026-11 为 v0.20.5）的主干结构；v0.21 尚未 GA，阅读时请以你 checkout 的 release 为准。我们关注的是**架构与数据流**，不是逐行 API。

## 17.1 源码地图

Kueue 仓库的主干可以按"入口、控制器、调度核心、集成、基础设施"五层来读。以下是 v0.20.x 稳定线的目录（不同版本会有增删，读你 checkout 的 release）：

```text
kueue/
├── cmd/kueue/                          # controller-manager 入口
├── pkg/
│   ├── config/                         # Configuration、默认值、校验
│   ├── features/                       # feature gates
│   ├── cache/
│   │   ├── queue/                      # QueueManager：heads/backlog/inadmissible
│   │   │   ├── manager.go
│   │   │   ├── cluster_queue.go
│   │   │   ├── local_queue.go
│   │   │   ├── inadmissible_workloads.go
│   │   │   └── afs/                    # Admission Fair Sharing 账本
│   │   ├── scheduler/                  # 调度缓存与快照
│   │   │   ├── cache.go
│   │   │   ├── snapshot.go
│   │   │   ├── clusterqueue.go
│   │   │   ├── cohort.go
│   │   │   ├── fair_sharing.go
│   │   │   ├── tas_cache.go
│   │   │   └── dra/
│   │   └── hierarchy/                  # Cohort 树
│   ├── scheduler/                      # 调度循环
│   │   ├── scheduler.go
│   │   ├── flavorassigner/
│   │   ├── preemption/
│   │   └── fair_sharing_iterator.go
│   ├── workload/                       # Workload 辅助逻辑、条件、优先级
│   ├── controller/
│   │   ├── core/                       # Workload/Queue/Flavor/Check/DQO 控制器
│   │   ├── jobframework/               # 作业集成框架与通用 reconciler
│   │   ├── jobs/                       # batch/Job、JobSet、Kubeflow、Ray 等适配
│   │   ├── admissionchecks/            # ProvisioningRequest、MultiKueue
│   │   ├── concurrentadmission/        # Parent/Variant
│   │   └── tas/                        # TAS 热替换等
│   ├── webhooks/                       # 默认值、校验、不可变字段
│   ├── visibility/                     # Visibility API
│   ├── dra/                            # DRA 辅助
│   └── util/                           # 资源量、优先级、索引等工具
└── test/                               # envtest、集成测试、性能测试
```

读源码时建议按这个顺序：

1. `cmd/kueue/main.go`：控制器如何装配；
2. `pkg/controller/core/workload_controller.go`：Workload 如何被协调；
3. `pkg/cache/queue/manager.go`：作业如何进入 heads/backlog；
4. `pkg/cache/scheduler/cache.go` + `snapshot.go`：配额账本与快照；
5. `pkg/scheduler/scheduler.go`：一次调度循环；
6. `pkg/scheduler/flavorassigner/` + `pkg/scheduler/preemption/`：Flavor 与抢占；
7. `pkg/controller/jobframework/`：真实 Job 如何被暂停/放行。

一个常见误区是去 `pkg/queue` 找 QueueManager——它现在在 `pkg/cache/queue`。另一个误区是把 `pkg/cache/scheduler/cache.go` 当成"调度器"；它只是账本，决策在 `pkg/scheduler`。

## 17.2 进程启动：控制器如何装配

`kueue-controller-manager` 基于 controller-runtime。启动过程可以简化为：

```text
main
  ├── 解析 flags / 读取 Configuration
  ├── 构造 manager（scheme、metrics、webhook、leader election）
  ├── 注册 informer / indexer
  ├── 构造 Cache        = scheduler cache（pkg/cache/scheduler）
  ├── 构造 QueueManager = queue cache（pkg/cache/queue）
  ├── 构造 Scheduler    = scheduler.New(queues, cache, client, recorder, opts...)
  ├── 注册 controllers（Workload、Queue、Flavor、AdmissionCheck、DQO、Job 集成）
  ├── 注册 webhooks
  └── manager.Start(ctx)
        ├── leader election
        ├── 启动 informer
        ├── 启动 controller workers
        └── 启动 scheduler 循环
```

注意这里有两个不同的 "cache"：

- `pkg/cache/queue.Manager`：队列运行时状态（谁在排队、谁是 head、谁 inadmissible）；
- `pkg/cache/scheduler.Cache`：配额账本（谁占了多少、CQ/Cohort 的 usage）。

它们都从 informer 事件更新，但职责不同。排障时"队列里有没有这个 Workload"和"账本里有没有扣配额"要分开查。

几个关键点：

- **只有 leader 执行写操作。** 多副本部署时，controller-runtime 的 leader election 保证同一时刻只有一个 manager 在 reconcile 和调度；其他副本待命。
- **Cache 和 QueueManager 是进程内状态。** 它们从 API server 的 informer 事件重建；控制器重启后不会丢数据，但需要重新 watch 全量对象。
- **调度不是每个 Workload 一个 goroutine。** Scheduler 是一个受控循环，收到通知后做一次快照调度。这样避免了大量并发决策互相踩踏。
- **Webhook 与 controller 是两条路径。** 创建/更新对象先经过 webhook 校验，再进入 informer 和 reconcile。

## 17.3 QueueManager：队列的运行时状态

`LocalQueue` 和 `ClusterQueue` 是 API 对象，但"队列里现在有哪些 pending Workload、队头是谁"不是 API 字段，而是 QueueManager 维护的运行时状态。

### 17.3.1 它维护什么

对每个 ClusterQueue，QueueManager 维护：

- **heads**：按优先级/创建时间排好序的 pending Workload；
- **backlog**：因为 inadmissible、暂停、重排队等原因暂时不能参与调度的 Workload；
- **LocalQueue 视图**：每个 namespace 的 LocalQueue 对应哪些 Workload；
- **inadmissible 集合**：例如 namespaceSelector 不匹配、Flavor 不存在的 Workload。

对使用者来说，`kubectl get clusterqueue -o wide` 里的 `PENDING WORKLOADS` 就来自这里。

### 17.3.2 为什么需要 backlog

如果每次调度都扫描所有 Workload，规模上来后会很慢。QueueManager 把"可调度"和"暂不可调度"分开：

- heads 里的 Workload 参与本次调度；
- backlog 里的 Workload 在条件变化时（例如 CQ 重新 Active、配额释放、Workload 被激活）被重新放回 heads。

这减少了无意义的重复尝试，也让 StrictFIFO 的"队头阻塞"有明确的数据结构支撑。

### 17.3.3 队列事件如何触发调度

QueueManager 的变化会通知 Scheduler。典型触发源：

- 新 Workload 创建/更新；
- Workload 被驱逐、完成、停用；
- ClusterQueue/LocalQueue 配置变化；
- Cache 中的配额释放；
- AdmissionCheck 状态变化；
- 周期性 resync。

Scheduler 不需要轮询每个对象，而是被"有事情发生"唤醒。这是 controller-runtime 事件驱动模型的典型用法。

### 17.3.4 关键方法

读 `pkg/cache/queue/manager.go` 时重点看：

| 方法 | 作用 |
|---|---|
| `AddOrUpdateWorkload` | Workload 进入/更新队列；决定 heads/backlog/inadmissible |
| `RequeueWorkload` | 驱逐/失败后按原因重排队 |
| `Heads(ctx)` | 取出每个活跃 CQ 的队头，供调度器提名 |
| `PopFrom(cq)` | 弹出队头；StrictFIFO 下后续扫描会停止 |
| `PendingWorkloads` / `Pending` | 供状态更新和指标 |
| `QueueAssociatedInadmissibleWorkloadsAfter` | 条件变化后把 inadmissible 放回队列 |

`Heads` 不是"所有 pending"，而是"当前可参与调度的队头"。这解释了为什么 `kubectl get workloads` 看到一堆 Pending，调度器却只处理少数几个。

## 17.4 Cache：配额的唯一账本

Kueue 的 `pkg/cache` 是调度决策的账本。它回答三个问题：

1. 某个 ClusterQueue 在某个 Flavor/Resource 上用了多少？
2. 某个 Cohort 子树总共用了多少、还剩多少可借？
3. 某个 Workload 当前占用了哪些 Flavor/Resource？

### 17.4.1 数据结构

从概念上，Cache 维护：

```text
Cache
├── clusterQueues: name -> ClusterQueue runtime
│     ├── usage: flavor -> resource -> quantity
│     ├── admittedWorkloads
│     └── cohort / borrowing / lending 状态
├── cohorts: name -> Cohort runtime
│     ├── parent / children
│     └── subtree usage / quota
└── workloads: key -> Workload runtime
      ├── admission（Flavor 分配）
      └── 所属 CQ / LQ
```

真实的 Kueue 还维护 resource flavor 的节点/拓扑信息、DRA 设备计数、TAS 的 domain 容量等，但核心账本仍是"谁在哪个 CQ/Flavor 上占了多少"。

### 17.4.2 Snapshot

调度器不会直接在可变 Cache 上做决策，而是先取一个 **Snapshot**。Snapshot 是某一时刻的一致性视图：

```text
Scheduler.Schedule
  ├── snapshot := cache.Snapshot(ctx)
  ├── queueManager.Heads(snapshot)
  ├── 对每个 Workload 做 fit / preemption 模拟
  └── 决策结果写回 API server，再由 Cache 的 informer 事件更新
```

快照的意义：

- 一次调度过程中，配额视图不会边算边变；
- 抢占模拟可以基于"移除某些 Workload 后"的副本进行；
- 决策和提交分离，失败时不会留下半成品。

MiniKueue 的 `scheduler.TryAdmit` + `removed` 集合就是这一思想的简化版。

### 17.4.3 Cache 如何更新

Cache 不是调度器直接写的，而是通过 informer 事件更新：

- Workload 变成 `QuotaReserved`/`Admitted` → 加入账本；
- Workload 被驱逐/完成/删除 → 释放账本；
- ClusterQueue/Cohort 配置变化 → 重建对应运行时状态；
- ResourceFlavor/Topology 变化 → 更新容量与拓扑信息。

这也是为什么"改了 CQ 配额后，调度不会在下一条 API 请求里立刻生效"：需要等 informer 收到更新、Cache 重建、调度循环被唤醒。

## 17.5 Scheduler：一次调度循环

Scheduler 的核心可以概括为：

```text
schedule(ctx):
  snapshot := cache.Snapshot(ctx)
  heads    := queues.Heads(ctx)
  entries  := nominate(ctx, heads, snapshot)
  for each entry:
      processEntry(ctx, entry, snapshot)
  requeueAndUpdate(...)
  update metrics / second pass
```

更细的调用链：

```text
Scheduler.Start
  └─ schedule
       ├─ cache.Snapshot
       ├─ queues.Heads
       ├─ nominate
       │    └─ nominateWorkload
       │         └─ flavorassigner.Assign
       ├─ processEntry
       │    ├─ updateAssignmentIfNeeded
       │    ├─ fits
       │    ├─ preemption.Preemptor（需要时）
       │    ├─ issuePreemptions
       │    ├─ admit
       │    │    ├─ prepareWorkload
       │    │    └─ assumeWorkload
       │    └─ waitForPodsReadyIfNeeded
       ├─ requeueAndUpdate
       └─ recordWorkloadAdmissionMetrics
```

`nominate` 和 `processEntry` 的分离很关键：先"提名"一批候选 entry，再按公平/优先级迭代处理。这样 Fair Sharing 的 iterator 可以在批次层面调整顺序，而不必每次重新扫描全队列。

真实实现比这复杂，但骨架一致。下面拆开看。

### 17.5.1 调度顺序

Kueue 不是简单地按 CQ 名字遍历。它要兼顾：

- 队列策略（StrictFIFO/BestEffortFIFO）；
- 优先级；
- 公平分享（Fair Sharing）；
- Cohort 的借用/回收；
- 抢占策略。

公平分享开启时，CQ 的处理顺序会受加权 share 影响；同一 CQ 内，Workload 按优先级和创建时间排序。

### 17.5.2 Flavor 分配

对一个 Workload，Scheduler 需要为每个资源组选择一个 Flavor。核心步骤：

1. 汇总 Workload 的 PodSet 资源；
2. 对每个资源组，按 Flavor 顺序尝试；
3. 对每个候选 Flavor，检查：
   - CQ 的 nominalQuota；
   - Cohort 的聚合配额；
   - borrowingLimit / lendingLimit；
   - 资源组约束（同组资源必须同 Flavor）；
   - TAS/DRA 等额外可行性。
4. 找到可行组合后，生成 `status.admission` 的 `podSetAssignments`。

`pkg/scheduler/flavorassigner` 负责这部分。它的输入是 Snapshot、Workload、CQ，输出是每个 PodSet 的 Flavor 分配和资源用量。

### 17.5.3 抢占

如果直接分配失败，Scheduler 会尝试抢占（前提是策略允许）。抢占的核心不是"找一个受害者"，而是：

1. 收集候选人（同 CQ、Cohort 内借用者等）；
2. 按启发式排序；
3. 逐个模拟移除，直到 Workload 能放下；
4. 反向最小化 victim 集合；
5. 提交驱逐和准入。

真实 Kueue 还会处理 Fair Sharing 抢占策略、`flavorFungibility`、预抢占等。MiniKueue 的 `findPreemption` 是这条路径的教学版。

### 17.5.4 提交决策

调度器算出决策后，需要：

- 更新 Workload 的 `status.conditions`；
- 写 `status.admission`；
- 驱逐 victim（如果被抢占）；
- 更新 Cache（通过事件或直接通知）；
- 记录事件/指标。

这里有一个重要约束：**决策和提交之间可能有竞态**。例如调度器决定准入时，Workload 可能被用户停用，或者 CQ 被改小。因此提交前要重新校验关键条件，失败则放弃本次决策。

## 17.6 Workload 控制器

`pkg/controller/core/workload_controller.go` 负责 Workload 的生命周期协调。它处理：

- 条件（Conditions）的维护；
- 驱逐（Evicted）和重排队；
- `active` 字段；
- 最大执行时间；
- `PodsReady` 超时；
- 与 AdmissionCheck 的交互；
- 完成/删除时的清理。

### 17.6.1 条件驱动，而不是单一状态字段

真实 Kueue 没有 `Workload.status.state` 这样的枚举。它用 Conditions 表达状态：

```text
QuotaReserved=True  → 配额已预留
Admitted=True       → 所有检查通过
Evicted=True        → 被驱逐，Reason 说明原因
Finished=True       → 完成
QuotaReserved=False（Reason=Inadmissible）→ 结构性不可准入
```

因此判断"一个 Workload 现在是什么状态"需要组合条件，而不是读一个字段。MiniKueue 为了 CLI 和测试方便保留了 `State`，同时维护 Conditions 作为对照。

注意一个容易混淆的点：**真实 Kueue 没有独立的 `Inadmissible` 条件类型**。它是在 `QuotaReserved=False` 上使用 `Reason=Inadmissible`（以及更细的 reason）。MiniKueue 为了方便 CLI/测试额外维护了一个 `Inadmissible` 条件，这是教学扩展，不要把它当成真实 API。同理，`Pending` 不是条件类型，而是"`QuotaReserved=False` 且原因不是结构性不可准入"的统称。

### 17.6.2 驱逐与重排队

驱逐不是简单地把条件设为 `Evicted=True`。控制器还要：

- 释放 Cache 中的配额；
- 决定是否重排队；
- 计算退避时间（`requeueState`）；
- 超过重试上限时停用 Workload；
- 通知 Job 集成暂停/清理 Pod。

这些逻辑分散在 Workload controller 和 scheduler/preemption 之间，读代码时要顺着"谁写条件、谁释放 Cache、谁触发重排队"三条线看。

### 17.6.3 一个 Workload 的条件时间线

典型的时间线：

```text
t0  Workload 创建，QuotaReserved=False
t1  Scheduler 预留，写 status.admission，QuotaReserved=True
t2  AdmissionCheck 全部 Ready，Admitted=True
t3  Pods 全部 Ready，PodsReady=True
t4  Job 完成，Finished=True
```

异常分支：

```text
t2' AdmissionCheck Retry -> 释放 admission，QuotaReserved=False，requeueState.requeueAt
t3' 超过 timeout -> Evicted=True(reason=PodsReadyTimeout)，requeueState
t4' 被抢占 -> Evicted=True(reason=Preempted)，Preempted=True
t5' 超过 max exec -> Evicted=True(reason=MaximumExecutionTime)，active=false
```

排障时把时间线和事件对上，比单看最终状态有效。

## 17.7 Job 集成框架

`pkg/controller/jobframework` 把"不同作业框架"抽象成统一接口。集成层要做的事情：

```text
Reconcile(Job)
  ├── 读取 Job 的 Pod 模板
  ├── 构造/更新 Workload
  ├── 如果 Workload 未 Admitted：暂停 Job
  ├── 如果 Workload Admitted：放行 Job
  ├── 如果 Workload Evicted：暂停并清理
  └── 同步完成/失败状态
```

### 17.7.1 通用 reconciler

Kueue 为常见框架提供通用逻辑，各集成只需要实现差异部分：

- Pod 模板从哪来；
- 如何暂停/恢复；
- 副本数/角色如何映射成 PodSet；
- 完成/失败如何判定；
- 是否需要注入 nodeSelector/tolerations。

### 17.7.2 为什么不能直接改 Pod

Kueue 不直接创建 Pod，也不直接调度 Pod。它通过暂停 Job 来阻止 Pod 创建。这是"作业级"语义的关键：**在 Pod 存在之前做决定**。

对于没有 suspend 字段的对象，集成层需要用等价机制：Deployment 通过 webhook 注入队列标签、再由 Pod 集成逐 Pod 使用 scheduling gate；StatefulSet 在 v0.20 用 Plain Pod Group 管理成员，**不支持扩缩**，不要用 `replicas=0` 暂停。具体行为以目标版本的集成文档为准。

## 17.8 AdmissionCheck 控制器

AdmissionCheck 是"配额预留后的外部闸门"。Kueue 本身只负责：

- 在 Workload 上维护 `status.admissionChecks`；
- 等待所有检查 `Ready`；
- 处理 `Retry` 和 `Rejected`。

真正的检查逻辑由外部控制器实现：

- **ProvisioningRequest**：向 Cluster Autoscaler 请求扩容；
- **MultiKueue**：把 Workload 分发到 worker 集群；
- **自定义控制器**：许可证、审批、安全扫描等。

### 17.8.1 状态传播

```text
Workload QuotaReserved
  → Kueue 写 status.admissionChecks[name]=Pending
  → 外部控制器观察并执行
  → 外部控制器写 Ready/Retry/Rejected
  → Kueue 读取并决定 Admitted / 释放 / 停用
```

`Retry` 会释放配额并退避重试；`Rejected` 会停用 Workload。这个区分非常重要：前者是"暂时不行"，后者是"永远不行"。

## 17.9 Webhook 与校验

Kueue 的 webhook 负责：

- 默认值（例如 `queueingStrategy`、`stopPolicy`）；
- 合法性校验（资源组、Flavor、borrow/lend、selector）；
- 不可变字段（例如已准入 Workload 的关键字段）；
- 一些状态同步和迁移。

Webhook 是 API 边界上的最后一道防线。它不能替代控制器逻辑，但能阻止明显非法的对象进入系统。

运维上要关注：

- webhook 证书是否有效；
- webhook 是否可用（不可用会导致创建/更新失败）；
- 升级时 CRD schema 和 webhook 是否兼容。

## 17.10 并发、一致性与故障恢复

### 17.10.1 并发模型

- **Controller-runtime workqueue**：每个 key 串行 reconcile，不同 key 可并发；
- **Scheduler 单循环**：一次只做一个调度决策序列，避免并发决策；
- **Cache 读写锁**：调度读快照，事件更新写 Cache；
- **API server 乐观并发**：resourceVersion 冲突时重试。

### 17.10.2 重启后会发生什么

控制器重启后：

1. informer 重新 list/watch 所有对象；
2. Cache 从 Workload/CQ/Cohort 重建；
3. QueueManager 重新构建 heads/backlog；
4. Scheduler 被唤醒，重新评估 pending Workload；
5. 已 Admitted 的 Workload 不会因为控制器重启而重新调度，但 Cache 需要重新记账。

这就是为什么"重启后 pending 作业没有立刻恢复"可能是正常的：需要等全量 list、缓存重建和调度循环完成。大规模集群上，这个恢复时间可能达到分钟级。

### 17.10.3 常见故障模式

| 故障 | 表现 | 排查 |
|---|---|---|
| Webhook 不可用 | 创建/更新失败 | 看 webhook 证书、Service、Pod |
| Cache 与 API 不一致 | 配额账本漂移 | 看控制器日志、重启后是否恢复 |
| 调度循环卡住 | pending 不收敛 | 看 workqueue 深度、CPU、锁竞争 |
| informer 落后 | 状态更新延迟 | 看 apiserver watch、控制器内存 |
| Leader 切换 | 短时间不调度 | 看 leader election 日志 |
| AdmissionCheck 卡住 | QuotaReserved 但未 Admitted | 看外部控制器和 status |

## 17.11 与 MiniKueue 的映射

| 真实 Kueue | MiniKueue | 说明 |
|---|---|---|
| controller-runtime manager | `controller.Engine` | 单进程、单锁 |
| informer/Cache | `internal/cache` + `internal/store` | 内存对象与账本 |
| QueueManager | `internal/queue` + `Engine.schedule` | 排序、过滤、公平顺序 |
| Scheduler + flavorassigner | `internal/scheduler` | 纯决策层 |
| Snapshot | `Cache.Admitted()` + `removed` 集合 | 克隆 + 模拟移除 |
| Workload controller | `Engine.Tick` | 生命周期、驱逐、重排队 |
| Job 集成 | 直接 Submit Workload | 无真实 Job/Pod |
| Webhook | `api.Validate*` | 内存校验 |
| AdmissionCheck 控制器 | `Engine.evaluateChecks` | 内存状态机，支持 requeueAfterSeconds |
| Admission Fair Sharing | `Engine.afsUsage` + `queue.SortByScore` | 历史用量、衰减、Entry Penalty |
| Concurrent Admission | `Engine.migrateConcurrentAdmissions` | 单 Workload 的 Flavor 迁移 |
| MultiKueue | `internal/multikueue` | 多 Engine、提名、选中、清理 |
| DRA 计费 | `Workload.TotalUsage` + `DeviceClassMappings` | 无真实设备分配 |
| Visibility API | `Engine.QueueStatus` | 只读查询 |
| Leader election/HA | 无 | 单进程 |

这张表也解释了 MiniKueue 为什么能把核心逻辑压缩到几千行：它去掉了 Kubernetes 的对象持久化、watch、并发和集成层，只保留"队列 → 快照 → 决策 → 提交"的主干。

## 17.12 读源码的七个切入点

如果你要深入 Kueue 源码，建议从这七个问题出发：

1. **一个 Workload 从创建到 Admitted，经过哪些 reconcile？**
   看 `pkg/controller/core/workload_controller.go`、`pkg/cache/queue/manager.go` 和 `pkg/scheduler/scheduler.go` 的调用链。
2. **Cache 在什么时候增加/减少用量？**
   看 `pkg/cache/scheduler/cache.go` 的 `AddOrUpdateWorkload`/`DeleteWorkload`，以及 Workload 条件变化时的 reconcile。
3. **StrictFIFO 的队头阻塞在哪里实现？**
   看 `pkg/cache/queue` 的 heads/PopFrom 和 scheduler 的扫描终止条件。
4. **抢占的 victim 是怎么选出来的？**
   看 `pkg/scheduler/preemption`、`fair_sharing_iterator.go` 和 `ClusterQueuePreemption` 的四条启发式。
5. **AdmissionCheck 的 Retry 如何释放配额并重排队？**
   看 Workload controller 对 `status.admissionChecks` 的处理，以及 `requeueAfterSeconds` 的使用。
6. **DQO 的 effectiveQuotas 在哪里被消费？**
   看 `pkg/cache/scheduler/clusterqueue.go` 的配额读取路径；调度器、抢占和指标应使用同一份配额。
7. **TAS 的 domain 容量从哪里来？**
   看 `pkg/cache/scheduler/tas_flavor.go`、`tas_flavor_snapshot.go`（具体文件名以你 checkout 的 release 为准）和 `snapshot.go` 的 topology domain usage 构建。

把这些问题对应的调用链画出来，比逐行读代码更有效。

## 17.13 本章小结

- Kueue 的主干是 **QueueManager → Cache Snapshot → Scheduler → 提交决策**。
- 两个 cache 要分清：`pkg/cache/queue` 是队列运行时，`pkg/cache/scheduler` 是配额账本。
- Scheduler 的调用链是 `schedule → nominate → processEntry → updateAssignmentIfNeeded → preemption → admit → requeueAndUpdate`。
- Workload 用 Conditions 表达生命周期，驱逐/重排队由控制器协调；`schedulingStats` 记录原因。
- Job 集成层负责把用户对象转换成 Workload，并在准入前暂停、准入后放行。
- AdmissionCheck 把外部条件接入准入，Retry 与 Rejected 语义不同；Retry 支持 requeueAfterSeconds。
- 并发模型是 workqueue + 单调度循环 + 快照；重启后需要重建缓存。
- MiniKueue 是这条主干的确定性模拟器，适合对照阅读。

---

[← 上一章：现状与未来方向](16-future-roadmap.md) | [返回目录](../README.md) | [下一章：生产落地手册 →](18-production-adoption.md)
