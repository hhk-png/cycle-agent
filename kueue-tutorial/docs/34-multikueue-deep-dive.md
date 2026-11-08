# 第 34 章 · MultiKueue 多集群调度深入

第 9 章已经介绍过 MultiKueue 的主干：manager 提名 worker，某个 worker 先准入，其他副本被清理。本章把这个主干拆到生产可用的粒度：对象怎么建、一次跨集群准入到底经过哪些状态、worker 失联时会发生什么、配额和公平如何跨集群计算、凭据如何管理，以及如何用 MiniKueue 复现故障切换。如果你还没有读过 9.4 节，建议先读那一节建立直觉，再回到本章。

> 版本提示：本章以 Kueue **v0.20.x 稳定线**（截至 2026-11 最新 patch 为 v0.20.5）的 MultiKueue 为背景；v0.21 仅有 devel，未 GA。MultiKueue 自 v0.9 起 beta 默认开启；v1beta2 把 `MultiKueueCluster.spec.kubeConfig` 改成 `spec.clusterSource.kubeConfig`。字段名以你部署版本的 API reference 和 CRD 为准。

## 34.1 为什么单集群队列不够

单集群的 Kueue 能回答"这个团队在当前集群里能不能开始"，但回答不了下面这些问题：

- 公司在三个 region 各有一个 GPU 集群，哪个集群应该跑这个作业？
- 某个集群今天在维护，作业能不能自动去另一个集群排队？
- 云上 spot 池便宜但不稳定，on-prem 池稳定但容量小，如何按成本和可用性选择？
- 训练数据在 region A，跨 region 读取代价很高，如何做数据 locality？
- 一个集群的控制面故障，是否要让整个平台的队列一起停摆？

这些问题的共同点是：**配额和作业不在同一个故障域里**。你可以用联邦控制面、Argo CD、自研分发器解决一部分，但它们通常不提供"作业级配额 + 单一选中 + 失败清理"的语义。MultiKueue 把 Kueue 的队列语义扩展到多个集群，同时保留一个关键约束：**一个 Workload 最终只在一个 worker 上运行**。

MultiKueue 不是集群联邦，也不是跨集群调度器。它不移动 Pod、不迁移运行中的作业、不做跨集群网络。它做的是：

```text
manager 集群：排队、配额、提名、选中、状态聚合
worker 集群：各自独立地排队、准入、运行 Job
```

这个边界非常重要。理解它之后，很多"为什么不能……"的问题就有了答案。

## 34.2 对象模型

MultiKueue 由四类对象组成：

| 对象 | 作用 | 作用域 |
|---|---|---|
| `MultiKueueCluster` | 描述一个 worker 集群及其连接方式 | 集群级 |
| `MultiKueueConfig` | 列出参与分发的 worker 集合 | 集群级 |
| `AdmissionCheck` | 把 MultiKueue 接入 ClusterQueue 的准入链 | 集群级 |
| `Workload` | manager 上的唯一源对象，携带提名/选中状态 | 命名空间级 |

### 34.2.1 MultiKueueCluster

v1beta2 的连接信息在 `spec.clusterSource` 下，二选一：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: MultiKueueCluster
metadata:
  name: worker-a
spec:
  clusterSource:
    kubeConfig:
      locationType: Secret
      # Secret 必须位于 kueue-controller-manager 所在 namespace
      # （通常是 kueue-system），key 必须是 kubeconfig
      location: worker-a-kubeconfig
```

另一种是 `clusterProfileRef`，需要 `MultiKueueClusterProfile` gate，并依赖 Cluster Inventory API 的 `ClusterProfile`：

```yaml
spec:
  clusterSource:
    clusterProfileRef:
      name: worker-a-profile
```

`locationType: Path` 只建议在开发环境使用：它让控制器直接读 Pod 文件系统上的 kubeconfig，路径受 `MultiKueueKubeConfigPathValidation` 限制，但仍然是高风险管理方式。

### 34.2.2 MultiKueueConfig

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: MultiKueueConfig
metadata:
  name: platform-workers
spec:
  clusters:
  - worker-a
  - worker-b
  - worker-c
```

`spec.clusters` 的顺序有意义：`AllAtOnce` 会按这个顺序创建副本；`Incremental` 在 `MultiKueueIncrementalDispatcherRespectConfigOrder` 开启时也按这个顺序扩张。

### 34.2.3 AdmissionCheck 与 ClusterQueue

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: AdmissionCheck
metadata:
  name: multikueue
spec:
  controllerName: kueue.x-k8s.io/multikueue
  parameters:
    apiGroup: kueue.x-k8s.io
    kind: MultiKueueConfig
    name: platform-workers
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: team-a
spec:
  admissionChecksStrategy:
    admissionChecks:
    - name: multikueue
  # ... 其余队列/配额字段
```

注意 v1beta2 使用 `admissionChecksStrategy.admissionChecks`，不是 v1beta1 的 `spec.admissionChecks` 列表。升级时这是最常见的漏改点之一。

### 34.2.4 Workload 上的状态

manager 上的 Workload 通过两个字段暴露分发状态：

| 字段 | 含义 | 可变性 |
|---|---|---|
| `status.nominatedClusterNames` | 当前轮次正在考虑的 worker | 每轮可更新 |
| `status.clusterName` | 最终选中的 worker | 设置后不可变 |

`clusterName` 的不可变性是"单一选中"的 API 级保证。即使 manager 之后失联、重启或重新提名，已经选中的 worker 不会被悄悄改掉；要换集群，必须创建新的 Workload。

## 34.3 一次跨集群准入的完整时序

下面是一次成功分发的时序。假设 manager 的 ClusterQueue 已经配置了 MultiKueue AdmissionCheck。

```text
用户                manager Kueue                    worker A              worker B
 |                       |                               |                     |
 |-- 创建 Job ---------->|                               |                     |
 |                       |-- 生成 Workload               |                     |
 |                       |-- 本地 QuotaReserved=True     |                     |
 |                       |-- AdmissionCheck=Pending      |                     |
 |                       |-- dispatcher 提名 [A,B]       |                     |
 |                       |-- 创建 Workload 副本 --------->|                     |
 |                       |-- 创建 Workload 副本 ------------------------------>|
 |                       |                               |-- 本地排队/准入     |
 |                       |<-- A Admitted ----------------|                     |
 |                       |-- 选中 A（clusterName=A）     |                     |
 |                       |-- 删除 B 的副本 ------------------------------------->|
 |                       |-- 在 A 创建 Job 副本 -------->|                     |
 |                       |<-- 状态同步 ------------------|                     |
 |<-- Job 状态更新 ------|                               |                     |
 |                       |                               |-- Job 完成          |
 |                       |<-- Finished ------------------|                     |
 |                       |-- 清理 A 上的远端对象 -------->|                     |
```

把它拆成状态机：

| 阶段 | manager 上发生什么 | worker 上发生什么 |
|---|---|---|
| 1. 本地预留 | Workload 获得 `QuotaReserved=True`，占住 manager 配额 | 尚无对象 |
| 2. 提名 | dispatcher 写 `nominatedClusterNames` | 尚无对象 |
| 3. 分发 | 为每个提名 worker 创建 Workload 副本 | 收到副本，进入自己的队列 |
| 4. 竞争 | 等待某个 worker 的副本变成 Admitted | 各自按本地配额/抢占/AdmissionCheck 决策 |
| 5. 选中 | 第一个被观察到的 Admitted 副本写 `clusterName` | 胜者保留副本；败者副本被删除 |
| 6. 落地 | 通知 Job 集成在胜者上创建真实 Job | Job 创建，Pod 开始运行 |
| 7. 同步 | 持续同步远端 Workload/Job 状态 | 状态变化回传 |
| 8. 完成 | 标记 Finished，触发清理 | 远端对象按 GC 策略删除 |

几个容易忽略的细节：

1. **manager 配额在分发前就已占用。** 所以 manager 的 nominalQuota 是"在途 + 已运行"的总闸门，不是"只统计已经跑起来的作业"。
2. **worker 的准入是独立决策。** manager 不能强迫 worker 准入；worker 自己的 CQ、Cohort、AdmissionCheck、抢占策略仍然生效。
3. **选中不是事务。** 两个 worker 可能几乎同时 Admitted；manager 以自己观察到的第一个为准，然后清理另一个。被清理的 worker 上可能已经创建了 Pod，所以清理必须幂等，作业也要能容忍"启动后被撤销"。
4. **Job 副本带 `kueue.x-k8s.io/prebuilt-workload-name` 标签。** 它告诉 worker 的 Job 集成复用已经存在的 Workload，而不是再生成一个。
5. **跨集群状态同步有延迟。** manager 看到的 Admitted 可能比 worker 实际发生晚几秒；不要把 manager 的时间戳当成精确的执行时间。

## 34.4 Dispatcher：三种提名策略

Dispatcher 决定"这一轮让哪些 worker 参与竞争"。Kueue 提供三种：

### 34.4.1 AllAtOnce

默认策略。一次把 Workload 复制到 `MultiKueueConfig.spec.clusters` 里的所有 worker。

优点：

- 选中延迟最低，谁先有空谁赢；
- 实现简单，状态容易推理。

缺点：

- 副本数量等于 worker 数量，manager 和 worker 的 API 负载都更高；
- 可能把作业发给明显放不下的集群，浪费一次调度；
- 跨 region 创建对象有网络成本。

适合：worker 数量少（个位数）、作业对启动延迟敏感、集群之间容量差异不大。

### 34.4.2 Incremental

分批提名。默认每轮最多 3 个 worker，等待 5 分钟仍无准入就扩大一批。

```text
round 1: [worker-a]
         └─ 5 分钟内没有 Admitted
round 2: [worker-a, worker-b]
         └─ 5 分钟内没有 Admitted
round 3: [worker-a, worker-b, worker-c]
```

两个 gate 影响行为：

- `MultiKueueIncrementalDispatcherRespectConfigOrder`：按 `MultiKueueConfig.spec.clusters` 的顺序扩张；关闭后按字母序。
- `MultiKueueIncrementalDispatcherConfig`：允许通过配置调整 `stepSize`。

```yaml
multiKueue:
  incrementalDispatcherConfig:
    stepSize: 1
```

`stepSize: 1` 的典型用法是"先试最便宜的 on-prem 集群，放不下再试 cloud"。

注意"轮次"只在实际提名时推进：选中 worker 失联后的等待 tick 不应该消耗 Incremental 的扩张预算，否则一次网络抖动后可能突然提名所有 worker。MiniKueue 用独立的 `rounds` 计数器实现这一点（第 34.12 节）。

适合：worker 多、跨云/跨 region 成本差异大、作业可以接受几分钟的启动延迟。

### 34.4.3 External

由外部控制器写 `status.nominatedClusterNames`。Kueue 只负责复制和选中，提名逻辑完全自定义。

这给了你实现成本、碳排、数据 locality、维护窗口等策略的空间，但也意味着：

- 外部控制器要处理幂等、并发和字段所有权；
- 需要 `WorkloadRequestUseMergePatch` gate，或使用 `kueue-admission` field manager，否则 SSA 字段所有权会阻止写入；
- 提名错误会直接变成"作业去了错误的集群"，必须有审计和回滚。

### 34.4.4 怎么选

| 场景 | 建议 |
|---|---|
| 2～5 个同构集群，追求最快启动 | `AllAtOnce` |
| 多 region / 多云，成本差异明显 | `Incremental`，按价格/优先级排序 |
| 需要数据 locality、碳排、维护窗口 | `External` |
| 不确定 | 先用 `AllAtOnce`，用指标确认副本开销后再换 |

## 34.5 Worker 选择与集群画像

即使使用 `AllAtOnce`，你仍然需要决定 worker 的集合和顺序。生产上建议给每个 worker 维护一份"集群画像"：

```yaml
# 逻辑画像，不是 Kueue 原生字段
name: worker-a
region: cn-north
provider: on-prem
gpu-types: [a100, h100]
total-gpus: 64
cost-per-gpu-hour: 0
network-to-data: 10Gb
maintenance-window: "Sun 02:00-04:00"
reliability: high
```

选择函数可以写成：

```text
score(worker, workload) =
    w1 * feasible(worker, workload)
  + w2 * (1 - normalized_queue_delay)
  + w3 * locality(worker, workload.data)
  + w4 * (1 - cost(worker, workload))
  + w5 * reliability(worker)
  - w6 * maintenance_risk(worker, now)
```

约束条件：

- worker 必须有作业需要的 Flavor/设备；
- worker 的 Kueue 版本与 manager 兼容；
- worker 的配额足够容纳 Workload（至少理论上）；
- 维护窗口内的 worker 不应被提名；
- 凭据有效的 worker 才能进入候选集。

一个常见错误是"选当前空闲 GPU 最多的集群"。这忽略了：

- manager 配额可能已经占满，空闲 GPU 再多也过不了 manager；
- 空闲是瞬时值，作业运行几分钟后可能被本地高优先级作业抢占；
- 数据 locality 和网络成本可能比空闲更重要。

## 34.6 一致性、状态与垃圾回收

### 34.6.1 单一事实来源

manager 是唯一的事实来源：

- Workload 的 spec 以 manager 为准；
- worker 上的副本是"执行副本"，不是独立作业；
- worker 对 spec 的修改不会回写 manager；
- worker 的状态会回传，但最终条件由 manager 聚合。

这带来一个设计约束：**不要把需要跨集群协调的状态放在 worker 上**。例如"这个作业已经重试了几次"应该由 manager 或作业框架自己维护，而不是依赖某个 worker 的本地计数。

### 34.6.2 状态同步

manager 与 worker 之间的状态同步包括：

- Workload 条件（`QuotaReserved`、`Admitted`、`Finished`、`Evicted`）；
- Job 状态（成功/失败、Pod 状态）；
- `clusterName` 与 `nominatedClusterNames`；
- 清理信号。

同步是最终一致的。排障时要区分：

| 现象 | 可能原因 |
|---|---|
| worker 已 Admitted，manager 仍显示 Pending | 状态同步延迟 / watch 中断 |
| manager 显示 Admitted，worker 上 Job 不存在 | 落地失败 / 权限问题 / Job 被删 |
| 两个 worker 都显示 Admitted | 选中竞态，manager 正在清理败者 |
| 败者副本一直不消失 | 清理失败 / worker 失联 / GC 未运行 |

### 34.6.3 垃圾回收

远端对象需要清理：

- 败者副本：选中后立即删除；
- 完成/失败的副本：按 `multiKueue.gcInterval` 周期清理；
- 失联 worker 上的副本：恢复后按 origin 标签清理。

manager 通常通过标签识别"这个远端对象是我创建的"，例如 origin 标签。清理必须是幂等的：重复删除、对象已不存在、worker 暂时不可达都应该被容忍。

### 34.6.4 manager 重启

manager 重启后：

1. 从 API server 重建 Workload 与 `clusterName`；
2. 重新连接 worker；
3. 对已选中的 Workload 继续同步状态；
4. 对未选中的 Workload 重新提名；
5. 清理在重启期间产生的重复副本。

因为 `clusterName` 不可变，重启不会导致"重新选一个集群"。但如果重启前刚好处于"两个 worker 都 Admitted、还没写 clusterName"的窗口，重启后仍可能选到其中一个，另一个需要清理。

## 34.7 故障模型

MultiKueue 的故障域比单集群多得多。下面是最常见的几类。

### 34.7.1 worker 失联

症状：

- manager 无法 list/watch worker 对象；
- 连接错误日志；
- 已选中的 worker 长时间没有状态更新。

Kueue 的行为：

- 连接失败按指数退避重试（常见实现是 1 分钟起步，逐步到 10 分钟）；
- `multiKueue.workerLostTimeout` 默认 15 分钟；
- 超过 timeout 后，manager 认为该 worker 丢失，允许重新提名/重新分发。

trade-off：

- timeout 太短：网络抖动就触发重新分发，可能导致重复执行；
- timeout 太长：manager 配额被一个已经死掉的 worker 占着，降低吞吐。

注意：在 `workerLostTimeout` 窗口内，`status.clusterName` 仍然指向失联 worker，Workload 也仍然显示"已选中"。这不是状态错误，而是有意的宽限期：manager 先观察连接是否恢复，避免一次网络抖动就重新分发。只有超时后才会进入重新提名/重新分发路径。

### 34.7.2 重复执行风险

这是 MultiKueue 最重要的语义边界：

> worker 失联不等于作业停止。worker 可能仍然在运行 Job，只是 manager 看不到它。

如果 timeout 后 manager 把作业重新分发到另一个 worker，就可能出现**两个集群同时运行同一个作业**。Kueue 通过 `clusterName` 不可变、清理协议和作业幂等性来降低风险，但不能在所有故障模式下完全消除。

因此：

- 训练作业必须支持 checkpoint/restart；
- 作业要有幂等的输出路径（例如按 run ID 覆盖，而不是追加）；
- 对"绝对不能重复执行"的作业，不要开启过于激进的 failover；
- 监控"同一作业在两个集群同时 Running"的异常。

一个真实的相关缺陷是 kueue#16909：worker 失联超时后作业被重新分发到新 worker，旧 worker 的副本仍在运行；重连后，旧副本因为 `Admitted` 的 `lastTransitionTime` 更早而被误判为"更好的副本"，导致 manager 删掉了新副本、保留了旧副本。根因是选副本时没有把 `status.clusterName` 作为收敛锚点。修复方向是：一旦 `clusterName` 已设置，就以它为准；其他集群上已 Admitted 的副本视为陈旧副本。MiniKueue 的 `cleanupStale(selected)` 采用的就是"以当前选中为准"的策略，但它没有真实的 watch 重连和 `lastTransitionTime` 竞态，不能替代真实环境的验证。

这也说明：**"失联后重新分发"和"重连后收敛"是两件事**。前者要处理超时与配额，后者要处理"哪个副本才是真的"；两者都要有幂等和审计。

### 34.7.3 manager 故障

manager 是单点（虽然有 leader election 多副本）。manager 全部不可用时：

- 已在 worker 上运行的作业继续运行；
- 新的 Workload 无法排队/分发；
- 状态不再同步；
- 恢复后 manager 从 API server 重建状态。

所以生产上要：

- manager 多副本 + leader election；
- etcd 备份；
- worker 侧不要依赖 manager 的实时状态做关键决策；
- 为 manager 故障准备降级 Runbook。

### 34.7.4 凭据过期

worker 的 kubeconfig/token 过期后：

- 连接失败，进入退避；
- 已运行作业不受影响；
- 新分发失败；
- 需要轮换 Secret 并观察连接恢复。

建议：

- 使用短期 token + 自动轮换；
- 监控连接状态和证书过期时间；
- 不要在多个 worker 之间复用同一凭据。

### 34.7.5 版本偏移

manager 与 worker 的 Kueue 版本不一致时：

- CRD schema 可能不兼容；
- 新的状态字段可能被旧 worker 忽略；
- webhook 默认值可能不同；
- 升级顺序可能影响分发。

建议：

- manager 与 worker 保持同一 minor 版本；
- 至少保证 CRD 兼容；
- 升级时先升级 worker，再升级 manager；或先在 staging 做 round-trip。

### 34.7.6 配额漂移

manager 的配额是逻辑值，worker 的实际容量可能变化：

- 云上节点缩容；
- worker 本地队列被其他租户占满；
- 维护窗口；
- DRA 设备不可用。

结果是"manager 认为放得下，worker 放不下"。这不会破坏正确性，但会降低分发效率。解决方式是：

- 定期校准 manager 配额与 worker 总容量；
- 用 DQO/CapacityProvider 把实际容量反馈给 manager（如果版本支持）；
- 监控提名成功率与 worker 侧 Pending 时间。

## 34.8 配额、公平与抢占

### 34.8.1 manager 配额是总闸门

一个常用的规划公式：

```text
manager nominalQuota(resource) ≈ Σ worker 可用配额(resource) × 安全系数
```

安全系数通常小于 1（例如 0.8～0.95），因为：

- 不是所有 worker 都随时可用；
- 作业有启动/清理开销；
- 需要留出故障切换余量。

如果 manager 配额太低：worker 有容量但作业进不来，浪费资源。
如果 manager 配额太高：大量作业被提名到放不下的 worker，增加 API 负载和排队时间。

### 34.8.2 公平分享是"每个 worker 内部"的

MultiKueue 不提供跨集群的全局公平分享。每个 worker 有自己的 Cohort/CQ/权重，manager 只负责提名。

这意味着：

- 同一个租户在不同 worker 上的份额可能不同；
- manager 层面的"公平"只体现在提名顺序；
- 如果你需要全局公平，需要自己实现 External dispatcher，或接受"局部公平"。

一个实用做法是：让每个 worker 的 CQ 结构保持一致（相同名字、相同权重、相同 Cohort 关系），这样局部公平至少是可比的。

### 34.8.3 抢占

worker 内部的抢占仍然由该 worker 的 Kueue 决定。跨集群的风险是：**所有副本同时尝试抢占**，造成不必要的全局扰动。

Kueue 的 `MultiKueueOrchestratedPreemption`（KEP-8303）通过 preemption gate 协调：

- 每个 MultiKueue Workload 的副本默认带一个关闭的 preemption gate；
- manager 侧的编排控制器一次只打开一个副本的 gate；
- 打开后等待 `SingleClusterPreemptionTimeout`（Beta 起可通过 Configuration 配置，默认 5 分钟）；
- 如果该副本没有完成抢占，再打开下一个；
- 被 gate 阻塞的 Workload 会带 `BlockedOnPreemptionGates` 条件。

该 gate 在 **v0.20 已毕业到 Beta 并默认开启**（KEP-8303，2026-09-25 毕业），不应再按 alpha/硬编码超时来描述。

这解决了"每个 worker 都以为自己是天选之子"的问题，但也增加了延迟和状态复杂度。生产上要监控：

- gate 打开/关闭的频率；
- 抢占超时；
- 因 gate 阻塞而饥饿的队列。

### 34.8.4 资源变换与 DRA

如果 worker 使用不同的设备模型（例如 A100 vs H100），manager 的资源变换/DRA 映射必须与 worker 兼容。否则：

- manager 按一种资源计费，worker 按另一种资源计费；
- 提名成功但 worker 侧放不下；
- 账本出现"逻辑上够、物理上不够"的偏差。

建议在每个 worker 上保持相同的 Flavor 名称和资源变换配置；如果做不到，至少保证 manager 的资源名是 worker 的超集。

## 34.9 安全与多租户

MultiKueue 的安全边界比单集群更敏感：manager 持有所有 worker 的凭据。

### 34.9.1 凭据管理

| 方式 | 推荐度 | 说明 |
|---|---|---|
| `ClusterProfile` + access provider | 生产推荐 | 短期凭据、集中管理 |
| Secret + kubeconfig | 生产可用 | 简单直接，但要轮换 |
| Path | 仅开发 | 文件系统路径，风险高 |

Secret 必须放在 Kueue controller manager 所在 namespace，key 固定为 `kubeconfig`。不要让租户直接读写这些 Secret。

### 34.9.2 最小权限

worker 侧的 ServiceAccount 只需要：

- 创建/更新/删除 Workload；
- 创建/更新/删除 Job（以及作业框架对象）；
- 读取自己的 CQ/LQ/Flavor；
- 写 Workload status；
- 读取 Pod 状态（如果集成需要）。

不需要：

- 集群级 RBAC；
- 读取其他 namespace 的 Secret；
- 修改 CRD；
- 访问 worker 的 kube-system。

### 34.9.3 网络与审计

- manager 到 worker 的 API server 走内网/TLS；
- 限制 egress，只允许访问受控的 worker 端点；
- 开启 audit log，记录跨集群的 Workload/Job 创建；
- 对 worker 的 kubeconfig 使用独立身份，便于溯源；
- 定期轮换凭据，监控过期时间。

### 34.9.4 租户隔离

租户只应看到 manager 上的 Workload/Job，不应直接访问 worker。worker 上的副本由 manager 管理，租户不需要（也不应该）有权限。

如果租户需要查看跨集群状态，用 manager 的 Visibility API 或只读聚合视图，而不是给 worker 的 kubeconfig。

## 34.10 可观测性

### 34.10.1 关键状态

```bash
# manager 上的 Workload
kubectl get workloads -A
kubectl get workload <name> -n <ns> -o yaml

# 重点字段
status.nominatedClusterNames
status.clusterName
status.conditions[]
```

### 34.10.2 关键指标

| 指标 | 用途 |
|---|---|
| `multikueue_workloads_dispatched_total` | 分发次数 |
| manager Workload 的 age / pending 时间 | 提名是否卡住 |
| worker 连接状态/重连次数 | 凭据/网络问题 |
| worker 侧 Pending Workload 数 | 提名是否过多 |
| 远端 Workload 的 age | 清理是否及时 |
| 抢占 gate 的打开/关闭次数 | 跨集群抢占是否异常 |

### 34.10.3 日志与事件

要看的日志：

- dispatcher 的提名决策（为什么选这些 worker）；
- 连接建立/失败/退避；
- 选中与清理；
- 状态同步错误；
- GC 失败。

要看的 K8s 事件：

- Workload 的 `QuotaReserved` / `Admitted` / `Evicted` / `Finished`；
- Job 的创建/删除；
- MultiKueue AdmissionCheck 的 Active 状态。

### 34.10.4 一个排障顺序

```text
1. manager Workload 是否 QuotaReserved？
   ├─ 否：本地配额/队列/AdmissionCheck 问题，先看第 10 章
   └─ 是：继续
2. status.nominatedClusterNames 是否非空？
   ├─ 否：dispatcher/配置/凭据问题
   └─ 是：继续
3. 提名的 worker 上是否有 Workload 副本？
   ├─ 否：创建失败/权限/网络
   └─ 是：继续
4. 副本是否 Admitted？
   ├─ 否：worker 本地配额/抢占/检查
   └─ 是：继续
5. status.clusterName 是否已设置？
   ├─ 否：manager 状态同步/选中竞态
   └─ 是：继续
6. 胜者上是否有 Job？Job/Pod 是否正常？
   └─ 否：Job 集成/权限/物理调度，转第 28 章
```

## 34.11 生产部署

### 34.11.1 启动顺序

1. 在每个 worker 安装 Kueue，配置 CQ/LQ/Flavor；
2. 生成 worker kubeconfig，创建 Secret；
3. 在 manager 创建 `MultiKueueCluster`，确认连接 Active；
4. 创建 `MultiKueueConfig`；
5. 创建 `AdmissionCheck`；
6. 在 manager 的 CQ 上挂 `admissionChecksStrategy`；
7. 提交一个测试 Job，观察提名/选中/清理；
8. 再接入真实租户。

### 34.11.2 HA

- manager 多副本 + leader election；
- dispatcher 是单例逻辑，不要让多个 manager 同时提名；
- worker 侧不需要为 MultiKueue 做额外 HA，但 worker 自己的 Kueue 要高可用；
- etcd 备份覆盖 manager 和 worker 的关键对象。

### 34.11.3 升级

推荐顺序（具体以 release notes 为准）：

1. staging 做 manager/worker 版本 round-trip；
2. 升级 worker（保持 CRD 兼容）；
3. 升级 manager；
4. 观察提名成功率、选中延迟、清理失败率；
5. 回滚时先回滚 manager，再回滚 worker。

如果目标版本有 CRD conversion/storage 变更，先读 release notes；不要假设"worker 先升"永远安全。

### 34.11.4 容量与成本

- manager 配额按 worker 总容量打折；
- 为每个 worker 设置维护窗口和 drain 流程；
- 监控跨集群网络成本；
- 对跨 region 的作业做数据 locality 评估；
- 定期审查"提名了但从未被选中"的 worker，调整顺序或权重。

## 34.12 MiniKueue 的 MultiKueue-lite

`mini-kueue/internal/multikueue` 用多个独立 `Engine` 模拟 worker。它不是网络模拟器，而是一个**确定性状态机**，专门用来演示提名、选中、清理和故障切换。

### 34.12.1 代码结构

```text
Manager
  ├── Workload（唯一源对象）
  ├── Workers []*Worker{Name, Engine, Workload, Down}
  ├── Config{Dispatcher, StepSize, WorkerLostTimeout}
  ├── Nominated / Selected
  ├── round（Incremental 的实际提名轮次，不随等待 tick 增长）
  ├── downTicks map[*Worker]int
  └── Tick:
        selected 检查 → 失联超时 → 清理陈旧副本
        → nominate（跳过 Down）→ Submit 副本
        → Tick 健康 worker → 第一个 Admitted 胜出
        → cleanupStale(selected)
```

### 34.12.2 已实现的行为

- `AllAtOnce`：一次提名所有健康 worker；
- `Incremental`：按 `StepSize` 分批，跳过 Down worker；等待失联超时的 tick 不消耗扩张轮次；
- 第一个 Admitted 的 worker 成为 `Selected`；
- 其他健康 worker 的副本被删除；
- `Nominated` 对应 `status.nominatedClusterNames`；worker 变为 Down 后会从提名列表移除，避免状态里继续展示不可达 worker；
- 重复 tick 不重复创建副本；
- 选中副本被删除后重新分发；
- **新增：Down worker 不参与提名，不接收副本，不被 tick；**
- **新增：选中的 worker 失联后，按 `WorkerLostTimeout` 等待，再重新分发；**
- **新增：失联 worker 恢复后，清理它持有的陈旧副本。**

### 34.12.3 运行

```bash
go run ./cmd/minikueue -scenario multikueue -ticks 3
go run ./cmd/minikueue -scenario multikueue-failover -ticks 5
```

`multikueue-failover` 的预期时间线：

```text
tick 1  worker-1 Down；worker-2 被提名并 Admitted，Selected=worker-2
tick 2  worker-2 失联；WorkerUnavailable（1/1 ticks），保持选中
tick 3  超过 timeout；WorkerLost，重新分发到 worker-1，Selected=worker-1
tick 4  worker-2 恢复；清理其陈旧副本
tick 5  保持 worker-1 选中
```

关键断言（对应单元测试）：

- `TestDownWorkerIsNotNominated`：Down worker 不进入提名列表；
- `TestFailoverAfterWorkerLostTimeout`：超时前保持选中，超时后切换；
- `TestNoAvailableWorker`：所有 worker 都 Down 时产生 `NoAvailableWorker` 事件。

### 34.12.4 它没有模拟什么

- 真实 kubeconfig/Secret/网络；
- 连接退避与 watch 中断；
- 跨集群状态同步延迟；
- 抢占 gate 的分布式协调；
- 远端对象 GC 的真实时序；
- 重复执行的语义（MiniKueue 的 Down 是"看不见"，不是"停止运行"）；
- 选中后 `Nominated` 的清理时机：MiniKueue 保留选中轮次的提名列表以便观察，真实 Kueue 在 `status.clusterName` 设置后会重置 `nominatedClusterNames`；
- 选中 worker 在 `workerLostTimeout` 宽限期内仍是 `Selected`：这是有意对齐真实语义，不是状态错误；
- 失联 worker 的副本保留到恢复后清理：真实 manager 也无法删除不可达对象；
- 每个 `Worker` 必须持有独立的 `Engine`；两个 Worker 共享同一个 Engine 会让"删除败者副本"误删胜者副本，导致反复分发，这是配置错误而不是受支持的模式。

这些差距提醒我们：MiniKueue 能验证**状态机主干**，不能验证**分布式故障语义**。生产环境必须用真实集群做故障演练。

## 34.13 多集群设计模式

### 34.13.1 Active-Passive

一个主集群承担大部分作业，另一个作为灾备/溢出。

- manager 提名顺序：主集群优先，备集群兜底；
- 备集群容量可以较小；
- 定期演练主集群不可用时的切换。

### 34.13.2 Active-Active

多个集群同时承担作业，按成本/region/容量选择。

- 需要统一租户视图；
- 每个 worker 的 CQ 结构保持一致；
- 用 External dispatcher 做成本/延迟优化；
- 注意跨集群的重复执行风险。

### 34.13.3 Cloud Burst

on-prem 为主，云上按需扩容。

- `Incremental` + `stepSize: 1` 很适合；
- 云上集群的 kubeconfig 和配额由平台自动管理；
- 监控云上启动延迟和成本；
- 作业要能容忍跨云的数据传输。

### 34.13.4 数据 Locality

训练数据在某个 region，优先在该 region 运行。

- worker 画像里记录数据位置；
- External dispatcher 按 locality 打分；
- 不要把"数据在 A，作业去 B"当成默认行为；
- 跨 region 读取的带宽/成本要计入容量模型。

### 34.13.5 成本/碳排感知

按每 GPU 小时成本、spot 中断概率、碳排强度选择 worker。

- 成本信号应该是"预期成本"，不是瞬时价格；
- spot 中断要纳入抢占/重试模型；
- 碳排信号需要数据源和更新频率；
- 不要让成本优化牺牲 SLO。

### 34.13.6 专用推理集群

推理服务对延迟敏感，通常不与训练共享 worker。

- 独立 worker + 独立 CQ；
- manager 只负责跨集群的容量调度；
- 推理的 TAS spreading、DRA、LWS 支持要单独验证；
- 不要把推理放进可被训练抢占的队列。

## 34.14 检查清单与常见错误

### 34.14.1 上线检查清单

- [ ] 每个 worker 的 Kueue 版本与 manager 兼容；
- [ ] worker kubeconfig 使用独立身份，权限最小化；
- [ ] Secret 位于 `kueue-system`，key 为 `kubeconfig`；
- [ ] `MultiKueueCluster` 的 `Active` 条件为 True；
- [ ] `MultiKueueConfig` 的 cluster 顺序符合成本/延迟策略；
- [ ] `AdmissionCheck` 的 `controllerName` 正确；
- [ ] CQ 使用 `admissionChecksStrategy`；
- [ ] manager 配额约等于 worker 总容量的安全折扣；
- [ ] `workerLostTimeout` 与作业的重试/幂等能力匹配；
- [ ] 监控覆盖提名、选中、清理、连接、抢占 gate；
- [ ] 有 manager/worker 故障演练；
- [ ] 有重复执行的检测和告警；
- [ ] 有升级/回滚 Runbook；
- [ ] 租户只能看到 manager，不直接访问 worker。

### 34.14.2 常见错误

| 错误 | 后果 | 修正 |
|---|---|---|
| manager 配额按"所有 worker 总和"满配 | 大量提名失败/排队 | 打安全折扣 |
| 所有 worker 用同一个 kubeconfig | 无法溯源、轮换困难 | 每 worker 独立身份 |
| `workerLostTimeout` 太短 | 网络抖动导致重复执行 | 按作业容忍度调大 |
| 选中后不清理败者 | 资源泄漏、重复运行 | 检查 GC/权限 |
| 忽略 `clusterName` 不可变 | 试图原地迁移，失败 | 创建新 Workload |
| 把跨集群公平当成 Kueue 原生能力 | 份额与预期不符 | 用 External dispatcher 或接受局部公平 |
| worker CRD 版本不一致 | 状态字段丢失/解析失败 | 统一版本 |
| 不做重复执行检测 | 静默双跑，结果污染 | 幂等输出 + 告警 |

## 34.15 本章小结

- MultiKueue 把队列扩展到多集群，但**一个 Workload 最终只在一个 worker 上运行**。
- 核心对象是 `MultiKueueCluster`、`MultiKueueConfig`、`AdmissionCheck` 和 manager 上的 `Workload`。
- 一次分发经过：本地预留 → 提名 → 副本 → 竞争 → 选中 → 落地 → 同步 → 清理。
- Dispatcher 有三种：`AllAtOnce`、`Incremental`、`External`；选择取决于延迟、成本和策略复杂度。
- `status.clusterName` 不可变，是单一选中的 API 保证。
- worker 失联不等于作业停止；failover 可能带来重复执行，作业必须幂等/可重启。
- manager 配额是总闸门，应按 worker 总容量打折；跨集群公平不是原生的。
- `MultiKueueOrchestratedPreemption` 用 preemption gate 协调跨集群抢占。
- 凭据、网络、审计和升级顺序是 MultiKueue 生产化的关键。
- MiniKueue 用 `Down` 状态和 `WorkerLostTimeout` 复现了失联/切换/清理的主干，但不模拟真实网络和重复执行语义。

---

[← 上一章：实现补全](33-implementation-completion.md) | [返回目录](../README.md) | [下一章：Kueue 扩展开发 →](35-kueue-extension-development.md)
