/**
 * mcp/client.ts —— 极简 MCP(stdio)客户端
 *
 * Model Context Protocol 是一套让 LLM 应用连接外部工具/数据的开放协议
 * (教程第 08 章详述)。mini-agent 实现了其中最有价值的一条路径:
 *   - stdio 传输(子进程 + stdin/stdout 上的换行分隔 JSON-RPC 2.0);
 *   - initialize 握手(协议版本 + 能力协商);
 *   - tools/list 发现工具;
 *   - tools/call 调用工具;
 *   - notifications/initialized 通知。
 *
 * 这个客户端零依赖、~200 行,足以连接真实的 MCP 服务器
 * (官方 TypeScript SDK 的实现思路一致:JSON-RPC + 待决请求表)。
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import type { JsonSchema } from '../types.ts';

export interface McpServerInfo {
  name: string;
  version: string;
  capabilities: Record<string, unknown>;
  protocolVersion: string;
}

export interface McpToolInfo {
  name: string;
  description?: string;
  inputSchema: JsonSchema;
}

export interface McpContentPart {
  type: string;
  text?: string;
}

export interface McpCallResult {
  content: McpContentPart[];
  isError?: boolean;
}

interface PendingRequest {
  resolve: (result: unknown) => void;
  reject: (err: Error) => void;
  method: string;
}

export interface StdioMcpClientOptions {
  protocolVersion?: string;
  /** 服务端 stderr 日志回调(默认打印到 console.error)。 */
  onServerLog?: (line: string) => void;
}

export class StdioMcpClient {
  readonly protocolVersion: string;
  private readonly cmd: string;
  private readonly args: string[];
  private child: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<number, PendingRequest>();
  private seq = 0;
  private buffer = '';
  private closed = false;
  private onServerLog: (line: string) => void;

  constructor(cmd: string, args: string[], opts: StdioMcpClientOptions = {}) {
    this.cmd = cmd;
    this.args = args;
    this.protocolVersion = opts.protocolVersion ?? '2024-11-05';
    this.onServerLog = opts.onServerLog ?? ((line) => console.error(`[mcp-server] ${line}`));
  }

  /** 启动子进程并完成握手,返回服务端信息。 */
  async connect(): Promise<McpServerInfo> {
    if (this.child) throw new Error('已经连接');

    const child = spawn(this.cmd, this.args, { stdio: ['pipe', 'pipe', 'pipe'] });
    this.child = child;

    child.stderr.on('data', (chunk: Buffer) => {
      for (const line of chunk.toString().split('\n')) {
        if (line.trim()) this.onServerLog(line);
      }
    });

    child.stdout.on('data', (chunk: Buffer) => {
      this.buffer += chunk.toString();
      let nl: number;
      while ((nl = this.buffer.indexOf('\n')) >= 0) {
        const line = this.buffer.slice(0, nl).trim();
        this.buffer = this.buffer.slice(nl + 1);
        if (!line) continue;
        this.handleLine(line);
      }
    });

    child.on('exit', (code) => {
      const err = new Error(`MCP 服务器进程退出,code=${code}`);
      for (const [, p] of this.pending) p.reject(err);
      this.pending.clear();
      this.child = null;
    });

    const result = (await this.request('initialize', {
      protocolVersion: this.protocolVersion,
      capabilities: {},
      clientInfo: { name: 'mini-agent', version: '1.0.0' },
    })) as Record<string, unknown>;

    this.notify('notifications/initialized');

    return {
      name: String((result.serverInfo as Record<string, unknown>)?.name ?? 'unknown'),
      version: String((result.serverInfo as Record<string, unknown>)?.version ?? '0'),
      capabilities: (result.capabilities as Record<string, unknown>) ?? {},
      protocolVersion: String(result.protocolVersion ?? this.protocolVersion),
    };
  }

  /** 列出服务器暴露的工具。 */
  async listTools(): Promise<McpToolInfo[]> {
    const result = (await this.request('tools/list', {})) as { tools?: McpToolInfo[] };
    return (result.tools ?? []).map((t) => ({
      name: t.name,
      description: t.description,
      inputSchema: (t.inputSchema ?? { type: 'object' }) as JsonSchema,
    }));
  }

  /** 调用一个工具。 */
  async callTool(name: string, args: Record<string, unknown>): Promise<McpCallResult> {
    const result = (await this.request('tools/call', { name, arguments: args })) as McpCallResult;
    return { content: result.content ?? [], isError: result.isError === true };
  }

  /** 关闭子进程并清理。 */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    const child = this.child;
    this.child = null;
    if (child) {
      child.stdin.end();
      const timer = setTimeout(() => child.kill('SIGKILL'), 1000);
      timer.unref();
      await new Promise<void>((resolve) => {
        if (child.exitCode !== null) return resolve();
        child.once('exit', () => resolve());
      });
    }
    for (const [, p] of this.pending) p.reject(new Error('客户端已关闭'));
    this.pending.clear();
  }

  private request(method: string, params: Record<string, unknown>): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const id = ++this.seq;
      this.pending.set(id, { resolve, reject, method });
      this.send({ jsonrpc: '2.0', id, method, params });
    });
  }

  private notify(method: string, params?: Record<string, unknown>): void {
    this.send({ jsonrpc: '2.0', method, params });
  }

  private send(obj: Record<string, unknown>): void {
    if (!this.child || this.closed) throw new Error('MCP 客户端未连接');
    this.child.stdin.write(JSON.stringify(obj) + '\n');
  }

  private handleLine(line: string): void {
    let msg: { id?: number; method?: string; result?: unknown; error?: { message?: string }; params?: Record<string, unknown> };
    try {
      msg = JSON.parse(line);
    } catch {
      this.onServerLog(`无法解析的 JSON:${line.slice(0, 80)}`);
      return;
    }

    // 响应
    if (msg.id !== undefined) {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if (msg.error) p.reject(new Error(`MCP 错误(${p.method}):${msg.error.message ?? 'unknown'}`));
      else p.resolve(msg.result);
      return;
    }

    // 服务器主动通知(教学实现:仅记录,真实客户端会按需处理 sampling / prompts)
    if (msg.method) {
      this.onServerLog(`收到通知:${msg.method}`);
    }
  }
}
