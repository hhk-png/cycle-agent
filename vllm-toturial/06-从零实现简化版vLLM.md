# 第 6 章：从零实现一个简化版 vLLM（mini-vLLM）

> 这一章是教程的核心。我们用 **纯 NumPy** 从零写一个可以真正运行的最小 vLLM——包括 PagedAttention、连续批处理调度、前缀缓存、抢占、分块 prefill、OpenAI 兼容服务器、量化演示和投机解码演示。代码全部在 `vllm-toturial/mini-vllm/`，可以在 CPU 上跑通。

---

## 6.1 为什么要写一个简化版？

vLLM 真正的价值不在于某一行代码，而在于一组**工程思想**：

1. **KV cache 是稀缺资源** —— 必须分页管理，不能整块分配。
2. **服务是动态的** —— 请求随时来、随时走，调度器必须连续批处理。
3. **调度与计算解耦** —— 调度器决定"跑什么"，模型执行器决定"怎么跑"。
4. **对外暴露标准 API** —— 让所有生态（OpenAI SDK 等）开箱即用。

这些思想用 CUDA kernel 实现会很复杂，但用 NumPy 表达就非常清楚。mini-vLLM 就是把这些思想用最直白的方式实现一遍：

- **同一个数据布局**：KV cache 是 `(num_blocks, block_size, num_heads, head_dim)` 的块状数组，配合 block table。
- **同一条调度逻辑**：每个 step 先 `schedule()` 再 `execute()`。
- **同一个服务形态**：FastAPI + SSE 流式。

### 设计原则

- **正确性优先**：任何一步都先和稠密（dense）注意力做数值对比。
- **工程结构清晰**：每个文件对应 vLLM 的一个核心模块。
- **可运行、可验证**：`tests/` 下的测试覆盖了关键不变量。

---

## 6.2 目录结构

```
mini-vllm/
├── pyproject.toml
├── minivllm/
│   ├── __init__.py        # 包入口
│   ├── config.py          # EngineConfig / ModelConfig / CacheConfig / SchedulerConfig / SamplingParams
│   ├── tokenizer.py       # 字符级 tokenizer（接口模仿 HF tokenizer）
│   ├── model.py           # TinyGPT：纯 NumPy 的 GPT 式 transformer
│   ├── attention.py       # PagedAttention：分块注意力
│   ├── kv_cache.py        # KVStore / BlockAllocator / PrefixCache / BlockManager
│   ├── scheduler.py       # 连续批处理调度器
│   ├── sampler.py         # 温度/top-k/top-p + 采样
│   ├── sequence.py        # Sequence / RequestOutput 数据结构
│   ├── engine.py          # LLMEngine：串起一切
│   ├── async_engine.py    # AsyncLLMEngine：后台线程跑引擎
│   ├── api_server.py      # OpenAI 兼容 FastAPI 服务器
│   ├── cli.py             # serve / chat / demo / spec / quant 子命令
│   ├── training.py        # 训练用的前向/反向（教育用途）
│   ├── quantize.py        # 纯 NumPy int8 权重量化
│   ├── speculative.py     # bigram 草稿模型的投机解码演示
│   ├── checkpoint.py      # 保存/加载 模型+配置+tokenizer
│   └── data.py            # 训练语料
├── scripts/train.py       # 训练入口
├── tests/                 # 测试
└── artifacts/tinygpt/     # 训练出的检查点
```

对应关系：`config.py ↔ vllm/config.py`、`scheduler.py ↔ vllm/core/scheduler.py`、`kv_cache.py ↔ vllm/core/block_manager_v1.py`、`attention.py ↔ vllm/attention/ops/paged_attn.py`、`engine.py ↔ vllm/engine/llm_engine.py`、`api_server.py ↔ vllm/entrypoints/openai/`。

---

## 6.3 配置层：`config.py`

真实 vLLM 用 `EngineArgs` 解析命令行，然后拆成 `ModelConfig`、`CacheConfig`、`SchedulerConfig`、`ParallelConfig` 传给各个组件。mini-vLLM 用 `dataclass` 表达同样的分层：

```python
@dataclass
class ModelConfig:
    vocab_size: int = 128
    n_embd: int = 64          # 隐藏层维度（d_model）
    n_layer: int = 2          # transformer 层数
    n_head: int = 4           # 注意力头数
    head_dim: int = 16        # 每头维度 = n_embd // n_head
    block_size: int = 64      # 最大上下文长度（n_positions）

@dataclass
class CacheConfig:
    block_size: int = 16      # KV block 大小（vLLM 默认 16）
    num_gpu_blocks: int = 64  # 物理 block 数量

@dataclass
class SchedulerConfig:
    max_num_seqs: int = 8             # 最大并发序列
    max_num_batched_tokens: int = 512 # 每步 token 预算
    enable_chunked_prefill: bool = True
    enable_preemption: bool = True

@dataclass
class SamplingParams:
    temperature: float = 1.0
    top_k: int = -1
    top_p: float = 1.0
    max_tokens: int = 64
    stop: Optional[List[str]] = None
    ignore_eos: bool = False
```

> 注意 `num_gpu_blocks`：真实 vLLM 会按 `gpu_memory_utilization` 自动算出一块显存能放下多少 block，这里显式给出，把"算"变成"配"。

---

## 6.4 Tokenizer

vLLM 依赖 HuggingFace 的 tokenizer（BPE / SentencePiece / Llama tokenizer）。mini-vLLM 用**字符级** tokenizer，好处是完全自包含。接口模仿 `PreTrainedTokenizer`：

```python
class CharTokenizer:
    def encode(self, text) -> List[int]:   # 字符 → id
    def decode(self, ids)  -> str:         # id → 字符
    @property
    def eos_token_id(self): return len(self.chars)  # 预留一个 EOS id
```

真实 BPE tokenizer 把整词/子词作为 token，效率高；字符级会把"the"拆成 3 个 token。但**分页 KV cache、连续批处理这些机制与 token 粒度无关**，所以我们用它来教学毫无问题。

---

## 6.5 模型与 PagedAttention

### 6.5.1 TinyGPT（`model.py`）

一个 GPT-2 式的小 transformer，所有运算用 `np.einsum` 表达。关键设计：**KV cache 在模型外部**。

> 架构注解：TinyGPT 是"最简组合"——**MHA**（4 头、每头独立 K/V）+ **learned 绝对位置编码**（`wpe[positions]`）+ 简单 LayerNorm + GELU。真实主流模型的 GQA / RoPE / SwiGLU / MoE 变体，在第 19 章《模型架构基础》里有系统讲解；模型层的 KV 语义对引擎来说完全一样。`forward()` 只负责：

1. 计算新 token 的 K/V；
2. 把 K/V 写入外部给的物理 block（`block_manager.write_kv`，自动处理 COW）；
3. 用 PagedAttention 计算注意力；
4. 返回 logits。

```python
def forward(self, x, positions, seqs, block_manager):
    tok_emb = self.wte[x] + self.wpe[positions]      # 词嵌入 + 位置嵌入
    for layer in range(n_layer):
        qkv = ln(tok_emb) @ self.c_attn_w[layer]     # q/k/v 投影
        q, k, v = split(qkv)                         # 切成三份，再按头 reshape
        # 写入分页 KV cache（处理 copy-on-write）
        block_manager.write_kv(layer, seq, pos, k, v)
        attn = paged_attention_batch(q, k, v, cached_lens,
                                     block_tables, kv_store, layer)
        ...
    return logits  # h @ wte.T（权值绑定）
```

注意：`block_tables` 和 `cached_lens` 是**逐序列**的。一个批次里不同序列的上下文长度不同、物理 block 不同，这正是 PagedAttention 存在的意义。

### 6.5.2 PagedAttention（`attention.py`）

真实 vLLM 用一个 Triton kernel 完成"根据 block table 抓取散落的 KV 并计算注意力"。mini-vLLM 把逻辑拆成两步，数值完全一致：

**第一步 `gather_kv`** —— 按 block table 把逻辑区间 `[logical_start, logical_end)` 的 K/V 从散落的物理 block 里取出来：

```python
def gather_kv(k_cache, v_cache, block_table, logical_start, logical_end,
              block_size, num_heads, head_dim):
    block_start = logical_start // block_size
    block_end   = (logical_end + block_size - 1) // block_size
    for bi in range(block_start, block_end):
        phys = block_table[bi]            # 逻辑 block → 物理 block
        lo = max(0, logical_start - bi * block_size)
        hi = min(block_size, logical_end - bi * block_size)
        ks.append(k_cache[phys, lo:hi])   # 取该物理 block 的一部分
    return np.concatenate(ks, 0), np.concatenate(vs, 0)
```

**第二步** —— 对每个序列，把"缓存前缀 + 本步新 token"拼成逻辑上的完整 K/V，做因果注意力：

```python
# 每个序列 b：
k_full = concat([gather_kv(...), k_new[b]], axis=0)   # 长度 = cached_len + T
scores = einsum("hit,jht->hij", q[b], k_full) / sqrt(head_dim)
mask   = key_positions > query_positions               # 因果 mask
probs  = softmax(where(mask, -inf, scores))
out    = einsum("hij,jht->hit", probs, v_full)
```

> **为什么正确**：虽然我们把缓存"抓取"成了连续数组，但它的来源是**非连续**的物理 block。数值上和把整个 K/V 存在一块连续内存里完全一样。真正的 vLLM 把抓取和注意力**融合**进 kernel，避免这块临时内存。

#### 验证

`tests/test_paged_attention.py` 把 PagedAttention 的输出和稠密注意力参考实现做 `allclose` 对比：

```bash
python tests/test_paged_attention.py   # test_paged_attention: OK
```

---

## 6.6 KV Cache 与 BlockManager（`kv_cache.py`）

这是 mini-vLLM 的心脏，对应 vLLM 的 `BlockManager`。

### KVStore

```python
shape = (num_blocks, block_size, num_heads, head_dim)
self.k_cache = [np.zeros(shape, np.float32) for _ in range(n_layer)]
self.v_cache = [np.zeros(shape, np.float32) for _ in range(n_layer)]
```

### BlockAllocator：空闲链表 + 引用计数

```python
class BlockAllocator:
    def allocate(self):
        b = self.free.pop(); self.ref_count[b] = 1; return b
    def free_block(self, b):
        self.ref_count[b] -= 1
        if self.ref_count[b] == 0:
            self.free.append(b)      # 只有引用归零才真正释放
```

引用计数是**前缀共享**与 **copy-on-write** 的基础：一个 block 可能被多条序列和前缀缓存同时引用，只有全部释放才回收。

### BlockManager

对外的核心操作：

```python
ensure_blocks(seq, n)      # 保证序列有足够物理 block
can_allocate(seq, n)       # 检查空闲 block 是否够
match_prefix(prompt_ids)   # 前缀缓存命中
attach_shared_blocks(...)  # 复用共享前缀的 block（引用 +1）
write_kv(layer, seq, pos, k, v)   # 写 K/V，必要时 COW
release(seq)               # 序列结束/被抢占时释放全部 block
register_prefix(seq)       # 序列结束后把 prompt 前缀存入缓存
```

#### Copy-on-Write：共享 block 不能乱写

如果某个物理 block 被多个序列共享（引用数 > 1），要往里面写数据时必须**先复制**：

```python
def write_kv(self, layer, seq, logical_pos, k, v):
    phys = table[logical_pos // block_size]
    if self.allocator.ref_count[phys] > 1:
        new_phys = self.allocator.allocate()          # 新 block
        self.kv_store.k_cache[layer][new_phys] = self.kv_store.k_cache[layer][phys]
        self.v_cache[layer][new_phys] = self.v_cache[layer][phys]
        self.allocator.free_block(phys)               # 释放一个共享引用
        table[logical_pos // block_size] = new_phys   # 更新 block table
    self.kv_store.k_cache[layer][phys, offset] = k    # 现在独占，安全写入
```

---

## 6.7 连续批处理调度器（`scheduler.py`）

调度器对应 vLLM 的 `vllm/core/scheduler.py`。每个 `schedule()` 调用产出**一个 engine step 的批**，包含三类工作：

```python
@dataclass
class ScheduledStep:
    prefill_items: List[(seq, chunk)]   # 要 prefill 的（序列, token 数）
    decode_items:  List[seq]            # 要 decode 一个 token 的序列
    preempted:     List[seq]            # 本步被抢占的序列
```

调度分三个阶段：

**Phase A —— decode**：所有已 prefill 完成（`phase == "DECODE"`）的运行中序列各 decode 一个 token。

**Phase B1 —— 续 prefill**：继续给"prefill 到一半"的序列喂下一块。

**Phase B2 —— 准入新请求**（FCFS）：把等待队列里的序列接进来。

### 分块 prefill 的 token 预算 + block 预算

`_chunk_for` 同时受**每步 token 预算**和**当前空闲 block**约束：

```python
def _chunk_for(self, seq, need, remaining):
    have_blocks = len(block_manager.get_block_table(seq))
    max_by_blocks = (have_blocks + block_manager.num_free_blocks) * block_size
    extra = max(0, max_by_blocks - seq.cached_len)
    return max(0, min(need, remaining, extra))
```

这个函数让"KV cache 放不下整个 prompt"时也能推进：宁可每次塞进能放下的一块，也不原地等待。

### 抢占（recompute 模式）

当 KV cache 满了、还有请求要进来时，调度器把**优先级最低**的运行中序列踢出去，释放它的 block。被抢占的序列保留已生成的 token，重新排到等待队列最前面，之后整体重算（`PreemptionMode.RECOMPUTE`）：

```python
def _preempt_one(self):
    seq = self.running.pop()          # 队尾（最晚准入者）
    self.block_manager.release(seq)   # 释放 KV block
    seq.cached_len = 0
    seq.phase = "PREFILL"
    self.waiting.appendleft(seq)      # 回到等待队列最前，重新 prefill
```

> 关于"优先级"的诚实说明：mini 版里 `Sequence.priority` 只是 `next(itertools.count())` 的**递增到达序号**，抢占实际是**从 `running` 队尾弹出**（LIFO，最晚准入的序列先被牺牲）——也就是"队尾 = 抢占目标"，而不是真的按权重选。真实 vLLM 的 `--priority`（`--scheduler-policy`）才支持真正的优先级调度（先到先服务 / 按 priority / longest-prefix）。mini 版保留了"抢占目标 = 队尾"这个**可实现**的子集。

一个重要细节：**刚在本步准入的序列不能被立即抢占**，否则会出现"准入一个又踢掉一个"的抖动。我们用 `_newly_admitted` 集合保护它们。

### 正确性测试

`tests/test_engine.py::test_preemption_recovers` 用"总需求远超 KV cache"的配置压测，断言所有请求最终都能完成。

### 6.7.1 错误处理与异常路径

mini-vLLM 把"KV 缓存耗尽"当作一等公民处理，而不是让它静默错乱。整套防线分三层：

**第一层：`NoFreeBlocksError`（兜底异常）**

`BlockAllocator.allocate()` 在空闲链表为空时抛出 `NoFreeBlocksError("KV cache is full (no free physical blocks)")`（`kv_cache.py`）：

```python
def allocate(self):
    if not self.free:
        raise NoFreeBlocksError("KV cache is full (no free physical blocks)")
```

这个异常在**正常调度下不会冒出来**——因为调度器在 `ensure_blocks` 之前总会先查 `can_allocate`。它真正的价值是：**当某个 bug 让调度器绕过检查时立刻炸出明确的错误**，而不是在注意力计算里读到错位的内存、默默输出一堆垃圾 token。真实 vLLM 也类似：`CacheEngine` 初始化时如果按 `gpu_memory_utilization` 算出的 block 数不足，会直接报错拒绝启动，绝不带病运行。

**第二层：调度器的优雅降级（graceful degradation）**

KV 不够时，调度器不崩溃，而是"少跑一点"：

- **decode 序列**：`can_allocate(seq, cached_len+1)` 失败 → 先 `_preempt_one()` 释放一个低优先级序列，再重试；仍失败则本步跳过该序列，下个 step 再试。decode 每步只推进 1 个 token，慢一拍不会被用户感知。
- **prefill 序列**：`_chunk_for` 用 `min(need, remaining, extra_by_blocks)` 把每步的块大小限制在**当前空闲 block 能装下的范围**，所以 KV 再紧张 prefill 也能小步推进，不会死等一整块。
- **准入新请求**：`_chunk_for(seq, prompt_remaining, remaining)` 返回 0 时（一个块都塞不下），`_preempt_one()` 先尝试踢掉一个旧序列腾地方；踢无可踢才 `break` 放弃本步准入。

**第三层：抢占保护"刚准入"的序列**

`_preempt_one` 里最重要的防抖逻辑：**本步刚准入的序列（`_newly_admitted`）永远不会被立即抢占**：

```python
for i in range(len(self.running) - 1, -1, -1):
    seq = self.running[i]
    if seq.seq_id in self._newly_admitted:
        continue          # 刚准入的跳过，避免"准入一个又踢掉一个"
```

否则会出现这样的抖动：B2 准入一个长 prompt（占了 10 个 block）→ 下一个请求来了发现不够 → 立刻把它踢掉 → 它回队首又重新 prefill…… 这种抖动会让准入形同虚设。保护它之后，抢占只针对"上一轮或更早就在跑"的序列。

此外，`_preempt_one` 还会把被抢占序列从**本步已排好的 `prefill_items` / `decode_items` 里删掉**（用列表过滤），避免引擎执行到一半发现序列已经不在 `running` 里。`_execute` 里还有双保险：`if seq.state != RUNNING: continue`。这一整套"异常不向上抛、而是转化为少跑/跳过"的思路，和真实 vLLM 调度器"宁可抢占也不 OOM"的设计同源。

---

## 6.8 采样器（`sampler.py`）

顺序与 vLLM 一致：`温度 → top-k → top-p → softmax → 采样`（每个参数的数学直觉与调参建议见第 05 章《采样与解码参数》）。

```python
def sample_token(logits, params, rng):
    if params.temperature == 0:                       # 贪心
        return argmax(logits)
    logits = apply_temperature(logits, params.temperature)
    logits = apply_top_k(logits, params.top_k)        # 保留 top-k
    logits = apply_top_p(logits, params.top_p)        # nucleus
    probs = softmax(logits)                           # 归一化
    return rng.multinomial(1, probs).argmax(-1)       # 采样
```

- `top_k`：把 logits 中低于第 k 大的值设成 `-inf`。
- `top_p`（nucleus）：从高到低累计概率，去掉累计超过 `p` 的尾部。

---

## 6.9 引擎：`LLMEngine`（`engine.py`）

引擎是唯一"有状态"的协调者，持有 `model`、`block_manager`、`scheduler`、`tokenizer`、`rng`。

### 核心循环

```python
def step(self):
    scheduled = self.scheduler.schedule()      # ① 调度：决定本步跑什么
    return self._execute(scheduled)            # ② 执行：跑模型 + 采样

def generate(self, prompt, sampling_params=None):
    self.add_request(prompt, sampling_params)  # ③ 入队
    while not self._seq_finished():
        self.step()                            # ④ 循环直到完成
```

### 一次 step 内部

`_execute` 处理两类工作：

**Prefill**：喂入 `all_ids[cached_len : cached_len+chunk]`，位置是 `[cached_len, cached_len+chunk)`。只有**本块把整个 prompt 填完**时才采样第一个输出 token；否则只存 KV，不采样。

```python
for seq, chunk in scheduled.prefill_items:
    self._run_forward(seq, tokens, positions)
    seq.cached_len += chunk
    if seq.cached_len == seq.num_tokens:      # prefill 完成
        seq.phase = "DECODE"
        seq.cached_len -= 1                   # 见下方"decode 约定"
        self._sample_and_apply(seq, seq.cached_len)
```

**Decode**：喂入 `all_ids[-1]`（上一个生成的 token），位置是 `cached_len`（= 当前长度 - 1）。`forward` 会把这个 token 的 KV 写进新 block，然后注意力覆盖 `[0, cached_len+1)`，预测下一个 token：

```python
for seq in scheduled.decode_items:
    self.block_manager.ensure_blocks(seq, seq.cached_len + 1)
    self._sample_and_apply(seq, seq.cached_len)
```

> **decode 约定（值得反复理解）**：decode 时 `cached_len == num_tokens - 1`，即"最新那个 token 还没入缓存"。模型把它作为输入，计算出它的 KV 并写入，再预测下一个。而 prefill 完成后 `cached_len == num_tokens`（全部已缓存），所以我们做一步 `cached_len -= 1` 让两个阶段统一。这样第一个 decode 会**幂等重写**最后一个 prompt token 的 KV——无害，且让代码只有一条路径。

### 6.9.1 一次 engine step 的逐步可视化：prefill → decode 过渡

把上面这套逻辑画成一张逐步表，最能看清 `cached_len` 的"填满 → 回退 1 → 继续走"。假设 `block_size = 8`、每步 token 预算很小（8 token），prompt 有 41 个字符（41 个 token），`max_tokens = 4`：

```
step   phase      输入(位置)                cached_len 变化    说明
───   ──────     ──────────────           ──────────────     ─────────────────────────────
 1    PREFILL    token[0:8]               0 → 8              只存 KV，不采样（8 < 41）
 2    PREFILL    token[8:16]              8 → 16
 3    PREFILL    token[16:24]             16 → 24
 4    PREFILL    token[24:32]             24 → 32
 5    PREFILL    token[32:40]             32 → 40
 6    PREFILL    token[40:41]             40 → 41             prefill 完成
     ▼ 过渡      cached_len -= 1 ⇒ 41 → 40                   用位置 40 采样第 1 个输出 token，
                                                             位置 40 的 KV 幂等重写一遍
 7    DECODE     token[41]（第 1 个输出）   41 → 42            写入位置 41 的 KV（首次），预测下一个
 8    DECODE     token[42]                42 → 43
 ...  DECODE     ...                      ...
```

几个关键看点：

1. **step 6 的过渡瞬间**：prefill 完成时 `cached_len == num_tokens == 41`，立刻 `-= 1` 变 40，然后 `_sample_and_apply(seq, 40)` 用**最后一个 prompt token（位置 40）**算 KV 并采样出第一个输出 token。位置 40 的 KV 在 step 6 的 prefill 里已经写过，这次是幂等重写。
2. **decode 的输入是"上一个输出 token"**：step 7 输入 `all_ids[41]`（即第 1 个输出 token），它的 KV 此刻**首次**写入（block 5 的 offset 1），`forward` 用绝对位置 41 做位置编码，注意力覆盖 `[0, 41]`，预测位置 42。
3. **block table 何时增长**：41 token ÷ block_size 8 = 6 个物理 block，step 6 时 table 已满 6 项；step 7 写入位置 41 落在第 6 个 block 内，不新增。只有 `cached_len` 跨过 48 才需要第 7 个 block。
4. **采样只在"本块把整段 context 填满"时发生**：step 1–5 只写 KV 不采样（见 `_execute` 的 `if seq.cached_len == seq.num_tokens` 分支）；采样永远紧跟 `cached_len -= 1`，保证 decode 路径只有一条。

把这张表逐行 `print` 出来（见 §6.13.1 调试指南），就能直观看到 `cached_len` 的这套节奏。

### 停止条件（`_apply_token`）

```python
if token == eos and not ignore_eos:      finish_reason = "stop"
elif seq.num_tokens >= max_model_len:    finish_reason = "length"
elif len(output_ids) >= max_tokens:      finish_reason = "length"
elif text.endswith(stop_str):            finish_reason = "stop"
```

### 前缀缓存的接入

序列结束时，如果它没有被打断，就把它的 **prompt 前缀**（前 `ceil(prompt_len / block_size)` 个 block）登记进 `PrefixCache`。之后相同前缀的请求会命中：

```python
def _finish(self, seq):
    if self.config.enable_prefix_caching and not seq.is_preempted:
        self.block_manager.register_prefix(seq)
    self.block_manager.release(seq)
    seq.state = FINISHED
```

`tests/test_engine.py::test_prefix_caching_reuses_blocks` 验证了第二个相同 prompt 能命中完整前缀。

---

## 6.10 OpenAI 兼容服务器（`async_engine.py` + `api_server.py`）

### AsyncLLMEngine：引擎与 Web 框架解耦

vLLM 的在线服务里，`AsyncLLMEngine` 用一个**专用的事件循环/线程**跑 engine step，Web 框架只负责收发。mini-vLLM 用后台线程 + 锁实现同样效果：

```python
class AsyncLLMEngine:
    def _run_loop(self):                      # 后台线程
        while not self._stop:
            with self._lock:
                if self._engine.has_pending():
                    outputs = self._engine.step()   # 每步一个 batch
                    for o in outputs:
                        self._streams[o.request_id].put(o)   # 按请求分发
            if not pending:
                time.sleep(0.001)

    async def stream(self, prompt, sampling_params):
        with self._lock:
            seq_id = self._engine.add_request(prompt, sampling_params)
            self._streams[seq_id] = q          # 先注册队列，避免丢输出
        while True:
            o = await asyncio.to_thread(q.get) # 不阻塞事件循环
            yield o
            if o.finished: break
```

### FastAPI 端点

`api_server.py` 实现了 `GET /health`、`GET /v1/models`、`POST /v1/completions`、`POST /v1/chat/completions`。流式走 SSE：

```python
async def _chat_stream(engine, prompt, params):
    prev_text = ""
    async for o in engine.stream(prompt, params):
        delta = o.outputs[0].text[len(prev_text):]   # 累计文本求差量
        prev_text = o.outputs[0].text
        chunk = {"choices": [{"delta": {"content": delta}, "finish_reason": ...}]}
        yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"
```

---

## 6.11 训练脚本（`training.py` + `scripts/train.py`）

vLLM 不训练模型，它**加载**预训练模型。但为了让教程完全自包含，我们用一个纯 NumPy 的反向传播把 TinyGPT 在语料上训练了几百步，产出可用的权重。这是标准的 GPT-2 训练循环：因果注意力 + MLP + LayerNorm + 权值绑定的 word embedding。

```bash
python scripts/train.py --steps 400 --embd 96 --layers 3 --out artifacts/tinygpt
# [train] step 400/400 loss ...   checkpoint saved
```

训练不是重点，重点是它证明了模型前向（含 PagedAttention）的数值正确：loss 能从随机的 `ln(97)≈4.57` 稳步降到 `≈2`。

---

## 6.12 量化与投机解码演示

### 量化（`quantize.py`）

真实 vLLM 用 GPTQ/AWQ/FP8 等，核心是**权重低精度存储 + 反量化/融合 kernel 计算**。mini-vLLM 实现数据层面的一半——对称逐列 int8 量化：

```python
def quantize_matrix(w):
    scale = np.max(np.abs(w), axis=0) / 127.0     # 每输出列一个 scale
    q = np.round(w / scale).astype(np.int8)
    return q, scale
```

然后把反量化后的权重加载到新引擎做生成对比，报告压缩率与最大误差：

```bash
python -m minivllm quant --model artifacts/tinygpt
# original : 1467264 bytes
# quantized:  374784 bytes (0.26x)
# max abs error: 0.00391
```

> **压缩率 0.26x 说明什么？** int8 权重量化的理论极限是 0.25x（`1/4 = 0.25`，fp32→int8）；0.26x 意味着量化面覆盖了全部权重矩阵，只多出逐列 scale 的 4 字节开销。max abs error 只有 `0.00391`（≈ 1/127.5 的量化步长），所以两套权重的生成结果几乎一致。实现上，checkpoint 的 `state_dict` 展平后，逐层权重是 `(n_layers, in, out)` 的 3 维数组；`quantize_state_dict` 对每一层**分别**做逐输出列量化（见 `minivllm/quantize.py`），scale 用 `(L, 1, out)` 的形状直接广播回原张量。真实大模型以 2D 权重为主（且 int4/int8 走 Marlin 内核，见第 09 章），压缩率同样接近理论值。

### 投机解码（`speculative.py`）

用一个**字符 bigram 草稿模型**先提出 K 个 token，再让目标模型一次 forward **并行验证**，只保留最长匹配前缀 + 一个 bonus token：

```bash
python -m minivllm spec --model artifacts/tinygpt
# draft tokens: 68, accepted: 16 (23.5%), target forwards: 28, steps: 14
```

**accept 逻辑走读**（`speculative_generate` 的核心，比"草稿→验证"多两个细节）：

1. **并行验证**：draft 提出 K 个 token 后，把"当前上下文 + K 个 draft token"一次性喂给目标模型做一次 forward，得到每个位置的目标分布。因为推理是**因果**的，第 j 个位置只看前 j−1 个 draft token，所以一次 forward 就能得到 K 个位置的独立验证分布——这就是"一次验证多个"的合法性来源；
2. **逐位接受 + bonus**：从位置 0 开始，用修正拒绝采样（第 02 章 §8.1）决定接受还是拒绝：接受就继续看下一位，第一个被拒绝的位置 m 处停止，输出该位置按补充分布重新采样的 **bonus token**，总共接受 m 个 draft + 1 个 bonus；
3. **KV 修复**：draft token 在验证 forward 里已经写进了分页 KV cache；被拒绝位置之后的 KV 是"废的"，要**回滚**（mini 版里 `speculative_generate` 在结束时会用正确 token 重写该位置的 KV，保证下一轮 decode 读到的是真实轨迹）。

这个 demo 展示的是**完整算法**，但 bigram 草稿太弱，`accepted: 16 (23.5%)` 说明接受率低、加速比 < 1（不划算）——这正好呼应第 02 章 §8 的结论"接受率低时投机没有价值"。真实的 EAGLE/Medusa 用更强的 draft 才能真正加速。

---

## 6.13 验证

| 测试 | 验证内容 |
|------|---------|
| `tests/test_paged_attention.py` | PagedAttention 输出与稠密注意力数值一致 |
| `tests/test_engine.py` | 确定性、批处理等价性、分块 prefill 等价性、前缀缓存、抢占、流式 |
| `tests/test_server.py` | 服务器四个端点 + SSE 流式 |

```bash
cd vllm-toturial/mini-vllm
python tests/test_paged_attention.py
python tests/test_engine.py
python tests/test_server.py
python -m minivllm demo --model artifacts/tinygpt   # 连续批处理可视化
```

**关键不变量**：

1. **分块 prefill 等价**：`test_chunked_prefill_matches_full` 断言用超小 token 预算（每步 6 token）生成的 token 序列和一次全量 prefill **完全相同**。
2. **批处理等价**：`test_batching_equivalent_to_solo` 断言"两个请求一起跑"的输出 == "各自单独跑"的输出（固定 seed 下）。
3. **抢占收敛**：`test_preemption_recovers` 断言在严重超订的 KV cache 下所有请求最终完成。

> **测试覆盖边界（诚实说明）**：`tests/` 目前只有 3 个文件（paged_attention / engine / server）。§6.14.1 表格里给 sampler、COW、checkpoint、量化、投机列出的"不变量"，前三个由现有测试间接覆盖，后几个（量化误差、投机无损、前缀引用计数回收）**还没有专门的测试守护**——这是刻意留给你的作业：第 18 章附录 C 的扩展练习 2（SWAP 抢占）、5（前缀命中率指标）、7（采样惩罚项）做完时，顺手各补一个不变量测试，就把它们焊死了。

### 6.13.1 动手跑一遍 + 调试指南

mini-vLLM 是纯 Python，`print` 和 `pdb` 断点都能直接下，不需要任何 GPU 工具链。下面示范怎么把"调度/引擎到底发生了什么"看个明白。

**想看的三个核心状态**

| 状态 | 怎么读 | 含义 | 常见疑惑 |
|---|---|---|---|
| `seq.cached_len` | 读 `Sequence.cached_len` | 已入 KV 缓存的 token 数 | decode 时它恒等于 `num_tokens - 1`；prefill 完成瞬间等于 `num_tokens`，随后 `-= 1` |
| `block_manager.get_block_table(seq)` | 返回 `List[int]` | 该序列的"逻辑块 → 物理块"映射 | 长度 = `ceil(cached_len / block_size)`，decode 每步不一定变 |
| `scheduler.running` | 返回 `List[Sequence]` | 正在运行的序列（按准入顺序） | **队尾 = 最晚准入 = 抢占目标**（mini 版 LIFO；真实 vLLM 的优先级语义见 §6.7） |

**加 print 的推荐位置**

在 `engine.py::_execute` 里加两行临时打印（看完删掉）：

```python
def _execute(self, scheduled):
    for seq, chunk in scheduled.prefill_items:
        print(f"[prefill] seq={seq.seq_id} chunk={chunk} "
              f"cached_len={seq.cached_len} -> {seq.cached_len + chunk} "
              f"table={self.block_manager.get_block_table(seq)}")
        ...
    for seq in scheduled.decode_items:
        print(f"[decode ] seq={seq.seq_id} "
              f"cached_len={seq.cached_len} tokens={seq.num_tokens} "
              f"table={self.block_manager.get_block_table(seq)}")
        ...
```

想看调度器视角，在 `scheduler.py::schedule` 的 `return step` 前加一行：

```python
print(f"[schedule] running={[(s.seq_id, s.phase, s.cached_len) for s in self.running]} "
      f"waiting={[s.seq_id for s in self.waiting]} "
      f"free_blocks={self.block_manager.num_free_blocks}")
```

**构造一个能复现的迷你场景**（小 block + 小 token 预算，逼出分块 prefill）：

```python
from minivllm.engine import LLMEngine
from minivllm.checkpoint import load_checkpoint
from minivllm.config import SamplingParams

model, cfg, tok = load_checkpoint("artifacts/tinygpt")
cfg.cache.block_size = 8                    # 更小的物理块，分页看得更清
cfg.scheduler.max_num_batched_tokens = 8    # 小预算 → 强制分块 prefill
e = LLMEngine(cfg, tokenizer=tok)
e.add_request("the quick brown fox", SamplingParams(temperature=0, max_tokens=4))  # 19 字符
for _ in range(4):
    e.step()
```

**样例调试输出（节选）与解读**

```
[schedule] running=[] waiting=[0] free_blocks=64
[prefill]  seq=0 chunk=8 cached_len=0 -> 8   table=[12]
[schedule] running=[(0,'PREFILL',8)] waiting=[] free_blocks=63
[prefill]  seq=0 chunk=8 cached_len=8 -> 16  table=[12, 7]
[schedule] running=[(0,'PREFILL',16)] waiting=[] free_blocks=62
[prefill]  seq=0 chunk=3 cached_len=16 -> 19 table=[12, 7, 41]
[schedule] running=[(0,'DECODE',19)] waiting=[] free_blocks=61
[decode ]  seq=0 cached_len=19 tokens=20     table=[12, 7, 41]
```

解读：

1. **准入只发生在 B2**：第一行 `running=[]`，第一步把 `waiting` 里的 seq=0 接进来。因为 token 预算只有 8，`_chunk_for` 返回 `min(19, 8, 512) = 8`，所以一次只 prefill 8 个 token。
2. **`PREFILL` 阶段 `cached_len` 只涨不采样**：前三步分别推进 8、8、3 个 token；到第 3 步 `cached_len` 到达 19（等于 `num_tokens`），才转 `DECODE` 并采样第一个输出 token（`tokens` 从 19→20）。
3. **decode 约定**：第 4 步 `[decode] seq=0 cached_len=19 tokens=20` —— `cached_len == num_tokens - 1`，正是 §6.9 说的约定。
4. **`table` 增长节奏**：19 token ÷ 8 = 3 个物理块（`[12, 7, 41]`），前 3 步各新增一块；`free_blocks` 从 64 → 63 → 62 → 61。
5. **多请求时**：如果再加第二条请求，`running` 里会出现 `(seq=1, 'PREFILL', ...)`，`[schedule]` 行能看到两条序列交错——`cached_len` 各自独立推进，互不影响，这就是连续批处理。
6. **如果 `table` 里出现重复物理块号**（如 `[11, 28, 11]`），说明前缀缓存 / COW 把同一物理块共享给了两条序列——这是正常现象，不是 bug。

**用 `pdb` 看抢占**：`test_preemption_recovers` 是压测抢占最直接的入口。运行 `python -m pdb tests/test_engine.py`，在 `scheduler.py` 的 `_preempt_one` 里下断点，观察 `running` 队尾是谁、`free_blocks` 何时归零、被抢占序列的 `is_preempted` 怎么被置位——这是理解"抢占保护新准入序列"（§6.7.1）最快的路径。

---

## 6.14 mini-vLLM 与真实 vLLM 对应表

| 概念 | 真实 vLLM | mini-vLLM |
|------|-----------|-----------|
| 分页 KV cache | `vllm/worker/model_runner.py` + CUDA | `kv_cache.py` + NumPy |
| Block table | GPU 上的 `block_tables` 张量 | `block_tables[seq_id]` 列表 |
| PagedAttention kernel | Triton/CUDA `paged_attn.py` | `attention.py::paged_attention_batch` |
| 连续批处理 | `vllm/core/scheduler.py` | `scheduler.py` |
| 抢占 | `PreemptionMode.SWAP/RECOMPUTE` | `_preempt_one()`（recompute） |
| 前缀缓存 | V2 block manager + hash | `PrefixCache` |
| 分块 prefill | `SchedulerConfig.enable_chunked_prefill` | `_chunk_for()` |
| 引擎 | `LLMEngine` / `AsyncLLMEngine` | `engine.py` / `async_engine.py` |
| OpenAI 服务器 | `vllm/entrypoints/openai/` | `api_server.py` |
| 量化 | GPTQ/AWQ/FP8/GGUF kernels | `quantize.py`（int8 数据层） |
| 投机解码 | `vllm/spec_decode/` | `speculative.py`（bigram 草稿） |

### 6.14.1 每个模块：先读哪个函数、维护什么不变量

上面的表看"概念对应"，这张表看"人怎么上手"——每个 `minivllm/` 文件，第一行该读哪个函数，读完它能验证哪个**不变量**：

| mini-vLLM 文件 | 对应真实 vLLM | 首读函数/入口 | 它维护的不变量 |
|---|---|---|---|
| `config.py` | `vllm/config.py` | `ModelConfig` / `SchedulerConfig` dataclass | 所有参数收敛于配置对象，无魔法数字散落 |
| `tokenizer.py` | `transformers.PreTrainedTokenizer` | `CharTokenizer.encode` / `decode` | `decode(encode(t)) == t`（字符级往返）；`eos_token_id` 恒定 |
| `sequence.py` | `vllm/sequence.py` | `Sequence` dataclass | `cached_len` 约定；`all_ids == prompt_ids + output_ids`；`num_tokens == len(all_ids)` |
| `kv_cache.py` | `vllm/core/block_manager_v1.py` | `BlockManager.write_kv` / `ensure_blocks` / `release` | 引用计数 ≥ 0；共享 block 写入前必 COW；release 后 block 一定回空闲表 |
| `scheduler.py` | `vllm/core/scheduler.py` | `Scheduler.schedule` | 一个 step 产出稳定 batch；调度不影响正确性；`_newly_admitted` 不被立即抢占 |
| `attention.py` | `vllm/attention/ops/paged_attn.py` | `paged_attention_batch` / `gather_kv` | 分页注意力输出 == 稠密注意力（`allclose`） |
| `model.py` | `vllm/model_executor/models/gpt2.py` | `TinyGPT.forward` | KV cache 在模型外部；同一 forward 同时服务 prefill 与 decode |
| `sampler.py` | `vllm/model_executor/layers/sampler.py` | `sample_token` | 处理顺序 温度→top-k→top-p→softmax→采样；temperature=0 时确定性 |
| `engine.py` | `vllm/engine/llm_engine.py` | `LLMEngine.step` / `_execute` | 每个 step 恰好 schedule 一次、execute 一次；finished 序列必走 `_finish` |
| `async_engine.py` | `vllm/engine/async_llm_engine.py` | `_run_loop` | 后台线程持锁推进引擎；每个请求一个队列，先注册后放输出 |
| `api_server.py` | `vllm/entrypoints/openai/` | `_chat_stream` | 流式 delta = 累计文本求差（前缀递增，可还原） |
| `cli.py` | `vllm` CLI | `main` / 各子命令 | 库 + 应用双形态；CLI 参数与库参数一致 |
| `training.py` | 无（vLLM 不训练） | 训练循环 | 前向（含 PagedAttention）数值正确——loss 从 ~4.57 降到 ~2 |
| `quantize.py` | GPTQ/AWQ/FP8 kernels | `quantize_matrix` | 对称逐列 int8；反量化后与原始权重误差有界 |
| `speculative.py` | `vllm/spec_decode/` | `speculative_generate` | 草稿→验证→接受/拒绝 无损；验证复用同一分页 KV cache |
| `checkpoint.py` | HF `save_pretrained` / `torch.load` | `load_checkpoint` | state_dict 可保存/加载往返；配置与权重一起序列化 |
| `data.py` | 训练语料 | `CORPUS` | 自包含、可复现 |

> 使用建议：想理解"数据布局"先读 `kv_cache.py`；想理解"流程"先读 `engine.step`；想理解"性能/正确性权衡"先读 `scheduler.schedule`。三者的不变量分别由 `test_paged_attention.py`、`test_engine.py`、`test_server.py` 焊死。

### 6.14.2 支撑模块走读：sequence / checkpoint / cli / data

上面表格里没有展开的四个"支撑模块"，一次讲清：

**`sequence.py` —— 贯穿全系统的数据结构**：`Sequence`（一条生成单元，`prompt_ids + output_ids` 拼成 `all_ids`，`cached_len` 约定见 §6.9）、`CompletionOutput`（单条输出，`delta()` 用于流式求增量）、`RequestOutput`（引擎产出，`from_sequence` 组装）。它还定义了四个状态常量 `WAITING / RUNNING / FINISHED / PREEMPTED`——**注意**：源码里 `PREEMPTED` 常量实际未被使用（抢占用 `is_preempted` 标志位表示），它是为"语义完整"保留的。

**`checkpoint.py` —— 可序列化的"模型 + 配置 + tokenizer"**：`save_checkpoint` 把 TinyGPT 的 `state_dict`（嵌套字典）**展平**成 `a/b/c` 式的键再存成 `model.npz`，同时写 `config.json`（`EngineConfig.to_json`）与 `tokenizer.json`；`load_checkpoint` 反展平还原。它模仿的是 HF 的 `save_pretrained / from_pretrained`——目录就是一份可独立分发的模型仓库。

**`cli.py` —— 库 + 应用双形态**：`python -m minivllm <serve|chat|demo|spec|quant>` 的入口，参数（`--model` / `--port` / `--temperature`…）故意模仿 `vllm` 命令。它只做"解析参数 → 调用库 API"，不含业务逻辑——这正是 §6.15.1"服务层薄封装"的体现。

**`data.py` —— 自包含语料**：一段关于 vLLM 的结构化文本，重复 `REPEAT` 次构成训练集。它让整个仓库"不需要任何外部数据也能训练"，是"可复现"的最小闭环。

---

## 6.15 工程化视角：mini-vLLM 为什么是一个"工程"项目

mini-vLLM 不只是"能跑的玩具代码"，它刻意按真实工程的规范组织，这正是它作为教学代码的价值所在。拆开看有五点：

### 6.15.1 模块分层遵循单一职责

```
mini-vllm/minivllm/
├── 配置层   config.py        # 所有参数收敛为 dataclass，杜绝魔法数字散落各处
├── 数据层   tokenizer.py / sequence.py / data.py
├── 核心层   kv_cache.py / scheduler.py / attention.py / sampler.py
│            # 每份文件只干一件事：管内存的不管调度，管调度的不管采样
├── 模型层   model.py         # 与核心层解耦：模型只认"block table + KVStore"
├── 引擎层   engine.py / async_engine.py   # 唯一有状态的协调者
└── 服务层   api_server.py / cli.py        # 薄封装，不含业务逻辑
```

真实 vLLM 也是这个分层：`vllm/config/`、`vllm/core/`、`vllm/model_executor/`、`vllm/engine/`、`vllm/entrypoints/`。**"把代码组织成职责单一的文件"本身就是工程化的第一课。**

### 6.15.2 测试即文档：三个关键不变量

`tests/` 里的测试不是凑数的，它们验证的是系统最容易被破坏的**不变量**：

| 测试 | 验证的不变量 | 为什么重要 |
|---|---|---|
| `test_paged_attention.py` | 分页注意力 == 稠密注意力（`allclose`） | 分页只是内存布局，**不允许**改变数值结果 |
| `test_batching_equivalent_to_solo` | 一起跑 == 单独跑 | 连续批处理只影响性能，**不允许**影响正确性 |
| `test_chunked_prefill_matches_full` | 分块 prefill == 全量 prefill | 分块只是调度策略，**不允许**改变 token 序列 |
| `test_preemption_recovers` | 超订下所有请求最终完成 | 抢占不能丢请求，必须收敛 |
| `test_streaming_yields_tokens` | 流式文本是前缀递增的 | SSE 增量 = 累计文本求差，增量必须可还原 |

> 这几个不变量对应第 07 章的核心论点："**调度只影响吞吐，不影响正确性**"。测试把它们焊死，改动任何一块都不会悄悄打破这个承诺。

### 6.15.3 打包与 CLI：可安装、可运行

`pyproject.toml` 声明了依赖（仅 `numpy` / `fastapi` / `uvicorn`）和 CLI 入口：

```toml
[project.scripts]
minivllm = "minivllm.cli:main"
```

于是 `pip install -e .` 之后可以直接 `minivllm serve / chat / demo / spec / quant`。CLI 参数（`--model` / `--port` / `--temperature`…）故意模仿 `vllm` 命令，**同一套代码既能被 `import` 调用，也能被命令行驱动**——这是"库 + 应用"双形态的标准做法。

### 6.15.4 数据流总览：一次 engine step 的旅程

把所有模块串起来，一次 `step()` 是这条链：

```
add_request(prompt)
  → tokenizer.encode()            tokenizer.py
  → Sequence 入等待队列           sequence.py / scheduler.py
  → Scheduler.schedule()          决定 prefill/decode 批次、分块、抢占
      ├─ match_prefix()            命中 → attach 共享 block（前缀缓存）
      └─ ensure_blocks()           分配物理 block，更新 block table
  → model.forward()               model.py
      ├─ write_kv()                写 KV（共享 block 先 COW）  kv_cache.py
      └─ paged_attention_batch()   按 block table 抓取散落 KV    attention.py
  → sample_token()                 sampler.py   温度→top-k→top-p→softmax→采样
  → _apply_token()                 停止条件判断 → _finish() → 释放 block
  → RequestOutput.from_sequence()  组装输出，返回给调用方
```

这条链和真实 vLLM 的 `schedule → execute(model) → sample → process`（第 03 章）逐段对应。

### 6.15.5 可复现的验证记录

本教程撰写时，在纯 CPU 环境（Python 3.14 + NumPy 2.x）实际跑过全部验证：

```bash
$ python tests/test_paged_attention.py
test_paged_attention: OK

$ python tests/test_engine.py
test_engine: OK

$ python tests/test_server.py        # 4 个端点 + SSE 流式
test_server: OK

$ python -m minivllm demo --model artifacts/tinygpt
[cli] done in 30 engine steps        # 4 路并发交错生成

$ python -m minivllm spec --model artifacts/tinygpt
draft tokens: 68, accepted: 16 (23.5%), target forwards: 28, steps: 14

$ python -m minivllm quant --model artifacts/tinygpt
original: 1467264 bytes, quantized: 374784 bytes (0.26x), max abs error: 0.00391
```

你也可以在任意子命令上加 `--quantize int8`（如 `python -m minivllm serve --model artifacts/tinygpt --quantize int8`）模拟"以 int8 引擎加载模型"。

---

## 6.16 如何扩展 mini-vLLM：把工程化落到行动

理解一个系统最好的方式，是**给它加一个真实 vLLM 有、而 mini 版没有的特性**。下面先列出 4 个有代表性的由浅入深的动手方向（完整版共 9 个练习，带实现思路、关键代码位置与验收标准，见第 18 章附录 C §7）：

| 练习 | 要加的特性 | 对应真实 vLLM | 难度 |
|---|---|---|---|
| 1 | 采样器加 `min_p` 过滤 | `vllm/model_executor/layers/sampler.py` | ★☆☆ |
| 2 | 实现 SWAP 抢占（现在是 recompute-only） | `vllm/core/scheduler.py` | ★★☆ |
| 3 | 实现 `n`（一个 prompt 生成多条序列） | `vllm.SamplingParams(n=...)` | ★★☆ |
| 4 | 实现 embedding 模式与 `/v1/embeddings` | `vllm --task embed` | ★★★ |

每个练习的验收标准都建议配一个**不变量测试**——比如 SWAP 抢占的验收是"超订 KV cache 下所有请求最终完成"（和 `test_preemption_recovers` 同样的承诺，只是实现换成了换出/换回）。

> 做完练习 2 和 3，你就给 mini-vLLM 加了两个真实 vLLM 的核心特性。第 18 章附录 C 的 §9 还给了"从 mini 到真实 vLLM"的四步演进地图（换内核 → 加显存规划 → 加并行 → 加部署）。

---

## 6.17 工程化检查清单

用这份清单评判任何"类 serving 引擎"代码（完整版见第 18 章附录 C §5）：

- [ ] **模块分层**：配置 / 数据 / 核心 / 模型 / 引擎 / 服务各司其职，依赖无环？
- [ ] **参数收敛**：没有魔法数字散落，配置对象可序列化？
- [ ] **测试焊死不变量**：等价性 / 收敛性等核心承诺有测试守护？
- [ ] **双形态**：同一套代码既可 `import` 又可命令行驱动？
- [ ] **可复现**：seed 从顶到底贯穿，固定 seed 输出确定？
- [ ] **错误处理**：KV 耗尽抛明确异常，而非静默错乱？
- [ ] **资源回收**：序列结束 / 被抢占时 block 一定释放？

---

## 6.18 本章小结

- mini-vLLM 用纯 NumPy 完整复刻了 vLLM 的**数据布局**、**调度逻辑**与**服务形态**。
- 最核心的三件事：**分页 KV cache**、**连续批处理**、**调度与执行解耦**。
- 通过测试证明了正确性，包括分块 prefill 和批处理的不变性。
- 工程化体现在：模块分层、测试即文档、可打包可安装、CLI 与库双形态。
- 量化与投机解码以"演示"级别覆盖，理解了它们就理解了真实实现的方向。

下一章《07-调度与连续批处理.md》深入调度器：为什么连续批处理能大幅提升吞吐，抢占与分块 prefill 是怎么配合的。
