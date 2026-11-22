# 第 10 章 单控制器、WorkerGroup 与 Ray 执行模型

## 10.1 控制器做什么、不做什么

`PPOTrainer`（V1 中是 `verl/trainer/ppo/v1/trainer_base.py` 的 `PPOTrainer`）是控制器。它：

- 读取 batch；
- 调用 rollout 生成；
- 调用 reward；
- 调用 actor/ref/critic 的 RPC；
- 计算 advantage；
- 记录指标、保存 checkpoint。

它**不做**：

- 模型前向/反向；
- tokenization（v0.9.1 的 dataset `__getitem__` 只返回 `raw_prompt`；chat template/tokenization 在 AgentLoop 完成，collate 只 padding/stack）；
- NCCL 通信；
- KV cache 管理。

控制器是“编排者”，不是“计算者”。这让 RL 算法可以用普通 Python 写，而不必在每个 rank 上重复。

## 10.2 Worker 的生命周期

一个 worker 的典型生命周期：

```text
构造（Ray actor 启动）
  -> init_model（加载权重、构建引擎/rollout）
  -> compute_log_prob / update_actor / ...（反复 RPC）
  -> save_checkpoint
  -> 销毁
```

`verl/single_controller/base/worker.py` 的 `Worker` 保存：

- `rank`、`world_size`、`local_rank`；
- `master_addr` / `master_port`；
- dispatch/collect 注册信息；
- 设备信息。

worker 之间通过 `torch.distributed` 或 Ray collective 通信；verl 的 `WorkerGroup` 负责初始化和监控。

## 10.3 ResourcePool 与 placement group

`ResourcePool(process_on_nodes=[8, 8])` 表示两台机器各 8 个进程。`RayResourcePool` 把它变成 placement group：

```python
bundle = {"CPU": max_colocate_count}
if use_gpu:
    bundle[device_name] = 1
pg_scheme = [[bundle.copy() for _ in range(count)] for count in process_on_nodes]
pgs = [placement_group(bundles=bundles, strategy="STRICT_PACK") for bundles in pg_scheme]
```

关键点：

- `STRICT_PACK`：同一节点的 worker 尽量放在同一台机器，减少跨节点通信；
- `max_colocate_count`：一个 bundle 里允许放几个逻辑 worker（actor/ref/rollout 共享 GPU 时）；
- `NodeAffinitySchedulingStrategy`：把某个 worker 固定到特定节点；
- `sort_placement_group_by_node_ip`：保证多机重启后 rank 与节点 IP 的对应关系稳定，避免 checkpoint 的本地路径错位。

## 10.4 RayWorkerGroup

`verl/single_controller/ray/base.py` 的 `RayWorkerGroup` 做四件事：

1. 根据 `RayClassWithInitArgs` 创建 N 个 Ray actor；
2. 把方法调用转换成 `actor.method.remote(...)`；
3. 按 dispatch 模式切参数；
4. 按 collect 模式合并结果。

伪代码：

```python
class RayWorkerGroup:
    def __getattr__(self, name):
        method = getattr(self.workers[0], name)
        dispatch = method._dispatch_mode
        def call(*args, **kwargs):
            args, kwargs = dispatch_fn(self, *args, **kwargs)
            futures = [w.method.remote(*a, **k) for w, a, k in zip(...)]
            outputs = ray.get(futures)
            return collect_fn(self, outputs)
        return call
```

MiniVerl 的 `LocalWorkerGroup.__getattr__` 是同一逻辑的同步版本。

## 10.5 Dispatch 模式再看一遍

| 模式 | 切分 | 收集 | 典型 RPC |
|---|---|---|---|
| `ONE_TO_ALL` | 不切，复制 | list | `init_model` |
| `RANK_ZERO` | 只 rank 0 | 单值 | `save_checkpoint` |
| `ALL_TO_ALL` | 调用者已按 rank 准备 | list | 自定义 |
| `DP_COMPUTE` | 参数是 per-rank 列表 | list | 手动分片 |
| `DP_COMPUTE_PROTO` | DataProto 按 world size 切 | concat | `compute_log_prob` |
| `DP_COMPUTE_PROTO_WITH_FUNC` | 第一个参数是函数 | concat | loss 函数下发 |
| `make_nd_compute_dataproto_dispatch_fn` | 按 mesh 的 DP 维切 | concat | Megatron PP/CP |

`make_nd_compute_dataproto_dispatch_fn` 是 v0.9.1 的重要改进。Megatron 的 PP 维会让“每个 rank 是否持有完整输出”变得复杂；这个函数查询 worker 注册的 `dp_rank` 与 `is_collect`，只从需要的 rank 收集，避免在 PP 维上重复 concat。

## 10.6 colocate：一个 GPU 上放多个角色

同步 PPO 中，actor、ref、rollout 共享同一组 GPU。verl 用 `create_colocated_worker_cls` 把多个 worker 类融合成一个 Ray actor。注意它接收的是 `RayClassWithInitArgs`（类 + 初始化参数），不是裸类：

```python
fused_cls = create_colocated_worker_cls({
    "actor": RayClassWithInitArgs(cls=ActorRolloutRefWorker, config=actor_config),
    "critic": RayClassWithInitArgs(cls=TrainingWorker, config=critic_config),
})
```

该函数在 v0.9.1 标记为 deprecated，新代码应优先看 `create_colocated_worker_cls_fused`（FusedWorker 路径）。

一个 actor 进程里同时持有：

- actor 引擎；
- ref 引擎（可选）；
- rollout 引擎；
- critic 引擎（如果同组）。

这减少了进程数与显存碎片，但也意味着：

- 一个 worker 崩溃会影响多个角色；
- 显存预算是共享的；
- sleep/wake 要按角色协调。

## 10.7 异步 RPC 与 future

`@register(..., blocking=False)` 返回 future。控制器可以先发起多个 RPC，再统一 `ray.get`：

```python
futures = [wg.update_actor.remote(batch) for batch in batches]
results = ray.get(futures)
```

verl 的 `DataProtoFuture` 让 future 也能参与 `concat`/`select`。这对异步训练很重要：controller 不必等每个 worker 完成才能继续调度。

MiniVerl 的 `LocalFuture` 是已完成 future，不提供真正并发；它只保证 API 形状一致。

## 10.8 aliveness 与故障

`WorkerGroup.start_worker_aliveness_check` 会启动后台线程，每秒检查 worker 是否存活。如果某个 worker 死亡，主进程收到 `SIGABRT`。Ray 是否重启 actor 取决于 `max_restarts`；verl v0.9.1 默认没有为这些 actor 配置重启，因此默认结果是任务失败，生产上应准备好从 checkpoint 续训。

生产环境还要考虑：

- Ray actor 重启后是否需要重新 `init_model`；
- checkpoint 是否在共享存储；
- NCCL 进程组是否需要重建；
- 数据队列中的 in-flight 样本如何处理。

第 31 章展开容错。

## 10.9 一个最小例子

```python
from mini_verl.single_controller.decorator import Dispatch, register
from mini_verl.single_controller.worker import Worker
from mini_verl.single_controller.worker_group import LocalWorkerGroup, ResourcePool
from mini_verl.protocol import DataProto
import numpy as np

class Echo(Worker):
    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def who(self):
        return self.rank

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def echo(self, data):
        return data

group = LocalWorkerGroup(ResourcePool.single_node(3), Echo)
print(group.who())                       # [0, 1, 2]
out = group.echo(DataProto(batch={"x": np.arange(10)}))
print(len(out), out.batch["x"].tolist()) # 10, [0..9]
```

运行 `examples/05_dispatch_and_sharding.py` 可以看到完整输出。

## 10.10 MiniVerl 对照

| 真实 verl | MiniVerl |
|---|---|
| `RayWorkerGroup` | `LocalWorkerGroup` |
| `RayResourcePool` | `ResourcePool` |
| `create_colocated_worker_cls` | 单类多角色（`role` 参数） |
| `Dispatch.DP_COMPUTE_PROTO` | 同名实现 |
| `make_nd_compute_dataproto_dispatch_fn` | 未实现（单维 DP） |
| `DataProtoFuture` | `LocalFuture` |
| aliveness 线程 | 无（同进程） |

MiniVerl 的 `sync_gradients=True` 是一个额外扩展：worker 只做 backward，group 在所有 worker 返回后 all-reduce 梯度并让每个 worker step。这把 NCCL 语义显式化，便于测试。

## 本章小结

- 控制器负责编排，worker 负责计算；二者通过 dispatch/collect 协议交互。
- ResourcePool + placement group 决定进程放在哪、如何共享 GPU。
- `DP_COMPUTE_PROTO` 是最常用的 RPC 模式；`make_nd_compute_*` 处理多维并行。
- colocate 减少进程与显存碎片，但增加故障耦合。
- MiniVerl 用同进程 rank 模拟这套语义，便于单步调试。

## 延伸阅读

- `verl/single_controller/base/decorator.py`
- `verl/single_controller/ray/base.py`
- Ray placement group 文档：<https://docs.ray.io/en/latest/ray-core/scheduling/placement-group.html>
- 下一章：FSDP/FSDP2 引擎。
