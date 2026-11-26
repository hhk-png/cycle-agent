# 第 28 章 迁移、选型与容量规划

## 28.1 选型从约束开始，不从功能表开始

调度器选型最常见的错误是拿一张功能对照表打勾：gang、队列、抢占、拓扑、GPU 共享……勾得越多越放心。真正决定成败的是四个约束：

1. **工作负载形状**：单 Pod 服务、gang 训练、MPI、Spark、推理、Agent；
2. **租户模型**：单团队、多 namespace、多队列、多集群；
3. **放置约束**：节点、机架、NUMA、GPU 型号、RDMA 拓扑；
4. **组织能力**：能否运维第二个调度器、能否改 Go 代码、能否接受 CRD。

功能表回答“能不能”，约束回答“值不值得”。一个只需要 namespace 配额的团队，引入 Volcano 的 gang/抢占/回收体系，可能只是给自己增加了一个要值班的组件。

## 28.2 决策树

```text
需要作业级 all-or-nothing（gang）吗？
├─ 否
│  ├─ 只需要 namespace/租户配额 → Kueue + 默认调度器
│  ├─ 只需要简单批处理 → Kubernetes Job / CronJob
│  └─ 需要 GPU 共享 → 设备插件/KAI/Volcano deviceshare
└─ 是
   ├─ 需要队列公平/借用/回收吗？
   │  ├─ 否 → scheduler-plugins coscheduling 或 Volcano 最小配置
   │  └─ 是 → Volcano
   ├─ 需要网络拓扑/NUMA/异构设备吗？
   │  ├─ 是 → Volcano + HyperNode/NUMA/deviceshare/DRA
   │  └─ 否 → Volcano 默认插件集
   └─ 需要多集群/跨站点吗？
      ├─ 是 → Volcano Global / Karmada + Volcano
      └─ 否 → 单集群 Volcano
```

三个判断点的含义：

- **gang** 是分水岭。没有 gang，默认调度器 + Kueue 往往更简单。
- **队列公平**是第二个分水岭。只有配额、没有借用/回收，Kueue 的 ClusterQueue 足够。
- **拓扑/设备**是第三个。它决定你要不要维护 HyperNode、NUMA、DRA 这些对象和插件。

## 28.3 从默认 kube-scheduler 迁移

### 28.3.1 迁移的不是 Pod，是“谁负责放置”

默认调度器按 Pod 调度；Volcano 按 PodGroup 调度。迁移时要把工作负载的“组”显式表达出来：

| 工作负载 | 迁移方式 |
|---|---|
| 裸 Pod / Deployment | 创建 PodGroup，Pod 加 `scheduling.k8s.io/group-name`，`schedulerName: volcano` |
| Kubernetes Job | 同上，或用 Volcano Job 包装 |
| Kubeflow / Training Operator | 用 Volcano 作为 gang scheduler，或改用 Volcano Job |
| Ray | KubeRay + Volcano PodGroup |
| Spark | Spark operator + Volcano PodGroup |

### 28.3.2 双调度器共存

Kubernetes 允许不同 Pod 使用不同 `schedulerName`。迁移期可以：

```text
schedulerName=default-scheduler  → 在线服务
schedulerName=volcano            → 批作业/训练
```

但要注意：

1. **资源账本不共享**：两个调度器各自看 API Server 的 Pod，可能同时认为节点有空闲，造成超卖。缓解方式是给批作业节点打 taint，或使用 node affinity 划分节点池。
2. **优先级不可比**：Kubernetes PriorityClass 是全局的，但两个调度器的抢占逻辑不同。
3. **PDB/拓扑各管各的**：Volcano 的 pdb 插件只保护它调度范围内的 Pod。
4. **排障要说明是哪一层**：`kubectl describe pod` 的 `Events` 会显示调度器名字，先确认是谁在拒绝。

### 28.3.3 迁移步骤

```text
阶段 0：盘点工作负载，标记 gang/队列/拓扑需求
阶段 1：建节点池与 taint，部署 Volcano，跑通一个非关键作业
阶段 2：把一个团队的批作业切到 volcano，保留默认调度器回滚路径
阶段 3：引入队列/配额，观察 2 周，校准 deserved
阶段 4：启用 preempt/reclaim，先在低优先级队列灰度
阶段 5：启用拓扑/设备插件，逐队列放开
阶段 6：下线旧路径，更新 runbook 与告警
```

每一阶段都要有**回滚条件**：例如“P99 调度延迟 > 5s 持续 10 分钟”“作业失败率 > 基线 2 倍”“队列账本出现负值”。

## 28.4 从 Kueue 迁移或组合

Kueue 的模型是“准入 + 配额”，Pod 仍由 kube-scheduler 放置。它与 Volcano 的边界：

| 能力 | Kueue | Volcano |
|---|---|---|
| 租户配额 | ClusterQueue/LocalQueue | Queue weight/capability/guarantee |
| 借用/借出 | borrowingLimit/lendingLimit | deserved + reclaim |
| gang | 有限（通过 PodGroup/Job 集成） | 核心 |
| 放置 | 交给 kube-scheduler | 自己做 |
| 拓扑/设备 | 依赖 kube-scheduler 插件 | 内置插件 |

### 28.4.1 组合模式

```text
提交 Job
  → Kueue 准入（配额、borrow/lend）
  → Volcano 调度（gang、拓扑、抢占）
```

组合的关键是**只让一层管配额**。常见错误是 Kueue 的 ClusterQueue 和 Volcano 的 Queue 各算一套，结果：

- Kueue 认为配额已用完，拒绝准入；
- Volcano 认为队列还有 deserved，继续调度；
- 或者反过来，Volcano 回收了 Kueue 已准入的作业，Kueue 的状态与事实不一致。

推荐做法：

1. 要么 Kueue 管准入、Volcano 队列只做 gang/拓扑（不配 capability/guarantee）；
2. 要么 Volcano 管全部配额，Kueue 只做作业排队（关闭 borrowing）；
3. 用 Kueue 的 AdmissionCheck 显式串行化，不要让两套控制器同时改同一个 Job 状态。

### 28.4.2 从 Kueue-only 迁到 Volcano

如果团队已经在用 Kueue，迁移不是“替换”，而是“下沉”：

- 保留 Kueue 的队列/准入接口，用户无感；
- 把 Pod 的 `schedulerName` 改为 volcano；
- 用 Volcano 的 PodGroup 表达 gang；
- 逐步把配额语义从 Kueue 迁到 Volcano Queue，避免双账本。

## 28.5 从 YuniKorn 迁移

YuniKorn 是 Apache 的独立批调度器，队列/公平模型与 Volcano 类似。迁移时主要对齐：

| YuniKorn | Volcano | 注意 |
|---|---|---|
| Queue（hierarchical） | Queue + parent | capacity 插件 |
| fairshare | proportion/fairshare | 权重语义不同 |
| gang | PodGroup minMember | 注解/CRD 不同 |
| preemption | preempt/reclaim | 同队列 vs 跨队列 |
| node sorting | nodeorder/binpack | 打分权重需重调 |

迁移风险最高的是**公平语义**：YuniKorn 的 fairshare 与 Volcano 的 proportion 在“借用后回收”的时机上不同。建议先用影子队列跑两周，比较两套系统的分配序列，再切换。

## 28.6 从 Slurm 迁移

Slurm 是 HPC 事实标准，Volcano 试图把类似能力带入 Kubernetes。映射关系：

| Slurm | Volcano/K8s | 差距 |
|---|---|---|
| Partition | Queue + node affinity | 分区是硬边界，队列可借用 |
| QOS | PriorityClass + Queue | 语义不完全等价 |
| `srun/sbatch` | Volcano Job / Pod | 作业脚本需容器化 |
| `--gres=gpu:8` | `nvidia.com/gpu: 8` | 设备插件 |
| `--constraint` | nodeSelector/affinity | 标签体系 |
| `--ntasks-per-node` | task replicas + 拓扑 | 需显式建模 |
| `sacct/squeue` | `vcctl`/metrics | 可观测性需补齐 |
| 拓扑（switch/rack） | HyperNode | 需发现机制 |

Slurm 用户最容易低估的是**容器化**：MPI 的进程启动、共享内存、RDMA 设备挂载、SSH 免密，都需要在镜像和 Pod 模板里解决。调度只是迁移的一部分。

## 28.7 从自研调度器迁移

自研调度器通常有三种结局：

1. **功能被 Volcano 覆盖**：直接迁移，保留自定义插件；
2. **有独特策略**：写成 Volcano 插件（第 7 章），复用 cache/session/statement；
3. **模型根本不同**：保留自研，用 Volcano 只做某些队列。

迁移前先做“语义盘点”：

```text
我们有哪些排序规则？
哪些是硬约束，哪些是打分？
哪些状态是调度器持有的，哪些在控制器？
失败回滚的边界在哪？
```

如果自研调度器的核心价值是“一个特殊打分函数”，写插件比自研整套框架便宜一个数量级。

## 28.8 容量规划：先算保底，再算突发

### 28.8.1 基本公式

设集群有 `G` 张 GPU，预留 `r`（系统/碎片/故障），可用容量：

```text
C = G × (1 - r)
```

队列 `q` 的 deserved 与 guarantee：

```text
deserved_q = C × w_q / Σw
guarantee_q ≤ deserved_q
capability_q ≥ deserved_q
```

所有队列的 guarantee 之和必须满足：

```text
Σ guarantee_q ≤ C
```

否则某些保底永远无法同时兑现。所有队列的 capability 之和可以大于 `C`，这是**超卖**，依赖实际需求不同时发生。

### 28.8.2 一个 512 卡集群的例子

```text
G = 512
r = 0.1（系统、故障、碎片）
C = 460.8 ≈ 460
```

四个队列：

| 队列 | 权重 | guarantee | capability | deserved（按权重） |
|---|---:|---:|---:|---:|
| train-a | 3 | 120 | 300 | 197 |
| train-b | 2 | 80 | 240 | 131 |
| inference | 1 | 60 | 120 | 66 |
| batch | 1 | 40 | 200 | 66 |

检查：

```text
Σ guarantee = 300 ≤ 460        ✓
Σ capability = 860 > 460       ✓（允许超卖）
```

如果 train-a 和 train-b 同时满载，inference 的 60 卡保底仍然可用，因为 guarantee 之和 300，剩余 160 卡可以被借用/回收。

### 28.8.3 gang 与碎片

一个 32 卡作业需要 4 台 8 卡节点。集群有 40 张空闲卡，但分散在 20 台各 2 卡的节点上，作业仍然放不下。这就是碎片。

近似估算可放置的 gang 数：

```text
N_gang ≈ min( floor(C / g), floor(N_node / n_node) )
```

其中 `g` 是单作业卡数，`n_node` 是单作业节点数。实际值还受拓扑、NUMA、CPU/内存配比影响。容量规划时建议保留 10%–20% 的 headroom，专门吸收碎片。

binpack 能减少碎片，但会降低故障域隔离；spread 相反。没有免费的午餐，只有与业务匹配的取舍。

### 28.8.4 GPU 共享与超卖

如果使用 MIG/vGPU/device share，单卡的“逻辑容量”大于 1。规划时要区分：

```text
物理卡数 × 切分比 = 逻辑卡数
```

但逻辑卡数不能直接用于 gang 估算：一个需要 8 张完整卡的训练作业，不能跑在 8 个 1/7 MIG 切片上。队列的 capability 应该按**资源形状**分别配置，而不是只配一个总数。

### 28.8.5 队列数量的经验值

- 少于 5 个队列：公平性容易观察，适合中小团队；
- 5–20 个队列：需要层级队列和自动化配额管理；
- 超过 20 个：考虑 NamespaceQueue 或按业务线拆集群；
- 每个队列都配 guarantee 会导致保底总和迅速超过容量，最终谁都无法兑现。

## 28.9 性能基准：测什么，怎么测

### 28.9.1 四个指标

| 指标 | 定义 | 关注点 |
|---|---|---|
| 调度吞吐 | 每秒绑定 Pod 数 | 大集群批量提交 |
| 调度延迟 | 从 Pod 创建到绑定的 P50/P99 | 交互式/推理 |
| 决策质量 | 装箱率、碎片率、抢占次数 | 成本与稳定性 |
| 资源效率 | GPU 利用率、空闲卡时 | 财务 |

### 28.9.2 测试方法

1. **kwok**：模拟数千节点，不需要真实 GPU；适合测调度器吞吐与 cache 行为。
2. **clusterloader2**：Kubernetes 官方规模测试框架；适合测 API Server/informer 压力。
3. **真实小集群**：验证设备插件、拓扑、DRA、kubelet 行为。
4. **回放生产轨迹**：把历史作业的提交时间/资源形状回放，比较调度结果。

一个可复现的实验设计：

```text
固定：节点数、资源形状、作业分布、seed
变量：调度器配置（插件/tier/权重）、队列参数
测量：P50/P99 延迟、吞吐、装箱率、抢占次数、未调度作业数
重复：至少 3 次，报告均值与方差
```

不要只报告“平均延迟”。调度器的尾延迟（P99）决定用户体验；一次 30 秒的卡顿比平均快 10ms 更值得关注。

### 28.9.3 常见性能陷阱

- **深拷贝事务**：MiniVolcano 的教学取舍，大集群不可用；
- **全量排序**：每轮对所有作业排序，作业数上万时成为瓶颈；
- **同步绑定**：逐个 Bind 调用 API Server，吞吐受限于网络；
- **无界队列**：informer 事件堆积，内存上涨；
- **指标基数爆炸**：每个 Pod 一个 label，Prometheus 先崩。

## 28.10 迁移 runbook

### 28.10.1 上线前

- [ ] 盘点工作负载：gang/队列/拓扑/设备需求；
- [ ] 确认节点池与 taint，避免双调度器争抢；
- [ ] 准备回滚：保留旧 schedulerName，保留旧配置；
- [ ] 压测：目标规模的 2 倍作业量；
- [ ] 演练：节点 NotReady、调度器重启、etcd 抖动；
- [ ] 监控：调度延迟、未调度作业、队列账本、抢占次数。

### 28.10.2 上线中

```text
T+0    部署 Volcano，跑通一个非关键作业
T+1d   一个团队切换，观察 24h
T+3d   引入队列配额，观察借用/回收
T+7d   启用 preempt/reclaim（低优先级队列）
T+14d  启用拓扑/设备插件
T+30d  复盘，决定是否扩大范围
```

### 28.10.3 回滚条件

- 调度失败率超过基线 2 倍；
- P99 延迟超过 SLA；
- 队列账本出现负值或持续不守恒；
- 抢占导致关键作业反复重启；
- 控制器/调度器 CrashLoop 超过 5 分钟。

回滚不是“把 schedulerName 改回去”这么简单：还要清理 PodGroup、Queue、Webhook，避免残留对象影响默认调度器。

## 28.11 组织与职责

调度器不是一个人的项目。一个可持续的落地至少需要四个角色：

| 角色 | 职责 |
|---|---|
| 平台工程 | 部署、升级、监控、容量 |
| 调度/算法 | 插件、队列策略、公平性调参 |
| 业务团队 | 作业形状、优先级、SLA |
| SRE/值班 | 告警、runbook、故障复盘 |

最常见的组织失败是“平台团队配了队列，业务团队不知道 weight 是什么意思”。配额不是技术参数，是组织契约；没有共识的 weight 一定会被滥用。

## 28.12 本章小结

- 选型从约束开始：gang、租户、拓扑、组织能力。
- 从默认调度器迁移的核心是把“组”显式化，并处理双调度器账本。
- Kueue 与 Volcano 可以组合，但只能让一层管配额。
- 从 YuniKorn/Slurm/自研迁移，难点通常在公平语义、容器化和状态边界，而不是 API。
- 容量规划先算 guarantee，再算突发；碎片和 GPU 共享会显著改变可用容量。
- 性能基准要测尾延迟、吞吐、决策质量和资源效率，而不只是平均值。
- 迁移 runbook 的核心是可回滚、可观测、可归因。

下一章回到 Volcano 本身，讨论它在 2026 年之后的技术方向。
