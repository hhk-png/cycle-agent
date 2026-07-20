# 第十四章 技能系统与可扩展性

> **本章衔接：** 第十三章学习了 TUI 应用测试。本章深入探讨如何为 TUI 应用构建可扩展的**技能系统**（Skills System）——让应用可以通过插件化的方式加载自定义命令和功能，实现可持续的功能演进。总结章节见第十五章。

## 14.1 什么是技能系统

技能系统（Skills System）是一种**插件化架构**，允许 TUI 应用通过"技能"（Skill）这一统一抽象单元来扩展功能。每个技能是一个独立的、自包含的功能模块，可以被动态注册、调用和管理。

### 技能 vs 插件 vs 命令

| 概念 | 范围 | 特征 | 示例 |
|------|------|------|------|
| **命令 (Command)** | 单个操作 | 简单、无状态 | `/help` 显示帮助 |
| **插件 (Plugin)** | 一组功能 | 复杂、可能有状态 | Markdown 渲染器插件 |
| **技能 (Skill)** | 中间层 | 自包含、可组合 | `/weather` 天气查询技能 |

技能系统位于命令和插件之间——比命令更结构化，比插件更轻量。

### 技能系统的核心价值

```
传统 TUI 应用:
  应用代码 ──→ 硬编码所有功能
  添加新功能: 修改核心代码 → 重新发布

带技能系统的 TUI 应用:
  应用核心 ──→ 技能注册表 ←── 技能 A
                           ←── 技能 B
                           ←── 技能 C (外部加载)
  添加新功能: 注册新技能 → 无需修改核心
```

| 价值 | 说明 |
|------|------|
| **可扩展性** | 无需修改核心代码即可添加新功能 |
| **模块化** | 每个技能独立开发、测试、维护 |
| **动态加载** | 技能可以在运行时加载或卸载 |
| **生态共享** | 技能可打包分享，形成生态 |
| **用户自定义** | 用户可以编写自己的技能 |
| **关注点分离** | 核心只负责调度，技能负责实现 |

### 在 LLM TUI 中技能系统的意义

在 AI 对话 TUI 中，技能系统尤其有价值：

```
传统 LLM TUI:
  用户输入 → LLM 处理 → 回复
  （所有功能依赖 LLM 的 tool calling）

带技能系统的 LLM TUI:
  用户输入
    ├── 以 "/" 开头 → 技能调度器 → 执行技能 → 返回结果
    └── 普通文本    → LLM 处理 → 回复
  
  技能可以:
  1. 直接调用本地 API（绕过 LLM，更快更可靠）
  2. 是固定提示词模板（快速启动常见任务）
  3. 是 UI 控制命令（切换主题、导出对话）
  4. 是自动化工作流（多步骤任务编排）
```

## 14.2 技能系统的架构设计

### 整体架构

```
┌─────────────────────────────────────────────────────┐
│                    TUI 应用                           │
│                                                       │
│  ┌─────────────┐  ┌─────────────────────────────┐   │
│  │  UI 层       │  │  技能系统                    │   │
│  │ (blessed)   │  │                              │   │
│  │  - 消息面板  │  │  ┌───────────────────────┐  │   │
│  │  - 输入框    │  │  │  技能调度器             │  │   │
│  │  - 状态栏    │  │  │  (Skill Dispatcher)    │  │   │
│  └──────┬───────┘  │  └───────────┬───────────┘  │   │
│         │          │              │               │   │
│         ▼          │              ▼               │   │
│  ┌───────────────────┐  ┌───────────────────────┐ │   │
│  │   事件总线/状态管理  │  │  技能注册表            │ │   │
│  │                   │  │  (Skill Registry)     │ │   │
│  └───────────────────┘  └──┬────┬────┬────┬────┘ │   │
│                             │    │    │    │      │   │
│                             ▼    ▼    ▼    ▼      │   │
│                     ┌────┐ ┌────┐ ┌────┐ ┌────┐  │   │
│                     │ /help │ /weather │ /export│ ...│  │
│                     └────┘ └────┘ └────┘ └────┘  │   │
└─────────────────────────────────────────────────────┘
```

### 核心组件的职责

| 组件 | 职责 | 说明 |
|------|------|------|
| **Skill 接口** | 定义契约 | 所有技能必须实现的接口 |
| **SkillRegistry** | 管理技能 | 注册、查找、注销技能 |
| **SkillDispatcher** | 调度执行 | 解析输入 → 匹配技能 → 执行 |
| **SkillContext** | 上下文传递 | 技能运行时可以访问的环境 |
| **SkillLoader** | 动态加载 | 从文件/包加载外部技能 |

## 14.3 技能系统的代码实现

### 14.3.1 核心接口定义

```typescript
/**
 * 技能执行上下文 —— 技能在运行时可以访问的环境
 */
interface SkillContext {
  /** TUI 屏幕引用 */
  screen: Widgets.Screen;
  /** 聊天面板 */
  chatBox: Widgets.BoxElement;
  /** 状态栏 */
  statusBar: Widgets.BoxElement;
  /** 发送系统消息到聊天 */
  sendSystemMessage: (content: string) => void;
  /** 发送用户消息 */
  sendUserMessage: (content: string) => void;
  /** 获取当前对话历史 */
  getMessages: () => ChatMessage[];
  /** 添加消息到历史 */
  addMessage: (msg: ChatMessage) => void;
  /** 获取配置 */
  getConfig: (key: string) => any;
  /** 日志记录 */
  log: (level: string, msg: string) => void;
  /** 请求 LLM 处理（可选） */
  askLLM?: (prompt: string) => AsyncGenerator<LLMEvent>;
}

/**
 * 技能执行结果
 */
interface SkillResult {
  /** 是否成功 */
  success: boolean;
  /** 输出到聊天的消息 */
  messages?: string[];
  /** 状态栏消息 */
  statusMessage?: string;
  /** 要执行的 UI 动作 */
  actions?: SkillAction[];
}

/** 技能可以触发的 UI 动作 */
type SkillAction =
  | { type: 'clear_chat' }
  | { type: 'export_chat'; format: 'json' | 'markdown' }
  | { type: 'set_theme'; theme: string }
  | { type: 'set_status'; text: string; color: string }
  | { type: 'focus_input' }
  | { type: 'scroll_to'; position: 'top' | 'bottom' };

/**
 * 技能定义 —— 所有技能必须实现的接口
 */
interface Skill {
  /** 技能名称（用于 `/name` 调用） */
  name: string;
  /** 技能别名 */
  aliases?: string[];
  /** 简短描述 */
  description: string;
  /** 详细帮助文本 */
  help?: string;
  /** 使用示例 */
  examples?: string[];
  /** 技能分类 */
  category?: 'utility' | 'llm' | 'ui' | 'data' | 'system';
  /** 执行函数 */
  execute(args: string[], ctx: SkillContext): Promise<SkillResult>;
  /** 可选：技能初始化（注册时调用） */
  init?(ctx: SkillContext): Promise<void>;
  /** 可选：技能清理（卸载时调用） */
  destroy?(): Promise<void>;
}
```

### 14.3.2 技能注册表

```typescript
/**
 * 技能注册表 —— 管理所有技能的注册、查找和注销
 */
class SkillRegistry {
  private skills = new Map<string, Skill>();
  private aliasMap = new Map<string, string>(); // alias → name

  /** 注册技能 */
  register(skill: Skill): void {
    if (this.skills.has(skill.name)) {
      throw new Error(`技能 "${skill.name}" 已存在`);
    }
    this.skills.set(skill.name, skill);

    // 注册别名
    if (skill.aliases) {
      for (const alias of skill.aliases) {
        this.aliasMap.set(alias, skill.name);
      }
    }
  }

  /** 注销技能 */
  unregister(name: string): boolean {
    const skill = this.skills.get(name);
    if (!skill) return false;

    // 清理别名
    if (skill.aliases) {
      for (const alias of skill.aliases) {
        this.aliasMap.delete(alias);
      }
    }

    this.skills.delete(name);
    return true;
  }

  /** 根据名称或别名查找技能 */
  get(nameOrAlias: string): Skill | undefined {
    return this.skills.get(nameOrAlias) || this.skills.get(this.aliasMap.get(nameOrAlias) || '');
  }

  /** 检查技能是否存在 */
  has(nameOrAlias: string): boolean {
    return this.skills.has(nameOrAlias) || this.aliasMap.has(nameOrAlias);
  }

  /** 获取所有技能（按分类） */
  getAll(): Skill[] {
    return Array.from(this.skills.values());
  }

  /** 按分类获取技能 */
  getByCategory(category: string): Skill[] {
    return this.getAll().filter(s => s.category === category);
  }

  /** 搜索技能 */
  search(query: string): Skill[] {
    const q = query.toLowerCase();
    return this.getAll().filter(s =>
      s.name.toLowerCase().includes(q) ||
      s.description.toLowerCase().includes(q) ||
      s.aliases?.some(a => a.toLowerCase().includes(q))
    );
  }

  /** 获取所有技能数量 */
  get count(): number {
    return this.skills.size;
  }

  /** 清空所有技能 */
  clear(): void {
    this.skills.clear();
    this.aliasMap.clear();
  }
}
```

### 14.3.3 技能调度器

```typescript
/**
 * 技能调度器 —— 解析用户输入，匹配并执行技能
 */
class SkillDispatcher {
  constructor(
    private registry: SkillRegistry,
    private ctx: SkillContext,
  ) {}

  /**
   * 尝试将输入解析为技能调用并执行
   * @returns true 如果输入是技能调用并已执行
   */
  async tryDispatch(input: string): Promise<boolean> {
    const parsed = this.parseInput(input);
    if (!parsed) return false;

    const { skillName, args } = parsed;
    const skill = this.registry.get(skillName);
    if (!skill) {
      this.ctx.sendSystemMessage(
        `{yellow-fg}⚠️ 未知技能: /${skillName}{/yellow-fg}\n` +
        `{gray-fg}输入 /help 查看所有可用技能{/gray-fg}`
      );
      return true; // 已消费（显示错误）
    }

    try {
      // 执行技能
      const result = await skill.execute(args, this.ctx);

      // 处理结果
      if (result.statusMessage) {
        this.ctx.statusBar.setContent(result.statusMessage);
      }

      if (result.messages) {
        for (const msg of result.messages) {
          if (msg) this.ctx.sendSystemMessage(msg);
        }
      }

      // 执行 UI 动作
      if (result.actions) {
        for (const action of result.actions) {
          this.executeAction(action);
        }
      }

      return true;
    } catch (err) {
      this.ctx.sendSystemMessage(
        `{red-fg}❌ 技能执行错误: /${skillName}{/red-fg}\n` +
        `{white-fg}${(err as Error).message}{/white-fg}`
      );
      return true;
    }
  }

  /**
   * 解析输入: "/skillName arg1 arg2" → { skillName, args }
   */
  private parseInput(input: string): { skillName: string; args: string[] } | null {
    const trimmed = input.trim();

    // 必须以 "/" 开头
    if (!trimmed.startsWith('/')) return null;

    // 解析技能名和参数
    const parts = trimmed.slice(1).split(/\s+/);
    const skillName = parts[0].toLowerCase();
    const args = parts.slice(1);

    // 技能名不能为空
    if (!skillName) return null;

    return { skillName, args };
  }

  /** 执行 UI 动作 */
  private executeAction(action: SkillAction): void {
    switch (action.type) {
      case 'clear_chat':
        // 清空聊天面板
        break;
      case 'focus_input':
        this.ctx.screen.render();
        break;
      case 'set_status':
        this.ctx.statusBar.setContent(action.text);
        this.ctx.screen.render();
        break;
      case 'scroll_to':
        // 滚动到指定位置
        break;
    }
  }
}
```

### 14.3.4 在 TUI 中集成技能系统

将技能系统集成到已有的 `ChatTUI` 类中：

```typescript
class ChatTUIWithSkills extends ChatTUI {
  private skillRegistry = new SkillRegistry();
  private skillDispatcher!: SkillDispatcher;

  /** 重写构造函数以初始化技能系统 */
  constructor() {
    super();
    this.initSkillSystem();
  }

  private initSkillSystem(): void {
    // 创建技能上下文
    const ctx: SkillContext = {
      screen: this.screen,
      chatBox: this.chatBox,
      statusBar: this.statusBar,
      sendSystemMessage: (msg) => this.addSystemMessage(msg),
      sendUserMessage: (msg) => {
        // 模拟用户消息
      },
      getMessages: () => [...this.messages],
      addMessage: (msg) => this.messages.push(msg),
      getConfig: (key) => undefined,
      log: (level, msg) => console.log(`[${level}] ${msg}`),
    };

    this.skillDispatcher = new SkillDispatcher(this.skillRegistry, ctx);

    // 注册内置技能
    this.registerBuiltinSkills();
  }

  private registerBuiltinSkills(): void {
    this.skillRegistry.register(helpSkill);
    this.skillRegistry.register(clearSkill);
    this.skillRegistry.register(exportSkill);
    this.skillRegistry.register(themeSkill);
    this.skillRegistry.register(statusSkill);
    // ... 更多技能
  }

  /**
   * 重写发送消息逻辑 —— 先检查是否为技能调用
   */
  protected async sendMessage(text: string): Promise<void> {
    // 先尝试技能调度
    const isSkill = await this.skillDispatcher.tryDispatch(text);
    if (isSkill) return;

    // 不是技能调用，走正常 LLM 流程
    super.sendMessage(text);
  }
}
```

## 14.4 构建实用技能

### 14.4.1 /help 技能 —— 显示所有可用技能

```typescript
const helpSkill: Skill = {
  name: 'help',
  aliases: ['h', '?', 'commands'],
  description: '显示所有可用技能的帮助信息',
  help: '/help [技能名] — 显示技能列表或指定技能的详细信息',
  examples: ['/help', '/help weather', '/?'],
  category: 'utility',

  execute: async (args, ctx) => {
    if (args.length > 0) {
      // 显示特定技能的帮助
      const skillName = args[0].toLowerCase();
      const skills = ctx.getConfig?.('skills') as SkillRegistry | undefined;
      const registry = skills || (ctx as any)._registry as SkillRegistry;

      // 查找技能
      let skill: Skill | undefined;
      // ... 查找逻辑

      if (skill) {
        return {
          messages: [
            `{cyan-fg}技能: {bold}/${skill.name}{/bold}{/cyan-fg}` +
            (skill.aliases?.length ? ` {gray-fg}(别名: ${skill.aliases.map(a => '/' + a).join(', ')}){/gray-fg}` : ''),
            `描述: ${skill.description}`,
            skill.help ? `用法: ${skill.help}` : '',
            skill.examples?.length ? `\n示例:\n${skill.examples.map(e => `  {green-fg}${e}{/green-fg}`).join('\n')}` : '',
            skill.category ? `分类: {yellow-fg}${skill.category}{/yellow-fg}` : '',
          ].filter(Boolean),
        };
      }
    }

    // 显示所有技能
    const registry = ctx.getConfig?.('skills') as SkillRegistry | undefined;
    if (!registry) {
      return { success: false, messages: ['{red-fg}技能系统未初始化{/red-fg}'] };
    }

    const all = registry.getAll();
    const categories = [...new Set(all.filter(s => s.category).map(s => s.category))];

    const categoryLabels: Record<string, string> = {
      utility: '🛠️ 工具',
      llm: '🤖 LLM',
      ui: '🎨 界面',
      data: '📊 数据',
      system: '⚙️ 系统',
    };

    const lines: string[] = [
      `{bold}{cyan-fg}📋 可用技能 (共 ${all.length} 个){/bold}{/cyan-fg}`,
      `输入 /help <技能名> 查看详情`,
      ``,
    ];

    for (const cat of categories) {
      const catSkills = all.filter(s => s.category === cat);
      lines.push(`{bold}${categoryLabels[cat as string] || cat}{/bold}`);
      for (const s of catSkills) {
        const aliases = s.aliases?.length
          ? ` {gray-fg}(${s.aliases.map(a => '/' + a).join(', ')}){/gray-fg}`
          : '';
        lines.push(`  {green-fg}/${s.name}{/green-fg}${aliases} — ${s.description}`);
      }
      lines.push('');
    }

    lines.push('{gray-fg}提示: 在输入框中输入 / 开头调用技能{/gray-fg}');

    return { messages: lines };
  },
};
```

### 14.4.2 /clear 技能 —— 清空对话

```typescript
const clearSkill: Skill = {
  name: 'clear',
  aliases: ['cls', 'clean', '重置'],
  description: '清空当前对话历史',
  help: '/clear — 清空所有消息，开始新对话',
  examples: ['/clear'],
  category: 'utility',

  execute: async (args, ctx) => {
    // 支持 "soft" 模式保留欢迎信息
    const soft = args.includes('soft') || args.includes('soft');

    // 清空消息
    ctx.sendSystemMessage('{yellow-fg}🗑️ 对话已清空，开始新对话{/yellow-fg}');

    return {
      success: true,
      actions: [{ type: 'clear_chat' }, { type: 'scroll_to', position: 'top' }],
    };
  },
};
```

### 14.4.3 /export 技能 —— 导出对话

```typescript
const exportSkill: Skill = {
  name: 'export',
  aliases: ['save', 'dl', '下载'],
  description: '导出对话历史到文件',
  help: '/export [format] — 导出对话，支持 json/markdown 格式',
  examples: ['/export', '/export json', '/export markdown'],
  category: 'data',

  execute: async (args, ctx) => {
    const format = (args[0] || 'markdown').toLowerCase() as 'json' | 'markdown';
    const messages = ctx.getMessages();

    if (messages.length === 0) {
      return {
        success: false,
        messages: ['{yellow-fg}⚠️ 没有可导出的对话内容{/yellow-fg}'],
      };
    }

    let content: string;
    let filename: string;

    if (format === 'json') {
      content = JSON.stringify(messages, null, 2);
      filename = `chat-export-${Date.now()}.json`;
    } else {
      content = messages.map(m => {
        const role = m.role === 'user' ? '## 👤 用户' :
                     m.role === 'assistant' ? '## 🤖 AI' : '## 💬 系统';
        return `${role}\n\n${m.content}\n`;
      }).join('\n---\n');
      filename = `chat-export-${Date.now()}.md`;
    }

    // 写入文件（实际实现）
    // await fs.promises.writeFile(filename, content, 'utf-8');

    return {
      messages: [
        `{green-fg}✅ 对话已导出 ({format} 格式){/green-fg}`,
        `{gray-fg}文件: ${filename}{/gray-fg}`,
        `{gray-fg}共 ${messages.length} 条消息, 约 ${(content.length / 1024).toFixed(1)} KB{/gray-fg}`,
      ],
    };
  },
};
```

### 14.4.4 /theme 技能 —— 切换主题

```typescript
const themeSkill: Skill = {
  name: 'theme',
  aliases: ['colors', 'color', '主题'],
  description: '切换界面主题',
  help: '/theme [dark|light|hacker|high-contrast] — 切换主题\n/theme list — 列出所有可用主题',
  examples: ['/theme dark', '/theme light', '/theme list'],
  category: 'ui',

  execute: async (args, ctx) => {
    if (args[0] === 'list' || args[0] === 'ls') {
      return {
        messages: [
          '{bold}{cyan-fg}🎨 可用主题{/bold}{/cyan-fg}',
          '  {green-fg}dark{/green-fg} — 深色主题（默认）',
          '  {yellow-fg}light{/yellow-fg} — 浅色主题',
          '  {green-fg}hacker{/green-fg} — 黑客风格（黑底绿字）',
          '  {red-fg}high-contrast{/red-fg} — 高对比度（无障碍）',
          '',
          '{gray-fg}使用: /theme <主题名> 切换{/gray-fg}',
        ],
      };
    }

    const theme = args[0] || 'dark';
    // 应用主题（实际实现需要重绘所有 UI 元素）
    // applyTheme(theme);

    return {
      messages: [`{green-fg}✅ 主题已切换为: ${theme}{/green-fg}`],
    };
  },
};
```

### 14.4.5 /status 技能 —— 显示系统状态

```typescript
const statusSkill: Skill = {
  name: 'status',
  aliases: ['stats', 'info', 'sys', '状态'],
  description: '显示应用和系统状态信息',
  help: '/status — 显示当前会话的状态信息',
  examples: ['/status'],
  category: 'system',

  execute: async (args, ctx) => {
    const messages = ctx.getMessages();
    const userMsgs = messages.filter(m => m.role === 'user').length;
    const aiMsgs = messages.filter(m => m.role === 'assistant').length;
    const totalChars = messages.reduce((sum, m) => sum + m.content.length, 0);

    // 内存使用
    const mem = process.memoryUsage();
    const memMB = (mem.heapUsed / 1024 / 1024).toFixed(1);
    const rssMB = (mem.rss / 1024 / 1024).toFixed(1);

    return {
      messages: [
        `{bold}{cyan-fg}📊 系统状态{/bold}{/cyan-fg}`,
        ``,
        `{bold}对话统计:{/bold}`,
        `  消息总数: ${messages.length}`,
        `  用户消息: ${userMsgs}`,
        `  AI 消息: ${aiMsgs}`,
        `  总字符数: ${totalChars.toLocaleString()}`,
        ``,
        `{bold}系统信息:{/bold}`,
        `  进程内存: ${memMB} MB (堆) / ${rssMB} MB (RSS)`,
        `  Node.js: ${process.version}`,
        `  平台: ${process.platform} ${process.arch}`,
        `  终端: ${process.stdout.columns}x${process.stdout.rows}`,
        `  PID: ${process.pid}`,
        ``,
        `{bold}运行时:{/bold}`,
        `  运行时间: ${Math.floor(process.uptime() / 60)} 分`,
        `  技能数: ${ /* registry.count */ 0 }`,
      ],
    };
  },
};
```

### 14.4.6 /search 技能 —— 搜索历史消息

```typescript
const searchSkill: Skill = {
  name: 'search',
  aliases: ['find', 'grep', '搜索'],
  description: '在对话历史中搜索关键词',
  help: '/search <关键词> — 搜索包含关键词的消息',
  examples: ['/search 天气', '/search TypeScript'],
  category: 'utility',

  execute: async (args, ctx) => {
    if (args.length === 0) {
      return { success: false, messages: ['{yellow-fg}⚠️ 请输入搜索关键词{/yellow-fg}\n  用法: /search <关键词>'] };
    }

    const query = args.join(' ').toLowerCase();
    const messages = ctx.getMessages();

    const results = messages
      .map((msg, i) => ({ msg, index: i }))
      .filter(({ msg }) => msg.content.toLowerCase().includes(query));

    if (results.length === 0) {
      return { messages: [`{yellow-fg}🔍 未找到包含 "${query}" 的消息{/yellow-fg}`] };
    }

    const lines = [
      `{bold}{cyan-fg}🔍 搜索结果: "${query}" (${results.length} 条){/bold}{/cyan-fg}`,
      '',
    ];

    for (const { msg, index } of results.slice(0, 10)) {
      const roleIcon = msg.role === 'user' ? '👤' :
                       msg.role === 'assistant' ? '🤖' : '💬';
      // 截取匹配上下文
      const idx = msg.content.toLowerCase().indexOf(query);
      const start = Math.max(0, idx - 20);
      const end = Math.min(msg.content.length, idx + query.length + 40);
      const snippet = (start > 0 ? '...' : '') +
                      msg.content.slice(start, end) +
                      (end < msg.content.length ? '...' : '');

      lines.push(`  {cyan-fg}[${index}]{/cyan-fg} ${roleIcon} {gray-fg}${snippet}{/gray-fg}`);
    }

    if (results.length > 10) {
      lines.push(`  {gray-fg}... 还有 ${results.length - 10} 条结果{/gray-fg}`);
    }

    return { messages: lines };
  },
};
```

### 14.4.7 /prompt 技能 —— 加载提示词模板

```typescript
/**
 * 提示词模板
 */
interface PromptTemplate {
  name: string;
  description: string;
  prompt: string;
  category?: string;
}

const promptTemplates: PromptTemplate[] = [
  {
    name: 'review',
    description: '代码审查',
    prompt: '请审查以下代码，指出潜在问题、改进建议和最佳实践：\n\n```CODE```',
  },
  {
    name: 'explain',
    description: '解释代码',
    prompt: '请详细解释以下代码的功能和工作原理：\n\n```CODE```',
  },
  {
    name: 'refactor',
    description: '重构建议',
    prompt: '请对以下代码提出重构建议，使其更简洁、可维护和高效：\n\n```CODE```',
  },
  {
    name: 'test',
    description: '生成测试',
    prompt: '请为以下代码生成单元测试：\n\n```CODE```',
  },
  {
    name: 'doc',
    description: '生成文档',
    prompt: '请为以下代码生成 API 文档注释：\n\n```CODE```',
  },
  {
    name: 'translate',
    description: '翻译文本',
    prompt: '请将以下文本翻译成中文：\n\n```TEXT```',
  },
  {
    name: 'summarize',
    description: '总结文本',
    prompt: '请总结以下文本的核心要点：\n\n```TEXT```',
  },
];

const promptSkill: Skill = {
  name: 'prompt',
  aliases: ['p', 'template', '模板'],
  description: '使用预定义的提示词模板',
  help: '/prompt <模板名> [附加内容] — 加载提示词模板\n/prompt list — 列出所有可用模板',
  examples: ['/prompt list', '/prompt review', '/prompt translate Hello world'],
  category: 'llm',

  execute: async (args, ctx) => {
    if (args[0] === 'list' || args[0] === 'ls' || args.length === 0) {
      const lines = [
        `{bold}{cyan-fg}📝 提示词模板 ({promptTemplates.length} 个){/bold}{/cyan-fg}`,
        `用法: /prompt <模板名> [附加内容]`,
        ``,
      ];

      for (const t of promptTemplates) {
        lines.push(`  {green-fg}/prompt ${t.name}{/green-fg} — ${t.description}`);
      }

      return { messages: lines };
    }

    const templateName = args[0].toLowerCase();
    const template = promptTemplates.find(t => t.name === templateName);

    if (!template) {
      return {
        success: false,
        messages: [`{yellow-fg}⚠️ 未找到模板 "${templateName}"{/yellow-fg}\n输入 /prompt list 查看所有可用模板`],
      };
    }

    // 构建完整提示词
    const extraContent = args.slice(1).join(' ') || '（请在此处提供要处理的内容）';
    const fullPrompt = template.prompt.replace('```CODE```', extraContent)
                                      .replace('```TEXT```', extraContent);

    // 将提示词作为用户消息发送到 LLM（需要 llm 集成）
    ctx.sendSystemMessage(
      `{green-fg}📝 已加载模板: ${template.name}{/green-fg}\n` +
      `{gray-fg}${template.description}{/gray-fg}`
    );

    return { messages: [`{gray-fg}${fullPrompt}{/gray-fg}`] };
  },
};
```

### 14.4.8 /model 技能 —— 切换模型

```typescript
const modelSkill: Skill = {
  name: 'model',
  aliases: ['switch', 'llm', '模型'],
  description: '切换 LLM 模型',
  help: '/model [模型名] — 切换当前使用的模型\n/model list — 列出可用模型',
  examples: ['/model list', '/model claude-sonnet-4', '/model gpt-4o'],
  category: 'llm',

  execute: async (args, ctx) => {
    const models = [
      { id: 'mock', name: 'Mock LLM (本地模拟)', provider: 'mock' },
      { id: 'claude-sonnet-4', name: 'Claude Sonnet 4', provider: 'anthropic' },
      { id: 'claude-opus-4', name: 'Claude Opus 4', provider: 'anthropic' },
      { id: 'gpt-4o', name: 'GPT-4o', provider: 'openai' },
      { id: 'gpt-4o-mini', name: 'GPT-4o Mini', provider: 'openai' },
    ];

    if (args[0] === 'list' || args[0] === 'ls' || !args[0]) {
      return {
        messages: [
          `{bold}{cyan-fg}🤖 可用模型{/bold}{/cyan-fg}`,
          `当前: {green-fg}mock{/green-fg} (Mock LLM)`,
          ``,
          ...models.map(m => `  {green-fg}${m.id}{/green-fg} — ${m.name} {gray-fg}(${m.provider}){/gray-fg}`),
          ``,
          `{gray-fg}使用: /model <模型ID> 切换{/gray-fg}`,
        ],
      };
    }

    const modelId = args[0].toLowerCase();
    const model = models.find(m => m.id === modelId);
    if (!model) {
      return {
        success: false,
        messages: [`{yellow-fg}⚠️ 未知模型: ${modelId}{/yellow-fg}\n输入 /model list 查看可用模型`],
      };
    }

    // 切换模型（实际实现）
    return {
      messages: [
        `{green-fg}✅ 模型已切换: ${model.name}{/green-fg}`,
        `{gray-fg}Provider: ${model.provider}{/gray-fg}`,
      ],
    };
  },
};
```

### 14.4.9 /tokens 技能 —— 显示 Token 计数

```typescript
const tokensSkill: Skill = {
  name: 'tokens',
  aliases: ['token', 'usage', 'tokens计数'],
  description: '显示当前会话的 Token 使用量估算',
  help: '/tokens — 显示 Token 使用统计',
  examples: ['/tokens'],
  category: 'data',

  execute: async (args, ctx) => {
    const messages = ctx.getMessages();

    // 简单 Token 估算（实际应使用 Tokenizer）
    // 中文约 1.5 tokens/字，英文约 1 token/4 字符
    let totalTokens = 0;
    for (const msg of messages) {
      const chineseChars = (msg.content.match(/[一-鿿]/g) || []).length;
      const otherChars = msg.content.length - chineseChars;
      totalTokens += Math.ceil(chineseChars * 1.5) + Math.ceil(otherChars / 4);
    }

    const userMsgs = messages.filter(m => m.role === 'user');
    const aiMsgs = messages.filter(m => m.role === 'assistant');

    const tokenBreakdown = {
      user: userMsgs.reduce((sum, m) => {
        const cn = (m.content.match(/[一-鿿]/g) || []).length;
        return sum + Math.ceil(cn * 1.5) + Math.ceil((m.content.length - cn) / 4);
      }, 0),
      assistant: aiMsgs.reduce((sum, m) => {
        const cn = (m.content.match(/[一-鿿]/g) || []).length;
        return sum + Math.ceil(cn * 1.5) + Math.ceil((m.content.length - cn) / 4);
      }, 0),
    };

    return {
      messages: [
        `{bold}{cyan-fg}📊 Token 使用估算 (近似值){/bold}{/cyan-fg}`,
        ``,
        `  总 Token 数: {bold}${totalTokens.toLocaleString()}{/bold}`,
        `  用户 Token: ${tokenBreakdown.user.toLocaleString()} ({userMsgs.length} 条消息)`,
        `  AI Token: ${tokenBreakdown.assistant.toLocaleString()} ({aiMsgs.length} 条回复)`,
        ``,
        `{gray-fg}注意: 此为粗略估算，实际值取决于使用的 Tokenizer{/gray-fg}`,
      ],
    };
  },
};
```

## 14.5 外部技能加载器

技能可以从外部文件动态加载，实现真正的插件化架构。

### 14.5.1 从文件加载技能

```typescript
import * as fs from 'fs';
import * as path from 'path';

/**
 * 从文件加载外部技能
 * 约定: 每个技能文件导出符合 Skill 接口的对象
 */
async function loadSkillFromFile(filePath: string): Promise<Skill | null> {
  try {
    const resolvedPath = path.resolve(filePath);

    // 检查文件是否存在
    if (!fs.existsSync(resolvedPath)) {
      console.error(`技能文件不存在: ${resolvedPath}`);
      return null;
    }

    // 动态导入
    const module = await import(resolvedPath);

    // 支持默认导出或命名导出
    const skill: Skill | undefined = module.default || module.skill;
    if (!skill || typeof skill.execute !== 'function') {
      console.error(`技能文件未正确导出 Skill 对象: ${resolvedPath}`);
      return null;
    }

    // 验证必填字段
    if (!skill.name || !skill.description) {
      console.error(`技能缺少必填字段 (name/description): ${resolvedPath}`);
      return null;
    }

    console.log(`已加载技能: /${skill.name} (${filePath})`);
    return skill;
  } catch (err) {
    console.error(`加载技能失败: ${filePath}`, err);
    return null;
  }
}

/**
 * 从目录批量加载技能
 */
async function loadSkillsFromDirectory(
  dirPath: string,
  registry: SkillRegistry,
): Promise<number> {
  let loaded = 0;
  try {
    if (!fs.existsSync(dirPath)) return 0;

    const files = fs.readdirSync(dirPath)
      .filter(f => f.endsWith('.skill.ts') || f.endsWith('.skill.js'));

    for (const file of files) {
      const skill = await loadSkillFromFile(path.join(dirPath, file));
      if (skill) {
        try {
          registry.register(skill);
          loaded++;
        } catch (err) {
          console.error(`注册技能失败: ${skill.name}`, err);
        }
      }
    }
  } catch (err) {
    console.error('扫描技能目录失败:', err);
  }
  return loaded;
}
```

### 14.5.2 技能文件示例

创建一个外部技能文件 `skills/weather.skill.ts`:

```typescript
/**
 * weather.skill.ts — 天气查询技能
 * 提供比内置工具调用更快的直接天气查询
 */
import type { Skill, SkillContext, SkillResult } from '../src/skill-system';

const weatherSkill: Skill = {
  name: 'weather',
  aliases: ['w', '天气', 'tianqi'],
  description: '直接查询天气（无需 AI 处理）',
  help: '/weather <城市名> — 快速查询天气\n/weather help — 查看详细说明',
  examples: ['/weather Beijing', '/weather 北京', '/w 上海'],
  category: 'utility',

  execute: async (args: string[], ctx: SkillContext): Promise<SkillResult> => {
    if (args.length === 0 || args[0] === 'help') {
      return {
        messages: [
          `{bold}{cyan-fg}🌤️ 天气查询技能{/bold}{/cyan-fg}`,
          `快速查询天气，不经过 AI 处理，响应更快。`,
          ``,
          `{bold}用法:{/bold}`,
          `  {green-fg}/weather <城市>{/green-fg} — 查询指定城市的天气`,
          `  {green-fg}/weather help{/green-fg} — 显示此帮助`,
          ``,
          `{bold}示例:{/bold}`,
          `  {green-fg}/weather Beijing{/green-fg}`,
          `  {green-fg}/天气 上海{/green-fg}`,
        ],
      };
    }

    const city = args.join(' ');
    ctx.log('info', `天气查询: ${city}`);

    // 直接调用天气 API（不经过 LLM）
    // const data = await weatherAPI.get(city);

    // Mock 天气数据（实际使用真实 API）
    const conditions = ['☀️ 晴', '⛅ 多云', '🌧️ 小雨', '🌬️ 大风'];
    const weather = {
      city,
      temperature: Math.round(8 + Math.random() * 28),
      condition: conditions[Math.floor(Math.random() * conditions.length)],
      humidity: Math.round(25 + Math.random() * 55),
      wind: `${Math.round(5 + Math.random() * 25)} km/h`,
    };

    return {
      messages: [
        `┌─ {cyan-fg}🌤️ 天气查询{/cyan-fg} ───────────────────────────┐`,
        `│  城市: {bold}${weather.city}{/bold}`,
        `│  温度: {yellow-fg}${weather.temperature}°C{/yellow-fg}`,
        `│  天气: ${weather.condition}`,
        `│  湿度: ${weather.humidity}%`,
        `│  风速: ${weather.wind}`,
        `└─────────────────────────────────────────────┘`,
      ],
    };
  },
};

export default weatherSkill;
```

### 14.5.3 技能管理器

```typescript
/**
 * 技能管理器 —— 统筹所有技能的加载、注册、生命周期
 */
class SkillManager {
  private registry = new SkillRegistry();
  private dispatcher!: SkillDispatcher;
  private ctx!: SkillContext;
  private skillsDir: string;

  constructor(
    private screen: Widgets.Screen,
    private chatBox: Widgets.BoxElement,
    private statusBar: Widgets.BoxElement,
    skillsDir: string = './skills',
  ) {
    this.skillsDir = path.resolve(skillsDir);
    this.ctx = this.createContext();
    this.dispatcher = new SkillDispatcher(this.registry, this.ctx);
  }

  private createContext(): SkillContext {
    const ctx: SkillContext = {
      screen: this.screen,
      chatBox: this.chatBox,
      statusBar: this.statusBar,
      sendSystemMessage: (content) => {
        // 实现系统消息发送
      },
      sendUserMessage: (content) => {
        // 实现用户消息发送
      },
      getMessages: () => [],
      addMessage: () => {},
      getConfig: (key) => {
        if (key === 'skills') return this.registry;
        return undefined;
      },
      log: (level, msg) => console.log(`[Skill:${level}] ${msg}`),
    };
    return ctx;
  }

  /** 初始化技能系统 —— 加载所有技能 */
  async initialize(): Promise<void> {
    // 1. 注册内置技能
    this.registerBuiltin();

    // 2. 加载外部技能
    const externalCount = await this.loadExternal();

    // 3. 初始化每个技能
    for (const skill of this.registry.getAll()) {
      try {
        await skill.init?.(this.ctx);
      } catch (err) {
        console.error(`技能初始化失败: ${skill.name}`, err);
      }
    }

    const total = this.registry.count;
    this.ctx.sendSystemMessage(
      `{green-fg}🔌 技能系统已就绪: ${total} 个技能${externalCount > 0 ? ` (${externalCount} 个外部)` : ''}{/green-fg}`
    );
  }

  private registerBuiltin(): void {
    this.registry.register(helpSkill);
    this.registry.register(clearSkill);
    this.registry.register(exportSkill);
    this.registry.register(themeSkill);
    this.registry.register(statusSkill);
    this.registry.register(searchSkill);
    this.registry.register(promptSkill);
    this.registry.register(modelSkill);
    this.registry.register(tokensSkill);
  }

  private async loadExternal(): Promise<number> {
    try {
      return await loadSkillsFromDirectory(this.skillsDir, this.registry);
    } catch (err) {
      console.warn('外部技能加载失败（技能目录可能不存在）:', err);
      return 0;
    }
  }

  /** 调度技能执行 */
  async dispatch(input: string): Promise<boolean> {
    return this.dispatcher.tryDispatch(input);
  }

  /** 停止所有技能 */
  async shutdown(): Promise<void> {
    for (const skill of this.registry.getAll()) {
      try {
        await skill.destroy?.();
      } catch (err) {
        console.error(`技能销毁失败: ${skill.name}`, err);
      }
    }
    this.registry.clear();
  }

  get registry_(): SkillRegistry { return this.registry; }
}
```

### 14.5.4 带技能系统的完整 ChatTUI

```typescript
class ChatTUIWithSkillSystem extends ChatTUI {
  private skillManager!: SkillManager;

  constructor() {
    super();
  }

  /** 初始化技能系统（在构造函数中调用） */
  protected async initSkillSystem(): Promise<void> {
    this.skillManager = new SkillManager(
      this.screen,
      this.chatBox,
      this.statusBar,
      path.join(process.cwd(), 'skills'),
    );

    await this.skillManager.initialize();
  }

  /** 重写发送逻辑 */
  protected async sendMessage(text: string): Promise<void> {
    // 检查是否为技能调用
    const isSkill = await this.skillManager.dispatch(text);
    if (isSkill) return;

    // 正常 LLM 对话
    super.sendMessage(text);
  }

  /** 重写清理方法 */
  protected cleanup(): void {
    this.skillManager.shutdown();
    super.cleanup();
  }
}
```

## 14.6 技能开发的进阶模式

### 14.6.1 多步骤对话式技能

某些技能需要多轮对话来完成（如配置向导）：

```typescript
const wizardSkill: Skill = {
  name: 'config',
  description: '配置向导（多步骤对话）',
  execute: async (args, ctx) => {
    // 步骤 1: 询问配置项
    ctx.sendSystemMessage('{cyan-fg}⚙️ 配置向导启动{/cyan-fg}\n请输入您的 API Key:');

    // 保存状态到技能上下文
    const state = { step: 1, apiKey: '', model: '' };

    // 注册临时键盘处理器来收集输入
    const handler = (ch: any, key: any) => {
      if (key.name === 'enter') {
        if (state.step === 1) {
          state.apiKey = '***';
          state.step = 2;
          ctx.sendSystemMessage('{green-fg}✓ API Key 已设置{/green-fg}\n请选择模型 (1-3):');
        } else if (state.step === 2) {
          ctx.sendSystemMessage('{green-fg}✅ 配置完成！{/green-fg}');
        }
      }
    };

    // 使用完后移除处理器
    // ctx.screen.key('enter', handler);

    return { success: true };
  },
};
```

### 14.6.2 带 UI 渲染的技能

技能可以创建自己的 TUI 组件：

```typescript
const fileBrowserSkill: Skill = {
  name: 'files',
  description: '文件浏览器',
  execute: async (args, ctx) => {
    // 创建文件选择列表
    const files = ['src/', 'docs/', 'package.json', 'README.md'];

    const list = blessed.list({
      parent: ctx.chatBox,
      top: 0, left: 0,
      width: '100%-2',
      height: Math.min(files.length + 2, 15),
      items: files.map(f => `  ${f.startsWith('src/') ? '📁' : '📄'} ${f}`),
      style: {
        selected: { fg: 'black', bg: '#88ccff' },
      },
      keys: true,
      vi: true,
      mouse: true,
    });

    // 关闭按钮
    const closeBtn = blessed.button({
      parent: ctx.chatBox,
      top: list.height as number + 1,
      left: 2,
      width: 12,
      height: 1,
      content: ' [关闭] ',
      style: { fg: 'white', bg: '#664422', focus: { bg: '#aa8844' } },
      mouse: true,
    });

    closeBtn.on('press', () => {
      list.detach();
      closeBtn.detach();
      ctx.screen.render();
    });

    ctx.screen.render();
    return { success: true };
  },
};
```

### 14.6.3 调用 LLM 的技能

高级技能可以调用 LLM 来处理复杂任务：

```typescript
const summarizeSkill: Skill = {
  name: 'summarize',
  description: '使用 AI 总结当前对话',
  execute: async (args, ctx) => {
    if (!ctx.askLLM) {
      return { success: false, messages: ['{yellow-fg}⚠️ LLM 不可用{/yellow-fg}'] };
    }

    const messages = ctx.getMessages();

    // 1. 显示进度
    ctx.sendSystemMessage('{yellow-fg}🔄 AI 正在总结对话...{/yellow-fg}');

    // 2. 调用 LLM 进行总结
    const prompt = `请总结以下对话的核心要点，用 3-5 句话概括：\n\n${
      messages.map(m => `[${m.role}]: ${m.content.slice(0, 100)}`).join('\n')
    }`;

    let summary = '';
    for await (const event of ctx.askLLM(prompt)) {
      if (event.type === 'text') {
        summary += event.content;
      }
    }

    // 3. 返回结果
    ctx.sendSystemMessage(`{bold}{cyan-fg}📝 对话总结{/bold}{/cyan-fg}\n${summary}`);

    return { success: true };
  },
};
```

### 14.6.4 权限与安全考虑

```typescript
interface SkillSecurityConfig {
  /** 技能的安全等级 */
  level: 'safe' | 'prompt' | 'confirm' | 'restricted';
  /** 允许的文件系统访问 */
  allowedPaths?: string[];
  /** 允许的网络访问 */
  allowedDomains?: string[];
  /** 是否允许执行 shell 命令 */
  allowShell?: boolean;
}

const SKILL_SECURITY: Record<string, SkillSecurityConfig> = {
  help:       { level: 'safe' },
  clear:      { level: 'prompt' },
  export:     { level: 'prompt', allowedPaths: ['.'] },
  theme:      { level: 'safe' },
  status:     { level: 'safe' },
  search:     { level: 'safe' },
  prompt:     { level: 'safe' },
  model:      { level: 'safe' },
  tokens:     { level: 'safe' },
  weather:    { level: 'safe', allowedDomains: ['api.weather.com'] },
  files:      { level: 'confirm', allowedPaths: ['.'] },
  config:     { level: 'restricted' },
};
```

## 14.7 技能系统的 Claude Code 参考

Claude Code 的技能（Slash Command）系统是一个成熟的可参考实现：

### Claude Code 技能系统特征

| 特征 | Claude Code | 本教程实现 |
|------|------------|-----------|
| **调用方式** | `/<skill-name>` | `/<skill-name>` |
| **参数传递** | 空格分隔 | 空格分隔 |
| **内置技能** | `/help`, `/review`, `/clear` 等 | `/help`, `/clear`, `/export` 等 |
| **自定义技能** | 通过配置注册 | 通过代码或文件注册 |
| **上下文访问** | 当前文件、git 状态等 | 对话历史、UI 组件等 |
| **UI 交互** | 命令输出到对话 | 消息卡片到对话 |
| **外部加载** | 配置式 | 文件加载器 |

### 从 Claude Code 学到的设计经验

```
1. 名命约定
   ── 技能名使用 kebab-case: /code-review, /clear-chat
   ── 别名提供便捷调用: /help vs /?
   ── 一致的参数风格

2. 用户体验
   ── /help 应该始终可用
   ── 未知技能给出友好的提示
   ── 执行耗时技能时显示进度

3. 安全边界
   ── 高风险操作需要确认
   ── 文件系统访问限制范围
   ── 网络请求限制域名
```

## 14.8 技能开发最佳实践

### 设计原则

| 原则 | 说明 |
|------|------|
| **单一职责** | 每个技能只做一件事，做好一件事 |
| **自文档化** | 提供完整的 description、help 和 examples |
| **容错处理** | 所有技能应有 try/catch，不因错误影响整个应用 |
| **及时反馈** | 耗时操作必须给出进度提示 |
| **清理资源** | 创建了 UI 组件的技能必须在结束时清理 |
| **权限最小化** | 只请求技能所需的最小权限 |
| **无副作用** | 技能应该可以重复执行而不产生意外副作用 |

### 技能开发检查清单

```
□ 所有输入参数有验证和默认值
□ 异常情况有友好的错误提示
□ 执行耗时操作时显示进度反馈
□ 创建的 UI 元素在结束时清理
□ 注册/注销生命周期方法正确实现
□ 提供完整的 help 和 examples
□ 不与已有技能冲突
□ 参数解析支持引号包裹的字符串
□ 输出在终端宽度内友好截断
// 进阶:
□ 支持撤销操作（如果适用）
□ 有日志记录供调试
□ 可配置的选项通过参数而非硬编码
□ 支持管道输入（如果适用）
```

### 性能建议

```typescript
// ❌ 不推荐：在技能中阻塞主线程
const heavySkill: Skill = {
  name: 'analyze',
  execute: async (args, ctx) => {
    // 同步阻塞操作会卡住整个 TUI
    const data = fs.readFileSync('large-file.json', 'utf-8');
    const result = heavyComputation(data);
    return { messages: [result] };
  },
};

// ✅ 推荐：异步执行并显示进度
const heavySkillFixed: Skill = {
  name: 'analyze',
  execute: async (args, ctx) => {
    ctx.sendSystemMessage('{yellow-fg}🔄 正在分析...{/yellow-fg}');
    
    // 异步读取
    const data = await fs.promises.readFile('large-file.json', 'utf-8');
    
    // 分片处理，避免阻塞
    const result = await new Promise<string>(resolve => {
      setImmediate(() => {
        resolve(heavyComputation(data));
      });
    });
    
    return { messages: [`{green-fg}✅ 分析完成{/green-fg}\n${result}`] };
  },
};
```

## 14.9 扩展阅读

### 与 MCP 的关联

技能系统与 MCP（第十一章）是互补关系：

```
技能系统 (Skills)               MCP 协议
────────────                   ────────
用户界面层的功能单元             后端能力的标准接口
`/weather 北京` → 直接调用      `tool_call: get_weather("Beijing")`
用户主动触发                    AI 自动触发
本地快速响应                    需要 AI 决策
轻量级插件架构                  标准化协议

结合使用:
  /mcp-weather 北京
  ├── 技能层: 解析命令 → 调用 MCP 客户端
  └── MCP 层: 标准 tool call → 返回结果
```

### 进一步演进

| 方向 | 描述 |
|------|------|
| **技能市场** | 分享和下载技能的社区平台 |
| **技能链** | 将多个技能串联为工作流 (`/chain build test deploy`) |
| **带 UI 的技能** | 技能可以创建表单、列表等交互组件 |
| **LLM 驱动的技能** | AI 根据用户意图自动推荐和调用技能 |
| **条件技能** | 基于上下文自动触发的技能（如检测到报错时自动触发 `/debug`） |

---

**实践：**
1. 为 `llm-chat.ts` 添加技能系统，实现 `/help`、`/clear` 和 `/status` 三个基本技能
2. 创建一个外部技能文件 `skills/memo.skill.ts`，实现笔记记录功能
3. 实现一个 `/translate` 技能，调用 LLM 翻译用户输入的文本

**上一章：** [第十三章：TUI 应用测试](13-testing.md)

**下一步：** [第十五章：总结与进阶实践](15-summary.md)
