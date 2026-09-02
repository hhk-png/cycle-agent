import { test } from 'node:test';
import assert from 'node:assert/strict';
import { VectorMemory } from '../src/memory/vector.ts';
import { ConversationBuffer } from '../src/memory/conversation.ts';
import { MemoryManager } from '../src/memory/manager.ts';
import type { Message } from '../src/types.ts';

test('向量记忆:相似文本检索排名靠前', () => {
  const vm = new VectorMemory();
  vm.add('我的生日是 1998 年 3 月');
  vm.add('今天天气很好,适合出游');
  vm.add('生日蛋糕要提前预订');
  const hits = vm.search('生日是什么时候', 2);
  assert.ok(hits.length >= 1);
  assert.ok(hits[0]!.text.includes('生日'), `应命中生日相关,实际:${hits[0]!.text}`);
});

test('向量记忆:阈值过滤', () => {
  const vm = new VectorMemory();
  vm.add('讨论模型压缩技术');
  const hits = vm.search('量子物理', 5, 0.01);
  // 无关查询得分很低,应被阈值过滤
  assert.ok(hits.length === 0);
});

test('向量记忆:删除与清空', () => {
  const vm = new VectorMemory();
  const e = vm.add('foo');
  assert.equal(vm.size, 1);
  assert.ok(vm.remove(e.id));
  assert.equal(vm.size, 0);
});

test('对话缓冲:滑动窗口丢弃最旧消息', () => {
  const buf = new ConversationBuffer({ maxMessages: 5 });
  for (let i = 0; i < 10; i++) {
    buf.add({ role: 'user', content: `msg${i}` });
  }
  assert.equal(buf.size, 5);
  const msgs = buf.messages();
  assert.equal(msgs[0]!.content, 'msg5');
  assert.equal(msgs[4]!.content, 'msg9');
});

test('对话缓冲:system 消息不被丢弃且不占配额', () => {
  const buf = new ConversationBuffer({ maxMessages: 3 });
  buf.add({ role: 'system', content: 'sys' });
  for (let i = 0; i < 5; i++) buf.add({ role: 'user', content: `m${i}` });
  const msgs = buf.messages();
  assert.equal(msgs[0]!.role, 'system');
  // 配额只约束非 system 消息:保留最近 3 条用户消息 + system = 4 条
  assert.equal(msgs.length, 4);
  assert.equal(msgs[1]!.content, 'm2');
  assert.equal(msgs[3]!.content, 'm4');
});

test('MemoryManager:remember 与 saveFact 协同', () => {
  const mm = new MemoryManager();
  mm.remember({ role: 'user', content: 'hello' } as Message);
  mm.saveFact('用户的昵称是小明');
  assert.equal(mm.factCount, 1);
  assert.equal(mm.recent(10).length, 1);
  const ctx = mm.recallAsContext('昵称');
  assert.ok(ctx.includes('小明'));
});
