# 第 6 章 配置、错误与数据实体

## 学习目标

- 理解配置优先级、路径解析和“构造时失败”的原则。
- 设计稳定的错误码，而不是把异常文本当协议。
- 理解实体对象的内部表示与线格式（wire format）为什么可以不同。
- 掌握 Windows 路径、时间戳、ID 等跨平台细节。

## 6.1 配置优先级

MiniMLflow 的配置来源：

```text
显式参数（代码/CLI） > MINI_MLFLOW_* 环境变量 > MLFLOW_* 兼容变量 > 默认值
```

真实 MLflow 还有配置文件与 profile；Mini 只实现 YAML 文件 + 环境变量，避免隐藏状态。

```python
from mini_mlflow.config import MiniConfig

config = MiniConfig(
    backend_store_uri="sqlite:///mlflow.db",
    artifact_root="./mlartifacts",
    host="127.0.0.1",
    port=5000,
)
print(config.backend_path)     # 绝对路径
print(config.tracking_uri)     # sqlite:///C:/...（Windows）
```

关键设计：

1. **构造时解析并校验**。`__post_init__` 会调用 `_sqlite_path` 与 `_artifact_path`，非法的 backend scheme 或 S3 artifact 路径会立刻抛出 `InvalidParameter`，而不是在第一个请求时 500。
2. **相对路径立即转绝对路径**。服务器进程之后 `chdir` 也不会写错位置。
3. **不做隐式网络访问**。配置解析阶段不连接数据库、不创建 bucket。
4. **未知配置键报错**。拼错的 `artifcat_root` 不会被静默忽略。

### 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `MINI_MLFLOW_BACKEND_STORE_URI` | SQLite URI/路径 | `sqlite:///mlflow.db` |
| `MINI_MLFLOW_ARTIFACT_ROOT` | artifact 根目录 | `./mlartifacts` |
| `MINI_MLFLOW_TRACKING_URI` | 客户端目标（HTTP 或 SQLite） | 本地 |
| `MINI_MLFLOW_HOST` / `MINI_MLFLOW_PORT` | 服务绑定 | 127.0.0.1:5000 |
| `MINI_MLFLOW_AUTH_TOKEN` | 服务端共享 token | 空 |
| `MINI_MLFLOW_TRACKING_TOKEN` | 客户端 token | 空 |
| `MINI_MLFLOW_MAX_REQUEST_BYTES` | 请求体上限 | 64 MiB |
| `MINI_MLFLOW_SEARCH_MAX_RESULTS` | 搜索上限 | 1000 |
| `MINI_MLFLOW_TRACE_MAX_PAYLOAD_BYTES` | Trace 单字段上限 | 256 KiB |
| `MINI_MLFLOW_CORS_ORIGIN` | 允许的浏览器源 | 空 |

完整列表见附录 C。

### Windows 特例

SQLite URI `sqlite:///C:/data/mlflow.db` 与 `file:///C:/data/a.txt` 在 Windows 上容易被解析成 `/C:/...`。`file_uri_to_path` 会去掉盘符前的斜杠；`path_to_file_uri` 依赖 `Path.as_uri()` 生成标准形式。测试覆盖 URI 往返。

### 配置文件

```yaml
# mini-mlflow.yaml
backend_store_uri: sqlite:///mlflow.db
artifact_root: ./mlartifacts
host: 127.0.0.1
port: 5000
auth_token: null
```

```bash
python -m mini_mlflow server --config mini-mlflow.yaml
```

不要把生产 token 提交到配置文件；用环境变量或 secret manager 注入。

## 6.2 错误模型

错误应满足三个条件：

1. 调用方能稳定分支；
2. 人类能看懂；
3. 不泄漏内部路径/堆栈。

MiniMLflow 的基类：

```python
class MiniMlflowError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500

    def to_dict(self):
        return {"error_code": self.code, "message": self.message}
```

常见错误码：

| 错误码 | HTTP | 场景 |
|---|---|---|
| `INVALID_PARAMETER_VALUE` | 400 | 缺字段、类型错、参数冲突 |
| `BAD_REQUEST` | 400 | JSON/协议错误 |
| `RESOURCE_DOES_NOT_EXIST` | 404 | 实验/run/模型版本不存在 |
| `RESOURCE_ALREADY_EXISTS` | 400 | 重名 |
| `PERMISSION_DENIED` | 401/403 | 鉴权失败 |
| `REQUEST_TOO_LARGE` | 413 | 超过请求体上限 |
| `RESOURCE_CONFLICT` | 409 | 状态冲突 |
| `PROJECT_EXECUTION_FAILED` | 400 | 项目子进程失败 |
| `MODEL_LOAD_FAILED` | 400 | MLmodel 不完整/反序列化失败 |
| `UNSUPPORTED_MODEL_FLAVOR` | 400 | 未支持的 flavor/loader |

反例：

```python
raise Exception("sqlite3.IntegrityError: UNIQUE constraint failed: runs.run_id")
```

这种错误会让客户端无法区分“重复创建”和“数据库挂了”，还可能泄漏表名。

### 异常 → HTTP 的映射

服务器捕获 `MiniMlflowError` 并序列化 `to_dict()`；未知异常记录堆栈后返回通用 500，不把堆栈发给客户端。客户端再把 `error_code` 映射回 Python 异常类，保证本地/远端体验一致。

## 6.3 实体与线格式

领域对象用 `dataclass`，内部方便属性访问：

```python
@dataclass
class RunInfo:
    run_id: str
    experiment_id: str
    run_name: str = ""
    status: str = "RUNNING"
    start_time: int = ...
    end_time: int | None = None
    artifact_uri: str = ""
    lifecycle_stage: str = "active"
```

线格式向真实 MLflow 靠拢：

```json
{
  "info": {"run_id": "...", "experiment_id": "1", "status": "FINISHED"},
  "data": {
    "metrics": [{"key": "auc", "value": 0.91, "timestamp": 0, "step": 0}],
    "params": [{"key": "alpha", "value": "0.1"}],
    "tags": [{"key": "team", "value": "nlp"}]
  }
}
```

为什么内部用 dict、线上用 list？

- 内部按键查找是 O(1)，适合校验和更新；
- 线上列表保持顺序、允许重复键的扩展（真实 API 历史原因），也便于跨语言；
- `from_dict` 同时接受 mapping 和 list，减少适配代码。

### 时间与 ID

- 时间戳统一用 UTC 毫秒（`now_ms`）；Trace 的 span 使用纳秒，因为要测量亚毫秒调用。
- run_id / trace_id 用 32 位十六进制（`os.urandom(16).hex()`），span_id 用 16 位。
- 实验 ID 用自增整数再序列化为字符串；模型版本同理。这样人类可读，又保持 API 字符型字段。
- `is_id` 只做格式校验；不要用 UUID 版本位假设。

### 参数/标签/指标的规范化

| 类型 | 规范化 | 限制 |
|---|---|---|
| Param | 标量转字符串；`None`/结构体拒绝 | 6000 字符 |
| Tag | 标量/结构转字符串或 JSON | 5000 字符 |
| Metric | 转有限 float；拒绝 bool/NaN/Inf | step>=0 |
| 名称 | 去首尾空白，拒绝控制字符 | 250 字符 |

注意：`coerce_metric(True)` 必须拒绝，否则布尔值会被静默当成 1.0，污染指标。

## 6.4 从 JSON 到实体：容错边界

REST 输入不可信。Mini 的处理原则：

1. HTTP 层只检查 body 是对象、字段存在；
2. 实体 `from_dict` 容错（缺字段用默认），但核心 ID 缺失会抛 `KeyError`；
3. 服务层做语义校验（例如状态值、时间戳）；
4. 已知的参数错误由 `MiniMlflowError`（如 `InvalidParameter`）映射为 4xx；`Trace.from_dict` 在 HTTP 边界单独兜底 `TypeError/ValueError/KeyError`。其他实体的解析异常目前仍可能落到通用 500，这是需要逐边界补齐的债务。

一个真实 bug 是：`Trace.from_dict` 缺字段导致 500。修复方式不是让 `from_dict` 永远不报错，而是在 HTTP 边界把解析错误转成客户端错误，同时保留内部严格性。新加实体时应该复用同一边界包装，而不是依赖全局异常兜底。

## 6.5 配置注入与测试

服务不读全局单例：

```python
service = TrackingService(MiniConfig(
    backend_store_uri=f"sqlite:///{tmp_path}/db.sqlite",
    artifact_root=str(tmp_path / "artifacts"),
))
```

测试因此可以并行、隔离，不需要 monkeypatch 全局数据库。只有 fluent API 使用进程级 client，这是为了模仿真实使用体验；测试会显式重置。

## 6.6 与真实 MLflow 的差距

- 真实配置支持 profile、`MLFLOW_TRACKING_USERNAME/PASSWORD`、云凭证链、TLS 证书等。
- 真实错误码更多，且 REST 字段有版本差异。
- 真实实体会包含更多字段（dataset inputs、tags 的 run link、model version metadata）。
- 真实 MLflow 的时间语义依后端，可能使用数据库时间；Mini 使用应用时间。

## 小结

- 配置必须尽早失败，路径必须尽早绝对化。
- 稳定错误码是协议的一部分；异常文本不是。
- 实体内部表示与线格式可以不同，但转换必须有测试。
- 时间、ID、数值规范化是跨平台正确性的基础。

## 练习

1. 给配置增加 `MAX_ARTIFACT_BYTES`，说明它应该在哪一层校验。
2. 设计一个 `RESOURCE_LOCKED` 错误码的使用场景，并写出客户端处理逻辑。
3. 为什么 `from_dict` 不应静默吞掉未知字段？给出两种策略的利弊。

---

下一章：[第 7 章 SQLite 存储层](07-sqlite-store.md)
