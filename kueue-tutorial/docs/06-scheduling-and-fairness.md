# 第 6 章 · 排队、调度与公平分享

## 6.1 一次调度循环里发生了什么

Kueue 的调度循环可以简化为：

```text
1. 从 LocalQueue/ClusterQueue 收集 pending Workload
2. 按队列策略和优先级排序
3. 对每个 Workload：
   a. 找到可用的 Flavor 组合
   b. 检查 CQ 配额和 Cohort 借用
   c. 如果放不下，评估是否抢占
   d. 放得下 → 预留配额
4. 更新 Cache 和 Workload 状态
```

不同的队列策略、优先级和公平算法，影响的是第 2 步和第 3c 步。

## 6.2 队列排序：优先级 + 创建时间

Kueue 的基本排序规则是：

1. **优先级高的先来**；
2. 优先级相同，**创建时间早的先来**；
3. 再相同，用稳定的名称/UID 作为 tie-breaker。

优先级来源见第 2 章：

- Pod PriorityClass（影响 `kube-scheduler`）；
- WorkloadPriorityClass（只影响 Kueue）。

### StrictFIFO

```yaml
spec:
  queueingStrategy: StrictFIFO
```

行为：

- 队头作业放不下时，后面的作业**不能插队**；
- 即使后面的作业很小、能放下，也要等。

优点：

- 严格公平，避免小作业不断插队导致大作业饿死；
- 行为可预测。

缺点：

- 队头大作业可能长期阻塞整个队列；
- 利用率可能很低。

### BestEffortFIFO

```yaml
spec:
  queueingStrategy: BestEffortFIFO   # 默认
```

行为：

- 仍然按优先级/创建时间排序；
- 队头放不下时，继续尝试后面的作业；
- 小作业可以"绕过"暂时放不下的大作业。

优点：

- 利用率高；
- 适合混合负载。

缺点：

- 大作业可能被持续绕过，产生饥饿；
- 需要配合公平分享或预留来保护大作业。

### 例子

CQ 配额 2 CPU：

```text
队头：big   需要 3 CPU
队尾：small 需要 1 CPU
```

- StrictFIFO：big 放不下 → 整个队列停住，small 也不准入。
- BestEffortFIFO：big 放不下 → 跳过 → small 准入。

MiniKueue 的 `strict-fifo` / `best-effort-fifo` 场景演示了这一点。

## 6.3 公平分享要解决什么

先看一个反例。

Cohort 有 A、B 两个 CQ，各有 nominal 10，Cohort 总量 20。

- A 先提交 20 个 1 CPU 的作业；
- B 后提交 20 个 1 CPU 的作业；
- 如果只按 FIFO，A 会占满 20，B 一直等待。

这看起来"先到先得"，但长期看：

- A 一直满载；
- B 一直饥饿；
- 集群利用率没有提高，只是分配不均。

公平分享的目标是：**在满足配额和优先级的前提下，让各租户的份额趋向加权公平。**

### 6.3.1 Kueue 里的两套"公平分享"

"Fair Sharing" 在 Kueue 里其实包含两套互补机制，经常被混为一谈：

| 机制 | 看什么 | 影响什么 | 配置入口 |
|---|---|---|---|
| **Preemption-based Fair Sharing** | 当前借用量的加权 DRS | 准入顺序、抢占谁 | Configuration `fairSharing.preemptionStrategies`；CQ/Cohort `fairSharing.weight` |
| **Admission Fair Sharing (AFS)** | LocalQueue 的历史累计用量 | 排队顺序（谁先被尝试） | CQ `admissionScope.admissionMode: UsageBasedAdmissionFairSharing`；LQ `fairSharing.weight`；Configuration `admissionFairSharing` |

前者回答"现在谁占得多"；后者回答"过去谁一直占得多"。两者可以同时开启，但调节的是不同的时间尺度。

另一个容易混淆的点：**Preemption-based Fair Sharing 不只用于抢占**。文档明确说，"During admission, Kueue prefers to admit Workloads from ClusterQueues that have the lowest share value first." 所以它同时影响准入顺序和抢占目标。

## 6.4 加权 Dominant Resource Share（DRS）

Kueue 的 Fair Sharing 核心是加权 Dominant Resource Share。它关注的不是"用了多少 nominal"，而是"超出 nominal、正在借用多少"。

对某个 CQ（或 Cohort 子树）和资源 `r`：

1. 计算超出保底的部分：
   ```text
   borrowed_r = max(0, usage_r - nominal_r)
   ```
2. 计算该层级的可借资源基数。Kueue 用的是**同一层级其他成员的 nominal 总和**（若设置了 `lendingLimit`，再用它截断该成员的可出借量），而不是对方此刻空闲多少：
   ```text
   lendable_r = Σ_{siblings} min(nominal_r, lendingLimit_r)
   ```
   这样份额不会随着 peer 当前用量抖动；peer 今天用满，不代表它明天不能出借。
3. 计算该资源的借用比例：
   ```text
   ratio_r = borrowed_r / lendable_r
   ```
   （分母为 0 时，Kueue 会剪枝这个节点；MiniKueue 用 `borrowed_r` 作为退化值。）
4. 取最大比例作为 unweighted DRS：
   ```text
   unweighted = max_r(ratio_r)
   ```
5. 用权重调整：
   ```text
   weightedShare = unweighted / weight
   ```
6. 准入时优先处理 `weightedShare` 最小的 CQ；
7. 抢占时优先从 `weightedShare` 最大的 CQ 中选受害者。

如果一个 CQ 完全在自己的 nominal 内，没有借用，它的 unweighted DRS 是 0，会被排除在抢占候选之外。这正是"公平分享只针对可借用资源"的体现。

为什么用"最大借用比例"而不是"总和"？

- 资源是多维的，不能简单相加；
- 一个 CQ 可能 CPU 借得多、内存借得少；
- Dominant Resource 是"最紧的那一维"，代表这个租户真正多占的瓶颈资源；
- 归一化到可借资源后，不同资源可以比较。

### 6.4.1 一个带数字的 DRS 例子

Cohort 中 A、B 两个 CQ，nominal 都是 10 CPU / 100Gi：

```text
A：usage 14 CPU / 10Gi   → borrowed_cpu=4，borrowed_mem=0
B：usage  8 CPU / 120Gi  → borrowed_cpu=0，borrowed_mem=20
```

可借资源基数来自对方的 **nominal**（这里没有 `lendingLimit`）：

```text
lendable_cpu = B 的 nominal 10
lendable_mem = A 的 nominal 100
```

则：

```text
A 的 unweighted DRS = max(4/10, 0/100)  = 0.4
B 的 unweighted DRS = max(0/10, 20/100) = 0.2
```

A 的份额更高，所以下一轮应优先让 B 准入；如果触发抢占，A 更可能成为 target。

如果 A 的权重是 10：

```text
A 的 weightedShare = 0.4 / 10 = 0.04
B 的 weightedShare = 0.2 / 1  = 0.2
```

A 的 weightedShare 反而更低，优先权翻转。这正是"权重不是硬配额，而是公平目标"的含义。

注意：分母用 nominal 而不是"对方当前空闲"，意味着份额不会因为 peer 今天用满就突然变大。真实 Kueue 还会按层级（AlmostLCA）比较并剪枝 DRS=0 的节点；不要把这里的数值当作生产参数。

### 权重

如果 A 的 `fairSharing.weight: 2`，B 的 `weight: 1`：

```text
A 的 weightedShare = unweighted_A / 2
B 的 weightedShare = unweighted_B / 1
```

权重越高，同样的借用比例算出的 share 越低，越容易优先获得资源。长期看，A 的份额会趋向 B 的两倍。

权重不是硬配额，而是公平目标。它不改变 nominalQuota 的保底/借用边界。权重为 0 是**合法值**（不是"未设置"）：借用中的 CQ 会被视为无穷 share，永远处于劣势；非零权重必须大于 `1e-9`。生产不要随意设为 0。

## 6.5 公平分享只针对"可借用的资源"

Kueue 的 Fair Sharing 主要作用于 Cohort 内**可借用的空闲 nominal**。原因是：

- 每个 CQ 的 nominal 是保底的，不应该被公平算法随意拿走；
- 公平问题主要出现在"大家都想借别人的空闲配额"时。

因此：

- 一个 CQ 在自己 nominal 内的使用，通常不参与公平抢占；
- 只有超出 nominal、正在借用（或可能借用）的 CQ，才是公平调整的对象。

这也解释了为什么 Kueue 的公平抢占会排除"没有借用"的 CQ。

### 6.5.1 Cohort 内的优先级排序

在 Cohort 内，如果有多个需要借用的 Workload，Kueue 默认按优先级排序；`PrioritySortingWithinCohort=false` 时改为按创建时间。这个 gate 影响的是"借用者之间的顺序"，不改变 CQ 自己的 QueueingStrategy。

一个常见误区：以为 `queueingStrategy: StrictFIFO` 就能保证全局 FIFO。实际上：

- StrictFIFO 只在单个 ClusterQueue 内阻塞；
- Cohort 内跨 CQ 的借用顺序由优先级/创建时间/公平份额决定；
- 高优先级 CQ 的 Workload 可能超过低优先级 CQ 的 Workload，即使后者先提交。

## 6.6 Admission Fair Sharing

除了基于当前用量的 Fair Sharing，Kueue 还提供 **Admission Fair Sharing (AFS)**：

- 关注 LocalQueue 的**历史资源使用**；
- 让长期占用较少的租户在排队时获得优先；
- 适合"突然爆发"的租户不应该长期压过稳定小租户的场景。

它和基于当前 DRS 的公平分享是互补的：

- 当前 DRS 解决"现在谁占得多"；
- 历史用量解决"过去谁一直占得多"。

### 6.6.1 启用方式

在 ClusterQueue 上设置：

```yaml
spec:
  admissionScope:
    admissionMode: UsageBasedAdmissionFairSharing
```

LocalQueue 可以有独立权重：

```yaml
kind: LocalQueue
spec:
  fairSharing:
    weight: "2"
```

全局参数在 Kueue Configuration：

```yaml
admissionFairSharing:
  usageHalfLifeTime: 168h       # 历史用量的半衰期
  usageSamplingInterval: 5m     # 采样间隔
  resourceWeights:
    nvidia.com/gpu: 10          # GPU 在用量统计中的权重
```

### 6.6.2 三个关键机制

1. **指数衰减的半衰期**：历史用量不是永久累计，而是按半衰期衰减。`usageHalfLifeTime` 越长，越"记仇"；越短，越看近期。
2. **Entry Penalty**：Workload 进入系统时先记一笔惩罚，防止租户在统计更新前刷入大量作业。惩罚在计量锚点结算。
3. **计量锚点**：`AdmissionFairSharingAnchorAtQuotaReservation`（v0.20 beta 默认开启）把计量从"Admitted"提前到"QuotaReserved"，这样等 AdmissionCheck 的 Workload 也会计入，避免通过慢检查占便宜。

### 6.6.3 观测

```bash
kubectl get localqueue team-a-queue -n team-a \
  -o jsonpath='{.status.fairSharing}'
```

`LocalQueue.status.fairSharing` 包含 `consumedResources`、`lastUpdate`、`weightedShare`。指标 `kueue_local_queue_admission_fair_sharing_usage` 也可以用来做仪表盘。

### 6.6.4 MiniKueue 的 AFS-lite

MiniKueue 实现了 Admission Fair Sharing 的可观测主干：

- `Engine.AFS` 配置半衰期（`HalfLifeTicks`）、Entry Penalty 和资源权重；
- 每次准入把 `Admission.Usage` 按权重折算后累加到 LocalQueue 的历史用量；
- 每个 tick 对历史用量做指数衰减：`factor = 0.5^(1/halfLife)`；
- CQ 的 `admissionScope.admissionMode: UsageBasedAdmissionFairSharing` 打开后，同一 CQ 内的 pending Workload 先按所属 LocalQueue 的 `history / weight` 升序，再按优先级/创建时间排序；
- LocalQueue 的 `fairSharing.weight` 参与分数计算。

它没有实现真实的采样窗口、自动 entry penalty 的结算方式、`AdmissionFairSharingAnchorAtQuotaReservation` 的精确语义；AFS 本身是在同一 ClusterQueue 内按 LocalQueue 排序，不存在"跨 CQ 的 AFS 比较"这个能力。场景 `admission-fair-sharing` 演示了"历史用量高的 LocalQueue 后准入"，运行 `-query` 可以观察分数。

历史用量不是硬配额，它只改变排队顺序；一个长期占用少的租户仍然可能因为优先级低而被压后。

MiniKueue 没有实现计量锚点和采样窗口，但已经覆盖了"历史用量 + 衰减 + 权重"这条主干；基于当前借用量的加权 DRS 仍由 `FairShare` 负责。

## 6.7 公平分享的实现要点

一个可工作的公平调度器需要处理：

### 1. 份额的时效性

Cache 中的 usage 是实时更新的。准入/驱逐后要立即重算 share，否则会连续做错决策。

### 2. 并发与快照

Kueue 在调度时使用 Cache 的快照，避免在计算过程中被其他事件修改。MiniKueue 用锁 + 单线程 Tick 简化了这一点。

### 3. 权重归一化

权重可以是小数/字符串（Kubernetes Quantity）。需要统一成可比较的浮点数，并处理 0/负数。

### 4. 层级 Cohort

在 Cohort 树中，不能只比较叶子 CQ 的 share，而要用 AlmostLCA 的分组份额。否则：

- 一个组织下有很多小 CQ，每个 share 都不高；
- 另一个组织只有一个大 CQ，share 较高；
- 直接比较叶子会把资源错误地倾斜给"数量多"的组织。

AlmostLCA 让比较发生在合适的层级。

### 5. 与优先级的关系

公平和优先级可能冲突：

- 高优先级作业是否应该无条件压过低优先级？
- 公平是否应该跨优先级生效？

Kueue 的默认行为是优先级优先，Fair Sharing 主要在相同/可比较的范围内调整。`reclaimWithinCohort` 和 Fair Sharing 抢占策略会进一步约束。

## 6.8 Fair Sharing 抢占策略

Kueue 支持两种预抢占策略（可组合）：

### `LessThanOrEqualToFinalShare`

只有满足以下条件才允许抢占：

```text
preemptor 准入后的 share ≤ victim 被驱逐后的 share
```

也就是说，抢占后不能让 preemptor 的份额超过 victim 原本的份额。

### `LessThanInitialShare`

只有满足以下条件才允许抢占：

```text
preemptor 准入后的 share < victim 被驱逐前的 share
```

它不依赖于 victim 被驱逐后的份额，因此更倾向于选择低优先级、最近才准入的 victim。

### 组合

```yaml
fairSharing:
  preemptionStrategies:
  - LessThanOrEqualToFinalShare
  - LessThanInitialShare
```

Kueue 只支持有限的组合/顺序；非法组合会导致配置校验失败。

### 防循环证明

Kueue 文档给出了一个关键性质：如果 A 能抢占 B，则 A 准入后的 DRS 严格小于 B 被驱逐前的 DRS；因此 B 之后不能反过来抢占 A。这避免了两个 CQ 之间的抢占循环。层级 Cohort 的完整证明仍在演进。

## 6.9 一个公平分享的推演

Cohort：A、B，各 nominal 10，总 20。

初始：

- A 使用 10；
- B 使用 0；
- 两者都没有借用，DRS 都是 0。

B 提交 5：

- B 新用量 5，仍在 nominal 内，直接准入；
- 两者 DRS 仍为 0。

B 再提交 5：

- B 新用量 10，仍在 nominal 内；
- 准入；
- 两者 DRS 仍为 0。

B 再提交 1：

- B 需要借 1，但 A 未使用 0，Cohort 总量已满，直接放不下；
- 如果 A 有可抢占的作业，B 是否能抢占，要看：
  - `reclaimWithinCohort`/Fair Sharing 策略；
  - B 准入后的 DRS 是否小于 A 被驱逐后的 DRS；
  - A 的作业优先级/准入时间。
- 如果 B 准入后借用 1，而 A 被驱逐后不再借用，B 的 DRS 可能高于 A，`LessThanOrEqualToFinalShare` 会阻止这次抢占。
- 这说明 Fair Sharing 不是"谁先要就给谁"，而是"抢占后必须更公平"。

注意：上面的数字是简化示意。真实 Kueue 的 DRS 以 FlavorResource 为单位、以 Cohort 的 lendable 为分母，并考虑层级 Cohort 的 AlmostLCA。

## 6.10 指标

关键指标：

- `kueue_cluster_queue_weighted_share`：CQ 的加权 share；
- `kueue_cluster_queue_resource_usage`：CQ 各资源用量；
- `kueue_cluster_queue_resource_reservation`：预留量；
- `kueue_cluster_queue_nominal_quota`：nominal；
- `kueue_admitted_workloads_total`：累计准入；
- `kueue_pending_workloads`：pending 数。

PromQL 示例：

```promql
# 各 CQ 当前加权份额
kueue_cluster_queue_weighted_share

# 借用最多的 CQ（usage - nominal，近似；保留 flavor 参与匹配）
sum by (cluster_queue) (
  kueue_cluster_queue_resource_usage
  - on(cluster_queue, flavor, resource) kueue_cluster_queue_nominal_quota
)
```

具体标签以你的版本和配置为准；自定义指标标签可能改变 label set。

## 6.11 MiniKueue 的实现

MiniKueue 的公平分享对齐了真实 Kueue 的计分语义：**只对"借用"计分，而不是对全部用量计分**；`lendable` 用其他成员的 **nominal 总和**（受 `lendingLimit` 截断），不随 peer 当前用量抖动。

```go
func (s *Scheduler) FairShare(cqName string) float64 {
    // 对每个 Flavor/Resource：
    //   borrowed = max(0, usage - effectiveNominal)
    //   lendable = Σ 其他 CQ 的 nominal（受 lendingLimit 截断）+ Cohort 共享池
    //   ratio    = borrowed / lendable（lendable 为 0 时剪枝）
    // 取最大 ratio，再除以 CQ 权重 × 路径上各 Cohort 权重
}
```

调度时：

```go
names = queue.FairShareOrder(names, e.Scheduler.FairShare)
```

即按加权 share 升序处理 CQ。一个完全在自己 nominal 内的 CQ，share 为 0；只有正在借用（或可能借用）的 CQ 才会被公平算法调整。这与第 6.5 节的语义一致。

它与真实 Kueue 的关键差异：

- MiniKueue 在**整个 Cohort 树**上聚合 lendable，但没有 AlmostLCA 的分层比较；
- 权重会让 CQ 权重与路径上每个显式 Cohort 的 `fairSharing.weight` 相乘，但没有 AlmostLCA 的分层比较；
- AFS-lite 有历史用量、半衰期和 Entry Penalty，但没有采样窗口与计量锚点；
- Fair Sharing 抢占是 lite：支持 `LessThanOrEqualToFinalShare` / `LessThanInitialShare`，但没有 AlmostLCA 和目标 CQ 排序/预抢占；
- 权重 0 的语义不同：真实 Kueue 把 0 当作无穷 share（永远处于劣势），MiniKueue 的 `fairShareWeight` 把 0 当默认值 1；需要这个边界时不要依赖 MiniKueue；
- 使用整数运算，最后才转 float，和真实 DRS 的 Quantity 精度不同。

这个实现足以演示"份额低的先准入""权重影响排序""nominal 内不计分"，但不能用于生产调参。

它没有实现：

- 基于 Cohort 树的 AlmostLCA；
- 目标 CQ 排序与预抢占（Fair Sharing 抢占目前是 lite）；
- AFS 的采样窗口与精确计量锚点；
- 完整的 DRF 资源组/多资源归一化；
- 与真实 Kueue 完全一致的 AlmostLCA 分层份额（当前是整棵 Cohort 树的扁平比较）。

Cohort 权重已实现：`fairShareWeight` 会把 CQ 权重与路径上各 Cohort 的 `fairSharing.weight` 相乘。

这些差距在第 15 章列出。

## 6.12 本章小结

- 队列排序是"优先级 + 创建时间"；StrictFIFO 会队头阻塞，BestEffortFIFO 会跳过。
- Fair Sharing 有两套：Preemption-based DRS（当前借用）和 Admission Fair Sharing（历史用量）。
- 加权 DRS 取"超出 nominal 的最紧资源比例 / 权重"；份额低的先准入，份额高的先被抢占。
- 公平主要作用于 Cohort 内可借用的空闲配额，不随意打破 nominal 保底。
- 层级 Cohort 使用 AlmostLCA 做分层比较；Cohort 级权重参与比较。
- Fair Sharing 抢占必须满足份额约束，避免抢占循环。
- MiniKueue 实现了基于借用量的 CQ 级加权 DRS 排序、Cohort 权重、AFS-lite（历史用量、衰减、Entry Penalty、LocalQueue 权重），以及 Fair Sharing 抢占 lite（两种 strategy）；仍未实现 AlmostLCA、目标 CQ 排序/预抢占、采样窗口和精确计量锚点。

---

[← 上一章：配额、Flavor 与 Cohort](05-quota-cohorts.md) | [返回目录](../README.md) | [下一章：抢占与回收 →](07-preemption-reclaim.md)
