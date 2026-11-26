# 第 7 章 请求、响应与表示设计

## 7.1 表示层是契约的“可见部分”

领域模型决定 API 的世界观，表示层决定这个世界观如何被编码成字节。表示层的每个选择都会影响调用方的代码、缓存、日志和演进空间。

本章讨论的“表示”包括：

- 媒体类型与内容协商；
- JSON 对象的结构；
- 字段命名与类型；
- 时间、金额、枚举、空值；
- 请求与响应的信封；
- 字段选择与展开；
- 压缩与编码；
- 错误表示与安全边界。

表示层的核心原则是：**精度优先于便利，稳定优先于简洁。** 一个字段少写两个字符，不会让 API 更好；但一个金额表示错一个数量级，会让公司损失真金白银。

## 7.2 媒体类型与内容协商

### 请求

```http
POST /orders
Content-Type: application/json
Accept: application/json
```

- `Content-Type` 描述请求体的格式，必须准确。
- `Accept` 描述客户端能处理的响应格式，服务端可以协商。
- `Accept-Encoding` 描述客户端能接受的压缩方式。
- `Accept-Language` 描述客户端期望的自然语言。

常见媒体类型：

```text
application/json
application/problem+json      # RFC 9457 错误
application/merge-patch+json  # JSON Merge Patch
application/json-patch+json   # JSON Patch
application/x-ndjson          # 流式 JSON
text/event-stream             # SSE
multipart/form-data           # 文件上传
application/octet-stream      # 二进制
application/cbor              # 二进制 JSON
```

### 错误处理

如果客户端发送不支持的 `Content-Type`，应返回 `415 Unsupported Media Type`；如果无法满足 `Accept`，返回 `406 Not Acceptable`。不要把“解析失败”一律返回 400。

### 内容协商的细节

`Accept` 可以带权重：

```http
Accept: application/json;q=1.0, application/cbor;q=0.5
```

服务端应选择最高权重的可支持类型。如果所有类型都不支持，返回 406，并在响应中列出支持的类型。

`Content-Type` 可以带参数：

```http
Content-Type: application/json; charset=utf-8
```

JSON 的默认编码是 UTF-8，通常不需要显式声明，但显式声明可以减少歧义。

### 版本化媒体类型

```http
Accept: application/vnd.example.v2+json
```

适合需要细粒度表示版本的场景，但对缓存、调试和客户端实现要求更高。

### 为什么媒体类型很重要

- 代理和缓存根据 `Content-Type` 决定是否缓存、如何压缩；
- 浏览器根据 `Content-Type` 决定如何渲染；
- 安全策略（如 `X-Content-Type-Options: nosniff`）依赖正确类型；
- 代码生成器根据媒体类型选择解析器；
- 错误格式 `application/problem+json` 让监控可以统一识别。

## 7.3 JSON 对象：裸对象还是信封

### 裸对象

```json
{
  "id": "ord_123",
  "status": "PAID",
  "total": { "amount": 12900, "currency": "CNY" }
}
```

优点：简单、字段直接、适合单资源。缺点：难以附加元数据（如警告、追踪 ID），扩展时需要新增顶层字段。

### 信封

```json
{
  "data": {
    "id": "ord_123",
    "status": "PAID"
  },
  "meta": {
    "requestId": "req_abc",
    "warnings": []
  }
}
```

优点：可扩展、统一。缺点：多一层嵌套，调用方需要解包，缓存和代码生成更繁琐。

### 建议

- 单资源默认裸对象；
- 列表使用统一分页信封；
- 错误使用 Problem Details，不混入成功信封；
- 元数据优先放 HTTP 头（如 `Request-Id`、`ETag`），必要时再放 `meta`。

不要为了“统一”给所有响应套三层信封：

```json
{ "success": true, "code": 0, "message": "ok", "data": { "data": {} } }
```

这种结构既不增加信息，又增加了解包成本和歧义。

### 信封的隐藏成本

- 每个客户端都要写解包逻辑；
- 缓存键需要忽略 `meta`；
- 代码生成器可能把 `data` 当成业务字段；
- 错误和成功的结构不一致时，客户端要写两套解析；
- 嵌套层级增加，日志和调试更啰嗦。

信封不是原罪，但应该有明确理由。列表分页信封有理由（游标、hasMore），单资源套信封通常没有。

### JSON 的边界

JSON 不是万能表示：

- 大整数精度：JavaScript 的 `number` 是 IEEE 754 双精度，超过 2^53 的整数会丢精度；
- 二进制数据：Base64 膨胀 33%；
- 日期：没有原生日期类型；
- 循环引用：无法表达；
- 流式：需要 NDJSON 或 JSON Sequence；
- 二进制 schema：不如 Protobuf/Avro 紧凑。

对金额、ID、时间等关键字段，应使用字符串或明确的数值范围，避免 JSON 精度陷阱。

## 7.4 字段命名与类型

### 命名

全 API 统一一种风格：

- `camelCase`：`orderId`、`createdAt`；
- `snake_case`：`order_id`、`created_at`。

选择哪一种不重要，重要的是不要混用。对外 API 通常用 `camelCase` 更符合 JavaScript/TypeScript 生态；数据平台可能偏好 `snake_case`。

### 类型要精确

| 概念 | 不推荐 | 推荐 |
|---|---|---|
| 金额 | `"12.99"` 或 `12.99` | `{ "amount": 1299, "currency": "CNY" }` |
| 时间 | `"2026-01-01"` 或时间戳 | ISO 8601 带时区：`"2026-01-01T10:00:00+08:00"` |
| 布尔 | `"true"`、`1`、`"Y"` | `true` |
| 枚举 | 任意字符串 | 明确枚举 + 未知值处理策略 |
| ID | 数字自增 | 不透明字符串 |
| 比例 | `0.1` 表示 10% | 明确 `rate` 与精度，或使用基点 `1000` |
| 数量 | 浮点数 | 整数 + 单位 |
| 大整数 | JSON number | 字符串 |

### 金额

金额必须同时包含数值和币种，并明确最小单位。本教程统一使用最小单位整数：

```json
{ "amount": 12990, "currency": "CNY" }
```

其中 `amount` 是最小货币单位（人民币为分）。不要混用 `scale`、`amountMinor`、十进制字符串等不同形式；如果需要展示，由客户端按 ISO 4217 格式化。

不要用浮点数表示金额。JSON 的 `number` 在不同语言中可能被解析为 IEEE 754 双精度，导致舍入误差。

更完整的金额表示还应包含：

- 是否含税；
- 税费金额；
- 舍入规则；
- 汇率（如果涉及多币种）；
- 价格版本（如果价格会变）。

```json
{
  "total": {
    "amount": 12990,
    "currency": "CNY",
    "taxIncluded": true,
    "taxAmount": 1181
  },
  "rounding": "HALF_UP",
  "priceVersion": "2026-11-01"
}
```

### 时间

- 使用 ISO 8601 / RFC 3339；
- 明确时区偏移；
- 区分“时刻”和“日期”；
- 不要在字段名中暗示错误语义：`createdAt` 是时刻，`billingDate` 是日期；
- 服务端统一存储 UTC，展示时按业务时区转换；
- 对“今天”“本月”等相对时间，明确以哪个时区为准。

```json
{
  "createdAt": "2026-11-26T10:15:30+08:00",
  "billingDate": "2026-11-26",
  "timeZone": "Asia/Shanghai"
}
```

### 枚举

枚举值应稳定、可读、可扩展：

```json
{ "status": "PAYMENT_FAILED" }
```

调用方必须处理未知枚举值，否则服务端新增状态会导致客户端崩溃。文档中应明确：

> 客户端应将未知枚举值视为 `UNKNOWN`，不得因此拒绝整个响应。

### 布尔与三态

布尔字段无法表达“未知”：

```json
{ "isVerified": false }   // 未验证，还是不知道？
```

需要三态时，使用可空布尔或枚举：

```json
{ "verificationStatus": "UNVERIFIED" | "VERIFIED" | "UNKNOWN" }
```

### 数组与集合

- 数组是否有序？如果有序，排序规则是什么？
- 是否允许重复？
- 空数组与 `null` 是否等价？
- 大数组是否有上限？
- 集合元素是否稳定排序（便于缓存和 diff）？

## 7.5 空值语义

`null`、字段缺失、空数组、空字符串，含义可能完全不同：

- `null`：字段存在，但值未知或不适用；
- 缺失：字段未被请求、未被修改，或服务端未返回；
- `[]`：集合为空；
- `""`：字符串为空。

在 PATCH 中尤其重要：

```json
{ "nickname": null }   // 清空昵称？
{ "nickname": "" }     // 设置为空字符串？
{}                     // 不修改？
```

JSON Merge Patch 用 `null` 表示删除字段；JSON Patch 用显式操作。选择一种并写清楚。

### 空值策略

| 策略 | 含义 | 适用 |
|---|---|---|
| 字段缺失 = 未知 | 服务端不返回未知字段 | 读接口，减少歧义 |
| 字段始终存在，值为 null | 结构稳定 | 强类型客户端 |
| 空数组代替 null | 集合语义一致 | 列表字段 |
| 空字符串代替 null | 字符串语义一致 | 文本字段 |

没有一种策略适用于所有场景，关键是**全 API 一致**，并在文档中说明。

### 序列化陷阱

- 某些语言会把 `null` 和缺失都反序列化为默认值；
- 某些 JSON 库会省略 `null` 字段；
- 某些客户端会把 `0`、`false`、`""` 当成“无值”；
- 某些 ORM 会把 `null` 和空字符串混为一谈。

契约应明确“字段存在性”和“值”的区别，并在测试中覆盖。

## 7.6 多态与可扩展字段

多态响应常见于：

- 支付方式：卡、银行转账、钱包；
- 事件载荷：不同类型不同字段；
- 通知渠道：邮件、短信、推送；
- 节点类型：文本、图片、视频、嵌入。

### 方案一：判别字段 + 扁平对象

```json
{
  "type": "card",
  "last4": "4242",
  "brand": "VISA",
  "expMonth": 12,
  "expYear": 2028
}
```

简单，但类型扩展时字段会混杂。

### 方案二：判别字段 + 嵌套

```json
{
  "type": "card",
  "card": {
    "last4": "4242",
    "brand": "VISA"
  }
}
```

更清晰，但多一层。

### 方案三：`oneOf` / `anyOf`

```yaml
oneOf:
  - $ref: "#/components/schemas/CardPayment"
  - $ref: "#/components/schemas/BankTransferPayment"
  - $ref: "#/components/schemas/WalletPayment"
```

适合强 schema 场景，但代码生成和客户端处理可能复杂。

**建议**：对外 API 优先显式判别字段；不要让客户端靠“有没有某个字段”猜类型。

### 扩展字段

有时需要允许客户端携带自定义数据：

```json
{
  "id": "ord_123",
  "metadata": {
    "internalRef": "ERP-9981",
    "campaign": "autumn"
  }
}
```

需要明确：

- 键的长度和字符限制；
- 值的类型（字符串？JSON？）；
- 是否参与搜索；
- 是否返回给所有调用方；
- 是否有总量限制；
- 是否可以被用户删除。

`metadata` 很容易变成“绕过 schema 的后门”，必须限制。

## 7.7 字段选择与展开

### 稀疏字段

```http
GET /orders/ord_123?fields=id,status,total
```

适合减少移动端流量，但会增加缓存键复杂度和权限风险（字段级权限）。

需要定义：

- 是否支持嵌套字段（`customer.name`）；
- 未知字段是忽略还是报错；
- 必填字段是否总是返回（如 `id`）；
- 字段选择是否影响权限检查；
- 是否允许 `*`。

### 展开关联

```http
GET /orders/ord_123?expand=customer,items.product
```

或：

```http
GET /orders/ord_123?include=customer
```

需要定义：

- 允许展开哪些关系；
- 展开深度限制；
- 展开是否影响权限；
- 展开结果的缓存策略；
- 默认返回 ID 还是对象；
- 展开数量上限。

**建议**：默认返回 ID；显式 `expand` 才返回对象；限制深度和数量。

### 字段选择的安全风险

如果字段级权限没有和字段选择配合，可能出现：

```http
GET /users/123?fields=email,phone,ssn
```

调用方请求了它无权查看的字段。服务端必须：

- 先做对象级授权；
- 再做字段级过滤；
- 对无权字段返回 403 或静默省略（需明确）；
- 不要让字段选择绕过权限。

## 7.8 请求 ID 与追踪

每个响应应包含可追踪 ID：

```http
Request-Id: req_01J9Z6Q8K3W7V2M4N5P6R7S8T9
```

调用方在报错时提供该 ID，服务端可以快速定位日志和 Trace。

也可以支持调用方传入：

```http
X-Request-Id: client-generated-id
```

服务端应校验格式、防止注入，并在内部生成独立 ID 以防冲突。

### 追踪头的关系

- `Request-Id`：单次请求的 ID；
- `traceparent` / `tracestate`：W3C Trace Context，用于分布式追踪；
- `Correlation-Id`：跨业务流程的关联 ID；
- `Idempotency-Key`：幂等键，不是追踪 ID。

这些头各有用途，不要混用。一个常见错误是把 `Idempotency-Key` 当 `Request-Id` 返回，导致日志关联错乱。

### 错误响应中的追踪

错误响应应包含：

```json
{
  "type": "https://errors.example.com/order/insufficient-inventory",
  "title": "库存不足",
  "status": 422,
  "requestId": "req_01J9Z6Q8K3W7V2M4N5P6R7S8T9",
  "traceId": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

这样调用方报错时只需提供一个 ID，支持团队就能找到完整链路。

## 7.9 压缩与编码

- 支持 `gzip`、`br`（Brotli）；
- 大响应考虑 `zstd`（需客户端支持）；
- 不要压缩已压缩的媒体（图片、视频）；
- 注意压缩炸弹与解压成本；
- 流式响应谨慎使用压缩，可能增加延迟；
- 对小响应（<1KB）压缩可能得不偿失。

```http
Accept-Encoding: gzip, br
Content-Encoding: br
```

### 压缩与缓存

- 缓存键应包含 `Accept-Encoding`，或使用 `Vary`；
- 不同压缩算法的响应不应互相覆盖；
- CDN 可能自动压缩，源站不要重复压缩；
- 压缩后的 `Content-Length` 是压缩后长度，不是原始长度。

### 流式与压缩

SSE、NDJSON 等流式响应如果使用压缩，可能因为缓冲区导致延迟。需要：

- 关闭或限制压缩缓冲；
- 使用 `Content-Encoding: identity`；
- 或在协议层协商。

## 7.10 响应设计检查清单

1. 字段名是否统一风格？
2. 金额、时间、枚举是否有明确类型和语义？
3. `null` 与缺失是否有文档说明？
4. 单资源与列表的结构是否一致？
5. 错误是否使用标准格式，且与成功响应分离？
6. 是否返回了 `Request-Id`？
7. 是否支持必要的字段选择/展开？是否有滥用防护？
8. 大响应是否有分页/流式/压缩策略？
9. 未知字段和未知枚举是否可兼容？
10. 响应是否泄露内部实现或敏感数据？
11. 是否包含缓存头（`Cache-Control`、`ETag`）？
12. 是否包含限流头？
13. 大整数、浮点数、Unicode 是否有精度/编码问题？
14. 空数组、空对象、`null` 是否一致？

## 7.11 本章小结

- 表示层是契约的可见部分，类型精度比字段数量更重要。
- 媒体类型要准确，错误格式要标准。
- 裸对象适合单资源，列表用统一分页信封。
- 金额、时间、枚举、空值必须有明确语义。
- JSON 有精度和编码边界，关键字段要用字符串或明确范围。
- 多态用判别字段，不靠字段存在性猜类型。
- 字段选择与展开要限制深度、权限和缓存复杂度。
- Request-Id 是可观测性的最低配置。
- 压缩要与缓存、流式、CDN 协同设计。
- 二进制格式、压缩与 Schema 演进的完整取舍，见[附录 AE](../appendices/ae-serialization-schema-evolution.md)；签名与幂等指纹的字节级要求，见[附录 AD](../appendices/ad-cryptography-pqc.md)。
- 时间、金额、单位、标识与语言这些“看起来只是类型”的字段，完整契约见[附录 AH](../appendices/ah-primitive-semantics.md)。

## 设计练习

为一个“酒店预订 API”设计表示：

- 酒店、房型、价格、库存、预订、取消；
- 金额含税费与币种；
- 时间含入住/退房日期与酒店时区；
- 房态枚举可能扩展；
- 支持按需展开酒店图片。

写出创建预订的请求与成功/失败响应，并说明每个字段的类型与语义。然后回答：

1. 入住日期是“日期”还是“时刻”？跨时区如何处理？
2. 价格中的税费、服务费、押金如何表示？
3. 房态新增枚举值时，旧客户端如何兼容？
4. 如果客户端请求 `fields=price,availability`，权限如何检查？

---

上一章：[命名、标识与 URL 设计](06-naming-and-urls.md)  
下一章：[错误设计：让失败可理解、可恢复](08-errors.md)
