# 第 5 章 · 配额、Flavor 与 Cohort

## 5.1 配额的三个层次

Kueue 的配额不是一个数字，而是三层结构的组合：

```text
ClusterQueue 的 nominalQuota
  └── 这个 CQ 的保底额度

Cohort 的聚合 nominalQuota
  └── 同 Cohort 内所有 CQ 的 nominal + Cohort 自己的共享额度

borrowingLimit / lendingLimit
  └── 借用和出借的边界
```

理解这三层，才能回答：

- 为什么 A 能用超过自己 nominal 的资源？
- 为什么 B 明明有 nominal，却拿不到？
- 为什么 C 设置了 borrowingLimit 还是借不到？
- 为什么 D 设置了 lendingLimit，别人就只能借一部分？

### 5.1.1 形式化模型

对某个 Flavor/Resource，记：

```text
N_c      ClusterQueue c 的 nominalQuota
U_c      c 的当前 usage（已预留 + 已准入）
B_c      c 的 borrowingLimit（null = 无穷）
L_c      c 的 lendingLimit（null = N_c）
T        当前 Cohort 树的共享 nominal 总和
```

一个待准入 Workload 请求 `r` 能放进 c，当且仅当同时满足：

```text
(1) U_c + r <= N_c + B_c            # 借入上限
(2) Σ_{peer} max(0, min(N_peer - U_peer, L_peer)) + free_shared
      >= max(0, U_c + r - N_c)      # 有人愿意借（含 Cohort 共享池空闲）
(3) Σ_{all} U + r <= Σ_{all} N + T  # Cohort 总量
```

注意第 (2) 条右边的可借量不是 `Σ N_peer`，而是"peer 愿意出借的空闲配额"（受各自 `lendingLimit` 截断）。这解释了为什么 `lendingLimit` 会把本来空闲的配额"锁住"。

三条约束缺一不可。实际排障时按这个顺序检查，比凭直觉猜"总量够不够"可靠得多。

## 5.2 基础配额

一个最简单的 ClusterQueue：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: cq
spec:
  namespaceSelector: {}
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: default
      resources:
      - name: cpu
        nominalQuota: 10
      - name: memory
        nominalQuota: 40Gi
```

在没有任何 Cohort 的情况下：

- CPU 最多 10；
- 内存最多 40Gi；
- 不能向别人借，也不能借给别人。

如果当前用量是 CPU 6、内存 20Gi，再提交一个 CPU 5 的 Workload：

- CPU：6+5=11 > 10 → 拒绝。

这就是最朴素的配额。

## 5.3 Flavor：同一资源的不同来源

```yaml
spec:
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: spot
      resources:
      - {name: cpu, nominalQuota: 100}
      - {name: memory, nominalQuota: 400Gi}
      - {name: nvidia.com/gpu, nominalQuota: 8}
    - name: on-demand
      resources:
      - {name: cpu, nominalQuota: 200}
      - {name: memory, nominalQuota: 800Gi}
      - {name: nvidia.com/gpu, nominalQuota: 16}
```

两个 Flavor 是同一个资源组，意味着：

- 一个 Workload 的 CPU、内存、GPU 必须来自同一个 Flavor；
- Kueue 按列表顺序尝试 `spot`，再尝试 `on-demand`；
- 每个 Flavor 有独立配额。

如果 Workload 需要 12 GPU：

- `spot` 只有 8，放不下；
- `on-demand` 有 16，可以放；
- 准入结果会记录 `on-demand`。

如果 Workload 需要 8 GPU、200 CPU：

- `spot` 的 CPU 只有 100，放不下；
- `on-demand` 可以。

### 资源组为什么重要

假设你把 CPU/内存放在组 A，GPU 放在组 B：

```yaml
resourceGroups:
- coveredResources: ["cpu", "memory"]
  flavors: [{name: cpu-default, ...}]
- coveredResources: ["nvidia.com/gpu"]
  flavors: [{name: gpu-a100, ...}]
```

这意味着 CPU/内存和 GPU 可以来自不同 Flavor。对于"GPU 是通过网络挂载，不跟节点绑定"的场景，这是合理的；对于"GPU 就在节点上"的场景，应该放在同一组，避免 CPU 在一种节点、GPU 在另一种节点，导致 Pod 无法落地。

## 5.4 Cohort 借用：核心公式

Kueue 文档给出的判定条件（对某个 Flavor/Resource）：

一个 Workload 能放下，当且仅当满足：

1. 请求 ≤ 该 CQ 在该 Flavor 上的**未使用 nominalQuota**；或
2. 请求 ≤ Cohort 内所有 CQ 的**未使用 nominalQuota 之和**；并且
3. 请求 ≤ 该 CQ 的 **未使用 nominalQuota + borrowingLimit**。

再加上 lendingLimit 的约束：

- 一个 CQ 的出借上限是 `lendingLimit`；
- 如果 `lendingLimit` 为 null，则它的全部 nominal 都可以出借；
- 实际可借到的量，不能超过其他 CQ 愿意出借的总和。

> 注意：`borrowingLimit` 为 null 表示**不限制**，不是 0。很多“为什么它能借这么多”的问题都来自这个默认值。

### 例 1：Cohort 总量足够，借得到

```text
Cohort shared
  A: nominal 10
  B: nominal 10
```

当前：

- A 使用 5；
- B 使用 0。

B 提交一个需要 12 的 Workload：

- B 的 nominal 是 10，需要借 2；
- A 未使用 5，可以出借；
- Cohort 总 nominal 20，当前总用量 5，加 12 = 17 ≤ 20；
- 结果：B 借 2，准入。

### 例 2：Cohort 总量不够，借不到

A 使用 10，B 使用 0，B 提交 12：

- B 需要借 2；
- A 未使用 0；
- Cohort 总用量 10 + 12 = 22 > 20；
- 结果：拒绝。

即使 B 的 borrowingLimit 是“无限制”，也没有空闲配额可借。

### 例 3：borrowingLimit 封顶

A 使用 0，B 使用 8，B 提交 3：

- B 当前用量 8，新作业 3，新用量 11；
- 需要借 1；
- Cohort 总用量 8 + 3 = 11 ≤ 20；
- 如果 B 的 `borrowingLimit: 0`，则借 1 > 0 → 拒绝；
- 如果 B 的 `borrowingLimit: 1` 或 null，则准入。

对比：A 使用 0，B 使用 8，B 提交 11：

- 新用量 19，需要借 9；
- Cohort 总用量 19 ≤ 20，但借 9 超过 `borrowingLimit: 1`；
- 仍然拒绝。

这组例子说明：**borrowingLimit 是"能借多少"的上限，不是"总能借到"的保证**。

### 例 4：lendingLimit 封顶

A nominal 10，`lendingLimit: 4`；B nominal 10。

A 使用 0，B 使用 8，B 提交 12：

- B 新用量 20，需要借 10；
- Cohort 总用量 20 ≤ 20；
- A 未使用 10，但 `lendingLimit: 4`，最多只愿意借 4；
- 借 10 > 4 → 拒绝。

换 B 使用 8，提交 4：

- B 新用量 12，需要借 2；
- Cohort 总用量 12 ≤ 20；
- A 愿意借 4，2 ≤ 4；
- 准入。

如果 A 的 `lendingLimit: 0`，则 B 只能用到自己的 nominal 10：
B 使用 8 时，最多还能提交 2；提交 3（新用量 11，借 1）也会被拒绝。

### 例 5：CQ 总量封顶

A nominal 10，B nominal 10，B 的 `borrowingLimit: 3`。

A 使用 0，B 使用 7：

- B 提交 3：新用量 10，借 0，准入；
- B 提交 4：新用量 11，借 1 ≤ 3，Cohort 总用量 11 ≤ 20，准入；
- B 提交 7：新用量 14，借 4 > 3 → 拒绝（此时 Cohort 总用量 14 ≤ 20，但借入上限不够）。

如果 B 提交 14（新用量 21）：
- 借 11 > 3，且 Cohort 总用量 21 > 20，两层都拒绝。

### 例 6：Cohort 共享额度

Cohort 本身可以有 `resourceGroups`，作为额外的共享池：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: shared
spec:
  resourceGroups:
  - coveredResources: ["cpu"]
    flavors:
    - name: default
      resources:
      - name: cpu
        nominalQuota: 12
```

A 和 B 的 CQ 都可以定义 `nominalQuota: 0` 的同一 Flavor；它们可以从 Cohort 的 12 中借。

这意味着 Cohort 既是一个"共享池"，也是 CQ 之间借用的边界。Cohort 的 nominal 可以继续向父 Cohort 出借，受 lendingLimit 限制。

MiniKueue 的 `cohort-shared-pool` 场景就是这一语义的最小复现：两个成员 CQ 的 nominal 都是 0，Cohort 自己提供 4 CPU；第一个 3 CPU 作业从共享池借到 3，第二个 3 CPU 作业因为超过共享池总量而 Pending。

### 5.4.1 隐式 Cohort 与显式 Cohort

（前面六个例子是 5.4 的算例；这一节讲对象形态。）

很多文档只写"设置 `cohortName` 就加入 Cohort"，容易让人以为必须创建 `Cohort` 对象。实际有两种形态：

| 形态 | 创建方式 | 能做什么 | 不能做什么 |
|---|---|---|---|
| 隐式 Cohort | 只在 CQ 上写相同的 `cohortName` | 互相借用空闲 nominal | 没有共享池、层级、Cohort 级 limit、权重 |
| 显式 Cohort | 创建同名 `Cohort` 对象（可带 `parentName`/`resourceGroups`/`fairSharing`） | 上述全部能力 | 需要维护额外对象和引用 |

实践建议：

- 只是两个团队临时共享，用隐式 Cohort 即可；
- 要做组织级配额、共享池、层级公平，直接建 `Cohort` 对象，不要后期迁移；
- `cohortName` 是分组名，不是对象引用；删除 `Cohort` 对象不会自动把 CQ 移出分组，行为要按版本验证。

MiniKueue 现在同时支持两种形态：CQ 的 `cohortName` 找不到同名 Cohort 对象时，按隐式扁平 Cohort 处理，仍可与同名字段的其他 CQ 借用/出借；只有创建 Cohort 对象后，才启用共享池、层级和 Cohort 级 limit。场景 `implicit-cohort` 演示了没有 Cohort 对象时的借用。

## 5.5 层级 Cohort

Cohort 可以嵌套：

```text
root
├── org-a (weight 3)
│   ├── team-a1 CQ
│   └── team-a2 CQ
└── org-b (weight 1)
    └── team-b CQ
```

规则：

- 同一 CohortTree 内的 CQ 可以共享资源；
- Cohort 可以定义自己的 nominalQuota，作为该子树的共享池；
- Cohort 和 CQ 都可以有 borrowingLimit/lendingLimit；
- Fair Sharing 的权重定义在 CQ 和 Cohort 上，用于计算加权份额。

### 5.5.1 Cohort 的边界与故障语义

以下四种情况会让整棵子树"突然不工作"：

| 情况 | 行为 | 处理 |
|---|---|---|
| 修改 `spec.parentName` | 整棵子树跟着移动；移动期间的配额/公平视图会重建 | 在维护窗口做；先确认没有跨子树借用依赖 |
| Cohort 环（A→B→A） | Kueue 禁用环内所有 Cohort/CQ，直到环被移除 | 删除/修正 `parentName`；不要在环里排查单个 CQ |
| 非根 Cohort 设置 `borrowingLimit`/`lendingLimit` | 只有存在父级时才允许；否则 webhook 拒绝 | 给 Cohort 设 `parentName`，或把 limit 放到 CQ 上 |
| `parentName` 指向不存在的 Cohort | 引用无效：对象被拒绝或进入 `Inactive`，**不会**静默挂到一个默认 Cohort | 先创建父 Cohort；检查 `status.conditions` 的 message |

这些错误在 `kubectl get cohort` 里可能只表现为 `Active=False`，真正的 message 要在 `status.conditions` 里看。

### AlmostLCA

在层级 Cohort 中，判断"两个 CQ 之间的公平"不能只看它们自己的 share，而要看它们在树上的相对位置。Kueue 使用 **Almost Least Common Ancestor (AlmostLCA)** 概念：

- 对 CQ x 和 y，找到它们的最低公共祖先 LCA；
- AlmostLCA(x, y) 是 x 到 LCA 路径上、LCA 下面那个节点；
- 公平比较使用 AlmostLCA 的 share，而不是 x/y 自己的 share。

直观理解：

- 同一个团队下的两个 CQ 竞争时，比较的是它们各自的份额；
- 不同组织下的 CQ 竞争时，先比较组织的份额，再比较组织内 CQ 的份额。

这让公平分享可以"先按组织分，再按团队分"，而不是让某个组织里的很多小 CQ 通过数量优势压过另一个组织。

一个具体的 AlmostLCA 例子：

```text
root
├── org-a (weight 3)
│   ├── cq-a1
│   └── cq-a2
└── org-b (weight 1)
    └── cq-b1
```

`cq-a1` 和 `cq-b1` 比较时，LCA 是 `root`，AlmostLCA(cq-a1, cq-b1) 是 `org-a`，AlmostLCA(cq-b1, cq-a1) 是 `org-b`。所以先比较两个组织的加权份额，而不是拿 `cq-a1` 和 `cq-b1` 自己的份额直接比。`cq-a1` 和 `cq-a2` 比较时，LCA 是 `org-a`，AlmostLCA 就是它们自己。

### Cohort 级 borrowing/lending limit

Cohort 也能定义 `borrowingLimit` 和 `lendingLimit`，约束的是**整个子树**向父 Cohort 借/出借的总量，而不是单个 CQ。典型用途：

- 一个组织最多从公司总池借 100 卡，无论组织内多少团队；
- 一个组织最多借出 50 卡给兄弟组织，保留至少一部分自用；
- 限制子组织把父级共享池"抽干"。

这会显著增加推理复杂度：同一笔借用可能同时受 CQ 级 limit、路径上每个 Cohort 的 limit、以及根总量约束。排障时不要只看最底层的 CQ。

MiniKueue 实现了 Cohort 树的配额聚合与共享池，并实现了简化版的 Cohort 级 borrowing/lending limit：

- **borrowingLimit** 被当作"整个子树向父 Cohort 借用的总量上限"。调度器沿 CQ 的 Cohort 路径逐层检查，只计算本次新增的借用量；子树已有借用按**每个成员的正借用之和**计算，不会用空闲成员的未用量去抵消另一个成员的借用；
- **lendingLimit** 被当作"每个成员最多能借出多少"，再对成员求和。注意这不是子树总量上限：子树有 N 个成员时，实际可出借量仍可能接近 N × limit；
- **共享池**按根树扁平聚合：目标 CQ 能看到整棵根树下所有显式 Cohort 的共享池，包括兄弟子树的共享池。真实 Kueue 的共享池只在子树内可见；
- 隐式 Cohort（只有 `cohortName`、没有 Cohort 对象）现在可以直接参与借用，不再要求创建对象。

它仍未实现 AlmostLCA 的层级公平比较，也没有按"每层实际借出量"精确跟踪 Cohort 级额度。第 15 章会标注剩余差距。

## 5.6 Flavor 分配顺序与 Fungibility

Kueue 按 `resourceGroups[*].flavors` 的顺序尝试 Flavor。`flavorFungibility` 控制"当前 Flavor 能通过借用/抢占放下时，要不要继续看下一个"：

```yaml
spec:
  flavorFungibility:
    whenCanBorrow: MayStopSearch      # MayStopSearch（默认）| TryNextFlavor
    whenCanPreempt: TryNextFlavor     # MayStopSearch | TryNextFlavor（默认）
    preference: BorrowingOverPreemption  # alpha: BorrowingOverPreemption | PreemptionOverBorrowing
```

语义：

- `whenCanBorrow: MayStopSearch`：当前 Flavor 能借到就停止搜索；
- `whenCanBorrow: TryNextFlavor`：继续看下一个 Flavor，可能找到完全在 nominal 内的；
- `whenCanPreempt: TryNextFlavor`：当前 Flavor 需要抢占时，先试下一个 Flavor，避免不必要的驱逐；
- 如果某个 Flavor 能**不借用、不抢占**地放下，Kueue 会立即选它，不受上面配置影响；
- 如果所有候选都要借用/抢占，Kueue 先选"只借用、不抢占"的，再按 `preference` 在借用与抢占之间取舍。

这会直接影响：

- 是否优先使用更便宜的 spot；
- 是否在本地 nominal 不足时立即借用，还是先找另一个 Flavor；
- 抢占的范围和次数。

MiniKueue 的简化策略是：

1. 先按顺序找所有 Flavor 的直接可行解；
2. 都不行时，再尝试抢占。

这相当于"先换 Flavor，再抢占"，但没有实现 `whenCanBorrow`/`whenCanPreempt`/`preference`。在"本地 nominal 不足时先借还是先换 Flavor"这类边界场景，结果可能与真实 Kueue 不同。

### 5.6.1 一个能看出差别的例子

CQ 有两个 Flavor，按顺序：

```text
spot:     nominal 4 GPU
ondemand: nominal 8 GPU
```

一个需要 6 GPU 的 Workload：

- `spot` 只能放 4，需要借 2；
- `ondemand` 的 nominal 8 足够，完全不用借。

不同配置的结果：

| 配置 | 选择 | 原因 |
|---|---|---|
| `whenCanBorrow: MayStopSearch`（默认） | `spot` | 借得到就停止搜索，哪怕后面有完全在 nominal 内的 Flavor |
| `whenCanBorrow: TryNextFlavor` | `ondemand` | 继续看下一个，找到不用借的 Flavor |
| `spot` 需要抢占、`whenCanPreempt: TryNextFlavor` | `ondemand` | 先换 Flavor，避免驱逐 |
| `spot` 需要抢占、`whenCanPreempt: MayStopSearch` | `spot` + 抢占 | 接受抢占，不再看下一个 |

这也是"同样的配额，为什么 Kueue 选了更贵的 ondemand"的常见答案：不是配额算错，而是 `flavorFungibility` 在控制搜索停止条件。排障时先看 CQ 的 `flavorFungibility`，再看 Workload 的 `status.admission.podSetAssignments`。

## 5.7 资源组、Flavor 与 ClusterQueue 的约束

- 每个资源只能属于一个资源组。
- 每个 Flavor 只能属于一个资源组（MiniKueue 也做了这个校验）。
- 资源组最多 16 个；所有 Flavor 总数最多 256 个（以当前 API reference 为准）。
- 每个被覆盖的资源在每个 Flavor 上都应该有 nominalQuota；至少要定义 0 才能借用。
- `pods` 是保留资源名，用于限制 Pod 数量；不要在 Pod 的 requests 里写它。
- 没有出现在任何 `coveredResources` 里的资源，不受 Kueue 配额管理。

最后一条容易被忽略：如果你请求了 `nvidia.com/gpu`，但 CQ 没有覆盖它，Kueue 不会为 GPU 做配额判断。它只管理你显式声明的资源。

## 5.8 配额单位与常见错误

### 单位

- CPU：Kubernetes quantity，`1` = 1 core，`500m` = 0.5 core。
- 内存：`Mi`、`Gi` 等。
- GPU/自定义资源：整数。
- `pods`：整数。

MiniKueue 内部用整数：CPU 用 millicore，内存用 MiB，其他用整数单位。这是为了确定性和易于测试。

### 错误 1：忘记 `namespaceSelector`

```yaml
spec:
  # 没有 namespaceSelector
```

真实 Kueue 中 `null` 表示**不允许任何 namespace**。如果 CQ 一直 Inactive/Pending，先检查这里。要允许所有 namespace，写：

```yaml
spec:
  namespaceSelector: {}
```

### 错误 2：以为 borrowingLimit 默认是 0

`borrowingLimit: null` 表示不限制。要禁止借用，显式写：

```yaml
borrowingLimit: 0
```

### 错误 3：以为 lendingLimit 默认是 0

`lendingLimit: null` 表示全部 nominal 都可出借。要保留一部分自用，写：

```yaml
lendingLimit: 4
```

这表示"最多借出 4"，等价于"至少为自己保留 nominal - 4"。

### 错误 4：把同一个 Flavor 放进多个资源组

API 会拒绝。一个 Flavor 只能属于一个资源组。

### 错误 5：用 ResourceQuota 的思维理解 Cohort

Cohort 不是"把多个 namespace 的 quota 加起来"那么简单。它涉及：

- nominal 的归属；
- 借用的上限；
- 出借的上限；
- 总量上限；
- 公平分享；
- 抢占回收。

## 5.9 动态配额编排（DQO）

Kueue 还在演进"动态配额编排"（Dynamic Quota Orchestration，alpha）：

- `CapacityProvider` 报告实际可用容量；
- `DynamicQuotaOrchestrator` 按比例把容量分给一棵 CQ/Cohort 子树；
- 结果写入 `status.effectiveQuotas`，调度器优先使用它，而不是 `spec.resourceGroups`。

这适合云上容量动态变化、多集群管理器超卖等场景。它是 alpha，行为和生产可用性要以目标版本为准。

MiniKueue 实现了一个 DQO-lite：`CapacityProvider` 报告每个 Flavor 的容量，Engine 在每个 tick 按 CQ 的静态 nominal 比例把它分配给引用该 provider 的 CQ，得到 `EffectiveQuotas`。调度器、Cohort 聚合、公平份额和 lendable 计算都使用有效配额，而不是静态 nominal。它的分配公式是：

```text
effective(cq, flavor, r) = providerCapacity(flavor, r) × nominal(cq, flavor, r)
                           / Σ nominal(同 provider 的 CQ, flavor, r)
```

整数除法产生的余数按 CQ 名称顺序分配给最后一个队列，保证确定性。provider 已报告某个 Flavor 但漏报的资源按 0 处理，只有完全没被 provider 报告的 Flavor 才回退静态 nominal。它没有实现真实 DQO 的 CapacityProvider 状态同步、effectiveQuotas 残留清理和容量下降时的驱逐；这些仍是差距（见第 15 章）。运行 `go run ./cmd/minikueue -scenario dynamic-quota` 可以看到两个 nominal 4000 的 CQ 在 provider 只报告 6000 时各拿到 3000。

## 5.10 资源变换（Resource Transformations）

有些资源在配额上不应该按 1:1 计费。例如：

- 一张 GPU 卡暴露成多个 MIG 切片；
- 某种加速器按显存/算力折算成标准单位；
- 云厂商的 vGPU 需要换算成物理卡。

Kueue 的 Configuration 支持资源变换。注意 MIG 的"4 个切片 = 1 张卡"需要**小数倍率**（`250m` 张卡/切片），真实 Kueue 用 `resource.Quantity` 可以表达；MiniKueue 的整数模型表达不了这种方向，所以下面的可运行示例改用 HAMi 的"按 vGPU 数量放大显存/算力"。

一个官方风格的 HAMi 例子（**这是 ConfigMap `controller_manager_config.yaml` 的内容，不是可独立 apply 的 CR**；合并流程见 3.6.1）：

```yaml
apiVersion: config.kueue.x-k8s.io/v1beta2
kind: Configuration
namespace: kueue-system
resources:
  transformations:
  - input: example.com/gpumem
    strategy: Replace
    multiplyBy: example.com/gpu
    outputs:
      example.com/total-gpumem: "1"
  - input: example.com/gpucores
    strategy: Replace
    multiplyBy: example.com/gpu
    outputs:
      example.com/total-gpucores: "1"
  - input: example.com/gpu
    strategy: Replace
    outputs: {}
```

语义要点：

- `input` 是 Pod 请求的资源，`outputs` 是配额账本使用的资源；
- `multiplyBy` 通常写成**另一个资源名**：Pod 请求了多少那个资源，就放大多少倍。也可以写成固定整数（教学实现支持，便于小例子）；
- `strategy: Replace` 表示用 outputs 替换 input；`Retain` 表示保留 input 并额外增加 outputs；
- `outputs` 为空 + `Replace` 是官方语义里的"让 Kueue 忽略这个资源"，常用于只把 vGPU 计数当作倍率、不单独计费；
- 每条变换都读**原始 PodSpec**，变换之间不会链式叠加；资源变换发生在调度前，所以看配置时不要被"Pod 明明只请求了 MIG"迷惑；
- 它和 DRA 的 deviceClassMappings 是两条独立的映射路径，不要重复配置。

Pod 请求 `gpu=2, gpumem=1024, gpucores=20` 时，配额账本看到的是：

```text
total-gpumem  = 1024 × 2 = 2048
total-gpucores = 20 × 2 = 40
gpu            = 忽略
pods           = 由 Kueue 自动计数
```

### 5.10.1 MiniKueue 的实现

MiniKueue 在 `api.ResourceTransformation` 中实现同一组语义：

- `Workload.Spec.ResourceTransformations` 在 `Submit` 时从 `Engine.ResourceTransformations` 复制并冻结，之后改配置不会追溯已有预留；
- `Workload.PodSetPerPodUsage` 先合并 DRA 设备，再调用 `ApplyResourceTransformations`，所以资源变换看得到设备映射后的资源，但不会与 DRA 重复计费；
- 所有规则都基于原始 `Requests` 计算，避免规则互相污染；
- `multiplyBy` 优先按整数解析；不是整数时当作资源名，Pod 没请求该资源则倍率为 0；
- 校验拒绝空 input、`pods` 作为 input/output、重复 input、负 output。

运行：

```bash
go run ./cmd/minikueue -scenario resource-transform -ticks 2
```

场景中两个 Pod 各请求 `gpu=2, gpumem=1024, gpucores=20`，CQ 只有 4096 total-gpumem / 80 total-gpucores，因此前两个 Admitted、第三个 Pending。测试见 `TestResourceTransformationsMatchHamiExample`、`TestResourceTransformationsRetainAndFixedMultiplier` 和 `TestResourceTransformationMissingMultiplierContributesZero`。

> 差距：MiniKueue 的变换是集群级配置的简化版，只支持 `Replace`/`Retain`，没有完整的 Configuration API、资源名合法性校验与 webhook 默认化。真实 Kueue 的 `multiplyBy` 字段类型是资源名，固定整数是教学扩展。

## 5.11 本章小结

- `nominalQuota` 是保底额度；`borrowingLimit` 是借入上限；`lendingLimit` 是出借上限；Cohort 总量是最终边界。
- `borrowingLimit` 和 `lendingLimit` 的 null 语义与 0 完全不同。
- Flavor 表达异构资源；资源组决定哪些资源必须同 Flavor；`flavorFungibility` 决定"先借还是先换 Flavor"。
- 隐式 Cohort 只有借用；显式 Cohort 才有共享池、层级、Cohort 级 limit 和权重。MiniKueue 两种形态都支持。
- 层级 Cohort 用树聚合配额，Fair Sharing 用 AlmostLCA 做分层比较；MiniKueue 实现了聚合、共享池和简化的 Cohort 级 borrow/lend limit，但未实现 AlmostLCA。
- 配额只覆盖 `coveredResources`；未覆盖的资源不受 Kueue 管理。
- 资源变换与 DRA 映射会改变"看到多少"和"计费多少"，排障时要先确认映射；MiniKueue 已实现 DRA-lite 映射和资源变换 lite（`Replace`/`Retain` + `multiplyBy` + `outputs`）。
- 下一章会讲队列如何排序、公平分享如何计算，以及为什么"公平"不等于"平均"。

---

[← 上一章：准入生命周期](04-admission-lifecycle.md) | [返回目录](../README.md) | [下一章：排队、调度与公平分享 →](06-scheduling-and-fairness.md)
