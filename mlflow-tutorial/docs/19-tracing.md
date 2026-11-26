# 第 19 章 Tracing：LLM 可观测性与 OpenTelemetry

## 学习目标

- 理解 Trace/Span 的时序、父子关系和状态语义。
- 用 ContextVar 实现同步/异步上下文传播，并知道线程边界。
- 设计采样、脱敏、存储与查询策略。
- 把内部 trace 映射到 OpenTelemetry 的 OTLP 结构。

## 19.1 为什么 Run 不够

一次训练 run 可以描述“模型怎么来的”，但不能描述：

- 一次 RAG 请求检索了哪些文档；
- LLM 调用的 token 数、成本、首 token 延迟；
- 工具调用是否失败、重试了几次；
- 多轮 Agent 的哪一步导致答案错误。

Trace 是**请求级**的因果时间线：

```text
Trace: answer_question (2.3s, OK)
  Span: retrieve (0.4s, RETRIEVER)
  Span: rerank (0.1s, RERANKER)
  Span: generate (1.7s, LLM)
    Event: token_usage {input: 1200, output: 300}
  Span: validate_json (0.05s, PARSER, ERROR)
```

## 19.2 数据模型

Mini 的 `Trace`：

- trace_id（32 hex）；
- name；
- experiment_id；
- timestamp（毫秒）；
- status（OK/ERROR）；
- tags；
- request_metadata；
- request/response；
- spans。

`Span`：

- span_id（16 hex）；
- trace_id；
- parent_span_id；
- name；
- span_type（CHAIN/RETRIEVER/RERANKER/LLM/TOOL/AGENT/PARSER/...）；
- start_time_ns / end_time_ns；
- status；
- inputs / outputs；
- attributes；
- events。

约束：

- 必须有至少一个 root span；
- parent 必须存在；
- end >= start；
- trace 内 span_id 唯一；
- 任一 span ERROR → trace ERROR。

## 19.3 上下文传播

Mini 使用：

```python
_ACTIVE_TRACE: ContextVar[TraceState | None]
_CURRENT_SPAN: ContextVar[Span | None]
```

同步：

```python
with start_span("retrieve", span_type="RETRIEVER", inputs={"q": q}) as span:
    docs = retriever.search(q)
    span.set_outputs(docs)
```

异步：

```python
@trace("async-agent")
async def run(...):
    ...
```

asyncio 任务在创建时复制上下文，所以：

- 同一任务链能继承当前 trace；
- 并发任务可能共享同一个 trace 的可变 spans 列表（Mini 的行为）；
- 每个任务的 current span 由 ContextVar 独立覆盖。

线程：

- 默认不继承；
- 需要手动创建 trace 或传递 parent；
- Mini 的 `get_current_span()` 在新线程返回 `None`。

## 19.4 隐式 trace 与显式 trace

Mini 支持两种用法：

```python
# 显式：控制 trace 名称/元数据
with start_trace("request", inputs=payload, client=client):
    with start_span("step"):
        ...

# 隐式：最外层 span 自动创建 trace
with start_span("step"):
    ...
```

隐式实现细节：

1. 无 active trace → 创建 root span + trace；
2. 当前 span 作为 root 的 child；
3. 最外层 span 退出时结束 root、flush trace；
4. 嵌套 span 只结束自身。

异常：

- span 标记 ERROR，追加 exception event；
- trace 状态变 ERROR；
- flush 后异常继续向上抛。

## 19.5 OTel 映射

Mini 的 `trace_to_otel` 输出：

```json
{
  "resourceSpans": [{
    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "mini-mlflow"}}]},
    "scopeSpans": [{
      "scope": {"name": "mini_mlflow.tracing", "version": "1.0.0"},
      "spans": [{
        "traceId": "...",
        "spanId": "...",
        "parentSpanId": "...",
        "name": "generate",
        "kind": 1,
        "startTimeUnixNano": "...",
        "endTimeUnixNano": "...",
        "attributes": [
          {"key": "mlflow.spanType", "value": {"stringValue": "LLM"}},
          {"key": "mlflow.spanInputs", "value": {"stringValue": "..."}}
        ],
        "events": [...],
        "status": {"code": 1, "message": "OK"}
      }]
    }]
  }]
}
```

注意：

- OTel 时间戳是纳秒字符串；
- attributes 是 typed any-value；
- `kind=1` 是 INTERNAL；
- 真实 MLflow/OTel 的 GenAI 语义约定字段可能不同；
- 这只是一个可导出的形状，Mini 不实现 OTLP HTTP/gRPC 传输。

## 19.6 采样

全量 trace 的成本：

- 存储：每请求几 KB 到几 MB；
- 写入：高 QPS 下阻塞；
- 敏感数据：prompt/completion 可能含 PII。

采样策略：

| 策略 | 适用 | 风险 |
|---|---|---|
| 头部采样（按请求比例） | 高 QPS、成本敏感 | 可能漏掉错误请求 |
| 尾部采样（按状态/延迟） | 保留错误/慢请求 | 需要缓冲 |
| 分层采样（按租户/模型） | 多租户 | 配置复杂 |
| 全量 + 短期保留 | 调试/合规 | 成本高 |

必须同时：

- 对错误/高延迟请求强制保留；
- 记录采样率，避免指标失真；
- 采样决策在 trace 开始时记录；
- 对 PII 做脱敏/哈希。

## 19.7 隐私与安全

Trace 常包含：

- 用户输入、模型输出；
- 检索文档（可能含内部数据）；
- 工具参数（API key、SQL）；
- 系统提示词（IP）；
- 用户标识。

措施：

- 字段级脱敏（Mini 的 `redact` 覆盖 password/token/api_key 等）；
- 长度截断（Mini 默认 256 KiB/字段）；
- 不记录原始音频/图像，只记录引用；
- 加密存储；
- 访问控制与审计；
- 保留期与删除权（GDPR/个人信息保护法）；
- 租户隔离。

Mini 的脱敏是启发式 key 匹配，不是完整 DLP；生产应使用策略引擎。

## 19.8 存储与查询

Mini：

- `traces` 表 + `spans` 表；
- JSON 负载；
- 同步写；
- 服务层 Python 过滤；
- 按时间倒序。

生产：

- 列式/OLAP（ClickHouse、BigQuery）或专用 trace 存储；
- 按 trace_id 分区/索引；
- 异步批写 + 背压；
- TTL/冷热分层；
- 全文检索（向量化可选）；
- 与评估数据集/标注系统关联。

查询维度：

- 时间、实验、状态、模型、租户；
- 延迟分位数、token/成本；
- span 类型/工具名；
- 错误类型；
- 反馈分数。

## 19.9 与评测的闭环

Trace 是线上数据，评测是离线判断。闭环：

```text
线上 trace → 采样/筛选 → 标注/LLM judge → 评测数据集
   ↑                                      ↓
   └────── 新版本回归评测 ←── 失败案例聚类 ──┘
```

关键：

- 用 trace_id 关联反馈；
- 保存输入/输出/上下文（脱敏后）；
- 记录 judge prompt/模型版本；
- 防止把线上数据直接当训练集造成泄漏；
- 定期复查评测集质量。

第 33 章把“反馈”落成 Assessment：

```python
client.log_feedback(trace_id, "quality", 0.3, source_type="HUMAN", rationale="missed policy")
client.search_traces(filter_string="assessments.quality < 0.5")
```

这比把分数塞进 trace tag 多出三件事：类型可校验、来源可追溯、可参与搜索。评估闭环因此不再依赖人工导表。

多轮 Agent 还需要一层**会话聚合**：用 `mlflow.trace.session` 把多条 trace 归到同一段对话，再在会话级做完成度、沮丧度、工具成功率与延迟判断。Mini 的 `agentops.py` 与真实的 `search_sessions`/`ConversationSimulator` 都在第 46 章展开；本章先把单条 trace 的因果链做对。

## 19.10 与真实 MLflow 的差距

- 真实 MLflow Tracing 支持自动埋点（OpenAI、LangChain、LlamaIndex、DSPy 等）、OTel 导入导出、UI 时间线、评估集成；
- 真实实现有异步写入、采样、后端存储；
- Mini 只提供手动 API + 简化持久化 + OTLP 形状导出；
- 真实 GenAI 语义约定更完整（模型、token、工具、检索文档）；
- 真实 assessment 支持更多 source/valid/overrides 语义，并与 labeling/评测数据集集成；Mini 实现了最小闭环。

## 小结

- Trace 是请求级因果时间线；Span 是其中的节点。
- ContextVar 解决进程内传播；线程/进程边界必须显式处理。
- OTel 是互操作标准；内部模型要能映射，但不必完全等同。
- 采样、脱敏、存储成本是生产落地的核心问题。

## 练习

1. 为一个 RAG 服务设计 span 树和 10 个 attributes。
2. 如何在不记录原文的情况下保留可调试性？
3. 设计一个尾部采样策略，保证错误请求 100% 保留。

---

下一章：[第 20 章 安全、可靠性与多租户](20-security-reliability.md)
