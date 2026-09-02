import { test } from 'node:test';
import assert from 'node:assert/strict';
import { StdioMcpClient } from '../src/mcp/client.ts';
import { mcpToolsToTools } from '../src/mcp/adaptor.ts';
import { ToolRegistry } from '../src/tools/registry.ts';

// 所有测试共享一个服务器进程;用 before/after 控制生命周期
import { before, after } from 'node:test';

let client: StdioMcpClient;

before(async () => {
  client = new StdioMcpClient(process.execPath, ['examples/mock-mcp-server.ts']);
  await client.connect();
});

after(async () => {
  await client.close();
});

test('MCP 握手:拿到服务器信息', async () => {
  const c2 = new StdioMcpClient(process.execPath, ['examples/mock-mcp-server.ts']);
  const info = await c2.connect();
  assert.equal(info.name, 'mock-mcp-server');
  assert.ok(info.protocolVersion.startsWith('2024-'));
  assert.ok('tools' in info.capabilities);
  await c2.close();
});

test('MCP tools/list:发现 3 个工具', async () => {
  const tools = await client.listTools();
  const names = tools.map((t) => t.name);
  assert.ok(names.includes('weather_get'));
  assert.ok(names.includes('echo'));
  assert.ok(names.includes('read_note'));
});

test('MCP tools/call:weather_get 返回城市天气', async () => {
  const result = await client.callTool('weather_get', { city: '北京' });
  assert.equal(result.isError, false);
  const text = result.content.map((c) => c.text ?? '').join('');
  assert.match(text, /北京/);
});

test('MCP tools/call:echo 原样返回', async () => {
  const result = await client.callTool('echo', { text: 'ping' });
  assert.equal(result.content[0]!.text, 'ping');
});

test('MCP 工具适配成本地 Tool 后可直接执行', async () => {
  const tools = await mcpToolsToTools(client);
  const registry = new ToolRegistry().registerMany(tools);
  const out = await registry.call('weather_get', { city: '上海' }, { log: () => {} });
  assert.equal(out.isError, false);
  assert.match(out.content, /上海/);
});

test('未知工具返回 isError', async () => {
  const result = await client.callTool('not_exist', {});
  assert.equal(result.isError, true);
});
