仓库地址：https://github.com/hhk-png/cycle-agent

# 附录 F：Ray Client、runtime_env 与多语言

> 本附录补三个正文里被一笔带过、但实际最常撞到的洞：
> **`runtime_env` 到底怎么工作**（第 21 章 A.7 只有一张八行的表）、
> **Ray Client 到底还能不能用**（它只出现在术语表和地址表里）、
> **Java / C++ 绑定到底是什么水平**（第 01 章的架构图里只有一个方框）。
> 三者是同一句话的三个侧面：**我的代码和依赖，怎么到达集群上的那个 worker？**
> 凡是本书没能在官方文档或源码里核对的，一律标注 **未确认**，不做猜测。

---

## F.1 本章要解决的问题

先看一张表。三个主题看起来不相关，但它们回答的其实是同一个流程里的三个不同环节：

| 问题 | 由谁回答 | 正文里的现状 |
|---|---|---|
| 我的 **依赖**（pip 包、环境变量、代码目录）怎么到 worker 上？ | `runtime_env` | 第 04 章 §4.4 一行注释、第 21 章 A.7 一张表 |
| 我的 **driver** 跑在哪？怎么连上去？ | `ray.init(address=)` / Ray Client / Jobs API | 第 02 章地址表 4 行、第 04 章 §4.7 |
| 我的代码是 **Java / C++** 写的呢？ | 跨语言绑定 | 第 01 章架构图一个方框、第 06 章"缺失"清单一行 |

把它们放在一起讲，是因为它们共享同一个心智模型：

```
   你写代码的地方（Jupyter / IDE / CI / 集群上的 shell）
        │                                    │
   ① driver 在哪跑？                  ② 代码怎么过去？
   ┌────▼──────────────────┐      ┌──────▼───────────────────┐
   │ ray.init() 本地        │      │ runtime_env:             │
   │ ray.init("ray://…")    │      │  working_dir / py_modules│
   │ ray job submit         │      │  pip / conda / uv        │
   │ KubeRay RayJob         │      │  env_vars / excludes     │
   └────┬──────────────────┘      └──────┬───────────────────┘
        └──────────────┬─────────────────┘
                       ▼
   ③ 另一门语言写的 worker → 跨语言对象模型 + code_search_path
                             （Python ⇄ Java ⇄ C++ 共用对象存储）
```

**一句话总结这三件事的关系**：`runtime_env` 决定「worker 里有什么」，
driver 的位置决定「谁在指挥」，语言绑定决定「指挥的是哪门语言的 worker」。
三者都是**运维面**问题而非 API 面问题 —— 这正好解释了为什么前 20 章
几乎没提它们：正文关心"怎么写分布式程序"，它们关心"这份程序怎么被送到机器上跑起来"。

> ⚠️ **本附录的时效基线**：Ray **2.58.0**（2026-08-23）。
> `runtime_env` 的字段集合与继承语义在近几个版本里**动过**（见 F.2.3），
> Ray Client 的状态在 2024 年底发生过官方表态（见 F.3.5）。
> 上线前请在你手头的版本上验证一次。

---

## F.2 `runtime_env` 深入

### F.2.1 它解决什么问题

不用 `runtime_env` 的集群长这样：**所有人都往镜像里塞依赖**。一个镜像跑所有
job，于是它必然膨胀到十几个 GB —— 因为 A 组要 `torch==2.4`、B 组要
`torch==2.7`、C 组要一个八年前的 `numpy`，而这三个需求**在同一个 Python 环境
里无法同时满足**。

`runtime_env` 的答案是把依赖**从镜像层面下移到 job / actor / task 层面**：

| 粒度 | 写法 | 典型用途 |
|---|---|---|
| **Job 级**（整个 driver + 它派生的所有东西） | `ray.init(runtime_env={...})` | 整个项目一套依赖 |
| **Task / Actor 级** | `@ray.remote(runtime_env={...})` 或 `.options(runtime_env={...})` | 某个算子要一个特殊版本的库 |
| **Job 提交级**（Jobs API） | `submit_job(runtime_env={...})` / `--runtime-env-json` | 生产入口（第 04 章 §4.7） |

> **一句话**：`runtime_env` 是 Ray 的**依赖注入**机制。它让"基础镜像"回到
> 只装 Ray 本身 + 最通用依赖的体积，把差异化依赖留给每个 job 自己声明。
> 代价从"镜像膨胀"转移成了"每台节点上的安装时间与缓存"（见 F.2.5、F.2.6）。

**mini-ray 对照**：mini-ray **只实现了 `env_vars` 这一类**
（`miniray/execution.py` 的 `runtime_env_context`，语义是"临时改环境变量、
执行完还原"）。`pip` / `conda` / `working_dir` / `py_modules` 一律不做 ——
这是刻意的，因为它们的本质是"进程外的包管理器调用 + 文件分发"，
不是"运行时内部的机制"。详见 F.2.9。

### F.2.2 字段全表

```python
ray.init(runtime_env={
    "pip": ["requests==2.31.0", "torch"],
    "env_vars": {"MY_FLAG": "1"},
    "working_dir": "./src",
    "excludes": [".git", "*.log"],
    "config": {"setup_timeout_seconds": 600, "eager_install": True},
})
```

| 字段 | 取值形态 | 作用 | 备注 |
|---|---|---|---|
| `pip` | `List[str]` / `str`（本地 requirements.txt 路径）/ `dict` | 在 worker 里创建隔离 virtualenv 并装包 | dict 形式支持 `packages` / `pip_check` / `pip_version` / `pip_install_options` |
| `uv` | 同 `pip` 的三种形态 | 用 `uv pip` 代替 `pip` 装包（更快） | **官方标注为 alpha**。字段名是 `uv`，dict 形式里是 `uv_version` / `uv_check` / `uv_pip_install_options` |
| `conda` | `str`（环境名）/ `str`（`environment.yml` 路径）/ `dict` | 用 conda 环境 | ⚠️ 互斥是**三方**的：**`pip` / `uv` / `conda` 两两不能同时指定**（不是只有 pip↔conda）；Windows 上的官方措辞是 **beta / experimental**（代码里是一条 `logger.warning`），不是"不支持"（第 21 章 A.7） |
| `py_modules` | `List[str]`（本地目录或 zip） | 上传 Python 模块并加进 `PYTHONPATH` | 适合"只有几个包"的场景，比 `working_dir` 轻 |
| `working_dir` | 本地目录 / 本地 zip / 远端 URI（如 `s3://…zip`） | 打包整个目录，**分发到每一台节点** | 成本最高的一项，见 F.2.6 |
| `env_vars` | `Dict[str, str]` | 给 worker 注入环境变量 | **唯一被 mini-ray 实现的字段** |
| `excludes` | `List[str]` | 打包时排除的文件/目录 | 用 **`.gitignore` 语法**；与 `working_dir` 或 `py_modules` 搭配 |
| `container` | `dict`（至少含镜像标识；**确切字段集合本书未确认**） | 指定 worker 用哪个容器镜像 | 官方**曾**弃用、后又**撤销了弃用警告**（PR #62732），保留为受支持的选项；新的替代方向是 `image_uri`。⚠️ 有下游 fork 记录称该字段"已从上游移除"，**两边说法冲突，本书标注未确认** |
| `java_jars` | `List[str]` | Java worker 的 classpath 追加项 | ⚠️ **没有配套的 `java_jvm_options`** —— JVM 选项是 **Java 侧的配置**（`ray.job.jvm-options`，走 HOCON/系统属性），**不是 `runtime_env` 的键**。本书早先把它写成配套字段，已修正（见 F.4.2） |
| `py_executable` | `str` | **覆盖 worker 启动用的 Python 命令** | 新引入、标注为实验性；`uv run` 集成就是通过它实现的（PR #50160） |
| `config` | `dict` 或 `RuntimeEnvConfig` | 安装行为配置 | 见 F.2.4 |
| `worker_process_setup_hook` | **模块名字符串（str）或可调用对象**（⚠️ 通过 **Jobs API 提交时只接受字符串**） | 每个 worker 进程启动时跑一次 | ✅ **2.58 里存在**：`python/ray/runtime_env/runtime_env.py` 的 docstring（:272）与字段白名单（:312）都有它，并在 :334 / :368-369 赋值。原文：*"A module name (string type) or callable (function) can be passed. … When a runtime env is specified by job submission API, only a module name (string) is allowed."* 官方标注为 **Experimental** |
| `image_uri` | `str`（容器镜像 URI） | worker 进程跑在该镜像里 | ✅ **2.58 存在**（`runtime_env.py` 的 `__init__` 形参与白名单；docstring 原文：*"URI to a container image. The Ray worker process runs in a container with this image. **This parameter only works alone, or with the `config` or `env_vars` parameters.**"*）。⚠️ **注意这个"只能单独用"的限制** —— 和 `pip` / `working_dir` 一起给是**非法组合**。这就是上文 `container` 那条提到的"新的替代方向" |
| `nsight` | `str` 或 `Dict[str, str]` | NVIDIA Nsight 性能剖析的配置 | ✅ **2.58 存在**，但**在 `runtime_env` 字典里的键名带下划线：`_nsight`**（`runtime_env.py` 的 `__init__` 收 `nsight=`，随后写成 `runtime_env["_nsight"]`）。⚠️ 属于 profiling 基础设施，日常不用写 |
| `rocprof_sys` | `str` 或 `Dict[str, Dict[str, str]]` | AMD ROCm `rocprof-sys`（原 Omniperf/rocProfiler）剖析配置 | ✅ **2.58 存在**，**键名同样带下划线：`_rocprof_sys`**（同上）。⚠️ 与 `nsight` 一样属于 profiling 基础设施 |

几条容易忽略的规则：

* **requirements.txt 的路径解析规则和你想的不一样。** `pip: "./requirements.txt"`
  里的相对路径是**相对于你本地当前工作目录**，**不是**相对于 `runtime_env`
  里的 `working_dir`；要引用 `working_dir` 里的文件得用
  `${RAY_RUNTIME_ENV_CREATE_WORKING_DIR}`。而且 **requirements.txt 里不能
  `-r` 链到 `working_dir` 之外的文件，也不能直接写 `./my-pkg.whl`。**
* **`.gitignore` 会被尊重**：`working_dir` 里有 `.gitignore` 时，里面列的
  文件默认不上传；要强行上传需在**上传端**设
  `RAY_RUNTIME_ENV_IGNORE_GITIGNORE=1`。
* **`ray[serve]` / `ray[tune]` 这类 extras 的 Ray 版本必须和集群一致**，
  否则装出来的是第二个 Ray。

### F.2.3 优先级与继承：「最具体的赢」到底是什么意思

三个设置点，从宽到窄：

```python
# ① Job 级：影响这个 job 里所有没有自己指定的 task / actor
ray.init(runtime_env={"pip": ["requests"], "env_vars": {"A": "a", "B": "b"}})

# ② 定义级：影响用这个 RemoteFunction / ActorClass 创建出来的所有东西
@ray.remote(runtime_env={"pip": ["torch"]})
def f(): ...

# ③ 调用级：只影响这一次
f.options(runtime_env={"env_vars": {"B": "new"}}).remote()
```

规则是「**更具体的覆盖更宽的**」，但**覆盖的粒度不是一个键，而是分两类**：

| 字段 | 合并语义 | 说明 |
|---|---|---|
| `env_vars` | **按 key 合并** | 父的先铺底，子的覆盖同名 key，未提到的 key 保留 |
| 其余全部字段（`pip` / `conda` / `working_dir` / `py_modules` / `excludes` / …） | **整体替换** | 子的一旦指定，父的那一项**整个没了** |

官方文档的例子（值得逐字看）：

```python
父:  {"pip": ["requests", "chess"], "env_vars": {"A": "a", "B": "b"}}
子:  {"pip": ["torch", "ray[serve]"], "env_vars": {"B": "new", "C": "c"}}
实际: {"pip": ["torch", "ray[serve]"],           # ← pip 被整体替换，"requests" 没了！
      "env_vars": {"A": "a", "B": "new", "C": "c"}}  # ← env_vars 按 key 合并
```

> ⚠️ **这张表最常害人的地方是 `working_dir`。** Jobs API 提交时
> `--working-dir .` 会把 `working_dir` 注入到 job 的 `runtime_env` 里；
> 如果你在代码里又写了 `.options(runtime_env={"env_vars": {...}})`,
> 看起来"只是加了个环境变量"，但在**某些版本**上会连带把 `working_dir` 顶掉，
> 于是 worker 起来后 `ModuleNotFoundError`。这正是下面这段历史的根因。

**这段历史必须知道**（它决定了你敢不敢依赖当前语义）：社区曾认为这套
"按 key 合并 + 其余替换"的规则**过于复杂且难以解释**，在
PR #22244 / #24538（"[runtime env] runtime env inheritance refactor"）里
提议改成**完全不合并**（没指定就继承父的，指定了就只用你指定的那份）。
该改动引发了 **P0 级回归**（典型症状正是上面那个"覆盖 `env_vars` 却丢掉了
Jobs API 注入的 `working_dir`"），评审认为用户本应被要求显式写
`{"working_dir": None}` 来清空。**重构最终没有以那个形态落地**，
但这意味着**不同小版本上行为可能不一致** —— 安全做法是
**永远显式写全你需要的每一项**，不要依赖父级兜底。

**两条硬约束**：**`eager_install` 不支持 task / actor 级设置**（只能在 job 级配）；
每次 `.options()` 都会**重新解析一遍 `runtime_env`，不做缓存** ——
可以动态传值，但别在热路径里反复计算它。
**Ray Client 模式下的继承语义本书未确认**（见 F.3），用 Ray Client 时请把
`runtime_env` 写全在 `ray.init()` 里。

### F.2.4 `RuntimeEnvConfig`

```python
from ray.runtime_env import RuntimeEnvConfig

ray.init(runtime_env={
    "pip": ["torch"],
    "config": RuntimeEnvConfig(setup_timeout_seconds=1800, eager_install=False),
    # config 也可以直接写成 dict：{"config": {"setup_timeout_seconds": 1800}}
})
```

| 字段 | 默认值 | 含义 |
|---|---|---|
| `setup_timeout_seconds` | **600** | 运行时环境创建的**总超时**（秒）。`-1` 表示**关闭超时逻辑**；除 `-1` 外**不允许 ≤ 0**，否则抛 `ValueError` |
| `eager_install` | **`True`** | 是否在 `ray.init()` 时就把环境装到**每一台节点**上（而不是等第一个任务来了再装） |
| `log_files` | `[]` | 该 runtime_env 下要在 dashboard 里展示的日志文件列表 |

三个要点：

* **`config` 不参与 runtime_env 的 hash 计算。** 两个 `config` 不同、但依赖
  声明完全相同的 runtime_env，在缓存看来**是同一个**。这是有意的（否则改个
  超时就导致全集群重装），但意味着**你没法靠"改 config"强制刷新缓存**。
* **`eager_install=True` 是"初始化慢、但第一次任务快"**：它在 `ray.init()`
  时就在**所有节点**上并行安装，300 台节点上一个 30 秒的 pip 安装会变成
  可观测的启动期尖峰。`False` 则是懒加载，**只有真正跑了任务的节点才装**，
  但**第一次任务要等安装**。
* **`setup_timeout_seconds` 是排查超时的抓手。** 环境装不上时任务会直接
  **失败**（而不是"慢"）。**调大默认 600 秒之前，先想清楚为什么一个
  pip install 要跑十分钟** —— 通常是依赖解析冲突或某个包要现场编译（F.2.8）。

### F.2.5 `runtime_env_agent`：为什么"装在哪台节点上"

这是理解 `runtime_env` 成本的**唯一关键机制**，而它在正文里从没出现过。

```
        ┌──────────────── 一台节点 ─────────────────┐
raylet  │  runtime_env_agent（独立 sidecar 进程）   │
（发现   │   PipPlugin / UvPlugin  → virtualenv     │
任务要  │   WorkingDirPlugin      → 下载 + 解压     │
一个还  │   CondaPlugin           → conda env      │
没装的  │   EnvVarsPlugin / PyExecutablePlugin      │
环境）   │   缓存：<session_dir>/runtime_resources/  │
        └───────────────────────────────────────────┘
```

机制拆开讲：

1. **`runtime_env_agent` 是每台节点一个的 sidecar 进程**，不是中心服务。
   raylet 发现"这个任务需要的环境本节点没有"时，向**本机的 agent**
   发 `GetOrCreateRuntimeEnv` RPC。
2. agent 按字段分派给插件，每个插件先**查缓存**，命中就跳过 ——
   文档说**从缓存加载一个环境几乎和普通 worker 启动一样快**（秒级），
   而真正的安装是**秒到分钟级**。
3. 装完返回一个 `RuntimeEnvContext`（环境变量 + 命令前缀），
   worker 进程就带着它启动。

**三条直接推论，每一条都能解释一个真实的困惑：**

| 推论 | 你看到的症状 |
|---|---|
| **N 台节点 = N 次安装**（除非都命中缓存） | "本地 3 秒跑完，集群上第一个任务卡了 4 分钟" |
| **缓存按节点、按环境 hash 分开存**，每类资源**默认上限 10 GB** | 大量不同 `working_dir` 反复提交 → 缓存被挤爆、老环境被清掉 → 下一波任务又要重装 |
| **agent 是本机的、没有中心视图** | "集群里有多少种 runtime_env"要靠 `state.list_runtime_envs()`（第 11 章）看 |

**可配的旋钮**：`RAY_RUNTIME_ENV_WORKING_DIR_CACHE_SIZE_GB` 之类的
**按字段**的缓存大小环境变量（**字段名本书未逐个确认**）。
缓存目录固定在 `<session_dir>/runtime_resources/`，**跟着会话走** ——
`ray stop` 之后重起，缓存是新的。

⚠️ **一个已报出的 agent 级 bug，值得记一笔**：**issue #65451** ——
`RuntimeEnvAgent` **每个 job 泄漏一个 logger 及其文件描述符**，长期运行的
集群最终因 `EMFILE` 而**所有 runtime_env 安装全部失败**。症状是"集群跑了
一阵之后，所有带 `runtime_env` 的新任务都起不来，但任务本身没有任何依赖
问题"。agent 日志里出现 `Too many open files` 就先怀疑这条。

> **安全提醒（第 17 章 §17.5 的缓解清单第 5 条）**：
> `runtime_env` 的 `pip` / `working_dir` / `env_vars` **会执行任意代码**
> （`pip` 会跑 setup.py，`working_dir` 会跑你上传的代码）。
> 所以"谁能设 `runtime_env`" 等价于 "谁能在集群上执行代码"。
> 带 token 认证的集群里，token 的权限边界要按这个来设计。

### F.2.6 `working_dir` 的成本模型：那个 2 GB 的事故

`working_dir` 是 `runtime_env` 里**唯一一个"体积直接决定集群负载"的字段**。
原因就是上一条：**它被分发到每一台节点**。

```
你写的：  runtime_env = {"working_dir": "./"}      # 本地看起来没什么
实际发生的：
  ./  →  打包 → 上传到 head → 分发到 N 台节点 → 每台上解压
  假设 ./ = 2 GB、集群 N = 50：
     网络传输 100 GB（head 出带宽是瓶颈）/ 磁盘 100 GB（50 × 2 GB，按 hash 缓存多份）
     解压时间 每台几十秒
```

**一个真实画像**：一个"本地 60 MB"的项目，`du -sh .` 显示 2 GB ——
因为里面有一个 1.9 GB 的 `.git`（历史 + 大文件残留），
以及 `node_modules/`、`.venv/`、`__pycache__/`、几个 `*.log`。
**提交时你不会看到任何提示**，只会看到任务迟迟不开始。

#### 尺寸上限（必须记住，它决定了失败形态）

| `working_dir` 形态 | 上限 | 备注 |
|---|---|---|
| 本地目录 | **500 MiB** | 超了直接失败 |
| 本地 zip 文件 | 解压后 **≤ 500 MiB** | ⚠️ 这种情况下 **`excludes` 完全不生效** |
| 远端 URI（`s3://…zip` 等） | **Ray 不施加大小限制** | 传输效率取决于你的对象存储与节点带宽 |

⚠️ **一个历史上存在、2.58 已补上的告警缺口（issue #45602）**：working_dir
上传的体积告警**只检查空目录和大文件（单文件 > 10 MiB）**，
**漏掉"由大量小文件组成的大目录"** —— 而 `.git` 恰好就是这种形态。

> ✅ **2.58 里这条告警已经加上了**（本书早先写"你很可能不会收到任何警告"，
> **已过时**）：`python/ray/_private/runtime_env/packaging.py` 里新增了
> `_warn_if_package_size_near_limit()`，**注释直接点名 `GH #45602`**：
> *"The per-file warning in `_zip_files` does not fire for directories that
> contain many small files (e.g. `.git`) … This warning closes that gap."*
> 阈值是 `PACKAGE_SIZE_WARNING = GCS_STORAGE_MAX_SIZE // 2`
> （`GCS_STORAGE_MAX_SIZE` 默认 `GRPC_CPP_MAX_MESSAGE_SIZE = 512 MiB`，
> **即打包后 ≈ 256 MiB 就会告警** —— 比 500 MiB 的硬上限提前很多，
> 这是刻意的"提前预警"），
> 可用环境变量 **`RAY_PACKAGE_SIZE_WARNING_MIB`** 调整
> （**设为 `-1` 可完全关闭这条告警**）。
> 所以现在的失败形态是：**先收到一条 warning（带本地 zip 路径与源目录名），
> 再在超限时拿到一个失败**（历史上是 `HTTPRequestEntityTooLarge`，
> 后来被包装为 `RayRuntimeEnvSetupError`）。
>
> ⚠️ **但"有告警"不等于"没问题"**：告警只在**接近上限时**才触发，
> 而且会被 `RAY_PACKAGE_SIZE_WARNING_MIB=-1` 关掉；
> **`.git` 依然永远该排。**

#### 修复：`excludes`

```python
ray.init(runtime_env={
    "working_dir": "./",
    "excludes": [
        ".git",            # 最大的一头，永远要排
        ".venv", "venv",   # 虚拟环境：worker 会自己建，别传
        "__pycache__", "node_modules",
        "*.log", "*.ipynb_checkpoints",
        "/data/",          # 前缀 / 表示相对 working_dir 的顶层目录
        "**/*.parquet",    # 数据文件绝不该进 working_dir
    ],
})
```

三条细则：

* **`excludes` 用的是 `.gitignore` 语法**，`/` 在开头或中间表示
  **相对于 `working_dir` 顶层**，**不要写绝对路径**。
* **`.gitignore` 里的条目默认本来就不上传**（见 F.2.2）。问题在于
  `.git/` 自己**从来不会被写进 `.gitignore`**，所以历史上所有人都得手写
  一次 `excludes: [".git"]`，或者建一个 `.rayignore`。
* **Ray 后来加了默认排除**（PR #59566），默认值是
  `.git,.venv,venv,__pycache__`。⚠️ **两个细节本书早先写错了**：
  ① `RAY_RUNTIME_ENV_DEFAULT_EXCLUDES` 是 **`ray_constants.py` 里的 Python 常量名**，
  **不是你可以 `export` 的环境变量** —— 用户可设的是
  **`RAY_OVERRIDE_RUNTIME_ENV_DEFAULT_EXCLUDES`**（逗号分隔；设成空串可整体关闭默认排除），
  写错名字会**静默无效**；
  ② `RAY_OVERRIDE_RUNTIME_ENV_DEFAULT_EXCLUDES` 替换的是**内置默认列表本身**；
  **你自己写的 `excludes` 仍然会追加在后面** —— 2.58 的源码就是
  `excludes = default_excludes + list(user_excludes)`
  （`python/ray/_private/runtime_env/working_dir.py:79-81`）。
  ⚠️ 本书早先这里写成"**不是与你的 `excludes` 取并集**"，**方向说反了**，已更正。
  ③ 这个默认值**是哪个版本引入的、你手头的版本有没有，本书未确认** ——
  请把它当成"可能没有"来写代码，显式列出这四项不会有害。

#### 正确的做法：数据不进 `working_dir`

和第 04 章 §4.7 "`working_dir` 是打包上传，不是挂载"是同一件事，
这里是它的量化版本：**`working_dir` 里只放代码 + 小配置文件（目标 < 50 MB）；
数据集放对象存储（S3/GCS）或共享文件系统（NFS、EFS），用路径参数传进任务；
大模型权重放共享文件系统或烤进镜像。**

> **"本地跑得通"和"集群上跑得动"之间最大的一个坑就在这里。**
> 本地 `working_dir` 是**零成本**的（同一个文件系统），集群上它是
> **O(节点数 × 体积)** 的网络与磁盘开销。这两个直觉不通用。

### F.2.7 pip vs conda vs container：决策表

| 方案 | 什么时候用 | 代价 | 主要坑 |
|---|---|---|---|
| **`pip`** | 默认选择。纯 Python 依赖、有 wheel | 每节点建一个 virtualenv，安装秒到分钟 | 解析冲突（见 F.2.8）；需要编译的包（无 wheel 的 C 扩展）在每台节点上都要现场编译 |
| **`uv`**（alpha） | 依赖多、想要快得多的安装 | 同 pip，但快数倍 | **alpha**；uv 集成由 `RAY_ENABLE_UV_RUN_RUNTIME_ENV`（**默认开**，`ray_constants.py:593`）控制，Ray Client 下由**客户端侧钩子** `_apply_uv_hook_for_client` 处理（`python/ray/util/client/__init__.py:20,185`）—— **可用**（F.3.4） |
| **`conda`** | 依赖里有**非 Python 的二进制**（CUDA、MKL、ffmpeg、编译器） | 最慢，且是**全局串行锁** | 与 `pip` / `uv` **三者两两互斥**；**Windows 上是 experimental/beta**（代码里只打一条 `logger.warning`，**不是拒绝**，`_private/runtime_env/validation.py:131-134`）；官方明确说"多进程并发装 conda env 不安全"，所以 agent 用全局锁串行化 |
| **`container` / `image_uri`** | 需要完整的系统级隔离（不同 CUDA 版本、不同 OS 库） | 拉镜像是分钟级，且要节点上有对应运行时 | `container` 字段的弃用/保留状态**上游与下游说法冲突（未确认）**；KubeRay 场景下的行为与裸机不同 |
| **自建镜像 + 不用 `runtime_env`** | 依赖稳定、且**反复运行的同一套负载** | 镜像大、构建慢，但**启动最快**（零安装） | 失去灵活性；多个团队的 CUDA 版本会在一个镜像里打架 |
| **`working_dir`** | **不是**依赖方案，是**代码分发**方案 | 见 F.2.6 | 别拿它分发数据 |

**一句话的选型口径**：
**用 `pip` 起步；有非 Python 二进制就上 `conda`；需要系统级隔离才上 `container`；
依赖集长期稳定、追求启动速度，就退出 `runtime_env` 回到镜像。**

### F.2.8 调试 `runtime_env` 失败

`runtime_env` 的失败有个共同特征：**报错信息和根因隔得很远**。
任务报的可能是 `ModuleNotFoundError`，真正的问题是 pip 解析冲突；
也可能是 `RuntimeEnvSetupError`，真正的问题是 agent 崩了。

#### 按症状分类

| 症状 | 最可能的原因 | 怎么确认 |
|---|---|---|
| 任务报 `RuntimeEnvSetupError` / 环境创建超时 | 安装超过了 `setup_timeout_seconds`（默认 600s） | 看 agent 日志；临时调大超时**只是为了看完整日志**，别当修复 |
| 任务报 `ModuleNotFoundError: No module named 'xxx'`，但你明明在 `pip` 里写了 | ① `working_dir` / `env_vars` 被 `.options()` 顶掉了（F.2.3）；② requirements.txt 路径解析错了；③ 装在了一个节点上、任务跑在另一个节点上（**不会发生** —— 每台节点各装各的，所以这条基本可排除） | 打印 `ray.get_runtime_context()` 和实际的环境变量；在任务里 `print(sys.path)` |
| pip 报依赖解析冲突（`ResolutionImpossible`） | 你 pin 的版本和集群基础镜像里的版本互斥；或者 `ray[serve]` 的版本与集群的 Ray 版本不一致 | 先在**干净 virtualenv** 里复现同一份 requirements；确认 Ray 版本号 |
| 安装"成功"但 worker 起不来 | `conda` 与 `pip` 同时指定；或 `py_executable` 指到了一个 worker 里不存在的解释器 | 看 agent 日志的 `RuntimeEnvContext` 部分 |
| 集群跑了一段时间后**所有** `runtime_env` 都失败 | agent 的 logger / fd 泄漏（**issue #65451**，`EMFILE`） | agent 日志里的 `Too many open files`；重启节点上的 agent 进程 |
| `working_dir` 上传失败 | 超过 500 MiB 上限；或 `.git` 太大 | 打一个包看看真实体积；**先找 `RayRuntimeEnvSetupError` 之前那条 package size warning**（`RAY_PACKAGE_SIZE_WARNING_MIB` 控制，`-1` 可关；见 F.2.6） |

#### 日志在哪 + 一个可复用的排查顺序

```
<session_dir>/logs/runtime_env_agent.log   # 每个节点一份，这是主战场
<session_dir>/runtime_resources/           # 缓存目录，能看到装了什么
```

`session_dir` 默认在 `/tmp/ray/session_latest/`（可用 `RAY_TMPDIR` 改）。
agent 日志是**按节点**的，所以排查顺序里的第 ② 步不能跳：

```
① ray.util.state.list_runtime_envs()        # 集群里到底注册了哪几种环境
② ray.util.state.list_tasks()               # 失败任务的 node_id
③ 登到那台节点看 runtime_env_agent.log      # ← 90% 的根因在这一步
④ 在干净环境里复现同一份 requirements        # 区分"Ray 的问题"和"依赖本身的问题"
⑤ 检查是否有 .options() 覆盖了父级 runtime_env
```

**第 ③ 步是最容易做错的一步**：默认会去看 head 的日志，
但任务很可能根本没在 head 上跑。`list_runtime_envs()` 是 **alpha API**
（第 11 章、第 17 章 §17.4），要装 `ray[default]` 且 dashboard 不能关。
更系统的排错请对照 **第 24 章（附录 D）** 的 D.1「安装与环境」与
D.2「启动与连接」—— 那里按症状组织，这里按机制组织。

### F.2.9 mini-ray 对照：只做了 `env_vars`

**说清楚这一条，是为了避免你产生错误的期待。**

| `runtime_env` 字段 | mini-ray | 具体位置 |
|---|---|---|
| `env_vars`（含顺带的 GPU 可见性） | ✅ **已实现** | `miniray/execution.py` 的 `runtime_env_context`：进入时 `os.environ[key] = value`，退出时**还原**（保存原值，`None` 就 `pop`）；同一个上下文里把 `CUDA_VISIBLE_DEVICES` 设成本次调度的 `gpu_ids` |
| `pip` / `uv` / `conda` | ❌ 未实现 | **静默忽略**：`miniray/execution.py` 只读 `payload["env_vars"]`，其余键直接丢掉 |
| `working_dir` / `py_modules` / `excludes` / `container` / `java_jars` | ❌ 未实现 | 同上 |

⚠️ **上表的"静默忽略"值得单独警告**：mini-ray 里**定义了**
`RayRuntimeEnvError`（`errors.py`，docstring 写"runtime_env 配置错误或不支持"），
**但全库没有任何一处 `raise` 它**。而且它**只出现在 `errors.py` 的模块级
`__all__` 里，并没有从包顶层导出** —— `from miniray import RayRuntimeEnvError`
会直接抛 **`ImportError`**（不是 `AttributeError`；`from X import Y` 里
`Y` 不在 `X` 中时，Python 抛的永远是 `ImportError: cannot import name ...`。
这点和 `miniray.ObjectLostError` 不一样，后者是导出的；
`from miniray.errors import RayRuntimeEnvError` 则可以拿到。
实跑复核：

```text
>>> from miniray import RayRuntimeEnvError
ImportError: cannot import name 'RayRuntimeEnvError' from 'miniray'
```
所以 `@ray.remote(runtime_env={"pip": ["torch"]})` 在 mini-ray 上**不会报错**，
任务照跑 —— 只是 `torch` 根本没被装。**这是"用 mini-ray 验证过的代码
迁到真实 Ray 上行为不一致"的一个真实来源。**

语义差异还要点出来：**mini-ray 的 `env_vars` 是"进程内临时改环境变量、退出
还原"，真实 Ray 的是"在 worker 进程启动前注入"**。两者在"任务里
`os.environ["X"]` 能看到 X"上一致，但真实 Ray 还涉及 `RuntimeEnvContext`
与 worker 命令行前缀，而 mini-ray 里**根本没有"另一台机器"这个概念**。

验证入口：`mini-ray/tests/test_core.py::test_runtime_env_env_vars`。
API 速查的对照列见第 21 章 A.7；工程手册见第 22 章。

> 📌 **另一处文档不一致，如实记录**：`miniray/execution.py` 的 docstring 写着
> "`pip` / `working_dir` 见 `runtime_env.py` 的说明"，但 **mini-ray 里并没有
> `runtime_env.py` 这个文件**。本附录不改 mini-ray，只在此指出。

---

## F.3 Ray Client（`ray://`）

### F.3.1 它到底是什么

Ray Client 解决的是一个很具体的场景：**你的笔记本上有 Jupyter，集群在远处，
你不想把代码打包提交，只想 `ray.init()` 一下就开始用那 200 张卡。**
它的实现方式是一个**代理**：

```
     你的笔记本                        集群
  ┌────────────────┐   gRPC      ┌───────────────────────┐
  │ driver 逻辑     │ ──────────► │ ray client server     │
  │ + @ray.remote   │ ◄────────── │ （head，端口 10001）   │
  │   函数定义      │             │        ↓              │
  │ （都在本地跑）   │             │ GCS / raylet 控制面    │
  └────────────────┘             │ （6379，GCS）          │
                                 └───────────────────────┘
```

| 项 | 值 |
|---|---|
| 连接串 | `ray.init("ray://<head_host>:10001")` |
| 服务端端口 | **10001**（Ray Client server），**不是** GCS 的 6379，也不是 dashboard 的 8265 |
| 需要的包 | **`pip install "ray[client]"`** —— 裸 `ray` 不带它 |
| 版本约束 | 客户端与服务端的 **Ray 版本必须一致**，且**Python 次版本也必须一致** |
| 谁在跑 driver | **你的本地进程**（这是理解一切限制的起点） |

### F.3.2 怎么用

```python
# 客户端（你的笔记本）
import ray
ray.init("ray://head.internal:10001")

@ray.remote
def f(x): return x * x
print(ray.get([f.remote(i) for i in range(8)]))
```

```bash
# 服务端两种起法
ray start --head --ray-client-server-port 10001          # ① 起 head 时一并打开
python -m ray.util.client.server --host <ip> --port 10001 # ② 在跑着的 head 上手动起

# 也可以用环境变量让代码完全不用改：之后 ray.init() 不带参数即可
export RAY_ADDRESS="ray://head.internal:10001"
```

> ⚠️ **安全**：Ray Client 的默认配置**没有任何鉴权**，任何能连到 10001 的人
> **都能在集群上执行任意代码**（官方文档明确警告不要把 client server
> 暴露到 `0.0.0.0/0`）。它和 dashboard 的 8265 是同一类风险面 ——
> 见第 17 章 §17.5 的缓解清单第 3 条（"永远不要暴露 8265 / 6379 / 10001"）。
> 开 `RAY_AUTH_MODE=token`（2.52 引入）能把它纳入保护范围。

### F.3.3 成本模型：为什么它对细粒度任务很糟

driver 在本地，意味着**每一次 `remote()` 提交、每一次 `ray.get()` 取值
都是一次跨网络的 gRPC 往返**。矩阵大概长这样：

| 场景 | 本地 `ray.init()` | Ray Client | 相对代价 |
|---|---|---|---|
| 8 个任务，每个耗时 10 秒 | 10 秒 | 约 10 秒 + 毫秒级往返 | 可忽略 |
| 10 万个任务，每个耗时 1 毫秒 | 亚秒级 | **往返开销完全主导** | **可能慢几十倍** |
| 传一个 1 GB 的 numpy 数组 | 同节点零拷贝 / 跨节点一次 | 本地 → server → 集群 | **双份序列化 + 双份传输** |
| 一个长驻 actor 反复调用 | 本地 IPC | 每次调用一次往返 | 取决于调用频率 |

**两条会让人措手不及的断开语义**（均来自官方文档）：
**连接断开超过 30 秒，负载会被终止**（笔记本合盖、Wi-Fi 切换、VPN 重连
都会触发 —— 这就是它不能用于长任务的根本原因）；
**客户端断开时，服务端持有的 object / actor 引用会被丢弃**，
即使很快重连，之前拿到的 `ObjectRef` 也已无意义。

**直接推论**：Ray Client 适合「**少量、粗糙、交互式**」的调用 ——
探索数据、调一次模型、跑个 demo；**不适合**任何"提交完可以走开"的东西。

### F.3.4 已知限制（这一节比上面都重要）

#### Ray Data：基本不兼容

* **`local://` 之类的本地文件路径不支持**，Ray Data 会直接抛错 ——
  因为 driver 的本地文件系统**不是集群的文件系统**。
  2.58 的真实报错文案是：
  *"Because you're using Ray Client, read tasks scheduled on the Ray cluster
  can't access your local files. To fix this issue, store files in cloud
  storage or a distributed filesystem like NFS."*
  （`python/ray/data/read_api.py:478-480`；`file_based_datasource.py:162` 同）。
  ⚠️ 本书早先引的 *"The local scheme paths … are not supported in Ray Client"*
  在 2.58 里**检索不到**，已换成源码原文。
* **写任务的调度语义不同**：Ray Data 会抛 `ValueError`，提示
  "If you're using Ray Client, Ray Data won't schedule write tasks on
  the driver's node" —— 当然，因为没有"driver 的节点"。
* **流式执行器（streaming executor）与 Ray Client 有大量不兼容**，
  自 **2.7** 起就存在；Ray 一度**跳过**了
  `test_client_compat.py::test_client_data_get` 这个兼容性测试，
  等将来的某个实现再支持（PR #41634 / #41665，两个 PR 标题均核对无误）。
  ⚠️ **该测试文件在 2.58 里已经不存在**（`test_client_compat.py` 已移除，
  全树检索 `test_client_data_get` 零命中）—— 所以"现在还跳不跳"**未确认**，
  但这条限制本身没有被官方宣布解除。

#### Ray Serve：**流式**调用被显式禁止，一元调用可用

* **流式的 `ObjectRefGenerator` 在 Ray Client 下不被支持**（**issue #43357**），
  而且 2.58 是**显式拦截**的：`handle.options(stream=True)` 在 Ray Client 上下文里
  直接抛 `RuntimeError: Streaming DeploymentHandles are not currently supported
  when connected to a remote Ray cluster using Ray Client.`
  （`python/ray/serve/handle.py:211-215`）。
* ⚠️ **但"完全不工作"是夸大了**：被拦的**只有 `stream=True`**。
  **一元（unary）调用是能用的** —— Serve 内部在 Ray Client 下把
  `enable_strict_max_ongoing_requests` 关掉来绕开
  （`python/ray/serve/_private/default_impl.py:213-216`）。
  本书早先这里写的"Serve 的数据面在 Ray Client 下实际完全不工作"
  与源码不符，已更正。
* 该 issue 里列的 TODO 包括"给流式 + Ray Client 加一个更好的报错"、
  "决定是否支持或显式禁用 `DeploymentHandle` 调用"。

#### 依赖管理：uv 集成在 2.58 **是工作的**

* ⚠️ **本书早先这里说"uv 集成在 Ray Client 下不工作"，已经过时**：
  2.58 给 Ray Client 加了**客户端侧钩子** `_apply_uv_hook_for_client`
  （`python/ray/util/client/__init__.py:20`，在 `connect()` 里于 :185 调用；
  其 docstring 明确写 *"UV detection must happen on client side where
  'uv run' process exists"*，见 issue #57991）。**uv + Ray Client 可用。**
* ⚠️ 另外**变量名也写错了**：管 uv-run 集成的是
  **`RAY_ENABLE_UV_RUN_RUNTIME_ENV`**（默认 `True`，`ray_constants.py:593`），
  **不是 `RAY_RUNTIME_ENV_HOOK`**。后者是一个**独立的通用用户钩子**
  （`ray_constants.py:167`），而 Ray 在 uv-run 生效时会**主动把它停用**
  （`ray_constants.py:585-592` 的注释）。

#### 其它

| 限制 | 说明 |
|---|---|
| 多集群连接 | **实验性**（同一个客户端进程连多个集群） |
| 版本匹配 | Ray 版本 + **Python 次版本**都要一致，否则连不上 |
| `runtime_env` 的继承语义 | **本书未确认**其在 Ray Client 下的行为是否与本地一致 |
| 各类库的支持矩阵 | 除上面点名的 Ray Data / Ray Serve 外，**其它库（Train / Tune / RLlib / `ray.data.llm`）在 Ray Client 下的具体支持情况，本书未逐一确认** |

> **一条经验性的判断（不是事实）**：Ray Client 的失效模式几乎总是
> "**在某个库的某个内部机制上撞墙**"，而不是"连不上"。
> 因为它的设计前提是"driver 在本地"，而 Ray 的很多库在设计时
> 就默认 driver 在集群里 —— 这个假设差在 Ray Data 的调度策略上，
> 差在 Serve 的 `ObjectRefGenerator` 上。
> **每引入一个新库，都要重新问一遍这个假设。**

### F.3.5 2026 年的状态：维护模式

**这是本节最有行动价值的一段。**

| 事实 | 来源 |
|---|---|
| **Ray Client 已进入官方认定的维护状态**，官方指引改推 **Jobs API** 与 runtime-environments 文档 | **issue #47700**（**2024-09-17** 创建，**2026-05-18** 以 `NOT_PLANNED`/"已过时"关闭）。⚠️ 关闭评论是**社区贡献者**（`dstrodtman`，页面上 `authorAssociation` 为 **CONTRIBUTOR**，**不是**维护者）写的 docs-team backlog 分诊，其中给出"Ray Client is deprecated、请用 Ray Jobs API"的指引。⚠️ 本书早先写成"**维护者回复（2024-11）**"——日期与身份都不对，已更正 |
| 🔴 **最硬的一条：代码级弃用标记** —— `ray.client()` 与 `ClientBuilder` 在 2.58 **都带 `@Deprecated`**（`python/ray/client_builder.py`；⚠️ 路径**不是** `ray/util/client/client_builder.py`）。运行时警告推荐的替代品是 **`ray.init`**（`"Starting a connection through `ray.client` will be deprecated in future ray versions in favor of `ray.init`."`），**不是** Jobs API | 源码 `python/ray/client_builder.py` 的 `@Deprecated`（`class ClientBuilder` 与 `def client`）与 `_client_deprecation_warn()`；`python/ray/util/annotations.py` 的 `Deprecated` |
| ⚠️ **但这个警告是"有条件"的**：`ray.init("ray://…")` 内部以 `_deprecation_warn_enabled=False` 调 `ray.client`（`python/ray/_private/worker.py`），所以**走 `ray.init` 这条路时静默不警告**；只有显式 `ray.client(...).connect()` 才会看到 `DeprecationWarning` | 同上 |
| 文档明确说：Ray Client 适合**本地 Python shell 里的交互式开发**，但**需要到集群的稳定连接**，且**断开超过 30 秒会终止负载**；**长时运行的工作负载请改用 Ray Jobs API** | 官方 Ray Client 文档 / `ray-client.rst` |
| 官方文档把它定位成**"专家向"（experts only）** | 同上 |
| 曾经讨论过的 "Ray Client V2" **状态不明** | **未确认** |

**对读者的意义**：

* **不要在新项目里把 `ray://` 当默认连接方式。** 第 02 章的地址表里
  `"ray://<head>:10001"` 之所以还列着，是因为它在交互式调试里确实省事，
  不是因为它是推荐架构。
* **"临时调试远端集群"是它唯一稳固的用途。** 任何"提交完可以走开"的场景
  都该走 **Jobs API**（第 04 章 §4.7）。两者不是同一样东西的两代：
  **前者是"远程控制"，后者是"远程提交"，进入维护模式的是前者。**
* 交叉参考：第 02 章（`address` 四种写法）、第 04 章 §4.7、第 17 章
  （安全清单里 10001 端口的位置）、第 24 章 **D.2 的 Q7**（连不上 / 不支持
  Ray Data 的处置）。

### F.3.6 决策表：四种"把活交给集群"的方式

| | **Ray Client** | **Jobs API** | **SSH 上去自己跑** | **KubeRay `RayJob`** |
|---|---|---|---|---|
| **driver 在哪** | 你的机器 | 集群（head 上的 entrypoint） | 集群（你 ssh 进去的 shell） | 集群（K8s 创建的 entrypoint pod） |
| **连接方式** | `ray.init("ray://…:10001")` | `ray job submit` / `JobSubmissionClient` | `ssh` + `ray.init(address="auto")` | `kubectl apply -f rayjob.yaml` |
| **断线后果** | **> 30 秒即终止负载**；引用被丢弃 | **无影响**，driver 在集群上 | **进程被 SIGHUP 杀掉**（除非 `nohup`/`tmux`） | **无影响** |
| **依赖管理** | `runtime_env` 可用（**uv 集成在 2.58 是工作的**，开关是 `RAY_ENABLE_UV_RUN_RUNTIME_ENV`）；Ray Data / Serve 的部分路径受限（见 F.3.4） | `runtime_env` + `--working-dir`（完整能力） | 手动；通常靠预装镜像 | `runtime_env` 写在 CRD 里 |
| **日志** | 在你自己终端里 | `ray job logs <id> --follow` | 在终端里，断了就没了 | `kubectl logs` / History Server（KubeRay v1.7，beta） |
| **生命周期管理** | 无 job 概念 | job_id + 五种状态 + 重提 | 无 | CRD 声明式 + 自动清理集群 |
| **适合** | **交互式实验、临时调试** | **绝大多数生产批任务** | 快速验证、一次性手段 | **K8s 上的生产批任务** |
| **不适合** | 长任务、任何生产 | 需要 K8s 声明式管理的场景 | 任何需要可靠性的事情 | 团队不懂 K8s（两套系统一起排障） |

**选型口径一句话**：
**交互式调试用 Ray Client；跑批用 Jobs API；K8s 上跑批用 RayJob；
SSH 只用来"看看环境对不对"。**

---

## F.4 多语言绑定（Java / C++）

### F.4.1 跨语言对象模型

Ray 的多语言**不是"进程间 RPC"，而是"多门语言的 worker 共用一个对象存储"**。
这是理解全部能力与全部限制的起点。

```
  Python worker        Java worker        C++ worker
        │                   │                 │
        └──────────┬────────┴────────┬────────┘
                   ▼                 ▼
        ┌──────────────────────────────────────────┐
        │        共享对象存储（Plasma）              │
        │  对象只有一份；谁都能按 ObjectRef 取        │
        │  owner 是"提交这次任务的那个进程"          │
        └──────────────────────────────────────────┘
```

* 跨语言调用返回的仍然是 **`ObjectRef`**（Java 侧是 `io.ray.api.ObjectRef<T>`；另有 id 类型 `io.ray.api.id.ObjectId`），
  指向对象存储里的值。
* 值的**序列化格式是 MessagePack**，Ray 用一个**元数据标签**标明这份数据
  是哪种序列化方式：

| 标签 | 含义 |
|---|---|
| `XLANG`（`OBJECT_METADATA_TYPE_CROSS_LANGUAGE`） | **跨语言兼容格式** —— 只有这类能被另一门语言读懂 |
| `JAVA` / `PYTHON` | 语言原生序列化（pickle / Java 序列化） |
| `RAW` | 裸字节数组（`byte[]` / `ByteBuffer`），Python 侧也能读 |
| `ACTOR_HANDLE` | actor 句柄 |

**只有 `XLANG` 那一类真正"跨得过去"。** 语言特有的类型会被包在
**MessagePack 扩展类型（ext type，ID 101）**里回退成语言自己的序列化 ——
**结果是另一门语言读不出来**。

#### 哪些类型能干净地跨语言

| 类型 | 能否跨语言 | 备注 |
|---|---|---|
| `None` / `null`、`bool`、整数、浮点、`str` / `String`、`bytes` / `byte[]` | ✅ | 基本类型，最安全 |
| 数组（`List` / `Object[]` / numpy） | ✅（部分） | Python 侧的 numpy 数组走对象存储；**Java 的 `List` / `Map` 不能直接跨语言传输，要用 `Object[]`** |
| `ObjectRef` / `ObjectId` / `ActorHandle` | ✅ | Ray 内建类型，有跨语言表示 |
| Java 的 `Map` / 自定义 Java 对象 | ❌ | 回退成 `JAVA` 标签，Python 侧读不出来 |
| Python 的任意对象（pickle） | ❌ | 回退成 `PYTHON` 标签，Java 侧读不出来 |
| 异常 | ⚠️ 部分 | 跨语言失败在 **Python 侧**传播为 **`ray.exceptions.CrossLanguageError`**；`CrossLanguageException` 是 **Java 侧**的类名（`io.ray.api.exception.CrossLanguageException`）。⚠️ 写 `except ray.exceptions.CrossLanguageException` 会 `AttributeError` |

> **实践口径**：**跨语言边界上只传基本类型、数组和 `ObjectRef`。**
> 任何"我定义了一个类，两边都用它"的想法，都会撞上序列化这道墙。
> 复杂的结构体请自己序列化成 `bytes` / JSON / Arrow 再传。

⚠️ **`List` / `Map` 这条限制不是"性能问题"，是"功能不可用"。**
如果你发现 Java 侧把 `HashMap` 传过去、Python 侧 `ray.get` 出来是个读不懂的
东西，那不是 bug，是设计。

### F.4.2 起一个多语言集群：`code_search_path`

多语言集群**不会自动找到你的代码**。你必须显式告诉 Ray "去哪些目录/jar 包里
找用户代码"，这个设置叫 **`code_search_path`**：

```python
# Python driver
import ray
from ray.job_config import JobConfig

ray.init(job_config=JobConfig(code_search_path=["/path/to/jars", "/path/to/pys"]))
```

```bash
# ① Java driver：系统属性，多个用 : 分隔
java -Dray.job.code-search-path=/path/to/jars:/path/to/pys ...

# ② Python 侧：走 JobConfig，不是 ray.init 的顶层参数
import ray
from ray.job_config import JobConfig
ray.init(job_config=JobConfig(code_search_path=["/path/to/code"]))
```

> ⚠️ **`ray start --code-search-path=...` 这个 flag 不存在** ——
> 本书早先给过这个写法（还带了一句"以 `--help` 为准"的免责），
> 但它是**可确定的错误**：`ray start` 的参数表里没有它，照抄会
> `unrecognized arguments`。**只有上面两条路**：
> Python 侧 `JobConfig(code_search_path=[...])`、
> Java 侧 `-Dray.job.code-search-path=...`。

它的语义是**双重的**：对 Java worker 是 **`CLASSPATH`**，
对 Python worker 是 **`PYTHONPATH`**。

⚠️ **两个容易混的东西**：**`code_search_path`（job 级 classpath /
pythonpath）和 `runtime_env` 的 `java_jars`（追加 Java worker 的 classpath）
是两回事**，但都影响 Java worker 的 classpath。`java_jars` 的实现在
`RuntimeEnvContext` 里：构造 Java worker 的 `-cp` 参数时，先放 Ray 自己的
jars 目录，再把 `java_jars` 里每一项的 `{jar}/*` 和 `{jar}` 追加进去；
它的取值形态是 URL 列表（如 `s3://jar1.zip`），由 dashboard agent 下载。
**两者同时用而 classpath 不符合预期时，先怀疑它们的叠加顺序。**

**mini-ray 完全不做多语言**（第 22 章的"缺失"清单、README 的"明确不做"），
所以本节内容**无法在 mini-ray 上验证**。

### F.4.3 `cross_language` 函数描述符

跨语言调用的 API 面很小，但两边不对称 —— **Python 侧有一个显式的
`ray.cross_language` 模块，Java 侧则是另一套 `PyFunction` / `PyActorClass`。**

#### Python → Java

```python
import ray
from ray import cross_language

# 引用一个 Java 静态方法（等价于 Java 侧的 Ray.task(...) 注册的那个函数）
java_f = cross_language.java_function("io.example.MyClass", "myStaticMethod")
print(ray.get(java_f.remote(1, 2)))

# 引用一个 Java actor 类
java_actor_cls = cross_language.java_actor_class("io.example.MyActor")
actor = java_actor_cls.remote()
print(ray.get(actor.someMethod.remote()))
```

#### Java → Python

Java 侧的入口是 `PyFunction` / `PyActorClass` / `PyActorMethod`
以及 `Ray.task(...)` / `Ray.actor(...).remote()`，返回 `ObjectRef`，
actor 侧拿到的是 `PyActorHandle`。

**硬前提：被 Java 调用的 Python 函数/类必须用 `@ray.remote` 装饰过。**
没有 `@ray.remote` 的 Python 函数对 Java 侧**不存在**。

```java
// 语义示意（不是可直接编译的完整示例）
ObjectRef<Object> ref = Ray.task(PyFunction.of("mymodule", "my_func", Integer.class))
                            .remote(1);
Object result = ref.get();
```

⚠️ **Java 侧的确切 API 形态（类名、方法签名、泛型参数）
本书未逐版本核对**，上例是语义示意。请在 Java 侧以
`io.ray.api.*` 的 Javadoc 为准。

### F.4.4 诚实的评估：生态压倒性地在 Python 这边

**先把结论放在前面**：**Java / C++ 绑定是"集成用的"，不是"用来写 Ray 应用的"。**
如果你在纠结"我的新项目要不要用 Java 写 Ray 任务"，答案在 99% 的情况下是不要。

#### 一条有据可查的例证

**PR #41194**（"[core] retryable exceptions for method"）给 actor 方法加了
`max_retries` 与 `retry_exceptions`：Python 侧通过
`@ray.method(max_retries=...)` 或 `actor.method.options(max_retries=2,
retry_exceptions=[ValueError])` 使用；而**该 PR 明确没有给 Java 和 C++ 加
这两个选项** —— 它们仍然只有 actor 级的 `max_task_retries`（针对 actor 死亡
的重试），**没有"按异常类型决定要不要重试"的能力**。

这说明的不是"Java 被歧视"，而是**能力演进沿着 Python 生态的真实需求走** ——
功能先在 Python 落地，其它语言跟上或者不跟。**这条可以推广成一条经验：
看到一个新特性，先假设"只有 Python 有"。**

> ⚠️ **不要据此认为 Java/C++ 绑定已经废弃。** 它们仍是官方维护的
> （第 01 章的架构图里 Java 和 C++ 都标注为"官方"）。准确的说法是：
> **维护存在，但特性对齐滞后，且使用者的绝对数量远小于 Python。**
> 另一条相关事实：第 16 章提到过一个横跨 **C++ / Java / Python 三侧**的特性
> 被从 Ray Core 移除（commit `db822f50`）—— **跨语言的重构成本确实是三份的**，
> 这也是这类特性推进慢的结构性原因。

#### 成熟度对照

| 维度 | Python | Java | C++ |
|---|---|---|---|
| 生态与文档 | 全部 | 少量 | 更少 |
| 上层的 AI 库（Data / Train / Tune / Serve / RLlib） | ✅ | ❌ | ❌ |
| 新特性对齐 | 首发 | **滞后或不跟**（如 PR #41194） | **滞后或不跟** |
| `runtime_env` 支持程度 | 完整 | 部分（`java_jars` 等；JVM 选项走 Java 侧配置，不在 runtime_env 里） | **本书未确认** |
| Dashboard / State API 里的可见性 | 完整 | 部分 | **未确认** |
| 适合的场景 | 一切默认场景 | **已有 Java 服务需要接入 Ray** | **极少数性能/集成场景** |

### F.4.5 什么时候它真的有用

有三个场景是"用得上"的（其余都是"能用但不该用"）：

**① 已有一个 Java 服务，想把它的计算接到 Ray 的调度上。**

```
Java 微服务（已有的 HTTP 入口）
      │  Ray.task(PyFunction.of("model", "predict"))   ← 跨语言调用
      ▼
Ray 集群上的 Python 模型推理任务（GPU 在这边）
```

**价值不在"用 Java 写分布式"，而在于"不用把已有的 Java 服务重写成
Python"。** 注意**方向很关键**：跨语言调用的开销是**一次对象存储往返 +
序列化**，所以**调用应该是"粗粒度"的**（一次干一批活），
不要做成 Java 侧高频 `ray.get` 的细粒度循环 —— 那就是把 F.3.3 里
Ray Client 的问题原样复制一遍。

**② C++ 侧的库只能在 C++ 里调。** 一些高性能库（自研算子、特定推理运行时）
只有 C++ 接口，而你想用 Ray 来调度它。
**③ 渐进式迁移的中间态。** 老系统是 Java、新系统是 Python，
用跨语言调用做过渡，而不是一次性重写。

> **一个更简单的替代方案，多数时候更好**：如果只是"Java 服务要调 Python
> 模型"，**把 Python 侧包成 Ray Serve 部署，Java 侧用 HTTP 调它**（第 15 章）——
> 你得到服务发现、扩缩、路由、版本管理，而**不需要碰跨语言绑定**。
> **这是本书的判断，不是官方建议**，但它是"最小惊讶"的那条路。

---

## F.5 三者组合的实战场景

### F.5.1 场景 A：笔记本上的 Jupyter，连远端集群做一次实验

**目标**：拿 200 张卡里的 4 张，跑一次模型 eval，看看输出对不对。
**不想做的事**：打包、提交、等队列、看日志。

```python
# ───── 笔记本上（Jupyter cell）；一次性准备：pip install "ray[client]" ─────
import ray

ray.init(
    "ray://head.internal:10001",                # ← Ray Client：driver 在本地
    runtime_env={
        "pip": ["transformers==4.44.0", "datasets"],  # ← 集群上没有的包
        "env_vars": {"HF_HOME": "/shared/hf"},
        "config": {"setup_timeout_seconds": 900},  # ← 首次装包给足时间
        # 注意：没有 working_dir —— 代码就在 cell 里，
        #      函数定义会随 .remote() 一起被序列化过去
    },
)

@ray.remote(num_gpus=1)
def evaluate(model_id, prompts):
    from transformers import pipeline
    pipe = pipeline("text-generation", model=model_id, device=0)
    return [pipe(p, max_new_tokens=32)[0]["generated_text"] for p in prompts]

refs = [evaluate.remote("Qwen/Qwen2.5-1.5B", chunk) for chunk in chunks]
results = ray.get(refs)          # ← 每次 get 都是一次网络往返
```

**这段代码值得注意的三件事**：

1. **函数定义本身就是"代码分发"。** `@ray.remote` 的函数会被 cloudpickle
   序列化随任务送到集群 —— 所以**小段交互式代码不需要 `working_dir`**，
   这正是 Jupyter 场景省事的真正原因。**但它有边界**：函数体里 `import` 的
   自定义模块、读的本地文件、依赖的相对路径，**都不会跟着走**。
2. **`pip` 的安装在集群每台节点上发生**（F.2.5），你看到的"第一次调用卡了
   很久"**不是分布式慢，是安装慢**。`eager_install=True`（默认）会在
   `ray.init()` 阶段就装。
3. **`ray.get` 的往返成本**（F.3.3）：几次、每次几 KB 完全可以接受；
   出现在 10 万次的循环里就要停下来重新想。

**什么时候该停下**：当实验从"看看输出对不对"变成"跑 6 小时的完整 eval"时
—— **必须切到场景 B**，因为 Ray Client 断开 30 秒就会终止负载（F.3.3）。

### F.5.2 场景 B：生产路径（Jobs API + KubeRay）

同一件事在生产里的形态完全不同：

```yaml
# rayjob.yaml —— KubeRay 的 RayJob CRD（第 17 章 §17.2）
apiVersion: ray.io/v1
kind: RayJob
metadata: { name: eval-qwen }
spec:
  entrypoint: python -m eval.run --model Qwen/Qwen2.5-7B --split test
  shutdownAfterJobFinishes: true
  runtimeEnvYAML: |
    pip: [transformers==4.44.0, datasets]
    env_vars: { HF_HOME: /shared/hf }
    working_dir: "s3://my-bucket/code/eval.zip"  # ← 从这里分发，不走本地打包
    excludes: [".git", "*.log"]
    config:
      setup_timeout_seconds: 900
      eager_install: false      # ← 批任务：用到才装，别在启动期全节点铺开
  rayClusterSpec: { headGroupSpec: {...}, workerGroupSpecs: [...] }
```

| 维度 | 场景 A（Ray Client） | 场景 B（Jobs API / RayJob） |
|---|---|---|
| driver 在哪 | **你的笔记本** | **集群**（entrypoint pod） |
| 你合上笔记本会怎样 | **30 秒后负载被终止** | **无影响** |
| 代码怎么过去 | 函数定义随 `remote()` 序列化 | `working_dir`（打包上传）或镜像内已有 |
| 依赖何时装 | 每台节点，`ray.init()` 时（默认 eager） | 每台**真正用到**的节点，懒加载更省 |
| 日志 | 你的终端，断了就没了 | `ray job logs` / `kubectl logs` / History Server |
| 失败了怎么办 | 手动重跑 | job_id + 状态机 + 重提；可配检查点恢复（第 10 章） |
| 可审计性 | **无** | 谁提交的、什么时候、什么资源、什么状态，都有 |
| 粒度假设 | **粗粒度**（往返成本高） | 无特殊限制 |

**注意一个反直觉的点**：B 的 `eager_install: false` 与 A 的默认 `true`
是**各自场景下的正确选择**，不是"哪个更好"。交互式场景里你**肯定会用到**
这个环境，启动时装好、之后的 `remote()` 才有意义；而 300 节点的批任务里
任务可能只落在 20 台上，启动期给 300 台都装一遍是纯粹的浪费。

### F.5.3 边界：什么**不要**带进生产

这一节是 F.5 存在的理由。**下面的每一条都是"本地用着很爽、生产会出事"的：**

```
❌ 不要把 Ray Client 带进生产
   · 断开 30 秒即终止负载；笔记本不是运行环境
   · 没有 job 生命周期、没有状态、没有重提
   · Ray Data / Ray Serve 在它下面受限或完全不可用（F.3.4）

❌ 不要把 working_dir 当成数据通道
   · 它是 O(节点数 × 体积) 的分发；500 MiB 是硬上限（F.2.6）
   · 每次提交都重新打包、重新分发

❌ 不要在集群里依赖"driver 上的本地文件"
   · 场景 A 里读得到的路径，场景 B 里读不到
   · 所有输入输出走共享存储 / 对象存储的路径

❌ 不要把 runtime_env 当成"随便谁都能配的东西"
   · pip / working_dir / env_vars 等价于在集群上执行代码（第 17 章）
   · 带 token 认证时，权限边界要按 RCE 设计

❌ 不要指望"跨语言"是零成本的
   · 每次跨语言调用 = 序列化 + 对象存储往返
   · 跨语言边界只传基本类型/数组/ObjectRef（F.4.1）
```

**一条可执行的判据**：**问自己"我合上笔记本，这个任务会怎样？"**
* 答案是"就断了" → 你还在场景 A，**它只适合实验**。
* 答案是"照跑不误" → 你已经在场景 B，可以开始谈生产了。

从 A 到 B 的迁移成本通常比想象的低（第 04 章 §4.7 的迁移提示：
代码常常一行都不用改，改的是运维侧）。**真正贵的是"把 A 的假设带进 B"** ——
比如在任务里读 driver 的本地文件，或者在 entrypoint 里做重活。

---

## F.6 小结

**`runtime_env`：**

* 它是**依赖注入**：把依赖从"镜像"下移到 **job / actor / task** 三级。
  字段全表见 F.2.2 —— 最常用的是 `pip` / `working_dir` / `env_vars` /
  `py_modules` / `excludes`；`uv` 是 **alpha**；`conda` 与 `pip` / `uv`
  **三者两两互斥**，且 **Windows 上是 beta / experimental 支持**（官方口径
  *"beta support on Windows"*，`doc/source/ray-core/handling-dependencies.rst`；
  代码里只打一条 `logger.warning`，**不是拒绝** —— ⚠️ **不是"Windows 不支持"**，
  本书早先这条写错了，已按官方口径更正，与 F.2.2 对齐）；
  `container` 的弃用状态**上下游说法冲突（未确认）**。
* **继承规则是"两类"而不是"一条"**：`env_vars` **按 key 合并**，
  **其余字段整体替换**；`eager_install` 不支持 task / actor 级设置。
  这套语义**曾被提议改成"完全不合并"并引发 P0 回归**（F.2.3）——
  **安全做法是把需要的项每次写全，不依赖父级兜底。**
* `RuntimeEnvConfig`：`setup_timeout_seconds` 默认 **600**（`-1` 关闭，
  不接受 ≤ 0）、`eager_install` 默认 **`True`**、`log_files`；
  **`config` 不参与 hash，改它不会刷新缓存。**
* **`runtime_env_agent` 是每节点一个的 sidecar 进程**，缓存落在
  `<session_dir>/runtime_resources/`（**每类资源默认缓存 10 GB**），
  这解释了"N 台节点 = N 次安装"。**已有 logger / fd 泄漏的 issue
  （#65451，`EMFILE`）：症状是集群跑久之后所有 runtime_env 都失败。**
* **`working_dir` 的成本是 O(节点数 × 体积)**：本地目录上限 **500 MiB**
  （本地 zip 按解压后算且 **`excludes` 不生效**），远端 URI 无 Ray 侧限制；
  `.gitignore` 默认被尊重；Ray 后加的默认排除 `.git,.venv,venv,__pycache__`
  （PR #59566，**引入版本未确认**）；**"大量小文件组成的大目录"的告警
  2.58 已补上**（`_warn_if_package_size_near_limit`，注释点名 issue #45602，
  阈值 ≈256 MiB，`RAY_PACKAGE_SIZE_WARNING_MIB` 可调 / `-1` 可关）——
  ⚠️ 本书早先写"`.git` 撑爆 `working_dir` 时你很可能收不到任何警告"，
  **已过时，与 F.2.6 对齐**；**但有告警不等于没问题：
  数据永远不要放进 `working_dir`。**
* 排查顺序：`list_runtime_envs()` → 失败任务的 `node_id` → **那台节点**的
  `runtime_env_agent.log`。**别默认去看 head 的日志。**
  **mini-ray 只实现了 `env_vars`**（`miniray/execution.py`，"临时改、退出
  还原"），其余一律不做。

**Ray Client：**

* 它是**代理**：driver 在你的机器上，每次 `remote()` / `ray.get()` 都是一次
  网络往返。端口 **10001**，需要 **`ray[client]`**，**Ray 版本与 Python
  次版本都必须与服务端一致**；只适合**粗粒度、交互式**调用。
  **连接断开超过 30 秒负载会被终止**；断开时服务端持有的引用被丢弃。
* **已知限制（比用法重要）**：Ray Data 的本地路径不支持、写任务调度抛
  `ValueError`、流式执行器自 2.7 起大量不兼容（兼容测试被跳过，
  PR #41634 / #41665）；**Serve 的数据面在 Ray Client 下被拦的只有
  `stream=True` —— 一元（unary）调用是可用的**
  （流式 `ObjectRefGenerator` 不被支持，**issue #43357**；
  2.58 起 `handle.options(stream=True)` 会被**显式拦截**并抛 `RuntimeError` ——
  ⚠️ 本书早先这条写成"Serve 数据面**实际不可用/完全不工作**"是**夸大了**，
  与 F.3.4 已按源码更正的口径**对齐**）。
  其余库的支持矩阵**本书未逐一确认**。
* **uv 集成在 2.58 是工作的**（含 Ray Client）：管它的是
  **`RAY_ENABLE_UV_RUN_RUNTIME_ENV`**（默认 `True`，
  `python/ray/_private/ray_constants.py`）——
  ⚠️ **不是 `RAY_RUNTIME_ENV_HOOK`**，后者是一个**独立的通用用户钩子**
  （同文件），且 Ray 在 uv-run 生效时会**主动把它停用**。
  2.58 还给 Ray Client 加了客户端侧钩子 `_apply_uv_hook_for_client`。
  详见 F.3.4 的"依赖管理"小节。**本书早先这两处（变量名 + "不工作"）
  都是错的，已更正。**
* **弃用的硬证据（不止 issue #47700）**：2.58 里 **`ray.client` 与
  `ClientBuilder` 都带了 `@Deprecated`**（`python/ray/client_builder.py`）——
  ⚠️ 注意文件路径**不是** `ray/util/client/`，是顶层的 `ray/client_builder.py`。
  运行时的警告文案推荐的替代品是 **`ray.init`**，**不是 Jobs API**：
  *"Starting a connection through `ray.client` will be deprecated in future
  ray versions in favor of `ray.init`"*。
  ⚠️ 而且 **`ray.init("ray://…")` 走的是 `_deprecation_warn_enabled=False`
  （`_private/worker.py`），会静默不警告** ——
  所以"没看到警告"不等于"没弃用"。
* **2026 年的状态：维护模式**（**issue #47700 于 2026-05 关闭**时的评论，该 issue 已以
  "Ray Client is deprecated、请用 Ray Jobs API" 关闭）。**不要把 `ray://`
  当新项目的默认方案。** 四选一：**交互式调试用 Ray Client；跑批用
  Jobs API；K8s 上跑批用 RayJob；SSH 只用来看看环境对不对。**

**多语言绑定：**

* 机制是**多门语言的 worker 共用一个对象存储**，值用 **MessagePack**
  序列化并带元数据标签；**只有 `XLANG` 那一类能被别的语言读懂**，
  语言特有类型回退成 `JAVA` / `PYTHON` 标签或 MessagePack ext type（ID 101），
  **另一门语言读不出来**。边界上只传**基本类型 / 数组 / `ObjectRef`**；
  **Java 的 `List` / `Map` 不能直接跨语言传输，要用 `Object[]`。**
* 要设 **`code_search_path`**（对 Java 是 `CLASSPATH`、对 Python 是
  `PYTHONPATH`），它与 `runtime_env` 的 `java_jars` **是两回事但都影响
  Java classpath**。Python → Java 用 `ray.cross_language.java_function` /
  `java_actor_class`；Java → Python 用 `PyFunction` / `PyActorClass` /
  `Ray.task(...)`，且**被调的 Python 函数必须带 `@ray.remote`**。
* **诚实的评估**：**PR #41194 给 Python 加了方法级 `max_retries` /
  `retry_exceptions`，明确没有给 Java / C++ 加** —— 这条不对齐可以推广成
  一条经验：**看到新特性，先假设"只有 Python 有"。** 它适合"已有 Java 服务
  接入 Ray 调度"这类集成场景；**"Java 服务要调 Python 模型"多数时候用
  Ray Serve + HTTP 更简单**（这是本书的判断，不是官方建议）。

**一句话收束整个附录**：这三个主题都是"**代码和环境怎么到达集群**"的不同
侧面，共同点是 —— **在笔记本上它们全都免费，在集群上它们全都有成本**。
在写第一行 `ray.init()` 之前先问一句"这份代码和这份依赖实际要经过多少台
机器、多少份拷贝"，就已经越过了本教程想教的那道线。

> **相关章节**：第 02 章（`address` 的四种写法）、第 04 章 §4.7（Jobs API）、
> 第 11 章（State API，含 `list_runtime_envs`）、第 17 章（部署与安全）、
> 第 21 章 A.7（`runtime_env` API 速查 + mini-ray 对照）、
> 第 22 章（mini-ray 工程手册）、第 24 章 **D.2 的 Q7**（Ray Client 的连接问题）。
