# 附录 G：GraphQL Schema 风格指南

## G.1 命名

- 类型：`PascalCase`；
- 字段：`camelCase`；
- 枚举：`UPPER_SNAKE_CASE`；
- 输入：`XxxInput`；
- 连接：`XxxConnection`；
- 边：`XxxEdge`；
- 变更载荷：`XxxPayload`；
- 查询/变更：动词开头。

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
  PENDING_PAYMENT
  PAID
  SHIPPED
  CANCELLED
}
```

## G.2 可空性

```graphql
items: [OrderItem!]!   # 列表非空，元素非空
items: [OrderItem]!    # 列表非空，元素可空
items: [OrderItem!]    # 列表可空，元素非空
```

建议：

- 核心字段非空；
- 外部/可选数据可空；
- 不要为了“严格”把可能缺失的字段设为非空；
- 非空字段为 null 时，错误会向上传播到**最近的可空父级**，不一定是直接父对象。例如 `Order.customer: User!` 中的 `User.name: String!` 为 null，可能让 `customer` 变成 null；如果 `Order.customer` 也是非空，则继续向上传播。设计可空性时要沿着整条路径检查。

## G.3 分页

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

type Query {
  orders(first: Int, after: String, last: Int, before: String, status: OrderStatus): OrderConnection!
}
```

规则：

- `first` 有上限；
- `cursor` 不透明；
- `totalCount` 谨慎；
- 排序稳定；
- 过滤参数明确。

## G.4 输入

```graphql
input CreateOrderInput {
  customerId: ID!
  items: [OrderItemInput!]!
  shippingAddressId: ID!
  couponCode: String
}

input OrderItemInput {
  skuId: ID!
  quantity: Int!
}
```

规则：

- 输入类型独立；
- 不要复用输出类型作为输入；
- 字段有默认值/约束；
- 敏感字段不暴露；
- 使用 `oneOf` 语义时明确。

## G.5 变更

```graphql
type Mutation {
  createOrder(input: CreateOrderInput!): CreateOrderPayload!
  cancelOrder(input: CancelOrderInput!): CancelOrderPayload!
}

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

规则：

- 业务错误放 `userErrors`，不要只用顶层 `errors`；
- 输入用单一 `input` 参数；
- 返回受影响的资源；
- 支持幂等键（如果适用）；
- 明确部分成功。

## G.6 错误

```json
{
  "data": { "order": null },
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

规则：

- `extensions.code` 稳定；
- `path` 指向字段；
- 不泄露敏感信息；
- 部分成功要可识别；
- HTTP 通常 200，除非整个请求失败。

## G.7 授权

- resolver 层对象级检查；
- 字段级 directive/策略；
- 不支持只靠端点级；
- 生产环境限制 introspection；
- 防止批量枚举。

```graphql
directive @auth(requires: [String!]!) on FIELD_DEFINITION | OBJECT

type User {
  email: String @auth(requires: ["user:email:read"])
  ssn: String @auth(requires: ["user:ssn:read"])
}
```

## G.8 复杂度限制

- 最大深度；
- 最大复杂度；
- 最大分页；
- 查询超时；
- 别名限制；
- 批量限制；
- 持久化查询；
- 允许列表。

## G.9 缓存

- 客户端规范化缓存；
- 持久化查询 + CDN；
- 字段级缓存；
- `@cacheControl`；
- 避免个性化数据进入共享缓存。

## G.10 Federation

```graphql
type User @key(fields: "id") {
  id: ID!
  name: String!
}
```

规则：

- 实体键稳定；
- 跨服务引用；
- 查询计划可观测；
- 避免跨服务 N+1；
- 版本治理；
- 网关高可用。

## G.11 检查清单

- [ ] 命名规范。
- [ ] 可空性合理。
- [ ] 分页有上限。
- [ ] 输入类型独立。
- [ ] 变更返回 `userErrors`。
- [ ] 错误有稳定 code。
- [ ] 字段级授权。
- [ ] 复杂度/深度限制。
- [ ] 持久化查询。
- [ ] 缓存策略。
- [ ] introspection 策略。
- [ ] 监控慢查询。
- [ ] 文档/示例。

---

返回：[目录](../SUMMARY.md)
