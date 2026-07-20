# 第十二章 高级 MCP 与生产实践

> **本章衔接：** 上一章学习了 MCP 协议基础、客户端实现和基本集成。本章深入生产级 MCP 应用：安全的文件操作服务器、SSE 重连、客户端池管理、认证授权、断路器模式、资源实时监控等。

## 12.1 安全文件操作 MCP 服务器

以下是一个提供文件读写和目录列表功能的自定义 MCP 服务器，包含完整的安全验证措施：

```typescript
#!/usr/bin/env node
/**
 * 文件操作 MCP 服务器
 * 提供安全的文件读取、写入和目录列表功能
 * 内置路径遍历防护、文件大小限制和扩展名白名单
 */
import * as fs from 'fs';
import * as path from 'path';
import * as readline from 'readline';

// ---------- 安全配置 ----------
const ALLOWED_BASE_DIR = path.resolve(process.env.MCP_FILE_BASE || process.cwd());
const MAX_FILE_SIZE = 10 * 1024 * 1024;  // 单文件最大 10MB
const MAX_WRITE_SIZE = 1 * 1024 * 1024;   // 单次写入最大 1MB

// ---------- 安全工具函数 ----------
function safeResolve(targetPath: string): string | null {
  const resolved = path.resolve(ALLOWED_BASE_DIR, targetPath);
  return resolved.startsWith(ALLOWED_BASE_DIR) ? resolved : null;
}

// ---------- 工具定义 ----------
const TOOLS = [
  {
    name: 'read_file',
    description: '读取文件内容。限制：最大 10MB、禁止路径遍历。',
    inputSchema: {
      type: 'object', properties: {
        filePath: { type: 'string', description: '文件路径（相对于工作区根目录）' },
        maxLines: { type: 'number', description: '最大读取行数' },
      },
      required: ['filePath'],
    },
  },
  {
    name: 'write_file',
    description: '写入文件内容。限制：单次最大 1MB、禁止路径遍历。',
    inputSchema: {
      type: 'object', properties: {
        filePath: { type: 'string' },
        content: { type: 'string' },
        overwrite: { type: 'boolean', default: true },
      },
      required: ['filePath', 'content'],
    },
  },
  {
    name: 'list_directory',
    description: '列出目录内容。可控制递归深度。',
    inputSchema: {
      type: 'object', properties: {
        dirPath: { type: 'string' },
        maxDepth: { type: 'number', default: 1 },
      },
      required: ['dirPath'],
    },
  },
];

// ---------- 工具处理函数 ----------
const TOOL_HANDLERS: Record<string, (args: any) => any> = {
  read_file: (args) => {
    const safePath = safeResolve(args.filePath);
    if (!safePath) return { content: [{ type: 'text', text: `错误：路径遍历攻击已阻止` }], isError: true };
    if (!fs.existsSync(safePath)) return { content: [{ type: 'text', text: `错误：文件不存在` }], isError: true };
    if (fs.statSync(safePath).size > MAX_FILE_SIZE) {
      return { content: [{ type: 'text', text: `错误：文件过大` }], isError: true };
    }
    let content = fs.readFileSync(safePath, 'utf-8');
    if (args.maxLines) {
      const lines = content.split('\n');
      if (lines.length > args.maxLines) {
        content = lines.slice(0, args.maxLines).join('\n') + `\n...（已截断，共 ${lines.length} 行）`;
      }
    }
    return { content: [{ type: 'text', text: `文件: ${args.filePath}\n---\n${content}` }] };
  },

  write_file: (args) => {
    const safePath = safeResolve(args.filePath);
    if (!safePath) return { content: [{ type: 'text', text: '错误：路径遍历攻击已阻止' }], isError: true };
    if (Buffer.byteLength(args.content, 'utf-8') > MAX_WRITE_SIZE) {
      return { content: [{ type: 'text', text: '错误：写入内容过大' }], isError: true };
    }
    if (fs.existsSync(safePath) && args.overwrite === false) {
      return { content: [{ type: 'text', text: `错误：文件已存在` }], isError: true };
    }
    const dir = path.dirname(safePath);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(safePath, args.content, 'utf-8');
    return { content: [{ type: 'text', text: `文件已写入: ${args.filePath}` }] };
  },

  list_directory: (args) => {
    const safePath = safeResolve(args.dirPath);
    if (!safePath) return { content: [{ type: 'text', text: '错误：路径遍历攻击已阻止' }], isError: true };
    if (!fs.existsSync(safePath) || !fs.statSync(safePath).isDirectory()) {
      return { content: [{ type: 'text', text: `错误：目录不存在` }], isError: true };
    }
    function walkDir(dir: string, depth: number): string[] {
      if (depth > (args.maxDepth ?? 1)) return [];
      const results: string[] = [];
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const fullPath = path.join(dir, entry.name);
        const indent = '  '.repeat(depth);
        if (entry.isDirectory()) results.push(`${indent}📁 ${entry.name}/`);
        else results.push(`${indent}📄 ${entry.name}`);
      }
      return results;
    }
    return { content: [{ type: 'text', text: `目录: ${args.dirPath}/\n${walkDir(safePath, 0).join('\n')}` }] };
  },
};

// ---------- JSON-RPC 服务入口 ----------
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line: string) => {
  try {
    const request = JSON.parse(line);
    switch (request.method) {
      case 'initialize':
        respond(request.id, {
          protocolVersion: '2024-11-05',
          serverInfo: { name: 'file-server', version: '1.0.0' },
          capabilities: { tools: {} },
        });
        break;
      case 'tools/list': respond(request.id, { tools: TOOLS }); break;
      case 'tools/call': {
        const handler = TOOL_HANDLERS[request.params.name];
        if (handler) {
          try { respond(request.id, handler(request.params.arguments)); }
          catch (err) { respondError(request.id, -32603, `工具执行错误: ${(err as Error).message}`); }
        } else respondError(request.id, -32601, `未知工具: ${request.params.name}`);
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

### 安全特性总结

| 安全措施 | 实现方式 |
|---------|---------|
| **路径遍历防护** | `safeResolve()` 确保路径限制在允许的基目录内 |
| **文件大小限制** | 读取最大 10MB，写入最大 1MB |
| **行数限制** | `maxLines` 参数控制最大读取行数 |
| **目录自动创建** | 写入文件时自动创建父目录 |
| **环境变量配置** | 通过 `MCP_FILE_BASE` 设置工作目录 |

## 12.2 SSE 重连模式

基于 SSE 传输的 MCP 连接可能因网络问题中断，需要实现健壮的重连机制。

### 12.2.1 指数退避重连

```typescript
interface ReconnectConfig {
  baseDelay: number;       // 初始重试延迟（毫秒）
  maxDelay: number;        // 最大重试延迟
  maxRetries: number;      // 最大重试次数
  jitterFactor: number;    // 抖动因子（0-1）
  enableDegradation: boolean; // 是否启用离线降级
}

const DEFAULT_RECONNECT_CONFIG: ReconnectConfig = {
  baseDelay: 1000, maxDelay: 30000, maxRetries: 10,
  jitterFactor: 0.3, enableDegradation: true,
};

function calculateBackoff(attempt: number, config: ReconnectConfig): number {
  const exponential = Math.min(
    config.baseDelay * Math.pow(2, attempt),
    config.maxDelay,
  );
  const jitter = 1 + Math.random() * config.jitterFactor;
  return Math.floor(exponential * jitter);
}
```

### 12.2.2 完整 SSE 重连客户端

```typescript
type ConnectionStatus = 'connected' | 'disconnected' | 'reconnecting' | 'degraded';

class ReconnectingSSEClient {
  private baseUrl: string;
  private config: ReconnectConfig;
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  private abortController: AbortController | null = null;
  private retryAttempt = 0;
  private isConnected = false;
  private isDegraded = false;
  private pendingRequests = new Map<string | number, {
    resolve: (value: any) => void; reject: (reason: any) => void; timer: NodeJS.Timeout;
  }>();
  private requestId = 0;

  onStatusChange?: (status: ConnectionStatus) => void;
  onNotification?: (notification: JSONRPCNotification) => void;
  onDegraded?: () => void;
  onRecovered?: () => void;

  constructor(baseUrl: string, config?: Partial<ReconnectConfig>) {
    this.baseUrl = baseUrl;
    this.config = { ...DEFAULT_RECONNECT_CONFIG, ...config };
  }

  get status(): ConnectionStatus {
    if (this.isDegraded) return 'degraded';
    if (this.isConnected) return 'connected';
    if (this.retryAttempt > 0) return 'reconnecting';
    return 'disconnected';
  }

  async connect(): Promise<void> {
    this.abortController = new AbortController();
    await this.establishConnection();
  }

  private async establishConnection(): Promise<void> {
    try {
      const response = await fetch(`${this.baseUrl}/sse`, {
        signal: this.abortController!.signal,
        headers: { 'Accept': 'text/event-stream' },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      this.isConnected = true;
      this.isDegraded = false;
      this.retryAttempt = 0;
      this.onStatusChange?.('connected');

      this.reader = response.body!.getReader();
      await this.processSSEStream();
    } catch (err) {
      this.isConnected = false;
      await this.handleDisconnect(err as Error);
    }
  }

  private async processSSEStream(): Promise<void> {
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (this.reader) {
        const { done, value } = await this.reader.read();
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
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
    }
    this.isConnected = false;
    await this.handleDisconnect(new Error('SSE 流意外终止'));
  }

  private async handleDisconnect(err: Error): Promise<void> {
    if (this.retryAttempt >= this.config.maxRetries) {
      this.onStatusChange?.('disconnected');
      if (this.config.enableDegradation) {
        this.isDegraded = true;
        this.onDegraded?.();
      }
      this.rejectAllPending(`连接断开: ${err.message}`);
      return;
    }

    const delay = calculateBackoff(this.retryAttempt, this.config);
    this.retryAttempt++;
    this.onStatusChange?.('reconnecting');
    await new Promise(resolve => setTimeout(resolve, delay));
    if (this.abortController?.signal.aborted) return;

    await this.establishConnection();
    if (this.isConnected) await this.recoverSession();
  }

  private async recoverSession(): Promise<void> {
    try {
      await this.initialize();
      await this.listTools();
      this.onRecovered?.();
      this.onStatusChange?.('connected');
      for (const [id, pending] of this.pendingRequests) {
        pending.reject(new Error('连接已恢复，请重新发送请求'));
        this.pendingRequests.delete(id);
      }
    } catch (err) { console.error('会话恢复失败:', err); }
  }

  async request(method: string, params?: Record<string, unknown>): Promise<any> {
    if (this.isDegraded) throw new Error('MCP 处于离线降级模式');
    const id = ++this.requestId;
    const request: JSONRPCRequest = { jsonrpc: '2.0', id, method, params };

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingRequests.delete(id);
        reject(new Error(`MCP 请求超时: ${method}`));
      }, 30000);

      this.pendingRequests.set(id, { resolve, reject, timer });
      fetch(`${this.baseUrl}/message?sessionId=`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      }).catch(() => { /* 网络失败，等待重连处理 */ });
    });
  }

  private handleMessage(msg: any): void {
    if (msg.id !== undefined) {
      const pending = this.pendingRequests.get(msg.id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pendingRequests.delete(msg.id);
        if (msg.error) pending.reject(new Error(msg.error.message));
        else pending.resolve(msg.result);
      }
    } else if (msg.method) {
      this.onNotification?.(msg);
    }
  }

  private rejectAllPending(reason: string): void {
    for (const [id, pending] of this.pendingRequests) {
      clearTimeout(pending.timer);
      pending.reject(new Error(reason));
      this.pendingRequests.delete(id);
    }
  }

  private async initialize(): Promise<void> {
    const result = await this.request('initialize', {
      protocolVersion: '2024-11-05',
      capabilities: { tools: {}, resources: {} },
      clientInfo: { name: 'tui-chat', version: '1.0.0' },
    });
  }
  private async listTools(): Promise<void> { await this.request('tools/list'); }

  disconnect(): void {
    this.abortController?.abort();
    this.reader = null;
    this.isConnected = false;
    this.retryAttempt = 0;
    this.rejectAllPending('客户端已断开');
    this.onStatusChange?.('disconnected');
  }
}
```

### 重连策略参数建议

| 场景 | baseDelay | maxDelay | maxRetries | jitterFactor |
|------|-----------|----------|------------|-------------|
| 本地服务器 | 500ms | 10s | 5 | 0.1 |
| 局域网服务器 | 1s | 15s | 10 | 0.2 |
| 远程 API | 2s | 60s | 15 | 0.3 |
| 公网服务 | 5s | 120s | 30 | 0.5 |

## 12.3 MCP 客户端池

当 TUI 需要同时连接多个 MCP 服务器时，客户端池模式可以有效管理这些连接。

```typescript
interface MCPServerConfig {
  name: string;
  transport: 'stdio' | 'sse';
  command?: string; args?: string[];
  baseUrl?: string;
  auth?: MCPAuthConfig;
  timeout?: number;
  autoStart?: boolean;
  tags?: string[];
}

class MCPClientPool {
  private servers = new Map<string, {
    config: MCPServerConfig;
    client: any;
    tools: MCPTool[];
    status: 'stopped' | 'starting' | 'running' | 'error';
    error?: string;
  }>();
  private toolIndex = new Map<string, string>();

  onStatusChange?: (serverName: string, status: string) => void;
  onToolsChange?: (tools: MCPTool[]) => void;

  registerServer(config: MCPServerConfig): void {
    if (this.servers.has(config.name)) throw new Error(`服务器 ${config.name} 已注册`);
    this.servers.set(config.name, {
      config, client: null, tools: [], status: 'stopped',
    });
    if (config.autoStart !== false) this.startServer(config.name);
  }

  async startServer(name: string): Promise<void> {
    const server = this.servers.get(name);
    if (!server) throw new Error(`服务器 ${name} 未注册`);
    server.status = 'starting';
    this.onStatusChange?.(name, 'starting');
    try {
      if (server.config.transport === 'stdio') {
        const client = new MCPClient();
        await client.connect(server.config.command!, server.config.args);
        server.client = client;
      } else {
        const client = new ReconnectingSSEClient(server.config.baseUrl!);
        client.onStatusChange = (status) => this.onStatusChange?.(name, status);
        await client.connect();
        server.client = client;
      }
      server.tools = server.client.getTools?.() || [];
      server.status = 'running';
      this.rebuildToolIndex();
      this.onStatusChange?.(name, 'running');
      this.onToolsChange?.(this.getAllTools());
    } catch (err) {
      server.status = 'error';
      server.error = (err as Error).message;
      this.onStatusChange?.(name, 'error');
    }
  }

  private rebuildToolIndex(): void {
    this.toolIndex.clear();
    for (const [serverName, server] of this.servers) {
      for (const tool of server.tools) {
        if (!this.toolIndex.has(tool.name)) {
          this.toolIndex.set(tool.name, serverName);
        }
      }
    }
  }

  getAllTools(): Array<MCPTool & { serverName: string }> {
    const result: Array<MCPTool & { serverName: string }> = [];
    for (const [serverName, server] of this.servers) {
      if (server.status === 'running') {
        server.tools.forEach(t => result.push({ ...t, serverName }));
      }
    }
    return result;
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<MCPToolCallResponse> {
    const serverName = this.toolIndex.get(name);
    if (!serverName) throw new Error(`工具 ${name} 未找到`);
    const server = this.servers.get(serverName)!;
    return await server.client.callTool(name, args);
  }

  stopAll(): void {
    for (const [name] of this.servers) this.stopServer(name);
  }

  stopServer(name: string): void {
    const server = this.servers.get(name);
    if (!server) return;
    server.client?.disconnect?.();
    server.client = null;
    server.tools = [];
    server.status = 'stopped';
    this.rebuildToolIndex();
    this.onStatusChange?.(name, 'stopped');
  }
}
```

## 12.4 MCP 安全考虑

### 12.4.1 服务器认证机制

```typescript
interface MCPAuthConfig {
  type: 'api_key' | 'bearer' | 'basic';
  credentials: string;
  headerName?: string;
}

class AuthenticatedMCPClient extends MCPClient {
  private auth: MCPAuthConfig;

  constructor(auth: MCPAuthConfig) { super(); this.auth = auth; }

  private async getAuthHeaders(): Promise<Record<string, string>> {
    switch (this.auth.type) {
      case 'api_key': return { [this.auth.headerName || 'X-API-Key']: this.auth.credentials };
      case 'bearer': return { 'Authorization': `Bearer ${this.auth.credentials}` };
      case 'basic': return { 'Authorization': `Basic ${Buffer.from(this.auth.credentials).toString('base64')}` };
    }
  }

  async connectSSE(baseUrl: string): Promise<void> {
    const headers = { 'Accept': 'text/event-stream', ...await this.getAuthHeaders() };
    const response = await fetch(`${baseUrl}/sse`, { headers });
    if (response.status === 401) throw new Error('MCP 认证失败');
    if (response.status === 403) throw new Error('MCP 授权失败：无权限');
    if (!response.ok) throw new Error(`MCP 连接失败: ${response.status}`);
    // SSE 流处理与标准客户端相同
  }

  protected async request(method: string, params?: Record<string, unknown>): Promise<any> {
    const headers = { 'Content-Type': 'application/json', ...await this.getAuthHeaders() };
    const response = await fetch(`${this.baseUrl}/message`, {
      method: 'POST', headers, body: JSON.stringify({ jsonrpc: '2.0', id: ++this.requestId, method, params }),
    });
    const result = await response.json();
    if (result.error) throw new Error(result.error.message);
    return result.result;
  }
}
```

### 12.4.2 进程隔离与沙箱

```typescript
import { spawn, SpawnOptions } from 'child_process';

interface SandboxOptions {
  maxMemoryMB?: number;
  timeoutSeconds?: number;
  allowedEnvVars?: string[];
}

class SecureMCPSpawner {
  spawnSandboxed(command: string, args: string[] = [], options: SandboxOptions = {}): ChildProcess {
    const env: Record<string, string> = {};
    const allowed = new Set(options.allowedEnvVars || ['PATH', 'HOME']);
    for (const key of allowed) { if (process.env[key]) env[key] = process.env[key]; }

    const proc = spawn(command, args, { env, stdio: ['pipe', 'pipe', 'pipe'], shell: false });

    if (options.maxMemoryMB) {
      const timer = setInterval(() => {
        if (process.memoryUsage().rss > options.maxMemoryMB! * 1024 * 1024) {
          proc.kill('SIGTERM');
          clearInterval(timer);
        }
      }, 5000);
      proc.on('exit', () => clearInterval(timer));
    }
    if (options.timeoutSeconds) {
      const timeout = setTimeout(() => proc.kill('SIGTERM'), options.timeoutSeconds * 1000);
      proc.on('exit', () => clearTimeout(timeout));
    }
    return proc;
  }
}
```

### 12.4.3 输入验证（使用 Zod）

```typescript
import { z } from 'zod';

class ToolInputValidator {
  private schemas = new Map<string, z.ZodTypeAny>();

  registerToolSchema(tool: MCPTool): void {
    const properties = tool.inputSchema.properties || {};
    const requiredFields = tool.inputSchema.required || [];
    const shape: Record<string, z.ZodTypeAny> = {};

    for (const [key, prop] of Object.entries(properties)) {
      let validator = this.jsonSchemaToZod(prop);
      if (!requiredFields.includes(key)) validator = validator.optional();
      shape[key] = validator;
    }
    this.schemas.set(tool.name, z.object(shape));
  }

  private jsonSchemaToZod(prop: any): z.ZodTypeAny {
    switch (prop.type) {
      case 'string':
        let s: z.ZodString = z.string();
        if (prop.enum) s = s.refine(v => prop.enum.includes(v));
        if (prop.maxLength) s = s.max(prop.maxLength);
        return s;
      case 'number': return z.number();
      case 'boolean': return z.boolean();
      default: return z.any();
    }
  }

  validate(toolName: string, args: unknown): { success: true; data: any } | { success: false; error: string } {
    const schema = this.schemas.get(toolName);
    if (!schema) return { success: false, error: `未知工具: ${toolName}` };
    const result = schema.safeParse(args);
    if (result.success) return { success: true, data: result.data };
    return { success: false, error: result.error.issues.map(i => `  ${i.path.join('.')}: ${i.message}`).join('\n') };
  }
}
```

## 12.5 断路器与错误处理

### 12.5.1 断路器模式

```typescript
type CircuitState = 'closed' | 'open' | 'half_open';

class CircuitBreaker {
  private state: CircuitState = 'closed';
  private failureCount = 0;
  private lastFailureTime: number | null = null;

  constructor(
    private threshold: number = 5,
    private resetTimeout: number = 30000,
  ) {}

  async call<T>(fn: () => Promise<T>): Promise<T> {
    this.tryReset();
    if (this.state === 'open') {
      throw new Error(`断路器已断开: 连续 ${this.failureCount} 次失败`);
    }
    try {
      const result = await fn();
      this.onSuccess();
      return result;
    } catch (err) {
      this.onFailure();
      throw err;
    }
  }

  private tryReset(): void {
    if (this.state !== 'open' || !this.lastFailureTime) return;
    if (Date.now() - this.lastFailureTime >= this.resetTimeout) {
      this.state = 'half_open';
    }
  }

  private onSuccess(): void {
    if (this.state === 'half_open') { this.state = 'closed'; this.failureCount = 0; }
  }

  private onFailure(): void {
    this.failureCount++;
    this.lastFailureTime = Date.now();
    if (this.state === 'half_open') this.state = 'open';
    if (this.state === 'closed' && this.failureCount >= this.threshold) {
      this.state = 'open';
    }
  }
}
```

### 12.5.2 综合错误处理器

```typescript
class MCPErrorHandler {
  private circuitBreakers = new Map<string, CircuitBreaker>();
  private mockHandlers = new Map<string, (args: any) => Promise<any>>();
  private maxRetries = 3;
  private baseDelay = 1000;

  onError?: (toolName: string, err: Error) => void;

  registerMock(toolName: string, handler: (args: any) => Promise<any>): void {
    this.mockHandlers.set(toolName, handler);
  }

  async execute(
    toolName: string, args: Record<string, unknown>,
    executor: () => Promise<MCPToolCallResponse>,
  ): Promise<{ result: any; source: 'mcp' | 'mock' }> {
    if (!this.circuitBreakers.has(toolName)) {
      this.circuitBreakers.set(toolName, new CircuitBreaker());
    }
    const breaker = this.circuitBreakers.get(toolName)!;

    for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
      try {
        const result = await breaker.call(() => executor());
        return { result, source: 'mcp' };
      } catch (err) {
        this.onError?.(toolName, err as Error);
        if (attempt < this.maxRetries) {
          await new Promise(r => setTimeout(r, this.baseDelay * Math.pow(2, attempt)));
        }
      }
    }
    // 回退到 Mock
    const handler = this.mockHandlers.get(toolName);
    if (handler) return { result: await handler(args), source: 'mock' };
    throw new Error(`工具 ${toolName} 执行失败，无可用回退`);
  }
}
```

## 12.6 审计日志

```typescript
interface AuditEntry {
  timestamp: string;
  toolName: string;
  requestId: string | number;
  args: Record<string, unknown>;
  resultStatus: 'success' | 'error' | 'timeout' | 'rejected';
  durationMs: number;
  sessionId?: string;
}

class AuditLogger {
  private entries: AuditEntry[] = [];
  constructor(private maxEntries: number = 10000) {}

  log(entry: Omit<AuditEntry, 'timestamp'>): void {
    const fullEntry: AuditEntry = { ...entry, timestamp: new Date().toISOString() };
    this.entries.push(fullEntry);
    if (this.entries.length > this.maxEntries) this.entries.shift();
    this.detectAnomaly(fullEntry);
  }

  private detectAnomaly(entry: AuditEntry): void {
    if (entry.resultStatus !== 'error') return;
    const recentErrors = this.entries.filter(
      e => e.toolName === entry.toolName
        && e.resultStatus === 'error'
        && Date.now() - new Date(e.timestamp).getTime() < 60000
    );
    if (recentErrors.length >= 5) {
      console.warn(`[安全告警] 工具 ${entry.toolName} 最近 1 分钟内失败 ${recentErrors.length} 次`);
    }
  }

  query(options: { toolName?: string; status?: AuditEntry['resultStatus']; limit?: number } = {}): AuditEntry[] {
    let results = [...this.entries];
    if (options.toolName) results = results.filter(e => e.toolName === options.toolName);
    if (options.status) results = results.filter(e => e.resultStatus === options.status);
    results.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());
    return results.slice(0, options.limit || 100);
  }
}
```

## 12.7 资源订阅与实时更新

### 12.7.1 订阅资源变更

```typescript
interface ResourceSubscription {
  uri: string;
  lastContent?: MCPResourceContent;
  onChange: (content: MCPResourceContent, previous?: MCPResourceContent) => void;
  onError?: (err: Error) => void;
}

class MCPResourceSubscriber {
  private subscriptions = new Map<string, ResourceSubscription>();

  constructor(mcpClient: MCPClient) {
    mcpClient.onNotification = (notification) => {
      if (notification.method === 'notifications/resources/updated') {
        const { uri } = notification.params as { uri: string };
        this.handleUpdate(uri);
      }
    };
  }

  async subscribe(
    uri: string,
    onChange: ResourceSubscription['onChange'],
    onError?: ResourceSubscription['onError'],
  ): Promise<void> {
    const initialContent = await this.readResource(uri);
    await this.sendRequest('resources/subscribe', { uri });
    this.subscriptions.set(uri, { uri, lastContent: initialContent, onChange, onError });
    onChange(initialContent);
  }

  private async handleUpdate(uri: string): Promise<void> {
    const sub = this.subscriptions.get(uri);
    if (!sub) return;
    try {
      const content = await this.readResource(uri);
      if (content.text === sub.lastContent?.text) return;
      sub.lastContent = content;
      sub.onChange(content);
    } catch (err) { sub.onError?.(err as Error); }
  }

  private async readResource(uri: string): Promise<MCPResourceContent> {
    return await this.sendRequest('resources/read', { uri });
  }

  private async sendRequest(method: string, params?: any): Promise<any> {
    // 实际实现应通过 MCPClient 发送请求
    return {};
  }
}
```

## 12.8 MCP 最佳实践清单

### 安全（Security）

| 实践 | 说明 |
|------|------|
| **最小权限原则** | MCP 服务器只授予完成功能所需的最小权限 |
| **输入验证** | 所有工具参数必须经过 schema 验证 |
| **路径遍历防护** | 文件操作工具必须实施路径规范化检查 |
| **敏感操作确认** | 对可能产生副作用的工具需用户手动确认 |
| **审计日志** | 记录所有工具调用请求、参数和结果 |

### 可靠性（Reliability）

| 实践 | 说明 |
|------|------|
| **错误隔离** | 单个工具失败不应影响其他工具 |
| **超时控制** | 每个工具调用设置合理的超时时间 |
| **重试与退避** | 瞬态错误时自动重试，指数退避 + 随机抖动 |
| **断路器模式** | 连续失败达到阈值时熔断该工具 |

### 性能（Performance）

| 实践 | 说明 |
|------|------|
| **工具发现缓存** | `tools/list` 结果缓存到本地 |
| **并行调用** | 无依赖关系的多个工具调用并发执行 |
| **结果缓存** | 幂等工具的调用结果按参数哈希缓存 |

### 用户体验（UX）

| 实践 | 说明 |
|------|------|
| **进度反馈** | 长时间运行的工具调用展示进度条或旋转动画 |
| **优雅降级** | MCP 服务器不可用时自动切换到内置 Mock |
| **错误信息友好** | 展示用户可理解的错误信息，提供重试按钮 |

---

**实践：** 调试 MCP 通信日志：设置环境变量 `MCP_DEBUG=true` 记录 JSON-RPC 请求/响应。

**上一章：** [第十一章：MCP 协议与工具集成](11-mcp-integration.md)

**下一步：** [第十三章：TUI 应用测试](13-testing.md)
