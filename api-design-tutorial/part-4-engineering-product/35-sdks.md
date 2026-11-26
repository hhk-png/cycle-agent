# 第 35 章 SDK 设计：把复杂度留在服务端

## 35.1 SDK 是 API 的产品化外壳

开发者不会直接读你的全部文档。他们通常：

1. 找一个快速开始；
2. 安装 SDK；
3. 复制三行代码；
4. 跑通；
5. 再深入。

SDK 决定了这五步是否顺畅。一个好的 SDK：

- 隐藏协议细节；
- 处理认证；
- 自动重试；
- 分页迭代；
- 流式响应；
- 错误映射；
- 类型安全；
- 可观测；
- 与语言习惯一致。

一个差的 SDK 会把服务端的设计缺陷放大：错误信息难懂、重试不安全、分页难用、版本冲突。

### SDK 的定位

SDK 不是“API 的翻译”，而是“API 的产品化外壳”。它应该：

- 让常见任务一行代码完成；
- 让错误可理解；
- 让危险操作显式；
- 让升级可预期。

## 35.2 生成 vs 手写

### 生成 SDK

从 OpenAPI/Proto/GraphQL 生成：

优点：

- 与契约同步；
- 覆盖全；
- 多语言；
- 成本低；
- 减少人为遗漏。

缺点：

- 语言习惯差；
- 错误处理生硬；
- 分页/流式支持弱；
- 需要定制层；
- 生成物可能巨大；
- 文档注释质量参差。

### 手写 SDK

优点：

- 语言惯用；
- 体验好；
- 可封装复杂逻辑；
- 文档/示例更自然；
- 可做高级抽象。

缺点：

- 维护成本高；
- 容易与 API 漂移；
- 多语言负担重；
- 需要持续测试。

### 混合模式

```text
生成核心客户端（低层）
  + 手写惯用封装（高层）
  + 共享重试/分页/认证中间件
```

这是大多数成功 SDK 的做法。

### 分层

```text
高层：client.orders.create(...)
  ↓
中层：分页、重试、错误映射
  ↓
低层：HTTP 请求、序列化
  ↓
生成：类型、请求/响应模型
```

## 35.3 设计原则

### 1. 让正确的事情容易做

```python
client.orders.create(
    customer_id="cus_123",
    items=[{"sku": "sku_1", "quantity": 2}],
    idempotency_key="...",
)
```

而不是要求用户手动拼 JSON、设置头、处理重试。

### 2. 默认安全

- 自动带认证；
- 默认超时；
- 默认重试（仅幂等）；
- 默认 TLS 校验；
- 不记录敏感信息；
- 不把密钥写入异常。

### 3. 显式优于魔法

- 配置可见；
- 错误清晰；
- 行为可覆盖；
- 不隐藏关键决策；
- 不自动吞异常。

### 4. 与语言习惯一致

- Python：snake_case、上下文管理器、迭代器；
- Go：context、error、显式配置；
- Java：Builder、Optional、异常体系；
- TypeScript：Promise、类型、AbortSignal；
- Ruby：block、符号；
- Rust：Result、async、所有权。

### 5. 可观测

- 请求 ID；
- 重试次数；
- 延迟；
- 错误分类；
- 可接入 OpenTelemetry；
- 可注入 HTTP 客户端。

## 35.4 认证

```python
client = Client(api_key=os.environ["API_KEY"])
```

或：

```go
client := NewClient(WithTokenSource(tokenSource))
```

要点：

- 不硬编码密钥；
- 支持环境变量；
- 支持短期令牌/刷新；
- 支持自定义 HTTP 客户端；
- 不把密钥写入日志；
- 支持多环境；
- 支持多租户；
- 支持代理。

### 令牌刷新

```python
client = Client(
    token_provider=lambda: get_token(),
    refresh_margin=60,  # 提前 60 秒刷新
)
```

SDK 应：

- 自动刷新；
- 并发安全；
- 刷新失败处理；
- 不重复刷新。

## 35.5 重试

SDK 应：

- 默认只重试幂等请求；
- 支持配置；
- 遵循 `Retry-After`；
- 指数退避 + 抖动；
- 有最大次数/总时长；
- 暴露重试事件；
- 不重试业务错误。

```python
client = Client(
    max_retries=3,
    retry_on=[429, 502, 503, 504],
    timeout=10.0,
)
```

### 重试与幂等

对非幂等请求：

- 默认不重试；
- 如果用户提供幂等键，可以重试；
- 文档说明。

### 重试的可观测性

```python
@client.on_retry
def on_retry(event):
    logger.warning("retrying", attempt=event.attempt, reason=event.reason)
```

## 35.6 分页

不要把“取一页”和“遍历全部”混成同一个方法。清晰的做法是：

```python
# 显式分页：返回一页
page = client.orders.list(limit=20, status="PAID")
for order in page.data:
    print(order.id)
next_cursor = page.next_cursor

# 自动遍历：内部处理游标
for order in client.orders.list_all(status="PAID"):
    print(order.id)
```

SDK 内部：

```python
def list(self, **params):
    return self._request("GET", "/orders", params=params)

def list_all(self, **params):
    cursor = None
    while True:
        page = self.list(**params, cursor=cursor)
        yield from page.data
        cursor = page.next_cursor
        if not cursor:
            break
```

统一使用一个游标字段名（例如 `next_cursor`），不要在 SDK、JSON、文档里混用 `nextCursor` / `next_cursor`。如果 API 使用驼峰，SDK 应在语言习惯层做一次显式转换，而不是两套名字并存。

### 分页的陷阱

- 自动分页可能无限循环；
- 数据变化导致重复/遗漏；
- 速率限制；
- 内存；
- 取消。

SDK 应：

- 支持手动分页；
- 支持限制总数；
- 支持取消；
- 遵循限流。

离线队列、同步游标与冲突合并的 SDK 设计，见[附录 L](../appendices/l-mobile-offline-sync.md)。

## 35.7 流式

SSE：

```python
with client.events.stream(order_id="ord_123") as stream:
    for event in stream:
        print(event.type, event.data)
```

WebSocket：

```python
async with client.connect() as conn:
    async for msg in conn:
        ...
```

要点：

- 自动重连；
- 断点恢复；
- 背压；
- 取消；
- 资源清理；
- 心跳。

## 35.8 错误

```python
try:
    client.orders.get("ord_123")
except NotFoundError as e:
    ...
except RateLimitError as e:
    print(e.retry_after)
except APIError as e:
    print(e.code, e.request_id, e.status)
```

错误层次：

```text
APIError
├── AuthenticationError
├── AuthorizationError
├── NotFoundError
├── ValidationError
├── ConflictError
├── RateLimitError
├── ServerError
└── NetworkError
```

错误对象应包含：

- HTTP 状态；
- 错误码；
- 消息；
- 字段级错误；
- request ID；
- 是否可重试；
- 原始响应（可选，脱敏）；
- 文档链接。

### 错误映射

```python
if status == 404:
    raise NotFoundError(...)
elif status == 409:
    raise ConflictError(...)
elif status == 422:
    raise ValidationError(field_errors=...)
```

### 不要隐藏错误

- 不要把所有错误都变成 `Exception`；
- 不要吞掉错误；
- 不要丢失 request ID；
- 不要暴露敏感信息。

## 35.9 版本与兼容

- SDK 版本遵循 SemVer；
- 主版本对应 API 主版本；
- 弃用警告；
- 迁移指南；
- 支持窗口；
- 生成代码与手写封装分离。

### SDK 版本策略

```text
SDK 2.x → API v2
SDK 1.x → API v1（维护）
```

- 旧 SDK 继续工作；
- 新功能只在新 SDK；
- 安全修复回补；
- 弃用时间表。

## 35.10 分发

- 包管理器；
- 签名；
- SBOM；
- 发布说明；
- 多语言；
- 示例；
- 安全公告；
- 版本锁定；
- 校验和；
- 供应链安全。

### 分发的信任

- 包名抢注；
- 依赖混淆；
- 签名验证；
- 发布者身份；
- 2FA；
- 构建可复现。

## 35.11 测试

- 单元测试；
- 契约测试；
- 集成测试；
- 重试/分页/流式测试；
- 错误映射测试；
- 多语言一致性；
- 真实沙箱。

## 35.12 文档

- 安装；
- 认证；
- 快速开始；
- 分页；
- 错误；
- 重试；
- 流式；
- 版本；
- 迁移；
- 常见问题。

示例应可运行，而不是伪代码。

## 35.13 反模式

- SDK 与 API 不同步；
- 默认无限重试；
- 隐藏错误；
- 分页要求手动；
- 流式无重连；
- 密钥写日志；
- 生成物巨大且不可读；
- 每种语言行为不一致；
- 没有版本策略；
- 没有沙箱；
- SDK 自动吞掉 4xx；
- SDK 的重试与网关重试叠加。

## 35.14 SDK 的遥测、错误分类与发布灰度

SDK 不只是“把 HTTP 包一层”。它还是 API 契约在客户端一侧的执行者：决定何时重试、如何分页、怎样暴露错误，以及把什么遥测带回服务端。这一节讨论三个容易被低估的部分。

### 35.14.1 遥测：让 SDK 成为可观测性的一部分

一个好的 SDK 应该让调用方**默认获得可观测性**，而不是要求每个人自己写埋点。

最低配：

- 每个请求生成 `request_id`，并通过响应头回传；
- 支持注入 OpenTelemetry `traceparent`；
- 在 User-Agent 中携带 SDK 名称、版本、语言、运行时；
- 暴露请求耗时、重试次数、状态码、错误码等指标；
- 提供 `debug` 模式，打印脱敏后的请求/响应摘要。

```text
User-Agent: acme-sdk-python/2.4.1 python/3.12.4 linux
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
```

注意事项：

- **默认不采集 PII**：URL 查询参数、请求体、响应体可能含敏感数据；
- 指标标签避免高基数（用户 ID、订单 ID 不要进标签）；
- 遥测上报要可关闭、可采样、有明确文档；
- 不要把 API Key 写进日志、trace 或错误信息；
- 服务端可以利用 SDK 版本分布做弃用与兼容决策。

### 35.14.2 错误分类：让调用方知道“下一步做什么”

错误层次不应只按 HTTP 状态码分组，而应按**调用方动作**分组：

| 类别 | 例子 | 调用方动作 |
|---|---|---|
| 可重试 | 429、503、网络超时 | 退避重试；遵守 `Retry-After` |
| 需修正请求 | 400、422 | 修参数；不要重试 |
| 需重新认证 | 401 | 刷新令牌；避免循环 |
| 无权限 | 403 | 申请权限；不要重试 |
| 不存在 | 404 | 检查 ID；不要盲目重试 |
| 状态冲突 | 409、412 | 重新读取；合并或让用户决策 |
| 服务端错误 | 500、502 | 有限重试；上报 request ID |
| 客户端取消 | 取消/超时 | 不要当作服务端错误 |

SDK 应该：

- 暴露 `retryable`、`request_id`、`docs_url`、`field_errors`；
- 对 401 做单次刷新，避免无限循环；
- 对 429 尊重 `Retry-After` 与抖动；
- 对非幂等请求默认不重试，除非调用方显式提供幂等键；
- 保留原始响应供排查，但默认脱敏；
- 不要把 4xx 吞掉变成空结果。

### 35.14.3 分页、流式与取消的统一抽象

不同语言的 SDK 应该有一致的行为语义：

```python
for order in client.orders.list(status="paid"):
    ...
```

背后要处理：

- 游标过期与自动刷新；
- 空页但仍有下一页；
- 并发修改导致的重复/遗漏；
- 最大页数保护；
- 取消与超时；
- 内存上限（不要默默把百万行读进列表）。

流式接口要：

- 自动重连并暴露“可能重复”的语义；
- 区分“正常结束”“服务端错误”“客户端取消”；
- 提供背压或至少可配置的缓冲上限；
- 在断线后从 last event ID 恢复（如果服务端支持）。

### 35.14.4 发布灰度与版本偏斜

SDK 发布比服务端发布更危险，因为**你无法回滚已经装到用户机器上的版本**。

实践：

- 先发 release candidate，给内部与合作伙伴试用；
- 用 feature flag 控制行为变更，而不是只靠版本号；
- 保留旧版本的兼容窗口与安全回补；
- 在 SDK 中打印弃用警告，并指向迁移指南；
- 监控各版本的使用分布、错误率与升级速度；
- 对新版本做跨语言一致性测试（同一请求在各语言 SDK 中产生等价结果）；
- 安全公告要有明确的受影响版本、修复版本与缓解措施。

### 35.14.5 SDK 作为契约守门人

| 契约风险 | SDK 可以做什么 |
|---|---|
| 重试风暴 | 指数退避 + 抖动 + 重试预算 |
| 重复写 | 自动生成幂等键（可配置） |
| 分页漂移 | 游标抽象 + 去重提示 |
| 流式中断 | 断线重连 + last event ID |
| 版本碎片 | User-Agent + 弃用警告 + 遥测 |
| 敏感泄露 | 默认脱敏日志 + 禁止 Key 入日志 |
| 跨语言不一致 | 共享测试向量与行为规范 |

## 35.15 本章小结

- SDK 是 API 的产品化外壳，决定首次体验。
- 生成 + 手写封装是常见且有效的组合。
- 认证、重试、分页、流式、错误映射是核心能力。
- 默认安全、显式配置、语言惯用。
- 版本、分发、测试、文档缺一不可。
- SDK 的质量会放大或掩盖 API 设计的质量。
- 遥测、错误分类与发布灰度决定 SDK 能否长期运营。
- SDK 是契约在客户端的守门人：它可以把正确的默认值变成习惯，也可以把错误放大到所有用户。

## 设计练习

为一个“文件存储 API”设计 Python 和 TypeScript SDK：

- 上传/下载；
- 分片上传；
- 预签名 URL；
- 分页列出文件；
- 错误映射；
- 重试；
- 异步任务。

写出核心接口与错误层次。然后回答：

1. 分片上传如何暴露进度？
2. 大文件下载如何流式处理？
3. 重试如何避免重复上传？
4. 两种语言的错误层次如何保持一致？

---

上一章：[可靠性与容错：超时、重试、熔断与背压](34-reliability.md)  
下一章：[文档、示例与开发者体验](36-docs-and-dx.md)
