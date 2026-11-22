# 第 38 章 网络、存储与安全实现

## 38.1 Service 与 EndpointSlice

`ServiceController`：

- 遍历 Service。
- 分配 ClusterIP（`10.96.0.0/16`，从 `.10` 开始）。
- 分配 NodePort（30000–32767）。
- LoadBalancer 写入 status `192.0.2.100`。
- ExternalName 清空 clusterIP。

```python
def _allocate_ip(self, used):
    for value in range(start, end):
        candidate = str(ipaddress.ip_address(value))
        if candidate not in used:
            return candidate
```

`EndpointsController`：

- 根据 selector 找 Pod；Ready Pod 进 `addresses`，其余进 `notReadyAddresses`。
- `publishNotReadyAddresses: true` 时把所有 Pod 放进 `addresses`。
- 生成 EndpointSlice，按端口集合哈希命名；每个 endpoint 带 ready/serving/terminating 条件与 zone。
- 所有 endpoint 在同一 zone 时写入 `hints.forZones`（拓扑感知路由的简化）。
- 删除过期 slice。

targetPort 解析：

```python
def _target_port(service_port, pods):
    target = service_port.get("targetPort", service_port.get("port"))
    if isinstance(target, int): return target
    if numeric: return int(target)
    for pod in pods:
        for container:
            for port:
                if port.name == target:
                    return port.containerPort
```

这比简单使用 service port 更接近真实。

## 38.2 DNS

`ClusterDNS.resolve` 支持：

```text
web
web.default
web.default.svc
web.default.svc.cluster.local
db-0.db
db-0.db.default.svc.cluster.local
```

最后两行是 StatefulSet 的逐 Pod 记录：先按 Service 解析（避免 `web.default` 这类名字歧义），Service 解析为空时再按 `<pod>.<headless-service>` 查找 Pod IP。

实现：

- 拆分 name。
- 解析 service/namespace。
- ClusterIP → 返回。
- Headless → 返回 Ready Pod IP（`publishNotReadyAddresses` 时包含未就绪）。
- ExternalName → 返回 externalName 目标（模拟 CNAME；真实解析器返回 CNAME 记录）。

`search_domains` 返回 Kubernetes 的 search 域。

测试：`test_dns_short_fqdn_and_headless`。

## 38.3 ServiceProxy

```python
def connect(source_pod, service_name, namespace, port, path):
    endpoints = self.endpoints(...)
    for endpoint in endpoints:
        if policies.can_connect(source, target, target_port):
            return connected
    return denied
```

- 默认轮询选择后端。
- `sessionAffinity: ClientIP` 时按源 Pod IP/name 哈希稳定选择。
- 调用 NetworkSimulator 判定。
- 返回模拟 HTTP 响应。

这不是真实转发，但可以验证：

- Endpoint 选择。
- targetPort。
- NetworkPolicy。
- Service 无端点。
- 会话亲和。

## 38.4 NetworkPolicy

`NetworkSimulator.can_connect`：

1. 找 egress 策略（源 namespace + 选择源 Pod + policyTypes 含 Egress）。
2. 如果存在且没有规则允许 → 拒绝。
3. 找 ingress 策略（目标 namespace + 选择目标 Pod + policyTypes 含 Ingress）。
4. 如果存在且没有规则允许 → 拒绝。
5. 否则允许。

`_peer_matches`：

- ipBlock：CIDR + except。
- namespaceSelector：命名空间 labels。
- podSelector：Pod labels。
- 空 peer：全部允许。

`_ports_match`：

- 无 ports：所有端口。
- protocol 匹配。
- port 可以是数字、命名端口（按目标 Pod 的容器端口解析）或 `endPort` 范围。

**与真实语义的差异**：在 `from`/`to` 的 peer 里，裸 `podSelector` 在真实 Kubernetes 中只匹配**策略所在 namespace** 的 Pod；MiniK8s 简化为匹配任意 namespace 的 Pod。需要跨 namespace 时，真实清单应写 `namespaceSelector` + `podSelector`，不要依赖裸 `podSelector`。另外，MiniK8s 不实现 `ipBlock` 的节点/主机网络豁免、策略优先级和 CNI 的执行细节。

测试：

- `test_proxy_enforces_network_policy`
- `test_network_policy_egress_and_namespace_selector`
- 命名端口/`endPort` 的单元测试（见 `tests/test_network.py`）

## 38.5 Ingress

`IngressRouter.route`：

1. 遍历 Ingress rules。
2. host 匹配（精确/通配）。
3. path 匹配（Exact/Prefix）。
4. 选最长 path。
5. 找不到则 defaultBackend。
6. 返回 Service/port/ClusterIP。

测试：

- `test_ingress_router_longest_prefix_and_default_backend`
- `test_ingress_status_published`

### 38.5.1 Gateway API

Gateway API 在 MiniK8s 中拆成两个模块，刻意复刻真实实现的分层：

| 模块 | 职责 | 真实世界对应 |
|---|---|---|
| `controllers/gateway.py` | 写 `Accepted`/`ResolvedRefs`/`Programmed` 状态、计算 attachedRoutes | Gateway API 控制器 |
| `network/gateway.py` | 只消费已接受的 Route，做 host/path/header 匹配与加权后端选择 | Envoy/NGINX/数据面 |

控制器逻辑：

```python
accepted = (
    section_name_matches
    and allowed_routes_permits_namespace
    and hostnames_intersect(route.hostnames, listener.hostname)
)
resolved = all(
    service_exists(ref) and service_has_port(ref) and reference_granted(ref)
    for ref in backendRefs
)
```

状态与数据面分离带来一个可测试的性质：**数据面只执行已接受的路由**。`GatewayRouter` 过滤 `Accepted=True && ResolvedRefs=True` 的 Route；如果 Route 被拒绝，路由表里就没有它。与“控制器推送 xDS、代理只执行”的真实实现相比，MiniK8s 额外做了一层防御性复核：listener 的 `allowedRoutes`、hostname 与 `ReferenceGrant` 在路由时再检查一次，避免控制器停止或 status 陈旧时把未授权后端放行。

```python
def _accepted_listeners(route):
    for parent in route["status"]["parents"]:
        if Accepted and ResolvedRefs:
            for listener in gateway.listeners:
                if (listener_valid(listener)
                        and listener_supported(listener)
                        and route_allowed(route, listener, gateway, labels)
                        and hostnames_intersect(route.hostnames, listener.hostname)):
                    yield gateway, listener
```

`ReferenceGrant` 的检查同时发生在控制器与数据面：控制器写 `ResolvedRefs=False/RefNotPermitted`，`GatewayRouter` 在路由前再查一次。这样即使 Route 对象本身合法、或 status 尚未刷新，数据面也不会把流量导向未授权 Service。

限制：没有真实 TCP 监听、TLS 终止、HTTP filter、GRPCRoute/TLSRoute/TCPRoute，也没有 Envoy 的连接池/重试/熔断。这些是“实现质量”而非“API 语义”，MiniK8s 用测试固定的是后者。

## 38.6 存储

`PVController`：

- 为 PVC 添加 `kubernetes.io/pvc-protection`，为已绑定的 PV 添加 `kubernetes.io/pv-protection` finalizer。
- 静态 PV 匹配：容量、accessModes、volumeMode、storageClass（区分“未设置”与显式 `""`）、selector（matchLabels/matchExpressions）、claimRef。
- 动态供给：创建 hostPath PV。
- WFFC：等 Pod 调度后绑定。
- 删除：PVC 被 Pod 使用则保留；否则移除 finalizer。PV 仍被 PVC 引用时保持 Terminating，引用消失后才真正删除。
- 默认 StorageClass（`standard`）由 `MiniCluster._bootstrap` 创建，PVController 只读取 `storageclass.kubernetes.io/is-default-class` 注解，不负责创建。

```python
if sc.volumeBindingMode == "WaitForFirstConsumer":
    if not consumer_scheduled(pvc):
        pending("WaitingForFirstConsumer...")
```

测试：

- 动态绑定。
- 静态绑定 + Retain。
- WFFC。
- PVC finalizer 保护。

## 38.7 RBAC

`RBACAuthorizer`：

- system:masters 直接放行。
- discovery 路径公开。
- 读取 ClusterRoleBinding，匹配 subject。
- 读取 namespace 的 RoleBinding。
- 规则匹配 verbs/apiGroups/resources/resourceNames。

```python
def _rule_allows(rule, attrs):
    if verb not in rule.verbs and "*" not in rule.verbs: return False
    if attrs.non_resource:
        return any(url matches nonResourceURLs)
    if group not in rule.apiGroups and "*" not in: return False
    requested = resource + "/" + subresource
    if requested not in rule.resources and "*" not in: return False
    if resourceNames and name not in: return False
    return True
```

测试：

- 拒绝 → 绑定 → 允许。
- resourceNames。
- 子资源 pods/log。
- SA token。

## 38.8 ServiceAccount token

`ServiceAccountTokenIssuer`：

- HMAC-SHA256 签名。
- claims 包含 `sub`、`aud`、`iat`、`nbf`、`exp`，以及嵌套的 `"kubernetes.io": {"namespace": ..., "serviceaccount": {"name": ..., "uid": ...}}`（不是平铺的 `kubernetes.io.namespace`）。
- `validate` 检查签名、过期、nbf、audience。

API Server：

- `/serviceaccounts/{name}/token` 签发。
- `/tokenreviews` 验证。

测试：`test_service_account_token_and_review`。

## 38.9 Leader election

`LeaderElector`：

- `try_acquire_or_renew`。
- Lease 不存在则创建。
- holder 是自己则续约。
- 过期/空则接管，`leaseTransitions++`。
- 使用注入时钟，测试可控制。

测试：

- 单 leader。
- 过期接管。
- 释放后立即接管。

## 38.10 Admission

`AdmissionChain`：

- namespace 存在。
- 默认值。
- LimitRange。
- 校验。
- PSA。
- CRD schema。
- Quota。

Quota：

```python
usage = quota_usage(store, namespace)
contribution = object_quota_usage(kind, obj, gvr)
old = object_quota_usage(kind, old_obj, gvr) if update
used = usage - old + contribution
if used > hard: raise Forbidden
```

Pod 的资源贡献不是简单求和：init 容器按“最大 init request”与“所有应用容器 request 之和”取较大者，sidecar（`restartPolicy: Always` 的 init 容器）按应用容器计入。实现见 `admission.py` 的 `pod_quota_usage`，测试覆盖“init 容器更大”“sidecar 持续占用”等场景。`ResourceQuotaController` 还会把格式化后的 `used` 写回 `status`，供 `kubectl describe quota` 观察。

测试：

- 超配额拒绝。
- 更新不重复计数。
- LimitRange 注入。
- PSA baseline/restricted。
- init/sidecar 的资源账本。

## 38.11 生命周期控制器：Namespace、SA、Node、GC、CRD

`controllers/lifecycle.py` 里的一组“小控制器”常被忽略，但它们决定了对象能否被正确清理：

| 控制器 | 行为 | 与真实实现的差异 |
|---|---|---|
| NamespaceLifecycle | 删除 namespace 时级联删除其中对象，等 finalizer 清空 | 简化了发现/批量删除与失败重试 |
| ServiceAccountLifecycle | 为 namespace 自动创建 `default` SA | 不实现 imagePullSecrets 自动挂载 |
| NodeLifecycle | 心跳超时把 Node 置 NotReady 并加 taint | 没有真实的 lease/心跳窗口与 eviction 速率限制 |
| GarbageCollector | 按 ownerReferences 删除孤儿对象 | 不实现 foreground/background 的完整协调 |
| CRDController | 注册/清理 CRD 动态类型、维护 status | 不做 conversion/webhook/多版本存储 |

这些控制器和真实实现一样遵循“watch store → 计算差异 → 写回”的模式；区别在于 MiniK8s 用轮询代替 informer，且没有完整的失败重试/限速。阅读时重点看“最终一致”如何达成，而不是逐行对照上游。

## 38.12 与真实实现的映射

| MiniK8s | 真实实现 |
|---|---|
| ServiceController | kube-controller-manager + cloud controller |
| EndpointsController | endpointslice-controller |
| ClusterDNS | CoreDNS |
| ServiceProxy | kube-proxy/eBPF |
| NetworkSimulator | CNI plugin |
| IngressRouter | Ingress Controller |
| PVController | external-provisioner + CSI |
| RBACAuthorizer | kube-apiserver RBAC authorizer |
| TokenIssuer | kube-apiserver TokenRequest |
| LeaderElector | client-go leaderelection |
| AdmissionChain | API Server admission plugins |

## 38.13 未实现的真实细节

- Service 的完整拓扑/流量分布与 LoadBalancer 实现。
- EndpointSlice 分片（100 个/片）与真实拓扑 hint 计算。
- NetworkPolicy 的完整默认语义和 CNI 执行。
- Ingress TLS/HTTP 解析。
- CSI attach/detach/快照。
- RBAC aggregation/bind/escalate。
- OIDC/webhook 认证。
- 审计。
- 多控制面选主。

第 40 章给出替换路线。

## 本章小结

- MiniK8s 用控制器和进程内模拟实现 Service/DNS/Ingress/NetworkPolicy/存储/RBAC/选举。
- 这些实现保留了真实系统的 API 形状和关键判定逻辑。
- 数据面（真实转发、CNI、CSI、内核策略）是模拟的，不能替代生产。
- 模块边界清晰，便于逐个替换为真实组件。

## 延伸阅读

- [第 13 章 Service、Endpoints 与 DNS](13-service-endpoints-dns.md)
- [第 15 章 CNI、eBPF 与 NetworkPolicy](15-cni-ebpf-networkpolicy.md)
- [第 23 章 认证、授权与 RBAC](23-authn-authz-rbac.md)
- [第 40 章 与真实 Kubernetes 的差距](40-gap-and-replacement.md)
