仓库地址：https://github.com/hhk-png/cycle-agent

# 第 38 章：Ray 与 Agent 工作负载

> 这本书到第 37 章为止，讲的都是**怎么把一个模型算得快**。
>
> 但 2025 到 2026 年真正长出来的负载，不太一样：
> **它一次请求要跑几十秒、要调十几次工具、要记住上一轮说了什么、
> 而且它的瓶颈常常不是 GPU，是"在等一个外部 API"。**
>
> 这一类负载叫 Agent。它把前 37 章的东西**重新组合**了一遍 ——
> 第 9 章的 actor（有状态）、第 10 章的容错（长时程任务一定会中途失败）、
> 第 15 章的 Serve（当成运行时）、第 12 章的数据管道（轨迹就是数据）、
> 第 36 章的追踪（一次请求跨十个服务）、
> 还有第 16 章的 RL（Agent 是要被训练的，不是只被推理的）。
>
> 所以这一章**不是"又一个 AI 库"**，而是**一条把前面串起来的负载主线**。
>
> 一句话概括立场：
> **Agent 的"智能"来自模型和框架；Agent 的"能不能跑起来、跑得起、跑不崩"
> 来自运行时 —— 而运行时正是 Ray 最擅长、也最少被讲清楚的那一段。**

---

## 38.1 先给 Agent 负载做一次画像：五个特征

不先把负载特征说清楚，后面所有的"该用什么机制"都是猜。
把 Agent 和本书前 37 章讲过的两类负载放在一起比，差异立刻显形：

| 维度 | 离线批处理（第 12 章） | 在线推理（第 15 章） | **Agent（这一章）** |
|---|---|---|---|
| 单次请求时长 | 秒级到分钟级 | **百毫秒级** | **十秒到十分钟级** |
| 状态 | 无状态 | 无状态（KV 在引擎里） | **有状态（对话历史、工作记忆）** |
| 计算在哪 | 集群内 | 集群内 | **集群内 + 集群外（工具 API）** |
| 故障影响 | 重跑 | 重试一次请求 | **丢掉的是一整段上下文，代价高** |
| 成本结构 | 摊薄在吞吐上 | GPU 时间 | **GPU 时间 + 外部 API 调用费 + 重试** |
| 瓶颈 | GPU | GPU | **常常是等待（工具调用、人类审批）** |

五个特征，逐个说清楚它们各自会咬到哪里：

**① 长时程（long-horizon）。** 一次 Agent 请求内部可能有 5～50 次模型调用。
这意味着**单次请求的失败概率被放大了**：哪怕每一步 99.5% 成功，
50 步之后整体成功率只有 78%。第 10 章讲的所有容错机制，
在这里第一次成为**主线**而不是备选项。

**② 有状态。** 对话历史必须跟着会话走。这不是"缓存"，
是**正确性的一部分** —— 把用户第二轮的消息路由到另一个没有历史的副本上，
模型会答非所问，而且**不报错**。

**③ 大部分时间在等。** Agent 的时间轴上，GPU 只占一小段，
真正长的是工具调用、检索、人类审批。这一条直接决定了部署形态：
**你绝不能让一个昂贵的 GPU 副本阻塞地等一个 HTTP API**
（第 15 章 §15.4 的 `@serve.batch` 与异步副本，在这里从"优化"变成"必需"）。

**④ 突发且不可预测。** 一个用户的一次点击可能瞬间扇出 20 个并行工具调用。
按平均负载配容量，峰值必挂；按峰值配，平时全是闲置。

**⑤ 可训练。** 这是和普通服务最不一样的一点：
Agent 的轨迹（trajectory）**既是日志，也是训练数据**。
这让第 12 章（数据管道）和第 16 章（RL）在这条线上重新汇合 —— 见 §38.9、§38.10。

> **一个诚实的观察（可自行复核）**：截至 Ray 2.58.0，
> 官方的 `doc/source/ray-overview/use-cases.rst` 里 **grep `agent` 命中 0 次** ——
> Ray 还没有把 "Agent" 单列成一个 use case 页，
> 相关内容主要落在 **Ray Serve LLM 的文档**与 **Anyscale 侧的示例**里
> （复核命令：`curl -s https://raw.githubusercontent.com/ray-project/ray/ray-2.58.0/doc/source/ray-overview/use-cases.rst | grep -i agent`）。
> 这说明：**这条线上 Ray 提供的是"零件"，不是"整机"** ——
> 整机（LangGraph、CrewAI、AutoGen 那一层）要你自己选。
> 这一章的立场就是：**零件怎么选、怎么接、接错在哪一层会痛。**

---

## 38.2 三层分工：把"哪一层负责"再问一遍

第 30 章 §30.9 立过一条规矩，全书都在用：
**遇到问题先问"这该由哪一层负责"。** Agent 这条线上，层是三层：

```
┌──────────────────────────────────────────────────────────┐
│  编排层（框架）  LangGraph / CrewAI / AutoGen / 自研循环   │
│  负责：Agent 的"思考流程"——什么时候调工具、怎么规划        │
│  它的形态：一坨 Python 代码，通常跑在某个 serverless 函数里  │
└───────────────────────────┬──────────────────────────────┘
                            │  调用（HTTP / SDK）
┌───────────────────────────▼──────────────────────────────┐
│  运行时层  ← ★ Ray Serve 在这里                            │
│  负责：谁在哪个副本、会话粘到哪、扩缩容、故障隔离、排队       │
│  它不关心你的 Agent 有几个节点、叫什么名字                  │
└───────────────────────────┬──────────────────────────────┘
                            │  调用（OpenAI 兼容 / 引擎 SDK）
┌───────────────────────────▼──────────────────────────────┐
│  引擎层  vLLM / SGLang / TensorRT-LLM （第 37 章）         │
│  负责：这一批 token 怎么算得快                            │
└──────────────────────────────────────────────────────────┘
```

**这张图最重要的一行是中间的箭头：**
**编排层到运行时层走的是网络调用，不是函数调用。**
这意味着编排层**可以被独立扩缩、独立重启、独立换框架** ——
而这正是把 Agent 从"一个脚本"变成"一个服务"的关键一步。

| 你遇到的问题 | 该动哪一层 | 看哪一章 |
|---|---|---|
| Agent 逻辑写错、工具选错 | 编排层 | 本章只讲边界，不教框架 |
| Agent 卡住不动了 | **运行时层**（副本不够 / 被阻塞等待） | 本章 §38.6、§38.7 |
| 同一会话的请求打到了不同副本 | **运行时层**（会话亲和性） | 本章 §38.3、§38.4 |
| 工具调用把服务搞崩了 | **运行时层**（隔离） | 本章 §38.6 |
| 首 token 慢、吐字慢 | **引擎层** | 第 37 章 §37.2 |
| GPU 利用率低 | 先看是不是**运行时层**没喂饱 | 第 37 章 §37.1 |

> ⚠️ **一个在 2026 年特别常见的错层**：看到"Agent 并发上不去"，
> 去调 `autoscaling_config`（运行时层），
> 但真实原因是**编排层是同步阻塞的** —— 每个副本一次只能处理一个会话，
> 而它在等一个 3 秒的工具 API。这种情况下扩到 100 个副本也只是
> **多买 100 份等待**。判据和 §37.1 那条一样：
> **先看资源利用率。** 副本的 CPU/GPU 都很低而排队很长 → 是阻塞，不是容量。

---

## 38.3 会话亲和性：一个必须讲准的边界

"把同一个会话的请求路由到同一个副本" 这件事，在 Agent 场景里
从"优化"升级成了"正确性"（§38.1 特征②）。

### 38.3.1 Ray 2.58.0 到底给到哪一步

这一节必须写得非常准确，因为**它是两个方向都会写错的地方**：
既容易把"PR 里有的"当成"现在有的"，也容易把"默认没接上的"当成"根本不存在"。
本书前几轮在这类错误上都栽过（见 README 里"上一轮修出来的错"那一类）。

先把**已经落地的**列出来（每一条都在 Ray 2.58.0 的源码里逐字核对过）：

| 能力 | 出处（Ray 2.58.0 源码） | 状态 |
|---|---|---|
| `session_id` 能从 **Python handle** 传进去 | `handle.py` 的 `DeploymentHandle.options(session_id=...)`，字段落在 `_private/handle_options.py` 的 `DynamicHandleOptionsBase.session_id`（`DynamicHandleOptions` 继承它） | ✅ 有 |
| `session_id` 能从 **HTTP header** 传进去 | `_private/constants.py`：`SERVE_SESSION_ID = get_env_str("RAY_SERVE_SESSION_ID_HEADER_KEY", "x-session-id")` | ✅ 有 |
| `session_id` 能从 **gRPC** 传进去 | 走 invocation metadata（`replica.py` 里 `parse_session_id_header` 把 metadata 解成 `RequestMetadata.session_id`） | ✅ 有 |
| `session_id` 能一路传到副本 | `_private/common.py` 的 `RequestMetadata.session_id` | ✅ 有 |
| **路由策略能自定义** | `ray/serve/config.py` 的 `RequestRouterConfig`，字段 `request_router_class` / `request_router_kwargs` | ✅ 有 |
| **会话粘性路由器本身** | `ray/serve/experimental/consistent_hash_router.py` 的 `ConsistentHashRouter`（对 `session_id` 做一致性哈希，带 vnode 与 fallback） | ✅ 有（**experimental**） |

⚠️ **这一节最容易犯的错，是只查了一个目录就下结论。**
`ConsistentHashRouter` **不在** `_private/request_router/` 里 ——
但它在 **`ray/serve/experimental/`** 里，真实存在
（第 15 章 §15.7 记为 **2.56 引入**，PR #62905 / #63096 / #62906 ——
本节的复核只到"2.58.0 里在"，**引入版本这一条转引自第 15 章**）：

```bash
curl -s "https://raw.githubusercontent.com/ray-project/ray/ray-2.58.0/python/ray/serve/experimental/consistent_hash_router.py" | grep -n "^class "
# 39:class ConsistentHashRouter(RequestRouter):
```

**所以真正要讲准的边界不是"有没有"，而是"默认接不接得上"：**

* **默认的 power-of-two-choices 路由器完全不看 `session_id`** ——
  你把它传进去，默认路由**不会因为它而改变任何决策**。
  以为"传了 `session_id` 就有粘性"，是这一章最容易踩的坑；
* 要粘性，必须**显式把 `ConsistentHashRouter` 挂到 deployment 的
  `request_router_config` 上**（写法见第 15 章 §15.7）。它是 experimental，
  `experimental/__init__.py` 还是**空的**，所以
  `from ray.serve.experimental import ConsistentHashRouter` 会 `ImportError`，
  正确路径是字符串
  `"ray.serve.experimental.consistent_hash_router.ConsistentHashRouter"`。

一句话：**管道铺好了，消费者也写好了，但默认不给你接上。**
"默认没有"和"没有"，在这里是两件事。

### 38.3.2 那现在怎么办：四条真能用的路

| 方案 | 怎么做 | 优点 | 代价 |
|---|---|---|---|
| **① 不用粘性，状态外置** | 会话状态放 Redis / 数据库；任何副本都能服务任何会话 | 最简单、最稳、扩缩容无痛 | 每次请求多一次外部 IO；状态一致性要自己保证 |
| **② 用现成的会话粘性路由** | `RequestRouterConfig(request_router_class="ray.serve.experimental.consistent_hash_router.ConsistentHashRouter", request_router_kwargs={"num_virtual_nodes": 100, "num_fallback_replicas": 2})` | 官方实现、按 `session_id` 真粘；副本增删只影响环上相邻的 key | **experimental**，接口可能变；粘性本身和扩缩容天然打架（§38.3.3） |
| **③ 用现成的 LLM 路由** | `RequestRouterConfig(request_router_class="ray.serve.llm.request_router.KVAwareRouter", ...)` | 官方实现、和 KV cache 复用挂钩 | 偏向"复用 KV cache"，**不是**通用的会话状态粘性 |
| **④ 自己写路由** | 实现一个 router 类，按 `RequestMetadata.session_id` 哈希选副本，从 `RequestRouterConfig` 挂上去 | 完全可控 | 副本增删时环的重建、失败回退都要自己处理（方案 ② 已经把这些做完了） |

⚠️ **关于方案 ③ 的一个细节**：`ray/serve/llm/request_router.py` 里
真实存在的类是 **`PrefixCacheAffinityRouter`** 和 **`KVAwareRouter`**
（文件中 `PrefixCacheAffinityRouter(_PrefixCacheAffinityRouter)` 是再导出关系）。
它们的目标是**让同一个前缀尽量落到同一个副本上以复用 KV cache** ——
这**在多数 Agent 场景下恰好等价于会话粘性**，但**语义上是两件事**：
* KV 亲和关心的是"前缀一样"，会话粘性关心的是"会话一样"；
* 前缀一样但会话不同时，KV 亲和会合并，会话粘性不会。

把这两件事混为一谈，会在"多个用户共用同一个 system prompt"时
得到意料之外的路由分布。**这是一个真实的语义差异，不是措辞问题。**

### 38.3.3 一条更朴素的建议

如果你的 Agent 还没到需要抠 KV cache 复用的规模，**认真考虑方案 ①**（会话粘性同理）。
理由：粘性会**把你的扩缩容能力锁死** ——
副本减少时，粘在它上面的会话必须迁移，而迁移状态在多数框架里没有现成实现
（`ConsistentHashRouter` 解决的是"同一个 key 稳定落到同一个副本"，
**不解决"副本要下线了、状态怎么搬走"**）。
第 15 章 §15.6 讲的自动扩缩容，和会话粘性是**一对天然矛盾**。

> **可以核实的部分**（这一半在源码里）：`ConsistentHashRouter` 的 docstring
> 自己写明它是 "affinity-first"、选中的副本是"会话 ID 与环状态的**纯函数**"，
> 并且明确说**不能**把队列深度 / 局部性 / multiplex 信号混进来，
> 因为那会破坏确定性。也就是说：一致性哈希路由**只看 key，不看负载**。
> 复核命令：
> ```bash
> curl -s https://raw.githubusercontent.com/ray-project/ray/ray-2.58.0/python/ray/serve/experimental/consistent_hash_router.py | sed -n '39,62p'
> ```
>
> **属于观点、不是源码事实的部分**：由此推出"**均衡的是会话数、不是实际负载**，
> 一个短会话和一个长跑会话被平等对待，多个 straggler 会撞到同一个副本上"，
> 以及"这就是 Anyscale 建议多数场景优先用 KVAwareRouter 的理由"。
> 这一层是从上面的机制推出来的**判断**（Anyscale 公开博客里持同样看法），
> 请按**厂商/作者观点**引用，不要当成 Ray 源码里的结论。

---

## 38.4 会话状态放哪：四种放法与判据

上一节说的是"路由"，这一节说的是"状态本身"。它们是两件事，
但经常被一起搞错。四种放法：

| 放法 | 实现方式 | 适合什么 | 致命问题 |
|---|---|---|---|
| **① 副本内存里** | 副本是个普通 Python 对象，`self.history` | 单副本、实验、Demo | 副本一重启全丢；扩缩容即失效 |
| **② Ray Actor 里** | 部署一个 `SessionActor`（第 9 章），按 `session_id` 取命名 actor | 状态中等、要求低延迟、能接受状态有副本 | actor 挂了状态就没了（第 10 章 §10.5：**Actor 容错一定会丢状态**） |
| **③ Ray Serve 的 deployment actor** | `serve.get_deployment_actor(name)` / `serve.get_deployment_actor_context()` | 需要**跟着副本走**的辅助状态（连接池、缓存） | 粒度是"副本"，不是"会话" |
| **④ 外部存储** | Redis / Postgres / 对象存储 | 生产、多副本、要能扩缩 | 每次请求一次 IO；一致性要自己兜 |

**判据（按顺序问自己）：**

1. **状态丢了会不会导致错误答案？** 会 → ④（或 ② + 定期落盘）。
2. **同一会话会不会超过一个副本的容量？** 会 → ④。
3. **能不能接受副本重启时这个会话中断？** 能 → ②。
4. 以上都无所谓 → ①，别过度设计。

> **③ 值得展开一句**，因为它是 Ray 2.58 里比较新、也最少被讲的一块：
> `serve.get_deployment_actor_context()` 返回一个 `DeploymentActorContext`，
> 只能在**部署 actor 内部**调用；`serve.get_deployment_actor(actor_name)` 则按名字
> 拿到一个 Ray `ActorHandle`（第 9 章的那个东西）。
> 源码里的 docstring 明确写着它**必须在运行中的副本内部调用**，
> 且 actor 必须先在部署的 deployment actors 配置里声明过。
> **注意它的语义是"部署级别"而不是"会话级别"** ——
> 它天然适合放"每个副本一个"的连接池、tokenizer 缓存，
> 而**不适合**放"每个用户一份"的对话历史。
> 把这两个粒度搞混，是这一章里第二容易踩的坑。

---

## 38.5 一个最小但完整的 Agent 服务

理论说完了，来一个**真的能跑起来**的。目标很简单：
**一个有会话记忆的对话服务，并且它能调一个工具。**

关键设计（三点，每一点都对应前面的一节）：

1. **LLM 副本是无状态的**，用 Ray Serve 的 LLM 部署（第 15 章 §15.11）；
2. **会话状态放在一个独立的部署里**（§38.4 的方案 ② 的部署形态），
   按 `session_id` 取，这样它和 LLM 副本的扩缩容**解耦**
   —— ⚠️ 但骨架里状态仍在这个副本的内存中，所以它必须单副本；真要扩缩，走方案 ④；
3. **编排逻辑放在入口副本里**，它是异步的（不阻塞等待工具 API）。

```python
# agent_demo.py —— 最小可跑的 Agent 服务骨架
#
# ⚠️ 这份代码是**结构示例**：它把三层分工、会话状态、异步不阻塞这三件事
#    摆在一起。真实部署要按第 15 章 §15.13 的完整示例补上
#    autoscaling_config / ray_actor_options / 健康检查。
#    其中 LLM 部署部分需要装 ray[llm] 且真的能拿到模型权重。

import asyncio
import uuid
from typing import Any

from ray import serve
from ray.serve.handle import DeploymentHandle


# ── 第 1 层：会话状态（§38.4 方案 ②） ──────────────────────────────
# 一个**独立的部署**，不是入口副本的一部分 ——
# 这样入口副本重启（或滚动升级）不会丢状态。
# ⚠️ 但状态就在这个副本的内存里，所以它**必须保持单副本**（默认就是 1）：
#    一旦给它配上 autoscaling_config，请求落到哪个副本就只有那个副本看得见，
#    等于退回 §38.12 反模式 5。要扩缩，先按 §38.4 方案 ④ 把状态搬出去。
@serve.deployment(
    ray_actor_options={"num_cpus": 0.1},
    # 单副本下的并发靠 max_ongoing_requests，不是靠副本数
    max_ongoing_requests=100,
)
class SessionStore:
    def __init__(self) -> None:
        # 生产环境把这一行换成 Redis 客户端（§38.4 方案 ④）
        self._sessions: dict[str, list[dict[str, Any]]] = {}

    def append(self, session_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # 收的是**一批**消息：一次请求追加一整轮。
        # （如果设计成"每次追加一条"，就很容易写成只追加最后一条 —— 见 chat 的 ④）
        history = self._sessions.setdefault(session_id, [])
        history.extend(messages)
        return list(history)          # 返回副本，避免调用方改到内部状态

    def get(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._sessions.get(session_id, []))

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# ── 第 2 层：工具（§38.6 讲隔离，这里先只要能调） ────────────────────
# 真实系统里工具**必须是独立部署**：它可能挂、可能慢、可能被打爆，
# 而你不希望它把 Agent 的入口副本一起拖下水。
@serve.deployment(ray_actor_options={"num_cpus": 0.2})
class ToolBox:
    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        # 关键：工具是 async 的。同步的 requests.get 会把整个副本的事件循环卡住，
        # 让这个副本在等待期间**无法处理任何其他请求**（§38.1 特征③）。
        if name == "now":
            import datetime
            return {"ok": True, "value": datetime.datetime.now().isoformat()}
        if name == "echo":
            return {"ok": True, "value": args.get("text", "")}
        return {"ok": False, "error": f"unknown tool: {name}"}


# ── 第 3 层：编排（入口副本） ──────────────────────────────────────
@serve.deployment(
    ray_actor_options={"num_cpus": 0.2},
    # 入口副本要并发处理很多会话，且大量时间在 await —— 
    # 这是"低 CPU、高并发"的典型形态（§38.2 的错层警告）
    max_ongoing_requests=64,
)
class AgentEntry:
    def __init__(self, sessions: DeploymentHandle, tools: DeploymentHandle) -> None:
        self._sessions = sessions
        self._tools = tools

    async def _llm(self, messages: list[dict[str, Any]]) -> str:
        """占位：真实实现换成 serve.get_deployment_handle('llm', app_name) 的调用。"""
        await asyncio.sleep(0)          # 模拟一次异步模型调用
        last = messages[-1]["content"] if messages else ""
        if last.startswith("/tool"):
            return "TOOL:echo:" + last.removeprefix("/tool").strip()
        return f"(mock reply to {last!r})"

    async def chat(self, session_id: str, text: str) -> dict[str, Any]:
        # ① 取历史（一次网络调用，但是异步的）
        history = await self._sessions.get.remote(session_id)
        prior = len(history)              # 记下"来之前有几条"，写回时要用

        history.append({"role": "user", "content": text})

        # ② 循环：模型 → 工具 → 模型……（这就是"长时程"的形状）
        for _step in range(8):            # 硬上限，见 §38.7
            reply = await self._llm(history)
            history.append({"role": "assistant", "content": reply})

            if not reply.startswith("TOOL:"):
                break

            parts = reply.split(":", 2)
            if len(parts) != 3:
                # 模型吐了个不成形的 `TOOL:` 就按终止处理——
                # 否则这里的解包异常会连带炸掉整个请求
                history.append({"role": "system", "content": f"无法解析的工具调用：{reply!r}"})
                break
            _, tool_name, tool_arg = parts

            # ③ 工具调用：这里是单发。一轮里要并发多个工具，
            #    就换成 asyncio.gather 扇出，不要串行 await（§38.1 特征④）
            result = await self._tools.call.remote(tool_name, {"text": tool_arg})
            history.append({"role": "tool", "name": tool_name, "content": str(result)})
        else:
            # for-else：循环**没有**被 break（= 8 步全是工具调用）才走这里，
            # 也就是真的撞到了步数上限
            history.append({"role": "system", "content": "已达到工具调用步数上限"})

        # ④ 写回**这一轮新增的**消息（异步）。
        #    ⚠️ 千万别写成 history[-1]：那样只有最后一条落盘，
        #       用户说过的每一句话都会在下一轮消失 —— 而下面 client 里那句
        #       len(...) 断言**照样通过**（它靠的是上一轮的 assistant 回复）。
        await self._sessions.append.remote(session_id, history[prior:])
        return {"session_id": session_id, "messages": history}

    async def new_session(self) -> str:
        return uuid.uuid4().hex


# 用 .bind() 把两个部署的句柄注入到入口。这是 **Serve 的 .bind()** ——
# 它把依赖绑成一张可部署的图，返回的是 Application（第 15 章 §15.2）。
# ⚠️ 别和第 2 章 §2.2 讲的那个 .bind() 混了：那是 Ray Core DAG 的节点构造，
# 官方明确说它**不是**偏函数应用；Serve 这个才是"把参数/句柄绑进去"。
app = AgentEntry.bind(SessionStore.bind(), ToolBox.bind())
```

跑起来、验证它真的有会话记忆。

> ⚠️ **先读这一条，否则这段客户端会 404。**
> Serve 的 HTTP 语义是：**发到 `/` 的请求只会路由到副本的 `__call__`**，
> 其它路径（`/chat`、`/new_session`）**必须在 FastAPI app 上注册**
> （`@serve.ingress(app)` + `@app.post(...)`），**不会**因为副本里有个
> 同名方法就自动成为路由。
>
> 上面那个 `AgentEntry` 骨架**既没有 `__call__`，也没有 `@serve.ingress`** ——
> 所以要么补上入口，要么干脆不走 HTTP：
>
> | 走法 | 怎么做 |
> |---|---|
> | **A. 补 HTTP 入口** | 给 `AgentEntry` 加 `@serve.ingress(FastAPI())`，再把 `chat()` / `new_session()` 注册成 `@app.post("/chat")` / `@app.post("/new_session")` |
> | **B. 直接拿句柄调**（更省事，推荐先跑通这条） | 不开 HTTP，在 driver 里直接 `h = serve.get_deployment_handle("AgentEntry", app_name=...)`，然后 `ray.get(h.chat.remote(sid, "你好"))` |
>
> **本节的验收点（第二轮能看到第一轮的用户输入）两种走法都适用。**
> 下面这段客户端代码对应**走法 A**：

```python
# client.py —— 对应上面的走法 A（即 AgentEntry 已经加了 @serve.ingress）
import requests

BASE = "http://127.0.0.1:8000"
sid = requests.post(f"{BASE}/new_session").json()

r1 = requests.post(f"{BASE}/chat", json={"session_id": sid, "text": "你好"}).json()
r2 = requests.post(f"{BASE}/chat", json={"session_id": sid, "text": "接着上面说"}).json()

# 第二次请求里必须能看到第一次的**用户输入** —— 这才是"有状态"的最小验收。
# ⚠️ 只断言 len(r2) > len(r1) 是不够的：服务端如果只把"最后一条回复"写回
#    存储，这个断言照样通过，而用户的历史其实已经丢光了（见 chat 的 ④）。
user_texts = [m["content"] for m in r2["messages"] if m["role"] == "user"]
assert "你好" in user_texts, "会话状态没生效：第一轮的用户输入没被记住"
print("OK，会话记忆生效")
```

> ⚠️ **这段代码里最值得回看的一行**是 `self._sessions.get.remote(session_id)`
> 前面的那个 **`await`**。
> 它的语义是：`handle.method.remote(...)` 返回的是一个 `DeploymentResponse`
> （一个 future，**不是值**），`await` 它才拿到值，而且**不阻塞事件循环**。
> 换成同步的 `ray.get(...)` / `.result()`，这个副本就会在等 SessionStore 回复时
> **整个卡住** —— 而"卡住"的表现不是报错，是
> **QPS 掉到 1、CPU 占用接近 0、日志一片安静**。
> 这是 Agent 服务里最典型的"看起来像容量问题、实际是阻塞问题"。
> （顺带纠正一个常见误读：单纯把 `await` 删掉**不会**卡住，会 `AttributeError`——
> 因为 `history` 变成了 `DeploymentResponse`。真正危险的是"改成同步取值"。）

---

## 38.6 工具执行的隔离：Agent 安全的主战场

第 17 章 §17.5 讲了集群安全，但 Agent 引入了一类新东西：
**它会执行"模型生成的、你事先没审过的"代码和参数。**

这不是危言耸听。工具调用的参数是**模型产出的字符串**，
它可能包含路径穿越、注入的命令、超大的 payload。
三层防线，从弱到强：

| 防线 | 做法 | 挡住什么 | 挡不住什么 |
|---|---|---|---|
| **① 参数校验** | 工具入口做 schema 校验（pydantic）、白名单参数 | 绝大多数手滑和注入 | 精心构造的越权 |
| **② 资源与时间上限** | 工具的 `ray_actor_options` 限 CPU、超时、并发上限 | 打爆集群、无限循环 | 逻辑层的越权 |
| **③ 进程/容器隔离** | 工具跑在独立进程（Ray worker 天然如此）或容器里（`runtime_env` 的 `container`，见第 27 章 §F.2） | 影响主进程、读宿主机文件 | 内核级逃逸 |

**Ray 天然给你的那一层，比想象中多：**

* **工具跑在独立 worker 进程里** —— Python 层的崩溃（段错误、`os._exit`、内存泄漏）
  不会带走入口副本。这是第 3 章 §3.5（CoreWorker）的机制，用在这里。
* **`ray_actor_options` 可以限资源** —— 一个失控工具最多吃掉它声明的那点 CPU。
* **`runtime_env` 的 `container` 可以把工具关进容器** —— 第 27 章 §F.2 讲了字段和坑。
* **超时可以用 `ray.get(..., timeout=...)` 或 `asyncio.wait_for` 表达** ——
  但注意下一节要讲的：**超时之后任务还在跑。**

**Ray 不给你、必须自己做的事：**

* **工具的权限边界**（它能读哪些文件、连哪些数据库）——
  Ray 管的是"在哪跑"，不是"能碰什么"。
* **人类审批（human-in-the-loop）的落地** ——
  OTel 的 GenAI 语义约定里**没有**"这次调用是否被批准"这个属性（§38.8 会讲），
  所以审批记录得你自己建模。
* **提示注入的防御** —— 这是模型/应用层的事，运行时层无能为力。

> ⚠️ **`ray.kill` 与超时的真实语义**（第 5 章 §5.8、第 9 章 §9.5 讲过，这里重申）：
> **超时只解除"等待"，不解除"执行"。**
> `ray.get(ref, timeout=5)` 抛 `TimeoutError` 之后，那个任务**还在另一个进程里跑**。
> 如果你不显式 `ray.kill(actor)` 或取消任务，它会继续占用资源 ——
> 在 Agent 场景里这意味着**一次超时变成了一直超时**，资源只出不进。
> 这是"Agent 服务跑了三天之后莫名其妙满了"的最常见成因。

---

## 38.7 长时程的四个必须：超时、取消、断点、上限

§38.1 特征①（长时程）在这里兑现。四件事，**缺一件都会在生产上出事**：

**① 每一层都要有超时，而且要从外到内递减。**
外层 30s、内层工具 10s —— 反过来（内层比外层长）会导致外层已经放弃了，
内层还在烧资源。这是**唯一能防止"雪崩式堆积"的结构**。

**② 取消要真的取消。** 见上一节末尾的警告。Ray 侧的动作是
`ray.cancel(ref)`（任务）或 `ray.kill(actor)`（actor）——
注意 `ray.cancel` 对**已经在跑的普通任务**默认是"尽力而为"的
（`force=True` 才会真的杀进程，代价是可能留下脏状态）。
**这是 Ray 的一个真实约束，不是配置问题**（第 5 章 §5.8）。

**③ 断点要能续。** 一个跑了 40 步、第 41 步失败的 Agent，
重头再来是**不可接受的**（既费钱又费时，还可能因为不确定性给出不同结果）。
做法：把每一步的 `history` 落盘（第 10 章 §10.8 的检查点思路），
重试时从最近一次检查点恢复。
**Ray 给你的是"任务能重试"（`max_retries`），不给你"从中间重试"** ——
后者要自己在应用层做。

**④ 步数与预算要有硬上限。** 一个模型陷入循环（"再查一次资料"×50）时，
**没有任何机制会替你喊停**。`for _step in range(8)` 那一行不是防御性编程，
是**必需**。同理，token 预算、工具调用次数、墙钟时间，
三者至少要有一个硬上限。

```python
# 三个上限的落地形状（片段）
import time

class AgentBudgetExceeded(RuntimeError):
    """预算耗尽 —— 是一个错误，不是一个结果（下面那条引用就是在说这件事）。"""

MAX_STEPS = 8                       # ① 步数
MAX_WALL_CLOCK_S = 120              # ② 墙钟
MAX_TOOL_CALLS = 16                 # ③ 工具次数

async def run_agent(...):
    deadline = time.monotonic() + MAX_WALL_CLOCK_S
    tool_calls = 0
    for step in range(MAX_STEPS):
        if time.monotonic() > deadline:
            raise AgentBudgetExceeded("wall clock")     # 不要 return None
        ...
        if tool_calls >= MAX_TOOL_CALLS:
            raise AgentBudgetExceeded("tool calls")
```

> **为什么这里强调"不要 `return None`"**：把"超出预算"表达成一个正常返回值，
> 会让调用方以为 Agent 得出了结论。**预算耗尽是一个错误，不是一个结果。**
> 这一类"用返回值掩盖失败"的写法，在 Agent 代码里比在别处更常见，
> 因为它看起来更"优雅"——而它会让你的线上成功率统计**永远好看**。

---

## 38.8 观测：Agent 的追踪要记什么

第 36 章讲了 OpenTelemetry 的接法，第 11 章讲了 Ray 自己的状态与指标。
Agent 这条线上，多了一套**正在成形的行业属性约定**：
**OpenTelemetry GenAI semantic conventions**。

下面这张表里的每一个属性名，都是我从规范仓库里逐字核对的
（`open-telemetry/semantic-conventions-genai`）：
属性名单在 `docs/registry/attributes/gen-ai.md`；
`create_agent` / `invoke_agent` 的 span 名在 `docs/gen-ai/gen-ai-agent-spans.md`；
而 **`execute_tool` 的 span 名在 `docs/gen-ai/gen-ai-spans.md`**
—— ⚠️ **不在** agent-spans 那个文件里。按文件名猜出处，这里一定会指错。

| 属性 | 语义 | 必需性 |
|---|---|---|
| `gen_ai.operation.name` | 这一步在干什么 | 各 span 上均为 **Required**，取值见下表 |
| `gen_ai.provider.name` | 模型提供方 | **Required** |
| `gen_ai.agent.name` | Agent 的名字 | Conditionally Required（"应用提供时"/"拿得到时"） |
| `gen_ai.conversation.id` | **会话标识** —— 把多轮串成一条 | Conditionally Required |
| `gen_ai.tool.name` | 工具名 | `execute_tool` span 上 **Required** |
| `gen_ai.tool.call.id` | 工具调用 ID | `execute_tool` span 上 Recommended |
| `gen_ai.tool.type` | 工具类型：`function` / `extension` / `datastore` | `execute_tool` span 上 Recommended |

（后两行别当成"必需"——规范给的是 `Recommended`；而 `gen_ai.tool.name` 是 `Required`。
三者的必需性**并不相同**。）

`gen_ai.operation.name` 的取值里，和 Agent 直接相关的是三个：

| 取值 | 含义 | span 名的写法 |
|---|---|---|
| `create_agent` | 构造（初始化）Agent | `create_agent {gen_ai.agent.name}` |
| `invoke_agent` | 跑一次 Agent | `invoke_agent {gen_ai.agent.name}`（名字拿不到时就是 `invoke_agent`） |
| `execute_tool` | 调一次工具 | `execute_tool {gen_ai.tool.name}` |

**三个必须说清楚的事实**（否则你会照着这张表写出经不起推敲的断言）：

1. **这套约定截至 2026 年仍是 Development 状态**，规范的每个属性都标着
   `![Development]` 徽章，**没有稳定时间表**。
   用它，但不要把 dashboard 绑死在具体属性名上。
2. **`gen_ai.system` 已被 `gen_ai.provider.name` 取代。**
   你在 2024–2025 年的博客里看到的 `gen_ai.system`，照着抄会踩坑。
3. **内容默认不被采集。** 对话内容要显式开
   `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` 才会记录 ——
   这是**合规上的默认安全**，不是 bug。

**规范明确没有覆盖的**（这一段比上面更有价值）：
**它记录"发生了什么"，不记录"是否被允许"。**
没有属性表达"这次工具调用是否经过策略审批""是否有人类批准"。
`gen_ai.agent.name` 记录了 Agent 是谁，**但没有记录"谁授权了这个 Agent"**。
所以：**治理与审批是你自己的模型，不要指望语义约定替你兜住。**

**如果你的工具是 MCP 工具，规范里还有一层专门的约定。**
规范仓库里另有一份 `docs/gen-ai/mcp.md`（同样是 **Development** 状态，
不是标准），server 侧与 client 侧的 span 都定义了，要点三条：

* span 名是 `{mcp.method.name} {target}`，其中 `target` SHOULD 匹配
  `{gen_ai.tool.name}` 或 `{gen_ai.prompt.name}`（没有合适的 target 时就是 `{mcp.method.name}`）；
* `mcp.method.name` 是 **Required**（如 `tools/call`），
  `gen_ai.prompt.name` 是 Conditionally Required；
* **最该记住的一条**：`gen_ai.tool.call.arguments` 与 `gen_ai.tool.call.result`
  的必需性是 **`Opt-In`** —— 默认**不记**。
  工具参数和返回值恰恰是最可能含敏感数据的地方（`gen_ai.tool.description`
  也挂着 "may contain sensitive information" 的警告）。
  这和上面第 3 点"内容默认不采集"是同一个合规立场，
  也正好接上 §38.9 那条"落盘前脱敏"。

**和 Ray 的接法**（第 36 章的机制直接复用）：

| 要串起来的东西 | 用什么串 | 看哪一节 |
|---|---|---|
| 一次 Agent 请求跨的多个副本 | OTel trace context 手动传播 | 第 36 章 §36.3 |
| Ray 的 task/actor ID 与 span 关联 | 把 Ray 的 ID 写进 span 属性 | 第 36 章 §36.5 |
| 每个工具调用的成功率与 p95 | 按 `gen_ai.tool.name` 切片 | 第 11 章 §11.5 |
| 会话级别的回放 | `gen_ai.conversation.id` | 本节 |

> **一个实践上的坑**：工具调用的失败率**很容易被整体错误率掩盖** ——
> 一个工具 5% 失败，整体成功率看起来还有 95%，
> 但**那 5% 的会话会走偏**（模型拿到错误后换一条路，最终给出错误的"正确答案"）。
> 所以工具维度的**单独**成功率曲线是必须的，不能只看总成功率。
> （这一条是行业实践共识，不是 Ray 的机制。）

---

## 38.9 数据面：轨迹既是日志，也是训练集

Agent 跑起来之后，**它自己会生产一种非常特殊的数据**：
轨迹（trajectory）—— 输入、每一步的模型输出、每一次工具调用的参数与返回、
最终结果、以及（如果有）人类反馈。

这份数据的特殊之处：**它同时是三个东西**
—— 排障用的日志、评估用的样本、训练用的数据。
第 12 章讲的 Ray Data 在这里第一次不是为了"处理业务数据"，
而是**为了处理 Agent 自己吐出来的东西**。

一条典型的轨迹处理流水线：

```python
import ray
from ray.data import Dataset

# 1) 读：轨迹通常是 JSONL（每行一条完整轨迹）
ds: Dataset = ray.data.read_json("s3://traces/2026-09-*/")     # 第 12 章 §12.3

# 2) 分类：把"预算耗尽""工具全挂""用户中断"这些**不该进训练集**的挑出来。
#    注意不要把失败样本直接丢掉 —— 它们对"失败模式分析"和"负样本"都有用
#    ⚠️ map 的函数签名是 Dict -> Dict，返回的 dict **就是新的行**：
#       只 return 一个字符串会把其它列全丢掉（第 12 章 §12.2）
def classify(row):
    if row.get("terminated_reason") == "budget_exceeded":
        label = "exhausted"
    elif row.get("tool_error_rate", 0) > 0.5:
        label = "tool_broken"
    else:
        label = "ok"
    return {**row, "label": label}

ds = ds.map(classify, compute="tasks")        # 第 12 章 §12.2

# 3) 先只留"判定成功"的轨迹，再去重：同一个 prompt 反复出现是常态
#    （用户重试、系统重跑）；Ray Data 的 groupby 比 pandas 的 drop_duplicates 更能扛量
#    ⚠️ groupby(...).count() 的输出**只有分组键和 count 两列** ——
#       聚合之后再想按 label 过滤，会发现那一列已经没有了
ok = ds.filter(lambda r: r["label"] == "ok")
deduped = ok.groupby("prompt_hash").count()

# 4) 构造训练集：这一步的形状由你用什么 RL 框架决定 —— 见 §38.10
train = deduped.filter(lambda r: r["count"] >= 1)
```

**四个必须注意的点**（都是这条线特有的，不是通用的数据处理建议）：

1. **轨迹里可能有 PII。** 用户的输入、工具返回的内容，
   都可能含个人信息。落盘前就该脱敏，**不是等训练时再脱**。
   `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` 默认关闭，
   正是这个道理（§38.8）。
2. **轨迹的体积会长得比你想象的快。** 一次 Agent 请求几 KB 到几十 KB，
   乘上每天的请求数 —— 这是**又一个"批处理该和在线服务共用集群"**的理由
   （第 12 章 §12.5 讲的分工）。
3. **"成功"的定义要先定下来。** 没有明确的判据，
   你的训练集里会混进大量"模型自认为成功"的样本。
   **这一步做错，后面所有的 RL 都白做。**
4. **轨迹的 schema 会变。** 你今天存 `{prompt, response}`，
   下周加了工具调用就变成 `{prompt, steps[], response}` ——
   第 35 章讲的数据版本与血缘，在这里不是"最佳实践"，是**必需品**。

---

## 38.10 训练面：Agent 的 RL 为什么又回到 Ray

第 16 章讲过 RLlib 和 `verl` 的选型，第 19 章 §19.8 画过 RL 框架生态地图。
这里只补一个 2026 年的增量：**Agent 的 RL 已经成了独立的一档需求**，
它的名字叫 **agentic RL**（或者 multi-turn / long-horizon tool-use RL）。

**它和传统 RLHF 的区别，一句话：**
**RLHF 优化的是"一次回答好不好"；agentic RL 优化的是"一串动作好不好"。**
后者的 rollout 不能再当成"一次前向"，而必须是
"跑完整个 Agent 环境，收集完整轨迹，再拿最终结果（或过程奖励）来算 advantage"。

于是第 16 章的所有基础设施问题**翻倍**：
一次 rollout 要几十次模型调用 → 采样成本高一个量级；
环境要能重放、要能并发跑上千个实例 → **这又回到了 Ray 最擅长的事**。

**2026 年这条线上的主要玩家**（下面这一档，我明确标注为
**"据各项目官方仓库/文档，未逐条核实到 commit"**）：

| 框架 | 定位 | 和 Ray 的关系 |
|---|---|---|
| **verl** | 大规模 RLHF / agentic RL，GRPO/PPO | 跑在 Ray 上（第 16 章 §16.12 已讲） |
| **SkyRL** | Agentic RL，长时程、真实环境（SWE 类任务） | 跑在 Ray 上；有独立的 agent 层 |
| **OpenRLHF** | 分布式 RLHF + agentic RL | 跑在 Ray 上 |
| **ART**（OpenPipe） | Agent 优先的 GRPO 循环，RULER 奖励 | 见其官方仓库 |
| **Agent Lightning**（微软） | **不改写现有 Agent 栈**，在其上做 RL | 见其官方仓库 |
| **RAGEN** | 多轮 Agent RL | 见其官方仓库 |

> ⚠️ **一条 2026 年的实操提醒（可自行复核）**：
> **`verl` 的仓库地址已经变了。**
> 很多 2025 年的文章指向 `volcengine/verl`，现在那是 **301 跳转**，
> 真实地址是 **`verl-project/verl`**。
> 复核命令：
> ```bash
> curl -s -o /dev/null -w "%{http_code}\n" https://github.com/verl-project/verl   # 200
> curl -s -o /dev/null -w "%{http_code}\n" https://github.com/volcengine/verl      # 301
> ```
> **照抄一年前的博客去 clone，是这条线上最常见的"环境问题"。**

**给读者的选型判据（按顺序问）：**

1. **我的 Agent 是现成框架写的、不想重写？** → Agent Lightning 这一档。
2. **我要跑的是代码/浏览器/SQL 这类需要真实环境的长时程任务？** → SkyRL 这一档。
3. **我要的是大规模、算法全、社区大？** → verl / OpenRLHF。
4. **我只想把 RLlib 用起来（第 16 章）？** → 先确认最新的 API stack 支持你的场景，
   不要照抄 2024 年的 `PPOConfig` 写法。

**一个反直觉但重要的事实**：
在这条线上，**环境和评测循环比算法名重要得多**。
"奖励能不能自动判定""工具调用日志能不能可靠落盘""失败能不能复现"
这三件事没做好，换哪个算法都不会有好结果。
这也解释了为什么 §38.9 那一节（数据面）值得写得比这一节还长。

---

## 38.11 成本与容量：Agent 比推理服务贵在哪

Agent 的成本结构和推理服务**不一样**，这决定了优化方向不一样：

| 成本项 | 推理服务 | Agent |
|---|---|---|
| GPU 时间 | **主项** | 中项（被 KV 复用摊薄） |
| 外部 API | 无 | **可能是主项** |
| 重试/失败重跑 | 低 | **高**（长时程放大） |
| 空转等待 | 低 | **高**（GPU 在等工具） |
| 存储（轨迹） | 低 | 中到高（§38.9） |

**三个立刻能省的杠杆：**

1. **不要把贵模型用在便宜步骤上。** 分类、抽取、路由这些步骤
   用一个小模型，规划与最终生成用大模型。这在 Ray 里体现为
   **两个独立的 LLM 部署，各自扩缩容**（第 15 章 §15.16 的模型多路复用也是这条路）。
2. **GPU 副本不能阻塞在工具调用上。** §38.2 那条错层警告的经济学版本：
   一个阻塞的 GPU 副本 = **你按 GPU 的价钱买等待时间**。
3. **重试要有上限且要区分类型。** 工具 500 错误值得重试，
   参数校验失败重试一万次也不会成功（第 10 章 §10.2 的重试语义）。

**混合部署**是 Agent 场景下最容易被忽略的一招：
Agent 的在线服务（低延迟、突发）和轨迹处理/离线评估（吞吐、可中断）
**天然适合放在同一个 Ray 集群**（第 12 章 §12.5 讲的分工）。
Ray 的资源模型（第 8 章 §8.2）让你能用 `num_cpus` 声明把这两类负载分开，
而**不用维护两套集群**。⚠️ 但要注意抢占与优先级 ——
第 8 章 §8.4 列的七种策略只回答"放到哪个节点"，
**没有一种回答"谁应该先让出来"**：Ray 没有面向用户的优先级抢占，
需要靠放置组（第 8 章 §8.5）分池、或干脆在应用层做逻辑隔离。

---

## 38.12 反模式清单

按"踩了会很痛"排序。前三条是会直接导致线上事故的。

| # | 反模式 | 症状 | 正解 |
|---|---|---|---|
| 1 | **同步阻塞的编排副本** | QPS 卡在个位数、CPU 接近 0、日志安静 | 全链路 `await`；工具用 async。§38.5 |
| 2 | **超时后不 kill** | 资源只出不进，跑几天后集群满了 | 超时后显式 `ray.kill` / `ray.cancel`。§38.6 |
| 3 | **没有步数/预算硬上限** | 偶发的无限循环吃满集群 | 三个上限一个都不能少。§38.7 |
| 4 | **传了 `session_id` 就以为有粘性** | 会话状态时有时无，**且不报错** | 默认的 Pow2 路由根本不看 `session_id`；要粘性得显式挂 `ConsistentHashRouter`。§38.3.1 |
| 5 | **把对话历史放在副本内存里** | 扩缩容/重启后用户"失忆" | 状态外置或用独立 actor 部署。§38.4 |
| 6 | **工具和编排在同一个副本里** | 一个慢工具拖垮整条服务 | 工具独立部署。§38.6 |
| 7 | **只看总成功率** | 5% 的工具失败率藏住了 5% 的走偏会话 | 工具维度单独监控。§38.8 |
| 8 | **预算耗尽 `return None`** | 线上成功率永远好看，实际大量无结论 | 预算耗尽是**错误**，要抛。§38.7 |
| 9 | **轨迹不脱敏就落盘** | 合规事故，且**不可回溯** | 落盘前脱敏。§38.9 |
| 10 | **照抄一年前的仓库地址** | `clone` 失败或拿到废弃版本 | `verl` 已迁到 `verl-project/verl`。§38.10 |

这 10 条里，**前 7 条全是"运行时层"的**（阻塞、资源泄漏、无上限、粘性、状态、隔离、观测）；
剩下 3 条分别属于**应用代码**（#8 用返回值掩盖失败）、**数据合规**（#9 落盘不脱敏）
和**依赖管理**（#10 照抄旧地址）。

**一条都不是"模型不够聪明"** —— 这个比例本身就是这一章的论点：
**Agent 失败，多数时候不是模型的问题，是运行时没搭对。**

---

## 38.13 与 mini-ray 的关系

诚实地说：**mini-ray 里没有 Agent 这一层，也不应该有。**
理由和它没有 Ray Data / Ray Train 一样 —— 那些是**库**，不是**核心**。

但 Agent 这条线**把 mini-ray 里已有的零件全部用了一遍**，值得逐个点名，
因为它们正是这一章所有建议的"最小可复现版本"：

| 这一章讲的能力 | mini-ray 里对应的实现 | 在哪 |
|---|---|---|
| 会话状态放在有状态对象里 | **Actor**（邮箱、顺序保证、句柄） | 第 9 章 §9.9、`miniray/actor.py` |
| 不阻塞的等待 | **async actor**：一个 asyncio 事件循环，每个并发组一条独立的长轮询 | 第 6 章 §6.9、第 9 章 §9.9、`miniray/worker.py` |
| 工具/任务失败不污染主流程 | **任务重试 + 异常跨进程回传** | 第 10 章 §10.10 |
| 超时与取消 | **`ray.get(timeout=)` / 取消路径** | 第 5 章 §5.8、§5.9 |
| 长时程任务的资源记账 | **调度器的资源回收**（`_release` 的幂等标记，第六轮审计的修复项之一） | `miniray/scheduler.py`；缺陷清单见 `mini-ray/README.md`、第 8 章 §8.10 |
| 观测 | **State API + 指标出口** | 第 11 章 §11.10 |
| 轨迹数据的批处理 | mini-ray 有 `util/queue.py` 这类原语，但**没有数据管道** | 附录 B §B.9 |

⚠️ **一个特别值得你自己动手验证的对照**：
这一章 §38.5 里那个"改成同步取值、副本就卡住"的例子，
在 mini-ray 里的对应物是 **async actor 的执行上下文**。
第六轮审计在 mini-ray 的 async actor 路径上抓到过一个真实缺陷 ——
**async 上下文只轮询 `""` 组，而 raylet 按方法声明的 `concurrency_groups` 投递，
导致其他组的邮箱永远没人取**。
这个 bug 的形状，和你在真实 Agent 服务里写错一个 `await`
（或者用了错误的并发组）时**完全一样**：
**不报错、不崩溃、就是没有进展。**
这正是"玩具实现值得写"的理由 —— **它把生产事故的形状，缩到了一个你能在五分钟里看完的文件里。**

---

## 38.14 本章小结

回到开头那句话：**Agent 的智能来自模型和框架，能不能跑起来来自运行时。**

1. **Agent 负载的五个特征**（长时程、有状态、大部分时间在等、突发、可训练）
   决定了它的所有工程约束。**先认负载，再选机制。**
2. **三层分工**：编排层 / 运行时层 / 引擎层。
   遇到问题先问"这该由哪一层负责"（第 30 章 §30.9 那条规矩）。
   **最常见的错层是把阻塞问题当容量问题**，于是花很多钱买等待。
3. **会话亲和性在 Ray 2.58.0 的真实边界**：
   `session_id` 的**管道已经铺好**（Python handle / HTTP header / gRPC 三个入口，
   常量 `RAY_SERVE_SESSION_ID_HEADER_KEY` 默认 `x-session-id`），
   **消费它的 `ConsistentHashRouter` 也已经存在**
   （`ray/serve/experimental/consistent_hash_router.py`，experimental）——
   但**默认挂的不是它**，默认的 Pow2 完全不看 `session_id`。
   把"默认没有"当成"没有"，和把"PR 里有"当成"现在有"，是这条线上对称的两个误解。
4. **会话状态有四种放法**，判据是"丢了会不会导致错误答案"。
   注意 `serve.get_deployment_actor` 的粒度是**副本**，不是**会话**。
5. **工具执行的隔离**是 Agent 安全的主战场。
   Ray 天然给了进程隔离与资源上限，但**权限边界、人类审批、提示注入防御要自己做**。
6. **长时程的四个必须**：分层超时、真取消、断点续跑、硬上限。
   ⚠️ 记住 **`ray.get(timeout=)` 只解除等待，不解除执行**。
7. **观测**：OTel GenAI 语义约定已经在成形
   （`invoke_agent` / `execute_tool` / `gen_ai.conversation.id` 等），
   但仍是 **Development 状态**，且**不覆盖"是否被批准"这类治理语义**。
8. **轨迹既是日志、也是样本、也是训练数据** ——
   它把第 12 章（数据）和第 16 章（RL）在这条线上重新缝到了一起。
9. **agentic RL 的核心难点是环境和评测循环，不是算法名。**
   选框架之前先回答："奖励能不能自动判定？失败能不能复现？"
10. **成本结构变了**：GPU 时间不再是唯一主项，
    重试、外部 API、空转等待都是。**别按 GPU 的价钱买等待。**

> **最后一句**：这一章和前 37 章的关系，可以用一个类比收尾 ——
> 第 37 章教你**怎么让一次模型调用更快**；
> 这一章教你**怎么让一千次模型调用加上一百次工具调用，
> 在会出故障的前提下，仍然能给用户一个答案**。
> 前者是性能问题，后者是系统问题。
> **Ray 的整个存在意义，一直在后者那边。**

---

> **全书结语不在这里。**
> 第八轮修订新增了 **[第 39 章：Ray 源码阅读与事实核查指南](ray教程-39-Ray源码阅读与事实核查指南.md)**，
> 它排在最后 —— **全书的结语也随之移到了那一章的末尾**。
>
> 为什么要加第 39 章：这一章不含新的 Ray 知识，
> 它教的是**怎么否决前面 38 章** —— 怎么拿到源码、去哪个目录查什么、
> 怎么证明"不存在"、怎么判断一个 API 的稳定性与弃用。
> 这本书反复宣示"回源码核对"，却一直没把这件事**作为方法教出来**。
>
> 所以这一章的收尾方式也变了：不再是"这本书讲完了"，
> 而是"**现在你可以来检查我们了**"。
>
> 阅读顺序：… → 第 37 章（推理引擎那一层）→ 第 38 章（Agent 工作负载）
> → **第 39 章（核查方法）→ 结语**。
