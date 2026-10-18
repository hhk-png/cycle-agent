# 附录 I. 自测题与答案

本附录用 78 道题覆盖全书主线。建议先自己回答，再对照答案；答案只给要点，详细推导见对应章节。

## I.1 基础与架构（1–8）

**1. KubeRay 与 Ray Cluster Launcher 的分工是什么？**

Ray Cluster Launcher 面向 VM/静态节点，负责“起机器 + 跑 Ray”；KubeRay 把 Ray 集群建模为 Kubernetes CRD，由 operator 持续调和 Pod/Service/PVC。两者都复用 Ray 运行时，区别在生命周期由谁管理。

→ [第 1 章](01-why-kuberay.md)

**2. 四个 CRD 分别解决什么问题？**

- `RayCluster`：长期集群；
- `RayJob`：一次性作业 + 可选自动建/删集群；
- `RayService`：Ray Serve 在线服务 + 升级；
- `RayCronJob`：按 cron 创建 RayJob。

→ [第 4 章](04-kuberay-overview.md)

**3. 为什么 head Service 默认是 headless？**

一个 RayCluster 只有一个 head，不需要负载均衡；worker 需要稳定 DNS 加入 GCS；`publishNotReadyAddresses` 让 head 未 Ready 时也能解析。

→ [第 6 章](06-pods-networking-storage.md)

**4. GCS 存什么？为什么是单点？**

节点、actor、job、object、placement group 等全局元数据；所有节点/组件依赖它注册/订阅。head 挂了，元数据丢失，集群不可恢复（除非 GCS FT）。

→ [第 2 章](02-ray-architecture.md)、[第 35 章](35-ray-runtime-internals.md)

**5. Pod Running 是否等于 Ray 节点可用？**

不等于。还要 GCS 可达、raylet 注册、资源形状有效、token/mTLS 匹配。`wait-gcs-ready` init container 就是为此存在。

→ [第 6 章](06-pods-networking-storage.md)、[第 35 章](35-ray-runtime-internals.md)

**6. `/dev/shm` 为什么重要？**

Ray 对象存储（Plasma）使用共享内存；容器默认 `/dev/shm` 常只有 64MB，太大会 spill/OOM。KubeRay 挂载 memory emptyDir，sizeLimit 由 memory request/limit 推导。

→ [第 6 章](06-pods-networking-storage.md)

**7. Operator 调和的五个原则是什么？**

幂等、先 spec 后 status、不假设顺序、冲突重试、失败可见（event/condition/log）。

→ [第 3 章](03-kubernetes-operator.md)

**8. KubeRay 的 admission webhook 是可选的还是必需的？什么时候应该开启？**

可选。默认部署靠 CRD schema + 控制器校验；开启环境变量 `ENABLE_WEBHOOKS=true` 后，少量 metadata/重复 worker group 规则会在 admission 阶段拒绝（不覆盖 SidecarMode 等跨字段规则）。需要更早失败/更强策略时开启，但要承担证书、Service 和 failurePolicy=Fail 带来的可用性风险。

→ [第 3 章](03-kubernetes-operator.md)

## I.2 CRD 与调和（9–18）

**9. worker 副本数如何计算？**

`desired = clamp(replicas, minReplicas, maxReplicas) × numOfHosts`；`status.desiredWorkerReplicas` 是所有 group 之和。

→ [第 5 章](05-raycluster-crd.md)

**10. 多主机 replica 的原子性指什么？**

一个 replica 由 `numOfHosts` 个 Pod 组成；任一 host 失败/缺失/多余，整个 replica 删除重建；缩容删整个 replica，不留下半个切片。

→ [第 5 章](05-raycluster-crd.md)、[第 33 章](33-mini-advanced-implementation.md)

**11. `upgradeStrategy: Recreate` 与 `None` 的区别？**

`Recreate` 在 spec hash 变化时删除所有 Pod 再重建；`None`/未设置不会因模板变化自动删除现有 Pod。hash 排除 replicas/workersToDelete。

→ [第 5 章](05-raycluster-crd.md)、[第 7 章](07-controller-reconcile.md)

**12. 为什么 Service/RBAC 要先于 Pod 创建？**

worker 启动时需要 head Service DNS 已存在；autoscaler sidecar 启动时需要 patch RayCluster 的 RBAC。

→ [第 7 章](07-controller-reconcile.md)

**13. scale expectations 解决什么问题？**

controller-runtime cache 是最终一致的；刚创建的 Pod 可能还没进 cache，下一轮会重复创建。expectations 记录“已期望创建/删除”，等 cache 追上再继续。

→ [第 7 章](07-controller-reconcile.md)、[第 38 章](38-porting-to-production-operator.md)

**14. `RayClusterProvisioned` 为什么是粘性的？**

第一次全部 Ready 后一直保持 True，**suspend 或后续 Pod 失败都不会重置**；这样上层能区分“曾经就绪”和“当前就绪”。当前可用性要看 `HeadPodReady`、ready worker 数或 Pod 状态。

→ [第 5 章](05-raycluster-crd.md)、[第 7 章](07-controller-reconcile.md)

**15. suspend 的状态机是什么？**

`Running -> Suspending -> Suspended -> Running`；第一次看到 suspend 只设置 condition，下一轮删 Pod，全部删除后设置 Suspended。

→ [第 7 章](07-controller-reconcile.md)

**16. `lastTransitionTime` 为什么不能每次都更新？**

会导致 status 永远在变、watch 风暴、GitOps 永远不同步；只在 status 变化时更新。

→ [第 3 章](03-kubernetes-operator.md)

**17. GCS FT 的 Redis 后端如何工作？**

GCS 元数据持久化到外部 Redis；head 重启后从 Redis 恢复；worker 重连；`externalStorageNamespace` 隔离集群。只恢复元数据，不恢复 task/actor 状态。

→ [第 13 章](13-ha-fault-tolerance.md)

**18. RocksDB 后端的限制是什么？**

单写者；同一时间只能一个 head 挂载；需要 gate；升级/故障切换要外部协调；`Retain` 保留 PVC；RayService 双 head 场景需要额外协调。

→ [第 13 章](13-ha-fault-tolerance.md)

## I.3 工作负载与生产（19–30）

**19. RayJob 四种提交模式的区别？**

- K8sJobMode：submitter Pod 提交；
- HTTPMode：operator 直接调 Dashboard；
- SidecarMode：head sidecar 提交；
- InteractiveMode：用户提交，operator 等待 `jobId`。

→ [第 10 章](10-rayjob.md)

**20. `JobStatus` 与 `JobDeploymentStatus` 有什么区别？**

前者是 Ray 作业状态（PENDING/RUNNING/SUCCEEDED/FAILED/STOPPED）；后者是 KubeRay 部署状态（Initializing/Running/Complete/Failed/...）。

→ [第 10 章](10-rayjob.md)

**21. `shutdownAfterJobFinishes` 与 `ttlSecondsAfterFinished` 的关系？**

前者在作业终态后删除 RayCluster/资源；后者把删除推迟到 `endTime + TTL`。TTL 不是 RayJob CR 的生命周期；CR 是否删除由 operator 的环境配置控制。两者必须同时使用。

→ [第 10 章](10-rayjob.md)

**22. deletion rules 与 legacy 字段为什么互斥？**

规则模式表达力更强，且与 `shutdownAfterJobFinishes` 的语义冲突；同时设置会产生不确定的清理顺序。

→ [第 10 章](10-rayjob.md)

**23. `clusterSelector` 有哪些限制？**

必须有 `ray.io/cluster`；不能 suspend/Sidecar、`backoffLimit` 应为 0；不要设置 shutdown/TTL/deletionStrategy（控制器会跳过共享集群删除，但不要依赖“校验一定拒绝”）；终态不删除共享集群。

→ [第 10 章](10-rayjob.md)

**24. RayService 什么时候创建新集群？**

只有 revision hash 变化（镜像/模板/资源/numOfHosts 等）才创建 pending；只改 `serveConfigV2`/`excludeHeadPodFromServeSvc` 时原地部署。

→ [第 11 章](11-rayservice.md)

**25. `ray.io/serve` 标签的作用？**

Serve Service selector 用它选择“proxy 健康、可接流量”的 Pod；head 默认 false（或由控制器管理），worker 默认 true；`excludeHeadPodFromServeSvc` 让 head 永远 false。

→ [第 11 章](11-rayservice.md)

**26. 增量升级如何迁移流量？**

创建 pending 集群 → Serve RUNNING → 按 `stepSizePercent`/`intervalSeconds` 改 HTTPRoute `backendRefs` 权重 → `targetCapacity` 随流量上升 → 100% 后 promote → 旧集群延迟删除。

→ [第 11 章](11-rayservice.md)

**27. RayCronJob v1.7 缺少哪些 Kubernetes CronJob 字段？**

`concurrencyPolicy`、`successfulJobsHistoryLimit`/`failedJobsHistoryLimit`、`startingDeadlineSeconds`；并发/历史要上层实现。

→ [第 12 章](12-raycronjob-batch.md)

**28. 批调度器与 Kueue 的分工？**

批调度器决定 Pod 怎么放（gang/队列/优先级）；Kueue 决定作业什么时候被准入（配额/借用/抢占）。可以组合，但不要同时开多个 gang 调度器。

→ [第 29 章](29-ecosystem-integrations.md)

**29. GCS FT 的删除流程？**

删除 Pod → 创建 Redis cleanup Job → 等 Complete → 移除 finalizer → CR 删除；超时（默认 300s）可强制释放，可能留下垃圾数据。

→ [第 13 章](13-ha-fault-tolerance.md)、[第 33 章](33-mini-advanced-implementation.md)

**30. 零停机升级的边界是什么？**

不是“请求绝不失败”：长连接可能断；旧集群删除前要等 in-flight；新集群模型加载慢；Gateway/Ingress draining 影响；客户端仍需重试。

→ [第 11 章](11-rayservice.md)

## I.4 实现、验证与运维（31–40）

**31. MiniKubeRay 为什么用 dict 而不是 dataclass？**

Kubernetes 对象是任意嵌套 JSON；dict 与 YAML/JSON 无缝转换，保留未知字段；代价是没有静态类型检查。

→ [第 19 章](19-mini-architecture.md)

**32. ApiStore 提供哪些 Kubernetes 语义？**

resourceVersion、generation、status subresource、finalizer 两阶段删除、watch、乐观并发、label/field selector、patch、dump/load。

→ [第 20 章](20-mini-api-store.md)

**33. 确定性时钟解决什么问题？**

避免 `sleep`/挂钟时间导致 flaky；TTL/超时/idle 可以精确快进；同一测试在任何机器上结果一致。

→ [第 24 章](24-testing-verification.md)

**34. MiniKubeRay 的 autoscaler 与真实 v2 的差距？**

没有多维装箱、label selector、placement group、并发 launch 限流、实例状态机、优雅 drain、节点池；只验证决策公式。

→ [第 34 章](34-autoscaler-v2-internals.md)

**35. 文档检查抓不到什么？**

语义正确性、外部链接、真实 K8s apply、真实 Ray 兼容、运行时性能；需要人工 review/e2e。

→ [第 24 章](24-testing-verification.md)

**36. 为什么 Helm 不升级 CRD？**

Helm 的 `crds/` 只在安装时创建，`helm upgrade` 不会更新；必须单独 apply，且 CRD schema 只增不减。

→ [第 17 章](17-production-runbook.md)、[第 31 章](31-upgrade-migration-dr.md)

**37. 生产升级的顺序？**

备份 → 读 release notes → staging 升级 CRD → operator → 观察 → 分批升级 Ray 镜像 → 验证 RayJob/RayService → 回滚预案。

→ [第 31 章](31-upgrade-migration-dr.md)

**38. GPU 请求的关键规则？**

扩展资源 requests=limits；device plugin 注册；Ray 的 `num-gpus`/顶层 `resources` 要与 Pod 一致；MIG/time-slicing/DRA 是不同隔离模型。

→ [第 36 章](36-gpu-heterogeneous-scheduling.md)

**39. 扩容端到端为什么是分钟级？**

autoscaler 观测 → patch → operator → 调度 → 镜像拉取 → 容器启动 → raylet 注册 → 再次观测；每步都有延迟。

→ [第 34 章](34-autoscaler-v2-internals.md)

**40. 故障排查的第一原则？**

先看 CR 的 conditions/events，再按回路定位（Ray demand → autoscaler → CR → Pod → 调度 → 运行时）；保留证据再止血。

→ [第 37 章](37-production-incident-postmortems.md)、[附录 C](appendix-c-troubleshooting.md)

## I.5 进阶（41–48）

**41. MiniKubeRay 的多主机调和为什么以 replica 为原子单位？**

因为同一 replica 的 host 共享 replica-name，必须整组工作；删除任一 host 会让切片不完整，所以整组替换。

→ [第 33 章](33-mini-advanced-implementation.md)

**42. History Server 的事件归属规则？**

RayCluster 用 `namespace/name`；RayJob 用 `status.rayClusterName`，RayService 用 active/pending 的 `rayClusterName`；RayCronJob 自身没有 `status.rayClusterName`，按 cronjob 名/子 RayJob 归属；Pod/Service/Job 用 `ray.io/cluster` 标签；Event 用 involvedObject。

→ [第 33 章](33-mini-advanced-implementation.md)

**43. 增量升级的 `targetCapacity` 与 `trafficRoutedPercent` 区别？**

`targetCapacity` 是 Serve 的全局副本容量百分比（预热）；`trafficRoutedPercent` 是 HTTPRoute 的流量权重。两者随迁移逐步上升，但不是同一个字段。

→ [第 11 章](11-rayservice.md)、[第 33 章](33-mini-advanced-implementation.md)

**44. 为什么 `minMember` 设置错误会导致 Pending？**

太小：部分 Pod 运行、部分 Pending，资源被占住；太大：队列永远无法满足。`minMember` 应覆盖 head + 所有 worker（按需求）。

→ [第 36 章](36-gpu-heterogeneous-scheduling.md)

**45. Ray 的 `num-cpus` 与 Pod requests 不一致会怎样？**

K8s 按 Pod requests 调度，Ray 按 `num-cpus` 调度；不一致会浪费节点或让 task 排队。

→ [第 2 章](02-ray-architecture.md)、[第 15 章](15-performance-cost.md)

**46. 为什么不能用 HPA 扩 Ray worker group？**

HPA 看指标时间序列，不感知 Ray demand/节点类型/placement group；应与 Ray autoscaler 分工，避免双控制器竞争。

→ [第 34 章](34-autoscaler-v2-internals.md)

**47. 生产平台的最小隔离组合？**

namespace + RBAC + ResourceQuota/LimitRange + NetworkPolicy + Pod Security + 镜像策略；强隔离再加节点池/集群。

→ [第 14 章](14-security.md)、[第 32 章](32-platform-capstone.md)

**48. 从 MiniKubeRay 移植到生产 Operator 的三个关键风险？**

cache 最终一致（scale expectations）、status/冲突（只写变化/重试）、finalizer/ownerReference 顺序（删除卡住/泄漏）。

→ [第 38 章](38-porting-to-production-operator.md)

## I.6 网络、规模化与工程化（49–54）

**49. 为什么 Ray 的 NetworkPolicy 不能只放行 6379？**

Ray 节点之间还有对象传输、raylet RPC、Dashboard agent（52365）、Serve proxy（8000）等动态端口。只放行 6379 可能能启动，但大对象/多节点任务会失败。

→ [第 40 章](40-kubernetes-network-internals.md)

**50. Ray Client（10001）和 Jobs API（8265）有什么区别？新项目该选哪个？**

Ray Client 已废弃，且要求客户端与集群 Ray/Python 次版本精确匹配；Jobs API 走 Dashboard 的 HTTP 端口，提交 entrypoint，客户端不需要与集群同版本。新项目选 Jobs API，Ray Client 只用于遗留代码。

→ [第 2 章](02-ray-architecture.md)、[第 53 章](53-interactive-dev-notebooks.md)

**51. 为什么 worker 的 preStop 与 `terminationGracePeriodSeconds` 必须一起配？**

preStop 的时间计入 grace，不是额外赠送。预算要满足 `preStop + drain + endpoint 传播 + buffer <= grace`；否则 preStop 或 drain 还没结束就被 SIGKILL，worker/推理副本会被硬杀。

→ [第 52 章](52-graceful-termination-disruption.md)

**52. 1000 个 RayCluster 的控制面容量按什么估算？**

按对象总数（Pod/Service/EndpointSlice/Event 通常是 CR 的 20 倍量级，极端对象组合可到 20 倍以上）、cache 内存、API 写入/QPS、reconcile 成本；不是只按 CR 数量。

→ [第 41 章](41-control-plane-at-scale.md)

**53. 故障实验室为什么用确定性时钟？**

`ManualClock` 让 TTL/deadline/idle/Serve 部署延迟可精确推进，不依赖 sleep，测试可重复；`settle` 只收敛，`step` 才推进时间。

→ [第 42 章](42-incident-lab.md)

**54. 加一个 CRD 字段的完整流程？**

类型 + kubebuilder 标记 → `make generate/manifests` → 默认值/校验 → 调和 → 单元/envtest/e2e → 文档/sample/release notes → 兼容性/feature gate。

→ [第 43 章](43-contribution-release-engineering.md)

## I.7 多可用区与灾难恢复（55–59）

**55. RTO 与 RPO 分别回答什么问题？**

RTO 是“故障后多久恢复服务”，RPO 是“最多能丢多少数据/时间窗口”。没有这两个数字，就无法验收 HA/DR，也无法决定备份频率和备用容量。

→ [第 44 章](44-multi-az-managed-kubernetes-dr.md)

**56. `topologySpreadConstraints` 的 `DoNotSchedule` 和 `ScheduleAnyway` 怎么选？**

`DoNotSchedule` 是硬约束，容量不足时 Pod 会 Pending，但能保证故障域分散；`ScheduleAnyway` 是软约束，优先均衡但不阻塞。生产共享池常用硬约束 + 足够容量，弹性池用软约束。

→ [第 44 章](44-multi-az-managed-kubernetes-dr.md)

**57. PDB 能防可用区故障吗？**

不能。PDB 只约束 drain/升级等**自愿驱逐**，不约束节点宕机、AZ 故障、OOM 或 kubelet 直接杀 Pod。它必须和拓扑打散、节点池容量、GCS FT 一起用。

→ [第 44 章](44-multi-az-managed-kubernetes-dr.md)

**58. RocksDB GCS FT 能扛可用区故障吗？**

通常不能。PVC 是区级资源，跨区无法挂载；RocksDB 适合同区 head 重启。跨区要用 Redis 多 AZ/Cluster，或对象存储备份 + 重建/切换。

→ [第 13 章](13-ha-fault-tolerance.md)、[第 44 章](44-multi-az-managed-kubernetes-dr.md)

**59. 托管 Kubernetes 上 KubeRay 的行为有变化吗？**

CRD/控制器行为基本一致；差异在节点弹性（Karpenter/NAP/Cluster Autoscaler）、身份（IRSA/Workload Identity）、存储（zonal PV）、网络（VPC CNI/Azure CNI）和 GPU/TPU 机型。设计时按这些维度逐项确认。

→ [第 44 章](44-multi-az-managed-kubernetes-dr.md)

## I.8 数据、训练、推理与治理（60–74）

**60. Ray Data 的并行度由什么决定？**

由 block 数、每个算子的 `concurrency`、资源形状（`num_cpus`/`num_gpus`）和调度器在途上限共同决定；worker 数只是上限，不是并行度本身。

→ [第 45 章](45-ray-data-execution-engine.md)

**61. 没有背压会发生什么？**

读算子会把 block 快速堆进对象存储，超过阈值后 spill 到磁盘；磁盘打满会导致 task 失败或节点异常。有界队列把内存峰值限制在“队列深度 × block 大小 × 活跃算子数”。

→ [第 45 章](45-ray-data-execution-engine.md)

**62. shuffle 的分区数怎么定？**

分区数约为总核数的 1–4 倍，单分区几百 MB；太少会倾斜/长尾，太多会造成小文件与调度开销。严重倾斜的 key 需要加盐或两阶段聚合。

→ [第 45 章](45-ray-data-execution-engine.md)

**63. checkpoint 只保存模型参数为什么不够？**

优化器状态、学习率调度、数据加载器位置、RNG 状态和并行策略元数据都会影响恢复后的训练轨迹；缺失会导致 loss 跳变或重复样本。

→ [第 46 章](46-elastic-distributed-training.md)

**64. 为什么 world size 只能在 checkpoint 边界改变？**

step 中间改变会让新 rank 缺少该 step 的梯度、all-reduce 参与者不一致，结果不可复现；checkpoint 边界能保证所有 rank 从同一状态恢复。

→ [第 46 章](46-elastic-distributed-training.md)

**65. gang scheduling 解决什么问题？**

避免“部分分配”占住资源：8 个 worker 只启动 5 个时，这 5 个会等待并且其他作业也无法使用。gang scheduling 要求全部满足或全部不调度。

→ [第 46 章](46-elastic-distributed-training.md)、[第 29 章](29-ecosystem-integrations.md)

**66. KV cache 每 token 的大小怎么算？**

`2 × num_layers × num_kv_heads × head_dim × dtype_bytes`。Llama-3.1-8B（32 层、8 KV heads、128、FP16）约 128 KiB/token，8k 上下文约 1 GiB。

→ [第 47 章](47-llm-inference-internals.md)

**67. continuous batching 为什么能提高吞吐？**

调度粒度从“整个 batch”降到“每个 iteration”：完成的请求立即释放槽位，新请求下一轮就能加入，减少 padding 与空转。

→ [第 47 章](47-llm-inference-internals.md)

**68. 前缀缓存对什么场景收益最大？**

共享 system prompt、few-shot、RAG 固定指令、多轮对话历史。缓存命中能直接减少 prefill 的 TTFT 和算力；跨 replica 需要缓存感知路由。

→ [第 47 章](47-llm-inference-internals.md)

**69. PD 分离的代价是什么？**

KV cache 传输。8k prompt、128 KiB/token 约 1 GiB；NVLink 几毫秒，25 GbE 可能几百毫秒。跨节点普通以太网可能吃掉收益，需要 RDMA/IB。

→ [第 47 章](47-llm-inference-internals.md)

**70. trace 相比 metrics/logs 多回答了什么？**

因果关系与耗时分解：哪个 reconcile 调用了哪个子操作、在哪一步失败、时间花在哪。metrics 是聚合，logs 是无因果顺序的文本。

→ [第 48 章](48-observability-2.md)

**71. 99.9% SLO 的 30 天错误预算和燃烧率怎么算？**

预算 = 43,200 分钟 × 0.1% = 43.2 分钟；观测错误率 0.12% 时坏分钟 = 51.84，预算消耗 120%，燃烧率 = 1.2x。多窗口用 1h/6h/3d 阈值减少误报。

→ [第 48 章](48-observability-2.md)

**72. 为什么 admission 策略要先 audit 再 enforce？**

直接 enforce 会把存量不合规对象、缺失字段和误报一次性暴露为拒绝；audit 先观察命中率与误报，修复规则/迁移后才能安全切换。

→ [第 49 章](49-platform-governance-supply-chain.md)

**73. point-in-time join 的规则是什么？**

只用 `event_time <= as_of` 且 `event_time > as_of - window` 的事件；按 ingestion time 过滤会看到未来才入库的数据，造成训练泄漏。

→ [第 50 章](50-data-model-governance.md)

**74. 回滚模型为什么可能不够？**

如果特征视图/schema 也变了，只回滚模型仍会用不兼容的特征；血缘必须覆盖模型 → 特征视图 → 数据集，回滚要一起回滚。

→ [第 50 章](50-data-model-governance.md)

## I.9 弹性、生命周期、开发体验与 operator 可靠性（75–78）

**75. Ray autoscaler 已经把 `replicas` 调大，但 Pod 一直 Pending，先查什么？**

先查 Pod 的 Pending 原因（配额、nodeSelector/taint、GPU 资源），再查节点池 `maxSize`/云容量，最后才看 Ray autoscaler。Ray autoscaler 不读取 Pending Pod，节点供给是 CA/Karpenter 的职责。

→ [第 51 章](51-kubernetes-elasticity-stack.md)

**76. PDB 能保护 Spot 中断吗？**

不能。PDB 只约束 eviction API（drain/CA 缩容），云厂商终止 Spot 实例不经过 eviction API。Spot 要靠中断通知 + 足够短的终止预算 + checkpoint/重试。

→ [第 52 章](52-graceful-termination-disruption.md)

**77. `runtime_env.working_dir` 应该包含什么？**

只包含代码和小配置；显式排除 `.git`、虚拟环境、`__pycache__`、数据和模型。基础依赖烘焙进镜像，大文件放对象存储/PVC。打包大小超过几百 MiB 就会显著拖慢启动。

→ [第 53 章](53-interactive-dev-notebooks.md)

**78. readiness 与 liveness 失败分别会发生什么？**

readiness 失败只会把 Pod 从 Service 端点移除，不重启容器；liveness 失败会触发 kubelet 重启容器。operator 的 readiness 应反映 cache 是否同步/是否 leader，liveness 只应反映事件循环是否还在推进，不能依赖 apiserver 可用性。

→ [第 54 章](54-operator-reliability-control-plane-slo.md)

## I.10 变更管理与活体诊断（79–82）

**79. 为什么改 `replicas` 不应该触发 `Recreate`？**

spec hash 排除了 autoscaler 拥有的字段（`replicas`、`minReplicas`/`maxReplicas`、`workersToDelete`）。否则每次扩缩容都会被判定为模板变化，在 `Recreate` 下把整个集群重启。如果扩缩容时全量重建，先检查 hash 是否被不稳定字段污染。

→ [第 55 章](55-change-management-upgrades.md)

**80. `upgradeStrategy: None` 下改镜像会发生什么？**

现有 Pod 不动，新创建/重建的 Pod 使用新模板，于是可能出现新旧 Ray 版本混跑。Ray 要求 head 与 worker 同版本，混合版本可能无法加入。跨版本升级应使用新集群、RayJob 重新提交或 RayService 的集群级升级，而不是 `None` + 扩容。

→ [第 55 章](55-change-management-upgrades.md)

**81. 删除 head Pod 后，怎样避免控制器立刻重建它？**

给 RayCluster 加注解 `ray.io/disable-provisioned-head-restart: "true"`，控制器在集群 provisioned 后不会自动重建 head。维护完成后必须移除注解，并确认 head Service 没有被删除。没有 GCS FT 时，head 恢复后是一个新集群，不要把它当成状态恢复。

→ [第 55 章](55-change-management-upgrades.md)

**82. task 卡在 `PENDING_NODE_ASSIGNMENT`，应该先看哪里？**

先看 `ray status` 的 Demands 与节点可分配资源，再看 `ray list tasks --detail` 的调度信息、placement group 是否 ready，最后才怀疑调度器。`PENDING_OBJ_STORE_MEM_AVAIL` 则是对象存储/对象大小问题，不是节点容量问题。

→ [第 56 章](56-live-diagnostics-state-api.md)

## I.11 垂直弹性与 Kubernetes 新原语（83–86）

**83. IPPR 的决策顺序是什么？为什么不能先水平扩容？**

先用现有 Pod 的 current 容量装箱，再用 `max-cpu`/`max-memory` 的 headroom 装箱，仍不够才水平扩容。先水平会放过“一个 Pod 就能吸收”的机会，增加启动时间、对象存储和调度碎片；先垂直能让运行中的 Raylet 保留对象、actor 和连接。细节见第 57.4 节。

→ [第 57 章](57-in-place-pod-resizing-ippr.md)

**84. 为什么 IPPR 要求 `rayStartParams` 里不能有 `num-cpus`/`memory`？**

因为 IPPR 同时改两半：cgroup 限额和 Raylet 的逻辑资源。如果 `num-cpus` 被硬编码，Pod 扩到 4 核后 Raylet 仍然只报告旧值，Ray 不会把 task 调度上去。KubeRay 直接拒绝这种配置，避免“机器变大但 Ray 用不上”。

→ [第 57 章](57-in-place-pod-resizing-ippr.md)

**85. 原生 sidecar 和 RayJob 的 `SidecarMode` 是一回事吗？**

不是。原生 sidecar 是 `initContainers` + `restartPolicy: Always`，解决启动/退出顺序和 Job 完成；`SidecarMode` 是 KubeRay 在 head Pod 里放一个普通 submitter 容器，解决“谁提交 Ray job”。两者的生命周期、启动顺序和失败语义都不同。见第 58.2.5 节。

→ [第 58 章](58-kubernetes-primitives.md)

**86. `schedulingGates` 能不能当“暂停 RayCluster”用？**

不能。gated Pod 不注册 Raylet，Ray autoscaler 会认为容量仍然不足，可能继续把副本加到 `maxReplicas`；Cluster Autoscaler 也不会为 gated Pod 扩容。暂停集群用 `spec.suspend`；gate 适合等待配额/缓存/模型等外部前置条件，并且必须有明确的移除责任人。见第 58.4 节。

→ [第 58 章](58-kubernetes-primitives.md)

← [返回目录](README.md)
