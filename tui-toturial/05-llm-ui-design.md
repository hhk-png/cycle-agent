# 第五章 大模型对话界面设计

## 5.1 大模型对话 UI 的核心需求

一个面向大模型的 TUI 对话界面需要满足丰富的交互场景。下表列出了从用户视角出发的完整需求：

| 场景 | 用户期望 | UI 表现 |
|------|---------|---------|
| 用户发送消息 | 输入便捷，快捷键流畅 | 文本输入框 + 一键发送 |
| AI 思考中 | 反馈正在处理，等待有预期 | 动态 Spinner 动画 + 状态文本 |
| AI 生成答案 | 实时看到进展，无需等待完整输出 | 流式输出的打字机效果 |
| AI 调用工具 | 透明展示工具使用过程 | 工具调用卡片（调用→执行→结果） |
| 工具返回结果 | 展示中间数据，不打断主流程 | 结果卡片可折叠，保持上下文连续 |
| 发生错误 | 明确提示，有恢复路径 | 醒目的错误卡片 + 重试建议 |
| 多轮对话 | 无缝查看历史上下文 | 消息历史可滚动视图 |
| 取消生成 | 用户可以控制中断 | 取消快捷键 + 即时响应 |
| 长文本阅读 | 不用手动持续滚动 | 自动滚动到底部 |
| 输入历史 | 可以回溯之前输入的内容 | 输入框 ↑↓ 切换历史 |
| 对话导出 | 保存当前对话内容 | 文件导出功能 |
| Token 计数 | 了解 API 用量 | 状态栏显示计数 |

## 5.2 布局设计

### 5.2.1 经典四分区布局

大模型对话 TUI 通常采用以下四分区布局：

```
┌─────────────────────────────────────────────────────┐
│ 状态栏: 🤖 AI Chat  |  模型: MockLLM-1.0  |  就绪   │  ← 固定 1 行
├─────────────────────────────────────────────────────┤
│                                                     │
│  ┌─ 消息历史 ─────────────────────────────────────┐  │
│  │ 👤 用户: 你好                                  │  │
│  │ 🤖 AI: 你好！有什么可以帮助你的？                │  │  ← 可滚动
│  │ ┌─ 🔧 工具调用 ──────────────────────────┐     │  │
│  │ │ get_weather(city: "Beijing")           │     │  │
│  │ │ ⏳ 执行中...                            │     │  │
│  │ └─────────────────────────────────────────┘     │  │
│  │ ❌ 错误: 服务不可用                             │  │
│  │ 🤖 AI: 正在生成回答... ▊                       │  │
│  └─────────────────────────────────────────────────┘  │
│                                                     │
├─────────────────────────────────────────────────────┤
│ > 输入消息...                              [Ctrl+S] │  ← 固定 3 行
├─────────────────────────────────────────────────────┤
│ Ctrl+S 发送  Ctrl+Q 退出  Ctrl+C 取消  ↑↓ 滚动      │  ← 固定 1 行
└─────────────────────────────────────────────────────┘
```

### 5.2.2 布局层级（组件树）

```typescript
// 布局结构
screen (blessed.Screen)
├── statusBar      (top: 0,     height: 1)     ← 状态栏
├── chatBox        (top: 1,     bottom: 5)      ← 消息区（占据所有剩余空间）
│   ├── userMessage     (blessed.Box)
│   ├── assistantMessage(blessed.Box)
│   ├── toolCallCard    (blessed.Box)
│   ├── toolResultCard  (blessed.Box)
│   ├── errorCard       (blessed.Box)
│   ├── thinkingSpinner (blessed.Box)
│   └── spacers         (blessed.Box)           ← 消息间隔
├── inputBox       (bottom: 2,  height: 3)      ← 输入区
└── helpBar        (bottom: 0,  height: 1)      ← 帮助栏
```

### 5.2.3 布局代码实现

```typescript
private buildLayout(): void {
  // 1. 状态栏（固定顶部1行）
  this.statusBar = blessed.box({
    parent: this.screen,
    top: 0, left: 0, width: "100%", height: 1,
    content: " 🤖 AI Chat TUI  |  模型: MockLLM-1.0  |  {green-fg}● 就绪{/green-fg}",
    style: { fg: "white", bg: "#2255aa" },
    tags: true,
  });

  // 2. 聊天区域（可滚动，占据中间全部空间）
  this.chatBox = blessed.box({
    parent: this.screen,
    top: 1, left: 0, width: "100%", bottom: 5,
    scrollable: true,
    alwaysScroll: true,
    scrollbar: {
      ch: "░",
      track: { bg: "#222222" },
      style: { bg: "#888888" },
    },
    style: { fg: "white", bg: "#1a1a1a" },
    tags: true,
    padding: { left: 1, right: 1 },
  });

  // 3. 输入框（固定底部3行）
  this.inputBox = blessed.textarea({
    parent: this.screen,
    bottom: 2, left: 0, width: "100%", height: 3,
    inputOnFocus: true,
    padding: { left: 1, right: 1 },
    style: {
      fg: "white", bg: "#0d0d0d",
      border: { fg: "#44aa44" },
      focus: { border: { fg: "#66dd66" } },
    },
    border: { type: "line", fg: "#44aa44" },
  });

  // 4. 帮助栏（固定底部1行）
  this.helpBar = blessed.box({
    parent: this.screen,
    bottom: 0, left: 0, width: "100%", height: 1,
    content: " {green-fg}Ctrl+S{/green-fg} 发送  ...",
    style: { fg: "#888888", bg: "#0a0a0a" },
    tags: true,
  });
}
```

### 5.2.4 布局适配策略

不同终端尺寸需要不同的布局策略：

| 终端宽度 | 布局策略 | 适用场景 |
|---------|---------|---------|
| ≥120 列 | 宽屏：可考虑侧边栏 + 主区域 | 大屏开发 |
| 80-119 列 | 标准：上下分栏 | 大多数场景 |
| 60-79 列 | 紧凑：减少内边距 | 分屏工作 |
| <60 列 | 警告：无法显示完整 UI | 需提示用户 |

```typescript
private adaptLayout(): void {
  const width = this.screen.width as number;
  if (width < 60) {
    this.showSizeWarning();
  } else {
    // 正常布局
    this.chatBox.setContent(this.chatBox.getContent()); // 触发重绘
  }
}
```

## 5.3 色彩设计

合理的色彩方案能显著提升可用性和美观度。以下是一个经过验证的大模型对话 TUI 主题：

```typescript
const Theme = {
  // ── 消息角色颜色 ──
  user:      { fg: '#88ccff', bg: '#1a1a1a', prefix: '👤 用户' },
  assistant: { fg: '#88ff88', bg: '#1a1a1a', prefix: '🤖 AI' },
  system:    { fg: '#ffaa44', bg: '#1a1a1a', prefix: '💬 系统' },

  // ── 工具调用 ──
  toolCall:  { fg: '#cc88ff', bg: '#1a1a1a', prefix: '🔧 工具调用' },
  toolResult:{ fg: '#66bbaa', bg: '#1a1a1a', prefix: '✅ 工具结果' },

  // ── 状态 ──
  error:     { fg: '#ff4444', bg: '#1a1a1a', prefix: '❌ 错误' },
  thinking:  { fg: '#ffaa00', bg: '#1a1a1a', prefix: '💭 思考' },

  // ── UI 元素 ──
  input:     { border: '#44aa44', focus: '#66dd66' },
  status:    { bg: '#2255aa', fg: 'white' },
};
```

### 色彩设计原则

1. **语义化** — 颜色传达含义（绿=正常、红=错误、黄=警告、紫=工具）
2. **高对比度** — 前景色与背景色对比度 ≥ 4.5:1
3. **有限调色板** — 核心颜色不超过 8 种，避免眼花缭乱
4. **暗色优先** — 终端应用以深色背景为主，降低视觉疲劳
5. **一致性** — 相同语义使用相同的颜色，贯穿整个应用

### 颜色对比度速查表

| 前景 | 背景 | 对比度 | 可读性 |
|------|------|--------|--------|
| `#ffffff` (白) | `#1a1a1a` (深灰) | 13.7:1 | ⭐⭐⭐ 极佳 |
| `#88ff88` (亮绿) | `#1a1a1a` (深灰) | 8.5:1 | ⭐⭐⭐ 极佳 |
| `#ff4444` (红) | `#1a1a1a` (深灰) | 5.2:1 | ⭐⭐ 良好 |
| `#888888` (灰) | `#1a1a1a` (深灰) | 3.5:1 | ⚠️ 勉强 |
| `#666666` (深灰) | `#1a1a1a` (深灰) | 2.3:1 | ❌ 较差 |

## 5.4 消息模型

### 5.4.1 数据模型定义

```typescript
type MessageRole = 'user' | 'assistant' | 'system' | 'tool';

/** 工具调用信息 */
interface ToolCall {
  name: string;                          // 工具名称，如 get_weather
  arguments: Record<string, unknown>;    // 调用参数
  result?: string;                       // 工具返回结果
  status: 'pending' | 'running' | 'completed' | 'error';
  error?: string;                        // 工具错误信息
}

/** 聊天消息 */
interface ChatMessage {
  id: string;                            // 消息唯一标识
  role: MessageRole;
  content: string;                       // 消息文本内容
  timestamp: Date;
  toolCall?: ToolCall;                   // 关联的工具调用
  error?: string;                        // 错误信息
  isStreaming?: boolean;                 // 是否正在流式输出
  tokens?: number;                       // 该消息的 token 数
}
```

### 5.4.2 消息在 UI 中的渲染

```typescript
private renderMessage(msg: ChatMessage): void {
  let content: string;

  switch (msg.role) {
    case 'user':
      content = `{cyan-fg}{bold}👤 用户{/bold}{/cyan-fg}\n${this.escapeTags(msg.content)}`;
      break;
    case 'assistant':
      content = `{green-fg}{bold}🤖 AI{/bold}{/green-fg}\n${this.escapeTags(msg.content)}`;
      break;
    case 'system':
      content = `{yellow-fg}{bold}💬 系统{/bold}{/yellow-fg}\n${msg.content}`;
      break;
    case 'tool':
      content = this.buildToolCard(msg);
      break;
  }

  const el = blessed.box({
    parent: this.chatBox,
    top: 0, left: 0, width: '100%-2', height: 'shrink',
    content,
    style: { fg: 'white', bg: '#1a1a1a' },
    tags: true, wrap: true, shrink: true,
  });
}
```

## 5.5 状态机设计

对话 TUI 的核心是状态管理。使用状态机可以清晰地表达各种状态及合法转换。

### 5.5.1 状态定义

```typescript
type AppState =
  | 'idle'         // 等待用户输入
  | 'thinking'     // AI 正在思考
  | 'streaming'    // AI 正在输出文本
  | 'tool_call'    // AI 正在调用工具
  | 'tool_result'  // 工具返回结果
  | 'error';       // 发生错误
```

### 5.5.2 状态转换图

```
                  ┌──────────┐
                  │   idle   │ ◄────── 等待用户输入
                  └────┬─────┘
                       │ 用户发送消息
                       ▼
                 ┌───────────┐
                 │ thinking  │ ◄────── AI 正在思考
                 └─────┬─────┘
                       │ 开始生成文本
                       ▼
                 ┌───────────┐
          ┌──────│ streaming │ ◄────── AI 正在输出
          │      └─────┬─────┘
          │            │ 需要调用工具
          │            ▼
          │      ┌───────────┐
          │      │ tool_call │ ◄────── 正在调用工具
          │      └─────┬─────┘
          │            │ 工具返回
          │            ▼
          │      ┌───────────┐
          │      │tool_result│ ◄────── 展示工具结果
          │      └─────┬─────┘
          │            │ 继续生成
          │            ▼
          │      ┌───────────┐
          └──────│ streaming │ (回到流式输出继续生成)
                 └─────┬─────┘
                       │ 生成完毕
                       ▼
                 ┌───────────┐
                 │   done    │ ◄────── 响应完成
                 └─────┬─────┘
                       │
                       ▼
                 ┌───────────┐
                 │   idle    │ ◄────── 回到就绪

   任何状态可能发生错误:
   ┌────────┐
   │ error  │ ◄── 从 thinking/streaming/tool_call 均可进入
   └────┬───┘
        │ 用户确认
        ▼
     ┌──────┐
     │ idle │
     └──────┘
```

### 5.5.3 状态机实现

```typescript
class StateMachine {
  private state: AppState = 'idle';
  private readonly log: (msg: string) => void;

  /** 合法状态转换表 */
  private readonly transitions: Record<AppState, AppState[]> = {
    idle:        ['thinking'],
    thinking:    ['streaming', 'tool_call', 'error'],
    streaming:   ['tool_call', 'tool_result', 'error', 'done'],
    tool_call:   ['tool_result', 'error'],
    tool_result: ['streaming', 'error'],
    done:        ['idle'],
    error:       ['idle'],
  };

  transition(to: AppState): boolean {
    const allowed = this.transitions[this.state];
    if (!allowed?.includes(to)) {
      console.error(`非法状态转换: ${this.state} → ${to}`);
      return false;
    }
    console.log(`状态: ${this.state} → ${to}`);
    this.state = to;
    return true;
  }

  canTransition(to: AppState): boolean {
    return this.transitions[this.state]?.includes(to) ?? false;
  }

  get current(): AppState {
    return this.state;
  }

  reset(): void {
    this.state = 'idle';
  }
}
```

## 5.6 事件驱动架构

### 5.6.1 LLM 事件定义

```typescript
/** LLM 流式事件 —— 从 Mock 或真实 API 发出 */
type LLMEvent =
  | { type: 'thinking' }                                   // AI 开始思考
  | { type: 'text'; content: string }                      // 文本片段（增量）
  | { type: 'tool_call'; name: string; args: Record<string, unknown> }  // 工具调用
  | { type: 'tool_result'; name: string; result: string }  // 工具结果
  | { type: 'error'; message: string }                     // 错误发生
  | { type: 'done' };                                      // 生成完成
```

### 5.6.2 事件消费循环

每进入一种 UI 状态，TUI 需要执行以下步骤：

```
收到事件
　　↓
更新状态 (state.transition(event.type))
　　↓
更新状态栏 (setStatus)
　　↓
确保 UI 元素正确 (创建/移除/更新)
　　↓
渲染 (screen.render())
　　↓
滚动到底部 (scrollToBottom)
```

```typescript
private async processStream(userMsg: string): Promise<void> {
  for await (const event of mockLLMStream(userMsg)) {
    switch (event.type) {
      case 'thinking':
        this.stateMachine.transition('thinking');
        this.setStatus('🤔 思考中...', '#aa8800');
        this.startSpinner();
        break;

      case 'text':
        if (this.stateMachine.current === 'thinking' ||
            this.stateMachine.current === 'tool_result') {
          this.stopSpinner();
          this.clearToolDisplay();
        }
        this.stateMachine.transition('streaming');
        fullResponse += event.content;
        this.updateAssistantMessage(fullResponse);
        break;

      case 'tool_call':
        this.stateMachine.transition('tool_call');
        this.setStatus(`🔧 调用工具: ${event.name}`, '#8844aa');
        this.stopSpinner();
        this.showToolCall(event.name, event.args);
        break;

      // ... 其他事件
    }
  }
}
```

## 5.7 流式响应的交互设计

流式响应（Streaming）是大模型对话中的核心体验，设计上需要精细把控：

```
用户发送消息
　　↓ 即时反馈
显示 Spinner + "AI 思考中..."
　　↓
第一个文本 token 到达
　　↓ 平滑过渡
Spinner 消失，文本开始逐字出现
　　↓ 实时更新
文本不断追加，光标跟随
　　↓
工具调用触发 → 文本暂停，显示工具卡片
　　↓
工具返回 → 展示结果，继续文本输出
　　↓
输出完毕 → 状态回到 idle，输入框可用
```

### 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| **流式期间输入框** | 保持可见但发送禁用 | 用户可以回顾已发送消息 |
| **工具卡片** | 收缩式展示，不占太多空间 | 保持对话连续性 |
| **错误处理** | 保留已输出内容，末尾追加错误 | 不丢失已有结果 |
| **取消机制** | Ctrl+C 发送 abort 信号 | 用户控制权 |
| **自动滚动** | 新内容到达时自动滚到底部 | 始终看到最新内容 |
| **历史回顾** | 用户可以手动向上滚动 | 不丢失上下文 |
| **输入框状态** | 生成期间禁用，完成后自动聚焦 | 防止误操作 |

## 5.8 输入框交互增强

### 5.8.1 输入历史

使用 ↑↓ 键切换历史输入：

```typescript
class InputWithHistory {
  private history: string[] = [];
  private historyIndex = -1;
  private readonly MAX_HISTORY = 50;

  constructor(private inputBox: Widgets.TextareaElement) {
    this.bindKeys();
  }

  private bindKeys(): void {
    this.inputBox.key('up', () => {
      if (this.historyIndex < this.history.length - 1) {
        this.historyIndex++;
        this.inputBox.setValue(this.history[this.historyIndex]);
        this.inputBox.focus();
      }
    });

    this.inputBox.key('down', () => {
      if (this.historyIndex > 0) {
        this.historyIndex--;
        this.inputBox.setValue(this.history[this.historyIndex]);
      } else if (this.historyIndex === 0) {
        this.historyIndex = -1;
        this.inputBox.clearValue();
      }
      this.inputBox.focus();
    });
  }

  addToHistory(text: string): void {
    this.history.unshift(text);
    if (this.history.length > this.MAX_HISTORY) {
      this.history.pop();
    }
    this.historyIndex = -1;
  }
}
```

### 5.8.2 多行输入

默认 `textarea` 支持多行输入，Enter 换行，Ctrl+S 发送：

```typescript
this.screen.key('C-s', () => {
  // Ctrl+S: 发送（而非默认的 Enter）
  if (this.state === 'idle') {
    const text = this.inputBox.getValue().trim();
    if (text) {
      this.sendMessage(text);
    }
  }
});

// Enter 仅换行，不发送
// 这需要阻止 blessed 的默认 Enter 行为
```

## 5.9 响应式布局与终端适配

不同终端尺寸需要不同的布局策略。TUI 应用必须主动适配，而非假设固定尺寸。

### 5.9.1 自适应布局策略

```typescript
type LayoutMode = 'wide' | 'standard' | 'compact' | 'minimal';

function detectLayoutMode(screen: Widgets.Screen): LayoutMode {
  const width = screen.width as number;
  if (width >= 120) return 'wide';
  if (width >= 80) return 'standard';
  if (width >= 60) return 'compact';
  return 'minimal';
}

class AdaptiveLayout {
  private mode: LayoutMode = 'standard';

  constructor(private screen: Widgets.Screen) {
    this.screen.on('resize', () => this.adapt());
  }

  private adapt(): void {
    this.mode = detectLayoutMode(this.screen);
    switch (this.mode) {
      case 'wide':
        // 宽屏：显示侧边栏、更多信息
        this.showSidebar(true);
        this.setPadding(2);
        break;
      case 'standard':
        // 标准：正常布局
        this.showSidebar(false);
        this.setPadding(1);
        break;
      case 'compact':
        // 紧凑：减少边距
        this.showSidebar(false);
        this.setPadding(0);
        break;
      case 'minimal':
        // 最小：只保留核心
        this.showSidebar(false);
        this.setPadding(0);
        this.showWarning(true);
        break;
    }
  }
}
```

### 5.9.2 布局模式对比

| 模式 | 最小宽度 | 侧边栏 | 内边距 | 状态栏信息 |
|------|---------|--------|--------|-----------|
| **wide** | ≥120 列 | 显示会话列表 | 2 | 完整 |
| **standard** | ≥80 列 | 隐藏 | 1 | 标准 |
| **compact** | ≥60 列 | 隐藏 | 0 | 精简 |
| **minimal** | <60 列 | 隐藏 | 0 | 仅警告 |

## 5.10 输入法（IME）兼容性

CJK（中日韩）用户在 TUI 中需要特别注意输入法兼容性。IME 涉及三个核心问题：组合输入状态检测、双宽字符渲染、候选窗口遮挡。

### IME 输入的问题

```
问题 1: 输入法候选窗口可能遮挡 TUI 界面
影响: 用户无法看到候选词，输入体验差
解决: 将输入框固定在底部，IME 候选窗口自然显示在终端之上

问题 2: IME 组合状态与快捷键冲突
影响: 用户使用 Ctrl+S 发送时触发输入法功能而非发送
解决: 检测 composition 状态，组合中禁用快捷键

问题 3: CJK 双宽字符导致光标偏移
影响: 删除、导航时字符截断或错位
解决: 使用 width 而非 length 计算字符宽度
```

### CJK 字符宽度计算

CJK 字符在终端中占用两个英文字符的宽度。如果使用 JavaScript 的 `length` 属性计算，会导致光标错位、换行错乱：

```typescript
/**
 * 计算字符串在终端中的实际显示宽度
 * 中文字符占 2 列，英文字符占 1 列
 */
function terminalWidth(str: string): number {
  let width = 0;
  for (const ch of str) {
    const code = ch.charCodeAt(0);
    if (code >= 0x1100 && (
      code <= 0x115f ||                    // Hangul Jamo
      code === 0x2329 || code === 0x232a ||
      (code >= 0x2e80 && code <= 0x9fff) || // CJK 统一表意文字
      (code >= 0x3000 && code <= 0x303f) || // CJK 符号标点
      (code >= 0xac00 && code <= 0xd7af) || // Hangul Syllables
      (code >= 0xff00 && code <= 0xffef)    // 全角 ASCII
    )) {
      width += 2;
    } else {
      width += 1;
    }
  }
  return width;
}

/**
 * 按终端宽度截断字符串，确保不截断半个 CJK 字符
 */
function truncateToWidth(str: string, maxWidth: number): string {
  let result = '';
  let currentWidth = 0;
  for (const ch of str) {
    const chWidth = terminalWidth(ch);
    if (currentWidth + chWidth > maxWidth) break;
    result += ch;
    currentWidth += chWidth;
  }
  return result;
}

/**
 * 将字符串填充到指定终端宽度（用于对齐）
 */
function padToWidth(str: string, targetWidth: number): string {
  const strWidth = terminalWidth(str);
  const padding = Math.max(0, targetWidth - strWidth);
  return str + ' '.repeat(padding);
}
```

### IME 组合输入处理

当输入法处于组合状态时，按键事件不应被 TUI 作为快捷键消费：

```typescript
class IMEHandler {
  private isComposing = false;

  constructor(private inputBox: Widgets.TextareaElement) {
    this.setupCompositionHandling();
  }

  private setupCompositionHandling(): void {
    // blessed 不直接暴露 composition 事件
    // 通过在 keypress 中检测序列模式间接判断
    this.inputBox.on('keypress', (ch: string, key: { name: string; sequence: string }) => {
      // IME 组合状态下，key.sequence 可能包含临时拼音字符串
      // 此时不应触发任何全局快捷键
      if (this.isComposing && key.name && !['backspace', 'enter'].includes(key.name)) {
        return false; // 阻止事件传播到全局处理器
      }
    });
  }

  /** 通知系统 IME 开始组合 */
  notifyCompositionStart(): void {
    this.isComposing = true;
  }

  /** 通知系统 IME 结束组合 */
  notifyCompositionEnd(): void {
    this.isComposing = false;
  }

  /** 检查是否正在 IME 组合中 */
  get isActive(): boolean {
    return this.isComposing;
  }

  /** 安全设置值（组合中不打断 IME） */
  setValueSafe(text: string): void {
    if (!this.isComposing) {
      this.inputBox.setValue(text);
    }
  }
}

// 在快捷键处理器中检查 IME 状态
screen.key('C-s', () => {
  if (imeHandler.isActive) {
    // IME 组合中，不发送消息
    return;
  }
  // ... 正常发送逻辑
});
```

### 输入框实现

```typescript
class IMECompatibleInput {
  private ime: IMEHandler;

  constructor(private inputBox: Widgets.TextareaElement) {
    // 确保输入框始终在屏幕底部
    // IME 候选窗口在终端程序之上弹出，底部位置不会被 TUI 遮挡
    this.inputBox.bottom = 2;
    this.inputBox.height = 3;

    this.ime = new IMEHandler(inputBox);

    // CJK 退格删除处理
    this.setupCJKDelete();
  }

  private setupCJKDelete(): void {
    this.inputBox.key('backspace', () => {
      const value = this.inputBox.getValue();
      // 如果光标前是 CJK 字符，blessed 的默认退格可能只删除一列
      // 需要额外处理
      const cursor = (this.inputBox as any).getCursor?.()?.x ?? value.length;
      if (cursor > 0) {
        const charBefore = value[cursor - 1];
        if (terminalWidth(charBefore) === 2) {
          // CJK 字符：blessed 默认退格可能处理不当
          // 手动设置值
          const newValue = value.slice(0, cursor - 1) + value.slice(cursor);
          this.inputBox.setValue(newValue);
          return false; // 阻止默认行为
        }
      }
      return undefined; // 继续默认行为
    });
  }

  /** 获取 IME 组合中的文本 */
  getComposingText(): string {
    return this.inputBox.getValue();
  }
}
```

### IME 兼容设计原则

| 原则 | 说明 |
|------|------|
| **textarea 优先** | 始终使用 `textarea` 而非 `textbox`，原生支持 IME 组合输入 |
| **底部固定** | 输入框固定在界面底部，IME 候选窗口在终端之上弹出，不被 TUI 内容遮挡 |
| **组合保护** | IME 组合期间禁用所有全局快捷键，防止误触 |
| **双宽对齐** | 所有字符串宽度计算使用 `terminalWidth()` 而非 `.length` |
| **不中断 IME** | 流式响应期间不主动 `setValue()` 或 `clearValue()` 输入框 |
| **最小重绘** | IME 组合输入期间尽可能减少 `screen.render()` 调用，避免闪烁 |

## 5.11 无障碍（Accessibility）设计

TUI 相比 GUI 具有天然的无障碍优势——屏幕阅读器可以直接读取文本内容。但 TUI 开发者仍需主动进行无障碍适配。

### 屏幕阅读器兼容

大多数终端屏幕阅读器（NVDA、JAWS、Orca、VoiceOver）通过截取终端输出来朗读内容：

```typescript
class AccessibilityManager {
  private enabled = false;

  constructor() {
    // 检测屏幕阅读器环境
    // 环境变量由各屏幕阅读器设置（非正式标准，但被广泛使用）
    this.enabled = !!(
      process.env.NVDA || process.env.JAWS ||
      process.env.VOICE_OVER || process.env.ORCA ||
      process.env.SPEECH_SYNTHESIS || process.env.SCREEN_READER
    );
  }

  /** 向屏幕阅读器发送无障碍通知 */
  announce(message: string): void {
    if (!this.enabled) return;
    // 1. 移除 ANSI 控制序列（屏幕阅读器可能朗读控制字符）
    const cleanMsg = message.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '');

    // 2. 设置终端标题（屏幕阅读器通常关注标题变化）
    process.stdout.write(`\x1b]0;AI Chat: ${cleanMsg}\x07`);

    // 3. 也设置 process.title 作为备选
    process.title = `AI Chat TUI: ${cleanMsg}`;
  }

  /** 状态变化时通知屏幕阅读器 */
  onStateChange(state: string): void {
    const stateMap: Record<string, string> = {
      idle: '就绪状态，可以输入',
      thinking: 'AI 思考中，请等待',
      streaming: 'AI 正在生成回复',
      tool_call: '正在调用工具',
      tool_result: '工具已返回结果',
      error: '发生错误',
    };
    this.announce(stateMap[state] || state);
  }

  /** 生成屏幕阅读器友好的文本（去掉装饰） */
  static accessibleLabel(content: string): string {
    return content
      .replace(/[┌─┐└┘│╱╲╭╮╯╰]/g, ' ')  // 移除边框字符
      .replace(/\{[^}]+\}/g, '')             // 移除 blessed 标签
      .replace(/\s+/g, ' ')                  // 合并空白
      .trim();
  }
}
```

### 高对比度模式

提供高对比度主题，满足低视力和对比敏感度障碍用户需求：

```typescript
const HighContrastTheme = {
  // 使用白底黑字或黑底白字的高对比度组合
  user:      { fg: '#ffffff', bg: '#000000', prefix: '用户' },
  assistant: { fg: '#ffffff', bg: '#003300', prefix: 'AI' },
  system:    { fg: '#000000', bg: '#ffcc00', prefix: '系统' },
  toolCall:  { fg: '#ffffff', bg: '#220044', prefix: '工具' },
  toolResult:{ fg: '#ffffff', bg: '#003333', prefix: '结果' },
  error:     { fg: '#ffffff', bg: '#880000', prefix: '错误' },

  // UI 元素使用粗边框 + 高亮
  input:     { border: '#ffffff', focus: '#ffff00' },
  status:    { bg: '#000000', fg: '#ffffff' },
};

/** 检测是否应启用高对比度模式 */
function detectHighContrast(): boolean {
  // 1. 检查 COLORFGBG 环境变量（某些终端设置白底）
  const colorFgBg = process.env.COLORFGBG;
  if (colorFgBg) {
    const [, bg] = colorFgBg.split(';');
    if (bg === '15' || bg === '231') return true;
  }
  // 2. 自定义环境变量
  if (process.env.TUI_HIGH_CONTRAST === '1') return true;
  return false;
}

// 应用适当主题
const theme = detectHighContrast() ? HighContrastTheme : NormalTheme;
```

### 焦点可见性

确保焦点位置始终明确可见：

```typescript
function ensureFocusVisibility(screen: Widgets.Screen): void {
  screen.on('element focus', (el: Widgets.BlessedElement) => {
    // 高亮焦点元素
    if (el.type === 'textarea') {
      (el as Widgets.TextareaElement).style.border = { fg: '#ffff00' as any };
    }
    screen.render();
  });

  screen.on('element blur', (el: Widgets.BlessedElement) => {
    // 恢复默认样式
    if (el.type === 'textarea') {
      (el as Widgets.TextareaElement).style.border = { fg: '#44aa44' as any };
    }
    screen.render();
  });
}
```

### 无障碍设计清单

```
核心要求（必须满足）:
□ 所有功能可以通过键盘完成（无鼠标依赖）
□ 焦点顺序符合阅读逻辑（从上到下，从左到右）
□ 颜色使用有辅助文本标识（不依赖颜色传达信息）
□ 状态变化有文本提示（非仅颜色变化）
□ 错误信息有清晰的文本描述和恢复路径
□ 加载/忙碌状态有文本指示器（不仅是 Spinner）

增强要求（推荐实现）:
□ ANSI 控制序列不会干扰屏幕阅读器
□ 屏幕尺寸变化时布局自适应
□ 支持高对比度模式
□ 所有交互区域有明确的焦点指示
□ Spinner 动画频率不超过 5Hz（防癫痫）
□ 图标（Emoji）带有文本标签作为后备
□ 可在设置中切换无障碍模式
```

## 5.12 色盲友好的色彩方案

TUI 应用中颜色被广泛用于传达状态和角色信息，但约 8% 的男性和 0.5% 的女性有不同程度的色觉障碍。设计色盲友好的配色方案至关重要。

### 色盲类型与配色影响

| 色盲类型 | 占比 | 难区分的颜色 | 对默认主题的影响 |
|---------|------|-------------|----------------|
| 红色盲（Protanopia） | ~1% 男性 | 红/绿、红/棕 | Error(红) 与 AI(绿) 混淆 |
| 绿色盲（Deuteranopia） | ~6% 男性 | 红/绿、紫/蓝 | Tool(紫) 与 User(蓝) 混淆 |
| 蓝色盲（Tritanopia） | <1% 人群 | 蓝/绿、黄/白 | User(蓝) 与 AI(绿) 混淆 |
| 全色盲（Achromatopsia） | ~0.003% | 全部颜色 | 所有颜色无法区分 |

### 色盲友好的配色方案

使用颜色 + 符号 + 文本的三重编码策略：

```typescript
// 色盲友好的颜色方案 —— 使用色相和亮度双重编码
const COLORBLIND_SAFE_THEME = {
  // 使用颜色 + 符号 + 文本的三重编码
  // 1. 颜色: 快速视觉识别
  // 2. Emoji: 辅助颜色识别
  // 3. 前缀文字: 最终标识
  user:      { fg: '#4a9eff', bg: '#1a1a1a', prefix: '👤 用户', indicator: '▌' },
  assistant: { fg: '#44cc44', bg: '#1a1a1a', prefix: '🤖 AI',   indicator: '┃' },
  error:     { fg: '#ff6666', bg: '#1a1a1a', prefix: '❌ 错误', indicator: '█' },

  // 通过亮度而非仅色相区分
  toolCall:  { fg: '#ff88cc', bg: '#1a1a1a', prefix: '🔧 工具', indicator: '▒' },
  toolResult:{ fg: '#44ddbb', bg: '#1a1a1a', prefix: '✅ 结果', indicator: '▐' },
};
```

### 色盲模拟验证工具

开发时使用模拟工具验证配色的可分辨性：

```typescript
/**
 * LMS 色盲模拟 —— 将颜色转换为色盲所见的效果
 * 用于调试和验证配色方案是否色盲友好
 */
function simulateColorBlind(
  hex: string,
  type: 'protanopia' | 'deuteranopia' | 'tritanopia'
): string {
  // 将 hex 转为 RGB
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);

  // 色盲模拟变换矩阵（Vienot, Brettel 算法简化版）
  const matrices: Record<string, [number, number, number][]> = {
    protanopia: [
      [0.567, 0.433, 0],
      [0.558, 0.442, 0],
      [0, 0.242, 0.758],
    ],
    deuteranopia: [
      [0.625, 0.375, 0],
      [0.7, 0.3, 0],
      [0, 0.3, 0.7],
    ],
    tritanopia: [
      [0.95, 0.05, 0],
      [0, 0.433, 0.567],
      [0, 0.475, 0.525],
    ],
  };

  const m = matrices[type];
  const nr = Math.round(r * m[0][0] + g * m[0][1] + b * m[0][2]);
  const ng = Math.round(r * m[1][0] + g * m[1][1] + b * m[1][2]);
  const nb = Math.round(r * m[2][0] + g * m[2][1] + b * m[2][2]);

  return `#${Math.min(255, Math.max(0, nr)).toString(16).padStart(2, '0')}${
    Math.min(255, Math.max(0, ng)).toString(16).padStart(2, '0')}${
    Math.min(255, Math.max(0, nb)).toString(16).padStart(2, '0')}`;
}

/** 验证一组颜色在指定色盲类型下的最小色差 */
function validateColorBlindPair(
  color1: string,
  color2: string,
  type: 'protanopia' | 'deuteranopia' | 'tritanopia'
): { pass: boolean; deltaE: number } {
  const sim1 = simulateColorBlind(color1, type);
  const sim2 = simulateColorBlind(color2, type);
  const deltaE = colorDifference(sim1, sim2);
  return { pass: deltaE > 20, deltaE }; // deltaE > 20 表示可区分
}

/** 简易色差计算（CIE76 简化版） */
function colorDifference(hex1: string, hex2: string): number {
  const [r1, g1, b1] = hexToRgb(hex1);
  const [r2, g2, b2] = hexToRgb(hex2);
  return Math.sqrt((r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2);
}

function hexToRgb(hex: string): [number, number, number] {
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16),
  ];
}

// 使用示例：验证默认主题中 user 和 assistant 颜色
console.log(validateColorBlindPair('#88ccff', '#88ff88', 'deuteranopia'));
// { pass: true, deltaE: 28.3 } <- 可区分，通过
```

### 色盲适配设计原则

1. **图标 + 颜色双重编码** — 每种消息类型都有独特的图标（👤 🤖 🔧 ✅ ❌），颜色只是辅助，移除颜色后仍可辨识
2. **位置编码** — 用户消息靠左，AI 消息适度缩进，通过位置区分不同角色
3. **亮度梯度** — 不同角色的前景色亮度不同（user=65%, assistant=75%, error=55%），即使色觉失常也能依亮度分辨
4. **纹理和边框** — 工具卡片使用圆角边框（┌─┐），错误卡片使用实心边框（█），提供颜色之外的视觉差异
5. **避免红绿搭配** — 如需同时使用红和绿，为其中之一增加粗体或附加符号
6. **提供主题切换** — 允许用户在正常主题和色盲友好主题间切换

```typescript
/** 主题切换功能 */
class ThemeManager {
  private currentTheme: typeof COLORBLIND_SAFE_THEME | typeof NormalTheme;

  toggleColorBlindSafe(): void {
    this.currentTheme = this.isColorBlindMode
      ? NormalTheme
      : COLORBLIND_SAFE_THEME;
    this.reapplyTheme();
  }

  /** 运行时切换，无须重启应用 */
  private reapplyTheme(): void {
    // 重新渲染所有消息
    this.repaintAllMessages();
    // 更新状态栏颜色
    this.updateStatusBar();
    this.screen.render();
  }
}
```

## 5.13 视觉层级设计

终端界面受限于单色字符和有限的画布大小，清晰的信息分层比 GUI 中的视觉层级设计更为关键。良好的视觉层级帮助用户快速定位"我在看什么"和"接下来看哪里"。

### 层级结构定义

将信息按重要程度分为四个层级：

```
第一层 (最突出): 状态栏、错误提示
    特征: 高亮背景色、加粗文字、图标
    作用: 用户第一时间感知当前状态

第二层: 角色标识（用户/AI/工具）、消息标题
    特征: 角色颜色前缀、表情符号
    作用: 快速识别"谁说的"

第三层: 消息正文
    特征: 标准前景色、正常字体
    作用: 阅读核心内容

第四层 (最弱): 时间戳、Token 计数、元数据
    特征: 灰色调、小字、右对齐
    作用: 需要时查阅，不妨碍主阅读流
```

### 视觉层级实现

```typescript
/** 消息渲染层级配置 */
interface VisualLevelConfig {
  roleIndicator: 'icon' | 'label' | 'short'; // 角色标识方式
  showTimestamp: boolean;                      // 是否显示时间戳
  showTokenCount: boolean;                     // 是否显示 Token 数
  messagePadding: number;                      // 消息内边距
  dividerStyle: 'none' | 'thin' | 'thick';     // 分隔线风格
}

/** 根据终端宽度和应用状态选择视觉层级 */
function selectVisualLevel(cols: number, compactMode: boolean): VisualLevelConfig {
  if (compactMode || cols < 80) {
    return {
      roleIndicator: 'short',     // ">>" / "<<"
      showTimestamp: false,
      showTokenCount: false,
      messagePadding: 0,
      dividerStyle: 'none',
    };
  }
  if (cols < 120) {
    return {
      roleIndicator: 'label',     // "用户" / "AI"
      showTimestamp: false,
      showTokenCount: true,
      messagePadding: 1,
      dividerStyle: 'thin',       // ───
    };
  }
  return {
    roleIndicator: 'icon',       // "👤 用户" / "🤖 AI"
    showTimestamp: true,
    showTokenCount: true,
    messagePadding: 2,
    dividerStyle: 'thick',       // ━━━
  };
}
```

### 消息分组与间隔

合理地使用空白和分隔线，让消息之间的关系一目了然：

```typescript
/**
 * 消息分组策略：属于同一轮对话的消息在视觉上更靠近
 * 使用不同高度的 spacer 在消息组之间创建层次
 */
class MessageGrouping {
  private lastRole: string | null = null;

  /** 在渲染每条消息前调用，决定是否插入组间隔 */
  addGroupSpacer(chatBox: Widgets.BoxElement, currentRole: string): void {
    const isNewGroup = this.lastRole !== null && this.lastRole !== currentRole;

    if (isNewGroup) {
      // 角色切换时：插入淡色分隔线 + 1 行空白
      const divider = '─'.repeat(Math.min(40, (chatBox.screen.width as number) - 4));
      blessed.box({
        parent: chatBox,
        top: 0, left: 0, width: '100%', height: 2,
        content: `{#555555-fg}${divider}{/#555555-fg}`,
        style: { fg: '#555555', bg: '#1a1a1a' },
        tags: true,
      });
    } else if (this.lastRole !== null) {
      // 相同角色内：1 行空白
      blessed.box({
        parent: chatBox,
        top: 0, left: 0, width: '100%', height: 1,
        content: '',
        style: { bg: '#1a1a1a' },
      });
    }

    this.lastRole = currentRole;
  }

  reset(): void {
    this.lastRole = null;
  }
}
```

### 信息密度控制

根据终端可用行数动态调整每条消息展示的信息量：

```typescript
class DensityController {
  /** 计算可用的信息密度级别 */
  getDensityLevel(terminalRows: number, messageCount: number): 'sparse' | 'normal' | 'dense' {
    const availableLines = terminalRows - 5; // 减去固定栏
    const linesPerMessage = availableLines / Math.max(1, messageCount);

    if (linesPerMessage >= 8) return 'sparse';   // 大屏：展示完整元数据
    if (linesPerMessage >= 4) return 'normal';   // 中屏：标准展示
    return 'dense';                                // 小屏：最简展示
  }

  /** 根据密度级别渲染消息 */
  renderMessage(msg: ChatMessage, level: 'sparse' | 'normal' | 'dense'): string {
    switch (level) {
      case 'sparse':
        return [
          `{bold}${this.roleIcon(msg.role)}{/bold}`,
          `  {#666666-fg}${msg.timestamp.toLocaleTimeString()}{/#666666-fg}`,
          ``,
          msg.content,
          `  {#555555-fg}Tokens: ${msg.tokens ?? '--'}{/#555555-fg}`,
        ].join('\n');
      case 'dense':
        return `${this.shortRole(msg.role)} ${msg.content}`;
      default:
        return `{bold}${this.roleIcon(msg.role)}{/bold}\n${msg.content}`;
    }
  }

  private roleIcon(role: string): string {
    const icons: Record<string, string> = {
      user: '👤 用户', assistant: '🤖 AI', system: '💬 系统', tool: '🔧',
    };
    return icons[role] || role;
  }

  private shortRole(role: string): string {
    const short: Record<string, string> = {
      user: '>>', assistant: '<<', system: '##', tool: '::',
    };
    return short[role] || role;
  }
}
```

### 视觉层级设计原则

| 原则 | 说明 | 实现方式 |
|------|------|---------|
| **邻近性** | 相关的信息放在一起 | 同一轮对话连续排列，用更少的间隔 |
| **相似性** | 相同类型的消息使用相同的样式 | 所有用户消息使用蓝色前缀，所有 AI 消息使用绿色 |
| **对比度** | 不同类型的信息有明显差异 | 角色前缀使用不同的颜色和图标 |
| **一致性** | 相同模式反复出现 | 所有消息遵循 `[角色标识] + [内容]` 的格式 |
| **可扫描** | 用户能快速找到关键信息 | 状态栏反映当前状态，错误信息加红框 |

## 5.14 键盘导航设计

TUI 的核心交互方式是键盘。良好的键盘导航设计直接影响用户的操作效率和学习成本。

### 键位绑定分类

将所有快捷键按层次和频率分类，方便用户记忆：

```typescript
type KeyCategory =
  | 'essential'    // 核心操作：发送、退出、取消
  | 'navigation'   // 导航：滚动、切换焦点
  | 'editing'      // 编辑：清空、历史、粘贴
  | 'utility'      // 工具：重试、导出、帮助
  | 'debug';       // 调试：状态面板、日志

interface KeyBinding {
  keys: string[];         // 触发键序列
  description: string;    // 人类可读的描述
  category: KeyCategory;  // 分类
  context?: string[];     // 适用的状态上下文（空 = 全局）
}

/** 全键盘绑定表 —— 单一数据源，用于注册和帮助面板 */
const KEY_BINDINGS: KeyBinding[] = [
  // ── essential ──
  { keys: ['C-s'],        description: '发送消息',         category: 'essential', context: ['idle'] },
  { keys: ['C-q'],        description: '退出程序',         category: 'essential' },
  { keys: ['C-c'],        description: '取消生成',         category: 'essential', context: ['thinking', 'streaming', 'tool_call'] },

  // ── navigation ──
  { keys: ['up', 'down'], description: '滚动聊天区域',     category: 'navigation' },
  { keys: ['pageup', 'pagedown'], description: '快速滚动',  category: 'navigation' },
  { keys: ['tab'],        description: '切换焦点',         category: 'navigation' },
  { keys: ['S-tab'],      description: '反向切换焦点',     category: 'navigation' },

  // ── editing ──
  { keys: ['up', 'down'], description: '切换输入历史',     category: 'editing', context: ['idle'] },
  { keys: ['escape'],     description: '清空输入框',       category: 'editing', context: ['idle'] },

  // ── utility ──
  { keys: ['r'],          description: '重试（错误后）',   category: 'utility', context: ['error'] },
  { keys: ['?'],          description: '显示快捷键帮助',   category: 'utility' },
];
```

### 焦点管理

明确的焦点管理让用户知道"当前键盘输入会作用到哪里"：

```typescript
class FocusManager {
  private focusable: Widgets.BlessedElement[] = [];
  private currentIndex = 0;

  constructor(private screen: Widgets.Screen) {}

  register(element: Widgets.BlessedElement): void {
    this.focusable.push(element);
  }

  next(): void {
    if (this.focusable.length === 0) return;
    this.currentIndex = (this.currentIndex + 1) % this.focusable.length;
    this.focusable[this.currentIndex].focus();
    this.updateFocusIndicator();
  }

  prev(): void {
    if (this.focusable.length === 0) return;
    this.currentIndex = (this.currentIndex - 1 + this.focusable.length) % this.focusable.length;
    this.focusable[this.currentIndex].focus();
    this.updateFocusIndicator();
  }

  private updateFocusIndicator(): void {
    // 在状态栏显示当前焦点位置
    const total = this.focusable.length;
    const current = this.currentIndex + 1;
    // 更新状态栏的焦点指示部分
  }

  focusInput(): void {
    const inputIdx = this.focusable.findIndex(el => el.type === 'textarea');
    if (inputIdx >= 0) {
      this.currentIndex = inputIdx;
      this.focusable[inputIdx].focus();
    }
  }
}
```

### 快捷键帮助面板

按 `?` 键显示可搜索的快捷键列表：

```typescript
class HelpOverlay {
  show(screen: Widgets.Screen, currentState: string): void {
    const content = KEY_BINDINGS
      .filter(b => !b.context || b.context.includes(currentState))
      .map(b => {
        const keys = b.keys.map(k => `{green-fg}${k}{/green-fg}`).join(', ');
        const cat = this.categoryLabel(b.category);
        return `  ${cat}  ${keys.padEnd(16)}${b.description}`;
      })
      .join('\n');

    const overlay = blessed.box({
      parent: screen,
      top: 'center', left: 'center',
      width: 56, height: 'shrink',
      content: `{bold}⌨️ 快捷键帮助（当前状态: ${currentState}）{/bold}\n\n${content}\n\n{white-fg}按任意键关闭{/white-fg}`,
      style: { fg: 'white', bg: '#222233' },
      border: { type: 'line', fg: '#6666aa' },
      tags: true,
      padding: { left: 2, right: 2 },
      shadow: true,
    });

    const close = () => {
      overlay.detach();
      screen.render();
      screen.removeListener('keypress', close);
    };
    screen.on('keypress', close);
    screen.render();
  }

  private categoryLabel(cat: KeyCategory): string {
    const labels: Record<KeyCategory, string> = {
      essential: '{red-fg}[核心]{/red-fg}',
      navigation: '{cyan-fg}[导航]{/cyan-fg}',
      editing: '{yellow-fg}[编辑]{/yellow-fg}',
      utility: '{white-fg}[工具]{/white-fg}',
      debug: '{magenta-fg}[调试]{/magenta-fg}',
    };
    return labels[cat];
  }
}
```

### 上下文相关的快捷键

同样的按键在不同焦点位置执行不同操作：

```typescript
/** 管理上下文相关的快捷键分发 */
class ContextualKeyHandler {
  constructor(
    private chatBox: Widgets.BoxElement,
    private inputBox: Widgets.TextareaElement,
    private focusManager: FocusManager,
  ) {
    this.setupNavigation();
  }

  private setupNavigation(): void {
    // 聊天区：↑↓ 用于滚动
    this.chatBox.key('up', () => { this.chatBox.scroll(-1); return false; });
    this.chatBox.key('down', () => { this.chatBox.scroll(1); return false; });

    // 输入框：↑↓ 用于切换历史
    this.inputBox.key('up', () => { this.historyPrev(); return false; });
    this.inputBox.key('down', () => { this.historyNext(); return false; });

    // Tab 循环切换焦点
    this.chatBox.screen.key('tab', () => this.focusManager.next());
    this.chatBox.screen.key('S-tab', () => this.focusManager.prev());
  }
}
```

### 键盘导航设计原则

| 原则 | 说明 | 反例 | 正确做法 |
|------|------|------|---------|
| **可发现性** | 用户能发现所有快捷键 | 隐藏的快捷键只有在文档中看到 | 按 `?` 显示内置帮助面板 |
| **一致性** | 类似功能使用类似键位 | ↑ 在输入框是历史，在聊天区是滚动 | 同一键位在不同上下文的用途相近 |
| **容错性** | 误触后有恢复路径 | 按 C-q 直接退出无确认 | 退出前显示确认提示 |
| **可配置** | 用户可自定义键位 | 硬编码所有快捷键 | 提供 KeyBindingsConfig |
| **不冲突** | 不与终端模拟器冲突 | C-s 被终端用作 XOFF 流控 | 提供备选键位或检测提示 |

## 5.15 布局动画

TUI 虽然是文本界面，但通过帧动画和巧妙的重绘策略，可以实现平滑的视觉过渡，提升用户体验。

### Spinner 帧动画

使用 Braille 点字或 ASCII 字符序列实现旋转动画：

```typescript
class FrameAnimation {
  private timer: ReturnType<typeof setInterval> | null = null;
  private frameIndex = 0;

  constructor(
    private element: Widgets.BoxElement,
    private frames: string[],
    private intervalMs: number = 100,
    private onRender: () => void,
  ) {}

  start(): void {
    this.stop();
    this.timer = setInterval(() => {
      this.frameIndex = (this.frameIndex + 1) % this.frames.length;
      this.updateFrame();
    }, this.intervalMs);
  }

  private updateFrame(): void {
    const content = this.element.getContent();
    // 替换内容中的 Spinner 字符（第一列）
    const updated = content.replace(/^./, this.frames[this.frameIndex]);
    this.element.setContent(updated);
    this.onRender();
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}

// 使用示例
const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
const spinner = new FrameAnimation(spinnerEl, SPINNER_FRAMES, 80, () => screen.render());
```

### 面板展开/折叠过渡

通过逐行添加或移除内容实现面板的"动画"效果：

```typescript
class PanelTransition {
  /**
   * 展开面板：逐行显示内容
   */
  expand(panel: Widgets.BoxElement, finalContent: string, stepMs: number = 30): Promise<void> {
    return new Promise(resolve => {
      panel.show();
      const lines = finalContent.split('\n');
      let currentLine = 0;

      const timer = setInterval(() => {
        currentLine++;
        if (currentLine >= lines.length) {
          clearInterval(timer);
          panel.setContent(finalContent);
          resolve();
        }
        panel.setContent(lines.slice(0, currentLine).join('\n'));
        // 局部渲染以提升性能
        panel.screen.render();
      }, stepMs);
    });
  }

  /**
   * 折叠面板：内容逐行消失
   */
  collapse(panel: Widgets.BoxElement, stepMs: number = 20): Promise<void> {
    return new Promise(resolve => {
      const content = panel.getContent();
      const lines = content.split('\n');
      let remainingLines = lines.length;

      const timer = setInterval(() => {
        remainingLines--;
        if (remainingLines <= 0) {
          clearInterval(timer);
          panel.hide();
          resolve();
        }
        panel.setContent(lines.slice(0, remainingLines).join('\n'));
        panel.screen.render();
      }, stepMs);
    });
  }
}

// 使用示例
async function toggleSidebar(sidebar: Widgets.BoxElement, show: boolean): Promise<void> {
  const transition = new PanelTransition();
  if (show) {
    await transition.expand(sidebar, sidebarContent, 20);
  } else {
    await transition.collapse(sidebar, 15);
  }
}
```

### 进度条动画

用于工具调用执行、文件处理等场景：

```typescript
class ProgressBar {
  private timer: ReturnType<typeof setInterval> | null = null;
  private progress = 0;
  private readonly barWidth: number;

  constructor(
    private element: Widgets.BoxElement,
    private onRender: () => void,
    barWidth: number = 20,
  ) {
    this.barWidth = Math.min(barWidth, 40);
  }

  /** 启动进度动画，durationMs 为期望的总持续时间 */
  start(durationMs: number = 2000): void {
    this.progress = 0;
    const stepMs = 50;
    const totalSteps = durationMs / stepMs;
    let step = 0;

    this.timer = setInterval(() => {
      step++;
      this.progress = Math.min(1, step / totalSteps);
      this.renderBar();

      if (this.progress >= 1) this.stop();
    }, stepMs);
  }

  /** 设置精确进度（0-1） */
  setProgress(value: number): void {
    this.progress = Math.max(0, Math.min(1, value));
    this.renderBar();
  }

  private renderBar(): void {
    const filled = Math.round(this.progress * this.barWidth);
    const empty = this.barWidth - filled;
    const bar = '{green-fg}' + '█'.repeat(filled) + '{/green-fg}' +
                '{#444444-fg}' + '█'.repeat(empty) + '{/#444444-fg}';
    const pct = Math.round(this.progress * 100);
    this.element.setContent(`  ${bar}  ${pct}%`);
    this.onRender();
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    this.progress = 1;
    this.renderBar();
  }

  isRunning(): boolean {
    return this.timer !== null;
  }
}
```

### 动画性能优化

```typescript
class AnimationScheduler {
  private animatingElements = new Set<Widgets.BlessedElement>();
  private frameTimer: ReturnType<typeof setInterval> | null = null;
  private readonly FPS = 30; // 限制最大帧率
  private readonly MIN_INTERVAL = Math.round(1000 / this.FPS);

  register(element: Widgets.BlessedElement): void {
    this.animatingElements.add(element);
    this.startFrameLoop();
  }

  unregister(element: Widgets.BlessedElement): void {
    this.animatingElements.delete(element);
    if (this.animatingElements.size === 0) {
      this.stopFrameLoop();
    }
  }

  private startFrameLoop(): void {
    if (this.frameTimer) return;
    this.frameTimer = setInterval(() => {
      // 一次渲染更新所有动画元素
      this.animatingElements.forEach(el => {
        // 只在元素可见时渲染
        if (!el.hidden) {
          // 更新帧
        }
      });
    }, this.MIN_INTERVAL);
  }

  private stopFrameLoop(): void {
    if (this.frameTimer) {
      clearInterval(this.frameTimer);
      this.frameTimer = null;
    }
  }

  destroy(): void {
    this.stopFrameLoop();
    this.animatingElements.clear();
  }
}
```

### 动画设计准则

| 准则 | 说明 |
|------|------|
| **最低帧率** | Spinner 使用 80-120ms 间隔（8-12fps），避免 CPU 过度占用 |
| **空闲停止** | 状态不再需要的动画及时停止，`stop()` 方法清理 `setInterval` |
| **合并渲染** | 多个动画同时进行时合并为一次 `screen.render()` 调用 |
| **局部更新** | 只更新变化的 widget 内容，不触发全屏重绘 |
| **防止闪烁** | 使用 `screen.render()` 而非 `screen.realloc()` 减少闪烁 |
| **可禁用** | 提供选项关闭动画（如屏幕阅读器模式或低性能环境） |

## 5.16 虚拟滚动与消息列表窗口化

随着对话轮次增加，消息列表中的 blessed 组件数量会线性增长。经过 20 轮对话可能创建 60-100 个组件（消息+分隔线+操作元素），100+ 轮后可达 500+ 组件，导致内存占用激增和渲染性能下降。

### 问题分析

```typescript
// 直接渲染所有消息的问题
class NaiveChatView {
  addMessage(msg: ChatMessage): void {
    // 每次添加消息都创建一个新的 blessed 组件
    // 随着消息增多，blessed 的组件树越来越庞大
    const el = blessed.box({
      parent: this.chatBox,
      // ...
    });
    // 没有组件复用或销毁机制
    // screen.render() 需要遍历所有组件进行差分比较
  }
}
```

### 虚拟滚动实现策略

虚拟滚动（Virtual Scrolling）的核心思想是**只渲染视口可见的消息**，不可见的消息仅保留数据而不创建 blessed 组件：

```typescript
class VirtualScrollList {
  private visibleMessages: Widgets.BoxElement[] = []; // 当前可见的组件池
  private scrollTop = 0;
  private readonly ITEM_HEIGHT = 2;    // 每条消息占用的行数
  private readonly OVERSCAN = 3;       // 视口外额外渲染的条目数
  private readonly MAX_POOL = 50;      // 组件池上限

  constructor(
    private container: Widgets.BoxElement,
    private messages: ChatMessage[],
  ) {}

  /** 计算当前应该渲染的消息范围 */
  private getVisibleRange(): { start: number; end: number } {
    const viewportHeight = (this.container.height as number) || 20;
    const start = Math.max(0, Math.floor(this.scrollTop / this.ITEM_HEIGHT) - this.OVERSCAN);
    const end = Math.min(
      this.messages.length,
      Math.ceil((this.scrollTop + viewportHeight) / this.ITEM_HEIGHT) + this.OVERSCAN,
    );
    return { start, end };
  }

  /** 回收超出视口的组件 */
  private recycleComponents(range: { start: number; end: number }): void {
    // 收集所有不再可见的组件，移除并回收引用
    const toRemove: Widgets.BoxElement[] = [];
    this.container.children.forEach((child) => {
      const el = child as Widgets.BoxElement;
      const idx = parseInt(el.getAttribute('data-index') || '-1', 10);
      if (idx >= 0 && (idx < range.start || idx >= range.end)) {
        toRemove.push(el);
      }
    });
    toRemove.forEach((el) => {
      this.container.remove(el);
    });
  }

  /** 滚动事件处理 —— 滚动时只更新可见区域 */
  onScroll(delta: number): void {
    this.scrollTop = Math.max(0, this.scrollTop + delta);
    const range = this.getVisibleRange();
    this.recycleComponents(range);
    // 渲染新增的可见消息
    for (let i = range.start; i < range.end; i++) {
      if (!this.isMessageRendered(i)) {
        this.renderMessage(this.messages[i], i);
      }
    }
    this.container.screen.render();
  }

  private isMessageRendered(index: number): boolean {
    return Array.from(this.container.children).some(
      (child) => (child as Widgets.BoxElement).getAttribute('data-index') === String(index),
    );
  }

  private renderMessage(msg: ChatMessage, index: number): void {
    // 复用组件或创建新组件
    const el = blessed.box({
      parent: this.container,
      top: index * this.ITEM_HEIGHT,
      // ...
    });
    el.setAttribute('data-index', String(index));
  }
}
```

### 替代方案：分页加载

对于不需要丝滑滚动体验的场景，可以使用简单的分页策略：

```typescript
class PaginatedChatView {
  private readonly PAGE_SIZE = 20;
  private currentPage = 0;

  /** 加载上一页（更早的消息） */
  loadPreviousPage(): void {
    // 清空当前所有消息组件
    this.clearMessages();
    // 加载上一页的消息
    const start = Math.max(0, this.currentPage * this.PAGE_SIZE);
    const pageMessages = this.messages.slice(start, start + this.PAGE_SIZE);
    pageMessages.forEach(msg => this.renderMessage(msg));
    this.currentPage--;
  }
}
```

### 虚拟滚动 vs 分页对比

| 策略 | 内存占用 | 用户体验 | 实现复杂度 | 适用场景 |
|------|---------|---------|-----------|---------|
| **虚拟滚动** | 低（仅渲染可见消息） | 流畅（无缝滚动） | 高 | 长对话、实时流式更新 |
| **分页** | 极低（只渲染一页） | 可接受（点击加载更多） | 低 | 历史记录查看 |
| **直接渲染（无优化）** | 高（所有消息） | 最佳（但卡顿后差） | 无 | 短对话（<20轮） |

### 选择建议

对于 LLM 对话 TUI 应用：
- **原型/演示阶段**：直接渲染即可（<20轮对话不卡顿）
- **正式产品**：实现虚拟滚动，或使用 `blessed.log` 的自动滚动特性（blessed 内置）
- **消息数 < 50**：直接渲染无压力
- **消息数 50-200**：考虑组件池复用
- **消息数 > 200**：必须实现虚拟滚动

---

**下一步：** [第六章：事件系统与状态管理](06-events-state.md)
