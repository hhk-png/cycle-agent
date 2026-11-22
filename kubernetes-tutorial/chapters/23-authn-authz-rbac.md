# 第 23 章 认证、授权与 RBAC

## 23.1 三个问题

每个 API 请求都要回答：

1. **你是谁？**（Authentication）
2. **你能做什么？**（Authorization）
3. **这个对象是否合法？**（Admission）

顺序不能反：先认证，再授权，再准入。未认证请求不应该进入授权逻辑；未授权请求不应该进入准入或存储。

## 23.2 用户、组与 ServiceAccount

Kubernetes 没有“User”对象。用户由认证器产生：

- X.509 客户端证书：CN 是用户名，O 是组。
- OIDC：JWT 的 `sub`/`email` 是用户名，claims 映射组。
- Webhook：外部认证服务返回用户信息。
- ServiceAccount：Pod 使用的身份，存储在集群内。

组是授权的核心：

- `system:masters`：超级权限（绕过 RBAC）。
- `system:authenticated`：所有认证用户。
- `system:unauthenticated`：匿名用户。
- `system:serviceaccounts`：所有 ServiceAccount。
- `system:serviceaccounts:<namespace>`：某命名空间的 ServiceAccount。

MiniK8s 的 `User` 数据类包含 name/groups；`StaticTokenAuthenticator` 映射 token → User；`ServiceAccountTokenIssuer` 签发 HMAC token。

## 23.3 认证方式

### X.509 客户端证书

API Server 启动时指定 `--client-ca-file`。客户端证书由集群 CA 签发。

```bash
openssl req -new -key user.key -out user.csr -subj "/CN=alice/O=developers"
openssl x509 -req -in user.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out user.crt -days 365
```

kubeconfig：

```yaml
users:
  - name: alice
    user:
      client-certificate: /path/user.crt
      client-key: /path/user.key
```

问题：

- 证书长期有效，吊销困难。
- 需要重新签发才能改组。
- 不适合大规模用户。

### OIDC

API Server 配置：

```text
--oidc-issuer-url=https://auth.example.com
--oidc-client-id=kubernetes
--oidc-username-claim=email
--oidc-groups-claim=groups
```

流程：

1. 用户通过 IdP 登录，获得 ID Token。
2. kubectl 把 token 放进 Authorization: Bearer。
3. API Server 验证签名、issuer、audience、过期时间。
4. 提取 username/groups。

优点：

- 集中身份管理。
- 短期 token。
- 支持 MFA/SSO。

**版本注意**：上面的 `--oidc-*` 参数在较新版本已进入弃用流程，推荐使用 `--authentication-config` 指向 `AuthenticationConfiguration`（结构化认证配置，1.34 GA）。它支持多个 JWT 认证器、CEL claim 映射、条件校验和轮换，适合多 IdP/复杂 claim 场景。迁移时先用目标版本的 `kube-apiserver --help` 和文档确认字段，再保留回滚配置。

### ServiceAccount

Pod 内的身份：

```yaml
spec:
  serviceAccountName: deployer
```

传统方式挂载长期 Secret token；新方式使用 TokenRequest：

- 短期（默认 1 小时）。
- 可指定 audience。
- 自动轮换（projected volume）。
- 绑定 Pod，Pod 删除后失效。

MiniK8s 的 `/serviceaccounts/{name}/token` 返回 TokenRequest，`TokenReview` 验证。

### Webhook

外部认证服务：

```yaml
apiVersion: authentication.k8s.io/v1
kind: TokenReview
```

API Server 把 token 发给 webhook，返回用户信息。适合接入企业身份系统。

## 23.4 kubeconfig

```yaml
apiVersion: v1
kind: Config
clusters:
  - name: prod
    cluster:
      server: https://api.prod.example.com
      certificate-authority-data: ...
contexts:
  - name: prod-admin
    context:
      cluster: prod
      user: admin
      namespace: default
current-context: prod-admin
users:
  - name: admin
    user:
      client-certificate-data: ...
      client-key-data: ...
```

安全建议：

- 不要把 kubeconfig 提交到 Git。
- 使用 `kubectl config use-context` 明确环境。
- CI 使用短期 token/工作负载身份。
- 定期轮换证书。
- 限制 kubeconfig 文件权限（0600）。

## 23.5 RBAC 模型

RBAC 四个对象：

- **Role**：命名空间内的权限规则。
- **RoleBinding**：把 Role 绑定到主体（User/Group/ServiceAccount）。
- **ClusterRole**：集群级权限规则。
- **ClusterRoleBinding**：把 ClusterRole 绑定到主体。

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: pod-reader
  namespace: team-a
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: alice-read
  namespace: team-a
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: pod-reader
subjects:
  - kind: User
    name: alice
    apiGroup: rbac.authorization.k8s.io
  - kind: Group
    name: developers
    apiGroup: rbac.authorization.k8s.io
  - kind: ServiceAccount
    name: deployer
    namespace: team-a
```

规则字段：

- `apiGroups`：`""` 是核心组，`*` 是全部。
- `resources`：`pods`、`pods/log`、`deployments`、`*`。
- `verbs`：get/list/watch/create/update/patch/delete/deletecollection。
- `resourceNames`：限定具体对象名，只对 `get/update/patch/delete` 等“单对象”动词生效；**不适用于 list/watch/create/deletecollection**。field selector 也不会让 RBAC 按名字过滤 list；要限制 list 范围，用 namespace/标签隔离或 API 层过滤。
- `nonResourceURLs`：`/healthz`、`/metrics` 等非资源路径。

## 23.6 授权决策

API Server 按配置的授权模式链式判断：

- `RBAC`：推荐。
- `Node`：kubelet 专用。
- `Webhook`：外部授权。
- `ABAC`：历史遗留，不推荐。

默认拒绝：没有任何规则允许则 403。

**结构化授权配置**：1.32 起 `AuthorizationConfiguration` GA，可以用 `--authorization-config` 指定多 authorizer 的顺序、失败策略、webhook 连接池等，不再只能靠逗号分隔的 `--authorization-mode`。迁移时先导出当前模式，确认 `Node` authorizer 与 `RBAC` 的先后顺序不变，否则 kubelet/控制器的隐式权限可能失效。

RBAC 规则匹配：

- verb 匹配。
- apiGroup 匹配。
- resource（含子资源）匹配。
- resourceName 匹配（如果指定）。
- namespace 匹配（RoleBinding 只在命名空间内生效）。

### Node 授权器与 NodeRestriction

kubelet 的身份是 `system:node:<nodeName>`，属于 `system:nodes` 组。它不适合用普通 RBAC 授权，因为 kubelet 需要读的资源范围很特殊。Kubernetes 用 **Node authorizer** 做细粒度约束：

- 读自己的 Node 对象。
- 读绑定到本节点的 Pod。
- 读这些 Pod 引用的 Secret、ConfigMap、PVC、ServiceAccount。
- 读 Service/EndpointSlice（用于网络配置）。
- 写自己的 Node status、Pod status、Event、Lease。

它**不允许** kubelet 读其他节点的 Pod 或任意 Secret。再叠加 **NodeRestriction** 准入插件：

- kubelet 只能修改自己的 Node 对象和绑定到自己的 Pod。
- 不能修改其他节点的标签/污点。
- 不能给 Pod 添加受保护的标签/注解（`node-restriction.kubernetes.io/` 前缀）。

这解释了为什么“攻陷一个节点”不等于“拿到整个集群的 Secret”：节点身份被限制在自己的命名空间范围内。

TLS Bootstrap 的链路是：

```text
bootstrap token → kubelet 生成私钥/CSR → CSR 提交 → controller-manager 批准
→ 签发 system:node:<name> 客户端证书 → kubelet 轮换
```

审批权限本身是高价值目标：能批准任意 CSR 的人可以伪造节点身份。生产要限制 `certificatesigningrequests/approval` 的权限，并优先使用 `signerName` 限定的审批控制器。

MiniK8s 的 `RBACAuthorizer` 读取 ClusterRole/Binding、Role/Binding，按主语和规则匹配。测试覆盖：

- `test_rbac_denies_then_allows`
- `test_rbac_resource_names_and_subresources`
- `test_service_account_token_and_review`

## 23.7 常见 RBAC 设计

### 命名空间隔离

```text
team-a RoleBinding → team-a namespace
team-b RoleBinding → team-b namespace
```

不要让团队共用 `default` 命名空间；否则 RBAC 边界失效。

### 最小权限

- 开发：get/list/watch + 有限 create/update。
- CI：只允许更新特定 Deployment/ConfigMap。
- Operator：只允许管理自己的 CRD 和关联资源。
- 监控：只读 metrics/logs/events。

### 聚合 ClusterRole

```yaml
aggregationRule:
  clusterRoleSelectors:
    - matchLabels:
        rbac.example.com/aggregate-to-monitoring: "true"
```

新增规则只需创建带标签的 ClusterRole，不需要改主角色。适合平台团队扩展内置角色。

### 内置角色

- `cluster-admin`：超级权限，慎用。
- `admin`：命名空间内几乎全部权限。
- `edit`：读写工作负载，不能改 RBAC。
- `view`：只读。

不要给所有人 `cluster-admin`。它是审计和事故的根源。

## 23.8 权限提升风险

RBAC 中一些权限组合等价于 root。关键是区分“单独就危险”和“需要配合才危险”：

| 权限 | 单独等价 root？ | 条件/说明 |
|---|---|---|
| 创建 Pod + hostPath/privileged/hostNetwork/hostPID | 是 | 可逃逸到节点；PSA/准入是第一道防线 |
| 读取 Secret / ServiceAccount token | 是（对该身份） | 可冒用其他身份、读取凭据 |
| `pods/exec`、`pods/attach` | 通常是 | 进入任意容器等于拥有其凭据与文件系统 |
| 创建 DaemonSet | 是 | 可在每个节点以高权限运行 |
| 创建/修改 MutatingWebhookConfiguration | 是 | 可拦截并篡改任意资源，包括 Pod 安全字段 |
| 创建 RoleBinding/ClusterRoleBinding | **否，单独不够** | RBAC 有提权防护：只能授予自己已拥有的权限；要授予超出范围的角色，需要对该角色有 `bind`，或对 Role/ClusterRole 有 `escalate` |
| 创建 CRD | **否** | CRD 只是类型；但配套的 conversion webhook/controller 会扩大该类型的攻击面 |
| `verbs: ["*"]` | 取决于资源 | 只覆盖该规则里的 `resources`；`verbs: ["*"]` + `resources: ["pods"]` 不等于 `impersonate`。危险的是 `resources: ["*"]` / RBAC/身份资源上的通配 |

因此：

- “能创建 Pod”不等于“低权限”；“能创建 RoleBinding”也不自动等于 root，但 `bind` + 高权限角色就是。
- 用 Pod Security Admission/策略限制危险字段。
- 限制 `escalate`/`bind`/`impersonate` 权限，审计 RoleBinding/ClusterRoleBinding 变更。
- 用 `kubectl auth can-i --list` 和授权审查工具定期找“隐式 root”组合，而不是只看单个动词。

## 23.9 ServiceAccount 最佳实践

- 每个工作负载使用独立 SA，不要共用 `default`。
- 关闭不需要的 token 自动挂载：

```yaml
spec:
  automountServiceAccountToken: false
```

- 使用 TokenRequest 的 audience/expiration。
- 给 SA 精确的 RoleBinding。
- 不要把 SA token 写进镜像/日志。
- 云上使用 Workload Identity（IRSA、GKE Workload Identity、Azure AD Workload Identity），避免长期密钥。

## 23.10 审计

审计日志记录：

- 谁（user/groups/SA）。
- 做了什么（verb/resource/namespace/name）。
- 结果（allowed/denied）。
- 请求来源（IP、user-agent）。
- 请求体（审计策略只能选择记录/不记录层级，**上游没有字段级脱敏**）。

审计级别：

- None：不记录。
- Metadata：记录元数据。
- Request：记录请求体（可能含 Secret）。
- RequestResponse：记录请求和响应。

生产建议：

- 至少 Metadata。
- 对 Secret 读写单独告警。
- 审计日志集中存储、防篡改。
- 与 SIEM/告警集成。

MiniK8s 不实现审计日志，但 API 层记录了认证用户，便于扩展。

### 审计日志的脱敏边界

审计策略只有四个级别，**没有字段级 redaction**。把 Secret 规则设为 `Request`/`RequestResponse`，就会把 Secret 的 `data`（base64）或 `stringData` 明文写进审计后端；`kubectl.kubernetes.io/last-applied-configuration` 注解也可能把完整对象（包括 Secret 内容）带进去。

可操作的规则：

1. `secrets`、`configmaps`、`serviceaccounts/token` 永远停在 `Metadata`；不要为了“审计更全”升级到 `Request`。
2. 需要记录写操作内容时，在采集侧（Vector/Fluent Bit/OTel Collector）或 SIEM 做脱敏/丢弃字段；不要指望 API Server。
3. 审计日志的访问权限比普通日志更严格；它是攻击者的高价值目标。
4. 用一次受控的 `kubectl create secret` 验证：在审计后端搜索明文值，确认搜不到。
5. 保留期与 WORM/防篡改由后端负责；本地文件轮转不是合规方案。

### 23.10.1 审计策略：在正确的地方记录正确的字段

审计策略是一个 `AuditPolicy` 对象，由 `--audit-policy-file` 指定。规则按顺序匹配，第一条命中的规则决定级别；未命中使用默认级别。

```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages:
  - RequestReceived
rules:
  # 1. 不记录高频只读的发现请求
  - level: None
    verbs: ["get", "list", "watch"]
    resources:
      - group: ""
        resources: ["endpoints", "services", "nodes"]
    namespaces: ["kube-system"]

  # 2. Secret/ConfigMap 只记元数据，避免把敏感值写进日志
  - level: Metadata
    resources:
      - group: ""
        resources: ["secrets", "configmaps"]

  # 3. 认证/授权/准入的写操作记录请求体
  - level: Request
    verbs: ["create", "update", "patch", "delete"]
    resources:
      - group: "rbac.authorization.k8s.io"
        resources: ["roles", "rolebindings", "clusterroles", "clusterrolebindings"]
      - group: "admissionregistration.k8s.io"
        resources: ["*"]

  # 4. exec/attach/portforward 是高危操作，记录请求与响应
  - level: RequestResponse
    resources:
      - group: ""
        resources: ["pods/exec", "pods/attach", "pods/portforward", "pods/proxy"]

  # 5. 默认记录写操作元数据
  - level: Metadata
    verbs: ["create", "update", "patch", "delete", "deletecollection"]
```

要点：

- `RequestResponse` 会记录响应体，可能包含 Secret 数据；对 Secret 资源应使用 `Metadata`。
- `omitStages` 可去掉 `RequestReceived`，减少重复。
- 规则顺序决定级别；宽泛规则放后面。
- 审计策略本身是高权限配置，修改要审计。
- 对 `system:kube-apiserver` 等系统组可降级，避免噪声淹没信号。

### 23.10.2 审计后端与阶段

审计事件有四个阶段：

```text
RequestReceived → ResponseStarted → ResponseComplete
                              └──→ Panic（异常）
```

后端：

- **log**：写本地文件，支持轮转；最简单，但难集中检索。
- **webhook**：批量发送到外部服务，适合 SIEM；要处理背压与失败策略。
- **log + webhook**：常见组合。

`--audit-log-maxsize`、`--audit-log-maxbackup`、`--audit-log-maxage` 控制本地文件；webhook 的 `batchTimeout`/`batchSize` 控制吞吐。审计写入失败时，API Server 可以选择阻塞或忽略；生产更常见的是“不能丢关键审计”，但也要避免审计后端故障拖垮控制面，需要明确策略。

### 23.10.3 成本与保留

审计日志量可能非常大：

- 高频 list/watch 会产生大量事件。
- `RequestResponse` 会放大体积。
- 客户端重试、聚合 API/代理转发、审计 webhook 重放会造成重复记录；**不是**“每个控制面副本都记录同一请求”——一个请求只由一个 API Server 处理，副本不会各自记录一遍。审计容量要按实际重试/放大比例估算，而不是乘以副本数。

控制成本：

1. 先 `Metadata`，只对高危资源升级到 `Request`。
2. 过滤系统组的高频只读请求。
3. 采集端做采样/聚合，但**不要采样高危写操作**。
4. 分级保留：热存 7–30 天，冷存 90–365 天（按合规要求）。
5. 对审计日志做完整性保护（WORM/对象锁/独立账号）。

### 23.10.4 取证工作流

审计日志的价值在于回答“谁在什么时候改了什么”。一个可执行的流程：

1. **确定时间窗**：从告警/变更单/用户报告得到起止时间。
2. **定位主体**：user、groups、ServiceAccount、impersonated 用户。
3. **看写操作**：create/update/patch/delete、RBAC、Secret、exec。
4. **关联对象**：resource/namespace/name/uid；注意同名对象可能被删除重建。
5. **关联请求来源**：sourceIPs、userAgent、responseStatus。
6. **交叉验证**：API 审计 vs 容器日志 vs 网络流 vs 云审计（CloudTrail 等）。
7. **固化证据**：导出原始 JSON、计算哈希、记录查询语句与时间。

常见调查场景：

- 谁删除了生产 namespace？
- 谁读取了所有 Secret？
- 哪个 ServiceAccount 在异常时间创建了 ClusterRoleBinding？
- 谁 exec 进了容器？执行了什么命令（需要容器层审计）？
- 哪个 IP 在暴力尝试认证？

注意：Kubernetes 审计只记录 API 请求，不记录容器内命令、文件访问或网络连接。容器内取证需要运行时安全工具（Falco/Tetragon）、eBPF、节点审计或应用日志。

### 23.10.5 告警规则示例

审计事件本身不直接暴露每一类资源的 Prometheus 指标，告警通常基于“审计日志 → SIEM/日志平台”的规则，而不是在 API Server 上做 PromQL 聚合。可用的 API Server 指标更偏整体健康，例如：

```promql
# 审计事件写入失败/丢弃（具体指标名以版本为准）
rate(apiserver_audit_error_total[5m]) > 0
```

更实际的做法是把审计日志送入 SIEM，用规则匹配：

- 非管理员读取 `secrets`。
- 创建/修改 `clusterrolebindings`。
- `pods/exec` 到生产 namespace。
- 来自非常用地理位置的 `system:anonymous` 请求。
- 短时间大量 403（权限探测）。

### 23.10.6 MiniK8s 的边界

MiniK8s 没有审计子系统。若要扩展，最小实现是：

- 在 `APIHandler._dispatch` 记录 user/verb/resource/namespace/name/status。
- 按 `AuditPolicy` 简化为 Metadata/Request 两级。
- 写入内存环形缓冲或文件，支持按时间/用户过滤。

这不是生产审计，但能演示“审计发生在 API 层、与认证/授权同链路”的契约。

## 23.11 常见错误

**给 `system:authenticated` 绑高权限**：所有登录用户都获得权限。

**用 Group 但 IdP 不返回 groups**：授权失效。

**RoleBinding 跨命名空间引用 Role**：不允许；Role 只能绑定本命名空间的 Role。

**resourceNames 用于 list**：list 不检查具体 name，权限可能不符合预期。

**忘记子资源**：有 `pods` 权限但没有 `pods/log`，`kubectl logs` 失败。

**ServiceAccount 共用**：一个被攻破，所有服务受影响。

**长期 token Secret**：泄露后长期有效。

## 23.12 MiniK8s 实操

创建 Role/RoleBinding：

```bash
python -m mini_k8s.cli apply -f examples/10-rbac.yaml
```

使用开发 token：

```bash
python - <<'PY'
from mini_k8s.client import ApiClient
client = ApiClient("http://127.0.0.1:8080", token="mini-dev-token")
print(client.can_i("get", "pods", namespace="team-dev"))
print(client.can_i("delete", "pods", namespace="team-dev"))
PY
```

MiniK8s 的 RBAC 是简化实现：

- 不支持 aggregationRule。
- 不支持 `bind`/`escalate` 的完整语义。
- 不做 SubjectAccessReview 的完整规则展开。
- 没有审计。

## 23.13 OIDC 落地与访问审查 API

OIDC 的配置项很少，但生产落地有一串容易踩的坑。

### token 校验到底校验什么

API Server 会检查：

- **签名**：token 必须由 issuer 的 JWKS 中对应 kid 的密钥签名；需要能访问 issuer 的 discovery/JWKS。
- **issuer**：必须与 `--oidc-issuer-url` 完全一致（包括结尾斜杠）。
- **audience**：必须包含 `--oidc-client-id`。
- **过期/生效时间**：`exp`/`nbf`，允许少量时钟偏差。
- **用户名/组**：按配置的 claim 提取；如果 IdP 不返回 groups，RBAC 的组规则全部失效。

常见故障：

- IdP 使用自签 CA，API Server 不信任 → 所有登录失败。
- issuer URL 内外网不一致（`https://auth.example.com` vs `https://auth.internal`）→ token 校验失败。
- 组 claim 嵌套在对象里，配置的 `--oidc-groups-claim` 取不到。
- token 过期后 kubectl 不自动刷新 → 配置 `exec` 插件或 `oidc` refresh。

### kubeconfig 的 exec 插件

不要在 kubeconfig 里放长期静态 token。推荐：

```yaml
users:
  - name: alice
    user:
      exec:
        apiVersion: client.authentication.k8s.io/v1
        command: oidc-login
        args: ["get-token", "--issuer-url=https://auth.example.com", "--client-id=kubernetes"]
```

kubectl 在每次请求前调用插件获取短期 token；插件负责浏览器登录/refresh。这样 token 不落盘，也支持 MFA。

### TokenReview 与 SubjectAccessReview

认证/授权 webhook 和排查都依赖这些 API：

```yaml
apiVersion: authentication.k8s.io/v1
kind: TokenReview
spec:
  token: "..."
  audiences: ["https://kubernetes.default.svc"]
```

返回 `status.authenticated`、`user.username/groups`。

```yaml
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: alice
  groups: ["developers"]
  resourceAttributes:
    namespace: team-a
    verb: get
    group: apps
    resource: deployments
```

返回 `status.allowed/reason`。常用变体：

- `SelfSubjectAccessReview`：`kubectl auth can-i get pods`。
- `SelfSubjectRulesReview`：列出当前用户在某 namespace 的规则。
- `LocalSubjectAccessReview`：指定 namespace 的审查。

### 模拟身份（impersonation）

管理员可以临时以他人身份操作：

```bash
kubectl auth can-i get secrets --as=alice --as-group=developers
kubectl get pods --as=system:serviceaccount:team-a:deployer
```

这需要 `impersonate` 权限，属于高权限；审计日志会记录真实用户和 impersonated 用户，必须开启审计。

### 最小权限的落地顺序

1. 先给团队一个 namespace 级 `view`/`edit`，不要直接 `cluster-admin`。
2. 用 `kubectl auth can-i --list` 和 SAR 验证实际权限。
3. 对 CI/Operator 使用独立 ServiceAccount + 精确 Role。
4. 对跨命名空间能力（Ingress、Gateway、CRD）单独评审。
5. 定期用工具（rbac-police、KubiScan）扫描高危绑定。

MiniK8s 支持 TokenReview、SubjectAccessReview 的核心字段和静态 token/HMAC SA token，但没有 OIDC/JWKS/impersonation；第 38 章展示其实现边界。

## 23.14 工作负载身份与云 IAM 联邦

人类用户用 SSO/OIDC 登录集群，工作负载（Pod）则需要另一种身份：它要访问对象存储、消息队列、数据库、密钥管理，但不应该携带长期 Access Key。把 Kubernetes ServiceAccount 与外部身份系统联邦，是平台安全的关键一环。

### 23.14.1 两种身份，两条链路

```text
人类身份：用户 → IdP（OIDC/SAML）→ kubectl → API Server
工作负载身份：Pod → ServiceAccount → 云 IAM/SPIFFE → 外部服务
```

不要把两者混为一谈：

- 人类身份关注登录、MFA、组、离职回收。
- 工作负载身份关注短期凭据、最小权限、自动轮换、跨集群可验证。

### 23.14.2 ServiceAccount token 的演进

传统方式：ServiceAccount 自动创建长期 Secret token，挂载到 Pod。问题是 token 长期有效、无法绑定 Pod、泄漏后难以吊销。

新方式：TokenRequest + projected volume：

```yaml
spec:
  serviceAccountName: app
  containers:
    - name: app
      image: example/app
      volumeMounts:
        - name: token
          mountPath: /var/run/secrets/tokens
          readOnly: true
  volumes:
    - name: token
      projected:
        sources:
          - serviceAccountToken:
              path: token
              audience: https://storage.example.com
              expirationSeconds: 3600
```

特点：

- 短期（默认 1 小时，可配置），自动轮换。
- 可指定 audience，token 只对特定服务有效。
- 绑定 Pod/Node（bound token），Pod 删除后失效。
- 不再需要长期 Secret；禁用 automount 可减少攻击面。

```yaml
automountServiceAccountToken: false
```

### 23.14.3 云 IAM 联邦的三种模式

**AWS IRSA / EKS Pod Identity**

- IRSA：ServiceAccount 通过 OIDC provider 与 IAM Role 关联，Pod 获取短期 STS 凭据。
- EKS Pod Identity：由 EKS 的 agent 注入凭据，减少 OIDC 配置。
- 关键：IAM trust policy 限制到特定 cluster/namespace/serviceaccount，避免过于宽松。

**GCP Workload Identity**

- Kubernetes ServiceAccount 映射到 Google Service Account。
- Pod 通过 metadata server 获取短期 token。
- 关键：GSA 的 IAM 权限、KSA 注解、Workload Identity Pool。

**Azure Workload Identity**

- 使用 OIDC issuer + federated credential，把 KSA 映射到 Azure AD 应用/托管身份。
- 关键：federated credential 的 subject 必须精确到 namespace/serviceaccount。

共同点：

- 都依赖集群的 OIDC issuer 或等价机制。
- 都把“Pod 身份”换成“短期云凭据”。
- 都需要限制 trust policy 的 subject，避免同集群其他 namespace 冒用。

### 23.14.4 SPIFFE/SPIRE

SPIFFE 定义了一套跨平台的工作负载身份：

- **SPIFFE ID**：`spiffe://trust-domain/path`。
- **SVID**：短期证书/Token，证明工作负载身份。
- **SPIRE**：签发与轮换 SVID 的实现，支持 Kubernetes、VM、裸金属。

适用场景：

- 多集群/混合云统一身份。
- 服务间 mTLS（与服务网格互补/替代）。
- 非 Kubernetes 工作负载也需要同一身份体系。

代价：

- 需要运行 SPIRE Server/Agent，理解 attestation。
- 证书轮换、信任域、跨集群联邦要设计。
- 与服务网格的 identity 可能重叠，需统一。

### 23.14.5 跨集群与多租户

- 每个集群应有独立 OIDC issuer；跨集群信任通过 federation 建立，而不是共享密钥。
- trust policy 的 subject 要包含 cluster/namespace/serviceaccount，防止“同名字符串”冒用。
- 多租户集群中，租户的 ServiceAccount 不应能获取其他租户的云凭据。
- 对高权限角色（能读所有 Secret、能创建 Pod）做额外审批与审计。
- 短期凭据也要有最小权限；短期不等于低风险。

### 23.14.6 落地顺序

1. 关闭不必要的 `automountServiceAccountToken`。
2. 为每个工作负载建独立 ServiceAccount，绑定最小 RBAC。
3. 用 TokenRequest/projected token 替代长期 Secret。
4. 接入云 IAM 联邦，删除静态 Access Key。
5. 用 policy/CI 检查 trust policy 与 RBAC 的高危组合。
6. 开启审计，监控异常凭据使用（异地、异常 API、权限探测）。
7. 定期轮换/撤销，演练凭据泄漏响应。

### 23.14.7 常见错误

- 所有 Pod 共用一个 `default` ServiceAccount，权限无法区分。
- trust policy 写成 `repo:org/*` 或过宽的 subject，任何工作负载都能AssumeRole。
- 把云凭据写进 Secret/环境变量，绕过联邦。
- 只限制 Kubernetes RBAC，不限制云 IAM；Pod 仍可读整个桶。
- 忽略 token audience，token 可被其他服务重放。
- 多集群共享同一 OIDC issuer/密钥，故障与越权影响面扩大。

### 23.14.8 MiniK8s 的边界

MiniK8s 实现了 ServiceAccount、TokenRequest、TokenReview 和静态 HMAC token，足以观察“签发—挂载—验证”的链路；它不实现 projected volume 轮换、OIDC/JWKS、云 IAM 联邦或 SPIFFE。真实工作负载身份必须在目标集群与云平台上验证。

## 本章小结

- 认证回答“你是谁”，授权回答“你能做什么”，准入回答“对象是否合法”。
- RBAC 用 Role/ClusterRole + Binding 表达权限，默认拒绝。
- ServiceAccount 是 Pod 身份，优先使用短期 TokenRequest 和工作负载身份。
- “能创建 Pod”可能等价于高权限，必须结合 PSA/策略限制。
- 最小权限、命名空间隔离、审计是生产基线。
- 工作负载身份要把 ServiceAccount 与云 IAM/SPIFFE 联邦，避免长期 Access Key；trust policy 的 subject 必须精确到 cluster/namespace/SA。
- 审计策略按资源/动词分级，高危写操作保留 RequestResponse，日志集中防篡改并配套取证流程。

## 延伸阅读

- [第 24 章 准入控制与供应链安全](24-admission-supply-chain.md)
- [第 26 章 多租户与资源治理](26-multitenancy-governance.md)
- [第 38 章 网络、存储与安全实现](38-network-storage-security.md)
- [附录 H 安全加固基线](../appendices/H-security-baseline.md)
- [附录 S PKI、证书与身份生命周期](../appendices/S-pki-and-certificates.md)：CSR、kubelet TLS bootstrap、ServiceAccount token 与轮换。
