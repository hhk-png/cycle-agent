# 第 26 章 调度语义的形式化与不变量

## 26.1 为什么要形式化

前面的章节讲“Volcano 怎么做”。这一章换一个问题：**它做的每一步，凭什么算对？**

调度器的 bug 有一个共同特征：它们不会让程序崩溃，只会让集群在某个角落慢慢失去平衡。比如：

- 一个队列的 capability 是 8 卡，实际却跑到了 9 卡；
- gang 作业回滚后，节点账本没有回滚干净，留下 2 卡的“幽灵占用”；
- 回收把受害者队列压到 guarantee 以下，第二天早上高优作业起不来；
- shuffle 每次驱逐同一批 Pod，集群每隔五分钟抖动一次；
- 两次相同输入的调度结果不同，测试只能碰运气。

这些都不是“功能缺失”，而是**不变量被破坏**。形式化的价值不是写论文，而是把“对”变成可以检查的命题，再把命题变成测试和监控。

本章给出六组定义：资源模型、gang 事务、队列公平、借用与回收、抢占与重调度、拓扑与装箱；最后落到 13 条可执行不变量。

## 26.2 资源模型：偏序而不是标量

### 26.2.1 资源向量与偏序

一个 Pod 的请求是一个多维向量：

```text
r = (cpu, memory, gpu, ...) ∈ ℕ^k
```

资源比较不是全序，而是**分量偏序**：

```text
a ≤ b  ⇔  ∀i: a_i ≤ b_i
```

这带来两个直接后果：

1. 两个 Pod 可能**不可比较**：A 的 CPU 多、GPU 少，B 反过来。调度器不能说“A 比 B 大”。
2. “剩余资源最多”的节点不一定能放下某个 Pod：总空闲大，但某一个维度不够。

Kubernetes 的 `ResourceList` 和 Volcano 的 `Resource` 都采用这个偏序。MiniVolcano 用 `LessEqual` 实现节点容量检查，用 `WithinLimits` 实现**部分约束**的 capability 检查。两者不能混用：

```text
LessEqual(r, limit)     # limit 中未出现的维度按 0 处理
WithinLimits(r, limit)  # 只检查 limit 中出现的维度
```

队列 capability 的语义是后者。只写 `nvidia.com/gpu: 8` 的队列不应该被解释为“CPU 上限为 0”。MiniVolcano 开发期就因为混用这两个函数，出现过“所有作业都无法入队”的故障（第 25 章复盘）。

### 26.2.2 主导份额

DRF 需要把多维资源压成一个可比较的标量。定义总容量 `C`，作业 `j` 的分配 `A_j`，则主导份额为：

```text
D_j = max_i ( A_j[i] / C[i] )
```

主导资源是取得最大值的维度。DRF 的排序规则是 `D_j` 小者优先。

一个例子。集群有 8 CPU、4 GPU：

| 作业 | CPU | GPU | CPU 份额 | GPU 份额 | 主导份额 |
|---|---:|---:|---:|---:|---:|
| A | 4 | 0 | 0.50 | 0 | 0.50 |
| B | 0 | 1 | 0 | 0.25 | 0.25 |

B 的主导份额更小，所以下一个 CPU 应该给 B，即使 B 当前没有用 CPU。这就是 DRF 与“按 CPU 使用率排序”的区别。

注意 DRF 的“需求”口径。Volcano 的 drf 插件在排序时会把 pending Pod 的请求计入 demand；MiniVolcano 的 `drf` 同样使用 `已分配 + pending 请求`。如果只用已分配，刚提交的大作业会因为份额为 0 而永远排在最前，形成饥饿。

### 26.2.3 资源守恒

对任意节点 `n`：

```text
Allocated(n) = Σ_{p ∈ Pods(n), p 占用节点} Requests(p)
```

对任意队列 `q`：

```text
Allocated(q) = Σ_{p: p.Queue=q, p 占用节点} Requests(p)
```

这两条看起来是废话，但它们是所有账本 bug 的试金石。MiniVolcano 的随机不变量测试每个 tick 都重算这两条；第 25 章记录的“完成 Pod 清空 NodeName”“Cron GC 不释放节点”都是它们被破坏的例子。

## 26.3 Gang 作为事务

### 26.3.1 定义

一个 gang 作业 `j` 有副本集合 `T_j`，最小成员数 `m_j`。调度结果是一个绑定集合 `B_j ⊆ T_j`。gang 约束是：

```text
|B_j| ≥ m_j  或者  B_j = ∅
```

不允许 `0 < |B_j| < m_j` 的可见状态。

Volcano 的 allocate 不是“逐个 Pod 绑定”，而是：

```text
打开 Statement（事务）
  for task in tasks:
      选择节点，写入 tentative binding
  if ready ≥ minAvailable:
      Commit
  else:
      Rollback
```

因此 gang 的正确性依赖两件事：

1. **事务隔离**：tentative binding 对其他调度决策不可见，除非 commit；
2. **回滚完整**：commit 前任何失败都必须撤销节点账本。

Volcano 用操作日志（operation log）实现；MiniVolcano 用 cache 深拷贝实现。两者的成本模型不同，但正确性条件相同：

```text
Commit 之前，外部世界看不到任何绑定；
Rollback 之后，cache 与事务开始时逐字段相等。
```

### 26.3.2 安全性、活性与饥饿

把 gang 看成事务后，可以区分三种性质：

| 性质 | 命题 | 谁来保证 |
|---|---|---|
| 安全性 | 不存在部分启动的可见状态 | Statement + 单一提交点 |
| 活性 | 资源足够且无更高优先级阻塞时，作业最终运行 | 调度循环 + 公平性 |
| 无饥饿 | 任何作业不会被无限期跳过 | SLA/aging/公平插件 |

安全性是硬约束，必须由实现保证。活性是**条件性**的：Volcano 默认没有老化机制，一个低优先级大 gang 可能长期排队。SLA 插件通过等待时间排序缓解饥饿，但不是严格保证。

这个区分很重要：很多“调度器不工作”的投诉，其实是活性问题（资源碎片、优先级长期压制），不是安全性 bug。安全性 bug 会让账本错；活性问题只会让作业等。

## 26.4 队列公平：三种常见定义

### 26.4.1 Proportion：按权重分容量

设有队列集合 `Q`，权重 `w_q`，集群总容量 `C`。proportion 的应得份额是：

```text
deserved_q = C * w_q / Σ_{p∈Q} w_p
```

再施加两个边界：

```text
deserved_q = min(deserved_q, capability_q)
deserved_q = max(deserved_q, guarantee_q)
```

排序时比较份额：

```text
share_q = max_i ( allocated_q[i] / deserved_q[i] )
```

`share` 小的队列先分配。这个定义有一个容易忽略的细节：分母是 **deserved**，不是 capability。capability 是硬上限，不参与“谁更该先跑”的比较；否则 capability 小的队列会被人为抬高优先级。

数值例子：总容量 8 GPU，三个队列权重 3:2:1：

| 队列 | 权重 | deserved | capability | 实际分配 | share |
|---|---:|---:|---|---:|---:|
| A | 3 | 4 | 8 | 4 | 1.00 |
| B | 2 | 2.67 | 8 | 2 | 0.75 |
| C | 1 | 1.33 | 1 | 1 | 0.75 |

B 和 C 的 share 相同，按名字稳定排序。A 已经达到 deserved，不应再优先获得空闲资源；但如果 B、C 都没有 pending 作业，A 可以借用，直到 capability。

MiniVolcano 用整数资源表示，`8 × 2/6` 和 `8 × 1/6` 会向下截断为 2 和 1；上表的 2.67/1.33 是连续数学值。两者对排序结论一致，但在精确断言里要按整数计算。

### 26.4.2 Max-min fairness 与权重

Max-min fairness 的直觉是“把水倒进池子，先填满低需求的池子，再平分剩余”。加权版本用权重控制注水速度。proportion 在**所有队列都持续有需求**时收敛到加权 max-min；当某些队列需求不足时，剩余容量被其他队列借用。

DRF 则是把 max-min 应用到**主导份额**上。两者的关系可以写成：

```text
proportion: 公平单位是队列，比较 allocated/deserved
DRF:        公平单位是作业，比较 max_i(allocated_i / total_i)
```

它们可以同时启用，但要注意 tier：如果 priority 在 tier 1 且对不同优先级总返回非 0，DRF 根本轮不到。第 7 章讲过这个短路语义。

### 26.4.3 层级 capacity 的递归

capacity 把同样的加权分配递归到队列树。设父队列 `p` 的应得为 `D_p`，子队列 `c` 的权重为 `w_c`，兄弟集合为 `S(p)`：

```text
D_c = D_p × w_c / Σ_{s∈S(p)} w_s
```

有效上限还要沿路径取最小：

```text
cap_eff(c) = min( capability_c, capability_p, capability_root, ... )
```

两条可检查的约束：

```text
Σ_{c∈children(p)} guarantee_c ≤ capability_p
D_c ≥ guarantee_c
```

第一条如果被违反，父队列的容量不可能同时兑现所有子队列的保底；第二条保证子队列的 deserved 不会被自己的 guarantee 击穿。MiniVolcano 的 `capacity` 插件在 `allocatedTree` 中汇总子树用量，并在 `Allocatable` 中同时检查本队列和祖先的 capability。

层级队列还有一个容易忽略的语义：**父队列本身通常不直接承载作业**。它的 `allocated` 是子树之和，而不是自己 Pod 的分配。把父队列当作普通队列往里提交作业，会让父子账本混在一起，排障时非常难拆。

### 26.4.4 Fairshare：历史用量

fairshare 引入时间维度。设半衰期 `H`，上次更新时间差 `Δt`，历史用量 `U`，当前份额 `s`：

```text
U ← U * 0.5^(Δt / H)
U ← U + s * Δt
```

排序按 `U` 升序。半衰期的含义是：`H` 分钟前的用量权重降到一半。它解决的是“短时间突发”与“长期占用”的不公平：一个队列即使当前分配不高，只要过去长期占满，也应该排后面。

三个插件回答三个不同问题：

| 插件 | 时间尺度 | 公平单位 | 典型用途 |
|---|---|---|---|
| proportion | 当前 | 队列 | 租户配额 |
| DRF | 当前 | 作业 | 多资源形状公平 |
| fairshare | 历史 | 队列/namespace | 防止长期霸占 |

## 26.5 借用、回收与不变量

### 26.5.1 借用的定义

队列 `q` 的分配可以超过 deserved，条件是：

```text
allocated_q ≤ capability_q
```

超出部分称为借用。借用的前提是其他队列当前不需要；一旦需要，借用方必须可回收。

### 26.5.2 回收的安全性

reclaim 的目标是让被欠的队列回到 deserved。它每次驱逐前检查受害者队列 `v` 是否 `Overused`：

```text
Overused(v) ⇔ allocated_v > deserved_v
```

但“驱逐前超额”并不足以推出“驱逐后仍不低于 deserved”。受害者是整 Pod 的，一个 8 卡 Pod 可以把队列从 8 卡直接打到 0。正确的实现必须额外检查：

```text
allocated_v - requests(victim) ≥ guarantee_v
```

不满足就跳过这个受害者，换一个更小的；如果都不满足，本次回收失败。这样能保证的不变量是：

```text
回收后 allocated_v ≥ guarantee_v
```

而不是 `≥ deserved_v`。后者在 Pod 粒度下不成立：队列可能从“略高于 deserved”被一个大 Pod 打到 deserved 以下，只要没跌破 guarantee 就是允许的。

注意它依赖三个实现细节：

1. `deserved` 必须先被 guarantee 抬高；
2. 每次驱逐后重新判断，而不是一次性算好“要驱逐几个”；
3. 驱逐前检查“post ≥ guarantee”，否则大 Pod 会造成保底击穿。

MiniVolcano 的 reclaim 还要求 `victim.Priority ≤ 抢占者优先级`，并且 PDB/gang 保护优先。这些是额外约束，不改变上面的主不变量。

### 26.5.3 双重计算

借用/回收最容易出的 bug 是“同一份资源被算两次”：

- 预留扣减了空闲，但 Bind 时又扣了一次；
- 驱逐释放了节点，但没有更新队列统计；
- 预占（pipeline）既算 allocated 又算 idle。

统一的口径是：**只有占用节点的 Pod 计入 allocated；预留只影响 IdleFor，不影响 allocated。** MiniVolcano 的 `IdleFor` 只从 idle 中扣除“预留中尚未被 owner 使用的部分”，不修改 `node.Allocated`，正是为了避免双重计算。

## 26.6 抢占：最小驱逐集与贪心

### 26.6.1 问题定义

给定一个 pending 的 gang 作业 `j`，节点集合 `N`，每个节点上的受害者集合 `V_n`，要求选择 `S ⊆ ∪V_n`，使得：

1. `j` 的每个 Pod 都能在驱逐 `S` 后找到节点；
2. `S` 中每个 Pod 都可抢占（优先级更低、同队列、PDB 允许、gang 安全）；
3. `|S|` 或 `Σ resources(S)` 尽量小。

这是带约束的集合覆盖/背包问题的变种，一般是 NP-hard。Volcano 和 MiniVolcano 都用贪心：

```text
按 TaskOrder 遍历 pending Pod
  找当前最优节点
  如果放不下，按受害者排序依次驱逐，直到能放下
  如果仍放不下，放弃这个 Pod
所有 Pod 都放置成功且 ready ≥ minAvailable 才 commit
```

贪心的质量取决于受害者排序。常见顺序是“低优先级优先、新创建优先、同队列优先”。MiniVolcano 使用：

```text
priority 升序 → creationTick 降序 → name 升序
```

### 26.6.2 为什么要事务

如果没有事务，抢占会出现“抢了但没跑”：

```text
驱逐 3 个受害者 → 绑定第 1 个抢占者 → 绑定第 2 个失败 → 作业仍然不能运行
```

此时集群已经损失了 3 个正在工作的 Pod，却没有换来任何收益。Volcano 的 preempt 在 Statement 中完成驱逐和绑定，只有 gang 满足才 commit；MiniVolcano 还额外要求 `evicted > 0 && bound > 0`，避免“没有实际收益的驱逐”。

### 26.6.3 gang 粒度抢占

传统 preempt 是“Pod 级受害者 + 作业级提交”，仍然可能为了一个 Pod 驱逐另一个 gang 的关键成员。v1.15 的 `gangPreempt` 先做放置模拟，确认抢占者 gang 能完整启动才驱逐。它更安全，但计算量更大，也更难处理跨队列。

MiniVolcano 没有实现 gangPreempt，但在 shuffle 中做了类似的“目标可行性干跑”：先证明每个受害者都有严格更优的去处，再提交驱逐。这个检查解决的是活锁，不是抢占收益。

## 26.7 拓扑：树上的约束满足

### 26.7.1 HyperNode 是一棵有序树

HyperNode 的约束可以写成：

```text
tier(parent) < tier(child)
members(leaf) ⊆ Nodes
```

MiniVolcano 在 `AddHyperNode` 和 `ValidateTopology` 中检查：

- 父子 tier 单调；
- 父/子引用存在；
- 无环；
- 叶子成员是真实节点。

这些检查不是形式主义。一个环会让递归展开无限循环；一个反向 tier 会让“选择更高层域”变成“选择更小域”，拓扑作业可能永远放不下。

### 26.7.2 域选择

hard 拓扑的语义是：作业的所有 Pod 必须落在某个 `tier ≤ highestTierAllowed` 的域内。算法分两步：

1. **选域**：枚举该 tier 的所有 HyperNode，计算每个域的可用容量，选择能容纳 `minResources` 的最小域；
2. **限域**：Predicate 只允许域内节点。

这是一个“先粗后细”的约束满足：先选一个足够大的盒子，再在盒子里放 Pod。它不保证全局最优（可能选了一个域，导致后续 Pod 放不下），但保证**如果选中的域容量足够，作业不会跨域**。

soft 模式把“域内”从硬约束变成打分项，允许跨域但扣分。

### 26.7.3 任务角色拓扑

task-topology 是另一类约束：不是“所有 Pod 在同一个域”，而是“某些角色靠近、某些角色远离”。它可以表达为任务对之间的亲和/反亲和图：

```text
affinity(ps, worker)      ⇒ 同节点或同域
anti-affinity(worker, worker) ⇒ 不同节点
```

这是一张带标签的约束图。MiniVolcano 只实现“同节点/不同节点”的简化版本，但组合语义与 HyperNode 相同：Predicate 做硬过滤，NodeOrder 做软偏好。

### 26.7.4 PodGroup 反亲和：对约束

`group-topology-affinity` 把约束从“任务之间”提升到“组之间”。设 PodGroup 集合 `G`，每个组 `g` 有一个标签向量 `L(g)`，规则 `(S, t, w)` 表示“标签匹配 `S` 的组两两不能共享 tier `t` 的域”。对任意两个匹配的组 `g1, g2`：

```text
domain(g1, t) ≠ domain(g2, t)    （required, w = 0）
penalty += w if domain(g1, t) = domain(g2, t)  （preferred, w > 0）
```

两个性质值得检查：

1. **对称性**：规则写在 `g1` 上，`g2` 后调度时也必须被约束；否则“先到先占”会绕过。
2. **传递性不成立**：`g1 ≠ g2` 且 `g2 ≠ g3` 不推出 `g1 ≠ g3`；required 只约束匹配对，不是全序。

MiniVolcano 在 `internal/plugins/group_topology.go` 中按“声明方 + 被选中方”双向评估；这是教学实现的加强，上游按对象逐一评估。形式化时要把“规则属于哪个对象”写清楚，否则很容易把对称性当成默认成立。

## 26.8 装箱、打散与目标函数

节点打分是一个多目标优化：

```text
score(pod, node) = Σ_k w_k * f_k(pod, node)
```

常见目标：

| 目标 | 方向 | 适用 |
|---|---|---|
| 空闲资源最多 | spread | 降低热点、便于突发 |
| 已用资源最多 | binpack | 减少碎片、腾出整机 |
| 资源均衡 | balance | 避免 CPU/内存倾斜 |
| 拓扑距离 | locality | 减少跨交换机流量 |
| 真实利用率 | usage | 避免 request 与 actual 脱节 |

这些目标可能互相抵消。例如 nodeorder 偏好空闲节点，binpack 偏好忙碌节点；两者权重相同就会“打架”。第 10 章的建议是：**先决定这一层要优化什么，再决定启用哪个插件**，不要把所有打分插件都打开。

装箱问题是 NP-hard 的（bin packing 的判定版本是 NP-complete）。Volcano 不求解最优，而是用贪心 + 打分近似。因此“装箱率”永远是一个经验指标，不是定理。

## 26.9 复杂度总表

下表以 `J` 作业数、`T` 每作业任务数、`N` 节点数、`P` Pod 数、`V` 受害者数、`H` hook 数为参数。

| 操作 | 复杂度 | 说明 |
|---|---|---|
| Snapshot | O(N + P + J) | 每轮一次 |
| allocate | O(J × T × N × H) | 主要成本，节点打分占大头 |
| preempt | O(J × T × V × N × H) | 每个 Pod 可能要试多个受害者 |
| reclaim | O(Q² × P) | 简化模型；生产实现更复杂 |
| shuffle | O(V) + 一次 Statement | 驱逐后下一轮重打包 |
| rescheduling | O(N + P) | 每轮计算利用率 |
| 拓扑选域 | O(D × N) | D 为候选域数 |
| 深拷贝事务 | O(cluster) | MiniVolcano 的取舍 |

生产 Volcano 用操作日志把事务成本从 O(cluster) 降到 O(操作数)，用增量 cache 降低快照成本，用分片降低单调度器的 `N`。这些优化的前提都是：**语义不变，只换数据结构**。

## 26.10 十三条可检查的不变量

下面每一条都可以写成断言、测试或 Prometheus 告警。

| # | 不变量 | 检查方式 | MiniVolcano 对应 |
|---:|---|---|---|
| I1 | 节点 allocated = 占用 Pod 请求之和 | 每 tick 重算 | 随机不变量 |
| I2 | 队列 allocated = 队列内占用 Pod 请求之和 | 每 tick 重算 | 随机不变量 |
| I3 | 节点不过载、不为负 | `allocated ≤ allocatable` | 随机不变量 |
| I4 | Running 的 gang 作业 ready ≥ minAvailable | 状态机检查 | 随机不变量 |
| I5 | 未提交事务不改变可见 cache | 深拷贝/操作日志测试 | `TestStatementDiscard` |
| I6 | 队列不超 capability | 分配后检查 | `proportion` 场景 |
| I7 | reclaim 后受害者队列 ≥ guarantee | 驱逐前检查 post | `reclaim` 场景 + `TestReclaimNeverBreaksGuarantee` |
| I8 | 抢占者优先级 > 受害者 | Preemptable 检查 | `priority` 场景 |
| I9 | PDB 保护下可用副本 ≥ minAvailable | 驱逐前检查 | `pdb` 场景/测试 |
| I10 | hard 拓扑作业不跨域 | 绑定后检查 | `topology` 场景 |
| I11 | 相同输入产生相同输出 | 两次运行比较 | `TestDeterminism` |
| I12 | 生命周期动作只触发一次 | 事件计数 | `TestTaskCompletedFiresOnce` |
| I13 | 角色门槛生效时，Running 作业每个角色 ready ≥ minTaskMember | 状态机/分配检查 | `task-gang` 场景 + `TestGangTaskMinMemberReadiness` |

这 13 条不是全部，但覆盖了资源、gang、公平、抢占、拓扑和控制器六个面。一个调度器如果这 13 条都成立，仍然可能有性能问题，但不会“悄悄算错账”。

I13 有一个容易忽略的前提：**角色门槛只在 `minAvailable >= Σ minTaskMember` 时生效**。门槛不生效时，I13 退化为空命题；把它写成“Running 作业必须满足每个角色”会在 `minAvailable` 较小时误报。这也是 6.2.2 强调“生效条件”的形式化表达。

## 26.11 用性质测试找反例

单元测试验证“已知路径正确”，性质测试验证“所有路径都不违反不变量”。两者的分工：

```text
单元测试：给定输入 → 断言输出
性质测试：随机输入 → 断言不变量
场景测试：真实拓扑 → 断言端到端行为
```

MiniVolcano 的 `TestRandomizedInvariants` 用 12 个 seed，每个 seed 生成 18 个作业、4 个节点、2 个队列，跑 25 tick，每个 tick 检查 I1–I4。它曾经抓到过单测没覆盖的问题，也曾经“假绿”过——因为断言本身编码了错误行为。

写性质测试的三条经验：

1. **不变量要独立于实现**。不要用 `QueueAllocated()` 去验证 `QueueAllocated()`；要遍历 Pod 重新求和。
2. **随机要可复现**。固定 seed，失败时打印 seed 和 tick。
3. **先让不变量失败一次**。故意注入一个 off-by-one，确认测试会红；否则你无法判断它是真绿还是假绿。

## 26.12 从不变式到监控：把 I1–I13 变成告警

不变量如果只存在于论文里，不会阻止事故。把它们分成三类，分别落地：

| 类型 | 例子 | 落地方式 | 频率 |
|---|---|---|---|
| 可即时计算 | I1/I2/I3 账本守恒 | 调度器自检、测试、审计 Job | 每 Session/每 tick |
| 可观测推导 | I6 队列不超 cap、I7 回收后 ≥ guarantee | Prometheus 指标 + 告警 | 每分钟 |
| 只能测试 | I5 未提交事务不可见、I11 确定性 | 单测/随机不变量/回放 | CI |

### 26.12.1 一张映射表

| 不变量 | 线上指标/查询（示意） | 告警条件 | 处理 |
|---|---|---|---|
| I1 节点账本 | `node_allocated - sum(pod_requests)` | 差值 ≠ 0 持续 5m | 立即排查，可能是释放遗漏 |
| I2 队列账本 | `queue_allocated - sum(queue pod requests)` | 差值 ≠ 0 | 同上 |
| I3 不超卖 | `allocated > allocatable` | 任意一次 | 严重 bug/并发问题 |
| I4 gang 就绪 | `job_ready < minAvailable` 且 phase=Running | 持续 5m | 状态机/绑定不一致 |
| I6 队列上限 | `queue_allocated > queue_capability` | 任意一次 | 配额插件/账本 bug |
| I7 回收底线 | 队列有 Pending 需求，但 `queue_allocated < queue_guarantee` | 持续 10m | 回收实现/配置问题 |
| I8 抢占方向 | `victim.priority >= preemptor.priority` 的事件 | 任意一次 | 优先级插件 bug |
| I9 PDB | `available < pdb.minAvailable` 且驱逐由调度器发起 | 任意一次 | PDB 插件未生效 |
| I10 拓扑 | Pod 的域 ≠ 作业声明的域 | 任意一次 | 拓扑插件/域选择 bug |
| I11 确定性 | 两次回放绑定序列 diff | 非空 | 打分/遍历顺序问题 |
| I12 生命周期 | 同一事件触发多次 | 任意一次 | 控制器幂等 bug |
| I13 角色门槛 | `role_ready < minTaskMember` 且 `minAvailable >= Σ minTaskMember` | 持续 5m | 角色门槛/状态机 bug |

### 26.12.2 为什么有些指标不能直接算

- **I5（事务隔离）**：外部只能看到提交后的状态，无法从指标判断“未提交的绑定是否泄漏”。只能靠单测、代码审计、事件日志。
- **I11（确定性）**：生产环境输入不断变化，无法直接对比；只能在固定回放/测试环境验证。
- **I4（gang 就绪）**：Pod 状态和 PodGroup 状态可能短暂不一致；告警要加“持续 5m”，避免误报。

### 26.12.3 一个自检 Job 的骨架

与其等监控发现账本不平，不如定期跑一个只读审计：

```text
每 5 分钟：
  1. 拉取所有 Node、Pod、Queue、PodGroup
  2. 重算节点/队列 allocated
  3. 与 status/指标比较
  4. 发现差异 → 打点 + 事件 + 告警
  5. 只读，不自动修复（自动修复可能掩盖 bug）
```

真实 Volcano 没有内置这个 Job；平台团队可以用 client-go 写一个几十行的 CronJob。它比“等用户报 Pending”更早发现账本问题。

### 26.12.4 告警设计原则

1. **不变量告警优先级高于容量告警**。账本不平是 bug，Pending 多可能只是忙。
2. **每个告警都要能归因到一层**：调度器/控制器/API Server/kubelet。
3. **不要自动“修复”账本**。自动重算会掩盖根因；先告警、再人工确认。
4. **保留现场**：告警触发时保存 Node/Pod/Queue 快照，事后可复算。
5. **测试与线上同源**：单测里的 invariant 函数和审计 Job 共用一份实现，避免两套定义。

## 26.13 排队论：等待时间、利用率与 SLA 的数学

前面的公平性讨论回答“谁先跑”，这一节回答“要等多久”。这两个问题经常被混为一谈：一个完全公平的调度器，在利用率接近 100% 时仍然会让所有人等到崩溃。

### 26.13.1 Little 定律

对任何稳定系统：

```text
L = λ × W
```

- `L`：系统中平均作业数（排队 + 运行）；
- `λ`：到达率（作业/秒）；
- `W`：平均逗留时间（排队 + 服务）。

它不依赖分布，只要求系统稳定。用途：

- 已知到达率和服务时间，估算需要多少并发容量；
- 已知集群容量和平均作业时长，反推可持续的到达率；
- 判断“队列无限增长”还是“只是暂时波动”。

### 26.13.2 利用率与等待

设服务率 `μ`（作业/秒），利用率 `ρ = λ/μ`。M/M/1（泊松到达、指数服务、单服务台）的平均排队时间：

```text
Wq = ρ / (1 - ρ) × E[S]
```

其中 `E[S] = 1/μ` 是平均服务时间。把它写成“等待是服务时间的多少倍”：

| 利用率 ρ | Wq / E[S] |
|---:|---:|
| 0.50 | 1.0 |
| 0.70 | 2.3 |
| 0.80 | 4.0 |
| 0.90 | 9.0 |
| 0.95 | 19.0 |
| 0.99 | 99.0 |

这张表解释了一个常见现象：利用率从 70% 提到 90%，看起来只多了 20 个百分点，但排队时间可能从 2.3 倍服务时间涨到 9 倍。**SLA 越紧，可持续利用率越低。**

### 26.13.3 G/G/1 近似

真实集群不是 M/M/1：到达不是泊松、服务时间不是指数、服务台也不止一个。Kingman 近似给出一个更有用的直觉：

```text
Wq ≈ (ρ / (1 - ρ)) × ((Ca² + Cs²) / 2) × E[S]
```

- `Ca`：到达间隔的变异系数（标准差/均值）；
- `Cs`：服务时间的变异系数。

两个结论：

1. **变异性放大等待**。即使 ρ 不高，`Ca`/`Cs` 很大（例如 gang 作业时快时慢、大作业长尾）也会让等待爆炸。
2. **降低方差和降低利用率同样重要**。把作业形状标准化、减少碎片、限制超大 gang，可能比“加卡”更有效。

### 26.13.4 多服务台与优先级

真实调度器有多个“服务台”（节点/队列），还有优先级。M/M/c 的等待比 M/M/1 低，但公式更复杂；工程上更实用的做法是：

- 按队列/优先级分开测量，不要用一个全局平均值；
- 对高优先级队列单独留 headroom；
- 用 `Wq` 的分位数（P95/P99）而不是均值做 SLO；
- 用实际到达/服务分布做离散事件模拟，而不是硬套公式。

### 26.13.5 SLA 预算怎么定

一个可操作的流程：

```text
1. 测 4 周的到达率 λ、服务时间分布 E[S]、P95/P99
2. 算当前 ρ = λ / μ；如果 ρ ≥ 0.9，先扩容或限流
3. 用 M/M/1/Kingman 估算目标 ρ 下的 Wq
4. 把 Wq 与业务可接受的等待预算比较
5. 如果预算不够：加容量、分队列、提优先级、降方差
6. 上线后持续测 P95/P99，按季度重算
```

注意：SLA 插件只能改变“谁先跑”，不能改变 `ρ`。当 `ρ ≥ 1` 时，任何排序算法都无法让队列稳定；这时需要的是容量或准入，而不是更复杂的公平插件。

### 26.13.6 与 gang、碎片的关系

gang 作业让服务时间分布更“块状”：

- 一个大 gang 到达，可能一次占满半个集群；
- 碎片让“资源够但放不下”，服务时间被拉长；
- 抢占/重启让服务时间出现重尾。

这些都会增大 `Cs`，从而放大 `Wq`。所以第 6、9、10 章的碎片、抢占、装箱不是“优化项”，它们直接进入排队模型。容量规划时要把“最大可调度 gang”作为一个独立指标，而不是只看总空闲 GPU。

### 26.13.7 一个数值例子

集群有 100 张 GPU，平均作业需要 10 张、运行 2 小时：

```text
μ = 100/10/2 = 5 作业/小时
λ = 4 作业/小时 → ρ = 0.8
Wq ≈ 4 × 2h = 8h（M/M/1 近似，实际会因多服务台/分布不同而偏离）
```

如果把到达率提到 4.5（ρ=0.9），`Wq` 近似 18 小时。**多 12.5% 的负载，等待翻倍还多。** 这就是为什么平台在 80% 利用率时就应该开始规划扩容，而不是等到 95%。

## 26.14 本章小结

- 资源是多维偏序，capability 是部分约束，不能用全序比较替代。
- gang 的本质是事务：未提交不可见，回滚必须完整。
- proportion、capacity、DRF、fairshare 回答四个不同层次/尺度/单位的公平问题。
- 回收的安全性来自“每次驱逐后重新检查 Overused”。
- 抢占是带约束的贪心，事务保证“抢了就能跑”。
- 拓扑是树上的约束满足，先选域再选节点。
- 装箱是 NP-hard，打分只是启发式。
- 排队论给出利用率的非线性代价：ρ 从 0.7 到 0.9，等待可能从 2.3 倍服务时间涨到 9 倍；SLA 越紧，越要留 headroom。
- 13 条不变量可以落地为测试、监控和告警。

下一章把这些语义变成一组可以照着做的实验。
