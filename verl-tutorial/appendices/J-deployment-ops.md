# 附录 J 部署与集群运维

> 这一篇回答“训练脚本跑通之后，怎么把它变成一台可持续运转的机器”。内容偏平台工程：调度、存储、网络、健康检查、监控、抢占与上线清单。

## J.1 三种部署形态

verl 假设“Ray 已经能在目标资源上启动”，自身不绑定调度器。常见形态：

| 形态 | 典型规模 | 控制面 | 适合 |
|---|---|---|---|
| 单机多卡 | 1～8 GPU | 本地 Ray | 开发、调试、小模型 |
| Slurm 集群 | 8～数百 GPU | `sbatch`/`srun` + Ray | 学术集群、超算、固定配额 |
| Kubernetes | 弹性/多租户 | KubeRay、RayJob、自定义 Operator | 云平台、SaaS、抢占实例 |

选择时先问三个问题：

1. 资源是**固定配额**还是**弹性申请**？
2. 是否需要**多租户隔离**与配额计费？
3. 故障时是**整机重启**还是**单 worker 恢复**？

单机不是“生产缩小版”：它没有 gang scheduling、没有跨机 NCCL、没有共享存储故障，很多问题不会暴露。至少要在多机环境跑过一次完整训练，再谈上线。

## J.2 进程与资源模型

一次 verl RL 训练通常包含：

```text
controller (driver)
  ├── Ray head (GCS, dashboard, object store)
  ├── actor workers        GPU
  ├── critic workers       GPU（可选）
  ├── ref workers          GPU（可与 actor colocate）
  ├── rollout servers      GPU（vLLM/SGLang）
  ├── reward servers       GPU/CPU（可选）
  └── shared storage       parquet / checkpoint / logs
```

资源规划的三个经验值：

- **rollout 与训练 colocate** 时，单卡显存要同时容纳训练状态与 KV cache；用 sleep/wake 切换，而不是让两个进程常驻；
- **reward 服务** 如果调用外部 API 或沙箱，放在 CPU 池并做限流，不要占用训练 GPU；
- **controller** 只做编排，CPU 即可，但要给它稳定的内存与网络，避免它成为单点。

## J.3 Slurm：用 srun 拉起 Ray

v0.9.1 仓库提供了 `examples/tutorial/slurm/ray_on_slurm.slurm` 作为起点，但它用 `nodes_array=("$nodes")` 把多行主机名塞进一个数组元素，且包装了 apptainer 与 `--node-ip-address`。下面是**修正后的核心结构**（不依赖容器；如果你的集群用 apptainer，把 `srun` 命令套进去即可）：

```bash
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:8
#SBATCH --time=24:00:00

# 每个节点启动一个 Ray 进程；第一个节点作为 head。
# scontrol 输出是“每行一个主机名”，必须读进 Bash 数组，不能当标量用。
mapfile -t nodes < <(scontrol show hostnames "$SLURM_JOB_NODELIST")
head="${nodes[0]}"
port=6379

srun --nodes=1 --ntasks=1 -w "$head" \
  ray start --head --port=$port --dashboard-host=0.0.0.0

for node in "${nodes[@]:1}"; do
  srun --nodes=1 --ntasks=1 -w "$node" \
    ray start --address="$head:$port" --block &
done

# 等所有 worker 加入，再提交训练；否则 controller 会在半个集群上启动。
for _ in $(seq 1 60); do
  if ray status --address="$head:$port" >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
ray status --address="$head:$port"

# 在 head 上提交训练；确保 PYTHONPATH、数据路径、checkpoint 路径一致。
srun --nodes=1 --ntasks=1 -w "$head" \
  python3 -m verl.trainer.main_ppo \
  trainer.nnodes="${#nodes[@]}" \
  trainer.n_gpus_per_node=8 \
  ...
```

注意事项：

- `srun` 通常会继承提交环境，但 batch 脚本里未 `export` 的变量、模块加载差异、不同节点的镜像/conda 环境仍会导致不一致；关键变量显式 `export` 或写进 `--export`，并在启动日志里打印；
- 节点名要能被所有节点解析；Ray 用地址发现，不依赖 DNS 也可以，但跨机端口要通；
- 共享文件系统（NFS/Lustre）上放数据和 checkpoint；本地盘只做缓存；
- 作业结束前 `ray stop`，避免残留进程占端口。

## J.4 Kubernetes：RayCluster / RayJob

K8s 上最常见的两种 CRD：

- **RayCluster**：长期存活的 Ray 集群，训练作业作为 client 提交；
- **RayJob**：一次性的 job + 自动清理，适合 CI/批量实验。

一个最小 RayCluster 结构（省略镜像与卷细节）：

```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: verl-ray
spec:
  headGroupSpec:
    rayStartParams:
      dashboard-host: "0.0.0.0"
    template:
      spec:
        containers:
          - name: ray-head
            image: registry.example.com/verl:0.9.1
            resources:
              limits: {cpu: "16", memory: "64Gi"}
  workerGroupSpecs:
    - groupName: gpu-workers
      replicas: 2
      minReplicas: 2
      maxReplicas: 2
      rayStartParams: {}
      template:
        spec:
          containers:
            - name: ray-worker
              image: registry.example.com/verl:0.9.1
              resources:
                limits: {nvidia.com/gpu: "8", cpu: "64", memory: "512Gi"}
              volumeMounts:
                - {name: data, mountPath: /data}
                - {name: ckpt, mountPath: /ckpt}
```

生产上还要补：

- **gang scheduling**：所有 worker 同时就绪再启动 controller，避免半启动卡死；
- **node affinity/taint**：GPU 型号、NVLink 域、IB 网卡要匹配；
- **hostNetwork + hugepages**：多机 NCCL 常见要求；
- **PVC/对象存储**：数据和 checkpoint 不要落在容器本地；
- **PodDisruptionBudget**：避免滚动升级把训练全部打断。

## J.5 存储布局

一个可维护的目录约定：

```text
/data/<dataset>/train.parquet
/data/<dataset>/val.parquet
/ckpt/<experiment>/<run_id>/global_step_<n>/
/logs/<experiment>/<run_id>/
```

要求：

- **原子写**：先写 `*.tmp`，完成后 rename；避免半截 checkpoint；
- **版本化**：数据、代码、配置、镜像 digest 一起记录；
- **保留策略**：最近 N 个 + 最佳 N 个 + 里程碑；不要无限增长；
- **读取带宽**：训练开始时所有 rank 同时读 parquet，共享存储的元数据操作可能成为瓶颈；可以先做本地缓存。

## J.6 网络与 NCCL

多机训练的硬约束：

- **IB/RoCE**：RDMA 需要正确的 GID、MTU、PFC/ECN 配置；
- **NCCL 拓扑**：`NCCL_DEBUG=INFO` 看它选了哪条链路；跨机走 IB，机内走 NVLink；
- **端口**：NCCL 使用的临时端口范围要开放；容器网络常在这里出问题；
- **超时**：`nccl_timeout` 要大于最慢的集合通信，但不要把 hang 掩盖成慢；
- **多网卡**：用 `NCCL_SOCKET_IFNAME`/`NCCL_IB_HCA` 明确指定，避免选错网卡。

一个实用检查：

```bash
# 两节点之间的带宽与延迟
ib_write_bw -d mlx5_0 -F --report_gbits
# NCCL 自检
all_reduce_perf -b 8 -e 8G -f 2 -g 8
```

如果 `all_reduce_perf` 正常但训练 hang，问题通常在应用层：某个 rank 没进入集合通信、进程组创建顺序不一致、或者某个 worker 先 OOM。

## J.7 启动、健康检查与优雅退出

启动脚本应包含 preflight：

```bash
set -euo pipefail

test -f "$DATA/train.parquet"
test -d "$CKPT"
python -c "import torch, ray; print(torch.cuda.device_count(), ray.__version__)"
nvidia-smi --query-gpu=index,name,memory.total --format=csv
```

健康检查分三层：

1. **进程存活**：Ray actor 是否在；
2. **服务就绪**：rollout server 的 `/health`、reward server 的端口；
3. **业务可用**：用一条固定 prompt 做 smoke generation，比较输出长度与 log-prob 是否合理。

退出时按反序：

```text
停止采样 → 等待 in-flight update → 保存 checkpoint → 关闭 rollout server
→ ray stop → 释放 PVC
```

收到 `SIGTERM` 时不要立即 `kill -9`；给 controller 一个 drain 窗口，把当前 step 做完。

## J.8 可观测性

最小监控面板应包含：

| 类别 | 指标 |
|---|---|
| 训练 | step 时间、tokens/s、MFU、loss、grad norm、clip fraction |
| rollout | 并发、p50/p95/p99 延迟、生成长度、KV cache 使用率、抢占次数 |
| reward | 调用量、失败率、超时、平均分、分布 |
| 同步 | 字节数、耗时、版本、失败次数 |
| 数据 | 队列长度、重复率、过滤比例 |
| 系统 | GPU 利用率、显存、温度、ECC、IB 带宽、磁盘延迟 |
| 质量 | 验证指标、pass@k、生成样例、人工抽查 |

日志要能按 `run_id`、`step`、`rank` 过滤；trace 要能定位一次 step 的各个阶段。没有这些，故障排查只能靠猜。

## J.9 故障演练

上线前至少做四次演练：

1. **杀 worker**：`kill -9` 一个 actor，记录真实行为。verl v0.9.1 没有给这些 actor 配 `max_restarts`/`max_task_retries`，Ray 默认不会自动重启；预期是 **任务失败 → 从 checkpoint 续训**，而不是原地恢复。若确实要 actor 重启，需自己在 Ray actor 选项里配 `max_restarts` 并验证状态重建（优化器、TransferQueue、权重版本）；
2. **抢占节点**：模拟竞价实例回收，确认 SIGTERM 处理与 checkpoint 完整性；
3. **网络抖动**：断开一台机器的 IB，确认 NCCL timeout 与恢复路径；
4. **存储变慢**：给 PVC 加延迟，确认数据加载不会拖垮整个 step。

每次演练记录：发现时间、恢复时间、丢失 step 数、根因、改进项。没有演练的“容错设计”通常只是愿望。

## J.10 安全与多租户

- **镜像固定 digest**，不要用 `latest`；
- **secrets** 放 K8s Secret/Vault，不要写进 YAML 或日志；
- **沙箱隔离**：代码执行类工具用独立 namespace/VM，限制网络与文件系统；
- **网络策略**：训练 namespace 只开放必要端口，禁止访问公网；
- **配额**：按团队/实验限制 GPU 与存储，避免一个 job 吃满集群；
- **审计**：记录谁在什么时间提交了什么配置、用了多少资源。

## J.11 上线检查清单

```text
[ ] 数据路径在共享存储，所有节点可读
[ ] 镜像 digest 固定，依赖版本锁定
[ ] Ray head/worker 端口与发现方式明确
[ ] gang scheduling 或等价机制已配置
[ ] NCCL 自检通过，IB/RoCE 配置有记录
[ ] checkpoint 原子写、保留策略、恢复演练通过
[ ] SIGTERM drain 与抢占恢复演练通过
[ ] 监控面板覆盖训练/rollout/reward/同步/系统
[ ] 日志可按 run_id/step/rank 检索
[ ] 安全：secrets、沙箱、网络策略、配额
[ ] 成本：GPU 小时、存储、出口流量有预算与告警
```

## 延伸阅读

- 多机启动：`docs/start/multinode.rst`
- Ray 调试：`docs/start/ray_debug_tutorial.rst`
- Slurm 示例：`examples/tutorial/slurm/ray_on_slurm.slurm`
- 故障排查：附录 D
- 硬件配方：附录 I
