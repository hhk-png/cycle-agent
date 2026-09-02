/**
 * mock-mcp-server.ts —— 一个「假」MCP 服务器(stdio)
 *
 * 用于离线演示与测试 mini-agent 的 MCP 客户端。它实现了经典 MCP
 * 的 4 个操作:initialize / notifications/initialized / tools/list / tools/call,
 * 使用换行分隔 JSON-RPC 2.0 与客户端通信(stdout 为协议通道,stderr 为日志)。
 *
 * 你可以在终端单独运行它,然后手动输入 JSON-RPC 看它响应:
 *   node examples/mock-mcp-server.ts
 *
 * 通过一个真实的进程边界,这套代码证明了 Agent 可以通过 MCP 协议
 * 与「另一个程序」对话——这正是 MCP 的用武之地。
 */

import * as readline from 'node:readline';
import * as fs from 'node:fs/promises';

const SERVER_NAME = 'mock-mcp-server';
const SERVER_VERSION = '0.1.0';
const PROTOCOL_VERSION = '2024-11-05';

function log(line: string): void {
  // stderr 是日志通道,stdout 必须是纯 NDJSON
  process.stderr.write(`[${SERVER_NAME}] ${line}\n`);
}

function send(obj: Record<string, unknown>): void {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

/** 工具实现。 */
async function runTool(name: string, args: Record<string, unknown>): Promise<{ content: Array<Record<string, string>>; isError?: boolean }> {
  switch (name) {
    case 'weather_get': {
      const city = String(args.city ?? '未知城市');
      const forecasts = [
        { city: '北京', weather: '晴,24°C' },
        { city: '上海', weather: '多云,27°C' },
        { city: '深圳', weather: '雷阵雨,30°C' },
      ];
      const hit = forecasts.find((f) => f.city.includes(city.replace('市', '')) || city.includes(f.city));
      if (hit) {
        return { content: [{ type: 'text', text: `${hit.city}:${hit.weather}` }] };
      }
      return { content: [{ type: 'text', text: `${city}:暂无数据(mock)` }], isError: true };
    }
    case 'echo': {
      return { content: [{ type: 'text', text: String(args.text ?? '') }] };
    }
    case 'read_note': {
      const path = `./.mcp-notes/${String(args.name ?? 'note')}.txt`;
      try {
        const content = await fs.readFile(path, 'utf-8');
        return { content: [{ type: 'text', text: content }] };
      } catch {
        return { content: [{ type: 'text', text: `笔记 ${args.name} 不存在` }], isError: true };
      }
    }
    default:
      return { content: [{ type: 'text', text: `未知工具:${name}` }], isError: true };
  }
}

function handleRequest(msg: { id: number; method: string; params: Record<string, unknown> }): void {
  const { id, method, params } = msg;
  switch (method) {
    case 'initialize':
      send({
        jsonrpc: '2.0',
        id,
        result: {
          protocolVersion: PROTOCOL_VERSION,
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
        },
      });
      return;

    case 'notifications/initialized':
      log('客户端已完成初始化');
      return;

    case 'tools/list':
      send({
        jsonrpc: '2.0',
        id,
        result: {
          tools: [
            {
              name: 'weather_get',
              description: '查询某个城市的天气(北京/上海/深圳)。',
              inputSchema: {
                type: 'object',
                properties: { city: { type: 'string', description: '城市名' } },
                required: ['city'],
              },
            },
            {
              name: 'echo',
              description: '把传入的文本原样返回。',
              inputSchema: {
                type: 'object',
                properties: { text: { type: 'string' } },
                required: ['text'],
              },
            },
            {
              name: 'read_note',
              description: '读取一张本地笔记(mock 文件系统)。',
              inputSchema: {
                type: 'object',
                properties: { name: { type: 'string' } },
                required: ['name'],
              },
            },
          ],
        },
      });
      return;

    case 'tools/call':
      void (async () => {
        const name = String(params.name ?? '');
        const args = (params.arguments as Record<string, unknown>) ?? {};
        const result = await runTool(name, args);
        send({ jsonrpc: '2.0', id, result });
      })();
      return;

    default:
      send({
        jsonrpc: '2.0',
        id,
        error: { code: -32601, message: `不支持的方法:${method}` },
      });
  }
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let msg: { id?: number; method?: string; params?: Record<string, unknown> };
  try {
    msg = JSON.parse(trimmed);
  } catch {
    log(`无法解析:${trimmed.slice(0, 80)}`);
    return;
  }
  if (typeof msg.method === 'string') {
    handleRequest({ id: msg.id ?? 0, method: msg.method, params: msg.params ?? {} });
  }
});

log(`${SERVER_NAME} v${SERVER_VERSION} 已启动,等待 JSON-RPC...`);
