# 第 26 章 多租户与资源治理

## 26.1 多租户不是“多建几个 namespace”

Namespace 只提供名字作用域和 RBAC/Quota 边界，不提供：

- 网络隔离（需要 NetworkPolicy）。
- 节点隔离（需要 nodeSelector/taints/节点池）。
- 内核隔离（需要沙箱运行时/VM）。
- 存储隔离（需要 CSI/加密/访问控制）。
- 控制面隔离（共享 API Server/etcd）。
- 故障隔离（noisy neighbor、OOM、API 风暴）。

多租户是分层设计：

```text
软多租户：共享集群，靠 namespace/RBAC/Quota/NetworkPolicy
硬多租户：独立节点池/独立集群/VM 级隔离
```

选择取决于信任模型和合规要求。

## 26.2 隔离级别

### 逻辑隔离（软）

- Namespace。
- RBAC。
- ResourceQuota/LimitRange。
- NetworkPolicy。
- Pod Security Admission。
- 节点亲和性（可选）。

适合：

- 同一组织的不同团队。
- 非恶意用户。
- 成本敏感、追求共享效率。

风险：

- 内核共享，容器逃逸影响全局。
- API Server/etcd 共享，资源耗尽互相影响。
- 网络策略配置错误导致横向移动。
- 节点故障影响多个租户。

### 节点池隔离

每个租户/团队使用专用节点池：

- taints/tolerations + nodeAffinity。
- 防止跨租户调度。
- 隔离 CPU/内存/GPU 噪声。
- 成本更高，资源利用率更低。

适合：

- GPU/高性能租户。
- 合规要求较高。
- 需要独立扩缩容。

### 集群隔离

每个租户独立集群：

- 最强隔离。
- 独立控制面/etcd/网络/存储。
- 运维和成本最高。
- 多集群管理复杂（Cluster API、Karmada、Argo CD）。

适合：

- 强合规/强安全。
- 大规模平台。
- 不同 SLA/版本要求。

### 虚拟集群

vCluster、Kamaji 等提供“集群中的集群”：

- 租户看到独立 API Server/控制面。
- 默认模式下工作负载共享宿主节点，隔离主要在 API/RBAC/CRD 层；vCluster 的 Private Nodes/Standalone 模式可以提供专属数据面。
- 隔离性从“namespace 和独立集群之间”到“接近独立集群”不等，取决于部署模式。
- 需要评估网络/存储/权限边界，不能默认把共享模式当生产强隔离。

### 26.2.1 层级命名空间与“租户即代码”

大型组织往往不是“一个团队一个 namespace”，而是“部门 → 团队 → 应用”的层级：

- **HNC（Hierarchical Namespace Controller）**：把 namespace 组织成树，父级的 RBAC/Quota/NetworkPolicy 可以向下传播；适合渐进式迁移和部门级治理。
- **Capsule**：用 `Tenant` CRD 把“一组 namespace + 配额 + 默认策略 + 用户/组”打包，租户自助创建 namespace，平台仍保留边界。
- **vCluster/Kamaji**：需要独立控制面时使用（见上）。

选择原则：层级治理优先用 HNC/Capsule，强隔离才上虚拟集群/独立集群。任何“租户自助创建 namespace”的方案都必须配套 Quota、LimitRange、NetworkPolicy、PSA 和准入，否则只是把冲突从平台团队转移给租户。

### 26.2.2 入口隔离：Gateway API 的 allowedRoutes 与 ReferenceGrant

多租户不仅怕 Pod 互通，也怕“一个租户的路由劫持另一个租户的域名/后端”。Gateway API 把边界显式化：

- `Gateway.spec.listeners[].allowedRoutes.namespaces`：限制哪些 namespace 的 Route 可以绑定到该 Gateway；`Same` 只允许同 namespace，`Selector` 按标签授权。
- `ReferenceGrant`：Route 跨 namespace 引用 Service/Secret 时，必须由**被引用方**显式授权；只靠 RBAC 或 NetworkPolicy 不够。
- `HTTPRoute` 的 `hostname`/`matches`：平台应限制通配符和冲突域名；同一 hostname 的多个 Route 可能产生不可预测的匹配。
- TLS 证书：证书 Secret 的 namespace 与 ReferenceGrant 要一起设计，避免租户读取他人证书。

验收方法：以租户 A 的身份创建一个指向租户 B Service 的 HTTPRoute，确认 `status.parents` 为 `ResolvedRefs=False`/`Accepted=False`；再检查没有 ReferenceGrant 时数据面不加载该路由。

## 26.3 ResourceQuota

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: team-a
  namespace: team-a
spec:
  hard:
    requests.cpu: "20"
    requests.memory: 40Gi
    limits.cpu: "40"
    limits.memory: 80Gi
    pods: "100"
    services: "20"
    persistentvolumeclaims: "10"
    count/deployments.apps: "30"
    count/jobs.batch: "50"
```

配额维度：

- 计算资源：requests/limits CPU/内存。
- 对象数量：pods、services、configmaps、secrets、PVC。
- 存储：requests.storage、特定 StorageClass。
- 扩展资源：nvidia.com/gpu。
- 自定义资源：count/<plural>.<group>。

注意：

- 配额是命名空间级总量。
- 创建 Pod 时检查 requests/limits；没有设置可能被拒绝（取决于配额配置）。
- Deployment 创建的 Pod 也计入配额。
- 配额状态 `status.used` 由控制器更新，不是实时强一致。
- 修改配额可能被现有使用量阻塞。

MiniK8s 在准入阶段计算 usage 和 delta，测试 `test_resource_quota_blocks_create`。

## 26.4 LimitRange

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: defaults
  namespace: team-a
spec:
  limits:
    - type: Container
      defaultRequest: {cpu: 100m, memory: 128Mi}
      default: {cpu: 500m, memory: 512Mi}
      min: {cpu: 50m, memory: 64Mi}
      max: {cpu: "4", memory: 8Gi}
      maxLimitRequestRatio: {cpu: "10", memory: "4"}
```

作用：

- 为未设置资源的容器注入默认值。
- 拒绝低于 min/高于 max 的容器。
- 限制 limit/request 比例（防止过度超卖）。

LimitRange 是命名空间级，不跨 namespace。

## 26.5 PodDisruptionBudget

PDB 保护自愿驱逐：

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: web
spec:
  minAvailable: 2
  selector:
    matchLabels: {app: web}
```

或：

```yaml
maxUnavailable: 1
```

字段与状态：

| 字段 | 说明 |
|---|---|
| `minAvailable` / `maxUnavailable` | 二选一，不能同时设置；可以是整数或百分比 |
| `selector` | 选中受保护的 Pod；空 selector 会选中 namespace 内所有 Pod，慎用 |
| `unhealthyPodEvictionPolicy` | `IfHealthyBudget`（默认）或 `AlwaysAllow`；决定不健康 Pod 是否可以被驱逐 |
| `status.currentHealthy` | 当前健康 Pod 数 |
| `status.desiredHealthy` | 预算要求的最小健康数 |
| `status.expectedPods` | 期望 Pod 数 |
| `status.disruptionsAllowed` | 当前还允许驱逐几个 |
| `status.observedGeneration` | 控制器处理到的 generation |

`kubectl drain` 和 Eviction API 会检查 `disruptionsAllowed`；直接 `kubectl delete pod` **不检查** PDB，因为那是非自愿/绕过 API 的删除。

一个常见的维护卡住场景：

```text
PDB: minAvailable: 3，期望 3 个副本
其中 1 个 Pod 处于 CrashLoopBackOff（不健康）
→ currentHealthy = 2 < desiredHealthy = 3
→ disruptionsAllowed = 0
→ drain 永远无法驱逐剩余 Pod
```

默认 `IfHealthyBudget` 下，不健康 Pod 也可能因为预算不足而无法驱逐；把策略设为 `AlwaysAllow` 可以让不健康 Pod 先被清走，但要接受短时可用性下降。PDB 只保护自愿驱逐，不保护节点故障、OOM 和直接删除。

MiniK8s 实现 PDB 状态和 Eviction 检查，测试 `test_pdb_blocks_eviction`。

## 26.6 PriorityClass

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: production-critical
value: 100000
preemptionPolicy: PreemptLowerPriority
```

用途：

- 调度顺序。
- 抢占。
- 驱逐顺序。

设计：

- 少量层级（如 3–5 个），不要给每个团队自定义数值。
- 系统组件使用 `system-*-critical`。
- 在线服务高于离线批处理。
- 高优先级意味着可以驱逐别人，必须配合配额/准入。

MiniK8s 支持 PriorityClass 和抢占，测试 `test_preemption_evicts_lower_priority`。

## 26.7 网络与存储隔离

网络：

- 默认拒绝 NetworkPolicy。
- 只允许同 namespace + 明确依赖。
- 控制 DNS、监控、日志、Ingress 的访问。
- 出口网关/白名单。

存储：

- 每个租户独立 StorageClass/后端。
- PVC 加密。
- 禁止 hostPath。
- 快照/备份隔离。
- 防止一个租户的 IO 影响其他人（QoS/限速）。

## 26.8 节点与资源治理

- 节点池按租户/工作负载类型划分。
- taints 防止跨池调度。
- 节点自动伸缩按池配置。
- 节点预留（kube-reserved/system-reserved）。
- 避免一个租户占满所有节点。

噪声邻居：

- CPU 节流。
- 内存 OOM。
- 磁盘 I/O。
- 网络带宽。
- API Server 请求风暴。

缓解：

- requests/limits。
- QoS。
- 节点隔离。
- API Priority and Fairness（APF）。
- 限流/配额。
- 监控与告警。

## 26.9 API Priority and Fairness

APF 防止某个客户端打爆 API Server：

- **FlowSchema** 把请求分类（按 user/group/serviceaccount、verb、resource、namespace）。
- **PriorityLevelConfiguration** 定义该类的并发份额与排队/拒绝策略。
- 区分系统组件、控制器、普通用户、长尾请求。

最小示例：

```yaml
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: PriorityLevelConfiguration
metadata:
  name: batch
spec:
  type: Limited
  limited:
    nominalConcurrencyShares: 5
    lendablePercent: 50
    limitResponse:
      type: Queue
      queuing:
        queues: 16
        handSize: 4
        queueLengthLimit: 50
---
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: FlowSchema
metadata:
  name: batch-jobs
spec:
  priorityLevelConfiguration:
    name: batch
  matchingPrecedence: 1000
  rules:
    - subjects:
        - kind: ServiceAccount
          serviceAccount:
            namespace: batch
            name: runner
      resourceRules:
        - verbs: ["*"]
          apiGroups: ["*"]
          resources: ["*"]
          clusterScope: true
          namespaces: ["*"]
```

关键字段：

| 字段 | 作用 |
|---|---|
| `nominalConcurrencyShares` | 该优先级层相对其他层的并发份额 |
| `lendablePercent` | 可借给其他层的空闲份额 |
| `limitResponse.type` | `Queue`（排队）或 `Reject`（直接 429） |
| `queuing.queues` / `handSize` / `queueLengthLimit` | 队列数量、洗牌宽度、单队列长度 |
| `matchingPrecedence` | 数字越小优先级越高；匹配后停止继续匹配 |
| `distinguisherMethod` | 按 flow distinguisher（如用户）拆队列，避免单客户端占满 |

行为：

- 超过并发限额时，`Queue` 会让请求排队；队列满/等待超时则返回 **429 Too Many Requests**，客户端应读取 `Retry-After` 并退避。
- APF 不替代 RBAC 或 ResourceQuota：它保护 API Server 的**处理能力**，不控制谁能创建什么、能占多少资源。
- 内置的 `system`、`workload-leader-election`、`node-high` 等 FlowSchema 保护关键流量，不要随意删除或降级。

监控指标：

```promql
# 被拒绝的请求
sum by (priority_level, flow_schema) (rate(apiserver_flowcontrol_rejected_requests_total[5m]))

# 排队中的请求
sum by (priority_level) (apiserver_flowcontrol_current_inqueue_requests)

# 正在执行的请求
sum by (priority_level) (apiserver_flowcontrol_current_executing_requests)

# 排队等待时间
histogram_quantile(0.99, sum by (le, priority_level) (rate(apiserver_flowcontrol_request_wait_duration_seconds_bucket[5m])))
```

生产建议：

- 保护 kubelet/controller-manager 的关键请求。
- 限制批量创建/删除的客户端。
- 监控 APF 的排队/拒绝指标；429 激增时先看是容量不足还是某个客户端失控。

## 26.10 成本治理

多租户必须回答“谁花了多少钱”：

- 标签/命名空间映射成本中心。
- GPU/CPU/内存/存储/网络成本分摊。
- 闲置资源识别。
- 配额与预算告警。
- Showback/Chargeback。

工具：

- Kubecost、OpenCost。
- 云厂商成本工具。
- Prometheus + 自定义报表。

没有成本可见性，多租户会变成“公共资源悲剧”。

## 26.11 平台治理清单

- 每个团队独立 namespace，禁止共用 default。
- 每个 namespace 有 ResourceQuota + LimitRange。
- 默认拒绝 NetworkPolicy，按需放行。
- Pod Security restricted/baseline。
- 镜像仓库白名单 + 签名校验。
- 关键服务 PDB。
- 优先级层级清晰。
- 节点池按工作负载隔离。
- 成本标签与配额告警。
- 定期审计 RBAC 和资源使用。

## 26.12 MiniK8s 的边界

MiniK8s 实现：

- Namespace 生命周期与级联删除。
- ResourceQuota/LimitRange。
- PDB/Eviction。
- PriorityClass/抢占。
- NetworkPolicy 判定。
- Pod Security 简化版。
- RBAC。

不实现：

- 节点池隔离。
- APF。
- 成本分摊。
- 虚拟集群。
- 真实内核隔离。

## 26.13 多租户隔离矩阵与验收方法

多租户方案不能只说“我们用了 namespace”。建议用一张矩阵逐项验收。

| 维度 | 软隔离（namespace） | 节点池 | 独立集群 | 验收方法 |
|---|---|---|---|---|
| 名字/API | RBAC + 独立 namespace | 同左 | 独立 API Server | `kubectl auth can-i --list` |
| 计算 | Quota/LimitRange | taints + 专用节点 | 独立节点 | 压测 noisy neighbor |
| 网络 | NetworkPolicy 默认拒绝 | 节点网络隔离 | 独立 CNI/VPC | 跨租户连通性测试 |
| 存储 | StorageClass + PVC 隔离 | 专用 CSI/后端 | 独立存储 | 越权挂载测试 |
| 内核 | PSA/运行时沙箱 | 同左 | 独立节点/VM | 逃逸演练 |
| 控制面 | 共享 API/etcd | 共享 | 独立 | API 风暴测试 |
| 故障域 | 共享节点/控制面 | 部分隔离 | 完全隔离 | 故障注入 |
| 成本 | 最低 | 中 | 最高 | 成本分摊报表 |

### 必测的“越权”场景

1. 租户 A 能否读取租户 B 的 Secret/ConfigMap/日志？
2. 租户 A 的 Pod 能否访问租户 B 的 Service/Pod IP？
3. 租户 A 能否创建 hostPath/privileged/hostNetwork Pod？
4. 租户 A 能否通过 PVC 挂载其他租户的卷？
5. 租户 A 能否创建 CRD/Webhook/RoleBinding 提权？
6. 租户 A 的资源风暴是否影响租户 B 的 API 延迟？
7. 租户 A 能否通过 DNS 探测其他 namespace 的服务？

### 常见“假隔离”

- 只建 namespace，没有默认拒绝 NetworkPolicy：网络全通。
- 只设 ResourceQuota，没有 LimitRange：用户不写 requests，配额无法准确计算。
- 只配 PSA `warn`，没有 `enforce`：违规 Pod 仍能创建。
- 共享 `default` ServiceAccount：token 权限过大。
- 共享 Ingress Controller/Gateway 但没有限制哪些 namespace 可创建路由：路由冲突/劫持。
- 共享节点但没有 taints：资源争抢和内核故障跨租户传播。

### 治理节奏

- 新租户上线前跑一遍矩阵和越权测试。
- 每月审计 RBAC/NetworkPolicy/Quota 使用。
- 每季度做一次跨租户故障演练。
- 把“隔离等级”写进平台合同/SLO，而不是口头承诺。

MiniK8s 支持 namespace/RBAC/Quota/NetworkPolicy/PSA 的简化判定，足以演示软隔离；硬隔离（节点池/集群/VM）不在模拟范围内。GPU 的整卡/MIG/time-slicing 隔离与多租户风险见第 22 章。

## 26.14 合规映射与审计证据

多租户/治理做到一定程度，就会遇到“合规”问题：CIS、SOC 2、ISO 27001、PCI DSS、等保 2.0。合规不是另一套技术，而是把已有控制项映射到框架，并持续产出证据。

### 26.14.1 常见框架关注什么

| 框架 | 关注点 | 与 Kubernetes 的关系 |
|---|---|---|
| CIS Kubernetes Benchmark | 控制面/节点/kubelet 的安全配置 | 大量具体检查项（文件权限、参数、RBAC） |
| NSA/CISA Kubernetes Hardening | 威胁模型与加固 | Pod 安全、网络隔离、审计、供应链 |
| SOC 2 | 安全性/可用性/保密性等信任原则 | 访问控制、变更管理、监控、事件响应 |
| ISO 27001 | 信息安全管理体系 | 资产、风险、供应商、持续改进 |
| PCI DSS | 支付卡数据 | 网络分段、加密、日志、访问控制、漏洞管理 |
| 等保 2.0 | 中国网络安全等级保护 | 身份鉴别、访问控制、审计、入侵防范、数据完整性 |

框架不会告诉你“把 `--anonymous-auth` 关掉”，而是要求“未授权访问被阻止”。你需要把要求翻译成具体控制项。

### 26.14.2 控制项映射示例

| 控制目标 | Kubernetes 机制 | 证据 |
|---|---|---|
| 身份鉴别 | OIDC/证书/SA token、MFA | IdP 配置、kubeconfig 策略、认证日志 |
| 最小权限 | RBAC、namespace、SA | Role/Binding 清单、`auth can-i` 报告 |
| 网络分段 | NetworkPolicy、CNI、多集群 | 策略清单、连通性测试、流量日志 |
| 数据加密 | etcd encryption、TLS、Secret 加密 | EncryptionConfiguration、证书、KMS 审计 |
| 审计追踪 | API 审计、容器日志、云审计 | 审计策略、日志保留、SIEM 查询 |
| 漏洞管理 | 镜像扫描、准入、运行时检测 | 扫描报告、拒绝事件、修复 SLA |
| 变更管理 | GitOps、PR、审批、渐进发布 | Git 历史、Argo CD 同步记录、回滚记录 |
| 备份恢复 | etcd 快照、Velero、应用备份 | 备份成功率、恢复演练报告 |
| 事件响应 | 告警、runbook、取证 | 告警记录、事故复盘、演练记录 |

这张表是“合规语言”和“平台语言”的翻译层。没有它，审计员问“你如何保证最小权限”，团队只能拿一堆 YAML。

### 26.14.3 证据要可持续产出

合规最怕“审计前突击”。可持续的证据来自自动化：

- **配置扫描**：kube-bench（CIS）、Polaris、kube-score，定期跑并归档。
- **策略即代码**：OPA/Gatekeeper、Kyverno、VAP，拒绝事件就是证据。
- **RBAC 报告**：定期导出 Role/Binding、`auth can-i --list`、高危权限扫描。
- **镜像/供应链**：SBOM、签名验证、漏洞扫描报告。
- **审计日志**：集中存储、完整性保护、保留策略。
- **备份/恢复**：每月恢复演练，记录 RTO/RPO。
- **变更记录**：GitOps 的 Git 历史 + 同步状态 + 审批。
- **访问审查**：季度性账号/权限复核。

证据要包含：时间、对象、结果、责任人、异常处理。只有“截图”不算证据。

### 26.14.4 共享责任模型

托管 Kubernetes 下，责任是分层的：

```text
云厂商：物理设施、控制面、托管服务、部分网络/存储
你：工作负载、RBAC、NetworkPolicy、Secret、镜像、数据、审计配置
```

常见误区：

- 以为“托管 = 合规”：厂商合规不等于你的用法合规。
- 以为“开了加密 = 数据安全”：密钥管理、访问控制、备份加密仍要自己做。
- 以为“有审计日志 = 可审计”：没有保留/检索/告警，审计日志等于没有。

### 26.14.5 审计失败的常见原因

1. 权限过大：CI 用 cluster-admin，长期不收敛。
2. 没有审计：或审计只保存在控制面本地，节点故障即丢失。
3. Secret 管理混乱：明文进 Git、长期 token、无轮换。
4. 网络全通：没有默认拒绝的 NetworkPolicy。
5. 镜像无扫描/无签名：供应链不可追溯。
6. 备份没演练：恢复时才发现缺依赖。
7. 变更无记录：谁在什么时候改了什么说不清。
8. 多租户隔离只靠 namespace：越权测试一做就穿。

### 26.14.6 等保/金融场景的额外要求

- 身份鉴别可能要求双因素、定期更换、失败锁定。
- 审计日志要求保留 6 个月以上（以当地法规为准），并防篡改。
- 网络边界要求访问控制、入侵防范、恶意代码防范。
- 数据完整性/保密性要求加密与校验。
- 可能要求独立的安全区域/堡垒机/运维审计。

这些往往不是 Kubernetes 原生能力，需要叠加密钥管理、堡垒机、SIEM、WAF、终端安全等。

### 26.14.7 MiniK8s 的边界

MiniK8s 可以演示 RBAC、Quota、NetworkPolicy、PSA 的判定，但没有审计、加密、合规扫描。它适合验证“控制项对应的机制是否存在”，不适合作为合规证据。真实合规必须在目标平台/云上产出。

## 本章小结

- 多租户是分层设计，namespace 只是起点。
- 软隔离靠 RBAC/Quota/NetworkPolicy/PSA，硬隔离靠节点池/集群/VM。
- PDB 保护维护，PriorityClass 管理抢占，APF 保护 API Server。
- 成本可见性和配额治理是多租户可持续的前提。
- 用隔离矩阵逐项验收，并实际测试越权场景；namespace 不是隔离的同义词。
- 合规是把控制项映射到框架并持续产出证据；托管不等于合规，审计日志不等于可审计。

## 延伸阅读

- [第 23 章 认证、授权与 RBAC](23-authn-authz-rbac.md)
- [第 24 章 准入控制与供应链安全](24-admission-supply-chain.md)
- [第 30 章 性能与规模化](30-performance-scale.md)
- [第 45 章 商业分析 III](45-business-platform.md)
