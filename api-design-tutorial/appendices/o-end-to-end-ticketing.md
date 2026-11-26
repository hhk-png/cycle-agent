# 附录 O：端到端实战——设计一个活动票务平台 API

前面五十六章分别讨论了契约、协议、安全、工程、领域与商业。真实的 API 设计不是把这些章节逐条套用，而是在同一组约束下同时做取舍。本附录用一个完整的案例，把分散的知识重新串起来。

案例选择“活动票务平台”，因为它同时包含：

- 强一致库存（不能超卖）；
- 高并发峰值（开票瞬间）；
- 支付与退款（资金不能错）；
- 实时状态（座位图）；
- 离线场景（现场检票）；
- 多租户与合作伙伴；
- 文件与媒体（票面、证件、场地图）；
- AI Agent（自然语言购票）；
- 清晰的商业模型（抽成 + SaaS 费）。

它几乎覆盖了本书所有重要主题，却又足够具体，可以做出一份能落地的契约。

## O.1 案例边界

假设我们要为“巡演、剧场、展览、体育赛事”提供一个多租户票务平台：

- 主办方（organizer）在平台上创建活动与场次；
- 场馆（venue）提供座位图与入场规则；
- 观众（customer）浏览、选座、下单、支付、收票；
- 检票员（staff）在场馆入口扫码核验；
- 合作伙伴（partner）通过 API 分销或查询库存；
- 客服（support）处理退款、改签与争议；
- AI 助手（agent）代表用户查询与购票。

不在本案例范围内：复杂动态定价算法、二级票务市场、跨境多币种清算、完整的税务引擎。这些可以作为扩展练习。

## O.2 需求、角色与约束

### 功能需求

1. 活动与场次管理；
2. 座位图与分区票价；
3. 实时库存与选座；
4. 预占、下单、支付、出票；
5. 退款、取消、改签；
6. 电子票、转让、检票；
7. 合作伙伴分销；
8. 报表与对账；
9. 多租户隔离；
10. AI 助手工具。

### 质量属性

| 属性 | 目标 | 主要影响 |
|---|---|---|
| 一致性 | 同一座位绝不重复售出 | 库存模型、事务、幂等 |
| 可用性 | 售票核心 99.95% | 多区域、降级、排队 |
| 延迟 | 选座 P95 < 300ms；下单 P95 < 800ms | 缓存、索引、异步 |
| 峰值 | 开票 1 分钟内 50 万请求 | 限流、队列、预热 |
| 安全 | 防刷、防黄牛、防伪造票 | 认证、风控、签名 |
| 离线 | 检票断网可继续 | 本地密钥、离线队列 |
| 合规 | 隐私、支付、消费者权益 | 数据最小化、审计、退款 |

### 关键约束

- 座位是**有限且不可替代**的资源，超卖比“少卖”严重得多；
- 支付结果可能异步到达，订单状态不能只靠同步响应；
- 现场网络不可靠，检票不能完全依赖在线；
- 主办方、场馆、合作伙伴都可能带来自己的身份体系；
- 大促流量可能是日常的 1000 倍，不能按均值做容量规划。

## O.3 消费者故事与 API 表面

先写消费者故事，而不是先画 URL。

| 消费者 | 故事 | 关键 API |
|---|---|---|
| 观众 | 我想看某场演出还有哪些座位 | 活动搜索、场次、座位图 |
| 观众 | 我想先锁座再慢慢付款 | 预占、订单、支付 |
| 观众 | 我想把票转给朋友 | 票转让 |
| 主办方 | 我想看销售与入场进度 | 报表、实时统计 |
| 场馆 | 我想控制入场与重复入场 | 检票、票策略 |
| 合作伙伴 | 我想批量查询库存并下单 | 分销 API、Webhook |
| 客服 | 我想处理退款与争议 | 订单查询、退款、备注 |
| AI 助手 | 我想用自然语言帮用户买票 | 工具集、确认、支付 |

### API 表面

```text
# 发现
GET    /v1/events
GET    /v1/events/{eventId}
GET    /v1/events/{eventId}/performances
GET    /v1/performances/{performanceId}/seat-map

# 库存与预占
POST   /v1/holds
GET    /v1/holds/{holdId}
DELETE /v1/holds/{holdId}

# 订单与支付
POST   /v1/orders
GET    /v1/orders/{orderId}
POST   /v1/orders/{orderId}/pay
POST   /v1/orders/{orderId}/cancel
POST   /v1/orders/{orderId}/refunds

# 票
GET    /v1/tickets/{ticketId}
POST   /v1/tickets/{ticketId}/transfer
POST   /v1/tickets/{ticketId}/revoke

# 检票
POST   /v1/check-ins
GET    /v1/check-ins/{checkInId}

# 合作伙伴
GET    /v1/partner/availability
POST   /v1/partner/orders
POST   /v1/webhook-subscriptions

# 管理
GET    /v1/reports/sales
GET    /v1/audit-events
```

这个表面刻意把“预占”和“订单”分开。预占是短生命周期的资源锁，订单是商业承诺，两者失败语义不同。把二者混成一个 `POST /orders` 会让“锁座失败”和“支付失败”难以区分。

## O.4 领域建模与边界

### 有界上下文

```text
Catalog        活动、场次、场馆、座位图、票价
Inventory      座位状态、预占、库存池
Sales          购物车、订单、价格快照、优惠
Payments       支付意图、扣款、退款、对账
Ticketing      票、二维码、转让、吊销
Access         检票、入场策略、设备
Partner        分销、佣金、结算
Identity       用户、组织、角色、租户
Analytics      销售、流量、转化
```

每个上下文有自己的聚合与不变量。跨上下文只通过 ID 与事件协作，不共享数据库表。

### 核心聚合

| 聚合 | 根 | 关键不变量 |
|---|---|---|
| Event | Event | 一个活动属于一个租户 |
| Performance | Performance | 场次时间、场馆、座位图版本固定 |
| SeatMap | SeatMap | 座位坐标与分区不可变；新版本另建 |
| Hold | Hold | 一个座位同时最多一个有效预占 |
| Order | Order | 金额、座位、票价在下单时快照 |
| Payment | PaymentIntent | 支付总额等于订单应付 |
| Ticket | Ticket | 一张票对应一个座位与一个场次 |
| CheckIn | CheckIn | 同一张票在同一场次只能有效入场一次 |

### 状态机

订单：

```text
draft → pending_payment → paid → confirmed
             ├→ expired
             ├→ canceled
             └→ payment_failed → pending_payment
confirmed → partially_refunded → refunded
confirmed → canceled（按政策）
```

预占：

```text
active → converted（转为订单）
      ├→ expired
      └→ released
```

票：

```text
issued → active → checked_in
              ├→ transferred
              ├→ revoked
              └→ refunded
```

状态机的每个转换都要回答：谁可以触发、是否幂等、失败后停在哪个状态、是否产生事件。

## O.5 风格选型

| 场景 | 选择 | 理由 |
|---|---|---|
| 活动/场次/订单 CRUD | REST + JSON | 资源清晰、缓存与工具成熟 |
| 座位图实时变化 | SSE | 单向推送、断线可恢复、实现简单 |
| 支付与渠道回调 | Webhook + 查询兜底 | 异步结果、可重试 |
| 报表导出 | 异步操作资源 | 耗时长、结果大 |
| 合作伙伴批量库存 | REST + 批量端点 | 简单、可缓存 |
| AI 助手 | MCP 工具 | 模型可直接发现与调用 |
| 内部服务 | gRPC | 强类型、低延迟、流式 |

不选 GraphQL 作为主入口：票务的查询模式相对固定，而 GraphQL 的复杂度限制、缓存与授权成本在这个场景不值得。可以在 BFF 层为多端聚合使用 GraphQL，但不把核心库存暴露为任意图查询。

## O.6 资源、标识与 URL

### 标识

```text
evt_01J9Z6...     活动
prf_01J9Z6...     场次
map_01J9Z6...     座位图版本
hold_01J9Z6...    预占
ord_01J9Z6...     订单
pay_01J9Z6...     支付意图
tkt_01J9Z6...     票
chk_01J9Z6...     检票记录
```

使用带前缀的不透明 ID，不暴露自增主键。ID 一旦对外，就不能复用，也不能编码敏感信息。

### URL 规则

- 资源用复数名词：`/events`、`/orders`、`/tickets`；
- 层级不超过两层：`/events/{id}/performances` 可以，`/venues/{id}/events/{id}/performances/{id}/seats` 太深；
- 动作优先用子资源或状态字段；确实需要命令时用 `:action` 或独立端点，并在全 API 保持一致；
- 版本放路径：`/v1/...`；破坏性变更才升主版本；
- 查询参数用 `camelCase`，JSON 字段也用 `camelCase`，不要一半 snake_case 一半 camelCase。

```http
GET /v1/events?city=shanghai&from=2026-12-01&to=2026-12-31&limit=20&cursor=...
GET /v1/performances/prf_123/seat-map?include=priceTiers,restrictions
```

## O.7 表示、错误与时间

### 金额

金额用最小货币单位整数，另带币种：

```json
{
  "total": { "amount": 12900, "currency": "CNY" }
}
```

`12900` 表示 129.00 元。不要用浮点数，也不要在字段名里暗示单位（如 `amountCents`）而不写清币种。

### 时间

- 时刻用 RFC 3339 UTC：`2026-12-01T11:30:00Z`；
- 场次的“本地时间”另带时区：`"localStart": "2026-12-01T19:30:00+08:00"`；
- 入场日期这种“没有时间的日期”用 `2026-12-01`，不要伪造成午夜时刻；
- 所有时间字段在文档中说明是“时刻”还是“本地日期”。

### 错误

统一使用 `application/problem+json`：

```json
{
  "type": "https://api.example.com/problems/seat-unavailable",
  "title": "座位不可用",
  "status": 409,
  "code": "SEAT_UNAVAILABLE",
  "detail": "座位 A-12 已被其他用户预占。",
  "instance": "/v1/holds",
  "requestId": "req_abc",
  "seats": ["A-12"],
  "retryable": false,
  "nextAction": "请重新选择座位。"
}
```

错误设计要点：

- `code` 稳定，调用方据此分支；
- `detail` 可本地化，但调用方不应解析；
- `retryable` 明确告诉客户端能否重试；
- `nextAction` 给人和 Agent 都可读的下一步；
- 不要用 `200 OK` 包裹业务失败。

## O.8 库存、座位与预占

### 座位图模型

```json
{
  "id": "map_123",
  "version": 7,
  "venueId": "ven_123",
  "sections": [
    {
      "id": "A",
      "name": "A 区",
      "seats": [
        {
          "id": "A-12",
          "row": "A",
          "number": "12",
          "x": 120,
          "y": 40,
          "priceTierId": "tier_1",
          "accessible": false,
          "obstructed": false
        }
      ]
    }
  ]
}
```

座位图是不可变版本。场馆改布局时创建新版本，已售出的票仍指向旧版本。不要把座位坐标与价格写进订单的可变引用，订单要保存快照。

### 预占语义

预占是“短期锁座”，不是订单：

```http
POST /v1/holds
Idempotency-Key: 7d3f9c2a-...
Content-Type: application/json

{
  "performanceId": "prf_123",
  "seats": ["A-12", "A-13"],
  "channel": "web"
}
```

```json
{
  "id": "hold_123",
  "status": "active",
  "performanceId": "prf_123",
  "seats": [
    { "seatId": "A-12", "priceTierId": "tier_1", "unitPrice": { "amount": 12900, "currency": "CNY" } },
    { "seatId": "A-13", "priceTierId": "tier_1", "unitPrice": { "amount": 12900, "currency": "CNY" } }
  ],
  "expiresAt": "2026-11-26T10:10:00Z",
  "createdAt": "2026-11-26T10:00:00Z"
}
```

规则：

- 一个座位在同一场次同一时刻最多一个 `active` 预占；
- 预占默认 5～10 分钟，可配置；到期自动释放；
- 预占不承诺价格永久有效，但应在响应中给出价格快照；
- 预占失败返回 `409 SEAT_UNAVAILABLE`，并列出冲突座位；
- 相同幂等键重试返回同一预占，不重复锁座；
- 用户取消或转为订单时，预占状态变为 `released` 或 `converted`。

### 防超卖

单库可以用唯一约束或行锁：

```sql
-- 座位状态表：一个座位一行
UPDATE seat_inventory
SET status = 'held', hold_id = $1, expires_at = $2
WHERE performance_id = $3
  AND seat_id = ANY($4)
  AND (status = 'available' OR (status = 'held' AND expires_at < now()))
RETURNING seat_id;
```

如果返回行数小于请求座位数，说明有座位不可用，事务回滚。这个方案简单、正确，但在极端峰值下会成为行锁热点。

高并发场景常用分层：

1. **Redis 预占**：用 Lua 脚本原子检查并写入带 TTL 的座位键；
2. **数据库最终一致**：异步把预占落入数据库，冲突时以数据库为准；
3. **对账修复**：定期扫描 Redis 与数据库差异，释放孤儿预占；
4. **排队**：开票瞬间用虚拟等待室把流量削峰，而不是让所有请求同时打库存。

```lua
-- 伪代码：原子预占多个座位
for _, seat in ipairs(KEYS) do
  if redis.call("EXISTS", seat) == 1 then
    return 0
  end
end
for _, seat in ipairs(KEYS) do
  redis.call("SET", seat, holdId, "PX", ttlMs)
end
return 1
```

Redis 方案要接受一个事实：Redis 不是唯一真相。进程崩溃、网络分区、TTL 提前过期都可能造成不一致。最终必须由数据库约束与对账兜底。

### 黄牛与刷票

- 账号维度限购：每用户/每证件/每支付方式；
- 设备指纹与行为风控；
- 预占接口单独限流，比查询更严；
- 高风险订单要求验证码或实名；
- 票转让限制次数与时间窗；
- 不要用“隐藏座位”作为主要防刷手段，那只是提高门槛。

## O.9 订单与支付

### 创建订单

```http
POST /v1/orders
Idempotency-Key: 8e4a...
Content-Type: application/json

{
  "holdId": "hold_123",
  "customer": { "email": "alice@example.com" },
  "acceptTermsVersion": "2026-10-01"
}
```

```json
{
  "id": "ord_123",
  "status": "pending_payment",
  "performanceId": "prf_123",
  "seats": ["A-12", "A-13"],
  "subtotal": { "amount": 25800, "currency": "CNY" },
  "fees": [
    { "type": "service_fee", "amount": { "amount": 2580, "currency": "CNY" } }
  ],
  "total": { "amount": 28380, "currency": "CNY" },
  "expiresAt": "2026-11-26T10:15:00Z",
  "payment": { "status": "requires_payment" }
}
```

要点：

- 价格在下单时快照，后续票价变化不影响已创建订单；
- 服务费、税费、折扣分行展示，账单可解释；
- 订单到期未支付自动取消并释放座位；
- 幂等键防止重复下单；
- 订单创建与预占转换在同一事务或可靠的补偿流程中完成。

### 支付

```http
POST /v1/orders/ord_123/pay
Idempotency-Key: 9f5b...
Content-Type: application/json

{
  "paymentMethodId": "pm_456",
  "returnUrl": "https://tickets.example.com/orders/ord_123/complete"
}
```

响应可能是：

```json
{
  "paymentId": "pay_123",
  "status": "requires_action",
  "nextAction": {
    "type": "redirect",
    "url": "https://bank.example.com/3ds?...",
    "expiresAt": "2026-11-26T10:10:00Z"
  }
}
```

支付状态：

```text
requires_payment_method → requires_confirmation → processing
                                              ├→ succeeded
                                              ├→ requires_action → processing
                                              └→ failed
```

规则：

- 支付成功以渠道 Webhook 或服务端查询为准，不以浏览器回调为准；
- 同一订单重复支付要幂等，不能产生两笔扣款；
- 支付超时后先查询渠道，再决定重试或取消；
- 支付成功后异步出票，但订单状态可以先变为 `paid`；
- 退款是独立资源，支持部分退款与多次退款；
- 所有资金操作有账本分录与对账 ID。

### 订单与支付的一致性

```text
创建订单（座位 held）
  → 创建支付意图
  → 用户支付
  → 渠道回调 succeeded
  → 订单 paid
  → 出票
  → 预占 converted
```

任何一步失败都要有补偿：

- 支付成功但出票失败：订单保持 `paid`，进入重试队列，不能让用户重复支付；
- 出票成功但订单状态未更新：以支付记录与出票记录对账修复；
- 预占过期但支付仍在处理：先查询支付结果，再决定释放还是保留。

## O.10 出票、核验与转让

### 票的结构

```json
{
  "id": "tkt_123",
  "orderId": "ord_123",
  "performanceId": "prf_123",
  "seatId": "A-12",
  "status": "active",
  "holder": { "name": "Alice", "email": "alice@example.com" },
  "qr": {
    "payload": "eyJhbGciOi...",
    "expiresAt": "2026-12-02T00:00:00Z"
  },
  "issuedAt": "2026-11-26T10:05:00Z"
}
```

### 二维码签名

二维码不应只是票 ID，否则可以被猜测或伪造。推荐使用签名令牌：

```json
{
  "tkt": "tkt_123",
  "prf": "prf_123",
  "seat": "A-12",
  "iat": 1795687500,
  "exp": 1796169600,
  "nonce": "n_abc",
  "iss": "https://api.example.com"
}
```

用 JWS 签名，检票端用公钥离线验签。要点：

- 私钥在服务端/KMS，公钥分发给检票设备；
- 令牌短命，最好与场次结束时间绑定；
- `nonce` 用于在线一次性校验，降低截图复用；离线场景无法实时查 nonce，需要设备侧去重与事后对账；
- 票转让后旧令牌失效，新持有人获得新令牌；
- 退款/吊销要把票加入撤销列表，并在设备联网时同步。

### 检票

```http
POST /v1/check-ins
Idempotency-Key: a1b2...
Content-Type: application/json

{
  "ticketToken": "eyJhbGciOi...",
  "gateId": "gate_3",
  "deviceId": "dev_9",
  "scannedAt": "2026-12-01T11:20:00Z"
}
```

```json
{
  "id": "chk_123",
  "ticketId": "tkt_123",
  "result": "accepted",
  "seat": "A-12",
  "holderName": "Alice",
  "checkedInAt": "2026-12-01T11:20:00Z"
}
```

结果：

- `accepted`：首次入场；
- `duplicate`：已经入场（可配置为放行或拒绝）；
- `invalid`：签名无效、过期、场次不符；
- `revoked`：票已退款/吊销；
- `wrong_gate`：入口不符；
- `offline_pending`：离线记录，待同步。

离线检票的设计：

1. 设备预下载有效公钥与场次信息；
2. 断网时本地验签、记录扫描日志（签名或哈希链）；
3. 恢复网络后批量上传，服务端按票 ID 去重；
4. 重复扫描以服务端首次记录为准，设备显示“待确认”；
5. 设备与服务端时间偏差要在允许范围内，否则拒绝或标记。

### 转让

```http
POST /v1/tickets/tkt_123/transfer
Idempotency-Key: b2c3...
Content-Type: application/json

{
  "toEmail": "bob@example.com",
  "message": "朋友，票转给你。"
}
```

转让要防：

- 黄牛批量转让；
- 转让后原持有人仍能入场；
- 邮件被转发导致多人持有同一令牌；
- 转让中的票被退款。

做法：转让生成新令牌、旧令牌立即失效、记录双方、限制次数与时间窗、转让确认后原持有人失去入场资格。

## O.11 实时座位图

座位图不需要每次变化都推送全量。推荐“快照 + 增量”：

```http
GET /v1/performances/prf_123/seat-map/stream
Accept: text/event-stream
```

```text
event: snapshot
id: 1
data: {"version":42,"seats":[{"id":"A-12","status":"available"},{"id":"A-13","status":"held"}]}

event: delta
id: 2
data: {"version":43,"changes":[{"id":"A-13","status":"sold"}]}

event: heartbeat
id: 3
data: {"ts":"2026-11-26T10:00:30Z"}
```

设计要点：

- `version` 单调递增，客户端据此判断是否漏事件；
- 断线重连带 `Last-Event-ID`，服务端从该版本之后重放；
- 重放窗口有限，超出窗口返回新快照；
- 事件可以合并/降频（如每 200ms 一批），不要为每个座位变化推送一次；
- 权限：只推送用户可见的座位与价格；
- 缓存：座位图是动态数据，不要在 CDN 缓存；
- 降级：SSE 不可用时回退到轮询，轮询间隔要退避。

## O.12 合作伙伴与 Webhook

合作伙伴需要批量查询库存与下单，但不能看到用户隐私。设计单独的 partner API：

```http
GET /v1/partner/availability?performanceId=prf_123&priceTierId=tier_1
Authorization: Bearer <partner token>
```

返回聚合库存，不返回具体座位持有人信息。下单使用 partner 订单，佣金与结算单独记录。

Webhook 事件：

```json
{
  "specversion": "1.0",
  "id": "evt_123",
  "type": "com.example.ticket.checked_in",
  "source": "https://api.example.com",
  "subject": "tickets/tkt_123",
  "time": "2026-12-01T11:20:00Z",
  "data": {
    "ticketId": "tkt_123",
    "performanceId": "prf_123",
    "gateId": "gate_3"
  }
}
```

投递规则：

- HMAC 签名覆盖时间戳与原始 body；
- 重试指数退避，最多 24 小时；
- 事件 ID 用于去重；
- 不保证顺序，消费者按 `occurredAt` 或业务版本处理；
- 订阅可过滤事件类型；
- 提供重放工具与投递日志；
- 合作伙伴端点必须 HTTPS、验证所有权、防 SSRF。

## O.13 认证、授权与多租户

### 调用方与认证

| 调用方 | 认证 | 说明 |
|---|---|---|
| 观众（Web） | BFF 会话 + HttpOnly Cookie | 令牌不暴露给浏览器 |
| 观众（App） | OAuth 2.1 + PKCE | 短期 access token |
| 主办方后台 | OIDC + MFA | 组织成员 |
| 合作伙伴 | OAuth 客户端凭证 + mTLS | 服务间 |
| 检票设备 | 设备证书 + 短期令牌 | 可离线验签 |
| AI Agent | OAuth + RAR + 用户确认 | 代表用户 |

### 授权模型

- 功能级：`events:read`、`orders:write`、`refunds:write`；
- 对象级：主办方只能访问自己租户的活动；
- 字段级：合作伙伴看不到用户邮箱与支付方式；
- 租户级：所有查询强制 `tenant_id` 过滤。

```json
{
  "sub": "user_123",
  "tenant_id": "t_123",
  "scope": "orders:read orders:write",
  "roles": ["organizer_admin"],
  "act": { "sub": "agent_ticketing" },
  "exp": 1795688130
}
```

### 多租户隔离

- 数据库行级安全（RLS）或查询层强制租户条件；
- 缓存键包含租户；
- 对象存储前缀包含租户；
- 搜索索引包含租户过滤；
- 队列消息带租户；
- 限流按租户；
- 日志与追踪带租户；
- 跨租户管理操作单独授权并审计。

不要只依赖“应用层记得加 `WHERE tenant_id = ?`”。一次遗漏就是跨租户泄露。

## O.14 限流、缓存与峰值

### 限流维度

| 端点 | 维度 | 建议 |
|---|---|---|
| 活动搜索 | IP + 用户 | 较宽松，允许缓存 |
| 座位图 | 用户 + 场次 | 中等，防刷 |
| 预占 | 用户 + 设备 + 租户 | 严格，防黄牛 |
| 支付 | 订单 + 用户 | 严格，防重复 |
| 检票 | 设备 + 场馆 | 宽松，离线优先 |
| 合作伙伴 | 客户端 + 租户 | 按合同 |

响应头：

```http
RateLimit-Limit: 60
RateLimit-Remaining: 12
RateLimit-Reset: 30
Retry-After: 30
```

### 缓存

- 活动列表与详情：可缓存 30～60 秒，`ETag` 验证；
- 座位图快照：短缓存或私有不缓存；
- 库存：不缓存，或只缓存“粗略余量”；
- 静态资源（海报、场馆图）：CDN 长缓存；
- 用户订单：私有、`no-store`。

### 峰值

```text
开票前：预热缓存、扩容、预生成座位图快照
开票瞬间：虚拟等待室、令牌桶、排队
售罄后：降级为只读、关闭预占、返回明确售罄状态
```

容量规划要按“峰值到达率 × 每请求成本”算，而不是按日活。压测要覆盖：

- 座位冲突；
- 支付超时；
- Redis 故障；
- 数据库主从延迟；
- 第三方渠道限流。

## O.15 契约、测试与发布

### 契约

- OpenAPI 3.2 作为 HTTP 契约（工具链不支持时回退 3.1）；
- AsyncAPI 描述 Webhook 事件；
- JSON Schema 描述座位图、票令牌、错误；
- 所有错误码进入错误目录；
- 契约变更在 CI 中做破坏性检测。

### 测试矩阵

| 层次 | 覆盖 |
|---|---|
| 单元 | 状态机、价格、权限、签名 |
| 集成 | 数据库约束、Redis 预占、支付适配器 |
| 契约 | 消费者驱动契约、OpenAPI lint |
| 并发 | 同座位竞争、重复支付、重复检票 |
| 属性/模糊 | 票令牌、座位图、查询参数 |
| 故障注入 | 支付超时、Redis 挂、Webhook 重试 |
| 负载 | 开票峰值、座位图流、检票高峰 |
| 安全 | 越权、IDOR、伪造票、SSRF、限流绕过 |

并发测试尤其重要。票务系统的很多 bug 只在“两个请求同时到达”时出现，单线程测试永远抓不到。

### 发布

- 契约先行，SDK 与 mock 同步生成；
- 灰度按租户/场次；
- 特性开关控制新库存策略；
- 回滚预案包含数据回滚与座位释放；
- 破坏性变更走扩展—收缩；
- 弃用提前 6～12 个月通知合作伙伴。

票务系统尤其需要“可重放”的沙箱：开票峰值、座位竞争、支付超时、Webhook 重复投递、检票离线，都必须在沙箱里稳定复现。沙箱的保真度、测试时钟与 Webhook 重放见[附录 AJ](aj-sandbox-mock-test-data.md)；开关类型、金丝雀与 kill switch 见[附录 AK](ak-feature-flags-experiments-progressive-delivery.md)。若库存服务跑在 Kubernetes 上，座位池、场次、限流策略的声明式管理还需要控制平面契约，见[附录 AL](al-infrastructure-as-api-terraform-crd-gitops.md)。

## O.16 可观测性与 SLO

### SLI

| SLI | 定义 |
|---|---|
| 下单成功率 | 成功创建订单 / 有效下单请求 |
| 选座延迟 | 座位图与预占的 P95 |
| 支付成功率 | 渠道成功 / 支付尝试（排除用户取消） |
| 出票延迟 | 支付成功到票可用的 P95 |
| 检票延迟 | 扫码到结果的 P95 |
| 重复售出 | 同一座位产生多个有效订单的次数 |

### SLO

```text
核心售票可用性：99.95%
选座 P95：< 300ms
下单 P95：< 800ms
支付成功率：> 99%（排除用户取消）
出票 P95：< 5s
检票 P95：< 200ms（在线）
```

### 告警

- 重复售出 > 0：P0；
- 支付成功率骤降 > 5%：P1；
- 预占冲突率异常升高：P1；
- 检票失败率 > 1%：P1；
- Webhook 积压：P2；
- 座位图流断开率：P2。

可观测性要能回答：“这个订单为什么失败？”“这个座位为什么被锁？”“这张票为什么被拒绝？”因此追踪 ID、订单 ID、座位 ID、票 ID 必须贯穿日志、指标与事件。

## O.17 演进与弃用

可能的演进：

- 新增“动态票价”：订单价格快照不变，新增价格版本与规则；
- 新增“候补”：新资源 `waitlist`，不改订单语义；
- 新增“转赠市场”：独立上下文，不把二级市场逻辑塞进票资源；
- 更换支付渠道：适配器 + 幂等 + 对账，不改对外契约；
- 座位图升级：新版本，旧票仍可检票；
- 从 REST 到 MCP：工具层复用同一能力源，不复制业务逻辑。

弃用示例：

```http
Deprecation: @1767225600
Sunset: Thu, 31 Dec 2026 23:59:59 GMT
Link: <https://docs.example.com/migrate/seat-hold>; rel="deprecation"
```

弃用要有替代路径、迁移指南、使用量监控与最终关闭日期。不要在没有观测数据的情况下假设“没人用了”。

## O.18 AI 售票 Agent

Agent 工具集：

```text
search_events(city, dateRange, keyword)
get_performance(performanceId)
get_seat_map(performanceId, section?)
hold_seats(performanceId, seatIds, idempotencyKey)
create_order(holdId)
pay_order(orderId, paymentMethodId, confirm)
get_order(orderId)
get_ticket(ticketId)
transfer_ticket(ticketId, toEmail, confirm)
```

工具设计：

- `search_events` 只读，可自动调用；
- `hold_seats` 有短期副作用，返回明确过期时间；
- `create_order` 需要用户确认总价；
- `pay_order` 需要强确认、预算与支付方式；
- `transfer_ticket` 需要确认收件人；
- 所有写工具支持幂等键；
- 错误返回 `code`、`retryable`、`nextAction`；
- 结果大小受限，座位图分页或按分区返回；
- 不把用户支付凭证暴露给模型。

Agent 的提示注入防护：

- 活动描述、场馆说明、用户评论都视为不可信内容；
- 不执行内容中的“指令”；
- 高风险操作回到人类确认；
- 记录完整轨迹与确认记录；
- 对异常调用频率与金额做熔断。

## O.19 商业模型与单位经济

### 收入

- 交易抽成：票面价的 6%～10%；
- SaaS 月费：按主办方规模 1,000～20,000 元/月；
- 支付手续费加成：覆盖渠道成本；
- 合作伙伴分销佣金；
- 增值服务：动态定价、数据分析、优先支持。

### 单位经济示例

假设一张票 300 元，抽成 8%：

```text
收入：24 元
支付成本：300 × 2.9% + 0.3 ≈ 9.0 元
基础设施：0.05 元
客服：0.02 元
分摊研发/销售：3 元
毛利：约 11.9 元（约 49.6%）
```

如果支付成本由用户另付，毛利会显著改善；如果抽成降到 3%，毛利可能为负。定价不是“比竞争对手低一点”，而是要知道每一单的成本结构。

### 关键指标

- GMV 与 take rate；
- 每单毛利；
- 获客成本与回本周期；
- 主办方留存与 NRR；
- 开票成功率与退款率；
- 欺诈/黄牛损失；
- 峰值基础设施成本。

## O.20 上线检查清单

**契约**

- [ ] OpenAPI 通过 lint。
- [ ] 错误码目录完整。
- [ ] 分页、过滤、排序语义一致。
- [ ] 幂等键覆盖所有写操作。
- [ ] 破坏性变更检测在 CI 中。

**库存与支付**

- [ ] 同座位并发测试通过。
- [ ] 预占过期与释放可靠。
- [ ] 支付超时后查询兜底。
- [ ] 重复支付/重复退款被阻止。
- [ ] 账本与对账可运行。

**安全**

- [ ] 对象级授权测试覆盖。
- [ ] 租户隔离测试覆盖。
- [ ] 票令牌签名与轮换。
- [ ] 检票防重放与离线对账。
- [ ] 合作伙伴 API 限流与审计。

**运行**

- [ ] SLO 与告警已配置。
- [ ] 峰值压测通过。
- [ ] 降级与排队演练。
- [ ] Webhook 重试与死信。
- [ ] 回滚与数据修复预案。
- [ ] 沙箱可复现开票峰值、座位竞争、支付超时与 Webhook 重复投递。
- [ ] 新库存策略通过特性开关按场次灰度，kill switch 已演练。
- [ ] 库存/限流等控制平面资源的字段所有权与漂移处理明确。

**商业**

- [ ] 定价与账单可解释。
- [ ] 退款政策与合同一致。
- [ ] 单位经济为正或有明确路径。
- [ ] 合作伙伴结算可对账。

## O.21 小结

这个案例没有引入任何“新原理”。它只是把书中的判断放进同一个系统：

- 领域边界决定了资源与状态机；
- 风格选型决定了哪些能力用 REST、SSE、Webhook 或 MCP；
- 契约决定了错误、分页、幂等与版本；
- 安全决定了认证、授权、租户隔离与票令牌；
- 工程决定了测试、可观测性、容量与发布；
- 商业决定了定价、成本与指标。

如果只能带走一句话：**先把不可逆的决策（标识、库存语义、支付与票的信任边界）做对，再优化其余部分。** 票务系统可以在 UI 上快速迭代，但一个重复售出的座位、一个可伪造的票、一笔重复的退款，会成为长期的信任负债。

---

返回：[目录](../SUMMARY.md)
