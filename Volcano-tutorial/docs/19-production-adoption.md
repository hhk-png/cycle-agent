# 第 19 章 生产落地

## 19.1 先回答五个问题

在把 Volcano 放进生产前，平台团队要能回答：

1. **谁用？** 训练、推理、HPC、大数据，各自的 SLO 是什么？
2. **资源怎么分？** 队列树、guarantee、capability、weight 的依据是什么？
3. **故障怎么办？** 调度器挂了、webhook 挂了、队列打满、抢占风暴。
4. **怎么迁移？** 现有作业如何逐步切到 Volcano，如何回滚？
5. **怎么证明有效？** 利用率、等待时间、抢占率、成本。

回答不了这五个问题，先不要上生产。

## 19.2 参考架构

```text
┌─────────────────────────────────────────────────────────┐
│ 用户：kubectl / vcctl / 平台 UI / Kubeflow / Ray         │
└───────────────────────┬─────────────────────────────────┘
                        │
              ┌─────────▼─────────┐
              │ API Server + RBAC │
              └─────────┬─────────┘
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
  Volcano 控制面    Kueue（可选）     其他 operator
  scheduler         admission         Ray/LWS/Kubeflow
  controller        queue/quota
        │
        ▼
  GPU/CPU 节点池（按型号/租户/拓扑划分）
        │
        ▼
  device plugin / DRA driver / GPU Operator
```

关键设计：

- **控制面独立节点池**：不要让 scheduler 和训练 Pod 抢 CPU。
- **GPU 节点池按型号/租户划分**：A100/H100/昇腾分开，污点 + toleration。
- **队列树与组织对齐**：部门 → 团队 → 项目。
- **可观测性独立**：Prometheus/Grafana/Loki 不与业务共享。
- **GitOps 管理 Queue/configmap**：避免手工漂移。

## 19.3 分阶段落地

### 阶段 0：评估（2–4 周）

- 盘点工作负载：框架、gang 大小、GPU 型号、时长、优先级。
- 盘点集群：节点、网络、存储、device plugin。
- 对比方案：Volcano、Kueue、云托管、Slurm。
- 定义成功指标：利用率、等待时间、抢占率、运维成本。

### 阶段 1：测试集群（4–8 周）

- 安装 Volcano；
- 用真实作业的子集跑通；
- 验证 gang、队列、抢占、拓扑；
- 压测（Benchmark/KWOK）；
- 写 runbook。

### 阶段 2：灰度（4–8 周）

- 选一个非关键团队；
- 独立节点池；
- 队列配额从宽到紧；
- 每周复盘；
- 保留回滚到默认调度器的路径。

### 阶段 3：推广（持续）

- 按团队迁移；
- 建立配额评审流程；
- 监控 SLO；
- 定期压测与故障演练。

## 19.4 队列设计

### 19.4.1 从组织出发

```text
root
├── research（研究）
│   ├── nlp
│   ├── cv
│   └── speech
├── product（产品）
│   ├── search
│   └── recommendation
└── platform（平台）
    ├── inference
    └── batch
```

### 19.4.2 从 SLO 出发

- 在线推理：guarantee 高、reclaimable=false、高优先级；
- 训练：weight 大、可借用、可回收；
- 实验：weight 小、capability 小、可被抢占；
- 数据/批处理：低优先级、TDM/夜间窗口。

### 19.4.3 配额评审

每次调整问：

- 这个团队的最坏情况需求是多少？
- 增加 guarantee 会挤压谁？
- capability 是否会导致碎片？
- 是否需要跨队列借用？
- 调整后如何验证？

### 19.4.4 自治队列、PDB 与分组上限

生产队列设计不只是“画一棵树”，还要决定三件事由谁负责：

| 机制 | 平台负责 | 业务负责 | 失败模式 |
|---|---|---|---|
| NamespaceQueue | 授权 `allowedNamespaces`、总 capability | 子树、guarantee 比例 | 租户把 guarantee 设满，子树内互相锁死 |
| PDB | 模板/审计 | 副本数、selector | selector 写错 = 没有保护 |
| NodeGroup limits | 节点组标签、总上限 | 队列注解 | 队列总量没超但某个池超了 |

建议：

- **NamespaceQueue 只在组织确实需要自治时启用**。它增加了一层 CRD 与授权逻辑，队列树稳定的团队用集群 Queue 更简单。
- **推理/在线服务必须有 PDB**，并在抢占/回收/shuffle 开启前验证 `disruptionsAllowed`。
- **异构节点池用 NodeGroup limits**，避免“A100 配额被 H100 作业吃掉”。
- 把这些对象的变更纳入 GitOps 与配额评审，不要只审 Queue。

一个常见的组织反模式：平台把 NamespaceQueue 当成“甩锅工具”，授权后不再看子树。结果是租户用 guarantee 把资源锁死，平台既没有总账，也无法回收。正确的做法是保留总 capability、定期审计子树、并对 `guarantee/capability` 比例设上限。

## 19.5 迁移策略

### 19.5.1 混合调度器

Volcano 与默认调度器可以共存：

- `schedulerName: volcano` 的 Pod 由 Volcano 调度；
- 其他 Pod 由 kube-scheduler 调度；
- 两者共享节点容量，但**配额体系不共享**。

风险：默认调度器不知道 Volcano 的队列配额，可能把 Pod 放到 Volcano 预留的节点上。缓解：

- 节点池隔离；
- taint/toleration；
- 只让一层管配额（第 28.4 节会展开 Kueue + Volcano 的双账本问题）；
- 统一用 Volcano 调度所有批作业；
- 监控节点容量冲突。

### 19.5.2 逐步切换

1. 新作业先用 Volcano；
2. 旧作业保持默认调度器；
3. 按团队切换；
4. 观察 2–4 周；
5. 全量。

### 19.5.3 回滚

- 保留默认调度器；
- 作业模板可切回 `schedulerName: default-scheduler`；
- 队列配置 Git 历史；
- 保留旧镜像/CRD。

## 19.6 高可用与灾备

### 19.6.1 HA

- scheduler/controller/admission 多副本；
- leader election；
- PodDisruptionBudget；
- 控制面节点反亲和；
- 监控 leader 切换。

### 19.6.2 备份

- etcd 快照；
- CRD 定义；
- Queue/PriorityClass/HyperNode 配置；
- scheduler configmap；
- 镜像 tag。

### 19.6.3 恢复顺序

1. etcd/集群恢复；
2. CRD；
3. 控制面；
4. Queue/PriorityClass/HyperNode；
5. 验证示例作业；
6. 恢复业务提交。

### 19.6.4 故障演练

- 杀掉 scheduler leader；
- 杀掉 admission；
- 队列打满；
- 节点 NotReady；
- 抢占风暴；
- 证书过期。

## 19.7 安全与合规

参考第 17 章。生产最低要求：

- RBAC 最小权限；
- 限制高优先级；
- NetworkPolicy；
- 镜像签名；
- 审计日志；
- patch 线跟进 CVE。

## 19.8 成本与利用率

Volcano 的价值最终体现在：

- **GPU 利用率提升**：binpack、队列借用、抢占让空闲资源被利用；
- **等待时间下降**：公平/优先级/SLA；
- **运维效率**：统一队列/配额/观测；
- **碎片减少**：拓扑/binpack。

iFLYTEK 的公开案例（KubeCon China 2025）报告：

- GPU 利用率提升 **40%**；
- 故障恢复加快 **70%**；
- 资源干扰降低 **50%**。

这些数字来自其特定负载，不应直接套用到你的集群，但说明了优化空间的方向。

## 19.9 组织与流程

技术之外，还需要：

- **配额委员会**：谁分配 guarantee/capability；
- **SLO 评审**：每季度看等待时间/抢占率；
- **变更管理**：scheduler config/Queue 变更走 GitOps + 审批；
- **on-call**：谁处理 Pending/抢占告警；
- **培训**：用户知道 minAvailable、queue、priority 的含义。

一个常见失败模式：技术上线成功，但没有人对配额负责，最后队列配置失控。

## 19.10 生产检查清单

- [ ] 队列树与组织/SLO 对齐。
- [ ] guarantee 总和不超过可用容量的安全比例。
- [ ] NamespaceQueue（如启用）的授权与 guarantee 比例受控。
- [ ] 推理/在线服务配置 PDB，并验证抢占/回收/shuffle 尊重 PDB。
- [ ] 异构节点池配置 NodeGroup limits。
- [ ] 高优先级类受控。
- [ ] 控制面 HA + PDB + 反亲和。
- [ ] 监控/告警/日志就绪。
- [ ] Benchmark/压测通过。
- [ ] 故障演练完成。
- [ ] 回滚路径验证。
- [ ] 备份/恢复演练。
- [ ] 安全加固完成。
- [ ] 用户文档与培训完成。
- [ ] 配额评审流程建立。

## 19.11 综合案例：一个 64 节点多租户 GPU 平台

把前面所有章节拼起来，看一个完整但可复现的设计。目标不是给出“标准答案”，而是展示每个决策背后的取舍与验证方式。

### 19.11.1 硬件与工作负载

| 项 | 规格 |
|---|---|
| 节点 | 64 台，每台 8 GPU |
| A100 池 | 48 台（384 卡），标签 `volcano.sh/nodegroup-name=a100` |
| H100 池 | 16 台（128 卡），标签 `volcano.sh/nodegroup-name=h100` |
| 网络 | 每 16 台一个 spine，HyperNode tier 3 = spine，tier 2 = rack |
| 工作负载 | 训练（32/64/128 卡 gang）、推理（LWS 多副本）、离线批处理 |

### 19.11.2 队列设计

```text
root
├── research-train   weight 4  guarantee 160  capability 384
├── product-train    weight 2  guarantee 64   capability 256
├── inference        weight 1  guarantee 64   capability 128  reclaimable=false
└── batch            weight 1  guarantee 0    capability 256
```

检查：

```text
Σ guarantee = 160 + 64 + 64 + 0 = 288 ≤ 512 × 0.9 ≈ 460   ✓
Σ capability = 1024 > 512                                  ✓（允许超卖）
保留 headroom = 460 - 288 = 172 卡，用于突发与碎片
```

为什么这样分：

- **research-train** 是主力，guarantee 覆盖“最坏情况下的 5 个 32 卡作业”；
- **product-train** 对交付时间敏感，guarantee 较小但优先级更高；
- **inference** 不可回收，保证在线副本不被训练抢占；
- **batch** 没有 guarantee，完全借空闲资源，最低优先级。

### 19.11.3 调度器配置

```yaml
actions: "enqueue, allocate, backfill, preempt, reclaim, shuffle"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
    enablePreemptable: true
  - name: pdb
  - name: conformance
- name: tier-2
  plugins:
  - name: drf
  - name: predicates
  - name: proportion          # 层级队列时替换为 capacity + enableHierarchy
  - name: network-topology-aware
  - name: group-topology-affinity
  - name: nodeorder
  - name: binpack
    arguments: {binpack.weight: "5"}
- name: tier-3
  plugins:
  - name: rescheduling
    arguments:
      interval: "10m"
      strategies:
        - name: lowNodeUtilization
          params:
            thresholds: {cpu: 20, memory: 20, pods: 20}
            targetThresholds: {cpu: 50, memory: 50, pods: 50}
  - name: resourcequota
  - name: nodegroup
```

注意 proportion 和 capacity 二选一。上面的队列树是扁平的，所以用 proportion；如果要表达“部门 → 团队 → 项目”，把 proportion 换成 capacity 并打开 `enableHierarchy`，不要同时启用两者。

### 19.11.4 关键对象

训练作业：

```yaml
spec:
  schedulerName: volcano
  queue: research-train
  minAvailable: 64          # 8 节点 × 8 卡
  maxRetry: 3
  policies:
    - event: PodEvicted
      action: RestartJob
  networkTopology:
    mode: hard
    highestTierAllowed: 3   # 整个 gang 在同一 spine
  tasks:
    - name: worker
      replicas: 64
      template:
        spec:
          nodeSelector:
            volcano.sh/nodegroup-name: a100
```

推理 PDB：

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: inference-pdb
spec:
  minAvailable: 75%
  selector:
    matchLabels: {app: inference}
```

节点组上限（上游用 Queue 注解，Mini 用类型字段）：

```yaml
metadata:
  annotations:
    volcano.sh/nodegroup-resource-limits: |
      {"a100": {"nvidia.com/gpu": "256"},
       "h100": {"nvidia.com/gpu": "96"}}
```

### 19.11.5 SLO 与告警

| SLO | 目标 | 告警 |
|---|---|---|
| 高优先级作业启动 P95 | < 2 min | 5 分钟内未满足 |
| 普通训练排队 P95 | < 30 min | 持续 15 分钟 |
| 调度延迟 P99 | < 5 s | 持续 5 分钟 |
| 抢占率 | < 5 次/小时 | 持续 1 小时 |
| inference PDB 违约 | 0 | 任意一次 |
| GPU 利用率 | > 65% | 连续 24 小时低于 50% |

### 19.11.6 上线与回滚

```text
第 1 周：部署控制面，跑通一个 8 卡作业，不接管生产队列
第 2 周：research-train 灰度，只调度新提交的作业
第 3 周：启用 proportion/reclaim，观察借用与回收
第 4 周：启用 topology/PDB，灰度推理副本
第 6 周：启用 shuffle，只对 batch 队列
第 8 周：复盘，决定是否扩到 product-train
```

回滚条件（任一触发）：

- P99 调度延迟 > 5s 持续 10 分钟；
- 队列账本出现负值或不守恒；
- inference PDB 违约；
- 抢占导致关键作业重启超过 2 次/天；
- scheduler/controller CrashLoop > 5 分钟。

### 19.11.7 故障演练脚本

| 演练 | 操作 | 期望 |
|---|---|---|
| scheduler leader 故障 | 删除 leader Pod | 30s 内新 leader，Pending 作业继续 |
| webhook 证书过期 | 篡改 Secret/停 admission | 提交被拒，告警触发，回滚证书 |
| 队列 guarantee 配置错误 | 把 guarantee 总和调到 > 容量 | 准入/评审拦截，不进入生产 |
| 抢占风暴 | 两个队列权重频繁调整 | 告警触发，临时移除 preempt |
| 节点 NotReady | 驱逐一个节点 | Pod 重新调度，PDB 不被击穿 |
| etcd 抖动 | 注入延迟 | 调度延迟升高但不丢账本 |

### 19.11.8 这个案例的边界

- 数字是示例，不是容量承诺；A100/H100 的比例要按实际采购调整。
- 没有覆盖多集群、DRA、MIG/vGPU；这些在第 13、20 章单独讨论。
- `shuffle` 只对 batch 队列开启，因为训练作业的驱逐成本远高于批处理。
- PDB 只保护推理；训练靠 gang + RestartJob，不靠 PDB。

## 19.12 成本模型与 Showback

平台上线后，第二个问题通常不是“能不能跑”，而是“谁用了多少、该不该给这么多”。Showback（展示成本）和 Chargeback（实际计费）是把调度数据变成组织语言的桥梁。

### 19.12.1 三个口径

| 口径 | 计算 | 用途 | 常见误差 |
|---|---|---|---|
| 分配成本 | `allocated GPU-hours × 单价` | 配额/预算 | 占着卡不干活也算钱 |
| 有效成本 | `实际利用率 × GPU-hours × 单价` | 效率评估 | 依赖 DCGM/usage 数据 |
| 机会成本 | `排队时间 × 业务价值` | 优先级/SLA | 价值难量化 |

Showback 报告至少要同时给出“分配”和“有效”两个数，否则会把“占卡”误当成“用卡”。

### 19.12.2 一个 512 卡集群的月度表

假设单价 $2/GPU-hour，月 730 小时，四个队列：

| 队列 | 分配 GPU-hours | 分配成本 | 实际利用率 | 有效成本 | 备注 |
|---|---:|---:|---:|---:|---|
| train-a | 150,000 | $300k | 72% | $216k | 大 gang，利用率高 |
| train-b | 80,000 | $160k | 65% | $104k | 有碎片 |
| inference | 70,000 | $140k | 35% | $49k | 副本冗余，保 SLA |
| batch | 40,000 | $80k | 80% | $64k | 低优先级，借空闲 |
| 空闲 | 33,760 | $67.5k | — | — | 未分配/故障 |

总容量核对：`512 × 730 = 373,760 GPU-hours`；四个队列分配 340,000，空闲 33,760，合计 373,760。

这张表能回答：

- 哪个队列“占着不用”？（inference 的分配/有效差距最大，但是 SLA 需要）
- 哪个队列“效率低”？（train-b 的碎片需要 binpack/拓扑优化）
- 空闲成本是否合理？（33,760 GPU-hours 是容量冗余还是调度失败？）

### 19.12.3 数据管线

```text
调度器指标/CRD status
  → 每小时快照（queue、namespace、job、allocated）
  → 与作业表/账单表关联（谁提交、属于哪个团队）
  → 乘单价（按 GPU 型号/合同）
  → 生成 showback 报表
  → 配额评审/预算告警
```

关键字段：

| 字段 | 来源 | 用途 |
|---|---|---|
| `queue` | PodGroup/Job | 租户归属 |
| `namespace` | Pod | 二级归属 |
| `job_uid` | Job | 去重/追溯 |
| `gpu_model` | 节点标签 | 单价差异 |
| `allocated` | 调度器/节点 | 分配成本 |
| `utilization` | DCGM/usage | 有效成本 |
| `start/end` | Job status | 时长 |
| `preempted` | 事件 | 中断成本 |

### 19.12.4 五个计费陷阱

1. **重复计费**：共享 GPU/MIG 的逻辑卡与物理卡混算。
2. **抢占重复**：被驱逐作业的已用时长和重启后的时长重复计入。
3. **空闲归属**：没人用的卡算给谁？通常算平台 overhead，不要摊给租户。
4. **借用成本**：A 借了 B 的份额，账单算给 A 还是 B？建议算给实际使用者，同时在报表里标注“借用”。
5. **时区/窗口**：按月还是按 4 周？跨月作业怎么切？先定义清楚再自动化。

### 19.12.5 从 Showback 到治理

Showback 本身不改变行为，除非和流程绑定：

- 季度配额评审：用报表讨论 guarantee/capability；
- 预算告警：队列月度分配超过预算的 80% 时通知；
- 效率目标：对“分配高、有效低”的队列做专项优化；
- 透明规则：让租户能看到自己的用量和单价，减少争议。

一个反模式：把 Showback 当成惩罚工具，突然按峰值收费。结果是大团队低报需求、私下占卡，反而降低利用率。成本模型的目标是“让资源流向价值高的地方”，不是“让账单好看”。

## 19.13 把 Volcano 藏在平台后面：平台 API 与租户体验

生产平台很少让用户直接写 `vcjob.yaml`。原因不是“CRD 不好”，而是：

- 用户不应该知道 `minAvailable`、`queue`、`priorityClassName` 的组合规则；
- 平台要强制镜像、资源、网络、安全策略；
- 同一个作业可能要在多个集群/队列之间迁移；
- 计费、审计、配额需要统一的提交入口；
- 错误信息要面向用户，而不是调度器日志。

这一节给一个“平台 API 层”的设计骨架。它不是 Volcano 的一部分，而是把 Volcano 变成产品的那一层。

### 19.13.1 API 表面

一个够用的提交 API：

```text
POST /api/v1/jobs
GET  /api/v1/jobs/{id}
GET  /api/v1/jobs?tenant=&state=
POST /api/v1/jobs/{id}:cancel
GET  /api/v1/jobs/{id}/logs
GET  /api/v1/jobs/{id}/events
POST /api/v1/quotas/requests
```

请求体只暴露用户真正需要决定的字段：

| 用户字段 | 含义 | 平台映射 |
|---|---|---|
| `name` | 作业名 | `metadata.name` + 唯一后缀 |
| `image` | 镜像 | 校验仓库/签名后写入 container |
| `command` | 启动命令 | container command/args |
| `replicas` | 副本数 | `tasks[].replicas` |
| `resources` | 每副本 CPU/内存/GPU | `requests/limits` |
| `gangSize` | 最小一起启动数 | `minAvailable` |
| `queueClass` | 队列类别（train/ft/infer/agent） | 映射到具体 Queue |
| `priorityClass` | 优先级类别（normal/high） | 映射到白名单 PriorityClass |
| `topologyClass` | 拓扑类别（single-node/rack/spine） | `networkTopology` |
| `checkpoint` | checkpoint URI | volume/环境变量 |
| `ttl` | 完成后保留时长 | `ttlSecondsAfterFinished` |
| `requestId` | 幂等键 | annotation/唯一索引 |

**平台控制的字段**：`schedulerName`、`queue` 的具体名字、`priorityClassName` 的具体值、`networkTopology.highestTierAllowed`、节点选择器、容忍、ServiceAccount、镜像拉取 Secret。用户只能选“类别”，不能直接写底层字段。

### 19.13.2 默认值与校验

提交时要回答的问题：

```text
1. 队列是否存在且 Open？租户是否有权使用？
2. gangSize 是否 ≤ replicas？是否满足 TP×PP 的整除约束？
3. GPU requests 是否等于 limits？是否超过租户配额？
4. 镜像是否在白名单/已签名？是否固定 digest？
5. priorityClass 是否在租户白名单？
6. topologyClass 是否有对应的 HyperNode 数据？
7. checkpoint URI 是否可访问、是否有写权限？
8. requestId 是否重复？
```

校验失败要返回**可操作**的错误，而不是把 API Server 的原始错误透传：

```json
{
  "code": "GANG_SIZE_INVALID",
  "message": "gangSize=6 is not a multiple of TP(4) x PP(2)",
  "field": "gangSize",
  "remediation": "use 8, 16, 24, ... or change the parallel plan",
  "docs": "https://platform.example.com/docs/gang"
}
```

错误码要稳定、可枚举；`message` 可以变，`code` 不要变，否则用户脚本会碎。

### 19.13.3 幂等与状态聚合

提交 API 必须幂等：

```text
客户端带 requestId
  → 平台查 (tenant, requestId)
  → 已存在：返回同一个 job id（200），不要创建第二个
  → 不存在：创建 Volcano Job + 记录映射
  → 创建成功但响应丢失：下一次重试仍返回同一个 id
```

状态聚合要把三层对象压成一个用户视角：

| 平台状态 | 来自 | 说明 |
|---|---|---|
| `QUEUED` | Job phase Pending；PodGroup Inqueue 表示已入队 | 还没拿到资源 |
| `STARTING` | PodGroup Inqueue + 部分 Pod Bound | 正在绑定/拉镜像 |
| `RUNNING` | PodGroup Running | 至少 minAvailable 在跑 |
| `SUCCEEDED` | Job Completed | 全部成功 |
| `FAILED` | Job Failed/Aborted | 带 reason |
| `CANCELLED` | 用户取消 | 记录操作人 |

不要直接把 `vcjob.status.phase` 透传给用户：Volcano 的 phase 有 `Restarting/Completing/Terminating` 等中间态，用户不需要理解全部；平台要映射成稳定的产品状态。

### 19.13.4 多租户绑定

```text
租户 → namespace → ServiceAccount → Queue 白名单 → PriorityClass 白名单
```

四条规则：

1. **一个租户一个 namespace（或一组 namespace）**，Queue 由平台创建，租户只读。
2. **用户不能创建 PodGroup/Queue/PriorityClass**；平台 API 用平台 SA 创建，或由准入校验 owner。
3. **队列名不直接暴露**：用户选 `queueClass=train`，平台按租户映射到 `tenant-a-train`；避免用户猜/抢别人的队列。
4. **提交人标签**：每个 Job 打 `platform.example.com/submitted-by`，用于审计/计费；标签由平台写入，用户不可改。

### 19.13.5 自助配额

配额不应全靠工单。一个可运营的流程：

```text
租户在平台提交 quota request（队列、资源、期限、理由）
  → 平台展示当前 allocated/deserved/guarantee 与影响
  → 审批（自动规则 + 人工）
  → GitOps 更新 Queue（PR）
  → 控制器同步到集群
  → 通知租户 + 记录审计
```

自动化规则示例：

- 申请 `guarantee` 后，Σ guarantee 不得超过集群可用容量的 90%；
- 单租户 `guarantee/capability` 比例不得超过 0.8；
- 申请高优先级必须说明截止时间/SLO，并限制队列内最高优先级；
- 临时配额带过期时间，到期自动回收。

### 19.13.6 平台 API 的反模式

1. **直接暴露 CRD**：用户复制粘贴别人的 YAML，绕过平台策略。
2. **让用户填 `priorityClassName`**：迟早有人给自己设最高优先级。
3. **一个用户一个 Queue**：队列数爆炸，公平性和配额都难管理。
4. **状态直接透传**：用户看到 `Restarting` 不知道该怎么办。
5. **没有幂等**：网络重试创建重复作业，重复计费。
6. **错误信息是 `admission webhook denied`**：用户不知道改哪个字段。
7. **平台 API 和 Volcano 版本强耦合**：升级 Volcano 就改用户接口。
8. **没有提交人/租户标签**：出问题无法归因，计费无法归属。

### 19.13.7 一个最小请求/响应

```json
POST /api/v1/jobs
{
  "requestId": "9f2c1e0a-...",
  "name": "llama-sft",
  "image": "registry.example.com/trainer@sha256:...",
  "command": ["torchrun", "--nnodes=4", "train_sft.py"],
  "replicas": 4,
  "resources": {"cpu": "16", "memory": "128Gi", "gpu": 2},
  "gangSize": 4,
  "queueClass": "finetune",
  "priorityClass": "normal",
  "topologyClass": "rack",
  "checkpoint": "s3://models/checkpoints/llama-sft",
  "ttl": "24h"
}
```

响应：

```json
{
  "id": "job-7f3a...",
  "state": "QUEUED",
  "queue": "tenant-a-finetune",
  "links": {"self": "/api/v1/jobs/job-7f3a...", "logs": "..."}
}
```

这一层的价值不是“多写一个服务”，而是把 Volcano 的复杂性收进平台，把**可审计、可配额、可迁移、可解释**的接口留给用户。

## 19.14 本章小结

- 生产落地是技术、流程、组织的组合。
- 分阶段：评估 → 测试 → 灰度 → 推广。
- 队列设计从组织和 SLO 出发，guarantee 是核心约束。
- 混合调度器要小心配额不共享。
- 平台 API 层决定租户体验：只暴露类别字段、平台控制 queue/priority/topology，错误码稳定、提交幂等、状态聚合、配额自助。
- HA/DR、安全、成本、组织缺一不可。

下一章讲 Volcano Global 与多集群：当单个集群不够时怎么办。
