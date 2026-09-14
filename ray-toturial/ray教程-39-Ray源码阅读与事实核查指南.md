仓库地址：https://github.com/hhk-png/cycle-agent

# 第 39 章：Ray 源码阅读与事实核查指南

> 前面 38 章，每一章都在做同一件事：**给出一个结论，并说明它来自哪里**。
>
> 这一章反过来 —— **它教你怎么否决这本书。**
>
> 这不是修辞。这本书把「回源码核对」当成自己的核心纪律反复宣示：
> 正文里用 `grep -c` / `curl` 这类
> **可以被别人否决的命令**下结论的地方有 20 余处
> （自己数 —— **要把本章排除掉**，否则数进来的是本章自己的示范命令：
> `grep -o "grep -c\|curl" ray教程-*.md | grep -v "ray教程-39" | wc -l` → `24`），
> 附录 C 的个别词条（如「Ray Direct Transport」）后面直接附了
> **「复核命令：」** 代码块，
> 而结语把「**唯一可靠的长期能力是知道去哪儿核对**」
> 列为全书五句话里唯一一条**能力型**的。
>
> 可是 —— 读完前 38 章，你仍然不知道：
>
> * `ray_constants.py` 在哪？（**答案是"两个地方都有，但只有一个是你要的"** ——
>   见 §39.3 的 ⚠️）
> * 怎么 grep 一个配置项的真名？
> * 怎么判断一个 API「**已经不存在了**」，而不是「**我没找到**」？
> * 你正在读的文档页，是**你装的那个版本**吗？
>
> 这本书花了七轮修订、用自己犯的错换来了几条方法论，
> 却从来没有把它们**作为方法教出来**。这一章补的就是这个洞。
>
> ⚠️ 先说清楚定位：**这一章不含任何新的 Ray 知识。**
> 它是一把**尺子**，用来量前面 38 章。所以它排在最后，
> 而且**全书的结语也移到了这一章末尾**。

---

## 39.1 这一章要解决什么问题

### 三个具体的时刻

不谈"授人以渔"这种空话。你会在下面这三个时刻需要这一章：

**时刻一：你想确认一个数字。**
你读到本书说「对象存储默认占可用内存 30%」，
想在自己装的那台机器上确认这个数到底是 30% 还是别的。
**去哪找？**

**时刻二：你怀疑一个开关没生效。**
你设了 `RAY_something=1`，行为没变。你需要区分三种完全不同的情况：

* 「这个开关**根本不存在**，我记错了名字」；
* 「开关存在，但**我设的位置不对**（环境变量 vs `ray.init()` vs `ray start --system-config`）」；
* 「开关生效了，但**它管的不是我以为的那件事**」。

这三种情况的排查路径完全不同。**而它们的共同前提是：你得先能证明这个开关存在。**

**时刻三：你要否决一个结论。**
你在 issue 里看到「Ray 2.58 里没有 XXX」，
或者本书某处写着「XXX 未确认」。你想自己判一下。
**"我搜了一下没搜到"是证据吗？**

### 与前 38 章的边界

这一章**不抢任何现有章节的问题**。分界线是这样的：

| 已有章节 | 它回答的问题 | 这一章的边界 |
|---|---|---|
| [第 20 章 §20.9.1](ray教程-20-现状与未来方向.md) | 「**哪些 API 会被弃用**、怎么扫出我自己的用法」 | 那是**升级场景的特例**；这一章讲**通则** —— 任何事实怎么查。这一章**引用**它，不重写 |
| [第 24 章 附录 D](ray教程-24-附录D-FAQ与排错.md) | 「**跑不起来**怎么办」（症状 → 原因 → 修法） | 这一章答的是「**这条结论对不对**」（可信度），不是「跑不跑得起来」 |
| [第 22 章 附录 B](ray教程-22-附录B-mini-ray工程手册.md) | mini-ray 的模块地图与走读 | 这一章是 **Ray 本体**的源码导航；mini-ray 在这一章里降级为**练习靶场**（§39.9） |
| [第 21 章 附录 A](ray教程-21-附录A-API速查.md) | 直接**给答案**（API 速查表） | 这一章给「**怎么验答案**」 |
| [第 36 章 §36.2](ray教程-36-分布式追踪与OpenTelemetry.md) | 已示范 3 条「本机没装 Ray 时怎么查」的命令（在 §36.2 的子节「怎么自己确认第一层的现状」里） | 这一章把它**泛化**为方法，并把散落在各章的招式收拢成清单 |

一句话：**其余 38 章给你结论，这一章给你否决结论的能力。**

---

## 39.2 先把源码拿到手

核查的第一件事，是**让源码在你手边**。有三种办法，适用场景完全不同。

### 办法一：用你装的那个 Ray（最容易，但只覆盖一半）

```bash
python -c "import ray, os; print(ray.__version__); print(os.path.dirname(ray.__file__))"
# 输出形如：
#   2.58.0
#   /usr/lib/python3.11/site-packages/ray
```

拿到路径之后，就可以直接读 Python 侧的源码了：

```bash
RAYDIR=$(python -c "import ray, os; print(os.path.dirname(ray.__file__))")
grep -rn "def get_gpu_ids" "$RAYDIR" | head
```

**这个办法的三个优点**：① 版本**必然**和你运行的一致（这是最重要的一点）；
② 可以看到你的安装里**真实存在的文件**（而不是仓库里的布局）；
③ 可以现场做实验 —— 读到一个函数，立刻 `python -c` 调一次看行为。

**它的硬边界**：**查不到 C++ 侧，也查不到仓库的原始布局。**

pip 装到 `site-packages/` 里的是**打包后的产物**：Python 包 `ray/`、
编译好的 `.so`，以及 `ray/_private/ray_constants.py` 这类运行期文件。
而这本书里大量引用的是**仓库路径**：

* `src/ray/common/ray_config_def.h` —— 配置项的定义与默认值**在这里**，
  不在 Python 包里；
* `src/ray/raylet/scheduling/` —— 调度策略的实现；
* `release/`、`ci/` —— 测试与 CI 的形状。

这些东西`site-packages` 里没有。要按仓库路径找文件，得用办法二。

### 办法二：下载 release tarball（能拿到全部，含 C++）

```bash
# 注意 tag 的写法是 ray-<版本号>
curl -L -o ray.tgz \
  https://github.com/ray-project/ray/archive/refs/tags/ray-2.58.0.tar.gz

# 实测：射线 2.58.0 的这个包约 195 MB，解压后顶层是 ray-ray-2.58.0/
tar xzf ray.tgz
```

**它包含 `src/`（C++ 侧）。** 这一点值得单独强调，
因为本书核查配置项时最常引用的 `ray_config_def.h` 就在里面：

```bash
cd ray-ray-2.58.0
grep -n "object_spilling_threshold" src/ray/common/ray_config_def.h
# RAY_CONFIG(float, object_spilling_threshold, 0.8)
```

**不必全解压。** 195 MB 的包容得下 1 万多个文件，但你往往只需要一棵子树：

```bash
tar xzf ray.tgz --wildcards '*/src/ray/common/*'
```

> ⚠️ **两个必须知道的坑。**
>
> **坑一：软链不会被解压成内容。**
> 2.58 起，RLlib 的源码在仓库**根的 `rllib/`**，
> 而 `python/ray/rllib` 是一个**软链**：
>
> ```bash
> tar tzvf ray.tgz | grep "python/ray/rllib"
> # lrwxrwxrwx root/root 0 ... python/ray/rllib -> ../../rllib
> ```
>
> 它**没有子文件**。所以你若解压后跑
> `grep -rn "class AlgorithmConfig" python/ray/rllib/`，
> 会得到**空结果** —— 而那不是"它不存在"，是"**你查的那棵树是空的**"。
> 正确路径是根目录的 `rllib/`。
>
> **坑二：不是所有解压工具都会保留软链。**
> 有些图形化解压工具会把软链变成空目录或直接跳过。
> 所以"解压后没有"和"仓库里没有"是两件事 ——
> **判据要用 `tar tzf` 看列表，不要用解压结果看目录。**

### 办法三：只取一个文件（最快，不下载）

很多时候你只想看一个文件的当前内容，那就别下 195 MB：

```bash
curl -s https://raw.githubusercontent.com/ray-project/ray/ray-2.58.0/python/ray/data/dataset.py \
  | grep -n "def write_delta"
# 4842:    def write_delta(
```

把 tag 换成别的版本号，就能做**跨版本对比** —— 这是回答
「这个行为是哪个版本变的」最直接的办法（§39.4 的 Q4）：

```bash
for v in 2.56.0 2.57.0 2.58.0; do
  n=$(curl -s "https://raw.githubusercontent.com/ray-project/ray/ray-$v/python/ray/data/dataset.py" \
      | grep -c "def write_delta")
  echo "$v: write_delta 定义数 = $n"
done
```

> 📌 **判断返回是否可信**：路径写错时返回的**不是源码**，
> 而是一行纯文本 ——
>
> ```bash
> curl -s ".../ray-2.58.0/python/ray/data/NOPE.py"
> # 404: Not Found
> ```
>
> ⚠️ **它只有一行，而且会被 grep 静静吞掉** —— 于是 `grep -c` 得到 `0`，
> 看起来和"这个符号不存在"**完全一样**。这是本章反复说的那个坑的
> 最便宜版本：**空输出最像"没有"。**
>
> 稳妥写法是**带一个对照组**：同一个文件里 grep 一个你确信存在的符号
> （比如 `def write_parquet`），它必须 > 0，否则说明文件没取到：

```bash
f=$(curl -s "https://raw.githubusercontent.com/ray-project/ray/ray-2.56.0/python/ray/data/dataset.py")
[ "$(echo "$f" | grep -c 'def write_parquet')" -gt 0 ] || echo "⚠️ 文件没取到，结论无效"
echo "$f" | grep -c "def write_delta"
```

### 陷阱：你读的是不是你装的版本？

**这是整套核查里最容易出错、后果最严重的一步。**

Ray 的文档站有**版本选择器**（页面顶部/侧边），
它默认可能指向 `master`（开发版）而不是你装的稳定版。
**用 master 的文档去核对一个 2.58 的行为，结论可能是错的。**

三条自保规则：

1. **先固定版本**：核查开始前，先写下你要核查的版本号
   （`python -c "import ray; print(ray.__version__)"`）。
   本书的基线是 **Ray 2.58.0（2026-08-23）**。
2. **优先用源码，而不是文档**：源码带 tag，文档页的版本选择器可能被忽略。
   `raw.githubusercontent.com/.../ray-2.58.0/...` 这个 URL 里的版本号**不会骗你**。
3. **交叉一次**：一个结论至少从**两个独立来源**拿到 ——
   比如「源码里的默认值」+「release note 里的说明」。
   两者不一致时，**以源码为准**，并把不一致记下来（这往往是个真问题）。

---

## 39.3 仓库地图：去哪个目录找什么

### 顶层：最常去的九个目录
 
（2.58.0 的 tarball 里顶层还有 `docker/` / `thirdparty/` / `bazel/` 等，
都比下面这几个小、也更少去，所以不展开。）

2.58.0 的 tarball 解压后，顶层目录与文件数：

| 目录 | 文件数 | 里面是什么 | 你什么时候会去 |
|---|---|---|---|
| `python/` | 3771 | **Python 侧全部源码**（`python/ray/`）与构建脚本 | 查 API、默认值、错误信息、Python 行为 |
| `doc/` | 1632 | **文档源文件**（`doc/source/`，含 `apis/` 与 `ray-contribute/`） | 查"官方怎么描述这个行为"、找贡献指南 |
| `rllib/` | 1599 | **RLlib 源码**（2.58 起从 `python/ray/` 移到这里） | 查 RLlib 的类与方法 |
| `release/` | 1210 | **发布测试**（`autoscaling_tests` / `benchmarks` / `cluster_tests` / `dashboard` / `golden_notebook_tests` / `hello_world_tests` …） | 查"这个特性**端到端**是怎么被验证的" |
| `src/` | 1035 | **C++ 侧**（`src/ray/common/`、`src/ray/raylet/`、`src/ray/gcs/` …） | 查配置项定义、常量、调度策略、核心数据结构 |
| `java/` | 388 | Java 绑定 | 查多语言支持的真实程度 |
| `ci/` | 328 | CI 配置（`build/` / `docker/` / `env/` / `k8s/` / `lint/`） | 查"官方在哪些平台上测"（§17.10 用过） |
| `cpp/` | 83 | C++ 的对外 API 与示例 | 查 C++ 用户 API 面 |
| `.buildkite/` | 61 | 构建流水线定义 | 查平台矩阵、发布流程 |

> 📌 **数这几行的时候踩了两个坑，都值得记住**（也正是本章要教的那种坑）。
>
> **坑一：口径必须写清楚。** 上面那列是**文件数**（`find <dir> -type f | wc -l`）。
> 如果你用 `tar tzf ray.tgz | awk -F/ '$2=="python"' | wc -l` 去数，
> 得到的是 **4328** —— 因为**它把目录本身也算进去了**（3771 个文件 + 557 个目录）。
> 两个数都对，**差别只在口径**。写数字时不说口径，就等于制造了一个无法复核的断言。
>
> **坑二：证据文件会消失。** 写这一节时，作者下载的 `ray.tgz`
> 在核对过程中**被清理掉了**，于是已经写下的数字**一度无法复核** ——
> 只能重新下载一遍（195 MB）再数。
> **教训**：一旦某个数字进入了你的结论，就要保证**重跑它只需要一条命令**，
> 而不是依赖一个可能不在了的中间产物。
> 这也是为什么下面每个例子都给的是**命令**而不是"我当时看到的结果"。

> 💡 **`release/` 是被低估的一个目录。** 想知道「这个特性到底能不能用」，
> 看 `release/` 里对应的测试比看文档更接近真相 ——
> 因为**它会真的跑起来**。比如 `autoscaling_tests/` 里的用例
> 就是自动扩缩容这个特性的**真实使用样本**。

### `python/ray/` 内部：按功能分层

| 子目录 | 是什么 |
|---|---|
| `_common/` | **跨层共享的基础件**：`ray_option_utils.py`、`deprecation.py`、`serialization.py` —— **2.58 里 `_private/` 下的一些文件挪到了这里** |
| `_private/` | 内部实现：worker、node、GCS 客户端、runtime_env 打包、服务发现；**`ray_constants.py`（649 行）仍然在这里** |
| `core/` | CoreWorker 相关的 Python 绑定层 |
| `data/` `train/` `tune/` `serve/` `air/` | 上层 AI 库（第 12–15 章的对象） |
| `dashboard/` | Dashboard 后端（第 11 章） |
| `autoscaler/` | 自动扩缩容（第 17 章） |
| `job_submission/` | Jobs API（第 4、33 章） |
| `runtime_env/` | runtime_env 的插件体系（附录 F） |
| `llm/` | LLM 相关（第 37 章） |
| `util/` | **对外工具 + 注解体系**：`annotations.py`（稳定性标注）、`metrics.py`、`state/`（State API） |
| `experimental/` | 实验性 API（**查不到东西时，务必回来这里翻一遍**，见 §39.5） |
| `tests/` | 单元测试（与 `release/` 的端到端测试互补） |
| `widgets/` | Jupyter 富文本展示（第 33 章 §33.8） |

### 八个"最常查"的关键文件

这张表是这一章最实用的部分。**把它存下来。**

| 你想知道 | 去哪 | 怎么查 |
|---|---|---|
| 一个**配置项**的默认值 | `src/ray/common/ray_config_def.h` | `grep -n "<名字>" src/ray/common/ray_config_def.h` |
| 一个**环境变量**的名字 | **两个来源**：C++ 系统配置项看 `src/ray/common/ray_config_def.h`；Python 侧环境变量看 `python/ray/_private/ray_constants.py`（`RAY_ADDRESS` / `RAY_NAMESPACE` / `RAY_TMPDIR` / `RAY_DEDUP_LOGS` 这类**不在** `ray_config_def.h` 里） | 见 §39.4 Q3 |
| 一个 Python 侧**常量** | `python/ray/_private/ray_constants.py`（**649 行，主要常量都在这**）；`python/ray/_common/ray_constants.py` 只有 **5 行**、3 个常量 | `grep -n "<名字>" python/ray/_private/ray_constants.py` |
| `ray.init()` 的**合法参数** | `python/ray/_common/ray_option_utils.py` | `grep -n "<参数名>" ...` |
| 一个 API 的**稳定性等级** | `python/ray/util/annotations.py` + 各 API 的装饰器 | `grep -n -B3 "def <名字>" <文件>` |
| 一个 API **是否已弃用** | 装饰器 `@Deprecated(...)` 或函数体里的 `deprecation_warning(` | `grep -rn "@Deprecated" <文件>` |
| **错误信息**的原文 | 搜信息里的一小段字符串 | `grep -rn "collate_fn cannot be used" python/` |
| 某特性**端到端**怎么测 | `release/<特性>_tests/` | `ls release/` |

> ⚠️ **2.58 有一处会让老经验失效的路径漂移 —— 但它只漂了一半，这才是最坑的地方。**
>
> 很多网上的帖子和旧代码会告诉你：常量在
> `python/ray/_private/ray_constants.py`、选项校验在
> `python/ray/_private/ray_option_utils.py`。**在 2.58 上：**
>
> | 文件 | 2.58 的实际位置 |
> |---|---|
> | `ray_option_utils.py` | ✅ **挪了** —— 在 `python/ray/_common/`，`_private/` 下**已不存在** |
> | `ray_constants.py` | ❌ **没挪** —— **主要常量仍在 `python/ray/_private/`**（649 行）。`_common/` 下那个同名文件**只有 5 行**、3 个常量（`DEFAULT_MAX_CONCURRENCY_ASYNC` / `LOGGING_ROTATE_BYTES` / `LOGGING_ROTATE_BACKUP_COUNT`） |
>
> **所以"`_private/` 下的文件挪到 `_common/` 了"这个说法只对一半。**
> 两个坑叠在一起：
>
> 1. `_private/` 目录**依然存在**（里面还有很多别的东西），所以你不会觉得路径可疑，
>    只会觉得"**这个文件不在那儿**"，然后可能得出错误结论；
> 2. 而且**同名文件在两个目录下都可能有** —— 光看"文件存在"是不够的，
>    **还要看它多大、里面有几个东西**。`_common/ray_constants.py` 存在，
>    但它**不是**你要找的那个常量表。
>
> **正确的做法是"先定位、再查内容"**：
>
> ```bash
> # 不要猜目录，让 grep 告诉你它在哪
> grep -rn "OBJECT_STORE_MINIMUM_MEMORY_BYTES" python/ray/ --include=*.py
> ```
>
> 这一条也是 §39.5 要治的病 —— 只不过它多教了一层：
> **"文件存在"不等于"内容在那儿"。**

---

## 39.4 五个问题走读法

"读源码"是个含糊的动词。把它拆成**五个可以用命令回答的问题**，
它才变成一个能执行的动作。下面每个问题都给出**命令 + 预期输出形状 + 判读规则**。

### Q1：这个 API 还在吗？

**命令**（以 `ray.get_gpu_ids` 为例）：

```bash
RAYDIR=$(python -c "import ray, os; print(os.path.dirname(ray.__file__))")
grep -rn "def get_gpu_ids" "$RAYDIR" | head
```

**预期输出形状**：`<文件路径>:<行号>:def get_gpu_ids(...)`。

**判读规则**：

* **有命中** → 存在。但**还要看它是不是在 `experimental/` 或标了 `alpha`**
  （决定你敢不敢用在生产）。
* **无命中** → ⚠️ **不能立刻说"不存在"**。跳到 §39.5，
  你还需要排除"名字变了""换了个模块""我只搜了子树"三种可能。

**加强版**：确认它**对外导出**了没有。搜到 `def` 只说明函数存在，
不说明它在你 `import ray` 之后能用：

```bash
grep -rn "get_gpu_ids" "$RAYDIR/__init__.py"
```

### Q2：它的默认值是多少？

**命令**：

```bash
# 配置项（C++ 定义，带注释）
grep -n -B8 "RAY_CONFIG(float, object_spilling_threshold" src/ray/common/ray_config_def.h

# Python 函数的默认参数 —— ⚠️ 注意这里**没有** `^` 锚点，原因见下方
grep -n -A20 "def iter_torch_batches" python/ray/data/iterator.py \
  | grep -E "batch_size|dtypes|collate_fn"
```

> ⚠️ **不要写 `^def <名字>` —— 这一条值得单独记。**
> Ray 里**大量 API 是类方法**（`Dataset.iter_torch_batches`、
> `Dataset.write_delta` …），它们在源码里是**缩进 4 空格**的：
>
> ```
> 383:    def iter_torch_batches(      ← 前面有 4 个空格
> ```
>
> 所以 `grep "^def iter_torch_batches"` 会**零命中**，
> 而零命中长得就像"没有这个方法"（§39.5 案例三的那个坑）。
> **`grep "^def"` 只对模块级函数有效** —— 例如 `read_mongo`
> 恰好是模块级函数，所以 `^def read_mongo` 能用，**但那是运气，不是规则**。
>
> 稳妥的写法是**去掉锚点**，或按"缩进 + def"写：
>
> ```bash
> grep -rn "def iter_torch_batches" python/ray/data/iterator.py
> ```

**预期输出形状**：配置项是 `RAY_CONFIG(<类型>, <名字>, <默认值>)`；
函数是签名里的 `参数=默认值`。

**判读规则**：

* 配置项的默认值**以 `ray_config_def.h` 为准** ——
  文档页上写的可能是"推荐值"或"历史值"。
* 注意 **`0` / `""` / `false` 这类"空默认值"往往意味着"运行时另有回退逻辑"**，
  真正生效的值要去读读它的代码，别停在定义处。

> 💡 **一个真实例子**（本书第六轮踩过）：
> 文档写 "默认 0.95"，源码写 `0.8`。**两个都"有出处"，
> 但只有源码是你运行时真正得到的。** 这类不一致本身值得记一笔。

### Q3：这个配置项/环境变量叫什么？

**这是最高频、也最容易静默出错的一类核查。**

铁律：**Ray 的配置名就是环境变量名。名字写错不会报错，只会静默无效。**

**命令**：

```bash
# 先在定义处确认它存在，并拿到准确拼写
grep -n "object_spilling" src/ray/common/ray_config_def.h
```

**判读规则**：

* 定义处**有** → 名字对了。环境变量的形式是 **`RAY_` + 配置名，
 大小写原样保留、不做任何变换**。

  这条规则的出处是宏本身（`src/ray/common/ray_config.h`）：

  ```cpp
  #define RAY_CONFIG(type, name, default_value)                       \
   private:                                                           \
    type name##_ = ReadEnv<type>("RAY_" #name, #type, default_value); \
   public:                                                            \
    inline type &name() { return name##_; }
  ```

  `"RAY_" #name` 是**字符串拼接**，没有任何大小写转换。头文件里的注释也写着
  *"Configs defined in this way can be overridden by setting the env variable
  `RAY_{name}=value` where `{name}` is the variable name."*

  所以：

  | 配置名 | 环境变量 |
  |---|---|
  | `object_spilling_threshold` | `RAY_object_spilling_threshold` |
  | `free_objects_period_milliseconds` | `RAY_free_objects_period_milliseconds` |
  | **`AUTH_MODE`** | **`RAY_AUTH_MODE`** |
  | **`USE_TLS`** | **`RAY_USE_TLS`** |

  ⚠️ **后两行是关键 —— 别把"大多数配置名是小写"当成规则。**
  配置名里**确实有本来就是大写的**（`AUTH_MODE` / `USE_TLS` /
  `TLS_SERVER_CERT` / `ENABLE_K8S_TOKEN_AUTH` …），它们的环境变量就长这样。
  **规则是"前缀 + 原样"，不是"前缀 + 大写"、也不是"前缀 + 小写"。**
  想确认某一个，回宏的定义看 —— 别看别人的博客。
* 定义处**没有** → **这个名字不存在**。此时最有价值的动作是
  **在同一个文件里搜它的"亲戚"**（同前缀/同后缀），
  你往往能发现真名：

```bash
grep -n "free_objects" src/ray/common/ray_config_def.h
# 真名是 free_objects_period_milliseconds，不是 free_objects_period_ms
```

> 💡 **这正是本书第五轮的头号发现**：`free_objects_period_ms`
> 这个名字**不存在**，但它在书里活了三轮 ——
> 因为它"看起来就该叫这个"。
> **在名字这件事上,直觉是所有证据里最差的一种。**

### Q4：这个行为是哪个版本变的？

**命令**（跨版本对比，见 §39.2 办法三）：

```bash
for v in 2.55.0 2.56.0 2.57.0 2.58.0; do
  n=$(curl -s "https://raw.githubusercontent.com/ray-project/ray/ray-$v/python/ray/data/dataset.py" \
      | grep -c "def write_delta")
  echo "$v: $n"
done
```

**预期输出形状**：`2.55.0: 0` / … / `2.58.0: 1`。
**第一次从 0 变成 1 的那个版本，就是这个能力的落地版本。**

**判读规则**：

* 这是**二分查找**。版本多时先测首尾，再往中间夹。
* ⚠️ **务必带对照组**（见 §39.2 的提醒）：
  如果**所有版本**都返回 0，八成是文件没取到，而不是"这个功能一直没有"。
* 对于 PR 号、issue 号，GitHub 的 API 有**未认证速率限制**
  （每小时 60 次，很容易打满）。打满之后 `curl` 会返回错误页，
  **不要把它当成"查不到"** —— 等一会儿，或改用网页。

### Q5：我能不能证伪这个结论？

这是**唯一一个真正重要的问题**。前四个问题的答案，都要经过它才算数。

**方法**：把结论改写成一条**命令**，使"结论为假"时命令会给出**不同的输出**。

| 结论 | 改写成哪条命令 | 什么结果会推翻它 |
|---|---|---|
| 「Ray 没有租户配额」 | `grep -rin "quota" python/ray/` | 出现任何"按 namespace 分配 CPU/GPU 上限"的代码 |
| 「`use_datasource_v2` 只在 `read_parquet` 被读取」 | `grep -rn "use_datasource_v2" python/ray/data/` | 出现定义/docstring 之外的第二个**读取点** |
| 「Kinesis 连接器不存在」 | `ls python/ray/data/_internal/datasource/ \| grep -i kinesis` | 列出 `kinesis_datasource.py` 之类 |
| 「RLlib 源码在仓库根」 | `tar tzvf ray.tgz \| grep "python/ray/rllib"` | 该路径**有子文件**（而不是软链） |

> **判据不是"我搜到了吗"，而是"如果我错了，我会看到什么"。**
> 写不出后半句，就说明这条结论还没被验证过。

---

## 39.5 证据的三个等级：怎么证明"不存在"

这一节是整章的核心，也是本书用**自己的失误**换来的。

### 三个等级

| 等级 | 形态 | 强度 | 能不能支撑"不存在" |
|---|---|---|---|
| **L1 印象** | 「我记得它不在那儿」「一般都不支持」 | **不算证据** | ❌ 不能 |
| **L2 命中** | 「我在 X 里搜了一下，没搜到」 | 弱 | ❌ **不能** |
| **L3 穷尽** | 「我搜了**全树**，并给出了**完整命中列表**」 | 强 | ✅ 能（且需附命令） |

**L2 是陷阱所在**：它感觉像是做了功课，实际上只证明了一件事 ——
**你在那一个地方没找到。** 而"那一个地方"可能是错的路径、
错的名字、或一棵空树。

### 三个真实案例（都是本书的伤）

**案例一：`ConsistentHashRouter` 到底在不在？（第七轮）**

本书作者当时要判断这一个类在不在 Ray 2.58 里，
依据是：「我列了 `python/ray/serve/_private/request_router/` 目录，里面没有它。」
于是写下「**它不在 Ray 2.58.0 里**」。

**它存在** —— 在 `python/ray/serve/experimental/` 下。
更难看的是：**本书第 15 章上一轮就已经把它写进表里了** ——
这本书用自己的另一章，证伪了自己这一章。

* **错在哪**：把"我在一个目录里没找到"（L2）当成了"它不存在"（L3）。
* **正确的做法**：全树搜类名，并看**所有**命中：

```bash
grep -rn "class ConsistentHashRouter" python/
# 会命中 experimental/ 下的那一个 —— 加上"我查过的目录列表"，才是 L3
```

**案例二：证据的**路径**本身过期了，而且"过期"这件事只发生了一半（第八轮）**

本书第 17 章用"三个文件里 grep `quota` 零命中"来论证
「**Ray 没有租户配额机制**」。结论是**对的**，
但它引的三个路径里，`python/ray/_private/ray_option_utils.py` **在 2.58 已经不存在了**
（实际在 `python/ray/_common/ray_option_utils.py`）。

**结论对，证据错** —— 这类问题最隐蔽，因为结论经得起检验，
于是没人会去复查证据。但它有一个真实的后果：
**下一个想复用这条证据的人，会照着一个不存在的路径去搜，然后怀疑结论。**

* **修法**：证据里写路径时，**先验证路径存在**；
  或者把证据写成"**全 `python/ray/` 检索**"这种**不依赖具体路径**的形式。

**而这个案例还有下半段 —— 它才是真正贵的部分。**

本轮的作者据此得出一条经验："2.58 里 `_private/` 下的一些文件挪到 `_common/` 了"，
并顺手把 `ray_constants.py` 也算了进去（它"看起来"应该跟 `ray_option_utils.py` 一起搬）。
**错了一半**：`ray_constants.py`（649 行，主要常量都在里面）**根本没挪**，
仍在 `python/ray/_private/` 下；
`python/ray/_common/ray_constants.py` **确实存在**，但只有 **5 行**、3 个常量。

* **错在哪**：把"**A 文件挪了**"推广成了"**这一批文件都挪了**";
  更根本的是 —— **用"文件存在"当成了"内容在那儿"。**
  同名文件在两个目录下都存在时，**存在性检查给不出答案**。
* **这一类错误的通用形态**：*我证实了一个特例，然后把它当成了规则。*
  它和前三条是同源的，但多了一层伪装：**它每一步都有证据。**
* **防身办法**：**别只问"文件在不在"，要问"我要的那个东西在不在这个文件里"**。
  最省事的写法是**不猜路径，直接全树搜符号**：

  ```bash
  # 不要猜它在 _common 还是 _private —— 让 grep 告诉你
  grep -rn "OBJECT_STORE_MINIMUM_MEMORY_BYTES" python/ray/ --include=*.py
  ```

  顺带的判据：**看文件多大**（`wc -l`）。一个 5 行的文件撑不起"常量表的所在地"。

**案例三：核查工具本身会骗你（第八轮，作者自己）**

还是在第八轮，作者为了核实「`python/ray/rllib` 是软链」这一条，
跑了：

```bash
tar tzvf ray.tgz | grep -E "python/ray/rllib$"
# 无输出 → 差点得出「没有这个条目，那条说法是错的」
```

**那条说法是对的。** 错的是命令：`tzvf`（verbose）的输出长这样 ——

```
lrwxrwxrwx root/root 0 2026-08-22 09:39 ray-ray-2.58.0/python/ray/rllib -> ../../rllib
```

**行尾有 ` -> ../../rllib`**，所以我那个 `$` 锚定的正则**永远匹配不上**。
换成不带锚点的 `grep "python/ray/rllib"`，一行命中。

* **教训是新的，而且比前两条更狠**：前两条是"证据不够"，
  这一条是**"工具用错了，却给出一个看起来很像结论的输出"** ——
  空输出。**空输出最像"不存在"。**
* **防身办法**：**永远带对照组**。你如果在核查"X 不存在"，
  就同一条命令、同一批输入里，**再查一个你确信存在的东西**。
  如果连对照组都是空的，那说明**是命令错了，不是 X 不存在**。

### 一条可复用的"证否"模板

要断言「Ray 里没有 X」，按这个顺序做完，再把**过程和输出**一起写出来：

1. **定范围**：说清楚你搜的是**哪棵树、哪个版本**
   （例：`ray-2.58.0` 完整源码树，含 `src/`）。
2. **定形式**：X 可能有多种形态 —— 函数名、类名、文件名、配置项、环境变量。
   **每一种都要搜**。
3. **全树搜，不猜路径**：
   ```bash
   grep -rin "<关键词>" . --include=*.py --include=*.h --include=*.cc
   ```
4. **带对照组**（案例三）：同一条命令里确认一个**已知存在**的符号能命中。
5. **列命中**：把**所有**命中列出来，并逐条解释为什么它们**不构成** X。
   （第 17 章的 `quota` 就是这么做的：命中的都是 cgroup CPU 配额、
   `memory` 参数的 docstring 措辞、沙箱/Job 模块的内部量。）
6. **写清边界**：如果某处你不确定，写「**未确认**」，不要写「不存在」。
   本书的立场是：**查不到就写"未确认"，这句话本身就是一种诚实。**

> 📌 第 5 步是**最关键也最容易被跳过**的一步。
> "零命中"是稀缺的；**更常见的是"有命中，但都不是我要找的东西"**。
> 这种情况下不做第 5 步，你既不能确认也不能否认 —— 而人往往会把
> "有命中但看不懂"误当成"确认了"。

---

## 39.6 稳定性与弃用：这个 API 我还能用多久

判断一个 API 能不能用，要回答两个独立的问题：
**它现在是什么等级**（§39.6.1）和**它是不是已经在被淘汰**（§39.6.2）。

[第 20 章 §20.9.1](ray教程-20-现状与未来方向.md) 已经系统讲过 Ray 的
弃用策略与三道扫描网，**这里不重写**，只补"**怎么在源码里一眼看出来**"。

### 39.6.1 稳定性三级：看装饰器

`python/ray/util/annotations.py` 定义了三个装饰器：
`PublicAPI`、`DeveloperAPI`、`Deprecated`。
其中 `PublicAPI` 的签名是 `PublicAPI(*, stability="stable", api_group="Others")` ——
也就是说 **`stability` 的默认值是 `"stable"`**。

由此得到一条**非常有用、但很容易搞错的判读规则**：

| 源码里长这样 | 含义 |
|---|---|
| `@PublicAPI(stability="alpha")` | **alpha** —— 可以变、可以删，别用在没人盯着的地方 |
| `@PublicAPI(stability="beta")` | **beta** —— 公开但仍在调整 |
| `@PublicAPI(stability="stable")` | **stable** —— 承诺跨小版本向后兼容 |
| **裸 `@PublicAPI`**（没写 stability） | **等价于 `stable`** —— 因为默认值就是 `"stable"` |
| `@DeveloperAPI` | 给二次开发者，**没有兼容承诺** |
| `@Deprecated(...)` | 已弃用，见下 |

> ⚠️ **第四行是反直觉的。** 我们习惯性地认为"没标注 = 不保证"，
> 但在 Ray 里**裸 `@PublicAPI` 恰恰是"stable"**。
>
> 💡 **这本书自己在第八轮就踩了这一条**：第 12 章曾把
> `read_sql` / `read_mongo` / `from_huggingface` 三个 reader 都标成
> "beta（**未确认**是否已转 stable）"。回源码核对后发现：
> 前两个是 `@PublicAPI(stability="alpha")`（**alpha**，不是 beta），
> 而第三个是**裸 `@PublicAPI`** —— **即 stable**。
> 三处**全部标错**，而且错的方向各不相同。
>
> 这个例子值得记住的地方在于：它们**不是"没查"，而是"查了但没查到点上"** ——
> 前两个低估了风险等级，第三个高估了不确定性。
> **稳定性标注这件事，猜是猜不对的。**

**实操**：

```bash
# 看一个函数头顶三行，就知道它的稳定性
grep -n -B4 "^def read_mongo" python/ray/data/read_api.py
# @PublicAPI(stability="alpha")
# def read_mongo(
```

**一个高频困惑点**：`stability` 说"会不会变"，
**不**说"能不能用"。`alpha` 的 API **完全可以用** ——
只是你要接受它在下个小版本可能变。**别把 alpha 当成"禁止使用"。**

### 39.6.2 弃用：两种形态，行为完全不同

源码里有**两套**弃用机制，**运行时行为不一样**（第 20 章 §20.9.1 有完整对照）：

**A 套 —— `deprecation_warning()`**，实现在
`python/ray/_common/deprecation.py`。它**走 logger**：

```python
def deprecation_warning(old, new=None, *, help=None, error=None, stacklevel=2):
    """Warns (via the `logger` object) or throws a deprecation warning/error."""
```

关键在 docstring 那句 **"via the `logger` object"** ——
走的是 logging，所以 **`-W error::DeprecationWarning` 抓不到它**。

**B 套 —— `@Deprecated(...)`**，实现在 `python/ray/util/annotations.py`：

```python
def Deprecated(*, message: str = ..., warning: bool = False): ...
```

它**默认 `warning=False`**，此时**只改 docstring、运行时不告警**；
只有显式写 `warning=True` 才会真的
`warnings.warn(warning_message, RayDeprecationWarning)`。

**所以在源码里判读弃用，要看两层**：

```bash
# 一层：函数头顶
grep -n -B5 "def zip" python/ray/data/dataset.py
#   @Deprecated(
#       message="`Dataset.zip` is deprecated and will be removed in Ray 2.64. ...",
#       warning=True,        ← 看这一行！
#   )
#   def zip(self, *other: "Dataset") -> "Dataset":
```

```bash
# 两层：函数体里有没有 deprecation_warning(...)
grep -rn "deprecation_warning(" python/ray/data/
```

**判读规则**：

* `@Deprecated(...)` 带 `warning=True` → **每次调用都会告警**。
* `@Deprecated(...)` **不带** `warning=` → **只改文档，运行时不告警**。
* `deprecation_warning(...)` 出现在函数体 → 走 logger，**静默得多**。

> 💡 **两种形态各举一个真例子**（第 20 章 §20.9.1 的表就是从这里来的）：
> `Dataset.add_column` 是 `@Deprecated(message="Use \`with_column\` API instead")` ——
> **没带 `warning=`**，所以运行时不吭声；
> 而 `Dataset.zip` 显式写了 `warning=True`，每次调用都会抛
> `RayDeprecationWarning`。**两个都在 `dataset.py` 里，行为完全不同。**
>
> 所以「**文档说会移除**」和「**运行时提醒你**」是两件事 ——
> 升级前只靠运行时告警，会漏掉前一种。

> 💡 **这对你意味着什么**：升级 Ray 时，**"运行时没报警"不能推出"没用到弃用 API"**。
> 两套机制里至少有一套默认不吭声。这就是为什么第 20 章要给出**三道扫描网** ——
> 其中一道是**静态扫源码**（`grep -rn "deprecation_warning("`）。
> 完整的扫描网与"什么算公开 API"的五条判据，见 **§20.9.1**。

---

## 39.7 时效：怎么知道"什么变了"

Ray 迭代很快（第 1 章 §1.4 有 2.44 → 2.58 的逐版本变化表）。
核查的最后一块拼图是：**搞清楚你要查的东西属于哪个版本。**

### 一个反直觉的事实：仓库里没有 changelog

你可能会想"去仓库里找 changelog"。**别找了，它不在那儿。**
这不是猜测，是可以自己复核的：

```bash
cd ray-ray-2.58.0
tar tzf ../ray.tgz | grep -icE "release[-_]?notes|changelog|whatsnew|whats-new"
# 0
```

**零命中。**（先做个对照组，确认命令没问题：
`tar tzf ../ray.tgz | grep -ic "index.rst"` 应给出 14。）

> 📌 **穷尽性还差最后一步：把命中解释掉。** 换 11 种命名模式
> （`changelog` / `CHANGELOG` / `whatsnew` / `whats-new` / `whats_new` /
> `release_notes` / `release-notes` / `releasenotes` / `NEWS` / `HISTORY` /
> `changes.rst`）交叉搜，结论不变。
> 而全表里唯一含 `notes` 的是
> `release/release_logs/1.2.0/notes.txt` —— 打开看是 **2021 年的一份性能基准
> 输出 + sanity check 记录**，**不是变更日志**。
>
> 这一步（§39.5 模板的第 5 步）**不能省**：只有把"看起来像的"逐条排除掉，
> "零命中"才算 L3 级的证据。

**这意味着两件事**：

1. **别在仓库里浪费时间找变更日志** —— 它不在。
2. **"什么变了"必须去仓库之外查**，见下。

> 📌 注意这个结论的形状：它不是"我没找到"（L2），
> 而是"**我在完整文件列表里按四种命名约定搜过，零命中，且对照组正常**"（L3）。
> **同样的动作，说法不同，可信度完全不同 —— 这一章要教的就是这个区别。**

### 该去哪查"什么变了"

| 来源 | 能回答什么 | 注意 |
|---|---|---|
| **GitHub Releases 页** | 每个版本的发布说明 | 这是**权威**的"什么变了"；仓库里没有对应文件 |
| **docs.ray.io 的版本页** | 官方文档口径 | ⚠️ **先确认版本选择器**指向你要的版本，别读成 master |
| **PyPI 的上传时间** | **这个版本什么时候发的** | 本书 §1.4 用这个办法定过版本日期（版本号本身不含日期） |
| **`raw.githubusercontent.com` 跨 tag 对比** | 某个具体符号**从哪个版本开始有** | §39.4 Q4；**必须带对照组** |
| **`release/` 目录** | 该特性的**端到端**测试怎么跑 | 最能反映"是否真的可用" |
| **PR / issue 号** | 变更的动机与讨论 | ⚠️ GitHub API 未认证时限速 60 次/小时，打满后返回错误页 |

### 一条纪律：版本号 ≠ 日期

「Ray 2.58」不等于"某年某月"，**版本号里没有时间信息**。
要谈"什么时候"，就去拿一个能核实的时间戳（PyPI 上传时间 / GitHub Release 时间），
并把它写出来。本书的基线写作 **「Ray 2.58.0（2026-08-23）」** ——
**那个括号里的日期是查来的，不是推的。**

---

## 39.8 一份可复用的核查清单

拿到**任何**一个关于 Ray 的结论（本书的、issue 里的、同事说的），按这七步过一遍：

| # | 步骤 | 动作 | 不通过时 |
|---|---|---|---|
| 1 | **定版本** | 写下结论针对的版本；确认它和你环境一致 | 版本不符 → 结论可能无效，重来 |
| 2 | **找一手来源** | 优先：源码 > release note > 官方文档 > 博客/帖子 | 只有二手来源 → 降级为"未确认" |
| 3 | **写否决条件** | 把结论改写成一条命令，说清"什么输出会推翻它"（Q5） | 写不出 → **这条结论还没被验证过** |
| 4 | **全树检索** | 不猜路径、不猜名字；多种形态（函数/类/文件/配置/环境变量）都搜 | 只在一处搜 → 只有 L2，不能下"不存在" |
| 5 | **带对照组** | 同一条命令里确认一个已知存在的符号 | 对照组也空 → **命令错了**，不是对象不存在 |
| 6 | **解释全部命中** | 有命中时逐条说明为什么不构成结论 | 解释不了 → 既不能确认也不能否认 |
| 7 | **写边界** | 不确定的明确写"未确认"，并写清**卡在哪一步** | —— |

> 第 3 步和第 5 步是这份清单里最容易被跳过、也最值钱的两步。
> 它们的作用是**让错误在你自己手上暴露**，而不是等到别人踩上去。

> ⚠️ **关于第 5 步的一个补充：对照组必须「隔离变量」。**
> 这是第八轮**作者自己踩出来的**一个新变体 ——
> 他想验证 mini-ray 的「旧写法 actor 放置组」是否可用，写了这样一个对照：
>
> 1. 先用**新写法**建一个 actor；
> 2. 再用**旧写法**建一个 actor，然后看它能不能起来。
>
> 结果是旧写法**挂了** —— 看起来是个真 bug。
> 但真相是：**那个放置组只有一个 bundle，已经被第 1 步的 actor 占满了** ——
> 旧写法在**等一个永远不会空的 bundle**。
> 换成「每种写法各用一个**全新**的放置组」之后，**三种写法全部正常**。
>
> **教训**：对照组不只是"要有一个已知能成功的样本"，
> 更要保证**样本之间不共享状态**。
> 一个和被测对象**抢同一个资源**的对照，不是在对照，是在**制造假阳性**。
>
> 这条和 §39.5 案例三是**同一个病的两面**：
> 案例三是对照组**缺席**（空输出被当成"不存在"），
> 这一条是对照组**在场但被污染**（把"资源被占"读成了"功能坏了"）。
> 两者的共同点：**你以为你在测量 A，其实你测到的是 B。**

### 一个反直觉的推论

这套流程会让你发现：**"确认一个东西存在"比"确认它不存在"容易得多。**

* 要证明**存在**：一次命中就够了（但最好再确认一次它是**导出的**、**什么等级**）。
* 要证明**不存在**：需要穷尽 + 对照组 + 解释全部命中。

所以**在写作和沟通里，应该对"不存在"类断言施加更高的证据标准**。
本书在第七、第八两轮里最贵的几个错，**全部是"不存在"类断言**。
这不是巧合：**否定判断天然更容易说出口，也更难被证伪。**

---

## 39.9 靶场：用 mini-ray 练这套方法

上面所有命令都需要一棵 Ray 源码树。**如果你想先练手（或者手边没有 Ray），
可以用本教程配套的 `mini-ray/` 当靶场。**

它有两个别处没有的好处：**你能改动它、能看到全部实现**（1.1 万行，没有编译产物），
而且**它的 `README.md` 里有一份"与真实 Ray 的语义差异"表** ——
那是一份现成的、**故意留下的**核查对象。

### 练习一（对应 Q1：它到底实现了没有）

`mini-ray/README.md` 的能力清单里声明了「**`max_restarts`**」等 actor 生命周期 API。
**别信这份清单 —— 去源码里验一遍：**

```bash
cd mini-ray
grep -rn "def list_actors\|def get_actor\|def kill" miniray/ | head
grep -n "list_actors\|get_actor\|\"kill\"" miniray/__init__.py    # 是不是真的导出了？
```

**判读**：这一条**验下来清单是对的** —— 三个函数都实现了，也都在
`miniray/__init__.py` 的导出列表里。（**验证的结果是"通过"也是结果**，
`__init__.py` 里那份 `__all__` 就是"存在"与"你能用"的分界线。）

> 💡 **这个练习不是编的 —— 第八轮就是在 mini-ray 上抓到了一个真例子。**
> 附录 A / B 都写着 `miniray.util.state.*` 是可用的「Ray 兼容路径」，
> 实测却是
> `AttributeError: module 'miniray.util' has no attribute 'state'` ——
> `miniray/util/__init__.py` **从来没有 import 过 `state`**
> （讽刺的是，**它自己的 docstring 里就写着** `from miniray.util import state`）。
>
> 更值得学的是**为什么测试没拦住**：`tests/test_util.py` 里那条
> 「util 命名空间可导入」的测试**一直是绿的** —— 因为它写的是
> `from miniray.util.state import list_actors`，
> **这条路径绕开了 `__init__.py`**，所以无论 `__init__.py` 有没有导入它都能过。
>
> **两句话的差别**：
>
> | 写法 | 走不走 `util/__init__.py` |
> |---|---|
> | `from miniray.util.state import list_actors` | ❌ **不走** —— 直接按文件路径导入 |
> | `miniray.util.state.list_actors()` | ✅ **走** —— 先取 `miniray.util` 这个**已导入的包对象**，再取它的属性 |
>
> 也就是说：**"能 import"和"能当属性访问"是两条不同的路径** ——
> 而**读者从真实 Ray 迁过来时用的是第二条**（`ray.util.state.list_actors()`）。
> 这正是 §39.5 那个病的又一个变体：**测了一条路径，就以为整个能力都没问题。**

> 💡 **顺着往下问一层会更有意思**：mini-ray 的 `max_restarts` 与真实 Ray
> **语义不同** —— 真实 Ray 的 actor 资源是"没写资源就终身占 0 CPU、
> **每次方法调用**占 1 CPU"，而 mini-ray **一律终身持有**，且不为方法调用扣 CPU
> （其 README 的「语义差异」表里写着）。**同一行文档，在两套实现里不是同一件事** ——
> 这正是第 10 章 §10.2 里那条 `retry_exceptions` 为什么必须区分
> "类级"与"方法级"的原因。

### 练习二（对应 Q2：默认值）

`mini-ray` 声称它的重试语义和真实 Ray **不同**（见其 README 的"语义差异"表：
真实 Ray **默认不重试**应用异常，mini-ray 对任何失败都重试）。
去源码里把这个默认值找出来：

```bash
grep -rn "max_retries" miniray/*.py | head -20
```

**判读**：找到默认值之后，再问一句
「**这个默认值在哪些路径上生效**」—— 这正是本书第七轮的教训
（同一个能力有多条实现路径）。

### 练习三（对应 Q5：否决一个结论）

本书第 22 章附录 B 给了一份「**已知限制：与真实 Ray 的完整差异**」清单。
**挑一条，试着否决它** ——
即：在 `mini-ray/` 的源码里找出它**其实已经实现/行为一致**的证据。

如果你找到了一条，那说明**文档和代码有一处不一致** ——
这正是本书第六、七两轮各抓到 10 个和 9 个缺陷的那类问题。
**你的第一个 PR 可以就是这个。**

---

## 39.10 本书的信息源清单

这一章最后给一张表：**这本书里的结论都是从哪来的。**
你可以按图索骥地复核任何一条。

| 结论类型 | 一手来源 | 可复核的形式 |
|---|---|---|
| API 名字 / 参数 / 默认值 | 源码：`python/ray/` | `grep -n -A20 "def <名字>"`（**不要锚 `^`** —— 大量 API 是缩进的类方法，`^def` 会零命中，见 §39.4 Q2） |
| 配置项 | `src/ray/common/ray_config_def.h` | `grep -n "<名字>"`（**C++ 配置项的唯一权威**） |
| Python 侧环境变量（`RAY_ADDRESS` 等） | `python/ray/_private/ray_constants.py` | `grep -n "<名字>"`（**这些不在 `ray_config_def.h` 里**） |
| Python 常量 | `python/ray/_private/ray_constants.py`（`_common/` 下的是 5 行小文件，别拿错） | `grep -n "<名字>"` |
| 稳定性等级 | `python/ray/util/annotations.py` + 装饰器 | `grep -n -B4 "^def <名字>"` |
| 弃用行为 | `python/ray/_common/deprecation.py`、`@Deprecated` | 看 `warning=` 取值 |
| 平台支持 | `ci/`、`get_wheel_filename()` | `grep -rn "get_wheel_filename" python/` |
| 版本时间 | PyPI 上传时间 / GitHub Release | 取时间戳 |
| 端到端可用性 | `release/<特性>_tests/` | 直接读测试用例 |
| 第三方引擎事实 | 引擎自己的仓库与文档 | ⚠️ **不是 Ray 的源码**，要单独查 |
| mini-ray 的行为 | `mini-ray/` 源码 | 直接读 + 跑测试 |

> ⚠️ **最后两行是本书最容易出错的地方，也是你要格外警惕的地方。**
> 只要结论涉及 **vLLM / SGLang / KubeRay / PyTorch** 这类外部项目，
> **Ray 的源码不是它的证据** —— 版本的漂移速度甚至比 Ray 更快。
> 本书第 37 章 §37.15 因此专门给了"自己复核的 grep 命令"。

---

## 39.11 本章小结

* **这一章不含新的 Ray 知识**，它是一把尺子：教你否决前面 38 章。
* **三种拿源码的办法**：pip 装的那个（能查 Python 侧、版本必然一致）、
  release tarball（**含 `src/` C++ 侧**，195 MB）、单文件 raw（跨版本对比最快）。
  ⚠️ 两个坑：**软链不会被解压成内容**（`python/ray/rllib -> ../../rllib`）、
  **你读的文档可能是 master 而不是你的版本**。
* **仓库地图**：`python/`（Python 侧）、`src/`（C++ 侧）、`rllib/`（2.58 起在**根目录**）、
  `release/`（端到端测试，被低估）、`ci/`（平台矩阵）、`doc/`（文档源）。
  八个最常查的文件见 §39.3 的表 —— **那张表值得存下来**。
* **五个问题走读法**：还在吗（Q1）、默认值多少（Q2）、
  配置项叫什么（Q3）、哪个版本变的（Q4）、**我能不能证伪**（Q5）。
* **证据三个等级**：印象（L1）不算证据；"我在一处没搜到"（L2）**不能支撑"不存在"**；
  只有穷尽检索 + 对照组 + 解释全部命中（L3）才可以。
* **三个真实案例**（全是本书自己的伤）：`ConsistentHashRouter` 查错目录、
  证据路径 `_private` → `_common` **只对了一半**、以及**核查命令自己出错却返回"空结果"**。
  **第三条最危险**，因为**空输出最像"不存在"** —— 防身办法是**永远带对照组**；
  第二条最隐蔽，因为它**每一步都有证据**，错在"把特例推广成规则"
  （提示：`_common/ray_constants.py` 存在，但只有 5 行 ——
  **"文件存在"不等于"内容在那儿"**）。
* **稳定性判读**：裸 `@PublicAPI` **等价于 stable**（默认值就是 `"stable"`）——
  这条反直觉。弃用有**两套机制**，其中一套（`@Deprecated` 不带 `warning=True`）
  **运行时不告警**，所以"没报警"推不出"没用弃用 API"。
  完整策略见 **§20.9.1**。
* **配置项与环境变量**：环境变量有**两个来源** —— C++ 系统配置项在
  `ray_config_def.h`（名字 = **`RAY_` + 配置名原文，大小写不变**；
  所以 `USE_TLS` → `RAY_USE_TLS`，**不是**"一律小写"），
  Python 侧环境变量（`RAY_ADDRESS` 等）在 `_private/ray_constants.py`。
  **别靠"应该长什么样"猜，回定义处抄原文。**
* **时效**：**仓库里没有 changelog**（可自己复核的零命中）。
  "什么变了"去 GitHub Releases / 文档版本页 / PyPI 时间戳查；
  **版本号不含日期**，谈时间要拿可核实的时间戳。
* **七步核查清单**（§39.8）：定版本 → 找一手来源 → **写否决条件** →
  全树检索 → **带对照组** → 解释全部命中 → 写边界。
* 一个推论：**"不存在"类断言天然更容易说出口、也更难被证伪** ——
  所以要**对它施加更高的证据标准**。

---

## 结语：这本书真的讲完了

从"Ray 是什么"（01），到 Core 机制（02–11）、AI 库（12–16）、
生产与安全（17–19）、现状与未来（20），
再到参考层（21–28），以及各轮修订追加的纵深（26、29–38），
最后是这一章的**核查方法**（39）。

如果全书只带走**五句话**：

1. **把 Ray 当编排层，不要当万能引擎。**
   计算、通信、训练、推理各有更专业的层，
   Ray 的价值在"谁在哪、起几个、挂了怎么办"——
   第 37 章把这条推到了推理场景，第 38 章把它推到了 Agent 场景；
2. **所有结论都要能落到「哪一层负责」。**
   显存 OOM 不是 Ray 的问题，调度不均不是 PyTorch 的问题，
   **Agent 并发上不去也常常不是容量问题而是阻塞问题** ——
   找错层就会白花几小时；
3. **确定性的事实要标注来源，不确定的要标注"不确定"。**
   这个生态在快速变化（vLLM 正在把 Ray 降级为放置层，见第 20 章；
   Agent 这一层的分工 2026 年还在剧烈重排，见第 38 章），
   唯一可靠的长期能力是**知道去哪儿核对** ——
   **这一章就是"去哪儿核对"的操作手册**；
4. **文档的可靠性来自交叉引用的一致性，而不是每一段单独写得有多好。**
   这本书改了八轮，最有价值的发现往往不是"新知识"，
   而是"上一轮改了正文、漏了小结"这种**两份状态没对齐** ——
   它和分布式系统里的 bug 是同一种形状（第 10 章 §10.4）；
5. **"我在某个地方没找到"永远不等于"它不存在"。**
   这是第七轮用自己的失误换来的一条，
   而**第八轮又给它的家族添了两个新成员**（证据路径会过期；
   核查工具本身会骗你，且它骗你的方式是**返回空结果**）。

### 八轮修订，留下了**四**条关于"怎么把知识写对"的教训

这本书的修订过程本身，比任何一章都更接近它的主题：
**一个由很多份状态组成的系统，怎么在持续改动中保持一致。**

**第一轮教训（第四轮）：一致性有时间维度。**
上一轮的**修正本身**也必须被下一轮验证。
第四轮把 5 处"统一"到 `free_objects_period_ms`，
三处对上了 —— **但对的是错的值**（真名是
`free_objects_period_milliseconds`，batch 默认是 100 不是 10000）。
**"改得一致"和"改得对"是两件事。**

**第二轮教训（第六轮）：测试全绿不等于没有 bug。**
前五轮都在审文档，第六轮第一次认真审 mini-ray 的**代码**，
结果在 **148 个测试全绿**的情况下**找出 10 个真实缺陷** ——
包括"GPU actor 根本创建不出来"和"生成器一失败消费端就永久挂起"。
**它只等于"没有测试覆盖到那个 bug"。**
判据不是"跑通了吗"，而是"**哪些路径一个测试都没有**"。

**第三轮教训（第七轮）：查到一半就下结论，是查错里最贵的一种。**
第七轮在写第 38 章时，认定 `ConsistentHashRouter` "不在 Ray 2.58.0 里"，
依据是"我列了 `python/ray/serve/_private/request_router/` 目录，里面没有它"。
**它是存在的** —— 在 `python/ray/serve/experimental/` 下，
而**本书第 15 章上一轮就已经把它写进表里了**：
本书自己就证伪了这句话。完整记录见第 37 章 §37.15 的 ⚠️。

**第四轮教训（第八轮）：核查这个动作本身，也会出错 —— 而且它出错时最像成功。**
第八轮为了核实"`python/ray/rllib` 是软链"，
跑了 `tar tzvf ray.tgz | grep -E "python/ray/rllib$"`，**无输出**，
差点把一条**正确的**结论判成错的。
原因很无聊：`tzvf` 的 verbose 输出在行尾带了 ` -> ../../rllib`，
那个 `$` 锚定永远匹配不上。

> 这一条比前三条更值得警惕。前三条是**证据不够**，
> 这一条是**工具用错了，却返回了一个看起来很像结论的东西** ——
> **空输出**。而"空输出"恰恰是"不存在"的标准长相。
>
> 前三轮的教训指向同一个病根：**拿到了一个局部事实，就把它当成了全局结论。**
> * "三处写法一致了" → 当成"值对了"；
> * "测试全绿了" → 当成"没有 bug"；
> * "这个目录里没有" → 当成"它不存在"。
>
> 第四轮补上了这个病的另一半：**你用来拿事实的那件工具，本身也可能是错的。**
> 所以防身手段要多一步 —— 不只是"把结论写成一条可以被否决的命令"，
> 还要**在这条命令里放一个对照组**，确认这条命令**今天、在这个输入上**确实在工作。

**对抗这四条的办法只有一个，而且四轮都一样**：
**把结论写成一条可以被别人否决的命令 —— 并且让这条命令自带一个"我知道它应该成功"的对照。**
这本书从第四轮起做的所有事情，本质上就这一件；
而**这一章，是把它从"作者的纪律"变成"读者的能力"。**

**现在去动手吧** ——
挑一个你工作里的真实问题，用第 25 章的路径把它搬到 Ray 上：
第一层（骨架）先跑通，再逐层替换。
**遇到"为什么慢"的时候，先回到第 37 章 §37.14 的症状表，
判断你在哪一层，再去调那一层的旋钮；
遇到"为什么它不往前走"的时候，先回到第 38 章 §38.2 的三层分工，
判断是阻塞还是容量 —— 然后再花钱；
遇到"这个结论对不对"的时候，回到这一章的 §39.8 七步清单。**
