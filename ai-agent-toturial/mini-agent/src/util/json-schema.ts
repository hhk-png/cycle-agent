/**
 * json-schema.ts —— 迷你 JSON Schema 校验器(draft-07 常用子集)
 *
 * 工具入参必须校验,这是 Agent 工程化的底线:
 * 模型可能产生任何参数,未经验证的参数会让工具在运行时崩溃,
 * 或者更糟——把恶意内容写进你的系统。零依赖前提下,
 * 我们实现覆盖工具场景所需的关键关键字:
 *   type / properties / required / items / enum / minimum / maximum /
 *   minLength / maxLength / pattern / additionalProperties
 *
 * 生产环境建议直接使用 ajv 等完整实现,原理相同。
 */

import type { JsonSchema } from '../types.ts';

export type ValidationError = string;

/** 校验一个值是否符合 schema,返回错误数组(空数组 = 通过)。 */
export function validate(value: unknown, schema: JsonSchema, path = '$'): ValidationError[] {
  const errors: ValidationError[] = [];
  if (!schema || typeof schema !== 'object') return errors;

  // --- 类型校验 ---
  if (schema.type !== undefined) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!types.some((t) => matchesType(value, t))) {
      errors.push(`${path}: 期望类型 ${types.join('|')},实际 ${describeType(value)}`);
      // 类型不对就直接返回,继续比较子字段没有意义,只会产生误导性错误。
      return errors;
    }
  }

  // --- enum ---
  if (schema.enum !== undefined) {
    if (!schema.enum.some((item) => deepEqual(item, value))) {
      errors.push(`${path}: 值不在 enum 允许范围内`);
    }
  }

  // --- 对象类型 ---
  if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
    const obj = value as Record<string, unknown>;

    // required
    for (const key of schema.required ?? []) {
      if (!(key in obj)) {
        errors.push(`${path}.${key}: 缺少必填字段`);
      }
    }

    // properties
    for (const [key, sub] of Object.entries(schema.properties ?? {})) {
      if (key in obj && sub) {
        errors.push(...validate(obj[key], sub, `${path}.${key}`));
      }
    }

    // additionalProperties
    if (schema.additionalProperties === false) {
      const known = new Set([...Object.keys(schema.properties ?? {}), ...(schema.required ?? [])]);
      for (const key of Object.keys(obj)) {
        if (!known.has(key)) {
          errors.push(`${path}.${key}: 不允许出现未声明的字段`);
        }
      }
    }
  }

  // --- 数组类型 ---
  if (Array.isArray(value)) {
    const items = schema.items;
    if (items) {
      value.forEach((item, i) => {
        errors.push(...validate(item, items, `${path}[${i}]`));
      });
    }
  }

  // --- 数值约束 ---
  if (typeof value === 'number') {
    if (schema.minimum !== undefined && value < schema.minimum) {
      errors.push(`${path}: 值 ${value} 小于最小值 ${schema.minimum}`);
    }
    if (schema.maximum !== undefined && value > schema.maximum) {
      errors.push(`${path}: 值 ${value} 大于最大值 ${schema.maximum}`);
    }
  }

  // --- 字符串约束 ---
  if (typeof value === 'string') {
    if (schema.minLength !== undefined && value.length < schema.minLength) {
      errors.push(`${path}: 长度 ${value.length} 小于 minLength ${schema.minLength}`);
    }
    if (schema.maxLength !== undefined && value.length > schema.maxLength) {
      errors.push(`${path}: 长度 ${value.length} 大于 maxLength ${schema.maxLength}`);
    }
    if (schema.pattern !== undefined) {
      try {
        const re = new RegExp(schema.pattern);
        if (!re.test(value)) errors.push(`${path}: 不匹配正则 ${schema.pattern}`);
      } catch {
        errors.push(`${path}: schema 中的 pattern 非法,跳过`);
      }
    }
  }

  return errors;
}

function matchesType(value: unknown, type: string): boolean {
  switch (type) {
    case 'string':
      return typeof value === 'string';
    case 'number':
      return typeof value === 'number';
    case 'integer':
      return typeof value === 'number' && Number.isInteger(value);
    case 'boolean':
      return typeof value === 'boolean';
    case 'object':
      return typeof value === 'object' && value !== null && !Array.isArray(value);
    case 'array':
      return Array.isArray(value);
    case 'null':
      return value === null;
    default:
      return true; // 未知类型放行,保持宽松
  }
}

function describeType(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const keysA = Object.keys(a as object);
    const keysB = Object.keys(b as object);
    if (keysA.length !== keysB.length) return false;
    return keysA.every((k) => deepEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
  }
  return false;
}
