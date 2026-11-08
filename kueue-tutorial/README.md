# Kueue 深入教程：从队列语义到可运行的 MiniKueue

> 面向 Kubernetes 批处理与 AI/ML 平台工程师。既讲清 Kueue 的 API、调度语义、控制器内部和运维方法，也带你从零实现一个有测试、有场景、可运行的简化控制面。

## 这套教程解决什么问题

Kubernetes 的默认调度器以 Pod 为单位工作。它擅长回答"这个 Pod 应该放在哪个节点"，却不擅长回答平台团队真正关心的问题：

- 这个团队的 8 卡训练作业什么时候能开始？
- 两个团队共享一批 GPU 时，谁应该先用？
- 集群满了以后，高优先级作业能否抢占低优先级作业？
- 一个作业的 32 个 Pod 是否应该一起等待、一起启动？
- 某个团队暂时不用配额时，能否借给另一个团队？原团队需要时如何收回？
- 作业在多个集群之间如何排队和分发？
- 配额是否应该随云上实际容量动态变化？

Kueue 是 Kubernetes SIG Scheduling 下的作业级排队与配额管理项目。它不替代 `kube-scheduler`，而是在 Pod 被创建之前，决定一个 **Workload** 是否应该被准入（admit），并为它预留逻辑配额。准入之后，Pod 才被放行，交给 `kube-scheduler` 做节点放置。

这套教程有十条主线：

1. **概念与语义**：Workload、LocalQueue、ClusterQueue、ResourceFlavor、Cohort、AdmissionCheck、Topology 等对象如何配合。
2. **算法与工程**：队列管理、配额缓存、公平分享、抢占、回收、两阶段准入、TAS、DQO、可观测性。
3. **内部原理**：QueueManager → Cache Snapshot → Scheduler → 提交决策的主干，以及控制器、webhook、Job 集成。
4. **可运行实现**：`mini-kueue/` 用 Go 实现了一个不依赖 Kubernetes 的确定性模拟器，覆盖核心控制面逻辑，并配有单元测试、随机不变量测试、26 个引擎场景和 2 个 MultiKueue 多集群演示。
5. **验证与质量**：不变量、随机化、差分、变形、故障注入、race 和性能门禁，以及从绿测试中找出真实缺陷的方法。
6. **物理落地**：Kueue 与 kube-scheduler、TAS、DRA、ProvisioningRequest 的分层协作，以及"Admitted 但 Pod 起不来"的诊断方法。
7. **容量与迁移**：排队论/离散事件模拟、配额评审方法，以及从 MiniKueue 到 controller-runtime 生产控制器的工程化迁移路径。
8. **运维与灾备**：`kueuectl` 工作流、自动化脚本、HA/leader election、备份恢复、升级回滚和故障演练。
9. **多集群、扩展与推理**：MultiKueue 的对象/协议/故障语义、AdmissionCheck 与 Job 集成开发、client-go 自动化、推理/弹性/混合负载的接入边界。
10. **可解释性与原生 WAS**：`-explain`/`-what-if` 如何把"Pending"变成证据链；Kubernetes 1.36/1.37 的原生 Workload/PodGroup 与 Kueue 的双轨期。

## 本版亮点

这一版在原有教程基础上做了十余件事：

- **补全纳管边界（本轮新增）**：第 8.5 节从"没有标签就不管"扩展为五种机制：显式 `queue-name`、默认 LocalQueue（`LocalQueueDefaulting`）、默认 WorkloadPriorityClass（`WorkloadPriorityClassDefaulting`）、`manageJobsWithoutQueueName` + `managedJobsNamespaceSelector`、ValidatingAdmissionPolicy。给出可复制的 VAP/CEL、默认队列/优先级 YAML、执行顺序、选型矩阵和六类失败模式；新增 `examples/kueue/enforce-management.yaml` 与 Lab 6 的纳管边界实验，并修正了"`manageJobsWithoutQueueName` 会使用默认队列"的旧表述。
- **配置工程（本轮新增）**：第 3.6 节补充 Configuration 的真实加载路径（`kueue-manager-config` 的 `controller_manager_config.yaml` 键、启动时读取、Helm 整块覆盖）、安全变更/回滚流程，以及 `featureGates` 在 v1beta2 是顶层 `map[string]bool`、不能与 `--feature-gates` 并用的约束。新增可运行的 `scripts/check_kueue_config.py`，检查 gate 依赖反转、危险纳管组合、保留资源名 `pods`、非法 duration 等；`verify_all.sh` 用正反两个 fixture 验证检查器本身。修正附录中把 `featureGates` 放在 `controllerManager` 下的错误。
- **三轮独立审计修复（本轮新增）**：统一 v1beta2 的 `status.admissionChecks`、`spec.admissionChecksStrategy`、`deviceClassMappings`（Configuration 字段）等事实；修正 kueuectl 命令面、`.totalQuota`、原生 PodGroup `minCount`、WAS 版本线、Cohort 叙事、TAS/GPU 实验、请求重试与 JobSet 映射等 30 余处问题。Fair Sharing 的 `lendable` 分母改为真实 Kueue 的 nominal 语义，并同步更新测试。
- **新增工程章节/小节（本轮新增）**：3.2.4 离线/气隙安装、10.4.1 控制器/Webhook 故障行为、11.3.1 ResourceQuota 共存、11.3.2 namespace/LQ 生命周期与孤儿配额、21.8.1 Showback/Chargeback、27.12 真实 Kueue 的 envtest/e2e 测试栈、5.6.1 `flavorFungibility` worked example。
- **新增示例（本轮新增）**：`enforce-management.yaml`、`jobset.yaml`、`dra.yaml`，以及 `examples/config/` 下的完整配置和片段；`verify_docs.py` 增加章节编号、README 计数和过期术语检查。
- **新增第 37 章"调度决策的可解释性"（本版新增）**：MiniKueue 新增 `-explain` 与 `-what-if`。诊断与准入共用 `evaluateFlavor`，逐资源输出 CQ nominal、Cohort 总量、borrowing/lending limit、可借资源与首个失败约束；控制器层再叠加 inactive/gate/stopPolicy/AdmissionCheck。`-what-if` 从 tick 0 重放同一确定性场景，对单变量配额覆盖做前后对比。对应 9 个新测试和本章的四种实战报告。
- **新增第 38 章"原生 WAS 与 gang scheduling"（本版新增）**：讲清 Kubernetes 1.36（Alpha）/1.37（Beta，`scheduling.k8s.io/v1beta1`，`GenericWorkload` 默认关闭）的 Workload/PodGroup、`CompositePodGroup` Alpha 与 `DRAWorkloadResourceClaims` Beta；Kueue 标签式 pod group 与原生 PodGroup 的双轨期、对象/单位对齐、RFC #12385 三阶段、版本 skew、迁移与回滚、故障模式和 MiniKueue 对照实验。
- **事实与版本校准（本版新增）**：稳定线改为 v0.20.x（2026-11 最新 patch v0.20.5；v0.21 仅 devel）；v1beta2 的 Workload 优先级字段改为 `priorityClassRef`；`MultiKueueOrchestratedPreemption` 更新为 Beta 默认开、timeout 可配置；DRA 基线更新到 Kubernetes 1.34 GA/1.37 Beta；StatefulSet 更正为"Kueue 不支持扩缩、用 Plain Pod Group"；补充 `storedVersions` 清理、`flavorsReservation`/`flavorsUsage` 查询路径、TASResidualPodAccounting 的 alpha 状态。
- **文档一致性修复（本版新增）**：修正第 13/14/15/19/27 章中"已实现却写成未实现"的矛盾（指数退避、Fair Sharing 抢占、Cohort 权重、ProvisioningRequest-lite、Configurable Preemption-lite）；按实际代码重写 `TotalUsage`、`canFitFlavor`、DRA 计费与 `AdmittedAt` 语义的走读片段；校准测试计数与基准叙事；补 partial slice 回归测试。
- **新增第 31～36 章**：第 31 章 kueuectl 运维；第 32 章 HA/灾备；第 33 章实现补全；第 34 章 MultiKueue 深入；第 35 章扩展开发；第 36 章推理/弹性。原有 30 章编号不变。
- **MiniKueue 能力与对抗性审计**：资源变换、ProvisioningRequest-lite、Configurable Preemption-lite、Fair Sharing 抢占-lite、可配置指数退避、MultiKueue failover、TAS slice/打散、DRA-lite、AFS-lite、Concurrent-lite、admission gate 等均已实现并有测试；两轮独立审计累计修复 30 类"测试全绿但语义错误"的缺陷，细节见 [VERIFICATION.md](VERIFICATION.md) 第 7 节。

## 先跑起来

### 运行 MiniKueue

需要 Go 1.24+（本教程在 Go 1.27 上验证）。

```bash
cd mini-kueue
go test ./...
go run ./cmd/minikueue -list
go run ./cmd/minikueue -scenario basic -ticks 4
go run ./cmd/minikueue -scenario tas -ticks 2
# 解释为什么某个作业 Pending
go run ./cmd/minikueue -scenario basic -ticks 1 -explain default/job-c
# 配额反事实：把 basic/default 的 CPU nominal 改成 3000
go run ./cmd/minikueue -scenario basic -ticks 1 -what-if "basic/default/cpu=3000"
```

### 在真实 Kubernetes 上体验 Kueue

```bash
# 需要 Kubernetes 集群和 kubectl；kind/minikube 均可
# v0.20.5 是 2026-11 的示例稳定版本，请替换成你要安装的 release
export KUEUE_VERSION=v0.20.5
kubectl apply --server-side -f \
  "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VERSION}/manifests.yaml"
kubectl wait deploy/kueue-controller-manager -n kueue-system \
  --for=condition=available --timeout=5m

kubectl apply -f examples/kueue/quick-start-setup.yaml
kubectl apply -f examples/kueue/quick-start-job.yaml
kubectl get workloads -n default
```

> 版本提示：Kueue 迭代很快。教程以 **v1beta2 API** 为主线，涉及 alpha/beta 特性时会标注。安装命令中的版本号请替换成你实际使用的 release；升级前务必阅读目标版本的 release notes。

## 知识地图

```text
                    ┌──────────────────────────────┐
                    │ 01 为什么需要 Kueue           │
                    │ 02 核心对象与资源模型         │
                    └──────────────┬───────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          ▼                        ▼                        ▼
┌──────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
│ 03 安装/快速上手  │   │ 04 准入生命周期       │   │ 05 配额/Flavor/Cohort │
└──────────────────┘   └──────────┬───────────┘   └──────────┬───────────┘
                                  │                          │
                                  ▼                          ▼
                       ┌──────────────────────┐   ┌──────────────────────┐
                       │ 06 排队与公平分享     │   │ 07 抢占与回收         │
                       └──────────┬───────────┘   └──────────┬───────────┘
                                  └────────────┬─────────────┘
                                               ▼
                       ┌─────────────────────────────────────────┐
                       │ 08 作业集成  09 高级能力  10 运维/指标   │
                       │ 11 安全      12 性能      17 控制器内部  │
                       └────────────────────┬────────────────────┘
                                            │
              ┌─────────────────────────────┼─────────────────────────────┐
              ▼                             ▼                             ▼
   ┌──────────────────────┐      ┌──────────────────────┐      ┌──────────────────────┐
   │ 13-15 MiniKueue 设计 │      │ 18 生产落地           │      │ 20 语义边界           │
   │ 实现、差距            │      │ 21 设计模式           │      │ 22 真实集群实验       │
   │ 19 扩展实验室         │      │ 23 附录               │      │ 24 API 迁移           │
   └──────────┬───────────┘      └──────────┬───────────┘      │ 25 集成实战           │
              │                             │                  │ 26 端到端案例         │
              └──────────────┬──────────────┘                  └──────────┬───────────┘
                             ▼                                            │
                  ┌──────────────────────┐                              │
                  │ 27 验证工程           │◄─────────────────────────────┘
                  └──────────┬───────────┘
                             ▼
         ┌──────────────────────────────────────────────┐
         │ 28 物理落地（kube-scheduler/TAS/DRA/自动扩缩）│
         │ 29 排队论与容量工程（可运行模拟器）           │
         │ 30 从 MiniKueue 到生产控制器                  │
         │ 31 kueuectl 与自动化运维                      │
         │ 32 高可用、灾备与一致性                       │
         │ 33 实现补全（变换/置备/可配置抢占/公平抢占）  │
         │ 34 MultiKueue 多集群深入                      │
         │ 35 扩展开发（AdmissionCheck/Job/client-go）   │
         │ 36 推理、弹性与混合负载                       │
         │ 37 调度决策可解释性（-explain/-what-if）      │
         │ 38 原生 WAS 与 gang scheduling 双轨期         │
         └──────────────────────────────────────────────┘
```

阅读时不必严格按编号：概念 → 语义 → 实现 → 验证 → 物理落地 → 生产化，是一条推荐的主线；第 20～27 章可以当作遇到问题时回查的"深水区"，第 37～38 章是运维诊断与 2026 年架构变化的专门补充。

## 章节目录

### 第一部分：概念与语义

| 章 | 主题 | 你会学到 |
|---|---|---|
| [01](docs/01-why-kueue.md) | 为什么需要 Kueue | 作业级排队、配额、公平性与抢占的问题域；Kueue 与调度器、ResourceQuota、Volcano 的边界 |
| [02](docs/02-core-objects.md) | 核心对象与资源模型 | Workload、LocalQueue、ClusterQueue、ResourceFlavor、Cohort、AdmissionCheck、Topology；静态配额与 effectiveQuotas |
| [03](docs/03-install-and-quickstart.md) | 安装与快速上手 | 安装、离线/气隙安装、Configuration 安全变更与静态检查、最小清单、提交 Job、观察生命周期 |
| [04](docs/04-admission-lifecycle.md) | 准入生命周期 | 配额预留与 AdmissionCheck 两阶段准入；条件、驱逐、重排队、StopPolicy、WaitForPodsReady、动态回收 |

### 第二部分：配额与调度

| 章 | 主题 | 你会学到 |
|---|---|---|
| [05](docs/05-quota-cohorts.md) | 配额、Flavor 与 Cohort | 资源组、借用/出借、层级 Cohort、共享配额、DQO、常见误区 |
| [06](docs/06-scheduling-and-fairness.md) | 排队、调度与公平分享 | StrictFIFO/BestEffortFIFO、优先级、加权 DRS、Admission Fair Sharing |
| [07](docs/07-preemption-reclaim.md) | 抢占与回收 | Classic Preemption、reclaim、borrowWithinCohort、Fair Sharing 抢占、可配置抢占 |
| [08](docs/08-workload-integrations.md) | 作业集成 | Job、JobSet、Kubeflow、Ray、Pod、Deployment、LeaderWorkerSet 与自定义集成；默认队列、默认优先级、namespace 纳管与 VAP 强制策略 |

### 第三部分：高级能力与生产

| 章 | 主题 | 你会学到 |
|---|---|---|
| [09](docs/09-advanced-features.md) | 高级能力 | TAS、DRA、ProvisioningRequest、MultiKueue、Concurrent Admission、DQO、Elastic Workload |
| [10](docs/10-operations.md) | 运维与可观测性 | 指标、事件、排障路径、控制器/Webhook 故障行为、容量分析、升级与卸载 |
| [11](docs/11-security-multitenancy.md) | 安全与多租户 | RBAC、namespaceSelector、ResourceQuota 共存、namespace/LQ 生命周期、优先级滥用、MultiKueue 凭据、威胁模型 |
| [12](docs/12-performance-scale.md) | 性能与规模 | 控制器缓存、快照、复杂度、性能测试、调优与容量规划 |

### 第四部分：MiniKueue 实现

| 章 | 主题 | 你会学到 |
|---|---|---|
| [13](docs/13-mini-kueue-design.md) | MiniKueue 设计 | 需求、分层、状态机、配额算法、抢占、TAS、就绪/回收、DQO-lite、DRA-lite、AFS-lite、Concurrent/MultiKueue-lite、并发模型与取舍 |
| [14](docs/14-mini-kueue-walkthrough.md) | MiniKueue 实现与验证 | 代码走读、26 个引擎场景 + 2 个 MultiKueue 演示、测试策略、不变量、扩展方式 |
| [15](docs/15-gap-to-real-kueue.md) | 与真实 Kueue 的差距 | 覆盖矩阵、语义差异、生产化缺口、从模拟器到控制器的路径 |
| [16](docs/16-future-roadmap.md) | 现状与未来方向 | 分层公平、DRA、TAS、多集群、动态配额、弹性作业与生态趋势 |

### 第五部分：深入、落地与实验

| 章 | 主题 | 你会学到 |
|---|---|---|
| [17](docs/17-kueue-internals.md) | Kueue 控制器内部原理 | 源码地图（v0.20.x）、QueueManager、Cache Snapshot、Scheduler 调用链、Workload 控制器、Job 集成、并发与故障恢复 |
| [18](docs/18-production-adoption.md) | 生产落地手册 | 分阶段落地、容量模型、配额合同、迁移、SLO、变更管理、Runbook |
| [19](docs/19-mini-kueue-extension-lab.md) | MiniKueue 扩展实验室 | 已实现的 TAS slice/partial slice/打散、DRA-lite、MultiKueue-lite；进阶实验：多层 slice、热替换、ProvisioningRequest-lite、资源变换、Configurable Preemption、AlmostLCA、子树隔离共享池；Quantity/controller-runtime 迁移 |
| [20](docs/20-scheduling-edge-cases.md) | 调度语义边界、失败分析与不变量 | 配额/队列/抢占/公平/生命周期/TAS/DRA/一致性边界；不变量目录与失败分析决策树 |
| [21](docs/21-platform-design-patterns.md) | 平台队列与配额设计模式 | 独占池、共享借用、层级组织、关键业务保护、弹性突发、多集群、成本/碳排与 Showback/Chargeback、训练推理混部；反模式与选型矩阵 |
| [22](docs/22-hands-on-labs.md) | 真实集群实验手册 | 12 个核心 kind 实验 + 8 个可选实验：安装、生命周期、FIFO、Cohort、抢占、selector、AdmissionCheck、TAS、DRA、MultiKueue、可观测性、故障演练、层级 Cohort、AFS、gate、Concurrent Admission、资源变换、ProvisioningRequest、Configurable Preemption、Fair Sharing 抢占 |
| [23](docs/23-appendix.md) | 附录 | 术语表、字段/gate/指标速查、kubectl/kueuectl 命令、练习与参考资料 |

### 第六部分：迁移、集成与端到端案例

| 章 | 主题 | 你会学到 |
|---|---|---|
| [24](docs/24-api-migration-upgrade.md) | API 迁移与版本升级 | v1beta1→v1beta2 字段映射、CRD 升级顺序、feature gate 变更、双版本过渡、回滚与验证 |
| [25](docs/25-integration-cookbook.md) | 生态集成实战 | JobSet、KubeRay、Kubeflow Training、LeaderWorkerSet、MPI、Pod/Deployment 的接入清单与常见坑 |
| [26](docs/26-end-to-end-case-study.md) | 端到端案例 | 从 96 卡 GPU 池到多租户平台：容量模型、对象拓扑、完整清单、上线、事故复盘与演进 |

### 第七部分：验证与质量

| 章 | 主题 | 你会学到 |
|---|---|---|
| [27](docs/27-verification-engineering.md) | 验证工程 | 不变量、随机化、差分测试、变形测试、故障注入、race/性能门禁；真实 Kueue 的 envtest/集成/e2e 测试栈；十个"绿测试掩盖"的真实缺陷与修复 |

### 第八部分：物理落地、容量与生产化

| 章 | 主题 | 你会学到 |
|---|---|---|
| [28](docs/28-physical-scheduling-layer.md) | 物理落地与调度器协作 | Kueue 与 kube-scheduler 的分层边界；suspend/scheduling gate；Flavor 注入；三层 gang；TAS/DRA/ProvisioningRequest 的真实放置；分层诊断决策树 |
| [29](docs/29-queueing-capacity-engineering.md) | 排队论与容量工程 | λ/S/c/ρ、Little 定律、Erlang C 的理想与局限、碎片概率、StrictFIFO vs BestEffortFIFO 的量化对比、借用流体模型、可运行的容量模拟器 |
| [30](docs/30-mini-to-production-controller.md) | 从 MiniKueue 到生产控制器 | CRD/status/webhook、controller-runtime 装配、事件驱动调度循环、Job 集成、AdmissionCheck 控制器、envtest/集成/故障测试、分阶段上线与回滚 |

### 第九部分：运维、灾备与实现补全

| 章 | 主题 | 你会学到 |
|---|---|---|
| [31](docs/31-kueuectl-operations.md) | kueuectl 与自动化运维 | 命令地图、conditions/事件查询、stop/resume、配额变更顺序、等待/日报/审计脚本、MiniKueue CLI 映射 |
| [32](docs/32-high-availability-dr.md) | 高可用、灾备与一致性 | 故障域、leader election、webhook HA、幂等与 finalizer、etcd/对象备份、恢复顺序、RTO/RPO、升级回滚、故障演练 |
| [33](docs/33-implementation-completion.md) | 实现补全 | 资源变换、ProvisioningRequest-lite、Configurable Preemption-lite、Fair Sharing 抢占、指数退避、CLI 扩展、工程原则与剩余差距 |

### 第十部分：多集群、扩展与推理

| 章 | 主题 | 你会学到 |
|---|---|---|
| [34](docs/34-multikueue-deep-dive.md) | MultiKueue 多集群调度深入 | MultiKueueCluster/Config/AdmissionCheck 对象、端到端分发时序、AllAtOnce/Incremental/External、worker 选择、一致性/GC、失联超时与重复执行、配额/公平/编排式抢占、凭据安全、生产部署、MiniKueue failover |
| [35](docs/35-kueue-extension-development.md) | Kueue 扩展开发 | AdmissionCheck 两个 reconciler、参数 CRD、状态机、GenericJob 自定义集成、client-go informers、webhook、envtest、RBAC/HA/发布与兼容性 |
| [36](docs/36-inference-elastic-workloads.md) | 推理、弹性与混合负载 | Deployment 逐 Pod 管理、StatefulSet/LWS/PodGroup、Partial Admission、Elastic Workload Slices、TAS 打散、DRA、HPA/KEDA 边界、推理容量与设计模式 |

### 第十一部分：可解释性与原生 WAS

| 章 | 主题 | 你会学到 |
|---|---|---|
| [37](docs/37-decision-explainability.md) | 调度决策的可解释性 | 决策谓词/见证/不可行证书、`-explain` 的四类报告、`-what-if` 反事实、与真实 Kueue 的证据源映射、诊断测试与复杂度 |
| [38](docs/38-native-was-gang-scheduling.md) | 原生 WAS 与 gang scheduling | Kubernetes 1.36/1.37 的 Workload/PodGroup、`GenericWorkload` gate、Kueue 双轨期、对象/单位对齐、RFC #12385、版本 skew、迁移回滚、故障模式与 MiniKueue 实验 |

## 目录结构

```text
kueue-tutorial/
├── README.md                     # 本文件
├── docs/                         # 38 章教程
├── examples/
│   ├── kueue/                    # v1beta2/v1alpha1 清单与说明
│   │   ├── ...                   # 22 个队列/抢占/高级能力示例
│   │   ├── enforce-management.yaml
│   │   ├── jobset.yaml
│   │   ├── dra.yaml
│   │   ├── multikueue.yaml       # MultiKueue 对象骨架
│   │   ├── custom-admission-check.yaml
│   │   └── inference-workloads.yaml
│   ├── config/                   # Configuration 片段（ConfigMap payload）
│   └── README.md                 # 示例说明
├── mini-kueue/                   # 可运行的简化实现
│   ├── cmd/minikueue/            # CLI 演示入口（scenario/get/describe/stop/resume）
│   ├── internal/api/             # Kubernetes 风格的 API 类型与校验
│   ├── internal/store/           # 内存 API server（含 Topology/CapacityProvider）
│   ├── internal/cache/           # 配额与拓扑账本
│   ├── internal/queue/           # 队列排序
│   ├── internal/scheduler/       # Flavor 分配、Cohort 借用、TAS、Classic/Configurable/Fair 抢占
│   ├── internal/controller/      # 协调循环、两阶段准入、置备/退避、就绪/回收
│   ├── internal/metrics/         # 指标
│   ├── internal/multikueue/      # Manager/worker 多集群分发模拟
│   └── internal/scenario/        # 可复现场景
├── scripts/                      # 校验脚本 + 容量模拟器 + 配置检查器
│   ├── verify_all.sh
│   ├── verify_docs.py
│   ├── check_kueue_config.py     # Configuration 静态检查
│   ├── testdata/                 # 检查器的反例
│   └── capacity_model.py         # 排队论/离散事件容量模拟
└── VERIFICATION.md               # 实际验证记录
```

## MiniKueue 场景

```bash
go run ./cmd/minikueue -list
```

| 场景 | 演示内容 |
|---|---|
| `basic` | FIFO 准入、配额耗尽、完成后释放 |
| `cohort-borrow` | ClusterQueue 借用 Cohort 空闲配额 |
| `preemption` | 高优先级抢占同 CQ 低优先级 |
| `reclaim` | 原主收回被借走的 nominal |
| `admission-check` | 配额预留 + 手动 AdmissionCheck |
| `strict-fifo` | 队头阻塞 |
| `best-effort-fifo` | 跳过放不下的队头 |
| `tas` | 4 卡作业跨 host 占满一个 rack |
| `pods-ready` | WaitForPodsReady 超时驱逐 |
| `dynamic-reclaim` | 作业回收 Pod 并释放配额 |
| `dynamic-quota` | CapacityProvider 缩小有效配额 |
| `label-selector` | matchLabels + matchExpressions 的 namespaceSelector |
| `localqueue-hold` | LocalQueue stopPolicy 只阻塞自己的 Workload |
| `retry-backoff` | AdmissionCheck Retry 的 requeueAfterSeconds 退避 |
| `dra` | DeviceClass 映射到逻辑配额资源，避免重复计费 |
| `tas-slice` | TAS slice 与跨 Workload 打散 |
| `admission-fair-sharing` | 按 LocalQueue 历史用量排序准入 |
| `concurrent-admission` | spot 先运行，reserved 空出后迁移 |
| `admission-gate` | 配额预留前的外部闸门与清除 |
| `hierarchical-limits` | Cohort 级 borrowing/lending limit |
| `implicit-cohort` | 只有 cohortName、没有 Cohort 对象的隐式 Cohort |
| `cohort-shared-pool` | 显式 Cohort 用自己的 nominal 作为成员共享池 |
| `configurable-preemption` | PreemptionConfig 规则在 Classic 全关时授权抢占 |
| `fair-sharing-preemption` | Fair Sharing 按份额收回被借走的容量 |
| `provisioning-request` | 预留期间置备、失败重试、延迟 Ready |
| `resource-transform` | vGPU 请求折算成总显存/总算力配额 |
| `multikueue` | manager/worker 提名、选中与失败副本清理 |
| `multikueue-failover` | 选中 worker 失联、超时切换、恢复后陈旧副本清理 |

## 阅读建议

- **第一次接触 Kueue**：按 01 → 02 → 03 → 04 顺序读，先把"对象、准入、配额、队列"四件事建立直觉。
- **要理解公平/抢占**：读 05 → 06 → 07 → 20，重点看"两套 Fair Sharing""四条抢占启发式"和边界案例。
- **平台架构师**：重点读 05、09、11、12、17、18、21，关注多租户、异构资源、控制器行为、落地方法和设计模式。
- **想自己实现**：先读 13，再对照 14 的代码；把 MiniKueue 跑起来，改一个场景，再加一个测试；最后做 19 的实验。
- **运维排障**：直接看 10、18 和 20.10 的决策树，再回到 04/05/07/17 找语义和内部原因。
- **要上真实集群**：按 22 的 Lab 顺序做一遍，再回到 18 做容量模型和变更管理。
- **要升级版本**：先读 24 的迁移地图和检查清单，再做 staging round-trip。
- **要接作业框架**：读 25 的集成契约、YAML 骨架和验收清单。
- **要设计纳管策略**：读 8.5 的五种机制（默认队列/默认优先级/namespace selector/VAP），再读 11 的权限边界和 18 的迁移路线；用 `examples/kueue/enforce-management.yaml` 验证执行顺序。
- **要改 Kueue 配置**：读 3.6 的加载/变更流程和 23.2.3 的字段表，提交前跑 `scripts/check_kueue_config.py --strict`。
- **要看完整落地案例**：读 26，从 96 卡容量模型一路走到事故复盘。
- **要验证自己的实现**：读 27，先建立不变量目录，再加随机化、差分和故障注入；用 27.10 的缺陷模式自查。
- **要排查"Admitted 了但 Pod 起不来"**：读 28，按"Kueue → Job 集成 → kube-scheduler → kubelet → DRA/autoscaler"分层定位。
- **要做容量规划/配额评审**：读 29，用 `scripts/capacity_model.py` 跑基线、变体和峰值场景，再决定 nominal/borrowingLimit。
- **要把模拟器变成生产控制器**：读 30，按 CRD → manager → 调度循环 → Job 集成 → webhook → 测试 → 分阶段上线的顺序推进。
- **要日常运维/排障**：读 31，把 `kueuectl`、conditions、JSONPath、stop/resume 和自动化脚本串成工作流。
- **要做 HA/灾备/升级**：读 32，先定 RTO/RPO，再设计 leader election、webhook HA、备份恢复顺序和故障演练。
- **要核对本版实现补全**：读 33，对照资源变换、ProvisioningRequest、Configurable Preemption、Fair Sharing 抢占的算法、测试和剩余差距。
- **要做多集群**：读 34，先定 manager/worker 边界和配额总闸门，再选 dispatcher、失联超时和凭据方案；用 `multikueue-failover` 场景验证状态机。
- **要写扩展**：读 35，按 AdmissionCheck 两个 reconciler → 参数 CRD → envtest → RBAC/HA → Job 集成/client-go 的顺序推进。
- **要上推理/弹性**：读 36，先确认 Deployment/StatefulSet/LWS/PodGroup 的真实管理方式，再决定 Partial Admission 还是 Elastic Slices，并给 HPA/KEDA 留出边界。
- **要解释"为什么 Pending"**：读 37，先看 `-explain` 的分层报告与 `-what-if` 的反事实语义，再把诊断 code 接进自己的告警/日报。
- **要规划原生 WAS/gang**：读 38，先确认 Kubernetes/Kueue/框架三层的 gate 与版本，再按"盘点 → 双写 → 对比 → 灰度 → 退场"迁移。

## 约定

- 资源数量中，CPU 用 millicore 表示（`1000m = 1`），内存用 MiB，其他资源用整数单位。
- `MiniKueue` 是**教学模拟器**，不是 Kueue 的 fork，也不能直接接管 Kubernetes 集群。它用内存对象替代 API server/etcd，用 `Tick` 替代控制器协调循环。
- 代码块中的 `<...>` 表示省略，不作为可执行清单。
- 真实 Kueue 的字段、默认值和 feature gate 可能随版本变化；以官方 API reference 和你部署的版本为准。
- 教程中的"MiniKueue 已实现"指教学语义，不等于生产可用；差距见第 15 章。特别地，层级 Cohort 的共享池按根树扁平聚合、Cohort lendingLimit 逐成员收紧、TAS group 视为硬约束、slice 固定最空优先；资源变换不支持 Quantity/小数，ProvisioningRequest 不连接云扩容，Configurable Preemption 无 preemptor selector，Fair Sharing 抢占无 AlmostLCA。

## 验证

```bash
# 一键校验（格式化、vet、测试、所有场景、文档/YAML）
bash scripts/verify_all.sh

# 配置静态检查
python scripts/check_kueue_config.py --strict examples/config/kueue-config.yaml

# 或分步执行
cd mini-kueue
gofmt -l .          # 应无输出
go vet ./...
go test ./... -count=1
go test ./... -cover
go test ./... -count=10
go test ./... -count=1 -shuffle=on
go test ./internal/scheduler -bench . -benchmem -run '^$'
# 或 make check / make cover / make stress / make bench
```

实际执行结果记录在 [VERIFICATION.md](VERIFICATION.md)。本次结果：`go test` 全部通过（146 个顶层测试 + 26 个场景子测试；按 `--- PASS` 全量统计还包含 selector 与 MultiKueue 子测试）、28 个 CLI 场景可运行、38 章文档与 26 个 YAML 通过校验，容量模拟器在固定 seed 下可复现。

容量模拟器的快速验证：

```bash
python scripts/capacity_model.py --nodes 12 --gpus-per-node 8 \
  --arrival-rate 0.5 --mean-duration 6 --job-size 8 --hours 2160 --json
```

## 参考资料

- Kueue 官方文档：<https://kueue.sigs.k8s.io/>
- Kueue GitHub：<https://github.com/kubernetes-sigs/kueue>
- Kueue v1beta2 API：<https://kueue.sigs.k8s.io/docs/reference/kueue.v1beta2/>
- Kubernetes 调度：<https://kubernetes.io/docs/concepts/scheduling-eviction/>
- Dynamic Resource Allocation：<https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/>
- JobSet：<https://jobset.sigs.k8s.io/>
- KubeRay：<https://docs.ray.io/en/latest/cluster/kubernetes/index.html>
- Kubeflow Training Operator：<https://www.kubeflow.org/docs/components/training/>
- Kubernetes 1.37 Workload-Aware Scheduling：<https://kubernetes.io/blog/2026/09/08/kubernetes-v1-37-advancing-workload-aware-scheduling/>
- Kueue KEPs：<https://github.com/kubernetes-sigs/kueue/tree/main/keps>
- Kueue WAS 集成 RFC：<https://github.com/kubernetes-sigs/kueue/issues/12385>
- Kueue v0.20 release notes：<https://github.com/kubernetes-sigs/kueue/releases/tag/v0.20.0>

---

开始阅读：[第 1 章 · 为什么需要 Kueue](docs/01-why-kueue.md)
