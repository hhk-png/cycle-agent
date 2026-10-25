# 附录 F. 真实集群实验命令速查

配合[第 26 章](kuberay教程-26-真实集群实验-从kind到第一个RayService.md)使用。所有命令假设 namespace 为 `default`，operator 在 `kuberay-system`。

## F.1 集群与安装

```bash
# kind
kind create cluster --name kuberay-lab --config kind-kuberay.yaml
kubectl config use-context kind-kuberay-lab
kubectl get nodes -o wide

# Helm
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update
helm install kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system --create-namespace
helm list -A
kubectl -n kuberay-system get pods,deploy

# 检查 CRD
kubectl get crd | grep ray.io
kubectl explain raycluster.spec --recursive | head -50
```

## F.2 RayCluster

```bash
kubectl apply -f manifests/samples/ray-cluster.yaml
kubectl get rayclusters -w
kubectl describe raycluster raycluster-sample
kubectl get pods -l ray.io/cluster=raycluster-sample -o wide --show-labels
kubectl get svc -l ray.io/cluster=raycluster-sample
kubectl get endpointslices -l kubernetes.io/service-name=raycluster-sample-head-svc

# 端口转发
kubectl port-forward svc/raycluster-sample-head-svc 8265:8265 10001:10001 8000:8000

# 进 head（Pod 名带随机后缀，用 selector 解析）
HEAD_POD=$(kubectl get pod -l ray.io/cluster=raycluster-sample,ray.io/node-type=head \
  -o jsonpath='{.items[0].metadata.name}')
kubectl exec -it "$HEAD_POD" -- bash
ray status
ray list nodes
ray list actors
ray job list
```

## F.3 RayJob

```bash
kubectl apply -f manifests/samples/ray-job.yaml
kubectl get rayjobs -w
kubectl describe rayjob rayjob-sample
kubectl get pods,jobs -l ray.io/originated-from-cr-name=rayjob-sample
kubectl logs job/rayjob-sample-submitter

# 作业日志（需要 head Pod 名）
kubectl exec -it <head-pod> -- ray job logs <job-id>
kubectl exec -it <head-pod> -- ray job status <job-id>
kubectl exec -it <head-pod> -- ray job stop <job-id>

# 状态字段
kubectl get rayjob rayjob-sample -o jsonpath='{.status.jobId}{"\n"}{.status.jobDeploymentStatus}{"\n"}{.status.jobStatus}{"\n"}'
```

## F.4 RayService

```bash
kubectl apply -f manifests/samples/ray-service.yaml
kubectl get rayservices -w
kubectl get rayclusters -l ray.io/originated-from-crd=RayService
kubectl get pods -l ray.io/cluster=<active-cluster> -o wide
kubectl exec -it <head-pod> -- ray serve status

kubectl port-forward svc/rayservice-sample-serve-svc 8000:8000
curl -s http://127.0.0.1:8000/text

# 升级观察
kubectl get rayservice rayservice-sample -o yaml | grep -A30 status
kubectl get httproute -A
kubectl get gateway -A
```

## F.5 RayCronJob

```bash
# 先确认 gate
helm upgrade kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system --reuse-values \
  --set 'featureGates[0].name=RayCronJob' \
  --set 'featureGates[0].enabled=true'

kubectl apply -f manifests/samples/ray-cronjob.yaml
kubectl get raycronjobs
kubectl get rayjobs -l ray.io/cronjob-name=raycronjob-sample
kubectl get raycronjob raycronjob-sample -o jsonpath='{.status.lastScheduleTime}{"\n"}'
```

## F.6 日志与事件

```bash
# operator
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=200
kubectl -n kuberay-system logs deploy/kuberay-operator -f

# Ray 容器
kubectl logs <head-pod> -c ray
kubectl logs <worker-pod> -c ray
kubectl logs <head-pod> -c autoscaler
kubectl logs <worker-pod> -c wait-gcs-ready

# Ray 内部日志
kubectl exec -it <head-pod> -- ls /tmp/ray/session_latest/logs
kubectl exec -it <head-pod> -- tail -n 200 /tmp/ray/session_latest/logs/raylet.out
kubectl exec -it <head-pod> -- tail -n 200 /tmp/ray/session_latest/logs/gcs_server.out

# 事件
kubectl get events --sort-by=.lastTimestamp | tail -40
kubectl describe raycluster <name> | sed -n '/Events/,$p'
```

## F.7 调试网络

```bash
kubectl exec -it <worker-pod> -- getent hosts <head-svc>
kubectl exec -it <worker-pod> -- nc -zv <head-svc> 6379
kubectl exec -it <worker-pod> -- nc -zv <head-svc> 8265
kubectl get networkpolicy -A
kubectl get svc <head-svc> -o yaml
kubectl get endpointslices -l kubernetes.io/service-name=<head-svc> -o yaml
```

## F.8 资源与 GPU

```bash
kubectl describe node <node> | sed -n '/Allocated resources/,/Events/p'
kubectl get pods -A -o wide | grep Pending
kubectl describe pod <pending-pod> | sed -n '/Events/,$p'
kubectl get nodes --show-labels | tr ',' '\n' | grep -E 'nvidia|gpu'
kubectl exec -it <gpu-pod> -- nvidia-smi
kubectl top pods
```

## F.9 升级与回滚

```bash
# 备份
kubectl get rayclusters,rayjobs,rayservices,raycronjobs -A -o yaml > backup.yaml

# CRD（Helm 不会自动升级）
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/ray-project/kuberay/v1.7.1/ray-operator/config/crd/bases/ray.io_rayclusters.yaml

# operator
helm upgrade kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system
kubectl -n kuberay-system rollout status deploy/kuberay-operator

# 回滚
helm rollback kuberay-operator -n kuberay-system
```

## F.10 清理

```bash
kubectl delete -f manifests/samples/ --ignore-not-found
helm uninstall kuberay-operator -n kuberay-system
kubectl delete namespace kuberay-system --ignore-not-found
kind delete cluster --name kuberay-lab
```

## F.11 MiniKubeRay 对照命令

```bash
python -m minikuberay demo --fast
python -m minikuberay server --history-path ./history.json
python -m minikuberay apply -f manifests/samples/ray-cluster.yaml
python -m minikuberay get rayclusters
python -m minikuberay get nodes -c raycluster-sample
python -m minikuberay get workergroups -c raycluster-sample
python -m minikuberay describe raycluster/raycluster-sample
python -m minikuberay logs rayjob/rayjob-sample
python -m minikuberay history list
python -m minikuberay history replay default/raycluster-sample
python -m minikuberay delete raycluster/raycluster-sample --force
```

## F.12 版本检查

```bash
kubectl version
helm version
kubectl -n kuberay-system get deploy kuberay-operator -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl get crd rayclusters.ray.io -o jsonpath='{.spec.versions[*].name}{"\n"}'
kubectl exec -it <head-pod> -- ray --version
kubectl exec -it <head-pod> -- python -c "import ray; print(ray.__version__)"
```

## F.13 GPU/DRA 检查

```bash
# 节点 GPU 容量/可分配
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu

# GFD 标签
kubectl get nodes --show-labels | tr ',' '\n' | grep -E 'nvidia|mig'

# device plugin
kubectl -n kube-system get pods | grep -E 'nvidia|device-plugin|gpu-operator'
kubectl -n kube-system logs <device-plugin-pod> --tail=100

# Pod 分配
kubectl get pod <pod> -o jsonpath='{.spec.containers[0].resources}'
kubectl exec <pod> -- nvidia-smi
kubectl exec <pod> -- env | grep -E 'CUDA_VISIBLE|NVIDIA_VISIBLE'

# DRA（如果集群启用）
kubectl get deviceclass,resourceslice,resourceclaim -A
kubectl describe resourceclaim <name>
```

## F.13.1 网络与 DNS

```bash
# 解析与端口
kubectl exec <pod> -- cat /etc/resolv.conf
kubectl exec <pod> -- getent hosts <svc>
kubectl exec <pod> -- dig +short <fqdn>
kubectl exec <pod> -- nc -zv <svc> 6379

# Service/EndpointSlice
kubectl get svc <svc> -o yaml
kubectl get endpointslices -l kubernetes.io/service-name=<svc> -o yaml

# NetworkPolicy
kubectl get networkpolicy -A
kubectl describe networkpolicy <name>

# 抓包
kubectl debug <pod> -it --image=nicolaka/netshoot -- \
  tcpdump -ni eth0 host <peer-ip> and port 6379

# MTU
kubectl exec <pod> -- ip link show eth0
kubectl exec <pod> -- ping -M do -s 1472 <peer-ip>
```

## F.13.2 控制面规模

```bash
python examples/scale_benchmark.py --clusters 100 --workers 2
python examples/scale_benchmark.py --clusters 200 --workers 1 --json

kubectl -n kuberay-system top pod
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=200 | \
  grep -iE 'error|timeout|429|conflict'
kubectl get --raw /metrics | grep -E 'controller_runtime|workqueue'
```

## F.14 故障演练命令

```bash
# 删除 head/worker
kubectl delete pod <head>
kubectl delete pod <worker>

# 制造 Pending（GPU 请求过大）
kubectl patch raycluster <name> --type=json \
  -p='[{"op":"replace","path":"/spec/workerGroupSpecs/0/template/spec/containers/0/resources/requests/nvidia.com~1gpu","value":"8"}]'

# 镜像拉取失败
kubectl patch raycluster <name> --type=json \
  -p='[{"op":"replace","path":"/spec/headGroupSpec/template/spec/containers/0/image","value":"rayproject/ray:does-not-exist"}]'

# 删除 autoscaler RBAC（观察 403）
kubectl delete role <cluster>-autoscaler -n <ns>

# 查看 finalizer
kubectl get raycluster <name> -o jsonpath='{.metadata.finalizers}'

# 采集证据
kubectl get raycluster,rayjob,rayservice,raycronjob -A -o yaml > crs.yaml
kubectl get events -A --sort-by=.lastTimestamp > events.txt
```

演练后记得恢复配置，并记录：

- 预期信号；
- 实际信号；
- 恢复时间；
- 需要新增的告警/测试。

## F.15 多可用区与单区故障演练

```bash
# 1) 看每个 AZ 的节点与 Ray Pod 分布
kubectl get nodes -L topology.kubernetes.io/zone
kubectl get pods -A -l ray.io/is-ray-node=true \
  -o custom-columns='NS:.metadata.namespace,POD:.metadata.name,NODE:.spec.nodeName'
# Pod 的 nodeSelector 通常为空，AZ 是 Node 标签；把上面两张表按 NODE 名字手工/脚本 join。

# 2) 看拓扑打散/PDB
kubectl get raycluster <name> -o yaml | grep -A12 topologySpreadConstraints
kubectl get pdb -A

# 3) 模拟单区不可调度（只对专用实验集群做）
kubectl get nodes -l topology.kubernetes.io/zone=<zone> -o name | xargs -r kubectl cordon
kubectl get nodes -l topology.kubernetes.io/zone=<zone> -o name | \
  xargs -r kubectl drain --ignore-daemonsets --delete-emptydir-data

# 4) 观察 Pending/重建/重连
kubectl get pods -A -o wide | grep -E 'Pending|CrashLoop|Terminating'
kubectl get events -A --sort-by=.lastTimestamp | tail -50
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=200

# 5) 恢复
kubectl get nodes -l topology.kubernetes.io/zone=<zone> -o name | xargs -r kubectl uncordon
```

不要在生产共享集群上直接 drain 整个区；真实故障注入请使用云厂商的故障注入服务，并提前通知业务方。验收记录见[第 44 章](kuberay教程-44-多可用区、托管Kubernetes与灾难恢复.md)的检查表。

## F.16 一页排障顺序

```text
1. CR 存在吗？        kubectl get raycluster/rayjob/rayservice
2. status/conditions  kubectl get ... -o yaml
3. Pod 存在/Ready？   kubectl get pods -l ray.io/cluster=...
4. 调度失败？         kubectl describe pod <pending>
5. 镜像失败？         kubectl describe pod | grep -A5 Events
6. 启动失败？         kubectl logs <pod> -c ray / init
7. worker 不加入？    DNS/6379/NetworkPolicy/token/mTLS
8. operator 报错？    kubectl -n kuberay-system logs deploy/kuberay-operator
9. 资源/配额？        ResourceQuota/Node/Pending
10. 删除卡住？        finalizers/Redis cleanup/ownerRef
```

## F.17 节点供给、驱逐与控制面

```bash
# 1) Pending 的真实原因
kubectl get pods -A --field-selector=status.phase=Pending
kubectl describe pod <pod> | sed -n '/Events/,$p'

# 2) 节点池上限 / 自动伸缩
kubectl get nodepool -o yaml | grep -E 'name:|maxSize|limits|consolidation'
kubectl get events -A --field-selector reason=NotTriggerScaleUp
kubectl -n kube-system logs deploy/cluster-autoscaler --tail=100

# 3) 配额
kubectl get resourcequota -A
kubectl describe resourcequota team-a -n team-a

# 4) 驱逐/PDB
kubectl get pdb -A
kubectl describe pdb <pdb>
kubectl get events -A --field-selector reason=Eviction

# 5) 优雅终止
POD=<pod>
kubectl get pod "$POD" -o jsonpath='{.spec.terminationGracePeriodSeconds}{"\n"}'
kubectl get pod "$POD" -o jsonpath='{.spec.containers[0].lifecycle}{"\n"}'
kubectl get pod "$POD" -o jsonpath='{.metadata.finalizers}{"\n"}'

# 6) operator 选主/重启
kubectl -n kuberay-system get lease
kubectl -n kuberay-system get pods -l app.kubernetes.io/name=kuberay-operator
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=200 | grep -i 'leader\|sync\|panic'
```

对应章节：第 51（节点弹性）、52（终止/PDB）、54（operator 可靠性）。

## F.18 IPPR 与 Kubernetes 新原语

```bash
# 1) IPPR 配置与状态
kubectl get raycluster <name> -o jsonpath='{.metadata.annotations.ray\.io/ippr}'
kubectl get pods -l ray.io/cluster=<name> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.ray\.io/ippr-status}{"\n"}{end}'

# 2) 原地 resize 的 K8s 视角
kubectl get pod <pod> -o jsonpath='{.status.conditions[?(@.type=="PodResizeInProgress")]}'
kubectl describe pod <pod> | grep -A5 -i resize
kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[?(@.name=="ray")].resources}'

# 3) Ray 是否真的看到了新容量
kubectl exec <head> -- ray status

# 4) 原生 sidecar 顺序
kubectl get pod <pod> -o jsonpath='{.spec.initContainers[*].name}{"\n"}'
kubectl get pod <pod> -o jsonpath='{.status.initContainerStatuses[*].ready}{"\n"}'

# 5) 调度门
kubectl get pod <pod> -o jsonpath='{.spec.schedulingGates}'
kubectl get pod <pod> -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")]}'

# 6) 拓扑路由
kubectl get svc <serve-svc> -o jsonpath='{.spec.trafficDistribution}'
kubectl get endpointslice -l kubernetes.io/service-name=<serve-svc> -o yaml | grep -A3 -i zone
```

对应章节：第 57（IPPR）、58（原生 sidecar/调度门/拓扑路由/Pod 级资源）。

安装参数见[附录 G](kuberay教程-65-附录G-Helm-Values与安装矩阵.md)，错误目录见[附录 H](kuberay教程-66-附录H-事件、错误与状态码目录.md)，自测题见[附录 I](kuberay教程-67-附录I-自测题与答案.md)，时序图见[附录 J](kuberay教程-68-附录J-端到端时序图集.md)，可运行实验索引见[附录 K](kuberay教程-69-附录K-可运行实验与验证索引.md)。

← [返回目录](README.md)
