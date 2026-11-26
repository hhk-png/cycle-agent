# 附录 C：错误目录模板

## C.1 错误对象模板

```json
{
  "type": "https://errors.example.com/{domain}/{error-slug}",
  "title": "简短的人类可读标题",
  "status": 422,
  "detail": "针对本次请求的具体说明",
  "instance": "/orders",
  "code": "STABLE_MACHINE_CODE",
  "requestId": "req_01J9Z6...",
  "retryable": false,
  "retryAfterSeconds": null,
  "errors": [
    {
      "field": "items[0].quantity",
      "code": "OUT_OF_RANGE",
      "message": "数量必须在 1 到 99 之间",
      "meta": { "min": 1, "max": 99, "actual": 0 }
    }
  ],
  "docsUrl": "https://docs.example.com/errors/STABLE_MACHINE_CODE"
}
```

### 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | 是 | 稳定错误类型 URI |
| `title` | 是 | 简短摘要，可本地化 |
| `status` | 是 | HTTP 状态码 |
| `detail` | 是 | 本次请求的具体说明，不用于程序判断 |
| `instance` | 否 | 资源路径或请求 ID |
| `code` | 是 | 稳定机器码 |
| `requestId` | 是 | 追踪 ID |
| `retryable` | 否 | 是否可重试 |
| `retryAfterSeconds` | 否 | 建议重试间隔 |
| `errors` | 否 | 字段级错误数组 |
| `docsUrl` | 否 | 错误文档链接 |

## C.2 命名规范

```text
{领域}_{原因}
```

示例：

```text
AUTH_TOKEN_EXPIRED
AUTH_INSUFFICIENT_SCOPE
ORDER_NOT_FOUND
ORDER_INVALID_STATE
PAYMENT_CARD_DECLINED
PAYMENT_DUPLICATE_REQUEST
RATE_LIMITED
VALIDATION_FAILED
DEPENDENCY_UNAVAILABLE
INTERNAL_ERROR
```

规则：

- 大写下划线；
- 稳定不复用；
- 不含动态信息；
- 不暴露内部实现；
- 有文档；
- 与 HTTP 状态码解耦。

通用错误码是保留集合，可以不带领域前缀：

```text
UNAUTHENTICATED
FORBIDDEN
NOT_FOUND
CONFLICT
VALIDATION_FAILED
RATE_LIMITED
DEPENDENCY_UNAVAILABLE
UPSTREAM_TIMEOUT
INTERNAL_ERROR
```

除此之外，业务错误应使用 `{领域}_{原因}`，例如 `ORDER_INVALID_STATE`、`PAYMENT_CARD_DECLINED`。不要一边要求领域前缀，一边在目录里混用无前缀的业务码。

## C.3 错误目录表

| code | HTTP | 可重试 | 含义 | 调用方动作 |
|---|---|---|---|---|
| AUTH_MISSING | 401 | 否 | 缺少凭证 | 添加认证 |
| AUTH_INVALID | 401 | 否 | 凭证无效 | 重新认证 |
| UNAUTHENTICATED | 401 | 否 | 未认证 | 添加/刷新凭证 |
| AUTH_TOKEN_EXPIRED | 401 | 否 | 令牌过期 | 刷新令牌 |
| AUTH_INSUFFICIENT_SCOPE | 403 | 否 | scope 不足 | 申请权限 |
| FORBIDDEN | 403 | 否 | 无权限 | 检查权限 |
| NOT_FOUND | 404 | 否 | 资源不存在 | 检查 ID |
| METHOD_NOT_ALLOWED | 405 | 否 | 方法不允许 | 查看 `Allow` |
| VALIDATION_FAILED | 400/422 | 否 | 参数校验失败 | 修改请求 |
| CONFLICT | 409 | 否 | 状态冲突 | 重新读取 |
| PRECONDITION_FAILED | 412 | 否 | 版本不匹配 | 重新读取 |
| IDEMPOTENCY_KEY_REUSED | 422 | 否 | 幂等键用于不同请求 | 使用新键 |
| RATE_LIMITED | 429 | 是 | 限流 | 退避重试 |
| QUOTA_EXCEEDED | 402/429 | 否 | 配额用尽 | 升级/等待 |
| DEPENDENCY_UNAVAILABLE | 503 | 是 | 依赖不可用 | 退避重试 |
| UPSTREAM_TIMEOUT | 504 | 是 | 上游超时 | 查询状态/重试 |
| INTERNAL_ERROR | 500 | 视情况 | 未预期错误 | 联系支持 |

## C.4 校验错误

```json
{
  "type": "https://errors.example.com/validation",
  "title": "请求参数校验失败",
  "status": 422,
  "code": "VALIDATION_FAILED",
  "errors": [
    {
      "field": "email",
      "code": "INVALID_FORMAT",
      "message": "邮箱格式不正确"
    },
    {
      "field": "items[2].quantity",
      "code": "OUT_OF_RANGE",
      "message": "数量必须在 1 到 99 之间",
      "meta": { "min": 1, "max": 99, "actual": 0 }
    }
  ]
}
```

要求：

- 字段路径与请求 JSON 一致；
- 不泄露敏感值；
- 一次返回多个错误（除非安全/性能限制）；
- 错误顺序稳定；
- 可本地化 `message`，但 `code` 不变。

## C.5 错误生命周期

1. 设计时定义；
2. 契约中声明；
3. 文档中解释；
4. 实现中映射；
5. 测试中覆盖；
6. 监控中统计；
7. 弃用时不删除，标记；
8. 保留历史映射。

## C.6 错误文档模板

```markdown
## ORDER_INVALID_STATE

HTTP: 409 Conflict

订单当前状态不允许执行该操作。

### 触发条件
- 订单已取消，尝试支付
- 订单已支付，尝试再次支付
- 订单已发货，尝试取消

### 响应示例
...

### 调用方处理
1. 调用 GET /orders/{id} 获取最新状态
2. 根据 status 选择合法操作
3. 不要盲目重试

### 相关
- 订单状态机
- 幂等键
```

## C.7 反模式

- 只返回 `message`；
- 错误码随消息变化；
- 同一语义多个错误码；
- 堆栈/SQL 泄露；
- 401/403 混用；
- 所有错误 500；
- 不提供 `requestId`；
- 错误文档缺失；
- 本地化文本用于程序判断。

---

返回：[目录](../SUMMARY.md)
