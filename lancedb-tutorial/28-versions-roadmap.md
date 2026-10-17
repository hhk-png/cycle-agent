# 第 28 章　版本号与路线图

这一章要讲一件很不性感、但踩坑率极高的事：**版本号。**

LanceDB 这套技术栈里，"版本"这个词至少有三层完全不同的含义，
它们互不相关、涨落节奏不同、兼容性承诺也不同。网上大量二手文章把它们混为一谈，
于是出现了"Lance 3.0 换了新文件格式"这类根本不存在的东西。
把这三层分清楚，是这一章的主要目标。

## 28.1 三层版本号

先给结论，再逐个解释。

**文件格式版本**指的是磁盘上的字节布局（存储选项 `data_storage_version`）。
截至 2026-10-17，稳定版是 **2.2**，在研的是 **2.3**（标注 unstable）；
**稳定版本之间有强兼容承诺，unstable 没有**。

**Rust crate 版本**指的是 crates.io 上的 `lance`：当前 **13.0.0**（2026-10-07），
GitHub 上还有 `v14.0.0-beta.x` 预发布；**大版本之间会破坏兼容**。

**Python 包版本**指的是 PyPI 上的 `lancedb`：当前 **0.40.0**（2026-10-07）。
它是 0.x，**按语义化版本理解会失望**。

这三层互不相关、涨落节奏不同，混着看就会造出"Lance 3.0 换了新文件格式"
这类根本不存在的东西。还有一条同样容易混的：
**`lance` 和 `lancedb` 是两个不同的包，负责两件不同的事。**

`lance`（PyPI 上叫 `pylance`）是 Lance 格式的 Python 绑定，
暴露 `lance.dataset()` 这类底层 API。
`lancedb` 是 LanceDB 这个数据库产品的 Python 包，建在 `lance` 之上。

**这里有一段必须记住的命名史：导入名和发行名不是一回事。**
`import lance` 里写的是 `lance`，但 `pip install` 时装的包叫 `pylance` ——
这个项目在 Python 侧就没有发布过叫 `lance` 的 PyPI 包。
上面那条 `ImportError: ... Please install with 'pip install pylance'` 就是这段历史的化石。
它还有一个副作用：`pip index versions lance` 和 `pip index versions pylance`
查的不是同一个东西，排查"装不上"时别查错对象。

**版本号不止 Python 这两条线。** 同一套技术栈里还有两条独立的发布流：
**TypeScript / Node** 侧是 `@lancedb/lancedb`（npm），**Rust 应用**侧是 `lancedb` crate
（注意不是 `lance` crate）—— 两者都自成一版，和上面三层版本号都不是一一对应的。
所以"我这套 LanceDB 是什么版本"这个问题的答案取决于你在哪个语言里问。

### 28.1.1 本机实测

我们在这台机器上跑了一遍：

```
python           : 3.14.6
lancedb          : 0.40.0
pyarrow          : 26.0.0
numpy            : 2.5.1
import lance FAILED: ModuleNotFoundError No module named 'lance'
```

`import lance` 直接失败，因为 `pylance` 没装。而且注意 `pip show lancedb` 的输出：

```
Requires: deprecation, lance-namespace, numpy, packaging, pyarrow, pydantic, tqdm
```

**依赖列表里没有 `lance`。** 因为 `lancedb 0.40.0` 把 Rust 原生扩展直接打进了自己的包里：

```
lancedb-0.40.0.dist-info/
lancedb/_lancedb.pyd        235,759,104 字节
lancedb/_lancedb.pyi
```

也就是说 `lancedb` 是一个**自包含**的包，它不需要 `pylance` 就能建表、建索引、写数据。
只有当你显式调用需要底层 Lance API 的功能（比如 `table.to_lance()`、
`table.compact_files()`）时，它才会去 `import lance`，
那时才会抛那条很有名的错误：

```
ImportError: The lance library is required to use this function.
Please install with 'pip install pylance'.
```

这是一个很常见的困惑源：装了 `lancedb` 却发现某些方法不能用，
以为装错了包，其实是"另一个包提供了那个方法"。

**顺带解释一个容易起疑的数字。** 上面列出的 `_lancedb.pyd` 是
**235,759,104 字节**（约 225 MiB），而 28.1.2 马上会给出 PyPI 上 `win_amd64` wheel
只有 83.4 MB —— 这两个不是同一层口径：wheel 是压缩归档，`.pyd` 是解包后的原生扩展，
约 2.8 倍的差距就是我们在这台机器上实测到的**压缩比**，不是数字写错了。

### 28.1.2 一个需要纠正的常见说法

有一类说法是"Python 3.14 上装不了 `pylance`，所以 `import lance` 失败"。
**这个归因是错的，我们实测把它纠正掉。**

在本机上：

```
$ pip index versions pylance
ERROR: No matching distribution found for pylance

$ pip index versions pylance --index-url https://pypi.org/simple
pylance (13.0.0)
Available versions: 13.0.0, 12.0.0, 11.0.0, 10.0.0, 9.0.1, ...
```

上游 PyPI 上 `pylance 13.0.0` 是存在的，而且给我们这个平台的文件是

```
pylance-13.0.0-cp310-abi3-win_amd64.whl          89.7 MB   Requires-Python: >=3.10
```

`cp310-abi3` 是 CPython 的稳定 ABI 标签，**3.10 及以上的解释器都能装，包括 3.14**。
所以真正的原因不是"3.14 没有 wheel"，而是**这台机器的 pip 指向了
一个内容滞后的镜像站**：

```
$ pip config list
global.index-url='https://pypi.tuna.tsinghua.edu.cn/simple'
```

把镜像站上的 `pylance` 页面抓下来看，它的 wheel 只到 9.0.1 为止 —— 10、11、12、13
都没有同步。换成上游索引就一切正常。

这个纠正的意义不只是"怪错了对象"：**在企业内网、离线环境、镜像站点上，
"装不上"的原因绝大多数是通路而不是平台**。排查顺序应该是
"先 `--index-url https://pypi.org/simple` 试一次，再怀疑 Python 版本"。

同一组检查里还顺手确认了两件事，都对选型有用：

```
lancedb-0.40.0-cp310-abi3-macosx_11_0_arm64.whl      67.5 MB
lancedb-0.40.0-cp310-abi3-manylinux_2_28_aarch64.whl 71.7 MB
lancedb-0.40.0-cp310-abi3-manylinux_2_28_x86_64.whl  75.2 MB
lancedb-0.40.0-cp310-abi3-win_amd64.whl              83.4 MB
```

**0.40.0 只有 4 个 wheel，没有 Intel Mac（`macosx_*_x86_64`）的那一份。**
而 lancedb 早期版本是有的：`lancedb 0.14.0` 就带
`cp38-abi3-manylinux_2_17_x86_64` 和 `cp38-abi3-manylinux_2_28_x86_64` 两份。
两年里发生的三件事——**丢掉 Intel Mac、glibc 基线从 2.17 抬到 2.28、
Python 下限从 3.8 抬到 3.10** ——都会在真实的生产环境里（CentOS 7、
Amazon Linux 2、glibc 2.17 的旧发行版）变成"装不上"。
下一节还会回到这个点。

## 28.2 文件格式版本：矩阵与别名

这一层是三层里最需要精确的一层，因为它决定"磁盘上的字节能不能被读出来"。

我们直接从官方规范的 `docs/src/format/file/versioning.md` 取了原文（下表的措辞是原文直译/照抄）：

| Version | Minimal Lance Version | Maximum Lance Version | Description |
|---|---|---|---|
| 0.1 | Any | 0.34 (write) | 初始 Lance 格式。**已经不能再写** |
| 2.0 | 0.16.0 | Any | 重构：去掉 row group，为 list / fixed size list / primitive 引入 null 支持 |
| 2.1 | 0.38.1 | Any | 增强整数与字符串压缩；struct 字段支持 null；改进嵌套字段的随机访问 |
| 2.2 | None | Any | 新增更新的嵌套类型/编码能力（**含 map 支持**）与 2.2 期的存储特性 |
| 2.3 (unstable) | None | Unspecified | 新增 sparse structural pages 与其它实验性编码 |
| `legacy` | N/A | N/A | 0.1 的别名 |
| `stable` | N/A | N/A | 你所用的那个 Lance release 里"新数据集默认版本"的别名 |
| `next` | N/A | N/A | 同一个 release 里"最新 unstable 版本"的别名 |

规范还明确了一条设计原则，值得逐字记住，原文是
「The Lance file format has a single version number for **both the overall file
format and the encoding strategy**. The major number is changed when the file
format itself is modified while the minor number is changed when only the
encoding strategy is modified.」——
也就是说，**文件格式与编码策略共用同一个版本号**。

**主版本号变 = 文件格式变了（布局、语义），次版本号变 = 只是编码策略变了（压缩率更好）。**
这条规则很有用：它意味着一次次版本升级通常只是"同样的数据写得更小"，
而主版本升级才可能要你拷一份数据集。

而且规范自己就把这条警告写在了最前面，原文是
「Any version explicitly labeled unstable, including the current 2.3 format and
the `next` alias, **should not be used for production use cases**. Unstable
formats have **no compatibility guarantee**: breaking encoding changes may make
files written by one Lance build unreadable by later builds.」。

"unstable 格式没有兼容性保证，一个 Lance 构建写出的文件可能无法被后续构建读取"——
这句话应该被当成硬约束读，而不是免责声明。

别名这一段还有一个容易踩的坑：`stable` 和 `next` **不是绝对版本，而是相对当前 release 的**。
规范说 "The `stable` and `next` aliases are resolved by the specific Lance release you are using"。
所以在升级 SDK 的同一时刻，`stable` 指向的东西可能就变了。
规范的建议是"在格式切换期间，用**显式版本号**而不要用别名，行为才跨环境确定"。

**关于在研的 2.3，必须承认已知信息很少。** 规范目前只写了它
"新增 sparse structural pages 与其它实验性编码"（`sparse structural pages`
这个措辞是逐字引自版本矩阵那一行），**具体的编码清单与稳定化时间表都没有公开**
（未经确认）。3.x 之前它都带着 unstable 标签，所以本章的正确用法是
"知道 2.3 存在、知道它不安全"，而不是去追它的特性。

**还有第四套版本号：Lance Namespace 规范。** catalog 层
（第 26 章 §26.6 讲的 namespace 与 REST）由 `lance-namespace` 这个单独的包承载，
它自成一版 —— 本机实测 `lancedb 0.40.0` 声明依赖 `lance-namespace>=0.3.2`，
实际装到的是 **0.13.0**。也就是说这套技术栈里至少有**四套**互不同步的版本号：
文件格式、`lance` crate、`lancedb` 包、namespace。引用任何一个版本号时，
先问清它属于哪一套。

### 28.2.1 用户能碰到的参数

LanceDB 侧暴露的是一个存储选项：

```
new_table_data_storage_version | legacy, stable | default stable
```

默认值是 `stable`，官方建议所有新表都用它，
"只有在旧 reader 也必须能读的时候才选 `legacy`"。
我们**没有找到**一个把表在 `2.1` 和 `2.2` 之间来回切的运行时开关，
**未经确认**。

另外，`create_table()` 上的 `data_storage_version` 参数**已经废弃**，
官方文档明确写了要改用 `storage_options["new_table_data_storage_version"]`。
但它的废弃方式不是"忽略"：传了旧参数会收到一条 `DeprecationWarning`，
实现再把 `data_storage_version` 的值**原样转发**到
`storage_options["new_table_data_storage_version"]`，老代码不报错、旧值也照旧生效，
只是多了一条警告（这个区别的工程含义放在 28.7.6 讲）。

## 28.3 "Lance V3" 到底是什么

先把结论摆出来：**截至 2026-10-17，不存在 "Lance 文件格式 V3"。**
上表的最高稳定版本是 2.2，在研的是 2.3（unstable）。
文件格式的版本号从来没有到过 3。

那为什么到处都能看到 "Lance V3"？因为它指的是**别的东西**，而且不止一种：

**第一种是向量索引格式 V3。** 官方规范的向量索引页面写得很直接：Lance 向量索引格式
「has **gone through 3 versions so far**. This document currently only records
**version 3 which is the latest version**」，而且「The specific version of the
vector index is recorded in the `index_version` field of the generic index
metadata」—— 这个 V3 指的**是索引格式**，不是文件格式。

同一个页面还顺手给了一个有用的结构事实：**每个向量索引由 2 个普通 Lance 文件组成**
——索引文件（`index.idx`）和辅助文件（`auxiliary.idx`）。
索引本身是用普通的 Lance 文件存的，所以它是"可移植、可管理"的，
不需要为索引单独设计一套存储。

注意 `index_version` 和文件格式版本是**两套独立编号**。
规范里另有一条兼容性细节：当 `IndexMetadata.index_version` 小于 1 时，
持久化的 segment 里存的是 **row id 而不是 row address**。这种"同一份元数据在不同
index_version 下含义不同"的设计，是索引格式单独编号的直接后果。

**第二种是 FTS 索引的格式版本。** LanceDB 的文档里有一个 FTS 参数：

```
block_size | int | 128 | Supported values are 128 and 256.
Setting this to 256 opts in to the experimental FTS V3 layout.
```

**这里的 V3 指的是全文索引的 posting 布局，不是文件格式。**
它的演进方向在第 29 章 §29.4 展开（那边讲 posting 编码怎么一代代变），
这里只需要记住一条：**看到 FTS V3，不要去文件格式表里找。**

**第三种是 crate 的历史版本。** `lance` crate 确实发布过 `3.0.0`
（2026-03-13），那是一个软件包版本，和文件格式毫无关系。

一句话总结这一节：**在 Lance 生态里，"V3" 大概率指索引格式或软件包版本，
几乎不指文件格式。** 任何写"Lance V3 文件格式"的文章，基本可以直接判定为不准确。

## 28.4 一次真实的兼容性事故

版本兼容性讨论很容易变成理论。但 Lance 的规范里专门有一节
`Compatibility Caveats`，用来记录**真实发生过的编码器 bug**，
这一节是本章最值得完整读一遍的材料。最典型的一条标题是
「FixedSizeList with all-null inner values (Lance 11.1.0)」，
受影响格式是 2.1 及以后；触发它的数据长这样，
「A `FixedSizeList` column where every inner value (not the outer list item
itself) is null — for example, `FixedSizeList<nullable Float32, dim=4>` where all
eight Float32 values across two outer rows are null.」

**有 bug 的写入端（Lance < 11.1.0）** 干了什么，规范写得很清楚：
「The encoder wrote `bits_per_value=0` into the FullZip page layout. **Readers of
any version rejected these pages with an error, so the data was unreadable
regardless of reader version.**」

**修好的写入端（Lance ≥ 11.1.0）** 则为这些 null 内层值存了逐行的 validity 字节，
「producing `bits_per_value > 0`. The fixed reader (Lance ≥ 11.1.0) can also decode
the old buggy pages, so old files written before 11.1.0 become readable after
upgrading.」

**向前兼容这一侧是不成立的**，而且失败方式很讲究：
「Files containing this pattern written by Lance ≥ 11.1.0 are **not readable by
Lance < 11.1.0**. Old readers encounter the `Compression::Constant` inner encoding
in the FSL descriptor and **panic rather than returning an error**.」

这个案例值得逐条拆开，因为它把"兼容性"这件事的三种形态都演了一遍。

**第一种形态：一个 bug 让数据谁都读不了。** 老写入端在"内层全 null"这种模式下
写了 `bits_per_value = 0`（即位宽为 0），这是一个数学上说得通、
实际上无法解码的编码 —— 零位宽意味着一个值都存不下来。
结果不是"新版本读不了"，而是**任何版本的 reader 都拒收**。
数据还在，就是读不出来。这类 bug 的严重程度和"文件损坏"一样，
但成因是编码器的边界条件，跟硬件没关系。

**第二种形态：修好之后向后兼容。** 新 reader 能解老的坏页。
这是最理想的情况，代价是它要求新 reader 里为这个 bug 留一段宽容代码 ——
而这段代码可能永远删不掉，因为不知道还有多少老文件在外面。

**第三种形态：修好之后向前不兼容，而且失败方式是 panic。**
新写出的文件，老 reader 遇到 FSL descriptor 里的 `Compression::Constant`
内层编码会"**panic 而不是返回错误**"。这个细节极其重要：
一个库在读到不认识的格式时**崩溃**而不是**报错**，
意味着你没法用 `try/except` 包住它做降级。
在混合版本的部署里（比如滚动升级期间，新写入端和老读取端并存），
这会把"读不到数据"升级成"进程挂掉"。

最后那行结语应该被抄进任何一份升级检查清单：**Minimum reader version for new
files: Lance 11.1.0.**

注意这里的量级：**为了让一类边界数据可读，整个生态的最低 reader 版本被抬到了 11.1.0。**
如果一个项目还锁在 11.0.x，它拿到的数据里凡是有这种模式的，就都读不了。

这类记录的价值在于它示范了一种工程态度：**兼容性不是"理论上应该没问题"，
而是一份"哪些模式踩过坑、最低 reader 版本是多少"的清单。**
规范把它单独列成一节，而不是塞在 changelog 里，这个做法本身就值得学。

## 28.5 时间线

关键节点按时间排开如下。日期都来自官方博客 / release feed / 规范文档，
来源写在括号里；**无法确认日期的条目都写了"未经确认"**。

**2024 年。** 04-13，官方博客《Lance v2: A New Columnar Container Format》
发布 **Lance 文件格式 2.0**。

**2025 年。** 03-25 的 Second Dinner 案例研究里用到了 "LanceDB Cloud" 这个说法
（官方博客）；03-27 发布 **Lance File 2.1**《Smaller and Simpler》；
04-08 的《The Future of Open Source Table Formats: Iceberg and Lance》确立了两者
"互补而非竞争"的定位；06-23 的《What is the LanceDB Multimodal Lakehouse?》
引出 06-24 的两件大事 —— **多模态湖仓套件（Search / EDA / Feature Engineering /
Training 四块）并入 LanceDB Enterprise**，以及同日宣布的 **$30M A 轮**
（Theory Ventures 领投）；08-08 发布《Manage Lance Tables in Any Catalog using
Lance Namespace and Spark》；10-03 **Lance File 2.1 宣布为 stable**；
11-18 Lance 社区治理方案公布，Lance 拆到 `lance-format` 组织（独立站点与 Discord）；
12-12 `pylance 1.0.0` 上 PyPI（Python 库开始用语义化版本）；
12-15 官方博客《Announcing Lance SDK 1.0.0》，SDK 版本与格式版本正式解耦。

**2026 年。** 03-09 发布**文件格式 2.2**《Taming Complex Data》
（Blob V2、原生 Map、压缩大升级）；04-06 的《Lance Format v2.2 Benchmarks:
Half the Storage, None of the Slowdown》给出配套基准；
04-29《How LanceDB Accelerates Vector Search at 10 Billion Scale》；
09-17 `lance` crate **12.0.0**（crates.io）；10-07 **`lance` crate 13.0.0 +
`lancedb` 0.40.0**（当前最新稳定，我们实测）；10-15 预发布 LanceDB
`v0.41.0-beta.7` 与 Lance `v14.0.0-beta.18`（GitHub releases）；
11-05 Reverie Summit（旧金山）是未来活动（官方站点）。

**这里要专门拆开一个坑：2025 年 12 月那三个"1.0.0"是三个不同的东西**，
不是同一次发布被记了三遍。PyPI 上的 `pylance`（Python 绑定包）1.0.0 在 **12-12**；
官方博客《Announcing Lance SDK 1.0.0》（宣布版本策略解耦）在 **12-15**；
crates.io 上的 `lance` crate 1.0.0 在 **12-18**（crates.io 口径，见第 29 章 §29.6.3）。
它们分属两个注册表和一套文档，日期本来就不必相同 ——
这正是本章"三层版本号"在真实数据上的样子：**同一个里程碑，三个版本流各记各的日期。**

关于 **LanceDB Cloud**，这里必须给一个和大多数文章不一致的更新。
我们在 2026-10-17 观察到的事实是：

- `lancedb.com/cloud` 返回 **404**；
- `/pricing` 不是定价页，而是跳转到联系页面；
- `docs.lancedb.com` 的 `llms.txt` 索引里**没有任何 Cloud 页面**。

也就是说 **Cloud 作为一个独立产品已经不在官网和文档里了**（2025 年它确实存在，
有案例研究和博客为证）。而 GitHub README 至今仍写着 "Cloud and Enterprise" ——
**文档和 README 没有同步**。这一条对外部读者最有误导性：
按 README 去找 Cloud，会找到一个 404。

所以本章**不描述 Cloud 的 GA 里程碑**。现存的两条线是 **开源版** 和 **企业版**。

**如果 Cloud 以某种形态回归，最可能的形状是什么？** 这是个推断（标**推测**）：
LanceDB 的对象存储原生架构决定了它不需要"多租户常驻集群"这种经典 Cloud 形态 ——
数据、索引工件、manifest 都能放进用户自己的对象桶，所以更自然的形态是
**控制面（catalog / namespace / 权限）托管、数据面留在用户桶里**，
也就是把现在企业版的 BYOC 再往"无服务控制面"推一步。这与 Turbopuffer 那种
"纯 SaaS、数据也在厂商侧"的路线相反（第 27 章 §27.12 有对照）。
在没有官方页面的情况下，这只是从架构推出来的判断，不要当事实引用。

企业版里有三件事在官方文档里 literally 写着 "Coming soon"：
**分布式检索**、**分布式索引与压实**、**企业版训练缓存**。
这三条是路线图里最"硬"的条目 —— 因为它们有明确的文档位置和明确的措辞。
除此之外的"路线图"都应该按传闻处理。

## 28.6 公司、仓库与治理

这一节放几条查证起来不难、但很容易被二手文章传错的事实。

**公司主体**：法务名称是 **Eto Labs, Inc.**，以 **LanceDB** 作为 dba，
**YC W22** 批次。这一条解释了仓库历史的另一半。

**仓库搬过两次**：`eto-ai/lance` → `lancedb/lance` → `lance-format/lance`。
这就是为什么网上大多数教程里的 Lance 链接是死的 ——
它们指向的是第一代或第二代的组织名。看到 404 不用怀疑内容，
先看组织名对不对。

**治理是不对称的，而且官方自己承认。** Lance **格式**有一个
Apache 风格的 PMC（项目管理委员会），带否决权，有真实的社区治理流程；
**LanceDB 开源项目没有**。官方那篇治理文章的原话是
"it doesn't, **at least not yet**"，并且承认
"by far the most contributions coming from LanceDB Inc."（贡献绝大多数来自公司）。
PMC 20 个席位里 LanceDB 员工占 9 席。

把这两件事并列会产生一个准确的判断：**"格式是社区的，实现是公司的。"**
格式层面的开放（spec 公开、有 PMC、有投票）和实现层面的集中（一个公司主导），
同时为真。这两者不矛盾，但对"我能不能自己维护一个 fork"这个问题，
答案取决于你依赖的是格式还是实现。

**企业版能力是真实存在的门槛。** 自动 GPU 索引、自动增量重建索引、
SQL 分析查询、Geneva 特征工程、远程表（`db://`）大写入调优 ——
这些都只出现在企业版文档树里。开源版没有 RBAC / SSO / 审计的现成实现：
2025-02-28 一位维护者在讨论区的原话是
"We haven't looked into any RBAC like system yet"。
所以要自己建。

## 28.7 升级与选型的实操建议

这一节是本章的收口。前面讲的是"是什么"，这里讲"怎么办"。

### 28.7.1 锁版本，而且是锁到格式那一层

**第一，Python 包锁死小版本。** `lancedb` 是 0.x，
语义化版本在 0.x 上的承诺本来就弱。
我们在 28.7.3 会看到这个包的发布节奏有多快。

**第二，更要紧的是锁住文件格式版本。** 用 `stable` 别名意味着
"跟着 SDK 走"，而规范明确说别名的解析取决于你用的那个 release。
在生产表上应该写**显式版本号**，并且在升级 SDK 前先确认
新 SDK 的 `stable` 还是不是你写在磁盘上的那个版本。

**第三，绝不要用 `next` 或 2.3。** 规范原文是 "no compatibility guarantee"。

### 28.7.2 跨版本读写必须先验证

升级前至少做三件事：

**读一遍目标版本的 `Compatibility Caveats`。** 28.4 那节就是一个现成的例子：
如果你的数据里有 `FixedSizeList` 内层全 null 的行，那么新写入的文件
在 11.1.0 之前的老 reader 上会 **panic**。混合版本部署（滚动升级、
新写入端 + 老 Serving 端）是这类问题的唯一触发场景，也是生产里最常见的场景。

**过一遍大版本的 breaking changes 列表。**
`lance` crate 13.0.0 的 release notes 自己列了 **9 条 Breaking Changes**，
并注明这是"距离上一个 release 以来 main 上的 202 个提交"。
202 个提交里 9 条破坏性变更，这个比例说明"小步快跑"的代价落到了用户身上。

**准备一次数据集重写。** 升级格式版本不是改一个开关：
官方在 2.1 变 stable 时的说明是"你需要**拷贝一份数据集**"
（`lance.write_dataset(ds, "my_2_1_dataset", data_storage_version="2.1")`）。
2.1 被官方称为"最后一个需要拷贝数据集才能升级的版本"，
但**这句话本身是对未来格式的承诺，不是对过去的描述** ——
从 2.0 升到 2.1 确实要拷。

顺便，官方还诚实地提醒了一件事：**升级不一定有收益。**
"Some workflows will not significantly benefit. **Vector embeddings, images,
and audio are all pre-compressed** and often make up the majority of the data
in a dataset, so there may not be much total impact."
如果你的表主要是向量和图片，格式升级带来的压缩收益可能接近零，
而重写整个数据集的成本是实打实的。

### 28.7.3 版本churn：数字比形容词有用

`lance` crate 的大版本节奏（数据来自 crates.io，我们直接查过）：
`10.0.0` 在 2026-08-07、`11.0.0` 在 2026-08-30、`12.0.0` 在 2026-09-17、
`13.0.0` 在 2026-10-07。**从 10.0.0 到 13.0.0 是 61 天，也就是 8 周零 5 天，
四个大版本。**
同一天（2026-08-06）还发布了 6 个老版本线的补丁：
`9.0.1`、`8.0.1`、`7.1.0`、`6.1.0`、`4.0.2`、`3.0.2`。
另外 `4.0.2` 之后直接是 `6.0.0` —— **`5.0.0` 从未发布过。**

`lancedb` 那边是另一套编号：PyPI 上共 40 个发布，从 `0.14.0` 到 `0.40.0`，
最新稳定是 0.40.0。Python 侧还一度和 Rust/Node 侧用不同的 tag
（出现过 `python-v0.36.0` 和 `v0.33.0` 并存），
**两个数字流一度是分叉的**。

这些数字不构成"这个项目不成熟"的判断 —— 快速迭代的数据库项目都长这样。
但它构成一条很具体的运维建议：**跨大版本升级要当成一次正式变更来做，
不要放在依赖的 `^` 范围里让它自己飘。**

### 28.7.4 平台矩阵在收缩

这一点在最开始就要验证，因为它能让整个项目直接跑不起来。

**Python 下限是 3.10。** `lancedb 0.40.0` 的 `Requires-Python` 是 `>=3.10`，
wheel 标签是 `cp310-abi3`。历史上是 `cp38-abi3`（0.14.0）→ `cp39-abi3`（0.17.0）
→ `cp310-abi3`（0.37.1）。**你已经不能再用 3.8/3.9 了。**

**glibc 下限是 2.28。** 0.40.0 只有 `manylinux_2_28` 的 Linux wheel。
而 0.14.0 同时提供 `manylinux_2_17` 和 `manylinux_2_28`。
也就是说 **CentOS 7 / RHEL 7 / Amazon Linux 2 这些 glibc 2.17 的发行版，
官方 wheel 已经装不上了**。

**Intel Mac 没有 wheel 了。** 0.40.0 的 macOS wheel 只有 `arm64`。

**wheel 体积涨了 2.8 倍。** 同样是 manylinux x86_64：
`0.14.0` 是 27.0 MB，`0.40.0` 是 75.2 MB。这不只是下载慢的问题 ——
它意味着容器镜像、函数计算包的体积和冷启动时间都在变。

**没有 sdist。** 官方没有承诺提供源码包路径，所有发布都只有 wheel。
这意味着在"平台不匹配"的时候，你**没有"自己编译一个"的退路**，
只能换平台或换版本。

**旧 wheel 也没有 EOL 公告。** 官方不承诺任一老版本的轮子会保留多久
（截至 2026-10-17 未见此类公告，**未经确认**）。含义很直接：
CI 里钉版本时最好把用到的 wheel 一并镜像到自己可控的源，
否则"平台矩阵收缩 + 没有 sdist + 老 wheel 下架"三件事叠起来，
会出现一个你无法复现的构建。

还有一个不在版本号里、但同样会让程序挂掉的坑：
**默认的 `lancedb` wheel 目标是 `x86-64-haswell`（要求 AVX2 + FMA + F16C）。**
在没有 AVX2 的老 CPU 上，`import lancedb` 会直接以
`Illegal instruction` 崩溃，而不是抛一个 Python 异常。
官方给的方案是换装 `lancedb-compat`。**这条应该在"部署前检查清单"的第一行**，
因为它连 try/except 都包不住。

### 28.7.5 官方自己承认的"还没稳定"

有三条官方的原话，建议在任何长期项目立项前读一遍。

**Rust 公共 API 仍是实验状态**（出自官方 Migration Guide）：
"Currently, we are refining the Rust public API so that we can move it out of
experimental status and make stronger commitments to backwards compatibility."
换句话说，**用 Rust 直接依赖 `lance` crate，你承受的是实验期 API。**

**`lance-arrow` 很可能永远不会 stable**，官方建议是 fork：
"we do not expect `lance-arrow` to ever be stable, and you may want to
consider forking these utilities."

**`fork` 是不安全的**：LanceDB 内部是多线程的，而 `fork` 加多线程进程本身就不安全，
必须用 `spawn`。这条直接影响 gunicorn / uwsgi 那类 pre-fork 部署模型；
完整的进程模型（`fork`、`spawn` 与 AVX2 的关系）在第 25 章 §25.5，这里不重复。

把这三条放在一起，得出的实践结论是：
**把 LanceDB 当成一个带版本契约的二进制依赖，而不是一个可以随便改的源码库。**
这在 Python 侧是自然的（本来就只有 wheel），在 Rust 侧需要有意识地去忍。

### 28.7.6 弃用、移除与 LTS：官方没有承诺

**先说结论：LanceDB 没有 LTS 版本，也没有公开的弃用 / 移除时间表。**
官方文档里能观察到的"弃用"只有"参数搬家"这一种形态 ——
比如 `create_table(data_storage_version=...)` 被废弃、改到
`storage_options["new_table_data_storage_version"]`（28.2.1 讲过）。

**但要看清楚它是怎么废弃的。** 本机实测（`lancedb 0.40.0`）传旧参数时发生三件事：
发一条 `DeprecationWarning`、把参数值**转发**到新位置、**旧值照旧生效** ——
显式传 `data_storage_version="legacy"` 建出来的仍然是格式 0.1 的表。
这是"换了位置、没换行为"。

这个区别在工程上完全不是一回事。**"发警告 + 转发参数 + 旧值仍生效"**这一种，
升级时不用怕：老写法继续能用，警告只是催你换位置。**"静默忽略、退回默认值"**
是另一种：老代码不报错，行为却已经悄悄改了，只能把一个一个参数拿去验证真实效果。
前者可以放心升级，后者必须逐个验证。LanceDB 现在落在前者，但碰到新的废弃参数，
先确认它属于哪一类再动手，别默认"没报错就是没变"。

大版本号也不保证连续：**`lance 5.0.0` 从未发布过**（`4.0.2` 之后直接是 `6.0.0`）。
所以"按版本号推断兼容性"在这里并不比"读 changelog"更省事。

**LTS 这一侧只能给出否定答案。** 截至 2026-10-17 官方没有公布任何
"长期支持版本"（**未经确认**）。对照它那 61 天四个大版本的节奏，
正确做法是**自己在 CI 里钉死具体版本、并把 wheel 归档到可控的源**，
而不是指望某条版本线被长期维护。

## 28.8 Python ↔ 文件格式 ↔ crate：一张组合兼容矩阵

前面把三层版本号分开讲了，这里把它们拼回一张表，回答一个具体问题：
**"我手上这个 SDK，能读哪些格式、能写出哪个格式。"**
下表把官方版本矩阵（28.2）与三层版本号对齐；**"`lancedb 0.40.0` 能否读 / 写"一列
是本机实测的**（0.1 到 2.3 用 `new_table_data_storage_version` 逐个试过，都能写出来），
其余列取自官方规范。

| 格式版本 | 读取所需的最低 Lance 库版本 | `lancedb 0.40.0` 能否读 / 写 | 别名 |
|---|---|---|---|
| 0.1 | 任意 | 能读能写（写出要显式选 `legacy`） | `legacy` |
| 2.0 | 0.16.0 | 能读能写 | — |
| 2.1 | 0.38.1 | 能读能写 | — |
| 2.2 | 无额外约束（规范写 None） | 能读能写，**新表默认版本** | `stable` |
| 2.3 (unstable) | 未指定（Unspecified） | 能读能写，**无兼容保证** | `next` |

**读这张表的顺序是"从右往左"。** 先确认你的 SDK 版本落在哪一行
（`lancedb 0.40.0` 捆绑的 `lance` 库远高于 0.38.1，所以 2.0 / 2.1 / 2.2 都能读），
再看你要写出的格式版本需不需要别的 reader 也读得到 ——
如果对方的 reader 还停在 11.0.x，那么 FSL 全 null 那一类数据就会踩 28.4 的坑。
**矩阵只回答"能不能读"，不回答"读出来对不对"**，后者要靠 Compatibility Caveats。

**0.1 那一行要单独说一句。** 官方规范在 0.1 一栏写的是"写入端 ≤ 0.34、已经不能再写"，
但实测并非如此：`lancedb 0.40.0` 只要显式指定 `legacy`，就能写出格式 0.1 的表，
也能往里追加、能检索。所以"能不能写"这一列要按 SDK 的实际行为读，
不能照抄规范的措辞 —— 规范的"不能再写"说的是它不再是默认写出目标，不是把写入能力删掉了。

**还有一句口径要交代清楚：这张表描述的是真实 LanceDB 的格式版本。**
本教程配套的 `mini_lancedb/` 迷你实现里根本没有"文件格式版本"这一层
（代码里搜 `data_storage_version` 零命中），所以这张表的任何一行都没法在迷你实现里复现，
要看这一层只能对着真实 `lancedb` 试。

## 28.9 小结

**"版本"在这里有三层，涨落节奏和兼容性承诺都不同。** 文件格式版本决定磁盘字节
能不能读（稳定 2.2，在研 2.3）；`lance` crate 版本（13.0.0）决定 API 长什么样；
`lancedb` 包版本（0.40.0）是你实际 `pip install` 的东西。

**不存在 "Lance 文件格式 V3"。** "V3" 指的是向量索引格式
（"gone through 3 versions so far ... only records version 3"）、
FTS 的 `block_size=256` 布局，或者历史上的 crate 版本。

**格式版本与编码策略共用一个版本号**：主版本变是格式变，次版本变只是编码策略变。
`stable`/`next` 是相对当前 release 解析的别名，**在生产表上应该写显式版本号**。

**一次真实的兼容性事故**：老写入端在"FixedSizeList 内层全 null"时写了
`bits_per_value=0`，导致任何版本都读不出来；修复在 Lance 11.1.0，
修复后写出的文件会让老 reader **panic 而不是报错**。
这是一个"兼容性清单"的样板。

**LanceDB Cloud 已从官网和文档中消失**（`/cloud` 404、`/pricing` 跳转联系页、
文档索引里没有 Cloud 页面），但 README 仍在宣传它。现存两条线是开源版和企业版，
企业版有三项能力在文档里仍写着 "Coming soon"。

**公司是 Eto Labs, Inc.（dba LanceDB，YC W22），仓库搬过两次**
（`eto-ai` → `lancedb` → `lance-format`）。**格式有 PMC 和投票，实现没有** ——
官方原话是 "it doesn't, at least not yet"。

**工程上的硬约束要提前验证**：Python ≥ 3.10、glibc ≥ 2.28、没有 Intel Mac wheel、
没有 sdist、默认 wheel 要求 AVX2。`lance` crate 在 61 天（8 周零 5 天）里发了
4 个大版本，13.0.0 带 9 条 breaking changes，`5.0.0` 从未发布。
官方自己说 Rust API 仍是实验状态、`lance-arrow` 建议 fork、`fork` 不安全
（进程模型见第 25 章 §25.5）。

下一章讲这套系统往前看的方向：哪些是已经写进规范但还没落地的、
哪些是公司愿景、哪些是已经能用的，以及有哪些明确的风险值得写进立项文档。
