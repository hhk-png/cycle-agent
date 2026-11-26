# 第 39 章 电商与交易 API

## 39.1 电商 API 的核心矛盾

电商系统同时追求：

- 商品丰富与实时；
- 库存准确；
- 价格正确；
- 下单流畅；
- 支付安全；
- 履约可靠；
- 促销复杂；
- 高并发；
- 可对账。

这些目标互相冲突。例如：库存要准确，但高并发下强一致会降低吞吐；促销要灵活，但规则组合会爆炸；订单要可追溯，但状态会变得复杂。

### 核心矛盾

| 矛盾 | 一方 | 另一方 | 常见解法 |
|---|---|---|---|
| 库存准确 vs 吞吐 | 强一致扣减 | 高并发下单 | 预占 + 最终一致 + 对账 |
| 价格灵活 vs 可解释 | 复杂促销 | 用户信任 | 价格明细 + 规则优先级 |
| 订单完整 vs 状态复杂 | 全生命周期 | 可维护性 | 多状态维度 + 状态机 |
| 履约灵活 vs 一致性 | 拆包/拆单 | 库存/账务 | 独立履约聚合 + 事件 |
| 高并发 vs 成本 | 峰值容量 | 闲时浪费 | 弹性 + 队列 + 降级 |

## 39.2 领域模型

```text
Catalog
├── Product
├── SKU
├── Category
└── Price

Inventory
├── Warehouse
├── StockItem
└── Reservation

Cart
├── Cart
└── CartItem

Order
├── Order
├── OrderItem
├── Address
├── Promotion
└── OrderEvent

Payment
├── PaymentIntent
├── Transaction
└── Refund

Fulfillment
├── Shipment
├── Package
└── Tracking
```

### 关键概念

- **Product vs SKU**：Product 是展示概念，SKU 是可售/可库存单位。
- **Price**：价格可能因渠道、会员、时间、促销而异。
- **Inventory**：库存按仓库/渠道/区域分布。
- **Cart**：临时聚合，可能跨设备。
- **Order**：交易契约，包含价格与商品快照。
- **Payment**：独立聚合，一个订单可能多次支付。
- **Fulfillment**：履约，可能拆包、拆单。
- **Return/Refund**：逆向流程，独立聚合。

### 聚合边界

| 聚合 | 不变量 | 事务边界 |
|---|---|---|
| Product/SKU | 商品信息、上下架 | 单商品 |
| Inventory | 可用量不为负 | 单 SKU/仓库 |
| Cart | 价格可重算 | 单购物车 |
| Order | 金额快照、状态机 | 单订单 |
| Payment | 支付金额、状态 | 单支付 |
| Fulfillment | 包裹与物流 | 单履约 |

跨聚合通过事件和补偿，不要试图一个大事务。

## 39.3 商品与目录 API

```text
GET  /products?category=...&q=...&cursor=...
GET  /products/{productId}
GET  /skus/{skuId}
GET  /skus/{skuId}/availability?warehouseId=...
```

设计要点：

- 商品详情可缓存；
- 价格/库存实时性要求不同；
- 多语言；
- 多币种；
- 图片/视频；
- 搜索与推荐分离；
- 下架/归档语义；
- 渠道可见性；
- SEO slug。

### 商品状态

```text
DRAFT → ACTIVE → INACTIVE → ARCHIVED
```

- `DRAFT`：未发布；
- `ACTIVE`：可售；
- `INACTIVE`：下架，不可售但保留；
- `ARCHIVED`：归档，不再展示。

### 价格模型

价格不应只有一个字段：

```json
{
  "skuId": "sku_1",
  "currency": "CNY",
  "listPrice": 13900,
  "salePrice": 12900,
  "memberPrice": 12400,
  "taxIncluded": true,
  "priceVersion": "2026-11-01"
}
```

价格可能因：

- 渠道；
- 会员等级；
- 促销；
- 地区；
- 时间；
- 数量。

价格计算应有独立服务，并返回明细。

## 39.4 库存

### 库存模型

```text
onHand      实际在库
reserved    已预占
available   onHand - reserved
incoming    在途
```

### 预占

下单时预占库存：

```text
POST /inventory/reservations
{
  "orderId": "ord_123",
  "items": [
    { "skuId": "sku_1", "warehouseId": "wh_1", "quantity": 2 }
  ],
  "expiresAt": "2026-11-26T10:15:00Z"
}
```

响应：

```json
{
  "reservationId": "res_123",
  "status": "RESERVED",
  "expiresAt": "..."
}
```

### 防超卖

- 数据库条件更新：`UPDATE stock SET reserved = reserved + ? WHERE available >= ?`；
- 行锁；
- 分布式锁（谨慎）；
- 预占 + 过期释放；
- 最终一致 + 对账；
- 库存分段/分桶；
- 热点商品独立处理。

### 幂等

预占必须幂等：

```http
Idempotency-Key: ...
```

否则重试会导致重复预占。

### 预占生命周期

```text
RESERVED → CONFIRMED
    │
    ├→ RELEASED
    └→ EXPIRED
```

- 下单后预占；
- 支付成功后确认；
- 取消/超时释放；
- 过期自动释放；
- 确认后扣减。

### 库存一致性

- 预占是“软锁”；
- 实际扣减在支付后；
- 预占超时释放；
- 释放可能失败，需要重试；
- 对账修正。

## 39.5 购物车

购物车是临时聚合：

```text
POST /carts
GET  /carts/{cartId}
POST /carts/{cartId}/items
PATCH /carts/{cartId}/items/{itemId}
DELETE /carts/{cartId}/items/{itemId}
POST /carts/{cartId}/checkout
```

设计要点：

- 价格实时计算 vs 快照；
- 库存校验；
- 促销应用；
- 多设备同步；
- 过期清理；
- 合并（登录后游客车合并）；
- 商品下架处理；
- 数量限制。

### 购物车价格

购物车可以：

- 实时计算（价格最新）；
- 或缓存价格（性能好）；
- 结算时重新计算。

无论哪种，结算时必须重新校验价格和库存。

### 游客车合并

```text
游客车 + 用户车 → 合并
```

需要处理：

- 相同 SKU 合并数量；
- 冲突价格；
- 过期商品；
- 上限。

## 39.6 订单

### 创建

```http
POST /orders
Idempotency-Key: ...
```

```json
{
  "cartId": "cart_123",
  "shippingAddressId": "addr_456",
  "paymentMethodId": "pm_789",
  "couponCode": "SAVE10"
}
```

响应：

```http
HTTP/1.1 201 Created
Location: /orders/ord_123
```

```json
{
  "id": "ord_123",
  "status": "PENDING_PAYMENT",
  "total": { "amount": 12900, "currency": "CNY" },
  "expiresAt": "2026-11-26T10:15:00Z"
}
```

### 订单状态机

```text
PENDING_PAYMENT → PAID → FULFILLING → SHIPPED → DELIVERED → COMPLETED
       │            │          │
       │            │          └→ CANCELLED
       │            └→ REFUNDING → REFUNDED
       └→ CANCELLED
```

### 价格快照

订单必须保存：

- 商品 SKU；
- 数量；
- 单价；
- 折扣；
- 税费；
- 运费；
- 币种；
- 促销来源；
- 价格版本。

不要在下单后重新计算历史订单价格。

### 幂等与重复下单

- 幂等键；
- 购物车状态；
- 客户端按钮防抖；
- 订单去重（相同用户/商品/时间窗口）；
- 返回已有订单。

### 订单查询

```text
GET /orders/{orderId}
GET /orders?status=...&from=...&to=...&cursor=...
GET /orders/{orderId}/events
```

订单列表需要：

- 游标分页；
- 权限过滤；
- 状态过滤；
- 时间范围；
- 不返回过大聚合。

## 39.7 支付

见第 40 章。电商侧要点：

- 支付与订单分离；
- 支付成功回调可能重复；
- 订单状态由支付事件驱动；
- 超时未支付自动取消；
- 退款独立；
- 部分支付/多次支付；
- 支付失败重试。

## 39.8 促销

促销规则可能包括：

- 满减；
- 折扣；
- 买赠；
- 优惠券；
- 会员价；
- 限时秒杀；
- 跨店满减；
- 叠加/互斥；
- 新人价；
- 阶梯价。

设计促销不能只列“优先级”和“最优组合”。至少要有一个规则模型与解析流程。

**规则模型**：

```json
{
  "ruleId": "promo_123",
  "type": "coupon",
  "scope": { "items": ["sku_1", "sku_2"], "categories": ["electronics"] },
  "conditions": {
    "minSubtotal": 10000,
    "memberTier": ["gold", "platinum"],
    "firstOrderOnly": false,
    "regions": ["CN"]
  },
  "benefit": {
    "kind": "fixed_amount",
    "amount": 1000,
    "currency": "CNY",
    "maxDiscount": 1000
  },
  "priority": 100,
  "stackable": true,
  "exclusivityGroup": "coupon",
  "budget": { "total": 100000, "used": 12000, "perUser": 1 },
  "validFrom": "2026-11-01T00:00:00Z",
  "validTo": "2026-11-30T23:59:59Z"
}
```

**解析流程**：

```text
1. 过滤：时间、地区、会员、门槛、库存、预算、每人限次
2. 排序：优先级、折扣金额、即将过期、平台补贴 vs 商家补贴
3. 组合：按 stackable / exclusivityGroup 生成候选组合
4. 择优：在允许的规则下选择对客户最优或对平台成本最低的组合
5. 分摊：把总折扣分摊到行项目，支持部分退货
6. 解释：返回每个规则的命中/未命中原因
```

**可解释的输出**：

```json
{
  "subtotal": 13900,
  "currency": "CNY",
  "discounts": [
    { "ruleId": "promo_123", "type": "coupon", "amount": 1000 },
    { "ruleId": "member_gold", "type": "member", "amount": 500 }
  ],
  "total": 12400,
  "unavailable": [
    {
      "ruleId": "promo_456",
      "reason": "MIN_SUBTOTAL_NOT_MET",
      "detail": "还差 1100 元可使用满 15000 减 2000"
    },
    {
      "ruleId": "promo_789",
      "reason": "EXCLUSIVITY_CONFLICT",
      "detail": "与 promo_123 属于同一互斥组"
    }
  ]
}
```

必须明确：

- 折扣按行分摊还是按订单分摊；
- 部分退货时如何回滚折扣；
- 平台补贴与商家补贴如何记账；
- 优惠券锁定与释放；
- 预算扣减的原子性；
- 规则变更是否影响已创建订单（通常按快照）。

没有这些，促销会成为财务对账与客服争议的黑洞。

其他要求：

- 促销计算服务独立于订单服务；
- 规则版本化，订单保存价格快照；
- 幂等；
- 防刷单；
- 预算控制与告警。

API 应返回价格明细：

```json
{
  "subtotal": 13900,
  "currency": "CNY",
  "discounts": [
    { "type": "coupon", "code": "SAVE10", "amount": 1000 },
    { "type": "member", "amount": 500 }
  ],
  "shipping": 0,
  "tax": 0,
  "total": 12400,
  "currency": "CNY"
}
```

### 促销的可解释性

用户需要知道：

- 为什么便宜了；
- 哪个优惠生效；
- 为什么某个优惠不可用；
- 还差多少能用。

```json
{
  "unavailablePromotions": [
    {
      "code": "SAVE20",
      "reason": "MIN_SPEND_NOT_MET",
      "remaining": 2000
    }
  ]
}
```

### 防刷

- 每用户限购；
- 设备指纹；
- 风控；
- 幂等；
- 优惠券唯一；
- 库存预占；
- 黑名单。

## 39.9 履约与退货

### 履约

```text
POST /orders/{orderId}/fulfillments
GET  /fulfillments/{id}
POST /fulfillments/{id}/ship
POST /fulfillments/{id}/deliver
```

支持：

- 拆包；
- 部分发货；
- 物流跟踪；
- 签收；
- 异常；
- 换货。

### 退货

```text
POST /returns
GET  /returns/{id}
POST /returns/{id}/approve
POST /returns/{id}/receive
POST /returns/{id}/refund
```

需要：

- 退货原因；
- 商品状态；
- 退款金额；
- 库存回补；
- 质检；
- 逆向物流；
- 退货期限；
- 部分退货。

### 退货状态机

```text
REQUESTED → APPROVED → IN_TRANSIT → RECEIVED → REFUNDED
    │           │            │
    ├→ REJECTED ├→ CANCELLED├→ CANCELLED
    └→ CANCELLED
```

注意：

- `REQUESTED` 可以直接取消或拒绝；
- `APPROVED` 后、发货前可以取消；
- `IN_TRANSIT` 后取消通常需要拦截物流或转为拒收；
- `RECEIVED` 后进入质检与退款；
- 部分退货/部分退款需要行项目级状态，而不是一个订单级状态。

## 39.10 事件

```text
OrderCreated
OrderPaid
OrderCancelled
OrderShipped
OrderDelivered
RefundRequested
RefundCompleted
InventoryReserved
InventoryReleased
```

事件用于：

- 通知；
- 履约；
- 积分；
- 推荐；
- 分析；
- 对账；
- 风控。

## 39.11 高并发

- 库存预占；
- 队列削峰；
- 缓存；
- 限流；
- 降级；
- 热点商品隔离；
- 数据库分片；
- 最终一致；
- 对账。

### 秒杀

- 独立集群；
- 预扣库存；
- 令牌；
- 队列；
- 限购；
- 防刷；
- 快速失败；
- 页面静态化；
- 异步下单。

### 大促

- 容量规划；
- 压测；
- 限流；
- 降级；
- 缓存预热；
- 数据库只读副本；
- 消息队列削峰；
- 预案。

## 39.12 对账

- 订单 vs 支付；
- 支付 vs 渠道；
- 库存 vs 实际；
- 退款 vs 账务；
- 优惠 vs 预算；
- 履约 vs 物流。

对账是电商 API 的“最后一道防线”。

## 39.13 本章小结

- 电商 API 的核心是库存、价格、订单、支付、履约的协同。
- 库存预占与幂等是防超卖的关键。
- 订单必须保存价格与商品快照。
- 状态机要明确合法迁移与补偿。
- 促销要可解释、可叠加、可防刷。
- 履约与退货是独立聚合。
- 高并发需要队列、缓存、限流、隔离和对账。
- 多币种、关税、金额/汇率/舍入与账期语义，见[附录 AH](../appendices/ah-primitive-semantics.md)。

## 设计练习

为“跨境电商平台”设计 API：

- 多币种；
- 多仓库；
- 关税；
- 支付；
- 物流；
- 退货；
- 促销；
- 对账。

画出订单状态机与关键事件。然后回答：

1. 多仓库库存如何分配？
2. 关税如何计算与展示？
3. 部分退货如何退款？
4. 如何防止超卖和重复下单？

---

上一章：[发布、弃用与迁移](../part-4-engineering-product/38-release-deprecation.md)  
下一章：[支付与金融 API](40-payments.md)
