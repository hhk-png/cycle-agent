# 第十一章 MCP 协议与工具集成

> **本章衔接：** 上一章学习了错误处理与安全防护。本章介绍 MCP（Model Context Protocol）协议的核心概念，以及如何在 TUI 中集成标准化的工具调用。

## 11.1 MCP 协议概述

**MCP**（Model Context Protocol，模型上下文协议）是 Anthropic 于 2024 年底发布的开源协议，它为 AI 模型与外部工具、数据源和资源之间提供了标准化的交互方式。MCP 可以被理解为"AI 应用的 USB-C 接口"——它提供了一种统一的方式让 LLM 与各种工具和服务连接。

### 11.1.1 MCP 的核心价值

```
传统工具调用:
  LLM → 客户端硬编码的工具函数 → 返回结果
  弊端：每种工具需要单独实现，不可复用，不可发现

MCP 工具调用:
  LLM → MCP 客户端 → MCP 服务器（工具） → 返回结果
  优势：标准化协议、工具发现、动态注册、跨模型兼容
```

| 特性 | 传统 Function Calling | MCP 协议 |
|------|---------------------|---------|
| **工具注册** | 代码中硬编码 | 动态发现（`tools/list`） |
| **工具执行** | 自定义封装 | 标准化（`tools/call`） |
| **资源访问** | 无标准 | 资源 URI 方案（`resources/read`） |
| **提示词模板** | 无标准 | 提示词管理（`prompts/get`） |
| **传输层** | HTTP 自定义 | JSON-RPC 2.0 over stdio/SSE |
| **跨模型** | 需适配 | 通用协议 |
| **流式支持** | 各 API 不同 | 统一事件流 |

### 11.1.2 MCP 在 TUI 中的架构

```
┌──────────────────────────────────────────────────┐
│              TUI 应用（MCP 客户端）                  │
│                                                    │
│  ┌─────────┐  ┌──────────┐  ┌─────────────────┐  │
│  │ UI 层    │  │ MCP 层   │  │ 工具卡片渲染     │  │
│  │ (blessed)│  │ (Client) │  │ 结果展示         │  │
│  └────┬─────┘  └────┬─────┘  └────────┬────────┘  │
│       │              │                 │           │
│       ▼              ▼                 ▼           │
│  ┌──────────────────────────────────────────────┐ │
│  │          事件总线 / 状态管理                    │ │
│  └──────────────────────────────────────────────┘ │
└──────────────────────┬───────────────────────────┘
                       │
          JSON-RPC 2.0 │ (stdio / SSE)
                       ▼
┌──────────────────────────────────────────────────┐
│              MCP 服务器层                           │
│                                                    │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  │
│  │ 工具服务器  │  │ 资源服务器  │  │ 提示词服务器 │  │
│  │ (天气/搜索) │  │ (文件/DB)  │  │ (模板)      │  │
│  └────────────┘  └────────────┘  └────────────┘  │
└──────────────────────────────────────────────────┘
```

## 11.2 JSON-RPC 2.0 基础

MCP 基于 JSON-RPC 2.0 协议。所有通信都是结构化的 JSON 消息：

```typescript
// JSON-RPC 2.0 请求
interface JSONRPCRequest {
  jsonrpc: '2.0';
  id: number | string;
  method: string;
  params?: Record<string, unknown>;
}

// JSON-RPC 2.0 响应
interface JSONRPCResponse {
  jsonrpc: '2.0';
  id: number | string;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

// JSON-RPC 2.0 通知（无需响应）
interface JSONRPCNotification {
  jsonrpc: '2.0';
  method: string;
  params?: Record<string, unknown>;
}
```

### 核心方法

**工具（Tools）：**

| 方法 | 方向 | 描述 |
|------|------|------|
| `tools/list` | 服务器 → 客户端 | 列出所有可用工具 |
| `tools/call` | 客户端 → 服务器 | 调用指定工具 |

**资源（Resources）：**

| 方法 | 方向 | 描述 |
|------|------|------|
| `resources/list` | 服务器 → 客户端 | 列出可用资源 |
| `resources/read` | 客户端 → 服务器 | 读取指定资源 |
| `resources/subscribe` | 客户端 → 服务器 | 订阅资源变更 |
| `resources/unsubscribe` | 客户端 → 服务器 | 取消订阅 |

**提示词（Prompts）：**

| 方法 | 方向 | 描述 |
|------|------|------|
| `prompts/list` | 服务器 → 客户端 | 列出可用提示词模板 |
| `prompts/get` | 客户端 → 服务器 | 获取指定提示词 |

## 11.3 工具定义格式

```typescript
/** MCP 工具定义 */
interface MCPTool {
  name: string;
  description?: string;
  inputSchema: {
    type: 'object';
    properties?: Record<string, {
      type: string;
      description?: string;
      enum?: string[];
    }>;
    required?: string[];
  };
}

/** 工具调用请求 */
interface MCPToolCallRequest {
  name: string;
  arguments?: Record<string, unknown>;
}

/** 工具调用响应 */
interface MCPToolCallResponse {
  content: Array<{
    type: 'text' | 'image' | 'resource';
    text?: string;
    mimeType?: string;
    data?: string;
    uri?: string;
  }>;
  isError?: boolean;
}
```

## 11.4 在 TUI 中实现 MCP 客户端

### 11.4.1 基础 MCP 客户端（stdio 传输）

```typescript
import { spawn, ChildProcess } from 'child_process';
import * as readline from 'readline';

/**
 * 简易 MCP 客户端 —— 通过 stdio 传输与 MCP 服务器通信
 */
class MCPClient {
  private process: ChildProcess | null = null;
  private rl: readline.Interface | null = null;
  private pendingRequests = new Map<string | number, {
    resolve: (value: any) => void;
    reject: (reason: any) => void;
  }>();
  private requestId = 0;
  private tools: MCPTool[] = [];
  private onNotification?: (notification: JSONRPCNotification) => void;

  /**
   * 连接到 MCP 服务器（通过 stdio）
   */
  async connect(serverCommand: string, args: string[] = []): Promise<void> {
    this.process = spawn(serverCommand, args, {
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    this.rl = readline.createInterface({
      input: this.process.stdout!,
      crlfDelay: Infinity,
    });

    // 处理服务器响应
    this.rl.on('line', (line: string) => {
      try {
        const msg = JSON.parse(line);
        this.handleMessage(msg);
      } catch (e) { /* 解析错误忽略 */ }
    });

    // 处理错误
    this.process.stderr?.on('data', (data: Buffer) => {
      console.error('MCP 服务器错误:', data.toString());
    });

    this.process.on('exit', (code) => {
      console.log(`MCP 服务器退出，代码: ${code}`);
      this.process = null;
    });

    // 初始化：获取工具列表
    await this.initialize();
  }

  private async initialize(): Promise<void> {
    const result = await this.request('initialize', {
      protocolVersion: '2024-11-05',
      capabilities: { tools: {}, resources: {} },
      clientInfo: { name: 'tui-chat', version: '1.0.0' },
    });
    console.log('MCP 初始化完成:', result?.serverInfo);
    this.notify('notifications/initialized', {});
    await this.listTools();
  }

  private request(method: string, params?: Record<string, unknown>): Promise<any> {
    return new Promise((resolve, reject) => {
      const id = ++this.requestId;
      const request: JSONRPCRequest = { jsonrpc: '2.0', id, method, params };
      this.pendingRequests.set(id, { resolve, reject });
      if (this.process?.stdin) {
        this.process.stdin.write(JSON.stringify(request) + '\n');
      } else {
        reject(new Error('MCP 未连接'));
      }
      setTimeout(() => {
        if (this.pendingRequests.has(id)) {
          this.pendingRequests.delete(id);
          reject(new Error(`MCP 请求超时: ${method}`));
        }
      }, 30000);
    });
  }

  private notify(method: string, params?: Record<string, unknown>): void {
    const notification: JSONRPCNotification = { jsonrpc: '2.0', method, params };
    if (this.process?.stdin) {
      this.process.stdin.write(JSON.stringify(notification) + '\n');
    }
  }

  private handleMessage(msg: any): void {
    if (msg.id !== undefined && msg.id !== null) {
      const pending = this.pendingRequests.get(msg.id);
      if (pending) {
        this.pendingRequests.delete(msg.id);
        if (msg.error) pending.reject(new Error(msg.error.message));
        else pending.resolve(msg.result);
      }
    } else if (msg.method) {
      this.onNotification?.(msg);
    }
  }

  async listTools(): Promise<MCPTool[]> {
    const result = await this.request('tools/list');
    this.tools = result?.tools || [];
    return this.tools;
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<MCPToolCallResponse> {
    const result = await this.request('tools/call', { name, arguments: args });
    return result as MCPToolCallResponse;
  }

  getTools(): MCPTool[] { return this.tools; }

  disconnect(): void {
    if (this.process) { this.process.kill(); this.process = null; }
    this.rl?.close();
    this.rl = null;
  }
}
```

### 11.4.2 带进度通知的客户端

```typescript
interface ProgressInfo { progress: number; total?: number; message: string; }

class MCPClientWithProgress extends MCPClient {
  private progressHandlers = new Map<string, (progress: ProgressInfo) => void>();

  constructor() {
    super();
    this.onNotification = (notification) => {
      if (notification.method === 'notifications/progress') {
        const params = notification.params as any;
        const handler = this.progressHandlers.get(params.progressToken);
        handler?.({
          progress: params.progress,
          total: params.total,
          message: params.message || '',
        });
      }
    };
  }

  async callToolWithProgress(
    name: string, args: Record<string, unknown>,
    onProgress: (progress: ProgressInfo) => void,
  ): Promise<MCPToolCallResponse> {
    const progressToken = `progress-${Date.now()}`;
    this.progressHandlers.set(progressToken, onProgress);
    try {
      const result = await this.request('tools/call', {
        name, arguments: args,
        _meta: { progressToken },
      });
      return result as MCPToolCallResponse;
    } finally {
      this.progressHandlers.delete(progressToken);
    }
  }
}
```

## 11.5 传输层详解

### 11.5.1 stdio 传输（推荐）

stdio 传输是最简单的 MCP 通信方式：MCP 服务器作为子进程运行，通过标准输入/输出进行 JSON-RPC 通信。

```
TUI 应用 ←── stdout (JSON-RPC 响应) ──→ MCP 服务器
         ←── stdin  (JSON-RPC 请求) ──→
```

**优点：** 无需网络配置、进程隔离安全性好、启动快速、适合本地工具。

### 11.5.2 SSE 传输（Server-Sent Events）

SSE 传输适用于远程 MCP 服务器，通过 HTTP 进行通信：

```typescript
class MCPClientSSE extends MCPClient {
  private baseUrl: string;
  private sessionId: string | null = null;

  constructor(baseUrl: string) { super(); this.baseUrl = baseUrl; }

  async connectSSE(): Promise<void> {
    const response = await fetch(`${this.baseUrl}/sse`, {
      headers: { 'Accept': 'text/event-stream', 'Cache-Control': 'no-cache' },
    });
    if (!response.ok) throw new Error(`SSE 连接失败: ${response.status}`);

    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop() || '';
      for (const event of events) {
        for (const line of event.split('\n')) {
          if (line.startsWith('data: ')) {
            this.handleMessage(JSON.parse(line.slice(6)));
          }
        }
      }
    }
  }

  protected async request(method: string, params?: Record<string, unknown>): Promise<any> {
    const id = ++this.requestId;
    const request: JSONRPCRequest = { jsonrpc: '2.0', id, method, params };
    const response = await fetch(
      `${this.baseUrl}/message?sessionId=${this.sessionId || ''}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      }
    );
    const result = await response.json();
    if (result.error) throw new Error(result.error.message);
    return result.result;
  }
}
```

### 11.5.3 传输层对比

| 特性 | stdio | SSE | WebSocket |
|------|-------|-----|-----------|
| **延迟** | 极低（进程内） | 中（HTTP 开销） | 低（全双工） |
| **安全性** | 高（进程隔离） | 中（需认证） | 中（需认证） |
| **远程支持** | ❌ | ✅ | ✅ |
| **启动速度** | 快 | 依赖网络 | 依赖网络 |
| **适用场景** | 本地工具 | 远程 API | 实时服务 |
| **实现复杂度** | 低 | 中 | 高 |

## 11.6 MCP 工具调用在 TUI 中的展示

### 11.6.1 工具调用流程

```
用户: "北京今天天气怎么样？"
  │
  ▼
┌──────────────────────────────┐
│ 1. LLM 返回工具调用           │
│    get_weather(city:"北京")   │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ 2. TUI 展示工具调用卡片       │
│    ┌─ 🔧 工具调用 ────────┐  │
│    │ get_weather          │  │
│    │ city: "北京"         │  │
│    │ ⏳ 执行中...         │  │
│    └──────────────────────┘  │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ 3. MCP 客户端调用工具         │
│    mcpClient.callTool(...)   │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ 4. TUI 展示工具结果           │
│    ┌─ ✅ 工具结果 ────────┐  │
│    │ 25°C, 晴            │  │
│    └──────────────────────┘  │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ 5. LLM 基于结果生成回答       │
│    "北京今天25°C，天气晴朗"   │
└──────────────────────────────┘
```

### 11.6.2 集成 MCP 到 ChatTUI

```typescript
class ChatTUI {
  private mcpClient!: MCPClient;
  private mcpTools: MCPTool[] = [];

  async initMCP(): Promise<void> {
    this.mcpClient = new MCPClient();
    try {
      await this.mcpClient.connect('npx', ['-y', '@modelcontextprotocol/server-weather']);
      this.mcpTools = this.mcpClient.getTools();
      this.setStatus(`● 就绪 | MCP: ${this.mcpTools.length} 工具`, '#22aa44');
    } catch (err) {
      this.showSystemMessage('⚠️ MCP 服务器连接失败，将使用内置工具');
      this.setStatus('● 就绪 (离线模式)', '#aa8800');
    }
  }

  private async handleToolCall(name: string, args: Record<string, unknown>): Promise<void> {
    this.showToolCall(name, args);
    try {
      const result = await this.mcpClient.callTool(name, args);
      const resultText = result.content
        .filter(c => c.type === 'text')
        .map(c => c.text)
        .join('\n');
      this.showToolResult(name, resultText);
    } catch (err) {
      this.showToolError(name, args, (err as Error).message);
    }
  }
}
```

### 11.6.3 工具调用结果卡片增强

MCP 工具可能返回多种类型的内容，需要增强工具结果卡片：

```typescript
private renderMCPResult(name: string, response: MCPToolCallResponse): void {
  if (response.isError) {
    this.showToolError(name, {}, response.content[0]?.text || '未知错误');
    return;
  }
  const parts = response.content.map(content => {
    switch (content.type) {
      case 'text': return this.escapeTags(content.text || '');
      case 'image': return `{yellow-fg}[图片] ${content.mimeType || '未知格式'}{/yellow-fg}`;
      case 'resource': return `{cyan-fg}[资源] ${content.uri || ''}{/cyan-fg}\n${this.escapeTags(content.text || '')}`;
      default: return '';
    }
  }).join('\n\n');

  const card = blessed.box({
    parent: this.chatBox, top: 0, left: 0,
    width: '100%-2', height: 'shrink',
    content: [
      `{cyan-fg}┌─ ✅ {bold}工具结果{/bold} ───────────────────────────┐{/cyan-fg}`,
      `{cyan-fg}│{/cyan-fg}  工具: {bold}${name}{/bold}`,
      `{cyan-fg}│{/cyan-fg}`,
      `{green-fg}${parts}{/green-fg}`,
      `{cyan-fg}│{/cyan-fg}`,
      `{cyan-fg}└────────────────────────────────────────────┘{/cyan-fg}`,
    ].join('\n'),
    style: { fg: 'white', bg: '#1a1a1a' }, tags: true, wrap: true, shrink: true,
  });
}
```

## 11.7 创建自定义 MCP 服务器

### 11.7.1 简易 MCP 服务器（Node.js）

```typescript
#!/usr/bin/env node
/**
 * 简易 MCP 服务器 —— 通过 stdio 通信
 * 提供计算器和时间查询工具
 */
import * as readline from 'readline';

const TOOLS = [
  {
    name: 'calculate',
    description: '执行数学计算',
    inputSchema: {
      type: 'object',
      properties: {
        expression: { type: 'string', description: '数学表达式，如 "2 + 2"' },
      },
      required: ['expression'],
    },
  },
  {
    name: 'get_time',
    description: '获取指定城市的当前时间',
    inputSchema: {
      type: 'object',
      properties: {
        timezone: { type: 'string', description: '时区，如 "Asia/Shanghai"' },
      },
      required: ['timezone'],
    },
  },
];

const TOOL_HANDLERS: Record<string, (args: any) => any> = {
  calculate: (args) => {
    try {
      const result = Function(`"use strict"; return (${args.expression})`)();
      return { content: [{ type: 'text', text: `${args.expression} = ${result}` }] };
    } catch (e) {
      return { content: [{ type: 'text', text: `计算错误: ${(e as Error).message}` }], isError: true };
    }
  },
  get_time: (args) => {
    const now = new Date();
    const timeStr = now.toLocaleString('zh-CN', { timeZone: args.timezone || 'Asia/Shanghai' });
    return { content: [{ type: 'text', text: `当前时间 (${args.timezone}): ${timeStr}` }] };
  },
};

// JSON-RPC 循环
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line: string) => {
  try {
    const request = JSON.parse(line);
    switch (request.method) {
      case 'initialize':
        respond(request.id, {
          protocolVersion: '2024-11-05',
          serverInfo: { name: 'custom-tools', version: '1.0.0' },
          capabilities: { tools: {} },
        });
        break;
      case 'tools/list':
        respond(request.id, { tools: TOOLS });
        break;
      case 'tools/call': {
        const handler = TOOL_HANDLERS[request.params.name];
        if (handler) respond(request.id, handler(request.params.arguments));
        else respondError(request.id, -32601, `未知工具: ${request.params.name}`);
        break;
      }
      case 'notifications/initialized': break;
      default: respondError(request.id, -32601, `未知方法: ${request.method}`);
    }
  } catch (e) { console.error('请求解析错误:', e); }
});

function respond(id: any, result: any): void {
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\n');
}
function respondError(id: any, code: number, message: string): void {
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, error: { code, message } }) + '\n');
}
```

### 11.7.2 在 TUI 中启动自定义服务器

```typescript
async function startWithCustomMCPServer(): Promise<void> {
  const client = new MCPClient();
  await client.connect('node', ['./custom-mcp-server.js']);
  const tools = client.getTools();
  console.log(`已加载 ${tools.length} 个自定义工具:`);
  tools.forEach(t => console.log(`  - ${t.name}: ${t.description}`));
}
```

## 11.8 工具结果流式处理

### 11.8.1 分页策略

```typescript
class PaginatedToolCaller {
  async callWithPagination(
    client: MCPClient, toolName: string, args: Record<string, unknown>,
    pageSize: number = 50, onPage: (page: any[], pageIndex: number) => void,
  ): Promise<{ allResults: any[]; totalPages: number }> {
    const allResults: any[] = [];
    let currentPage = 0;
    let hasMore = true;
    while (hasMore) {
      const pageArgs = { ...args, _page: currentPage, _pageSize: pageSize };
      const response = await client.callTool(toolName, pageArgs);
      const pageData = this.extractData(response);
      onPage(pageData, currentPage);
      allResults.push(...pageData);
      hasMore = pageData.length >= pageSize;
      currentPage++;
    }
    return { allResults, totalPages: currentPage };
  }

  private extractData(response: MCPToolCallResponse): any[] {
    const text = response.content.filter(c => c.type === 'text').map(c => c.text).join('');
    try {
      const parsed = JSON.parse(text);
      return Array.isArray(parsed) ? parsed : parsed.data || parsed.items || parsed.results || [];
    } catch { return text.split('\n').filter(Boolean); }
  }
}
```

### 11.8.2 TUI 渐进式结果展示

```typescript
class ProgressiveResultRenderer {
  private container: Widgets.BoxElement;
  private lines: string[] = [];
  private maxVisibleLines: number;
  private renderTimer: NodeJS.Timeout | null = null;

  constructor(parent: Widgets.Screen, options: { maxVisibleLines?: number } = {}) {
    this.maxVisibleLines = options.maxVisibleLines || 100;
    this.container = blessed.box({
      parent, top: 0, left: 0, width: '100%', height: 'shrink',
      content: '', tags: true, wrap: true, scrollable: true, alwaysScroll: true,
    });
  }

  addChunk(chunk: string | any): void {
    const text = typeof chunk === 'string' ? chunk : JSON.stringify(chunk, null, 2);
    for (const line of text.split('\n')) {
      this.lines.push(line);
      if (this.lines.length > this.maxVisibleLines * 2) {
        this.lines.splice(0, this.lines.length - this.maxVisibleLines);
      }
    }
    this.scheduleRender();
  }

  private scheduleRender(): void {
    if (this.renderTimer) return;
    this.renderTimer = setTimeout(() => {
      this.renderTimer = null;
      this.doRender();
    }, 50);
  }

  private doRender(): void {
    const visibleLines = this.lines.slice(-this.maxVisibleLines);
    this.container.setContent(
      `{cyan-fg}┌─ 📊 工具结果（共 ${this.lines.length} 行）{/cyan-fg}\n` +
      visibleLines.map(line => `│ ${line}`).join('\n') +
      `\n{cyan-fg}└── 滚动查看全部结果 ──{/cyan-fg}`
    );
    this.container.screen.render();
  }
}
```

## 11.9 离线模式与 Mock 回退

当 MCP 服务器不可用时，TUI 应优雅降级到内置 Mock：

```typescript
class ToolExecutionEngine {
  private mcpClient: MCPClient | null = null;
  private mockHandlers: Map<string, (args: any) => Promise<any>> = new Map();

  constructor() {
    this.registerMock('get_weather', async (args) => ({
      city: args.city, temperature: 25, condition: '晴', humidity: 45,
    }));
    this.registerMock('calculate', async (args) => {
      const result = Function(`"use strict"; return (${args.expression})`)();
      return { expression: args.expression, result };
    });
  }

  registerMock(name: string, handler: (args: any) => Promise<any>): void {
    this.mockHandlers.set(name, handler);
  }

  async connectMCP(serverCommand: string): Promise<boolean> {
    try {
      this.mcpClient = new MCPClient();
      await this.mcpClient.connect(
        serverCommand.split(' ')[0],
        serverCommand.split(' ').slice(1)
      );
      return true;
    } catch { this.mcpClient = null; return false; }
  }

  async executeTool(name: string, args: Record<string, unknown>): Promise<{ result: string; fromMock: boolean }> {
    if (this.mcpClient) {
      try {
        const response = await this.mcpClient.callTool(name, args);
        const text = response.content.filter(c => c.type === 'text').map(c => c.text).join('\n');
        return { result: text, fromMock: false };
      } catch (err) {
        console.warn(`MCP 工具 ${name} 失败，回退到 Mock:`, err);
      }
    }
    const handler = this.mockHandlers.get(name);
    if (handler) {
      const data = await handler(args);
      return { result: JSON.stringify(data, null, 2), fromMock: true };
    }
    throw new Error(`未知工具: ${name}`);
  }
}
```

## 11.10 MCP 与工具调用的未来演进

### 11.10.1 流式工具参数

部分 LLM API（如 Anthropic）支持流式工具参数（`input_json_delta`），可以在 UI 中实时展示参数构建过程：

```typescript
private handleToolInputDelta(delta: string): void {
  if (!this.partialJsonBuffer) this.partialJsonBuffer = '';
  this.partialJsonBuffer += delta;
  try {
    const partial = JSON.parse(this.partialJsonBuffer);
    if (this.currentToolCallEl) {
      const argsStr = Object.entries(partial)
        .map(([k, v]) => `  ${k}: {yellow-fg}${JSON.stringify(v)}{/yellow-fg}`)
        .join('\n');
      // 更新工具卡片上的参数显示
    }
  } catch { /* JSON 还不完整 */ }
}
```

### 11.10.2 并行工具调用

```typescript
async function parallelToolCalls(
  client: MCPClient,
  calls: Array<{ name: string; args: Record<string, unknown> }>,
  onEachComplete?: (name: string, result: MCPToolCallResponse) => void,
): Promise<Map<string, MCPToolCallResponse>> {
  const results = new Map<string, MCPToolCallResponse>();
  await Promise.all(calls.map(async ({ name, args }) => {
    const result = await client.callTool(name, args);
    results.set(name, result);
    onEachComplete?.(name, result);
  }));
  return results;
}
```

### 9.10.3 MCP + TUI 演进趋势

| 趋势 | 说明 | 影响 |
|------|------|------|
| **流式工具参数** | 参数实时构建，逐字段展示 | 增强用户对工具调用的信任感 |
| **并行工具调用** | 多个工具同时执行 | 减少总响应时间，提升效率 |
| **工具链编排** | 工具输出作为另一工具的输入 | 支持复杂任务分解与协作 |
| **MCP 生态爆发** | 数千个 MCP 服务器上线 | TUI 可连接的数据源和工具急剧增多 |
| **统一认证** | MCP 服务器认证标准化 | 简化跨工具的安全管理 |

---

**实践：** 尝试连接真实的 MCP 天气服务器：`npx -y @modelcontextprotocol/server-weather`

**上一章：** [第十章：错误处理与安全](10-error-handling.md)

**下一步：** [第十二章：高级 MCP 与生产实践](12-mcp-advanced.md)
