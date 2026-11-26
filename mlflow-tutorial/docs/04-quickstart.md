# 第 4 章 15 分钟快速上手：真实 MLflow 与 MiniMLflow 双轨

## 学习目标

- 在本地启动 MiniMLflow 服务器，完成一次带 artifact、模型注册和 UI 查看的闭环。
- 理解同一个概念在真实 MLflow 中大致如何写。
- 学会用 CLI/curl 验证“数据真的写进去了”。

## 4.1 安装

在本教程目录：

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

MiniMLflow 的运行时依赖只有 PyYAML；测试需要 pytest。

如果你想同时体验真实 MLflow，建议单独建虚拟环境，避免依赖冲突：

```bash
python -m venv .venv-mlflow
# Windows: .venv-mlflow\Scripts\activate
# macOS/Linux: source .venv-mlflow/bin/activate
python -m pip install "mlflow>=3,<4"
mlflow --version
```

本教程不把真实 MLflow 作为测试依赖；所有验证都针对 MiniMLflow。

## 4.2 启动 MiniMLflow

```bash
python -m mini_mlflow server \
  --backend-store-uri sqlite:///mlflow.db \
  --artifact-root ./mlartifacts \
  --host 127.0.0.1 \
  --port 5000
```

看到：

```text
MiniMLflow listening on http://127.0.0.1:5000
Backend: sqlite:///...
Artifacts: ...
```

打开 <http://127.0.0.1:5000/ui>，此时只有 Default 实验。

> 生产/团队环境至少加 `--auth-token`，并通过反向代理启用 TLS。见第 20 章。

## 4.3 第一次训练与记录

另开终端，在教程根目录执行：

```python
# 与 examples/01_quickstart.py 等价的 fluent 写法（示例 01 本身用 MiniClient 展示显式 API）
import mini_mlflow as mlflow
from mini_mlflow.models import LinearRegression, infer_signature
from mini_mlflow.evaluate import evaluate_model

mlflow.set_tracking_uri("http://127.0.0.1:5000")
experiment = mlflow.get_experiment_by_name("quickstart") or mlflow.create_experiment("quickstart")

X = [{"floor": 2.0}, {"floor": 5.0}, {"floor": 8.0}, {"floor": 11.0}]
y = [2.1, 3.9, 6.2, 8.1]

with mlflow.start_run(experiment_id=experiment.experiment_id, run_name="linear-baseline") as run:
    model = LinearRegression(alpha=0.01).fit(X, y)
    predictions = model.predict(X)
    mlflow.log_params({"model": "linear", "alpha": 0.01, "features": "floor"})
    mlflow.log_metric("train_rmse", (sum((p - t) ** 2 for p, t in zip(predictions, y)) / len(y)) ** 0.5)
    mlflow.log_text("first run", "notes/readme.md")
    info = mlflow.log_model(
        model,
        artifact_path="model",
        registered_model_name="house-price-linear",
        input_example=[{"floor": 6.0}],
        signature=infer_signature(X, predictions),
    )
    evaluation = evaluate_model(
        model, X, y, task="regression",
        client=run.client, run_id=run.info.run_id,
        prefix="eval",
    )
    print("model_uri:", info.model_uri)
    print("eval:", evaluation.metrics)
```

运行：

```bash
python examples/01_quickstart.py
```

> 如果使用脚本里的默认本地 SQLite（不设置 remote URI），它会写 `examples/runtime/mlflow.db`，artifact 在 `examples/runtime/artifacts/`。整个 `examples/runtime/` 可以删除重建；也可以设置 `MINI_MLFLOW_TRACKING_URI` 指定服务器。

查看 UI：Experiments 页面应出现 `quickstart`，Run 列表出现 `linear-baseline`，详情页包含参数、指标历史、artifact 树。

## 4.4 用 CLI 验证

```bash
python -m mini_mlflow experiments list --tracking-uri http://127.0.0.1:5000
python -m mini_mlflow runs list --experiment-id 1 --tracking-uri http://127.0.0.1:5000
python -m mini_mlflow registry list --tracking-uri http://127.0.0.1:5000
```

如果服务器启用了 token：

```bash
export MINI_MLFLOW_AUTH_TOKEN=secret
python -m mini_mlflow runs list --tracking-uri http://127.0.0.1:5000
```

`MiniClient` 会从 `MINI_MLFLOW_TRACKING_TOKEN` 或 `MINI_MLFLOW_AUTH_TOKEN` 读取 token。

## 4.5 用 curl 直接看协议

```bash
curl -s http://127.0.0.1:5000/api/2.0/mlflow/experiments/get?experiment_id=1 | python -m json.tool
```

创建实验：

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/experiments/create \
  -H 'Content-Type: application/json' \
  -d '{"name":"curl-demo","tags":[{"key":"source","value":"curl"}]}'
```

在真实 MLflow 中，路径同样是 `/api/2.0/mlflow/...`，但响应字段更丰富，鉴权、错误码和 artifact 协议也不同。Mini 的 JSON 是“同构而非逐字节兼容”。

## 4.6 真实 MLflow 对照

```python
import numpy as np
import pandas as pd
import mlflow
import mlflow.sklearn
from sklearn.linear_model import LinearRegression
from mlflow.models import infer_signature

X = pd.DataFrame({"floor": [2.0, 5.0, 8.0, 11.0]})
y = np.array([2.1, 3.9, 6.2, 8.1])

mlflow.set_tracking_uri("http://127.0.0.1:5000")
mlflow.set_experiment("quickstart")

with mlflow.start_run(run_name="sklearn-baseline"):
    model = LinearRegression().fit(X, y)
    pred = model.predict(X)
    mlflow.log_param("fit_intercept", True)
    mlflow.log_metric("train_rmse", float(((pred - y) ** 2).mean() ** 0.5))
    signature = infer_signature(X, pred)
    mlflow.sklearn.log_model(
        model,
        name="house-price-sklearn",   # 2.x 用 artifact_path="model"
        signature=signature,
        input_example=X.head(3),
        registered_model_name="house-price-sklearn",
    )
```

`X.head(3)` 只适用于 pandas DataFrame；上面的 `X` 已显式构造为 DataFrame。若用 numpy 数组，应传 `X[:3]`。

启动真实服务器（命令随版本可能变化）：

```bash
mlflow server \
  --backend-store-uri sqlite:///mlflow.db \
  --artifacts-destination ./mlartifacts \
  --host 127.0.0.1 --port 5000
```

关键差异：

| 主题 | 真实 MLflow | MiniMLflow |
|---|---|---|
| 依赖 | FastAPI/uvicorn（旧版 Flask/gunicorn）、SQLAlchemy、alembic 等 | 标准库 + PyYAML |
| flavor | sklearn/xgboost/torch/transformers... | 仅内置 linear 教学 flavor |
| 自动日志 | `mlflow.autolog()` 支持大量框架 | 只 patch 自己的 LinearRegression |
| Registry | 完整 REST/UI/审批集成 | 核心版本/别名/阶段 |
| Artifact | S3/GCS/Azure、分片上传 | 本地文件、base64 JSON |
| Tracing | 自动埋点、采样、OTel | 手动 API + OTel 导出 |
| 迁移 | 官方升级路径 | 不承诺跨版本迁移 |

## 4.7 载入模型并预测

本地/远程统一：

```python
import mini_mlflow as mlflow

model = mlflow.load_model("models:/house-price-linear@champion")
# 或 mlflow.load_model("models:/house-price-linear/1")
# 或 mlflow.load_model("runs:/<run_id>/model")
print(model.predict([{"floor": 7.0}]))
```

注意：

- `models:/...@alias` 需要 Registry 中已存在别名；
- `runs:/` 需要能访问对应的 artifact（本地目录或远端服务）；
- 加载远程模型会先下载到临时目录，再反序列化为内存对象。

## 4.8 常见首次失败

| 症状 | 原因 | 处理 |
|---|---|---|
| UI 打不开 | 端口被占 / host 绑定错 | 换端口，检查 `--host` |
| 401 | 设置了 auth token | 客户端带 Bearer token |
| 模型加载 404 | run/alias 不存在或 artifact 被删 | `registry versions` 查 source |
| 指标查不到 | 写到了另一个数据库/服务器 | 检查 `tracking_uri` 与 env |
| 搜索语法错误 | DSL 不支持 SQL 函数 | 看附录 D 的语法子集 |
| 上传 413 | 请求体超过 64MB | 调大限制或改用对象存储 |

## 4.9 与真实 MLflow 的差距

Mini 的“成功”只证明控制面能闭环，不代表生产可用。真实环境还要处理：

- 数据库连接池与迁移；
- 对象存储权限、分片、断点续传；
- 多租户与审计；
- 高并发日志写入；
- UI 的对比图和模型审批。

## 小结

- 启动服务器、跑一个 run、在 UI 和 CLI 里验证，是建立直觉的最快方式。
- 元数据、artifact、模型版本必须一起验证，否则容易“看起来成功”。
- 真实 MLflow 的概念一致，但实现细节和生态范围大得多。

## 练习

1. 把示例改成三个不同 `alpha` 的 run，用搜索找出验证误差最小的。
2. 给模型设置别名 `champion`，再用 `models:/...@champion` 加载。
3. 停止服务器，直接打开 SQLite 查看 `runs`、`metrics`、`model_versions` 三张表，解释每一行的来源。

---

下一章：[第 5 章 工程蓝图](05-mini-blueprint.md)
