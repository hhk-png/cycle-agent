仓库地址：https://github.com/hhk-png/cycle-agent

# 第 7 章：对象存储与内存管理

> 本章目标：把「对象存储」这个 Ray 的性能地基讲透 —— 它的容量、位置、
> 零拷贝的适用条件、引用的生命周期、溢出的三层结构，以及**内存出问题时怎么查**。
> 这一章的内容直接决定你能不能在生产里稳住内存。

---

## 7.1 为什么需要对象存储

先回答一个基础问题：分布式框架为什么需要一个「对象存储」，
而不是像 RPC 那样直接传值？

| 如果直接传值 | 有对象存储 |
|---|---|
| 一个对象被 N 个任务用 → 拷贝 N 次 | 存一份，N 个消费者零拷贝读 |
| 大对象在网络上重复传输 | 同节点内存共享，跨节点只传一次（拉进本地 plasma） |
| 任务的返回值必须立刻送回调用方 | 结果先落对象存储，调用方什么时候取都行 |
| 无法做依赖的「就绪」语义 | 对象的就绪状态就是依赖的触发器 |
| 故障后中间结果全丢 | 中间结果在对象存储里，可被 lineage 重建引用 |

**一句话**：对象存储是「不可变数据的共享内存 + 依赖图的物化点」。

---

## 7.2 容量、位置与上限

| 项 | 默认值 | 怎么改 |
|---|---|---|
| 容量 | **可用内存的 30%** | `ray.init(object_store_memory=…)` / `ray start --object-store-memory=…` |
| 上限 | 200 GB | 常量 `DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES`（`ray_constants.py`，**没有 `RAY_` 前缀**） |
| 下限 | 75 MiB | 太小会拒绝启动 |
| 位置（Linux） | `/dev/shm`（**内存文件系统**） | —— |
| 位置（macOS） | `/tmp`（**磁盘**） | 官方明确说性能受影响 |
| 慢存储保护 | 阈值 10GB | `REQUIRE_SHM_SIZE_THRESHOLD = 10**10`：当**对象存储容量**超过 10GB 而 `/dev/shm` 不够时，Ray 默认拒绝启动以避免疯狂 swap；要强行放开设 `RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE=1`（`services.py` 读的就是这个名字） |

> **关于两个变量名**：`RAY_ALLOW_SLOW_STORAGE` 这个短名**并非"查无依据"** ——
> 它确实出现在 `ray_constants.py` 紧跟 `REQUIRE_SHM_SIZE_THRESHOLD` 的注释里
> （*"…unless the user sets RAY_ALLOW_SLOW_STORAGE=1"*）。
> 但那行注释是**陈旧的**：真正被 `services.py` 读取、能生效的名字是
> `RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE`。**用长名，别用短名。**

### `/dev/shm` 到底要多大？—— 算一遍

这是部署时最常算错的一个数。链条是这样的：

```
对象存储容量 = min(可用内存 × 0.3, 200 GB)     ← 常量 DEFAULT_OBJECT_STORE_MEMORY_PROPORTION = 0.3
        │
        ▼
Plasma 会把这部分**预分配**在 /dev/shm 里
        │
        ▼
所以 /dev/shm 必须 ≥ 对象存储容量,否则启动报错或频繁溢出
```

**算例**：一台 64 GB 内存的机器 → 对象存储 ≈ 19.2 GB → `/dev/shm` 至少要
给到 20 GB 以上。

```bash
df -h /dev/shm           # 先看现在多大
# Docker: 默认只有 64MB,必须显式放大
docker run --shm-size=24g ...
# K8s: 用 emptyDir 挂 medium: Memory 到 /dev/shm,并计入 pod 的 memory limit
```

> ⚠️ **K8s 里最容易犯的错**：给 pod 设了 `memory: 64Gi`，就以为对象存储能拿 19 GB。
> 但 `/dev/shm` 默认挂的是**节点磁盘**或一个小 `emptyDir`，
> 而对象存储**预分配**，于是 pod 一起就被 OOMKilled。
> 正确做法是把 `/dev/shm` 显式声明成 `medium: Memory` 的 `emptyDir`，
> 并让 `sizeLimit` + `object_store_memory` 都落在 pod 的 memory limit 之内。

**两个反直觉的点**：

* **调大 `object_store_memory` 不一定更好**：它是预分配的，
  拿走的是**同一台机器上 worker 的内存**。给太多 → worker 被 OOM killer 杀；
* **调太小则频繁溢出**：对象还没被消费就被写盘，吞吐掉下来。
  经验区间是**可用内存的 20%–30%**，其余留给 worker。

两个必须记住的现实问题：

**① Docker 里的 `/dev/shm` 默认只有 64MB。**

```bash
docker run --shm-size=2g ...        # ← 不设这个,Ray 启动时会报错或频繁溢出
```

**② 声明过大反而危险。** 对象存储是**预分配**的（plasma 会先把共享内存占住），
如果声明得比实际可用内存还大，会挤压 worker 的内存，导致任务被 OOM killer 杀掉。
实践建议：**给对象存储 20–30%，其余留给 worker**；K8s 里要注意 pod 的
memory limit 与 `object_store_memory` 的关系（后者算在前者里）。

---

## 7.3 对象的生命周期：四个阶段

官方文档把对象生命周期拆成四步（*Object Spilling* 与 *Memory Management* 两页）：

```
① Creation      任务返回值 / ray.put → Create RPC → 在 plasma 里分配共享内存
      │
② Pinning       有进程拿到「零拷贝引用」时 → PinObjectIDs RPC → 钉住
      │           (被 pin 的对象不能被驱逐、不能被溢出)
      │
③ Consumption   消费者零拷贝读共享内存
      │
④ Deletion      所有引用消失(unpin + 引用计数归零)→ 释放共享内存
```

**②为什么必须存在**：没有 pin 的话，一个正在被读的对象可能在「引用计数归零的瞬间」
被回收，而持有指针的进程还在这块内存上算 —— 直接段错误。

Ray 里 pin 的生命周期**跟着这块内存的客户端引用**（`PlasmaBuffer`），
不跟着 ObjectRef。这个区别在下面这个场景里很关键：

```python
arr = ray.get(ref)     # arr 是零拷贝视图,pin 建立
del ref                # ObjectRef 没了,但 arr 还在用 → pin 必须还在!
use(arr)               # 不能因为 ref 被删就把内存回收了
```

---

## 7.4 零拷贝：什么时候有、代价是什么

### 有零拷贝的情况

```python
@ray.remote
def make():
    return np.zeros((1000, 1000))       # 8MB 数组

ref = make.remote()
arr = ray.get(ref)                       # 同节点:零拷贝视图
arr.flags.writeable                      # False
```

Ray 对 numpy 数组做了**特判**：数组的原始缓冲区直接写进 plasma，
消费者（同节点）拿到的是共享内存上的视图。

### 没有零拷贝的情况

| 场景 | 代价 |
|---|---|
| 跨节点读（节点 A 的对象给节点 B 用） | 一次网络/进程间拷贝（Ray 会先拉进 B 的 plasma） |
| 非连续数组 / 结构化 dtype 里的对象引用 | 退回 pickle 路径（会拷贝） |
| 小数组（几 KB） | 有零拷贝，但意义不大（元数据开销占比高） |
| 读的时候做了转换（`.astype()`、切片成新数组） | 你自己触发了一次拷贝 |

### 只读不是「限制」，是「契约」

```python
arr = ray.get(ref)
arr[0] = 1          # ValueError: assignment destination is read-only
editable = arr.copy()   # 想改就复制一份
```

如果允许改，所有共享这块内存的读者都会看到「别人改到一半」的数据 ——
这是共享内存最经典的竞态。Ray 用「不可变」这个约定把问题从根上消掉。

> **mini-ray 的实现**：`object_store.create_ndarray_view()` 在消费者进程里
> `np.ndarray(buffer=shm.buf, offset=…)` 建视图并设 `writeable=False`；
> pin 通过 `weakref.finalize(array, release_pin, oid)` 挂在数组上
> （见第 6 章 6.5）。

---

## 7.5 引用计数与 owner：内存什么时候被回收

### owner 是谁

创建 ObjectRef 的那个进程就是该对象的 owner，它负责引用计数。
这里有一个容易被忽略的规则：

> **owner 进程死了，其它进程手里的 ObjectRef 会失效** —— 拿到的是
> `OwnerDiedError`（不是 `ObjectLostError`，两者的区别见第 10 章 §10.4）。
> ⚠️ 但**类型上 `OwnerDiedError` 是 `ObjectLostError` 的子类**
> （`ray/exceptions.py`），所以 `except ObjectLostError` 会把两者一起捕获 ——
> 要区分必须把 `OwnerDiedError` 写在前面（见第 5 章 §5.5）。

所以「把 ref 交给一个短命任务持有」是危险的：

```python
@ray.remote
def producer():
    return np.zeros(10**8)         # 这个任务的返回值,owner 是提交它的 driver

# 但如果是这样的写法:
@ray.remote
def inner():
    return big_task.remote()       # ← 返回一个 ref:owner 是 inner 所在的 worker!
ref = ray.get(inner.remote())      # inner 结束后,ref 可能已失效
```

Ray 会尽量做引用计数的**归属传递**（owner transfer），但这类「用 ref 组成
ref 链」的写法仍然是最容易出问题的模式。实践建议：**不要把 ref 当返回值传来传去**，
要让数据本身流动。

### 引用计数决定回收时机

Ray 的策略比 mini-ray 激进：**引用归零后就会安排回收**（mini-ray 是内存压力下才回收）。
但**注意「安排」不等于「瞬时」** —— 引用计数的上报是**异步批量**的，
从归零到内存真正释放之间隔着一个 flush 周期：**上限**是
`free_objects_period_milliseconds`（默认 **1000 ms**），但本地缓存里
攒满 `free_objects_batch_size`（默认 **100**）个对象就会**提前** flush，
所以高吞吐时通常远快于 1 秒（见第 6 章 §6.5）。
**因此「`del` 了大对象但内存没立刻降」是正常现象，不是泄漏。**
这意味着 Ray 的内存曲线是「该降就降」，但也意味着**你的 ref 必须一直活着**：

```python
refs = [f.remote(i) for i in range(1000)]
# 如果你把 refs 丢了但还要用 → 对象已经被回收 → ObjectLostError
```

### 排查内存的三件工具

```bash
ray memory --stats-only       # 对象存储的聚合统计(总量/对象数/各状态占比)
ray memory                    # 列出每个对象 + 引用它的地方(call_site!)
ray status                    # 集群资源与节点状态
```

```python
from ray.util import state
for obj in state.list_objects():
    # ⚠️ 字段名是 object_size(单位:字节),不是 size
    print(obj["object_id"], obj["object_size"], obj["reference_type"], obj["call_site"])
```

> ⚠️ **两个容易踩的细节**：
> ① 字段名是 **`object_size`**（字节），写 `obj["size"]` 会直接 `KeyError`；
> ② **`call_site` 默认不记录** —— 需要先设 `RAY_record_ref_creation_sites=1`
> （在 `ray.init(_system_config={...})` 或启动集群时设），否则拿到的是空值。
> 这也解释了为什么很多人「明明照着文档写了却看不到调用点」。

`call_site` 会直接告诉你是**哪一行代码**创建了这个引用 —— 这是排查泄漏最快的路径。

---

## 7.6 对象溢出（Spilling）：三层结构

内存不够时，Ray 的做法是**写盘而不是丢数据**。它由三层组成（官方文档明确写出）：

```
① 检测    Plasma 存储线程的 CreateRequestQueue 发现「快 OOM 了」
            │
② 编排    Raylet 主线程的 LocalObjectManager:按 LRU 选牺牲者,检查 pin
            │
③ 执行    Python IO worker 进程池(默认 4 个)真正写盘
```

**关键设计**：plasma 存储和 raylet 主事件循环跑在**不同线程**，
所以「溢出一块 1GB 的对象」不会阻塞调度。

### 参数表（源码 `ray_config_def.h`）

| 参数 | 默认 | 含义 |
|---|---|---|
| `automatic_object_spilling_enabled` | `true` | 自动溢出总开关 |
| `object_spilling_threshold` | `0.8` | 用到 80% 就开始溢（留 20% 余量给在飞的对象） |
| `object_spilling_directory` | 空 | 溢出目录（**优先级高于** `object_spilling_config`） |
| `object_spilling_config` | 空 | 溢出目标配置（可指向 S3 等外部存储） |
| `max_io_workers` | 4 | 溢出/恢复的 IO 进程数 |
| `min_spilling_size` | 100 MB | 攒够这个量再写（避免小对象碎片化写盘） |
| `verbose_spill_logs` | 2 GiB | 每累计溢出这个量级打一条日志 |
| `local_fs_capacity_threshold` | `0.95` | 溢出盘用到 95% 就停止溢出，改为报 `OutOfDiskError` |
| `max_fused_object_count` | 2000 | 单次批量溢出最多合并多少个对象 |
| `max_spilling_file_size_bytes` | 依版本 | 单个溢出文件的上限，超过则切分 |
| `buffer_size` | 依版本 | 溢出/恢复时的 IO 缓冲区大小 |

**默认溢出目录**落在**节点自己的临时目录**下 ——
`python/ray/_private/node.py` 里的策略注释写得很明确（逐字）：
*"If the head node doesn't specify an object spilling directory, and the worker node
doesn't specify one, **use the `temp_dir` of the worker node as the object spilling
directory**."*（第 3 条策略。）
⚠️ 常见写法是 `/tmp/ray/session_*/spill`，但 **`spill` 这一级是不是默认就有的，
本书未确认** —— 能确认的是"**在 `temp_dir` 下**"。

真正要记住的是结论而不是路径：**它不在共享存储上**，
所以节点一挂，溢出文件跟着丢（这点在后面「什么时候不该依赖溢出」里再强调）。
复核命令：
```bash
curl -s https://raw.githubusercontent.com/ray-project/ray/ray-2.58.0/python/ray/_private/node.py | sed -n '1696,1702p'
```

**溢出到对象存储（S3 / NFS）的配置形态**：

```python
import json

# ⚠️ 两个高频错误,先看清楚:
#   ① 溢出配置是**系统配置**,不是 ray.init 的形参 ——
#      必须放进 _system_config,写成 ray.init(object_spilling_config=...) 会报
#      "unexpected keyword argument"。
#   ② 后端类型里**没有 "s3"**。S3 走的是 smart_open 后端(靠 URI 识别),
#      本地/NFS 走 filesystem 后端。

# 溢出到 S3 / GCS / HDFS(借 smart_open 的 URI 前缀)
ray.init(
    _system_config={
        "object_spilling_config": json.dumps({
            "type": "smart_open",
            "params": {"uri": "s3://my-ray-spill/spill/"},
        })
    }
)

# 溢出到本地 SSD / NFS(注意参数名是 directory_path,不是 directory)
ray.init(
    _system_config={
        "object_spilling_config": json.dumps({
            "type": "filesystem",
            "params": {"directory_path": "/mnt/nvme/ray-spill"},
        })
    }
)
```

> ⚠️ 溢出到 S3 这类**慢存储**时，Ray 会要求你**显式确认**
> （即上面提到的 `RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE=1` 类保护）。
> 原因很实在：S3 的延迟比本地盘高一到两个数量级，
> 「溢出」会从「稍微慢一点」变成「吞吐直接崩掉」。
> **溢出到本地 SSD 是扩容，溢出到 S3 是兜底** —— 别把它当性能方案。
> 具体字段名与支持的后端**以当前版本文档为准**（**未确认**：字段结构在版本间调整过）。

观测方式：

```bash
# raylet 日志里会看到:
#   local_object_manager.cc: Spilled 50 MiB, 1 objects, write throughput 230 MiB/s
ray memory            # 输出里的 "Aggregate object store stats" 段落
```

### 什么时候不该依赖溢出

* **溢出到本地磁盘只解决容量，不解决延迟**：读回来要等 IO；
* **节点故障时溢出文件也丢**（如果溢出目录不在共享存储上）；
* **对象大于可用内存时**：溢出不解决问题，只会疯狂 swap —— 这才是
  Ray 对「慢存储」做保护（需要显式设 `RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE=1`）的原因。

**正确姿势**：让「单个对象的最大值」远小于对象存储容量，
把大结果切成块（用流式生成器或 Ray Data）。

---

## 7.7 实战：四类内存事故与处置

### 事故 1：driver 内存爆（但对象存储没满）

**症状**：driver 进程 RSS 一直涨，最后 OOM。
**根因**：`ray.get` 把大对象**反序列化到 driver 进程**。对象存储没爆，
但 driver 自己装不下。

```python
big = ray.get(giant_ref)              # ✗ 800MB 进了 driver 的堆
for chunk in stream.remote():         # ✓ 用流式生成器逐块处理
    process(ray.get(chunk))
```

### 事故 2：对象存储满（`ObjectStoreFullError`）

**排查顺序**：

1. `ray memory --stats-only` 看总量与对象数；
2. `ray memory` 找 `call_site` 里「不该存在」的对象；
3. 检查是否有 pin 住的巨大数组（正在被某个长期任务读）；
4. 看溢出是否在工作（`spilled` 计数是否在涨）。

**处置**：调大 `object_store_memory`（同时确认机器/容器有内存）、
开启/调整溢出目录、减少同时在飞的对象（背压）、切小对象。

### 事故 3：worker 被 OOM killer 杀（`WorkerCrashedError`）

**症状**：任务报 `WorkerCrashedError`，日志里有 `Killed`。
**根因**：worker 自己的堆爆了（不是对象存储）。常见来源是
「一个任务处理了太多数据」或者「模型加载 + batch 太大」。
**处置**：把任务拆小；给任务声明 `memory=`（⚠️ 内存**从 Ray 2.0 起就参与调度**——
`memory` 一直是 `_resource_option` 的正式成员。本书早先写的"2.55 起"是错的）；
降低 batch；用 actor 常驻模型避免重复加载。

### 事故 4：内存曲线「阶梯上升不下降」

**根因**：某处持续持有 ObjectRef（缓存、闭包、全局容器）。
**处置**：`ray memory` 看谁的引用一直挂着；把「缓存」改成自己管理生命周期的
actor 或外部存储；确认没有把 ref 塞进长生命周期容器。

### 附：内存监控与 OOM Killer 的机制

上面四类事故里，「worker 被杀」的**执行者**是 Ray 自己（不是 Linux OOM killer）——
Ray 在**每个节点的 raylet 进程内部**跑一个**内存监控组件**（memory monitor），
主动杀任务来保命。
理解它的行为，才能解释那些「为什么偏偏杀了我这个任务」的问题。

> ⚠️ **一个措辞上容易写错、也真的被写错过的地方**：它是
> **raylet 内的一个成员，不是一个独立的进程**。
> 源码证据：`src/ray/raylet/node_manager.h` 里它是一个成员变量
> `std::vector<std::unique_ptr<MemoryMonitorInterface>> memory_monitors_;`，
> 由 `MemoryMonitorFactory::Create(...)` 在 raylet 内部创建；
> 而在 `python/ray/_private/services.py` 里 grep `memory_monitor`
> **一个进程都没有**（那里只有 log monitor 与 autoscaler monitor）。
> 复核命令：
> ```bash
> curl -s https://raw.githubusercontent.com/ray-project/ray/ray-2.58.0/src/ray/raylet/node_manager.h | grep -n "memory_monitors_"
> curl -s https://raw.githubusercontent.com/ray-project/ray/ray-2.58.0/python/ray/_private/services.py | grep -c -i "memory_monitor"   # → 0
> ```
> **为什么值得较真**：如果是独立进程，那"监控进程自己挂了"就是一个合理的怀疑方向；
> 而它既然是 raylet 的一部分，**raylet 挂了它就一起没了** ——
> 排查方向完全不同（转而去查 raylet 的存活与日志，见第 11 章 §11.4）。

⚠️ **先纠正一个流传很广的说法**：Ray 并不是「一律按内存占用从大到小杀」。
**杀谁要分两路看**（官方 OOM Prevention 文档的规则是**先分流**）：

| 候选 | 选择规则 |
|---|---|
| **idle / 可被杀的 worker** | **优先选内存占用最大的那个**（官方原文：*"select the worker with the largest memory footprint first"*）。⚠️ 冷启动就 idle 的 worker 还要**超过 `RAY_idle_worker_killing_memory_threshold_bytes`（默认 1 GiB）**才会被杀 |
| **active worker** | 2.56 起默认 **time-based worker killing policy**（`worker_killing_policy_by_group` 默认 `false`）：**① 可重试的优先被杀**（`max_retries>0` / `max_restarts>0`）→ ② 同级里「运行更短/更新」的优先 → ③ task 优先于 actor → ④ **必要时一次杀多个** |

所以「**不是**杀占用最多的」这句话**只对 active worker 成立** —— 对 idle worker
恰恰相反。第 10 章 §10.3 是同一套规则，可互看。

流程大致是：

```
内存监控按固定间隔采样节点内存(memory_monitor_refresh_ms, 默认 250ms)
        │
        ▼  超过阈值(memory_usage_threshold, 默认 0.95)
分流:这个 worker 是 idle 还是 active?
        │
        ├── idle → 选内存占用最大的那个(冷启动就 idle 的还要超 1 GiB 阈值)
        │
        └── active → 按 time-based 策略:
                     ① 可重试的优先 → ② 运行更短/更新 → ③ task 优先于 actor
        │
        ▼
杀掉(SIGKILL),记录 WorkerExitType = NODE_OUT_OF_MEMORY
        │
        ▼
必要时**一次杀多个**,直到降到阈值以下   ← 不是"一次只杀一个"
```

**为什么这个顺序重要**：它优先牺牲「**重试代价最小**」的任务 ——
可重试的任务被杀掉能自动重跑，不可重试的（`max_retries=0`）杀掉就永久失败。
所以你会看到「**声明了重试的任务反而先被杀**」，这不是 bug，是设计。

**实际表现**：内存超标的集群会**连续丢任务**，
表现为「任务莫名其妙随机失败、且每次失败的不是同一个」。
看到这种模式，就该怀疑节点内存压力，而不是去查任务本身。

> **想回到旧行为（active worker 一路）**：设 `RAY_worker_killing_policy_by_group=true`。

| 参数 | 默认 | 含义 |
|---|---|---|
| `memory_usage_threshold` | `0.95` | 节点内存用到 95% 就开杀 |
| `memory_monitor_refresh_ms` | `250` | 采样间隔（毫秒） |
| `RAY_idle_worker_killing_memory_threshold_bytes` | `1 GiB` | 冷启动就 idle 的 worker 要超过这个占用才会被杀 |
| `RAY_memory_monitor_refresh_ms=0` | —— | **关掉内存监控**（调试用，生产别关） |

> ⚠️ **关掉内存监控不是解决方案**：关掉之后 Ray 不再主动杀任务，
> 但 Linux 的 OOM killer 会接手 —— 而它可能杀掉 **raylet 或 GCS**，
> 那就从「丢一个任务」升级成「丢一个节点」。宁可让 Ray 杀，也别让内核杀。
> 参数与默认值以官方 *Ray OOM Prevention* 文档为准，**版本间有调整**。

**怎么区分「Ray 杀的」和「自己崩的」**：看 `WorkerExitType`。
`NODE_OUT_OF_MEMORY` = Ray 因内存压力杀的；
`WORKER_DIED` / 段错误之类 = worker 自己崩的（见第 10 章 §10.3）。
这个区分直接决定你该去调容量，还是去查代码。

---

## 7.8 Ray Direct Transport：让数据留在 GPU 上（选读）

前面讲的是 CPU 侧的对象存储。当数据是 **GPU 上的张量**时，
把它搬回 CPU 再传给下一个 actor 是巨大浪费。RDT（Ray Direct Transport，
**2.48 引入 API；作为一个模块最早出现在 2.55**；
⚠️ "2.50 公布 alpha"这个流传很广的说法**未确认** ——
版本号的口径与三条复核命令见第 1 章 §1.4 注③与附录 C 的 RDT 条）解决这件事：

```python
@ray.remote
class Model:
    @ray.method(tensor_transport="nccl")     # 或 "gloo"
    def forward(self, batch):
        return self.net(batch)               # 返回值留在 GPU,直接走 NCCL
```

限制（官方文档明确）：**当前只支持 actor 任务返回的 `torch.Tensor`**，
传输后端有 Gloo / NCCL / NIXL（RDMA）。它是 alpha，接口可能变。

---

## 7.9 mini-ray 的实现对照

| 机制 | 真实 Ray | mini-ray |
|---|---|---|
| 分配器 | Plasma（C++，mmap 匿名文件 + 分桶） | `SharedMemoryAllocator`（`multiprocessing.shared_memory` + 2 的幂分桶 + free list） |
| 零拷贝 | ✅ | ✅（跨进程写穿有测试） |
| 只读 | ✅ | ✅（`writeable=False`，可用环境变量放开做实验） |
| pin 的释放 | `PlasmaBuffer` 生命周期 | `weakref.finalize` 挂在**数组**上 |
| 回收策略 | refcount=0 后**批量上报**释放（`free_objects_period_milliseconds` 默认 1000 ms、`free_objects_batch_size` 默认 **100**，**非瞬时**） | **内存压力下**才回收（`reclaim_only`） |
| 溢出 | 三层 + 独立 IO 池 + mmap 读回 | 同步溢出（`_spill_locked`），恢复时读回内存 |
| 对象目录 | GCS + raylet 缓存 | GCS `objects: oid → [node_id]` + raylet 缓存 |
| 跨节点 | ObjectManager pull（可 RDMA/NCCL） | 一次拷贝（不缓存） |
| 容量 | 30% 内存，上限 200GB | 默认 512MB（可配），`num_nodes` 均分 |

**mini-ray 有意偏保守的一点**：只在内存压力下回收 refcount=0 的对象。
真实 Ray 会**主动**释放（引用归零后经由批量上报释放，比 mini-ray 积极得多），
但那需要引用计数完全准确 —— 教学实现选择「宁可不回收，也不提前回收」。
（⚠️ 别把这里读成"Ray 立即回收"：**批量上报不是瞬时**，
`free_objects_period_milliseconds` 默认 1000 ms，见 §7.5 与第 3 章 §3.6。）

---

## 7.10 本章小结

* 对象存储 = **不可变数据的共享内存 + 依赖图的物化点**，是 Ray 性能的地基。
* 容量默认是可用内存的 **30%**（上限 200GB，下限 75MiB）；
  Linux 在 `/dev/shm`，**macOS 落磁盘**；Docker 要记得 `--shm-size`。
* 对象生命周期四步：创建 → pin → 消费 → 删除；**pin 跟着消费者对内存的引用走**，
  不跟着 ObjectRef。
* 零拷贝只对「同节点 + numpy 数组」成立，且拿到的是**只读**视图；
  跨节点、非连续数组都会回落成拷贝。
* 引用计数决定回收 —— 但**不是瞬时**：释放是批量上报的
  （`free_objects_period_milliseconds` 默认 **1000 ms**、
  `free_objects_batch_size` 默认 **100**），归零到内存真正释放隔一个 flush 周期
  （§7.5 与第 2 章 §2.1、第 3 章 §3.6 三处口径已统一）；**owner 死了引用失效**，
  所以不要拿 ref 当返回值传来传去。
* 溢出是三层结构（检测/编排/执行），默认用到 80% 触发、
  4 个 IO worker、最小溢出块 100MB；定期看 `ray memory` 与 raylet 日志。
* 内存事故分四类：driver 堆爆、对象存储满、worker 被杀、引用泄漏 ——
  **处置手段完全不同**，先用 `ray memory` 的 `call_site` 定位。

下一章讲调度：资源模型、两层调度、放置组，以及「为什么我的任务没去我想去的节点」。
