# 样例清单说明

这些 YAML 是教学样例，不是“一个 `kubectl apply` 就能在任意集群跑通”的生产清单。下表列出每个样例的前置条件。

| 文件 | 用途 | 前置条件 |
|---|---|---|
| `ray-cluster.yaml` | 最小 RayCluster | 无（Ray 镜像可拉取） |
| `ray-cluster-autoscaling.yaml` | autoscaler v2 | 无 |
| `ray-cluster-ippr.yaml` | In-Place Pod Resizing（IPPR） | Kubernetes 1.35+、cgroup v2、autoscaler v2；`ray.io/ippr` 为 alpha |
| `ray-cluster-native-sidecar.yaml` | 原生 sidecar（日志采集） | Kubernetes 1.33+；sidecar readiness 通过后 Ray 才启动 |
| `ray-cluster-gpu.yaml` | GPU worker | NVIDIA device plugin、GPU 节点、`NVIDIA-A100-SXM4-80GB` 标签 |
| `ray-cluster-dra.yaml` | DRA 设备申请（教学） | Kubernetes DRA API/driver、`DeviceClass`、`ResourceClaimTemplate`；Ray 需显式 `num-gpus` |
| `ray-cluster-auth.yaml` | token auth | Secret（样例内含） |
| `ray-cluster-gcs-ft.yaml` | Redis GCS FT | 可达 Redis、`redis-password` Secret |
| `ray-cluster-history-server.yaml` | History Server collector | `RayClusterHistoryServer` gate、collector 镜像 |
| `ray-cluster-mtls.yaml` | mTLS | `RayClusterMTLS` gate、cert-manager |
| `ray-cluster-network-policy.yaml` | NetworkPolicy | `RayClusterNetworkPolicy` gate、支持 NetworkPolicy 的 CNI |
| `ray-cluster-rocksdb.yaml` | RocksDB GCS FT | `GCSFaultToleranceEmbeddedStorage` gate、StorageClass `fast-ssd` |
| `ray-cluster-multihost.yaml` | multi-host worker | 支持多主机调度的节点/TPU 环境 |
| `ray-cluster-multiaz.yaml` | 跨可用区放置与 PDB | ≥3 个 AZ、`topology.kubernetes.io/zone` 标签、稳定节点池 `node.kubernetes.io/lifecycle=normal` |
| `ray-cluster-spot.yaml` | head 用 on-demand、worker 用 Spot | Karpenter/Cluster Autoscaler、Spot 节点池与 `spot=true:NoSchedule` 污点、`karpenter.sh/capacity-type` 标签 |
| `ray-cluster-graceful.yaml` | 优雅终止预算（preStop + SIGTERM + drain） | `RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S` 为所用 Ray 版本支持；`terminationGracePeriodSeconds` > preStop + drain |
| `ray-cluster-dev.yaml` | 交互式开发/Notebook 共享集群 | 能访问 head Dashboard（port-forward 8265）；不要对外暴露 10001 |
| `ray-cluster-ingress.yaml` | Dashboard Ingress | Ingress Controller `nginx`、TLS Secret |
| `ray-cluster-batch-scheduler.yaml` | Volcano gang scheduling | operator `--batch-scheduler=volcano`、Volcano |
| `ray-cluster-kueue.yaml` | Kueue 本地准入 | Kueue、LocalQueue、`kueue.x-k8s.io/queue-name`；MultiKueue 需另加 `managedBy` |
| `ray-job.yaml` | 最小 RayJob | 无（entrypoint 是自包含的） |
| `ray-job-http.yaml` | HTTPMode | operator 能访问 head Dashboard |
| `ray-job-sidecar.yaml` | SidecarMode | Ray 2.54+、K8s 1.35+（容器重启 gate） |
| `ray-job-interactive.yaml` | InteractiveMode | 用户手动提交 job 并写 `spec.jobId` |
| `ray-job-cluster-selector.yaml` | 复用集群 | 先创建 `raycluster-sample` |
| `ray-job-deletion-rules.yaml` | deletion rules | 无 |
| `ray-job-train.yaml` | Ray Train 模板 | GPU 节点；entrypoint 需替换为真实训练脚本 |
| `ray-job-data.yaml` | Ray Data 模板 | 对象存储凭据；entrypoint 需替换 |
| `ray-service.yaml` | RayService | 能下载 `runtime_env.working_dir` 的代码 |
| `ray-service-incremental.yaml` | 增量升级 | Gateway API CRD/GatewayClass、autoscaling、代码 URL |
| `ray-service-canary.yaml` | adopt HTTPRoute | 已存在 `platform-ray-route`、Gateway API |
| `ray-service-llm.yaml` | LLM 推理 | `rayproject/ray-llm` 镜像、GPU、HF/模型存储 |
| `ray-cronjob.yaml` | RayCronJob | `RayCronJob` gate |

## 使用顺序

```bash
# 1. 安装 operator（见第 26 章）
helm install kuberay-operator kuberay/kuberay-operator -n kuberay-system --create-namespace

# 2. 先跑最小集群
kubectl apply -f ray-cluster.yaml
kubectl get rayclusters -w

# 3. 再按需启用 gate/外部组件
kubectl apply -f ray-job.yaml
```

## 注意

- 样例中的镜像 tag 固定为 Ray 2.59.0；升级时同步修改。
- GPU 样例假设节点有 `nvidia.com/gpu.product=NVIDIA-A100-SXM4-80GB` 标签；用 `kubectl get nodes --show-labels` 确认，或改成你的机型。
- GCS FT/NetworkPolicy/mTLS/History Server 需要 feature gate，默认关闭。
- 带 `runtime_env.working_dir` 的样例需要节点能访问 URL；离线环境换成内部地址。
- `rayproject/ray` 不含示例业务代码；训练/数据/LLM 样例需要自己的镜像或代码。
