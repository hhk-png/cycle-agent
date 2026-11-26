# 附录 K MLflow 3.x 迁移与 API 对照

> 本附录是第 34 章的速查表。版本以 MLflow 3.x 的公开文档为准；不同 minor 的细节会变化，升级前核对 release notes。Mini 的 LoggedModel 参考实现见第 40 章。

## K.1 实体对照

| 2.x | 3.x | 说明 |
|---|---|---|
| Run 的 artifact 目录中的模型 | LoggedModel | 模型成为一等实体，有 `model_id` |
| `ModelVersion.source = runs:/...` | `ModelVersion.model_id` + source | 版本可反查 LoggedModel |
| `Staging/Production/Archived` | alias（如 `champion`/`challenger`） | stage 弃用 |
| `mlflow.evaluate`（顶层） | `mlflow.models.evaluate` / `mlflow.genai.evaluate` | 通用 vs GenAI |
| Trace（实验性） | Trace + Assessment（GA 方向） | feedback/expectation 一等化 |
| 无 Prompt 实体 | PromptVersion + alias | `mlflow.genai` 命名空间 |
| 独立 deployments server | 移除 | 用 `mlflow models serve`；3.9+ AI Gateway 内嵌在 tracking server |
| Mini 旧版：只有 `runs:/` 模型 | Mini 当前：`log_model(name=...)` + `models:/m-...`，同时保留 `runs:/` | 四张表、三状态、params/metrics/tags 与本地加载链（第 40 章） |

## K.2 URI 对照

| 用途 | 2.x 常见写法 | 3.x 推荐写法 |
|---|---|---|
| 加载 run 中的模型 | `runs:/<run_id>/model` | `info.model_uri` 或 `models:/<model_id>` |
| 加载注册版本 | `models:/churn/3` | `models:/churn/3`（仍有效） |
| 部署读取 | `models:/churn/Production` | `models:/churn@champion` |
| 最新版本 | `models:/churn/latest` | 不推荐用于部署；显式 alias |
| Prompt | 无 | `prompts:/support/3` 或 `prompts:/support@production` |

## K.3 API 对照

| 任务 | 2.x | 3.x |
|---|---|---|
| 记录模型 | `log_model(model, artifact_path="model")` | `log_model(model, name="model")` |
| 注册模型 | `register_model(runs:/..., "name")` | `register_model(info.model_uri, "name")` |
| 发布 | `transition_model_version_stage(..., "Production")` | `set_registered_model_alias(..., "champion", v)` |
| 解析发布版本 | `get_latest_versions("name", stages=["Production"])` | `get_model_version_by_alias("name", "champion")` |
| 列出模型 | 扫 run artifact | `search_logged_models` / Logged Models UI |
| 评测 | `mlflow.evaluate(...)` | `mlflow.models.evaluate(...)` / `mlflow.genai.evaluate(...)` |
| 评测门禁 | `baseline_model` 等参数 | `validate_evaluation_results` |
| Prompt | 无 | `mlflow.genai.register_prompt/load_prompt` |
| 反馈 | 自定义 tag | `mlflow.log_feedback` / assessment |

## K.4 CLI 对照

| 任务 | 2.x | 3.x |
|---|---|---|
| 启动跟踪服务 | `mlflow server ...` | `mlflow server ...`（参数有增减） |
| 启动网关 | `mlflow gateway start --config-path ...` | 独立 server 已移除；3.9+ 随 `mlflow server` 启动，在 UI/API 配置 endpoint |
| 启动 deployments | `mlflow deployments start-server ...` | 已移除 |
| 评分服务 | `mlflow models serve -m ...` | `mlflow models serve -m ...`（FastAPI/uvicorn） |
| 数据库迁移 | `mlflow db upgrade <uri>` | `mlflow db upgrade <uri>` |
| 环境诊断 | `mlflow doctor` | `mlflow doctor` |

## K.5 已移除/弃用的高频项

- Recipes（原 Pipelines）；
- fastai、mleap、diviner、gluon 等 flavor；
- `run_uuid`（用 `run_id`）；
- `signature_dict`（用 `signature`）；
- `mlflow.gitBranchName` / `mlflow.gitRepoURL` 自动 tag；
- `example_no_conversion`、`code_path`、部分 `requirements_file`/`inference_config` 参数；
- `baseline_model` 评测参数（用 `validate_evaluation_results`）；
- `higher_is_better`（用 `greater_is_better`）；
- `custom_metrics`（用 evaluator/scorer 机制）；
- MLServer 后端（3.13 起移除）；
- gateway `routes`/`route_type` 配置键与 deployments server。

## K.6 序列化默认值变化

| Flavor | 3.x 默认方向 | 迁移注意 |
|---|---|---|
| sklearn / lightgbm | skops | 加载端依赖与信任边界 |
| PyTorch | torch.export | 动态图/自定义算子兼容性 |
| XGBoost | UBJSON | 旧模型加载回归 |
| 其他 | 以版本文档为准 | 固定版本 + golden inputs |

迁移时不要只看“训练能跑”，要验证“旧模型能在新运行时加载并给出相同输出”。

## K.7 配置与环境变量

| 变量/配置 | 说明 |
|---|---|
| `MLFLOW_TRACKING_URI` | 客户端连接地址 |
| `MLFLOW_TRACKING_TOKEN` | 认证 token（视部署） |
| `MLFLOW_TRACKING_USERNAME/PASSWORD` | basic auth（视部署） |
| `MLFLOW_ARTIFACT_ROOT` | 默认 artifact 位置（服务端/客户端语义不同） |
| `MLFLOW_S3_ENDPOINT_URL` | S3 兼容存储 |
| `MLFLOW_DEPLOYMENTS_TARGET` | 旧 deployments 目标（随移除失效） |

Mini 的对应变量是 `MINI_MLFLOW_*`，并接受部分 `MLFLOW_*` 作为回退；两者不要混用。

## K.8 升级检查表

- [ ] 固定客户端/服务端版本，确认兼容矩阵；
- [ ] 备份数据库、artifact、配置、密钥引用；
- [ ] 扫描 `artifact_path=`、`runs:/`、stage、`mlflow.evaluate`、gateway 配置；
- [ ] 替换为 `name=`、`info.model_uri`、alias、明确评测命名空间；
- [ ] 模型加载回归（所有生产 flavor）；
- [ ] 评分服务镜像/参数适配 FastAPI/uvicorn；
- [ ] 迁移脚本在 staging 演练并计时；
- [ ] 监控能区分 2.x/3.x 客户端错误；
- [ ] 回滚包与回滚演练完成；
- [ ] 通知使用方并记录升级窗口。

## K.9 3.9–3.18 新增的配置与 API（速查）

| 能力 | 版本 | 入口/开关 | 迁移注意 |
|---|---|---|---|
| 多工作区 | 3.10 | `mlflow server --enable-workspaces` / `MLFLOW_ENABLE_WORKSPACES` | 请求可带 `X-MLFLOW-WORKSPACE`；资源可 `mlflow db move-resources` |
| RBAC 角色 | 3.13 | Admin UI + `/mlflow/users/permissions/*` | 旧逐资源权限 API 移除；`default_permission` 语义变化 |
| 子资源 + DENY | 3.17 | `add_role_permission(..., "DENY")` | DENY 覆盖正向 grant；子资源 tier 只接受 `*` |
| Gateway 内嵌 | 3.9 | `mlflow server` + UI/API | 不再需要独立进程；注意资源竞争与密钥边界 |
| Gateway 预算 | 3.11 | 预算策略 + Redis BudgetTracker | 多副本必须用共享状态，否则额度按副本放大 |
| Guardrail | 3.12 | Gateway endpoint 配置 | pre/post-LLM；执行产生 trace span |
| MCP Registry | 3.15 | `mlflow.genai.register_mcp_server` 等 | 实验功能；版本/别名/工具快照；供应链风险 |
| Review Queue | 3.14 | `mlflow.genai.review_queues` | 答案写入 assessment/expectation |
| pytest 回归 | 3.14 | `@mlflow.test` | 用 `result.passed/reason` 断言 |
| Trace 自定义视图 | 3.16 | experiment tag `mlflow.customView.view.v1.*` | 视图是配置，需版本化与权限控制 |
| 模型代码执行开关 | 3.18 | `MLFLOW_ALLOW_MODEL_CODE_EXECUTION=false` | 与 pickle 开关分开治理 |
| presigned artifact | 3.18 | presigned-only 模式 | 有效期、对象键绑定、签发审计 |
| 数据库升级 | 3.18 | `mlflow db upgrade` | 不支持混合版本；先备份、停写、演练 |

## K.10 一句话总结

**3.x 把“模型”从 run 的附件变成了有身份证的实体；3.9 之后又把“平台治理”变成了产品的一部分。迁移动作要同时覆盖数据模型与运维边界。**

---

返回：[教程首页](../README.md) · [附录 L 真实 MLflow 源码地图与调试入口](appendix-l-source-map.md)
