# 附录 I 验证报告

> 验证日期：2026-11-26。环境：Windows 11，Python 3.14.6。测试/示例数字会随代码演进，以实际命令输出为准。

## I.1 命令

```bash
python -m pytest -q
python scripts/check_docs.py
python scripts/check_deploy.py
python scripts/verify.py
```

## I.2 结果

| 检查 | 结果 |
|---|---|
| Pytest | **273 passed**（收集 273；Windows 11 / Python 3.14） |
| 文档结构/链接/代码围栏 | 通过（47 章 + 13 附录） |
| 示例 01–20 | 全部通过；`examples/deploy/` 仅静态检查 |
| 部署静态检查 | 通过（Compose YAML、Nginx 指令、shell 语法） |
| `scripts/verify.py` | 通过（4 个阶段） |

## I.3 覆盖范围

- 配置/路径/URI/校验；
- SQLite schema、事务、软删除；
- Tracking 生命周期、批量原子、指标历史；
- 搜索 DSL 解析/编译/注入防护，以及 `datasets.name/digest/context` 反查与 EXISTS 语义；
- Artifact 路径安全/下载；
- 模型保存/加载/签名/未知 loader；
- Registry 版本/别名/阶段/搜索；
- HTTP 本地/远端契约、鉴权、UI 转义；
- Projects 子进程/失败/注入；
- Evaluate 公式与记录；
- Autolog/fluent；
- Tracing 嵌套/异常/异步/线程/OTel；
- Prompt Registry 版本/别名/变量/渲染/删除；
- Assessment 类型/来源/upsert/trace 内嵌/搜索；
- Gateway 成功/回退/全失败/限流/缓存/成本/配置校验/HTTP；
- Webhook CRUD/URL 校验/事件过滤/签名/重试/失败/重启恢复/HTTP 契约；
- LoggedModel：创建/默认制品目录/软删除可见性/不可变参数/标签 upsert/指标 step 优先/终态幂等/搜索方言与分页/`models:/<id>` 加载/远程 REST 契约；
- CLI：prompts/feedback/gateway/webhooks/logged-models 子命令；
- 评分服务：健康检查、四种输入形状、签名校验、401/400/413 与请求体上限；
- 第 43 章综合实战：数据血缘、切片指标、注册别名、在线打分、Trace 与 Feedback；
- 第 44 章评测数据集：记录契约、digest 稳定性、schema/profile、合并版本、CAS 冲突、并发 merge、搜索、删除、远端与 CLI；
- 第 45 章监控与漂移：基线 profile、数据集绑定、PSI/KS/卡方、缺失率、新类别、常量基线、小样本、阈值、不可变窗口、删除级联、REST/CLI 与统计函数；
- 第 46 章 AgentOps：会话排序/token/工具/错误聚合、空 tag 忽略、脚本模拟的正常/异常/截断、用例校验、会话生成用例、五个 scorer 边界、CODE assessment 幂等、在线采样率 0/1/0.5 与 filter、issue 签名归一化与严重度、配对 bootstrap 的显著改进/回归/样本不足、dataset records 形状；
- 第 47 章 RAG 评测：BM25 排序/ tie-break/无重叠/元数据过滤/参数校验、中文 bigram 与复数折叠、recall/precision/MRR/nDCG、retrieval_report 聚合与校验、引用提取/精确率/召回率/F1、词项 groundedness 与引用忽略；
- 示例 20：Prompt 版本/别名、Gateway 路由与缓存、Trace retrieve/rerank/generate/validate、黄金集、配对门禁、在线 CODE assessment + HUMAN feedback、稳定/漂移 monitor、发布证据 artifact。

## I.4 未覆盖/限制

- 真实 Postgres/S3/云平台集成；
- 浏览器级 UI E2E/可访问性；
- 大规模并发/故障注入/压力测试；
- 真实 MLflow REST 兼容性；
- 企业 RBAC/多租户/审计；
- 反序列化沙箱（仅拒绝未知 loader）；
- Conda/Docker 项目环境；
- 采样/异步 Trace；
- 分布式限流/共享缓存/真实 provider SDK（Gateway 为教学实现）；
- Webhook 的事务性 outbox/secret 加密/多副本 claiming/DLQ 回放（第 38 章分析）；
- 真实 MLflow `mlflow.genai` API 的自动化测试（第 39 章为真实环境示例，不随 Mini 测试运行）；
- LoggedModel 的 `registrations` 反向关系、模型别名、数据集关联、远程制品下载、GC 与 UI；
- chat-style prompt 模板、labeling session；
- 评测数据集仍缺真实 MLflow 的 inputs 哈希去重、记录级删除/更新、分页与 `mlflow.genai.evaluate` 直连；
- 监控为批式单进程实现：无流式聚合、标签 join、分群、告警投递、权限与保留期；卡方 p 值为近似，基线样本上限 2000（第 45 章）；
- AgentOps 为派生式批处理实现：无 LLM 模拟用户、异步评分、独立 session 表、跨服务会话拼接、Issues 状态机、BCa/多重比较校正与 Skill Registry（第 46 章）；
- RAG 评测为确定性最小实现：无向量检索、真实 reranker（示例为 identity rerank）、NLI/LLM judge、分布式评测、流式在线评分与真实 provider；BM25 的 `recall@2=1.0` 只说明黄金集容易，不代表检索质量（第 47 章、附录 M）；
- run input 的 schema/profile、自动 digest 与按 source 过滤；
- 2026 平台化能力（工作区、RBAC、MCP Registry、Gateway 预算）仅在第 37 章分析，未实现；
- 评分服务的动态批处理、流式响应、TLS/OIDC、限流、多模型路由与 GPU 未实现（第 42 章给出生产清单）；
- 第 43 章综合实战使用合成数据，指标不代表真实业务效果；真实项目需重做数据契约、基线与门禁。

## I.5 如何复现

```bash
cd mlflow-tutorial
python -m pip install -r requirements.txt
python scripts/verify.py
```

如果示例 07 端口被占用，会自动使用随机端口；若失败，检查防火墙/本机安全软件是否阻止 localhost 绑定。

## I.6 结论

MiniMLflow 在“本地/可信网络”范围内实现了可运行的 MLflow 核心控制面闭环，并通过自动化测试、文档检查与示例验证。它**不是**生产平台的替代品；第 20～25 章、第 36～38 章给出了生产化所需的数据库、对象存储、鉴权、并发、可观测性、事件投递、运维与治理能力清单，第 40 章给出模型中心的参考实现，第 41 章给出参考部署编排与验收清单，第 42 章给出服务协议与生产检查清单，第 43 章把整条交付链串成可运行的综合实战，第 44 章补上评测数据集的资产化与治理，第 45 章补上上线后的监控、漂移与告警闭环，第 46 章补上 Agent 的会话评测、在线评分、问题分诊与统计门禁，第 47 章把知识助手的检索、引用、门禁、在线抽样与监控串成可运行、可回滚的 GenAI 交付链。

---

返回：[教程首页](../README.md) · [附录 J](appendix-j-reading.md)
