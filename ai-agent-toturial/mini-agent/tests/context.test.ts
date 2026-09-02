import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ContextWindow } from '../src/context/window.ts';
import { TokenEstimator } from '../src/util/tokens.ts';
import type { Message } from '../src/types.ts';

function msg(role: Message['role'], content: string): Message {
  return { role, content };
}

test('预算足够时不裁剪', () => {
  const cw = new ContextWindow({ maxTokens: 10_000 });
  const msgs = [msg('system', 's'), msg('user', 'hello'), msg('assistant', 'world')];
  const r = cw.trim(msgs);
  assert.equal(r.dropped, 0);
  assert.equal(r.messages.length, 3);
});

test('超预算时保留 system 与最近消息', () => {
  const cw = new ContextWindow({ maxTokens: 60 });
  const msgs: Message[] = [msg('system', '系统提示')];
  for (let i = 0; i < 20; i++) msgs.push(msg('user', `第 ${i} 条消息`));
  const r = cw.trim(msgs);
  assert.ok(r.dropped > 0);
  assert.equal(r.messages[0]!.role, 'system');
  // 剩余必须都在预算内
  assert.ok(cw.estimator.estimate(r.messages) <= 60);
  // 最后一条保留
  assert.equal(r.messages.at(-1)!.content, '第 19 条消息');
});

test('system 过长时被截断', () => {
  const cw = new ContextWindow({ maxTokens: 50 });
  const longSys = '非常长的系统提示 '.repeat(100);
  const r = cw.trim([msg('system', longSys), msg('user', 'hi')]);
  const sysLen = r.messages[0]!.content.length;
  assert.ok(sysLen < longSys.length, 'system 应被截断');
});
