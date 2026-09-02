import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ToolRegistry } from '../src/tools/registry.ts';
import { createBuiltinTools } from '../src/tools/builtin.ts';
import type { ToolContext } from '../src/tools/types.ts';

const ctx: ToolContext = { log: () => {} };

test('注册与列出工具', () => {
  const registry = new ToolRegistry().registerMany(createBuiltinTools());
  assert.ok(registry.has('calculator'));
  assert.ok(registry.get('get_time'));
  const names = registry.list().map((t) => t.name);
  assert.ok(names.includes('calculator'));
  assert.ok(names.includes('web_search'));
});

test('重复注册抛错', () => {
  const registry = new ToolRegistry();
  registry.register(createBuiltinTools()[0]!);
  assert.throws(() => registry.register(createBuiltinTools()[0]!));
});

test('calculator 正确执行', async () => {
  const registry = new ToolRegistry().registerMany(createBuiltinTools());
  const out = await registry.call('calculator', { expression: '(2+3)*4' }, ctx);
  assert.equal(out.content, '20');
  assert.equal(out.isError, false);
});

test('参数校验失败返回 isError', async () => {
  const registry = new ToolRegistry().registerMany(createBuiltinTools());
  const out = await registry.call('calculator', {}, ctx);
  assert.equal(out.isError, true);
  assert.match(out.content, /参数校验失败/);
});

test('执行异常不向外抛,而是 isError', async () => {
  const registry = new ToolRegistry().registerMany(createBuiltinTools());
  const out = await registry.call('calculator', { expression: '1/0' }, ctx);
  assert.equal(out.isError, true);
  assert.match(out.content, /除以零|计算失败/);
});

test('未知工具返回 isError', async () => {
  const registry = new ToolRegistry();
  const out = await registry.call('nope', {}, ctx);
  assert.equal(out.isError, true);
});

test('toDefinitions 返回模型可用的工具描述', () => {
  const registry = new ToolRegistry().registerMany(createBuiltinTools());
  const defs = registry.toDefinitions();
  const calc = defs.find((d) => d.name === 'calculator');
  assert.ok(calc);
  assert.equal(calc?.inputSchema.required?.[0], 'expression');
});
