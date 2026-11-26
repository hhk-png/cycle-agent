# 第 46 章 RAG、向量检索与知识 API

## 46.1 RAG 的 API 边界

RAG（Retrieval-Augmented Generation）不是单一 API，而是一条流水线：

```text
文档接入 → 解析 → 分块 → 向量化 → 索引
用户问题 → 查询改写 → 检索 → 重排 → 组装上下文 → 生成 → 引用
```

API 设计要明确哪些能力由平台提供，哪些由调用方负责：

- 只提供向量检索；
- 提供检索 + 重排；
- 提供完整 RAG 问答；
- 提供知识库管理。

边界不同，契约完全不同。

### 四种产品形态

| 形态 | 平台提供 | 调用方负责 | 适用 |
|---|---|---|---|
| 向量数据库 | 存储 + 相似度 | 分块、嵌入、生成 | 自建 RAG |
| 检索 API | 解析、分块、嵌入、检索 | 生成 | 半自建 |
| RAG API | 全流水线 | 业务集成 | 快速上线 |
| 知识库产品 | 全流水线 + UI | 内容 | 企业 |

## 46.2 知识库资源

```text
KnowledgeBase
├── Documents
├── Chunks
├── Indexes
├── Queries
└── Evaluations
```

### 创建知识库

```http
POST /knowledge-bases
```

```json
{
  "name": "product-docs",
  "embeddingModel": "embedding-model-id",
  "chunking": {
    "strategy": "semantic",
    "maxTokens": 512,
    "overlapTokens": 64
  },
  "metadataSchema": {
    "source": "string",
    "product": "string",
    "locale": "string",
    "updatedAt": "datetime"
  }
}
```

### 知识库的状态

```text
CREATING → READY → UPDATING → READY
              │        │
              └→ FAILED└→ FAILED
```

## 46.3 文档接入

```http
POST /knowledge-bases/{kbId}/documents
```

```json
{
  "source": { "type": "url", "url": "https://docs.example.com/guide" },
  "metadata": { "product": "api", "locale": "zh-CN" },
  "acl": { "groups": ["eng", "support"] }
}
```

异步处理：

```json
{
  "documentId": "doc_123",
  "status": "PROCESSING",
  "operationId": "op_456"
}
```

状态：

```text
PENDING → PARSING → CHUNKING → EMBEDDING → INDEXED
                                      ├→ FAILED
                                      └→ DELETED
```

### 文档来源

- URL；
- 文件上传；
- 对象存储；
- SaaS 连接器（Confluence、Notion、Google Drive）；
- API 推送；
- 数据库。

### 文档解析

- PDF；
- Word；
- HTML；
- Markdown；
- 表格；
- 图片（OCR）；
- 代码。

解析质量直接影响检索质量。

## 46.4 分块

分块策略影响检索质量：

- 固定 token；
- 按段落/标题；
- 语义分块；
- 代码/表格特殊处理；
- 重叠窗口；
- 父子块（小块检索，大块返回上下文）。

API 应支持：

- 自定义分块；
- 重新分块；
- 分块预览；
- 元数据继承；
- 版本化。

### 分块大小

- 太小：上下文不足，召回碎片；
- 太大：噪声多，浪费 token；
- 通常 256～1024 token；
- 重叠 10%～20%；
- 按文档类型调整。

### 父子块

```text
子块（小）用于检索；
父块（大）用于返回上下文。
```

优点：

- 检索精度；
- 上下文完整。

## 46.5 向量化

```http
POST /embeddings
```

```json
{
  "model": "embedding-model-id",
  "input": ["文本 1", "文本 2"],
  "dimensions": 1024
}
```

响应：

```json
{
  "data": [
    { "index": 0, "embedding": [0.012, -0.034, 0.056] },
    { "index": 1, "embedding": [0.011, 0.022, 0.033] }
  ],
  "usage": { "inputTokens": 42 }
}
```

要点：

- 批量；
- 维度；
- 归一化；
- 模型版本；
- 成本；
- 速率限制；
- 缓存；
- 隐私。

### 嵌入模型的选择

- 语言支持；
- 维度；
- 上下文长度；
- 领域适配；
- 成本；
- 延迟；
- 是否需要微调。

### 模型版本

嵌入模型升级会导致：

- 向量空间变化；
- 旧向量不兼容；
- 需要重建索引；
- 查询与文档必须同一模型。

API 应记录：

```json
{
  "embeddingModel": "embedding-model-id",
  "embeddingVersion": "2026-10-01"
}
```

## 46.6 检索 API

```http
POST /knowledge-bases/{kbId}/query
```

```json
{
  "query": "如何配置 OAuth 回调地址？",
  "topK": 10,
  "filters": {
    "product": "api",
    "locale": "zh-CN"
  },
  "hybrid": {
    "enabled": true,
    "alpha": 0.5
  },
  "rerank": true
}
```

响应：

```json
{
  "results": [
    {
      "chunkId": "chunk_123",
      "documentId": "doc_123",
      "score": 0.87,
      "text": "在控制台的 OAuth 设置中...",
      "metadata": { "source": "...", "title": "..." },
      "citation": {
        "url": "https://docs.example.com/oauth#callback",
        "startOffset": 120,
        "endOffset": 340
      }
    }
  ],
  "usage": { "queryTokens": 12 }
}
```

### 检索模式

- 向量检索；
- 关键词/BM25；
- 混合检索；
- 元数据过滤；
- 重排；
- 多路召回；
- 查询改写。

### 混合检索

```text
向量分数 + 关键词分数 → 加权融合
```

优点：

- 语义召回；
- 精确匹配；
- 互补。

`alpha` 控制权重：

```text
alpha = 1 → 纯向量
alpha = 0 → 纯关键词
```

### 重排

检索 top 50，重排 top 10：

- 提高精度；
- 增加延迟；
- 增加成本；
- 适合高价值查询。

## 46.7 过滤与权限

检索必须遵守文档 ACL：

```json
{
  "filters": {
    "tenantId": "t_123",
    "aclGroups": ["eng"],
    "locale": "zh-CN"
  }
}
```

关键：

- 租户隔离；
- 文档级权限；
- 字段级脱敏；
- 过滤不能只靠客户端；
- 向量索引要支持元数据过滤；
- 结果不能跨租户召回。

### 预过滤 vs 后过滤

- 预过滤：在检索前过滤，结果准确，但可能影响召回；
- 后过滤：先检索再过滤，可能过滤掉太多，结果不足。

对权限，必须预过滤或使用带过滤的索引。

### 权限变更

- 文档 ACL 变化；
- 用户组变化；
- 权限缓存；
- 实时性；
- 重新索引。

## 46.8 引用与可追溯

RAG 的价值之一是引用：

```json
{
  "answer": "回调地址需要在控制台配置...",
  "citations": [
    {
      "documentId": "doc_123",
      "chunkId": "chunk_123",
      "url": "...",
      "quote": "..."
    }
  ]
}
```

要求：

- 引用可点击；
- 引用内容与答案一致；
- 不伪造来源；
- 引用过期处理；
- 多语言；
- 引用片段高亮。

### 引用的验证

- 答案中的每个事实是否有引用；
- 引用是否真的支持答案；
- 引用是否最新；
- 引用是否可访问。

## 46.9 新鲜度与更新

- 文档变更→重新索引；
- 增量更新；
- 删除同步；
- 版本化；
- 索引延迟；
- 缓存失效；
- 一致性。

API 应暴露：

```json
{
  "indexedAt": "2026-11-26T10:00:00Z",
  "sourceUpdatedAt": "2026-11-25T18:00:00Z",
  "stale": false
}
```

### 更新策略

- 全量重建；
- 增量更新；
- 双索引切换；
- 版本化；
- 回滚。

## 46.10 评估

RAG 需要评估：

- 召回率；
- 精确率；
- 排序质量；
- 答案忠实度；
- 引用准确性；
- 延迟；
- 成本；
- 拒绝率。

API：

```http
POST /evaluations
```

```json
{
  "knowledgeBaseId": "kb_123",
  "dataset": "eval_set_1",
  "metrics": ["recall@10", "mrr", "faithfulness"]
}
```

### 评估集

- 真实问题；
- 标准答案；
- 相关文档；
- 难例；
- 多语言；
- 定期更新。

### 指标

| 指标 | 定义 |
|---|---|
| Recall@K | `|relevant ∩ topK| / |relevant|`，前 K 个结果覆盖了多少相关文档 |
| Precision@K | `|relevant ∩ topK| / K`，前 K 个结果中有多少是相关的 |
| MRR | `mean(1 / rank_first_relevant)`，第一个相关结果排名的倒数的平均值 |
| NDCG | 考虑排序位置与相关度等级的归一化折损累计增益 |
| Faithfulness | 答案是否忠于检索到的上下文 |
| Citation accuracy | 引用是否准确指向支撑答案的文档 |
| Answer relevance | 答案是否回答了问题 |

三个查询的示例：

```text
Q1 相关文档 {A, B}，Top3 = [A, C, D]
  Recall@3 = 1/2 = 0.50
  RR = 1/1 = 1.00

Q2 相关文档 {C}，Top3 = [D, C, A]
  Recall@3 = 1/1 = 1.00
  RR = 1/2 = 0.50

Q3 相关文档 {A, B, C}，Top3 = [D, E, F]
  Recall@3 = 0/3 = 0.00
  RR = 0

平均 Recall@3 = (0.50 + 1.00 + 0.00) / 3 = 0.50
MRR = (1.00 + 0.50 + 0.00) / 3 = 0.50
```

注意：`Recall@K` 不是“前 K 个结果包含相关文档”的布尔命中率；后者是 Hit Rate。`MRR` 也不是“第一个相关结果的位置”，而是排名倒数的平均。

## 46.11 成本

- 嵌入 token；
- 存储；
- 向量索引；
- 查询；
- 重排；
- 生成；
- 缓存。

优化：

- 批量嵌入；
- 维度压缩；
- 分层索引；
- 缓存查询；
- 只重排 topK；
- 小模型路由；
- 按需重建索引。

## 46.12 反模式

- 只做向量检索，不做权限过滤；
- 分块过大/过小；
- 不更新索引；
- 引用伪造；
- 无评估；
- 把所有文档塞进上下文；
- 忽略关键词检索；
- 不处理多语言；
- 不监控成本；
- 嵌入模型升级不重建索引；
- 权限过滤依赖客户端。

## 46.13 本章小结

- RAG 是流水线，API 边界要明确。
- 知识库、文档、分块、索引、查询是核心资源。
- 分块与混合检索影响质量。
- 检索必须遵守租户与文档权限。
- 引用要可追溯、可验证。
- 新鲜度、评估、成本是生产必备。
- 嵌入模型版本变化需要重建索引。
- 检索日志、引用片段与评估集都可能携带个人数据；最小化、脱敏与隐私预算见[附录 AG](../appendices/ag-privacy-enhancing-tech.md)。

## 设计练习

为“企业文档问答 API”设计：

- 知识库管理；
- 文档接入；
- 分块；
- 混合检索；
- 权限过滤；
- 引用；
- 评估；
- 成本控制。

写出检索请求/响应与权限模型。然后回答：

1. 文档权限变化后，索引如何更新？
2. 如何评估引用准确性？
3. 分块策略如何按文档类型调整？
4. 如何防止跨租户召回？

---

上一章：[LLM API 设计：流式、Token、工具与结构化输出](45-llm-apis.md)  
下一章：[Agent 协议：MCP、A2A 与工具契约](47-agents-mcp-a2a.md)
