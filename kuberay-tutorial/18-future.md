# 第 18 章 未来方向：Roadmap 与生态趋势

本章基于 KubeRay v1.7.1（2026-09）和 Ray 2.59 的公开信息，讨论已经出现的功能和可以合理预期的方向。**不要把它当作承诺**；具体版本以官方 release notes 为准。

## 18.1 KubeRay 1.7 已经把什么做完了

- 四个 CRD（RayCluster/RayJob/RayService/RayCronJob）；
- autoscaler v2；
- RayJob 四种提交模式；
- RayService NewCluster 零停机升级；
- GCS FT（Redis，RocksDB alpha）；
- token auth / K8s RBAC 委托；
- NetworkPolicy / mTLS alpha；
- History Server：发布说明称 beta（feature gate 阶段以目标 tag 为准）；
- 批调度器集成（Volcano/YuniKorn/Kueue/scheduler-plugins/WAS）；
- APIServer v2 alpha、Dashboard experimental、kubectl-ray beta。

这意味着 KubeRay 已经跨过“能不能用”的阶段，进入“生产细节”的阶段。

## 18.2 近期路线（从 alpha/beta 到默认）

### 18.2.1 History Server

- v1.6 alpha，v1.7 beta；
- 目标：集群删除后仍能回放 Ray Dashboard；
- 趋势：更多存储后端、更好的 UI/API 覆盖、更低内存；
- 对平台的意义：RayJob 调试不再依赖“保留集群”。

### 18.2.2 增量升级

- v1.5 alpha，v1.7 beta；feature gate 的默认值在 v1.7 分支上有 promote/revert，按目标 tag 确认；
- 依赖 Gateway API；
- 趋势：更好的回滚、容量管理、失败处理；
- 对平台的意义：RayService 升级不再必须 2 倍资源。

### 18.2.3 安全能力

- mTLS（v1.7 alpha）；
- NetworkPolicy（v1.7 alpha）；
- K8s RBAC token auth（v1.6）；
- 趋势：默认更安全、证书轮换、审计；
- 对平台的意义：多租户/合规场景可用。

### 18.2.4 GCS FT

- Redis 成熟；
- RocksDB（v1.7 alpha）；
- active-passive head（master/REP-65 方向）；
- 趋势：更少的 Redis 依赖、更快的 head 恢复；
- 对平台的意义：降低对 Redis 运维的依赖。

### 18.2.5 调度

- Volcano/YuniKorn/Kueue/scheduler-plugins；
- Kubernetes WAS（v1.7 alpha）；
- NodeLabelDelivery（v1.8 alpha）；
- 趋势：与 Kubernetes 原生 gang scheduling 标准化；
- 对平台的意义：多租户队列/配额/抢占更统一。

### 18.2.6 RayCronJob

- v1.6 alpha，默认关闭；
- 目前只有 schedule/timeZone/suspend/jobTemplate；
- 趋势：并发策略、历史限制、startingDeadline；
- 对平台的意义：简单定时任务可以不用外部 Cron。

### 18.2.7 IPPR（In-Place Pod Resizing）

- Kubernetes 1.35 把原地 Pod resize 转为 stable；KubeRay 在 autoscaler v2 上提供 `ray.io/ippr` 注解（alpha）；
- 目标：不重建 Pod 就把 worker 的 CPU/memory 调大；
- 当前限制：只扩不缩、只支持 CPU/memory、需要节点余量、与 VPA 冲突；
- 趋势：逐步支持缩容、更多资源类型、注解字段迁到 `autoscalerOptions`；
- 对平台的意义：启动慢/状态重的 worker 可以少建 Pod，降低成本与冷启动。

### 18.2.8 Kubernetes 新原语

- 原生 sidecar（1.33 GA）改变日志/指标代理的启动与退出顺序；
- 容器级重启（1.35）支撑 `SidecarSubmitterRestart`；
- `schedulingGates` 把“等待配额/缓存”从 operator 逻辑下移到调度层；
- `trafficDistribution: PreferSameZone` 影响多 AZ 的 Serve 流量与成本；
- Pod 级资源（1.34 beta）改变调度记账，但 Ray 的探测仍以容器 requests 为主；
- 趋势：KubeRay 会越来越多地“透传”这些原语，而不是自己实现；
- 对平台的意义：很多运维问题会从 operator 层转移到 Kubernetes 层，排查边界要同步更新。

### 18.2.9 特性成熟度矩阵（v1.7.1）

把“已经存在”与“可以依赖”分开看。版本/发布时间以[第 4 章](04-kuberay-overview.md)为事实源，下表只补充成熟度与生产判断：

| 特性 | 成熟度 | Gate | 外部依赖 | 生产建议 | 章节 |
|---|---|---|---|---|---|
| autoscaler v2 | 稳定（Ray 2.48+ 默认） | 无 | 节点池/云 API | 可作为主路径 | 9、34 |
| RayJob 四种提交模式 | 稳定 | 无 | 无（Sidecar 需 K8s 1.35+ 才可容器级重启） | 可作为主路径 | 10 |
| RayService NewCluster | 稳定 | 无 | 无 | 默认升级方式 | 11 |
| 增量升级 | beta（v1.5 alpha） | `RayServiceIncrementalUpgrade`（按 tag 确认默认值） | Gateway API 实现 + in-tree autoscaler | 先在非关键服务验证 | 11、33 |
| History Server | beta/alpha 以 tag 为准（v1.6 alpha） | `RayClusterHistoryServer`（默认关闭） | 对象存储 | 作为调试/审计，不做关键路径 | 8、33 |
| GCS FT（Redis） | 稳定 | 无 | Redis 运维 | 关键集群可用 | 13 |
| GCS FT（RocksDB） | alpha | `GCSFaultToleranceEmbeddedStorage` | PVC、单写者 | 仅实验/评估 | 13 |
| K8s token auth | v1.6 | 无（字段开启） | Ray 2.55+、RBAC | 多租户可评估；RayJob/RayService 暂不支持 | 14 |
| mTLS | alpha | `RayClusterMTLS` | cert-manager | 合规场景先做 PoC | 5、14 |
| NetworkPolicy | alpha | `RayClusterNetworkPolicy` | 支持 NP 的 CNI | 先验证 DNS egress | 5、14 |
| RayCronJob | alpha | `RayCronJob`（默认关） | 无 | 简单定时任务可用，并发要自管 | 12 |
| 批调度（Volcano/YuniKorn/Kueue） | 稳定 | 无（WAS 除外） | 对应调度器 | 按现有生态选一个 | 12、29 |
| WAS | alpha | `KubernetesWAS` | K8s 版本支持 | 仅评估，不要与 `--batch-scheduler` 混用 | 29 |
| APIServer v2 | alpha | 无 | 无 | 平台适配层，锁定版本 | 16 |
| Dashboard | experimental | 无 | 无 | 内部工具，不作为 SLA 依赖 | 16 |
| kubectl-ray | beta | 无 | 无 | 运维辅助 | 16 |

读这张表的三个原则：

1. **gate 默认开 ≠ 稳定**。beta 默认开只表示“默认体验”，不表示语义不会再变；
2. **alpha 功能要单独监控/单独回滚**，不要和主路径共享同一个开关；
3. **外部依赖往往比功能本身更危险**：Gateway API、cert-manager、Redis、对象存储的故障会放大成 Ray 集群故障。

## 18.3 Ray 侧的趋势

### 18.3.1 Autoscaler v2

- Ray 2.48+ 默认；
- 与 KubeRay API 更紧密；
- 更好的多 group、label selector、优先级；
- 趋势：更细粒度的资源需求、GPU 拓扑感知。

### 18.3.2 Label Selector

- Ray 2.49 引入；
- KubeRay 1.5 顶层 `labels`/`resources`；
- 趋势：按标签调度、节点标签传递（NodeLabelDelivery）；
- 对平台的意义：GPU 型号/可用区/租户标签可以直接参与 Ray 调度。

### 18.3.3 Ray Serve / LLM

- Serve 的 autoscaling、dynamic batching、multi-app；
- LLM 推理的 GPU 共享/批处理；
- 与 vLLM/SGLang 等集成；
- 趋势：RayService 成为 LLM 推理的平台抽象；
- 对平台的意义：需要更强的 GPU 调度、模型缓存、流量管理。

### 18.3.4 Ray Data / Train

- 数据本地性、流式执行；
- 分布式训练/checkpoint；
- 与 KubeRay 的弹性/容错结合；
- 趋势：更少的“手动建集群”，更多“作业即集群”。

### 18.3.5 Ray 3.0？

社区有关于 Ray 3.0 的讨论，但截至本文写作时间没有可依赖的稳定 API。对平台的影响更可能是：

- 运行时内部改进；
- 更好的 K8s 集成；
- 更强的可观测性；
- 向后兼容的 API 演进。

不要把教程绑定到未发布的版本。

## 18.4 平台工程趋势

### 18.4.1 Kubernetes 原生

- Gateway API 替代 Ingress；
- Pod Security Standards；
- Kueue 成为队列标准；
- Workload-Aware Scheduling；
- 多集群/多租户。

### 18.4.2 声明式 + GitOps

- CR 作为唯一事实来源；
- 策略即代码（OPA/Kyverno）；
- 自动回滚/漂移检测；
- 审计。

### 18.4.3 可观测性

- OpenTelemetry 统一 traces/metrics/logs；
- eBPF 网络/性能；
- GPU/DCGM 指标；
- History Server 保留调试上下文。

### 18.4.4 Serverless / 弹性

- 按需创建 Ray 集群；
- 缩容到零（head 也停）；
- 冷启动优化；
- 与 Knative/KEDA 等结合（需要仔细处理 Ray 的有状态性）。

## 18.5 可能的变化与风险

- **CRD 变大**：更多字段/默认值，升级需要谨慎；
- **alpha 功能默认开启**：行为变化；
- **Gateway API 依赖**：集群必须有 Gateway 实现；
- **Ray 版本快速迭代**：镜像/依赖兼容；
- **安全默认值收紧**：旧 YAML 可能不再适用；
- **多集群/多租户复杂度**：KubeRay 不是完整平台。

## 18.6 给平台团队的建议

1. 先把 RayCluster/RayJob/RayService 三条主路径跑稳；
2. 再上 autoscaler/批调度器；
3. 最后上增量升级/History Server/mTLS 等 alpha/beta；
4. 每个 alpha 功能单独 gate、单独监控；
5. 保持 CRD/operator/Ray 版本矩阵；
6. 定期做故障演练；
7. 不要把未发布的功能写进长期架构。

## 18.7 值得关注的信号

- KubeRay release notes 的 “Breaking Changes”；
- feature gate 默认值变化；
- CRD schema 的新字段/弃用字段；
- Ray 的 autoscaler/Serve/Job API 变化；
- Kubernetes Gateway API/WAS/Kueue 的成熟度；
- 社区 issue 中的生产案例。

## 18.8 如何评估一个新特性能否上生产

不要用“release notes 说支持”作为结论。按下面顺序评估：

1. **读 API 定义**：字段是否在 v1 schema 中？有没有 `+kubebuilder:validation`/默认值/互斥规则？
2. **读 feature gate**：默认值是什么？关闭时行为如何？升级/回滚是否需要重启 operator？
3. **读依赖**：Gateway API 实现、cert-manager、对象存储、Redis、K8s 版本、Ray 版本，缺一个就不能用。
4. **读失败路径**：超时、finalizer、清理、重试、回滚。没有失败路径文档的功能不能进关键路径。
5. **跑最小实验**：在 kind 上按第 26 章做一次正向 + 一次故障注入；记录时间线和证据。
6. **定义回滚**：关闭 gate、恢复旧 CR、删除新子资源，三步都要演练。
7. **定监控**：至少一个“功能是否在工作”的指标 + 一个“功能是否在拖垮系统”的指标。

一个实用判据：**如果这个功能坏了，你的 SLO 会不会受影响？** 会，就按生产标准做；不会，就放到“辅助工具”层级，别让它阻塞主路径。

## 18.9 小结

- KubeRay 已进入生产细节阶段：History Server、增量升级、安全、GCS FT、调度集成。
- 近期重点是把 alpha/beta 功能做稳，而不是不断增加 CRD。
- Ray 侧的 autoscaler v2、label selector、Serve/LLM、Data/Train 会继续影响 KubeRay 的设计。
- 平台团队应循序渐进：主路径 -> 弹性 -> 多租户 -> 高级功能。

## 18.10 延伸阅读

- 真实集群落地：第 26 章；
- ML 工作负载：第 27 章；
- LLM 推理：第 28 章；
- 队列/调度/工作流：第 29 章；
- 源码与二次开发：第 30 章；
- 升级/迁移/灾备：第 31 章；
- 多租户平台：第 32 章；
- Autoscaler v2 内部机制：第 34 章；
- Ray 运行时内幕：第 35 章；
- GPU/MIG/DRA：第 36 章；
- 故障复盘：第 37 章；
- 生产移植：第 38 章；
- 选型与替代方案：第 39 章；
- 网络深潜：第 40 章；
- 控制面规模化：第 41 章；
- 可运行故障实验室：第 42 章；
- 贡献与发布工程：第 43 章；
- 多可用区/托管 Kubernetes/DR：第 44 章；
- Ray Data 执行引擎：第 45 章；
- 弹性分布式训练：第 46 章；
- LLM 推理内部机制：第 47 章；
- 可观测性 2.0：第 48 章；
- 平台治理与供应链：第 49 章；
- 数据与模型治理：第 50 章；
- 节点弹性/优雅终止/开发回路/operator 可靠性：第 51–54 章；
- 变更管理与升级：第 55 章；
- Ray 活体诊断：第 56 章。

下一章开始简版实现：先看架构与设计取舍。

→ [第 19 章 MiniKubeRay 架构与设计取舍](19-mini-architecture.md)
