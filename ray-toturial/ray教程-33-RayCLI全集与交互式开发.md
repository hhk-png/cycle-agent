仓库地址：https://github.com/hhk-png/cycle-agent

# 第 33 章：Ray CLI 全集与交互式开发

> 本章目标：**把 `ray` 这个命令行工具用全**。
>
> 全书前面提到过 `ray status`、`ray logs`、`ray job submit`、`ray memory`、
> `ray debug`……但**没有一处把它们聚在一起**，也没有讲清
> 「哪些命令属于哪套体系、哪套已经过时、找不到东西时该敲哪一条」。
> 这导致一个很实际的后果：**很多人的 Ray 调试时间是浪费在"不知道有这个命令"上的**。
>
> 本章分两半：上半是 **CLI 速查**（可以当字典翻），
> 下半是**交互式开发**（Notebook / 调试器 / 快速迭代循环）。
>
> 所有命令以 **Ray 2.58** 为准；凡本书未能核对的，一律写"**未确认**"。
> **遇到任何一条对不上，先跑 `ray <子命令> --help`** ——
> Ray CLI 在版本间动得比 Python API 频繁得多，这是本章反复强调的一句话。

---

## 33.1 先理清 CLI 的四套体系

`ray` 的子命令**不是**按统一规则命名的，这是历史包袱。
把它分成四套，找命令时就不会乱：

| 体系 | 形态 | 典型命令 | 状态 |
|---|---|---|---|
| **① 集群生命周期** | `ray <动作>` | `start` / `stop` / `status` / `up` / `down` | 稳定 |
| **② 集群内查询（State CLI）** | `ray <list\|summary\|get> <资源>` | `ray list tasks` / `ray summary actors` / `ray get objects <id>` | 稳定，**最该先学** |
| **③ 独立子命令组** | `ray <组> <动作>` | `ray job` / `ray logs` / `ray serve` / `ray debug` | 各组规则不同 |
| **④ 传统集群启动器** | `ray <动作>` | `up` / `down` / `attach` / `exec` / `rsync` | **正在被 KubeRay 取代**（第 17 章） |

> ⚠️ **一条最容易记错的规则**：
> `ray list` / `ray summary` / `ray get` 是**三组子命令**，
> **没有** `ray state list` 这个命令 ——
> `ray.util.state.list_*` 是 **Python** 侧的写法（第 11 章 §11.2）。
> 而 `ray logs` 是**独立的子命令组**，不属于 `ray list` 体系。
> **别试图找规律，用 `--help`。**

---

## 33.2 集群生命周期

```bash
ray start --head --port=6379 --num-cpus=8 --num-gpus=0   # 起 head
ray start --address=<head>:6379 --num-cpus=16            # worker 加入
ray status                                               # 看集群状态(最常用)
ray stop                                                 # 停本节点
ray stop --force                                         # 清掉残留(端口占用时用)
```

⚠️ **`--num-cpus` 在容器/K8s 里必须显式设**。Ray 默认按**宿主机**核数算，
于是"以为有 64 核、cgroup 只给了 8 核"，任务超卖、集体变慢。
这是第 24 章 Q35 专门讲的一个 FAQ，也是新用户最常见的资源类困惑。

```bash
ray status          # 输出分三段:节点、资源使用、待处理需求(demand)
```

`ray status` 是**排查"任务为什么不跑"的第一条命令**（第 8 章 §8.9）：
* `Resources` 段：`0.0/8.0 CPU` 说明资源被占满了；
* `Demand` 段出现 `{"CPU": 4.0} x 3`，说明有 3 个任务在等 4 CPU ——
  这时候问题不是调度器，是**集群没那么大**；
* `Active` 段里的节点数对不上你的机器数 → 有节点没起来。

### 传统集群启动器（选读，正在过时）

```bash
ray up cluster.yaml        # 按 yaml 起云上集群
ray attach cluster.yaml    # ssh 进去
ray exec cluster.yaml "nvidia-smi"   # 远程执行一条命令
ray rsync_up cluster.yaml ./local /remote   # 同步代码
ray down cluster.yaml      # 拆掉
```

> ⚠️ **这条路正在被 KubeRay 取代**（第 17 章 §17.1）。
> 新项目优先 KubeRay；只有在"没有 K8s、又要管云 VM"时才用集群启动器。
> 另外注意：**autoscaler 的两种形态（集群启动器 / KubeRay）配置完全不同**，
> 这也是"autoscaler 不扩容"类问题最常见的根因（第 24 章 Q36）。

---

## 33.3 State CLI：最该先学的一组

这组命令是**第 11 章 State API 的命令行入口**，底层同一个数据源。
**先学这组，收益最大。**

```bash
# ── list:列出一类资源 ──
ray list nodes
ray list tasks       --limit=100
ray list actors      --detail
ray list objects
ray list workers
ray list jobs
ray list placement-groups
ray list cluster-events          # 控制面事件(节点加入/退出、GCS 重启…)
ray list runtime-envs            # 集群上在用哪些 runtime_env

# ── summary:聚合视图(先看这个,再看 list)──
ray summary tasks
ray summary actors
ray summary objects              # ⭐ 排查内存问题时先看这个,再看 ray list objects

# ── get:拿单个对象 ──
ray get tasks <task_id>
ray get actors <actor_id>
```

**过滤与筛选**（各子命令支持的旗标不同，**以 `--help` 为准**）：

```bash
ray list tasks --filter "state=FAILED"          # 只看失败的任务
ray list tasks --filter "state=RUNNING"
ray list actors --filter "state=ALIVE"
```

> ⚠️ **字段名陷阱（第 11 章 §11.2 讲过，这里再强调一次，
> 因为它会直接报 `KeyError`）**：
> * 对象大小是 **`object_size`**，不是 `size`；
> * 重试次数是 **`attempt_number`**（`num_attempts` 是 mini-ray 的叫法）；
> * `call_site` 需要先开 `RAY_record_ref_creation_sites=1`，
>   否则显示 `disabled`。
>
> 命令行版的输出是**格式化过的表格**，所以字段名问题在这里表现为
> "筛选不到东西"而不是报错 —— **更隐蔽**。想确认字段名，
> 用 `ray get <资源> <id>` 看单条的完整字段，或者回 Python 侧
> `ray.util.state.list_*()` 打印 **`[0].asdict()`**。
>
> ⚠️ **不是 `[0].keys()`** —— `ray.util.state` 返回的是 `StateSchema`
> 子类（dataclass），它为了向后兼容提供了 `__getitem__` / `get()` / `asdict()` /
> `columns()`，但**没有 `keys()`**，写 `keys()` 会 `AttributeError`。
> （顺带说明：本书大量使用的 `t["state"]`、`o.get("object_size", 0)` 这类
> **下标写法是成立的**，走的就是同一层兼容处理，不必改。）

### `ray memory`：内存排查专用

```bash
ray memory                          # 列出所有对象及引用它们的调用点
ray memory --stats-only             # 只要聚合统计,不列明细(集群很大时先跑这个)
ray memory --group-by STACK_TRACE   # 按调用栈分组(默认 NODE_ADDRESS)
ray memory --sort-by OBJECT_SIZE    # 按对象大小排序(默认值;也可给 PID / REFERENCE_TYPE)
```

> ⚠️ **`--group-by` / `--sort-by` 的取值是 click 的 `Choice`,大小写敏感**：
> `--group-by` 只接受 **`NODE_ADDRESS` / `STACK_TRACE`**，
> `--sort-by` 只接受 **`PID` / `OBJECT_SIZE` / `REFERENCE_TYPE`**。
> 写成小写短横线形式（`stack-trace` / `object-size`）会被 CLI 直接拒掉。

`ray memory` 回答的是**「谁在占着对象存储」**——
它把每个对象的**引用位置**（谁还攥着这个 ObjectRef）打出来。
第 7 章 §7.5 讲的"内存下不去的第一嫌疑永远是某个地方还攥着 ObjectRef"，
`ray memory` 就是找那个地方的命令。

> ⚠️ **它需要 dashboard 组件**（`ray[default]`）。
> 只装了裸 `ray` 的话这条命令会报缺依赖（第 24 章 Q1）。

---

## 33.4 `ray job`：把脚本提交到集群

这是**官方推荐的远端执行方式**（第 4 章 §4.7、附录 F §F.3.5）——
在 Ray Client 进入维护状态之后，它就是标准答案。

```bash
ray job submit --address=http://<head>:8265 \
  --working-dir=./my_project \
  -- python train.py --epochs 10

ray job list                       # 所有 job(含终态)
ray job status <submission_id>     # 单个 job 的状态
ray job logs <submission_id> -f    # 跟着看日志(--follow)
ray job stop <submission_id>       # 停掉
```

> ⚠️ **`--address` 是 dashboard 的 HTTP 地址（8265），不是 GCS 的 6379**。
> 这一点与 `ray start --address=` 完全不同 ——
> 前者是 Jobs API 的 REST 入口，后者是集群内部连接。
> **写错端口的报错是"连不上"，很容易误判成集群没起来。**

**`--working-dir` 是这条路最实用的一个旗标**：它把本地目录打包上传，
省掉了自己配 `runtime_env.working_dir` 的步骤（附录 F §F.2）。
代价是**每次提交都要重传**——目录大的话，改用 `runtime_env` 里指向共享存储的路径。

---

## 33.5 `ray logs`：子命令组这一形态已经很久了

⚠️ 老文档里的 `ray logs --actor-id=...` 这种扁平写法**早就不能用了** —— 但
**它不是 2.58 的新变更**：子命令形态至少自 **Ray 2.3**（PR #30422，2022-12）就存在，
Ray **2.5.1** 的官方文档里已经是五个子命令。
（本书早先把它写成"2.58 的变更"，那是**版本归属错误** ——
会让读者误判自己手上旧版本的行为。第 11 章 §11.4 已同步修正。）

```bash
ray logs actor  --id <actor_id> --follow     # 某个 actor 的日志
ray logs task   --id <task_id>  --err        # 某个 task(--err 看 stderr)
ray logs worker --pid <PID>                  # ⚠️ 某个 worker 进程:是 --pid!
ray logs job    --id <job_id>                # 某个 job
ray logs cluster raylet.out --tail 500       # 集群级:某节点上的某个日志文件
ray logs cluster raylet.out --node-id <NODE_ID>   # 指定节点
```

Commands 一共五个：**`actor` / `cluster` / `job` / `task` / `worker`**。

> ⚠️ **三个子命令的标识方式并不一致**：
> **`worker` 只认 `--pid`**（worker 的标识就是它的进程号）；
> **`actor` 两种都认** —— 给 `--id`、给 `--pid`、或者两个都给都行，
> CLI 的报错原文是 *"At least one of `--pid` and `--id` has to be set"*；
> `task` / `job` 只认 `--id`。
> 最容易记错的是 `worker`（它**没有** `--id`），
> 第 11 章 §11.4 与第 4 章 §4.5 都已按 `--pid` 改写。

> ⚠️ **`ray logs` 只能看到「还活着的节点」上的日志**
> （官方原文：*"only the logs from alive nodes are available"*）。
> **节点已经没了，日志就捞不回来** —— 这是"节点被抢占后查不到原因"的根本原因。
> 大集群必须接集中式日志（Loki/ELK），别指望这条命令（第 17 章 §17.4）。

---

## 33.6 `ray debug` 与 `ray timeline`

```bash
ray debug            # ⚠️ 这是【旧版/legacy】调试器的入口,详见下
ray timeline         # 导出 Chrome Trace(⚠️ 没有 --output,路径由它自己算并打印)
```

### `local_mode` 没了，但替代品有**两个**，别拼错

⚠️ **这是 2026 年最容易踩的一个变化**：

```python
ray.init(local_mode=True)     # ✗ 真实 Ray 已移除,直接 RuntimeError
```

**`local_mode` 在真实 Ray 里已经不存在了**（第 11 章 §11.7、第 16 章 §16.4）。
但替代它的调试路径有两条，**它们不是同一个工具的两个步骤**：

| | **Ray Distributed Debugger**（新，**2.39 起默认**） | **Ray Debugger**（legacy） |
|---|---|---|
| 怎么下断点 | 在 `@ray.remote` 里写 **`breakpoint()`** | 同样写 `breakpoint()` |
| 前端 | **VS Code 扩展**（Cursor 等 VS Code 系亦可） | 终端 **pdb** |
| 入口 | 扩展自动 attach | **`ray debug`** |
| 前置 | `pip install "ray[default]" debugpy` | **`RAY_DEBUG=legacy`**（如经 `runtime_env`） |
| 事后调试 | 设 **`RAY_DEBUG_POST_MORTEM=1`** → 未捕获异常时 Ray **冻结**该任务等接入（**默认关闭**） | 是更早 `RAY_PDB` 一脉的旗标 |

```python
# 新版（推荐）:什么都不用设,写断点 + VS Code 扩展 attach
@ray.remote
def f(x):
    breakpoint()          # ← 就在这里下断点
    return x * x

# 旧版(legacy):必须先把 RAY_DEBUG 设成 legacy,ray debug 才肯干活
ray.init(runtime_env={"env_vars": {"RAY_DEBUG": "legacy"}})
```

> ⚠️ **修正（第八轮）：`RAY_DEBUG_POST_MORTEM` 两套调试器都支持。**
> 第四轮说它"只在 legacy 下有效"、第五轮改成"只属于新版" —— **两次都不对**。
> 源码里 `ray/util/rpdb.py` 的 `_post_mortem()` 按 `RAY_DEBUG` 分支
> （`"1"` → 新版 debugpy；否则 → legacy 终端 pdb），
> 而调用侧只判 `RAY_DEBUG_POST_MORTEM`、不判 `RAY_DEBUG`，所以两条路都生效；
> 官方 legacy 文档的示例也把它和 `RAY_DEBUG=legacy` 一起设。
> 完整证据见第 11 章 §11.7 的对照表。
>
> 🔴 **本书第四轮在这里写错了，第五轮对着 2.58 的 `scripts.py` 改正**：
> 原文把 **`RAY_DEBUG=1` + `ray debug`** 当成了 Ray Distributed Debugger 的用法。
> 实际上 —— **`ray debug` 是 legacy 调试器的命令**。不设 `RAY_DEBUG=legacy` 时，
> 2.58 的 `ray debug` 会先打印：
> *"NOTE: The distributed debugger … is now the default … If you want to keep
> using 'ray debug' please set RAY_DEBUG=legacy in your cluster."*
> 也就是说 `RAY_DEBUG=1` **根本不是一个有效入口**：2.58 的代码里
> **只显式分支处理了 `"1"`（新版 debugpy 路径）和 `"legacy"` 两个值** ——
> 其它任何取值**两个分支都不进**，`breakpoint()` 会变成**静默 no-op**，
> 既不进新版也不进 legacy，连一条提示都没有。
> （所以"其余一律走新版"这个说法也是错的：不走新版，是**什么都不走**。）
> `ray debug` 列出的是 **"Active breakpoints"（活跃断点）**，
> 而且和 `breakpoint()` 是配套的 —— 不写断点，它没有东西可列。

> ⚠️ 另外注意：**`local_mode` 只是从真实 Ray 移除了** ——
> 本书配套的 mini-ray **仍然保留**它（那是它的教学特性，第 6 章）。
> 所以 mini-ray 上跑得好好的调试代码，搬到真实 Ray 会直接报错。
> 这是"两边都叫同一个名字、但一边有一边没有"的又一处，值得专门记一下。

### `ray timeline` 的先决条件

```bash
RAY_PROFILING=1 RAY_task_events_report_interval_ms=0 python my_script.py

# ⚠️ ray timeline 只有一个选项 --address,【没有】--output。
#    它把 JSON 写到自己算的一个临时路径下,并把那个路径**打印出来**,
#    照着打印结果去拖就行:
ray timeline

# 想在脚本里指定文件名,直接用 API(而不是 CLI):
python -c "import ray; ray.init(); ray.timeline('timeline.json')"
# 然后拖进 chrome://tracing / Perfetto / speedscope
```

> ⚠️ **两个变量缺一个，你都会得到一个空文件 —— 而且它不报错。**
> 缺了只会打一条 warning（原文含 *"make sure
> RAY_task_events_report_interval_ms=0"*），随后 `ray.timeline()`
> **照样把文件写出来**，内容是 `[]`。
> 这是「timeline 用不了」的头号原因（第 11 章 §11.6）。
> 这个坑值得单独记住，因为**它看起来是成功的**，只是结果没用。
>
> 📌 顺带澄清：`ray timeline` CLI 本身不会因为没开 profiling 而失败 ——
> 它只是调 `ray.timeline(filename=...)` 打个文件到 Ray 临时目录。
> 唯一会让它抛异常的情况是 **Ray 还没 `init`**
> （`RuntimeError: Ray has not been started yet. Timeline requires Ray to be
> initialized first.`），那与 profiling 无关。

---

## 33.7 其他常用命令

```bash
ray health-check            # 探活(⚠️ hidden 命令,官方声明"NOT a public API",见下)
ray status --verbose        # 集群资源与 autoscaler 的详细视图(第 8 章 §8.8)
ray dashboard <cluster.yaml>  # 把 dashboard 端口转发到本地(集群启动器场景)
ray microbenchmark          # 内置微基准:测本机/集群的基础性能
ray stack                   # 批量 dump 所有 worker 的 Python 栈(卡住时用)
ray cluster-dump            # 打包诊断信息(提 issue / 提工单时用)
```

| 命令 | 什么时候用 |
|---|---|
| `ray health-check` | CI 里等集群就绪（附录 G §G.2）；⚠️ 但它是 **`hidden=True` 的命令**，docstring 原文写着 *"This is NOT a public API."* —— 更稳的 CI 检查用 `ray status` 的退出码，或直接打 dashboard 的 `/api/healthz` |
| `ray status --verbose` | 确认 autoscaler 是不是 v2、哪个 node type 在扩容 |
| `ray stack` | **进程卡住不动**时的第一手证据（第 31 章 §31.3） |
| `ray cluster-dump` | 准备提 issue / 工单时，一次性收集日志与状态 |
| `ray microbenchmark` | 怀疑"是不是机器本身就有问题"时做基线 |

> **`ray stack` 到底是什么**：Ray 的进程模型是"一个 driver + N 个 worker +
> 若干 actor"，卡住时你**不知道卡在哪个进程里**。`ray stack` 帮你一次性
> 把所有相关进程的 Python 栈打出来 —— 省掉手工 `for pid in $(pgrep -f "ray::")` 那一步。
>
> ⚠️ **但它不是另一个工具**：`ray stack` **内部就是调用 `py-spy`**，
> 官方明确要求 **`py-spy` 已安装**。两个直接后果：
> ① 没装 py-spy 的环境里，"先 `ray stack` 定位"这一步会**直接失败**；
> ② 第 31 章 §31.3 讲的 ptrace/capability 容器限制，对 `ray stack` **同样成立**。
> 把它理解成"**对全部 worker 批量跑 py-spy 的便捷包装**"最准确。
> **先 `ray stack` 定位进程，再用 `py-spy record` 深挖那个进程**，是更省时间的顺序。

### `ray serve`：§33.1 那张表里承诺、但上面一直没兑现的一组

§33.1 把 `ray serve` 列为「独立子命令组」的典型，前面却一次都没展开。
补在这里（完整背景见第 15 章）：

```bash
ray serve deploy config.yaml       # 用 YAML 配置部署(生产常用,替代 @serve.deployment)
ray serve status                   # 看每个 deployment 的副本数/健康/路由
ray serve config                   # 打印当前生效的完整配置
ray serve run app:deployment       # 本地跑一个 deployment(开发用,--blocking 保持前台)
ray serve shutdown                 # 关掉当前 Serve 实例
```

| 命令 | 什么时候用 |
|---|---|
| `ray serve status` | **服务请求返回 503 / 超时**时第一个该敲的命令 —— 看副本是不是没起来 |
| `ray serve config` | 确认 `http_options` / `proxy_location` / 自动扩缩参数真的生效了 |
| `ray serve run` | 改完代码本地验证，不必走完整 KubeRay 流程 |
| `ray serve deploy` | 生产环境用声明式 YAML 拉起，配合 CI（附录 G §G.2） |

> ⚠️ **`serve deploy` 与 `serve run` 不是同一件事**：
> `serve run` 是**开发态**（前台、绑当前目录、Ctrl-C 就停），
> `serve deploy` 是**声明式**（提交 YAML 给已在运行的集群）。
> 用混了会得到"我明明部署了但 `status` 里什么都没有"这类现象。

---

## 33.8 交互式开发：Notebook 工作流

Ray 在 Jupyter 里用得很多（探索性数据处理、模型调试），但有几条**特有的注意事项**。

### `ray.init()` 在 Notebook 里只该调一次

Notebook 的 cell 可以反复执行，而 `ray.init()` 重复调用会报
`ReinitializationError`。两个解法：

```python
# ① 幂等初始化(推荐)
import ray
if not ray.is_initialized():
    ray.init()

# ② 或者接受重init(不推荐,容易掩盖状态问题)
ray.init(ignore_reinit_error=True)
```

> ⚠️ **更隐蔽的问题**：Notebook 里反复执行**定义 `@ray.remote` 函数的 cell**，
> 每次都会定义**新的**函数对象。已提交的任务仍指向旧定义，
> 于是你会看到"我明明改了函数，结果没变"。
> **解法是改完重新 `ray.init()`（或重启 kernel）**，
> 别指望热更新 —— 函数的定义在提交那一刻就被序列化走了（第 5 章 §5.4）。

### `RAY_DEBUG` 与 Notebook 的关系

⚠️ **在 Notebook 里单步调试 Ray 任务是个已知的痛点**。
`local_mode` 已被移除（§33.6），而 **Ray Distributed Debugger 是一个
VS Code 扩展**（底层是 debugpy）—— `ray debug` 只是它的 **legacy** 入口，
不是它本身；不管哪条路，在 Notebook 里都用不顺手。
**未确认**：2.58 是否有官方的 Notebook 内调试方案。
实践上更可行的路径是：**把要调试的逻辑抽成普通函数，在 Notebook 里直接调用调试**，
只有验证通过后才包成 `@ray.remote`。

### 环境变量与依赖

Notebook 里 `pip install` 装的包，**worker 进程不一定有**
（worker 是另外拉起的进程）。这与 §32.7 坑 2 是同一个问题：

```python
ray.init(runtime_env={"pip": ["my-internal-pkg"]})   # 让 worker 也装上
```

### `ray.widgets`：Notebook 里的富文本展示（锦上添花，不是必需）

先说结论：**它是一个"有更好、没有也完全不影响功能"的模块**，
本书前 36 章一次都没提它，是因为它不参与任何分布式机制 ——
但既然这一章讲交互式开发，就该把它的定位讲准。

**它是什么**：`ray.widgets` 是 Ray 自己的一个 `@DeveloperAPI` 模块
（2.58 源码里的 `python/ray/widgets/`）。它导出的公开名字只有两个：

| 名字 | 出处 | 作用 |
|---|---|---|
| `Template` | `ray.widgets.render` | 用 Jinja2 模板渲染 HTML 片段 |
| `make_table_html_repr` | `ray.widgets.util` | 把对象的属性渲染成一张 HTML 表格 |

**它怎么用**：你**几乎不会直接调用它** —— 它是 Ray 各类对象
`_repr_html_()` / `_repr_mimebundle_()` 的实现细节。
比如 `ray.data.Dataset` 在 `dataset.py` 里就写着：

```python
from ray.widgets import Template
from ray.widgets.util import repr_with_fallback

@repr_with_fallback(["ipywidgets", "8"])     # ← 2.58 源码里的真实装饰器
def _repr_mimebundle_(self, ...): ...
```

所以**在 Jupyter 里 `ds` 敲回车会看到一张漂亮的表**（schema / 行数 / 分片），
在终端里则退化成一串纯文本 —— 这就是 `ray.widgets` 的全部可见效果。

**前提条件（缺一个就自动退回纯文本，而且不会报错）**：

* 必须在 **Jupyter Notebook** 里（源码里的判据是
  `get_ipython().__class__.__name__ == "ZMQInteractiveShell"`）；
* 必须装了 **`ipywidgets`（且 ≥ 8）** —— 缺了只会打一条
  *"Run `pip install -U ipywidgets`, then restart the notebook server
  for rich notebook output."*；
* 它**故意在 IPython 终端和 Google Colab 里关闭** ——
  `repr_with_fallback` 的 docstring 把这两条列成了明确的排除项。

```bash
pip install "ipywidgets>=8"     # 唯一需要做的事
```

> ⚠️ **诚实定位**：**别为了它去改架构或排故障。**
> 它不影响任何调度、性能或正确性；显示成表格还是纯文本，
> 对你在 §33.9 那条定位流程图上的工作没有任何影响。
> 它唯一的价值是**降低"看一眼数据结构"的成本** ——
> 而当你真的需要**集群级**的可视化时，该去的是 Dashboard（第 11 章）
> 和第 31 章的 timeline，那才是能回答"慢在哪"的东西。
>
> 📌 如果你想在**自己的**类上复用这套渲染，
> 直接 `import` 它即可（它是 `@DeveloperAPI`，会尽力保持兼容，
> 但不像 `ray.data` / `ray.train` 的公开 API 那样有稳定性承诺）。
> 具体签名以 `ray/widgets/util.py` 为准 —— 本书未逐版本核对它的参数变化。

---

## 33.9 一条完整的定位路径

把本章的命令串成一条**实际的排查流程**。这是本章最该带走的东西：

```
现象:任务提交了但一直不跑
  ① ray status                      → 看 Resources / Demand(§33.2)
       ├ 资源占满 → 集群不够大,或有人占着不放(ray list actors)
       └ 资源够但没动 → 往下
  ② ray list tasks --filter "state=PENDING"
       └ 看卡在哪个 state(第 5 章 §5.1 的 14 个状态)
  ③ ray list actors                 → 有没有 actor 占着资源不干活

现象:任务跑起来了但是失败
  ① ray list tasks --filter "state=FAILED"
  ② ray get tasks <task_id>          → 看 error 字段
  ③ ray logs task --id <id> --err    → 看完整 stderr(§33.5)

现象:任务卡住不动(不报错、不结束)
  ① ray stack                        → 先定位是哪个进程卡住(§33.7)
  ② py-spy dump --pid <pid>          → 再看那个进程的栈(第 31 章 §31.3)
  ③ ray list tasks                   → 确认它到底在 RUNNING 还是 PENDING

现象:内存下不去 / 对象存储满
  ① ray memory                       → 谁在占着对象(§33.3)
  ② ray list objects                 → object_size 排序(注意字段名)
  ③ 第 7 章 §7.7 的四类事故对照表

现象:节点被抢占后想查原因
  ⚠️ ray logs 捞不回来了(§33.5) → 只能翻集中式日志
```

> **这张图和第 11 章 §11.8 的「按症状排查手册」是同一件事的两个视图**：
> 那张表按**症状**组织、给的是 **Python API**；这张图按**命令**组织。
> 实际用的时候先在这张图上定位到命令，拿到数据后再回第 11 章查字段含义。

---

## 33.10 与 mini-ray 的关系

mini-ray 有一个刻意做薄的 CLI（`miniray/cli.py`）：

```bash
python -m miniray status --num-cpus 8    # 类似 ray status
python -m miniray demo                    # 内置演示(含 timeline 生成)
python -m miniray version
```

**为什么只做三条**：mini-ray 的所有状态都在 driver 进程里，
没有"跨进程查询"这回事 —— 所以 `ray list`/`ray summary`/`ray get`
这一整套 State CLI 在它这里退化成**几个 Python 函数**
（`miniray.state.list_tasks()` 等，第 11 章 §11.10）。

| 真实 Ray CLI | mini-ray 的对应 |
|---|---|
| `ray status` | `python -m miniray status` ✅ |
| `ray list tasks` / `summary` / `get` | `miniray.state.list_*()` / `summarize_*()`（Python 函数） |
| `ray start` / `ray stop` | `miniray.init()` / `miniray.shutdown()`（进程内） |
| `ray logs` | 直接继承终端输出（第 22 章 B.11 的取舍清单） |
| `ray job submit` / `ray debug` / `ray memory` / `ray timeline` | ❌ 不做 |

> **教学上的价值**：对比一下会发现 ——
> **真实 Ray 的 CLI 有一大半是为了「跨越进程边界」而存在的**
> （连集群、查远端状态、把远端日志捞回来、往远端提交作业）。
> mini-ray 把整个控制面压进一个进程之后，这一层**整体消失了**。
> 这反过来印证了第 3 章的那句话：
> **分布式系统的复杂度，几乎全部来自"你看不见另一台机器"这件事。**

---

## 33.11 本章小结

* **CLI 分四套**：集群生命周期 / State CLI / 独立子命令组 / 传统集群启动器。
  **没有统一命名规律，用 `--help`。**
* **`ray status` 是排查的第一条命令**；`Resources` + `Demand` 两段就能判断
  "是集群不够大，还是调度出问题"。
* **State CLI（`ray list/summary/get`）最该先学**；
  注意 **`object_size` / `attempt_number`** 两个字段名，
  以及 **`call_site` 需要先开记录开关**。
* ⚠️ **`ray logs` 是子命令组**（`ray logs actor|task|worker|job|cluster`），
  从 **Ray 2.3** 起就是这样 —— **不是 2.58 的新变更**（本书早先写错了版本归属）。
  注意 **`worker` 只认 `--pid`、`task` / `job` 用 `--id`、`actor` 两者都认**；
  而且**只能看活着的节点**。
* ⚠️ **`local_mode` 已移除**，交互式调试改用 **Ray Distributed Debugger**
  （remote 函数里 `breakpoint()` + VS Code 扩展）。
  **注意：`ray debug` 不是它的入口** —— `ray debug` 属于 **legacy** 调试器，
  要么先设 `RAY_DEBUG=legacy`，要么就用新版扩展。
  **mini-ray 仍保留 `local_mode`** —— 别把两边的经验互搬。
* ⚠️ **`ray timeline` 必须先开 `RAY_PROFILING=1` 且
  `RAY_task_events_report_interval_ms=0`**，
  否则**不报错、只 warning，并写出一个内容为 `[]` 的文件** ——
  "看起来成功了，其实什么也没有"。
* **`ray stack` 是"卡住"场景的第一手证据** ——
  但它**内部就是批量调用 `py-spy`**，所以**必须先装 py-spy**，
  且第 31 章 §31.3 的 ptrace/capability 限制对**它同样成立**。
* **Notebook 里**：`ray.is_initialized()` 做幂等 init；
  **改完 remote 函数要重启 kernel**（定义在提交那刻就被序列化了）；
  worker 的依赖要用 `runtime_env` 送。
* **`ray.widgets` 是"锦上添花"而非必需**（§33.8）：
  它只负责 Notebook 里把 `Dataset` 之类的对象渲染成 HTML 表格，
  前提是 **Jupyter + `ipywidgets>=8`**；缺了会**自动退回纯文本且不报错**。
  **不要为它改架构或排故障** —— 集群级可视化该看 Dashboard 与 timeline。
* **§33.9 那张定位流程图是本章的收口** ——
  按现象走到命令，再回第 11 章查字段含义。

---

> **本章在全书的位置**：它是一份**操作手册**，不是新概念 ——
> 所有底层机制在第 3、11、31 章已经讲过。
> **它后面还有 6 章**（34 表格数据与传统 ML、35 数据版本与模型注册、
> 36 分布式追踪与 OTel、37 LLM 推理引擎与性能优化、
> 38 Ray 与 Agent 工作负载、39 源码阅读与事实核查指南）
> —— 本书早先在这里写过"把它放在最后"，那是过时的：
> 第四轮修订在后面追加了三章，第六轮追加了第 37 章，
> 第七轮追加了第 38 章，**第八轮又追加了第 39 章**。
> 建议的用法：把 §33.9 的流程图截屏存下来，遇到问题时按图索骥一遍。

> ⚠️ **另外两处同类的过时说法**（第四轮修订一并修正，列在这里便于对照）：
> ① 第 31 章 §31.11 曾自称"全教程回顾 / 全书到此结束"——
>    它其实是**「机制与调优」这条主线的收尾**，不是全书结尾；
> ② 第 32 章结尾曾写"**下一章**：到这里教程就完整了"，
>    但它后面还有 33–39 七章。**三章各自主张自己是全书结尾，现在统一了 ——
>    而第七轮新增第 38 章、第八轮新增第 39 章之后，收尾点再次后移到了第 39 章。**
