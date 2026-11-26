# 第 13 章 REST Server：协议、错误模型与中间件

## 学习目标

- 理解一个 MLflow 风格服务端的路由、序列化和错误边界。
- 掌握鉴权、请求体限制、CORS、内容类型和 XSS 防护。
- 能用 curl 完成完整闭环，并能定位 4xx/5xx 的来源。
- 识别单进程 `http.server` 与生产 WSGI/ASGI 的差距。

## 13.1 为什么自己写 HTTP 层

Mini 没有使用 Flask/FastAPI，原因是：

- 依赖少，读者能读完每个请求路径；
- 避免框架魔法掩盖路由/鉴权/错误处理；
- 教学重点是语义，不是 Web 框架。

代价：

- 没有自动参数校验/OpenAPI；
- 线程/连接管理简单；
- 性能与可观测性有限；
- 需要手写 JSON、CORS、错误映射。

## 13.2 路由表

### 健康与 UI

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 不需要鉴权，返回版本/后端/是否启用鉴权 |
| GET | `/` | 302 到 `/ui` |
| GET | `/ui` | 实验与 run 概览 |
| GET | `/ui/experiments/{id}` | 实验详情 |
| GET | `/ui/runs/{id}` | run 详情 |
| GET | `/ui/traces` / `/ui/traces/{id}` | trace 列表/详情（含 assessments） |
| GET | `/ui/registry` | 模型注册表 |
| GET | `/ui/prompts` | prompt 版本/别名 |

### Tracking

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/2.0/mlflow/experiments/create` | 创建实验 |
| GET | `/experiments/get` | 按 ID |
| GET | `/experiments/get-by-name` | 按名称 |
| POST | `/experiments/search` | 过滤/排序/分页 |
| POST | `/experiments/delete` / `restore` | 软删除/恢复 |
| POST | `/experiments/set-experiment-tag` | 标签 |
| POST | `/runs/create` | 创建/启动 run |
| POST | `/runs/get` | 详情 |
| POST | `/runs/update` | 改名/状态 |
| POST | `/runs/end` | 终结 |
| POST | `/runs/search` | 搜索 |
| POST | `/runs/log-parameter` / `log-metric` / `log-batch` | 日志 |
| POST | `/runs/set-tag` / `delete-tag` | 标签 |
| GET | `/metrics/get-history` | 指标历史 |
| POST | `/runs/log-input` | 数据集输入 |

### Artifacts

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/artifacts/list` | 列出 run 下路径 |
| POST | `/artifacts/log` | JSON + base64 上传 |
| GET | `/artifacts/get` | 原始字节下载 |

### Registry

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/2.0/mlflow/registered-models/create` | 创建注册模型 |
| GET | `/registered-models/get` | 按 name 读取 |
| POST | `/registered-models/search` | 搜索 |
| POST | `/registered-models/delete` | 删除 |
| POST | `/model-versions/create` | 创建版本 |
| GET | `/model-versions/get` | 按 name/version |
| POST | `/model-versions/search` | 搜索版本 |
| POST | `/model-versions/aliases/set` / `delete` | 别名 |
| GET | `/model-versions/get-by-alias` | 按别名解析 |

完整字段与 LoggedModel/Registry 的 3.x 关系见附录 A.5。

### Trace / Prompt / Assessment / Webhook

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/2.0/mlflow/traces/log` | 写 trace |
| GET | `/traces/get` | 读 trace |
| POST | `/traces/search` | 搜索 trace |
| POST | `/traces/assessments/log` | feedback/expectation |
| GET | `/traces/assessments/get` / `list` | 读 assessment |
| POST | `/traces/assessments/delete` | 删除 |
| POST | `/prompts/create` | 注册新版本 |
| GET | `/prompts/get` | 按 name/version/alias |
| GET | `/prompts/versions/list` | 版本列表 |
| POST | `/prompts/search` | 搜索最新版本 |
| POST | `/prompts/aliases/set` / `delete` | 别名 |
| POST | `/prompts/delete` | 删除 prompt |
| POST | `/webhooks/create` | 创建事件订阅 |
| GET | `/webhooks/get` / `list` | 查询订阅 |
| POST | `/webhooks/update` / `delete` / `test` | 修改、删除、测试投递 |
| GET | `/webhooks/deliveries` | 投递历史（教学扩展） |

### LoggedModel / Dataset / Monitor

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/2.0/mlflow/logged-models` | 创建 PENDING 模型 |
| GET/PATCH/DELETE | `/logged-models/{model_id}` | 读取、finalize、删除 |
| POST | `/logged-models/search` | 模型实体搜索 |
| POST | `/logged-models/{model_id}/params` / `metrics` | 模型级数据 |
| POST | `/datasets/create` | 创建评测数据集 |
| GET | `/datasets/get` | 读取记录 |
| POST | `/datasets/search` | 元数据搜索 |
| POST | `/datasets/merge-records` / `delete` | 合并、删除 |
| POST | `/monitors/create` | 创建监控 |
| GET | `/monitors/get` / `list` | 读取 |
| POST | `/monitors/delete` | 删除（级联窗口） |
| POST | `/monitors/windows/create` | 评估并写窗口 |
| GET | `/monitors/windows/list` | 窗口历史 |

完整字段见附录 A.5.1、A.6.3、A.6.6。

### Gateway

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/gateway/routes` | 路由与 provider（不含密钥） |
| GET | `/gateway/stats` | 请求/成本/延迟统计 |
| POST | `/gateway/invocations` | `{route, messages, params}` |

Gateway 路由只在服务端配置了 `--gateway-config` 时可用；否则返回 404。它不在 `/api/2.0/mlflow/` 前缀下，因为它是独立的控制面组件，而不是 tracking 协议的一部分。

Webhook 路由在 `/api/2.0/mlflow/webhooks/*` 下，订阅 Registry/Prompt/Dataset 的写事件；`/webhooks/test` 会同步调用一次投递器并返回 `WebhookDelivery`，便于验证签名与网络。真实 MLflow OSS 的 API 形状相近，但 Databricks 使用 `/registry-webhooks/*` 和不同的事件/签名规范（第 38 章）。

完整字段见附录 A。除健康检查、UI 与 `/gateway/*` 外，API 路径以 `/api/2.0/mlflow/` 为前缀。

## 13.3 请求处理链

```text
socket
  → BaseHTTPRequestHandler
  → 鉴权（Bearer / Cookie / 查询 token）
  → Content-Length 校验/请求体上限
  → JSON 解析
  → 路由 → Service
  → 序列化
  → 错误映射
  → 安全响应头
```

`server.py` 的原则：**路由很薄**。每个 handler 只做：

1. 必填字段提取；
2. 简单类型转换（带错误码）；
3. 调用 service；
4. `to_dict()`。

业务规则在 `TrackingService`/`ModelRegistry`，HTTP 与 CLI/本地客户端共享同一套校验，减少行为分歧；协议序列化/字段覆盖仍需契约测试兜底。

## 13.4 错误响应

统一：

```json
{
  "error_code": "RESOURCE_DOES_NOT_EXIST",
  "message": "run 'abc' not found"
}
```

HTTP 状态与错误码对应：

- 400：参数/语法/冲突（业务重名）；
- 401/403：鉴权；
- 404：资源不存在；
- 409：状态冲突（Mini 用于部分场景）；
- 413：请求体过大；
- 500：未预期异常。

未知异常：

```python
LOGGER.exception(...)
send({"error_code": "INTERNAL_ERROR", "message": "internal server error"})
```

不要把堆栈或 SQL 发给客户端。日志保留 traceback，响应只给通用信息。

### 常见 500 来源

- `int()` 转换未捕获（Mini 使用 `_int` 修复）；
- `Trace.from_dict` 缺字段（HTTP 边界转 400）；
- 文件系统权限/路径错误（应映射为 4xx/503）；
- SQLite 锁超时（应为 503/重试，而不是 500）；
- 序列化不支持的对象（如 `Path`）。

## 13.5 鉴权

Mini 的模型：

- 服务端配置一个共享 token；
- API 支持 `Authorization: Bearer <token>`；
- 浏览器 UI 支持 `?token=` 设置 HttpOnly Cookie，之后用 Cookie 读取；
- Cookie 只授权 GET/HEAD（UI 与 artifact 下载），写操作必须用 Bearer，降低 CSRF 风险；
- 健康检查不需要 token。

```bash
curl -H "Authorization: Bearer secret" \
  http://127.0.0.1:5000/api/2.0/mlflow/runs/get \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"..."}'
```

局限：

- 没有用户身份，只有“有 token/无 token”；
- 没有 RBAC（谁能读哪个实验）；
- 没有 token 轮换/过期；
- 查询字符串 token 会进日志/浏览器历史，应仅用于本地引导；
- Cookie 方式没有 CSRF token（SameSite=Lax 降低风险，但不等于安全）。

生产应使用：

- OIDC/OAuth2；
- 反向代理/网关做 TLS 与身份；
- 细粒度授权（实验/模型/环境）；
- 审计日志；
- 密钥管理。

## 13.6 请求体与上传

- `Content-Length` 必须存在；
- 超过 `max_request_bytes` 返回 413；
- JSON 必须是对象；
- artifact 上传 base64 解码后再次检查大小；
- 解码失败返回 400。

为什么限制 JSON 请求体？

- 防止内存耗尽；
- 防止超大 batch 拖垮数据库；
- 给客户端明确的重试/分片信号。

真实生产还应：

- 限制并发连接；
- 设置读超时；
- 对慢客户端做保护；
- 对 artifact 使用流式上传。

## 13.7 CORS

配置 `cors_origin` 后：

```text
Access-Control-Allow-Origin: https://example.test
Access-Control-Allow-Methods: GET, POST, OPTIONS
Access-Control-Allow-Headers: Authorization, Content-Type
```

注意：

- 不要用 `*` 配合凭证；
- CORS 不是鉴权；
- 预检请求不携带业务凭证，OPTIONS 不应要求 token；
- 生产应明确允许的源，而不是反射任意 Origin。

## 13.8 UI 的安全渲染

Mini 的 UI 是服务端字符串拼接，所以每个动态值都通过 `html.escape`：

```python
def _e(value): return html.escape(str(value), quote=True)
```

风险点：

- 实验名/标签/错误信息/artifact 路径；
- `<pre>` 中的 JSON；
- URL 查询参数。

测试覆盖了 `<script>` 和 `<img onerror>` 注入。artifact 文件下载强制 `application/octet-stream` + `Content-Disposition: attachment`，避免上传恶意 HTML 后形成同源 XSS。

## 13.9 线程模型与并发

`ThreadingHTTPServer` 每个请求一个线程：

- 简单，适合低并发；
- 无连接池上限，可能被大量连接耗尽；
- 每个线程一个 SQLite 连接；
- 写操作串行（SQLite 单写者）。

生产选择：

- Gunicorn/Uvicorn + 多 worker；
- 连接池；
- 限流；
- 异步 trace 写入；
- 健康检查与优雅停机。

## 13.10 与真实 MLflow 的差距

- 真实服务器基于 Flask/Gunicorn，接口字段和错误码更完整；
- 真实 artifact 协议支持 multipart/预签名；
- 真实鉴权支持 basic/OIDC/云 IAM；
- 真实 UI 是 React 应用，功能丰富；
- 真实部署有反向代理、TLS、监控、配额。

## 小结

- 路由应薄，业务规则在 service；错误码是协议。
- 鉴权、请求体限制、CORS、安全响应头缺一不可。
- 服务端 HTML 必须全量转义；artifact 不能作为可执行内容返回。
- `http.server` 仅适合本地/教学，生产要换成熟服务器。

## 练习

1. 给 `/health` 增加数据库连通性检查，设计超时和状态码。
2. 为 artifact 上传设计流式 multipart，说明如何限制大小和校验 digest。
3. 设计一个最小 RBAC 模型（实验级读/写、Registry 发布权限）。

---

下一章：[第 14 章 Python Client](14-client.md)
