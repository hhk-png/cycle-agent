/**
 * tools/builtin.ts —— 内置工具集
 *
 * 用「工厂函数 + 依赖注入」的方式组装,而不是全局单例:
 * 每个 Agent 可以拥有自己的 sandboxDir、memoryManager,互不污染。
 * 这是工程化里最常见的组合方式——把依赖从「工具内部 new 出来」
 * 变成「由外部注入」,工具才可测试、可复用。
 */

import * as path from 'node:path';
import * as fs from 'node:fs/promises';
import { safeEvaluate } from '../util/calc.ts';
import type { MemoryManager } from '../memory/manager.ts';
import type { Tool } from './types.ts';

export interface BuiltinToolOptions {
  /** read_file / write_file 的沙箱根目录;缺省为当前目录,不配置则禁用文件工具。 */
  sandboxDir?: string;
  /** remember 工具要写入的记忆管理器。 */
  memoryManager?: MemoryManager;
  /** 注入一个可选的网络检索函数,代替 mock web_search。 */
  webSearch?: (query: string) => Promise<string>;
  /** 注入当前时间源,便于测试固定时间。 */
  now?: () => Date;
}

/** 校验路径在 sandboxDir 内,防止路径穿越。 */
function resolveInSandbox(sandboxDir: string, requested: string): string {
  const root = path.resolve(sandboxDir);
  const target = path.resolve(root, requested);
  if (target !== root && !target.startsWith(root + path.sep)) {
    throw new Error(`路径越界:${requested}`);
  }
  return target;
}

export function createBuiltinTools(opts: BuiltinToolOptions = {}): Tool[] {
  const now = opts.now ?? (() => new Date());
  const sandboxDir = opts.sandboxDir ? path.resolve(opts.sandboxDir) : undefined;
  const memoryManager = opts.memoryManager;
  const webSearch = opts.webSearch ?? mockWebSearch;

  const tools: Tool[] = [];

  tools.push({
    name: 'calculator',
    description: '计算一个数学表达式,支持 + - * / % 和括号。例如 "(2+3)*4"。',
    inputSchema: {
      type: 'object',
      properties: {
        expression: { type: 'string', description: '要计算的数学表达式', minLength: 1, maxLength: 200 },
      },
      required: ['expression'],
      additionalProperties: false,
    },
    async execute(args) {
      const r = safeEvaluate(String(args.expression));
      return r.ok
        ? { content: String(r.value) }
        : { content: `计算失败:${r.error}`, isError: true };
    },
  });

  tools.push({
    name: 'get_time',
    description: '获取当前的日期与时间(ISO 8601)。',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    async execute() {
      return { content: now().toISOString() };
    },
  });

  tools.push({
    name: 'web_search',
    description: '搜索网络并返回摘要结果(mock 实现,真实场景请接入搜索 API)。',
    inputSchema: {
      type: 'object',
      properties: { query: { type: 'string', minLength: 1 } },
      required: ['query'],
      additionalProperties: false,
    },
    async execute(args, ctx) {
      const q = String(args.query);
      ctx.log(`[web_search] "${q}"`);
      const content = await webSearch(q);
      return { content };
    },
  });

  if (sandboxDir) {
    tools.push({
      name: 'read_file',
      description: `读取沙箱(${sandboxDir})内文本文件的内容。`,
      inputSchema: {
        type: 'object',
        properties: { path: { type: 'string', minLength: 1 } },
        required: ['path'],
        additionalProperties: false,
      },
      async execute(args) {
        const target = resolveInSandbox(sandboxDir, String(args.path));
        const content = await fs.readFile(target, 'utf-8');
        return { content };
      },
    });

    tools.push({
      name: 'write_file',
      description: `把内容写入沙箱(${sandboxDir})内的文件(覆盖写)。`,
      inputSchema: {
        type: 'object',
        properties: {
          path: { type: 'string', minLength: 1 },
          content: { type: 'string' },
        },
        required: ['path', 'content'],
        additionalProperties: false,
      },
      async execute(args) {
        const target = resolveInSandbox(sandboxDir, String(args.path));
        await fs.writeFile(target, String(args.content), 'utf-8');
        return { content: `已写入 ${args.path}(${String(args.content).length} 字符)` };
      },
    });
  }

  tools.push({
    name: 'echo',
    description: '原样返回传入的文本,用于连通性测试。',
    inputSchema: {
      type: 'object',
      properties: { text: { type: 'string' } },
      required: ['text'],
      additionalProperties: false,
    },
    async execute(args) {
      return { content: String(args.text) };
    },
  });

  if (memoryManager) {
    tools.push({
      name: 'remember',
      description: '把一条重要事实写入长期记忆,后续对话可以检索到它。',
      inputSchema: {
        type: 'object',
        properties: { text: { type: 'string', minLength: 1, maxLength: 2000 } },
        required: ['text'],
        additionalProperties: false,
      },
      async execute(args) {
        memoryManager.saveFact(String(args.text));
        return { content: '已保存到长期记忆。' };
      },
    });
  }

  return tools;
}

/** 默认 mock 搜索:返回固定的伪结果,用于离线演示。 */
async function mockWebSearch(query: string): Promise<string> {
  return [
    `[mock search] 查询:${query}`,
    '1. 这是一条模拟的搜索结果。',
    '2. 接入真实搜索 API(如 Tavily / SerpAPI)后,这里会变成真实的摘要。',
    '3. 注意:工具是否诚实标注为 mock,会影响模型对结果可信度的判断。',
  ].join('\n');
}
