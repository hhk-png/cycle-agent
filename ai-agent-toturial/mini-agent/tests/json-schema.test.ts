import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validate } from '../src/util/json-schema.ts';
import type { JsonSchema } from '../src/types.ts';

test('通过合法参数', () => {
  const schema: JsonSchema = {
    type: 'object',
    properties: { expression: { type: 'string', minLength: 1 } },
    required: ['expression'],
  };
  assert.deepEqual(validate({ expression: '2+3' }, schema), []);
});

test('缺必填字段报错', () => {
  const schema: JsonSchema = { type: 'object', required: ['expression'], properties: { expression: { type: 'string' } } };
  const errors = validate({}, schema);
  assert.equal(errors.length, 1);
  assert.match(errors[0]!, /缺少必填字段/);
});

test('类型错误报错', () => {
  const schema: JsonSchema = { type: 'object', properties: { n: { type: 'integer' } }, required: ['n'] };
  const errors = validate({ n: 'x' }, schema);
  assert.equal(errors.length, 1);
  assert.match(errors[0]!, /期望类型/);
});

test('enum 校验', () => {
  const schema: JsonSchema = { type: 'string', enum: ['a', 'b'] };
  assert.equal(validate('c', schema).length, 1);
  assert.equal(validate('a', schema).length, 0);
});

test('additionalProperties=false 拒绝未知字段', () => {
  const schema: JsonSchema = { type: 'object', properties: { x: { type: 'number' } }, additionalProperties: false };
  assert.equal(validate({ x: 1, y: 2 }, schema).length, 1);
  assert.equal(validate({ x: 1 }, schema).length, 0);
});

test('嵌套数组校验', () => {
  const schema: JsonSchema = { type: 'array', items: { type: 'number' } };
  assert.equal(validate([1, 2, '3'], schema).length, 1);
  assert.equal(validate([1, 2, 3], schema).length, 0);
});
