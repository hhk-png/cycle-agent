# 示例

所有示例都可以从教程根目录直接运行：

```bash
python examples/01_quickstart.py
```

默认写 `examples/runtime/`（SQLite + artifacts）；设置 `MINI_MLFLOW_TRACKING_URI` 可指向服务器：

```bash
MINI_MLFLOW_TRACKING_URI=http://127.0.0.1:5000 \
MINI_MLFLOW_TRACKING_TOKEN=secret \
python examples/01_quickstart.py
```

| 文件 | 演示 |
|---|---|
| 01_quickstart.py | 训练/评测/模型/注册/加载 |
| 02_search_and_artifacts.py | 搜索/数据集过滤/历史/artifact |
| 03_registry_and_aliases.py | 版本/别名 |
| 04_evaluate.py | 分类评测 |
| 05_tracing.py | Trace/Span/OTel |
| 06_project.py | MLproject 执行 |
| 07_remote_server.py | 进程内 HTTP 服务 |
| 08_serving.py | 评分服务 |
| 09_autolog.py | autolog |
| 10_end_to_end.py | 生命周期闭环 |
| 11_prompts_feedback.py | Prompt 版本/别名、trace feedback、assessment 搜索 |
| 12_gateway.py | 路由、回退、缓存、成本统计 |
| 13_webhooks.py | 签名 Webhook、outbox、投递验证 |
| 14_logged_models.py | LoggedModel 生命周期、搜索、`models:/<id>` 加载 |
| 15_serving_protocol.py | 评分协议矩阵：健康检查、四种输入形状、鉴权与错误码 |
| 16_capstone_churn.py | 数据血缘 → 训练 → 切片评测 → 注册 → 服务 → Trace/Feedback |
| 17_datasets.py | 评测数据集：创建、digest、合并、搜索、删除 |
| 18_monitoring.py | 监控基线、稳定/漂移窗口、PSI/KS 报告 |
| 19_agent_ops.py | 会话模拟、多轮评分、issue 分诊、配对比较、golden set |
| 20_genai_capstone.py | 知识助手：BM25、Prompt/Gateway/Trace、离线门禁、在线评分、漂移监控 |

`gateway.yaml` 是 CLI/服务端网关配置示例，不参与示例脚本自动验证。

`projects/churn/` 是 06 使用的 MLproject。

`deploy/` 是第 41 章的生产编排参考（Compose + Postgres + MinIO + Nginx）。教程 CI 只做静态检查，不会启动 Docker。`15` 对应第 42 章的评分协议，`16` 对应第 43 章的综合实战，`17` 对应第 44 章的评测数据集，`18` 对应第 45 章监控，`19` 对应第 46 章 AgentOps，`20` 对应第 47 章 GenAI 综合实战与附录 M。

验证：

```bash
python scripts/verify.py
```
