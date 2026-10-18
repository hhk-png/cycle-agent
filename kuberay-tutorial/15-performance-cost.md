# 第 15 章 性能、容量与成本

Ray 集群的性能问题经常被误判为“KubeRay 慢”，实际根因可能在对象存储、镜像拉取、GPU 利用率、调度错配或 Serve 副本。本章给出一套从 Pod 到 Ray 再到成本的排查框架。

## 15.1 性能分层

| 层 | 关注点 | 工具 |
|---|---|---|
| 节点 | CPU/GPU/内存/磁盘/网络 | node-exporter、DCGM、nvidia-smi |
| Pod | 资源请求/限制、调度、重启 | kubectl、metrics-server |
| Ray | task/actor 排队、对象存储、GCS | Ray Dashboard、Prometheus |
| 应用 | Python 热点、序列化、锁 | py-spy、cProfile、OTel |
| Serve | 队列、延迟、副本 | Serve metrics、Grafana |
| 控制面 | reconcile、API 限流 | operator metrics |

## 15.2 共享内存与对象存储

### 15.2.1 `/dev/shm`

- 容器默认 64MB，Ray 对象存储需要更多；
- KubeRay 默认挂载 memory emptyDir，sizeLimit 由 Ray 容器 memory request/limit 推导；MiniKubeRay 固定 2Gi 仅作模拟；
- 大对象训练需要增大 `sizeLimit`；
- `sizeLimit` 会计入节点内存，注意 OOM；
- 设置 `plasma-directory` 可改用磁盘，但会慢。

### 15.2.2 object store memory

`rayStartParams.object-store-memory` 控制对象存储上限。太小：

- 频繁 spill 到磁盘；
- 任务变慢；
- 可能 OOM。

太大：

- 占用节点内存；
- 影响 worker 进程。

建议从容器可用内存（memory limit/request）的 30% 左右开始，而不是整台节点的内存；再根据对象大小/溢出率调整。`/dev/shm` 的容量与 Ray 的 `object-store-memory` 要一起看。

### 15.2.3 对象大小

- 大对象跨节点传输会成为瓶颈；
- 尽量让数据本地化；
- 用 Ray Data 的分块/流式处理；
- 避免在 driver 里 `ray.get` 大量对象；
- 用 `ray.put` 共享只读数据。

## 15.3 CPU/GPU 资源

### 15.3.1 对齐

- Pod requests = Ray 可见资源；
- GPU 请求 = 容器 GPU limit；
- 不要超卖 GPU（除非用 MIG/共享方案）；
- CPU limit 过紧会导致节流。

### 15.3.2 GPU 利用率

低利用率的常见原因：

- 数据加载是瓶颈（CPU/IO）；
- batch size 太小；
- 副本/worker 不足；
- 模型并行通信瓶颈；
- GPU 型号混用；
- Ray task 粒度过细。

工具：

- `nvidia-smi`/DCGM；
- Ray Dashboard 的 GPU 指标；
- PyTorch profiler；
- Nsight Systems。

### 15.3.3 拓扑

- GPU 训练尽量同机/同 NVLink；
- 多机训练注意 IB/RoCE；
- 跨可用区带宽/延迟；
- `topologySpreadConstraints` 打散或聚合要按业务选择。

## 15.4 镜像与启动时间

- 镜像越大，扩容越慢；
- 预装依赖，减少 runtime env pip install；
- 使用镜像缓存/预热；
- 避免 init container 做重活；
- 固定镜像 digest，避免 tag 漂移；
- GPU 镜像与驱动兼容。

## 15.5 调度与装箱

- `requests` 决定调度，`limits` 决定运行时上限；
- 装箱过紧会导致碎片；
- 大 GPU Pod 可能因为碎片无法调度；
- 用 node pool 按机型隔离；
- 用 priority/affinity 控制放置；
- 监控 Pending Pod 的原因。

## 15.6 Ray 配置

### 15.6.1 常用环境变量

| 变量 | 作用 |
|---|---|
| `RAY_object_store_memory` | 对象存储上限 |
| `RAY_gcs_rpc_server_reconnect_timeout_s` | worker 重连 GCS 的超时 |
| `RAY_TASK_MAX_RETRIES` | task 重试（仅系统故障；应用异常需 `retry_exceptions`） |
| `RAY_SERVE_KV_TIMEOUT_S` | Serve KV 超时 |
| `RAY_DASHBOARD_ENABLE_K8S_DISK_USAGE` | Dashboard 磁盘指标 |

### 15.6.2 Serve 调优

- `num_replicas` / `autoscaling_config`；
- `target_num_ongoing_requests_per_replica`；
- `max_concurrent_queries`；
- `ray_actor_options.num_cpus/num_gpus`；
- 模型加载时间；
- 批次推理（dynamic batching）。

## 15.7 成本模型

### 15.7.1 成本构成

```text
总成本 =
  GPU/CPU 节点成本
+ 存储成本（PVC/对象存储）
+ 网络成本（跨区/出网）
+ 控制面成本（operator/监控）
+ 闲置成本（长期空转的开发集群）
```

### 15.7.2 降本手段

- RayJob 完成后删除集群；
- autoscaler 缩容；
- suspend 开发集群；
- 多 group priority 优先便宜机型；
- GPU 共享/MIG（如果业务允许）；
- Spot/Preemptible（配合 checkpoint）；
- 对象存储生命周期；
- 镜像/层缓存；
- 监控闲置 GPU。

### 15.7.3 Spot 实例

- 适合可重试/可 checkpoint 的训练；
- 需要处理中断；
- Ray 的 task/actor 重试要配置；
- KubeRay 的 Pod 重建会重新拉镜像；
- 不要在有状态 Serve 上无保护使用。

### 15.7.4 垂直弹性省的是什么

IPPR（第 57 章）不会凭空增加节点容量：它把已有 Pod 的 requests 调大，前提是节点还有 allocatable 余量。它省下的是：

- 新 Pod 的镜像拉取、Raylet 启动和节点注册；
- 多份 sidecar/对象存储/共享内存预留；
- 跨节点对象传输和更碎的调度；
- 启动慢的模型 worker 的冷启动。

它**不省**节点账单：Pod 变大后仍然占节点的 CPU/memory；如果节点没有余量，resize 失败后还是要走水平扩容 + Cluster Autoscaler。另一个成本陷阱是“只扩不缩”：高峰期变大的 Pod 不会自动变小，需要水平缩容或重建 Pod 才能把资源还回去。容量规划时把 IPPR 当作“减少 Pod 数量/启动开销”的手段，而不是“减少总资源”的手段。

## 15.8 容量规划

1. 统计业务峰值 CPU/GPU/内存；
2. 估算 Ray 对象存储/网络；
3. 留出 autoscaler 扩容缓冲；
4. 为 RayService 升级预留 2 倍或 `maxSurgePercent`；
5. 为 Kueue/批调度器留队列配额；
6. 监控 Pending Pod 和扩容失败；
7. 定期压测。

## 15.9 性能排查顺序

1. **Pod 是否 Ready？** 不是 -> 调度/镜像/资源；
2. **Ray 是否看到资源？** 不是 -> num-cpus/num-gpus 对齐；
3. **任务是否排队？** 是 -> autoscaler/容量；
4. **对象存储是否 spill？** 是 -> 增大内存/减少对象；
5. **GPU 利用率低？** -> 数据/批大小/通信；
6. **Serve 延迟高？** -> 副本/队列/模型；
7. **控制面慢？** -> reconcile 错误/API 限流。

## 15.10 可复现的性能方法论

不要用“感觉慢”做优化。一个可复现的测量流程：

### 15.10.1 定义指标

| 负载 | 指标 |
|---|---|
| Ray Data ETL | rows/s、MB/s、任务失败率、对象存储 spill |
| Ray Train | samples/s、step time、GPU 利用率、checkpoint 时间 |
| Ray Tune | trials/min、GPU 利用率、排队时间 |
| Ray Serve | RPS、P50/P95/P99 延迟、TTFT、TPOT、队列深度 |
| 控制面 | RayCluster 创建到 Ready、reconcile 延迟、API 限流 |

### 15.10.2 固定变量

- 镜像 digest、Ray/KubeRay 版本；
- 节点机型/GPU 型号/网络；
- 数据规模/分布；
- 并发/批大小；
- 副本数/autoscaler 配置；
- 测量时长/预热时间。

### 15.10.3 压测脚本

```python
# serve_load.py: 固定并发，记录 TTFT/TPOT
import asyncio, time, statistics, aiohttp

URL = "http://127.0.0.1:8000/v1/chat/completions"
PAYLOAD = {"model": "my-model", "messages": [{"role": "user", "content": "hello"}], "stream": True}

async def one(session, latencies):
    start = time.perf_counter()
    first = None
    async with session.post(URL, json=PAYLOAD) as response:
        async for line in response.content:
            if first is None and line.strip():
                first = time.perf_counter()
    end = time.perf_counter()
    latencies.append((first - start, end - start))

async def main(concurrency=32, total=200):
    latencies = []
    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(concurrency)
        async def run():
            async with sem:
                await one(session, latencies)
        await asyncio.gather(*(run() for _ in range(total)))
    ttft = [item[0] for item in latencies]
    e2e = [item[1] for item in latencies]
    print("TTFT p50/p95:", statistics.median(ttft), sorted(ttft)[int(len(ttft)*0.95)])
    print("E2E  p50/p95:", statistics.median(e2e), sorted(e2e)[int(len(e2e)*0.95)])

asyncio.run(main())
```

### 15.10.4 逐步加压

1. 1 并发，测基线；
2. 逐步加到 2/4/8/16/32/64；
3. 记录延迟/吞吐/错误率；
4. 找到 SLO 拐点；
5. 观察 GPU KV cache/队列；
6. 用同一脚本对比不同配置/版本。

### 15.10.5 成本测量

- OpenCost/Kubecost 按 namespace/label 分摊；
- GPU 小时 = GPU 数 × 运行时长；
- 记录“请求资源”和“实际使用”；
- 统计闲置时间（扩容后无任务）；
- 把成本指标和业务指标放同一张看板。

## 15.11 MiniKubeRay 的性能视角

简版实现不模拟真实计算性能，但保留：

- 调度器的资源拟合（CPU/GPU/内存）；
- runtime 的节点资源与 demand；
- autoscaler 的扩缩容决策；
- 指标和事件。

可以用它验证“资源请求变化 -> 调度/扩容决策”的控制面逻辑，但不能用来测真实吞吐。

## 15.12 容量公式与压测模板

### 15.12.1 批处理/训练

```text
所需副本 = max_r ceil(demand[r] / capacity_per_replica[r])
capacity_per_replica[r] = 单 Pod 的 r × numOfHosts
```

如果目标是 80% 利用率：

```text
所需副本 = ceil(demand / (capacity × 0.8))
```

### 15.12.2 在线推理

```text
Little's Law: L = λ × W
副本数 ≈ ceil(λ × W / target_concurrency_per_replica)
```

其中：

- `λ`：到达率（req/s）；
- `W`：SLO 延迟（s）；
- `target_concurrency_per_replica`：单副本目标并发。

### 15.12.3 GPU 显存

```text
权重 ≈ 参数量 × bytes_per_param
KV cache ≈ 2 × layers × num_kv_heads × head_dim × context × batch × bytes
总显存 ≈ 权重 + KV cache + 激活/碎片
```

`2` 表示 K 和 V 两份；`num_kv_heads` 不等于 `num_attention_heads` 时就是 GQA/MQA。用 `hidden` 代替 `num_kv_heads × head_dim` 会把 GQA 模型的 KV cache 高估数倍。

例：Llama-3.1-8B（32 层、8 个 KV head、head_dim 128、FP16、4K context、batch=1）
= 2 × 32 × 8 × 128 × 4096 × 1 × 2 bytes ≈ **512 MiB**；若误用 hidden=4096，会算成约 2 GiB（4×）。

### 15.12.4 压测模板

```text
1. 固定镜像 digest / Ray / KubeRay 版本
2. 固定数据规模/分布
3. 预热（模型/缓存/JIT）
4. 逐步加压：1, 2, 4, 8, 16, 32, 64 并发
5. 记录：RPS、P50/P95/P99、错误率、GPU 利用率、队列、KV cache
6. 找 SLO 拐点
7. 用同一脚本对比配置/版本
8. 记录“请求资源 vs 实际使用”
```

### 15.12.5 容量规划表

| 项 | 公式/来源 | 示例 |
|---|---|---|
| 峰值 demand | 压测/历史 | 120 CPU、7 GPU |
| 单副本容量 | Pod requests | 8 CPU、1 GPU |
| 基础副本 | `ceil(demand/capacity)` | 15 |
| 余量 | ×1.2–1.5 | 18–23 |
| 升级 surge | 2× 或 maxSurgePercent | +100% |
| 故障冗余 | N+1 | +1 节点 |
| 启动时间 | 镜像+调度+注册 | 3 分钟 |

更完整的 autoscaler 公式和 LLM 显存模型见[第 34 章](34-autoscaler-v2-internals.md)、[第 28 章](28-llm-serving.md)。

## 15.13 基准记录模板与回归门禁

### 15.13.1 一次基准必须记录的元数据

```yaml
run_id: bench-2026-10-18-001
date: 2026-10-18
owner: platform-team
workload: ray-serve-llm
git_commit: <repo>@<sha>
kuberay_version: v1.7.1
ray_version: 2.59.0
kubernetes_version: v1.32.4
cni: cilium-1.18
gpu_operator: 25.3
node_type: a100-80g
image_digest: sha256:...
model: llama-3.1-8b-instruct
quantization: fp16
tensor_parallel: 1
replicas: 2
concurrency_levels: [1, 2, 4, 8, 16, 32, 64]
duration_per_level: 300s
warmup: 60s
dataset: prompts-2026-09.jsonl
```

没有这些元数据，结果无法复现，也无法判断“变快/变慢”是代码还是环境。

### 15.13.2 结果表

| 并发 | RPS | P50 | P95 | P99 | TTFT P95 | TPOT P95 | 错误率 | GPU 利用率 | KV cache |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | | | | | | | | | |
| 4 | | | | | | | | | |
| 16 | | | | | | | | | |
| 32 | | | | | | | | | |

### 15.13.3 回归门禁

| 指标 | 门禁 | 说明 |
|---|---|---|
| P95 延迟 | 不超过基线 10% | 超过则阻塞合并/发布 |
| 错误率 | 不超过 0.5% | 绝对阈值 |
| RPS | 不低于基线 5% | 吞吐回归 |
| GPU 利用率 | 不低于基线 10% | 资源效率 |
| 冷启动 | 不超过基线 20% | 模型/镜像变化 |

时间/机器差异大时，用“同机同镜像 A/B”比较，不要跨环境比绝对值。

### 15.13.4 基准的常见错误

- 只跑一次，没有预热；
- 只报平均值，没有 P95/P99；
- 混用不同 prompt/上下文长度；
- 把 GPU 利用率当成唯一指标；
- 不记录镜像 digest/驱动/CNI；
- 用压测客户端和 Ray 抢同一批 CPU。

## 15.14 小结

- 性能问题要分层定位：Pod -> Ray -> 应用 -> Serve -> 控制面。
- `/dev/shm`、对象存储、资源对齐是最常见的三个坑。
- GPU 利用率低通常不是 KubeRay 的问题，而是数据/批大小/通信。
- 成本优化从“用完即删、自动缩容、优先级、Spot、监控闲置”开始。
- 容量规划要给升级和扩容留缓冲。

下一章 APIServer、Dashboard、kubectl-ray。

→ [第 16 章 KubeRay APIServer、Dashboard 与 kubectl-ray](16-apiserver-dashboard-cli.md)
