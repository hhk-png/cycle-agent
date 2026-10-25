# 第 14 章 安全：RBAC、网络、Secret、mTLS 与多租户

Ray 集群能执行任意 Python 代码，这使它天然是一个高权限工作负载。KubeRay 的安全设计必须同时覆盖控制面（operator/CRD）和数据面（Ray Pod）。本章给出一套可落地的分层模型。

## 14.1 威胁模型

| 威胁 | 例子 | 缓解 |
|---|---|---|
| 租户越权 | 团队 A 读取团队 B 的 Ray 集群 | namespace + RBAC + NetworkPolicy |
| 容器逃逸 | 用户 Pod 提权到节点 | Pod Security、seccomp、非 root |
| 凭据泄漏 | 环境变量/日志暴露 token | Secret、最小权限、日志脱敏 |
| 横向移动 | 被攻陷的 worker 访问其他服务 | NetworkPolicy、mTLS |
| 镜像供应链 | 恶意基础镜像 | 镜像签名、SBOM、私有仓库 |
| Dashboard 暴露 | 公网访问 8265 | Ingress TLS、认证、IP allowlist |
| Operator 权限过大 | 一个 bug 删除全集群资源 | 最小 RBAC、分 namespace |
| 拒绝服务 | 大量 Pod/Job 打爆 API | ResourceQuota、LimitRange、Kueue |

## 14.2 RBAC

### 14.2.1 Operator 的权限

KubeRay operator 需要：

- `ray.io` CRD 全量读写；
- Pod/Service/Secret/ConfigMap/PVC/ServiceAccount；
- Job；读取 EndpointSlice（判断 Serve 后端；EndpointSlice 本身由 Kubernetes 维护）；
- Role/RoleBinding（给 autoscaler）；
- Node 只读；
- 可选：NetworkPolicy、Ingress、Gateway API、cert-manager。

生产建议：

- 用 ClusterRole 管理 CRD，但对租户 namespace 的敏感资源（Secret）要评估；
- 如果 operator 只服务一个 namespace，用 Role/RoleBinding 替代 ClusterRole；
- 禁止 operator 拥有 `cluster-admin`；
- 定期审计 `kubectl auth can-i --list`。

### 14.2.2 Autoscaler 的权限

每个 RayCluster 会创建：

- ServiceAccount `<cluster>-autoscaler`；
- Role：patch RayCluster/status、list/delete Pod；
- RoleBinding。

只授予它需要的 namespace/资源。不要复用 operator 的 ServiceAccount。

### 14.2.3 租户权限

租户应只能：

- 在自己的 namespace 创建/修改 RayCluster/RayJob/RayService/RayCronJob；
- 读取自己的 Pod/Service/Event；
- 不能读取 Secret、不能改 RBAC、不能跨 namespace。

示例 Role（概念）：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: team-a
  name: ray-user
rules:
  - apiGroups: [ray.io]
    resources: [rayclusters, rayjobs, rayservices, raycronjobs]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [""]
    resources: [pods, pods/log, services, events]
    verbs: [get, list, watch]
```

## 14.3 Pod Security

Ray 镜像默认以 root 运行（取决于镜像）。生产建议：

```yaml
spec:
  template:
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: ray
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: [ALL]
```

注意：

- Ray 需要写 `/tmp/ray`、`/dev/shm`、工作目录；
- 不要盲目 `readOnlyRootFilesystem: true`；
- GPU 容器可能需要额外的 capabilities/设备挂载；
- 使用 Pod Security Admission（restricted/baseline）时先在测试集群验证。

## 14.4 NetworkPolicy

KubeRay v1.7 的 `spec.networkPolicy` 可以自动创建 head/worker 的 NetworkPolicy：

```yaml
spec:
  networkPolicy:
    mode: DenyAll
    head:
      ingressRules:
        - from:
            - namespaceSelector:
                matchLabels:
                  kubernetes.io/metadata.name: monitoring
          ports:
            - port: 8080
      egressRules:
        - to:
            - namespaceSelector:
                matchLabels:
                  kubernetes.io/metadata.name: kube-system
          ports:
            - port: 53
              protocol: UDP
            - port: 53
              protocol: TCP
        - to:
            # 按集群实际 API Server Service/Endpoint 调整
            - namespaceSelector:
                matchLabels:
                  kubernetes.io/metadata.name: default
          ports:
            - port: 443
              protocol: TCP
    worker:
      egressRules:
        - to:
            - namespaceSelector:
                matchLabels:
                  kubernetes.io/metadata.name: kube-system
          ports:
            - port: 53
              protocol: UDP
            - port: 53
              protocol: TCP
        - to:
            - namespaceSelector:
                matchLabels:
                  kubernetes.io/metadata.name: default
          ports:
            - port: 443
              protocol: TCP
```

要点：

- `DenyAll` 只自动放行 KubeRay 管理的集群内流量（head/worker 之间），不自动放行 DNS、API Server 或其他 namespace；
- DNS egress 不会自动放行；head 和 worker 都需要；
- autoscaler v2、token auth/K8s RBAC 委托需要访问 kube-apiserver（443），少了这条会表现为 403/超时；
- RayJob submitter 需要访问 head Dashboard（8265）；
- 外部 Dashboard/Client 访问要显式放行；
- NetworkPolicy 创建与 Pod 启动之间有竞态窗口，先建策略再建负载，或接受短暂的空窗；
- 多租户建议默认拒绝，按需加规则；
- 需要 CNI 支持 NetworkPolicy（Calico、Cilium 等）。

## 14.5 Secret 与凭据

### 14.5.1 不要写进 YAML

- Redis 密码；
- 对象存储 AK/SK；
- Ray auth token；
- 模型 API key。

用 Secret + `valueFrom`：

```yaml
env:
  - name: REDIS_PASSWORD
    valueFrom:
      secretKeyRef:
        name: redis-password
        key: password
```

### 14.5.2 Secret 的边界

- Secret 默认只做 base64，不是加密；
- 开启 etcd encryption at rest；
- 限制 `get/list/watch secrets` 的 RBAC；
- 不要把 Secret 挂载到不需要的 Pod；
- 日志中不要打印 env/Secret。

## 14.6 Ray Token Auth

### 14.6.1 配置

```yaml
spec:
  authOptions:
    mode: token
    secretName: ray-cluster-token
```

Secret：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: ray-cluster-token
type: Opaque
stringData:
  auth_token: "replace-with-a-long-random-token"
```

KubeRay 会把 token 注入 Ray 容器，Ray 的 Dashboard/Client 要求认证。客户端需要：

```bash
export RAY_AUTH_MODE=token
export RAY_AUTH_TOKEN='<token>'
ray job submit --address http://...
```

（不同 Ray 版本的 CLI/SDK 认证参数可能不同；以 Ray token auth 文档为准。）

### 14.6.2 Kubernetes RBAC 委托（v1.6+）

```yaml
spec:
  authOptions:
    mode: token
    enableK8sTokenAuth: true
```

- Ray 使用 Kubernetes ServiceAccount token 认证；
- Raylet 的 ServiceAccount 需要两组权限：
  - `authentication.k8s.io/tokenreviews` 的 `create`；
  - `authorization.k8s.io/subjectaccessreviews` 的 `create`；
  - 以及对目标 `rayclusters.ray.io` 的读写/自定义 `ray:write` verb（按版本和授权模型而定）；
- 客户端用 `Authorization: Bearer <serviceaccount-token>`（Ray API）访问；
- 适用于 Ray 2.55+、KubeRay v1.6+；
- 部分 Ray 版本曾要求 ServiceAccount token 的 audience 为 `ray.io`，随后又回退；以你使用的 Ray 版本为准，必要时在 projected volume 里配置 `audience`；
- 目前不支持 RayJob/RayService 的同等委托。

一个最小 ClusterRole：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ray-authenticator
rules:
  - apiGroups: ["authentication.k8s.io"]
    resources: ["tokenreviews"]
    verbs: ["create"]
  - apiGroups: ["authorization.k8s.io"]
    resources: ["subjectaccessreviews"]
    verbs: ["create"]
```

把它绑定到 Ray 集群使用的 ServiceAccount；否则 Ray 无法校验 token，表现为所有请求 401/403。好处：统一身份、审计、轮换；代价：依赖 K8s API 可用性，且 K8s API 抖动会直接影响 Ray 认证。

### 14.6.3 完整的最小 RBAC 示例

Operator（namespace 级）：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: kuberay-operator
  namespace: kuberay-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: kuberay-operator
  namespace: team-a
rules:
  - apiGroups: ["ray.io"]
    resources: ["rayclusters", "rayjobs", "rayservices", "raycronjobs"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["ray.io"]
    resources: ["rayclusters/status", "rayjobs/status", "rayservices/status", "raycronjobs/status"]
    verbs: ["get", "update", "patch"]
  - apiGroups: [""]
    resources: ["pods", "services", "secrets", "configmaps", "persistentvolumeclaims", "serviceaccounts", "events"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list", "watch", "create", "delete"]
  - apiGroups: ["discovery.k8s.io"]
    resources: ["endpointslices"]
    verbs: ["get", "list", "watch", "create", "update", "delete"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses", "networkpolicies"]
    verbs: ["get", "list", "watch", "create", "update", "delete"]
  - apiGroups: ["gateway.networking.k8s.io"]
    resources: ["gateways", "httproutes"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

租户角色：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ray-user
  namespace: team-a
rules:
  - apiGroups: ["ray.io"]
    resources: ["rayclusters", "rayjobs", "rayservices", "raycronjobs"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["pods", "pods/log", "services", "events"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ray-user
  namespace: team-a
subjects:
  - kind: Group
    name: team-a
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: ray-user
  apiGroup: rbac.authorization.k8s.io
```

注意：

- 不给租户 `secrets` 的读权限；
- 不给 `pods/exec`（等于容器内任意命令）；
- 不给 `rayclusters/finalizers` 的写权限；
- 如果使用 Kueue，租户只需要 `rayjobs`，由 Kueue 创建 Workload。

### 14.6.4 Pod Security Admission

```bash
kubectl label namespace team-a \
  pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/warn=restricted
```

Ray 镜像可能需要放宽：

- `runAsNonRoot: true` + 非 root 用户；
- `seccompProfile: RuntimeDefault`；
- `allowPrivilegeEscalation: false`；
- `capabilities.drop: ["ALL"]`；
- 需要写 `/tmp/ray`、`/dev/shm`、工作目录。

先在 audit/warn 模式跑一段时间，再 enforce。

### 14.6.5 默认拒绝的 NetworkPolicy

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: team-a
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns
  namespace: team-a
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
```

然后按需放行：head 6379、Serve 8000、对象存储、镜像仓库、监控。不要忘记显式 DNS。

### 14.6.6 凭据轮换

- Ray auth token：生成新 Secret -> 滚动重启 head/worker -> 更新客户端；
- Redis 密码：先让 Redis 接受新旧密码，再更新 Secret，最后移除旧密码；
- mTLS 证书：cert-manager 自动轮换，确认 SAN/挂载更新；
- 对象存储密钥：用短期 token/Workload Identity，避免长期 AK/SK；
- 轮换要有回滚窗口，不要把旧 token 立即删掉。

## 14.7 mTLS（v1.7 alpha）

```yaml
spec:
  tlsOptions:
    enabled: true
```

需要：

- `RayClusterMTLS` feature gate；
- cert-manager；
- 自签 CA、head/worker 证书；
- 证书 SAN 随 Pod IP 更新；
- Ray 容器注入 `RAY_USE_TLS` 和证书路径。

mTLS 保护 Ray 内部 gRPC/对象传输。外部入口仍需 Ingress/Gateway TLS。

cert-manager 侧的典型对象（name 以 operator 实际生成为准）：

```yaml
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: ray-ca
  namespace: team-a
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: ray-head
  namespace: team-a
spec:
  secretName: ray-head-tls
  issuerRef:
    name: ray-ca
  dnsNames:
    - raycluster-sample-head-svc
    - raycluster-sample-head-svc.team-a.svc
  duration: 720h
  renewBefore: 168h
```

证书 SAN 要覆盖 Service DNS 和 Pod IP；Pod 重建后 IP 变化，证书需要重新签发。`RayClusterMTLS` 仍是 alpha，升级前先在测试集群验证 cert-manager 版本/轮换行为。

## 14.8 供应链安全

- 使用官方/内部可信镜像；
- 固定 digest（`image@sha256:...`）而不是 floating tag；
- 镜像签名（cosign/Notation）；
- SBOM（Syft/Trivy）；
- 私有镜像仓库 + 扫描；
- 禁止 `latest`；
- 定期重建镜像（基础镜像 CVE）；
- 限制 Pod 拉取镜像的 Secret 权限。

## 14.9 多租户

### 14.9.1 隔离维度

| 维度 | 手段 |
|---|---|
| 命名空间 | 每团队一个 namespace |
| 计算 | ResourceQuota、LimitRange、Kueue |
| 网络 | NetworkPolicy、mTLS |
| 存储 | PVC/StorageClass、对象存储前缀 |
| 身份 | ServiceAccount、RBAC |
| 镜像 | 私有仓库、ImagePullSecret |
| 调度 | nodeSelector/taint/toleration、优先级 |
| 可观测性 | 日志/指标标签隔离 |

### 14.9.2 共享 operator 的风险

一个 operator 管理所有 namespace 时：

- 一个租户的 CR 可能触发大量 Pod；
- operator 的 bug 影响所有租户；
- Secret 读取权限是跨 namespace 的。

缓解：

- ResourceQuota；
- Kueue 队列；
- 每租户一个 operator（成本高）：用 `watchNamespace`/`singleNamespaceInstall` 把每个 operator 限制在租户 namespace，配合 namespace 级 Role/RoleBinding；否则“每租户一个 operator”仍然会 watch 全集群，隔离是假的（见第 3.2.3 节、附录 G.2.2）；
- 限制 CR 字段（admission policy）；
- 审计。

## 14.10 审计与合规

- 开启 Kubernetes audit log；
- 记录谁创建/修改/删除了 Ray CR；
- 记录 Secret 访问；
- 记录 operator 的 RBAC 变更；
- 保留 Ray Job 的提交者/镜像/entrypoint；
- 对高风险操作（删除集群、读取 Secret）告警。

## 14.11 MiniKubeRay 的安全模拟

- API Server 支持 Bearer token（`--token`）；
- `--read-only` 拒绝写操作；
- 不实现 RBAC/NetworkPolicy 数据面，但会创建/校验 NetworkPolicy 对象；
- auth secret 会被创建，head env 会引用；
- mTLS 字段会被校验/建模，但不签发证书。

这些设计让安全相关的**控制面流程**可测试，而不是假装实现真实加密。

## 14.12 安全清单

- [ ] operator 不使用 cluster-admin
- [ ] 每租户 namespace + ResourceQuota
- [ ] Secret 访问最小化
- [ ] NetworkPolicy 默认拒绝
- [ ] Pod 非 root + seccomp + drop capabilities
- [ ] 镜像固定 digest + 扫描/签名
- [ ] Dashboard/Client 不暴露公网
- [ ] token/mTLS 按需启用
- [ ] 审计日志开启
- [ ] 定期轮换凭据

## 14.13 STRIDE 威胁建模

把威胁按 STRIDE 分类，能避免“只想到 RBAC”：

| 威胁 | 在 KubeRay 中的表现 | 缓解 |
|---|---|---|
| **S**poofing（伪装） | 伪造 worker 加入集群、伪造 Dashboard 客户端 | mTLS、token auth、ServiceAccount |
| **T**ampering（篡改） | 修改 CR/Pod 模板、替换镜像、篡改 checkpoint | RBAC、准入策略、镜像签名、digest |
| **R**epudiation（抵赖） | 无法证明谁提交/删除了作业 | audit log、RayJob metadata、GitOps |
| **I**nformation disclosure（信息泄露） | Secret/日志/prompt/模型权重泄露 | Secret、日志脱敏、NetworkPolicy、最小权限 |
| **D**enial of service（拒绝服务） | 大量 Pod/Job 打爆 API/GPU/节点 | ResourceQuota、Kueue、limit/priority |
| **E**levation of privilege（提权） | 容器逃逸、hostPath、privileged、RBAC 过大 | Pod Security、seccomp、drop capabilities、最小 RBAC |

### 14.13.1 按资产梳理

| 资产 | 谁需要访问 | 主要威胁 |
|---|---|---|
| Ray CR | 租户、operator、GitOps | 篡改/越权删除 |
| Pod/Service | operator、kubelet | 篡改模板、SSRF |
| Secret | Ray Pod、operator | 读取/泄漏 |
| 对象存储 | Ray 作业、History Server | 越权读写/数据外泄 |
| Dashboard/Serve | 用户、Gateway | 未授权访问/DDoS |
| 镜像仓库 | 节点、租户 | 供应链投毒 |
| 审计日志 | 平台/安全 | 篡改/缺失 |

### 14.13.2 攻击路径示例

```text
租户可以创建任意 Pod template
  -> hostPath 挂载节点根目录
  -> 读取 kubelet/其他 Pod 的凭据
  -> 横向移动到其他 namespace
```

缓解不是“禁止 hostPath”一条，而是：

- 准入策略（Kyverno/Gatekeeper）禁止 hostPath/privileged；
- Pod Security Admission；
- 独立节点池；
- NetworkPolicy；
- 审计与告警。

### 14.13.3 安全设计的最小闭环

```text
资产 -> 威胁 -> 控制 -> 检测 -> 响应
```

只有“控制”没有“检测/响应”，安全事件不会被发现；只有“检测”没有“控制”，只能事后追责。

## 14.14 小结

- Ray 是高权限工作负载，安全要覆盖控制面和数据面。
- RBAC 最小化、namespace 隔离、NetworkPolicy、Secret 管理是基础。
- token auth 与 K8s RBAC 委托统一身份；mTLS 保护 Pod 间通信。
- 供应链和多租户是平台级问题，不能只靠 KubeRay 字段解决。
- 安全是持续过程：审计、轮换、扫描、演练。

准入策略、CEL、Kyverno/Gatekeeper、SBOM/签名/SLSA 与多租户护栏的工程化落地见[第 49 章](kuberay教程-49-平台治理-准入控制、策略即代码与供应链安全.md)。

下一章性能、容量与成本。

→ [第 15 章 性能、容量与成本](kuberay教程-15-性能、容量与成本.md)
