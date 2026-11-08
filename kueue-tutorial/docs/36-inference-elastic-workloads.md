# 第 36 章 · 推理、弹性与混合负载

前 35 章的主线大多是"批处理作业"：提交、排队、准入、运行、完成。但 2026 年的 AI 平台不只有训练。推理服务、Notebook、RayService、LeaderWorkerSet、弹性训练作业、在线/离线混部，都在争夺同一批 GPU。它们对 Kueue 的要求和 batch Job 很不一样：

- 推理服务长期运行，没有"完成"这个自然终点；
- 副本要按流量扩缩，不能一次性 gang 起来；
- 延迟敏感，不能随便被抢占；
- 需要跨故障域打散，而不是尽量挤在一个 rack；
- 弹性训练希望在部分容量上先跑起来，之后再扩。

本章讲清楚 Kueue 在这些场景里的真实能力边界，以及怎么设计队列和配额。**不要假设"Kueue 能管 Job，就一定能管推理"**；很多语义需要显式配置，甚至需要承认 Kueue 不负责。

> 版本提示：本章以 Kueue v0.20.x 稳定线（截至 2026-11 为 v0.20.5）为背景；v0.21 尚未 GA。Partial Admission、Elastic Workloads、TAS Spreading 都是 feature gate 控制的特性，默认值和成熟度以目标 release 为准。

## 36.1 为什么推理与弹性是另一类问题

把批处理作业和推理服务放在一张表里对比：

| 维度 | 批处理 Job | 推理服务 |
|---|---|---|
| 生命周期 | 有明确完成点 | 长期运行，滚动更新 |
| 副本语义 | 全部副本一起启动 | 按流量逐步扩缩 |
| 资源释放 | 完成后释放 | 缩容/删除时释放 |
| 延迟要求 | 排队可接受 | P99 延迟敏感 |
| 抢占容忍 | 通常可 checkpoint | 通常不可接受 |
| 拓扑要求 | 尽量集中（NVLink/IB） | 尽量打散（故障域） |
| 扩缩单位 | 整个 Job | 单个 Pod/副本 |
| 失败处理 | 重试/重排队 | 保持可用副本数 |

这些差异导致：

1. **不能把推理服务当成一个 gang Job。** 一个 100 副本的 Deployment，不需要 100 个 Pod 同时准入；它需要"能起多少起多少，再逐步扩"。
2. **不能把推理放进可被训练抢占的队列。** 训练作业一抢占，推理 P99 直接爆炸。
3. **不能只用 nominalQuota 表达推理容量。** 推理需要预留 headroom、HPA 弹性、故障域冗余。
4. **"完成"语义要重新定义。** Deployment/StatefulSet 没有 Finished；Kueue 的账本需要靠缩容/reclaimable pods 释放。

## 36.2 长运行负载的集成边界

Kueue 对不同对象的管理方式不同。这是本章最重要的一张表：

| 对象 | Kueue 如何管理 | 暂停/放行机制 | 关键限制 |
|---|---|---|---|
| `batch/v1 Job` | 一个 Job = 一个 Workload | `spec.suspend` | 完成语义清晰 |
| `Pod` | 一个 Pod = 一个 Workload | scheduling gate | 无 gang，逐 Pod 准入 |
| `Deployment` | **逐 Pod 通过 Pod 集成管理**，不是单个 Workload | pod 模板注入队列标签，Pod 用 scheduling gate | 副本之间不原子；滚动更新/扩缩容要单独考虑 |
| `StatefulSet` | Plain Pod Group/整体管理 | 不支持扩缩；不要用 `replicas=0` 暂停 | 需要理解 PodGroup 与成员记账 |
| `LeaderWorkerSet` | leader/worker 两个 PodSet | 由集成负责 | group 语义、拓扑要求 |
| `PodGroup`（同组 Pod） | 一组 Pod = 一个 Workload | 共享 scheduling gate/组标签 | 需要角色划分和组标签 |
| JobSet / Kubeflow / Ray | 多 PodSet 作业 | 各框架的 suspend 字段 | 见第 25 章 |

### 36.2.1 Deployment：不是"一个 Deployment 一个 Workload"

这一点经常被误解。Kueue 的 Deployment 集成主要是 **webhook 注入 + Pod 集成**：

- Deployment 的 Pod 模板带上 `kueue.x-k8s.io/queue-name`；
- 每个 Pod 被当作独立的 Plain Pod Workload 管理；
- 扩容出来的 Pod 在准入前被 scheduling gate 挡住；
- 缩容删除 Pod 后，对应 Workload 释放配额；
- Deployment 本身不会被"整体暂停"，也不会因为某个 Pod 没准入而全部不创建；
- 这种逐 Pod 行为依赖 Pod 集成（不同版本里 deployment 集成可能默认包含或需要显式启用，按目标版本确认）。

这意味着：

- 一个 100 副本的 Deployment 可能只有 60 个 Pod 被准入，另外 40 个 Pending；
- 滚动更新时，新旧 Pod 会同时计入配额；
- HPA 扩容可能创建一批"暂时进不来"的 Pod；
- "Deployment 的最小副本数"不等于"Kueue 保证的副本数"。

如果你需要"整个 Deployment 原子准入"，Deployment 不是合适的抽象；考虑 StatefulSet/PodGroup 或自定义 Job 集成。

### 36.2.2 StatefulSet：Plain Pod Group，不支持扩缩

Kueue v0.20 的 StatefulSet 集成用 Plain Pod Group 管理成员：

- StatefulSet 的副本作为一个组，整体做配额准入；
- webhook 负责注入队列名/父对象注解；
- 成员 Pod 的放行由 Pod Group 集成协调；
- **官方明确不支持 StatefulSet 的扩缩操作**；不要手动把 `spec.replicas` 置 0 再恢复，也不要依赖 Kueue 做有状态副本的弹性伸缩。

这比 Deployment 更接近"整体管理"，但边界要讲清楚：

- 需要暂停/弹性时，用受支持的 Pod Group 路径或自定义集成，不要改 `replicas`；
- 有状态服务的启动顺序/数据迁移仍由 StatefulSet 控制器负责，Kueue 不介入；
- 不适合"始终保持一部分副本在线"的推理服务；
- 需要逐 Pod 弹性的推理，应该看 Deployment/PodGroup 或框架自带的弹性能力，而不是 StatefulSet 扩缩。

### 36.2.3 LeaderWorkerSet：leader/worker 两个 PodSet

LWS 把一个 group 分成 leader 和 worker：

- leader 通常承担协调/服务入口；
- worker 承担计算；
- Kueue 把它们映射成两个 PodSet，资源分别汇总；
- group 的拓扑/ gang 语义由 LWS 集成处理。

推理场景里，LWS 常用于"一个 leader + 多个 worker 的模型并行服务"。要确认：

- leader 和 worker 是否必须同域；
- 缩容时先删 worker 还是 leader；
- 滚动更新时 group 的原子性；
- 故障时 leader 重新选举是否影响 Workload 账本。

### 36.2.4 PodGroup：把一组 Pod 变成一个 Workload

PodGroup 集成通过 `kueue.x-k8s.io/pod-group-name` 把同组 Pod 聚合成一个 Workload：

- 组内 Pod 共享队列/优先级；
- 按角色哈希成 PodSet；
- 准入前所有 Pod 都被 gate；
- 准入后一起放行；
- 缩容/删除时按组释放。

它适合"没有专用 CRD、但需要 gang 语义"的自研服务。要注意：

- 组标签必须稳定，不能滚动变化；
- 角色划分要明确，否则资源汇总会错；
- 组内 Pod 数量变化要能触发 Workload 更新；
- 不要让不同租户的 Pod 误入同一组。

## 36.3 部分准入：Partial Admission

### 36.3.1 问题

一个 32 副本的训练/推理作业，当前只有 20 副本的配额。传统 Kueue 的选择是：

- 全部 32 个都等（all-or-nothing）；或者
- 拒绝，直到有 32 个配额。

但很多场景希望"先跑 20 个，剩下的以后扩"。这就是 Partial Admission。

### 36.3.2 机制

Kueue 用 `PodSet.minCount` 表达"最低可接受副本数"：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Workload
metadata:
  name: elastic-workload
spec:
  podSets:
  - name: main
    count: 32
    minCount: 20
    template:
      # ...
```

需要：

- `PartialAdmission` feature gate；
- Job 侧通过 `kueue.x-k8s.io/job-min-parallelism` 注解表达最低并行度；
- 目前只支持**一个 PodSet** 使用 `minCount`；
- `minCount` 必须 ≤ `count`；
- v0.20 起 `minCount` 可以为 0（用于"先占位、后扩"的场景）。

Flavor 分配器会在 `[minCount, count]` 区间内做二分查找，找到一个能放下的副本数。结果写在：

```yaml
status:
  admission:
    podSetAssignments:
    - name: main
      count: 20
```

### 36.3.3 语义

- 准入的副本数是"当前能放下的最大值"，不保证之后一定扩到 `count`；
- 作业框架必须支持"用较少副本启动"；
- 后续扩容需要作业框架/集成更新 Workload；
- 缩容/释放要走 reclaimable pods 或受支持的更新路径；
- 部分准入与 Elastic Workload Slicing **不能同时启用**（webhook 会拒绝）。

### 36.3.4 适合与不适合

适合：

- 弹性训练（能容忍较少 worker）；
- 推理服务（先起一部分副本，后续扩容）；
- 批处理（先跑一部分，提高利用率）。

不适合：

- 严格 gang（MPI、某些分布式训练）；
- 需要固定 world size 的算法；
- 对副本数变化没有恢复能力的作业。

## 36.4 弹性工作负载：Workload Slices

### 36.4.1 问题

Partial Admission 解决"启动时少放一些副本"，但作业运行中还想：

- 扩容：从 20 副本扩到 32；
- 缩容：从 32 缩到 20，释放配额；
- 滚动替换：用新 slice 替换旧 slice，而不中断服务。

### 36.4.2 机制

Elastic Workloads（`ElasticJobsViaWorkloadSlices`，v0.18 beta）用 **Workload Slice** 表达变化：

- 作业通过 `kueue.x-k8s.io/elastic-job: "true"` 选择加入；
- 扩容时创建一个新的 Workload Slice，并用 `kueue.x-k8s.io/workload-slice-replacement-for` 指向旧 slice；
- 缩容时原地更新现有 Workload；
- 旧的 slice 在新 slice 准入后被替换/清理；
- `kueue.x-k8s.io/elastic-job-scale-up-strategy` 控制扩容策略：
  - `atomic`（默认）：新 slice 整体准入；
  - `partial`：允许部分副本先扩容，需要 `ElasticJobsViaWorkloadSlicesWithPartialReplicaScaleUp` gate。

### 36.4.3 与 Partial Admission 的关系

两者目标相似，但机制不同：

| 能力 | Partial Admission | Elastic Workload Slices |
|---|---|---|
| 启动时少放副本 | 是 | 是（通过 slice） |
| 运行时扩容 | 作业框架自己更新 | Kueue 创建新 slice |
| 运行时缩容 | reclaimable pods/更新 | 原地更新 |
| 多 PodSet | 只支持一个 | 视版本/集成而定 |
| 组合 | **不能同时启用** | **不能同时启用** |

选择建议：

- 只需要"启动时少放一些"：Partial Admission；
- 需要"运行中扩缩容 + 替换"：Elastic Workload Slices；
- 不确定：先用 Partial Admission，简单可控。

### 36.4.4 账本与回收

弹性场景最容易出错的是账本：

- 扩容后 Workload 的 `count` 变了，但旧 slice 可能还在运行；
- 缩容后 Pod 删了，但 Workload 没有更新；
- 替换期间新旧 slice 同时占配额；
- 作业框架报告的 `reclaimablePods` 与实际删除不一致。

要保证：

- 每次扩缩容都有明确的 Workload 更新；
- 旧 slice 的配额在新 slice 准入后释放；
- 缩容先更新账本再删 Pod，或使用原子回收；
- 监控"账本副本数 vs 实际 Running Pod 数"的偏差。

## 36.5 TAS 与推理打散

### 36.5.1 训练要集中，推理要打散

训练作业通常希望：

- 所有 GPU 在同一个 rack/leaf switch；
- NVLink/IB 域内；
- 减少跨域通信。

推理服务通常希望：

- 副本分散到不同 zone/rack；
- 单个故障域不要承载过多副本；
- 保证 P99 和可用性。

Kueue 的 TAS 同时支持这两种方向：

- `kueue.x-k8s.io/podset-required-topology`：必须落在某个拓扑层级内（集中）；
- `kueue.x-k8s.io/podset-preferred-topology`：优先某个层级；
- `kueue.x-k8s.io/podset-unconstrained-topology`：不约束；
- `kueue.x-k8s.io/podset-group-name`：多个 PodSet 共享域约束；
- `kueue.x-k8s.io/podset-topology-spreading`：跨 Workload 打散（alpha，`TASTopologySpreading`）。

### 36.5.2 打散注解

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: main-inference-service
spec:
  template:
    metadata:
      annotations:
        kueue.x-k8s.io/podset-required-topology: topology.kubernetes.io/zone
        kueue.x-k8s.io/podset-topology-spreading: |
          {
            "workloadLabelSelectors": [
              {"key": "app", "operator": "In", "values": ["main-inference-service"]}
            ],
            "rules": [
              {
                "topologyKey": "topology.kubernetes.io/zone",
                "maxShareAllowingPlacement": "0.45",
                "enforcementMode": "Preferred"
              }
            ]
          }
```

语义：

- `podset-required-topology` 必须先有，spreading 不能单独使用；
- `workloadLabelSelectors` 决定"和哪些 Workload 互相打散"；省略时默认同一 job UID 的 Workload；
- `maxShareAllowingPlacement` 是单个域的最大份额；
- `enforcementMode: Required` 会排除超过份额的域；`Preferred` 只是降权，仍可能回退；
- `0.45` 大约意味着"先开 3 个域再复用"（`ceil(1/0.45) = 3`）。

### 36.5.3 打散不是负载均衡

TAS spreading 只影响**放置**，不做运行时流量均衡：

- 它不会根据 QPS 调整副本；
- 它不会迁移已经运行的 Pod；
- 它不会保证每个域的副本数完全相等；
- 它不能替代 Service/Ingress 的负载均衡。

不要把"打散"当成"高可用"的全部。高可用还需要：

- 多副本；
- PodDisruptionBudget；
- 跨 zone 的 Service；
- 故障域容量冗余；
- 滚动更新策略。

## 36.6 DRA、设备与推理

推理对设备的要求可能比训练更细：

- 不同模型需要不同显存；
- 可能共享 GPU（MIG、time-slicing）；
- 需要特定网卡/互联；
- 设备健康状态影响可用性。

Kueue 的 DRA 集成负责：

- 把 DeviceClass 映射成配额资源；
- 在准入时检查设备可行性；
- 与 TAS 结合做拓扑/设备放置；
- 处理设备污点/不可用。

但要注意：

- **配额计费 ≠ 设备分配。** Kueue 预留的是逻辑配额，真正的设备分配由 DRA/kube-scheduler 完成；
- **共享设备的计费模型要明确。** 是按卡、按显存、按时间片，还是按 MIG 实例；
- **设备健康状态会变化。** 准入时可用不代表运行时可用，需要热替换/重调度；
- **不要让 DRA 映射绕过配额。** 映射到空资源或未覆盖资源会导致"设备用了但没计费"。

推理场景的建议：

- 为不同设备型号建不同 Flavor；
- 用资源变换把显存/算力折算成统一配额（如果版本支持）；
- 对共享设备明确超卖比例；
- 监控"配额已预留但设备未分配"的 gap。

## 36.7 优先级、抢占与 SLO

### 36.7.1 推理通常不应该被抢占

Kueue 的抢占是为批处理设计的：

- 高优先级作业可以驱逐低优先级作业；
- 被驱逐的作业释放配额并重排队；
- 训练作业可以 checkpoint/restart。

推理服务被驱逐的代价：

- 请求失败/延迟抖动；
- 模型加载时间长；
- 副本重建可能触发冷启动；
- 可能违反 SLO。

因此：

- 推理队列使用独立的 CQ/Cohort；
- 推理的优先级高于训练；
- 不要让训练的 `reclaimWithinCohort`/`borrowWithinCohort` 指向推理；
- 如果必须共享，给推理预留 headroom，并限制训练的借用上限。

### 36.7.2 部分抢占仍是方向

Kueue 社区在讨论"部分抢占 serving workloads"（例如只驱逐一部分副本，而不是整个 Workload），但这不是当前稳定能力。不要在生产设计里假设它存在。

### 36.7.3 SLO 与容量

推理的容量模型和训练不同：

- 训练看吞吐（job/hour、GPU-hour）；
- 推理看延迟（P50/P99）、错误率、并发；
- 推理需要按峰值 + 冗余规划，而不是平均利用率；
- 扩缩容有冷启动时间，容量要覆盖启动窗口。

一个简单的容量公式：

```text
所需副本数 =
    ceil(峰值 QPS × 平均处理时间 / 目标并发)
  + 故障域冗余
  + 滚动更新冗余
```

再用 Kueue 的队列保证"至少这么多副本能拿到配额"，而不是"所有副本都 gang 起来"。

## 36.8 自动扩缩与 Kueue 的边界

### 36.8.1 HPA/KEDA 与 Kueue 的分工

| 组件 | 负责 |
|---|---|
| HPA/KEDA | 根据指标调整副本数 |
| Kueue | 决定新 Pod 能否拿到配额并被放行 |
| kube-scheduler | 决定 Pod 落在哪个节点 |
| Cluster Autoscaler | 决定是否加节点 |

关键点：

- HPA 扩容会创建 Pod；这些 Pod 可能因为 Kueue 配额不足而 Pending；
- Kueue 不会阻止 HPA 创建 Pod，只会 gate 它们；
- 缩容删除 Pod 后，Kueue 释放配额；
- HPA 的 `minReplicas` 不等于 Kueue 的保证副本数；
- 如果 HPA 和 Kueue 的容量策略冲突，会出现"扩了又缩、缩了又扩"的抖动。

### 36.8.2 设计建议

- 给推理队列预留 `minReplicas` 对应的配额；
- 限制 HPA 的 `maxReplicas`，不要超过队列能承载的上限；
- 用 `scaleDownStabilizationWindow` 避免快速缩容导致配额抖动；
- 监控"Pending 的推理 Pod 数"和"扩容被 gate 的时长"；
- 不要让 HPA 和 Kueue 同时做"容量决策"。

### 36.8.3 Deployment 的逐 Pod 准入

由于 Deployment 是逐 Pod 管理：

- 扩容 10 个副本，可能只有 6 个先准入；
- 剩余 4 个会一直 Pending，直到配额释放；
- 滚动更新时，新旧 ReplicaSet 的 Pod 同时占配额；
- 如果 `maxSurge` 太大，可能瞬间需要双倍配额。

建议：

- 推理 Deployment 的 `maxSurge` 设为 0 或较小；
- 为滚动更新预留额外配额；
- 监控 `kube_deployment_status_replicas_unavailable` 与 Kueue Pending 的关系。

## 36.9 多集群推理

MultiKueue 目前主要面向作业分发，长运行服务的跨集群能力仍在演进（社区路线图包含 long-running services）。当前可以做的：

- **Active-Passive**：主集群跑推理，备集群待命；
- **按 region 分流**：不同区域的用户走不同集群；
- **故障转移**：主集群故障时，在备集群重建服务（不是迁移运行中的 Pod）；
- **容量溢出**：主集群满时，把新副本放到备集群（需要服务发现/流量切换）。

不要假设 MultiKueue 会自动迁移一个运行中的推理服务。它不做跨集群 Pod 迁移，也不做流量切换。

## 36.10 队列与配额设计模式

### 36.10.1 专用推理池

```text
Cohort: inference
├── CQ: online-critical   nominal=40 GPU   priority=1000
├── CQ: online-batch      nominal=20 GPU   priority=500
└── CQ: batch-burst       nominal=10 GPU   priority=100   borrowingLimit=10
```

特点：

- 关键推理独占 nominal；
- burst 队列可以借用空闲容量，但不能抢占关键推理；
- 训练/离线作业不进入这个 Cohort。

### 36.10.2 共享池 + headroom

```text
Cohort: gpu-shared
├── CQ: inference   nominal=60 GPU  borrowingLimit=0
└── CQ: training    nominal=40 GPU  borrowingLimit=40
```

规则：

- 推理的 nominal 是硬预留；
- 训练可以借用推理空闲的 40；
- 推理需要扩容时，通过 reclaim 收回（前提是训练可被抢占）；
- 为推理保留 `minReplicas` 的 headroom。

### 36.10.3 弹性突发

```text
CQ: inference-burst
  nominal=20 GPU
  borrowingLimit=40 GPU
  cohort=cloud-burst
```

特点：

- 平时用 on-prem；
- 峰值借用云上容量；
- 云上容量用 DQO/CapacityProvider 动态反馈；
- 作业/服务要能容忍跨云延迟。

### 36.10.4 反模式

| 反模式 | 问题 |
|---|---|
| 推理和训练共用一个可抢占 CQ | 训练抢占导致推理 SLO 崩 |
| 用 all-or-nothing 管 100 副本推理 | 永远起不来或永远等 100 个 |
| 不做 headroom，按平均利用率规划 | 峰值时大量 Pending |
| HPA maxReplicas 远大于队列容量 | 扩容后大量 Pod 卡住 |
| 把 Deployment 当原子 Workload | 滚动更新/扩缩容账本混乱 |
| 用 TAS 打散替代高可用 | 放置分散不等于故障隔离 |
| 把 DRA 设备映射当配额 | 设备用了但没计费 |

## 36.11 MiniKueue 的对照

MiniKueue 当前覆盖了：

- `dynamic-reclaim`：作业报告 `reclaimablePods`，释放配额；
- `tas-slice`：TAS slice 与跨 Workload 打散；
- `concurrent-admission`：Flavor 迁移；
- `dra`：DeviceClass 映射计费；
- `multikueue-failover`：多集群失联切换。

它**没有**实现：

- Partial Admission 的 `minCount` 二分；
- Elastic Workload Slices；
- Deployment/StatefulSet/LWS 的真实集成；
- HPA/KEDA 交互；
- 推理的 SLO/延迟模型。

### 36.11.1 实验：给 MiniKueue 加 Partial Admission-lite

目标：让一个 8 副本的 Workload 在只有 5 副本配额时，以 5 副本准入。

步骤：

1. 在 `api.PodSet` 增加 `MinCount int32`；
2. 在 `Workload.TotalUsage`/调度器中支持"请求 8、最低 5"；
3. 在 `scheduler.canFit` 里做二分查找，找到最大可放置副本数；
4. 在 `status.Admission` 记录实际准入副本数；
5. 增加场景 `partial-admission`：CQ 只有 5 CPU，Workload 请求 8、minCount=5；
6. 增加测试：5 副本准入、后续扩容到 8、缩容释放。

验收：

- 准入副本数等于当前可放置最大值；
- 账本按实际副本数计费；
- 扩容/缩容后账本一致；
- 不改变 existing all-or-nothing 场景。

这个实验能帮助你理解真实 Kueue 的 Partial Admission 为什么需要二分、为什么只支持一个 PodSet，以及为什么和 Elastic Slices 互斥。

## 36.12 检查清单

### 集成

- [ ] 明确对象是 Deployment / StatefulSet / LWS / PodGroup / 自定义 Job；
- [ ] 确认暂停/放行机制（gate、PodGroup、suspend；StatefulSet 不要用 replicas=0）；
- [ ] 确认"完成"语义和账本释放路径；
- [ ] 确认滚动更新/扩缩容期间的配额；
- [ ] 确认多 PodSet/角色映射；
- [ ] 确认 TAS/DRA 组合。

### 配额

- [ ] 推理与训练分离；
- [ ] 推理有 headroom；
- [ ] HPA `maxReplicas` ≤ 队列容量；
- [ ] 滚动更新预留额外配额；
- [ ] 借用/回收不会伤到推理；
- [ ] 监控 Pending 推理 Pod 数与扩容 gate 时长。

### 可靠性

- [ ] 多副本 + PDB；
- [ ] 跨故障域打散；
- [ ] 故障转移演练；
- [ ] 模型加载/冷启动时间纳入容量；
- [ ] 不依赖"运行中迁移"；
- [ ] SLO/告警覆盖 P99、错误率、可用副本数。

### 弹性

- [ ] 选择 Partial Admission 或 Elastic Slices（不要同时）；
- [ ] 作业框架支持较少副本启动；
- [ ] 扩容/缩容账本一致；
- [ ] reclaimable pods 正确报告；
- [ ] 替换期间新旧 slice 配额可控。

## 36.13 本章小结

- 推理/弹性负载与批处理 Job 的语义不同：长运行、逐步扩缩、SLO 敏感、需要打散。
- Deployment 是**逐 Pod**通过 Pod 集成管理，不是单个 Workload；StatefulSet 用 Plain Pod Group 且不支持扩缩（不要用 `replicas=0`）；LWS 用 leader/worker PodSet；PodGroup 用组标签聚合。
- Partial Admission 用 `minCount` + 二分查找，先跑一部分副本；只支持一个 PodSet，且与 Elastic Slices 互斥。
- Elastic Workload Slices 用新 slice 扩容、原地更新缩容；扩容策略有 atomic/partial。
- TAS 对训练是"集中"，对推理是"打散"；spreading 注解需要 required/preferred topology 配合。
- DRA 负责设备可行性，Kueue 负责配额计费；两者不能混为一谈。
- 推理通常不应被抢占；共享池要留 headroom，限制训练的借用/回收。
- HPA/KEDA 负责副本数，Kueue 负责准入；两者要协调，避免扩容后大量 Pending。
- MultiKueue 目前不迁移运行中的推理服务；跨集群推理要靠 Active-Passive/流量切换。
- MiniKueue 覆盖了 dynamic reclaim/TAS slice/DRA/MultiKueue failover，但没有 Partial Admission/Elastic Slices；可以用 36.11 的实验补齐直觉。

---

[← 上一章：Kueue 扩展开发](35-kueue-extension-development.md) | [返回目录](../README.md) | [下一章：调度决策的可解释性 →](37-decision-explainability.md)
