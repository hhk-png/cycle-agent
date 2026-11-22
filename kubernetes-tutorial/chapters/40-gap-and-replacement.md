# 第 40 章 与真实 Kubernetes 的差距及替换路线

## 40.1 差距总表

| 领域 | MiniK8s | 真实 Kubernetes | 影响 |
|---|---|---|---|
| 存储 | 内存 dict | etcd（Raft、MVCC、持久化） | 重启丢失、无多副本 |
| API | HTTP JSON 子集 + 简化 SSA | 完整 REST/protobuf/OpenAPI/SSA（关联列表、默认值所有权） | 客户端兼容性有限 |
| 认证 | 静态 token + HMAC SA | x509/OIDC/webhook/TokenRequest | 无企业 SSO |
| 授权 | 简化 RBAC | RBAC+Node+Webhook+ABAC | 缺少高级语义 |
| 准入 | 内置链 | 插件 + mutating/validating webhook | 无法接入策略引擎 |
| 调度 | 单线程轮询 | 调度框架、缓存、队列、插件 | 规模/策略有限 |
| 加速器 | 扩展资源整数账本 | Device Plugin/DRA/驱动/MIG | 不执行真实 GPU 工作负载 |
| 控制器 | 轮询 store | informer/workqueue/限速/leader | 规模/可靠性有限 |
| 运行时 | 模拟状态机 | CRI + containerd/CRI-O | 不运行真实容器 |
| 网络与入口 | 进程内 ServiceProxy + Ingress/Gateway 路由表 | CNI/kube-proxy/eBPF + L7 代理/TLS | 无真实连通与 HTTP 监听 |
| 存储 | hostPath 模拟 | CSI + 云盘/NFS | 无真实卷 |
| 高可用 | 单进程 | 多控制面/etcd quorum | 无故障切换 |
| 可观测 | 简单 metrics/events | Prometheus/OTel/审计 | 无生产可观测 |
| 安全 | 简化 PSA | 完整 PSS/运行时安全 | 无强隔离 |

## 40.2 替换存储：内存 → etcd

接口在 `ResourceStore`（注意关键字参数与返回值）：

```python
create(gvr, obj, kind, namespace)
get(gvr, namespace, name)
list(gvr, namespace=None, *, label_selector=None, field_selector=None,
     limit=None, continue_token=None) -> (objects, next_token)
update(gvr, obj, kind, namespace)
delete(gvr, namespace, name, kind, *, force=False, propagation=None)
watch(gvr, namespace=None, *, resource_version=None, send_initial=False) -> Watcher
```

替换思路：

1. 保留 API 层，增加 `EtcdResourceStore`。
2. key 设计：

```text
/registry/<group>/<plural>/<namespace>/<name>
```

3. value 存 JSON/protobuf。
4. resourceVersion 用 etcd modRevision。
5. watch 用 etcd watch，转换为 WatchEvent。
6. 实现乐观并发：比较 modRevision。
7. finalizer 逻辑在 API 层保留。

难点：

- etcd 事务/租约。
- watch 历史/compaction。
- 多版本转换。
- 对象大小限制。

## 40.3 替换运行时：模拟 → CRI

MiniK8s 的 `Kubelet` 没有暴露“创建 sandbox/容器”的公共接口，它只有 `start/stop`、`reconcile`、`update_node_status`、`logs`、`exec`；容器的推进是私有的 `_advance_container` 状态机。真实 kubelet 通过 CRI 完成 sandbox/容器生命周期，替换时要把这个私有状态机改成 CRI 调用，而不是“实现一个已有的公共方法”。

替换思路：

1. 使用 `cri-api` gRPC 客户端（Python 可用 grpcio + protobuf）。
2. 实现：

```text
RuntimeService.RunPodSandbox
RuntimeService.CreateContainer
RuntimeService.StartContainer
RuntimeService.StopContainer
RuntimeService.RemoveContainer
RuntimeService.ExecSync
ImageService.PullImage
```

3. CNI 调用：创建 network namespace、调用 CNI 插件。
4. CSI 挂载：调用 CSI Node service。
5. 探针：HTTP/TCP/exec 真实执行。
6. 日志：读取 CRI 日志或节点文件。

这不是“改几行”，而是重写 kubelet 的数据面。建议用真实 kubelet 替换整个模块，而不是逐步模拟。

## 40.4 替换网络：模拟 → CNI/kube-proxy

MiniK8s 的 `ServiceProxy`/`NetworkSimulator` 只做判定。替换：

1. 节点安装 CNI（Calico/Cilium）。
2. 运行 kube-proxy 或 eBPF agent。
3. API Server 的 Service/EndpointSlice 对象保持不变。
4. 删除 ServiceProxy，使用真实转发。
5. NetworkPolicy 由 CNI 执行。

注意：

- CNI 需要真实 Pod sandbox/network namespace。
- Service CIDR/Pod CIDR 要规划。
- kube-proxy 需要 kubeconfig/权限。

Gateway API 的替换更接近“换数据面而非换 API”：

1. 保留 `GatewayController` 的状态契约（或改用 Envoy Gateway/NGINX Gateway Fabric 的控制器）。
2. 用真实的 Gateway 实现替换 `GatewayRouter`，由它监听端口、终止 TLS、执行 filter/重试/限流。
3. `HTTPRoute`/`ReferenceGrant` 对象与 RBAC 不变；应用团队的 YAML 可以继续使用。
4. 删除进程内路由表，改为控制器下发 xDS/配置；验收时对比 `status.parents` 与真实数据面的可达性。

## 40.5 替换存储：hostPath → CSI

1. 部署 CSI 驱动（Controller/Node/sidecars）。
2. 创建 StorageClass 指向驱动。
3. PVController 改为依赖 external-provisioner，不自己创建 PV。
4. kubelet 调用 CSI NodeStage/NodePublish。
5. PVC finalizer/拓扑由 CSI 处理。

MiniK8s 的 PVController 可以保留用于静态 PV，但动态供给应交给 CSI。

## 40.6 替换 API Server

如果目标是与真实 Kubernetes 兼容，最简单是直接使用 kube-apiserver + etcd，而不是扩展 MiniK8s。

步骤：

1. 部署 etcd。
2. 部署 kube-apiserver（证书、service account key、authorization mode）。
3. 部署 controller-manager/scheduler。
4. 节点安装 kubelet/kube-proxy/CNI。
5. 使用真实 kubectl。

这就是 kubeadm/k3s/kind 做的事情。MiniK8s 的价值是理解内部，不是替代它们。

## 40.7 替换认证/授权/准入

- 认证：API Server 配置 OIDC/webhook；MiniK8s 需要实现对应 authenticator。
- 授权：实现 `Authorizer` 接口，调用 webhook。
- 准入：在 `AdmissionChain` 中增加 HTTP webhook 调用：

```python
AdmissionReview → POST webhook → 解析 allowed/patch
```

注意：

- TLS、证书、超时、failurePolicy。
- 递归调用防护。
- 高可用。

## 40.8 替换调度器

- 实现调度框架插件（Go）。
- 或运行自定义调度器，设置 `spec.schedulerName`。
- MiniK8s 的 Scheduler 可以作为算法原型，但不适合生产。

生产自定义调度器要考虑：

- 缓存一致性。
- 高可用/leader election。
- 与默认调度器共存。
- 性能/可观测。

## 40.9 替换控制器

- 用 client-go/controller-runtime 重写。
- informer/workqueue/限速/leader election。
- CRD/Operator 用 Kubebuilder。

MiniK8s 的控制器逻辑（滚动、Job、CronJob）可以作为伪代码参考，但生产实现要处理更多边界。

## 40.10 什么时候直接用真实 Kubernetes

- 需要运行真实容器/网络/存储。
- 需要生产 SLA/高可用。
- 需要生态工具（Helm、Operator、Service Mesh）。
- 团队没有维护自研控制面的能力。

MiniK8s 适合：

- 学习/教学。
- 控制面算法原型。
- CI 中验证 YAML/控制器逻辑。
- 面试/培训。

不要用 MiniK8s 跑生产。它是“可运行的教材”，不是“轻量 Kubernetes”。

## 40.11 从 MiniK8s 到真实集群的迁移清单

- 替换镜像名为真实镜像。
- 替换 demo 注解（simulate.*）。
- 配置 StorageClass/CSI。
- 配置 CNI/NetworkPolicy。
- 配置 Ingress Controller/TLS。
- 配置 RBAC/SA/IRSA。
- 配置资源 requests/limits；扩展资源（GPU）要安装真实 device plugin/DRA 驱动。
- 配置探针/PDB/HPA；推理服务改用延迟/队列指标。
- 配置日志/指标/追踪。
- 压测/故障演练。

## 40.12 一条可执行的替换路线：MiniK8s → kind → 生产

“替换某模块”很容易写成一句口号。更可行的做法是分阶段提高保真度，每一阶段都有明确的验收标准。

### 阶段 0：把 MiniK8s 当契约测试

目标：验证 YAML、控制器逻辑、状态机与错误路径。

验收：

- 同一份 YAML 在 MiniK8s 上 apply 成功，关键 status 达到预期。
- 单元/集成/E2E 覆盖主要分支与失败路径。
- 文档标注哪些字段是模拟的；完整的 `mini-k8s.io/simulate.*` 注解表见第 37.4 节。

这一阶段不追求真实网络/存储，追求**可重复、可断言**。

### 阶段 1：kind/k3d 验证 API 与调度

目标：用真实 kube-apiserver、etcd、kubelet、CNI 跑同一批 YAML。

验收：

- `kubectl apply` 无 schema/准入错误。
- Pod 真正 Running，Service/DNS/Ingress 可达。
- readiness/liveness、PDB、HPA、PVC 行为符合预期。
- 真实集群多出的字段/对象（EndpointSlice、conditions、QoS）被测试覆盖。

这一阶段会暴露 MiniK8s 没模拟的差异：镜像拉取、CNI、CSI、cgroup、证书、调度器细节。

### 阶段 2：预发集群验证运维路径

目标：验证升级、备份、恢复、扩缩容、故障注入。

验收：

- 节点 drain/cordon、版本升级、etcd 快照恢复演练通过。
- 网络策略、RBAC、准入策略在真实身份下生效。
- 监控/日志/追踪能定位一次人为故障。
- 容量压测找到拐点，知道扩容阈值。

### 阶段 3：生产灰度

目标：用真实流量验证 SLO、成本与应急。

验收：

- 金丝雀/灰度有明确指标与回滚条件。
- on-call runbook、告警、事故演练就绪。
- 成本/配额/多租户治理生效。
- 有退出/降级方案。

### 一张对照表

| 能力 | MiniK8s | kind | 生产集群 |
|---|---|---|---|
| API 语义 | 子集 | 真实 | 真实 + 版本差异 |
| 调度 | 简化 filter/score | 真实调度器 | 真实 + 自定义插件 |
| 运行时 | 模拟状态机 | containerd/CRI | 多运行时/沙箱 |
| 网络 | 进程内模拟 | 真实 CNI | CNI/eBPF/云网络 |
| 存储 | hostPath 模拟 | 本地/CSI | 云盘/分布式存储 |
| 高可用 | 单进程 | 单控制面 | 多控制面/多 AZ |
| 运维 | 无 | 少量 | 升级/备份/值守 |

替换的顺序建议是“先 API 契约，再数据面，再运维”，而不是一次性重写。每一步都保留可回滚的基线，才知道新实现是否真的更好。

## 40.13 替换的接口契约与验收测试

“把 store 换成 etcd”不是把 dict 换成 HTTP 客户端那么简单。每个替换点都有一组必须保持的契约，以及一组预期会暴露差异的测试。

### 40.13.1 存储：ResourceStore → etcd

必须保持：

- `create/update/delete` 的 resourceVersion 单调性与乐观并发。
- watch 从指定 RV 重放、410 过期、慢消费者断开。
- finalizer 的“先标记删除、清空后物理删除”。
- list 的 selector、分页、排序稳定性。
- 对象大小上限、命名空间隔离。

替换后应新增的测试：

- 用两个客户端并发 update 同一对象，一个成功、一个 409。
- kill etcd leader 后，watch 客户端收到 410/重连并 relist。
- 压缩/碎片整理后，旧 RV 的 watch 行为符合预期。
- 大对象/大量对象时的分页与超时。

预期差异：

- RV 从整数变成 etcd revision 字符串；客户端不能解析。
- 事件顺序、watch 延迟、list 一致性语义不同。
- 需要处理 etcd 不可用时的 API Server 降级。

### 40.13.2 运行时：模拟 → CRI

必须保持：

- Pod sandbox 与容器生命周期、重启计数、退出码。
- 探针执行、日志、exec 流。
- 镜像拉取/GC、节点磁盘压力。
- Pod IP/网络命名空间由 CNI 提供。

替换后测试：

- 镜像不存在/拉取失败的事件与重试。
- init container 失败、主容器崩溃、liveness 重启。
- exec 的 stdin/TTY/退出码。
- 节点重启后容器状态恢复/清理。
- cgroup 限制下的 OOM/CPU 节流。

预期差异：

- 真实启动慢、镜像大、网络/存储依赖多。
- 容器运行时错误码与事件更丰富。
- 模拟的“确定性”消失，测试需要等待/重试。

### 40.13.3 网络：模拟 → CNI/kube-proxy

必须保持：

- Service ClusterIP/NodePort/LoadBalancer 语义。
- Endpoints/EndpointSlice 的 ready/terminating 条件。
- DNS 解析（包括 headless/ExternalName）。
- NetworkPolicy 的默认拒绝与选择器语义。
- 跨节点 Pod 连通与 MTU。

替换后测试：

- 同节点/跨节点 Pod 互通。
- Service 会话亲和、拓扑路由、externalTrafficPolicy。
- NetworkPolicy ingress/egress 的正反用例。
- DNS 超时/重试、NodeLocal DNS。
- 网络分区、conntrack 表满、MTU 不匹配。

预期差异：

- 真实网络有延迟、丢包、conntrack、iptables/IPVS/eBPF 规则。
- CNI 的 IPAM、双栈、多网卡行为因实现而异。

### 40.13.4 存储：hostPath → CSI

必须保持：

- PV/PVC 绑定、AccessMode、回收策略。
- 动态供给、WFFC、拓扑。
- 扩容、快照、克隆。
- 挂载/卸载与 Pod 生命周期的关系。

替换后测试：

- 多可用区调度与卷拓扑。
- attach 上限、节点故障、卷不可用。
- 快照恢复、跨命名空间恢复。
- 扩容后的文件系统容量。
- 删除 PVC 后的卷回收/保留。

预期差异：

- 真实卷有 IOPS/吞吐/延迟限制。
- attach/detach 是异步的，Pod 可能长时间 ContainerCreating。
- 快照一致性取决于驱动/应用。

### 40.13.5 认证/授权/准入

必须保持：

- 认证用户/组/SA 的身份映射。
- RBAC 默认拒绝与聚合规则。
- 准入的顺序、默认值、拒绝原因。
- 审计记录（如果启用）。

替换后测试：

- OIDC token 过期/错误 audience。
- SA token 轮换与 audience。
- RoleBinding 越权尝试。
- Webhook 超时/不可用时的失败策略。
- dry-run 不产生副作用。

### 40.13.6 替换的验收门槛

对每个模块，定义一个“可以替换”的门槛：

```text
契约测试全绿
+ 关键失败路径有测试
+ 性能在预算内
+ 回滚路径演练过
+ 可观测性能定位一次人为故障
```

没有这五项，替换只是“能跑”，不是“可生产”。

## 本章小结

- MiniK8s 在 API/控制面层面接近真实 Kubernetes，在数据面是模拟。
- 存储、运行时、网络、CSI、认证、准入都有明确替换点。
- 真正生产化通常意味着用真实组件替换整个模块，而不是“补几行代码”。
- 知道差距，才能正确使用这个教学实现。

## 延伸阅读

- [第 34 章 MiniK8s 总体设计](34-minik8s-design.md)
- [第 35 章 API Server 与存储](35-apiserver-store.md)
- [第 42 章 云原生未来方向](42-future.md)
- [附录 Q 扩展点与交付物开发实战](../appendices/Q-extension-development.md)：把 MiniK8s 的控制器/客户端替换成真实 client-go/controller-runtime 的工程路径。
