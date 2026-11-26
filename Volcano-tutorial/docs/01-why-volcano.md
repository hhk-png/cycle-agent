# 第 1 章 为什么需要 Volcano

## 1.1 一个 Pod 调度得很好，一个作业却不一定

Kubernetes 默认调度器 `kube-scheduler` 的工作单位是 **Pod**。它回答的问题是：*这个 Pod 应该放在哪个节点上？* 它通过 Filter/Score 选出节点，写入 `pod.spec.nodeName`，然后 kubelet 拉起容器。这个模型对无状态服务非常成功，但对批处理、HPC 和分布式训练并不够用。

考虑一个 64 卡训练作业：

- 64 个 Pod 必须**一起开始**。如果只启动了 32 个，另外 32 个永远等不到资源，先启动的 32 个会占着 GPU 空转，整个作业死锁。
- 用户希望这个作业在 **A 队列**里排队，A 队列有 32 卡的保底配额，但空闲时可以借用 B 队列的卡；B 队列需要时能收回。
- 集群里同时有推理服务、离线训练、数据处理。训练作业优先级低，但明天早上八点前必须完成；推理服务的延迟不能因为训练抢占而抖动。
- 64 张卡分布在 8 台机器上，机器间有 RDMA 网络。调度器最好把作业放在同一个 spine 下，减少跨交换机流量。
- 集群同时有 A100、H100、昇腾 910B，作业只能跑在 A100 上。

这些都不是“单 Pod 放哪”的问题，而是**作业级、队列级、拓扑级**的问题。Volcano 就是在这个缝隙里成长起来的。

## 1.2 默认调度器的六个边界

| 边界 | 默认调度器的行为 | 批处理/训练需要的行为 |
|---|---|---|
| 调度单位 | 单个 Pod | 一组 Pod（gang） |
| 资源归属 | 节点 | 队列/租户，带配额与借用 |
| 冲突解决 | 优先级 + 抢占单个 Pod | 作业级抢占、跨队列回收、重启策略 |
| 放置约束 | 节点亲和/反亲和 | 网络拓扑、NUMA、GPU 型号、任务角色拓扑 |
| 生命周期 | Pod 失败由控制器处理 | 作业级重试、状态机、事件策略 |
| 公平性 | 无明显租户公平概念 | 加权公平、DRF、历史用量公平 |

Kubernetes 生态对此有两类回答：

1. **在默认调度器前面加一层准入控制**：Kueue 决定“作业什么时候可以被放行”，Pod 的放置仍交给 `kube-scheduler`。这种方案侵入小，但放置阶段的 gang/拓扑能力受限于 kube-scheduler 及其插件。
2. **替换/并行运行一个批调度器**：Volcano 自己维护一套调度循环，直接为 Pod 选择节点并绑定。它承担了更多责任，也获得了更强的作业级控制。

本教程的主角是第二种。

## 1.3 Volcano 是什么

Volcano 是 CNCF 旗下的云原生批处理系统（Cloud Native Batch System），最初源于华为的 **kube-batch**，2019 年 6 月开源，2020 年进入 CNCF Sandbox，2022 年 4 月成为 CNCF Incubating 项目，是 CNCF 第一个批计算项目。截至 2026 年 11 月，它仍处于 Incubating 阶段，最新稳定版本是 **v1.16.0**（2026-10-28 发布）。

它由四个可部署组件构成：

- `vc-scheduler`：批调度器，核心。
- `vc-controller-manager`：Job、PodGroup、Queue、CronJob、JobFlow、HyperNode 等控制器。
- `vc-webhook-manager`：CRD 的默认值注入与合法性校验。
- `vcctl`：命令行客户端。

Volcano 的核心抽象只有三个：

- **Job**（`batch.volcano.sh/v1alpha1`）：一个批作业，包含若干 task，每个 task 有副本数和 Pod 模板。
- **PodGroup**（`scheduling.volcano.sh/v1beta1`）：一组需要一起调度的 Pod 的最小成员数与最小资源。Volcano Job 会自动创建 PodGroup；非 Volcano 工作负载也可以手动创建并用注解关联。
- **Queue**（`scheduling.volcano.sh/v1beta1`）：集群级队列，承载权重、能力上限、保底、是否可回收等配额语义。

围绕这三个对象，Volcano 提供了 gang 调度、队列公平、优先级抢占、跨队列回收、拓扑感知、NUMA、异构设备、DRA 配额等能力。

## 1.4 Volcano 的设计哲学

理解 Volcano 的实现，抓住四条主线就够了：

### 1.4.1 会话（Session）是调度的基本单位

每次调度不是“为某个 Pod 找节点”，而是打开一个 **Session**：

1. 从 informer cache 复制一份集群快照；
2. 执行配置好的 **actions**（enqueue、allocate、preempt、reclaim、backfill、shuffle…）；
3. 每个 action 通过 **plugins** 注册的扩展点做决策；
4. 决策先写入事务性的 **Statement**，满足条件才 commit；
5. 关闭 Session，把绑定结果写回 API Server。

这解释了为什么 Volcano 能做 gang：一个作业的所有 Pod 在同一个 Session 里被“试分配”，全部成功才提交，否则整体回滚。

### 1.4.2 插件按“层”（tier）组合

Volcano 的调度策略不是写死的 if/else，而是插件注册的函数。多个插件可能同时注册 `JobOrderFn`，于是配置里用 **tier** 规定优先级：先按 tier 1 的插件比较，平手再看 tier 2。这是 Volcano 可扩展性的根。

### 1.4.3 队列是资源治理的第一公民

Queue 的 `weight`、`capability`、`deserved`、`guarantee`、`reclaimable` 五个字段，配合 proportion/capacity 插件，构成了“保底 + 上限 + 借用 + 回收”的完整租户模型。没有队列，Volcano 只是一个 gang 调度器；有了队列，它才是平台。

### 1.4.4 作业生命周期由控制器而不是调度器负责

调度器只负责“能不能跑、放哪”。作业是否重启、失败后是否重试、Pod 被驱逐后是否整体重来，由 `vc-controller-manager` 的 Job controller 根据 `policies` 决定。调度与生命周期解耦，是 Volcano 能同时支持训练、HPC、大数据作业的原因。

## 1.5 Volcano 与相邻项目

| 项目 | 核心模型 | 与 Volcano 的关系 |
|---|---|---|
| **Kueue** | 作业准入 + 配额，Pod 交给 kube-scheduler | 常与 Volcano 组合：Kueue 决定“何时准入”，Volcano 决定“如何放置” |
| **Apache YuniKorn** | 独立批调度器，队列/公平 | 同类竞品，生态与社区规模不同 |
| **Slurm** | HPC 原生调度器 | 高性能计算事实标准；Volcano 试图把类似能力带入 K8s |
| **KAI Scheduler** | GPU 共享与 gang | 更聚焦 GPU 虚拟化/共享 |
| **scheduler-plugins** | kube-scheduler 插件集（含 coscheduling） | 轻量 gang，缺少完整队列/回收体系 |
| **Ray / Kubeflow Trainer** | 框架层 | 可把 Volcano 作为底层 batch scheduler |

一个常见的企业架构是：

```text
用户提交 Job
   │
   ├─ Kueue：租户配额、准入、borrow/lend
   │
   └─ Volcano：gang、拓扑、NUMA、异构设备、抢占/回收
          │
          └─ kubelet / device plugin / DRA driver
```

两者并非互斥。Kueue 的强项是“准入与配额治理”，Volcano 的强项是“放置与作业级调度”。组合时要小心两套配额系统各算各的，容易死锁——这是第 31 章会展开的商业与架构话题。

## 1.6 什么时候该用 Volcano

适合：

- 分布式训练（PyTorch、TensorFlow、MPI、Ray、MindSpore、PaddlePaddle）。
- HPC/CAE/基因/渲染等批处理作业。
- 多租户 GPU 平台，需要队列配额、借用、回收、抢占。
- 需要 gang、拓扑感知、NUMA、异构 GPU 的集群。
- 已经在 Kubernetes 上，但默认调度器的 Pod 级模型成为瓶颈。

不一定适合：

- 只有无状态在线服务，没有批作业。
- 只需要简单的队列/配额，且希望最小侵入——Kueue 可能更合适。
- 团队没有能力运维第二个调度器，且云厂商托管方案已经满足需求。
- 作业规模很小、资源充足，gang/公平带来的复杂度大于收益。

## 1.7 本教程怎么读

教程分九部分：

1. **基础（第 1–5 章）**：定位、架构、安装、Job、Queue。
2. **调度内核（第 6–14 章）**：gang、框架、公平、抢占、打分、拓扑、预留/SLA/TDM、异构/DRA、作业生态。
3. **工程与运维（第 15–20 章）**：控制器、可观测性、安全、性能、生产落地、多集群。
4. **可运行的 MiniVolcano（第 21–25 章）**：从零实现一个工程化的简化调度器，28 个可执行场景、141 个测试通过、随机不变量验证。
5. **形式化与实验（第 26–27 章）**：资源偏序/DRF/回收安全性/13 条不变量，14 个可复现实验。
6. **迁移与商业（第 28–32 章）**：迁移决策树、容量规划、未来方向、市场/竞争/TCO。
7. **扩展与上游工程化（第 33 章）**：扩展层选择、CRD/API 变更链、插件/Action、测试金字塔、版本与发布、上游协作。
8. **大模型工作负载（第 34 章）**：预训练/微调/推理/Agent 的作业形状、并行策略、gang 尺寸、checkpoint/弹性、容量模型与参考架构。
9. **附录（第 35 章）**：API、配置、术语、命令、参考资料，以及混合负载综合轨迹（T）、容量/ROI 计算器（U）、生产故障演练剧本（V）、任务级 gang 设计笔记（W）。

如果你只想快速跑起来，读第 3 章、第 23 章和第 27 章；如果你要二次开发，读第 7、21、22、24、33 章（第 7 章有调度器插件开发、源码地图与真实 Volcano 插件骨架，第 22 章有 MiniVolcano 的 shuffle/PDB/分组拓扑走读，第 33 章讲调度器插件、Action、作业插件的工程链与上游协作）；如果你要做技术选型，读第 1、19、28、30–32 章；如果你要做 LLM 平台，读第 13、34 章，再回到第 19 章看平台 API 与配额治理。附录还提供配置配方（L）、FAQ（M）、提交作业的三种方式（N）、升级清单（O）、功能覆盖矩阵（P）、Kubernetes 调度基础（Q）、错误信息索引（R）、自测题（S）、混合负载综合轨迹（T）、容量/ROI 计算器（U）与生产故障演练剧本（V）、任务级 gang 设计笔记（W）。

## 1.8 本章小结

- Volcano 解决的是**作业级**而非 Pod 级的调度问题。
- 它的四个支点是 Session、tier/plugin、Queue、controller。
- 它和 Kueue 不是替代关系，常见组合是“Kueue 准入 + Volcano 放置”。
- 是否采用，取决于你是否真的需要 gang、租户公平、拓扑与异构设备调度。

下一章从组件和对象出发，建立一张完整的架构地图。
