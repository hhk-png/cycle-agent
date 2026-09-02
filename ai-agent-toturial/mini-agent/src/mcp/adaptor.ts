/**
 * mcp/adaptor.ts —— 把 MCP 工具适配成本地 Tool
 *
 * 这是「协议适配」的样板:MCP 服务器的工具 → 本地 ToolRegistry 的统一格式。
 * 一旦适配,Agent 完全无感知——它调用工具的方式,与调用内置 calculator 一模一样。
 * 新增一个 MCP 服务器 = 启动一个 client + 跑一遍这个适配器,零 Agent 改动。
 */

import type { Tool } from '../tools/types.ts';
import type { StdioMcpClient } from './client.ts';

export interface McpAdapterOptions {
  /** 工具名前缀,避免多服务器工具名冲突。 */
  prefix?: string;
}

/** 连接 MCP 客户端,把其全部工具适配成本地 Tool。 */
export async function mcpToolsToTools(client: StdioMcpClient, opts: McpAdapterOptions = {}): Promise<Tool[]> {
  const tools = await client.listTools();
  const prefix = opts.prefix ? `${opts.prefix}_` : '';

  return tools.map((t) => ({
    name: `${prefix}${t.name}`,
    description: `[MCP] ${t.description ?? t.name}(来自 MCP 服务器)`,
    inputSchema: t.inputSchema,
    async execute(args, ctx) {
      ctx.log(`MCP 调用 ${t.name}`);
      const result = await client.callTool(t.name, args);
      const text = result.content
        .map((c) => (c.type === 'text' ? (c.text ?? '') : `[${c.type}]`))
        .join('\n');
      return { content: text || '(空结果)', isError: result.isError === true };
    },
  }));
}
