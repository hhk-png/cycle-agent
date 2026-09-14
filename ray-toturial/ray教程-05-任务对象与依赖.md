仓库地址：https://github.com/hhk-png/cycle-agent

# 第 5 章：任务、对象与依赖（深入）

> 本章目标：把第 2 章那三个抽象讲到**能排查问题**的深度 —— 任务的完整生命周期、
> 依赖的解析细节、`num_returns` 与流式生成器、传参的序列化代价、
> 对象的引用计数与所有权。读完你应该能看着一段 Ray 代码就说出它的性能瓶颈在哪。

---

## 5.1 任务的完整生命周期

一个任务从提交到结束，Ray 内部会经历一串状态（`ray.util.state.list_tasks()`
的 `state` 字段就是它们）：

```
                     ┌────────────────────────┐
   f.remote() ──────▶│ PENDING_ARGS_AVAIL     │  依赖对象还没就绪
                     └───────────┬────────────┘
                                 │ 依赖全部就绪
                     ┌───────────▼────────────┐
                     │ PENDING_NODE_ASSIGNMENT│  等资源/等节点
                     └───────────┬────────────┘
                                 │ 分到节点
                     ┌───────────▼────────────┐
                     │ PENDING_OBJ_STORE_MEM_ │  等对象存储腾出空间
                     │ AVAIL                  │
                     └───────────┬────────────┘
                                 │
                     ┌───────────▼────────────┐
                     │ PENDING_ARGS_FETCH     │  把依赖对象拉到本地(跨节点时)
                     └───────────┬────────────┘
                                 │
                     ┌───────────▼────────────┐
                     │ SUBMITTED_TO_WORKER    │  已派给某个 worker
                     └───────────┬────────────┘
                                 │
              ┌──────────────────▼───────────────────┐
              │ RUNNING                              │
              │  ├ RUNNING_IN_RAY_GET   (卡在 ray.get)
              │  └ RUNNING_IN_RAY_WAIT  (卡在 ray.wait)
              └──────────────────┬───────────────────┘
                                 │
                     ┌───────────▼────────────┐
                     │ FINISHED  或  FAILED    │
                     └────────────────────────┘
```

**状态枚举一共 14 个**，上面画的是**主路径**。剩下几个只在特定场景出现，
但恰恰是排查 actor 问题时的关键：

| 状态 | 卡住的原因 | 怎么办 |
|---|---|---|
| `PENDING_ARGS_AVAIL` | 依赖没就绪；**actor 任务还会在这里等 actor 创建** | 顺着依赖链往上找那个真正慢/失败的任务 |
| `PENDING_NODE_ASSIGNMENT` | **资源不足**（最常见） | 看 `available_resources()`；检查是不是有 actor 占着资源 |
| `PENDING_OBJ_STORE_MEM_AVAIL` | 对象存储满了（是 `PENDING_NODE_ASSIGNMENT` 的子状态，主要用于打点） | 调大 `object_store_memory`，或减少同时在飞的对象 |
| `PENDING_ARGS_FETCH` | 跨节点拉数据慢 | 检查是不是把巨大的依赖传给了远处的任务（调度本地性） |
| `PENDING_ACTOR_TASK_ARGS_FETCH` | **actor 任务**在拉自己的依赖 | 同上，但注意 actor 任务不会被调度到别处 |
| `PENDING_ACTOR_TASK_ORDERING_OR_CONCURRENCY` | **actor 邮箱里排队**：等前面的调用、或等并发槽位 | 调 `max_concurrency`；看是不是有慢方法堵住了队列 |
| `GETTING_AND_PINNING_ARGS` | 正在把依赖 pin 到本地（防止被回收） | 大依赖时这一步会明显变慢，属正常 |
| `RUNNING_IN_RAY_GET` | 任务**自己**在等别的任务 | 警惕死锁：worker 池被「等待者」占满 |
| `RUNNING_IN_RAY_WAIT` | 同上，卡在 `ray.wait` | 同上 |
| `NIL` / `FINISHED` / `FAILED` | 占位 / 正常结束 / 失败 | 见第 10 章 |

> 枚举定义在 `ray/_private/custom_types.py::TASK_STATUS`，
> 且会与 protobuf 的 `TaskStatus` 做同步校验。

最后一行是分布式系统里的经典死锁：**所有 worker 都在等别人，没人干活**。
Ray 不会自动解决它 —— 需要在设计上避免（比如把「等待」集中到 driver，
或者用 `ray.wait` + 超时）。

---

## 5.2 依赖：对象级，不是任务级

Ray 的依赖粒度是**单个对象**，而不是「阶段」。这个区别值得单独体会：

```python
# 阶段式思维(Spark 风格):stage2 要等 stage1 全部完成
stage1 = [step1.remote(x) for x in inputs]
stage2 = [step2.remote(y) for y in stage1]     # ← 看起来像阶段,其实不是

# 实际发生的是:step2_i 只等 stage1_i
# inputs = [a, b, c],a 算得快 → step2(step1(a)) 立刻就能跑
```

### 依赖是怎么被发现的

你不需要声明依赖 —— Ray 从**参数的序列化结果**里推断：

```
args 序列化时,ObjectRef 被写成占位符(而不是值)
  │
  ├─ 序列化流里出现的每个 ObjectRef ID → 就是这个任务的依赖集合
  │
  └─ 调度器建反查表:{对象 ID → 等它的任务}
        对象就绪 → 唤醒这些任务(事件驱动,不是轮询)
```

Ray 的实现是在提交时把参数里的 ref 列表单独算一份；mini-ray 用的是
「扫 pickle 指令流找占位符」（见第 6 章 `serialization.extract_object_ids`）——
两种做法效果一样：**不需要反序列化就能建依赖图**。

### 依赖失败会传播

```python
@ray.remote(max_retries=0)
def broken():
    raise ValueError("上游炸了")

@ray.remote
def downstream(x):
    return x

ref = downstream.remote(broken.remote())
ray.get(ref)     # 抛 RayTaskError,消息里能看到「上游失败」
```

Ray 的做法是：失败任务的**每个**结果对象里都写入一个错误对象；下游任务取到这个
对象时立即失败，于是错误沿依赖链一路传下去。这也意味着**一个任务失败会连带
让它下游的一整条链失败**，这是排查时要顺着看的地方。

---

## 5.3 `num_returns` 与流式生成器

### 一次返回多个对象

```python
@ray.remote(num_returns=2)
def split(text):
    return text, len(text)

part, size = ray.get(split.remote("hello"))          # 元组解包
refs = split.remote("hello")                          # 也可以拿到 ref 列表
ray.get(refs)                                         # ['hello', 5]
```

`num_returns` 的语义：**返回值必须是长度恰好为 n 的 tuple/list**，否则任务失败。
为什么要有这个？因为两个返回值会成为**两个独立的对象** —— 下游可以只依赖其中一个，
另一个还没算完不影响它。

### 流式生成器：边算边产（`num_returns="streaming"`）

```python
@ray.remote(num_returns="streaming")     # 其实可省:生成器函数默认就是 streaming
def stream_chunks(total):
    for i in range(total):
        yield {"chunk": i, "data": load_part(i)}

gen = stream_chunks.remote(100)
for ref in gen:                 # 每 yield 一个,下游就能取一个
    consume(ray.get(ref))
```

它解决的问题很具体：**大结果不必等全部算完才能用**。

```
普通任务:  [======== 30s ========] → 结果(30s 后才能用)
生成器:    [=3s=]→c0  [=3s=]→c1  [=3s=]→c2 …   (每 3s 就有一个可用)
```

五个注意点：

1. **重试语义：整个任务从头重放，用 `attempt_number` 丢弃旧产出。**
   一个生成器如果已经 yield 了 4 个值再抛异常，Ray 会把**当前 attempt 标记为失败、
   attempt number 加一，然后重新提交同一份 task spec** —— 也就是**从头再跑一遍**，
   不是从第 5 个继续。
   下游之所以看到「没有重复、也没有断档」，是因为**旧 attempt 产出的对象被
   `attempt_number` 过滤掉了**，而不是因为函数跳过了前 4 个。
   ⚠️ **它假设你的生成器是幂等且确定性的**（官方 Internals 原文：
   *"this assumes that the generator task is idempotent and deterministic"*）。
   如果生成器的输出依赖内部随机状态、时间、或外部副作用，**重放就会产出不一样的东西**。
   （常见误解有两个：一是「流不能重放所以不重试」，二是「会从断点续传」——
   两个都不对，Ray **支持**重试，但方式是**整任务重放**。详见第 10 章 §10.2。）
2. **`ray.get(gen)` 会物化成列表**（等全部产完），适合小规模场景；
3. **异常在迭代时抛出**：生成器中途抛异常，你会在迭代到那一步时看到；
4. **背压可调，但默认是关的**：`_generator_backpressure_num_objects` 控制
   「未消费的产出堆积到多少个就暂停生产」—— **默认值就是 `-1`（不背压）**，
   想开启要显式设（`1` 最接近本地生成器的语义）。
   * async generator 早期不支持这个参数，**现在已实现**（PR #64383），
     另有 actor 级开关 `_actor_generator_backpressure_num_objects`（PR #63310）；
   * 生成器任务的背压与 **actor 的邮箱背压**（`max_pending_calls`，见第 9 章）
     是两套独立机制，别混。
5. 它也是 Ray Data 流式执行、以及很多「流式推理」实现的底层机制
   （官方 Internals 里有专门的 *Streaming Generator* 一节）。

> ⚠️ **mini-ray 的差异**：mini-ray **没有实现**重试（它要求生成器任务的
> `max_retries=0`），也**不实现** `_generator_backpressure_num_objects`。
> 所以在 mini-ray 上验证过的流式容错设计，搬到真实 Ray 前要重新确认 ——
> 这是两者最容易产生「本地全绿、线上全红」的地方之一。

---

## 5.4 传参的代价：序列化是分布式计算最容易被低估的成本

### 什么能传，什么不能

Ray 用 **cloudpickle**（pickle 的超集）序列化参数和函数。这意味着：

| 能传 | 不能传（或代价很大） |
|---|---|
| 基本类型、容器、numpy 数组、pandas DataFrame | 打开的文件句柄、socket 连接 |
| `__main__` 里定义的函数、闭包、lambda | 线程锁（`threading.Lock`） |
| 类定义（含 dataclass） | 生成器对象（要用 `num_returns="streaming"`） |
| 大多数第三方对象（只要可 pickle） | 依赖进程本地状态的 C 扩展对象 |

**函数是怎么传过去的**（这是 cloudpickle 存在的唯一理由）：

| 函数定义位置 | 传法 | 为什么 |
|---|---|---|
| 可导入模块里的函数 | 按**引用**（`module.qualname`） | worker `import` 一下就有 |
| `__main__` 里的函数（脚本、REPL、Jupyter） | 按**值** | worker 的 `__main__` 是另一个模块，找不到 |
| 闭包 / lambda | 按**值**（含被引用的外部变量） | 它们不是模块属性，无法按引用找 |
| 被装饰器替换掉的名字（`@ray.remote def f`） | 按**值** | 模块里 `f` 已经是包装器，不再是那个函数 |

所以有一个著名的坑：**闭包会把被捕获的对象一起复制到每个 worker**。

```python
big_model = load_model()          # 假设 2GB

@ray.remote
def infer(x):
    return big_model(x)           # ✗ 每个 worker 都会拿到一份 2GB 的副本!

@ray.remote
class Model:
    def __init__(self, m): self.m = m
    def infer(self, x): return self.m(x)

model = Model.remote(big_model)   # ✓ 只传一次,之后常驻
```

### numpy 的特殊待遇

Ray 对 numpy 数组做了**零拷贝特判**：数组的数据直接进 plasma，
消费者拿到的是共享内存上的只读视图。

```python
arr = np.arange(10**8)            # 800MB
ref = ray.put(arr)
view = ray.get(ref)               # 没有 800MB 的拷贝
view.flags.writeable              # False(不可变,所以可以安全共享)
```

**大对象传法对照表**（重复第 2 章的结论，但给了数字）：

| 写法 | 800MB 数组的代价 |
|---|---|
| `f.remote(arr)` × 100 个任务 | 100 次序列化 + 每个 worker 一份拷贝 → 内存直接爆 |
| `ref = ray.put(arr)` 后传 ref | 1 次写入共享内存，同节点零拷贝读 |

---

## 5.5 对象的引用计数与「所有权」

这是最容易被忽略、又最容易踩坑的一节。

### 谁是 owner

创建 ObjectRef 的那个进程（driver 或某个 worker）就是该对象的 **owner**。
owner 负责：

* 维护引用计数（本进程持有多少引用）；
* 在引用归零时通知系统回收对象；
* **owner 挂了，别的进程手里的引用会失效** —— 拿到的是 `OwnerDiedError`
  （语义上**不是** `ObjectLostError`：后者是「对象丢了但可以靠 lineage 重建」，
  前者是「负责记账的进程没了」，**不会**触发重建，见第 10 章 §10.4）。
  ⚠️ **但类型上它恰恰是子类** —— 2.58 的 `ray/exceptions.py` 里写的是
  `class OwnerDiedError(ObjectLostError)`。实践含义：**只写
  `except ObjectLostError` 会把两者一起吞掉**，你会把"不可重建"的
  owner 死亡当成"可重建"来处理。要靠语义区分必须**显式先判 `OwnerDiedError`**：

  ```python
  try:
      val = ray.get(ref)
  except ray.exceptions.OwnerDiedError:      # ← 必须写在前面
      ...    # 不可重建，重新 ray.put / 重跑产出它的任务
  except ray.exceptions.ObjectLostError:
      ...    # 可重建，等 Ray 走 lineage
  ```

这就是为什么「把 ref 传给一个短命的任务，再从别处用它」是危险的：
如果那个任务里的 ref 是唯一的引用，任务结束时对象就被回收了。

### 引用计数怎么影响你

```python
refs = [f.remote(i) for i in range(1000)]    # 1000 个对象都活着(你持有 ref)
del refs                                      # 引用归零 → 系统可以回收
```

**内存下不去的排查套路**：

```python
from ray.util import state
for obj in state.list_objects():
    # ⚠️ 字段名是 object_size（不是 size）；写错会直接 KeyError
    # ⚠️ 这里没有 refs 字段 —— 判断「谁还引用着」要看 reference_type
    if obj["reference_type"] == "LOCAL_REFERENCE":
        print(obj["object_id"], obj["object_size"], obj["call_site"])
```

`call_site`（创建这个 ref 的那行代码）通常一眼就能定位泄漏点。

### 三个常见泄漏

```python
# ① 把 ref 塞进全局容器忘了清
CACHE[task_id] = f.remote(x)          # ✗ 一直涨

# ② 循环里只 append 不消费
pending = []
for x in items:
    pending.append(f.remote(x))       # ✗ 10 万条之后对象存储爆
    # ✓ 正确:定期 ray.wait(...) 收一批

# ③ 任务的默认参数持有大对象(闭包捕获)
@ray.remote
def f(x, ctx=build_huge_context()):    # ✗ 每次提交都序列化一份
    ...
```

---

## 5.6 提交模式的反模式清单

| 反模式 | 症状 | 正确写法 |
|---|---|---|
| `for r in refs: ray.get(r)` | 串行化，并行度被压平 | `ray.get(refs)` 一次取 |
| 一次性提交 100 万个任务 | 调度队列/对象存储爆 | `ray.wait` 背压（5.7） |
| 每个任务重算一遍全局资源 | 启动开销吃满 | actor 常驻 |
| 任务粒度太小（< 1ms） | 调度开销 > 计算开销 | 批量处理（一个任务处理一批） |
| 用 `num_cpus=0` 偷偷超卖 | 看起来没事，压测时内存/线程爆炸 | 明确资源需求 |
| 把大对象当参数传 | 序列化 + 拷贝开销 | `ray.put` + 传 ref |

**关于「任务太小」的量化**：Ray Core 一次任务派发有约 1ms 级开销（官方文档口径），
compiled graph 能压到 50µs 级。所以**任务本身至少应该跑几毫秒**，
否则把开销算进去会更慢。

---

## 5.7 `ray.wait` 的三种用法

```python
# ① 背压(最基本):维持「最多 N 个在飞」
MAX_PENDING = 100
pending = []
for item in items:
    while len(pending) >= MAX_PENDING:
        ready, pending = ray.wait(pending, num_returns=1)
        handle(ray.get(ready)[0])
    pending.append(process.remote(item))

# ② 超时:到点就走,处理不完整的批次
ready, not_ready = ray.wait(refs, num_returns=len(refs), timeout=10.0)
if not_ready:
    print(f"{len(not_ready)} 个任务在 10 秒内没完成")

# ③ 流式处理:谁先完成先处理谁(不保证顺序)
pending = list(refs)
while pending:
    ready, pending = ray.wait(pending, num_returns=1)
    results.append(ray.get(ready[0]))
```

⚠️ `num_returns` 的语义容易记反：它是「**至少**有这么多就绪就返回」，
不是「等到恰好这么多」。所以 `ready` 的长度可能超过你要求的数量。

---

## 5.8 取消、超时与「怎么处理卡住的任务」

```python
ref = slow.remote()

ray.get(ref, timeout=30)          # 超时抛 GetTimeoutError(任务还在跑!)
ray.cancel(ref)                   # 只对「还没开始执行」的有效
ray.cancel(ref, force=True)       # 掐掉正在执行它的 worker(影响该 worker 上其它任务)
```

三个实践要点：

1. **`ray.get` 超时不会取消任务**，只是你不等了 —— 任务会继续占用资源；
2. **非 force 的 `ray.cancel` 是「尽力而为」**：已经在跑的任务收不了手；
3. **「卡住」优先看状态而不是猜**：`state.list_tasks()` 一眼就能区分
   「排队」和「在跑但慢」。

---

## 5.9 mini-ray 的对应实现

第 6 章的 mini-ray 把这一章的所有机制都实现了一遍，对照表：

| 本章机制 | mini-ray 的实现位置 |
|---|---|
| 任务状态机 | `scheduler.py` 的 `TaskState`（PENDING/READY/RUNNING/FINISHED/FAILED/CANCELLED） |
| 依赖发现 | `serialization.extract_object_ids()`：扫 pickle 流里的占位符 |
| 依赖唤醒 | `scheduler.py` 的 `_waiting: {oid → 等它的任务}` + `mark_object_ready` |
| `num_returns` | `execution.normalize_results()`（长度不符直接报错） |
| 流式生成器 | `worker.py` 的 `_run_generator` + `ObjectRefGenerator` |
| 传参序列化 | `serialization.py`（cloudpickle-lite + 模块全局的按名 import） |
| 引用计数 | `object_ref.py` + `core_worker.ReferenceCounter`（批量上报） |
| `ray.wait` | `raylet.wait_for_objects()`（条件变量，不是轮询） |
| 取消 | `raylet.cancel_task()`（force 时杀掉 worker） |

---

## 5.10 本章小结

* 任务生命周期有 **14 个状态枚举**（`ray/_private/custom_types.py` 的
  `TASK_STATUS`），**状态本身就是最好的排查线索**：
  `PENDING_NODE_ASSIGNMENT` = 资源不足，`PENDING_ARGS_AVAIL` = 依赖没就绪，
  `PENDING_ARGS_FETCH` = 在跨节点拉数据，
  `RUNNING_IN_RAY_GET` = 你的代码在等别人（可能是死锁）。
* 依赖是**对象级**的，从参数里的 ObjectRef 自动推断；错误会沿依赖链传播。
* `num_returns` 让一个任务产出多个独立对象；`num_returns="streaming"` 是流式生成器，
  适合「边算边产」。**生成器任务会重试**，但语义是「**整任务从头重放**」——
  旧 attempt 的产出靠 `attempt_number` 被丢弃，**不是断点续传**
  （所以官方要求生成器任务必须幂等且确定，详细语义见 §5.3 与第 10 章 §10.2）。
* 序列化是隐藏成本：闭包会复制捕获的对象，大对象应该 `ray.put` 后传引用，
  numpy 有零拷贝特判。
* 对象有 **owner**：owner 死了引用就失效；引用计数决定回收，
  排查内存请用 `state.list_objects()` 看 `call_site`。
* `ray.wait` 是背压的标准工具；`ray.get(timeout=)` 不取消任务。

下一章是全书的重头戏：我们用约 1.1 万行可运行代码，从零实现一个 Ray ——
把这一章和下一章讲的每一个机制都真正写出来，并跑测试验证。
