# 第 24 章 准入控制与供应链安全

## 24.1 准入控制的位置

```text
请求 → 认证 → 授权 → Mutating Admission → 对象校验 → Validating Admission → 持久化
```

准入控制能：

- 注入默认值（LimitRange、sidecar）。
- 修改对象（标签、镜像、securityContext）。
- 拒绝不符合策略的对象。
- 调用外部 webhook。

它不能：

- 绕过认证/授权。
- 修改已持久化对象。
- 保证运行时安全。

## 24.2 Mutating 与 Validating

### MutatingAdmissionWebhook

可以修改对象。典型：

- 注入 sidecar（服务网格）。
- 注入标签/注解。
- 设置默认 securityContext。
- 改写镜像仓库。

顺序：所有 mutating webhook 执行完，才做 schema 校验和 validating。

完整配置示例：

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: sidecar-injector.example.com
webhooks:
  - name: sidecar-injector.example.com
    admissionReviewVersions: [v1]
    sideEffects: None
    failurePolicy: Fail
    reinvocationPolicy: IfNeeded
    timeoutSeconds: 5
    namespaceSelector:
      matchLabels: {sidecar-injection: enabled}
    objectSelector:
      matchLabels: {sidecar-injection: enabled}
    rules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE"]
        resources: ["pods"]
    clientConfig:
      service:
        namespace: injector
        name: injector
        path: /mutate
```

关键字段：

| 字段 | 作用 | 注意 |
|---|---|---|
| `admissionReviewVersions` | 支持的 AdmissionReview 版本 | v1 必填 |
| `sideEffects` | webhook 是否有副作用 | `None` 或 `NoneOnDryRun`；dry-run 安全的前提 |
| `failurePolicy` | 不可用时 `Fail` 还是 `Ignore` | 安全与可用性的取舍 |
| `matchPolicy` | API 版本匹配用 `Equivalent` 还是 `Exact` | 默认 `Equivalent`，避免漏掉版本转换 |
| `timeoutSeconds` | 调用超时 | 默认 10 秒，最大 30 秒 |
| `reinvocationPolicy` | 后续 webhook 改动对象后是否再次调用 | `IfNeeded` 用于多注入器场景 |
| `namespaceSelector` / `objectSelector` | 只对匹配对象调用 | 降低延迟与爆炸半径 |
| `matchConditions` | CEL 条件过滤（1.30 GA） | 比 selector 更灵活；注意 CEL 成本预算与失败语义 |

顺序与语义：

1. 所有 mutating webhook 按配置顺序执行（顺序不应被依赖）。
2. 全部完成后做 schema 校验。
3. 然后执行 validating webhook。
4. 如果后面的 mutating webhook 又改了对象，`reinvocationPolicy: IfNeeded` 会让前面的 webhook 再跑一次。

webhook 必须**幂等**：同一个对象被调用多次不能重复注入 sidecar。不要依赖“我只被调用一次”。

风险：

- 修改后的对象可能不符合用户预期。
- webhook 失败可能导致创建失败（failurePolicy）。
- 顺序和幂等性难保证。
- 超时/不可用会直接拖慢或阻塞所有匹配的写入。

### ValidatingAdmissionWebhook

只能允许/拒绝，不能修改。典型：

- 策略引擎（OPA Gatekeeper、Kyverno）。
- 镜像签名校验。
- 禁止 latest 标签。
- 要求资源 limits。
- 限制 hostPath/privileged。

配置：

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: policy.example.com
webhooks:
  - name: policy.example.com
    admissionReviewVersions: [v1]
    sideEffects: None
    failurePolicy: Fail
    clientConfig:
      service:
        namespace: policy-system
        name: policy-webhook
        path: /validate
    rules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["pods"]
```

`failurePolicy`：

- `Fail`：webhook 不可用时拒绝请求（安全但可用性风险）。
- `Ignore`：webhook 不可用时放行（可用但安全风险）。

生产策略 webhook 必须高可用、低延迟，否则会拖垮整个集群的写入。

## 24.3 内置准入插件

除了 webhook，API Server 还有内置插件：

- NamespaceLifecycle：禁止在终止的 namespace 创建对象。
- LimitRanger：应用 LimitRange 默认值/校验。
- ResourceQuota：配额检查。
- ServiceAccount：注入默认 SA。
- PodSecurity：Pod Security Admission。
- DefaultStorageClass：默认 StorageClass。
- Priority：解析 PriorityClass。
- TaintNodesByCondition：节点条件污点（历史）。

MiniK8s 的 `AdmissionChain` 实现了类似集合：

- namespace 存在性。
- Pod/Deployment/Service 默认值。
- LimitRange。
- ResourceQuota。
- Pod Security（baseline/restricted 简化）。
- CRD schema 校验。
- 名称/selector/资源校验。

## 24.4 Pod Security Admission

Pod Security Standards 三个级别：

- **Privileged**：无限制。
- **Baseline**：防止已知提权。
- **Restricted**：最强，要求非 root、禁止提权、drop capabilities、seccomp 等。

命名空间标签：

```yaml
metadata:
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: v1.31
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

模式：

- `enforce`：拒绝违规 Pod。
- `audit`：记录审计事件。
- `warn`：返回警告。

Restricted 的关键要求：

- `runAsNonRoot: true`。
- `allowPrivilegeEscalation: false`。
- `capabilities.drop: ["ALL"]`。
- `seccompProfile.type: RuntimeDefault` 或 Localhost。
- 禁止 privileged、hostNetwork、hostPID、hostIPC、hostPath。

MiniK8s 的 `_pod_security` 实现 baseline/restricted 的核心检查，测试：

- `test_pod_security_baseline_rejects_privileged`
- `test_pod_security_restricted_requires_non_root`
- `test_restricted_pod_can_be_admitted`
- `test_baseline_rejects_hostpath_volume`
- `test_restricted_rejects_container_level_run_as_non_root_false`

PSA 只是第一道门；运行时隔离、seccomp/AppArmor、沙箱运行时和节点加固见第 25 章。PSA 通过不代表容器安全。

## 24.5 策略引擎

Pod Security 只是基线。组织策略通常需要：

- 镜像仓库白名单。
- 必须设置 resources。
- 禁止 latest 标签。
- 要求标签（owner、cost-center）。
- 禁止 NodePort/LoadBalancer。
- 限制卷类型。
- 要求 NetworkPolicy。
- 命名规范。

工具：

- **OPA Gatekeeper**：Rego 策略，ConstraintTemplate + Constraint。
- **Kyverno**：YAML 策略，validate/mutate/generate/verifyImages。
- **Kubewarden**：WASM 策略。
- **Cloud Provider Policy**：GKE Policy Controller、Azure Policy。

示例（Kyverno）：

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-resources
spec:
  validationFailureAction: Enforce
  rules:
    - name: check-resources
      match:
        any:
          - resources:
              kinds: [Pod]
      validate:
        message: "CPU and memory requests are required"
        pattern:
          spec:
            containers:
              - resources:
                  requests:
                    cpu: "?*"
                    memory: "?*"
```

策略设计原则：

- 先 Audit，再 Enforce。
- 有明确的例外机制（namespace/label/annotation）。
- 策略即代码，纳入 Git 和测试。
- 监控拒绝率，避免误伤发布。

## 24.6 镜像与供应链

供应链攻击面：

- 基础镜像漏洞。
- 依赖包恶意代码。
- 构建环境被入侵。
- 镜像仓库被篡改。
- 部署时拉取到错误镜像。

防护层次：

1. **构建**：最小基础镜像、多阶段构建、锁依赖、可重复构建。
2. **扫描**：Trivy、Grype、Snyk、云厂商扫描。
3. **签名**：cosign/Notation，验证镜像来源。
4. **SBOM**：Syft、SPDX/CycloneDX，记录依赖。
5. **准入**：只允许签名/扫描通过的镜像。
6. **运行时**：Falco、Tetragon，检测异常行为。

### 签名验证

cosign：

```bash
cosign sign --key cosign.key registry.example.com/app@sha256:...
cosign verify --key cosign.pub registry.example.com/app@sha256:...
```

Kyverno `verifyImages` 或 OPA Gatekeeper 可以在准入时验证签名。

注意：

- 用 digest 而不是 tag。
- 签名密钥管理（KMS、HSM）。
- 密钥轮换/吊销。
- 离线/气隙环境的信任根。

### SLSA

SLSA（Supply-chain Levels for Software Artifacts）v1.0 把“构建完整性”分为 Build L1–L3（不再使用旧版的 L4 构建等级；更早的 L4 属于历史定义，部分要求被并入 L3/独立轨道）：

- **L1**：构建过程有文档、自动生成 provenance。
- **L2**：托管构建服务、签名 provenance。
- **L3**：加固构建环境、不可篡改的 provenance，构建平台与源码/制品隔离。

SLSA 还有独立的 **Source track**（源码完整性），不要和 Build track 混为一个等级。目标是证明“这个镜像确实由这个源码、这个构建系统产生”。落地通常结合 GitHub Actions、Tekton、Sigstore。

### 24.6.1 一条可落地的供应链流水线

```text
源码 → 构建（隔离/可复现）→ 生成 SBOM → 扫描 → 签名 → 推送 digest
  → 生成 provenance/SLSA → 准入验证签名/SBOM/扫描结果 → 部署
```

命令示例（工具名/参数以版本为准）：

```bash
# 1. 构建并生成 SBOM
syft registry.example.com/app:v1 -o spdx-json > app.spdx.json
# 2. 漏洞扫描（按严重级失败）
trivy image --exit-code 1 --severity HIGH,CRITICAL registry.example.com/app:v1
# 3. 用 digest 签名
cosign sign --key cosign.key registry.example.com/app@sha256:...
# 4. 生成/附加 provenance（取决于构建系统）
cosign attest --key cosign.key --predicate provenance.json registry.example.com/app@sha256:...
# 5. 部署前验证
cosign verify --key cosign.pub registry.example.com/app@sha256:...
```

### 24.6.5 Keyless 签名、OIDC 身份与透明度日志

长期密钥签名（`cosign.key`）在 CI 中很难安全保管。**Keyless** 模式用构建系统的 OIDC 身份（GitHub Actions、GitLab CI、SPIFFE）向 Fulcio 申请短期证书，签名后把证书/签名写入 Rekor 透明度日志，验证方按“身份 + issuer”而不是公钥来信任。

```bash
# 概念性流程：使用 OIDC 身份签名（具体参数以 cosign 版本为准）
cosign sign --identity-token "$OIDC_TOKEN" registry.example.com/app@sha256:...
# 验证：限定签发者与身份，而不是只验证“有签名”
cosign verify \
  --certificate-identity-regexp 'https://github.com/org/repo/.github/workflows/build.yml@refs/heads/main' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  registry.example.com/app@sha256:...
```

要点：

- 准入策略要校验 **identity + issuer**，否则任何能拿到 Fulcio 证书的人都能签名。
- 透明度日志提供“签名发生过”的公开证据；气隙环境要自建 Rekor/信任根，或改用离线密钥 + 严格轮换。
- Keyless 不是“无信任”：仍然依赖 Fulcio/Rekor/OIDC 的可用性与配置。

关键原则：

- **以 digest 为 identity**：tag 可变，digest 不可变。准入策略应校验 digest，而不是只校验 tag。
- **构建与部署分离**：构建系统持有签名私钥，集群只持有公钥/信任根。
- **扫描结果可追溯**：记录扫描器版本、数据库时间、漏洞白名单/例外。
- **失败策略明确**：高危漏洞阻断，还是告警？例外谁批准、多久复审。

### 24.6.2 准入验证：Kyverno/OPA/Gatekeeper

Kyverno 示例（概念）：

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-images
spec:
  validationFailureAction: Enforce
  rules:
    - name: check-signature
      match:
        any:
          - resources:
              kinds: ["Pod"]
      verifyImages:
        - imageReferences:
            - "registry.example.com/*"
          attestors:
            - entries:
                - keys:
                    publicKeys: |-
                      -----BEGIN PUBLIC KEY-----
                      ...
                      -----END PUBLIC KEY-----
```

注意：

- 策略要覆盖所有能创建 Pod 的资源（Deployment/StatefulSet/Job/CronJob/裸 Pod），否则可以绕过。
- init/ephemeral containers 的镜像也要校验。
- 镜像仓库代理/缓存可能改变 digest；确认信任链。
- 准入 webhook 故障时，`failurePolicy` 决定放行还是拒绝；生产通常选择“拒绝关键命名空间，放行系统组件”。

### 24.6.3 SLSA provenance 到底证明什么

Provenance 是一份“构建声明”：

- 源码仓库/commit。
- 构建系统/流水线。
- 构建参数/依赖。
- 产物 digest。

它能证明“产物与构建记录一致”，但不能证明：

- 源码没有后门（需要代码审计/审查）。
- 依赖没有漏洞（需要扫描）。
- 运行时没有被篡改（需要运行时安全）。

因此供应链安全是分层的：签名 + provenance + SBOM + 扫描 + 运行时检测，缺一不可。

### 24.6.4 常见失败模式

1. **只签 tag 不签 digest**：tag 被覆盖后签名失效/可被绕过。
2. **准入只覆盖 Deployment**：攻击者用裸 Pod/Job 绕过。
3. **签名密钥在 CI 明文**：构建环境被攻破即失去信任。
4. **扫描只在构建时做**：新 CVE 披露后，已部署镜像无人管。
5. **SBOM 生成了但没人用**：漏洞响应时无法快速定位受影响服务。
6. **例外永久化**：白名单没有到期/复审。
7. **webhook 故障导致全集群无法部署**：没有分级 failurePolicy/应急路径。

## 24.7 Secret 与配置安全

- Secret 加密存储（KMS）。
- 外部密钥管理（Vault、云 KMS）。
- 短期凭据/Workload Identity。
- 禁止把 Secret 写入日志/镜像。
- 限制 `get secrets` RBAC。
- 审计 Secret 访问。

## 24.8 运行时安全

准入只控制“部署什么”，运行时控制“运行中做什么”：

- seccomp/AppArmor/SELinux。
- 只读根文件系统。
- 非 root。
- 禁止特权/提权。
- 运行时检测（Falco/Tetragon）。
- 网络策略。
- 文件完整性监控。

运行时安全工具通常用 eBPF/kprobe 检测：

- 异常 exec。
- 敏感文件访问。
- 反向 shell。
- 容器逃逸尝试。
- 加密货币挖矿。

运行时隔离、节点加固、取证与响应的完整讨论见第 25 章。

## 24.9 准入 webhook 的高可用

webhook 是关键路径：

- 多副本、反亲和、PDB。
- 低延迟（超时通常 10–30 秒，实际要更短）。
- 证书轮换（cert-manager）。
- 失败策略与熔断。
- 监控请求量/延迟/拒绝率。
- 避免 webhook 调用外部慢服务。

事故模式：

- webhook 服务挂了，failurePolicy=Fail 导致整个集群无法创建 Pod。
- webhook 证书过期，所有请求失败。
- 策略误伤系统组件（kube-system），导致控制面故障。
- webhook 递归调用自身。

## 24.10 MiniK8s 的边界

MiniK8s 实现内置准入，不调用外部 webhook：

- 默认值、校验、Quota、PSA、CRD schema。
- 没有 MutatingAdmissionWebhook/ValidatingAdmissionWebhook 执行。
- 没有镜像签名验证。

第 40 章说明如何替换为真实 webhook。

## 24.11 ValidatingAdmissionPolicy 与 CEL

不是所有策略都需要一个 webhook。Kubernetes 从 1.30 起稳定支持 **ValidatingAdmissionPolicy（VAP）**，用 CEL 在 API Server 内做校验，避免外部服务带来的延迟和可用性风险。

### 基本对象

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: require-resources
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["pods"]
  validations:
    - expression: |
        object.spec.containers.all(c,
          has(c.resources) && has(c.resources.requests) &&
          has(c.resources.requests.cpu) && has(c.resources.requests.memory))
      message: "all containers must set CPU and memory requests"
```

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: require-resources-binding
spec:
  policyName: require-resources
  validationActions: [Deny]
  matchResources:
    namespaceSelector:
      matchLabels:
        policy.example.com/enforce: "true"
```

### CEL 的优势

- **无外部调用**：校验在 API Server 进程内完成，没有网络跳转。
- **类型化**：基于 OpenAPI schema，字段错误在策略编写/加载时暴露。
- **可审计**：策略是 API 对象，能纳入 GitOps。
- **参数化**：`paramKind` 引用 ConfigMap/CRD，避免把值硬编码进表达式。
- **成本可控**：有表达式预算/限制，避免慢策略拖垮 API Server。

### 适用与不适用

适合：

- 必填字段/取值范围/标签规范。
- 禁止 privileged/hostPath/latest 标签。
- 资源 requests/limits 检查。
- 命名规范、副本数上限。

不适合：

- 需要调用外部系统（镜像签名验证、CMDB 查询）。
- 需要修改对象（VAP 只校验；变更用 MutatingAdmissionPolicy 或 webhook）。
- 非常复杂的跨对象/跨集群逻辑。

### 24.11.1 MutatingAdmissionPolicy：用 CEL 做无 webhook 变更

`MutatingAdmissionPolicy`（1.32 alpha，1.34 beta，1.36 GA）是 VAP 的“变更”对应物：用 CEL 生成 apply configuration 或 JSONPatch，在 API Server 内完成默认值/注入/标签补齐，不再需要维护一个高可用 webhook。

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingAdmissionPolicy
metadata:
  name: add-team-label
spec:
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE"]
        resources: ["pods"]
  mutations:
    - patchType: ApplyConfiguration
      applyConfiguration:
        expression: |
          Object{
            metadata: Object.metadata{
              labels: {"platform.example.com/managed": "true"}
            }
          }
```

适合：补标签/注解、注入 sidecar、设置 securityContext、按 namespace 标准化字段。
不适合：需要外部系统、复杂分支/循环、跨对象查询。与 webhook 的顺序/冲突要显式设计；默认值被覆盖或重复注入是常见事故。

### 与 webhook 的关系

- 先用 VAP/MAP 覆盖静态校验与纯字段变更，减少 webhook 调用与高可用负担。
- 签名验证、动态白名单等仍需 webhook/策略引擎。
- 注意 `failurePolicy`：`Fail` 更安全，但策略表达式错误可能导致所有创建失败；先在 audit 模式验证。
- 策略要排除 `kube-system` 等关键命名空间，避免自锁。

### 调试

```bash
kubectl get validatingadmissionpolicy
kubectl get validatingadmissionpolicybinding
kubectl apply --dry-run=server -f pod.yaml   # 触发策略但不落库
kubectl get events --field-selector reason=FailedCreate
```

MiniK8s 只实现内置校验，不执行 CEL/webhook；VAP 是真实集群中“把策略前移到 API Server”的重要演进。

## 本章小结

- 准入控制是策略执行点，分为 mutating 和 validating。
- Pod Security 提供基线，组织策略需要 OPA/Kyverno 等。
- 供应链安全需要构建、扫描、签名、SBOM、准入、运行时多层配合。
- webhook 是高可用关键路径，必须防止它拖垮集群。
- ValidatingAdmissionPolicy / MutatingAdmissionPolicy + CEL 可以把静态校验与纯字段变更前移到 API Server，减少 webhook 依赖；签名/外部查询仍需 webhook。

## 延伸阅读

- [第 23 章 认证、授权与 RBAC](23-authn-authz-rbac.md)
- [第 25 章 运行时安全与节点加固](25-runtime-security-node-hardening.md)
- [第 26 章 多租户与资源治理](26-multitenancy-governance.md)
- [第 42 章 云原生未来方向](42-future.md)
- [附录 H 安全加固基线](../appendices/H-security-baseline.md)
- [附录 L 云原生生态地图](../appendices/L-ecosystem-map.md)：策略、扫描、签名、运行时检测的选型。
- [附录 Q 扩展点与交付物开发实战](../appendices/Q-extension-development.md)：webhook 的 Go 实现、TLS/failurePolicy 与自举问题。
