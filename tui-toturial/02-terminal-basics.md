# 第二章 终端基础

## 2.1 终端的工作原理

终端（Terminal）是一个文本输入/输出环境。当你在终端中运行程序时，数据流在程序与终端模拟器之间双向传输：

```
┌─────────────┐    stdout (写)   ┌──────────────┐
│  你的程序    │ ──────────────► │  终端模拟器   │
│  (TUI 应用)  │ ◄────────────── │  (Terminal)   │
└─────────────┘    stdin (读)    └──────────────┘
     │                                  │
     │    stderr (错误)                  │
     │ ──────────────────────────────►  │
     │                                  │
     │  ANSI 转义序列控制:              │
     │  光标位置、颜色、样式、清屏       │
     └──────────────────────────────────┘
```

### 核心概念

1. **stdout** — 程序向终端发送文本和转义序列（标准输出通道）
2. **stdin** — 终端将用户键盘输入发送给程序（标准输入通道）
3. **stderr** — 错误输出通道（可独立重定向）
4. **ANSI 转义序列** — 以 `ESC` 开头的控制代码，控制光标、颜色、样式

### 终端模拟器 vs 物理终端

历史上"终端"指物理设备（如 VT100），现代开发环境使用的是**终端模拟器**（Terminal Emulator）：

| 终端模拟器 | 平台 | 特点 |
|-----------|------|------|
| **Windows Terminal** | Windows | 微软新一代终端，支持 TrueColor、Unicode |
| **iTerm2** | macOS | 功能丰富的 macOS 终端 |
| **Terminal.app** | macOS | macOS 自带终端 |
| **xterm** | Linux | X Window 系统标准终端 |
| **VSCode 终端** | 跨平台 | IDE 内嵌终端 |
| **tmux** | 跨平台 | 终端复用器（在终端中运行） |
| **Alacritty** | 跨平台 | GPU 加速终端 |
| **kitty** | 跨平台 | GPU 加速，功能丰富 |

## 2.2 ANSI 转义码

ANSI 转义码是所有 TUI 的基础。它们以 `ESC` 字符（`\x1b` 或 `\033`）开头，后跟 `[` 和具体的控制序列。

### 2.2.1 光标控制

```typescript
const ESC = '\x1b';

// ── 清屏 ──
process.stdout.write(`${ESC}[2J`);        // 清空整个屏幕
process.stdout.write(`${ESC}[0J`);        // 清空光标到屏幕底部
process.stdout.write(`${ESC}[1J`);        // 清空光标到屏幕顶部
process.stdout.write(`${ESC}[K`);         // 清空当前行光标到行尾

// ── 光标定位 ──
process.stdout.write(`${ESC}[H`);         // 光标回到 (0,0)
process.stdout.write(`${ESC}[3;5H`);      // 光标移动到第 3 行第 5 列

// ── 光标偏移 ──
process.stdout.write(`${ESC}[1A`);        // 光标上移 1 行
process.stdout.write(`${ESC}[2B`);        // 光标下移 2 行
process.stdout.write(`${ESC}[3C`);        // 光标右移 3 列
process.stdout.write(`${ESC}[4D`);        // 光标左移 4 列

// ── 光标保存/恢复 ──
process.stdout.write(`${ESC}[s`);         // 保存光标位置
process.stdout.write(`${ESC}[u`);         // 恢复光标位置

// ── 光标可见性 ──
process.stdout.write(`${ESC}[?25l`);      // 隐藏光标
process.stdout.write(`${ESC}[?25h`);      // 显示光标
```

### 2.2.2 颜色控制

ANSI 支持三种颜色模式，从低到高：

| 模式 | 颜色数 | 格式 | 适用场景 |
|------|--------|------|---------|
| **标准色** | 16 (8 色 + 亮色) | `\x1b[31m` | 兼容所有终端 |
| **256 色** | 256 | `\x1b[38;5;196m` | 终端支持 256 色 |
| **TrueColor** | 16,777,216 | `\x1b[38;2;255;100;50m` | 现代终端（推荐） |

```typescript
// ── 标准 16 色 ──
const FGs: Record<string, number> = {
  black: 30, red: 31, green: 32, yellow: 33,
  blue: 34, magenta: 35, cyan: 36, white: 37,
};
const BGs: Record<string, number> = {
  black: 40, red: 41, green: 42, yellow: 43,
  blue: 44, magenta: 45, cyan: 46, white: 47,
};
// 亮色 = 标准色 + 60
const brightRed = 91;

// ── 256 色 ──
process.stdout.write(`\x1b[38;5;196m红色文字\x1b[0m\n`);   // 前景色 196 号（红色）
process.stdout.write(`\x1b[48;5;235m深灰背景\x1b[0m\n`);   // 背景色 235 号（深灰）
// 常用的终端 256 色:
//   16-231: 6×6×6 色彩立方
//   232-255: 灰度梯度（232最黑→255最白）

// ── TrueColor (24 位) ──
process.stdout.write(`\x1b[38;2;255;100;50m橙色文字\x1b[0m\n`);
process.stdout.write(`\x1b[48;2;30;30;50m深蓝背景\x1b[0m\n`);

// ── 辅助函数 ──
function rgb(r: number, g: number, b: number, isBg = false): string {
  return `\x1b[${isBg ? 48 : 38};2;${r};${g};${b}m`;
}
function reset(): string { return '\x1b[0m'; }

console.log(`${rgb(255, 100, 50)}Hello TUI${reset()}`);

// ── 高级：渐变色生成 ──
function gradient(text: string, start: [number,number,number], end: [number,number,number]): string {
  return text.split('').map((ch, i) => {
    const t = i / (text.length - 1 || 1);
    const r = Math.round(start[0] + (end[0] - start[0]) * t);
    const g = Math.round(start[1] + (end[1] - start[1]) * t);
    const b = Math.round(start[2] + (end[2] - start[2]) * t);
    return `\x1b[38;2;${r};${g};${b}m${ch}${reset()}`;
  }).join('');
}
console.log(gradient('Hello TUI!', [255,0,0], [0,0,255]));  // 红 → 蓝 渐变
```

### 2.2.3 样式控制

```typescript
const Style = {
  reset:         '\x1b[0m',
  bold:          '\x1b[1m',
  dim:           '\x1b[2m',
  italic:        '\x1b[3m',
  underline:     '\x1b[4m',
  blink:         '\x1b[5m',     // 注意：现代终端通常忽略闪烁
  reverse:       '\x1b[7m',     // 反转前景/背景色
  hidden:        '\x1b[8m',
  strikethrough: '\x1b[9m',
};

console.log(`${Style.bold}粗体${Style.reset}`);
console.log(`${Style.italic}斜体${Style.reset}`);
console.log(`${Style.underline}下划线${Style.reset}`);
console.log(`${Style.bold}${Style.reverse}粗体+反白${Style.reset}`);
```

### 2.2.4 清屏与滚动

```typescript
// 滚动
process.stdout.write(`\x1b[S`);           // 向上滚动一行
process.stdout.write(`\x1b[T`);           // 向下滚动一行

// 设置滚动区域（常用于分屏）
process.stdout.write(`\x1b[2;20r`);       // 设置滚动区域为行 2-20
process.stdout.write(`\x1b[r`);           // 重置滚动区域
```

### 实践：制作一个"终端画布"

将以上知识组合，实现一个终端画布工具：

```typescript
class TerminalCanvas {
  private readonly ESC = '\x1b';

  clear(): void {
    process.stdout.write(`${this.ESC}[2J${this.ESC}[H`);
  }

  write(x: number, y: number, text: string, color?: string): void {
    process.stdout.write(`${this.ESC}[${y};${x}H`);
    if (color) process.stdout.write(color);
    process.stdout.write(text);
    if (color) process.stdout.write('\x1b[0m');
  }

  drawBox(x: number, y: number, w: number, h: number, title?: string): void {
    const top = '┌' + '─'.repeat(w - 2) + '┐';
    const mid = '│' + ' '.repeat(w - 2) + '│';
    const bot = '└' + '─'.repeat(w - 2) + '┘';

    // 如果有标题，写入标题栏
    if (title) {
      const titleStr = ` ${title} `;
      this.write(x + 2, y, titleStr);
    }

    this.write(x, y, top);
    for (let i = 1; i < h - 1; i++) this.write(x, y + i, mid);
    this.write(x, y + h - 1, bot);
  }

  drawProgressBar(x: number, y: number, width: number, percent: number, color: string): void {
    const filled = Math.round((percent / 100) * (width - 2));
    const bar = '[' + '■'.repeat(filled) + ' '.repeat(width - 2 - filled) + ']';
    this.write(x, y, bar);
    this.write(x + 1, y, `${color}${'■'.repeat(filled)}\x1b[0m`);
  }

  hideCursor(): void { process.stdout.write(`${this.ESC}[?25l`); }
  showCursor(): void { process.stdout.write(`${this.ESC}[?25h`); }
}

// 使用示例
const canvas = new TerminalCanvas();
canvas.hideCursor();
canvas.clear();
canvas.drawBox(5, 3, 40, 10, '我的 TUI 画布');
canvas.write(10, 5, 'Hello TUI!');
canvas.write(8, 7, '\x1b[32m绿色文字\x1b[0m');
canvas.drawProgressBar(8, 9, 30, 65, '\x1b[32m');
canvas.write(5, 14, '按任意键退出...');

// 等待按键后恢复
process.stdin.setRawMode(true);
process.stdin.resume();
process.stdin.once('data', () => {
  canvas.showCursor();
  canvas.clear();
  process.stdin.setRawMode(false);
  process.exit(0);
});
```

### 2.2.5 CJK（中文/日文/韩文）字符处理

对于中/日/韩（CJK）用户，终端中的 CJK 字符处理是一个关键问题。CJK 字符在终端中占用**两个英文字符宽度**（即 "wide" 或 "fullwidth" 字符），这给 TUI 布局带来了特殊挑战。

#### CJK 字符的宽度问题

```typescript
// CJK 字符宽度检测
function isWideChar(char: string): boolean {
  const code = char.codePointAt(0) ?? 0;
  // CJK 统一表意文字 (CJK Unified Ideographs)
  if (code >= 0x4E00 && code <= 0x9FFF) return true;
  // CJK 扩展 A
  if (code >= 0x3400 && code <= 0x4DBF) return true;
  // CJK 扩展 B
  if (code >= 0x20000 && code <= 0x2A6DF) return true;
  // 全角标点符号 (Fullwidth Punctuation)
  if (code >= 0xFF01 && code <= 0xFF60) return true;
  // CJK 兼容表意文字
  if (code >= 0xF900 && code <= 0xFAFF) return true;
  // 全角空格
  if (code === 0x3000) return true;
  return false;
}

function stringWidth(str: string): number {
  let width = 0;
  for (const char of str) {
    width += isWideChar(char) ? 2 : 1;
  }
  return width;
}

// 应用：CJK 感知的文本截断
function truncateByWidth(text: string, maxWidth: number): string {
  let result = '';
  let currentWidth = 0;
  for (const char of text) {
    const charWidth = isWideChar(char) ? 2 : 1;
    if (currentWidth + charWidth > maxWidth) break;
    result += char;
    currentWidth += charWidth;
  }
  return result;
}
```

#### CJK 在 TUI 布局中的注意事项

| 问题 | 说明 | 解决方案 |
|------|------|---------|
| **列对齐错误** | CJK 占 2 列，按 `String.length` 计算会错位 | 使用 `stringWidth()` 函数计算实际宽度 |
| **光标定位偏移** | 光标移动到 CJK 字符中间会导致乱码 | 始终将光标定位到 CJK 字符的起始列 |
| **换行断词** | CJK 字符在行尾断开可能显示异常 | 避免在 CJK 字符中间换行 |
| **Emoji 组合** | Emoji 可能占 2 列，且含零宽连接符序列 | 使用专门的库如 `grapheme-splitter` |
| **输入法集成** | IME 输入法在原始模式下可能不正常 | 保留终端原始模式的 IME 支持 |

#### CJK 友好的布局计算

```typescript
// CJK 感知的文本对齐
function padRight(text: string, width: number): string {
  const textWidth = stringWidth(text);
  const padding = Math.max(0, width - textWidth);
  return text + ' '.repeat(padding);
}

function padLeft(text: string, width: number): string {
  const textWidth = stringWidth(text);
  const padding = Math.max(0, width - textWidth);
  return ' '.repeat(padding) + text;
}

// 示例：CJK 表格对齐
function formatTableRow(cells: string[], columnWidths: number[]): string {
  return cells.map((cell, i) => padRight(cell, columnWidths[i])).join(' │ ');
}

const headers = ['文件名', '大小', '状态'];
const widths = [20, 10, 10];
console.log(formatTableRow(headers, widths)); // 文件名正确对齐
console.log(formatTableRow(['项目文档.md', '2.5 KB', '已完成'], widths));
```

#### 终端 CJK 兼容性测试

```typescript
// 检测终端 CJK 支持
function testCJKSupport(): boolean {
  // 写一个 CJK 字符串然后读取光标位置
  const testStr = '中文测试';
  process.stdout.write(`\x1b[6n${testStr}`);

  // 如果终端正确报告光标在 (行, 列+len*2)，则支持 CJK
  // 否则光标可能只移动了 len 列
  return true; // 简化示例：现代终端基本都支持 CJK
}

// 推荐：使用 `wcwidth` 或 `string-width` npm 包
// npm install string-width
// import stringWidth from 'string-width';
```

### 2.2.6 终端颜色空间管理

现代终端支持多种颜色空间，从最基本的 16 色到 24 位 TrueColor。颜色空间管理是 TUI 应用跨终端兼容性的核心问题。

#### 颜色空间层级

```
16 色 (ANSI 标准色)     ← 所有终端支持，最低兼容
   │
   ▼
256 色 (8 位调色板)     ← 2000 年后终端基本都支持
   │
   ▼
TrueColor (24 位/RGB)   ← 2015 年后现代终端支持
   │
   ▼
HDR/广色域              ← 2023+ 部分终端实验性支持
```

#### 颜色空间检测与降级策略

```typescript
type ColorDepth = '16' | '256' | 'truecolor';

interface ColorPreference {
  depth: ColorDepth;
  theme: 'light' | 'dark';
  contrast: 'normal' | 'high';
}

// 智能颜色适配器
class ColorManager {
  private depth: ColorDepth;
  private theme: 'light' | 'dark';

  constructor() {
    this.depth = this.detectColorDepth();
    this.theme = this.detectTheme();
  }

  private detectColorDepth(): ColorDepth {
    const term = process.env.TERM || '';
    const colorterm = process.env.COLORTERM || '';

    if (colorterm === 'truecolor' || colorterm === '24bit' ||
        term.includes('truecolor') || term.includes('24bit')) {
      return 'truecolor';
    }
    if (term.includes('256') || colorterm === '256') {
      return '256';
    }
    return '16';
  }

  private detectTheme(): 'light' | 'dark' {
    // 检查终端背景色（OSC 11），或者检查系统主题
    // 某些终端支持 `\x1b]11;?\x07` 查询背景色
    return 'dark'; // 默认深色主题
  }

  /** 根据颜色深度输出 ANSI 颜色序列 */
  color(r: number, g: number, b: number, isBg = false): string {
    const prefix = isBg ? 48 : 38;

    switch (this.depth) {
      case 'truecolor':
        return `\x1b[${prefix};2;${r};${g};${b}m`;
      case '256': {
        // 将 RGB 近似到最近的 256 色
        const index = this.rgbTo256(r, g, b);
        return `\x1b[${prefix};5;${index}m`;
      }
      case '16': {
        // 将 RGB 近似到最近的 16 色
        const index = this.rgbTo16(r, g, b);
        return `\x1b[${isBg ? index + 10 : index}m`;
      }
    }
  }

  private rgbTo256(r: number, g: number, b: number): number {
    // 6×6×6 色彩立方近似
    const ri = Math.round(r / 51);
    const gi = Math.round(g / 51);
    const bi = Math.round(b / 51);
    return 16 + ri * 36 + gi * 6 + bi;
  }

  private rgbTo16(r: number, g: number, b: number): number {
    // 简单的亮度判断，映射到 16 色中的近似色
    const avg = (r + g + b) / 3;
    if (avg > 200) return 97; // 亮白
    if (avg > 100) return 37; // 灰白
    return 30; // 黑
  }
}

// 使用示例
const cm = new ColorManager();
const redText = `${cm.color(255, 80, 80)}红色文字\x1b[0m`;
process.stdout.write(redText);
```

#### 主题色与调色板设计

```typescript
// TUI 应用调色板定义
interface Palette {
  primary: string;     // 主色（品牌色）
  secondary: string;   // 辅助色
  accent: string;      // 强调色
  background: string;  // 背景色
  foreground: string;  // 前景色
  error: string;       // 错误色
  warning: string;     // 警告色
  success: string;     // 成功色
  muted: string;       // 弱化色
}

const darkPalette: Palette = {
  primary:    '\x1b[38;2;70;130;180m',   // 钢蓝
  secondary:  '\x1b[38;2;100;149;237m',  // 矢车菊蓝
  accent:     '\x1b[38;2;255;165;0m',    // 橙色
  background: '\x1b[48;2;30;30;40m',     // 深灰蓝
  foreground: '\x1b[38;2;220;220;220m',  // 浅灰
  error:      '\x1b[38;2;255;80;80m',    // 红
  warning:    '\x1b[38;2;255;200;50m',   // 黄
  success:    '\x1b[38;2;80;200;120m',   // 绿
  muted:      '\x1b[38;2;120;120;130m',  // 灰
};

const lightPalette: Palette = {
  primary:    '\x1b[38;2;50;100;180m',
  secondary:  '\x1b[38;2;70;130;200m',
  accent:     '\x1b[38;2;200;130;0m',
  background: '\x1b[48;2;245;245;245m',
  foreground: '\x1b[38;2;30;30;30m',
  error:      '\x1b[38;2;200;50;50m',
  warning:    '\x1b[38;2;180;150;0m',
  success:    '\x1b[38;2;50;150;80m',
  muted:      '\x1b[38;2;140;140;140m',
};

function getPalette(theme: 'dark' | 'light'): Palette {
  return theme === 'dark' ? darkPalette : lightPalette;
}
```

#### 颜色管理最佳实践

| 实践 | 说明 |
|------|------|
| **首选 TrueColor** | 现代终端（Windows Terminal、iTerm2、kitty）均支持 |
| **提供降级方案** | 检测到低色深终端时自动降级 |
| **避免硬编码颜色** | 使用调色板对象而非直接写 ANSI 序列 |
| **支持亮/暗主题** | 根据终端主题自动切换调色板 |
| **测试多种终端** | Windows Terminal、iTerm2、xterm、VSCode 终端至少测试 |
| **使用 COLORTERM 环境变量** | 比 TERM 更可靠的颜色深度指示器 |

## 2.3 终端能力检测

不同终端支持不同的功能，TUI 应用需要检测并适应：

```typescript
import * as os from 'os';

interface TermCapabilities {
  trueColor: boolean;     // TrueColor (24位色) 支持
  color256: boolean;      // 256 色支持
  unicode: boolean;       // Unicode 支持
  rows: number;           // 终端行数
  cols: number;           // 终端列数
  mouse: boolean;         // 鼠标事件支持
  clipboard: boolean;     // 剪贴板支持 (OSC 52)
  hyperlinks: boolean;    // 终端超链接支持 (OSC 8)
  cursorStyle: boolean;   // 光标样式设置支持
}

function detectTerminalCapabilities(): TermCapabilities {
  const term = process.env.TERM || '';
  const colorTerm = process.env.COLORTERM || '';
  const isWindows = os.platform() === 'win32';

  return {
    // 颜色深度
    trueColor: colorTerm === 'truecolor' || colorTerm === '24bit' ||
               term.includes('truecolor') || term.includes('24bit') ||
               isWindows, // Windows Terminal 支持 TrueColor
    color256: term.includes('256') || isWindows,

    // Unicode — 检查 LANG 环境变量
    unicode: (process.env.LANG || '').includes('UTF-8') || isWindows,

    // 终端尺寸
    rows: process.stdout.rows || 24,
    cols: process.stdout.columns || 80,

    // 鼠标支持（现代终端基本都支持）
    mouse: true,

    // 剪贴板（OSC 52 序列 — 用于复制粘贴）
    clipboard: term.includes('xterm') || term.includes('tmux') ||
               term.includes('screen'),

    // 超链接（OSC 8 序列 — 点击即可打开的链接）
    hyperlinks: true, // 大多数现代终端支持

    // 光标样式设置（\x1b[ q 序列）
    cursorStyle: true,
  };
}

// 使用示例
const cap = detectTerminalCapabilities();
if (!cap.trueColor) {
  console.warn('⚠️ 当前终端不支持 TrueColor，将回退到 256 色模式');
}
console.log(`终端尺寸: ${cap.cols}x${cap.rows}`);
console.log(`Unicode: ${cap.unicode ? '✅' : '❌'}`);
```

## 2.4 原始模式（Raw Mode）

默认情况下，终端是"行缓冲"（cooked）模式——用户输入在按下 Enter 后才发送给程序。TUI 应用需要**原始模式**来实时获取每个按键：

```typescript
import { Key } from 'readline';

// ── 启用原始模式 ──
function enableRawMode(): void {
  if (process.stdin.isTTY) {
    process.stdin.setRawMode(true);
  }
  process.stdin.resume();
  process.stdin.setEncoding('utf8');
}

// ── 禁用原始模式 ──
function disableRawMode(): void {
  if (process.stdin.isTTY) {
    process.stdin.setRawMode(false);
  }
  process.stdin.pause();
}

// ── 原始模式下的按键解析 ──
enableRawMode();

process.stdin.on('data', (data: Buffer) => {
  const key = data.toString();

  // 控制字符
  if (key === '\x03') {        // Ctrl+C
    disableRawMode();
    process.exit(0);
  }
  if (key === '\x04') {        // Ctrl+D
    disableRawMode();
    process.exit(0);
  }

  // ANSI 转义序列（方向键等）
  switch (key) {
    case '\x1b[A':  console.log('↑ 上键');   break;
    case '\x1b[B':  console.log('↓ 下键');   break;
    case '\x1b[C':  console.log('→ 右键');   break;
    case '\x1b[D':  console.log('← 左键');   break;
    case '\x1b[H':  console.log('Home');      break;
    case '\x1b[F':  console.log('End');       break;
    case '\x1b[2~': console.log('Insert');    break;
    case '\x1b[3~': console.log('Delete');    break;
    case '\x1b[5~': console.log('PageUp');    break;
    case '\x1b[6~': console.log('PageDown');  break;
    case '\r':      console.log('Enter');     break;
    case '\x7f':    console.log('Backspace'); break;
    case '\t':      console.log('Tab');       break;
    default:
      if (key.length === 1) {
        console.log(`字符: ${key} (${key.charCodeAt(0)})`);
      } else {
        console.log(`原始: ${Array.from(key).map(c => c.charCodeAt(0)).join(' ')}`);
      }
  }
});
```

### Kick 序列对照表

常见键盘按键对应的 ANSI 转义序列：

| 按键 | 序列 | 说明 |
|------|------|------|
| ↑ | `\x1b[A` | 上箭头 |
| ↓ | `\x1b[B` | 下箭头 |
| → | `\x1b[C` | 右箭头 |
| ← | `\x1b[D` | 左箭头 |
| Home | `\x1b[H` 或 `\x1b[1~` | 行首 |
| End | `\x1b[F` 或 `\x1b[4~` | 行尾 |
| Insert | `\x1b[2~` | 插入键 |
| Delete | `\x1b[3~` | 删除键 |
| PageUp | `\x1b[5~` | 上翻页 |
| PageDown | `\x1b[6~` | 下翻页 |
| Enter | `\r` | 回车 |
| Tab | `\t` | 制表符 |
| Backspace | `\x7f` | 退格 |
| Escape | `\x1b` | 退出键 |
| F1-F4 | `\x1b[OP` - `\x1b[OS` | 功能键 |

### 原始模式 vs 行缓冲模式对比

| 特性 | 行缓冲模式（Cooked） | 原始模式（Raw） |
|------|-------------------|----------------|
| 输入即时性 | 按 Enter 后发送 | 每键即时发送 |
| 回显 | 自动显示输入 | 需手动控制 |
| 行编辑 | 内置（退格、删除） | 需自己实现 |
| Ctrl+C | 发送 SIGINT | 需手动处理 |
| Ctrl+S/Q | 流控（XON/XOFF） | 可自由使用 |
| Ctrl+Z | 发送 SIGTSTP | 需手动处理 |
| 适合场景 | CLI 命令 | TUI 应用 |

## 2.5 终端事件

### 2.5.1 鼠标事件

现代终端支持在原始模式下启用鼠标事件跟踪。共有三种鼠标协议：

| 协议 | 启用序列 | 特点 |
|------|---------|------|
| **X10** | `\x1b[?9h` | 仅按下事件，5 位编码 |
| **VT200** | `\x1b[?1000h` | 按下 + 释放，5 位编码 |
| **SGR（推荐）** | `\x1b[?1006h` | 扩展坐标 (≥223 列)，标准编码 |

```typescript
function enableMouse(): void {
  // 启用鼠标事件（需要已在原始模式下）
  process.stdout.write('\x1b[?1000h');   // 鼠标按下/释放事件
  process.stdout.write('\x1b[?1002h');   // 鼠标拖动事件
  process.stdout.write('\x1b[?1003h');   // 任何鼠标移动（可选，可能消耗性能）
  process.stdout.write('\x1b[?1006h');   // SGR 扩展鼠标格式（推荐）
}

function disableMouse(): void {
  process.stdout.write('\x1b[?1000l');
  process.stdout.write('\x1b[?1002l');
  process.stdout.write('\x1b[?1003l');
  process.stdout.write('\x1b[?1006l');
}

// 处理鼠标事件
process.stdin.on('data', (data: Buffer) => {
  const str = data.toString();

  // SGR 鼠标格式: \x1b[<Cb>;<x>;<y>M (按下) 或 m (释放)
  const mouseMatch = str.match(/\x1b\[<(\d+);(\d+);(\d+)([Mm])/);
  if (mouseMatch) {
    const [, button, x, y, action] = mouseMatch;
    const btn = Number(button);
    const isPress = action === 'M';

    let btnName = '左键';
    if (btn === 1) btnName = '中键';
    if (btn === 2) btnName = '右键';
    if (btn >= 64) btnName = '滚轮向上';
    if (btn >= 65) btnName = '滚轮向下';

    console.log(`鼠标: ${btnName} ${isPress ? '按下' : '释放'} 于 (${x}, ${y})`);
  }
});
```

### 2.5.2 窗口大小变化

```typescript
process.stdout.on('resize', () => {
  const { rows, columns } = process.stdout;
  console.log(`终端大小调整: ${columns}x${rows}`);

  // TUI 中需要重新渲染
  // screen.render();
});
```

### 2.5.3 焦点事件

部分终端支持焦点变化事件，这在 TUI 中非常有用（例如：失去焦点时暂停动画）：

```typescript
// 启用/禁用焦点事件
process.stdout.write('\x1b[?1004h');   // 启用焦点事件

process.stdin.on('data', (data: Buffer) => {
  const str = data.toString();
  if (str === '\x1b[I') console.log('终端获得焦点 — 恢复动画');
  if (str === '\x1b[O') console.log('终端失去焦点 — 暂停动画');
});

// 注意：不是所有终端都支持焦点事件
// 测试方法：在支持 SGR 鼠标事件的终端中通常也支持焦点事件
```

## 2.6 终端兼容性矩阵

| 功能 | xterm | iTerm2 | Windows Terminal | VSCode 终端 | tmux | Alacritty | kitty |
|------|-------|--------|-----------------|------------|------|-----------|-------|
| 16 色 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 256 色 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| TrueColor | ✅ | ✅ | ✅ | ✅ | ⚠️ 需配置 | ✅ | ✅ |
| Unicode | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| CJK 字符 | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ 部分 | ✅ |
| 鼠标事件 | ✅ | ✅ | ✅ | ✅ | 需配置 | ✅ | ✅ |
| 剪贴板 (OSC 52) | ✅ | ✅ | ✅ | ❌ | 需配置 | ✅ | ✅ |
| 超链接 (OSC 8) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 焦点事件 | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ |

## 2.7 终端复用器集成（tmux/screen）

**tmux地址：https://github.com/tmux/tmux**

### tmux 与 TUI 应用的交互

`tmux` 是终端复用器的行业标准，许多开发者将 TUI 应用运行在 tmux 会话中。这种组合带来了一些需要特别注意的问题。

#### tmux 转发 ANSI 序列的机制

tmux 在终端和 TUI 应用之间作为中间层，它会解析并重新编码 ANSI 转义序列：

```
TUI 应用 ←→ tmux (解析/重编码) ←→ 终端模拟器
```

```
TUI 应用发送:
  \x1b[38;2;255;0;0m       (TrueColor 红色)
         │
         ▼
  tmux 检查配置:
    - 如果终端支持 TrueColor: 透传
    - 如果终端只支持 256 色: 降级转换
    - 默认: 可能修改序列
         │
         ▼
  实际发送到终端:
  \x1b[38;5;196m            (256 色近似红色)
```

#### tmux 环境下的颜色兼容性

```typescript
// 检测是否运行在 tmux 中
function isInTmux(): boolean {
  return process.env.TMUX !== undefined;
}

// 获取 tmux 版本
function getTmuxVersion(): string | null {
  return process.env.TMUX?.split(',')[2] ?? null;
}

// 检测 tmux 是否配置了 TrueColor 支持
function tmuxSupportsTrueColor(): boolean {
  // 需要: tmux >= 2.2 且设置 'set -g default-terminal "tmux-256color"'
  const term = process.env.TERM || '';
  return term === 'tmux-256color' || term === 'screen-256color';
}

// tmux 感知的颜色输出
function safeColor(r: number, g: number, b: number, isBg = false): string {
  const prefix = isBg ? 48 : 38;
  const isTmux = isInTmux();
  const tmuxTrueColor = tmuxSupportsTrueColor();

  if (isTmux && !tmuxTrueColor) {
    // 在 tmux 中且未配置 TrueColor 时降级到 256 色
    const index = rgbTo256(r, g, b);
    return `\x1b[${prefix};5;${index}m`;
  }
  return `\x1b[${prefix};2;${r};${g};${b}m`;
}

function rgbTo256(r: number, g: number, b: number): number {
  const ri = Math.round(r / 51);
  const gi = Math.round(g / 51);
  const bi = Math.round(b / 51);
  return 16 + ri * 36 + gi * 6 + bi;
}
```

#### tmux 环境下的事件处理

```typescript
// tmux 会修改鼠标事件序列，需额外处理
class TmuxEventAdapter {
  private readonly inTmux: boolean;

  constructor() {
    this.inTmux = isInTmux();
  }

  /** 启用鼠标事件（兼容 tmux） */
  enableMouse(): void {
    if (this.inTmux) {
      // tmux 需要先启用 SGR 鼠标模式，再启用应用模式
      process.stdout.write('\x1b[?1006h');  // SGR 模式
      process.stdout.write('\x1b[?1000h');  // 基本鼠标事件
      // tmux 对鼠标事件的包装格式
      process.stdout.write('\x1b[?1002h');  // 拖动事件
    } else {
      process.stdout.write('\x1b[?1000h');
      process.stdout.write('\x1b[?1006h');
    }
  }

  /** 解析 tmux 包装后的鼠标事件 */
  parseMouseEvent(raw: string): { button: number; x: number; y: number } | null {
    // tmux 可能添加 \x1b[ 前缀包装
    // 标准格式: \x1b[<Mx;y;zM 或 m
    const match = raw.match(/\x1b\[<(\d+);(\d+);(\d+)([Mm])/);
    if (match) {
      return {
        button: Number(match[1]),
        x: Number(match[2]),
        y: Number(match[3]),
      };
    }
    return null;
  }
}
```

#### tmux 配置最佳实践

在 `~/.tmux.conf` 中配置 TUI 友好的选项：

```bash
# 启用 TrueColor 支持
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",*256col*:Tc"
set -ga terminal-overrides '*:Ss=\E[%p1%d q:Se=\E[ q'

# 启用鼠标支持（TUI 需要）
set -g mouse on

# 设置 ESC 超时（避免 TUI 卡顿）
set -sg escape-time 10

# 启用剪贴板转发
set -g set-clipboard on
```

#### tmux vs screen 对比

| 特性 | tmux | screen | 对 TUI 的影响 |
|------|------|--------|-------------|
| **TrueColor 转发** | 支持（需配置） | 不支持 | tmux 可保留 24 位色 |
| **鼠标事件** | 完整支持 | 有限支持 | tmux 更适应 TUI 鼠标操作 |
| **ESC 超时** | 可配置（默认 500ms） | 固定 | tmux 降低超时后可减少延迟 |
| **剪贴板集成** | OSC 52 支持 | 有限 | tmux 支持 TUI 复制粘贴 |
| **嵌套会话** | 原生支持 | 有限 | tmux 适合远程开发场景 |
| **性能开销** | 低 | 低 | 两者相差不大 |

### screen 特别注意事项

虽然 `screen` 已较少使用，但仍有场景需要兼容：

```typescript
// 检测 screen
function isInScreen(): boolean {
  return process.env.STY !== undefined;
}

// screen 中需要避免的 ANSI 序列
const screenUnsafe: string[] = [
  '\x1b[?1004h',  // 焦点事件（screen 不支持）
  '\x1b[?1006h',  // SGR 鼠标（screen 不支持）
  '\x1b[?25h',    // 光标显示（部分版本有问题）
];
```

## 2.8 双缓冲与屏幕管理

复杂 TUI 通常使用**双缓冲**技术避免闪烁——在内存中构建完整画面，然后一次性输出：

```typescript
class ScreenBuffer {
  private buffer: string[][] = [];
  private rows: number;
  private cols: number;

  constructor() {
    this.rows = process.stdout.rows || 24;
    this.cols = process.stdout.columns || 80;
    this.clear();
  }

  clear(): void {
    this.buffer = Array.from({ length: this.rows }, () =>
      Array(this.cols).fill(' ')
    );
  }

  set(x: number, y: number, char: string): void {
    if (x >= 0 && x < this.cols && y >= 0 && y < this.rows) {
      this.buffer[y][x] = char[0] || ' ';
    }
  }

  write(x: number, y: number, text: string): void {
    for (let i = 0; i < text.length; i++) {
      this.set(x + i, y, text[i]);
    }
  }

  flush(): void {
    const output = this.buffer.map(row => row.join('')).join('\n');
    process.stdout.write(`\x1b[H${output}`);
  }

  resize(): void {
    this.rows = process.stdout.rows || 24;
    this.cols = process.stdout.columns || 80;
    this.clear();
  }
}

// 差分更新 — 只输出变化的行（性能优化）
class DiffScreenBuffer extends ScreenBuffer {
  private previousBuffer: string[][] = [];

  flush(): void {
    const output: string[] = [];
    for (let y = 0; y < this.rows; y++) {
      const currentRow = this.buffer[y].join('');
      const prevRow = this.previousBuffer[y]?.join('') || '';
      if (currentRow !== prevRow) {
        output.push(`\x1b[${y + 1};1H${currentRow}`);
      }
    }
    if (output.length > 0) {
      process.stdout.write(output.join(''));
    }
    // 保存当前缓冲作为下一次的"前一帧"
    this.previousBuffer = this.buffer.map(row => [...row]);
  }
}
```

### 差分更新 vs 全量更新

| 方式 | 输出量 | 适用场景 | 闪烁风险 |
|------|--------|---------|---------|
| **全量更新** | 整屏 (~2KB) | 低频更新、初始渲染 | 高 |
| **差分更新** | 仅变化行 (~100B) | 高频流式更新 | 低 |
| **smartCSR (blessed)** | 仅变化字符 | 所有场景 | 极低 |

### 2.8.1 终端备选缓冲（Alternate Screen Buffer）

备选缓冲（Alternate Screen Buffer）是 TUI 应用的基石技术。它允许你的应用"独占"整个终端屏幕，退出时恢复原始内容。

#### 工作原理

终端维护两个缓冲区：

- **主缓冲（Main Buffer）**：正常的回滚式缓冲，显示 shell 历史、命令输出等
- **备选缓冲（Alternate Buffer）**：全屏程序使用的独立缓冲，没有回滚

```
切换前（主缓冲）:                   切换后（备选缓冲）:
┌──────────────────┐               ┌──────────────────┐
│ $ ls -la         │               │ ┌── TUI App ──┐  │
│ -rw-r--r-- 1 ... │               │ │             │  │
│ $ git status     │    ───→       │ │             │  │
│ On branch main   │               │ │             │  │
│ $ _              │               │ └─────────────┘  │
│ [回滚区: 1000行]  │               │ (退出后恢复原内容)  │
└──────────────────┘               └──────────────────┘
```

#### 备选缓冲控制序列

```typescript
// 基本控制
function enterAlternateBuffer(): void {
  process.stdout.write('\x1b[?1049h');  // 进入备选缓冲（清屏 + 保存光标）
}

function exitAlternateBuffer(): void {
  process.stdout.write('\x1b[?1049l');  // 退出备选缓冲（恢复主缓冲）
}

// 旧式控制（部分终端仍在使用）
function enterAlternateBufferLegacy(): void {
  process.stdout.write('\x1b[?47h');    // 切换至备选缓冲
}

function exitAlternateBufferLegacy(): void {
  process.stdout.write('\x1b[?47l');    // 切换回主缓冲
}
```

#### 异常退出保护

TUI 应用必须确保在异常退出时恢复主缓冲，否则用户会看到一个"空白终端"：

```typescript
class AlternateBufferManager {
  private entered = false;

  enter(): void {
    if (this.entered) return;
    enterAlternateBuffer();
    this.entered = true;

    // 注册各种退出处理
    this.registerCleanup();
  }

  exit(): void {
    if (!this.entered) return;
    exitAlternateBuffer();
    this.entered = false;
  }

  private registerCleanup(): void {
    // Ctrl+C 处理
    process.on('SIGINT', () => {
      this.exit();
      process.exit(130);
    });

    // SIGTERM 处理
    process.on('SIGTERM', () => {
      this.exit();
      process.exit(0);
    });

    // 未捕获异常处理
    process.on('uncaughtException', (error: Error) => {
      this.exit();
      console.error('未捕获的异常:', error);
      process.exit(1);
    });

    // 未捕获 Promise 拒绝
    process.on('unhandledRejection', (reason: unknown) => {
      this.exit();
      console.error('未处理的 Promise 拒绝:', reason);
      process.exit(1);
    });

    // 正常退出
    process.on('exit', () => {
      // 注意: exit 事件中只能执行同步操作
      // ANSI 序列仍然可以发送（stdout 是同步的）
      if (this.entered) {
        process.stdout.write('\x1b[?1049l');
      }
    });
  }
}

// 使用示例
const bufferManager = new AlternateBufferManager();
bufferManager.enter();

// ... 运行 TUI 应用 ...

// 正常退出
// bufferManager.exit();
```

#### 备选缓冲最佳实践

| 实践 | 说明 |
|------|------|
| **使用 `\x1b[?1049h/l`** | 这是最广泛支持的序列（合并保存光标 + 切换缓冲） |
| **注册所有退出信号** | SIGINT、SIGTERM、uncaughtException、exit 全覆盖 |
| **仅备选缓冲中修改终端状态** | 在主缓冲中不更改终端模式（原始模式、鼠标事件等） |
| **退出前重置终端状态** | 恢复光标、显示光标、禁用鼠标、重置颜色 |
| **备选缓冲嵌套** | 避免重复进入（检测 `this.entered` 标志） |
| **测试回滚兼容** | 不同终端退出后回滚表现略有差异 |

#### 备选缓冲与 tmux

在 tmux 中，备选缓冲的行为有所不同：

```typescript
function isTmuxManaged(): boolean {
  // tmux 会拦截备选缓冲切换，用其内部的 pane 缓冲替代
  return process.env.TMUX !== undefined;
}

// tmux 下的备选缓冲仍在工作，但行为稍有不同：
// 1. 退出后不显示切换前的"闪回"
// 2. 备选缓冲内容可以通过 tmux 回滚查看
// 3. 窗口大小变化时 tmux 管理重排
```

## 2.9 TUI 框架的职责

手动使用 ANSI 转义码可以控制终端，但构建复杂 TUI 时，框架会帮你处理以下问题：

| 职责 | 手动实现 | 框架处理 |
|------|---------|---------|
| **屏幕缓冲管理** | 自己维护双缓冲 | 内置差分更新 |
| **布局计算** | 手算坐标和尺寸 | 自动相对定位 |
| **事件抽象** | 解析 ANSI 序列 | 统一键盘/鼠标事件 |
| **样式系统** | 管理 ANSI 代码 | 声明式样式 |
| **焦点管理** | 手动 Tab 切换 | 内置焦点链 |
| **组件系统** | 从零构建 | 丰富的组件库 |
| **滚动处理** | 视口计算 | 内置虚拟滚动 |
| **鼠标支持** | 解析 SGR 协议 | 自动转换事件 |

这正是我们接下来要学习的 **blessed** 框架所做的事情。

## 2.10 Windows 特别注意事项

### 2.10.1 Windows Terminal 的优势

Windows Terminal 是 Windows 上最佳的 TUI 开发终端：
- 原生 TrueColor 支持
- 完整 Unicode/Emoji 支持（需安装终端字体）
- 鼠标事件原生支持
- JSON 配置文件，高度可定制

### 2.10.2 已知问题与解决方案

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| Ctrl+S 不发送 | Windows 无流控 | 使用其他快捷键 |
| Emoji 显示为方框 | 缺少字体 | 安装 Cascadia Code / Nerd Font |
| ANSI 序列不生效 | 旧版 CMD/PowerShell | 使用 Windows Terminal |
| 中文乱码 | 编码问题 | 终端设置 UTF-8 |
| `setRawMode` 不支持 | 非 TTY 输入 | 检查 `isTTY` |

## 2.11 终端会话持久化

TUI 应用通常是长时间运行的（如聊天界面、监控面板）。会话持久化机制确保用户断开后能恢复应用状态。

### 使用 tmux/screen 进行会话管理

最简单的持久化方式是将 TUI 应用运行在 tmux 或 screen 会话中：

```bash
# 创建持久化 tmux 会话并在其中启动 TUI 应用
tmux new-session -s my-tui-app 'node dist/chat-tui.js'

# 断线后重新连接
tmux attach -t my-tui-app

# 在脚本中安全重启
tmux new-session -d -s my-tui-app 'node dist/chat-tui.js; tmux wait-for -S tui-done'
```

### 应用级别的会话状态持久化

```typescript
import * as fs from 'fs';
import * as path from 'path';

// 会话状态接口
interface SessionState {
  sessionId: string;
  createdAt: string;
  updatedAt: string;
  conversationHistory: Array<{
    role: 'user' | 'assistant' | 'system';
    content: string;
    timestamp: number;
  }>;
  uiState: {
    scrollPosition: number;
    activePanel: string;
    inputBuffer: string;
    theme: 'dark' | 'light';
  };
  toolCallHistory: Array<{
    toolName: string;
    args: Record<string, unknown>;
    result: string;
    timestamp: number;
  }>;
  metadata: Record<string, unknown>;
}

// 会话管理器
class SessionManager {
  private readonly sessionDir: string;
  private currentSession: SessionState | null = null;

  constructor(sessionDir?: string) {
    this.sessionDir = sessionDir || path.join(process.cwd(), '.sessions');
    this.ensureSessionDir();
  }

  private ensureSessionDir(): void {
    if (!fs.existsSync(this.sessionDir)) {
      fs.mkdirSync(this.sessionDir, { recursive: true });
    }
  }

  /** 创建新会话 */
  createSession(): SessionState {
    const session: SessionState = {
      sessionId: `session_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      conversationHistory: [],
      uiState: {
        scrollPosition: 0,
        activePanel: 'chat',
        inputBuffer: '',
        theme: 'dark',
      },
      toolCallHistory: [],
      metadata: {},
    };
    this.currentSession = session;
    this.saveSession();
    return session;
  }

  /** 保存当前会话到磁盘 */
  saveSession(): void {
    if (!this.currentSession) return;
    this.currentSession.updatedAt = new Date().toISOString();
    const filePath = path.join(this.sessionDir, `${this.currentSession.sessionId}.json`);
    fs.writeFileSync(filePath, JSON.stringify(this.currentSession, null, 2), 'utf8');
  }

  /** 从磁盘加载会话 */
  loadSession(sessionId: string): SessionState | null {
    const filePath = path.join(this.sessionDir, `${sessionId}.json`);
    if (!fs.existsSync(filePath)) return null;
    try {
      const data = fs.readFileSync(filePath, 'utf8');
      this.currentSession = JSON.parse(data) as SessionState;
      return this.currentSession;
    } catch {
      return null;
    }
  }

  /** 列出所有已保存的会话 */
  listSessions(): Array<{id: string; createdAt: string; messageCount: number}> {
    const files = fs.readdirSync(this.sessionDir)
      .filter(f => f.endsWith('.json'));

    return files.map(file => {
      const filePath = path.join(this.sessionDir, file);
      try {
        const data = JSON.parse(fs.readFileSync(filePath, 'utf8')) as SessionState;
        return {
          id: data.sessionId,
          createdAt: data.createdAt,
          messageCount: data.conversationHistory.length,
        };
      } catch {
        return null;
      }
    }).filter((s): s is NonNullable<typeof s> => s !== null)
      .sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
  }

  /** 添加对话消息 */
  addMessage(msg: SessionState['conversationHistory'][0]): void {
    if (!this.currentSession) this.createSession();
    this.currentSession!.conversationHistory.push(msg);
    this.saveSession();
  }

  /** 更新 UI 状态 */
  updateUIState(partial: Partial<SessionState['uiState']>): void {
    if (!this.currentSession) return;
    Object.assign(this.currentSession.uiState, partial);
    this.saveSession();
  }

  /** 自动保存（每 30 秒或关键操作时触发） */
  enableAutoSave(interval = 30000): () => void {
    const timer = setInterval(() => this.saveSession(), interval);
    return () => clearInterval(timer);
  }
}
```

### 崩溃恢复与自动重连

```typescript
// 崩溃检测与恢复
class CrashRecovery {
  private readonly stateFile: string;

  constructor(sessionDir: string) {
    this.stateFile = path.join(sessionDir, '.crash_recovery');
  }

  /** 标记应用正在运行 */
  markRunning(): void {
    const state = {
      pid: process.pid,
      startTime: Date.now(),
      sessionId: new Date().toISOString(),
    };
    fs.writeFileSync(this.stateFile, JSON.stringify(state));
  }

  /** 标记应用正常退出 */
  markExited(): void {
    if (fs.existsSync(this.stateFile)) {
      fs.unlinkSync(this.stateFile);
    }
  }

  /** 检查上次是否崩溃 */
  checkCrash(): boolean {
    if (!fs.existsSync(this.stateFile)) return false;
    try {
      const state = JSON.parse(fs.readFileSync(this.stateFile, 'utf8'));
      const { pid } = state;
      // 检查进程是否还在运行
      try {
        process.kill(pid, 0); // 进程存在
        return false;
      } catch {
        return true; // 进程不存在，说明上次崩溃了
      }
    } catch {
      return false;
    }
  }

  /** 启动恢复流程 */
  async recover(): Promise<void> {
    if (!this.checkCrash()) return;
    console.log('检测到上次异常退出，正在恢复...');
    // 在实际实现中，这里可以：
    // 1. 读取最近的自动保存
    // 2. 恢复对话上下文
    // 3. 通知 LLM 重新加载上下文
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
}
```

### 持久化最佳实践

| 实践 | 说明 |
|------|------|
| **定期自动保存** | 每 30-60 秒或每次对话轮次后保存 |
| **使用 JSON 格式** | 易于调试和迁移 |
| **限制历史大小** | 保留最近 200 条消息，超出时归档 |
| **异步写入** | 使用 `writeFile` 而非 `writeFileSync`（生产环境） |
| **崩溃检测标志** | 启动时写 PID，正常退出时删除 |
| **tmux 守护** | 将 TUI 包装在 tmux 中实现连接/断线重连 |
| **增量保存** | 只保存变化部分而非完整状态（大对话场景） |

**下一步：** [第三章：TUI 框架与技术选型](03-tui-frameworks.md)