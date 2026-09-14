仓库地址：https://github.com/hhk-png/cycle-agent

# 附录 D：FAQ 与排错

> 本附录目标：**按症状查**，不按概念查。
> 每条都是「问题 → 如何确认 → 怎么修 → 对应文档页名」四段式。
> 凡本书未核实的结论，会明确标注"未确认"，不做猜测。

**怎么用**：先在下面的索引里找和你**看到的报错/现象**最接近的那一条；
"如何确认"里的命令**先跑一遍再动手改**——Ray 的排错最忌讳"猜着改参数"。

**分节**：

```
D.1 安装与环境        D.6 调度
D.2 启动与连接        D.7 AI 库（Data / Train / Tune / Serve）+ 表格 ML / 注册 / 追踪
D.3 任务              D.8 安全
D.4 内存              D.9 提问前请准备好什么
D.5 Actor             D.10 小结
```

**问题索引**（共 39 条；编号**不按文档顺序**排列 ——
D.1–D.3 是早期写的，D.4–D.8 里带「升/配/自」标记的几条是后来补的，
Q37–Q39 是第四轮修订随第 34–36 章一起补的）：

| # | 所属节 | 问题 |
|---|---|---|
| Q1 | D.1 | `pip install ray` 和 `ray[default]` 差在哪 |
| Q2 | D.1 | Python 版本要求 |
| Q3 | D.1 | Windows 上能用吗 |
| Q4 | D.1 | 能同时装 `ray` 和 `mini-ray` 吗 |
| Q34 | D.2 | **升级 Ray 版本后，原有代码跑不起来了** |
| Q5 | D.2 | `ray.init()` 卡住不动 |
| Q6 | D.2 | `Address already in use` / 端口被占 |
| Q7 | D.2 | 集群连不上 / `address="auto"` 找不到集群 |
| Q35 | D.2 | **Ray 声明的资源和我机器的实际资源对不上** |
| Q8 | D.2 | dashboard 打不开 |
| Q9 | D.3 | 任务一直 PENDING |
| Q10 | D.3 | `ray.get` 超时 |
| Q11 | D.3 | `RayTaskError` 的堆栈怎么读 |
| Q12 | D.3 | `WorkerCrashedError`（OOM / 段错误） |
| Q13 | D.3 | 任务里 `print` 的东西去哪了 |
| Q14 | D.4 | `ObjectStoreFullError` |
| Q15 | D.4 | 怎么配对象溢出、往哪溢 |
| Q16 | D.4 | 容器/K8s 里 `/dev/shm` 不够 |
| Q17 | D.4 | driver 内存一直涨，被 OOM 杀掉 |
| Q18 | D.4 | 为什么 Mac 上更慢 |
| Q19 | D.4 | `ray memory` 怎么用 |
| Q20 | D.5 | actor 死了怎么办 / `max_restarts` 恢复什么 |
| Q21 | D.5 | actor 方法返回顺序乱了 |
| Q22 | D.5 | actor 占着 GPU 不放 |
| Q23 | D.5 | `The actor with name=X already exists` |
| Q24 | D.6 | 任务没被调度到我想去的节点 |
| Q25 | D.6 | 节点亲和与放置组的区别 |
| Q26 | D.6 | 0 CPU 的任务能起多少个 |
| Q27 | D.6 | K8s 里 pod 起不来 / head 上跑了业务负载 |
| Q36 | D.6 | **autoscaler 不扩容（或该缩容时不缩）** |
| Q28 | D.7 | Ray Data 跑着跑着 OOM |
| Q29 | D.7 | Ray Train 从 checkpoint 恢复失败 |
| Q30 | D.7 | Tune 不收敛 / 比手动调参还慢 |
| Q31 | D.7 | Ray Serve 请求超时 / 背压 |
| Q32 | D.8 | dashboard 暴露有什么风险 |
| Q33 | D.8 | token 认证怎么开 |
| Q37 | D.7 | **GBDT 训练报「找不到列」/ 上线后预测全错**（特征顺序与编码表） |
| Q38 | D.7 | **模型注册了但 `@production` 读不到 / 回滚没生效** |
| Q39 | D.7 | **Jaeger 里 span 是断开的，拼不成一条链** |

> **为什么要这张表**：这些补充 FAQ（Q34–Q39）都是在原有编号之后追加的，
> 按主题插在了对应小节里，所以**编号在文档里不是递增的**。
> 直接按上面的表跳转，不要按编号顺序读。

---

## D.1 安装与环境

### Q1：`pip install ray` 和 `pip install ray[default]` 差在哪？我该装哪个？

**如何确认**：看官方安装页（`ray-overview/installation`）给出的两条推荐命令：

```bash
pip install -U "ray[default]"                        # 通用应用
pip install -U "ray[data,train,tune,serve]"          # ML 应用
```

**怎么修**：**通用/生产场景装 `ray[default]`**。裸 `ray` 是核心包，
`[default]` 额外带来 dashboard、集群启动器（autoscaler）以及一批观测与 CLI 依赖；
少装它最典型的症状是"dashboard 起不来""`ray status` 行为异常""某些 CLI 报缺少依赖"。
**具体缺哪些包，本书未逐项核对**——如果遇到 `ModuleNotFoundError`，
先补装 `ray[default]` 再排查别的。

**文档页**：`ray-overview/installation`

### Q2：Python 版本要求是什么？

**如何确认**：

```bash
python -V
python -c "import ray; print(ray.__version__)"
```

**怎么修**：当前要求 **Python ≥ 3.10**；**3.9 自 2.52 弃用、2.54.0 起正式不再支持**（第 1 章）。
Windows 上的 wheel 是 `win_amd64` 标签，别去下 Linux 的 manylinux 包。

**文档页**：`ray-overview/installation`

### Q3：Windows 上能用吗？有没有坑？

**如何确认**：官方安装文档的 Windows 段落；自己跑一遍最小例子。

**怎么修**：**官方表述是"Ray on Windows 目前处于 beta"**。已知注意事项：

* **必须先装 Visual C++ 运行库**，否则会报
  `FileNotFoundError: Could not find module '_raylet.pyd'` 或缺 `VCRUNTIME140_1.dll`；
* **多节点 Ray 集群在 Windows 上是实验性的、不推荐**；
* **`runtime_env` 在 Windows 上长期受限**：Ray 的测试与源码里有多处
  "runtime_env not supported on Windows" 的注释，conda 环境明确"在 Mac/Linux 上通过
  runtime_env 支持"。**当前 2.58 上 runtime_env 的 Windows 支持边界，本书未确认**——
  用之前请在你的版本上试跑一次；
* 文件打开更慢，日志相关性能会受影响；Windows 没有 fork 的 copy-on-write，
  新进程内存开销更大。

**文档页**：`ray-overview/installation`（Windows 段落）

### Q4：能同时装 `ray` 和 `mini-ray` 吗？

**怎么修**：能，它们是不同的包名（`miniray`）。但如果代码里写了
`import miniray as ray`，请确保那个文件里**没有**同时 `import ray`——
两者混用会让"这个 ObjectRef 到底是哪个库的"变成调试噩梦。

---

## D.2 启动与连接

### Q34：升级 Ray 版本后，原有代码跑不起来了

**为什么值得单列**：Ray 的**核心 API 很稳**，但**上层库和默认值**在版本间动得多，
所以"升级就崩"几乎总是下面三类之一。

**① 默认值变了（最隐蔽：不报错，行为不一样）**

| 变更 | 版本 | 症状 |
|---|---|---|
| Train V2 默认开启 | 2.51 | 旧的 V1 trainer 写法失效或告警（回退：`RAY_TRAIN_V2_ENABLED=0`） |
| autoscaler v2 **默认值按启动路径不同**（🔴 见下方"按路径区分"的说明） | `ray up` 自 **2.50.0** 起默认开；裸 `ray start` 默认关 | 开启后扩缩容行为与旧版不同 |
| Serve `max_ongoing_requests` 100 → 5 | 2.32（PR #45943） | 同样的负载，吞吐掉下来（因为每副本并发变小了） |
| Serve `target_ongoing_requests` 1.0 → 2.0 | **同一个 PR #45943** | 扩缩容目标变了：副本数会比以前少（详见第 15 章 §15.5） |

> 🔴 **"autoscaler v2 默认开还是默认关"必须按路径回答 —— 笼统写哪个都是错的。**
> 这是**两个不同的层**，值也确实不同：
>
> | 路径 | v2 默认 | 依据 |
> |---|---|---|
> | **`ray up`**（VM 集群，autoscaler 的主路径） | ✅ **默认开**（自 **2.50.0**） | `python/ray/autoscaler/_private/commands.py`：`if os.getenv("RAY_UP_enable_autoscaler_v2", "1") == "1":` → `with_envs(..., {"RAY_enable_autoscaler_v2": "1", ...})`。注释原文 *"The default value is 1 since Ray 2.50.0."*；启动时还会打一条 "Autoscaler v2 is now enabled by default" 的提示。**回退：`RAY_UP_enable_autoscaler_v2=0`** |
> | **裸 `ray start`**（以及 raylet / 组件侧的配置默认） | ❌ **默认关** | `src/ray/common/ray_config_def.h`：`RAY_CONFIG(bool, enable_autoscaler_v2, false)` —— 不注入环境变量就是 v1 |
> | **KubeRay** | ❌ **显式 opt-in**（不是默认开） | operator 只在 `spec.autoscalerOptions.version: v2` **显式指定**时才注入 `RAY_ENABLE_AUTOSCALER_V2=true`；不写 `autoscalerOptions` 时两个 env 都不注入，落到 raylet 的 `false` |
>
> **为什么容易写反**：`ray up` 走的是"**在 `ray start` 命令行前面 `with_envs` 注入环境变量**"这条路，
> 所以"默认开"体现在 **`commands.py` 的默认值 `"1"`** 上，**不体现在 `ray_config_def.h` 里**。
> 只 grep 配置头文件会得出"默认关"的结论 —— 第 8 章 §8.8 引用的正是
> `ray_config_def.h` 那一层，它描述的是**裸 `ray start` / 组件默认**，与 `ray up` 层不冲突；
> 两处合起来看才是完整的。
>
> **一句可执行的判据**：如果你是用 `ray up` / `cluster.yaml` 起的集群，**默认就是 v2**，
> 排障时先按 v2 的行为去理解；`ray status` 里 autoscaler 相关的报错信息最可靠。

**② API 被移除（会明确报错）**

| 移除项 | 版本 |
|---|---|
| `RayServeHandle` | 2.10（PR #42526） |
| Ray Workflows | 2.44 弃用、之后移除（`ray==2.47` 是最后一个含它的版本） |
| **Ray Core 的 `local_mode`**（连带清理了 RLlib/Tune 里的调用点） | 见第 11 章 §11.7、第 16 章 §16.4 |

**③ Python 版本支持变了**：3.9 自 **2.52** 弃用、**2.54.0 起正式不再支持**。

**怎么确认**：

```bash
ray --version                       # 当前版本
python -W error::DeprecationWarning my_script.py    # 把告警当错误,提前暴露
```

再对着 [release notes] 的 **"Deprecations" / "Breaking changes"** 小节看一遍。

**怎么修**：升级前先**在预发环境跑一遍**你的测试；把 `ray==2.x.y`
**钉死在依赖里**（别用 `ray>=`），升级当成一次**有意识的行为**，
而不是 `pip install -U` 顺手做的事。

**文档页**：各版本 release notes；`ray-core/...`

---

### Q5：`ray.init()` 卡住不动，没有报错

**如何确认**：

```bash
echo $RAY_ADDRESS            # 有没有指向一个早就没了的集群
ray status                   # 另开一个终端
```

**怎么修**：按可能性排序：

1. **`RAY_ADDRESS` 指向了失效的集群**。这是最常见的原因：环境变量还留着上一次实验的地址，
   `ray.init()` 就会去连那个不存在的地址。解法是 `unset RAY_ADDRESS` 或显式
   `ray.init(address=None)`。
2. **网络/防火墙**：跨机场景下 gRPC 端口不通。用 `ray status` 看 GCS 地址，
   再用 `nc -vz host port` 试通。
3. **`/dev/shm` 太小**导致对象存储初始化慢或失败（容器里常见），见 Q16。

> ⚠️ **一条曾被错列在这里的情况：在 worker/task 里调 `ray.init()`** ——
> 它**不属于"卡住"类**，因为真实 Ray **立刻抛**，不卡：
>
> ```text
> RuntimeError: Maybe you called ray.init twice by accident? This error can be
> suppressed by passing in 'ignore_reinit_error=True' or by calling
> 'ray.shutdown()' prior to 'ray.init()'.
> ```
>
> 依据：`python/ray/_private/worker.py` —— `global_worker.connected` 为真时
> 直接 `raise RuntimeError(...)`，**这个判断发生在任何连接尝试之前**。
> 而 worker 进程为什么一定是 connected：`python/ray/_private/workers/default_worker.py`
> 在启动时就调了 `ray._private.worker.connect(...)`，那里会
> `set_is_connected(True)`。两处合起来 = **worker 里调 `ray.init()` 必抛，不卡**。
> 所以它解释不了"卡住不动"这个症状。
> **症状是"立刻看到 RuntimeError"时，往这条上想；症状是"卡住"时，看上面 1~3 条。**
> 本书早先把这条归档在"卡住"下面，是错的，已移出。
> （mini-ray 里同样会直接报错并说明原因，见 `miniray/runtime.py`。）

**文档页**：`ray-core/troubleshooting`、`cluster/vms/...`

### Q6：`Address already in use` / 端口被占

**如何确认**：

```bash
ray stop --force                                   # 清掉残留进程
ss -lntp | grep -E '6379|8265|10001'               # Linux 上看端口占用
```

**怎么修**：

* 残留的 raylet/GCS 进程没退干净 → `ray stop --force`；
* 确实要和别的服务共存 → 显式换端口：GCS 端口用 `ray start --head --port=...`，
  dashboard 用 `--dashboard-port=...`；
* 容器里多实例共存时，**`RAY_TMPDIR` 也要分开**，否则会话目录撞车。

**文档页**：`ray-core/troubleshooting`

### Q7：集群连不上 / `ray.init(address="auto")` 报找不到集群

**如何确认**：

```bash
ray status                                  # 它自己能不能连上
echo $RAY_ADDRESS                           # 客户端读的就是这个
```

**怎么修**：

* `address="auto"` 靠 **`RAY_ADDRESS`** 或 `ray start --address=<head>` 的上下文来发现集群；
  客户端机器上没设 `RAY_ADDRESS` 就会失败。显式写地址最稳：
  `ray.init(address="ray://head:10001")`（Ray Client）或 `ray.init(address="head:6379")`。
* **Ray Client（`ray://`）不支持 Ray Data 的 dataset API**，
  需要把 dataset 调用包进 remote task 里（Databricks 文档里明确写了这个限制）。
* 集群侧没起：在 head 上 `ray status` 确认；worker 节点用 `ray start --address=<head>:6379`。

**文档页**：`cluster/...`、`ray-core/troubleshooting`

### Q35：Ray 声明的资源和我机器的实际资源对不上

**症状**：`ray.cluster_resources()` 显示的 CPU/GPU 数不对 ——
容器里常见「内核数被 Ray 按宿主机算」，于是 Ray 以为有 64 核，
实际 cgroup 只给了 8 核，结果任务**超卖**、集体变慢。

**如何确认**：

```python
print(ray.cluster_resources())     # Ray 以为的总量
print(ray.available_resources())   # 现在还能用的
# 和容器/机器的真实限制对比:
import os; print(os.cpu_count())   # ⚠️ 容器里这个值常常也不准
```

Shell 侧看真实限制：`nproc`、`cat /sys/fs/cgroup/cpu.max`（cgroup v2）。

**怎么修**：

```bash
ray start --head --num-cpus=8 --num-gpus=0     # 显式告诉 Ray 真实上限
# 或
ray.init(num_cpus=8)
```

**K8s 里这是必做项**：容器的 CPU limit 与 `--num-cpus` 必须一致，
否则 Ray 会把任务排到「它以为有、其实没有」的核上。

> 相关：开启 cgroup 资源隔离（`enable_resource_isolation`）时，
> Ray 还会额外预留系统资源，可用量比声明的更少 —— 见第 8 章 §8.2。

**文档页**：`ray-core/configure/...`（资源声明）

---

### Q8：dashboard 打不开

**如何确认**：

```bash
ray status         # 看有没有 dashboard 地址
curl -s localhost:8265 | head            # 本机能否访问
```

**怎么修**：确认装的是 `ray[default]`；`ray.init(include_dashboard=True)`；
远程访问时注意 dashboard 默认只监听 head 节点的端口 **8265**，
需要 SSH 端口转发或反代。**不要为了"方便访问"把它暴露到公网**——见 D.8。

**文档页**：`ray-observability/...`（Dashboard 章节）

---

## D.3 任务

### Q9：任务一直 PENDING，不跑也不报错

**如何确认**：按顺序查这四件事：

```python
import ray
from ray.util.state import list_tasks, list_placement_groups

ray.cluster_resources()          # 集群总共有什么
ray.available_resources()        # 现在还剩什么
list_tasks(filters=[("state", "=", "PENDING_ARGS_AVAIL")])[:5]   # ⚠️ 不是 "PENDING",见下
list_placement_groups()
```

> 🔴 **`"PENDING"` 不是一个合法状态值 —— 写它会静默返回空列表。**
> `ray.util.state` 允许的 `state` 取值来自
> **`python/ray/_private/custom_types.py` 的 `TASK_STATUS`**，里面**没有**裸的
> `"PENDING"`，只有六个**前缀型**的 PENDING：
> `PENDING_ARGS_AVAIL` / `PENDING_NODE_ASSIGNMENT` /
> `PENDING_OBJ_STORE_MEM_AVAIL` / `PENDING_ARGS_FETCH` /
> `PENDING_ACTOR_TASK_ARGS_FETCH` / `PENDING_ACTOR_TASK_ORDERING_OR_CONCURRENCY`。
> 而 `("state", "=", ...)` 是**全串等值比较**（`state_api_utils.py`），
> 不是前缀匹配 —— 所以 `"PENDING"` **不报错、不警告，直接返回空**。
> 你会得出「没有 pending 任务」的**错误结论**，然后往错误的方向排查。
>
> **正确写法**：要么逐个前缀值查（任务卡的阶段不同，值就不同），
> 要么用 `list_tasks()` 不带过滤、在客户端按前缀筛：
>
> ```python
> from ray.util.state import list_tasks
> # ① 逐个查（推荐:能看出卡在哪个阶段）
> for st in ["PENDING_ARGS_AVAIL", "PENDING_NODE_ASSIGNMENT",
>            "PENDING_OBJ_STORE_MEM_AVAIL", "PENDING_ARGS_FETCH",
>            "PENDING_ACTOR_TASK_ARGS_FETCH",
>            "PENDING_ACTOR_TASK_ORDERING_OR_CONCURRENCY"]:
>     print(st, len(list_tasks(filters=[("state", "=", st)])))
> # ② 或者在客户端筛
> pending = [t for t in list_tasks() if t.state and t.state.startswith("PENDING")]
> ```
>
> ⚠️ 顺带一提：「任务一直 PENDING」这个说法本身也是**口语化的**——
> 真正要看的是它在**哪个** PENDING 阶段（等资源 / 等节点分配 / 等对象 /
> 等 actor 并发槽），那才是原因所在。

**怎么修**：PENDING 的原因只有四类，对应四种解法：

| 原因 | 确认方式 | 修法 |
|---|---|---|
| 资源不够 | `available_resources()` 里要的资源是 0 | 减 `num_cpus`/`num_gpus`，或加节点；注意 **actor 的资源是终身持有的**，先看有没有僵尸 actor |
| 依赖未满足 | 任务的参数里有没就绪的 ObjectRef | 查上游任务是否 PENDING/失败；`list_tasks()` 看依赖链（⚠️ 就是上面 `from ray.util.state import list_tasks` 那个名字，**没有** `state.` 前缀 —— 本章别的作用域里 `state` 是 mini-ray 的模块，两者不通用） |
| 放置组没就绪 | `list_placement_groups()` 里 state 不是 `CREATED` | 放置组的 bundle 资源声明比集群还大 → 永远等不到；`ray.get(pg.ready())` 前先看资源 |
| 硬节点亲和 | 任务声明了 `NodeAffinitySchedulingStrategy(soft=False)` | 目标节点没资源就**永远等**；改 `soft=True` 或换节点 |

**文档页**：`ray-core/scheduling/...`、`ray-core/placement-group`

### Q10：`ray.get` 超时了怎么办？

**如何确认**：

```python
from ray.util.state import list_tasks
# ⚠️ 必须用具体的 PENDING_* 值,没有裸 "PENDING"(Q9 的方框里解释了为什么)
list_tasks(filters=[("state", "=", "PENDING_ARGS_AVAIL")])   # 看是不是根本没跑
```

**怎么修**：超时通常**不是超时本身的问题，而是任务没在跑**。
先按 Q9 排查资源/依赖。确认在跑的另一种可能是**任务真的慢**——
那就把 `timeout` 调大，并且**不要在生产代码里依赖 `timeout` 做流程控制**：
超时只是诊断工具。mini-ray 里超时抛 `GetTimeoutError`，消息里会提示
"对象可能还在排队（资源不足）、依赖未完成，或者上游任务失败了"。

**文档页**：`ray-core/...`（`ray.get` 的 docstring 与 troubleshooting）

### Q11：`RayTaskError` 的堆栈怎么读？

**如何确认**：

```python
try:
    ray.get(ref)
except ray.exceptions.RayTaskError as e:
    print(type(e.cause))          # 原始异常类型
    print(e.traceback_str)        # 远程 worker 里的完整堆栈
    raise e.as_instanceof_cause() # 需要按原始类型 except 时用它
```

**怎么修**：读堆栈的顺序是 **① `e.cause` 是什么异常 → ② `e.traceback_str` 里
"报错那一步在你代码的哪一行"**。`RayTaskError` 只是一个"这是远程错误"的外壳，
真正的信息全在 `cause` 与 `traceback_str` 里。mini-ray 完整复刻了这套语义
（`miniray/errors.py`），连 `as_instanceof_cause()` 的拼接方式都一样。

**文档页**：`ray-core/...`（exceptions / troubleshooting）

### Q12：`WorkerCrashedError`（OOM / 段错误）

**如何确认**：

```bash
ray logs worker --pid <PID> --err --tail 200   # worker 的 stderr 里通常有真正的死因
#                                      ^^^^^ 这个 --err 不能省!
# ⚠️ 不加 --err 读的是 **stdout**(.out),而崩溃信息几乎总在 **stderr**(.err) 里 ——
#    你会拿到一份"看起来很正常"的输出,然后误判成"没有日志"。
#    源码:python/ray/util/state/state_cli.py 里 `suffix="err" if err else "out"`
# ⚠️ 扁平写法 `ray logs --tail 200 <worker_id>` 早已不可用(子命令形态自 Ray 2.3)；
#    而且 worker 用 --pid,不是 --id(见第 11 章 §11.4、第 33 章 §33.5)
```

常见死因两种：**C 扩展段错误**（numpy/torch 的 ABI 不匹配、底层库 bug）
和**被内存监控/OOM killer 杀掉**（后者会出现
`ray.exceptions.OutOfMemoryError: 1 worker(s) were killed due to the node
running low on memory. <details>`）。
⚠️ **报错文案在 2.55 改过**：2.54 及更早是 *"Task was killed due to the node
running low on memory"*，**2.55 起换成上面的 "N worker(s) were killed …"**
（源码 `src/ray/raylet/node_manager.cc:3218`；2.58 的测试也断言新文案，
`python/ray/tests/test_memory_pressure.py`）。照旧文案去 grep 日志会**搜不到**。

**怎么修**：

* 段错误 → 看是不是同一个任务**必崩**（那就是代码/依赖问题），
  用 **Ray Distributed Debugger**（`ray debug`），或在单机上直接跑那个函数
  拿到完整堆栈（⚠️ **不要**用 `local_mode=True` —— 它已经被移除了，
  见第 11 章 §11.7）；
* 内存被杀 → 降低单任务内存（减小 batch、加 `memory=` 声明让调度器避开），
  或减少并发（把 `num_cpus` 调大以降低同节点任务数）；
* **注意语义**：`WorkerCrashedError` 意味着"进程没了、要换 worker 重跑"，
  它和"任务逻辑抛异常"是两类问题。mini-ray 里它们是两个不同的异常类
  （`WorkerCrashedError` vs `RayTaskError`）。
* **幂等性检查**：被重跑的任务如果写了文件/发了请求，会有重复副作用。

**文档页**：`ray-observability/user-guides/debug-apps/debug-memory`、
`ray-core/troubleshooting`

### Q13：任务里 `print` 的东西去哪了？

**怎么修**：worker 的 stdout/stderr 会被 Ray 转发，**加 `(pid=<数字>)` 前缀** ——
是**带值**的，不是字面的 `(pid)`：**同 IP 的另一个 worker** 输出打
**`(pid=12345)`**，**跨 IP（别的节点）** 的则带上 IP，打成
**`(pid=12345, ip=10.0.0.1)`**（源码 `python/ray/experimental/tqdm_ray.py`：
`"(pid={}) ".format(state.get("pid"))` 与
`"(pid={}, ip={}) ".format(...)`）。
⚠️ 别拿 `grep '(pid)'` 去日志里捞 —— **一条也捞不到**，要用 `grep -E '\(pid=[0-9]+'`。
本地看得到就直接看终端；集群上看 `ray logs` 或 dashboard 的日志页。
mini-ray 是**直接继承终端**，不带前缀（README 的"已知取舍"第 6 条）——
所以别拿日志格式当跨实现的判据。

---

## D.4 内存

### Q14：`ObjectStoreFullError`：对象存储满了

**如何确认**：

```bash
ray memory --sort-by=OBJECT_SIZE        # 谁在占
ray memory --group-by=STACK_TRACE       # 哪一行代码在泄漏(⚠️ 需要先开开关,见下)
ray summary objects                     # 结构化汇总
```

> 🔴 **`--group-by=STACK_TRACE` 依赖一个默认关闭的开关：`RAY_record_ref_creation_sites=1`。**
> 不设它，**所有 `call_site` 都记成 `disabled`，按栈聚合出来的东西毫无意义** ——
> 你以为在读"泄漏发生在哪一行"，实际读的是一堆 `disabled`。
> 源码依据：`python/ray/dashboard/state_aggregator.py` 里
> `callsite_enabled = env_integer("RAY_record_ref_creation_sites", 0)` —— **默认 0**。
> **开关必须在 `ray start` / `ray.init()` 之前**设好，事后补设对已创建的对象无效：
>
> ```bash
> RAY_record_ref_creation_sites=1 ray start --head ...   # 集群侧
> ```
>
> 同一个开关也决定 `state.list_objects()` 里的 `call_site` 字段与
> "对象创建位置"的告警（第 07 章 §7.5、第 11 章 §11.2、附录 A §A.7）。
> **排查内存泄漏时，这是第一个该开的开关。**
>
> （`ray memory --stats-only` 也是可用选项 —— 只打印对象存储的聚合统计、不逐条列对象；
> 已在 `ray/scripts/scripts.py` 中核实。）

**怎么修**：按引用类型定位（第 18 章的表）：

* `LOCAL_REFERENCE` 累积太多 → 你攒 ref 不释放，用 `ray.wait` 背压；
* `PINNED_IN_MEMORY` → `x = ray.get(big_ref)` 之后 x 一直活着，及时 `del x`；
* `USED_BY_PENDING_TASK` → 无界提交（第 18 章反模式 6）；
* `CAPTURED_IN_OBJECT` → 闭包捕获大对象（反模式 4）。

容量侧的旋钮：`object_store_memory`（单机）/ `ray start --object-store-memory`（集群）；
溢出配置见 Q15。**注意"满了"和"溢出"是两件事**：溢出成功就不会报错。

**文档页**：`ray-core/scheduling/memory-management`、`ray-core/objects/object-spilling`

### Q15：怎么配对象溢出？往哪溢？

**如何确认**：看当前配置是否生效——溢出发生时会在日志里出现 spill 相关记录，
`state.list_objects()` 里也能看到对象的状态变化。

**怎么修**：通过 `ray.init(_system_config={...})` 或
`ray start --system-config='{...}'`（也可以用 `RAY_<配置名>` 环境变量）：

```python
ray.init(_system_config={
    "object_spilling_config": '{"type": "filesystem", "params": {"directory_path": ["/mnt/nvme/spill"]}}',
    "object_spilling_threshold": 0.8,      # 默认 0.8
    "max_io_workers": 4,                   # 默认值(ray_config_def.h: RAY_CONFIG(int, max_io_workers, 4))
    "min_spilling_size": 100 * 1024 * 1024 # 默认值:一次至少 100MB(ray_config_def.h: 100 * 1024 * 1024)
})
```

* 溢出目标用 **本地 NVMe** 最好，别用网络盘；
* `automatic_object_spilling_enabled` 默认是开的；**有用户在 2.3/2.44 报告关不掉**
  （论坛帖），要"接近于关"可以试 `RAY_object_spilling_threshold=1.0`；
* **`object_spilling_directory` 与 `object_spilling_config` 同时给时的优先级，
  本书未确认**——用 `ray start --help` 看你手头的版本；
* **溢出是安全网，不是容量方案**：恢复时的延迟是磁盘级的。

**文档页**：`ray-core/objects/object-spilling`

### Q16：容器/K8s 里报 `/dev/shm` 不够

**如何确认**：报错文本会直接给你两个数字：

```
ValueError: The configured object store size (10.24 GB) exceeds /dev/shm size (8.0 GB).
This will harm performance. ... To ignore this warning, set
RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE=1.
```

**怎么修**：

* **首选**：把容器的 shared memory 调大（Docker `--shm-size=...`，
  K8s 用 `emptyDir: {medium: Memory}`），让它 ≥ 对象存储容量；
* 或者把对象存储调小（`--object-store-memory=`）；
* 兜底：设 **`RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE=1`** 接受慢速存储。
  **注意用这一长串**。短名 `RAY_ALLOW_SLOW_STORAGE` 虽然出现在
  `ray_constants.py` 的注释里，但那是陈旧注释 —— `services.py` 读的是长名，设短名不生效。

**文档页**：`ray-core/scheduling/memory-management`

### Q17：driver 的内存一直涨，最后被 OOM 杀掉

**如何确认**：`ray memory` 里看 `PINNED_IN_MEMORY` 与 `LOCAL_REFERENCE`；
用 `htop` 看 driver 进程的 RSS（注意看 **RSS 减去 SHR**，共享内存不算你的）。

**怎么修**：根因通常是**把大对象 `ray.get` 到了 driver**——
反序列化出来的副本住在 driver 堆里。修法：

1. 不要在 driver 聚合大结果，让 worker 之间直接传 ObjectRef（数据流）；
2. 必须落地的结果就写文件/对象存储，不要留在内存；
3. 用 `iter_batches()` 这类流式接口，别一次 `materialize()`；
4. 及时 `del` 掉不再需要的大对象和 ref（Ray 的引用计数才能回收）。

**文档页**：`ray-observability/user-guides/debug-apps/debug-memory`

### Q18：为什么 Mac 上跑同样的代码更慢？

**怎么修**：因为 **macOS 上没有 `/dev/shm` 的共享内存语义，Ray 的对象存储落到 `/tmp`
（磁盘）**。这是系统层面的差异，不是配置问题。规避方式：把计算压到 Linux 机器上，
或者在 Mac 上只做**功能验证、不做性能测量**（对象存储路径与延迟都不一样）。
⚠️ 别想用 `local_mode=True` 回避 —— 它已经被移除了（第 11 章 §11.7）。

**文档页**：`ray-core/scheduling/memory-management`

### Q19：`ray memory` 具体怎么用？

**怎么修**：

```bash
ray memory                          # 完整快照(对象 + 引用类型 + call site)
ray memory --sort-by=OBJECT_SIZE    # 按大小排序,找大对象
ray memory --group-by=STACK_TRACE   # 按调用栈聚合,找泄漏的那一行(⚠️ 需 RAY_record_ref_creation_sites=1)
ray summary objects                 # 更容易读的汇总(State API CLI)
```

> ⚠️ **`call site` 与 `--group-by=STACK_TRACE` 默认都是空的**：不开
> `RAY_record_ref_creation_sites=1`（默认 0，且**必须在 `ray start` 前设**），
> 这一列全是 `disabled`（见 Q14 的方框）。**本节的四条命令里，
> 只有第三条依赖它** —— 前两条、以及 `ray summary objects` 不受影响。

输出里每一行包含：IP/PID/进程类型、ObjectRef、对象大小、**引用类型**、call site。
**"引用类型"是这份输出里最有价值的列**——它告诉你对象被"谁"钉住了（见 Q14 的四类）。
（`call site` 那列则要先开上面的开关才有内容。）
`ray summary objects` 的官方描述是"和 `ray memory` 差不多但输出更好懂"。

**文档页**：`ray-core/scheduling/memory-management`、
`ray-observability/...`（State CLI 参考）

---

## D.5 Actor

### Q20：actor 死了怎么办？`max_restarts` 到底恢复什么？

**如何确认**：

```python
from ray.util.state import list_actors
list_actors(filters=[("state", "=", "ALIVE")])   # 看 num_restarts 与状态
```

**怎么修**：

* **默认 `max_restarts=0`，即不重启**。actor 一死，在飞的方法全部失败，
  客户端看到 `ActorDiedError`（真实 Ray 的继承链是
  **`ActorDiedError` → `RayActorError` → `RayError`**，
  见 `exceptions.py:456` 与 `:417`）。
  ⚠️ **这条链不经过 `RayTaskError`**（`RayActorError` 直接继承 `RayError`），
  所以 `except ray.exceptions.RayTaskError` **接不住** `ActorDiedError` ——
  要接就写 `except RayActorError`，或更宽地 `except RayError`。
  ⚠️ **mini-ray 的继承链不一样**：`miniray/errors.py:104,108` 把
  `RayActorError` / `WorkerCrashedError` 都做成了 `RayTaskError` 的子类，
  所以 `except RayTaskError` 在 mini-ray 上**能**接住。**两边的 `except` 子句
  不能直接照搬** —— 按 mini-ray 写的 `except RayTaskError` 迁到真实 Ray 上
  会漏掉 actor 死亡这一类。
* 设了 `max_restarts=N` 之后，actor 会重启，**但状态会丢**——
  重启跑的是 `__init__`，不是"从死前那一刻继续"。**有状态恢复必须自己写检查点。**
* 判断"该不该重启"的标准：这个 actor 的状态能不能从外部重建？
  能 → 重启有意义；不能 → 重启只会产生一个"看起来活着但数据错了"的 actor，
  比直接失败更危险。

**文档页**：`ray-core/actors/...`（Actor Fault Tolerance）

### Q21：actor 方法返回顺序乱了

**如何确认**：看 actor 的 `max_concurrency`：

```python
handle = MyActor.options(max_concurrency=4).remote()
```

**怎么修**：**并发度 > 1 时，方法之间没有执行顺序保证**（`max_concurrency=1` 时才是
严格按提交顺序）。这是设计而非 bug。如果你的逻辑依赖顺序：
把并发度设回 1，或者把需要保序的操作合并成一个方法。
另外 **async actor 的默认并发度是 1000**，所以"我明明没写 max_concurrency 但顺序乱了"
通常意味着你用了 `async def`。

**注意**：`max_concurrency>1` 的同步 actor 是**真多线程**，共享状态要加锁。

**文档页**：`ray-core/actors/concurrency`

### Q22：actor 占着 GPU 不放，任务排不上队

**如何确认**：

```python
from ray.util.state import list_actors
list_actors()                       # 看每个 actor 的资源和状态
ray.available_resources()           # GPU 是不是全被预留了
```

**怎么修**：**actor 的资源是终身持有的**（第 18 章反模式 9）。所以：

* 用完就 `ray.kill(handle)`，别让它挂着；
* 检查有没有 detached 命名 actor 泄漏（Q23）；
* 纯推理场景可以用 `num_gpus` + 常驻模型（这是**正确的**用法），
  但要按卡数起 actor，不是按请求数；
* 需要"弹性占用"时考虑 Ray Serve（它按请求压力扩缩副本）而不是裸 actor。

**文档页**：`ray-core/actors/...`、`ray-core/scheduling/resources`

### Q23：报 `The actor with name=X already exists`

**如何确认**：

```python
import ray
from ray.util.state import list_actors
list_actors(filters=[("state", "=", "ALIVE")])
# ⚠️ 注意:真实 Ray 里 **ray.list_actors 与 ray.util.list_actors 都不存在**
#   (本书早先写成"用 ray.util.list_actors 或 ray.util.state.list_actors",
#    那是错的 —— 两者并不等价,前者根本不存在)。
#   正确路径只有两条:
#     ray.util.state.list_actors()    # 列全部 actor(含匿名)
#     ray.util.list_named_actors()    # 只列命名 actor
#   mini-ray 才把 list_actors 放在顶层 —— 那是它的差异。
```

**怎么修**：上一次跑的 detached 命名 actor 还活着。三种做法：
① `ray.kill` 掉它；② 用 `get_actor` 做幂等获取（拿不到再创建）；
③ 换个命名空间或名字。**根因是 `lifetime="detached"` 的 actor 活过 driver**，
所以"跑完就没人回收"是它的固有代价。参考写法见第 18 章反模式 10 的代码。

**文档页**：`ray-core/actors/named-actors`

---

## D.6 调度

### Q24：为什么任务没被调度到我想去的节点？

**如何确认**：

```python
import ray
print(ray.nodes())                          # 节点列表与资源
from ray.util.state import list_tasks
print(list_tasks(filters=[("state", "=", "PENDING_NODE_ASSIGNMENT")]))  # ⚠️ 不是 "PENDING"
```

**怎么修**：Ray 的默认调度是 **hybrid**：按 `scheduler_spread_threshold`（默认 0.5）
把节点分成"轻载/重载"，再在 top-k 候选（`scheduler_top_k_fraction`，默认 0.2）里**随机**选。
所以"为什么没去那台空闲机器"的答案常常是**它就是随机的**。

要确定性放置，用显式约束：

```python
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
f.options(scheduling_strategy=NodeAffinitySchedulingStrategy(node_id, soft=True)).remote()
```

**别用 `soft=False` 去"逼"调度**——资源不够时它会**永远等**（Q9 的第四类原因）。

**文档页**：`ray-core/scheduling/`（Scheduling at a glance / resources）

### Q25：节点亲和与放置组有什么区别？该用哪个？

**怎么修**：

| | 节点亲和 | 放置组 |
|---|---|---|
| 粒度 | 单个任务/actor | 一组任务/actor |
| 解决的问题 | "这个任务必须在节点 X" | "这 N 个角色必须同时拿到资源（否则整体死锁）" |
| 资源语义 | 不预留 | **先原子预留**再往里放 |
| 典型场景 | 数据就在这台机器上、要读本地盘 | 分布式训练/推理的 worker 组 |
| 代价 | 几乎为零 | 预留的资源普通任务用不了 |

**判断标准**：只有一个角色 → 节点亲和；**有"要成组"的语义 → 放置组**。
两者可以叠加（放进放置组的同时指定 bundle）。

**文档页**：`ray-core/placement-group`、`ray-core/scheduling/`

### Q26：0 CPU 的任务能起多少个？

**怎么修**：**没有上限**。`num_cpus=0` 的任务/actor 不占调度资源，
官方论坛的原话是"0 CPU 的话你可以起无限个 actor 直到把系统搞崩"。
**actor 在"运行时"默认就是 0 CPU**（历史原因），所以不写资源声明的 actor 同样没有上限。

要限流，用**小数资源**而不是 0：

```python
IOActor.options(num_cpus=0.1).remote()      # 每节点最多约 10 个这类 actor
```

**注意**：`ray.wait` 背压是"限在飞任务数"，**不是"限并发度"**——
官方文档明确不建议用它限并发（会损害调度性能），限并发要用资源声明。

**文档页**：`ray-core/scheduling/resources`、`ray-core/patterns/limit-pending-tasks`

### Q27：K8s 里 pod 起不来 / head 节点上跑了业务负载

**如何确认**：

```bash
kubectl get pods -l ray.io/cluster=<name>
kubectl describe pod <pod>        # 看 Events 里的调度失败原因
```

**怎么修**：

* **给 head pod 设 `num-cpus: "0"`**（官方推荐），避免业务负载跑到 head 上跟 GCS 抢资源；
* 用了 Kueue/Volcano 时，**每个 pod 只能有一个调度器**（由 `schedulerName` 决定），
  配置漏了就会绕过 gang scheduling；
* **各调度器的 GPU 配额互不相通**（Kueue 的 ClusterQueue、Volcano 的 Queue…），
  别让它们超发同一份物理卡，否则会真的死锁；
* autoscaler 与 gang scheduling 有已知冲突（PodGroup 的 `minMember` 要随副本数更新，
  KubeRay issue #697 / #4360）。

**文档页**：`cluster/kubernetes/...`、`cluster/kubernetes/k8s-ecosystem/kueue`

---

### Q36：autoscaler 不扩容（或该缩容时不缩）

**如何确认**（按顺序排查，前两步能排掉一半问题）：

```bash
ray status                 # 看 PendingDemand 与各节点的资源
ray list nodes             # 节点数是否已经到上限
```

**六个常见原因**：

| # | 原因 | 怎么确认 | 怎么修 |
|---|---|---|---|
| 1 | **已达 `max_workers`** | `ray status` 里的上限 | 调大 `max_workers`（⚠️ 它是 **worker 节点数上限,不含 head** —— 依据是 CLI 帮助原文 *"Override the configured max **worker node count** for the cluster."*，`python/ray/scripts/scripts.py:1609`。⚠️ 本书早先引的 *"does not include the head node"* 在 2.58/2.40/2.9 的官方文档里**都检索不到**，已换成可核对的 CLI 原文） |
| 2 | **`available_node_types` 的 `resources` 写错了** | 对比真实实例规格 | Ray **不会**自动探测实例规格，必须手写正确 |
| 3 | **云配额 / 库存不足** | 看 autoscaler 日志里的创建失败信息 | 提配额、换实例类型、换可用区 |
| 4 | **`upscaling_speed` 节流** | 扩容速度被限制在「当前规模 × 该系数」 | 适当调大（但注意账单） |
| 5 | **需求本身不可满足** | 放置组里有 `{"CPU": 8}` 但节点最大只有 4 核 | **autoscaler 不会为「永远满足不了的需求」扩容** —— 改小 bundle |
| 6 | **和 K8s Cluster Autoscaler 打架** | 两侧 min/max 是否对齐 | 对齐；或只让一边负责扩缩 |

> ⚠️ **第 5 条最容易误判**：一个 PENDING 的放置组看起来"有需求"，
> 但如果它的 bundle 在任何节点类型上都放不下，autoscaler **永远不会**扩容 ——
> 它不会去猜你的意图。看到「有 PENDING 但不扩容」，先验证这个 bundle
> 在**最大的节点类型**上放不放得下。

**缩容方向**：该缩不缩通常是 `idle_timeout_minutes` 设太大，
或者节点上还有**没释放的 actor**（actor 常驻 = 节点永远不空）。
用 `ray status` 与 `state.list_actors()` 一起看。

**文档页**：`cluster/autoscaling/...`（Autoscaling 配置参考）

---

## D.7 AI 库

### Q28：Ray Data 跑着跑着 OOM

**如何确认**：

```python
ds = ray.data.read_parquet(...)
print(ds.schema(), ds.count())          # 先看数据规模
```

再看 `ray.util.state.list_tasks()` 里的并发数与每个任务的输入大小
（`from ray.util.state import list_tasks`；⚠️ **不能写成 `ray.state.list_tasks()`** ——
顶层 `ray.state` 是 `ray._private.state` 的弃用包装，**没有 `list_tasks`**）。

**怎么修**，按优先级：

1. **减小批大小**：`map_batches(fn, batch_size=...)`；一个 batch 的内存
   × 并发数 = 峰值内存，这两个都要降；
2. **降低并行度**：`map_batches(..., num_cpus=2)` 或全局
   `ray.init(...)` 的资源声明，让同节点同时跑的 block 变少；
3. **别一次 `materialize()`**：用 `iter_batches()` 流式消费；
4. **控制分块数**：用 `override_num_blocks`（**该参数的可用版本与语义请查你手头版本的
   `Dataset` docstring，本书未逐一确认**）；
5. 输出侧开溢出（Q15），并把结果尽快写出去而不是留在对象存储。

**文档页**：`data/...`（Memory Management / Troubleshooting）

### Q29：Ray Train 从 checkpoint 恢复失败

**如何确认**：

```python
import ray.train
print(ray.train.get_context())          # 确认在 V2 还是 V1 语义下
```

**怎么修**：先确认三件事：① 跑的是 Train **V2**（2.51 起默认开启；
混用 V1/V2 会**直接报错**，错误信息会提示你改 import 或设 `RAY_TRAIN_V2_ENABLED=0`）；
② 恢复用的 checkpoint 是通过 `ray.train.Checkpoint` 上报的同一个对象，
而不是你自己随手存的路径；③ checkpoint 的存储位置对所有 worker 可见
（共享存储/对象存储，而不是某个 worker 的本地盘）。
**具体 API 名称与恢复路径的写法请以你手头版本的 Train 文档为准**，V2 期间这块仍在演进。

**文档页**：`train/user-guides/...`（checkpointing / fault tolerance）

### Q30：Tune 的试验不收敛 / 跑得比手动调参还慢

**如何确认**：

```python
tuner = ray.tune.Tuner(...)
results = tuner.fit()
print(results.get_dataframe())          # 看每个 trial 的指标分布
```

**怎么修**：

* **看分布而不是看最优值**：如果所有 trial 的指标都很差，是搜索空间的问题，
  不是 Tune 的问题；
* **检查每个 trial 的资源声明**（`ray.tune.with_resources`）：声明过大 → 串行执行，
  当然慢；声明过小 → 每个 trial 自己又并行，超订 CPU；
* **加调度器早停**（ASHA 一类），否则会浪费大量算力在明显差的试验上；
* **`Tuner(trainer)` 写法已弃用**（⚠️ 本书早先写作"2.43 起"，但该版本号
  在 2.43/2.44/2.47/2.58 的 `ray/tune/tuner.py` 里**都检索不到 `deprecat`**，
  **出处未确认**），
  迁移到 V2 的函数式写法。

**文档页**：`tune/...`、`train/user-guides/hyperparameter-optimization`

### Q31：Ray Serve 请求超时 / 背压

**如何确认**：

```python
from ray import serve
print(serve.status())                    # 看部署副本数与状态
```

再看 dashboard 上该 deployment 的队列长度。

**怎么修**：

* **调副本数与并发**：`max_ongoing_requests`（每个副本的在飞请求上限，背压的核心旋钮）、
  autoscaling 的 min/max 副本数；
* **调超时**：请求超时（`request_timeout_s` 一类）要大于 P99 延迟，
  否则你会看到"服务其实在算，但客户端先超时了"；
* **检查是否被上游拖住**：下游模型/数据库慢，前面加多少副本都没用；
* 用了 Ray Serve LLM 的话，确认 KV-cache 感知路由生效，否则前缀缓存命中率上不去。

**文档页**：`serve/...`（Autoscaling / Performance Tuning）

---

## D.8 安全

### Q32：dashboard 暴露有什么风险？怎么防？

**如何确认**：

```bash
# 看 dashboard 监听在哪个地址
ray status
# 从外部机器验一下(应该是不可达)
curl -s http://<head-ip>:8265 | head
```

**怎么修**：**Ray 默认不做鉴权**，它假设集群在网络隔离的可信环境里。这不是理论风险：

* **CVE-2025-62593**（CVSS 9.4，影响 < 2.52.0）：DNS rebinding + User-Agent 绕过，
  开发者只要在浏览器里打开一个恶意页面，本机 dashboard 端口（8265）就可能被用来
  **执行任意命令**；2026-08-17 被 CISA 列入已知被利用漏洞目录。
* **CVE-2026-27482**（中危，修复于 2.54.0）：dashboard 的 DELETE 端点缺少防护。

加固清单（第 17 章展开）：

1. **不要把 8265 / 6379 / 10001 暴露到不可信网络**；用私有子网 + 安全组/NetworkPolicy；
2. 需要远程访问就用 **SSH 端口转发**或带鉴权的反代；
3. 升级到 ≥ 2.54.0，最好到 2.58；
4. **开启 token 认证**（见下一问）。

**文档页**：`ray-security/...`、`ray-core/internals/token-authentication`

### Q33：token 认证怎么开？

**如何确认**：

```bash
echo $RAY_AUTH_MODE                       # 应该是 token
ls -l ~/.ray/auth_token                   # 或用 RAY_AUTH_TOKEN_PATH 指定的文件
```

**怎么修**（依据官方 token-auth 文档）：

1. **全集群一致**地设 `RAY_AUTH_MODE=token`（`disabled` 是默认值，
   所以"只有一半节点开了"是最常见的失败模式）；
2. 准备 token，三种来源按**优先级**：
   `RAY_AUTH_TOKEN`（直接给值，优先级最高）→ `RAY_AUTH_TOKEN_PATH`（文件路径）
   → 默认路径 `~/.ray/auth_token`（Windows 是 `%USERPROFILE%\.ray\auth_token`）。
   官方**推荐文件方式**，因为环境变量会被其他读 env 的代码看到；
3. `ray start --head` 前要先 `ray get-auth-token --generate`，
   否则会抛 `AuthenticationError`；
4. **KubeRay v1.5.1+** 可以用 `authOptions` API：它自动建 Secret 并把
   `RAY_AUTH_TOKEN` / `RAY_AUTH_MODE` 注入所有 Ray 容器。
   ⚠️ **别和第 17 章 §17.2 的"v1.7 新增 token 认证"搞混** —— 那是两件不同的事：
   本条说的是 **`authOptions`（帮你在 K8s 里把 token 配好，v1.5.1+）**，
   第 17 章说的是 **基于 Kubernetes RBAC 的 token 认证集成（v1.7 新增）**。
   前者是"配置便利"，后者是"鉴权模型"，版本号不同并不矛盾；
5. 客户端（CLI、SDK、浏览器）会用 `Authorization: Bearer <token>`；
   dashboard 会让你输一次 token，然后存在 HttpOnly Cookie 里（最长 30 天）。
   **HTTP 上令牌是明文传输的，所以 TLS 要在前置代理上终止**——
   `RAY_TLS_*` 这组环境变量**确实存在**（Ray 用它加密组件间的 gRPC），
   但**具体变量名与取值本书未逐条核实**，请以你所用版本的
   `ray start --help` 与官方 "TLS / Encryption" 文档为准。
   ⚠️ 与第 17 章 §17.5 新增的"内部 RPC 默认明文"一节口径一致：
   **"token 鉴权 + TLS + NetworkPolicy" 三件要一起做**，缺任何一件都留着一个洞。

**文档页**：`ray-core/internals/token-authentication`（另有 `ray-security/token-auth`）

---

### Q37：GBDT 训练报「找不到列」/ 上线后预测全错

**症状**：训练时 `KeyError` / `ValueError: feature_names mismatch`；
或者更糟 —— **训练和评估都正常，一上线预测就全错**，而且**不报错**。

**如何确认**：

```python
# ① 训练侧:把特征顺序与类别映射打出来,存成文件
print(list(pdf.drop(columns=["label"]).columns))
print({c: dict(list(m.items())[:3]) for c, m in mapping.items()})
# ② 推理侧:加载同一个文件,逐项比对
```

**怎么修**：这是第 34 章 §34.5 与 §34.8 讲的问题 ——
**部署产物不只是模型文件**，它至少包含三样：

```
① 模型文件          model.json / model.txt
② 特征顺序 + 编码表  [("amount_log","float32"), ("city_id","int16"), ...]
                    + 类别映射 {"city_id": {"北京": 0, "上海": 1, ...}}
③ 训练参数           params / num_boost_round / 库版本
```

**② 是最常被漏掉、也最容易造成事故的一项**。
特征顺序错一位、或类别映射表训练与推理不一致，
**模型不会报错，只会给出错误的预测** —— 这类 bug 只能靠约定防住。

**根治办法**：把 ② 与模型一起进 checkpoint 的元数据
（第 35 章 §35.4 的 `Checkpoint.set_metadata()`），
并给它一个**版本号**；推理侧**只从 checkpoint 读特征清单**，不要自己维护一份。

**文档页**：第 34 章 §34.5 / §34.8、第 35 章 §35.3

### Q38：模型注册了但 `@production` 读不到 / 回滚没生效

**如何确认**：

```python
from mlflow import MlflowClient
c = MlflowClient()
print(c.get_model_version_by_alias("fraud-detector", "production"))   # 现在指向哪一版
print([m.version for m in c.search_model_versions("name='fraud-detector'")])
```

**怎么修**：按可能性排序：

1. **用了 `stage` 而不是 `alias`**。MLflow 2.x 起 `stage` 是**弃用方向**，
   新写法是 `set_registered_model_alias(name, alias, version)`。
   用 `stage` 设的"Production"和 `alias="production"` **是两套东西**，
   按 alias 读自然读不到（见第 35 章 §35.5）；
2. **代码里硬编码了版本号**（`models:/fraud-detector/17`）。
   正确写法是 `models:/fraud-detector@production` ——
   **生产代码永远按 alias 读**，否则"回滚"对你没有意义；
3. **注册与部署是两件事**。注册表只是**指针**，
   它**不会自动把模型推到线上** —— 回滚 = 改 alias **+ 让部署流程重新拉一次**。
   只改 alias 不重拉，线上跑的还是旧进程里的那个模型；
4. **`tracking_uri` 不一致**：注册写到了本地 `mlruns/`，生产读的是远端服务。
   查 `MLFLOW_TRACKING_URI`。

**文档页**：第 35 章 §35.5 / §35.6

### Q39：Jaeger 里 span 是断开的，拼不成一条链

**症状**：能看到一堆 span，每个都有耗时，但**它们不在一棵树上** ——
看起来像十几个互不相关的独立请求。

**根因（第 36 章 §36.3）**：`opentelemetry.context` 是**进程内**的，
**它跨不过 `ray.remote`** —— Ray 的调用是"序列化的函数 + 参数"，
不像 HTTP 请求那样自动带 `traceparent` 头。

**如何确认**：在 worker 里打印当前 context：

```python
from opentelemetry import trace
ctx = trace.get_current_span().get_span_context()
print(ctx.is_valid, format(ctx.trace_id, "032x"))
# 如果每个 worker 打印的 trace_id 都不一样 → context 没传过去
```

**怎么修**：三步，缺一不可：

```python
# ① 上游:把 context 塞进一个普通 dict
from opentelemetry import propagate
carrier = {}; propagate.inject(carrier)

# ② 跨进程:把它当作**普通参数**传过去
ref = downstream.method.remote(carrier, payload)

# ③ 下游:恢复出来,并显式挂到新 span 上
ctx = propagate.extract(carrier)
with tracer.start_as_current_span("downstream", context=ctx): ...
```

另外**两个同样常见的原因**：

* **采样器没用 `ParentBased`**：上游采了、下游独立掷骰子没采中，
  链路就断在中间。正确写法是
  `ParentBased(TraceIdRatioBased(0.1))`；
* **`TracerProvider` 只在 driver 里 setup 了**。它是**进程级**的，
  **worker 进程里没有** —— 每个 worker 第一次运行时必须自己 setup 一次
  （配置用 `runtime_env` 的 `env_vars` 送进去）。

**文档页**：第 36 章 §36.3 / §36.4 / §36.6

---

## D.9 提问前请准备好什么

在论坛/issue 里发问前，把下面这些准备好。**大部分问题在准备这些材料的过程中
就自己解决了**——因为你会被迫去读真正的报错。

### 1. 最小可复现（MRE）

* 一个**单文件**脚本，`python repro.py` 就能跑出问题；
* 去掉一切业务逻辑、去掉私有依赖、把数据换成随机生成的 `np.random.rand(...)`；
* 标注它**期望**什么、**实际**什么。

```python
# repro.py —— 期望:打印 [0, 1, 4];实际:卡在 ray.get
import ray

@ray.remote(num_gpus=1)          # ← 关键:确保你的机器真有 GPU
def square(x):
    return x * x

ray.init()
print(ray.get([square.remote(i) for i in range(3)]))
```

### 2. 版本信息

```bash
python -V
python -c "import ray; print(ray.__version__)"
pip show ray | head -5
uname -a                          # 或 Windows 版本
nvidia-smi                        # GPU 相关问题时必须
```

### 3. 集群状态快照

```bash
ray status                        # 资源、节点、autoscaler 状态
ray summary tasks                 # 任务状态分布
ray summary objects               # 对象与溢出情况
ray memory --group-by=STACK_TRACE # 内存问题时(⚠️ 需 RAY_record_ref_creation_sites=1,否则 call site 全是 disabled)
ray list nodes                    # 或 ray.util.state.list_nodes()
```

### 4. 完整的报错与日志

* **完整 traceback**，不要只贴最后一行；
* 如果是 `RayTaskError`，**`e.cause` 和 `e.traceback_str` 都要贴**；
* worker 侧日志：`ray logs worker --pid <PID> --err --tail 200`
  （⚠️ **`--err` 别省** —— 不加读的是 stdout，崩溃原因在 stderr）；
* **不要贴截图**。截图里的堆栈没法被搜索、没法被复制。

### 5. 你已经试过什么

把"我试过改 X、结果是 Y"写清楚。这能省掉一轮"你试过 X 吗"的往返，
也能让对方判断你的心智模型哪里偏了。

### 6. 一个反直觉但有用的建议

**先跑一遍 mini-ray 的最小例子。** 如果 mini-ray 里同样的代码行为**不同**，
那你的问题很可能在"真实 Ray 的某层机制"上（调度、对象所有权、runtime_env），
而不是在你的算法逻辑里——这能把排查范围直接砍掉一半。
`python examples/01_hello_ray.py` 是本书第 06 章的配套示例。

---

## D.10 小结

* 排错的顺序永远是：**先看 State API 与 `ray status` 的资源/状态视图，
  再看报错文本里的具体数字，最后才改参数。**
* 四类高频根因覆盖了大多数"玄学问题"：**资源不足（含 actor 终身占用）、
  依赖/放置组未就绪、无界提交导致的内存爆、鉴权/网络这类环境问题。**
* 三个值得记住的"名不对实"：**对象存储慢是 macOS 的 `/tmp`**、
  **溢出≠对象存储满**、**`RAY_max_pending_tasks` 在 2.58 不存在**。
* 仍标"未确认"的点（`RAY_TLS_*` 完整清单、Windows 上 runtime_env 的边界、
  `object_spilling_directory` 与 `object_spilling_config` 的优先级），
  **都请在你手头的版本上用 `--help` 与源码确认一次**。
  ✅ 其中 `ray memory --stats-only` 已核实为**真实存在**的 flag，不再是未确认项。
* 最后一条：**提问的质量等于你提供的可复现材料质量**。
  最小复现 + 版本号 + `ray status` 快照，这三样能解决 80% 的沟通成本。

> **第四轮补的三条（Q37–Q39）属于"不会报错的那一类"**：
> 特征顺序错、alias 没生效、trace 断开 ——
> **它们全都不抛异常，只是结果不对**。
> 这正是第 20 章 §20.9 那条建议的实例：
> **"主干稳定"不等于"全都稳定"，而最贵的 bug 是静默的那种。**
