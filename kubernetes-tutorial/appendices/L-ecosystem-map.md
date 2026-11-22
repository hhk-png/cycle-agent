# 附录 L 云原生生态地图与选型方法

> Kubernetes 本身只提供 API、控制循环和少量内置资源。生产平台的能力来自一层层外部组件：运行时、CNI、CSI、网关、可观测性、安全、GitOps、批处理、多集群。本附录给出一张可讨论的生态地图，以及比“哪个项目更火”更可靠的选型方法。

## L.1 为什么要看生态

一个常见误区是“Kubernetes 装好就完事”。实际平台至少还要回答：

- Pod 怎么拿到 IP、怎么跨节点通信、怎么做网络策略？
- 镜像从哪里拉、怎么签名、怎么扫描？
- 卷从哪里来、怎么做快照、怎么扩容？
- 南北向流量怎么进、TLS 怎么终止、怎么做灰度？
- 指标、日志、追踪、事件怎么采集、怎么关联、怎么控制成本？
- 谁改了集群、改了什么、怎么回滚？
- GPU/批处理/多集群/边缘怎么接入？

这些问题的答案通常不是“核心 Kubernetes”，而是**生态组件 + 组织流程**。生态越大，选型越需要边界。

## L.2 一张分层地图

```text
┌─────────────────────────────────────────────────────────────┐
│ 平台工程 / 开发者体验：Backstage、Crossplane、Port、内部门户   │
├─────────────────────────────────────────────────────────────┤
│ 交付：Argo CD、Flux、Argo Rollouts、Flagger、Tekton、Argo Workflows │
├─────────────────────────────────────────────────────────────┤
│ 可观测性：Prometheus、Grafana、Loki、Tempo、Jaeger、OTel、OpenCost │
├─────────────────────────────────────────────────────────────┤
│ 安全：OPA/Gatekeeper、Kyverno、Falco、Tetragon、Trivy、Cosign   │
├─────────────────────────────────────────────────────────────┤
│ 网络：Calico、Cilium、Antrea、Ingress NGINX、Envoy Gateway、Istio │
├─────────────────────────────────────────────────────────────┤
│ 存储：CSI 驱动、Ceph/Rook、Longhorn、OpenEBS、Velero             │
├─────────────────────────────────────────────────────────────┤
│ 运行时：containerd、CRI-O、gVisor、Kata、Firecracker             │
├─────────────────────────────────────────────────────────────┤
│ 集群：kubeadm、k3s、Talos、kind、EKS/GKE/AKS、OpenShift、Rancher │
├─────────────────────────────────────────────────────────────┤
│ 多集群/边缘/虚拟化：Cluster API、Karmada、vCluster、KubeVirt、KubeEdge │
└─────────────────────────────────────────────────────────────┘
```

层次不是严格的依赖，而是**责任边界**。例如 Cilium 同时覆盖 CNI、网络策略、Service 和可观测性；Istio 同时覆盖流量管理、mTLS 和遥测。边界越模糊，越要明确“谁是事实来源、谁负责故障”。

## L.3 集群发行版与托管服务

| 类型 | 代表 | 适合 | 代价 |
|---|---|---|---|
| 本地实验 | kind、minikube、k3d | 学习、CI、快速验证 | 不是生产 |
| 轻量发行版 | k3s、k0s、MicroK8s | 边缘、小集群、资源受限 | 生态/支持差异 |
| 通用发行版 | kubeadm、Talos、OpenShift、Rancher | 自建、可控、合规 | 运维责任大 |
| 托管控制面 | EKS、GKE、AKS、ACK、TKE | 降低控制面运维 | 费用、锁定、网络/权限差异 |
| Serverless 容器 | Cloud Run、Fargate、ACI、Knative | 请求驱动、低运维 | 冷启动、限制、可移植性 |

选型时不要只比较“有没有某个功能”，要比较：

- **控制面 SLA 与升级窗口**：谁负责 etcd、证书、版本偏斜？
- **网络模型**：Pod CIDR、Service CIDR、双栈、NetworkPolicy、出口。
- **身份**：云 IAM 与 Kubernetes RBAC 怎么映射（IRSA、Workload Identity）？
- **存储**：CSI 驱动、快照、跨 AZ、加密。
- **可观测与审计**：控制面日志、审计日志、指标保留。
- **退出成本**：迁移到另一家要改多少 YAML、IAM、网络、存储？

## L.4 运行时与沙箱

- **containerd**：默认/最常见，稳定，生态广。
- **CRI-O**：偏 OpenShift/轻量，和 CRI 对齐紧密。
- **Docker Engine + cri-dockerd**：历史路径，不是新集群首选。
- **gVisor**：用户态内核，强隔离，系统调用兼容性和性能有代价。
- **Kata Containers**：VM 级隔离，接近容器体验，需要虚拟化支持。
- **Firecracker**：轻量 microVM，常用于 Serverless/多租户。

选型问题：

1. 威胁模型是“防误操作”还是“防恶意租户”？
2. 能否接受系统调用兼容性/性能损失？
3. 节点是否支持嵌套虚拟化/KVM？
4. 镜像、GPU、特权容器是否需要直通？

RuntimeClass 让不同工作负载选择不同运行时，但不要假设“换个 RuntimeClass 就自动安全”；节点配置、内核、设备直通都要一起验证。

## L.5 网络：CNI 与数据面

| 项目 | 特点 | 常见场景 |
|---|---|---|
| Calico | BGP/Overlay、NetworkPolicy、成熟 | 通用自建/混合云 |
| Cilium | eBPF、L3-L7 策略、Service、可观测 | 高性能/零信任/可观测 |
| Antrea | Open vSwitch、Kubernetes 原生 | 企业/VM 混合 |
| Flannel | 简单 Overlay | 学习/小集群 |
| 云 CNI | 与 VPC 集成、原生路由 | 托管云 |

关键判断：

- 是否需要 **NetworkPolicy**？是否需要 L7 策略？
- 是否需要 **BGP/Underlay**、固定 Pod IP、与 VM/裸金属互通？
- 是否需要 **双栈**、多网卡、SR-IOV、RDMA？
- 是否接受 eBPF 对内核版本的要求？
- 谁负责 kube-proxy 替换、Service 转发、conntrack 调优？

CNI 是“Pod 网络的事实来源”；NetworkPolicy 只是 API，执行靠 CNI。选 CNI 时要把“策略执行、可观测、性能、升级”一起评估。

## L.6 存储：CSI 与数据服务

- **CSI 驱动**：云盘、文件存储、对象存储网关、本地盘。
- **Ceph/Rook**：统一块/文件/对象，运维复杂但能力强。
- **Longhorn**：轻量分布式块存储，适合边缘/小集群。
- **OpenEBS**：多种本地/复制引擎。
- **Velero**：备份/恢复/迁移 Kubernetes 资源与 PV（依赖存储快照或 Restic/Kopia）。

选型问题：

1. 需要 **RWO/RWX/ROX** 哪种访问模式？
2. 是否需要快照、克隆、扩容、加密、跨 AZ？
3. 延迟/IOPS/吞吐是否满足数据库/训练？
4. 谁负责数据重建、备份、恢复演练？
5. 卷的生命周期与 Pod/节点故障如何交互？

存储是最容易被“能挂上”误导的领域。能挂载不等于能恢复，能恢复不等于能满足 RPO/RTO。

## L.7 网关、Ingress 与服务网格

- **Ingress NGINX**：**已退役（2026 年 3 月停止维护）**，不再接收安全补丁；不要用于新部署。存量集群应迁移到 Gateway API 实现或厂商支持的控制器，可先用 `ingress2gateway` 转换清单。
- **Traefik**：动态配置、易用，适合中小规模；同时支持 Ingress 与 Gateway API。
- **Envoy Gateway / Gateway API**：面向下一代网关 API，角色分离、表达力强；新部署的默认候选之一。
- **Istio**：流量管理、mTLS、遥测、策略，功能全但复杂度高。
- **Linkerd**：轻量、简单、mTLS/可观测，功能相对聚焦。
- **Cilium Service Mesh / Ambient**：eBPF/无 sidecar 方向，减少注入成本。

边界问题：

- 南北向（Ingress/Gateway）与东西向（Service Mesh）是否由一个组件承担？
- 是否需要 L7 灰度、重试、超时、熔断、mTLS、授权？
- Sidecar 的资源/延迟/升级成本能否接受？
- 是否愿意把网络策略与网格策略统一到一套 CRD？

不要因为“服务网格很热”就引入；如果只有几个服务、没有 mTLS/细粒度流量治理需求，Ingress + NetworkPolicy 可能足够。

## L.8 可观测性

| 能力 | 常见组件 | 关键问题 |
|---|---|---|
| 指标 | Prometheus、Thanos、VictoriaMetrics、Mimir | 保留期、基数、远程写 |
| 日志 | Loki、ELK/OpenSearch、Fluent Bit/Vector | 采集开销、索引成本、脱敏 |
| 追踪 | Tempo、Jaeger、OTel Collector | 采样、上下文传播、存储 |
| 可视化/告警 | Grafana、Alertmanager | 告警质量、值班 |
| 成本 | OpenCost、Kubecost | 分摊、标签规范 |
| 事件 | Kubernetes Events、事件导出 | 保留、聚合、噪声 |

原则：

- 先定义 SLO/黄金信号，再选工具。
- 指标基数（label 组合）是成本与稳定性的头号杀手。
- 日志/追踪要有采样与保留策略；全量长期保留通常不经济。
- 用 OpenTelemetry 统一采集与语义约定，降低锁定。
- 可观测性组件本身也要高可用、限流、隔离，避免“监控拖垮集群”。

## L.9 安全、策略与供应链

- **策略**：OPA/Gatekeeper、Kyverno、ValidatingAdmissionPolicy（CEL）。
- **运行时检测**：Falco、Tetragon、eBPF 审计。
- **镜像扫描**：Trivy、Grype、云仓库扫描。
- **签名/来源**：Cosign/Sigstore、SLSA、in-toto。
- **密钥**：External Secrets、Sealed Secrets、Vault、云 KMS。
- **网络**：NetworkPolicy、Cilium L7、mTLS。
- **基线**：CIS Benchmark、kube-bench、Pod Security Standards。

分层思路：

```text
构建：依赖扫描、镜像扫描、签名、SBOM
分发：仓库准入、签名验证、复制策略
部署：RBAC、准入策略、PSA、资源配额
运行：沙箱、seccomp、最小权限、运行时检测
响应：审计、告警、隔离、取证、轮换
```

任何单点工具都不能覆盖全链路；选型时要问“它拦在哪一层、失败时是 fail-open 还是 fail-closed”。

## L.10 GitOps、CI/CD 与渐进式交付

- **Argo CD**：Application/ApplicationSet，UI 友好，生态广。
- **Flux**：控制器组合，GitOps 原生，适合平台化。
- **Argo Rollouts / Flagger**：Canary/蓝绿，基于指标自动推进/回滚。
- **Tekton / Argo Workflows**：Kubernetes 原生流水线。
- **Kustomize / Helm**：渲染层；两者不是 GitOps 本身。

关键设计：

- Git 是期望状态的事实来源；集群是执行结果。
- 环境/应用/所有权如何分仓？谁来审批生产变更？
- 密钥如何进入集群（SOPS、Sealed Secrets、External Secrets）？
- 漂移如何处理：自动同步、告警、还是阻止手动变更？
- 回滚是 Git revert 还是运行时回滚？两者的一致性如何保证？

GitOps 不是“装个 Argo CD”。没有仓库结构、权限、审批、密钥管理和回滚策略，只会把混乱搬到 Git。

## L.11 批处理、AI 与数据

- **Kueue**：队列/配额/借还，作业准入后才创建 Pod。
- **Volcano/YuniKorn**：gang scheduling、队列、批处理调度。
- **Kubeflow**：训练/流水线/Notebook。
- **Ray on Kubernetes**：分布式计算。
- **KServe**：模型推理服务。
- **vLLM/Triton**：推理运行时。
- **NVIDIA GPU Operator**：驱动、Device Plugin、DCGM、MIG。
- **JobSet**：一组相关 Job 的编排。
- **Argo Workflows**：DAG 流水线。

选型问题：

1. 作业是“服务型”还是“批处理型”？是否需要排队/公平/抢占？
2. 是否需要 gang scheduling、拓扑感知、RDMA？
3. GPU 是独占、MIG、时间片还是 DRA 共享？
4. 训练/推理的指标、扩缩容、成本如何采集？
5. 数据从哪里来、checkpoint 存哪里、故障如何恢复？

AI 平台的难点通常不在“跑一个 Pod”，而在队列、拓扑、存储吞吐、故障恢复和成本分摊。

## L.12 多集群、边缘与虚拟化

- **Cluster API**：用 Kubernetes 对象管理集群生命周期。
- **Karmada**：多集群调度、分发、故障转移。
- **vCluster**：共享宿主集群上的虚拟控制面。
- **KubeVirt**：VM 作为 Kubernetes 对象。
- **Knative**：请求驱动/事件驱动 Serverless。
- **KubeEdge/OpenYurt**：边缘节点与云边协同。
- **Submariner/Liqo**：跨集群网络互联。

判断：

- 多集群是为了**隔离/合规/容灾/就近**，还是只是为了“看起来高可用”？
- 控制面、网络、身份、策略、可观测是否统一？
- 故障转移是 DNS、全局 LB、还是应用层？
- 边缘是否容忍控制面断连？本地自治到什么程度？

多集群的成本不只是“多几个控制面”，而是重复的 agent、网络、备份、升级和人员。

## L.13 平台工程与开发者体验

- **Backstage**：服务目录/脚手架/文档门户。
- **Crossplane**：用 Kubernetes API 管理云资源。
- **Port/内部平台**：自助式环境/服务创建。
- **Tekton/Argo Workflows**：流水线。
- **OpenCost/Kubecost**：成本可见性。

平台工程的目标不是“再包一层 kubectl”，而是降低开发者的认知负荷：

- 黄金路径：从代码到生产的最短安全路径。
- 自助服务：环境、数据库、队列、密钥、域名。
-  guardrails：配额、策略、默认安全配置。
- 可观测的自助：日志/指标/追踪开箱可用。
- 反馈闭环：DORA 指标、工单、满意度。

平台团队要像产品团队一样运营内部平台：有用户、有路线图、有 SLA、有弃用策略。

## L.14 选型矩阵

用一个加权矩阵代替“感觉”：

| 维度 | 问题 | 权重示例 |
|---|---|---|
| 用例匹配 | 是否解决当前最关键的问题？ | 25% |
| 成熟度 | 版本、采用者、维护活跃度、CVE 响应 | 15% |
| 生命周期 | 是否已 EOL/停止安全更新？维护窗口还有多久？ | 10% |
| 可运维性 | 安装、升级、排障、备份、可观测 | 15% |
| 生态与标准 | 是否符合 CRI/CNI/CSI/Gateway API 等 | 10% |
| 团队能力 | 现有技能、学习曲线、招聘 | 10% |
| 成本 | 许可、基础设施、人力、退出成本 | 10% |
| 风险 | 锁定、治理、路线图、支持 | 5% |

评分时给每一项写**证据**：不是“Cilium 性能好”，而是“在我们的内核版本与流量模型下，P99 延迟/CPU 占用是多少”。

## L.15 成熟度判断与反模式

判断项目成熟度可以看：

- 是否有稳定的 API/版本策略与弃用窗口。
- 是否有多个独立生产采用者（不只是供应商案例）。
- 是否通过一致性/互操作测试（如 Kubernetes conformance、Gateway API conformance）。
- 维护者多样性、issue/PR 响应、安全公告流程。
- 升级路径与破坏性变更记录。
- 文档是否覆盖故障与恢复，而不只是“快速开始”。

CNCF 的 Sandbox/Incubating/Graduated 是有用信号，但**不是生产就绪的充分条件**；具体版本、使用方式、团队能力仍然决定成败。

常见反模式：

1. **简历驱动**：为了用新技术而引入，没有明确问题。
2. **Logo 驱动**：把“CNCF 项目”当成“生产可用”。
3. **工具堆叠**：每个问题装一个 Operator，最后没人能升级。
4. **单点全能**：让一个组件同时做 CNI、网关、网格、可观测，故障域过大。
5. **忽略退出成本**：没有抽象层，换实现要改所有应用。
6. **先买后补**：先上商业平台，再补权限/网络/成本治理。
7. **只做安装**：没有升级、备份、演练、值班，等于把故障延后。

## L.16 组合示例

**小团队（< 20 服务）**

- 托管 K8s + 云 CNI/CSI + Gateway API 实现（Envoy Gateway/Traefik）或云网关；不要新装 Ingress NGINX。
- Prometheus/Grafana/Loki 托管或轻量自建。
- Argo CD + Kustomize；Sealed Secrets/External Secrets。
- 先不引入服务网格；用 NetworkPolicy + mTLS（如有需要）。

**中大型平台（多团队）**

- 多集群/多环境，Cluster API 或托管 + GitOps。
- Cilium/Calico + Gateway API + 策略引擎（Kyverno/OPA）。
- 统一 OTel 采集、SLO、成本分摊。
- 平台门户/脚手架、配额与审批、租户隔离。

**AI 平台**

- GPU Operator + Device Plugin/DRA + 拓扑感知。
- Kueue/Volcano + JobSet/Ray/Kubeflow。
- 高吞吐存储 + checkpoint + 推理网关（KServe/vLLM）。
- GPU 利用率/显存/温度/成本可观测。

**边缘**

- k3s/k0s/KubeEdge + 轻量 CNI/存储。
- 离线/断连自治、差分升级、设备接入。
- 控制面回传带宽与安全边界。

## L.17 选型检查清单

- [ ] 我们解决的是哪个具体问题？不用这个项目会怎样？
- [ ] 它在我们的版本/内核/云/规模下验证过吗？
- [ ] 谁负责安装、升级、备份、排障、值班？
- [ ] 失败模式是什么？fail-open 还是 fail-closed？
- [ ] 退出成本多大？数据/配置/API 能否迁移？
- [ ] 是否符合标准接口（CRI/CNI/CSI/Gateway API/OTel）？
- [ ] 是否有可观测性、配额、租户隔离？
- [ ] 许可证/支持/治理是否可持续？
- [ ] 我们能否在预发环境做一次故障演练？

## 延伸阅读

- [第 15 章 CNI、eBPF 与 NetworkPolicy](../chapters/15-cni-ebpf-networkpolicy.md)
- [第 16 章 服务网格与零信任](../chapters/16-service-mesh-zero-trust.md)
- [第 32 章 GitOps 与渐进式交付](../chapters/32-gitops-progressive-delivery.md)
- [第 33 章 多集群、集群生命周期与虚拟集群](../chapters/33-multicluster-cluster-api.md)
- [第 41 章 通用计算平台](../chapters/41-general-compute-platform.md)
- [附录 E 延伸阅读](E-reading.md)
