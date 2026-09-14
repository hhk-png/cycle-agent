仓库地址：https://github.com/hhk-png/cycle-agent

# 第 31 章：性能剖析与调试工具链

> 本章目标：把「我的 Ray 程序很慢」变成一个**能定位到具体行**的过程。
>
> 第 11 章讲的是**看集群在干什么**（State API、Dashboard、日志、指标），
> 第 18 章讲的是**常见的坑和旋钮**。但这两章都没有回答最后一个问题：
> **代码跑到哪一行、卡在哪个函数、谁在吃内存？**
>
> 那需要剖析器（profiler）。这一章讲四把刀各自的切法，以及一条从
> 「慢」到「哪一行慢」的完整路径。

---

## 31.1 为什么 `print` 大法在分布式里失效

单机调试里最有效的手段是「在可疑的地方打日志」。到了 Ray 里，它会同时坏掉三次：

| 问题 | 表现 |
|---|---|
| **输出交叉** | N 个 worker 进程同时往同一个 stdout 写，日志互相插队，读不出时序 |
| **不知道是谁** | 同一行 `print("start")` 出现在 8 个 worker 上，你分不清哪条属于哪个 rank |
| **打点本身改变行为** | 高频 `print` 会拖慢任务，把「本来不慢」的地方变成瓶颈（观测者效应） |

所以第一件事是**让每条输出都能归属到进程**：

```python
import os
import ray

@ray.remote
def work(x):
    ctx = ray.get_runtime_context()
    tag = f"[w={ctx.get_worker_id()[:6]} n={ctx.get_node_id()[:6]}]"
    print(f"{tag} start x={x}", flush=True)      # ← flush 很重要
    ...
    print(f"{tag} done", flush=True)
```

`flush=True` 在**重定向到文件或管道**时是必须的 —— 否则 buffer 会攒到进程退出才写，
而 worker 可能一直不退出，你会「看不到任何日志」。

> **Ray 官方推荐的做法**：用 `RAY_LOG_TO_STDERR=1` 让所有 worker 日志走 stderr，
> 再用 `ray logs` / Dashboard 的日志页按 worker 过滤。
> 手写 `tag` 前缀是**兜底方案**，在容器/CI 里更可靠。

但即使日志打对了，它也只能告诉你「慢**发生在**哪个任务」，
不能告诉你「慢**在任务内部的哪一行**」。那需要采样剖析。

---

## 31.2 四类「慢」，四把刀

先分类，再选刀 —— 选错工具是最常见的时间浪费。

```
「慢」
 │
 ├─ A. 任务内部某段代码慢（CPU/纯 Python）
 │     → py-spy（采样栈）   /  cProfile（函数级）
 │
 ├─ B. 任务内部某段代码吃内存（Python 对象泄漏）
 │     → memray（分配跟踪）
 │
 ├─ C. GPU 利用率低 / kernel 慢 / kernel 之间有空隙
 │     → nsys（时间线）    /  ncu（单 kernel 细节）
 │
 └─ D. 分布式层面的慢：调度延迟、序列化、跨节点搬运、worker 冷启动
       → Ray 自带：timeline / task events / state API（★ 先看这个）
```

**顺序很重要**：**D 永远该先查**。因为 D 类的开销是**每个任务**都要付的，
而且常常是数量级差异；A/B/C 往往只在少数任务里。

第 18 章 §18.1 给了四个开销来源的量级对照表，先回去看一眼再往下选。

---

## 31.3 `py-spy`：不停机看栈

**最该先装的一个工具。**

```bash
pip install py-spy
```

### 它解决什么

**「某个任务卡住了 / 某个 worker 在空转 CPU」** ——
`py-spy` 能在**不修改代码、不重启进程**的情况下，读出任何一个 Python 进程的调用栈。

### 三种用法

```bash
# ① 看当前在干什么（快照,最常用）
py-spy dump --pid 12345

# ② 采样 30 秒,出一份火焰图（SVG,浏览器打开）
py-spy record --pid 12345 --duration 30 --output /tmp/profile.svg

# ③ 直接启动并剖析一个脚本（不需要改代码）
py-spy record -- python train.py
```

### 在 Ray 里怎么用

第一步是**找到 PID**：

```bash
# 列出本节点所有 ray worker 进程
ps aux | grep "ray::" | grep -v grep

# 或者从 Ray 的状态里反查
python -c "
from ray.util import state
for w in state.list_workers():
    print(w['pid'], w['worker_id'], w.get('worker_type'))
"
```

```bash
# 盯着某个 worker 采样 30 秒
py-spy record --pid <PID> --duration 30 --output /tmp/worker.svg

# 一个节点上按名匹配,全部采样(排查「谁在空转」)
for pid in $(pgrep -f "ray::"); do
    py-spy dump --pid "$pid" > "/tmp/dump-$pid.txt"
done
grep -l "time.sleep\|recv\|wait" /tmp/dump-*.txt      # 看谁在等
```

> 💡 **别手写上面这个循环 —— 先试 `ray stack`**。
> `ray stack` 做的就是这件事（对全部相关 worker 批量 dump 栈），一条命令即可。
> ⚠️ 但它**内部就是调用 `py-spy`**，所以：**必须先装好 `py-spy`**，
> 而且下面讲的 ptrace/capability 限制对它**同样成立**。
> 把它理解成"批量 py-spy 的包装"最准确（第 33 章 §33.7）。
> **`ray stack` 定位 → `py-spy record` 深挖**，是更省时间的顺序。

> ⚠️ **容器里的一个坑**：`py-spy` 需要 `ptrace` 权限。
> 在 K8s 里默认的 `seccomp`/`capabilities` 下会被拒绝，报
> `Permission denied: ptrace`. 缓解办法是给容器加 `SYS_PTRACE`
> capability。另一条路是加 `--nonblocking`：**它的真实作用是让 py-spy
> 采样时「不暂停目标进程」**（默认是短暂的 stop-the-world，采样一致性更好），
> 所以它换来的是更低的侵入性、代价是**栈可能读到不一致的瞬间状态**。
> 注意 `--nonblocking` **不能绕过 ptrace 权限问题** —— 那是内核层面的拒绝，
> 与采样方式无关。
> **这本身就是一条生产建议**：**部署时就想好「出事了怎么进容器看栈」** ——
> 事后再加 capability 要重启 pod，而问题往往重启就没了。

### 火焰图怎么读

`py-spy record` 产出的是**火焰图**（flame graph）。三条规则：

```
宽度 = 被采样到的次数（≈ 时间占比）  ← 只看宽度,不要看高度
顶部 = 正在执行的函数（叶子）
每一层 = 调用的父函数
```

* **横向越宽 = 越耗时**。找最宽的那条「塔」，而不是最高的；
* **平坦的宽顶**（很多小函数并排）通常意味着「在遍历/解析大量小对象」；
* **`time.sleep` / `select` / `recv` 很宽** = **在等**，不是 CPU 瓶颈 ——
  这时候该去看 I/O 或上游，而不是优化这段代码；
* **`_pickle` / `pickle` / `cloudpickle` 很宽** = **序列化是瓶颈** ——
  对应第 18 章的反模式 2（大对象反复传）。

> **一句话**：火焰图回答的是「CPU 时间花在哪些调用路径上」。
> 它**不回答**「为什么在等」—— 那是 §31.6 的 timeline 的活。

---

## 31.4 `memray`：Python 内存剖析

`py-spy` 看的是**时间**，`memray` 看的是**内存**。

```bash
pip install memray
```

### 它解决什么

**「对象的引用计数明明归零了，内存为什么不降？」**
**「这个 worker 的内存为什么随任务数单调上涨？」**

第 7 章 §7.7 把内存事故分成四类，memray 能精确定位到**第 4 类（泄漏）
和「谁分配的」**。

### 用法

```bash
# ① 记录一次运行的内存分配
#    ⚠️ memray run 直接吃脚本路径 —— 不要再写一个 python,
#       否则它会把 "python" 当成脚本名去找。两种正确形式:
python -m memray run -o /tmp/out.bin my_script.py
memray run -o /tmp/out.bin my_script.py

# ② 生成火焰图（按内存分配量,不是时间）
memray flamegraph /tmp/out.bin -o /tmp/mem.html

# ③ 附到运行中的进程上
#    ⚠️ PID 是**位置参数**,没有 --pid 这个旗标
memray attach 12345
memray attach 12345 --duration 30     # 想限时:--duration 是合法的
memray detach 12345                   # 提前停止跟踪(detach 是独立子命令)
```

> ⚠️ **`memray attach` 与 `py-spy` 撞同一堵墙**：它也需要 **ptrace 权限**。
> Linux 上还受 `/proc/sys/kernel/yama/ptrace_scope` 限制，
> 容器里需要 `SYS_PTRACE` capability（见上面 §31.3 那段）——
> 最常见的绕法是 `sudo memray attach`。
> **同一个坑只讲了 py-spy 那一半，是最容易白花时间的地方。**

### 读内存火焰图

和 CPU 火焰图长得一样，但**宽度代表「谁分配了多少字节」**。所以：

* 找最宽的塔 → 那就是内存大头；
* 但它**不一定**是泄漏 —— 也可能是「本来就该这么大」。
  判断泄漏要看**两次快照的差**：

```bash
# 在训练循环的第 10 步和第 100 步各取一次快照,然后比较
memray run --live-remote ...
```

更实用的做法是**用 Ray 的状态做粗筛，再用 memray 做精确定位**：

```python
from ray.util import state

# 粗筛:哪个对象最大(字段名是 object_size)
objs = sorted(state.list_objects(), key=lambda o: o["object_size"], reverse=True)
for obj in objs[:10]:
    print(obj["object_id"], obj["object_size"])

# 粗筛:对象存储总共占了多少、谁在引用(CLI 一条命令,见第 33 章 §33.3)
#   ray memory --stats-only
```

> ⚠️ **`state.list_workers()` 里没有内存字段 —— 别在它上面做"按 worker 排内存"。**
> `WorkerState` 的字段是 `worker_id` / `is_alive` / `worker_type` / `exit_type` /
> `node_id` / `ip` / `pid` / `exit_detail` / `worker_launch_time_ms` /
> `worker_launched_time_ms` / `start_time_ms` / `end_time_ms` /
> `debugger_port` / `num_paused_threads` —— **`w.get("memory")` 永远是 `None`**。
> 要看**进程级**内存，得去 node exporter 的 `ray_component_rusage_*` 指标
> （第 11 章），或者最朴素地按 PID 用 `psutil` / `top` 看。
> `WorkerState` 能给你的关键信息是 **`pid`**，拿到 PID 才轮到 memray。

**先确定「是哪个进程」，再用 `memray attach` 盯那个 PID** ——
不要一上来就对整个集群做 memray，输出会让你无法阅读。

### 一个真实的定位流程

```
内存随 step 上涨
  │
  ├─ 1. 按 PID 看(psutil/top,或 ray_component_rusage_* 指标)
  │     → 是所有 worker 都涨,还是只有一个?
  │     └─ 只有一个 → 那个 worker 上的任务有问题(可能是特定数据分片)
  │
  ├─ 2. 对那个 PID 做 memray attach --duration 60
  │
  ├─ 3. 看内存火焰图:哪个调用路径宽
  │     ├─ 某个库的缓存(如 tokenizer 的 lru_cache) → 设上限
  │     ├─ torch 张量累积 → 是不是忘了 .detach() / .item()
  │     └─ 自己写的 list/dict 只增不减 → 修业务逻辑
  │
  └─ 4. 修完再采一次,对比确认
```

> ⚠️ **memray 会明显拖慢程序**（它是**跟踪每一次分配**，不是采样）。
> 所以它适合「复现一个已知问题」，不适合长期开在生产上。
> 长期监控请用第 11 章 §11.5 的指标 + 第 7 章的内存监控机制。

---

## 31.5 GPU 侧：`nsys` 与 `ncu`

CUDA 的剖析是另一套工具链，**和 Python 层的工具完全不重叠**。

| 工具 | 粒度 | 回答什么问题 |
|---|---|---|
| **`nsys`**（Nsight Systems） | **时间线**（整次运行） | GPU 有没有闲着？kernel 之间有没有空隙？CPU-GPU 同步在哪？ |
| **`ncu`**（Nsight Compute） | **单 kernel** | 这个 kernel 为什么慢？访存瓶颈还是算力瓶颈？ |

### `nsys`：先看「GPU 有没有在干活」

```bash
nsys profile -t cuda,nvtx,osrt -o /tmp/ray_prof --force-overwrite true \
    python trainer.py

# 转成可读报告(或用 GUI 打开 .nsys-rep)
nsys stats /tmp/ray_prof.nsys-rep
```

**最该看的一张图**是「GPU 利用率条」：

* **条是满的** → GPU 在忙，问题在 kernel 本身（上 `ncu`）；
* **条是断续的、中间有大片空白** → **GPU 在等**。等什么？
  * 等数据（DataLoader 不够快）→ 加 worker / 换格式；
  * 等 CPU 上的 Python（每个 step 的 Python 开销太大）→ 上 `py-spy`；
  * 等通信（NCCL）→ 看第 30 章 §30.4。

**这个判断顺序很重要**：`nsys` 先用一次，就能把问题分成「GPU 内部」和
「GPU 之外」两大类，后面的工具才好选。

### 在 Ray 里加标记

`nsys` 的时间线可以叠加自定义区间（NVTX），这样你能在时间线上看到
「这一步是 rank 0 在存 checkpoint」：

```python
import torch

@ray.remote(num_gpus=1)
def train_step(batch):
    torch.cuda.nvtx.range_push("forward")
    out = model(batch)
    torch.cuda.nvtx.range_pop()
    return out
```

### `ncu`：单 kernel 分析

```bash
ncu --set full -k my_kernel_name -c 5 python trainer.py
```

⚠️ **`ncu` 的开销极大**（可能慢 100 倍），**不要**直接对着训练脚本跑 ——
先确定可疑 kernel，再用 `-k` 只抓它，用 `-c` 限制次数。

---

## 31.6 Ray 自带的三件套（★ 先看这个）

上面三个都是通用工具。但**分布式特有的慢**，Ray 自己有更好的观测点。

### ① `ray.timeline()`：看并行度与空隙

```python
import ray

ray.init()
# ... 跑你的负载 ...
ray.timeline("timeline.json")     # Chrome Trace 格式
```

> ⚠️ **必须先开 `RAY_PROFILING=1`**（Windows: `set RAY_PROFILING=1`），
> **并且同时设 `RAY_task_events_report_interval_ms=0`**。
> Ray **默认不记录任务级的 profiling 事件** —— 不开的话**它不报错**，
> 只在日志里打一条 warning，然后**照样把文件写出来**，内容是一对空方括号 `[]`。
> 你打开 chrome://tracing 看到一片空白，然后以为「timeline 坏了」。
> 这是「timeline 用不了」的头号原因（第 11 章 §11.6 也强调过）。

拖进 `chrome://tracing` 或 Perfetto 看。**要找的模式**：

| 你在图上看到 | 含义 | 对应章节 |
|---|---|---|
| 长条之间有大片空隙，worker 轨道空着 | 并行度不够 / 提交得太慢 | 第 5 章 §5.6 |
| 一条轨道特别长，其它很短 | **队头阻塞** —— 一个慢任务卡住了后续 | 第 2 章 §2.4 |
| 每个任务前面都有一小段等待 | **调度或 fetch 延迟** | 第 8 章 §8.9 |
| worker 轨道频繁起停 | **worker 冷启动** —— 任务太短 | 第 18 章 §18.1 |

### ② Task Events：每个任务的时间都花在哪

```python
from ray.util import state

for task in state.list_tasks():
    print(task["task_id"], task["name"],
          task["state"],
          task.get("start_time_ms"), task.get("end_time_ms"),
          task.get("attempt_number"))
```

Task Events 会把一个任务拆成几个阶段（等待调度 / 等待依赖 / 执行），
**这才是回答「任务为什么不跑」的正确工具**（第 5 章 §5.1 的状态机 + 第 11 章 §11.2）。

> ⚠️ **2.58 把 task events 移出了 GCS 热路径**（第 20 章 §20.5 的「控制面瘦身」）。
> 这个改动会**影响 task events 的采集与可见方式** ——
> 如果你从旧版本升级上来发现 task events 少了，先查这一条。
> **具体字段与保留策略本书未逐条核实**，请对照你所用版本的 release notes。

### ③ 给代码块打自定义区间

**用 NVTX** —— 它是 CUDA 生态的标准，`nsys` 原生支持，
而且不依赖 Ray 的内部实现：

```python
import torch

@ray.remote(num_gpus=1)
def slow_fn():
    torch.cuda.nvtx.range_push("interesting_part")
    try:
        ...
    finally:
        torch.cuda.nvtx.range_pop()
```

> ⚠️ **别去找 `ray.util.profile` 了 —— 它不存在。**
> `ray.util` 的 `__init__` 里**没有**导出一个叫 `profile` 的东西，
> `from ray.util import profile` 直接 `ImportError`。
> 想在任务内部打自定义区间，本节的答案就是上面的 **NVTX**：
> 它是 CUDA 生态的标准，`nsys` 原生支持，而且不依赖 Ray 内部。
> 如果只是想把「我关心的那段」和「框架开销」分开，
> 用 `py-spy record` 看调用栈里的函数名通常就够了，不必额外打点。

### 三件套与通用工具的分工

```
先 ray.timeline()          → 是不是分布式的锅?（空隙/队头阻塞/冷启动）
   │
   ├─ 是 → 按上表定位,改并行度/批大小/资源声明
   │
   └─ 否 → 任务内部慢,继续往下
        ├─ CPU → py-spy
        ├─ 内存 → memray
        └─ GPU  → nsys → ncu
```

---

## 31.7 一个完整的定位案例

**症状**：一个 4 worker × 1 GPU 的批量推理脚本，GPU 利用率只有 25%，
整体吞吐远低于预期。

```
第 1 步:开 RAY_PROFILING=1,导出 timeline
  → 发现:GPU worker 的轨道上有大量空隙,每个任务前后各有约 200ms 空白

第 2 步:看空隙在哪一侧
  → 任务开始前有 ~150ms 空白 → 这是「等待开始执行」,不是执行慢
  → 查 state.list_tasks() 的 start_time_ms
    → 发现任务从提交到开始执行平均 150ms

第 3 步:为什么是 150ms?
  → 集群 4 CPU / 4 GPU,而每个推理任务声明了 num_gpus=1 **且** num_cpus=1
  → 每个任务实际需要:1 CPU 做数据预处理 + 1 GPU 做推理
  → 4 个 GPU worker 把 4 个 CPU 全占了 → **预处理任务排不上队**
  → 这就是空隙的来源:GPU 在等 CPU 上的预处理

第 4 步:验证
  → py-spy dump 到某个 GPU worker → 栈停在等上游 ObjectRef
  → 确认不是 GPU 慢,是**流水线断了**

第 5 步:修
  → 把预处理拆成独立的、num_cpus=2 的 stage(Ray Data 的 map_batches)
  → 或者给集群加 CPU(GPU:CPU 比例失衡是最常见的配置错误之一)
  → GPU 利用率 → 85%
```

**这个案例的价值在于它的形状**：

> **「GPU 利用率低」的根因，通常不在 GPU 上。**
> 先用 timeline 把「执行慢」和「等」分开，再顺着「等谁」往上查 ——
> 而不是一上来就 `ncu` 那个 kernel。

同样形状的还有：
* 「训练慢」→ 先看是不是每个 step 都在等 DataLoader；
* 「任务卡住」→ 先看状态机（第 5 章 §5.1），而不是先怀疑代码。

---

## 31.8 工具选择决策表

| 你观察到 | 第一把刀 | 第二把刀 |
|---|---|---|
| 整体吞吐低，不知道从哪下手 | `ray.timeline()` | `state.summarize_tasks()` |
| 任务不开始 / 一直 PENDING | `state.list_tasks()` 的状态机 | 第 8 章 §8.9 排查表 |
| 某个任务特别慢 | `py-spy dump --pid` | `py-spy record` 火焰图 |
| CPU 打满但不干活 | `py-spy record` | 看是不是序列化（火焰图里找 `pickle`） |
| 内存单调上涨 | 按 PID 粗筛：`psutil` / `ray_component_rusage_*`；对象存储用 `ray memory --stats-only` | `memray attach` 精确定位 |
| GPU 利用率低 | `nsys` 看空隙 | 按空隙位置分流（数据/CPU/通信） |
| 单个 kernel 慢 | `nsys` 定位是哪个 kernel | `ncu -k <kernel>` |
| 跨节点慢 | `state.list_objects()` 看对象落在哪 | 第 7 章 §7.4 零拷贝条件 |
| 日志乱、找不到是谁 | `RAY_LOG_TO_STDERR=1` + `ray logs` | 手写 worker_id 前缀 |
| 任务卡在 NCCL | 第 30 章 §30.4 的排查顺序 | `NCCL_DEBUG=INFO` |

### 三条使用纪律

1. **先量级，再工具**。第 18 章 §18.1 的四类开销量级差两个数量级 ——
   先算一笔账判断「这是不是根本不该优化」，再动剖析器；
2. **一次只改一个变量**。同上，剖析结果要在**同一份输入、同一份集群配置**下对比；
3. **剖析器本身有开销**。`memray` 和 `ncu` 会让程序慢一个数量级，
   `py-spy` 几乎不慢。别拿 `ncu` 的数字去推断生产吞吐。

---

## 31.9 与 mini-ray 的关系

mini-ray 实现的是**这条工具链里最底层、也最不可替代的那一层：
时间线的数据来源**。

```
真实 Ray 的 timeline                  mini-ray 的 timeline
─────────────────────                ─────────────────────
task events (GCS, 可采样)      ←→    每次调度/执行都记一条事件
  ↓                                    ↓
ray.timeline() 导出 JSON        ←→    miniray.timeline() 导出 JSON
  ↓                                    ↓
chrome://tracing / Perfetto     ←→    外加一个自包含 HTML 甘特图
```

```python
import miniray as ray

ray.init(num_cpus=4, logging_level="warning")

@ray.remote
def work(n):
    import time
    time.sleep(0.05)
    return n

try:
    ray.get([work.remote(i) for i in range(8)])

    # ① Chrome Trace JSON —— 拖进 chrome://tracing 或 Perfetto
    events = ray.timeline("timeline.json")
    print(f"记录了 {len(events)} 条事件")

    # ② 自包含 HTML 甘特图 —— 双击就能看,不需要任何工具
    #    ⚠️ 两个路径必须不同:filename 是 JSON,html 是 HTML。
    #    都写同一个文件的话,HTML 会**原地覆盖**刚写出的 JSON,
    #    最后只剩 HTML(本书早先的示例就是这样,已修正)。
    ray.timeline("timeline.json", html="timeline.html")
    print("甘特图已写出")
finally:
    ray.shutdown()
```

> **真实 Ray 必须先 `RAY_PROFILING=1`；mini-ray 默认就记** ——
> 因为 mini-ray 不需要为「高吞吐下的事件开销」做取舍（`miniray/raylet.py`
> 里的 `_MAX_EVENTS` 上限就是它对应的小规模取舍，值 **20000**，
> 列在第 22 章 B.5 的「内部常量」表里）。
> **但「先导出 timeline 看空隙」这个动作本身，两边是一样的。**

第 6 章的模块走读里，`_timeline.py` 与 raylet 的事件记录是**唯一一个
"为了可观测性而存在"的子系统** —— 这本身就是个值得注意的设计事实：

> **一个分布式系统值不值得信任，一半取决于你能不能看见它在干什么。**
> Ray 在 2.58 专门把 task events 移出 GCS 热路径，
> 也是在为「既能看见、又不拖慢」做取舍。

---

## 31.10 本章小结

* **`print` 在分布式里会同时坏三次**（交叉、无归属、观测者效应）。
  先解决归属（worker_id 前缀 / `RAY_LOG_TO_STDERR`），再谈别的；
* **先分类再选刀**：CPU 用 `py-spy`、内存用 `memray`、
  GPU 用 `nsys`/`ncu`、**分布式层面用 `ray.timeline()`**；
* **D 类（分布式开销）永远该先查** —— 它是每个任务都要付的，
  且常常是数量级差异；
* **`RAY_PROFILING=1` + `RAY_task_events_report_interval_ms=0` 是 timeline 的两个前提**，
  缺一个就只有空文件（而且**不报错**，只有一条 warning）——
  这是「timeline 用不了」的头号原因；
* **火焰图只看宽度**。`time.sleep` 宽 = 在等（不是 CPU 问题），
  `pickle` 宽 = 序列化是瓶颈；
* **`nsys` 先跑一次，把问题分成「GPU 内」和「GPU 外」**，
  再决定要不要上 `ncu`（它的开销极大，别对训练脚本直接跑）；
* **「GPU 利用率低」的根因通常不在 GPU 上** —— §31.7 的案例里，
  是 CPU 被 GPU worker 占满导致预处理排不上队；
* **剖析器本身有开销**（`memray`/`ncu` 慢一个数量级，`py-spy` 几乎不慢），
  别拿它们的数字去推断生产吞吐；
* **mini-ray 实现了这条链的源头**（事件记录 → `timeline()` → Chrome Trace /
  HTML 甘特图），并且默认就开 —— 因为小规模下不必做采样取舍。

---

## 31.11 阶段回顾（注意：这不是全书结尾）

到这里，从 Ray 是什么、怎么用、内部怎么运作，到怎么实现一个简化版、
怎么上生产、出了问题怎么查，这一整套都走完了 —— **「机制与调优」这条主线到此结束**。

> **后面还有章节**（按需读，不必通读）：
> 第 32 章（实验追踪与 MLOps）、第 33 章（Ray CLI 全集）、
> 第 34 章（表格数据与传统 ML）、第 35 章（数据版本与模型注册）、
> 第 36 章（分布式追踪与 OTel）、第 37 章（LLM 推理引擎与性能优化）、
> 第 38 章（Ray 与 Agent 工作负载）、第 39 章（源码阅读与事实核查指南），
> 以及附录 A–G 的参考层。
> ⚠️ 本书早先在这里写过"全书到此结束"，那是**过时的** ——
> 后续修订新增了 32 章以后的内容。真正的收尾在**第 39 章**
> （第六轮追加了第 37 章讲推理引擎那一层，**第七轮追加了第 38 章讲
> Agent 工作负载**，**第八轮追加了第 39 章讲核查方法**；
> 全书的结语跟着移到了第 39 章末尾）。

如果只带走**三句话**：

1. **把 Ray 当编排层，不要当万能引擎。** 计算、通信、训练、推理
   各有更专业的层，Ray 的价值在「谁在哪、起几个、挂了怎么办」；
2. **所有结论都要能落到「哪一层负责」。** 显存 OOM 不是 Ray 的问题，
   调度不均不是 PyTorch 的问题 —— 找错层就会白花几小时；
3. **不确定的事，标注「不确定」。** 这个生态在快速变化
   （vLLM 正在把 Ray 降级为放置层，见第 20 章），
   唯一可靠的长期能力是**知道去哪儿核对**。

**开始动手吧** —— 挑一个你工作里的真实问题，用第 25 章的路径把它搬到 Ray 上：
第一层（骨架）先跑通，再逐层替换。

（离全书结尾还有 8 章，但**这一章之后你就可以动手了** ——
剩下的章节是按需查阅的纵深，不是前置条件。）
