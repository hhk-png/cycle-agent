仓库地址：https://github.com/hhk-png/cycle-agent

# 附录 E：端到端实战案例

> 把整本书的能力拼成一条真实流水线：**数据准备 → 批量推理 → 训练 → 在线服务 → 可观测性 → 容错**。
> 配套代码 `mini-ray/examples/11_end_to_end.py`，**可以直接运行**（纯 Python + NumPy，无需 GPU）。
> 每一节都给出「真实 Ray 里对应哪个组件」，所以这份案例可以直接迁移到生产栈。

---

## E.1 案例全景

```
 ① 数据准备        4 个分片 → 清洗/特征化(任务链,依赖自动并行)
      │
 ② 批量推理        ActorPool + 常驻模型(模型只加载一次)
      │
 ③ 训练            放置组 + 参数服务器 + 数据并行 worker(资源原子性预留)
      │
 ④ 在线服务        有状态推理端点(真实场景 = Ray Serve)
      │
 ⑤ 可观测性        State API + timeline 甘特图
      │
 ⑥ 容错演练        注入 worker 崩溃 + 丢弃全部对象,验证重试与 lineage 重建
```

| 阶段 | 真实 Ray 对应 | 本文用到的 Ray Core API |
|---|---|---|
| ① 数据准备 | Ray Data（`read_*` + `map_batches`） | `@ray.remote`、ObjectRef 依赖 |
| ② 批量推理 | Ray Data LLM：`vLLMEngineProcessorConfig` + `build_processor` | `util.ActorPool`、actor 常驻 |
| ③ 训练 | Ray Train（+ Ray Tune 调参） | 放置组、`PlacementGroupSchedulingStrategy` |
| ④ 在线服务 | Ray Serve / Ray Serve LLM | actor 方法调用 |
| ⑤ 可观测性 | `ray.util.state` + Dashboard + `ray.timeline`（⚠️ 真实 Ray 的 `timeline(filename)` **不接受 `html=`**，见 E.6） | `state.*`、`timeline()`、`timeline(..., html=)`（**后者是 mini-ray 专有**） |
| ⑥ 容错 | 同样的机制（重试 / lineage 重建 / actor 重启） | `max_retries`、故障注入 |

**跑起来**：

```bash
cd ray-toturial/mini-ray
python examples/11_end_to_end.py
```

下面逐段讲解，并给出真实 Ray 的等价写法。

> 🔴 **读之前必看：下面每个片段都是"幻灯片"，单独复制粘贴跑不起来。**
> 它们是 `examples/11_end_to_end.py` 的**删节摘录**（省掉了 `print`、错误分支、
> 以及一部分变量定义），**片段之间是承接关系**，`feature_batches` / `server` /
> `losses` / `vectorizers` / `requests` / `workdir` 这些变量和 `Vectorizer` 类
> **都只在源文件里定义过，本章正文里不重复定义**。
>
> **想跑，就用源文件**（上一条命令）；想验证理解，就对着源文件看本章的讲解。
> 逐字抄本章片段会依次撞上 `NameError`（缺变量）和 `NameError: name 'ray'
> is not defined`（缺 import）。
>
> **如果你确实要在自己的脚本里照抄，先补上这一份前置（源文件的头部）：**
>
> ```python
> from __future__ import annotations
> import os, random, sys, time
> import miniray as ray                              # 换成真实 Ray 就写 import ray
> from miniray import state
> from miniray.util import ActorPool
> from miniray.util.placement_group import (
>     placement_group, remove_placement_group,      # ⚠️ remove_ 这个别漏
> )
> from miniray.util.scheduling_strategies import PlacementGroupSchedulingStrategy
> ```
>
> **集群是这样起的**（源文件 `main()` 的开头，**本章正文里没有重复这一段**，
> 所以你在上面任何片段里都找不到 `ray.init()`）：
>
> ```python
> def main() -> None:
>     import tempfile
>     ray.init(num_cpus=6, num_gpus=2, object_store_memory=256 * 1024 * 1024)
>     workdir = tempfile.mkdtemp(prefix="miniray-e2e-")   # ← 后面 ⑤⑥ 段用的 workdir
>     ...
> ```
>
> 🔴 **`num_cpus=6` 这个数字要记住** —— E.4 那段"CPU 被 actor 占死"、
> 以及"放置组必须释放"的推算**全都建立在"集群只有 6 个 CPU"上**。
> 换成真实 Ray 时，你要自己写 `ray.init()`（或在 `ray start` 起的集群里靠
> `RAY_ADDRESS` 自动连，见附录 D 的 Q5）。

---

## E.2 ① 数据准备：用依赖表达流水线

> 本节及后续的 `@ray.remote` 定义**承接上面的前置**：`ray` / `random` 已导入，
> `feature_batches` 等变量在源文件的 ② 段里定义。

```python
@ray.remote
def make_shard(shard_id: int, rows: int = 200):
    """模拟「从数据湖读一批数据」。真实场景:ray.data.read_parquet(...)"""
    rng = random.Random(shard_id)
    return {"shard": shard_id,
            "rows": [{"text": f"doc-{shard_id}-{i}", "label": rng.randint(0, 1)}
                     for i in range(rows)]}

@ray.remote
def preprocess(shard: dict) -> dict:
    tokens = [len(row["text"].split("-")) for row in shard["rows"]]
    return {"shard": shard["shard"], "features": tokens,
            "labels": [row["label"] for row in shard["rows"]], "count": len(tokens)}

@ray.remote
def summarize(partials: list) -> dict:
    ...
```

```python
raw = [make_shard.remote(i) for i in range(4)]
prepared = [preprocess.remote(shard) for shard in raw]     # 依赖自动建立
summary = ray.get(summarize.remote(prepared))              # 聚合成一个结果
```

**这一段教什么**：

* 依赖是**数据流隐式表达**的 —— 传 ref 就是声明依赖；
* 每个 `preprocess` 只等**自己那一个**分片，不等整批（这是与「按阶段调度」的本质区别）；
* `summarize` 收到 4 个 ref，会在开始时**一次性拉齐** 4 个依赖对象 ——
  所以「聚合点」的依赖体积要心里有数。

**真实 Ray 写法**（Ray Data）：

```python
import ray
from ray.data import ActorPoolStrategy, TaskPoolStrategy   # ← 池策略要显式导入(两个都在这里)

ds = ray.data.read_parquet("s3://bucket/events/")
ds = ds.map_batches(
    preprocess_fn, batch_format="pandas",
    compute=ActorPoolStrategy(size=4),      # ⚠️ 不是 concurrency=N —— 2.51 已弃用
)
print(ds.aggregate(...))     # 或 ds.take_all()
```

> **只想用默认的 task 池**（无状态 UDF、不需要复用）：`compute=TaskPoolStrategy()`
> 或干脆不传 `compute`。**需要复用对象**（加载了模型的类 UDF、tokenizer）才用
> `ActorPoolStrategy` —— size 就是常驻 actor 数（详见第 12 章 §12.2）。
> ⚠️ **`TaskPoolStrategy` 和 `ActorPoolStrategy` 一样，都在 `ray.data` 里，
> 都得显式 import**（`python/ray/data/__init__.py` 同时导出这两者）——
> 上面那行 `from ray.data import ...` 里已经把两个都写上了。
> 只 import 了 `ActorPoolStrategy` 就在正文用 `TaskPoolStrategy()`，
> 会直接 `NameError`；本书早先的片段就是这个问题，已补。

---

## E.3 ② 批量推理：模型常驻 + ActorPool

```python
@ray.remote
class Predictor:
    """真实场景里 __init__ 里加载的是 transformer 权重(GPU 上)。"""

    def __init__(self, model_version: str = "v1"):
        self.version = model_version
        self.weights = [0.1, 0.2, 0.3]      # 假装是模型参数(教学实现里是常数)
        self.served = 0

    def predict_batch(self, features: list) -> list:
        self.served += len(features)
        return [
            sum(f * w for f, w in zip(features, self.weights)) for _ in range(len(features))
        ]

    def stats(self) -> dict:
        return {"version": self.version, "served": self.served}
```

> 🟡 **注意 `predict_batch` 的返回值是"退化"的 —— 这不是一个正确的批量推理示范。**
> 看那行列表推导：`sum(f * w for f, w in zip(features, self.weights))` 里
> **`f` 的取值范围是整批 `features`，不随 `_` 变化** —— 所以它算出**一个标量**，
> 然后 `for _ in range(len(features))` 把这个**同一个值复制 n 份**返回。
> 实跑一遍：**传入 200 条，返回 200 个一模一样的 `1.8`**
> （`features` 全是 token 数 3.0，`weights=[0.1,0.2,0.3]` →
> `3.0*(0.1+0.2+0.3) = 1.8`；`zip` 按 `weights` 的 3 个元素截断）。
>
> **这是为了教学简化的退化输出**：本例的重点是"ActorPool + 模型常驻 + 背压"
> 这套**编排骨架**，不是推理数学。**真实的 `predict_batch` 必须逐条返回**
> ——每条记录对应一个结果，形状还与输入 batch 对齐，比如：
>
> ```python
> def predict_batch(self, batch: list[list[float]]) -> list[float]:
>     return [sum(f * w for f, w in zip(row, self.weights)) for row in batch]
> ```
>
> ⚠️ **别把这个退化版本抄进生产**：它在下游表现为"所有样本分数相同"，
> 而且**不会报错** —— 你会以为模型坏了、去查权重，其实是这里的 bug。

> ⚠️ **上面这段是从 `mini-ray/examples/11_end_to_end.py` 逐字抄来的**。
> 本书早先的版本写了 `load_model(...)` / `self._forward(...)` ——
> 那两个名字**在示例源码里并不存在**，读者对照代码时会以为拿错了文件。
> **本章的代码以 `examples/11_end_to_end.py` 为准**（它可以直接跑）；
> 为了讲清一个点，个别片段做了**删节**（比如省掉 `print` 与错误分支），
> 遇到对不上的地方**一律以源文件为权威**。

```python
pool = ActorPool([Predictor.remote("v1") for _ in range(2)])
results = []
for batch in pool.map_unordered(
    lambda actor, rec: actor.predict_batch.remote(rec["features"]), feature_batches[:12]
):
    results.extend(batch)
```

**这一段教什么**：

1. **模型必须常驻**：如果写成普通任务，每个任务都会重新加载一次模型 ——
   这是新手最常犯的性能错误；
2. **`ActorPool` 自带背压**：它保证「每个 actor 手里最多一个任务」，
   所以不会把邮箱和对象存储挤爆（内部用 `ray.wait` 实现，见第 5 章）；
3. **`map_unordered` vs `map`**：前者吞吐优先（谁先算完先返回），
   后者顺序优先但有队头阻塞；推理场景通常用前者。

**一个必须知道的坑（本例用 `ray.kill` 正面处理了）**：actor 的资源是**终身持有**的。
在 6 CPU 的集群上创建 2 个 `Vectorizer` + 2 个 `Predictor`（各占 1 CPU）
再加一个参数服务器（1 CPU），**5 CPU 会被长期占住** ——
后面的训练阶段就会没有 CPU 可用。

⚙️ **本例是怎么绕开的**：示例在创建放置组与参数服务器**之前**就
`ray.kill` 掉了两个 `Vectorizer`（`examples/11_end_to_end.py` 里
`ray.kill(vectorizer)` 位于 `placement_group(...)` 之前），
所以**这 5 个 actor 从未同时在世** ——
真正长期占住的是 `Predictor` 与参数服务器。

> ⚠️ **本书早先把这处写成"本例踩到了 5 CPU 被占死"，那与源码时序不符** ——
> 代码里确实做了 `ray.kill`，只是**位置在创建放置组之前**。
> 但**结论完全成立**：如果不 kill，训练阶段就会因为前面那 5 个 CPU
> 被占住而永远起不来。这正是"看到 actor 用完不杀"这类事故的形状。

```python
for vectorizer in vectorizers:
    ray.kill(vectorizer)     # 用完就杀,把资源还给集群
```

真实 Ray 里同样如此。**不需要的 actor 一定要 `ray.kill`**，
否则资源会一直被占着（actor 泄漏是生产环境的常见事故）。

**真实 Ray 写法**（Ray Data LLM）：

```python
from ray.data.llm import vLLMEngineProcessorConfig, build_processor

config = vLLMEngineProcessorConfig(
    model_source="meta-llama/Llama-3.1-8B-Instruct",
    concurrency=4,              # 自动扩到 4 个 GPU stage
    batch_size=32,
    engine_kwargs={"max_model_len": 4096},
)
processor = build_processor(config, preprocess=..., postprocess=...)
ds = processor(ds)              # 输入 Dataset → 输出带生成结果的 Dataset
```

---

## E.4 ③ 训练：放置组 + 参数服务器

```python
@ray.remote
class ParameterServer:
    def __init__(self, dim): self.weights = [0.1] * dim; self.version = 0
    def pull(self): return list(self.weights), self.version
    def push(self, gradients, lr):
        for i, g in enumerate(gradients): self.weights[i] -= lr * g
        self.version += 1
        return self.version

@ray.remote
def train_worker(server, shard: dict, lr: float = 0.05) -> float:
    """一个训练 worker:拉权重 → 算梯度 → 推回去。"""
    weights, _ = ray.get(server.pull.remote())
    dim = len(weights)
    features, labels = shard["features"], shard["labels"]
    n = len(features)

    gradients = [0.0] * dim
    loss = 0.0
    for i in range(n):
        x = float(features[i] % 5)
        y = float(labels[i])
        pred = sum(w * x for w in weights) / dim
        err = pred - y
        loss += err * err
        for d in range(dim):
            gradients[d] += 2 * err * x / (n * dim)

    ray.get(server.push.remote(gradients, lr))
    return loss / n
```

> ⚠️ **别照抄"看起来合理"的简写**：本书早先这里写的是
> `gradients = compute_gradients(weights, shard)` + `return loss` ——
> **`compute_gradients` 和 `loss` 在示例里都没有定义**，抄下来会直接 `NameError`。
> 梯度计算是**内联展开**的，这是刻意的：让"参数服务器协议"本身成为焦点，
> 而不是把细节藏进一个函数里。

```python
pg = placement_group([{"CPU": 1}, {"CPU": 1}], strategy="PACK")
ray.get(pg.ready())                      # ← 关键:等资源真的占下来

for epoch in range(5):
    refs = [
        train_worker.options(
            scheduling_strategy=PlacementGroupSchedulingStrategy(
                pg, placement_group_bundle_index=i % 2)
        ).remote(server, feature_batches[i])
        for i in range(2)
    ]
    losses.append(sum(ray.get(refs)) / len(refs))

remove_placement_group(pg)               # 🔴 必须释放!否则后面节会超时,见下
```

> 🔴 **`remove_placement_group(pg)` 这一行不能省 —— 漏掉它，照本章流程抄下去会在 E.7 超时失败。**
> 放置组占的 bundle 资源是**从公共池里挖走的、在显式释放前不会还回去**，
> 而集群只有 **6 个 CPU**（`main()` 里 `ray.init(num_cpus=6, ...)`）。到 E.7 那一刻还活着的占用者：
>
> | 占用者 | CPU | 说明 |
> |---|---|---|
> | `Predictor` × 2 | 2 | **从不被 kill** |
> | `ParameterServer` × 1 | 1 | 训练完仍在（后面还要 `pull`） |
> | `InferenceEndpoint` × 1 | 1 | E.5 创建，仍是 ALIVE |
> | 放置组 2 个 bundle | 2 | **没有 `remove_placement_group` 就一直在** |
> | **合计** | **6 / 6** | **一个 CPU 都不剩** |
>
> 于是 E.7 里的任务**永远排不上队**，`ray.get(..., timeout=60)` 会
> **整整 60 秒后超时**——症状是"明明前面都跑通了，到这里就卡住"。
> 参照 `mini-ray/examples/11_end_to_end.py:275` 的 `remove_placement_group(pg)`，
> 它就在训练循环结束、进入 ④ 在线服务之前。
>
> ⚙️ **可以自己复核这条**（mini-ray 直接可跑，无需真实集群）：
>
> ```python
> ray.init(num_cpus=6)
> print(ray.available_resources()["CPU"])      # 6.0
> pg = placement_group([{"CPU": 1}, {"CPU": 1}], strategy="PACK")
> ray.get(pg.ready())
> print(ray.available_resources()["CPU"])      # 4.0  ← 2 个 CPU 被挖走且不还
> remove_placement_group(pg)
> print(ray.available_resources()["CPU"])      # 6.0  ← 只有显式释放才回来
> ```
>
> ⚠️ 注意释放的**时机**：要在**在那块资源上跑的任务/actor 都结束后**再释放。
> 反过来（先释放、后提交）会让后续任务被调度到公共池，失去放置组"要么全有、要么全无"的保证。

**这一段教什么**：

1. **放置组用来「先占资源再放东西」**：它保证「要么 2 个 worker 都放得下，
   要么都不放」，避免「起了一个、另一个永远等」的启动期卡死；
2. **`ray.get(pg.ready())` 不能省**：不确认就提交任务，你只会看到任务莫名排队；
   **`remove_placement_group(pg)` 同样不能省** —— 放置组是**租约**，不是自动回收的临时对象；
3. **参数服务器是 actor 的经典用法**：状态串行更新（避免并发写坏权重），
   worker 是无状态任务（可以随便重试）；
4. **worker 里嵌套 `ray.get(actor.method.remote())`** 是允许的 ——
   但要清楚代价：worker 会阻塞在等 actor 上，所以**不要形成循环等待**（第 9 章的死锁场景）。

**真实 Ray 写法**（Ray Train）：

```python
from ray.train.torch import TorchTrainer
from ray.train import ScalingConfig, RunConfig   # ⚠️ RunConfig 必须一起 import

trainer = TorchTrainer(
    train_loop_per_worker=train_fn,
    scaling_config=ScalingConfig(
        num_workers=8,
        use_gpu=True,
        placement_strategy="PACK",     # ⚠️ 是 placement_strategy,不是 placement_group_strategy
    ),
    run_config=RunConfig(storage_path="s3://bucket/ckpt", name="run-1"),
)
result = trainer.fit()          # 自动处理:进程编排、DDP、checkpoint、容错
```

> ⚠️ **字段名很容易记错**：`ScalingConfig` 上是 **`placement_strategy`**
> （取值 `"PACK"` / `"SPREAD"` / `"STRICT_PACK"` / `"STRICT_SPREAD"`），
> 底层才是放置组。写 `placement_group_strategy` 会报 `TypeError` ——
> 这个错名流传很广，注意。详见第 13 章 §13.3。

真实训练里，「推给中心 actor」会被 **all-reduce**（NCCL）取代，
但**并行骨架完全一样**：数据分片 → 并行计算 → 同步梯度。
差别只是「同步」那一层从「应用层 RPC」换成了「集合通信库」。

---

## E.5 ④ 在线服务：有状态推理端点

```python
@ray.remote
class InferenceEndpoint:
    def __init__(self, model_version: str):
        self.version = model_version
        self.calls = 0

    def handle(self, request: dict) -> dict:
        self.calls += 1
        return {
            "id": request["id"],
            "score": round(sum(request["features"]) * 0.01, 4),   # 假装的打分逻辑
            "model": self.version,
        }

    def health(self) -> dict:
        return {"status": "ok", "version": self.version, "calls": self.calls}
```

```python
endpoint = InferenceEndpoint.remote("v1")
responses = ray.get([endpoint.handle.remote(req) for req in requests])
print(ray.get(endpoint.health.remote()))
```

**这一段教什么**：

* 一个 actor 天然就是一个「有状态服务」：模型常驻、请求串行（默认并发度 1）。
  要提高并发就 `max_concurrency=N` 或写成 async actor（第 9 章）；
* **健康检查**是生产服务的必备项 —— 在 actor 里暴露一个 `health()` 方法，
  外部探针就能判断副本是否可用。

**真实 Ray 写法**（Ray Serve）：

```python
from ray import serve

@serve.deployment(
    autoscaling_config={"min_replicas": 1, "max_replicas": 8,
                        "target_ongoing_requests": 16},
    ray_actor_options={"num_gpus": 1},
)
class Endpoint:
    def __init__(self, model_path):
        self.model = load_model(model_path)      # 每个副本一份

    async def __call__(self, request):
        payload = await request.json()
        return {"result": self.model(payload)}

serve.run(Endpoint.bind("s3://bucket/model"))
# 之后就有 HTTP 入口、自动扩缩、副本级健康检查、请求路由
```

LLM 场景直接用 `ray.serve.llm.build_openai_app`（第 15 章），
它把 vLLM 引擎、OpenAI 兼容端点、KV-aware 路由都包好了。

---

## E.6 ⑤ 可观测性：看清每一步

```python
from miniray import state
print(state.summarize_tasks())      # 各状态任务数 / 重试数 / 耗时
print(state.summarize_objects())    # 对象数与内存
print(state.summarize_actors())     # actor 状态分布
print(state.list_workers())         # worker 池:pid / 状态 / 已执行任务数

ray.timeline("timeline.json", html="timeline.html")   # ⚠️ html= 是 mini-ray 专有,真实 Ray 没有
```

> ⚠️ **`html=` 这个参数是 mini-ray 自己加的，真实 Ray 的 `ray.timeline()` 没有它。**
> 真实 Ray 的签名是 **`timeline(filename: Optional[str] = None)`**
> （`python/ray/_private/state.py`）—— **只能导出一个 Chrome tracing 格式的 JSON**，
> 没有"顺便生成甘特图 HTML"这一步。写到真实 Ray 上会直接
> `TypeError: timeline() got an unexpected keyword argument 'html'`。
>
> 换到真实 Ray 时对应的做法：
>
> ```python
> ray.timeline("timeline.json")     # 只出 JSON
> # 然后在 Chrome 打开 chrome://tracing,把这个 JSON 拖进去看时间线
> ```
>
> 另外，真实 Ray 的 timeline **需要先开 profiling** ——
> `RAY_PROFILING=1` 启动集群，并设 `RAY_task_events_report_interval_ms=0`
> （`state.py` 的 docstring 原文），否则拿到的是一份空的事件列表。
> mini-ray 不需要这个前提，这也是两者的一个行为差异。

一次真实运行的输出（节选）。**注意：耗时与计数字段每次运行都不同**，
下面这组只是某一次的样本：

```
任务统计: {'total': 23, 'by_state': {'FINISHED': 23}, 'retried': 0,
           'duration_total_s': 2.01, 'duration_max_s': 0.45}
对象统计: {'num_objects': 61, 'used_bytes': 32768, 'capacity_bytes': 268435456,
           'counters': {...}, 'num_reconstructions': 0}
actor 统计: {'total': 6, 'by_state': {'DEAD': 2, 'ALIVE': 4}, 'total_restarts': 0}
worker 池: 8 个进程,总共执行了 23 个任务
```

**这几个数字能读出什么**：

| 观察 | 说明 |
|---|---|
| `23 个任务 / 8 个 worker` | worker 被复用（23 ÷ 8 ≈ 3 个任务/进程） |
| `total_restarts: 0` | 没有 actor 崩溃 |
| `used_bytes` 只有 32KB | 中间结果都是小对象；大数组会显著抬高它 |
| `duration_max_s: 0.45` | 最慢的任务（训练那一步，包含嵌套 actor 调用） |

**真实 Ray 的对应**：`ray.util.state`（同一个 API 形状）+ Dashboard
（可视化，注意默认无鉴权，不要暴露）+ `ray memory`（内存排查）。

---

## E.7 ⑥ 容错演练：让故障真的发生

```python
@ray.remote(max_retries=2)
def flaky_stage(shard, marker_dir):
    """第一次执行时崩溃(模拟被 OOM killer 杀掉),重试后成功。"""
    marker = os.path.join(marker_dir, f"shard-{shard['shard']}.marker")
    if not os.path.exists(marker):
        open(marker, "w").write("crashed")
        os._exit(1)               # 直接干掉 worker 进程
    return f"shard-{shard['shard']}-ok"
```

```python
recovered = ray.get([flaky_stage.remote(s, workdir) for s in feature_batches[:2]], timeout=60)
# → ['shard-0-ok', 'shard-1-ok']   换一个 worker 重跑成功

ref = summarize.remote(feature_batches)    # ← 源码里 ref 是在这里定义的
before = ray.get(ref)
from miniray._private import fault_injection
lost = fault_injection.lose_objects()      # 模拟节点故障:丢掉所有对象
after = ray.get(ref, timeout=60)
print(f"丢光对象后重新取值: {before == after}(值与之前一致)")
```

> ⚠️ **这段是节选**：源码在 `ray.get(ref, timeout=60)` 外面还包了一层
> `try/except`，重建失败时会把「哪个对象没就绪、哪些任务卡住了」逐条打印出来
> （`examples/11_end_to_end.py` 的 ⑥ 段）。这里只保留主干，别把它当成
> 逐字原文 —— 结果一致时源码走的是 `print` 而不是 `assert`。

一次真实运行的输出：

```
崩溃后重试结果: ['shard-0-ok', 'shard-1-ok']
重试过的任务数: 2
丢光对象后重新取值: True(值与之前一致)
重建次数: 26
```

**这一段教什么**：

1. **容错必须被测试**。不注入故障，你只是「相信」它能容错；
2. **lineage 重建是透明的**：`fault_injection.lose_objects()` 会**清空整个对象存储**，
   之后 `ray.get` 依然拿到**一样的值** —— 因为系统重放了生产这些对象的任务
   （沿用原对象 ID，调用方无感）。上面那次运行重建了 **26** 个对象；
3. **不是所有东西都能重建**：`lose_objects()` 返回的是
   `{'lost': [对象 ID…], 'reconstructed': 个数}` —— 注意前者是**列表**、
   后者是**整数计数**（源码 `_private/fault_injection.py` 的 docstring 为准）。
   救不回来的个数 = `len(lost["lost"]) - lost["reconstructed"]`：
   **`ray.put` 的对象和 actor 方法的结果没有血缘**，丢了只会报 `ObjectLostError`。
   这正是「重要数据自己落盘」的理由（第 10 章 §10.4）。

> ⚠️ **关于具体数字**：上面这些计数**每次运行都不一样**
> （取决于当时对象存储里有多少对象、有多少是可重建的）。
> 引用它们时请以你自己那次运行的输出为准 —— 本案例里的数字都标注了来源运行，
> 不要当作固定值。**能确定的是行为，不是数字。**

**真实 Ray 的对应**：杀掉一个 worker pod（`kubectl delete pod`）、
驱逐节点、或者在任务里主动 `os._exit(1)`。机制完全相同。

---

## E.8 从案例到生产：还差什么

这份案例把**编排骨架**跑通了，但离生产还有一段距离。逐项对照：

| 维度 | 案例里 | 生产里需要 |
|---|---|---|
| 数据源 | 生成假数据 | Ray Data + 对象存储（Parquet/Iceberg/Delta） |
| 模型 | 假权重 | Ray Data LLM / vLLM 引擎，GPU + 张量并行 |
| 训练 | 参数服务器 + 手写梯度 | Ray Train（DDP/FSDP + NCCL + checkpoint 恢复） |
| 调参 | 固定超参 | Ray Tune（ASHA 早停 + 贝叶斯搜索） |
| 服务 | actor 当端点 | Ray Serve LLM（OpenAI 兼容 + 自动扩缩 + KV 路由） |
| 容错 | 重试 + lineage | 同上 + 检查点 + 优雅排水 + 多副本 |
| 可观测 | State API + timeline | Prometheus + 集中式日志 + Dashboard + 告警 |
| 部署 | 单机多进程 | KubeRay（RayCluster/RayJob/RayService）+ 镜像固定 digest |
| 安全 | 无 | token 认证、网络隔离、dashboard 不暴露、runtime_env 视同 RCE 面 |

**迁移路径建议**（务实版）：

1. 先用 Ray Core 写出**可运行的骨架**（就像这份案例）——把并行结构和数据流定下来；
2. 把数据加载换成 Ray Data（先不管性能，先跑通）；
3. 把训练换成 Ray Train（拿免费的重启与 checkpoint 恢复）；
4. 把服务换成 Ray Serve（拿免费的路由、扩缩、健康检查）；
5. 最后接监控与 K8s —— 这一步最容易低估工作量，建议预留时间。

每一步都**只换一层**，这样出问题时你能立刻知道是哪一层引入的。

---

## E.9 自己动手

把案例改造成你自己的场景，建议按顺序做：

1. **换数据**：把 `make_shard` 换成读真实文件（本地 CSV 起步），观察任务大小对耗时的影响；
2. **调并行**：把分片数从 4 改成 16，`num_cpus` 从 6 改成 2，看 timeline 里的并行度变化；
3. **加超参搜索**：用 `ActorPool` 或嵌套任务实现一个朴素的网格搜索（真实场景用 Ray Tune）；
4. **加检查点**：让训练循环定期把权重写到磁盘，然后**杀掉进程再启动**，验证能续上；
5. **加背压**：把批量推理的输入改成 1000 条，用 `ray.wait` 控制并发，观察内存曲线；
6. **加监控**：把 `state.summarize_tasks()` 的耗时统计定期打出来，画一条曲线。

---

## E.10 小结

* 这条流水线覆盖了 AI 工程的完整生命周期：
  **数据 → 推理 → 训练 → 服务 → 观测 → 容错**；
* 每个阶段在真实 Ray 里都有一个专门的库，但**底层用的都是同一套 Core 原语**
  （任务、对象、actor、放置组、重试、lineage）；
* 三个最容易踩的坑，**前两个在本案例里真实出现过**：
  **actor 终身占用资源**（要 `ray.kill`）、
  **放置组任务释放顺序**（资源要还回 bundle，不是公共池）；
  第三个（**轮询与上报共用连接导致 async actor 并发失效**）属于
  **异步 actor 与队列**的讨论，**本案例里没有对应代码** ——
  要读它请看**第 9 章 §9.4《异步 Actor：`await` 一切》**（事件循环、
  `await ObjectRef` 的语义、什么会卡住整个 loop）与
  **附录 B §B.9《走读：`util/queue.py` 的四个设计取舍》**
  （队列体为什么必须是 async actor）——
  ⚠️ 本书早先这里指的是**第 12 章**，**那是错的**：
  第 12 章是《Ray Data 与数据管道》，**全文不涉及队列**；已更正；
  ⚠️ 本书更早还把它算进"本案例里都真实出现过"，同样不准确，一并修正；
* 迁移到生产的路径是「**一次只换一层**」，并且**容错必须主动注入故障来验证**。
