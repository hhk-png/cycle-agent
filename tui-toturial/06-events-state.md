# 第六章 事件系统与状态管理

## 6.1 TUI 中的事件系统

TUI 应用本质上是**事件驱动的**——它在等待用户操作（按键、鼠标点击）或系统事件（定时器、异步数据到达）并做出响应。

### 6.1.1 事件类型总览

```typescript
type TUIEvent =
  | KeyboardEvent      // 按键、组合键 —— 最核心的交互方式
  | MouseEvent         // 点击、滚轮、悬停、拖拽
  | FocusEvent         // 焦点获得/失去
  | ResizeEvent        // 终端窗口大小变化
  | TimerEvent         // setTimeout / setInterval 定时器
  | AsyncEvent         // 异步数据到达（如 LLM 流式响应）
  | CustomEvent;       // 自定义业务事件
```

### 6.1.2 blessed 事件处理模式

```typescript
// ── 1. 屏幕级键盘事件（全局快捷键） ──
screen.key(['C-q', 'C-c'], () => process.exit(0));
screen.key('tab', () => switchFocus());
screen.key(['up', 'down'], (ch, key) => handleNav(key.name));

// ── 2. 组件级事件 ──
button.on('press', () => handleClick());
list.on('select', (item) => handleSelect(item));
textarea.on('submit', (value) => handleSubmit(value));

// ── 3. 鼠标事件 ──
widget.on('click', (mouseData) => handleClick(mouseData));
widget.on('wheelup', () => scrollUp());
widget.on('wheeldown', () => scrollDown());
screen.on('mouse', (data) => globalMouseHandler(data));

// ── 4. 生命周期事件 ──
screen.on('resize', () => recalcLayout());
screen.on('destroy', () => cleanup());
widget.on('show', () => { /* 组件显示时 */ });
widget.on('hide', () => { /* 组件隐藏时 */ });
```

### 6.1.3 事件传播机制

blessed 的事件传播遵循特定的优先级链：

```
键盘按键按下
　　↓
screen.key(...) 全局快捷键处理（优先级最高）
　　↓ 若未被消费（return false）
screen.keypress 事件
　　↓ 若有焦点组件
已聚焦的 widget.key(...) 组件级处理
　　↓ 若有 textarea/textbox
组件的输入处理（输入文本）
```

**事件消费规则：**

```typescript
// 在全局处理中消费事件并阻止传播
screen.key('tab', () => {
  handleTabSwitch();
  return false;  // ← 阻止事件继续传播到焦点组件
});

// 在组件级处理中消费
textarea.on('keypress', (ch, key) => {
  if (key.name === 'enter') {
    handleSubmit();
    return false;  // ← 阻止 Enter 进入文本
  }
});
```

### 6.1.4 键盘事件详解

blessed 的键盘事件使用简写命名规范：

```typescript
// ── 组合键 ──
'C-a'        // Ctrl+A
'M-a'        // Meta+Alt+A (在 macOS 终端中可能映射到 Esc+A)
'S-a'        // Shift+A
'C-S-a'      // Ctrl+Shift+A

// ── 功能键 ──
'f1' - 'f12' // 功能键 F1-F12

// ── 方向键 ──
'up', 'down', 'left', 'right'

// ── 编辑键 ──
'backspace', 'delete', 'insert'
'home', 'end'
'pageup', 'pagedown'

// ── 特殊键 ──
'tab', 'enter', 'escape', 'space'

// ── 监听所有按键 ──
screen.on('keypress', (ch, key) => {
  // ch: 输入的字符（如果有）
  // key: { name, ctrl, meta, shift, seq }
});
```

## 6.2 状态管理

对于大模型对话 TUI 这样的复杂应用，良好的状态管理至关重要。

### 6.2.1 全局状态对象

```typescript
interface AppState {
  // ── 应用运行时状态 ──
  status: AppStateValue;
  messages: ChatMessage[];

  // ── 当前响应状态 ──
  currentAssistantText: string;
  currentTool: ToolCallDisplay | null;
  currentToolResult: string | null;

  // ── UI 控制 ──
  isInputEnabled: boolean;
  streamingMessageId: string | null;

  // ── 统计信息 ──
  totalTokens: number;
  messageCount: number;
}
```

### 6.2.2 基于类的状态管理

```typescript
class ChatTUI {
  private state: AppState = {
    status: 'idle',
    messages: [],
    currentAssistantText: '',
    currentTool: null,
    currentToolResult: null,
    isInputEnabled: true,
    streamingMessageId: null,
    totalTokens: 0,
    messageCount: 0,
  };

  /**
   * 安全地更新状态，并触发 UI 刷新
   */
  private setState(partial: Partial<AppState>): void {
    Object.assign(this.state, partial);
    this.onStateChanged();
  }

  /**
   * 状态变更后的 UI 同步
   */
  private onStateChanged(): void {
    // 1. 更新状态栏
    this.updateStatusBar();

    // 2. 根据状态控制输入可用性
    if (this.state.isInputEnabled) {
      this.inputBox.show();
    } else {
      this.inputBox.hide();
    }

    // 3. 重新渲染
    this.screen.render();
  }
}
```

### 6.2.3 使用状态机防止非法转换

```typescript
class StateMachine {
  private currentState: AppState = 'idle';
  private onTransition?: (from: AppState, to: AppState) => void;

  /** 合法状态转换表 */
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
    const from = this.currentState;
    const allowed = StateMachine.TRANSITIONS[from];

    if (!allowed?.includes(to)) {
      console.error(`[状态机] 非法转换: ${from} → ${to}`);
      return false;
    }

    console.log(`[状态机] ${from} → ${to}`);
    this.currentState = to;
    this.onTransition?.(from, to);
    return true;
  }

  canTransition(to: AppState): boolean {
    return StateMachine.TRANSITIONS[this.currentState]?.includes(to) ?? false;
  }

  get state(): AppState {
    return this.currentState;
  }

  reset(): void {
    this.currentState = 'idle';
  }
}
```

### 6.2.4 状态变更与 UI 同步流程

```
用户操作（如发送消息）
　　↓
stateMachine.transition('thinking')
　　↓
UI 更新:
├── statusBar: "思考中..."
├── startSpinner()
├── inputBox.hide()  (禁用输入)
└── screen.render()
　　↓
LLM 事件到达
　　↓
stateMachine.transition('streaming')
　　↓
UI 更新:
├── statusBar: "生成中..."
├── stopSpinner()
├── createAssistantMessage()
└── screen.render()
　　↓
... 继续处理 ...
```

## 6.3 异步处理模式

### 6.3.1 AsyncGenerator 模式

LLM 流式响应天然适合用 AsyncGenerator 消费：

```typescript
// ── 生产者：Mock LLM ──
async function* mockLLMStream(prompt: string): AsyncGenerator<LLMEvent> {
  yield { type: 'thinking' };
  await delay(500);

  yield { type: 'text', content: '你好！' };
  await delay(100);

  yield { type: 'tool_call', name: 'get_weather', args: { city: 'Beijing' } };
  await delay(1000);

  yield { type: 'tool_result', name: 'get_weather', result: '{"temp": 25}' };
  yield { type: 'text', content: '北京今天 25°C。' };
  yield { type: 'done' };
}

// ── 消费者：TUI 应用 ──
async function consumeStream(stream: AsyncGenerator<LLMEvent>) {
  for await (const event of stream) {
    switch (event.type) {
      case 'thinking':   showSpinner(); break;
      case 'text':       appendText(event.content); break;
      case 'tool_call':  showToolCall(event); break;
      case 'tool_result':showToolResult(event); break;
      case 'error':      showError(event.message); break;
      case 'done':       finishResponse(); break;
    }
  }
}
```

### 6.3.2 取消令牌（AbortController）模式

支持用户取消正在进行的生成操作：

```typescript
class ChatTUI {
  private abortController: AbortController | null = null;

  /**
   * 发送消息并处理流式响应
   */
  async sendMessage(text: string): Promise<void> {
    // 创建新的取消令牌
    this.abortController = new AbortController();
    const signal = this.abortController.signal;

    try {
      for await (const event of mockLLMStream(text)) {
        // 每次迭代检查是否被取消
        if (signal.aborted) {
          console.log('生成被用户取消');
          break;
        }

        await this.handleLLMEvent(event);
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        this.showSystemMessage('⏹️ 生成已取消');
      } else {
        this.showError(err as Error);
      }
    } finally {
      this.abortController = null;
      this.state = 'idle';
      this.setStatus('● 就绪', '#22aa44');
      this.inputBox.focus();
    }
  }

  /**
   * 取消正在进行的生成
   */
  cancelGeneration(): void {
    if (this.abortController && this.state !== 'idle') {
      this.abortController.abort();
    }
  }
}
```

### 6.3.3 渲染节流（Throttle）

流式更新时，避免不必要的过度渲染：

```typescript
class RenderThrottle {
  private lastRenderTime = 0;
  private readonly MIN_INTERVAL = 33; // ~30fps 上限
  private pending = false;

  constructor(private renderFn: () => void) {}

  request(): void {
    const now = Date.now();
    if (now - this.lastRenderTime >= this.MIN_INTERVAL) {
      // 距离上次渲染已超过最小间隔，立即执行
      this.lastRenderTime = now;
      this.renderFn();
    } else if (!this.pending) {
      // 还没有等待中的渲染请求，调度一个
      this.pending = true;
      const delay = this.MIN_INTERVAL - (now - this.lastRenderTime);
      setTimeout(() => {
        this.lastRenderTime = Date.now();
        this.pending = false;
        this.renderFn();
      }, delay);
    }
    // 如果已经有等待中的请求，跳过（合并更新）
  }
}
```

### 6.3.4 事件批处理模式

对于高频事件（如流式文本），使用批处理合并更新：

```typescript
class BatchProcessor {
  private buffer: string[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly BATCH_INTERVAL = 50; // 50ms 批次

  constructor(private flushFn: (batch: string[]) => void) {}

  add(item: string): void {
    this.buffer.push(item);
    if (!this.timer) {
      this.timer = setTimeout(() => this.flush(), this.BATCH_INTERVAL);
    }
  }

  private flush(): void {
    if (this.buffer.length > 0) {
      const batch = [...this.buffer];
      this.buffer = [];
      this.flushFn(batch);
    }
    this.timer = null;
  }

  destroy(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.buffer.length > 0) {
      this.flush(this.buffer);
      this.buffer = [];
    }
  }
}

// 使用
const batchProcessor = new BatchProcessor((batch) => {
  const text = batch.join('');
  updateAssistantMessage(text);
});
```

### 6.3.5 EventBus — 全局事件总线模式

对于大型 TUI 应用，使用事件总线解耦组件：

```typescript
type BusEvent =
  | { type: 'user:send'; text: string }
  | { type: 'llm:thinking' }
  | { type: 'llm:text'; content: string }
  | { type: 'llm:tool_call'; name: string; args: Record<string, unknown> }
  | { type: 'llm:tool_result'; name: string; result: string }
  | { type: 'llm:error'; message: string }
  | { type: 'llm:done' }
  | { type: 'ui:resize'; cols: number; rows: number }
  | { type: 'ui:focus'; component: string }
  | { type: 'app:exit' };

class EventBus {
  private listeners = new Map<string, Array<(event: any) => void>>();

  on<T extends BusEvent>(type: T['type'], handler: (event: T) => void): void {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, []);
    }
    this.listeners.get(type)!.push(handler as any);
  }

  emit<T extends BusEvent>(event: T): void {
    const handlers = this.listeners.get(event.type);
    handlers?.forEach(h => h(event));
  }

  off(type: string, handler: Function): void {
    const handlers = this.listeners.get(type);
    if (handlers) {
      const idx = handlers.indexOf(handler as any);
      if (idx >= 0) handlers.splice(idx, 1);
    }
  }

  clear(): void {
    this.listeners.clear();
  }
}

// 全局单例
const bus = new EventBus();

// ── 使用 ──
// 状态栏组件订阅事件
bus.on('llm:thinking', () => statusBar.setContent('思考中...'));
bus.on('llm:text', (e) => appendText(e.content));

// 发送组件发布事件
bus.emit({ type: 'user:send', text: inputText });
```

## 6.4 实际案例：llm-chat.ts 中的事件流转

以下是 `examples/llm-chat.ts` 中的完整事件流转逻辑：

```typescript
private async processStream(userMsg: string): Promise<void> {
  let fullResponse = '';

  for await (const event of mockLLMStream(userMsg)) {
    switch (event.type) {
      case 'thinking':
        this.state = 'thinking';
        this.setStatus('🤔 思考中...', '#aa8800');
        this.startSpinner();
        break;

      case 'text':
        if (this.state === 'thinking' || this.state === 'tool_result') {
          this.stopSpinner();
          this.clearToolDisplay();
        }
        this.state = 'streaming';
        this.setStatus('💬 生成中...', '#44aa44');
        fullResponse += event.content;
        this.updateAssistantMessage(fullResponse);
        break;

      case 'tool_call':
        this.state = 'tool_call';
        this.setStatus(`🔧 调用工具: ${event.name}`, '#8844aa');
        this.stopSpinner();
        this.clearToolDisplay();
        this.showToolCall(event.name, event.args);
        break;

      case 'tool_result':
        this.state = 'tool_result';
        this.setStatus(`✅ 工具返回: ${event.name}`, '#4488aa');
        this.showToolResult(event.name, event.result);
        break;

      case 'error':
        this.state = 'error';
        this.setStatus('❌ 错误', '#cc2222');
        this.stopSpinner();
        this.clearToolDisplay();
        this.showError(event.message);
        break;

      case 'done':
        this.state = 'idle';
        this.setStatus('● 就绪', '#22aa44');
        this.stopSpinner();
        if (fullResponse) {
          this.messages.push({ role: 'assistant', content: fullResponse });
        }
        break;
    }
  }
}
```

### 6.4.1 终端安全退出装置

`llm-chat.ts` 最核心的基础设施是全局退出守卫。无论程序如何退出（正常、异常、信号），都能恢复终端到原始状态。这是 **TUI 程序最重要的安全措施**：

```typescript
// examples/llm-chat.ts
function resetTerminal(): void {
  try {
    process.stdout.write('\x1b[2J\x1b[H');   // 清屏
    process.stdout.write('\x1b[?25h');        // 恢复光标
    process.stdout.write('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l'); // 禁用鼠标
    process.stdout.write('\x1b[0m');          // 重置样式
    if (process.stdin.isTTY) {
      process.stdin.setRawMode(false);
    }
    process.stdin.pause();
  } catch { /* 清理出错也不抛出 */ }
}

/** 安装全局退出守卫 —— 拦截所有退出路径 */
function installExitGuard(): void {
  process.on('exit', () => resetTerminal());
  process.on('SIGINT', () => { resetTerminal(); process.exit(0); });
  process.on('SIGTERM', () => { resetTerminal(); process.exit(0); });
  process.on('uncaughtException', (err) => {
    resetTerminal();
    console.error('\n❌ 未捕获异常:', err);
    process.exit(1);
  });
  process.on('unhandledRejection', (reason) => {
    resetTerminal();
    console.error('\n❌ 未处理的 Promise 拒绝:', reason);
    process.exit(1);
  });
}
```

### 6.4.2 消息历史上限保护

防止长时间对话导致的内存泄漏：

```typescript
private static readonly MAX_HISTORY = 100;

// 消息历史上限保护：超出时移除最早的 10 条
if (this.messages.length >= ChatTUI.MAX_HISTORY) {
  const removed = this.messages.splice(0, 10);
  removed.forEach(m => {
    this.removeMessageElement(m);
  });
}

// 按内容匹配移除 UI 元素（用于历史清理）
private removeMessageElement(msg: ChatMessage): void {
  const toRemove: Widgets.BoxElement[] = [];
  this.chatBox.children.forEach((child) => {
    const el = child as Widgets.BoxElement;
    const snippet = msg.content.slice(0, 20);
    if (snippet && el.getContent().includes(snippet)) {
      toRemove.push(el);
    }
  });
  toRemove.forEach((el) => {
    try { this.chatBox.remove(el); } catch { /* 已移除 */ }
  });
}
```

### 6.4.3 输入验证与防误触

```typescript
// 输入长度限制
if (text.length > 2000) {
  this.addSystemMessage(
    `{yellow-fg}⚠️ 消息过长 (${text.length} 字符)，最大支持 2000 字符{/yellow-fg}`
  );
  return;
}

// 空输入提示：输入框边框闪烁
private flashInputBorder(): void {
  const originalFg = this.inputBox.style.border?.fg;
  this.inputBox.style.border = { fg: 'red' as any };
  this.screen.render();
  setTimeout(() => {
    this.inputBox.style.border = { fg: (originalFg || '#44aa44') as any };
    this.screen.render();
  }, 300);
}
```

### 6.4.4 完整的错误处理与恢复路径

```typescript
// 用户取消 vs 系统错误的区别处理
try {
  await this.processStream(text, signal);
} catch (err: unknown) {
  if (err instanceof DOMException && err.name === 'AbortError') {
    this.addSystemMessage('{yellow-fg}⏹️ 生成已取消{/yellow-fg}');
  } else {
    const msg = err instanceof Error ? err.message : '未知错误';
    this.showError(msg);
  }
} finally {
  this.abortController = null;
  this.state = 'idle';
  this.setStatus('● 就绪', '#22aa44');
  this.inputBox.focus();
}

// 错误状态下的重试按钮
private addRetryButton(originalMessage: string): void {
  const retryBtn = blessed.button({
    parent: this.chatBox,
    top: 0, left: 2,
    width: 16, height: 1,
    content: ' 🔄 重试 ',
    style: { fg: 'white', bg: '#664422', focus: { bg: '#aa8844' }, hover: { bg: '#886633' } },
    mouse: true,
  });

  retryBtn.on('press', () => {
    this.removeAllByType('error');
    this.screen.render();
    this.sendMessage(originalMessage);
  });

  this.screen.key('r', () => {
    if (this.state === 'error' && this.lastMessage) {
      this.removeAllByType('error');
      this.state = 'idle';
      this.screen.render();
      this.sendMessage(this.lastMessage);
    }
  });
}
```

### 6.4.5 实际应用中的状态流转追踪

在实际运行时，`llm-chat.ts` 的完整状态流转如下（以天气查询为例）：

```
用户输入: "北京天气"
    │
    ├── state = idle → thinking       ← 状态机转换
    ├── 状态栏: "🤔 思考中..."         ← 状态栏更新
    ├── Spinner 开始旋转               ← 动画启动
    │
    ├── event: thinking               ← LLM 事件
    ├── event: text("让我查询...")      ← 流式文本
    │
    ├── state = thinking → streaming  ← 状态转换
    ├── 停止 Spinner                   ← 动画停止
    ├── 创建 AI 消息元素               ← UI 创建
    │
    ├── event: tool_call              ← 工具调用事件
    ├── state = streaming → tool_call ← 状态转换
    ├── 显示工具调用卡片               ← 特殊 UI
    │
    ├── event: tool_result            ← 工具返回事件
    ├── state = tool_call → tool_result
    ├── 替换为工具结果卡片             ← UI 更新
    │
    ├── event: text("🌤️ ...")         ← 继续流式
    ├── state = tool_result → streaming
    ├── 清除工具卡片，追加文本         ← UI 切换
    │
    ├── event: done
    ├── state = streaming → idle
    ├── 保存消息到 history
    ├── 状态栏: "● 就绪"
    └── 输入框重新获得焦点
```

## 6.5 设计原则总结

| 原则 | 说明 | 反例 | 正确做法 |
|------|------|------|---------|
| **单一数据源** | 所有状态集中管理，避免分散 | 在多个方法中各自维护状态 | 一个 `AppState` 对象 |
| **状态可见** | 状态栏清晰显示当前状态 | 用户不知道 AI 是否在思考 | 实时更新状态栏文本和颜色 |
| **防止非法转换** | 状态机约束流转路径 | 在 idle 时显示工具结果 | 使用 `canTransition()` 检查 |
| **可中断** | 用户可以随时取消操作 | 用户无法打断过长的生成 | 使用 `AbortController` |
| **优雅降级** | 任何异步操作都要能处理错误 | 网络错误导致整个程序崩溃 | 每层都有 `try/catch` |
| **即时反馈** | 用户操作后有即时 UI 响应 | 点击发送后界面无反应 | 立即显示 Spinner |
| **一致性** | 相似操作触发相似 UI 变化 | 工具调用有时显示卡片有时不显示 | 严格按照状态机流程 |
| **渲染节流** | 高频更新时限制渲染频率 | 每 10ms 渲染一次导致 CPU 飙升 | 使用 throttle 或批处理 |

## 6.6 响应式状态管理（Reactive State）

对于复杂的 TUI 应用，可以使用 Observable 模式实现响应式状态更新：

```typescript
type StateChangeHandler<T> = (prev: T, next: T) => void;

class ObservableState<T extends Record<string, unknown>> {
  private state: T;
  private handlers = new Map<keyof T, StateChangeHandler<any>[]>();
  private globalHandlers: Array<(prev: T, next: T) > = [];

  constructor(initial: T) {
    this.state = { ...initial };
  }

  /** 获取当前状态快照 */
  get(): Readonly<T> {
    return Object.freeze({ ...this.state });
  }

  /** 更新状态并触发通知 */
  set<K extends keyof T>(key: K, value: T[K]): void {
    const prev = { ...this.state };
    this.state[key] = value;
    // 触发特定 key 的监听器
    this.handlers.get(key)?.forEach(h => h(prev[key], value));
    // 触发全局监听器
    this.globalHandlers.forEach(h => h(prev, this.state));
  }

  /** 批量更新 */
  patch(partial: Partial<T>): void {
    const prev = { ...this.state };
    Object.assign(this.state, partial);
    for (const key of Object.keys(partial) as Array<keyof T>) {
      this.handlers.get(key)?.forEach(h => h(prev[key], this.state[key]));
    }
    this.globalHandlers.forEach(h => h(prev, this.state));
  }

  /** 订阅特定字段变化 */
  on<K extends keyof T>(key: K, handler: StateChangeHandler<T[K]>): () => void {
    if (!this.handlers.has(key)) this.handlers.set(key, []);
    this.handlers.get(key)!.push(handler);
    return () => {
      const arr = this.handlers.get(key);
      if (arr) {
        const idx = arr.indexOf(handler);
        if (idx >= 0) arr.splice(idx, 1);
      }
    };
  }

  /** 订阅所有变化 */
  watch(handler: (prev: T, next: T) => void): () => void {
    this.globalHandlers.push(handler);
    return () => {
      const idx = this.globalHandlers.indexOf(handler);
      if (idx >= 0) this.globalHandlers.splice(idx, 1);
    };
  }
}

// 使用示例
const appState = new ObservableState({
  status: 'idle' as AppState,
  messages: [] as ChatMessage[],
  isInputEnabled: true,
});

// 订阅状态变化 → UI 更新
appState.on('status', (prev, next) => {
  updateStatusBar(next);
  render();
});

appState.on('messages', (prev, next) => {
  scrollToBottom();
});
```

## 6.7 事件溯源模式（Event Sourcing）

对于消息历史管理，事件溯源模式可以提供完整的可回溯性：

```typescript
/** 所有可能发生的消息事件 */
type MessageEvent =
  | { type: 'MESSAGE_SENT'; id: string; content: string; timestamp: number }
  | { type: 'MESSAGE_RECEIVED'; id: string; content: string; timestamp: number }
  | { type: 'TOOL_CALLED'; id: string; name: string; args: unknown; timestamp: number }
  | { type: 'TOOL_RESULT'; id: string; result: string; timestamp: number }
  | { type: 'MESSAGE_EDITED'; id: string; content: string; timestamp: number }
  | { type: 'MESSAGE_DELETED'; id: string; timestamp: number }
  | { type: 'ERROR_OCCURRED'; id: string; message: string; timestamp: number };

/** 事件溯源 —— 所有状态变更通过事件日志重建 */
class MessageEventStore {
  private events: MessageEvent[] = [];
  private listeners: Array<(event: MessageEvent) => void> = [];

  /** 追加事件 */
  append(event: MessageEvent): void {
    this.events.push(event);
    this.listeners.forEach(h => h(event));
  }

  /** 基于事件日志重建消息列表 */
  rebuild(): ChatMessage[] {
    const messages: ChatMessage[] = [];
    for (const event of this.events) {
      switch (event.type) {
        case 'MESSAGE_SENT':
          messages.push({ role: 'user', content: event.content });
          break;
        case 'MESSAGE_RECEIVED':
          messages.push({ role: 'assistant', content: event.content });
          break;
        // ... 其他事件类型
      }
    }
    return messages;
  }

  /** 订阅新事件 */
  subscribe(handler: (event: MessageEvent) => void): () => void {
    this.listeners.push(handler);
    return () => {
      const idx = this.listeners.indexOf(handler);
      if (idx >= 0) this.listeners.splice(idx, 1);
    };
  }

  /** 导出事件日志 */
  export(): MessageEvent[] {
    return [...this.events];
  }

  /** 从事件日志导入 */
  import(events: MessageEvent[]): void {
    this.events = [...events];
  }
}

// 使用：每次用户发送消息或 AI 回复时追加事件
const eventStore = new MessageEventStore();
eventStore.subscribe(event => {
  if (event.type === 'MESSAGE_SENT' || event.type === 'MESSAGE_RECEIVED') {
    scrollToBottom();
  }
});
```

## 6.8 并发状态处理（Race Condition 防护）

在 LLM TUI 中，用户可能快速连续操作，需要防止竞态条件：

```typescript
class ConcurrencyGuard {
  private operationLock = false;
  private pendingOperations: Array<() => Promise<void>> = [];

  /** 执行操作，如果正在执行则排队 */
  async enqueue(operation: () => Promise<void>): Promise<void> {
    if (this.operationLock) {
      // 如果已有相同操作在排队，丢弃旧的
      if (operation.toString() === this.pendingOperations[0]?.toString()) {
        return;
      }
      this.pendingOperations.push(operation);
      return;
    }

    this.operationLock = true;
    try {
      await operation();
    } finally {
      this.operationLock = false;
      // 处理下一个排队操作
      const next = this.pendingOperations.shift();
      if (next) await this.enqueue(next);
    }
  }

  /** 清空排队 */
  clear(): void {
    this.pendingOperations = [];
  }
}

// 使用 —— 防止用户在 AI 回复时重复发送
const guard = new ConcurrencyGuard();

screen.key('C-s', () => {
  guard.enqueue(async () => {
    const text = inputBox.getValue().trim();
    if (!text) return;
    await sendMessage(text);
  });
});
```

## 6.9 状态持久化

保存和恢复对话状态，支持会话中断后继续：

```typescript
interface PersistedState {
  messages: ChatMessage[];
  config: {
    model: string;
    maxTokens: number;
  };
  lastTimestamp: number;
  eventLog: MessageEvent[];
}

class StatePersistence {
  private static readonly SAVE_INTERVAL = 30000; // 30 秒自动保存

  constructor(
    private state: ObservableState<any>,
    private storagePath: string,
  ) {}

  /** 启动自动保存 */
  startAutoSave(): void {
    setInterval(() => this.save(), StatePersistence.SAVE_INTERVAL);
  }

  /** 手动保存 */
  async save(): Promise<void> {
    const snapshot: PersistedState = {
      messages: this.state.get().messages,
      config: { model: 'mock', maxTokens: 4096 },
      lastTimestamp: Date.now(),
      eventLog: [], // 从 eventStore 导出
    };
    await fs.promises.writeFile(
      this.storagePath,
      JSON.stringify(snapshot, null, 2),
    );
  }

  /** 加载保存的状态 */
  async load(): Promise<boolean> {
    try {
      const data = await fs.promises.readFile(this.storagePath, 'utf-8');
      const snapshot: PersistedState = JSON.parse(data);
      this.state.patch({
        messages: snapshot.messages,
      });
      return true;
    } catch {
      return false; // 首次运行或无保存文件
    }
  }
}
```

## 6.10 撤销/重做（Undo/Redo）

对话 TUI 中，用户可能误操作或想回溯到之前的对话状态。提供 Undo/Redo 支持需要管理状态快照。

### 状态快照模式

```typescript
interface StateSnapshot {
  messages: ChatMessage[];
  timestamp: number;
  description: string;  // 人类可读的操作描述
}

class UndoRedoManager {
  private undoStack: StateSnapshot[] = [];
  private redoStack: StateSnapshot[] = [];
  private readonly MAX_HISTORY = 50;

  /** 保存当前状态快照 */
  pushSnapshot(
    messages: ChatMessage[],
    description: string,
  ): void {
    // 深拷贝当前消息列表
    const snapshot: StateSnapshot = {
      messages: JSON.parse(JSON.stringify(messages)),
      timestamp: Date.now(),
      description,
    };

    this.undoStack.push(snapshot);
    // 新操作清空重做栈
    this.redoStack = [];

    // 限制历史深度
    if (this.undoStack.length > this.MAX_HISTORY) {
      this.undoStack.shift();
    }
  }

  /** 撤销：恢复到上一个状态 */
  undo(currentMessages: ChatMessage[]): ChatMessage[] | null {
    const snapshot = this.undoStack.pop();
    if (!snapshot) return null;

    // 保存当前状态到重做栈
    this.redoStack.push({
      messages: JSON.parse(JSON.stringify(currentMessages)),
      timestamp: Date.now(),
      description: '撤销',
    });

    return snapshot.messages;
  }

  /** 重做：恢复到撤销前的状态 */
  redo(currentMessages: ChatMessage[]): ChatMessage[] | null {
    const snapshot = this.redoStack.pop();
    if (!snapshot) return null;

    // 保存当前状态到撤销栈
    this.undoStack.push({
      messages: JSON.parse(JSON.stringify(currentMessages)),
      timestamp: Date.now(),
      description: '重做',
    });

    return snapshot.messages;
  }

  /** 是否可以撤销 */
  get canUndo(): boolean {
    return this.undoStack.length > 0;
  }

  /** 是否可以重做 */
  get canRedo(): boolean {
    return this.redoStack.length > 0;
  }

  /** 获取撤销历史描述列表（用于 UI 展示） */
  getUndoHistory(): string[] {
    return [...this.undoStack].reverse().map(s => s.description);
  }

  /** 清空历史 */
  clear(): void {
    this.undoStack = [];
    this.redoStack = [];
  }
}
```

### 命令模式（更精细的 Undo/Redo）

相比于整个状态的快照，命令模式支持细粒度的撤销/重做：

```typescript
interface Command {
  description: string;
  execute(): void;
  undo(): void;
}

class AddMessageCommand implements Command {
  description = '添加消息';

  constructor(
    private chatBox: Widgets.BoxElement,
    private message: ChatMessage,
    private messages: ChatMessage[],
  ) {}

  execute(): void {
    this.messages.push(this.message);
    // 渲染消息到 UI
  }

  undo(): void {
    const idx = this.messages.indexOf(this.message);
    if (idx >= 0) {
      this.messages.splice(idx, 1);
      // 从 UI 中移除消息元素
    }
  }
}

class CommandHistory {
  private commands: Command[] = [];
  private currentIndex = -1;

  execute(command: Command): void {
    command.execute();
    // 清空当前索引之后的所有命令（新的分支）
    this.commands = this.commands.slice(0, this.currentIndex + 1);
    this.commands.push(command);
    this.currentIndex = this.commands.length - 1;
  }

  undo(): void {
    if (this.currentIndex >= 0) {
      this.commands[this.currentIndex].undo();
      this.currentIndex--;
    }
  }

  redo(): void {
    if (this.currentIndex < this.commands.length - 1) {
      this.currentIndex++;
      this.commands[this.currentIndex].execute();
    }
  }
}
```

### 集成到 TUI

```typescript
class ChatTUIWithUndo extends ChatTUI {
  private undoRedo = new UndoRedoManager();

  protected sendMessage(text: string): void {
    // 发送消息前保存快照
    this.undoRedo.pushSnapshot(this.messages, `发送消息: ${text.slice(0, 20)}`);
    super.sendMessage(text);
  }

  // 绑定快捷键
  protected bindUndoRedoKeys(): void {
    this.screen.key('C-z', () => {
      const restored = this.undoRedo.undo(this.messages);
      if (restored) {
        this.messages = restored;
        this.rebuildMessageList();
        this.addSystemMessage('{yellow-fg}↩️ 已撤销{/yellow-fg}');
      }
    });

    this.screen.key('C-y', () => {
      const restored = this.undoRedo.redo(this.messages);
      if (restored) {
        this.messages = restored;
        this.rebuildMessageList();
        this.addSystemMessage('{yellow-fg}↪️ 已重做{/yellow-fg}');
      }
    });

    // 在帮助栏显示 Undo/Redo 状态
    this.updateHelpBar();
  }
}
```

### Undo/Redo 设计考量

| 考量 | 方案 |
|------|------|
| **快照 vs 命令** | 快照实现简单但占用内存；命令模式更精确但实现复杂 |
| **历史深度** | 限制最多 50 步快照，防止内存溢出 |
| **深拷贝** | 使用 `JSON.parse(JSON.stringify())` 确保快照不被后续修改影响 |
| **UI 反馈** | 撤销/重做后显示提示信息，帮助用户感知操作结果 |
| **自动保存触发** | 每次用户发送消息时自动保存快照，无需手动 |
| **流式期间禁用** | 在 streaming/thinking 状态下禁用 Undo/Redo，防止状态不一致 |

## 6.11 状态检视与调试面板

开发过程中需要能够观察应用内部状态的变化。以下是在 TUI 中集成调试工具的方法。

### 状态变更日志

```typescript
class StateDebugLogger {
  private log: Array<{
    timestamp: Date;
    action: string;
    prevState: Partial<AppState>;
    nextState: Partial<AppState>;
  }> = [];
  private readonly MAX_LOG = 1000;

  /** 记录一次状态变更 */
  snapshot(
    action: string,
    prevState: Partial<AppState>,
    nextState: Partial<AppState>,
  ): void {
    this.log.push({
      timestamp: new Date(),
      action,
      prevState: { ...prevState },
      nextState: { ...nextState },
    });

    // 日志上限保护
    if (this.log.length > this.MAX_LOG) {
      this.log.splice(0, this.log.length - this.MAX_LOG);
    }

    // 开发模式下输出到控制台
    if (process.env.DEBUG) {
      console.log(`[State] ${action}:`, prevState, '→', nextState);
    }
  }

  /** 获取最近的 N 条日志 */
  recent(n: number = 10): string[] {
    return this.log.slice(-n).map(entry =>
      `[${entry.timestamp.toISOString().slice(11, 19)}] ${entry.action}`
    );
  }

  /** 导出完整日志 */
  export(): string {
    return this.log.map(entry =>
      `[${entry.timestamp.toISOString()}] ${entry.action}`
    ).join('\n');
  }

  /** 搜索日志 */
  search(query: string): typeof this.log {
    return this.log.filter(entry =>
      entry.action.toLowerCase().includes(query.toLowerCase())
    );
  }
}

// 使用
const stateLogger = new StateDebugLogger();

// 每次状态变化时记录
class ChatTUIDebug extends ChatTUI {
  private setState(partial: Partial<AppState>): void {
    const prev = { status: this.state.status, ... };
    Object.assign(this.state, partial);
    stateLogger.snapshot('setState', prev, partial);
  }
}
```

### 内置调试面板

按 `F12` 或 `C-d` 打开覆盖层，实时查看状态：

```typescript
class DebugPanel {
  private panel: Widgets.BoxElement | null = null;
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private screen: Widgets.Screen,
    private getState: () => AppState,
    private getMessages: () => ChatMessage[],
  ) {}

  toggle(): void {
    if (this.panel) {
      this.close();
    } else {
      this.open();
    }
  }

  private open(): void {
    this.panel = blessed.box({
      parent: this.screen,
      top: 1, right: 0,
      width: 40,
      height: '50%',
      style: { fg: 'white', bg: '#111122' },
      border: { type: 'line', fg: '#4444aa' },
      label: ' 调试面板 ',
      tags: true,
      scrollable: true,
      alwaysScroll: true,
    });

    // 每 500ms 刷新状态显示
    this.refreshTimer = setInterval(() => this.update(), 500);
    this.update();
  }

  private update(): void {
    if (!this.panel) return;
    const state = this.getState();
    const msgs = this.getMessages();

    const content = [
      '{bold}⚙️ 运行时状态{/bold}',
      `状态: {${this.stateColor(state.status)}-fg}${state.status}{/${this.stateColor(state.status)}-fg}`,
      `消息数: ${msgs.length}`,
      `Token 数: ${state.totalTokens ?? '--'}`,
      '',
      '{bold}📋 最近事件{/bold}',
      ...stateLogger.recent(8).map(line => `  ${line}`),
      '',
      '{bold}💾 内存{/bold}',
      `  Messages: ~${JSON.stringify(msgs).length} bytes`,
      `  Undo 栈: ${undoRedoManager ? undoRedoManager['undoStack']?.length ?? 0 : 0}`,
    ].join('\n');

    this.panel.setContent(content);
    this.panel.setScrollPerc(100);
    this.screen.render();
  }

  private stateColor(status: string): string {
    const colors: Record<string, string> = {
      idle: 'green', thinking: 'yellow', streaming: 'cyan',
      tool_call: 'magenta', tool_result: 'blue', error: 'red',
    };
    return colors[status] || 'white';
  }

  private close(): void {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
    if (this.panel) {
      this.panel.detach();
      this.panel = null;
      this.screen.render();
    }
  }
}

// 绑定调试面板快捷键
const debugPanel = new DebugPanel(screen, () => state, () => messages);
screen.key('f12', () => debugPanel.toggle());
screen.key('C-d', () => debugPanel.toggle()); // 备选
```

### 开发模式与生产模式的分离

```typescript
class DebugMode {
  static isEnabled(): boolean {
    return process.env.DEBUG === 'true' ||
           process.env.NODE_ENV === 'development' ||
           process.argv.includes('--debug');
  }

  static setup(screen: Widgets.Screen): void {
    if (!this.isEnabled()) return;

    // 开发模式下：显示更多信息，添加调试快捷键
    const stateDebugger = new StateDebugLogger();

    // 捕获所有 screen.render() 调用并记录频率
    const originalRender = screen.render.bind(screen);
    let renderCount = 0;
    screen.render = () => {
      renderCount++;
      originalRender();
    };

    // 在状态栏显示渲染计数
    setInterval(() => {
      if (renderCount > 0) {
        // 更新状态栏显示渲染统计
        renderCount = 0;
      }
    }, 5000);
  }
}

// 启动时检查
DebugMode.setup(screen);
```

### 调试工具设计原则

| 原则 | 说明 |
|------|------|
| **无侵入** | 调试代码通过条件编译或环境变量控制，不影响生产环境 |
| **实时性** | 状态面板每 500ms 刷新一次，接近实时观察状态变化 |
| **可检索** | 状态变更日志支持关键词搜索，快速定位问题 |
| **可导出** | 日志和状态快照可导出为 JSON，便于离线分析 |
| **低开销** | 日志上限 1000 条，定期清理，不会导致内存膨胀 |
| **快捷键** | F12 随时开关，不干扰正常操作 |

---

**实践：** 查看 `examples/llm-chat.ts` 中的完整状态管理实现，尝试添加一个新的状态（如 `confirming` —— 工具调用前等待用户确认）。

**下一步：** [第七章：工具调用 UI 实现](07-tool-calls.md)
