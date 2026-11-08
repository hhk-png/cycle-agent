# 第 20 章 · 调度语义边界、失败分析与不变量

前 19 章讲了"正常情况下 Kueue 怎么工作"。本章反过来：**在边界、异常和组合条件下，哪些直觉会失效？** 这些案例大多无法靠读 API 文档想明白，但它们的组合正是生产事故的高发区。

本章的案例都以 v1beta2 语义为准；MiniKueue 能复现的部分会标注"可复现"，不能复现的会说明差距。

## 20.1 为什么边界比主路径更重要

一个调度系统的正确性，往往不是由"最常见的那条路径"决定的，而是由这些问题的答案决定：

- 配额刚好差 1m 时，会准入还是 Pending？
- 两个同优先级作业互相需要对方的配额时，会不会死锁？
- 借用者被回收后，重排队会不会立刻再抢回来？
- WaitForPodsReady 超时和抢占同时发生时，先处理谁？
- 控制器重启后，Cache 重建是否会重复计费？

主路径决定系统能不能跑；边界决定系统会不会在凌晨三点炸。本章给出可复用的分析框架，而不是罗列所有 if/else。

## 20.2 配额边界

### 20.2.1 案例：borrowingLimit 不是"总能借到"

```text
Cohort shared
  A: nominal 10, borrowingLimit null
  B: nominal 10, borrowingLimit 0
```

B 提交 11：

- B 的 nominal 是 10，需要借 1；
- `borrowingLimit: 0` 是显式 0，不是 null；
- 结果：拒绝。

很多"为什么它不能借"的问题，根因是把 `borrowingLimit: 0` 和"不写"混为一谈。

### 20.2.2 案例：lendingLimit 会让空闲配额"消失"

```text
A: nominal 10, lendingLimit 2
B: nominal 10, 无 limit
```

A 使用 0，B 使用 10，B 提交 3：

- B 新用量 13，借 3；
- A 空闲 10，但只愿意借 2；
- Cohort 总量 13 ≤ 20；
- 结果：拒绝，因为可借量只有 2。

注意：`lendingLimit` 限制的是"别人能借走多少"，不是"A 自己能用多少"。A 自己仍然可以用满 10。

### 20.2.3 案例：Cohort 共享池是"额外的"

Cohort 定义 `nominalQuota: 12`，两个成员 CQ 各 nominal 10：

```text
Cohort 总量 = 10 + 10 + 12 = 32
```

不是 12，也不是 20。共享池是加在成员配额之上的。很多容量模型算错，是因为把 Cohort nominal 当成了"总池"。

### 20.2.4 案例：没有 nominalQuota 的 CQ 不能借

CQ 只覆盖 `nvidia.com/gpu`，但没有为它写 `nominalQuota`（哪怕 0）：

- 正常情况下，webhook 会直接拒绝这个 CQ（`flavor ... does not define nominal quota`）；
- 如果对象通过其他途径已存在，Kueue 也不会把这个 CQ 视为该资源的借用参与者，结果仍然无法借 GPU。

要借，必须显式写 `nominalQuota: 0`。这与第 23.2.5 节的报错处理是同一条规则。

### 20.2.5 案例：未覆盖资源不受管

CQ 只覆盖 `cpu` 和 `memory`，Workload 请求 `nvidia.com/gpu: 8`：

- Kueue 不检查 GPU；
- Workload 可能被准入；
- Pod 创建后，GPU 由 kube-scheduler 竞争。

这不是 bug，是契约：**Kueue 只管理 `coveredResources`**。如果你期望 GPU 被配额管理，必须把它写进 CQ。

### 20.2.6 案例：资源组的"同 Flavor"约束

CQ 有两个资源组：

```text
G1: cpu, memory
G2: nvidia.com/gpu
```

Workload 请求 CPU、内存、GPU。Kueue 可以为 G1 选 `cpu-flavor`、为 G2 选 `gpu-flavor`。如果这两个 Flavor 对应不同的节点池，Pod 可能无法落地——CPU 节点没有 GPU，GPU 节点没有足够的 CPU。逻辑配额完全正确，物理上却不可行。

这是"资源组"最常见的误用：**把同一个 Pod 需要的资源拆到两个组，以为只是计费独立，实际上破坏了放置可行性。**

### 20.2.7 案例：DRA 重复计费与未映射设备

一个 Pod 同时写了：

```yaml
resources:
  requests:
    nvidia.com/gpu: "1"
# 以及一个映射到 nvidia.com/gpu 的 DeviceClass 请求
```

如果实现把两者相加，账本会扣 2 卡，但节点上只用了 1 卡。正确语义是取 `max(request, deviceCount)`，不是求和。

另一个边界：DeviceClass 没有映射。真实 Kueue 会把它视为不受配额管理的设备，MiniKueue 在 Submit 时直接拒绝，避免"看起来计费了、其实没计"的静默错误。

排障时先确认：

- DeviceClass 是否在 `deviceClassMappings` 里；
- 同一资源是否走了两条路径；
- 计费模式是设备数、Counter 还是 Capacity。

### 20.2.8 案例：Cohort 级 limit 与 CQ 级 limit 的叠加

一个 CQ 可能同时受：

- 自己的 `borrowingLimit`；
- 父 Cohort 的 `borrowingLimit`；
- 根 Cohort 的 `borrowingLimit`；
- 总量约束；
- 出借方的 `lendingLimit`。

MiniKueue 的简化实现把 Cohort 级 borrowingLimit 当作"子树借用总量上限"，只检查本次新增借用；lendingLimit 被保守地施加到每个成员。生产排障时不要只看最底层 CQ，要沿 Cohort 路径逐层看。

### 20.2.9 配额不变量

可以用作测试的配额不变量：

```text
I1. 任何 CQ/Flavor/Resource 的 usage >= 0
I2. 无 Cohort 的 CQ：usage <= nominal
I3. 显式 borrowingLimit：usage <= nominal + borrowingLimit
I4. Cohort 总量：Σ usage <= Σ CQ nominal + Σ Cohort shared nominal
I5. 出借量 <= Σ peer 的 lendingLimit（未设置时 <= peer nominal）
I6. 正在借用的 CQ 必须有至少一个资源 usage > nominal
I7. 已准入 Workload 的 Admission.Usage == 其 PodSet 资源汇总
```

MiniKueue 的随机不变量测试覆盖了 I1–I4、I7 的一部分（见第 14 章）。

### 20.2.10 资源变换边界

资源变换会改变"配额看到的资源"，因此有三类容易踩的坑：

1. **input 不存在**：规则不生效，请求原样进入账本；不会报错，容易被误认为变换已生效。
2. **multiplyBy 是资源名但 Pod 没请求它**：MiniKueue 把倍率视为 0，outputs 不产生；真实 Kueue 也可能产出 0。如果期望"至少按 1 倍"，应该用固定整数或补上该资源。
3. **Replace + 空 outputs**：input 被完全忽略，常用于"只把 vGPU 计数当倍率"。如果 CQ 又覆盖了 input，会看到"CQ 有配额但用量永远是 0"的现象。
4. **与 DRA 重复计费**：如果同一个资源既通过 DeviceClass 映射、又在 `transformations` 里处理，可能被算两次。MiniKueue 的顺序是 DRA 先、变换后；生产上要坚持"一条路径只映射一次"。

对应不变量：

```text
T1. 变换后每个 Workload 的 TotalUsage 只包含 CQ 可见的资源
T2. Replace 的 input 不出现在 usage 中
T3. 每条 transformation 只基于原始 PodSpec 计算，不链式污染
T4. Retain 同时保留 input 和 outputs，不能只保留 outputs
```

## 20.3 队列策略边界

### 20.3.1 StrictFIFO 的饥饿

```text
CQ 2 CPU
队头：big 需要 3 CPU
队尾：small 需要 1 CPU
```

StrictFIFO 下：

- big 放不下 → 整个队列停止；
- small 不能插队；
- 即使集群里还有 1 CPU 空闲，也不会被利用。

这是"公平"与"利用率"的取舍。StrictFIFO 适合"顺序比吞吐重要"的场景，不适合混合负载。

### 20.3.2 BestEffortFIFO 的饥饿

BestEffortFIFO 跳过 big，让 small 跑。如果 small 源源不断：

- big 可能长期拿不到 3 CPU；
- 从吞吐看很健康，从 big 的角度看是饥饿。

需要配合公平分享、预留或最大等待时间来保护大作业。

### 20.3.3 优先级反转

默认排序是"优先级高的先来"。如果低优先级作业先占满配额：

- 高优先级作业来了，可能触发抢占；
- 但如果 `withinClusterQueue: Never`，高优先级只能等。

"优先级高"不等于"一定能抢占"；抢占是独立开关。

### 20.3.4 Admission gate 与 StrictFIFO

一个被 gate 卡住的 Workload 是否应该阻塞 StrictFIFO 的后续作业？

- 如果它还没拿到配额，严格 FIFO 会把它当作"暂时不可准入"的队头，后面的作业不能插队；
- 这与 BestEffortFIFO 的行为不同：后者会跳过它。

MiniKueue 在 StrictFIFO 下把带 gate 的 Workload 当作阻塞队头，在 BestEffortFIFO 下跳过。这个选择会影响"审批慢的作业是否拖住整条队列"，上线前要和业务确认。

### 20.3.5 同优先级死锁

两个同优先级、各需要 2 CPU 的作业，CQ 有 3 CPU：

- 第一个准入，占 2；
- 第二个需要 2，剩 1，放不下；
- 如果允许同优先级抢占，A 抢 B、B 抢 A 可能循环。

Kueue 用 `LowerOrNewerEqualPriority` + 创建时间解决同优先级抢占的有序性；MiniKueue 用 `olderWins` 做全序。没有全序的抢占实现会抖动。

## 20.4 抢占边界

### 20.4.1 抢占不足

preemptor 需要 4 CPU，victim 只占 2 CPU，且没有其他候选人：

- 抢了也不够；
- Kueue 不会做"抢一半"；
- 结果：不抢占，继续 Pending。

抢占是"一次性满足"，不是"逐步腾挪"。

### 20.4.2 抢占过多

候选人很多，每个都很小：

- 贪心加 victim 直到放得下；
- 可能抢了 10 个小作业，而其实 2 个大的就够；
- Kueue 的反向最小化会去掉不必要的 victim，但不保证全局最优。

这是启发式，不是最优解。生产上要监控"每次抢占的 victim 数"。

### 20.4.3 抢占风暴

触发条件：

- 高优先级作业频繁提交；
- 被抢占者退避时间太短；
- 公平分享/借用策略让同一批作业反复互相抢；
- 对称的 Configurable Preemption 规则。

防护：

- 退避 + 最大重试；
- 非对称规则；
- 监控 `kueue_preempted_workloads_total` 和驱逐原因；
- 对关键作业使用更高的 nominal，而不是只调优先级。

### 20.4.4 抢占与 TAS 的组合

TAS 作业被抢占后，腾出的不是"抽象的 CPU"，而是具体 domain 的容量。如果 victim 占了 rack-a 的一半，preemptor 需要整个 rack-a，仅移除 victim 可能仍然放不下；调度器会把 victim 从 `TopologyUsage` 中排除后再试。

边界：如果 preemptor 需要跨多个 domain，victim 集合的最小化会变得更复杂；MiniKueue 用同一套 `removed` 集合处理，但没有真实 TAS 的碎片整理。

### 20.4.5 抢占不变量

```text
P1. 抢占后 preemptor 必须能放下；否则不应提交
P2. victim 集合最小化后，移除任一 victim 都会导致放不下
P3. 同优先级抢占必须有全序，不能 A↔B 循环
P4. 被抢占者必须进入重排队（或停用），不能静默消失
P5. 抢占提交前要重新校验，失败则回滚
```

### 20.4.6 Configurable Preemption 的对称性风险

`PreemptionConfig` 的强大之处也是危险之处：规则可以跨 CQ、跨 Cohort 选择 victim。两条看似合理的规则如果对称，会让 A 抢 B、B 又抢 A：

```text
rule 1: A 可以抢 B（priority >=）
rule 2: B 可以抢 A（priority >=）
```

生产规则要显式设计成非对称：

- 只允许高优先级抢低优先级；
- 或只允许大作业抢小作业（numeric label）；
- 或只允许 hero 队列抢 batch 队列（clusterQueueSelector）；
- 配合 Classic Preemption 的 `Never`，避免两套规则叠加。

MiniKueue 的 Configurable 候选取并集后统一排序，最终仍用"逐个移除 + 反向最小化"；它不会自动检测规则对称性，必须在评审时人工检查。

### 20.4.7 Fair Sharing 抢占的等份额边界

`LessThanOrEqualToFinalShare` 允许 `preemptorNew == targetNew`。当两边都回到 0 份额时，等号成立，抢占是合法的；这不是 bug，而是"把借用者拉回 nominal"的正常结果。

需要警惕的是：

- 如果 target 被驱逐后 share 仍大于 preemptor，说明抢占没有改善公平性；
- `LessThanInitialShare` 更严格，但可能选不出任何 victim；
- 两种策略同时开启时，要明确"任一满足"还是"按顺序尝试"；MiniKueue 采用"任一满足"。

## 20.5 公平边界

### 20.5.1 权重为 0

Kueue 文档明确：权重 0 被视为无穷 share，借用中的 CQ 永远处于劣势。它不是"不参与公平"，而是"最不公平"。

生产不要随意设 0；如果想让某个 CQ 不参与，应该用独立的 Cohort 或配额，而不是权重 0。

### 20.5.2 分母为 0

如果 Cohort 中没有可借资源（所有 peer 都用满或 lendingLimit=0）：

- `borrowed / lendable` 的分母是 0；
- Kueue 会剪枝 DRS=0 的节点；
- MiniKueue 退化为用 `borrowed` 本身作为比例。

这会导致"没有可借资源时，公平排序退化为按借用量排序"。方向一致，但数值不可比。

### 20.5.3 AlmostLCA 的分组效应

```text
root
├── org-a
│   ├── cq-a1 ... cq-a10   （每个都很小）
└── org-b
    └── cq-b1              （一个大 CQ）
```

如果直接比较叶子 CQ 的 share，org-a 的 10 个小 CQ 可能各自都不高，而 cq-b1 很高；结果资源被"数量多"的 org-a 拿走。AlmostLCA 让比较发生在 org 层级，避免这种分组偏差。

### 20.5.4 AFS 历史用量：滞后与可操纵性

Admission Fair Sharing 看的是衰减后的历史用量，因此同时存在"统计滞后"和"可操纵"两类边界：

- **冷启动**：新 LocalQueue 历史为 0，会连续优先；真实 Kueue 的 entry penalty 由公式自动计算、不可直接配置，租户仍可以不断新建 LocalQueue 刷低份额。
- **半衰期太长** → 新租户长期吃亏；**半衰期太短** → 租户可以通过"停一段时间再爆发"刷低份额。
- **采样间隔太长** → 统计滞后，Entry Penalty 只能部分弥补。
- **迁移**：把作业换到另一个 LocalQueue 提交，历史不会跟着走。
- **权重为 0**：和 Fair Sharing 一样，权重 0 会让分数趋于无穷，等于永远最后。

没有一组参数适合所有平台；要用真实到达过程调。MiniKueue 的 AFS-lite 把 Entry Penalty 和 LocalQueue 权重暴露为可调参数，用来演示前两类问题；真实 Kueue 只暴露 `usageHalfLifeTime`/`usageSamplingInterval`/`resourceWeights`，entry penalty 不是用户可配字段。生产上要把 LocalQueue 的创建权限和团队维度绑定纳入治理。

### 20.5.6 公平不变量

```text
F1. nominal 内的 usage 不应被公平算法惩罚
F2. 加权份额应与权重成反比（长期）
F3. 公平抢占后不能出现 A↔B 循环
F4. DRS=0 的 CQ 不参与公平抢占
F5. 历史用量的衰减必须单调、可复现
F6. 公平抢占后 preemptorNewShare 必须满足所选 strategy，否则不能提交
F7. 公平抢占的 victim 必须来自同一 Cohort 树且优先级不高于 preemptor
```

## 20.6 生命周期边界

### 20.6.1 Retry 与 Rejected 的边界

- `Retry` 是"暂时不行"：释放配额，退避重试；
- `Rejected` 是"永远不行"：驱逐、停用，需要人工改 `active`。

把永久失败标成 Retry，会导致无限重试和配额抖动；把临时失败标成 Rejected，会导致作业被误杀。检查控制器要明确区分。

### 20.6.2 Retry 与 WaitForPodsReady 的叠加

Workload 在等 AdmissionCheck Retry 时，还没有 Admitted，不会触发 WaitForPodsReady。只有 Admitted 之后才开始计时。不要把两个 timeout 混在一起调。

### 20.6.3 max execution time 的累计

`maximumExecutionTimeSeconds` 是累计 Admitted 时间，跨多次 Admit/Evict。一个作业被抢占多次，累计时间仍然增长；重新激活后才清零。

边界：如果作业在 Admitted 的同一 tick 内被驱逐，计时是否计入？不同实现可能有细微差异；测试时要固定 tick 语义。

### 20.6.4 deactivate 的连锁反应

`active=false` 会驱逐并停止重排队。如果上层 Job 控制器看到 Workload 被停用后重新创建 Job/Workload，可能形成"停用—重建—再停用"的循环。集成层要尊重 `active`，不要自动复活。

### 20.6.5 完成与失败的边界

`Finished=True` 表示 Workload 完成，配额释放。它不区分成功/失败；失败重试仍由 Job 控制器负责。不要把 Kueue 的 Finished 当成"业务成功"。

## 20.7 TAS/DRA 边界

### 20.7.1 配额够但拓扑放不下

CQ 有 12 GPU，三个 4 卡作业，每个 rack 只有 4 卡：

- 配额上 12 卡够；
- 前两个各占一个 rack；
- 第三个没有完整 rack，Pending。

这正是 TAS 要解决的问题：把"物理可行性"提前到准入阶段。

### 20.7.2 碎片

四个 rack，每个 2 卡；一个 4 卡作业：

- 总容量 8 卡，足够；
- 但没有一个 rack 有 4 卡；
- unconstrained 模式可以跨 rack，required 模式不行。

TAS 的 `preferred` 会逐级放宽，`required` 不会。选哪种模式取决于通信需求。

### 20.7.3 热替换的边界

节点故障时：

- 单节点故障：尝试只替换坏节点；
- 多节点故障：可能整体驱逐；
- 替换失败且 `FailFast`：不再重试；
- 节点污点：`NoExecute` 驱逐，`NoSchedule` 不驱逐已运行 Pod。

热替换不是"永远能救回来"，它有明确的失败路径。

### 20.7.4 slice 的 partial 与跨 domain

- `count=10`、`SliceSize=4`：切成 4+4+2，最后一个 partial slice 仍必须在一个 domain 内；
- 如果每个 host 只有 2 卡，partial slice 的 2 个 Pod 可以放一个 host；如果只有 1 卡，它会失败或需要跨 host（取决于实现）；
- 多层 slice：内层 size 必须整除外层，否则某些 Pod 没有归属；
- slice 与 `Group` 组合没有明确定义，MiniKueue 直接拒绝。

排障时看 `status.admission.podSetAssignments[*].topologyAssignment` 是否产生多条 assignment，以及每条 `PodCount` 是否等于 slice size。

### 20.7.5 DRA 的"配额已预留但设备未分配"

Kueue 的配额检查看到的是逻辑设备数；kube-scheduler/DRA driver 才做真实分配。可能：

- 配额够，节点没有设备；
- 设备被其他 Pod 占用但未计入；
- 设备分配失败，Pod Pending。

`KueueDRADeviceFeasibility` 只能部分弥补；最终仍要靠 WaitForPodsReady 兜底。

## 20.8 一致性与恢复边界

### 20.8.1 快照与提交的竞态

Scheduler 基于快照做决策，提交前对象可能已经变了：

- Workload 被用户停用；
- CQ 被改小；
- 另一个控制器改了 Workload。

Kueue 在提交前重新校验；失败则放弃本次决策。MiniKueue 用单锁串行化，避免了这类竞态，但也失去了并发。

### 20.8.2 重启后的账本重建

控制器重启后：

1. informer 全量 list；
2. Cache 重建；
3. QueueManager 重建 heads/backlog；
4. Scheduler 重新评估。

在此期间：

- 已 Admitted 的 Workload 不会重新调度；
- 新 Workload 可能短暂不被处理；
- Cache 如果重建错误，会重复计费或漏计。

大规模集群的重建时间可能到分钟级。不要因为"重启后没立刻调度"就反复重启。

### 20.8.3 重复事件与幂等

informer 可能重复投递同一事件；控制器必须幂等：

- 同一个 Workload 不能重复 Add 到 Cache；
- 同一个 AdmissionCheck 不能重复扣配额；
- 同一个 ProvisioningRequest 不能重复创建。

Kueue 用 key + resourceVersion + 幂等更新处理；MiniKueue 用 `Cache.Add` 的重复 key 检查。

### 20.8.4 Concurrent Admission 的迁移循环

迁移必须满足"严格更优"才执行，否则会出现 A↔B 抖动：

- 只允许迁移到 Flavor 列表更靠前的索引；
- 迁移后重新计算仍应得到同一个更优 Flavor；
- `LastAcceptableFlavorName` 是上界，不是目标；
- 迁移会重建 Pod，必须考虑 checkpoint 和中断成本。

MiniKueue 用"索引严格变小"保证终止；它不模拟 Variant 的并行尝试，因此不会出现多个 Variant 同时占配额的问题。

### 20.8.5 MultiKueue 的分裂与过度提名

- manager 配额远大于 worker 总量 → 大量 Workload 被提名但永远放不下；
- 两个 worker 同时准入 → 必须只保留一个，否则同一作业跑两份；
- worker 失联 → 需要 `workerLostTimeout` 和清理；
- 状态同步延迟 → manager 可能短时间内认为没有 worker 准入。

MiniKueue 的 MultiKueue-lite 在单 tick 内按 worker 顺序选择第一个 Admitted，并立即删除其他副本；它用 `Worker.Down` + `WorkerLostTimeout` 模拟"选中 worker 失联后超时切换、恢复后清理陈旧副本"（`multikueue-failover` 场景）。但它没有真实网络分区、凭据失败、连接退避和 split-brain 模型，因此不能替代真实多集群故障演练。

## 20.9 不变量目录

把下面这份清单直接变成测试断言：

### 配额

```text
Q1  usage >= 0
Q2  无 Cohort：usage <= nominal
Q3  borrowingLimit 显式：usage <= nominal + limit
Q4  Cohort 总量 <= Σ CQ nominal + Σ Cohort shared
Q5  借入量 <= 可借量
Q6  Admission.Usage == Σ PodSet request × effective count
Q7  DRA：同一逻辑资源取 max(extended, device)，不得重复累加
Q8  Cohort 级 borrowingLimit：子树借用总量 <= limit
Q9  AFS 分数非负；排序不修改配额
```

### 队列

```text
S1  StrictFIFO 下，队头放不下时后续不得准入
S2  BestEffortFIFO 下，后续可跳过队头
S3  requeueAt 未到的 Workload 不参与调度
S4  inadmissible 的 Workload 不占用 heads
```

### 抢占

```text
P1  提交抢占后 preemptor 能放下
P2  victim 集合最小
P3  被抢占者进入重排队/停用
P4  同优先级有全序
```

### 生命周期

```text
L1  QuotaReserved/Admitted 的 Workload 必须在 Cache 中
L2  Cache 中的 Workload 必须 QuotaReserved/Admitted
L3  驱逐后不得继续占用 Cache
L4  Finished 后配额释放
L5  active=false 不再重排队
L6  带 admission gate 的 Workload 不占 Cache
L7  Concurrent 迁移只向更小 Flavor 索引，且不重置 AdmittedAt
```

### TAS

```text
T1  每个 topology/level/domain 的 usage <= 聚合容量（用 Scheduler.TopologyUsage 跨层检查）
T2  Required 的 PodSet 在同一个 domain
T3  Group 的 PodSet 共享 domain
T4  抢占模拟移除 victim 后，拓扑容量正确恢复
T5  slice 的每条 assignment.PodCount <= SliceSize，且总 PodCount == count
T6  Spreading 选择的 domain 当前用量不高于任何同样可行的 domain
```

MiniKueue 的 `invariant_test.go` 覆盖了 Q1–Q6、Q8、L1–L3、T1、T5 的大部分；单元测试另外覆盖 Q7、Q9、L6–L7、T6 的关键路径。第 27 章会把这份不变量目录扩展成验证流程。完整的目录仍然可以作为扩展练习。

## 20.10 失败分析决策树

面对"作业没跑起来"，按下面的顺序走：

```text
1. Workload 存在吗？
   ├─ 否 → Job 集成/标签/控制器是否工作
   └─ 是 ↓
2. Finished=True？
   ├─ 是 → 作业已结束；看业务结果
   └─ 否 ↓
2.5 AdmissionGated=True？
   ├─ 是 → 外部 gate 未清除；找 gate owner，不要加配额
   └─ 否 ↓
3. QuotaReserved=False 且 Reason=Inadmissible？
   ├─ 是 → selector/LimitRange/Flavor/资源名；改配置
   └─ 否 ↓
4. QuotaReserved=True？
   ├─ 否 → 配额/Cohort/借用/队列策略/退避
   └─ 是 ↓
5. Admitted=True？
   ├─ 否 → AdmissionCheck/ProvisioningRequest/多集群
   └─ 是 ↓
6. Pod 创建了吗？
   ├─ 否 → Job 集成/暂停/调度门控
   └─ 是 ↓
7. Pod 调度了吗？
   ├─ 否 → nodeSelector/toleration/碎片/TAS/DRA
   └─ 是 ↓
8. Pod Ready 了吗？
   ├─ 否 → 镜像/启动/资源/Probe；WaitForPodsReady 是否超时
   └─ 是 → 运行中；看业务日志
```

每一步只回答一个问题，避免"一上来就看控制器日志"。

## 20.11 本章小结

- 配额边界的关键是 null/0、共享池是额外的、未覆盖资源不受管、资源组影响物理可行性。
- 队列边界的关键是两种 FIFO 都会造成某种饥饿，优先级不等于抢占。
- 抢占边界的关键是"一次性满足"、最小 victim、全序防循环、提交前校验。
- 公平边界的关键是权重 0、分母 0、AlmostLCA 分组、历史滞后。
- 生命周期边界的关键是 Retry/Rejected 区分、累计计时、active 的连锁反应。
- TAS/DRA 边界的关键是配额 ≠ 物理可放置，热替换有失败路径。
- 一致性边界的关键是快照竞态、重启重建、幂等。
- 新能力边界：DRA 取 max 防重复计费、Cohort limit 逐层叠加、AFS 可被冷启动/换队操纵、gate 与 StrictFIFO 的交互、Concurrent 只向更优迁移、MultiKueue 的重复准入。
- 把 20.9 的不变量目录变成测试，比逐个场景断言更能抓住账本错误。
- 20.10 的决策树可以直接用于 on-call。

---

[← 上一章：MiniKueue 扩展实验室](19-mini-kueue-extension-lab.md) | [返回目录](../README.md) | [下一章：平台队列与配额设计模式 →](21-platform-design-patterns.md)
