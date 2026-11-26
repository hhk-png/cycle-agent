# 第 26 章 Webhook 工程：签名、重试、去重与可观测

## 26.1 Webhook 是反向 API

普通 API 是调用方主动请求；Webhook 是服务端主动通知调用方。它把“谁控制节奏”反转了：

```text
普通 API：Client → Server
Webhook：Server → Client 的 HTTP 端点
```

这意味着：

- 你的服务器要发起出站请求；
- 对方的端点可能宕机、慢、拒绝；
- 网络不可靠；
- 需要重试、签名、去重；
- 需要防止 SSRF；
- 需要可观测性；
- 需要处理对方的安全策略；
- 需要管理订阅生命周期。

Webhook 不是“发个 POST 就完了”，而是一套可靠的异步投递系统。

## 26.2 订阅模型

### 静态配置

调用方在控制台填写 URL，选择事件类型。

### API 管理

```http
POST /webhook-subscriptions
```

```json
{
  "url": "https://partner.example.com/hooks/orders",
  "eventTypes": ["order.created", "order.paid", "order.cancelled"],
  "secret": "whsec_...",
  "description": "生产环境订单同步",
  "active": true
}
```

响应：

```json
{
  "id": "whsub_123",
  "url": "https://partner.example.com/hooks/orders",
  "eventTypes": ["order.created", "order.paid", "order.cancelled"],
  "secretLastFour": "3f2a",
  "createdAt": "2026-11-26T10:00:00Z"
}
```

### 事件类型

- 使用稳定命名：`order.created`、`payment.succeeded`；
- 支持通配符要谨慎；
- 版本化：`order.created.v2`；
- 文档说明 payload schema；
- 允许订阅全部或按资源过滤。

### 订阅生命周期

```text
CREATED → PENDING_VERIFICATION → ACTIVE → PAUSED → DISABLED
                                  │         │
                                  │         └→ ACTIVE（恢复）
                                  └→ DISABLED（主动停用）
PENDING_VERIFICATION → DISABLED（验证超时）
```

需要支持：

- 创建；
- 更新 URL；
- 暂停；
- 恢复；
- 删除；
- 轮换密钥；
- 查看投递记录；
- 手动重放。

### 订阅的过滤

```json
{
  "eventTypes": ["order.paid"],
  "filter": {
    "amount": { "gte": 10000 },
    "currency": "CNY"
  }
}
```

过滤可以：

- 减少无效投递；
- 降低接收方负载；
- 但增加提供方复杂度；
- 需要明确过滤语义。

## 26.3 投递格式

```http
POST /hooks/orders HTTP/1.1
Host: partner.example.com
Content-Type: application/json
User-Agent: Example-Webhooks/1.0
X-Webhook-Id: evt_01J9Z6...
X-Webhook-Timestamp: 1795688130
X-Webhook-Signature: v1=base64...
X-Webhook-Event: order.paid
```

```json
{
  "id": "evt_01J9Z6...",
  "type": "order.paid",
  "version": "1",
  "createdAt": "2026-11-26T10:15:30Z",
  "data": {
    "orderId": "ord_123",
    "amount": 12900,
    "currency": "CNY"
  }
}
```

### 幂等

每个事件有唯一 `id`。接收方必须按 `id` 去重，因为投递是至少一次。

### 顺序

Webhook 通常不保证全局顺序。若需要顺序：

- 按资源 ID 串行投递；
- 携带 `sequence`；
- 接收方按版本处理；
- 或者改用事件流。

### 投递的响应

接收方应快速返回 2xx：

```http
HTTP/1.1 200 OK
```

不要在 Webhook 处理器里做长时间同步工作；应落库后异步处理。

响应体通常被忽略，但可以返回：

```json
{ "received": true }
```

## 26.4 签名

### HMAC

```text
signed_content = timestamp + "." + raw_body
signature = HMAC-SHA256(secret, signed_content)
```

发送：

```http
X-Webhook-Timestamp: 1795688130
X-Webhook-Signature: v1=base64(HMAC)
```

接收方验证：

1. 读取原始 body（不要先解析再序列化）；
2. 检查时间戳在容忍窗口内（如 5 分钟）；
3. 计算 HMAC；
4. 常量时间比较；
5. 拒绝旧/重复请求。

### 为什么要签时间戳

只签名 body 不能防止重放：攻击者可以截获请求并重复发送。时间戳 + 事件 ID 去重可以限制重放窗口。

### 为什么要用原始 body

很多框架会解析 JSON 再重新序列化，导致：

- 键顺序变化；
- 空格变化；
- 数字格式变化；
- Unicode 转义变化。

签名会失败。应保留原始字节。

### 非对称签名

使用 Ed25519/RSA：

- 发送方私钥签名；
- 接收方公钥验证；
- 公钥可轮换；
- 更适合多接收方；
- 不需要共享 secret。

### 密钥轮换

- 支持多个 active secret；
- 新旧密钥并行；
- 接收方按签名尝试所有 active key；
- 提供轮换窗口；
- 旧密钥最终禁用。

### 标准化的签名格式

每个平台都自定义一套签名头，会让接收方重复实现、重复踩坑。两类标准值得优先采用：

**Standard Webhooks**（适合大多数 Webhook）：

```http
webhook-id: msg_2b3c...
webhook-timestamp: 1764200000
webhook-signature: v1,Base64(HMAC-SHA256(secret, id + "." + timestamp + "." + body))
```

- 签名内容为 `${webhook-id}.${webhook-timestamp}.${rawBody}`；
- 密钥通常以 `whsec_` 前缀分发，Base64 解码后使用；
- 接收方允许例如 5 分钟的时钟偏差；
- 轮换期间可同时发送多个 `v1` 签名，任一匹配即通过；
- `webhook-id` 同时作为去重键。

**RFC 9421 HTTP Message Signatures**（适合跨组织、需要覆盖方法/路径/头部的场景）：

```http
Content-Digest: sha-256=:d435t5...:
Signature-Input: sig1=("@method" "@target-uri" "content-digest");created=1764200000;keyid="wh-2026-11";alg="ed25519"
Signature: sig1=:MEUCIQ...:
```

它把“签哪些组件、何时过期、用哪把密钥”标准化，适合请求经过多个网关、代理、集成平台，或需要非对称验证的场景。完整讨论见[附录 AD](../appendices/ad-cryptography-pqc.md)。

| 方案 | 复杂度 | 适用 | 注意 |
|---|---|---|---|
| 自定义 HMAC | 低 | 内部/简单合作 | 每家格式不同，文档要极清楚 |
| Standard Webhooks | 低 | 大多数 SaaS Webhook | 统一头、时间戳、轮换 |
| RFC 9421 | 中高 | 跨组织、网关改写、审计 | 规范化、组件选择、工具支持 |
| 非对称 JWS | 中 | 多接收方、公开验证 | `alg`/`kid`/JWKS 校验 |

无论选哪种，验证顺序都应是：**解析签名头 → 解析密钥 → 检查时间窗 → 校验 body digest → 验签 → 查重放 → 再处理业务**。

## 26.5 重试

推荐策略：

```text
第 1 次：立即
第 2 次：10 秒
第 3 次：30 秒
第 4 次：2 分钟
第 5 次：10 分钟
第 6 次：1 小时
第 7 次：6 小时
...
最长 24～72 小时
```

要点：

- 指数退避 + 抖动；
- 尊重接收方 `Retry-After`（如果返回 429/503）；
- 4xx（除 408/429）通常不重试；
- 5xx/超时重试；
- 记录每次尝试；
- 死信 + 人工重放；
- 防止重试风暴。

### 哪些响应重试

| 响应 | 重试 | 原因 |
|---|---|---|
| 2xx | 否 | 成功 |
| 400 | 否 | 请求格式错误 |
| 401/403 | 否 | 认证/权限问题 |
| 404 | 否 | 端点不存在 |
| 408 | 是 | 请求超时 |
| 409 | 视情况 | 可能重复 |
| 429 | 是 | 限流，遵循 Retry-After |
| 5xx | 是 | 服务端问题 |
| 超时 | 是 | 网络问题 |

### 重试的副作用

重试可能导致重复处理。接收方必须幂等。

## 26.6 去重

接收方：

```text
if event_id seen:
    return 200
else:
    store event_id and process transactionally
    return 200
```

去重表需要：

- 唯一约束；
- TTL/清理；
- 与业务处理同事务；
- 处理并发重复；
- 按订阅/消费者隔离。

### 去重的边界

- 去重窗口多长？
- 事件 ID 是否全局唯一？
- 多实例如何共享去重状态？
- 去重失败如何处理？

## 26.7 安全

### SSRF

Webhook URL 由用户提供，可能指向内网：

- 禁止私有 IP；
- 禁止元数据地址；
- DNS 解析后校验；
- 不跟随重定向；
- 出网代理；
- 限制端口/协议；
- 超时与响应大小限制；
- 只允许 https。

### 重放

- 时间戳窗口；
- 事件 ID 去重；
- 签名；
- 不接受无签名的请求；
- 记录失败尝试；
- 使用 nonce。

### 密钥泄露

- 每个订阅独立 secret；
- 支持轮换；
- 不在日志中打印；
- 泄露后可立即吊销；
- 提供密钥轮换 API；
- 通知订阅者。

### 接收方安全

- 验证签名；
- 限制请求大小；
- 限制处理时间；
- 防重放；
- 幂等；
- 不要把 Webhook 端点暴露为管理员接口；
- 记录审计。

## 26.8 端点验证

订阅时验证 URL 所有权：

- 发送 challenge；
- 对方回显；
- 或发送签名请求，对方返回 2xx。

防止攻击者订阅他人的 URL 造成骚扰。

### Challenge 流程

```text
1. 订阅创建 → PENDING_VERIFICATION
2. 发送 POST，带 challenge
3. 接收方返回 challenge
4. 验证通过 → ACTIVE
5. 超时未验证 → 删除
```

## 26.9 可观测性

发送方：

- 投递成功率；
- 延迟；
- 重试次数；
- 失败原因；
- 每个订阅的健康度；
- 事件积压；
- 手动重放；
- 端点响应时间；
- 按租户/事件类型统计。

接收方：

- 收到/处理成功；
- 签名失败；
- 重复事件；
- 处理延迟；
- 错误日志；
- 死信。

### 订阅健康度

```json
{
  "subscriptionId": "whsub_123",
  "status": "DEGRADED",
  "lastSuccessAt": "2026-11-26T09:00:00Z",
  "lastFailureAt": "2026-11-26T10:00:00Z",
  "consecutiveFailures": 12,
  "deliverySuccessRate24h": 0.82
}
```

## 26.10 订阅 API 的完整契约

Webhook 不只是“往 URL 发 POST”。订阅本身是一个需要生命周期管理的资源：

```http
POST   /webhook-subscriptions
GET    /webhook-subscriptions
GET    /webhook-subscriptions/{id}
PATCH  /webhook-subscriptions/{id}
DELETE /webhook-subscriptions/{id}
POST   /webhook-subscriptions/{id}/test
POST   /webhook-subscriptions/{id}/rotate-secret
POST   /webhook-subscriptions/{id}/pause
POST   /webhook-subscriptions/{id}/resume
GET    /webhook-subscriptions/{id}/deliveries
POST   /webhook-subscriptions/{id}/deliveries/{deliveryId}/replay
```

订阅资源至少包含：

```json
{
  "id": "whsub_01J9...",
  "url": "https://partner.example.com/hooks/orders",
  "eventTypes": ["order.paid", "order.refunded"],
  "filters": { "currency": ["CNY"] },
  "status": "active",
  "secret": { "lastRotatedAt": "2026-11-01T00:00:00Z", "nextRotationAt": "2027-02-01T00:00:00Z" },
  "description": "生产订单回调",
  "createdAt": "2026-10-01T00:00:00Z"
}
```

设计要点：

- **事件类型**：用稳定名称（`order.paid`），不要用内部类名；
- **过滤**：过滤语义要写进契约，且不能泄露未授权数据；
- **状态**：`active` / `paused` / `disabled` / `pending_verification`；
- **密钥**：创建时只返回一次明文，之后只显示指纹与轮换时间；
- **测试事件**：与真实事件同结构，有明确标记，不触发业务副作用；
- **投递日志**：保留请求 ID、响应码、耗时、重试次数、失败原因；
- **重放**：按投递 ID 或事件 ID 重放，权限与审计要明确；
- **暂停/恢复**：暂停期间的事件是否补发？必须在契约中写清；
- **自动禁用**：连续失败后禁用，并通知订阅者；
- **多租户**：订阅属于租户，URL、密钥、日志都要隔离。

订阅变更本身也要幂等：重复创建同一 URL/事件类型不应产生重复订阅；PATCH 的语义要区分“未提供”和“清空”。

## 26.11 投递系统的容量与隔离

Webhook 的故障经常不是“签名错了”，而是“某个接收方慢了，拖垮了所有人”。

### 队列与隔离

```text
事件 → 按订阅/租户分区的队列 → 发送 worker → 接收方
```

- 每个租户/订阅有独立队列或至少独立并发配额；
- 慢接收方不能占满全局 worker；
- 重试使用延迟队列，不要阻塞主队列；
- 限制单订阅的 in-flight 数量；
- 对高价值事件与低价值事件分层。

### 重试预算与熔断

- 每个订阅有重试预算（次数/时间窗）；
- 连续失败触发熔断，暂停投递并告警；
- 4xx（除 408/429）通常不重试，避免把错误请求重放成风暴；
- 429 要尊重 `Retry-After`；
- 重试要有抖动，避免所有接收方同时被重试。

### 顺序与分区

- 全局顺序通常不值得承诺；
- 需要顺序时，按资源 ID 分区，单分区串行；
- 顺序与重试冲突：失败的事件会阻塞后续事件，需要明确的队头阻塞策略；
- 跨分区不保证顺序，契约要写清。

### 容量指标

至少监控：

- 队列深度与最老事件年龄；
- 投递成功率、失败率、重试率；
- 各响应码分布；
- 单订阅 in-flight 与积压；
- 投递延迟 P50/P95/P99；
- 死信数量与重放次数；
- 单事件平均投递次数（重试放大）；
- 接收方超时比例。

### 防重试风暴

- 指数退避 + 抖动；
- 每订阅/每租户的重试上限；
- 熔断与半开探测；
- 死信后人工介入，而不是无限重试；
- 对同一接收方的并发重试限流；
- 大范围故障时暂停低优先级事件。

## 26.12 调试工具

- 事件日志；
- 请求/响应记录；
- 手动重发；
- 测试事件；
- 签名验证工具；
- 沙箱端点；
- Webhook 签名示例代码；
- 事件回放；
- 端点健康检查。

## 26.13 反模式

- 无签名；
- 无重试；
- 无去重；
- 同步处理长任务；
- 不校验 URL（SSRF）；
- 重试无退避；
- 4xx 也无限重试；
- 不记录投递结果；
- 不提供重放；
- 事件 payload 包含敏感数据；
- 没有版本化；
- 订阅密钥全局共享；
- 端点验证可绕过；
- 重试风暴拖垮接收方。

## 26.14 本章小结

- Webhook 是反向 API，需要可靠的投递系统。
- 订阅、事件类型、签名、重试、去重、可观测性缺一不可。
- HMAC 签名要基于原始 body 和时间戳；跨组织场景优先考虑 Standard Webhooks 或 RFC 9421。
- 接收方应快速 2xx，异步处理。
- SSRF 防护是必须的。
- 提供日志、重放和测试工具，才能让集成可运维。
- 订阅生命周期和密钥轮换需要设计。
- 订阅 API 本身要有完整契约：事件类型、过滤、状态、密钥、测试事件、投递日志、重放、暂停/恢复。
- 投递系统要按租户/订阅隔离，有重试预算、熔断与容量指标，避免慢接收方拖垮全局。

## 设计练习

为“支付平台”设计 Webhook：

- 支付成功、退款、争议；
- 多租户；
- 签名与密钥轮换；
- 重试 72 小时；
- 死信与重放；
- 防 SSRF；
- 接收方去重。

写出订阅 API、投递格式、签名算法和运维界面。然后回答：

1. 接收方返回 500 时，重试多少次？
2. 接收方返回 400 时，是否重试？
3. 如何防止攻击者订阅内网地址？
4. 密钥轮换期间，如何兼容新旧签名？

---

上一章：[事件驱动：CloudEvents、AsyncAPI 与消息契约](25-event-driven.md)  
下一章：[API 网关与边缘架构](27-gateway-and-edge.md)
