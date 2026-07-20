#!/usr/bin/env npx tsx
/**
 * basic-tui.ts — TUI 基础示例
 *
 * 展示 blessed 库的核心功能：屏幕管理、布局组件、颜色样式、事件处理、动画效果。
 * 对应教程第四章。
 *
 * 运行方式:
 *   cd examples && npm install && npx tsx basic-tui.ts
 *
 * 快捷键:
 *   Tab          — 切换焦点
 *   Shift+Tab    — 反向切换焦点
 *   ↑/↓          — 列表导航
 *   Enter        — 确认/点击
 *   Ctrl+Q       — 退出
 *   Escape       — 取消
 *   Home/End     — 列表首尾跳转
 *
 * 注意: 如果 Ctrl+Q 在您的终端中被拦截，请运行: stty -ixon
 */

import * as blessed from "blessed";
import { Widgets } from "blessed";

// ============================================================
// 全局: 终端安全退出装置
// ============================================================
function resetTerminal(): void {
  try {
    process.stdout.write('\x1b[2J\x1b[H');
    process.stdout.write('\x1b[?25h');
    process.stdout.write('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l');
    process.stdout.write('\x1b[0m');
    if (process.stdin.isTTY) process.stdin.setRawMode(false);
    process.stdin.pause();
  } catch { /* ignore */ }
}
process.on('exit', () => resetTerminal());
process.on('SIGINT', () => { resetTerminal(); process.exit(0); });
process.on('SIGTERM', () => { resetTerminal(); process.exit(0); });
process.on('uncaughtException', (err) => {
  resetTerminal();
  console.error('\nFatal:', err);
  process.exit(1);
});
process.on('unhandledRejection', (reason) => {
  resetTerminal();
  console.error('\nUnhandled Rejection:', reason);
  process.exit(1);
});

// ============================================================
// 辅助函数
// ============================================================

/** 异步延迟 */
const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * 创建一个自定义进度条组件
 * 展示如何在 blessed 中构建自定义组件
 */
function createProgressBar(
  screen: Widgets.Screen,
  parent: Widgets.BoxElement,
  opts: { top: number; label: string; color: string; width?: number },
): { start: () => void; stop: () => void; reset: () => void; isRunning: () => boolean } {
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
    border: { type: "line" as const, fg: opts.color as any },
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
        if (!running) return;
        progress = Math.min(100, progress + Math.random() * 8 + 1);
        render();
        if (progress >= 100) {
          running = false;
          if (timer) clearInterval(timer);
          timer = null;
        }
      }, 150);
    },
    stop() {
      running = false;
      if (timer) clearInterval(timer);
      timer = null;
    },
    reset() {
      this.stop();
      progress = 0;
      render();
    },
    isRunning() {
      return running;
    },
  };
}

/**
 * 简易 Markdown 渲染 —— 用于演示 Markdown 在终端中的渲染效果
 * 将 Markdown 文本转换为 blessed 标签格式
 */
function simpleRenderMarkdown(md: string): string {
  let result = md
    .replace(/\{/g, "&lcub;").replace(/\}/g, "&rcub;")  // 转义花括号
    .replace(/^### (.+)/gm, "{bold}{cyan-fg}$1{/cyan-fg}{/bold}")       // h3
    .replace(/^## (.+)/gm, "{bold}{yellow-fg}$1{/yellow-fg}{/bold}")    // h2
    .replace(/^# (.+)/gm, "{bold}{white-fg}{cyan-bg} $1 {/cyan-bg}{/white-fg}{/bold}") // h1
    .replace(/\*\*\*(.+?)\*\*\*/g, "{bold}{italic}$1{/italic}{/bold}")  // 粗斜体
    .replace(/\*\*(.+?)\*\*/g, "{bold}$1{/bold}")                       // 粗体
    .replace(/\*(.+?)\*/g, "{italic}$1{/italic}")                       // 斜体
    .replace(/`([^`]+)`/g, "{black-bg}{white-fg}$1{/white-fg}{/black-bg}") // 行内代码
    .replace(/^> (.+)/gm, "{yellow-fg}│ $1{/yellow-fg}")                // 引用
    .replace(/^[-*+] (.+)/gm, "  {cyan-fg}•{/cyan-fg} $1")             // 列表
    .replace(/^(\d+)\. (.+)/gm, "  {cyan-fg}$1.{/cyan-fg} $2");        // 有序列表
  return result;
}

// ============================================================
// 主程序
// ============================================================

async function main() {
  // ──────────────────────────────────────────
  // 1. 创建屏幕
  // ──────────────────────────────────────────
  const screen = blessed.screen({
    smartCSR: true,       // 智能光标 —— 只发送变化部分
    fullUnicode: true,    // 完整 Unicode 支持（emoji/CJK）
    title: "TUI 基础示例",
    useBCE: true,         // 使用背景色擦除
    resizeTimeout: 200,   // resize 事件节流 (ms)
  });

  // 启用鼠标支持
  screen.on("mouse", () => {
    /* 鼠标事件已启用 */
  });

  // ──────────────────────────────────────────
  // 2. 构建布局
  // ──────────────────────────────────────────

  // 顶部标题栏（固定 1 行）
  const titleBar = blessed.box({
    parent: screen,
    top: 0,
    left: 0,
    width: "100%",
    height: 1,
    content:
      " {bold}TUI 基础示例{/bold}  |  Blessed.js 0.1  |  Tab 切换焦点  |  ↑↓ 导航  |  Ctrl+Q 退出",
    style: { fg: "white", bg: "#2255aa" },
    tags: true,
  });

  // 左侧面板 — 组件展示区（宽度 50%）
  const leftPanel = blessed.box({
    parent: screen,
    top: 1,
    left: 0,
    width: "50%",
    bottom: 1,
    label: " 组件展示 ",
    border: { type: "line", fg: "#44aacc" as any },
    style: { fg: "white", bg: "#111111" },
    tags: true,
    padding: { left: 1, right: 1, top: 0, bottom: 0 },
  });

  // 右侧面板 — 事件日志区（宽度 50%）
  const rightPanel = blessed.box({
    parent: screen,
    top: 1,
    left: "50%",
    width: "50%",
    bottom: 1,
    label: " 事件日志 ",
    border: { type: "line", fg: "#cc8844" as any },
    style: { fg: "#cccccc", bg: "#0d0d0d" },
    tags: true,
    padding: { left: 1, right: 1, top: 0, bottom: 0 },
    scrollable: true,
    alwaysScroll: true,
  });

  // 底部状态栏（固定 1 行）
  const statusBar = blessed.box({
    parent: screen,
    bottom: 0,
    left: 0,
    width: "100%",
    height: 1,
    content:
      " {green-fg}●{/green-fg} 运行中  |  Tab:切换焦点  ↑↓:导航  Enter:确认  Ctrl+Q:退出",
    style: { fg: "#cccccc", bg: "#0a0a0a" },
    tags: true,
  });

  // ──────────────────────────────────────────
  // 3. 左侧面板 — 组件
  // ──────────────────────────────────────────

  // 3.1 文本标签 — 展示 blessed 的颜色标签语法
  blessed.box({
    parent: leftPanel,
    top: 0,
    left: 1,
    width: "100%-2",
    height: 1,
    content: "{bold}{cyan-fg}■ 文本展示{/cyan-fg}{/bold}",
    tags: true,
    style: { fg: "white", bg: "transparent" },
  });

  blessed.box({
    parent: leftPanel,
    top: 1,
    left: 3,
    width: "100%-6",
    height: 1,
    content:
      "正常文本  {red-fg}红色{/red-fg}  {green-fg}绿色{/green-fg}  {yellow-fg}黄色{/yellow-fg}  {bold}粗体{/bold}  {underline}下划线{/underline}",
    tags: true,
    style: { fg: "white", bg: "transparent" },
  });

  // 3.2 按钮
  blessed.box({
    parent: leftPanel,
    top: 3,
    left: 1,
    width: "100%-2",
    height: 1,
    content: "{bold}{cyan-fg}■ 按钮{/cyan-fg}{/bold}",
    tags: true,
    style: { fg: "white", bg: "transparent" },
  });

  // 确定按钮（绿色）
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
      focus: { bg: "#44cc44" },
      hover: { bg: "#338833" },
    },
    mouse: true,
  });

  // 取消按钮（红色）
  const btnCancel = blessed.button({
    parent: leftPanel,
    top: 4,
    left: 18,
    width: 12,
    height: 1,
    content: " [ 取消 ] ",
    align: "center",
    style: {
      fg: "white",
      bg: "#662222",
      focus: { bg: "#cc4444" },
      hover: { bg: "#883333" },
    },
    mouse: true,
  });

  // 3.3 列表
  blessed.box({
    parent: leftPanel,
    top: 6,
    left: 1,
    width: "100%-2",
    height: 1,
    content: "{bold}{cyan-fg}■ 列表{/cyan-fg}{/bold}",
    tags: true,
    style: { fg: "white", bg: "transparent" },
  });

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
      "  🔄 选项四：重新加载",
      "  ❌ 选项五：停止服务",
    ],
    style: {
      fg: "white",
      bg: "transparent",
      selected: { fg: "black", bg: "#88ccff" },
      item: { fg: "white", bg: "transparent" },
    },
    tags: true,
    mouse: true,
    keys: true,
    vi: true,
  });

  // 3.4 进度条动画区域
  blessed.box({
    parent: leftPanel,
    top: 13,
    left: 1,
    width: "100%-2",
    height: 1,
    content: "{bold}{cyan-fg}■ 进度条动画{/cyan-fg}{/bold}",
    tags: true,
    style: { fg: "white", bg: "transparent" },
  });

  // 创建三个不同颜色的进度条
  const bar1 = createProgressBar(screen, leftPanel, {
    top: 14,
    label: "任务 A",
    color: "green",
  });
  const bar2 = createProgressBar(screen, leftPanel, {
    top: 17,
    label: "任务 B",
    color: "yellow",
  });
  const bar3 = createProgressBar(screen, leftPanel, {
    top: 20,
    label: "任务 C",
    color: "red",
  });

  // 进度条控制按钮
  const btnStart = blessed.button({
    parent: leftPanel,
    top: 23,
    left: 3,
    width: 14,
    height: 1,
    content: " ▶ 开始动画 ",
    style: {
      fg: "white",
      bg: "#2255aa",
      focus: { bg: "#4488dd" },
      hover: { bg: "#3366bb" },
    },
    mouse: true,
  });

  const btnReset = blessed.button({
    parent: leftPanel,
    top: 23,
    left: 20,
    width: 14,
    height: 1,
    content: " ↺ 重置 ",
    style: {
      fg: "white",
      bg: "#664422",
      focus: { bg: "#aa8844" },
      hover: { bg: "#886633" },
    },
    mouse: true,
  });

  // 3.5 Markdown 渲染演示
  blessed.box({
    parent: leftPanel,
    top: 25,
    left: 1,
    width: "100%-2",
    height: 1,
    content: "{bold}{cyan-fg}■ Markdown 渲染演示{/cyan-fg}{/bold}",
    tags: true,
    style: { fg: "white", bg: "transparent" },
  });

  const btnMarkdown = blessed.button({
    parent: leftPanel,
    top: 26,
    left: 3,
    width: 22,
    height: 1,
    content: " 📝 显示 Markdown ",
    style: {
      fg: "white",
      bg: "#334466",
      focus: { bg: "#5588cc" },
      hover: { bg: "#446688" },
    },
    mouse: true,
  });

  // ──────────────────────────────────────────
  // 4. 右侧面板 — 日志输出
  // ──────────────────────────────────────────

  /**
   * 向日志面板写入消息
   * 显示时间戳并自动滚动到最新
   */
  function log(msg: string) {
    const lines = rightPanel.getContent().split("\n");
    lines.push(`[${new Date().toLocaleTimeString()}] ${msg}`);
    if (lines.length > 30) lines.splice(0, lines.length - 30);
    rightPanel.setContent(lines.join("\n"));
    rightPanel.setScrollPerc(100);
    screen.render();
  }

  log("系统就绪，等待操作...");

  // ──────────────────────────────────────────
  // 5. 事件绑定
  // ──────────────────────────────────────────

  // 5.1 按钮事件
  btnOk.on("press", () => log("{green-fg}确定{/green-fg} 按钮被点击"));
  btnCancel.on("press", () => log("{red-fg}取消{/red-fg} 按钮被点击"));

  // 5.2 进度条控制
  btnStart.on("press", () => {
    log("开始运行进度条动画");
    bar1.start();
    bar2.start();
    bar3.start();
  });

  btnReset.on("press", () => {
    log("重置进度条");
    bar1.reset();
    bar2.reset();
    bar3.reset();
  });

  // 5.3 列表选择事件
  list.on("select", (item) => {
    const text = item.getContent().trim();
    log(`选中列表项: {cyan-fg}${text}{/cyan-fg}`);
  });

  // 5.4 焦点管理 — Tab 循环切换
  const focusable = [btnOk, btnCancel, list, btnStart, btnReset, btnMarkdown];
  let focusIndex = 0;

  screen.key("tab", () => {
    focusIndex = (focusIndex + 1) % focusable.length;
    focusable[focusIndex].focus();
    log(
      `焦点切换到: {yellow-fg}${focusable[focusIndex].getContent().trim() || "列表"}{/yellow-fg}`,
    );
  });

  screen.key("S-tab", () => {
    focusIndex = (focusIndex - 1 + focusable.length) % focusable.length;
    focusable[focusIndex].focus();
    log(
      `焦点切换到: {yellow-fg}${focusable[focusIndex].getContent().trim() || "列表"}{/yellow-fg}`,
    );
  });

  // 鼠标点击自动更新焦点索引
  focusable.forEach((w) => {
    w.on("focus", () => {
      focusIndex = focusable.indexOf(w);
    });
  });

  // 5.5 Markdown 渲染演示按钮
  const mdSample =
    "# Markdown 渲染演示\n\n" +
    "这是 **粗体**、*斜体* 和 `行内代码` 的展示。\n\n" +
    "## 代码示例\n\n" +
    "这里是一个 `TypeScript` 函数：\n\n" +
    "> 编程是一种艺术形式\n\n" +
    "列表展示：\n" +
    "- 第一项：可以包含 *强调* 文本\n" +
    "- 第二项：可以包含 `代码` 元素\n" +
    "- 第三项：普通列表项\n\n" +
    "1. 有序第一\n" +
    "2. 有序第二\n" +
    "3. 有序第三";

  btnMarkdown.on("press", () => {
    log("Markdown 渲染演示:");
    const rendered = simpleRenderMarkdown(mdSample);
    rendered.split("\n").forEach((line) => log(line));
    log("{dim}────────────────────{/dim}");
  });

  // 5.6 窗口大小变化
  screen.on("resize", () => {
    log(`窗口大小调整: ${screen.width}x${screen.height}`);
    screen.render();
  });

  // ──────────────────────────────────────────
  // 6. 全局快捷键
  // ──────────────────────────────────────────

  // 退出（key.ignore=true 阻止焦点组件消费此键）
  screen.key("C-q", (_ch, key) => {
    key.ignore = true;
    log("用户退出程序");
    setTimeout(() => process.exit(0), 100);
  });

  // Escape
  screen.key("escape", () => {
    log("按下 Escape 键");
  });

  // 通用 keypress 监听：Ctrl+Q 兜底 + 记录其他按键
  screen.on("keypress", (ch, key) => {
    // Ctrl+Q 兜底（screen.key 匹配失败时的备选）
    if (key.name === "q" && key.ctrl) {
      log("用户退出程序");
      setTimeout(() => process.exit(0), 100);
      return;
    }
    // 过滤控制类按键，避免日志刷屏
    if (key.name === "tab" || key.name === "escape") return;
    if (ch && /^[\x00-\x1f]$/.test(ch)) return;
    if (["up", "down", "left", "right"].includes(key.name || "")) return;
  });

  // ──────────────────────────────────────────
  // 7. 渲染与启动
  // ──────────────────────────────────────────

  screen.render();
  log("TUI 基础示例启动完成");

  // 启动后聚焦第一个元素
  setTimeout(() => {
    btnOk.focus();
    screen.render();
  }, 100);

  // ── 兜底: 原始 stdin 监听（Windows 下 blessed key 事件可能失效） ──
  process.stdin.on("data", (raw: Buffer | string) => {
    const data = typeof raw === "string" ? raw : raw.toString("utf8");
    for (const ch of data) {
      if (ch.charCodeAt(0) === 17) { // Ctrl+Q
        setTimeout(() => process.exit(0), 100);
        return;
      }
    }
  });
}

// ──────────────────────────────────────────
// 启动程序
// ──────────────────────────────────────────

main().catch((err) => {
  console.error("程序出错:", err);
  process.exit(1);
});
