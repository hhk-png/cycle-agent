仓库地址：https://github.com/hhk-png/cycle-agent

# 附录 A：API 速查表

> 本附录目标：一页之内找到"这个功能叫什么、参数是什么、mini-ray 实现到哪一步"。
> **mini-ray 对照列是本表最有价值的部分**——它把第 06 章的配套实现
> （`mini-ray/miniray/__init__.py` 与 `mini-ray/README.md` 的特性清单）
> 逐项对齐到真实 Ray 的 API 上，让你一眼看出"哪些机制你已经从零写过、
> 哪些只能靠真实 Ray"。

**图例**

| 标记 | 含义 |
|---|---|
| ✅ 已实现（同样的名字） | mini-ray 里有同名 API，语义对齐，迁移只需改 import |
| ⚠️ 简化版 | 有这个名字，但参数/语义有删减；细节见"备注" |
| ❌ 未实现 | mini-ray 里没有，只有真实 Ray 提供 |

**版本基准**：真实 Ray 侧以 **2.58** 为准。凡本书未能在官方文档/源码里
核对到的条目，一律标注"**未确认**"，不做猜测。

---

## A.1 Ray Core 顶层 API

### `ray.init()` 常用参数

| 参数 | 含义 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `address` | 连到已有集群（`"auto"`）或本地起 | ⚠️ 简化版 | mini-ray 只接受 `None` / `"local"` / `"auto"` / **`""`**（空串也放行），其余直接报错（没有跨进程集群发现）。判据是 `miniray/runtime.py` 的 `if address not in (None, "local", "auto", ""):` |
| `num_cpus` / `num_gpus` | 本节点的逻辑资源 | ✅ 已实现（同样的名字） | |
| `resources` | 自定义资源（`{"accelerator": 4}`） | ✅ 已实现（同样的名字） | |
| `object_store_memory` | 对象存储容量（字节） | ✅ 已实现（同样的名字） | mini-ray 默认取 `min(512MB, 可用内存×30%)` |
| ~~`local_mode`~~ | 单进程执行（调试用） | ⚠️ **真实 Ray 已移除**，写了会 `RuntimeError`；mini-ray 保留 | 调试请用 Ray Distributed Debugger（第 11 章 §11.7） |
| `ignore_reinit_error` | 重复 init 不报错 | ✅ 已实现（同样的名字） | |
| `logging_level` | 日志级别 | ✅ 已实现（同样的名字） | |
| `namespace` | 命名空间（隔离命名 actor） | ✅ 已实现（同样的名字） | |
| `runtime_env` | 运行环境 | ⚠️ 简化版 | 只支持 `env_vars`；pip/conda/working_dir 不做 |
| `include_dashboard` | 是否起 dashboard | ❌ 未实现 | 传 `True` 只会打印一条提示；用 `miniray.state` + `miniray.timeline` 代替 |
| `_system_config` | 覆盖内部配置 | ❌ 未实现 | 真实 Ray 用它改 `scheduler_*` / `object_spilling_*` 等 |
| `dashboard_host` / `dashboard_port` | dashboard 监听地址 | ❌ 未实现 | 与安全强相关（第 17 章） |
| —— | —— | ⚠️ mini-ray 特有 | `num_nodes`（模拟多节点）、`max_workers_per_node`、`worker_idle_timeout_ms`、`enable_object_spilling`、`object_spilling_directory`、`temp_dir` |

### 集群信息与生命周期

| API | 作用 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `ray.shutdown()` | 关闭运行时 | ✅ 已实现（同样的名字） | |
| `ray.is_initialized()` | 是否已初始化 | ✅ 已实现（同样的名字） | |
| `ray.nodes()` | 节点列表（`NodeID` / `Alive` / `Resources` …） | ✅ 已实现（同样的名字，字段名对齐） | |
| `ray.cluster_resources()` | 集群总资源 | ✅ 已实现（同样的名字） | |
| `ray.available_resources()` | 当前可用资源 | ✅ 已实现（同样的名字） | |
| `ray.get_runtime_context()` | 拿 `worker_id` / `node_id` / `job_id` / `actor_id` / `namespace` | ⚠️ 简化版 | mini-ray 的 `RuntimeContext` 提供同名 getter |
| `ray.get_gpu_ids()` | 当前 worker 分到的 GPU 序号 | ✅ 已实现（同样的名字） | |
| `ray.timeline(filename=...)` | 导出 Chrome Trace | ✅ 已实现（同样的名字） | mini-ray 还能额外输出自包含 HTML 甘特图 |
| `ray.get_actor(name, namespace=...)` | 按名字取命名 actor | ✅ 已实现（同样的名字） | 找不到时抛 `ValueError` |
| `ray.kill(actor, no_restart=True)` | 杀掉 actor | ✅ 已实现（同样的名字） | |
| `ray.util.state.list_actors()` | 列出所有 actor（列命名 actor 用 `ray.util.list_named_actors()`） | ⚠️ 简化版 | ⚠️ **`ray.list_actors` 与 `ray.util.list_actors` 都不存在**；mini-ray 除了**顶层** `miniray.list_actors()`，也同时暴露了 Ray 兼容的两个路径 **`miniray.state.list_actors()`** 与 **`miniray.util.state.list_actors()`**（见 §A.8） |

---

## A.2 任务：`@ray.remote` / `.options()` / `.bind()`

### `@ray.remote(...)` 与 `.options(...)` 的 task 选项

| 选项 | 含义 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `num_cpus` | 逻辑 CPU（支持小数，如 `0.5`） | ✅ 已实现（同样的名字） | |
| `num_gpus` | 逻辑 GPU | ✅ 已实现（同样的名字） | 只做调度层面，不含显存管理 |
| `memory` | 堆内存声明 | ✅ 已实现（同样的名字） | mini-ray 记录该声明并参与调度 |
| `resources` | 自定义资源 | ✅ 已实现（同样的名字） | |
| `max_retries` | 失败重试次数（不含系统错误） | ✅ 已实现（同样的名字） | |
| `num_returns` | 返回对象个数；**`"streaming"`** 表示流式生成器（`"dynamic"` 是**已弃用的旧名**） | ⚠️ **字面量不同**：mini-ray 只认 **`"dynamic"`**（见下） | 真实 Ray：生成器任务**默认就是** `"streaming"`，不必显式声明。⚠️ **mini-ray 两处都不同**：① **要求显式声明**；② **认的字面量是 `"dynamic"`** —— 写 `"streaming"` 会 `ValueError: invalid literal for int()`。**迁移时这两条都要反过来改** |
| `runtime_env` | 任务级运行环境 | ⚠️ 简化版 | 只支持 `env_vars` |
| `scheduling_strategy` | 调度策略对象 | ✅ 已实现（同样的名字） | |
| `name` | 任务名（出现在 State API 里） | ✅ 已实现（同样的名字） | 默认取函数名 |
| `placement_group` / `placement_group_bundle_index` | **旧写法**，等价于 `PlacementGroupSchedulingStrategy` | ✅ 已实现（同样的名字） | 真实 Ray 已弃用该写法，推荐 `scheduling_strategy` |
| `max_calls` | worker 执行多少次后被回收（缓解内存泄漏） | ❌ 未实现 | |
| `retry_exceptions` | 哪些异常算"可重试" | ❌ 未实现 | |
| `accelerator_type` | 加速器型号约束 | ❌ 未实现 | |
| `concurrency_groups` | 仅 actor 有 | —— | 见 A.4 |

### 调用形式

| 写法 | 作用 | mini-ray 对照 |
|---|---|---|
| `f.remote(*args)` | 提交任务，立刻返回 `ObjectRef` | ✅ 已实现（同样的名字） |
| `f.options(num_cpus=2).remote(...)` | 运行期改选项（返回副本，不改原对象） | ✅ 已实现（同样的名字） |
| `f.bind(*args)` | **只有一种语义**：返回 **DAG 节点 `FunctionNode`**（**自 Ray 2.0** 的 DAG API 就有），**不能 `.remote()`** —— 抛 `AttributeError: .remote() cannot be used on <class 'ray.dag.function_node.FunctionNode'>`（报错里是**全限定名**，此处按 `dag_node.py:720` 原文）；要执行就 `.execute()`。可以只绑一部分参数（剩下的在 `execute()` 时给），但**返回的仍是节点，不是 `RemoteFunction`** | ⚠️ **语义分叉**：mini-ray 的 `.bind()` 返回 `RemoteFunction`、可以继续 `.remote()`。见第 2 章 §2.2 与第 26 章 §26.2 |
| `f(*args)` | **直接调用会报错**（这是有意的） | ✅ 已实现（同样的名字） |
| `ref = f.remote()` 后 `ray.get(ref)` | 取值 | ✅ 已实现（同样的名字） |

---

## A.3 取值、等待与取消

| API | 作用 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `ray.get(refs, timeout=None)` | 阻塞取值；支持嵌套 list/tuple/dict | ✅ 已实现（同样的名字） | 嵌套结构按原形状返回 |
| `ray.get` 的异常语义 | 远程异常重新抛出为 `RayTaskError`，原始异常在 `.cause` | ✅ 已实现（同样的名字） | 还有 `.traceback_str` / `.as_instanceof_cause()` |
| `ray.put(value)` | 放进对象存储，返回 `ObjectRef` | ✅ 已实现（同样的名字） | 大 numpy 数组走共享内存零拷贝 |
| `ray.wait(refs, num_returns=1, timeout=None, fetch_local=True)` | 返回 `(ready, remaining)` | ✅ 已实现（同样的名字） | 背压的标准工具（第 18 章） |
| `ray.cancel(ref, force=False, recursive=True)` | 取消任务 | ⚠️ 简化版 | `force=False` 只取消未开始的任务，`True` 会杀 worker；**`recursive` 被接受但不生效**（只取消该 ref 对应的那一个 task） |
| `ray.get` 的 `timeout` 语义 | 超时抛异常 | ✅ 已实现（同样的名字） | mini-ray 抛 `GetTimeoutError` |
| `ObjectRefGenerator` | 流式取生成器任务的多个返回值 | ✅ 已实现（同样的名字） | |
| `ray._private.internal_api.free(obj_ref)` | 提前释放对象 | ❌ 未实现 | 内存调试用的高级手段。⚠️ **2.58 已弃用** —— 见 §A.10 |

### 异常体系（`ray.exceptions`）

| 异常 | 何时出现 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `RayTaskError` | 远程任务抛异常 | ✅ 已实现（同样的名字） | |
| `RayActorError` | actor 方法抛异常 | ✅ 已实现（同样的名字） | |
| `WorkerCrashedError` | worker 进程崩溃（段错误 / OOM） | ✅ 已实现（同样的名字） | |
| `ActorDiedError` | actor 死亡且无法重启 | ✅ 已实现（同样的名字） | |
| `TaskCancelledError` | 任务被取消 | ✅ 已实现（同样的名字） | |
| `GetTimeoutError` | `ray.get` 超时 | ✅ 已实现（同样的名字） | |
| `ObjectLostError` | 对象丢失且无法重建 | ✅ 已实现（同样的名字） | |
| `OwnerDiedError` | owner 进程死亡（**不可重建**） | ❌ **未实现** | ⚠️ **它其实是 `ObjectLostError` 的子类**：真实 Ray 里 `class OwnerDiedError(ObjectLostError)`（`exceptions.py:807`），所以 `except ObjectLostError` **能**接住它 —— 区分的依据是**语义**（owner 死了就不会触发 lineage 重建），**不是类型树**。另外**别被上一行的 ✅ 带偏**：mini-ray 实现了 `ObjectLostError`（可重建）与 `ActorDiedError` / `WorkerCrashedError`，但**没有** `OwnerDiedError` —— 它没有"owner 进程"这个概念（控制面都在一个进程里）（第 5/7 章） |
| `ObjectStoreFullError` | 对象存储写满 | ✅ 已实现（同样的名字） | |
| `OutOfMemoryError` | Ray 的内存监控杀掉 worker | ❌ 未实现 | 真实 Ray 2.2+ 有应用级内存监控 |
| `RaySystemError` | 系统级错误 | ✅ 已实现（同样的名字） | |

---

## A.4 Actor

### 创建与选项

| 选项 | 含义 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `num_cpus` / `num_gpus` / `memory` / `resources` | 资源声明 | ✅ 已实现（同样的名字） | **actor 的资源是终身持有的**（第 18 章反模式 9） |
| `max_concurrency` | 方法并发度（sync actor 默认 1；async actor 默认 1000） | ✅ 已实现（同样的名字） | |
| `concurrency_groups` | 并发组：`{"io": 2, "compute": 4}` | ✅ 已实现（同样的名字） | 配合 `@ray.method(concurrency_group="io")` |
| `max_restarts` | 重启次数（默认 0，即不重启） | ✅ 已实现（同样的名字） | 重启**不恢复状态**，`__init__` 会重跑 |
| `max_task_retries` | 单个方法调用的重试次数 | ✅ 已实现（同样的名字） | |
| `name` | 命名 actor | ✅ 已实现（同样的名字） | |
| `namespace` | 命名空间 | ✅ 已实现（同样的名字） | |
| `lifetime` | `"detached"` 让 actor 活过 driver | ⚠️ 简化版 | mini-ray 的 detached actor **只在同一进程生命周期内有效**（没有独立 raylet 进程） |
| `runtime_env` | 运行环境 | ⚠️ 简化版 | 只支持 `env_vars` |
| `scheduling_strategy` | 调度策略 | ✅ 已实现（同样的名字） | |
| `max_pending_calls` | 邮箱积压上限（背压） | ❌ 未实现 | Ray 用它做 actor 级背压 |

### 方法与元数据

| API | 作用 | mini-ray 对照 |
|---|---|---|
| `Handle.method.remote(*args)` | 异步调用，返回 `ObjectRef` | ✅ 已实现（同样的名字） |
| `Handle.method.options(...)` | 方法级选项 | ✅ 已实现（同样的名字） |
| `Handle.method.bind(...)` | 绑定参数（返回新的 `ActorMethod`，仍要 `.remote()`） | ✅ 已实现（同样的名字） |
| `@ray.method(concurrency_group=..., num_returns=...)` | 声明方法元数据 | ✅ 已实现（同样的名字） |
| `handle._actor_id` | actor id | ✅ 已实现（同样的名字） |
| async actor（`async def` 方法） | 单线程事件循环内并发 | ✅ 已实现（同样的名字） |

---

## A.5 调度策略

```python
from ray.util.scheduling_strategies import (
    NodeAffinitySchedulingStrategy,
    PlacementGroupSchedulingStrategy,
)
```

| 策略 | 作用 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `"DEFAULT"`（或 `""`） | 混合调度（hybrid） | ❌ **未实现（传了就报错）** | mini-ray **完全不接受字符串策略**：实跑 `f.options(scheduling_strategy="DEFAULT").remote()` 直接抛 `MiniRayError: 不认识的调度策略: 'DEFAULT'`（`""` 与 `"SPREAD"` 同样）。它只认**策略对象**（`NodeAffinitySchedulingStrategy` / `PlacementGroupSchedulingStrategy`）。⚠️ 这和"记录了但不用"是两回事 —— 后者是本书早先的错误描述 |
| `"SPREAD"` | 尽量铺开 | ❌ **未实现（传了就报错）** | 同上，抛 `MiniRayError: 不认识的调度策略: 'SPREAD'`。⚠️ **但真实 Ray 里这两个字符串是生效的**（`SchedulingType::SPREAD` / `DEFAULT`）—— 这是 **mini-ray 的局限，不是 Ray 的语义**。真正的 SPREAD 在 mini-ray 未实现（附录 B §B.10 把它列为练习 4） |
| `NodeAffinitySchedulingStrategy(node_id, soft)` | 钉在指定节点 | ⚠️ **只实现了硬亲和** | ⚠️ **`soft` 在真实 Ray 里是必填的**（2.58 的签名是 `(node_id, soft, _spill_on_unavailable=False, _fail_on_unavailable=False)`，`soft` **没有默认值**；只有 mini-ray 才给它 `False` 默认值）。⚠️ mini-ray **不实现 `soft=True` 的回退**：它把 `soft` 编码进策略，但 raylet 只取 `node_id`、**丢掉 `soft`**（`raylet.py:543`），调度器于是**无条件硬过滤**（`miniray/scheduler.py` 的 `pick_node`：`record.node_affinity is not None and node_id != record.node_affinity` 这一行直接 `continue`，候选集里根本不会出现别的节点；⚠️ mini-ray 在持续迭代，**行号会漂**，复核请用 `grep -n 'node_id != record.node_affinity' miniray/scheduler.py`）。候选为空时调度器**内部**确实抛 `ScheduleError`，但该异常在 raylet 侧被接住、只写进任务的 `error` 字段（`raylet.py:342`）—— **调用方拿不到它**：任务一直停在 `READY`，`ray.get()` **永久挂起**（实测 25 秒不返回；只有显式传 `timeout=` 才会得到 `GetTimeoutError`）。所以别写 `except ScheduleError`，要靠 `list_tasks()` 的 `error` 字段排查。`soft=True` 的"资源不够就退回普通调度"**只有真实 Ray 有** |
| `...(_spill_on_unavailable=True)` | 资源不足时赶走该节点已有任务腾地方 | ❌ 未实现 | |
| `PlacementGroupSchedulingStrategy(pg, placement_group_bundle_index=-1)` | 放进放置组 | ✅ 已实现（同样的名字） | `-1` = 任意装得下的 bundle |
| `...placement_group_capture_child_tasks=` | 子任务是否继承放置组 | ⚠️ 简化版 | 记录该标志但不实现继承 |
| `NodeLabelSchedulingStrategy` | 按标签/表达式调度（alpha） | ❌ 未实现 | |
| 拓扑感知调度（NVLink 域 / `ray.io/gpu-domain`） | 同域放置 | ❌ 未实现 | 2.56 起支持；2.57 起有公开 API |

---

## A.6 放置组

```python
from ray.util.placement_group import (
    placement_group, remove_placement_group, get_placement_group,
)
```

| API | 作用 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `placement_group(bundles, strategy="PACK", name=None)` | 创建（**异步**：立刻返回，后台重试分配） | ✅ 已实现（同样的名字） | bundle 键名大写：`{"CPU": 1, "GPU": 1}` |
| `strategy` | `PACK` / `SPREAD` / `STRICT_PACK` / `STRICT_SPREAD` | ✅ 已实现（同样的名字） | 四种语义与 Ray 一致 |
| `pg.ready()` | **返回 ObjectRef**（值是 `{bundle 下标: 节点 ID}`） | ✅ 已实现（同样的名字） | 标准写法是 `ray.get(pg.ready())` |
| `pg.wait(timeout_seconds=30)` | 阻塞等待就绪，返回 bool | ⚠️ **参数名不同**：mini-ray 是 `pg.wait(timeout=...)` | ⚠️ **形参名是 `timeout_seconds`、没有 `timeout`**（`python/ray/util/placement_group.py:69`：`def wait(self, timeout_seconds: Union[float, int] = 30) -> bool:`）—— 写 `pg.wait(timeout=5)` 会 **`TypeError: wait() got an unexpected keyword argument 'timeout'`**。mini-ray 的参数名恰恰是 `timeout`（`miniray/placement_group.py` 的 `def wait(self, timeout: Optional[float] = None)`），**两边刚好相反**，迁移时要改 |
| `remove_placement_group(pg)` | 删除并释放预留资源 | ⚠️ 简化版 | Ray 会**连带杀掉**使用该 pg 的 actor/任务；mini-ray 只释放资源 |
| `get_placement_group(name)` | 按**全局名字**取（**不是 id**） | ⚠️ **签名不同**：mini-ray 是 `get_placement_group(placement_group_id)`，按 **id** 取 | ⚠️ Ray 收的是**全局名字**：`def get_placement_group(placement_group_name: str) -> PlacementGroup:`（`python/ray/util/placement_group.py:247`），空串抛 `ValueError("Please supply a non-empty value to get_placement_group")`、查不到抛 `ValueError(f"Failed to look up placement group with name: ...")`（`:257-267`）。mini-ray **恰好相反** —— 它按 id 查（`miniray/placement_group.py` 的 `def get_placement_group(placement_group_id: Any)`）。**按名字建组时记得传 `name=`，否则两边都取不回来** |
| `get_current_placement_group()` | 当前任务所属 pg | ⚠️ 简化版 | mini-ray **总是返回 `None`**（未实现继承） |
| `placement_group_table()` | 所有 pg 的状态 | ✅ 已实现（同样的名字） | |
| `pg.bundle_specs`（**属性**，`ray.util.placement_group.PlacementGroup.bundle_specs`） | 查询 bundle 规格（`List[Dict]`） | ⚠️ 简化版（mini-ray 也叫 `pg.bundle_specs`） | ⚠️ **Ray 里没有 `placement_group_bundles()` 这个函数** —— `grep -rn "def placement_group_bundles" python/` 只命中 `ray/serve/_private/` 里的**方法**（`deployment_state.py`、`test_utils.py`），`ray/util/placement_group.py` 与 `ray/util/__init__.py` 都没有它。bundle 规格要靠 **`PlacementGroup.bundle_specs` 属性**（`python/ray/util/placement_group.py:81`）。mini-ray 提供的正是这个属性名 |

---

## A.7 `runtime_env`

| 字段 | 作用 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `env_vars` | 注入环境变量 | ✅ 已实现（同样的名字） | |
| `pip` | 安装 pip 依赖（可配 `pip_check` / `pip_version`） | ❌ 未实现 | |
| `conda` | conda 环境 | ❌ 未实现 | ⚠️ 真实 Ray 里 **`pip` / `uv` / `conda` 两两互斥**；Windows 上是 **beta/experimental**，不是"不支持" |
| `working_dir` | 打包并上传工作目录 | ❌ 未实现 | |
| `py_modules` | 上传 Python 模块 | ❌ 未实现 | |
| `excludes` | 打包时排除文件 | ❌ 未实现 | 用 `.gitignore` 语法；与 `working_dir` / `py_modules` 搭配 |
| ~~`_set_ray_env_vars`~~ | —— | ⚠️ **本节早先写错了** | 真实 Ray 的 `runtime_env` **没有这个字段**（见附录 F §F.2.2 的字段全表），已从表中移除 |
| `RAY_RUNTIME_ENV_HOOK` | 自定义运行时环境钩子 | ❌ 未实现 | |
| `miniray.util.serialization.register_serializer` | 注册不可 pickle 对象的序列化器 | ⚠️ **名字一样，签名不兼容** | 真实 Ray 的是 `register_serializer(cls, *, serializer, deserializer)` —— **关键字限定**，中间那个参数叫 `serializer`（`python/ray/util/serialization.py:7`）；mini-ray 的是 `register_serializer(cls, reducer, deserializer)`（三个**位置**参数，`miniray/serialization.py:88-92`）。照着 mini-ray 的写法去调真实 Ray 会 `TypeError` |

---

## A.8 State API（可观测性）

```python
from ray.util.state import list_tasks, list_objects, list_actors, summarize_tasks
```

| API | 作用 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `list_tasks()` | 任务列表（状态/耗时/重试次数/错误） | ✅ 已实现（同样的名字） | mini-ray 字段：`task_id` / `name` / `state` / `node_id` / `worker_id` / `num_attempts` / `duration` / `error`。⚠️ 真实 Ray 对应字段是 **`attempt_number`**，别拿错 |
| `list_objects()` | 对象列表（**大小**/所在节点/引用计数/是否落盘） | ⚠️ 简化版 | 排查"内存下不去"的第一入口。⚠️ mini-ray 的 `list_objects()` **没有"大小"与溢出入参**（字段只有 `object_id` / `state` / `node_id` / `produced_by` / `refs`）；字节数要看 `summarize_objects()` 的 `used_bytes`，或 `ObjectStore.stats()` |
| `list_actors()` | actor 列表（状态/邮箱积压/重启次数） | ✅ 已实现（同样的名字） | |
| `list_nodes()` | 节点列表 | ✅ 已实现（同样的名字） | |
| `list_workers()` | worker 进程池 | ✅ 已实现（同样的名字） | |
| `list_placement_groups()` | 放置组列表 | ✅ 已实现（同样的名字） | |
| `summarize_tasks()` / `summarize_objects()` / `summarize_actors()` | 聚合视图 | ✅ 已实现（同样的名字） | mini-ray 的实现见 `miniray/state.py` |
| `list_jobs()` | 提交过的 job 列表 | ❌ 未实现 | 配合 Jobs API，见第 4 章 §4.7 |
| `list_logs()` | 日志文件清单 | ❌ 未实现 | **先列再读** —— `ray logs` 的目标名很容易写错 |
| `list_cluster_events()` | 集群事件（节点加入/退出、autoscaler 决策） | ❌ 未实现 | 「节点为什么没了」的答案在这里 |
| `list_runtime_envs()` | runtime_env 安装状态 | ❌ 未实现 | 排查「任务卡在 PENDING 是不是依赖没装好」，见附录 F |
| `get_task(id)` / `get_actor(id)` / **`get_objects(...)`** / `get_log(...)` | 按 id 取明细 | ❌ 未实现 | ⚠️ **是复数 `get_objects`**（可传 id 列表），**没有** `get_object` 这个单数函数（`python/ray/util/state/__init__.py` 的 `__all__`） |
| `add_*` 系列 | 写入类接口 | ❌ 未实现 | mini-ray 只做只读 snapshot |
| `get_state()` | 整份状态快照 | ⚠️ mini-ray 特有 | 真实 Ray 没有这个顶层函数 |
| CLI：`ray status` / `ray summary tasks` / `ray memory` | 命令行视图 | ⚠️ 简化版 | mini-ray 用 `python -m miniray status` 提供资源/worker 视图 |
| CLI：`ray logs actor\|task\|worker\|job\|cluster` | 拉取/跟随日志 | ❌ 未实现 | **子命令组**形态（自 Ray 2.3 起）。注意 **`worker` 用 `--pid`、其余用 `--id`**；⚠️ 本书早先这里写的是已废弃的扁平写法 `ray logs --actor-id=` |
| profiling（`ray.util.state.profile` 等） | 性能剖析 | ❌ 未实现 | 需 `RAY_PROFILING=1`，见第 11 章 §11.6 |

### 应用自定义指标（`ray.util.metrics`）

> 这一节是**业务指标**入口，容易和上面那张表里的**系统状态**接口混淆：
> State API 回答「Ray 现在什么样」，`ray.util.metrics` 回答「**我的程序**现在什么样」。

```python
from ray.util.metrics import Counter, Gauge, Histogram
```

| API | 作用 | mini-ray 对照 | 备注 |
|---|---|---|---|
| `Counter(name, description=, tag_keys=)` | 只增计数（请求数/错误数） | ✅ 已实现（`miniray.util.metrics`） | `.inc(v, tags={...})`；`tag_keys` 必须先声明 |
| `Gauge(name, description=)` | 可增减的瞬时值（队列长度） | ✅ 已实现（同名） | `.set(v)` 与 Ray 一致；⚠️ `.get()` 是 mini-ray 的**教学扩展，真实 Ray 没有**（2.58 的 `Gauge` 只有 `__init__` / `set` / `__reduce__`） |
| `Histogram(name, description=, boundaries=)` | 分布（延迟） | ⚠️ 已实现，**多一个 `.timer()`** | `.observe(v)` 与 Ray 一致；`.timer()` 是 mini-ray 的**教学扩展，真实 Ray 没有** |
| `_metrics_export_port` / `--metrics-export-port` | Prometheus 抓取端口 | ❌ 未实现 | 系统指标与自定义指标**共用**这个端点；mini-ray 的 `metrics.snapshot()` 只导**当前进程**的点 |

> ⚠️ 两个硬约束：**① 标签必须在 `tag_keys` 里先声明**（防止基数爆炸，
> 这是故意的）；**② 绝不要把 `user_id` / `request_id` 这类高基数维度当标签** ——
> 会把 Prometheus 打挂。高基数内容该进日志或 trace。
> 完整讨论见第 11 章 §11.5。

---

## A.9 AI 库的现代入口

> 这一层的入口变动很快。下表以 **Ray 2.58** 的 V2 API 为准；
> 凡本书未核对的细节都标注出来。**mini-ray 明确不做这些库**
> （见 `mini-ray/README.md` 的"明确不做的"清单），所以对照列一律是 ❌。

### Ray Data

| 入口 | 作用 | mini-ray 对照 |
|---|---|---|
| `ray.data.read_parquet(...)` / `read_csv` / `read_json` / `read_images` | 读数据源；DataSourceV2 默认开启 | ❌ 未实现 |
| `ray.data.from_items(...)` / `range(...)` / `from_spark(df)` | 从内存/Spark 构造 | ❌ 未实现 |
| `Dataset.map_batches(fn, batch_size=..., batch_format=...)` | **批量映射**（反模式 1 的正解） | ❌ 未实现 |
| `Dataset.map` / `filter` / `flat_map` | 逐行算子 | ❌ 未实现 |
| `Dataset.groupby(...).map_groups(...)` / `Dataset.sort()`（hash shuffle） | 分组与排序 | ❌ 未实现 |
| `Dataset.iter_batches()` / `materialize()` | 流式消费 / 触发执行 | ❌ 未实现 |
| `Dataset.write_parquet(...)` | 写出 | ❌ 未实现 |
| `Dataset.write_delta(...)` | 写 Delta 表（2.57，PR #64923） | ❌ 未实现 |
| `Dataset.write_delta(path, catalog=DatabricksUnityCatalog(...))` | 写 Databricks / Unity Catalog 表（`write_databricks_table` 这个名字**在 2.58 并不存在**） | ❌ 未实现 |
| `ray.data.llm`（LLM 批推理处理器） | 批推理入口 | ❌ 未实现；**具体 API 名称本书未逐版本确认** |

### Ray Train（V2）

| 入口 | 作用 | mini-ray 对照 |
|---|---|---|
| `ray.train.report(metrics, checkpoint=...)` | 上报指标/检查点 | ❌ 未实现 |
| `ray.train.get_dataset_shard(...)` / `get_context()` | 拿数据分片 / 上下文 | ❌ 未实现 |
| `ray.train.torch.TorchTrainer` | 训练器（**唯一入口**；`ray.train` 里**没有**通用的 `Trainer` 类，`__all__` 里**一个 `Trainer` 都没有** —— 只有 `get_checkpoint` / `get_context` / `get_dataset_shard` / `report` / `BackendConfig` / `Checkpoint` / `CheckpointConfig` / `DataConfig` / `FailureConfig` / `Result` / `RunConfig` / `ScalingConfig` / `SyncConfig` / `TrainContext` / `TrainingFailedError` / `TRAIN_DATASET_KEY`，见 `python/ray/train/__init__.py:78-95`） | ❌ 未实现 |
| `ray.train.Checkpoint` | 检查点对象 | ❌ 未实现 |
| `RAY_TRAIN_V2_ENABLED` | V2 特性开关（2.51 起默认开启） | ❌ 未实现 |

### Ray Tune

| 入口 | 作用 | mini-ray 对照 |
|---|---|---|
| `ray.tune.Tuner(trainable, param_space, tune_config=..., run_config=...)` | 调参入口 | ❌ 未实现 |
| `ray.tune.TuneConfig` / `RunConfig` | 配置对象 | ❌ 未实现 |
| `ray.tune.with_resources(...)` / `with_parameters(...)` | 包裹 trainable | ❌ 未实现 |
| `ray.tune.trainable` / `ray.tune.Callback` | 自定义 trainable / 回调 | ❌ 未实现 |
| `Tuner(trainer)`（把 Trainer 塞进 Tuner） | **已弃用**（2.43 起） | ❌ 未实现 |

### Ray Serve

| 入口 | 作用 | mini-ray 对照 |
|---|---|---|
| `@serve.deployment(...)` | 定义部署 | ❌ 未实现 |
| `serve.run(app)` | 启动应用 | ❌ 未实现 |
| `serve.get_app_handle(name)` / `serve.get_deployment_handle(...)` | 拿句柄 | ❌ 未实现 |
| `DeploymentHandle.remote()` → `DeploymentResponse`，`.result()` / `await` | 调用与取值 | ❌ 未实现 |
| `handle.options(stream=True)` | 流式响应 | ❌ 未实现 |
| `serve.ingress(app)` | HTTP 入口（FastAPI 风格） | ❌ 未实现 |
| `@serve.batch(max_batch_size=, batch_wait_timeout_s=, max_concurrent_batches=)` | **攒批**（CPU 小模型吞吐第一手段）。默认 `10` / `0.01` / `1`；Serve 要求 `max_ongoing_requests >= max_batch_size × max_concurrent_batches`（§15.4） | ❌ 未实现 |
| `@serve.multiplexed(...)` / `serve.get_multiplexed_model_id()` | **模型多路复用**：一套副本服务很多模型（§15.16） | ❌ 未实现 |
| `serve.start()` / `serve.shutdown()` | 不经过 HTTP 的进程内用法（§15.15） | ❌ 未实现 |
| `serve.get_app_handle(name)` / `serve.get_deployment_handle(name, app_name=)` | 取句柄。**多应用时必须给 `app_name`**（§15.19） | ❌ 未实现 |
| LLM 相关（KV-cache 感知路由、PD 分离） | 2.58 新增能力 | ❌ 未实现 |

---

## A.10 已弃用 / 已移除 API

**这一节是升级时最容易踩的地方。** 每一条都给出处，能标注版本的就标。

| API / 特性 | 状态 | 出处与备注 |
|---|---|---|
| **Ray Workflows**（`ray.workflow` / `ray.workflows`） | 2.44 **弃用**，之后**移除**（PR #53612）；import 会抛 `RuntimeError` 说明已弃用并移除 | 最后一个包含它的版本是 **`ray==2.47`**（错误信息里明确写出） |
| **`RayServeHandle` / `RayServeSyncHandle`** | **2.10 已完全移除**（PR #42526 "Fully remove old handle API"） | 替代品：`DeploymentHandle` / `DeploymentResponse` |
| **Ray Train V1 API**（`ray.air` 那一套） | 弃用路径；V2 自 **2.51 默认开启**（PR #57857） | 混用 V1/V2 会直接报错（PR #57570），错误信息提示改 import 或设 `RAY_TRAIN_V2_ENABLED=0` |
| **`Tuner(trainer)`** | **2.43 起弃用** | 迁移方向是 `RAY_TRAIN_V2_ENABLED=1` + 函数式 `train_driver_fn` |
| **RLlib 的 TF 支持与旧 API stack** | 弃用/移除路线 | **本书未确认具体版本号**；请以 RLlib 官方 release notes 为准 |
| **Python 3.9** | 自 2.52 弃用，**2.54.0 起正式不再支持** | 当前要求 **Python ≥ 3.10** |
| **`RAY_max_pending_tasks`** | **在 2.58 中不存在** | 这个环境变量常见于旧博客；官方的背压方案是应用层 `ray.wait`（`ray-core/patterns/limit-pending-tasks`） |
| **`ray.init(local_mode=True)`** | **已移除** | 写了会 `RuntimeError: local_mode is no longer supported`（第 11 章 §11.7；PR #60647）。调试改用 **Ray Distributed Debugger**（remote 里 `breakpoint()` + VS Code 扩展）；`ray debug` 是 **legacy** 入口，需 `RAY_DEBUG=legacy`。⚠️ **mini-ray 仍保留**该参数，那是教学特性 |
| **`ray.util.queue.Queue`** | 现行可用 | mini-ray 也已实现（`miniray.util.queue`），见附录 B §B.3.6 |
| `ray.services` / 老的 `ray.utils` 等内部模块 | 早已移到 `ray._private` | 不保证稳定，不要 import |
| **Ray Data 的 `DatasetPipeline`** | 弃用 | **本书未确认移除版本**；新代码用 `iter_batches` + 流式执行 |
| **`DAGNode.execute()`**（未编译的 DAG 执行） | **已弃用** —— `DeprecationWarning: DAGNode.execute() is deprecated and will be removed in a future release.`（PR **#63716**，关闭 issue **#63666**） | **原因**：未编译路径每次 `execute()` 都走 `FunctionNode._execute_impl()` 内部的 `ray.remote(self._body)`，**每次执行都往 GCS internal KV 导出新的函数元数据**，而这些条目在 job 生命周期内**无清理、无淘汰** → **GCS KV 无界增长**。出路是编译图（`experimental_compile()`，第 26 章 §26.3） |
| **`ray.train.lightning.LightningTrainer`** / **`ray.train.huggingface.TransformersTrainer`** / **`AccelerateTrainer`** | **2.9 起已移除**（2.7 弃用 → 2.8 报错 → 2.9 移除，REP *"Unify Torch based Trainers on the TorchTrainer API"*） | 统一收敛到 **`TorchTrainer`**：把框架自己的 `Trainer` 建在训练函数**内部**，用 `prepare_trainer` / `RayTrainReportCallback` 接回 `ray.train.report`。⚠️ `ray.train.lightning` 模块**还在**，只是 `__all__` 里没有 `LightningTrainer`（第 13 章 §13.3、第 29 章） |
| **`Checkpoint.from_uri()`** | **不存在** | 用构造函数 **`Checkpoint(path="s3://bucket/ckpt")`**；远端路径会从 URI 推断文件系统（第 13 章 §13.5） |
| **`ray.util.list_actors()`** / **`ray.list_actors()`** | **两者都不存在** | 正确路径是 `ray.util.state.list_actors()`（列全部）与 `ray.util.list_named_actors()`（只列命名） |
| **`TensorFlow` 在 RLlib 新 stack 中** | `ray-2.38.0` 起新 stack 移除 PPO/IMPALA/APPO 的 TF 支持；**2.49.0（PR #55042）正式弃用** | RLlib 收敛为**单一框架 PyTorch**。⚠️ 本书早先只含糊写"弃用/移除路线、版本未确认"，第五轮已补上具体版本 |
| **`rllib_contrib`** | **2.40 起已从主仓库删除**（2.39 是最后一个带它的版本） | 它是 `ray-project/ray` 仓库里的**一个目录**（不是独立仓库）；安装名是 **`rllib-a3c`** 这类形式，**没有 `-contrib-`**（`rllib-contrib-a3c` 在 PyPI 上是 404）。详见第 16 章 §16.2 |
| **`Algorithm.save()` / `restore()`（RLlib）** | ⚠️ **不是弃用** —— 本书第四轮曾这么写，是错的 | 它们**不在 RLlib 里定义**，而是继承自 Ray Tune 的 `Trainable`，在 2.58 里标的是 `@DeveloperAPI`、**未标弃用**。准确说法是"RLlib **推荐** `save_to_path()` / `restore_from_path()`"，属**推荐差异**而非弃用（第 16 章 §16.18） |
| **`ray._private.internal_api.free()`** | **2.58 已弃用，但弃用只写在 docstring 与 `warnings.warn` 里** | docstring 开头：*"DeprecationWarning: `free` is a deprecated API and will be removed in a future version of Ray."*（`python/ray/_private/internal_api.py:181-185`）。⚠️ **它没有 `@Deprecated` 装饰器**，但函数体里**确实**调了 `warnings.warn(..., DeprecationWarning)`（同文件 `:221-225`）—— 所以"声明"与"运行时行为"在这里是**一致**的，只是 Python 的默认过滤规则会吞掉非 `__main__` 模块发出的 `DeprecationWarning`，**看不到 warning ≠ 没弃用**。复核：`grep -n "DeprecationWarning" python/ray/_private/internal_api.py` |

---

## A.11 关键环境变量

**样本来源**：`src/ray/common/ray_config_def.h`（系统配置）与
`python/ray/_private/ray_constants.py`（Python 侧常量）。
命名规律是 **`RAY_<配置名>`**，例如配置 `scheduler_top_k_fraction`
对应环境变量 `RAY_scheduler_top_k_fraction`。
**凡本书未能核对的默认值都写"未确认"。**

### 连接与集群

| 变量 | 作用 | 备注 |
|---|---|---|
| `RAY_ADDRESS` | 要连接的集群地址（等价于 `ray.init(address=...)`） | 最常用的一个；写错会导致 `ray.init()` 卡住或连错集群 |
| `RAY_NAMESPACE` | 默认命名空间 | |
| `RAY_OVERRIDE_RESOURCES` | 覆盖节点的**自定义资源**声明。源码注释写得很明确：*"Used by autoscaler to set the node custom resources and labels from cluster.yaml"*（`python/ray/_private/ray_constants.py:249-251`，同组还有 `RAY_OVERRIDE_LABELS`） | ⚠️ 本书早先标的是"未确认语义"，第六轮已按源码补上 |
| `RAY_gcs_storage` | GCS 的存储后端。**取值可确认**：`'memory'`（**默认**）、`'redis'`、`'rocksdb'`（`src/ray/common/ray_config_def.h:446-448`：`RAY_CONFIG(std::string, gcs_storage, "memory")`） | ⚠️ **默认是 `memory`，即默认不做容错**。⚠️ **不是"2.57 起改用 RocksDB"**：官方 `doc/source/ray-core/fault_tolerance/gcs.rst` 写明 **External Redis 是 officially supported**（`:11`、`:67`），**Embedded RocksDB 是 alpha**（`:12`、`:79-84`，2.57 引入）—— 两者都是**显式 opt-in**。⚠️ 选 `rocksdb` **必须同时设 `RAY_gcs_storage_path`**，否则 `python/ray/_private/node.py` 抛 `ValueError: RAY_gcs_storage=rocksdb requires RAY_gcs_storage_path to be set to a writable directory.`（`:507`、`:565`）。完整讨论见第 10 章 §10.6、附录 C §C.5 |
| `RAY_TMPDIR` | 会话临时目录（溢出目录、socket 都在这附近） | ⚠️ **只有这一个名字，没有 `RAY_temp_dir`** —— 全树 `grep -rn "RAY_temp_dir" python/` **0 命中**，`ray_config_def.h` 里也没有 `temp_dir` 配置项。读取点在 `python/ray/_common/utils.py` 的 `get_default_system_temp_dir()`（`:326-343`）：先看 `RAY_TMPDIR`，**Linux 上再看 `TMPDIR`**，都没有则 Linux/macOS 回退 `/tmp`、其他平台用 `tempfile.gettempdir()` |

### 可观测性（第 11 章）

| 变量 | 作用 | 备注 |
|---|---|---|
| `RAY_PROFILING=1` | 打开 timeline 采集 | ⚠️ **不开就没有任务级事件**，而且**不报错、只打 warning、照样写出一个内容为 `[]` 的文件**（比报错更易误判）。**还要同时设 `RAY_task_events_report_interval_ms=0`**（§11.6） |
| `RAY_task_events_report_interval_ms=0` | timeline 的**第二个**必需变量 | 只设 `RAY_PROFILING` 而不设它，事件可能停在上报队列里没进 GCS，结果**同样是空文件**（§11.6） |
| `RAY_DEBUG=legacy` | 回到**旧版** Ray Debugger（配合 `ray debug`） | ⚠️ **默认是"新版"**（2.39 起）。`RAY_DEBUG` 的取值里**只有 `legacy` 有特殊含义**，写 `RAY_DEBUG=1` 不是新版入口（§11.7、第 33 章 §33.6） |
| `RAY_DEBUG_POST_MORTEM=1` | 打开**新版**调试器的**事后调试**（未捕获异常时冻结任务等接入） | ⚠️ **默认关闭**。它属于**新版**调试器 —— 用之前要**先去掉** `RAY_DEBUG=legacy` 与 `--ray-debugger-external`（两者二选一）。本书第四轮曾把它写成"只在 legacy 下有效"，**方向反了**（§11.7） |
| `RAY_enable_task_events_to_dashboard_head` | **2.58 把 task events 移出 GCS 热路径**的开关 | 升级到 2.58 后若发现 `state.list_tasks()` 字段变少 / dashboard 上看不到 task events，先查这里（§11.2） |
| `RAY_memory_monitor_refresh_ms` | 内存监控刷新间隔（默认 `250`） | 调大可降低监控开销，代价是 OOM 判定变迟钝 |
| `RAY_DEFAULT_OBJECT_STORE_MEMORY_PROPORTION` | 覆盖对象存储占内存的比例常量（默认 `0.3`） | 见第 12 章坑 2：**官方已建议不要动这个旋钮** |

### 安全（第 17、24 章）

| 变量 | 作用 | 备注 |
|---|---|---|
| `RAY_AUTH_MODE` | `disabled`（默认）/ `token` | **2.52 引入**；必须**全集群一致**设置 |
| `RAY_AUTH_TOKEN` | 直接给 token 字符串（优先级最高） | 官方建议改用下面的文件方式，避免被其他读环境变量的代码看到 |
| `RAY_AUTH_TOKEN_PATH` | 从文件读 token（读不到直接 fatal，不静默回退） | |
| `RAY_ENABLE_K8S_TOKEN_AUTH` | 用 K8s ServiceAccount token | 与 `RAY_AUTH_MODE=token` 组合 |
| `RAY_TLS_SERVER_CERT` / `RAY_TLS_SERVER_KEY` / `RAY_TLS_CA_CERT` | gRPC 服务端 TLS 证书链、私钥、CA（配合 `RAY_USE_TLS=1` 使用） | **存在且可核对**：`python/ray/_common/tls_utils.py` 的 `load_certs_from_env()` 里 `tls_env_vars = ["RAY_TLS_SERVER_CERT", "RAY_TLS_SERVER_KEY", "RAY_TLS_CA_CERT"]`，逐个 `os.environ[...]` 读取。⚠️ **三个必须同时设置** —— 只设一部分会抛 `RuntimeError: If the environment variable RAY_USE_TLS is set to true then RAY_TLS_SERVER_CERT, RAY_TLS_SERVER_KEY and RAY_TLS_CA_CERT must also be set.`。⚠️ 本书早先标的是"**未确认存在**"，已按源码更正（TLS 的**其他**用法——在前置代理上终止 TLS——仍然是官方文档的主要口径，见第 17 章） |

### 对象存储与内存（第 18 章）

| 变量 | 作用 | 默认/备注 |
|---|---|---|
| `RAY_DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES` | 对象存储容量上限 | 默认 `200GB`；**必须在 Python 进程启动前设置**（import 时读取） |
| `RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE` | 允许对象存储超过 `/dev/shm` 大小（走慢速存储） | 正确名字是这一长串，**不是 `RAY_ALLOW_SLOW_STORAGE`** |
| `RAY_object_spilling_threshold` | 已用比例超过它开始溢出 | `0.8` |
| `RAY_automatic_object_spilling_enabled` | 自动溢出开关 | Ray 1.3+ 默认开启；有用户报告在 2.3/2.44 上**关不掉** |
| `RAY_max_io_workers` | 溢出/恢复的 IO 线程数 | **默认 `4`**（`ray_config_def.h`） |
| `RAY_min_spilling_size` | 一次最少溢出字节数 | **默认 `100MB`**（`ray_config_def.h`） |
| `RAY_memory_monitor_refresh_ms` | 内存监控采样间隔 | `250` ms；设 **`0` 关掉内存监控**（调试用，生产**别关** —— 关掉后换成 Linux OOM killer 出手，可能杀掉 raylet/GCS）。见第 7 章 §7.7 |

### 容错与调试

| 变量 | 作用 | 备注 |
|---|---|---|
| `RAY_TASK_MAX_RETRIES` | 全局覆盖任务的 `max_retries` | ⚠️ **设成 `0` 会连带关掉 lineage 重建** —— 对象丢了就只能报 `ObjectLostError`。这是"为了止血关重试，结果容错也没了"的典型事故，见第 10 章 §10.4 |
| `RAY_record_ref_creation_sites` | 记录 ObjectRef 的**创建位置**（`call_site`） | **默认关闭**；不开的话 `ray memory` / `list_objects()` 里的 `call_site` 全是 `disabled`。排查内存泄漏必开的开关，见第 11 章 §11.2 |
| `RAY_PROFILING` | 打开任务级 profiling 事件 | **timeline 必须设它才有内容**；不开只会 warning + 写出空文件。另需 `RAY_task_events_report_interval_ms=0`，见第 11 章 §11.6 |
| `RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S` | 优雅排水超时 | 抢占 / 滚动升级时让节点上的任务从容结束；**必须在预发演练**，见第 10 章 §10.7 |

### 调度与 worker（第 18 章）

| 变量 | 作用 | 默认 |
|---|---|---|
| `RAY_scheduler_spread_threshold` | 判定"轻载节点"的利用率阈值 | `0.5` |
| `RAY_scheduler_top_k_fraction` | 候选集占节点数的比例 | `0.2` |
| `RAY_scheduler_top_k_absolute` | 候选集大小下限 | `1` |
| `RAY_num_workers_soft_limit` | **空闲** worker 数的软上限 | `-1`（表示用可用 CPU 数） |
| ~~`RAY_worker_idle_timeout_ms`~~ | —— | ⚠️ **本书早先写错了**：`RAY_worker_idle_timeout_ms` / `worker_idle_timeout_ms` 在 **2.58 里检索不到**（`src/ray/common/ray_config_def.h` 0 命中）。真实 Ray 里管这件事的旋钮是 `kill_idle_workers_interval_ms` 与 `idle_worker_killing_time_threshold_ms`。同名不带前缀的 `worker_idle_timeout_ms` 是 **mini-ray 自己的** `miniray.init(...)` 选项（`miniray/runtime.py:155`，默认 10000），别当成 Ray 的变量 |
| `RAY_DISABLE_WORKER_POOL` 等 | —— | ⚠️ 未确认，勿凭记忆使用 |

### AI 库

| 变量 | 作用 | 备注 |
|---|---|---|
| `RAY_TRAIN_V2_ENABLED` | 启用 Ray Train V2 | 2.51 起**默认开启**；设 `0` 回退 V1；Tune 会把该变量**传播**到 Train driver |
| `RAY_CHDIR_TO_TRIAL_DIR` | Tune trial 是否 chdir | Tune 侧；**语义细节未确认** |
| `RAY_TUNE_*` 系列 | —— | ⚠️ **本书未确认存在成体系的 `RAY_TUNE_*` 变量**，不列举 |
| `RAY_UC_VOLUMES_FUSE_TEMP_DIR` | 写 Databricks Unity Catalog 表时的临时目录 | 见 Databricks 文档 |
| `VLLM_USE_RAY_V2_EXECUTOR_BACKEND` | vLLM 的新 Ray executor 开关（默认 `False`） | 见 vLLM RFC #35848 / PR #36836 |

---

## A.12 小结

* Ray Core 的 API 面其实很小：**`remote` / `options` / `get` / `put` / `wait` /
  `cancel` + actor 的四个生命周期函数**。mini-ray 把这套**全部**实现了，
  所以你在第 06 章写过的代码，迁移到真实 Ray 通常只需要**改一行 import**
  （`import miniray as ray` → `import ray`）**并删掉 `sys.path` 引导**。
* ⚠️ **唯一的例外是 `.bind()`，它的语义是分叉的**（详见 §A.2）：
  mini-ray 的 `.bind()` 返回一个还能继续 `.remote()` 的 `RemoteFunction`，
  真实 Ray 的 `.bind()` 返回 DAG 节点 `FunctionNode` —— 在节点上 `.remote()`
  直接抛 `AttributeError`。所以凡是「`.bind()` 之后再 `.remote()`」的代码，
  迁移时**必须改写成建图 + `execute()`**，**不是换一行 import 就能跑**。
* **差异集中在"周边"**：`runtime_env`（只做 `env_vars`）、State API（只做只读查询）、
  调度策略（两种 vs 四种）、放置组（不做继承/不杀 actor）、dashboard / autoscaler /
  AI 库（明确不做）。
* **升级时最该记住的四条**：Workflows 没了（`ray==2.47` 是最后一代）、
  `RayServeHandle` 2.10 就没了、Train V1 混用会直接报错、Python 3.9 不支持了。
* 凡是本书标"未确认"的条目，正确的做法永远是**在你手头的版本上验证一次**：
  `python -c "import ray; help(ray.init)"`、`ray start --help`、
  `ray memory --help`。文档会滞后，源码不会。
