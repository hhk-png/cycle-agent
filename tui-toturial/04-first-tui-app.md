# 第四章 第一个 TUI 应用

## 4.1 项目初始化

运行本教程的示例前，请先安装依赖：

```bash
cd tui-toturial/examples
npm install
```

这会安装以下依赖：

| 包 | 用途 | 版本 |
|------|------|------|
| `blessed` | TUI 运行时框架 | ^0.1.81 |
| `tsx` | TypeScript 执行器（无需编译，直接运行 .ts 文件） | ^4.19.0 |
| `typescript` | TypeScript 类型检查 | ^5.5.0 |
| `@types/blessed` | blessed 的类型定义 | ^0.1.19 |

### 为什么使用 tsx 而不编译？

```
传统方式:  .ts ──→ tsc ──→ .js ──→ node
tsx 方式:  .ts ──→ tsx  ──→ 直接执行

优势: 无需构建步骤，即时运行，体验接近脚本语言
```

## 4.2 运行基础示例

```bash
cd tui-toturial/examples
npx tsx basic-tui.ts
```

您将看到一个包含按钮、列表和进度条动画的分栏 TUI 应用：

```
┌──────────────────────────────────────────────────────────────┐
│ TUI 基础示例 | Blessed.js 0.1 | Tab切换 | ↑↓导航 | Ctrl+Q退出│
├────────────────────┬─────────────────────────────────────────┤
│ ■ 组件展示         │ ■ 事件日志                              │
│                    │                                         │
│ 正常文本 红色 绿色  │ [12:00:01] 系统就绪                     │
│ 黄色 粗体          │ [12:00:05] 确定按钮被点击                │
│                    │                                         │
│ ■ 按钮             │                                         │
│ [ 确定 ] [ 取消 ]  │                                         │
│                    │                                         │
│ ■ 列表             │                                         │
│ ▸ 🚀 选项一       │                                         │
│   ⚙️ 选项二       │                                         │
│   📊 选项三       │                                         │
│   🔄 选项四       │                                         │
│   ❌ 选项五       │                                         │
│                    │                                         │
│ ■ 进度条动画       │                                         │
│ ┌─ 任务A ────┐    │                                         │
│ │ ▓▓▓░░░ 45% │    │                                         │
│ └────────────┘    │                                         │
│ [ ▶ 开始 ] [ ↺ ]│                                         │
├────────────────────┴─────────────────────────────────────────┤
│ ● 运行中 | Tab:切换焦点 ↑↓:导航 Enter:确认 Ctrl+Q:退出        │
└──────────────────────────────────────────────────────────────┘
```

## 4.3 代码深度解析

### 4.3.1 创建屏幕

```typescript
import * as blessed from "blessed";
import type { Widgets } from "blessed";

const screen = blessed.screen({
  smartCSR: true,       // 智能光标保存/恢复 —— 减少闪烁
  fullUnicode: true,    // 完整 Unicode 支持（emoji/CJK）
  title: "TUI 基础示例", // 终端标签标题
  useBCE: true,         // 使用背景色擦除（减少绘制量）
  resizeTimeout: 200,   // resize 事件节流 (ms)
});
```

**关键选项解释：**

| 选项 | 作用 | 推荐值 | 效果 |
|------|------|--------|------|
| `smartCSR` | 启用后只更新变化区域，而非全屏重绘 | `true` | 大幅减少闪烁和输出量 |
| `useBCE` | 使用背景色擦除字符，减少输出量 | `true` | 减少终端 IO |
| `fullUnicode` | 完整 Unicode 支持（如 emoji） | `true` | 防止中文/emoji 显示异常 |
| `useBCE` | 使用背景色擦除减少输出 | `true` | 减少终端 IO |
| `resizeTimeout` | 窗口 resize 事件的节流时间 (ms) | `200` | 避免频繁重绘 |

### 4.3.2 布局构建

```typescript
// ── 顶部标题栏（固定 1 行） ──
const titleBar = blessed.box({
  parent: screen,
  top: 0,
  left: 0,
  width: "100%",
  height: 1,
  content: " {bold}TUI 基础示例{/bold}  |  Ctrl+Q 退出",
  style: { fg: "white", bg: "#2255aa" },
  tags: true,
});

// ── 左侧面板（宽度 50%） ──
const leftPanel = blessed.box({
  parent: screen,
  top: 1,
  left: 0,
  width: "50%",
  bottom: 1,           // 底部留出 1 行给状态栏
  border: { type: "line", fg: "#44aacc" },
  style: { fg: "white", bg: "#111111" },
  padding: { left: 1, right: 1 },
});

// ── 右侧面板（宽度 50%，日志区域） ──
const rightPanel = blessed.box({
  parent: screen,
  top: 1,
  left: "50%",
  width: "50%",
  bottom: 1,
  label: " 事件日志 ",
  border: { type: "line", fg: "#cc8844" },
  style: { fg: "#cccccc", bg: "#0d0d0d" },
  scrollable: true,     // 启用滚动
  alwaysScroll: true,
});
```

**布局计算规则：**

```
top + height + bottom ≤ 屏幕高度
left + width + right ≤ 屏幕宽度

使用百分比时，值基于屏幕当前尺寸动态计算
使用 'shrink' 时，尺寸由内容决定
```

### 4.3.3 交互组件

```typescript
// ── 按钮 ──
const btnOk = blessed.button({
  parent: leftPanel,
  top: 4,
  left: 3,
  width: 12,
  height: 1,
  content: " [ 确定 ] ",
  align: "center",
  style: {
    fg: "white",
    bg: "#226622",
    focus: { bg: "#44cc44" },  // 聚焦时绿色更亮
    hover: { bg: "#338833" },
  },
  mouse: true,  // 启用鼠标点击
});

btnOk.on("press", () => {
  log("{green-fg}确定{/green-fg} 按钮被点击");
});

// ── 列表 ──
const list = blessed.list({
  parent: leftPanel,
  top: 7,
  left: 3,
  width: "100%-6",
  height: 5,
  items: [
    "  🚀 选项一：启动服务",
    "  ⚙️  选项二：配置参数",
    "  📊 选项三：查看统计",
  ],
  style: {
    selected: { fg: "black", bg: "#88ccff" },  // 选中项高亮
  },
  keys: true,  // 启用键盘导航（↑↓）
  vi: true,    // 启用 vim 风格导航（j/k）
  mouse: true, // 启用鼠标选择
});

list.on("select", (item: Widgets.BoxElement) => {
  log(`选中: ${item.getContent().trim()}`);
});
```

**组件的关键属性：**

| 属性 | 作用 | 示例值 |
|------|------|--------|
| `mouse` | 启用鼠标交互 | `true` |
| `keys` | 启用键盘导航 | `true` |
| `vi` | Vim 风格键位 | `true` |
| `inputOnFocus` | 获取焦点后自动进入输入模式 | `true` |
| `shrink` | 根据内容自动调整尺寸 | `true` |

### 4.3.4 事件处理

```typescript
// ── 全局快捷键（key.ignore=true 防止事件泄漏到焦点组件） ──
screen.key("C-q", (_ch, key) => {
  key.ignore = true;
  process.exit(0);
});
screen.key("escape", () => log("按下 Escape"));

// ── Tab 循环切换焦点 ──
const focusable = [btnOk, btnCancel, list, btnStart, btnReset];
let focusIndex = 0;

screen.key("tab", () => {
  focusIndex = (focusIndex + 1) % focusable.length;
  focusable[focusIndex].focus();
});

screen.key("S-tab", () => {
  focusIndex = (focusIndex - 1 + focusable.length) % focusable.length;
  focusable[focusIndex].focus();
});

// ── 窗口大小变化 ──
screen.on("resize", () => {
  screen.render();
});

// ── 鼠标点击自动聚焦 ──
focusable.forEach((w) => {
  w.on("focus", () => {
    focusIndex = focusable.indexOf(w);
  });
});
```

> **💡 关于 `key.ignore`：** 在 blessed 中，`screen.key()` 注册的全局快捷键触发后，按键事件仍会继续传播到当前焦点的组件（如 `textarea`、`list`），可能导致控制字符被插入输入框。设置 `key.ignore = true` 可以阻止焦点组件处理此键。这是 Windows 下避免 `Ctrl+Q`、`Ctrl+S` 等按键产生乱字符的关键技巧。

#### Windows 兼容性说明

Windows Terminal + ConPTY 环境下，blessed 的键盘事件处理可能不稳定。建议采用**三层兜底**策略确保核心快捷键可用：

```
层级 1: screen.key('C-q', handler)   ← blessed 标准绑定
层级 2: screen.on('keypress', ...)    ← keypress 事件直检键名
层级 3: process.stdin.on('data', ...) ← 原始字节流匹配（绕过 blessed）
```

具体实现参看 `examples/basic-tui.ts` 和 `examples/llm-chat.ts` 中的相关代码。

#### 4.3.4.1 事件委托模式

事件委托是一种将事件处理逻辑集中管理而非分散在各组件中的设计模式。

**委托模式 1：全局快捷键注册表**

将所有快捷键集中在一个地方管理：

```typescript
interface KeyBinding {
  combo: string;
  description: string;
  handler: (ch: string, key: { name: string; full: string; ctrl: boolean; shift: boolean }) => void;
}

class KeyBindingManager {
  private bindings: Map<string, KeyBinding> = new Map();
  private screen: Widgets.Screen;

  constructor(screen: Widgets.Screen) {
    this.screen = screen;
  }

  register(keyCombo: string, description: string, handler: KeyBinding['handler']): void {
    this.bindings.set(keyCombo, { combo: keyCombo, description, handler });
    this.screen.key(keyCombo, (ch, key) => {
      handler(ch, key);
    });
  }

  getHelpText(): string {
    return Array.from(this.bindings.values())
      .map(b => `  ${b.combo}: ${b.description}`)
      .join('\n');
  }
}

const keys = new KeyBindingManager(screen);
keys.register('C-q', '退出', () => process.exit(0));
keys.register('C-s', '发送消息', () => sendMessage());
keys.register('tab', '切换焦点', () => cycleFocus());
```

**委托模式 2：事件总线**

使用中央事件总线解耦组件间通信：

```typescript
import { EventEmitter } from 'events';

interface AppEventMap {
  'message:send': [text: string];
  'message:receive': [text: string, role: 'user' | 'assistant'];
  'stream:chunk': [chunk: string];
  'stream:end': [fullText: string];
  'error:occur': [error: Error];
  'panel:resize': [width: number, height: number];
}

class TypedEventBus {
  private emitter = new EventEmitter();

  emit<K extends keyof AppEventMap>(event: K, ...args: AppEventMap[K]): void {
    this.emitter.emit(event as string, ...args);
  }

  on<K extends keyof AppEventMap>(event: K, handler: (...args: AppEventMap[K]) => void): void {
    this.emitter.on(event as string, handler as (...args: any[]) => void);
  }

  off<K extends keyof AppEventMap>(event: K, handler: (...args: AppEventMap[K]) => void): void {
    this.emitter.off(event as string, handler as (...args: any[]) => void);
  }
}

const bus = new TypedEventBus();

bus.on('message:send', (text: string) => {
  appendToChatLog(text, 'user');
  callLLMApi(text);
});

bus.on('message:receive', (text: string, role: 'user' | 'assistant') => {
  appendToChatLog(text, role);
});
```

**委托模式 3：组件消息路由**

```typescript
interface ComponentMessage {
  from: string;
  to: string;
  type: string;
  payload: unknown;
}

class MessageRouter {
  private components: Map<string, Widgets.BoxElement> = new Map();

  register(id: string, component: Widgets.BoxElement): void {
    this.components.set(id, component);
  }

  unregister(id: string): void {
    this.components.delete(id);
  }

  send(message: ComponentMessage): void {
    const target = this.components.get(message.to);
    if (target) {
      target.emit('message', message);
    }
  }

  broadcast(type: string, payload: unknown): void {
    this.components.forEach((component, id) => {
      component.emit('message', { from: 'system', to: id, type, payload });
    });
  }
}
```

**委托模式选择指南：**

| 模式 | 适用场景 | 复杂度 | 解耦程度 |
|------|---------|-------|---------|
| 快捷键注册表 | 集中管理快捷键 | 低 | 中 |
| 事件总线 | 跨组件通信 | 中 | 高 |
| 消息路由 | 大型应用、面向消息架构 | 高 | 极高 |
| 回调注入 | 简单父子通信 | 低 | 低 |

### 4.3.5 焦点管理详解

焦点管理是 TUI 交互的核心。blessed 中的焦点链（focus chain）决定了 Tab 切换的顺序：

```
Screen 级别:
  screen.key('tab', ...)   ← 全局 Tab 处理

组件级别:
  widget.focus()           ← 手动聚焦到某个组件
  widget.on('focus', ...)  ← 聚焦事件
  widget.on('blur', ...)   ← 失焦事件

样式反馈:
  style: {
    focus: { bg: '#4488cc' }  ← 聚焦时自动应用此样式
  }
```

**实现焦点管理的两种策略：**

```typescript
// 策略 1: 手动维护焦点列表（推荐）
const focusable = [btnOk, btnCancel, list, btnStart, btnReset];
let focusIndex = 0;
screen.key('tab', () => {
  focusIndex = (focusIndex + 1) % focusable.length;
  focusable[focusIndex].focus();
});

// 策略 2: 遍历组件树（需要组件实现 focusable 标记）
function findNextFocusable(elements: Widgets.BoxElement[], current: Widgets.BoxElement): Widgets.BoxElement {
  const idx = elements.indexOf(current);
  return elements[(idx + 1) % elements.length];
}
```

### 4.3.6 自定义进度条实现

```typescript
function createProgressBar(
  screen: Widgets.Screen,
  parent: Widgets.BoxElement,
  opts: { top: number; label: string; color: string; width?: number },
) {
  const W = opts.width ?? 40;
  let progress = 0;
  let running = false;
  let timer: ReturnType<typeof setInterval> | null = null;

  const bar = blessed.box({
    parent,
    top: opts.top,
    left: 2,
    width: W + 4,
    height: 3,
    border: { type: "line", fg: opts.color },
    label: ` ${opts.label} `,
    tags: true,
    style: { fg: "white", bg: "black" },
  });

  function render() {
    const filled = Math.round((progress / 100) * W);
    const empty = W - filled;
    const pct = String(progress).padStart(3);
    bar.setContent(
      ` {${opts.color}-fg}{bold}${"▓".repeat(filled)}{/bold}${"░".repeat(empty)} ${pct}%{/${opts.color}-fg}`,
    );
    screen.render();
  }

  return {
    start() {
      if (running) return;
      running = true;
      progress = 0;
      render();
      timer = setInterval(() => {
        progress = Math.min(100, progress + Math.random() * 8 + 1);
        render();
        if (progress >= 100) {
          running = false;
          if (timer) clearInterval(timer);
          timer = null;
        }
      }, 150);
    },
    stop() { /* ... */ },
    reset() { /* ... */ },
  };
}
```

### 4.3.7 组件生命周期钩子

TUI 组件从创建到销毁经历完整的生命周期。理解这些阶段是编写可靠 TUI 应用的基础：

```
创建 (create) → 挂载 (mount) → 渲染 (render) → 更新 (update) → 销毁 (destroy)
```

| 阶段 | 触发时机 | 关键操作 | 示例 |
|------|---------|---------|------|
| **创建** | 调用 `blessed.box()` 等构造函数 | 初始化属性、绑定事件 | `const box = blessed.box({ ... })` |
| **挂载** | 设置 `parent` 并首次 `render()` | 布局计算、首次绘制 | `screen.render()` |
| **渲染** | 每次 `screen.render()` 调用 | 差分比较、终端输出 | `screen.render()` |
| **更新** | 调用 `setContent()`、`setLabel()` 等 | 内容/样式变更 | `box.setContent('新内容')` |
| **销毁** | 调用 `detach()` 或 `destroy()` | 清理资源、解绑事件 | `box.detach(); screen.render()` |

**生命周期实现示例：**

```typescript
import * as blessed from 'neo-blessed';
import type { Widgets } from 'neo-blessed';

interface ChatBubbleOptions {
  parent: Widgets.BoxElement;
  top: number;
  content: string;
  role: 'user' | 'assistant';
}

class ChatBubble {
  private box: Widgets.BoxElement;
  private created: number;
  private cleanupFns: Array<() => void> = [];

  constructor(private opts: ChatBubbleOptions) {
    // ── 创建阶段 ──
    this.created = Date.now();
    const isUser = opts.role === 'user';
    
    this.box = blessed.box({
      parent: opts.parent,
      top: opts.top,
      left: 0,
      width: '100%',
      height: 'shrink',
      padding: { left: 1, right: 1 },
      style: {
        fg: 'white',
        bg: isUser ? '#1a3a1a' : '#1a1a3a',
      },
      tags: true,
    });

    // ── 初始化阶段 ──
    this.updateContent(opts.content);
    this.bindEvents();
  }

  // ── 更新阶段 ──
  updateContent(content: string): void {
    const prefix = this.opts.role === 'user' ? '{bold}{green-fg}你{/green-fg}{/bold}' : '{bold}{cyan-fg}AI{/cyan-fg}{/bold}';
    this.box.setContent(`${prefix}: ${content}`);
  }

  // ── 事件绑定 ──
  private bindEvents(): void {
    const onResize = () => this.box.emit('resize');
    this.cleanupFns.push(() => this.box.removeListener('resize', onResize));
  }

  // ── 渲染阶段 ──
  render(): void {
    this.box.screen.render();
  }

  // ── 销毁阶段 ──
  destroy(): void {
    // 执行所有清理函数
    this.cleanupFns.forEach(fn => fn());
    this.cleanupFns = [];
    // 从屏幕移除组件
    this.box.detach();
  }
}

// 使用示例
const bubble = new ChatBubble({
  parent: chatPanel,
  top: 0,
  content: 'Hello! 有什么可以帮助你的？',
  role: 'assistant',
});
bubble.render();
// ... 一段时间后清理
bubble.destroy();
```

### 4.3.8 TUI 组件的清理模式

TUI 组件的正确清理至关重要，否则会导致**内存泄漏**、**事件重复绑定**和**终端状态异常**。

#### 模式 1：清理注册表（Recommended）

```typescript
import type { Widgets } from 'neo-blessed';

interface CleanupRegistry {
  timers: Set<ReturnType<typeof setInterval>>;
  listeners: Map<string, Set<(...args: any[]) => void>>;
  children: Set<Widgets.BoxElement>;
}

function createCleanupRegistry(): CleanupRegistry {
  return {
    timers: new Set(),
    listeners: new Map(),
    children: new Set(),
  };
}

function registerTimer(registry: CleanupRegistry, timer: ReturnType<typeof setInterval>): void {
  registry.timers.add(timer);
}

function registerListener(
  registry: CleanupRegistry,
  target: { on: (event: string, handler: (...args: any[]) => void) => void },
  event: string,
  handler: (...args: any[]) => void,
): void {
  target.on(event, handler);
  if (!registry.listeners.has(event)) {
    registry.listeners.set(event, new Set());
  }
  registry.listeners.get(event)!.add(handler);
}

function cleanupAll(registry: CleanupRegistry): void {
  registry.timers.forEach(timer => clearInterval(timer));
  registry.timers.clear();
  registry.listeners.clear();
  registry.children.forEach(child => child.detach());
  registry.children.clear();
}
```

#### 模式 2：析构函数模式

```typescript
abstract class TUIComponent {
  protected destroyed = false;
  private cleanupQueue: Array<() => void> = [];

  protected onCleanup(fn: () => void): void {
    this.cleanupQueue.push(fn);
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    for (const fn of this.cleanupQueue.reverse()) {
      try {
        fn();
      } catch (err) {
        console.error('Cleanup error:', err);
      }
    }
    this.cleanupQueue = [];
    if (this.element && this.element.parent) {
      this.element.detach();
    }
  }

  protected abstract get element(): Widgets.BoxElement | null;
}

class ProgressBarWidget extends TUIComponent {
  private bar: Widgets.BoxElement;
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(parent: Widgets.BoxElement) {
    super();
    this.bar = blessed.box({
      parent,
      top: 0,
      left: 0,
      width: '100%',
      height: 1,
    });
    this.onCleanup(() => {
      if (this.timer) {
        clearInterval(this.timer);
        this.timer = null;
      }
    });
  }

  protected get element(): Widgets.BoxElement | null {
    return this.bar;
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => {
      this.bar.setContent('█'.repeat(Math.floor(Math.random() * 50)));
      this.bar.screen.render();
    }, 100);
  }
}
```

#### 模式 3：终端状态恢复（必须）

```typescript
function withTUISafeExit(): void {
  const originalExit = process.exit.bind(process);
  const exitHandler = (signal: string) => {
    process.stdin.setRawMode?.(false);
    process.stdout.write('\x1b[2J\x1b[H');
    process.stdout.write('\x1b[?25h');
    process.stdout.write('\x1b[?1000l');
    process.stdout.write('\x1b[?1002l');
    process.stdout.write('\x1b[?1006l');
    originalExit(signal === 'exit' ? 0 : 1);
  };

  process.on('SIGINT', () => exitHandler('SIGINT'));
  process.on('SIGTERM', () => exitHandler('SIGTERM'));
  process.on('exit', () => exitHandler('exit'));
}
```

#### 组件清理检查清单

| 资源类型 | 清理方式 | 必须清理？ |
|---------|---------|-----------|
| `setInterval` / `setTimeout` | `clearInterval` / `clearTimeout` | ✅ 是 |
| 事件监听（`on`/`addListener`） | `removeListener` / `off` | ✅ 是 |
| 子组件（`detach`） | `child.detach()` | ✅ 是 |
| 文件描述符（日志文件等） | `fd.close()` | ✅ 是 |
| 子进程（LLM API 调用） | `child.kill()` | ⚠️ 推荐 |
| 终端原始模式 | `stdin.setRawMode(false)` | ✅ 是 |
| 动画/渲染循环 | 设置停止标志 | ✅ 是 |

## 4.4 调试 TUI 应用的技巧

TUI 应用调试比较特殊，因为 `console.log` 输出会破坏界面。以下是推荐的方法：

### 4.4.1 日志到文件

```typescript
import * as fs from "fs";

const logFile = fs.createWriteStream("/tmp/tui-debug.log", { flags: "a" });

function debug(msg: string) {
  logFile.write(`[${new Date().toISOString()}] ${msg}\n`);
}

// 使用
debug(`当前状态: ${state}, 消息数: ${messages.length}`);
```

### 4.4.2 使用 blessed.log 组件

```typescript
const logBox = blessed.log({
  parent: screen,
  top: "50%",
  left: 0,
  width: "100%",
  height: "50%",
  content: "日志区域",
  style: { fg: "white", bg: "#111111" },
});

logBox.log("这条消息会自动滚动显示");
logBox.log(`组件状态: ${JSON.stringify(state)}`);
```

### 4.4.3 调试模式切换

```typescript
class DebugManager {
  private static debugMode = false;
  private static logStream: fs.WriteStream | null = null;

  static init(debugFile = "/tmp/tui-debug.log"): void {
    if (process.env.TUI_DEBUG === "1") {
      this.debugMode = true;
      this.logStream = fs.createWriteStream(debugFile, { flags: "a" });
      this.log("Debug 模式已启动");
    }
  }

  static log(msg: string): void {
    if (this.debugMode && this.logStream) {
      this.logStream.write(`[${new Date().toISOString()}] ${msg}\n`);
    }
  }

  static isDebug(): boolean {
    return this.debugMode;
  }
}

// 启动时:
// TUI_DEBUG=1 npx tsx basic-tui.ts
DebugManager.init();
DebugManager.log("应用启动");
```

### 4.4.4 错误捕获与终端恢复

```typescript
process.on("uncaughtException", (err) => {
  // 先退出 TUI 模式，恢复终端
  process.stdout.write("\x1b[2J\x1b[H");   // 清屏
  process.stdout.write("\x1b[?25h");        // 显示光标
  process.stdout.write("\x1b[?1000l");      // 禁用鼠标事件
  process.stdout.write("\x1b[?1002l");
  process.stdout.write("\x1b[?1006l");
  process.stdin.setRawMode?.(false);        // 退出原始模式

  console.error("未捕获异常:", err);
  process.exit(1);
});
```

### 4.4.5 调试 TUI 渲染问题

TUI 渲染问题是开发中最常见的调试场景。以下是针对 blessed 渲染问题的系统诊断方法：

#### 渲染问题速查表

| 现象 | 可能原因 | 解决方案 |
|------|---------|---------|
| 组件显示为空白 | 未调用 `screen.render()` | 每次修改后调用 `screen.render()` |
| 内容更新后无变化 | smartCSR 未检测到变化 | 确保使用 `setContent()` 而非直接修改 |
| 闪烁或撕裂 | 渲染频率过高 | 增加节流，或启用 `smartCSR: true` |
| 布局错乱 | 未处理 `resize` 事件 | 监听 `screen.on('resize', () => screen.render())` |
| 边框重叠 | 相邻组件边框冲突 | 启用 `dockBorders: true` |
| 颜色显示不对 | TrueColor 不支持 | 回退到 256 色命名色 |
| emoji/中文乱码 | Unicode 支持未开启 | 设置 `fullUnicode: true` |
| 鼠标事件无响应 | 鼠标支持未开启 | 设置 `screen.enableMouse()` |

#### 渲染诊断工具

```typescript
class RenderDebugger {
  private renderCount = 0;
  private lastRenderTime = 0;
  private totalRenderTime = 0;
  private screen: Widgets.Screen;

  constructor(screen: Widgets.Screen) {
    this.screen = screen;
  }

  /** 包装 screen.render() 进行性能诊断 */
  wrapRender(): void {
    const originalRender = this.screen.render.bind(this.screen);
    this.screen.render = () => {
      const start = performance.now();
      originalRender();
      const elapsed = performance.now() - start;
      this.renderCount++;
      this.totalRenderTime += elapsed;
      this.lastRenderTime = elapsed;
    };
  }

  /** 获取渲染统计 */
  getStats(): { renderCount: number; avgTime: number; lastTime: number; totalTime: number } {
    return {
      renderCount: this.renderCount,
      avgTime: this.renderCount > 0 ? this.totalRenderTime / this.renderCount : 0,
      lastTime: this.lastRenderTime,
      totalTime: this.totalRenderTime,
    };
  }

  /** 检测不必要的重复渲染 */
  detectExcessiveRenders(threshold = 60): void {
    let lastCheck = performance.now();
    let rendersInWindow = 0;

    const originalRender = this.screen.render.bind(this.screen);
    this.screen.render = () => {
      rendersInWindow++;
      const now = performance.now();
      if (now - lastCheck > 1000) {
        if (rendersInWindow > threshold) {
          debug(`[WARN] 渲染频率过高: ${rendersInWindow}次/秒 (阈值: ${threshold})`);
        }
        rendersInWindow = 0;
        lastCheck = now;
      }
      originalRender();
    };
  }

  /** 可视化渲染区域（调试用） */
  showRenderRegions(component: Widgets.BoxElement): void {
    const origContent = component.content;
    component.setContent('{red-bg}                                                                                                                                    {/red-bg}');
    component.screen.render();
    setTimeout(() => {
      component.setContent(origContent);
      component.screen.render();
    }, 500);
  }
}

// 使用
const debugger = new RenderDebugger(screen);
debugger.detectExcessiveRenders(60);
```

#### 常见渲染误区

1. **每次流式更新都调用 render()**
   ```typescript
   // ❌ 错误：逐字渲染会导致闪烁和性能问题
   stream.on('data', (chunk) => {
     content += chunk;
     box.setContent(content);
     screen.render();  // 每收到一个字就渲染
   });

   // ✅ 正确：使用节流
   let renderScheduled = false;
   stream.on('data', (chunk) => {
     content += chunk;
     box.setContent(content);
     if (!renderScheduled) {
       renderScheduled = true;
       setTimeout(() => {
         screen.render();
         renderScheduled = false;
       }, 50);
     }
   });
   ```

2. **修改组件属性后忘记 render()**
   ```typescript
   // ❌ 错误：不会生效
   list.setItems(newItems);

   // ✅ 正确：必须调用 render()
   list.setItems(newItems);
   screen.render();
   ```

3. **未设置 fullUnicode 导致文字截断**
   ```typescript
   // ❌ 错误：中文/emoji 显示异常
   const screen = blessed.screen({ smartCSR: true });

   // ✅ 正确：开启 fullUnicode
   const screen = blessed.screen({ smartCSR: true, fullUnicode: true });
   ```

#### 调试启动命令

```bash
# 启用渲染调试
TUI_DEBUG=1 npx tsx your-app.ts

# 捕获渲染日志到文件
TUI_DEBUG=1 TUI_DEBUG_FILE=./render-trace.log npx tsx your-app.ts

# 仅启用部分调试
TUI_DEBUG_RENDER=true TUI_DEBUG_EVENTS=true npx tsx your-app.ts
```

## 4.5 完整示例运行效果

```typescript
// 以下是 basic-tui.ts 的主流程
async function main() {
  // 1. 创建屏幕
  const screen = blessed.screen({ ... });

  // 2. 构建布局
  const titleBar = blessed.box({ ... });
  const leftPanel = blessed.box({ ... });
  const rightPanel = blessed.box({ ... });

  // 3. 添加组件
  const btnOk = blessed.button({ ... });
  const list = blessed.list({ ... });
  // ...

  // 4. 绑定事件
  btnOk.on('press', () => { ... });
  screen.key('C-q', () => process.exit(0));
  // ...

  // 5. 渲染
  screen.render();
}
```

## 4.6 组件间通信模式

在 TUI 中，不同的组件需要协作通信。以下是通用的模式：

### EventEmitter 模式

```typescript
import { EventEmitter } from 'events';

class AppBus extends EventEmitter {
  // 全局事件总线
}

const bus = new AppBus();

// 组件 A: 发布事件
bus.emit('message:send', text);

// 组件 B: 订阅事件
bus.on('message:send', (text) => {
  // 处理消息
});
```

### 回调注入

```typescript
interface Callbacks {
  onSend: (text: string) => void;
  onCancel: () => void;
  onError: (err: Error) => void;
}

class InputComponent {
  constructor(private callbacks: Callbacks) {}

  private handleSend() {
    this.callbacks.onSend(this.inputText);
  }
}
```

## 4.7 练习

### 基础练习

1. **添加计时器按钮**：修改 `basic-tui.ts`，添加一个"显示时间"按钮，点击后在右侧日志面板显示当前时间
2. **改变列表样式**：将列表的选中样式改为红色背景白色文字
3. **添加新的进度条**：在左侧面板底部添加一个蓝色进度条"D"，点击开始按钮后所有进度条同时运行
4. **实现输入框**：在左侧面板底部添加一个 `textarea` 输入框，输入内容回显到日志面板
5. **添加状态指示**：在标题栏添加一个闪烁的"●"指示器表示运行状态
6. **实现面板折叠**：添加一个按钮可以折叠/展开左侧面板

### 进阶练习

7. **实现键盘导航历史**：在输入框中添加上下箭头键浏览历史输入记录的功能
8. **添加主题切换**：实现暗色/亮色主题切换功能（使用 `Ctrl+t` 切换）
9. **缓冲渲染优化**：修改示例，使进度条更新使用 50ms 节流，对比优化前后的性能差异
10. **事件总线改造**：将现有的直接事件调用改为使用 `TypedEventBus` 模式重构
11. **组件生命周期**：选择一个组件，为其添加完整的生命周期管理（创建 → 更新 → 销毁）
12. **自适应布局**：实现一个响应式布局，当窗口宽度小于 80 列时自动将左右面板改为上下排列
13. **渲染性能监控**：集成 `RenderDebugger` 类，监控应用渲染次数和平均渲染时间
14. **清理模式实践**：为现有进度条组件添加 `TUIComponent` 基类，实现自动清理定时器

### 综合项目

15. **简易 LLM 聊天界面**：结合所学知识，构建一个最小化的 LLM 聊天界面：
   - 左侧为对话历史面板（可滚动）
   - 底部为多行输入框
   - 右侧为状态面板（显示连接状态、消息数等）
   - 支持 Ctrl+Enter 发送消息
   - 使用节流渲染优化流式输出
   - 实现基本的清理和错误恢复

## 4.8 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 组件不可见 | 未设置 `parent` 或坐标重叠 | 检查 parent、top/left 属性 |
| 键盘无响应 | 终端不在原始模式 | 确保 `setRawMode(true)` |
| Ctrl+S 被拦截 | 终端流控（XON/XOFF） | 运行 `stty -ixon` |
| 界面闪烁 | 渲染频率过高 | 使用 `smartCSR: true` |
| emoji 显示为方框 | 终端编码不支持 Unicode | 设置 `fullUnicode: true` |
| 颜色不准确 | TrueColor 不支持 | 回退到 256 色 |
| 布局错乱 | 窗口大小变化未处理 | 监听 `resize` 事件 |

---

## 本章更新日志

| 版本 | 日期 | 更新内容 |
|------|------|---------|
| v1.1 | 2026-07 | 新增 4.3.7 组件生命周期钩子 |
| v1.1 | 2026-07 | 新增 4.3.8 TUI 组件的清理模式 |
| v1.1 | 2026-07 | 新增 4.3.4.1 事件委托模式 |
| v1.1 | 2026-07 | 新增 4.4.5 调试 TUI 渲染问题 |
| v1.1 | 2026-07 | 扩展 4.7 练习，新增进阶练习和综合项目 |
| v1.1 | 2026-07 | 完善所有代码示例的 TypeScript 类型定义 |
| v1.1 | 2026-07 | 修复 `as any` 类型转换，使用更精确的类型 |
| v1.1 | 2026-07 | 为 `list.on('select')` 回调添加显式类型 |
| v1.0 | 2026-06 | 初版发布 |

---

**下一步：** [第五章：大模型对话界面设计](05-llm-ui-design.md)
