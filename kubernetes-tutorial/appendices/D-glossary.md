# 附录 D 术语表

## A–C

- **Admission Controller**：API Server 在持久化前执行的准入插件，分为 mutating/validating。
- **Affinity**：Pod/节点亲和性，控制调度位置。
- **APF（API Priority and Fairness）**：API Server 的请求分类/排队/限流机制。
- **API Group**：Kubernetes API 的分组，如 `apps`、`batch`。
- **Argo CD**：GitOps 持续交付工具，以 Application 管理集群状态。
- **Cluster API（CAPI）**：用 Kubernetes 对象管理集群生命周期。
- **CRD（CustomResourceDefinition）**：注册自定义 API 类型。
- **CRI（Container Runtime Interface）**：kubelet 与容器运行时的接口。
- **CSI（Container Storage Interface）**：存储插件标准接口。
- **CNI（Container Network Interface）**：网络插件标准接口。
- **ClusterIP**：Service 的集群内部虚拟 IP。
- **ConfigMap**：非敏感配置对象。
- **Compaction**：清理 etcd 旧 revision，回收历史版本空间。
- **Cohort**：Kueue 中共享配额借还的队列组。
- **Conformance**：Kubernetes 一致性认证，验证发行版是否满足 API/行为要求。
- **Controller**：把实际状态调谐到期望状态的循环。
- **Cordon**：标记节点不可调度。
- **CronJob**：定时创建 Job 的资源。

## D–G

- **DaemonSet**：每个符合条件的节点运行一个 Pod。
- **Deployment**：管理无状态应用发布和 ReplicaSet。
- **Device Plugin**：kubelet 发现/分配 GPU 等设备的 gRPC 插件。
- **Defrag**：整理 etcd 数据文件碎片，回收磁盘空间。
- **DRA（Dynamic Resource Allocation）**：动态设备/资源分配。
- **Drain**：驱逐节点上的 Pod 以进行维护。
- **EndpointSlice**：Service 后端的分片列表。
- **Endpoints**：Service 后端的传统对象。
- **etcd**：Kubernetes 的一致性键值存储。
- **Eviction**：驱逐 Pod（自愿/资源压力）。
- **Finalizer**：删除前的清理钩子。
- **Gateway API**：Ingress 的下一代 L7/L4 路由 API。
- **Generation**：对象 spec 的代次，spec 变化才递增。
- **Flux**：一组 GitOps 控制器（source/kustomize/helm 等）。
- **GitOps**：以 Git 为期望状态源，控制器持续同步。
- **GPUDirect/RDMA**：GPU/高性能网络技术。
- **gRPC**：高性能 RPC 框架，常用于 CRI/CSI。

## H–L

- **Gang scheduling**：一组 Pod 要么全部调度、要么全部不调度。
- **HPA（HorizontalPodAutoscaler）**：水平 Pod 自动伸缩。
- **Headless Service**：`clusterIP: None`，DNS 直接返回 Pod IP。
- **IDP（Internal Developer Platform）**：内部开发者平台。
- **Ingress**：L7 路由资源。
- **Init Container**：主容器前顺序执行的初始化容器。
- **Informer**：List/Watch + 本地缓存的客户端机制。
- **Job**：运行到完成的任务。
- **Karmada**：多集群分发/调度/故障转移。
- **Knative**：请求驱动/事件驱动的 Serverless 层。
- **Kueue**：Kubernetes 原生的作业队列/配额准入系统。
- **Kubelet**：节点代理，管理 Pod 生命周期。
- **KubeVirt**：在 Kubernetes 中运行虚拟机。
- **kube-proxy**：实现 Service 转发的节点组件。
- **Kubernetes**：容器编排控制平面。
- **Lease**：用于选主/租约的协调对象。
- **LimitRange**：命名空间内资源默认值/范围。
- **Liveness Probe**：容器存活探针，失败重启。
- **LoadBalancer**：云负载均衡 Service 类型。

## M–R

- **Metrics Server**：提供资源指标 API。
- **MIG（Multi-Instance GPU）**：把 GPU 切成硬件隔离的实例。
- **MVCC**：多版本并发控制，etcd 用 revision 保存对象历史。
- **mTLS**：双向 TLS。
- **Namespace**：逻辑隔离/权限边界。
- **ndots**：DNS 解析时先尝试 search 域的点数阈值。
- **NetworkPolicy**：L3/L4 网络策略。
- **Node**：集群中的工作机器。
- **NodePort**：在每个节点暴露端口的 Service 类型。
- **OIDC**：OpenID Connect，常用于企业身份。
- **Operator**：CRD + 控制器的领域自动化。
- **PDB（PodDisruptionBudget）**：自愿驱逐保护。
- **PersistentVolume（PV）**：集群存储资源。
- **PersistentVolumeClaim（PVC）**：存储请求。
- **Pod**：最小调度/运行单元。
- **Pod Security Admission（PSA）**：命名空间级 Pod 安全标准。
- **PriorityClass**：Pod 优先级。
- **Probe**：startup/readiness/liveness 探针。
- **QoS**：Guaranteed/Burstable/BestEffort。
- **Quorum**：分布式共识所需的多数派，etcd 为 `N/2 + 1`。
- **Readiness Probe**：就绪探针，失败摘流。
- **Revision**：etcd MVCC 的全局版本号，Kubernetes resourceVersion 与之相关。
- **ReplicaSet**：保证 Pod 副本数。
- **ResourceQuota**：命名空间总量配额。
- **RBAC**：基于角色的访问控制。
- **RollingUpdate**：滚动发布策略。
- **RuntimeClass**：选择容器运行时/沙箱。

## S–Z

- **Scheduler**：为 Pod 选择节点。
- **Secret**：敏感数据对象（默认仅 base64）。
- **Service**：稳定虚拟 IP/DNS 的后端集合。
- **ServiceAccount**：Pod 身份。
- **Sidecar**：与主容器同 Pod 的辅助容器。
- **SLSA**：软件供应链完整性等级框架。
- **TCO**：总拥有成本（基础设施、人力、迁移、风险等）。
- **StatefulSet**：稳定身份/存储的有状态工作负载。
- **StorageClass**：动态存储供给配置。
- **Taint/Toleration**：节点排斥/Pod 容忍。
- **Time-slicing**：多个容器分时共享一张 GPU，无显存隔离。
- **TopologySpreadConstraints**：按拓扑域均匀分布。
- **vCluster**：在共享集群上提供虚拟控制面。
- **VPA（VerticalPodAutoscaler）**：垂直资源伸缩。
- **Volume**：Pod 的存储抽象。
- **Wasm（WebAssembly）**：轻量沙箱运行时，可用于函数/插件。
- **Watch**：增量事件流。
- **Webhook**：外部准入/认证/授权回调。
- **Workload**：可部署的应用抽象（Deployment/STS/Job 等）。
- **Workqueue**：控制器去重/限速/重试队列。

## 易混概念

| 概念 | 不是什么 |
|---|---|
| Namespace | 不是网络/内核隔离 |
| requests | 不是硬限制；是调度依据 |
| limits | 不是调度依据；是运行时上限 |
| QoS | 不保证不被驱逐 |
| PDB | 不保护节点故障/手动删除 |
| Ingress | 不是负载均衡器本身 |
| Service | 不是进程/容器 |
| Secret | 不是默认加密 |
| StatefulSet | 不提供数据复制/选主 |
| HPA | 不保证有节点可调度 |
| readiness | 不是 liveness；不触发重启 |
| liveness | 不应检查外部依赖 |

## 延伸阅读

- [第 4 章 API 对象模型](../chapters/04-api-object-model.md)
- [附录 A API 资源速查](A-api-resources.md)
- [附录 E 延伸阅读](E-reading.md)
