# 发布流程

把 `*-toturial/` 目录下的 Markdown 章节发到**掘金**和**微信公众号**。

只有一个入口,第一个参数选平台:

```bash
node src/publish/publish.ts <平台> [配置名] [选项…]
```

跑 `node src/publish/publish.ts` 不带参数会打印用法和当前已有的配置名。

| 平台 | 参数 | 说明 |
|---|---|---|
| 掘金 | `juejin` / `jj` / `掘金` | 能建草稿、能发布、能同步已有草稿 |
| 微信 | `wechat` / `wx` / `微信` | **只能建草稿** —— 个人订阅号没有发布接口,发表要在后台点 |

---

## 一、一次性准备

### 掘金:Cookie

```bash
pnpm publish:cookie
```

按提示粘贴浏览器里的掘金 Cookie,写入仓库根目录的 `.juejin-cookie`(已被 `.gitignore` 覆盖)。
也可以用环境变量 `JUEJIN_COOKIE`,优先级更高。

登录态会过期。命令报「登录态已失效」时重跑一次上面的命令即可,不会影响已建好的草稿。

### 微信:AppID / AppSecret / IP 白名单

1. **AppID** 填在微信配置文件的 `appId` 字段里(不是密码,可以明文)。
2. **AppSecret** 写到仓库根目录的 `.wechat-secret`(已被 gitignore):

   ```bash
   echo "你的AppSecret" > .wechat-secret
   ```

   或在环境变量 `WECHAT_APPSECRET` 里给。**不要把它贴进任何对话或日志。**
   拿法:微信开发者平台(developers.weixin.qq.com/platform/)→ 我的业务 → 公众号 → 基础信息 → 开发密钥。
   平台只在重置时显示一次。

3. **IP 白名单**。这是微信侧唯一的日常维护点:

   ```bash
   node src/publish/publish.ts wechat <微信配置名> --ip   # 查本机公网出口 IP
   ```

   把结果加进 **开发者平台 → 我的业务 → 公众号 → 基础信息 → 开发密钥 → API IP 白名单**。
   不支持通配符、不带端口,保存后要等几分钟生效。家用宽带换 IP 就要重新加。
   报 `40164 invalid ip` 就是这个。

---

## 二、配置一个教程

每个教程需要**两个**配置文件,放在 `src/publish/configs/`:

| 文件 | 用途 |
|---|---|
| `<教程目录名>.ts` | 掘金 |
| `wechat-<教程目录名>.ts` | 微信 |

新建时复制一份同名教程的配置改名即可。字段含义见文件里的注释,要点:

| 字段 | 说明 |
|---|---|
| `sourceDir` | 教程目录名,如 `ray-toturial` |
| `filePrefix` | 文件名里 `<前缀>-` 的部分。文件名是 `ray教程-00-前言与导读.md` 就填 `ray教程`;文件名没有前缀(如 `00-前言与导读.md`)就填**空串** |
| `titlePrefix` | 只在 `titleSource: 'fileName'` 时生效,给标题补系列名前缀。源文件名自带前缀就不用填 |
| `fromNumber` | 从第几章开始发。编号不连续会直接报错停住 |
| `titleSource` | `'fileName'` 用文件名当标题,`'h1'` 用源文件的一级标题 |
| `briefs` / `digests` | 每篇摘要,key 是两位编号。**微信配置直接 `...juejin.briefs` 复用掘金那份** —— 一个教程的摘要只有一处可改 |

### 硬约束

| 项 | 限制 | 超了会怎样 |
|---|---|---|
| 掘金摘要 | **单行纯文本,50~100 字**(下限按码点、上限按 UTF-16 计) | 建草稿被拒 |
| 微信 digest | ≤120 字 | 只警告 |
| 微信标题 | **≤32 字**(接口限制) | 被拒或截断。超限的篇目在配置里用 `titleOverrides` 缩短 |
| 掘金标签 | **最多 3 个** | 建草稿失败(`err_no=4031`) |
| 微信封面 | **必填** | 不传 `thumb_media_id` 建草稿报 `40007 invalid media_id` |

改完配置先校验,不联网:

```bash
node src/publish/publish.ts juejin <掘金配置名> --list
node src/publish/publish.ts wechat <微信配置名> --list
```

掘金的 `--list` 会逐条报出摘要字数与校验结果,并且要求 `categoryId` / `tagIds` 已填。
查真实 id:

```bash
node src/publish/publish.ts juejin <掘金配置名> --categories
node src/publish/publish.ts juejin <掘金配置名> --tags vllm
```

写不出摘要时,`--suggest-briefs` 会把 README 章节表里的「内容一句话」和正文引言摆出来当素材(只打印,不改文件)。

微信封面在配置里用 `coverImage` 指向一张本地图。**用 2.35:1 的图** —— 公众号按这个比例裁切,
正方形直传会被裁掉一半以上的高度。仓库里 `assets/wechat-cover.jpg` 就是按这个比例处理过的。

---

## 三、发布流程

两个平台都是同一套节奏:**先校验 → 再建草稿 → 第一篇停下看排版 → 然后一次跑完**。

### 掘金

```bash
# 1. 校验(不联网)
node src/publish/publish.ts juejin <配置名> --list

# 2. 建第 1 篇草稿,然后停下
node src/publish/publish.ts juejin <配置名> --drafts-only

#    → 打开打印出来的草稿链接:设置封面、检查 Markdown 排版
#    → 封面设好后脚本会把 cover_image URL 读回来,复用到其余各篇
#      (掘金没有图片上传接口,封面只能这样"借"一次)

# 3. 继续建完剩下的
node src/publish/publish.ts juejin <配置名> --drafts-only --yes

# 4. 发布
node src/publish/publish.ts juejin <配置名> --yes
```

`--drafts-only` 建出来的是**私有草稿,不公开**。想分批发布就先只建草稿,
之后去掉这个开关重跑 —— 已建好的草稿会被**复用**,不会重复建。

只想发一部分:

```bash
--from 5 --to 10      # 编号区间
--only 07             # 单篇
--delay 30000         # 篇间隔调大,被风控拦时用
```

### 微信

```bash
# 1. 校验 + 生成离线预览页(都不联网)
node src/publish/publish.ts wechat <配置名> --list
node src/publish/publish.ts wechat <配置名> --build
#    → 浏览器打开 .wechat-out/<配置名>/preview.html 逐期检查排版

# 2. 建草稿(第 1 期之后会停下看排版)
node src/publish/publish.ts wechat <配置名> --drafts

# 3. 继续建完
node src/publish/publish.ts wechat <配置名> --drafts --yes

# 4. 对账草稿箱(只读)
node src/publish/publish.ts wechat <配置名> --check

# 5. 排期表
node src/publish/publish.ts wechat <配置名> --plan
```

微信封面是**自动**的:配置里的 `coverImage` 上传成永久素材,拿到的 `thumb_media_id` 各期复用。
所以微信侧不需要手动设封面那一步。

**最后一步在后台**:公众号后台 → 草稿箱 → 逐篇发表,或设「定时发表」(最多提前 7 天)。

---

## 四、改了内容之后

草稿一旦建好就冻在那儿。**改了配置里的摘要或标题,已有草稿不会自动跟着变**,要显式同步:

```bash
node src/publish/publish.ts juejin <配置名> --sync
```

`--sync` 把草稿对齐到配置的当前值:

- **未发布**的草稿 → 就地改,仍是私有草稿
- **已发布**的文章 → 就地改 + 重新发布(article_id、链接、阅读量都保留)
- 只改**真的变了**的东西;正文只在源文件确实改过时才重写

微信侧不用单独同步 —— 重跑 `--drafts` 就会把内容变了的期次**就地更新**(不会删了重建):

```bash
node src/publish/publish.ts wechat <配置名> --drafts --yes
```

想彻底重建某一期(比如换了渲染器想从头来),加 `--force`,它会删掉旧草稿重建。
这是唯一会**删除**东西的操作,且只删草稿、动不到已发表的文章。

---

## 五、状态与可续跑

进度记在仓库根目录的两个文件里(都已 gitignore):

```
.juejin-publish-state.<掘金配置名>.json
.wechat-publish-state.<微信配置名>.json
```

每完成一步就落盘,所以任何时刻中断(Ctrl-C、断网、报错)都可以**用原命令直接重跑**,
已经建好的会跳过。同一时刻只允许一个进程跑同一个配置(有 pid 锁)。

⚠️ 这两个文件记着「哪些已经发出去了」,**不要删**。删了会导致已发布的文章被重发。
文件损坏时脚本会硬停,也不会接受 `--force` 绕过 —— 那种情况下按提示修复或移走它。

退出码:`0` 完成 · `1` 出错 · `2` 在确认点暂停(状态已落盘,可续跑) · `130` 中断。

---

## 六、命令速查

### 掘金

| 命令 | 作用 |
|---|---|
| `--list` | 列待发文章 + 摘要字数预检(不联网) |
| `--suggest-briefs` | 打印摘要素材(不联网,不改文件) |
| `--categories` / `--tags <词>` | 查分类 / 标签的真实 id |
| `--dry-run` | 只打印不发送 |
| `--drafts-only` | **只建草稿,不发布** |
| `--yes` | 跳过确认门 |
| `--sync` | 把已有草稿对齐到配置的标题与摘要 |
| `--from NN` / `--to NN` / `--only NN` | 限定编号范围 |
| `--force` | 正文改过后重建草稿(旧草稿变孤儿,用 `--orphans` 查) |
| `--orphans` | 列遗留草稿 |
| `--republish NN` | 发布结果未知时,确认未发布后重发 |
| `--delay <ms>` | 篇间隔 |

### 微信

| 命令 | 作用 |
|---|---|
| `--list` | 期次清单 + 长度/标题/摘要预检(不联网) |
| `--build` | 生成预览页与 HTML 片段(不联网) |
| `--plan` | 排期表(不联网) |
| `--ip` | 查本机公网出口 IP |
| `--check` | 对账草稿箱(只读) |
| `--drafts` | 建草稿;内容变了的就地更新 |
| `--force` | 删掉旧草稿重建(唯一会删除的操作) |
| `--only NN` | 只处理某一期 |

---

## 七、代码结构

入口只有一个,**平台各自的逻辑分开住**,互不干扰:

```
publish.ts          唯一入口,按第一个参数派发;SIGINT 也只在这里注册一次
juejin.ts           掘金:建草稿 / 发布 / 同步 / 重命名
wechat.ts           微信:建草稿 / 对账 / 排期 / 预览
configs/            每个教程两个配置文件
api/juejin.ts       掘金接口(草稿、发布、分类、标签)
api/wechat.ts       微信接口(token、素材、草稿增删改查)
articles.ts         扫目录 → 拆标题/正文 → 摘要校验
wechat-issues.ts    把章节编排成公众号期次(含按 h2 拆篇的护栏)
markdown.ts         Markdown → 公众号 HTML
publish-state.ts    状态落盘、原子写、pid 锁
publish-config.ts   掘金配置的加载与校验
wechat-config.ts    微信配置的加载、校验与凭据读取
credential.ts       凭据文件读取 + gitignore 强制校验
set-cookie.ts       掘金 Cookie 的交互式写入
```
