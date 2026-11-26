# 第 14 章 Python Client 与远端一致性

## 学习目标

- 理解本地/远端客户端的同一套接口如何工作。
- 掌握 active run 的上下文、线程/进程边界。
- 设计批处理、重试、超时和错误映射。
- 能用契约测试证明“本地和远端行为一致”。
- 知道 Python、R、Java 与直接 REST 的边界，能为非 Python 团队设计最小契约测试。

## 14.1 两种后端，一套接口

```python
from mini_mlflow import MiniClient

local = MiniClient(config=config)          # SQLite + 本地目录
remote = MiniClient("http://host:5000")    # HTTP
```

共同方法：

```python
experiment = client.create_experiment("demo")
run = client.start_run(experiment_id=experiment.experiment_id)
client.log_metric(run.info.run_id, "auc", 0.91)
client.log_text(run.info.run_id, "note", "notes/a.md")
runs, token = client.search_runs("metrics.auc > 0.8")
```

区别：

- 本地：直接调用 `TrackingService`；
- 远端：`urllib` 发 REST，解析实体，映射错误。

`MiniClient.search_runs` 返回 `(list[Run], next_token)`；真实 `mlflow.search_runs` 返回 pandas DataFrame，真实 `MlflowClient.search_runs` 返回 Run 实体列表。迁移时不要直接套用解包方式，详见第 8.9 节。

`MiniClient` 不试图继承真实 `mlflow.MlflowClient`，因为字段/协议不同；它的价值是让读者看到“客户端只是协议适配器”。Registry 的真实客户端用法（`create_registered_model`、`create_model_version`、`set_registered_model_alias`、`get_model_version_by_alias`）见第 12.3 节。

## 14.2 本地/远端一致性

一致性风险：

- 本地接受某字段、远端拒绝；
- 本地返回 dataclass、远端返回 dict；
- 本地错误是 `InvalidParameter`、远端是 `RemoteError`；
- 本地 artifact 路径是 Path、远端是相对字符串；
- 分页 token 的格式不同。

Mini 的策略：

1. 服务层是唯一业务规则来源；
2. 远端客户端把 JSON 还原为同一实体（`from_dict`）；
3. 服务器把异常映射为稳定错误码，客户端再映射回异常类；
4. 测试对关键场景分别跑 local/remote（如参数类型、搜索、artifact、Registry），断言核心字段相等；尚未覆盖全部方法，新增 API 应补契约测试。

测试 `tests/test_server_client.py` 覆盖：

- 实验/run/指标/搜索；
- artifact 上传/下载；
- 模型日志/加载/别名；
- trace；
- 错误形状。

仍然无法保证的：

- 序列化精度（浮点）；
- 列表顺序（真实后端可能不同）；
- 分页边界；
- 并发时序。

### 14.2.1 新增控制面方法的契约

第 33 章新增的三组方法也遵循同一套本地/远端双后端：

```python
# Prompt
prompt = client.register_prompt("support", "Hi {{name}}", tags={"owner": "qa"})
client.set_prompt_alias("support", "production", prompt.version)
client.load_prompt("prompts:/support@production")

# Assessment
client.log_feedback(trace_id, "quality", 0.9, rationale="good")
client.list_assessments(trace_id)

# Gateway（只有远端服务端可能配置）
client.gateway_routes()
client.gateway_invoke("support", [{"role": "user", "content": "hi"}])
```

注意：

- `load_prompt` 的 URI 解析在本地与远端各有一份，必须保持同一规则（`@alias` 优先于数字版本，显式 version 与 alias 互斥）；
- Assessment 的 upsert 语义由服务端决定，客户端不能自己“先查再写”，否则并发下会产生重复；
- Gateway 是服务端资源，本地客户端调用会明确报错，而不是悄悄启动一个本地网关；
- 这三组方法都应有本地/远端契约测试，特别是错误码（404/400/429/502）的映射。

## 14.3 Active Run 与 ContextVar

Fluent API：

```python
with mlflow.start_run(run_name="x") as run:
    mlflow.log_metric("auc", 0.9)
```

内部用 `ContextVar` 存 active run 栈：

```python
_ACTIVE_RUNS: ContextVar[tuple[ActiveRun, ...]] = ContextVar(..., default=())
```

原因：

- 线程安全：每个线程独立上下文；
- asyncio：每个任务在创建时继承当前上下文，之后可独立；
- 嵌套 run 可实现（`nested=True` → `mlflow.parentRunId`）。

限制：

- 子进程不继承 active run；
- 线程默认不继承（Python 的 ContextVar 不会自动跨线程）；
- 后台任务如果没显式传递 client/run_id，可能写到别处。

跨进程做法：

```python
with mlflow.start_run(run_id=os.environ["MINI_MLFLOW_RUN_ID"]):
    ...
```

Mini 的 project runner 会注入 `MINI_MLFLOW_RUN_ID` 与 `MINI_MLFLOW_TRACKING_URI`。

## 14.4 批处理

单条日志：

```python
for step, loss in enumerate(losses):
    client.log_metric(run_id, "loss", loss, step=step)
```

问题：

- 每次 HTTP 往返；
- 每个事务提交；
- 服务端压力大。

批量：

```python
client.log_batch(
    run_id,
    metrics=[{"key": "loss", "value": v, "step": i} for i, v in enumerate(losses)],
    params={"lr": 0.001},
    tags={"phase": "train"},
)
```

建议：

- 按大小（例如 1–5 MB）或时间（例如 1 秒）聚合；
- 控制单批 metric 数量，避免长事务；
- 失败时区分可重试（网络/5xx）与不可重试（4xx）；
- 指标重试可能重复追加，需要幂等键或接受重复。

## 14.5 重试与幂等

Mini 不做自动重试。原因：

- `log_metric` 不是幂等的（追加历史）；
- 创建 run 不是幂等的（除非客户端提供 run_id）；
- 创建模型版本可能重复。

生产客户端应：

```text
GET/搜索：可重试（指数退避）
创建 run：客户端预生成 run_id，重试时复用
log_batch：带批次 ID，服务端去重
模型版本：使用 digest/幂等键
```

超时：

- 连接超时与读超时分开；
- 大 artifact 上传不能用短超时；
- 超时后不要假设操作失败（可能已写入），需要幂等对账。

## 14.6 错误映射

```python
_ERROR_CLASSES = {
    "INVALID_PARAMETER_VALUE": InvalidParameter,
    "RESOURCE_DOES_NOT_EXIST": ResourceNotFound,
    "PERMISSION_DENIED": PermissionDenied,
    ...
}
```

远端异常流程：

1. `HTTPError` 读取 body；
2. 解析 `error_code/message`；
3. 选择对应异常类；
4. 保留 message，必要时附 HTTP 状态。

如果服务端返回 HTML/代理错误页：

- 不要尝试解析为业务错误；
- 抛 `RemoteError`，提示检查网关/地址；
- 记录状态码和截断后的 body（避免泄漏敏感信息）。

## 14.7 超时与连接复用

Mini 使用 `urllib.request`，每次请求新建连接（无连接池）。生产应：

- 使用 `requests`/`httpx`/`aiohttp`；
- 连接池 + keep-alive；
- 分离连接/读/写超时；
- 支持代理与 TLS 校验；
- 对 429/503 做退避；
- 对 5xx 做有限重试。

## 14.8 客户端配置

优先级：

```text
显式参数 > MINI_MLFLOW_TRACKING_URI > MLFLOW_TRACKING_URI > 本地默认
```

token：

```text
显式 auth_token > MINI_MLFLOW_TRACKING_TOKEN > MINI_MLFLOW_AUTH_TOKEN
```

不要在代码里硬编码 token；用环境变量/secret manager。

## 14.9 线程与异步

线程：

```python
def worker(i):
    client = MiniClient(...)  # 每线程独立客户端/连接
    run = client.start_run(...)
    ...
```

不要跨线程共享同一个 fluent active run；SQLite 连接也是线程本地。

asyncio：

- fluent 的 `ContextVar` 在任务创建时复制；
- 并发任务可能共享同一个 trace 状态对象（Span 列表可变），需要谨慎；
- HTTP 客户端应使用异步库，而不是阻塞 `urllib`。

## 14.10 R、Java 与 REST：多语言客户端

MLflow 的“真相”是 REST 协议与数据库语义，Python 只是最完整的绑定。团队里出现 R/Java/Scala 用户时，要分清三层：

| 层 | 作用 | 兼容性来源 |
|---|---|---|
| REST API | 所有客户端最终调用的 HTTP 契约 | `/api/2.0/mlflow/*`、JSON schema |
| 语言绑定 | 封装请求、类型、错误、artifact 上传 | Python / R / Java 官方包 |
| 约定层 | 命名、标签、指标口径、run 结构 | 团队规范，不是 SDK 保证 |

### 14.10.1 Python 之外的官方绑定

- **R**：`mlflow` R 包提供 `mlflow_start_run()`、`mlflow_log_metric()`、`mlflow_log_param()`、`mlflow_log_artifact()`、`mlflow_set_experiment()` 等函数；适合统计/数据科学团队。
- **Java/Scala**：`mlflow-client` 提供 `MlflowClient`、`createRun`、`logMetric`、`logParam`、`logArtifact` 等；适合 Spark/JVM 服务。
- **其他语言**：通常直接调 REST，或由平台团队写薄封装；不要假设所有语言的 API 面与 Python 一致。

### 14.10.2 版本偏斜

客户端与服务端版本不一致是常见故障：

- 新客户端调用旧服务端没有的字段/路由 → 404/400；
- 旧客户端读新服务端的新字段 → 忽略或解析失败；
- artifact 上传协议变化（base64 vs multipart vs 预签名）→ 上传成功但内容损坏；
- 认证方式变化（basic/OIDC/token）→ 401。

策略：

1. 先升级服务端，再升级客户端；保留一个版本的兼容窗口；
2. 用契约测试覆盖“旧客户端 × 新服务端”“新客户端 × 旧服务端”；
3. 不在客户端做“只有 Python 有”的隐式行为（自动重试、自动分页、自动类型转换）；
4. 记录客户端版本到 run tag，排障时能定位。

### 14.10.3 直接调 REST 的最小例子

```bash
curl -s -X POST "$MLFLOW_TRACKING_URI/api/2.0/mlflow/runs/log-metric" \
  -H "Authorization: Bearer $MLFLOW_TRACKING_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"<RUN_ID>","key":"auc","value":0.91,"timestamp":1764000000000,"step":1}'
```

REST 直连要自己处理：时间戳单位（毫秒）、错误码映射、分页 token、artifact 上传格式、重试幂等。第 13 章的错误模型与第 14.5 节的重试语义同样适用。

### 14.10.4 Mini 的边界

Mini 只有 Python 客户端与 REST；没有 R/Java 包，也没有跨语言契约测试。它的 REST 形状可以作为写薄客户端的参考，但字段是教学子集。若要验证多语言，最小做法是：

```text
1. 用 curl 跑通 create run -> log metric -> end run
2. 用另一种语言实现同样三步
3. 断言服务端读到的字段/类型/时间戳一致
4. 把这条脚本纳入 CI
```

## 14.11 与真实 MLflow 的差距

- 真实 `MlflowClient` 有更多 API、重试和 artifact 下载语义；
- 真实客户端支持云存储凭证、代理、TLS；
- 真实 fluent 的 active run 还处理环境变量、notebook、nested run 等；
- Mini 的 base64 artifact 协议不兼容真实 MLflow。

## 小结

- 客户端是协议适配器；业务规则必须在服务端。
- ContextVar 解决了线程/异步隔离，但跨进程必须显式恢复 run。
- 批处理降低往返成本；重试必须考虑幂等性。
- 契约测试是本地/远端一致性的唯一可靠保证。
- R/Java/其他语言最终都走 REST；版本偏斜与 artifact 协议要单独做兼容测试。

## 练习

1. 为 `log_batch` 设计批次 ID 去重表，说明存储和 GC。
2. 写一个线程池训练脚本，确保每个 worker 的 run 不串。
3. 设计一个“超时但可能成功”的对账流程。

---

下一章：[第 15 章 CLI 与 Web UI](15-cli-ui.md)
