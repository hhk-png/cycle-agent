仓库地址：https://github.com/hhk-png/cycle-agent

# 第 11 章：可观测性与调试

> 本章目标：把「出问题了怎么查」变成一套可执行的流程。
> 包含四层可观测性（状态/日志/指标/追踪）、每层的工具与坑，
> 一份按症状索引的排查手册，以及一个真实事故的复盘。

---

## 11.1 四层可观测性

| 层 | 回答的问题 | Ray 的工具 | 什么时候用 |
|---|---|---|---|
| **状态（State）** | 现在集群里有什么、在干什么 | `ray.util.state` / Dashboard / `ray status` | 第一个该看的 |
| **日志（Logs）** | 具体的错误信息与堆栈 | driver 日志 + worker 日志（自动转发）/ `ray logs` | 定位到具体任务后 |
| **指标（Metrics）** | 趋势、水位、速率 | Prometheus exporter / OTel | 长期监控、容量规划 |
| **追踪（Tracing）** | 一次请求经过哪些组件、时间花在哪 | `ray.timeline` / Task events / Distributed Debugger | 性能问题 |

> ⚠️ **这张表里"追踪"那一行有个名实不符的地方，本书第四轮修订专门纠正了**：
> 上面列的三个工具（timeline / task events / Distributed Debugger）
> **都不是"追踪"** —— 它们是"看单点"的工具。
> 真正回答「**一次请求跨了 5 个 actor，慢在哪一段**」的，
> 是**分布式追踪**（OpenTelemetry / Jaeger 那一套）——
> 而本书前三轮**没有任何一章真正讲过它**
> （⚠️ 第五轮纠正：早先这里写的是"`OpenTelemetry` 在前 35 章命中数是 0"，
> 那是**自我否证**的 —— 这一行本身就算一处命中。改成定性表述）。
> **那一层现在在第 36 章**，包括：`opentelemetry.context` 为什么跨不过
> `ray.remote`、怎么把 context 当参数传、以及怎么把 Ray 的 ID 写进 span
> 来和第 11 章的 State API 对上。
>
> **什么时候去第 36 章**：当你的问题是"**这条请求**为什么慢"，
> 而不是"**整体**哪里不对"时。两者是不同的问题（第 36 章 §36.1 有分工表）。

**使用顺序建议**：状态 → 日志 → 指标 → 追踪。
很多人一上来就翻日志，结果被几万行输出淹没；先看状态能把范围缩小 90%。

**这四层的分工，一句话记**：
**指标告诉你"出问题了"，追踪告诉你"问题在哪一段"，日志告诉你"那一段里发生了什么"。**
（timeline 是第四个维度 —— 机器视角，不是追踪的替代品。）

---

## 11.2 State API：最直接的一层

> ⚠️ 官方标注 State API 为 **alpha**，需要 `ray[default]`。

```python
from ray.util import state

# 聚合视图(先看这个)
state.summarize_tasks()          # 各状态个数 / 重试数 / 耗时统计
state.summarize_objects()        # 对象数 / 内存占用 / 溢出与驱逐计数
state.summarize_actors()         # actor 状态分布

# 列表视图
state.list_tasks()               # 每个任务:状态/耗时/重试/节点/worker
state.list_objects()             # 每个对象:object_size/所在节点/reference_type/call_site
state.list_actors()              # 每个 actor:状态/节点/重启次数
state.list_nodes()               # 节点:资源总量/可用量/标签
state.list_workers()             # worker 进程:pid/类型/已执行任务数
state.list_placement_groups()
state.list_runtime_envs()        # 每个 runtime_env 的状态(装好了没、耗时多久)
state.list_jobs()                # 提交过的 job(配合 Jobs API,见第 4 章 §4.7)
state.list_logs()                # 日志文件清单(哪个节点/哪个文件)
state.list_cluster_events()      # 集群事件:节点加入/退出、autoscaler 决策

# 单个对象
state.get_task(task_id) / state.get_actor(actor_id) / state.get_objects(...)
```

**三个容易被忽略但很关键的函数**：

* **`list_runtime_envs()`** —— 当任务卡在 `PENDING` 且你怀疑是依赖没装好时，
  这是唯一能直接看到「runtime_env 装到哪一步、失败没有」的接口，
  比翻日志快得多（配合附录 F 的 runtime_env 排错）；
* **`list_cluster_events()`** —— 「节点为什么突然没了 / autoscaler 为什么缩容了」
  的答案在这里，而不是在任务列表里；
* **`list_logs()`** —— 先列出**有哪些日志文件**，再去读。
  `ray logs` 的目标名（actor id / worker id / 节点 ip）经常写错，
  先用 `list_logs()` 确认有什么，能省很多时间。

命令行版本（生产环境更好用，不用写代码）：

```bash
ray list tasks                     # 列任务(可加 -f state=PENDING 这类过滤)
ray summary tasks                  # 聚合视图(按状态/类型分组计数)
ray summary objects                # 对象存储的聚合视图(查内存先看这个)
ray list objects                   # 列对象
ray get tasks <task_id>            # 看单个实体的详情(⚠️ 复数:ray get tasks)
ray status                         # 集群资源与自动扩缩状态
```

> ⚠️ **三处最容易记错的拼写**（第 33 章 §33.3 有完整对照）：
> ① `ray get` 后面的资源名是**复数**（`ray get tasks <id>`，不是 `ray get task`）；
> ② `ray list logs` **不存在** —— 对应的 Python API 是
> `ray.util.state.list_logs()`，CLI 侧走 `ray logs cluster`；
> ③ 对象的字段是 **`object_size`** 不是 `size`（见下面 §11.4 的字段表）。

⚠️ **`ray logs` 是自成一体的子命令组**（`ray logs [OPTIONS] COMMAND`），
老文档里的 `ray logs --actor-id=...` 这种**扁平写法早已不可用** ——
子命令形态自 **Ray 2.3**（PR #30422）就存在，**不是 2.58 的新变更**：

```bash
ray logs cluster raylet.out --tail 500     # 集群级:看某个节点上的某个日志文件
ray logs actor --id <actor_id> --follow    # 某个 actor 的日志
ray logs task --id <task_id> --err         # 某个 task 的日志(--err 看 stderr)
ray logs worker --pid <PID>                # ⚠️ worker 只认 --pid;actor 两种都认
ray logs job --id <job_id>                 # 某个 job 的日志
```

Commands 一共五个：**`actor` / `cluster` / `job` / `task` / `worker`**。

> ⚠️ 注意 CLI 的形状：`ray list <resource>` / `ray summary <resource>` /
> `ray get <resource> <id>` 是三组子命令，**没有** `ray state list` 这个命令
> （那是 **Python** 侧 `ray.util.state.list_*` 的写法）。
> 而 `ray logs` 自成一组**子命令组**，与 `ray list` 体系并列 ——
> 命名不一致是 Ray CLI 的历史包袱（`worker` 用 `--pid`、其余用 `--id` 就是例子），
> 别试图找规律，**用 `--help` 确认**。
>
> ⚠️ 另外记住：**`ray logs` 只能看到「还活着的节点」上的日志**
> （官方原文 *"only the logs from alive nodes are available"*）。
> 节点已经没了，日志就得靠集中式日志系统去捞（见下文）。

### 实战：三个最常用的查询

```python
# ① 哪些任务卡住了?卡在哪一步?
from ray.util import state
stuck = [t for t in state.list_tasks() if t["state"].startswith("PENDING")]
for t in stuck[:10]:
    print(t["state"], t["name"], t["task_id"])

# ② 哪些对象最占内存?谁创建的?
#    字段名是 object_size(不是 size);call_site 需要先开记录开关,见下
objs = sorted(state.list_objects(), key=lambda o: -o.get("object_size", 0))[:10]
for o in objs:
    print(o["object_size"] / 1e6, "MB", o["object_id"], o.get("call_site"))

# ③ 哪些任务重试过?(环境抖动的信号)
#    字段名是 attempt_number(不是 num_attempts —— 那是 mini-ray 的叫法)
retried = [t for t in state.list_tasks() if t.get("attempt_number", 1) > 1]
print(f"{len(retried)} 个任务被重试过")
```

> ⚠️ **`call_site` 默认是空的。** 引用创建点默认**不记录** ——
> 必须用 `RAY_record_ref_creation_sites=1` 启动 Ray，否则所有 `call_site`
> 都会显示成 `disabled`，你会以为"排查不出泄漏"，其实是开关没开。
> 这是"内存一直涨却找不到谁创建"最常见的原因
> （见第 07 章 §7.7 的「四类内存事故」）。

**还有两个必须知道的使用约束**（否则大集群上会既慢又不准）：

* **State API 不是快照**：官方明确说返回结果可能
  **过期（stale）、不完整（partial）、被截断（truncated）** ——
  单次查询默认上限 100 条（`DEFAULT_LIMIT`）、超时 30 秒，
  十万级实体以上可能拿不全，已结束的资源还可能已被 GC 掉。
  所以**务必带过滤条件**，别拉全量：

  ```python
  state.list_tasks(filters=[("state", "=", "PENDING")], limit=50, timeout=10)
  ```

* **Log API 只能拿存活节点的日志**。节点一旦被回收，它的历史日志就取不到了 ——
  生产上要靠集中的日志系统（Loki/ES）或 KubeRay 的 History Server，
  而不是指望事后用 `ray logs` 去捞。

---

## 11.3 Dashboard：好看，但有代价

`ray[default]` 自带 Dashboard（默认 `http://localhost:8265`），包含：
Jobs（提交与查看 job）、Cluster（节点与资源）、Actors、Tasks、Logs、
Metrics、Ray Data / Ray Train / RLlib 的专用面板（部分仍标 beta）、
以及 **Ray Distributed Debugger** 的入口。

**三个必须知道的坑**：

1. **默认没有鉴权** —— 这是被实际利用过的攻击面（CVE-2025-62593，
   CVSS 9.4：DNS rebinding + User-Agent 绕过，浏览器打开恶意页面即可在
   本机执行命令；2026-08-17 被 CISA 列入已知被利用漏洞目录）。
   **不要把 8265 暴露到不可信网络**；
2. **它在 head 节点上跑**，会消耗 head 节点的资源与网络带宽；
3. **不要把它当监控系统**：长期指标请接 Prometheus（见 11.5）。

```bash
# 关闭 dashboard(安全要求严格的环境)
ray start --head --include-dashboard=false
# 或 ray.init(include_dashboard=False)
```

---

## 11.4 日志：worker 的日志去哪了

Ray 的日志模型：

```
driver 进程 ────────────▶ 你的终端(直接打印)
worker 进程 ── 转发 ──▶ driver 的终端(带 (pid) 前缀)
             └────────▶ 节点上的日志文件(session 目录下)
```

相关配置：

| 配置 | 默认 | 作用 |
|---|---|---|
| `log_to_driver` / `RAY_LOG_TO_DRIVER` | `true` | 是否把 worker 日志转发到 driver |
| `RAY_LOG_TO_STDERR` | `false` | 转到 stderr 而不是 stdout |
| `RAY_DEDUP_LOGS` | `true` | **日志去重**（相同消息只打一次 + 计数） |
| `RAY_DEDUP_LOGS_AGG_WINDOW_S` | 5 | 去重窗口 |
| `logging_level` / `RAY_LOGGER_LEVEL` | INFO | 日志级别 |

```bash
ray logs worker --pid=<PID>              # 按 worker 进程查（注意是 --pid，不是 --id）
ray logs actor  --id=<ACTOR_ID>          # 按 actor 查
ray logs task   --id=<TASK_ID>           # 按 task 查
ray logs job    --id=<SUBMISSION_ID>     # 按 job 查
ray logs cluster <filename> --node-id=<NODE_ID>   # 按节点捞整个文件
ray logs cluster <filename> --follow             # 跟随
```

> ⚠️ **`--pid` 的用法和别的不一样，别试图找规律**：`task` / `job` 用 `--id`；
> `worker` 用 `--pid`（**没有** `--id`）；而 `actor` **两者都接受**
> （`--id` 按 ActorID、`--pid` 按进程号，见第 33 章 §33.5 的对照表）。
> 敲之前用 `--help` 确认一下最省事。
> **扁平写法 `ray logs <worker_id>` 早已不可用** —— 子命令形态自 **Ray 2.3**
> （PR #30422）就存在了，不是 2.58 的新变更。

**最常见的困惑**：「我的 worker 里 print 的消息去哪了？」
默认会转发到 driver（所以终端里能看到，只是带 pid 前缀且可能交错）。
如果设了 `RAY_LOG_TO_DRIVER=false`，就去节点的日志目录里找。

**大集群建议**：把日志导到集中式系统（Loki/ELK），别指望 Dashboard 的日志面板。
⚠️ **2.58 引入了一个迁移开关，但默认是关的**：
`RAY_enable_task_events_to_dashboard_head`（`ray_config_def.h` 里默认 **false**）。
打开后，事件由 aggregator 从 GCS 热路径导出到 head 上的内存存储，
state API / timeline 改从 head 读 —— 目的是降低大集群的控制面开销。
**升级到 2.58 后如果发现 task events 变少或字段缺失，先查这个开关**；
反过来，如果你没开它，就不要以为"2.58 已经默认搬家了"。

---

## 11.5 指标：接 Prometheus

Ray 自带 metrics exporter：

```python
ray.init(..., _metrics_export_port=8080)     # 或 ray start --metrics-export-port=8080
```

```
Prometheus ── scrape ──▶ <node>:8080/metrics
```

关键指标族（按用途）：

| 用途 | 指标示例 |
|---|---|
| 资源水位 | `ray_node_cpu_utilization`、`ray_node_mem_used`、`ray_node_disk_usage` |
| 对象存储 | `ray_object_store_memory`、`ray_object_store_available_memory`（溢出/驱逐计数） |
| 任务/actor | `ray_tasks`（按 state/name）、`ray_actors` |
| 调度 | `ray_scheduler_...`（各调度阶段的耗时/排队数） |
| Serve | 请求数/延迟/错误率/排队长度（`ray_serve_*`），LLM 场景还有专门的 dashboard |
| 自动扩缩 | 节点数、待处理 demand |

**两个必须知道的现实问题**：

1. **2.52–2.56.1 的 OTel metrics recorder 死锁**（2.57 修复）：
   该 bug 会冻结 head 节点的 dashboard agent，导致**提交的 job 永远不启动**。
   如果你的版本在这一区间，升级到 2.57+；
2. **指标缺失也是 bug**：Ray Data 有多个背压策略，
   但**从不暴露「是哪个策略阻塞了 operator」**（官方 issue #65607 自认），
   所有情况看起来都是 `[backpressure]`，只能靠翻 executor 日志手工追 ——
   这是可观测性上的真实缺口，遇到时不要怀疑自己。

### 应用自定义指标：`ray.util.metrics`

上面这些 `ray_*` 指标都是**系统级**的。你自己的业务指标（队列长度、
推理延迟、缓存命中率）要接进同一个 Prometheus 端点，用的是 `ray.util.metrics`——
**这是整个 Ray 里最实用、却最少被讲到的 API 之一**：

```python
from ray.util.metrics import Counter, Gauge, Histogram

# ⚠️ 在模块顶层创建(或 ray.init 之后),不要放进任务函数里反复创建
req_total = Counter(
    "app_requests_total",
    description="处理的请求总数",
    tag_keys=("model", "status"),        # ← 标签必须先在这里声明
)
queue_depth = Gauge("app_queue_depth", description="当前排队深度")
latency = Histogram(
    "app_latency_seconds",
    description="推理延迟",
    boundaries=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0],   # 自定义分桶
)

@ray.remote
def infer(batch):
    queue_depth.set(len(batch))
    start = time.perf_counter()
    result = model(batch)
    latency.observe(time.perf_counter() - start)     # 手动计时
    req_total.inc(1, tags={"model": "bert", "status": "ok"})
    return result
```

> ⚠️ **真实 Ray 的 `Histogram` 只有 `.observe(value, tags=...)`，没有 `.timer()`**。
> 网上（包括一些官方示例的早期版本）流传的 `with hist.timer():` 会 `AttributeError`。
> 计时器要自己写 —— 就上面那三行。
>
> 本教程配套的 mini-ray **额外提供了** `.timer()` 上下文管理器
> （`mini-ray/miniray/util/metrics.py`），那是为了演示方便加的教学扩展，
> **不是 Ray 的 API**。用真实 Ray 时不要照抄。

三种类型的语义：

| 类型 | 方法 | 用途 |
|---|---|---|
| `Counter` | `.inc(value=1, tags={...})` | **只增不减**：请求数、错误数、处理的字节数 |
| `Gauge` | `.set(v)` | **瞬时值**：队列长度、在飞请求数、缓存占用 —— ⚠️ Gauge **只有 `.set()`**，没有 `.inc()` / `.dec()`；要自增就自己 `.set(current + 1)`，或改用 `Counter` |
| `Histogram` | `.observe(v)`（Ray 没有 `.timer()`） | **分布**：延迟、batch 大小（产出分位数） |

**四个必须知道的约束**（不知道就会白折腾）：

1. **指标是「每个进程一份」的。** 每个 worker 进程都有自己的指标注册表，
   Prometheus 从这个节点的 `--metrics-export-port` 上把它们**汇总**抓走。
   所以你在 driver 里创建的 Counter，**不会**自动出现在 worker 里 ——
   想统计 worker 内的事件，就得让 worker 里也有一份（通常放在模块顶层，
   被 import 时创建）。
2. **标签必须先声明。** `tag_keys` 里没写的键，`inc(tags={...})` 时会报错。
   这是**故意的**：Prometheus 的标签基数（cardinality）一旦爆炸，
   整个监控系统都会挂 —— 所以 Ray 强制你显式列出。
3. **绝不要用高基数标签。** 把 `user_id`、`request_id`、`object_ref` 当标签，
   会瞬间产生百万级时间序列，**把 Prometheus 打挂**。
   高基数的东西该进日志或 trace，不该进指标。
4. **创建时机**：在 `ray.init()` 之后（或模块顶层）创建。
   在任务函数**内部**每次创建会重复注册，行为依版本而异（**未确认**）。

**验证它接上了**：

```bash
curl -s localhost:8080/metrics | grep app_          # 应该能看到你的指标
```

> **为什么值得单独讲**：绝大多数人止步于「Ray 有 Dashboard」，
> 于是业务指标只能靠 print 和日志。而 `ray.util.metrics` 让你用**几行代码**
> 就把业务指标接进和系统指标同一个 Prometheus —— 后面接 Grafana 告警时，
> 「GPU 利用率」和「我的队列深度」就能画在同一张图上，这是排查
> 「到底是资源不够还是我的代码慢」的关键。
>
> **mini-ray 也实现了这一层** —— `miniray/util/metrics.py` 提供了
> `Counter` / `Gauge` / `Histogram` 三件套，语义与上面一致
> （含 `tag_keys` 校验、桶插值分位数、重复注册报错）。
> ⚠️ 唯一的**超集**是它额外提供了 `.timer()` 上下文管理器 ——
> **真实 Ray 没有这个 API**，别照抄。对照见 §11.10。

---

## 11.6 时间线与追踪

### ⚠️ 先开开关，否则拿到空文件

**这是「timeline 用不了」的头号原因**：Ray 默认**不记录**任务级的 profiling 事件。

```bash
RAY_PROFILING=1 ray start --head ...       # 启动集群时就带上
# 或
RAY_PROFILING=1 python my_script.py
```

没设这个变量时，**Ray 不会抛异常，只会打一条 warning，然后把文件照写出来**：

```
WARNING ... No profiling events found. Ray profiling must be enabled by
setting RAY_PROFILING=1, and make sure RAY_task_events_report_interval_ms=0.
```

紧接着它仍然执行 `open(filename, "w")` + `json.dump([])` ——
于是你拿到一个**内容为 `[]` 的文件**，"导出成功"的假象是这里最容易误判的地方。

> 📌 **本条的目的地是提醒两个前提，不是一个**（2.58 源码 `state.py` 的
> `chrome_tracing_dump()`：`if not all_events: logger.warning(...)`，
> 之后无条件写文件）：
> **① `RAY_PROFILING=1`；② `RAY_task_events_report_interval_ms=0`。**
> 只设了 ① 而没设 ②，事件仍可能停在上报队列里没进 GCS，
> 你同样会得到一个空文件。
>
> ⚠️ **本书第四轮曾把这里写成"通常会直接报错"，那是错的** ——
> 报错只来自"Ray 还没 `init`"（`RuntimeError: Ray has not been started yet`），
> 与 profiling 无关。第五轮对着 2.58 源码改回"warning + 空文件"，
> 并补上了第二个环境变量。

所以排查顺序是：先 `echo $RAY_PROFILING` 确认它是 `1`，
再确认 `RAY_task_events_report_interval_ms=0`，然后才怀疑别的 ——
这一步能省掉大量瞎猜。

### 导出与查看

```python
import ray
ray.timeline("timeline.json")              # Chrome Trace Format
# 拖进 chrome://tracing、Perfetto、或 speedscope 看
```

能直接回答的问题：

* 任务之间的**空隙**在哪（调度延迟 / 排队）；
* 并行度够不够（CPU 是否有空转）；
* 某个阶段为什么慢（是计算还是等待）。

### 三个概念别混

| 名字 | 是什么 | 怎么用 |
|---|---|---|
| **Timeline** | 一次性导出的 Chrome Trace 文件 | `ray.timeline("f.json")`，事后分析 |
| **Task events** | 每个任务的**状态变迁时间戳**（何时入队/开跑/结束） | `state.list_tasks(detail=True)` 里的时间字段；Dashboard 可直接看 |
| **Distributed Debugger** | 交互式断点调试（见 §11.7） | 代码里写 `breakpoint()`，VS Code 侧 attach |

`timeline` 适合回答「**这段为什么慢**」（看空闲缝隙），
task events 适合回答「**这个任务卡在哪一步**」（看状态与耗时），
两者互补。注意 timeline 是**采样/缓冲**的，超大集群上可能不完整
（**未确认**：缓冲上限与丢弃策略在版本间有调整）。

---

## 11.7 调试工具链

| 工具 | 用法 | 场景 |
|---|---|---|
| **Ray Distributed Debugger**（新，2.39 起默认） | 在 `@ray.remote` 里写 **`breakpoint()`**，用 **VS Code 扩展** attach | **首选**：① 断点进任务/actor；② **事后调试**（任务抛未捕获异常时 Ray 会**冻结**它等调试器接入，**但要显式开** `RAY_DEBUG_POST_MORTEM=1` —— 默认是关的） |
| **Ray Debugger（legacy）** | 设 `RAY_DEBUG=legacy`（例如经 `runtime_env`），再 **`ray debug`** | 旧路径：`ray debug` 会列出**活跃断点与异常**让你挑一个进 pdb。⚠️ 不设 `RAY_DEBUG=legacy` 时 `ray debug` 只会打印一条"新版已是默认"的提示 |
| `py-spy dump --pid <pid>` | 抓 raylet/worker 的堆栈 | 卡死、CPU 打满（**不改代码、不停进程**） |
| `ray logs` + `grep` | 过滤日志 | 已知错误关键字 |
| `state.list_tasks()` | 看状态机 | 任务没动静 |
| `ray memory` | 看对象与引用 | 内存问题 |
| asyncio debug 模式 | `PYTHONASYNCIODEBUG=1` | async actor 卡住（找阻塞事件循环的调用） |
| `make_remote_task`/本地小函数 | 把逻辑抽出来单测 | 最快的"调试"是别在分布式环境里调 |

> ⚠️ **`local_mode` 已经没有用了 —— 这是一个常见过时信息。**
> 替代它的调试路径有**两条，别混**（这是本书第四轮写得最含糊的一处，
> 第五轮对着 2.58 的 `scripts.py` 与官方文档拆开）：
>
> | | 新版 **Ray Distributed Debugger**（2.39 起默认） | 旧版 **Ray Debugger**（legacy） |
> |---|---|---|
> | 怎么下断点 | 在 `@ray.remote` 里写 **`breakpoint()`** | 同样是 `breakpoint()` |
> | 前端 | **VS Code 扩展**（Cursor 等 VS Code 系 IDE 亦可） | 终端里的 **pdb** |
> | 入口 | 扩展自动 attach（**不需要** `ray debug`） | **`ray debug`** 列出活跃断点/异常，选一个进入 |
> | 前置 | `pip install "ray[default]" debugpy` | 设 **`RAY_DEBUG=legacy`**（如经 `runtime_env`） |
> | 事后调试 | 设 **`RAY_DEBUG_POST_MORTEM=1`** → 任务抛未捕获异常时 Ray **冻结**它等调试器接入（**默认关闭**） | **同一套机制**：`RAY_DEBUG_POST_MORTEM=1` 在 legacy 下同样生效，只是接到终端 pdb（见下方 🔴） |
>
> 🔴 **`RAY_DEBUG_POST_MORTEM` 两套调试器都支持 —— 这条本书改错过两次。**
>
> * 第四轮写的是"**只在 legacy 模式下有效**"；
> * 第五轮改成"**只属于新版**，用之前要先去掉 legacy 旗标"；
> * **第八轮回源码核对后的结论：两个分支都有。**
>
> `python/ray/util/rpdb.py` 的 `_post_mortem()` 是**按 `RAY_DEBUG` 分支**的：
>
> ```python
> def _post_mortem():
>     if os.environ.get("RAY_DEBUG", "1") == "1":
>         return ray.util.ray_debugpy._post_mortem()     # 新版（debugpy）
>     rdb = _connect_ray_pdb(...)                        # legacy（终端 pdb）
>     rdb.post_mortem()
> ```
>
> 而唯一的调用侧判据（`python/ray/_raylet.pyx`，任务抛未捕获异常时）
> **只看 `RAY_DEBUG_POST_MORTEM`，不看 `RAY_DEBUG`** ——
> 所以两条路都生效。官方 legacy 调试器文档的示例也正是
> `{"RAY_DEBUG": "legacy", "RAY_DEBUG_POST_MORTEM": "1"}` **一起设**。
>
> 前两轮之所以各对一半，是因为都只查了一个分支就下了全局结论 ——
> **同一个错误形状，第三次出现**（详见第 39 章 §39.5）。
>
> （📌 相关读取点：`python/ray/util/rpdb.py:337` 的
> `_is_ray_debugger_post_mortem_enabled()`，读的就是 `RAY_DEBUG_POST_MORTEM`，
> **默认 `"0"`（关闭）**；另一处消费点在 `ray/data/exceptions.py`。
> ⚠️ 它**不在** `worker.py` / `scripts.py` / `ray_config_def.h` 里 ——
> 只 grep 那三个文件会落空，这也是本书早先把它误标成"未在源码中定位到"的原因。）
>
> 📌 **最容易搞错的一点**：`ray debug` **不是**新版的入口，它是 **legacy** 的。
> 2.58 的 `ray debug` 在不设 `RAY_DEBUG=legacy` 时会先打印
> *"The distributed debugger … is now the default … If you want to keep using
> 'ray debug' please set RAY_DEBUG=legacy"*。
> **`RAY_DEBUG=1` + `ray debug` 这个组合是把两条路径拼错了** ——
> `RAY_DEBUG` 的合法取值里，只有 `legacy` 才表示"用旧的"。

**为什么不能再用 `local_mode`？** 因为它在 Ray Core 层**已经不可用**了 ——
⚠️ 但要说准：**形参还在签名里**（`ray/_private/worker.py` 的
`def init(..., local_mode: bool = False, ...)`），只是**传 `True` 会直接抛**：

```python
ray.init(local_mode=True)
# RuntimeError: `local_mode` is no longer supported. For interactive debugging
# consider using the Ray distributed debugger.
```

（源码：`ray/_private/worker.py` 的 `if local_mode: raise RuntimeError(...)`。
移除发生在 Ray Core 层，连带 RLlib / Tune 一起 —— PR #60647。）

**那怎么获得"单进程调试"的体验？** 三条现实路径：

1. **把任务体抽成普通函数**，先脱离 Ray 单测通过，再接回 `@ray.remote` ——
   这是最有效也最被低估的一条；
2. **用 Ray Distributed Debugger**（在 remote 函数里写 `breakpoint()`，
   再用 VS Code 扩展 attach），它能在真正的 worker 进程里停下断点；
3. **在 mini-ray 里用 `local_mode=True`** —— 见下方"与 mini-ray 的关系"，
   它是本教程的简化实现，保留了 `local_mode`。

---

## 11.7.1 分布式进度条：`ray.experimental.tqdm_ray`

**它解决什么问题**：普通 `tqdm` 把进度条画在**当前进程的终端**上。Ray 里同时跑
8 个 worker，每个都 `import tqdm` 自己画一条 —— 8 条进度条会互相覆盖、
刷新时把彼此刷花，最后你往往只能看到其中一条（还停在 100%）。
`tqdm_ray` 的做法是：**worker 只上报状态，进度条统一由 driver 渲染**，
driver 侧按 `(ip, pid)` 给每个 worker 分配互不重叠的 `position`
（`_BarManager` 里的注释把它比作"虚拟内存管理器"）。

```python
from ray.experimental.tqdm_ray import tqdm      # ⚠️ 类名是小写的 tqdm

@ray.remote
def process(shard):
    for item in tqdm(shard, desc="shard"):      # ① 直接包住可迭代对象
        handle(item)

@ray.remote
def count(n):
    bar = tqdm(total=n, desc="counting")        # ② 也可以手动 update
    for _ in range(n):
        bar.update(1)
    bar.close()                                 # ← close 会强制上报最后一次
```

它只支持 **tqdm 参数的一个受限子集**（别指望 `postfix` / `ncols` / `leave`）：

| 参数 | 含义 | 默认 |
|---|---|---|
| `iterable` | 包住它就能自动 `update(1)` | `None` |
| `desc` / `total` / `unit` | 与 tqdm 同名同义 | `""` / `None` / `"it"` |
| `position` | 同一 worker 内多条进度条的相对位置 | `0` |
| `flush_interval_s` | **上报间隔**（名字是 `_s`，不是 `_seconds`；类常量叫 `DEFAULT_FLUSH_INTERVAL_SECONDS`） | `1.0` |

方法只有 `update(n=1)` / `set_description(desc)` / `refresh()` / `close()`，
外加 `n` / `total` 两个属性和 `__iter__`。

**代价（这是本节最该记住的部分）**：

1. **每个 worker 独立上报到 driver**。worker 侧把状态 `print` 成一行带魔术标记
   （`__ray_tqdm_magic_token__`，常量 `RAY_TQDM_MAGIC`）的 JSON；
   driver 是在**日志转发流**里识别这行标记的（`ray/_private/worker.py`：
   `if RAY_TQDM_MAGIC in line: process_tqdm(line)`）。
   所以它**依赖日志转发**（`RAY_LOG_TO_DRIVER` 默认开着，见 §11.4），
   并且每个 worker 都要多付一份"序列化 + 打印 + driver 解析"的开销 ——
   **worker 越多、bar 越多，这笔开销越大**。
2. **上报是节流的，默认 1 秒一次**。`flush_interval_s` 窗口内的 `update()`
   只在 worker 本地累加、不发消息；下一次 flush 发的是**累计值**，
   所以中间值不会丢，但 **driver 侧看到的刷新频率上限就是 `1/flush_interval_s`**。
   把 `flush_interval_s` 调到很小、又开了很多条 bar，等价于让每个 worker
   每秒往日志里灌很多行 JSON —— 这是它"额外开销"的主要来源。
   反过来：**别把它当高频计数器用**（要计数请用 §11.5 的 `Counter`）。
3. **driver 侧要装 `tqdm`**：没装时不会报错，只打一条 warning
   （*"tqdm is not installed. Progress bars will be disabled."*），进度条静默消失。
4. **它会改写 `print`**：第一次用到 `tqdm_ray` 时，`builtins.print` 会被替换成
   `safe_print`（打印前先隐藏进度条、打完再恢复），这正是"日志与进度条不打架"的原因。
   **不接受这个副作用就设 `RAY_TQDM_PATCH_PRINT=0`。**

**和 logging 的取舍**：两者不是替代关系，判据是**更新频率与信息是否要留痕**：

| 场合 | 用 `tqdm_ray` | 用 logging |
|---|---|---|
| 更新频率 | **高**（每个 item 一次） | 低（每个事件一次） |
| 关注点 | **只关心最新值**（已处理多少 / 还剩多久） | **每条都要留痕**（开始/结束/失败/关键参数） |
| 生命周期 | 临时，跑完就该消失 | 持久，要能按级别过滤、进集中式日志系统 |
| 中间值 | 合并上报（丢掉中间**快照**没关系） | 每条都是真实事件，会进日志文件 |

> ⚠️ **两者最常一起踩的坑**：把变长的诊断信息塞进 `desc`。
> `desc` 每次上报都会随 JSON 一起发给 driver，字符串越长单价越高，
> 而它最终只是显示在**一行**里 —— 诊断信息请走 logging。
>
> **一句话总结**：`tqdm_ray` 管"还要多久"，logging 管"发生了什么"。
> 而"一共发生了多少次、按标签怎么分布"两个都不合适 —— 那是指标（§11.5）。

---

## 11.8 排查手册：按症状索引

| 症状 | 第一步 | 第二步 | 常见结论 |
|---|---|---|---|
| 任务一直不开始 | `state.list_tasks()` 看状态 | `available_resources()` | 资源不足 / 依赖未就绪 / 放置组 PENDING |
| 任务很慢 | `state.list_tasks()` 看 duration | timeline 看空隙 | 排队 / 跨节点拉数据 / 任务太大 |
| 任务失败 | 看 `RayTaskError.traceback_str` | 看 `attempt_number` | 逻辑错误（需显式 `retry_exceptions`）/ 环境抖动 |
| 任务崩溃 | 看 `WorkerCrashedError` | `dmesg`/容器事件找 `Killed` | OOM / 段错误 |
| 内存一直涨 | `ray memory` | `state.list_objects()` 看 `call_site` | 引用泄漏 / driver 持有大对象 |
| 对象存储满 | `ray memory --stats-only` | raylet 日志看溢出速率 | 调容量 / 背压 / 切小对象 |
| actor 没响应 | `state.list_actors()` 看状态与邮箱 | `ray logs actor --id=<actor_id>` | 卡在某个方法 / 死锁 / 已死 |
| 集群整体卡住 | `ray status` | head 节点 dashboard agent 是否活着 | 见 11.9 的真实案例 |
| 提交 job 没反应 | 检查 head 的 dashboard agent | 版本是否在 2.52–2.56.1 | OTel 死锁 bug（升级到 2.57+） |
| 调度看起来不对 | `list_tasks()` 的 node_id | 依赖对象所在节点 | 本地性优先（不是 bug） |

---

## 11.9 一个真实案例：1337 节点的 autoscaler 卡死

这个案例值得完整读一遍，它展示了「可观测性不足时排查有多难」。

**现象**：生产集群（1337 个活跃节点）的 autoscaler 每轮 reconcile 要
**10–44 分钟**，monitor 进程 CPU 打到 **95.8%**，扩缩容基本失效。

**排查**：

1. `ray status` / 日志看不出所以然（只是"很慢"）；
2. `ps aux` 找到 autoscaler monitor 进程；
3. `py-spy dump --pid <pid>` 抓堆栈 → 热点在 `SerializeToString`；
4. 定位到 `UnschedulableRequestCache.contains()`；
5. 根因：Autoscaler V2 的 scheduler 缺少 V1 的 1000 条上限，
   13,315 个 demand × 1,337 个节点 → 每轮约 **1780 万次** `SerializeToString`。

**修复**：给 V2 scheduler 补上限（PR #64175）；Autoscaler V2 后续还加了
优先级感知的 worker group 选择。

**这个案例教给我们三件事**：

1. **`py-spy` 是分布式 Python 系统里最值钱的调试工具**（不用改代码、不停进程）；
2. **「慢」的问题要看热点而不是看日志**；
3. **升级到新版不等于更稳** —— 这个 bug 恰恰是"新 autoscaler"引入的复杂度。
   这也是引入大版本前必须压测的理由。

---

## 11.10 mini-ray 的对应实现

mini-ray 没有 Dashboard，但把「背后的表」直接变成了 Python API +
一个自包含的 HTML 甘特图：

```python
from miniray import state
state.list_tasks()        # 状态/耗时/重试/节点
state.list_objects()      # 状态/所在节点/引用计数/生产者任务
state.list_actors()       # 状态/邮箱积压/在飞请求/重启次数
state.list_workers()      # pid/状态/已执行任务数
state.summarize_tasks()   # 聚合视图
state.summarize_objects() # ★ 大小在这里:num_objects / used_bytes /
                          #   capacity_bytes / counters / num_reconstructions

import miniray as ray
ray.timeline("timeline.json")     # Chrome Trace
ray.timeline(html="timeline.html")  # 自包含 SVG 甘特图(双击就能看)
```

> ⚠️ **`list_objects()` 里没有"大小"字段** —— 它的字段是
> `object_id / state / node_id / produced_by / refs`
> （`raylet.py` 源码为准）。要看字节数用 `summarize_objects()` 的
> `used_bytes`，或直接读 `ObjectStore.stats()`。
> 这一点与真实 Ray 恰好相反：真实 Ray 的 `state.list_objects()` **有**
> `object_size` 字段。**同一个名字，字段集不同** —— 迁移时容易踩。

指标三件套（对应 §11.5）：

```python
from miniray.util.metrics import Counter, Gauge, Histogram

reqs = Counter("app_requests_total", description="请求数", tag_keys=("model",))
depth = Gauge("app_queue_depth", description="排队深度")
lat = Histogram("app_latency_ms", boundaries=[1, 5, 10, 50, 100])

@ray.remote
def infer(x):
    depth.set(1)
    with lat.timer():          # ⚠️ 真实 Ray 没有 .timer(),这是 mini-ray 扩展
        return model(x)
```

> 🔴 **`.timer()` 的单位是毫秒，不是秒** —— 这是本书第四轮漏掉的一个坑。
> `miniray/util/metrics.py` 里写的是
> `self.observe((time.perf_counter() - start) * 1000.0, ...)`，
> 文档串也明说"自动以**毫秒**为单位记录"。
> 所以指标名和分桶都要按**毫秒**写（上面已从 `app_latency_seconds`
> 改成 `app_latency_ms`、分桶改成毫秒级）。
> 照第四轮那个 `app_latency_seconds` + 秒级分桶写，
> 几百毫秒的耗时会被塞进 `+Inf` 桶，**分位数完全没有意义**。
> 真实 Ray 没有 `.timer()`，你自己写 `latency.observe(seconds)`，
> **单位由你定** —— 两边别互抄。

命令行：

```bash
python -m miniray status --num-cpus 8    # 类似 ray status
python -m miniray demo                   # 内置演示(含 timeline 生成)
```

**设计取舍**：不做 Dashboard（需要 HTTP 服务 + 前端资源 + 常驻进程），
但把 State API、timeline 与自定义指标做扎实 —— 教学场景下这三样覆盖了
90% 的排查需求。**没有**做成 Prometheus 端点：那需要常驻 HTTP 服务，
与「零依赖、单进程」的设计目标冲突（mini-ray 的指标是**进程内可读**的，
不导出到网络）。也**没有**实现分布式进度条（`ray.experimental.tqdm_ray`，见 §11.7.1）。

---

## 11.11 本章小结

* 四层可观测性：**状态 → 日志 → 指标 → 追踪**，按这个顺序用。
* State API（alpha，需 `ray[default]`）是最直接的一层：
  `summarize_*` 先看全局，`list_*` 再定位，`call_site` 找内存泄漏。
* Dashboard 好用但**默认无鉴权**（真实 CVE，已被 CISA 列入 KEV），
  不要暴露；它也不该替代监控系统。
* 日志默认转发到 driver；大集群请接集中式日志；注意去重行为。
* 指标接 Prometheus；注意 2.52–2.56.1 的 OTel 死锁 bug（升级到 2.57+）；
  也要知道 Ray Data 的背压目前**无法从指标判断是哪个策略阻塞**（官方 issue #65607 承认）。
* `timeline` 看并行度与空隙（**要先开 `RAY_PROFILING=1` 与
  `RAY_task_events_report_interval_ms=0`，否则只会得到一个空文件**）；
  `py-spy` 看热点；
  调试首选是 **Ray Distributed Debugger**（remote 里 `breakpoint()` + VS Code 扩展；
  旧的 `ray debug` 需 `RAY_DEBUG=legacy`）——
  ⚠️ **`local_mode` 已不可用** —— 形参还在，但传 `True` 会抛 `RuntimeError`
  （它退化成了一个"专门用来报错"的形参，详见 §11.7）。
* 多 worker 的进度条用 **`ray.experimental.tqdm_ray.tqdm`**（§11.7.1）：
  worker 独立上报、driver 统一渲染，默认 **1 秒** flush 一次；
  它与 logging 的分工是「**进度用 tqdm、事件用 logging**」，别互相塞。
* 排查的核心能力是**把症状映射到状态字段**，这就是 11.8 那张表的价值。

到这里，Ray Core 的机制部分讲完了。从下一章开始进入 AI 库：
Ray Data 的数据管道、Ray Train 的分布式训练、Ray Tune 的超参搜索、
Ray Serve 的在线服务、RLlib 的强化学习。
