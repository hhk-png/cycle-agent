# 第 32 章 GitOps 与渐进式交付

> 第 5 章介绍了声明式工作流、Kustomize/Helm 和 GitOps 的基本概念。本章把它做成一条可运营的交付流水线：Git 中的期望状态如何安全地到达多个集群，如何做灰度、验证、回滚，以及如何避免“GitOps 只是把 kubectl apply 换成机器人”。

## 32.1 从 CI 到 CD：推与拉

传统 CD 是“推”模型：

```text
CI 构建镜像 → CI 持有集群凭据 → CI 执行 kubectl/helm → 集群
```

问题：

- CI 需要长期集群凭据，泄露面大。
- 集群状态与流水线日志脱节，难以知道“现在到底部署了什么”。
- 多集群、多环境需要复制凭据和脚本。
- 回滚依赖重新跑流水线，容易与当前状态不一致。

最常见的 GitOps 实现是“拉”模型，但并非所有实现都如此：

```text
开发者提交 → Git 仓库（期望状态）→ 集群内 agent 拉取 → 调谐 → 集群
```

Flux 是每集群拉取；Argo CD 默认是集中式推模型（见 32.1.1）。两者共享“Git 是事实来源 + 持续调谐”的核心，不要用“拉/推”一刀切。

特点：

- **Git 是唯一事实来源**：部署什么、配置什么、版本是什么，都在 Git 历史里。
- **持续调谐**：漂移会被发现并纠正（或至少告警）。
- **回滚 = revert commit**：审计、评审、权限都复用 Git 流程。

GitOps 不等于“有 Git 仓库”。它至少需要：声明式配置、版本化、自动调谐。

### 32.1.1 拉模型、集中式推模型与凭据边界

“GitOps = 集群内 agent 拉取”只描述了 Flux 这一类实现。Argo CD 默认是**集中式推模型**：控制器跑在管理集群，持有每个目标集群的 kubeconfig/凭据，主动向目标集群 API 写入。两者都叫 GitOps，但威胁模型完全不同：

| 模型 | 控制器位置 | 凭据在哪 | 网络方向 | 主要风险 |
|---|---|---|---|---|
| 每集群拉取（Flux） | 目标集群内 | 目标集群内；只需 Git 出站 | 目标集群 → Git | 每个集群都要装 agent；Git 凭据分散 |
| 集中式推（Argo CD） | 管理集群 | 管理集群持有所有目标集群凭据 | 管理集群 → 每个目标集群 API | 管理集群是超级特权单点；被攻破等于所有集群失守 |
| CI 直推 | CI runner | CI 持有集群凭据 | CI → 集群 API | 凭据散落在 CI；难以持续调谐 |

选择时问三个问题：凭据集中在哪里？被攻破后的爆炸半径多大？网络是否需要目标集群开放入站？集中式推要特别加固管理集群（SSO、RBAC、网络策略、审计、break-glass），并限制 Argo CD 的 cluster-admin 范围；每集群拉取要解决 agent 与 Git 凭据的规模化分发。不要把两种模型混成一句“CI 不需要集群权限”。

## 32.2 仓库结构：环境、应用与所有权

一个可扩展的目录结构：

```text
fleet/
├── clusters/
│   ├── dev/
│   │   ├── kustomization.yaml
│   │   └── apps.yaml
│   ├── staging/
│   └── prod/
├── apps/
│   ├── payments/
│   │   ├── base/
│   │   │   ├── deployment.yaml
│   │   │   ├── service.yaml
│   │   │   └── kustomization.yaml
│   │   └── overlays/
│   │       ├── dev/
│   │       ├── staging/
│   │       └── prod/
│   └── frontend/
└── platform/
    ├── ingress/
    ├── monitoring/
    └── policies/
```

两种常见组织方式：

- **按环境分目录**：`clusters/prod` 引用 `apps/*/overlays/prod`，环境差异集中在 overlay。
- **App-of-Apps**：一个根 Application 管理一组子 Application，新增应用只需改根清单。

关键原则：

- **环境差异显式化**：副本数、资源、域名、特性开关放在 overlay，不要藏在脚本里。
- **基础清单可复用**：base 不包含环境特定值。
- **所有权清晰**：应用团队改 `apps/<service>`，平台团队改 `platform/`，通过 CODEOWNERS 约束。
- **不要手工改集群**：紧急变更也要回写 Git，否则下次调谐会被覆盖。

## 32.3 Argo CD：应用控制器与同步

Argo CD 的核心组件：

- **API Server/UI**：展示应用、差异、同步状态。
- **Repository Server**：拉取 Git、渲染 Helm/Kustomize/Jsonnet。
- **Application Controller**：持续比较期望状态与集群实际状态，执行同步。
- **Redis**：缓存。
- **Dex**：可选 SSO。

一个 Application：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: payments-prod
  namespace: argocd
spec:
  project: payments
  source:
    repoURL: https://github.com/example/fleet.git
    targetRevision: main
    path: apps/payments/overlays/prod
  destination:
    server: https://kubernetes.default.svc
    namespace: payments
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ApplyOutOfSyncOnly=true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
```

关键字段：

- `prune: true`：删除 Git 中已移除的资源。生产环境要配合 finalizer/备份，避免误删。
- `selfHeal: true`：集群漂移时自动同步。对事故排查可能造成干扰，需谨慎。
- `ApplyOutOfSyncOnly`：只 apply 有差异的资源，降低 API 压力。
- `retry`：同步失败退避重试。

### 同步波次与钩子

复杂应用有依赖顺序：CRD → namespace → 配置 → 工作负载 → 验证。

```yaml
metadata:
  annotations:
    argocd.argoproj.io/sync-wave: "-1"   # 先于默认 0
```

钩子：

- `PreSync`：迁移、备份、预检。
- `Sync`：主资源。
- `PostSync`：冒烟测试、通知。
- `SyncFail`：失败清理。

钩子 Job 要幂等、可重入、有超时；不要把长时间迁移塞进 PostSync 阻塞同步。

### ApplicationSet

多环境/多集群时，手工写 Application 不可维护。ApplicationSet 用生成器批量创建：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: payments
  namespace: argocd
spec:
  generators:
    - clusters:
        selector:
          matchLabels:
            env: prod
  template:
    metadata:
      name: 'payments-{{name}}'
    spec:
      project: payments
      source:
        repoURL: https://github.com/example/fleet.git
        targetRevision: main
        path: 'apps/payments/overlays/{{metadata.labels.env}}'
      destination:
        server: '{{server}}'
        namespace: payments
```

生成器类型：list、cluster、git、matrix、merge、pull request。PR 生成器可以做“预览环境”：每个 PR 创建一套临时 Application，合并后删除。

## 32.4 Flux：控制器组合

Flux 把 GitOps 拆成一组控制器：

- `source-controller`：拉取 Git/Helm/OCI 源。
- `kustomize-controller`：应用 Kustomize。
- `helm-controller`：管理 HelmRelease。
- `notification-controller`：事件与告警。
- `image-reflector-controller` / `image-automation-controller`：镜像扫描与自动更新。

一个 GitRepository + Kustomization：

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: fleet
  namespace: flux-system
spec:
  interval: 1m
  url: https://github.com/example/fleet.git
  ref:
    branch: main
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
  namespace: flux-system
spec:
  interval: 10m
  sourceRef:
    kind: GitRepository
    name: fleet
  path: ./clusters/prod
  prune: true
  wait: true
  timeout: 5m
```

Flux 与 Argo CD 的差异（选型时关注）：

| 维度 | Argo CD | Flux |
|---|---|---|
| 形态 | 有 UI/API 的应用管理平台 | 一组控制器/CLI |
| 多集群 | 集中式 Application 管理 | 每集群/每租户部署控制器 |
| 同步 | Application 资源 | Kustomization/HelmRelease |
| 渐进式 | 可集成 Argo Rollouts | 可集成 Flagger |
| 权限模型 | 集中式，需管理集群凭据 | 拉模型更彻底，凭据在集群内 |
| 学习曲线 | UI 友好，概念较多 | 组合灵活，偏 Git/CLI |

没有绝对优劣。大组织常见“Argo CD 做应用交付 + Flux 做平台组件”或反之。

## 32.5 Kustomize、Helm 与渲染边界

GitOps 仓库里常见三种形态：

- **纯 YAML**：最直观，但环境差异难复用。
- **Kustomize**：base + overlay，适合“同一应用多环境”。
- **Helm**：模板 + values，适合分发和参数化。

实践建议：

- 应用团队用 Kustomize overlay 管理环境差异。
- 第三方组件用 Helm，但把 chart 版本和 values 都锁进 Git。
- 不要在 GitOps agent 之外执行 `helm upgrade`；否则状态分裂。
- 渲染结果要可复现：锁定 chart 版本、镜像 digest、Kustomize 版本。
- 慎用 `latest` tag 和动态 values；它们让“Git 中的期望状态”不再确定。

镜像固定到 digest：

```yaml
image: example/payments@sha256:0123456789abcdef...
```

tag 可变，digest 不可变。生产环境应使用 digest，或至少用准入策略拒绝 `latest`。

## 32.6 密钥管理

不要把明文 Secret 提交到 Git。常见方案：

### SOPS + age/KMS

加密后的 Secret 可以进 Git：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: payments-db
stringData:
  password: ENC[AES256_GCM,data:...,type:str]
sops:
  age:
    - recipient: age1...
  encrypted_regex: ^(data|stringData)$
```

解密发生在集群内（Flux/Argo CD 插件）或 CI。密钥轮转要重新加密并提交。

### Sealed Secrets

用集群公钥加密，只有集群内控制器能解密：

```bash
kubeseal --format yaml < secret.yaml > sealed-secret.yaml
```

优点：可以安全提交。缺点：绑定集群/密钥，跨集群迁移需要重新加密。

### External Secrets Operator

Git 只存“引用”，真实值在 Vault/云 Secrets Manager：

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: payments-db
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault
    kind: ClusterSecretStore
  target:
    name: payments-db
  data:
    - secretKey: password
      remoteRef:
        key: payments/db
        property: password
```

优点：集中轮转、审计、最小暴露。缺点：多一个依赖，故障时可能拿不到密钥。

无论哪种方案，都要：限制谁能读解密后的 Secret、开启审计、定期轮转、备份恢复演练。

### 32.6.1 OCI 制品与 Gitless GitOps

Git 不是唯一的“事实来源”。Flux 的 `OCIRepository`（2.x）、Argo CD 的 OCI 支持，以及 Helm chart 的 OCI 分发，让“渲染后的制品”可以签名、版本化、按 digest 拉取：

```text
源码/Git → CI 渲染（Kustomize/Helm）→ 推送 OCI artifact（签名/SBOM）
         → 集群 agent 按 digest 拉取 → 调谐
```

优点：

- 集群不需要访问 Git，只需要访问镜像仓库；网络边界更简单。
- 制品可以用 cosign 签名/验证，和镜像供应链统一。
- 渲染结果不可变，避免“同一 commit 在不同时间渲染出不同结果”。

代价：

- Git 里的“人可读 diff”变成 OCI 里的“制品 diff”，评审/排障工具要跟上。
- 需要镜像仓库的保留/复制/签名策略。
- 回滚是切 digest/tag，不是 revert commit；审计链路要重新设计。

采用建议：从“Git + 渲染”起步，等制品签名/仓库治理成熟后再引入 OCI；不要把 OCI 当成“更快的 Git”。

## 32.7 渐进式交付：canary、蓝绿与流量控制

“一次全量发布”风险最高。渐进式交付把流量逐步切给新版本，并用指标决定继续或回滚。

### Argo Rollouts

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: payments
spec:
  replicas: 10
  strategy:
    canary:
      steps:
        - setWeight: 5
        - pause: { duration: 5m }
        - setWeight: 25
        - pause: { duration: 10m }
        - setWeight: 50
        - pause: { duration: 10m }
      analysis:
        templates:
          - templateName: success-rate
        startingStep: 1
      stableService: payments-stable
      canaryService: payments-canary
      trafficRouting:
        istio:
          virtualService:
            name: payments
            routes: [primary]
          destinationRule:
            name: payments
            stableSubsetName: stable
            canarySubsetName: canary
```

> `stableService`/`canaryService` 是 `canary` 下的字段，不是 `trafficRouting.nginx` 的子字段。Ingress NGINX 已退役；新平台用 Istio、Gateway API（Argo Rollouts 的 Gateway API 插件仍属 out-of-tree/alpha）或服务网格做流量切分，不要照搬旧 NGINX 示例。

分析模板查询 Prometheus：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: success-rate
spec:
  metrics:
    - name: success-rate
      interval: 1m
      successCondition: result[0] >= 0.99
      failureLimit: 3
      provider:
        prometheus:
          address: http://prometheus.monitoring:9090
          query: |
            sum(rate(http_requests_total{job="payments",code!~"5.."}[1m]))
            /
            sum(rate(http_requests_total{job="payments"}[1m]))
```

### Flagger

Flagger 与 Service Mesh/Ingress 集成，自动创建 canary Deployment、调整流量、运行 webhook 检查。适合已有 Istio/Linkerd/Gateway API 的团队；Ingress NGINX 已退役，不要再把它作为新平台的流量切分基础。

### 蓝绿

蓝绿同时保留两套完整环境，切换流量后观察，失败可快速切回。代价是双倍资源。适合无法做细粒度流量切分的应用。

### 数据库兼容性

渐进式发布的前提是**新旧版本能同时运行**：

- 迁移采用 expand/contract：先加列/表（兼容旧代码），再双写/回填，最后删旧字段。
- 不做破坏性 schema 变更；迁移与应用发布解耦。
- 读路径兼容旧数据；写路径考虑回滚。
- 大数据量迁移用独立 Job，带限速、断点续跑、校验。

## 32.8 漂移、回滚与灾难恢复

GitOps 的“持续调谐”会带来一个新问题：**有人手工改了集群，agent 又改回去**。处理方式：

- 明确“集群只读”文化，紧急变更走 break-glass 流程并回写 Git。
- 对关键资源设置 `selfHeal`，对调试中的资源暂停自动同步。
- 用 diff 视图/告警发现漂移，而不是等 agent 覆盖。
- 记录 break-glass 操作，事后补 Git commit。

回滚策略：

1. **Git revert**：最干净，触发完整调谐。
2. **Argo CD 历史回滚/Flux 回滚**：快速，但要确认 Git 与集群一致。
3. **镜像/Chart 回退**：只回退版本，保留配置变更。
4. **数据库回滚**：通常不可逆，优先 forward-fix。

灾难恢复：

- Git 仓库本身要备份/多副本；它是新的“控制面”。
- 集群引导（bootstrap）要能从一个空集群恢复到完整平台：先装 GitOps agent，再由它拉起所有组件。
- 密钥解密依赖（KMS/Vault/Sealed Secrets key）要纳入灾备。
- 定期做“空集群重建”演练，验证 Git 中的状态真的足够。

### 32.8.1 App-of-Apps 与引导顺序

一个空集群如何变成完整平台？常见做法是 **app-of-apps**：

```text
root Application
├── platform/crds
├── platform/cni
├── platform/csi
├── platform/observability
├── platform/policy
└── apps/team-a
```

关键：

- root 只引用子 Application，不直接部署业务。
- 用 sync wave/health 保证顺序：CRD → 基础设施 → 平台组件 → 业务。
- 子 Application 的 `prune`/`selfHeal` 策略要按组件区分；平台组件更保守。
- 引导脚本只做“装 GitOps agent + 创建 root Application”，其余交给 Git。

风险：

- root 应用失败会导致整个平台无法引导；保留手工 fallback。
- 删除 root 可能级联删除所有子 Application（finalizer 语义）；加保护。
- 平台组件与业务应用在同一仓库但不同所有权，review 规则要分开。

### 32.8.2 漂移检测与告警

“selfHeal 自动改回去”不等于“你知道发生了漂移”。需要：

- 定期对比 Git 渲染结果与集群 live 状态（Argo CD diff/Flux drift detection）。
- 对关键资源（RBAC、NetworkPolicy、Gateway、CRD）的漂移告警。
- 区分“预期漂移”（控制器写入的 status/默认值）与“人为修改”。
- 对 break-glass 操作打标签/注解，事后补 Git。

一个实用规则：

```text
任何 OutOfSync 超过 15 分钟
  → 若非 break-glass 标记，告警
  → 若为 break-glass，检查是否有回写 Git 的工单
```

## 32.9 多集群 GitOps

多集群不是复制 Application，而是管理“集群舰队”：

- **集群注册**：Argo CD 用 Secret 注册集群；Flux 用 kubeconfig Secret 或 Cluster API。
- **环境分层**：dev/staging/prod 用不同 overlay 和不同同步策略。
- **区域/租户**：按区域/业务线拆分 ApplicationSet。
- **渐进式推广**：先在 canary 集群验证，再推广到其余集群。
- **故障隔离**：一个集群的 agent 故障不应阻塞其他集群。

集群级清单（CNI、CSI、监控、策略）和应用清单要分开管理，避免“平台升级”和“业务发布”互相阻塞。

## 32.10 安全与合规

GitOps 把“集群写权限”集中到了 agent，安全重点转移：

- **仓库权限**：谁能合并到 main，谁就能改生产。用分支保护、CODEOWNERS、签名提交。
- **Agent 权限**：Argo CD/Flux 通常有较高权限；用 Project/AppProject、namespace 隔离、RBAC 限制。
- **镜像与清单**：准入校验签名/SBOM；禁止 `latest`；固定 digest。
- **密钥**：加密/外部化；限制解密权限；审计读取。
- **供应链**：CI 构建产物签名，GitOps 只部署已验证的 digest。
- **审计**：Git 历史 + 集群审计日志 + GitOps 同步记录，三者关联。

合规场景还要注意：谁批准了变更、变更影响了哪些集群、是否有四眼原则、能否证明生产与 Git 一致。

## 32.11 MiniK8s 的对照

MiniK8s 没有 GitOps 控制器，但可以模拟交付流水线的核心契约：

```bash
# CI 中：渲染/校验清单
python check_docs.py
python examples/run_all.py

# 部署到 MiniK8s
export MINIK8S_SERVER=http://127.0.0.1:8080
python -m mini_k8s.cli apply -f examples/08-ingress.yaml
python -m mini_k8s.cli get pods -o wide
```

你可以把 `mk8s apply` 包在一个脚本里，由 CI 在合并后执行，模拟“推”模型；也可以写一个轮询 Git 的小控制器，模拟“拉”模型。第 19 章的 Operator 模式可以作为实现起点。

MiniK8s 不实现：Application/Kustomization CRD、漂移检测、同步波次、密钥解密、渐进式流量控制。这些需要在真实集群用 Argo CD/Flux/Argo Rollouts/Flagger 验证。

## 32.12 生产清单

- [ ] Git 是唯一事实来源，生产变更必须经过 Git 评审。
- [ ] 环境差异在 overlay/values 中显式化，基础清单可复用。
- [ ] 镜像固定 digest，禁止可变 tag。
- [ ] Secret 加密或外部化，轮转与恢复有演练。
- [ ] 同步策略明确：prune/selfHeal 的风险已评估。
- [ ] 关键应用使用 canary/蓝绿，并有自动分析/回滚。
- [ ] 数据库迁移遵循 expand/contract，与应用发布解耦。
- [ ] 空集群 bootstrap 演练通过。
- [ ] 多集群有分层推广与故障隔离。
- [ ] 仓库、agent、密钥、供应链权限最小化并可审计。

## 本章小结

- GitOps 的核心是声明式、版本化、拉取式调谐，而不是某个工具。
- Argo CD 偏应用平台，Flux 偏控制器组合；选型看组织、权限模型和多集群需求。
- 仓库结构决定可维护性：base/overlay、环境分离、所有权清晰。
- 密钥、镜像 digest、数据库兼容性是渐进式交付的前提。
- 漂移、回滚、灾备和多集群推广要从第一天设计，而不是事后补。

## 延伸阅读

- [第 5 章 声明式工作流与 kubectl](05-declarative-workflow.md)
- [第 8 章 Deployment 与 ReplicaSet](08-deployment-replicaset.md)
- [第 14 章 Ingress 与 Gateway API](14-ingress-gateway-api.md)
- [第 16 章 服务网格与零信任](16-service-mesh-zero-trust.md)
- [第 24 章 准入控制与供应链安全](24-admission-supply-chain.md)
- [第 45 章 商业分析 III：平台工程、开源与创业](45-business-platform.md)：GitOps 与平台产品的组织/ROI。
- [第 49 章 应用工程](49-application-engineering.md)：镜像、数据库迁移兼容与发布前检查。
- [附录 L 云原生生态地图](../appendices/L-ecosystem-map.md)：Argo CD、Flux、Rollouts 的选型位置。
- [附录 Q 扩展点与交付物开发实战](../appendices/Q-extension-development.md)：Helm chart 工程化、OCI 发布与 GitOps 的衔接。
