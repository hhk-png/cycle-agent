# 第 35 章 云原生部署与 Kubernetes

本章导读：推理引擎最终要跑在某种编排系统上。Kubernetes 不是唯一选择，但它把“调度、健康检查、滚动更新、弹性、隔离”这些能力标准化了。本章从 GPU/NPU 设备插件讲到 Pod 生命周期、探针、HPA/KEDA、Ingress 与 SSE、模型缓存、PD 分离的编排、优雅退出和多租户隔离，并用 `deploy/` 目录的 Dockerfile 与 `k8s.yaml` 做对照。读完本章，你应该能画出一次推理请求从 Ingress 到 Pod 再到 GPU 的完整路径，并知道每个环节会在哪里坏。

## 35.1 为什么推理服务需要编排

单机部署的推理服务也能跑，但会遇到四个问题。

第一，**资源碎片**。一台 8 卡机器可能只跑一个 4 卡实例，剩下 4 卡闲置；或者多个小实例抢同一张卡，互相干扰。编排系统负责把 Pod 放到合适的节点，按 GPU 型号、拓扑、标签做调度。

第二，**弹性**。流量有峰谷，推理实例的启动又很慢（加载权重、warmup、捕获 CUDA Graph）。编排系统负责扩容、缩容和滚动更新，但“扩得多快”取决于冷启动时间，不是简单地改一个 replica 数。

第三，**故障恢复**。GPU 掉卡、节点失联、容器 OOM 都需要自动摘除和重建。编排系统提供探针、重启策略、PDB 和节点级故障转移，但推理引擎要配合实现 drain、KV 交接和 EP size 重定。

第四，**隔离与治理**。多租户、配额、网络策略、镜像扫描、审计，都需要一个统一的控制面。Kubernetes 的 Namespace、ResourceQuota、NetworkPolicy、RBAC 提供了基础能力，但推理服务的“租户”通常映射到网关的 API key，而不是 K8s 的 Namespace。

DeepInfer 的生产系统并不是“一个 Deployment 跑天下”。它把 Prefill、Decode、Vision Encoder 分成不同的实例池，用专门的负载均衡器路由，KV 放在共享 3FS 上。Kubernetes 负责“把进程跑起来、保持健康、按需扩缩”，但不负责“请求去哪个 P、哪个 D、哪个 rank”。这两层要分开设计。

## 35.2 容器镜像

推理镜像通常很大，因为它包含 CUDA/CANN 运行时、编译好的 kernel、Python 依赖和可能的权重。镜像设计要兼顾三点：可重复、可扫描、可启动。

**基础镜像固定版本。** 不要用 `nvidia/cuda:latest` 或 `pytorch/pytorch:latest`；固定到具体 tag 和 digest，记录在 manifest 里。CUDA 13、CANN 9.2 这样的版本要求要和驱动版本匹配，升级驱动前先在测试集群验证。

**多阶段构建。** 编译 C++/CUDA/TileLang 扩展需要完整的工具链，运行只需要运行时库。用多阶段构建把编译产物复制到精简的运行镜像，可以显著减小体积和攻击面。

**非 root、只读根文件系统。** 推理进程不需要写代码目录；只挂载模型缓存、临时目录和 KV 换出目录。`deploy/Dockerfile` 用 `useradd` 创建 uid 10001 的用户，`k8s.yaml` 里设置 `runAsNonRoot`、`readOnlyRootFilesystem` 和 `drop: ALL`。这些设置会增加一点配置成本，但能显著缩小容器逃逸后的影响面。

**健康检查放在镜像里。** Dockerfile 的 `HEALTHCHECK` 和 K8s 的 readiness/liveness 探针可以复用同一个 `/health` 端点。注意探针的超时要覆盖模型加载时间；一个 552B 模型加载可能需要几分钟，`initialDelaySeconds` 太短会让 Pod 在启动完成前被杀，陷入 CrashLoopBackOff。

**镜像里不要塞权重。** 权重应该放在共享 PVC、对象存储或模型仓库，由 init container 或启动脚本拉取。把几百 GB 权重打进镜像会让构建、分发和回滚都变得极其笨重。`deploy/k8s.yaml` 用 `PersistentVolumeClaim` 挂载 `/models`，并设为只读。

## 35.3 Pod 设计与资源模型

一个推理 Pod 通常包含一个主容器，可能加一个 sidecar 做指标代理或日志采集。主容器的资源请求要写清楚：CPU、内存、GPU/NPU 数量、共享内存、临时存储。

**GPU 是扩展资源。** Kubernetes 通过设备插件把 GPU 暴露成 `nvidia.com/gpu` 这样的资源；请求和限制必须相等，GPU 不支持超卖。Ascend 的设备插件资源名由厂商定义。没有设备插件时，Pod 只能看到宿主机的设备文件，调度器无法保证独占，容易出现两个 Pod 抢同一张卡。

**共享内存要显式申请。** PyTorch/NCCL 和很多推理 kernel 会用 `/dev/shm` 做进程间通信；默认 64 MB 往往不够。生产 Pod 通常挂一个 `emptyDir: {medium: Memory}` 到 `/dev/shm`，大小按模型和通信 buffer 估算。

**拓扑要约束。** EP/TP 组需要高带宽互联；如果 Pod 被调度到不同机架，all-to-all 会变慢。可以用 node affinity、pod affinity、topology spread constraints 把同一组的 Pod 放到同一 NVLink 域或同一 supernode。Kubernetes 原生不理解 NVLink 拓扑，通常要配合厂商的拓扑感知调度器或自定义 device plugin。

**NUMA 和 CPU pinning。** 大模型加载和 tokenizer 可能受内存带宽影响；对延迟敏感的服务可以用 CPU manager 的 static policy 和 hugepages。不是所有集群都支持，收益也取决于瓶颈，不要默认开启。

## 35.4 Deployment、StatefulSet 与 PD 分离

无状态推理实例适合用 Deployment：每个 Pod 独立加载模型，处理请求，不保存跨请求状态。滚动更新由 Deployment 控制，`maxUnavailable: 0` 和 `maxSurge: 1` 可以保证更新期间至少有一个旧 Pod 可用。

但推理服务并不是完全无状态：KV cache 是请求级状态，PD 分离后 P 实例要把 KV 交给 D 实例，D 实例重启会丢失运行中的序列。于是有两种编排方式。

**把 KV 放在共享存储。** P/D 实例都无状态，KV 写到 3FS 或共享内存服务；实例重启后从存储恢复。这要求 KV store 有低延迟和高吞吐，且格式版本兼容。

**把会话绑定到实例。** 用一致性哈希或会话亲和把同一会话的请求路由到同一 D 实例；实例故障时会话需要重算或迁移。这种方式实现简单，但弹性差，故障影响大。

StatefulSet 适合需要稳定网络标识和持久存储的组件，例如 KV store 的元数据服务、模型缓存节点。推理引擎本身通常用 Deployment；如果每个 Pod 需要固定的 rank 和 hostname（例如多机 EP），可以用 StatefulSet 加 headless Service，让 rank 通过 DNS 发现彼此。

PD 分离的编排要按角色分开：

```text
Ingress -> prefill Service -> P Deployment (EP2-8, 8 卡节点)
                          -> KV transfer -> decode Service -> D Deployment (EP64-128, supernode)
```

P 和 D 的副本数、资源规格、扩缩容指标都不同，不能放在同一个 Deployment 里。网关或全局负载均衡器负责把请求分到 P，再把 KV 交接给 D；Kubernetes Service 只负责 Pod 发现和四层负载均衡，不理解“这个请求应该去哪个 P”。

## 35.5 探针、生命周期与优雅退出

推理 Pod 的生命周期比普通 Web 服务长，因为加载和 warmup 很慢。探针设计要区分“进程活着”“能接流量”“能完成请求”。

本节和 35.11 的清单描述的是**目标状态**，不完全是 `deploy/k8s.yaml` 的当前内容。仓库里的清单已经包含 readiness/liveness、`preStop: sleep 10`、Prometheus 注解、PDB 和一个示例 HPA；`server.py` 已经提供 `/admin/drain`，但清单的 preStop 还没接上，仍然只是 sleep。缺的是 startupProbe、ServiceMonitor/PodMonitor，以及把 preStop 换成真实的 drain 调用。HPA 用的 `mini_deepinfer_queue_depth` 指标也没有被 server 暴露，清单里的注释已经标明这一点，应用前要换成你的 metrics adapter 提供的指标，不要以为“文件里有”等于“已经配置”。

**startupProbe** 用于覆盖最长的启动时间。模型加载、权重校验、CUDA Graph 捕获都完成后再让 readiness 通过；startupProbe 失败可以重启，避免永久卡住。

**readinessProbe** 检查“现在能不能接新请求”。当实例进入 drain、KV 空间不足、依赖的 3FS 不可用时，readiness 应该失败，让 Service 把 Pod 摘掉，而不是继续接请求然后超时。`server.py` 的 `/ready` 已经实现了这条语义：draining 时返回 503；`deploy/k8s.yaml` 把 readinessProbe 指向 `/ready`，liveness 仍然用 `/health`，这样 drain 不会触发重启。

**livenessProbe** 检查“进程是否还需要重启”。不要用 liveness 检查下游依赖；3FS 短暂抖动不应该导致所有 Pod 同时重启。liveness 只应该检查进程内部死锁、事件循环卡死这类问题。

**preStop 与 terminationGracePeriodSeconds。** 缩容时，Kubernetes 先调用 preStop，再发 SIGTERM，最后在 grace period 结束后 SIGKILL。推理服务应该在 preStop 里调用 drain 端点：停止接收新请求，等待 running 请求完成，或把未完成的请求和 KV 转发到其他实例。`deploy/k8s.yaml` 的 preStop 还是 `sleep 10`；`server.py` 已经有 `/admin/drain`，生产化时应该把 sleep 换成 `curl -X POST /admin/drain`（并确认 readiness 先失败），再等待 `terminationGracePeriodSeconds`。

**PDB。** PodDisruptionBudget 保证节点维护时至少有一个副本可用。对推理服务，PDB 的 `minAvailable` 要按 SLO 和容量冗余设置；如果只有一个副本，PDB 会让节点永远无法驱逐，需要配合维护窗口。

## 35.6 自动扩缩容

HPA 默认按 CPU/内存扩缩容，这对推理服务通常不够。decode 可能 GPU 打满但 CPU 很闲；prefill 可能 CPU 在等 tokenizer；KV 存储可能先于 GPU 成为瓶颈。

更有意义的扩缩容指标是：

- **等待队列长度**：有多少请求在排队，直接反映容量缺口。
- **KV 使用率**：显存或 3FS 的 KV 占用，决定还能接多少并发。
- **P95 TTFT / TPOT**：直接对应 SLO，但抖动大，需要平滑。
- **goodput**：满足 SLO 的请求吞吐，最接近业务价值。
- **GPU 利用率 / HBM 带宽**：判断是算力还是带宽瓶颈。

Kubernetes 的 HPA 支持自定义指标和外部指标；KEDA 可以从 Prometheus、消息队列或 HTTP 端点拉取指标，适合按队列长度扩缩容。`deploy/k8s.yaml` 用 `mini_deepinfer_queue_depth` 作为示例指标，真实集群需要 metrics adapter 把它暴露给 HPA。

扩缩容的难点是冷启动。加载 552B 权重、初始化通信组、warmup、捕获 CUDA Graph 可能需要几分钟；如果流量涨得比扩容快，SLO 会先崩。常见的缓解手段有：

- 保留“ready but unoccupied”的实例作为 buffer；
- 预拉取镜像和权重到节点；
- 用小模型或降级配置做临时兜底；
- 按预测流量提前扩容，而不是等指标触发；
- 对扩容设置速率上限，避免同时拉起太多实例把存储和网络打满。

缩容要比扩容保守。`scaleDown.stabilizationWindowSeconds` 设长一些，避免流量抖动导致频繁扩缩；缩容前先 drain，确认没有 running 请求再退出。

## 35.7 网关、Ingress 与流式响应

推理服务的入口通常是 Ingress 或 Gateway API，后面接一个专门的 API 网关。网关要做的事情比普通 Web 服务多。

**请求体大小限制。** 一个超长 prompt 可能几百 MB；网关要在读 body 之前就拒绝，而不是让引擎分配完 KV 才报错。

**超时。** 推理请求可能持续几十秒到几分钟，普通网关的 30 秒默认超时会截断流式响应。要为推理路由单独设置 read timeout、send timeout 和 idle timeout。

**SSE 与缓冲。** 流式响应是 `text/event-stream`；网关和反向代理不能缓冲整个响应，否则用户会等到生成结束才看到第一个 token。Nginx 需要 `proxy_buffering off`，Envoy 需要关闭 response buffering。

**重试语义。** 幂等的 GET 可以重试；POST 生成请求重试可能导致重复计费或重复生成。网关应该只对连接建立失败重试，不对已经收到 token 的请求重试。

**鉴权与配额。** API key、OAuth、租户配额、速率限制通常放在网关，而不是引擎。引擎只负责“给定请求生成 token”，不负责“这个租户这个月还能用多少”。`gateway.py` 展示了这层逻辑的最小实现：认证、请求/ token 双桶、并发上限、前缀亲和路由和 drain。

**请求 id 与追踪。** 网关生成或透传 request id，注入 W3C trace context，引擎和 KV store 继续传播。没有统一的 request id，跨服务排障会非常困难。

## 35.8 存储：模型缓存与 KV

推理服务有两类存储需求：模型权重和 KV cache。

**模型权重**通常是只读的、巨大的、需要快速随机读取。可以用 PVC（ReadOnlyMany）、对象存储加本地缓存、或专用的模型仓库。首次加载要下载几百 GB，后续启动应该命中节点本地缓存。Kubernetes 的 `emptyDir` 在 Pod 重启后丢失，不适合做模型缓存；`hostPath` 简单但和节点绑定，不适合多租户；PVC 更通用，但要注意访问模式和性能。

**KV cache** 分两层：GPU HBM 里的 in-flight KV，和 3FS/共享存储里的持久 KV。前者由引擎管理，后者需要专门的客户端、元数据服务、TTL 和 GC。Kubernetes 只提供存储卷和网络，不理解 KV 的语义；PD 分离的 KV 传输通常走 RDMA，而不是挂一个 PVC。

存储的故障域要和计算分开。如果 KV store 和推理实例在同一批节点上，一次节点故障会同时丢计算和缓存；如果分开，计算可以重建，缓存可以从副本恢复。代价是网络延迟和跨域流量，需要异步预取和本地缓存来掩盖。

## 35.9 可观测性

Kubernetes 上的推理服务要暴露四类信号。

**应用指标**：请求数、错误数、token 数、TTFT/TPOT、队列长度、KV 命中率、抢占、推测接受率。用 Prometheus 抓取，Grafana 展示，Alertmanager 告警。

**GPU/NPU 指标**：利用率、HBM 带宽、显存占用、温度、功耗、ECC 错误、NVLink 流量。NVIDIA 用 DCGM Exporter，Ascend 用厂商的监控组件。GPU 掉卡和 ECC 错误要单独告警，因为它们不会表现为普通的 HTTP 错误。

**日志**：结构化 JSON，带 request id、tenant id、model manifest fingerprint、实例 id。日志采集用 Fluent Bit/Vector，落到集中存储，设置保留期限和脱敏规则。

**追踪**：OpenTelemetry Collector 接收 OTLP，采样后写到 Jaeger/Tempo。推理请求的 span 包括网关排队、prefill、decode step、KV 读取、通信、采样。追踪的开销要控制，通常只对一小部分请求采样。

可观测性本身也要有容量规划。高并发下每个请求都打一条详细日志，日志系统会先于推理系统崩溃。采样、聚合和分级是必须的。

## 35.10 多租户与隔离

Kubernetes 的 Namespace 适合隔离团队和资源，但推理服务的租户通常更细：一个 Namespace 里可能有几百个 API 租户。租户隔离要靠网关和引擎共同实现。

**资源配额**：每个租户的并发、token/分钟、最大 prompt、最大输出。网关做粗粒度限流，引擎做细粒度准入。

**KV 隔离**：不同租户的前缀哈希加盐，避免共享 block；KV 存储按租户分目录或分桶；请求结束后清零或标记不可复用，防止读到残留数据。

**网络隔离**：NetworkPolicy 限制 Pod 之间的横向流量；租户不能直接访问其他租户的实例或 KV store。

**审计**：记录谁在什么时候调用了哪个模型、用了多少 token、是否命中缓存。审计日志要防篡改、可检索、有保留期限。

**故障隔离**：一个大租户的突发流量不应该拖垮其他租户。可以用优先级队列、配额、独占实例池，或者按租户分片。独占隔离最好，但成本高；共享池利用率高，但需要更复杂的公平调度。

## 35.11 一个最小部署清单

`deploy/k8s.yaml` 给出了一个可运行的最小组合。上线前逐项检查：

- 镜像 tag 是唯一且不可变的，不是 `latest`；
- 资源请求和限制已设置，GPU 请求等于限制；
- startup/readiness/liveness 探针覆盖加载和运行两个阶段；
- preStop 调用 drain，terminationGracePeriodSeconds 覆盖最长请求；
- PDB 保证维护期间有足够副本；
- HPA 使用有意义的自定义指标，缩容窗口足够长；
- 模型缓存是只读共享存储，不是镜像内权重；
- NetworkPolicy 限制入口和横向流量；
- ServiceMonitor/PodMonitor 已配置，指标能被抓取；
- 日志、追踪、GPU 指标已接入；
- 多租户配额、KV 隔离、审计已配置；
- 回滚镜像和配置已准备好，且 KV 格式兼容。

这个清单比任何 benchmark 都重要。一个性能很好但探针配置错误、缩容直接杀请求的服务，上线后可能一次滚动更新就造成大面积超时。

## 35.12 成本与调度

Kubernetes 的成本优化空间很大，但前提是理解推理负载。

**节点池按角色划分。** P 实例用普通 8 卡节点，D 实例用 64 到 128 卡的 supernode，VE 用便宜的 GPU。不同类型的节点有不同的价格、互联和供应情况，混在一起会让调度和成本都变复杂。

**利用 bin packing 提高利用率。** 小模型实例可以共享节点；大模型实例通常独占节点。调度器要按 GPU 显存、NUMA、NVLink 拓扑做 bin packing，而不是只看 GPU 数量。

**Spot/竞价实例适合无状态、可中断的负载。** D 实例如果有 grace period 和 KV 转发，可以用一部分 spot 降低成本；但这不是无条件的：D 持有请求级 KV，只有 KV 已外置到共享存储、或者能在中断窗口内转发/重建时，才适合 spot。P 实例通常更适合按需或预留，因为它是 TTFT 的关键路径。

**闲置成本要算进去。** 为了峰值买的机器，低峰闲置，实际利用率可能只有 30%；这时的每有效 GPU 小时成本是 70% 利用率的两倍多。混合云、预留实例和竞价实例可以把闲置成本转嫁给云，但会牺牲稳定性和数据驻留。

## 35.13 多机 EP 的 gang scheduling

一个 EP64 的 decode 实例需要 64 个 Pod 同时就绪才能开始服务。如果 Kubernetes 先启动 63 个、最后一个因为 GPU 不足一直 Pending，这 63 个 Pod 会占着 GPU 空转，其他任务也拿不到资源。这就是典型的“gang scheduling”问题：一组 Pod 要么一起启动，要么都不启动。

Kubernetes 原生调度器是逐个 Pod 调度的，不保证整组原子性。解决方式有三类。第一，**调度器插件**：Volcano、Kueue、Slurm operator 等支持 PodGroup、minMember、队列和抢占，能把 64 个 Pod 作为一个 gang 排队。第二，**资源预留**：给 EP 组预留整机或整组 GPU，用 node affinity/topology spread 把 Pod 钉在特定节点，避免碎片化。第三，**应用层降级**：如果 gang 起不来，自动缩小 EP size 或切到较小实例，但这需要引擎支持动态 EP（第 15.6 节）。

gang scheduling 还要和拓扑一起考虑。EP64 需要 64 张卡在同一个 NVLink/supernode 域内；如果调度器把它们分散到不同机架，all-to-all 的跨机延迟会吃掉性能。Kubernetes 的 `topologySpreadConstraints` 可以控制分布，但“分散”和“聚在一起”是相反的目标：高可用希望分散，EP 性能希望聚拢。一个实用的做法是按“supernode/机架”定义 topology key，让同一 EP 组尽量落在同一域内，同时用多副本做故障域隔离。

GPU 碎片是另一个问题。一个节点有 8 张卡，如果前面跑了 3 个单卡任务，只剩 5 张，EP8 就排不进去；调度器要么等，要么抢占，要么把任务放到别的节点。生产集群通常给推理任务单独的节点池，用 taint/toleration 隔离，避免和训练/批处理抢卡；同时用 Kueue 的队列配额限制每个团队的并发，防止一个团队占满整个集群。

最后要监控“排队时间”和“启动时间”。gang 的 P95 排队时间、从 Pending 到 Ready 的时间、因拓扑不满足而重调度的次数，都是容量和调度策略的指标。只看 GPU 利用率会忽略“任务在等卡”的成本；一个集群 GPU 利用率 90%，但新任务平均等 10 分钟，对在线业务来说仍然不可用。

## 35.14 GPU 共享：整卡、MIG、MPS 与时间片

前面的调度都假设“一个 Pod 独占一张 GPU”。真实集群里还有三种共享方式，它们对推理服务的影响完全不同，选错会比“卡不够”更糟。

**整卡独占**是默认方式，也是大模型推理最安全的方式。设备插件把整张 GPU 作为一个扩展资源分配，Pod 拿到完整的 HBM、SM 和 NVLink 带宽；EP/TP 组可以按拓扑聚在一起，故障域清晰。代价是利用率：一个 7B 模型可能只用到 30% 的算力，剩下 70% 闲置。对延迟敏感、需要稳定 SLO 的服务，这个代价通常是值得的，因为共享带来的尾延迟抖动很难用调度弥补。

**MIG（Multi-Instance GPU）**把一张 GPU 切成若干隔离实例，每个实例有独立的 SM 切片、L2 切片和 HBM 切片。A100/H100 支持多种 profile，例如把 80 GB 卡切成 7 个 10 GB 左右的实例，或者切成 2 个 40 GB 的实例。MIG 的优点是计算和显存隔离，一个实例 OOM 不会拖垮另一个；适合小模型、多租户、开发和评测。限制也很明确：profile 是固定的，不能任意组合；实例之间的互联和共享资源要按厂商文档确认；大模型或 EP 组通常无法跨 MIG 实例获得想要的带宽。把 EP8 硬塞进 8 个 MIG 切片，得到的不是“8 卡 EP”，而是一组互相隔离的小卡，all-to-all 的拓扑假设完全不成立。

**MPS（Multi-Process Service）**允许多个进程同时向一张 GPU 提交 kernel，由硬件调度器并发执行。它能提高小模型的利用率，减少上下文切换；但进程之间共享显存空间，没有故障隔离，一个进程的非法访问可能影响其他进程。MPS 适合“多个可信的小模型共享一张卡”，不适合多租户隔离，也不适合把大模型的 EP 组切碎。

**时间片（time-slicing）**让多个 Pod 轮流使用同一张 GPU，由设备插件配置共享份数。它没有显存隔离，也没有计算隔离；上下文切换会带来延迟抖动。它适合开发和批处理，能让多个小任务“都能跑起来”，但不适合在线推理的 P99。把时间片当成“提高利用率”的手段，往往会在流量高峰时得到一组互相拖慢的实例。

选择哪一种，先问三个问题。第一，模型能不能放进一个切片？如果不能，MIG 和时间片都没有意义，只能用整卡。第二，EP/TP 组需不需要高带宽互联？如果需要，整卡加拓扑调度是唯一稳妥的方案。第三，隔离要求是什么？多租户、合规、故障隔离要求高时，MIG 优于 MPS，MPS 优于时间片；只追求开发和批处理吞吐时，时间片最便宜。

Kubernetes 侧的配置也不同。整卡用 `nvidia.com/gpu: 1`；MIG 用 `nvidia.com/mig-<profile>: 1`，节点要预先创建 MIG 实例并打标签；MPS 需要以 DaemonSet 方式启动 MPS control daemon，并让容器共享 `/tmp/nvidia-mps`；时间片由设备插件的 `sharing.timeSlicing` 配置控制，Pod 仍然请求整卡资源，但物理上被复用。无论哪种方式，监控都要能区分“物理 GPU 利用率”和“Pod 实际拿到的算力”，否则会把“多个 Pod 抢一张卡”误判成“模型变慢了”。

对推理容量规划来说，共享方式会改变单位经济。整卡独占时，成本按 GPU 小时算，利用率低就是浪费；MIG 可以把一张卡拆给多个小模型，按切片计价，单位成本可能更低，但每个切片的 KV 容量和带宽也按比例缩小；时间片能把闲置算力卖出去，但 SLO 无法保证。第 30 章的单位成本模型要带上这个维度，不能只比较“每 GPU 小时多少钱”。

mini-deepinfer 不涉及 GPU 共享，但 `edge.py` 和 `roofline.py` 提供了类似的“设备能力与工作点”模型：给定显存、带宽和算力，判断一个请求能不能放下、延迟是多少。你可以把这些模型看成“单设备版的共享决策”：先算清楚一个工作点需要多少资源，再决定是独占、切片还是排队。

## 小结

Kubernetes 为推理服务提供了调度、探针、滚动更新、弹性和隔离的标准能力，但它不理解推理的语义：GPU 拓扑、KV 交接、PD 分离、drain、模型 manifest。一个可用的部署要把两者接起来：镜像固定、非 root、只读根；Pod 资源、共享内存、拓扑约束明确；探针区分启动、就绪和存活；preStop 做 drain；HPA 用队列/KV/SLO 指标；网关处理 SSE、超时、鉴权和配额；存储把模型缓存和 KV 分开；可观测性覆盖应用、GPU、日志和追踪；多租户做配额、KV 隔离和审计。`deploy/` 目录是一个最小起点，生产化要按本章清单逐项补齐。

## 练习

1. 画出一次请求从 Ingress 到 Pod 再到 GPU 的路径，标出每一跳的超时、重试和鉴权位置。
2. 为什么 liveness 探针不应该检查 3FS？如果检查了，3FS 抖动会发生什么？
3. 设计一个基于队列长度和 P95 TTFT 的 HPA 策略，说明两个指标的权重、窗口和扩容速率上限。
4. 一个 D 实例收到缩容通知，写一次完整的 drain 流程：停止接新请求、处理 running、转发 KV、释放资源、退出。
5. 用 `deploy/k8s.yaml` 做一次改造：把 CPU 资源换成 GPU，加上 node affinity 和 topology spread constraints，并说明每个字段的作用。
