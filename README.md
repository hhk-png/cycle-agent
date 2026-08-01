# cycle-iteration — 教程迭代工具(TypeScript 版)

对初始描述反复迭代,逐轮生成并精炼 `tui-toturial` 教程:每轮把描述 + 提示词通过 stdin 喂给 `claude -p`,以上一轮输出为基础细化。
由原先的 `tui-tutorial.sh` / `vllm-tutorial.sh` 用 `ts@7.0.2` + `node@24` 重写,支持 loading 状态、实时计时、彩色输出。

## 环境要求

- Node ≥ 24(原生运行 `.ts`,无需构建)
- pnpm(包管理)
- 已安装并登录 [Claude Code](https://claude.com/claude-code)

## 安装

```bash
pnpm install
```

## 用法

入口文件直接执行 claude 命令:

```bash
node src/run.ts "<初始描述>" [最大迭代次数]
```

| 参数 | 说明 | 必填 |
|---|---|---|
| `初始描述` | 任意文本描述(必填,缺省时报错退出) | 是 |
| `最大迭代次数` | 最多跑多少轮(默认 5) | 否 |

```bash
# 使用默认 5 轮
node src/run.ts "你的描述"

# 指定跑 3 轮
node src/run.ts "你的描述" 3
```

## 参数在文件里定义

`src/run.ts` 顶部有一组配置常量,按需修改:

```ts
const title = '教程迭代';              // 标题
const targetDir = 'vllm-toturial';     // 目标目录名(同时用于提示词拼接与结束提示)
const resultLabel = `结果: ./${targetDir}/`;
const claudeFlags = [/* 固定:--verbose 始终附加 */];
const startAt = 1;                     // 起始轮次,默认 1
const defaultMaxIterations = 5;        // 默认最大迭代次数(可用命令行第 2 个参数覆盖)
const dryRun = false;                  // true 时只打印每轮提示词,不调用 claude(验证用)
const firstRoundPrompt = `...`;        // 首轮提示词模板,{description}/{targetDir} 会被替换
const refinePrompt = `...`;            // 精炼轮提示词模板
const descArg = process.argv[2];       // 初始描述(命令行第 1 个参数)
const iterArg = process.argv[3];       // 最大迭代次数(命令行第 2 个参数)
```

本工具固定为 tutorial 模式:claude 始终附加 `--verbose`(复现 vllm-tutorial.sh),目标为 `./${targetDir}/`。

## 运行流程

```text
第 1 轮:  初始描述(生成模板) ──→  Claude 生成 → 保存到目标
第 2 轮:  读取目标内容      ──→  Claude 精炼 → 保存到目标
第 N 轮:  读取目标内容      ──→  Claude 精炼 → 保存到目标
```

`startAt` 大于 1 时,所有轮次都用「精炼」模板(配合已生成的内容续跑)。

## 交互效果

- 每轮调用 claude 时显示 loading spinner,实时刷新「已输出字节数 · 已用秒数」
- 成功显示 `✔ 完成(Ns)`,失败显示错误摘要,交互终端里会询问「是否继续下一轮」
- 非 TTY / 管道环境自动退化为纯文本行输出,便于脚本化
- Ctrl+C 中断时停掉 spinner 并终止子进程,退出码 130

## 常用脚本

```bash
pnpm start                 # 等价于 node src/run.ts
pnpm typecheck             # tsc --noEmit 类型检查(typescript 7)
```

## 输出

最终结果保存在 `./${targetDir}/`(`targetDir` 在 `src/run.ts` 顶部定义)。
