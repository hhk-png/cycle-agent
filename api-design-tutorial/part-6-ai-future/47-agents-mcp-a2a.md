# 第 47 章 Agent 协议：MCP、A2A 与工具契约

## 47.1 当 API 的消费者是 Agent

传统 API 的消费者是人写的代码。Agent 的消费者是大模型驱动的循环：

```text
观察 → 计划 → 选择工具 → 调用 → 观察结果 → 修正 → ...
```

这带来新的契约要求：

- 工具描述必须让模型理解“何时用、怎么用”；
- 参数 schema 必须严格；
- 错误必须可恢复；
- 操作必须可审计；
- 权限必须最小化；
- 高风险操作需要确认；
- 结果必须可验证；
- 上下文和 token 成本要可控。

Agent 不会读你的营销页，也不会猜测含糊的字段。它依赖机器可读的契约。

> **版本基线（2026 年 11 月）**：MCP 的最新稳定修订是 **2026-07-28**，它把核心从“有状态会话”改为“无状态请求上下文”；A2A 于 2026 年 3 月发布 **1.0**，并在 2026 年 8 月成为 Agentic AI Foundation（AAIF）托管项目，MCP 也在 AAIF 体系下治理。本章讨论设计问题，但会把这两个版本的关键变化写清楚，因为旧版 MCP 的 `initialize` 与 `Mcp-Session-Id` 仍是许多存量实现的现实。

### 人 vs Agent 的差异

| 维度 | 人类开发者 | Agent |
|---|---|---|
| 学习 | 读文档、问人 | 读工具描述 |
| 错误 | 查日志、调试 | 依赖错误信息 |
| 重试 | 判断后重试 | 可能自动重试 |
| 权限 | 账号权限 | 可能有过宽权限 |
| 上下文 | 可以记住 | 有限窗口 |
| 行为 | 可预测 | 概率性 |

## 47.2 MCP：模型上下文协议

MCP（Model Context Protocol）是一个开放协议，用于让 AI 应用连接外部工具、资源和提示。它基于 JSON-RPC 2.0，但 2026-07-28 修订对“连接”的假设做了根本调整：不再要求先建立会话，而是让每个请求自带协议版本、客户端能力与认证上下文。

### 架构

```text
Host（AI 应用）
├── MCP Client
│   └── 连接 → MCP Server（工具/资源/提示）
├── MCP Client
│   └── 连接 → MCP Server（数据库）
└── ...
```

- **Host**：运行模型的 AI 应用；
- **Client**：与单个 Server 保持连接；
- **Server**：暴露工具、资源、提示。

### 核心原语

| 原语 | 能力拥有方 | 请求发起方 | 用途 |
|---|---|---|---|
| Tools | Server | Client | 可执行动作 |
| Resources | Server | Client | 可读取的上下文数据 |
| Prompts | Server | Client | 预定义提示模板 |
| Sampling | Client（Host 的模型） | Server | 请求 Host 进行模型采样 |
| Roots | Client | Server | 文件系统/工作区边界 |
| Elicitation | Client（用户交互） | Server | 向用户请求补充信息 |

“能力拥有方”和“请求发起方”是两个不同的轴。Tools/Resources/Prompts 由服务端提供、客户端发起；Sampling/Roots/Elicitation 由客户端提供能力、服务端发起请求。只写“方向”会把这两种情况混在一起。

2026-07-28 之后，Sampling/Roots/Elicitation 这类反向交互通过 MRTR 表达：服务端返回 `input_required`，客户端带补充信息重试，而不是在长连接上随时反向调用。

### 传输

- **stdio**：本地进程；
- **Streamable HTTP**：远程，支持流式；2026-07-28 之后不再依赖 `Mcp-Session-Id`，请求可以落到任意实例；Streamable HTTP 的工具调用通常还需要 `Mcp-Method`、`Mcp-Name` 等头，便于网关路由与审计；
- 旧版 HTTP+SSE 传输已逐步被 Streamable HTTP 取代；
- 2026-07-28 移除了旧式长连接上的 `resources/subscribe` 与 GET SSE 流，改由 `subscriptions/listen` 提供单一长连接订阅；断线恢复不再依赖 `Last-Event-ID`，而是重新拉取快照后继续。

这一变化对部署影响很大：MCP Server 可以像普通无状态 API 一样水平扩展、跑在 Serverless/边缘，而不需要粘性会话或会话存储。代价是客户端必须在每个请求中携带足够的上下文，服务端也要重新设计“订阅”和“多轮请求”。

### 工具定义

```json
{
  "name": "get_order",
  "description": "根据订单 ID 查询订单。仅在用户提供订单 ID 时调用。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "orderId": {
        "type": "string",
        "description": "订单 ID，格式 ord_ 开头"
      }
    },
    "required": ["orderId"],
    "additionalProperties": false
  }
}
```

### 调用

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "tools/call",
  "params": {
    "name": "get_order",
    "arguments": { "orderId": "ord_123" }
  }
}
```

响应：

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "result": {
    "content": [
      { "type": "text", "text": "{\"status\":\"PAID\"}" }
    ],
    "isError": false
  }
}
```

### 资源

```json
{
  "uri": "file:///workspace/spec.md",
  "name": "API 规范",
  "mimeType": "text/markdown"
}
```

资源适合只读上下文；工具适合有副作用的操作。

### 授权

MCP 的远程连接通常使用 OAuth 2.1：

- 授权码 + PKCE；
- 资源指示器（RFC 8707）限制令牌用途；
- 短期令牌；
- 最小 scope；
- 用户同意；
- 令牌不能跨 Server 复用。

### MCP 的设计取舍

- 标准化接入；
- 工具发现；
- 传输灵活；
- 但工具质量参差；
- 安全边界复杂；
- 版本演进中。

### 工具注解与结构化结果

MCP 的工具定义不只包含 `name`、`description`、`inputSchema`。生产级实现通常还会使用：

```json
{
  "name": "refund_order",
  "title": "为订单退款",
  "description": "对已支付订单发起退款。调用前必须确认用户意图与退款金额。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "orderId": { "type": "string" },
      "amount": { "type": "integer", "minimum": 1 },
      "reason": { "type": "string", "enum": ["DAMAGED", "NOT_RECEIVED", "OTHER"] }
    },
    "required": ["orderId", "amount", "reason"],
    "additionalProperties": false
  },
  "outputSchema": {
    "type": "object",
    "properties": {
      "refundId": { "type": "string" },
      "status": { "type": "string" }
    },
    "required": ["refundId", "status"]
  },
  "annotations": {
    "title": "为订单退款",
    "readOnlyHint": false,
    "destructiveHint": true,
    "idempotentHint": true,
    "openWorldHint": false
  }
}
```

这些注解不是装饰：

- `readOnlyHint`：告诉 Host 这个工具不会改变状态，可用于自动批准；
- `destructiveHint`：提示可能造成不可逆影响，应要求确认；
- `idempotentHint`：允许在超时后安全重试；
- `openWorldHint`：表示会访问外部世界（网页、第三方 API），结果不可信、范围不可控。

工具结果也应尽量结构化，而不是只返回一段文本：

```json
{
  "content": [
    { "type": "text", "text": "退款已受理，预计 3 个工作日到账。" }
  ],
  "structuredContent": {
    "refundId": "rf_123",
    "status": "PENDING"
  },
  "isError": false
}
```

`content` 给模型看，`structuredContent` 给程序和后续工具看。不要只提供人类可读文本，也不要把关键状态只放在文本里。

### 版本协商与能力发现（2026-07-28）

2026-07-28 修订移除了 `initialize` / `initialized` 握手与 `Mcp-Session-Id`。新的模型是：

1. 客户端可以调用 `server/discover` 获取服务端支持的协议版本、能力、身份与扩展；
2. 每个请求在 `_meta` 中携带 `protocolVersion`、客户端能力与必要上下文；
3. 服务端根据请求自带的信息进行校验，不再依赖“这个连接之前协商过什么”；
4. 版本不兼容时返回明确的错误码，而不是降级猜测。

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "server/discover",
  "params": {}
}
```

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "serverInfo": { "name": "orders-mcp", "version": "2.3.0" },
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": {
      "tools": {},
      "resources": {},
      "subscriptions": {},
      "extensions": ["io.modelcontextprotocol/tasks"]
    }
  }
}
```

> 上面的响应是结构示意，具体字段名以 2026-07-28 官方规范为准；实现时不要依赖示例中的每一个键。

一次工具调用可能长这样（注意 `_meta` 中的版本与能力）：

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "tools/call",
  "params": {
    "name": "get_order",
    "arguments": { "orderId": "ord_123" },
    "_meta": {
      "protocolVersion": "2026-07-28",
      "clientCapabilities": { "elicitation": true }
    }
  }
}
```

工程要点：

- 客户端不应假设服务端支持某个可选能力，必须先看 `server/discover` 或响应中的能力；
- 协议版本不兼容时要明确失败，不要“试着调用”；
- 工具列表可能变化，支持变更通知的客户端要能刷新缓存；
- 远程服务端应提供稳定发现入口，并记录工具来源与版本；
- 工具名是契约的一部分，重命名等同于破坏性变更；
- 存量客户端可能仍在用 2025-06-18 / 2025-11-25 的 `initialize` 与 `Mcp-Session-Id`，服务端若要兼容，必须把旧路径当作**遗留适配层**，而不是新设计的基础。

### 多轮请求、任务与订阅

2026-07-28 把几类“服务端反向请求”改成了更适合无状态部署的模型：

| 旧模型（≤2025-11-25） | 2026-07-28 |
|---|---|
| `initialize` / `initialized` | `server/discover` + 每请求 `_meta` |
| `Mcp-Session-Id` | 无会话；请求自带上下文 |
| 服务端发起的 roots/sampling/elicitation 长请求 | 多轮请求（MRTR）：`InputRequiredResult` + `inputResponses` |
| GET SSE + `resources/subscribe` | `subscriptions/listen` 单一长连接 |
| `tasks/list`、阻塞式 `tasks/result` | 任务扩展：`tasks/get` + `tasks/update` |
| `ping`、`logging/setLevel` | 移除；日志级别按请求通过扩展传递 |

**MRTR（Multi Round-Trip Requests）** 是理解新版 MCP 的关键。服务端不再保持一个“可以随时反问客户端”的长连接，而是返回一个 `input_required` 结果；客户端带着 `inputResponses` 重试原请求。这让每一次交互都是可重放、可路由、可超时的普通请求。

**任务扩展**把长耗时操作从“阻塞调用”变成可查询的状态机：

```text
tools/call → 返回 taskId
tasks/get  → status: working | input_required | completed | failed
tasks/update → 补充输入或取消
```

**扩展机制**使用反向 DNS 标识（如 `io.modelcontextprotocol/tasks`），通过能力协商启用，独立于核心规范演进。设计工具契约时，应把扩展当作可选能力：不支持时降级，而不是崩溃。

### 迁移清单

如果你维护一个 MCP Server，从 2025 系列迁移到 2026-07-28，至少检查：

- [ ] 删除对 `initialize` 状态的依赖，改为解析每请求 `_meta`。
- [ ] 删除 `Mcp-Session-Id` 的生成、校验与粘性路由。
- [ ] 实现 `server/discover`，返回支持的版本、能力与扩展。
- [ ] 把采样/roots/elicitation 改成 MRTR，或明确声明不支持。
- [ ] 把资源订阅迁移到 `subscriptions/listen`，设计重连后的快照恢复。
- [ ] 把长任务迁移到 tasks 扩展，提供 `tasks/get` 与取消。
- [ ] 校验 OAuth `iss`（RFC 9207）与 `aud`，使用 OAuth 2.1 + PKCE。
- [ ] 对旧版客户端保留适配层，并设定退出日期。

### 安全最佳实践

MCP 的安全边界比普通 API 复杂，因为连接的一端可能是本地进程、远程服务，甚至第三方市场里的未知实现。

**令牌不能透传。** MCP Server 不应把客户端给的令牌原样转发给下游 API。正确做法是：

- MCP Server 作为 OAuth 资源服务器，校验令牌的 `aud` 是否为自己，并校验 `iss`（RFC 9207 授权服务器标识）；
- 需要访问下游时，用令牌交换（RFC 8693）换取下游 audience 的短期令牌；
- 每个工具声明所需 scope，Host 在调用前检查；
- 使用 OAuth 2.1 + PKCE；不要把 refresh token 或长期密钥放进工具参数。

**本地服务端也是攻击面。** stdio 启动的本地 Server 拥有当前用户权限，可能读取文件、执行命令。Host 应：

- 显示并审核将要启动的命令；
- 限制工作目录与环境变量；
- 不把整个主目录暴露为 root；
- 对高风险工具要求逐次确认；
- 记录工具调用与文件访问。

**远程服务端要防 SSRF 与混淆代理。** 服务端抓取用户提供的 URL、访问内网资源、代表用户调用第三方时，必须做：

- 出网 allowlist 与私网地址拦截；
- DNS 重绑定防护；
- 逐请求授权，而不是“连接时授权一次”；
- 用户、租户、工具、下游资源四者的绑定校验；
- 审计完整的委托链。

**工具描述本身不可信。** 第三方 Server 的 `description` 可能包含提示注入。Host 应把工具描述当作不可信输入展示给用户或做隔离，不要让描述直接覆盖系统指令。

## 47.3 A2A：Agent 与 Agent 协作

A2A（Agent2Agent）关注的是 Agent 之间的互操作：

- Agent 发现；
- 能力声明；
- 任务委派；
- 消息；
- 产物；
- 流式更新；
- 推送通知。

截至 2026 年 11 月，A2A 的稳定版本是 **1.0.x**（1.0 于 2026 年 3 月发布）。与早期草案相比，生产实现需要关注：

- **三种绑定**：JSON-RPC 2.0、gRPC、HTTP+JSON/REST；客户端与服务端可以在 Agent Card 中声明偏好；
- **版本协商**：通过 `A2A-Version` 头或等价机制协商，不能假设对方支持最新版；
- **签名 Agent Card**：卡片可用 JWS 签名，客户端验证后才能信任其中的端点、公钥与能力；
- **多租户**：同一端点可以服务多个租户，租户上下文必须进入认证与授权链；
- **治理**：A2A 已进入 Linux Foundation 体系，2026 年 8 月起成为 Agentic AI Foundation（AAIF）托管项目；MCP 也在 AAIF 体系下治理；
- **存量生态**：不少已部署实现仍使用 0.3 时代的线格式，SDK 与网关的支持程度不一，跨组织协作要留降级与适配层。

### Agent Card

Agent 通过卡片声明能力。下面的 JSON 是结构示意，字段名与必填性以 A2A 1.0 规范为准：

```json
{
  "protocolVersion": "1.0",
  "name": "invoice-agent",
  "description": "处理发票开具与查询",
  "url": "https://agent.example.com/a2a",
  "preferredTransport": "JSONRPC",
  "additionalInterfaces": [
    { "transport": "GRPC", "url": "https://agent.example.com/a2a/grpc" },
    { "transport": "HTTP+JSON", "url": "https://agent.example.com/a2a/rest" }
  ],
  "capabilities": {
    "streaming": true,
    "pushNotifications": true,
    "multiTenant": true
  },
  "skills": [
    {
      "id": "create-invoice",
      "name": "开具发票",
      "description": "根据订单创建发票",
      "inputModes": ["application/json"],
      "outputModes": ["application/json"]
    }
  ],
  "securitySchemes": {
    "oauth2": {
      "type": "oauth2",
      "flows": {
        "authorizationCode": {
          "authorizationUrl": "https://auth.example.com/authorize",
          "tokenUrl": "https://auth.example.com/token",
          "scopes": { "tasks:write": "创建任务" }
        }
      }
    }
  },
  "signatures": [
    { "protected": "...", "signature": "..." }
  ]
}
```

### 任务

```json
{
  "id": "task_123",
  "status": "working",
  "messages": [],
  "artifacts": []
}
```

A2A 的任务状态以本节末尾的任务生命周期图与状态表为准。这里先给出完整集合与终态标记：

| 状态 | 终态 | 含义 |
|---|---|---|
| `submitted` | 否 | 已提交，等待处理 |
| `working` | 否 | 处理中 |
| `input-required` | 否 | 需要调用方补充信息，可回到 `working` |
| `auth-required` | 否 | 需要额外授权，可回到 `working` |
| `completed` | 是 | 成功完成 |
| `failed` | 是 | 失败 |
| `canceled` | 是 | 已取消 |
| `rejected` | 是 | 被拒绝 |

`input-required` 与 `auth-required` 不是错误，也不是终态；调用方补充信息或授权后应继续同一任务。

### 与 MCP 的区别

- MCP：Agent 连接工具/资源，偏“能力接入”；
- A2A：Agent 与 Agent 协作，偏“任务委派”；
- 两者可以组合：Agent 通过 MCP 使用工具，通过 A2A 把任务交给另一个 Agent。

### Agent Card 的发现与签名

A2A 的 Agent Card 通常发布在：

```text
https://agent.example.com/.well-known/agent-card.json
```

它描述 Agent 的身份、端点、能力、技能、安全方案与输入输出模式。生产环境要把它当成“公开的契约”，而不是营销页：

- **签名**：卡片可以用 JWS 签名，客户端验证签名后才能信任其中的公钥与端点；
- **版本**：卡片带 `protocolVersion`，请求用 `A2A-Version` 或等价机制协商；技能与端点变化要可追踪；
- **最小披露**：公开卡片只暴露必要能力，内部技能通过认证后的扩展卡片提供；
- **安全方案**：明确 OAuth scope、API Key 位置、mTLS 要求，不要让客户端猜；
- **扩展**：用 `extensions` 声明非标准能力，未支持的客户端应忽略而不是失败；
- **缓存**：卡片可以缓存，但要有 TTL 与重新验证机制。

### 任务生命周期与推送

A2A 的任务不是一次 RPC，而是一个可长时间运行的状态机：

```text
submitted → working → completed
             ├→ input-required → working
             ├→ auth-required → working
             ├→ failed
             ├→ canceled
             └→ rejected
```

这张图与 47.3 开头的状态表是同一套状态；实现时以那张表的“终态/非终态”为准，不要在两处各维护一套互相冲突的枚举。

设计要点：

- 任务 ID 稳定，消息与产物分别有 ID；
- 流式更新用 SSE，事件要有顺序与终止标记；
- 推送通知是 Webhook 的反向调用：要签名、要重试、要防重放、要能退订；
- `input-required` 是正常状态，不是错误；调用方要能带着补充信息继续任务；
- 取消是协作式的：服务端可以拒绝立即取消，但必须给出当前状态；
- 任务产物要有 MIME 类型、大小、校验和与保留期。

### MCP、A2A 与 AG-UI 的边界

| 协议 | 解决的关系 | 典型问题 |
|---|---|---|
| MCP | Agent ↔ 工具/资源 | 如何发现并调用一个能力 |
| A2A | Agent ↔ Agent | 如何委派任务、交换产物 |
| AG-UI | Agent ↔ 前端 | 如何把流式过程呈现给人并接受干预 |

三者不是替代关系。一个企业助手可能通过 MCP 访问数据库，通过 A2A 把理赔审核交给专业 Agent，再通过 AG-UI 把进度、确认与人工接管呈现给客服。真正的设计难点不在协议选择，而在**身份、授权、审计与失败恢复如何跨这三条链路保持一致**。

### AG-UI

AG-UI 关注 Agent 与前端 UI 的交互协议，例如流式消息、状态更新、人工确认。它解决“Agent 如何把过程呈现给人”的问题。

## 47.4 工具契约设计

### 1. 名称

- 动词 + 名词：`get_order`、`create_refund`；
- 避免歧义；
- 避免一个工具多个用途；
- 避免相似名称；
- 使用业务词汇。

### 2. 描述

描述应回答：

- 这个工具做什么？
- 什么时候应该用？
- 什么时候不应该用？
- 参数含义；
- 返回值；
- 错误；
- 副作用；
- 权限要求；
- 是否幂等；
- 成本。

```text
创建退款。仅当订单已支付且用户明确要求退款时调用。
该操作会发起真实资金退款，不可撤销。
相同 idempotencyKey 重复调用不会重复退款。
```

### 3. Schema

- 严格类型；
- 枚举；
- 范围；
- 必填；
- 禁止额外字段；
- 示例；
- 默认值；
- 格式说明。

### 4. 错误

错误应让 Agent 恢复：

```json
{
  "isError": true,
  "content": [
    {
      "type": "text",
      "text": "ORDER_NOT_PAID: 订单 ord_123 当前状态为 PENDING_PAYMENT，只有 PAID 状态可以退款。请先确认支付状态。"
    }
  ]
}
```

避免：

```text
Error: 400
```

### 5. 幂等与确认

- 写工具支持幂等键；
- 高风险操作要求 `confirm: true` 或用户确认；
- 返回可验证的 ID；
- 提供查询工具。

### 6. 结果大小

- 限制返回大小；
- 分页；
- 摘要 + 引用；
- 避免把整个数据库塞进上下文。

### 工具设计检查清单

1. 名称是否清晰？
2. 描述是否说明何时用/不用？
3. schema 是否严格？
4. 错误是否可恢复？
5. 是否幂等？
6. 是否需要确认？
7. 权限是否最小？
8. 结果是否可控？
9. 是否可审计？
10. 是否有查询/验证工具？

### 工具契约的版本化

工具 schema 的演进规则与普通 API 类似，但更敏感，因为模型会在运行时“即兴”构造参数：

- 新增**可选**参数通常兼容；
- 新增必填参数是破坏性变更，必须给默认值或新工具名；
- 修改枚举、收紧范围、改字段含义都是破坏性变更；
- 删除或重命名工具，等同于删除端点；
- 工具描述的变化也会改变模型行为，应进入变更日志；
- 参数错误信息要指出“哪个字段、期望什么、实际是什么”，否则模型很难自愈。

版本化策略：

```text
get_order            # 稳定别名，指向当前推荐版本
get_order@2026-06    # 固定行为，供关键工作流锁定
```

对 Agent 而言，工具名的稳定性比版本号更重要。不要让同一个工作流在模型升级后突然找不到工具。

### 工具结果的可信度

Agent 会把工具结果当作事实继续推理。因此结果要能区分：

- **权威数据**：来自你自己的数据库，有稳定 ID 与版本；
- **外部数据**：来自第三方 API，可能过期、可能被污染；
- **用户内容**：来自邮件、文档、网页，必须当作不可信输入；
- **模型生成**：来自上一次采样，不是事实。

推荐在结果中显式标注来源与信任等级：

```json
{
  "structuredContent": { "status": "PAID" },
  "_meta": {
    "source": "orders-db",
    "trust": "authoritative",
    "retrievedAt": "2026-11-26T10:00:00Z",
    "schemaVersion": "1.4"
  }
}
```

如果结果来自外部网页，应该用 `trust: "untrusted"` 并在 Host 侧做内容隔离；不要让模型把网页中的“请调用 delete_all”当成工具指令。

## 47.5 安全

Agent 工具带来新的攻击面：

### 提示注入

恶意文档/网页可能指示 Agent 调用危险工具。防护：

- 工具权限最小化；
- 不可信内容与指令分离；
- 高风险操作确认；
- 输出过滤；
- 沙箱；
- 审计。

### 混淆代理（Confused Deputy）

Agent 拥有高权限，攻击者诱导它代表自己操作。防护：

- 令牌绑定用户与租户；
- 每次调用检查授权；
- 不把用户输入当授权；
- 委托链可追踪；
- 最小 scope。

### 工具投毒

恶意 MCP Server 提供看似正常但会泄露数据的工具。防护：

- 信任来源；
- 签名/审核；
- 权限隔离；
- 网络限制；
- 审计。

### 数据外泄

Agent 可能把敏感数据发送到外部工具。防护：

- 数据分类；
- 出网控制；
- 脱敏；
- 用户确认；
- 审计。

### 过度代理

Agent 可能：

- 调用过多工具；
- 重复调用；
- 误用高权限；
- 递归调用。

防护：

- 调用次数限制；
- 工具白名单；
- 预算；
- 人工确认；
- 循环检测。

### MCP 特有的攻击面

| 攻击 | 场景 | 防护 |
|---|---|---|
| 令牌透传 | Server 把客户端令牌转发给下游 | 校验 `aud` 与 `iss`，令牌交换，逐服务授权 |
| 版本降级 | 攻击者诱导客户端使用弱版本/旧能力 | 版本白名单、`server/discover` 校验、拒绝未知版本 |
| 发现污染 | 伪造 `server/discover` 结果或工具清单 | TLS、签名/来源审核、固定发现入口、缓存 TTL |
| 本地服务端提权 | stdio Server 读取敏感文件 | 沙箱、最小权限、启动确认 |
| 工具投毒 | 描述中藏提示注入 | 来源审核、签名、隔离描述 |
| 恶意资源 | `resource_link` 指向内网 | URL allowlist、私网拦截、内容扫描 |
| 过度采样 | Server 反复请求 Host 采样 | 采样配额、用户可见、成本上限 |
| 供应链 | 第三方 Server 包被篡改 | 锁定版本、校验和、签名、审计 |
| 遗留会话 | 旧版 `Mcp-Session-Id` 被劫持 | 仅作兼容层、短 TTL、绑定主体、尽快迁移 |

一个容易被忽略的点：**MCP Server 的“工具列表”本身就是动态输入**。如果 Server 可以在运行中新增或修改工具，Host 必须重新做信任判断，而不是沿用连接建立时的授权。工具清单变化应触发通知、重新审核，必要时断开连接。

## 47.6 可观测性

Agent 调用链需要：

- 会话 ID；
- 任务 ID；
- 工具调用 ID；
- 模型版本；
- 输入/输出 token；
- 延迟；
- 错误；
- 人工确认；
- 成本；
- 完整轨迹（注意隐私）。

### 轨迹

```json
{
  "sessionId": "sess_123",
  "taskId": "task_456",
  "steps": [
    {
      "type": "tool_call",
      "tool": "get_order",
      "arguments": { "orderId": "ord_123" },
      "result": { "status": "PAID" },
      "tokens": { "input": 120, "output": 45 },
      "latencyMs": 320
    }
  ]
}
```

轨迹用于：

- 调试；
- 评估；
- 审计；
- 复现；
- 成本分析。

### 用 OpenTelemetry 表达 Agent 轨迹

OpenTelemetry 的 GenAI 语义约定把模型调用、token 用量、工具调用纳入统一的 span 模型。落地时建议：

- 一次用户任务是一个 trace，每次模型调用与工具调用是子 span；
- span 属性记录 `gen_ai.provider.name`（旧版语义约定中的 `gen_ai.system` 已调整/弃用，落地前核对目标 semconv 版本）、`gen_ai.request.model`、`gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens`；
- 工具调用记录工具名、参数摘要（脱敏）、结果状态、延迟；
- 不在 span 中记录完整 prompt 与用户数据，默认只记录哈希、长度与采样后的片段；
- 用自定义属性（如 `session.id`、`task.id`、`agent.id`）关联跨服务轨迹；
- 对高风险操作记录人工确认事件，作为独立 span event。

没有统一语义时，每个团队会发明自己的日志字段，最后既无法比较模型成本，也无法定位“为什么这个 Agent 多调了三次工具”。

## 47.7 评估

Agent 的评估比单次 API 更难：

- 任务成功率；
- 工具选择正确率；
- 参数正确率；
- 错误恢复率；
- 人工介入率；
- 成本；
- 延迟；
- 安全性；
- 轨迹质量。

需要可重复的评估集和沙箱。

### 评估集

- 真实任务；
- 边界任务；
- 恶意输入；
- 多轮对话；
- 工具失败；
- 权限不足。

## 47.8 本章小结

- Agent 消费者要求机器可读、严格、可恢复的工具契约。
- MCP 标准化工具/资源/提示接入，基于 JSON-RPC，支持 stdio 与 Streamable HTTP。
- MCP 2026-07-28 从有状态会话转向无状态请求上下文：`server/discover`、每请求 `_meta`、MRTR、`subscriptions/listen` 与 tasks 扩展是新的设计基础。
- A2A 1.0 关注 Agent 间任务协作与能力发现，支持 JSON-RPC、gRPC、HTTP+JSON 三种绑定、版本协商与签名 Agent Card。
- 工具设计要明确何时用、参数、错误、副作用、幂等和确认。
- 安全重点是提示注入、混淆代理、工具投毒、数据外泄与令牌透传。
- 工具注解、结构化结果、版本协商与来源标记是生产级工具契约的一部分。
- Agent 需要完整的可观测性与评估，OpenTelemetry GenAI 语义约定可作为共同语言。
- 过度代理、工具数量、版本降级、发现污染与遗留会话需要治理。
- Agent 的身份、委托链与审计要落进统一的安全模型；AI 系统的透明度、训练数据与事件报告见[附录 T](../appendices/t-compliance-engineering.md)。

## 设计练习

为一个“企业差旅 Agent”设计工具集：

- 查询航班；
- 预订机票；
- 取消预订；
- 报销；
- 审批；
- 查询政策。

为每个工具写描述、schema、错误、幂等与确认策略，并说明如何防止越权。然后回答：

1. 哪些工具需要用户确认？
2. 如何防止 Agent 用错工具？
3. 如何限制工具调用次数？
4. 如何审计一次差旅预订？

---

上一章：[RAG、向量检索与知识 API](46-rag-vector.md)  
下一章：[AI 原生 API：当消费者不是人](48-ai-native-apis.md)
