/**
 * calc.ts —— 安全的数学表达式求值器
 *
 * 为什么不直接 eval / new Function?
 *   模型可能生成任意字符串作为表达式参数,直接执行等于把模型变成 RCE 通道
 *   (prompt injection 的首选目标)。这里用递归下降解析器,
 *   只接受数字、`+ - * / %`、括号与空白,从根本上杜绝任意代码执行。
 * 这也是「工具是最小的攻击面」原则的体现:宁可多写 60 行,也要把
 * 不可信输入挡在边界外。
 */

export interface CalcResult {
  ok: boolean;
  value?: number;
  error?: string;
}

export function safeEvaluate(input: string): CalcResult {
  const src = input.replace(/\s+/g, '');
  if (!src) return { ok: false, error: '空表达式' };

  let pos = 0;

  function peek(): string {
    return src[pos] ?? '';
  }

  function error(msg: string): never {
    throw new Error(`${msg}(位置 ${pos})`);
  }

  // expr := term (('+' | '-') term)*
  function expr(): number {
    let left = term();
    for (;;) {
      const c = peek();
      if (c === '+') {
        pos += 1;
        left += term();
      } else if (c === '-') {
        pos += 1;
        left -= term();
      } else {
        return left;
      }
    }
  }

  // term := factor (('*' | '/' | '%') factor)*
  function term(): number {
    let left = factor();
    for (;;) {
      const c = peek();
      if (c === '*') {
        pos += 1;
        left *= factor();
      } else if (c === '/') {
        pos += 1;
        const right = factor();
        if (right === 0) error('除以零');
        left /= right;
      } else if (c === '%') {
        pos += 1;
        const right = factor();
        if (right === 0) error('模零');
        left %= right;
      } else {
        return left;
      }
    }
  }

  // factor := '-' factor | '(' expr ')' | number
  function factor(): number {
    const c = peek();
    if (c === '-') {
      pos += 1;
      return -factor();
    }
    if (c === '(') {
      pos += 1;
      const v = expr();
      if (peek() !== ')') error('缺少右括号');
      pos += 1;
      return v;
    }
    return number();
  }

  // number := [0-9]+ ('.' [0-9]+)?
  function number(): number {
    const start = pos;
    while (/[0-9]/.test(peek())) pos += 1;
    if (peek() === '.') {
      pos += 1;
      while (/[0-9]/.test(peek())) pos += 1;
    }
    if (start === pos) error(`非法的字符 '${peek() || '结尾'}'`);
    return Number(src.slice(start, pos));
  }

  try {
    const value = expr();
    if (pos < src.length) {
      return { ok: false, error: `非法字符 '${src[pos]}'(位置 ${pos})` };
    }
    if (!Number.isFinite(value)) {
      return { ok: false, error: '结果不是有限数值' };
    }
    return { ok: true, value };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}
