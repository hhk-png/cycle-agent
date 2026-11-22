# Kubernetes 深度教程与 MiniK8s 工程实现

> 从 API 对象、调度器、控制器与数据面一路走到生产运维、未来演进和商业分析；同时从零实现一个可运行、可测试、可扩展的迷你 Kubernetes。

本教程不是“命令大全”。它试图回答三个问题：

1. **Kubernetes 为什么这样设计？** 每个机制解决什么失效模式，代价是什么。
2. **它在代码里到底怎么跑？** 用 9000 多行的 Python 实现 MiniK8s，把 API Server、watch、准入、调度、控制器、kubelet、Service、DNS、存储、RBAC 真实跑起来。
3. **生产环境如何取舍？** 高可用、规模化、安全、成本、商业化的工程判断。

## 验证状态

| 项目 | 结果 |
|---|---|
| 单元 / 集成测试 | **217 passed** |
| 端到端示例 | **18 / 18 passed** |
| 章节 | **50 章 + 20 篇附录** |
| 文档检查 | `check_docs.py` 全部通过 |

本机验证命令（Windows Git Bash / Linux / macOS 均可）：

```bash
python -m pytest
python examples/run_all.py
python check_docs.py
```

一次跑完：

```bash
python scripts/verify_all.py
```

## 本版增强

- 新增 **附录 S PKI、证书与身份生命周期**：四套信任域、kubeadm 证书地图、CSR API、kubelet TLS bootstrapping、控制面轮换、投影 ServiceAccount token、front-proxy 聚合层、工作负载身份与 Pod 证书，以及故障速查与检查清单。
- 新增 **附录 T Windows 容器与混合操作系统集群**：process/Hyper-V 隔离、Windows 节点组件、混合调度、win-overlay/l2bridge、CSI Proxy、HostProcess 安全边界、可观测性与 TCO，以及 MiniK8s 扩展练习。
- 新增 **第 2 章 2.11 流式 API**：logs/exec/attach/port-forward 的请求路径、SPDY/WebSocket 通道、子资源授权、日志轮转与生产安全边界。
- 新增 **MiniK8s Gateway API 实现**：`GatewayClass`/`Gateway`/`HTTPRoute`/`ReferenceGrant` 的完整状态契约（`Accepted`、`ResolvedRefs`、`Programmed`、`attachedRoutes`）、`allowedRoutes`（Same/All/Selector）、hostname 交集、`sectionName`/`port` 过滤、跨 namespace `ReferenceGrant`、进程内 L7 路由（Exact/PathPrefix/RegularExpression、header/method/query、加权后端）以及 `HTTPRoute → Service → Endpoints → Pod` 端到端验证。新增 22 个测试、第 18 个示例场景，并更新第 14、38 章。
- 新增 **第 49 章 应用工程：把代码写成能在 Kubernetes 上运行的系统**：从“平台能提供什么、应用必须自己做什么”的运行契约出发，覆盖三类探针的职责边界与时间预算、零停机终止的真实时序与 Go/Java/Python/Node 排空写法、配置/Secret 轮换、JVM/Go/Python/Node/.NET 的资源陷阱、并发与过载模型、超时/重试/幂等/熔断/Outbox/Saga、可观测性契约、kind/Skaffold/Tilt/Telepresence/mirrord 内循环、镜像与滚动升级兼容、既有应用迁移评估与生产就绪清单。
- 新增 **第 50 章 事件驱动、工作流与数据管道**：从同步与异步的边界讲到 Kafka/Pulsar/NATS/RabbitMQ 选型，Strimzi 的副本/ISR/存储/分区/扩缩升级，KEDA 按积压扩缩与缩到零的陷阱，Knative Eventing 的路由/过滤/DLQ，Argo Workflows/Tekton/Airflow/Temporal 的分工，CDC/流处理/湖仓，at-least-once + 幂等 + Outbox + Saga 的一致性工程，以及 lag/SLO、测试、灾备与反模式。
- 新增 **第 48 章 平台工程实战：Golden Path、多租户与自服务 API**：从租户模型/默认护栏到 Golden Path 模板、`Application` CRD/控制器、多租户负向验收、平台 SLO、成本治理、90 天落地计划与反模式，把前 47 章的机制串成可运营的平台。
- 新增 **附录 R Kubernetes 1.32–1.37 版本演进与特性采用手册**：支持窗口（2026-11：1.35/1.36/1.37，1.34 已 EOL）、逐版本特性总表、sidecar GA、原地扩缩容 GA、DRA GA 与设备污点、Pod 证书、gang scheduling、MutatingAdmissionPolicy、User Namespaces、cgroup v1 移除、nftables/IPVS、SELinuxMount、HPA 容差/scale-to-zero，以及升级验收矩阵、MiniK8s 扩展设计和迁移事故。
- 统一计数与版本口径：测试数 217，代码规模约 9900 行；附录 I 的支持窗口与版本台账更新到 2026-11，明确 1.34 于 2026-10-27 EOL。`check_docs.py` 新增“章节/附录测试数必须与 `tests/` 实际函数数一致”的校验，防止再次漂移。
- 本轮事实性修正（按章节）：Ingress NGINX 退役与迁移（第 3、14 章）、kube-proxy 端口/LIST 一致性（第 2 章）、kubectl replace 与 SSA 删除语义（第 4、5 章）、containerd 2.x 配置与 swap/原地扩缩容（第 7 章）、StatefulSet `maxUnavailable` 门控与 CSI `CSINode` 字段（第 9、12 章）、IPVS 弃用/`PreferSameZone`/`topologyKeys`（第 13 章）、DRA `resource.k8s.io/v1`（第 21、22 章）、RBAC `resourceNames`/审计重复/结构化认证（第 23 章）、SLSA v1.0/MutatingAdmissionPolicy/keyless 签名（第 24 章）、AppArmor 弃用/User Namespaces/SELinuxMount（第 25 章）、etcdutl 恢复（第 28 章）、kubelet 状态写入模型/etcd 分片（第 30 章）、CAPI v1beta2/vCluster 模式（第 33 章）、scheduler 打分与控制器状态字段（第 36 章）、GC/exec env/QoS（第 37 章）、watch selector 合成/分页语义/存储接口（第 35、40 章）。
- 新增 **附录 Q 扩展点与交付物开发实战**：client-go typed/dynamic client、分页/watch/bookmark、informer + workqueue、controller-runtime Reconcile/finalizer/status、envtest、validating webhook、kubectl 插件与 Helm chart 的完整工程骨架。
- 本轮事实修正与加固：
  - 修正 requests/limits 默认化（limits-only 会补 request，可能成为 Guaranteed）、Gateway API `allowedRoutes` 与 `ReferenceGrant` 的分工、分页快照语义、Istio 默认允许/拒绝与 `PeerAuthentication`、StatefulSet `subdomain`、init/native sidecar 资源账本、访问模式与 Multi-Attach、Job `successPolicy` 版本、CronJob 触发保证、Kueue `borrowingLimit`、RBAC 提权组合、审计日志脱敏边界、GQA KV cache 公式、Argo CD 推/拉模型、etcd WAL 备份边界、PDB 驱逐预算、API Server/etcd 调优旋钮、Service 排障树、用户命名空间、Ingress NGINX EOL、GPU 成本 PromQL、TCO 人力量级。
  - 修复 MiniK8s：客户端不能通过 PUT/PATCH 注入 `deletionTimestamp`；limits-only 资源默认化；扩展资源整数/对称校验；PodMetrics 多容器拆分；失败 Job 写入 `completionTime` 以支持 TTL；PriorityClass 的 `preemptionPolicy` 继承；CronJob `status.active` 及时清理；`deletecollection` 按 selector 删除；`exec env` 解析 ConfigMap/Secret/fieldRef。
  - 新增 6 个回归测试（当时总计 195），覆盖上述行为。
- 新增 **附录 K etcd 深入**：Raft/quorum、MVCC、compaction/defrag、快照恢复、容量与故障处理。
- 新增 **附录 L 云原生生态地图**：运行时、CNI/CSI、网关/网格、可观测、安全、GitOps、AI、多集群的选型框架。
- 新增 **附录 M 生产事故案例库**：15 类故障的症状、证据、根因、止血与预防。
- 新增 **附录 N 成本工程手册**：从账单到 Pod 的成本模型、OpenCost/PromQL 查询、分摊规则、优化杠杆与容量核算。
- 新增 **附录 O 网络取证手册**：DNS、Service/conntrack、iptables/IPVS、overlay/MTU 的分层取证与六个实战案例。
- 新增 **附录 P 字段语义与 YAML 陷阱**：常用资源的默认值、可变性矩阵、30 条高频配置陷阱。
- 新增 **第 17 个端到端场景**：跨节点拓扑分布约束（maxSkew）。
- **本版新增（v2）**：
  - 实现 **Server-Side Apply**：`managedFields` 字段所有权、冲突 409、`force` 接管、省略字段删除、dry-run；新增 12 个测试。
  - 实现 **watch 书签与 `resourceVersion=0` 初始快照**；新增 4 个测试。
  - 修复 13 个控制器/调度器缺陷：抢占最小受害者前缀（等大 Pod 下等价于最小集合）、PDB 驱逐同步扣减预算、ReplicaSet 缩容顺序、Deployment `Recreate` 等待旧 Pod、调度器 `schedulerName`/Unschedulable 条件、QoS 计入 init 容器、客户端伪造 deletionTimestamp、ExternalName DNS、CronJob active 列表、Service 会话亲和、Endpoints notReady/EndpointSlice 拓扑提示、HPA 10% 容差与多指标取最大、DaemonSet 在 cordon 节点保留且支持 nodeAffinity。
  - 新增/扩写 20 余个深度专题：数据系统运维、Spark/Flink、LLM 推理、工作负载身份、合规映射、Prometheus 生产栈、控制面容量模型、集群级测试、CSI 内部、节点生命周期、调度高级语义、Gateway API 路由、服务网格授权、审计日志、运行时检测、API Server 请求管线、替换验收矩阵、版本演进阅读法，以及**第 47 章综合架构案例**。
- 实现加固（上一版新增 26 个回归测试，当时总计 159）：
  - API 机制：watch 410/慢消费者、清空 finalizer 自动删除、PUT 必须带 resourceVersion、标签选择器 watch 的 ADDED/DELETED 转换、客户端伪造 uid/resourceVersion 被拒绝。
  - 调度：未声明扩展资源的节点不再接收 GPU Pod、`preemptionPolicy: Never` 生效、跨节点同拓扑域的 required anti-affinity。
  - 存储：同一 PV 不能双绑、PV protection、显式 `storageClassName: ""` 不再套默认类。
  - 控制器：终止 Pod 及时摘除 Endpoint、命名 NetworkPolicy 端口、LimitRange min/max/ratio、PSA 绕过、StatefulSet 逐 Pod DNS、Indexed Job 复用失败索引、`maxUnavailable` 向下取整。
  - 运行时：startupProbe、HPA 内存指标、TokenReview audience、QoS 分类、terminal runtime 清理。
- 第 21、40、42 章补充 Kueue 生产配置、MiniK8s→kind→生产的替换路线、前沿技术采用路线图。

## 5 分钟启动

要求 Python 3.10+，安装依赖：

```bash
python -m pip install -r requirements.txt
```

启动开发集群（第一个终端）：

```bash
python -m mini_k8s --port 8080 --nodes 2
```

输出会包含 API Server 地址和开发用管理员令牌。另开一个终端：

```bash
export MINIK8S_SERVER=http://127.0.0.1:8080
export MINIK8S_TOKEN=mini-admin-token
python -m mini_k8s.cli get nodes
python -m mini_k8s.cli apply -f examples/01-hello.yaml
python -m mini_k8s.cli get pods -o wide
python -m mini_k8s.cli logs <pod-name>
```

也可以安装为命令：

```bash
python -m pip install -e .
mk8s get nodes
```

> MiniK8s 的容器、网络与存储是**确定性模拟**：不会真的拉镜像、开网络命名空间或写宿主机磁盘。它模拟的是 Kubernetes 可观测的契约（Pod 相位、条件、重启、Endpoints、PVC 绑定、watch 事件），用于学习、测试控制面逻辑和验证 YAML，而不是替代生产集群。

## 快速架构图

```text
                    ┌─────────────────────────────┐
  mk8s / curl ─────▶│  API Server                 │
                    │  authn → RBAC → admission   │
                    │  REST + watch + discovery   │
                    └──────────────┬──────────────┘
                                   │ resourceVersion / watch
                    ┌──────────────▼──────────────┐
                    │  ResourceStore (etcd 替身)   │
                    │  CAS + finalizer + history  │
                    └───┬───────────┬───────────┬─┘
                        │           │           │
              ┌─────────▼──┐  ┌─────▼─────┐ ┌───▼─────────┐
              │ Scheduler  │  │Controllers│ │ Kubelet(s)  │
              │ filter/    │  │ Deploy/RS │ │ probes/logs │
              │ score/     │  │ STS/DS/Job│ │ status/IPAM │
              │ preempt    │  │ Svc/PV/HPA│ │ simulated   │
              └────────────┘  └───────────┘ └─────────────┘
                        │           │
                  ClusterDNS   ServiceProxy / Ingress / NetworkPolicy
```

## 教程地图

### 第一部分：基础与架构

1. [为什么需要 Kubernetes](chapters/01-why-kubernetes.md)
2. [架构总览](chapters/02-architecture.md)
3. [实验环境与 MiniK8s 快速开始](chapters/03-lab-and-quickstart.md)
4. [API 对象模型](chapters/04-api-object-model.md)
5. [声明式工作流与 kubectl](chapters/05-declarative-workflow.md)

### 第二部分：工作负载与存储

6. [Pod 与生命周期](chapters/06-pod-lifecycle.md)
7. [容器运行时、资源与 QoS](chapters/07-runtime-resources-qos.md)
8. [Deployment 与 ReplicaSet](chapters/08-deployment-replicaset.md)
9. [StatefulSet](chapters/09-statefulset.md)
10. [DaemonSet、Job 与 CronJob](chapters/10-daemonset-job-cronjob.md)
11. [配置与密钥](chapters/11-configmap-secret.md)
12. [存储：Volume、PV、PVC、StorageClass、CSI](chapters/12-storage.md)

### 第三部分：网络与服务

13. [Service、Endpoints 与 DNS](chapters/13-service-endpoints-dns.md)
14. [Ingress 与 Gateway API](chapters/14-ingress-gateway-api.md)
15. [CNI、kube-proxy、eBPF 与 NetworkPolicy](chapters/15-cni-ebpf-networkpolicy.md)
16. [服务网格与零信任](chapters/16-service-mesh-zero-trust.md)

### 第四部分：调度、控制与扩展

17. [调度器](chapters/17-scheduler.md)
18. [控制器模式与 Informer/Workqueue](chapters/18-controller-pattern.md)
19. [CRD 与 Operator 工程化](chapters/19-crd-operator.md)
20. [自动伸缩](chapters/20-autoscaling.md)
21. [批处理与队列](chapters/21-batch-and-queues.md)
22. [GPU、加速器与 AI 推理平台](chapters/22-gpu-ai-inference.md)

### 第五部分：安全与治理

23. [认证、授权与 RBAC](chapters/23-authn-authz-rbac.md)
24. [准入控制与供应链安全](chapters/24-admission-supply-chain.md)
25. [运行时安全与节点加固](chapters/25-runtime-security-node-hardening.md)
26. [多租户与资源治理](chapters/26-multitenancy-governance.md)

### 第六部分：可观测性与生产运维

27. [可观测性](chapters/27-observability.md)
28. [高可用与灾难恢复](chapters/28-ha-disaster-recovery.md)
29. [升级与节点运维](chapters/29-upgrade-node-ops.md)
30. [性能与规模化](chapters/30-performance-scale.md)
31. [故障排查方法论](chapters/31-troubleshooting.md)
32. [GitOps 与渐进式交付](chapters/32-gitops-progressive-delivery.md)
33. [多集群、集群生命周期与虚拟集群](chapters/33-multicluster-cluster-api.md)

### 第七部分：MiniK8s 实现

34. [总体设计与工程结构](chapters/34-minik8s-design.md)
35. [API Server 与存储](chapters/35-apiserver-store.md)
36. [调度器与控制器实现](chapters/36-scheduler-controllers.md)
37. [Kubelet 与运行时模拟](chapters/37-kubelet-runtime.md)
38. [网络、存储与安全实现](chapters/38-network-storage-security.md)
39. [测试策略](chapters/39-testing.md)
40. [与真实 Kubernetes 的差距](chapters/40-gap-and-replacement.md)

### 第八部分：前沿与演进

41. [通用计算平台：虚拟化、Serverless、Wasm 与边缘](chapters/41-general-compute-platform.md)
42. [云原生未来方向](chapters/42-future.md)

### 第九部分：商业分析

43. [商业分析 I：市场格局与价值链](chapters/43-business-market.md)
44. [商业分析 II：托管 Kubernetes 的经济学](chapters/44-business-economics.md)
45. [商业分析 III：平台工程、开源与创业](chapters/45-business-platform.md)
46. [商业分析 IV：企业 TCO、选型与落地路线图](chapters/46-business-tco-selection.md)

### 第十部分：综合架构

47. [综合架构案例：六种平台的取舍](chapters/47-architecture-casebook.md)

### 第十一部分：平台工程实战

48. [平台工程实战：Golden Path、多租户与自服务 API](chapters/48-platform-engineering-in-practice.md)

### 第十二部分：应用与事件驱动工程（跨领域）

> 这两章从“使用平台的人”和“异步系统”两个视角补齐前 48 章未展开的工程契约，可在读完第 6～12 章后随时插入阅读。

49. [应用工程：把代码写成能在 Kubernetes 上运行的系统](chapters/49-application-engineering.md)
50. [事件驱动、工作流与数据管道](chapters/50-event-driven-workflows.md)

### 附录

- [A. API 资源速查](appendices/A-api-resources.md)
- [B. kubectl 速查](appendices/B-kubectl.md)
- [C. 故障排查清单](appendices/C-troubleshooting-checklist.md)
- [D. 术语表](appendices/D-glossary.md)
- [E. 延伸阅读](appendices/E-reading.md)
- [F. 集群搭建与生产化手册](appendices/F-cluster-build.md)
- [G. Kubernetes 上游源码导读](appendices/G-source-reading.md)
- [H. 安全加固基线](appendices/H-security-baseline.md)
- [I. 版本、特性门控与弃用路线](appendices/I-version-and-deprecation.md)
- [J. 自测题库与实验手册](appendices/J-self-assessment-labs.md)
- [K. etcd 深入：一致性、备份、恢复与调优](appendices/K-etcd-operations.md)
- [L. 云原生生态地图与选型方法](appendices/L-ecosystem-map.md)
- [M. 生产事故案例库](appendices/M-incident-casebook.md)
- [N. 成本工程：度量、分摊与优化](appendices/N-cost-engineering.md)
- [O. 网络取证手册](appendices/O-network-forensics.md)
- [P. 字段语义、默认值与 YAML 陷阱](appendices/P-defaults-and-yaml-traps.md)
- [Q. 扩展点与交付物开发实战](appendices/Q-extension-development.md)
- [R. Kubernetes 1.32–1.37 版本演进与特性采用手册](appendices/R-version-evolution-1.32-1.37.md)
- [S. PKI、证书与身份生命周期](appendices/S-pki-and-certificates.md)
- [T. Windows 容器与混合操作系统集群](appendices/T-windows-and-mixed-os.md)

## 推荐学习路径

不同目标不必从头读到尾：

- **平台/后端工程师（系统掌握）**：1–5 → 6–12 → 13–16 → 17–22 → 23–26 → 27–33 → 48 → 49。
- **想读实现/面试**：1–5 → 17–19 → 34–40 → 附录 Q（真实 client-go/controller-runtime），再回看 13–16 与 23–26，最后用附录 J 自测。
- **SRE / 运维**：6–7 → 12–15 → 27–31 → 附录 K（etcd）→ 附录 S（证书）→ 附录 M（事故案例）→ 附录 O（网络取证）→ 附录 N（成本）。
- **应用开发者**：1–5 → 6–11 → 27 → 32 → 49 → 附录 B/P；先把探针、资源、优雅终止和可观测性做对，再谈平台自服务。
- **数据/事件平台**：9 → 12 → 20–21 → 30 → 50 → 附录 K/N；先理解有状态存储和队列，再设计 Kafka/工作流/CDC。
- **架构师/技术负责人**：1–2 → 22–24 → 27–33 → 41–46 → 47 → 48，选型时配合附录 L、N、P、T；应用与数据平台方案再读 49–50。
- **AI 基础设施**：17、20–22 → 30 → 32–33 → 41 → 44 → 附录 N（GPU 成本）。
- **只想快速跑起来**：3 → 5 → 34，然后按需查附录 A/B/P；做完实验看附录 J。

## 目录结构

```text
kubernetes-tutorial/
├── README.md
├── chapters/                  # 50 章教程
├── appendices/                # 速查、搭建、源码导读、安全基线、版本弃用/演进、自测、etcd、生态、案例、扩展开发
├── mini_k8s/
│   ├── api/                   # API Server、资源注册表
│   ├── store/                 # 带 resourceVersion/watch 的存储
│   ├── admission/             # 默认值、校验、Quota、PSA
│   ├── auth/                  # token 认证、RBAC
│   ├── scheduler/             # filter/score/preemption/extended resources
│   ├── controllers/           # 工作负载、网络、存储、治理
│   ├── kubelet/               # 模拟运行时与探针
│   ├── network/               # DNS、Ingress、NetworkPolicy、Proxy
│   ├── coordination/          # Lease 选举
│   ├── client.py              # kubectl 客户端库
│   └── cli/                   # mk8s 命令
├── examples/                  # 18 个可执行场景 + Operator
├── tests/                     # 217 个测试
├── check_docs.py
└── scripts/verify_all.py
```

## 版本与边界

- API 语义基线：Kubernetes **v1.31** 的稳定接口（`apps/v1`、`batch/v1`、`networking.k8s.io/v1`、`rbac.authorization.k8s.io/v1` 等）。真实集群版本更高时，本教程中的稳定 API 仍然适用。
- **重要**：v1.31 已于 2025-10-28 结束社区维护。示例冻结在 v1.31 是为了可复现，**不要把 v1.31 用于新建生产集群**。新集群应使用当前受支持的 minor，并按 minor 逐个升级；安装前核对 CNI/CSI/运行时对该版本的兼容矩阵（见附录 F、I）。
- 真实集群的安装、证书、升级和加固分别见附录 F、G、H、I；正文以“机制解释 + MiniK8s 对照 + 生产取舍”为主线。
- MiniK8s 支持：Namespace、Node、Pod、Service、Endpoints/EndpointSlice、ConfigMap、Secret、Deployment、ReplicaSet、StatefulSet、DaemonSet、Job、CronJob、PV/PVC/StorageClass、Ingress、Gateway API（GatewayClass/Gateway/HTTPRoute/ReferenceGrant）、NetworkPolicy、ServiceAccount、Role/Binding、HPA、PDB、ResourceQuota、LimitRange、PriorityClass、CRD、Lease、Event、Metrics，扩展资源（如 `nvidia.com/gpu`）的调度账本，以及 Server-Side Apply/managedFields、watch 书签和 `resourceVersion=0` 初始快照。
- MiniK8s **不**支持：真实容器隔离、真实 CNI/CSI、etcd 持久化、API 聚合层、真实 webhook 调用、完整 OpenAPI、审计日志、Windows 容器、多控制面 Raft、Device Plugin/DRA、GPU 实际执行、真实 TCP/TLS 入口与 GRPCRoute/TLSRoute/TCPRoute。对应章节会明确说明差距和替换路径。
- API 基线是 v1.31，但新章节会标注 DRA、Gateway API、KubeVirt/Knative 等仍在快速演进的接口；采用前以目标集群版本文档为准。
- 截至 2026-11，社区支持的 minor 为 **1.35 / 1.36 / 1.37**；1.34 已于 2026-10-27 EOL。1.32–1.37 的特性、升级验收和 MiniK8s 扩展设计见**附录 R**。

## 读者对象

- 已经会 `kubectl apply`，但想理解控制器、调度、API 机制的后端/平台工程师。
- 准备面试或做架构评审，需要把零散知识连成系统的人。
- 想读 Kubernetes 源码，但被仓库规模劝退的人：先读这个可运行的缩小版。
- 需要向团队解释 Kubernetes 成本、边界和商业价值的技术负责人。

## 许可

仅供学习与研究。示例中的镜像名、域名和令牌均为演示值，不应直接用于生产。
