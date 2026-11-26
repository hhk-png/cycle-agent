# 第 6 章 命名、标识与 URL 设计

## 6.1 命名是契约的一部分

字段名和 URL 一旦发布，就会出现在调用方的代码、日志、报表、数据库、SDK 和文档中。改名不是文本替换，而是一次跨组织迁移。

命名的目标不是“好看”，而是：

- **无歧义**：同一个词在系统内只有一个含义；
- **可预测**：调用方可以猜出相邻资源的命名；
- **稳定**：不随实现细节变化；
- **可读**：人类和代码生成器都能理解；
- **可扩展**：未来增加概念时不需要推翻现有结构；
- **可搜索**：在日志、文档、代码中容易定位。

命名失败通常不是因为“英文不好”，而是因为**没有提前定义词汇表**。同一个概念在不同端点里叫 `user`、`customer`、`account`、`member`，调用方每次都要猜。

### 先建词汇表

在写第一个端点之前，先列出核心词汇：

| 概念 | 统一用词 | 禁止用词 |
|---|---|---|
| 购买者 | `customer` | `user`、`buyer`、`client` |
| 订单 | `order` | `purchase`、`trade` |
| 退款 | `refund` | `reversal`、`money-back` |
| 组织 | `organization` | `company`、`tenant`（除非确实指租户） |
| 成员 | `member` | `user`（在组织上下文中） |

词汇表不需要很长，但必须被执行。代码评审、schema lint、文档模板都应该引用它。

## 6.2 资源命名规则

一个实用的默认规则集：

1. 资源用**复数名词**：`/orders`、`/customers`、`/invoices`。
2. 使用**小写**和**连字符**：`/shipping-addresses`，不用 `/shippingAddresses`。
3. 层级表达从属关系，但不超过 2～3 层。
4. 用查询参数表达过滤、排序、分页，不塞进路径。
5. 动作优先建模为子资源或状态变化，而不是动词路径。
6. 不在 URL 中暴露数据库主键、表名、框架名。
7. 不使用文件扩展名表示格式，用 `Accept` 协商。
8. 不在 URL 中放敏感信息（token、密码、身份证号）。
9. 保持单复数一致：不要 `/order` 和 `/customers` 混用。
10. 避免缩写，除非是行业通用词（`sku`、`vat`、`iban`）。

### 示例

```text
GET  /orders
GET  /orders/{orderId}
GET  /orders/{orderId}/items
GET  /orders/{orderId}/refunds
POST /orders/{orderId}/refunds
GET  /customers/{customerId}/addresses
```

### 不推荐

```text
GET  /getOrders
POST /createOrder
GET  /order_list
GET  /orders/{id}/doCancel
GET  /api/v1/order-items/123?action=delete
```

这些写法不是“语法错误”，而是把 RPC 或数据库习惯泄漏进了资源路径，增加了调用方的学习成本。

### 层级深度

层级不是越深越好：

```text
# 过深
GET /organizations/{orgId}/projects/{projectId}/environments/{envId}/services/{serviceId}/instances

# 更好
GET /instances?serviceId={serviceId}&environmentId={envId}
```

层级表达“从属关系”和“作用域”，查询参数表达“过滤条件”。当层级超过三层时，通常意味着最后一层是一个可以用过滤条件表达的集合。

### 单数资源

有些资源天然是单例：

```text
GET /me
GET /organizations/{orgId}/settings
GET /organizations/{orgId}/billing-profile
```

单例资源用单数名词是合理的，但要在文档中明确它不是集合。

## 6.3 动作如何表达

并非所有操作都能自然表达为资源状态变化。常见处理方式：

### 1. 子资源

```text
POST /orders/{orderId}/cancellation
POST /orders/{orderId}/refunds
POST /users/{userId}/password-resets
```

优点是保持资源风格；缺点是可能产生很多“动作资源”。

子资源适合：

- 动作会产生一个可查询的记录；
- 动作有独立的状态和生命周期；
- 动作需要被审计或重试。

### 2. 冒号动作（Google AIP 风格）

```text
POST /orders/{orderId}:cancel
POST /orders/{orderId}:capture
```

优点是表达清晰，适合复杂动作；缺点是与传统 REST 工具链兼容性稍差，需要团队约定。

冒号动作适合：

- 动作是瞬时的、不产生独立资源；
- 动作名是业务动词；
- 需要强表达力。

### 3. 状态字段 + PATCH

```http
PATCH /orders/{orderId}
Content-Type: application/merge-patch+json

{ "status": "CANCELLED" }
```

不推荐用于复杂动作，因为 PATCH 难以表达原因、幂等、权限和副作用。

PATCH 适合：

- 简单字段修改；
- 状态确实是资源的一个普通属性；
- 没有额外副作用。

### 4. 独立命令端点

```text
POST /order-commands/cancel
```

适合事件驱动/命令总线架构，但对普通 REST 消费者较重。

**建议**：简单状态变化用子资源；复杂动作可用冒号语法；避免用 PATCH 修改业务状态机。

### 动作设计的判断标准

| 问题 | 子资源 | 冒号动作 | PATCH |
|---|---|---|---|
| 是否产生可查询记录？ | 是 | 否 | 否 |
| 是否需要原因/备注？ | 是 | 是 | 难 |
| 是否有独立权限？ | 是 | 是 | 难 |
| 是否可重试？ | 是 | 是 | 是 |
| 是否改变状态机？ | 是 | 是 | 不推荐 |

## 6.4 标识符设计

### 不要暴露自增主键

自增 ID 会泄漏：

- 业务规模（`/orders/1000000` 说明有百万订单）；
- 增长速率；
- 可枚举性，增加爬取与越权风险；
- 分库分表后的迁移困难；
- 竞争情报（对手可以估算你的订单量）。

### 推荐的不透明 ID

- **UUIDv4**：随机、通用，但索引局部性差；
- **UUIDv7**：按时间有序，适合数据库索引；
- **ULID**：时间有序、可排序、字符串友好；
- **带前缀 ID**：`ord_01J...`、`cus_01J...`，便于调试和防止类型混淆；
- **Snowflake**：分布式有序，但需要机器 ID 管理；
- **加密/哈希 ID**：不可枚举，但需要处理碰撞和长度。

```json
{
  "id": "ord_01J9Z6Q8K3W7V2M4N5P6R7S8T9",
  "customerId": "cus_01J9Z6Q8K3W7V2M4N5P6R7S8T9"
}
```

前缀的价值在于：

- 日志里一眼能看出 ID 类型；
- 把订单 ID 误传给客户接口时能更早失败；
- SDK 可以做类型区分；
- 支持人员可以快速判断 ID 是否属于当前系统。

### ID 设计对比

| 方案 | 有序 | 不可枚举 | 长度 | 适用 |
|---|---|---|---|---|
| 自增整数 | 是 | 否 | 短 | 内部、低敏感 |
| UUIDv4 | 否 | 是 | 36 字符 | 通用 |
| UUIDv7 | 是 | 是 | 36 字符 | 数据库主键 |
| ULID | 是 | 是 | 26 字符 | URL 友好 |
| 前缀 + ULID | 是 | 是 | 30 字符 | 公开 API |
| Snowflake | 是 | 部分 | 64 位 | 高并发内部 |

### ID 的稳定性

- ID 一旦分配，永不复用；
- 不要用可变字段（邮箱、手机号、slug）作为主标识；
- 如果需要人类友好标识，增加 `slug` 或 `displayId`，但保留不可变 ID；
- 外部 ID（如渠道订单号）应作为独立字段，不要替代内部 ID；
- 跨环境（测试/生产）的 ID 不应冲突；
- 迁移时保留旧 ID 的映射表。

### 不要把 ID 当令牌

有些系统用“不可猜测的 ID”代替授权。这是危险的：

- ID 可能通过日志、截图、分享链接泄漏；
- ID 可能被暴力枚举（如果熵不够）；
- ID 无法表达“谁有权访问”。

ID 的不可枚举性只是 defense in depth，不是授权机制。每次访问都必须做对象级授权。

## 6.5 URL 版本策略

常见方案：

### 路径版本

```text
/v1/orders
```

优点：直观、易路由、易缓存、易文档化。缺点：版本膨胀，容易把版本当成“大爆炸式重写”的借口。

### 请求头版本

```http
Accept: application/vnd.example.v2+json
```

或：

```http
X-API-Version: 2026-11-01
```

优点：URL 稳定；缺点：调试和缓存更复杂，浏览器直接访问不方便。

### 查询参数版本

```text
/orders?version=2
```

不推荐作为主版本机制，因为容易被忽略、难以缓存、语义弱。

### 日期版本

```http
Stripe-Version: 2026-11-26
```

优点：可以按日期精确控制行为变更；缺点：服务端需要长期维护多个行为分支。

### 务实建议

- 公开 API 使用主版本路径（`/v1`），但把版本视为**兼容性边界**，不是功能发布单位；
- 内部 API 尽量通过消费者驱动契约和扩展—收缩避免版本；
- 不使用微版本（`/v1.2.3`）表达兼容变更；
- 版本切换应有明确的生命周期和弃用政策；
- 同一 API 不要同时支持三种版本机制。

更多版本化讨论见第 11 章。

## 6.6 大小写与分隔符

选择一套并坚持：

| 场景 | 推荐 |
|---|---|
| URL 路径 | 小写 + 连字符：`/shipping-addresses` |
| 查询参数 | 小写 + 下划线或驼峰，按团队约定 |
| JSON 字段 | 小写驼峰 `orderId` 或蛇形 `order_id`，全 API 统一 |
| 枚举值 | 大写下划线 `PAYMENT_FAILED` 或小写点分 `payment.failed` |
| HTTP 头 | 连字符，大小写不敏感 |
| 事件类型 | 小写点分：`com.example.order.paid` |

最忌讳的是混用：同一个 API 里既有 `orderId` 又有 `order_id`，既有 `PAID` 又有 `paid`。这会让代码生成、日志分析和调用方映射变得脆弱。

### 为什么不建议在 URL 中用驼峰

URL 路径在大小写敏感的系统（Linux、部分 CDN、部分代理）中可能表现不一致。`/shippingAddresses` 与 `/shippingaddresses` 可能被视为不同路径，导致缓存穿透或 404。

### 枚举大小写的兼容性

枚举值的大小写是契约的一部分。如果从 `PAID` 改成 `paid`，即使语义相同，所有做字符串比较的客户端都会失败。这类变更应视为破坏性变更。

## 6.7 查询参数设计

查询参数应有明确语法：

```text
?status=PAID&status=SHIPPED        # 多值
?createdAt[gte]=2026-01-01          # 操作符
?sort=-createdAt,totalAmount        # 排序，- 表示降序
?fields=id,status,total             # 稀疏字段
?pageSize=50&pageToken=...          # 分页
```

需要定义：

- 多值是重复参数还是逗号分隔？
- 操作符如何表达？括号、点号还是前缀？
- 未知参数是忽略还是报错？
- 参数大小写是否敏感？
- 空值如何表达？
- 数组如何编码？
- 参数长度上限是多少？
- 重复参数如何合并？

**建议**：未知参数默认报错（400），避免调用方以为过滤生效但实际被忽略。对公开 API 可以提供严格模式与宽松模式，但要在文档中明确。

### 查询参数的编码

- 空格用 `%20` 或 `+`（`+` 在 query 中通常表示空格，但路径中不是）；
- 逗号在数组中可能需要编码；
- 中文字符必须 percent-encode；
- 括号 `[` `]` 在某些代理中可能被处理，需测试；
- 参数值不要放 JSON（除非有明确约定），否则长度和转义会出问题。

### 参数命名

- 过滤字段用资源字段名：`status`、`createdAt`；
- 分页用 `pageSize` / `pageToken` 或 `limit` / `cursor`，全 API 统一；
- 排序用 `sort`，不要用 `orderBy` 和 `sortBy` 混用；
- 字段选择用 `fields`，展开用 `expand`；
- 幂等键用 HTTP 头，不要放 query。

## 6.8 规范化与别名

同一个资源可能有多个 URL：

```text
/orders/123
/orders/ord_123
/customers/cus_1/orders/123
```

应明确：

- 哪个是规范 URL（canonical）；
- 其他 URL 是否重定向；
- 缓存键如何归一；
- 权限检查是否一致；
- 日志和监控如何统计。

不要让多个路径返回细微不同的表示，否则调用方会依赖偶然行为。

### 规范化策略

- 对外只暴露一个规范 URL；
- 别名用 `301` 永久重定向到规范 URL；
- 缓存键基于规范 URL；
- 权限检查在规范 URL 上做；
- 文档只写规范 URL。

### URL 中的大小写与尾斜杠

- 路径大小写敏感还是不敏感，必须在网关层统一；
- 尾斜杠 `/orders/` 与 `/orders` 是否等价，必须明确；
- 重复斜杠 `//orders` 是否归一；
- 这些细节会影响缓存命中、签名和路由。

## 6.9 命名中的业务语言

优先使用业务词汇，而不是技术词汇：

| 不推荐 | 推荐 |
|---|---|
| `data`、`info`、`payload` | `order`、`invoice`、`shipment` |
| `flag`、`type`、`status` | `paymentStatus`、`refundReason` |
| `userId` 表示客户 | `customerId` |
| `deleteFlag` | `archivedAt` 或明确的删除状态 |
| `amount` 无币种 | `Money { amount, currency }` |
| `date` 无时区 | `createdAt`、`billingDate` |
| `name` 单一字段 | `givenName`、`familyName`、`displayName` |

字段名应回答“这是什么”，而不是“它存在哪里”。

### 国际化命名

如果 API 面向全球：

- 字段名保持英文；
- 不要用拼音（`dingdan`）或本地缩写；
- 姓名不要假设 `firstName`/`lastName` 结构；
- 地址不要假设“省/市/区”三层；
- 日期、数字、货币格式由客户端本地化，API 返回结构化数据；
- 枚举值保持英文稳定，展示文本另行本地化。

## 6.10 URL 设计的反模式

- **动词堆叠**：`/api/v1/order/create/new`
- **实现泄漏**：`/api/v1/mysql/orders`
- **框架泄漏**：`/api/v1/orders/index.php`
- **过深层级**：`/orgs/{o}/projects/{p}/envs/{e}/services/{s}/instances/{i}/logs`
- **在 URL 中放敏感信息**：`/users?token=...`
- **用 GET 执行写操作**：`/orders/123/cancel`
- **路径中包含文件扩展名**：`/orders.json`
- **不一致的复数**：`/order`、`/customers`、`/product`
- **把动作塞进查询参数**：`/orders?action=cancel`
- **用 URL 表达批量操作**：`/orders/bulk-cancel?ids=...`（应使用 POST + body）

过深层级通常意味着需要独立资源或复合查询参数：

```text
GET /instances?serviceId=...&environmentId=...
```

### 关于 GET 写操作

用 GET 执行写操作会带来：

- 浏览器预取、爬虫、代理可能意外触发；
- 无法安全重试；
- 缓存可能缓存“写结果”；
- 监控和审计难以区分读/写；
- 违反 HTTP 语义，中间件行为不可预测。

即使内部工具，也应使用 POST。

## 6.11 本章小结

- 命名是长期契约，优先业务语言、复数资源、一致大小写。
- 先建词汇表，再写端点。
- 复杂动作用子资源或冒号语法，不要用 PATCH 硬改状态机。
- ID 应不透明、稳定、不可枚举，推荐 UUIDv7/ULID/带前缀 ID。
- ID 不是授权机制，每次访问都要做对象级授权。
- 版本策略应服务于兼容性，而不是功能发布。
- 查询参数需要明确语法和错误行为。
- 规范化 URL 与别名策略能避免缓存和权限不一致。

## 设计练习

为一个“活动报名系统”设计 URL：

- 活动列表、详情、场次；
- 报名、取消报名、候补；
- 签到；
- 票券；
- 组织者后台查询。

要求：资源命名一致、动作语义清晰、ID 不透明，并写出至少 10 个端点。然后回答：

1. 哪些资源需要独立 URL？哪些应该是子资源？
2. 取消报名、候补转正、签到分别用什么动作风格？
3. 如果活动 ID 被泄漏，攻击者能做什么？如何防止？

---

上一章：[API 风格选型：REST、RPC、GraphQL、事件与流](05-style-selection.md)  
下一章：[请求、响应与表示设计](07-representations.md)
