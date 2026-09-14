仓库地址：https://github.com/hhk-png/cycle-agent

# 附录 B：mini-ray 工程手册

> 这是第 6 章的配套工程手册：API 参考、数据约定、配置项、验证矩阵、
> 调试方法与练习。**当工具书用** —— 写到哪查到哪。

---

## B.1 怎么用这份附录

| 你想做的事 | 看哪节 |
|---|---|
| 把 mini-ray 跑起来 | B.2 |
| 查某个 API 的语义 / 和 Ray 的差异 | B.3 |
| 看懂内部的 ID、对象负载、任务消息格式 | B.4 |
| 调参数（容量、worker 池、超时） | B.5 |
| 想知道某个机制在哪实现 | B.6 |
| 想知道「哪些行为被测试验证过」 | B.7 |
| 出问题了怎么查 | B.8 |
| 想知道 `Queue` / `metrics` 是怎么设计出来的 | B.9（走读四个取舍） |
| 给它加个新 API | B.9 |
| 练手 | B.10 |
| 它和真实 Ray 差在哪 | B.11 |

---

## B.2 快速开始

```bash
# 依赖:Python >= 3.10 + numpy(没有别的了)
cd ray-toturial/mini-ray

# 1) 跑测试(约 9 分钟,192 个用例)
python -m pytest tests/ -q

# 2) 跑示例(12 个,每个都可独立运行)
python examples/01_hello_ray.py
python examples/06_fault_tolerance.py     # lineage 重建
python examples/10_parameter_server.py    # 参数服务器
python examples/11_end_to_end.py          # 端到端流水线

# 3) 命令行
python -m miniray --help
python -m miniray demo                     # 内置演示
python -m miniray status --num-cpus 8      # 资源视图
python -m miniray version

# 4) 可选:安装成包(示例里有 sys.path 引导,不装也能跑)
pip install -e .[test]

# 5) 教程文档自检(⚠️ 这不是 mini-ray 的功能,是教程正文的一致性检查)
python tools/check_docs.py                 # 10 项检查,退出码 0 = 全通过
```

> **`tools/check_docs.py` 是什么**：它**只读教程的 `.md` 文件**,
> 和 mini-ray 的 Python 代码没有任何关系 —— 放在这里是因为它需要一个
> 能跑 Python 的地方,而教程正文与 mini-ray 同处一个仓库。
>
> 它检查 10 项：章号连续（00–39）、代码围栏闭合、跨章引用不越界、
> §小节引用不悬空、本地链接可达、**表格列数一致**（正确处理 Markdown 的
> 转义竖线 `\|`）、自述数字一致（测试数/示例数/篇数在全书里只有一个值）、
> **自指计数一致**（「往后/后面/离全书结尾还有 N 章」必须等于真实章号差）、
> **结语位置一致**（「结语」只出现在一章，且所有指向它的说法都指向那一章）。
>
> 最后两项是**第八轮新增**的（脚本 docstring 里注明），针对一类
> **由改动自身引发**的错误：加新章时，前面几章里「结语在哪」「后面还有几章」
> 的说法会同时过期 —— 而它们上一轮刚被核对过。具体口径以脚本 docstring 为准：
> `sed -n '13,26p' mini-ray/tools/check_docs.py`。
>
> **它存在的意义**：教程 README 与 `.verify.out` 里那句「文档侧自检」
> 以前是**一句声明**，读者只能选择相信；现在是一条**能跑、会失败、
> 带退出码**的命令。**把"声明"变成"可验证的断言"** ——
> 这正是教程 §0.6 那条态度的直接应用（详见 README 的第四批修订清单）。

最小可运行片段：

```python
import sys
sys.path.insert(0, "ray-toturial/mini-ray")
import miniray as ray

@ray.remote
def square(x):
    return x * x

ray.init(num_cpus=4)
try:
    print(ray.get([square.remote(i) for i in range(8)]))
finally:
    ray.shutdown()
```

---

## B.3 公共 API 参考

### B.3.1 任务

| API | 语义 | 与 Ray 的差异 |
|---|---|---|
| `@miniray.remote` | 包装函数/类 | 一致 |
| `f.remote(*args, **kwargs)` | 提交任务，返回 `ObjectRef`（`num_returns=1`）或列表 | 一致 |
| `f.options(**opts)` | 返回带新选项的副本 | 一致（支持的键见本节下面的「选项键」一段） |
| `f.bind(*args, **kwargs)` | 绑定部分参数，返回新的 `RemoteFunction`（**仍可 `.remote()`**） | ⚠️ **名字同、语义不同**：真实 Ray 的 `.bind()` 返回 **DAG 节点 `FunctionNode`**，**不能 `.remote()`**（自 Ray 2.0 的 DAG API）。mini-ray 实现的是"参数部分套用"，Ray 没这一档 —— 见第 2 章 §2.2、第 26 章 §26.9 |
| `handle.method.bind(*args, **kwargs)` | 给 **actor 方法**绑定部分参数，返回新 `ActorMethod` | ⚠️ 同上：真实 Ray 返回 `ClassMethodNode`（**不能 `.remote()`**）。`options()` / `bind()` 可链式组合，均返回副本不改原对象 |
| `@miniray.method(concurrency_group=..., num_returns=...)` | actor 方法元数据 | 一致 |
| `miniray.get(refs, timeout=None)` | 取值，支持嵌套结构 | 一致 |
| `miniray.put(value)` | 放入对象存储 | 一致（大 numpy 零拷贝） |
| `miniray.wait(refs, num_returns=1, timeout=None, fetch_local=True)` | 等够 N 个 | 一致（返回扁平列表） |
| `miniray.cancel(ref, force=False, recursive=True)` | 取消任务 | `recursive` 被忽略 |
| 生成器：`@remote(num_returns="dynamic")` + `ObjectRefGenerator` | 流式产出 | ⚠️ **字面量不同**：真实 Ray 用 `"streaming"`（`"dynamic"` 是已弃用的旧名），mini-ray **只认 `"dynamic"`** —— 写 `"streaming"` 会 `ValueError: invalid literal for int()`。另外 mini-ray **要求显式声明**，真实 Ray 的生成器任务默认就是 streaming |

**选项键**（`f.options(...)` / `@remote(...)`）：
`num_cpus`、`num_gpus`、`memory`、`resources`、`max_retries`（默认 3）、
`num_returns`（整数或 `"dynamic"` —— ⚠️ 真实 Ray 用的是 `"streaming"`）、`runtime_env`、`scheduling_strategy`、
`name`、以及兼容用的 `placement_group` / `placement_group_bundle_index`。

### B.3.2 Actor

| API | 语义 | 与 Ray 的差异 |
|---|---|---|
| `@miniray.remote`（装饰类） | 返回 `ActorClass` | 一致 |
| `ActorClass.remote(*args)` | 创建 actor（异步），返回 `ActorHandle` | 一致 |
| `ActorClass.options(**opts)` | 返回副本 | 一致 |
| `handle.method.remote(*args)` | 调用方法（异步） | 一致 |
| `miniray.get_actor(name, namespace=None)` | 按名取句柄 | 一致 |
| `miniray.list_actors()` | 列出 actor 信息 | 返回 dict 列表。⚠️ 真实 Ray 的路径是 **`ray.util.state.list_actors()`**（列全部）或 **`ray.util.list_named_actors()`**（只列命名的）—— **`ray.util.list_actors()` 这个函数不存在**（本书第四轮在附录 A §A.1 与附录 D 都纠正过，附录 B 这里漏改了） |
| `miniray.kill(handle, no_restart=True)` | 杀死 actor | 一致 |
| handle 可作参数传递 | ✅ | 一致 |

**Actor 选项**：`num_cpus`（默认 1）、`num_gpus`、`memory`、`resources`、
`max_restarts`（默认 0）、`max_task_retries`（默认 0）、`max_concurrency`（默认 1；
**async actor 默认 1000**）、`concurrency_groups`、`name`、`namespace`、
`lifetime`（`"non_detached"`/`"detached"`，**detached 只在同进程生命周期内有效**）、
`runtime_env`、`scheduling_strategy`。

### B.3.3 集群与运行时

| API | 说明 |
|---|---|
| `miniray.init(**kwargs)` | 启动本地集群（见 B.5 参数表） |
| `miniray.shutdown()` | 关停（先 worker 后服务） |
| `miniray.is_initialized()` | 是否在运行时里 |
| `miniray.cluster_resources()` / `available_resources()` | 资源视图 |
| `miniray.nodes()` | 节点列表（字段名对齐 `ray.nodes()`） |
| `miniray.get_runtime_context()` | worker/node/job id、namespace |
| `miniray.get_gpu_ids()` | 当前 worker 分到的 GPU 序号 |
| `miniray.get_session_dir()` | 会话目录（worker 配置、溢出文件） |

### B.3.4 调度

| API | 说明 |
|---|---|
| `miniray.util.placement_group(bundles, strategy="PACK", name=None)` | 创建放置组 |
| `pg.ready()` → `ObjectRef` / `pg.wait(timeout)` → `bool` | 等就绪 |
| `pg.id` / `pg.bundle_count` / `pg.state` | 属性 |
| `miniray.util.remove_placement_group(pg)` | 删除并释放资源 |
| `miniray.util.get_placement_group(pg_or_id)` | 查询 |
| `miniray.util.placement_group_table()` | 全部放置组状态 |
| `NodeAffinitySchedulingStrategy(node_id, soft=False)` | 节点亲和（**mini-ray 只实现硬亲和**：`soft=True` 的"资源不够就退回普通调度"未实现。候选为空时调度器抛 `ScheduleError`，但它在 raylet 侧被接住、只写进任务 `error` 字段 —— 调用方看到的是 `ray.get()` **永久挂起**，见附录 A §A.5） |
| `PlacementGroupSchedulingStrategy(pg, placement_group_bundle_index=-1)` | 放进 bundle |
| `scheduling_strategy="SPREAD"` / `"DEFAULT"` | ❌ **字符串策略一律不支持** —— 传了直接抛 `MiniRayError: 不认识的调度策略`（**不是**"记录但不生效"，见 B.11） |

### B.3.5 可观测性

| API | 说明 |
|---|---|
| `miniray.state.list_tasks()` | 任务：状态/耗时/重试/节点/worker |
| `miniray.state.list_objects()` | 对象：`object_id` / `state` / `node_id` / `produced_by` / `refs`（**没有"大小"字段**） |
| `miniray.state.list_actors()` | actor：状态/邮箱/在飞/重启次数 |
| `miniray.state.list_workers()` | worker：pid/状态/已执行任务数 |
| `miniray.state.list_nodes()` | 节点：资源总量/可用/利用率 |
| `miniray.state.summarize_tasks/objects/actors()` | 聚合视图 |
| `miniray.timeline(filename=None, html=None)` | Chrome Trace / HTML 甘特图 |
| `miniray.util.state.*` | 上面 state API 的 Ray 兼容路径 |

### B.3.6 工具

| API | 说明 |
|---|---|
| `miniray.util.ActorPool(actors)` | `map`（有序）/ `map_unordered` / `submit` / `get_next` / `pop_idle` |
| `miniray.util.queue.Queue(maxsize=0, actor_options=None)` | actor 支撑的分布式队列：`put` / `get`（含 `block=` / `timeout=`）/ `qsize` / `empty` / `full` / `shutdown`；`Empty` / `Full` 就是**标准库**的那两个 |
| `miniray.util.metrics.Counter / Gauge / Histogram` | 自定义指标；`Counter.inc(v, tags)` / `Gauge.set(v)` / `Histogram.observe(v)` + `.timer()`（`.timer()` 是 mini-ray 扩展，真实 Ray 没有） |
| `miniray.util.serialization.register_serializer(cls, reducer, deserializer)` | 自定义序列化。⚠️ 与真实 Ray **签名不兼容**：Ray 的是 `register_serializer(cls, *, serializer, deserializer)` —— **关键字限定**，中间那个参数叫 `serializer`（`python/ray/util/serialization.py:7`），照这里的位置参数写法调过去会 `TypeError` |
| `miniray._private.fault_injection.lose_objects(node_id=None)` | 故障注入（测试用） |

---

## B.4 关键数据约定

理解这些格式，就能读懂 raylet 与 worker 之间的每一条消息。

### B.4.1 ID（16 字节）

> ⚠️ **以下是 mini-ray 的布局，不是真实 Ray 的**。真实的 Ray ID 里
> **根本没有类型码字节** —— 按官方 `src/ray/design_docs/id_specification.md`，
> 长度相同的 ID 在二进制上无法区分，靠**上下文**判断它是哪一个类型。
> 长度本身也和这里不同：ActorID **16 字节**、TaskID **24 字节**、
> ObjectID **28 字节**（4 字节 index + 24 字节 TaskID）。
> 所以「首字节写类型码」是 **mini-ray 自己的设计**（`miniray/ids.py` 的 docstring
> 写的是「Ray 在 ID 的首字节写入类型码」，这一句并不成立，别被它带偏）。
> 你能拿它理解"ID 里可以编什么"，
> 但**不要拿它推算真实 Ray 的字节布局**。

```
 0                    12                16
 +---------------------+-----------------+
 |   随机 12 字节       |  类型码 4 字节   |      ← mini-ray 的顺序
 +---------------------+-----------------+
                         ^^^  "TASK" / "OBJ\0" / "ACT\0" / "NODE" / "WRKR" / "JOB\0" / "PGRP" / "FUNC" / "DRVR"
```

* `hex()` 32 字符；`short()` 前 8 字符（日志用）；
* `nil()` 随机部分全零、**类型码保留**（所以 `ObjectID.nil() != TaskID.nil()`）；
* pickle 时通过 `id_from_hex` 还原子类，保证类型不丢。

### B.4.2 对象的三种负载形态

raylet 与 worker 之间流动的「值」有以下形态（`kind` 字段区分）：

| kind | 方向 | 字段 | 含义 |
|---|---|---|---|
| `bytes` | 双向 | `data` | pickle 之后的小对象 |
| `bytes_in_block` | 双向 | `shm`/`offset`/`nbytes` | 字节存在共享内存里（避免一次 socket 拷贝） |
| `ndarray` | raylet→worker | `shm`/`offset`/`nbytes`/`dtype`/`shape`/`readonly` | **零拷贝视图描述符** |
| `ndarray_raw` | raylet→worker | `dtype`/`shape`/`data` | 跨节点传输（必须拷贝） |
| `block` | worker→raylet | —— | 结果已经由 worker 写进共享内存（raylet 无需处理） |
| `error` | worker→raylet | `error: {type, message, traceback}` | 任务失败（raylet 会把它写成结果对象） |

**内存序**：worker 写大结果时先 `allocate_object(size)` 拿一块共享内存，
写完再 `commit_object(...)` 登记 —— 数据只写一次，之后的消费者零拷贝读。

### B.4.3 任务消息（raylet → worker）

```python
{
  "kind": "task",
  "task_id": "...", "name": "square",
  "function": {"function_id": "...", "module": "...", "qualname": "..."},   # 函数描述符(不是函数体)
  "args": b"...",                    # pickle 后的参数(ObjectRef 是占位符)
  "num_returns": 1,
  "deps": ["<object_id_hex>", ...],  # 依赖对象(从 args 的 pickle 流里扫出来的)
  "result_ids": ["...", ...],        # 结果对象 ID(调度时就分配好了)
  "is_generator": False,
  "generator_id": None,
  "runtime_env": {"env_vars": {...}},
  "gpu_ids": [0],                    # → CUDA_VISIBLE_DEVICES
  "actor_id": None,                  # actor 方法调用时非空
  "attempt": 1,
}
```

### B.4.4 worker 配置（父进程 → 子进程）

worker 通过 `python -c` 引导 + **pickle 配置文件**启动（每个 worker 一份，
文件名是 worker_id 的 sha1）：

```python
{
  "raylet_address": "127.0.0.1:port", "gcs_address": "127.0.0.1:port",
  "node_id": "...", "worker_id": "...", "job_id": "...",
  "namespace": "default", "session_dir": "...",
  "worker_type": "task" | "actor", "is_actor_worker": False,
  "sys_path": [...],                 # 恢复父进程的 sys.path
  "actor_spec": {...},               # actor worker 独有:类的描述符 + 创建参数
}
```

### B.4.5 错误负载

```python
{"type": "ValueError", "message": "bad value", "traceback": "...", "function": "square"}
```

**不传异常对象本身**（异常经常不可序列化）。raylet 侧用 `builtins` 里的同名类
重建一个等价的异常，包进 `RayTaskError`（actor 方法则是 `RayActorError`）。

---

## B.5 配置项

### `miniray.init()` 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `num_cpus` | 自动探测（`os.cpu_count()`） | 集群总 CPU（按 `num_nodes` 均分） |
| `num_gpus` | 0 | 总 GPU 数（只做调度与 `CUDA_VISIBLE_DEVICES`） |
| `resources` | `{}` | 自定义资源（均分到各节点） |
| `object_store_memory` | 512MB 与可用内存 30% 的较小值 | 对象存储容量（按节点均分） |
| `local_mode` | `False` | 全部在 driver 进程里执行（调试用；actor 也支持） |
| `num_nodes` | 1 | **模拟**节点数（mini-ray 特有） |
| `max_workers_per_node` | 0（自动 = `max(16, 8×CPU)`） | worker 池上限 |
| `worker_idle_timeout_ms` | 10000 | 空闲 worker 回收时间（0 = 不回收；测试里常设 0） |
| `enable_object_spilling` | `True` | 是否允许溢出到磁盘 |
| `object_spilling_directory` | session 目录下 | 溢出目录 |
| `temp_dir` | 系统临时目录 | 会话目录的父目录 |
| `namespace` | `"default"` | 命名空间 |
| `logging_level` | `INFO` | 支持 int 或字符串 |
| `ignore_reinit_error` | `False` | 重复 init 时忽略 |
| `include_dashboard` | —— | 未实现（会打一条提示） |
| `address` | `None` | 只支持 `None`/`"local"`/`"auto"`（无跨机发现） |

### 环境变量

| 变量 | 作用 |
|---|---|
| `MINIRAY_ALLOW_ZERO_COPY_WRITES=1` | 允许零拷贝视图可写（默认只读；**仅用于实验**） |
| `MINIRAY_ACTOR_ID` | actor worker 进程里由框架设置，`get_runtime_context().get_actor_id()` 会读它 |
| `PYTHONIOENCODING=utf-8` | Windows 上跑中文输出建议设置（否则控制台 GBK 会报错） |

### 内部常量（改这些要小心）

| 位置 | 常量 | 值 | 含义 |
|---|---|---|---|
| `object_store.py` | `_SHM_THRESHOLD` | 4096 | 小于这个大小不走共享内存 |
| `object_store.py` | `_MIN_BUCKET_SHIFT` / `_MAX_BUCKET_SHIFT` | 12 / 28 | 分配器桶范围（4KB–256MB） |
| `raylet.py` | `_POLL_TIMEOUT` | 1.0s | 长轮询超时（兼心跳） |
| `raylet.py` | `_TICK` | 0.2s | 调度循环兜底 tick |
| `raylet.py` | `_MAX_EVENTS` | 20000 | timeline 事件保留上限 |
| `core_worker.py` | `_REF_FLUSH_INTERVAL` | 0.05s | 引用计数上报间隔 |
| `worker_pool.py` | `_WORKER_BOOTSTRAP` | —— | worker 进程引导代码 |

---

## B.6 模块地图与调用关系

```
                          用户代码
                             │
        ┌────────────────────┼─────────────────────┐
        ▼                    ▼                     ▼
  remote.RemoteFunction  actor.ActorClass    miniray.get/put/wait
        │                    │                     │
        └────────┬───────────┘                     │
                 ▼                                 ▼
          core_worker.CoreWorker ◀──────────── object_ref.ObjectRef
                 │    │                            (引用计数 → ReferenceCounter)
      ┌──────────┘    └──────────┐
      ▼                          ▼
 raylet_client.RayletClient   gcs.GcsClient
      │                          │
      │ RPC                      │ RPC
      ▼                          ▼
┌─────────────────────────────────────────────────────────┐
│ raylet.Raylet(driver 进程内)          gcs.GcsService     │
│  ├ scheduler.TaskScheduler(资源/依赖/放置组)             │
│  ├ object_store.ObjectStore × N(共享内存/零拷贝/溢出)    │
│  ├ worker_pool.WorkerPool(subprocess 池)                │
│  ├ function_manager.FunctionManager(函数表缓存)          │
│  └ _lineage(血缘)+ _actors(actor 运行时)+ _events(timeline)│
└─────────────────────────────────────────────────────────┘
      ▲
      │ RPC(长轮询)
      ▼
 worker.py(worker 进程)
   ├ 任务循环:fetch deps → loads → 调用 → write_object → task_done
   └ actor 运行时:同步线程池 / asyncio 事件循环(并发组)
```

**依赖方向的原则**：`scheduler.py` 不 import 任何 I/O 模块（可单测）；
`execution.py` 被 worker 与 local_mode 共用；`serialization.py` 通过
`register_object_ref_type` 反向注册 `ObjectRef`，避免循环依赖。

---

## B.7 验证矩阵

```bash
python -m pytest tests/ -q      # 192 passed
```

| 文件 | 用例 | 验证的不变量 |
|---|---|---|
| `test_serialization.py` | 7 | 按值序列化（`__main__`/闭包/lambda/递归/自引用类）、跨解释器反序列化、ref 内联、自定义序列化器、错误信息 |
| `test_object_store.py` | 15 | 分桶分配与复用、新段分配、小/大对象路径、**跨进程写穿（零拷贝）**、只读、引用计数回收、溢出/恢复、pin 保护、超大对象报错、stats 形状 |
| `test_core.py` | 30 | 任务/依赖/深链、kwargs、嵌套 get、put/get、零拷贝数组、异常类型与堆栈、重试次数（4 次/1 次）、wait 语义与超时、get 超时、背压模式、取消（含 force）、`bind`、`options`、`runtime_env`、worker 复用、闭包/lambda |
| `test_actors.py` | 19 | 状态持久、进程隔离、顺序保证、异常、创建失败、句柄传参、`max_concurrency`、默认串行、并发组、async 并发、`await ref`、重启、无重启死亡、命名、list、资源持有、方法多返回、options 拷贝、`method.bind()` |
| `test_scheduling.py` | 11 | 多节点视图、资源释放、跨节点分散、节点亲和、PG 四策略、资源预留与释放、bundle 内任务、PG 等待后自动分配、PG 内 actor（**断言 actor 落在 bundle 所在节点** —— 第八轮把旧断言「两个 actor 在同一节点」换掉了，因为默认调度恰好也满足它，**测不出策略被丢弃**）、**PG 任务多轮运行** |
| `test_fault_tolerance.py` | 6 | 崩溃→`WorkerCrashedError`、崩溃后重试成功、lineage 重建、依赖链递归重建、`ray.put` 丢失报错、多返回值失败 |
| `test_observability.py` | 9 | State API 形状、聚合、资源视图、runtime context、timeline JSON、timeline HTML、对象统计、worker 池、actor 邮箱 |
| `test_local_mode.py` | 6 | 同进程执行、依赖、错误、actor、wait、state |
| `test_util.py` | 15 | ActorPool（有序/无序/背压/校验）、GPU 分配、自定义资源、溢出、超大对象、零拷贝跨 worker、生命周期、命名空间、util 命名空间、ref 传参 |
| `test_queue_metrics.py` | 30 | **队列**：FIFO、`qsize`/`empty`/`full`、`get/put` 的 `block=False` 与 `timeout`、`Empty`/`Full` 与**标准库同类**、空队列阻塞后被生产者唤醒、队列当参数传、多消费者**不重不漏**、背压上界、`shutdown` 杀 actor、`repr`。**指标**：`Counter` 累加与标签、未声明标签报错、`Gauge` 覆盖语义、`Histogram` 分桶与分位数插值、`timer()` 毫秒与异常路径、名字校验、重复注册报错、`snapshot`、**8 线程并发不丢计数**、任务与 actor 内打点 |
| `test_regressions.py` | 26 | **回归**（第六轮代码审计抓到的 10 个缺陷，加上第七轮抓到的 9 个「同一缺陷在另一条实现路径上的复制」 —— 它们在「148 个测试全绿」时依然存在）：GPU actor 创建与设备分配、actor 的 `memory`/`runtime_env` 不被静默丢弃、未知并发组**报错而不是静默**、async actor + 并发组、生成器成功路径/惰性/失败**报错而不是挂起**/失败**不覆盖已投递的 chunk**、依赖错误不当作值传下去、`ObjectStore.set_ref_count` 存在且往返、引用计数上报、`available` 永不超过 `total`、`release` 幂等与重分配复位 |
| `test_regressions_r8.py` | 18 | **第八轮回归**（10 个缺陷，在「**174 个测试全绿**」时依然存在 —— 用例名带 `test_fN_` 前缀，与缺陷编号一一对应）：**F1** `local_mode` 慢任务/慢生成器只执行一次 + 调度器两道闸门各自成立、**F2** GPU 放置组任务跑得起来且 bundle 账目归还、**F3** actor 死亡时在飞调用**与邮箱中排队**的调用都抛 `ActorDiedError`（不是挂起）、**F4** 调用时 `options(num_returns=N)` 生效且真不符时**显式报错**、**F5** 错误对象存在**生产它的那个节点**上、**F6** actor 的节点亲和与 PG 策略**真的生效**、**F7** `local_mode` 下 `runtime_env`/`gpu_ids` 生效且**不污染 driver 环境**、**F8** 带并发组的 async actor **不串行**、**F9** `ray.cancel(actor 方法 ref)` **不再是空操作**、**F10** 引用计数上报失败时**报出真实异常**且链路真的送到 raylet |

**三个「必须真跨进程才算数」的测试**（这是 mini-ray 验证的核心）：

1. `test_ndarray_cross_process_zero_copy`：子进程写数组 → 父进程看到变化；
2. `tests/_ser_roundtrip.py`：脚本里定义的函数/类/闭包 → 在**新的解释器**里执行；
3. `test_lineage_reconstruction`：丢光对象 → `ray.get` 依然返回正确值。

---

## B.8 调试与排查

### 出问题时的第一件事

```python
from miniray import state
print(state.summarize_tasks())      # 有没有失败/重试
print(state.summarize_objects())    # 对象存储有没有满/溢出
print(state.list_workers())         # worker 池状态
print(ray.available_resources())    # 资源
```

### 症状 → 原因对照

| 症状 | 最可能的原因 | 怎么确认 |
|---|---|---|
| 任务一直 PENDING | 依赖未就绪 / 资源不足 / worker 池满 | `state.list_tasks()` 的状态字段；日志里有 worker 池告警 |
| actor 创建后没反应 | **actor 终身占用资源，占满了** | 日志里 10 秒后会打「actor 已等待资源」告警 |
| `ray.get` 超时 | 上游失败 / 对象丢失 | `state.list_objects()` 找 PENDING/RECONSTRUCTING 的对象 |
| `ObjectLostError` | 对象没有血缘（`ray.put` 的对象）或重建失败 | 日志里的「取不到」告警会给出状态与生产者任务 |
| 重建很慢 | 依赖链很长，或重建任务排不上队 | 日志里「开始重建」「重建任务已提交」的成对出现 |
| `ObjectStoreFullError` | 容量太小或有对象被 pin 住 | `state.list_objects()` 看 `refs > 0`；**`num_pinned` 不在 `list_objects()` 里**，要去 `state.get_state()["object_store"]["per_node"][node]["num_pinned"]`（即 `ObjectStore.stats()`） |
| 内存不下降 | 引用泄漏（某个容器一直拿着 ref） | `state.list_objects()` 里 `refs > 0` 的对象 |
| 并发上不去 | 0 CPU 任务受 worker 池限制 | 调大 `max_workers_per_node` |

### 打开详细日志

```python
ray.init(logging_level="debug")     # raylet 的调度/重建/告警都会打出来
```

关键日志（`miniray.raylet`）：`actor 已等待资源 N 秒`、
`worker 池已达上限`、`开始重建对象`、`重建任务已提交`、`对象 ... 取不到`。

### 看时间线

```python
ray.timeline("timeline.json", html="timeline.html")
```

HTML 里能直接看出：任务是否并行、有没有空隙、哪个 worker 忙。

---

## B.9 走读：`util/queue.py` 的四个设计取舍

`miniray.util.queue.Queue` 是一个**完整实现**（不是骨架），
它把 Ray 的 `ray.util.queue.Queue` 语义对齐了：`maxsize` 阻塞、
`qsize` / `empty` / `full` / `shutdown` / `__len__`。
⚠️ **公开的 `Queue` 类上没有 `put_nowait` / `get_nowait`** ——
那两个名字只存在于内部的 `_QueueActor` 上（本书早先的对照表把它们写成了公开 API，已修正）。
要非阻塞语义请用 `put(item, block=False)` / `get(block=False)`。
源码 270 行上下（含 docstring），但里面有**四个值得单独讲的取舍** ——
它们全都是「分布式编程里反复出现的那几类问题」的实例。

### 取舍一：队列体是一个 **async actor**

```
普通 actor（max_concurrency=1）           async actor（max_concurrency=1000）
  消费者 A ─┐                               消费者 A ─┐
  消费者 B ─┼─► 排队 ─► 一个一个执行           消费者 B ─┼─► 同时挂在队列上等
  消费者 C ─┘   （B、C 白等）                 消费者 C ─┘
```

如果队列是一个**同步** actor，那么「A 在等一个还没来的元素」这件事会把整个
actor 堵死 —— B 和 C 根本进不来。结果就是「多消费者」形同虚设。

```python
@remote
class _QueueActor:
    async def get(self):
        return await self._ensure().get()     # 挂起,但不占住 actor
    async def put(self, item):
        await self._ensure().put(item)
```

Ray 的 Queue 也是这个思路（它也是个 async actor）。

### 取舍二：**延迟创建** `asyncio.Queue`

```python
def __init__(self, maxsize=0):
    self._maxsize = maxsize
    self._queue = None            # ← 故意不在这里创建

def _ensure(self):
    if self._queue is None:
        self._queue = asyncio.Queue(self._maxsize)   # 第一次用到时才建
    return self._queue
```

**为什么要这么绕？** 因为 actor 的 `__init__` 在**事件循环启动之前**执行，
而 `asyncio.Queue` 会在第一次 `put`/`get` 时绑定到「当时正在跑的那个 loop」。
在 `__init__` 里创建，在某些 Python 版本上会撞到
`got Future attached to a different loop`。

> **这是第 6 章「12 个坑」里的第 13 个** —— 它是在加这个模块时才发现的，
> 所以不在原来那 12 个里。规律是一样的：**跨进程/跨循环的初始化时机，
> 永远比看起来更麻烦。**

### 取舍三：**异常在 driver 侧抛，不在 actor 里抛**

```python
# actor 侧:返回状态,不抛异常
async def get_nowait(self):
    if queue.empty():
        return (False, None)          # ← 不 raise
    return (True, queue.get_nowait())

# driver 侧:由 API 边界负责抛
def get(self, block=True, timeout=None):
    if not block:
        ok, item = ray_get(self._actor.get_nowait.remote())
        if not ok:
            raise Empty               # ← 标准库的 queue.Empty
        return item
```

**为什么绕这一圈？** 因为**异常一旦从 actor 内部抛出，框架会把它包成
`RayActorError`**（第 2 章 §2.8：Ray 和 mini-ray 都是这个行为）。
那样调用方就只能写 `except RayActorError` 再拆包，
而不能写符合直觉的 `except queue.Empty`。

**把判定挪到 driver 侧，API 边界上的异常类型才是干净的。**
这是个通用原则：**框架内部包了什么异常，不该泄漏到你的 API 表面上。**

### 取舍四：`maxsize` 是**真的**（背压的载体）

```python
async def put(self, item):
    await self._ensure().put(item)    # 队列满 → 挂起,直到有人取走
```

这一行的价值在于：**它让「生产者太快」变成「生产者慢下来」，
而不是「内存涨上去」。** 实测见 `examples/12_queue_pipeline.py`
的第二节：生产者想塞 40 个、`maxsize=8`，它会在第 9 个处**卡住**，
`qsize()` 稳定在 8，直到消费者开始取。

> ⚠️ **顺带一个真实踩到的死锁**：队列自己是个 actor，**mini-ray 里默认占 1 个 CPU**
> （⚠️ **真实 Ray 的默认值不一样** —— 见下面的注）。
> 在 4 核机器上，如果 4 个消费者 + 1 个生产者都要 CPU，
> 生产者会因为**排不上队**而永远不执行 —— 而消费者在等生产者喂数据。
> 修法是给队列传 `actor_options={"num_cpus": 0}`（第 8 章 §8.2 的 0-CPU 模式）。
> 这个坑就写在 `examples/12_queue_pipeline.py` 的文档字符串里。
>
> > **⚠️ 不要在真实 Ray 上照抄「默认占 1 个 CPU」这个前提。**
> > 真实 Ray 的 actor 资源语义**分两种情况**（第 9 章 §9.6 有完整对照表）：
> > **完全没写资源**时，actor 终身占 **0** 个 CPU，而**每次方法调用**占 1 个；
> > **写了资源**时才是终身持有。
> > 对这段死锁来说，**结论不受影响**（队列的方法调用确实要吃 CPU，
> > 该用 `num_cpus=0` 还是要用），但**你估算「能起几个 actor」时会差很多**。
> > mini-ray 只实现了"终身持有"这一种 —— 这是它**刻意的简化**。

### 想自己加一个 API？流程还是那五步

1. **想清楚语义**（阻塞？超时？异常类型？与标准库对照）；
2. **用现有能力做原型**（先别改框架）；
3. **找到原型的硬伤**（轮询？死锁？异常泄漏？）——上面四个取舍都是这么来的；
4. **补测试**：`tests/test_queue_metrics.py` 里有 30 个用例，
   把「满/空/超时/多消费者/不重不漏/背压上界」都钉住了；
5. **补示例与文档**（`examples/12_queue_pipeline.py` + 本节）。

---

## B.10 练习（由浅入深）

1. **热身**：把 `examples/01_hello_ray.py` 里的 `num_cpus` 改成 1，观察耗时变化；
   解释为什么 8 个任务的耗时不是线性的。
2. **改配置**：在 `examples/04_object_store.py` 里把 `object_store_memory`
   改成 16MB，观察溢出计数与耗时；再关掉溢出（`enable_object_spilling=False`），
   观察 `ObjectStoreFullError`。
3. **加 API**：给 `Queue` 加一个**真正的** actor 侧 `shutdown` 语义 ——
   现在的实现是「标记 `_closed` + driver 侧 `ray.kill`」，
   中途挂起的 `get()` 拿到的是 `RayActorError`。把它改成
   所有等待者都收到一个明确的 `RuntimeError('队列已关闭')`，并写测试。
4. **改调度**：先让 `_encode_scheduling_strategy` **接受**字符串 `"SPREAD"` / `"DEFAULT"`
   （现在传了直接抛 `MiniRayError`），再在 `scheduler.pick_node` 里实现真正的 `SPREAD`
   （总是选利用率最低的节点）—— 目前字符串策略**根本不支持**（见 §B.11），
   写测试证明它与默认策略在 2 节点场景下行为不同。
5. **观察 actor 语义**：写一个 actor 把 `max_concurrency` 设为 4，
   在方法里 `time.sleep`，用 `state.list_actors()` 观察 `inflight` 字段
   ——什么时候会 >1？
6. **加指标**：给 `state.summarize_tasks()` 增加 `p50/p99` 耗时统计，并写测试。
7. **优化跨节点传输**：让 `_pull_from_other_node` 把拉过来的对象**缓存**进本地节点
   的对象存储，测「同一个对象拉两次」的第二次不再走网络。
8. **实现背压**：给生成器任务加消费端背压（提示：raylet 知道每个 chunk 的引用计数，
   当未消费的 chunk 超过 N 时暂停投递）。
9. **最难**：把 `raylet.py` 里的「单调度策略」抽成策略模式，
   实现 `hybrid`（阈值 0.5）与 `spread` 两种，并用一条「2 节点 + 6 个任务」的测试
   断言两者的节点分布不同。

---

## B.11 已知限制：与真实 Ray 的完整差异

| 项 | 真实 Ray | mini-ray | 影响 |
|---|---|---|---|
| 控制面 | GCS/raylet 独立进程 | driver 进程内线程 | driver 退出 = 集群结束 |
| 多机 | `ray start` + autoscaler | `num_nodes` 模拟 | 无法验证真实网络行为 |
| 调度策略 | hybrid/spread/label/topology + lease | 单策略（本地性 → 最空闲）；**字符串策略直接报错**，只支持两种策略对象 | 调度行为是简化近似 |
| 对象回收 | refcount 归零后由**批量上报**触发回收（不是瞬时，有 flush 周期） | 内存压力下才回收 | mini-ray 内存占用偏高 |
| 溢出 | 独立 IO 池 + mmap 读回 + 可选外部存储 | 同步溢出，恢复读回内存 | 大对象溢出时延更高 |
| 序列化 | cloudpickle（含 module/generator/sliced 等特殊处理） | cloudpickle-lite | 极端场景会退化（见 06 章 6.11） |
| 循环引用 | state 通道（cloudpickle） | 延迟引用 + 回填 | 罕见位置会拿到可调用代理 |
| Actor | detached 跨 job 存活、actor 迁移 | detached 只在本进程生命周期内 | 无法跨 job 共享 actor |
| 并发组 | 线程池/asyncio 任意组合 | 线程数 = 并发度 | 语义一致，实现更朴素 |
| 缺失 | Dashboard、autoscaler、Jobs API、AI 库、多语言、鉴权、GPU 显存管理、NCCL/RDMA | —— | 明确不在范围内 |
| 对象传输 | ObjectManager + RDT + RDMA | 同节点零拷贝、跨节点一次拷贝 | 无 GPU 直传 |

### ⚠️ 更危险的一类差异：**实现了，但语义不同**

上面那张表是「**没实现什么**」——缺了就报错，容易发现。
下面这些是「**实现了，但行为不一样**」——**代码能跑，结果却和真实 Ray 不同**，
这类差异迁移时最容易造成「本地全绿、线上全红」：

| 项 | 真实 Ray | mini-ray | 影响 |
|---|---|---|---|
| **应用异常重试** | **默认不重试**（`max_retries` 只管 worker 崩溃；要开 `retry_exceptions`） | `max_retries=3` 对**任何**失败都重试（共 4 次尝试） | 在 mini-ray 上「靠重试扛过偶发异常」的代码，搬到真实 Ray **会直接失败** |
| **生成器任务重试** | **支持**重试，语义是「**整个任务从头重放**」（旧 attempt 的产出按 `attempt_number` 丢弃，**不是断点续传**，所以要求生成器幂等） | **不支持**重试：调度时把 `max_retries` **静默强制为 0**（传 `max_retries=3` 也**不报错**，只是被忽略） | 依赖流式重放的代码在 mini-ray 上根本跑不了 |
| **`call_site`** | 需 `RAY_record_ref_creation_sites=1` 才记录（`ray memory` / `list_objects()` 里的 call site 列） | **不记录**：`ObjectRef._call_site` 字段存在但**恒为空串**（`miniray/object_ref.py`，无任何调用方传值） | ⚠️ **本书早先写反了**（写成"mini-ray 总是记录"）。实测：`grep -rn "call_site=" --include=*.py .` **0 命中**，`grep -rn "call_site" --include=*.py . \| grep -v object_ref.py` **也 0 命中** —— 全书只有 `ObjectRef.__init__(..., call_site: str = "")` 那一个默认值。所以想知道"对象是谁产的"只能用 `list_objects()` 的 **`produced_by`**（产出它的 task_id），**拿不到源码位置**；真实 Ray 开了那个变量才有源码位置 |
| **`ray.util.metrics`** | 进程内注册，**跨进程由 Prometheus 从各节点 exporter 汇总** | 只有**进程内**注册表，`snapshot()` 看不到别的进程 | 在 worker 里打点，driver 的 `snapshot()` **看不到** —— 要让 worker 把结果带回来 |
| **`Histogram.timer()`** | **不存在**，只有 `observe(v)` | 有 `.timer()` 上下文管理器 | 在 mini-ray 上写的 `with hist.timer():`，搬到真实 Ray **会 `AttributeError`** |

> **怎么用这份清单**：如果你在 mini-ray 上验证过某个容错设计，
> **搬到真实 Ray 之前一定要重新确认重试语义** ——
> 这是两者最容易产生「本地全绿、线上全红」的一处。
> 正文的完整讨论见第 05 章 §5.3、第 10 章 §10.2、第 11 章 §11.2。
>
> 这三条的共同点是：**它们都不会让你写不出代码，只会让你写错语义。**
> 所以「mini-ray 测试全过」不能作为「搬到 Ray 一定没问题」的证据 ——
> 它证明的是**机制理解正确**，不是**语义完全等价**。

**看待这份表的方式**：每一条差异背后都有一个"为什么"。
能把每条讲清楚（因为压进一个进程、因为零依赖、因为没有集群发现……），
说明你已经理解了 Ray 的设计约束 —— 这正是本附录的目的。
而上面这张「语义差异」表还要多做一步：**把它当成迁移前的检查清单**。

---

## B.12 小结

* mini-ray 是**可以真的跑起来**的 Ray 简化实现：192 个测试、12 个示例全部通过。
* API 与 Ray 对齐（名字/参数/语义/异常），迁移只需改 import。
* 数据约定（ID 布局、负载形态、任务消息、worker 配置、错误负载）是读懂源码的钥匙。
* 扩展它的方法是**工程方法**：想清语义 → 用现有能力做原型 → 找硬伤 →
  判断是否动底层 → 补测试。
* 差异清单不是"缺陷列表"，而是"设计约束的产物"——读完应该能解释每一条。
