# 附录 D：OpenAPI 3.2 模板（兼容 3.1）

> 这是一个偏完整的订单 API 骨架。实际使用时按需裁剪，但不要删掉错误、认证、分页和幂等部分。模板以 OpenAPI 3.2 为目标；如果你的工具链仍停留在 3.1，把版本号改回 `3.1.0` 即可，核心结构不变。

```yaml
openapi: 3.2.0

info:
  title: Orders API
  version: 1.0.0
  description: |
    订单创建、查询、取消与退款 API。

    ## 约定
    - 所有金额使用最小货币单位（分）。
    - 所有时间使用 RFC 3339，带时区偏移。
    - 写操作支持 `Idempotency-Key`。
    - 错误使用 RFC 9457 Problem Details。

servers:
  - url: https://api.example.com/v1
    description: Production
  - url: https://sandbox.example.com/v1
    description: Sandbox

security:
  - bearerAuth: []

tags:
  - name: Orders
    description: 订单生命周期
  - name: Payments
    description: 支付
  - name: Operations
    description: 异步操作

paths:
  /orders:
    get:
      operationId: listOrders
      summary: 列出订单
      tags: [Orders]
      parameters:
        - $ref: "#/components/parameters/Cursor"
        - $ref: "#/components/parameters/Limit"
        - name: status
          in: query
          description: 按订单状态过滤
          schema:
            type: array
            items:
              $ref: "#/components/schemas/OrderStatus"
          style: form
          explode: true
        - name: createdAtGte
          in: query
          schema:
            type: string
            format: date-time
        - name: createdAtLt
          in: query
          schema:
            type: string
            format: date-time
      responses:
        "200":
          description: 订单列表
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/OrderList"
        "400":
          $ref: "#/components/responses/BadRequest"
        "401":
          $ref: "#/components/responses/Unauthorized"
        "403":
          $ref: "#/components/responses/Forbidden"
        "429":
          $ref: "#/components/responses/TooManyRequests"
        "500":
          $ref: "#/components/responses/InternalError"

    post:
      operationId: createOrder
      summary: 创建订单
      tags: [Orders]
      parameters:
        - $ref: "#/components/parameters/IdempotencyKey"
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/CreateOrderRequest"
            examples:
              basic:
                summary: 创建一个订单
                value:
                  customerId: cus_123
                  items:
                    - skuId: sku_1
                      quantity: 2
                  shippingAddressId: addr_456
      responses:
        "201":
          description: 订单已创建
          headers:
            Location:
              description: 新订单 URL
              schema:
                type: string
                format: uri
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Order"
        "400":
          $ref: "#/components/responses/BadRequest"
        "401":
          $ref: "#/components/responses/Unauthorized"
        "403":
          $ref: "#/components/responses/Forbidden"
        "409":
          $ref: "#/components/responses/Conflict"
        "422":
          $ref: "#/components/responses/UnprocessableEntity"
        "429":
          $ref: "#/components/responses/TooManyRequests"
        "500":
          $ref: "#/components/responses/InternalError"

  /orders/{orderId}:
    get:
      operationId: getOrder
      summary: 获取订单
      tags: [Orders]
      parameters:
        - $ref: "#/components/parameters/OrderId"
      responses:
        "200":
          description: 订单
          headers:
            ETag:
              schema:
                type: string
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Order"
        "401":
          $ref: "#/components/responses/Unauthorized"
        "403":
          $ref: "#/components/responses/Forbidden"
        "404":
          $ref: "#/components/responses/NotFound"
        "429":
          $ref: "#/components/responses/TooManyRequests"

    patch:
      operationId: updateOrder
      summary: 更新订单
      tags: [Orders]
      parameters:
        - $ref: "#/components/parameters/OrderId"
        - $ref: "#/components/parameters/IfMatch"
      requestBody:
        required: true
        content:
          application/merge-patch+json:
            schema:
              $ref: "#/components/schemas/UpdateOrderRequest"
      responses:
        "200":
          description: 更新后的订单
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Order"
        "400":
          $ref: "#/components/responses/BadRequest"
        "401":
          $ref: "#/components/responses/Unauthorized"
        "403":
          $ref: "#/components/responses/Forbidden"
        "404":
          $ref: "#/components/responses/NotFound"
        "409":
          $ref: "#/components/responses/Conflict"
        "412":
          $ref: "#/components/responses/PreconditionFailed"
        "422":
          $ref: "#/components/responses/UnprocessableEntity"

  /orders/{orderId}/cancel:
    post:
      operationId: cancelOrder
      summary: 取消订单
      tags: [Orders]
      parameters:
        - $ref: "#/components/parameters/OrderId"
        - $ref: "#/components/parameters/IdempotencyKey"
      requestBody:
        required: false
        content:
          application/json:
            schema:
              type: object
              properties:
                reason:
                  type: string
                  maxLength: 500
      responses:
        "200":
          description: 已取消
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Order"
        "409":
          $ref: "#/components/responses/Conflict"
        "422":
          $ref: "#/components/responses/UnprocessableEntity"

  /orders/{orderId}/refunds:
    post:
      operationId: createRefund
      summary: 创建退款
      tags: [Payments]
      parameters:
        - $ref: "#/components/parameters/OrderId"
        - $ref: "#/components/parameters/IdempotencyKey"
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/CreateRefundRequest"
      responses:
        "202":
          description: 退款已受理
          headers:
            Location:
              schema:
                type: string
                format: uri
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Operation"
        "409":
          $ref: "#/components/responses/Conflict"
        "422":
          $ref: "#/components/responses/UnprocessableEntity"

  /operations/{operationId}:
    get:
      operationId: getOperation
      summary: 查询异步操作
      tags: [Operations]
      parameters:
        - name: operationId
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: 操作状态
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Operation"
        "404":
          $ref: "#/components/responses/NotFound"

components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT
      description: OAuth 2.1 access token

  parameters:
    OrderId:
      name: orderId
      in: path
      required: true
      description: 不透明订单 ID
      schema:
        type: string
        pattern: "^ord_[A-Za-z0-9]+$"
    Cursor:
      name: cursor
      in: query
      description: 不透明分页游标
      schema:
        type: string
    Limit:
      name: limit
      in: query
      description: 每页数量，最大 100
      schema:
        type: integer
        minimum: 1
        maximum: 100
        default: 20
    IdempotencyKey:
      name: Idempotency-Key
      in: header
      required: true
      description: |
        幂等键。相同键与相同请求体在 24 小时内只执行一次。
        相同键与不同请求体返回 422。
      schema:
        type: string
        minLength: 16
        maxLength: 128
    IfMatch:
      name: If-Match
      in: header
      required: true
      description: 乐观并发版本，来自 GET 的 ETag
      schema:
        type: string

  responses:
    BadRequest:
      description: 请求格式或参数错误
      content:
        application/problem+json:
          schema:
            $ref: "#/components/schemas/Problem"
          example:
            type: https://errors.example.com/validation
            title: 请求参数校验失败
            status: 400
            code: VALIDATION_FAILED
            requestId: req_123
    Unauthorized:
      description: 未认证或凭证无效
      headers:
        WWW-Authenticate:
          description: 认证挑战，例如 `Bearer realm="orders", error="invalid_token"`
          schema:
            type: string
      content:
        application/problem+json:
          schema:
            $ref: "#/components/schemas/Problem"
    Forbidden:
      description: 无权限
      content:
        application/problem+json:
          schema:
            $ref: "#/components/schemas/Problem"
    NotFound:
      description: 资源不存在
      content:
        application/problem+json:
          schema:
            $ref: "#/components/schemas/Problem"
    Conflict:
      description: 状态冲突
      content:
        application/problem+json:
          schema:
            $ref: "#/components/schemas/Problem"
    PreconditionFailed:
      description: 版本不匹配
      content:
        application/problem+json:
          schema:
            $ref: "#/components/schemas/Problem"
    UnprocessableEntity:
      description: 语义校验失败
      content:
        application/problem+json:
          schema:
            $ref: "#/components/schemas/Problem"
    TooManyRequests:
      description: 限流
      headers:
        Retry-After:
          schema:
            type: integer
      content:
        application/problem+json:
          schema:
            $ref: "#/components/schemas/Problem"
    InternalError:
      description: 内部错误
      content:
        application/problem+json:
          schema:
            $ref: "#/components/schemas/Problem"

  schemas:
    Money:
      type: object
      required: [amount, currency]
      additionalProperties: false
      properties:
        amount:
          type: integer
          format: int64
          description: 最小货币单位（如分）
          minimum: 0
        currency:
          type: string
          pattern: "^[A-Z]{3}$"
          description: ISO 4217 币种

    OrderStatus:
      type: string
      description: |
        订单状态。服务端可能新增状态值；客户端必须容忍未知值，
        将其原样展示或归入 UNKNOWN，不得因未知值崩溃。
      enum:
        - PENDING_PAYMENT
        - PAID
        - FULFILLING
        - SHIPPED
        - DELIVERED
        - CANCELLED
        - REFUNDING
        - REFUNDED
      x-extensible-enum:
        - PENDING_PAYMENT
        - PAID
        - FULFILLING
        - SHIPPED
        - DELIVERED
        - CANCELLED
        - REFUNDING
        - REFUNDED

    OrderItem:
      type: object
      required: [skuId, quantity, unitPrice, total]
      additionalProperties: false
      properties:
        skuId:
          type: string
        quantity:
          type: integer
          minimum: 1
        unitPrice:
          $ref: "#/components/schemas/Money"
        total:
          $ref: "#/components/schemas/Money"

    Order:
      type: object
      required: [id, status, items, total, createdAt, version]
      additionalProperties: false
      properties:
        id:
          type: string
        customerId:
          type: string
        status:
          $ref: "#/components/schemas/OrderStatus"
        items:
          type: array
          items:
            $ref: "#/components/schemas/OrderItem"
        total:
          $ref: "#/components/schemas/Money"
        createdAt:
          type: string
          format: date-time
        version:
          type: integer

    OrderList:
      type: object
      required: [data, pagination]
      properties:
        data:
          type: array
          items:
            $ref: "#/components/schemas/Order"
        pagination:
          $ref: "#/components/schemas/Pagination"

    Pagination:
      type: object
      required: [hasMore, limit]
      properties:
        nextCursor:
          type: ["string", "null"]
        hasMore:
          type: boolean
        limit:
          type: integer

    CreateOrderRequest:
      type: object
      required: [customerId, items, shippingAddressId]
      additionalProperties: false
      properties:
        customerId:
          type: string
        items:
          type: array
          minItems: 1
          maxItems: 100
          items:
            type: object
            required: [skuId, quantity]
            additionalProperties: false
            properties:
              skuId:
                type: string
              quantity:
                type: integer
                minimum: 1
                maximum: 99
        shippingAddressId:
          type: string
        couponCode:
          type: string

    UpdateOrderRequest:
      type: object
      additionalProperties: false
      properties:
        shippingAddressId:
          type: string
        note:
          type: ["string", "null"]
          maxLength: 1000

    CreateRefundRequest:
      type: object
      required: [amount, reason]
      additionalProperties: false
      properties:
        amount:
          $ref: "#/components/schemas/Money"
        reason:
          type: string
          enum: [customer_request, damaged, duplicate, fraud, other]

    Operation:
      type: object
      required: [id, status, createdAt]
      properties:
        id:
          type: string
        status:
          type: string
          enum: [PENDING, RUNNING, SUCCEEDED, FAILED, CANCELLED]
        progress:
          type: object
          properties:
            percent:
              type: integer
              minimum: 0
              maximum: 100
        result:
          type: object
          additionalProperties: true
        error:
          $ref: "#/components/schemas/Problem"
        createdAt:
          type: string
          format: date-time
        completedAt:
          type: ["string", "null"]
          format: date-time

    Problem:
      type: object
      required: [type, title, status, code, requestId]
      properties:
        type:
          type: string
          format: uri
        title:
          type: string
        status:
          type: integer
        detail:
          type: string
        instance:
          type: string
        code:
          type: string
        requestId:
          type: string
        retryable:
          type: boolean
        retryAfterSeconds:
          type: integer
        errors:
          type: array
          items:
            type: object
            properties:
              field:
                type: string
              code:
                type: string
              message:
                type: string
              meta:
                type: object
                additionalProperties: true
        docsUrl:
          type: string
          format: uri
```

## D.1 OpenAPI 3.2 的关键变化

OpenAPI 3.2 于 2025 年 9 月发布，向后兼容 3.1。与 API 设计最相关的变化：

- **流式媒体类型**：SSE、`application/jsonl`、`multipart/mixed` 等可以用 `itemSchema` 描述每个事件/条目的结构，而不是只写一个笼统的 `string`；
- **QUERY 方法**：可以在 Path Item 中描述 RFC 10008 定义的 `QUERY`；
- **层级标签**：Tag 支持 `summary`、`parent`、`kind`，大型 API 的导航更清晰；
- **`in: querystring` 参数**：描述整个查询字符串的编码方式，适合复杂过滤；
- **设备授权流程**：OAuth 2.0 Device Authorization Flow 可以正式描述；
- **`$self`**：文档可以声明自己的规范 URL；
- **`defaultMapping`**：多态 discriminator 的默认映射更明确；
- **`additionalOperations`**：描述非标准 HTTP 方法，但不要用它绕过契约评审。

采用建议：

1. 新项目直接以 3.2 为目标，但确认 lint、mock、SDK 生成、网关导入都支持；
2. 存量 3.1 文档不必为了“版本新”而迁移；3.2 的收益主要在流式与查询语义；
3. 不要把 3.2 当作“可以随便加方法”的理由；契约的可理解性比规范版本更重要；
4. OpenAPI 4.0（Moonwalk）尚未发布，不要在生产计划中依赖它。

## D.2 使用建议

- `operationId` 稳定，用于代码生成。
- 错误响应必须声明，不要只写 200。
- 枚举要说明未知值处理。
- 金额/时间用明确格式。
- 分页用 `cursor`，不要混用 `offset`。
- 幂等键放在 header，并声明作用域/TTL。
- `additionalProperties: false` 可防 mass assignment，但会收紧兼容性，需权衡。
- 用 Spectral 做 lint。
- 用 `oasdiff` 做破坏性检测。
- 用 Prism 做 mock。
- 契约纳入 CI。

---

返回：[目录](../SUMMARY.md)
