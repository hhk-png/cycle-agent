# 第 19 章 CRD 与 Operator 工程化

## 19.1 CRD 是类型，不是行为

CustomResourceDefinition 只做三件事：

1. 注册新的 API 类型。
2. 定义 schema、版本、作用域。
3. 让 API Server 存储/返回自定义对象。

它不会自动：

- 创建任何资源。
- 处理业务逻辑。
- 保证最终一致。
- 提供默认值（除非 schema/webhook）。

行为由 Controller/Operator 提供。

## 19.2 CRD 完整结构

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: websites.example.com
spec:
  group: example.com
  scope: Namespaced
  names:
    plural: websites
    singular: website
    kind: Website
    shortNames: [web]
    categories: [all]
  versions:
    - name: v1
      served: true
      storage: true
      subresources:
        status: {}
        scale:
          specReplicasPath: .spec.replicas
          statusReplicasPath: .status.replicas
      additionalPrinterColumns:
        - name: Ready
          type: integer
          jsonPath: .status.readyReplicas
        - name: URL
          type: string
          jsonPath: .status.url
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          properties:
            spec:
              type: object
              required: [image]
              properties:
                image: {type: string}
                replicas:
                  type: integer
                  minimum: 1
                  maximum: 100
                port:
                  type: integer
                  minimum: 1
                  maximum: 65535
            status:
              type: object
              properties:
                readyReplicas: {type: integer}
                url: {type: string}
```

关键字段：

- `group/names/plural`：CRD 名字必须是 `plural.group`。
- `scope`：Namespaced/Cluster。
- `versions`：可以多个 served 版本，只能一个 storage。
- `subresources.status`：启用 `/status` 子资源，控制器写状态不增加 generation。
- `subresources.scale`：让 HPA/kubectl scale 能操作 CR。
- `additionalPrinterColumns`：`kubectl get` 显示自定义列。
- `schema`：OpenAPI v3 结构化 schema。

## 19.3 Schema 校验

好的 CRD schema 应该：

- `required` 明确必填字段。
- 数值范围（minimum/maximum）。
- 字符串格式/pattern。
- 枚举。
- 列表 item 类型。
- 默认值（1.17+ 支持 `default`）。
- `x-kubernetes-*` 扩展（listType、int-or-string、preserve-unknown-fields）。

MiniK8s 的 `validate_openapi` 支持：

- type（object/array/string/number/boolean）。
- required。
- properties。
- enum。
- pattern。
- minimum/maximum。
- additionalProperties: false。

不支持完整 OpenAPI、默认值、CEL 校验规则。真实 API Server 支持 CEL（1.25+），可以做复杂跨字段校验。

## 19.4 多版本与转换

一个 CRD 可以有多个版本：

```yaml
versions:
  - name: v1alpha1
    served: true
    storage: false
  - name: v1
    served: true
    storage: true
```

API Server 需要在版本之间转换：

- 如果 schema 兼容，可以无损转换。
- 否则需要 Conversion Webhook。

转换 webhook 是一个 HTTPS 服务，API Server 调用它：

```yaml
spec:
  conversion:
    strategy: Webhook
    webhook:
      conversionReviewVersions: [v1]
      clientConfig:
        service:
          namespace: system
          name: webhook
          path: /convert
```

风险：

- webhook 不可用导致 CRD 读写失败。
- 转换逻辑错误导致数据损坏。
- 版本升级顺序（先部署 webhook，再改 CRD）。

### 19.4.1 转换 webhook 的实现契约

Conversion webhook 接收 `ConversionReview`，返回转换后的对象：

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: ConversionReview
request:
  uid: 705ab4f5-6393-11e8-b7cc-42010a800002
  desiredAPIVersion: example.com/v1
  objects:
    - apiVersion: example.com/v1alpha1
      kind: Widget
      metadata: {name: w1, namespace: default}
      spec: {size: 3}
```

响应必须：

- 保留 `metadata`（name/namespace/uid/resourceVersion 等）。
- 返回请求的 `desiredAPIVersion`。
- 对无法转换的对象返回 `status: Failure` 与原因。
- 保持幂等；API Server 可能重试。
- 不修改存储中的原对象（转换是读时/写时行为）。

实现建议：

- 用 controller-runtime 的 conversion 接口，写单元测试覆盖每个版本对。
- 对“字段重命名/语义变化”写显式转换，不要依赖 JSON round-trip。
- 保留未知字段（`preserveUnknownFields` 或显式字段）避免丢数据。
- webhook 证书轮换与高可用要设计；它是 CRD 的关键路径。
- 升级顺序见 19.4.2：先加新版本 `served: true, storage: false`，再部署/验证 webhook，最后切 storage。

### 19.4.2 存储版本迁移与裁剪

CRD 的多版本有一个容易忽略的规则：

- 只有一个版本可以 `storage: true`。
- 客户端可以读写任意 `served: true` 的版本，但 etcd 只存 storage 版本。
- 修改 storage 版本后，旧对象不会自动重写；需要迁移/重写才能让旧版本真正退出。

迁移步骤（概念）：

1. **先加新版本**：`served: true, storage: false`；旧版本仍是 storage。
2. 部署/更新 conversion webhook，验证新→旧、旧→新的 round-trip 与未知字段保留。
3. **切换 storage**：把新版本设为 `storage: true`，旧版本改为 `storage: false`（仍 served）。
4. 通过脚本/控制器重写对象，让旧对象以新版本落盘；观察错误与版本分布。
5. 确认没有旧版本客户端后，把旧版本设为 `served: false`，再考虑删除。

不要在 webhook 未验证前就把新版本设为 storage；也不要先删旧版本再补转换，否则旧对象会无法读取。

裁剪（pruning）：

- 未在 schema 中声明的字段默认会被裁剪（structural schema）。
- `x-kubernetes-preserve-unknown-fields: true` 可保留未知字段，但会削弱校验。
- 裁剪发生在写入/读取时，可能让客户端“写进去的字段消失”；升级 CRD schema 时要评估。

### 19.4.3 CEL 校验与策略

CRD 支持 `x-kubernetes-validations`（CEL）：

```yaml
openAPIV3Schema:
  type: object
  properties:
    spec:
      type: object
      x-kubernetes-validations:
        - rule: "self.replicas <= 10"
          message: "replicas must be <= 10"
        - rule: "self.minReplicas <= self.replicas"
          message: "minReplicas must not exceed replicas"
```

进阶用法：

- **不可变字段**：用 `oldSelf` 比较旧值，例如 `rule: "self == oldSelf"`；只允许某一状态转换时用 `optionalOldSelf`，避免对历史对象误判。
- **状态机**：`rule: "oldSelf.phase == 'Pending' ? self.phase == 'Running' : true"`，把合法转换写进 schema。
- **错误可读性**：`messageExpression` 可以拼接字段值；`reason`（`FieldValueInvalid`/`FieldValueForbidden` 等）和 `fieldPath` 帮助客户端定位。
- **成本预算**：CEL 规则有估算成本上限；大列表/复杂正则可能被拒绝。把昂贵校验拆成多条简单规则。

优点：

- 在 API Server 内执行，不依赖 webhook。
- 适合跨字段/不变量校验与状态转换。
- 失败返回 422，客户端可读。

限制：

- CEL 不能做外部调用/复杂计算。
- 规则有成本估算限制；复杂规则可能被拒绝。
- 规则变更对已存对象不回溯；需要迁移/审计。
- 不是所有字段都能被 CEL 访问（取决于 schema）。
- CEL 适合“拒绝”，不适合“变更”；变更用 MutatingAdmissionPolicy 或 webhook。

## 19.5 Status 与 Conditions

自定义资源也应该遵循 Kubernetes 约定：

```yaml
status:
  observedGeneration: 3
  readyReplicas: 2
  conditions:
    - type: Ready
      status: "True"
      reason: ReconciliationSucceeded
      message: "2/2 replicas ready"
      lastTransitionTime: "..."
      observedGeneration: 3
```

原则：

- `observedGeneration` 表示控制器看到的最新 spec 代次。
- conditions 用 True/False/Unknown，不要用字符串 "ok"。
- 每个 condition 有 reason/message，便于排障。
- 不要把大日志写进 status。

## 19.6 Operator 的工程结构

以 Go + controller-runtime 为例：

```text
cmd/manager/main.go
api/v1/website_types.go       # CRD Go 类型
controllers/website_controller.go
config/crd/bases/...
config/rbac/...
config/samples/...
```

核心 Reconcile：

```go
func (r *WebsiteReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var site examplev1.Website
    if err := r.Get(ctx, req.NamespacedName, &site); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }
    desired := buildDeployment(&site)
    if err := controllerutil.SetControllerReference(&site, desired, r.Scheme); err != nil {
        return ctrl.Result{}, err
    }
    _, err := controllerutil.CreateOrUpdate(ctx, r.Client, desired, func() error {
        desired.Spec.Replicas = site.Spec.Replicas
        return nil
    })
    if err != nil {
        return ctrl.Result{}, err
    }
    // update status
    return ctrl.Result{}, nil
}
```

工具链：

- Kubebuilder：脚手架、RBAC、CRD 生成。
- Operator SDK：基于 controller-runtime，集成 OLM。
- controller-gen：生成 CRD/RBAC/DeepCopy。
- envtest：本地 API Server 测试。
- OLM：Operator 打包/升级/权限。

## 19.7 MiniK8s 的 Website Operator

`examples/website_operator.py`：

- 读取所有 Website。
- 生成 Deployment/Service。
- 使用 ownerReference。
- 更新 status。
- 轮询调谐。

核心代码：

```python
def desired_deployment(website):
    name = website["metadata"]["name"]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "ownerReferences": [owner_reference(website)],
        },
        "spec": {
            "replicas": website["spec"].get("replicas", 1),
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [{
                    "name": "web",
                    "image": website["spec"]["image"],
                }]},
            },
        },
    }
```

运行：

```bash
python examples/website_operator.py --server http://127.0.0.1:8080
```

示例：

```bash
python -m mini_k8s.cli apply -f examples/14-crd-operator.yaml
python -m mini_k8s.cli get websites
python -m mini_k8s.cli get deployments blog
```

测试 `test_website_operator_reconciles` 验证 Deployment/Service/status。

## 19.8 Operator 的常见错误

**把 CRD 当 Helm Chart**：只注册类型，不写控制器，什么都不会发生。

**没有 status**：用户无法知道调谐结果。

**不处理删除/finalizer**：外部资源泄漏。

**不幂等**：每次调谐都创建重复资源。

**不做 leader election**：多副本重复创建。

**权限过大**：Operator 通常需要读写多种资源，必须最小化 RBAC。

**没有升级策略**：CRD 版本、控制器版本、数据迁移需要一起设计。

**没有测试**：至少要有 envtest/集成测试，验证调谐和状态。

## 19.9 Operator 的适用边界

适合：

- 有状态服务（数据库、Kafka、Prometheus）。
- 需要领域知识（备份、故障切换、升级）。
- 复杂多资源编排。
- 平台团队封装内部能力。

不适合：

- 简单 Deployment + Service。
- 没有运维领域知识的 CRUD 包装。
- 团队没有能力维护长期控制器。

“写 Operator”不是成熟度标志；能不用就不用。

## 19.10 CRD 的运维风险

- CRD 删除会删除所有 CR（取决于 finalizer/清理）。
- CRD schema 变更可能破坏现有对象。
- CRD 数量多会增加 API Server 内存/discovery 开销。
- CRD 的 RBAC 容易遗漏（新资源默认无权限）。
- CRD 的 conversion webhook 是高可用关键路径。
- CRD 的 status 子资源必须启用，否则控制器写 status 会触发 spec 冲突。

## 19.11 Kubebuilder 实战：从 CRD 到可部署 Operator

前文讲了结构，这里给出一条可执行的工程路径（Go + controller-runtime）。

### 初始化与脚手架

```bash
mkdir website-operator && cd website-operator
kubebuilder init --domain example.com --repo example.com/website-operator
kubebuilder create api --group example --version v1 --kind Website --resource --controller
```

生成：

```text
api/v1/website_types.go
internal/controller/website_controller.go
config/crd/bases/example.com_websites.yaml
config/rbac/role.yaml
config/samples/example_v1_website.yaml
```

### 定义 Spec/Status

```go
type WebsiteSpec struct {
    Image    string `json:"image"`
    Replicas int32  `json:"replicas,omitempty"`
}

type WebsiteStatus struct {
    ReadyReplicas int32              `json:"readyReplicas,omitempty"`
    URL           string             `json:"url,omitempty"`
    Conditions    []metav1.Condition `json:"conditions,omitempty"`
}
```

给 Status 加 `+kubebuilder:subresource:status`，给 Spec 字段加 `+kubebuilder:validation:Minimum=1` 等标记；`make manifests` 会生成 CRD schema。

### Reconcile 的关键顺序

```go
func (r *WebsiteReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var site examplev1.Website
    if err := r.Get(ctx, req.NamespacedName, &site); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }
    if !site.DeletionTimestamp.IsZero() {
        // 清理外部资源后移除 finalizer
        return ctrl.Result{}, r.cleanup(ctx, &site)
    }
    if err := r.ensureFinalizer(ctx, &site); err != nil {
        return ctrl.Result{}, err
    }
    if err := r.ensureDeployment(ctx, &site); err != nil {
        return ctrl.Result{}, err
    }
    if err := r.ensureService(ctx, &site); err != nil {
        return ctrl.Result{}, err
    }
    return ctrl.Result{}, r.updateStatus(ctx, &site)
}
```

要点：

- 先处理删除，再做创建；finalizer 只加一次。
- `CreateOrUpdate` 应使用 `controllerutil.CreateOrUpdate` 或 SSA，避免覆盖其他管理者字段。
- `updateStatus` 用 `r.Status().Update`，不要改 Spec 触发新 generation。
- 返回 `ctrl.Result{RequeueAfter: ...}` 做周期性检查；返回 error 会走指数退避。

### 测试与部署

```bash
make manifests generate
make test          # envtest 启动真实 API Server + etcd
make docker-build IMG=registry.example.com/website-operator:v1
make deploy IMG=registry.example.com/website-operator:v1
```

`envtest` 不启动 kubelet/scheduler，适合验证 CRD/控制器语义；端到端仍要在 kind/真实集群跑。

### 升级与兼容

- 先加新 CRD 版本（served），保留旧版本 storage，再写 conversion。
- 控制器要能处理旧对象；不要假设所有 CR 都已迁移。
- CRD 删除是危险操作：先确认没有 CR/finalizer，再删除。
- 权限用 `make manifests` 生成最小 RBAC，不要手写 `cluster-admin`。

MiniK8s 的 Website Operator 是同一模式的 Python 简化版，缺少 webhook/envtest/OLM；理解这里的顺序后，再看 `examples/website_operator.py` 会更容易。

## 本章小结

- CRD 注册类型，Operator 提供行为。
- Schema、status 子资源、printer columns 是良好 CRD 的基础。
- Operator 需要幂等、ownerReference、finalizer、leader election 和测试。
- 多版本 CRD 需要转换策略，webhook 是关键故障域。
- 不是所有场景都需要 Operator。
- Kubebuilder 的 scaffolds/manifests/envtest 把 CRD、RBAC、测试和部署串成可重复流程；顺序是 finalizer → 下级资源 → status。

## 延伸阅读

- [第 18 章 控制器模式](18-controller-pattern.md)
- [第 24 章 准入控制与供应链安全](24-admission-supply-chain.md)
- [第 36 章 调度器与控制器实现](36-scheduler-controllers.md)
- [附录 L 云原生生态地图](../appendices/L-ecosystem-map.md)：Operator 之外的平台组件如何分层。
- [附录 Q 扩展点与交付物开发实战](../appendices/Q-extension-development.md)：controller-runtime、envtest、webhook 与 Helm 交付的完整骨架。
