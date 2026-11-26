# 第 44 章 内部平台与平台工程 API

## 44.1 内部 API 也是产品

内部平台 API 的消费者是：

- 产品团队；
- 数据团队；
- SRE；
- 安全团队；
- 自动化系统；
- AI Agent。

如果内部 API 难用，团队会：

- 绕过平台；
- 自建影子系统；
- 复制粘贴；
- 把成本转嫁给平台团队。

因此内部平台需要产品思维：

- 用户是谁；
- 他们的任务；
- 上手成本；
- 自助能力；
- 反馈循环；
- 采用率。

### 内部平台的悖论

- 平台团队认为“我们提供了能力”；
- 业务团队认为“用平台比自己搭更慢”。

如果平台不能降低总成本，团队就会绕过它。

## 44.2 平台的核心能力

```text
Developer Portal
├── Service Catalog
├── API Catalog
├── Templates
├── Self-service Provisioning
├── CI/CD
├── Observability
├── Secrets
├── Policy
└── Cost Visibility
```

### 平台能力的优先级

1. 服务目录（知道有什么）；
2. 黄金路径（快速创建）；
3. 自助部署；
4. 可观测性；
5. 密钥管理；
6. 策略；
7. 成本可见性。

## 44.3 黄金路径

黄金路径（Golden Path）是“默认最好走的路”：

```text
创建服务 → 选模板 → 自动生成仓库 → CI/CD → 部署 → 监控 → 告警 → 文档
```

好的黄金路径：

- 默认安全；
- 默认可观测；
- 默认合规；
- 少配置；
- 可偏离；
- 有文档；
- 有支持。

### 黄金路径 vs 强制

- 黄金路径是“默认”，不是“唯一”；
- 团队可以偏离，但要承担成本；
- 偏离应可见；
- 定期复盘。

## 44.4 服务目录

```json
{
  "name": "order-service",
  "owner": "team-orders",
  "lifecycle": "production",
  "tier": 1,
  "language": "go",
  "repository": "...",
  "apis": ["orders-v1"],
  "dependencies": ["payment-service", "inventory-service"],
  "slos": { "availability": 99.95 },
  "oncall": "..."
}
```

用途：

- 所有权；
- 依赖分析；
- 故障影响；
- 合规；
- 成本；
- 文档。

### 目录的自动化

- 从 CI 自动注册；
- 从 Kubernetes 发现；
- 从网关发现；
- 从代码扫描；
- 定期校验；
- 过期清理。

## 44.5 自助式 API

把“读目录”和“提交变更”分开，避免多个写入口竞争：

```text
GET  /services                      # 只读服务目录
GET  /services/{name}
POST /service-requests              # 唯一的自助写入口
GET  /service-requests/{id}
POST /service-requests/{id}/cancel
```

服务创建、数据库、密钥、环境等资源都通过 `service-requests` 的请求体表达，而不是为每种资源再开一个顶层写端点。这样审批、配额、审计、幂等和异步任务只需要实现一次。

要求：

- 审批自动化；
- 配额；
- 策略；
- 审计；
- 幂等；
- 异步任务；
- 回滚；
- 成本可见。

### 自助的边界

- 低风险：自动批准；
- 中风险：策略检查；
- 高风险：人工审批；
- 紧急：事后审计。

### 自助开通流程

```text
1. 用户提交请求（服务名、模板、环境、资源）
2. 校验命名/配额/策略
3. 自动批准或转人工
4. 创建资源（异步）
5. 生成仓库/CI/部署
6. 返回状态与访问方式
7. 审计与成本归因
```

每一步都应：

- 幂等；
- 可查询；
- 可取消；
- 可回滚；
- 有错误码；
- 有审计。

### 申请资源的 API

```http
POST /service-requests
Idempotency-Key: ...
```

```json
{
  "serviceName": "orders",
  "owner": "team-orders",
  "template": "go-http-api",
  "environment": "staging",
  "resources": {
    "database": { "engine": "postgres", "size": "small" },
    "cache": { "engine": "redis", "size": "small" }
  }
}
```

响应：

```json
{
  "id": "req_123",
  "status": "PENDING_APPROVAL",
  "links": {
    "self": "/service-requests/req_123",
    "cancel": "/service-requests/req_123/cancel"
  }
}
```

## 44.6 控制平面 vs 数据平面

- **控制平面**：创建/配置/策略；
- **数据平面**：实际请求处理。

平台 API 通常是控制平面：

```text
POST /clusters
POST /deployments
PATCH /config
```

控制平面要：

- 声明式；
- 幂等；
- 最终一致；
- 可观测；
- 可回滚；
- 防误操作。

### 控制平面的设计

- `spec` 表达期望；
- `status` 表达实际；
- 控制器收敛；
- 事件；
- 条件；
- finalizer。

### 控制器循环示例

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

关键：

- 幂等；
- 可重入；
- 失败重试；
- 事件驱动；
- 状态可观测；
- 不依赖一次性脚本。

### 自助开通的状态机

`service-request` 不应是一个长时间“转圈”的同步调用。它需要完整状态机：

```text
PENDING_APPROVAL → APPROVED → PROVISIONING → PROVISIONED
        │              │            │
        ├→ REJECTED    ├→ FAILED    ├→ FAILED
        └→ CANCELLED   └→ CANCELLED └→ CANCELLED
PROVISIONED → DEPROVISIONING → DEPROVISIONED
```

每个状态：

- 可查询；
- 可审计；
- 有错误码与失败原因；
- 支持取消（在允许的状态）；
- 支持重试/回滚；
- 有超时与补偿。

幂等：

- 请求 ID + `Idempotency-Key`；
- 重复提交返回同一请求；
- 部分资源已创建时，重试要能继续而不是重复创建；
- 失败后清理已创建资源，或标记为可恢复。

控制平面还要防止“孤儿资源”：请求失败但数据库/队列/云资源已创建。用 finalizer、补偿任务和对账扫描收尾。

## 44.7 Kubernetes 风格 API

Kubernetes 的 API 设计值得借鉴：

- 资源（Pod、Service、Deployment）；
- 声明式 spec/status；
- 控制器循环；
- 标签/选择器；
- 命名空间；
- 准入控制；
- watch；
- finalizer。

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders
spec:
  replicas: 3
  template: ...
status:
  readyReplicas: 3
```

平台 API 可以复用 44.6 的控制平面模式（`spec`/`status`/控制器/事件/条件），本节只关注 Kubernetes 风格的具体映射，不再重复同一组概念。

### 声明式 API 的好处

- 幂等；
- 可重放；
- 可 diff；
- 可审计；
- 可 GitOps；
- 最终一致。

### 声明式 API 的代价

- 收敛延迟；
- 状态复杂；
- 冲突处理；
- 调试困难。

### Terraform Provider、CRD 与 GitOps 的分工

平台控制平面通常不是一种技术，而是三种契约的组合：

| 形态 | 管理对象 | 状态所有者 | 适合 |
|---|---|---|---|
| Terraform Provider | 多云/外部资源、集群与网络 | Terraform state + 远端 | 基础设施生命周期 |
| Kubernetes CRD | 集群内资源 | etcd + 控制器 | 应用与集群内平台能力 |
| GitOps | 期望状态的分发与审计 | Git + 同步器 | 持续部署与漂移修复 |

常见分工是：Terraform 创建集群、网络、数据库等基础资源；CRD/Operator 管理集群内能力；GitOps 把应用清单和策略持续同步到集群。三者最容易冲突的地方是**字段所有权**：同一个对象被 Terraform 和 GitOps 同时管理，或控制器写入的 status 被 Git 反复覆盖。设计时必须明确“谁创建、谁改 spec、谁写 status、谁负责删除”，并用 server-side apply 的 field manager、`ignoreDifferences` 或独立资源边界隔离。

Terraform 的 schema/Read/幂等/导入/state 升级，CRD 的版本/conversion/status/finalizer/控制器循环，以及 GitOps 的密钥/晋级/回滚/漂移，完整设计见[附录 AL](../appendices/al-infrastructure-as-api-terraform-crd-gitops.md)。

## 44.8 模板与脚手架

模板不是另一个写端点，而是 `service-request` 的一个字段：

```http
POST /service-requests
Idempotency-Key: ...
```

```json
{
  "template": "go-http-api",
  "serviceName": "orders",
  "owner": "team-orders",
  "environment": "staging",
  "options": {
    "database": "postgres",
    "observability": true
  }
}
```

生成：

- 仓库；
- CI；
- Dockerfile；
- Helm chart；
- OpenAPI 骨架；
- 监控；
- 告警；
- 文档。

### 模板的维护

- 版本化；
- 测试；
- 更新；
- 弃用；
- 迁移。

## 44.9 平台 SLO

平台 API 也有 SLO：

- 创建服务的时间；
- 部署成功率；
- 控制平面可用性；
- 配置生效时间；
- 支持响应时间。

平台故障会阻塞所有团队，影响面大。

### 平台 SLO 的特殊性

- 影响面广；
- 依赖多；
- 恢复慢；
- 需要变更管理；
- 需要容量规划。

## 44.10 采用与治理

- 采用率；
- 黄金路径覆盖率；
- 绕过率；
- 工单量；
- 开发者满意度；
- 迁移进度；
- 创建服务时间；
- 部署频率。

治理：

- 策略即代码；
- 准入控制；
- 例外；
- 审计；
- 成本。

### 采用指标

```text
采用率 = 使用平台的服务数 / 总服务数
黄金路径覆盖率 = 本季度通过模板创建的新服务数 / 本季度新建服务数
存量覆盖率     = 使用黄金路径的服务数 / 总服务数
绕过率         = 绕过平台的服务数 / 总服务数
```

分子与分母必须使用同一个时间窗口。用“历史累计使用模板的服务数”除以“本季度新服务数”会得到大于 100% 的假指标。

## 44.11 反模式

- 平台团队闭门造车；
- 只提供 UI，不提供 API；
- 强制所有团队使用但体验差；
- 没有自助；
- 审批链过长；
- 平台 API 无版本；
- 控制平面无幂等；
- 没有 SLO；
- 绕过平台无人知；
- 把平台当项目而不是产品；
- 模板不更新；
- 平台 API 与 UI 行为不一致。

## 44.12 本章小结

- 内部平台 API 也是产品，需要用户研究、采用和反馈。
- 黄金路径降低认知负担。
- 服务目录、模板、自助、策略、可观测性是核心。
- 控制平面要声明式、幂等、最终一致、可回滚。
- Kubernetes 风格 API 提供了可借鉴的模式。
- Terraform、CRD 与 GitOps 是三种控制平面契约，关键在字段所有权与漂移处理，见[附录 AL](../appendices/al-infrastructure-as-api-terraform-crd-gitops.md)。
- 平台需要 SLO 和采用指标。

## 设计练习

为“公司内部 PaaS”设计 API：

- 创建服务；
- 配置数据库；
- 部署；
- 查看日志/指标；
- 申请密钥；
- 服务目录；
- 成本；
- 审批。

写出资源模型、控制平面 API 和黄金路径。然后回答：

1. 如何让自助创建既安全又快速？
2. 如何衡量平台采用率？
3. 如何防止团队绕过平台？
4. 平台故障时如何降级？

---

上一章：[数据平台与分析 API](43-data-platform.md)  
下一章：[LLM API 设计：流式、Token、工具与结构化输出](../part-6-ai-future/45-llm-apis.md)
