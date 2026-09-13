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
node src/iterate/run.ts                          # configs/ 下只有一个配置时直用,多个则交互选择
node src/iterate/run.ts <配置名>                 # 运行 configs/<配置名>.ts(如 vllm-toturial)
node src/iterate/run.ts --list                   # 列出 configs/ 下所有已保存的配置
```

```bash
# 运行已保存的 vllm 教程配置
node src/iterate/run.ts vllm-toturial
```

新建教程 = 复制 `configs/` 下任一配置文件改名,再改里面的字段(标题、目录、描述、轮次、提示词等),然后 `node src/iterate/run.ts <新配置名>` 运行。

## 代码结构

`src/` 按**两件不同的事**分开:教程迭代,和发布。发布那部分再按职责分层。

```text
src/
  shared/                 # 两条线共用
    ui.ts                 #   终端输出(彩色 / spinner / header / info / error)
    types.ts              #   共用类型

  iterate/                # 教程迭代(把描述反复喂给 claude -p)
    run.ts                #   入口
    config.ts             #   读 configs/*.ts
    claude.ts             #   调 claude CLI

  publish/                # 发布(掘金 + 微信)
    juejin.ts             #   入口:掘金
    wechat.ts             #   入口:微信
    set-cookie.ts         #   入口:写入掘金 sessionid
    articles.ts           #   读教程 md,围栏感知取标题(两条线共用)
    credential.ts         #   凭据文件读写 + .gitignore 校验
    publish-config.ts     #   掘金配置加载(也提供 configs 目录常量)
    publish-state.ts      #   发布状态持久化(掘金/微信靠 namespace 区分)
    wechat-config.ts      #   微信配置加载
    wechat-issues.ts      #   微信期次编排:量长度 + 按 h2 拆分
    markdown.ts           #   Markdown → 微信兼容 HTML
    api/
      juejin.ts           #   掘金接口层
      wechat.ts           #   微信接口层(唯一网络出入)
```

只有 `api/` 下面那两个文件会发网络请求,其余都是本地逻辑 —— 排查「到底调了什么接口」时
从这里入手。四个入口都带 `--help` 式的用法注释在文件头,也可以直接看文件开头。

## 教程配置(configs/ 目录)

每个教程一个文件,内容为完整变量,新老教程的配置都会保留:

```ts
// configs/vllm-toturial.ts
import type { TutorialConfig } from '../src/iterate/config.ts';

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
pnpm start                 # 运行教程(等价于 node src/iterate/run.ts)
pnpm configs               # 列出 configs/ 下所有已保存的配置
pnpm typecheck             # tsc --noEmit 类型检查(typescript 7)
pnpm publish               # 把教程发布到掘金(见下节)
pnpm publish:list          # 列出待发布文章 + 摘要字数校验
pnpm publish:cookie        # 交互式写入掘金 sessionid 并验证登录态
pnpm wechat                # 把教程发布到微信公众号(见下节)
pnpm wechat:list           # 列出公众号期次 + 正文长度/标题/摘要预检(不联网)
pnpm wechat:build          # 生成微信排版预览页与 HTML 片段(不联网,零密钥)
pnpm wechat:plan           # 打印排期表与定时发表轮次(不联网)
pnpm wechat:check          # 对账草稿箱(只读,需要密钥)
pnpm wechat:drafts         # 逐期建草稿
```

## 发布到掘金

把教程章节批量发布到[掘金](https://juejin.cn)。发布配置在 `configs/publish/<名称>.ts`,
与教程配置分开存放(子目录不会被 `src/iterate/run.ts --list` 扫到)。

### 准备 Cookie

掘金没有公开 API,脚本走登录态的接口,需要浏览器里的 `sessionid`。

**推荐用交互脚本**(输入不回显,写完立刻验证登录态是否真的有效):

```bash
pnpm publish:cookie        # 或 node src/publish/set-cookie.ts
```

它会检查 `.gitignore` 是否已覆盖凭据文件(不通过就拒绝写入)、只打印掩码
(`sessionid=69c4…5bd6`)、并用一个**必须登录的只读接口**验证 —— 把失效的
Cookie 拦在发任何文章之前。非登录性问题(网络不通等)不会阻塞,文件照常保存。

也可以手动设置(两种方式都被 `.gitignore` 覆盖,不会进仓库):

```bash
export JUEJIN_COOKIE="sessionid=..."        # 方式一:环境变量(优先级更高)
echo "sessionid=..." > .juejin-cookie       # 方式二:仓库根目录的文件
```

只粘裸值(`69c4b531...`)也能识别,脚本会自动补成 `sessionid=<值>`。

> ⚠️ **`sessionid` 是 HttpOnly cookie,在 Console 里敲 `document.cookie` 看不到它。**
> 只能从 **Application(应用)面板 → Cookies → https://juejin.cn** 取,
> 或用 **Network 面板**里任意 `api.juejin.cn` 请求的「请求头 → `cookie:`」整行。
> 如果只能看到 `_tea_utm_cache`、`__tea_cookie_tokens`、`s_v_web_id` 这类,
> 说明拿到的是匿名 cookie,那里面没有登录态。

### 用法

```bash
node src/publish/juejin.ts --categories        # 查掘金真实分类 id(不需要 Cookie)
node src/publish/juejin.ts --tags vllm         # 按关键词查标签 id(不需要 Cookie)
node src/publish/juejin.ts --suggest-briefs    # 打印摘要素材(只打印,不改文件)
node src/publish/juejin.ts --list              # 待发文章 + 标题 + 摘要字数预校验
node src/publish/juejin.ts --dry-run           # 只打印不发送(不需要 Cookie)
node src/publish/juejin.ts                     # 发布:建第 1 篇草稿后暂停等你确认
node src/publish/juejin.ts --yes               # 跳过确认,一次发完
node src/publish/juejin.ts --rename            # 改已发布文章的标题(见「标题命名与改名」)
```

其余开关:`--only NN`(只发某篇)、`--from NN`(从某篇开始)、`--delay 30000`(放大篇间隔)、
`--orphans`(列出遗留草稿)、`--republish NN`(显式重发已公开文章)、`--force`(重建过期草稿)、
`--rename`(改已发布文章的标题,见下节)、`--skip-brief-check`、`--skip-body-check`、`--debug`。

### 标题命名与改名

配置里的 `titleSource` 决定标题取哪里:

- `'h1'`(默认)—— 源文件的一级标题,如 `# 09 · 量化（Quantization）`
- `'fileName'` —— 文件名去掉 `.md`,如 `vllm教程-09-量化`

掘金上 00~07 章用的是文件名这种(系列名 + 编号 + 短标题),所以配置里写了 `titleSource: 'fileName'`。

已经发出去的文章用 `--rename` 对齐标题:

```bash
node src/publish/juejin.ts --rename --dry-run     # 只打印「旧标题 → 新标题」,不发请求
node src/publish/juejin.ts --rename --only 12     # 只改一篇
node src/publish/juejin.ts --rename               # 改全部
```

掘金的模型里**已发布文章仍挂着一份草稿**,所以「改标题」= 改草稿 + 重新 publish;
实测 publish 是**就地更新** —— article_id 与链接不变,阅读量等数据保留,只有标题换掉。

> ⚠️ **草稿改了 ≠ 线上变了。** 掘金把草稿标题同步到线上文章记录是**异步**的,而且不保证
> 一次就生效:实测同一批 14 篇里,有的几分钟就变过来,有的过了半小时还没变。
> 所以 `--rename` 每次运行都会**先读线上文章记录的标题**(`article/list_by_user`),
> 而不是只看本地状态 —— 线上还是旧标题的会被重新补发布(此时草稿已是目标标题,只补发布,
> 不会重复改草稿),已经生效的跳过。**过几分钟再跑一次 `--rename` 就能补齐没同步的。**

三条安全设计:

- **只动标题** —— `article_draft/update` 必须带全字段(只传 title 有把正文清空的风险),
  所以正文/摘要/分类/标签/封面一律沿用**远端草稿读回来的值**原样传回,远端为空才退回本地值
- **改完先回读再发布** —— 回读确认标题生效、且正文长度没变;正文被误改就停住不发
  (已公开文章的正文被删是不可逆的)
- **幂等** —— 状态文件记着每篇上次发布的标题,配合线上标题一起判断:
  两边都已是目标标题才跳过;改完草稿但发布失败/没同步的重跑只会补发布,不会重复改

### 流程与安全设计

```text
建第 1 篇草稿(不公开) → 【暂停】你去掘金草稿箱设置封面 + 检查排版
                       → 脚本读回封面 URL
                       → 发布这篇 → 自动建并发其余各篇(复用封面)
```

退出码:`0` 完成、`1` 出错、`2` 在确认点暂停(状态已落盘,可续跑)、`130` 中断。

几个刻意的设计:

- **确认点放在草稿阶段** —— 建草稿没有公开副作用,排版/封面有问题时**一篇文章都还没公开**
- **每完成一步就落盘** —— 建草稿拿到 id 立刻保存。任何时刻中断最多损失一篇进度,最坏留一个孤儿草稿(草稿不公开)
- **发布请求不自动重试** —— POST 超时时「是否已发布」不可知,自动重发可能产生第二篇公开文章,所以交给人判断
- **状态文件损坏则硬停** —— `.juejin-publish-state.<配置名>.json` 解析失败时直接退出,绝不当作空状态(那会把已发布的文章重发一遍)
- **摘要在发任何请求之前全量预检** —— 14 篇一次报出全部不合格项,而不是发到一半才发现
- **发布前逐篇校验草稿正文** —— 建完草稿后用 `article_draft/detail` 把 `mark_content` 读回来与本地比对,
  不一致或为空就**停住不发**。这一条挡的是最凶的风险:接口返回成功但正文没存进去,发出去就是 14 篇空文章。
  (`--skip-body-check` 可跳过)

> 实测备忘(接口随时可能变,踩到时先看这里):
> - `query_category_briefs` **必须 GET**,POST 返回「请求路由不存在」
> - `query_tag_list` 的标签名在**嵌套的 `tag.tag_name`** 里,顶层只有 `tag_id`
> - 单篇**最多 3 个标签**,多给返回 `err_no=4031`
> - `article_draft/list_by_user` 会把 `mark_content`/`html_content` **抹成空字符串**,读正文/封面要用 `article_draft/detail`(数据在 `data.article_draft` 里,不在顶层)
> - `article_draft/update` 的键名是 **`id`,不是 `draft_id`** —— 用 `draft_id` 只会得到 `err_no=2「参数错误」`,不说是哪个字段
> - `article/publish` 对「已带 article_id 的草稿」是**就地更新**,返回同一个 article_id(改标题就靠这个)
> - 不存在 `article/update`、`article/edit`、`article/offline`、`article_draft/remove` 等路由 —— 改文章只能走「改草稿 + 重新发布」
> - 标题改动**异步**同步到线上文章记录,时延从几分钟到半小时以上不等,不保证一次生效;重发一次能催
> - `article/list_by_user` 的 `data` **直接是数组**(不是 `data.data`),标题在**嵌套的 `article_info.title`** 里
> - 新发布的文章公开页可能先 404 几分钟才可见,但创作中心里已是发布状态

> ⚠️ 掘金接口是非官方的,靠抓包得来,随时可能变。一次性连发多篇也可能触发风控,
> 被拦时用 `--delay` 调大间隔后重跑即可。

## 发布到微信公众号

把教程章节搬到微信公众号。配置在 `configs/publish/wechat-<名称>.ts`
(靠 `wechat-` 前缀与掘金配置区分,`publish/juejin.ts` 的配置选择器会跳过它),摘要直接从
掘金那份配置 `import` 过来 —— 一个教程的摘要只有一处可改。

微信和掘金有三个本质区别,决定了文案流程不一样:

1. **正文有 2 万字符硬上限**,而且这 2 万算的是 **HTML 源码长度**(含标签),
   不是读者看到的字数。转换后实测 15 篇里有 9 篇超限。
2. **个人订阅号没有群发/发布接口。** 未认证个人主体调群发接口返回 `48001`。
   草稿箱接口(`draft/add`)不受影响 —— 所以自动化只覆盖到「建草稿」,
   最后一步「发表」只能在后台点。
3. **公众号不认 Markdown,截图外链也会被过滤。** 必须转成 HTML,
   且不能带 `<style>`/`<script>`/`<iframe>`(会被剥掉)。

### 准备凭据

```bash
echo "你的AppSecret" > .wechat-secret     # 仓库根目录;已被 .gitignore 覆盖
```

AppID 明文写在配置里(不是密码),**AppSecret 只放 gitignored 的 `.wechat-secret`
或环境变量 `WECHAT_APPSECRET`**(优先级更高)。读取前会先用 `git check-ignore`
确认该文件确实被忽略,**不通过就拒绝使用** —— 免得凭据已经躺在会被提交的位置上
而我们还在用它发文章。运行时只打印掩码,如 `dumm…test`,明文永不回显、不进
`--debug` 输出。

> ⚠️ **别把 AppSecret 贴进对话或 issue** —— 贴了就会留在记录里,
> 和上节那个 `sessionid` 一样。写进文件即可。

还要把**调用方的公网 IP** 加进后台「设置与开发 → 基本配置 → IP 白名单」,
否则 `access_token` 那一步就返回 `40164`。脚本报这个错时会**直接把当前出口 IP
和配置路径打出来**:

```bash
node src/publish/wechat.ts --ip      # 只打印当前公网 IP,不发任何微信请求
```

### 用法

```bash
node src/publish/wechat.ts --list          # 期次清单 + 长度/标题/摘要预检(不联网)
node src/publish/wechat.ts --build         # 生成预览页与 HTML 片段(不联网,零密钥)
node src/publish/wechat.ts --plan          # 排期表 + 定时发表轮次(不联网)
node src/publish/wechat.ts --check         # 对账草稿箱(只读,需要密钥)
node src/publish/wechat.ts --drafts        # 逐期建草稿
node src/publish/wechat.ts --check --debug # 打印请求行(不含密钥)
```

其余开关:`--only NN`(只做某期,`--only 08` 会把上下两篇一起选中)、`--force`(重建已建过的草稿)、
`--yes`(跳过确认点)、`--delay 5000`(放大期间隔)、`--debug`(打印请求行,不含密钥)。
有多个微信配置时把**配置名作为位置参数**给出来,如 `node src/publish/wechat.ts wechat-vllm --drafts`。

退出码与掘金那套一致:`0` 完成、`1` 出错、`2` 在确认点暂停(状态已落盘,可续跑)、`130` 中断。

### 两条路:建草稿 / 复制粘贴

**A. 建草稿(`--drafts`,需要密钥)**

```text
access_token → 上传封面拿 thumb_media_id(只做一次,缓存进状态文件)
            → 逐期 draft/add {title, author, digest, content, thumb_media_id}
```

**B. 复制粘贴(`--build`,零密钥)**

生成 `.wechat-out/<配置名>/preview.html`:一页列出全部期次,**每期一个「复制正文」
按钮**(复制 `text/html`,粘进编辑器保留排版)。同时落每期的 HTML 片段文件,
就是 API 用的同一份 `content` —— 一份产物两用。不想给密钥、或者想先看排版时走这条。

### 拆篇:超长文章按 h2 切开

```text
转成 HTML → 量字符数
  ≤ 上限   → 一期
  > 上限   → 在 h2 边界切开,取「最长那份尽量短」的分法(经典 minimax 划分)
             切完把每份 Markdown **重新整体转换一次**再量,确认合规
```

两条不可退让的规则:

- **只切在 h2 边界,绝不按字符数硬切** —— 硬切会把代码块或表格劈成两半。
  找不到 h2 边界的超限篇会**报错停住**,而不是悄悄硬切。(`## ` 的识别带围栏感知,
  代码块里的 `## 注释` 不算标题 —— 与 `articles.ts` 的 `findTitleLine` 同一套逻辑。)
- **切点按「转换后的字符数」找,不按 Markdown 长度。** 表格小节转换后约是源文的
  2.5~3 倍,代码块约 1.3 倍 —— 按 Markdown 长度对半切会切出一个超限、一个很空。

标题后缀:两份用 `(上)`/`(下)`,三份以上用 `(一)`/`(二)`/`(三)`;摘要用全角 `（上）`。

配置里的 `boxedTables` 控制表格单元格是否加内联边框。**默认 `false`**:加了就是
约 14 万字符的纯开销,会把期数从 27 撑到 30 多。理由与实测见 `src/publish/markdown.ts` 的
`HtmlOptions` —— 嫌表格没边框就改成 `true` 再看期数。

### 排期

公众号后台自带「定时发表」**最多只能提前 7 天**排期,所以多期要分轮排。
`--plan` 按配置里的 `startDate` 逐日排下去并按 7 天分组:

```text
第 1 轮  2026-09-12 起 7 期 → 2026-09-18   · 操作日 2026-09-12
第 2 轮  2026-09-19 起 7 期 → 2026-09-25   · 操作日 2026-09-19
...
```

每天只发一篇(公众号订阅号的日限额),所以期数 = 需要的天数。

### 流程与安全设计

```text
建第 1 期草稿(不公开) → 【暂停】你去后台核对排版 + 设置封面
                       → 脚本从草稿箱把那个 thumb_media_id 借出来
                       → 复用封面,建完其余各期
```

- **确认点放在草稿阶段** —— 建草稿没有公开副作用,排版有问题时一篇都还没发出去
- **封面可以「借」** —— 你手动排好第一篇并设好封面后,脚本用 `draft/batchget`
  把草稿箱拉回来,从**已有草稿**里取一个 `thumb_media_id` 复用,所以不需要本地图片文件
- **每完成一步就落盘** —— 拿到 `media_id` 立刻保存。任何时刻中断最多损失一期进度
- **`draft/add` 会自动重试(3 次)** —— 草稿不公开,重复建最坏是多一个草稿,
  `--check` 能看出来;这点和掘金的 publish 不同(那边 POST 超时**绝不重试**,
  因为可能产生第二篇公开文章)
- **长度校验前置** —— 转换完立刻量,超限的期在发任何请求之前就报出来,
  而不是建到第 7 期才失败
- **状态文件损坏则硬停** —— `.wechat-publish-state.<配置名>.json` 解析失败直接退出,
  绝不当作空状态
- **`--check` 自动识别已发表** —— 本地记了草稿但草稿箱里没有的,说明你已经发出去并
  从草稿箱移走了,据此把状态标成 `published` 并跳过(也可能是在后台手工删了草稿,
  所以这一条只是推断,脚本会把推断结果打印出来让你看一眼)

> 实测备忘(接口随时可能变,踩到时先看这里):
> - `draft/add` 的 `content` **必须少于 2 万字符**(算 HTML 源码)且小于 1MB;
>   这跟编辑器里提示的「20000 字」不是一回事,后者算纯文本
> - 个人订阅号调群发/发布类接口一律 `48001`,**只能建草稿**
> - `access_token` 要求调用方公网 IP 在后台 IP 白名单里,否则 `40164`
>   (报错信息里带 IP,脚本会把它抠出来)
> - `type=thumb` 的永久素材上限 64KB,超过要改用 `type=image`
> - 草稿封面 `thumb_media_id` 基本是必需的,没有封面不好发
> - 微信标题上限 **32 字**(编辑器 64 字),摘要 `digest` 上限 120 字
> - `draft/delete` 是有的,草稿没公开所以删了没有副作用

## 输出

最终结果保存在 `./${targetDir}/`(`targetDir` 在 `src/iterate/run.ts` 顶部定义)。
