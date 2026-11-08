# 第 21 章 · 平台队列与配额设计模式

第 5 章讲语义，第 18 章讲落地流程。本章补上中间那一层：**当你面对一个真实的组织、一批真实的作业时，队列和配额应该怎么设计？** 下面给出八种可复用的模式、它们的组合方式、反模式清单和选型矩阵。

## 21.1 从"资源池"到"配额产品"

一个平台团队最初通常只有一个资源池：

```text
GPU 池：64 卡，先到先得
```

随着团队增多，问题从"有没有卡"变成：

- 谁应该先拿到卡？
- 一个团队长期不用，能不能借给别人？
- 关键作业能否插队？
- 预算怎么分摊？
- 出问题谁负责？

这时配额就从"一个数字"变成"一份产品合同"。设计模式就是这份合同的常见形态。

## 21.2 模式一：独占池（Dedicated Pool）

### 结构

```text
Cohort: （无）
  cq-team-a: nominal = 32
  cq-team-b: nominal = 32
```

### 适用

- 团队之间强隔离，不能互相借；
- 每个团队容量可预测；
- 监管/合规要求资源不共享。

### 优点

- 行为最简单，排障容易；
- 一个团队的超载不会影响另一个；
- 不需要 Cohort/fair sharing。

### 缺点

- 利用率低：A 空闲时 B 不能用；
- 容量规划要求高，容易一边闲置一边排队。

### 变体

给每个 CQ 加 `lendingLimit: 0`，即使误加入 Cohort 也不会出借。

## 21.3 模式二：共享池 + 借用（Borrow / Lend）

### 结构

```text
Cohort: shared
  cq-team-a: nominal 32, borrowingLimit 16, lendingLimit 8
  cq-team-b: nominal 32, borrowingLimit 16, lendingLimit 8
```

### 适用

- 团队负载有明显峰谷；
- 希望提高利用率，但保留保底；
- 能接受一定的抢占/回收。

### 优点

- 保底 + 弹性的平衡；
- `lendingLimit` 保留自用余量；
- `borrowingLimit` 防止单团队抽干共享池。

### 缺点

- 需要解释"为什么我的作业被借走/被回收"；
- 抢占策略配置不当会抖动；
- 容量模型更复杂。

### 关键配置

```yaml
preemption:
  reclaimWithinCohort: LowerPriority
  borrowWithinCohort:
    policy: LowerPriority
    maxPriorityThreshold: 100
```

不要一上来用 `Any`；先用 `LowerPriority`，观察一段时间再收紧。

## 21.4 模式三：层级组织（Hierarchical Cohort）

### 结构

```text
root
├── org-research (weight 3)
│   ├── cq-research-a
│   └── cq-research-b
├── org-product (weight 2)
│   └── cq-product
└── org-shared (weight 1)
    └── cq-shared
```

### 适用

- 公司/部门/团队多层组织；
- 需要"先按组织分，再按团队分"；
- 有共享池和跨组织借用。

### 优点

- 表达能力强；
- 公平可以在正确层级比较（AlmostLCA）；
- 容量可以逐层聚合。

### 缺点

- 排障复杂：一笔借用可能受多层 limit 约束；
- AlmostLCA/权重不直观；
- 层级过深会增加计算和沟通成本。

### 经验

- 2–4 层足够；超过 4 层通常说明组织模型需要简化；
- 每层都要有 owner 和容量合同；
- 先在纸上画出"谁借谁、最坏情况借多少"。

## 21.5 模式四：关键业务保护（Priority + Reclaim）

### 结构

```text
Cohort: shared
  cq-critical: nominal 16, WorkloadPriorityClass=critical(1000), reclaimWithinCohort: Any
  cq-batch:    nominal 48, WorkloadPriorityClass=batch(0)
```

注意：`priority` 不是 ClusterQueue 字段，而是 Workload/WorkloadPriorityClass 的属性；这里写在结构图里只是表达"该队列的作业使用哪个优先级类"。

### 适用

- 在线推理/关键训练必须优先；
- 批处理可以被打断；
- 需要"关键作业随时能 reclaim"。

### 优点

- 关键业务有强保障；
- 批处理填充空闲，利用率高。

### 缺点

- `Any` 会抢占任意优先级，批处理可能反复被驱逐；
- 需要 checkpoint/重试机制；
- 关键业务自身也可能被更高优先级抢（如果没有上限）。

### 防护

- 给批处理设置最大重试/退避；
- 监控批处理的驱逐率；
- 关键业务使用独立 PriorityClass/WorkloadPriorityClass；
- 不要对"不可中断"的作业使用可抢占队列。

## 21.6 模式五：弹性/突发（Spot + On-demand）

### 结构

```text
resourceGroup:
  flavors:
  - spot        nominal 100, borrowingLimit 0
  - on-demand   nominal 20,  borrowingLimit 0
```

### 适用

- 成本敏感；
- 能容忍 spot 中断；
- 关键作业走 on-demand。

### 设计要点

- Flavor 顺序 = 成本/可靠性偏好；Kueue 按顺序 first-fit；
- `flavorFungibility` 控制"spot 借不到时是否先试 on-demand"；
- `Concurrent Admission` 可以先在 spot 跑，reservation 空出后迁移（alpha）；
- WaitForPodsReady + checkpoint 处理 spot 中断；
- 不要把 GPU 型号差异只体现在价格上，拓扑/显存也要建 Flavor。

### 反模式

只建一个 Flavor，把 spot/on-demand 混在一起。Kueue 无法区分，配额和抢占都会失去意义。

## 21.7 模式六：多集群/联邦（MultiKueue）

### 结构

```text
Manager Cluster
  ├── cq-global: nominal = Σ worker 容量
  └── MultiKueue AdmissionCheck

Worker Clusters
  ├── worker-onprem
  ├── worker-cloud-a
  └── worker-cloud-b
```

### 适用

- 多区域/多可用区；
- 本地 + 云突发；
- 数据 locality/合规；
- 团队各自管理 worker。

### 设计要点

- manager 的 nominal ≈ worker 总可用量，不能严重超卖；
- Incremental dispatcher 的集群顺序 = 成本/优先级顺序；
- worker 凭据用 Secret/ClusterProfile；
- 监控跨集群同步延迟和 workerLostTimeout；
- 明确 manager 故障时的行为。

### 反模式

把 manager 当成"第 N 个 worker"（角色共享不支持）；或者 manager 配额远大于 worker 总量，导致大量 Workload 被提名却永远放不下。

## 21.8 模式七：成本/碳排感知

### 结构

```text
Flavor: gpu-a100-spot       （便宜、可中断）
Flavor: gpu-a100-ondemand   （贵、稳定）
Flavor: gpu-h100-ondemand   （最贵、最快）

AdmissionCheck: budget-check（可选）
```

### 适用

- 成本中心需要分摊；
- 有碳排/能耗目标；
- spot 中断可接受。

### 设计要点

- 成本信号可以放在 Flavor 选择顺序、优先级或 AdmissionCheck；
- 不要让 Kueue 直接做财务结算；它只做准入；
- 用 labelKeysToCopy 把 cost-center 复制到 Workload，便于账单关联；
- 预算超限应该由外部系统拒绝/降级，而不是靠配额"猜"。

### 21.8.1 Showback/Chargeback 的口径

Kueue 只提供"谁占了多少配额/资源"的信号，不提供账单。要把信号变成可解释的账单，先固定四个口径：

| 维度 | 建议口径 | 说明 |
|---|---|---|
| 计量对象 | Pod/Workload 的实际运行时长 | 不是排队时长，也不是请求时长 |
| 资源单位 | GPU-hour、CPU-core-hour、GiB-hour | 按 Flavor/资源分别定价 |
| 借用 | 借入方按自己的价格计费 | 不要向出借方重复收费 |
| 空闲 nominal | 单独出"预留成本"报表 | 保底是承诺，不等于已使用 |

一个最小 PromQL（按 CQ/Flavor 做 showback，指标名以目标版本为准）：

```promql
# 过去 1 小时的平均 GPU 使用量 × 3600 = GPU-seconds
avg_over_time(
  kueue_cluster_queue_resource_usage{resource="nvidia.com/gpu"}[1h]
) * 3600
```

如果要按团队/成本中心归因，需要在 Workload 上保留 `labelKeysToCopy` 复制的标签，或者用 Pod 标签关联账单系统；Kueue 的队列级指标本身不带业务成本中心。

常见口径错误：

- **把 nominal 当实际消耗**：保底配额没被用掉也要收钱，会让团队不敢申请；
- **把排队时间算进 GPU-hour**：用户会为"没拿到卡"付费；
- **借用双重计费**：借入方和出借方都记一笔，总账翻倍；
- **被抢占作业按请求时长计费**：应按实际运行时长，抢占后的重跑单独计；
- **用 CQ 级聚合替代 Workload 级归因**：同一 CQ 下多个团队会被平均掉。

预算控制建议分成两层：**配额合同**负责"最多能用多少"，**外部预算系统/AdmissionCheck**负责"这个月还能不能花"。不要让 Kueue 的 `nominalQuota` 同时承担财务预算的角色，两者的变更节奏和审批人通常不同。

## 21.9 模式八：训练与推理混部

### 结构

```text
Cohort: gpu-pool
  cq-training: nominal 48, WorkloadPriorityClass=train(100), 可被抢占
  cq-inference: nominal 16, WorkloadPriorityClass=serving(1000), 不可抢占
```

同样，优先级属于 WorkloadPriorityClass，不属于 ClusterQueue。

### 适用

- 同一批 GPU 既跑训练又跑推理；
- 推理需要低延迟/常驻；
- 训练可以填谷。

### 设计要点

- 推理用 Deployment/LeaderWorkerSet，训练用 Job/JobSet；
- Deployment 是逐 Pod 通过 Pod 集成管理的，不是单个 Workload；滚动更新/扩缩容期间新旧 Pod 会同时占配额；
- 推理的"完成"语义与训练不同，配额释放要明确；
- 训练任务要 checkpoint，能承受抢占；
- 不要让推理和训练共享同一个 LocalQueue；
- 监控推理的 P99 延迟和训练的排队时间，分别设 SLO；
- 推理的接入细节、Partial Admission、TAS 打散和 HPA 边界见[第 36 章](36-inference-elastic-workloads.md)。

## 21.10 组合模式

真实平台通常是多个模式的组合：

```text
root
├── org-research (weight 3)
│   ├── cq-training-a100   （共享池 + 借用）
│   └── cq-training-h100   （独占 + spot/on-demand）
├── org-product (weight 2)
│   ├── cq-inference       （关键业务保护）
│   └── cq-batch           （可抢占）
└── org-shared
    └── cq-burst           （云突发 / MultiKueue）
```

组合时要回答：

1. 哪些边界是"硬隔离"，哪些是"软共享"？
2. 哪些资源可以跨组织借？上限是多少？
3. 哪些作业可以被抢占？谁来承担重试成本？
4. 哪些队列走多集群？manager 配额如何规划？
5. 成本/碳排信号放在哪一层？

## 21.11 配额合同模板

一份可以直接放进 Git 的合同：

```yaml
tenant: team-a
owner: team-a-lead
namespaces:
- team-a
entry:
  localQueue: team-a-queue
  allowedFrameworks: [batch/job, jobset, rayjob]
quota:
  clusterQueue: team-a-cq
  flavors:
    a100-spot:
      cpu: 128
      memory: 1Ti
      nvidia.com/gpu: 16
    a100-ondemand:
      cpu: 64
      memory: 512Gi
      nvidia.com/gpu: 8
borrowing:
  cohortName: org-research
  borrowingLimit:
    nvidia.com/gpu: 8
  lendingLimit:
    nvidia.com/gpu: 4
priority:
  allowedClasses: [standard, high]
  maxPriority: 100
preemption:
  canBePreempted: true
  canPreempt: false
fairSharing:
  weight: "1"
slo:
  p99QueueTime: 30m
  p99AdmissionTime: 60s
change:
  approver: platform-team
  rollback: git revert
  observationWindow: 2h
```

合同不是越细越好；字段要能映射到实际配置和监控，否则会变成"写了没人看"的文档。

## 21.12 反模式清单

1. **所有 CQ 都 `namespaceSelector: {}`**：等于没有租户边界；任何 namespace 都能用任何队列。
2. **borrowingLimit 全部不写**：等于允许抽干 Cohort；一个团队可能占满所有空闲。
3. **lendingLimit 全部不写**：本团队需要时才发现配额被借走。
4. **无脑 `reclaimWithinCohort: Any`**：低优先级作业被反复驱逐，吞吐下降。
5. **把 StrictFIFO 当默认**：大作业阻塞整个队列，利用率低。
6. **用配额表达安全隔离**：Kueue 不是 RBAC/NetworkPolicy。
7. **层级过深**：4 层以上，排障和公平都很难解释。
8. **Flavor 爆炸**：每个型号/价格/区域都建 Flavor，调度搜索空间爆炸。
9. **manager 配额远超 worker 总量**：MultiKueue 提名一堆放不下的作业。
10. **把 DQO alpha 当生产容量来源**：容量下降不驱逐，effectiveQuotas 会残留。
11. **只依赖 AFS 的自动 entry penalty、不限制 LocalQueue 创建**：租户仍可以通过新建队列刷低历史份额；真实 Kueue 的 entry penalty 不可直接配置，治理要落在 LQ 创建权限和团队维度绑定上。
12. **只设 CQ 级 borrowingLimit、不设 Cohort 级上限**：单个组织下的多个 CQ 可以合起来抽干共享池。
13. **DRA 同时走扩展资源和 DeviceClass 映射**：账本可能重复扣费；只保留一条计费路径。
14. **把 admission gate 当 AdmissionCheck 用**：gate 在配额之前，AdmissionCheck 在之后，混用会让排障链路变长。

## 21.13 选型矩阵

| 需求 | 推荐模式 | 关键配置 | 主要风险 |
|---|---|---|---|
| 强隔离 | 独占池 | 无 Cohort / lendingLimit 0 | 利用率低 |
| 提高利用率 | 共享池 + 借用 | borrowing/lending limit | 抢占抖动 |
| 多组织公平 | 层级 Cohort | AlmostLCA + 权重 | 复杂度 |
| 关键业务保障 | 优先级 + reclaim | `Any` + 高优先级 | 批处理饥饿 |
| 成本优化 | spot/on-demand Flavor | Flavor 顺序 + 暂停 | spot 中断 |
| 多集群 | MultiKueue | dispatcher + 凭据 | 同步/配额规划 |
| 训练推理混部 | 分队列 + 优先级 | 不同 StopPolicy/抢占 | SLO 冲突 |
| 弹性容量 | DQO（alpha） | CapacityProvider + orchestrator | 容量下降不驱逐 |
| 历史公平 | AFS | admissionScope + LQ weight + 自动 entry penalty | 冷启动/换队列操纵 |
| 组织级借用上限 | Cohort 级 borrowingLimit | 子树 limit + CQ limit | 逐层账本复杂 |
| 设备配额 | DRA | deviceClassMappings | 配额与真实设备分配有 gap |

这张表不是"标准答案"，而是把问题拆成可讨论的维度。真正的设计要从"组织如何协作"出发，而不是从 YAML 字段出发。

## 21.14 设计评审问题清单

在任何配额方案上线前，问十个问题：

1. 每个 CQ 的 owner 是谁？谁有权改配额？
2. nominal 总和与物理容量的关系是什么？安全余量多少？
3. 谁可以借谁？最坏情况借多少？原主回收时怎么办？
4. 哪些作业可以被抢占？重试成本谁承担？
5. 公平的层级和权重如何解释给租户？
6. 队列策略（StrictFIFO/BestEffortFIFO）与业务语义匹配吗？
7. Flavor 数量是否必要？是否会造成调度搜索爆炸？
8. 是否有 TAS/DRA 需求？物理放置是否被验证？
9. 多集群/动态容量的边界在哪里？失败时如何回退？
10. 监控、告警、runbook、回滚是否就绪？

答不上来的问题，就是上线后的故障点。

## 21.15 本章小结

- 配额设计是从"资源池"到"配额产品"的转变，核心是合同、边界和可解释性。
- 八种模式：独占、共享借用、层级组织、关键业务保护、弹性突发、多集群、成本/碳排、训练推理混部。
- 组合模式是常态；先定义硬隔离/软共享，再选字段。
- 配额合同要能映射到配置、监控和变更流程，否则只是文档。
- 反模式清单和设计评审问题可以直接用于方案评审。
- 选型矩阵帮助把"用哪个功能"转化为"解决哪个组织问题"。

---

[← 上一章：调度语义边界、失败分析与不变量](20-scheduling-edge-cases.md) | [返回目录](../README.md) | [下一章：真实集群实验手册 →](22-hands-on-labs.md)
