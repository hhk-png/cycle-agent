# 第 31 章 故障排查方法论

## 31.1 先分层，再动手

Kubernetes 故障通常可以按层定位：

```text
1. 用户/客户端
2. DNS
3. Ingress/Gateway/LB
4. Service/Endpoints
5. Pod/容器
6. 节点/kubelet/运行时
7. 控制面/API/etcd
8. 网络/存储/云基础设施
```

不要一上来就 `kubectl delete pod`。先回答：

- 影响范围：一个 Pod、一个服务、一个节点、一个可用区、整个集群？
- 开始时间：与发布/配置/扩缩容/节点变更是否相关？
- 症状：4xx/5xx、超时、连接拒绝、DNS 失败、Pending、CrashLoop？
- 最近变更：代码、镜像、配置、RBAC、网络策略、证书？

## 31.2 第一组命令

```bash
kubectl get pods -A -o wide
kubectl get events -A --sort-by=.lastTimestamp
kubectl get nodes
kubectl get deployments -A
kubectl get svc,endpoints -A
kubectl get pod <pod> -o yaml
kubectl describe pod <pod>
kubectl logs <pod> --previous
```

关注：

- Pod phase / Ready / restarts。
- Event 的 reason/message。
- Node 条件。
- Deployment available/updated。
- Endpoints 是否为空。
- 容器 lastState（OOM/退出码）。

## 31.3 Pod Pending

决策树：

```text
Pod Pending
├── 有 nodeName? 
│   ├── 否 → 调度失败：看 FailedScheduling 事件
│   │      ├── 资源不足
│   │      ├── 污点/容忍
│   │      ├── nodeSelector/affinity
│   │      ├── PVC/拓扑
│   │      ├── hostPort
│   │      └── 所有节点 NotReady/unschedulable
│   └── 是 → kubelet 未启动容器
│          ├── 镜像拉取
│          ├── 卷挂载
│          ├── CNI/IPAM
│          └── 运行时错误
└── 有 deletionTimestamp? → 卡在 finalizer/优雅终止
```

命令：

```bash
kubectl describe pod <pod> | sed -n '/Events/,$p'
kubectl get pod <pod> -o jsonpath='{.spec.nodeName}'
kubectl get node <node> -o yaml
kubectl get pvc -n <ns>
```

## 31.4 CrashLoopBackOff

```bash
kubectl logs <pod> --previous
kubectl describe pod <pod>
kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[*].lastState}'
```

常见原因：

- 应用启动失败（配置/依赖/权限）。
- OOMKilled（退出码 137）。
- liveness 探针误杀。
- 命令/参数错误。
- 镜像 entrypoint 不对。
- 只读文件系统/权限。
- 证书/Secret 缺失。

区分：

- `Error` + exit 1：应用错误。
- `OOMKilled` + 137：内存 limit 太小或泄漏。
- `Completed` + restart：一次性任务用了 Always。
- `ContainerCannotRun`：运行时/权限/架构问题。

## 31.5 ImagePullBackOff

```bash
kubectl describe pod <pod> | grep -A5 "Failed"
```

原因：

- 镜像名/tag 错。
- 私有仓库未配置 imagePullSecret。
- 凭据过期。
- 仓库限流/网络不通。
- 节点无法访问仓库。
- 架构不匹配（arm64/amd64）。
- 镜像被删除。

排查：

```bash
crictl pull <image>       # 在节点上
crictl images
curl -I https://registry.example.com/v2/
```

## 31.6 Service 不通

决策树：

先分成两个完全不同的分支：**Endpoints 为空**（对象/控制器层）和 **Endpoints 有地址但连不上**（数据面/应用层）。

```text
Service 不通
├── Endpoints/EndpointSlice 为空?
│   ├── selector 与 Pod labels 不匹配
│   ├── Pod 存在但 NotReady（探针/启动失败）
│   ├── targetPort 是命名端口，但容器没有同名 containerPort
│   ├── Service 没有 selector（手工 Endpoints 未创建/为空）
│   └── namespace/Service 名写错
├── Endpoints 有地址但连不上?
│   ├── targetPort 数字错（Endpoints 仍有地址，只是端口不对）
│   ├── 应用监听 127.0.0.1 而不是 0.0.0.0
│   ├── NetworkPolicy/安全组/主机防火墙
│   ├── kube-proxy/conntrack/节点本地规则
│   └── Pod IP 本身不可达（CNI/路由）
├── DNS 解析失败?
│   ├── CoreDNS/NetworkPolicy 53/ndots/search 域
│   └── 解析成功但连错 Service
└── 只有部分客户端失败?
    ├── 拓扑/亲和
    ├── 会话保持
    └── 节点本地规则
```

关键区别：**数字 `targetPort` 写错不会让 Endpoints 变空**。EndpointSlice 仍会填充 Pod IP 和错误端口，表现为 connection refused/超时；只有命名 `targetPort` 找不到对应 `containerPort` 时，Endpoint 控制器才会丢掉该 Pod。把这两种情况混在一起，会把人引向错误的排查方向。

命令：

```bash
kubectl get endpoints <svc> -o yaml
kubectl get endpointslice -l kubernetes.io/service-name=<svc>
kubectl run nettest --image=nicolaka/netshoot -- sleep 3600
kubectl exec -it nettest -- nslookup <svc>
kubectl exec -it nettest -- curl -v http://<svc>:<port>
kubectl exec -it nettest -- curl -v http://<pod-ip>:<target-port>
# 对比 Service targetPort 与 EndpointSlice 实际端口
kubectl get svc <svc> -o jsonpath='{.spec.ports[*].targetPort}'
kubectl get endpointslice -l kubernetes.io/service-name=<svc> -o jsonpath='{.items[*].ports[*].port}'
# 看应用到底监听在哪个地址/端口
kubectl exec -it <pod> -- ss -lntp
```

### 31.6.1 Gateway API 排障：先看 conditions，再看 Route

Gateway API 的故障通常不在 Service，而在“Gateway 没接受/Route 没绑定/ReferenceGrant 缺失”：

```bash
kubectl get gatewayclass
kubectl get gateway -A -o yaml | yq '.items[].status.conditions'
kubectl get httproute,grpcroute -A -o yaml | yq '.items[].status.parents'
kubectl get referencegrant -A
kubectl describe gateway <name>
```

排查顺序：

1. **GatewayClass**：`Accepted=True`？控制器是否在运行、是否支持该 class。
2. **Gateway**：`Programmed=True`/`Accepted=True`？地址是否分配、Listener 是否有冲突（同端口/同 hostname）。
3. **Route**：`status.parents[].conditions` 的 `Accepted`/`ResolvedRefs`；backendRefs 是否指向存在的 Service/端口。
4. **跨 namespace**：Route 引用其他 namespace 的 Service 需要 `ReferenceGrant`；只靠 NetworkPolicy 放行不够。
5. **Listener allowedRoutes**：Gateway 的 `allowedRoutes.namespaces` 是否允许该 Route 的 namespace；`Same` 只允许同 namespace。
6. **TLS**：证书 Secret 是否存在、`hostname` 是否匹配、`BackendTLSPolicy` 是否 `Accepted`。

常见误判：`kubectl get httproute` 显示对象存在就以为生效；实际要看 `status.parents`，没有父 Gateway 的 Route 不会被任何数据面加载。

### 31.6.2 双栈与 IPv6 排障

- 确认集群/CNI 是否启用双栈；Pod 可能同时有 IPv4/IPv6，但 Service 只选了其中一个 family。
- `kubectl get svc <svc> -o jsonpath='{.spec.ipFamilies}{" "}{.spec.ipFamilyPolicy}'`。
- 应用是否监听 `::`/`0.0.0.0`；只监听 IPv4 会导致 IPv6 访问失败。
- NetworkPolicy 的 `ipBlock` 要分别覆盖两个 family；`except` 不要误伤。
- DNS 的 A/AAAA 记录、CoreDNS 的 `bind`/`template` 配置。

## 31.7 DNS 失败

```bash
kubectl exec -it <pod> -- cat /etc/resolv.conf
kubectl exec -it <pod> -- nslookup kubernetes.default
kubectl -n kube-system get pods -l k8s-app=kube-dns
kubectl -n kube-system logs -l k8s-app=kube-dns
```

原因：

- CoreDNS 不 Ready/副本不足。
- NetworkPolicy 阻止 53。
- Service 名/namespace 错。
- ndots/search 域导致解析到错误名字。
- 节点 resolv.conf/上游 DNS 故障。
- conntrack 表满/ UDP 丢包。

## 31.8 节点 NotReady

```bash
kubectl describe node <node>
ssh <node>
systemctl status kubelet
journalctl -u kubelet -n 200
crictl ps
df -h
free -m
```

原因：

- kubelet 停止/崩溃。
- 容器运行时不可用。
- 磁盘压力/内存压力/PID 压力。
- 网络插件未就绪。
- 证书过期。
- 节点到 API Server 不通。

## 31.9 控制面问题

症状：

- `kubectl` 超时/5xx。
- 无法创建/更新对象。
- 调度停止。
- 控制器不工作。

排查：

```bash
kubectl get --raw='/readyz?verbose'
kubectl get --raw='/livez?verbose'
kubectl -n kube-system logs <apiserver-pod>
kubectl -n kube-system logs <etcd-pod>
```

etcd：

```bash
etcdctl endpoint health
etcdctl endpoint status --write-out=table
```

关注：

- etcd leader/quorum。
- 磁盘 fsync 延迟。
- DB 大小/碎片。
- API Server 证书。
- 负载均衡健康检查。
- 控制面节点资源。

## 31.10 网络深度排查

节点上：

```bash
ip addr
ip route
ip link
iptables-save | grep <svc>
nft list ruleset
conntrack -L | grep <ip>
tcpdump -i any -n host <ip>
```

Pod 内：

```bash
ip addr
ip route
cat /etc/resolv.conf
ss -lntp
curl -v
tcpdump -i any -n
```

常见：

- MTU 不匹配：小包通、大包丢。
- 跨节点路由/BGP 未建立。
- 安全组/防火墙。
- NetworkPolicy 拒绝。
- conntrack 满。
- 节点本地 DNS 缓存。

## 31.11 存储问题

```bash
kubectl get pvc,pv
kubectl describe pvc <pvc>
kubectl get events --field-selector involvedObject.name=<pvc>
kubectl -n kube-system logs <csi-controller>
```

原因：

- StorageClass 不存在/默认类冲突。
- 容量不足/IOPS 限制。
- 拓扑不匹配（WFFC）。
- attach/detach 限流。
- 文件系统损坏。
- 权限（fsGroup）。
- 卷被其他节点挂载（RWO）。

## 31.12 性能问题

先区分：

- 应用慢。
- 网络慢。
- 调度慢。
- 启动慢。
- API 慢。

工具：

- `kubectl top pod/node`。
- Prometheus 指标。
- 分布式追踪。
- `kubectl exec` 里 `top`/`ss`/`curl`。
- 节点 `perf`/`bpftrace`（谨慎）。
- `kubectl debug`/ephemeral containers。

常见：

- CPU throttling。
- 内存压力/OOM。
- 连接池耗尽。
- DNS 慢。
- 下游依赖慢。
- 探针超时导致重启。
- 镜像拉取慢。
- 调度器/Pending。

## 31.13 调试工具

```bash
kubectl debug -it <pod> --image=busybox --target=<container>
kubectl debug node/<node> -it --image=busybox
kubectl exec -it <pod> -- sh
kubectl port-forward <pod> 8080:8080
kubectl auth can-i --list
kubectl api-resources
kubectl explain pod.spec
```

节点：

```bash
crictl ps -a
crictl logs <container>
crictl inspect <container>
crictl stats
systemd-cgls
```

### 31.13.1 工作负载与治理对象排障

Pod 层之外，最常见的“控制器不工作”有固定证据链。先看 condition/status，再看事件，最后才看控制器日志。

| 对象 | 卡住的表现 | 第一证据 | 常见根因 | 处理 |
|---|---|---|---|---|
| Deployment | rollout 不前进 | `kubectl rollout status`、`Progressing` condition、`ProgressDeadlineExceeded` | 新 RS 创建失败、配额、探针、镜像、PDB/容量不足 | `kubectl describe deploy`、`get rs`、`get events` |
| ReplicaSet | 副本数不达标 | `status.replicas/readyReplicas`、`FailedCreate` 事件 | 配额/LimitRange/PSA 拒绝、模板不合法 | 看 RS events，修正模板/配额 |
| HPA | 不扩容/不缩容 | `ScalingActive`、`AbleToScale`、`ScalingLimited` condition | metrics 缺失、目标容器没有 requests、已达 maxReplicas、指标延迟 | `kubectl describe hpa`、检查 metrics-server/Adapter |
| Job | 一直 Active 或 Failed | `status.conditions`（`BackoffLimitExceeded`/`DeadlineExceeded`）、Pod 日志 | 应用失败、重试策略、索引/完成数配置 | 看 Job events 与失败 Pod 的 previous logs |
| CronJob | 不触发/堆积 | `status.active`、`lastScheduleTime`、Events | 时区、`Forbid`、`startingDeadlineSeconds`、错过 100 次 | `kubectl describe cronjob`，检查 concurrencyPolicy |
| PDB | `drain` 卡住 | `status.disruptionsAllowed=0` | 不健康 Pod 占预算、副本不足 | 修 Pod 或设 `unhealthyPodEvictionPolicy: AlwaysAllow` |
| ResourceQuota | 创建被拒 | `exceeded quota` 事件、`status.used`/`hard` | 配额不足、LimitRange 默认值推高用量 | `kubectl describe quota`，调整请求/配额 |

命令组合：

```bash
kubectl rollout status deploy/<name>
kubectl describe deploy/<name> | sed -n '/Conditions/,$p'
kubectl describe hpa/<name>
kubectl get job/<name> -o jsonpath='{.status.conditions}'
kubectl get cronjob/<name> -o jsonpath='{.status}'
kubectl get pdb/<name> -o jsonpath='{.status}'
kubectl describe quota
```

一个常见误判：HPA 显示 `ScalingLimited=True` 并不一定是故障，可能只是已经到 `maxReplicas`；`ScalingActive=False` 才说明指标/目标配置有问题。Deployment 的 `ProgressDeadlineExceeded` 也不代表控制器坏了，通常要回到新 RS/Pod 找失败原因。

## 31.14 事故处理原则

1. 先止血：回滚、扩容、切流、降级。
2. 保留证据：日志、事件、指标、现场。
3. 明确沟通：影响、进展、预计恢复时间。
4. 不要在生产上做未验证的“试试看”。
5. 恢复后复盘：根因、时间线、改进项、负责人。

## 31.15 MiniK8s 排障

```bash
python -m mini_k8s.cli get pods -o wide
python -m mini_k8s.cli describe pod <pod>
python -m mini_k8s.cli get events
python -m mini_k8s.cli logs <pod>
python -m mini_k8s.cli top pods
```

MiniK8s 的 `diagnostics()` 返回节点、Pod、Deployment 摘要，适合测试/脚本。

## 31.16 事故复盘模板

排障结束后，复盘比“找到根因”更有价值。以下模板适用于 Kubernetes 平台事故。

```markdown
# 事故复盘：<标题>

## 1. 摘要
- 影响：<哪些服务/租户/区域，错误率/延迟/不可用比例>
- 持续时间：<开始/发现/止血/恢复>
- 严重级别：<P0/P1/P2>
- 当前状态：<已恢复/观察中>

## 2. 时间线（UTC+8）
- 10:00 <变更/事件>
- 10:03 <告警>
- 10:05 <第一响应>
- 10:12 <止血动作>
- 10:30 <恢复确认>

## 3. 根因
- 直接原因：
- 根本原因：
- 为什么现有防护没拦住：

## 4. 影响面
- 用户影响：
- 数据影响（丢失/不一致/泄露）：
- 成本影响：

## 5. 处置过程
- 有效动作：
- 无效/误导动作：
- 哪些信息缺失导致变慢：

## 6. 改进项
| 类型 | 动作 | 负责人 | 截止 | 验证方式 |
|---|---|---|---|---|
| 预防 |  |  |  |  |
| 检测 |  |  |  |  |
| 止血 |  |  |  |  |
| 恢复 |  |  |  |  |

## 7. 经验
- 做得好的：
- 需要改进的：
- 可复用的 runbook/自动化：
```

几个原则：

- **对事不对人**：复盘不是追责，否则大家会隐藏信息。
- **区分直接原因与系统性原因**：例如“删了 namespace”是直接原因，缺少回收站/审批/备份是系统原因。
- **改进项要可验证**：不要写“加强监控”，要写“新增 Pending Pod > 5 分钟告警，并在预发演练”。
- **时间线用统一时区**，保留命令/事件/截图。
- **把复盘变成测试**：能自动化的检查写进 CI/巡检，而不是只写在文档里。

MiniK8s 的 `diagnostics()` 和事件适合本地复现问题，但真实事故需要结合审计、监控、变更记录和云厂商事件。

## 本章小结

- 排障先分层、定范围、看变更，再动手。
- Pending、CrashLoop、ImagePull、Service、DNS、Node、控制面各有决策树。
- 事件和 status 是第一手证据；日志用于细节。
- 性能问题要区分应用/网络/调度/启动/API。
- 事故处理先止血，后根因，最后复盘。
- 复盘要区分直接原因与系统原因，改进项必须可验证、有负责人和截止时间。

## 延伸阅读

- [附录 C 故障排查清单](../appendices/C-troubleshooting-checklist.md)
- [第 27 章 可观测性](27-observability.md)
- [第 15 章 CNI、eBPF 与 NetworkPolicy](15-cni-ebpf-networkpolicy.md)
- [第 32 章 GitOps 与渐进式交付](32-gitops-progressive-delivery.md)
- [第 33 章 多集群、集群生命周期与虚拟集群](33-multicluster-cluster-api.md)
- [附录 M 生产事故案例库](../appendices/M-incident-casebook.md)
- [附录 K etcd 深入](../appendices/K-etcd-operations.md)
