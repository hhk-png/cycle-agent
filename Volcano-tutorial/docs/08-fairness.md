# 第 8 章 公平分享：proportion、DRF、fairshare 与 SLA

## 8.1 公平不是一个函数

“公平”至少有四种不同含义：

1. **队列之间按权重分资源**（proportion）。
2. **作业之间按主导资源份额分**（DRF）。
3. **按历史用量补偿**（fairshare）。
4. **按等待时间补偿**（SLA）。

Volcano 把它们拆成不同插件，允许组合。理解每个插件的假设，比记住配置更重要。

## 8.2 proportion：队列级加权公平

proportion 的职责是给每个队列一个 deserved：

```text
deserved_i = total * weight_i / Σ weight
```

然后按 `allocated / deserved` 排序队列：份额低的队列先分配。

举例：8 张 GPU，A 权重 3，B 权重 1：

```text
deserved_A = 6
deserved_B = 2
```

如果 A 已经用了 6，B 用了 0，则 B 的 share=0，A 的 share=1，下一轮先给 B。

### 8.2.1 capability 与 guarantee 的修正

- deserved 超过 capability → 截断；
- deserved 低于 guarantee → 抬高；
- 队列可以借用空闲资源，但超过 deserved 的部分可能被 reclaim。

### 8.2.2 proportion 的局限

proportion 只看队列总量，不看作业形状。一个队列里可能有一个占 90% 资源的大作业和一堆小作业，proportion 不会在作业之间做公平。这是 DRF 的职责。

## 8.3 DRF：主导资源公平

DRF（Dominant Resource Fairness）解决多维资源公平：

```text
job_share = max over resources (allocated_or_demanded / total)
```

每次选择 share 最小的作业。

### 8.3.1 一个例子

集群：8 CPU、8 GPU。

| 作业 | 每任务请求 | 副本 | 主导资源 |
|---|---|---|---|
| CPU-job | 1 CPU | 8 | CPU |
| GPU-job | 1 GPU | 8 | GPU |

如果先跑 CPU-job 4 个任务，它用了 4 CPU，share=0.5；GPU-job 还是 0，所以下一个任务给 GPU-job。最终两者各占一半 CPU/GPU。

如果 CPU-job 每任务要 2 CPU，GPU-job 每任务要 1 GPU：

```text
CPU-job 每个任务 share = 2/8 = 0.25
GPU-job 每个任务 share = 1/8 = 0.125
```

DRF 会优先给 GPU-job 更多任务，直到两者的主导份额接近。这不是“平均分配 Pod 数”，而是“平均分配最紧张的维度”。

### 8.3.2 DRF 的局限

- 只看当前分配，不记得历史；一个作业昨天占了 24 小时，今天仍然和刚来的作业同权。
- 对 gang 作业不友好：一个 gang 要么一次拿很多，要么拿不到，DRF 的平滑性被打断。
- 多维权重需要人为设定；如果某个维度总量为 0，会被跳过。

## 8.4 fairshare：历史用量公平（v1.16）

fairshare 是 v1.16 引入的 Slurm 风格插件：在**同一个队列内**按 namespace 记录**衰减的历史用量**，让“过去用得多”的租户在未来排序中靠后。它补的是 DRF 的时间盲区：DRF 只看当前分配，fairshare 看累计消耗。

核心公式：

```text
usage_t = usage_{t-1} * 0.5^(Δt / halfLife) + current_usage
```

默认半衰期 240 分钟（4 小时）。效果：

- 刚跑完大作业的 namespace，usage 高，下一批作业排序靠后；
- 时间过去，usage 指数衰减，租户重新获得机会；
- 避免“谁先提交谁长期占优”的饥饿。

### 8.4.1 配置

```yaml
- name: fairshare
  arguments:
    fairshare.targetQueues: "gpu-queue"          # 逗号分隔；省略表示所有队列
    fairshare.resourceKey: "nvidia.com/gpu"      # 默认跟踪的资源
    fairshare.resourceKey.retro-queue: "cpu"     # 按队列覆盖
    fairshare.halfLifeMinutes: "240"
    fairshare.enableEnqueueGate: "false"
    fairshare.persistState: "true"
    fairshare.stateNamespace: "volcano-system"
    fairshare.stateConfigMap: "fairshare-usage-state"
    fairshare.flushIntervalSeconds: "30"
```

| 参数 | 默认 | 语义 |
|---|---|---|
| `targetQueues` | 全部 | 只对列出的队列生效 |
| `resourceKey` | `nvidia.com/gpu` | 累计用量的资源维度 |
| `resourceKey.<queue>` | 无 | 单队列覆盖 |
| `halfLifeMinutes` | 240 | 半衰期；越小越“健忘” |
| `enableEnqueueGate` | false | 达到应得份额后阻止 namespace 继续入队 |
| `persistState` | false | 把 usage 写入 ConfigMap，重启后恢复 |
| `stateNamespace` / `stateConfigMap` | `volcano-system` / `fairshare-usage-state` | 持久化位置 |
| `flushIntervalSeconds` | 30 | 刷新间隔 |

表中为简短名；写进 ConfigMap 时统一加 `fairshare.` 前缀（例如 `fairshare.halfLifeMinutes`）。

### 8.4.2 它是“排序 + 可选门控”，不是抢占

fairshare **不注册 PreemptableFn/ReclaimableFn**，所以它不会驱逐正在运行的作业。它只做两件事：

1. 调整 Pending 作业的排序（历史用量低的 namespace 优先）；
2. 可选的 enqueue gate：某 namespace 达到应得份额时，不让新作业进入 Inqueue。

这意味着：

- 已经跑起来的低公平租户不会被“纠正”，只能等它自然结束；
- 如果需要强制回收，仍然依赖 reclaim + 队列份额；
- enqueue gate 打开后，用户会看到“Job 合法但一直 Pending”，排障时要看 fairshare 的 usage/份额，而不是只看 Queue capability。

### 8.4.3 顺序与交互

推荐顺序：

```yaml
tier-1:
  - priority        # 硬优先级永远先
  - gang
  - fairshare       # 历史公平
  - sla
tier-2:
  - drf             # 当前多维公平
  - proportion
```

`JobOrderFn` 的规则是“第一个非 0 的结果胜出”，所以：

- `priority` 放最前：PriorityClass 仍然是硬约束；
- `fairshare` 放 `drf` 前：先补偿历史，再比当前份额；
- 如果把 `fairshare` 放在 `drf` 后面，DRF 几乎总会先返回非 0，fairshare 形同虚设。

### 8.4.4 一个数值例子

两个 namespace 共享一个 8 卡队列，半衰期 4 小时：

```text
t=0   A 用了 8 卡 × 2h = 16 GPU-hour，B 用了 0
t=2h  usage_A = 16 * 0.5^(2/4) ≈ 11.3，usage_B = 0
      B 的新作业排在 A 前面
t=4h  usage_A ≈ 8，A 重新获得机会
```

注意单位是“资源 × 时间”，不是“当前占用”。这就是它能区分“短时间占满”和“长期占用”的原因。

### 8.4.5 运维要点

- 开启 `persistState` 前确认 scheduler 对 ConfigMap 有写权限；
- 多调度器/分片场景下，每个 scheduler 的本地 usage 可能不同，持久化 ConfigMap 是共享点，但要避免并发写冲突；
- 监控 `namespace_share` 与 `namespace_decayed_usage` 两个 gauge（名称以版本为准）；
- 半衰期是业务参数：太大接近“永久记账”，太小接近“只看当前”；
- fairshare 只按 namespace 统计，不按用户；需要 per-user 公平时要额外建模。

与 DRF 的区别：

| | DRF | fairshare |
|---|---|---|
| 时间尺度 | 当前瞬间 | 历史 + 衰减 |
| 公平对象 | 作业/队列的当前份额 | namespace 的累计资源使用 |
| 统计维度 | 多维资源 | 单资源（可配置） |
| 是否驱逐 | 否 | 否 |
| 典型问题 | 多维资源分配 | 长期饥饿/先到先得 |

## 8.5 SLA：等待时间预算

SLA 插件解决“作业等了太久”：

```yaml
metadata:
  annotations:
    volcano.sh/sla-waiting-time: "1h"
```

- 作业等待超过预算后，被标记为 overdue；
- 排序时优先；
- 某些实现会 pipeline 过期作业，让它更容易被部分调度；
- 超过 SLA 会记录事件，便于运营。

SLA 不是“保证 1 小时内一定跑完”，而是“尽量不让它继续等”。真正的完成时间还取决于资源和优先级。

## 8.6 四个插件如何组合

推荐层次：

```yaml
tiers:
- name: tier-1
  plugins:
  - name: priority          # 硬优先级
  - name: gang              # gang 合法性
  - name: sla               # 等待时间补偿
  - name: fairshare         # 历史用量补偿
  - name: conformance
- name: tier-2
  plugins:
  - name: drf               # 当前多维公平
  - name: predicates
  - name: proportion        # 队列份额
  - name: nodeorder
  - name: binpack
```

排序逻辑：

1. 先比优先级（高者先）；
2. 同优先级，比 SLA/fairshare（谁等得久/用得少）；
3. 再比 DRF（当前份额）；
4. 最后比创建时间/名字（稳定排序）。

注意：proportion 影响的是**队列顺序**，DRF 影响的是**作业顺序**。两者不是同一层。

## 8.7 一个多租户的完整例子

集群：64 GPU。

| 队列 | weight | guarantee | capability | deserved |
|---|---|---|---|---|
| prod | 2 | 16 | 48 | 21.3 |
| train | 3 | 8 | 64 | 32 |
| dev | 1 | 0 | 16 | 10.7 |

场景：

1. prod 空闲，train 提交 64 卡作业，用到 64（借了 prod 与 dev 的份额）。
2. prod 提交 32 卡作业，reclaim 从 train 收回 32 卡，train 降到 deserved 32，prod 得 32。
3. prod 想再要 16 卡（到 capability 48），但 train 已到 deserved 32，reclaim 不能继续；prod 只能等 train 完成，或由管理员调整配额。跨队列不靠 preempt，preempt 只在同队列内按优先级工作。
4. dev 提交小作业，稳态份额约 10.7 卡、上限 16 卡；如果 prod/train 占满，dev 等待，guarantee=0 意味着没有保底。

这个例子说明：

- weight 决定稳态份额；
- guarantee 决定最坏情况；
- capability 决定上限；
- 抢占/回收决定动态调整。

## 8.8 MiniVolcano 的实现

MiniVolcano 提供四个插件：

- `proportion`：deserved = total * weight/sum，capability 截断，guarantee 抬底；队列顺序按 allocated/deserved。
- `drf`：share = dominant(allocated + pending demands) / total；作业顺序按 share 升序。
- `fairshare`：按 tick 衰减，usage += share * Δt；队列/作业顺序按 usage 升序。
- `sla`：按 `volcano.sh/sla-waiting-time`（tick 单位）标记 overdue；overdue 优先。

场景验证：

- `proportion`：3:1 权重，8 卡，A 得 6、B 得 2。
- `drf`：2 CPU 集群，已有 1 CPU 被占，DRF 选择需求更小的 B，而不是先创建的 A2。
- `fairshare`：A 先跑满 4 卡，A 的第二个作业与 B 的作业竞争，历史用量让 B 先拿。
- `sla`：J3 等待超过 1 tick，在 J1 完成后优先于更早创建的 J2。

简化：

- proportion 不做剩余份额再分配；
- DRF 使用“已分配 + 待分配请求”作为需求，而不是纯历史；
- fairshare 的 halfLife 以 tick 计，不是小时；
- SLA 只做排序，不做 pipeline。

## 8.9 公平性的度量

不要只看“是否按配置分配”。建议监控：

- **队列份额偏差**：`allocated / deserved` 的长期分布；
- **等待时间分位数**：P50/P95/P99，按队列/优先级；
- **饥饿作业数**：等待超过 SLA 的作业；
- **抢占/回收次数**：频繁回收说明份额配置或容量不足；
- **GPU 利用率**：公平但利用率低，说明碎片或 gang 太大。

公平性和利用率经常冲突。一个完全公平的调度器可能把 GPU 切得很碎，导致大 gang 永远凑不齐。生产配置要在两者之间找平衡。

## 8.10 本章小结

- proportion 管队列份额，DRF 管作业的多维当前份额，fairshare 管历史用量，SLA 管等待时间。
- 它们通过 tier 组合，顺序决定语义。
- 公平不等于高利用率；gang 和碎片会让公平调度变复杂。
- MiniVolcano 用四个小插件复刻了核心行为，并在场景中验证。

下一章讲抢占与回收：当公平分配不够时，如何安全地“抢”。
