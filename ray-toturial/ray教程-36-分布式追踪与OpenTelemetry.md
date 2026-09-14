仓库地址：https://github.com/hhk-png/cycle-agent

# 第 36 章：分布式追踪与 OpenTelemetry

> 第 11 章 §11.1 把可观测性分成四层 —— **状态 / 日志 / 指标 / 追踪**，
> 其中**最后一层（追踪）**写的是：
> 「**追踪（Tracing）：一次请求经过哪些组件**」。
> 然后**那一层再也没出现过** —— 该章其余部分讲的是 State API、task events、
> timeline、日志、指标、Distributed Debugger，**全都是"看单点"，
> 没有一个是"追一条链路"**。
>
> 这一章补上第 11 章承诺了却没兑现的那一层。
> 它回答的是分布式系统区别于单机的那个问题：
> **一次请求跨了 5 个 actor，我该从哪看起？**
>
> > ⚠️ **一个措辞上的自我纠正（第五轮）**：本书早先在这里写过
> > "一个 grep 就能确认：前 35 章里 `OpenTelemetry` 命中数是 **0**"
> > —— **这句话是自我否证的**，因为写出这句话的第 11 章那一行本身
> > 就是一处命中 —— 实测**第 01–35 章共 7 行**提到 `OpenTelemetry`
> > （分布在第 11、17、23、32、35 章；**把第 00 章前言也算上才是 8 行**）、
> > 3 行提到 `Jaeger`，但**都只是"这是缺口"式的交叉引用与术语表条目，
> > 没有一行真正讲过怎么做**。准确的说法是定性的：
> > **本轮之前，全书没有任何一章真正讲过分布式追踪怎么做。**
> > 用"命中数为 0"这种容易被读者一条 grep 推翻的论证，本身就是
> > 一个值得写进第 0 章 §0.6 的教训：**别用一个能被一条命令证伪的断言
> > 去支撑一个本来正确的结论。**

---

## 36.1 先分清四件事：它们回答的问题完全不同

这是本章最重要的一张表。**选错工具比不会用工具更浪费时间。**

| 工具 | 回答的问题 | 时间维度 | 粒度 | 在本书哪里 |
|---|---|---|---|---|
| **日志** | 这一刻**发生了什么**？ | 单点事件 | 一条消息 | 第 11 章 §11.4 |
| **指标** | 整体**健康度如何**？ | 时间序列 | 聚合数字 | 第 11 章 §11.5 |
| **Ray timeline** | 这段时间**机器在干什么**？ | 甘特图 | 进程/任务 | 第 11 章 §11.6 |
| **分布式追踪** | 这条**请求经过了谁**？ | 因果链（span 树） | 一次调用 | **本章** |

**用三个真实问题来体会差别**：

| 你的问题 | 去哪个工具 | 为什么别的工具答不了 |
|---|---|---|
| 「p99 延迟从 200ms 涨到 2s 了」 | **指标** | 日志里没有分布，只有单条 |
| 「这个任务为什么卡在 RUNNING？」 | **State API**（第 11 章 §11.2） | 追踪不知道"卡住"，它只记已完成/进行中的 span |
| 「**用户 A 的那次请求**为什么慢？」 | **追踪** | 指标是聚合的，看不到单条请求的分解；日志要靠 request-id 手工拼，且拼不出耗时分解 |
| 「这段时间 GPU 在空转吗？」 | **timeline** | 追踪只记"请求",不记"空闲" |

> **一句话记住分工**：
> **指标告诉你"出问题了"，追踪告诉你"问题在哪一段"，日志告诉你"那一段里发生了什么"。**
> timeline 是**第四个维度**（机器视角），不是追踪的替代品。

---

## 36.2 Ray 与 OpenTelemetry：先把边界说清楚

这是本章必须"诚实标注"的一节 —— 因为 Ray 的 OTel 支持在版本间一直在动。

### 分两层，不要混

```
┌─ 第二层:你的应用追踪 ──────────────────────────────────┐
│  一个用户请求 → ingress actor → 若干 worker actor      │
│  → 数据读取 → 模型推理 → 写回                          │
│  ⭐ 这一层是本章的主体,用标准 OpenTelemetry Python SDK │
│     实现,不依赖 Ray 的任何 OTel 支持                   │
└────────────────────────────────────────────────────────┘
┌─ 第一层:Ray 自身的内部追踪 ────────────────────────────┐
│  GCS / raylet / core worker 之间的 RPC 调用链          │
│  ✅ 2.58 里【有】,而且是源码树里可查的实现:            │
│   · ray/util/tracing/tracing_helper.py                 │
│     —— W3C _DictPropagator 的 inject/extract、         │
│        _function_with_tracing、_tracing_task_invocation│
│   · ray/util/tracing/setup_tempo_tracing.py            │
│     / setup_local_tmp_tracing.py                       │
│     —— 两个现成的 setup_tracing() 钩子,导出到 OTLP     │
│   · ray start --tracing-startup-hook=<module>          │
│     —— 公开的 CLI 开关(⚠️ hidden=True)                 │
│   · ray/remote_function.py 与 ray/actor.py 已经        │
│     import 并使用上面的注入逻辑                        │
│     ⇒ 所以它【也】能自动传播你的 context,见下面一节    │
│  ✅ 指标侧另有 OpenTelemetryMetricRecorder(第 17 章)   │
│     —— 那是 metrics,不是 tracing,别混。                │
│  ⚠️ 官方文档页 docs.ray.io/tracing.html 对版本敏感     │
│     (不同版本的内容差别很大),以你所用版本为准。        │
│     见下面的"怎么自己确认"                             │
└────────────────────────────────────────────────────────┘
```

> 📌 **本书第五轮在这里做过两次修正，方向正好相反。**
> 最早写的是"Ray 有基于 OpenTelemetry 的内部追踪实现"（对，但当时没给出处）；
> 第四轮因为找不到出处把它改成了"未确认"（**改错了 —— 它一直都在**）；
> 第五轮对着 2.58 的源码树逐条核对，改回**有**，并给出上面这串文件与开关。
> **教训是：把"我没查到"写成"不存在"和把"我记得有"写成"有"，是同一种错。**

**为什么本章主体仍然放在第二层**：**第一层是 Ray 团队用来排查
Ray 自身 RPC 的工具，不是用来排查你的请求的。**
你要回答的问题 99% 是"我的请求为什么慢"，而不是"raylet 的 RPC 为什么慢"。
而且**第二层完全用标准 OpenTelemetry 实现，不依赖任何 `hidden=True`
的 CLI 开关**，今天的代码三年后还能跑。
第一层的钩子更适合"我已经在用标准 OTel，想让 Ray 的跨 actor 边界也自动接上"时叠加使用。

### 怎么自己确认第一层的现状（给要深入的人）

不要相信任何二手博客（包括本书）写死的开关名。**三条命令就是答案**：

```bash
# ① 看内部追踪模块在不在(最直接 —— 有这两个文件就说明实现存在)
python -c "import ray,os;print(os.path.dirname(ray.__file__))" | xargs -I{} ls {}/util/tracing/

# ② 看 CLI 开关。⚠️ 它是 hidden=True 的,`ray start --help` 里【看不到】,
#    所以别去 help 里找 —— 直接 grep 源码:
python -c "import ray,os;print(os.path.dirname(ray.__file__))" | xargs -I{} grep -rn "tracing-startup-hook" {}

# ③ 看官方文档(带版本号) —— docs.ray.io/tracing.html
#    ⚠️ 这个页面对版本敏感,先看页面顶部的版本选择器:
#       你读的是不是你的版本?
```

> ⚠️ **一个通用的判断规则**（第 20 章 §20.9 讲过）：
> 任何带 `experimental_` / `_` 前缀的、以及"调试开关"性质的参数，
> **都当成随时会没**。`--tracing-startup-hook` 就是 `hidden=True` 的一类 ——
> 能用，但别把它当成稳定契约。**本章主体教的是标准 OTel，它不在这个风险里。**

### Ray 其实已经有自动传播：`_ray_trace_ctx`

**这是本节最该带走的一条事实。**

在 2.58 里，只要你在启动集群时挂上 tracing 钩子：

```bash
# 启动集群/worker 时指定一个 setup 模块 —— Ray 会把它写进 internal KV,
# 每个 worker 起来时 import 它一次(相当于"全集群的 OTel 初始化")
ray start --head --tracing-startup-hook=my_pkg.setup_tracing
```

（这个开关在 `scripts.py` 里是 `hidden=True`，不会出现在
`ray start --help` 的公开列表里；它的值存在 internal KV，
由 worker 启动时读出来执行。）

那么**从 `@ray.remote` 任务到 actor 方法之间的 span context 是 Ray 替你传的**：

* 发送侧：`tracing_helper.py` 会往 kwargs 里塞
  **`kwargs["_ray_trace_ctx"] = _DictPropagator.inject_current_context()`**
  （actor 方法走的是同一条路）；
* 接收侧：worker 在执行前做
  `with _use_context(_DictPropagator.extract(_ray_trace_ctx)), tracer.start_as_current_span(...)`，
  于是新 span **自动挂在上游 span 下面**（PRODUCER → CONSUMER 的语义）。

**所以"Ray 上做追踪必须自己传 context"这个说法是错的** ——
自己传只是**其中一条路**，而且不是默认那条。下一节讲的是这条路，
以及什么时候仍然该用它。

---

## 36.3 手动传播：当你不想（或不能用）自动那条路

### 为什么 Ray 上的 context 默认会断

在普通的 Web 微服务里，链路追踪**几乎是免费的**：
HTTP 请求头会自动携带 `traceparent`，中间件帮你 `extract`，
你什么都不用做就能在 Jaeger 里看到完整的调用链。

**在 Ray 里，默认没有这个"自动"** —— 因为 Ray 的调用是**跨进程的函数调用**，
不是 HTTP：

| | Web 微服务 | Ray |
|---|---|---|
| 调用载体 | HTTP 请求 | 序列化的函数 + 参数 |
| context 传播 | **框架自动**（请求头） | **默认不传播**；挂上 `--tracing-startup-hook` 后 Ray 用 `_ray_trace_ctx` 自动传（见上节），或者**你自己传**（作为参数） |
| 一个进程内 | 一个请求一个线程 | **一个 actor 处理多个请求** |
| 谁是谁 | 靠 URL 区分 | 靠 `task_id` / `actor_id` 区分 |

**第二行是关键**：Ray 的 worker 是**独立的 Python 进程**，
`opentelemetry.context` 是**进程内的线程局部变量** ——
**它跨不过 `ray.remote` 那道边界**。

**所以在你没有打开自动传播时，你必须自己把 context 显式地序列化、
作为参数传过去。** 下面讲的就是这条路 ——
它**依然值得学**，因为①自动传播只覆盖 Ray 内部的边界，
driver ↔ HTTP 入口这种边界它管不到；②想把 span 名/属性改成你业务的样子，
最终还是要自己插一手。

### 那件事叫什么：W3C Trace Context

好消息是**标准已经定好了**。OpenTelemetry 的 `propagate` 模块提供两个函数：

```python
from opentelemetry import propagate

carrier = {}                      # 一个普通的 dict
propagate.inject(carrier)         # 把当前 context 塞进 dict
# carrier == {"traceparent": "00-<trace_id>-<span_id>-01", ...}

propagate.extract(carrier)        # 从 dict 里恢复 context
```

**这个 `carrier` 就是一个 dict，可以 pickle，可以当 `ray.remote` 的参数传。**
**这就是"手动传播"这条路的全部魔法** ——
注意它和 Ray 内部 `_DictPropagator` 做的事**一模一样**，
只是由你自己在正确的位置调。

```
        driver / ingress
              │  with tracer.start_as_current_span("handle_request"):
              │      carrier = {}
              │      propagate.inject(carrier)        ← 序列化 context
              ▼
        ┌─────────────────────────┐
        │  carrier = {...}         │  ← 跟着函数参数一起跨进程
        └─────────────────────────┘
              │
              ▼   actor_b.remote(carrier, payload)
        worker 进程
              │  ctx = propagate.extract(carrier)   ← 恢复 context
              │  with tracer.start_as_current_span("worker_step", context=ctx):
              ▼      ...  ← 这个 span 会挂在**上游那个 span 下面**
```

**没有这两步（也没有打开 Ray 的自动传播），你会在 Jaeger 里看到一堆
互不相连的孤立 span** —— 它们各自有耗时，但**拼不成一条链**。
这是初学时最常见的现象。

---

## 36.4 一个完整可跑的跨 actor 追踪示例（手动传播版）

下面这段是**最小可用版本**，改动你自己的代码时基本就是照搬结构。
**它走的是手动传播那条路** —— 即完全不依赖 `--tracing-startup-hook`，
把 `carrier` 当普通参数传。这样不管你跑在哪个 Ray 版本上、
有没有权限改集群启动参数，它都能工作。
（如果你的集群已经挂了 tracing 钩子，这一节的
`propagate.inject()` / `propagate.extract()` 会与 Ray 的自动注入**叠加** ——
两处都在设 `_ray_trace_ctx` 对应的 context，选一条路走就行，别混。）

### ① 初始化（一次，在 driver 里）

```python
# otel_setup.py
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

def setup_tracing(service_name: str, otlp_endpoint: str | None = None):
    """在 driver 里调一次。worker 进程会各自调一次(见下)。"""
    resource = Resource.create({
        "service.name": service_name,
        # ⚠️ 把 Ray 的 job_id 放进 resource:同一个 job 的所有 span 能被一起筛出来
        "ray.job_id": os.environ.get("RAY_JOB_ID", "local"),
    })
    # ⚠️ 采样必须用 ParentBased 包住:否则下游 worker 可能"漏采",
    #    导致链路断在半路(父 span 采了、子 span 没采)
    sampler = ParentBased(TraceIdRatioBased(float(os.environ.get("OTEL_SAMPLE", "0.1"))))

    provider = TracerProvider(resource=resource, sampler=sampler)
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=otlp_endpoint or os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])
    ))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
```

> ⚠️ **`ParentBased` 不是可有可无的**。默认的采样器是"每条 trace 独立掷骰子"——
> 如果上游采了、下游独立掷骰子没采中，**链路就在中间断了**，
> 你会看到"span 有一半不见了"。**这一步漏掉是新手最常见的坑。**

### ② worker 侧的初始化（关键：每个进程都要有自己的 provider）

```python
# ⚠️⚠️ 这是本章最容易错的一处:
#   TracerProvider 是**进程级**的。driver 里 setup 了,**worker 进程里没有** ——
#   因为 worker 是另外的解释器。
#   所以每个 worker 第一次运行时,必须自己 setup 一次。

_worker_tracer = None

def get_tracer():
    global _worker_tracer
    if _worker_tracer is None:
        # worker 侧通常不导出到自己,而是沿用 driver 的配置
        _worker_tracer = setup_tracing(
            service_name="ray-worker",
            otlp_endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"],
        )
    return _worker_tracer
```

**把 `OTEL_EXPORTER_OTLP_ENDPOINT` 通过 `runtime_env` 送进去**（附录 F §F.2）：

```python
ray.init(runtime_env={"env_vars": {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4317",
    "OTEL_SAMPLE": "0.1",
}})
```

### ③ 跨 actor 传 context

```python
import ray
from opentelemetry import propagate, trace

@ray.remote
class Embedder:
    def __init__(self):
        self.tracer = get_tracer()          # ← worker 侧初始化

    def embed(self, carrier: dict, texts: list[str]) -> dict:
        ctx = propagate.extract(carrier)                     # ← 恢复上游 context
        with self.tracer.start_as_current_span("embed", context=ctx) as span:
            self._tag_ray_ids(span)                          # ← 见 §36.5
            span.set_attribute("embed.batch_size", len(texts))
            vecs = self._model.encode(texts)
            span.set_attribute("embed.dim", len(vecs[0]))
            return {"carrier": _out_carrier(), "vecs": vecs}

@ray.remote
class VectorStore:
    def __init__(self):
        self.tracer = get_tracer()

    def search(self, carrier: dict, vec: list[float]) -> dict:
        ctx = propagate.extract(carrier)
        with self.tracer.start_as_current_span("vector_search", context=ctx) as span:
            self._tag_ray_ids(span)
            hits = self._index.search(vec, k=10)
            span.set_attribute("search.hits", len(hits))
            return {"carrier": _out_carrier(), "hits": hits}

def _out_carrier() -> dict:
    """把**当前** context 序列化出去,供下游 extract。"""
    c = {}
    propagate.inject(c)
    return c
```

### ④ 编排：一条请求的入口

```python
@ray.remote
def handle_query(raw: str) -> str:
    tracer = get_tracer()
    # ⚠️ 入口自己起一个 root span,后面的都挂在它下面
    with tracer.start_as_current_span("handle_query") as span:
        span.set_attribute("query.length", len(raw))
        span.set_attribute("query.text", raw[:80])       # ⚠️ 见 §36.6 隐私注意

        c0 = _out_carrier()
        emb = ray.get(embedder.embed.remote(c0, [raw]))

        c1 = _out_carrier()
        res = ray.get(store.search.remote(c1, emb["vecs"][0]))

        span.set_attribute("search.hits", len(res["hits"]))
        return res["hits"][0]["text"]
```

### ⑤ 在 Jaeger 里能看到什么

```
handle_query                       [================= 340ms =================]
├── embed                          [==== 120ms ====]
│   └── (模型前向的 span,如果你也埋了)
└── vector_search                                   [== 210ms ==]
```

**这一眼就回答了三件指标和日志都答不了的问题**：
* 340ms 里**哪一段是大头**（`vector_search` 210ms）；
* `embed` 和 `vector_search` 是**串行**的（如果能并行，理论上能省 120ms）；
* 这次请求**经过了哪两个 actor**。

> **这就是追踪的不可替代性**：它是唯一能把"一次请求"的**时间分解**和
> **调用拓扑**同时呈现出来的工具。

---

## 36.5 让追踪真正好用的关键：把 Ray 的 ID 写进 span

**这是本章最有实用价值的一节。** 埋点只做了 §36.4 的话，
你会在追踪 UI 里看到漂亮的链路，然后**想查这个 span 对应哪个 Ray 任务时，无从下手** ——
因为两边没有关联键。

**解法：把 Ray 的运行时 ID 作为 span 属性写进去。**

```python
import ray

def _tag_ray_ids(self, span):
    """把 Ray 的身份信息挂到当前 span 上 —— 打通追踪与 State API 的桥。"""
    rc = ray.get_runtime_context()
    span.set_attribute("ray.task_id",   rc.get_task_id())
    span.set_attribute("ray.node_id",   rc.get_node_id())
    span.set_attribute("ray.worker_id", rc.get_worker_id())
    # actor 里才有:
    try:
        span.set_attribute("ray.actor_id", rc.get_actor_id())
        span.set_attribute("ray.actor_name",
                           ray.get_runtime_context().get_actor_name() or "")
    except Exception:
        pass
    # 第 35 章的血缘字段也可以带上,让"慢的那批请求"和"哪个模型版本"对上
    span.set_attribute("model.version", os.environ.get("MODEL_VERSION", "unknown"))
```

**做完这一步，四条数据能互相跳转**：

| 从 | 到 | 怎么跳 |
|---|---|---|
| Jaeger 里的慢 span | State API 里的那个任务 | 拿 `ray.task_id` → `ray get tasks <id>` |
| 日志里的报错 | 对应的 trace | **把 `trace_id` 打进日志**（见下） |
| 指标异常的时间段 | 那段时间的慢请求 | 按 `model.version` 分组筛 |
| 线上的模型 | 训练它的实验 | `model.version` → 第 35 章的注册表 |

### 把 `trace_id` 打进日志（另一半的桥）

```python
import logging
from opentelemetry import trace

class TraceIdFilter(logging.Filter):
    """让每条日志自动带上 trace_id / span_id。"""
    def filter(self, record):
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            record.trace_id = format(ctx.trace_id, "032x")
            record.span_id  = format(ctx.span_id, "016x")
        else:
            record.trace_id = record.span_id = "-"
        return True

# 接到 Ray 的 logger 上
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [trace_id=%(trace_id)s span_id=%(span_id)s] %(message)s"
))
handler.addFilter(TraceIdFilter())
logging.getLogger("ray").addHandler(handler)
```

**输出长这样**：

```
2026-09-14 10:23:01 ERROR [trace_id=4bf92f3577b34da6a3ce929d0e0e4736 span_id=00f067aa0ba902b7] ...
```

**然后你就能在 Jaeger 里用这个 `trace_id` 搜出完整的链路，在日志系统里搜出这条链路里的所有报错。**
**这是"日志、指标、追踪三件套"真正结合起来的地方** ——
不打通的话，它们只是三个各自漂亮的孤岛。

---

## 36.6 四个必须注意的实践问题

### ① 采样率：别默认 100%

```python
sampler = ParentBased(TraceIdRatioBased(0.1))     # 10%
```

* **100% 采样在 Ray 上尤其危险** —— 一个任务可能产生几十个 span，
  高 QPS 下导出器本身会成为瓶颈（`BatchSpanProcessor` 会把内存吃满）；
* **但排查期要能临时调高**：用环境变量 `OTEL_SAMPLE`（§36.4 的写法），
  不要改代码；
* ⚠️ **`ParentBased` 必须包住**（§36.4 的注释）—— 否则链路会在中间断掉。

### ② 别把敏感数据写进 span 属性

```python
# ✗ 危险:span 属性会被**完整导出**到追踪后端,并长期保留
span.set_attribute("user.email", email)
span.set_attribute("query.text", raw_query)

# ✓ 做法:只记**形状**,不记**内容**
span.set_attribute("user.id_hash", hashlib.sha256(email.encode()).hexdigest()[:12])
span.set_attribute("query.length", len(raw_query))
```

**追踪后端的保留策略通常比日志宽松得多**（因为它的用途就是"回看"）——
**写进去就等于长期留存**。这一条在合规场景里是硬要求。

### ③ span 的粒度：一个 actor 方法一个，不是一个操作一个

```python
# ✗ 太细:每个循环都开 span → 导出量爆炸、UI 不可读
for text in texts:
    with tracer.start_as_current_span("encode_one"):
        ...

# ✓ 合理:一个方法一个 span + 属性记规模
with tracer.start_as_current_span("encode_batch") as span:
    span.set_attribute("batch_size", len(texts))
    ...
```

**判据**：**这个 span 的耗时会是你想单独优化的一段吗？**
是 → 开；不是 → 用属性记下来就够了。
在一个 batch 内做 1000 个 span，只会让你在 UI 里什么都看不见。

### ④ 开销：本地实测一下，别猜

追踪**不是免费的**：`BatchSpanProcessor` 有内存开销，
导出有网络开销，埋点有 CPU 开销。

```python
# 用第 18 章 §18.6 的实测模板,跑两次对比:
#   OTEL_SAMPLE=0    (完全关)
#   OTEL_SAMPLE=0.1  (10% 采样)
# 看 throughput_per_s 的差异
```

**经验**：采样后的开销通常在 **1–5%** 量级；
**但 100% 采样 + 细粒度埋点会到 20%+**，那就得不偿失了。
**这一条和第 31 章 §31.11 的"剖析器本身有开销"是同一个道理。**

---

## 36.7 部署：最小可用的追踪后端

你不需要一步到位上生产级的方案。**本地开发用一个 all-in-one 就够**：

```yaml
# docker-compose.yml —— 本地追踪的最小栈
services:
  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "16686:16686"    # UI
      - "4317:4317"      # OTLP gRPC ← 你的应用往这里发
    environment:
      - COLLECTOR_OTLP_ENABLED=true
```

```bash
docker compose up -d
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
python your_ray_job.py
# 打开 http://localhost:16686 选 service → 找 trace
```

**生产环境的三条选择**：

| 方案 | 特点 | 什么时候选 |
|---|---|---|
| **Jaeger** | 专做追踪，UI 好用，部署简单 | 只想上追踪 |
| **Grafana Tempo** | 与 Grafana/Loki/Prometheus 同一套栈 | 已经在用 Grafana（多数团队的答案） |
| **云厂商托管** | 免运维，按量计费 | 不想自己运维存储 |

> ⚠️ **不管选哪个，中间加一个 OTel Collector**：
> 应用只往 collector 发（`OTLP`），由 collector 决定往哪转、怎么采样、
> 怎么加属性。**这样换后端不用改应用代码** ——
> 这是 OpenTelemetry 存在的全部意义。

**配置好 Collector 之后，你能做的事**：

```yaml
# otel-collector-config.yaml 的核心几行
processors:
  tail_sampling:                      # ← 只留"有问题的"trace
    policies:
      - name: errors                  # 有错误的全留
        type: status_code
        status_code: {status_codes: [ERROR]}
      - name: slow                    # 超过 1 秒的全留
        type: latency
        latency: {threshold_ms: 1000}
      - name: sample_rest             # 其余 1%
        type: probabilistic
        probabilistic: {sampling_percentage: 1}
```

**`tail_sampling` 是追踪相对指标的一个独特优势**：
**你可以"先全收，再决定留哪些"** —— 这样"慢请求"和"报错请求"永远留得住，
而正常请求只留 1%。**采样率的取舍问题就这样被消掉了大半。**

---

## 36.8 什么时候不该上追踪

和本书其他"什么时候别用"的章节一样，这一节和前面的内容一样重要。

| 你的情况 | 该做什么 | 不该做什么 |
|---|---|---|
| 单机、一个进程 | 用日志 + 第 31 章的 `py-spy` | 引入追踪栈 |
| 想知道"集群整体健康" | **指标**（第 11 章 §11.5） | 追踪（它是按请求的，看不到聚合） |
| 想查"任务为什么卡住" | **State API**（第 11 章 §11.2） | 追踪（卡住的任务没有完成的 span） |
| 想做性能剖析 | 第 31 章（`py-spy` / `nsys` / `memray`） | 追踪（它的时间精度是 span 级，不是行级） |
| 请求跨 **3 个以上** actor/task，且**延迟问题说不清在哪一段** | ⭐ **这就是追踪的主场** | —— |

**最后一行是唯一的判据。** 如果你的负载是"一次批量跑 10 万个独立任务"，
追踪帮不上忙 —— 你要的是**指标 + timeline**。
但如果是"一个用户请求触发一串 actor 协作"（LLM 服务、agent、RAG、推荐打分），
**追踪就是那个能一眼看到时间去哪了的工具**。

> **和第 20 章 §20.8 的那句话对齐**：
> **把每个工具用在对的层上**。追踪很贵（埋点、导出、存储、维护），
> **只在"链路长 + 延迟说不清"时它才划算。**

---

## 36.9 与 mini-ray 的关系

**mini-ray 没有、也不会实现分布式追踪** —— 这是设计取舍：

* `opentelemetry` 是**第三方依赖**，而 mini-ray 的硬约束是
  **"纯 Python 标准库 + NumPy"**（`mini-ray/README.md` 的取舍清单）；
* 更重要的是，**追踪的价值在"跨进程、跨节点"** ——
  而 mini-ray 的所有"节点"都在一个进程里，
  **它连"context 跨进程会断"这个问题都复现不出来**。

> **但有一个真实的教学连接点**：mini-ray 的 **timeline 实现**
> （`miniray/_timeline.py`，Chrome Trace + 自包含 HTML 甘特图）
> 和追踪**共享同一个数据模型** —— 都是"带 parent 关系的区间集合"。
>
> **区别只在"记的是什么"**：
> * timeline 记的是**进程/任务在时间轴上的占用**（机器视角）；
> * 追踪记的是**一次请求的因果链**（请求视角）。
>
> 打开 `mini-ray/examples/09_observability.py` 生成的 HTML 甘特图，
> 你会看到和 Jaeger 里**一模一样的父子嵌套结构** ——
> **只是 timeline 的父子关系来自"任务依赖"，追踪的来自"context 传播"。**
> 想清楚这一点，你就同时理解了这两个工具。

**动手练习**：给 `miniray/_timeline.py` 的数据结构加一个 `trace_id` 字段，
让同一个"逻辑请求"触发的多个任务共享一个 trace_id ——
**这就是"在 Ray 上实现追踪"的核心，只是规模小了一万倍。**

---

## 36.10 本章小结

* **四层观测的分工**：日志（这一刻发生了什么）、指标（整体健康度）、
  timeline（机器在干什么）、**追踪（这条请求经过了谁）**。
  **选错工具比不会用工具更浪费时间。**
* **追踪的主场只有一个判据**：**请求跨 3 个以上 actor/task，
  且延迟问题说不清在哪一段**。批量独立任务用指标 + timeline，不用追踪。
* **Ray 2.58 自带的自动传播是真的**：`ray start --tracing-startup-hook=<module>`
  + 内部的 `_ray_trace_ctx`，会在 `@ray.remote` 任务与 actor 方法之间
  自动 inject/extract W3C context（源码见 §36.2 列的那串文件）。
  **但它默认关着**，而且这个开关是 `hidden=True` 的。
* **默认情况下的核心难点**：`opentelemetry.context` 是**进程内**的，
  **跨不过 `ray.remote`** —— 这时才需要你自己用 `propagate.inject()` /
  `propagate.extract()` **把 context 序列化成 dict 当参数传**。
  **这条路的适用场景是**：想自定义 span 名/属性，或要跨过
  Ray 覆盖不到的边界（如 driver ↔ HTTP 入口）—— 它不是唯一的路。
* **`TracerProvider` 是进程级的**：driver 里 setup 了，**worker 里没有** ——
  每个 worker 进程要自己 setup 一次（用 `runtime_env` 把配置送进去）。
* **采样器必须用 `ParentBased`** 包住，否则链路会断在中间。
* **把 Ray 的 ID 写进 span 属性**（`task_id` / `actor_id` / `node_id` / `worker_id`）、
  **把 `trace_id` 打进日志** —— 这两步是打通"日志/指标/追踪"的桥。
  不做的话，它们只是三个各自漂亮的孤岛。
* **别把敏感数据写进 span**：追踪后端通常长期保留，写进去等于留存。
* **span 粒度**：一个 actor 方法一个，不是一个循环一个。
  判据是"这段耗时会是你想单独优化的吗"。
* **采样 + Collector 的 `tail_sampling`** 能消掉大半的采样率取舍：
  **先全收，错误和慢请求永远留，正常请求留 1%。**
* **本章主体用标准 OpenTelemetry 实现**，不依赖 Ray 的追踪开关 ——
  今天写的代码三年后还能跑。Ray 自带的那条自动传播是**可选的加速**，
  它的 CLI 开关是 `hidden=True`、官方文档页也对版本敏感；
  §36.2 给了自己确认的三条命令。
* **mini-ray 不实现追踪**（第三方依赖 + 跨进程问题复现不出来），
  但它的 **timeline 与追踪共享同一个数据模型**，
  区别只在"父子关系来自任务依赖还是 context 传播"。

---

## 36.11 本章之后

> **全书的结语不在这里。** 第六轮新增了第 37 章（LLM 推理引擎与性能优化），
> **第七轮新增了第 38 章（Ray 与 Agent 工作负载）**，
> **第八轮又新增了第 39 章（Ray 源码阅读与事实核查指南）**，它现在排在最后 ——
> 全书的结语也随之移到了
> **[第 39 章的「结语：这本书真的讲完了」](ray教程-39-Ray源码阅读与事实核查指南.md)**。
>
> 这一章自己该带走的一句话，留在 §36.10 的小结里：
> **追踪不是"再加一个后端"，而是把"一次请求跨了哪几个组件"变成可查询的数据。**
>
> 如果你是从这一章直接跳到文末的，往下读的顺序建议是：
> 第 37 章（推理引擎那一层）→ 第 38 章（Agent 工作负载）
> → 第 39 章（核查方法）→ 结语。
