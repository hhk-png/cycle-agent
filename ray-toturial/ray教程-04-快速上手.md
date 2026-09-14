仓库地址：https://github.com/hhk-png/cycle-agent

# 第 4 章：快速上手

> 本章目标：10 分钟内把 Ray 跑起来，并掌握「从单机脚本迁移到 Ray」的固定套路。
> 这一章不追求原理深度，追求**可操作性**：每一步都可以直接复制粘贴。

---

## 4.1 安装

```bash
# 最小安装(Core + dashboard/CLI 之外的附加能力都在 extras 里)
pip install "ray[default]"           # 推荐:含 dashboard、State API、日志聚合等
pip install "ray[data,train,tune,serve]"   # 按需装 AI 库
pip install "ray[llm]"               # Ray Serve LLM / Ray Data LLM(会带上 vLLM)
pip install "ray[all]"               # 除 llm 与 cpp 外的全部 extras(体积很大)
#   ⚠️ "llm" 与 "cpp" 【都不在】 all 里(官方 by design:vllm 依赖集太大;
#      cpp 那套另有 all-cpp),这两个要单独装
```

| 事实 | 值 |
|---|---|
| 当前稳定版 | **2.58.0**（2026-08-23） |
| Python | **≥ 3.10**（3.9 自 2.52 弃用、**2.54.0 起不再支持**；分类器里有 3.14） |
| 平台 | Linux 最佳；macOS 可用但对象存储落 `/tmp`（磁盘），性能受影响；Windows 上 `runtime_env` 是 beta |

**为什么推荐 `ray[default]`**：不带它就没有 Dashboard、`ray.util.state`、
日志聚合 —— 而这些恰好是排查问题时最需要的东西。生产镜像里通常也会带上它。

### 检查安装

```bash
python -c "import ray; print(ray.__version__)"
ray --version
ray status        # 没有集群时会提示连不上,这是正常的
```

---

## 4.2 起一个集群：四种方式

| 方式 | 命令 | 适用 |
|---|---|---|
| **本地多进程**（默认） | `ray.init()` | 开发、调试、单机多核 |
| **多机集群** | 每台机器 `ray start --address=...` | 有机器、想自己管 |
| **Kubernetes** | KubeRay Operator + `RayCluster` CRD | 生产（第 17 章） |
| **托管** | Anyscale Platform 等 | 不想运维（注意：Anyscale 2026-07 宣布被 Nscale 收购，交易待交割） |

本地开发直接：

```python
import ray
ray.init()                       # 自动探测本机资源,启动一个本地集群
```

多机集群（先在 head 节点）：

```bash
ray start --head --port=6379 --num-cpus=8 --num-gpus=4 --dashboard-host=0.0.0.0
# 输出会打印 head 的地址,在 worker 节点上:
ray start --address='10.0.0.1:6379' --num-cpus=8
```

然后 driver 里 `ray.init(address="auto")` 或设 `RAY_ADDRESS` 环境变量。

```bash
ray status         # 集群资源与节点状态
ray stop           # 停掉本机的 ray 进程
```

---

## 4.3 第一个程序：把它拆成 5 步

```python
import ray
import time

# ① 启动(本地)
ray.init()

# ② 把普通函数变成远程函数
@ray.remote
def slow_square(x):
    time.sleep(0.5)
    return x * x

# ③ 提交任务(异步:立刻返回句柄)
start = time.time()
refs = [slow_square.remote(i) for i in range(4)]
print(f"提交 4 个任务耗时 {time.time() - start:.4f}s")     # 毫秒级

# ④ 取值(同步:一次等齐)
values = ray.get(refs)
print(f"总耗时 {time.time() - start:.2f}s,结果 {values}")   # 约 0.5s,而不是 2s

# ⑤ 关闭
ray.shutdown()
```

跑起来的三个关键点：

1. **提交是异步的**：4 个 `remote()` 加起来不到 1 毫秒；
2. **执行是并行的**：4 个各 0.5 秒的任务总共约 0.5 秒（取决于 CPU 数）；
3. **`ray.get` 是唯一的同步点**。

### 换成 mini-ray 跑

本教程配套的 mini-ray（第 6 章）把这段代码原样跑一遍，只需要改 import：

```python
import sys
sys.path.insert(0, "mini-ray")
import miniray as ray        # ← 唯一的改动

# 下面每一行都和上面一样
```

mini-ray 还带了一个命令行演示：

```bash
cd mini-ray
python -m miniray demo              # 任务/依赖/actor/对象/可观测性 全流程演示
python -m miniray status --num-cpus 8   # 打印资源与 worker 视图
```

---

## 4.4 `ray.init()` 常用参数

```python
ray.init(
    address=None,              # None=本地起; "auto"=连已有集群
    num_cpus=8,                # 限制本进程可见的 CPU(默认自动探测)
    num_gpus=0,
    resources={"TPU": 4},      # 自定义资源
    object_store_memory=2 * 1024**3,   # 对象存储容量(默认=可用内存 30%,上限 200GB)
    namespace="team-a",        # 命名空间(影响命名 actor)
    runtime_env={...},         # 依赖注入(环境变量/pip/working_dir)
    logging_level=logging.INFO,
    ignore_reinit_error=False, # 重复 init 时是否忽略
    include_dashboard=True,
)
```

两个容易踩的点：

* **`num_cpus` 是「给这个 Ray 实例的资源上限」，不是「物理核数」**。
  设成比真实核数大，Ray 会真的按你声明的数量去并发（可能拖慢彼此）；
* **`local_mode` 已经被移除了，不要再写它** —— 写了会直接
  `RuntimeError: local_mode is no longer supported`。
  想单步调试请用 **Ray Distributed Debugger**（在 remote 函数里写 `breakpoint()`，
  再用 VS Code 扩展 attach；**不是** `ray debug`，那是 legacy 入口，见第 11 章 §11.7）；
  mini-ray 仍然保留 `local_mode`（那是它的教学特性，不是 Ray 的现状）。

> **`address` 的四种写法（含 `ray://`）与依赖注入（`runtime_env`）**
> 在本章只覆盖了"本地/`auto`"这两种最常用的。
> 笔记本连远端集群、`pip`/`conda`/`working_dir` 怎么把依赖送到每个节点、
> 多语言绑定什么水平 —— 这三块原本散落在附录 A 的表格里，
> 现在集中在**附录 F（第 27 章）**：
> `runtime_env` 的完整键表与继承规则见 §F.2，Ray Client 的代价与现状见 §F.3。

---

## 4.5 跑起来之后：先装好三件「脚手架」

新手最常见的困境是「跑得动，但出问题不知道从哪看」。这三样东西应该一开始就用：

```python
# ① 可观测性:看集群在干什么
from ray.util import state
state.list_tasks()          # 每个任务的状态/耗时/重试次数/所在节点
state.summarize_tasks()     # 聚合视图(先看这个)
state.list_actors()

# ② 时间线:看并行度与空隙
#    ⚠️ 必须先开 profiling。没开的话**不会报错，而是给你一个空文件**：
#       日志里只有一条 warning:
#       "No profiling events found. Ray profiling must be enabled by
#        setting RAY_PROFILING=1, and make sure
#        RAY_task_events_report_interval_ms=0."
#       但文件**照样被写出来**，内容是一对空方括号 []。
#       —— "导出成功、打开什么都没有"比报错更容易误判（见第 11 章 §11.6）
#    两个前提都要满足：RAY_PROFILING=1 且 RAY_task_events_report_interval_ms=0
#    启动前:  set RAY_PROFILING=1   (Windows)
#             export RAY_PROFILING=1  (Linux/macOS)
ray.timeline("timeline.json")     # 可以用 chrome://tracing 或 Perfetto 打开

# ③ 上下文:在任务里知道自己是谁
@ray.remote
def who():
    ctx = ray.get_runtime_context()
    return ctx.get_worker_id(), ctx.get_node_id(), ctx.get_job_id()
```

再进一步就是 **Dashboard**（`ray[default]` 自带，默认 `http://localhost:8265`）
与 `ray memory`：

```bash
ray memory --stats-only                    # 对象存储的聚合统计
ray memory --group-by STACK_TRACE          # 列出每个对象的引用位置(排查内存泄漏)
ray logs worker --pid=<PID> --follow       # 查看某个 worker 的日志(见第 33 章 §33.5)
```

> ⚠️ **安全提醒**：Dashboard 端点**默认没有鉴权**。不要把它暴露到不可信网络 ——
> 这已经导致过被实际利用的 CVE（CVE-2025-62593，CVSS 9.4，浏览器 DNS rebinding
> 可远程执行命令，2026-08 被 CISA 列入已知被利用漏洞目录）。
> 见第 17 章的安全清单。

---

## 4.6 从单机脚本迁移到 Ray：五个改写套路

这一节是本章最有价值的部分。**绝大多数单机代码迁移到 Ray 只需要五种改写。**

### 套路 1：把「批量循环」改成「提交 + 一次取」

```python
# 改写前
results = []
for item in items:
    results.append(expensive(item))          # 串行

# 改写后
refs = [expensive.remote(item) for item in items]
results = ray.get(refs)                       # 自动并行
```

### 套路 2：把「加载一次的全局资源」改成 Actor

```python
# 改写前:每次调用都要重新加载模型
def predict(text):
    model = load_model()      # 慢!
    return model(text)

# 改写后:模型常驻在 actor 里
@ray.remote
class Predictor:
    def __init__(self):
        self.model = load_model()      # 只加载一次

    def predict(self, text):
        return self.model(text)

predictor = Predictor.remote()
```

### 套路 3：把「大数据反复传」改成 `ray.put` + 传引用

```python
# 改写前:每个任务都收到一份完整数据(每个任务一次序列化 + 一次拷贝)
refs = [process.remote(big_data, i) for i in range(100)]

# 改写后:数据放对象存储一次,任务收到的是引用(同节点零拷贝)
big_ref = ray.put(big_data)
refs = [process.remote(big_ref, i) for i in range(100)]
```

### 套路 4：把「有依赖的阶段」改成「传 ref 表达依赖」

```python
# 改写前
stage1 = [step1(x) for x in inputs]
stage2 = [step2(y) for y in stage1]      # 必须等 stage1 全部结束

# 改写后:依赖粒度是单个对象,而不是整个阶段
s1 = [step1.remote(x) for x in inputs]
s2 = [step2.remote(y) for y in s1]        # 每个 s2 只等它自己那个 s1
results = ray.get(s2)
```

这一个小改动常常直接把墙钟时间砍一半 —— 因为它去掉了「按阶段同步」。

### 套路 5：把「不可控的提交速率」加上背压

```python
# 改写前:100 万个任务一次性提交 → 对象存储爆、调度队列爆
refs = [process.remote(x) for x in huge_list]

# 改写后:滚动提交,用 ray.wait 收结果
pending, results, it = [], [], iter(huge_list)
for _ in range(100):                      # 预填
    pending.append(process.remote(next(it)))
while pending:
    ready, pending = ray.wait(pending, num_returns=1)
    results.extend(ray.get(ready))
    try:
        pending.append(process.remote(next(it)))
    except StopIteration:
        pass
```

### 完整前后对比

```python
# ───── 改写前:单机串行 ─────
def process_all(paths):
    results = []
    for path in paths:
        data = load(path)
        cleaned = clean(data)
        results.append(analyze(cleaned))
    return results

# ───── 改写后:Ray ─────
@ray.remote
def load(path): ...
@ray.remote
def clean(data): ...
@ray.remote
def analyze(data): ...

def process_all(paths):
    loaded = [load.remote(p) for p in paths]
    cleaned = [clean.remote(d) for d in loaded]
    analyzed = [analyze.remote(c) for c in cleaned]
    return ray.get(analyzed)
```

注意改写后**没有一个显式的「阶段屏障」**：第 3 个 `analyze` 不必等第 1 个
`load`，只等它自己那条链。这就是 Ray 相对「按阶段调度」的框架最大的差别。

---

## 4.7 把脚本提交到集群：Jobs API

前面几节你都是**在自己机器上跑 driver**（`ray.init()` 然后执行脚本）。
这在开发时没问题，但一上生产就会撞到三个问题：

1. driver 跑在你的笔记本上 —— 合上盖子、断网、SSH 掉线，**job 就死了**；
2. 集群上有 200 张卡，你的 driver 却还在本地；
3. 没法回答"昨晚那个任务跑了没、日志在哪、谁提交的"。

**Jobs API 就是为这三件事生的**：它把你的 driver **跑到集群上去**，
并给每个 job 一个 ID 和完整的生命周期管理。

> 一句话理解：`ray.init()` 是"**我连到集群**"，Jobs API 是"**我把活儿交给集群**"。

### 命令行：最常用的一条命令

```bash
# 在集群的 head 节点上(或任何能访问 dashboard 8265 端口的机器)
ray job submit --address http://127.0.0.1:8265 \
    --working-dir . \
    -- python my_script.py --epochs 10
```

* `--working-dir .` —— 把**当前目录打包上传**到集群。
  这是最重要的一步：集群上的机器没有你的代码，不打包就会
  `ModuleNotFoundError: No module named 'my_script'`。
* `--` 之后是 **entrypoint**：一个普通的 shell 命令，写什么跑什么
  （可以是 `python train.py`，也可以是 `bash run.sh` 或 `python -m my_pkg`）。
* `--address` 指向 head 的 dashboard 端口（默认 8265），不是 6379。

然后：

```bash
ray job list                       # 所有 job 及其状态
ray job status <job_id>            # PENDING / RUNNING / SUCCEEDED / FAILED / STOPPED
ray job logs <job_id> --follow     # 实时跟日志(等价于 tail -f)
ray job stop <job_id>              # 停掉
```

### Python API：要在代码里提交时用

```python
from ray.job_submission import JobSubmissionClient, JobStatus
import time

client = JobSubmissionClient("http://127.0.0.1:8265")

job_id = client.submit_job(                    # ← 异步:立刻返回 job_id
    entrypoint="python my_script.py --epochs 10",
    runtime_env={"working_dir": "./"},
    entrypoint_num_cpus=1,                     # entrypoint 自身的资源
)
print("submitted:", job_id)                    # raysubmit_xxxxxxxxxxxx

# 轮询到终态
while True:
    status = client.get_job_status(job_id)
    if status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED):
        break
    time.sleep(2)

print(status)
print(client.get_job_logs(job_id))             # 拿全量日志
```

**五个状态**（`JobStatus`）：`PENDING` → `RUNNING` → `SUCCEEDED` / `FAILED` / `STOPPED`。

| 方法 | 作用 |
|---|---|
| `submit_job(entrypoint=, runtime_env=, ...)` | 提交，返回 `job_id` |
| `get_job_status(job_id)` | 当前状态 |
| `get_job_logs(job_id)` / `tail_job_logs(job_id)` | 全量日志 / 流式日志 |
| `get_job_info(job_id)` | 详情（提交时间、资源、元数据） |
| `list_jobs()` | 列出所有 job |
| `stop_job(job_id)` / `delete_job(job_id)` | 停止 / 删除 |

### 三个容易踩的点

**① entrypoint 的资源是独立的。**
`--entrypoint-num-cpus` 管的是**跑 driver 那个进程**占多少资源，
和 job 内部 `@ray.remote(num_cpus=...)` 申请的资源是两回事。
默认 entrypoint **0 CPU / 0 GPU** —— 它通常落在 head 节点上，
所以别在 entrypoint 里做重活。

**② `working_dir` 是打包上传，不是挂载。**
每次提交都会重新打包，所以**别把大数据集放进工作目录** ——
你会反复上传几百 MB。数据集应该放对象存储 / S3 / 共享文件系统，
用路径传进去。工作目录里只放代码和小配置文件。

**③ job 的 driver 死了，job 就结束了。**
这和本地跑一样 —— 区别只是 driver 现在跑在集群上，
所以**你的笔记本掉线不影响它**。这也意味着：job 里**不要**依赖
"driver 上的本地状态"，要重跑就得从检查点恢复（第 10 章）。

### 和 `ray.init(address="auto")` 怎么选

| 场景 | 用什么 |
|---|---|
| 本地开发、交互式调试、Notebook | `ray.init()` / `ray.init(address="auto")` |
| 生产跑批、定时任务、要日志与重试 | **Jobs API** |
| K8s 上的生产 | KubeRay 的 `RayJob` CRD（第 17 章）—— 它是 Jobs API 的声明式封装 |

> **迁移提示**：如果你的脚本现在是"本地 `ray.init()` + 跑 main()"，
> 改成 Jobs API **通常一行都不用改代码** —— 只要保证脚本的入口是
> `if __name__ == "__main__":`，然后用
> `ray job submit --working-dir . -- python 你的脚本.py` 提交即可。
> 真正的改动在**运维侧**：日志要看 `ray job logs`、失败要重提、
> 检查点要落在共享存储上。

---

## 4.8 启动期常见问题

| 症状 | 原因 | 处理 |
|---|---|---|
| `Ray is already initialized` | 重复 `ray.init()` | `ray.shutdown()` 或用 `ignore_reinit_error=True` |
| 对象存储启动失败 | Docker 里 `/dev/shm` 太小（默认 64MB） | `--shm-size=2g`，或设 `object_store_memory` |
| `Address already in use` | 端口被占 | `ray start --port=...`、`--dashboard-port=...` |
| 连不上已有集群 | `RAY_ADDRESS` 不对/防火墙 | `ray status`、检查 6379/dashboard 端口 |
| macOS 上任务慢 | 对象存储落在 `/tmp`（磁盘） | 减小对象体积、接受现实、或换 Linux |
| 任务一直 PENDING | 资源不足 / 依赖未满足 / 放置组未就绪 | `state.list_tasks()` 看状态，第 8 章有排查表 |
| `An attempt has been made to start a new process before the current process has finished its bootstrapping phase` | 在**模块顶层**直接调用了 `ray.init()` / `.remote()`，而 worker 或子进程重新 import 了你的模块 | 把入口包进 `if __name__ == "__main__":`。⚠️ Ray 的 worker 是**直接执行脚本**启动的（`<python> …/workers/setup_worker.py …/workers/default_worker.py --node-ip-address=…`，见 `services.py` 的 `start_worker_command`）、**不**重新 import 用户脚本，所以**大多数**场景不需要这个保护 —— 但 `ray.util.multiprocessing`（spawn 语义）、Windows、以及被别的框架以 spawn 方式拉起的场景**需要**（第 6 章 §6.6 有原理） |
| `RayTaskError` 里说找不到你自己的模块/函数 | worker 是另一个解释器，只拿到**按值序列化**过去的函数（第 5 章 §5.4） | 用 `runtime_env={"working_dir": ...}` 把代码送过去（第 27 章 §F.2） |

---

## 4.9 本章小结

* 安装用 `pip install "ray[default]"`；版本 2.58，Python ≥ 3.10。
* 起集群：本地 `ray.init()`、多机 `ray start`、生产用 KubeRay。
* 三件脚手架要一开始就装好：**State API**（发生了什么）、
  **timeline/Dashboard**（时间花在哪）、**runtime context**（我是谁）。
* 迁移套路就五个：循环→批量提交、全局资源→actor、大对象→`ray.put`+ref、
  阶段依赖→ref 依赖、无限提交→`ray.wait` 背压。
* **上了集群就用 Jobs API**：`ray job submit --working-dir . -- python x.py`。
  它把 driver 搬到集群上跑，所以你的笔记本掉线不会杀死任务 ——
  这是"本地 `ray.init()`"和生产之间最重要的一道分界线。
* 立刻要记住的安全底线：**不要暴露 Dashboard 端口**。

下一章我们把「任务、对象、依赖」这三件事讲透 —— 包括 `num_returns`、
流式生成器、引用计数的实战含义，以及那些「看起来能用但性能很差」的写法。
