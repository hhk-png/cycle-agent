仓库地址：https://github.com/hhk-png/cycle-agent

# mini-ray

一个**可运行、可验证**的 Ray 简化实现。纯 Python 标准库 + NumPy,25 个顶层模块
(含 `util/` 与 `_private/` 子包共 35 个文件)、约 1.1 万行代码,把 Ray 的核心机制完整复刻了一遍:

* **任务调度**:依赖图、资源模型、节点亲和、放置组、调度循环
* **对象存储**:Plasma 式的共享内存零拷贝、引用计数、pin、溢出到磁盘
* **Actor**:常驻进程、邮箱、并发组、async actor、重启
* **容错**:任务重试、worker 崩溃恢复、**lineage 重建**
* **可观测性**:State API、Chrome Trace / HTML 甘特图、自定义指标(`util.metrics`)
* **通信原语**:actor 支撑的分布式队列 `util.queue.Queue`(含背压与多消费者)
* **序列化**:cloudpickle-lite(按值传函数/闭包/类)+ 函数注册表 + ref 内联

它是《Ray 教程》第 06 章的配套代码,也是第 22 章(附录 B)的「工程手册」对象。

```python
import miniray as ray

@ray.remote
def square(x):
    return x * x

ray.init(num_cpus=4)
print(ray.get([square.remote(i) for i in range(8)]))
ray.shutdown()
```

## 与真实 Ray 的关系

mini-ray **不是** Ray 的替代品,而是一个「把内部机制摊开给你看」的教学实现。
公共 API 与 Ray 对齐(名字、参数、语义、异常),所以从 Ray 迁移的代码通常
只需要把 `import ray` 换成 `import miniray as ray`。

| | 真实 Ray(2.58) | mini-ray |
|---|---|---|
| 控制面进程 | GCS / raylet / core worker 都是独立进程(C++) | 全在 driver 进程的线程里 |
| 对象传输 | ObjectManager,支持 RDMA / NCCL / GPU Direct | 同节点共享内存,跨节点一次拷贝 |
| 调度器 | 多策略(hybrid/spread/label/topology…),lease 机制 | 单策略(本地性 → 最空闲) |
| 多语言 | Python / Java / C++ | 仅 Python |
| 集群 | `ray start` 起多机,或 KubeRay | driver 进程内自带,`num_nodes` 模拟多节点 |
| 部署 | Dashboard、autoscaler、Jobs API、Serve、Train… | 无(用 State API + timeline 代替) |

**为什么压进一个进程?** 为了「零依赖、可调试、能在一台笔记本上跑完」。
代价写在每一章的「与真实 Ray 的差异」小节里,不含糊。

## 目录结构

```
mini-ray/
├── miniray/
│   ├── __init__.py         # 公共 API(与 ray 对齐)
│   ├── runtime.py          # init / shutdown / 全局状态
│   ├── remote.py           # @miniray.remote、RemoteFunction、.options()/.bind()
│   ├── actor.py            # ActorClass / ActorHandle / ActorMethod
│   ├── object_ref.py       # ObjectRef / ObjectRefGenerator / 引用计数
│   ├── raylet.py           # ⭐ 本地调度器 + 对象管理 + worker 池 + actor + 血缘
│   ├── scheduler.py        # 资源模型、依赖图、调度决策(纯逻辑,可单测)
│   ├── worker.py           # worker 进程:任务循环 + actor 运行时
│   ├── worker_pool.py      # worker 进程池(按需拉起/复用/退休/回收)
│   ├── core_worker.py      # 每个进程的「Ray 客户端」
│   ├── raylet_client.py    # raylet 的 RPC 封装
│   ├── gcs.py              # 集群元数据服务(节点/actor/函数/对象目录/放置组)
│   ├── object_store.py     # ⭐ Plasma 式对象存储(共享内存/零拷贝/溢出/LRU)
│   ├── serialization.py    # ⭐ cloudpickle-lite + 函数表 + ref 内联
│   ├── function_manager.py # 函数导出/取回(GCS 函数表 + 本地缓存)
│   ├── execution.py        # 任务执行公共逻辑(worker 与 local_mode 共用)
│   ├── placement_group.py  # 放置组
│   ├── scheduling_strategies.py  # 节点亲和 / 放置组策略
│   ├── state.py            # State API
│   ├── _timeline.py        # timeline(Chrome Trace + HTML 甘特图)
│   ├── rpc.py              # 极简 RPC(TCP + 长度前缀 + pickle)
│   ├── ids.py / errors.py  # ID 体系 / 异常体系
│   ├── util/               # ray.util 兼容命名空间(ActorPool / Queue / metrics / state / 策略)
│   ├── _private/           # 故障注入等内部接口
│   └── cli.py              # python -m miniray status|demo|version
├── tests/                  # 192 个测试(见下)
├── examples/               # 12 个可运行示例(对应教程章节)
└── tools/
    └── check_docs.py       # ⭐ 教程文档结构自检(与 mini-ray 代码无关)
```

> **`tools/check_docs.py` 是什么**：它不是 mini-ray 的一部分,
> 而是**教程正文的一致性自检脚本** —— 检查章号连续、跨章引用不悬空、
> §小节引用存在、本地链接可达、代码围栏闭合、表格列数一致、自述数字唯一。
> 放在这里是因为教程正文与 mini-ray 同处一个仓库,
> 而这个脚本需要一个"能跑 Python"的地方:
>
> ```bash
> python mini-ray/tools/check_docs.py    # 或 cd mini-ray && python tools/check_docs.py
> ```
>
> 它**只读** `.md` 文件,不碰 mini-ray 的代码。放在这个目录还有一个原因:
> **它让 README 里那句「文档结构自检」从"声明"变成"可复现的命令"** ——
> 这正是教程第 20 章 §0.6 那条态度的直接应用。

## 快速开始

```bash
# 依赖:Python >= 3.10 + numpy(仅此而已)
pip install -e .[test]          # 或者什么都不装,示例里有 sys.path 引导

# 1) 跑测试(约 9 分钟,192 个用例)
python -m pytest tests/ -q

# 2) 跑示例
python examples/01_hello_ray.py
python examples/06_fault_tolerance.py    # 看 lineage 重建
python examples/10_parameter_server.py   # 参数服务器骨架
python examples/12_queue_pipeline.py     # 生产者/消费者 + 背压

# 3) 命令行
python -m miniray demo                   # 内置演示
python -m miniray status --num-cpus 8    # 打印资源/worker 视图

# 4) 教程文档自检(检查 .md 的一致性,与上面的 python 代码无关)
python tools/check_docs.py               # 10 项检查,退出码 0 = 全通过
```

## 特性清单

| 能力 | API | 状态 |
|---|---|---|
| 远程函数 | `@miniray.remote` / `.options()` / `.bind()` | ✅ |
| 取值 | `ray.get`(支持嵌套结构)/ `ray.put` / `ray.wait` | ✅ |
| 取消 | `ray.cancel(ref, force=True)` | ✅ |
| Actor | `@miniray.remote` 装饰类、`max_concurrency`、`concurrency_groups`、async actor | ✅ |
| Actor 方法 | `.remote()` / `.options()` / `.bind()` | ✅ |
| Actor 生命周期 | `ray.get_actor` / `ray.list_actors` / `ray.kill` / `max_restarts` | ✅ |
| 流式生成器 | `num_returns="dynamic"` + `ObjectRefGenerator` | ✅（⚠️ **字面量是 `"dynamic"`** —— 真实 Ray 用 `"streaming"`，这是 mini-ray 保留旧名的**刻意差异**，见「语义差异」一节） |
| 对象存储 | 共享内存零拷贝、只读视图、pin、引用计数、LRU 溢出 | ✅ |
| 调度 | 资源模型、节点亲和、放置组(PACK/SPREAD/STRICT_*) | ✅ |
| 容错 | 重试、worker 崩溃、**lineage 重建**、actor 重启 | ✅ |
| 可观测性 | `miniray.state` / `miniray.timeline` / `get_runtime_context` | ✅ |
| 调试 | `local_mode=True` | ✅ |
| 工具 | `util.ActorPool`、`runtime_env.env_vars`、自定义序列化器 | ✅ |

**明确不做的**(诚实清单):dashboard、autoscaler、Jobs API、跨机部署、
Ray Data/Train/Tune/Serve/RLlib 这些上层库、C++/Java 语言绑定、
多租户与鉴权、GPU 显存管理(只做调度层面)、NCCL/RDMA 数据面。

## 验证状态

```
$ python -m pytest tests/ -q
192 passed
```

测试覆盖(每个文件对应一类语义):

| 文件 | 覆盖 |
|---|---|
| `test_serialization.py` | 按值序列化(`__main__`/闭包/lambda/递归函数/类)、ref 内联、自定义序列化器 |
| `test_object_store.py` | 分桶分配器、跨进程零拷贝、只读语义、引用计数回收、溢出与恢复、pin 保护 |
| `test_core.py` | 任务、依赖、嵌套取值、num_returns、异常与重试、`ray.wait` 背压、取消、`local_mode` 之外的调度语义 |
| `test_actors.py` | 状态、顺序、并发度、并发组、async actor、重启、命名 actor、资源持有 |
| `test_scheduling.py` | 多节点资源视图、节点亲和、放置组四种策略、资源预留与释放 |
| `test_fault_tolerance.py` | worker 崩溃重试、lineage 重建(含依赖链递归重建)、`ObjectLostError` |
| `test_observability.py` | State API、资源视图、timeline JSON/HTML、worker 池视图 |
| `test_local_mode.py` | 同进程执行、依赖、异常、actor |
| `test_util.py` | ActorPool、GPU 分配、自定义资源、对象溢出、生命周期、命名空间 |
| `test_queue_metrics.py` | 队列 FIFO/阻塞/超时/多消费者不重不漏/背压上界/`shutdown`；指标 `Counter`/`Gauge`/`Histogram` 的桶、分位数、标签校验、线程安全、actor 内打点 |
| `test_regressions.py` | **第六、七两轮代码审计抓到的 19 个缺陷**（第六轮 10 个 + 第七轮 9 个）：GPU actor 的创建与卡分配、actor 的 `memory`/`runtime_env`、未声明的并发组要报错、async actor + 并发组、生成器成功/失败/不覆盖已交付数据、依赖异常不被当参数、`set_ref_count`、资源归还幂等 |
| `test_regressions_r8.py` | **第八轮抓到的 10 个缺陷**（在 **174 个测试全绿**时依然存在）。用例名带 `test_fN_` 前缀，与缺陷编号一一对应：`local_mode` 慢任务/慢生成器只执行一次、GPU 放置组任务跑得起来、actor 死亡时在飞与排队调用都报错而不挂起、actor 的 `num_returns` 以调用时为准且不符时显式报错、错误对象存在生产它的节点、actor 的节点亲和与 PG 策略真的生效、`local_mode` 下 `runtime_env`/`gpu_ids` 生效且不污染 driver、带并发组的 async actor 不串行、`ray.cancel(actor 方法 ref)` 不再是空操作、引用计数上报失败时报出真实异常 |

另外 `examples/` 下的 **12 个示例**全部可运行(每个都验证过输出;
第七轮的记录见 `.verify_r7_final.txt` 与 `.verify_r7_a.txt`,
第六轮 `.verify_r6_final.txt` / `.verify_round6.txt`,
更早几轮的记录留在 `.verify.out` / `.verify_round5.txt`)。

> ⚠️ **第七轮修掉了几个「代码和文档说得不一样」的行为** —— 也就是说，
> 修复是让代码回到本文档与教程原本声称的语义，不是改文档：
> `local_mode` 现在**每个任务只执行一次**（此前在 driver 里执行一次后，
> 又会被调度器派给子进程再执行一次，副作用翻倍而返回值看起来完全正常）；
> `local_mode` 下**失败的任务不再被重试到子进程里**（此前会被调 5 次）；
> `ray.cancel(生成器 chunk)` 现在**真的生效**（此前静默什么都不做）；
> 排队等到资源的 GPU actor 现在**拿得到自己的 `gpu_ids`**。
> 这些路径此前**一个测试都没有**，现已由 `test_regressions.py` 的 26 个用例覆盖。
>
> ⚠️ **第八轮的 10 个缺陷也是同一类**（见 `test_regressions_r8.py`）——
> 其中 5 个是**第六、七轮修过的那类缺陷在另一条实现路径上的复制**。
> 最有代表性的两个：`local_mode` 下**慢任务会被执行两次**
> （第七轮加的闸门只挡住了"短于调度 tick"的任务），
> 以及 actor 的 `scheduling_strategy` / `placement_group` **被静默丢弃**
> （现有测试只断言"两个 actor 在同一节点"，而默认调度恰好也满足）。
> **"同一能力有几条实现路径"不是一次性作业。**

## 已知取舍

1. **控制面单点**:raylet/GCS 在 driver 进程里,driver 退出 = 集群结束。
   真实 Ray 的 driver 退出后集群仍在(只是 job 结束)。
2. **没有跨机**:`num_nodes` 是模拟参数,用来观察调度/放置组语义。
3. **对象回收偏保守**:只在内存压力下回收 refcount=0 的对象(真实 Ray 在引用归零后由批量上报触发回收,同样不是瞬时,见教程第 07 章 §7.5)。
   换来的是更少的「对象被提前回收」类事故。
4. **序列化层的循环引用**用「延迟引用 + 载入后回填」实现,与 cloudpickle 的
   state 通道方案不同(原理见 `serialization.py` 的长注释)。极端情况下
   (函数出现在自己 globals 里的深层容器内)你会拿到一个**可调用的代理对象**,
   而不是函数本身 —— 调用、取属性都对,`is` 判断不成立。
5. **`detached` actor 只在同一进程生命周期内有效**(因为没有独立的 raylet 进程)。
6. **worker 日志直接继承终端**(Ray 会转发并加 `(pid)` 前缀)。
7. 卸载/清理:worker 是 `subprocess` 拉起的普通进程,driver 正常退出会关停它们;
   driver 被 `kill -9` 时,worker 会在 1 秒内因 RPC 断开而自己退出。

## ⚠️ 与真实 Ray 的**语义**差异(不只是缺功能)

上面是"没实现什么",下面这几条是"**实现了但语义不同**"——
它们更危险,因为代码能跑,结果却和真实 Ray 不一样:

| 项 | 真实 Ray | mini-ray | 影响 |
|---|---|---|---|
| **应用异常重试** | **默认不重试**(`max_retries` 只管 worker 崩溃);要开 `retry_exceptions` | `max_retries=3` 对**任何**失败都重试(共 4 次尝试) | 在 mini-ray 上"靠重试扛过偶发异常"的代码,搬到真实 Ray 会**直接失败** |
| **生成器任务重试** | 支持重试,但语义是"**整个任务从头重放**"——旧 attempt 的产出靠 `attempt_number` 被丢弃,所以**要求生成器函数幂等且确定性** | 要求 `max_retries=0`,**不支持**重试 | 依赖流式重放的代码在 mini-ray 上跑不了 |
| **`call_site`** | 需 `RAY_record_ref_creation_sites=1` 才记录 | **不记录**：`ObjectRef._call_site` 字段存在但**恒为空串**（无任何调用方传值） | ⚠️ **本条早先写反了**（写成"总是记录"）。想知道"对象是谁产的"只能用 `list_objects()` 的 **`produced_by`**（产出它的 task_id），**拿不到源码位置** |
| **`num_returns` 的字面量** | `"streaming"`（`"dynamic"` 是**已弃用的旧名**），且生成器任务**默认就是** streaming，不必显式声明 | **只认 `"dynamic"`**（保留旧名），且**要求显式声明** | 从 mini-ray 抄到真实 Ray 会 `ValueError`；反过来也一样。**两处都要改** |
| **`OwnerDiedError`** | 存在，与 `ObjectLostError` 语义相反（不可重建 vs 可重建） | **不存在**（没有独立的 owner 进程概念） | `except miniray.OwnerDiedError` 会 `AttributeError` |
| **actor 的资源语义** | **分两种情况**：没写资源 → actor 终身占 **0** CPU、**每次方法调用**占 1 CPU；写了资源 → **终身持有**，方法调用占 0 | 只有一种：actor 的资源**一律终身持有**，且**不为方法调用扣 CPU**（默认 1 CPU） | 按 mini-ray 估算的 actor 容量，搬到真实 Ray 上会**偏保守**（真实 Ray 默认能起得多得多）。**这是刻意的简化** —— 把“每次方法调用扣 1 CPU”也做对，需要在邮箱派发路径上引入一次资源申请/释放。教程第 09 章 §9.6 有完整对照表 |

> **怎么用这份清单**:如果你在 mini-ray 上验证了某个容错设计,
> **搬到真实 Ray 之前一定要重新确认重试语义** —— 这是两者最容易产生
> "本地全绿、线上全红"的一处。教程第 10 章有对照说明。
