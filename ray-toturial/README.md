仓库地址：https://github.com/hhk-png/cycle-agent

# Ray 完全教程

> 从原理到实战，从架构到复现——一份关于 Ray 的完整教程。
> 包含一个**可运行、可验证**的简化版 Ray 实现（纯 Python + NumPy，约 1.1 万行，192 个测试全部通过）。
> 事实标注来源，不确定的明确写"未确认"；现状截至 **Ray 2.58.0（2026-08-23）**。

**第八次修订（2026-09，本次）**：**新增第 39 章（Ray 源码阅读与事实核查指南）** ——
补上全书**唯一一处章节级结构缺口**，**全书的结语也随之移入第 39 章**。
这一轮还有两个新东西：**把一类反复出现的错误交给了脚本拦**，
以及**一次纯代码审计又抓出 10 个真实缺陷**（附录审计另外揪出 1 处「文档写了、代码没有」）。

摘要（完整清单见文末「第八批修订清单」）：

* **🆕 第 39 章：Ray 源码阅读与事实核查指南** —— 这一章**不含任何新的 Ray 知识**，
  它是一把**尺子**：教你**否决前面 38 章**。为什么需要它：本书反复宣示
  「回源码核对」是核心纪律（正文里用 `grep -c` / `curl` 这类**可否决命令**
  下结论的地方有 **24 处**），结语把「知道去哪儿核对」列为五句话里**唯一一条能力型**的 ——
  可是**读完前 38 章，读者仍然不知道怎么执行这个纪律**。
  内容是：三种拿到源码的办法（**含 C++ 侧**）、仓库地图与八个最常查的文件、
  **五个问题走读法**、**证据的三个等级与"怎么证明不存在"**、稳定性与弃用的判读、
  一份**七步核查清单**。
* **🔧 `check_docs.py` 新增两项检查，把一类错误交给脚本拦。**
  第七轮加第 38 章时，第 31/32/33/36/37 章共 **8 处**「全书结语在哪」「后面还有几章」
  的说法同时过期 —— 而它们**上一轮刚被核对过**。第八轮加第 39 章时，同一件事本来会
  **再发生一次**。于是新增：**自指计数**（「往后/后面/离全书结尾还有 N 章」必须等于真实章号差）
  与**结语位置**（「结语」只出现在一章，且所有指向它的说法都指向那一章）。
  **这是本书第一次把"修订纪律"变成"修订工具"。**
* **🔴 代码审计又抓到 10 个真实缺陷**（在上一轮 174 个测试全绿的情况下），
  附录审计另外揪出 1 处「**文档写了、代码没有**」（`miniray.util.state.*`）——
  其中 **4 个是「永久挂起且零诊断」**：`local_mode` 下任务**执行两次**、
  **GPU 放置组永远跑不了自己的任务**、`max_task_retries>0` 时 actor 死亡导致**在飞调用塞回死邮箱**、
  actor 的 `options(num_returns=N)` 与 `@ray.method` 不一致时**多余结果对象永远 PENDING**。
  另有 4 个「文档承诺 ✅ 但静默不生效」：actor 的 `scheduling_strategy` / `placement_group`
  被静默丢弃（**教程里"先占资源再往 bundle 放 actor"的经典模式直接死锁**）、
  `local_mode` 下 `runtime_env` 与 GPU 可见性被丢、async actor 声明并发组后**整个 actor 串行**。
  **第七轮的教训（"同一能力有几条实现路径"）这一轮又被验证了一次**：
  这些缺陷里有 5 个**正是第六、七轮修过的那类缺陷在另一条路径上的复制**。
* **文档侧**：四路正文审计（00–11 / 12–20 / 21–28 / 29–38）+ 附录 C–G 五路子审计 +
  **新章节单独一路对抗性复核**，修正 **60 余处**，其中
  **7 条是会直接崩溃或把结论说反的硬错** —— 例如
  `XGBoostTrainer` 的 legacy kwargs 在 2.58 是**抛异常**而不是"打告警"、
  放置组的节点故障语义被**说反**（连带第 8 章与附录 C 两处）、
  `list_tasks(filters=[("state","=","PENDING")])` 里的 `PENDING` **根本不是合法状态值**
  （过滤器**静默返回空**）、autoscaler v2 的默认值被**更正反了**。
* **本轮的方法论增量，写在结语里**：
  **核查这个动作本身也会出错 —— 而且它出错时最像成功。**
  作者为了核实「`python/ray/rllib` 是软链」，跑了
  `tar tzvf ray.tgz | grep -E "python/ray/rllib$"`，**空输出**，
  差点把一条**正确的**结论判成错的 —— 因为 verbose 输出行尾带了 ` -> ../../rllib`，
  那个 `$` 锚定永远匹配不上。
  前三条教训是"证据不够"；这一条是"**工具用错了，却返回了一个看起来很像结论的东西**"。
  **防身办法**：在核查命令里放一个**对照组**，确认这条命令**今天、在这个输入上**确实在工作。

---

**第七次修订（2026-09）**：**新增第 38 章（Ray 与 Agent 工作负载）**，
并第一次遇到一类**由本轮改动自身引发**的错误。

摘要（完整清单见文末「第七批修订清单」）：

* **🆕 第 38 章：Agent 工作负载** —— 这一章不是"又一个 AI 库"，而是**一条把前面
  串起来的负载主线**：Agent 的五个负载特征（长时程 / 有状态 / 大部分时间在等 /
  突发 / 可训练）、三层分工（编排层 / 运行时层 / 引擎层）、
  **会话亲和性在 Ray 2.58.0 的真实边界**、会话状态的四种放法、
  工具沙箱与超时取消、OTel GenAI 语义约定、轨迹数据、agentic RL、成本结构。
* **🔴 代码审计又抓到 9 个真实缺陷，而它们全是第六轮修过的那类缺陷
  「在另一条实现路径上的复制」** —— `local_mode` 下**每个任务执行两次**、
  生成器失败在 `local_mode` 下**永久挂起**、排队等到资源的 GPU actor
  **拿不到 `gpu_ids`**、`ray.cancel(生成器 chunk)` **静默不生效**、等等。
  第六轮的 17 个回归测试**全部只覆盖了分布式那一条路径**。
  回归用例 **17 → 26**，测试 **165 → 174**。
  **教训：修 bug 时要问"这个能力有几条实现路径"，每一条都要修、都要有测试。**
* **⚠️ 本轮自己犯了一个"查到一半就下结论"的错**：主线程认定
  `ConsistentHashRouter` "不在 Ray 2.58.0 里"，依据是"我列了
  `_private/request_router/` 目录，里面没有" —— 它在 `serve/experimental/` 下，
  **而本书第 15 章上一轮就已经把它写进表里了**（本书自己证伪了这句话）。
  错误已修正，并把教训写进了第 37 章 §37.15、第 1 章 §1.4 注③与第 38 章的结语。
* **🔴 一类"由本轮改动自身引发"的连锁错误**：加了第 38 章之后，
  全书**"全书结语在哪""后面还有几章"的说法集体过期**（第 31/32/33/36/37 章共 8 处）。
  这和第四轮那次"三章各自主张自己是全书结尾"**是同一个形状又发生了一次** ——
  说明**"改动本身也需要被检查"**这件事，不会因为知道它就不再发生。
  全书的**结语**因此从第 37 章移到了**第 38 章末尾**。
* **补齐的内容缺口**：`ray.experimental.tqdm_ray`（第 11 章）、`ray.widgets`（第 33 章）、
  Delta / Hudi / Kinesis 数据源（第 12 章）、**多租户与配额** + **非 x86 架构与 Windows**（第 17 章）、
  **API 稳定性与弃用策略**（第 20 章）、**第 37 章的「推理引擎全景与分离式服务」**（§37.15）、
  附录 C 新增 **13 条 Agent 相关术语**。
* **文档侧**：四路并行审计（00–11 / 12–20 / 21–28 / 29–37）**合计改动 200 余处**，
  其中多条是**会直接 `AttributeError` / `ImportError` / 永久挂起**的硬错
  （详见文末「第七批修订清单」）。新写的第 38 章还额外**单独派了一路独立复核**。

---

**第六次修订（2026-09）**：**新增第 37 章**，并把审计的**重心第一次转回代码本身**。
前五轮都在审文档；这一轮在四路文档审计之外**加了一路 mini-ray 代码审计**，
结果在 **148 个测试全绿**的情况下，找出了 **10 个真实缺陷** ——
其中四个是"**用户拿到错误结果、或者永远卡住，却没有任何诊断信息**"的那一类。

摘要（完整清单见文末「第六批修订清单」）：

* **🔴 GPU actor 完全创建不出来**（`raylet.py`）——
  `Resources.from_request(num_cpus, num_gpus, memory, custom)` 的**第三个位置参数是
  `memory`**，而调用点传的是 `1.0 if num_gpus else None`。
  于是任何 `num_gpus>0` 的 actor 都凭空多要 `1.0` 内存，
  而节点总内存是 `0.0` → `ScheduleError`，报错还把矛头指向内存。
  **一个核心特性死掉，而报错信息在误导你。**
* **🔴 生成器任务失败 → 消费端永久死循环**（`raylet.py` / `object_ref.py`）——
  `task_done` 会调 `_finish_generator`，`task_failed` **不会**。
  于是生成器的 `done` 永远是 `False`，而 `wait_for_generator` 超时**只返回、不抛错**
  —— `for ref in gen` 每 60 秒空转一次，**永不结束、不报任何错**。
  同一个路径还有第二个缺陷：生成器的 `result_ids` 是边产边追加的，
  "把异常写进每一个结果对象"会**覆盖消费者已经取走的数据**。
* **🔴 引用计数的整条链路从未生效，而且被静默吞掉**（`raylet.py` / `object_store.py`）——
  raylet 调用 `store.set_ref_count(...)`，而**这个方法根本不存在**。
  每一次上报都抛 `AttributeError`，每一次都被
  `except Exception: pass` 丢掉 —— 于是对象存储的 `ref_count` 永远停在插入时的值，
  「refcount 归零才回收」成了**死代码**。**这个错误瞒过了整整 148 个测试。**
* **🔴 async actor + `concurrency_groups` → 永久挂起**（`worker.py`）——
  async 执行上下文只轮询 `""` 组，而 raylet 按方法声明的组投递
  → 投进 `"io"` 邮箱的任务**永远没人取**。
* **🔴 调度竞争下任务被静默丢弃**（`raylet.py`）——`assign` 失败时调
  `remove_task`，再没有人会写它的结果对象 → `ray.get` 永久挂起。
* **🔴 取消/丢失的依赖被当成普通参数喂进下游任务**（`core_worker.py`）——
  判据写成了 `isinstance(value, RayTaskError)`，而 `TaskCancelledError` /
  `ObjectLostError` 继承的是 `MiniRayError`，于是**穿过**检查，
  用户看到一个莫名其妙的 `TypeError`，真正的原因被彻底掩盖。
* **🔴 资源被重复归还**（`scheduler.py`）——`_release` 不幂等：
  取消路径归还一次、临死 worker 的 `task_done` 在另一个线程里再归还一次
  → `available_resources()` 能**大于** `cluster_resources()`。
* **🔴 actor 的 GPU 从来没传给它自己**（`raylet.py` / `worker.py`）——
  资源在 raylet 侧扣对了，但 `gpu_ids` 没进 actor 进程：
  `ray.get_gpu_ids()` 返回 `[]`、**`CUDA_VISIBLE_DEVICES` 根本没设**，
  actor 里的 `torch.cuda` 会看到机器上**全部**的卡。
* **一个被审计"顺出来"的缺口**：`memory=` 是合法的 actor 选项、教程也写着它
  "参与调度"，但**节点总内存被硬编码成 0.0** → 任何带内存声明的 actor 都调度不上去。
  已补上节点内存建模（`ray.init(memory=...)`，默认按可用内存的 70% 探测）。
* **文档侧**：四路并行审计（00–11 / 12–20 / 21–28 / 29–36）合计改动 **120 余处**，
  其中 **10 条是会直接 `ImportError` / `AttributeError` / `TypeError` 的硬错**
  （详见文末清单）。
* **新增测试**：`tests/test_regressions.py`，**17 个测试**专门盯住上面每一条 ——
  它们覆盖的正是"以前一个测试都没有"的那几条路径。

> **这一轮最大的教训，写在结语里**：
> **测试全绿不等于没有 bug，只等于"没有测试覆盖到那个 bug"。**
> 上面 10 个缺陷全部在 148 个测试通过的情况下活着。

---

**第五次修订（2026-09）**：**没有新增章节，改的全是"已有的错"** ——
四路并行审计 + 逐条回源码核对，合计改动 **80 余处**，
覆盖 **00–36 全部章节**。这一轮最重要的发现是一类新形态：

> **前四轮自己"修"出来的错误。** 有 5 条是**第四轮把对的说成错的**、
> 或者只改了一半就收工。它们的共同点是：**上一轮引入了新结论，
> 却没有回头验证那个结论本身**。

摘要（完整清单见文末「第五批修订清单」）：

* **🔴 最高危：配置名写错 + 默认值差 100 倍**（第 3、7 章，共 5 处）——
  `free_objects_period_ms` **这个名字不存在**，真名是
  **`free_objects_period_milliseconds`**（Ray 的配置名就是环境变量名，
  写错会**静默无效**）；`free_objects_batch_size` 默认是 **100** 不是 10000。
  ⚠️ **这条是第四轮"统一口径"时引入的** —— 三处对上了，但**对的是错的值**。
* **🔴 第四轮把 timeline 的因果说反了**（第 4、11、21、31、33 章，共 6 处）——
  第四轮把"导出的是空文件"改成"**通常直接报错**"，并加了"不要以为是空文件"。
  对着 2.58 的 `state.py` 看：**它是 `logger.warning`，不抛异常，
  而且照样把文件写出来，内容是 `[]`**。所以**第四轮之前是对的、第四轮改错了**。
  这一轮改回来，并补上一直被漏掉的**第二个必需环境变量
  `RAY_task_events_report_interval_ms=0`**。
* **🔴 `ray debug` 被张冠李戴**（第 4、11、21、23、33 章，共 6 处）——
  前四轮把 **`RAY_DEBUG=1` + `ray debug`** 当成 Ray Distributed Debugger 的用法。
  实际 `ray debug` 是 **legacy 调试器**的入口；新版是 **VS Code 扩展 +
  `breakpoint()`**，**不需要 `ray debug`**。而且第 11 章同一个小节里
  上面说"Distributed Debugger = ray debug"、下面说"旧的调试器（ray debug）
  要 `RAY_DEBUG=legacy`" —— **自相矛盾**。
  **附带又纠正一条**：`RAY_DEBUG_POST_MORTEM=1` 被写成"只在 legacy 模式下有效"，
  实际**恰好相反** —— 它属于**新版**调试器，用之前要**先去掉** `RAY_DEBUG=legacy`
  与 `--ray-debugger-external`（两者**二选一**）。
* **🔴 `.bind()` 的"两种语义"是本书生造的**（第 2、21、22、26 章，共 6 处）——
  附录 A/B 与第 26 章说"① 参数部分套用（Ray 2.8+，返回 `RemoteFunction`）；
  ② 构 DAG（Ray 2.0+）"，与第 2/3 章的说法**3:3 对立**。回源码看：
  **`.bind()` 只有一种语义** —— 返回 DAG 节点 `FunctionNode`，
  `.remote()` 会抛 `AttributeError`；所谓"参数部分套用"是**同一个机制**的
  自然用法。**"Ray 2.8 那一档"不存在**，已删。
* **🆕 顺带发现一条 2026 年的新事实**：**`DAGNode.execute()` 已被弃用**
  （PR **#63716**，关闭 issue **#63666**）—— 未编译的 DAG 每次 `execute()`
  都会往 GCS 的 internal KV 导出新函数元数据，且**无清理无淘汰**，
  反复执行会让 **GCS KV 无界增长**。出路是编译图。已补进第 2、21、26 章。
* **🆕 两个"已移除"的 Trainer**（第 13、21、29 章）：
  **`LightningTrainer` / `TransformersTrainer` / `AccelerateTrainer`
  自 Ray 2.9 起已从 Ray 中删除**（2.7 弃用 → 2.8 报错 → 2.9 移除）。
  前四轮把它们当成"状态未确认"并列在推荐表里 —— 读者照抄会 `ImportError`。
* **🔴 第 13 章的 Checkpoint 一节整段建立在错误前提上**：
  `Checkpoint.from_uri()` **不存在**；`as_directory()` 的"两种返回形态
  （`str` 或上下文管理器）"**也不存在** —— 它被 `@contextlib.contextmanager`
  装饰、永远只返回上下文管理器。那段 `if isinstance(cp, str): ... else: with cp as d:`
  的"正确姿势"和"最容易踩的地方"都要重写。
* **🔴 第 11/36 章的"grep 可证命中数为 0"是自我否证的** ——
  写出这句话的那一行本身就是一处命中，读者**照做一条 grep 就能推翻全书立论**。
  已改成定性表述，并把它当成"§0.6 事实态度"的一个反面教材写进正文。
* **🔴 第 8 章的 autoscaler v2"默认开启"：同节自相矛盾、跨章不一致、
  且与源码相反** —— 源码里 `RAY_CONFIG(bool, enable_autoscaler_v2, false)`，
  **v2 是 opt-in**。已统一为"未确认 + 源码信号相反"。
* **🔴 第 10 章的 OOM"杀谁"只写了一半**，与第 7 章冲突 ——
  官方规则是**先分流**：**idle worker 选内存占用最大的**（与第 10 章原文
  "不是杀占用最多的"**恰好相反**），active worker 才走 time-based policy。
* **一批"照抄会炸"的 API 细节**：`ActorPoolStrategy(min_size=0)` 直接
  `ValueError`（下限是 1）；`TUNE_DISABLE_AUTO_CALLBACK_SYNCER` 这个环境变量
  **不存在**；`from_huggingface` 的流式/`DatasetDict` 支持**说反了**
  （`IterableDataset` **可以**、`DatasetDict` **不可以**）；
  `rllib-contrib-a3c` 装不上（真名 `rllib-a3c`，且 `rllib_contrib` 已从主仓库删除）；
  `config.build_env_runner()` **不存在**；`Algorithm.save()/restore()` **并未弃用**
  （它们是 Tune `Trainable` 的 `@DeveloperAPI` 方法）。
* **6 处"上一轮改了正文、漏了小结 / 邻节"**：第 13 章 §13.5 的
  `ray.train.get_world_rank()` 残留、第 13 章 §13.2 的 `FailureConfig` 字段清单、
  第 15 章 §15.19 的"四种拿法"、第 33 章 §33.6 与 §33.11 的 timeline 说法打架、
  第 35 章 §32.6→§32.5 的交叉引用、附录 B 的 `ray.util.list_actors()`。
* **补了 5 个内容缺口**：Ray Data 的**常规表算子表**（`unique`/`aggregate`/
  `with_column`/`add_column`/`iter_rows`/`to_torch`）与**关掉流式执行器的方法**；
  第 8 章的 **`label_selector` / `fallback_strategy`**；第 9 章的
  **`get_if_exists=True`**；第 13 章的**弹性训练入口**
  （`ScalingConfig(num_workers=(min, max))`）；第 16 章的
  **`RLlibCallback` 常用钩子清单**；第 20 章补上**推理编排层竞品**
  （Dynamo / llm-d / KServe）与 **V1 Train API / Compiled Graph 的时间线缺口**。

**明确的"没改"**：审计里有一条说 `CheckpointConfig.checkpoint_frequency`
在 2.58 已弃用 —— **回源码看是错的**（`ray/air/config.py` 里它仍是普通字段、
默认 0、无弃用告警），**故不改**。这类"看起来该改、其实不该改"的条目
本轮共驳回 2 条，都记在文末清单里。

---

**第四次修订（2026-09）**：新增第 **34–36** 章，合计改动 **100 余处**
（事实修正 / 跨章对齐 / 内容补充）。这是四轮里**改动面最广**的一次 ——
前 33 章几乎每一章都有条目。摘要：

* **新增 3 章** —— 它们补的都是**"零命中"级别的洞**：
  * **第 34 章 表格数据与传统 ML**：`scikit-learn` 在前 33 章里
    **命中数是 0**，`LightGBM` / `CatBoost` 也是 0。而表格数据 + GBDT
    恰恰是 Ray 落地量最大的场景之一。这一章讲 `XGBoostTrainer` /
    `LightGBMTrainer`、Ray Data 做特征工程、**什么时候不该上 Ray**（判据表）；
  * **第 35 章 数据版本、产物血缘与模型注册**：第 32 章把读者领到
    "registry 是你的事"就停了。这一章把那件事讲完 —— 数据指纹、
    `Checkpoint.set_metadata()` 血缘、Model Registry 的 alias 与回滚；
  * **第 36 章 分布式追踪与 OpenTelemetry**：第 11 章 §11.1 把"追踪"
    列为四层观测之一，**然后那一层再没出现过**（⚠️ 原文写的是"全书
    `OpenTelemetry` 命中 0、`Jaeger` 命中 0" —— **那是自我否证的**，
    第五轮已改成定性表述）。这一章补上：跨 actor 的 context 传播、
    把 Ray 的 ID 写进 span、`trace_id` 打进日志。
* **最高危的一类修正不是"新知识"，而是"上一轮改了正文、漏了小结"** ——
  这一轮修掉的 6 条属于这种：第 2 章 §2.8 改对了、**§2.11 小结还写着
  "默认会重试应用异常"**；第 5/10 章正文改对了、**两处小结还写着
  "断点续传"**；第 3 章 §3.2 改对了、**§3.4 与第 8 章 §8.7 还写着"10 秒"**；
  第 11 章 §11.2 改对了、**§11.4 还在用 `ray logs` 的扁平写法**。
  它们**不会让代码报错，只会让结论错**。
* **新增可复现的文档自检脚本** `mini-ray/tools/check_docs.py` ——
  以前"文档结构自检"是一句**声明**，现在是一条**命令**（10 项检查，带退出码）。
* **全书的"自述数字"统一到可核对的基线**：第四轮时是 37 篇正文（00–36）、
  148 个测试、12 个示例、约 1.1 万行；**第六轮更新为 38 篇（00–37）、165 个测试；第七轮更新为 39 篇（00–38）、174 个测试**。

---

**第三次修订（2026-09）**：全书做了一轮**三方交叉审计**，分三批改动。

* **第一批（补缺口）**：新增第 **26–28** 章 —— Compiled Graph、
  `runtime_env`/Ray Client、CI/CD 与 Slurm；
* **第二批**：新增第 **29–31** 章 ——
  **Ray × PyTorch/HuggingFace 生态集成**、**GPU 编程与集合通信**、
  **性能剖析工具链**；同时给 mini-ray 补上了
  **`ray.util.queue.Queue`** 与 **`ray.util.metrics`**（+30 个测试、+1 个示例）；
* **第三批**：新增第 **32–33** 章 ——
  **实验追踪与 MLOps 集成**（MLflow / W&B / TensorBoard，
  这是全书原来的**零命中缺口**）、**Ray CLI 全集与交互式开发**
  （CLI 四套体系速查 + Notebook 工作流 + 一条完整的定位流程）；
* **修正的事实错误**：三批合计 **90 余条**，其中影响最大的几条是
  **`DataConfig(train_ds=…)` 这个 API 根本不存在**（第 13 章教错了 V2 的数据入口）、
  **`concurrency=` 早在 2.51 就弃用**（第 12 章通篇在用）、
  **`.bind()` 的返回值不能 `.remote()`**（第 2 章给了会抛 `AttributeError` 的代码）、
  **`max_calls` 默认值是「CPU 不限 / GPU = 1」而非「不限」**、
  **`num_returns="dynamic"` 已弃用**（应为 `"streaming"`，且生成器任务**默认就是**它）、
  **生成器重试是「整任务重放」而非「断点续传」**、
  **`torch.timedelta` 不存在**（第 30 章的代码必然崩）、
  **内存监控杀 worker 不是「按内存占用杀」**（2.56 起是 time-based policy）、
  **`worker_idle_timeout_ms` 在 2.58 已不存在**（5 处仍在当真实 Ray 配置讲）、
  **`ray.train.get_device()` 少了一层命名空间**、
  **`local_mode` 已被移除但 4 处仍在教它用**、
  **`obj["size"]` 应为 `object_size`**。
  完整清单见文末。

所有修订都重新跑过测试与示例。

---

## 这是一份什么样的教程

Ray 是当下最主流的分布式计算框架之一，但资料呈现两极：入门文章停留在
`@ray.remote` 三件套，源码分析又直接扎进 C++ 的 raylet 与 GCS。
**中间那片"机制到底怎么运作、为什么这么设计、出问题怎么查"的空白，
就是这份教程要填的。**

三个特点：

**① 讲到能排查问题的深度。** 不是"Ray 用对象存储"，而是：对象存储多大、
在哪（`/dev/shm` 还是 `/tmp`）、什么阈值触发溢出、对象什么时候被回收、
pin 的语义是什么、owner 死了会怎样；一个任务的状态机（14 个状态枚举）分别意味着什么、
卡在哪个状态该查什么；调度是两层 + 租约、放置组为什么能避免启动期死锁。
第 11 章配了一份**按症状索引的排查手册**。

**② 配套一个真的能跑的简化实现。** [`mini-ray/`](mini-ray/) 用纯 Python 标准库
把 Ray Core 的核心机制全部实现了一遍：任务调度与依赖图、共享内存零拷贝对象存储、
Actor（含并发组与 async）、lineage 重建、放置组、State API、timeline 甘特图。
**192 个测试全部通过，12 个示例全部可运行。**

**③ 事实有来源，判断与事实分开。** 版本号、默认值、行为描述尽量标注一手来源
（release notes / 官方文档 / issue 编号）；查不到的一律写"未确认"。
第 20 章把「已发生的事实 / 官方路线图 / 我的判断」严格分节。

---

## 章节导航

| 章节 | 标题 | 一句话 |
|---|---|---|
| 00 | [前言与导读](ray教程-00-前言与导读.md) | 教程定位、六条阅读路径、7 天计划、结构地图（§0.4.1）、验证状态 |
| 01 | [认识 Ray](ray教程-01-认识Ray.md) | 是什么、解决什么问题、生态全景、2026 年现状 |
| 02 | [核心概念与 API](ray教程-02-核心概念与API.md) | 任务/对象/Actor/ObjectRef、依赖、资源、错误模型 |
| 03 | [架构设计](ray教程-03-架构设计.md) | GCS、raylet、CoreWorker、Plasma、调度、数据面 |
| 04 | [快速上手](ray教程-04-快速上手.md) | 安装、起集群、第一个程序、五个迁移套路、**Jobs API**、三件「脚手架」 |
| 05 | [任务、对象与依赖（深入）](ray教程-05-任务对象与依赖.md) | 生命周期状态机、流式生成器、序列化代价、引用计数 |
| 06 | [从零实现简化版 Ray](ray教程-06-从零实现简化版Ray.md) | **mini-ray 走读 + 踩过的 12 个坑 + 验证矩阵** |
| 07 | [对象存储与内存管理](ray教程-07-对象存储与内存管理.md) | 零拷贝/pin/溢出/四类内存事故与处置 |
| 08 | [调度与资源模型](ray教程-08-调度与资源模型.md) | 资源语义、两层调度、策略家族、放置组、排查表 |
| 09 | [Actor 模型与并发](ray教程-09-Actor模型与并发.md) | 邮箱、三种并发模型、async、五个模式、四个反模式、`get_if_exists` |
| 10 | [容错机制](ray教程-10-容错机制.md) | 重试、崩溃恢复、lineage 重建、检查点、故障注入 |
| 11 | [可观测性与调试](ray教程-11-可观测性与调试.md) | State API、日志、指标、timeline、**新/旧两套调试器**、按症状排查手册 |
| 12 | [Ray Data 与数据管道](ray教程-12-RayData与数据管道.md) | 流式执行、背压、DataSourceV2、Ray Data LLM、**常规表算子表** |
| 13 | [Ray Train 与分布式训练](ray教程-13-RayTrain与分布式训练.md) | Train V2、并行策略、checkpoint 恢复、**弹性训练**、vs torchrun |
| 14 | [Ray Tune 与超参搜索](ray教程-14-RayTune与超参搜索.md) | Tuner、采样器、ASHA、成本控制 |
| 15 | [Ray Serve 与 LLM 服务](ray教程-15-RayServe与LLM服务.md) | 部署/扩缩/路由、Serve LLM、PD 分离、KV 路由 |
| 16 | [RLlib 与强化学习](ray教程-16-RLlib与强化学习.md) | 新 API stack、RL 生态地图（verl/SkyRL/OpenRLHF） |
| 17 | [生产部署与安全](ray教程-17-生产部署与安全.md) | KubeRay、自动扩缩、可观测性接入、CVE 与加固清单 |
| 18 | [性能调优与反模式](ray教程-18-性能调优与反模式.md) | 10 条反模式、内存/调度/序列化调优、实测模板 |
| 19 | [生态对比与选型](ray教程-19-生态对比与选型.md) | vs Spark/Dask/torchrun/Monarch/K8s，决策树 |
| 20 | [现状与未来方向](ray教程-20-现状与未来方向.md) | 三条主线、路线图、竞争格局、批评与回应 |
| 21 | [附录 A：API 速查表](ray教程-21-附录A-API速查.md) | 全 API + **mini-ray 对照列** + 弃用清单 |
| 22 | [附录 B：mini-ray 工程手册](ray教程-22-附录B-mini-ray工程手册.md) | API 参考、数据约定、配置、验证矩阵、9 个练习 |
| 23 | [附录 C：术语表](ray教程-23-附录C-术语表.md) | 约 200 条术语，分主题 + 索引 |
| 24 | [附录 D：FAQ 与排错](ray教程-24-附录D-FAQ与排错.md) | 按症状组织的 Q/A，含"如何确认 + 怎么修" |
| 25 | [附录 E：端到端实战案例](ray教程-25-附录E-端到端实战案例.md) | 数据→推理→训练→服务→观测→容错 全流程 |
| 26 | [Ray Compiled Graph 与 DAG API](ray教程-26-RayCompiledGraph与DAG-API.md) | `bind()`/`InputNode`、`experimental_compile()`、通道数据面、多 GPU 通信 |
| 27 | [附录 F：Ray Client、runtime_env 与多语言](ray教程-27-附录F-RayClient与runtime_env与多语言.md) | 依赖怎么送到集群、`ray://` 还能不能用、Java/C++ 什么水平 |
| 28 | [附录 G：CI/CD、Slurm 与云上调度](ray教程-28-附录G-CICD与Slurm与云上调度.md) | 集群形状的测试、只有 Slurm 怎么跑、成本模型、编排集成 |
| 29 | [Ray 与 PyTorch / HuggingFace 生态集成](ray教程-29-Ray与PyTorch-HuggingFace生态集成.md) | 三层边界、`from_huggingface`、HF Trainer 接 Ray、LoRA/QLoRA 分布式微调 |
| 30 | [GPU 编程、集合通信与显存管理](ray教程-30-GPU编程-集合通信与显存管理.md) | `CUDA_VISIBLE_DEVICES` 语义、分数 GPU、NCCL 排查、四种 OOM |
| 31 | [性能剖析与调试工具链](ray教程-31-性能剖析与调试工具链.md) | `py-spy`/`memray`/`nsys`、火焰图怎么读、一条完整的定位路径 |
| 32 | [实验追踪与 MLOps 集成](ray教程-32-实验追踪与MLOps集成.md) | MLflow / W&B / TensorBoard、`RunConfig(callbacks=)`、checkpoint 之后的事 |
| 33 | [Ray CLI 全集与交互式开发](ray教程-33-RayCLI全集与交互式开发.md) | CLI 四套体系速查、Notebook 工作流、**按现象查命令的定位流程图** |
| 34 | [表格数据与传统 ML](ray教程-34-Ray与表格数据传统ML.md) | **XGBoost / LightGBM / scikit-learn**、四条路径、**什么时候不该上 Ray** |
| 35 | [数据版本、产物血缘与模型注册](ray教程-35-数据版本产物血缘与模型注册.md) | 数据指纹、`Checkpoint.set_metadata()`、Model Registry 的 alias 与回滚 |
| 36 | [分布式追踪与 OpenTelemetry](ray教程-36-分布式追踪与OpenTelemetry.md) | **跨 actor 的 context 传播**、把 Ray 的 ID 写进 span、`trace_id` 打进日志 |
| 37 | [LLM 推理引擎与性能优化](ray教程-37-LLM推理引擎与性能优化.md) | **引擎层 vs 编排层**、continuous batching、KV cache 与 PagedAttention、prefix caching、chunked prefill、投机解码、量化、CUDA Graph、TP/PP/DP、**引擎全景与分离式服务** |
| 38 | [Ray 与 Agent 工作负载](ray教程-38-Ray与Agent工作负载.md) | **Agent 负载画像**、三层分工、会话亲和性与会话状态、工具执行隔离、长时程四必须、轨迹既是日志也是训练集 |
| 39 | [Ray 源码阅读与事实核查指南](ray教程-39-Ray源码阅读与事实核查指南.md) | **全书的收尾章**：三种拿到源码的办法（含 C++ 侧）、仓库地图与八个最常查的文件、**五个问题走读法**、证据的三个等级与"怎么证明不存在"、稳定性与弃用的判读、**七步核查清单** —— 它教的是**怎么否决前面 38 章** |

**如果只读三章**：第 06 章（从零实现）、第 08 章（调度）、第 18 章（反模式）。

**按问题找章节**：

| 你的问题 | 去哪 |
|---|---|
| 「任务为什么不跑 / 卡在哪」 | 第 05 章（状态机）、第 08 章 §8.9（调度排查）、第 24 章（FAQ） |
| 「内存为什么下不去」 | 第 07 章 §7.7（四类事故）、第 24 章 D.4 |
| 「怎么把依赖/环境送到集群」 | 附录 F §F.2（runtime_env） |
| 「笔记本怎么连远端集群」 | 附录 F §F.3（Ray Client vs Jobs API） |
| 「怎么把现有脚本搬上 Ray」 | 第 04 章 §4.6（五个迁移套路）、附录 G §G.2（CI/CD） |
| 「细粒度任务开销太大」 | 第 26 章（Compiled Graph）、第 18 章 §18.1 |
| 「HF/Transformers 怎么接 Ray」 | 第 29 章 |
| 「GPU / NCCL / 显存 OOM」 | 第 30 章 |
| 「慢在哪一行 / 内存漏在哪」 | 第 31 章（剖析工具链） |
| 「只有 Slurm，没有 K8s」 | 附录 G §G.3 |
| 「实验指标/模型产物往哪落」 | 第 32 章（MLOps 集成） |
| 「**某个命令行该怎么敲 / 该敲哪一条**」 | **第 33 章 §33.9 的定位流程图** |
| 「**表格数据 / GBDT / sklearn 怎么上 Ray**」 | **第 34 章** |
| 「**这个线上模型是哪份数据训的 / 怎么回滚**」 | **第 35 章**（数据指纹 + Model Registry） |
| 「**一次请求跨 5 个 actor，慢在哪一段**」 | **第 36 章**（分布式追踪；先看 §36.1 的分工表） |
| 「**怎么在 remote 函数里下断点**」 | **第 11 章 §11.7**（新/旧两套调试器的对照表）+ 第 33 章 §33.6 |
| 「**timeline 导出是空的 / 到底报不报错**」 | **第 11 章 §11.6**（是 warning + 空文件，且要**两个**环境变量） |
| 「**某个配置项/环境变量的名字到底对不对**」 | 第 21 章 §A.11、第 23 章 §C.5 —— ⚠️ Ray 的**配置名就是环境变量名**，写错会**静默无效**（第五轮就抓到过一个写错 4 轮的） |
| 「**要不要为几个聚合操作回去上 Spark/pandas**」 | 第 12 章 §12.2 末尾的「常规表算子表」 |
| 「**节点随时来走，训练怎么不断**」 | 第 13 章 §13.6（`ScalingConfig(num_workers=(min, max))` + 两个前提） |
| 「**升级到新版本后跑不起来了**」 | 第 21 章 §A.10（已弃用/已移除清单，含 2.9 移除的三个 Trainer 与 `DAGNode.execute()`） |
| 「**推理服务的 QPS 上不去 / TTFT 忽高忽低**」 | **第 37 章**（先看 §37.14 的**按症状查表**，判断是编排层还是引擎层） |
| 「**长 prompt 一进来，所有人都在卡**」 | 第 37 章 §37.7（chunked prefill） |
| 「**多轮对话/Agent 每轮都要重新读一遍 prompt**」 | 第 37 章 §37.6（prefix caching + 把变动内容挪到 prompt 后面） |
| 「**显存不够，能塞多少并发**」 | 第 37 章 §37.4（每 token ~128 KB 的算式） |
| 「**`engine_kwargs` 里到底能写什么**」 | 第 37 章 §37.12（`LLMConfig` 字段全景表）+ 你那个版本的引擎 `--help` |
| 「**除了 vLLM/SGLang 还能用哪个引擎 / 有没有官方集成**」 | **第 37 章 §37.15**（七个引擎的对比表 + 自己复核的 grep 命令） |
| 「**Dynamo / LMCache / NIXL 和 Ray Serve 什么关系**」 | **第 37 章 §37.15**（PD 分离、KV 传输、KV 分层各自解决什么） |
| 「**Agent 跑几十秒、要调工具、要记住上一轮**」 | **第 38 章**（先看 §38.1 的负载画像与 §38.2 的三层分工） |

---

## 配套实现：mini-ray

```python
import sys; sys.path.insert(0, "ray-toturial/mini-ray")
import miniray as ray            # 与 Ray 同名 API,迁移只需改 import

@ray.remote
def square(x):
    return x * x

@ray.remote
class Counter:
    def __init__(self): self.n = 0
    def inc(self): self.n += 1; return self.n

ray.init(num_cpus=4)
try:
    print(ray.get([square.remote(i) for i in range(8)]))
    counter = Counter.remote()
    print(ray.get([counter.inc.remote() for _ in range(5)]))
finally:
    ray.shutdown()
```

**实现了什么**：任务与依赖图、对象存储（共享内存零拷贝 / 只读 / pin /
引用计数 / 溢出到磁盘）、Actor（并发度 / 并发组 / async / 重启 / 命名）、
流式生成器、放置组（四种策略 + 资源预留）、节点亲和、容错
（重试 / worker 崩溃恢复 / **lineage 重建**）、State API、timeline
（Chrome Trace + 自包含 HTML 甘特图）、`local_mode`、`util.ActorPool`、
**`util.queue.Queue`**（actor 支撑的分布式队列，含背压与多消费者）、
**`util.metrics`**（Counter / Gauge / Histogram）、
自定义序列化器、故障注入。

**明确不做**：Dashboard、autoscaler、Jobs API、跨机部署、
Ray Data/Train/Tune/Serve/RLlib、多语言绑定、鉴权、GPU 显存管理、NCCL/RDMA。

> ⚠️ 注意区分「**mini-ray 不实现**」和「**教程不讲**」：
> 上面这份是 **mini-ray 实现**的取舍清单。Jobs API、Dashboard、autoscaler、
> 上层 AI 库这些，**教程正文都讲**（分别见第 4、11、17、12–16 章），
> 只是 mini-ray 这个教学实现不含它们。这也是第 21 章 API 速查表里
> 那列 ❌ 的含义。

详细的能力清单与已知取舍见 [`mini-ray/README.md`](mini-ray/README.md)，
完整工程手册见[第 22 章](ray教程-22-附录B-mini-ray工程手册.md)。

### 快速开始

```bash
cd ray-toturial/mini-ray
python -m pytest tests/ -q                 # 192 个测试(约 9 分钟)
python examples/01_hello_ray.py            # 从最小示例开始
python examples/06_fault_tolerance.py      # 看 lineage 重建
python examples/11_end_to_end.py           # 端到端流水线
python -m miniray demo                     # 命令行演示
python -m miniray status --num-cpus 8      # 资源与 worker 视图
```

---

## ✅ 验证状态

本教程配套代码的验证结果（**可在你机器上复现**）：

```
$ python -m pytest tests/ -q
192 passed                                    # 12 个测试文件

$ for f in examples/*.py; do python "$f"; done
12 个示例全部 exit=0                          # 每个都有人工确认的输出
```

| 项 | 结果 |
|---|---|
| 测试 | **192 个全部通过**（序列化 7 / 对象存储 15 / 核心 30 / actor 19 / 调度 11 / 容错 6 / 可观测 9 / local_mode 6 / 工具 15 / 队列与指标 30 / **第六七轮回归 26** / **第八轮回归 18** = 192） |
| 示例 | **12 个全部可运行**（含端到端流水线、故障注入与队列流水线） |
| 端到端稳定性 | `examples/11_end_to_end.py` **连续 10 次运行 10 次通过**（此前有约 2/3 概率超时，根因与修复见下） |
| 跨进程关键验证 | 跨进程零拷贝写穿、`__main__` 函数跨解释器执行、lineage 重建 |
| 依赖 | Python ≥ 3.10 + NumPy；无其它第三方依赖 |
| 验证环境 | Windows 11 + Python 3.14.6 + NumPy 2.5.1（无需 GPU） |
| **本次修订后重跑** | 三条命令一次跑完的记录在 **`mini-ray/.verify_r8_final.txt`**：测试 **192 passed / exit 0**；示例 **12/12 exit 0**；结构自检 **exit 0**（10 项检查）。第七轮的记录留作对照在 `.verify_r7_final.txt`；第六轮 `.verify_r6_final.txt`；第七轮修复前的基线 `.verify_round6.txt`；第五轮 `.verify_round5.txt`，第四轮 `.verify.out` |
| **第八轮对 mini-ray 源码的改动** | **11 个**（10 个来自代码审计 + 1 处来自附录审计的「文档写了、代码没有」，见文末「第八批修订清单」）—— 其中 **4 个是「永久挂起且零诊断」**，另 **5 个是第六、七轮修过的那类缺陷在另一条实现路径上的复制**。每条都做了「**回退修复 → 测试失败 → 恢复 → 测试通过**」的验证（13 个回退点）。回归用例 **26 → 44**（新增 `test_regressions_r8.py` 18 个）。**174 → 192**，零回归 |
| **文档结构自检** | **40 篇正文章号连续（00–39）**、跨章引用无悬空、本地链接全部可达、代码块围栏全部闭合、**表格列数一致**、自述数字唯一，**第八轮起新增两项**：**自指计数**（「往后/后面/离全书结尾还有 N 章」必须等于真实章号差）与**结语位置**（「结语」只出现在一章，且所有指向它的说法都指向那一章）。第六轮起**把 `mini-ray/README.md` 也纳入检查范围**（它以前不在，却正是读者会照着跑的那一份） |
| **自检可复现** | 上面那一行不再是声明，而是一条命令：**`python mini-ray/tools/check_docs.py`**（10 项检查 + 退出码）。脚本本身也处理了 Markdown 的转义竖线 `\|`，不会把正常表格误报成"列数不一致" |
| **第六轮对 mini-ray 源码的改动** | **10 个真实缺陷的行为修复**（见文末「第六批修订清单」），外加节点内存建模；新增 `tests/test_regressions.py`（**17 个测试**）专门覆盖这些以前**一个测试都没有**的路径。**148 → 165**，零回归 |
| **第七轮对 mini-ray 源码的改动** | **9 个真实缺陷**（见文末「第七批修订清单」）——全是第六轮修过的那类缺陷**在另一条实现路径上的复制**（local_mode vs 分布式、排队等资源 vs 创建即有资源、生成器 vs 普通任务）。回归用例 **17 → 26**。**165 → 174**，零回归 |

> **第四轮的一个方法论改进**：以前"文档侧自检"只写在这张表里，
> 读者只能选择相信。现在它是一条**能跑、会失败、带退出码**的命令 ——
> **把"声明"变成"可验证的断言"**，这也是全书 §0.6 那条态度的直接应用。

### 一次真实的 bug 修复（发生在第四轮修订中）

**症状**：`examples/11_end_to_end.py` 的「容错演练」一步
（丢掉全部对象后靠 lineage 重建恢复）**约 2/3 的概率超时失败**，
报错是 `'ObjectLostError' object is not subscriptable`。

**根因**（两个叠加）：

1. `lose_objects()` 把对象从存储里删掉后，**没有同步 `_object_states`** ——
   账本里那些对象仍然写着 `READY`；
2. 于是「重建依赖」的判定以为依赖还在，**直接重放任务**，
   而任务实际拿到的参数是一个 `ObjectLostError` 占位对象。

**修复**：① 失效化 —— 丢对象时立刻把状态改成 `LOST`；
② 新增 `_is_available()` —— 把「**状态是 READY**」和
「**真的能当输入用**」区分开（被判定丢失的对象状态也是 READY，
但不能喂给任务）。

**验证**：修复前 6 次跑 4 次失败；修复后**连续 10 次全部通过**，
且 148 个测试无回归。

> 这条留在 README 里，是因为它本身就是教程第 10 章讲的东西的**真实样本**：
> 分布式系统的 bug 往往不在"主流程"，而在**两份状态没对齐**
> （存储 vs 账本）的那个瞬间。

**诚实说明**：mini-ray 在本机 **完整验证**；教程里对**真实 Ray** 的行为描述
基于官方文档与 release notes（本机未安装 Ray 2.58，Python 3.14 的兼容性未验证），
因此 Ray 侧示例属"文档级准确"而非"本机跑过"，正文中已逐处标注来源。

---

## 关于事实与时效性

* 写作时间：**2026 年 9 月**；Ray 最新稳定版 **2.58.0**（2026-08-23）；Python ≥ 3.10。
* 标注规则：无标注 = 官方文档/源码级事实；`（某版本 release notes / issue #N）` = 有一手来源；
  **未确认** = 检索不到可靠来源或来源冲突。
* 第 20 章专门把「事实 / 官方路线图 / 我的判断」分节，
  **不要把判断当事实引用**。
* 已知的生态变化（会影响你的技术选型）：
  **vLLM 正在用 `RayExecutorV2` 替换基于 Ray compiled graph 的 executor**
  （RFC #35848，由 Ray 团队提交）——Ray 在推理栈里正收缩为"放置与调度层"，
  而在训练/后训练/数据侧反而更深。详见第 20 章。

---

## 目录结构

```
ray-toturial/
├── README.md                 # 本文件
├── ray教程-00..39-*.md        # 40 篇正文(含 7 个附录 A–G)
└── mini-ray/                  # 配套实现
    ├── miniray/               # 25 个顶层模块(含 util/ 与 _private/ 共 35 个文件)、约 1.1 万行
    ├── tests/                 # 192 个测试(12 个测试文件 + 3 个辅助脚本)
    ├── examples/              # 12 个可运行示例
    ├── tools/
    │   └── check_docs.py      # ⭐ 文档结构自检(10 项检查,带退出码,可复现)
    ├── README.md              # 工程说明与取舍清单
    ├── .verify_r6_final.txt   # 第六轮（最近一次）三条命令一次跑完的完整记录
    ├── .verify_r8_final.txt   # 第八轮（最近一次）三条命令一次跑完的完整记录
    ├── .verify_r7_final.txt   # 第七轮三条命令一次跑完的完整记录（当时 174 个用例全绿）
    ├── .verify_r7_a.txt       # 第七轮 pytest + 示例的原始输出
    ├── .verify_r6_full.txt    # 第六轮 pytest 单独记录（当时的 165 个用例，用于对照）
    ├── .verify_r6_examples.txt# 第六轮示例记录：12/12 exit 0
    ├── .verify_round6.txt     # 第六轮**修复前**的基线（148 个用例，用于对照）
    ├── .verify_round5.txt     # 第五轮的验证记录
    ├── .verify.out            # 第四轮的验证记录（保留以便对照）
    └── pyproject.toml
```

**一次完整的验证 = 三条命令**：

```bash
cd ray-toturial/mini-ray
python -m pytest tests/ -q              # ① 192 passed
for f in examples/*.py; do python "$f"; done   # ② 12/12 exit=0
cd .. && python mini-ray/tools/check_docs.py   # ③ 10 项结构检查全通过
```

---

## 第八批修订清单（2026-09 第八轮审计）

### 第一类：新增第 39 章，并把结语搬过去

**为什么加这一章**：它是全书**唯一一处章节级结构缺口**，而且是用 grep 证出来的 ——

```bash
# 全书 1563 个标题里，只有 1 个在教读者"如何自己确认"
$ grep -rn "^#\{2,4\}.*\(怎么自己确认\|如何确认\|核查\|自查\|如何验证\)" ray教程-*.md
ray教程-36-...md:104:### 怎么自己确认第一层的现状（给要深入的人）   # 只针对 OTel 一个话题

# 唯一的"源码走读"标题，走读的是 mini-ray 自己的 queue.py，不是 Ray
$ grep -rn "^#\{1,4\}\s.*\(走读\|读源码\|源码阅读\|源码导览\)" ray教程-*.md
ray教程-22-...md:432:## B.9 走读：`util/queue.py` 的四个设计取舍
```

即：**这本书把「回源码核对」当作核心纪律反复宣示，却从未把这个纪律教出来。**
第 39 章补的就是它，**不含任何新的 Ray 知识**。

**连带改动（这是第四轮/第七轮踩过的同一个形状，第三次）**：
结语从第 38 章搬进第 39 章，于是所有"全书收尾点"的说法必须一起改 ——

| 位置 | 原来说 | 现在 |
|---|---|---|
| 第 20 章 §20.10 | 往后还有 **18** 章（五组） | **19** 章（六组，新增"方法层"） |
| 第 31 章 §31.11 | 离全书结尾还有 **7** 章；真正收尾在**第 38 章** | **8** 章；第 **39** 章 |
| 第 32 章尾 | "第 38 章 = 全书的收尾章" | 第 **39** 章 |
| 第 33 章尾 | 后面还有 **5** 章、33–38 **六**章 | **6** 章、33–39 **七**章 |
| 第 36 章 §36.11 / 第 37 章 §37.18 | 结语链接指向第 38 章 | 指向第 **39** 章 |
| 第 38 章尾 | `## 结语：这本书真的讲完了` | 改为指向第 39 章；**结语整体搬过去** |
| 第 1 章 §1.6 结构块 | 只到"第 11 部分 / 37" | 补 **第 12（38）、第 13（39）部分** |
| 第 0 章 §0.4 / §0.4.1 / §0.7 | 39 篇（00–38） | **40 篇（00–39）** |

### 第二类：把一类反复出现的错误**交给脚本拦**

这是本轮最重要的**工具性**改动。**同一件事，两轮内发生了两次**：

* 第七轮加第 38 章 → 第 31/32/33/36/37 章共 **8 处**"全书结语在哪/后面还有几章"**同时过期**（而它们上一轮刚核对过）；
* 第八轮加第 39 章 → **同一件事本来会再发生一次。**

所以 `mini-ray/tools/check_docs.py` 新增**两项检查**（脚本里的第 8、9 项）：

| 检查 | 判据 |
|---|---|
| **自指计数一致** | 「(往后\|后面\|离全书结尾)还有 **N** 章」必须等于 `max_chapter − current_chapter` |
| **结语位置一致** | 「结语」标题**只许出现在一章**，且所有指向它的说法（`第 N 章的「结语」`/`结语移到了第 N 章`/`收尾在**第 N 章**`）都必须指向那一章 |

两个设计取舍值得记：
1. **只抓带阿拉伯数字的写法**（「后面还有几章」「33–39 七章」这类中文数字故意不抓）——
   **宁可漏，不可误报**，因为一个总在误报的检查很快会被所有人忽略。
2. **只扫正文、不扫 README** —— README 的修订清单是**历史记录**
   （"第七轮把结语从 37 搬到 38"记录的是当时的状态），把它纳入检查会制造必然的误报。

### 第三类：mini-ray 代码缺陷（10 个 —— 4 个是"永久挂起且零诊断"）+ 1 处「文档写了、代码没有」

**在上一轮 174 个测试全绿的情况下找出来的。** 审计方法照旧先问
「**一个能力有几条实现路径**」，再逐条路径跑最小复现（脚本在
`%TEMP%\rayaudit\final\`，全部有实跑输出）。

| # | 缺陷 | 级别 |
|---|---|---|
| 1 | `local_mode=True` 下任务**执行两次**（第二次在凭空拉起的子进程里）—— **超过 ~0.05 秒就中招**，且状态 API 的 `num_attempts` 仍显示 1，**看不出来** | 🔴 |
| 2 | **GPU 放置组永远跑不了自己的任务**：reserve 把卡从公共池扣走、assign 又去公共池取 → `ray.get` 永挂；且第一次失败的 assign 会把 bundle 的 GPU 永久扣掉 | 🔴 |
| 3 | `max_task_retries > 0` 时 actor 死亡 → 在飞调用被**塞回死 actor 的邮箱**，`ray.get` 永久挂起（`max_task_retries=0` 反而会正常抛 `ActorDiedError`） | 🔴 |
| 4 | actor 的 `options(num_returns=N)` 与 `@ray.method(num_returns=…)` 不一致时，多出来的结果对象**永远 PENDING**（task 路径会显式报错，actor 路径把不一致吸收成了挂起） | 🔴 |
| 5 | 多节点下失败任务的**错误对象存在 driver 节点**，却对外宣称在生产它的节点上 → 下游拿到"对象已丢失"而不是真实原因（**把用户指向与事实相反的方向**） | 🟠 |
| 6 | actor 的 `scheduling_strategy` / `placement_group` 被**静默丢弃** → 教程里"先占资源、再往 bundle 里放 actor"的经典模式**直接死锁**（actor 永远 PENDING，预留资源摆着用不上） | 🟠 |
| 7 | `local_mode=True` 下 `runtime_env`（env_vars）与 GPU 可见性被**静默丢弃**（task 与 actor 两条路径都丢） | 🟠 |
| 8 | async actor 只要声明了 `concurrency_groups`，`max_concurrency` 就**塌成 1** → **整个 actor 串行**，组的并发上限全部失效 | 🟠 |
| 9 | `ray.cancel()` 作用在 actor 方法 ref 上是**静默空操作**（方法照跑，不报错不告警） | 🟡 |
| 10 | 引用计数上报"失败时的诊断路径"本身是坏的：`CoreWorker` 上没有 `_stop` 属性 → 真实异常被 `AttributeError` 顶掉 | 🟡 |
| 11 | **文档写了、代码没有**：`miniray.util.state.*` 这条"Ray 兼容路径"在附录 A/B 里都写着可用，实际 `AttributeError: module 'miniray.util' has no attribute 'state'` —— `util/__init__.py` **从没 import 过 `state`**（它自己的 docstring 里却写着 `from miniray.util import state`）。`tests/test_util.py` 的 import 测试**绿着**，因为它走的是 `from miniray.util.state import …`，**绕开了 `__init__.py`** | 🟡 |

> **第七轮的教训这一轮又被验证了一次**：上面有 **5 个**（#1 #4 #5 #7 #10）
> **正是第六、七轮修过的那类缺陷在另一条实现路径上的复制**。
> 「同一能力有几条实现路径」这条问法，**不是一次性作业**。

**两条最有代表性的根因**：
* **#1 是"上一轮的修复只覆盖了一半路径"**：第七轮为它加了一道闸门
  （按 `state is READY` 过滤），但那个闸门**挡不住"正在 inline 执行中"** ——
  于是修复只在*任务短于调度 tick* 时有效。他们的测试用的是瞬时任务 + 事后 `sleep(1.0)`，
  **结构上测不出来**。
* **#6 是"文档承诺 ✅ 但代码从没读过这个字段"**：附录 A.4 把 actor 的
  `scheduling_strategy` 标成「✅ 已实现」，第 8 章正文教的也正是那个写法 ——
  而 `create_actor` **从头到尾没读过 `options["scheduling_strategy"]`**。
  现有测试 `test_actor_inside_placement_group` 只断言"两个 actor 在同一节点"，
  而**默认调度恰好也满足**，所以测不出。

### 第四类：会直接崩 / 结论说反的文档硬错

| 位置 | 原文 | 事实 |
|---|---|---|
| 第 34 章 §34.3/§34.4（3 处） | `XGBoostTrainer(label_column=..., params=...)` 「2.58 里**仍然能跑，但会打弃用告警**」 | 🔴 **直接抛 `DeprecationWarning`**。Train V2 自 2.51 默认开启，解析到的是 `train/v2/…/xgboost_trainer.py`，它的 `__init__` 里是 **`raise`**（LightGBM 逐字相同）。**正文把出处指到了 V1 那个只 `_log_deprecation_warning` 的同名文件** |
| 第 8 章 §8.5/§8.10 + 附录 C「放置组重建」条 | 「组里的 actor/任务**不会被重新拉起**」「资源不够会**退化成 `REMOVED`**」 | 🔴 **两条都说反了**。官方原文：*"Ray **reschedules** Actors and tasks that use the bundle … **based on their fault tolerant policy**"*；资源不够时是 *"remains in the **partially created state indefinitely**"*。源码里唯一的 `UpdateState(...REMOVED)` 属于**显式删除**路径。**"训练跑了一夜全挂了"的真实根因不是"Ray 不恢复"，而是"没开 `max_restarts`"** |
| 第 2 章 §2.2/§2.8 + 第 10 章 §10.2（5 处） | `retry_exceptions=lambda e: …` **判定函数**写法，且第 10 章用一整段 callout 推荐它 | 🔴 2.58 的类型约束是 `(bool, list, tuple)`，**传 callable 在 `@ray.remote` 装饰期就抛 `TypeError`**。官方文档全文没有 callable 写法。想要 5xx 粒度只能**在函数体里判断、转成白名单异常类型** |
| 第 12 章 §12.2 的 `iter_torch_batches` 示例 | 同时传 `dtypes=` 与 `collate_fn=` | 🔴 两者**互斥**，`iterator.py` 里 `if collate_fn is not None and dtypes is not None: raise ValueError(...)` |
| 附录 D（3 处） | `list_tasks(filters=[("state","=","PENDING")])` | 🔴 **没有 `PENDING` 这个状态值**（只有 `PENDING_ARGS_AVAIL` 等 6 种前缀值），`=` 是全串等值比较 → 过滤器**静默返回空列表**，读者会得出"没有 pending 任务"的**错误结论** |
| 附录 D | autoscaler v2「默认 `false`、opt-in —— 早先写成『默认开启』是错的，已更正」 | 🔴 **更正反了**。`ray up` 自 **2.50.0** 起默认注入 `RAY_enable_autoscaler_v2=1`（`commands.py`：注释就写着 *"The default value is 1 since Ray 2.50.0."*） |
| 第 33 章 / 第 11 章 §11.7 | `RAY_DEBUG_POST_MORTEM` 「属于**新版**调试器，用前要先去掉 legacy 旗标」 | 🟠 **两套调试器都支持** —— `rpdb.py` 的 `_post_mortem()` 按 `RAY_DEBUG` 分支，而调用侧只判 `RAY_DEBUG_POST_MORTEM`。**这条本书已错过两次**（第四轮"只在 legacy 有效"→第五轮"只属于新版"→第八轮"两者都支持"），**每次都是只查了一个分支就下全局结论** |

### 第五类：跨章矛盾与"半更正"（本轮最集中的形态）

**"改了正文、漏了小结/索引/决策表"这一次出现在 4 个附录里**：

* **附录 F**：F.3.4 已用源码更正过的结论（Serve 数据面在 Ray Client 下可用、
  `RAY_ENABLE_UV_RUN_RUNTIME_ENV` 的名字、conda 在 Windows 上是 beta），
  在 **F.6 小结 / F.3.6 决策表**里**仍是旧版** —— 同一篇文档里新旧结论并存。
* **附录 B**：`check_docs.py` 的检查项数在同一条注释里「8 项」与「10 项」并存，且章号区间还写着 `00–38`（实为 **00–39**）。已统一为 **10 项** —— 口径取「脚本实际输出的结果行数」，这也是本书历来的口径（加这两项之前是 8 项）。
* **附录 C**：「Compiled Graph 处于 `experimental`」与**第 26 章的 beta** 冲突；
  「Task Events 2.58 已移出 GCS 热路径」与**第 20 章 §20.5 的"默认关闭的 opt-in"**冲突；
  「放置组重建」与**第 8 章**冲突（见上表）。
* **附录 G**：`local_mode` 「已被移除」—— 形参**仍在签名里**，只是传 `True` 抛 `RuntimeError`。

**"把源码/文档里查得到的东西标成未确认"**（本书的立场是"查不到才写未确认"）：

* `RAY_DEBUG_POST_MORTEM` 的读取点（其实在 `python/ray/util/rpdb.py`，只是**不在** `worker.py`/`scripts.py`/`ray_config_def.h` 那三个文件里）；
* `RAY_TLS_SERVER_CERT` / `RAY_TLS_SERVER_KEY` / `RAY_TLS_CA_CERT`（`ray/_common/tls_utils.py` 里逐字写着，且**要求三个同时设置**）；
* `RAY_gcs_storage` 的取值（`ray_config_def.h`：`'memory'`(默认) / `'redis'` / `'rocksdb'`；**RocksDB 是 alpha、Redis 仍是 officially supported**，不是"改用"）；
* `config.build()`（就是 `@Deprecated(new="AlgorithmConfig.build_algo", error=False)` 的转发别名）；
* `object_spilling_directory` 的优先级（`ray_config_def.h` 的注释原文写了）；
* `chat_template_stage` / `ChatTemplateStageConfig`（类定义就在 `ray/data/llm.py`）；
* `ray logs actor --pid`（**存在** —— `actor` 子命令同时接受 `--id` 与 `--pid`，与 `worker`/`task`/`job` 都不一样）。

**"结论对、证据错"**：第 17 章论证「Ray 没有租户配额」时引的三个"零命中"路径里，
`python/ray/_private/ray_option_utils.py` **在 2.58 已不存在**（在 `_common/`）。
结论经得起检验，**但下一个想复现这条证据的人会搜空，然后反过来怀疑结论**。
已改成穷尽式证据（全 `python/ray/` 检索 + 逐条解释 28 处命中）。

### 第六类：过期数字

* `raylet.py` 的行数 **2122 → 2227**（第 6 章，第六轮加过代码后没同步）；
* 第 12 章三处源码行号漂移（`571 → 568`、`583 → 573`）；
* `free()` 在 2.58 的 docstring 里**已标弃用**（⚠️ 但**没有** `@Deprecated` 装饰器，所以**不会真的发 warning** —— 这个区别正是本书一直在强调的"文档声明 vs 运行时行为"）；
* 附录 A §A.1 的 mini-ray `address` 还接受**空串** `""`；
* 附录 G 的 5 个 GH Actions major 集体落后（checkout v4→v7 等）。

### 第七类：关掉两个**标了好几轮的「未确认」**（本轮最值钱的两条）

「未确认」是这本书的诚实机制，但它也有成本 —— 一个其实查得到的事实被标成未确认，
读者就永远拿不到答案。本轮关掉了两个：

**① topology-aware 调度到底是哪个版本？**（标了 3 轮，而且**书内自相矛盾**：第 1 章写 2.57、第 3 章写 2.56）
逐 tag 检索后的答案是**两件事落在两个版本上**：

| 层 | 从哪个版本起 | 判据（可复核） |
|---|---|---|
| 公开 Python API `topology_strategy` | **2.57** | `python/ray/util/placement_group.py` grep `topology_strategy`：2.55=**0**、2.56=**0**、2.57=**34**、2.58=**34**（对照组 `def placement_group` 每版 = 2） |
| C++ 侧独立策略 `topology_bundle_scheduling_policy` | **2.58** | 该 `.cc` 在 2.57 = **404**、2.58 = **200**（对照组 `hybrid_scheduling_policy.cc` 两版都 200）；`scheduling_options.h` 里 `topology` 命中数 2.57=**0**、2.58=**22** |

**所以"2.56 起"是错的**（2.56 两层都没有）。而 2.57 那一版是**过渡实现**：
拓扑策略还只在 Python 侧近似（源码注释自称 *"Current implementation derives node
level strategy from topology_strategy…"*，`_derive_node_level_strategy()` 只从
`ray.io/node-id` 推出节点级策略）。**要用完整拓扑语义需要 2.58+。**

**② autoscaler v2 是不是默认开启？**（标了 **4 轮**，第 1/8/17 三章各说各话）
答案是**「分路径」** —— 这也解释了它为什么一直对不上：**那些信号说的不是同一层**。

| 路径 | 默认 | 判据 |
|---|---|---|
| **`ray up`** | **2.50.0 起默认开启** | `autoscaler/_private/commands.py` 的 `os.getenv("RAY_UP_enable_autoscaler_v2", "<默认>")`：**2.49.0 = `"0"`、2.50.0 起 = `"1"`**（逐 tag 可复核）；注释原文 *"The default value is 1 since Ray 2.50.0."* |
| **裸 `ray start`** | 关闭 | `ray_config_def.h` 的 `RAY_CONFIG(bool, enable_autoscaler_v2, false)` |
| **KubeRay** | opt-in（要显式写 `autoscalerOptions.version: v2`） | operator 侧要求该字段显式为 `v2` |

**所以本书早先那句"2.54.0 起默认开启"是错的** —— 翻转在 **2.50.0**，且**只对 `ray up` 成立**。

> 💡 **这两条的教训是同一个**：一个"未确认"拖了很多轮，往往不是因为它查不到，
> 而是因为**把两个不同层的问题当成了一个**（API 层 vs 实现层、
> `ray up` 层 vs 组件默认层）。**拆开问，就查到了。**

**③ 还有一个"未确认"是被证伪的**：第 30 章曾断言
「`ScalingConfig` 没有指定 bundle 的入口（第 13 章 §13.1 标注**未确认**）」，
而第 13 章 §13.1 写的是**已核实**（误引），且 `ScalingConfig` 实际**有 `label_selector`**
（节点标签级的 worker 放置）—— 大多数"想把 worker 放到某些机器上"的需求用它就够了。
已改（第 30 章 §30.7）。

### 本轮的方法论增量

1. **核查这个动作本身也会出错 —— 而且它出错时最像成功。**
   作者为了核实「`python/ray/rllib` 是软链」，跑
   `tar tzvf ray.tgz | grep -E "python/ray/rllib$"` → **空输出**，
   差点把一条**正确的**结论判成错的（verbose 输出行尾带了 ` -> ../../rllib`，
   那个 `$` 锚定永远匹配不上）。
   **前三条教训是"证据不够"；这一条是"工具用错了，却返回了一个看起来很像结论的东西"** ——
   而"空输出"恰恰是"不存在"的标准长相。
   **防身办法：在核查命令里放一个对照组**，确认这条命令**今天、在这个输入上**确实在工作。
2. **"文件存在"不等于"内容在那儿"。**
   本轮据此推出"`_private/` 下的文件挪到 `_common/` 了"，并顺手把 `ray_constants.py`
   也算了进去 —— **错了一半**：`ray_constants.py`（649 行）**根本没挪**，
   `_common/` 下那个同名文件**只有 5 行**。
   这一类错误的形态是：**我证实了一个特例，然后把它当成了规则** ——
   **它每一步都有证据，所以才贵。**
3. **一个检查如果总在误报，很快会被所有人忽略。**
   新增的两项检查**故意只抓带阿拉伯数字的写法、且只扫正文不扫 README** ——
   宁可漏，不可误报。**工具的可用性和它的严格性是一对取舍。**
4. **"同一能力有几条实现路径"不是一次性作业。**
   第六、七轮修过的缺陷，第八轮又在**另一条路径**上抓到 5 个复制品。
   这条问法必须**固化进每次审计的清单**，而不是当成一次性的顿悟。
5. **对照组必须「隔离变量」—— 一个抢同一个资源的对照，不是在对照。**
   本轮作者想验证 mini-ray 的「旧写法 actor 放置组」能不能用，于是先建一个新写法 actor、
   再建一个旧写法 actor 看它起不起得来 —— **旧写法挂了，看起来是个真 bug**。
   真相是：**那个放置组只有一个 bundle，已经被第一个 actor 占满了**，旧写法在等一个
   永远不会空的 bundle。改成「每种写法各用一个**全新**的放置组」后，**三种写法全部正常**。
   **这是"对照组"这条纪律的一个新变体**：第 39 章 §39.5 案例三讲的是对照**缺席**
   （空输出被当成"不存在"），这一条讲的是对照**在场但被污染**
   （把"资源被占"读成了"功能坏了"）。**两者的共同点是：你以为你在测 A，其实测到的是 B。**

---

## 第七批修订清单（2026-09 第七轮审计）

**本轮新增第 38 章（Ray 与 Agent 工作负载）**，并沿用第六轮确立的做法：
**四路文档审计 + 一路 mini-ray 代码审计**，另外给新写的章节**单独派了一路独立复核**。

### 第一类：mini-ray 代码缺陷（9 个 —— 全是"上一轮只修了一半"）

这一轮最重要的发现不是"又抓到了 bug"，而是它们的**形状**：

> **9 个缺陷全部是第六轮修过的那类缺陷，在另一条实现路径上的复制。**
> mini-ray 里同一种能力往往有两条路："分布式"与"`local_mode`"、
> "创建时就有资源"与"排队等到资源"、"普通任务"与"流式生成器"。
> 第六轮修好了其中一条，**另一条一模一样地坏着** ——
> 而第六轮加的 17 个回归测试，**全部只覆盖了分布式那一条路径**。

| # | 缺陷 | 形状 | 用户看到什么 |
|---|---|---|---|
| 1 | **`local_mode` 下每个任务执行两次** | `_execute_inline` 跑完没在调度器收尾，任务仍在 `READY` 队列里，下个 tick 又被派给子进程 | 副作用翻倍（写文件/发请求），而 `ray.get` 的返回值**完全正常** |
| 2 | **`local_mode` 下失败任务被重试进子进程** | local_mode 从不走 `assign`，`num_attempts` 恒为 0 → 判定恒为"可重试" | 函数被调 **5 次**，调用方只看到一次报错 |
| 3 | **`local_mode` 生成器失败 → 永久挂起** | except 分支不区分生成器：异常写进**每一个** result_id（覆盖已交付的 chunk），且不标 `done` | 每 60 秒空转，永不结束 |
| 4 | **actor 的 `__init__` 收到 ObjectRef → actor 创建不出来** | `actor_spec` 里没有 `creation_deps`，worker 永远读到空列表 | 报错指向"上游的 bug（或者对象已丢失）"，**误导方向** |
| 5 | **排队等到资源的 GPU actor 拿不到 `gpu_ids`** | `runtime.gpu_ids` 是创建那一刻的快照，`assign` 之后没同步回去 | `ray.get_gpu_ids()` 返回 `[]`、`CUDA_VISIBLE_DEVICES` 不设，**每个 actor 都以为自己有全部卡** |
| 6 | **调度失败被判成"可重试" → READY 队列每 0.2 秒翻倍** | 调度失败没走到 assign → `num_attempts` 恒 0 → 恒"可重试"；且旧条目没被摘除，重试又 append 一次 | 2 秒内队列涨到 6 万条；`ray.get` 永久等，**完全不报错**。函数名写着 `permanently`，行为却是死循环 |
| 7 | **`ray.get_actor(name)` 把 `num_returns` 写死成 1** | 忽略了 GCS 里 worker 注册上来的真实元数据 | **同一个方法**：用创建时的 handle 调返回 `[5, 10]`，用 `get_actor` 的 handle 调返回 `7` —— **静默丢返回值** |
| 8 | **`ray.cancel(生成器 chunk)` 静默无效** | 生成器 chunk 从没登记 `_object_tasks`，取消查不到记录就直接 `return` | 取消"成功"，生成器照样把 30 个 chunk 产完 |
| 9 | **生成器 chunk 的 `produced_by` 永远是 `None`** | 同上（state API 里查不到产出者） | 排查流式任务时**问不出"这个 chunk 是谁产的"** |

**修完后的验证**：回归用例 **17 → 26**，测试 **165 → 174**，**零回归**。
每一条回归测试都验证过"**没有修复时会失败**"（逐个把修复回退、单独跑、确认 exit=1，再还原）。

**方法论增量（可复用）：**
**不要问"哪些路径没测"，要问"哪些路径的孪生路径没测"** ——
测试漏掉的情况不是随机分布的，而是**沿着实现路径成片地漏**。

### 第二类：本轮自己造成的错（这一类最有教训价值）

**① "查到一半就下结论"：`ConsistentHashRouter` 的乌龙。**

主线程在写第 38 章时认定 `ConsistentHashRouter` "**不在 Ray 2.58.0 里**"，
依据是"我列了 `python/ray/serve/_private/request_router/` 目录，里面没有它"。
**它是存在的** —— 在 `python/ray/serve/experimental/consistent_hash_router.py:39`：

```bash
curl -s https://raw.githubusercontent.com/ray-project/ray/ray-2.58.0/python/ray/serve/experimental/consistent_hash_router.py | grep -n "^class "
# → 39:class ConsistentHashRouter(RequestRouter):
```

**加重情节**：本书**第 15 章 §15.7 早在上一轮就已经把它写进了"三个可选 router"表**，
并且给出了正确的 import 路径 —— **本书自己就证伪了这句话**。
更糟的是，这个错误结论一度被**通过消息传递给了负责第 37 章的那一路**，
于是它也被写进了 §37.15，已一并修正。

**教训**：**"我在某个地方没找到"永远不等于"它不存在"。**
正确的核实命令必须查**两个**目录（`_private/request_router/` 与 `experimental/`）。

**② 加了一章之后，全书的"收尾点"集体过期。**

新增第 38 章让"全书最后一章"从 37 变成 38，于是 5 章里 **8 处**
"全书结语在哪""后面还有几章"的说法**同时失效**：

| 位置 | 原来说 | 现在 |
|---|---|---|
| 第 31 章 §31.11 | "真正的收尾在**第 37 章**"、"离全书结尾还有 **5** 章" | 第 38 章 / **7** 章 |
| 第 32 章 | "本章之后还剩 **4** 章" | **6** 章 |
| 第 33 章 | "后面还有 **3** 章"、"33–36 **四**章" | **5** 章 / **6** 章 |
| 第 36 章 §36.11 | 结语链接指向第 37 章 | 指向第 38 章（**行内链接文字与目标文件同时改**） |
| 第 37 章 | `## 结语：这本书真的讲完了` | 改为 §37.18「本章之后」，**结语整体搬到第 38 章末尾** |

**这是第四轮那次"三章各自主张自己是全书结尾"的同一个形状，又发生了一次。**
它说明：**"改动本身也需要被检查"这件事，不会因为你已经知道它就不再发生。**
第 38 章的结语把这条写成了给读者的第五条教训。

### 第三类：会直接崩 / 会永久挂起的文档硬错

| 位置 | 原文 | 事实 |
|---|---|---|
| 第 12 章 §12.6 | `ctx.use_streaming_executor = False  # 回到旧的 bulk executor` | **整节改写**：`use_streaming_executor` 在 2.58 的 `context.py` 里**零命中**，仓库里**根本没有 `bulk_executor.py`**；而 `DataContext.__setattr__` 是一长串 `if/elif` **没有兜底 `else: raise`** → 该赋值**静默写进实例、什么都不发生**（"关不掉了"，不是"能关"） |
| 第 12 章 | `ctx.execution_options.resource_limits.object_store_memory = ...` | `object_store_memory` 是**只读 property**（无 setter）→ `AttributeError`；且 `ExecutionResources` **不是 dataclass**，原文建议的 `dataclasses.replace` 会 `TypeError` |
| 第 13 章（多处） | `Checkpoint.from_dict({...})` / `ckpt.to_dict()` | **2.58 里这两个已不存在**：`_CheckpointMetaClass.__getattr__` 对 `from_dict`/`to_dict`/`from_bytes`/`to_bytes` **抛迁移错误**（"only directories are supported"）。示例代码**跑起来直接 `AttributeError`** |
| 第 13 章 | `ScalingConfig(use_cpu=...)` | **`use_cpu` 在 `ray/air/config.py` 与 `train/v2/api/config.py` 里各零命中**；改用 `resources_per_worker={"CPU": n}` |
| 第 14 章 | `CheckpointConfig(checkpoint_frequency=5, checkpoint_at_end=True)` | 两个字段在 V2 里是**弃用哨兵，传了就 `raise DeprecationWarning`** —— 原文还给了"存太勤→I/O 瓶颈"的调参建议，方向完全反了 |
| 第 17 章 §17.6 | `RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S` 默认「**0 = 关闭**」 | 源码里默认是 **`30.0` 秒、排空默认开着**，`0` 才是关掉。原文的推论（"不要依赖默认值，因为默认值本身是矛盾的"）**导出方向与事实相反**，连带改了小结与清单 |
| 第 21/22 章 | `NodeAffinitySchedulingStrategy` 候选为空时"**直接抛 `ScheduleError`**" | 异常在 raylet 侧被接住，只写进任务的 `error` 字段 —— **调用方 `ray.get()` 永久挂起**。别写 `except ScheduleError` |
| 第 21 章 | `get_object(id)` | 真实 API 是 **`get_objects(...)`**（复数），**没有单数函数** |
| 第 26 章 | "编译图**不支持 kwargs**" | kwargs **可用**（`InputNode()["name"]` 声明）；真正的约束是**不能与直接输入混用**。引文 "Compiled DAGs do not support kwargs" 在 2.44/2.51/2.58 全树**检索不到** |
| 第 26 章 | `visualize(..., return_dot=...)` | **该参数不存在**（签名只有 `filename/format/view/channel_details`）；返回值本身就是 DOT 字符串 |
| 第 27 章 | uv 集成在 Ray Client 下"**不工作**" | **可用**（2.58 有客户端侧钩子）；且变量名是 `RAY_ENABLE_UV_RUN_RUNTIME_ENV`，**不是** `RAY_RUNTIME_ENV_HOOK` |
| 第 28 章 §G.3 | Slurm 示例 `--address=127.0.0.1:6379 --min-nodes=4` | `symmetric_run.py` 会**剔除所有 localhost IP**，于是**每个节点都判定自己不是 head**，全部退进 worker 分支，超时后 `Timed out waiting for head node to start.` |

### 第四类：跨章矛盾、"上一轮修出来的错"与过期数字

* **`init_process_group` 默认超时**：通用默认 **30 分钟**，**只有 NCCL 后端**才是 10 分钟。
  第 30 章写 10 分钟是**对的**（上下文明确指定了 `backend="nccl"`）；
  分片 C 一度提出异议，被分片 D 基于 NCCL 源码**驳回** —— 这条留在"驳回清单"里。
* **RDT（Ray Direct Transport）的版本号第三次被写错**：第六轮之后被改成"**最早出现在 2.52**"，
  依据是在 `ray_constants.py` 里 grep `Ray Direct Transport`。**这个依据本身不成立**：
  实测 **2.52.0 / 2.52.1 都是 0 命中，最早出现在 2.53**；而且那句只是**注释里的一次提及**。
  更硬的证据是**模块**何时存在：`python/ray/experimental/rdt/__init__.py` 在 **2.54 是 404、2.55 起 200**。
  现在统一为："**API 从 2.48 起；RDT 作为一个模块最早出现在 2.55**；'2.50 公布 alpha'仍**未确认**"。
* **"内存监控进程"是错的**：`node_manager.h:1022` 里 `memory_monitors_` 是 **raylet 内的成员变量**，
  不是独立进程；`services.py` 里 grep `memory_monitor` **一个进程都没有**。
  第 7 章已改（术语表本来就对，是第 7 章错）。
* **OOM 报错文案在 2.55 变过**：`Task was killed due to the node running low on memory.`
  → **`N worker(s) were killed due to the node running low on memory.`**
  第 18 章与附录 D 都引了旧文案 —— 照旧文案去 grep 日志会**一行都搜不到**。
* **过期数字**：第 22 章 §B.7 的验证矩阵漏了 `test_regressions.py`，行合计 148 ≠ 165；
  第 2 章 §2.8/§2.11、第 23 章 §C.6/§C.8、第 6 章 §6.12、第 26 章 §26.9 等多处
  "改了正文、漏了小结"或数字未同步。全书的测试数本轮统一到 **174**。
* **第 11 章的章内自相矛盾**：§11.2/§11.4 刚说"扁平的 `ray logs <worker_id>` 早已不可用"，
  §11.8 的排查表里又用了扁平写法 → 改为 `ray logs actor --id=<actor_id>`。

### 第五类：补齐的内容缺口

* **`ray.experimental.tqdm_ray`**（第 11 章新增 §11.7.1）—— 分布式进度条：
  多 worker 时原生 tqdm 会互相覆盖输出；`tqdm_ray` 让 worker 只上报、driver 统一渲染。
  代价是**每个 worker 独立上报**（走日志转发流），且上报节流默认 `flush_interval_s=1.0`
  → **不适合高频计数**；首次使用会**改写 `builtins.print`**（`RAY_TQDM_PATCH_PRINT=0` 可关）。
* **`ray.widgets`**（第 33 章 §33.8）—— Jupyter 里的富文本展示。
  真实公开导出只有 `Template` 与 `make_table_html_repr`；缺 `ipywidgets>=8` 时只打 warning 退回纯文本；
  **故意在 IPython 终端与 Colab 关闭**。诚实定位：**锦上添花，不要为它排故障**。
* **Delta / Hudi / Kinesis**（第 12 章 §12.3）—— Delta 一次只能读一张表、写只有 APPEND/OVERWRITE；
  Hudi **只能读**（没有 `hudi_datasink.py`），且 **`filters` 只在分区列上生效**（非分区列 filter **不报错也不过滤**）；
  **Kinesis 根本不存在**（给出可复现判定与三条替代路径）。
  收尾三件"诚实的没有"：**无无界/流式源**、**无 exactly-once sink**、`from_huggingface` 只有读没有写。
* **多租户与配额**（第 17 章 §17.9）—— 先给"Ray **没有**配额机制"的零命中证据，
  再用一张表逐项判定**哪些是真隔离、哪些只是约定**（`namespace` ❌ 只是命名分组；
  KubeRay 每租户 `RayCluster` 与 K8s `ResourceQuota`/`LimitRange` ✅ 真边界）。
* **非 x86 架构与 Windows**（第 17 章 §17.10）—— 平台清单来自 `get_wheel_filename()` 的实际分支
  （**`win32` 只有 `win_amd64`，没有 `win_arm64`**）；aarch64 是一等公民（有整条 CI 流水线）；
  Windows 集群依据 `ray_constants.py` 那句 *"Ray clusters are not explicitly supported for Windows and OSX"*。
* **API 稳定性与弃用策略**（第 20 章 §20.9.1）—— 🔴 核心结论：
  **Ray 里有**两套**弃用机制，行为完全不同**。
  **A 套**走 `logger.warning`，**`-W error::DeprecationWarning` 抓不到**；
  **B 套**走真正的 `warnings.warn(..., RayDeprecationWarning)`，能被 `-W` 抓到；
  而 `warning=False`（默认）时**只改 docstring、运行时不告警**。
  给出三道可执行的扫描网。
* **推理引擎全景与分离式服务**（第 37 章新增 §37.15，约 210 行）——
  七个引擎的对比表（vLLM / SGLang / TensorRT-LLM / Triton / LMDeploy / TGI / llama.cpp）；
  分离式服务三分解（PD 分离 / KV 传输 NIXL / KV 分层 LMCache）；
  **Dynamo 与 Ray Serve 的"协作 + 张力"**（附源码级证据，张力部分**标"无定论/未确认"**）。
  原 §37.15/§37.16 顺延为 §37.16/§37.17。
* **附录 C 新增 13 条 Agent 术语**（`## C.11`，原索引顺延为 §C.12）——
  Agent、长时程、会话亲和性、`ConsistentHashRouter`、人类审批、提示注入、MCP、轨迹、
  agentic RL、分离式服务、NIXL、LMCache、Dynamo。**A2A 全书未展开，故不设条目**（不凭空造定义）。

### 本轮的方法论增量

1. **修 bug 时要问"这个能力有几条实现路径"。** 第六轮修好的 10 个缺陷，
   只覆盖了分布式路径；这一轮的 9 个缺陷**全是另一条路径上的同一个 bug**。
   测试的漏不是随机的，是**沿着实现路径成片地漏**。
2. **"我在某个地方没找到"≠"它不存在"。** 本轮的 `ConsistentHashRouter` 乌龙，
   和第四轮的"三处一致了对的是错的值"、第六轮的"测试全绿了"是**同一个错误的三个变体**：
   **拿到了一个局部事实，就把它当成了全局结论。**
3. **改动本身也需要被检查。** 加一章 → 8 处"全书收尾点"失效。
   这是第四轮同类错误的**重演**，说明它不会因为"已经知道"就不再发生。
4. **新写的章节仍然要单独派一路独立复核。** 第六轮这条规则救回了 14 处问题；
   这一轮它救回的是**一个会把读者引向完全相反结论的 router 断言**
   （以及连带的 6 处下游结论）。

---

## 第六批修订清单（2026-09 第六轮审计）

**本轮新增第 37 章，并且第一次把审计重心转回代码本身。**

前五轮的审计对象都是**文档**。这一轮在四路文档审计（00–11 / 12–20 / 21–28 / 29–36）
之外，**加了一路 mini-ray 代码审计** —— 结果证明这是本轮最有价值的一个决定：
在 **148 个测试全绿**的情况下，代码里活着 **10 个真实缺陷**。

### 第一类：mini-ray 代码缺陷（本轮最高危）

这一类全部满足同一个形状：**用户拿到错误结果、或者永远卡住，却没有任何诊断信息。**
对一个教学实现来说，这是最坏的一种失败 —— 读者会以为是自己的代码写错了。

| # | 位置 | 症状 | 根因 |
|---|---|---|---|
| 1 | `raylet.py` `create_actor` | **`num_gpus>0` 的 actor 完全创建不出来** | `Resources.from_request(num_cpus, num_gpus, memory, custom)` 的**第三个位置参数是 `memory`**，而调用点传的是 `1.0 if num_gpus else None`。于是 GPU actor 凭空多要 1.0 内存，而节点总内存是 `0.0` → `ScheduleError`，**报错还把矛头指向内存** |
| 2 | `raylet.py` `task_failed` / `object_ref.py` | **生成器任务失败 → 消费端永久死循环** | `task_done` 会调 `_finish_generator`，`task_failed` **不会** → `done` 永远是 `False`；而 `wait_for_generator` 超时**只返回、不抛错**，`for ref in gen` 每 60 秒空转一次，**永不结束、不报错** |
| 3 | 同上 | 失败会**覆盖消费者已取走的数据** | 生成器的 `result_ids` 是边产边追加的，"把异常写进每一个结果对象"会连带覆盖已交付的 chunk |
| 4 | `raylet.py` / `object_store.py` | **引用计数整条链路从未生效**，且被静默吞掉 | raylet 调用 `store.set_ref_count(...)`，而**这个方法根本不存在** → 每次上报抛 `AttributeError`、每次被 `except Exception: pass` 丢掉 → `ref_count` 永远停在插入时的值，**「refcount 归零才回收」成了死代码** |
| 5 | `worker.py` `_AsyncActorContext` | **async actor + `concurrency_groups` → 永久挂起** | async 执行上下文只轮询 `""` 组，而 raylet 按方法声明的组投递 → 投进 `"io"` 邮箱的任务**永远没人取** |
| 6 | `raylet.py` `_dispatch` | 调度竞争下**任务被静默丢弃** | `assign` 失败时调 `remove_task`，之后**再没有人会写它的结果对象** → `ray.get` 永久挂起 |
| 7 | `core_worker.py` `fetch_dep_values` | 取消/丢失的依赖被**当成普通参数喂进用户函数** | 判据写成 `isinstance(value, RayTaskError)`，而 `TaskCancelledError` / `ObjectLostError` 继承的是 `MiniRayError`，**穿过**检查 → 用户看到无关的 `TypeError`，真正原因被掩盖 |
| 8 | `scheduler.py` `_release` | **资源被重复归还**，`available > cluster_resources` | `_release` 不幂等：取消路径归还一次、临死 worker 的 `task_done` 在另一个线程里再归还一次 |
| 9 | `raylet.py` / `worker.py` | actor 的 GPU **从来没传给它自己** | 资源在 raylet 侧扣对了，但 `gpu_ids` 没进 actor 进程 → `ray.get_gpu_ids()` 返回 `[]`、**`CUDA_VISIBLE_DEVICES` 根本没设**，actor 里的 `torch.cuda` 会看到机器上**全部**的卡 |
| 10 | `raylet.py` `RayletConfig` | 任何带 `memory=` 声明的 actor 都**调度不上去** | `memory` 是合法选项、教程也写着它"参与调度"，但**节点总内存被硬编码成 `0.0`** —— 顺带补上了节点内存建模（`ray.init(memory=...)`，默认按可用内存 70% 探测） |

**另外三处"沉默的失败"被改成"说出来"**：

* `ReferenceCounter.flush` 的 `except Exception: pass` → 只在收尾阶段安静，
  运行期的错误打 warning（**正是这个 `pass` 让 #4 瞒过了 148 个测试**）；
* `_report_ref_counts` 里「上报计数失败」不再连带跳过「释放 pin」；
* actor 声明了**不存在的并发组**时，从"永久挂起"改成**创建时报错**。

**新增 `tests/test_regressions.py`（17 个测试）**，专门覆盖上面每一条 ——
它们盯的正是"以前一个测试都没有"的那几条路径（生成器、引用计数链路、GPU actor、
async + 并发组）。**148 → 165，零回归。**

### 第二类：会直接崩的文档硬错（10 条）

| # | 位置 | 写了什么 | 实际 |
|---|---|---|---|
| 1 | 第 29 章（3 处） | `from ray.train.huggingface import RayTrainReportCallback` | 该 `__init__.py` 是**空文件**；正确路径是 `ray.train.huggingface.transformers`。**本章两个旗舰示例都会在 import 行崩掉** |
| 2 | 第 29 章 | `TransformersTrainer` 标"存在，V2 可用性未确认" | **2.9 起已从 Ray 移除** —— 与第 13/21 章和本 README 直接矛盾（这是"改了别处、漏了这里"） |
| 3 | 第 29 章 | `ray.data.from_items(gen(), parallelism=32)` | `from_items` 内部调 `len()` / `items[j]` → 生成器 `TypeError`；且 `parallelism` 已弃用 |
| 4 | 第 31 章 | `from ray.util import profile` | **`ray.util.profile` 不存在** → `ImportError`。**而且同一节下面还写着"本节不给 `ray.util.profile` 的示例"** —— 上面就有 |
| 5 | 第 33 章 | `ray timeline --output=timeline.json` | `ray timeline` **只有 `--address` 一个选项**，输出路径自动生成 → click 直接拒绝 |
| 6 | 第 33 章 §33.8 | "Ray Distributed Debugger 是**终端形态**的（`ray debug`）" | §33.6 刚刚确立 `ray debug` 是 **legacy**、新版是 **VS Code 扩展** —— **同章自相矛盾** |
| 7 | 第 34 章 | `import pyarrow.dataset as pa` 后用 `pa.dataset.field(...)` | `pa.dataset` 是该模块里的**函数**，`.field` → `AttributeError` |
| 8 | 第 26 章（4 处） | `TorchTensorType(transport="nccl")` | 构造函数**只接受** `auto`/`cpu`/`accelerator` → `ValueError`；`"nccl"` 只对 `with_tensor_transport()` 合法 |
| 9 | 第 26 章 | `with_type_hint()` | **2.58 里不存在**（不是"两个名字择一"）→ `AttributeError` |
| 10 | 第 12 章 | `DatasetPipeline` / `Dataset.repeat` / `Dataset.to_torch` / `ds.unique()` 返回 `Dataset` / `with_column` 与 `add_column` 的语义**写反了** | 全部与实际版本不符；`DatasetPipeline` 在 2.58 已**删除** |

### 第三类：跨章矛盾与"上一轮改了正文、漏了小结"（本轮的固定形态）

* 第 13 章**小结**还写着 `as_directory()`"可能返回 `str`"，而**正文**第四轮已改对；
* 第 14 章**小结**还说 `AxSearch` 不支持保存/恢复，而**同章表格**已写明它真的实现了；
* 第 34 章 §34.3 说 `XGBoostTrainer` 的 V2 状态"未确认"，与第 13 章"已 V2 化"矛盾
  —— 而源码里 `ray/train/v2/xgboost/` 就在那儿；
* 第 21 章 A.11 把**2.58 已移除**的 `RAY_worker_idle_timeout_ms` 当活配置列着，
  而第 23 章写着它不存在；
* 第 12 章与第 19 章同错：`write_databricks_table` **也不存在**（第四轮"改"出来的名字）；
* 第 21 章说 `OwnerDiedError` 与 `ObjectLostError`"语义完全不同、别混用"，
  而源码里 `class OwnerDiedError(ObjectLostError)` —— **它是子类**，
  这直接影响读者的 `except` 范围；
* 第 24 章说 `ActorDiedError` 继承 `RayTaskError`，实际是 `RayActorError → RayError`
  —— **`except RayTaskError` 捕不到它**，而这正是读者会照抄的地方；
* 第 9 章的 actor 资源语义写错了（见下）。

### 第四类：把源码里查得到的东西标成"未确认"

前几轮确立的纪律是"查不到就标未确认"。本轮发现这条被**用过头**了 ——
有两处把**源码里明确写着**的东西标成了未确认，反而误导读者：

* 第 36 章 §36.2 说"Ray 2.58 没有公开的 tracing 实现" ——
  实际有 `ray/util/tracing/tracing_helper.py`、`setup_tempo_tracing.py`，
  以及 `ray start --tracing-startup-hook`（`hidden=True`）。
  ⚠️ **这条是第五轮把一句正确的话"改"错的** —— 又一个"上一轮修出来的错"。
* 第 34 章 §34.3 / 第 27 章 §F：`XGBoostTrainer` 的 V2 状态、
  `worker_process_setup_hook` 的存在性，都能在 2.58 源码里直接核到。

> **教训**：**"未确认"是一种结论，不是一个安全的默认值。**
> 能核而不核，和凭空断言一样是错误 —— 只是方向相反。

### 第五类：第 9 章的 actor 资源语义（一条被三方证据共同定死的事实）

第 9 章原文写"**actor 的资源是终身持有的**""**actor 默认占 1 CPU**"。
回 Ray 2.58 源码看，真相是**分两种情况**的（`python/ray/actor.py` 的 `_remote()`，
官方文档 `doc/source/ray-core/scheduling/resources.rst`）：

| | 完全没写资源 | 写了资源 |
|---|---|---|
| 创建时占的 CPU | **0**（终身持有 0） | **1**，**终身持有** |
| 每次方法调用 | **1** | **0** |

"终身持有"**只在写了资源时才成立**。第 18 章 §18.7 其实写对了，
第 9 章写错了 —— 而这条直接决定读者怎么估算 actor 容量。
已改成对照表，并**在 mini-ray README 里明确标注了 mini-ray 的简化**
（mini-ray 只有"终身持有"这一种模型，不为方法调用扣 CPU）。

### 第六类：补齐的内容缺口

* **新增第 37 章**（推理引擎层），补上"连续批处理 / 投机解码 / CUDA Graph"
  这三块**前 36 章命中为 0** 的空白；
* 第 8 章补上客户端的 **`label_selector` / `fallback_strategy`**（README 第四轮声称补过，实际只有第 12 章有）；
* 第 26 章补上 **`RAY_CGRAPH_teardown_timeout`**（默认 **30**，不是 10）；
* 第 33 章给 `ray health-check` 加上"`hidden=True` + 官方自称 **NOT a public API**"的警告；
* 第 36 章补上 **Ray 其实有自动 context 传播**（`_ray_trace_ctx`）这一节 ——
  原先把手工 `propagate.inject` 讲成了唯一路径。

### 本轮的方法论增量

1. **代码审计要单独派一路。** 文档审计再细，也查不出"文档描述的行为在代码里根本不生效"。
   #4（引用计数）就是典型：文档写得对、代码也在调，**但那个方法不存在**。
2. **"148 个测试全绿"必须被当成一个待解释的现象，而不是一个结论。**
   真正该问的是"**哪些路径一个测试都没有**" —— 本轮 10 个缺陷全部落在
   生成器、引用计数链路、GPU actor、async 并发组这四块，而它们此前**覆盖率为 0**。
3. **回源码核对的纪律要双向执行。** 它既用来**确认**（`OwnerDiedError` 是子类、
   `TorchTensorType` 的合法取值），也用来**驳回**（`XGBoostTrainer` 不是"未确认"）。
4. **⭐ 新增的章节本身也要被独立审计，而且要趁早。**
   第 37 章是本轮新写的。写完之后**立刻**派了一个独立小组去逐条核对
   （Ray 源码 / vLLM 源码 / KV cache 算术 / 跨章引用），结果抓到 **14 处问题**，
   包括：
   * 一个会在 pydantic 校验就报错的字段名（`ray.data.llm` 的 `model_source`
     被写成了 `model` —— 而 `extra="forbid"`，示例**跑不起来**）；
   * **一句自我否证的"零命中"断言** —— 初稿写"「投机解码」「CUDA Graph」
     在前 36 章命中数为 0"，而**第 00 章的章节目录里逐字就有这两个词**
     （本书章号从 00 起）。**和本书前几轮修正过的那几条自我否证断言
     是同一个形状** —— 说明这个坑不因为"知道它"就不会再踩；
   * 5 处交叉引用"指到了不讲这件事的地方"；
   * 1 处**技术性过度断言**：把 `max_model_len` 说成"决定预留多少 KV 空间"
     —— 那是 **V0 时代的行为**，vLLM V1 的 KV 池是
     `gpu_memory_utilization × 总显存 − 权重 − profiling 峰值` 算出来的。
     实践建议（别乱设大）仍然对，但**机制描述是过时的**。

   这些都**已修**，并且把自我纠正的过程留在了第 37 章的正文里
   （读者能看见"这本书也会写错、以及怎么发现的"）。
   **教训是：新写的章节是最危险的部分** —— 它没有经过任何一轮交叉审计，
   而人对自己的新作最容易"看着都对"。

---

## 第五批修订清单（2026-09 第五轮审计）

**本轮不新增章节**（37 篇正文的覆盖面已经够宽，且重排会打断所有跨章引用）。
80 余处改动全部是**修正与补齐**。审计方法是四路并行分片
（00–11 / 12–20 / 21–28 / 29–36）+ **逐条回 Ray 2.58.0 源码核对**，
另有一个专门的小组负责求证 Serve / RLlib / 生产安全三块的事实。

### 第一类：前四轮"修"出来的错（本轮最高危）

这一类是本轮**新识别**的形态：**上一轮引入了新结论，却没回头验证结论本身。**

| # | 位置 | 第四轮写的 | 2.58 源码/实跑 | 影响 |
|---|---|---|---|---|
| 1 | 第 3 章 §3.6、第 7 章 §7.9/§7.10（**5 处**） | `free_objects_period_ms` 默认 1000 ms、`free_objects_batch_size` 默认 **10000** | 真名是 **`free_objects_period_milliseconds`**（`ray_config_def.h:173`，默认 1000）；`free_objects_batch_size` 默认 **100**（`:181`） | 🔴 配置名即环境变量名，写错**静默无效**；10000 让"多久 flush 一次"的估算完全错位。**三处"统一"了，但对的是错的值** |
| 2 | 第 4 章 §4.5、第 11 章 §11.6、第 21 章 A.11（2 处）、第 31 章 §31.5/§31.10、第 33 章 §33.6/§33.11（**6 处**） | "timeline 没开 profiling 时**通常直接报错**"、并特意加了"不要以为是空文件" | `state.py` 的 `chrome_tracing_dump()`：`if not all_events: logger.warning(...)`，**随后无条件 `open(filename,"w")` + `json.dump(all_events, ...)`** → **是 warning，不抛异常，文件照写，内容是 `[]`** | 🔴 **第四轮改错了方向**：改之前是对的。而且第四轮还漏掉了提示语里的后半句 —— **还要设 `RAY_task_events_report_interval_ms=0`**。读者按"等报错"去排查，实际会先拿到一个"看起来成功"的空文件 |
| 3 | 第 8 章 §8.8 | "Autoscaler v2 自 2.54.0 起默认开启（release notes）"，**同节下面又说"没找到发布说明原文"** | `RAY_CONFIG(bool, enable_autoscaler_v2, false)`；`start_monitor(..., autoscaler_v2: bool = False)`；`autoscaler/v2/utils.py`：*"If env var is set to enable autoscaler v2, we should always return True"* → **v2 是 opt-in** | 🔴 同节自相矛盾 + 与第 1 章"未确认"不一致 + **与源码相反**。三处已统一 |
| 4 | 第 10 章 §10.3 | "**但「杀谁」不是「杀占用最多的」**"，只给了 time-based policy 一条路 | 官方 OOM Prevention 是**两路**：**idle worker → "select the worker with the largest memory footprint first"**；active worker 才走"先可重试、再最新" | 🔴 对 idle worker 而言**说反了**，且与第 7 章 §7.7 直接冲突。两章已同源 |
| 5 | 第 11 章 §11.1、第 36 章开篇 | "`OpenTelemetry` 在前 35 章**命中数是 0**，`Jaeger` 是 0"（并教读者"一个 grep 就能确认"） | 实测前 35 章有 **8 行**提到 `OpenTelemetry`、**3 行**提到 `Jaeger` —— **写出这句话的那一行本身就是一处命中** | 🔴 **自我否证的论证**：读者照做一条 grep 就能推翻本章的立论基础。已改成定性表述（"本轮之前没有任何一章真正讲过"），并把它当反面教材写进正文 |

### 第二类：照抄会报错 / 结论完全错的

| 位置 | 原来 | 现在 |
|---|---|---|
| **第 13 章 §13.3** | 推荐 `from ray.train.lightning import LightningTrainer` / `from ray.train.huggingface import TransformersTrainer`，表格里 V2 状态标"未确认" | ❌ **两个都已从 Ray 删除**（2.7 弃用 → 2.8 报错 → **2.9 移除**，REP *"Unify Torch based Trainers on the TorchTrainer API"*）。`AccelerateTrainer` 同样。统一收敛到 **`TorchTrainer`**；`ray.train.lightning` 模块还在，但 `__all__` 里没有 `LightningTrainer` |
| **第 13 章 §13.5** | 表格列出了 `Checkpoint.from_uri(uri)`；`ckpt.as_directory()` 的"**两种返回形态**（本地返回 `str`、远端返回上下文管理器）"，并配了一段 `if isinstance(cp, str): ... else: with cp as d:` 的"正确姿势" | ❌ **`from_uri` 不存在** → 用 `Checkpoint(path="s3://…")`。❌ **`as_directory()` 永远只返回上下文管理器**（`@contextlib.contextmanager`，`-> Iterator[str]`），差别只在里面（本地不拷贝不删、远端下载后退出即清理）。那段"两种形态"的代码会**永远走 else 分支**，把一个不存在的坑教给了读者 |
| **第 13 章 §13.3** | V1/V2 对照段落里仍有 `ray.train.get_world_rank()`「模块级快捷函数与 `get_context()` 方法**都在**」 | 🔴 **`ray.train.get_world_rank()` 不存在**（`ray/train/__init__.py` 的 `__all__` 里一个 rank 助手都没有）。第四轮只改了 §13.3 的警告框和 §13.11 的小结，**这一段漏改** —— 与本章自己两处**直接冲突** |
| **第 13 章 §13.2** | "V2 的容错只由 `max_failures`（默认 0）加 `controller_failure_limit` 控制" | **漏了第三个字段 `max_preemption_failures`（默认 -1）** —— 而"抢占单独计数、不占 `max_failures` 额度"正是 V2 的关键设计，与 §13.5 冲突 |
| **第 12 章 §12.2** | "`min_size=0` 允许全部缩掉；所以 `min_size` 通常不该是 0" | ❌ **`min_size` 下限是 1** —— `ray/data/_internal/compute.py`：`if min_size is not None and min_size < 1: raise ValueError("min_size must be >= 1", min_size)`。传 0 **直接报错**，不是"缩到 0" |
| **第 14 章 §14.13** | 环境变量表里有 `TUNE_DISABLE_AUTO_CALLBACK_SYNCER` | ❌ **这个变量不存在**（2.58 的 `ray/tune/constants.py` 里零命中）。改为真实存在的 `TUNE_DISABLE_AUTO_CALLBACK_LOGGERS` |
| **第 29 章 §29.2** | "HF 的 `load_dataset(..., streaming=True)` 返回 `IterableDataset`，**不能**直接喂给 `from_huggingface`"；"`DatasetDict` **可以**一次转进来，拿到 `dict[str, Dataset]`" | 🔴 **两条都说反了**。签名是 `dataset: Union[datasets.Dataset, datasets.IterableDataset]` → **流式可以**；文档串明写 *"`DatasetDict` and `IterableDatasetDict` are not supported"* → **整包传不行**。原文**恰好劝退了大数据集唯一正确的路径** |
| **第 29 章 §29.5** | 注释说"`adapter_name` 不要用 `default`，重名会抛异常。**要么省略**，要么换个名字" —— 而代码正是"省略" | 🔴 **注释与它自己的代码打架**（省略就等于 `default`）。改为 PEFT 续训的标准写法 `PeftModel.from_pretrained(model, path, is_trainable=True)` |
| **第 29 章 §29.4** | `from ray.train.torch import TorchTrainer, prepare_model` 后写 `optimizer = prepare_optimizer(optimizer)` | `prepare_optimizer` **没导入**、右侧的 `optimizer` **从未定义**。已补 import 与 `build_optimizer(model)` |
| **第 29 章 §29.4** | `checkpoint_score_attribute="loss"` 注释成"多份并存时挑最好的那份；留给 `result.checkpoint` / `get_best_checkpoint` 用" | 它的真实语义是：**设了它，`num_to_keep` 保留的是"按该指标排名的前 K 份"，而不是"最近的 K 份"**；与 `result.checkpoint` / `get_best_checkpoint()` **无关**（后者的 metric/mode 是显式传参）。与 §29.3 的口径已统一 |
| **第 29 章 §29.7** | 表格行"`ray.put` 广播 → worker **零拷贝取**"，下方警示又说"8 个 worker 抢同一个只读缓冲区然后**各自复制一遍**" | 同一节两句话说反。已收紧为：**零拷贝只对 Arrow/ndarray 类载荷成立，`nn.Module` 经 `ray.get` 是各自反序列化**（GPU 张量是否走 CUDA IPC **未确认**） |
| **第 16 章 §16.2** | `rllib_contrib` 是"**社区维护子包**"，装在 `pip install "rllib-contrib-a3c"` | ❌ 它是 **`ray-project/ray` 仓库里的一个目录**（不是独立仓库）；安装名是 **`rllib-a3c`**（`rllib-contrib-a3c` 在 PyPI 上 **Not Found**）。**且该目录已在 2.40 从主仓库删除**（2.39 是最后一版） |
| **第 16 章 §16.15** | `env_runner = config.build_env_runner()`，标"未确认" | ❌ **这个方法不存在**（2.58 的 `algorithm_config.py` / `algorithm.py` 零命中）。`build_learner_group()` 存在；拿现成 EnvRunner 用 `algo.env_runner_group` |
| **第 16 章 §16.18** | "`Algorithm.save()` / `restore()` 是旧 stack 的名字，**已弃用**" | ❌ **并未弃用** —— 它们**不在 RLlib 里定义**，而是继承自 Tune 的 `Trainable`，2.58 里标 `@DeveloperAPI`、**未标弃用**。准确说法是"RLlib **推荐** `save_to_path()`"，属**推荐差异**而非弃用 |
| **第 30 章 §30.9** | mini-ray 能力表把 `accelerator_type` / MIG 标 ✅ | ❌ mini-ray **没有这个参数**（`grep -rn accelerator_type mini-ray/` 零命中；未知 kwargs 只打一条 warning 就**静默忽略**）。要近似只能用自定义资源 `resources={"A100": 2}` |
| **第 31 章 §31.3** | `memray attach --stop 12345` | ❌ 没有 `--stop` 这个旗标。提前停止跟踪是**独立子命令** `memray detach <pid>` |
| **第 31 章 §31.9** | `ray.timeline("timeline.html", html="timeline.html")` | 同一个路径既当 JSON 又当 HTML 输出 → HTML **原地覆盖**刚写出的 JSON，示例①的两种产物只剩一种。改为 `ray.timeline("timeline.json", html="timeline.html")` |
| **第 33 章 §33.3** | 建议用 `ray.util.state.list_*()` 打印 `[0].keys()` 确认字段名 | ❌ `StateSchema` 是 dataclass，有 `__getitem__` / `get()` / `asdict()`，**没有 `keys()`** → `AttributeError`。改为 `asdict()` |
| **第 34 章 §34.5** | 示例里连着两次 `read_parquet` 同一个源（第一次是死代码），且用了 `pa.dataset.field(...)` 与 `np.log1p(...)` 却没 import | 删掉重复读、补 `import numpy as np` 与 `import pyarrow.dataset as ds`（**注意不是 `as pa`** —— `pa.dataset` 是模块里的**函数**，`.field` 会 `AttributeError`，第六轮已修正） |
| **第 35 章** | 交叉引用"第 32 章 **§32.6** 提过这个细节" | 该细节在 **§32.5** 的 `PromoteBest` 骨架里 |

### 第三类：跨章矛盾与"改了正文漏了邻节"

* **`ray debug` 张冠李戴**（第 4、11、21、23、33 章，**6 处**）——
  前四轮把 **`RAY_DEBUG=1` + `ray debug`** 当成 Ray Distributed Debugger 的用法。
  2.58 的 `scripts.py` 里 `debug()` 会打印 *"The distributed debugger … is now
  the default … If you want to keep using 'ray debug' please set
  `RAY_DEBUG=legacy`"*。**`ray debug` 是 legacy 入口**；新版是
  **VS Code 扩展 + `breakpoint()`**，前置是 `pip install "ray[default]" debugpy`，
  **不需要 `ray debug`**。而且 `RAY_DEBUG` 的取值里**只有 `legacy` 有特殊含义**。
  ⚠️ 第 11 章同一个 §11.7 里上面写"Distributed Debugger = `RAY_DEBUG=1` + `ray debug`"、
  下面写"旧的调试器要 `RAY_DEBUG=legacy`" —— **同节自相矛盾**，本轮一并拆成两行对照表。
* **`.bind()` 的"两种语义"是本书生造的**（第 2、21、22、26 章，**6 处**）——
  附录 A/B 与第 26 章说"① 参数部分套用（**Ray 2.8+**，返回 `RemoteFunction`）；
  ② 构 DAG（Ray 2.0+，返回 `FunctionNode`）"，与第 2/3 章**3:3 对立**。
  回源码：`RemoteFunction.bind()` 直接 `return FunctionNode(...)`；
  `FunctionNode` 有 `_execute_impl`（*"Executor of FunctionNode by ray.remote()"*），
  **没有 `.remote()`** —— 调用会抛
  `AttributeError: .remote() cannot be used on <class 'FunctionNode'>`。
  **"Ray 2.8 那一档"不存在**，六处已统一到"只有一种语义"。
* **`free_objects` 三处口径"统一"到了错的值**（见第一类第 1 条）。
* **第 3 章 §3.6 说"mini-ray 反而更激进"**，与第 6/7 章三处"Ray 更激进、mini-ray 更保守"
  相反 —— 已改写为"**上报间隔更短、但真正回收更保守**"，把两件事分开说。
* **`enable_resource_isolation` 的版本号**（第 8 章 **2 处**）：写的是"2.55 起"，
  实际 **2.51.0 就已是 `ray.init()` 的公开形参**、实现文件 2.48.0 就存在。
* **第 7 章 §7.7 "Ray 2.55 起内存参与调度决策"**：`memory` **自 2.0 起**就是
  `_resource_option` 成员，一直是参与调度的资源项。
* **第 15 章 §15.19 "四种拿法"**：§15.2 已改成"两种"，且实际是**三种**
  —— 补上公开 API **`serve.get_app_handle(name)`**（`@PublicAPI(stability="alpha")`）。
* **第 15 章 §15.4** 说"附录 A 的 Serve 表里只列了 `serve.ingress` 一行，没有 `@serve.batch`"
  —— 附录 A §A.9 里两者都有。
* **第 28 章 §G.3 小结说"四个坑"**，正文小标题写的是"五个"—— 补齐第 ④ 条
  （每个节点只起一个 Ray 进程）。
* **第 33 章 §33.6 与 §33.11 的 timeline 说法互相打架**（前者"空文件"、后者"报错"）
  —— 已统一到源码口径。
* **附录 B 的 `ray.util.list_actors()`** —— 附录 A §A.1 与附录 D 都纠正过
  "这个函数不存在"，附录 B 漏改；同时补上 mini-ray 也暴露了
  `miniray.state.list_actors()` / `miniray.util.state.list_actors()` 两个兼容路径。
* **第 26 章引用 `miniray/remote.py:164`** —— 实测 `def bind` 在 **170 行**。
* **第 1 章 §1.4 的 2.57 月份** —— 早先标的是 2026-07 的**推测值 + 未确认**；
  PyPI 上 `ray 2.57.0` 的上传时间是 **2026-08-11**，已改成实测值。
* **RDT 的起始版本**（第 1、3、7 章，**3 处**）—— 三处都写"2.50 起 alpha"。
  `@ray.method(tensor_transport=...)` 在 `ray/actor.py` 里的出现次数是
  **2.47=0 / 2.48=38 / 2.49=50 / 2.50=77**，即 **API 在 2.48 就落地**。
  已统一为"**2.48 引入 API、2.50 以 alpha 公布**"，并注明
  **官方 release note 未找到**。
* **`OwnerDiedError` 与 `ObjectLostError` 的类层次**（第 5、7、10 章）——
  语义区分是对的，但 2.58 的 `exceptions.py` 里写的是
  **`class OwnerDiedError(ObjectLostError)`**。所以
  **`except ObjectLostError` 会把两者一起吞掉**，要区分必须把
  `OwnerDiedError` 写在前面。三处都补了这条 + 代码示例。
* **第 11 章 §11.10 的 mini-ray 指标示例单位错**（`app_latency_seconds` +
  秒级分桶）—— mini-ray 的 `Histogram.timer()` 记录的是**毫秒**
  （`observe((perf_counter()-start) * 1000.0)`），照抄会让所有数值落进 `+Inf` 桶。
  已改名 `app_latency_ms`、分桶改毫秒级。
* **第 20 章 §20.5** 两行需要收紧：GCS 的 RocksDB 是"**新增可选后端**"而非
  "摆脱外部 Redis"（默认仍是 Redis）；task events 移出 GCS 是
  "**可选开关且默认关闭**"。
* **附录 C 的三处内部矛盾**：C.5 里"任务重试"有**两条**，其中一条正是被同节
  判定为"说反了"的旧话（本轮删除）；C.6 的 GCS 条与 C.5 相反（已统一）；
  `编译图 / Compiled Graph` 与 `Jobs API` 各有**两份完整定义**（已合并为指路）；
  索引里 `LightGBM`、`object_size` 的字母序错误已修正。

### 第四类：补齐的内容缺口

| 位置 | 补了什么 | 为什么 |
|---|---|---|
| 第 12 章 §12.2 末尾 | **一张常规表算子表**：`unique` / `aggregate`(+`Count`/`Mean`/`Sum`) / `groupby().aggregate()` / `rename_columns` / `with_column` / `add_column` / `iter_rows` / `to_torch`；以及 **`DataContext.use_streaming_executor = False`**（关掉流式执行器回到 bulk） | 本章主体是"管道"，容易让人以为 Ray Data 只有 `map_batches` + `filter`。这决定了"要不要为几个聚合回去上 Spark/pandas"。`with_column`（逐行）vs `add_column`（按批）的粒度差异有数量级代价 |
| 第 12 章 §12.6 | 具名 worker 选项补 **`fallback_strategy=`** | `#65501` 与 `label_selector` 一起加的软约束回退。（完整取值未确认，已标注） |
| 第 8 章 §8.4 之后 | **`label_selector` 与 `fallback_strategy`** | 第 8 章标题是"资源模型的全部细节"，却只讲了服务端的 `--labels`，**没讲客户端怎么按标签要资源**；`fallback_strategy` 全书 0 命中 |
| 第 9 章 反模式 4 | **`get_if_exists=True`** | 本章提出了"命名 actor 重名报错"这个高频问题，却没给 Ray 自带的一行修法（`Option(bool, default_value=False)`）：它是**幂等获取**语义 |
| 第 13 章 §13.6 | **弹性训练的入口**：`ScalingConfig(num_workers=(min, max))`，以及它必须配 `FailureConfig(max_failures=N)` 与周期性 `report(checkpoint=)` 才有意义 | 本章把"弹性"当成 Ray 的固有优势写进对比表，**却从没给过 API** —— 读者根本无从下手 |
| 第 16 章 §16.2 | **`RLlibCallback` 的常用钩子清单**（11 个） | 从 `DefaultCallbacks` 迁移时最需要对照的就是钩子名，原文只说了"基类改名" |
| 第 20 章 §20.6 | **推理编排层竞品**：NVIDIA Dynamo / llm-d / AIBrix / KServe | §20.2 的主线是"Ray 在推理栈里角色收缩"，而这一层恰是 2025–2026 竞争最激烈处，表格原先完全没提。已写明是"**集成 + 竞争**"的双重关系 |
| 第 20 章 §20.5 | **V1 Train API / `ray.air` 的移除时间线**（官方未承诺，标未确认）与 **Compiled Graph 的成熟度**（2.58 仍是 beta，GA 未确认） | 读者最关心的两个"什么时候能放心用"，原先在路线图里没有对应条目 |
| 附录 A §A.10 | 新增 6 行升级清单：`DAGNode.execute()` 弃用、三个已移除的 Trainer、`Checkpoint.from_uri()`、`ray.util.list_actors()`、RLlib 的 TF 移除版本、`rllib_contrib` 现状、`Algorithm.save()` **未**弃用 | §A.10 是"升级时最容易踩的地方"，这些恰好都是升级会撞上的 |

### 第五类：本轮新发现的 2026 年事实（不是修正，是新增）

* 🆕 **`DAGNode.execute()` 已被弃用**（PR **#63716**，关闭 issue **#63666**）——
  `DeprecationWarning: DAGNode.execute() is deprecated and will be removed in a
  future release.` **根因**：未编译路径每次 `execute()` 都走
  `FunctionNode._execute_impl()` 里的 `ray.remote(self._body)`，
  **每次执行都动态定义一个新 remote 函数**并导出元数据到 GCS 的
  internal KV（`fun` 命名空间），而这些条目**在 job 生命周期内无清理、无淘汰**
  → GCS KV **无界增长**。这正是官方警告过的"别在循环里重新定义 remote 函数"
  反模式，只不过被藏进了 `execute()` 里面。
  **已补进第 2 章 §2.2、第 21 章 §A.10、第 26 章 §26.2 与 §26.9。**
* 🆕 **RLlib 新 API stack 的默认化是三步，不是一步**（第 16 章 §16.1）：
  **2.38 = SAC/DQN**（#47217）→ **2.39 = PPO**（#48284）→ **2.40 = APPO/IMPALA/
  BC/MARWIL/CQL + 全局默认翻转**（#48516、#48599）。原文只写了"2.40+ 所有算法"，
  这在 **2.39** 上不成立 —— 排查"我的 PPO 行为为什么和教程不一样"时，
  这个版本边界就是答案。
* 🆕 **两处"看起来该改、其实不该改"的驳回**（**本轮明确不改**，记账备查）：
  1. 审计报告称 **`CheckpointConfig.checkpoint_frequency` 在 2.58 已弃用**
     （PR #58022）—— **回源码看是错的**：`ray/air/config.py` 里
     `checkpoint_frequency: Optional[int] = 0` 与 `checkpoint_at_end` 都是
     **普通字段、无弃用告警**（只有 `_checkpoint_keep_all_ranks` /
     `_checkpoint_upload_from_workers` 是 `_DEPRECATED_VALUE`）。故**不改**。
  2. 审计报告称 **`AxSearch` 不支持保存/恢复** —— 实际 2.58 的
     `ax_search.py` 里有真实的 `save(checkpoint_path)` / `restore(checkpoint_path)`
     （第 468/473 行）。第 14 章已按"**已实现**"改正。

### 本轮的方法论：为什么"上一轮修出来的错"最难查

前四轮的审计都在问"**这写得对不对**"。本轮第一次系统地问了另一个问题：
"**上一轮改的那一处，改对了吗**" —— 结果揪出了 5 条。

它们的共同形状是：**上一轮拿到了一条新信息（比如"实际是批量上报
不是立即回收"），把它套用到所有相关位置，却没有对"套用后的值"本身做一次
独立验证**（于是 `free_objects_period_ms` 这个错名字被"统一"到了三个地方）。
这和分布式系统的 bug 是**同一个形状**：**一次修正被传播到了多处，
但传播过程本身没有校验。**

所以本轮的验证纪律是：**凡是"回源码能定死"的条目，一律回源码**。
本轮 80 余处改动里，**约 60 处**是直接对着 `ray-2.58.0` 的 tag 源码
（`ray_config_def.h` / `state.py` / `scripts.py` / `exceptions.py` /
`compute.py` / `read_api.py` / `ax_search.py` / `tune/constants.py` …）
或 mini-ray 源码**逐行核对**过才落的笔；
另有 **2 条被这个纪律驳回**（见上）。

**已知仍未做的**（诚实说明，与前四轮一致）：
* 本机**未安装真实 Ray**（Python 3.14 兼容性未验证），
  所有 Ray 侧行为描述属"**对着 2.58 源码的文档级准确**"，
  不是"在本机跑过"；
* **未验证 GPU / 多机 / K8s** 相关章节（本机无 GPU、无集群）；
* 第 34–36 章的 XGBoost / LightGBM / MLflow / OpenTelemetry 示例
  均**未在本机安装对应库**；
* 仍有一批条目标着"未确认"（RDT 的 alpha 宣告版本、
  topology-aware 的 2.56/2.57 归属、auto scaler v2 的官方宣告、
  弹性伸缩的完整语义、`fallback_strategy` 的取值等）——
  **它们是诚实的空白，不是遗漏。**

---

## 第四批修订清单（2026-09 第四轮审计）

### 新增章节

| 章 | 补的是哪个洞 |
|---|---|
| **34** 表格数据与传统 ML | **`scikit-learn` 在前 33 章里命中数是 0**，`LightGBM` / `CatBoost` 也是 0，`XGBoost` 只有 8 次点名且**没有任何可运行片段**。而表格数据 + GBDT 是 Ray 落地量最大的场景之一。补上四条路径（单机 / Ray Data 做 ETL / `XGBoostTrainer` / Tune + sklearn）、全局类别编码的陷阱、checkpoint 布局，以及一张**"什么时候不该上 Ray"**的判据表 —— 本章有一半篇幅在用来说不要用 |
| **35** 数据版本、产物血缘与模型注册 | 第 32 章把读者领到「**谁是这个模型的生产版本，是你的事**」就停了，而且**那句之后没有下文**。补上：数据指纹算法（**Ray 没有公开的 Dataset 指纹 API —— `Dataset` 内部的 UUID 不是版本标识**）、`Checkpoint.set_metadata()` 血缘、MLflow Registry 的 `alias`（不是已弃用的 `stage`）与回滚、DVC/lakeFS 与 Ray 的**接力**关系，以及把一个"有竞态的骨架回调"改成幂等固化步骤 |
| **36** 分布式追踪与 OpenTelemetry | 第 11 章 §11.1 把「**追踪**」列为四层观测之一，**然后那一层再没出现过**。（⚠️ 原注"全书 `OpenTelemetry` 命中 0、`Jaeger` 命中 0"**是自我否证的** —— 写出这句话的那一行本身就算一处命中，读者一条 grep 就能推翻。第五轮已改成定性表述，并把它写进正文当反面教材。）补上：`opentelemetry.context` **跨不过 `ray.remote`** 这个核心难点、`propagate.inject()/extract()` 的载体传参、worker 侧要自己 setup provider、采样器必须 `ParentBased`、**把 Ray 的 ID 写进 span + 把 `trace_id` 打进日志**这条打通三件套的桥、以及一张"什么工具答什么问题"的分工表 |

### 本批修正的事实错误（按严重程度）

**六条「上一轮改了正文、漏了小结 / 邻章」—— 这批最高危的一类：**

| 位置 | 原来 | 现在 |
|---|---|---|
| **第 2 章 §2.11 小结** | 「默认 `max_retries=3` 会把**确定性错误也重跑 4 遍**」 | **与同章 §2.8、第 10 章直接冲突**（README 早先声称已修 —— 只修了 §2.8）。改为「默认**不**重试应用异常；`max_retries` 只管 worker 崩溃」 |
| **第 5 章 §5.10 / 第 10 章 §10.11 小结** | 生成器重试「**跳过已产出的值、从断点继续**」（2 处） | **与两章自己的正文相反**。改为「**整任务从头重放**，旧 attempt 的产出按 `attempt_number` 丢弃」 |
| **第 3 章 §3.4 / 第 8 章 §8.7** | 空闲 worker 回收「**默认 10 秒**」（2 处） | **1 秒**（`idle_worker_killing_time_threshold_ms=1000`）。§3.2 早就改对了，这两处没改 |
| **第 11 章 §11.4 / 第 4 章 §4.5** | 仍在用 `ray logs` 的**扁平写法**（§11.2 自己刚说过"不能用了"） | 统一为子命令组写法，并强调 **`worker` 用 `--pid`、其余用 `--id`** |
| **第 20 章 §20.10** | 「往后还有 **11** 章」并列表（漏了 32、33） | **16 章**，并补上第四轮新增的三章；同时说明**为什么 26 排在附录后面**（四轮修订依次追加，重排会打断所有跨章引用） |
| **第 31/32/33 章结尾** | **三章各自主张自己是"全书结尾"** | 统一：§31.11 改为「阶段回顾」、§32 的"下一章"指向 33–36、§33 明确说"后面还有 3 章" |

**会导致代码直接报错或得出完全错误结论的：**

| 位置 | 原来 | 现在 |
|---|---|---|
| **第 29 章 §29.3/§29.5** | `save_strategy="no"` + `RayTrainReportCallback()` 被称为"标准组合" | **反了**。该回调重写的是 **`on_save()`** —— HF 不存盘回调就不触发，结果是 **Ray 侧一个 checkpoint 都不产生**，`get_checkpoint()` 永远 `None`，**抢占恢复全部失效且不报错**。正确组合是保留 `save_strategy="steps"` 且与 `evaluation_strategy` 对齐 |
| **第 14 章 §14.7/§14.9/§14.13/§14.10** | `tune.TuneConfig(resources_per_trial={...})`（4 处） | **`TuneConfig` 没有这个字段**，照抄 `TypeError`。改用 `tune.with_resources(trainable, {...})`；并补上 **`max_concurrent_trials` 与 `ConcurrencyLimiter` 互斥**（同时给会抛异常） |
| **第 12 章 §12.2** | `ds.join(ds2, on=…, how="inner")` | 形参是 **`join_type=`**，取值含 **`full_outer`**（没有 `outer`）与 `right_semi`/`right_anti`，且 **`num_partitions` 必填** |
| **第 29 章 §29.4** | `model, optimizer = prepare_model(model, ...)` | `prepare_model` **只返回模型**；优化器要用 **`prepare_optimizer()`** —— 原写法必然 `ValueError: not enough values to unpack` |
| **第 29 章 §29.5** | `model.load_adapter(d, adapter_name="default")` | 三个坑叠加：目录层级要在 `checkpoint/` 里、**peft 的 `is_trainable` 默认 `False`**（续训会静默地什么都不训练）、`"default"` 是已占用的适配器名 |
| **第 13 章 §13.3/§13.10** | `ray.train.get_world_size()` / `get_local_rank()` 等列为**模块级函数**（2 处） | 它们是 **`TrainContext` 的方法**（`ray.train.get_context().get_world_rank()`）；`ray/train/__init__.py` 的 `__all__` 里没有这些。照抄 `AttributeError` |
| **第 31 章 §31.4** | `memray run -o out.bin python my_script.py`；`memray attach --pid <PID>` | `memray run` 直接吃脚本路径（多写的 `python` 会被当成脚本名）；**`attach` 的 PID 是位置参数**，没有 `--pid` |
| **第 30 章 §30.4** | `for name, size in torch.cuda.memory_snapshot()[0].items()` | 该返回值是「段的列表」，键是 `total_size`/`allocated_size`/`segment_type`/`blocks` 等，**没有 `name`/`size`** |
| **第 33 章 §33.3** | `ray memory --group-by stack-trace` / `--sort-by object-size` | 取值是 click 的 `Choice`、**大小写敏感**：`STACK_TRACE` / `OBJECT_SIZE` / `PID` / `REFERENCE_TYPE`。小写短横线写法**会被 CLI 直接拒掉** |
| **第 29 章 §29.3/§29.4** | `fsdp_config={"fsdp_transformer_layer_cls_to_wrap": …}`；`tokenizer=` | 键名**没有 `fsdp_` 前缀**（`transformer_layer_cls_to_wrap`，旧名计划在 HF v5 移除）；`tokenizer=` 的弃用自 **transformers 4.46** 起，不是"HF 5.x 的变化" |
| **第 29 章 §29.6** | 批量 `generate` 用 tokenizer 默认 padding | decoder-only 批量推理必须 **`padding_side="left"`** —— 右填充**不报错，只是输出全错** |

**会给出错误结论的：**

| 位置 | 原来 | 现在 |
|---|---|---|
| **第 33 章 §33.5 / 第 11 章 §11.4** | 「`ray logs` **在 2.58** 已改成子命令组」（被称为"本章最需要注意的变更"） | **版本归属错误**：子命令形态自 **Ray 2.3**（PR #30422，2022-12）就存在，2.5.1 的官方文档已是五个子命令。照旧会**误判读者手上旧版本的行为** |
| **第 33 章 §33.7** | 「`ray stack` 比 `py-spy` 更 Ray 原生、快得多」 | **`ray stack` 内部就是调用 `py-spy`**，官方要求 py-spy 已安装。所以：没装 py-spy 时这一步**直接失败**；第 31 章 §31.3 的 ptrace/capability 限制对**它同样成立** |
| **第 2/3/7 章** | 「引用计数归零后**立即**回收」（3 处）与第 2 章「不是瞬时」互相矛盾 | 真相是**批量上报**。⚠️ **本条在第五轮被推翻了一半**：第四轮把三处"统一"到了 `free_objects_period_ms` 默认 1000 ms / `free_objects_batch_size` **10000** —— **三处对上了，但对的是错的值**。2.58 源码是 `free_objects_period_**milliseconds**`（默认 1000）与 `free_objects_batch_size` 默认 **100**。配置名写错会**静默无效**。详见第五批清单第 1 条 |
| **第 2 章 §2.5** | 「**Ray 没有 DAG 声明 API**」 | 与同章 §2.2 大段讲的 `.bind()` DAG API、以及第 26 章整章矛盾。改为「**默认路径下**靠数据流推断；显式声明用 `.bind()`（为了让它能被**编译**）」 |
| **第 10 章 §10.9** | 「Ray **没有**官方故障注入 API」 | 没有的是**面向用户的公开** API；Ray 仓库内部有成套 RPC 混沌机制（`RAY_testing_rpc_failure`、`rpc_chaos.cc`，PR #58512）用于自己的容错测试 |
| **第 10 章 §10.10 / 第 6 章 §6.10** | mini-ray 的任务重试与 Ray「**一致**（默认 3 次）」 | ⚠️ **方向相反**：mini-ray **连应用异常也重试**，Ray **默认不重试**。已补进第 6 章 §6.13 的"诚实清单" |
| **第 6 章 §6.8** | `raylet.py` "**1994 行**" | 实测 **2024 行** |
| **第 6 章 §6.6** | 「真实 Ray **并不需要** `if __name__ == '__main__'`」 | 表述过于绝对：`ray.util.multiprocessing`（spawn）、**Windows** 等场景仍要求主模块保护 |
| **第 13 章 §13.3** | 「`DataConfig` 是 **V2 新增的**，V1 教程里根本没有它」 | 它早在 **Ray 2.6/2.7**（PR #35236）就有，比 Train V2 早约一年半。**V1 教程里 `dataset_config=DataConfig(...)` 是常见写法** |
| **第 13 章 §13.3** | `prepare_model` "按当前 worker 的 rank 换成对应的并行包装" | 包装方式由 **`parallel_strategy=` 参数**决定（默认 DDP），不是按 rank/world_size 自动切换 |
| **第 13 章 §13.3** | `trainer_resources` 的替代是 `resources_per_worker` | **没有替代参数** —— `resources_per_worker` 描述的是 worker 的资源。V2 的语义就是"控制面不占逻辑资源"，**正确做法是什么都不写** |
| **第 14 章 §14.2** | 「`session.report` 2.0 起弃用」 | **方向反了**：它是 **2.0 起推荐的新 API**，到 Train V2（2.51）才被取代。V1 教程里出现它是正常的 |
| **第 14 章 §14.5** | ASHA 图与文字写「按指标淘汰**一半**」 | 与同节自己写的「`reduction_factor` 默认 **4**（淘汰 3/4）」矛盾。默认是**淘汰 3/4** |
| **第 14 章 §14.16** | `time_budget_s` "在 Tuner/RunConfig 上是否接受**未确认**" | **找错方向了**：它是 **`tune.TuneConfig` 的正式字段**（不是 `RunConfig` 的），现在仍可用 |
| **第 19 章 §19.8 vs 第 16 章 §16.8** | AReaL 一处说"构建在 Ray 上"、一处标"未确证" | 统一为「**来源是 Anyscale 自家页面，本书未独立核实**」，并显式标注来源层级 |
| **第 20 章 §20.5 vs 第 12 章 §12.6** | actor-only 重构一处写 2.57、一处写 2.58 | 说明是**同一件事的不同阶段**（2.57 起陆续弃用；`ray_remote_args=` 在 2.58 周期弃用、计划 2.64 移除），不是两个版本号说错 |
| **第 20 章 §20.9** | 「Ray 的 API 很稳定（Core API 几年没大变）」 | 补上反例：`local_mode` 已移除、`worker_idle_timeout_ms` 已不存在、`ray_remote_args=` 在弃用中。**"主干稳定"≠"全都稳定"** |
| **第 11 章 §11.2** | `list_objects()` 字段写「引用计数」 | 字段是 **`object_size` / `reference_type`**（没有引用计数，见第 5 章 §5.5） |
| **第 11 章 §11.2** | `ray get task <id>`；`ray list logs` | 前者应为**复数** `ray get tasks <id>`；后者 **CLI 里不存在**（Python 侧才是 `list_logs()`） |
| **第 4 章 §4.5 / 第 11 章 §11.6** | timeline 没开 profiling 时「生成一个只有系统事件的文件」 | ⚠️ **本条是错的，第五轮已改回并加强**。第四轮改成了"更典型的是**直接报错**" —— 对着 2.58 的 `state.py` 看，**它只 `logger.warning`，不抛异常，而且照样把文件写出来（内容 `[]`）**。所以**第四轮之前说的是对的**。第五轮改回"warning + 空文件"，并补上被漏掉的第二个必需变量 `RAY_task_events_report_interval_ms=0`。详见第五批清单第 2 条 |
| **第 29 章 §29.2** | `from_huggingface` 同节点"真正的零拷贝" | 与 §29.7 的警告口径不一。补上代价：零拷贝省的是 CPU 与拷贝时间，**不省内存**，且 `ray.put` 的大对象**会被 pin 住** |
| **第 18 章 §18.3** | `object_spilling_threshold` 默认 `0.8` | **三个来源互相冲突**（官方文档 0.95 / 源码一档 0.9 / 另说 0.8）。**不再给单一数字**，改为指路自行核对 |
| **第 7 章 §7.7** | 内存监控的排序规则漏了最关键一条 | 补上官方原文：**先选内存占用最大的 worker**（`"select the worker with the largest memory footprint first"`） |
| **第 5 章 §5.3** | async generator 不支持背压参数（未确认） | **已实现**（PR #64383）；且 `_generator_backpressure_num_objects` **默认就是 `-1`（不背压）** —— 原来没给默认值。该节标题写"四个注意点"实列 5 条，一并修正 |

### 本批补齐的内容缺口

`ray serve` 在 CLI 章里被承诺却从没展开（补 `deploy/status/config/run/shutdown` 与
何时用哪个）；State CLI 缺 `ray list cluster-events` / `runtime-envs` / `ray summary objects`；
`ray memory` 的分组与排序取值；
**Ray 内部 RPC 默认明文**（TLS 那一层，以及"token 鉴权 + TLS + NetworkPolicy 三件套"）；
日志外送的三条可落地路径；
`DeploymentResponse` 不能塞进容器再传；
Serve 三个新 router 的切换入口；
RLlib 的 `learning_starts` 已弃用（新入口
`training(num_steps_sampled_before_learning_starts=)`）；
`rllib_contrib` 的真实安装名（每个算法一个包）；
`JoinType` 的完整取值；`read_parquet` 的 `filter=`/`partition_filter=`（谓词下推）；
`iter_batches` 的 `batch_format` 默认值；
`ray stack` 与 `py-spy` 的真实关系；
CLI 章缺的 `ray status --verbose` / `ray dashboard --address`；
以及 `__main__` 保护在什么场景下**仍然必需**。

### 附录层的修正（本批第二轮：附录 A–G 逐行对照 mini-ray 源码）

这一轮的附录审计**逐行核对了附录 A 的 ✅/⚠️/❌ 标记与 mini-ray 源码**，
并实跑了关键判定 —— 结果抓到几处"标记与代码不符"的硬错：

| 位置 | 原来 | 现在 |
|---|---|---|
| **附录 A §A.3** | `OwnerDiedError` 标 ✅ 已实现 | **❌ 未实现**。实跑 `hasattr(miniray, 'OwnerDiedError')` → `False`。危险在于**上一行 `ObjectLostError` 标的是 ✅**，读者会以为两条路径都教学过 |
| **附录 A §A.2 / 附录 B §B.3.1（2 处）/ mini-ray README** | `num_returns="streaming"` | **mini-ray 只认 `"dynamic"`**（真实 Ray 用 `"streaming"`，`"dynamic"` 是**已弃用的旧名**）。实跑 `@ray.remote(num_returns="streaming")` → `ValueError: invalid literal for int()`。**四处一起改**，并在 mini-ray README 的「语义差异」表里正式登记 |
| **附录 A §A.5** | `NodeAffinitySchedulingStrategy` 标 ✅，备注说 "`soft=True` 时退回普通调度" | **⚠️ 只实现了硬亲和** —— mini-ray 把 `soft` 编码进策略但调度器**无条件硬过滤**，候选为空直接 `ScheduleError`。`soft=True` 的回退**只有真实 Ray 有** |
| **附录 A §A.7** | `excludes` / `_set_ray_env_vars` 同列一行 | `_set_ray_env_vars` **在真实 Ray 里根本不存在**（不在 `RuntimeEnv.known_fields`）。已删除该行并注明 |
| **附录 A §A.6 / §A.9** | `placement_group_table()` 与 `serve.get_app_handle` 各出现两次 | 合并重复行 |
| **附录 B §B.9** | `Queue` 的 API 列了 `put_nowait` / `get_nowait` | **公开的 `Queue` 类上没有这两个方法**（只在内部 `_QueueActor` 上）。正确写法是 `put(item, block=False)` |
| **附录 D（2 处）** | `ray logs --tail 200 <worker_id>` 扁平写法 | 子命令组写法 + **`worker` 用 `--pid`**。这是**已修错误在附录 D 的残留** |
| **附录 D** | 「用 `ray.util.list_actors` 或 `ray.util.state.list_actors`」 | **`ray.util.list_actors` 同样不存在**（"或"字并列会让读者以为两者等价）。正确路径是 `ray.util.state.list_actors()` / `ray.util.list_named_actors()` |
| **附录 F** | `ray start --head --code-search-path=/path/to/code` | **该 flag 不存在**，照抄 `unrecognized arguments`。只有两条路：Python 侧 `JobConfig(code_search_path=[...])`、Java 侧 `-Dray.job.code-search-path=...` |
| **附录 F** | `java_jars` 「与 `java_jvm_options` 配套」 | `runtime_env` **只有 `java_jars`**；JVM 选项是 **Java 侧配置**（`ray.job.jvm-options`），不是 runtime_env 的键 |
| **附录 F** | `CrossLanguageException`（Python 侧） | Python 侧是 **`ray.exceptions.CrossLanguageError`**；`CrossLanguageException` 是 **Java 侧**类名。写 `except ray.exceptions.CrossLanguageException` 会 `AttributeError` |
| **附录 F** | `RAY_RUNTIME_ENV_DEFAULT_EXCLUDES` 是"你显式写的 excludes 取并集" | 它是 **`ray_constants.py` 里的常量名，不是环境变量**（用户可设的是 **`RAY_OVERRIDE_RUNTIME_ENV_DEFAULT_EXCLUDES`**，写错名字**静默无效**）；且它是**整体替换**不是并集 |
| **附录 F** | `conda` "与 `pip` 不能同时指定；Windows 上不支持" | 互斥是**三方**的（`pip`/`uv`/`conda` 两两互斥）；Windows 的官方措辞是 **beta/experimental** |
| **附录 G** | CI 工作流用 `pytest -n auto` 但只装 `pytest pytest-timeout` | **必须装 `pytest-xdist`** —— `-n` 来自它，ray 不依赖它。漏了 CI 在这一步**直接退出** |
| **附录 G** | 「`submit_job` 是否有 `submission_id` 本书未确认，不要依赖」 | **它是正式参数**（`job_id` 是弃用别名），语义是"服务器拒绝重复使用同一 id"。写成未确认会让读者**主动放弃这条最有用的机制** |
| **附录 G** | `ray.init()` 「等价写法 `ray.init(address="auto")`」 | **语义不同**：前者读 `RAY_ADDRESS` 连指定 head，后者是"在本机找已存在的实例"。示例里巧合一致，换节点就连错 |
| **附录 G** | `ray.init(port=..., dashboard_port=...)` | **`ray.init()` 没有 `port` 参数**；GCS 端口只能在 `ray start` 侧用 `--port` |
| **附录 G** | §G.3 只给手工 `srun` + `ray start --head &` | 补上 **2.49+ 的官方封装 `ray symmetric-run`**（含 `--min-nodes`，这正是"集群没成形就开跑"的根因） |
| **术语表 C.5** | 「系统级错误默认不占重试额度」 | **说反了** —— `max_retries` 管的**正是** worker 崩溃；默认不重试的是**应用异常**。已修正并补上 `max_retries` 独立条目 |
| **术语表 C.5 / C.3** | 生成器重试「从断点继续」；内存监控「一次杀一个」 | 与第 5 章 §5.3、第 7 章 §7.7 的正文**直接相反**（正文早已改对，术语表是漏网）。已统一 |
| **术语表 C.8 / C.9 / C.2** | 「四层是 Dashboard/State API/ray memory/Timeline」；Jobs API「完整定义见 C.9」；拓扑感知与 autoscaler 版本号 | 四层是**状态/日志/指标/追踪**（工具 ≠ 层次）；Jobs API 指向改为**附录 F §F.3**；两处版本号统一标 **未确认** |
| **附录 E（3 段代码）** | `load_model(...)` / `compute_gradients(...)` / `self._score(...)` | 这三个名字**在 `examples/11_end_to_end.py` 里都不存在** —— 附录开头声明"配套代码可以直接运行"，读者会以为拿错了文件。已**逐字对齐源码** |
| **第 27 章 §F.2.2（表格）** | `working_dir` 的真"零拷贝"口径、`container` 字段集合等 | 只做措辞收敛（这一节的 `working_dir` / 缓存 / `RuntimeEnvConfig` 默认值**一个都没错**） |

> **附录 F 与第 26 章是核对质量最高的两份**（第 26 章的 mini-ray 关键词探测、
> 行号引用、代码片段**全部属实**；附录 F 最容易写错的那些数字也全对）。
> 这条记在这里，是为了让下一轮审计**不必重查这两块**。

### 本批的方法论说明

第四批最值得记的一点：**修掉的高危错误里，有 6 条是"上一轮改了正文、漏了小结"**。

这不是偶然。前三轮的修正都是**针对性地改某一段**，
而"同一件事的第二种说法"（小结、邻章、README 的自述表、CLI 示例）
不在那次修改的视野里。于是**错误以"小结"的形式复活**。

它和第 10 章 §10.4 那个真实 bug **是同一种形状**：
**两份状态没对齐** —— 只不过这次，"两份状态"是"正文小节"与"本章小结 / 相邻章"。

**所以这一轮做了一件前几轮没做的事**：把"文档结构自检"从**声明**变成
**可执行的脚本**（`mini-ray/tools/check_docs.py`）。
脚本能查的是形式（章号连续、引用不悬空、表格列数一致、自述数字唯一），
**查不了内容** —— 但它把"哪些一致性是机械可验证的"这件事**固化了下来**，
剩下的才是需要人读的部分。

> **一句话**：文档的可靠性来自**交叉引用的一致性**，
> 而不是每一段单独写得有多好。这本书改到第四轮时写下这句话，
> **第五轮又给它加了一个更锋利的推论**：一致性还有一个**时间维度** ——
> **上一轮的修正本身也必须被下一轮验证**。本轮 5 条最高危的错误，
> 全都是第四轮"改对了方向、却改错了内容"或"只改了一半"留下的。
> 单轮之内的交叉引用一致，**不等于**跨轮次的一致。

---

## 结语

| 章 | 补的是哪个洞 |
|---|---|
| **26** Ray Compiled Graph 与 DAG API | 原来只有 §3.8 半页。补齐 `bind()`/`InputNode`/`MultiOutputNode`、`experimental_compile()`、通道数据面、多 GPU collective、与普通任务的取舍 |
| **27** 附录 F：Ray Client、runtime_env 与多语言 | `runtime_env` 原来只有一张八行的表；Ray Client 只出现在术语表；多语言只是架构图里一个方框 |
| **28** 附录 G：CI/CD、Slurm 与云上调度 | 全书原本没有"怎么测 Ray 应用""只有 Slurm 没有 K8s 怎么办""这套东西花多少钱" |
| **29** Ray × PyTorch / HuggingFace 生态集成 | 2026 年最主流的真实负载形态。原来 `HuggingFace` 只在 2 个文件里出现过，`ray.data.from_huggingface` 只在 1 个文件里提了一句；PEFT/LoRA 完全零命中 |
| **30** GPU 编程、集合通信与显存管理 | 第 8 章讲资源语义、第 13 章讲并行策略，但**没有一章回答**「`CUDA_VISIBLE_DEVICES` 什么时候设的」「NCCL 卡住看哪个变量」「OOM 有几种成因」 |
| **31** 性能剖析与调试工具链 | 第 11 章讲看集群、第 18 章讲旋钮，但**没有一章讲剖析器** —— `py-spy`/`memray`/`nsys` 在全书各出现 0–1 次 |

### 第二批新增（2026-09 第二轮审计）

**新增 3 章**：29（Ray × PyTorch/HuggingFace）、30（GPU 与集合通信）、
31（性能剖析工具链）。

**mini-ray 新增能力**（都是真实 Ray 的 API，之前标着 ❌）：

| 模块 | 内容 | 对应章节 |
|---|---|---|
| `miniray/util/queue.py` | `Queue(maxsize, actor_options)` —— async actor 承载，非轮询阻塞；`put/get` 的 `block=`/`timeout=`、`qsize/empty/full/shutdown`；`Empty`/`Full` 复用标准库 | 第 6 章、第 29 章 |
| `miniray/util/metrics.py` | `Counter` / `Gauge` / `Histogram`，含 `tag_keys` 校验、桶插值分位数、重复注册报错 | 第 11 章 §11.5 |

配套：`tests/test_queue_metrics.py`（30 个用例）、
`examples/12_queue_pipeline.py`。**测试总数 118 → 148，示例 11 → 12。**

### 第二批修正的事实错误

| 位置 | 原来 | 现在 |
|---|---|---|
| **第 13 章 §13.3** | `dataset_config=DataConfig(train_ds=…, valid_ds=…)`，并称 `datasets=` 是"V1 旧写法" | **这个 API 不存在，而且说法是反的**。改为 `datasets={"train": …, "valid": …}` + `DataConfig(datasets_to_split=…)`（它只管**切分策略**，不承载数据） |
| **第 12 章（通篇）** | `compute="actors"` + `concurrency=N` | **`concurrency=` 已于 2.51 弃用**（PR #57035）；字符串形式 `compute="tasks"/"actors"` 是 2.9 之前的拼法。统一改为 `compute=ActorPoolStrategy(size=…)` / `TaskPoolStrategy()` |
| **第 12 章 §12.2** | `cache()` 是 `materialize()` 的"流式变体" | `cache()` 是 `materialize()` **在 2.4 之前的旧名**（PR #34169），不是两种语义 |
| **第 12 章 §12.3** | `write_unity_catalog`；与 `write_kafka` 挤在一行 | ⚠️ **第四轮这里改错了**：`write_databricks_table` 在 2.58 里**也不存在**（第六轮回源码核对）；正确写法是 `write_delta(path, catalog=DatabricksUnityCatalog(...))`，第 12/19 章已改 |
| **第 12/19/20 章** | Hash Shuffle V2 分别写成 2.50 / 2.57 / 2.58 | 统一为「`#63598` 引入、**2.58 支持 `join`**」，并与 `DataSourceV2`（2.57 默认开启）**显式区分为两件事** |
| **第 13 章 §13.3/§13.6** | `ray.train.get_device()` | 正确路径是 **`ray.train.torch.get_device()`** |
| **第 13 章 §13.4** | `trainer_resources` "给控制面留资源" | **V2 已弃用**：传值抛 `DeprecationWarning`，driver **不再预留任何逻辑资源** |
| **第 13 章 §13.2** | `fail_fast` 与 `max_failures` "互斥" | V2 下 `fail_fast` **整体不可用**（传了就报错），不是互斥 |
| **第 14 章 §14.1** | "`RunConfig` 不在 `tune.` 命名空间，漏 import 就 `NameError`" | `tune.RunConfig` **是可用的**；真正要小心的是 `ray.tune.RunConfig` 与 `ray.train.RunConfig` **同名不同类** |
| **第 14 章 §14.9/§14.13** | `RAY_TUNE_MAX_CONCURRENT_TRIALS`；`Tuner.restore(..., with_parameters=)` | 前者**不存在**（该用 `TUNE_MAX_PENDING_TRIALS_PG`）；后者**没有这个形参**（要重新传 `trainable=tune.with_parameters(...)`） |
| **第 15 章 §15.5** | `target_ongoing_requests` 默认 1.0，"是否已改未确认" | **默认 2.0**（与 `max_ongoing_requests` 100→5 是 PR #45943 的同一个改动） |
| **第 15 章 §15.4** | `@serve.batch(max_batch_size=16)` 示例用默认 `max_ongoing_requests` | 2.32 起默认是 5，**批次永远攒不满 16**。示例补 `max_ongoing_requests=32` |
| **第 17 章 §17.5** | CISA KEV 日期"未核实"；"KEV 窗口通常是天级（约两周）" | 补上可核实的日期表（**2026-08-17 收录 / 2026-08-20 截止**），删掉错误的天数概括 |
| **第 18 章 §18.7** | mini-ray worker 池上限 `max(4, CPU×4)` | 源码是 **`max(16, CPU×8)`**（`worker_pool.py:204`） |
| **第 21 章 A.4/A.10** | 字符串调度策略标 ✅；`local_mode` 标 ✅ | 策略字符串**只被记录、不参与决策**，降为 ⚠️；`local_mode` 移入"已移除 API"清单 |
| **第 21 章 A.8** | `metrics` 三件套标 ❌ 未实现 | 已实现。同时说明 `.timer()` 是 mini-ray 的扩展、**真实 Ray 没有** |
| **第 25 章 E.7** | `lose_objects()` 返回 `{'lost': [...], 'reconstructed': [...]}` | `reconstructed` 是**整数计数不是列表**（源码为准）；"救不回来"的个数是 `len(lost) - reconstructed` |
| **第 23 章 C.5** | Model Multiplexing 指向 §15.7 | 应为 **§15.16**；术语表条目数与索引排序一并修正 |
| **第 04/21/23/24/28 章** | 仍在教 `local_mode=True` 调试 | **Ray 已移除**（`RuntimeError`），统一改为指向 Ray Distributed Debugger |
| **第 02/05 章** | `max_calls`"mini-ray 已实现"；`obj["size"]`；`__wrapped__` | mini-ray **没有** `max_calls`（未知选项会报错）；字段是 **`object_size`**；`RemoteFunction` **没有** `__wrapped__` |
| **第 05/07 章** | owner 死亡抛 `ObjectLostError` | 是 **`OwnerDiedError`**（前者可重建，后者不可，语义完全不同） |
| **第 07 章 §7.6** | `ray.init(object_spilling_config=…)` + `"type": "s3"` | 形参错（应为 `_system_config=`）、后端错（S3 走 **`smart_open`**，没有 `"s3"`）、`filesystem` 的参数名是 `directory_path` |
| **第 11 章 §11.5** | `Histogram.timer()` | **真实 Ray 没有 `.timer()`**，只有 `.observe()`；mini-ray 的 `.timer()` 是扩展 |
| **第 08 章 §8.5** | 放置组"**不会**被重新调度" | 会进入 **`RESCHEDULING`** 并尝试重分配，但**组内的 actor/任务不会被拉起来**，且重分配可能失败退化为 `REMOVED`。补全四个状态 |
| **第 03 章 §3.8** | 孤立引用块；vLLM 的 "RFE" | 并入正文；统一为 **RFC #35848** |

### 修正的事实错误（按严重程度）

| 位置 | 原来 | 现在 |
|---|---|---|
| **第 2 章 §2.8** | 称 `max_retries=3` 会重试 `ValueError` 等应用异常，"这是 Ray 的默认行为" | **与第 10 章直接冲突**。已改为官方语义：应用异常**默认不重试**，需 `retry_exceptions` |
| **第 5 章 §5.3** | 称生成器任务"不能重试" | 与第 10 章冲突。已改为 Ray **支持**重试、语义是"跳过已产出值、从断点继续" |
| **第 7 章 §7.5** | State API 字段写 `obj["size"]` | 正确字段是 **`object_size`**（写错会 `KeyError`）；并补上 `call_site` 需开记录开关 |
| **第 23 章 C.2** | mini-ray worker 池上限 `max(4, CPU×4)` | 源码实际是 **`max(16, CPU×8)`** |
| **第 18 章 反模式 8** | 参数服务器指向"第 10 章" | 应为**第 09 章 §9.7** |
| **第 25 章 E.7** | "丢掉 64 个对象""38 个对象没有血缘" | 数字无法复现且与 E.6 的 61 不符。已改为描述实际返回结构 `{lost, reconstructed}`，并注明**计数每次运行都不同** |
| **第 25 章 E.4** | `ScalingConfig(placement_group_strategy=...)` | 正确字段是 **`placement_strategy`** |
| **第 8 章 §8.2** | system reserved 写成通用默认 `max(1,5%)` | 那是 **cgroup 隔离开启时**的参数，且有上下限（`min(3.0, max(1.0, 5%))`） |
| **mini-ray 源码** `object_ref.py` | 注释称"**Ray 要求**生成器任务 `max_retries=0`" | 这是 mini-ray 的**刻意简化**，不是 Ray 的限制。注释已改正 |
| 第 1/3 章 | topology-aware 调度版本号 2.56 vs 2.57 各说各话 | 两处已互相注明，并标 **未确认** |
| 第 17 章 | BOD 26-04 "只有 3 天" | 未能从 CISA 原文核实，已标 **未确认**并改为强调"KEV 意味着已被在野利用" |
| 第 23/24 章 | CRD 是"三种" vs 第 17 章列了四种；KubeRay token 版本不一致 | 两处已对齐并说明差异来源（alpha 的 `RayCronJob`；`authOptions` 与 RBAC 集成是两件事） |

### 补齐的内容缺口

`ray.util.metrics`（自定义指标，全书原本完全没有）、`max_calls`（worker 回收）、
`max_pending_calls`（邮箱背压）、`OwnerDiedError`、`retry_exceptions` 判定函数、
`RAY_TASK_MAX_RETRIES=0` 会**连带关掉 lineage 重建**、内存监控/OOM killer 机制、
`/dev/shm` 容量推算、溢出盘满阈值、放置组**不会自动恢复**、
`accelerator_type` 与 MIG、autoscaler 的 `available_node_types` 配置与 `ray up/down`、
`ray://` 四种 address 写法、`fetch_local` 语义、State API 的
`list_logs`/`list_cluster_events`/`list_jobs`/`list_runtime_envs`；
Ray Data 的表算子/`ActorPoolStrategy`/`ExecutionOptions`/`iter_torch_batches` 旋钮；
Ray Train 的 `DataConfig`/`ScalingConfig` 字段/`Checkpoint` 进阶；
Ray Tune 的 `Stopper`/`Callback`/`with_parameters`/`ConcurrencyLimiter`/PBT 实战；
Ray Serve 的 `serve.start`/模型多路复用/`autoscaling_config` 全字段/Gradio；
RLlib 的**多智能体**/`LearnerGroup`/`ConnectorV2`/`EnvRunner`/`RLModule`/离线 RL。

以及 3 条新 FAQ（升级版本后跑不起来、Ray 资源与机器对不上、autoscaler 不扩容）。

> **这份清单本身就是教程第 20 章的实践**：先分清哪些是**事实**（版本号、字段名、
> 默认值），哪些是**判断**（该不该用、往哪走）。上面每一条事实错误，
> 都是"看起来对、其实错"的那一类 —— 它们不会让代码报错，只会让结论错。

---

## 第三批修订清单（2026-09 第三轮审计）

### 新增章节

| 章 | 补的是哪个洞 |
|---|---|
| **32** 实验追踪与 MLOps 集成 | 全书对 `mlflow` / `wandb` / `W&B` 的**命中数是 0** —— 而"训练跑出来的数字和产物最后去哪了"是任何真实项目都绕不开的问题。补上 `RunConfig(callbacks=)` 这一个统一入口、`ray.air.integrations.*` 的官方集成、Train 侧的 **rank-0 语义**、`Checkpoint` 之后的事（**Ray 不做 model registry**），以及 8 条坑 |
| **33** Ray CLI 全集与交互式开发 | CLI 命令散落在 10 多个章节里，**没有一处聚齐**。补上四套体系的划分、State CLI 的字段名陷阱、以及一条**按现象查命令的定位流程图**（§33.9）。同时讲清 Notebook 工作流与"改完 remote 函数要重启 kernel"这类实战细节 |

### 修正的事实错误（本批，按严重程度）

**会导致代码直接报错的三条：**

| 位置 | 原来 | 现在 |
|---|---|---|
| **第 2 章 §2.2** | `add_10 = add.bind(10)` 后接 `ray.get(add_10.remote(5))` | **真实 Ray 里 `.bind()` 返回 DAG 节点（`FunctionNode`），只能 `.execute()`** —— 写 `.remote()` 会抛 `AttributeError: .remote() cannot be used on <class 'FunctionNode'>`。mini-ray 的 `.bind()` 返回 `RemoteFunction`，所以那边 `.remote()` 有效 —— **这是两者语义分叉的地方** |
| **第 30 章 §30.3** | `timeout=torch.timedelta(minutes=30)` | **`torch.timedelta` 这个类不存在**。`init_process_group` 要求 `datetime.timedelta` —— 照抄必然 `AttributeError` |
| **第 2 章 §2.2** | `max_calls` 默认值写成「`0`（不限）」 | 源码是「**CPU 任务不限 / GPU 任务 = 1**」（`num_gpus > 0 and max_calls is None → max_calls = 1`）。**这才是"GPU 任务每次都重新加载模型"的根因** —— 想让 GPU 任务复用 worker 必须显式 `max_calls=0` |

**会给出错误结论的五条：**

| 位置 | 原来 | 现在 |
|---|---|---|
| **第 5/9/10/21/22 章** | `num_returns="dynamic"`（共 8 处） | **`"dynamic"` 已弃用，正确值是 `"streaming"`**；而且**生成器任务/方法默认就是 streaming，"必须显式声明"是错的**（源码：`if num_returns is None: num_returns = "streaming" if self._is_generator else 1`）。**真正要求显式声明的是 mini-ray，不是 Ray** |
| **第 5 章 §5.3 / 第 10 章 §10.2** | 生成器重试是「**跳过已产出的值、从断点继续**」 | **是「整任务从头重放」**：旧 attempt 的产出靠 `attempt_number` 被丢弃。官方明确写 *"this assumes that the generator task is idempotent and deterministic"* —— **不是断点续传** |
| **第 7 章 §7.7 / 第 10 章 §10.3** | 内存监控「**按占用从大到小**杀**一个** worker」「一个一个杀」 | **2.56 起默认是 time-based killing policy**：① **可重试的 worker 优先被杀** → ② 运行更短/更新的做 tie-break、优先 idle → ③ task 优先于 actor → ④ **必要时一次杀多个**。实践含义反直觉：**声明了重试的任务反而先被牺牲** |
| **第 3/6/18/23 章** | `worker_idle_timeout_ms`（默认 10000 ms）当作**真实 Ray** 的配置（共 5 处） | **该常量在 Ray 2.58 已不存在**（Ray 1.x 遗留）。现在是 `kill_idle_workers_interval_ms`(200ms) + `idle_worker_killing_time_threshold_ms`（默认 **1 秒**，**不是 10 秒**）+ 1 GiB 内存门槛。⚠️ mini-ray **仍然保留**这个旧名字与旧默认值 —— 两边数字**不要互相对照** |
| **第 1/4/21/24 章** | Python 3.9 支持「在 2.52.0 终止」 | **自 2.52 弃用、2.54.0 起正式不再支持**（PyPI 元数据：2.53.0 仍带 `Python :: 3.9` 分类器） |

**其余修正：**

| 位置 | 原来 | 现在 |
|---|---|---|
| **第 2 章 §2.10** | `ray.list_actors` / `ray.util.list_actors` | **两个都不存在**。正确路径是 `ray.util.state.list_actors()`（列全部）/ `ray.util.list_named_actors()`（列命名） |
| **第 2 章 §2.5** | `max_pending_calls` 超限抛「`RayActorError`」 | 抛 **`ray.exceptions.PendingCallsLimitExceeded`**，且计数**按 handle** 计（默认 `-1` 无上限） |
| **第 2 章 §2.9** | `address="local"` = 「强制单进程、不起 worker（等价已移除的 `local_mode`）」 | 语义是「**忽略已有集群、强制新起一个本地多进程集群**」—— 既不是单进程，也与 `local_mode` 无关 |
| **第 11 章 §11.4** | `ray logs --actor-id=...` 扁平写法 | **2.58 已改成子命令组**：`ray logs actor\|task\|worker\|job\|cluster`，用法如 `ray logs actor --id ABC --follow`。并保留"只能看活着的节点"这条限制 |
| **第 11 章 §11.4** | 「2.58 起把 task events 移出 GCS 热路径」 | **是可选开关且默认关闭**（`RAY_enable_task_events_to_dashboard_head` 默认 `false`）。升级后若发现 task events 变少，**先查这个开关** |
| **第 11 章 §11.5/§11.10** | 「mini-ray **没有**实现 metrics 这一层」 | **已实现**（`miniray/util/metrics.py`，第二批判 README 里就写过了，正文漏改）。同时把 `list_objects()` 的字段说明改正：**mini-ray 没有 `size` 字段**（真实 Ray 反而有 `object_size`）—— 方向与直觉相反 |
| **第 3 章 §3.8** | 「DAG API 自 2.32」 | `.bind()` 的 DAG API **自 Ray 2.0** 就有；`experimental_compile()` 自 2.32 前后引入、**2.44 进 beta** |
| **第 15 章 §15.21** | 小结里 `target_ongoing_requests` 默认「1.0」 | **2.32 起默认 2.0**（与 `max_ongoing_requests` 100→5 是**同一个 PR #45943**）—— 小结与 §15.5 正文自相矛盾，已统一 |
| **第 13 章 §13.3** | `datasets_to_split="all" \| "none" \| [...]` | **`"none"` 不是合法值**（签名是 `Literal['all'] \| List[str]`）；关闭切分传 **`[]`** |
| **第 13 章 §13.2** | V1/V2 对比表有一行**只有 2 个单元格**（表头 3 列） | 补齐为 3 列 |
| **第 14 章 §14.14/§14.15** | `from ray.train import CheckpointConfig` / `FailureConfig` 后塞进 `tune.RunConfig` | **三件套（`RunConfig`/`CheckpointConfig`/`FailureConfig`）在 `ray.tune` 与 `ray.train` 下各有一套**，给 `Tuner` 传 `ray.train.*` 会触发弃用警告。§14.1 的警告已从"一个类"扩到"三件套" |
| **第 14 章 §14.16** | `Tuner.restore()` 的 `param_space`「**技术上可以**借恢复改搜索空间」 | **不能**。该形参**只用于重建失效的 object ref**，键集不一致会抛 `ValueError`（官方：*"Changing the hyperparameter search space then resuming is NOT supported"*）—— 且与 §14.19 自相矛盾 |
| **第 12 章 §12.6** | 「join/shuffle 密集负载官方建议把对象存储比例提到 **0.5**」 | **该建议已被官方撤销**（`#63389` 删推荐文案、`#62387` 删告警）。理由正是"调大挤压 UDF 内存、降并发、反而更易 OOM" |
| **第 12 章 §12.2** | `zip` 作为普通表算子 | **已弃用**（`#65111`）：依赖全局有序，块乱序时会**静默错配行**，改用基于 key 的 `join` |
| **第 12 章 §12.6** | 推荐 `ray_remote_args=` 透传调度参数 | **`ray_remote_args=` / `ray_remote_args_fn=` 已在 2.58 周期弃用**（`#65228`/`#64963`，actor-only 重构），计划 **2.64 移除**。改用具名选项 `resources=` / `accelerator_type=` / `label_selector=` / `runtime_env=` / `max_calls=` |
| **第 12 章 §12.3** | `DataSourceV2`「重写文件读取的 scan/listing 路径」 | **作用范围比名字窄**：`use_datasource_v2` **只在 `read_parquet()` 一处被读取**（`#65155`），其余 reader 仍走 V1 |
| **第 12 章 §12.2** | lazy/eager 表里 `materialize` 与 `cache` 并列 | `cache()` 是 `materialize()` **2.4 之前的旧名**（`#34169`），不是两种语义 |
| **第 12 章 §12.4** | `vLLMEngineProcessorConfig(concurrency=4)` 与 §12.2 弃用的 `map_batches(concurrency=)` 混在一起 | 前者是 **processor config 自己的字段**，**没被 #57035 波及** —— 同名不同物，已显式区分 |
| **第 12 章 §12.3** | 交叉引用「见第 19 章 §19.4」（该节是 vs torchrun） | 改为 **§19.2（vs Spark）**，那才是 Unity Catalog 相关内容所在 |
| **第 16 章 §16.2** | 「类替换关系」表漏了回调基类改名 | 补 `DefaultCallbacks` → **`RLlibCallback`** |
| **第 17 章 §17.2** | 「v1.6 与 v1.7 **一起发布**」 | **不成立**：v1.6.0 发布于 **2026-03-19**，比 v1.7.0（2026-08-20）早五个月。原文混淆了"官方把两个版本放在一篇文章里介绍" |
| **第 17 章 §17.5** | CVSS「9.4」 | **v4.0 = 9.4**（v3.1 = 8.8）—— 不带版本号会与 NVD 页面对不上，引用时必须带上 |
| **第 21 章 A.1/A.3/A.7** | 三张表表头 3 列、个别行 4 列 | 补 `备注` 列，全部对齐 |
| **第 21 章 A.9** | Serve 表只有 `serve.ingress` 一行 | 补 `@serve.batch` / `@serve.multiplexed` / `serve.start` / `get_*_handle` 四行（第 15 章正文曾让读者"自己去记"） |
| **第 21 章 A.10** | 异常表缺 `OwnerDiedError` | 补上，并注明它与 `ObjectLostError` 语义相反（可重建 vs 不可重建） |
| **第 21 章 A.11** | 环境变量表缺可观测性一组 | 补 `RAY_PROFILING` / `RAY_enable_task_events_to_dashboard_head` / `RAY_memory_monitor_refresh_ms` / `RAY_DEFAULT_OBJECT_STORE_MEMORY_PROPORTION` |
| **第 22 章 B.2** | 「跑示例（**11** 个）」 | **12 个**（第二批判加了 `12_queue_pipeline.py` 后漏改） |
| **第 22 章 B.4.1** | ID 布局未说明是谁的 | 补「**这是 mini-ray 的布局**；真实 Ray 类型码在**首字节**、长度也不同」 |
| **第 22 章 B.9** | `queue.py`「源码只有 200 行出头」 | **270 行**（含 docstring） |
| **第 23 章 C.2** | 「mini-ray 约 9000 行、**116** 个测试」 | **约 1.1 万行、148 个测试**（全书唯一的漂移点） |
| **第 23 章 C.2** | `DataConfig(train_ds=..., valid_ds=...)`「**取代**了 `datasets=`」 | **该 API 不存在，且说法是反的**（与第 13 章直接冲突）。数据走 `datasets=`，`DataConfig` 只管切分 |
| **第 23 章 C.2** | Hash Shuffle V2「2.50 起为默认」 | 与第 12/19/20 章统一为「hash shuffle 自 2.50 默认；**V2 由 `#63598` 引入、2.58 支持 `join`**」，并显式区分于 `DataSourceV2` |
| **第 23 章 C.2** | 放置组「**不会**自动恢复，节点一挂即 `REMOVED`」 | **与第 8 章 §8.5 相反**：会进入 **`RESCHEDULING`** 并尝试重分配，失败才 `REMOVED`；但**组内 actor/任务不会被拉起来** |
| **第 23 章 C.2** | `worker_idle_timeout_ms`（见第 3 章条） | 同上 |
| **第 23 章 C.8/C.9** | **Jobs API 重复定义**两次 | 合并，C.8 那条改为指向 C.9 |
| **第 23 章 C.11** | 索引漏收 `Health Check` / `Object Reconstruction` | 补上（按字母序插入） |
| **第 24 章 目录** | FAQ 编号在文档里**倒挂**（Q1–Q4 → **Q35** → Q5–Q7 → **Q36** → …），且 **Q34 不存在** | 三条补充 FAQ 重编号为 **Q34/Q35/Q36**（1–36 连续），**新增一张 36 条的问题索引表**说明编号为何不按文档顺序 |
| **第 24 章 D.2** | autoscaler v2「2.54 默认开启」直陈 | 补上「⚠️ 见第 17 章 §17.3 的**未确认**说明 —— 本书对这一条的出处标注不一致」 |
| **第 24 章 D.2** | 只列了 `max_ongoing_requests` 100→5 | 补 **同一个 PR #45943 还把 `target_ongoing_requests` 从 1.0 改成 2.0** |
| **第 25 章 E.2** | `map_batches(..., concurrency=4)` | 改用 `compute=ActorPoolStrategy(size=4)`（2.51 已弃用），并补上 `ActorPoolStrategy` 的 import |
| **第 25 章 E.7** | `lose_objects()` 返回 `{'lost': [...], 'reconstructed': [...]}`「两个列表的差集」 | **`reconstructed` 是整数计数不是列表**（源码 docstring 为准）；救不回来的个数 = `len(lost["lost"]) - lost["reconstructed"]` |
| **第 29 章 §29.6** | 同一个 `map_batches` 上同时写 `compute=ActorPoolStrategy(...)` 和 `concurrency=2` | 删掉被弃用的 `concurrency=`，改用 **`max_concurrency=2`** —— 原写法把"池大小"误当成了"单 actor 内并发" |
| **第 30 章 §30.6** | `memory_snapshot()[0].items()` 的用法 | 该返回值的键里**没有** `name`/`size`，原写法会误导 |
| **第 27 章** | 两处引用「第 24 章 **D.Q7**」 | 该格式在附录 D 里不存在，改为 **「D.2 的 Q7」** |
| **第 20 章 §20.10** | 结尾说「接下来是附录：……」（**漏了 26、29–31**） | 补一张表说明**后面还有 11 章**，并解释为什么 26 与 29–31 排在后面 |
| **第 0 章 §0.7** | 「示例 **11** 个」「**29 篇**正文章号连续（00–28）」 | **12 个** / **34 篇（00–33）** —— ⚠️ **这是第三轮当时的数字**；第四轮又加了 3 章，现在是 **37 篇（00–36）**。历史清单保留原值以便对照 |
| **第 1/5/6 章 & mini-ray README** | 「11066 行」 | 统一为「约 1.1 万行」（免疫后续漂移；实测 11071） |

### 本批的方法论说明

这批修正里，**有 6 条是"书内自相矛盾"**（同一件事两章说法相反），
而且**都是靠交叉审计发现的**，不是靠读单章：
`target_ongoing_requests` 的 1.0/2.0、放置组的 `REMOVED`/`RESCHEDULING`、
`param_space` 能不能改、mini-ray 有没有 metrics、示例 11 还是 12 个、
测试 116 还是 148 个。

**这恰好是分布式系统 bug 的同一种形状**（第 10 章 §10.4 那个真实 bug 也是它）：
**两份状态没对齐**。只不过在这里，"两份状态"是两章文字。

结论很朴素但值得记：**文档的可靠性来自交叉引用的一致性，
而不是每一段单独写得有多好。** 这也解释了为什么这次要专门补一张
第 24 章的问题索引表、并把第 33 章做成一张按现象查命令的流程图 ——
**它们都是"把散落的一致性显式化"的手段。**

---

## 结语

Ray 的 API 很稳定，但它的**生态位置**在变。学 API 是一次性投入；
理解"它为什么在那里、边界在哪、什么时候不该用它"才是长期能力。

这份教程的每一章都在朝这个方向写。
如果只带走一句话：**把 Ray 当成编排层，不要当成万能引擎。**
