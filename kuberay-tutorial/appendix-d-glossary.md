# 附录 D. 术语表

按主题分组；同一术语只出现一次。括号里的章节指向第一次系统讲解的位置。

## D.1 Ray 与运行时

| 术语 | 解释 |
|---|---|
| **Ray** | 分布式计算框架，提供 task/actor/object store/Serve。 |
| **GCS** | Global Control Store，Ray 的全局元数据服务（第 2、35 章）。 |
| **Raylet** | 每个节点上的本地调度/对象管理进程。 |
| **Object Store** | Ray 的共享内存对象存储（Plasma）。 |
| **Plasma** | 对象存储的共享内存实现；容量受 `/dev/shm` 限制。 |
| **Driver** | 执行用户入口的进程。 |
| **Task/Actor** | Ray 的两种计算单元：无状态任务与有状态 actor。 |
| **Placement Group** | 一组原子调度的资源；常用于 gang scheduling。 |
| **Serve Controller** | 管理 Serve deployment 的控制器。 |
| **Proxy** | Serve 的 HTTP 入口；`proxy_location` 决定它跑在哪些节点。 |
| **Deployment** | Serve 的可扩缩副本集合。 |
| **Runtime Env** | Ray 作业的依赖/环境（pip、working_dir、env_vars）。 |
| **targetCapacity** | Serve 的副本容量百分比；增量升级时随流量逐步提高。 |
| **Serve 状态** | `DEPLOYING`/`HEALTHY`/`UNHEALTHY`/`UPSCALING`/`DOWNSCALING` 等。 |
| **JobSupervisor** | Ray 中监督 driver 进程、管理作业状态的组件。 |

## D.2 KubeRay CRD 与控制器

| 术语 | 解释 |
|---|---|
| **RayCluster** | KubeRay CRD，描述 head + worker groups 的长期集群。 |
| **RayJob** | KubeRay CRD，一次性作业 + 可选自动建/删集群。 |
| **RayService** | KubeRay CRD，Ray Serve 服务 + 零停机升级。 |
| **RayCronJob** | KubeRay CRD，按 cron 创建 RayJob。 |
| **Head Pod** | 运行 GCS、Dashboard、Client Server、Raylet 的 Pod。 |
| **Worker Pod** | 运行 Raylet/worker 的 Pod。 |
| **workerGroup** | 一组同构 worker 的模板/副本/资源边界。 |
| **numOfHosts** | 多主机场景下每个 replica 包含的 Pod 数（第 5 章）。 |
| **revision hash** | RayService 根据集群模板计算的版本哈希，变化时触发 pending 集群。 |
| **active/pending** | RayService 当前接流量/正在准备的集群状态。 |
| **serveService** | RayService 暴露 Serve 的稳定 Service。 |
| **ray.io/serve** | 标记 Pod 是否接 Serve 流量的标签。 |
| **upgradeStrategy** | RayCluster/RayService 的升级方式（Recreate/None/NewCluster/增量）。 |
| **submissionMode** | RayJob 的提交方式：K8sJobMode/HTTPMode/SidecarMode/InteractiveMode。 |
| **clusterSelector** | RayJob 复用已有 RayCluster 的选择器。 |
| **deletionStrategy** | RayJob 的细粒度清理规则（deletionRules/onSuccess/onFailure）。 |
| **lastScheduleTime** | RayCronJob 上次成功调度的计划时刻。 |
| **timeZone** | RayCronJob 解释 cron 表达式所用时区。 |
| **CRD** | Custom Resource Definition，自定义资源类型定义。 |
| **Operator** | 用控制器管理 CRD 的程序。 |
| **Reconcile** | 让实际状态逼近期望状态的循环。 |
| **Finalizer** | 删除前必须完成的清理钩子。 |
| **OwnerReference** | 子对象对父对象的归属，用于级联 GC。 |
| **Condition** | 结构化状态（True/False/Unknown + reason）。 |
| **resourceVersion** | 对象版本，用于乐观并发/watch。 |
| **generation** | spec 版本，spec 变化时递增。 |
| **Status subresource** | 只更新 status 的 API 子资源。 |
| **WorkQueue** | 控制器去重/限速队列。 |
| **RateLimiter** | 控制器失败重试的指数退避。 |
| **Leader Election** | 多副本 operator 选主。 |
| **Scale Expectations** | 控制器记录“已期望创建/删除”的 Pod，避免缓存延迟导致重复创建。 |
| **SSA** | Server-Side Apply，按字段所有权合并对象。 |
| **Admission Webhook** | API Server 在写入前调用外部校验/变更的机制；KubeRay 默认不依赖它，但 v1.7 提供可选的 validating webhook。 |
| **OpenAPI Schema** | CRD 的字段校验/裁剪规则；未声明字段可能被静默裁剪。 |
| **managedBy** | 把 CR 交给外部控制器（如 Kueue MultiKueue）的字段。 |

## D.3 扩缩容与调度

| 术语 | 解释 |
|---|---|
| **Autoscaler** | 根据需求调整节点/副本。 |
| **v1/v2** | Ray autoscaler 的两代实现；2.48+ 默认 v2。 |
| **idleTimeoutSeconds** | 空闲多久后缩容。 |
| **upscalingMode** | Default/Aggressive/Conservative。 |
| **workersToDelete** | autoscaler 指定的待删除 Pod。 |
| **Instance Manager** | 维护节点实例状态机（QUEUED/REQUESTED/ALLOCATED/RAY_RUNNING/TERMINATING）的组件。 |
| **NodeProvider** | 把 launch/terminate 请求翻译成基础设施动作（KubeRay/云/本地）的接口。 |
| **ResourceDemandScheduler** | 把 pending 资源请求装进节点类型的调度器。 |
| **Bin Packing** | 多维装箱，决定用多少个节点满足资源请求。 |
| **Demand Vector** | 待调度任务的资源向量（CPU/GPU/内存/自定义资源）。 |
| **Gang Scheduling** | 一组 Pod 要么全调度、要么全不调度。 |
| **PodGroup** | gang scheduling 的 Pod 组；Volcano/scheduler-plugins 使用。 |
| **Kueue** | Kubernetes 作业队列/配额系统。 |
| **ClusterQueue / LocalQueue** | Kueue 的集群/namespace 队列。 |
| **MultiKueue** | Kueue 的跨集群调度模式。 |
| **Volcano/YuniKorn** | 批调度器，支持 gang scheduling。 |
| **WAS** | Kubernetes Workload-Aware Scheduling。 |
| **Cluster Autoscaler** | 根据 Pending Pod 调整节点数的经典组件。 |
| **Karpenter** | 以节点池/实例类型为单位做弹性伸缩的控制器。 |
| **HPA/VPA** | Kubernetes 的水平/垂直 Pod 自动扩缩。 |
| **IPPR** | In-Place Pod Resizing：KubeRay 用 `ray.io/ippr` 注解让 autoscaler v2 原地调大 worker Pod 的 CPU/memory（K8s 1.35+，alpha）。 |
| **resizePolicy** | 容器级 CPU/memory 调整策略；IPPR 要求 `NotRequired`（不重启容器）。 |
| **Native sidecar** | `initContainers` + `restartPolicy: Always` 的 sidecar；有启动/退出顺序保证（K8s 1.33 GA）。 |
| **Scheduling gate** | `spec.schedulingGates`；Pod 被调度器忽略，直到 gate 被移除（K8s 1.30 稳定）。 |
| **trafficDistribution** | Service 的流量分布策略；`PreferSameZone` 优先同区、`PreferSameNode` 优先同节点（1.33/1.35）；旧值 `PreferClose` 在 1.34 弃用。 |
| **Pod-level resources** | `spec.resources` 的 Pod 级资源；1.34 beta，容器未声明时生效。 |
| **ResourceQuota** | namespace 级资源总量配额。 |
| **LimitRange** | namespace 级默认/最小/最大资源限制。 |
| **PriorityClass** | Pod 优先级；影响抢占与驱逐顺序。 |
| **PDB** | PodDisruptionBudget，限制自愿驱逐时的可用副本。 |
| **QoS** | Pod 的服务质量等级（Guaranteed/Burstable/BestEffort）。 |
| **taint/toleration** | 节点污点与 Pod 容忍，用于隔离 GPU/专用节点池。 |
| **nodeSelector/affinity** | 把 Pod 约束到特定节点/拓扑。 |
| **topologySpreadConstraints** | 按拓扑域分散 Pod，避免单点/单机架。 |

## D.4 容错、存储与安全

| 术语 | 解释 |
|---|---|
| **GCS FT** | GCS 容错（Redis/RocksDB）。 |
| **RocksDB backend** | GCS 状态持久化到 head PVC。 |
| **Active-Passive Head** | 主备 head，通过 leader election 切换。 |
| **Redis cleanup finalizer** | 删除 GCS FT 集群前清理 Redis 数据的 finalizer。 |
| **mTLS** | 双向 TLS，客户端和服务端都验证证书。 |
| **SAN** | Subject Alternative Name，证书中的 DNS/IP 标识。 |
| **NetworkPolicy** | Kubernetes 的 Pod 网络允许列表；是否生效取决于 CNI。 |
| **RBAC** | 基于角色的访问控制（Role/ClusterRole/Binding）。 |
| **ServiceAccount** | Pod 访问 API Server 的身份。 |
| **Pod Security Standards** | restricted/baseline/privileged 三档 Pod 安全策略。 |
| **Secret** | 存放令牌/密码/证书的 Kubernetes 对象。 |
| **OIDC** | 用外部身份提供方做认证的协议。 |
| **STRIDE** | 威胁建模框架（欺骗/篡改/否认/信息泄露/拒绝服务/提权）。 |
| **SBOM** | Software Bill of Materials，软件物料清单。 |
| **PVC/StorageClass** | 持久卷声明与存储类型；GCS FT RocksDB/数据缓存使用。 |
| **emptyDir** | Pod 生命周期的临时卷；`/dev/shm` 常用。 |
| **/dev/shm** | 共享内存挂载点；对象存储容量的关键约束。 |
| **Eviction** | kubelet 在资源压力下驱逐 Pod；QoS/优先级影响顺序。 |

## D.4.1 GPU 与异构资源

| 术语 | 解释 |
|---|---|
| **Device Plugin** | kubelet 的设备注册/分配协议；`nvidia.com/gpu` 由它上报。 |
| **GPU Operator** | NVIDIA 提供的驱动/device plugin/DCGM 等组件的 Operator。 |
| **GFD** | GPU Feature Discovery，给节点打 GPU 型号/显存等标签。 |
| **MIG** | NVIDIA Multi-Instance GPU，把一张卡切成硬件隔离的实例。 |
| **time-slicing** | 多个容器按时间片共享 GPU，无显存隔离。 |
| **MPS** | NVIDIA Multi-Process Service，多进程共享 GPU。 |
| **DRA** | Dynamic Resource Allocation，Kubernetes 的结构化设备分配 API。 |
| **ResourceClaim** | DRA 的一次设备分配请求。 |
| **DeviceClass** | DRA 的设备类别。 |
| **NUMA** | 非统一内存访问，影响 CPU/GPU/内存的本地性。 |
| **NVLink** | NVIDIA GPU 间高带宽互连。 |
| **RoCE/IB** | 多机训练常用高带宽网络（RDMA）。 |
| **TPU** | Google 的张量处理器；多主机训练常用 `numOfHosts`/切片。 |

## D.5 网络与入口

| 术语 | 解释 |
|---|---|
| **CNI** | Container Network Interface，负责 Pod 网络接入/IPAM 的插件协议。 |
| **veth** | 虚拟以太网设备对，一端在 Pod netns，一端在节点。 |
| **VXLAN** | 常见 overlay 封装协议，会减少可用 MTU。 |
| **EndpointSlice** | Service 后端 Pod 地址/端口的切片对象，kube-proxy/CNI 据此转发。 |
| **kube-proxy** | 实现 Service 转发的节点组件（iptables/IPVS 模式）。 |
| **IPVS** | 内核四层负载均衡，常用于大规模 Service。 |
| **Headless Service** | `clusterIP: None` 的 Service，直接返回 Pod DNS。 |
| **CoreDNS** | Kubernetes 默认 DNS，提供 Service/Pod 记录。 |
| **ndots** | resolv.conf 中“点数量阈值”，影响 search 域查询顺序。 |
| **Gateway API** | Ingress 的继任者，提供 Gateway/HTTPRoute 等 L7 资源。 |
| **HTTPRoute** | Gateway API 的路由规则；增量升级用它做权重迁移。 |
| **Ingress** | 传统七层入口；KubeRay 可为 Dashboard 创建。 |
| **MTU** | 最大传输单元；overlay 下通常小于 1500。 |
| **GSO/GRO** | 网卡/内核的 TCP 分段/聚合卸载，配置不当会导致大包问题。 |
| **conntrack** | 连接跟踪表，NAT/Service 转发依赖它。 |

## D.6 可观测性与平台工程

| 术语 | 解释 |
|---|---|
| **Jobs API** | Ray Dashboard 8265 上的作业提交接口；Ray Client 的替代路径。 |
| **runtime_env** | Ray 的运行时依赖描述（working_dir/pip/conda/env_vars）。 |
| **Ray Client** | 已废弃的 `ray://` 客户端模式（10001），要求版本精确匹配。 |
| **History Server** | 回放 Ray 事件的服务；集群删除后仍可查看。 |
| **Collector Sidecar** | 采集 Ray 事件并写对象存储的 sidecar。 |
| **SLI/SLO** | 服务水平指标/目标。 |
| **Prometheus** | 指标系统。 |
| **DCGM** | NVIDIA GPU 指标。 |
| **py-spy** | Python 采样分析器。 |
| **OpenTelemetry** | 统一的 traces/metrics/logs 采集标准。 |
| **RED/USE** | 服务（Rate/Errors/Duration）与资源（Utilization/Saturation/Errors）方法论。 |
| **P99** | 99 分位延迟。 |
| **TCO** | Total Cost of Ownership，总拥有成本。 |
| **RTO/RPO** | Recovery Time/Point Objective：恢复所需时间/可接受的数据丢失窗口（第 44 章）。 |
| **Warm Standby / Pilot Light** | 灾备模式：预热备用环境/只保留最小基础设施（第 44 章）。 |
| **GitOps** | 用 Git 作为期望状态来源，由 Argo CD/Flux 同步。 |
| **Helm/Kustomize** | Kubernetes 的两种模板/叠加管理工具。 |
| **APIServer** | KubeRay 的 REST 接口。 |
| **kubectl-ray** | kubectl 插件。 |
| **Informer Cache** | controller-runtime 的本地对象缓存，最终一致。 |
| **APF** | API Priority and Fairness，API Server 的请求分级/限流。 |
| **REP** | Ray Enhancement Proposal，Ray/KubeRay 的设计提案流程。 |
| **envtest** | controller-runtime 的测试环境（真实 apiserver+etcd，无 kubelet）。 |
| **Golden Test** | 用固定期望文件比较完整输出（YAML/JSON）的测试。 |
| **Version Skew** | 组件版本不一致（operator/CRD/Ray/K8s）导致的兼容问题。 |
| **Idempotency** | 同一操作执行多次与执行一次效果相同；重试/补跑的前提。 |
| **Eventual Consistency** | 缓存/状态最终一致；控制器必须容忍短暂的不一致。 |
| **Backoff** | 失败后的退避重试。 |
| **TTL** | 对象/作业在完成后保留的时间。 |
| **Fencing** | 用 token/版本校验阻止旧 leader 在失去租约后继续写入。 |
| **Informer Cache / Cache Sync** | 控制器的本地对象缓存；启动时必须先完成同步。 |
| **WorkQueue** | 控制器的事件队列；去重、限速、指数退避。 |
| **Error Budget** | SLO 允许的失败额度；burn rate 衡量消耗速度。 |

## D.7 Kubernetes 基础

| 术语 | 解释 |
|---|---|
| **Pod** | 最小调度单元，一个或多个共享网络/存储的容器。 |
| **Deployment** | 无状态 Pod 的副本/滚动更新控制器。 |
| **StatefulSet** | 有稳定网络标识/存储的 Pod 控制器。 |
| **Job/CronJob** | 一次性/定时任务控制器。 |
| **Service** | 稳定的虚拟 IP/端口，选择一组 Pod。 |
| **Namespace** | 资源与权限的逻辑隔离边界。 |
| **Node** | 运行 Pod 的机器（物理/虚拟）。 |
| **Availability Zone (AZ)** | 云区域内独立供电/网络的故障域；`topology.kubernetes.io/zone` 标识。 |
| **Failure Domain** | 一起失效的一组资源（进程/Pod/节点/机架/AZ/区域）。 |
| **Kubelet** | 节点上管理 Pod 生命周期的组件。 |
| **kube-scheduler** | 把 Pod 绑定到节点的调度器。 |
| **API Server** | Kubernetes 的声明式 API 入口。 |
| **etcd** | Kubernetes 的一致性键值存储。 |
| **DNS-1035** | 以小写字母开头的 DNS label；Ray CR 名使用。 |
| **preStop** | 容器终止前执行的阻塞钩子；时间计入 grace。 |
| **terminationGracePeriodSeconds** | Pod 终止的总预算（preStop + SIGTERM + drain）。 |
| **Eviction API** | 受 PDB 约束的驱逐接口；`kubectl drain`/CA 使用。 |

## D.8 数据、训练、推理与治理

| 术语 | 解释 |
|---|---|
| **Block** | Ray Data 的最小调度单位（通常是 Arrow/pandas 分片）。 |
| **Backpressure** | 上游按下游消费速度放慢，防止内存/对象存储无限堆积。 |
| **Streaming Executor** | Ray Data 的流式执行器；按 block 流调度而非全量物化。 |
| **Operator Fusion** | 把相邻窄依赖算子融合到同一 task，减少中间落盘。 |
| **Shuffle** | 按 key 重分布数据；通常需要全局屏障、磁盘和网络。 |
| **Spill** | 内存/对象存储不足时把数据写到磁盘。 |
| **Checkpoint** | 训练的可恢复快照（模型、优化器、step、RNG、数据位置）。 |
| **World Size** | 参与训练的 worker/rank 数；弹性训练中可变。 |
| **Rendezvous** | 分布式进程互相发现并确定 rank/world size 的机制。 |
| **TorchElastic** | PyTorch 的弹性训练运行时（torchrun）。 |
| **Global Batch** | `micro_batch × world_size × grad_accum_steps`。 |
| **PagedAttention** | 用固定大小 block + block table 管理 KV cache。 |
| **Prefix Caching** | 复用共享前缀的 KV block，降低 TTFT 与算力。 |
| **Chunked Prefill** | 把长 prompt 的 prefill 切片，与 decode 交错执行。 |
| **PD Disaggregation** | Prefill 与 Decode 分离到不同 worker/节点池。 |
| **ITL / Goodput** | inter-token latency / 满足 SLO 的有效吞吐。 |
| **Span/Trace** | 一次操作/一条调用链；span 有 parent 关系。 |
| **SLO/SLI/Error Budget** | 目标/指标/允许的失败额度。 |
| **Burn Rate** | 错误预算消耗速度相对目标的比例。 |
| **Admission Control** | 对象写入前进行变更/校验的机制。 |
| **CEL** | Kubernetes 的 Common Expression Language 校验。 |
| **Kyverno/Gatekeeper** | 策略即代码引擎（YAML/Rego）。 |
| **SLSA** | 供应链完整性等级（Build L1–L3；SBOM 见 D.4）。 |
| **Data Contract** | 生产者与消费者对 schema/语义/SLA 的显式约定。 |
| **Point-in-Time Join** | 只用预测时刻之前已知的事件计算特征。 |
| **Training/Serving Skew** | 训练与推理的特征/数据分布不一致。 |
| **Model Registry** | 模型版本、阶段、门禁与血缘的注册表。 |
| **Lineage** | 数据/特征/模型/部署之间的来源关系。 |

← [返回目录](README.md)
