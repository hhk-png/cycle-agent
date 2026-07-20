# 第七章 工具调用 UI 实现

## 7.1 Function Calling 简介

大语言模型的**工具调用**（Function Calling / Tool Use）是指 LLM 在对话过程中调用外部工具/API 的能力。这是 LLM 与外部世界交互的关键机制。

### 7.1.1 调用流程

```
用户: "北京今天天气怎么样？"
　　↓
AI 识别用户意图 → 选择工具 get_weather
　　↓
AI 响应中包含工具调用:
  get_weather(city: "Beijing")
　　↓
客户端执行工具调用 → 获取天气 API 数据
　　↓
工具结果返回给 AI:
  {"temperature": 25, "condition": "晴"}
　　↓
AI 基于工具结果生成最终回答:
  "北京今天 25°C，天气晴朗。"
```

### 7.1.2 工具定义（Schema）

在使用工具调用前，需要向模型声明可用的工具：

```typescript
// 工具定义 —— 符合 OpenAI/Anthropic 规范
const TOOLS = {
  get_weather: {
    name: 'get_weather',
    description: '获取指定城市的当前天气信息',
    input_schema: {
      type: 'object',
      properties: {
        city: { type: 'string', description: '城市名称（英文）' },
        unit: { type: 'string', enum: ['celsius', 'fahrenheit'] },
      },
      required: ['city'],
    },
  },
  calculate: {
    name: 'calculate',
    description: '执行数学计算',
    input_schema: {
      type: 'object',
      properties: {
        expression: { type: 'string', description: '数学表达式' },
      },
      required: ['expression'],
    },
  },
  search_web: {
    name: 'search_web',
    description: '搜索互联网信息',
    input_schema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: '搜索关键词' },
      },
      required: ['query'],
    },
  },
};
```

## 7.2 工具调用的 TUI 设计目标

在终端中展示工具调用不同于 Web UI，需要满足以下目标：

1. **透明性** — 用户能看到 AI 在调用什么工具、传入什么参数
2. **实时反馈** — 工具执行的状态（执行中/完成/失败）一目了然
3. **可读性** — 参数和结果以格式化方式展示在狭窄的终端中
4. **非侵入性** — 工具卡片不打断对话流，用户可专注阅读
5. **可追溯** — 历史消息中的工具调用可以回顾查看

## 7.3 工具卡片三态设计

每个工具调用在 UI 中经历三个状态，每个状态有对应的视觉表现。

### 7.3.1 调用中状态（Pending/Running）

```
┌─ 🔧 工具调用 ────────────────────────────────────┐
│  工具: get_weather                                 │
│  参数:                                             │
│    city: "Beijing"                                 │
│    unit: "celsius"                                 │
│                                                   │
│  ⏳ 执行中...                                     │
└───────────────────────────────────────────────────┘
```

### 7.3.2 完成状态（Completed）

```
┌─ ✅ 工具结果 ────────────────────────────────────┐
│  工具: get_weather                                 │
│                                                   │
│  {                                                │
│    "city": "Beijing",                             │
│    "temperature": 25,                             │
│    "condition": "☀️ 晴",                          │
│    "humidity": 45                                 │
│  }                                                │
│                                                   │
└───────────────────────────────────────────────────┘
```

### 7.3.3 错误状态（Error）

```
┌─ ❌ 工具错误 ────────────────────────────────────┐
│  工具: get_weather                                 │
│  参数:                                             │
│    city: "InvalidCity"                             │
│                                                   │
│  错误: API 返回 404 - 未找到该城市                 │
│                                                   │
│  [重试]                                           │
└───────────────────────────────────────────────────┘
```

## 7.4 工具调用 UI 的实现解析

### 7.4.1 数据模型

```typescript
/** 工具调用的 UI 展示状态 */
interface ToolCallDisplay {
  name: string;                           // 工具名称
  args: Record<string, unknown>;          // 调用参数
  result?: string;                        // 返回结果
  status: 'pending' | 'running' | 'completed' | 'error';
  error?: string;                         // 错误信息
}
```

### 7.4.2 渲染工具调用卡片

```typescript
/**
 * 展示工具调用中的卡片（紫色边框，⏳ 状态）
 */
private showToolCall(name: string, args: Record<string, unknown>): void {
  // 格式化参数为可读的键值对
  const argsStr = Object.entries(args)
    .map(([k, v]) => `  ${k}: {yellow-fg}${JSON.stringify(v)}{/yellow-fg}`)
    .join('\n');

  this.currentToolCallEl = blessed.box({
    parent: this.chatBox,
    top: 0,
    left: 0,
    width: '100%-2',
    height: 'shrink',
    content:
      `{magenta-fg}┌─ 🔧 {bold}工具调用{/bold} ─────────────────────────────────┐{/magenta-fg}\n` +
      `{magenta-fg}│{/magenta-fg}  工具: {bold}${name}{/bold}\n` +
      `{magenta-fg}│{/magenta-fg}  参数:\n${argsStr}\n` +
      `{magenta-fg}│{/magenta-fg}\n` +
      `{magenta-fg}│{/magenta-fg}  {yellow-fg}⏳ 执行中...{/yellow-fg}\n` +
      `{magenta-fg}└────────────────────────────────────────────────┘{/magenta-fg}`,
    style: { fg: 'white', bg: '#1a1a1a' },
    tags: true,
    wrap: true,
    shrink: true,
  });

  this.addSpacer();
  this.screen.render();
  this.scrollToBottom();
}

/**
 * 展示工具结果卡片（青色边框，✅ 状态）
 */
private showToolResult(name: string, result: string): void {
  // 清除之前的 tool call 卡片
  this.clearToolCall();

  this.currentToolResultEl = blessed.box({
    parent: this.chatBox,
    top: 0,
    left: 0,
    width: '100%-2',
    height: 'shrink',
    content:
      `{cyan-fg}┌─ ✅ {bold}工具结果{/bold} ─────────────────────────────────┐{/cyan-fg}\n` +
      `{cyan-fg}│{/cyan-fg}  工具: {bold}${name}{/bold}\n` +
      `{cyan-fg}│{/cyan-fg}\n` +
      `{green-fg}${this.escapeTags(result)}{/green-fg}\n` +
      `{cyan-fg}│{/cyan-fg}\n` +
      `{cyan-fg}└────────────────────────────────────────────────┘{/cyan-fg}`,
    style: { fg: 'white', bg: '#1a1a1a' },
    tags: true,
    wrap: true,
    shrink: true,
  });

  this.addSpacer();
  this.screen.render();
  this.scrollToBottom();
}

/**
 * 展示错误卡片（红色边框，❌ 状态）
 */
private showToolError(name: string, args: Record<string, unknown>, error: string): void {
  const argsStr = Object.entries(args)
    .map(([k, v]) => `  ${k}: {yellow-fg}${JSON.stringify(v)}{/yellow-fg}`)
    .join('\n');

  const card = blessed.box({
    parent: this.chatBox,
    top: 0, left: 0,
    width: '100%-2', height: 'shrink',
    content:
      `{red-fg}┌─ ❌ {bold}工具错误{/bold} ─────────────────────────────────┐{/red-fg}\n` +
      `{red-fg}│{/red-fg}  工具: {bold}${name}{/bold}\n` +
      `{red-fg}│{/red-fg}  参数:\n${argsStr}\n` +
      `{red-fg}│{/red-fg}\n` +
      `{red-fg}│{/red-fg}  错误: {white-fg}${this.escapeTags(error)}{/white-fg}\n` +
      `{red-fg}└────────────────────────────────────────────────┘{/red-fg}`,
    style: { fg: 'white', bg: '#1a1a1a' },
    tags: true, wrap: true, shrink: true,
  });
}
```

### 7.4.3 工具调用与文本的交替显示

真实 LLM 交互中，工具调用可能嵌在文本流中，需要无缝交替：

```typescript
for await (const event of llmStream) {
  switch (event.type) {
    case 'text':
      // 如果刚从 tool_call/tool_result 切换过来
      if (state === 'tool_result') {
        clearToolDisplay();  // 清除工具结果卡片
      }

      // 累加文本并更新 UI
      fullResponse += event.content;
      updateAssistantMessage(fullResponse);
      break;

    case 'tool_call':
      // 文本流暂停，展示工具调用卡片
      showToolCall(event.name, event.args);
      break;

    case 'tool_result':
      // 调用完成 → 将工具卡片切换为结果卡片
      showToolResult(event.name, event.result);
      break;
  }
}
```

## 7.5 Mock 中的工具调用模拟

在 `examples/llm-chat.ts` 中，我们模拟了多种工具调用场景：

### 7.5.1 天气查询

```typescript
if (msg.includes('天气') || msg.includes('weather')) {
  // 1. 先输出一段思考文本（流式）
  const phrase = "我来查询天气数据...";
  for (const ch of phrase) {
    yield { type: 'text', content: ch };
    await delay(25 + Math.random() * 20);
  }

  // 2. 发起工具调用
  yield { type: 'tool_call', name: 'get_weather', args: { city, unit: 'celsius' } };

  // 3. 模拟工具执行延迟
  await delay(1000 + Math.random() * 800);

  // 4. 返回工具结果（模拟数据）
  const result = {
    city, temperature: 25,
    condition: '☀️ 晴', humidity: 45, wind: '15 km/h',
  };
  yield { type: 'tool_result', name: 'get_weather', result: JSON.stringify(result, null, 2) };

  // 5. 基于工具结果生成最终回答（流式）
  const reply = `\n\n🌤️ ${city} 天气预报\n\n${result.condition}\n温度：${result.temperature}°C\n湿度：${result.humidity}%\n`;
  for (const ch of reply) {
    yield { type: 'text', content: ch };
    await delay(15 + Math.random() * 10);
  }
}
```

### 7.5.2 数学计算

```typescript
if (msg.includes('计算') || /\d+\s*[+\-*/]\s*\d+/.test(msg)) {
  yield { type: 'text', content: '正在计算...' };
  await delay(200);

  yield { type: 'tool_call', name: 'calculate', args: { expression: expr } };
  await delay(600 + Math.random() * 400);

  let result: number;
  try {
    result = Function(`"use strict"; return (${expr})`)();
  } catch {
    result = Math.floor(Math.random() * 10000);
  }
  yield { type: 'tool_result', name: 'calculate', result: `${expr} = ${result}` };

  const reply = `\n\n计算结果：${expr} = ${result}`;
  for (const ch of reply) {
    yield { type: 'text', content: ch };
    await delay(20 + Math.random() * 10);
  }
}
```

## 7.6 扩展：多种工具调用模式

### 7.6.1 并行工具调用

真实场景中，LLM 可能同时调用多个工具：

```typescript
// 并行调用多个工具
yield {
  type: 'parallel_tool_calls',
  calls: [
    { name: 'get_weather', args: { city: 'Beijing' } },
    { name: 'get_weather', args: { city: 'Shanghai' } },
    { name: 'get_air_quality', args: { city: 'Beijing' } },
  ],
};
```

```typescript
/**
 * 并行工具调用 UI：使用多行卡片展示
 */
private showParallelToolCalls(calls: Array<{name: string; args: Record<string, unknown>}>): void {
  const items = calls.map(c =>
    `  • {bold}${c.name}{/bold}({yellow-fg}${JSON.stringify(c.args)}{/yellow-fg})`
  ).join('\n');

  const card = blessed.box({
    parent: this.chatBox,
    top: 0, left: 0,
    width: '100%-2', height: 'shrink',
    content:
      `{magenta-fg}┌─ 🔧 {bold}并行工具调用 ({calls.length}个){/bold} ──────────────┐{/magenta-fg}\n` +
      `{magenta-fg}│{/magenta-fg}\n${items}\n` +
      `{magenta-fg}│{/magenta-fg}\n` +
      `{magenta-fg}│{/magenta-fg}  {yellow-fg}⏳ 执行中...{/yellow-fg}\n` +
      `{magenta-fg}└────────────────────────────────────────────────┘{/magenta-fg}`,
    // ...
  });
}
```

#### 个体状态追踪

为每个并行调用维护独立的执行状态，实现逐工具的精细控制：

```typescript
interface ToolCallInstance {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: 'pending' | 'running' | 'completed' | 'error';
  result?: string;
  error?: string;
  startTime: number;
  endTime?: number;
  duration?: number;
}

/**
 * 并发执行多个工具调用，逐个更新状态
 */
private async executeParallelCalls(calls: ToolCallInstance[]): Promise<void> {
  // 初始化所有调用为 pending
  calls.forEach(c => {
    c.status = 'pending';
    c.startTime = Date.now();
  });
  this.renderParallelStatus(calls);

  // 并发执行，每个工具独立追踪
  await Promise.all(calls.map(async (call) => {
    call.status = 'running';
    this.renderParallelStatus(calls);
    try {
      const result = await this.toolEngine.execute(call.name, call.args);
      call.status = 'completed';
      call.result = result;
      call.endTime = Date.now();
      call.duration = call.endTime - call.startTime;
    } catch (err) {
      call.status = 'error';
      call.error = String(err);
      call.endTime = Date.now();
      call.duration = call.endTime - call.startTime;
    }
    this.renderParallelStatus(calls);
  }));
}
```

#### 结果收集与排序

```typescript
interface ParallelResult {
  name: string;
  args: Record<string, unknown>;
  result: string;
  duration: number;     // 执行耗时(ms)
  completedAt: number;  // 完成时间戳
}

/**
 * 收集执行结果，按完成时间排序
 */
private collectParallelResults(calls: ToolCallInstance[]): ParallelResult[] {
  return calls
    .filter(c => c.status === 'completed')
    .map(c => ({
      name: c.name,
      args: c.args,
      result: c.result!,
      duration: c.duration ?? 0,
      completedAt: c.endTime ?? c.startTime,
    }))
    .sort((a, b) => a.completedAt - b.completedAt); // 按完成时间排序
}
```

#### 并发执行视觉布局

展示每个工具的独立进度和执行状态：

```
┌─ 🔧 并行工具调用 (3个) ───────────────────────┐
│                                                  │
│  ⏳ get_weather(Beijing)                         │
│     ████████░░ 65%                               │
│                                                  │
│  ✅ get_weather(Shanghai)                        │
│     ██████████ 100%                              │
│                                                  │
│  🔄 get_air_quality(Beijing)                     │
│     ████░░░░░░ 40%                               │
│                                                  │
│  完成: 1/3  耗时: 1.2s                          │
│                                                  │
└──────────────────────────────────────────────────┘
```

#### 执行速度对比

```typescript
/**
 * 渲染执行速度对比条形图
 */
private renderSpeedComparison(calls: ToolCallInstance[]): string {
  const completed = calls.filter(c => c.status === 'completed' && c.endTime);
  if (completed.length < 2) return '';

  const maxDur = Math.max(...completed.map(c => c.duration ?? 0));
  const bars = completed.map(c => {
    const dur = c.duration ?? 0;
    const barLen = Math.max(1, Math.round((dur / maxDur) * 10));
    const bar = '█'.repeat(barLen) + '░'.repeat(Math.max(0, 10 - barLen));
    return `  {cyan-fg}${bar}{/cyan-fg} ${c.name} {gray-fg}(${dur}ms){/gray-fg}`;
  }).join('\n');

  return `\n{yellow-fg}执行速度对比:{/yellow-fg}\n${bars}`;
}
```

### 7.6.2 工具调用确认

某些场景下，敏感工具需要用户确认后再执行：

```typescript
/**
 * 工具调用确认对话框
 */
private async showToolConfirm(toolName: string, args: Record<string, unknown>): Promise<boolean> {
  return new Promise((resolve) => {
    const confirmBox = blessed.box({
      parent: this.chatBox,
      top: 0, left: 0,
      width: '100%-2', height: 5,
      content:
        `{yellow-fg}┌─ ⚠️ 确认工具调用 ──────────────────────────────┐{/yellow-fg}\n` +
        `{yellow-fg}│{/yellow-fg}  工具: {bold}${toolName}{/bold}\n` +
        `{yellow-fg}│{/yellow-fg}  {bold}[Y] 确认  [N] 拒绝{/bold}\n` +
        `{yellow-fg}└────────────────────────────────────────────────┘{/yellow-fg}`,
      tags: true, style: { fg: 'white', bg: '#1a1a1a' },
    });

    const keyHandler = (ch: any, key: any) => {
      if (key.name === 'y' || key.name === 'Y') {
        this.chatBox.remove(confirmBox);
        this.screen.render();
        resolve(true);
      } else if (key.name === 'n' || key.name === 'N' || key.name === 'escape') {
        this.chatBox.remove(confirmBox);
        this.screen.render();
        resolve(false);
      }
    };

    this.screen.key(['y', 'Y', 'n', 'N', 'escape'], keyHandler);
  });
}
```

#### 风险等级颜色标识

根据工具的风险级别使用不同的颜色提示用户：

```typescript
type RiskLevel = 'low' | 'medium' | 'high';

interface ToolRiskConfig {
  level: RiskLevel;
  color: string;       // 终端颜色标签
  borderColor: string; // 边框颜色
  autoApprove: boolean; // 低风险是否自动放行
}

/**
 * 工具风险配置表
 */
private readonly toolRiskMap: Record<string, ToolRiskConfig> = {
  get_weather:  { level: 'low',    color: '{green-fg}',  borderColor: '{green-fg}',  autoApprove: true },
  calculate:    { level: 'low',    color: '{green-fg}',  borderColor: '{green-fg}',  autoApprove: true },
  search_web:   { level: 'medium', color: '{yellow-fg}', borderColor: '{yellow-fg}', autoApprove: false },
  write_file:   { level: 'high',   color: '{red-fg}',    borderColor: '{red-fg}',    autoApprove: false },
  execute_code: { level: 'high',   color: '{red-fg}',    borderColor: '{red-fg}',    autoApprove: false },
  delete_file:  { level: 'high',   color: '{red-fg}',    borderColor: '{red-fg}',    autoApprove: false },
};

/**
 * 根据风险等级获取确认框标题
 */
private getRiskTitle(level: RiskLevel): string {
  switch (level) {
    case 'low':    return 'ℹ️ 低风险工具调用';
    case 'medium': return '⚠️ 中风险工具调用';
    case 'high':   return '🚨 高风险工具调用';
  }
}
```

不同风险等级的 UI 展示效果：

```
低风险（绿色）:
┌─ ℹ️ 低风险工具调用 ───────────────────────────┐
│  工具: get_weather                               │
│  已自动批准（低风险操作）                          │
└──────────────────────────────────────────────────┘

中风险（黄色）:
┌─ ⚠️ 中风险工具调用 ───────────────────────────┐
│  工具: search_web                                │
│  参数: {"query": "latest news"}                  │
│  [Y] 确认执行  [N] 拒绝                          │
└──────────────────────────────────────────────────┘

高风险（红色）:
┌─ 🚨 高风险工具调用 ───────────────────────────┐
│  工具: delete_file                               │
│  参数: {"path": "/etc/config.json"}              │
│  ⏱ 剩余 15 秒  （超时将自动拒绝）               │
│  [Y] 确认执行  [N] 拒绝                          │
└──────────────────────────────────────────────────┘
```

#### 确认超时自动拒绝

```typescript
/**
 * 带超时的工具调用确认 — 超时自动拒绝
 */
private async showToolConfirmWithTimeout(
  toolName: string,
  args: Record<string, unknown>,
  timeoutMs: number = 15000
): Promise<boolean> {
  return new Promise((resolve) => {
    let resolved = false;
    let remaining = timeoutMs;

    const confirmBox = blessed.box({
      parent: this.chatBox,
      top: 0, left: 0,
      width: '100%-2', height: 7,
      content:
        `{yellow-fg}┌─ ⚠️ 确认工具调用 ──────────────────────────────┐{/yellow-fg}\n` +
        `{yellow-fg}│{/yellow-fg}  工具: {bold}${toolName}{/bold}\n` +
        `{yellow-fg}│{/yellow-fg}  参数: {yellow-fg}${JSON.stringify(args)}{/yellow-fg}\n` +
        `{yellow-fg}│{/yellow-fg}\n` +
        `{yellow-fg}│{/yellow-fg}  {bold}[Y] 确认  [N] 拒绝{/bold}  ` +
        `{gray-fg}({/gray-fg}{yellow-fg}${Math.ceil(remaining / 1000)}{/yellow-fg}{gray-fg}s){/gray-fg}\n` +
        `{yellow-fg}└────────────────────────────────────────────────┘{/yellow-fg}`,
      tags: true, style: { fg: 'white', bg: '#1a1a1a' },
    });

    // 倒计时更新
    const timer = setInterval(() => {
      remaining -= 1000;
      if (remaining <= 0 && !resolved) {
        resolved = true;
        clearInterval(timer);
        this.chatBox.remove(confirmBox);
        this.screen.render();
        resolve(false); // 超时自动拒绝
      } else if (!resolved) {
        // 更新倒计时显示
        confirmBox.setContent(
          `{yellow-fg}┌─ ⚠️ 确认工具调用 ──────────────────────────────┐{/yellow-fg}\n` +
          `{yellow-fg}│{/yellow-fg}  工具: {bold}${toolName}{/bold}\n` +
          `{yellow-fg}│{/yellow-fg}  参数: {yellow-fg}${JSON.stringify(args)}{/yellow-fg}\n` +
          `{yellow-fg}│{/yellow-fg}\n` +
          `{yellow-fg}│{/yellow-fg}  {bold}[Y] 确认  [N] 拒绝{/bold}  ` +
          `{gray-fg}({/gray-fg}{red-fg}${Math.ceil(remaining / 1000)}{/red-fg}{gray-fg}s){/gray-fg}\n` +
          `{yellow-fg}└────────────────────────────────────────────────┘{/yellow-fg}`
        );
        this.screen.render();
      }
    }, 1000);

    const keyHandler = (ch: any, key: any) => {
      if (!resolved) {
        resolved = true;
        clearInterval(timer);
        this.chatBox.remove(confirmBox);
        this.screen.render();
        resolve(key.name === 'y' || key.name === 'Y');
      }
    };

    this.screen.key(['y', 'Y', 'n', 'N', 'escape'], keyHandler);
  });
}
```

#### 批量确认

```typescript
interface PendingToolCall {
  name: string;
  args: Record<string, unknown>;
  riskLevel: RiskLevel;
}

/**
 * 批量工具调用确认 — 一次展示多个待确认的工具
 */
private async showBatchConfirm(calls: PendingToolCall[]): Promise<{
  confirmed: PendingToolCall[];
  rejected: PendingToolCall[];
}> {
  return new Promise((resolve) => {
    const itemsHtml = calls.map((c, i) =>
      `  [{yellow-fg}${i + 1}{/yellow-fg}] {bold}${c.name}{/bold}(${JSON.stringify(c.args)})`
    ).join('\n');

    const box = blessed.box({
      parent: this.chatBox,
      top: 0, left: 0,
      width: '100%-2', height: 'shrink',
      content:
        `{yellow-fg}┌─ ⚠️ 批量确认工具调用 ─────────────────────────┐{/yellow-fg}\n` +
        `{yellow-fg}│{/yellow-fg}  以下 ${calls.length} 个工具需要确认:\n` +
        `${itemsHtml}\n` +
        `{yellow-fg}│{/yellow-fg}\n` +
        `{yellow-fg}│{/yellow-fg}  {bold}[A] 全部接受  [R] 全部拒绝  [1-9] 逐个选择{/bold}\n` +
        `{yellow-fg}└────────────────────────────────────────────────┘{/yellow-fg}`,
      tags: true, style: { fg: 'white', bg: '#1a1a1a' },
    });

    const keyHandler = (ch: any, key: any) => {
      if (key.name === 'a' || key.name === 'A') {
        this.chatBox.remove(box);
        this.screen.render();
        resolve({ confirmed: [...calls], rejected: [] });
      } else if (key.name === 'r' || key.name === 'R' || key.name === 'escape') {
        this.chatBox.remove(box);
        this.screen.render();
        resolve({ confirmed: [], rejected: [...calls] });
      } else if (/^[1-9]$/.test(key.name)) {
        const idx = parseInt(key.name) - 1;
        if (idx < calls.length) {
          const target = calls[idx];
          // 单独确认该工具
          this.chatBox.remove(box);
          this.screen.render();
          this.showToolConfirmWithTimeout(target.name, target.args).then(approved => {
            if (approved) {
              resolve({ confirmed: [target], rejected: calls.filter((_, i) => i !== idx) });
            } else {
              resolve({ confirmed: calls.filter((_, i) => i !== idx), rejected: [target] });
            }
          });
        }
      }
    };

    this.screen.key(['a', 'A', 'r', 'R', 'escape', '1', '2', '3', '4', '5', '6', '7', '8', '9'], keyHandler);
  });
}
```

#### 确认历史记录

```typescript
interface ConfirmationRecord {
  toolName: string;
  argsKey: string;        // 序列化的参数摘要
  approved: boolean;
  timestamp: number;
}

/**
 * 确认历史管理器 — 记住用户对不同工具调用的决策
 */
class ConfirmationHistory {
  private history: ConfirmationRecord[] = [];
  private readonly maxSize = 100;

  /** 记录一次决策 */
  record(toolName: string, args: Record<string, unknown>, approved: boolean): void {
    this.history.push({
      toolName,
      argsKey: JSON.stringify(args),
      approved,
      timestamp: Date.now(),
    });
    if (this.history.length > this.maxSize) {
      this.history.shift();
    }
  }

  /** 查询上次对该工具+参数的决策 */
  getLastDecision(toolName: string, args: Record<string, unknown>): boolean | null {
    const argsKey = JSON.stringify(args);
    for (let i = this.history.length - 1; i >= 0; i--) {
      const h = this.history[i];
      if (h.toolName === toolName && h.argsKey === argsKey) {
        return h.approved;
      }
    }
    return null; // 无历史记录
  }

  /** 获取某工具的统计摘要 */
  getStats(toolName: string): { total: number; approved: number; rejected: number } {
    const entries = this.history.filter(h => h.toolName === toolName);
    return {
      total: entries.length,
      approved: entries.filter(e => e.approved).length,
      rejected: entries.filter(e => !e.approved).length,
    };
  }
}

// 在确认流程中使用历史记录
private async confirmWithHistory(toolName: string, args: Record<string, unknown>): Promise<boolean> {
  const lastDecision = this.confirmHistory.getLastDecision(toolName, args);
  if (lastDecision !== null) {
    // 显示历史决策供用户快速选择
    const reuseMsg = `{gray-fg}上次选择: ${lastDecision ? '✓ 批准' : '✗ 拒绝'}  ` +
      `(按 R 复用上次选择){/gray-fg}`;
    this.addSystemMessage(reuseMsg);

    // 监听 R 键复用
    return new Promise((resolve) => {
      const handler = (ch: any, key: any) => {
        if (key.name === 'r' || key.name === 'R') {
          resolve(lastDecision);
        }
      };
      this.screen.key(['r', 'R'], handler);
      // 同时弹出标准确认框作为后备
      this.showToolConfirm(toolName, args).then(decision => {
        this.confirmHistory.record(toolName, args, decision);
        resolve(decision);
      });
    });
  }

  const decision = await this.showToolConfirm(toolName, args);
  this.confirmHistory.record(toolName, args, decision);
  return decision;
}
```

#### 参数详情预览

```typescript
/**
 * 生成参数预览文本，支持折叠/展开
 */
private renderParamPreview(args: Record<string, unknown>, expanded: boolean): string {
  const entries = Object.entries(args);
  const lines: string[] = [];

  for (const [key, value] of Object.entries(args)) {
    let valueStr: string;
    if (typeof value === 'object' && value !== null) {
      valueStr = expanded
        ? JSON.stringify(value, null, 2)
        : JSON.stringify(value).slice(0, 50) + '...';
    } else {
      valueStr = String(value);
    }
    const typeTag = Array.isArray(value) ? '{magenta-fg}[array]{/magenta-fg}'
      : typeof value === 'object' && value !== null ? '{yellow-fg}{object}{/yellow-fg}'
      : typeof value === 'number' ? '{cyan-fg}(num){/cyan-fg}'
      : typeof value === 'boolean' ? '{green-fg}(bool){/green-fg}'
      : '{blue-fg}(str){/blue-fg}';

    lines.push(`  {cyan-fg}${key}{/cyan-fg}: ${typeTag} ${valueStr}`);
  }

  // 折叠模式下只显示前 3 行
  if (!expanded && entries.length > 3) {
    lines.splice(3);
    lines.push(`  {gray-fg}... 还有 ${entries.length - 3} 个参数 (按 → 展开){/gray-fg}`);
  }

  return lines.join('\n');
}
```

### 7.6.3 链式工具调用

一个工具的结果作为另一个工具的输入：

```
用户: "北京和上海的时差是多少？"
　　↓
tool_call: get_timezone(city: "Beijing")  →  返回 UTC+8
　　↓
tool_call: get_timezone(city: "Shanghai")  →  返回 UTC+8
　　↓
tool_result: 两个城市都在 UTC+8，没有时差
　　↓
最终回答: "北京和上海没有时差。"
```

### 7.6.4 流式工具参数

部分 API 支持工具参数的流式传输（如 Anthropic 的 `input_json_delta`），可以在 UI 中实时展示参数构建过程：

```typescript
// 流式工具参数的 UI 更新
private handleToolInputDelta(delta: string): void {
  if (!this.partialJsonBuffer) {
    this.partialJsonBuffer = '';
  }
  this.partialJsonBuffer += delta;

  // 尝试解析部分 JSON 并更新卡片
  try {
    const partial = JSON.parse(this.partialJsonBuffer);
    if (this.currentToolCallEl) {
      this.currentToolCallEl.setContent(/* 更新参数显示 */);
      this.screen.render();
    }
  } catch {
    // JSON 还不完整，忽略
  }
}
```

#### 实时部分 JSON 解析可视化

```typescript
interface PartialJsonState {
  raw: string;
  parsed: Record<string, unknown> | null;
  validKeys: string[];          // 已成功解析的 key
  parseError: string | null;    // 当前解析错误提示
}

/**
 * 将部分 JSON 解析状态可视化为 UI 文本
 */
private visualizePartialJson(state: PartialJsonState): string {
  const lines: string[] = ['{cyan-fg}参数构建中...{/cyan-fg}\n'];

  // 显示已成功解析的参数（绿色）
  if (state.validKeys.length > 0) {
    const validLines = state.validKeys.map(key =>
      `  {green-fg}✓{/green-fg} {bold}${key}{/bold}: ` +
      `{white-fg}${JSON.stringify(state.parsed?.[key])}{/white-fg}`
    );
    lines.push(...validLines);
  }

  // 显示解析中的提示
  if (state.parseError) {
    lines.push(`  {yellow-fg}⏳ 正在接收: ${state.parseError}{/yellow-fg}`);
  }

  // 显示原始 JSON 缓冲区（带光标）
  lines.push(`\n{yellow-fg}${state.raw}█{/yellow-fg}`);

  return lines.join('\n');
}
```

UI 中的实时效果：

```
┌─ 🔧 工具调用 ────────────────────────────────────┐
│  工具: get_weather                                 │
│  参数:                                             │
│  ✓ city: "Beijing"                                 │
│  ⏳ 正在接收下一个参数...                           │
│                                                    │
│  {"city": "Beijing", █                             │
└────────────────────────────────────────────────────┘

→ 片刻后:

┌─ 🔧 工具调用 ────────────────────────────────────┐
│  工具: get_weather                                 │
│  参数:                                             │
│  ✓ city: "Beijing"                                 │
│  ✓ unit: "celsius"                                 │
│                                                    │
│  {"city": "Beijing","unit": "celsius"}█             │
└────────────────────────────────────────────────────┘
```

#### 逐类型参数揭示

```typescript
/**
 * 根据参数类型显示不同的颜色标签
 */
private renderParamWithType(key: string, value: unknown): string {
  let typeTag: string;
  switch (typeof value) {
    case 'string':
      typeTag = '{blue-fg}(string){/blue-fg}';
      break;
    case 'number':
      typeTag = '{cyan-fg}(number){/cyan-fg}';
      break;
    case 'boolean':
      typeTag = '{green-fg}(boolean){/green-fg}';
      break;
    case 'object':
      if (value === null) {
        typeTag = '{gray-fg}(null){/gray-fg}';
      } else if (Array.isArray(value)) {
        typeTag = '{magenta-fg}(array:{/magenta-fg}{yellow-fg}' +
                  value.length + '{/yellow-fg}{magenta-fg}){/magenta-fg}';
      } else {
        const keys = Object.keys(value as Record<string, unknown>);
        typeTag = '{yellow-fg}(object:{/yellow-fg}{cyan-fg}' +
                  keys.length + '{/cyan-fg}{yellow-fg} keys){/yellow-fg}';
      }
      break;
    default:
      typeTag = '{gray-fg}({/gray-fg}{white-fg}' + typeof value + '{/white-fg}{gray-fg}){/gray-fg}';
  }

  const displayValue = typeof value === 'string'
    ? `"${value}"`
    : JSON.stringify(value);

  return `  {white-fg}"${key}"{/white-fg}: ${typeTag} {gray-fg}${displayValue}{/gray-fg}`;
}
```

#### 内联校验

```typescript
interface ParamValidation {
  key: string;
  required: boolean;
  present: boolean;
  valid: boolean;
  message: string;
}

/**
 * 校验流式构建中的参数是否符合 schema
 */
private validateStreamingParams(
  parsed: Record<string, unknown>,
  schema: { properties: Record<string, any>; required?: string[] }
): ParamValidation[] {
  return Object.entries(schema.properties || {}).map(([key, def]: [string, any]) => {
    const present = key in parsed;

    // 类型校验
    let typeValid = true;
    if (present && def.type) {
      const value = parsed[key];
      switch (def.type) {
        case 'string':
          typeValid = typeof value === 'string';
          break;
        case 'number':
          typeValid = typeof value === 'number';
          break;
        case 'boolean':
          typeValid = typeof value === 'boolean';
          break;
        case 'array':
          typeValid = Array.isArray(value);
          break;
        case 'object':
          typeValid = typeof value === 'object' && value !== null && !Array.isArray(value);
          break;
      }
    }

    const isRequired = schema.required?.includes(key) ?? false;

    let message: string;
    if (present && typeValid) {
      message = '{green-fg}✓ 校验通过{/green-fg}';
    } else if (present && !typeValid) {
      message = `{red-fg}✗ 类型错误: 期望 ${def.type}{/red-fg}`;
    } else if (isRequired) {
      message = '{yellow-fg}⏳ 等待中 (必填){/yellow-fg}';
    } else {
      message = '{gray-fg}○ 可选参数{/gray-fg}';
    }

    return { key, required: isRequired, present, valid: typeValid, message };
  });
}
```

#### 动画参数构建

```typescript
/**
 * 渲染参数构建动画 — 新出现的参数使用高亮闪烁效果
 */
private animateParamBuilding(
  parsed: Record<string, unknown>,
  prevParsed: Record<string, unknown> | null
): string {
  if (!prevParsed) {
    return Object.entries(parsed).map(([key, value]) =>
      `  {yellow-fg}{bold}✦ ${key}: ${JSON.stringify(value)}{/bold}{/yellow-fg} {blink}{red-fg}NEW{/blink}{/red-fg}`
    ).join('\n');
  }

  const newKeys = Object.keys(parsed).filter(k => !(k in prevParsed));
  const lines: string[] = [];

  // 先渲染所有已稳定的参数
  for (const [key, value] of Object.entries(parsed)) {
    if (newKeys.includes(key)) {
      // 新参数使用闪烁高亮
      lines.push(`  {bold}{yellow-fg}✦ ${key}: ${JSON.stringify(value)} {blink}NEW{/blink}{/bold}{/yellow-fg}`);
    } else {
      // 已稳定的参数使用绿色
      lines.push(`  {green-fg}✓{/green-fg} {white-fg}${key}: ${JSON.stringify(value)}{/white-fg}`);
    }
  }

  return lines.join('\n');
}
```

## 7.7 工具调用的完整生命周期

| 阶段 | 状态 | UI 显示 | 用户所见 |
|------|------|---------|---------|
| 1 | `thinking` | Spinner | AI 正在思考是否需要工具 |
| 2 | `text` (可选) | 打字机文本 | "我来查一下数据..." |
| 3 | `tool_call` | 紫色卡片 | 看到工具名称和参数 |
| 4 | `tool_result` | 青色卡片 | 看到工具返回的数据 |
| 5 | `text` | 打字机文本 | 基于数据的最终回答 |
| 6 | `done` | 就绪状态 | 消息完成，可以继续对话 |

## 7.8 实际工具执行引擎

在 Mock 中我们模拟了工具调用，真实场景中需要一个工具执行引擎：

```typescript
class ToolEngine {
  private tools: Map<string, (args: any) => Promise<any>> = new Map();

  constructor() {
    this.register('get_weather', this.getWeather.bind(this));
    this.register('calculate', this.calculate.bind(this));
    this.register('search_web', this.searchWeb.bind(this));
  }

  register(name: string, handler: (args: any) => Promise<any>): void {
    this.tools.set(name, handler);
  }

  async execute(name: string, args: any): Promise<string> {
    const handler = this.tools.get(name);
    if (!handler) {
      throw new Error(`未知工具: ${name}`);
    }
    const result = await handler(args);
    return JSON.stringify(result, null, 2);
  }

  private async getWeather(args: { city: string; unit?: string }): Promise<any> {
    // 真实场景中调用天气 API
    // const response = await fetch(`https://api.weather.com/v1/${args.city}`);
    // return response.json();

    // Mock 数据
    await delay(800);
    return {
      city: args.city,
      temperature: 25,
      condition: '☀️ 晴',
      humidity: 45,
    };
  }

  private async calculate(args: { expression: string }): Promise<any> {
    const result = Function(`"use strict"; return (${args.expression})`)();
    return { expression: args.expression, result };
  }

  private async searchWeb(args: { query: string }): Promise<any> {
    // 需要接入搜索 API
    await delay(1200);
    return { results: [`关于 "${args.query}" 的搜索结果...`] };
  }
}
```

## 7.9 流式工具参数（Streaming Tool Parameters）

部分 LLM API（如 Anthropic）支持工具参数的流式传输，UI 需要实时展示参数的构建过程：

```typescript
/** 流式工具参数构建 */
class StreamingToolParamBuilder {
  private partialJson = '';
  private currentArgs: Record<string, unknown> = {};

  /** 处理流入的 JSON delta */
  processDelta(delta: string): { parsed: Record<string, unknown>; isComplete: boolean } {
    this.partialJson += delta;

    try {
      // 尝试解析部分 JSON
      this.currentArgs = JSON.parse(this.partialJson);
      return { parsed: { ...this.currentArgs }, isComplete: true };
    } catch {
      // JSON 还不完整，返回当前已解析的部分
      return { parsed: { ...this.currentArgs }, isComplete: false };
    }
  }

  /** 获取当前已解析的参数 */
  getCurrentArgs(): Record<string, unknown> {
    return { ...this.currentArgs };
  }

  /** 重置 */
  reset(): void {
    this.partialJson = '';
    this.currentArgs = {};
  }
}

// 在 UI 中实时更新参数显示
const paramBuilder = new StreamingToolParamBuilder();

// 在流式循环中:
for await (const event of stream) {
  if (event.type === 'tool_call_delta') {
    const { parsed, isComplete } = paramBuilder.processDelta(event.delta);
    // 实时更新工具卡片上的参数
    updateToolCallCard(parsed);
  }
  if (event.type === 'tool_call') {
    // 工具调用完整参数到达
    showToolCall(event.name, event.args);
    paramBuilder.reset();
  }
}
```

## 7.10 工具调用确认（Tool Confirmation）

敏感工具（如文件删除、数据库写入）需要在执行前获得用户确认：

```typescript
/**
 * 工具调用确认对话框 —— 模态弹窗
 */
private async confirmToolCall(
  toolName: string,
  args: Record<string, unknown>
): Promise<boolean> {
  return new Promise((resolve) => {
    const argsStr = Object.entries(args)
      .map(([k, v]) => `  ${k}: ${JSON.stringify(v)}`)
      .join('\n');

    const confirmBox = blessed.box({
      parent: this.screen, // 使用 screen 作为父级（覆盖整个屏幕）
      top: 'center',
      left: 'center',
      width: '60%',
      height: 8,
      content:
        `{yellow-fg}┌─ ⚠️ 确认工具调用 ───────────────────────────┐{/yellow-fg}\n` +
        `{yellow-fg}│{/yellow-fg}  工具: {bold}${toolName}{/bold}\n` +
        `{yellow-fg}│{/yellow-fg}\n${argsStr}\n` +
        `{yellow-fg}│{/yellow-fg}\n` +
        `{yellow-fg}│{/yellow-fg}  {bold}[Y] 确认执行  [N] 取消{/bold}\n` +
        `{yellow-fg}└────────────────────────────────────────────────┘{/yellow-fg}`,
      tags: true,
      style: { fg: 'white', bg: '#1a1a1a' },
      border: { type: 'line', fg: 'yellow' },
      shadow: true,
    });

    const handleKey = (ch: any, key: any) => {
      this.screen.key(['y', 'Y', 'n', 'N', 'escape'], () => {});
      this.screen.remove(confirmBox);
      this.screen.render();
      resolve(key.name === 'y' || key.name === 'Y');
    };

    this.screen.key(['y', 'Y', 'n', 'N', 'escape'], handleKey);
    this.screen.render();
  });
}

// 使用示例
if (await this.confirmToolCall('delete_file', { path: '/tmp/data.txt' })) {
  // 用户确认后执行
  await toolEngine.execute('delete_file', { path: '/tmp/data.txt' });
} else {
  this.addSystemMessage('{yellow-fg}⏹️ 工具调用已取消{/yellow-fg}');
}
```

## 7.11 工具执行引擎增强

真实场景中的工具引擎需要更完善的错误处理、缓存和并发控制：

```typescript
interface ToolExecutionOptions {
  timeout?: number;        // 超时（毫秒）
  cache?: boolean;         // 是否缓存结果
  retryCount?: number;     // 失败重试次数
  onProgress?: (progress: number) => void;
}

class EnhancedToolEngine extends ToolEngine {
  private cache = new Map<string, { result: string; timestamp: number }>();
  private readonly CACHE_TTL = 60000; // 1 分钟缓存

  constructor() {
    super();
    // 注册增强工具处理器
    this.register('search_web', this.enhancedSearch.bind(this));
  }

  async executeWithOptions(
    name: string,
    args: Record<string, unknown>,
    options: ToolExecutionOptions = {}
  ): Promise<string> {
    // 1. 缓存检查
    const cacheKey = `${name}:${JSON.stringify(args)}`;
    if (options.cache !== false) {
      const cached = this.cache.get(cacheKey);
      if (cached && Date.now() - cached.timestamp < this.CACHE_TTL) {
        return cached.result;
      }
    }

    // 2. 执行（带超时和重试）
    let lastError: Error | null = null;
    const maxRetries = options.retryCount ?? 0;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        const result = await this.executeWithTimeout(name, args, options.timeout ?? 30000);

        // 3. 缓存结果
        if (options.cache !== false) {
          this.cache.set(cacheKey, { result, timestamp: Date.now() });
        }

        return result;
      } catch (err) {
        lastError = err instanceof Error ? err : new Error(String(err));
        if (attempt < maxRetries) {
          await new Promise(r => setTimeout(r, 1000 * (attempt + 1))); // 递增延迟
        }
      }
    }

    throw lastError || new Error(`工具 ${name} 执行失败`);
  }

  private async executeWithTimeout(
    name: string,
    args: Record<string, unknown>,
    timeoutMs: number
  ): Promise<string> {
    return Promise.race([
      this.execute(name, args),
      new Promise<string>((_, reject) =>
        setTimeout(() => reject(new Error(`工具 ${name} 超时 (${timeoutMs}ms)`)), timeoutMs)
      ),
    ]);
  }

  private async enhancedSearch(args: { query: string }): Promise<any> {
    // 真实搜索实现
    await delay(800);
    return {
      results: [
        { title: '结果1', url: 'https://example.com/1', snippet: '...' },
        { title: '结果2', url: 'https://example.com/2', snippet: '...' },
      ],
      totalResults: 2,
    };
  }
}
```

## 7.12 工具执行进度条集成

为工具执行过程提供可视化的进度追踪和 ETA 预估，让用户了解工具执行的各个阶段。

### 7.12.1 进度追踪器

```typescript
interface ProgressStage {
  label: string;       // 阶段名称
  weight: number;      // 权重 (0-100)
}

interface ToolExecutionProgress {
  toolName: string;
  stages: ProgressStage[];
  currentStage: number;
  startTime: number;
  estimatedTotal: number;  // 预估总耗时 (ms)
}

/**
 * 进度追踪器 — 基于历史执行数据估算 ETA
 */
class ProgressTracker {
  private history: Map<string, number[]> = new Map();

  /** 根据历史记录估算某工具的执行时间 */
  estimateDuration(toolName: string): number {
    const times = this.history.get(toolName);
    if (!times || times.length === 0) return 2000; // 默认 2s
    // 使用移动平均
    const avg = times.reduce((a, b) => a + b, 0) / times.length;
    return Math.round(avg);
  }

  /** 记录一次执行耗时 */
  recordExecution(toolName: string, duration: number): void {
    if (!this.history.has(toolName)) {
      this.history.set(toolName, []);
    }
    const list = this.history.get(toolName)!;
    list.push(duration);
    // 只保留最近 10 次
    while (list.length > 10) list.shift();
  }

  /** 渲染进度条 */
  renderProgressBar(progress: ToolExecutionProgress): string {
    const elapsed = Date.now() - progress.startTime;
    const eta = Math.max(0, progress.estimatedTotal - elapsed);
    const pct = Math.min(100, (elapsed / progress.estimatedTotal) * 100);
    const barLen = 20;
    const filled = Math.round((pct / 100) * barLen);
    const empty = barLen - filled;

    const bar = '{green-fg}' + '█'.repeat(filled) + '{/green-fg}' +
                '{gray-fg}' + '░'.repeat(empty) + '{/gray-fg}';

    const stage = progress.stages[progress.currentStage] ?? { label: '完成', weight: 100 };
    const weightPct = progress.stages
      .slice(0, progress.currentStage)
      .reduce((sum, s) => sum + s.weight, 0);

    return [
      `  {bold}${bar} ${pct.toFixed(0)}%{/bold}`,
      `  阶段 {cyan-fg}[${progress.currentStage + 1}/${progress.stages.length}]{/cyan-fg}: ` +
        `{white-fg}${stage.label}{/white-fg}`,
      `  已用: {yellow-fg}${(elapsed / 1000).toFixed(1)}s{/yellow-fg}` +
        `  ETA: {yellow-fg}${(eta / 1000).toFixed(1)}s{/yellow-fg}`,
    ].join('\n');
  }
}
```

### 7.12.2 多阶段进度定义

```typescript
// 不同工具的多阶段进度定义
const PROGRESS_STAGES: Record<string, ProgressStage[]> = {
  get_weather: [
    { label: '正在连接天气 API...', weight: 20 },
    { label: '正在获取数据...',     weight: 40 },
    { label: '正在解析响应...',     weight: 25 },
    { label: '正在格式化结果...',   weight: 15 },
  ],
  search_web: [
    { label: '正在构建搜索请求...', weight: 15 },
    { label: '正在请求搜索引擎...', weight: 35 },
    { label: '正在解析搜索结果...', weight: 30 },
    { label: '正在提取摘要...',     weight: 20 },
  ],
};
```

### 7.12.3 进度条 UI 渲染

```
┌─ 🔧 工具调用 ────────────────────────────────────┐
│  工具: search_web                                  │
│  参数: {"query": "latest AI news"}                 │
│                                                    │
│  ████████████████░░░░ 75%                          │
│  阶段 [3/4]: 正在解析搜索结果...                    │
│  已用: 1.8s  ETA: 0.6s                             │
│                                                    │
│  ⏳ 执行中... 请稍候                                │
└────────────────────────────────────────────────────┘
```

### 7.12.4 与流式文本的集成

```typescript
/**
 * 在工具执行期间同步输出流式文本
 */
private async executeToolWithStreamingText(
  name: string,
  args: Record<string, unknown>
): Promise<string> {
  // 1. 显示进度条和初始提示
  this.showToolCall(name, args);
  const progress = this.initProgress(name);
  this.renderProgress(progress);

  // 2. 并行执行：工具调用 + 流式提示文本
  const [result] = await Promise.all([
    this.toolEngine.execute(name, args),
    this.streamToolHintText(name),   // 工具执行期间输出提示文字
  ]);

  // 3. 将进度更新为完成
  this.finalizeProgress(progress);
  return result;
}

/**
 * 工具执行期间输出流式提示文本
 */
private async streamToolHintText(toolName: string): Promise<void> {
  const hints: Record<string, string> = {
    get_weather: '正在查询气象数据...',
    search_web: '正在搜索相关网页...',
    calculate: '正在计算结果...',
  };

  const text = hints[toolName] ?? '正在执行...';
  for (const ch of text) {
    this.appendStreamingText(ch);
    await delay(30);
  }
}
```

## 7.13 工具结果缓存

缓存机制可以显著提升反复调用相同工具场景下的响应速度。

### 7.13.1 缓存键设计

```typescript
class ToolCache {
  /**
   * 生成标准化的缓存键
   * 规则: 工具名 + 按 key 排序后的序列化参数
   */
  buildKey(name: string, args: Record<string, unknown>): string {
    const sortedArgs = Object.keys(args)
      .sort()
      .reduce((acc, key) => {
        acc[key] = args[key];
        return acc;
      }, {} as Record<string, unknown>);

    return `${name}:${JSON.stringify(sortedArgs)}`;
  }

  // 示例
  // buildKey('get_weather', { city: 'Beijing', unit: 'celsius' })
  // => "get_weather:{\"city\":\"Beijing\",\"unit\":\"celsius\"}"
  //
  // 注意: 参数顺序不影响缓存键
  // buildKey('get_weather', { unit: 'celsius', city: 'Beijing' })
  // => 同样的键
}
```

### 7.13.2 LRU 缓存实现

```typescript
interface CacheEntry {
  result: string;
  createdAt: number;
  ttl: number;           // 生存时间 (ms)
  accessCount: number;   // 访问次数
}

/**
 * LRU (Least Recently Used) 缓存
 */
class LRUToolCache {
  private cache: Map<string, CacheEntry> = new Map();
  private readonly maxSize: number;

  constructor(maxSize = 100) {
    this.maxSize = maxSize;
  }

  /** 获取缓存 */
  get(key: string): string | null {
    const entry = this.cache.get(key);
    if (!entry) return null;

    // 检查是否过期
    if (Date.now() - entry.createdAt > entry.ttl) {
      this.cache.delete(key);
      return null;
    }

    // LRU: 删除再插入（将该条目移到 Map 末尾 = 最近使用）
    entry.accessCount++;
    this.cache.delete(key);
    this.cache.set(key, entry);

    return entry.result;
  }

  /** 写入缓存 */
  set(key: string, result: string, ttl: number = 60000): void {
    // 淘汰最久未使用的条目
    while (this.cache.size >= this.maxSize) {
      const oldestKey = this.cache.keys().next().value;
      if (oldestKey === undefined) break;
      this.cache.delete(oldestKey);
    }

    this.cache.set(key, {
      result,
      createdAt: Date.now(),
      ttl,
      accessCount: 0,
    });
  }

  /** 缓存统计 */
  stats(): { size: number; maxSize: number; hitRate: string } {
    let hits = 0;
    let total = 0;
    for (const entry of this.cache.values()) {
      hits += entry.accessCount;
      total++;
    }
    return {
      size: this.cache.size,
      maxSize: this.maxSize,
      hitRate: total > 0 ? `${((hits / total) * 100).toFixed(1)}%` : '0%',
    };
  }

  /** 清空过期条目 */
  evictExpired(): number {
    let count = 0;
    const now = Date.now();
    for (const [key, entry] of this.cache.entries()) {
      if (now - entry.createdAt > entry.ttl) {
        this.cache.delete(key);
        count++;
      }
    }
    return count;
  }
}
```

### 7.13.3 缓存命中/未命中视觉指示

```typescript
/**
 * 渲染缓存状态指示
 */
private renderCachedResult(result: string, cacheHit: boolean, cacheAge?: number): string {
  const borderColor = cacheHit ? '{green-fg}' : '{cyan-fg}';
  const label = cacheHit
    ? `📦 缓存结果 {gray-fg}(${cacheAge ?? 0}ms 前){/gray-fg}`
    : '✅ 工具结果（新获取）';

  const refreshHint = cacheHit
    ? '\n  {gray-fg}按 R 刷新缓存{/gray-fg}'
    : '';

  return (
    `${borderColor}┌─ ${label} ─────────────────────────────┐${borderColor}\n` +
    `${borderColor}│${borderColor}  ${result.replace(/\n/g, '\n' + borderColor + '│' + borderColor + '  ')}` +
    `${refreshHint}\n` +
    `${borderColor}└────────────────────────────────────────────┘${borderColor}`
  );
}
```

缓存命中的卡片使用绿色边框，未命中使用青色边框：

```
缓存命中（绿色边框）:
┌─ 📦 缓存结果 (320ms 前) ─────────────────────────┐
│  {"temperature": 25, "condition": "☀️ 晴"}         │
│  按 R 刷新缓存                                      │
└────────────────────────────────────────────────────┘

缓存未命中（青色边框）:
┌─ ✅ 工具结果（新获取）───────────────────────────┐
│  {"temperature": 25, "condition": "☀️ 晴"}         │
└────────────────────────────────────────────────────┘
```

### 7.13.4 持久化选项

```typescript
import * as fs from 'fs/promises';
import * as path from 'path';

/**
 * 支持磁盘持久化的缓存
 */
class PersistentToolCache extends LRUToolCache {
  private readonly persistPath: string;
  private dirty = false;

  constructor(maxSize = 100, persistPath?: string) {
    super(maxSize);
    this.persistPath = persistPath ?? path.join(process.cwd(), '.tool-cache.json');
  }

  /** 从磁盘加载缓存 */
  async loadFromDisk(): Promise<void> {
    try {
      const data = await fs.readFile(this.persistPath, 'utf-8');
      const entries = JSON.parse(data);
      for (const [key, entry] of Object.entries(entries)) {
        const e = entry as CacheEntry;
        // 跳过已过期的条目
        if (Date.now() - e.createdAt <= e.ttl) {
          (this as any).cache.set(key, e);
        }
      }
    } catch {
      // 缓存文件不存在或格式错误，忽略
    }
  }

  /** 保存到磁盘 */
  async saveToDisk(): Promise<void> {
    if (!this.dirty) return;
    const entries: Record<string, CacheEntry> = {};
    for (const [key, entry] of (this as any).cache.entries()) {
      entries[key] = entry;
    }
    await fs.writeFile(this.persistPath, JSON.stringify(entries, null, 2), 'utf-8');
    this.dirty = false;
  }
}
```

## 7.14 工具调用历史

记录所有工具调用供用户回顾和重新执行。

### 7.14.1 历史数据结构

```typescript
interface ToolCallRecord {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result: string;
  status: 'completed' | 'error';
  duration: number;           // 执行耗时 (ms)
  timestamp: number;          // 执行时间戳
  isCached: boolean;          // 是否来自缓存
}

/**
 * 工具调用历史管理器
 */
class ToolCallHistory {
  private records: ToolCallRecord[] = [];
  private readonly maxRecords = 500;
  private nextId = 1;

  /** 添加一条记录 */
  add(record: Omit<ToolCallRecord, 'id'>): ToolCallRecord {
    const full: ToolCallRecord = { id: `tool-${this.nextId++}`, ...record };
    this.records.push(full);
    if (this.records.length > this.maxRecords) {
      this.records.shift();
    }
    return full;
  }

  /** 获取所有记录 */
  getAll(): ToolCallRecord[] {
    return [...this.records];
  }

  /** 按条件筛选 */
  filter(options: {
    name?: string;
    status?: 'completed' | 'error';
    since?: number;
    until?: number;
    search?: string;
  }): ToolCallRecord[] {
    return this.records.filter(r => {
      if (options.name && r.name !== options.name) return false;
      if (options.status && r.status !== options.status) return false;
      if (options.since && r.timestamp < options.since) return false;
      if (options.until && r.timestamp > options.until) return false;
      if (options.search) {
        const searchLower = options.search.toLowerCase();
        const argsMatch = JSON.stringify(r.args).toLowerCase().includes(searchLower);
        const resultMatch = r.result.toLowerCase().includes(searchLower);
        if (!argsMatch && !resultMatch) return false;
      }
      return true;
    });
  }
}
```

### 7.14.2 历史面板 UI

```typescript
/**
 * 紧凑的历史面板渲染
 */
private renderHistoryPanel(records: ToolCallRecord[]): string {
  if (records.length === 0) {
    return '{gray-fg}┌─ 工具调用历史 (空) ──────────────────────┐{/gray-fg}\n' +
           '{gray-fg}│{/gray-fg}  暂无工具调用记录\n' +
           '{gray-fg}└────────────────────────────────────────┘{/gray-fg}';
  }

  const lines: string[] = [
    `{cyan-fg}┌─ 工具调用历史 (${records.length}条) ───────────────┐{/cyan-fg}`,
  ];

  records.slice(-10).reverse().forEach((r, i) => {
    const icon = r.status === 'completed' ? '{green-fg}✓{/green-fg}' : '{red-fg}✗{/red-fg}';
    const time = new Date(r.timestamp).toLocaleTimeString();
    const name = r.name.length > 12 ? r.name.slice(0, 10) + '..' : r.name;
    const duration = r.duration < 1000
      ? `${r.duration}ms`
      : `${(r.duration / 1000).toFixed(1)}s`;

    lines.push(
      `{cyan-fg}│{/cyan-fg}  ${icon} {bold}#${r.id}{/bold} ` +
      `{yellow-fg}${name}{/yellow-fg} ` +
      `{gray-fg}${time} (${duration}){/gray-fg}`
    );

    if (i < Math.min(records.length - 1, 9)) {
      lines.push(`{cyan-fg}│{/cyan-fg}  {gray-fg}│{/gray-fg}`);
    }
  });

  lines.push(`{cyan-fg}└────────────────────────────────────────┘{/cyan-fg}`);
  return lines.join('\n');
}
```

历史面板效果：

```
┌─ 工具调用历史 (12条) ──────────────────────────┐
│  ✓ #3 get_weather  14:32:15 (0.8s)              │
│  │                                                │
│  ✗ #2 search_web  14:31:58 (2.1s)               │
│  │                                                │
│  ✓ #1 get_weather  14:31:45 (0.6s)               │
│  ...                                              │
│  [↑↓] 浏览  [F] 筛选  [R] 重新执行               │
└───────────────────────────────────────────────────┘
```

### 7.14.3 筛选功能

```typescript
/**
 * 历史筛选对话框
 */
private showHistoryFilter(): Promise<{
  name?: string;
  status?: 'completed' | 'error';
}> {
  return new Promise((resolve) => {
    const filterBox = blessed.box({
      parent: this.screen,
      top: 'center', left: 'center',
      width: '50%', height: 7,
      content:
        `{cyan-fg}┌─ 筛选工具历史 ──────────────────────┐{/cyan-fg}\n` +
        `{cyan-fg}│{/cyan-fg}  [1] 仅显示 get_weather           \n` +
        `{cyan-fg}│{/cyan-fg}  [2] 仅显示 search_web            \n` +
        `{cyan-fg}│{/cyan-fg}  [3] 仅显示成功 (✓)              \n` +
        `{cyan-fg}│{/cyan-fg}  [4] 仅显示失败 (✗)              \n` +
        `{cyan-fg}│{/cyan-fg}  [5] 清除筛选                     \n` +
        `{cyan-fg}│{/cyan-fg}  [ESC] 关闭                       \n` +
        `{cyan-fg}└────────────────────────────────────────────┘{/cyan-fg}`,
      tags: true,
      style: { fg: 'white', bg: '#1a1a1a' },
    });

    this.screen.key(['1', '2', '3', '4', '5', 'escape'], (ch, key) => {
      this.screen.remove(filterBox);
      this.screen.render();

      switch (key.name) {
        case '1': resolve({ name: 'get_weather' }); break;
        case '2': resolve({ name: 'search_web' }); break;
        case '3': resolve({ status: 'completed' }); break;
        case '4': resolve({ status: 'error' }); break;
        case '5': resolve({}); break;
        case 'escape': resolve({}); break;
      }
    });
  });
}
```

### 7.14.4 重新执行历史调用

```typescript
/**
 * 从历史记录中重新执行工具调用
 */
private async reExecuteFromHistory(
  records: ToolCallRecord[],
  index: number
): Promise<string> {
  const record = records[index];
  if (!record) {
    throw new Error(`不存在索引 ${index} 的历史记录`);
  }

  // 显示重新执行标识
  this.addSystemMessage(
    `{cyan-fg}⟳ 重新执行: {/cyan-fg}{bold}${record.name}{/bold}` +
    `{gray-fg} (原始: ${new Date(record.timestamp).toLocaleTimeString()}){/gray-fg}`
  );

  // 执行工具
  this.showToolCall(record.name, record.args);
  try {
    const result = await this.toolEngine.execute(record.name, record.args);
    this.showToolResult(record.name, result);

    // 更新历史
    this.toolHistory.add({
      name: record.name,
      args: record.args,
      result,
      status: 'completed',
      duration: 0, // 未追踪
      timestamp: Date.now(),
      isCached: false,
    });

    return result;
  } catch (err) {
    this.showToolError(record.name, record.args, String(err));
    throw err;
  }
}
```

## 7.15 真实工具示例

以下是几种常见真实场景的工具集成实现。

### 7.15.1 搜索 Web

```typescript
/**
 * 搜索工具 — 返回带摘要的结果列表
 */
async searchWebTool(args: { query: string; count?: number }): Promise<string> {
  const count = args.count ?? 5;

  // 真实场景中调用搜索 API
  // const response = await fetch(
  //   `https://api.search.service/v1/search?q=${encodeURIComponent(args.query)}&count=${count}`,
  //   { headers: { Authorization: `Bearer ${process.env.SEARCH_API_KEY}` } }
  // );
  // const data = await response.json();

  // Mock 实现
  await delay(1000 + Math.random() * 500);
  const results = Array.from({ length: count }, (_, i) => ({
    title: `结果 ${i + 1}: ${args.query} 相关页面`,
    url: `https://example.com/result-${i + 1}`,
    snippet: `这是关于「${args.query}」的第 ${i + 1} 条搜索结果摘要...`,
  }));

  return JSON.stringify({ results, total: count }, null, 2);
}
```

搜索结果展示卡片：

```
┌─ ✅ 搜索结果 ──────────────────────────────────┐
│  查询: "latest AI news"                          │
│                                                   │
│  1. AI 突破: 新模型发布                          │
│     https://example.com/ai-news-1                 │
│     🔍 最新 AI 模型在多项基准测试中...            │
│                                                   │
│  2. 机器学习框架更新                             │
│     https://example.com/ai-news-2                 │
│     🔍 新版框架支持更高效的训练...                │
│                                                   │
│  共 5 条结果 (0.8s)                               │
└───────────────────────────────────────────────────┘
```

### 7.15.2 图片生成

```typescript
/**
 * 图片生成工具 — 显示逐步进度
 */
async generateImageTool(args: {
  prompt: string;
  size?: string;
  style?: string;
}): Promise<string> {
  const steps = [
    { label: '正在解析提示词...', duration: 500 },
    { label: '正在构建模型输入...', duration: 800 },
    { label: '正在生成图像...', duration: 3000 },
    { label: '正在后处理...', duration: 600 },
  ];

  let totalProgress = 0;
  for (const step of steps) {
    // 更新进度
    totalProgress += step.duration;
    this.updateProgress('generate_image', totalProgress, step.label);
    await delay(step.duration);
  }

  // Mock 返回图片 URL 或 base64
  return JSON.stringify({
    url: `https://example.com/generated/${Date.now()}.png`,
    prompt: args.prompt,
    size: args.size ?? '1024x1024',
    seed: Math.floor(Math.random() * 1000000),
  }, null, 2);
}
```

图片生成进度展示：

```
┌─ 🎨 图片生成 ──────────────────────────────────┐
│  提示词: "a cat wearing a hat"                    │
│                                                   │
│  ████████████████░░░░ 78%                         │
│  阶段 [3/4]: 正在生成图像...                      │
│                                                   │
│  ┌─────────────────────────────────┐              │
│  │                                 │              │
│  │        🖼️ 生成中...              │              │
│  │                                 │              │
│  └─────────────────────────────────┘              │
│                                                   │
│  ⏳ 预计剩余 2.5s                                 │
└───────────────────────────────────────────────────┘
```

### 7.15.3 文件操作

```typescript
import * as fs from 'fs/promises';
import * as path from 'path';

/**
 * 文件读取工具 — 带路径验证和安全检查
 */
async readFileTool(args: { path: string; encoding?: string }): Promise<string> {
  // 安全验证：防止目录遍历
  const resolvedPath = path.resolve(args.path);
  const allowedBase = path.resolve(process.cwd(), 'workspace');
  if (!resolvedPath.startsWith(allowedBase)) {
    throw new Error(`⛔ 路径不在允许的工作目录内: ${resolvedPath}`);
  }

  // 检查文件是否存在
  try {
    await fs.access(resolvedPath);
  } catch {
    throw new Error(`❌ 文件不存在: ${args.path}`);
  }

  const encoding = (args.encoding as BufferEncoding) ?? 'utf-8';
  const content = await fs.readFile(resolvedPath, encoding);

  return JSON.stringify({
    path: args.path,
    size: content.length,
    encoding,
    content: content.slice(0, 2000), // 限制返回长度
    truncated: content.length > 2000,
  }, null, 2);
}

/**
 * 文件写入工具 — 需要用户确认
 */
async writeFileTool(args: { path: string; content: string }): Promise<string> {
  // 安全检查
  const resolvedPath = path.resolve(args.path);
  const allowedBase = path.resolve(process.cwd(), 'workspace');
  if (!resolvedPath.startsWith(allowedBase)) {
    throw new Error(`⛔ 路径不在允许的工作目录内`);
  }

  // 确认覆盖
  const confirmed = await this.showToolConfirm('write_file', args);
  if (!confirmed) {
    return JSON.stringify({ status: 'cancelled', message: '用户取消了写入' });
  }

  // 确保目录存在
  await fs.mkdir(path.dirname(resolvedPath), { recursive: true });
  await fs.writeFile(resolvedPath, args.content, 'utf-8');

  return JSON.stringify({
    status: 'success',
    path: args.path,
    size: args.content.length,
  }, null, 2);
}
```

### 7.15.4 数据库查询

```typescript
/**
 * 数据库查询工具 — 返回表格格式
 */
async queryDatabaseTool(args: {
  query: string;
  params?: unknown[];
  limit?: number;
}): Promise<string> {
  // 真实场景中使用数据库驱动
  // import sqlite3 from 'sqlite3';
  // const db = new sqlite3.Database('./data.db');
  // const rows = await db.all(args.query, ...(args.params ?? []));

  // Mock: 模拟返回表格数据
  await delay(500 + Math.random() * 1000);
  const mockData = {
    columns: ['id', 'name', 'email', 'created_at'],
    rows: [
      [1, '张三', 'zhangsan@example.com', '2024-01-15'],
      [2, '李四', 'lisi@example.com', '2024-02-20'],
      [3, '王五', 'wangwu@example.com', '2024-03-10'],
    ],
    rowCount: 3,
    executionTime: `${(Math.random() * 100 + 50).toFixed(0)}ms`,
  };

  return JSON.stringify(mockData, null, 2);
}
```

数据库结果表格渲染：

```
┌─ 🗄️ 数据库查询结果 ──────────────────────────┐
│  SQL: SELECT * FROM users LIMIT 3               │
│  执行时间: 62ms                                  │
│                                                   │
│  ┌────┬────────┬────────────────────┬────────────┐│
│  │ id │ name   │ email              │ created_at ││
│  ├────┼────────┼────────────────────┼────────────┤│
│  │ 1  │ 张三   │ zhangsan@...      │ 2024-01-15 ││
│  │ 2  │ 李四   │ lisi@example...   │ 2024-02-20 ││
│  │ 3  │ 王五   │ wangwu@examp...   │ 2024-03-10 ││
│  └────┴────────┴────────────────────┴────────────┘│
│  共 3 行                                           │
└───────────────────────────────────────────────────┘
```

### 7.15.5 代码执行沙箱

```typescript
/**
 * 代码执行工具 — 捕获 stdout/stderr
 */
async executeCodeTool(args: {
  language: string;
  code: string;
  timeout?: number;
}): Promise<string> {
  const supportedLanguages = ['javascript', 'python', 'bash'];
  if (!supportedLanguages.includes(args.language)) {
    throw new Error(`不支持的语言: ${args.language}，支持: ${supportedLanguages.join(', ')}`);
  }

  const timeout = args.timeout ?? 10000;

  // 真实场景中在沙箱容器中执行
  // const result = await sandbox.run(args.code, { language: args.language, timeout });

  // Mock 实现
  await delay(Math.random() * 1000 + 500);
  let stdout: string;
  let stderr = '';

  switch (args.language) {
    case 'javascript':
      try {
        const fn = new Function(`"use strict"; ${args.code}`);
        const result = fn();
        stdout = String(result);
      } catch (err) {
        stderr = String(err);
      }
      break;
    case 'python':
      stdout = `Python 执行结果 (模拟): ${args.code.slice(0, 30)}...`;
      break;
    case 'bash':
      stdout = `$ ${args.code}\n执行完成，退出码: 0`;
      break;
    default:
      stdout = '';
  }

  return JSON.stringify({
    stdout,
    stderr,
    exitCode: stderr ? 1 : 0,
    executionTime: `${(Math.random() * 500 + 100).toFixed(0)}ms`,
  }, null, 2);
}
```

代码执行结果展示：

```
┌─ 💻 代码执行 ──────────────────────────────────┐
│  语言: JavaScript                                 │
│  代码:                                             │
│  │ const arr = [1,2,3,4,5];                      │
│  │ return arr.map(x => x * 2);                   │
│                                                   │
│  ┌─ stdout ─────────────────────────────────────┐│
│  │ [2, 4, 6, 8, 10]                             ││
│  └──────────────────────────────────────────────┘│
│                                                   │
│  退出码: 0  执行时间: 2ms                         │
└───────────────────────────────────────────────────┘
```

---

**实践：** 修改 `llm-chat.ts`，添加一个新的工具调用场景（如翻译、搜索新闻），并实现对应的工具卡片渲染。

**下一步：** [第八章：流式响应与动画](08-streaming-animations.md)
