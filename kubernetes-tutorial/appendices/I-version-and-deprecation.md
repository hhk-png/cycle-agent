# 附录 I Kubernetes 版本、特性门控与弃用路线

> Kubernetes 每几个月就发布一个新版本。生产事故中有一类很常见：**集群升级后，某个用了多年的 API 被移除，GitOps 同步失败，应用起不来**。本附录给出一套版本管理与弃用排查方法；具体版本号以目标集群的 release notes 为准。

## I.1 版本节奏与支持窗口

Kubernetes 大致每年发布 3 个 minor 版本（约每 15 周一个）。社区通常维护**最近 3 个 minor** 的安全与 bug 修复：

```text
当前最新：1.N
支持：    1.N, 1.N-1, 1.N-2
不再支持：1.N-3 及更早
```

这意味着：

- 一个集群如果落后 3 个版本以上，升级路径会变长，跳版风险变高。
- 托管服务的支持窗口可能不同，以其文档为准。
- 升级不是“一年一次大动作”，而是持续的小步快跑。

以 2026-11 为时间点的实际情况：

| minor | 发布 | 状态 | 说明 |
|---|---|---|---|
| 1.35 | 2025-12 | 受支持 | 原地扩缩容 GA、cgroup v1 移除、IPVS 弃用 |
| 1.36 | 2026-04 | 受支持 | User Namespaces GA、MutatingAdmissionPolicy GA |
| 1.37 | 2026-08 | 受支持（最新） | Pod 证书 GA、DRA 设备污点 GA、SELinuxMount 默认 |
| 1.34 | 2025-08 | 2026-10-27 EOL | 不再接收安全补丁 |
| 1.33 及更早 | — | 已 EOL | 不应继续用于生产 |

版本号语义：

- `MAJOR.MINOR.PATCH`，例如 `1.37.2`。
- `MINOR` 带新特性/弃用；`PATCH` 只修 bug/安全。
- 不要跨多个 minor 直接跳；按顺序升级，必要时使用中间版本。
- 1.32–1.37 的具体特性、API 阶段与验收方法见[附录 R](R-version-evolution-1.32-1.37.md)。

## I.2 版本偏斜（version skew）

Kubernetes 允许组件之间存在有限版本差：

| 组件 | 允许偏斜（相对 kube-apiserver） |
|---|---|
| kubelet | 最多落后 3 个 minor（较新版本策略；旧版本可能更严格） |
| kube-proxy | 与 kubelet 类似，随版本策略变化 |
| kube-controller-manager | 最多落后 1 个 minor |
| kube-scheduler | 最多落后 1 个 minor |
| kubectl | 前后 1 个 minor |

实践建议：

- 先升级控制面，再升级 kubelet/kube-proxy，最后升级附加组件。
- 不要把“支持偏斜”当成“可以长期不升级”；偏斜是升级窗口，不是稳定状态。
- 升级前用 `kubectl version`、`kubeadm upgrade plan` 检查实际版本。
- 托管服务的控制面可能已升级，节点池仍停留在旧版本；要分别管理。

## I.3 特性门控（Feature Gates）

特性门控控制某个功能的启用状态：

```text
--feature-gates=DynamicResourceAllocation=true,SomeAlphaFeature=false
```

阶段：

- **Alpha**：默认关闭，API 可能变化，不建议生产。
- **Beta**：默认开启（多数），API 趋于稳定，但仍可能调整。
- **GA/Stable**：默认开启，通常不能再关闭；门控最终会被移除。

注意：

- 门控是**组件级**的：apiserver、kubelet、scheduler、controller-manager 可能各有自己的门控。
- 开启 alpha 功能前要确认所有组件版本一致、回滚路径可行。
- 门控被移除后，旧配置里的 `--feature-gates` 可能导致组件启动失败。
- 托管集群通常不允许用户改控制面门控。

## I.4 API 弃用策略

Kubernetes 的 API 版本：

- `v1alpha*`：实验性，随时可能删除/改变。
- `v1beta*`：相对稳定，但仍有弃用周期。
- `v1`：稳定，兼容性承诺最强。

弃用规则（简化）：

- Beta API 被弃用后，通常至少支持 **9 个月或 3 个 release**（以较长者为准）。
- GA API 不会轻易删除；删除会走正式弃用流程。
- 弃用会通过 API warning、release notes、文档标注。

发现弃用 API：

```bash
# 查看 API Server 的弃用指标
kubectl get --raw /metrics | grep apiserver_requested_deprecated_apis

# 查看对象使用的 apiVersion
kubectl get ingress -A -o jsonpath='{range .items[*]}{.apiVersion}{"\n"}{end}'

# 静态扫描清单/Helm chart
pluto detect-files -d .
kubent
```

`kubectl convert` 可以把旧清单转换到新 API（需要安装插件）；转换后仍要人工检查字段语义。

## I.5 关键 API 迁移表

以下是近年最常见的移除项（版本为社区移除版本，托管服务可能略有差异）：

| 旧 API | 新 API | 移除版本 | 备注 |
|---|---|---|---|
| `extensions/v1beta1` Ingress | `networking.k8s.io/v1` | 1.22 | `pathType` 必填，后端结构变化 |
| `extensions/v1beta1` Deployment/DaemonSet/ReplicaSet | `apps/v1` | 1.16 | selector 必填且不可变 |
| `apiextensions.k8s.io/v1beta1` CRD | `apiextensions.k8s.io/v1` | 1.22 | 需要 structural schema |
| `admissionregistration.k8s.io/v1beta1` webhook | `.../v1` | 1.22 | `sideEffects`、`admissionReviewVersions` 必填 |
| `rbac.authorization.k8s.io/v1beta1` | `.../v1` | 1.22 | 字段基本兼容 |
| `storage.k8s.io/v1beta1` StorageClass/CSIDriver/CSINode/VolumeAttachment | `.../v1` | 1.22 | 字段有调整 |
| `batch/v1beta1` CronJob | `batch/v1` | 1.25 | 字段基本兼容 |
| `discovery.k8s.io/v1beta1` EndpointSlice | `.../v1` | 1.25 | 推荐使用 v1 |
| `policy/v1beta1` PodDisruptionBudget | `policy/v1` | 1.25 | 空 selector 语义变化 |
| `policy/v1beta1` PodSecurityPolicy | 无（改用 PSA） | 1.25 | PSP 彻底移除 |
| `autoscaling/v2beta1` HPA | `autoscaling/v2` | 1.25 | metrics 结构变化 |
| `autoscaling/v2beta2` HPA | `autoscaling/v2` | 1.26 | 字段基本兼容 |
| `flowcontrol.apiserver.k8s.io/v1beta1/v1beta2` | `.../v1beta3` 或 `v1` | 1.29+ | APF 配置需迁移 |

迁移检查：

1. 用 `pluto`/`kubent` 扫描 Git 仓库、Helm chart、Operator 清单。
2. 在预发集群开启 API 弃用告警，观察至少一个发布周期。
3. 升级前用 `kubectl apply --dry-run=server` 验证新 API。
4. 检查 CRD 的 `served` 版本与 conversion webhook。
5. 检查 Helm chart 依赖的 `apiVersion`，升级 chart 而不是手改渲染结果。

## I.6 近期特性与采用建议

以下特性在 1.32–1.37 窗口内成熟，采用前确认集群版本与托管服务支持。逐项拆解、API 阶段和升级注意见[附录 R](R-version-evolution-1.32-1.37.md)：

- **原生 sidecar 容器**（1.33 GA）：`initContainers` 中 `restartPolicy: Always`；关注启动/终止顺序、探针、Job 语义与资源账本。
- **Pod 原地扩缩容**（1.33 beta，1.35 GA）：调整 CPU/内存 requests/limits 而不重建 Pod；依赖 cgroup v2、运行时支持和 `resizePolicy`。
- **DRA**（1.34 GA，`resource.k8s.io/v1`）：GPU/加速器的结构化分配；关注 DeviceClass/ResourceSlice/ResourceClaim、设备污点、可分区设备和驱动兼容矩阵。
- **Pod 证书**（1.35 beta，1.37 GA）：kubelet 签发短期 X.509 证书，用于工作负载身份；核心不内置签名者。
- **Gang scheduling**（1.35 alpha，1.37 beta）：`Workload`/`PodGroup` 整组调度；与 Kueue/Volcano 的队列能力配合。
- **User Namespaces**（1.36 GA）：rootless 容器与更强隔离；验证内核、运行时、卷和 PSA 兼容性。
- **MutatingAdmissionPolicy + CEL**（1.36 GA）：把纯字段变更的 webhook 迁到 API Server；外部调用类逻辑仍需 webhook。
- **Gateway API**：Ingress 的继任者，角色分离、类型化路由；控制器实现差异较大，BackendTLSPolicy 等能力在较新版本进入 Standard。
- **APF（API Priority and Fairness）**：保护 API Server 不被单租户打爆；大规模集群应配置。
- **kube-proxy nftables / IPVS 弃用**：1.35 起 IPVS 弃用，nftables 是继任方向；升级前做 Service/DNS/NetworkPolicy 回归。
- **cgroup v2**：1.35 起 kubelet 不再支持 cgroup v1；这是节点升级的硬前提。
- **JobSet/Job 索引**：批量/AI 作业的编排能力增强。

采用原则：

- 生产只用 GA 或成熟 beta，并有回滚方案。
- 先在非关键集群验证，再逐步推广。
- 记录使用的特性门控/API 版本，升级时逐项复核。

## I.7 升级计划清单

- [ ] 确认当前版本、目标版本、支持窗口和偏斜策略。
- [ ] 扫描弃用 API（清单、CRD、Helm、Operator、监控/安全 agent）。
- [ ] 检查 CRD conversion、webhook 证书、聚合 API。
- [ ] 备份 etcd、关键 PV、GitOps 仓库。
- [ ] 在预发环境完整升级一遍，记录耗时与问题。
- [ ] 升级控制面 → 节点池 → 附加组件，分批进行。
- [ ] 升级后验证：节点 Ready、核心工作负载、Ingress/DNS、存储、策略、监控。
- [ ] 保留回滚路径（etcd 快照、节点镜像、旧版本二进制）。
- [ ] 更新文档/runbook，记录新版本的行为变化。

升级的详细操作见第 29 章，规模化影响见第 30 章，MiniK8s 的差距见第 40 章。

## I.8 MiniK8s 的版本策略

MiniK8s 以 Kubernetes v1.31 的稳定 API 为语义基线：

- 不实现 feature gates；所有模拟行为默认开启。
- 不维护多版本 API/conversion；CRD 只做简化注册。
- 不模拟弃用警告/升级偏斜。

它的价值是理解“稳定 API 的契约”，而不是验证版本迁移。真实升级必须在真实集群/托管服务上做。

## I.9 持续跟踪：把版本治理变成节奏

版本风险不是靠“升级那天小心”解决的，而是靠固定节奏：

| 节奏 | 动作 | 产出 |
|---|---|---|
| 每月 | 看目标版本 release notes、弃用公告、安全公告 | 风险条目 |
| 每季度 | 扫描仓库/集群弃用 API；检查证书与偏斜 | 升级候选清单 |
| 每半年 | 预发完整升级演练；更新 runbook | 演练报告 |
| 每年 | 评估支持窗口、发行版/托管服务续约 | 版本路线图 |

维护一张版本台账。目标版本应为“当前受支持的 minor 中的 N-2 或 N-1”，不要一次跨多个 minor；每个组件单独写支持截止时间：

```text
组件            当前      目标      EOL/支持截止        负责人   风险/阻塞
kube-apiserver  1.35.x    1.37.x    —（受支持）          ...
kubelet         1.35.x    1.37.x    —（受支持）          ...
Ingress NGINX   1.x       迁移      2026-03(停止维护)    ...
cert-manager    1.x       1.y       ...
CNI             1.x       1.y       ...
CSI driver      1.x       1.y       ...
```

注意：

- 截至 2026-11，受支持 minor 是 1.35/1.36/1.37；1.34 已于 2026-10-27 EOL。台账里的“当前”如果还停在 1.34 或更早，应优先升级，而不是继续加功能。
- Ingress NGINX 已于 2026-03 停止维护；不是“以后再看”，而是迁移项（优先评估 Gateway API）。
- 目标版本不要一次跨多个 minor；kubeadm 不支持跳版升级。
- EOL 日期以官方 release 页面和项目公告为准；本表是示例，不是承诺。

跟踪来源：

- Kubernetes Release Notes 与 Deprecation Guide（每个 minor 的必读项）。
- KEP（kubernetes/enhancements）：看 stage、graduation criteria、是否默认开启。
- 托管服务自己的版本日历与不支持项（往往比上游更严格）。
- 你的 operator/CRD/Helm chart 的兼容矩阵。

两个容易忽略的点：

- **客户端也要升级**：kubectl、CI 镜像、Operator SDK、client-go 都有偏斜限制。
- **附加组件不是“装完就不管”**：CNI、CSI、Ingress、cert-manager、策略引擎都有独立的支持窗口。

## 延伸阅读

- [第 29 章 升级与节点运维](../chapters/29-upgrade-node-ops.md)
- [第 30 章 性能与规模化](../chapters/30-performance-scale.md)
- [第 40 章 与真实 Kubernetes 的差距](../chapters/40-gap-and-replacement.md)
- [第 42 章 云原生未来方向](../chapters/42-future.md)
- [附录 R Kubernetes 1.32–1.37 版本演进与特性采用手册](R-version-evolution-1.32-1.37.md)
- [附录 A API 资源速查](A-api-resources.md)
- [附录 K etcd 深入：升级前的快照、恢复与容量检查](K-etcd-operations.md)
