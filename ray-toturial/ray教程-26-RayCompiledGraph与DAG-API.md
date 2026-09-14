仓库地址：https://github.com/hhk-png/cycle-agent

# 第 26 章：Ray Compiled Graph 与 DAG API

> 本章目标：把第 3 章 §3.8 里那半页纸的 Compiled Graph 展开成一章。
> 读完你应该能：用 `InputNode` / `MultiOutputNode` / `bind()` 手写一张 DAG 并执行它；
> 判断什么负载值得 `experimental_compile()`；说清它为什么能绕开对象存储；
> 在通道超时、图没绑定、actor 复用踩坑时知道去哪儿找原因；
> 并且知道**它现在到底算不算"能用"**。

---

## 26.1 为什么需要 Compiled Graph

### 先算一笔账：Ray 的一次普通任务调用有多贵

第 3 章 §3.2 那张 16 步时序图讲的是「一次 `f.remote()` 走了哪些进程」。
现在换个角度问：**那些步骤加起来，是多少微秒？**

官方文档给的量级是：**经典 Ray Core API 单次调用的系统开销约 1ms**。

这 1ms 不是为了传输数据花的，绝大部分是**控制面**：

| 开销项 | 大致构成 | 为什么省不掉 |
|---|---|---|
| 参数序列化 | pickle + ObjectRef 占位 | 参数必须能跨进程 |
| **提交 RPC** | driver → 本地 raylet 一次往返 | 这是「登记任务」 |
| 依赖解析 | raylet 查对象目录、判断依赖是否就绪 | 得知道能不能跑 |
| **调度决策** | 选节点、选 worker，可能触发拉起新 worker | 见第 8 章 §8.3、§8.4 |
| **派发消息** | raylet → worker 一次往返 | 任务体得送过去 |
| 取依赖 | worker 从 plasma 读（同节点零拷贝） | 见第 3 章 §3.6 |
| **结果落盘** | worker 写 plasma + 上报 raylet | 见第 3 章 §3.4 |
| **唤醒下游** | raylet 更新对象目录、通知等待者 | 见第 3 章 §3.2 第 ⑬ 步 |

> ⚠️ **关键点：这 1ms 是「每个任务」的，不是「每个作业」的。**
> 它跟你任务里算了多少东西**完全无关**。一个 `return x * 2` 的任务和一个
> 训了 10 分钟的 PyTorch step，付的是同一笔控制开销。

### 什么时候这笔账会失控

只要**单任务的计算时间 ≲ 控制开销**，你的 CPU 就主要花在调度上，而不是计算上：

```
单任务计算耗时            控制开销占比             结论
─────────────            ──────────              ────
100 ms   （批处理）        ~1%                  完全无所谓
10 ms    （小批推理）      ~10%                 还行
1 ms     （小算子）        ~50%                 开始难受
50 µs    （单层 attn）     ~95%                 ❌ 不可接受
```

三类负载会直接撞上这堵墙：

1. **多 GPU 张量并行推理**。一层 attention 在单卡上可能只算几十微秒，
   但层与层之间要跨卡 all-reduce。如果每层都走一次「提交 → 对象存储 → ray.get」，
   通信开销会比计算本身贵一个数量级 —— 这是本章最重要的一类负载。
2. **RL 的 rollout / 环境步进**（第 16 章）。每次环境 step 是毫秒级甚至微秒级，
   而 agent 与 env worker 之间来回频繁。
3. **小而多的流水线**。比如数据预处理里「归一化 → 分词 → 编码」三个小步骤。

第 5 章 §5.6（提交模式的反模式清单）已经给过实践建议：
**任务本身至少应该跑几毫秒**。
Compiled Graph 就是给「做不到这一点」的负载准备的逃生舱。

### 核心思路：把图编译一次，然后当「一个分布式单元」执行

普通 Ray 的执行模型是**逐任务、急切（eager）**的：每次 `.remote()` 都是
一次独立的「提交 → 调度 → 派发」。

Compiled Graph 换了模型：

```
普通 Ray(急切)                     Compiled Graph(静态)
────────────                      ────────────────────
每次调用都付一遍:                   编译一次:
  序列化 → 提交 RPC → raylet         ① 遍历整张 DAG 并校验
  查依赖 / 调度 / 选 worker          ② 给每条边建一条「通道」(channel)
  派发 → worker 写 plasma            ③ 预建 NCCL 通信组 / 预分配资源
  raylet 通知下游                    ④ 排好 READ/WRITE/COMPUTE 执行序

每个任务 ~1ms                      之后每次 execute: 只推输入、收输出
                                   → < 50µs
```

> 官方文档的表述是：Compiled Graph 提供「一个经典 Ray Core 风格的 API」，
> 但对**反复执行同一张图**的负载，系统开销**低于 50µs**；
> 而经典 API 每次任务启动的开销是 **~1ms**（Anyscale 博客
> *Announcing Compiled Graphs* 的说法是 1–2ms 对约 50µs）。

一句话：**把「每任务一次的控制面往返」摊薄成「每张图一次」**，
图内部的数据流动直接走预先建好的通道。

---

## 26.2 DAG API 基础

DAG API 是 Compiled Graph 的地基 —— 你**先显式描述一张图**，
然后才谈得上编译它。所以这一节讲怎么把图写出来。

### 三个新东西

| 概念 | 写法 | 对应普通 Ray 里的什么 |
|---|---|---|
| **`InputNode`** | `with InputNode() as inp:` | 图里的**占位输入**，运行时才填值 |
| **`.bind()`** | `w.recv.bind(inp)` | 取代 `.remote()`：**不执行，只描述边** |
| **`MultiOutputNode`** | `MultiOutputNode([a, b])` | 图有多个出口时的**汇合点** |

> ⚠️ **`InputNode` 与 `MultiOutputNode` 都在 `ray.dag` 下**：
> `from ray.dag import InputNode, MultiOutputNode`。
> 但注意 **`.bind()` 不是新东西** —— **自 Ray 2.0 的 DAG API 就有**，
> 而且它**只有一种语义**：返回 DAG 节点（`FunctionNode` / `ClassMethodNode`），
> **不能 `.remote()`**。DAG API 做的只是把这个已有的机制**收集成一张图**。
> 常见的混淆是把 mini-ray 的 `.bind()`（参数部分套用、返回 `RemoteFunction`）
> 当成了真实 Ray 的行为 —— **那是 mini-ray 的分叉，不是 Ray 的**（见 §26.9）。

### 一个走通的例子：三阶段流水线

```python
import ray
from ray.dag import InputNode, MultiOutputNode

ray.init()


@ray.remote
class Preprocess:
    def run(self, x):
        return x * 2


@ray.remote
class Model:
    def forward(self, x):
        return x + 100


@ray.remote
class Postprocess:
    def run(self, x, scale):
        return x * scale


pre = Preprocess.remote()
model = Model.remote()
post = Postprocess.remote()

with InputNode() as inp:
    a = pre.run.bind(inp)          # ① 输入 → 预处理
    b = model.forward.bind(a)      # ② 预处理 → 模型
    c = post.run.bind(b, inp)      # ③ 模型 → 后处理(注意:还把 inp 又用了一次)
    dag = MultiOutputNode([c])     # ④ 声明出口

# ---- 不编译,直接执行(走的是 DAG API 的默认执行后端) ----
ref = dag.execute(3)
print(ray.get(ref))                # [((3*2)+100)*3] = [318]
```

有几个细节值得停下来看：

* **`inp` 可以被用多次**。上面第 ③ 步把 `inp` 又喂给了 `post.run`，
  于是它同时依赖 `b` 和原始输入 —— 这是一条**扇入（fan-in）**边。
  普通 Ray 里你也能这么写，但要自己把 ref 传来传去。
* **`MultiOutputNode` 只接受 list（`tuple` 会被自动转成 list）**。
  传单个非序列值会直接 `ValueError: Invalid input type for \`args\`, <type>.`
  （源码 `python/ray/dag/output_node.py:18-21`）。
  哪怕只有一个出口也要写成 `MultiOutputNode([c])`。
* **`inp.x` 这种属性访问是合法的**。`InputNode` 支持下标/属性
  （`inp["batch"]`、`inp.x`），用来在运行时按名字取值。

### 不编译也能跑，但没有任何优化

上面那段 `dag.execute(3)` **没有调用 `experimental_compile()`**，
官方 quickstart 特意强调过：*"Note that there is no compilation
happening here."* 它走的是 DAG API 的默认执行后端 ——
也就是说，DAG API 本身只是**一层「显式建图 + 惰性求值」的写法糖**，
性能上不比手写 `.remote()` 更好。**真正的收益全部来自 §26.3 的编译。**

> 🔴 **别照着这段写生产代码 —— `DAGNode.execute()` 已经被弃用了。**
> Ray 给 `DAGNode.execute()` 加了 `DeprecationWarning`（PR **#63716**，
> 关闭 issue **#63666**）：
>
> ```
> DAGNode.execute() is deprecated and will be removed in a future release.
> ```
>
> **为什么弃用**：未编译的 DAG 每次 `execute()` 都会走到
> `FunctionNode._execute_impl()`，它内部执行 `ray.remote(self._body)`
> —— 也就是**每次执行都动态定义一个新的 remote 函数**，往 GCS 的
> internal KV（`fun` 命名空间）里导出新元数据，而**这些条目在 job 生命周期内
> 没有任何清理或淘汰策略**。反复执行的 DAG 会让 GCS KV **无界增长**。
> 这正是官方文档警告过的反模式（"别在循环里重新定义 remote 函数"），
> 只不过 DAG 把它藏在了 `execute()` 里面。
>
> **结论**：DAG API 的"未编译执行"这条路径，**官方已不推荐用于生产**，
> 出路就是本章的编译图（`experimental_compile()`）。本节保留这段是为了
> 说明"建图"与"编译"是两件事 —— 但**真正要用，请直接跳到 §26.3**。

> ⚠️ **一个真实的 API 不一致**：未编译的 DAG 与已编译的 DAG，
> `execute()` 的参数形式**不一样**（未编译接受参数列表，已编译要求
> 按 `InputNode` 属性逐个传位置参数），传错会报
> `ValueError: dag.execute() or dag.execute_async() must be called with
> N positional args, got M`（issue #46441 记录了这处不一致）。
> 迁移时这是第一个会绊到你的地方。

> ⚠️ **一张编译图只允许一个 `InputNode`**：否则编译时报
> `NotImplementedError: Compiled DAGs currently require exactly one InputNode`
> （`python/ray/dag/compiled_dag_node.py:1146`，2.58 原文如此；
> 2.44 / 2.51 的措辞也完全相同）。多输入的图要把参数打包成一个结构。
>
> ✅ **kwargs 是支持的**（2.58）：用 `InputNode()["name"]` 这种**字符串下标**
> 声明 kwargs，执行时 `compiled.execute(name=...)`；`_check_inputs`
> 会校验你没有漏传（漏了抛
> `dag.execute() or dag.execute_async() must be called with kwarg \`name\``）。
> 真正的约束是**不能混用**：同一张图里要么让所有任务直接用 `InputNode()`
> 本身，要么全都下标到具体的 args / kwargs —— 混着写会抛
> `ValueError: All tasks must either use InputNode() directly, or they must
> index to specific args or kwargs.`（`compiled_dag_node.py:1282`）。
>
> ⚠️ **本节早先的写法是错的**：它说「编译后的图不支持 kwargs」并引用了
> `Compiled DAGs do not support kwargs` 这个报错文案 —— 该文案在 2.58
> （以及 2.44 / 2.51）的 `python/ray/dag/` 下**检索不到**，kwargs 实际可用。
> 已按源码更正。

---

## 26.3 `experimental_compile()`

### 最小可用代码

```python
import ray
from ray.dag import InputNode

@ray.remote
class Receiver:
    def recv(self, data):
        return data * 2

receiver = Receiver.remote()

with InputNode() as inp:
    graph = receiver.recv.bind(inp)

compiled = graph.experimental_compile()          # ① 编译(只需一次)

for i in range(1000):
    ref = compiled.execute(i)                    # ② 提交(非阻塞)
    print(ray.get(ref))                          # ③ 取值(每个 ref 只能取一次!)

compiled.teardown()                              # ④ 收拾干净

# 同一件事的两种写法(官方文档的对照):
#   普通 Ray Core:   ref = receiver.recv.remote(data)   → ~1ms
#   Compiled Graph:  ref = compiled.execute(data)       → <50µs
```

四步：**建图 → 编译 → 反复 execute → teardown**。注意编译只需一次，收益体现在**后面每一次** `execute()` 上。

### 编译到底做了什么

编译是一次性的、**看得见全图**的过程。因为图是静态的，
编译期可以做很多急切执行时做不了的事：

| 编译期动作 | 为什么急切执行做不到 | 效果 |
|---|---|---|
| **预分配资源** | 急切执行不知道下一个任务要什么 | 省掉每任务的调度决策 |
| **给每条边建通道** | 不知道这条边会不会再被用 | 数据不再走对象存储(§26.5) |
| **预建 NCCL 通信组** | 不知道谁和谁要通信 | 免掉运行时建组握手 |
| **排执行序** | 没有全局视图,只能按依赖事件反应 | 避免 NCCL 死锁(§26.6) |
| **算重叠机会** | —— | 通信与计算重叠(见下) |

官方的说法是：静态执行模型使能了「预分配资源、以无死锁调度准备 NCCL
通信组、实验性的 GPU 计算/通信重叠、以及更好的多节点性能」。

### 执行与急切执行到底差在哪

```
急切执行:  driver ─SubmitTask→ raylet ─调度→ worker ─写plasma→ raylet ─通知→ driver
                      └─ 每跳都是一次 RPC / 一次状态更新

编译图:    driver ─写输入通道→ [ 图内 actor,按预排执行序跑 ] ─读输出通道→ driver
                      └─ 控制面在编译期就退场了
```

关键在于：**图内部节点之间的数据流动不再需要 raylet 参与**。
raylet 只在编译期和拆解期出现。

### ⚠️「experimental」这个名字是认真的：2026 年它仍然不稳

这是本章最需要打标的一段。

| 问题 | 状态 |
|---|---|
| API 首次可用 | **Ray 2.32**，当时的文档措辞是 *"developer preview stage. The APIs are subject to change and expected to evolve."* |
| 升级为 beta | **Ray 2.44**（2.44.0 release notes 把它列为该版本 highlight：*"This release features Ray Compiled Graph (beta)"*）。文档措辞变为 *"currently in beta (since Ray 2.44). The APIs are subject to change and expected to evolve. The API is available from Ray 2.32, but it's recommended to use a version after 2.44."* |
| 方法名本身 | 就叫 `experimental_compile()` —— 带 `experimental` 前缀这件事本身是官方态度 |
| 2.58 是否仍为 beta | **未确认**。我能核到的官方措辞持续到 2.51.1 仍是「beta (since 2.44)」；2.52–2.58 之间的措辞变化没有找到第一手来源 |
| 参数默认值 | 文档里出现的 `_max_inflight_executions`、`_overlap_gpu_communication` 等，**默认值未确认** |
| 支持普通函数节点 | **未确认**。有一份明确的 feature request 在要这个能力（issue #51593 *"[cgraph] Support function nodes"*），但**没有找到它被哪个版本实现的证据**。稳妥做法：**只用 actor 方法节点** |

> ⚠️ **实践结论**：把 Compiled Graph 当成「一个实验性但已被大规模生产使用过的
> 优化」来看。方向确定、收益确定、**接口不稳定**。用之前先固定 Ray 版本，
> 升级时专门回归这张图。

### 编译期的可调开关（都是下划线开头的私有参数）

```python
compiled = graph.experimental_compile(
    _overlap_gpu_communication=True,     # 通信/计算重叠(实验性)
    _max_inflight_executions=4,          # 允许几张图同时在飞
)
```

* **`_overlap_gpu_communication=True`**：官方 overlap 文档给的例子在开启后
  延迟从 **1.067s 降到 0.921s**（约 14%）。代价是**图的执行序会被重排** ——
  用 `RAY_CGRAPH_VISUALIZE_SCHEDULE=1` 可以把重排后的调度渲成图，被改过
  执行顺序的节点会标红（见 §26.8）。
* **`_max_inflight_executions`**：允许同时有多张图在飞（配合
  `enable_asyncio=True` 时用 `execute_async()`）。**超限抛
  `RayCgraphCapacityExceeded`** —— 注意不是排队，是报错。

> ⚠️ 下划线前缀意味着**这些参数随时可能改名或消失**。别把它们写进
> 生产配置的硬编码路径里，至少包一层。

---

## 26.4 三个约束（以及它们为什么必须存在）

Compiled Graph 的所有别扭之处，都源自同一件事：
**编译期必须知道整张图的完整形状**。

因为要预分配资源、要预先建通道、要预先排 NCCL 执行序 ——
这全都要求「图在编译那一刻就已经确定了」。

### 约束一：图必须是静态的

```python
# ✗ 编译图里不允许「按数据决定下一步跑什么」
@ray.remote
class Router:
    def route(self, x):
        if x > 0:
            return positive.work.remote(x)      # 动态提交任务
        return negative.work.remote(x)
```

编译期无法知道分支会走哪边，于是**无法为这条边预建通道**。
所以图里不能有「动态提交任务」这种把拓扑变成运行时决定的写法。

### 约束二：图里不能有 `ray.get()`

```python
# ✗ 图里不能把「等值」这件事塞进来
@ray.remote
class Bad:
    def step(self, ref):
        v = ray.get(ref)        # 一旦在图里阻塞,整张图的执行序就崩了
        return v + 1
```

因为编译图是**按预排的执行序推进的**：每个节点在拿到通道数据后立刻算、
算完立刻写通道。中间插一个阻塞等待，会破坏这个节奏 ——
更糟的是可能造成**跨 actor 的循环等待**。

官方表述是：不允许对 DAG 节点调用 `ray.get()` / `ray.wait()`，
必须**执行这张图**来求值。

### 约束三：所有节点都必须被 `bind()` 过

```python
# ✗ 忘了 bind 的节点根本不在图里
with InputNode() as inp:
    a = pre.run.bind(inp)
    b = model.forward.remote(a)      # ← 用了 .remote() 而不是 .bind()
    dag = MultiOutputNode([b])
```

`.remote()` 是**立即提交**的：它会当场发一个任务出去，
而返回的 `ObjectRef` 在编译期是个未知量。图里出现未知量，
编译期就建不出这条边的通道。

**规则**：图里的每一个节点都必须用 `.bind()` 描述，
包括那些你「顺手」写的中间步骤。

### 还有一批「编译期硬校验」

除了上面三条设计层面的约束，编译期还有一堆**会直接抛错的硬规则**。
它们的报错文案都很直白，值得背下来：

| 报错文案 | 含义 | 怎么改 |
|---|---|---|
| `NotImplementedError: Compiled DAGs currently require exactly one InputNode` | 一张图只允许一个输入节点 | 多输入打包成一个结构 |
| `ValueError: All tasks must either use InputNode() directly, or they must index to specific args or kwargs.` | **kwargs 本身可用**，但同一张图里「直接用 `InputNode()`」与「下标取某个参数」**不能混用** | 统一成一种写法（不要一半直接、一半下标） |
| `Compiled DAGs currently only support actor method nodes` | **图里只能有 actor 方法节点** | 把函数包成一个 actor；或用 `bind` 的模板任务技巧 |
| `ValueError: ... Please call experimental_compile() first` | 在编译前调了 `visualize()` | 先编译 |
| `ValueError: ... Please reuse the existing compiled DAG or create a new one` | 对同一张 DAG 调了两次 `experimental_compile()` | 复用已有的编译图，或重新建图 |
| `ray.get() can only be called once on a CompiledDAGRef...` | 对同一个 ref 取了两次值 | 见 §26.8 —— 底层内存会被复用 |

> ⚠️ **「只能有 actor 方法节点」这条最容易被忽略**，因为 DAG API
> 在**未编译**时是允许函数节点的。也就是说：一段不编译能跑的代码，
> 加上 `experimental_compile()` 之后可能直接报错。
> 应对办法是把函数包成一个轻量 actor（就是 issue #51593 里那位作者
> 抱怨的 workaround：「给每个可用 CPU 建一个 runner actor，轮流分发」——
> 他明确指出这**会引入人为瓶颈**）。

---

## 26.5 通信优化：从对象存储到通道

### 回顾：对象存储是 Ray 的数据面

第 3 章 §3.6、§3.7 讲过：普通 Ray 里，**所有**跨节点的数据都以
对象（Object）的形式走对象存储（Plasma）与 ObjectManager。

```
普通 Ray 的一条边:
  worker A ──写──▶ plasma(A) ──ObjectManager pull──▶ plasma(B) ──读──▶ worker B
                   └─ 跨节点要拉一次,同节点零拷贝
```

问题在于：即使 A、B 在同一台机器、同一张 GPU 上，数据也要先进对象存储、
被对象目录登记、由 raylet 协调 —— 因为它服务的对象生命周期是
「有 ID、可被任意消费者取、可溢出」。对象存储的设计目标是
**通用的不可变对象共享**（第 7 章），为此付出了 ID 分配、目录登记、
引用计数、pin、溢出这一整套代价。对「只在 A 和 B 之间传一次」的
流水线边来说，**这套机制全是开销**。

### 编译图的答案：通道（channel）

编译图给**每一条边**建一条专用的**通道**，
数据从生产者直接推到消费者，**不进对象存储**：

```
编译图的一条边:
  worker A ══写══▶ [ channel ] ══读══▶ worker B

  通道在编译期创建,类型由两端的位置决定:
    ├─ 同进程          → IntraProcessChannel(完全不序列化)
    ├─ 同节点          → 共享内存通道
    └─ 跨节点 GPU 张量 → NCCL 通道
```

通道的类型与实现都在 `ray.experimental.channel` 下
（接口是 `ChannelInterface`，定义在 `python/ray/experimental/channel/common.py`）：

| 通道 | 何时用 | 关键实现 |
|---|---|---|
| **进程内通道** | 读写双方在同一个 worker 进程里 | `IntraProcessChannel` —— **完全跳过序列化**，靠 `_SerializationContext` 直接换引用 |
| **共享内存通道** | 同节点、不同进程 | `SharedMemoryType` / `CompositeChannel`，缓冲区用对象存储（Plasma）分配，多读者/跨进程用信号量协调；带缓冲的版本是 `BufferedSharedMemoryChannel` |
| **NCCL 通道** | 跨节点/跨 GPU 的 `torch.Tensor` | `TorchTensorAcceleratorChannel`，底层 `_NcclGroup`（包 NCCL 原语，有独立的收发 CUDA 流用于重叠） |

### 传输类型是怎么选的

你不需要手写选路 —— Ray 有一个解析器（`AutoTransportType` 的
`TypeHintResolver`）按下面的规则自动决定：

| 条件 | 选择的传输 |
|---|---|
| 写端或读端是 **driver**（`None` actor） | **共享内存** —— 因为不支持 driver 落在 GPU 上 |
| 写端、读端**不是全都用 GPU** | **共享内存** |
| 写端、读端**用同节点上的同一张 GPU** | **共享内存** |
| 写端、读端**用不同的 GPU** | `TorchTensorType(transport="accelerator")` |

想手动钉住，就用 tensor 类型提示：

```python
with InputNode() as inp:
    branch = sender.send.bind(shape, dtype, inp)
    # 显式要求 NCCL：注意这是 with_tensor_transport 的参数，不是 TorchTensorType 的
    branch = branch.with_tensor_transport(
        "nccl", _static_shape=True, _direct_return=True
    )
```

`with_tensor_transport(transport)` 的合法取值：

* `"auto"` —— **默认**，走主机内存、以 NumPy 为序列化格式；
  双方都在 GPU 上时会自动选加速器通道；
* `"accelerator"` —— **推荐**写法，映射到
  `TorchTensorType(transport="accelerator")`，免掉主机内存拷贝；
* `"nccl"` —— **向后兼容**的别名，内部同样映射到 `"accelerator"`
  （`dag_node.py:196-198`）；
* `"shm"` —— 强制退回共享内存，调试用。

> ⚠️ **别把它的参数和 `TorchTensorType` 构造器的参数混了**：
> `TorchTensorType` 的构造器**只接受** `"auto"` / `"cpu"` / `"accelerator"` 三个值，
> 传 `"nccl"` 或 `"gloo"` 会直接抛 `ValueError`
> （2.58 的 `python/ray/experimental/channel/torch_tensor_type.py` 就是这么校验的）。
> `"nccl"` 只在 `with_tensor_transport()` 里合法。

> ⚠️ **API 改名过一次**：早期写法是 `with_type_hint(TorchTensorType(...))`，
> 后来提供了更直的 `with_tensor_transport(transport="nccl", ...)`。
> vLLM 侧的相关记录显示 **Ray 2.42 起** 用 `with_tensor_transport(...)`、
> `transport="auto"` 在双方都用 GPU 时会自动选 NCCL
> （vLLM PR #15831 把环境变量从 `VLLM_USE_RAY_COMPILED_DAG_NCCL_CHANNEL`
> 改成了 `VLLM_USE_RAY_COMPILED_DAG_CHANNEL_TYPE`，可用 `"shm"` 强制退回
> 共享内存以便调试）。
> ⚠️ **但 `with_type_hint()` 在 2.58 里并不存在**：对 `python/ray/dag/dag_node.py`
> 与 `compiled_dag_node.py` 检索 `with_type_hint` 是 **0 命中**，
> 存在的只有 `with_tensor_transport`（`dag_node.py:142`）和一个 `type_hint` 属性
> （`:230-237`）。所以调它不是"两个名字挑一个"，而是直接 `AttributeError`。
> **至少 2.58 只有 `with_tensor_transport`。**

`_static_shape` / `_direct_return` 是**性能换稳定性**的开关：
开了能更快，但你必须保证「形状与 dtype 恒定」且「返回值确实是 tensor」，
否则**整张图会被拆掉**。

### 与 RDT 的关系：别搞混

第 3 章 §3.7 与第 7 章 §7.8 讲过 **RDT（Ray Direct Transport，2.50 alpha）**。
两者目标相近、机制不同：

| | RDT | Compiled Graph 的通道 |
|---|---|---|
| 解决什么 | **单个 actor 的返回值**怎么传 | **整张图的所有边**怎么传 |
| 声明方式 | `@ray.method(tensor_transport="nccl")` | 编译期自动选 + `with_tensor_transport` |
| 需要静态图吗 | **不需要**，急切执行也能用 | **需要**，这是编译的前提 |
| 支持后端 | Gloo / NCCL / NIXL（RDMA） | 进程内 / 共享内存 / NCCL |

一句话区分（第 3 章 §3.7 已经点过）：**RDT 解决「怎么传」，
Compiled Graph 解决「怎么少传控制消息」**。两者可以叠加。

---

## 26.6 多 GPU 与 collective 通信

### 为什么张量并行推理非用它不可

回忆 §26.1 的三类负载。其中**多 GPU 张量并行推理**是 Compiled Graph
最核心的用武之地，原因是它的通信模式特别苛刻：

```
张量并行的一层(示意):
  每层的计算可能只有几十微秒,但层与层之间必须交换激活值(可达几百 MB)。
  如果每次交换都走「对象存储 + ray.get」,通信就是纯亏损。
```

编译图在这件事上提供三样东西：

| 能力 | 官方描述 |
|---|---|
| **原生 GPU↔GPU 通信** | 经典 Ray API **没有**直接的 GPU↔GPU RDMA 通信，需要外部工具；编译图用 NCCL 补上这一块 |
| **无死锁的 NCCL 排程** | 编译期就知道谁和谁通信，可以排出不会互相等待的执行序 |
| **通信/计算重叠** | `_overlap_gpu_communication=True`（实验性），官方例子 1.067s → 0.921s（约 14%） |

`_NcclGroup` 同时实现了**点对点 send/recv** 与 **collective（all-reduce）**，
并带独立的收发 CUDA 流来做重叠。

### collective 的支持现状：几条线索，时间点不一致

这是一个**事实本身比较乱**的地方，值得单独说清 —— 因为你在不同年份的
文章里会读到互相矛盾的结论。

| 时间/来源 | 说法 |
|---|---|
| 最初的 NCCL 通道提交 | 明确写着 *"p2p only, no collectives"*（PR #45092） |
| 官方 troubleshooting 页面 | 限制仍然写作「只支持点对点 GPU↔GPU 通信；collective ops 是 *coming soon*」 |
| 实际代码里 | 已经有 `ray.experimental.collective`（如 `allreduce`）、`CollectiveOutputNode`、`_CollectiveOperation`（在 `ray/dag/collective_node.py`），`allreduce.bind(...)` 的 `transport` 默认就是 NCCL |
| 更新的模块 | 出现了 `ray.experimental.rdt`，含 `CollectiveTensorTransport` / `NCCLTensorTransport` / `GLOOTensorTransport`，且 `DEFAULT_TRANSPORTS = ["NIXL", "GLOO", "NCCL", "CUDA_IPC"]` |

**结论**：collective 能力**已经在代码里**，但**官方文档的措辞没有同步更新**。
至于「在 Ray 2.58 里 collective 是否已算正式可用」—— **未确认**。
已知的明确边界是：自定义 transport 必须与输入节点的 actor 集合匹配
（否则抛 `ValueError: Expected all input lists to have the same set of
actor handles.`，`python/ray/dag/collective_node.py:120-124`）。

⚠️ **操作种类**：2.58 的 `ray.experimental.collective` 导出**三种** ——
`allreduce` / `allgather` / `reducescatter`
（`python/ray/experimental/collective/__init__.py` 的 `__all__`）。
本书早先写的「collective 目前只实现了 `allreduce` 一种操作
（*"Only ReduceOp is implemented"*）」**在 2.44 是对的**
（那一版 `__all__ = ["allreduce"]`），但**自 2.48 起已经是三种**；
而且那句文案在 2.58 的 `experimental/collective/` 与 `dag/collective_node.py`
里都**检索不到**。注意别把 `ReduceOp`（「怎么归约」的枚举 SUM/PROD/MIN/MAX，
在 `experimental/util/types.py:12`）当成「有哪几种 collective」。已按源码更正。

> ⚠️ **遇到矛盾时的做法**：直接在你的 Ray 版本里
> `import ray.experimental.collective` 试一下，或者干脆退回
> 「在 actor 内部用 `torch.distributed` 自己建组」——
> 这正是下面 vLLM 的选择。

### 现实对照：vLLM 正在**离开**这条路

这一条非常重要，因为它是「Compiled Graph 是不是未来」这个问题的
最直接的反证。第 20 章 §20.2 已有完整展开，这里只取与本章相关的部分：

vLLM 的 RFC **#35848 "Revamp Ray Distributed Executor Backend"**
（**由 Ray 团队自己提交**）提出用 **`RayExecutorV2`** 取代旧的、
基于 compiled graph 的 Ray executor：

| 维度 | 旧（compiled graph） | 新（`RayExecutorV2`） |
|---|---|---|
| 继承自 | Ray DAG / compiled graph | `MultiprocExecutor` |
| 控制面 | Ray 的 DAG 执行 | **MessageQueue**（共享内存，跨节点退回 ZMQ/TCP） |
| 数据面 | Ray 的对象传输 / 编译图通道 | **`torch.distributed` / NCCL** |
| 热路径上的 `ray.remote` | 有 | **没有** |

保留 Ray 变体的理由被收敛到三条：**跨节点并行的简便性、GPU worker 的
细粒度放置、资源感知调度** —— 注意，**一条都不是数据面性能**。

这件事的正确解读方式（也是第 20 章 §20.2 的判断）：

> **这不是「Compiled Graph 没做对」，而是「分工成熟了」。**
> 编译图证明了「把控制面压到微秒级」是可做到的；
> 但当纯粹的 GPU 集群内通信成为主矛盾时，
> 直接用 `torch.distributed` + NCCL 比「让 Ray 来做数据面」更划算。
> **Ray 剩下的价值是「谁在哪里、什么时候起、给多少资源」。**

**给你的实践含义**：如果你要把 Compiled Graph 用在推理上，
先想清楚你需不需要它提供的**调度与放置**能力。
如果不需要（比如就是一堆固定 rank 的 GPU），直接用 NCCL 更简单也更快。

---

## 26.7 与普通 `ray.remote` 的对比表

### 逐项对比

| 维度 | 普通 `ray.remote` | Compiled Graph |
|---|---|---|
| **单次调用开销** | **~1ms**（官方口径） | **<50µs**（重复执行同一张图时） |
| **数据面** | 对象存储（Plasma）+ ObjectManager | 通道：进程内 / 共享内存 / NCCL |
| **调度时机** | 每个任务提交时都要调度 | 编译期一次 |
| **资源分配** | 运行时按需 | 编译期预分配 |
| **动态任务提交** | ✅ 自由 | ❌ 图必须静态 |
| **图里 `ray.get()`** | ✅ 可以 | ❌ 不允许 |
| **节点类型** | 函数 / actor 方法都可以 | **目前只支持 actor 方法节点**（issue #51593 在要函数节点） |
| **GPU↔GPU 通信** | ❌ 无原生支持（要 RDT 或自己建 NCCL） | ✅ NCCL 通道 |
| **调试** | pdb / `ray debug` / timeline / State API 全都能用 | 额外有 `visualize()`；但**图内部节点不产生普通 task 事件** |
| **容错** | 任务重试、lineage 重建、actor 重启（第 10 章） | 应用异常不影响后续执行；**系统异常会导致整张图关停**；actor 死亡抛 `ActorDiedError` |
| **生命周期** | 无（每个 ref 独立） | 必须 `teardown()`；actor 复用前不 teardown 会资源冲突甚至段错误 |
| **结果对象的用法** | `ObjectRef` 可任意传递、pickle、`ray.wait` | `CompiledDAGRef` **只能取一次值**，不能传给别的任务/actor |
| **并行度控制** | worker 池 + 资源 | `_max_inflight_executions`，**超限报错不排队** |
| **API 稳定性** | 稳定 | **beta（自 2.44）+ 方法带 `experimental_` 前缀** |
| **适用规模** | 通用 | 单机到多机多卡、固定拓扑 |

### 决策规则

按顺序问自己四个问题：

```
① 单次计算 < 几毫秒?         否 → 用普通 ray.remote(编译图只会多一堆约束)
② 在反复执行同一张图?        否 → 先试「批量化」(第 18 章反模式那节),
                                  把 N 次小调用合成 1 次,收益通常更大
③ 需要跨 GPU 传张量?         否 → 编译图能帮你,但先量一下值不值得
④ 需要 Ray 的调度与放置?     否 → 直接上 torch.distributed + NCCL,
                                  别绕 Ray(§26.6 的 vLLM 教训)
                            是 → ✅ Compiled Graph
```

三条补充的判断题，任一条命中就**别用**编译图：

| 命中 | 原因 |
|---|---|
| 图里有分支/递归/动态提交 | 违反静态约束（§26.4） |
| 需要在图里 `ray.get()` | 违反约束二 |
| 图里的节点是普通函数（不是 actor 方法） | 目前不支持（issue #51593） |
| 需要把中间结果拿出去给别的任务用 | `CompiledDAGRef` 不能外传 |
| 你用的是一个会随时升级 Ray 版本的环境 | beta + `experimental_` 前缀 + 未确认的参数默认值 |

> ⚠️ **最后一句最重要**：编译图的收益（<50µs）是有前提的 ——
> **必须反复执行同一张图**。一次性执行的图，编译开销还没摊回来就结束了。
> 官方的建议是「重复执行同一张图」的负载。

---

## 26.8 调试与排查

### 先认清：编译图的失败模式与普通 Ray 很不一样

普通 Ray 的调试核心是 **State API**（第 11 章 §11.2）：
每个任务有状态机、有 `attempt_number`、有节点归属。

**编译图里没有这些** —— 图内部节点之间的数据流不经过 raylet，
所以它们**不产生普通的 task 事件**。你能看到的是：
编译、执行、拆解这些**图级别**的动作，以及**通道错误**。

| 层级 | 工具 | 能看到什么 |
|---|---|---|
| 图结构 | `compiled.visualize()` | 节点、边、通道细节、执行序 |
| 执行序 | `RAY_CGRAPH_VISUALIZE_SCHEDULE=1` | 优化后的调度（重排过的节点标红） |
| 时间 | `ray.timeline(...)` | 图级别的执行跨度（但看不清图内部） |
| Actor | `state.list_actors()` | 挂着图的那些 actor 还活着吗 |
| 通道 | 环境变量调超时 + 异常信息 | 超时/关停的原因 |

### `visualize()`：唯一能看见图本身的手段

```python
with InputNode() as inp:
    graph = receiver.recv.bind(inp)
compiled = graph.experimental_compile()      # ← 必须先编译
compiled.visualize()                          # 默认写 ./compiled_graph.png
```

> ⚠️ **在 `experimental_compile()` 之前调 `visualize()` 会直接抛
> `ValueError`**，提示你先编译。

可用的参数与渲染约定（官方 visualization 文档）：

| 参数 | 作用 |
|---|---|
| `filename` | 输出文件名（**不含扩展名**，默认 `"compiled_graph"` → 写出 `compiled_graph.png`） |
| `format` | `png` / `pdf` / `jpeg` / **`ascii`**（没装 graphviz 时的救命选项） |
| `view` | 渲染后自动打开 |
| `channel_details` | 连通道信息一起画。⚠️ **与 `format="ascii"` 互斥** —— 同时用会抛 `ValueError` |
| （返回值） | **返回值本身就是 DOT 字符串**（`ascii` 格式则返回 ASCII 图），不需要额外参数 |

> ⚠️ **签名只有这四个参数**：`visualize(filename="compiled_graph", format="png",
> view=False, channel_details=False) -> str`（`python/ray/dag/compiled_dag_node.py:3051-3057`）。
> 本书早先表里列的 **`return_dot` 参数在 2.58 里不存在**（2.44 / 2.51 也检索不到）——
> 要拿 DOT 文本直接用返回值即可。

节点形状/颜色的约定（看图时对号入座）：

| 节点 | 渲染 |
|---|---|
| `InputNode` / `InputAttributeNode[key]` | 浅蓝矩形 |
| `MultiOutputNode` | 黄色矩形 |
| `ClassMethodNode`（actor 方法调用） | 椭圆（浅绿或按 actor 着色） |
| `ClassMethodOutputNode[idx]` | 橙色矩形 |

> **依赖**：需要 graphviz（测试里还用 pydot）。
> 装不上就 `format="ascii"` —— 在终端里直接打出图，
> 排查时比截图快得多。

要看**优化后的执行序**（尤其是开了 `_overlap_gpu_communication` 之后）：

```bash
RAY_CGRAPH_VISUALIZE_SCHEDULE=1 python my_graph.py
# 会在 experimental_compile 时额外产出 compiled_graph_schedule.png
# 执行顺序被优化改动的节点标红
```

### 常见错误速查

| 症状 / 异常 | 原因 | 处置 |
|---|---|---|
| 编译期硬校验（多个 `InputNode`、`InputNode()` 与下标取参数**混用**、函数节点、重复编译） | 见 §26.4 的硬校验表 | 照那张表改 |
| `ValueError: ... call experimental_compile() first` | 编译前调了 `visualize()` | 先编译 |
| `ValueError: ray.get() can only be called once on a CompiledDAGRef` | 对同一个 ref 取了两次值 | 见下方「单次限制」 |
| `RayChannelError` | **系统异常**（网络错误等） | 图**会自动关停**；检查网络与 actor 存活 |
| `ActorDiedError` | actor 死了 | 图自动关停，但**其余 actor 不受影响**；按第 10 章处理 actor 容错 |
| `RayChannelTimeoutError` | 通道超时 | 见下方「三个超时陷阱」 |
| `RayCgraphCapacityExceeded` | 在飞执行数超过 `_max_inflight_executions` | 先 `ray.get` 收掉旧结果，或调大上限 |
| 行为诡异 / 段错误 | **复用了 actor 但没 teardown** | 见下方 |

### 应用异常 vs 系统异常：图会不会死？

这是编译图容错语义里最重要的一条区分（第 10 章讲的是普通任务的对应版本）：

| 异常类型 | 例子 | 图的命运 |
|---|---|---|
| **应用异常** | 你的代码抛 `ValueError` | 异常传播到输出；**图仍然可执行** |
| **系统异常** | 网络错误 → `RayChannelError` | **整张图自动关停** |
| **actor 死亡** | → `ActorDiedError` | **整张图关停**，但**不影响其它 actor** |

也就是说：**写错代码不会毁掉你的图，但基础设施抖一下会**。

### 三个超时陷阱（都很容易踩）

编译图为 `compiled_dag.execute()` 和 `ray.get()` 都设了超时，**默认 10 秒**：

| 环境变量 | 管什么 |
|---|---|
| `RAY_CGRAPH_submit_timeout` | `compiled_dag.execute()` 的提交超时 |
| `RAY_CGRAPH_get_timeout` | `ray.get()` 的取值超时 |
| `RAY_CGRAPH_teardown_timeout` | `compiled_dag.teardown()` 的拆除超时 —— **默认 30 秒**，不是 10（`python/ray/dag/context.py:13-15`） |

`ray.get()` 还支持**单次调用**的 `timeout=` 参数。
超时时抛 `RayChannelTimeoutError`，异常信息会提示你：
如果是**预期内的大计算**就调大超时，否则说明**执行卡住了**。

**陷阱清单**：

1. **NumPy 零拷贝导致的假死**。Ray 在可能的情况下会对 NumPy 数组做
   **零拷贝反序列化**。如果你上一次执行拿到的数组**还没被释放**，
   就去取下一次的结果，可能**直接挂死**，最终表现为
   `RayChannelTimeoutError`。官方建议是**显式 `del`**，别指望 Python 的 GC
   及时跑 —— 因为「零拷贝值会阻塞后续执行，直到它离开作用域」，
   这是一个潜在的**死锁**（文档明确写了 risk of deadlock）。
2. **没 teardown 就复用 actor**。用上一张编译图的 actor 去建新图、
   却不显式 `teardown()`，**有资源冲突甚至段错误的风险** ——
   因为 Python 不保证在你预期的时候回收旧图。
   ```python
   compiled.teardown()                  # 保留 actor
   compiled.teardown(kill_actors=True)  # 连 actor 一起杀掉
   ```
   `teardown()` 自己也有超时：`RAY_CGRAPH_teardown_timeout`，**默认 30 秒**
   —— 卡在拆除阶段就调它。
3. **在飞执行数超了**。编译图**不排队**：超过
   `_max_inflight_executions` 直接抛 `RayCgraphCapacityExceeded`。
   正确写法是**先收结果再提交**，而不是无脑往循环里塞。

### 一堆「单次」限制（写代码前必须知道）

| 限制 | 后果 |
|---|---|
| `ray.get(CompiledDAGRef)` **只能调一次** | 第二次抛异常 —— 底层内存可能要被复用，限制取值次数是为了简化缓冲区追踪 |
| `CompiledDAGRef` **不能**传给别的任务/actor | 抛 `TypeError` / `ValueError` |
| `CompiledDAGRef` **不能** pickle / copy / deepcopy | 同上 |
| `CompiledDAGRef` **不能**用在 `ray.wait()` 里 | 同上 |
| 一个 actor **同时只能跑一张编译图** | 换图前必须先 teardown |
| **只有编译这张图的进程**能调 `execute()` | 换个进程会让你重新编译 |

> ⚠️ **一个已知 bug 值得记住**（issue #46284）：
> 在 `dag.teardown()` 之后、或 actor 已经失败之后，再去
> `ray.get()` 一个 `CompiledDAGRef`，**会挂住而不是抛异常**。
> 也就是说它违反了「失败要快速报错」的原则。
> 排查时如果发现「取结果这一步静默不动了」，先怀疑这个。

### 排查顺序（建议照抄）

```
编译报错        → 看异常文案,基本都在 §26.4 的硬校验表里
结果不对        → visualize(format="ascii") 看拓扑;查有没有误用 .remote()
慢 / 卡住       → RayChannelTimeoutError? → 三个陷阱;先 del 掉上一轮 NumPy 结果
整张图突然死    → 分清 RayChannelError 还是 ActorDiedError;list_actors() 看存活
复用 actor 崩了 → 忘了 teardown()
```

---

## 26.9 mini-ray 的对照

### 直说：mini-ray 没有实现 Compiled Graph

这是**刻意不做**的，不是遗漏。mini-ray 的诚实清单里
（`mini-ray/README.md` 的「明确不做的」一节）明确列了 `NCCL/RDMA 数据面`。

我逐关键字搜过整个 mini-ray 代码库：

| 关键词 | 结果 |
|---|---|
| `channel` | **零命中** |
| `InputNode` / `MultiOutputNode` | **零命中** |
| `CompiledDAG` / `execution_plan` | **零命中** |
| `tensor_transport` | **零命中** |
| `compile`（作为 API） | **零命中**（⚠️ 字面量 `compile` 有 4 处命中，但全在 `tools/check_docs.py` 的 `re.compile(...)` 里 —— 那是正则编译，不是 DAG 编译。作为 **API** 讲，mini-ray 确实一个都没有） |
| `graph` | **零命中** |
| `dag` / `DAG` | **只出现在注释与示例文案里** |
| `NCCL` | 只作为**类比**和**非目标**出现 |

`mini-ray/examples/02_dependencies.py` 的第一行注释写着「依赖图与流水线：
把任务组织成 DAG」，但紧接着第 8 行就说清了：

> 你不需要写 DAG、不需要显式声明边 —— 把上一个任务的 ObjectRef 当参数传进去。

**这就是最本质的差别**：真实 Ray 的编译图是一个**显式声明、可编译的产物**
（`InputNode` / `MultiOutputNode` / channel / `experimental_compile()`）；
mini-ray 的「图」是**从数据依赖里涌现出来的**，
你既建不出这个对象，也没法编译它。

### ⚠️ 一个容易误会的点：mini-ray 有 `bind()`，但它不是 DAG API

mini-ray 里确实有 `.bind()`，在 `miniray/remote.py:170` 与
`miniray/actor.py:106`，测试里也有覆盖。但它的语义是
**「参数部分套用」** —— 把一部分参数绑死，
返回一个新的「模板任务」。文档字符串写得很明确：
「绑定的参数会**前置**到本次调用的参数之前」。

> ⚠️ **这里有个本书第五轮才纠正的措辞**：早先写的是
> "Ray 2.8 的「参数部分套用」"，暗示真实 Ray 也有这么一档。
> **真实 Ray 没有** —— 它的 `.bind()` **只有一种语义**：
> 返回 DAG 节点 `FunctionNode`（自 **Ray 2.0** 的 DAG API 就有），
> 在那上面调 `.remote()` 会抛
> `AttributeError: .remote() cannot be used on <class 'ray.dag.function_node.FunctionNode'>`
> （真实报错会把类型打成**全限定名**，后面还跟着 "To execute the task …"，
> 这里按 `dag_node.py:720` 的原文写）。
> 所谓"绑一部分参数"只是**同一个机制**的自然用法
> （未绑的参数在 `execute()` 时补上），它返回的**仍然是节点，不是 `RemoteFunction`**。
> 「两种语义 / Ray 2.8 那一档」是本书生造出来的，第五轮已删除。

**这不是建图**。mini-ray 的 `bind` 返回的是一个 `RemoteFunction`，
不是 DAG 节点；它没有边，也不能被编译。

### 那么 mini-ray 里「谁在扮演编译图的角色」

答案是：**没有任何一个机制在扮演它**。但有两个机制在**部分功能**上覆盖了编译图所依赖的基础设施 —— 这反过来是理解「编译图到底新在哪」的好角度。

| 编译图的组成部分 | mini-ray 里的对应物 | 差在哪 |
|---|---|---|
| **图结构（显式拓扑）** | 无。依赖关系由「传 ObjectRef」隐式产生 | 没有编译期，也没有全局视图 |
| **编译期静态分析** | 无 | 调度决策全部在运行时做 |
| **通道（数据面）** | **对象存储**（`object_store.py`） | 见下 |
| **依赖唤醒** | **`TaskScheduler._waiting` 反向索引** | 见下 —— 这是最接近的类比 |
| **预分配资源** | 无（`pick_node` 每次现算） | —— |
| **NCCL / GPU 传输** | 明确不做 | —— |
| **反复执行同一张图** | 无 | 每个示例都自己在 Python `for` 循环里 `.remote()` |

### 类比一：`TaskScheduler._waiting` —— 一个「运行时的依赖索引」

这是 mini-ray 里与编译图**结构上最像**的东西，
在 `mini-ray/miniray/scheduler.py`：

```python
#: 依赖 → 等它的 task
self._waiting: Dict[bytes, Set[str]] = {}
#: 就绪队列(FIFO;Ray 里也是按提交顺序 + 优先级)
self._ready: List[str] = []
```

`_waiting[object_id]` 给出「哪些任务在等这个对象」，
核心消费函数是 `mark_object_ready()`：

```python
for task_hex in list(self._waiting.pop(object_id, ())):
    record = self.tasks.get(task_hex)
    if record is None or record.state is not TaskState.PENDING:
        continue
    record.missing_deps -= 1
    if record.missing_deps <= 0:
        newly_ready.append(record)
```

它的文档字符串自己说：「这是调度器里最重要的一个函数：
整个依赖图靠它「流动」起来。」唤醒是**事件驱动、不是轮询**：
对象就绪时 `Raylet._mark_object_ready()`（「对象就绪的**唯一入口**」）
会 `notify_all()` 并 `self._schedule_needed.set()` 叫醒调度循环
（`_TICK = 0.2` 那个定时器只是保底）。

**为什么这不能替代编译图**：

| | `_waiting` 反向索引 | 编译图的执行序 |
|---|---|---|
| 建立时机 | **运行时**，任务提交时才登记 | **编译期**，一次看全 |
| 形态 | 一张会变的哈希表 | 一张固定的 READ/WRITE/COMPUTE 计划 |
| 每次执行 | 重新走一遍「减计数 → 入队 → 派发」 | 按计划直接推，不需要重新决策 |
| 数据面 | 对象就绪后，消费者再从对象存储**取** | 生产者直接**推**进通道 |

也就是说：mini-ray 用**索引 + 事件**换来了「不需要全局视图」的简单性，
代价是**每一次执行都要重新付一遍调度成本** —— 这正好是 §26.1 算的那笔账。

### 类比二：对象存储 —— 它是数据面，但它是「通用」的数据面

`mini-ray/miniray/object_store.py`（**844 行**）实现了完整的
「共享内存 + 零拷贝 + pin + 溢出 + LRU 驱逐」（`SharedMemoryAllocator`
用 2 的幂分桶 + free list；回收顺序在 `_enforce_capacity()` 的文档字符串里
写得很清楚：**「按「回收 → 溢出 → 报错」的顺序腾空间」**）。

它对应的是**真实 Ray 的对象存储**（第 3 章 §3.6、第 7 章），
**不是编译图的通道**。两者的定位差别正是 §26.5 讲的：

| | 对象存储 | 编译图的通道 |
|---|---|---|
| 目标是 | **任意消费者、任意时刻**可取 | 一条**固定边**上的一次传递 |
| 为此付出 | ID、目录登记、引用计数、pin、溢出 | 几乎没有 |
| mini-ray 有吗 | ✅ 有 | ❌ 没有 |

**这正是编译图能快的原因**：它砍掉了对象存储那一整套通用机制。
mini-ray 保留了通用机制 —— 这在教学上是**对的**。

### 为什么「跳过编译」对教学实现是可接受的

三条理由：

1. **编译图的全部收益都建立在「有东西可编译」之上。**
   mini-ray 的依赖关系是**从数据流涌现的**，没有一个可枚举的图对象。
   要支持编译，得先重构出一套显式的建图 API ——
   那是另一套抽象，不是「简化版 Ray」。
2. **它服务的问题在单机教学场景里不存在。** 编译图针对的是
   「控制开销 ≫ 计算」的负载，而 mini-ray 的示例全是一个 driver 进程里的
   毫秒级任务 —— 把开销从 1ms 降到 50µs，在 `num_cpus=4` 上看不出来。
3. **真正难的部分它已经教了。** 依赖反向索引（`_waiting`）、对象存储、
   事件驱动的唤醒、lineage 重建 —— 这些是编译图**也建立在上面**的
   基础设施。编译图是「在有了这些之后，再往上做一次静态化」的优化。

> **一句话**：mini-ray 教你「依赖图是怎么跑起来的」，
> 编译图教你「依赖图跑起来之后，怎么把它的开销压掉一个数量级」。
> 前者是后者的前提。第 3 章 §3.9 的映射表里
> 「Compiled Graph / RDT → 未实现」，说的就是这条。

---

## 26.10 本章小结

* **动机**：经典 Ray Core API 每次任务调用约 **1ms** 系统开销，
  与任务本身算了多少无关。当单任务计算 ≲ 几毫秒时（张量并行推理、
  RL rollout、小算子流水线），控制开销会占掉绝大部分。
* **核心思路**：把图**编译一次**，之后当成一个分布式单元反复执行 ——
  控制面只在编译期出现，图内部数据走预建通道。
  官方口径：反复执行同一张图时**开销 < 50µs**。
* **DAG API 三件套**：`ray.dag.InputNode`（占位输入）、
  `MultiOutputNode`（多出口，**必须传 list / tuple**）、`.bind()`（建边，取代 `.remote()`）。
  **不编译的 DAG 也能跑**（`dag.execute()`），但没有任何性能收益，
  而且 **`DAGNode.execute()` 已被弃用**（PR #63716，原因是 GCS KV 无界增长）
  —— **生产代码请一律走编译路径**。
  ⚠️ mini-ray 里也有 `.bind()`，但那是**参数部分套用**、返回 `RemoteFunction`，
  不是建图；真实 Ray 的 `.bind()` 只有"返回 DAG 节点"一种语义（见 §26.9）。
* **`experimental_compile()`**：可用自 **2.32**（developer preview），
  **2.44 起 beta**（2.44.0 release notes 列为 highlight）。
  编译期做四件事：预分配资源、建通道、预建 NCCL 组、排执行序。
  ⚠️ **2026 年它仍然是 beta**，方法名带 `experimental_`，
  **2.52–2.58 的 beta 措辞与参数默认值我未确认**。
* **三个约束**（都源自「编译期必须知道整张图」）：
  **图必须静态**、**图里不能 `ray.get()`/`ray.wait()`**、
  **所有节点必须 `bind()` 过**。另有一批硬校验：
  只允许一个 `InputNode`、**`InputNode()` 与「下标取参数」不能混用**、
  **目前只支持 actor 方法节点**（issue #51593 在要函数节点）。
  ⚠️ **kwargs 本身是支持的**（用 `InputNode()["name"]` 声明），本书早先
  这里写的「不支持 kwargs」是错的，见 §26.4。
* **数据面**：编译图给**每条边**建一条**通道**
  （`ray.experimental.channel`），取代对象存储：
  同进程用 `IntraProcessChannel`（**完全不序列化**）、
  同节点用共享内存、跨 GPU 用 NCCL。
  传输类型由 `AutoTransportType` 自动选（driver 参与或非全 GPU → 共享内存；
  不同 GPU → NCCL）。手动指定一律用 `with_tensor_transport()`
  （vLLM 侧记录显示 **2.42 起**）。⚠️ 两个容易写错的点：
  `with_type_hint()` 在 **2.58 里根本不存在**（检索 0 命中，调它抛 `AttributeError`）；
  而 `TorchTensorType` 的**构造器**只认 `auto` / `cpu` / `accelerator`，
  传 `"nccl"` 会 `ValueError` —— `"nccl"` 只是 `with_tensor_transport` 的向后兼容参数。
  另外拆图有独立的 `RAY_CGRAPH_teardown_timeout`（**默认 30 秒**）。
* **多 GPU**：这是编译图最重要的用武之地 —— 经典 API **没有**原生
  GPU↔GPU RDMA，编译图用 NCCL 补上，并支持通信/计算重叠
  （`_overlap_gpu_communication=True`，官方例子 1.067s → 0.921s）。
  ⚠️ **collective 的支持现状自相矛盾**：早期提交写着 *p2p only*、
  troubleshooting 页还写 *coming soon*，但代码里已经有
  `ray.experimental.collective`（2.48 起是 `allreduce` / `allgather` /
  `reducescatter` 三种，2.44 时才只有 `allreduce`）。
  **在 2.58 里的正式可用状态未确认**。
  ⚠️ 而 vLLM 正在**离开**这条路（RFC **#35848**，`RayExecutorV2`，
  Ray 侧 issue #62505）—— 见第 20 章 §20.2。
* **选型**：只在「**反复执行同一张图** + **单任务 < 几毫秒**」时才考虑；
  需要跨 GPU 传张量、但**不需要** Ray 的调度与放置时，
  直接用 `torch.distributed` + NCCL。
  图里有分支/动态提交/普通函数节点、或需要把中间结果外传，
  就**别用**。
* **调试**：`visualize()`（**必须先编译**；`format="ascii"` 最省事）、
  `RAY_CGRAPH_VISUALIZE_SCHEDULE=1` 看优化后的执行序。
  编译图**不产生普通 task 事件**，State API 帮不上忙。
  ⚠️ 三大陷阱：**NumPy 零拷贝导致假死**（显式 `del`）、
  **没 teardown 就复用 actor**（可能段错误）、
  **在飞执行数超限直接报错不排队**。
  ⚠️ 已知 bug：`teardown()` 之后 `ray.get()` 会**挂住而不是报错**（issue #46284）。
* **mini-ray 没有实现它**，这是刻意的：`channel` / `InputNode` /
  `CompiledDAG` / `NCCL` 全部零命中。`TaskScheduler._waiting`
  （`Dict[bytes, Set[str]]` 反向索引）在**结构上**最接近，
  但它是**运行时索引**、不是**编译期计划**；
  mini-ray 用一个**通用**的对象存储当数据面，而不是每边一条通道 ——
  这恰恰是编译图能快的反面。
  跳过编译对教学实现是**正确的简化**：它教的是编译图的**前提**。

下一章是**附录 F**：`Ray Client`、`runtime_env` 与多语言绑定 ——
把本章反复出现的「你的代码怎么送到远端 worker 上」这件事讲透。
