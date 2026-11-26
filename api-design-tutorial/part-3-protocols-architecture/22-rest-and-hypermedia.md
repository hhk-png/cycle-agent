# 第 22 章 REST 与超媒体：成熟度、链接与发现

## 22.1 REST 的原意

REST（Representational State Transfer）是 Roy Fielding 在 2000 年博士论文中提出的架构风格，包含六个约束：

1. **客户端—服务器**分离；
2. **无状态**：每个请求包含理解它所需的全部信息；
3. **可缓存**；
4. **统一接口**；
5. **分层系统**；
6. **按需代码**（可选）。

统一接口又包含：

- 资源标识；
- 通过表示操作资源；
- 自描述消息；
- 超媒体作为应用状态引擎（HATEOAS）。

今天大多数被称为“RESTful”的 API，实际上只满足了“HTTP + JSON + 资源 URL”，并没有实现完整的 REST 约束。这不一定错，但需要知道自己在做什么。

### REST 不是协议

REST 不规定：

- 用什么媒体类型；
- 用什么 URL 结构；
- 用什么认证；
- 用什么错误格式。

它只规定架构约束。这意味着“REST API”可以有很多种样子，也意味着“不符合 REST”不等于“不好”。

### 无状态的代价

无状态让服务端容易扩展，但代价是：

- 每个请求都要携带认证信息；
- 客户端要管理更多状态；
- 大令牌增加请求体积；
- 某些交互（如多步向导）需要显式状态资源。

REST 不是“完全不能有状态”，而是“服务端不依赖会话状态”。

## 22.2 Richardson 成熟度模型

| 级别 | 特征 |
|---|---|
| 0 | 单一端点，RPC 风格（如 SOAP/XML-RPC） |
| 1 | 资源 |
| 2 | HTTP 动词 + 状态码 |
| 3 | 超媒体控制 |

大多数“REST API”处于第 2 级。第 3 级并不总是必要，但在某些场景（工作流、状态机、长期演进）价值很高。

### 成熟度不是质量

- 第 0 级的 RPC 可以非常成功（如 gRPC）；
- 第 2 级的 REST 可以支撑巨大平台；
- 第 3 级如果客户端不利用超媒体，只是增加了响应体积。

成熟度描述的是“约束满足程度”，不是“好坏”。

## 22.3 超媒体是什么

超媒体意味着响应中不仅包含数据，还包含**可执行的下一步链接**：

```json
{
  "id": "ord_123",
  "status": "PENDING_PAYMENT",
  "total": { "amount": 12900, "currency": "CNY" },
  "_links": {
    "self": { "href": "/orders/ord_123" },
    "pay": { "href": "/orders/ord_123/payments", "method": "POST" },
    "cancel": { "href": "/orders/ord_123/cancel", "method": "POST" }
  }
}
```

客户端不需要硬编码“待支付订单可以调用 /payments”，而是根据服务端提供的链接行动。服务端可以在不破坏客户端的情况下改变 URL、增加步骤或调整工作流。

### 超媒体的核心思想

```text
客户端知道如何解释媒体类型；
服务端告诉客户端“现在能做什么”；
客户端不需要预先知道所有 URL。
```

这与传统客户端形成对比：

```text
传统：客户端硬编码 /orders/{id}/cancel
超媒体：客户端读取 _links.cancel
```

### 一个更完整的状态机示例

```json
{
  "id": "claim_123",
  "status": "NEED_MORE_INFO",
  "allowedActions": [
    {
      "rel": "upload-document",
      "href": "/claims/claim_123/documents",
      "method": "POST",
      "title": "补充材料"
    },
    {
      "rel": "withdraw",
      "href": "/claims/claim_123/withdraw",
      "method": "POST",
      "title": "撤回申请"
    }
  ]
}
```

当状态变为 `APPROVED`，`allowedActions` 变为：

```json
[
  {
    "rel": "view-payout",
    "href": "/claims/claim_123/payout",
    "method": "GET"
  }
]
```

客户端不需要写 `if status == ...` 的分支，只需要渲染可用动作。

### 优点

- 客户端与服务端解耦；
- 工作流可演进；
- 可发现性；
- 减少硬编码 URL；
- 适合状态机复杂的领域；
- 权限可以体现在可用动作中。

### 缺点

- 客户端实现更复杂；
- 工具链支持弱；
- 响应更大；
- 调试更困难；
- 大多数开发者不熟悉；
- 链接语义需要严格定义；
- 链接可能被客户端滥用。

## 22.4 常见超媒体格式

### HAL

```json
{
  "id": "ord_123",
  "status": "PAID",
  "_links": {
    "self": { "href": "/orders/ord_123" },
    "customer": { "href": "/customers/cus_456" }
  },
  "_embedded": {
    "items": [
      {
        "sku": "sku_1",
        "quantity": 2,
        "_links": { "self": { "href": "/products/sku_1" } }
      }
    ]
  }
}
```

媒体类型：`application/hal+json`。

特点：

- 简单；
- `_links` 和 `_embedded`；
- 支持嵌入资源；
- 工具支持较多。

### JSON:API

```json
{
  "data": {
    "type": "orders",
    "id": "123",
    "attributes": { "status": "PAID" },
    "relationships": {
      "customer": { "data": { "type": "customers", "id": "456" } }
    },
    "links": { "self": "/orders/123" }
  },
  "included": []
}
```

媒体类型：`application/vnd.api+json`。规范了资源、关系、分页、错误、稀疏字段等。

特点：

- 规范完整；
- 支持复合文档；
- 学习曲线陡；
- 适合大型 API。

### Siren

```json
{
  "class": ["order"],
  "properties": { "status": "PAID" },
  "actions": {
    "cancel": {
      "method": "POST",
      "href": "/orders/123/cancel",
      "fields": [
        { "name": "reason", "type": "text" }
      ]
    }
  },
  "links": [{ "rel": ["self"], "href": "/orders/123" }]
}
```

更适合“动作”和“表单”场景。

### Collection+JSON

面向集合与查询的简单超媒体格式。

### 格式对比

| 格式 | 复杂度 | 动作 | 嵌入 | 工具 |
|---|---|---|---|---|
| HAL | 低 | 弱 | 是 | 多 |
| JSON:API | 高 | 弱 | 是 | 中 |
| Siren | 中 | 强 | 是 | 少 |
| Collection+JSON | 低 | 弱 | 否 | 少 |

## 22.5 链接关系

常见的标准链接关系（IANA 注册或广泛约定）：

```text
self
next
prev
first
last
item
collection
create-form
edit-form
delete-form
payment
```

注意：`create`、`edit`、`delete` 不是 IANA 注册的关系名，注册形式是 `create-form`、`edit-form`、`delete-form`。如果要表达“执行取消/退款”等动作，使用自定义 URI 关系更清晰：

```text
https://api.example.com/rels/cancel
```

自定义关系应使用 URI 或命名空间，避免冲突：

```json
{
  "rel": "https://api.example.com/rels/cancel",
  "href": "/orders/123/cancel"
}
```

### 链接的语义

- `href` 是 URI；
- `method` 是 HTTP 方法；
- `title` 是人类可读描述；
- `type` 是媒体类型；
- `rel` 是关系；
- 可能还有 `templated`（URI 模板）。

链接不应包含：

- 权限令牌；
- 敏感参数；
- 会过期的签名（除非明确）。

## 22.6 超媒体的适用场景

适合：

- 工作流/状态机；
- 多步骤审批；
- 公开 API 需要长期演进；
- 客户端不应硬编码 URL；
- 服务端需要控制下一步；
- 权限决定可用动作。

不适合：

- 简单 CRUD；
- 性能敏感；
- 客户端团队不熟悉；
- 工具链不支持；
- 响应体积敏感；
- 客户端需要强类型代码生成。

**务实建议**：不必追求完整 HATEOAS，但可以在关键状态机响应中提供 `actions` 或 `_links`，让客户端少写硬编码逻辑。

### 渐进式超媒体

```text
阶段 1：资源 URL + 标准方法
阶段 2：关键响应增加 _links
阶段 3：客户端使用 links 驱动导航
阶段 4：完整 HATEOAS
```

大多数团队停在阶段 2 或 3，这已经能获得大部分收益。

## 22.7 REST 的常见误解

### “REST 就是 JSON over HTTP”

不是。REST 是架构风格，JSON 只是表示之一。

### “URL 里不能有动词”

REST 并没有禁止动词，只是建议用资源表达。复杂动作很难资源化，冒号动作或命令资源是合理选择。

### “必须用 HATEOAS”

HATEOAS 是 REST 的统一接口约束之一，但实践中很多 API 不实现。是否采用取决于演进需求与客户端能力。

### “REST 一定比 RPC 好”

不一定。内部高性能调用、流式传输、强类型契约可能更适合 gRPC。

### “状态码必须严格对应 CRUD”

状态码表达语义，不是 CRUD 的机械映射。业务冲突可以用 409，校验失败可以用 422。

### “REST 必须无状态，所以不能用会话”

REST 要求服务端不依赖会话状态来理解请求，但可以使用会话作为缓存或优化。关键是请求本身携带足够信息。

## 22.8 REST 与 RPC 的混合

很多成功 API 是混合的：

```text
GET  /orders/{id}           # 资源读取
POST /orders                # 创建
POST /orders/{id}/cancel    # 动作
POST /search                # 复杂查询（需要 body）
GET  /reports/{id}/download # 下载
```

关键是：

- 命名一致；
- 语义清晰；
- 错误统一；
- 文档完整；
- 不为了“纯度”牺牲可用性。

### 混合的边界

- 资源操作用标准方法；
- 复杂动作用子资源/冒号动作；
- 查询用 GET 或专用查询资源；
- 批量操作用 POST + 结果资源；
- 异步用 202 + 操作资源。

## 22.9 设计检查清单

1. 资源是否用名词、复数、稳定 ID？
2. 方法是否匹配安全/幂等语义？
3. 状态码是否准确？
4. 是否支持缓存/条件请求？
5. 错误是否标准？
6. 分页/过滤/排序是否一致？
7. 关键工作流是否提供可执行链接？
8. 是否有版本与弃用策略？
9. 文档是否包含示例与错误？
10. 客户端是否能不依赖内部实现？

## 22.10 本章小结

- REST 是一组架构约束，不只是 URL 风格。
- Richardson 成熟度模型帮助定位 API 的设计层次。
- 超媒体适合复杂工作流与长期演进，但不是所有 API 的必需品。
- HAL、JSON:API、Siren 各有侧重。
- REST 与 RPC 可以混合，关键是语义一致。
- 不必追求纯度，但要知道取舍。
- 渐进式超媒体是更务实的选择。

## 设计练习

为一个“保险理赔 API”设计超媒体响应：

- 理赔状态机：提交、审核、补件、审批、赔付；
- 不同状态下允许不同动作；
- 客户端不应硬编码 URL；
- 需要权限控制。

写出至少 5 个状态的响应与 `actions`。然后回答：

1. 补件状态下，哪些动作可用？
2. 权限不足时，动作是隐藏还是标记为不可用？
3. 客户端如何知道“下一步”是什么？
4. 如果服务端改变 URL，客户端是否需要升级？

---

上一章：[HTTP 深度：语义、连接、缓存与调试](21-http-deep-dive.md)  
下一章：[gRPC 与 Protobuf：高性能契约的代价](23-grpc-protobuf.md)
