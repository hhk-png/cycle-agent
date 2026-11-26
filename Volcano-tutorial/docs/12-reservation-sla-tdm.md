# 第 12 章 预留、SLA、TDM 与混部

## 12.1 先澄清：Volcano 没有 Reservation CRD

很多资料把“资源预留”画成一个 `Reservation` 对象。Volcano 的核心 API 里**没有**这样的 CRD。它的预留能力由三部分组成：

1. **Queue 的 `guarantee`**：队列维度的长期保底；
2. **reservation 插件 + `elect`/`reserve` action**（v1.1 时代）：作业维度的短期预留；
3. **SLA 插件**（v1.2+）：用等待时间预算替代部分预留语义。

`reserve` action 后来被标记为废弃，推荐用 SLA 插件。不同版本的 action 名称与行为有差异，升级时要看 release notes。

本教程的 MiniVolcano 里有一个 `Reservation` 结构，那是**教学抽象**，用来演示“在时间窗内锁定节点资源”的语义，不代表真实 Volcano API。第 24 章会把它列为差异。

## 12.2 Queue guarantee：最基础的预留

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: prod
spec:
  guarantee:
    cpu: "32"
    memory: 128Gi
    nvidia.com/gpu: "8"
```

语义：

- 这 8 张 GPU 不会被其他队列的**回收**拿走；
- 但空闲时，其他队列可以**借用**（如果 capability 允许）；
- 当 prod 需要时，reclaim 会把借用者赶到 deserved 线以下，但不会击穿 prod 的 guarantee。

guarantee 是“最坏情况下的保底”，不是“独占”。要独占，需要把 capability 也设为 guarantee，并关闭 reclaimable。

## 12.3 作业级预留（reservation 插件）

历史设计的目标：给一个即将到来的作业预留节点资源，避免它被其他作业挤走。

大致机制：

- `elect` action 选出一批节点；
- `reserve` action 在这些节点上锁定资源；
- reservation 插件让其他作业看不到这些资源；
- 目标作业到达后使用预留。

现实中的问题：

- 预留会降低利用率；
- 预留过期/作业取消后的清理复杂；
- 与 gang/拓扑/抢占的交互多；
- 维护成本高。

因此 Volcano 后来更推崇 Queue guarantee + SLA，而不是细粒度作业预留。

## 12.4 SLA：用时间换确定性

```yaml
metadata:
  annotations:
    volcano.sh/sla-waiting-time: "1h"
```

SLA 插件：

- 记录作业等待时间；
- 超过预算 → overdue；
- overdue 作业在排序中优先；
- 某些版本会 pipeline 过期作业，提高其被部分调度的概率；
- 产生事件，便于 SLO 统计。

SLA 的价值：

- 不需要预先锁定资源；
- 对“必须在一定时间内开始”的作业提供软保障；
- 与优先级、fairshare 组合，能缓解饥饿。

局限：

- 不是硬保证；资源真不够时仍然等；
- 过度使用会让所有作业都“紧急”，失去区分度；
- 需要配合监控，否则 SLA 违约无人知晓。

### 12.4.1 SLA 预算怎么定：三个可算的量

SLA 不是“给所有作业设 1 小时”就完了。至少先算三个量：

```text
1. 到达率 λ：每小时新增作业数
2. 服务率 μ：每小时能完成的作业数（受资源与作业时长限制）
3. 排队长度 L：平均同时在等的作业数
```

在稳定状态下，Little 定律给出 `L = λ × W`（W 是平均等待时间）。它不能预测尾部延迟，但能快速判断预算是否离谱。利用率与等待时间的非线性关系（ρ 从 0.7 到 0.9，等待可能从 2.3 倍服务时间涨到 9 倍）见第 26.13 节。

- 若 `λ > μ`，队列会无限增长，SLA 设多少都会违约；先扩容或限流。
- 若 `λ ≈ μ`，等待时间对突发极其敏感；SLA 应留出更大余量。
- 若 `λ << μ`，SLA 主要被大作业的 gang 等待主导，而不是平均负载。

预算的设定建议用“分位数 + 违约率”，而不是单一阈值：

```text
P95 等待 ≤ 30 分钟，且 SLA 违约率 ≤ 5%
```

只设“P95”会被长尾掩盖；只设“违约率”会被大量短作业稀释。两者一起看，才能区分“少数大作业饿死”和“整体变慢”。

SLA 插件通常只做“overdue 优先”，不做资源预留。因此它适合：

- 有明确等待预算、但可以接受软保障的作业；
- 与 fairshare/DRF 组合，缓解低权重队列的长期饥饿；
- 作为 SLO 观测指标，而不是合同里的硬承诺。

## 12.5 TDM：时间维度的复用

TDM（Time-Division Multiplexing）用于“同一批节点在不同时间段给不同 workload”：

- 白天给在线服务；
- 夜间给离线训练；
- 或者反过来，把可撤销节点在特定窗口给批作业。

Volcano 的实现：

- 节点打标签 `volcano.sh/revocable-zone: rz1`；
- 作业注解 `volcano.sh/preemptable: "true"`；
- scheduler config 配置时间窗：

```yaml
- name: tdm
  arguments:
    tdm.revocable-zone.rz1: "1:00-4:00"
    tdm.evict.period: "1m"
```

行为：

- preemptable 作业只能进入 revocable 节点，且必须在窗口内；
- 非 preemptable 作业在窗口内不能使用这些节点；
- 窗口结束后，TDM 会驱逐 preemptable 作业（按 evict.period）。

TDM 与抢占的区别：

| | 抢占 | TDM |
|---|---|---|
| 触发 | 资源不足/优先级 | 时间窗 |
| 范围 | 队列/作业 | 节点池 |
| 依据 | 优先级/份额 | 时钟 |
| 典型场景 | 紧急任务插入 | 日间/夜间混部 |

### 12.5.1 一个日周期的时间线

假设 revocable 节点池 `rz1` 白天给在线服务、夜间给离线训练，窗口是 `20:00–08:00`：

```text
08:00  窗口关闭
       - 新的 preemptable 作业不能进入 rz1
       - 已在 rz1 上运行的 preemptable 作业被 TDM 驱逐（按 evict.period）
       - 在线服务开始使用 rz1，guarantee 生效

20:00  窗口打开
       - preemptable 作业可以进入 rz1
       - 非 preemptable 作业在窗口内不能再使用 rz1
       - 若在线服务仍占用 rz1，需要先缩容/迁移；TDM 不会替在线服务做优雅终止
```

三个工程细节：

1. **驱逐不是瞬时的**：`evict.period` 决定检查频率；训练作业要配合 checkpoint，否则窗口切换等于强制中断。
2. **窗口边界要留缓冲**：在线服务扩容、镜像预热、模型加载都需要时间；把窗口设成“正好 08:00”会在边界抖动。
3. **时区与夏令时**：真实 Volcano 用墙钟，集群跨时区时要在配置层统一；MiniVolcano 用 tick，不涉及这个问题。

## 12.6 混部：把在线和离线放在一起

混部的目标是提高利用率：

- 在线服务通常 requests 高、实际用量低；
- 离线作业可以吃空闲 CPU/内存；
- 但离线不能影响在线的延迟。

Volcano 的混部能力（v1.14 增强）：

- **CPU Burst**：允许容器短时间超过 CPU limit；
- **CPU Dynamic Suppression**：根据在线流量动态压制 BestEffort 的 CPU 配额；
- **Memory QoS（Cgroup v2）**：设置 memory.high/low/min，保护在线；
- **通用 OS 支持**：不再只支持 OpenEuler；
- **自定义超卖资源名**：用扩展资源表达超卖。

配置通常通过 `ColocationConfiguration` CRD 与节点侧的 agent 完成（v1.14+）。具体字段随版本变化，生产上要先在测试集群验证内核/cgroup 版本。

### 12.6.1 混部的风险

- 在线服务延迟抖动；
- 离线作业被 OOM kill；
- 节点级资源争抢导致 kubelet 不健康；
- 监控误判（requests 与 usage 不一致）。

混部不是“把两个队列放一起”这么简单，它需要节点级隔离、内核参数、监控和容量模型共同配合。

### 12.6.2 节点侧隔离的四个层次

调度器只能决定“把 Pod 放哪”，不能保证“放上去之后不互相伤害”。混部的隔离至少分四层：

| 层次 | 机制 | 保护谁 | 失败表现 |
|---|---|---|---|
| CPU 时间 | CPU Burst、CFS quota、动态压制 | 在线服务的延迟 | 离线任务被 throttling，或在线被离线抢 CPU |
| 内存 | cgroup v2 `memory.high/low/min`、Memory QoS | 在线服务不被 OOM | 离线 OOM、节点 kubelet 不健康 |
| IO/网络 | cgroup blkio、TC/eBPF、优先级队列 | 在线请求的 P99 | 离线刷盘拖垮在线 |
| 内核/隔离 | PSI、NUMA 绑核、隔离核、runtime class | 延迟敏感型负载 | 噪声邻居、跨 NUMA 访问 |

一个常见的误区是“把离线队列的 request 调低就够了”。request 只影响调度账本，不限制运行期实际用量；真正的隔离要靠 cgroup 与内核。反过来说，如果节点侧没有隔离，调度器把在线和离线放在同一台机器上就是一次赌博。

监控上至少要同时看：

- 在线服务的 P99 延迟与错误率；
- 节点的 PSI（CPU/内存/IO 压力）；
- 离线的 throttling 时间与 OOM 次数；
- `requests` 与实际 `usage` 的偏差。

## 12.7 预留、SLA、TDM 怎么选

三种机制经常被混用。一个简单的选择表：

| 需求 | 首选 | 原因 | 不适合 |
|---|---|---|---|
| 队列长期保底 | Queue `guarantee` | 简单、可审计、与回收集成 | 需要精确到作业/时间窗 |
| 作业必须在某时间前开始 | SLA 插件 + 监控 | 不锁定资源，利用率高 | 硬合同、必须开始的场景 |
| 某批节点分时段给不同负载 | TDM + 节点标签 | 时间维度复用，边界清晰 | 需要秒级切换、频繁变更 |
| 未来某时刻预留一批卡 | Queue guarantee + 容量规划 | 无需额外对象 | 需要“到时才扣减”的精确预留 |
| 在线/离线混部 | 队列隔离 + 节点侧 cgroup | 同时管调度与运行期 | 只配队列、不做节点隔离 |

组合建议：

```text
平台层：Queue guarantee 保底 + capability 防吃满
作业层：SLA 表达等待预算，不承诺开始时间
节点层：TDM 划分时段，在线/离线分池
运行层：cgroup v2 + PSI 监控，防止噪声邻居
```

这四层各管一件事，不要用一层解决所有问题。

## 12.8 一个混合负载的配置

```yaml
# 在线队列：保底高，不可回收
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: online
spec:
  weight: 2
  reclaimable: false
  capability:
    cpu: "64"
    memory: 256Gi
  guarantee:
    cpu: "32"
    memory: 128Gi
---
# 离线队列：可回收，权重低
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: offline
spec:
  weight: 1
  reclaimable: true
  capability:
    cpu: "128"
    memory: 512Gi
```

配合：

- 在线作业高优先级、`preemptionPolicy: PreemptLowerPriority`；
- 离线作业低优先级、可被抢占；
- 离线作业加 TDM 注解，只在不影响在线的窗口跑；
- 节点侧开启 CPU Burst/Memory QoS。

## 12.9 MiniVolcano 的实现

MiniVolcano 提供：

- `reservation` 插件：一个教学用的 `Reservation` 对象，指定队列、节点、资源、时间窗；活动期内只从其他队列的可用资源中扣除“预留中尚未被 owner 使用”的部分，避免重复扣减。
- `sla` 插件：按 tick 的等待预算，overdue 优先。
- `tdm` 插件：节点标签 + 作业注解 + tick 窗口；preemptable 作业只能在窗口内的 revocable 节点上调度。
- `overcommit` 插件：enqueue 时允许超过当前空闲的因子。

没有实现：

- Queue guarantee 的完整回收保护（proportion/capacity 的 reclaim 会停在 deserved，间接保护 guarantee）；
- 混部/CPU Burst/Memory QoS；
- 真实的作业级预留 action；
- TDM 窗口结束后的自动驱逐（只有 predicate，没有 eviction）。

场景：

- `reservation`：4 卡预留阻止 8 卡作业直到窗口结束；
- `sla`：等待超预算的作业优先于更早创建的作业；
- `tdm`：preemptable 作业等到窗口打开才进入 revocable 节点。

## 12.10 本章小结

- Volcano 没有 Reservation CRD；预留靠 Queue guarantee + reservation 插件/action + SLA。
- SLA 用等待时间换确定性，是软保障；预算要同时看分位数与违约率，并用到达率/服务率判断是否根本不可达。
- TDM 用时间窗复用节点，适合昼夜混部；窗口边界、驱逐周期和 checkpoint 要一起设计。
- 混部需要节点级隔离与监控（CPU/内存/IO/PSI），不是单纯队列配置。
- 预留、SLA、TDM 各管一层：队列保底、等待预算、时间窗；先用 12.7 的选择表定位，再组合。
- MiniVolcano 用教学抽象演示了预留、SLA、TDM 的核心语义，并明确标注差异。

下一章讲异构设备与 DRA：GPU 型号、MIG、vGPU、昇腾、NUMA 与设备配额。
