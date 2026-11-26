# 第 9 章 抢占与回收

## 9.1 两个动作，两种语义

Volcano 把“赶走别人”拆成两个动作：

| 动作 | 范围 | 触发场景 | 依据 |
|---|---|---|---|
| `preempt` | **同一队列内** | 高优先级作业被低优先级作业挡住 | priority、gang、preemptable |
| `reclaim` | **跨队列** | 队列低于 deserved，其他队列超额 | proportion/capacity 的 overused/reclaimable |

为什么分开？

- 队列内的优先级是“作业之间”的秩序，用户自己能理解。
- 跨队列的回收是“租户之间”的配额治理，应该由队列份额决定，而不是 Pod 优先级。
- 混在一起会让“高优先级但低配额”的作业抢走其他租户的保底资源。

## 9.2 preempt：队列内优先级抢占

### 9.2.1 触发条件

一个作业成为抢占者（preemptor）通常需要：

1. 处于 Inqueue 且 `Session.JobStarving` 为 true（gang hook 判定 `Waiting + Ready < MinAvailable`；`enableJobStarving` 默认 true，显式设为 false 会让 hook 不注册、作业不会被当作 starving）；
2. 有 Pending 的任务；
3. 优先级高于潜在受害者；
4. 同队列存在可抢占的 Pod；
5. 抢占后 gang 能满足（v1.15 的 gangPreempt 会显式检查）。

### 9.2.2 受害者选择

Volcano 的 preempt action 大致流程：

```text
for preemptor in jobs ordered by JobOrder:
    if not starving: continue
    victims = []
    for task in preemptor.pending_tasks:
        for node in candidate_nodes:
            for pod on node:
                if ssn.Preemptable(preemptor, pod):
                    victims.add(pod)
        if can_place(task, victims):
            statement.Evict(victims)
            statement.Pipeline(task)
    if job.Pipelined >= minAvailable:
        statement.Commit()
    else:
        statement.Rollback()
```

关键点：

- **Preemptable** 由插件决定：priority 比较优先级，gang 决定是否保护 gang 作业。
- **Pipeline** 表示“抢占后预分配”，不是真正绑定；只有整个 gang 能凑齐才提交。
- **Rollback** 保证“抢了但没跑起来”不会发生。

### 9.2.3 受害者排序

常见排序：

1. 优先级低的先；
2. 同优先级，创建时间晚的先（更“年轻”的作业让位）；
3. 非 gang 作业/未 Ready 的任务先；
4. 占用资源多、释放后收益大的先。

v1.15 的 gangPreempt 进一步：

- 以作业为粒度评估受害者；
- 优先驱逐“多余副本”（超过 minAvailable 的副本），保护关键副本；
- 驱逐前模拟放置，确认抢占者能启动。

### 9.2.4 preemptionPolicy: Never

PriorityClass 可以设置：

```yaml
preemptionPolicy: Never
```

含义是“这个类的 Pod 不主动抢占别人”，但它仍然可以被更高优先级的 Pod 抢占。Volcano 会在日志中记录 `not eligible due to preemptionPolicy=Never`。

### 9.2.5 抢占的代价

- 被驱逐的作业可能要从 checkpoint 恢复，损失训练进度。
- 频繁抢占会导致抖动（thrashing）。
- 分布式作业的部分驱逐可能破坏一致性，通常配合 `PodEvicted -> RestartJob`。

## 9.3 reclaim：跨队列回收

### 9.3.1 触发条件

reclaim 发生在：

- 队列 Q 的 allocated < deserved（欠服务）；
- 队列 R 的 allocated > deserved（超额）；
- R 的 `reclaimable=true`；
- Q 有 Inqueue 且需要资源的作业。

### 9.3.2 回收边界

回收的目标是让 R 回到 deserved 附近：

```text
while R.allocated > R.deserved: evict one victim
```

但受害者是整 Pod，最后一步可能**越过** deserved。例如 R 有 8 卡、deserved 4 卡，唯一受害者是 8 卡 Pod；驱逐后 R 直接变成 0。因此实现必须在每次驱逐前额外检查：

```text
R.allocated - requests(victim) >= R.guarantee
```

不满足就换一个更小的受害者，或放弃本次回收。这样保证的是“不击穿 guarantee”，而不是“精确停在 deserved”。MiniVolcano 的 reclaim 实现了这个检查，并有 `TestReclaimNeverBreaksGuarantee` 回归测试。

### 9.3.3 与抢占的区别

| | preempt | reclaim |
|---|---|---|
| 范围 | 同队列 | 跨队列 |
| 依据 | Pod/Job 优先级 | 队列 deserved/overused |
| 受害者 | 低优先级作业 | 超额队列的 Pod |
| 目的 | 让高优先级作业跑 | 让欠服务队列拿回份额 |
| 常见配置 | `preempt` action | `reclaim` action |

一个作业可能同时被两者影响：高优先级作业先 preempt 同队列的低优先级作业；如果还不够，reclaim 可能从其他队列回收。

## 9.4 一个完整例子

集群：8 GPU。队列 A、B 权重 1:1，guarantee 各 2。

初始：

```text
A: 8 GPU（a-job，优先级 0）
B: 0 GPU
```

B 提交 b-job（4 GPU，优先级 0）：

1. enqueue：B 的 allocated 0 < deserved 4，允许 Inqueue。
2. allocate：无空闲，失败。
3. preempt：b-job 在 B 队列内没有受害者（A 不同队列），失败。
4. reclaim：B 欠服务，A 超额（8 > 4），回收 A 的 4 个 Pod；b-job 获得 4 GPU。
5. 结果：A 4、B 4。

如果 A 的 job 优先级是 100，b-job 是 0：

- reclaim 仍然可能发生，因为跨队列依据是份额，不是 Pod 优先级；
- 但某些实现会保护高优先级作业，具体取决于插件参数与版本。

如果 A 的 `reclaimable=false`：

- B 无法回收 A 的资源，只能等 A 完成；
- 这是“生产队列不让位”的配置，适合延迟敏感业务。

## 9.5 重调度：shuffle、rescheduling 与碎片整理

### 9.5.1 为什么需要“不抢优先级”的驱逐

preempt 和 reclaim 都回答“谁该让位”。但还有一类问题与优先级无关：

- 两个节点各用 50%，一个 8 卡 gang 却因为碎片无法启动；
- 一批小作业散落在 10 个节点，缩容时无法腾空任何节点；
- 某个节点利用率长期偏低，而其他节点接近饱和。

这时需要的是**碎片整理**，不是抢占。Volcano 用 `shuffle` action + `rescheduling` 插件表达这类需求。

### 9.5.2 shuffle 的执行模型

上游 Volcano 的 shuffle 是一个**纯驱逐** action：

```text
shuffle.Execute(ssn):
    victims = ssn.VictimTasks()       # 插件提名
    for v in victims:
        ssn.Evict(v)                  # 直接驱逐，失败记日志
```

关键点：

- **shuffle 不选节点**：它只负责驱逐，下一轮 `allocate` 决定 Pod 去哪；
- **shuffle 不比较优先级**：受害者由插件策略决定；
- **shuffle 需要重打包策略配合**：如果 allocate 的方向与驱逐方向相反，Pod 会被放回原处，形成抖动；
- **上游没有 Statement 回滚**：驱逐是逐条执行的，失败不会整体撤销。MiniVolcano 用 `Statement` 包了一层，并增加了“目标节点严格更优”的干跑检查，这是教学实现的安全增强，不是上游行为。

### 9.5.3 rescheduling：lowNodeUtilization 与碎片整理

先纠正一个常见误解：上游 `lowNodeUtilization`（LNU）的方向是**从高利用率节点驱逐到低利用率节点**，用来做负载均衡；它不是“把半满节点清空、装箱到另一个节点”。

LNU 的判定：

```text
1. 存在“低利用率节点”（util < thresholds）
2. 存在“高利用率节点”（util > targetThresholds）
3. 从高利用率节点选受害者，驱逐到其回落到 targetThresholds
4. 下一轮 allocate 把 Pod 分散到低利用率节点
```

因此 LNU 通常与 `nodeorder`/spread 类打分配合，而不是与 binpack 配合。

真实 Volcano 的配置形态（参数名以目标版本为准）：

```yaml
- name: rescheduling
  arguments:
    interval: "5m"
    strategies:
      - name: lowNodeUtilization
        params:
          thresholds: {cpu: 20, memory: 20, pods: 20}
          targetThresholds: {cpu: 50, memory: 50, pods: 50}
```

MiniVolcano 实现两种策略：

| 策略 | 方向 | 配合的打分 | 用途 |
|---|---|---|---|
| `lowNodeUtilization`（默认） | 高于 `targetThreshold` → 低于 `threshold` | `nodeorder`/spread | 负载均衡 |
| `fragmentation`（教学） | 低于 `threshold` → 高于源节点 | `binpack` | 碎片整理/装箱 |

`fragmentation` 不是上游策略。它存在的目的是演示“碎片 → 疏散 → 重打包”的完整链路；场景 `shuffle` 用的就是它：

```yaml
- name: rescheduling
  arguments:
    strategy: "fragmentation"
    threshold: "0.6"
```

Mini 还要求“目标节点分数严格高于源节点”，否则拒绝驱逐。没有这个检查，binpack 会把 Pod 放回原节点，形成每 tick 驱逐一次的活锁。

### 9.5.4 组合语义与顺序

- `shuffle` 通常放在 `allocate` **之后**：本轮先正常分配，再驱逐，下一轮重打包；
- 如果放在 `allocate` 之前，同一轮就能重打包，但要求受害者不会被原样放回；
- `rescheduling` 的 `VictimTasksFn` 按 tier 短路：第一个返回非空结果的 tier 生效，后面的 tier 不再参与；
- `pdb` 在上游会参与受害者约束；Mini 不把它做成 `VictimTasksFn`，而是注册 `DisruptableFn`，在 shuffle、preempt、reclaim 三条路径上统一过滤。这样它不需要和 `rescheduling` 争同一个 tier，也不会被短路规则绕过。
- 同一 tier 的多个供应商会被合并；Mini 在 shuffle action 里再做一次合并后的 minAvailable 校验，避免“每个供应商都以为只驱逐一个安全，合起来不安全”。

### 9.5.5 生产风险

1. **重启策略放大影响**：如果作业配置了 `PodEvicted -> RestartJob`，shuffle 驱逐一个 Pod 会导致整个作业重启。对训练作业要极其谨慎。
2. **无放置保证**：shuffle 只保证“驱逐”，不保证“重新跑起来”；节点选择器、拓扑、配额都可能让 Pod 再次 Pending。
3. **抖动**：驱逐后如果被放回同一节点，下一轮又会驱逐。必须让 allocate 的打分方向与策略一致（碎片整理用 binpack，LNU 用 spread/nodeorder）。
4. **PDB 缺位**：没有 PDB/gang 保护时，shuffle 可能一次打散多个关键副本。
5. **指标缺失**：至少监控 `Shuffled` 事件、驱逐数、重调度后 Pending 时长。

一个安全的灰度顺序：

```text
测试集群 → 只对低优先级/可中断队列开启 → 观察一周
→ 加入 PDB 与 minAvailable 保护 → 再扩到训练队列
```

## 9.6 PodDisruptionBudget：驱逐的安全带

### 9.6.1 PDB 是什么

PDB 是 Kubernetes 原生对象，约束“自愿中断”时至少保留多少副本：

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: inference-pdb
  namespace: team-a
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: inference
```

也可以写 `maxUnavailable`。控制器维护 `status.disruptionsAllowed`，表示当前还能中断多少个 Pod。完整示例见 `examples/queues/pdb.yaml`。

### 9.6.2 Volcano 的 pdb 插件

Volcano 的 `pdb` 插件在以下扩展点介入：

| 扩展点 | 作用 |
|---|---|
| `PreemptableFn` | 被抢占的 Pod 若会导致 PDB 违约，则不可抢占 |
| `ReclaimableFn` | 被回收的 Pod 同样受保护 |
| `VictimTasksFn` | shuffle 提名受害者时过滤掉会违约的 Pod |

它读取 informer 缓存中的 PDB，按 selector 匹配 Pod，并考虑 `DisruptedPods`（已经被 API Server 处理、但 PDB 控制器尚未观测到的驱逐）。这避免了同一轮调度里“连续驱逐多个 Pod，每个都以为还有余量”的问题。

MiniVolcano 用一个更小的等价机制实现同样的安全性：`pdb` 插件注册 `PreemptableFn`、`ReclaimableFn` 和 `DisruptableFn`；shuffle 在 Statement 的 trial cache 里逐个重放驱逐，因此第 N 个受害者的检查能看到前 N-1 个已经离开。4 副本、`minAvailable=3` 时，第 1 个允许、第 2 个被拒绝。对应场景 `pdb` 和测试 `TestDisruptableVictimsAppliesPDBCumulatively`。

启用方式（示意）：

```yaml
actions: "enqueue, allocate, backfill, preempt, reclaim, shuffle"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
  - name: pdb
  - name: conformance
```

### 9.6.3 PDB 的边界

PDB 不是“绝对不中断”：

- 它只约束**自愿中断**（eviction/drain），不阻止节点故障、OOM、内核崩溃；
- 它依赖 selector 匹配；标签写错等于没有保护；
- `minAvailable` 设得过高会让抢占/回收找不到合法受害者，表现为“该抢不抢”；
- 多个 PDB 叠加时，取交集，可能比预期更严格；
- Volcano 的 pdb 插件保护的是**调度器发起的驱逐**；集群外部的 `kubectl delete pod` 不走这条路径。

### 9.6.4 一个排障例子

现象：高优先级作业一直 Pending，日志显示 `no victims`。

排查：

```bash
kubectl get pdb -n <ns>
kubectl describe pdb <name>          # 看 disruptionsAllowed
kubectl get podgroup <pg> -o yaml    # 看 minMember
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i pdb
```

常见原因：

- `disruptionsAllowed=0`；
- 受害者标签不匹配 PDB selector；
- gang 的 `minAvailable` 与 PDB 叠加后没有合法组合；
- pdb 插件未启用，但用户以为启用了。

## 9.7 抖动与稳定性

抢占/回收最常见的生产问题是抖动：

```text
t1: A 抢 B
t2: B 欠服务，回收 A
t3: A 又抢 B
...
```

缓解：

- 设置合理的 guarantee，避免队列长期低于 deserved；
- 不要让两个队列的优先级/权重频繁变化；
- 给被驱逐作业设置重启退避；
- 监控 `Evicted`/`Reclaimed` 事件频率；
- 使用 gangPreempt，避免“抢了但没跑起来”；
- 对关键队列设置 `reclaimable=false`；
- 对弹性训练/推理作业启用 `cdp`，给刚启动的 Pod 一个冷却窗口（见 9.7.1）。

### 9.7.1 cdp：冷却保护

弹性训练或推理的 Pod 可能“刚启动就被抢占，重启后又立刻被抢占”。这不是优先级算法错了，而是受害者选择只看优先级、不看“这个 Pod 已经稳定运行了多久”。`cdp`（Cooldown Protection）插件补上这个时间维度。

它注册 `PreemptableFn` 与 `ReclaimableFn`，在候选受害者列表上过滤：

```text
如果 Pod 是 Running，且 PodScheduled 条件的 LastTransitionTime + cooldown > now
    → 从候选受害者中移除
其他状态（Pending/未就绪）→ 仍可作为受害者
```

冷却时间由注解给出，可以放在 Job 或 task 的 Pod 模板上：

```yaml
metadata:
  annotations:
    volcano.sh/preemptable: "true"     # 允许被抢占
    volcano.sh/cooldown-time: "600s"   # 启动后 10 分钟内不被选为受害者
```

调度器配置（放在 tier 1，与 priority/gang 同层）：

```yaml
actions: "enqueue, allocate, preempt, backfill"
tiers:
- plugins:
  - name: priority
  - name: gang
  - name: conformance
  - name: cdp
- plugins:
  - name: drf
  - name: predicates
  - name: proportion
  - name: nodeorder
```

边界：

- **只过滤 preempt/reclaim 的受害者**，不阻止节点 drain、spot 回收、kubelet 驱逐或用户手动删除；
- 只对 `Running` 且拿到 `PodScheduled=True` 的 Pod 生效；Pending/未就绪的 Pod 没有冷却保护；
- 使用真实时间（`time.Now()`），不是调度器的 tick；测试时要注入时钟或缩短窗口；
- 冷却窗口太长会让高优先级作业“有资源却抢不动”，窗口太短则退化为原来的抖动。训练作业常用 5–15 分钟，推理副本常用 1–5 分钟。

它和 `volcano.sh/preemptable` 的关系：后者决定“能不能被抢”，`cdp` 决定“什么时候可以被抢”。两个注解都配，才既有抢占弹性又有启动稳定性。

## 9.8 与其他机制的交互

### 9.8.1 与 gang

gang 作业被驱逐一个 Pod 后，可能不再满足 minAvailable，Job 进入 Restarting；PodGroup 可能回到 Inqueue。策略 `PodEvicted -> RestartJob` 会让整个作业重来，避免部分 worker 空转。

### 9.8.2 与拓扑

hard 拓扑作业被驱逐后，重新调度可能找不到同一个域。gangPreempt 会做放置模拟；没有模拟的实现可能反复抢占失败。

### 9.8.3 与 PDB

PDB 的细节见 9.6。这里只强调：Volcano 的 PDB 保护由 `pdb` 插件提供，**不启用插件就没有保护**；不要假设 preempt/reclaim 默认遵守 PDB。生产上要显式启用并用 `kubectl describe pdb` 验证 `disruptionsAllowed` 的变化。

### 9.8.4 与 DRA

DRA 设备（GPU、网卡）被驱逐后，ResourceClaim 的释放与重新分配可能有时延。DRA 队列配额（v1.15）会把 claim 计入队列用量，回收时要考虑 claim 生命周期。

## 9.9 诊断抢占/回收

```bash
# 看事件
kubectl get events --field-selector reason=Evicted
kubectl get events --field-selector reason=Reclaimed

# 看作业状态
kubectl describe vcjob <name>
kubectl get podgroup <name> -o yaml

# 看调度器日志
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i -E 'preempt|reclaim|evict'
```

常见问题：

- **抢了但没跑**：gang 没凑齐，或拓扑不允许；检查是否有 gangPreempt。
- **该抢不抢**：`enablePreemptable=false`、优先级相同、preemptionPolicy=Never、PDB 阻止。
- **反复抢**：配额/权重不合理，或受害者选择不稳定。
- **跨队列不回收**：`reclaimable=false`、队列未超 deserved、没有欠服务队列。

## 9.10 MiniVolcano 的实现

MiniVolcano 的 `preempt` action：

1. 遍历 Inqueue、starving、有 Pending Pod 的作业；
2. 收集同队列、低优先级、不同作业的受害者；
3. 逐个驱逐，直到抢占者能绑定；
4. 用 Statement 暂存驱逐+绑定；
5. 只有 `ready >= minAvailable` **且至少成功绑定了一个 Pending Pod** 才 Commit，否则 Discard；后者防止“驱逐了受害者但抢占者仍没跑起来”。

`reclaim` action：

1. 找 allocated < deserved 的欠服务队列；
2. 找其他 overused 且 reclaimable 的队列；
3. 驱逐受害者，直到受害者队列回到 deserved；
4. 尝试绑定欠服务队列的作业；
5. 成功才 Commit。

`shuffle` action + `rescheduling` 插件：

1. `rescheduling` 注册 `VictimTasksFn`，支持 `lowNodeUtilization`（默认，高利用率 → 低利用率）与 `fragmentation`（教学，半满节点 → 更忙节点）两种策略；
2. 用“仍占用节点的 Pod 数”而不是 `ReadyCount` 做 minAvailable 保护，避免把 Succeeded 副本也算成在跑；
3. `shuffle` 在 action 层再做一次合并后的 gang 校验，并在 Statement 里干跑“驱逐 + 绑定到更优节点”，证明不会原地折返后才提交驱逐；
4. 下一轮 `allocate` 按配置的打分把 Pod 重新放置；
5. `VictimTasksFn` 按 tier 短路，和真实 Volcano 的组合规则一致。

测试覆盖：

- `TestPreemptOnlySameQueue`：跨队列不抢占；
- `TestPreemptSameQueueEvicts`：同队列抢占，总分配不变；
- `TestReclaimStopsAtDeserved`：回收停在 deserved；
- `TestReschedulingVictimsRespectUtilizationAndGang`：低利用率节点提名 + gang 保护；
- `TestReschedulingLowNodeUtilizationDirection`：LNU 从高利用率节点、按 targetThreshold 选受害者；
- `TestShuffleSkipsWhenNoBetterTarget`：没有更优目标时拒绝驱逐，防止活锁；
- `TestShuffleEvictsThenAllocateRepacks`：驱逐后重打包，节点账本守恒；
- 场景 `shuffle`：端到端验证“碎片 → 疏散 → 单节点装满”。

简化：

- 没有 gangPreempt 的“受害者副本分级”；
- PDB 是简化模型：有 selector、minAvailable/maxUnavailable 和累计保护，但没有 `disruptionsAllowed`/Eviction API/健康探测；
- 没有 preemptionPolicy；
- 受害者排序是“优先级升序 + 创建时间降序”，不是完整的 Volcano 策略；
- `rescheduling` 用单一阈值/目标阈值，没有真实 Volcano 的 CPU/内存/Pod 数分维百分比阈值；LNU 也没有完整的 queueSelector/labelSelector。

## 9.11 抢占的成本模型：什么时候不该抢

抢占在调度器眼里只是“驱逐一个 Pod”，在业务眼里可能是“损失 8 小时训练进度”。把成本写出来，才能判断一次抢占是否值得。

### 9.11.1 一次抢占的真实成本

```text
总成本 = checkpoint 丢失的进度
       + 容器重启/镜像拉取/模型加载时间
       + 重新排队等待时间
       + 集群抖动（其他作业被连带驱逐）
       + 运维排查成本
```

不同负载的量级差异很大：

| 负载 | 驱逐一个 Pod 的代价 | 建议 |
|---|---|---|
| 无状态批处理 | 低，重跑即可 | 可以积极抢占 |
| 短时推理副本 | 中，请求会重试 | 配 PDB，限制并发中断 |
| 分布式训练（有 checkpoint） | 高，回滚到最近 checkpoint | 只在必要时抢占，配 `RestartJob` |
| 分布式训练（无 checkpoint） | 极高，可能全部白跑 | 基本不应抢占；用预留/配额隔离 |
| HPC/MPI | 高，进程组一起重启 | 用 gangPreempt，避免部分驱逐 |

### 9.11.2 一个简单的决策规则

对每个抢占者，估算：

```text
收益 = 抢占者等待时间的减少 × 单位时间价值
成本 = 受害者重启成本 + 集群抖动风险
```

如果 `收益 < 成本`，不应该抢占，而应该：

- 让抢占者继续排队（SLA 插件可以提升它的排序）；
- 扩容/借用其他队列；
- 调整配额，避免长期争抢。

调度器本身通常不做这个经济计算；它按优先级和插件规则执行。**成本模型是平台团队配置策略的依据**，不是调度器的内置逻辑。

### 9.11.3 PDB、Eviction API 与节点 drain 的关系

三个概念经常混：

| 机制 | 谁发起 | 是否遵守 PDB | 典型场景 |
|---|---|---|---|
| 调度器 preempt/reclaim | Volcano | 取决于 `pdb` 插件是否启用 | 高优先级作业抢占 |
| Eviction API（`policy/v1`） | 运维/控制器 | 是，检查 `disruptionsAllowed` | 节点维护、drain |
| 直接 `kubectl delete pod` | 人或脚本 | 否 | 紧急止损、调试 |

结论：

- 启用 Volcano 的 `pdb` 插件，调度器发起的驱逐才会参考 PDB；
- `kubectl drain` 走 Eviction API，和 Volcano 插件是两条路径；
- 直接 delete 绕过一切保护，生产上要限制权限；
- PDB 只约束自愿中断，节点故障/OOM/内核崩溃不受它保护。

### 9.11.4 降低抢占伤害的六个手段

1. **checkpoint**：训练框架定期保存，重启从最近点恢复。
2. **PDB**：给推理/在线副本设 `minAvailable`，限制一次能中断多少。
3. **gangPreempt**：先模拟放置，避免“抢了但凑不齐”。
4. **保护关键队列**：`reclaimable=false` 或高 guarantee。
5. **重启退避**：避免被驱逐作业立刻回来又被驱逐，形成抖动。
6. **监控与预算**：把 `Evicted`/`Reclaimed` 速率纳入 SLO，超过阈值就告警。

### 9.11.5 一个反例：为了 1 张卡驱逐 8 卡作业

集群只剩 1 张空闲卡，一个高优先级 1 卡作业到达。调度器发现唯一可抢占对象是一个 8 卡训练 Pod：

```text
驱逐 8 卡 Pod → 释放 8 卡 → 高优先级 1 卡作业运行
受害者损失：8 卡 × 已运行时长 + checkpoint 回滚
收益：1 卡作业提前若干分钟开始
```

除非业务明确要求“这 1 卡必须立刻可用”，否则这是典型的负收益抢占。缓解：

- 用 guarantee/capability 把关键作业隔离，避免它成为唯一受害者；
- 用 PDB 保护大作业；
- 在调度器配置里限制可抢占的队列/优先级；
- 对高优先级作业使用预留或专用节点池，而不是依赖抢占。

## 9.12 本章小结

- preempt 管队列内优先级，reclaim 管跨队列份额。
- 抢占/回收都通过 Statement 保证“要么成功，要么回滚”。
- 受害者选择要考虑 gang、拓扑、PDB、DRA。
- shuffle + rescheduling 解决碎片与低利用率，不是抢占；驱逐后必须有重打包策略配合。
- PDB 是驱逐的安全带，但只有启用 `pdb` 插件才生效。
- 抖动是生产中的头号问题，靠 guarantee、reclaimable、gangPreempt、`cdp` 冷却和监控缓解。
- MiniVolcano 实现了简化但可验证的 preempt/reclaim/shuffle，并覆盖 gang 安全的受害者过滤。

下一章讲节点打分：在通过 predicates 之后，如何选出“最好”的节点。
