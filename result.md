# Agent 客户端与大模型交互的通信机制详解

## 1 概述

Agent 客户端与大模型（LLM）之间的交互是现代 AI 应用架构的核心环节。客户端将用户的请求封装为结构化消息，通过标准通信协议发送至模型服务端，服务端推理完成后将结果返回。这一过程涉及**通信方式**的选择、**通信协议**的运用以及 **HTTP 协议字段**的精细配合。

> 本文以业界最广泛采用的 **OpenAI API 兼容协议** 为主要蓝本，同时详细标注各主流厂商（Anthropic、Azure OpenAI、Google Gemini 等）的关键差异，力求覆盖从协议选型到工程落地的全链路知识。

### 1.1 交互的基本流程

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  用户/UI 层   │────▶│ Agent SDK    │────▶│  LLM API     │
│              │◀────│  客户端       │◀────│  服务端       │
└──────────────┘     └──────────────┘     └──────────────┘
                           │
                           ▼
                     ┌──────────────┐
                     │ 外部工具/函数  │
                     │ (Tool Use)    │
                     └──────────────┘
```

核心交互路径为：**用户输入 → 客户端构建消息 → 协议封装 → 网络传输 → 模型推理 → 结果返回 → 客户端解析 → 展示给用户**。

若过程中触发工具调用（Tool Use / Function Calling），则插入「客户端执行工具 → 将结果回传模型 → 模型继续生成」的额外轮次，形成多轮内循环。

### 1.2 交互角色与职责

| 角色 | 职责 | 典型实现 |
|------|------|----------|
| **用户/UI 层** | 提供输入、展示输出 | Web 界面、CLI、移动应用 |
| **Agent 客户端** (SDK) | 消息构建、协议封装、重试/容错、工具编排 | OpenAI SDK、LangChain、Vercel AI SDK、自定义 SDK |
| **LLM API 服务端** | 模型推理、Token 生成、策略应用 | OpenAI API、Anthropic API、Azure OpenAI |
| **外部工具** | 提供模型不具备的实时/专有能力 | 天气 API、数据库、搜索引擎、代码执行器 |

---

## 2 通信方式

Agent 客户端与 LLM 之间的通信方式选择直接影响用户体验、系统复杂度和成本效率。以下是四种主流模式及其适用场景。

### 2.1 同步请求-响应（Non-Streaming）

最基础的交互模式。客户端发送完整请求，服务端在全部推理完成后一次性返回完整响应。

```
客户端                         服务端
  │                              │
  ├──── POST /v1/chat/completions ──►  开始推理
  │      (完整消息体)               │
  │                              ├── 完整生成（耗时 T）
  │◄──── 200 OK ──────────────────┤
  │      {完整响应 JSON}           │
```

| 维度 | 说明 |
|------|------|
| **优点** | 实现简单，适合短文本生成或不需要即时反馈的场景 |
| **缺点** | 首字节等待时间长（TTFB ≈ 完整推理耗时 T），长文本时用户体验差 |
| **适用场景** | Embedding 向量化、分类/提取类任务、对延迟不敏感的批处理 |
| **典型策略** | 结合超时控制（通常 60s-300s），大模型场景使用较长超时 |

### 2.2 流式传输（SSE Streaming）

服务端通过 **SSE（Server-Sent Events）** 将生成内容逐 Token（或逐片段）推送给客户端，客户端可实时展示增量结果。

```
客户端                         服务端
  │                              │
  ├──── POST /v1/chat/completions ──►  开始推理
  │      {stream: true}           │
  │                              ├── 生成 Token₁
  │◄── data: {"content":"你好"} ──┤
  │                              ├── 生成 Token₂
  │◄── data: {"content":"，"} ─────┤
  │                              ├── 生成 Token₃
  │◄── data: {"content":"世界"} ──┤
  │                              ├── 生成完成
  │◄── data: [DONE] ─────────────┤
```

| 维度 | 说明 |
|------|------|
| **优点** | 首 Token 延迟低（TTFB 仅需推理第一个 Token），用户体验流畅，适合长文本生成 |
| **缺点** | 客户端需处理事件流解析和中间状态累积逻辑，网络抖动时可能出现断流 |
| **适用场景** | 对话机器人、文本生成、代码补全等需要实时反馈的场景 |

> SSE 是一种 **HTTP 长连接** 技术，不同于 WebSocket。SSE 是服务端向客户端的 **单向** 推送，基于纯 HTTP 协议，浏览器原生支持 `EventSource` API。客户端无法通过同一连接向服务端发送数据。SSE 的优势在于：协议开销小、自动重连机制（EventSource 规范内置）、兼容性好。

#### SSE 协议标准细节

SSE 遵循 [W3C 规范](https://html.spec.whatwg.org/multipage/server-sent-events.html)，其事件流格式有严格定义：

```
<event-stream> ::= <event>*
<event> ::= [<field>]*(CR | LF | CRLF)
<field> ::= <field-name> ":" [<space>] <field-value>

字段类型:
  data:  <字符串>     — 事件数据（可多行拼接）
  id:    <字符串>     — 事件 ID，断连重连时作为 Last-Event-ID 发送
  event: <字符串>     — 事件类型（默认 message）
  retry: <整数>       — 重连间隔（毫秒）
```

**SSE 边缘情况处理**：

```
1. 多行 data:
   data: Hello
   data: World
   → 解析为 "Hello\nWorld"（data 字段按行拼接）

2. 空 data（data: 后面无内容）
   data:
   → 解析为空字符串，不是跳过

3. 注释行
   : 这是注释，不会被解析
   → 以冒号开头的行是注释，客户端应忽略

4. 事件结束
   每个事件以空行（CRLF/CR/LF）分隔
```

### 2.3 工具调用（Tool Use / Function Calling）

模型在生成过程中可请求调用外部工具（函数），客户端执行工具后返回结果，模型据此继续生成。这是一种**多轮内循环**通信模式，是 Agent 获取外部能力的核心机制。

```
客户端                         服务端
  │                              │
  ├── 请求（含 tools 定义） ────────►
  │                              │
  ◄── 响应: finish_reason=       ─┤
  │    "tool_calls"               │
  │                              │
  │  调用外部 API / 执行函数       │
  │                              │
  ├── 工具结果 → 继续对话 ────────►
  │                              │
  ◄── 最终回答 ──────────────────┤
```

#### 工具调用协程的状态机

```
        ┌──────────┐
        │  发送请求  │
        └────┬─────┘
             │
        ┌────▼──────┐
        │ 解析响应    │
        └────┬──────┘
             │
     ┌───────┴───────────┐
     ▼                   ▼
  finish_reason=     finish_reason=
  "tool_calls"       "stop" / "length"
     │                   │
     ▼                   ▼
  ┌──────────┐     ┌──────────┐
  │ 执行工具   │     │ 输出结果   │
  └────┬─────┘     └──────────┘
       │
  ┌────▼──────┐
  │ 回传结果    │────────▶ 继续请求
  └───────────┘
```

| 维度 | 说明 |
|------|------|
| **优点** | 赋予模型访问实时数据、执行计算的能力，突破模型本身的知识和功能边界 |
| **缺点** | 增加交互轮次和延迟，工具执行可靠性取决于第三方 API 的稳定性 |
| **适用场景** | 信息查询、数据库操作、API 调用、代码执行等需要外部能力的场景 |
| **并行调用** | 支持 `parallel_tool_calls: true` 时，一次响应可返回多个工具调用并行执行 |

### 2.4 WebSocket 长连接

对于需要频繁低延迟双向交互的场景（如实时语音对话、协同编程、流式推理的持续交互），可建立 WebSocket 持久连接实现双向推送。

```
客户端                           服务端
  │                                │
  ├── WebSocket 握手 (HTTP 101) ────►  协议升级
  ├── JSON 请求帧 ──────────────────►
  ◄── JSON 响应帧 ─────────────────┤
  ├── 新一轮请求帧 ────────────────►
  ◄── 新一轮响应帧 ───────────────┤
  ├── Ping/Keepalive ──────────────►  连接保活
  ◄── Pong ───────────────────────┤
  ├── Close ──────────────────────►  连接关闭
```

| 维度 | 说明 |
|------|------|
| **优点** | 无 HTTP 首部开销，低延迟，支持真正的双向实时通信 |
| **缺点** | 实现复杂度高，需要处理重连、心跳、会话恢复、粘包等逻辑 |
| **适用场景** | 实时语音对话（如 ChatGPT 语音模式）、协同编程、持续推理的交互式应用 |
| **协议开销** | WebSocket 帧头部最小仅 2 字节，远小于 HTTP 请求头（通常数百字节） |

#### WebSocket 与 SSE 选型决策

```
┌─────────────────────────────────────────────────────┐
│  需要双向通信？                                       │
│  ├─ 是 → WebSocket                                    │
│  │  ├─ 需要传输二进制（音频/视频）→ WebSocket           │
│  │  └─ 纯文本 → WebSocket 或 HTTP/2 Server Push       │
│  └─ 否（仅服务端推送）→ SSE                              │
│     ├─ 浏览器环境 → SSE（EventSource API）               │
│     └─ Node.js 环境 → SSE（自定义解析）                  │
└─────────────────────────────────────────────────────┘
```

### 2.5 gRPC 流

部分厂商（如 Google Gemini）和自建推理平台支持 gRPC 协议，利用 HTTP/2 的多路复用和流式特性实现高效通信。

```
客户端                         服务端
  │                              │
  ├── gRPC Bidirectional Stream  ►
  │   ClientRequest{...}         │
  ◄── ServerResponse{...}        │
  │   (流式返回 token)            │
  │                              │
```

| 维度 | 说明 |
|------|------|
| **优点** | 强类型 schema（Protobuf）、流式原生支持、HTTP/2 多路复用、低延迟 |
| **缺点** | 实现复杂度高，浏览器兼容需 gRPC-Web，调试工具不如 REST 丰富 |
| **适用场景** | Google Gemini API、自建模型推理服务、微服务间 LLM 调用 |
| **数据格式** | Protocol Buffers（二进制，比 JSON 更紧凑） |

### 2.6 通信方式选型决策树

```
                       Agent-LLM 通信场景
                              │
                     ┌────────┴────────┐
                     ▼                 ▼
               需要实时反馈？      不需要实时反馈
                     │                 │
              ┌──────┴──────┐          ▼
              ▼             ▼    同步 HTTP
          需要多轮交互？  单次流式      (Non-Streaming)
              │             │
       ┌──────┴──────┐      ▼
       ▼             ▼    SSE Stream
  高频双工？     低频双工
       │             │
       ▼             ▼
  WebSocket     SSE + 重复 HTTP
  (语音/协作)    (工具调用)
```

### 2.7 协议对比总览

| 维度 | HTTP (Non-Streaming) | HTTP (SSE Streaming) | WebSocket | gRPC |
|------|----------------------|----------------------|-----------|------|
| 传输方向 | 请求-响应 | 服务端→客户端单向流 | 全双工 | 全双工流 |
| 协议基础 | HTTP/1.1+ | HTTP/1.1+ | WS over HTTP | HTTP/2 |
| 首字节延迟 | 高（完整推理） | 低（首 Token） | 低 | 低 |
| 实现复杂度 | 低 | 中 | 高 | 中高 |
| 浏览器兼容 | 原生 | EventSource API | 需库（如 Socket.IO） | 需 gRPC-Web |
| 数据类型 | JSON | 文本（UTF-8） | 文本/二进制 | Protobuf（二进制） |
| 连接开销 | 每次请求独立 | 长连接（持续） | 长连接（持续） | 多路复用 |
| 适用场景 | 短文本/批处理 | 对话/生成 | 实时语音/协作 | 微服务流式推理 |
| 主流采用 | ✅ 通用 | ✅ 通用 | ⚠️ 专用场景 | ⚠️ 部分厂商 |
| 自建成本 | 低 | 低 | 中 | 高（需 Protobuf 维护） |

---

## 3 HTTP 通信协议层详解

当前主流 Agent 客户端与大模型的通信几乎全部基于 **HTTP/1.1** 或 **HTTP/2**，以 RESTful API 的形式承载。HTTPS（TLS 1.2+）是事实上的通信安全标准，所有生产环境通信均需强制使用。

### 3.1 请求头（Request Headers）

请求头字段负责传递请求的元数据，包括认证、内容协商、追踪、缓存控制等。

#### 字段一览

| 字段 | 必选 | 含义与用途 |
|------|------|------------|
| `Authorization: Bearer <token>` | **是** | 身份认证。Bearer Token 通常是 API Key，服务端据此识别调用者身份、计量用量、实施权限控制 |
| `Content-Type: application/json` | **是** | 声明请求体为 JSON 格式，这是绝大多数 LLM API 的请求体编码方式 |
| `Accept: text/event-stream` | 流式时**必选** | 告知服务端客户端期望以 SSE 流式响应，否则服务端默认返回完整 JSON |
| `Accept: application/json` | 非流式时推荐 | 告知服务端期望 JSON 格式响应 |
| `User-Agent` | 推荐 | 标识客户端身份，如 `agent-sdk/1.0`，便于服务端做兼容性处理和监控 |
| `X-Request-Id` / `X-Correlation-Id` | 推荐 | 请求追踪 ID，用于跨系统链路追踪、日志关联和问题排查 |
| `anthropic-version` | Anthropic **必选** | 指定 API 版本号，如 `2023-06-01`，避免后端升级造成破坏性变更 |
| `anthropic-beta` | Anthropic 可选 | 启用 Beta 功能，如 `prompt-caching-2024-07-31` |
| `Cache-Control: no-cache` | 流式时推荐 | 防止代理和网关缓存 SSE 响应，确保每个 Token 即时推送 |
| `Connection: keep-alive` | 推荐 | 复用 TCP 连接，减少多次请求的握手开销 |
| `Origin` / `Referer` | 可选 | 浏览器端跨域请求时的来源标识，服务端据此实施 CORS 策略 |
| `Accept-Encoding: gzip` | 推荐 | 启用响应体压缩，减少传输数据量（非流式场景有效） |
| `X-Api-Version` | 可选 | 显式指定 API 版本号（OpenAI 等厂商支持） |

#### 不同厂商的认证头部差异

| 厂商 | 认证方式 | 示例 |
|------|---------|------|
| OpenAI | `Authorization: Bearer sk-...` | 标准 Bearer Token |
| Anthropic | `x-api-key: sk-ant-...` | 自定义头部，`Authorization` 可选 |
| Azure OpenAI | `api-key: ...` | 自定义头部，或 `Authorization: Bearer` + Entra ID |
| 阿里云通义千问 | `Authorization: Bearer sk-...` | 标准 Bearer Token（兼容 OpenAI 格式） |
| Google Gemini | `x-goog-api-key: ...` | 自定义头部 |
| 百度文心一言 | `Authorization: Bearer <access_token>` | 需先通过 API Key + Secret Key 获取 access_token |

#### 请求头组合示例

```
POST /v1/chat/completions HTTP/1.1
Host: api.openai.com
Authorization: Bearer sk-proj-xxxxxxxxxxxxxxxx
Content-Type: application/json
Accept: text/event-stream
User-Agent: my-agent-sdk/2.1.0
X-Request-Id: req_20240712_a1b2c3d4e5f6
Cache-Control: no-cache
Connection: keep-alive
Accept-Encoding: gzip
X-Api-Version: 2024-02-01
```

### 3.2 请求体（Request Body）关键字段

以聊天补全（Chat Completions）API 为例，展示完整请求体及其各字段含义。

#### 完整 JSON 请求体

```json
{
  "model": "gpt-4o",
  "messages": [
    {
      "role": "system",
      "content": "你是一个专业的 AI 助手，请用中文回答。"
    },
    {
      "role": "user",
      "content": "请解释量子纠缠的原理。"
    }
  ],
  "temperature": 0.7,
  "max_tokens": 1024,
  "top_p": 1.0,
  "frequency_penalty": 0,
  "presence_penalty": 0,
  "stream": true,
  "stream_options": {
    "include_usage": true
  },
  "stop": ["\n---", "User:"],
  "seed": 42,
  "user": "user_abc123",
  "tools": [],
  "tool_choice": "auto",
  "parallel_tool_calls": true,
  "response_format": { "type": "text" }
}
```

#### 字段详细说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | **是** | 指定模型标识符，服务端据此加载对应模型。如 `gpt-4o`、`claude-sonnet-4-20250514` |
| `messages` | array | **是** | 对话上下文序列。每个元素包含 `role` 和 `content`，是模型理解上下文的核心 |
| `temperature` | float | 否 | 采样温度 [0, 2]。越低越确定（趋近贪心解码），越高越随机。默认 1.0 |
| `max_tokens` | integer | 否 | 生成的最大 Token 数上限，控制响应长度 |
| `top_p` | float | 否 | 核采样概率阈值 [0, 1]。模型只从累积概率 ≤ top_p 的 Token 中采样。典型值 0.9-0.95 |
| `frequency_penalty` | float | 否 | 频率惩罚 [-2, 2]。正值降低已出现 Token 的重复概率，负值鼓励重复 |
| `presence_penalty` | float | 否 | 存在惩罚 [-2, 2]。正值鼓励模型谈论新话题/新概念 |
| `stream` | boolean | 否 | 是否启用流式输出。`true` 时响应使用 SSE `text/event-stream` |
| `stream_options` | object | 否 | 流式选项。`{include_usage: true}` 在最后一条事件中包含用量统计 |
| `stop` | array | 否 | 停止序列。遇到列表中任一字符串即终止生成。最大 4 条 |
| `seed` | integer | 否 | 随机种子。在支持时可提供确定性输出（非完全保证） |
| `user` | string | 否 | 用户唯一标识，用于监控和滥用检测 |
| `tools` | array | 否 | 可供模型调用的工具/函数定义列表（遵循 JSON Schema 格式） |
| `tool_choice` | string/object | 否 | 工具调用模式控制 |
| `parallel_tool_calls` | boolean | 否 | 是否允许多个工具同时调用，默认 `true` |
| `response_format` | object | 否 | 指定输出格式，如 `{type: "json_object"}` 强制输出合法 JSON |
| `n` | integer | 否 | 为每条消息生成 n 个候选，默认为 1（非流式） |
| `logprobs` | boolean | 否 | 是否返回 Token 级别的对数概率 |
| `top_logprobs` | integer | 否 | 返回每个位置 top N 个 Token 的概率（需 `logprobs: true`） |
| `repetition_penalty` | float | 否 | Anthropic 等厂商的参数名，类似 frequency_penalty |

#### 消息角色（Message Roles）

| 角色 | 含义 | 典型用途 | 是否可为空 content |
|------|------|----------|-------------------|
| `system` | 系统提示词 | 设定模型的行为、风格、约束。优先级最高 | ❌ |
| `user` | 用户消息 | 用户的提问或指令 | ❌ |
| `assistant` | 助手消息 | 模型的回复，或用于传入历史对话、历史 tool_calls | ✅（工具调用时 content 为 null） |
| `tool` | 工具结果 | 工具函数执行后返回的结果，必须关联 `tool_call_id` | ❌ |

#### 消息格式的两种形式

```json
// 形式一：纯文本消息
{"role": "user", "content": "你好"}

// 形式二：多模态消息（含图片）
{
  "role": "user",
  "content": [
    {"type": "text", "text": "这张图片里有什么？"},
    {
      "type": "image_url",
      "image_url": {
        "url": "data:image/png;base64,iVBORw0KGgo...",
        "detail": "high"
      }
    }
  ]
}
```

多模态消息中，`content` 从字符串变为数组，每个数组元素是一个 `Content Part`，可以嵌套文本、图片、音频等内容块。

#### tools 字段详解

`tools` 数组中的每个工具定义遵循 **JSON Schema** 格式：

```json
{
  "type": "function",
  "function": {
    "name": "search_knowledge",
    "description": "搜索知识库获取实时信息",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "搜索关键词"
        },
        "limit": {
          "type": "integer",
          "description": "返回结果数量",
          "default": 5
        }
      },
      "required": ["query"]
    }
  }
}
```

**`tool_choice` 可能值**：

| 值 | 行为 |
|----------------|------|
| `"none"` | 禁止调用任何工具，模型仅凭自身知识回答 |
| `"auto"` | 模型自行判断是否需要调用工具（默认） |
| `"required"` | 强制模型调用一个工具（无论是否需要） |
| `{"type": "function", "function": {"name": "..."}}` | 指定调用某个特定工具 |

### 3.3 响应状态码（Response Status Codes）

Agent 客户端应正确处理以下状态码，实现优雅的容错和恢复。

| 状态码 | 含义 | 典型处理策略 |
|--------|------|-------------|
| **200 OK** | 请求成功，响应体包含完整结果 | 正常解析响应体 |
| **201 Created** | 资源创建成功（如 Embeddings 或文件上传） | 读取返回的资源 ID |
| **400 Bad Request** | 请求格式错误，参数校验失败 | 检查 messages 格式、model 名称、参数范围等 |
| **401 Unauthorized** | API Key 无效或缺失 | 刷新认证凭据或提示用户 |
| **403 Forbidden** | API Key 无权限访问该资源 | 检查 API Key 权限范围和区域限制 |
| **404 Not Found** | 请求的模型或端点不存在 | 检查 endpoint URL 和 model 名称 |
| **408 Request Timeout** | 请求超时 | 重试（可增加 timeout 配置） |
| **429 Too Many Requests** | 触发速率限制 | **指数退避重试**，等待 `Retry-After` 头指示的时间 |
| **500 Internal Server Error** | 服务端内部错误 | 重试，多次失败则降级 |
| **502 Bad Gateway** | 上游服务不可用 | 等待后重试，切换备用端点 |
| **503 Service Unavailable** | 服务过载或维护中 | 等待后重试，检查服务状态页 |
| **529 Too Many Requests** | Anthropic 特有：服务过载 | 同 429 处理策略 |

#### 错误响应体示例

```json
// 400 Bad Request — 参数校验失败
{
  "error": {
    "message": "Invalid 'max_tokens': value must be between 1 and 4096, got 99999",
    "type": "invalid_request_error",
    "param": "max_tokens",
    "code": null
  }
}

// 401 Unauthorized — 认证失败
{
  "error": {
    "message": "Incorrect API key provided: sk-xxxx. You can find your API key at https://platform.openai.com/account/api-keys.",
    "type": "invalid_request_error",
    "param": null,
    "code": "invalid_api_key"
  }
}

// 429 Too Many Requests — 速率限制
{
  "error": {
    "message": "Rate limit exceeded for requests per minute.",
    "type": "rate_limit_error",
    "param": null,
    "code": "rate_limit_exceeded"
  }
}

// 500 Internal Server Error
{
  "error": {
    "message": "Internal server error. Please try again.",
    "type": "server_error",
    "param": null,
    "code": "internal_error"
  }
}
```

### 3.4 响应头（Response Headers）

响应头携带服务端的处理状态、速率限制信息和使用情况统计。

```
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8     ← 流式
Content-Length: 2834                                ← 非流式
X-Request-Id: req_abc123def456
OpenAI-Organization: org-xxxxxxxx
OpenAI-Processing-Ms: 2341                          ← 服务端处理耗时
X-RateLimit-Limit-Requests: 10000                   ← 请求速率限制上限
X-RateLimit-Remaining-Requests: 9995                ← 剩余请求额度
X-RateLimit-Reset-Requests: 6ms                     ← 额度重置时间
X-RateLimit-Limit-Tokens: 1000000                   ← Token 速率限制上限
X-RateLimit-Remaining-Tokens: 999998                ← 剩余 Token 额度
X-RateLimit-Reset-Tokens: 0s                        ← Token 额度重置时间
Retry-After: 30                                     ← 429 时指示等待秒数
Cache-Control: no-store                             ← 防止缓存敏感/时序数据
```

#### 响应头字段详解

| 响应头 | 说明 |
|--------|------|
| `Content-Type` | 指示响应编码格式：`application/json`（非流式）或 `text/event-stream`（流式） |
| `Content-Length` | 响应体长度（字节），仅非流式响应包含 |
| `X-Request-Id` | 服务端请求标识，用于日志关联和问题排查 |
| `X-Request-Id` 回显 | 客户端可用于验证请求是否被正确路由 |
| `OpenAI-Processing-Ms` | 服务端处理耗时（毫秒），对性能监控很有价值 |
| `X-RateLimit-*` | 速率限制信息，客户端应据此调整请求频率 |
| `Retry-After` | 429 时指示客户端等待秒数后再重试 |
| `Cache-Control: no-store` | 禁止中间节点缓存响应内容 |
| `anthropic-ratelimit-*` | Anthropic 的速率限制头部（命名不同） |
| `x-khulnasoft-*` | 其他 AI 代理/网关的自定义头部 |
| `OpenAI-Version` | 服务端 API 版本标识 |
| `Strict-Transport-Security` | 强制 HTTPS（`max-age=31536000`） |

#### 不同厂商速率限制头部对比

| 厂商 | Requests 限流头 | Tokens 限流头 |
|------|----------------|--------------|
| OpenAI | `X-RateLimit-Limit-Requests` | `X-RateLimit-Limit-Tokens` |
| Anthropic | `anthropic-ratelimit-requests-limit` | `anthropic-ratelimit-tokens-limit` |
| Azure OpenAI | `x-ratelimit-remaining-requests` | `x-ratelimit-remaining-tokens` |

### 3.5 响应体详解

#### 3.5.1 非流式完整响应（200 OK）

```json
{
  "id": "chatcmpl-9zZzZzZzZzZzZzZzZzZzZz",
  "object": "chat.completion",
  "created": 1720771200,
  "model": "gpt-4o",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "量子纠缠是量子力学中一种特殊的现象..."
      },
      "finish_reason": "stop",
      "logprobs": null
    }
  ],
  "usage": {
    "prompt_tokens": 32,
    "completion_tokens": 128,
    "total_tokens": 160,
    "prompt_tokens_details": {
      "cached_tokens": 0
    }
  },
  "system_fingerprint": "fp_abc123"
}
```

| 字段 | 说明 |
|------|------|
| `id` | 本次生成的唯一标识符，可用于后续关联和问题排查 |
| `object` | 对象类型，`chat.completion` 表示完整响应 |
| `created` | 创建时间的 Unix 时间戳（秒级） |
| `model` | 实际使用的模型标识（可能不同于请求中的 model，如自动路由到最新快照） |
| `choices[]` | 生成结果列表（`n`>1 时有多个候选，通常 `n=1`） |
| `choices[].message.content` | 模型生成的文本内容（工具调用时为 null） |
| `choices[].finish_reason` | 终止原因 |
| `choices[].logprobs` | 对数概率信息（请求时设置 `logprobs` 才有） |
| `usage` | Token 用量统计，用于计费和监控 |
| `usage.prompt_tokens_details.cached_tokens` | 命中的缓存 Token 数（某些厂商支持） |
| `system_fingerprint` | 服务端部署指纹，用于识别模型配置变更 |

**`finish_reason` 枚举值**：

| finish_reason | 含义 | 后续处理 |
|---------------|------|----------|
| `stop` | 模型正常结束生成（遇到 stop 序列或自然结束） | ✅ 输出结果 |
| `length` | 达到 `max_tokens` 上限 | ⚠️ 截断或截断后继续请求 |
| `tool_calls` | 模型请求调用工具 | 🔧 执行工具后回传结果 |
| `content_filter` | 内容被过滤策略拦截（部分内容被截断） | ⚠️ 提示用户调整输入 |
| `null` | 流式中间片段的完结状态为 null | 🔄 继续接收后续事件 |

#### 3.5.2 流式响应（SSE 事件流）

流式模式下，服务端返回 `Content-Type: text/event-stream`，每个事件以 `data:` 开头，事件间用空行分隔。

**事件流结构**：

```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"量子"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"纠缠"},"finish_reason":null}]}

...

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":32,"completion_tokens":128,"total_tokens":160}}

data: [DONE]
```

**流式事件的核心要点**：

1. **首事件** —— `delta.role` 只在第一个事件中出现（值为 `"assistant"`），用于初始化角色
2. **增量传输** —— `delta.content` 包含当前分片的**增量文本**（非完整累积文本），客户端需逐片拼接
3. **终止信号** —— `finish_reason` 在最后一个事件中赋值为 `"stop"` / `"length"` / `"tool_calls"`，此前为 `null`
4. **用量信息** —— 若设置了 `stream_options: {include_usage: true}`，最终事件包含 `usage` 字段
5. **结束标记** —— 最后收到 `data: [DONE]` 标记流结束
6. **`id` 和 `retry`** —— 事件间可能包含可选的事件 ID 和重连间隔

**完整 SSE 规范格式**（含可选字段）：

```
id: event-001
retry: 3000
data: {"content":"Hello"}

id: event-002
data: {"content":" World"}

```

#### 3.5.3 工具调用响应（tool_calls）

当模型决定调用工具时，流式和非流式两种模式的事件格式有所不同。

**非流式响应**：`finish_reason` 为 `"tool_calls"`，响应中包含 `tool_calls` 数组：

```json
{
  "id": "chatcmpl-xxx",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_abc123",
          "type": "function",
          "function": {
            "name": "search_knowledge",
            "arguments": "{\"query\": \"量子纠缠 最新研究 2024\"}"
          }
        }
      ]
    },
    "finish_reason": "tool_calls"
  }]
}
```

**流式响应**：tool_calls 的 name 和 arguments 也是增量传输的，客户端需要手动拼接：

```
data: {"choices":[{"index":0,"delta":{"role":"assistant","content":null},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_abc123","type":"function","function":{"name":"search_knowledge","arguments":""}}]},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"query\":\""}}]},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"量子"}}]},"finish_reason":null}]}

...

data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}

data: [DONE]
```

**客户端处理 tool_calls 的步骤**：

```
1. 将所有 delta.tool_calls 按 index 分组
2. 对每个分片的 function.arguments 做字符串拼接
3. 对每个分片的 function.name 取值（非流式时直接获取）
4. 将拼接后的完整 JSON 字符串反序列化为工具调用参数
5. 根据 name 路由到对应的工具处理器
6. 执行工具函数，得到结果
7. 构造 tool 角色消息，回传给模型
```

### 3.6 结构化输出（Structured Outputs / JSON Mode）

OpenAI 等厂商支持结构化输出，保证模型返回合法 JSON。这在与数据库、表单系统等需要严格数据类型交互的场景中至关重要。

#### JSON 模式（简单）

```json
{
  "model": "gpt-4o",
  "response_format": {
    "type": "json_object"
  },
  "messages": [
    {
      "role": "user",
      "content": "从以下文本中提取信息并以 JSON 返回：{}"
    }
  ]
}
```

#### JSON Schema 模式（严格）

```json
{
  "model": "gpt-4o-2024-08-06",
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "extract_people",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "people": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "name": { "type": "string" },
                "age": { "type": "integer" }
              },
              "required": ["name", "age"],
              "additionalProperties": false
            }
          }
        },
        "required": ["people"],
        "additionalProperties": false
      }
    }
  }
}
```

> 开启 `strict: true` 时，`additionalProperties` 必须显式设为 `false`，所有字段必须有描述。

### 3.7 多模态内容传输

现代 LLM（如 GPT-4o、Gemini 1.5 Pro）支持多模态输入（文本 + 图片 + 音频）。图片通常通过 `data URI`（Base64 内联）或可公开访问的 `URL` 传输。

**Base64 内联**（小文件，< 20MB）：

```json
{
  "model": "gpt-4o",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "这张图中显示了什么症状？"},
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/jpeg;base64,/9j/4AAQ...",
            "detail": "high"
          }
        }
      ]
    }
  ],
  "max_tokens": 300
}
```

**URL 引用**（大型文件/不重复传输）：

```json
{
  "type": "image_url",
  "image_url": {
    "url": "https://example.com/images/symptom.jpg",
    "detail": "auto"
  }
}
```

**数据传输方案对比**：

| 方式 | 说明 | 适用场景 | 优缺点 |
|------|------|----------|--------|
| Base64 内联 | 直接编码在请求体中 | 小文件 (< 20MB)，无需额外存储 | ✅ 简单直接 ❌ 增加请求体大小（膨胀约 33%） |
| URL 引用 | 提供可访问的 URL | 已有公开或预签名 URL 的大文件 | ✅ 不增加请求体大小 ❌ 需确保 URL 可达 |
| 文件上传 API | 先上传获取文件 ID，再引用 | 需重复使用的大文件 | ✅ 文件一次上传多次引用 ❌ 多一步上传流程 |

---

## 4 客户端 SDK 核心实现模式

### 4.1 请求构建层

```python
# 伪代码：Agent SDK 的请求构建流程
class LLMRequestBuilder:
    """构建 LLM API 请求的核心构建器"""

    def __init__(self, config: LLMConfig):
        self.config = config

    def build_chat_request(
        self,
        messages: list,
        tools: list = None,
        stream: bool = False,
        response_format: dict = None,
    ) -> dict:
        """构建聊天补全请求体"""
        request = {
            "model": self.config.model,
            "messages": self._format_messages(messages),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
        }

        if tools:
            request["tools"] = self._format_tools(tools)
            request["tool_choice"] = self.config.tool_choice or "auto"
            request["parallel_tool_calls"] = self.config.parallel_tool_calls

        if response_format:
            request["response_format"] = response_format

        return request

    def build_headers(self, stream: bool = False) -> dict:
        """构建 HTTP 请求头"""
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": self.config.user_agent or "agent-sdk/1.0",
            "X-Request-Id": generate_request_id(),
        }

        if stream:
            headers["Accept"] = "text/event-stream"
            headers["Cache-Control"] = "no-cache"

        if self.config.api_version:
            headers["X-Api-Version"] = self.config.api_version

        return headers

    def _format_messages(self, messages: list) -> list:
        """格式化消息，注入 system prompt 等"""
        formatted = []
        for msg in messages:
            if isinstance(msg.content, str):
                formatted.append({"role": msg.role, "content": msg.content})
            else:
                # 多模态内容（content 为 ContentPart 数组）
                formatted.append({"role": msg.role, "content": msg.content_parts})
        return formatted
```

### 4.2 传输层（HTTP 调用）

```python
# 流式传输：使用 httpx（支持 HTTP/2 和连接池）
import httpx
import json
import asyncio

class LLMClient:
    """LLM API 客户端，封装传输层细节"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=50),
            http2=True,  # 启用 HTTP/2
        )
        self.rate_limiter = TokenBucketRateLimiter(
            max_rpm=config.max_rpm,
            max_tpm=config.max_tpm,
        )

    async def stream_chat(self, request: dict) -> AsyncGenerator[dict, None]:
        """流式聊天补全"""
        headers = self._build_headers(stream=True)
        url = f"{self.config.base_url}/v1/chat/completions"

        await self.rate_limiter.acquire()

        try:
            async with self.client.stream(
                "POST", url, json=request, headers=headers,
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    await self._handle_error(response.status_code, error_body)
                    return

                buffer = ""
                async for raw_chunk in response.aiter_bytes():
                    buffer += raw_chunk.decode("utf-8")
                    events = self._parse_sse(buffer)
                    buffer = events["remainder"]
                    for event in events["parsed"]:
                        if event == "[DONE]":
                            return
                        yield json.loads(event)
        except httpx.TimeoutException:
            await self._handle_timeout()
        except httpx.NetworkError:
            await self._handle_network_error()

    async def non_stream_chat(self, request: dict) -> dict:
        """非流式聊天补全"""
        headers = self._build_headers(stream=False)
        url = f"{self.config.base_url}/v1/chat/completions"

        await self.rate_limiter.acquire()

        response = await self.client.post(url, json=request, headers=headers)
        if response.status_code != 200:
            await self._handle_error(response.status_code, response.content)
        return response.json()
```

### 4.3 中间件/拦截器模式

Agent 客户端的 HTTP 调用层可实现中间件链，用于横切关注点（日志、认证、重试、监控、缓存、熔断）：

```python
class MiddlewareChain:
    """HTTP 中间件链，类似 Koa/Express 的洋葱模型"""

    def __init__(self):
        self.middlewares = []

    def use(self, middleware):
        """注册中间件: middleware(request, next) -> response"""
        self.middlewares.append(middleware)

    async def execute(self, request):
        """执行中间件链"""

        async def runner(index):
            if index < len(self.middlewares):
                return await self.middlewares[index](request, lambda: runner(index + 1))
            # 最终处理器：实际发送 HTTP 请求
            return await self._send_http(request)

        return await runner(0)


# 中间件示例
async def logging_middleware(request, next):
    """请求日志中间件"""
    start = time.monotonic()
    request_id = request.headers.get("X-Request-Id", "unknown")
    logger.info(f"[{request_id}] → {request.method} {request.url}")

    try:
        response = await next()
        elapsed = (time.monotonic() - start) * 1000
        logger.info(f"[{request_id}] ← {response.status_code} ({elapsed:.0f}ms)")
        return response
    except Exception as e:
        logger.error(f"[{request_id}] ✗ {type(e).__name__}: {e}")
        raise


async def retry_middleware(request, next):
    """自动重试中间件（指数退避）"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = await next()
            if response.status_code < 500 or attempt == max_retries - 1:
                return response
            wait = 2 ** attempt
            await asyncio.sleep(wait)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)
```

### 4.4 错误处理与重试策略

```python
import asyncio
import random

async def request_with_retry(client, url, json, headers, max_retries=3):
    """带重试机制的 HTTP 请求"""
    for attempt in range(max_retries):
        response = await client.post(url, json=json, headers=headers)

        if response.status_code == 200:
            return response

        # 客户端错误（4xx）— 除 429 外不重试
        if 400 <= response.status_code < 500 and response.status_code != 429:
            error_body = await response.text()
            raise APIError(
                status_code=response.status_code,
                message=error_body,
            )

        # 429 Rate Limit — 使用 Retry-After + jitter
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 2 ** attempt))
            wait = retry_after + random.uniform(0, 1)
            await asyncio.sleep(wait)
            continue

        # 服务端错误（5xx）— 指数退避 + jitter
        if response.status_code >= 500:
            wait = min(2 ** attempt + random.uniform(0, 1), 60)
            await asyncio.sleep(wait)
            continue

        raise APIError(
            status_code=response.status_code,
            message=f"Unexpected status code: {response.status_code}",
        )

    raise MaxRetriesExceededError(f"Failed after {max_retries} retries")


class APIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")
```

#### 重试决策表

| 条件 | 重试？ | 策略 |
|------|--------|------|
| 429 (Rate Limit) | ✅ 是 | 读取 `Retry-After` + 随机 jitter |
| 500 / 502 / 503 | ✅ 是 | 指数退避（2^n），最多 3-5 次 |
| 408 (超时) | ✅ 是 | 短超时重试 1 次，长超时报错 |
| 400 / 401 / 403 / 404 | ❌ 否 | 直接报错给上层，不重试 |
| 网络错误 / DNS 错误 | ✅ 是 | 指数退避，检查备用端点 |
| 连接重置 | ✅ 是 | 重建连接后重试 |

### 4.5 速率限制管理（Rate Limiter）

```python
from collections import deque
import time
import asyncio

class TokenBucketRateLimiter:
    """基于滑动窗口的速率限制器（请求级 + Token 级）"""

    def __init__(self, max_rpm=60, max_tpm=100000):
        self.max_rpm = max_rpm                # 每分钟最大请求数
        self.max_tpm = max_tpm                # 每分钟最大 Token 数
        self.request_timestamps = deque()     # 请求时间戳队列
        self.token_usage = deque()            # Token 消耗队列 [(timestamp, count)]

    async def acquire(self, estimated_tokens=0):
        """等待直到速率限制允许发送请求"""
        now = time.monotonic()

        # 清理 60 秒前的过期记录
        while self.request_timestamps and now - self.request_timestamps[0] > 60:
            self.request_timestamps.popleft()
        while self.token_usage and now - self.token_usage[0][0] > 60:
            self.token_usage.popleft()

        # 检查请求速率
        if len(self.request_timestamps) >= self.max_rpm:
            sleep = self.request_timestamps[0] + 60 - now
            await asyncio.sleep(max(0, sleep))

        # 检查 Token 速率
        total_tokens = sum(t for _, t in self.token_usage)
        if total_tokens + estimated_tokens > self.max_tpm:
            sleep = self.token_usage[0][0] + 60 - now
            await asyncio.sleep(max(0, sleep))

        self.request_timestamps.append(time.monotonic())
        self.token_usage.append((time.monotonic(), estimated_tokens))
```

### 4.6 SSE 解析器（含边界情况处理）

```python
def parse_sse_events(buffer: str) -> dict:
    """解析 Server-Sent Events 格式的数据

    严格按照 W3C SSE 规范解析：
    - data: 字段可以跨行（用空行结束事件）
    - id: 字段记录事件 ID
    - retry: 字段设置重连间隔
    - 注释行（以 : 开头）被忽略

    Args:
        buffer: 原始字符串缓冲区

    Returns:
        {"parsed": [...], "remainder": "未完成行..."}
    """
    events = []
    lines = buffer.split("\n")
    remaining_lines = []
    current_data_lines = []
    current_id = None
    current_retry = None

    for line in lines:
        if line.startswith("data: "):
            current_data_lines.append(line[6:])
        elif line.startswith("data:"):
            # data: 后面无空格但跟了内容的边缘情况
            current_data_lines.append(line[5:])
        elif line == "data:":
            # data: 后面无内容 — 仍然是有效事件，但 content 为空
            current_data_lines.append("")
        elif line.startswith("id: "):
            current_id = line[4:]
        elif line.startswith("id:"):
            current_id = line[3:]
        elif line.startswith("retry: "):
            current_retry = int(line[7:])
        elif line.startswith(":"):
            # 注释行，跳过
            continue
        elif line == "":
            # 空行标记事件结束
            if current_data_lines:
                data = "\n".join(current_data_lines)
                events.append(data)
            current_data_lines = []
            current_id = None
            current_retry = None
        else:
            # 不完整行，保留到下次处理
            remaining_lines.append(line)
            continue

    # 将未关闭的事件数据行保留到下次解析
    return {
        "parsed": events,
        "remainder": "\n".join(remaining_lines),
    }
```

### 4.7 熔断器模式（Circuit Breaker）

熔断器防止连续失败造成级联故障，在与 LLM API 交互时尤为重要（API 不稳定期间快速失败，而非浪费资源重试）。

```python
import time
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"       # 正常工作
    OPEN = "open"           # 熔断开启，直接拒绝请求
    HALF_OPEN = "half_open" # 半开状态，允许试探请求

class CircuitBreaker:
    """熔断器，防止连续失败造成级联故障"""

    def __init__(self, failure_threshold=5, recovery_timeout=30, half_open_max=1):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0
        self.half_open_attempts = 0

    async def call(self, func, *args, **kwargs):
        """执行被熔断保护的调用"""
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.last_failure_time >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.half_open_attempts = 0
            else:
                raise CircuitBreakerOpenError("Circuit breaker is open")

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure(e)
            raise

    def _on_success(self):
        if self.state == CircuitState.HALF_OPEN:
            self.half_open_attempts += 1
            if self.half_open_attempts >= self.half_open_max:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
        else:
            self.failure_count = 0

    def _on_failure(self, exception):
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
```

---

## 5 完整端到端交互示例

### 场景：Agent 助手回答用户关于「今日天气」的问题，并调用天气 API 获取实时数据

本节展示一个完整的 Agent-LLM 交互过程，涵盖：请求构建 → 工具调用 → 工具执行 → 结果回传 → 最终生成。

#### Step 1：用户发起请求

```
用户: "北京今天天气怎么样？"
```

#### Step 2：Agent 客户端构建请求

客户端将用户消息封装为 API 请求，设置适当的 HTTP 头和请求体：

**HTTP 完整请求报文：**

```
POST /v1/chat/completions HTTP/1.1
Host: api.openai.com
Authorization: Bearer sk-xxxxxxxxxxxxxxxxxxxxxxxx
Content-Type: application/json
Accept: text/event-stream
User-Agent: weather-agent/1.0.0
X-Request-Id: req_20240712_abcdef
Cache-Control: no-cache
Connection: keep-alive
Accept-Encoding: gzip

{
  "model": "gpt-4o",
  "messages": [
    {
      "role": "system",
      "content": "你是一个天气助手。当用户询问天气时，调用 get_weather 函数获取数据，然后基于返回数据生成回答。回答时用中文给出完整天气描述。"
    },
    {
      "role": "user",
      "content": "北京今天天气怎么样？"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "获取指定城市的实时天气信息",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {
              "type": "string",
              "description": "城市名称，如北京、上海"
            },
            "date": {
              "type": "string",
              "description": "日期，格式 YYYY-MM-DD，默认今天"
            }
          },
          "required": ["city"]
        }
      }
    }
  ],
  "tool_choice": "auto",
  "parallel_tool_calls": false,
  "temperature": 0.3,
  "max_tokens": 1024,
  "stream": true,
  "seed": 42
}
```

#### Step 3：模型识别需要调用工具，流式返回 tool_calls

**响应头：**

```
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
X-Request-Id: req_20240712_abcdef
OpenAI-Processing-Ms: 987
Cache-Control: no-store
```

**SSE 事件流（tool_calls 分片传输）：**

```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":null,"tool_calls":[{"id":"call_weather_001","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"city\":\""}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"北京"}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"}"}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}

data: [DONE]
```

#### Step 4：客户端解析 tool_calls，执行本地工具函数

```python
# 客户端收集所有 tool_calls 分片，按 index 合并
tool_call_fragments = {
    0: {
        "id": "call_weather_001",
        "type": "function",
        "function": {
            "name": "get_weather",
            "arguments": '{"city":"北京"}'  # 拼接完成
        }
    }
}

# 工具处理器注册表
TOOL_HANDLERS = {
    "get_weather": lambda args: call_weather_api(**args),
    "search_knowledge": lambda args: search_knowledge_base(**args),
}

def execute_tool_calls(tool_calls: list) -> list:
    """执行工具调用并返回结果"""
    results = []
    for tc in tool_calls:
        handler = TOOL_HANDLERS.get(tc["function"]["name"])
        if not handler:
            raise UnknownToolError(f"Unknown tool: {tc['function']['name']}")

        arguments = json.loads(tc["function"]["arguments"])
        result = handler(arguments)

        results.append({
            "tool_call_id": tc["id"],
            "output": json.dumps(result, ensure_ascii=False),
        })
    return results

# 执行 get_weather 工具
tool_results = execute_tool_calls(tool_call_fragments.values())
# tool_results[0]["output"]:
#   '{"temperature": 32, "condition": "晴", "humidity": 45, "wind": "东南风 3级", "city": "北京"}'
```

#### Step 5：客户端将工具结果回传给模型

客户端将原始请求 + tool_calls 响应 + 工具结果拼接为新一轮请求：

```
POST /v1/chat/completions HTTP/1.1
Authorization: Bearer sk-xxxxxxxxxxxxxxxxxxxxxxxx
X-Request-Id: req_20240712_abcdefg
Content-Type: application/json
Accept: text/event-stream

{
  "model": "gpt-4o",
  "messages": [
    {"role": "system", "content": "你是一个天气助手。当用户询问天气时，调用 get_weather 函数获取数据，然后基于返回数据生成回答。"},
    {"role": "user", "content": "北京今天天气怎么样？"},
    {"role": "assistant", "content": null,
     "tool_calls": [
       {"id": "call_weather_001", "type": "function",
        "function": {"name": "get_weather", "arguments": "{\"city\": \"北京\"}"}}
     ]
    },
    {"role": "tool",
     "tool_call_id": "call_weather_001",
     "content": "{\"temperature\": 32, \"condition\": \"晴\", \"humidity\": 45, \"wind\": \"东南风 3级\", \"city\": \"北京\"}"
    }
  ],
  "tools": [
    {"type": "function", "function": {
      "name": "get_weather",
      "description": "获取指定城市的实时天气信息",
      "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "date": {"type": "string"}}, "required": ["city"]}
    }}
  ],
  "stream": true,
  "temperature": 0.3
}
```

> **关键点**：`tool` 角色的 `content` 必须是字符串（通常是 JSON 序列化后的结果），`tool_call_id` 必须与模型返回的 `tool_calls[].id` 严格一致。

#### Step 6：模型基于工具结果生成最终回答（流式）

```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"北京"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"今天"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"天气"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"晴朗"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"，"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"气温"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"32"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"°C"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"，"}},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"东南风"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"3"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"级"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"，"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"湿度"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"45"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"%"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"。"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1720771200,"model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":89,"completion_tokens":38,"total_tokens":127}}

data: [DONE]
```

#### Step 7：客户端拼接流式结果并展示

```python
class StreamingResponseHandler:
    """流式响应处理器，负责拼接增量内容"""

    def __init__(self):
        self.accumulated_content = ""
        self.finish_reason = None
        self.usage = None
        self.on_token = None      # 每 Token 回调（用于实时 UI 更新）
        self.on_done = None       # 完成回调

    def process_chunk(self, chunk: dict):
        """处理一个 SSE 事件 chunk"""
        if not chunk.get("choices"):
            # 可能包含 usage 的最终事件
            if chunk.get("usage"):
                self.usage = chunk["usage"]
            return

        choice = chunk["choices"][0]
        delta = choice.get("delta", {})

        # 累积增量文本
        content = delta.get("content", "")
        if content:
            self.accumulated_content += content
            if self.on_token:
                self.on_token(content)

        # 检查终止原因
        finish = choice.get("finish_reason")
        if finish:
            self.finish_reason = finish
            if finish == "tool_calls":
                # 进入工具调用分支
                self._handle_tool_calls(delta)

        if self.usage:
            if self.on_done:
                self.on_done(self.accumulated_content, self.finish_reason, self.usage)

    def get_full_response(self) -> str:
        return self.accumulated_content


# 使用示例
handler = StreamingResponseHandler()
handler.on_token = lambda token: print(token, end="", flush=True)
handler.on_done = lambda text, reason, usage: print(f"\n\n[完成] {reason}, 用量: {usage}")

# 处理每个 SSE 事件
async for chunk in client.stream_chat(request):
    handler.process_chunk(chunk)
```

#### Step 8：完整对话链路时序图

```
用户                    Agent 客户端                   LLM API                 天气 API
 │                          │                          │                       │
 │ "北京天气怎么样？"        │                          │                       │
 │─────────────────────────▶│                          │                       │
 │                          │  POST /chat/completions  │                       │
 │                          │  含 tools 定义 + stream  │                       │
 │                          │─────────────────────────▶│                       │
 │                          │                          │                       │
 │                          │  SSE: tool_calls(分片)   │                       │
 │                          │◄─────────────────────────│                       │
 │                          │                          │                       │
 │                          │  解析 tool_calls         │                       │
 │                          │  调用 get_weather("北京") │                       │
 │                          │─────────────────────────────────────────────────▶│
 │                          │                          │                       │
 │                          │           天气数据        │                       │
 │                          │◄─────────────────────────────────────────────────┤
 │                          │                          │                       │
 │                          │  POST /chat/completions  │                       │
 │                          │  追加 tool 结果          │                       │
 │                          │─────────────────────────▶│                       │
 │                          │                          │                       │
 │                          │  SSE: 增量生成回答       │                       │
 │                          │◄─────────────────────────│                       │
 │                          │                          │                       │
 │  "北京今天天气晴朗..."    │                          │                       │
 │◄─────────────────────────│                          │                       │
```

### 容错场景示例

#### 场景 A：触发速率限制（429）

```
请求:
POST /v1/chat/completions
X-Request-Id: req_001

响应:
HTTP/1.1 429 Too Many Requests
Retry-After: 30
X-RateLimit-Remaining-Requests: 0

{
  "error": {
    "message": "Rate limit exceeded for requests per minute.",
    "type": "rate_limit_error",
    "code": "rate_limit_exceeded"
  }
}
```

**客户端处理**：

```python
if response.status_code == 429:
    retry_after = int(response.headers.get("Retry-After", 30))
    jitter = random.uniform(0, min(5, retry_after * 0.1))
    wait = retry_after + jitter
    logger.warning(f"Rate limited. Waiting {wait:.1f}s before retry...")
    await asyncio.sleep(wait)
    return await self.execute(request)  # 重试原请求
```

#### 场景 B：服务端临时故障（502）

```
请求:
POST /v1/chat/completions
X-Request-Id: req_002

响应:
HTTP/1.1 502 Bad Gateway
```

**客户端处理**：

```python
if response.status_code >= 500:
    attempt = getattr(request, "_retry_count", 0)

    if attempt < MAX_RETRIES:
        wait = min(2 ** attempt + random.uniform(0, 1), 30)
        logger.warning(f"Server error (502). Retry {attempt+1}/{MAX_RETRIES} in {wait:.1f}s")

        request._retry_count = attempt + 1
        await asyncio.sleep(wait)

        # 切换到备用端点（如果有）
        if self.config.fallback_base_url:
            request.url = request.url.replace(
                self.config.base_url, self.config.fallback_base_url
            )

        return await self.execute(request)
    else:
        raise ServiceDegradedError("All retries failed for 502")
```

---

## 6 MCP（Model Context Protocol）—— 新一代 Agent 通信协议

### 6.1 概述

MCP（Model Context Protocol）是 Anthropic 提出的开源协议标准，旨在为 AI 模型与外部工具/数据源之间建立统一的通信接口。类比于 USB-C 为外设提供的标准化接口，MCP 为 AI 应用与数据源之间提供了标准化的连接方式。

```
┌─────────────────────────────────────┐
│          AI 应用（Host）              │
│  ┌───────────────────────────────┐   │
│  │  MCP 客户端                    │   │
│  └──────────┬────────────────────┘   │
│             │ MCP 协议               │
└─────────────┼───────────────────────┘
              │
    ┌─────────┴─────────┐
    │                   │
┌───▼────┐        ┌────▼───┐
│ MCP     │        │ MCP    │
│ 服务端 A │        │ 服务端 B│
│ (数据库) │        │ (API)  │
└─────────┘        └────────┘
```

### 6.2 MCP 的核心概念

| 概念 | 说明 | 类比 |
|------|------|------|
| **Host** | 运行 MCP 客户端的 AI 应用（如 Claude Desktop、IDE 插件） | 操作系统 |
| **Client** | 与服务端建立 1:1 连接的协议客户端 | USB 控制器 |
| **Server** | 对外暴露工具、资源和提示词的 MCP 服务端 | USB 设备驱动 |
| **Transport** | 通信传输层，支持 stdio（进程内）和 SSE（远程） | 物理连接线 |
| **Capability** | 服务端声明的能力（tools、resources、prompts） | 设备功能声明 |
| **Initialization** | 启动时通过 `initialize` → `initialized` 完成能力协商 | 设备握手 |

### 6.3 MCP 的传输方式

**方式一：stdio Transport（进程内通信）**

```json
{
  "mcpServers": {
    "my-db": {
      "command": "node",
      "args": ["mcp-server.js"],
      "env": {
        "DB_HOST": "localhost"
      }
    }
  }
}
```

- 客户端通过子进程的 stdin/stdout 与服务端通信
- 使用 JSON-RPC 2.0 作为消息格式
- 低延迟（无需网络开销）
- 适合本地工具、数据库连接、文件系统操作

**方式二：SSE Transport（远程通信）**

```json
{
  "mcpServers": {
    "my-api": {
      "url": "https://api.example.com/mcp"
    }
  }
}
```

- 服务端暴露 SSE 端点，供远端客户端连接
- 客户端先通过 HTTP POST 发送 JSON-RPC 请求，服务端通过 SSE 推送结果
- 适合远程 API 调用、分布式系统中的能力暴露

### 6.4 MCP 的消息格式

MCP 基于 JSON-RPC 2.0，消息格式如下：

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "method": "tools/call",
  "params": {
    "name": "get_weather",
    "arguments": {
      "city": "北京"
    }
  }
}
```

**初始化流程**：

```
客户端                         服务端
  │                              │
  ├── initialize ────────────────►
  │   {protocolVersion,           │
  │    capabilities}              │
  │                              │
  ◄── initialized ──────────────┤
  │   {protocolVersion,           │
  │    capabilities,              │
  │    serverInfo}                │
  │                              │
  ├── initialized 通知 ──────────►
  │   (通知客户端初始化完成)        │
  │                              │
  ├── tools/list ────────────────►  获取工具列表
  ├── resources/list ────────────►  获取资源列表
  ├── prompts/list ──────────────►  获取提示词模板
```

### 6.5 MCP 与传统 HTTP API 的对比

| 维度 | 传统 LLM API | MCP |
|------|-------------|-----|
| 通信协议 | HTTP REST | JSON-RPC 2.0 over stdio/SSE |
| 能力发现 | 文档手动配置 | `tools/list`、`resources/list` 自动发现 |
| 工具注册 | 在 LLM 请求体中硬编码 tools | MCP 服务端声明，动态注册 |
| 传输层 | HTTP/HTTPS | stdio（本地）/ SSE（远程）|
| 标准化程度 | 各厂商差异大 | 统一标准 |
| 适用场景 | 客户端 ↔ LLM 直连 | Agent ↔ 工具/数据源 间连接 |
| 协议目标 | 模型推理 | 工具集成与能力暴露 |

### 6.6 在 Agent 架构中的位置

MCP 并非要替代 LLM API 协议，而是**补充** Agent 的工具接入层：

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  用户/UI 层   │────▶│ Agent 编排层  │────▶│  LLM API     │
│              │◀────│ (协调+路由)   │◀────│  服务端       │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
                    ┌───────┴────────┐
                    │  MCP Client     │
                    │  (工具接入层)    │
                    └───┬───────┬────┘
                        │       │
                  ┌─────▼─┐ ┌───▼────┐
                  │ MCP   │ │ MCP    │
                  │ 服务端A│ │ 服务端 B│
                  │ (DB)  │ │ (API)  │
                  └───────┘ └────────┘
```

---

## 7 关键设计考量

### 7.1 连接管理

- **连接复用**：使用 `Connection: keep-alive` 复用 TCP 连接，避免多次 TLS 握手开销。典型场景下 HTTPS 握手耗时 100-500ms，keep-alive 可显著降低延迟
- **连接池**：客户端维护连接池，默认大小通常为 10-50，根据并发请求数合理配置。过高可能导致端口耗尽
- **HTTP/2 多路复用**：在单个连接上并行处理多个请求，消除 HTTP/1.1 的头对头阻塞

**超时设置参考**：

| 超时类型 | 非流式 | 流式 | 说明 |
|---------|--------|------|------|
| `connect_timeout` | 10s | 10s | TCP 连接建立超时 |
| `read_timeout` | 60s | 300s | 等待响应的超时（流式更长） |
| `write_timeout` | 30s | 30s | 请求体发送超时 |
| `pool_timeout` | 10s | 10s | 从连接池获取连接的超时 |

### 7.2 认证与安全

- **安全传输**：API Key 通过 `Authorization: Bearer` 头传递，**绝不可出现在 URL 或请求体中**
- **传输加密**：所有通信强制使用 HTTPS（TLS 1.2+），防止中间人攻击和窃听
- **密钥管理**：
  - API Key 应存储在环境变量或密钥管理服务（如 AWS Secrets Manager、Vault、Azure Key Vault）中
  - 不可硬编码在代码中，不可提交到版本控制
  - 支持多 Key 轮换机制，Key 泄露时能快速更换
- **请求验证**：对服务端响应做完整性校验（如验证 `X-Request-Id` 回显一致性）
- **IP 白名单**：对使用自有 API Key 的客户端，建议配置 IP 白名单限制 API Key 的可使用 IP 范围

### 7.3 重试与容错

| 错误类型 | 处理策略 | 备注 |
|---------|---------|------|
| 429 (Rate Limit) | 读取 `Retry-After` 头，指数退避 + jitter | 同时配合客户端侧速率限制预防 |
| 5xx (服务端错误) | 指数退避重试，最多 3-5 次 | 可配置备用端点做故障转移 |
| 网络超时 | 短连接超时重试 1 次，长超时直接降级 | 区分 connect / read / write 超时 |
| 400 / 401 / 403 | **不重试**，直接报错给上层 | 修改输入或凭据后重试 |
| 连接断开（流式） | 断点续传（记录已收到的内容位置） | 需应用层支持 continue 参数 |

### 7.4 流式传输的最佳实践

1. **缓冲区管理**：使用环形缓冲区处理 SSE 数据，避免内存无限增长
2. **心跳检测**：设置 30s 空闲超时，配合 `data: [DONE]` 检测流是否意外终止
3. **降级策略**：当服务端不支持流式时，客户端自动降级为非流式模式
4. **速率预判**：通过 `X-RateLimit-Remaining-*` 头部预判是否即将被限流，主动降速
5. **部分结果保留**：流中断时保留已收到的文本，断连重连后从中断处继续（若 API 支持）

### 7.5 监控与可观测性

```
关键指标:
  - TTFB (Time to First Byte):       衡量服务端响应速度（流式=首 Token 延迟）
  - ITL (Inter-Token Latency):       流式场景下相邻 Token 之间的延迟间隔
  - Token 吞吐量:                    每秒生成的 Token 数 (Token/s)
  - 请求成功率:                      非 4xx/5xx 响应的占比
  - 速率限制命中率:                  429 响应占所有请求的比例
  - p50/p95/p99 延迟:               各百分位的端到端延迟
  - 工具调用轮次:                    单次对话中 tool_calls 的平均轮次
  - Token 利用率 (completion/prompt ratio): 输出 Token 数 / 输入 Token 数
```

**全链路追踪实践**：

```python
def generate_request_id() -> str:
    """生成唯一请求 ID，用于全链路追踪"""
    import uuid
    timestamp = int(time.time() * 1000)
    trace_id = uuid.uuid4().hex[:12]
    return f"req_{timestamp}_{trace_id}"


# 在请求头注入追踪 ID
headers = {
    "X-Request-Id": generate_request_id(),
    "X-Correlation-Id": session_correlation_id,
}

# 日志输出格式
logger.info(
    "LLM request | request_id=%s | model=%s | prompt_tokens=%d | status=%d | latency=%dms",
    request_id, model, prompt_tokens, status_code, latency_ms,
)
```

### 7.6 成本控制

| 策略 | 说明 | 实现方式 |
|------|------|----------|
| Token 预算 | 限制单次对话的最大 Token 消耗 | `max_tokens` 参数 + 计数检查 |
| 模型选择 | 简单任务用小模型，复杂任务用大模型 | 任务路由：`gpt-4o-mini` vs `gpt-4o` |
| 缓存复用 | 相同请求命中缓存，避免重复计费 | 请求级缓存（语义相似度匹配） |
| 上下文压缩 | 压缩或摘要历史对话，避免超出上下文窗口 | 滑动窗口 + 关键信息抽取 |
| 速率限制 | 控制请求频率，避免超额 | 客户端 Token Bucket 限流器 |
| Prompt 优化 | 减少不必要的提示词 Token | 精简 system prompt，去除冗余描述 |
| 监控告警 | 实时监控 Token 消耗趋势 | 上报 usage 字段到监控系统 |

---

## 8 主流厂商 API 差异对照

| 维度 | OpenAI | Anthropic | Azure OpenAI | Google Gemini |
|------|--------|-----------|-------------|---------------|
| API 端点 | `/v1/chat/completions` | `/v1/messages` | `/{deployment}/chat/completions` | `/v1/models/{model}:generateContent` |
| 认证头 | `Authorization: Bearer` | `x-api-key` | `api-key` 或 Entra ID | `x-goog-api-key` |
| 流式格式 | SSE `data:` | SSE `data:` | SSE `data:` | SSE `data:` |
| 角色名称 | system/user/assistant/tool | system/user/assistant/tool | system/user/assistant/tool | user/model/function |
| 版本指定 | `X-Api-Version` 头 | `anthropic-version` 头 | 端点中编码版本 | 不指定（参数可选） |
| 工具调用 | `tools` / `tool_choice` | `tools` / `tool_choice` | `tools` / `tool_choice` | `tools` / `tool_config` |
| 结构化输出 | `response_format.json_schema` | 不直接支持 | `response_format.json_schema` | `response_mime_type` / `response_schema` |
| 上下文缓存 | 不直接支持 | `anthropic-beta: prompt-caching` | 不直接支持 | `context_caching_config` |
| 多模态 | 图片 (base64/URL) | 图片 (base64/URL) | 图片 (base64/URL) | 图片/音频/视频 |
| 并行工具调用 | 原生支持 | 原生支持 | 原生支持 | SDK 支持 |
| 速率限流头部 | `X-RateLimit-*` | `anthropic-ratelimit-*` | `x-ratelimit-*` | 不公开 |

---

## 9 总结

Agent 客户端与大模型的交互建立在 **HTTP 协议**之上，通过精心设计的 **请求头/响应头**、**请求体/响应体** 结构，以及 **流式/非流式/工具调用** 等通信模式，实现高效、实时、可扩展的 AI 对话服务。理解并善用 HTTP 各字段的含义和作用，是构建健壮的 Agent 客户端的基础。

### 核心要点

1. **认证安全** —— `Authorization: Bearer` 和 HTTPS 是不可或缺的安全基石，API Key 绝不可硬编码或泄露
2. **流式优先** —— 优先使用 SSE 流式响应以降低首 Token 延迟，提升用户体验
3. **工具调用** —— Function Calling 是 Agent 获取外部能力的关键机制，需正确处理多轮内循环
4. **弹性容错** —— 正确解读状态码和限流头部，实施指数退避、优雅降级和熔断保护
5. **可观测性** —— 利用 `X-Request-Id` 和速率限制头部实现全链路追踪和性能监控
6. **成本合理** —— 根据任务复杂度选择模型、实现 Token 预算控制和缓存复用
7. **厂商适配** —— 不同厂商的 API 在认证方式、端点路径和功能支持上存在差异，客户端需做好适配层
8. **标准化演进** —— MCP（Model Context Protocol）作为新兴标准，正在统一 Agent 工具接入层的协议规范

### 关键设计决策速查表

```
使用 SSE 流式？           ✅ 是（默认），除外：Embedding/分类任务
开启 HTTP/2？             ✅ 是（降低连接延迟）
使用连接池？              ✅ 是（建议 10-50 连接）
实现本地限流器？          ✅ 是（配合服务端限流头）
启用熔断器？              ✅ 是（故障期间快速失败）
使用备用端点？            ✅ 是（Region/厂商级容灾）
记录 X-Request-Id？       ✅ 是（全链路追踪基础）
```

Agent 客户端与 LLM 的通信本质上是一套 **基于 HTTP 的消息传递系统**，其设计质量直接影响 AI 应用的可靠性、响应速度和用户体验。在构建 Agent 系统时，应当将通信层的健壮性置于与 AI 能力同等的优先级。
