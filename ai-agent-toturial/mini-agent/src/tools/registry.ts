/**
 * tools/registry.ts —— 工具注册中心
 *
 * 负责三件事:
 *   1. 注册/反注册工具;
 *   2. 校验模型传来的参数(JSON Schema);
 *   3. 执行工具并统一把异常转成 ToolOutput(isError),保证 Agent 循环不会
 *      因为某个工具崩溃而整体挂掉。
 *
 * 参数校验在这里集中做,而不是散落在各工具里:
 * 一个模型可能同时发起 5 个工具调用,其中一个参数类型错误,
 * 应当在该工具调用处失败,而不是污染整个循环。
 */

import type { JsonSchema, ToolDefinition, ToolOutput } from '../types.ts';
import type { Tool, ToolContext } from './types.ts';
import { validate } from '../util/json-schema.ts';

export class ToolRegistry {
  private tools = new Map<string, Tool>();

  register(tool: Tool): this {
    if (this.tools.has(tool.name)) {
      throw new Error(`工具 ${tool.name} 重复注册`);
    }
    this.tools.set(tool.name, tool);
    return this;
  }

  registerMany(tools: Tool[]): this {
    for (const t of tools) this.register(t);
    return this;
  }

  /** 反注册(测试或动态能力时用)。 */
  unregister(name: string): boolean {
    return this.tools.delete(name);
  }

  get(name: string): Tool | undefined {
    return this.tools.get(name);
  }

  has(name: string): boolean {
    return this.tools.has(name);
  }

  /** 按名字排序返回,保证工具列表的确定性(对 trace 与测试友好)。 */
  list(): Tool[] {
    return [...this.tools.values()].sort((a, b) => a.name.localeCompare(b.name));
  }

  /** 序列化成模型工具描述(供 provider 使用)。 */
  toDefinitions(): ToolDefinition[] {
    return this.list().map(({ name, description, inputSchema }) => ({ name, description, inputSchema }));
  }

  /**
   * 执行工具。参数校验失败或执行抛错都不会向外抛异常,
   * 而是返回 isError 结果——Agent 会把错误信息回喂给模型,让它自行修正。
   */
  async call(name: string, args: Record<string, unknown>, ctx: ToolContext): Promise<ToolOutput> {
    const tool = this.tools.get(name);
    if (!tool) {
      return { content: `未知工具:${name}`, isError: true };
    }

    const schemaErrors = validate(args, tool.inputSchema);
    if (schemaErrors.length > 0) {
      return {
        content: `参数校验失败:\n${schemaErrors.join('\n')}`,
        isError: true,
      };
    }

    const startedAt = Date.now();
    try {
      const output = await tool.execute(args, ctx);
      return { content: output.content, isError: output.isError === true, durationMs: Date.now() - startedAt };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return {
        content: `工具执行异常:${msg}`,
        isError: true,
        durationMs: Date.now() - startedAt,
      };
    }
  }
}
