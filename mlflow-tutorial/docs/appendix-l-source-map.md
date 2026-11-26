# 附录 L 真实 MLflow 源码地图与调试入口

> 配合第 35 章使用。路径按 MLflow 3.x 公开仓库整理；不同 minor 可能移动文件，阅读时以锁定版本为准。

## L.1 顶层目录

```text
mlflow/
  __init__.py
  tracking/
  store/
  models/
  pyfunc/
  server/
  cli/
  genai/
  projects/
  protos/
tests/
```

## L.2 关键文件速查

| 你想知道 | 去看 |
|---|---|
| fluent API 怎么实现 | `mlflow/tracking/fluent.py` |
| 显式客户端怎么做 | `mlflow/tracking/client.py` |
| URI 如何解析成 store | `mlflow/tracking/_tracking_service/` |
| 存储接口 | `mlflow/store/tracking/abstract_store.py` |
| 本地文件存储 | `mlflow/store/tracking/file_store.py` |
| SQL 存储 | `mlflow/store/tracking/sqlalchemy_store.py` |
| 注册表存储 | `mlflow/store/model_registry/` |
| artifact 抽象 | `mlflow/store/artifact/artifact_repository.py` |
| artifact registry | `mlflow/store/artifact/artifact_repository_registry.py` |
| 模型格式/保存加载 | `mlflow/models/model.py` |
| 签名 | `mlflow/models/signature.py` |
| PyFunc 契约 | `mlflow/pyfunc/__init__.py` |
| 评分服务 | `mlflow/pyfunc/scoring_server/` |
| 跟踪服务端 | `mlflow/server/`（`handlers.py` 是路由核心） |
| REST 协议 | `mlflow/protos/service.proto` |
| CLI | `mlflow/cli/` |
| GenAI | `mlflow/genai/` |
| 评测数据集 | `mlflow/genai/datasets/`、`mlflow/entities/evaluation_dataset.py` |
| 训练数据血缘 | `mlflow/data/` |
| LoggedModel 实体 | `mlflow/entities/logged_model.py` |
| LoggedModel 存储 | `mlflow/store/tracking/sqlalchemy_store.py` 的 `SqlLoggedModel` 相关方法 |
| LoggedModel 搜索 | `mlflow/utils/search_utils.py` / 版本对应的 `SearchLoggedModelsUtils` |
| Mini 的 LoggedModel | `mini_mlflow/entities.py`、`tracking.py`、`store/sqlite_store.py`、`search.py` |
| Mini 的 RAG 评测 | `mini_mlflow/rag.py`（BM25、recall/precision/MRR/nDCG、citation、grounding） |
| 插件入口 | 仓库 `pyproject.toml` / `setup.py` 的 entry points |

## L.3 一次请求的三条路径

### 本地文件

```text
fluent -> client -> FileStore -> ./mlruns/<exp>/<run>/
```

### 数据库直连

```text
fluent -> client -> SqlAlchemyStore -> SQLite/Postgres
```

### 远端 HTTP

```text
fluent -> client -> POST /api/2.0/mlflow/... -> server/handlers.py -> store
```

## L.4 插件 entry point 速查

| 类型 | group | 备注 |
|---|---|---|
| Tracking store | `mlflow.tracking_store` | entry point 名 = URI scheme |
| Artifact repository | `mlflow.artifact_repository` | 同上 |
| Model registry store | `mlflow.model_registry_store` | 同上 |
| Run context provider | `mlflow.run_context_provider` | 采集环境/用户/git |
| 认证/请求头 | 以版本文档为准 | 企业 SSO/网关注入 |

示例：

```toml
[project.entry-points."mlflow.tracking_store"]
memory = "my_pkg.store:MemoryStore"
```

安装后：`pip install -e .`，再使用 `memory://...`。

## L.5 调试命令

```bash
mlflow doctor
mlflow server --backend-store-uri sqlite:///mlflow.db \
  --default-artifact-root ./mlartifacts --host 127.0.0.1 --port 5000
mlflow db upgrade sqlite:///mlflow.db
mlflow models serve -m models:/churn@champion -p 5001
```

Python：

```python
from mlflow.tracking import MlflowClient
c = MlflowClient("http://127.0.0.1:5000")
print(c.get_run(run_id))
print(c.search_model_versions("name = 'churn'"))
print(c.get_model_version_by_alias("churn", "champion"))
```

## L.6 测试即文档

```bash
pytest tests/store/tracking/test_sqlalchemy_store.py -q
pytest tests/tracking/test_client.py -q -k metric
pytest --collect-only -q | grep -i alias
```

关注：

- fixture 如何造 store；
- 错误类型/错误码；
- 客户端/服务端是否成对测试；
- 删除/恢复/并发等边界。

## L.7 源码阅读顺序

1. `mlflow/__init__.py`（公开 API 面）
2. `tracking/fluent.py`（active run 与日志入口）
3. `tracking/client.py`（显式 API）
4. `store/tracking/abstract_store.py`（接口）
5. `store/tracking/sqlalchemy_store.py`（实现）
6. `server/handlers.py`（协议）
7. `models/model.py` + `pyfunc/`（模型）
8. `genai/`（Prompt/Trace/Eval）
9. webhooks（事件/签名/重试）
10. `protos/service.proto`（字段事实）
11. `tests/`（行为）

## L.8 Mini 对照

| 真实 | Mini |
|---|---|
| `tracking/fluent.py` | `fluent.py` |
| `tracking/client.py` | `client.py` |
| `store/tracking/sqlalchemy_store.py` | `store/sqlite_store.py` |
| `store/artifact/*` | `artifacts.py` |
| `models/model.py` | `models/model.py` |
| `server/handlers.py` | `server.py` |
| `genai/prompts/` | `prompts.py` |
| `genai/datasets/` | `datasets.py`（digest/schema/profile/合并 CAS） |
| 无原生监控表 | `monitoring.py`（基线/PSI/KS/卡方/窗口报告；教学扩展） |
| `genai/simulators/`、`genai/scorers/`、`genai/evaluation/` | `agentops.py`（会话/模拟/scorer/分诊/比较；教学子集） |
| `data/` | `run_inputs` + `log_input`（指针与摘要，不含记录） |
| `webhooks/` + tracking store | `webhooks.py`（outbox/签名/重试） |
| 3.9+ 内嵌 Gateway | `gateway.py`（教学控制面） |
| `pyfunc/scoring_server/` | `scoring.py` |
| 前端 | `ui.py` |

## L.8.1 Mini 的 LoggedModel 调试入口

| 你想知道 | 去看 |
|---|---|
| 实体与线格式 | `mini_mlflow/entities.py` 的 `LoggedModel` / `normalize_logged_model_status` |
| 状态机与创建 | `mini_mlflow/tracking.py` 的 `create_logged_model` / `finalize_logged_model` |
| 表与事务 | `mini_mlflow/store/schema.sql`、`store/sqlite_store.py` 的 `create_logged_model` |
| 搜索编译 | `mini_mlflow/search.py` 的 `compile_logged_model_filter` |
| REST/PATCH | `mini_mlflow/server.py` 的 `do_PATCH` 与 `logged-models` 路由 |
| `models:/<id>` | `mini_mlflow/models/__init__.py` 的 `_load_logged_model` |
| 测试 | `tests/test_logged_models.py` |
| 示例 | `examples/14_logged_models.py` |

## L.8.2 Mini 的评分协议调试入口

| 你想知道 | 去看 |
|---|---|
| 请求形状 → 记录 | `mini_mlflow/scoring.py` 的 `records_from_payload` |
| 端点与状态码 | 同文件的 `_ScoringHandler.do_GET` / `do_POST` |
| 鉴权与请求体上限 | `_authenticate`、`max_request_bytes` |
| 模型加载与签名 | `create_scoring_server` → `models.load_model` → `PyFuncModel.predict` |
| 协议契约测试 | `tests/test_serving_protocol.py` |
| 可运行矩阵 | `examples/15_serving_protocol.py` |

## L.8.3 Mini 的评测数据集调试入口

| 你想知道 | 去看 |
|---|---|
| 记录契约与校验 | `mini_mlflow/datasets.py` 的 `_clean_record` / `_clean_records` |
| digest 与 schema/profile | 同文件的 `_digest`、`_schema_and_profile` |
| 合并与并发 CAS | `merge_records` 的 retry 循环 + `store/sqlite_store.py` 的 `merge_dataset_records` |
| 表与索引 | `mini_mlflow/store/schema.sql` 的 `datasets` / `dataset_records` |
| REST 路由 | `mini_mlflow/server.py` 的 `datasets/*` |
| 客户端 | `mini_mlflow/client.py` 的 `create_dataset` / `merge_dataset_records` |
| CLI | `mini_mlflow/cli.py` 的 `datasets` 子命令 |
| 测试/示例 | `tests/test_datasets.py`、`examples/17_datasets.py` |

## L.8.4 Mini 的监控调试入口

| 你想知道 | 去看 |
|---|---|
| 基线 profile（分箱/样本/类别） | `mini_mlflow/monitoring.py` 的 `_numeric_profile` / `_categorical_profile` / `_build_baseline` |
| PSI/KS/卡方 | 同文件的 `_psi`、`_ks_two_sample`、`_chi2_sf` |
| 窗口评估与严重度 | `_evaluate` / `_evaluate_numeric` / `_evaluate_categorical` |
| 表与级联 | `mini_mlflow/store/schema.sql` 的 `monitors` / `monitor_windows` |
| REST 路由 | `mini_mlflow/server.py` 的 `monitors/*` |
| 客户端/CLI | `mini_mlflow/client.py` 的 `create_monitor`、`cli.py` 的 `monitoring` 子命令 |
| 统计与窗口测试 | `tests/test_monitoring.py` |
| 可运行示例 | `examples/18_monitoring.py` |

## L.8.5 Mini 的 AgentOps 调试入口

| 你想知道 | 去看 |
|---|---|
| 会话分组与统计 | `mini_mlflow/agentops.py` 的 `group_sessions` / `Session` |
| 脚本模拟与目标检查 | `ScriptedConversationSimulator.run` / `_expectations_met` |
| 会话 → 测试用例 | `generate_test_cases` |
| 五个确定性 scorer | `ConversationCompleteness` / `UserFrustration` / `ConversationalSafety` / `ToolSuccessRate` / `LatencyBudget` |
| CODE assessment 幂等 | `score_sessions` + `TrackingService.log_feedback` |
| 在线采样 | `ScorerSamplingConfig` / `_sampled` / `score_online` |
| 问题分诊与签名归一化 | `detect_issues` / `_normalise_signature` |
| 配对比较与门禁 | `compare_paired` / `assert_improved` |
| 会话 → 数据集记录 | `sessions_to_dataset_records` |
| 测试/示例 | `tests/test_agentops.py`、`examples/19_agent_ops.py` |

## L.8.6 Mini 的 RAG 评测调试入口

| 你想知道 | 去看 |
|---|---|
| 分词与复数折叠 | `mini_mlflow/rag.py` 的 `tokenize` / `_fold_plural` |
| BM25 打分与 tie-break | `BM25Index.search` |
| 元数据/ACL 过滤 | `BM25Index.search(metadata_filter=...)` |
| 四个检索指标 | `recall_at_k` / `precision_at_k` / `mrr_at_k` / `ndcg_at_k` |
| 聚合与逐 query 行 | `retrieval_report` |
| 引用提取与 F1 | `extract_citations` / `citation_metrics` |
| 词项 groundedness | `grounding_report` / `grounding_score` |
| 端到端示例 | `examples/20_genai_capstone.py` |
| 测试 | `tests/test_rag.py` |

## L.9 判断信息可信度

优先级从高到低：

1. 你锁定版本的源码与测试；
2. `protos/service.proto`；
3. 官方文档（注意版本前缀）；
4. release notes / migration guide；
5. 博客/教程（最容易过期）。

看到“MLflow 支持 X”时，先问：哪个版本？开源还是托管平台？API 在哪个命名空间？是否已弃用？

---

返回：[教程首页](../README.md)
