# 附录 C 配置与环境变量

## C.1 MiniConfig 字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `backend_store_uri` | `sqlite:///mlflow.db` | 仅 SQLite 文件；不支持 `:memory:` |
| `artifact_root` | `./mlartifacts` | 仅本地/file URI |
| `host` | `127.0.0.1` | 服务绑定 |
| `port` | `5000` | 服务端口，0=随机 |
| `auth_token` | `None` | 共享 Bearer token |
| `max_request_bytes` | 64 MiB | 请求体上限 |
| `sqlite_busy_timeout_ms` | 5000 | 锁等待 |
| `search_max_results` | 1000 | 单次搜索上限 |
| `trace_max_payload_bytes` | 256 KiB | 服务端接受的单个 trace 字段上限；tracing 模块另有同名环境变量用于客户端截断 |
| `cors_origin` | `None` | 允许的浏览器源 |

## C.2 环境变量

| 变量 | 作用 |
|---|---|
| `MINI_MLFLOW_BACKEND_STORE_URI` | 服务端 SQLite 路径 |
| `MINI_MLFLOW_ARTIFACT_ROOT` | artifact 根 |
| `MINI_MLFLOW_TRACKING_URI` | 客户端目标（http/https/sqlite） |
| `MINI_MLFLOW_HOST` | 服务 host |
| `MINI_MLFLOW_PORT` | 服务 port |
| `MINI_MLFLOW_AUTH_TOKEN` | 服务端 token（客户端也会读取作为回退） |
| `MINI_MLFLOW_TRACKING_TOKEN` | 客户端 token（优先于 AUTH_TOKEN） |
| `MINI_MLFLOW_MAX_REQUEST_BYTES` | 请求体上限 |
| `MINI_MLFLOW_SQLITE_BUSY_TIMEOUT_MS` | SQLite 锁等待 |
| `MINI_MLFLOW_SEARCH_MAX_RESULTS` | 搜索上限 |
| `MINI_MLFLOW_TRACE_MAX_PAYLOAD_BYTES` | trace 字段上限 |
| `MINI_MLFLOW_CORS_ORIGIN` | CORS 允许源 |
| `MINI_MLFLOW_USER` | 记录到 `mlflow.user` |
| `MINI_MLFLOW_EXPERIMENT_ID` | fluent 默认实验 |
| `MINI_MLFLOW_RUN_ID` | 项目子进程恢复 run |
| `MINI_MLFLOW_WEBHOOK_TIMEOUT_S` | webhook 单次 HTTP 超时（默认 5） |
| `MINI_MLFLOW_WEBHOOK_MAX_ATTEMPTS` | 最大投递次数（默认 4） |
| `MINI_MLFLOW_WEBHOOK_RETRY_BACKOFF_S` | 首次退避秒数（默认 1，之后指数增长） |
| `MINI_MLFLOW_DEMO_SUFFIX` | 仅示例/验证脚本使用：给实验名、run 名加后缀，避免重复运行冲突 |

兼容回退：

- `MLFLOW_BACKEND_STORE_URI`
- `MLFLOW_ARTIFACT_ROOT`
- `MLFLOW_TRACKING_URI`
- Windows 用户名 `USERNAME` / POSIX `USER`

## C.3 优先级

```text
显式函数/CLI 参数
  > MINI_MLFLOW_*
  > MLFLOW_*
  > 默认
```

客户端：

```text
显式 tracking_uri
  > MINI_MLFLOW_TRACKING_URI
  > MLFLOW_TRACKING_URI
  > 本地默认
```

token：

```text
显式 auth_token
  > MINI_MLFLOW_TRACKING_TOKEN
  > MINI_MLFLOW_AUTH_TOKEN
```

## C.4 YAML 文件

```yaml
backend_store_uri: sqlite:///mlflow.db
artifact_root: ./mlartifacts
host: 127.0.0.1
port: 5000
auth_token: null
max_request_bytes: 67108864
sqlite_busy_timeout_ms: 5000
search_max_results: 1000
trace_max_payload_bytes: 262144
cors_origin: null
```

未知键报错；相对路径按**进程当前目录**解析为绝对路径。

## C.5 CLI 示例

```bash
python -m mini_mlflow server \
  --backend-store-uri sqlite:///mlflow.db \
  --artifact-root ./mlartifacts \
  --host 0.0.0.0 \
  --port 5000 \
  --auth-token "$MINI_MLFLOW_AUTH_TOKEN"
```

```bash
MINI_MLFLOW_TRACKING_URI=http://localhost:5000 \
MINI_MLFLOW_TRACKING_TOKEN=secret \
python -m mini_mlflow runs list
```

## C.6 安全建议

- 不要把 `auth_token` 写进版本库；
- 生产使用 secret manager；
- 反向代理终止 TLS，或使用 `--tls-cert/--tls-key`；
- 限制 CORS 源；
- 不要把服务器直接暴露公网；
- 定期轮换 token。

## C.7 Gateway 配置

Gateway 与 tracking 配置分离，通过 `--gateway-config gateway.yaml` 启用：

```yaml
providers:
  fast:
    kind: mock              # mock | callable
    model: mock-fast
    cost_per_1k_input: 0.15
    cost_per_1k_output: 0.60
    timeout_ms: 5000
    max_output_tokens: 512
    metadata:
      api_key_env: OPENAI_API_KEY   # 只存变量名
routes:
  support:
    primary: fast
    fallbacks: [strong]
    rate_limit_rpm: 120
    cache_ttl_s: 30
```

约束：

- `kind=callable` 的 provider 必须在代码里用 `CallableProvider` 注册；
- 路由引用的 provider 必须存在；
- 成本必须 >= 0；`rate_limit_rpm` 必须是正整数或 `null`（0 会被拒绝），`cache_ttl_s` 必须为非负整数；
- 未知键直接报错，避免拼写错误被静默忽略；
- 配置文件不应包含明文密钥。

CLI：

```bash
python -m mini_mlflow gateway validate --config examples/gateway.yaml
python -m mini_mlflow gateway invoke --config examples/gateway.yaml \
  --route support --message "hello"
```

## C.7.1 Webhook 配置

Webhook 没有单独的 YAML 文件；订阅通过 API/CLI 写入数据库。投递参数用环境变量：

```bash
export MINI_MLFLOW_WEBHOOK_TIMEOUT_S=5
export MINI_MLFLOW_WEBHOOK_MAX_ATTEMPTS=4
export MINI_MLFLOW_WEBHOOK_RETRY_BACKOFF_S=1
```

约束：

- 只允许 `http`/`https` URL，禁止 URL 内嵌凭据与 fragment；
- 事件必须是 `webhooks.EVENT_NAMES` 中的 `entity.action`；
- secret 存在数据库中，API 默认不返回；
- 单进程投递；`serve` 启动后台 dispatcher，测试用 `drain_once()`。

## C.7.2 评测数据集上限

数据集校验使用代码常量而非环境变量（避免部署时被悄悄放大）：

| 限制 | 值 | 位置 |
|---|---|---|
| 单条记录 | 1 MiB | `mini_mlflow/datasets.py` 的 `_MAX_RECORD_BYTES` |
| 单次 create/merge | 10,000 条 | `_MAX_RECORDS_PER_MERGE` |
| 单个数据集总记录 | 50,000 条 | `_MAX_TOTAL_RECORDS` |
| 单个数据集总字节 | 16 MiB | `_MAX_TOTAL_BYTES` |
| source | 4 KiB | `_MAX_SOURCE_BYTES` |

如需按团队调整，建议改成 `MiniConfig` 字段并写入附录/测试；直接改常量会让已部署实例行为不一致。

## C.8 与真实 MLflow 的差异

真实 MLflow 支持：

- `MLFLOW_TRACKING_USERNAME/PASSWORD`；
- `MLFLOW_TRACKING_INSECURE_TLS`；
- 云存储凭证链；
- profile/配置文件；
- 更多后端 URI；
- webhook secret 加密（`MLFLOW_WEBHOOK_SECRET_ENCRYPTION_KEY`）、可配置 worker 数/重试次数。

Mini 只实现上述子集。

---

返回：[教程首页](../README.md) · [附录 D](appendix-d-search-syntax.md)
