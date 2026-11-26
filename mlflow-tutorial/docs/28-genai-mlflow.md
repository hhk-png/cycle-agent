# 第 28 章 GenAI 时代的 MLflow：Tracing、Prompt、Eval 与 Gateway

## 学习目标

- 理解 MLflow 从“实验追踪”扩展到 GenAI 生命周期的动因。
- 掌握 Trace、Prompt Registry、评测数据集、Scorer、AI Gateway 的职责边界。
- 设计一个可上线的 LLM 应用可观测与评测闭环。
- 区分“当前稳定能力”与“版本相关/实验性能力”，不把营销词当架构。

## 28.1 为什么传统实验追踪不够

传统 run 记录“训练一次模型”的结果。GenAI 应用的问题是：

- 同一模型 + 不同 prompt/检索/工具 = 不同系统；
- 在线输出质量依赖上下文，不能只看离线 AUC；
- 成本/token/延迟是核心指标；
- 失败模式是长尾、非确定性的；
- 需要人工/模型评分，而不是单一标签；
- prompt 本身需要版本管理。

MLflow 3.x 的方向是：把 Tracing、Prompt、Eval 纳入同一套实验/资产模型与 UI。Gateway 的独立进程在 3.0 被移除，但 **3.9 起以 tracking server 内嵌组件回归**；本章仍讨论它，因为路由、限流、成本、密钥这些问题不会消失。具体 API 和可用性随版本/部署（开源 vs Databricks）变化，使用前查官方文档。

## 28.2 能力地图

| 能力 | 解决的问题 | 关键对象 |
|---|---|---|
| Tracing | 一次请求的调用链、延迟、成本、错误 | Trace、Span |
| Prompt Registry | prompt 版本、别名、复用、审计 | Prompt、PromptVersion |
| Prompt Optimization | 从评测反馈自动改进 prompt（实验性） | `optimize_prompts`、优化器、train data |
| Evaluation | 数据集、scorer、judge、报告 | Dataset、Scorer、EvaluationRun |
| AI Gateway | 统一 provider、密钥、限流、路由 | Endpoint、Route、Credential |
| Feedback | 用户反馈、人工标注 | Feedback、Assessment |
| Guardrails | 输入/输出安全策略 | Policy（通常外部） |

MiniMLflow 实现了：

- 手动 Trace/Span + OTel 形状导出；
- 基础 LLM 指标（exact/token F1/ROUGE/JSON validity）；
- Prompt Registry：文本模板、版本、别名、安全渲染（第 33 章）；
- Feedback/Assessment：类型、来源、upsert、trace 搜索（第 33 章）；
- 教学版 AI Gateway：路由、回退、限流、缓存、成本（第 33 章）。

第 44 章补上了评测数据集的最小实现（记录、digest、schema/profile、合并与搜索）；第 45 章补上了上线后的批式监控（基线、PSI/KS/卡方、窗口报告）；第 46 章补上了会话聚合、脚本化多轮模拟、确定性多轮 scorer、在线采样、规则分诊与配对比较；第 47 章补上了知识助手的 BM25 检索、recall@k/MRR/nDCG、引用 F1、词项 groundedness 与端到端门禁。仍然没有实现：真实 MLflow 的 inputs 哈希去重、labeling session、chat-style prompt 模板、真实 provider SDK、分布式限流/缓存、LLM 模拟用户与 prompt 优化。

文档覆盖这些能力的设计，是为了让你在真实平台中能判断“该用哪一层”。

## 28.3 Prompt Registry

Prompt 不应该硬编码在代码里：

```python
prompt = load_prompt("support-reply@production")
```

Prompt Registry 管理：

- 模板文本/消息结构；
- 变量 schema；
- 版本（不可变）；
- 别名（production/canary）；
- 模型参数（temperature、max_tokens）；
- 标签/描述；
- 审计。

为什么重要：

- prompt 改动会改变系统行为，应该像代码一样评审；
- 同一个 prompt 被多个服务复用；
- 回滚 prompt 不应重新部署；
- 评测报告需要绑定 prompt 版本。

设计要点：

- 模板注入要防 SSTI/变量泄漏；
- 变量校验；
- PII 脱敏；
- 版本不可变，别名可变；
- 与 trace 关联（记录 prompt 版本）。

Mini 已实现一个文本模板子集：`prompts.py` 复用 Registry 的版本/别名模式，增加变量提取、缺/多变量校验、控制流拒绝、`to_single_brace_format()`；API 为 `register_prompt`/`load_prompt`/`set_prompt_alias`。chat-style message 模板、与评测/标注的联动仍是生产平台能力。详见第 33 章。

## 28.4 评测数据集与 Scorer

一个 GenAI 评测需要：

```text
数据集（输入、参考输出、上下文、标签）
  + 应用/模型版本
  + Scorer（确定性/模型/人工）
  + 聚合/切片/置信区间
  = 评测报告
```

Scorer 类型：

- 精确匹配/包含；
- 结构化校验（JSON schema、必填字段）；
- 检索质量（recall@k、MRR、nDCG）；
- 答案质量（token F1、ROUGE、BERTScore）；
- LLM-as-judge（rubric）；
- 安全/合规（毒性、PII、越狱）；
- 业务（转化、人工采纳）。

评测数据集要求：

- 版本化/digest；
- 覆盖真实分布与长尾；
- 防止训练污染；
- 标注一致性；
- 隐私/许可证。

Mini 的 `evaluate_model` 是简化版：预测 + 指标 + JSON 报告；第 44 章补了数据集版本/digest/schema/profile 与合并，但仍没有 Scorer 注册表、记录级去重和 `mlflow.genai.evaluate` 直连。

## 28.5 LLM-as-Judge 的工程化

风险：

- judge 偏好长答案/特定风格；
- 位置偏差（A/B 顺序）；
- 自我偏好；
- 提示注入；
- 成本/延迟；
- 不可复现（温度/模型更新）。

控制：

- 固定 judge 模型版本/参数；
- 盲评/随机顺序；
- 多 judge/多次采样；
- 与人工标注校准（准确率/kappa）；
- 记录 judge prompt 与原始输出；
- 对 judge 输出做结构化解析与校验；
- 定期回归测试。

不要把 judge 分数当 ground truth；它是另一个需要评测的模型。

## 28.6 AI Gateway

Gateway 解决：

- 多 provider（OpenAI/Anthropic/自建）；
- 密钥不落入应用；
- 统一鉴权/配额；
- 路由/fallback；
- 缓存；
- 成本归因；
- 审计/脱敏；
- 限流。

架构：

```text
App → Gateway → Provider A
             ├→ Provider B (fallback)
             └→ 自建模型
```

风险：

- 单点故障；
- 额外延迟；
- 供应商锁定；
- 日志含敏感 prompt；
- 路由策略错误导致质量下降。

MLflow 的 AI Gateway 能力与开源/托管版本相关；不要假设开源版具备完整企业网关功能。很多团队选择 LiteLLM/Envoy/云网关 + MLflow tracing。

**版本事实（容易写错）**：MLflow 2.9（2024-01）把 AI Gateway 改名为 MLflow Deployments for LLMs；MLflow 3.0（2025-06）移除了独立 deployments server 与 `start-server`，旧 `routes`/`route_type` 配置键也被移除。**3.9（2026-01）起 Gateway 以 tracking server 内嵌组件回归**，先加入路由/回退/passthrough；**3.11 加预算，3.12 加 guardrail**。因此准确说法是“独立进程消失，控制面能力回到 server”，而不是“开源 MLflow 没有网关”。Mini 的 `gateway.py` 是教学实现，演示路由、回退、限流、缓存、成本、密钥隔离；它不是对某个版本的逐字节复刻。详见第 33、37 章。

## 28.7 端到端闭环

```text
Prompt Registry ──> App ──> Trace ──> Feedback/Eval Dataset
       ↑                              │
       └──── 新版本评测/发布 ←── Scorer/Judge
```

关键连接：

- Trace 记录 prompt 版本、模型版本、检索文档、工具调用；
- 反馈写回 trace/assessment；
- 失败样本进入评测集；
- 新版本在固定评测集上回归；
- 报告绑定 run/版本；
- 发布用 alias；
- 线上监控触发再评测。

## 28.8 成本与延迟

GenAI 的可观测性不能只记 trace，还要聚合：

- token in/out；
- 每请求成本；
- 缓存命中率；
- p50/p95/p99 延迟；
- 首 token 延迟；
- 工具/检索耗时；
- 失败/重试；
- 每租户/模型/功能成本。

这些指标应进入监控/计费系统，而不是只躺在 trace 存储里。

## 28.9 安全与隐私

- prompt/completion 可能含 PII/密钥；
- 检索文档可能含内部数据；
- 工具调用可能执行副作用；
- trace 存储成为新的敏感数据源；
- Gateway 是密钥集中点。

措施：

- 字段级脱敏/截断；
- 采样；
- 加密/访问控制；
- 保留期；
- 租户隔离；
- 工具调用白名单/审批；
- 输出 guardrails；
- 红队/越狱测试。

## 28.10 Agent 观测的特殊性

Agent 的 trace 不是简单树：

- 可能有循环/重试；
- 工具调用有副作用；
- 多 Agent 协作；
- 长时运行；
- 人类介入；
- 失败后重规划。

需要记录：

- 计划/决策；
- 工具输入/输出（脱敏）；
- 重试/回退；
- 终止原因；
- 人工干预；
- 成本预算。

只用 Span 树可能不够表达循环；需要事件/链接或图模型。Mini 只实现树，生产要评估。

## 28.11 与真实 MLflow 的差距

- 真实 MLflow 3.x 提供 tracing、prompt/eval 相关 API（具体随版本）；
- AI Gateway 在 3.0 移除了独立 server，但 **3.9 起以 tracking server 内嵌组件回归**，并加入路由/回退、预算与 guardrail；
- 真实 MLflow 3.1+ 的 Prompt Registry 支持 `mlflow.genai.register_prompt/load_prompt`、chat-style 消息模板、`response_format` 与别名；
- Mini 实现了手动 trace + 基础指标 + prompt **文本**模板 + assessment + 教学 gateway；
- Mini 已有最小的评测数据集（第 44 章），仍缺 chat-style prompt、labeling session、记录级去重、真实 provider SDK、分布式限流；
- 生产还需要 guardrails、权限审计、评测平台、成本系统。

## 28.12 2026 年更新：3.9–3.18 把控制面做成了平台

本章的能力地图完成于 3.x 早期。到 2026-11 的 3.18，GenAI 控制面又补了以下平台能力；细节与架构影响见第 37 章：

| 能力 | 版本 | 一句话 |
|---|---|---|
| Gateway 内嵌 tracking server | 3.9 | 不再需要独立进程；路由、回退、passthrough |
| Trace 总览与成本 | 3.9/3.10 | 延迟/错误/token/成本进入 dashboard |
| 多工作区 | 3.10 | 一个 server 服务多个逻辑租户 |
| Gateway 预算 | 3.11 | 日/周/月限额、429、Redis 原子扣减 |
| Guardrail | 3.12 | pre/post-LLM 策略与审计 span |
| RBAC | 3.13/3.17 | 角色、子资源、显式 DENY |
| Review Queue / `@mlflow.test` | 3.14 | 人工评审与 pytest 回归进入闭环 |
| MCP Registry | 3.15 | Agent 工具的版本/别名/工具快照 |
| Trace Explorer / Span Links / 自定义视图 | 3.16 | 可配置的评审界面 |
| 无 pickle 与模型代码执行开关 | 3.11/3.18 | 供应链安全默认收紧 |

因此，“MLflow 适不适合做 GenAI 平台”这个问题在 2026 年要拆成两个：**开源版有没有能力**（大部分有，部分实验性），以及**你的团队能不能承担数据库、Redis、对象存储、身份与升级的运维成本**（见第 31～32、37 章）。

本章是能力地图；真实 API 的端到端写法（`@mlflow.trace`、`mlflow.genai.evaluate`、scorer/judge、`@mlflow.test`）见第 39 章；把 Prompt、Gateway、Trace、数据集、检索指标、在线抽样与监控装进一个可运行产品的写法见第 47 章。

## 小结

- GenAI 生命周期的核心资产是 prompt、trace、评测数据集和 scorer。
- Tracing 提供请求级因果；Eval 提供质量判断；Prompt Registry 提供版本化；Gateway 提供统一治理。
- 会话级评测需要稳定的 `mlflow.trace.session` 归属；prompt 优化需要固定评测集与 scorer，优化的产物仍是新版本，不是直接上线。
- LLM-as-judge 需要校准和版本化，不能当 ground truth。
- 安全/隐私/成本是 GenAI 平台的一等公民。

## 练习

1. 设计一个 Prompt Registry 的 schema（模板/版本/别名/变量）。
2. 为一个 RAG 应用定义 5 个 scorer 和对应评测集切片。
3. 设计一个 Gateway 的 fallback 与成本归因方案。
4. 为一段多轮客服对话设计 session/turn 标识与两个会话级 scorer。
5. 用 `optimize_prompts` 的输入契约设计一次 prompt 优化实验：训练集、scorer、独立测试集、回滚条件。

---

下一章：[第 29 章 未来方向](29-future.md)
