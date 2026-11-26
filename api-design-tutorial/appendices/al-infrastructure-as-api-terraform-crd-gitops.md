# 附录 AL：基础设施即 API——Terraform Provider、CRD 与 GitOps 契约

大多数 API 设计讨论的是数据平面：一次请求进来，返回一个响应。但还有一类 API，消费者不是终端用户，而是 CI、控制器和审计员；它们不关心“这一次调用返回什么”，而关心“系统最终是否收敛到我声明的状态”。这就是控制平面 API。

Terraform Provider、Kubernetes CRD 和 GitOps 是这类 API 的三种主要形态。它们的共同点是：**声明期望状态、观察实际状态、通过协调循环收敛、用版本和权限管理变更**。不同点在于状态归谁所有、谁来协调、失败如何暴露。很多团队把基础设施代码当作“脚本的集合”，结果得到的是不可重放、不可审计、无法处理漂移的自动化。本章把基础设施 API 当作契约来设计。

## AL.1 为什么基础设施 API 也是 API

控制平面 API 的消费者：

- **人**：平台工程师、SRE、应用团队；
- **CI**：`terraform plan`、`kubectl diff`、策略检查；
- **控制器**：Kubernetes operator、GitOps 同步器；
- **审计与合规**：谁在什么时候改变了什么，依据是什么；
- **Agent**：自动化运维、故障修复、容量调整。

与数据平面 API 的差异：

| 维度 | 数据平面 API | 控制平面 API |
|---|---|---|
| 调用语义 | 请求—响应 | 声明—收敛 |
| 成功标准 | 本次响应正确 | 最终状态一致 |
| 时间尺度 | 毫秒到秒 | 秒到小时 |
| 失败模式 | 超时、错误码 | 漂移、部分创建、孤儿资源 |
| 幂等 | 通常按请求 | 必须按状态 |
| 回滚 | 反向操作 | 反向声明、补偿、finalizer |
| 权限 | 终端用户 | 平台角色、服务账号、审批 |
| 审计 | 访问日志 | 变更记录、plan、Git 历史 |

控制平面 API 的核心契约不是“端点列表”，而是：

1. **资源身份**：这是什么对象，如何唯一标识；
2. **期望状态**：用户声明什么；
3. **实际状态**：系统观察到什么；
4. **生命周期**：创建、更新、删除、导入；
5. **收敛保证**：多久收敛、失败如何重试、如何报告；
6. **所有权**：谁可以改、谁负责清理。

## AL.2 声明式契约的五要素

一个设计良好的声明式资源通常包含：

```yaml
apiVersion: platform.example.com/v1
kind: Database
metadata:
  name: orders-db
  namespace: team-orders
spec:
  engine: postgres
  version: "16"
  size: small
  storageGB: 100
  highAvailability: true
status:
  observedGeneration: 3
  phase: Ready
  conditions:
    - type: Ready
      status: "True"
      reason: Provisioned
      message: "database is accepting connections"
      lastTransitionTime: "2026-11-26T10:00:00Z"
      observedGeneration: 3
  endpoint: orders-db.internal:5432
```

### 身份

- 资源类型（group/version/kind 或 provider 的 resource type）；
- 名称与命名空间；
- 外部 ID（云资源 ARN、数据库实例 ID）；
- 稳定 ID 格式，避免因重建而变化；
- 导入时如何从外部 ID 找到资源。

### spec

- 只表达期望，不混入观测值；
- 字段有明确默认值和校验；
- 可变字段与不可变字段区分清楚；
- 避免“空值 = 使用默认”与“空值 = 显式清空”的歧义；
- 对破坏性变更要求显式确认，而不是静默重建。

### status

- 由控制器/提供方写入，用户只读；
- 包含 `observedGeneration`，表明状态对应哪一版 spec；
- 用 conditions 表达多维度健康；
- 包含足够的诊断信息，但不泄露密钥；
- 状态更新要幂等、可重入。

### 生命周期

- 创建：幂等，重复创建返回同一资源或明确冲突；
- 更新：区分原地更新与替换；
- 删除：级联、保留、finalizer、超时；
- 导入：把已有资源纳入管理；
- 漂移：外部变更如何被发现和修复。

### 条件与事件

- conditions 用稳定的 `type`、`status`、`reason`；
- 事件用于短期诊断，不作为唯一状态来源；
- 错误要可操作：缺什么权限、哪个字段非法、下一步做什么；
- 不要用自由文本 `message` 作为机器判断依据。

## AL.3 Terraform Provider 契约

Terraform Provider 是“把云 API 翻译成声明式资源”的适配层。它的契约由 schema、CRUD、状态和 plan 语义共同组成。

### 资源与数据源

- **Resource**：可创建、更新、删除的对象；
- **Data Source**：只读查询，不进入状态管理；
- **Ephemeral Resource**（较新版本）：短期凭证等不落状态的数据；
- **Provider**：认证、端点、默认标签等全局配置。

### Schema 语义

下面的示例使用 Terraform Plugin SDKv2 语法（存量 Provider 仍很常见）。Plugin Framework 的概念相同，但 API 不同：资源实现 `resource.Resource` 的 `Create/Read/Update/Delete`，用 `planmodifier`、`validator` 和 `resp.State.RemoveResource(ctx)` 表达对应语义。迁移时不要混用两套 SDK。

```go
"size": {
    Type:     schema.TypeString,
    Required: true,
    ValidateDiagFunc: validation.ToDiagFunc(
        validation.StringInSlice([]string{"small", "medium", "large"}, false),
    ),
},
"storage_gb": {
    Type:     schema.TypeInt,
    Optional: true,
    Default:  100,
    ValidateDiagFunc: validation.ToDiagFunc(
        validation.IntBetween(10, 10000),
    ),
},
"endpoint": {
    Type:     schema.TypeString,
    Computed: true,
},
"engine": {
    Type:     schema.TypeString,
    Required: true,
    ForceNew: true, // 修改引擎会重建实例
},
"password": {
    Type:      schema.TypeString,
    Required:  true,
    Sensitive: true,
},
```

关键字段语义：

| 属性 | 含义 | 常见错误 |
|---|---|---|
| Required | 用户必须提供 | 把可由服务端推导的值设为必填 |
| Optional | 可选 | 没有默认值，导致 plan 噪声 |
| Computed | 服务端计算 | 用户可写又 Computed，语义混乱 |
| ForceNew | 变更触发重建 | 本可原地更新的字段被设为 ForceNew |
| Default | 默认值 | 默认值与服务端默认值不一致，产生永久 diff |
| Sensitive | 日志/输出脱敏 | 以为 Sensitive 会加密 state |
| DiffSuppressFunc | 忽略等价差异 | 滥用导致真实变更被忽略 |

### State 是契约的一部分

Terraform state 不只是缓存，它记录了“Terraform 认为存在什么”。因此：

- state 结构变化需要 `SchemaVersion` 和 `StateUpgraders`；
- state 中可能包含明文密钥，必须加密存储、限制访问；
- state 损坏或丢失会导致资源被遗忘或重复创建；
- 远程 backend 要有锁、版本控制和备份；
- 不要让多个工作区管理同一资源。

```go
func resourceDatabase() *schema.Resource {
    return &schema.Resource{
        SchemaVersion: 2,
        StateUpgraders: []schema.StateUpgrader{
            {
                Version: 0,
                Type:    resourceDatabaseV0().CoreConfigSchema().ImpliedType(),
                Upgrade: resourceDatabaseStateUpgradeV0,
            },
            {
                Version: 1,
                Type:    resourceDatabaseV1().CoreConfigSchema().ImpliedType(),
                Upgrade: resourceDatabaseStateUpgradeV1,
            },
        },
        // ...
    }
}
```

### Read 与漂移

`Read` 必须反映远端真实状态：

- 用资源 ID 查询远端；
- 把远端值写回 state；
- 如果资源已不存在，调用 `RemoveResource` 从 state 移除；
- 不要用配置值覆盖远端值，否则永远发现不了漂移；
- 对最终一致的 API，要处理“创建后暂时读不到”的情况。

```go
func resourceDatabaseRead(d *schema.ResourceData, meta interface{}) error {
    client := meta.(*Client)
    id := d.Id()

    db, err := client.GetDatabase(context.Background(), id)
    if isNotFound(err) {
        d.SetId("") // 从 state 移除；下次 plan 会按配置重新创建
        return nil
    }
    if err != nil {
        return fmt.Errorf("read database %s: %w", id, err)
    }

    d.Set("endpoint", db.Endpoint)
    d.Set("status", db.Status)
    return nil
}
```

### 幂等与 plan 语义

一个健康的 provider 应该满足：

```text
apply 后立即 plan → 无差异
```

常见破坏幂等的因素：

- 服务端会规范化字段（大小写、空白、顺序），provider 未做等价处理；
- 默认值与服务端默认值不一致；
- 创建后返回的字段与读取时不同；
- 时间戳、ETag 等易变字段被写入 schema；
- 列表顺序不稳定；
- 浮点数精度差异。

`CustomizeDiff` 可以在 plan 阶段做校验和计算，但不能替代 Read：

```go
CustomizeDiff: func(ctx context.Context, d *schema.ResourceDiff, meta interface{}) error {
    if d.HasChange("storage_gb") && d.Get("engine").(string) == "sqlite" {
        return fmt.Errorf("sqlite does not support storage_gb changes")
    }
    return nil
},
```

### Import

已有资源必须能纳入管理，否则迁移只能“删了重建”：

```bash
terraform import example_database.orders orders-db-prod
```

要求：

- import ID 格式有文档；
- 复合 ID 用明确分隔符，或使用 JSON/URL 编码；
- import 后 `plan` 应尽量为空；
- import 不触发创建/更新；
- 对无法导入的资源明确说明。

```go
Importer: &schema.ResourceImporter{
    StateContext: schema.ImportStatePassthroughContext,
},
```

### 超时、重试与限流

- 每个资源支持 `timeouts` 块；
- 创建/更新/删除分别设置超时；
- 对 429/5xx 做带抖动的重试，尊重 `Retry-After`；
- 不要无限重试；
- 对长时间操作使用等待器，而不是固定 sleep；
- 记录请求 ID，便于和云厂商支持对接。

### 版本与弃用

- Provider 遵循 SemVer；
- schema 字段弃用用 `Deprecated` 标记，并在文档中给替代方案；
- 删除字段是破坏性变更，需要主版本升级；
- 资源类型重命名需要状态迁移或别名；
- 维护 changelog 和升级指南；
- 对 Terraform 核心协议版本变化保持兼容。

### 测试

| 层次 | 内容 | 工具 |
|---|---|---|
| 单元 | schema 校验、diff 逻辑、状态转换 | Go test |
| 契约 | 请求/响应符合云 API | httptest、mock |
| 验收 | 真实创建/更新/删除/导入 | `terraform-plugin-testing` |
| 漂移 | 外部修改后 plan 能发现 | 验收测试 + 手动变更 |
| 升级 | 旧 state 能升级 | StateUpgrader 测试 |

验收测试必须覆盖：

- 创建后 plan 为空；
- 更新可变字段；
- 替换不可变字段；
- 导入后 plan 为空；
- 删除后资源确实消失；
- 资源不存在时 Read 不报错；
- 权限不足、限流、超时的错误信息可操作。

### 常见 bug

| 症状 | 根因 | 修复 |
|---|---|---|
| 每次 plan 都有 diff | Read 未反映远端/默认值不一致 | 对齐默认值，规范化等价字段 |
| 修改一个字段导致重建 | 误设 ForceNew | 改为原地更新，或用 CustomizeDiff 校验 |
| 创建后读不到 | 最终一致，Read 太快 | 等待器/重试，区分“未就绪”和“不存在” |
| 删除后 state 仍有资源 | Read 未 RemoveResource | 检测 404 并移除 |
| 密钥出现在日志 | 未标记 Sensitive | 标记 Sensitive，审查输出 |
| 导入后 plan 巨大 | 导入未填充计算字段 | 导入时读取远端完整状态 |
| 多工作区互相覆盖 | 同一资源被多个 state 管理 | 明确所有权，拆分工作区 |

## AL.4 Kubernetes CRD 契约

CRD 把 Kubernetes API 的声明式模型开放给自定义资源。它不只是一个 YAML schema，而是一整套 API 契约：版本、校验、默认值、状态、删除和权限。

### 资源定义

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: databases.platform.example.com
spec:
  group: platform.example.com
  scope: Namespaced
  names:
    kind: Database
    plural: databases
    singular: database
    shortNames: [db]
    categories: [platform]
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          properties:
            spec:
              type: object
              required: [engine, version]
              properties:
                engine:
                  type: string
                  enum: [postgres, mysql]
                version:
                  type: string
                  pattern: '^[0-9]+(\.[0-9]+)?$'
                size:
                  type: string
                  enum: [small, medium, large]
                  default: small
                storageGB:
                  type: integer
                  minimum: 10
                  maximum: 10000
                  default: 100
                highAvailability:
                  type: boolean
                  default: false
            status:
              type: object
              properties:
                phase:
                  type: string
                endpoint:
                  type: string
                observedGeneration:
                  type: integer
      subresources:
        status: {}
      additionalPrinterColumns:
        - name: Engine
          type: string
          jsonPath: .spec.engine
        - name: Phase
          type: string
          jsonPath: .status.phase
        - name: Age
          type: date
          jsonPath: .metadata.creationTimestamp
```

### 校验

三层校验：

1. **结构 schema**：类型、必填、枚举、范围、正则；
2. **CEL 规则**：跨字段约束，如“高可用至少需要 medium”；
3. **准入 webhook**：需要外部查询、复杂默认值或跨资源校验时使用。

```yaml
x-kubernetes-validations:
  - rule: "self.highAvailability ? self.size != 'small' : true"
    message: "highAvailability requires size medium or large"
```

原则：

- 能在 schema/CEL 表达的，不要放到 webhook；
- webhook 可能不可用或被绕过，关键约束要有第二道防线；
- 校验错误要指向具体字段；
- 默认值要写在 CRD schema 中，避免 webhook 默认化导致 Git 与集群不一致；
- 对已存在对象的 schema 变更要考虑 ratcheting，避免旧对象突然非法。

### 版本化

```yaml
versions:
  - name: v1alpha1
    served: true
    storage: false
  - name: v1
    served: true
    storage: true
```

要求：

- 只有一个 storage version；
- 所有 served version 都能读写；
- 版本间通过 conversion webhook 转换；
- 转换必须可逆、无信息丢失，或有明确的有损规则；
- hub/spoke 模型：一个 hub 版本，其他版本转换为 hub；
- 对转换做 round-trip fuzz 测试；
- 弃用版本要有 `deprecationWarning` 和移除时间表；
- 存储版本升级要先迁移所有对象，再切换。

### 状态与条件

```yaml
status:
  observedGeneration: 3
  phase: Ready
  conditions:
    - type: Ready
      status: "True"
      reason: Provisioned
      message: "database is accepting connections"
      lastTransitionTime: "2026-11-26T10:00:00Z"
      observedGeneration: 3
```

要求：

- `observedGeneration` 与 `metadata.generation` 对应；
- conditions 的 `type` 稳定，`reason` 机器可读；
- `lastTransitionTime` 只在状态变化时更新；
- 不要用 phase 表达所有维度，conditions 更适合多维度健康；
- status 更新失败要重试，不能静默丢弃。

### Finalizer 与删除

Finalizer 让控制器在对象删除前完成清理：

```yaml
metadata:
  finalizers:
    - platform.example.com/database-cleanup
```

风险：

- finalizer 没有超时，对象永久卡在 Terminating；
- 控制器崩溃，没人移除 finalizer；
- 外部资源已删除，finalizer 仍在等待；
- 多控制器争抢 finalizer。

要求：

- 清理逻辑幂等；
- 有超时和强制移除流程；
- 明确谁负责移除 finalizer；
- 删除保护（如 `prevent_destroy` 等价物）要可审计；
- 孤儿资源有对账扫描。

### 控制器循环

```text
for {
  desired := readSpec()
  actual := observe()
  if desired != actual {
    reconcile(desired, actual)
  }
  waitForChange()
}
```

控制器设计要求：

- **水平触发**：基于当前状态，而不是事件次数；
- **幂等**：重复协调不产生重复副作用；
- **可重入**：中途失败后能继续；
- **有界重试**：指数退避，避免热循环；
- **并发控制**：同一资源串行，不同资源可并行；
- **所有权**：用 ownerReferences 管理从属资源；
- **可观测**：暴露协调次数、耗时、错误、队列深度；
- **避免脑裂**：多副本选主或分片。

### 安全与多租户

- CRD 的 RBAC 要细到 verb/resource/namespace；
- 集群级 CRD 风险更高，尽量用命名空间级；
- 准入策略限制危险字段（如宿主机挂载、特权）；
- 控制器使用最小权限 service account；
- 防止租户通过 CRD 让控制器访问其他租户资源；
- 审计所有 spec 变更；
- 对 `kubectl apply`、GitOps、控制器写入的字段所有权要清晰。

### 规模

- etcd 单对象有大小限制，避免把大数组塞进 spec；
- 高频更新 status 会增加 etcd 压力；
- watch 缓存和 informer 内存随对象数增长；
- 大量小对象可能导致控制器队列积压；
- 分片、聚合和归档要提前设计。

## AL.5 GitOps 作为发布契约

GitOps 把 Git 当作期望状态的唯一来源，由同步器持续把集群收敛到 Git。

### 核心契约

- Git 中的清单是期望状态；
- 集群是实际状态；
- 同步器负责 diff 和收敛；
- PR 是变更评审；
- commit 是审计记录；
- 回滚是 revert 或切换到旧 revision。

### 同步策略

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: orders
spec:
  project: default
  source:
    repoURL: https://github.com/example/platform.git
    path: apps/orders/overlays/prod
    targetRevision: main
  destination:
    server: https://kubernetes.default.svc
    namespace: orders
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - PruneLast=true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
```

关键选择：

- `prune`：Git 中删除的资源是否从集群删除；
- `selfHeal`：集群被手动修改后是否自动恢复；
- `PruneLast`：先创建新资源再删除旧资源；
- 同步窗口：避免在业务高峰自动同步；
- 自动同步与人工审批的边界。

### 密钥

不要把明文密钥放进 Git：

- **SOPS**：加密文件，密钥由 KMS/ age 管理；
- **Sealed Secrets**：集群内控制器解密；
- **External Secrets Operator**：从 Vault/云密钥服务同步；
- **Secrets Store CSI Driver**：挂载为卷。

要求：

- 加密密钥的权限与轮换；
- Git 历史中不能有明文密钥；
- 密钥同步失败要告警；
- 轮换后工作负载能重启/热加载；
- 审计谁读取了密钥。

### 环境晋级

```text
base/ + overlays/dev + overlays/staging + overlays/prod
```

或 Helm values：

```text
charts/orders/values.yaml
charts/orders/values-staging.yaml
charts/orders/values-prod.yaml
```

原则：

- 环境差异显式化；
- 镜像用 digest 固定，不用可变 tag；
- 配置与代码版本关联；
- 晋级是 PR，不是手动改集群；
- 生产变更需要审批和观察窗口。

### 回滚

- `git revert` 触发同步；
- 或把 Application 指向旧 revision；
- 数据库迁移通常不可逆，回滚代码不等于回滚数据；
- 回滚要验证 CRD/schema 兼容；
- 回滚后要防止自动同步再次推进。

### 多集群

- 集群注册表；
- 按环境/地区/租户选择集群；
- 应用集（ApplicationSet）生成；
- 漂移检测和批量同步；
- 故障隔离，避免一个集群拖垮全局；
- 集群级资源与命名空间级资源分开管理。

### 策略即代码

- OPA/Gatekeeper、Kyverno、ValidatingAdmissionPolicy；
- 限制镜像仓库、特权、资源配额、标签；
- 策略也要版本化和测试；
- 策略拒绝要有可操作的错误；
- 例外有到期时间和审批。

## AL.6 三种控制平面对照

| 维度 | REST 控制平面 | Kubernetes CRD | Terraform Provider |
|---|---|---|---|
| 状态所有者 | 服务端 | etcd + 控制器 | Terraform state + 远端 |
| 协调者 | 服务端/异步任务 | 控制器 | Terraform apply |
| 声明式 | 可选 | 强 | 强 |
| 漂移检测 | 需自建 | 控制器持续 | plan/refresh 时 |
| 权限模型 | OAuth/RBAC | Kubernetes RBAC | 云 IAM + state 权限 |
| 版本化 | API 版本 | CRD version + conversion | Provider 版本 + schema |
| 测试 | 契约/集成 | envtest/kind | 单元/验收 |
| 爆炸半径 | 取决于 API | 集群级可能很大 | 取决于 workspace |
| 适合 | SaaS、跨云 | 集群内资源 | 多云/外部资源 |

### 何时建哪种

- **SaaS 产品**：REST 控制平面 + 异步任务 + Webhook；
- **Kubernetes 原生平台**：CRD + operator；
- **多云/混合云**：Terraform Provider；
- **混合**：Terraform 创建集群和基础网络，CRD 管理集群内资源，REST 管理 SaaS 侧配置。

不要用 Terraform 管理高频变化的集群内对象；也不要用 CRD 管理跨云资源，除非你愿意写一个完整的云控制器。

## AL.7 漂移、对账与所有权

### 三种状态

```text
期望状态（Git/spec）
实际状态（远端/集群）
记录状态（Terraform state/status）
```

漂移是三者不一致。检测方式：

- Terraform：`plan`/`refresh`；
- Kubernetes：控制器持续观察；
- GitOps：同步器定期 diff；
- 自建：定期对账任务。

### 漂移处理模式

| 模式 | 行为 | 适用 |
|---|---|---|
| 仅报告 | 产生告警/工单 | 生产核心资源 |
| 自动修复 | 收敛到期望 | 非关键、幂等资源 |
| 手动批准 | 人工确认后修复 | 高风险/破坏性变更 |
| 忽略 | 明确豁免 | 外部管理的字段 |

### 所有权冲突

常见冲突：

- GitOps 与 Terraform 管理同一对象；
- 手动 `kubectl edit` 与 selfHeal 冲突；
- HPA/VPA 修改副本数与 Git 声明冲突；
- 控制器写入 status，Git 也声明 status；
- 两个 operator 争抢同一 finalizer。

解决：

- 明确字段所有权；
- 用 server-side apply 的 field manager；
- GitOps 只管理 spec，不管理 status；
- 自动扩缩容字段从 Git 中移除或使用 ignoreDifferences；
- 一个资源只有一个创建者；
- 冲突检测和告警。

### 导入与收养

- 已有资源先导入，再纳入声明式管理；
- 导入后 plan 应为空；
- 对无法导入的资源，明确“外部管理”边界；
- 孤儿资源定期扫描；
- 删除保护避免误删。

## AL.8 权限、租户与供应链

### 权限

- 谁能 plan，谁能 apply；
- 生产 apply 是否需要审批；
- 工作区/命名空间隔离；
- 云 IAM 权限最小化；
- 控制器 service account 最小权限；
- 审计谁在什么时候改了什么。

### 密钥与状态

- Terraform state 可能包含明文密钥；
- 使用加密 backend、访问控制、审计；
- 不要把 state 提交到 Git；
- 不要把密钥放进 CRD spec；
- 使用 External Secrets/Secret 引用；
- 日志和 plan 输出要脱敏。

### 供应链

- Provider 从官方 registry 安装；
- 锁定版本和校验和（`.terraform.lock.hcl`）；
- 验证签名；
- 审查第三方 provider 权限；
- 镜像用 digest，启用签名验证；
- 策略阻止未签名/不可信来源；
- 定期扫描漏洞。

## AL.9 测试与验证

| 对象 | 测试 | 工具 |
|---|---|---|
| Terraform schema | 校验、diff、状态升级 | Go test |
| Terraform provider | 请求/响应契约 | httptest/mock |
| Terraform 资源 | 创建/更新/删除/导入/漂移 | acceptance test |
| CRD schema | 合法/非法对象、默认值、CEL | envtest、API server |
| CRD 转换 | round-trip、无损 | fuzz test |
| 控制器 | 协调幂等、错误重试、finalizer | fake client、envtest |
| GitOps | diff、同步、回滚、漂移 | kind、临时集群 |
| 策略 | 允许/拒绝矩阵 | conftest、Kyverno test |

### 失败注入

- 云 API 超时、429、500；
- 创建成功但读取失败；
- 部分资源创建后失败；
- 控制器崩溃/重启；
- Git 不可用；
- webhook 不可用；
- 权限被撤销；
- 配额耗尽；
- 删除时外部依赖仍在。

## AL.10 反模式

- 把命令式脚本包一层就叫 provider；
- Read 不反映远端，永久 diff；
- 所有字段都 ForceNew；
- 默认值与服务端不一致；
- state 明文存密钥并提交 Git；
- 多个工作区管理同一资源；
- CRD 只靠 webhook 校验，webhook 挂了就绕过；
- 默认值放在 webhook，Git 与集群不一致；
- 多版本 CRD 没有 conversion；
- finalizer 没有超时，对象永久 Terminating；
- 控制器热循环，打爆 API server；
- GitOps 自动 prune 删掉手动创建的资源；
- GitOps 与 Terraform 争抢同一对象；
- 镜像用 `latest` tag；
- 密钥明文进 Git；
- 只在 happy path 测试；
- 没有导入路径，迁移只能重建；
- 删除保护缺失，一次误操作删库。

## AL.11 检查清单

### Terraform Provider

- [ ] 资源有稳定 ID 和导入路径。
- [ ] Read 反映远端，资源不存在时从 state 移除。
- [ ] 创建/更新后 plan 为空。
- [ ] 可变/不可变字段区分正确，ForceNew 有理由。
- [ ] 默认值与服务端一致，避免永久 diff。
- [ ] 支持 state 版本升级。
- [ ] 超时、重试、限流有边界。
- [ ] 有验收测试覆盖创建/更新/替换/导入/删除。
- [ ] 密钥标记 Sensitive，state 加密存储。
- [ ] Provider 版本和 schema 变更遵循 SemVer。

### Kubernetes CRD

- [ ] group/version/kind、plural、scope、shortNames 明确。
- [ ] 结构 schema、CEL、准入三层校验分工合理。
- [ ] 默认值在 CRD schema 中，不依赖 webhook。
- [ ] 多版本有 conversion，round-trip 测试通过。
- [ ] 有且只有一个 storage version。
- [ ] status 有 observedGeneration 和稳定 conditions。
- [ ] finalizer 幂等、有超时、有强制清理流程。
- [ ] 控制器水平触发、幂等、有界重试、可观测。
- [ ] RBAC 最小权限，多租户隔离。
- [ ] 对象大小和更新频率在 etcd 承受范围内。

### GitOps

- [ ] Git 是期望状态唯一来源，PR 是评审入口。
- [ ] prune/selfHeal 策略与业务风险匹配。
- [ ] 密钥加密，Git 历史无明文。
- [ ] 镜像用 digest，环境差异显式。
- [ ] 晋级和回滚有流程、有观察窗口。
- [ ] 策略即代码，例外有期限。
- [ ] 多集群隔离和故障域清晰。
- [ ] 漂移有检测、告警和处理模式。
- [ ] 字段所有权明确，避免 GitOps/Terraform/手动互相覆盖。

## AL.12 小结

- 控制平面 API 的契约是“声明—收敛”，不是“请求—响应”。
- 声明式资源要设计身份、spec、status、生命周期、条件和所有权。
- Terraform Provider 的核心是 schema、Read、幂等、导入和 state 升级；永久 diff 通常是 Read 或默认值问题。
- CRD 的核心是版本、校验、conversion、status、finalizer 和控制器循环。
- GitOps 把 Git 变成期望状态和审计记录，但要处理密钥、晋级、回滚和字段所有权。
- 漂移不可避免，关键是检测、归因和选择“报告/自动修复/人工批准”。
- 基础设施 API 的爆炸半径更大，权限、审批、删除保护和供应链安全必须前置。
- 最好的控制平面是让正确的事情容易做、错误的事情难以做、失败可以恢复。

## 设计练习

为一个“内部数据库平台”设计控制平面 API：

- 开发者声明数据库引擎、版本、规格、高可用、备份策略；
- 平台在云上创建实例、网络、密钥、监控；
- 支持导入已有实例；
- 支持版本升级和回滚；
- 支持多租户和配额；
- 支持 GitOps 和 Terraform 两种入口；
- 删除前需要保护；
- 需要审计和成本归因。

回答：

1. 资源身份和外部 ID 如何设计？
2. spec 中哪些字段不可变？变更时是重建还是原地升级？
3. status 如何表达“创建中/就绪/降级/失败”？
4. Terraform 与 GitOps 的字段所有权如何划分？
5. 删除时如何避免孤儿资源和误删？
6. 如何测试漂移、部分失败和控制器重启？
7. 如果只能先做三件事，优先做哪三件？

## 相关章节

- [第 10 章 写操作、幂等、并发与一致性](../part-1-foundations/10-writes-idempotency-concurrency.md)
- [第 12 章 异步任务与长时操作](../part-1-foundations/12-async-and-long-running.md)
- [第 27 章 API 网关与边缘架构](../part-3-protocols-architecture/27-gateway-and-edge.md)
- [第 28 章 服务网格、东西向流量与内部 API](../part-3-protocols-architecture/28-service-mesh-internal-apis.md)
- [第 37 章 API 治理、评审与平台化](../part-4-engineering-product/37-governance.md)
- [第 44 章 内部平台与平台工程 API](../part-5-domain-playbooks/44-internal-platform.md)
- [附录 A API 设计检查清单](a-design-checklist.md)
- [附录 M API 设计模式与反模式速查](m-patterns-antipatterns.md)
- [附录 R API 实现架构与框架选型](r-implementation-architecture.md)
- [附录 X 遗留 API 现代化与迁移实战](x-legacy-modernization.md)
- [附录 Z 分布式一致性与多区域 API 设计](z-distributed-consistency-multi-region.md)

---

返回：[目录](../SUMMARY.md)
