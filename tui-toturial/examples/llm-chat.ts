#!/usr/bin/env npx tsx
/**
 * llm-chat.ts — 大模型对话 TUI 完整实现
 *
 * 演示功能:
 *   - 流式输出 / 打字机效果
 *   - 工具调用 (Function Calling) UI（三态卡片）
 *   - 思考状态指示 (Spinner 动画)
 *   - 错误状态展示（红色卡片）
 *   - 多场景对话模拟（天气、计算、代码、错误、默认）
 *   - 状态管理（6 种状态的状态机）
 *   - 键盘导航与快捷键
 *
 * 运行方式:
 *   cd examples && npm install && npx tsx llm-chat.ts
 *
 * 快捷键:
 *   Ctrl+S    — 发送消息
 *   Ctrl+Q    — 退出程序
 *   Ctrl+C    — 取消生成（预留接口）
 *   ↑/↓       — 滚动聊天区域
 *   PageUp/Dn — 快速滚动
 *   Esc       — 清空输入框
 *
 * 对话场景（在输入框中输入以下关键词触发）:
 *   🌤️  天气查询  — 说 "北京天气" 触发工具调用
 *   🧮  数学计算  — 说 "计算 42*7" 触发工具调用
 *   💻  代码生成  — 说 "写一段代码" 查看代码输出
 *   ❌  错误模拟  — 说 "制造错误" 查看错误 UI
 *   💬  自由对话  — 任何其他内容演示流式输出
 *
 * 注意: 如果 Ctrl+S 在您的终端中被拦截（XOFF 流控），请运行:
 *   stty -ixon
 */

import * as blessed from "blessed";
import { Widgets } from "blessed";

// ============================================================
// 全局: 终端安全退出装置
// 原理: 无论程序如何退出（正常、异常、信号），都恢复终端到原始状态
// 这是 TUI 程序最重要的安全措施，防止用户在程序崩溃后"卡"在终端里
// ============================================================

/**
 * 完整终端恢复 —— 在任何退出路径上被调用
 */
function resetTerminal(): void {
  try {
    // 1. 清屏
    process.stdout.write('\x1b[2J\x1b[H');
    // 2. 恢复光标
    process.stdout.write('\x1b[?25h');
    // 3. 禁用鼠标事件
    process.stdout.write('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l');
    // 4. 重置颜色和样式
    process.stdout.write('\x1b[0m');
    // 5. 退出原始模式
    if (process.stdin.isTTY) {
      process.stdin.setRawMode(false);
    }
    process.stdin.pause();
  } catch {
    // 清理过程出错也尽量不抛出
  }
}

/** 安装全局退出守卫 */
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

// 安装退出守卫
installExitGuard();

// ============================================================
// 类型定义
// ============================================================

/** LLM 流式事件 —— 从 Mock LLM 发出的所有可能事件类型 */
type LLMEvent =
  | { type: "thinking" }
  | { type: "text"; content: string }
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; name: string; result: string }
  | { type: "error"; message: string }
  | { type: "done" };

/** 应用状态 —— 6 种互斥状态 */
type AppState = "idle" | "thinking" | "streaming" | "tool_call" | "tool_result" | "error";

/** 聊天消息结构 */
interface ChatMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  toolCall?: { name: string; args: Record<string, unknown>; result?: string };
  error?: string;
}

// ============================================================
// Markdown 渲染（简化版 —— 用于 AI 回复中的代码块和格式）
// ============================================================

/**
 * 简易 Markdown 渲染：在 blessed 标签中将 Markdown 转换为格式化文本
 * 支持：代码块、行内代码、粗体、斜体、列表、标题
 */
function renderMarkdown(text: string): string {
  let result = text;

  // 先转义花括号
  result = result.replace(/\{/g, "&lcub;").replace(/\}/g, "&rcub;");

  // 代码块（使用反引号包裹的区域）
  result = result.replace(
    /```(\w*)\n([\s\S]*?)```/g,
    (_: string, lang: string, code: string) => {
      const langTag = lang ? ` {yellow-fg}[${lang}]{/yellow-fg}` : "";
      const border = "─".repeat(40);
      return (
        `\n{black-bg}{white-fg}┌${border}┐${langTag}{/white-fg}{/black-bg}\n` +
        code
          .trim()
          .split("\n")
          .map(
            (line: string) =>
              `{black-bg}{white-fg}│ ${line}{/white-fg}{/black-bg}`
          )
          .join("\n") +
        `\n{black-bg}{white-fg}└${border}┘{/white-fg}{/black-bg}\n`
      );
    }
  );

  // 行内代码
  result = result.replace(
    /`([^`]+)`/g,
    "{black-bg}{white-fg} $1 {/white-fg}{/black-bg}"
  );

  // 标题
  result = result
    .replace(/^### (.+)/gm, "{bold}{cyan-fg}$1{/cyan-fg}{/bold}")
    .replace(/^## (.+)/gm, "{bold}{yellow-fg}$1{/yellow-fg}{/bold}")
    .replace(
      /^# (.+)/gm,
      "{bold}{white-fg}{cyan-bg} $1 {/cyan-bg}{/white-fg}{/bold}"
    );

  // 粗体和斜体
  result = result
    .replace(/\*\*\*([^*]+)\*\*\*/g, "{bold}{italic}$1{/italic}{/bold}")
    .replace(/\*\*([^*]+)\*\*/g, "{bold}$1{/bold}")
    .replace(/\*([^*]+)\*/g, "{italic}$1{/italic}");

  // 无序列表
  result = result.replace(/^[-*+] (.+)/gm, "  {cyan-fg}•{/cyan-fg} $1");

  // 有序列表
  result = result.replace(/^(\d+)\. (.+)/gm, "  {cyan-fg}$1.{/cyan-fg} $2");

  // 引用
  result = result.replace(/^> (.+)/gm, "{yellow-fg}│ $1{/yellow-fg}");

  // 水平线
  result = result.replace(
    /^(?:---|\*\*\*)\s*$/gm,
    "{dim}" + "─".repeat(40) + "{/dim}"
  );

  return result;
}

// ============================================================
// 输入历史管理器
// ============================================================

class InputHistory {
  private history: string[] = [];
  private historyIndex: number = -1;
  private readonly MAX_HISTORY = 50;

  constructor(private inputBox: Widgets.TextareaElement) {}

  /** 绑定键盘事件 */
  bindKeys(): void {
    this.inputBox.key("up", () => {
      if (this.history.length === 0) return;
      if (this.historyIndex < this.history.length - 1) {
        this.historyIndex++;
        this.inputBox.setValue(this.history[this.historyIndex]);
        this.inputBox.focus();
      }
    });

    this.inputBox.key("down", () => {
      if (this.historyIndex > 0) {
        this.historyIndex--;
        this.inputBox.setValue(this.history[this.historyIndex]);
        this.inputBox.focus();
      } else if (this.historyIndex === 0) {
        this.historyIndex = -1;
        this.inputBox.clearValue();
        this.inputBox.focus();
      }
    });
  }

  /** 添加一条历史记录 */
  add(text: string): void {
    // 避免添加重复的连续消息
    if (this.history[0] === text) return;
    this.history.unshift(text);
    if (this.history.length > this.MAX_HISTORY) {
      this.history.pop();
    }
    this.historyIndex = -1;
  }

  /** 获取全部历史 */
  getAll(): string[] {
    return [...this.history];
  }

  /** 清空历史 */
  clear(): void {
    this.history = [];
    this.historyIndex = -1;
  }
}

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** 主题色常量 */
const Theme = {
  userFg: "#88ccff",
  assistantFg: "#88ff88",
  systemFg: "#ffaa44",
  toolCallFg: "#cc88ff",
  toolResultFg: "#66bbaa",
  errorFg: "#ff4444",
  thinkingFg: "#ffaa00",
  statusBg: "#2255aa",
  inputBorder: "#44aa44",
  inputFocusBorder: "#66dd66",
  bg: "#1a1a1a",
} as const;

// ============================================================
// Mock LLM — 模拟大模型对话
// ============================================================

/**
 * 模拟大模型流式响应生成器。
 * 根据用户输入关键词触发不同场景：天气、计算、代码、错误、默认对话。
 *
 * 这是一个 AsyncGenerator，天然适合按事件序列消费的场景。
 */
async function* mockLLMStream(userMessage: string): AsyncGenerator<LLMEvent> {
  const msg = userMessage.toLowerCase().trim();

  // ── 所有场景共用的初始思考延迟 ──
  yield { type: "thinking" };
  await delay(500 + Math.random() * 500);

  // ── 场景 1: 天气查询（工具调用演示） ──
  if (msg.includes("weather") || msg.includes("天气") || msg.includes("温度") || msg.includes("气温")) {
    // 识别城市
    const cityMap: [RegExp, string][] = [
      [/beijing|北京/, "Beijing"],
      [/shanghai|上海/, "Shanghai"],
      [/shenzhen|深圳/, "Shenzhen"],
      [/tokyo|东京/, "Tokyo"],
      [/london|伦敦/, "London"],
      [/paris|巴黎/, "Paris"],
      [/new\s*york|纽约/, "New York"],
    ];
    let city = "Beijing";
    for (const [re, name] of cityMap) {
      if (re.test(msg)) {
        city = name;
        break;
      }
    }

    // 流式输出思考过程
    const phrase = "让我查询天气数据...";
    for (const ch of phrase) {
      yield { type: "text", content: ch };
      await delay(25 + Math.random() * 20);
    }

    // 工具调用
    yield { type: "tool_call", name: "get_weather", args: { city, unit: "celsius" } };
    await delay(1000 + Math.random() * 800);

    // 模拟天气数据
    const conditions = ["☀️ 晴", "⛅ 多云", "🌧️ 小雨", "🌬️ 大风"];
    const result = {
      city,
      temperature: Math.round(8 + Math.random() * 28),
      condition: conditions[Math.floor(Math.random() * conditions.length)],
      humidity: Math.round(25 + Math.random() * 55),
      wind: `${Math.round(5 + Math.random() * 25)} km/h`,
    };
    yield { type: "tool_result", name: "get_weather", result: JSON.stringify(result, null, 2) };

    // 基于工具结果的最终回答
    const reply = `\n\n🌤️ ${city} 天气预报\n${result.condition}\n温度：${result.temperature}°C\n湿度：${result.humidity}%\n风速：${result.wind}\n\n还需要其他帮助吗？`;
    for (const ch of reply) {
      yield { type: "text", content: ch };
      await delay(15 + Math.random() * 10);
    }
  }

  // ── 场景 2: 数学计算（工具调用） ──
  else if (
    msg.includes("计算") ||
    msg.includes("运算") ||
    msg.includes("calculate") ||
    msg.includes("math") ||
    /\d+\s*[\+\-\*\/]\s*\d+/.test(msg)
  ) {
    // 提取表达式
    const exprMatch = msg.match(/([\d\s+\-*/.()^]+)/);
    const expr = exprMatch ? exprMatch[1].trim() : "42 * 7";

    yield { type: "text", content: "正在计算..." };
    await delay(200);
    yield { type: "tool_call", name: "calculate", args: { expression: expr } };
    await delay(600 + Math.random() * 400);

    // 安全计算
    let calcResult: number;
    try {
      calcResult = Function(`"use strict"; return (${expr})`)();
    } catch {
      calcResult = Math.floor(Math.random() * 10000);
    }
    yield { type: "tool_result", name: "calculate", result: `${expr} = ${calcResult}` };

    const reply = `\n\n计算结果：${expr} = ${calcResult}`;
    for (const ch of reply) {
      yield { type: "text", content: ch };
      await delay(20 + Math.random() * 10);
    }
  }

  // ── 场景 3: 代码生成 ──
  else if (
    msg.includes("代码") ||
    msg.includes("code") ||
    msg.includes("写一个") ||
    msg.includes("编程") ||
    msg.includes("function")
  ) {
    const code = `\n\n好的，这里是一个 TypeScript 的 TUI 组件示例：\n\`\`\`typescript
interface Component {
  render(): void;
  onKey(key: string): void;
  destroy(): void;
}

class Button implements Component {
  constructor(
    private label: string,
    private onClick: () => void
  ) {}

  render(): void {
    process.stdout.write(\`[\${this.label}]\`);
  }

  onKey(key: string): void {
    if (key === "enter") this.onClick();
  }

  destroy(): void {
    // 清理资源
  }
}\`\`\``;
    for (const ch of code) {
      yield { type: "text", content: ch };
      await delay(10 + Math.random() * 8);
    }
    await delay(300);
    const followup = "\n\n这是一个基础的组件接口，可以扩展实现更复杂的 TUI 控件。";
    for (const ch of followup) {
      yield { type: "text", content: ch };
      await delay(20 + Math.random() * 15);
    }
  }

  // ── 场景 4: 错误模拟 ──
  else if (msg.includes("错误") || msg.includes("error") || msg.includes("失败") || msg.includes("异常")) {
    yield { type: "text", content: "正在处理您的请求..." };
    await delay(800);
    yield {
      type: "error",
      message: "服务暂不可用 (HTTP 503)\n\n后端服务超时，请稍后重试。您也可以尝试其他问题。",
    };
  }

  // ── 场景 5: 默认对话（流式输出） ──
  else {
    const samples = [
      "这是一个很好的问题！\n\n从架构设计的角度来看，TUI（文本用户界面）结合了 CLI 的高效与 GUI 的直观。",
      "让我来详细解释。\n\nTUI 应用运行在终端模拟器中，通过 ANSI 转义码控制光标、颜色和样式。",
      "这个问题很有意思。\n\n现代 TUI 框架如 blessed、ink、ratatui 提供了声明式布局和事件系统。",
    ];
    const text = samples[Math.floor(Math.random() * samples.length)];
    for (const ch of text) {
      yield { type: "text", content: ch };
      await delay(20 + Math.random() * 25);
    }
    await delay(400);
    const tail = ["\n\n您还想了解哪方面的细节？", "\n\n希望这个回答对您有帮助！", "\n\n还有其他问题吗？"][
      Math.floor(Math.random() * 3)
    ];
    for (const ch of tail) {
      yield { type: "text", content: ch };
      await delay(15 + Math.random() * 12);
    }
  }

  yield { type: "done" };
}

// ============================================================
// ChatTUI — 大模型对话 TUI 应用
// ============================================================

class ChatTUI {
  // ── Blessed 组件 ──
  private screen!: Widgets.Screen;
  private statusBar!: Widgets.BoxElement;
  private chatBox!: Widgets.BoxElement;
  private inputBox!: Widgets.TextareaElement;
  private helpBar!: Widgets.BoxElement;

  // ── 应用运行时状态 ──
  private state: AppState = "idle";
  private messages: ChatMessage[] = [];

  // ── 输入历史 ──
  private inputHistory!: InputHistory;

  // ── 上次发送的消息（用于重试） ──
  private lastMessage: string = "";

  // ── 动态 UI 元素引用 ──
  private currentAssistantMsgEl: Widgets.BoxElement | null = null;
  private currentToolCallEl: Widgets.BoxElement | null = null;
  private currentToolResultEl: Widgets.BoxElement | null = null;
  private currentErrorEl: Widgets.BoxElement | null = null;
  private spinnerTimer: ReturnType<typeof setInterval> | null = null;

  // ── 取消令牌（预留：用于打断生成） ──
  private abortController: AbortController | null = null;

  // ── Spinner 字符序列（Braille 点字） ──
  private static readonly SPINNERS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

  // ── 历史消息数上限（防止内存泄漏） ──
  private static readonly MAX_HISTORY = 100;

  constructor() {
    // 创建屏幕
    this.screen = blessed.screen({
      smartCSR: true,
      title: "AI Chat TUI — 大模型对话终端",
      cursor: { artificial: true, shape: "line", blink: true, color: "cyan" },
      dockBorders: true,
      ignoreDockContrast: true,
      fastCSR: true,
      useBCE: true,
      resizeTimeout: 200,
    });

    this.buildLayout();
    this.bindKeys();

    // 检查最小终端尺寸
    this.checkTerminalSize();

    this.showWelcome();

    // 初始化输入历史
    this.inputHistory = new InputHistory(this.inputBox);
    this.inputHistory.bindKeys();

    // 窗口 resize 事件
    this.screen.on("resize", () => {
      this.checkTerminalSize();
      this.screen.render();
    });

    // 初始聚焦到输入框
    this.inputBox.focus();
    this.screen.render();
  }

  /** 检查终端尺寸是否满足最小要求 */
  private checkTerminalSize(): void {
    const minCols = 60;
    const minRows = 12;
    const cols = this.screen.width as number;
    const rows = this.screen.height as number;
    if (cols < minCols || rows < minRows) {
      this.statusBar.setContent(
        ` {red-fg}{bold}WARNING: Terminal too small! Need at least ${minCols}x${minRows}, current ${cols}x${rows}{/bold}{/red-fg}`
      );
      this.screen.render();
    }
  }

  // ==========================================================
  // 布局构建
  // ==========================================================

  private buildLayout(): void {
    // ── 状态栏（固定顶部 1 行） ──
    this.statusBar = blessed.box({
      parent: this.screen,
      top: 0,
      left: 0,
      width: "100%",
      height: 1,
      content: ` 🤖 AI Chat TUI  |  模型: MockLLM-1.0  |  {green-fg}● 就绪{/green-fg}`,
      style: { fg: "white", bg: Theme.statusBg },
      tags: true,
    });

    // ── 聊天区域（可滚动，占据中间全部空间） ──
    this.chatBox = blessed.box({
      parent: this.screen,
      top: 1,
      left: 0,
      width: "100%",
      bottom: 5, // 留出输入框(3行) + 帮助栏(1行) + 间隙
      scrollable: true,
      alwaysScroll: true,
      scrollbar: {
        ch: "░",
        track: { bg: "#222222" },
        style: { bg: "#888888" },
      },
      style: { fg: "white", bg: Theme.bg },
      tags: true,
      padding: { left: 1, right: 1, top: 0, bottom: 0 },
    });

    // ── 输入框（固定底部 3 行） ──
    this.inputBox = blessed.textarea({
      parent: this.screen,
      bottom: 2,
      left: 0,
      width: "100%",
      height: 3,
      inputOnFocus: true,
      padding: { left: 1, right: 1 },
      style: {
        fg: "white",
        bg: "#0d0d0d",
        border: { fg: Theme.inputBorder },
        focus: { border: { fg: Theme.inputFocusBorder } },
      },
      border: { type: "line", fg: Theme.inputBorder as any },
    });

    // ── 帮助栏（固定底部 1 行） ──
    this.helpBar = blessed.box({
      parent: this.screen,
      bottom: 0,
      left: 0,
      width: "100%",
      height: 1,
      content:
        " {green-fg}Ctrl+S{/green-fg} 发送  {red-fg}Ctrl+Q{/red-fg} 退出  {yellow-fg}Ctrl+C{/yellow-fg} 取消  {cyan-fg}↑↓{/cyan-fg} 输入历史/滚动  {white-fg}R{/white-fg} 重试  {white-fg}Esc{/white-fg} 清空",
      style: { fg: "#888888", bg: "#0a0a0a" },
      tags: true,
    });
  }

  // ==========================================================
  // 键盘事件绑定
  // ==========================================================

  private bindKeys(): void {
    // 发送消息
    this.screen.key("C-s", () => {
      if (this.state !== "idle") return;
      const text = this.inputBox.getValue().trim();
      if (!text) {
        this.flashInputBorder();
        return;
      }
      this.inputBox.clearValue();
      this.screen.render();
      this.sendMessage(text);
    });

    // 退出
    this.screen.key("C-q", () => {
      this.addSystemMessage("{cyan-fg}感谢使用 AI Chat TUI，再见！👋{/cyan-fg}");
      this.screen.render();
      setTimeout(() => {
        this.cleanup();
        process.exit(0);
      }, 500);
    });

    // 取消生成（预留）
    this.screen.key("C-c", () => {
      if (this.state !== "idle" && this.abortController) {
        this.abortController.abort();
      }
    });

    // 聊天区域滚动
    const scroll = (amount: number) => {
      this.chatBox.scroll(amount);
      this.screen.render();
    };
    this.chatBox.key("up", () => scroll(-1));
    this.chatBox.key("down", () => scroll(1));
    this.chatBox.key("pageup", () => scroll(-Math.floor((this.chatBox.height as number) / 2)));
    this.chatBox.key("pagedown", () => scroll(Math.floor((this.chatBox.height as number) / 2)));

    // 鼠标滚轮
    this.chatBox.on("wheeldown", () => scroll(3));
    this.chatBox.on("wheelup", () => scroll(-3));

    // Esc 清空输入
    this.screen.key("escape", () => {
      if (this.state === "idle") {
        this.inputBox.clearValue();
        this.screen.render();
      }
    });

    // R 键重试（仅在 error 状态时有效）
    this.screen.key("r", () => {
      if (this.state === "error" && this.lastMessage) {
        // 移除错误卡片
        this.removeAllByType("error");
        this.state = "idle";
        this.screen.render();
        this.sendMessage(this.lastMessage);
      }
    });

    // 保持输入框焦点
    this.screen.on("focus", () => this.inputBox.focus());

    // 窗口 resize
    this.screen.on("resize", () => {
      this.screen.render();
    });
  }

  // ==========================================================
  // 欢迎信息
  // ==========================================================

  private showWelcome(): void {
    const content = [
      `{cyan-fg}╔══════════════════════════════════════════╗`,
      `║   欢迎使用 AI Chat TUI — 大模型对话终端   ║`,
      `╚══════════════════════════════════════════╝`,
      "",
      "这是一个使用 {bold}blessed{/bold} 构建的大模型对话 TUI 演示。",
      "",
      "{yellow-fg}🌤️  天气查询{/yellow-fg}  — 说 \"北京天气\" 触发工具调用",
      "{yellow-fg}🧮  数学计算{/yellow-fg}  — 说 \"计算 42*7\" 触发工具调用",
      "{yellow-fg}💻  代码生成{/yellow-fg}  — 说 \"写一段代码\" 查看代码输出",
      "{yellow-fg}❌  错误模拟{/yellow-fg}  — 说 \"制造错误\" 查看错误 UI",
      "{yellow-fg}💬  自由对话{/yellow-fg}  — 任何其他内容演示流式输出",
      "",
      "{green-fg}试试在下方输入框输入并按 Ctrl+S 发送消息！{/green-fg}",
    ].join("\n");

    this.addSystemMessage(content);
  }

  // ==========================================================
  // 状态栏更新
  // ==========================================================

  private setStatus(text: string, color: string = Theme.statusBg) {
    this.statusBar.setContent(
      ` 🤖 AI Chat TUI  |  模型: MockLLM-1.0  |  {${color}-fg}${text}{/${color}-fg}`,
    );
    this.screen.render();
  }

  // ==========================================================
  // 核心：发送消息 → 消费流式事件 → 逐帧更新 UI
  // ==========================================================

  private async sendMessage(text: string): Promise<void> {
    // 输入验证
    if (text.length > 2000) {
      this.addSystemMessage(
        `{yellow-fg}⚠️ 消息过长 (${text.length} 字符)，最大支持 2000 字符{/yellow-fg}`
      );
      return;
    }

    // 保存用户消息到历史
    this.inputHistory.add(text);
    this.lastMessage = text;
    this.addUserMessage(text);
    this.messages.push({ role: "user", content: text });

    // 消息历史上限保护
    if (this.messages.length >= ChatTUI.MAX_HISTORY) {
      const removed = this.messages.splice(0, 10);
      removed.forEach(m => {
        // removeMessageElement 通过内容匹配移除
      });
    }

    // 进入 thinking 状态
    this.state = "thinking";
    this.setStatus("🤔 思考中...", "#aa8800");

    this.abortController = new AbortController();
    const signal = this.abortController.signal;

    try {
      await this.processStream(text, signal);
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === "AbortError") {
        this.addSystemMessage("{yellow-fg}⏹️ 生成已取消{/yellow-fg}");
      } else {
        const msg = err instanceof Error ? err.message : "未知错误";
        this.showError(msg);
      }
    } finally {
      this.abortController = null;
      this.state = "idle";
      this.setStatus("● 就绪", "#22aa44");
      this.inputBox.focus();
    }
  }

  /**
   * 消费 LLM 流式事件，根据事件类型逐帧更新 UI
   * 这是整个 TUI 的核心事件循环
   * @param signal - AbortSignal，用于用户取消生成
   */
  private async processStream(userMsg: string, signal?: AbortSignal): Promise<void> {
    let fullResponse = "";
    this.currentAssistantMsgEl = null;

    for await (const event of mockLLMStream(userMsg)) {
      // 检查是否被取消
      if (signal?.aborted) {
        throw new DOMException('生成已取消', 'AbortError');
      }
      switch (event.type) {
        case "thinking":
          this.state = "thinking";
          this.setStatus("🤔 思考中...", "#aa8800");
          this.startSpinner();
          break;

        case "text":
          // 从 thinking 或 tool_result 切换到 streaming
          if (this.state === "thinking" || this.state === "tool_result") {
            this.stopSpinner();
            this.clearToolDisplay();
          }
          this.state = "streaming";
          this.setStatus("💬 生成中...", "#44aa44");
          fullResponse += event.content;
          this.updateAssistantMessage(fullResponse);
          break;

        case "tool_call":
          this.state = "tool_call";
          this.setStatus(`🔧 调用工具: ${event.name}`, "#8844aa");
          this.stopSpinner();
          this.clearToolDisplay();
          this.showToolCall(event.name, event.args);
          break;

        case "tool_result":
          this.state = "tool_result";
          this.setStatus(`✅ 工具返回: ${event.name}`, "#4488aa");
          this.showToolResult(event.name, event.result);
          break;

        case "error":
          this.state = "error";
          this.setStatus("❌ 错误", "#cc2222");
          this.stopSpinner();
          this.clearToolDisplay();
          this.showError(event.message);
          break;

        case "done":
          // 停止所有动画，保存消息
          this.stopSpinner();
          if (fullResponse) {
            this.messages.push({ role: "assistant", content: fullResponse });
          }
          break;
      }
    }
  }

  // ==========================================================
  // UI 渲染方法
  // ==========================================================

  // ── 用户消息 ──
  private addUserMessage(content: string): void {
    blessed.box({
      parent: this.chatBox,
      top: 0,
      left: 0,
      width: "100%-2",
      height: "shrink",
      content: `{${Theme.userFg}-fg}{bold}👤 用户{/bold}{/${Theme.userFg}-fg}\n{white-fg}${this.escapeTags(content)}{/white-fg}`,
      style: { fg: "white", bg: Theme.bg },
      tags: true,
      padding: { left: 1, right: 1, top: 0, bottom: 0 },
      wrap: true,
      shrink: true,
    });
    this.addSpacer();
    this.screen.render();
    this.scrollToBottom();
  }

  // ── 系统消息 ──
  private addSystemMessage(content: string): void {
    blessed.box({
      parent: this.chatBox,
      top: 0,
      left: 0,
      width: "100%-2",
      height: "shrink",
      content: `{${Theme.systemFg}-fg}{bold}💬 系统{/bold}{/${Theme.systemFg}-fg}\n${content}`,
      style: { fg: "white", bg: Theme.bg },
      tags: true,
      padding: { left: 1, right: 1, top: 0, bottom: 0 },
      wrap: true,
      shrink: true,
    });
    this.addSpacer();
    this.screen.render();
    this.scrollToBottom();
  }

  // ── AI 回复（流式更新并显示打字机效果 + Markdown 渲染） ──
  private updateAssistantMessage(content: string): void {
    // 使用 Markdown 渲染（代码块、标题、列表等）
    const renderedContent = renderMarkdown(content);

    if (!this.currentAssistantMsgEl) {
      this.currentAssistantMsgEl = blessed.box({
        parent: this.chatBox,
        top: 0,
        left: 0,
        width: "100%-2",
        height: "shrink",
        content:
          `{${Theme.assistantFg}-fg}{bold}🤖 AI{/bold}{/${Theme.assistantFg}-fg}\n${renderedContent}`,
        style: { fg: "white", bg: Theme.bg },
        tags: true,
        padding: { left: 1, right: 1, top: 0, bottom: 0 },
        wrap: true,
        shrink: true,
      });
      this.addSpacer();
    } else {
      this.currentAssistantMsgEl.setContent(
        `{${Theme.assistantFg}-fg}{bold}🤖 AI{/bold}{/${Theme.assistantFg}-fg}\n${renderedContent}`,
      );
    }
    this.screen.render();
    this.scrollToBottom();
  }

  // ── Spinner 动画 ──
  private startSpinner(): void {
    this.stopSpinner();

    const el = blessed.box({
      parent: this.chatBox,
      top: 0,
      left: 0,
      width: "100%-2",
      height: 1,
      content: "",
      style: { fg: "yellow", bg: Theme.bg },
      tags: true,
      padding: { left: 2, right: 1, top: 0, bottom: 0 },
    });

    let i = 0;
    this.spinnerTimer = setInterval(() => {
      i = (i + 1) % ChatTUI.SPINNERS.length;
      el.setContent(
        `  {${Theme.thinkingFg}-fg}{bold}${ChatTUI.SPINNERS[i]} AI 思考中...{/bold}{/${Theme.thinkingFg}-fg}`,
      );
      this.screen.render();
    }, 100);

    this.scrollToBottom();
  }

  private stopSpinner(): void {
    if (this.spinnerTimer) {
      clearInterval(this.spinnerTimer);
      this.spinnerTimer = null;
    }
    // 移除所有 thinking 元素
    this.removeAllByType("thinking");
  }

  // ── 工具调用卡片 ──
  private showToolCall(name: string, args: Record<string, unknown>): void {
    const argsStr = Object.entries(args)
      .map(([k, v]) => `  ${k}: {yellow-fg}${JSON.stringify(v)}{/yellow-fg}`)
      .join("\n");

    this.currentToolCallEl = blessed.box({
      parent: this.chatBox,
      top: 0,
      left: 0,
      width: "100%-2",
      height: "shrink",
      content:
        `{${Theme.toolCallFg}-fg}┌─ 🔧 {bold}工具调用{/bold} ─────────────────────────────────┐{/${Theme.toolCallFg}-fg}\n` +
        `{${Theme.toolCallFg}-fg}│{/${Theme.toolCallFg}-fg}  工具: {bold}${name}{/bold}\n` +
        `{${Theme.toolCallFg}-fg}│{/${Theme.toolCallFg}-fg}  参数:\n${argsStr}\n` +
        `{${Theme.toolCallFg}-fg}│{/${Theme.toolCallFg}-fg}\n` +
        `{${Theme.toolCallFg}-fg}│{/${Theme.toolCallFg}-fg}  {yellow-fg}⏳ 执行中...{/yellow-fg}\n` +
        `{${Theme.toolCallFg}-fg}└────────────────────────────────────────────────┘{/${Theme.toolCallFg}-fg}`,
      style: { fg: "white", bg: Theme.bg },
      tags: true,
      padding: { left: 1, right: 1, top: 0, bottom: 0 },
      wrap: true,
      shrink: true,
    });

    this.addSpacer();
    this.screen.render();
    this.scrollToBottom();
  }

  // ── 工具结果卡片 ──
  private showToolResult(name: string, result: string): void {
    // 清除之前的 tool call 卡片
    this.clearToolCall();

    this.currentToolResultEl = blessed.box({
      parent: this.chatBox,
      top: 0,
      left: 0,
      width: "100%-2",
      height: "shrink",
      content:
        `{${Theme.toolResultFg}-fg}┌─ ✅ {bold}工具结果{/bold} ─────────────────────────────────┐{/${Theme.toolResultFg}-fg}\n` +
        `{${Theme.toolResultFg}-fg}│{/${Theme.toolResultFg}-fg}  工具: {bold}${name}{/bold}\n` +
        `{${Theme.toolResultFg}-fg}│{/${Theme.toolResultFg}-fg}\n` +
        `{green-fg}${this.escapeTags(result)}{/green-fg}\n` +
        `{${Theme.toolResultFg}-fg}│{/${Theme.toolResultFg}-fg}\n` +
        `{${Theme.toolResultFg}-fg}└────────────────────────────────────────────────┘{/${Theme.toolResultFg}-fg}`,
      style: { fg: "white", bg: Theme.bg },
      tags: true,
      padding: { left: 1, right: 1, top: 0, bottom: 0 },
      wrap: true,
      shrink: true,
    });

    this.addSpacer();
    this.screen.render();
    this.scrollToBottom();
  }

  // ── 错误卡片（含重试按钮） ──
  private showError(message: string): void {
    this.stopSpinner();
    this.clearToolDisplay();

    this.currentErrorEl = blessed.box({
      parent: this.chatBox,
      top: 0,
      left: 0,
      width: "100%-2",
      height: "shrink",
      content:
        `{${Theme.errorFg}-fg}┌─ ❌ {bold}错误{/bold} ───────────────────────────────────┐{/${Theme.errorFg}-fg}\n` +
        `{${Theme.errorFg}-fg}│{/${Theme.errorFg}-fg}\n` +
        `{${Theme.errorFg}-fg}│{/${Theme.errorFg}-fg}  {white-fg}${this.escapeTags(message)}{/white-fg}\n` +
        `{${Theme.errorFg}-fg}│{/${Theme.errorFg}-fg}\n` +
        `{${Theme.errorFg}-fg}│{/${Theme.errorFg}-fg}  请稍后重试或尝试其他问题。\n` +
        `{${Theme.errorFg}-fg}└────────────────────────────────────────────────┘{/${Theme.errorFg}-fg}`,
      style: { fg: "white", bg: Theme.bg },
      tags: true,
      padding: { left: 1, right: 1, top: 0, bottom: 0 },
      wrap: true,
      shrink: true,
    });

    this.addSpacer();

    // 如果有上次发送的消息，显示重试按钮
    if (this.lastMessage) {
      this.addRetryButton(this.lastMessage);
    }

    this.screen.render();
    this.scrollToBottom();
  }

  // ── 辅助渲染方法 ──

  /** 添加一个空行作为消息间隔 */
  private addSpacer(): void {
    blessed.box({
      parent: this.chatBox,
      top: 0,
      left: 0,
      width: "100%",
      height: 1,
      content: "",
      style: { bg: Theme.bg },
    });
  }

  /** 自动滚动到底部以显示最新内容 */
  private scrollToBottom(): void {
    this.chatBox.setScrollPerc(100);
  }

  /** 清除工具调用卡片 */
  private clearToolCall(): void {
    if (this.currentToolCallEl) {
      this.chatBox.remove(this.currentToolCallEl);
      this.currentToolCallEl = null;
    }
  }

  /** 清除工具结果卡片 */
  private clearToolResult(): void {
    if (this.currentToolResultEl) {
      this.chatBox.remove(this.currentToolResultEl);
      this.currentToolResultEl = null;
    }
  }

  /** 清除所有工具相关 UI */
  private clearToolDisplay(): void {
    this.clearToolCall();
    this.clearToolResult();
  }

  /** 按元素类型批量移除聊天区域中的动态元素 */
  private removeAllByType(type: string): void {
    const toRemove: Widgets.BoxElement[] = [];
    this.chatBox.children.forEach((child) => {
      const el = child as Widgets.BoxElement;
      const content = el.getContent();
      if (
        (type === "thinking" && content.includes("AI 思考中")) ||
        (type === "error" && content.includes("错误"))
      ) {
        toRemove.push(el);
      }
    });
    toRemove.forEach((el) => {
      try {
        this.chatBox.remove(el);
      } catch {
        // 元素可能已被移除
      }
    });
  }

  // ── 重试按钮（在错误卡片下方显示） ──
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
      this.chatBox.remove(retryBtn);
      if (this.currentErrorEl) {
        this.chatBox.remove(this.currentErrorEl);
        this.currentErrorEl = null;
      }
      this.screen.render();
      this.sendMessage(originalMessage);
    });

    // 提示文本
    blessed.box({
      parent: this.chatBox,
      top: 0, left: 20,
      width: '100%-24', height: 1,
      content: '{yellow-fg}按 R 键重试 或 重新输入消息{/yellow-fg}',
      tags: true,
      style: { fg: 'yellow', bg: Theme.bg },
    });

    this.screen.render();
    this.scrollToBottom();
  }

  // ── 按内容移除消息元素（用于历史管理） ──
  private removeMessageElement(msg: ChatMessage): void {
    const toRemove: Widgets.BoxElement[] = [];
    this.chatBox.children.forEach((child) => {
      const el = child as Widgets.BoxElement;
      const content = el.getContent();
      // 匹配消息内容的前 20 个字符作为标识
      const snippet = msg.content.slice(0, 20);
      if (snippet && content.includes(snippet)) {
        toRemove.push(el);
      }
    });
    toRemove.forEach((el) => {
      try { this.chatBox.remove(el); } catch { /* ignore */ }
    });
  }

  /** 输入框边框闪烁（空输入提示） */
  private flashInputBorder(): void {
    const originalFg = this.inputBox.style.border?.fg;
    this.inputBox.style.border = { fg: "red" as any };
    this.screen.render();
    setTimeout(() => {
      this.inputBox.style.border = { fg: (originalFg || Theme.inputBorder) as any };
      this.screen.render();
    }, 300);
  }

  /**
   * 转义 blessed 标签语法（花括号）
   * 防止用户输入或 API 返回的内容被解析为样式标签
   */
  private escapeTags(text: string): string {
    return text.replace(/\{/g, "&lcub;").replace(/\}/g, "&rcub;");
  }

  // ==========================================================
  // 清理与退出
  // ==========================================================

  private cleanup(): void {
    this.stopSpinner();
    if (this.abortController) {
      this.abortController.abort();
      this.abortController = null;
    }
    this.screen.destroy();
  }
}

// ============================================================
// 启动
// ============================================================

let app: ChatTUI;

try {
  app = new ChatTUI();
} catch (err) {
  console.error("启动失败:", err);
  process.exit(1);
}
