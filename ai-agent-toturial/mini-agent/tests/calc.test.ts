import { test } from 'node:test';
import assert from 'node:assert/strict';
import { safeEvaluate } from '../src/util/calc.ts';

test('基础四则运算', () => {
  assert.equal(safeEvaluate('2+3').value, 5);
  assert.equal(safeEvaluate('2+3*4').value, 14);
  assert.equal(safeEvaluate('(2+3)*4').value, 20);
  assert.equal(safeEvaluate('10/4').value, 2.5);
  assert.equal(safeEvaluate('10%3').value, 1);
  assert.equal(safeEvaluate('-5+2').value, -3);
});

test('小数与括号', () => {
  assert.equal(safeEvaluate('(1.5 + 2.5) * 2').value, 8);
});

test('非法表达式返回错误而非抛出', () => {
  const r = safeEvaluate('2 + * 3');
  assert.equal(r.ok, false);
  assert.match(r.error ?? '', /非法|错误|缺少/);
});

test('除以零报错', () => {
  const r = safeEvaluate('1/0');
  assert.equal(r.ok, false);
});

test('空表达式', () => {
  assert.equal(safeEvaluate('').ok, false);
});

test('不允许代码注入', () => {
  // 任何非数学字符都会被拒绝
  assert.equal(safeEvaluate('process.exit()').ok, false);
  assert.equal(safeEvaluate('require("fs")').ok, false);
  assert.equal(safeEvaluate('2; global.x=1').ok, false);
});
