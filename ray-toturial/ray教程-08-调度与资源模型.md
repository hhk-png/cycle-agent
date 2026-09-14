仓库地址：https://github.com/hhk-png/cycle-agent

# 第 8 章：调度与资源模型

> 本章目标：搞清楚 Ray 怎么决定「这个任务放到哪儿」。包含资源模型的全部细节、
> 两层调度与租约、策略家族、放置组的完整语义，以及**调度问题的排查方法**。

---

## 8.1 一句话概括

> **你用 `@ray.remote` 上的几个数字声明需求，Ray 负责找到满足它的节点和进程。**

调度的输入有三样，输出是一个「在哪个节点、用哪个 worker 跑」的决定：

```
输入                                          输出
────                                          ────
① 资源需求(num_cpus/num_gpus/resources)  ┐
② 依赖对象在哪(本地性)                   ├──▶  节点 + worker
③ 放置约束(亲和/放置组/标签/拓扑)        ┘
```

---

## 8.2 资源模型：全部细节

### 声明方式

```python
@ray.remote
def f(): ...                      # 默认:1 CPU,0 GPU

@ray.remote(num_cpus=2, num_gpus=1, memory=8 * 1024**3)
def g(): ...

@ray.remote(resources={"TPU": 1, "custom_thing": 2})
def h(): ...

F = f.options(num_cpus=0)         # 0 CPU:不占资源
```

集群侧：

```python
ray.init(num_cpus=32, num_gpus=8, resources={"TPU": 16})
# 或 ray start --num-cpus=32 --num-gpus=8 --resources='{"TPU": 16}'
```

### 语义要点

| 要点 | 说明 |
|---|---|
| **CPU 是记账，不是隔离** | `num_cpus=0.5` 让两个任务共享一个核；Ray 不做 CPU cgroup 隔离（实验性的 `enable_resource_isolation` + cgroup：本书早先写的"2.55 起"是错的 —— 该形参在 **2.48.0** 的 `ray.init()` 签名里就已经存在，2.51.0 亦然；它**功能上**从哪一版开始真正可用，本书**未确认**） |
| **0 CPU 任务不占资源** | 于是理论上可以无限并发 —— 真正的上限是 **worker 池大小** |
| **GPU 默认独占，可声明小数** | 通过 `CUDA_VISIBLE_DEVICES` 实现可见性隔离；`num_gpus=0.5` 让两个任务共享一张卡（**显存要自己分**）；**Ray 不会阻止你自己绕过它** |
| **资源不足 → 排队** | 不是失败。只有「请求量 > 单节点总量」才是硬错误（`TaskUnschedulableError`，永远等不到） |
| **system reserved** | 系统预留**只在开启 `enable_resource_isolation`（cgroup 隔离）时**才有那套比例；见下方说明 |

> ⚠️ **关于 system reserved 的一个常见误引**：网上流传的
> 「CPU 预留 `max(1, 5%)`、内存预留 `max(500MB, 10%)`」是
> **`enable_resource_isolation`（cgroup 资源隔离，**自 2.51 起**可用）下的参数默认值**
> `system_reserved_cpu` / `system_reserved_memory`，**不是通用默认**。
> 而且官方公式**带上下限**：CPU 是 `min(3.0, max(1.0, 5%))`、内存是
> `min(10GB, max(500MB, 10%))`。
> 不开启 cgroup 隔离时，Ray **不做**这种预留。引用前请对照当前版本文档。

### 关于 0-CPU 任务的实践建议

它是「我怎么提高并发」的答案，也是「我的集群为什么卡死」的常见原因：

```python
# ✓ 适合 0 CPU 的场景:IO 等待型(网络请求、读外部存储)
@ray.remote(num_cpus=0)
def fetch(url): ...

# ✗ 危险:CPU 密集任务声明 0 CPU → 所有人抢同一批核,反而更慢
@ray.remote(num_cpus=0)
def compute(): ...       # 什么时候该声明 num_cpus=1 就声明
```

### 异构 GPU：`accelerator_type` 与 MIG

前面的 `num_gpus=1` 只说「要一张卡」，**没说要多好的卡**。集群里混着
A100 和 H100 时，调度器可能把你的大模型扔到小卡上。`accelerator_type`
就是给 GPU 贴标签：

```python
# 集群侧:给不同节点贴上不同的加速器标签
# ray start --num-gpus=8 --resources='{"accelerator_type:A100": 1}'

@ray.remote(num_gpus=1, accelerator_type="A100")
def train(): ...          # 只要 A100 节点
```

> ⚠️ `accelerator_type` 是**自定义资源的语法糖**，不是内置概念。
> Ray 在内部把它翻译成 `resources={"accelerator_type:A100": 0.001}` 这类
> 带前缀的自定义资源来参与调度。所以**标签必须由节点侧显式声明**，
> 写错了不会报错，只会**永远排不上队**。命名规则以当前版本文档为准。

**MIG（Multi-Instance GPU）** 是另一条路：把一张物理 A100/H100 切成多个
互相隔离的实例，每个实例有独立的显存和算力。它和 Ray 的关系是：

* **Ray 不认识 MIG**。Ray 只认「这个节点上有几张 GPU」这个数字 ——
  这个数字由你通过 `--num-gpus` **告诉**它；
* 所以典型做法是**运维侧先把 MIG 实例切好**，然后告诉 Ray
  「这台机器有 7 张 GPU」（对应 7 个 MIG 实例），Ray 就会按 7 份来调度；
* **代价是显存隔离由 MIG 负责，而不是 Ray** —— 这也正是它的价值：
  相比 `num_gpus=0.5` 的「共享一张卡、显存自己分」，MIG 提供**硬件级隔离**，
  一个任务 OOM 不会连累邻居。

| 方案 | 隔离级别 | 显存 | 适用 |
|---|---|---|---|
| `num_gpus=0.5` | 无（仅记账） | 共享，自己分 | 小模型多副本、开发环境 |
| `accelerator_type` | 无（只是打标签） | 独占整卡 | 异构集群里**选对卡** |
| MIG | **硬件级** | 独立切片 | 多租户、稳定性优先 |

> MIG 的具体配置（`nvidia-smi mig`、K8s 的 MIG 资源声明）属于运维范畴，
> 且与驱动/CUDA 版本强相关，**未确认**：不同 K8s 版本暴露 MIG 资源的方式不同，
> 请以 NVIDIA 与你的 K8s 发行版文档为准。

---

## 8.3 两层调度与租约（lease）

Ray 的调度是两层（和 YARN/Mesos 的经典结构一致）：

```
┌──────────────── GCS 层(cluster_lease_manager) ────────────────┐
│  维护全局资源视图;把资源以「租约」形式授予节点的 raylet         │
└───────────────────────────┬───────────────────────────────────┘
                            │ lease
┌───────────────────────────▼───────────────────────────────────┐
│  Raylet 层(local_lease_manager)                               │
│  决定「这个任务交给哪个 worker」;可以**拒绝**租约              │
│  (本地资源被更高优先级占走 / 对象存储腾不出空间)                │
└───────────────────────────────────────────────────────────────┘
```

**为什么要 lease 而不是直接指派？** 因为 GCS 的资源视图是**最终一致**的：
它可能以为某节点有空闲，但那个节点的 raylet 刚刚把资源给了别的任务。
`lease + 拒绝 + 重试` 让调度决策保持乐观，同时不会真的超卖。

---

## 8.4 策略家族

策略实现在 `src/ray/raylet/scheduling/policy/`：

| 策略 | 何时用 | 行为 |
|---|---|---|
| `hybrid`（默认） | 绝大多数场景 | 利用率低于阈值就**堆**（pack），否则**摊**（spread） |
| `spread` | 想均匀分布、避免热点 | 总是选利用率最低的节点 |
| `random` | 大规模集群避免惊群 | 随机 |
| `node_affinity` | 想钉在指定节点 | 见下面 |
| `node_label`（alpha） | 按标签/表达式调度（如「只去有 SSD 的节点」） | `In`/`NotIn`/`Exists`/`DoesNotExist` |
| `bundle` | 放置组 | 见 8.5 |
| `topology_strategy`（**公开 API，自 2.57**；C++ 侧实现自 2.58） | NVLink 域/机架感知 | 传一个 dict，如 `{ray.io/node-id: "PACK", ray.io/gpu-domain: "STRICT_PACK"}`；⚠️ **它和 `strategy=` 互斥**，同时给会 `ValueError` |

几个关键常量（`ray_config_def.h`）：

| 常量 | 默认 | 作用 |
|---|---|---|
| `scheduler_spread_threshold` | 0.5 | hybrid 的「堆/摊」分界 |
| `scheduler_top_k_fraction` | 0.2 | 从利用率最低的前 k% 节点里随机选（防惊群） |
| `scheduler_top_k_absolute` | 1 | 至少看 k 个节点 |
| `num_workers_soft_limit` | -1（自动） | worker 池上限 |
| `max_pending_lease_requests_per_scheduling_category` | -1（不限制） | 待处理 lease 上限（背压） |

### 节点亲和（NodeAffinitySchedulingStrategy）

```python
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

nodes = ray.nodes()
target = nodes[1]["NodeID"]

@ray.remote
def f(): ...

# 硬亲和:节点资源不够就**一直等**
f.options(scheduling_strategy=NodeAffinitySchedulingStrategy(target, soft=False)).remote()

# 软亲和:等不到就退回普通调度
f.options(scheduling_strategy=NodeAffinitySchedulingStrategy(target, soft=True)).remote()
```

⚠️ 硬亲和 + 该节点资源被自己的其它任务占满 = **死锁**。
这正是放置组要解决的问题。

### 拓扑感知调度（2026 年的新东西）

**GPU domain 感知的放置组**（拓扑感知）（NVIDIA GB300 NVL72 这类
「72 卡一个 NVLink 域」的机器上，跨域通信会慢很多）：

```
拓扑策略(alpha,**具体引入版本未确认** —— 本教材的不同章节里出现过 2.56 与 2.57 两种说法,
已统一标注为未确认;请以你的版本文档为准):
    topology_strategy = {ray.io/node-id: "PACK", ray.io/gpu-domain: "STRICT_PACK"}

已知限制:仅 GB200/GB300 触发;域级只支持 STRICT_PACK;
        需要运维给节点打标签(如 ray start --labels="ray.io/gpu-domain=rack-1");
        ray.io/accelerator-type 由 Ray 从 NVML 自动附加,ray.io/gpu-domain 需要手工设置
```

官方博客给出的收益：在 GB300 NVL72 上，**RL 迭代吞吐提升约 13%**
（NVIDIA GEAR lab 验证；Ray Summit 2026 的 keynote 也引用了这个数字）。
这条线索很重要：**当硬件变成「一个机架=一个巨型 GPU」时，调度器要考虑的
从「哪台机器」变成了「哪个域」**。

### 客户端软约束：`label_selector=` 与 `fallback_strategy=`

上面的 `node_label` 策略得写在 `.options(scheduling_strategy=...)` 里。2.58 还给了
一对**直接写在任务选项上**的键，它们属于公共选项（`_common_options`，
源码 `python/ray/_common/ray_option_utils.py`），所以 `@ray.remote` 装饰器和
`.options()` 上都能用：

```python
@ray.remote(label_selector={"disk": "ssd"}, fallback_strategy=["spread"])
def read_shard(path):
    ...
```

| 键 | 类型 | 含义 |
|---|---|---|
| `label_selector` | `dict` | **软约束**：优先调度到标签匹配的节点；找不到匹配节点时按回退策略处理，**不会像硬亲和那样一直等死** |
| `fallback_strategy` | `list` | `label_selector` 找不到满足的节点时**怎么办**；不写就用默认回退 |

两个点必须记住：

* 它是**软约束**，不是 `node_affinity(soft=False)` 那种硬保证 —— 要「必须落在某类
  节点上」，仍然得用放置组或硬亲和；
* `fallback_strategy` 的**完整取值与默认值本书未确认**，以你所用版本的算子签名/文档为准。

> 这对键在 **Ray Data** 的具名 worker 选项里也能透传，坑位说明见第 12 章 §12.6；
> Ray Train 的 `ScalingConfig` 里也有同名的 `label_selector` 字段。

---

## 8.5 放置组：Ray 里最被低估的机制

### 它解决什么问题

假设你要起 4 个各需 1 GPU 的 actor：

```python
# ✗ 危险写法:逐个创建
actors = [Worker.remote() for _ in range(4)]
```

如果集群有 4 张 GPU，但其中 2 张已被别的任务占用：
前 2 个 actor 创建成功，**后 2 个永远等下去** —— 你的训练卡死在启动阶段，
而且前 2 个还占着 GPU 不肯放。

放置组的答案：**先把资源原子性地占下来，再往里放东西**。

```python
from ray.util.placement_group import placement_group
from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy

# ① 声明:4 个 bundle,每个 1 CPU + 1 GPU
pg = placement_group([{"CPU": 1, "GPU": 1}] * 4, strategy="STRICT_PACK")

# ② 等它真的就绪(资源占下来了才返回)
ray.get(pg.ready())

# ③ 把 actor 放进指定的 bundle
actors = [
    Worker.options(
        scheduling_strategy=PlacementGroupSchedulingStrategy(
            pg, placement_group_bundle_index=i
        )
    ).remote()
    for i in range(4)
]
```

### 四种策略

| 策略 | 语义 | 典型用途 |
|---|---|---|
| `STRICT_PACK` | 所有 bundle 在**同一节点** | 单机多卡训练、需要共享内存的流水线 |
| `PACK` | 尽量少用节点（装不下才换） | 一般的数据并行 |
| `SPREAD` | 尽量摊开（允许同节点） | 想分散故障域 |
| `STRICT_SPREAD` | 每 bundle 一个节点，**不允许同节点** | 高可用、跨机架 |

### 关键语义（每条都踩过坑）

* **bundle 是一个「必须能放进单节点」的资源集合**：`{"CPU": 8}` 在有 4 CPU 的
  节点上永远无法满足；
* **资源不足时放置组保持 PENDING**，等资源释放后**自动**变 CREATED ——
  这是「会一直等」而不是「失败」；
* **`placement_group_bundle_index=-1`（默认）表示「任意装得下的 bundle」**；
* **删除放置组会释放资源**（Ray 还会杀掉使用它的 actor/任务，
  因为它违背了资源承诺 —— 这是有意的语义，不是 bug）；
* **`placement_group_capture_child_tasks`** 控制子任务是否自动继承放置组
  （默认 `False`；为 `True` 时，actor 内部再起的任务也受同一个 bundle 约束）。

### ⚠️ 节点故障：组会被重新调度，**组里的 actor/任务也会按各自的容错策略被拉起**

这是放置组最容易搞错的一条语义，也是「训练跑了一夜，早上发现全挂了」的常见根因 ——
但**根因不是「Ray 不恢复」，而是「你没开重启」**。

先把三件事分开 —— 它们经常被混成一句「放置组不会恢复」：

| 问题 | 答案 |
|---|---|
| 放置组本身会不会被重新调度？ | **会。** 宿主节点故障时 Ray 把组置为 `RESCHEDULING`，并尝试在剩余节点上重新分配丢失的 bundle（优先级高于普通调度） |
| 原来跑在里面的 actor / 任务会不会被重新拉起来？ | **会尝试 —— 但受它们各自的容错策略约束。** 官方原文：*"Ray reschedules Actors and tasks that use the bundle (reserved resources) based on their fault tolerant policy once Ray recovers the bundle."* 也就是说 `max_restarts>0` / `max_task_retries>0` 的 actor 会在 bundle 恢复后重建；**默认 `max_restarts=0` 的 actor 不会被重启** |
| 重新调度一定成功吗？ | **不一定。** 资源不够时组会**无限期停在「部分创建」状态**（官方原文：*"the placement group remains in the partially created state indefinitely"*），一直等在 pending 队列里 —— 如果 autoscaler 补上机器，它还能恢复。**它不会退化成 `REMOVED`** |

所以准确的表述是：**Ray 既会找回「资源承诺」，也会按容错策略找回「你的进程」；
真正找不回来的是那些没有开重启策略的 actor。**

> 📌 **这条一度被本书写反过**（第七轮的措辞是「组里的东西不会回来」）。
> 回源码核对的结果：`src/ray/gcs/gcs_placement_group_manager.cc` 里
> `RescheduleIfStillHasUnplacedBundles()` 只要还有未放置的 bundle 就**重新入 pending 队列继续重试**，
> 状态仍是 `RESCHEDULING`；整个文件里唯一的 `UpdateState(...REMOVED)` 属于
> `RemovePlacementGroup()`（**显式删除**）路径 —— 不是「重调度失败」。
> `gcs.proto` 的枚举注释也写着 `REMOVED = "already removed and won't be reschedule"`。

后果是什么：

* 组可能正处在 `RESCHEDULING`（在重试，等它就好），也可能已经 `REMOVED`
  （**只可能是被显式删除**，等不到了）—— 不查 `state` 就分不清；
* **`max_restarts=0` 的 actor 是真的回不来** —— 但那是默认值的选择，
  不是 Ray 的能力边界。长时程训练/推理的 actor **应当显式设** `max_restarts`；
* 「组在 RESCHEDULING 中」和「组已 REMOVED」在代码里看起来都是「任务不动」，
  这才是真正难查的地方；
* 已建好的 actor 若因节点故障而死，重启后会**找不到原来的 bundle**。

**正确做法**：检测到节点故障后，**显式重建放置组**，再重建 actor：

```python
def build_cluster(pg_spec, strategy):
    pg = placement_group(pg_spec, strategy=strategy)
    ray.get(pg.ready())                      # 必须等就绪
    actors = [Worker.options(
        scheduling_strategy=PlacementGroupSchedulingStrategy(pg, placement_group_bundle_index=i)
    ).remote() for i in range(len(pg_spec))]
    return pg, actors

pg, actors = build_cluster(spec, "STRICT_PACK")
# 故障后:丢弃旧的 pg,重新来一遍 —— 不要试图复用
```

所以**生产训练脚本必须自己实现「放置组重建」这一层**。Ray Train / Ray Tune
帮你做了这件事（它们内部就是这么处理的），这也是「为什么要用 Ray Train
而不是自己写 actor 编队」的一个实际理由（见第 13 章）。

### 排查放置组：两个现成的接口

```python
pg = ray.util.get_current_placement_group()   # 在任务内部:我属于哪个组?
print(pg.id, pg.bundle_count)                 # 组 ID、bundle 数量

# 全集群视角:每个组的状态与 bundle 落点
from ray.util.state import list_placement_groups
for g in list_placement_groups():
    print(g["placement_group_id"], g["state"], g["bundles"])
    # state: PENDING / CREATED / REMOVED / RESCHEDULING
```

> `state` 字段是判断「现在到底该等还是该重建」的关键，**四个取值都要认清**：
>
> | 取值 | 含义 | 你该做什么 |
> |---|---|---|
> | `PENDING` | 还在等资源，组尚未就绪 | 等（或去查是谁占着资源） |
> | `CREATED` | 已就绪，资源已占住 | 正常提交任务 |
> | `RESCHEDULING` | 宿主节点故障，**正在尝试重新分配** | 等一会儿再查。**没开 `max_restarts` 的 actor 不会被拉起来，需要你自己重建**；开了的会自动回来 |
> | `REMOVED` | 已经废了（**只能由显式删除或创建者退出触发**，不是「重调度失败」） | 重建，别再等了 |
>
> 分不清这几个，就会在「等一个已经死掉的组」上浪费几个小时 ——
> 或者反过来，在组本来能恢复的时候急着重启。界面等价物是 Dashboard 的 Placement Group 页。

### 什么时候会踩坑

| 坑 | 说明 |
|---|---|
| 忘了 `ray.get(pg.ready())` | 直接往里放 actor 会等到天荒地老（其实也在等，但你看不到原因） |
| bundle 声明过大 | 单节点装不下 → 永远 PENDING；用 `pg.bundle_count` 与 `ray.nodes()` 对一下 |
| 忘记删除 | 资源一直被占着；`remove_placement_group(pg)` |
| 硬亲和代替放置组 | 见 8.4 的死锁场景 |
| 混用 `placement_group=` 参数 | 老写法（`@ray.remote(placement_group=pg)`）已不推荐，用 `scheduling_strategy` |

---

## 8.6 本地性（locality）：为什么你的任务被放到了"远处"

Ray 的调度器会尽量把任务放到**依赖对象所在的节点**上 —— 因为跨节点传输
是实打实的带宽成本（除非你用 RDT/NCCL）。

```
节点 A 有对象 X(1GB)         节点 B 空闲 CPU 更多
        │                            │
        └── 任务 f(X) 放哪儿? ───────┘

Ray 的选择:优先 A(省一次 1GB 传输),即使 B 更空
```

实践含义：

* **依赖加权**：任务依赖的对象越大/越关键，越应该「就地计算」；
* **`PENDING_ARGS_FETCH` 状态**就是在提醒你：任务已经分好节点了，
  正在跨节点拉数据；
* 如果发现任务总是被放到远处的节点，先看依赖对象在哪
  （`state.list_objects()` 的 `node_id`）。

---

## 8.7 worker 池与「并发上不去」

回顾第 3 章：worker 是进程，池子有上限。它的行为：

```
任务到达 ──▶ 有空闲 worker? ──是──▶ 直接派活(复用)
                │否
                ▼
         池子没满? ──是──▶ 拉起新 worker(带任务出生)
                │否
                ▼
             排队等待(状态 PENDING_NODE_ASSIGNMENT)
```

上限由 `num_workers_soft_limit` 决定（默认约等于节点 CPU 数）。
所以：

* **0 CPU 任务的并发上限 = worker 池大小**，不是无限；
* 想提高并发：调大 worker 池上限，或者把任务做成「一个任务处理一批」
  （更推荐 —— 减少调度开销）；
* **worker 空闲会被回收**（Ray 默认 **1 秒**，不是 10 秒 —— 见 §3.2 与 §18.6），
  所以「刚跑完的缓存」可能已经没了；
  需要长期存在的状态请放进 **actor**。

---

## 8.8 Autoscaler：集群自己变大变小

* **Autoscaler v2 自 Ray 2.54 起可用并持续演进**；⚠️ **是否"默认开启"本书
  未获官方发布说明确认** —— 见下面的说明；
* 它根据「待处理的任务 + 资源需求」决定加多少节点，空闲时缩容；
* 与 K8s 的关系要小心：**Ray Autoscaler 和 K8s Cluster Autoscaler 不共享状态**，
  两边的 min/max 配置必须对齐，否则会互相打架；
* Autoscaler v2 新增了「优先级感知的 worker group 选择」。

> ✅ **「默认开不开」这个标了四轮的「未确认」，第八轮已核实 —— 答案是"分路径的"。**
> 上面那些"看起来矛盾"的信号，其实**说的不是同一层**：
>
> * **组件默认**：`ray_config_def.h` 是 `RAY_CONFIG(bool, enable_autoscaler_v2, false)`，
>   `services.py` 是 `start_monitor(..., autoscaler_v2: bool = False)` ——
>   所以**裸 `ray start` 是关的**；
> * **`ray up` 那一层**：它会注入 `RAY_enable_autoscaler_v2=1` ——
>   **自 2.50.0 起是默认行为**（判据：`autoscaler/_private/commands.py` 里
>   `os.getenv("RAY_UP_enable_autoscaler_v2", "<默认值>")`，2.49.0 是 `"0"`、
>   2.50.0 起是 `"1"`；注释原文 *"The default value is 1 since Ray 2.50.0."*）。
>
> **完整的三路径对照表见第 17 章 §17.3**（含 KubeRay 的 opt-in）。
> 本书早先写的"2.54.0 起默认开启"**已核实为错**（翻转在 2.50.0）。
> 以你实际使用的版本为准，启动时看 `ray status` 里 autoscaler 的报错信息最可靠。

### 配置：用 `available_node_types`，不是 `head_node`/`worker_nodes`

**这是最常抄错的地方** —— 网上大量教程还在用 `head_node` / `worker_nodes` /
`min_workers` / `max_workers` 这套**已废弃**的写法。现行格式是按
**节点类型（node type）**组织：

```yaml
# cluster.yaml
cluster_name: demo

# 谁当 head —— 指定一个 node type 的名字
head_node_type: head

# 每个 node type 的 min/max 是**独立**的
available_node_types:
  head:
    min_workers: 1          # head 通常固定 1 台,min=max=1
    max_workers: 1
    resources: {"CPU": 8}
    node_config:            # ← 云厂商的实例规格放在 node_config 里
      InstanceType: m5.2xlarge
  gpu_worker:
    min_workers: 0
    max_workers: 8
    resources: {"CPU": 32, "GPU": 4}   # 必须与实例真实规格一致
    node_config:
      InstanceType: p3.8xlarge

# 扩缩容行为
upscaling_speed: 1.0        # 允许一次扩到当前规模的几倍(越大越激进)
idle_timeout_minutes: 5     # worker 空闲多久被回收
max_workers: 9              # 全集群 worker 节点上限(不含 head;也不是增量)
```

几个字段的语义值得单独说：

| 字段 | 含义 | 调错的后果 |
|---|---|---|
| `resources` | **必须手写**，Ray 不会自动探测实例规格 | 写多了 → 调度器以为有资源却用不了；写少了 → 白买机器 |
| `upscaling_speed` | 单次扩容的倍数上限 | 太大 → 突发流量时疯狂开机器（账单事故） |
| `idle_timeout_minutes` | worker 空闲回收时间 | 太小 → 反复创建销毁；太大 → 空转烧钱 |
| `target_utilization` | 期望利用率，低于它才缩容 | 太小 → 资源闲置；接近 1 → 频繁抖动 |
| `max_workers` | **worker** 节点数上限（**不含 head 节点**） | 与 K8s 侧上限不一致 → 两边打架 |

### 启动与关闭

```bash
ray up cluster.yaml            # 创建/更新集群(幂等)
ray exec cluster.yaml 'ray status'
ray down cluster.yaml          # 销毁(⚠️ 会删掉所有节点,确认清楚)
```

> ⚠️ **`ray down` 是不可逆的**。它会按配置销毁节点，**节点上的本地数据
> （包括溢出到本地盘的 spill 文件和没同步的 checkpoint）一起没**。
> 真正跑过训练的人都会在 `ray down` 前确认 checkpoint 已经落到对象存储 / NFS 上。
> 这也是第 13 章反复强调 checkpoint 的原因。

### 程序化请求资源

不想手动改 YAML 时，任务可以**主动声明**自己要什么：

```python
from ray.autoscaler.sdk import request_resources
request_resources(num_cpus=64, bundles=[{"GPU": 8}])   # 告诉 autoscaler:我要这些
```

这在「按需起一批 GPU 跑一轮训练」的场景里很有用 —— 但注意它是
**请求**不是**保证**：autoscaler 可能因为配额、实例库存、`max_workers`
而给不出来，所以**拿到资源前不要假设它一定到位**。

> **API 与字段名以官方 *Ray Cluster Launcher* 文档为准** —— 这套 YAML
> 的字段在版本间有过调整（**未确认**：`target_utilization` 等字段的具体默认值）。

---

## 8.9 调度问题排查手册

**第一步永远是看状态**：

```python
from ray.util import state
for t in state.list_tasks():
    # 字段名是 attempt_number(不是 num_attempts)
    print(t["state"], t["name"], t["node_id"], t["attempt_number"])
print(ray.available_resources())
print(ray.cluster_resources())
```

| 症状 | 最可能的原因 | 下一步 |
|---|---|---|
| 任务一直 `PENDING_NODE_ASSIGNMENT` | 资源被占满（常见：actor 持有） | `state.list_actors()` 看谁占着资源 |
| 任务 `PENDING_ARGS_AVAIL` | 依赖没就绪 | 往上找那个慢/失败的上游任务 |
| 任务 `PENDING_OBJ_STORE_MEM_AVAIL` | 对象存储满 | 第 7 章的四种内存事故 |
| 任务跑到了「不该去」的节点 | 本地性优先于空闲度 | 用 `scheduling_strategy` 强制 |
| 任务分到了节点但迟迟不开始 | 跨节点拉大对象（`PENDING_ARGS_FETCH`） | 减少依赖体积或用 RDT |
| 并发上不去 | worker 池上限 / 0 CPU 任务的池限制 | 调 `num_workers_soft_limit` |
| 集群有资源但没人用 | 硬节点亲和指向了资源不足的节点 | 检查 affinity 与放置组 |

---

## 8.10 mini-ray 的实现对照

| 机制 | mini-ray | 与 Ray 的差异 |
|---|---|---|
| 资源模型 | `scheduler.Resources`（CPU/GPU/memory/custom，float） | 语义一致 |
| 调度决策 | `TaskScheduler.pick_node`：放置组 → 硬亲和 → 本地性 → 最空闲 | 单策略；Ray 有 hybrid/spread/label/topology + lease |
| 依赖本地性 | `_locality_score()`：数依赖对象在该节点上的个数 | 简化版（Ray 还考虑关键路径） |
| 放置组 | `allocate_placement_group()` 四种策略 + `reserve_placement_group()` 预留 | 语义一致（含「资源不够就等」） |
| 节点亲和 | `NodeAffinitySchedulingStrategy(soft=False)` | 无 `_spill_on_unavailable` |
| worker 池 | `WorkerPool`（subprocess，按需拉起，可配空闲退休） | 无 worker 缓存复用池的精细控制 |
| autoscaler | 无 | 明确不做 |
| 拓扑/标签调度 | 无 | 明确不做 |
| 多节点 | `num_nodes` 模拟（资源均分 + 每节点一个对象存储） | 用于观察调度语义 |

---

## 8.11 本章小结

* 资源就是几个数字：CPU 是**记账**不是隔离、GPU 靠 `CUDA_VISIBLE_DEVICES` **独占**、
  **0 CPU 任务不占资源**所以并发上限由 worker 池决定。
* 调度是**两层 + 租约**：GCS 发 lease、raylet 决定 worker，可以拒绝重试。
* 策略家族：`hybrid`（默认，阈值 0.5）、`spread`、**`random`**、`node_affinity`、
  `node_label`（alpha）、`bundle`（放置组）、`topology_strategy`（NVLink 域感知，
  **公开 API 自 2.57、C++ 实现自 2.58** —— 见 §8.4 的策略表与第 1 章 §1.4 注①，
  两处已对齐）。
* **放置组是「先占资源再放东西」**：四种策略、bundle 必须能放进单节点、
  资源不足会一直等（这是特性不是 bug）、删除会释放资源。
* **放置组会恢复「资源预留」，组里的 actor/任务则按各自的容错策略恢复**：宿主节点
  故障时 Ray 把组置为 `RESCHEDULING` 并重找 bundle，**恢复后按各自的容错策略重启
  使用该 bundle 的 actor/任务**（官方原文：*"based on their fault tolerant policy"*）。
  所以「训练跑了一夜全挂了」的常见根因**不是「Ray 不恢复」，而是「没开重启策略」** ——
  **默认 `max_restarts=0` 的 actor 不会被拉起**，长时程负载应当显式设置它。
  资源不够时组**无限期停在「部分创建」状态**（不会退化成 `REMOVED`），
  autoscaler 补上机器还能恢复。
  动手前先看组的 `state`：`PENDING` / `CREATED` / `RESCHEDULING` 说明组还在，
  `REMOVED` 是**被显式删除**、只能重建 —— 光看「任务不动」是分不出来的。
* Autoscaler v2（`enable_autoscaler_v2`）**默认 `false`、是 opt-in**：
  别以为装好 Ray 就自动有了新的扩缩容逻辑（见 §8.8）。
* 调度器**优先本地性**：依赖对象在哪就把任务放哪，代价是可能不用最空的节点。
* 排查调度问题看**任务状态**：`PENDING_NODE_ASSIGNMENT`＝资源、
  `PENDING_ARGS_AVAIL`＝依赖、`PENDING_ARGS_FETCH`＝跨节点拉数据。

下一章讲 Actor：邮箱、并发模型、异步 actor，以及那些「看起来能用但会咬人」的写法。
