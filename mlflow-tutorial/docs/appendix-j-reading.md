# 附录 J 延伸阅读

## 官方与标准

- MLflow 文档：<https://mlflow.org/docs/latest/>
- MLflow 版本归档与 release notes：<https://mlflow.org/releases/archive>
- MLflow 3 迁移指南：<https://mlflow.org/docs/latest/ml/mlflow-3/>
- MLflow 3 breaking changes：<https://mlflow.org/docs/latest/genai/mlflow-3/breaking-changes/>
- MLflow GenAI 文档：<https://mlflow.org/docs/latest/genai/>
- MLflow Prompt Registry：<https://mlflow.org/docs/latest/genai/prompt-registry/create-and-edit-prompts/>
- MLflow GenAI Evaluation：<https://mlflow.org/docs/latest/genai/eval-monitor/running-evaluation/>
- MLflow Regression Testing（`@mlflow.test`）：<https://mlflow.org/docs/latest/genai/eval-monitor/regression-testing/>
- MLflow 多轮评测：<https://mlflow.org/docs/latest/genai/eval-monitor/running-evaluation/multi-turn/>
- MLflow 对话模拟：<https://mlflow.org/docs/latest/genai/eval-monitor/running-evaluation/conversation-simulation/>
- MLflow 自动问题检测：<https://mlflow.org/docs/latest/genai/eval-monitor/ai-insights/detect-issues/>
- MLflow 评测运行比较：<https://mlflow.org/docs/latest/genai/eval-monitor/comparing-evaluation-runs/>
- MLflow 自定义 code scorer：<https://mlflow.org/docs/latest/genai/eval-monitor/scorers/custom/>
- MLflow Skill Registry：<https://mlflow.org/docs/latest/genai/skill-registry/>
- MLflow Webhooks（OSS）：<https://mlflow.org/docs/latest/self-hosting/webhooks/>
- MLflow MCP Registry：<https://mlflow.org/docs/latest/genai/mcp-registry/>
- MLflow RBAC：<https://mlflow.org/docs/latest/self-hosting/security/role-based-access-control>
- MLflow 搜索语法（含 datasets 字段）：<https://mlflow.org/docs/latest/ml/search/search-runs/>
- MLflow Plugins：<https://mlflow.org/docs/latest/ml/plugins/>
- MLflow 源码：<https://github.com/mlflow/mlflow>
- OpenTelemetry GenAI 语义约定：<https://opentelemetry.io/docs/specs/semconv/gen-ai/>
- OpenTelemetry 文档：<https://opentelemetry.io/docs/>
- SQLite WAL：<https://www.sqlite.org/wal.html>
- Python pickle 安全：<https://docs.python.org/3/library/pickle.html>
- OWASP Top 10：<https://owasp.org/www-project-top-ten/>

## 机器学习工程

- *Designing Machine Learning Systems*，Chip Huyen。
- *Machine Learning Design Patterns*，Lakshmanan、Robinson、Munn。
- *Reliable Machine Learning*，Chen 等。
- Google, "Rules of Machine Learning"：<https://developers.google.com/machine-learning/guides/rules-of-ml>
- Sculley et al., "Hidden Technical Debt in Machine Learning Systems"（NeurIPS 2015）。

## 服务与推理

- MLflow Models / 部署文档：<https://mlflow.org/docs/latest/ml/>
- KServe：<https://kserve.github.io/kserve/>
- MLServer（KServe V2 协议）：<https://mlserver.readthedocs.io/>
- NVIDIA Triton Inference Server：<https://docs.nvidia.com/deeplearning/triton-inference-server/>
- BentoML：<https://docs.bentoml.com/>
- Ray Serve：<https://docs.ray.io/en/latest/serve/>
- Kubernetes 探针：<https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/>

## 数据与分布式系统

- *Designing Data-Intensive Applications*，Martin Kleppmann。
- *Site Reliability Engineering*，Google SRE 团队。
- *Database Internals*，Alex Petrov。

## LLM 与评测

- OpenTelemetry GenAI conventions（见上）。
- *AI Engineering*，Chip Huyen。
- 各模型提供商的评测/安全指南（版本变化快，读官方）。
- 关于 LLM-as-judge 的论文与博客：关注偏差、校准、位置效应；具体论文更新快，建议以最新综述为准。
- MLflow Evaluation Datasets SDK：<https://mlflow.org/docs/latest/genai/datasets/sdk-guide/>
- MLflow `mlflow.data` API：<https://mlflow.org/docs/latest/python_api/mlflow.data.html>
- 数据版本与血缘：先读 Iceberg/Delta 的 snapshot/transaction 文档，再对照本教程第 44 章。
- 模型监控与漂移：Evidently 文档（<https://docs.evidentlyai.com/>）、NannyML（<https://www.nannyml.com/>）、WhyLabs（<https://whylabs.ai/>）；统计基础可读 *Monitoring Machine Learning Models in Production* 一类综述。
- 分布比较与假设检验：PSI/KS/卡方的定义、样本量与多重比较；不要只看“p<0.05”。
- 检索评测：`recall@k`、`precision@k`、MRR、nDCG 的原始定义；BM25 论文（Robertson & Zaragoza, 2009）与 Lucene 的 BM25 实现说明。
- RAG 评测综述：关注 retrieval、faithfulness、answer relevance、citation 的拆分；具体榜单更新快，先固定自己的黄金集与切片。
- LLM-as-judge 偏差：位置偏差、长度偏差、自我偏好、校准；读最新综述并做本地校准实验。

## 安全

- OWASP Top 10（见上）。
- NIST AI Risk Management Framework：<https://www.nist.gov/itl/ai-risk-management-framework>
- SLSA 供应链安全：<https://slsa.dev/>
- Sigstore：<https://www.sigstore.dev/>

## 商业与组织

- *Crossing the Chasm*，Geoffrey Moore。
- *Obviously Awesome*，April Dunford（定位）。
- *The Business of Platforms*，Cusumano 等。
- 云厂商/上市公司财报中的 AI 平台收入披露（自行核对口径）。

## 如何读源码

建议顺序：

1. `mini_mlflow/entities.py` → 对象模型；
2. `store/schema.sql` → 数据模型；
3. `tracking.py` → 用例；
4. `search.py` → DSL；
5. `server.py`/`client.py` → 协议；
6. `models/` → 打包；
7. `tracing.py` → 可观测性；
8. `prompts.py` → Prompt Registry；
9. `gateway.py` → 路由/回退/限流/成本；
10. `webhooks.py` → 事件订阅/outbox/签名/重试；
11. `scoring.py` → 评分协议/鉴权/输入形状（第 42 章）；
12. `rag.py` → BM25/检索指标/引用/groundedness（第 47 章、附录 M）。

对照真实 MLflow 时，先找相同概念，再看字段/后端差异；不要从 UI 代码开始。第 35 章与附录 L 给出了真实仓库的地图。

---

返回：[教程首页](../README.md) · [附录 K 迁移与 API 对照](appendix-k-mlflow3-migration.md) · [附录 M 检索与 RAG 评测](appendix-m-rag-evaluation.md)
