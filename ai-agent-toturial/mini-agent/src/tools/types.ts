/**
 * tools/types.ts —— 工具抽象
 *
 * 工具是 Agent 的「手脚」。在真实工程里,工具往往散布在各个微服务/函数库中,
 * 这里用统一的 Tool 接口把它们收敛起来,交给 ToolRegistry 统一管理。
 * 工具访问外部依赖(文件系统、数据库、记忆)的方式是注入 ToolContext,
 * 而不是自己在工具里 new——这样工具保持纯函数,便于测试。
 */

import type { JsonSchema, ToolDefinition, ToolOutput } from '../types.ts';

/** 工具执行时的上下文:依赖注入的入口。 */
export interface ToolContext {
  /** 结构化日志。 */
  log(line: string): void;
  /** 取消信号(用户中断、超时)。 */
  signal?: AbortSignal;
  /** 扩展点:内存管理器、配置、数据库句柄等,按需注入。 */
  [key: string]: unknown;
}

/** 一个可注册、可被模型调用的工具。 */
export interface Tool extends ToolDefinition {
  execute(args: Record<string, unknown>, ctx: ToolContext): Promise<ToolOutput>;
}
