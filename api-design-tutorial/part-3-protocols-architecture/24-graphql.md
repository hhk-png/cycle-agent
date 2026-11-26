# 第 24 章 GraphQL：灵活查询与治理成本

## 24.1 GraphQL 解决什么问题

GraphQL 让客户端声明需要的数据形状：

```graphql
query OrderDetail($id: ID!) {
  order(id: $id) {
    id
    status
    total { amount currency }
    customer { name email }
    items {
      sku
      quantity
      product { name imageUrl }
    }
  }
}
```

服务端返回匹配的 JSON：

```json
{
  "data": {
    "order": {
      "id": "ord_123",
      "status": "PAID",
      "total": { "amount": 12900, "currency": "CNY" },
      "customer": { "name": "Alice", "email": "a@example.com" },
      "items": [
        {
          "sku": "sku_1",
          "quantity": 2,
          "product": { "name": "耳机", "imageUrl": "..." }
        }
      ]
    }
  }
}
```

它解决的核心问题：

- 多客户端字段需求不同；
- 减少往返次数；
- 聚合多个数据源；
- 前端快速迭代；
- 强类型 schema；
- 自省（introspection）；
- 单一端点。

### GraphQL 不解决什么

GraphQL 本身不内置这些能力，需要在 schema、resolver、指令、网关或工程流程中实现：

- 授权（字段级/对象级）；
- HTTP 缓存与客户端缓存策略；
- N+1 查询（DataLoader/批量加载）；
- 性能与复杂度限制；
- 版本化与弃用；
- 团队协作与 schema 治理。

它把“查询规划”的一部分责任转移给了客户端，同时把“治理”责任留给了服务端。

## 24.2 Schema 设计

```graphql
type Order {
  id: ID!
  status: OrderStatus!
  total: Money!
  customer: Customer!
  items: [OrderItem!]!
  createdAt: DateTime!
}

enum OrderStatus {
  PENDING
  PAID
  SHIPPED
  CANCELLED
}

type Money {
  amount: Int!
  currency: String!
}

type Query {
  order(id: ID!): Order
  orders(first: Int, after: String, status: OrderStatus): OrderConnection!
}

type Mutation {
  createOrder(input: CreateOrderInput!): CreateOrderPayload!
  cancelOrder(input: CancelOrderInput!): CancelOrderPayload!
}
```

### 命名

- 类型用大驼峰；
- 字段用小驼峰；
- 枚举值用大写下划线；
- 输入类型以 `Input` 结尾；
- 连接类型遵循 Relay 规范（`Connection`、`Edge`、`PageInfo`）；
- Mutation 用动词开头；
- 避免缩写。

### 可空性

`!` 表示非空。设计时要谨慎：

- 非空字段一旦为 null，会导致父对象整个为 null；
- 列表项非空 vs 列表非空要区分；
- 外部数据可能缺失时，宁可为空。

```graphql
items: [OrderItem!]!   # 列表非空，元素非空
items: [OrderItem]!    # 列表非空，元素可空
items: [OrderItem!]    # 列表可空，元素非空
```

### 可空性的连锁反应

```graphql
type Order {
  customer: Customer!   # 如果 customer 缺失，整个 Order 变 null
}
```

如果一个非空字段解析失败，错误会向上冒泡，可能导致整个查询返回 `data: null`。对不可靠的外部数据，使用可空字段并在客户端处理缺失。

### 输入类型

- 输入与输出类型分开；
- 不要复用输出类型作为输入；
- 输入字段可空性要明确；
- 使用 `oneOf` 风格表达多态（通过多个可选字段 + 校验）。

## 24.3 错误处理

GraphQL 的错误模型与 REST 不同：

```json
{
  "data": {
    "order": null
  },
  "errors": [
    {
      "message": "Order not found",
      "path": ["order"],
      "extensions": {
        "code": "ORDER_NOT_FOUND",
        "requestId": "req_123"
      }
    }
  ]
}
```

关键点：

- 部分成功是常态；
- 错误应有稳定 `code`；
- `path` 指向出错字段；
- 不要在 `message` 中泄露敏感信息；
- 认证/授权错误应统一处理；
- HTTP 状态码通常仍是 200，除非整个请求失败。

### 错误分类

```text
1. 请求错误（语法、变量）→ 400
2. 认证错误 → 401
3. 授权错误 → 200 + errors（字段级）
4. 业务错误 → 200 + errors
5. 系统错误 → 200 + errors 或 500
```

这种“200 + errors”模型对监控不友好，需要：

- 解析 `errors`；
- 按 `code` 统计；
- 区分字段级错误和整体错误；
- 设置告警。

### Mutation 的错误

Mutation 应返回业务结果，而不是抛异常：

```graphql
type CreateOrderPayload {
  order: Order
  userErrors: [UserError!]!
}

type UserError {
  field: [String!]
  code: String!
  message: String!
}
```

这样客户端可以同时处理成功和失败。

## 24.4 分页

Relay 风格：

```graphql
type OrderConnection {
  edges: [OrderEdge!]!
  pageInfo: PageInfo!
  totalCount: Int
}

type OrderEdge {
  node: Order!
  cursor: String!
}

type PageInfo {
  hasNextPage: Boolean!
  hasPreviousPage: Boolean!
  startCursor: String
  endCursor: String
}
```

查询：

```graphql
query {
  orders(first: 20, after: "cursor") {
    edges {
      node { id status }
      cursor
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

### 分页的注意点

- `first` 必须有上限；
- `after` 是游标，不是 offset；
- `totalCount` 可能很贵；
- 排序必须稳定；
- 游标要绑定过滤条件；
- 嵌套连接可能放大查询。

## 24.5 N+1 问题

```graphql
query {
  orders(first: 100) {
    items {
      product { name }
    }
  }
}
```

如果每个订单的每个 item 都单独查 product，会产生大量查询。

解决：

- DataLoader 批处理；
- 预加载；
- 查询计划；
- 数据源层批量接口；
- 限制嵌套深度；
- 限制分页大小；
- 使用 `@defer`/`@stream`（如果支持）。

DataLoader 示例（伪代码）：

```js
const productLoader = new DataLoader(async (ids) => {
  const products = await db.products.findMany({ where: { id: { in: ids } } });
  return ids.map((id) => products.find((p) => p.id === id));
});
```

### N+1 的检测

- 统计每个请求的数据库查询数；
- 设置阈值告警；
- 追踪慢 resolver；
- 在测试中检查查询数；
- 使用 APM。

## 24.6 授权

GraphQL 的授权挑战：

- 字段级权限；
- 嵌套对象权限；
- 批量查询可能绕过端点级检查；
- introspection 泄露 schema；
- 别名可以绕过简单限流；
- 片段可以隐藏查询意图。

策略：

- 在 resolver 层做对象级检查；
- 字段级 directive/策略；
- 统一授权服务；
- 禁用生产 introspection 或限制；
- 查询复杂度限制；
- 防止批量枚举；
- 对敏感字段单独授权；
- 记录字段级访问审计。

```graphql
type User {
  email: String @auth(requires: ["user:email:read"])
  ssn: String @auth(requires: ["user:ssn:read"])
}
```

### 授权的性能

每个字段都调用授权服务会很慢。优化：

- 批量授权；
- 请求级缓存；
- 只对敏感字段检查；
- 在父对象解析时预取权限；
- 使用策略引擎的批量接口。

## 24.7 查询复杂度与滥用

攻击方式：

- 深层嵌套；
- 大量别名；
- 大分页；
- 复杂片段；
- 批量查询；
- 循环片段（通过深度限制阻止）；
- 重复字段。

防护：

- 深度限制；
- 复杂度评分；
- 分页上限；
- 查询超时；
- 持久化查询（persisted queries）；
- 允许列表；
- 速率限制；
- 监控慢查询；
- 查询成本预算。

### 复杂度评分

```text
cost = sum(field weights) + multiplier * nested cost
```

例如：

```text
orders(first: 100) { items(first: 50) { product { name } } }
cost ≈ 100 * 50 * 1 = 5000
```

设置每个请求的最大成本。

### 持久化查询

客户端发送查询 ID，而不是完整查询：

```json
{
  "id": "query_123",
  "variables": { "id": "ord_123" }
}
```

优点：

- 减少请求体积；
- 防止任意查询；
- 可缓存；
- 可审计；
- 可预热。

### 允许列表

只允许预定义的查询：

```text
query_123: order detail
query_124: order list
```

适合公开 API 或高安全场景。

## 24.8 缓存

GraphQL 的缓存比 REST 复杂：

- 单一端点，URL 缓存失效；
- 查询形状多样；
- 需要客户端规范化缓存（Apollo/Relay）；
- 服务端可使用持久化查询 + CDN；
- 字段级缓存；
- `@cacheControl` 指令；
- 响应中的 `extensions` 可携带缓存提示。

### 客户端缓存

- 规范化缓存按对象 ID 存储；
- 同一对象在不同查询间共享；
- 需要 `id` 和 `__typename`；
- 更新后自动失效；
- 适合交互式应用。

### 服务端缓存

- 持久化查询 + CDN；
- 按查询 ID + 变量缓存；
- 短 TTL；
- 权限敏感数据不缓存；
- 字段级缓存。

## 24.9 Federation

GraphQL Federation 把多个服务的 schema 组合成一个图：

```graphql
# 用户服务（子图 A）
type User @key(fields: "id") {
  id: ID!
  name: String!
}

# 订单服务（子图 B）
type Order {
  id: ID!
  customer: User!
}

# 订单服务必须声明 User 的实体 stub，否则组合会失败
extend type User @key(fields: "id") {
  id: ID! @external
}
```

订单服务的 `customer` resolver 至少要返回实体引用：

```python
def resolve_order_customer(order, info):
    return {"__typename": "User", "id": order.customer_id}
```

否则组合阶段会报 `type User not found`，或运行时无法解析实体。网关负责查询计划与结果合并。

优点：

- 团队自治；
- 统一图；
- 跨服务查询；
- 渐进迁移。

缺点：

- 复杂度高；
- 查询计划调试难；
- 跨服务 N+1；
- 版本治理；
- 网关成为关键路径；
- 实体解析失败；
- 跨团队 schema 冲突。

### Federation 的治理

- 每个团队拥有自己的子图；
- 共享实体定义；
- schema 变更评审；
- 查询计划监控；
- 网关性能；
- 跨服务授权。

## 24.10 何时使用 GraphQL

适合：

- 多客户端字段差异大；
- BFF/聚合层；
- 快速迭代的前端；
- 有治理团队；
- 需要强类型 schema；
- 需要减少往返。

不适合：

- 简单 CRUD；
- 高缓存要求；
- 公开 API 低门槛；
- 团队没有 GraphQL 运维经验；
- 文件上传/流式为主；
- 复杂权限但无字段级治理能力；
- 高频低延迟内部调用。

### 决策清单

1. 是否有多个客户端需要不同字段？
2. 是否需要聚合多个数据源？
3. 是否有团队维护 schema 和查询治理？
4. 是否能接受缓存复杂度？
5. 是否能处理 N+1？
6. 是否有字段级授权方案？
7. 是否需要公开 API？
8. 是否有替代方案（REST + BFF）？

## 24.11 本章小结

- GraphQL 把查询规划责任部分转移给客户端。
- Schema、可空性、错误、分页需要严格设计。
- N+1、复杂度攻击、字段级授权是主要风险。
- 持久化查询与复杂度限制是生产必备。
- Federation 适合多团队聚合，但治理成本高。
- GraphQL 不是 REST 的默认替代品。
- 错误模型需要额外监控和分类。

## 设计练习

为一个“内容管理平台”设计 GraphQL schema：

- 文章、作者、标签、评论；
- 多语言；
- 草稿/发布状态；
- 字段级权限；
- 分页；
- 搜索。

写出 schema、错误模型、复杂度限制和缓存策略。然后回答：

1. 多语言字段如何建模？
2. 草稿对未授权用户如何隐藏？
3. 评论的 N+1 如何解决？
4. 如何限制一次查询的成本？

---

上一章：[gRPC 与 Protobuf：高性能契约的代价](23-grpc-protobuf.md)  
下一章：[事件驱动：CloudEvents、AsyncAPI 与消息契约](25-event-driven.md)
