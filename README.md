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

每个教程的变量保存在一个独立配置文件里(`configs/<配置名>.ts`),运行只从配置加载字段,不再通过命令行传参。

```bash
node src/run.ts                          # configs/ 下只有一个配置时直用,多个则交互选择
node src/run.ts <配置名>                 # 运行 configs/<配置名>.ts(如 vllm-toturial)
node src/run.ts --list                   # 列出 configs/ 下所有已保存的配置
```

```bash
# 运行已保存的 vllm 教程配置
node src/run.ts vllm-toturial
```

新建教程 = 复制 `configs/` 下任一配置文件改名,再改里面的字段(标题、目录、描述、轮次、提示词等),然后 `node src/run.ts <新配置名>` 运行。

## 教程配置(configs/ 目录)

每个教程一个文件,内容为完整变量,新老教程的配置都会保留:

```ts
// configs/vllm-toturial.ts
import type { TutorialConfig } from '../src/config.ts';

const config: TutorialConfig = {
  title: '教程迭代',            // 标题
  targetDir: 'vllm-toturial',   // 目标目录名(结果保存到 ./<targetDir>/ 下)
  claudeFlags: [/* claude 启动参数,默认固定附加 --verbose */],
  startAt: 1,                   // 起始轮次,默认 1;>1 时全部用精炼模板续跑
  dryRun: false,                // true 时只打印每轮提示词,不调用 claude(验证用)
  description: '...',           // 初始描述
  maxIterations: 10,            // 最大迭代次数
  firstRoundPrompt: `...`,      // 首轮提示词模板,{description}/{targetDir} 会被替换
  refinePrompt: `...`,          // 精炼轮提示词模板
};

export default config;
```

新建教程 = 复制一个已有配置文件改名,再按需微调;字段全部来自该文件,运行时不接收命令行参数。
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
pnpm start                 # 运行教程(等价于 node src/run.ts)
pnpm configs               # 列出 configs/ 下所有已保存的配置
pnpm typecheck             # tsc --noEmit 类型检查(typescript 7)
```

## 输出

最终结果保存在 `./${targetDir}/`(`targetDir` 在 `src/run.ts` 顶部定义)。
