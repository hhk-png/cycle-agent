# 示例清单

共 37 个文件：10 个队列/拓扑/中断预算/配额、20 个作业、7 个调度器配置（6 YAML + 1 JSON）。YAML 面向真实 Volcano v1.16；JSON 是 MiniVolcano 的配置格式。

## 队列与优先级

| 文件 | 内容 |
|---|---|
| `queues/queues.yaml` | team-a/team-b 的 weight/capability/guarantee |
| `queues/hierarchical-queues.yaml` | org → org-a/org-b 层级队列 |
| `queues/priorityclasses.yaml` | high/low PriorityClass |
| `queues/dra-queue.yaml` | v1.15+ DRA DeviceClass 队列配额 |
| `queues/hypernode.yaml` | HyperNode 树（tier/members） |
| `queues/namespace-queue.yaml` | v1.16 NamespaceQueue 子树与 `allowedNamespaces` 授权 |
| `queues/pdb.yaml` | PodDisruptionBudget（pdb 插件使用） |
| `queues/resourcequota.yaml` | Namespace + ResourceQuota（resourcequota 插件与 API Server 双重保护） |
| `queues/llm-queues.yaml` | LLM 四队列：train/finetune/infer/agent 的 guarantee/capability |
| `queues/queue-nodegroup-affinity.yaml` | 队列 × 节点组：affinity/antiAffinity + 分组资源上限 |

## 作业

| 文件 | 内容 |
|---|---|
| `jobs/vcjob-pytorch.yaml` | PyTorch 分布式，ssh/svc/pytorch 插件 |
| `jobs/vcjob-mpi.yaml` | MPI master/worker |
| `jobs/vcjob-tensorflow.yaml` | TF ps/worker |
| `jobs/vcjob-ray.yaml` | Ray head/worker |
| `jobs/vcjob-gang-minavailable.yaml` | minAvailable < replicas 的弹性 gang |
| `jobs/vcjob-task-gang.yaml` | task 级 minAvailable（minTaskMember）；刻意不使用 dependsOn（会与严格 gang 死锁） |
| `jobs/vcjob-min-success.yaml` | minSuccess：2/4 分片成功即提前完成 |
| `jobs/vcjob-network-topology.yaml` | networkTopology hard |
| `jobs/vcjob-tdm-preemptable.yaml` | TDM preemptable 作业 |
| `jobs/vcjob-sla.yaml` | SLA 等待预算 |
| `jobs/vcjob-heterogeneous.yaml` | A100 nodeSelector |
| `jobs/vcjob-cron.yaml` | Cron VolcanoJob |
| `jobs/podgroup-raw.yaml` | Deployment + PodGroup gang |
| `jobs/jobtemplates.yaml` | JobFlow 引用的 JobTemplate |
| `jobs/jobflow.yaml` | prep → train DAG |
| `jobs/podgroup-anti-affinity.yaml` | v1.16 PodGroup 拓扑反亲和 |
| `jobs/vcjob-llm-pretrain.yaml` | 64 卡预训练：8 Pod × 8 GPU、严格 gang、hard 拓扑 |
| `jobs/vcjob-llm-finetune.yaml` | 8 卡全参微调：4 Pod × 2 GPU、checkpoint 重启 |
| `jobs/vcjob-torchft-elastic.yaml` | 弹性训练：8 副本、minAvailable=6 |
| `jobs/vcjob-vllm-serving.yaml` | 多机 serving 组 + PDB（gang + 中断保护） |

## 调度器配置

| 文件 | 内容 |
|---|---|
| `config/scheduler-config.yaml` | proportion 完整流水线 |
| `config/scheduler-config-hierarchical.yaml` | capacity 层级队列 |
| `config/scheduler-config-topology.yaml` | network-topology-aware |
| `config/scheduler-config-dra.yaml` | DRA 队列配额 |
| `config/scheduler-config-tdm.yaml` | TDM 时间窗 |
| `config/scheduler-config-shuffle.yaml` | shuffle + rescheduling + pdb |
| `config/minivolcano-scheduler.json` | MiniVolcano JSON 配置 |

## 使用顺序

```bash
kubectl apply -f examples/queues/priorityclasses.yaml
kubectl apply -f examples/queues/queues.yaml
kubectl apply -f examples/jobs/vcjob-pytorch.yaml

kubectl get vcjob,podgroup,queue
```

拓扑示例需要先把 `hypernode.yaml` 里的节点名替换成真实节点，再创建 HyperNode。

调度器配置是 ConfigMap。修改后重启：

```bash
kubectl apply -f examples/config/scheduler-config.yaml
kubectl -n volcano-system rollout restart deploy/volcano-scheduler
```

## 注意

- 镜像/命令是示例，不保证在所有环境可直接运行。
- GPU 作业需要 device plugin 与 `nvidia.com/gpu` 资源。
- `hypernode.yaml` 的节点名、GPU Operator 标签、DRA DeviceClass 都需按集群替换。
- 不要把 `minivolcano-scheduler.json` 应用到 Kubernetes；它只给 MiniVolcano 用。
- 真实 Volcano 没有 Reservation CRD；本目录不提供该对象的清单。
- LLM 示例（`vcjob-llm-*`、`vcjob-torchft-*`、`vcjob-vllm-*`）中的训练脚本、vLLM 多机启动参数、PVC、NCCL 网卡名都是占位，需按集群替换；细节见教程第 34 章。

验证 YAML/JSON 语法：

```bash
python ../scripts/check_docs.py
```
