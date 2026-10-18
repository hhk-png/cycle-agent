# 附录 C. 故障排查手册

## C.0 按症状查

| 症状 | 先看 |
|---|---|
| RayCluster 一直不 Ready | C.2；时序图 J.1 |
| Pod Pending（节点容量/配额/Spot） | C.3；第 6、36、51 章；J.17 |
| ImagePullBackOff | C.4 |
| CrashLoopBackOff | C.5 |
| worker 无法加入 | C.6、C.19；J.1 |
| RayJob 卡在 Initializing/Waiting/Running | C.7；J.2–J.5 |
| RayService 不 Ready | C.8；J.6 |
| 升级卡住/不切流 | C.9、C.16；J.6、J.7 |
| 删除卡住（finalizer）/优雅终止 | C.10；第 52 章；J.14、J.18 |
| operator 自身异常/重启风暴 | C.11；第 54 章；J.10、J.19 |
| 网络/DNS/Service | C.12、C.19；第 40 章 |
| GPU/资源 | C.13；第 36 章 |
| History Server 无数据 | C.15 |
| Gateway/增量升级不生效 | C.16 |
| 批调度 Pod 一直 Pending | C.17；第 29 章 |
| 规模化/控制面压力 | C.20；第 41 章 |
| task 一直 PENDING/actor 重启/现场取证 | 第 56 章；`ray status`、`ray list` |
| 改镜像/副本/模板后异常 | 第 55 章；spec hash、`upgradeStrategy` |
| IPPR 不 resize/resize 失败/回滚 | C.21；第 57 章 |

## C.1 一分钟定位流程

```text
1. kubectl get raycluster/rayjob/rayservice
2. kubectl describe <kind>/<name>        # events/conditions
3. kubectl get pods -l ray.io/cluster=<name> -o wide
4. kubectl logs <pod> [-c ray|autoscaler|init]
5. kubectl -n kuberay-system logs deploy/kuberay-operator
6. kubectl exec <head> -- ray status / ray list nodes
7. 资源/配额/镜像/网络/RBAC
```

完整的五层证据链（status → Pod → Ray 节点 → 应用 → 日志）见[第 56 章](56-live-diagnostics-state-api.md)。

## C.2 RayCluster 不 Ready

### 症状

```text
NAME    STATUS   DESIRED WORKERS   AVAILABLE WORKERS
demo            2                 0
```

### 检查

```bash
kubectl get raycluster demo -o jsonpath='{.status.conditions}' | jq
kubectl describe pod demo-head
kubectl describe pod -l ray.io/cluster=demo
kubectl get svc demo-head-svc -o yaml
```

### 常见原因

| 现象 | 原因 | 处理 |
|---|---|---|
| head Pending | CPU/内存/GPU 不足、taint、affinity | 看 describe events |
| head ImagePullBackOff | 镜像/tag/Secret/网络 | 修镜像 |
| head CrashLoopBackOff | 命令/权限/端口/探针 | 看 logs |
| worker Pending | 资源/配额/亲和性 | 扩容/改 request |
| worker 无法加入 | head DNS/6379/NetworkPolicy | exec 测试 |
| Service 无 endpoint | selector 不匹配 | 看 labels |
| status 不更新 | operator 权限/崩溃 | 看 operator logs |

## C.3 Pod Pending

```bash
kubectl describe pod <pod>
```

关注 Events：

- `0/3 nodes are available: 3 Insufficient cpu` -> 扩容/降 request；
- `node(s) had untolerated taint` -> 加 toleration；
- `node affinity/selector mismatch` -> 改 selector；
- `exceeded quota` -> 调 ResourceQuota；
- `volume node affinity conflict` -> 存储拓扑；
- `PodSecurity` -> securityContext。

如果原因指向“节点不够”，继续查节点层（第 51 章）：

```bash
kubectl get nodepool -o yaml | grep -E 'name:|maxSize|limits|consolidation'
kubectl -n kube-system logs deploy/cluster-autoscaler --tail=100
kubectl get events -A --field-selector reason=NotTriggerScaleUp
kubectl get resourcequota -A
```

Ray 侧同时看 `ray status` 与 `replicas`，确认 demand 是否真的变成了 Pod。

## C.4 ImagePullBackOff

```bash
kubectl describe pod <pod> | grep -A5 Events
kubectl get secret <pull-secret> -o yaml
```

- 镜像名/tag；
- 私有仓库认证；
- 节点网络/DNS；
- 架构（arm64/amd64）；
- 仓库限流；
- 镜像被删除。

## C.5 CrashLoopBackOff

```bash
kubectl logs <pod> --previous
kubectl logs <pod> -c ray
kubectl exec <pod> -- sh -c 'echo $RAY_CLUSTER_NAME; env | sort'
```

- 命令参数错误；
- 端口占用；
- 权限/只读文件系统；
- 探针失败；
- OOMKilled（`kubectl describe` 看 last state）；
- GCS/Redis 不可达。

## C.6 Worker 无法加入

```bash
kubectl exec -it <worker> -- getent hosts demo-head-svc
kubectl exec -it <worker> -- nc -zv demo-head-svc 6379
kubectl logs <worker> -c wait-gcs-ready
```

- NetworkPolicy 阻断 DNS/6379；
- head Service selector 错；
- head 未 Ready 且 publishNotReadyAddresses=false；
- token/mTLS 不匹配；
- Ray 版本不一致；
- init container 卡住。

## C.7 RayJob 卡住

### Initializing

- RayCluster 不 Ready；
- 资源不足；
- 镜像拉取；
- `preRunningDeadlineSeconds` 未设置。

### Running 但无进展

- Dashboard 不可达（HTTPMode）；
- submitter 失败（K8sJobMode）；
- job ID 不存在；
- 作业在排队；
- autoscaler 无容量。

### 不结束

- entrypoint 不退出；
- `activeDeadlineSeconds` 未设置；
- Ray job 状态检查失败；
- submitter 卡住。

## C.8 RayService 不 Ready

```bash
kubectl get rayservice <name> -o yaml
kubectl get pods -l ray.io/cluster=<active>
kubectl exec <head> -- ray serve status
kubectl get svc <name>-serve-svc -o yaml
```

- Serve app DEPLOY_FAILED；
- deployment UNHEALTHY；
- `ray.io/serve` 未变 true；
- Serve Service selector 不匹配；
- endpoint 0；
- NetworkPolicy 阻断 proxy；
- pending 集群资源不足。

## C.9 升级卡住

- pending cluster 不 Ready；
- Serve app 不 RUNNING；
- Gateway/HTTPRoute 未就绪（增量）；
- 资源配额不足；
- 旧集群未删除（finalizer/延迟）；
- 回滚条件未满足。

## C.10 删除卡住

```bash
kubectl get <kind> <name> -o jsonpath='{.metadata.finalizers}'
kubectl get pods -l ray.io/cluster=<name>
kubectl get jobs
```

- GCS FT Redis cleanup；
- RayService 等待 RayCluster；
- RayJob 等待 stop；
- ownerReference 循环；
- CRD finalizer 逻辑 bug。

处理顺序：

1. 查 operator logs；
2. 手动删除卡住的 Pod/Job；
3. 确认 Redis cleanup；
4. 必要时移除 finalizer（记录风险）。

## C.11 Operator 问题

```bash
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=300
kubectl -n kuberay-system get events --sort-by=.lastTimestamp
kubectl auth can-i create pods --as=system:serviceaccount:kuberay-system:kuberay-operator
```

- RBAC；
- leader election；
- CRD 未安装/版本旧；
- webhook 证书；
- 资源限制/ OOM；
- API Server 限流。

## C.12 网络

```bash
kubectl exec -it <pod> -- nc -zv <service> <port>
kubectl exec -it <pod> -- nslookup <service>
kubectl get networkpolicy
```

- DNS；
- NetworkPolicy；
- Service selector；
- headless vs ClusterIP；
- 跨 namespace FQDN；
- mTLS。

## C.13 资源/GPU

```bash
kubectl describe node <node> | grep -A10 Allocated
kubectl get pods -A -o json | jq ...
nvidia-smi
kubectl logs <pod> | grep -i cuda
```

- GPU 未分配；
- 驱动/CUDA 不匹配；
- device plugin；
- 节点碎片；
- limit/request 不一致。

## C.14 日志位置

| 组件 | 位置 |
|---|---|
| operator | `kubectl logs deploy/kuberay-operator` |
| Ray head/worker | `kubectl logs <pod> -c ray` |
| autoscaler | `kubectl logs <head> -c autoscaler` |
| init | `kubectl logs <pod> -c wait-gcs-ready` |
| Ray 内部 | `/tmp/ray/session_latest/logs/` |
| Serve | `/tmp/ray/session_latest/logs/serve/` |
| K8s events | `kubectl get events` |

## C.15 History Server 无数据

```bash
kubectl get pods -l app.kubernetes.io/name=ray-history-server
kubectl logs deploy/ray-history-server
kubectl exec <head> -c ray-history-collector -- env | grep -i history
kubectl get raycluster <name> -o yaml | grep -A10 historyServerOptions
```

- `RayClusterHistoryServer` gate 未开；
- collector sidecar 没有存储权限/桶不存在；
- 存储端点不可达（S3/GCS/Azure/OSS）；
- 集群删除太快，collector 还没 flush；
- History Server 与 collector 用了不同的 bucket/prefix；
- 保留策略把对象删了。

## C.16 Gateway/增量升级不生效

```bash
kubectl get gateway,httproute -A
kubectl describe httproute <name>
kubectl get rayservice <name> -o yaml | grep -A20 pendingServiceStatus
kubectl get svc <active>-serve-svc <pending>-serve-svc
```

- Gateway API CRD 未安装/版本太低；
- GatewayClass 没有可用实现；
- `gatewayClassName` 与 `httpRouteName` 同时设置（仅 MiniKubeRay 的校验会拒绝；上游没有 `httpRouteName`）；
- pending 集群未 Ready/Serve 未 RUNNING；
- HTTPRoute `backendRefs` 指向的 Service 不存在；
- 权重为 0/100 但 EndpointSlice 为空；
- 接管模式下 operator 没有 patch 权限。

## C.17 批调度 Pod 一直 Pending

```bash
kubectl get podgroup -A
kubectl describe podgroup <name>
kubectl get queue -A
kubectl get pods -o wide | grep Pending
kubectl describe pod <pending> | grep -A10 Events
```

- operator 没有 `--batch-scheduler=volcano`；
- PodGroup 的 `minMember` 大于队列可容纳资源；
- 队列 capability 不足；
- `schedulerName` 与已安装调度器不匹配；
- 节点池没有对应 GPU/机型；
- 多个 gang 调度器冲突。

## C.18 MiniKubeRay 排查

```bash
python -m minikuberay demo --fast
python -m minikuberay server
python -m minikuberay get rayclusters
python -m minikuberay describe raycluster/demo
python -m minikuberay logs rayjob/job
```

- 测试失败：先看 `tests/` 的断言；
- 状态不收敛：`operator.run_once()` 是否返回 True；
- watch 卡住：Store 的 resourceVersion；
- 时区问题：Windows 无 tzdata，使用 UTC 或固定偏移回退；
- API 403：`--token`/`--read-only`。

## C.19 网络与 DNS

```bash
kubectl exec <pod> -- cat /etc/resolv.conf
kubectl exec <pod> -- getent hosts <svc>
kubectl exec <pod> -- nc -zv <svc> 6379
kubectl get endpointslices -l kubernetes.io/service-name=<svc> -o yaml
kubectl get networkpolicy -A
kubectl debug <pod> -it --image=nicolaka/netshoot -- tcpdump -ni eth0 port 6379
```

- DNS 被 NetworkPolicy 阻断；
- headless Service 无 endpoint；
- 只放行 6379，Ray 动态端口被拦；
- MTU 过大导致大包卡住；
- Gateway/Ingress 超时/缓冲。

详见[第 40 章](40-kubernetes-network-internals.md)。

## C.20 规模化问题

```bash
kubectl -n kuberay-system top pod
kubectl -n kuberay-system logs deploy/kuberay-operator | grep -iE 'error|timeout|429|conflict'
kubectl get --raw /metrics | grep controller_runtime
kubectl get rayclusters -A --chunk-size=100
```

- operator 内存/缓存过大；
- API 429/超时；
- 升级/重启后调和风暴；
- 单 key 热点；
- 指标基数爆炸。

详见[第 41 章](41-control-plane-at-scale.md)。

## C.21 IPPR 不生效或 resize 失败

按“配置 -> 调度 -> kubelet -> Ray”四层看：

```bash
# 1) 注解是否合法（group 名、三个字段、max 是否高于当前 request）
kubectl get raycluster <name> -o jsonpath='{.metadata.annotations.ray\.io/ippr}'
kubectl describe raycluster <name> | grep -A3 -i validation

# 2) 前置条件：autoscaler v2、没有 num-cpus/memory、resizePolicy
kubectl get raycluster <name> -o jsonpath='{.spec.autoscalerOptions.version}'
kubectl get raycluster <name> -o jsonpath='{.spec.workerGroupSpecs[*].rayStartParams}'
kubectl get raycluster <name> -o jsonpath='{.spec.workerGroupSpecs[*].template.spec.containers[0].resizePolicy}'

# 3) Pod 的 resize 状态
kubectl get pod <pod> -o jsonpath='{.metadata.annotations.ray\.io/ippr-status}'
kubectl describe pod <pod> | grep -A5 -i resize

# 4) Raylet 是否看到新容量
kubectl exec <head> -- ray status
```

常见原因：

- `max-cpu` 低于当前 request：校验直接失败；
- `rayStartParams.num-cpus` 存在：Pod 变大但 Ray 仍只看到旧值；
- `resizePolicy: RestartContainer`：IPPR 拒绝；
- 节点没有 allocatable 余量：resize 失败/回滚，随后走水平扩容；
- kubelet/运行时不是 1.35+ 或不是 cgroup v2：控制面接受、数据面不生效；
- VPA 同时改同一个 Pod：两个写入者打架。

完整状态机与生产限制见[第 57 章](57-in-place-pod-resizing-ippr.md)。

更多事件/错误字符串见[附录 H](appendix-h-events-errors-status.md)；真实案例见[第 37 章](37-production-incident-postmortems.md)。

← [返回目录](README.md)
