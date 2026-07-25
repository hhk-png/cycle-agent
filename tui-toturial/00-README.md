# AI 教程系列

在ai时代，





使用到的工具，工具可以起到蒸馏大模型知识的作用。

ai可以帮我们快速补齐一个领域所需要的模块。

重要的可能不是具体的知识，重要的是知道有什么知识。







# 🖥️ TUI 应用开发教程：大模型对话终端界面

> 从零开始，使用 TypeScript + blessed 构建一个完整的大模型对话 TUI 应用。
>
> 理论与实践并重，涵盖终端底层原理、框架选型、UI/UX 设计、状态管理、工具调用、动画效果、Markdown 渲染、错误安全、MCP 协议集成、技能系统、测试策略、打包分发等完整知识体系。

## 📚 教程目录

| # | 章节 | 核心内容 | 实践 |
|---|------|---------|------|
| 01 | [TUI 概述](01-tui-overview.md) | TUI 概念、发展历史、AI 时代复兴、架构模式、与 CLI/GUI 对比 | TUI 生态调研 |
| 02 | [终端基础](02-terminal-basics.md) | ANSI 转义码、颜色/样式控制、原始模式、鼠标/焦点事件、双缓冲、差分更新、CJK 支持 | 终端控制实验 + 画布制作 |
| 03 | [TUI 框架与技术选型](03-tui-frameworks.md) | 主流框架对比（JS/TS/Go/Rust/Python）、blessed 核心概念、布局/样式/事件系统、标签语法 | 框架选择指南 + 性能基准 |
| 04 | [第一个 TUI 应用](04-first-tui-app.md) | 屏幕管理、组件树构建、布局计算、焦点循环、事件绑定、动画实现、调试技巧、组件通信 | `basic-tui.ts` 源码分析 + 练习 |
| 05 | [大模型对话界面设计](05-llm-ui-design.md) | UI/UX 核心需求、四分区布局、响应式适配、色彩系统、消息模型、状态机设计、输入增强、IME 支持、无障碍、虚拟滚动 | 界面设计模式 + 布局适应 + 长对话优化 |
| 06 | [事件系统与状态管理](06-events-state.md) | 事件传播机制、全局状态管理、状态机、AsyncGenerator 模式、AbortController、EventBus、渲染节流、批处理、事件溯源 | 状态管理实现 + 并发控制 |
| 07 | [工具调用 UI 实现](07-tool-calls.md) | Function Calling 流程、工具定义 Schema、三态卡片设计、流式参数、并行/链式调用、执行引擎、调用确认、结果缓存 | 卡片渲染 + 多种调用模式 |
| 08 | [流式响应与动画](08-streaming-animations.md) | 打字机效果、Spinner 大全、进度条、SSE/Anthropic/OpenAI 协议解析、Mock 流适配、性能优化、聚焦感知动画、过渡动画 | 流式渲染 + 动画管理 |
| 09 | [Markdown 渲染与富文本展示](09-markdown-rendering.md) | 正则替换 vs AST 方案、代码块渲染、语法高亮、表格渲染、链接/URL 处理、Emoji 适配、终端宽度自适应、渲染缓存、安全转义 | Markdown 渲染器实现 + 流式集成 |
| 10 | [错误处理与安全](10-error-handling.md) | 错误分类、优雅恢复、重试机制、边界情况（空/长/快/小）、ANSI 注入防护、输入净化、API Key 安全、进程级保护、退出设计 | 容错设计 + 安全加固 |
| 11 | [MCP 协议与工具集成](11-mcp-integration.md) | MCP 协议详解、JSON-RPC 2.0、stdio/SSE 传输、客户端实现、资源管理、进度通知、自定义服务器、离线回退 | MCP 客户端实现 + 基本服务器 |
| 12 | [高级 MCP 与生产实践](12-mcp-advanced.md) | 安全文件服务器、SSE 重连模式、客户端池管理、认证授权、断路器模式、审计日志、资源订阅、最佳实践清单 | 客户端池 + 断路器 + 鉴权 |
| 13 | [TUI 应用测试](13-testing.md) | 测试挑战与策略、单元测试、异步流测试、快照测试、虚拟终端、集成测试、CI/CD 集成、覆盖率管理 | 状态机测试 + 渲染快照 |
| 14 | [技能系统与可扩展性](14-skills-extension.md) | 技能系统架构、Skill 接口、注册表、调度器、实用技能开发、外部技能加载器、权限管理、Claude Code 参考、最佳实践 | 技能系统实现 + 外部技能加载 |
| 15 | [总结与进阶实践](15-summary.md) | 关键技术回顾、真实 API 集成、配置管理、主题系统、多会话管理、性能优化、打包分发、故障排查 FAQ、术语表、未来趋势 | 进阶实践 + 问题排查 |

## 🚀 快速开始

### 安装依赖

```bash
cd tui-toturial/examples
npm install
```

### 运行示例

**基础 TUI 示例**（展示 blessed 的基本组件和交互）：

```bash
cd tui-toturial/examples
npx tsx basic-tui.ts
```

**大模型对话 TUI**（完整的 LLM 聊天界面，含工具调用、流式输出）：

```bash
cd tui-toturial/examples
npx tsx llm-chat.ts
```

### 快捷键

| 快捷键 | 基础示例 | 大模型对话 |
|--------|---------|-----------|
| `Ctrl+S` / `Alt+S` | — | 发送消息（Alt+S 做 Windows 备选） |
| `Ctrl+Q` | 退出 | 退出 |
| `Ctrl+C` / `Ctrl+X` | — | 取消生成 |
| `Tab` / `Shift+Tab` | 切换焦点 | — |
| `↑` / `↓` | 列表导航 | 输入历史 / 聊天区域滚动 |
| `PageUp` / `PageDown` | — | 快速滚动 |
| `Enter` | 确认/点击 | — |
| `Esc` | — | 清空输入框 |
| `R` | — | 错误后重试 |

### 对话场景

在 `llm-chat.ts` 中输入以下关键词触发不同场景：

| 关键词 | 触发场景 | UI 展示 |
|--------|---------|---------|
| "北京天气" / "weather" | 🌤️ 天气查询 | 工具调用 + 结果卡片 |
| "计算 42\*7" / "calculate" | 🧮 数学计算 | 工具调用 + 结果卡片 |
| "写一段代码" / "code" | 💻 代码生成 | 流式输出 + Markdown 渲染代码块 |
| "制造错误" / "error" | ❌ 错误模拟 | 红色错误卡片 + R 键重试 |
| 其他任意内容 | 💬 自由对话 | 流式打字机效果 |

## 🎯 核心示例功能

`examples/llm-chat.ts` 是一个完整的大模型对话 TUI，支持以下所有功能：

- ✅ **流式输出** — 逐字显示 AI 生成文本，模拟打字机效果
- ✅ **工具调用 UI** — 可视化展示 Function Calling 的调用和结果（三态卡片）
- ✅ **思考动画** — 动态 Spinner 指示 AI 思考状态
- ✅ **Markdown 渲染** — 代码块、标题、列表等 Markdown 元素渲染
- ✅ **错误展示** — 结构化的红色错误提示卡片 + 重试按钮
- ✅ **多场景模拟** — 天气查询、数学计算、代码生成、错误模拟、自由对话
- ✅ **消息历史** — 多轮对话的滚动查看与状态管理（上限保护）
- ✅ **状态管理** — 状态机驱动的 UI 状态流转（6 种状态）
- ✅ **键盘导航** — 快捷键驱动，支持鼠标滚轮
- ✅ **输入历史** — ↑↓ 键切换历史输入
- ✅ **终端安全** — 进程退出时自动恢复终端状态（光标、原始模式、颜色）
- ✅ **窗口适应** — 最小尺寸检查，防止布局错乱
- ✅ **输入验证** — 空输入检测、长度检查、ANSI 注入过滤

## 📂 项目结构

```
tui-toturial/
├── README.md                    ← 本文件（教程介绍）
├── 01-tui-overview.md           ← 第一章：TUI 概述
├── 02-terminal-basics.md        ← 第二章：终端基础
├── 03-tui-frameworks.md         ← 第三章：TUI 框架与技术选型
├── 04-first-tui-app.md          ← 第四章：第一个 TUI 应用
├── 05-llm-ui-design.md          ← 第五章：大模型对话界面设计
├── 06-events-state.md           ← 第六章：事件系统与状态管理
├── 07-tool-calls.md             ← 第七章：工具调用 UI 实现
├── 08-streaming-animations.md   ← 第八章：流式响应与动画
├── 09-markdown-rendering.md     ← 第九章：Markdown 渲染与富文本展示
├── 10-error-handling.md         ← 第十章：错误处理与安全
├── 11-mcp-integration.md        ← 第十一章：MCP 协议与工具集成
├── 12-mcp-advanced.md           ← 第十二章：高级 MCP 与生产实践
├── 13-testing.md                ← 第十三章：TUI 应用测试
├── 14-skills-extension.md       ← 第十四章：技能系统与可扩展性
├── 15-summary.md                ← 第十五章：总结与进阶实践
└── examples/
    ├── package.json             ← npm 依赖配置
    ├── node_modules/            ← 依赖包目录（npm install 后生成）
    ├── basic-tui.ts             ← 基础 TUI 示例（章节四配套）
    └── llm-chat.ts              ← 大模型对话 TUI 完整实现（章节五~十四章配套）
```

## 💡 学习路径

| 阶段 | 目标 | 行动 | 预计时间 |
|------|------|------|---------|
| **基础** | 理解 TUI 概念 | 阅读第 1-3 章 | 60 分钟 |
| **入门** | 能运行并理解示例 | 第 4 章 + basic-tui.ts | 90 分钟 |
| **进阶** | 理解 LLM TUI 设计 + Markdown 渲染 | 第 5-9 章 + llm-chat.ts | 120 分钟 |
| **实战** | 错误处理、MCP 集成、真实 API | 第 10-12 章练习 | 120 分钟 |
| **创新** | 测试、打包分发、连接真实 API | 第 13-14 章 + 真实 API Key | 60+ 分钟 |
| **扩展** | 技能系统、插件化架构、外部技能开发 | 第 14 章 + 自定义技能开发 | 90 分钟 |

建议边阅读边运行 `examples/` 目录中的代码，理论与实践结合效果最佳。

## 📋 系统要求

- **Node.js** >= 18.x
- **npm** >= 9.x
- **终端**: Windows Terminal / iTerm2 / xterm-256color / VSCode 终端（推荐支持 TrueColor 的终端）

## ⚠️ 注意事项

### 1. Ctrl+S 被终端拦截？

**Unix (Linux/macOS)**: 终端会拦截 Ctrl+S 作为流控（XOFF）。运行以下命令禁用：

```bash
stty -ixon
```

**Windows Terminal**: Ctrl+S 默认不会被拦截（Windows 无 XOFF 流控）。如果仍无效，尝试以下方案：
- 使用 **Alt+S** 作为替代发送键（已内置支持）
- 在 Windows Terminal 设置中检查是否有键位冲突：
  - 设置 → 操作 → 搜索 "ctrl+s" → 删除或修改绑定
- 或使用 VSCode 终端运行（通常无此问题）

### 2. Windows 用户

建议使用 **Windows Terminal**（而非旧版 CMD 或 PowerShell），以获得最佳的 TrueColor 和 Unicode 支持。

### 3. 退出异常导致终端异常？

本教程的示例代码已内置终端保护装置。但如果遇到极端情况导致终端显示异常，运行以下命令恢复：

```bash
reset
stty sane
```

### 4. TypeScript 运行时兼容性

本教程使用 `tsx` 直接运行 `.ts` 文件，无需编译步骤：

```bash
# 确保使用 Node.js 18+（推荐 20+）
node --version

# 重新安装依赖
cd examples && rm -rf node_modules && npm install
```

## 🔧 故障排查快速参考

| 问题 | 可能原因 | 解决方案 |
|------|---------|---------|
| `npx tsx` 找不到命令 | tsx 未正确安装 | `npm install` 重新安装 |
| 界面空白无响应 | 终端太小或编码问题 | 确保终端 ≥ 80x24 且为 UTF-8 |
| 颜色显示异常 | 终端不支持 TrueColor | 检查 `$TERM` 环境变量 |
| emoji 显示为方框 | 缺少字体 | 安装 Nerd Font 或 Cascadia Code |
| Ctrl+S/Alt+S 无响应 | 被终端流控拦截 或 Windows 键位冲突 | Unix: `stty -ixon`；Windows: 用 Alt+S 发送 或检查 Windows Terminal 键位设置 |
| 中文显示乱码 | 编码非 UTF-8 | 终端设置 UTF-8 编码 |
| 程序崩溃后终端异常 | 未正确恢复状态 | 运行 `reset` 命令恢复 |

---

**开始阅读：[第一章：TUI 概述](01-tui-overview.md)**
