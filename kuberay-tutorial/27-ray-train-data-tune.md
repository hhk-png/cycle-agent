# 第 27 章 在 KubeRay 上运行 Ray Train / Data / Tune / RLlib

KubeRay 只负责把 Ray 集群摆好；真正决定作业形态的是上层的 Ray 库。本章把四类最常见的工作负载——分布式训练、数据处理、超参搜索、强化学习——映射到 RayCluster/RayJob 的配置上，并给出可执行的 entrypoint 结构、资源规划、checkpoint 与容错做法。

## 27.1 先选工作负载形态

| 工作负载 | 典型库 | 生命周期 | 推荐 CRD | 关键资源 |
|---|---|---|---|---|
| 分布式训练 | Ray Train | 一次性/可重试 | RayJob（或 RayCluster + 提交） | GPU、网络、checkpoint |
| 批数据处理 | Ray Data | 一次性/周期性 | RayJob / RayCronJob | CPU、对象存储、内存 |
| 超参搜索 | Ray Tune | 一次性、多个 trial | RayJob | CPU/GPU、调度公平性 |
| 强化学习 | RLlib | 一次性/长期 | RayJob / RayService | CPU、GPU、环境依赖 |
| 在线推理 | Ray Serve | 长期 | RayService | GPU、延迟、副本 |
| 交互式开发 | Ray Core | 长期 | RayCluster | 资源池、隔离 |

判断规则：

- **作业会结束吗？** 会 -> RayJob；不会 -> RayService/RayCluster。
- **需要对外 HTTP 吗？** 需要 -> RayService。
- **需要复用一个长期集群吗？** 是 -> RayCluster + RayJob `clusterSelector`。
- **需要定时跑吗？** -> RayCronJob。

## 27.2 Driver 在哪里运行

Ray 作业的入口是 driver。先分清“谁提交”和“driver 在哪运行”：

| 模式 | 提交者 | Driver 位置 | 适合 |
|---|---|---|---|
| K8sJobMode | 独立 submitter Pod（只执行 `ray job submit`） | Ray 集群内（head 或 worker，由资源调度决定） | 生产批作业，隔离好 |
| HTTPMode | operator 通过 Dashboard 提交 | Ray 集群内 | 网络简单 |
| SidecarMode | head Pod 的 sidecar | Ray 集群内 | 严格网络隔离 |
| InteractiveMode | 用户自己（Notebook/终端） | 用 Jobs API 时仍在 Ray 集群内；只有已废弃的 Ray Client 会让 driver 跑在用户进程 | 调试 |

submitter/sidecar 不是 driver；它们只负责把 entrypoint 交给 Ray。driver 的 CPU/GPU 消耗要看 `entrypointNumCpus`/`entrypointNumGpus`/`entrypointResources` 和 Ray 调度结果（第 35.7 节）。

无论哪种模式，entrypoint 里都应该：

```python
import ray
ray.init(address="auto")   # 连接到当前集群
```

不要写死 `ray.init(address="ray://...")`，除非你明确使用 Ray Client；KubeRay 注入的 `RAY_ADDRESS` 已经指向正确的 GCS 地址。

## 27.3 runtime_env 与依赖管理

### 27.3.1 三种依赖来源

| 方式 | 优点 | 缺点 |
|---|---|---|
| 预装进镜像 | 启动快、可复现 | 镜像大、构建慢 |
| `runtime_env.pip` | 灵活 | 每个节点安装、启动慢、易失败 |
| `working_dir` | 代码随作业走 | 大目录传输慢，需对象存储 |

生产推荐：**基础依赖进镜像，业务代码用 working_dir 或镜像 tag，临时依赖才用 pip**。

### 27.3.2 runtimeEnvYAML 示例

```yaml
spec:
  runtimeEnvYAML: |
    working_dir: s3://example-bucket/code/train-v42.zip
    pip:
      - transformers==4.46.0
      - datasets==3.1.0
    env_vars:
      HF_HOME: /mnt/cache/huggingface
      NCCL_DEBUG: INFO
    config:
      setup_timeout_seconds: 600
```

注意：

- `working_dir` 会下载到每个节点；大文件会显著拖慢扩容。
- `pip` 安装发生在每个节点，GPU 节点很多时是扩容瓶颈。
- 敏感变量用 Secret 注入，不要写进 `runtimeEnvYAML`。
- `setup_timeout_seconds` 太短会让慢节点反复失败。

### 27.3.3 镜像 vs runtime_env 的取舍

```text
镜像：CUDA、PyTorch、Ray、系统库、常用 Python 包
runtime_env：作业代码、少量版本覆盖、实验性依赖
```

如果每次作业都 pip 安装 PyTorch，扩容 16 个 GPU 节点会变成 16 次下载；这通常比训练本身还慢。

## 27.4 Ray Train

### 27.4.1 最小 TorchTrainer

```python
# train.py
import ray
from ray.train import ScalingConfig, RunConfig, CheckpointConfig
from ray.train.torch import TorchTrainer

def train_loop_per_worker(config):
    import torch
    from ray.train.torch import prepare_model, prepare_data_loader

    model = prepare_model(torch.nn.Linear(32, 1))
    optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
    for epoch in range(config["epochs"]):
        # ... 训练 ...
        pass
    # 保存 checkpoint 并上报指标
    import tempfile, os
    from ray.train import Checkpoint, report
    with tempfile.TemporaryDirectory() as tmp:
        torch.save(model.state_dict(), os.path.join(tmp, "model.pt"))
        report(
            {"loss": 0.0, "epoch": config["epochs"]},
            checkpoint=Checkpoint.from_directory(tmp),
        )

ray.init(address="auto")
trainer = TorchTrainer(
    train_loop_per_worker=train_loop_per_worker,
    train_loop_config={"epochs": 10},
    scaling_config=ScalingConfig(
        num_workers=4,
        use_gpu=True,
        resources_per_worker={"CPU": 8, "GPU": 1},
    ),
    run_config=RunConfig(
        storage_path="s3://example-bucket/ray-results",
        name="llm-finetune",
        checkpoint_config=CheckpointConfig(num_to_keep=2),
    ),
)
result = trainer.fit()
print(result.metrics)
```

### 27.4.2 KubeRay 侧的对应配置

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: train-job
spec:
  entrypoint: python /home/ray/app/train.py
  submissionMode: K8sJobMode
  shutdownAfterJobFinishes: true
  ttlSecondsAfterFinished: 600
  activeDeadlineSeconds: 86400
  rayClusterSpec:
    rayVersion: "2.59.0"
    headGroupSpec:
      template:
        spec:
          containers:
            - name: ray
              image: my-registry/train:2.59.0
              resources:
                requests: {cpu: "4", memory: 16Gi}
    workerGroupSpecs:
      - groupName: gpu
        replicas: 4
        minReplicas: 0
        maxReplicas: 32
        template:
          spec:
            containers:
              - name: ray
                image: my-registry/train:2.59.0
                resources:
                  requests: {cpu: "8", memory: 64Gi, nvidia.com/gpu: "1"}
                  limits: {nvidia.com/gpu: "1"}
```

关键对齐：

- `ScalingConfig.num_workers` × `resources_per_worker` 必须能在 Ray 资源里满足；
- Ray 看到的 GPU 数 = Pod 的 `nvidia.com/gpu`；
- NVIDIA device plugin 注入的是 `NVIDIA_VISIBLE_DEVICES`；`CUDA_VISIBLE_DEVICES` 由 Ray 按 task/actor 的资源分配设置。不要手动覆盖这两个变量；
- 如果使用 gang scheduling，确保 PodGroup 的 `minMember` 覆盖 head + 所有 worker（第 12/29 章）。

### 27.4.3 Checkpoint 与容错

Ray Train 的 checkpoint 默认写到 `RunConfig.storage_path`。生产建议：

- 使用对象存储（S3/GCS/Azure Blob），不要写 Pod 本地盘；
- 设置 `CheckpointConfig(num_to_keep=N)`，避免无限增长；
- checkpoint 路径带作业 ID/时间戳，便于回溯；
- 训练循环要能 `resume_from_checkpoint`；
- `max_failures` 控制自动恢复次数。

KubeRay 的配合：

- **worker Pod 崩溃**：控制器重建 Pod，Ray Train 从 checkpoint 恢复（需要 `max_failures`）；
- **head 崩溃**：无 GCS FT 时整个集群丢失，RayJob 失败/重试；有 GCS FT 时可能恢复；
- **Spot 中断**：依赖 checkpoint + 重试，不要假设 Pod 长期存在；
- **`backoffLimit`**：RayJob 级别重试，每次创建新集群；训练必须幂等。

### 27.4.4 数据本地性与网络

- 训练数据尽量放对象存储，按 rank 分片读取；
- 多机训练需要高带宽网络（IB/RoCE）；在 K8s 上通过 SR-IOV/RDMA device plugin 暴露；
- `NCCL_SOCKET_IFNAME`、`NCCL_IB_DISABLE` 等变量按集群网络配置；
- 同一可用区/同一节点池减少跨区流量；
- `topologySpreadConstraints` 只负责**打散**；训练若希望同机架/同区**聚合**，要用 `podAffinity`/`nodeAffinity`（或节点池/标签），两者方向相反。

## 27.5 Ray Data

### 27.5.1 典型 ETL

```python
# etl.py
import ray

ray.init(address="auto")

ds = ray.data.read_parquet("s3://example-bucket/raw/")
ds = ds.map_batches(
    lambda batch: {"text": [t.strip().lower() for t in batch["text"]]},
    batch_format="pandas",
    batch_size=1024,
)
ds.write_parquet("s3://example-bucket/curated/")
```

对应 RayJob：

```yaml
spec:
  entrypoint: python /home/ray/app/etl.py
  runtimeEnvYAML: |
    env_vars:
      AWS_REGION: us-west-2
  rayClusterSpec:
    workerGroupSpecs:
      - groupName: etl
        replicas: 4
        minReplicas: 0
        maxReplicas: 32
```

### 27.5.2 资源与并行度

- Ray Data 的并行度由 block 数、`num_cpus`、`concurrency` 决定；
- `map_batches` 的 `batch_size` 影响内存和吞吐；
- 大 shuffle 会使用对象存储/磁盘，注意 `/dev/shm` 和 `object-store-memory`；
- 输出文件数过多会产生小文件问题；用 `write_parquet(..., min_rows_per_file=...)` 控制；
- Ray Data 的 autoscaling 与 Ray autoscaler 配合：先有 block 需求，再有 worker 扩容。

### 27.5.3 数据本地性

Ray 的对象存储是节点本地的。跨节点读取会走网络。优化：

- 让输入数据与计算同区；
- 用 `ds.repartition()` 控制 block 分布；
- 避免 driver `ray.get` 大对象；
- 使用流式执行（Ray Data 默认）而不是一次性物化。

## 27.6 Ray Tune

### 27.6.1 最小示例

```python
# tune.py
import ray
from ray import tune

ray.init(address="auto")

def trainable(config):
    score = (config["x"] - 3) ** 2 + (config["y"] + 2) ** 2
    tune.report({"score": score})

tuner = tune.Tuner(
    trainable,
    param_space={"x": tune.grid_search([0, 1, 2, 3, 4]), "y": tune.grid_search([-2, 0, 2])},
    tune_config=tune.TuneConfig(num_samples=1, max_concurrent_trials=4),
    run_config=tune.RunConfig(storage_path="s3://example-bucket/tune-results", name="grid"),
)
results = tuner.fit()
print(results.get_best_result(metric="score", mode="min"))
```

### 27.6.2 资源规划

- 每个 trial 的资源由 `tune.with_resources` 或 trainable 的 `ray_remote_args` 决定；
- `max_concurrent_trials` 控制并发，避免一次拉起数百个 trial；
- GPU trial 要设置 `num_gpus`，否则 Ray 不会分配 GPU；
- trial 之间可能争抢对象存储/网络；
- 结果写对象存储，避免 head 本地盘爆满。

KubeRay 侧：

- 小规模 Tune：单个 RayJob + 固定 worker group；
- 大规模 Tune：开启 autoscaling，让 trial 需求驱动 worker 扩容；
- 需要 gang scheduling 时，PodGroup 的 `minMember` 要覆盖最坏情况，否则调度器会拒绝。

## 27.7 RLlib

RLlib 的入口通常是 `ray.init(address="auto")` + `Algorithm`/`PPO`：

```python
import ray
from ray.rllib.algorithms.ppo import PPOConfig

ray.init(address="auto")
config = (
    PPOConfig()
    .environment("CartPole-v1")
    .env_runners(num_env_runners=8, num_cpus_per_env_runner=1)
    .training(train_batch_size_per_learner=4000)
)
algo = config.build()
for i in range(10):
    result = algo.train()
    # Ray 2.x 新版把 rollout 指标放在 env_runners 下；旧版可能是顶层
    # episode_reward_mean。以当前 Ray 版本的 result 结构为准。
    print(i, result["env_runners"]["episode_return_mean"])
```

资源注意：

- `num_env_runners` 决定并行环境数；
- learner 可能需要 GPU；
- 环境依赖（MuJoCo、Atari ROM）要预装镜像；
- checkpoint 目录要持久化；
- RLlib 的 rollout worker 对延迟敏感，网络/CPU 不足会拖慢训练。

## 27.8 四类负载的共同陷阱

### 27.8.1 Driver 与 worker 资源竞争

Driver 在 Ray 集群内运行（head 或 worker，取决于资源请求）；submitter 只负责提交。如果 driver 做大量数据处理，会占用所在节点资源；建议：

- 重活放到 task/actor；
- 给 driver 预留 CPU（`entrypointNumCpus`/`entrypointResources`，视版本）；
- 不要把大规模 `ray.get` 放在 driver。

### 27.8.2 资源请求与 Ray 资源不一致

```yaml
# Pod 请求 1 GPU
resources:
  requests:
    nvidia.com/gpu: "1"
# Ray 却看到 0 GPU（num-gpus: "0"）
rayStartParams:
  num-gpus: "0"
```

结果：Ray 永远不会把 GPU task 调度到该节点。用顶层 `resources` 或确保 `rayStartParams` 与 Pod 一致。

### 27.8.3 对象存储溢出

- `/dev/shm` 太小 -> spill/OOM；
- `object-store-memory` 太大 -> 挤压 worker 内存；
- 大对象跨节点传输 -> 网络瓶颈；
- 训练/数据处理要控制 batch 大小。

### 27.8.4 启动时间被低估

- 镜像拉取（几 GB）；
- runtime_env pip 安装；
- 模型/数据下载；
- init container 等待 GCS；
- GPU 驱动初始化。

扩容 32 个节点可能需要几分钟到十几分钟。容量规划要把启动时间算进去。

### 27.8.5 失败重试不幂等

RayJob 的 `backoffLimit` 会创建新集群重跑 entrypoint。如果 entrypoint：

- 追加写输出文件 -> 重复数据；
- 覆盖 checkpoint -> 可能破坏恢复；
- 重复提交外部任务 -> 副作用。

要让 entrypoint 可重入：使用作业 ID/幂等键，输出到新路径或原子替换。

## 27.9 观测与调试

```bash
# Ray 侧
kubectl exec <head> -- ray status
kubectl exec <head> -- ray list nodes
kubectl exec <head> -- ray list actors
kubectl exec <head> -- ray job list
kubectl exec <head> -- tail -n 200 /tmp/ray/session_latest/logs/raylet.out

# KubeRay 侧
kubectl get rayjob <name> -o yaml
kubectl describe rayjob <name>
kubectl logs job/<name>-submitter
kubectl get pods -l ray.io/cluster=<cluster> -o wide
```

关键指标：

- task/actor 排队数；
- GPU 利用率；
- 对象存储使用率/spill；
- worker 重启次数；
- 作业时长与失败率；
- 扩容耗时（Pending→Running）。

## 27.10 练习

1. 把一个单机 PyTorch 训练脚本改成 `TorchTrainer`，在 2 个 GPU worker 上跑通。
2. 给 RayJob 加 `runtime_env.working_dir`，观察每个节点的下载时间。
3. 用 Ray Data 处理 10GB Parquet，比较 4/8/16 worker 的吞吐。
4. 用 Ray Tune 跑 20 个 trial，设置 `max_concurrent_trials=4`，观察队列。
5. 故意让一个 worker Pod 崩溃，验证 Ray Train 从 checkpoint 恢复。
6. 把 `backoffLimit` 设为 2，设计一个幂等的 entrypoint。

## 27.11 端到端 ML 流水线：从数据到服务

把 Ray Data、Train、Tune、Serve 串成一条可重复的流水线，才能真正检验 KubeRay 的边界。

### 27.11.1 四个阶段

```text
[1] 数据准备（RayJob / Ray Data）
      raw -> curated，分区/去重/特征
        |
[2] 训练（RayJob / Ray Train）
      curated -> checkpoint，记录 metrics
        |
[3] 调参（RayJob / Ray Tune，可选）
      checkpoint + 搜索空间 -> best checkpoint
        |
[4] 部署（RayService / Ray Serve）
      best checkpoint -> 推理服务
```

每个阶段都是一个独立 RayJob/RayService，通过对象存储传递产物：

```text
s3://bucket/pipeline/<pipeline_id>/
├── data/curated/...
├── train/checkpoints/...
├── tune/best/...
└── serve/model/...
```

### 27.11.2 产物契约

| 产物 | 生产者 | 消费者 | 契约 |
|---|---|---|---|
| curated 数据 | 数据准备 | 训练 | schema/分区/行数校验 |
| checkpoint | 训练 | 调参/部署 | 路径 + metrics + 版本 |
| best checkpoint | 调参 | 部署 | 路径 + 超参 + 评估指标 |
| 模型服务 | 部署 | 客户端 | route/model/版本 |

用 `_SUCCESS`/manifest 标记完成，不要靠“文件存在”判断。

### 27.11.3 编排选择

- **Argo Workflows**：DAG、重试、产物；
- **Airflow**：定时/依赖；
- **KubeRay RayCronJob**：简单定时；
- **平台 API**：统一提交/查询/配额。

不要让两层都做重试：明确“工作流重试”还是“RayJob backoff”。

### 27.11.4 资源池划分

| 阶段 | 资源池 | 特点 |
|---|---|---|
| 数据准备 | CPU 池 | 高并行、对象存储带宽 |
| 训练 | GPU 池 | 长任务、checkpoint、gang |
| 调参 | GPU/CPU 混合 | 多 trial、公平性 |
| 部署 | GPU 推理池 | 低延迟、常驻、升级 surge |

用 `nodeSelector`/taint/队列隔离，避免训练占满推理 GPU。

### 27.11.5 失败与幂等

- 每个阶段用 `pipeline_id`/`run_id` 作为输出路径；
- 训练从 checkpoint 恢复；
- 调参按 trial ID 去重；
- 部署用 RayService 的 active/pending；
- 任何阶段失败都能单独重跑，不污染下游。

### 27.11.6 一个最小验收

1. 用 1GB 数据跑通数据准备；
2. 训练 1 个 epoch，产出 checkpoint；
3. 用 4 个 trial 调参（可选）；
4. 部署成 RayService，发一个请求；
5. 删除中间集群，确认产物仍在对象存储；
6. 用同一 `pipeline_id` 重跑，确认幂等。

## 27.12 小结

- Ray Train/Data/Tune/RLlib 都是 Ray 上的库；KubeRay 只提供集群和生命周期。
- 依赖管理优先镜像，runtime_env 只做补充。
- 资源三对齐：Pod requests、Ray 资源、库的 scaling 配置。
- checkpoint 写对象存储，entrypoint 要幂等，重试才有意义。
- 启动时间、对象存储、网络拓扑是分布式负载的三大隐性成本。

Ray Data 的执行器/背压/shuffle 细节见[第 45 章](45-ray-data-execution-engine.md)；checkpoint、弹性 world size 与 gang scheduling 见[第 46 章](46-elastic-distributed-training.md)。

下一章进入 LLM 推理：Ray Serve、vLLM/SGLang、GPU 共享与自动扩缩容。

→ [第 28 章 LLM 推理平台：Ray Serve、vLLM 与 SGLang](28-llm-serving.md)
