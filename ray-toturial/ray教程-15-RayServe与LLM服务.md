仓库地址：https://github.com/hhk-png/cycle-agent

# 第 15 章：Ray Serve 与 LLM 服务

> 本章目标：把 Ray Serve 从「一个 HTTP 框架」讲到「LLM 推理的编排层」。
> 读完你应该能回答：Serve 的核心对象是什么、请求在集群里怎么走、
> 自动扩缩与路由策略怎么选、Ray Serve LLM 走到了哪一步、
> **以及哪些 API 已经死了、哪些还只是 beta**。
> 版本号与 PR 号都标注来源；查不到出处的写「未确认」，不编。

---

## 15.1 为什么 Ray Core 上面还要一层

第 09 章的 actor 池已经能写出「服务」了：`workers[hash(req) % 4].infer.remote(req)`。
它能跑，但离生产可用的在线服务差了一整套东西：

| 缺什么 | 手写方案 | Serve 提供什么 |
|---|---|---|
| 多副本与负载均衡 | 自己写 round-robin / 一致性哈希 | 内置 router，可换策略 |
| 自动扩缩容 | 自己统计 QPS、调 actor 数 | `autoscaling_config` |
| 背压与排队 | 自己实现队列与拒绝策略 | `max_ongoing_requests` + 队列 |
| 零停机升级 | 双倍资源起新版本、切流量 | 版本化部署 + 优雅排空 |
| HTTP / gRPC 接入 | 自己起 Uvicorn、写 protobuf | HTTP Proxy + gRPC Proxy |

**Serve 没有引入新的执行模型**（replica 就是 actor，`handle.remote()` 就是
actor method call），它引入的是**控制面**。

> mini-ray 只实现 Ray Core，**没有 Serve**，本章代码跑不了；最接近的
> `util.ActorPool` + `ActorHandle` 覆盖的是「池化」而不是「服务」。

## 15.2 四个核心对象

```
@serve.deployment        →  Deployment        （类/函数 + 配置）
Deployment.bind(...)     →  Application       （一个有向无环图）
serve.run(app)           →  DeploymentHandle  （可调用的句柄）
handle.remote(...)       →  DeploymentResponse（未来值）
```

```python
from ray import serve

@serve.deployment(num_replicas=2, max_ongoing_requests=16)
class Sentiment:
    def __init__(self, model_path: str):     # 参数全部来自 bind()
        self.model = load(model_path)
    def __call__(self, text: str) -> float:
        return self.model(text)

app = Sentiment.bind("/models/sentiment")    # → Application
handle = serve.run(app, name="sentiment", route_prefix="/sentiment")
print(handle.remote("great movie").result())
```

`DeploymentResponse` 支持链式组合：`b = second.remote(first.remote(x))`，
只在最后 `ray.get(b)` 取一次 —— 这是组合模式的基础。⚠️ **历史遗留坑**：
老教程写的是 `RayServeHandle` / `RayServeSyncHandle`，这两个类**早在 Ray 2.10
就被完全移除**（PR #42526，官方文档原话 "fully removed as of Ray 2.10"）。
今天的同步调用是 `handle.remote(...).result()`，而不是 `.remote()` 直接返回。

句柄的**三种拿法**（这是"获取"的全部途径 —— 第五轮核出**是三个不是两个**）：

1. **`serve.run()` 的返回值** —— 拿到的是 ingress 的 handle；
2. **`serve.get_app_handle("sentiment")`** —— 按 **app 名**取该 app 的 ingress
   handle。这是**公开 API**（`@PublicAPI(stability="alpha")`），
   跨进程/跨代码模块拿句柄时用它最稳；
3. **`serve.get_deployment_handle("Sentiment", app_name="sentiment")`** ——
   按名字直接取某个 **deployment** 的 handle（不必经过 ingress）。

拿到 handle 之后的**两种调用修饰**（它们不是"拿法"，是在改调用行为）：

* 调非 `__call__` 的方法：`handle.options(method_name="embed").remote(x)`；
* 要流式：`handle.options(stream=True).remote(x)` → 得到
  `DeploymentResponseGenerator`。

> ⚠️ 把后两条也算成"拿法"是本书早先的措辞错误 —— 它们改的是**同一个 handle
> 的调用方式**，不产生新 handle。区分这一点在排错时有用：
> `handle.options(...)` 每次返回的都是**新的 handle 对象**，
> 所以 `handle.options(...)` 本身可以安全地复用。

## 15.3 架构：三类 actor + 一个控制面

```
              ┌────────────────────────────────────┐
              │   Serve Controller（单 actor）      │
              │   建/删 replica、扩缩容决策、       │
              │   路由表、版本管理、状态查询         │
              └──────────────────┬─────────────────┘
                                 │ 控制指令
┌──────────────────┐   ┌─────────┴──────┐   ┌────────────────────┐
│ HTTP Proxy       │   │ gRPC Proxy     │   │ Replica (actor)    │
│ Uvicorn 默认:8000│   │ 默认:9000      │   │ max_ongoing_       │
│ proxy_location   │   │ grpc_options   │   │ requests = N       │
└────────┬─────────┘   └────────┬───────┘   └─────────▲──────────┘
         └──── 请求 → 队列 → 选 replica → 发送 ────────┘
```

**Controller**：每个 Serve 实例有且只有一个，全局唯一，负责创建/销毁
replica、跑扩缩容控制循环、维护并广播路由表、承载 `serve.status()`。
**它是单点** —— controller 挂掉会导致 Serve 实例重建。

**HTTP Proxy**：实现是跑在 actor 里的 **Uvicorn**。`proxy_location` 决定数量
与位置：`EveryNode`（每个有 replica 的节点一个）、`HeadOnly`（只在 head 一个）、
`Disabled`（不起，只用 handle 调用）。

⚠️ 两条必须知道的版本差异：

1. 老教程说"默认在 head 上一个 Uvicorn"。这个默认值已经变了 ——
   **2.58 源码里的依据**（`ray/serve/config.py` 第 806 行的 `ProxyLocation`
   枚举 docstring）：*"EveryNode: run a proxy on every node in the cluster that
   has at least one replica actor. **This is the default.**"*。
   同时注意 **`HTTPOptions.location` 这个字段本身已经被弃用**
   （同文件第 862 行的字段说明写着 *"[DEPRECATED: use `proxy_location` field
   instead]"*，且 "This field defaults to None; Serve uses `proxy_location`"）——
   所以"改 `HTTPOptions.location` 的默认值"这个说法已经过时，
   **现在该看的是 `proxy_location`**。**显式写 `proxy_location` 最稳。**
2. Ray 2.55.0 的已知回归：`RAY_SERVE_THROUGHPUT_OPTIMIZED=1` 会自动打开
   direct ingress，而它会**无条件把 `http_options.location` 改写成 `HeadOnly`**，
   用户设的 `EveryNode` 被静默忽略（只有 info 日志，issue #62982）。
   排查「每节点 proxy 怎么没了」先看这个变量。
   ⚠️ **这一条本书未能在 2.58 源码里复现**：`RAY_SERVE_THROUGHPUT_OPTIMIZED`
   确实存在（`ray/serve/_private/constants.py` 第 1063 行），它开启后会改的默认值
   是 `RAY_SERVE_RUN_USER_CODE_IN_SEPARATE_THREAD` /
   `RAY_SERVE_USE_GRPC_BY_DEFAULT` / **`RAY_SERVE_ENABLE_DIRECT_INGRESS`** /
   `RAY_SERVE_FREEZE_GC_ON_STARTUP` 这几个（**确认了"会自动打开 direct
   ingress"这半句**），但本版源码里**看不到**强行改写 `location` 的分支 ——
   可能已在后续版本修掉。这是一条**版本特定的历史回归**，请以 issue #62982 为准。

`proxy_location` 与 `http_options` 是**集群级、运行期不可改**的：修改会直接抛
`RayServeConfigException`（PR #56507），而不是像早期只打 warning。

**gRPC Proxy**：只有配了 `grpc_options.port`（默认 **9000**）**和**
`grpc_servicer_functions` 才启动 —— 后者默认为空列表，**所以默认不启动
gRPC server**，这点常被误解。

**Replica**：就是 actor。并发上限由 `max_ongoing_requests` 控制
（**Ray 2.32.0 起默认值从 100 调整为 5**，PR #45943）。这个值对 CPU 上的
小模型与 GPU 推理服务是完全不同的量级，**不要用默认值上生产**。

## 15.4 两个必会的装饰器：`@serve.ingress` 与 `@serve.batch`

前面几节的例子都用 `async def __call__(self, request)` 手写 HTTP 处理，
那是 Serve 的**底层形态**。真实项目里几乎都用下面这两个装饰器 ——
正文一直没展开，这里补上。（附录 A §A.9 的 Ray Serve 表里
`serve.ingress` / `@serve.batch` / `@serve.multiplexed` / `serve.start` /
`get_*_handle` 都在，可以直接对照。）

### `@serve.ingress`：接 FastAPI，而不是手写 HTTP

`@serve.ingress(app)` 把一个 **FastAPI 应用**挂到 deployment 上，
于是你可以用熟悉的 `@app.get` / `@app.post` 写路由：

```python
from fastapi import FastAPI, Request
from ray import serve

app = FastAPI()

@serve.deployment(num_replicas=2, ray_actor_options={"num_cpus": 0.2})
@serve.ingress(app)                       # ← 关键：把 app 挂上来
class MyDeployment:
    @app.get("/")                          # ← 普通 FastAPI 路由
    async def root(self):
        return {"status": "ok"}

    @app.post("/predict")                  # ← 路径参数、请求体都照常用
    async def predict(self, request: Request):
        body = await request.json()
        return {"result": body["x"] * 2}

serve.run(MyDeployment.bind())
```

**几个要点**：

* **`@serve.ingress` 写在 `@serve.deployment` 下面（更靠内）** ——
  装饰器从下往上应用，顺序反了会报错；
* 在方法里 **`self` 仍然是 deployment 实例**，所以你能直接访问它的状态
  （这正是 Serve 比裸 FastAPI 强的地方：**有状态**）；
* 路径是 **application 级**的：多个 deployment 挂在同一个 FastAPI 上时，
  路由由**根 deployment** 统一分发；
* 不用 FastAPI 也可以用 `request` 对象手写（就是前面几节的形态），
  但**别两套混用**，容易出难查的路由冲突。

### `@serve.batch`：把小请求攒成大 batch

这是 CPU 上跑小模型（embedding、rerank、小分类器）**把吞吐拉起来的第一手段** ——
和 vLLM 在 GPU 上做的 continuous batching 是同一个思路，只是规模小得多：

```python
# ⚠️ 必须显式放大 max_ongoing_requests，否则这个批次永远攒不满 ——
#    2.32 起它的默认值是 5（见 §15.3），一个副本最多 5 个在途请求，
#    而 max_batch_size=16 需要至少 16 个。Serve 侧还要求
#    max_ongoing_requests >= max_batch_size × max_concurrent_batches。
@serve.deployment(max_ongoing_requests=32)
class Embedding:
    @serve.batch(max_batch_size=16, batch_wait_timeout_s=0.05)
    async def embed(self, texts: list[str]) -> list[list[float]]:
        # 注意：这里收到的是一批，返回的必须是一批、且**顺序对应**
        return self.model.encode(texts)     # 一次前向算 16 条

    async def __call__(self, request):
        text = (await request.json())["text"]
        vector = await self.embed(text)     # 单个请求的写法不变
        return {"vector": vector}
```

**参数与语义**（签名来自 `ray/serve/batching.py`）：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `max_batch_size` | **10** | 一批最多攒多少条 |
| `batch_wait_timeout_s` | **0.01** | 最多等多久（等不满也发车） |
| `max_concurrent_batches` | **1** | 同时跑几批 |
| `batch_size_fn` | `None` | 按"元素个数"而不是"请求个数"计量（如变长序列） |

**三个必须记住的约束**：

1. **函数签名必须是"列表进、列表出"**，且**顺序严格对应** ——
   批量函数返回的顺序错位，是这类 bug 里最难查的一种（症状是
   "结果偶尔张冠李戴"，且在低 QPS 下不复现）；
2. 用 **`async def`**。同步的批处理函数会占住事件循环，
   把并发的收益全部抵消；
3. `batch_wait_timeout_s` 是**延迟与吞吐的取舍旋钮**：
   调大 → 批次更满、吞吐更高，但每个请求的**尾延迟变大**。
   0.01 秒是个安全的默认起点，延迟敏感的服务要往下调。

> **什么时候不该用 `@serve.batch`**：GPU 上的大模型推理。
> 那种场景该交给 vLLM 自己的调度器（见 15.11），
> 在 Serve 层再攒一次批反而会干扰 vLLM 的 continuous batching。

## 15.5 请求路径与背压

```
client ──HTTP──► [HTTP Proxy] ① 解析 URL → application → deployment
                              ② 放进该 deployment 的请求队列
                              ③ [Router 选 replica]  满了 → 排队
                                 max_queued_requests 满 → 503 / BackPressureError
                              ④ [Replica actor] 执行，in-flight 计数 -1
```

| 参数 | 层级 | 作用 |
|---|---|---|
| `max_ongoing_requests` | replica 级 | 背压。满了在队列等，而不是继续灌 |
| `max_queued_requests` | 调用方级（实验性） | 默认 `-1` 无限制；超了 handle 抛 `BackPressureError`，HTTP 返回 **503** |
| `target_ongoing_requests` | 扩缩容目标 | **默认 2.0**（PR #45943 把它从 1.0 调到 2.0，与 `max_ongoing_requests` 100→5 是**同一个 PR 的两半**，2.32 起生效；LLM 相关的 PR #59323 里那句 "default of 1.0" 是没跟上改动的过时描述） |

**经验法则**（官方 advanced-autoscaling 文档）：`max_ongoing_requests` 应比
`target_ongoing_requests` 高 **20%–50%**；两者相等会导致**永远不扩容** ——
这是最常见的配置错误。另一条前提：**503 背压只在同时配了
`autoscaling_config` 时才有保证**。监控 `rejected_requests`，正常应为 0。

## 15.6 自动扩缩容

### 声明式

```python
@serve.deployment(
    autoscaling_config={"min_replicas": 1, "max_replicas": 10,
                        "target_ongoing_requests": 8,
                        "upscale_delay_s": 5, "downscale_delay_s": 60,
                        # ⚠️ 别再写 metrics_interval_s —— 2.58 已标 DEPRECATED，
                        #    采样间隔改由 RAY_SERVE_*_METRIC_PUSH_INTERVAL_S 控制
                        "look_back_period_s": 30},
    max_ongoing_requests=12,      # 比 target 高 50%
)
class Model: ...
```

算法是「**采样**在途请求数，用 `look_back_period_s`（默认 30 秒）窗口内的
平均值与 `target_ongoing_requests` 比较，再受 `upscale_delay_s`（默认 30 秒）/
`downscale_delay_s`（默认 600 秒）约束」。采样间隔那个字段
（`metrics_interval_s`）**已经弃用**，别再配它。
它不是 PID，是带滞后的比例控制器 ——
**对尖刺流量反应慢，尖刺要靠 `max_ongoing_requests` 扛。**

### 命令式：自定义策略

能力分两步到位：**2.49.0 引入部署级自定义扩缩容**（PR #55253）；
**2.51.0 增加应用级协调扩缩容**，`AutoscalingContext` 提升为公开 API 并补齐
文档（PR #57535 / #57548 / #57637 / #57756 / #57600）。应用级策略拿到的是
`dict[DeploymentID, AutoscalingContext]`，返回 `(每个部署的目标副本数,
要持久化的策略状态)`，在 `serve.yaml` 里通过 `autoscaling_policy.policy_function`
挂上。

`AutoscalingContext` 暴露 `deployment_id` / `deployment_name` / `app_name` /
`current_num_replicas` / `target_num_replicas` / `running_replicas` /
`total_running_requests` / `total_queued_requests` / `total_num_requests` 与
`async_inference_task_queue_length`。

⚠️ 已知 bug：2.51.0 里应用级策略的 `last_scale_up_time` /
`last_scale_down_time` 恒为 `None`（issue #59001），依赖"上次扩容距今多久"
的策略要自己维护状态。2.51 同时补了指标聚合（min / max / 时间加权平均）与
**策略状态持久化**（PR #59118，修复 issue #59008）—— 策略函数返回的 dict
会在下一轮迭代原样传回，这是实现带记忆控制器的唯一正确入口。

## 15.7 路由策略

默认是**每 router 独立的 Power-of-Two-Choices**（Pow2）：随机选两个 replica，
取负载低的。它在请求互相独立时很好，但两类场景会退化：**供应受限**（多个
router 各自为政、同时选中同一 replica）与**请求间有亲和性**（多轮对话 /
共享 system prompt，打散会让缓存命中归零）。2.56 引入（或补文档）了三个
可选 router：

| Router | 引入 | 解决什么 | 状态 |
|---|---|---|---|
| `ConsistentHashRouter` | 2.56（#62905 / #63096 / #62906） | 会话粘性：同一 key 落同一 replica | 实验性 |
| `CapacityQueueRouter` | 2.56（#62323） | 全局容量令牌，消除 router 间碰撞 | 实验性（追踪 #62399） |
| `RoundRobinRouter` | **2.56 起重新作为可选 router 暴露**（#63238） | 均分，用于对照 / 调试 | 实验性 ⚠️ 注意：轮询是 Serve **切到 Pow2 之前的旧默认实现**，不是 2.56 才发明的 |

> ⚠️ **"引入版本"这一列的来源层级**：本表的 PR 号与版本号**转引自 release notes
> 与社区整理，本轮未回源码逐条核实** —— 能核到的部分是"这些类在 2.58 确实存在、
> 路径如本小节代码块所示"（`ray/serve/experimental/consistent_hash_router.py`
> 与 `ray/serve/experimental/capacity_queue_router.py`），
> **核不到的是"它们是哪一版进来的"**。引用版本号时请自己回查 release notes。

`CapacityQueueRouter` 的机制：起一个中心化的 `CapacityQueue` deployment
actor 记录每个 replica 的在途数，router 转发前**先领一个容量令牌**，拿到
令牌即等于拿到确定位置，因此不会撞车。它带指数退避重试（`max_fault_retries`
默认 3）、令牌 TTL 回收（`token_ttl_s`）、队列不可用时降级回 Pow2，
**全部用 Serve 已有扩展点实现**（`DeploymentActorConfig` + `RequestRouter`）。
⚠️ 但它是**中心化 actor**，本身会成为热点与故障点 —— 官方标为实验性并开
追踪 issue 转正，就是这个原因。

### 怎么把 router 换掉（本节最该给的一行）

上表列了三个 router，但没说**挂在哪里** —— 这才是动手时会卡住的地方。
入口是 **deployment 层的 `request_router_class`**：

```python
from ray.serve.config import RequestRouterConfig
# ✅ 2.58 的实际路径（已核实）：router 类在 experimental 下的**子模块**里，
#    `ray/serve/experimental/__init__.py` 是**空文件**，
#    所以 `from ray.serve.experimental import ConsistentHashRouter` 会 ImportError
from ray.serve.experimental.consistent_hash_router import ConsistentHashRouter

@serve.deployment(
    request_router_config=RequestRouterConfig(
        request_router_class=ConsistentHashRouter,   # 也接受全路径字符串
    )
)
class Chat:
    ...
```

> ⚠️ **三个诚实的提醒**：
> ① 这三个 router 都是**实验性**，导入路径与配置类的**确切位置在版本间变过**
>    —— 请以你所装版本的 `ray.serve` 命名空间为准（`dir(ray.serve)` 先看一眼）。
>    已核实的两个落点：`ray.serve.experimental.consistent_hash_router` 与
>    `ray.serve.experimental.capacity_queue_router`；
>    **`ray.serve.experimental` 本身是空 `__init__`**，别从它直接 import 类名。
>    `RequestRouterConfig` 本身在 **`ray.serve.config`**（已核实），
>    `request_router_class` 可以传类对象，也可以传 `"模块:类名"` 形式的字符串；
> ② **只在有明确理由时换**：默认的 Pow2 在请求互相独立时是最优的，
>    换 hash/capacity 都是为了解决**特定退化场景**（会话粘性 / 供应受限），
>    不是为了"更好"。换错会让尾延迟变差；
> ③ `@serve.deployment(request_router_config=...)` 这个挂载点是官方文档示例里
>    的写法（`doc/source/serve/doc_code/custom_request_router_app.py`），
>    但它属于**实验性接口**，升级前请重新核对签名。

### ⚠️ `DeploymentResponse` 不能塞进容器再传

这一条是扇出（§15.19）时最容易踩的：

```python
# ✗ 不支持:把 DeploymentResponse 放进 list/dict 再传给下游
resp = handle.remote(x)
out = await downstream.remote([resp, resp])       # ← 官方明确不支持

# ✓ 正确:要么逐个 await,要么先转成对象引用
refs = [r._to_object_ref() for r in responses]    # 手动转换
out = await downstream.remote(refs)
```

官方文档把它列为**不支持的用法**，而不是"能跑但不推荐" ——
所以症状往往是**难懂的序列化/反序列化错误**，而不是一句清楚的报错。
记住一句话：**`DeploymentResponse` 只能被 `await` 或被显式转成 ObjectRef，
不能当普通值随手放进数据结构**。

## 15.8 组合模式

```python
@serve.deployment
class Preprocessor:
    def __call__(self, raw: str) -> list[float]: ...

@serve.deployment
class Model:
    def __init__(self, pre: DeploymentHandle):
        self.pre = pre
    async def __call__(self, raw: str):
        features = await self.pre.remote(raw)   # await，不要阻塞事件循环
        return self.infer(features)

app = Model.bind(Preprocessor.bind())
```

`bind()` 的参数是 **bind 之后的 Application**，Serve 解析出依赖图，两个部署
的副本数、资源、扩缩容配置各自独立。多应用用 `serve.run_many([app_a, app_b])`
或 `serve.run(app, name="a")`：多应用是**逻辑隔离** —— 各有各的控制循环与
句柄命名空间（`serve.get_deployment_handle("Model", app_name="a")`），
但共享同一个 Ray 集群与同一个 controller。

## 15.9 升级、状态与健康检查

再次调用 `serve.run(app)` 时，Serve 对比新旧 Application：代码/配置**变了**
就起新版本、等就绪切流量、排空旧版本；完全没变是空操作。判定基于配置与代码
哈希 —— **所以改一个 `max_ongoing_requests` 也会触发滚动升级。**
`serve status` 看 app / deployment / replica 状态，`serve config` 看当前运行
配置（用于 diff）；副本状态里 `HEALTHY` / `UPDATING` / `UNHEALTHY` 的区分能
直接告诉你是"模型加载慢"还是"进程崩了"。

⚠️ **K8s 里的真实坑**：KubeRay 生成的 RayService worker **liveness probe
只检查本地 raylet**（`/api/local_raylet_healthz`），readiness 才额外看
Serve 的 `/-/healthz`。proxy actor 卡住时 liveness 依然通过 → pod 不重启
→ **部署永久卡死**（kuberay issue #4685）。维护者不推荐把 proxy actor 健康
放进 worker liveness，建议自定义探针或上层恢复。

## 15.10 与 Kubernetes 配合

KubeRay 四个 CRD：`RayCluster`（集群）、`RayJob`（一次性任务）、
`RayService`（**Serve 专用**，内置零停机升级与流量切换）、`RayCronJob`
（cron 调度的 RayJob，v1.6 alpha 引入，v1.7 加时区）。`RayService` 两种升级策略：

* `NewCluster`（蓝绿）：新集群就绪后**原子切换**；
* `NewClusterWithIncrementalUpgrade`（增量）：新集群逐步扩容，用
  **Gateway API / HTTPRoute** 按 `stepSizePercent` 每 `intervalSeconds` 迁移
  一部分流量，最后提升新集群、删除旧集群（默认延迟 60 秒）。配置项
  `maxSurgePercent` / `stepSizePercent` / `intervalSeconds` / `gatewayClassName`，
  需打开 `RayServiceIncrementalUpgrade` feature gate。

⚠️ **状态必须标注为不确定**：Anyscale 的 KubeRay v1.7 介绍文章（2026-08-25）
把 "Incremental upgrades with rollback (beta)" 列为 v1.7 特性，Ray 侧文档也有
rollback 支持（PR #65249）；但检索到一条 KubeRay 提交 "Revert '[RayService]
Promote Incremental Upgrade Feature to Beta (#4599)' (#4602)"，把 feature gate
默认值与 pre-release 状态**退回 Alpha**。**请以你实际使用的 KubeRay 版本的
CRD 与 feature gate 默认值为准。**

## 15.11 Ray Serve LLM

### 现代写法

```python
from ray import serve
from ray.serve.llm import LLMConfig, build_openai_app

llm_config = LLMConfig(
    model_loading_config={"model_id": "qwen2.5-7b-instruct",
                          "model_source": "Qwen/Qwen2.5-7B-Instruct"},
    deployment_config={"autoscaling_config": {"min_replicas": 1, "max_replicas": 4}},
    accelerator_type="L4",
    engine_kwargs={"tensor_parallel_size": 1,
                   "gpu_memory_utilization": 0.9, "max_model_len": 8192},
)

llm_app = build_openai_app({"llm_configs": [llm_config]})
serve.run(llm_app, name="qwen", route_prefix="/qwen")
```

起完就是 OpenAI 兼容端点，直接 `POST /qwen/v1/chat/completions`。
`OpenAiIngress` 支持 `/v1/chat/completions`、`/v1/completions`、
`/v1/embeddings`、`/v1/score`、`/v1/models` 以及转写与分词端点。

### `engine_kwargs` 与 `vllm serve` 的对应

`engine_kwargs` 的 key **就是 vLLM 的 engine 参数名**，可以直接拿
`vllm serve` 的 flag 逐个对照：

| `vllm serve` | `engine_kwargs` | 说明 |
|---|---|---|
| `--tensor-parallel-size 8` | `tensor_parallel_size: 8` | 权重切到 8 张卡 |
| `--pipeline-parallel-size 2` | `pipeline_parallel_size: 2` | 层切到 2 段 |
| `--gpu-memory-utilization 0.92` | `gpu_memory_utilization: 0.92` | KV cache 上限 |
| `--max-model-len 16384` | `max_model_len: 16384` | 上下文长度 |
| `--trust-remote-code` | `trust_remote_code: True` | 自定义建模代码 |

⚠️ **不要把 Ray 的分布式参数混进来**：官方文档明确说"不需要手动设置
`VLLM_WORKER_MULTIPROC_METHOD`，Ray 负责进程编排与放置"。TPU 是例外，
会显式写 `distributed_executor_backend="ray"`（PR #65026 的 TPU 示例）。
另一个真实的口径细节：vLLM 要求 `max_num_batched_tokens >= max_model_len`，
否则引擎初始化抛 `ValueError`（PR #65026 评审意见）—— 调小
`max_model_len` 时特别容易撞上。

> ⚠️ **这条有前提，别当成无条件成立**（第六轮核对时补的）：
> 它对应的是 **chunked prefill 关闭**（或更早版本）的路径 ——
> 那时 `max_num_batched_tokens` 会按 `max_model_len` 推导，
> 两者绑定。**开了 chunked prefill 之后，`max_num_batched_tokens`
> 完全可以小于 `max_model_len`**（一个 32K 的 prompt 被切成几块分别调度），
> 这正是第 37 章 §37.7 讲 chunked prefill 的前提。
> 遇到这个 `ValueError` 时，除了调大 batched tokens，
> **也确认一下 `enable_chunked_prefill` 的当前状态**。

### 并行方式

| 并行 | 切什么 | 入口 |
|---|---|---|
| 张量并行 TP | 权重按维度切 | `engine_kwargs.tensor_parallel_size` |
| 流水线并行 PP | 层按段切 | `engine_kwargs.pipeline_parallel_size` |
| 专家并行 EP | MoE 专家分散 | `enable_expert_parallel`（wide-EP，见 Anyscale 博客的 `build_dp_deployment`） |
| 数据并行 DP | 整模型复制多份 | 独立 deployment / `data_parallel_size` |

单副本总卡数 = TP × PP。放置由 placement group 管：默认策略 **`PACK`**，
可用 `placement_group_config` 指定 `bundles` 与 `strategy`；**DP 部署会自动
覆盖为 `STRICT_PACK`**；EKS/KubeRay 的多机 TP 示例用 `STRICT_SPREAD` +
两个 bundle 强制分片落在不同节点。必须知道的边界：**你可以指定 TP/PP 度数，
但"哪个 rank 落到哪张卡"由 vLLM 引擎决定，Ray Serve LLM 的 API 不暴露这个
控制**（官方 cross-node parallelism 文档），"把 rank0 固定在某节点"做不到。

### PD 分离（prefill-decode disaggregation）

prefill 是**算力密集**，decode 是**显存带宽密集**，放同一批 GPU 上互相干扰：
prefill 一来 decode 的 TPOT 就抖。PD 分离把它们拆到两组 GPU，各自用最合适
的配置，中间传输 KV cache。

vLLM 路线：Serve 做编排，告诉 prefill 节点 decode 在哪、把 prefill 产出的
KV 元数据传给 decode；请求以 `max_tokens=1` 发给 prefill 填充 KV cache，
元数据通过 `kv_transfer_params`（NIXL）或请求 ID 编码（MoRIIO）传递，
builder 是 `build_pd_openai_app`。SGLang 路线不同：它有自己的 bootstrap
server，router 同时把请求发给两边，decode 向 prefill 的 bootstrap 端点换
一个 `bootstrap_room` ID 并预留显存页，prefill 用 RDMA 写入
（RFC issue #62953 / #63257）。

**官方博客的数据（2026-06-12）**：Anyscale 的 "Achieving Up to 67% Cost Savings
with Prefill-Decode Disaggregation Using Ray + vLLM on AMD MI325X"
（作者 Kourosh Hakhamaneshi）报告：相同 GPU 预算与 SLA 下，PD 可支撑
**1.3x–2.3x 更多 QPS**，最高 **2.7x goodput**（goodput = 满足 TTFT/TPOT/E2E
SLA 的 QPS），折算**最高 67% 成本下降**；KV 传输用 AMD 的 RIXL。

### PD 的反例（这段比上面的数字更重要）

同一篇博客明确写了：**PD 并不总是有帮助。**

1. **PD 不会让 prefill 更快，反而可能让 TTFT 变差。** 相同 GPU 占用下，
   聚合式 serving 的 TTFT 恒等于或优于 PD；
2. PD 引入额外成本：KV cache 要跨节点传输，P:D 比例要按负载调，
   运维复杂度显著上升。

```
是否同时满足？
  ├─ 输出长度显著大于输入（decode 主导，prefill 干扰才是痛点）
  ├─ 有稳定高并发，P:D 比例可预测
  ├─ 有跨节点高带宽互联（NVLink / RIXL / EFA 之类）
  └─ 团队能承担额外运维复杂度
        ├─ 全部满足 → 做 A/B 压测，用 goodput 而非吞吐下结论
        └─ 有一条不满足 → 先做聚合式 + 前缀缓存 + 更好的路由
```

### 路由的演进：prefix cache → KV + token aware

| 阶段 | 机制 | 路由目标 |
|---|---|---|
| 早期 | 默认 Pow2 | 负载均衡（对缓存无感知） |
| 前缀感知 | `PrefixCacheAffinityRouter` | 同前缀请求打到同一副本，命中 KV 前缀缓存 |
| 2.57 | **实验性** KV-cache-aware routing | 负载之外叠加缓存重叠度 |
| 2.58 | **完成** KV + token-aware routing | 以 **token 负载**为评分目标 |

2.58 的关键变化是**把分词搬到 ingress replica**：`LLMRouter` ingress 负责
分词、做路由决策、把 KV 生命周期事件广播给所有 ingress replica，并让
"选择 + 预留"变成原子操作（PR #64642 / #64920 / #64949 / #65010）；token
走带外通道传给引擎，引擎跳过再次分词。同时支持 KV cache offloading 感知
（CPU 上的 KV 也参与评分），并新增 KV offload/reload 与 SGLang 的 dashboard
（PR #65063 / #65095 / #65122 / #64797）。

**核心概念转折是**：**KV cache 重叠度从"目标"退化成"负载的代理指标"。**
`KVAwareRouter` 真正的评分目标是 token 负载 —— 由"算力受限的剩余 prefill
token 数"（GPU 常驻缓存块给满信用，CPU offload 的块给较少信用）与
"显存受限的 decode token 数"组成。原因是**只追缓存命中会导致请求羊群效应**：
所有请求涌向"缓存最热"的副本，p99 反而变差。架构上 Ray 集成了 NVIDIA
Dynamo 的 KV 感知选择机制。

⚠️ **2.58 的已知限制**（直接决定你能不能上）：`KVAwareRouter`
**只支持 direct streaming**；**一个 application 只能一个模型**；
**不支持 LoRA / multiplex 感知路由**；**不支持数据并行部署**；
**不支持 PD 分离**（后两者在计划中）。调参开关有
`RAY_SERVE_LLM_ENABLE_DECODE_BLOCK_PROGRESS` 与
`RAY_SERVE_INGRESS_ROUTER_REPLICAS_PER_NODE`。

**关于 PR 号**：任务材料提到接口在 **PR #64084**，我检索到的是一组
#64642 / #64920 / #64949 / #65010 与文档 PR #65569。**#64084 未确认**，
本节不引用它。

### SGLang：正在成为一等引擎

路线图在 **ray-project/ray issue #62796**（"[Serve][LLM][SGLang] Ongoing
SGLang + Ray Serve LLM Roadmap"），初始社区支持追踪是已关闭的 **#61114**。

工作流包括采纳 `RayEngine`（PR #62504）、移除 signal handler 覆盖（PR #61914），
以及一批协议对齐项（`shutdown`、`reset_prefix_cache`、`resolve_lora`、
`sleep/wake`、`pause/resume`、`SGLangEngineConfig` 校验、公开 API 再导出、
`setup.py` extras、`MockSGLangEngine`、profiler 开关、`score/rerank` 接线）。
其中不少**依赖 SGLang 上游先改**：多进程 Prometheus opt-out、基于 Ray
reference 的零拷贝权重传输、atexit/signal opt-out、版本化的 disaggregation
元数据契约、专家放置可观测性。已落地的一例：PR #64611 给 `SGLangServer`
加了 `__serve_build_asgi_app__` 支持 direct streaming
（`RAY_SERVE_LLM_ENABLE_DIRECT_STREAMING=1`），在单节点 B200、GLM-5 FP4、TP4 上验证。

> 📌 **一个本节没覆盖、但 2026 年会立刻撞上的负载形状**：上面讲的 PD 分离与
> KV/token-aware 路由，前提都是"**请求是短时、独立的**"—— 一次 chat/completions
> 进来，几个 token 出去。**Agent 负载不是这样**：它是长时程（一次任务几十轮）、
> 有状态（会话要黏在同一个副本上）、而且**大部分时间在等外部工具返回**
> （CPU 侧等待，不是 GPU 侧算力）。
> 这时"编排层（谁放哪张卡）/ 运行时层（扩缩、路由、会话状态）/ 引擎层（vLLM
> 本身的批与 KV 管理）"的分工要**重新问一遍** —— Serve 该负责哪一段、
> 哪一段该交给沙箱与队列，结论和纯推理服务并不一样。
> **完整讨论见第 38 章（Ray 与 Agent 工作负载）**；
> 会话亲和性在 2.58 的真实边界见其 §38.3。

**怎么读**：SGLang 支持是"进行中"而非"已完成"，且以上游改动为前置。
重度依赖 SGLang 的团队把它加进 watch list，而不是指望某个版本号之后就好。

## 15.12 弃用与状态矩阵

已经死掉的 API：

| API | 状态 | 替代 |
|---|---|---|
| `RayServeHandle` / `RayServeSyncHandle` | **2.10 完全移除**（PR #42526） | `DeploymentHandle` + `.remote().result()` |
| `LLMServer.as_deployment()` | 已移除 | `serve.deployment(LLMServer).options(**LLMServer.get_deployment_options(cfg))` |
| `get_serve_options()` | 已改名 | `get_deployment_options()` |

标了 `@Deprecated` 但还能用的：旧写法 `from ray.serve.llm import LLMServer, LLMRouter` 已标 `@Deprecated`，
新路径是 `ray.serve.llm.deployment.LLMServer` 与
`ray.serve.llm.ingress.OpenAiIngress`；`LLMRouter` 现在是 `OpenAiIngress` 的
**别名**（PR #57181）。⚠️ 会浪费半小时的坑：类名是 **`OpenAiIngress`**
（"Ai" 里 i 是小写），但**弃用提示的文案写成了 `OpenAIIngress`**，
照抄会得到 `AttributeError`。

另外 `build_openai_app()` / `build_llm_deployment()` 这些公开 builder
**接口没变**，只改了内部导入（PR #57181 原文 "no changes to its public
interface"），用公开 builder 的代码不需要改。

成熟度矩阵：

| 能力 | 成熟度（截至 2.58） |
|---|---|
| Serve Core（部署/组合/扩缩容/升级） | 稳定 |
| `ray.serve.llm` / `ray.data.llm` 全系列 | **beta** |
| `KVAwareRouter` / `KVRouterActor` | 2.58 完成，但限制多 |
| `ConsistentHashRouter` / `CapacityQueueRouter` | 实验性 |
| PD 分离（vLLM） | 可用，官方博客有完整方法论 |
| PD 分离（SGLang） / SGLang 一等支持 | 进行中（issue #62796） |

**"beta"的含义**：`ray.data.llm` 与 `ray.serve.llm` **截至 2.58 全部仍标
beta**，意味着 API 可能不发大版本就变更 —— 这正是上面那些移除/改名的原因。
上生产可以，但要**锁版本 + 把 LLM 相关导入面收窄到一个模块**。

## 15.13 完整示例一：经典 Serve 部署

```python
# serve_app.py
import numpy as np
from ray import serve
from ray.serve.handle import DeploymentHandle

@serve.deployment(num_replicas=1, max_ongoing_requests=32,
                  ray_actor_options={"num_cpus": 0.5})
class Preprocessor:                     # 无状态纯 CPU，给很小的配额
    def __call__(self, text: str) -> list[float]:
        v = np.frombuffer(text.encode()[:64], dtype=np.uint8).astype(np.float32)
        return (v / 255.0).tolist()

@serve.deployment(
    autoscaling_config={"min_replicas": 1, "max_replicas": 6,
                        "target_ongoing_requests": 8,
                        "upscale_delay_s": 3, "downscale_delay_s": 60},
    max_ongoing_requests=12,            # 比 target 高 50%
    ray_actor_options={"num_cpus": 1},
)
class Model:
    def __init__(self, pre: DeploymentHandle):
        self.pre = pre
    async def __call__(self, text: str) -> dict:
        feats = await self.pre.remote(text)      # await，别阻塞事件循环
        return {"score": float(sum(feats) / (len(feats) + 1e-9)), "dim": len(feats)}

app = Model.bind(Preprocessor.bind())

if __name__ == "__main__":
    print(serve.run(app, name="demo", route_prefix="/demo")
          .remote("hello serve").result())
```

生产上用 `serve build` 生成 `serve.yaml` 再手改（顶层 `proxy_location` /
`http_options` / `grpc_options`，`applications` 里 `import_path` 与每个
deployment 的配置），然后 `serve deploy` 提交。

## 15.14 完整示例二：LLM 服务 + 压测 + 排错清单

```python
# llm_app.py
from ray.serve.llm import LLMConfig, build_openai_app

llm_config = LLMConfig(
    model_loading_config={"model_id": "qwen2.5-7b-instruct",
                          "model_source": "Qwen/Qwen2.5-7B-Instruct"},
    deployment_config={"autoscaling_config": {"min_replicas": 1, "max_replicas": 4}},
    accelerator_type="L4",
    runtime_env={"env_vars": {"VLLM_USE_V1": "1"}},
    engine_kwargs={"tensor_parallel_size": 1, "gpu_memory_utilization": 0.9,
                   "max_model_len": 8192, "max_num_seqs": 64,
                   "enable_prefix_caching": True, "trust_remote_code": True},
)
app = build_openai_app({"llm_configs": [llm_config]})
```

```bash
ray start --head --num-gpus=4 --object-store-memory=$((8*1024**3))
serve run llm_app:app --name llm --route-prefix /llm
```

**压测**：不要用 `curl` 循环。固定并发、固定输入/输出长度、报告
**goodput**（同时满足 TTFT / TPOT / E2E SLA 的 QPS），而不是只报吞吐。
参考 anyscale/ray-serve-llm-perf-examples 的 `prefill_decode/` 与
`pd+kv_offloading/`，它们就是官方博客实验的脚本。压测矩阵至少覆盖
输入 × 输出 ∈ {128, 1k, 4k} × {128, 1k}、并发 ∈ {1, 8, 32, 128}、
KV 命中率 ∈ {低（随机 prompt）, 高（共享 system prompt）}。**只比吞吐是错的**
—— PD 的收益在 goodput 上，代价在 TTFT 上。

**排错清单**

| 症状 | 先查什么 |
|---|---|
| 副本一直 `UPDATING` | 模型下载卡住？`gpu_memory_utilization` 过高？ |
| 大量 503 | `max_ongoing_requests` 太小或 `max_replicas` 不够；看 `rejected_requests` |
| 扩容不触发 | `target_ongoing_requests >= max_ongoing_requests`（最常见的配置错误） |
| 多机 TP 起不来 | NCCL 初始化失败；`pipeline_parallel_size > 1` 时的 PP hang |
| `max_num_batched_tokens < max_model_len` | 引擎初始化 `ValueError`，调大 batched tokens（⚠️ **只在 chunked prefill 关闭时成立**，见 §15.11 的注；开了之后这个组合是合法的） |
| `AttributeError: OpenAIIngress` | 抄了弃用提示的拼写，正确是 `OpenAiIngress` |
| `get_deployment_handle` 找不到 | 多应用场景没传 `app_name` |
| 每节点 proxy 不见了 | 检查 `RAY_SERVE_THROUGHPUT_OPTIMIZED`（2.55 已知回归） |
| pod 永不重启但服务死了 | worker liveness 只看 raylet（kuberay #4685），补自定义探针 |

## 15.15 `serve.start()` / `serve.shutdown()`：不经过 HTTP 的用法

前面所有例子都走 `serve.run(app)` + HTTP。有两类场景不需要 HTTP：**在别的
Python 程序里调用 Serve**（训练脚本里嵌一个推理服务、批处理里调模型），以及
**先建一个常驻实例、再逐个挂应用**。

```python
from ray import serve

serve.start()                                # 起一个空的 Serve 实例（不含应用）
handle = serve.run(app, name="demo",         # 之后可以按需挂应用
                   route_prefix="/demo")
print(handle.remote("hi").result())

serve.shutdown()                             # 关掉整个 Serve 实例
```

| API | 作用 | 什么时候用 |
|---|---|---|
| `serve.start(...)` | 启动 Serve 实例（controller + proxy），**不部署任何应用** | 想先建实例再逐个 `serve.run()`；或只用 handle 调用 |
| `serve.run(app, name=, route_prefix=)` | 部署 / 更新一个应用，返回 `DeploymentHandle` | 常规入口 |
| `serve.shutdown()` | 关停 Serve 实例，**所有应用与副本一起销毁** | 进程退出前清理、测试 teardown |
| `serve.status()` | 返回 app / deployment / replica 的状态对象 | 脚本化巡检（§15.9 的 `serve status` 是它的 CLI 包装） |

**要点与坑**：

* **`serve.run()` 会确保实例已启动**，所以只用 `serve.run()` 时不需要显式
  `serve.start()` —— 这正是它成为推荐入口的原因。反过来，`serve.start()` 之后
  没挂应用时实例是**空转**的（controller 与 proxy 都在占资源）。
* **测试里一定用 `try/finally: serve.shutdown()`**。否则上一个用例的应用会留在
  实例里，下一个用例的 `serve.run()` 变成"更新已有应用"而不是新建，断言会莫名
  其妙地互相污染。
* **`serve.shutdown()` 是整实例级别的**：多应用场景删单个应用请用
  `serve.delete(name)`（见 §15.8 的多应用说明）。
* ⚠️ 这是**进程内**的 Python API。driver 进程退出后实例是否存活取决于启动方式
  （**未确认** detached 的具体行为）。生产环境请用 `serve run` / `serve deploy`
  这类 CLI 入口，不要靠 `serve.start()` 撑着。

## 15.16 模型多路复用：一套副本服务很多模型

**问题**：你有 500 个 LoRA adapter（或按租户微调的小模型），每个都要服务，
但每个 QPS 都很低。一个模型一个 deployment = 500 个常驻副本，绝大部分在空转。

**Model Multiplexing** 让**同一个 replica 按需装多个模型**，请求里带上"这次要
哪个模型"，由 Serve 负责把请求路由到**已经装了这个模型**的副本上：

```python
import torch
from ray import serve

@serve.deployment(max_ongoing_requests=16)
class ModelInferencer:
    @serve.multiplexed(max_num_models_per_replica=3)   # 一个副本最多装 3 个
    async def get_model(self, model_id: str):
        # 只在"这个副本还没有这个模型"时被调用 —— 加载完会被缓存
        return torch.load(f"/models/{model_id}/model.pt")

    async def __call__(self, request):
        model_id = serve.get_multiplexed_model_id()    # 从请求头里取模型 id
        model = await self.get_model(model_id)
        return model.forward(torch.rand(64, 3, 512, 512))


app = ModelInferencer.bind()
serve.run(app, name="multiplex", route_prefix="/mux")
```

怎么指定要哪个模型：

| 调用方 | 写法 |
|---|---|
| HTTP | 请求头 `serve_multiplexed_model_id: <id>` |
| `DeploymentHandle` | `handle.options(multiplexed_model_id="<id>").remote(...)` |
| 上游 deployment | `await self.next_h.options(multiplexed_model_id="<id>").remote(...)` |

| 机制 | 行为 |
|---|---|
| 路由 | router 按模型 id 选副本，**已经装了这个模型的副本优先** |
| 装不下时 | 所有装了这个模型的副本都过载 → 路由到一个**新副本**，由它去加载 |
| 缓存淘汰 | 超过 `max_num_models_per_replica` 时按 **LRU 淘汰**；被淘汰的模型会调它的 `__del__`（要清理就实现它） |
| 请求头缺失 | 当成普通请求、**随机选一个副本** —— 不报错，这是最容易静默出错的地方 |
| 扩缩容 | 仍然是**副本级**的，没有"按模型扩缩容"；指标按副本统计 |

> ⚠️ **两点警告**：
> 1. 模型加载发生在**请求路径上**：第一次请求某个模型时会等加载完。要避免首请求
>    抖动，只能靠预热请求把常用模型"摸"一遍，或者让上游把冷启动摊开。
> 2. 官方把 Model Multiplexing 标为**实验性**（experimental），API 可能变。另外
>    §15.11 提到 2.58 的 `KVAwareRouter` **不支持 multiplex 感知路由** ——
>    走 LLM 路由时这个特性用不上。

## 15.17 `autoscaling_config` 全字段与 scale-to-zero

§15.6 给了声明式写法和算法直觉，这里把字段表补全。

| 字段 | 含义 | 备注 |
|---|---|---|
| `min_replicas` | 副本数下限 | **设为 0 才能启用 scale-to-zero** |
| `max_replicas` | 副本数上限 | 手工配 autoscaling 时基础默认为 **1**；用 `num_replicas="auto"` 时的默认配置里 `max_replicas` 是 **100** |
| `initial_replicas` | 初始副本数 | 不设则从 `min_replicas` 起步；冷启动敏感的服务应设大一点 |
| `target_ongoing_requests` | 扩缩容目标：每副本在途请求数 | 与 §15.5 的 `max_ongoing_requests` 是**两件事**；**默认 2**（见 §15.5） |
| `upscaling_factor` | 单次扩容的倍数上限 | 越大越激进。**默认已核实为 `None`**（= 不限倍数） |
| `downscaling_factor` | 单次缩容的倍数上限 | 越大越激进。**默认已核实为 `None`** |
| `upscale_delay_s` | 扩容前必须持续超标的时长 | 防抖。**默认已核实为 `30.0` 秒** |
| `downscale_delay_s` | 缩容前必须持续低载的时长 | 防抖。**默认已核实为 `600.0` 秒**（10 分钟） |
| `downscale_to_zero_delay_s` | **1 → 0** 这一步单独等多久 | 见下。**默认 `None`** = 沿用 `downscale_delay_s` |
| `aggregation_function` | 窗口内指标的聚合方式 | `"mean"` / `"max"` / `"min"`，默认 `mean`；**只在聚合模式下生效**（见下） |
| `look_back_period_s` | 回看窗口 | **默认已核实为 `30.0` 秒**，决定控制回路的时间尺度 |
| `metrics_interval_s` | 采样间隔 | ⚠️ **已弃用**（字段说明写着 *"[DEPRECATED] How often to scrape for metrics. Will be replaced by the environment variables `RAY_SERVE_REPLICA_AUTOSCALING_METRIC_PUSH_INTERVAL_S` 和 `RAY_SERVE_HANDLE_AUTOSCALING_METRIC_PUSH_INTERVAL_S`"*）—— 本书 §15.6 的例子里还在传它，能跑但会走旧路径 |
| `policy` | 部署级自定义扩缩容策略 | `AutoscalingPolicy(policy_function=..., kwargs=...)`，默认是内置的请求驱动策略；见 §15.6 的命令式写法 |

> 上表全部对照 2.58 的 `ray/serve/config.py`（`class AutoscalingConfig`，
> 第 599 行起）逐字段核对过默认值。另外该 class 里还留着三个**明确标了
> DEPRECATED** 的字段（`smoothing_factor`、`upscale_smoothing_factor`、
> `downscale_smoothing_factor`），新代码不要用。

> ⚠️ **`max_replicas_per_node` 不在上面这张表里** —— 它不是
> `AutoscalingConfig` 的字段，而是 **`@serve.deployment(...)` 的顶层参数**，
> 所以放在 §15.18 讲副本放置的地方（本书早先把它混在这里，已更正）。
> 2.58 的 `AutoscalingConfig` 字段就是上表这些（`ray/serve/config.py`）。

### `downscale_to_zero_delay_s`：省钱的最后一步

缩容是**两段式**的：

```
当前副本数 ──downscale_delay_s──► 1 个副本 ──downscale_to_zero_delay_s──► 0 个副本
                                   （前提：min_replicas = 0）
```

* 不设 `downscale_to_zero_delay_s` 时，**1 → 0 这一段沿用 `downscale_delay_s`**。
* 它的价值是给你一个"更保守的最后一步"。文档给的典型用法是
  `downscale_delay_s=300`（5 分钟没流量就把副本收到 1 个）
  \+ `downscale_to_zero_delay_s=1800`（再等 30 分钟确实没流量才收到 0）。
* **代价是真实的**：缩到 0 之后**第一个请求要吃完整冷启动**（拉镜像、加载权重、
  建 CUDA context）。对 GPU 大模型这可能是几十秒到几分钟。所以**延迟敏感或模型
  很大的服务，宁可 `min_replicas=1` 常驻**，也不要用 scale-to-zero 省那点钱；
  只有"长时间完全空闲 + 偶尔来一发"的流量形状才值得开。

### `aggregation_function` 的生效条件

`aggregation_function` 决定窗口内的指标怎么合成一个数（`mean` = 时间加权平均、
对波动平滑；`max` = 对峰值敏感；`min` = 更保守）。⚠️ **未确认**：本次检索到的
说法是它**只在聚合模式下生效** —— 需要打开 `RAY_SERVE_AGGREGATE_METRICS_AT_CONTROLLER=1`
（实验性）；默认模式下控制器拿到的已经是各 router 预先算好的平均值之和，
你设的 `aggregation_function` 不生效。遇到"我设了 `max` 为什么还是被平均掉"，
先去查这个开关（以你所装版本的官方文档为准）。

## 15.18 `ray_actor_options` 与副本放置

`ray_actor_options` 里的每个字段最终都变成**创建 replica actor 时传给 Ray Core 的
资源声明**（就是第 08 章的调度模型）。它决定副本能不能被放下、会被放在哪。

| 字段 | 作用 | 与放置的关系 |
|---|---|---|
| `num_cpus` | 该副本占用的逻辑 CPU 数，**可以是小数**（如 0.2） | 节点剩余 CPU 不足 → replica 卡在 PENDING，**不报错** |
| `num_gpus` | 逻辑 GPU 数，同样支持小数 | 决定一个节点最多放几个副本；`num_gpus=1` + 4 卡节点 = 最多 4 副本 |
| `memory` | 堆内存声明（字节） | 是**声明值、不是限制值**：声明少了不会被杀，声明多了会"有空闲却放不下"。按实测 RSS 的 1.5 倍给 |
| `resources` | **自定义资源**（`{"tpu": 1, "model_server": 1}` 之类） | 配合 `ray start --resources` 做异构机型定向调度 |
| `runtime_env` | 该副本的运行时环境（pip / env_vars / working_dir） | 每个副本各自建环境，**变更会触发副本重建** |
| `accelerator_type` | 指定加速器型号（如 `"L4"` / `"A100"`） | 集群有混合卡时用它把副本钉在指定机型上 |

```python
@serve.deployment(
    num_replicas=2,
    ray_actor_options={
        "num_cpus": 0.5,                      # 小模型：别按整核配
        "num_gpus": 1,
        "accelerator_type": "L4",             # A100 与 L4 混布时必写
        "resources": {"model_server": 1},     # 只落在标了这个自定义资源的节点上
        "runtime_env": {"env_vars": {"VLLM_USE_V1": "1"}},
    },
)
class Model: ...
```

**三条实践经验**：

1. **CPU 小模型要压 `num_cpus`**。默认一个副本吃满一个逻辑核，节点上能放的副本
   数被硬生生限制住，吞吐上不去。0.1–0.5 是常见起点（配合 §15.4 的
   `@serve.batch` 效果最好）。
2. **`num_gpus` 决定"每节点几个副本"**。`num_gpus=0.5` 表示两个副本共享一张卡 ——
   只有模型确实很小、能塞进半张卡时才这么写，否则就是显存 OOM。
3. **`accelerator_type` 是混合机型集群的救命字段**。不写时调度器可能把需要 L4 的
   副本放到只有 A100 的节点上，或者反过来浪费贵卡。

### `max_replicas_per_node`：一个**不在** `ray_actor_options` 里的放置旋钮

```python
@serve.deployment(
    num_replicas=8,
    max_replicas_per_node=2,      # 每个节点最多 2 个副本
    ray_actor_options={"num_gpus": 1},
)
class Model: ...
```

* **它是 `@serve.deployment(...)` 的顶层参数**，与 `autoscaling_config` 平级 ——
  **不是** `AutoscalingConfig` 的字段（§15.17 早先把它列进那张表，已更正）。
  作用就是"小模型别全挤在一个节点上"，让副本散开。
* ⚠️ **它不能和 `placement_group_bundles` 同时用**：两个一起传会直接抛
  `ValueError`（`ray/serve/_private/config.py` 里显式校验）。要么让 Serve
  按资源自己散副本，要么用 placement group 显式指定布局 —— 二选一。
* 默认值以官方文档为准。

## 15.19 句柄进阶：取句柄、扇出与流式

§15.2 提过句柄的**三种**拿法，这里补正文一直没展开的两件事：**多应用场景里怎么取
句柄**，以及**在一个副本内部怎么同时打多个下游**。

### 取句柄：`get_app_handle` / `get_deployment_handle`

```python
from ray import serve

# 应用级句柄：拿应用入口（根 deployment）的句柄
app_handle = serve.get_app_handle("sentiment")

# deployment 级句柄：任意一个 deployment，多应用时必须给 app_name
d_handle = serve.get_deployment_handle("Sentiment", app_name="sentiment")

vec = d_handle.options(method_name="embed").remote("hi")   # 调非 __call__ 方法
gen = d_handle.options(stream=True).remote("hi")           # → DeploymentResponseGenerator
```

| API | 返回 | 什么时候用 |
|---|---|---|
| `serve.get_app_handle(name)` | 应用入口的 `DeploymentHandle` | 从"当前应用之外"调另一个应用 |
| `serve.get_deployment_handle(name, app_name=...)` | 指定 deployment 的句柄 | 调非入口 deployment、写运维脚本 |
| `serve.run(app)` 的返回值 | 根 deployment 的句柄 | 单应用、同进程内的常规用法 |

> ⚠️ **多应用场景里 `app_name` 必须给**。不给时 Serve 只在"当前应用"里找，找不到
> 就抛异常 —— 这就是 §15.14 排错表里"`get_deployment_handle` 找不到"那一行的原因。

### 扇出：`DeploymentResponse` 与 `asyncio.gather`

`DeploymentResponse` 是**未来值**，可以组合、可以并发等待。在一个副本内部要同时
调多个下游时，**先把所有 `remote()` 发出去，最后统一 `await`**：

```python
import asyncio
from ray import serve
from ray.serve.handle import DeploymentHandle, DeploymentResponse

@serve.deployment
class Ensemble:
    def __init__(self, members: list[DeploymentHandle]):
        self.members = members

    async def __call__(self, text: str) -> dict:
        # ① 全部发出去：remote() 不阻塞，立刻返回 DeploymentResponse
        responses: list[DeploymentResponse] = [m.remote(text) for m in self.members]
        # ② 一起等：await DeploymentResponse 直接拿到结果，不需要 .result()
        results = await asyncio.gather(*responses)
        return {"votes": results, "n": len(results)}
```

**三条语义要点**：

1. **`handle.remote()` 不阻塞**：它把请求送出去并立刻返回 `DeploymentResponse`，
   真正的等待发生在 `await` / `.result()`。所以"先全部 `remote()` 完再统一等"
   才是并发；写成"每个都 `.result()` 再发下一个"就是串行。
2. **`await` 与 `.result()` 的区别**：同步代码用 `.result()`（阻塞）；**在
   `async def` 里一律用 `await`** —— `.result()` 会阻塞整个副本的事件循环，把
   并发能力清零。这是 Serve 里最常见的性能事故。
3. **`DeploymentResponseGenerator`**：`handle.options(stream=True).remote(...)`
   返回它，用 `async for chunk in gen:` 逐块消费。它**只能消费一次**，且同样优先
   用 `async for`；`.result()` 拿到的是整个 generator，那就失去流式的意义了。

> **扇出 vs 组合**：`second.remote(first.remote(x))`（§15.2）是**串联**，
> `asyncio.gather` 是**并联**。真实服务里两者混用 —— 并联的关键是把 `remote()`
> 先全部发出去，中间不要插入任何 `.result()`。

## 15.20 Gradio / Streamlit 集成

Serve 能在同一套副本、扩缩容、路由基础设施上托管交互式 UI —— 对 demo、内部工具、
人工评测界面特别有用。**Gradio** 可以被挂到 Serve 上：

```python
import gradio as gr
from fastapi import FastAPI
from ray import serve

gr_app = gr.Interface(fn=lambda x: x.upper(), inputs="text", outputs="text")

api = FastAPI()
gr.mount_gradio_app(api, gr_app, path="/ui")     # Gradio 官方的 FastAPI 挂载方式

@serve.deployment(num_replicas=2)
@serve.ingress(api)                              # Serve 挂 FastAPI —— §15.4 的主路径
class Demo:
    pass

serve.run(Demo.bind(), route_prefix="/demo", name="ui-demo")
```

**要点**：

* `@serve.ingress` 接受的是**实现了 ASGI 接口的应用对象**，所以 FastAPI 之外，
  Gradio 这类 ASGI 应用理论上都能挂；但**最稳的组合是"Gradio 挂到 FastAPI 上、
  Serve 挂 FastAPI"**（两步各自的官方支持都很明确，中间的胶水层最少）。
* 需要装对应依赖：Gradio 的 extra 名字在不同版本**未确认**（一种说法是
  `ray[serve-gradio]`）。不确定时直接 `pip install gradio`，并把版本锁进
  `runtime_env` 里 —— demo 类应用最怕的就是依赖漂移。
* **Streamlit 没有找到官方的一等集成**（**未确认**）。它的执行模型是"独立的
  server 进程 + 脚本反复重跑"，和 Serve 的副本模型并不吻合。务实做法：
  Streamlit 作为**前端独立部署**，通过 HTTP 调 Serve 的端点 —— 这样前端的重启
  不会影响推理副本，两者也能各自扩缩容。
* **别把 UI 的会话状态放在 deployment 实例上**。副本会扩缩容、会被重启，
  `self.history` 会莫名其妙消失。会话状态请放外部存储，或配合 §15.7 的
  `ConsistentHashRouter` 做会话粘性。

## 15.21 本章小结

* API 面只有四个对象：`@serve.deployment` → `.bind()` → `serve.run()` →
  `DeploymentHandle`；**`RayServeHandle` 系列 2.10 就删了**。
* 实际写服务时几乎不会手写 `__call__(request)`，而是用两个装饰器：
  **`@serve.ingress(app)`** 接 FastAPI 路由、
  **`@serve.batch(max_batch_size=, batch_wait_timeout_s=)`** 把小请求攒批
  （CPU 小模型的吞吐第一手段；GPU 大模型别用，交给 vLLM 自己批）。
* 控制面是单 actor 的 Controller，数据面是 proxy（HTTP Uvicorn / gRPC 9000）
  加 replica actor；`proxy_location` 与 `grpc_options` 集群级、运行期不可改，
  且**默认值在版本间变过，务必显式写**。
* 背压靠 `max_ongoing_requests`（2.32 起默认 5），扩缩容目标是
  `target_ongoing_requests`（**2.32 起默认 2.0** —— 与前者是 PR #45943
  的同一个改动，见 §15.5），**前者必须显著大于后者**。
  路由从 Pow2 出发，2.56 补了 `ConsistentHashRouter` 与
  `CapacityQueueRouter`，两者都还是实验性。
* Ray Serve LLM 的现代写法是 `LLMConfig` + `build_openai_app`；顶层
  `LLMServer` / `LLMRouter` 已 `@Deprecated`；**`ray.serve.llm` 全系列
  截至 2.58 仍是 beta**。
* 2.58 完成的 KV + token-aware 路由，核心是把评分目标从"缓存重叠"改成
  "token 负载"以避开请求羊群；但它只支持 direct streaming、单模型、
  无 DP、无 PD。SGLang 正走向一等引擎（issue #62796），按"进行中"看待。
* PD 分离有真实收益（MI325X 上最高 2.7x goodput / 最高 67% 成本下降），
  **但同一篇官方博客也写了 PD 可能让 TTFT 变差** —— 它是条件性优化。
* **`serve.run()` 会按需启动实例**，所以要单独用 `serve.start()` / `serve.shutdown()`
  的场景只有"进程内不经过 HTTP 调用"与"先建实例再逐个挂应用"；测试里务必
  `try/finally: serve.shutdown()`。
* **模型多路复用**（`@serve.multiplexed` + `serve.get_multiplexed_model_id()`）让
  一个副本按需装多个模型，靠 `serve_multiplexed_model_id` 请求头 /
  `handle.options(multiplexed_model_id=...)` 路由，超限按 **LRU 淘汰**；
  它仍是**实验性**，且请求头缺失时会**静默**路由到随机副本。
* `autoscaling_config` 的完整旋钮里有两个容易忽略的：**`downscale_to_zero_delay_s`**
  （`min_replicas=0` 时 1→0 单独等的时长，省钱但首个请求吃完整冷启动）与
  **`aggregation_function`**（默认 `mean`，是否生效取决于聚合模式开关）。
  ⚠️ 同时注意 **`metrics_interval_s` 已标 DEPRECATED**（采样间隔改由
  `RAY_SERVE_*_METRIC_PUSH_INTERVAL_S` 控制），别再往 `autoscaling_config` 里写它。
  `ray_actor_options` 的 `num_cpus` / `num_gpus`（支持小数）直接决定**每节点能放
  几个副本**，`accelerator_type` 是混合机型集群的救命字段。
* **路由的换法**：入口是 `@serve.deployment(request_router_config=
  RequestRouterConfig(request_router_class=...))`，而 router 类在
  **`ray.serve.experimental` 的子模块**里（`ray.serve.experimental` 本身是空
  `__init__`，从它直接 import 类名会 `ImportError`）。三个 router 都是实验性，
  引入版本号转引自 release notes、**本轮未回源码核实**（见 §15.7 的说明）。
* **Agent 负载要另算一笔账**：本节的 PD 分离与 KV/token-aware 路由都假设
  "请求短时、互相独立"。长时程、有状态、大部分时间在等工具的 Agent 负载，
  需要把"编排层 / 运行时层 / 引擎层"的分工重新问一遍 —— **见第 38 章**。
* **副本内部扇出下游要用 `asyncio.gather` + 先发后等**，`async def` 里一律
  `await` 而不是 `.result()`（后者会阻塞整个副本的事件循环）。
  `serve.get_app_handle` / `get_deployment_handle` 在多应用场景**必须给
  `app_name`**。

下一章进入强化学习：RLlib 的新 API stack 已全面默认，而它的生态位正在被
verl 这类后训练框架挤压 —— 这两件事要放在一起看才看得懂。
