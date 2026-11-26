# 附录 E：AsyncAPI、CloudEvents 与 Webhook 模板

## E.1 AsyncAPI 3.0 示例

> AsyncAPI 3.x 以 3.0 为稳定基线；3.1 线已出现，但工具链支持程度不一。下面的模板以 3.0.0 为安全选择，升级到 3.1 前先验证 lint、代码生成与注册中心。

```yaml
asyncapi: 3.0.0
info:
  title: Order Events
  version: 1.0.0
  description: 订单生命周期事件

servers:
  production:
    host: events.example.com:9092
    protocol: kafka
    description: Production Kafka

channels:
  orderCreated:
    address: orders.created
    messages:
      orderCreated:
        $ref: '#/components/messages/OrderCreated'
  orderPaid:
    address: orders.paid
    messages:
      orderPaid:
        $ref: '#/components/messages/OrderPaid'
  orderCancelled:
    address: orders.cancelled
    messages:
      orderCancelled:
        $ref: '#/components/messages/OrderCancelled'

operations:
  publishOrderCreated:
    action: send
    channel:
      $ref: '#/channels/orderCreated'
  consumeOrderCreated:
    action: receive
    channel:
      $ref: '#/channels/orderCreated'

components:
  messages:
    OrderCreated:
      name: OrderCreated
      title: 订单已创建
      contentType: application/json
      payload:
        $ref: '#/components/schemas/OrderCreatedData'
      examples:
        - payload:
            orderId: ord_123
            customerId: cus_456
            total:
              amount: 12900
              currency: CNY
    OrderPaid:
      name: OrderPaid
      title: 订单已支付
      contentType: application/json
      payload:
        $ref: '#/components/schemas/OrderPaidData'
    OrderCancelled:
      name: OrderCancelled
      title: 订单已取消
      contentType: application/json
      payload:
        $ref: '#/components/schemas/OrderCancelledData'

  schemas:
    Money:
      type: object
      required: [amount, currency]
      properties:
        amount:
          type: integer
          format: int64
        currency:
          type: string

    OrderCreatedData:
      type: object
      required: [orderId, customerId, total]
      properties:
        orderId:
          type: string
        customerId:
          type: string
        total:
          $ref: '#/components/schemas/Money'
        createdAt:
          type: string
          format: date-time

    OrderPaidData:
      type: object
      required: [orderId, paymentId, amount]
      properties:
        orderId:
          type: string
        paymentId:
          type: string
        amount:
          $ref: '#/components/schemas/Money'

    OrderCancelledData:
      type: object
      required: [orderId, reason]
      properties:
        orderId:
          type: string
        reason:
          type: string
```

> 上面的 `contentType: application/json` 表示 payload 是业务数据本身。如果要使用 CloudEvents 结构化模式，应改为 `application/cloudevents+json`，并让 payload 包含完整的 CloudEvents 信封（`specversion`、`id`、`source`、`type`、`time`、`data`），而不是只放 `OrderCreatedData`。

## E.2 CloudEvents 信封

```json
{
  "specversion": "1.0",
  "id": "evt_01J9Z6Q8K3W7V2M4N5P6R7S8T9",
  "source": "https://api.example.com/orders",
  "type": "com.example.order.paid",
  "subject": "ord_123",
  "time": "2026-11-26T10:15:30Z",
  "datacontenttype": "application/json",
  "dataschema": "https://schemas.example.com/order/paid/v1",
  "data": {
    "orderId": "ord_123",
    "paymentId": "pay_456",
    "amount": 12900,
    "currency": "CNY"
  }
}
```

### 字段

| 字段 | 说明 |
|---|---|
| `specversion` | CloudEvents 版本 |
| `id` | 事件唯一 ID |
| `source` | 事件源 |
| `type` | 事件类型 |
| `subject` | 主题/资源 |
| `time` | 发生时间 |
| `datacontenttype` | 数据类型 |
| `dataschema` | schema URL |
| `data` | 业务载荷 |

## E.3 Webhook 订阅 API

```http
POST /webhook-subscriptions
```

```json
{
  "url": "https://partner.example.com/hooks/orders",
  "eventTypes": ["order.created", "order.paid", "order.cancelled"],
  "secret": "whsec_...",
  "description": "生产订单同步",
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

## E.4 Webhook 投递

```http
POST /hooks/orders HTTP/1.1
Host: partner.example.com
Content-Type: application/json
User-Agent: Example-Webhooks/1.0
X-Webhook-Id: evt_01J9Z6...
X-Webhook-Timestamp: 1795688130
X-Webhook-Event: order.paid
X-Webhook-Signature: v1=base64(HMAC-SHA256(secret, timestamp + "." + body))
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

## E.5 签名验证伪代码

```python
import hmac
import hashlib
import base64
import time

def verify(raw_body: bytes, timestamp: str, signature: str, secret: bytes, tolerance=300):
    now = int(time.time())
    if abs(now - int(timestamp)) > tolerance:
        return False

    signed = timestamp.encode() + b"." + raw_body
    expected = base64.b64encode(
        hmac.new(secret, signed, hashlib.sha256).digest()
    ).decode()

    return hmac.compare_digest(expected, signature.removeprefix("v1="))
```

要求：

- 使用原始 body；
- 常量时间比较；
- 时间戳窗口；
- 支持多密钥轮换；
- 事件 ID 去重。

## E.6 重试策略

```text
尝试 1：立即
尝试 2：10 秒
尝试 3：30 秒
尝试 4：2 分钟
尝试 5：10 分钟
尝试 6：1 小时
尝试 7：6 小时
尝试 8：24 小时
```

规则：

- 指数退避 + 抖动；
- 4xx（除 408/429）不重试；
- 5xx/超时重试；
- 尊重 `Retry-After`；
- 记录每次尝试；
- 死信 + 人工重放；
- 最长保留 72 小时（可配置）。

## E.7 接收方处理

```text
1. 读取原始 body
2. 验证签名与时间戳
3. 按 event.id 去重
4. 落库/入队
5. 快速返回 2xx
6. 异步处理
7. 失败重试/告警
```

不要：

- 在 Webhook 处理器里做长时间同步工作；
- 先解析 JSON 再签名；
- 忽略重复；
- 无限重试。

## E.8 事件设计检查清单

- [ ] 事件名是过去式事实。
- [ ] 有唯一 `id`。
- [ ] 有类型、版本、时间、来源。
- [ ] 有租户/组织 ID。
- [ ] 有最小必要载荷。
- [ ] 有 schema。
- [ ] 有兼容性策略。
- [ ] 有顺序/分区键说明。
- [ ] 有重试/死信。
- [ ] 有消费者幂等。
- [ ] 有可观测性。
- [ ] 有重放能力。

---

返回：[目录](../SUMMARY.md)
