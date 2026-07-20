# 第十三章 TUI 应用测试

> **本章衔接：** 上一章学习了 MCP 的高级特性和生产实践。本章聚焦 TUI 应用特有的测试策略，涵盖单元测试、集成测试、快照测试和 CI/CD 集成。测试是生产级 TUI 应用的关键环节。

## 13.1 TUI 测试的挑战

TUI 应用测试具有特殊性——它涉及终端交互、ANSI 序列和异步事件流。

| 挑战 | 说明 | 解决方案 |
|------|------|---------|
| **终端依赖** | 测试依赖于终端模拟器特性 | 使用虚拟终端进行隔离测试 |
| **异步事件流** | LLM 流式输出需要时间维度验证 | 使用 FakeTimers 控制时间 |
| **ANSI 序列** | 渲染结果含大量控制字符 | 剥离 ANSI 后校验内容 |
| **组件树** | blessed 组件树难以直接断言 | 对逻辑层做单元测试 |
| **用户交互** | 键盘、鼠标事件的模拟 | 事件驱动测试 |

### 测试策略金字塔

```
        /\
       /  \         E2E 测试（少）—— 完整用户流程
      /    \
     /──────\       集成测试（中）—— 组件协作
    /        \
   /──────────\  单元测试（多）—— 逻辑层
  /            \
 /──────────────\ 静态分析 —— 类型检查
```

**TUI 测试策略建议：**
- **底层逻辑**（状态机、Markdown 渲染、Mock LLM）：100% 单元测试覆盖
- **组件渲染**（消息卡片、工具卡片）：快照测试
- **用户流程**（发送消息、取消生成）：集成测试
- **终端兼容**：多终端模拟器的 E2E 测试

## 13.2 单元测试

将纯逻辑与 UI 渲染分离，对逻辑层进行标准的单元测试。

### 13.2.1 状态机测试

```typescript
import { describe, it, expect } from 'vitest';

class StateMachine {
  private currentState: AppState = 'idle';

  private static readonly TRANSITIONS: Record<AppState, AppState[]> = {
    idle:        ['thinking'],
    thinking:    ['streaming', 'tool_call', 'error'],
    streaming:   ['tool_call', 'tool_result', 'error', 'done'],
    tool_call:   ['tool_result', 'error'],
    tool_result: ['streaming', 'error'],
    done:        ['idle'],
    error:       ['idle'],
  };

  transition(to: AppState): boolean {
    const allowed = StateMachine.TRANSITIONS[this.currentState];
    if (!allowed?.includes(to)) return false;
    this.currentState = to;
    return true;
  }

  canTransition(to: AppState): boolean {
    return StateMachine.TRANSITIONS[this.currentState]?.includes(to) ?? false;
  }

  get state(): AppState { return this.currentState; }
  reset(): void { this.currentState = 'idle'; }
}

describe('StateMachine', () => {
  it('should allow valid state transitions', () => {
    const sm = new StateMachine();
    expect(sm.transition('thinking')).toBe(true);
    expect(sm.state).toBe('thinking');
  });

  it('should reject invalid state transitions', () => {
    const sm = new StateMachine();
    expect(sm.transition('error')).toBe(false);
    expect(sm.state).toBe('idle');
  });

  it('should track a complete weather query flow', () => {
    const sm = new StateMachine();
    const flow = ['thinking', 'streaming', 'tool_call', 'tool_result', 'streaming', 'done', 'idle'];
    for (const state of flow) {
      expect(sm.transition(state as AppState)).toBe(true);
    }
    expect(sm.state).toBe('idle');
  });

  it('should check transitions without changing state', () => {
    const sm = new StateMachine();
    expect(sm.canTransition('thinking')).toBe(true);
    expect(sm.canTransition('error')).toBe(false);
    expect(sm.state).toBe('idle');
  });

  it('should track error flow correctly', () => {
    const sm = new StateMachine();
    sm.transition('thinking');
    sm.transition('streaming');
    sm.transition('error');
    expect(sm.state).toBe('error');
    sm.transition('idle');
    expect(sm.state).toBe('idle');
  });
});
```

### 13.2.2 Markdown 渲染器测试

```typescript
describe('MarkdownRenderer', () => {
  it('should render heading with cyan-bg', () => {
    const result = renderMarkdown('# Hello');
    expect(result).toContain('cyan-bg');
    expect(result).toContain('Hello');
  });

  it('should render code block with black-bg', () => {
    const result = renderMarkdown('```ts\nconst x = 1;\n```');
    expect(result).toContain('black-bg');
    expect(result).toContain('const x = 1;');
  });

  it('should escape curly braces in JSON', () => {
    const result = renderMarkdown('{"key": "value"}');
    expect(result).not.toContain('{key}');
  });

  it('should render ordered list', () => {
    const result = renderMarkdown('1. First\n2. Second');
    expect(result).toContain('1.');
    expect(result).toContain('2.');
  });

  it('should render blockquote', () => {
    const result = renderMarkdown('> A quote');
    expect(result).toContain('yellow-fg');
    expect(result).toContain('A quote');
  });

  it('should handle empty input gracefully', () => {
    expect(renderMarkdown('').trim()).toBe('');
  });

  it('should handle nested bold+italic', () => {
    const result = renderMarkdown('***bold italic***');
    expect(result).toContain('bold');
    expect(result).toContain('italic');
  });
});
```

### 13.2.3 Mock LLM 流测试

```typescript
describe('mockLLMStream', () => {
  it('should emit events in correct order', async () => {
    const events: string[] = [];
    for await (const event of mockLLMStream('hello')) {
      events.push(event.type);
    }
    expect(events[0]).toBe('thinking');
    expect(events[events.length - 1]).toBe('done');
  });

  it('weather query should include tool_call and tool_result', async () => {
    const events: string[] = [];
    for await (const event of mockLLMStream('北京天气')) {
      events.push(event.type);
    }
    expect(events).toContain('tool_call');
    expect(events).toContain('tool_result');
  });

  it('error scenario should emit error event', async () => {
    const events: string[] = [];
    for await (const event of mockLLMStream('制造错误')) {
      events.push(event.type);
    }
    expect(events).toContain('error');
  });

  it('should handle empty input without crashing', async () => {
    const events: LLMEvent[] = [];
    for await (const event of mockLLMStream('')) {
      events.push(event);
    }
    expect(events.length).toBeGreaterThanOrEqual(2);
  });

  it('code generation should produce text content', async () => {
    const events: LLMEvent[] = [];
    for await (const event of mockLLMStream('写代码')) {
      events.push(event);
    }
    const textEvents = events.filter(e => e.type === 'text') as Array<{ type: 'text'; content: string }>;
    const fullContent = textEvents.map(e => e.content).join('');
    expect(fullContent).toContain('TypeScript');
  });
});
```

### 13.2.4 输入验证测试

```typescript
describe('InputValidator', () => {
  it('should reject empty input', () => {
    const result = InputValidator.validate('');
    expect(result.valid).toBe(false);
    expect(result.errors).toContain('输入不能为空');
  });

  it('should reject overly long input', () => {
    const longInput = 'a'.repeat(2001);
    const result = InputValidator.validate(longInput);
    expect(result.valid).toBe(false);
  });

  it('should sanitize ANSI sequences', () => {
    const result = InputValidator.validate('hello\x1b[31mworld');
    expect(result.valid).toBe(true);
    expect(result.warnings.length).toBeGreaterThan(0);
  });

  it('should pass normal input', () => {
    const result = InputValidator.validate('北京今天天气怎么样？');
    expect(result.valid).toBe(true);
    expect(result.errors.length).toBe(0);
  });
});
```

## 13.3 异步流测试

AsyncGenerator 是 TUI 中处理 LLM 流式输出的核心机制。

```typescript
describe('AsyncGenerator 流测试', () => {
  it('should support cancellation via AbortController', async () => {
    const ac = new AbortController();
    const received: string[] = [];
    setTimeout(() => ac.abort(), 30);

    try {
      for await (const event of mockLLMStream('long message', ac.signal)) {
        received.push(event.type);
        await new Promise(r => setTimeout(r, 20));
      }
    } catch (err) {
      expect((err as Error).name).toBe('AbortError');
    }
    expect(received.length).toBeGreaterThan(0);
    expect(received.length).toBeLessThan(10);
  });

  it('tool call events should contain complete parameters', async () => {
    const events: LLMEvent[] = [];
    for await (const event of mockLLMStream('北京天气')) {
      events.push(event);
    }
    const toolCall = events.find(e => e.type === 'tool_call');
    expect(toolCall).toBeDefined();
    if (toolCall && toolCall.type === 'tool_call') {
      expect(toolCall.name).toBe('get_weather');
      expect(toolCall.args).toHaveProperty('city');
    }
  });
});
```

## 13.4 快照测试

快照测试通过捕获 TUI 渲染输出，与预先存储的基准快照对比，检测非预期变化。

### 13.4.1 虚拟终端

```typescript
import { EventEmitter } from 'events';

/**
 * 虚拟终端 —— 用于集成测试
 * 模拟 blessed 屏幕，避免真正的终端依赖
 */
class VirtualTerminal {
  public screen: Widgets.Screen;
  private output = '';

  constructor(width: number = 80, height: number = 24) {
    this.screen = blessed.screen({
      smartCSR: true,
      terminal: 'xterm-256color',
      input: new EventEmitter() as any,
      output: {
        write: (data: string) => { this.output += data; },
        columns: width, rows: height,
        on: () => {},
      } as any,
      log: '/dev/null',
    });
  }

  /** 模拟按键 */
  pressKey(key: string, ctrl: boolean = false): void {
    this.screen.emit('keypress', null, {
      name: key, ctrl, meta: false, shift: false,
      full: ctrl ? `C-${key}` : key,
    } as any);
  }

  /** 获取屏幕输出（剥离 ANSI 序列） */
  getOutput(): string {
    return this.output.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '');
  }

  /** 获取原始输出（含 ANSI 序列） */
  getRawOutput(): string { return this.output; }
}
```

### 13.4.2 快照测试示例

```typescript
describe('ChatUI 快照测试', () => {
  it('initial layout should match snapshot for 80x24', () => {
    const ui = new ChatTUI();
    const output = captureScreen(ui.screen);
    expect(output).toMatchSnapshot('initial-layout-80x24');
  });

  it('tool call card should display correctly', () => {
    const ui = new ChatTUI();
    ui.showToolCall('get_weather', { city: 'Beijing' });
    const output = captureScreen(ui.screen);
    expect(output).toContain('🔧');
    expect(output).toContain('get_weather');
    expect(output).toMatchSnapshot('tool-call-card');
  });

  it('error card should display correctly', () => {
    const ui = new ChatTUI();
    ui.showError('服务暂不可用');
    const output = captureScreen(ui.screen);
    expect(output).toContain('❌');
    expect(output).toContain('服务暂不可用');
    expect(output).toMatchSnapshot('error-card');
  });

  it('AI response should include correct role identifier', () => {
    const ui = new ChatTUI();
    ui.addMessage({ role: 'user', content: 'Hello' });
    ui.addMessage({ role: 'assistant', content: 'Hi there!' });
    const output = captureScreen(ui.screen);
    expect(output).toContain('🤖');
    expect(output).toMatchSnapshot('ai-response');
  });
});
```

### 13.4.3 快照管理建议

- 每次运行 `vitest --update` 自动更新快照
- 代码审查时务必检查快照变更
- 为不同终端尺寸（80x24、120x40）分别维护快照
- 将 `__snapshots__` 目录纳入版本控制

## 13.5 集成测试

```typescript
describe('ChatTUI 集成测试', () => {
  it('sending message should transition state to thinking', () => {
    const ui = new ChatTUI();
    ui.sendMessage('tell me a joke');
    expect(ui.state).toBe('thinking');
  });

  it('status bar should show error on error', () => {
    const ui = new ChatTUI();
    ui.showError('Network error');
    const statusContent = ui.statusBar.getContent();
    expect(statusContent).toContain('错误');
  });

  it('input box should be cleared after send', () => {
    const ui = new ChatTUI();
    ui.inputBox.setValue('test message');
    ui.handleUserInput();
    expect(ui.inputBox.getValue()).toBe('');
  });

  it('error retry flow should work', async () => {
    const ui = new ChatTUI();
    ui.sendMessage('make error');
    await new Promise(r => setTimeout(r, 200));
    expect(ui.state).toBe('error');
    // Press R to retry
    ui.screen.emit('keypress', null, { name: 'r', ctrl: false } as any);
    expect(ui.lastMessage).toBe('make error');
  });
});
```

## 13.6 Spinner 时序测试

```typescript
describe('Spinner 时序测试', () => {
  it('spinner should cycle frames at correct interval', () => {
    vi.useFakeTimers();
    const spinner = new Spinner();
    const frames: string[] = [];

    spinner.start();
    for (let i = 0; i < 4; i++) {
      frames.push(spinner.currentFrame);
      vi.advanceTimersByTime(80);
    }
    spinner.stop();

    expect(frames).toEqual(['⠋', '⠙', '⠹', '⠸']);
    vi.useRealTimers();
  });

  it('spinner should stop updating after stop() is called', () => {
    vi.useFakeTimers();
    const spinner = new Spinner();
    spinner.start();
    spinner.stop();
    const frameBefore = spinner.currentFrame;
    vi.advanceTimersByTime(1000);
    expect(spinner.currentFrame).toBe(frameBefore);
    vi.useRealTimers();
  });
});
```

## 13.7 测试覆盖率

### 覆盖率目标

| 层级 | 目标覆盖率 | 重点覆盖内容 |
|------|-----------|-------------|
| 状态机逻辑 | 100% | 所有状态转换路径 |
| Markdown 渲染 | 100% | 所有标记类型 |
| 事件处理 | >90% | 所有事件类型的分发 |
| 错误处理 | 100% | 所有异常分支 |
| 集成测试 | >80% | 主要用户操作流 |

### vitest 配置

```typescript
// vitest.config.ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov', 'html'],
      include: ['src/**/*.ts'],
      exclude: ['src/**/*.test.ts', 'src/examples/**'],
      thresholds: {
        branches: 80,
        functions: 85,
        lines: 85,
        statements: 85,
      },
    },
  },
});
```

## 13.8 CI/CD 集成

### GitHub Actions

```yaml
# .github/workflows/tui-ci.yml
name: TUI CI Pipeline

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest, macos-latest]
        node-version: [18, 20, 22]

    runs-on: ${{ matrix.os }}

    steps:
      - uses: actions/checkout@v4
      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node-version }}
          cache: 'npm'
      - name: Install dependencies
        run: npm ci
      - name: Type check
        run: npx tsc --noEmit
      - name: Run unit & integration tests
        run: npx vitest --coverage
      - name: Run snapshot tests
        run: npx vitest --run src/__tests__/snapshots/
      - name: Upload coverage report
        uses: codecov/codecov-action@v4
        with:
          files: ./coverage/lcov.info
          flags: ${{ matrix.os }}
```

### 本地运行测试

```bash
# 完整测试套件
npx vitest --run --coverage

# 仅快照测试
npx vitest --run src/__tests__/snapshots/

# 更新快照
npx vitest --update

# 监视模式（开发时使用）
npx vitest
```

## 13.9 测试策略总结

```
基础功能测试:
□ 状态机转换是否覆盖所有路径？
□ 所有 AsyncGenerator 分支是否都测试到？
□ 空输入是否被正确处理？
□ 输入过长是否被截断？

UI 渲染测试:
□ 每种角色消息是否正确渲染？
□ Spinner 是否在 thinking 状态启动？
□ 工具卡片的三种状态是否正确展示？
□ 错误卡片是否在所有错误场景展示？

边界情况测试:
□ 连续快速发送消息不会导致 race condition？
□ 窗口 resize 后布局是否正确？
□ 终端不支持 TrueColor 时的降级？
□ 超长 AI 回答是否截断？
□ 超过 100 条历史消息不会泄漏？

错误恢复测试:
□ 网络超时是否显示重试按钮？
□ 用户取消生成是否干净退出？
□ 进程崩溃后终端是否完全恢复？
□ ANSI 注入是否被正确过滤？
```

### 测试设计原则

| 原则 | 说明 |
|------|------|
| **隔离逻辑层** | 状态机、渲染器、验证器可独立于 blessed 进行单元测试 |
| **Mock 终端** | 使用 VirtualTerminal 模拟终端环境 |
| **控制时间** | 使用 FakeTimers 控制动画和流式输出的时序 |
| **快照+断言** | 快照检测整体变化，具体断言验证特定内容 |
| **CI 集成** | 测试在 CI 中自动运行，覆盖多平台 |

---

**实践：** 为 `llm-chat.ts` 中的 `InputHistory` 类编写完整的单元测试，覆盖边界情况（空历史、历史上限、重复消息）。

**上一章：** [第十二章：高级 MCP 与生产实践](12-mcp-advanced.md)

**下一步：** [第十四章：总结与进阶实践](14-summary.md)
