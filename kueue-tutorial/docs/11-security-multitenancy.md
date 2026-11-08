# 第 11 章 · 安全与多租户

## 11.1 先明确边界

Kueue 是**队列与配额控制器**，不是安全边界。它不能替代：

- Kubernetes RBAC；
- Namespace 隔离；
- NetworkPolicy；
- Pod Security Standards；
- Admission Policy/Gatekeeper/Kyverno；
- 镜像签名与供应链安全；
- 节点隔离、运行时安全。

Kueue 能做的是：

- 控制谁能创建/修改队列对象；
- 控制哪些 namespace 能使用哪个 ClusterQueue；
- 通过配额、公平和抢占限制资源竞争；
- 通过 AdmissionCheck 接入外部审批/合规检查。

安全设计应该把 Kueue 当作"资源治理层"，而不是"信任边界"。

```text
信任边界（Kubernetes/平台负责）
  ├── 身份认证：OIDC、ServiceAccount、证书
  ├── 授权：RBAC、准入策略
  ├── 网络：NetworkPolicy、mTLS
  ├── 运行时：Pod Security、RuntimeClass、沙箱
  └── 供应链：镜像签名、扫描、仓库策略

资源治理层（Kueue 负责）
  ├── 谁能用哪个 LocalQueue/ClusterQueue
  ├── 哪些 namespace 能提交
  ├── 配额、借用、公平、抢占
  └── AdmissionCheck 的外部闸门
```

Kueue 的每个安全决策都建立在"Kubernetes 已经做对了身份、命名空间和 RBAC"之上。如果这些基础不牢，Kueue 的配额只会让攻击面更复杂，而不是更安全。

## 11.2 角色划分

常见的两类角色：

### 平台管理员（batch administrator）

负责：

- 创建/维护 ResourceFlavor；
- 创建/维护 Cohort；
- 创建/维护 ClusterQueue；
- 配置 AdmissionCheck；
- 配置 Fair Sharing/Preemption；
- 监控容量与公平。

### 租户用户（batch user）

负责：

- 在自己的 namespace 创建 LocalQueue（是否允许由平台决定）；
- 提交 Job/JobSet/RayJob 等；
- 查看自己队列的 Workload/事件；
- 不直接修改 ClusterQueue/Cohort/Flavor。

### 内置角色与示意 RBAC

Kueue 安装清单自带两个 ClusterRole：`kueue-batch-admin-role` 和 `kueue-batch-user-role`。生产上优先用内置角色绑定，再按租户收紧；下面的 YAML 只是示意，不是对内置角色的替代：

```bash
kubectl get clusterrole | grep kueue-batch
```

以下只是示意，实际 resourceNames/verbs 要按你的安全模型收紧：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kueue-batch-admin
rules:
- apiGroups: ["kueue.x-k8s.io"]
  resources:
  - clusterqueues
  - cohorts
  - resourceflavors
  - admissionchecks
  - topologies
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: kueue-batch-user
  namespace: team-a
rules:
# 如果允许租户自建 LocalQueue，才给 create/update；否则删掉这两项，
# 由平台统一创建 LQ。
- apiGroups: ["kueue.x-k8s.io"]
  resources: ["localqueues"]
  verbs: ["get", "list", "watch", "create", "update", "patch"]
- apiGroups: ["kueue.x-k8s.io"]
  resources: ["workloads"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

如果租户能创建/修改 LocalQueue，必须再加一层校验：否则它可以把 `spec.clusterQueue` 指向任何被 `namespaceSelector` 放行的 ClusterQueue，绕过平台分配的队列。用 ValidatingAdmissionPolicy/策略引擎把 `spec.clusterQueue` 限制在 namespace 允许的 CQ 白名单内，或干脆只允许平台创建 LQ。

关键点：

- 不要让普通用户创建/修改 ClusterQueue、Cohort、ResourceFlavor。
- 不要让普通用户创建/修改 AdmissionCheck（它会决定谁能被准入）。
- 如果允许用户创建 LocalQueue，要限制它能指向哪些 ClusterQueue（可用准入策略/控制器校验）。
- Workload 通常由集成控制器创建，普通用户只读；不要让用户伪造 Workload 绕过 Job 集成。

### 11.2.1 只读/审计角色

排障和门户通常只需要读权限，但读的范围要限制：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: kueue-readonly
  namespace: team-a
rules:
- apiGroups: ["kueue.x-k8s.io"]
  resources: ["localqueues", "workloads"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["visibility.kueue.x-k8s.io"]
  resources: ["*"]
  verbs: ["get", "list"]
```

注意：

- `workloads` 的 `spec.podSets` 可能包含镜像、命令、环境变量；只读权限也可能泄露敏感信息。
- `kueuectl` 的 `stop/resume` 需要写权限；不要把它给普通租户。
- 审计日志要记录谁改了 ClusterQueue/Cohort/AdmissionCheck/PriorityClass。

## 11.3 namespaceSelector 与租户隔离

`ClusterQueue.spec.namespaceSelector` 决定哪些 namespace 可以使用该 CQ。

```yaml
spec:
  namespaceSelector:
    matchLabels:
      team: team-a
```

注意：

- `null` 不允许任何 namespace；
- `{}` 允许所有 namespace；
- 标签由集群管理员控制，租户不应能随意改自己的 Namespace 标签。

如果租户能修改 Namespace 标签，就能把作业"迁移"到别的 CQ 的配额里。因此：

- 限制 `namespaces` 的 patch/update 权限；
- 用准入策略保护关键标签；
- 不要把敏感 CQ 的 selector 设计成用户可控的标签。

### 11.3.1 与 ResourceQuota/LimitRange 共存

Kueue 的配额和 namespace 的 `ResourceQuota` 是**两本独立的账**：

- Kueue 在 Pod 创建前决定"这个 Workload 能不能拿到逻辑配额"；
- `ResourceQuota` 在 Pod 创建时决定"这个 namespace 还能不能创建这么多资源"；
- `LimitRange` 会改写 Pod 的默认 requests/limits，而 Kueue 的资源汇总也会受它影响。

两者不一致时，最典型的故障是：**Kueue 显示 Admitted，但 Job 的 Pod 创建不出来**。原因通常是 `ResourceQuota` 比 Kueue 的 nominal 更紧，或者配额统计的维度不同（例如 RQ 统计 `requests.cpu`，Kueue 统计 Workload 的资源组）。

| 现象 | 根因 | 处理 |
|---|---|---|
| Workload `Admitted=True`，Job 仍 suspended/Pod 不出现 | RQ 拒绝 Pod 创建 | 看 `kubectl describe resourcequota` 和 Job/Pod 事件；RQ 必须 ≥ Kueue 允许的最大并发 |
| Workload 能准入但很快被 `WaitForPodsReady` 驱逐 | RQ 导致 Pod 永远起不来 | 先修 RQ，再考虑放宽 Kueue 配额 |
| Kueue 账本与 RQ 数字对不上 | LimitRange 默认值/统计维度不同 | 对比 Pod requests、Workload `resourceUsage` 和 RQ `used` |
| 迁移期间作业双重受限 | Kueue 和 RQ 都设了硬上限 | 迁移期只保留一本"硬账"，另一本先设为观察/宽松 |

迁移建议：

1. **先盘点**：列出每个 namespace 的 RQ、LimitRange 和 Kueue CQ/LQ，确认 RQ 的上限不小于 Kueue 允许的峰值；
2. **先放宽 RQ**：把 RQ 调到"不会挡住 Kueue 准入"的水平，用告警观察而不是直接拒绝；
3. **再切 Kueue**：先用 `Warn`/`Audit` 策略观察未声明队列的作业，再强制；
4. **保留 RQ 做兜底**：RQ 仍然可以防住绕过 Kueue 的 Pod（例如没有 queue-name 的裸 Pod），但不要让两本账互相打架；
5. **加对账**：定期比较 `kueue_cluster_queue_resource_usage` 与 `kube_resourcequota{resource="requests.cpu"}`，差异持续存在就排查统计维度。

一个实用原则：**Kueue 是"作业级承诺"，ResourceQuota 是"namespace 级保险丝"**。保险丝不应该比承诺更紧；否则承诺就是空的。

### 11.3.2 namespace/LocalQueue 生命周期与孤儿配额

多租户平台里，"谁删了什么"比"谁创建了什么"更容易留下脏状态。常见边界：

| 操作 | 会发生什么 | 处理 |
|---|---|---|
| 删除 namespace | 其中的 LocalQueue、Workload、Job 被级联删除；如果有 finalizer 且控制器不可用，namespace 可能卡在 Terminating | 先恢复控制器；确认 Workload 的 finalizer 能被清理 |
| 删除 LocalQueue，但有 Pending Workload | Workload 留在原地，变成 `Inadmissible`（找不到队列）；同名重建 LQ 且指向同一 CQ 后可恢复 | 删除前先 drain；恢复时保持同名同 namespace |
| 用户手工删除 Workload | Job 集成可能重新创建（Job 还在），新 Workload 的 UID 变了；旧配额如果没释放会泄漏 | 不要手工删 Workload；用 `kueuectl stop` 或删 Job |
| 孤儿 Workload（owner 已删） | 可能仍占着配额，直到 finalizer/控制器清理 | 审计 `ownerReferences` 为空的 Workload；用 `objectRetentionPolicies` 兜底 |
| namespace 删除后同名重建 | 旧 LQ 不在了，新 Job 若没有 queue-name 会直接不受管理或 Pending | 重建时同时重建 LQ；用 GitOps 保证 namespace 与 LQ 原子创建 |
| 集成控制器停止 reconcile | Workload 可能一直 Admitted，配额不释放 | 监控 owner 是否存在；用 `maximumExecutionTimeSeconds` 做最后兜底 |

一个最小孤儿审计：

```bash
kubectl get workloads -A -o json | jq -r '
  .items[]
  | select((.metadata.ownerReferences // []) | length == 0)
  | "\(.metadata.namespace)/\(.metadata.name)\t\(.status.conditions // [])"
'
```

清理前先确认它不是正在运行的作业（看 `Admitted`/`Finished` 条件和 owner 对象），不要直接批量 delete。namespace 删除是破坏性操作，应该由平台流程而不是租户自助完成。

## 11.4 优先级与抢占的滥用

### 11.4.1 高优先级抢占

如果用户能创建高优先级 Workload，就能抢占别人的作业。风险：

- 恶意用户反复提交高优先级作业，造成拒绝服务；
- 关键业务被低优先级"刷子"影响；
- 抢占风暴导致集群抖动。

防护：

- 限制 `WorkloadPriorityClass` 的创建/使用；
- 用准入策略限制哪些 namespace/ServiceAccount 能用哪些优先级；
- 对 `withinClusterQueue`/`reclaimWithinCohort: Any` 要非常谨慎；
- 监控 `kueue_preempted_workloads_total` 和驱逐原因；
- 给关键队列设置更高的 nominal/权重，而不是只靠高优先级。

### 11.4.2 公平分享权重

`fairSharing.weight` 会影响长期份额。普通用户不应能修改 CQ 的权重。

### 11.4.3 Configurable Preemption

`PreemptionConfig` 的 `AnyClusterQueue` 作用域非常强。规则如果配置错误或对称，可能导致：

- 跨队列级联抢占；
- 作业反复被驱逐；
- 集群有效吞吐下降。

只允许平台管理员管理 `PreemptionConfig`，并做非对称设计。规则变更要走代码评审和 staging 验证；不要让租户通过 annotation 指定任意 `preemption-config-name`。

### 11.4.4 资源变换的配额绕过风险

资源变换是集群级 Configuration，普通用户不能修改。配置错误会导致配额被绕过或误计：

- **Replace + 空 outputs**：input 被忽略；如果它本来是唯一的计费资源，用户可能"免费"使用；
- **multiplyBy 缺失**：输出为 0，资源不计费；
- **错误映射到便宜的 output**：用户用昂贵资源却只消耗便宜配额；
- **和 DRA 重复映射**：同一资源被算两次或一次都不算。

防护：

- 只允许平台管理员修改 Configuration；
- 对变换后的资源做 CQ 覆盖校验（真实 Kueue 的 webhook/准入应拒绝无法计费的组合）；
- 在 staging 用真实 PodSpec 做端到端验证；
- 监控"Pod 请求的资源"与"Workload 计费资源"的差异；
- 定期审计 Configuration 的变更历史。

## 11.5 AdmissionCheck 的信任边界

AdmissionCheck 控制器是一个"准入闸门"。如果它被攻破或配置错误：

- 可以阻止所有作业；
- 可以把不该运行的作业放行；
- 可以触发云资源创建（ProvisioningRequest）；
- 可以跨集群复制 Workload（MultiKueue）。

防护：

- AdmissionCheck 控制器只部署可信镜像；
- 限制谁能创建/修改 AdmissionCheck 和它的 parameters；
- 给控制器最小权限；
- 监控检查的 Pending/Retry/Rejected 率；
- 对 ProvisioningRequest 设置云配额/预算上限；
- 对 MultiKueue 的 kubeconfig 使用 Secret/ClusterProfile，不用 Path。

## 11.6 MultiKueue 凭据安全

MultiKueue 需要访问 worker 集群。kubeconfig 来源：

| 来源 | 风险 |
|---|---|
| `ClusterProfile` | 推荐，联邦凭据发现 |
| `Secret` | 推荐，Kubernetes Secret 管理 |
| `Path` | 仅开发，控制器文件系统路径，可能被路径穿越/任意文件读取 |

`MultiKueueKubeConfigPathValidation` 限制 Path 只能指向固定前缀，防止 `..`、符号链接逃逸。生产不要用 Path。

防护：

- worker kubeconfig 使用最小权限 ServiceAccount；
- 定期轮换凭据；
- 限制谁能创建 `MultiKueueConfig`/`MultiKueueCluster`；
- 审计 manager 到 worker 的跨集群操作；
- 不要把 worker 的管理员 kubeconfig 交给 manager。

## 11.7 Webhook 与 CRD 安全

Kueue 的 webhook 负责：

- 默认值；
- 合法性校验；
- 不可变字段检查；
- 某些状态同步。

风险：

- webhook 证书过期/不可用会导致创建/更新失败；
- webhook 配置错误可能拒绝所有 Kueue 对象；
- CRD 权限过宽会让用户绕过校验。

防护：

- 监控 webhook 可用性和证书有效期；
- 使用 cert-manager 或 Kueue 内置证书管理；
- 限制 CRD 的 RBAC；
- 升级前在 staging 验证 webhook。

## 11.8 Pod 与节点隔离

Kueue 的 ResourceFlavor 可以把作业定向到特定节点（nodeSelector/tolerations），但：

- 它不保证节点之间的强隔离；
- 多租户共享节点时，仍有侧信道、资源争抢、内核漏洞风险；
- 高安全场景应使用专用节点池、taint/toleration、RuntimeClass、Sandbox（如 gVisor/Kata）；
- GPU 隔离（MIG、时间片、DRA 分区）由设备插件/运行时负责，不是 Kueue。

## 11.9 供应链与镜像

- 只允许可信镜像仓库；
- 使用签名/扫描；
- 限制 Job 的 imagePullPolicy/privileged/hostPath；
- 用 Pod Security Admission 限制权限；
- 对 AdmissionCheck/ProvisioningRequest 控制器做供应链审计。

Kueue 不检查镜像内容；它只决定作业是否获得配额。

## 11.10 审计与可观测性中的敏感信息

- Workload/Job 名称、namespace、镜像、命令可能包含敏感信息；
- 指标标签可能暴露租户/队列名称；
- 日志可能包含 kubeconfig、云凭据（尤其是 MultiKueue）；
- 限制谁可以读 Workload/Event/日志；
- 对审计日志做保留和访问控制；
- 不要在 Job annotation 里放密钥。

## 11.11 威胁模型表

| 威胁 | 影响 | 缓解 |
|---|---|---|
| 用户创建高优先级作业 | 抢占/饥饿 | 限制 PriorityClass/WorkloadPriorityClass |
| 用户修改 Namespace 标签 | 越权使用 CQ | 限制 namespace patch；准入策略保护标签 |
| 用户创建/修改 CQ | 破坏配额 | 仅管理员 RBAC |
| 用户伪造 Workload | 绕过 Job 集成 | Workload 只读；webhook 校验 |
| AdmissionCheck 控制器被攻破 | 任意准入/拒绝 | 最小权限、镜像信任、审计 |
| MultiKueue Path kubeconfig | 任意文件读取/跨集群提权 | 用 Secret/ClusterProfile，限制 Path |
| 抢占规则对称 | 级联抢占/拒绝服务 | 非对称规则、优先级/作用域限制 |
| 资源变换配置错误 | 配额绕过/重复计费 | 仅管理员可改 Configuration；覆盖校验；差异审计 |
| 过度宽松的 namespaceSelector | 跨租户资源使用 | 标签治理、准入策略 |
| 日志泄露凭据 | 集群接管 | 脱敏、访问控制、轮换 |

## 11.12 安全基线检查清单

### 身份与权限

- [ ] 只有平台管理员能创建/修改 ClusterQueue、Cohort、ResourceFlavor、AdmissionCheck、Topology；
- [ ] 只有平台管理员能创建/修改 WorkloadPriorityClass 和 PreemptionConfig；
- [ ] 租户不能 patch 自己的 Namespace 标签；
- [ ] Workload 对租户只读；
- [ ] `kueuectl stop/resume` 权限受控；
- [ ] Visibility/只读角色有最小权限。

### 配置

- [ ] `namespaceSelector` 没有误用 `{}` 放开所有 namespace；
- [ ] 资源变换没有把关键资源 Replace 成空 outputs；
- [ ] 资源变换的 outputs 都被目标 CQ 覆盖；
- [ ] 不安全 CQ 没有配 `reclaimWithinCohort: Any`；
- [ ] `borrowWithinCohort.maxPriorityThreshold` 有上限；
- [ ] Configurable Preemption 规则非对称；
- [ ] `manageJobsWithoutQueueName` 关闭或限定 namespace；
- [ ] namespace 的 ResourceQuota 不小于 Kueue 允许的峰值，或迁移期已放宽；
- [ ] LimitRange 默认值与 Kueue 资源汇总口径一致。

### 凭据与运行时

- [ ] MultiKueue 使用 Secret/ClusterProfile，不用 Path；
- [ ] worker kubeconfig 是最小权限、定期轮换；
- [ ] AdmissionCheck/ProvisioningRequest 控制器镜像可信；
- [ ] ProvisioningRequest 有云预算/配额上限；
- [ ] webhook 证书有效期有监控；
- [ ] Pod Security/NetworkPolicy/节点隔离独立配置。

### 审计

- [ ] 记录 CQ/Cohort/AdmissionCheck 变更；
- [ ] 记录抢占/驱逐事件；
- [ ] 日志脱敏，不泄露 kubeconfig/云凭据；
- [ ] 监控异常优先级/抢占/队列创建。

## 11.13 本章小结

- Kueue 是资源治理层，不是安全边界；RBAC/Namespace/NetworkPolicy/Pod Security 仍然必需。
- 平台管理员与租户的权限要分开；CQ/Cohort/Flavor/AdmissionCheck/PriorityClass 不应由普通用户管理。
- namespaceSelector 的安全性依赖 Namespace 标签的治理；`{}` 是"全部允许"，不是"无 selector"。
- ResourceQuota 和 Kueue 是两本账；RQ 比 Kueue 更紧会导致"Admitted 但 Pod 建不出来"，迁移期只保留一本硬账。
- 优先级、抢占、Configurable Preemption 都是可被滥用的能力，需要限制和监控。
- 只读角色也可能泄露 Workload 里的镜像/命令/环境变量。
- MultiKueue 凭据是高价值目标，生产用 Secret/ClusterProfile。
- 供应链、镜像、节点隔离、审计仍然要单独设计。
- 用 11.12 的检查清单做上线前基线。

---

[← 上一章：运维与可观测性](10-operations.md) | [返回目录](../README.md) | [下一章：性能与规模 →](12-performance-scale.md)
