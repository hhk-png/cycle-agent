# 第 48 章 平台工程实战：Golden Path、多租户与自服务 API

> Kubernetes 是原材料，不是平台。平台是“把正确的做法做成默认路径”，让开发者不用读完整本教程也能安全上线。本章把前面章节的机制串成一个可落地的平台：租户模型、Golden Path 模板、自服务 CRD、隔离验收、可观测性、成本与 90 天计划。
>
> 商业视角的组织/ROI/创业分析见第 45 章；多租户机制见第 26 章；GitOps 与渐进式交付见第 32 章；CRD/Operator 工程见第 19 章与[附录 Q](../appendices/Q-extension-development.md)。

## 48.1 平台的最小可用产品（MVP）

平台的第一版不应该是一个门户网站，而是一条**端到端可验收的 Golden Path**：

```text
开发者提出“我要一个服务”
  → 从模板生成仓库（代码、Dockerfile、CI、清单）
  → CI 构建、扫描、签名、推送 digest
  → GitOps 同步到 dev/staging/prod
  → 默认有指标/日志/告警/域名/TLS
  → 默认满足安全/配额/成本标签
  → 出问题时有 runbook 和负责人
```

MVP 的验收指标（都要能自动采集）：

| 能力 | 指标 | 建议目标 |
|---|---|---|
| 创建服务 | 从脚手架到 dev 可访问 | < 30 分钟 |
| 首次发布 | 从合并到 dev | < 15 分钟 |
| 生产发布 | 从合并到 prod（低风险） | < 1 小时 |
| 回滚 | 决定到恢复 | < 10 分钟 |
| 可观测 | 新服务默认有指标/日志/告警 | 100% |
| 安全 | 默认非 root、无特权、镜像签名校验 | 100% |
| 成本 | 默认 requests、归属标签、预算 | 100% |
| 支持 | 平台工单首次响应 | < 4 小时 |

先做“一个团队、一个服务类型、一条环境链路”，跑通后再扩展。不要一开始支持所有语言、所有中间件、所有云。

## 48.2 租户模型与命名空间布局

### 48.2.1 先定义“租户”是什么

租户可以是一个团队、一个业务域或一个外部客户。关键不是名字，而是**边界**：

- 谁可以创建 namespace/工作负载？
- 谁能读 Secret、看日志、改网络策略？
- 配额、成本、审计、SLO 归谁？
- 故障时谁 on-call？

常见布局：

```text
tenant-payments/
├── tenant-payments-dev
├── tenant-payments-staging
└── tenant-payments-prod
```

用标签把“租户/环境/成本中心/负责人”固定下来：

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: tenant-payments-prod
  labels:
    platform.example.com/tenant: payments
    platform.example.com/env: prod
    platform.example.com/cost-center: "cc-1042"
    platform.example.com/owner: payments-team
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: v1.37
```

标签不是装饰：配额、NetworkPolicy、成本分摊、告警路由、备份策略都靠它选择对象。标签治理要从第一天做，后面补很痛苦。

### 48.2.2 默认护栏：一个 namespace 的最小配置

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: tenant-quota
  namespace: tenant-payments-prod
spec:
  hard:
    requests.cpu: "40"
    requests.memory: 80Gi
    limits.cpu: "80"
    limits.memory: 160Gi
    pods: "200"
    services.loadbalancers: "2"
    persistentvolumeclaims: "20"
---
apiVersion: v1
kind: LimitRange
metadata:
  name: tenant-defaults
  namespace: tenant-payments-prod
spec:
  limits:
    - type: Container
      default:
        cpu: 500m
        memory: 512Mi
      defaultRequest:
        cpu: 100m
        memory: 128Mi
      max:
        cpu: "8"
        memory: 16Gi
      maxLimitRequestRatio:
        cpu: "10"
        memory: "4"
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: tenant-payments-prod
spec:
  podSelector: {}
  policyTypes: ["Ingress", "Egress"]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns-and-platform
  namespace: tenant-payments-prod
spec:
  podSelector: {}
  policyTypes: ["Egress"]
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
    - to:
        - namespaceSelector:
            matchLabels:
              platform.example.com/gateway: "true"
      ports:
        - protocol: TCP
          port: 8080
```

要点：

- **默认拒绝**，再显式放行 DNS、网关、必要依赖；不要先全通再“以后收紧”。
- Quota 同时限制 requests/limits，避免“只写 limits 不写 requests”或反之。
- LimitRange 给默认值，但不要给得太大；默认值是平台最有力的成本杠杆。
- NetworkPolicy 需要 CNI 支持；没有执行能力的 CNI 只会让策略“看起来存在”。
- PSA `restricted` 是起点，不是终点；镜像、供应链、运行时检测见第 24、25 章。

### 48.2.3 层级与自助

- **HNC**：namespace 树，父级 RBAC/Quota/NetworkPolicy 向下传播，适合部门/团队层级。
- **Capsule**：`Tenant` CRD 打包 namespace、配额、默认策略、用户/组，租户可自助创建 namespace。
- **vCluster/Kamaji**：需要独立控制面时使用（第 33 章）；共享节点模式不是生产强隔离。

选择原则：能用 namespace + 策略解决就不要上虚拟集群；需要强隔离/独立版本时再升级形态。

## 48.3 Golden Path：模板、CI 与 GitOps

Golden Path 不是“唯一允许的写法”，而是“默认正确的写法”：开发者可以偏离，但要显式承担成本。

### 48.3.1 仓库模板

一个最小服务模板：

```text
checkout/
├── Dockerfile
├── src/
├── deploy/
│   ├── base/                 # Deployment/Service/HTTPRoute/PDB
│   └── overlays/
│       ├── dev/
│       ├── staging/
│       └── prod/
├── .github/workflows/ci.yaml # 构建、扫描、签名、更新 Git
└── catalog-info.yaml         # 服务目录/所有权/依赖
```

模板应自带：

- 多阶段构建、非 root、只读 rootfs（可行时）、固定基础镜像 digest。
- readiness/liveness/startup 探针、资源 requests/limits、PDB。
- 结构化日志、Prometheus 指标、OTel 追踪。
- 安全上下文、NetworkPolicy、ServiceAccount（默认不挂 token）。
- 成本标签、负责人、runbook 链接。

### 48.3.2 一条可验收的流水线

```text
PR:
  lint + 单元测试 + 构建镜像 + SBOM + 漏洞扫描 + 清单 dry-run
merge:
  推送 digest → cosign 签名/attestation → 更新 GitOps 仓库的 dev overlay
dev:
  Argo CD/Flux 同步 → 冒烟测试 → 自动晋级或人工批准
staging:
  集成/E2E + 负载测试 + 安全扫描
prod:
  渐进式发布（canary/蓝绿）→ SLO 观察 → 完成或回滚
```

关键原则：

- **以 digest 部署**，不要用可变 tag；Git 里记录 digest。
- **构建与部署分离**：CI 只写 Git/制品库，不直接持有生产集群凭据。
- **环境差异显式化**：副本数、资源、域名、特性开关放 overlay，不复制整份 YAML。
- **回滚是切 digest/revert commit**，不是重新构建。

### 48.3.3 默认可观测性与 SLO

模板生成的每个服务默认应有：

- RED 指标（请求率、错误率、延迟）或 USE 指标（利用率、饱和度、错误）。
- 结构化日志，带 request id/trace id，不打印敏感字段。
- OTel 追踪，至少覆盖入口和关键依赖。
- 告警：错误率、P99 延迟、Pod 重启、证书到期、队列积压。
- 一个 SLO 和错误预算；告警绑定 runbook 和负责人。

平台团队要提供“可观测性 SDK/边车/Collector 配置”，而不是让每个团队自己接一遍。

## 48.4 自服务 API：用一个 CRD 隐藏十个 YAML

当模板稳定后，下一步是把“复制仓库 + 改 YAML”变成“提交一个平台对象”。

### 48.4.1 定义 Application CRD

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: applications.platform.example.com
spec:
  group: platform.example.com
  scope: Namespaced
  names:
    kind: Application
    plural: applications
    singular: application
    shortNames: ["app"]
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources:
        status: {}
      additionalPrinterColumns:
        - name: Ready
          type: string
          jsonPath: .status.conditions[?(@.type=="Ready")].status
        - name: URL
          type: string
          jsonPath: .status.url
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required: ["image", "port"]
              properties:
                image:
                  type: string
                  description: 必须是 digest 引用
                port:
                  type: integer
                  minimum: 1
                  maximum: 65535
                replicas:
                  type: integer
                  minimum: 1
                  maximum: 50
                  default: 2
                host:
                  type: string
                resources:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
            status:
              type: object
              properties:
                url:
                  type: string
                conditions:
                  type: array
                  items:
                    type: object
                    x-kubernetes-preserve-unknown-fields: true
```

一个实例：

```yaml
apiVersion: platform.example.com/v1alpha1
kind: Application
metadata:
  name: checkout
  namespace: tenant-payments-prod
spec:
  image: registry.example.com/payments/checkout@sha256:0123456789abcdef
  port: 8080
  replicas: 3
  host: checkout.example.com
```

### 48.4.2 控制器做什么

`ApplicationController` 的 reconcile 应该只做“从 spec 到一组子对象”的收敛：

```text
reconcile(Application app):
  1. 校验/默认化（digest、端口、host、资源）
  2. 生成/更新 Deployment（ownerReference=app）
  3. 生成/更新 Service（ClusterIP）
  4. 生成/更新 HTTPRoute（host + backendRef）
  5. 生成/更新 PDB、ServiceMonitor、NetworkPolicy（按平台默认）
  6. 等待子对象就绪，写 status.conditions / status.url
  7. 处理删除：finalizer 清理外部资源（DNS/证书/队列）
```

工程要点：

- **幂等**：每次 reconcile 都从 spec 重新计算，不依赖“上一次创建了什么”。
- **所有权**：所有子对象设置 ownerReference；用户手改会被纠正（或告警）。
- **状态**：用 `Ready`/`Progressing`/`Degraded` conditions，附 `observedGeneration`。
- **校验**：CRD schema + CEL 做静态校验；跨对象/外部检查用 admission webhook（第 19、24 章）。
- **升级**：CRD 多版本按“先 served 非 storage → webhook → 切 storage → 迁移 → 下线旧版”的顺序（第 4、19 章）。
- **限速/重试**：用 workqueue + 指数退避，不要把错误吞掉（附录 Q）。

MiniK8s 的 Website Operator（`examples/website_operator.py`）是一个可运行的缩小版：它把 `Website` 对象变成 Deployment/Service/Ingress，并维护 status。可以先在 MiniK8s 上跑通“自定义对象 → 子对象 → 状态”的闭环，再迁移到 controller-runtime。

### 48.4.3 平台 API 的边界

不要让一个 CRD 变成“万能应用定义”。分层：

| 层 | 例子 | 谁维护 |
|---|---|---|
| 平台 API | Application、Database、Queue | 平台团队 |
| 标准 Kubernetes | Deployment、Service、HTTPRoute | 平台/租户 |
| 基础设施 | Cluster、NodePool、Network | 云/基础设施团队 |

平台 API 应该稳定、少字段、有默认值；标准 Kubernetes 对象保留给需要精细控制的团队。不要用 CRD 包装所有东西，否则平台会成为新的瓶颈。

## 48.5 多租户隔离与验收

### 48.5.1 隔离的六个面

| 面 | 机制 | 验收 |
|---|---|---|
| API | RBAC、Aggregated API、impersonation 限制 | 租户 A 不能 get/list 租户 B 的对象 |
| 准入 | PSA、Quota、LimitRange、VAP/MAP | 特权 Pod、超配额、无 request 被拒绝/默认化 |
| 网络 | NetworkPolicy、Gateway API allowedRoutes/ReferenceGrant | 跨租户 Pod/路由/证书不可达 |
| 存储 | StorageClass、PVC 配额、卷访问模式 | 不能挂载他人 PVC；RWX 不越权 |
| 节点 | 污点/亲和、RuntimeClass、沙箱 | 普通租户不能落到系统/GPU 专用节点 |
| 身份 | ServiceAccount、工作负载身份、Secret 隔离 | 不能读他人 Secret/ token |

### 48.5.2 一组可执行的负向测试

```bash
# 以租户 A 的身份尝试读租户 B 的 Secret（应 403）
kubectl auth can-i get secret/payments-db \
  --namespace tenant-payments-prod \
  --as system:serviceaccount:tenant-search-dev:default

# 尝试创建特权 Pod（应被 PSA/策略拒绝）
kubectl apply --dry-run=server -f privileged-pod.yaml \
  --as system:serviceaccount:tenant-search-dev:default

# 尝试跨 namespace 路由（应 ResolvedRefs=False）
kubectl apply --dry-run=server -f cross-tenant-route.yaml

# 尝试超配额（应 Forbidden）
kubectl apply -f huge-deployment.yaml
```

把负向测试纳入 CI/定期巡检；不要只靠“策略对象存在”。

### 48.5.3 隔离矩阵

| 租户类型 | 推荐形态 | 数据面 | 适用 |
|---|---|---|---|
| 受信团队 | namespace + Quota/PSA/NetworkPolicy | 共享 | 内部平台大多数团队 |
| 一般多租户 | Capsule/HNC + 强策略 | 共享 | 部门/业务域 |
| 强隔离 | vCluster Private Nodes 或独立集群 | 专属节点 | 合规/外部客户 |
| 强合规 | 独立集群 + 独立账户/网络 | 独立 | 金融/政府 |

不要用“命名空间 + 网络策略”宣称满足强合规；审计要看数据面、身份、密钥和证据链（第 26.14 节）。

## 48.6 平台可观测性与开发者体验

平台自身也是一个系统，需要 SLO：

| 平台 SLO | 指标 | 告警 |
|---|---|---|
| 供应延迟 | 从 Application 创建到 Ready 的 P95 | > 10 分钟 |
| GitOps 同步延迟 | commit 到集群生效的 P95 | > 5 分钟 |
| 准入可用性 | webhook/策略错误率 | > 0.1% |
| 配额服务 | Quota API 错误率/延迟 | 持续 5 分钟 |
| 工单响应 | 首次响应时间 | 超过目标 |
| 成本可见性 | 有归属标签的 Pod 比例 | < 99% |

开发者体验指标：

- **DORA**：部署频率、Lead time、变更失败率、恢复时间。
- **SPACE**：满意度、绩效、活动、协作、效率。
- **平台采用**：Golden Path 使用率、绕过率、模板创建到首次部署时间。

告警要绑定 runbook 和负责人；平台团队不能只做“告警转发”。

## 48.7 成本与配额治理

平台最容易被诟病的是“资源给了但账单没人认”。最小闭环：

1. **标签**：tenant/env/cost-center/owner 必须由准入强制。
2. **配额**：namespace 级 Quota + LimitRange 默认值。
3. **可见性**：OpenCost/Kubecost 按 namespace/租户分摊；每周 showback。
4. **优化**：闲置副本、过度 request、未挂载 PV、长尾镜像、GPU 利用率。
5. **治理**：预算告警、超额审批、季度回顾。

GPU 租户额外做队列（Kueue）、gang、配额借用和 checkpoint 策略（第 21、22 章）。不要用“先到先得”管理昂贵设备。

## 48.8 平台升级与变更

平台版本不止 Kubernetes：

```text
Kubernetes minor
├── CNI/CSI/运行时
├── Gateway/Ingress 控制器
├── 策略引擎/证书/可观测 agent
├── 平台 CRD/Controller
├── Golden Path 模板
└── 租户工作负载的兼容性
```

变更策略：

- 维护版本台账与兼容矩阵（附录 I、R）。
- 平台组件先在“平台自身 namespace”灰度，再推到租户。
- CRD 变更走多版本/转换/弃用流程，不直接改字段语义。
- 模板变更要可回滚；租户可以选择 pin 版本，但要有升级截止时间。
- 每次升级做负向测试：旧 API、旧 CRD、旧策略、旧 webhook 是否仍工作。

## 48.9 组织与运营

- **Team Topologies**：stream-aligned（业务）、platform（平台）、enabling（赋能）、complicated-subsystem（复杂子系统）。
- **平台即产品**：有产品经理/路线图/用户访谈/采用率，不是“基础设施团队的后台项目”。
- **支持分层**：自助文档 → 模板/CI → 工单 → on-call；每层有 SLA。
- **例外流程**：Golden Path 之外的需求要能申请、评审、限期、回收；没有例外流程的平台会逼团队绕过。
- **文档**：每个模板有“为什么这样默认”“如何偏离”“如何回滚”。
- **社区**：office hours、内部博客、案例分享；平台采用是组织变革，不只是技术。

## 48.10 一个 90 天落地计划

| 阶段 | 周 | 交付 | 退出标准 |
|---|---|---|---|
| 发现 | 1–2 | 用户访谈、服务清单、痛点/基线 | 3 个高优场景、可量化基线 |
| MVP | 3–4 | 租户模型、namespace 护栏、一个模板 | 一个真实服务跑通 dev |
| 流水线 | 5–6 | CI、签名/SBOM、GitOps、可观测性默认值 | 合并到 dev < 15 分钟 |
| 自服务 | 7–8 | Application CRD/Controller、配额/成本标签 | 开发者不提交工单即可上线 |
| 试点 | 9–10 | 2–3 个团队、SLO/告警/runbook | 无 P1、采用率 > 50% |
| 扩展 | 11–12 | 第二个服务类型、例外流程、培训 | 工单量下降、绕过率下降 |
| 运营 | 13+ | 季度回顾、成本优化、版本升级 | SLO 达标、路线图持续 |

每周复盘：交付、阻塞、采用、成本、事故。不要用“平台上线”作为终点；平台的价值在持续采用和降低认知负担。

## 48.11 反模式

- **UI 先行**：门户很漂亮，底层没有 Golden Path/策略，开发者还是要手工填 YAML。
- **没有租户模型**：所有团队一个 namespace，配额/网络/成本全部失控。
- **Golden Path 只是文档**：没有模板、CI、默认值，等于没有。
- **平台团队成为瓶颈**：所有变更都要平台审批，开发者开始绕过。
- **策略没有例外**：要么全员被卡，要么策略被关闭。
- **成本不可见**：账单月底才出现，没人对资源负责。
- **过度抽象**：一个 CRD 包办所有中间件，字段爆炸、升级困难。
- **把共享集群当强隔离**：用 namespace 宣称多租户合规，审计时拿不出证据。

## 本章小结

- 平台 = Golden Path + 护栏 + 自服务 API + 可运营的反馈闭环，不是 kubectl 的 UI。
- 先定义租户和边界，再用 Quota/LimitRange/PSA/NetworkPolicy 做默认护栏。
- Golden Path 要可验收：模板、CI、签名、GitOps、可观测性、成本标签缺一不可。
- 自服务 API 用少量稳定的 CRD 隐藏复杂度；控制器要幂等、可观测、可升级。
- 多租户隔离要按 API/准入/网络/存储/节点/身份六面验收，负向测试比策略清单可靠。
- 平台自身要有 SLO、成本、版本和例外流程；90 天计划以“真实服务跑通”为里程碑。

## 延伸阅读

- [第 19 章 CRD 与 Operator 工程化](19-crd-operator.md)：CRD schema、controller、升级。
- [第 24 章 准入控制与供应链安全](24-admission-supply-chain.md)：PSA、VAP/MAP、签名与 SBOM。
- [第 26 章 多租户与资源治理](26-multitenancy-governance.md)：Quota、隔离矩阵、合规证据。
- [第 32 章 GitOps 与渐进式交付](32-gitops-progressive-delivery.md)：Argo CD/Flux、canary、回滚。
- [第 45 章 商业分析 III：平台工程、开源与创业](45-business-platform.md)：组织、ROI、商业化。
- [第 47 章 综合架构案例](47-architecture-casebook.md)：不同约束下的平台取舍。
- [第 49 章 应用工程](49-application-engineering.md)：Golden Path 模板背后的应用契约与生产就绪清单。
- [第 50 章 事件驱动、工作流与数据管道](50-event-driven-workflows.md)：平台上的消息、工作流与数据管道能力。
- [附录 Q 扩展点与交付物开发实战](../appendices/Q-extension-development.md)：client-go/controller-runtime/envtest/Helm。
- [附录 N 成本工程](../appendices/N-cost-engineering.md)：从账单到 Pod 的成本分摊。
