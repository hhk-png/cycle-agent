# 第十章 错误处理与安全

> **本章衔接：** 上一章学习了 Markdown 渲染与富文本展示。本章聚焦 LLM TUI 中的错误处理、边界情况以及安全防护。

## 10.1 大模型对话中的错误类型

| 错误类型 | 示例 | 对 UI 的影响 | 恢复策略 |
|----------|------|-------------|---------|
| **网络错误** | 连接超时、DNS 解析失败 | 无法获取响应 | 显示重试按钮 |
| **API 错误** | 鉴权失败、速率限制 | 返回错误状态码 | 提示用户检查配置 |
| **模型错误** | 上下文超长、内容过滤 | 截断或拒绝生成 | 显示具体原因 |
| **工具错误** | 工具调用失败、参数无效 | 工具卡片显示错误 | 展示卡片 + 重试 |
| **解析错误** | 响应格式异常 | 流式解析中断 | 提示格式异常 |
| **终端错误** | 窗口太小、编码不支持 | 显示异常 | 礼貌提示 + 降级 |

## 10.2 错误 UI 设计

### 10.2.1 错误提示卡片

```typescript
/**
 * 显示错误卡片（红色边框，清晰的错误信息）
 */
private showError(message: string): void {
  // 清除所有动画元素
  this.stopSpinner();
  this.clearToolDisplay();

  const errorBox = blessed.box({
    parent: this.chatBox,
    top: 0,
    left: 0,
    width: '100%-2',
    height: 'shrink',
    content:
      `{red-fg}┌─ ❌ {bold}错误{/bold} ───────────────────────────────────┐{/red-fg}\n` +
      `{red-fg}│{/red-fg}\n` +
      `{red-fg}│{/red-fg}  {white-fg}${this.escapeTags(message)}{/white-fg}\n` +
      `{red-fg}│{/red-fg}\n` +
      `{red-fg}│{/red-fg}  请稍后重试或尝试其他问题。\n` +
      `{red-fg}└────────────────────────────────────────────────┘{/red-fg}`,
    style: { fg: 'white', bg: '#1a1a1a' },
    tags: true,
    wrap: true,
    shrink: true,
  });

  this.addSpacer();
  this.screen.render();
  this.scrollToBottom();
}
```

### 10.2.2 状态栏错误指示

```typescript
private setErrorStatus(message: string): void {
  this.statusBar.setContent(
    ` 🤖 AI Chat TUI  |  模型: MockLLM-1.0  |  {red-fg}❌ 错误: ${message}{/red-fg}`
  );
  this.statusBar.style.bg = '#992222';  // 红色背景
  this.screen.render();
}

private setIdleStatus(): void {
  this.statusBar.setContent(
    ` 🤖 AI Chat TUI  |  模型: MockLLM-1.0  |  {green-fg}● 就绪{/green-fg}`
  );
  this.statusBar.style.bg = '#2255aa';  // 恢复蓝色背景
  this.screen.render();
}
```

## 10.3 优雅的错误恢复

```typescript
private async processStream(userMsg: string): Promise<void> {
  try {
    for await (const event of mockLLMStream(userMsg)) {
      // ... 事件处理
    }
  } catch (err: unknown) {
    // ── 分类处理错误 ──
    if (isAbortError(err)) {
      // 用户取消 —— 不需要显示错误提示
      this.showSystemMessage('⏹️ 生成已取消');
    } else if (isNetworkError(err)) {
      // 网络问题 —— 建议重试
      this.showError('网络连接失败，请检查网络后重试');
      this.addRetryButton(userMsg);  // 添加重试按钮
    } else if (isTimeoutError(err)) {
      // 超时 —— 建议简化问题
      this.showError('请求超时，请尝试简化您的问题');
    } else if (isRateLimitError(err)) {
      // 速率限制
      this.showError('请求过于频繁，请稍后重试');
    } else {
      // 未知错误 —— 显示详细信息
      const msg = err instanceof Error ? err.message : String(err);
      this.showError(`未知错误: ${msg}`);
    }
  } finally {
    // 确保状态被重置，不会"卡"在某个中间状态
    this.state = 'idle';
    this.setIdleStatus();
    this.stopSpinner();
    this.clearToolDisplay();
    this.inputBox.focus();
  }
}

// 错误类型判断函数
function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}
function isNetworkError(err: unknown): boolean {
  return err instanceof TypeError && err.message.includes('fetch');
}
function isTimeoutError(err: unknown): boolean {
  return err instanceof Error && err.message.includes('timeout');
}
function isRateLimitError(err: unknown): boolean {
  return err instanceof Error && (err.message.includes('429') || err.message.includes('rate limit'));
}
```

### 10.3.1 添加重试按钮

错误后提供一键重试，提升用户体验：

```typescript
private addRetryButton(originalMessage: string): void {
  const retryBtn = blessed.button({
    parent: this.chatBox,
    top: 0,
    left: 2,
    width: 16,
    height: 1,
    content: ' 🔄 重试 ',
    style: {
      fg: 'white',
      bg: '#664422',
      focus: { bg: '#aa8844' },
      hover: { bg: '#886633' },
    },
    mouse: true,
  });

  retryBtn.on('press', () => {
    // 移除错误卡片和重试按钮
    this.chatBox.remove(retryBtn);
    if (this.currentErrorEl) {
      this.chatBox.remove(this.currentErrorEl);
      this.currentErrorEl = null;
    }
    // 重新发送
    this.sendMessage(originalMessage);
  });

  // 添加提示文本
  blessed.box({
    parent: this.chatBox,
    top: 0, left: 20,
    width: '100%-24', height: 1,
    content: '{yellow-fg}按 R 键重试 或 重新输入消息{/yellow-fg}',
    tags: true,
    style: { fg: 'yellow', bg: '#1a1a1a' },
  });

  // 快捷键重试
  const retryHandler = (ch: any, key: any) => {
    if (key.name === 'r') {
      this.screen.key('r', () => {}); // 解绑
      this.chatBox.remove(retryBtn);
      this.sendMessage(originalMessage);
    }
  };
  this.screen.key('r', retryHandler);

  this.screen.render();
  this.scrollToBottom();
}
```

## 10.4 边界情况处理

### 10.4.1 空输入（Empty Input）

```typescript
screen.key('C-s', () => {
  const text = this.inputBox.getValue().trim();
  if (!text) {
    this.flashInputBorder();  // 闪烁提示
    return;
  }
  this.sendMessage(text);
});

/**
 * 输入框边框闪烁 —— 视觉反馈提示用户输入内容
 */
private flashInputBorder(): void {
  const originalFg = this.inputBox.style.border?.fg;
  this.inputBox.style.border = { fg: 'red' };
  this.screen.render();

  setTimeout(() => {
    this.inputBox.style.border = { fg: originalFg || '#44aa44' };
    this.screen.render();
  }, 300);
}
```

### 10.4.2 消息过长（Message Too Long）

```typescript
private readonly MAX_MESSAGE_LENGTH = 2000;

private sendMessage(text: string): void {
  if (text.length > this.MAX_MESSAGE_LENGTH) {
    this.showSystemMessage(
      `⚠️ 消息过长 (${text.length} 字符)，最大支持 ${this.MAX_MESSAGE_LENGTH} 字符`
    );
    return;
  }
  // ... 正常发送
}
```

### 10.4.3 终端窗口过小（Small Terminal）

```typescript
constructor() {
  this.screen = blessed.screen({ ... });

  // 检查最小尺寸
  this.checkMinimumSize();

  // 监听窗口变化
  this.screen.on('resize', () => this.checkMinimumSize());
}

private checkMinimumSize(): void {
  const minCols = 60;
  const minRows = 12;
  const cols = this.screen.width as number;
  const rows = this.screen.height as number;

  if (cols < minCols || rows < minRows) {
    const warning = `⚠️ 窗口太小！需要至少 ${minCols}x${minRows}，当前 ${cols}x${rows}`;

    // 显示警告覆盖层
    const overlay = blessed.box({
      parent: this.screen,
      top: 'center', left: 'center',
      width: '80%', height: 3,
      content: `{red-fg}{bold}${warning}{/bold}{/red-fg}`,
      style: { bg: '#000000', fg: 'white' },
      tags: true,
      align: 'center',
      valign: 'middle',
    });

    this.screen.render();
  }
}
```

### 10.4.4 消息历史管理（防止内存泄漏）

```typescript
private readonly MAX_HISTORY = 100;

private addMessage(msg: ChatMessage): void {
  this.messages.push(msg);

  // 限制历史消息数，防止内存泄漏
  if (this.messages.length > this.MAX_HISTORY) {
    const removed = this.messages.splice(0, this.messages.length - this.MAX_HISTORY);

    // 同时也从 UI 中移除旧消息
    removed.forEach(m => {
      if (m.uiElement) {
        this.chatBox.remove(m.uiElement);
      }
    });
  }
}

// UI 消息数上限（防止渲染过多元素导致卡顿）
private readonly MAX_VISIBLE_MESSAGES = 50;

private trimVisibleMessages(): void {
  const children = this.chatBox.children;
  while (children.length > this.MAX_VISIBLE_MESSAGES * 2) { // *2 因为有 spacer
    const child = children[0];
    if (child) this.chatBox.remove(child);
  }
}
```

### 10.4.5 连续快速发送（Race Condition 防护）

```typescript
private async sendMessage(text: string): Promise<void> {
  if (this.state !== 'idle') {
    // 非空闲状态下的发送请求 —— 闪烁提示并忽略
    this.flashInputBorder();
    return;
  }

  this.state = 'thinking';
  // ... 处理响应
}
```

### 10.4.6 终端编码不兼容

```typescript
private detectEncodingIssues(): boolean {
  try {
    // 测试 Unicode 渲染
    const testStr = '中文测试 🌍🚀';
    Buffer.from(testStr, 'utf-8');
    return true;
  } catch {
    this.showSystemMessage(
      '⚠️ 检测到编码问题，请确保终端设置为 UTF-8 编码'
    );
    return false;
  }
}
```

### 10.4.7 输出溢出保护

当 AI 输出过长时（如生成大段代码），需要保护 UI 不被撑爆：

```typescript
private readonly MAX_DISPLAY_LENGTH = 5000;  // 最大显示字符数

private updateAssistantMessage(content: string): void {
  // 截断过长内容
  const truncated = content.length > this.MAX_DISPLAY_LENGTH
    ? content.slice(0, this.MAX_DISPLAY_LENGTH) +
      `\n\n{yellow-fg}... 内容过长，已截断 (${content.length} 字符)${/yellow-fg}`
    : content;

  // 渲染截断后的内容
  this.currentAssistantMsgEl?.setContent(
    `{green-fg}{bold}🤖 AI{/bold}{/green-fg}\n${this.escapeTags(truncated)}`
  );
  this.screen.render();
  this.scrollToBottom();
}
```

## 10.5 进程级异常处理（终端保护）

TUI 程序崩溃后如果终端处于原始模式（Raw Mode），用户可能"卡"在终端里。**必须确保退出时恢复终端状态：**

```typescript
/**
 * 全局异常处理器 —— 确保退出时恢复终端状态
 * 这是 TUI 应用最重要且最容易忽略的安全措施
 */
function setupGlobalErrorHandlers(screen: Widgets.Screen): void {
  const cleanup = () => {
    try {
      // 1. 清屏
      process.stdout.write('\x1b[2J\x1b[H');

      // 2. 恢复光标
      process.stdout.write('\x1b[?25h');

      // 3. 禁用鼠标事件
      process.stdout.write('\x1b[?1000l');
      process.stdout.write('\x1b[?1002l');
      process.stdout.write('\x1b[?1003l');
      process.stdout.write('\x1b[?1006l');

      // 4. 恢复颜色
      process.stdout.write('\x1b[0m');

      // 5. 退出原始模式
      process.stdin.setRawMode?.(false);
      process.stdin.pause();

      // 6. 销毁 blessed 屏幕
      try { screen.destroy(); } catch {}
    } catch (e) {
      // 清理过程本身出错也尽量恢复
    }
  };

  // ── 正常退出 ──
  process.on('exit', (code) => {
    cleanup();
  });

  // ── 未捕获异常 ──
  process.on('uncaughtException', (err) => {
    cleanup();
    console.error('\n❌ 未捕获异常:', err);
    process.exit(1);
  });

  // ── 未处理的 Promise 拒绝 ──
  process.on('unhandledRejection', (reason) => {
    cleanup();
    console.error('\n❌ 未处理的 Promise 拒绝:', reason);
    process.exit(1);
  });

  // ── 系统信号 ──
  process.on('SIGINT', () => {
    cleanup();
    process.exit(0);
  });
  process.on('SIGTERM', () => {
    cleanup();
    process.exit(0);
  });

  // ── blessed 屏幕销毁事件 ──
  screen.on('destroy', cleanup);
}
```

### 终端恢复的检查清单

```
退出 TUI 时，检查以下各项是否已恢复：

□ 光标可见     (\x1b[?25h)
□ 原始模式关闭 (process.stdin.setRawMode(false))
□ 鼠标事件关闭 (\x1b[?1006l)
□ 颜色重置     (\x1b[0m)
□ 清屏         (\x1b[2J\x1b[H)
□ 回显恢复     (stty echo)
□ 行缓冲恢复   (stty icanon)
```

## 10.6 有温度的退出设计

提供一个友好的退出体验，而不是"啪"地消失：

```typescript
screen.key('C-q', () => {
  // 禁用输入
  this.inputBox.setValue('');

  // 显示退出信息
  const exitMsg = blessed.box({
    parent: this.chatBox,
    top: 0, left: 0,
    width: '100%-2', height: 1,
    content: '  {cyan-fg}感谢使用 AI Chat TUI，再见！👋{/cyan-fg}',
    tags: true,
    style: { fg: 'white', bg: '#1a1a1a' },
  });

  this.screen.render();

  // 短暂延迟后退出（让用户看到告别信息）
  setTimeout(() => {
    this.cleanup();
    process.exit(0);
  }, 500);
});
```

## 10.7 测试错误场景

在 `examples/llm-chat.ts` 中，您可以通过特定关键词触发错误，用于测试 UI 的容错表现：

| 用户输入 | 触发场景 | UI 期望表现 |
|---------|---------|------------|
| "制造一个错误" | Mock LLM 触发 error 事件 | 红色错误卡片，状态回到 idle |
| "天气" + 快速连续发送 | 在 thinking 状态再次按 Ctrl+S | 输入闪烁提示，忽略发送 |
| 窗口拖到很小 | resize 事件触发尺寸检查 | 显示窗口太小警告 |
| 发送空白消息 | 空输入检测 | 输入框边框闪烁 |
| 发送超长消息 | 长度超过 MAX_MESSAGE_LENGTH | 系统消息提示过长 |

这种**错误注入**机制非常适合演示和测试 TUI 应用在各种异常情况下的表现。

## 10.8 TUI 安全防护

大模型对话 TUI 应用面临多种安全风险。作为 AI 工具，必须做好安全防护。

### 10.8.1 ANSI 注入攻击

ANSI 注入（又称终端转义序列注入）是 TUI 应用最严重的安全风险之一。攻击者可以通过在输入中嵌入恶意 ANSI 序列来破坏终端状态、窃取数据甚至执行任意命令。

```typescript
// ── 危险的 ANSI 注入示例 ──
// 如果用户输入包含以下内容，终端可能遭受攻击：
const maliciousInput = '\x1b[2J\x1b[H'; // 清屏
const maliciousInput2 = '\x1b[?25l';    // 隐藏光标

// ── 更严重的攻击：终端命令注入 ──
// 某些终端支持 OSC 序列执行操作
// \x1b]52;c;...（剪贴板操作）
// \x1b]8;;...  （超链接欺骗）
```

### 10.8.2 输入过滤与净化

```typescript
/**
 * 安全输入处理 —— 防止 ANSI 注入
 */
class InputSanitizer {
  /**
   * 移除或转义输入中的 ANSI 转义序列
   * 保留基本的格式字符（如 \n, \t）
   */
  static sanitize(input: string): string {
    // 移除所有 ANSI 转义序列（ESC 开头）
    return input.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '')   // CSI 序列
                .replace(/\x1b\][^\x1b]*(\x1b\\|\x07)/g, '') // OSC 序列
                .replace(/\x1b[PX^_].*?\x1b\\/g, '')        // 其他控制序列
                .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, ''); // 控制字符
  }

  /**
   * 安全显示用户输入（用于消息气泡中）
   * 保留可见文本，过滤所有控制字符
   */
  static safeDisplay(text: string): string {
    // 1. 移除控制字符
    let safe = this.sanitize(text);
    // 2. 转义 blessed 标签符号
    safe = safe.replace(/\{/g, '{open}').replace(/\}/g, '{close}');
    return safe;
  }

  /**
   * 检测是否包含可疑的 ANSI 序列
   */
  static containsAnsiSequences(text: string): boolean {
    return /[\x1b\x9b]/.test(text);
  }

  /**
   * 检测是否包含常见的终端攻击 payload
   */
  static isPotentialAttack(text: string): boolean {
    const attackPatterns = [
      /\x1b\]52;/,                        // 剪贴板操作
      /\x1b\]8;;/,                         // 超链接注入
      /\x1b\[[0-9;]*[hi]/,                // 设置模式（如原始模式）
      /\\x1b\[[0-9;]*m.*\\x1b\[[0-9;]*m/, // 重复的颜色注入
      /\x1b\[\?[0-9;]*[hl]/,               // DEC 私有模式
    ];
    return attackPatterns.some(p => p.test(text));
  }
}
```

### 10.8.3 在 TUI 中输入安全处理流程

```typescript
class ChatTUI {
  /**
   * 安全处理用户输入 —— 在发送前进行净化
   */
  private handleUserInput(): void {
    const rawText = this.inputBox.getValue();

    // 1. 安全检查：检测是否包含 ANSI 序列
    if (InputSanitizer.containsAnsiSequences(rawText)) {
      // 记录警告
      console.warn('检测到 ANSI 序列:', rawText.slice(0, 50));

      // 净化输入
      const sanitized = InputSanitizer.sanitize(rawText);
      this.inputBox.setValue(sanitized);
    }

    // 2. 安全检查：检测是否可能是攻击
    if (InputSanitizer.isPotentialAttack(rawText)) {
      this.showSystemMessage('⚠️ 检测到不安全的输入内容，已阻止');
      this.inputBox.clearValue();
      return;
    }

    // 3. 正常处理
    const text = InputSanitizer.safeDisplay(rawText.trim());
    if (!text) {
      this.flashInputBorder();
      return;
    }

    this.sendMessage(text);
  }

  /**
   * 安全显示 AI 回复 —— 防止 AI 输出中的 ANSI 注入
   * 注意：AI 输出可能包含合法的 Markdown 格式，
   * 所以只移除控制字符，保留格式符号
   */
  private safeDisplayAIContent(text: string): string {
    // 只移除控制字符，保留 Markdown 符号
    return text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '');
  }
}
```

### 10.8.4 其他安全最佳实践

| 风险 | 描述 | 防护措施 |
|------|------|---------|
| **ANSI 注入** | 恶意转义序列破坏终端 | 输入净化、正则过滤 |
| **隐私泄漏** | API Key 等敏感信息泄露 | 环境变量配置、不硬编码 |
| **提示注入** | 恶意 Prompt 劫持模型行为 | 输入长度限制、系统提示词强化 |
| **SSRF 攻击** | 工具调用中访问内部网络 | URL 白名单、禁止私有 IP |
| **工具滥用** | 工具被调用来执行危险操作 | 敏感工具需确认、权限分级 |
| **日志泄露** | 日志中包含敏感对话内容 | 日志脱敏、本地日志加密 |
| **MCP 安全** | MCP 服务器被恶意替换 | 校验服务器签名、路径白名单 |

### 10.8.5 API Key 安全处理

```typescript
class ApiKeyManager {
  private static readonly KEY_ENV_VARS = [
    'ANTHROPIC_API_KEY',
    'OPENAI_API_KEY',
    'GEMINI_API_KEY',
  ];

  /**
   * 从环境变量获取 API Key
   */
  static getKey(provider: string): string | null {
    const envVar = this.KEY_ENV_VARS.find(v =>
      v.toLowerCase().includes(provider.toLowerCase())
    );
    return envVar ? process.env[envVar] || null : null;
  }

  /**
   * 安全显示 —— 只显示 Key 的前 4 位
   */
  static maskDisplay(key: string): string {
    if (key.length <= 8) return '****';
    return key.slice(0, 4) + '****' + key.slice(-4);
  }

  /**
   * 检查是否配置了 API Key
   */
  static hasAnyKey(): boolean {
    return this.KEY_ENV_VARS.some(v => !!process.env[v]);
  }
}
```

### 10.8.6 安全退出与终端恢复

已在 [10.5 进程级异常处理](#105-进程级异常处理终端保护) 中详细说明。以下为安全检查清单补充：

```typescript
// ── 安全检查：退出时确保所有敏感信息被清除 ──
private secureCleanup(): void {
  // 1. 清空输入框
  this.inputBox.clearValue();

  // 2. 清除 API Key 相关显示
  this.statusBar.setContent(' 正在安全退出...');

  // 3. 清除剪贴板中的敏感信息
  // （如果使用了 OSC 52 剪贴板）

  // 4. 恢复终端状态
  this.cleanupTerminal();
}
```

## 10.9 错误处理检查清单

```
□ 所有 async 操作都有 try/catch
□ 状态机禁止非法转换（canTransition 校验）
□ 未捕获异常恢复终端状态
□ 未处理 Promise 拒绝有 handler
□ 输入框空值检测
□ 输入长度限制
□ 消息历史上限防止内存泄漏
□ 快速点击防护（race condition）
□ 窗口最小尺寸检查
□ Unicode 编码兼容检测
□ 终结点友好的退出信息
□ 退出后终端完全恢复
```

## 10.10 异步错误边界（Async Error Boundary）

在 LLM TUI 中，错误可能发生在多个层级。建议分层捕获，每层有独立的恢复策略。

### 10.10.1 边界管理器

```typescript
interface BoundaryOptions {
  /** 错误上下文名称 */
  context: string;
  /** 是否显示错误提示 */
  showUserFeedback?: boolean;
  /** 最大重试次数 */
  maxRetries?: number;
  /** 错误回调 */
  onError?: (err: Error) => void;
}

class ErrorBoundary {
  private handlers: Array<(err: Error, context: string) => void> = [];
  private retryCounts = new Map<string, number>();
  private readonly MAX_RETRIES = 3;

  /** 注册错误处理器 */
  addHandler(handler: (err: Error, context: string) => void): void {
    this.handlers.push(handler);
  }

  /** 包装异步操作 —— 自动捕获并分类错误 */
  async wrap<T>(context: string, fn: () => Promise<T>): Promise<T | null> {
    try {
      return await fn();
    } catch (err) {
      const error = err instanceof Error ? err : new Error(String(err));
      this.handlers.forEach(h => h(error, context));
      return null;
    }
  }

  /** 带重试的包装 */
  async wrapWithRetry<T>(
    context: string,
    fn: () => Promise<T>,
    options: { maxRetries?: number; retryDelay?: number } = {}
  ): Promise<T | null> {
    const { maxRetries = 2, retryDelay = 1000 } = options;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        return await fn();
      } catch (err) {
        const error = err instanceof Error ? err : new Error(String(err));

        if (attempt < maxRetries && this.isRetryable(error)) {
          this.handlers.forEach(h =>
            h(error, `${context}(重试 ${attempt + 1}/${maxRetries})`)
          );
          await new Promise(r => setTimeout(r, retryDelay * (attempt + 1)));
          continue;
        }

        this.handlers.forEach(h => h(error, context));
        return null;
      }
    }
    return null;
  }

  /** 判断错误是否可重试 */
  private isRetryable(err: Error): boolean {
    const nonRetryablePatterns = [
      'AbortError', 'ValidationError', 'PermissionDenied',
      'SyntaxError', 'TypeError',
    ];
    return !nonRetryablePatterns.some(p => err.name.includes(p) || err.message.includes(p));
  }

  /** 在 TUI 中使用 —— 显示友好的错误信息 */
  static forTUI(screen: Widgets.Screen, statusBar: Widgets.BoxElement): ErrorBoundary {
    const boundary = new ErrorBoundary();
    boundary.addHandler((err, context) => {
      statusBar.setContent(
        ` {red-fg}❌ [${context}] ${err.message.slice(0, 40)}{/red-fg}`
      );
      screen.render();
    });
    return boundary;
  }
}
```

### 10.10.2 分层错误处理

LLM TUI 的错误捕获应在四个层面进行：

```
层面 1: 进程级         → 全局 uncaughtException / unhandledRejection
层面 2: 会话级         → 单次对话的错误边界（重试、恢复）
层面 3: 组件级         → 单个 UI 组件的错误隔离
层面 4: 操作级         → 单个操作 try/catch
```

```typescript
// ── 层面 2: 会话级错误边界 ──
class SessionErrorBoundary {
  private retryManager = new RetryManager({ maxRetries: 3, retryDelay: 1000, showRetryButton: true });
  private currentUserMessage: string | null = null;

  /** 执行对话并处理错误 */
  async executeSession(userMsg: string): Promise<void> {
    this.currentUserMessage = userMsg;

    try {
      await this.streamResponse(userMsg);
    } catch (err) {
      await this.handleSessionError(err);
    }
  }

  private async handleSessionError(err: unknown): Promise<void> {
    if (err instanceof Error && err.name === 'AbortError') {
      this.showSystemMessage('⏹️ 生成已取消');
      return;
    }

    // 显示错误卡片
    const errMsg = err instanceof Error ? err.message : String(err);
    this.showErrorCard(errMsg);

    // 自动重试（网络错误等可恢复错误）
    if (this.retryManager.canRetry() && this.isRecoverable(err)) {
      const remaining = this.retryManager.remainingRetries();
      this.showSystemMessage(`⏳ ${remaining} 秒后自动重试...`);
      await delay(3000);
      await this.executeSession(this.currentUserMessage!);
    }
  }

  private isRecoverable(err: unknown): boolean {
    if (err instanceof TypeError && err.message.includes('fetch')) return true;
    if (err instanceof Error && err.message.includes('timeout')) return true;
    if (err instanceof Error && err.message.includes('429')) return true;
    return false;
  }
}

// ── 层面 3: 组件级错误隔离 ──
class ComponentErrorBoundary {
  private fallbackContent: string;

  constructor(fallback: string = '{red-fg}[组件渲染错误]{/red-fg}') {
    this.fallbackContent = fallback;
  }

  /** 安全渲染组件 */
  renderSafe(renderFn: () => string): string {
    try {
      return renderFn();
    } catch (err) {
      console.error('组件渲染错误:', err);
      return this.fallbackContent;
    }
  }
}

// 使用示例
const cardBoundary = new ComponentErrorBoundary(
  '{red-fg}⚠️ 该消息无法显示{/red-fg}'
);

messageBox.setContent(cardBoundary.renderSafe(() => {
  return renderMarkdown(llmResponse);
}));
```

### 10.10.3 在 llm-chat.ts 中的应用

```typescript
// 在 ChatTUI 中使用错误边界
class ChatTUI {
  private boundary = new ErrorBoundary();
  private sessionBoundary = new SessionErrorBoundary();

  constructor() {
    // 设置全局边界
    this.boundary = ErrorBoundary.forTUI(this.screen, this.statusBar);
  }

  async sendMessage(text: string): Promise<void> {
    if (this.state !== 'idle') {
      this.flashInputBorder();
      return;
    }

    this.state = 'thinking';
    this.setThinkingStatus();

    // 使用会话级错误边界
    await this.sessionBoundary.executeSession(text);

    this.state = 'idle';
    this.setIdleStatus();
    this.inputBox.focus();
  }
}
```

## 10.11 跨平台终端恢复（Cross-Platform Safety）

不同操作系统需要不同的终端恢复策略。一个忽略平台差异的 TUI 在某些系统上可能导致终端无法恢复。

### 10.11.1 平台检测与差异化恢复

```typescript
type TerminalPlatform = 'win32' | 'darwin' | 'linux' | 'unknown';

interface TerminalState {
  rawMode: boolean;
  echo: boolean;
  cursorVisible: boolean;
  colorMode: string;
}

class PlatformTerminalRestorer {
  private static savedState: Partial<TerminalState> = {};

  /** 保存当前终端状态 */
  static saveState(): void {
    try {
      this.savedState = {
        rawMode: process.stdin.isRaw ?? false,
        cursorVisible: true,
        colorMode: '256',
      };
    } catch { /* 静默失败 */ }
  }

  /** 平台特定清理 */
  static cleanup(): void {
    // ── 通用 ANSI 恢复（所有平台） ──
    this.restoreAnsi();

    // ── 平台特定恢复 ──
    switch (process.platform) {
      case 'win32':
        this.cleanupWin32();
        break;
      case 'darwin':
        this.cleanupDarwin();
        break;
      case 'linux':
        this.cleanupLinux();
        break;
      default:
        this.cleanupGeneric();
    }

    // ── 最终验证 ──
    this.verifyRestored();
  }

  /** ANSI 转义序列恢复（通用） */
  private static restoreAnsi(): void {
    try {
      process.stdout.write('\x1b[2J\x1b[H');     // 清屏
      process.stdout.write('\x1b[?25h');          // 显示光标
      process.stdout.write('\x1b[0m');            // 重置颜色
      process.stdout.write('\x1b]104\x07');       // 重置调色板
      // 禁用鼠标事件
      process.stdout.write('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l');
    } catch { /* 忽略 */ }
  }

  /** Windows 平台恢复 */
  private static cleanupWin32(): void {
    try {
      // Windows 终端通常不需要 stty
      process.stdin.setRawMode?.(false);
      process.stdin.pause();

      // 恢复控制台标题
      process.title = 'Windows Terminal';

      // 确保回显开启
      const { execSync } = require('child_process');
      execSync('echo on', { stdio: 'ignore', timeout: 1000 });

      // 对于 Git Bash / WSL，尝试 stty
      if (process.env.SHELL?.includes('bash')) {
        execSync('stty sane 2>/dev/null || true', { stdio: 'ignore', timeout: 1000 });
      }
    } catch { /* Windows 下某些命令可能失败 */ }
  }

  /** macOS 平台恢复 */
  private static cleanupDarwin(): void {
    try {
      const { execSync } = require('child_process');
      // macOS Terminal.app 需要完整恢复
      execSync('stty sane', { stdio: 'ignore', timeout: 1000 });
      execSync('stty echo', { stdio: 'ignore', timeout: 1000 });
      execSync('stty icanon', { stdio: 'ignore', timeout: 1000 });

      // 恢复备用屏幕（如果使用了 smcup）
      process.stdout.write('\x1b[?1049l');
    } catch { /* 忽略 */ }
  }

  /** Linux 平台恢复 */
  private static cleanupLinux(): void {
    try {
      // Linux 终端通常需要 stty + terminfo
      const { execSync } = require('child_process');
      execSync('stty sane', { stdio: 'ignore', timeout: 1000 });
      execSync('stty echo icanon', { stdio: 'ignore', timeout: 1000 });

      // 恢复备用屏幕
      process.stdout.write('\x1b[?1049l');

      // 确保 tty 模式正确
      if (process.stdin.isTTY) {
        process.stdin.setRawMode?.(false);
        process.stdin.pause();
      }
    } catch { /* 忽略 */ }
  }

  /** 通用平台恢复（后备方案） */
  private static cleanupGeneric(): void {
    try {
      process.stdin.setRawMode?.(false);
      process.stdin.pause();

      const { execSync } = require('child_process');
      execSync('stty sane 2>/dev/null', { stdio: 'ignore', timeout: 1000 });
    } catch { /* 忽略 */ }
  }

  /** 验证终端是否已完全恢复 */
  static verifyRestored(): boolean {
    try {
      // 尝试写入一个简单序列并验证
      process.stdout.write('\x1b[6n'); // 请求光标位置
      return true;
    } catch {
      console.error('警告: 终端可能未完全恢复');
      return false;
    }
  }

  /**
   * 注册到全局错误处理器
   * 使用: PlatformTerminalRestorer.registerGlobalHandler()
   */
  static registerGlobalHandler(): void {
    this.saveState();

    const cleanup = () => this.cleanup();

    process.on('exit', cleanup);
    process.on('SIGINT', () => { cleanup(); process.exit(0); });
    process.on('SIGTERM', () => { cleanup(); process.exit(0); });
    process.on('uncaughtException', (err) => {
      cleanup();
      console.error('\n❌ 未捕获异常:', err.message);
      process.exit(1);
    });
    process.on('unhandledRejection', (reason) => {
      cleanup();
      console.error('\n❌ 未处理的 Promise 拒绝:', reason);
      process.exit(1);
    });
  }
}
```

### 10.11.2 平台差异速查

| 平台 | 特点 | 风险点 | 推荐策略 |
|------|------|--------|---------|
| **Windows** | Windows Terminal / PowerShell | ConPTY 兼容性、编码问题 | `setRawMode(false)` + 标题恢复 |
| **macOS** | Terminal.app / iTerm2 | 备用屏幕、色彩配置 | `stty sane` + `\x1b[?1049l` |
| **Linux** | GNOME Terminal / xterm | 鼠标事件、原始模式 | `stty sane icanon` + 信号处理 |
| **WSL** | 混合环境 | 两种平台的问题叠加 | 同时尝试 Win32 和 Unix 恢复 |

### 10.11.3 完整的终端恢复集成

在 `llm-chat.ts` 中集成跨平台恢复：

```typescript
// ── 应用启动时 ──
class ChatTUI {
  constructor() {
    // 保存终端状态
    PlatformTerminalRestorer.saveState();

    // 注册全局错误处理（包含跨平台恢复）
    PlatformTerminalRestorer.registerGlobalHandler();

    // 创建 blessed 屏幕
    this.screen = blessed.screen({
      smartCSR: true,
      title: 'AI Chat TUI',
    });

    // ... 其他初始化 ...
  }

  private cleanupTerminal(): void {
    // 退出时恢复终端
    PlatformTerminalRestorer.cleanup();
  }
}
```

### 10.11.4 测试终端恢复

```typescript
/**
 * 终端恢复测试脚本
 * 在开发或 CI 中验证终端恢复是否正常工作
 */
function testTerminalRecovery(): boolean {
  const tests = [
    { name: '光标恢复', fn: () => process.stdout.write('\x1b[?25h') },
    { name: '颜色重置', fn: () => process.stdout.write('\x1b[0m') },
    { name: '原始模式关闭', fn: () => process.stdin.setRawMode?.(false) },
  ];

  let allPassed = true;
  for (const test of tests) {
    try {
      test.fn();
      console.log(`  ✓ ${test.name}`);
    } catch (err) {
      console.error(`  ✗ ${test.name}: ${err}`);
      allPassed = false;
    }
  }

  return allPassed;
}
```

## 10.12 输入验证（Input Validation）

完整输入净化管道，涵盖用户输入和 AI 输出的验证。

### 10.12.1 用户输入验证

```typescript
interface ValidationResult {
  valid: boolean;
  sanitized: string;
  errors: string[];
  warnings: string[];
}

class InputValidator {
  private static readonly MAX_LENGTH = 2000;
  private static readonly MIN_LENGTH = 1;

  /**
   * 完整输入验证管道
   */
  static validate(input: string): ValidationResult {
    const result: ValidationResult = {
      valid: true,
      sanitized: input.trim(),
      errors: [],
      warnings: [],
    };

    // 1. 长度验证
    const trimmed = input.trim();
    if (trimmed.length < this.MIN_LENGTH) {
      result.errors.push('输入不能为空');
      result.valid = false;
      return result;
    }

    if (trimmed.length > this.MAX_LENGTH) {
      result.errors.push(`输入过长（${trimmed.length} 字符），最大 ${this.MAX_LENGTH} 字符`);
      result.valid = false;
      return result;
    }

    // 2. ANSI 序列检测
    if (/[\x1b\x9b]/.test(trimmed)) {
      result.warnings.push('输入包含 ANSI 转义序列，已自动移除');
      result.sanitized = trimmed.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '')
                                 .replace(/\x1b\][^\x1b]*(\x1b\\|\x07)/g, '');
    }

    // 3. 危险模式检测（SQL 注入、XSS 等）
    const dangerousPatterns = [
      { pattern: /<script[\s>]/i, warning: '包含脚本标签' },
      { pattern: /['"];\s*DROP\s+TABLE/i, warning: '包含可疑 SQL 片段' },
      { pattern: /\x00/, warning: '包含空字节' },
      { pattern: /(?:;|\||`|\$\()\s*(?:rm|del|format|shutdown)/i, warning: '包含可疑命令' },
    ];

    for (const { pattern, warning } of dangerousPatterns) {
      if (pattern.test(result.sanitized)) {
        result.warnings.push(warning);
      }
    }

    // 4. 控制字符清理
    result.sanitized = result.sanitized.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '');

    // 5. 空白规范化（多个空格保留，但换行规范化）
    result.sanitized = result.sanitized.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

    return result;
  }

  /**
   * 检查输入是否安全（快速检测）
   */
  static isSafe(input: string): boolean {
    return !InputSanitizer.isPotentialAttack(input);
  }
}
```

### 10.12.2 输入验证集成

```typescript
class ChatTUI {
  private sendMessage(text: string): void {
    // 完整验证
    const validation = InputValidator.validate(text);

    if (!validation.valid) {
      // 显示验证错误
      this.showSystemMessage(`{red-fg}⚠️ ${validation.errors[0]}{/red-fg}`);
      this.flashInputBorder();
      return;
    }

    if (validation.warnings.length > 0) {
      // 有警告但继续（记录日志）
      console.warn('输入警告:', validation.warnings);
    }

    // 使用净化后的输入
    this.processMessage(validation.sanitized);
  }
}
```

### 10.12.3 AI 输出安全过滤

```typescript
class OutputFilter {
  /**
   * 过滤 AI 输出中的不安全内容
   * 保留 Markdown 格式，移除控制字符
   */
  static filterOutput(text: string): string {
    let filtered = text;

    // 移除控制字符（保留 \n \t）
    filtered = filtered.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '');

    // 截断过长输出
    const MAX_OUTPUT = 10000;
    if (filtered.length > MAX_OUTPUT) {
      filtered = filtered.slice(0, MAX_OUTPUT) +
        '\n\n{yellow-fg}... 输出过长，已截断{/yellow-fg}';
    }

    // 检测并移除潜在的终端注入
    filtered = filtered.replace(/\x1b\]52;/g, ''); // 剪贴板操作
    filtered = filtered.replace(/\x1b\]8;;/g, '');  // 超链接注入

    return filtered;
  }

  /**
   * 检测输出是否包含敏感信息
   */
  static containsSensitiveData(text: string): boolean {
    const patterns = [
      /sk-[a-zA-Z0-9]{20,}/,      // OpenAI API Key
      /sk-ant-[a-zA-Z0-9]{20,}/,  // Anthropic API Key
      /AKIA[0-9A-Z]{16}/,         // AWS Access Key
      /-----BEGIN (RSA |EC )?PRIVATE KEY-----/,  // 私钥
    ];
    return patterns.some(p => p.test(text));
  }
}
```

## 10.13 用户反馈循环（User Feedback Loop）

错误处理不应该只是静默捕获，而应该让用户参与恢复决策：

```
错误发生
  ↓
显示错误卡片（红色，清晰的信息）
  ↓  
提供恢复选项:
  ├── 重试 (R键)     — 重新发送相同请求
  ├── 修改 (Esc键)   — 清空输入框重新输入
  └── 忽略 (继续)    — 继续对话，跳过错误
  ↓
用户选择后:
  ├── 重试 → 清空错误卡片 → 重新发送
  ├── 修改 → 聚焦输入框 → 等待输入
  └── 忽略 → 保持错误卡片在历史中 → 继续
```

```typescript
interface RetryOptions {
  /** 错误重试次数限制 */
  maxRetries: number;
  /** 重试延迟 (ms) */
  retryDelay: number;
  /** 是否显示重试按钮 */
  showRetryButton: boolean;
  /** 重试回调 */
  onRetry?: () => void;
  /** 重试后回调 */
  onSuccess?: () => void;
  /** 放弃回调 */
  onAbandon?: () => void;
}

class RetryManager {
  private retryCount = 0;
  private readonly maxRetries: number;
  private readonly retryDelay: number;
  private onRetry?: () => void;
  private onSuccess?: () => void;
  private onAbandon?: () => void;

  constructor(opts: RetryOptions) {
    this.maxRetries = opts.maxRetries;
    this.retryDelay = opts.retryDelay;
    this.onRetry = opts.onRetry;
    this.onSuccess = opts.onSuccess;
    this.onAbandon = opts.onAbandon;
  }

  /** 是否可以重试 */
  canRetry(): boolean {
    return this.retryCount < this.maxRetries;
  }

  /** 剩余重试次数 */
  remainingRetries(): number {
    return this.maxRetries - this.retryCount;
  }

  /** 执行重试 */
  async retry<T>(fn: () => Promise<T>): Promise<T> {
    this.retryCount++;
    this.onRetry?.();
    await delay(this.retryDelay * this.retryCount); // 指数退避
    try {
      const result = await fn();
      this.onSuccess?.();
      return result;
    } catch (err) {
      if (!this.canRetry()) {
        this.onAbandon?.();
      }
      throw err;
    }
  }

  /** 重置计数器 */
  reset(): void {
    this.retryCount = 0;
  }
}

// 带用户反馈的指数退避重试
async function retryWithFeedback<T>(
  fn: () => Promise<T>,
  onRetry: (attempt: number, delay: number) => void,
  options: { maxRetries?: number; baseDelay?: number } = {}
): Promise<T> {
  const { maxRetries = 3, baseDelay = 1000 } = options;

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (err) {
      if (attempt === maxRetries) throw err;

      const delay = baseDelay * Math.pow(2, attempt - 1); // 指数退避
      onRetry(attempt, delay);
      await new Promise(r => setTimeout(r, delay));
    }
  }

  throw new Error('重试耗尽');
}
```

### 10.13.1 多选项错误卡片

```typescript
/**
 * 多选项错误卡片 —— 提供多个恢复路径
 */
function showErrorWithOptions(
  parent: blessed.Widgets.BoxElement,
  screen: blessed.Widgets.Screen,
  context: { message: string; originalInput: string }
): void {
  const card = blessed.box({
    parent,
    top: 0, left: 0,
    width: '100%-2',
    height: 7,
    content: [
      `{red-fg}┌─ 操作失败 ──────────────────────────────┐{/red-fg}`,
      `{red-fg}│{/red-fg}`,
      `{red-fg}│{/red-fg}  {white-fg}${context.message}{/white-fg}`,
      `{red-fg}│{/red-fg}`,
      `{red-fg}│{/red-fg}  {yellow-fg}[R] 重试   [E] 编辑   [I] 忽略{/yellow-fg}`,
      `{red-fg}└──────────────────────────────────────────┘{/red-fg}`,
    ].join('\n'),
    tags: true,
    style: { bg: '#1a1a1a' },
  });

  // 键盘绑定
  screen.key(['r', 'R'], () => {
    card.detach();
    screen.render();
    context.originalInput; // 触发重试
  });

  screen.key(['e', 'E'], () => {
    card.detach();
    screen.render();
    // 聚焦输入框让用户修改
  });

  screen.key(['i', 'I'], () => {
    card.detach();
    screen.render();
    // 忽略错误继续
  });
}
```

## 10.14 优雅降级（Graceful Degradation）

当某些功能不可用时，应提供合理的降级体验。

### 10.14.1 特性检测与降级

```typescript
interface CapabilityMap {
  hyperlinks: boolean;
  emoji: boolean;
  trueColor: boolean;
  mouse: boolean;
  clipboard: boolean;
  unicode: boolean;
}

/**
 * 检测终端能力并存储
 */
class CapabilityDetector {
  private static capabilities: CapabilityMap | null = null;

  static detect(): CapabilityMap {
    if (this.capabilities) return this.capabilities;

    const term = process.env.TERM || '';
    const termProgram = process.env.TERM_PROGRAM || '';

    this.capabilities = {
      hyperlinks: !!(process.env.WT_SESSION || term.includes('kitty') || term.includes('alacritty')),
      emoji: !!(process.env.WT_SESSION || termProgram === 'iTerm.app' || termProgram === 'WezTerm' || term.includes('kitty')),
      trueColor: !!(term.includes('truecolor') || term.includes('24bit') || process.env.COLORTERM === 'truecolor'),
      mouse: !!(process.stdin.isTTY && (term.includes('xterm') || term.includes('kitty'))),
      clipboard: !!(term.includes('kitty') || termProgram === 'iTerm.app'),
      unicode: (() => {
        try { return Buffer.from('测试', 'utf-8').length === 3; }
        catch { return false; }
      })(),
    };

    return this.capabilities;
  }

  /** 获取降级配置 */
  static getDegradationConfig(): Record<string, any> {
    const caps = this.detect();
    return {
      useHyperlinks: caps.hyperlinks,
      useEmoji: caps.emoji,
      useTrueColor: caps.trueColor,
      useMouse: caps.mouse,
      useAltScreen: caps.unicode, // 备用屏幕需要 Unicode 支持
    };
  }
}
```

### 10.14.2 降级渲染组件

```typescript
/**
 * 可降级的 Markdown 渲染器
 * 根据终端能力图自动调整渲染策略
 */
class DegradableRenderer {
  private config: Record<string, any>;

  constructor() {
    this.config = CapabilityDetector.getDegradationConfig();
  }

  render(text: string): string {
    let result = text;

    // 根据能力选择渲染策略
    if (!this.config.useHyperlinks) {
      // 降级: OSC 8 超链接不可用，显示为纯文本
      result = this.degradeLinks(result);
    }

    if (!this.config.useEmoji) {
      // 降级: Emoji 不可用
      result = EmojiHandler.processEmojis(result);
    }

    if (!this.config.useTrueColor) {
      // 降级: 真彩色不可用，使用 16 色
      result = this.degradeColors(result);
    }

    return result;
  }

  private degradeLinks(text: string): string {
    // 将 OSC 8 链接降级为带下划线的 URL 文本
    return text.replace(
      /\x1b]8;;[^\x1b]+\x1b\\[^\x1b]+\x1b]8;;\x1b\\/g,
      (match) => {
        const urlMatch = match.match(/\x1b]8;;([^\x1b]+)\x1b\\([^\x1b]+)/);
        if (urlMatch) {
          const [_, url, displayText] = urlMatch;
          return `{underline}${displayText}{/underline} ({cyan-fg}${url}{/cyan-fg})`;
        }
        return match;
      }
    );
  }

  private degradeColors(text: string): string {
    // 将 {#hex-fg} 降级为标准 16 色标签
    return text
      .replace(/\{[a-f0-9]{6}-fg\}/g, '{white-fg}')
      .replace(/\{[a-f0-9]{6}-bg\}/g, '{black-bg}');
  }
}

// 使用
const renderer = new DegradableRenderer();
messageBox.setContent(renderer.render(llmResponse));
```

### 10.14.3 降级策略矩阵

| 功能 | 全支持 | 部分支持 | 不支持 |
|------|--------|---------|--------|
| **超链接** | OSC 8 可点击链接 | 下划线 + URL 文本 | 纯 URL 文本 |
| **Emoji** | 原生 Emoji | 部分 Emoji | 文本回退 `[OK]` |
| **真彩色** | 24-bit 颜色 | 256 色近似 | 标准 16 色 |
| **鼠标** | 点击交互 | 键盘替代 | 纯键盘 |
| **Unicode** | 全 Unicode | ASCII 降级 | 基本 ASCII |

## 10.15 错误报告与遥测（Error Reporting / Telemetry）

为 TUI 应用添加本地错误记录和可选的匿名错误上报。

### 10.15.1 本地错误日志

```typescript
interface ErrorRecord {
  timestamp: string;
  level: 'error' | 'warn' | 'info';
  context: string;
  message: string;
  stack?: string;
  platform: string;
}

class ErrorLogger {
  private static logs: ErrorRecord[] = [];
  private static readonly MAX_LOGS = 100;

  /**
   * 记录错误到本地缓冲区
   */
  static log(level: ErrorRecord['level'], context: string, err: unknown): void {
    const record: ErrorRecord = {
      timestamp: new Date().toISOString(),
      level,
      context,
      message: err instanceof Error ? err.message : String(err),
      stack: err instanceof Error ? err.stack : undefined,
      platform: `${process.platform} ${process.arch}`,
    };

    this.logs.push(record);

    // 限制日志条数
    if (this.logs.length > this.MAX_LOGS) {
      this.logs.shift();
    }

    // 控制台输出（开发环境）
    if (process.env.NODE_ENV === 'development') {
      console.error(`[${record.timestamp}] [${level.toUpperCase()}] [${context}] ${record.message}`);
    }
  }

  /**
   * 导出日志（可用于保存到文件）
   */
  static export(): ErrorRecord[] {
    return [...this.logs];
  }

  /**
   * 获取错误统计
   */
  static getStats(): { total: number; byLevel: Record<string, number>; byContext: Record<string, number> } {
    const byLevel: Record<string, number> = {};
    const byContext: Record<string, number> = {};

    for (const record of this.logs) {
      byLevel[record.level] = (byLevel[record.level] || 0) + 1;
      byContext[record.context] = (byContext[record.context] || 0) + 1;
    }

    return { total: this.logs.length, byLevel, byContext };
  }
}

// 使用
ErrorLogger.log('error', 'send-message', new Error('API 超时'));
ErrorLogger.log('warn', 'input-validation', '输入包含特殊字符');
```

### 10.15.2 错误报告结构

```typescript
interface ErrorReport {
  app: {
    name: string;
    version: string;
    uptime: number;
  };
  environment: {
    platform: string;
    nodeVersion: string;
    terminal: string;
    terminalWidth: number;
  };
  error: {
    message: string;
    context: string;
    timestamp: string;
  };
  recentLogs: ErrorRecord[];
}

/**
 * 生成错误报告（脱敏后）
 */
function generateErrorReport(context: string, err: Error): ErrorReport {
  return {
    app: {
      name: 'ai-chat-tui',
      version: process.env.npm_package_version || '1.0.0',
      uptime: process.uptime(),
    },
    environment: {
      platform: process.platform,
      nodeVersion: process.version,
      terminal: process.env.TERM || 'unknown',
      terminalWidth: process.stdout.columns || 80,
    },
    error: {
      message: err.message,
      context,
      timestamp: new Date().toISOString(),
    },
    recentLogs: ErrorLogger.export().slice(-10), // 最近 10 条
  };
}
```

### 10.15.3 遥测注意事项

```typescript
/**
 * 匿名遥测管理器
 * 默认关闭，需要用户明确同意
 */
class TelemetryManager {
  private static enabled = false;
  private static readonly STORAGE_KEY = 'ai-chat-tui-telemetry';

  /** 启用/禁用遥测 */
  static setEnabled(enabled: boolean): void {
    this.enabled = enabled;
    // 保存用户偏好
    try {
      localStorage?.setItem(this.STORAGE_KEY, String(enabled));
    } catch { /* 非浏览器环境 */ }
  }

  /** 是否启用 */
  static isEnabled(): boolean {
    return this.enabled;
  }

  /** 上报匿名错误（不包含敏感信息） */
  static async reportError(context: string, err: Error): Promise<void> {
    if (!this.enabled) return;

    try {
      const report = generateErrorReport(context, err);

      // 脱敏处理: 移除文件路径中的用户名
      report.error.message = this.sanitizePaths(report.error.message);

      // TODO: 替换为实际上报端点
      // await fetch('https://telemetry.example.com/error', {
      //   method: 'POST',
      //   body: JSON.stringify(report),
      //   headers: { 'Content-Type': 'application/json' },
      // });
    } catch {
      // 静默失败 —— 不上报错误的上报错误
    }
  }

  private static sanitizePaths(text: string): string {
    // 移除用户主目录路径
    return text.replace(/\/home\/[^/]+/g, '/home/[user]')
               .replace(/\/Users\/[^/]+/g, '/Users/[user]');
  }
}

// 使用示例
TelemetryManager.reportError('stream-error', new Error('连接中断'));

// 应用启动时检查用户偏好
if (TelemetryManager.isEnabled()) {
  console.log('匿名遥测已启用，仅上报匿名错误数据');
}
```

### 10.15.4 遥测透明度原则

```
□ 默认关闭，用户明确同意后才启用
□ 不上报任何对话内容或 API Key
□ 只上报: 错误类型、终端环境、Node 版本
□ 提供查看已收集数据的方式
□ 提供一键清除所有本地日志
□ 文件路径中的用户名会被自动脱敏
```

## 10.16 操作安全（Operational Safety）

当 LLM 可以调用工具时，需要防止危险操作。

### 10.16.1 危险操作检测

```typescript
interface ToolCall {
  tool: string;
  args: Record<string, any>;
}

interface SafetyCheckResult {
  safe: boolean;
  riskLevel: 'low' | 'medium' | 'high' | 'critical';
  warnings: string[];
  requiresConfirmation: boolean;
}

class OperationalSafety {
  // 高危工具列表 —— 执行前必须确认
  private static readonly DANGEROUS_TOOLS = new Set([
    'rm', 'del', 'format', 'shutdown', 'reboot',
    'exec', 'eval', 'spawn', 'sudo', 'chmod',
    'drop table', 'drop database', 'truncate',
  ]);

  // 高危参数模式
  private static readonly DANGEROUS_PATTERNS = [
    /\brm\s+-rf\b/i,
    /\bchmod\s+777\b/i,
    /\bchown\b/i,
    />(?:\s*>)?\s*\/dev\//,
    /:\/\/[^@]+:[^@]+@/,   // URL 中的明文密码
  ];

  /**
   * 检查工具调用的安全性
   */
  static checkToolCall(toolCall: ToolCall): SafetyCheckResult {
    const result: SafetyCheckResult = {
      safe: true,
      riskLevel: 'low',
      warnings: [],
      requiresConfirmation: false,
    };

    // 1. 工具名安全检查
    const toolLower = toolCall.tool.toLowerCase();
    if (this.DANGEROUS_TOOLS.has(toolLower)) {
      result.warnings.push(`高危工具: ${toolCall.tool}`);
      result.riskLevel = 'critical';
      result.requiresConfirmation = true;
    }

    // 2. 参数安全检查
    const argsStr = JSON.stringify(toolCall.args);
    for (const pattern of this.DANGEROUS_PATTERNS) {
      if (pattern.test(argsStr)) {
        result.warnings.push(`参数包含危险模式: ${pattern.source}`);
        result.riskLevel = 'high';
        result.requiresConfirmation = true;
      }
    }

    // 3. 敏感参数检测
    if (toolCall.args['command'] || toolCall.args['cmd'] || toolCall.args['script']) {
      result.warnings.push('工具包含动态命令执行参数');
      result.riskLevel = Math.max(result.riskLevel === 'critical' ? 'critical' : 'high', 'high') as any;
      result.requiresConfirmation = true;
    }

    // 4. 结果汇总
    result.safe = !result.requiresConfirmation;

    return result;
  }

  /**
   * 文件系统安全防护
   */
  static validateFilePath(path: string): { safe: boolean; resolved: string; error?: string } {
    const resolved = path.replace(/\\/g, '/');

    // 禁止访问的系统路径
    const blockedPaths = [
      '/etc/passwd', '/etc/shadow', '/etc/sudoers',
      '/root/', '/sys/', '/proc/',
      process.env.HOME + '/.ssh/',
    ];

    for (const blocked of blockedPaths) {
      if (resolved.startsWith(blocked) || resolved.includes(blocked)) {
        return { safe: false, resolved, error: `禁止访问系统文件: ${blocked}` };
      }
    }

    return { safe: true, resolved };
  }
}
```

### 10.16.2 用户确认流程

```typescript
/**
 * 危险操作确认对话框
 */
function showConfirmationDialog(
  screen: blessed.Widgets.Screen,
  toolCall: ToolCall,
  warnings: string[],
  onConfirm: () => void,
  onReject: () => void
): void {
  const dialog = blessed.box({
    parent: screen,
    top: 'center',
    left: 'center',
    width: 60,
    height: 8 + warnings.length,
    content: [
      '{bold}{red-fg}⚠️ 危险工具调用确认{/red-fg}{/bold}',
      '',
      `工具: {bold}${toolCall.tool}{/bold}`,
      `参数: {bold}${JSON.stringify(toolCall.args)}{/bold}`,
      ...warnings.map(w => `  {yellow-fg}⚠ ${w}{/yellow-fg}`),
      '',
      '{yellow-fg}[Y] 确认执行  [N] 拒绝执行  [V] 查看详情{/yellow-fg}',
    ].join('\n'),
    tags: true,
    border: { type: 'line', fg: 'red' },
    style: { bg: '#1a1a1a', fg: 'white' },
    keys: true,
    vi: true,
  });

  screen.append(dialog);
  screen.render();

  // 键盘处理（一次性绑定）
  const handler = (ch: any, key: any) => {
    if (key.name === 'y' || key.name === 'Y') {
      dialog.detach();
      screen.key('y', () => {});
      screen.key('n', () => {});
      screen.key('v', () => {});
      onConfirm();
    } else if (key.name === 'n' || key.name === 'N') {
      dialog.detach();
      screen.key('y', () => {});
      screen.key('n', () => {});
      screen.key('v', () => {});
      onReject();
    } else if (key.name === 'v' || key.name === 'V') {
      // 显示详细工具参数
      showToolDetails(screen, toolCall);
    }
    screen.render();
  };

  screen.key(['y', 'n', 'v'], handler);
}
```

### 10.16.3 安全配置

```typescript
interface SafetyConfig {
  /** 是否启用操作安全检查 */
  enabled: boolean;
  /** 需要确认的高危工具列表 */
  dangerousTools: string[];
  /** 是否自动拒绝高危操作 */
  autoReject: boolean;
  /** 允许访问的文件路径前缀 */
  allowedPaths: string[];
  /** API Key 白名单（可选） */
  allowedApiKeys?: string[];
}

const DEFAULT_SAFETY_CONFIG: SafetyConfig = {
  enabled: true,
  dangerousTools: ['rm', 'del', 'format', 'shutdown', 'reboot', 'sudo', 'chmod'],
  autoReject: false, // 默认询问用户
  allowedPaths: [process.cwd(), '/tmp'],
};

class SafetyManager {
  private config: SafetyConfig;

  constructor(config: Partial<SafetyConfig> = {}) {
    this.config = { ...DEFAULT_SAFETY_CONFIG, ...config };
  }

  async checkAndConfirm(
    toolCall: ToolCall,
    screen: blessed.Widgets.Screen
  ): Promise<boolean> {
    if (!this.config.enabled) return true;

    const check = OperationalSafety.checkToolCall(toolCall);

    if (!check.requiresConfirmation) return true;

    if (this.config.autoReject) {
      return false;
    }

    // 要求用户确认
    return new Promise((resolve) => {
      showConfirmationDialog(
        screen,
        toolCall,
        check.warnings,
        () => resolve(true),
        () => resolve(false)
      );
    });
  }
}
```

---

**实践：** 修改 `llm-chat.ts`，添加完整的安全工具调用确认机制，当 LLM 请求调用高危工具时弹出确认对话框。

**上一章：** [第九章：Markdown 渲染与富文本展示](09-markdown-rendering.md)

**下一步：** [第十一章：MCP 协议与工具集成](11-mcp-integration.md)
