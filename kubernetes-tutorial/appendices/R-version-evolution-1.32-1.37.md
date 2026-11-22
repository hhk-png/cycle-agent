# 附录 R Kubernetes 1.32–1.37 版本演进与特性采用手册

> 本书正文以 Kubernetes **v1.31 的稳定 API** 为语义基线，目的是让示例在较新集群上仍然成立，而不是把教程冻结在 1.31。现实是：v1.31 已于 2025-10-28 结束社区维护，v1.34 于 2026-10-27 结束支持；截至 2026-11，受支持的 minor 是 **1.35、1.36、1.37**。本附录补齐这段版本窗口，重点不是背特性名单，而是回答三个问题：**哪些变化会影响升级？哪些能力值得现在采用？采用前要验证什么？**

版本号、发布日期和支持窗口以 [Kubernetes Releases](https://kubernetes.io/releases/) 与各版本 release notes 为准；托管服务可能推迟、禁用或回移某些特性，采用前必须查目标集群的支持矩阵。

## R.1 这六个版本改变了什么

把 1.32–1.37 的变化按“对生产的影响”归类，比逐条读 release notes 更有效：

| 主题 | 代表变化 | 影响 |
|---|---|---|
| AI/加速器 | DRA 从 beta 到 GA；设备污点、可分区设备、优先级备选；gang scheduling 进入 alpha/beta | GPU/NPU 的分配从“整数扩展资源”转向“属性 + 声明 + 拓扑” |
| 工作负载生命周期 | 原生 sidecar GA；原地扩缩容从 alpha 到 GA；Pod 级资源调整 beta | 不再只能“改模板 → 重建 Pod” |
| 身份与隔离 | Pod 证书 beta/GA；User Namespaces GA；MutatingAdmissionPolicy GA | 工作负载身份、rootless、CEL 变更准入进入主流 |
| 数据面 | kube-proxy nftables 推进、IPVS 弃用；cgroup v1 移除；SELinuxMount 默认 | 节点镜像、CNI、存储挂载和升级验收都要改 |
| 可观测与运维 | metrics.k8s.io/v1 稳定；节点声明特性；HPA 容差与 scale-to-zero | 指标 API、调度安全和伸缩行为更可控 |

这五条线里，**数据面和准入/身份的变化最容易在升级时“静默”伤到生产**；DRA 和 gang scheduling 则是新能力，不用不会坏，用了要按新模型验收。

## R.2 支持窗口与升级路径（2026-11）

| minor | 发布 | 当前状态 | 备注 |
|---|---|---|---|
| 1.32 | 2024-12 | 已 EOL | DRA v1beta2、Memory Manager GA |
| 1.33 | 2025-04 | 已 EOL | sidecar GA、原地扩缩容 beta、Job successPolicy GA |
| 1.34 | 2025-08 | 2026-10-27 EOL | DRA GA、User Namespaces beta |
| 1.35 | 2025-12 | 受支持 | 原地扩缩容 GA、Pod 证书 beta、cgroup v1 移除 |
| 1.36 | 2026-04 | 受支持 | User Namespaces GA、MutatingAdmissionPolicy GA |
| 1.37 | 2026-08 | 受支持 | Pod 证书 GA、DRA 设备污点 GA、SELinuxMount 默认 |

升级路径的硬约束：

1. **不能跳 minor**。kubeadm 不支持从 1.33 直接升到 1.37；必须 1.33 → 1.34 → 1.35 → 1.36 → 1.37 逐个走。
2. **先控制面，再 kubelet/kube-proxy，最后附加组件**。kubelet 可以落后 apiserver 若干 minor，但这是升级窗口，不是长期状态。
3. **每升一个 minor 都做一次完整验收**。尤其是 CNI、CSI、Ingress/Gateway 控制器、策略引擎、监控 agent、DRA 驱动；它们往往比 Kubernetes 本身更晚支持新版本。
4. **托管服务的“支持”不等于“所有特性可用”**。控制面版本可能已到 1.37，但某些 feature gate、DRA 驱动或节点池仍停留在旧版本。

## R.3 一页总表：每个版本该记住什么

| minor | 稳定/GA | Beta/Alpha（关注） | 升级注意 |
|---|---|---|---|
| 1.32 | Memory Manager、Structured Authorization Configuration、CRD 字段选择器 | DRA v1beta2、原地扩缩容 alpha | 检查授权配置迁移；DRA 仍需显式开启 |
| 1.33 | 原生 sidecar、Job `successPolicy`、MultiCIDRServiceAllocator | 原地扩缩容 beta、DRA v1beta2 | sidecar 打破“init 容器只运行一次”的旧假设；Job 行为变化 |
| 1.34 | **DRA GA（`resource.k8s.io/v1`）**、CPU Manager Policy Options | User Namespaces beta、MutatingAdmissionPolicy beta | DRA 驱动必须支持 v1；旧 v1beta1/v1beta2 客户端要适配 |
| 1.35 | **原地扩缩容 GA**、`PreferSameZone`、DRA 默认启用 | gang scheduling alpha、Pod 证书 beta | **cgroup v1 被移除**；IPVS 模式弃用；节点必须 cgroup v2 |
| 1.36 | **User Namespaces GA**、**MutatingAdmissionPolicy GA**、kubelet API 细粒度授权 GA | Pod 级原地扩缩容 beta、CRI List Streaming beta、HPA 外部指标回退 beta | SELinux 卷标签行为变化；Pod 级 resize 需要多个门控 |
| 1.37 | **Pod 证书 GA**、**DRA 设备污点 GA**、SELinuxMount 默认、metrics API v1、节点声明特性 GA、HPA 容差 GA | gang scheduling beta、HPA scale-to-zero beta | nftables 迁移；SELinuxMount 可能让共享卷的 Pod 起不来 |

> 表中“GA”指该能力到达稳定阶段，不代表所有托管服务都已开放。alpha 特性不要用于生产；beta 特性要有回滚方案。

## R.4 重点特性拆解

### R.4.1 原生 sidecar：从“多容器技巧”到一等公民

sidecar 在 1.33 达到 GA。它的写法仍然是 init 容器，但 `restartPolicy: Always` 让它贯穿 Pod 整个生命周期：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: app-with-sidecar
spec:
  initContainers:
    - name: log-shipper
      image: example/log-shipper:1.0
      restartPolicy: Always
      volumeMounts:
        - name: logs
          mountPath: /var/log/app
  containers:
    - name: app
      image: example/app:1.0
      volumeMounts:
        - name: logs
          mountPath: /var/log/app
  volumes:
    - name: logs
      emptyDir: {}
```

语义要点：

- **启动顺序**：普通 init 容器先完成，sidecar 随后启动；主容器在 sidecar 就绪后启动。sidecar 可以阻塞主容器启动（例如等待代理可用）。
- **探针**：sidecar 支持 readiness/liveness/startup 探针；readiness 会影响 Pod 就绪。
- **终止顺序**：Pod 终止时，主容器先收到 TERM，sidecar 在之后终止，便于把日志/连接排空。具体顺序以目标版本文档为准。
- **Job 中的 sidecar**：sidecar 不会阻止 Job 完成；Job 控制器会忽略 sidecar 容器的退出。这让“日志/代理 sidecar + 一次性任务”终于有官方语义。
- **资源账本**：sidecar 的资源仍计入 Pod 总量；调度器按所有容器（含 sidecar）的 requests 之和计算。init 容器与 sidecar 的资源取“最大值/求和”的规则要看版本，升级时用 `kubectl describe node` 核对实际账本。

迁移建议：把“普通容器 + 启动脚本 sleep”改成原生 sidecar；不要再依赖“主容器里再起一个进程”的写法。升级到 1.33+ 后，检查自定义控制器是否把 init 容器当成“只运行一次”。

### R.4.2 原地扩缩容：不重建 Pod 改资源

时间线：1.31/1.32 alpha，1.33 beta 默认开启，1.35 GA。它通过 Pod 的 `resize` 子资源修改运行中容器的 CPU/内存 requests/limits，不重建 Pod。

关键概念：

- **`resizePolicy[]`**：容器的 resize 策略列表，每一项由 `resourceName`（`cpu`/`memory`）和 `restartPolicy` 组成（注意这是 resize 策略，不是 Pod 的 restartPolicy）：`NotRequired` 表示尽量原地调整，`RestartContainer` 表示该资源变化需要重启容器。
- **状态条件**：1.34 起引入 `PodResizePending` 与 `PodResizeInProgress`，用于表达“排队中/进行中/失败”。排障时先看 conditions，不要只看 Pod phase。
- **运行时要求**：依赖 cgroup v2 和容器运行时支持；1.35 起 cgroup v1 已被移除，这个前提变成硬性要求。
- **限制**：不是所有字段都能原地改；缩容内存上限可能触发 OOM 竞争，某些版本对“降低内存 limit”有额外限制。以目标版本为准。
- **与 HPA/VPA 的关系**：HPA 改副本数，VPA 改资源；原地扩缩容让 VPA 不必重建 Pod，但也意味着“资源变更”不再是天然的重启点，健康检查/连接池要能处理运行中限额变化。

运维影响：

1. 监控要区分“模板变更”和“原地 resize”；审计日志里会出现 `pods/resize` 子资源操作。
2. 资源配额/优先级在 resize 时可能重新计算；先在小范围验证。
3. 不要假设 resize 一定成功；控制器要处理 `PodResizePending` 长期不消失的情况。

### R.4.3 DRA：设备成为一等 API 对象

DRA 在 1.34 达到 GA，API 为 `resource.k8s.io/v1`。它把“设备”从 `nvidia.com/gpu: 1` 这种整数扩展资源，升级为可描述属性、可选择、可共享、可拓扑感知的对象。

对象模型（概念性）：

```text
DeviceClass        设备类别与驱动（例如 gpu.example.com）
ResourceSlice      驱动上报的设备清单与属性（由 DRA driver 创建）
ResourceClaim      一次具体的设备请求（Pod 引用它）
ResourceClaimTemplate  为每个 Pod 生成 Claim 的模板
```

典型流程：

1. 管理员安装 DRA driver，创建 `DeviceClass`。
2. 工作负载创建 `ResourceClaim`（或 `ResourceClaimTemplate`），声明“需要 1 张显存 ≥ 40GiB、同 NUMA 节点的 GPU”。
3. 调度器读取 `ResourceSlice`，在调度阶段完成设备分配，并把分配结果写入 Claim 的 status。
4. kubelet 与 driver 协作，把设备注入容器（通常通过 CDI）。

1.35–1.37 的能力增量：

- **设备污点/容忍（1.37 GA）**：单张 GPU 故障可以打 `NoSchedule`/`NoExecute`，避免继续调度；配套 `DeviceTaintRule` 和驱逐控制器。
- **可分区设备与可消费容量（beta）**：一张物理卡可以切成多个逻辑设备，按容量/份额分配。
- **优先级备选（1.36）**：一个请求可以列出多个可接受设备，调度器按优先级选择。
- **组请求共享（1.37 beta）**：多个 Pod 共享同一组设备，适合 gang/推理场景。
- **管理访问**：驱动/管理员可以声明对设备的特殊访问，不再靠特权容器绕过。

与 Device Plugin 的关系：DRA 不是“立刻取代” Device Plugin。大量现有 GPU 集群仍用 Device Plugin；DRA 的优势在属性选择、拓扑、共享和污点。迁移时要确认驱动、运行时、调度器和监控都支持 DRA，并准备好“同一集群两种模型并存”的过渡期。

### R.4.4 Pod 证书与工作负载身份

时间线：1.35 beta，1.37 GA。它让 kubelet 为 Pod 签发短期 X.509 证书，并通过投影卷挂载，作为 ServiceAccount token 之外的“持有证明”身份。

对象与组件：

- **`PodCertificateRequest`**：Pod 向签名者请求证书的 API 对象。
- **`ClusterTrustBundle`**：分发信任根。
- **投影卷**：kubelet 负责签发、轮换、挂载证书。
- **签名者**：核心不内置签名者，需要第三方（SPIFFE/SPIRE、cert-manager、云 KMS 等）提供。

适用场景：

- mTLS 服务身份，不依赖长期 Secret。
- 需要证书 Subject 绑定 Pod/ServiceAccount/节点信息的场景。
- 与 SPIFFE 等工作负载身份体系对接。

注意：

- 不是“开箱即用的零信任”；没有签名者就没有证书。
- 证书生命周期短，应用要能热加载/轮换。
- 与 ServiceAccount token 的边界要设计清楚：token 适合调用 API Server，Pod 证书适合服务间 mTLS。

### R.4.5 Gang scheduling：整组调度

时间线：1.35 alpha，1.37 beta。核心是 `Workload`/`PodGroup`：一组 Pod 要么全部调度成功，要么都不调度，避免分布式训练“起了 7 个 worker，第 8 个永远 Pending”的资源死锁。

与 Kueue/Volcano 的关系：

- **Kueue** 解决“队列、配额、借用、准入”，gang 是它的一种调度约束。
- **Volcano** 提供 gang、队列、公平共享等完整批处理调度。
- **原生 gang** 把“全有或全无”下沉到调度器，但仍需要队列/配额系统配合。

采用建议：如果只是偶尔跑分布式训练，先用 Kueue/Volcano；如果平台要统一原生 API、减少第三方调度器，评估原生 gang 的成熟度（1.37 仍是 beta）。注意 gang 会放大资源碎片和死锁风险，必须配合队列和超时。

### R.4.6 MutatingAdmissionPolicy：用 CEL 做变更准入

时间线：1.32 alpha，1.34 beta，1.36 GA。它把“给对象补默认值/注入字段”的常见 webhook 逻辑，改成 API Server 内执行的 CEL 策略。

适合：

- 给 Pod 注入 labels/annotations、sidecar、securityContext。
- 按 namespace/标签做标准化变更。
- 不希望维护一个高可用 webhook 服务的场景。

不适合：

- 需要调用外部系统（镜像扫描、审批系统）的逻辑。
- 复杂的分支/循环/数据转换；CEL 不是通用编程语言。
- 需要读取集群其他对象的场景（受限于 API Server 的可用信息）。

迁移建议：先把“纯字段变更”的 webhook 迁到 CEL，保留外部调用类 webhook；用 dry-run 对比新旧策略的输出，避免默认值顺序变化。

### R.4.7 User Namespaces：更强的 rootless 隔离

1.36 GA（Linux）。Pod 可以拥有独立的 user namespace，容器内的 root 映射到宿主机的非特权 UID，降低容器逃逸后的权限。

采用前提：

- 节点内核、容器运行时（containerd/CRI-O）支持。
- 镜像和卷的 UID/GID 映射要验证；hostPath、NFS、部分 CSI 驱动可能不兼容。
- Pod Security Standards 的行为随版本变化，`restricted` 配置要重新验收。

它不是“取代沙箱运行时”：user namespace 缩小权限，但不提供独立内核；强多租户仍需要 gVisor/Kata/VM 级隔离。

### R.4.8 数据面：nftables、cgroup v2 与 SELinuxMount

**kube-proxy nftables**：1.35 起 IPVS 模式被弃用，nftables 成为 iptables/IPVS 的继任方向；官方计划在后续版本禁用 IPVS，并在更晚版本移除。升级前要：

- 确认 kube-proxy 模式与 CNI 的兼容性；Cilium 等 eBPF 数据面可能完全绕过 kube-proxy。
- 用 `kubectl get svc`、连接跟踪、跨节点/跨 AZ 流量做回归。
- 检查监控/排障脚本是否还在解析 iptables/IPVS 规则。

**cgroup v1 移除**：1.35 起 kubelet 要求 cgroup v2。节点 OS、容器运行时、JVM/数据库等对 cgroup v2 的适配要在升级前验证；这是“节点 NotReady”的常见原因。

**SELinuxMount**：1.37 默认启用，用 `mount -o context=<label>` 替代逐文件重打标签。风险是同一节点上不同 SELinux 标签的 Pod 共享卷时可能无法挂载；必要时用 `seLinuxChangePolicy: Recursive` 回退，但这会牺牲挂载性能。升级前要盘点 RWX/共享卷和多租户场景。

### R.4.9 自动伸缩与指标

- **HPA 可配置容差（1.37 GA）**：不同业务可以设置不同的抖动容忍度，减少副本震荡。
- **HPA scale-to-zero（1.37 beta）**：对象/外部指标可以驱动到 0；仍然需要工作负载控制器和冷启动设计。CPU/内存指标通常不能直接 scale-to-zero。
- **HPA 外部指标回退（1.36 beta）**：外部指标源故障时使用回退值，避免伸缩“冻结”在错误状态。
- **metrics.k8s.io/v1（1.37 稳定）**：`kubectl top`/HPA 的指标 API 版本升级；自定义指标适配器要检查兼容性。
- **与原地扩缩容的交互**：VPA 可以改资源而不重建 Pod，HPA 仍改副本数；两者同时作用时要有优先级和防抖策略，避免“边扩副本边改规格”。

### R.4.10 节点与运行时

- **CRI List Streaming（1.36 beta）**：高密度节点上降低 kubelet 一次性列出所有容器/镜像的内存峰值。
- **kubelet API 细粒度授权（1.36 GA）**：不再需要给所有需要 `nodes/proxy` 的主体过大权限；审计现有 ClusterRole。
- **节点声明特性（1.35 alpha，1.37 GA）**：节点在 `status.declaredFeatures` 中声明支持的特性，调度/准入可以据此做安全决策，减少“调度到不支持的节点”。
- **containerd 2.0**：1.33 起生态逐步支持；升级运行时要做镜像、cgroup、CNI、GPU 的兼容性回归。
- **rootless kubelet（1.37 beta）**：在 user namespace 中运行 kubelet，进一步缩小节点权限；仍属实验性，不建议直接上生产。

### R.4.11 存储与 API 机制

- **PVC last-used 跟踪（1.37 beta）**：`PersistentVolumeClaimUnusedSinceTime` 帮助识别长期未使用的 PVC，降低“删了怕坏、留着花钱”的治理成本。
- **DRA 与 CSI 的分工**：CSI 管块/文件卷的生命周期，DRA 管设备（GPU/NIC/FPGA）的分配；两者会在 AI 节点上同时出现，不要混为一谈。
- **comparable resourceVersion（1.35）**：resourceVersion 变成可比较的十进制值，客户端可以更可靠地检测丢失更新；但不要把它当业务版本号。
- **server-side sharded watch/list（1.36 alpha）**：大规模集群的分片 watch，降低单个 watcher 的负载；客户端/控制器要支持分片语义。
- **KYAML（1.37 稳定）**：kubectl 的 KYAML 输出更易读，但不要假设所有工具都支持；CI 里仍以 JSON/YAML 为准。

## R.5 升级验收矩阵

每升一个 minor，至少跑完这张表：

| 领域 | 检查 | 工具/命令 | 失败信号 |
|---|---|---|---|
| API 移除 | 清单/CRD/Helm 是否使用已移除版本 | `pluto detect-files`、`kubent`、`kubectl apply --dry-run=server` | 同步失败、404/410 |
| 特性门控 | 组件启动参数是否引用已移除门控 | 组件日志、`--feature-gates` | 组件启动失败 |
| 运行时 | 节点是否 cgroup v2；containerd/CRI-O 版本 | `stat -fc %T /sys/fs/cgroup`、`crictl info` | 节点 NotReady |
| 网络 | kube-proxy 模式、CNI 版本、跨节点/跨 AZ | `kubectl get cm -n kube-system kube-proxy -o yaml`、连通性测试 | Service 不通、DNS 超时 |
| 存储 | CSI 驱动、快照、RWX、SELinux 卷 | 挂载/快照/恢复演练 | 挂载失败、Pod 起不来 |
| 准入 | webhook 证书、CEL 策略、PSA | `kubectl apply --dry-run=server`、策略测试 | 拒绝率异常 |
| 身份 | ServiceAccount、OIDC、Pod 证书签名者 | TokenReview、mTLS 握手 | 401/403、证书错误 |
| 调度 | DRA driver、gang、拓扑、节点声明特性 | 调度失败事件、`kubectl describe pod` | 长期 Pending |
| 伸缩 | HPA 指标、容差、scale-to-zero | 压测、指标源故障注入 | 副本震荡/冻结 |
| 可观测 | metrics API、Prometheus 抓取、日志 | `kubectl top`、Grafana | 指标缺失、告警失效 |

验收原则：**先验证“集群还能提供原有服务”，再验证“新特性可用”**。升级事故大多不是新特性引起的，而是旧行为被默认值/移除项改变。

## R.6 与本书章节的对照

| 版本主题 | 本书位置 | 阅读重点 |
|---|---|---|
| sidecar | [第 6 章 Pod 与生命周期](../chapters/06-pod-lifecycle.md) | init/sidecar 顺序、探针、Job |
| 原地扩缩容与 QoS | [第 7 章 运行时、资源与 QoS](../chapters/07-runtime-resources-qos.md)、[第 20 章 自动伸缩](../chapters/20-autoscaling.md) | resizePolicy、VPA/HPA 交互 |
| DRA 与 GPU | [第 21 章 批处理与队列](../chapters/21-batch-and-queues.md)、[第 22 章 GPU 与 AI 推理](../chapters/22-gpu-ai-inference.md) | Claim/Slice、设备污点、gang |
| Pod 证书与身份 | [第 23 章 认证、授权与 RBAC](../chapters/23-authn-authz-rbac.md)、[第 25 章 运行时安全](../chapters/25-runtime-security-node-hardening.md) | SA token、mTLS、SPIFFE |
| MutatingAdmissionPolicy | [第 24 章 准入与供应链](../chapters/24-admission-supply-chain.md) | CEL、webhook 迁移 |
| User Namespaces | [第 25 章 运行时安全](../chapters/25-runtime-security-node-hardening.md) | rootless、PSA、卷兼容 |
| nftables/IPVS/cgroup v2 | [第 15 章 CNI 与网络](../chapters/15-cni-ebpf-networkpolicy.md)、[第 29 章 升级与节点运维](../chapters/29-upgrade-node-ops.md) | 数据面、节点升级 |
| SELinuxMount | [第 12 章 存储](../chapters/12-storage.md)、[第 25 章 运行时安全](../chapters/25-runtime-security-node-hardening.md) | 共享卷、多租户 |
| HPA 容差/scale-to-zero | [第 20 章 自动伸缩](../chapters/20-autoscaling.md) | 指标、抖动、冷启动 |
| 版本治理 | [附录 I](I-version-and-deprecation.md) | 支持窗口、弃用、升级清单 |

## R.7 把新特性映射到 MiniK8s：一份实现设计

MiniK8s 的定位是“可读的契约实现”，不追逐每个新 API。但如果要把本附录的能力做成教学实验，可以按下面的优先级扩展；每一项都能在现有组件边界内实现，并配套测试。

| 能力 | 最小实现 | 改动点 | 验收测试 |
|---|---|---|---|
| 原地扩缩容 | 允许更新 Pod 容器 resources，kubelet 直接改模拟限额并写 conditions | `api/server.py` 的 update 校验、`kubelet` 状态机、admission | 改 CPU 不重建 Pod；非法缩容被拒绝；conditions 正确 |
| DRA-lite | 新增 `DeviceClass`/`ResourceClaim` 对象；调度器按 claim 分配模拟设备；kubelet 把它注入 Pod | registry、scheduler filter/score、controllers、admission | 无可用设备时 Pending；属性选择正确；释放后可复用 |
| gang scheduling | 新增 `PodGroup`；调度器先尝试整组，失败则全部回滚 | scheduler 队列/事务、controllers | 组内任一 Pod 不可调度则全组 Pending；资源足够时全组 Running |
| Pod 证书 | 模拟签发短期证书对象，kubelet 轮换并挂载 | `auth`、`kubelet`、store | 证书过期自动轮换；未授权签名者被拒绝 |
| CEL 准入 | 实现一个极简表达式子集（字段存在/相等/默认值） | admission | 变更顺序、拒绝原因、dry-run 与真实一致 |
| nftables 对照 | 不实现数据面，只记录 Service→Endpoint 规则并做一致性检查 | network/proxy | 规则与 EndpointSlice 同步 |

实现原则与正文一致：**先定义可观测契约，再写控制器；先写失败测试，再写实现；每个简化都在文档中标注**。不要为了“支持 DRA”而引入真实 GPU/驱动依赖——教学实现模拟的是 API 语义和调度决策，不是设备执行。

## R.8 实验：在真实集群上验证版本特性

以下实验以 1.35+ 的 kind/托管集群为例；具体镜像与字段以目标版本为准。

### 实验 1：确认版本与节点条件

```bash
kubectl version
kubectl get nodes -o wide
kubectl get --raw /metrics | grep apiserver_requested_deprecated_apis
```

### 实验 2：sidecar 生命周期

部署一个带原生 sidecar 的 Pod，观察：

```bash
kubectl get pod app-with-sidecar -w
kubectl logs app-with-sidecar -c log-shipper
kubectl delete pod app-with-sidecar
```

重点看：sidecar 是否在主容器之前就绪、删除时是否最后终止、Job 场景是否阻止完成。

### 实验 3：原地扩缩容

对一个运行中的 Pod 修改 resources（用 `kubectl edit`/`apply`，或对 `pods/resize` 子资源发 PATCH），观察：

```bash
kubectl get pod <pod> -o jsonpath='{.status.conditions}'
kubectl describe pod <pod> | grep -A5 Resize
```

重点看：Pod 是否重建（UID 不变）、`PodResizePending`/`PodResizeInProgress` 是否出现、缩容失败时的原因。

### 实验 4：DRA 对象是否存在

```bash
kubectl api-resources | grep resource.k8s.io
kubectl get deviceclasses
kubectl get resourceslices
```

没有 DRA driver 时，`ResourceSlice` 为空是正常的；不要据此判断“DRA 不可用”。真正验证需要安装驱动并创建 Claim。

### 实验 5：升级回归

在预发集群按 minor 逐个升级，每升一个版本跑一遍 R.5 的矩阵；记录耗时、失败项和回滚路径。不要在生产直接“跳版”。

## R.9 典型迁移事故与规避

1. **cgroup v1 节点升级后 NotReady**：节点 OS 太旧或运行时未启用 cgroup v2。升级前用 `stat -fc %T /sys/fs/cgroup` 检查，先升级 OS/运行时。
2. **SELinuxMount 默认后共享卷 Pod 起不来**：同节点不同标签的 Pod 共享 RWX 卷。盘点共享卷，必要时设置 `seLinuxChangePolicy: Recursive`，并评估性能。
3. **IPVS 弃用后规则不生效**：kube-proxy 模式切换与 CNI/eBPF 冲突。先在预发切换并做 Service/DNS/NetworkPolicy 回归。
4. **DRA 驱动版本不匹配**：客户端用 `resource.k8s.io/v1`，集群只服务 v1beta1/v1beta2。升级前确认驱动支持矩阵，或在代码里做 served-version 发现。
5. **MutatingAdmissionPolicy 与 webhook 顺序冲突**：默认值被覆盖、注入重复 sidecar。用 dry-run 对比新旧输出，明确“谁拥有哪个字段”。
6. **原地 resize 被误当成“无风险变更”**：内存 limit 降低触发 OOM、应用未热加载新限额。把 resize 纳入变更管理和压测。
7. **gang scheduling 放大资源碎片**：整组等待导致大块资源闲置。配合队列、优先级和超时，不要用它替代容量规划。

## R.10 检查清单

- [ ] 已确认当前 minor、目标 minor、支持窗口与升级路径（不跳版）。
- [ ] 已扫描并修复弃用 API、feature gate、CRD conversion、webhook 证书。
- [ ] 节点已切 cgroup v2；运行时/OS/CNI/CSI 支持目标版本。
- [ ] kube-proxy 模式（iptables/nftables/IPVS）与 CNI 兼容，已完成回归。
- [ ] SELinuxMount、共享卷、RWX、多租户场景已验收。
- [ ] DRA 驱动/Device Plugin 与 `resource.k8s.io` 版本匹配。
- [ ] HPA/VPA/原地扩缩容的交互有防抖和回滚策略。
- [ ] Pod 证书/ServiceAccount/OIDC 的身份链路已测试轮换与过期。
- [ ] 预发完成至少一次“升级 + 回滚”演练。
- [ ] 版本台账、runbook、on-call 联系人已更新。

## 延伸阅读

- [附录 I 版本、特性门控与弃用路线](I-version-and-deprecation.md)：升级治理与 API 迁移表。
- [附录 F 集群搭建与生产化手册](F-cluster-build.md)：节点、CNI/CSI、证书与升级操作。
- [附录 K etcd 深入](K-etcd-operations.md)：升级前的快照、恢复与容量检查。
- [第 29 章 升级与节点运维](../chapters/29-upgrade-node-ops.md)：kubeadm 升级序列与节点运维。
- [第 42 章 云原生未来方向](../chapters/42-future.md)：DRA、Gateway API、AI 工作负载的采用判断。
- [Kubernetes Releases](https://kubernetes.io/releases/)、[KEP 索引](https://github.com/kubernetes/enhancements)：版本与特性的权威来源。
