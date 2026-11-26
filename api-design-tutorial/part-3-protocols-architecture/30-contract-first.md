# 第 30 章 契约优先、代码生成与 Schema 治理

## 30.1 契约优先不是“先写 YAML”

契约优先（contract-first）意味着：

1. 先定义 API 的接口、数据、错误和行为；
2. 让消费者与提供方基于同一份契约开发；
3. 用工具验证实现是否符合契约；
4. 把契约作为版本化、评审和发布的中心。

它不是指“必须手写 OpenAPI”。你也可以从类型、Protobuf、GraphQL SDL 或领域模型生成契约。关键是**契约在实现之前被明确，并成为共同真相**。

### 契约优先的收益

- 消费者可并行开发；
- 设计评审更早；
- 错误路径被覆盖；
- 代码生成；
- mock；
- 文档；
- 破坏性变更检测；
- 契约测试。

### 契约优先的成本

- 前期时间；
- 工具链；
- 团队学习；
- 契约漂移；
- 维护成本。

## 30.2 设计优先 vs 代码优先

### 设计优先

先写 OpenAPI / Proto / SDL，再生成代码和文档。

优点：

- 消费者可以并行开发；
- 评审更早；
- 契约更完整；
- 代码生成；
- mock 可用。

缺点：

- 前期成本；
- 契约与实现可能漂移；
- 工具链要求；
- 设计者需要懂实现。

### 代码优先

先写实现，再从代码生成契约。

优点：

- 上手快；
- 契约与实现同步；
- 适合小型团队。

缺点：

- 契约容易被实现细节污染；
- 评审滞后；
- 难以消费者驱动；
- 生成质量依赖框架；
- 错误路径容易缺失。

### 务实选择

- 公开/合作伙伴 API：设计优先；
- 内部小型服务：代码优先 + 严格 lint；
- gRPC：Proto 天然契约优先；
- GraphQL：SDL 设计优先；
- 事件：AsyncAPI/Schema 设计优先。

## 30.3 契约的单一真相

一个 API 可能有多份“契约”：

- OpenAPI 文件；
- 代码中的 DTO；
- 数据库 schema；
- 文档；
- SDK；
- 测试。

如果它们各自演化，就会漂移。需要指定**单一真相**：

```text
OpenAPI / Proto / SDL → 生成代码、文档、mock、SDK
```

或者：

```text
代码 → 生成 OpenAPI → 生成文档/SDK
```

无论哪个方向，都要有 CI 检查漂移。

### 漂移检测

- 生成物是否与源一致；
- 文档是否与 schema 一致；
- SDK 是否与 schema 一致；
- 实现是否通过契约测试；
- 网关配置是否与 schema 一致。

## 30.4 OpenAPI 实践

### 结构

```yaml
openapi: 3.1.0
info:
  title: Orders API
  version: 1.0.0
servers:
  - url: https://api.example.com/v1
paths:
  /orders:
    post:
      operationId: createOrder
      ...
components:
  schemas:
    Order:
      ...
```

### 设计要点

- 每个 operation 有稳定 `operationId`（用于代码生成）；
- 复用 `components`，避免复制；
- 定义错误响应；
- 定义认证 scheme；
- 定义分页参数；
- 使用 `examples`；
- 使用 `oneOf`/`discriminator` 表达多态；
- 用 `deprecated: true` 标记弃用；
- 使用 JSON Schema 2020-12 特性（OpenAPI 3.1/3.2）；
- 定义 `security`；
- 定义 `servers`；
- 定义错误格式。

### OpenAPI 3.1 与 3.2 的变化

OpenAPI 3.1：

- 完全兼容 JSON Schema 2020-12；
- `nullable` 改为 `type: ["string", "null"]`；
- 支持 `$schema`；
- 支持 `webhooks`；
- 更好的示例支持。

OpenAPI 3.2（2025 年 9 月，向后兼容 3.1）：

- 流式媒体类型（SSE、JSONL、multipart/mixed）可以用 `itemSchema` 描述；
- 可以描述 RFC 10008 的 `QUERY` 方法；
- Tag 支持层级（`parent`、`kind`、`summary`）；
- 支持 `in: querystring` 参数、设备授权流程、`$self`、`defaultMapping`；
- 新项目应评估 3.2，但先确认 lint、mock、SDK 生成与网关导入的支持程度。
- OpenAPI 4.0（“Moonwalk”）尚未发布，不要作为生产基线。

### Lint

工具如 Spectral 可以检查：

- 命名规范；
- 必填描述；
- 错误响应；
- 版本策略；
- 安全 scheme；
- 示例有效性。

```yaml
rules:
  operation-operationId: error
  operation-description: error
  oas3-api-servers: error
  no-eval-in-markdown: error
```

### 常见 lint 规则

- 所有 operation 有 `operationId`；
- 所有 operation 有 `summary`；
- 所有响应有 description；
- 所有 4xx/5xx 有 schema；
- 所有参数有 description；
- 所有 schema 有 example；
- 不使用 `additionalProperties: true`（除非有意）；
- 不使用 `nullable`（3.1 用 union type）。

## 30.5 破坏性变更检测

CI 中比较新旧契约：

```bash
oasdiff breaking old.yaml new.yaml
```

或：

```bash
buf breaking --against '.git#branch=main'
```

检测：

- 删除字段/端点；
- 类型变化；
- 必填性变化；
- 枚举值删除；
- 参数变化；
- 响应码变化；
- 认证变化；
- 分页变化。

破坏性变更应：

- 阻止合并；
- 要求版本升级；
- 要求消费者确认；
- 记录例外。

### 检测的盲区

- 语义变化（类型不变但含义变）；
- 行为变化（排序、默认值）；
- 运行变化（超时、限流）；
- 错误码含义变化。

工具只能检测语法，语义需要人工评审。

## 30.6 Mock 与沙箱

契约可以生成 mock：

```bash
prism mock openapi.yaml
```

优点：

- 消费者不必等待后端；
- 测试错误路径；
- 验证契约；
- 前端并行开发。

沙箱应：

- 与生产隔离；
- 使用测试密钥；
- 可重置数据；
- 模拟延迟/错误；
- 有使用限制；
- 支持 webhook；
- 支持异步任务；
- 与真实行为一致。

### 模拟保真度的六个等级

“有 Mock”不等于“能验证契约”。按保真度从低到高，可以把模拟分成六级：

| 等级 | 形态 | 能验证 | 不能验证 |
|---|---|---|---|
| L0 | 文档静态示例 | 请求/响应形状 | 任何运行时行为 |
| L1 | 契约生成的静态 Mock | 路由、字段、基本校验 | 状态、权限、并发 |
| L2 | 状态化 Mock | CRUD、分页、错误注入 | 真实业务规则、外部依赖 |
| L3 | 隔离沙箱 | 真实业务逻辑、异步、Webhook | 生产规模、真实第三方 |
| L4 | 影子/回放 | 生产流量下的行为差异 | 写操作的真实副作用 |
| L5 | 生产测试模式 | 真实计费/清算前的最终验证 | 无（但风险最高） |

选择原则：前端并行开发用 L1；消费者契约测试用 L1/L2；联调与提供方验证用 L3；高风险迁移用 L4 做差分；支付、计费、清算才用 L5，并配合额度、审计与人工兜底。

### 对拍：让 Mock 不撒谎

Mock 最大的风险是“消费者与 Mock 一致，但提供方与契约不一致”。至少要做三件事：

1. Mock 的每个响应都通过 OpenAPI/JSON Schema 校验；
2. 同一组消费者契约测试在 Mock 与沙箱各跑一次；
3. 定期比较 Mock 与沙箱的字段、类型、状态码和错误码，差异进入缺陷列表。

沙箱的隔离模型、测试时钟、Webhook 重放与漂移检测，见[附录 AJ](../appendices/aj-sandbox-mock-test-data.md)。

### Mock 的局限

- 不验证业务逻辑；
- 不验证权限；
- 不验证并发；
- 可能与真实行为不一致；
- 需要维护。

## 30.7 代码生成

生成：

- 服务端接口骨架；
- 客户端 SDK；
- 类型定义；
- 文档；
- 测试夹具；
- mock。

注意：

- 生成代码不要手改；
- 版本固定；
- 生成物可复现；
- 命名与语言习惯；
- 错误处理；
- 分页/流式支持；
- 弃用标记；
- 认证；
- 重试。

### 生成 vs 手写 SDK

- 生成：一致、快、覆盖广；
- 手写：体验好、可定制；
- 混合：生成基础，手写封装。

### 一个可运行的契约 CI

“契约优先”如果没有 CI，最终会退化成“先写 YAML，然后代码和 YAML 各自漂移”。一个最小流水线：

```yaml
# .github/workflows/contract.yml
name: contract
on: [pull_request]
jobs:
  contract:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint OpenAPI
        run: npx @stoplight/spectral-cli lint openapi.yaml --fail-severity=warn
      - uses: actions/setup-go@v5
        with:
          go-version: "1.22"
      - name: Detect breaking changes
        run: |
          go install github.com/oasdiff/oasdiff@latest
          "$(go env GOPATH)/bin/oasdiff" breaking \
            --base origin/main:openapi.yaml \
            --revision openapi.yaml \
            --fail-on ERR
      - name: Generate server/client
        run: |
          npx @openapitools/openapi-generator-cli generate \
            -i openapi.yaml \
            -g typescript-fetch \
            -o generated/
      - name: Ensure generated code is committed
        run: git diff --exit-code -- generated/
      - name: Provider contract verification
        run: npm run test:pact
```

每一步的失败策略：

| 阶段 | 失败意味着 | 处理 |
|---|---|---|
| Spectral | 风格/规范错误 | 阻断合并 |
| oasdiff | 破坏性变更 | 阻断或要求版本升级 + 迁移计划 |
| 代码生成 | 生成器/契约不兼容 | 阻断 |
| `git diff --exit-code` | 生成物未提交/漂移 | 阻断，要求重新生成 |
| Pact | 实现不满足消费者契约 | 阻断 |

生成的服务端接口骨架示例（TypeScript）：

```ts
export interface Order {
  id: string;
  status: "PENDING_PAYMENT" | "PAID" | "SHIPPED" | "CANCELLED";
  total: Money;
}

export interface OrdersApi {
  getOrder(orderId: string): Promise<Order>;
  createOrder(body: CreateOrderRequest, idempotencyKey?: string): Promise<Order>;
}
```

生成代码不要手改；需要定制时，在外面包一层手写 SDK/适配器，而不是改生成物。

## 30.8 Schema Registry

对于事件与 Protobuf，Schema Registry 可以：

- 存储 schema；
- 版本管理；
- 兼容性检查；
- 消费者/生产者注册；
- 运行时校验；
- 迁移。

```text
schema: com.example.order.paid
version: 3
compatibility: BACKWARD
```

### 兼容性策略

- BACKWARD：新 schema 读旧数据；
- FORWARD：旧 schema 读新数据；
- FULL：双向；
- NONE：不检查。

## 30.9 契约测试

- 消费者驱动契约（Pact）；
- 提供方验证；
- Schema 校验；
- 示例验证；
- 端到端测试；
- 破坏性变更检测。

### 契约测试的层次

```text
1. Schema 校验：响应符合 OpenAPI
2. 示例验证：示例可反序列化
3. 消费者契约：字段/行为符合消费者期望
4. 端到端：真实集成
```

## 30.10 治理流程

```text
设计提案 → 契约评审 → Lint/破坏性检查 → 实现 → 契约测试
→ 发布 → 监控使用 → 弃用 → 删除
```

需要：

- API 风格指南；
- 评审委员会/Guild；
- 例外流程；
- 版本政策；
- 所有权；
- 目录；
- 自动化。

### 评审的重点

- 资源模型；
- 错误路径；
- 权限；
- 分页/过滤；
- 版本；
- 可观测性；
- 兼容性。

不要只检查命名。

## 30.11 工具链示例

| 用途 | 工具 |
|---|---|
| OpenAPI 编辑 | Stoplight、Swagger Editor、Redocly |
| Lint | Spectral |
| 破坏性检测 | oasdiff、openapi-diff |
| Mock | Prism、WireMock |
| 代码生成 | OpenAPI Generator、oapi-codegen |
| Protobuf | buf、protoc |
| GraphQL | GraphQL Code Generator、Apollo |
| 契约测试 | Pact |
| 文档 | Redoc、Scalar、Swagger UI |
| Schema Registry | Confluent、Apicurio |

## 30.12 反模式

- OpenAPI 是手写文档，和实现不一致；
- 契约没有 CI；
- 生成代码被手改；
- 破坏性变更不检测；
- 每个团队一套风格；
- 契约只覆盖成功路径；
- mock 与真实行为不一致；
- schema 没有版本；
- 契约评审变成橡皮图章；
- 只做 lint 不做设计评审；
- 契约与网关配置不一致。

## 30.13 本章小结

- 契约优先是让契约成为共同真相，而不是先写 YAML。
- 设计优先适合公开 API，代码优先适合小型内部服务。
- 单一真相 + CI 漂移检查是关键。
- Lint、破坏性检测、mock、代码生成、契约测试构成工具链。
- Schema Registry 管理事件/Protobuf 演进。
- 治理需要流程、所有权和例外机制。
- 工具只能检测语法，语义需要人工评审；关键不变量可用状态机、性质测试或 TLA+/Alloy 验证，见[附录 AA](../appendices/aa-formal-contracts-model-checking.md)。
- 数据契约除 schema 外还有语义、新鲜度、质量与所有权，见[附录 AB](../appendices/ab-data-contracts-metrics.md)。
- 序列化格式、压缩与 schema 演进的兼容性规则，见[附录 AE](../appendices/ae-serialization-schema-evolution.md)；多步工作流的描述与测试可参考 Arazzo，见[第 49 章](../part-6-ai-future/49-protocol-future.md)。
- 数据库自动生成接口的“契约”是 schema + 权限 + 生成器配置，必须用稳定视图与 CI diff 管理，见[附录 AF](../appendices/af-database-as-api-baas.md)。
- Mock 只是模拟阶梯的一级；沙箱隔离、测试时钟、Webhook 重放与 Mock/沙箱对拍，见[附录 AJ](../appendices/aj-sandbox-mock-test-data.md)。

## 设计练习

为一个“订单 API”建立契约流水线：

- OpenAPI 设计；
- Spectral 规则；
- 破坏性变更检测；
- mock；
- 服务端/客户端代码生成；
- Pact 契约测试；
- 文档发布。

画出 CI 流程，并说明每步失败如何处理。然后回答：

1. 哪些变更会被工具漏掉？
2. 如何处理紧急破坏性变更？
3. mock 与真实服务的差异如何管理？
4. 契约与网关配置如何保持一致？

---

上一章：[BFF、聚合层与后端边界](29-bff-and-aggregation.md)  
下一章：[API 测试：从单元到契约、模糊与混沌](../part-4-engineering-product/31-testing.md)
