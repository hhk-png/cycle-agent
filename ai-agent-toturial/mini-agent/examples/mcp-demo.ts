/**
 * mcp-demo.ts —— 通过 MCP 协议调用外部程序的能力
 *
 * 运行:node examples/mcp-demo.ts
 *
 * 演示流程:
 *   1. 用 StdioMcpClient 启动 examples/mock-mcp-server.ts(一个独立的子进程);
 *   2. 完成 MCP 握手(initialize + initialized);
 *   3. tools/list 发现服务器提供的工具;
 *   4. 用 mcpToolsToTools 把 MCP 工具适配成本地 Tool;
 *   5. Agent 把它当成普通工具使用(weather_get)。
 *
 * 这证明:只要实现 MCP 协议,Agent 就能与任何语言/任何进程写的
 * 工具对话——这正是「协议化集成」的价值。
 */

import { StdioMcpClient } from '../src/mcp/client.ts';
import { mcpToolsToTools } from '../src/mcp/adaptor.ts';
import { Agent } from '../src/agent.ts';
import { MockProvider, type MockResponder } from '../src/provider/mock.ts';
import { ToolRegistry } from '../src/tools/registry.ts';
import type { Message } from '../src/types.ts';

async function main(): Promise<void> {
  // 1. 启动 MCP 服务器(子进程)
  const client = new StdioMcpClient(process.execPath, ['examples/mock-mcp-server.ts']);
  const info = await client.connect();
  console.log(`已连接 MCP 服务器:${info.name} v${info.version} (协议 ${info.protocolVersion})`);

  // 2. 发现工具
  const tools = await mcpToolsToTools(client);
  console.log(`发现 ${tools.length} 个 MCP 工具:${tools.map((t) => t.name).join(', ')}`);

  // 3. 注册进 Agent
  const registry = new ToolRegistry().registerMany(tools);

  // 4. 用 MockProvider 让模型「决定」调用天气工具
  const responder: MockResponder = (messages: Message[]) => {
    const lastTool = [...messages].reverse().find((m) => m.role === 'tool');
    if (!lastTool) {
      return {
        text: '我来查一下上海的天气。',
        toolCalls: [{ name: 'weather_get', arguments: { city: '上海' } }],
      };
    }
    return { text: `上海天气查询结果:${lastTool.content}` };
  };

  const agent = new Agent({
    name: 'weather-bot',
    provider: new MockProvider({ responder }),
    registry,
    systemPrompt: '使用天气工具回答用户问题。',
  });
  agent.on('tool_call', (p) => console.log(`  [行动] ${p.call.name}(${JSON.stringify(p.call.arguments)})`));
  agent.on('tool_result', (p) => console.log(`  [观察] ${p.output.content}`));

  const result = await agent.run('上海的天气怎么样?');
  console.log(`\n回答:${result.output}`);
  console.log(`统计:${result.stats.llmCalls} 次模型调用 / ${result.stats.toolCalls} 次工具调用`);

  await client.close();
  console.log('\nMCP 连接已关闭');
}

void main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
