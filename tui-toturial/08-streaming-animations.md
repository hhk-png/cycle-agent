# 第八章 流式响应与动画

## 8.1 TUI 中的动画基础

终端动画的核心原理是**快速更新屏幕内容**。利用 ANSI 转义码和 blessed 的渲染机制，我们可以实现丰富的动画效果。

### 8.1.1 简单动画示例

```typescript
// 一个简单的字符旋转动画
const frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
let i = 0;

const timer = setInterval(() => {
  process.stdout.write(`\r${frames[i++ % frames.length]} 加载中...`);
}, 100);

// 3 秒后停止
setTimeout(() => {
  clearInterval(timer);
  process.stdout.write('\r✅ 完成!      \n');
}, 3000);
```

**关键点：** `\r`（回车符）将光标移回行首，使下一帧覆盖当前行——这就是终端动画的核心机制。

### 8.1.2 终端动画 vs GUI 动画

| 维度 | GUI 动画 | TUI 动画 |
|------|---------|---------|
| 渲染方式 | 像素级重绘 | 字符级覆盖 |
| 帧率 | 60fps | 10-30fps（合理范围） |
| 资源消耗 | GPU 加速，高功耗 | CPU 轻量，几乎无 GPU |
| 实现复杂度 | 复杂（CSS/Canvas） | 简单（定时器 + 字符） |
| 动画能力 | 无限（渐变、3D） | 有限（旋转、进度、闪烁） |

## 8.2 打字机效果（流式文本输出）

打字机效果是大模型对话 TUI 中最核心的动画——文本逐字出现，模拟 LLM 的真实生成过程。

### 8.2.1 基础实现

```typescript
/**
 * 打字机效果：逐字更新 UI
 * 每次调用更新完整文本（而非追加字符），确保 UI 一致性
 */
private updateAssistantMessage(content: string): void {
  if (!this.currentAssistantMsgEl) {
    // 首次创建消息元素
    this.currentAssistantMsgEl = blessed.box({
      parent: this.chatBox,
      top: 0,
      left: 0,
      width: '100%-2',
      height: 'shrink',
      content: `{green-fg}{bold}🤖 AI{/bold}{/green-fg}\n${this.escapeTags(content)}`,
      style: { fg: 'white', bg: '#1a1a1a' },
      tags: true,
      wrap: true,
      shrink: true,
    });
    this.addSpacer();
  } else {
    // 增量更新：替换内容，保留元素引用
    this.currentAssistantMsgEl.setContent(
      `{green-fg}{bold}🤖 AI{/bold}{/green-fg}\n${this.escapeTags(content)}`
    );
  }

  // 每次更新后自动滚动到底部
  this.chatBox.setScrollPerc(100);
  this.screen.render();
}
```

### 8.2.2 渲染性能优化

频繁调用 `screen.render()` 在字符密集时会有性能问题，以下是三种优化方案：

**方案 1：节流渲染（Throttle）**

```typescript
private lastRenderTime = 0;
private readonly MIN_RENDER_INTERVAL = 33; // ~30fps

private throttledRender(): void {
  const now = Date.now();
  if (now - this.lastRenderTime >= this.MIN_RENDER_INTERVAL) {
    this.screen.render();
    this.lastRenderTime = now;
  }
}
```

**方案 2：批处理更新（Batch）**

```typescript
private batchTimeout: ReturnType<typeof setTimeout> | null = null;
private pendingContent = '';

private batchUpdate(chunk: string): void {
  this.pendingContent += chunk;

  if (!this.batchTimeout) {
    this.batchTimeout = setTimeout(() => {
      this.flushBatch();
    }, 50); // 每 50ms 批量刷新一次
  }
}

private flushBatch(): void {
  if (this.pendingContent) {
    this.updateAssistantMessage(this.pendingContent);
    this.pendingContent = '';
  }
  this.batchTimeout = null;
}
```

**方案 3：差分更新（利用 smartCSR）**

```typescript
// blessed 内置 smartCSR 自动处理差分更新
// 只需确保：
const screen = blessed.screen({
  smartCSR: true,    // 开启智能光标保存/恢复
  fullUnicode: true, // 完整 Unicode 支持（emoji/CJK）
  fastCSR: true,     // 快速 CSR 模式
});
// 剩下的 blessed 会自动处理
```

### 8.2.3 流式文本的光标指示

流式输出中的光标（`▊`）能增强"正在生成中"的感知：

```typescript
// 在流式文本末尾添加闪烁光标
private updateAssistantMessage(content: string): void {
  // 在流式模式末尾添加光标指示器
  const displayContent = this.state === 'streaming'
    ? content + '▊'  // 使用半块字符作为光标
    : content;

  // ... 更新 UI
}
```

### 8.2.4 打字机效果变体

基础逐字输出虽然可用，但缺乏表现力。以下是多种打字机变体，可为不同场景选择合适节奏。

**速度模式：加速、减速、波浪**

```typescript
type SpeedPattern = 'uniform' | 'accelerating' | 'decelerating' | 'wave';

/**
 * 根据速度模式和总进度计算当前字符的延迟
 * @param index 当前字符索引
 * @param total 字符总数
 * @param baseDelay 基础延迟 (ms)
 * @param pattern 速度模式
 */
function getTypewriterDelay(
  index: number,
  total: number,
  baseDelay: number,
  pattern: SpeedPattern = 'uniform',
): number {
  const progress = index / Math.max(total - 1, 1);

  switch (pattern) {
    case 'accelerating':
      // 从慢到快：开头 2x 延迟，结尾 0.5x 延迟
      return baseDelay * (2 - 1.5 * progress);
    case 'decelerating':
      // 从快到慢：开头 0.5x 延迟，结尾 2x 延迟
      return baseDelay * (0.5 + 1.5 * progress);
    case 'wave':
      // 波浪节奏：快慢交替，模拟自然语言韵律
      return baseDelay * (0.6 + 0.8 * Math.sin(progress * Math.PI * 4) ** 2);
    case 'uniform':
    default:
      return baseDelay;
  }
}

// 使用示例
async function* acceleratedTypewriter(text: string): AsyncGenerator<LLMEvent> {
  for (let i = 0; i < text.length; i++) {
    yield { type: 'text', content: text[i] };
    const delay = getTypewriterDelay(i, text.length, 20, 'accelerating');
    await delay(delay);
  }
}
```

**标点暂停**

在句号、逗号、问号后插入更长的停顿，让阅读体验更自然：

```typescript
const PUNCTUATION_PAUSE: Record<string, number> = {
  '.': 200,   // 句号 — 较长停顿
  '!': 250,   // 感叹号 — 强调停顿
  '?': 250,   // 问号 — 思考停顿
  ',': 120,   // 逗号 — 短停顿
  ';': 120,   // 分号 — 短停顿
  ':': 100,   // 冒号 — 短暂停顿
  '\n': 300,  // 换行 — 段落停顿
};

async function* typewriterWithPunctuationPause(
  text: string,
  charDelay: number = 25,
): AsyncGenerator<LLMEvent> {
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    yield { type: 'text', content: ch };
    const extraPause = PUNCTUATION_PAUSE[ch] ?? 0;
    await delay(charDelay + extraPause);
  }
}
```

**逐词分块 vs 逐字分块**

对比两种分块策略对用户体验的影响：

```typescript
/**
 * 逐词分块：每块包含一个或多个完整词语
 * 优点：阅读流畅，语义完整
 * 缺点：首字延迟较长
 */
async function* wordByWord(text: string): AsyncGenerator<LLMEvent> {
  const words = text.split(/(\s+)/); // 保留空白分隔符
  for (const word of words) {
    if (word.length === 0) continue;
    yield { type: 'text', content: word };
    await delay(40 + Math.random() * 30);
  }
}

/**
 * 逐字分块：每次输出一个字符
 * 优点：实时感强，模拟 LLM token 生成
 * 缺点：阅读体验破碎
 */
async function* charByChar(text: string): AsyncGenerator<LLMEvent> {
  for (const ch of text) {
    yield { type: 'text', content: ch };
    await delay(15 + Math.random() * 25);
  }
}

/**
 * 混合策略：短词逐词输出，长词逐字输出
 * 平衡流畅度与实时感
 */
async function* hybridTypewriter(text: string): AsyncGenerator<LLMEvent> {
  const words = text.split(/(\s+)/);
  for (const word of words) {
    if (word.length === 0) continue;

    if (word.length <= 2) {
      // 短词（2 字符以内）：整体输出
      yield { type: 'text', content: word };
      await delay(30 + Math.random() * 20);
    } else {
      // 长词：前几个字符快速逐字，最后整体
      const fastChars = word.slice(0, -1);
      for (const ch of fastChars) {
        yield { type: 'text', content: ch };
        await delay(10 + Math.random() * 15);
      }
      yield { type: 'text', content: word.slice(-1) };
      await delay(30 + Math.random() * 30);
    }
  }
}
```

**随机速度变化与种子控制**

可复现的随机延迟变化，用于测试和一致体验：

```typescript
/**
 * 带种子的伪随机数生成器 (Mulberry32)
 */
function createSeededRandom(seed: number): () => number {
  let s = seed | 0;
  return () => {
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

async function* typewriterWithSeed(
  text: string,
  seed: number = 42,
  minDelay: number = 10,
  maxDelay: number = 40,
): AsyncGenerator<LLMEvent> {
  const rng = createSeededRandom(seed);
  for (const ch of text) {
    yield { type: 'text', content: ch };
    await delay(minDelay + rng() * (maxDelay - minDelay));
  }
}
```

**退格修正模拟**

模拟人类输入时发现错误、删除并重新输入的效果：

```typescript
async function* typewriterWithBackspace(
  text: string,
  mistakeProbability: number = 0.05,
): AsyncGenerator<LLMEvent> {
  const backspaceChar = '\b'; // 使用退格序列
  let buffer = '';

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];

    // 模拟输入错误
    if (Math.random() < mistakeProbability) {
      // 输入一个错误字符
      const wrongChar = String.fromCharCode(
        97 + Math.floor(Math.random() * 26)
      );
      yield { type: 'text', content: wrongChar };
      await delay(80 + Math.random() * 60);

      // 退格删除
      yield { type: 'text', content: backspaceChar };
      await delay(100 + Math.random() * 50);

      // 重新输入正确字符
      yield { type: 'text', content: ch };
      await delay(60 + Math.random() * 40);
    } else {
      yield { type: 'text', content: ch };
      await delay(15 + Math.random() * 35);
    }
  }
}
```

**选择指南**

| 变体 | 场景 | 推荐延迟 |
|------|------|---------|
| 加速模式 | 代码生成（开头慢，后续快） | baseDelay 15-25ms |
| 减速模式 | 故事叙述（开头抓人，尾韵悠长） | baseDelay 20-30ms |
| 波浪模式 | 诗歌/歌词（韵律感） | baseDelay 15-20ms |
| 标点暂停 | 正式文档/对话 | charDelay 20-30ms |
| 逐词分块 | 长文本/翻译 | 每词 40-80ms |
| 混合策略 | 通用最佳体验 | 默认推荐 |
| 退格修正 | 模拟人类输入/教程演示 | mistakeProbability 0.03-0.08 |

## 8.3 Spinner 动画

Spinner 是 TUI 中最常见的"加载中"指示器，用于 AI 思考阶段。

### 8.3.1 字符序列大全

```typescript
// 各种风格的 Spinner 序列
const SPINNER_SETS = {
  // ── 旋转类 ──
  dots:     ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'],
  line:     ['-', '\\', '|', '/'],
  arc:      ['◜', '◠', '◝', '◞', '◡', '◟'],
  circle:   ['◐', '◓', '◑', '◒'],
  triangle: ['◢', '◣', '◤', '◥'],

  // ── 弹跳类 ──
  bounce:   ['⠁', '⠂', '⠄', '⡀', '⢀', '⠠', '⠐', '⠈'],

  // ── 时钟类 ──
  clock:    ['🕐', '🕑', '🕒', '🕓', '🕔', '🕕', '🕖', '🕗', '🕘', '🕙', '🕚', '🕛'],

  // ── 条形类 ──
  bar:      ['▁', '▃', '▄', '▅', '▆', '▇', '█', '▇', '▆', '▅', '▄', '▃', '▁'],

  // ── 方块类 ──
  squares:  ['▖', '▘', '▝', '▗'],

  // ── 箭头类 ──
  arrows:   ['←', '↖', '↑', '↗', '→', '↘', '↓', '↙'],
};
```

### 8.3.2 在 blessed 中实现 Spinner

```typescript
class Spinner {
  private timer: ReturnType<typeof setInterval> | null = null;
  private element: Widgets.BoxElement | null = null;
  private frameIndex = 0;

  private static readonly FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

  constructor(
    private parent: Widgets.BoxElement,
    private screen: Widgets.Screen,
    private frames: string[] = Spinner.FRAMES,
    private interval: number = 100,
  ) {}

  start(text: string = 'AI 思考中...'): void {
    if (this.element) return; // 防止重复启动

    this.element = blessed.box({
      parent: this.parent,
      top: 0, left: 0,
      width: '100%-2', height: 1,
      content: '',
      style: { fg: 'yellow', bg: '#1a1a1a' },
      tags: true,
    });

    this.frameIndex = 0;
    this.timer = setInterval(() => {
      if (!this.element) return;
      this.frameIndex = (this.frameIndex + 1) % this.frames.length;
      this.element.setContent(
        `  {yellow-fg}{bold}${this.frames[this.frameIndex]} ${text}{/bold}{/yellow-fg}`
      );
      this.screen.render();
    }, this.interval);

    this.parent.setScrollPerc(100);
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.element) {
      this.parent.remove(this.element);
      this.element = null;
      this.screen.render();
    }
  }

  setText(text: string): void {
    if (this.element) {
      this.element.setContent(
        `  {yellow-fg}{bold}${this.frames[this.frameIndex]} ${text}{/bold}{/yellow-fg}`
      );
      this.screen.render();
    }
  }
}
```

### 8.3.3 多种动画的组合流程

```
阶段 1: [思考中]
  ⠋ AI 思考中...        ← Spinner 动画，持续旋转
  ⠙ AI 思考中...
  ⠹ AI 思考中...
  ... (每 100ms 更新)

阶段 2: [过渡]
  Spinner 停止并移除
  消息容器被创建（空）

阶段 3: [输出中]
  🤖 AI
  你好！有什么可以帮助  ← 打字机效果，逐字出现
  你的吗？▊
  ... (每 20-50ms 追加)

阶段 4: [完成]
  🤖 AI
  你好！有什么可以帮助  ← 静态文本，光标消失
  你的吗？
```

## 8.4 进度条动画

除了 Spinner，进度条是另一种常见的动画形式。

### 8.4.1 使用 blessed 内置进度条

```typescript
const progressBar = blessed.progressbar({
  parent: container,
  top: 0, left: 0,
  width: '100%-4', height: 1,
  style: {
    bar: { bg: 'green' },
    filled: { bg: 'green' },
  },
  pch: '■',      // 填充字符
  value: 0,       // 0-100
});

// 模拟进度
let progress = 0;
const timer = setInterval(() => {
  progress += Math.random() * 10;
  if (progress > 100) progress = 100;
  progressBar.setProgress(progress);
  screen.render();

  if (progress >= 100) clearInterval(timer);
}, 200);
```

### 8.4.2 自定义进度条

```typescript
function createCustomProgressBar(
  screen: Widgets.Screen,
  parent: Widgets.BoxElement,
  label: string,
  color: string,
  width: number = 40
) {
  const bar = blessed.box({
    parent,
    top: 0, left: 2,
    width: width + 4, height: 3,
    border: { type: 'line', fg: color },
    label: ` ${label} `,
    tags: true,
    style: { fg: 'white', bg: 'black' },
  });

  return {
    update(progress: number) {
      const filled = Math.round((progress / 100) * width);
      const empty = width - filled;
      const pct = String(Math.round(progress)).padStart(3);
      bar.setContent(
        ` {${color}-fg}{bold}${'▓'.repeat(filled)}{/bold}` +
        `${'░'.repeat(empty)}{/${color}-fg} ${pct}%`
      );
      screen.render();
    },
  };
}
```

### 8.4.3 工具执行进度条

在工具调用场景中，进度条可以展示工具执行进度：

```typescript
class ToolProgressBar {
  private bar: ReturnType<typeof createCustomProgressBar>;
  private statusText: Widgets.BoxElement;

  constructor(
    parent: Widgets.BoxElement,
    screen: Widgets.Screen,
    toolName: string,
  ) {
    this.bar = createCustomProgressBar(screen, parent, ` ${toolName} `, 'cyan', 30);
    
    this.statusText = blessed.box({
      parent,
      top: 0, left: 2,
      width: '100%-4', height: 1,
      content: '',
      style: { fg: '#888888', bg: '#1a1a1a' },
    });
  }

  update(progress: number, status: string): void {
    this.bar.update(progress);
    this.statusText.setContent(`  ${status}`);
  }

  complete(result: string): void {
    this.bar.update(100);
    this.statusText.setContent(`  {green-fg}✅ ${result}{/green-fg}`);
  }

  error(err: string): void {
    this.bar.update(0);
    this.statusText.setContent(`  {red-fg}❌ ${err}{/red-fg}`);
  }
}
```

## 8.5 LLM 流式协议详解

大模型 API 使用多种流式传输协议。理解这些协议有助于设计更好的 TUI 交互。

### 8.5.1 Server-Sent Events (SSE)

SSE 是大多数 LLM API（OpenAI、Anthropic）使用的标准流式协议：

```
GET /v1/messages HTTP/1.1
Accept: text/event-stream

← data: {"type": "content_block_delta", "delta": {"text": "你好"}}
← data: {"type": "content_block_delta", "delta": {"text": "！"}}
← data: {"type": "content_block_stop"}
← data: [DONE]
```

```typescript
/**
 * 解析 SSE 流
 */
async function* parseSSE<T>(
  response: Response,
): AsyncGenerator<T> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || ''; // 保留未完成的行

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const data = line.slice(6);
        if (data === '[DONE]') return;
        try {
          yield JSON.parse(data) as T;
        } catch {
          // 忽略解析错误
        }
      }
    }
  }
}
```

### 8.5.2 Anthropic Stream Events

Anthropic API 的事件类型更丰富，适合 TUI 展示：

```typescript
type AnthropicStreamEvent =
  | { type: 'message_start'; message: { id: string; model: string } }
  | { type: 'content_block_start'; index: number; content_block: { type: string } }
  | { type: 'content_block_delta'; index: number; delta: { type: string; text?: string } }
  | { type: 'content_block_stop'; index: number }
  | { type: 'message_delta'; delta: { stop_reason: string }; usage: { output_tokens: number } }
  | { type: 'message_stop' }
  | { type: 'ping' };

/**
 * 将 Anthropic 流事件转换为统一 LLMEvent
 */
async function* anthropicToLLMEvent(
  stream: AsyncGenerator<AnthropicStreamEvent>,
): AsyncGenerator<LLMEvent> {
  for await (const event of stream) {
    switch (event.type) {
      case 'message_start':
        yield { type: 'thinking' };
        break;

      case 'content_block_delta':
        if (event.delta.type === 'text_delta' && event.delta.text) {
          yield { type: 'text', content: event.delta.text };
        }
        break;

      case 'message_delta':
        yield { type: 'meta', tokens: event.usage?.output_tokens };
        break;

      case 'message_stop':
        yield { type: 'done' };
        break;
    }
  }
}
```

### 8.5.3 OpenAI Chat Completion Chunks

```typescript
type OpenAIChunk = {
  id: string;
  choices: Array<{
    delta: {
      content?: string;
      tool_calls?: Array<{
        index: number;
        id?: string;
        function?: { name?: string; arguments?: string };
      }>;
    };
    finish_reason: string | null;
  }>;
};

async function* openaiToLLMEvent(
  stream: AsyncGenerator<OpenAIChunk>,
): AsyncGenerator<LLMEvent> {
  for await (const chunk of stream) {
    const choice = chunk.choices[0];
    if (!choice) continue;

    const delta = choice.delta;

    if (delta.content) {
      yield { type: 'text', content: delta.content };
    }

    if (delta.tool_calls) {
      for (const tool of delta.tool_calls) {
        if (tool.function?.name) {
          yield {
            type: 'tool_call',
            name: tool.function.name,
            args: tool.function.arguments
              ? JSON.parse(tool.function.arguments)
              : {},
          };
        }
      }
    }

    if (choice.finish_reason === 'stop') {
      yield { type: 'done' };
    }
  }
}
```

### 8.5.4 Mock 流适配器

在开发阶段，使用 Mock 流模拟上述协议：

```typescript
/**
 * Mock LLM 流 —— 模拟真实 API 的流式事件
 */
async function* mockLLMStream(prompt: string): AsyncGenerator<LLMEvent> {
  // 1. 思考阶段
  yield { type: 'thinking' };
  await delay(300 + Math.random() * 300);

  // 2. 检测并匹配场景
  if (prompt.includes('天气') || prompt.includes('weather')) {
    yield* mockWeatherScenario(prompt);
  } else if (prompt.includes('计算') || /\d+\s*[+\-*/]\s*\d+/.test(prompt)) {
    yield* mockCalculateScenario(prompt);
  } else if (prompt.includes('错误') || prompt.includes('error')) {
    yield { type: 'error', message: '模拟错误：服务暂时不可用' };
  } else {
    yield* mockChatScenario(prompt);
  }

  // 3. 完成
  yield { type: 'done' };
}

/**
 * 逐字生成文本的辅助函数
 */
async function* typeText(text: string, minDelay = 15, maxDelay = 30): AsyncGenerator<LLMEvent> {
  for (const ch of text) {
    yield { type: 'text', content: ch };
    await delay(minDelay + Math.random() * (maxDelay - minDelay));
  }
}
```

## 8.6 流式文本的性能优化

### 8.6.1 综合优化方案

| 技术 | 说明 | 效果 |
|------|------|------|
| **节流渲染（Throttle）** | 限制渲染帧率 (16-50ms) | 减少 CPU 使用，省电 |
| **差分更新（smartCSR）** | 只输出变化区域 | 减少终端 IO |
| **批量写入（Batch）** | 收集变化一次性刷新 | 减少渲染次数 |
| **降低 Spinner 频率** | 100-150ms 即可 | 减少无谓渲染 |
| **使用 setInterval** | 优于 requestAnimationFrame | 更可控 |
| **元素复用** | 更新内容而非创建/销毁 | 减少 GC 压力 |

### 8.6.2 性能量化参考

```typescript
// 不优化：50ms 间隔渲染 1000 次
// CPU: 约 15-20% 单核
// 终端输出: 约 5MB 数据

// 优化后：33ms 节流 + 批处理
// CPU: 约 3-5% 单核
// 终端输出: 约 1MB 数据
```

### 8.6.3 综合渲染管理器

```typescript
class StreamRenderManager {
  private lastRenderTime = 0;
  private readonly MIN_INTERVAL = 33;
  private batchBuffer: string[] = [];
  private batchTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly BATCH_INTERVAL = 50;

  constructor(
    private renderFn: (content: string) => void,
    private screen: Widgets.Screen,
  ) {}

  /** 添加流式文本片段 */
  addChunk(chunk: string): void {
    this.batchBuffer.push(chunk);

    if (!this.batchTimer) {
      this.batchTimer = setTimeout(() => this.flush(), this.BATCH_INTERVAL);
    }
  }

  /** 批量刷新到 UI */
  private flush(): void {
    if (this.batchBuffer.length === 0) {
      this.batchTimer = null;
      return;
    }

    const content = this.batchBuffer.join('');
    this.batchBuffer = [];

    // 节流渲染
    const now = Date.now();
    if (now - this.lastRenderTime >= this.MIN_INTERVAL) {
      this.renderFn(content);
      this.lastRenderTime = now;
    }

    this.batchTimer = null;
  }

  /** 强制刷新（流结束时调用） */
  flushAll(): void {
    if (this.batchTimer) {
      clearTimeout(this.batchTimer);
      this.batchTimer = null;
    }

    if (this.batchBuffer.length > 0) {
      const content = this.batchBuffer.join('');
      this.batchBuffer = [];
      this.renderFn(content);
    }

    this.screen.render();
  }
}
```

## 8.7 完整的动画生命周期

在 `examples/llm-chat.ts` 中，动画的完整生命周期如下：

```typescript
// 1. 用户发送消息后立即启动 Spinner
this.state = 'thinking';
this.startSpinner('AI 思考中...');   // ← 动画开始

// 2. 收到第一个文本 token
this.stopSpinner();                   // ← Spinner 动画停止
this.state = 'streaming';
this.updateAssistantMessage('');      // ← 创建消息容器

// 3. 流式更新 — 打字机效果
for await (const event of stream) {
  if (event.type === 'text') {
    this.renderManager.addChunk(event.content);  // 批处理添加
  }
  if (event.type === 'tool_call') {
    this.renderManager.flushAll();  // 刷新剩余文本
    this.showToolCall(event.name, event.args);
  }
}

// 4. 生成完成
this.renderManager.flushAll();  // 确保所有文本已渲染
this.state = 'idle';           // ← 回到静态状态
```

## 8.8 聚焦感知动画

当终端失去焦点时暂停动画，减少 CPU 消耗：

```typescript
// 在 Screen 级别监听焦点事件
process.stdout.write('\x1b[?1004h');  // 启用焦点事件

let isFocused = true;

process.stdin.on('data', (data: Buffer) => {
  const str = data.toString();

  // 焦点事件序列
  if (str === '\x1b[I') {
    isFocused = true;
    // 恢复动画
  }
  if (str === '\x1b[O') {
    isFocused = false;
    // 暂停动画
  }
});

// 修改动画循环，只在聚焦时渲染
private spinnerTick(): void {
  if (!isFocused) return;  // 失去焦点时不更新
  // ... 更新动画帧
}
```

### 8.8.1 跨平台焦点检测

xterm 焦点事件（`\x1b[I` / `\x1b[O`）在 Windows 终端上不一定支持。以下提供回退方案：

```typescript
type FocusDetector = {
  onFocus: (cb: () => void) => void;
  onBlur: (cb: () => void) => void;
  start: () => void;
  stop: () => void;
};

function createFocusDetector(): FocusDetector {
  const focusCbs: Array<() => void> = [];
  const blurCbs: Array<() => void> = [];

  // 尝试启用 xterm 焦点事件
  let useXtermEvents = true;
  try {
    process.stdout.write('\x1b[?1004h');
  } catch {
    useXtermEvents = false;
  }

  // Windows 回退：检测终端窗口可见性
  let visibilityTimer: ReturnType<typeof setInterval> | null = null;
  let wasFocused = true;

  const onData = (data: Buffer) => {
    const str = data.toString();
    if (str === '\x1b[I') focusCbs.forEach(cb => cb());
    if (str === '\x1b[O') blurCbs.forEach(cb => cb());
  };

  return {
    onFocus: (cb) => focusCbs.push(cb),
    onBlur: (cb) => blurCbs.push(cb),

    start: () => {
      if (useXtermEvents) {
        process.stdin.on('data', onData);
      }

      // Windows 回退：通过定时检测窗口状态
      // 使用 child_process 检查窗口句柄活动状态
      if (process.platform === 'win32') {
        visibilityTimer = setInterval(() => {
          // 简化检测：如果超过 500ms 没有终端输出活动，认为失去焦点
          // 实际应用可调用 Windows API 检测窗口状态
        }, 1000);
      }
    },

    stop: () => {
      process.stdin.removeListener('data', onData);
      if (visibilityTimer) {
        clearInterval(visibilityTimer);
        visibilityTimer = null;
      }
      try {
        process.stdout.write('\x1b[?1004l'); // 关闭焦点事件
      } catch {
        // 忽略
      }
    },
  };
}
```

### 8.8.2 blessed 焦点集成

利用 blessed 的 Screen 事件实现更自然的集成：

```typescript
class FocusAwareAnimationManager {
  private isFocused = true;
  private savedFrameIndex = 0;
  private savedElapsed = 0;
  private animationStartTime = 0;
  private pausedIndicator: Widgets.BoxElement | null = null;
  private pauseTimer: ReturnType<typeof setInterval> | null = null;

  constructor(private screen: Widgets.Screen) {
    // blessed screen 的 focus/blur 事件
    // 注意：blessed 的 focus 事件通常用于元素焦点，
    // 但可以通过全局 keypress 和终端事件组合实现
    this.screen.on('keypress', () => {
      if (!this.isFocused) {
        // 任何按键输入都表明终端已重新获得焦点
        this.handleFocus();
      }
    });
  }

  /** 整合 xterm 焦点事件到 blessed */
  integrateWithTerminal(): void {
    process.stdout.write('\x1b[?1004h');
    process.stdin.on('data', (data: Buffer) => {
      const str = data.toString();
      if (str === '\x1b[I') this.handleFocus();
      if (str === '\x1b[O') this.handleBlur();
    });
  }

  /** 失去焦点：保存状态，暂停动画 */
  private handleBlur(): void {
    if (!this.isFocused) return;
    this.isFocused = false;

    // 记录当前帧信息供恢复使用
    this.savedElapsed = Date.now() - this.animationStartTime;

    this.showPausedIndicator();
    this.emitPauseEvent();
  }

  /** 恢复焦点：还原状态，继续动画 */
  private handleFocus(): void {
    if (this.isFocused) return;
    this.isFocused = true;

    this.hidePausedIndicator();
    this.emitResumeEvent();
  }

  /** 在屏幕角落显示暂停指示器 */
  private showPausedIndicator(): void {
    if (this.pausedIndicator) return;

    this.pausedIndicator = blessed.box({
      parent: this.screen,
      bottom: 0,
      right: 0,
      width: 14,
      height: 1,
      content: ' {yellow-fg}⏸ PAUSED{/yellow-fg} ',
      tags: true,
      style: { bg: '#333333' },
    });
    this.screen.render();
  }

  private hidePausedIndicator(): void {
    if (this.pausedIndicator) {
      this.screen.remove(this.pausedIndicator);
      this.pausedIndicator = null;
      this.screen.render();
    }
  }

  /** 持久化存储帧状态 */
  saveFrameState(frameIndex: number): void {
    this.savedFrameIndex = frameIndex;
    this.animationStartTime = Date.now();
  }

  getSavedFrameState(): { frameIndex: number; elapsed: number } {
    return {
      frameIndex: this.savedFrameIndex,
      elapsed: this.savedElapsed,
    };
  }

  get isActive(): boolean {
    return this.isFocused;
  }

  // 供外部订阅的事件
  private pauseCbs: Array<() => void> = [];
  private resumeCbs: Array<() => void> = [];

  onPause(cb: () => void): void { this.pauseCbs.push(cb); }
  onResume(cb: () => void): void { this.resumeCbs.push(cb); }

  private emitPauseEvent(): void {
    this.pauseCbs.forEach(cb => cb());
  }

  private emitResumeEvent(): void {
    // 恢复到暂停前的帧
    this.resumeCbs.forEach(cb => cb());
  }
}
```

### 8.8.3 节流 vs 暂停策略

| 策略 | 行为 | 适用场景 |
|------|------|---------|
| **暂停** | 完全停止定时器，恢复时从保存状态继续 | CPU 敏感环境、笔记本电池模式 |
| **节流** | 将 FPS 降至 1-2fps，继续更新但极慢 | 需要保持"活动"视觉、监控场景 |
| **降质** | 停止动画帧更新，但继续文本追加 | 流式输出中失去焦点时 |

```typescript
type BlurStrategy = 'pause' | 'throttle' | 'degrade';

class AdaptiveAnimationController {
  private strategy: BlurStrategy = 'pause';
  private focused = true;
  private originalInterval = 100;
  private currentTimer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private animate: () => void,
    private focusManager: FocusAwareAnimationManager,
  ) {
    this.focusManager.onPause(() => this.onBlur());
    this.focusManager.onResume(() => this.onFocus());
  }

  setStrategy(s: BlurStrategy): void {
    this.strategy = s;
  }

  private onBlur(): void {
    this.focused = false;
    if (this.strategy === 'pause') {
      this.stopTimer();
    } else if (this.strategy === 'throttle') {
      this.restartTimer(1000); // 降低到 1fps
    }
    // 'degrade' 策略保持原样：只停止动画帧，文本继续
  }

  private onFocus(): void {
    this.focused = true;
    this.restartTimer(this.originalInterval);
  }

  private stopTimer(): void {
    if (this.currentTimer) {
      clearInterval(this.currentTimer);
      this.currentTimer = null;
    }
  }

  private restartTimer(interval: number): void {
    this.stopTimer();
    this.currentTimer = setInterval(() => {
      this.animate();
    }, interval);
  }
}
```

## 8.9 实验：自定义动画效果

尝试在 `llm-chat.ts` 中替换 Spinner 动画风格：

```typescript
// 使用弹跳风格
const bounceFrames = ['⠁', '⠂', '⠄', '⡀', '⢀', '⠠', '⠐', '⠈'];

// 或使用时钟风格（注意：emoji 需要真彩色支持）
const clockFrames = ['🕐', '🕑', '🕒', '🕓', '🕔', '🕕', '🕖', '🕗', '🕘', '🕙', '🕚', '🕛'];
```

**选择 Spinner 的建议：**
- **dots**: 最通用，所有终端都支持
- **line**: 最轻量，适合性能敏感场景
- **clock**: 视觉新颖，但需要 emoji 支持
- **bar**: 适合进度相关的加载指示

## 8.10 动画性能测量

量化每种动画类型的性能开销，帮助识别瓶颈。

### 8.10.1 FPS 计数器叠加层

```typescript
class FPSCounter {
  private frameCount = 0;
  private lastMeasureTime = Date.now();
  private currentFPS = 0;
  private element: Widgets.BoxElement;

  constructor(screen: Widgets.Screen) {
    this.element = blessed.box({
      parent: screen,
      top: 0,
      right: 0,
      width: 14,
      height: 1,
      content: ' FPS: -- ',
      style: { fg: 'green', bg: '#222222' },
      tags: true,
    });
  }

  /** 每帧调用一次 */
  tick(): void {
    this.frameCount++;
    const now = Date.now();
    const elapsed = now - this.lastMeasureTime;

    if (elapsed >= 1000) {
      this.currentFPS = Math.round((this.frameCount * 1000) / elapsed);
      this.frameCount = 0;
      this.lastMeasureTime = now;

      const color = this.currentFPS >= 25 ? 'green' :
                    this.currentFPS >= 15 ? 'yellow' : 'red';
      this.element.setContent(
        ` {${color}-fg}FPS: ${String(this.currentFPS).padStart(2)}{/${color}-fg} `
      );
    }
  }

  get fps(): number {
    return this.currentFPS;
  }

  show(): void { this.element.show(); }
  hide(): void { this.element.hide(); }
  destroy(): void { this.element.detach(); }
}
```

### 8.10.2 帧时序直方图

```typescript
class FrameTimingHistogram {
  private timestamps: number[] = [];
  private readonly MAX_SAMPLES = 120; // 最近 120 帧
  private element: Widgets.BoxElement;

  constructor(parent: Widgets.BoxElement) {
    this.element = blessed.box({
      parent,
      top: 0, left: 0,
      width: 30, height: 6,
      label: ' Frame Timing (ms) ',
      border: { type: 'line', fg: 'cyan' },
      tags: true,
      style: { fg: 'white', bg: 'black' },
    });
  }

  recordFrame(): void {
    const now = Date.now();
    if (this.timestamps.length > 0) {
      const delta = now - this.timestamps[this.timestamps.length - 1];
      if (delta > 0) {
        this.timestamps.push(delta);
      }
    } else {
      this.timestamps.push(0);
    }

    if (this.timestamps.length > this.MAX_SAMPLES) {
      this.timestamps = this.timestamps.slice(-this.MAX_SAMPLES);
    }

    this.renderHistogram();
  }

  private renderHistogram(): void {
    const deltas = this.timestamps.slice(1); // 跳过第一个 0
    if (deltas.length === 0) return;

    const min = Math.min(...deltas);
    const max = Math.max(...deltas);
    const avg = deltas.reduce((a, b) => a + b, 0) / deltas.length;

    // 将时序分布可视化为条形直方图
    const buckets = [0, 8, 16, 33, 50, 100, 200];
    const counts = new Array(buckets.length).fill(0);

    for (const d of deltas) {
      for (let i = buckets.length - 1; i >= 0; i--) {
        if (d >= buckets[i]) {
          counts[i]++;
          break;
        }
      }
    }

    const total = deltas.length;
    const lines = buckets.map((b, i) => {
      const pct = total > 0 ? (counts[i] / total) * 100 : 0;
      const barLen = Math.round(pct / 5);
      const bar = '█'.repeat(barLen);
      const label = `${String(b).padStart(3)}+`;
      return ` ${label} ${bar} ${counts[i]}`;
    });

    const content = [
      ` Min:${String(min).padStart(3)}ms  Avg:${String(Math.round(avg)).padStart(3)}ms  Max:${String(max).padStart(3)}ms`,
      ' --- Distribution ---',
      ...lines,
    ].join('\n');

    this.element.setContent(content);
  }
}
```

### 8.10.3 各动画类型渲染开销

```typescript
/**
 * 性能基准测试：对比不同动画类型的渲染开销
 */
async function benchmarkAnimations(screen: Widgets.Screen): Promise<void> {
  const testBox = blessed.box({
    parent: screen,
    top: 0, left: 0,
    width: 50, height: 5,
    style: { bg: 'black' },
  });

  const results: Array<{ name: string; avgRenderMs: number; fps: number }> = [];

  // 1. Spinner 动画开销
  const spinner = new Spinner(testBox, screen);
  const spinnerTimes: number[] = [];

  spinner.start('测试');
  for (let i = 0; i < 50; i++) {
    const start = performance.now();
    screen.render();
    spinnerTimes.push(performance.now() - start);
    await delay(100);
  }
  spinner.stop();

  results.push({
    name: 'Spinner',
    avgRenderMs: average(spinnerTimes),
    fps: 1000 / average(spinnerTimes),
  });

  // 2. 打字机效果开销
  const textBox = blessed.box({
    parent: testBox,
    top: 0, left: 0,
    width: '100%', height: 3,
    style: { fg: 'white', bg: 'black' },
  });

  const typewriterTimes: number[] = [];
  for (let i = 0; i < 100; i++) {
    textBox.setContent('A'.repeat(i + 1));
    const start = performance.now();
    screen.render();
    typewriterTimes.push(performance.now() - start);
  }

  results.push({
    name: 'Typewriter',
    avgRenderMs: average(typewriterTimes),
    fps: 1000 / average(typewriterTimes),
  });

  // 3. 进度条动画开销
  const progressTimes: number[] = [];
  const pBar = blessed.progressbar({
    parent: testBox,
    top: 0, left: 0,
    width: '100%', height: 1,
  });

  for (let v = 0; v <= 100; v++) {
    pBar.setProgress(v);
    const start = performance.now();
    screen.render();
    progressTimes.push(performance.now() - start);
  }

  results.push({
    name: 'ProgressBar',
    avgRenderMs: average(progressTimes),
    fps: 1000 / average(progressTimes),
  });

  // 输出结果
  console.table(results);
  testBox.detach();
  screen.render();
}

function average(arr: number[]): number {
  return arr.reduce((a, b) => a + b, 0) / arr.length;
}
```

### 8.10.4 内存使用追踪

```typescript
class MemoryTracker {
  private totalContentLength = 0;
  private elementCount = 0;
  private accumulationHistory: number[] = [];
  private element: Widgets.BoxElement;

  constructor(parent: Widgets.BoxElement) {
    this.element = blessed.box({
      parent,
      top: 0, left: 0,
      width: 40, height: 4,
      label: ' Memory ',
      border: { type: 'line', fg: 'magenta' },
      tags: true,
      style: { fg: 'white', bg: 'black' },
    });
  }

  /** 追踪累积文本量 */
  trackContent(length: number): void {
    this.totalContentLength += length;
    this.accumulationHistory.push(this.totalContentLength);

    // 仅保留最近 100 个采样点
    if (this.accumulationHistory.length > 100) {
      this.accumulationHistory.shift();
    }
  }

  trackElementAdded(): void { this.elementCount++; }
  trackElementRemoved(): void { this.elementCount = Math.max(0, this.elementCount - 1); }

  /** 估算内存占用（粗略） */
  private estimateMemoryBytes(): number {
    // 每个字符约 2 字节（UTF-16）
    const textMemory = this.totalContentLength * 2;
    // 每个 blessed 元素约 2KB 基础开销
    const elementMemory = this.elementCount * 2048;
    return textMemory + elementMemory;
  }

  updateDisplay(): void {
    const memBytes = this.estimateMemoryBytes();
    const memKB = (memBytes / 1024).toFixed(1);
    const contentKB = ((this.totalContentLength * 2) / 1024).toFixed(1);

    this.element.setContent(
      ` Content: ${this.totalContentLength} chars (${contentKB}KB)\n` +
      ` Elements: ${this.elementCount}\n` +
      ` Estimated: ${memKB}KB total`
    );
  }
}
```

### 8.10.5 性能分析技巧

- **使用 `performance.now()` 而非 `Date.now()`** 获得亚毫秒级精度，适合测量单帧渲染时间
- **监控 `screen.render()` 耗时** — 在 render 前后打点，耗时超过 50ms 表示需要优化
- **关注元素数量** — blessed 在元素超过 500 时渲染性能显著下降，考虑虚拟滚动
- **避免频繁的 `setContent` 调用** — 合并多次更新为一次批量 setContent
- **使用 `screen.debug` 日志** — blessed 支持 `screen.debug = true` 输出调试信息到日志文件
- **测量终端 IO** — 大量 ANSI 序列输出会导致终端"喘不过气"；使用 `process.stdout.write` 计数监控

## 8.11 过渡动画

在不同 UI 状态间添加平滑过渡，提升视觉连贯性。

### 8.11.1 字符密度淡入淡出

利用空格到完整字符的密度变化实现淡入效果：

```typescript
/**
 * 淡入效果：从纯空格逐渐过渡到目标文本
 * @param element 目标 blessed 元素
 * @param targetText 最终显示的文本
 * @param steps 过渡步数（越高越平滑）
 * @param interval 每步间隔 (ms)
 */
async function fadeIn(
  element: Widgets.BoxElement,
  screen: Widgets.Screen,
  targetText: string,
  steps: number = 10,
  interval: number = 30,
): Promise<void> {
  for (let i = 1; i <= steps; i++) {
    const progress = i / steps;

    // 将每个字符从空格逐步过渡到完整字符
    const fadedText = targetText.split('').map(ch => {
      if (ch === ' ' || ch === '\n') return ch;
      // 使用密度字符模拟渐变：' ' -> '░' -> '▒' -> '▓' -> ch
      const densityChars = [' ', '░', '▒', '▓'];
      const densityIndex = Math.min(
        Math.floor(progress * densityChars.length),
        densityChars.length - 1,
      );

      if (progress < 0.3) {
        // 低进度：仅显示密度字符
        return ch.match(/\w/) ? densityChars[densityIndex] : ch;
      } else if (progress < 0.7) {
        // 中进度：密度字符混合目标字符
        return Math.random() < progress
          ? ch
          : densityChars[densityIndex];
      } else {
        // 高进度：逐渐显示完整字符
        return ch;
      }
    }).join('');

    element.setContent(fadedText);
    screen.render();
    await delay(interval);
  }

  // 确保最终内容精确
  element.setContent(targetText);
  screen.render();
}

/**
 * 淡出效果：从完整文本过渡到空白
 */
async function fadeOut(
  element: Widgets.BoxElement,
  screen: Widgets.Screen,
  steps: number = 8,
  interval: number = 25,
): Promise<void> {
  const currentContent = element.getContent();
  await fadeIn(element, screen, '', steps, interval);
}
```

### 8.11.2 从右侧滑入工具卡片

```typescript
/**
 * 工具调用卡片从右侧滑入
 */
async function slideInFromRight(
  element: Widgets.BoxElement,
  screen: Widgets.Screen,
  slideWidth: number = 30,
  duration: number = 200,
): Promise<void> {
  const steps = 10;
  const interval = duration / steps;

  // 保存原始 left 值，假设为绝对数值或百分比
  const originalLeft = element.left;
  element.left = slideWidth; // 从右侧外开始

  for (let i = 1; i <= steps; i++) {
    const progress = i / steps;
    // 缓出效果：快速开始，慢速结束
    const eased = 1 - Math.pow(1 - progress, 2);
    const currentLeft = Math.round(slideWidth * (1 - eased));

    element.left = currentLeft;
    screen.render();
    await delay(interval);
  }

  // 恢复原始位置
  element.left = originalLeft;
  screen.render();
}

/**
 * 滑出到右侧
 */
async function slideOutToRight(
  element: Widgets.BoxElement,
  screen: Widgets.Screen,
  slideWidth: number = 30,
  duration: number = 150,
): Promise<void> {
  const steps = 8;
  const interval = duration / steps;

  for (let i = 1; i <= steps; i++) {
    const progress = i / steps;
    const eased = progress * progress; // 缓入效果：慢速开始，快速结束
    element.left = Math.round(slideWidth * eased);
    screen.render();
    await delay(interval);
  }

  element.hide();
  screen.render();
}
```

### 8.11.3 展开/折叠详细视图

```typescript
/**
 * 展开视图：从 0 高度逐步增加到目标高度
 */
async function expandView(
  element: Widgets.BoxElement,
  screen: Widgets.Screen,
  targetHeight: number,
  duration: number = 300,
): Promise<void> {
  const steps = Math.min(targetHeight, 15);
  const interval = duration / steps;

  element.show();

  for (let i = 1; i <= steps; i++) {
    const progress = i / steps;
    // 使用 overshoot 缓动产生"弹性展开"效果
    const eased = progress < 0.8
      ? progress / 0.8 * 0.9
      : 0.9 + 0.1 * (progress - 0.8) / 0.2;

    element.height = Math.round(targetHeight * eased);
    screen.render();
    await delay(interval);
  }

  element.height = targetHeight;
  screen.render();
}

/**
 * 折叠视图：高度逐渐归零
 */
async function collapseView(
  element: Widgets.BoxElement,
  screen: Widgets.Screen,
  duration: number = 200,
): Promise<void> {
  const currentHeight = typeof element.height === 'number'
    ? element.height
    : parseInt(element.height as string) || 5;

  const steps = Math.min(currentHeight, 10);
  const interval = duration / steps;

  for (let i = 1; i <= steps; i++) {
    const progress = i / steps;
    element.height = Math.round(currentHeight * (1 - progress));
    screen.render();
    await delay(interval);
  }

  element.hide();
  screen.render();
}
```

### 8.11.4 内容状态间交叉淡变

```typescript
/**
 * 交叉淡变：新旧内容同时以不同透明度混合
 * 利用 blessed 叠加元素实现
 */
async function crossFade(
  container: Widgets.BoxElement,
  screen: Widgets.Screen,
  newContent: string,
  duration: number = 200,
): Promise<void> {
  const overlay = blessed.box({
    parent: container.parent,
    top: container.top,
    left: container.left,
    width: container.width,
    height: container.height,
    content: newContent,
    style: { fg: 'white', bg: 'black' },
    tags: true,
    wrap: true,
  });

  const steps = 10;
  const interval = duration / steps;

  // 旧内容逐渐透明（依赖 blessed 的隐藏），新内容逐渐显示
  for (let i = 1; i <= steps; i++) {
    const progress = i / steps;

    if (progress > 0.3) {
      container.hide();
    }

    screen.render();
    await delay(interval);
  }

  // 最终切换：新内容替换旧内容，移除叠加层
  container.setContent(newContent);
  container.show();
  overlay.detach();
  screen.render();
}
```

### 8.11.5 过渡时长与缓动选择

| 过渡类型 | 推荐时长 | 缓动函数 | 适用场景 |
|---------|---------|---------|---------|
| 淡入 | 150-300ms | ease-out | 新消息出现 |
| 淡出 | 100-200ms | ease-in | 消息消失 |
| 滑入 | 200-350ms | ease-out | 工具卡片 |
| 滑出 | 100-200ms | ease-in | 卡片关闭 |
| 展开 | 200-400ms | overshoot | 详情面板 |
| 折叠 | 150-250ms | ease-in | 面板收起 |
| 交叉淡变 | 150-250ms | linear | 内容更新 |

### 8.11.6 弹跳过渡（超调缓动）

```typescript
/**
 * 带超调的弹性过渡：目标值附近来回震荡几次后稳定
 */
function bounceEaseOut(t: number): number {
  if (t < 1 / 2.75) {
    return 7.5625 * t * t;
  } else if (t < 2 / 2.75) {
    t -= 1.5 / 2.75;
    return 7.5625 * t * t + 0.75;
  } else if (t < 2.5 / 2.75) {
    t -= 2.25 / 2.75;
    return 7.5625 * t * t + 0.9375;
  } else {
    t -= 2.625 / 2.75;
    return 7.5625 * t * t + 0.984375;
  }
}

/**
 * 超调缓动：先超过目标值，再回弹
 */
function overshootEaseOut(t: number, magnitude: number = 1.2): number {
  return Math.min(1, t * t * ((magnitude + 1) * t - magnitude));
}

/**
 * 弹跳展开效果
 */
async function expandWithBounce(
  element: Widgets.BoxElement,
  screen: Widgets.Screen,
  targetHeight: number,
): Promise<void> {
  const steps = 20;
  const interval = 20;

  element.show();

  for (let i = 1; i <= steps; i++) {
    const progress = i / steps;
    const eased = bounceEaseOut(progress);
    const currentHeight = Math.max(1, Math.round(targetHeight * eased));
    element.height = currentHeight;
    screen.render();
    await delay(interval);
  }

  element.height = targetHeight;
  screen.render();
}
```

## 8.12 脉冲/呼吸效果

为等待状态和专注于动画的 UI 元素添加有机的生命感。

### 8.12.1 呼吸动画（空闲等待）

```typescript
/**
 * 呼吸效果：字符在可见与不可见之间平滑变化
 * 适用于空闲等待状态，提示用户系统仍在运行
 */
class BreathingAnimation {
  private timer: ReturnType<typeof setInterval> | null = null;
  private element: Widgets.BoxElement;
  private phase = 0;

  constructor(
    parent: Widgets.BoxElement,
    private screen: Widgets.Screen,
    private amplitude: number = 1.0,    // 振幅：0.0-1.0
    private frequency: number = 0.5,    // 频率：每秒周期数
    private text: string = '● 等待中...',
  ) {
    this.element = blessed.box({
      parent,
      top: 0, left: 0,
      width: '100%-2', height: 1,
      content: '',
      style: { fg: 'cyan', bg: '#1a1a1a' },
      tags: true,
    });
  }

  start(): void {
    this.timer = setInterval(() => {
      this.phase += 0.05 * this.frequency * 2 * Math.PI;

      // 呼吸波形：使用 sin 模拟吸气和呼气
      const breath = (Math.sin(this.phase) + 1) / 2; // 0-1 范围
      const scaledBreath = 0.3 + breath * this.amplitude * 0.7;

      // 通过 Unicode 上标/下标字符模拟大小变化
      const sizeIndicator = scaledBreath > 0.7 ? '◉' :
                            scaledBreath > 0.4 ? '◎' : '○';

      // 亮度通过透明度字符模拟
      const density = Math.round(scaledBreath * 8);
      const shimmer = '░▒▓█'[Math.min(density % 4, 3)];

      this.element.setContent(
        `  {cyan-fg}${sizeIndicator} ${shimmer} ${this.text}{/cyan-fg}`
      );
      this.screen.render();
    }, 50);
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    this.element.setContent('');
    this.screen.render();
  }
}
```

### 8.12.2 Unicode 块字符渐变

```typescript
/**
 * 使用完整块字符梯度创建平滑颜色过渡效果
 * 通过字符密度组合模拟亮度渐变
 */
const GRADIENT_CHARS = [' ', '▏', '▎', '▍', '▌', '▋', '▊', '▉', '█'];

/**
 * 创建水平渐变条
 */
function createGradientBar(
  parent: Widgets.BoxElement,
  screen: Widgets.Screen,
  width: number = 30,
  colors: Array<{ r: number; g: number; b: number }> = [
    { r: 0, g: 100, b: 255 },
    { r: 100, g: 200, b: 255 },
    { r: 255, g: 100, b: 100 },
  ],
): { update: (progress: number) => void } {
  const element = blessed.box({
    parent,
    top: 0, left: 0,
    width,
    height: 1,
    style: { bg: 'black' },
  });

  return {
    update(progress: number) {
      // 将 progress (0-1) 映射到颜色渐变
      const clampedProgress = Math.max(0, Math.min(1, progress));

      // 插值颜色
      const segmentCount = colors.length - 1;
      const segment = Math.min(
        Math.floor(clampedProgress * segmentCount),
        segmentCount - 1,
      );
      const localProgress = (clampedProgress * segmentCount) - segment;

      const c1 = colors[segment];
      const c2 = colors[segment + 1];

      const r = Math.round(c1.r + (c2.r - c1.r) * localProgress);
      const g = Math.round(c1.g + (c2.g - c1.g) * localProgress);
      const b = Math.round(c1.b + (c2.b - c1.b) * localProgress);

      // 使用块字符构建渐变条
      const barChars = Array.from({ length: width }, (_, i) => {
        const charProgress = i / width;
        const charIndex = Math.floor(charProgress * GRADIENT_CHARS.length);
        return GRADIENT_CHARS[Math.min(charIndex, GRADIENT_CHARS.length - 1)];
      });

      element.setContent(
        `{${r}-${g}-${b}-fg}${barChars.join('')}{/${r}-${g}-${b}-fg}`
      );
      screen.render();
    },
  };
}
```

### 8.12.3 心跳模式

```typescript
/**
 * 心跳脉冲模式：快速双脉冲后暂停
 * 适合表示"活跃"或"监听中"状态
 */
class HeartbeatAnimation {
  private timer: ReturnType<typeof setInterval> | null = null;
  private element: Widgets.BoxElement;
  private phase = 0;

  constructor(
    parent: Widgets.BoxElement,
    private screen: Widgets.Screen,
    private bpm: number = 72, // 每分钟心跳数
  ) {
    this.element = blessed.box({
      parent,
      top: 0, left: 0,
      width: 6, height: 1,
      content: '  ♥  ',
      style: { fg: 'red', bg: '#1a1a1a' },
    });
  }

  start(): void {
    const interval = Math.round((60000 / this.bpm) / 8); // 每拍 8 帧

    this.timer = setInterval(() => {
      this.phase = (this.phase + 1) % 8;

      // 心跳模式：lub-dub 双脉冲
      const isLub = this.phase === 0 || this.phase === 1;
      const isDub = this.phase === 2 || this.phase === 3;

      if (isLub || isDub) {
        // 脉冲峰值
        const brightness = isLub ? 'bright' : 'dim';
        this.element.style.fg = brightness === 'bright' ? '#ff4444' : '#aa2222';
        this.element.setContent('  ♥  ');
      } else {
        // 谷值
        this.element.style.fg = '#551111';
        this.element.setContent('  ♡  ');
      }

      this.screen.render();
    }, interval);
    // 实际效果：lub(1-2) dub(3-4) 休息(5-8)
    // 72bpm 时每拍约 833ms，分 8 帧每帧约 104ms
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    this.element.setContent('');
    this.screen.render();
  }
}
```

### 8.12.4 脉冲边框

```typescript
/**
 * 脉冲边框：使元素边框颜色或亮度周期性变化
 * 适合需要吸引用户注意的提示元素
 */
class PulsingBorder {
  private timer: ReturnType<typeof setInterval> | null = null;
  private phase = 0;

  constructor(
    private element: Widgets.BoxElement,
    private screen: Widgets.Screen,
    private baseColor: string = 'yellow',
    private pulseSpeed: number = 1.0,  // 速度倍率
  ) {}

  start(): void {
    this.timer = setInterval(() => {
      this.phase += 0.05 * this.pulseSpeed;

      // 使用正弦波控制亮度
      const brightness = (Math.sin(this.phase) + 1) / 2; // 0-1
      const intensity = Math.round(155 + brightness * 100); // 155-255

      // 颜色逐步在基础色和亮色之间变化
      const borderColor = brightness > 0.5
        ? `${this.baseColor}`
        : `#${intensity.toString(16).repeat(3)}`;

      this.element.style.border = { fg: borderColor as any };
      this.screen.render();
    }, 50);
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    this.element.style.border = { fg: this.baseColor as any };
    this.screen.render();
  }
}
```

### 8.12.5 颜色脉冲（色相循环）

```typescript
/**
 * HSL 色相循环：在 360 度色环上平滑过渡
 */
function hslToRgb(h: number, s: number, l: number): { r: number; g: number; b: number } {
  h = h / 360;
  s = s / 100;
  l = l / 100;

  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => {
    const k = (n + h * 12) % 12;
    return l - a * Math.max(Math.min(k - 3, 9 - k, 1), -1);
  };

  return {
    r: Math.round(f(0) * 255),
    g: Math.round(f(8) * 255),
    b: Math.round(f(4) * 255),
  };
}

class ColorPulseAnimation {
  private timer: ReturnType<typeof setInterval> | null = null;
  private hue = 0;

  constructor(
    private element: Widgets.BoxElement,
    private screen: Widgets.Screen,
    private speed: number = 30,  // 每秒色相变化度数
    private saturation: number = 80,
    private lightness: number = 60,
  ) {}

  start(): void {
    this.timer = setInterval(() => {
      this.hue = (this.hue + this.speed * 0.05) % 360;
      const { r, g, b } = hslToRgb(this.hue, this.saturation, this.lightness);

      // 更新元素前景色
      this.element.style.fg = { r, g, b } as any;
      this.screen.render();
    }, 50);
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
```

### 8.12.6 振幅与频率控制

所有脉冲/呼吸效果都支持两个关键参数：

```typescript
interface PulseOptions {
  /** 振幅 (0.0-1.0)：控制变化幅度，0 为静止 */
  amplitude: number;
  /** 频率倍率 (0.1-5.0)：控制变化速度，1.0 为默认 */
  frequency: number;
}

/**
 * 统一的脉冲控制器
 */
class PulseController {
  private animations: Array<{
    stop: () => void;
    setAmplitude: (a: number) => void;
    setFrequency: (f: number) => void;
  }> = [];

  register(animation: {
    stop: () => void;
    setAmplitude: (a: number) => void;
    setFrequency: (f: number) => void;
  }): void {
    this.animations.push(animation);
  }

  setGlobalAmplitude(a: number): void {
    this.animations.forEach(anim => anim.setAmplitude(a));
  }

  setGlobalFrequency(f: number): void {
    this.animations.forEach(anim => anim.setFrequency(f));
  }

  pause(): void {
    this.setGlobalAmplitude(0);
  }

  resume(): void {
    this.setGlobalAmplitude(1);
  }

  stopAll(): void {
    this.animations.forEach(anim => anim.stop());
    this.animations = [];
  }
}
```

## 8.13 无障碍动画

确保动画不会对特定用户群体造成不适，并提供足够的控制选项。

### 8.13.1 prefers-reduced-motion 检测

```typescript
/**
 * 检测用户是否在 OS 级别开启了"减少动效"偏好
 */
function prefersReducedMotion(): boolean {
  // 方式 1：通过终端转义序列查询
  // DECRPM 报告 — 并非所有终端支持
  try {
    process.stdout.write('\x1b[?1$p'); // 查询 DECRPM 状态
  } catch {
    // 忽略
  }

  // 方式 2：通过环境变量检测
  const envHints = [
    process.env.WEBKIT_FORCE_COMPOSITING_MODE,
    process.env.NO_EMOTICON,  // 部分 Linux 发行版使用
  ];

  // 方式 3：通过 NO_COLOR 等标准推断简化渲染需求
  if (process.env.NO_COLOR || process.env.TERM === 'dumb') {
    return true;
  }

  // 方式 4：配置文件检测
  // 用户可在 ~/.claude/config 中设置
  // animation: reduced | no-preference

  return false;
}

/**
 * 简化动画配置
 */
type MotionPreference = 'no-preference' | 'reduced' | 'no-animation';

function getMotionPreference(): MotionPreference {
  if (process.env.CLAUDE_ANIMATION === 'none') return 'no-animation';
  if (process.env.CLAUDE_ANIMATION === 'reduced') return 'reduced';

  // 检测 OS 级偏好（macOS 辅助功能 → "减少动效"）
  if (prefersReducedMotion()) return 'reduced';

  return 'no-preference';
}
```

### 8.13.2 NO_COLOR 与 TERM 环境变量

```typescript
interface AnimationEnvironment {
  /** 是否支持颜色 */
  hasColor: boolean;
  /** 是否支持 Unicode */
  hasUnicode: boolean;
  /** 终端类型 */
  term: string;
  /** 建议最大帧率 */
  suggestedMaxFPS: number;
}

function detectAnimationEnvironment(): AnimationEnvironment {
  const term = process.env.TERM || 'unknown';
  const hasNoColor = process.env.NO_COLOR !== undefined;

  // 根据终端类型判断能力
  const isDumb = term === 'dumb';
  const isSimple = ['xterm', 'vt100', 'linux'].includes(term);

  return {
    hasColor: !hasNoColor && !isDumb,
    hasUnicode: !isDumb && !isSimple,
    term,
    suggestedMaxFPS: isDumb ? 0 : isSimple ? 8 : 30,
  };
}

/**
 * 根据终端能力选择适当的 spinner 序列
 */
function selectSpinnerByEnvironment(
  env: AnimationEnvironment,
): { frames: string[]; interval: number } {
  if (!env.hasUnicode) {
    // 回退到 ASCII-only spinner
    return { frames: ['-', '\\', '|', '/'], interval: 150 };
  }
  if (!env.hasColor) {
    return { frames: ['◐', '◓', '◑', '◒'], interval: 120 };
  }
  // 完整能力
  return {
    frames: ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'],
    interval: 100,
  };
}
```

### 8.13.3 动画速度控制（用户可配置）

```typescript
/**
 * 全局动画速度控制器
 * 所有动画组件都应通过此控制器获取实际间隔/延迟
 */
class AnimationSpeedController {
  private static instance: AnimationSpeedController;

  /** 速度倍率：1.0 = 正常，2.0 = 两倍快，0.5 = 半速 */
  private speedMultiplier = 1.0;

  static getInstance(): AnimationSpeedController {
    if (!AnimationSpeedController.instance) {
      AnimationSpeedController.instance = new AnimationSpeedController();
    }
    return AnimationSpeedController.instance;
  }

  /** 设置速度倍率 */
  setSpeed(multiplier: number): void {
    this.speedMultiplier = Math.max(0, Math.min(10, multiplier));
  }

  /** 获取实际延迟（已应用速度倍率） */
  getDelay(baseDelay: number): number {
    if (this.speedMultiplier === 0) return Infinity; // 完全停止
    return baseDelay / this.speedMultiplier;
  }

  /** 获取实际帧间隔 */
  getInterval(baseInterval: number): number {
    if (this.speedMultiplier === 0) return 60000; // 停止时用长间隔
    return baseInterval / this.speedMultiplier;
  }

  get multiplier(): number {
    return this.speedMultiplier;
  }
}

// 使用示例
// const actualDelay = AnimationSpeedController.getInstance().getDelay(100);
```

### 8.13.4 各动画类型的静态替代方案

为 `reduced-motion` 偏好提供静态替代：

| 动画类型 | 正常 | 简化（reduced） | 无动画 |
|---------|------|----------------|--------|
| Spinner | 旋转字符动画 | 固定图标 + "加载中"文字 | 纯文字 "加载中..." |
| 打字机 | 逐字出现 | 逐词出现（50ms/词） | 整段一次显示 |
| 进度条 | 增量填充 | 仅百分比数字更新 | 完成后显示结果 |
| 呼吸 | 脉冲/颜色变化 | 静态图标 | 无 |
| 过渡 | 淡入/滑入 | 直接显示/隐藏 | 立即切换 |

```typescript
class ReducedMotionAdapter {
  constructor(private preference: MotionPreference) {}

  /** 获取打字机效果适配器 */
  createTypewriter(renderFn: (text: string) => void): {
    update: (text: string) => void;
    flush: () => void;
  } {
    if (this.preference === 'no-animation') {
      // 无动画模式：一次性渲染所有文本
      return {
        update: (text: string) => renderFn(text),
        flush: () => {},
      };
    }

    if (this.preference === 'reduced') {
      // 简化模式：按段落分批显示
      let buffer = '';
      return {
        update: (chunk: string) => {
          buffer += chunk;
          // 每遇到句号或换行渲染一次
          if (chunk.includes('.') || chunk.includes('\n')) {
            renderFn(buffer);
          }
        },
        flush: () => {
          if (buffer) renderFn(buffer);
          buffer = '';
        },
      };
    }

    // 正常模式：传入标准打字机实现
    return {
      update: () => {},
      flush: () => {},
    };
  }

  /** 获取 spinner 静态替代 */
  createSpinnerIndicator(parent: Widgets.BoxElement, screen: Widgets.Screen): {
    start: (text: string) => void;
    stop: () => void;
  } {
    if (this.preference === 'no-animation') {
      let indicator: Widgets.BoxElement | null = null;
      return {
        start: (text: string) => {
          indicator = blessed.box({
            parent,
            top: 0, left: 0,
            width: '100%-2', height: 1,
            content: `  ${text}...`,
            style: { fg: 'yellow', bg: '#1a1a1a' },
          });
          screen.render();
        },
        stop: () => {
          if (indicator) {
            parent.remove(indicator);
            indicator = null;
            screen.render();
          }
        },
      };
    }

    // reduced 模式：静态图标
    let indicator: Widgets.BoxElement | null = null;
    return {
      start: (text: string) => {
        indicator = blessed.box({
          parent,
          top: 0, left: 0,
          width: '100%-2', height: 1,
          content: `  {yellow-fg}⟳ ${text}{/yellow-fg}`,
          tags: true,
          style: { bg: '#1a1a1a' },
        });
        screen.render();
      },
      stop: () => {
        if (indicator) {
          parent.remove(indicator);
          indicator = null;
          screen.render();
        }
      },
    };
  }
}
```

### 8.13.5 WCAG 指南映射

| WCAG 标准 | TUI 动画对应实现 | 检查项 |
|-----------|----------------|--------|
| **2.2.2 暂停/停止/隐藏** | 全局暂停热键 (`Ctrl+P`)、聚焦失焦自动暂停 | 所有动画持续时间 < 5 秒或有暂停机制 |
| **2.3.1 三次闪烁** | 限制帧率 ≥ 60ms（< 16.6Hz） | 任何 1 秒内不超过 3 次亮度变化 |
| **2.3.2 三次闪烁阈值** | 禁止纯红交替闪烁 | 避免 `red←→white` 交替 |
| **1.4.1 颜色使用** | 动画不依赖颜色传达信息 | Spinner + 文字双重指示 |
| **1.4.4 文本缩放** | 不使用固定像素尺寸 | 所有元素使用相对尺寸 |

```typescript
/**
 * 安全检查：验证动画参数符合无障碍标准
 */
function validateAnimationSafety(params: {
  minFrameInterval: number;
  usesRedFlash: boolean;
  duration: number;
  hasPauseControl: boolean;
}): { passed: boolean; warnings: string[] } {
  const warnings: string[] = [];

  if (params.minFrameInterval < 60) {
    warnings.push(
      `帧间隔 ${params.minFrameInterval}ms 低于推荐的 60ms（≥16.6Hz 可能触发闪烁风险）`
    );
  }

  if (params.usesRedFlash) {
    warnings.push('使用纯红交替闪烁违反 WCAG 2.3.1 标准');
  }

  if (params.duration > 5000 && !params.hasPauseControl) {
    warnings.push(`动画持续 ${params.duration}ms > 5s 但缺少暂停控制`);
  }

  return {
    passed: warnings.length === 0,
    warnings,
  };
}
```

### 8.13.6 暂停全部动画热键

```typescript
class GlobalAnimationPauseController {
  private paused = false;
  private callbacks: Array<(paused: boolean) => void> = [];

  constructor(screen: Widgets.Screen) {
    // 注册全局热键 Ctrl+P 暂停/恢复所有动画
    screen.key(['C-p'], () => {
      this.togglePause();
    });

    // ESC 键单独暂停，后续操作恢复
    screen.key(['escape'], () => {
      if (!this.paused) this.pause();
    });

    // 鼠标活动自动恢复
    screen.on('mouse', () => {
      if (this.paused) this.resume();
    });
  }

  register(cb: (paused: boolean) => void): void {
    this.callbacks.push(cb);
  }

  unregister(cb: (paused: boolean) => void): void {
    this.callbacks = this.callbacks.filter(c => c !== cb);
  }

  togglePause(): void {
    this.paused ? this.resume() : this.pause();
  }

  pause(): void {
    if (this.paused) return;
    this.paused = true;
    this.callbacks.forEach(cb => cb(true));
  }

  resume(): void {
    if (!this.paused) return;
    this.paused = false;
    this.callbacks.forEach(cb => cb(false));
  }

  get isPaused(): boolean {
    return this.paused;
  }
}
```

### 8.13.7 癫痫安全

```typescript
/**
 * 安全帧间隔守卫
 * 确保任何动画帧间隔不低于安全阈值
 */
const SAFE_MIN_FRAME_INTERVAL_MS = 60; // ≈ 16.6 Hz，低于闪烁阈值

function safeInterval(requestedInterval: number): number {
  return Math.max(requestedInterval, SAFE_MIN_FRAME_INTERVAL_MS);
}

/**
 * 安全动画定时器包装
 */
class SafeAnimationTimer {
  private timer: ReturnType<typeof setInterval> | null = null;

  start(callback: () => void, requestedInterval: number): void {
    this.stop();
    const safeMs = safeInterval(requestedInterval);
    this.timer = setInterval(callback, safeMs);
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}

/**
 * 动画安全检查清单（运行时自查）
 */
function runAnimationSafetyCheck(): string[] {
  const issues: string[] = [];

  // 检查是否设置了 NO_COLOR
  if (process.env.NO_COLOR) {
    issues.push('NO_COLOR 已设置，应禁用彩色动画');
  }

  // 检查终端类型
  if (process.env.TERM === 'dumb') {
    issues.push('TERM=dumb，应禁用所有动画');
  }

  // 检查是否可通过配置文件禁用
  const configPath = process.env.HOME + '/.claude/config';
  // 实际应用时可读取配置文件

  return issues;
}
```

## 8.14 CSS 风格动画缓动函数

将 Web 动画的缓动概念引入 TUI，使动画节奏更自然。

### 8.14.1 标准缓动函数

```typescript
/**
 * 缓动函数类型
 * 输入 t: 0-1（时间进度）
 * 输出: 0-1（动画进度），可超调
 */
type EasingFunction = (t: number) => number;

// ── 线性 ──
const linear: EasingFunction = (t) => t;

// ── ease（默认）─ 缓慢开始，加速，缓慢结束 ──
const ease: EasingFunction = (t) => {
  // 三次贝塞尔近似 cubic-bezier(0.25, 0.1, 0.25, 1.0)
  return t < 0.5
    ? 2 * t * t
    : -1 + (4 - 2 * t) * t;
};

// ── ease-in ── 缓慢开始，加速 ──
const easeIn: EasingFunction = (t) => t * t * t;

// ── ease-out ── 快速开始，减速 ──
const easeOut: EasingFunction = (t) => {
  return 1 - Math.pow(1 - t, 3);
};

// ── ease-in-out ── 缓慢开始和结束 ──
const easeInOut: EasingFunction = (t) => {
  return t < 0.5
    ? 4 * t * t * t
    : 1 - Math.pow(-2 * t + 2, 3) / 2;
};
```

### 8.14.2 三次贝塞尔实现

```typescript
/**
 * 三次贝塞尔曲线插值
 * 支持自定义控制点，类似 CSS cubic-bezier()
 */
function cubicBezier(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): EasingFunction {
  const ZERO_LIMIT = 1e-6;

  // 牛顿法求 x 对应的 t
  function sampleCurveDerivativeX(t: number): number {
    return 3 * (1 - t) * (1 - t) * x1 + 6 * (1 - t) * t * (x2 - x1) + 3 * t * t * (1 - x2);
  }

  function sampleCurveX(t: number): number {
    return ((1 - t) * (1 - t) * (1 - t)) * 0 +
      3 * ((1 - t) * (1 - t)) * t * x1 +
      3 * (1 - t) * (t * t) * x2 +
      (t * t * t) * 1;
  }

  function sampleCurveY(t: number): number {
    return ((1 - t) * (1 - t) * (1 - t)) * 0 +
      3 * ((1 - t) * (1 - t)) * t * y1 +
      3 * (1 - t) * (t * t) * y2 +
      (t * t * t) * 1;
  }

  function solveCurveX(x: number): number {
    let t0 = 0;
    let t1 = 1;
    let t2 = x;
    let x2_ = x;

    for (let i = 0; i < 8; i++) {
      x2_ = sampleCurveX(t2) - x;
      if (Math.abs(x2_) < ZERO_LIMIT) return t2;
      const d2 = sampleCurveDerivativeX(t2);
      if (Math.abs(d2) < ZERO_LIMIT) break;
      t2 = t2 - x2_ / d2;
    }

    t2 = x;
    if (t2 < t0) return t0;
    if (t2 > t1) return t1;

    while (t0 < t1) {
      x2_ = sampleCurveX(t2);
      if (Math.abs(x2_ - x) < ZERO_LIMIT) return t2;
      if (x > x2_) t0 = t2;
      else t1 = t2;
      t2 = (t1 - t0) / 2 + t0;
    }

    return t2;
  }

  return (t: number) => {
    if (t <= 0) return 0;
    if (t >= 1) return 1;
    return sampleCurveY(solveCurveX(t));
  };
}

// CSS 标准缓动的贝塞尔实现
const easingFunctions = {
  linear:         cubicBezier(0, 0, 1, 1),
  ease:           cubicBezier(0.25, 0.1, 0.25, 1.0),
  'ease-in':      cubicBezier(0.42, 0, 1.0, 1.0),
  'ease-out':     cubicBezier(0, 0, 0.58, 1.0),
  'ease-in-out':  cubicBezier(0.42, 0, 0.58, 1.0),
};
```

### 8.14.3 阶跃缓动函数

```typescript
/**
 * step-start：直接跳到终点
 */
const stepStart: EasingFunction = () => 1;

/**
 * step-end：保持起点，结束时跳到终点
 */
const stepEnd: EasingFunction = (t) => t >= 1 ? 1 : 0;

/**
 * steps(N)：将动画分为 N 个离散跳跃
 * @param count 跳跃次数
 * @param position 'start' | 'end' — 跳跃发生的时刻
 */
function steps(count: number, position: 'start' | 'end' = 'end'): EasingFunction {
  return (t: number) => {
    if (t <= 0) return 0;
    if (t >= 1) return 1;

    const step = position === 'start'
      ? Math.ceil(t * count) / count
      : Math.floor(t * count) / count;

    return step;
  };
}

// 使用示例
// const step4End = steps(4, 'end');  // 0 → 0.25 → 0.5 → 0.75 → 1
// const step4Start = steps(4, 'start'); // 0.25 → 0.5 → 0.75 → 1 → 1
```

### 8.14.4 弹性与弹跳缓动

```typescript
/**
 * 弹性缓动：类似弹簧振荡
 * @param amplitude 振幅倍率
 * @param period 振荡周期
 */
function elasticEaseOut(
  amplitude: number = 1,
  period: number = 0.4,
): EasingFunction {
  return (t: number) => {
    if (t <= 0) return 0;
    if (t >= 1) return 1;

    const s = period / (2 * Math.PI) * Math.asin(1 / amplitude);
    return amplitude * Math.pow(2, -10 * t) *
      Math.sin((t - s) * (2 * Math.PI) / period) + 1;
  };
}

function elasticEaseIn(
  amplitude: number = 1,
  period: number = 0.4,
): EasingFunction {
  return (t: number) => {
    if (t <= 0) return 0;
    if (t >= 1) return 1;

    const s = period / (2 * Math.PI) * Math.asin(1 / amplitude);
    return -(amplitude * Math.pow(2, 10 * (t - 1)) *
      Math.sin((t - 1 - s) * (2 * Math.PI) / period));
  };
}

/**
 * 弹跳缓出：球落地式弹跳
 */
const bounceEaseOutFn: EasingFunction = (t: number) => {
  if (t < 1 / 2.75) return 7.5625 * t * t;
  if (t < 2 / 2.75) return 7.5625 * (t -= 1.5 / 2.75) * t + 0.75;
  if (t < 2.5 / 2.75) return 7.5625 * (t -= 2.25 / 2.75) * t + 0.9375;
  return 7.5625 * (t -= 2.625 / 2.75) * t + 0.984375;
};

const bounceEaseInFn: EasingFunction = (t: number) => {
  return 1 - bounceEaseOutFn(1 - t);
};
```

### 8.14.5 缓动函数应用于 Spinner 转速

```typescript
/**
 * 带缓动的 Spinner：转速随时间变化
 */
class EasedSpinner {
  private timer: ReturnType<typeof setInterval> | null = null;
  private element: Widgets.BoxElement;
  private frameIndex = 0;
  private startTime = 0;
  private totalDuration: number;
  private baseInterval: number;

  constructor(
    parent: Widgets.BoxElement,
    private screen: Widgets.Screen,
    private frames: string[] = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'],
    private easing: EasingFunction = easeInOut,
    duration: number = 2000,    // 整个加速过程持续 2s
    baseInterval: number = 150,
  ) {
    this.totalDuration = duration;
    this.baseInterval = baseInterval;

    this.element = blessed.box({
      parent,
      top: 0, left: 0,
      width: '100%-2', height: 1,
      content: '',
      style: { fg: 'yellow', bg: '#1a1a1a' },
      tags: true,
    });
  }

  start(text: string = 'AI 思考中...'): void {
    this.startTime = Date.now();
    this.frameIndex = 0;

    this.timer = setInterval(() => {
      const elapsed = Date.now() - this.startTime;
      const progress = Math.min(elapsed / this.totalDuration, 1);
      const eased = this.easing(progress);

      // 当前间隔：从 baseInterval 逐渐降到 baseInterval * 0.3
      const currentInterval = this.baseInterval * (1 - eased * 0.7);

      this.frameIndex = (this.frameIndex + 1) % this.frames.length;
      this.element.setContent(
        `  {yellow-fg}{bold}${this.frames[this.frameIndex]} ${text}{/bold}{/yellow-fg}`
      );
      this.screen.render();

      // 动态调整下一帧间隔
      if (this.timer) {
        clearInterval(this.timer);
        this.timer = setInterval(arguments.callee as any, currentInterval);
      }
    }, this.baseInterval);
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.element) {
      this.element.detach();
      this.screen.render();
    }
  }
}
```

### 8.14.6 缓动函数应用于打字机字符延迟

```typescript
/**
 * 带缓动的打字机效果
 * 控制字符出现的节奏
 */
async function* easedTypewriter(
  text: string,
  easing: EasingFunction = easeOut,
  baseDelay: number = 25,
  minDelay: number = 5,
): AsyncGenerator<LLMEvent> {
  const totalChars = text.length;

  for (let i = 0; i < totalChars; i++) {
    yield { type: 'text', content: text[i] };

    const progress = i / Math.max(totalChars - 1, 1);
    const eased = easing(progress);

    // 映射缓动值到延迟范围
    const delay = minDelay + (baseDelay - minDelay) * (1 - eased);
    await delay(delay);
  }
}

/**
 * 帧插值：在动画帧之间生成中间帧，使过渡更平滑
 */
function interpolateFrames<T>(
  startValue: T,
  endValue: T,
  easing: EasingFunction,
  steps: number,
): Array<{ value: T; t: number }> {
  const result: Array<{ value: T; t: number }> = [];

  if (typeof startValue === 'number' && typeof endValue === 'number') {
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      const easedT = easing(t);
      const value = (startValue as number) +
        (endValue as number - (startValue as number)) * easedT;
      result.push({ value: value as T, t: easedT });
    }
  }

  return result;
}

// 使用示例：生成 Spinner 旋转的插值间隔
// const intervals = interpolateFrames(150, 30, elasticEaseOut(0.5), 20);
// intervals 可用来生成非均匀定时器序列
```

### 8.14.7 缓动函数速查表

| 函数 | CSS 等效 | 效果 | 适用动画 |
|------|---------|------|---------|
| `linear` | `linear` | 匀速 | 进度条 |
| `easeIn` | `ease-in` | 慢→快 | 淡出、滑出 |
| `easeOut` | `ease-out` | 快→慢 | 淡入、滑入、展开 |
| `easeInOut` | `ease-in-out` | 慢→快→慢 | 交叉淡变 |
| `elasticEaseOut(1, 0.4)` | — | 弹性振荡后稳定 | 弹跳展开、通知出现 |
| `bounceEaseOut` | `cubic-bezier(0.68, -0.55, 0.27, 1.55)` | 球落地弹跳 | 消息弹入 |
| `steps(4, 'end')` | `steps(4, end)` | 离散跳跃 | 状态切换指示器 |
| `cubicBezier(0.1, 0.8, 0.3, 1.2)` | 自定义 | 指定控制点 | 自定义动画曲线 |

---

**实践：** 修改 `llm-chat.ts`，将 Spinner 动画换一种风格（如 bounce 或 arc 序列），同时调整动画速度参数体验不同效果。

**上一章：** [第七章：工具调用 UI 实现](07-tool-calls.md)

**下一步：** [第九章：Markdown 渲染与富文本展示](09-markdown-rendering.md)
