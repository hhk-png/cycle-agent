# 第 31 章 商业分析（二）：竞争格局

## 31.1 竞争不是“功能清单”

调度器的选型很少是“谁的 gang 更强”。真正的决策变量是：

- **模型假设**：它假设“排队/准入”和“放置”能不能分开？
- **运维成本**：多一个调度器，多一套 HA/升级/排障。
- **生态**：框架、云厂商、硬件、社区。
- **失败边界**：在什么条件下它的假设会断裂？

用“功能对比表”选调度器，通常会选错。

## 31.2 主要玩家

| 项目 | 定位 | 调度单位 | 与 kube-scheduler 的关系 | 治理 |
|---|---|---|---|---|
| **Volcano** | 全功能批调度器 | Job/PodGroup | 替换/并行 | CNCF Incubating |
| **Kueue** | 作业准入 + 配额 | Workload | 前置，不替换 | Kubernetes SIG Scheduling |
| **YuniKorn** | 批调度器 | Queue/Job | 替换/并行 | Apache |
| **KAI Scheduler** | GPU 共享 + gang | PodGroup | 并行 | 开源（NVIDIA 生态） |
| **Slurm** | HPC 调度器 | Job/Step | 独立 | SchedMD/社区 |
| **Run:ai** | 商业 GPU 编排 | 工作负载 | 平台层 | NVIDIA 旗下（收购，以官方公告为准） |
| **scheduler-plugins** | kube-scheduler 插件集 | Pod | 扩展 | Kubernetes SIG |
| **Ray/KubeRay** | 框架 + 调度集成 | RayCluster | 可选 Volcano | Anyscale/社区 |
| **Kubeflow Trainer** | 训练 operator | Job | 可选 Volcano | Kubeflow |

## 31.3 逐家分析

### 31.3.1 Kueue

**模型假设**：作业的“准入/配额”与 Pod 的“放置”可以分开。Kueue 决定一个 Workload 是否 admit，admit 后 Pod 交给 kube-scheduler。

**强项**：

- 与 Kubernetes 原生调度器无缝；
- ClusterQueue/LocalQueue/Cohort 的配额模型成熟；
- 两阶段准入、borrowing、preemption；
- MultiKueue 多集群；
- 侵入小，适合“只缺配额”的团队。

**失败边界**：

- 当放置本身是难点（大 gang、网络拓扑、NUMA、异构设备）时，Kueue 把放置交给 kube-scheduler，能力受限于后者；
- TAS 能缓解拓扑，但不是完整的 HyperNode/多级 gang；
- 与 Volcano 组合时，两套配额账本可能不一致。

**适合**：只需要队列/配额、希望最小侵入、已有 kube-scheduler 投资。

### 31.3.2 Volcano

**模型假设**：调度器必须理解作业（PodGroup）和队列（Queue），并在一个 Session 里做作业级决策。

**强项**：

- gang、DRF、proportion/capacity、抢占/回收；
- 拓扑（HyperNode）、NUMA、异构、DRA；
- 作业生态（JobFlow/Cron/Job plugins）；
- 中国生态/华为云/昇腾；
- CNCF 治理。

**失败边界**：

- 运维复杂：第二个调度器、配置、升级、排障；
- 与默认调度器共享节点时配额不共享；
- 文档/版本碎片化；
- 没有第一方企业支持；
- 某些高级特性是 Alpha。

**适合**：GPU 训练/HPC/多租户平台，需要作业级放置。

### 31.3.3 YuniKorn

**模型假设**：Kubernetes 上的批/大数据工作负载需要队列公平与 gang。

**强项**：

- 队列/公平/层级；
- 与 Hadoop/Spark 生态的历史渊源；
- Apache 治理。

**失败边界**：

- 社区规模/生态不如 Volcano/Kueue；
- GPU/拓扑/DRA 的深度不如 Volcano；
- 运维同样需要第二个调度器。

**适合**：已有 Hadoop/Spark 存量、需要队列公平的团队。

### 31.3.4 KAI Scheduler

**模型假设**：GPU 共享/隔离与 gang 是核心，调度器应围绕 GPU 做。

**强项**：

- GPU 共享（MIG/vGPU/时间片）；
- gang；
- 与 NVIDIA 生态结合。

**失败边界**：

- 队列/多租户/通用批处理的广度；
- 生态锁定；
- 社区治理。

**适合**：GPU 密集型、需要共享/隔离的团队。

### 31.3.5 Slurm

**模型假设**：HPC 作业在专用集群上运行，调度器与操作系统/网络深度集成。

**强项**：

- HPC 事实标准；
- 拓扑/NUMA/MPI 成熟；
- 运维工具/人才；
- 性能。

**失败边界**：

- Kubernetes 原生生态/弹性/容器化；
- 多租户云原生平台；
- 快速迭代的 AI 框架集成。

**适合**：传统 HPC、超算中心、MPI 为主。

### 31.3.6 Run:ai / 商业 GPU 平台

**模型假设**：企业愿意为 Turnkey GPU 编排/共享/配额付费。

**强项**：

- 产品化/UI/支持；
- GPU 共享/配额；
- 与 NVIDIA 生态。

**失败边界**：

- 成本；
- 锁定；
- 开源替代（Volcano/Kueue/KAI）。

**适合**：预算充足、希望少自研的企业。

### 31.3.7 scheduler-plugins

**模型假设**：在 kube-scheduler 上加插件即可满足批调度。

**强项**：

- 原生、轻量；
- coscheduling/gang；
- 与 Kueue 组合。

**失败边界**：

- 队列/公平/回收的完整体系；
- 大规模多租户；
- 作业级生命周期。

**适合**：轻量 gang、已有 kube-scheduler 平台。

## 31.4 组合模式

### 31.4.1 Kueue + Volcano

```text
Kueue：租户配额、准入、borrow/lend
Volcano：gang、拓扑、NUMA、设备
```

优点：各取所长。
风险：两套配额账本；Kueue admit 了但 Volcano 调度不了；需要统一监控。

### 31.4.2 Volcano + Ray/Kubeflow

Volcano 负责底层 gang/队列，Ray/Kubeflow 负责框架生命周期。常见于训练平台。

### 31.4.3 Slurm + Kubernetes

Slurm 管 HPC 分区，K8s 管服务；通过桥接/统一门户。适合 HPC 存量大的组织。

### 31.4.4 Kueue + kube-scheduler TAS

不想引入第二个调度器，但需要拓扑/配额。适合中小规模。

## 31.5 Volcano 的 SWOT

### 优势

- 全功能作业级调度：gang、队列、抢占/回收、拓扑、NUMA、异构、DRA。
- CNCF 治理与社区。
- 华为云/昇腾/中国生态。
- 生产案例（iFLYTEK 等）。
- 插件/action 可扩展。

### 劣势

- 运维复杂，第二个调度器。
- 没有第一方企业支持/付费版。
- 文档与版本碎片化。
- 与 Kueue/原生 API 的竞争。
- 某些高级特性 Alpha。

### 机会

- 推理/Agent 调度。
- DRA/异构设备。
- 多集群/ sovereign AI。
- GPU 云/neocloud 的利用率需求。
- 与 Kueue 组合的“准入+放置”方案。

### 威胁

- Kueue 成为 Kubernetes 原生标准。
- NVIDIA/Run:ai/KAI 的生态绑定。
- 云厂商自研调度器。
- 开源替代增多。
- 人才/运维成本。

## 31.6 厂商格局

| 厂商/平台 | 与 Volcano 的关系 |
|---|---|
| 华为云 CCE | 集成 Volcano，深度合作 |
| DaoCloud/KubeSphere | 生态/发行版 |
| iFLYTEK | 生产用户/贡献者 |
| Nebius | 以 self-managed app 提供 Volcano |
| 阿里云市场 | Volcano 社区版 |
| AWS/Azure/GCP | 多云托管批处理，竞争/替代 |

没有看到 Volcano 项目本身的企业订阅；商业化主要由云厂商/集成商/支持商完成。

## 31.7 竞争策略建议

### 31.7.1 不要在“准入/配额”正面打 Kueue

Kueue 是 Kubernetes SIG 项目，生态位稳固。更好的定位：

- Volcano 是“放置引擎”，Kueue 是“准入层”；
- 提供组合方案与一致性监控；
- 在 gang/拓扑/设备上建立差异。

### 31.7.2 聚焦 GPU 训练 + 推理

- 训练：大 gang、拓扑、DRA、checkpoint；
- 推理：LWS、KV cache、弹性；
- Agent：低延迟、沙箱。

### 31.7.3 产品化短板

开源项目的短板是：

- 安装/升级/排障；
- 可观测性/可视化；
- 支持/SLA；
- 培训/认证。

这些是商业机会。

### 31.7.4 生态绑定

- 与 GPU 厂商/云厂商合作；
- 与 Ray/Kubeflow/LWS 集成；
- 与 Kueue 组合而不是对抗；
- 参与 CNCF/Kubernetes 标准。

## 31.8 选型决策树

```text
只需要配额/准入，放置交给 kube-scheduler？
  └─ 是 → Kueue（+ TAS）
  └─ 否 → 需要 gang/拓扑/设备/作业级调度？
        └─ 是 → 规模/团队能运维第二个调度器？
              └─ 是 → Volcano / YuniKorn / KAI
              └─ 否 → 云托管/商业平台
        └─ 否 → 默认调度器 + 简单配额
```

## 31.9 竞争情报跟踪清单

调度器竞争格局变化很快，建议每季度更新一次下面这张表：

| 维度 | 看什么 | 数据源 |
|---|---|---|
| 版本节奏 | 最近 2 个 release 的关键特性 | GitHub releases、CNCF 博客 |
| 治理 | CNCF 阶段、SIG 归属、维护者变化 | 项目官网、community 文档 |
| 采用 | 新增生产案例、云厂商集成 | 案例博客、云市场 |
| 能力 | gang/队列/拓扑/设备/推理 | 文档、源码、release notes |
| 商业化 | 是否有企业版/支持订阅/托管 | 定价页、销售材料 |
| 风险 | CVE、deprecation、社区争议 | advisory、issue、邮件列表 |

跟踪时的三个原则：

- **看 release notes，不看营销页**：功能是否 GA、默认是否开启、有无 breaking change。
- **看失败边界**：一个项目“能做什么”容易抄，“在什么条件下不工作”才是选型关键。
- **看生态绑定**：云厂商、GPU 厂商、框架项目的集成，往往比单个功能更能决定去留。

## 31.10 本章小结

- 竞争的核心是模型假设与失败边界，不是功能清单。
- Kueue 管准入，Volcano 管放置；两者可组合也可竞争。
- YuniKorn/KAI/Slurm/Run:ai 各有生态位。
- Volcano 的优势在全功能作业级调度与中国生态；劣势在运维/支持/标准化。
- 策略：不与 Kueue 正面竞争，聚焦训练/推理/设备，补产品化短板。

下一章算账：TCO、ROI、定价与采购决策。
