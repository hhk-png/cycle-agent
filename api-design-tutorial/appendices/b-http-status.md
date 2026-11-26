# 附录 B：HTTP 状态码与方法速查

## B.1 方法

| 方法 | 安全 | 幂等 | 请求体 | 成功状态 | 典型用途 |
|---|---|---|---|---|---|
| GET | 是 | 是 | 不应有 | 200 | 读取资源 |
| HEAD | 是 | 是 | 无 | 200 | 读取元数据 |
| OPTIONS | 是 | 是 | 无 | 204/200 | 能力发现/CORS |
| POST | 否 | 否 | 有 | 201/202/200 | 创建/动作 |
| PUT | 否 | 是 | 有 | 200/201/204 | 全量替换/创建 |
| PATCH | 否 | 视实现 | 有 | 200/204 | 部分修改 |
| DELETE | 否 | 是 | 可无 | 204/200/202 | 删除 |
| QUERY | 是 | 是 | 可有 | 200 | 安全复杂查询（RFC 10008，2026-06） |

### 说明

- **安全**：不改变服务端状态。
- **幂等**：多次执行的效果与一次相同。
- 非幂等 POST 可用 `Idempotency-Key` 实现幂等。
- PATCH 的幂等性取决于操作语义。
- QUERY 的支持度需确认客户端/代理/框架。

## B.2 状态码

### 2xx 成功

| 码 | 名称 | 用途 |
|---|---|---|
| 200 | OK | 一般成功 |
| 201 | Created | 创建成功，带 `Location` |
| 202 | Accepted | 异步接受，带操作资源 |
| 203 | Non-Authoritative Information | 代理修改了响应（少用） |
| 204 | No Content | 成功无响应体 |
| 206 | Partial Content | 范围请求 |
| 207 | Multi-Status | 批量部分成功（WebDAV，慎用） |
| 208 | Already Reported | WebDAV |
| 226 | IM Used | Delta 编码（少用） |

### 3xx 重定向

| 码 | 名称 | 用途 |
|---|---|---|
| 300 | Multiple Choices | 多选择 |
| 301 | Moved Permanently | 永久重定向，可能改方法 |
| 302 | Found | 临时重定向，历史方法改写 |
| 303 | See Other | 用 GET 查看结果 |
| 304 | Not Modified | 条件请求未变化 |
| 305 | Use Proxy | 已弃用 |
| 307 | Temporary Redirect | 临时，保持方法 |
| 308 | Permanent Redirect | 永久，保持方法 |

API 中重定向要谨慎，可能改变方法、丢失 Authorization、被缓存。

### 4xx 客户端错误

| 码 | 名称 | 典型场景 |
|---|---|---|
| 400 | Bad Request | 语法/参数错误 |
| 401 | Unauthorized | 未认证/凭证无效 |
| 402 | Payment Required | 付费/配额（少用） |
| 403 | Forbidden | 已认证但无权限 |
| 404 | Not Found | 资源不存在 |
| 405 | Method Not Allowed | 方法不允许，带 `Allow` |
| 406 | Not Acceptable | 无法满足 `Accept` |
| 407 | Proxy Authentication Required | 代理认证 |
| 408 | Request Timeout | 请求超时 |
| 409 | Conflict | 状态冲突/重复创建 |
| 410 | Gone | 永久移除 |
| 411 | Length Required | 缺少 `Content-Length` |
| 412 | Precondition Failed | `If-Match` 失败 |
| 413 | Content Too Large | 请求体过大 |
| 414 | URI Too Long | URL 过长 |
| 415 | Unsupported Media Type | 媒体类型不支持 |
| 416 | Range Not Satisfiable | 范围无效 |
| 417 | Expectation Failed | `Expect` 失败 |
| 418 | I'm a teapot | 玩笑/彩蛋（不要用于生产语义） |
| 421 | Misdirected Request | 请求发错服务器 |
| 422 | Unprocessable Content | 语义校验失败 |
| 423 | Locked | 资源锁定（WebDAV） |
| 424 | Failed Dependency | 依赖失败（WebDAV） |
| 425 | Too Early | 重放风险（TLS 1.3） |
| 426 | Upgrade Required | 需要升级协议 |
| 428 | Precondition Required | 要求条件请求 |
| 429 | Too Many Requests | 限流 |
| 431 | Request Header Fields Too Large | 头过大 |
| 451 | Unavailable For Legal Reasons | 法律原因 |

### 5xx 服务端错误

| 码 | 名称 | 典型场景 |
|---|---|---|
| 500 | Internal Server Error | 未预期错误 |
| 501 | Not Implemented | 未实现 |
| 502 | Bad Gateway | 上游无效响应 |
| 503 | Service Unavailable | 暂不可用/过载 |
| 504 | Gateway Timeout | 上游超时 |
| 505 | HTTP Version Not Supported | 版本不支持 |
| 506 | Variant Also Negotiates | 内容协商循环 |
| 507 | Insufficient Storage | 存储不足（WebDAV） |
| 508 | Loop Detected | 循环（WebDAV） |
| 510 | Not Extended | 扩展（少用） |
| 511 | Network Authentication Required | 网络认证（热点） |

## B.3 选择建议

### 创建

- 同步创建成功：`201 Created` + `Location`。
- 异步接受：`202 Accepted` + 操作资源。
- 已存在且幂等：`200` 返回已有资源，或 `409`（视语义）。

### 更新

- 全量替换：`PUT`，`200`/`204`。
- 部分更新：`PATCH`，`200`/`204`。
- 版本冲突：`409` 或 `412`。

### 删除

- 成功无内容：`204`。
- 异步删除：`202`。
- 已删除：`204`（幂等）或 `404`（视策略）。
- 永久移除：`410`。

### 校验

- 语法/格式：`400`。
- 语义校验：`422`。
- 冲突：`409`。

### 认证/授权

- 未认证：`401` + `WWW-Authenticate`。
- 无权限：`403`。
- 隐藏资源存在性时可用 `404`（需文档说明）。

### 限流/过载

- 限流：`429` + `Retry-After`。
- 过载/维护：`503` + `Retry-After`。
- 上游超时：`504`。

## B.4 常见错误

- 所有错误 200。
- 所有错误 500。
- 401/403 混用。
- 400/422 混用。
- 409 用于参数校验。
- 429 不返回 `Retry-After`。
- 503 不说明可重试。
- 用 301 做 API 迁移导致方法改变。
- 204 返回响应体。
- 201 不带 `Location`。

## B.5 相关头

```http
Allow: GET, POST
Location: /orders/ord_123
Retry-After: 30
WWW-Authenticate: Bearer realm="api"
Content-Range: bytes 0-1023/1048576
Accept-Ranges: bytes
ETag: "v42"
Cache-Control: private, max-age=60
```

---

返回：[目录](../SUMMARY.md)
