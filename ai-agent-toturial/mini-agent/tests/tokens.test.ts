import { test } from 'node:test';
import assert from 'node:assert/strict';
import { TokenEstimator } from '../src/util/tokens.ts';

const est = new TokenEstimator();

test('中文按 1 字符 ≈ 1 token', () => {
  const tokens = est.estimateText('人工智能');
  assert.equal(tokens, 4);
});

test('英文按 4 字符 ≈ 1 token', () => {
  const tokens = est.estimateText('hello world'); // 11 字符
  assert.equal(tokens, 3); // ceil(11/4)=3
});

test('空文本为 0', () => {
  assert.equal(est.estimateText(''), 0);
});

test('estimate 汇总多条消息并计入角色开销', () => {
  const msgs = [
    { role: 'system', content: '你是一个助手' },
    { role: 'user', content: 'hello' },
  ];
  const total = est.estimate(msgs);
  assert.ok(total > est.estimateText('你是一个助手hello'));
});
