# 第 21 章 HTTP 深度：语义、连接、缓存与调试

## 21.1 HTTP 不只是“请求和响应”

HTTP 是一套语义层，定义了：

- 方法与安全/幂等性；
- 状态码；
- 头部字段；
- 内容协商；
- 缓存；
- 条件请求；
- 范围请求；
- 认证框架；
- 代理行为；
- 连接管理。

很多 API 问题源于只把 HTTP 当作传输管道，而忽略了它的语义。

### 为什么 API 设计者要懂 HTTP

因为 HTTP 的每一层语义都会被中间件使用：

- 浏览器根据方法决定是否预取；
- CDN 根据缓存头决定是否缓存；
- 代理根据状态码决定是否重试；
- 网关根据 `Content-Type` 决定如何解析；
- 监控根据状态码判断健康；
- 安全扫描器根据头部判断配置。

你不使用这些语义，不代表它们不存在；它们会以“意外行为”的形式回来。

## 21.2 方法与语义

| 方法 | 安全 | 幂等 | 请求体 | 典型用途 |
|---|---|---|---|---|
| GET | 是 | 是 | 不应有 | 读取 |
| HEAD | 是 | 是 | 无 | 元数据 |
| OPTIONS | 是 | 是 | 无 | 能力发现/CORS |
| POST | 否 | 否 | 有 | 创建/动作 |
| PUT | 否 | 是 | 有 | 全量替换 |
| PATCH | 否 | 视实现 | 有 | 部分修改 |
| DELETE | 否 | 是 | 可无 | 删除 |
| QUERY | 是 | 是 | 可有 | 复杂查询（RFC 10008） |

### 安全方法

安全方法不应改变服务端状态。用 GET 执行删除或支付是严重错误，因为：

- 浏览器预取；
- 爬虫；
- 缓存；
- 代理重试；
- 用户刷新；
- 链接预览；
- 安全扫描器。

### 幂等方法

幂等意味着多次执行效果相同。PUT 和 DELETE 应设计为幂等；POST 可通过幂等键实现。

### GET 与请求体

HTTP 语义允许 GET 带请求体，但实践中：

- 很多代理/服务器会丢弃；
- 缓存可能忽略；
- 客户端库支持不一；
- 语义不清晰。

需要复杂查询条件时：

- 用查询参数（有长度限制）；
- 用 POST + 查询资源（牺牲缓存语义）；
- 用 QUERY 方法（RFC 10008，如果中间件支持）；
- 用持久化查询 ID。

### QUERY 方法

QUERY 于 2026 年 6 月发布为 RFC 10008，用于“安全、幂等、可缓存、但需要请求体”的查询场景，例如复杂搜索条件。它解决 GET URL 过长、查询参数泄露以及 POST 不保证安全/幂等的问题。标准发布不等于生态就绪：采用前要确认客户端、代理、网关、WAF、CDN 和框架能否正确处理新方法，并保留 POST + 查询资源或持久化查询 ID 的回退。

## 21.3 状态码

状态码分为五类：

- 1xx：信息；
- 2xx：成功；
- 3xx：重定向；
- 4xx：客户端错误；
- 5xx：服务端错误。

常见组合：

```text
200 OK                 读取/更新成功
201 Created            创建成功，带 Location
202 Accepted           异步接受
204 No Content         成功但无响应体
206 Partial Content    范围请求
301/308                永久重定向
302/307                临时重定向
304 Not Modified       条件请求未变化
400 Bad Request        格式/参数错误
401 Unauthorized       未认证
403 Forbidden          无权限
404 Not Found          不存在
405 Method Not Allowed 方法不允许
406 Not Acceptable     无法满足 Accept
409 Conflict           状态冲突
410 Gone               已永久移除
412 Precondition Failed 条件失败
413 Content Too Large  请求体过大
415 Unsupported Media Type 媒体类型不支持
422 Unprocessable Content 语义校验失败
428 Precondition Required 需要条件请求
429 Too Many Requests  限流
500 Internal Server Error 未预期错误
501 Not Implemented    未实现
502 Bad Gateway        上游错误
503 Service Unavailable 暂不可用
504 Gateway Timeout    上游超时
```

### 重定向

- `301` 永久，可能被缓存；API 中慎用。
- `308` 永久且保持方法。
- `302` 临时，历史上有方法改写问题。
- `307` 临时且保持方法。

API 中重定向应谨慎，因为：

- 客户端可能不跟随；
- 可能改变方法；
- 可能泄露 `Authorization` 头；
- 可能缓存错误；
- 跨域重定向可能被浏览器阻止。

如果必须重定向，优先 `307`/`308`，并确保客户端支持。

### 1xx

- `100 Continue`：客户端可以继续发送请求体；
- `103 Early Hints`：提前发送资源提示；
- `101 Switching Protocols`：WebSocket 升级。

API 中较少直接使用，但网关可能生成。

## 21.4 头部字段

### 通用

```http
Host: api.example.com
User-Agent: ...
Accept: application/json
Accept-Encoding: gzip, br
Content-Type: application/json
Content-Length: 123
Authorization: Bearer ...
Request-Id: ...
```

### 缓存

```http
Cache-Control
ETag
Last-Modified
If-None-Match
If-Match
Vary
Age
```

### 限流

```http
RateLimit-Limit
RateLimit-Remaining
RateLimit-Reset
Retry-After
```

### 追踪

```http
traceparent
tracestate
baggage
```

### 安全

```http
Strict-Transport-Security
Content-Security-Policy
X-Content-Type-Options
Access-Control-Allow-Origin
```

### 自定义头

- 使用 `X-` 前缀已不推荐（历史遗留）；
- 自定义头应文档化；
- 不要用自定义头传递敏感信息；
- 不要依赖代理一定转发；
- 头名大小写不敏感。

## 21.5 内容协商

```http
Accept: application/json
Accept-Language: zh-CN
Accept-Encoding: gzip, br
```

服务端可以返回：

```http
HTTP/1.1 406 Not Acceptable
```

如果无法满足。也可以忽略 `Accept` 返回默认格式，但应文档化。

### 质量值

```http
Accept: application/json;q=1.0, application/xml;q=0.5
```

服务端应选择可满足的最高优先级格式。

### 内容协商的陷阱

- 某些客户端发送 `Accept: */*`，服务端应返回默认格式；
- `Accept` 可能被代理修改；
- 缓存需要 `Vary: Accept`；
- 错误响应也应遵守 `Accept`（至少返回 JSON）；
- 不要让内容协商影响权限。

## 21.6 条件请求

```http
GET /orders/123
If-None-Match: "v42"
```

```http
HTTP/1.1 304 Not Modified
ETag: "v42"
```

写操作：

```http
PUT /orders/123
If-Match: "v42"
```

```http
HTTP/1.1 412 Precondition Failed
```

条件请求同时服务缓存和并发控制。

### ETag 的生成

- 基于内容哈希；
- 基于版本号；
- 基于更新时间；
- 必须唯一标识表示；
- 不要基于随机数（无法验证）；
- 弱 ETag 不能用于 `If-Match`。

### 条件请求的顺序

服务器应：

1. 验证认证；
2. 验证授权；
3. 评估前置条件；
4. 执行操作。

如果先执行再检查前置条件，可能产生副作用。

## 21.7 范围请求

```http
GET /videos/video.mp4
Range: bytes=0-1048575
```

```http
HTTP/1.1 206 Partial Content
Content-Range: bytes 0-1048575/104857600
Accept-Ranges: bytes
```

用于视频拖动、断点续传、大文件。

### 多范围

```http
Range: bytes=0-1023, 2048-3071
```

响应为 `multipart/byteranges`。实现复杂，很多服务器只支持单范围。

### 范围请求与压缩

`Range` 作用于**选定表示（selected representation）的字节**，也就是内容编码之后的字节。启用压缩时，客户端请求的偏移量指向压缩后的表示，而不是原始文件。

实践建议：

- 对支持范围请求的大文件，明确 `Content-Encoding` 与 `ETag` 的语义；
- 如果压缩会改变字节偏移，要么禁用该资源的动态压缩，要么在文档中说明偏移基于编码后表示；
- 范围请求与压缩同时使用时，确保 `ETag` 能区分不同编码版本；
- 不要假设 `Range: bytes=0-1023` 对应解压后的前 1024 字节。

## 21.8 连接管理

### HTTP/1.1

- 持久连接；
- 管道化（实际很少用）；
- 队头阻塞；
- 浏览器每域名连接数限制；
- `Connection: keep-alive`；
- `Transfer-Encoding: chunked`。

### HTTP/2

- 二进制分帧；
- 多路复用；
- 头部压缩（HPACK）；
- 服务器推送（已逐渐弃用）；
- 流优先级；
- 仍受 TCP 队头阻塞影响；
- 单连接多请求；
- 需要 ALPN。

### HTTP/3

- 基于 QUIC/UDP；
- 0-RTT/1-RTT；
- 解决 TCP 队头阻塞；
- 连接迁移；
- 需要 UDP 可达；
- 部分网络环境限制；
- TLS 1.3 集成。

### 对 API 的影响

- HTTP/2 减少连接数，适合大量小请求；
- HTTP/3 改善移动/弱网；
- gRPC 依赖 HTTP/2；
- WebTransport 依赖 HTTP/3；
- 代理和负载均衡必须支持相应协议；
- 长连接影响负载均衡；
- 连接复用可能导致流量不均。

## 21.9 trailers 与早提示

### Trailer

在响应体之后发送头部，适合流式场景的校验和、统计信息：

```http
Trailer: X-Checksum
```

```http
HTTP/1.1 200 OK
Trailer: X-Checksum
Transfer-Encoding: chunked

<chunked body>
X-Checksum: sha256=...
```

### 103 Early Hints

在最终响应前发送资源提示：

```http
HTTP/1.1 103 Early Hints
Link: </style.css>; rel=preload
```

对 API 场景较少用，但对文档站点有帮助。

## 21.10 代理、网关与调试

API 请求通常经过：

```text
客户端 → CDN → WAF → 负载均衡 → API 网关 → 服务 → 数据库
```

每一层可能：

- 修改头部；
- 缓冲响应；
- 超时；
- 限制大小；
- 改变协议；
- 缓存；
- 注入追踪；
- 重写路径；
- 添加/删除 CORS 头；
- 压缩。

调试时要检查：

- `Via`、`X-Forwarded-For`、`Forwarded`；
- `Server-Timing`；
- `traceparent`；
- 响应头是否被网关覆盖；
- 超时设置；
- 请求体大小限制；
- 缓存命中（`Age`、`X-Cache`）。

### 常见代理问题

- 代理缓冲 SSE，导致实时性丢失；
- 代理超时短于后端处理时间；
- 代理改写 `Location` 头；
- 代理丢弃自定义头；
- 代理缓存了带 `Authorization` 的响应；
- 代理不支持 HTTP/2；
- WAF 误杀合法请求。

### 调试工具

- `curl -v`；
- `curl --http2`；
- `openssl s_client`；
- 浏览器 DevTools；
- `mtr` / `traceroute`；
- 网关访问日志；
- 分布式追踪。

## 21.11 HTTP 反模式

- 用 GET 做写操作；
- 所有错误返回 200；
- 忽略 `Accept`；
- 不使用条件请求；
- 缓存头缺失或过宽；
- 在 URL 放令牌；
- 依赖 301 做 API 迁移；
- 不设置超时；
- 不处理 `HEAD`；
- 不支持 `OPTIONS`/CORS；
- 响应体过大；
- 不使用 `Content-Type`；
- 返回 HTML 错误页给 API 客户端；
- 在错误响应中返回堆栈。

## 21.12 本章小结

- HTTP 是语义层，方法、状态码、头部都有明确含义。
- 正确使用方法的安全/幂等性。
- 条件请求同时服务缓存与并发控制。
- 内容协商、范围请求、连接管理影响兼容性与性能。
- HTTP/2/3 改变了连接与多路复用模型。
- 代理链会影响头部、超时和缓存。
- 调试需要看完整请求链路。
- 结构化字段、HTTP Message Signatures（RFC 9421）与 Content-Digest（RFC 9530）把头部从“约定字符串”提升为可验证契约，见[附录 AD](../appendices/ad-cryptography-pqc.md)。
- `Date`、`Last-Modified`、`If-Modified-Since`、`Retry-After`、`Expires`、`Sunset` 的时间语义与精度陷阱，见[附录 AH](../appendices/ah-primitive-semantics.md)。

## 设计练习

用 `curl -v` 观察一个真实 API 的请求与响应，列出：

1. 使用了哪些方法/状态码/头部；
2. 是否有缓存与条件请求；
3. 是否支持压缩；
4. 是否暴露了不必要的版本信息；
5. 代理链是否改变了什么。

然后回答：

1. 如果把该 API 迁移到 HTTP/3，哪些行为会改变？
2. 哪些头是网关添加的？哪些是源站添加的？
3. 如果响应被 CDN 缓存，缓存键是什么？
4. 错误响应是否符合 `Accept`？

---

上一章：[安全纵深：OWASP API Top 10 与供应链](../part-2-security-runtime/20-security-in-depth.md)  
下一章：[REST 与超媒体：成熟度、链接与发现](22-rest-and-hypermedia.md)
