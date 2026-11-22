# 附录 M 生产事故案例库

> 以下案例是综合多类真实故障模式编写的教学案例，不指向任何具体公司或事故。每个案例都按“症状 → 证据 → 根因 → 止血 → 修复 → 预防”组织，重点不是记住某个命令，而是建立“先分层、再定位、后修复”的判断顺序。

## M.1 怎么读案例

读每个案例时，先遮住“根因”，只看症状与证据，自己判断下一步查什么；再对照案例的排查路径。你会发现：

- 大多数事故的**直接原因**很简单，难的是在压力下找到它。
- 大多数事故的**系统原因**在变更、监控、权限或流程，而不是某个组件。
- 很多“组件故障”其实是配置/容量/证书/依赖问题。

案例索引：

| 案例 | 一句话症状 | 主要章节 |
|---|---|---|
| M.2 | 所有写操作失败，日志出现 NOSPACE | 第 28、30 章，附录 K |
| M.3 | kubectl 报 x509，集群整体不可用 | 第 2、23、29 章 |
| M.4 | 升级 CNI 后跨节点 Pod 不通 | 第 15 章 |
| M.5 | 服务调用偶发超时，DNS 解析慢 | 第 13、27 章 |
| M.6 | 节点磁盘有空间但 Pod 起不来 | 第 7、25 章 |
| M.7 | 容器反复 OOMKilled | 第 7 章 |
| M.8 | HPA 副本数来回震荡 | 第 20 章 |
| M.9 | 发布后网关 502，Pod 却是 Running | 第 8、13、14 章 |
| M.10 | 删除 PVC 后数据无法恢复 | 第 12 章 |
| M.11 | CRD 升级后控制器崩溃 | 第 19 章 |
| M.12 | 升级后部分节点 kubelet 不工作 | 第 29 章，附录 I |
| M.13 | 多集群切换后流量打到空后端 | 第 33 章 |
| M.14 | 异常调用来自被泄露的 SA token | 第 23、24 章 |
| M.15 | 节点维护卡住，drain 一直失败 | 第 26、28 章 |
| M.16 | 节点磁盘压力触发连锁驱逐 | 第 7、28 章 |

## M.2 案例：etcd 磁盘满，所有写操作失败

**背景**：一个自建集群运行约 18 个月，事件量较大，平时只监控节点磁盘使用率。

**症状**：

- `kubectl apply` 全部失败，返回 500。
- Deployment 不再创建新 Pod，控制器日志刷 `etcdserver: mvcc: database space exceeded`。
- 节点磁盘 `df -h` 看起来还有空间。

**证据**：

```bash
etcdctl endpoint status --cluster -w table
etcdctl alarm list
```

- etcd DB 大小接近 quota（默认常见 2 GiB）。
- NOSPACE 告警存在。
- `df -h` 的剩余空间是宿主机磁盘，不是 etcd backend quota。
- compaction 指标长期没有下降，历史 revision 堆积。

**根因**：事件与高频对象更新导致 etcd 增长；compaction 未按预期生效/长期未 defrag，数据库达到 quota；团队把“磁盘剩余空间”误当成“etcd 还有容量”。

**止血**：

1. 暂停大规模创建/删除与批量控制器操作。
2. 对 etcd 做 compact 与逐成员 defrag。
3. 确认 DB 大小下降后 `alarm disarm`。
4. 观察写入恢复，再逐步恢复业务变更。

**修复**：

- 调整 Event 保留/聚合，降低高频小写入。
- 核查 kube-apiserver 的 compaction 配置与组件健康。
- 评估提高 `--quota-backend-bytes` 或扩容磁盘，但先解决增长根因。
- 增加 etcd DB 大小、revision、告警、fsync 延迟的监控。

**预防**：

- 监控 etcd 自身指标，而不是只看宿主机磁盘。
- 定期 defrag 与快照，做恢复演练。
- 对大对象（ConfigMap/Secret/CRD）设上限与评审。
- 变更前评估写入放大。

**教训**：“磁盘有空间”不等于“etcd 有空间”；quota 是独立约束。

## M.3 案例：API Server 证书过期，集群整体不可用

**背景**：一个自建集群运行一年，证书由脚本签发，没有到期监控。

**症状**：

- `kubectl` 报 `x509: certificate has expired or is not yet valid`。
- kubelet 无法上报状态，节点变 NotReady。
- 业务 Pod 仍在运行，但无法创建/更新。

**证据**：

```bash
openssl x509 -in /etc/kubernetes/pki/apiserver.crt -noout -dates -subject -ext subjectAltName
kubeadm certs check-expiration
journalctl -u kubelet | grep -i x509
```

- API Server 服务端证书已过期。
- kubelet 的 client 证书也临近/已过期。
- 部分组件的 kubeconfig 指向的 VIP 不在证书 SAN 中。

**根因**：证书生命周期没有纳入监控与变更管理；签发时 SAN 不完整；到期前没有演练续期。

**止血**：

1. 续期/重签证书，确保 SAN 包含 VIP、DNS、所有 API Server 地址。
2. 按顺序重启控制面组件与 kubelet。
3. 验证 `kubectl get --raw /readyz?verbose` 与节点状态。

**修复**：

- 用 kubeadm 证书管理或自动化轮换；记录到期时间。
- 增加证书到期告警（如 30/14/7 天）。
- 把 SAN 校验加入集群验收。
- 定期演练证书续期。

**预防**：

- 所有证书（apiserver、kubelet、etcd、front-proxy、CA）纳入清单。
- 不要手工复制“看起来一样”的证书；CA 与 SAN 必须匹配。
- 升级/扩容时检查新节点是否能通过 TLS bootstrap。

**教训**：证书故障往往在“什么都没改”的时候爆发；它是最典型的“定时炸弹”。

## M.4 案例：升级 CNI 后跨节点 Pod 不通

**背景**：集群从旧版 CNI 升级到新版本，单节点测试正常。

**症状**：

- 同节点 Pod 互访正常，跨节点 Pod 超时。
- Service ClusterIP 访问不稳定，部分后端不可达。
- 节点 Ready，Pod Running，应用日志无异常。

**证据**：

```bash
kubectl get pods -n kube-system -o wide
kubectl logs -n kube-system <cni-pod> --previous
ip route
ip -d link show
kubectl get nodes -o wide
```

- 部分节点 CNI Pod  crash/重启。
- 节点路由表缺少对端 Pod CIDR，或隧道设备未建立。
- CNI 配置中的 MTU 与底层网络不一致。
- 新旧版本 CNI 的配置格式/字段不兼容。

**根因**：CNI 升级未做滚动兼容；配置迁移不完整；MTU/隧道参数不一致；部分节点没有正确重启 CNI。

**止血**：

1. 回滚 CNI 到上一版本，或先修复异常节点。
2. 确认所有节点 CNI Pod 健康、路由/隧道一致。
3. 用 `kubectl debug`/临时 Pod 做跨节点连通性验证。

**修复**：

- 按 CNI 官方升级顺序执行，先控制面/配置，再逐节点。
- 用 DaemonSet 的滚动策略与就绪探针控制节奏。
- 统一 MTU、Pod CIDR、隧道模式。
- 升级后在预发做跨节点/跨 AZ/NetworkPolicy 回归。

**预防**：

- 把“跨节点连通、DNS、Service、NetworkPolicy”纳入 CNI 升级检查单。
- 监控 CNI Pod 重启、隧道接口、路由数量。
- 保留可快速回滚的镜像/配置。

**教训**：CNI 是数据面的事实来源；单节点通过不代表集群通过。

## M.5 案例：CoreDNS 解析慢导致服务调用超时

**背景**：微服务数量增长后，部分调用 P99 抖动，偶尔 5 秒超时。

**症状**：

- 应用日志出现 DNS 解析超时。
- 直接访问 Pod IP 正常，访问 Service 名偶发失败。
- CoreDNS CPU/延迟升高，但未崩溃。

**证据**：

```bash
kubectl get pods -n kube-system -l k8s-app=kube-dns
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=100
kubectl get svc -n kube-system kube-dns
kubectl exec <pod> -- cat /etc/resolv.conf
kubectl exec <pod> -- nslookup kubernetes.default
```

- `resolv.conf` 中 `ndots:5`，应用大量短域名查询走多次 search 域。
- CoreDNS 上游 DNS 超时/重试。
- 节点 conntrack 表压力或 UDP 丢包。
- 部分 CoreDNS 副本未就绪/分布不均。

**根因**：`ndots` 与 search 域导致解析放大；上游 DNS 不稳定；CoreDNS 容量/调度不足；缺少缓存与超时控制。

**止血**：

1. 扩容 CoreDNS/调整拓扑分布。
2. 修复上游 DNS 或缩短超时/重试。
3. 对关键服务使用 FQDN（末尾加点）减少 search 次数。
4. 必要时调整 `ndots`（谨慎，影响所有 Pod）。

**修复**：

- 给 CoreDNS 配置缓存、合理的 forward 与超时。
- 监控 CoreDNS 的请求量、延迟、错误、上游健康。
- 用 NodeLocal DNSCache 降低跨节点查询与 conntrack 压力。
- 应用侧使用连接池/长连接，减少每次请求的 DNS 查询。

**预防**：

- 把 DNS 纳入 SLO 与告警。
- 变更 CoreDNS 配置前做压测。
- 避免在高峰做集群级 DNS 变更。

**教训**：DNS 故障经常表现为“应用超时”，而不是“DNS 报错”；先看 `resolv.conf` 与解析路径。

## M.6 案例：节点磁盘有空间，Pod 却起不来

**背景**：节点 `df -h` 显示 30% 剩余，但新 Pod 一直 ContainerCreating/Failed。

**症状**：

- 事件出现 `no space left on device` 或 `failed to create ... inode`。
- 镜像拉取失败/容器创建失败。
- 节点 NotReady 或 DiskPressure=True。

**证据**：

```bash
df -h
df -i
kubectl describe node <node>
journalctl -u kubelet --since "1 hour ago"
crictl images
du -sh /var/lib/containerd /var/log /var/lib/kubelet
```

- `df -i` 显示 inode 耗尽。
- 大量旧镜像/容器日志/emptyDir 未清理。
- 节点 DiskPressure 触发驱逐。

**根因**：inode 耗尽（大量小文件/日志），或镜像/日志占用超过 kubelet 驱逐阈值；`df -h` 只看字节，不看 inode。

**止血**：

1. 清理无用镜像/停止的容器/日志（确认不影响运行中业务）。
2. 扩容磁盘或迁移负载。
3. 解除节点压力后观察驱逐是否停止。

**修复**：

- 配置镜像/日志轮转与保留策略。
- 给 emptyDir/日志设大小限制（`sizeLimit`）。
- 监控 inode 与 DiskPressure，而不只是磁盘使用率。
- 调整 kubelet 驱逐阈值与预留资源。

**预防**：

- 节点基线包含 inode、日志、镜像、容器数量。
- 避免在系统盘上跑高写入工作负载。
- 定期清理与容量评审。

**教训**：磁盘有两个维度：字节与 inode；Kubernetes 的驱逐看的是节点压力，不是“还有多少 GB”。

## M.7 案例：容器反复 OOMKilled

**背景**：Java 服务迁移到 Kubernetes 后频繁重启，Pod 状态 CrashLoopBackOff。

**症状**：

- `kubectl describe pod` 显示 `Last State: Terminated, Reason: OOMKilled`。
- 容器内存使用接近 limit 后被杀。
- 节点内存充足，Pod 仍被杀。

**证据**：

```bash
kubectl describe pod <pod>
kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[*].lastState}'
kubectl top pod <pod>
kubectl logs <pod> --previous
```

- limit 低于应用实际峰值/堆外内存。
- JVM 未设置 `-XX:MaxRAMPercentage`/`-Xmx`，堆与元空间/直接内存叠加超限。
- 多个容器共享 Pod 资源，sidecar 占用未计入。

**根因**：requests/limits 配置与实际内存模型不匹配；只按“平均使用”设置 limit；缺少压测与内存剖析。

**止血**：

1. 提高 limit（确认节点容量与 QoS 影响）。
2. 重启并观察是否稳定。
3. 必要时临时降流量/降并发。

**修复**：

- 用压测确定峰值，设置合理 requests/limits。
- JVM 使用容器感知参数，给堆外留余量。
- 监控 working set、RSS、OOM 次数。
- 对关键服务考虑 Guaranteed QoS。

**预防**：

- 上线前做内存/并发压测。
- 把 OOMKilled 纳入告警与发布检查。
- 不要用“加大 limit”掩盖泄漏；区分峰值与泄漏。

**教训**：OOMKilled 是 cgroup 层的硬杀，不是节点内存不足；先看容器 limit 与进程内存构成。

## M.8 案例：HPA 副本数来回震荡

**背景**：一个 Web 服务配置 HPA，目标 CPU 50%，流量平稳时副本数仍反复增减。

**症状**：

- HPA 事件频繁 `SuccessfulRescale`。
- 副本数在 2–6 之间震荡。
- 应用延迟有波动，但未超 SLO。

**证据**：

```bash
kubectl describe hpa <hpa>
kubectl get hpa <hpa> -w
kubectl top pods
kubectl get deployment <deploy> -o yaml
```

- CPU 指标本身抖动，接近阈值。
- 扩容后新 Pod 启动慢/预热，指标被稀释/拉高。
- 缩容窗口太短，未等待稳定。
- requests 设置过低，利用率计算失真。

**根因**：指标噪声 + 阈值过近 + 缺少 stabilization window + 应用预热/冷启动；HPA 是反馈控制，天然会围绕目标波动。

**止血**：

1. 调整 `behavior.scaleDown.stabilizationWindowSeconds`，避免快速缩容。
2. 提高目标利用率或使用多指标（CPU + 自定义 QPS/延迟）。
3. 检查 requests 是否合理。

**修复**：

- 用负载测试确定目标值与副本上下限。
- 让应用就绪后再接流量，减少冷启动对指标的冲击。
- 对突发流量使用预扩容/定时伸缩。
- 监控 HPA 决策与指标，而不是只看副本数。

**预防**：

- HPA 上线前做阶梯流量测试。
- 避免把 HPA 当“自动容量规划”；节点不足时仍需 Cluster Autoscaler。
- 对延迟敏感服务用自定义指标，而不是只看 CPU。

**教训**：HPA 不是“设个阈值就自动稳定”；它是一个需要调参和观测的反馈系统。

## M.9 案例：发布后网关 502，Pod 却是 Running

**背景**：滚动发布新版本后，Ingress 返回 502/503，部分请求失败。

**症状**：

- Pod 状态 Running，日志正常。
- Ingress/网关日志显示 upstream connect error/no healthy upstream。
- Endpoints/EndpointSlice 中后端数量不稳定。

**证据**：

```bash
kubectl get pods -o wide
kubectl get endpointslices -o wide
kubectl describe pod <pod>
kubectl logs <ingress-controller> --tail=200
kubectl get deployment <deploy> -o yaml
```

- readinessProbe 配置错误/路径不存在，Pod 从未 Ready。
- readinessProbe 过早通过，应用尚未监听端口。
- `targetPort` 与容器端口不一致。
- 滚动更新 `maxUnavailable` 过大，后端瞬间不足。
- Endpoints 收敛有延迟，网关仍打到已终止 Pod。

**根因**：就绪语义与真实可用性不一致；发布参数过激；端口/探针配置错误。

**止血**：

1. 回滚到上一版本。
2. 修正 readinessProbe/端口，确认 Pod Ready 且 Endpoints 有后端。
3. 调整发布参数，增加 `minReadySeconds`。

**修复**：

- readiness 检查真实依赖与监听状态，不要只检查进程存活。
- 用 `minReadySeconds` 给新 Pod 稳定时间。
- 网关配置重试/超时/健康检查。
- 发布过程观察错误率与后端数量。

**预防**：

- 把“Endpoints Ready 数量”纳入发布门禁。
- 做金丝雀/灰度，先小流量验证。
- 统一端口命名与探针模板。

**教训**：Running 不等于 Ready，Ready 不等于能接流量；发布要看端到端链路。

## M.10 案例：删除 PVC 后数据无法恢复

**背景**：团队清理测试环境时删除了 PVC，随后发现生产数据也在这个 StorageClass 上。

**症状**：

- PVC 删除后 PV 进入 Released/Deleted。
- 重新创建 PVC 得到空卷。
- 备份系统没有覆盖该卷。

**证据**：

```bash
kubectl get pv,pvc -A
kubectl get storageclass <sc> -o yaml
kubectl describe pv <pv>
```

- StorageClass 的 `reclaimPolicy: Delete`。
- 没有 VolumeSnapshot/备份。
- PV 的 `persistentVolumeReclaimPolicy` 未单独保护。
- 删除操作没有准入/审批/回收站。

**根因**：默认回收策略 + 缺少备份 + 权限过大 + 环境混用。

**止血**：

1. 立即停止对该 StorageClass 的删除操作。
2. 从存储快照/备份恢复（如果有）。
3. 检查云盘回收站/快照策略。

**修复**：

- 关键数据使用 `Retain` 或独立 StorageClass。
- 配置 VolumeSnapshot/备份与恢复演练。
- 用 RBAC/准入限制 PVC/PV 删除。
- 环境隔离，生产凭证不进入测试。

**预防**：

- 给 PVC 打数据分级标签，策略按标签保护。
- 删除前检查挂载与备份状态。
- 定期做“误删恢复”演练。

**教训**：Kubernetes 的删除是“对象删除 + 存储回收”两件事；reclaimPolicy 决定后者。

## M.11 案例：CRD 升级后控制器崩溃

**背景**：Operator 升级，新增 CRD 字段并把旧字段标为必填/改变类型。

**症状**：

- 新版本控制器启动后 CrashLoop。
- 旧 CR 无法被读取/转换。
- API 报 schema 校验错误。

**证据**：

```bash
kubectl get crd <crd> -o yaml
kubectl get <cr> -o yaml
kubectl logs <operator> --previous
kubectl describe crd <crd>
```

- CRD schema 不兼容：类型变化、required 新增、枚举收窄。
- 多版本转换 webhook 不可用/逻辑错误。
- 旧对象没有默认值，新控制器假设字段存在。

**根因**：CRD 是 API 契约，升级没有遵循兼容性规则；缺少转换/默认值/迁移方案。

**止血**：

1. 回滚 CRD 与控制器到上一版本。
2. 恢复转换 webhook/证书。
3. 修复不兼容 schema，先做数据迁移。

**修复**：

- 遵循 CRD 版本兼容规则：新增可选字段、避免改类型、谨慎加 required。
- 使用 conversion webhook 做多版本转换，并测试旧对象。
- 为字段提供默认值与校验。
- 升级前备份 CR 并做 dry-run。

**预防**：

- 把 CRD 当公共 API 管理：版本、弃用、迁移、回滚。
- 在预发用真实旧对象测试升级。
- 监控 conversion webhook 的可用性与延迟。

**教训**：CRD 不是“随便改的 YAML”；它是用户依赖的 API 契约。

## M.12 案例：升级后部分节点 kubelet 不工作

**背景**：集群从 1.31 升级到 1.32，控制面先升级，部分工作节点后升级。

**症状**：

- 部分节点 NotReady。
- kubelet 日志报版本不兼容/证书/CRI 错误。
- 新 Pod 无法调度到这些节点。

**证据**：

```bash
kubectl get nodes -o wide
kubectl describe node <node>
journalctl -u kubelet --since "2 hours ago"
kubelet --version
kubeadm version
```

- kubelet 1.31 对 API Server 1.32 **仍在支持偏斜内**（kubelet 可比 API Server 旧最多 3 个 minor）。不要把它本身当作根因。
- 真正的根因通常是：控制面升级了，但节点上的 kubelet/CRI/CNI 只升级了一部分，或升级后配置/证书没同步；也可能是 1.32 的行为变更/弃用 API 影响了工作负载。
- 证书未轮换或 bootstrap token 过期。
- 节点内核/cgroup 配置不满足新版本要求。

**根因**：部分组件升级不同步（kubelet/CRI/CNI 版本组合不受支持），而不是“1.31 vs 1.32 的偏斜”。升级前缺少组件兼容矩阵与逐节点验证。

> 版本偏斜规则：kubelet 可以比 kube-apiserver 旧最多 3 个 minor，但不能更新；kubectl 支持 ±1 minor。看到“一个 minor 的差距”时，先查组件组合和配置，不要急着把所有节点升到同一版本。

**止血**：

1. 将异常节点隔离/排水。
2. 按支持矩阵升级 kubelet/运行时/CNI。
3. 修复证书/CRI 配置后重新加入。

**修复**：

- 遵循“控制面先、工作节点后；kubelet 不高于 API Server”的顺序。
- 用 `kubeadm upgrade node` 或发行版工具，不要手工替换二进制。
- 升级前跑 `kubeadm upgrade plan`/弃用检查。
- 分批升级，观察节点与业务。

**预防**：

- 维护版本矩阵与升级 runbook。
- 在预发完整演练一次。
- 监控节点版本、kubelet 重启、NotReady。

**教训**：Kubernetes 升级不是“换二进制”，而是控制面、节点、运行时、CNI、CSI、API 的综合变更。

## M.13 案例：多集群切换后流量打到空后端

**背景**：主集群故障，DNS/全局负载均衡切换到备集群，但大量请求 503。

**症状**：

- 备集群 Pod 正常，Service 存在。
- 全局 DNS 已切换，部分客户端仍失败。
- 备集群 Ingress/网关后端为空或证书不匹配。

**证据**：

```bash
kubectl --context backup get pods,svc,endpointslices -A
kubectl --context backup get ingress -A
kubectl --context backup get certificates -A
dig <service> @<dns>
```

- 备集群应用未部署/未 Ready。
- Ingress/Gateway 配置未同步，TLS 证书缺失/域名不匹配。
- DNS TTL 过长，客户端缓存旧记录。
- 数据层未复制/未切换，应用启动失败。

**根因**：多集群只做了“计算资源”，没有做“配置、证书、数据、DNS、演练”的完整切换设计。

**止血**：

1. 确认备集群后端 Ready 与证书有效。
2. 降低 DNS TTL/刷新缓存，逐步切流。
3. 必要时切回主集群。

**修复**：

- 用 GitOps 同步应用/策略/证书到多集群。
- 定义数据复制与切换流程（RPO/RTO）。
- 做端到端故障转移演练，包括 DNS/LB/证书/数据。
- 监控备集群的“可切换状态”，而不只是“集群存在”。

**预防**：

- 多集群不是目的，故障转移能力才是。
- 定期演练“主集群完全不可用”。
- 把证书、密钥、配额、网络策略纳入同步范围。

**教训**：故障转移的最后一公里通常是 DNS、证书和数据，而不是 Pod。

## M.14 案例：异常调用来自被泄露的 ServiceAccount token

**背景**：安全团队发现异常 API 调用，来源是一个应用 Pod 的 ServiceAccount。

**症状**：

- 审计日志出现大量 list secrets/创建 Pod 的请求。
- 调用来自集群内一个被攻陷的容器。
- 该 SA 绑定了过宽的 ClusterRole。

**证据**：

```bash
kubectl auth can-i --list --as=system:serviceaccount:<ns>:<sa>
kubectl get rolebindings,clusterrolebindings -A -o wide
# 1.24+ 默认使用 TokenRequest/投影卷，不再自动创建长期 token Secret
kubectl create token <sa> -n <ns> --duration=1h
# 仅用于排查手工创建的 legacy Secret：
kubectl get secret <sa-token-secret> -o yaml
```

- SA 绑定了 `cluster-admin` 或可读 Secret 的 Role。
- token 被写入镜像/日志/环境变量/CI；或使用了长期不过期的 legacy token Secret。
- 1.24+ 的投影 token 通常短期、绑定 audience/对象，泄露窗口更小，但不是“不会泄露”。
- 没有网络策略限制出站。
- 审计日志未告警。

**根因**：过度授权 + token 泄露 + 缺少运行时检测/网络隔离。

**止血**：

1. 禁用/轮换泄露的 token 与 SA。
2. 收紧 RBAC，移除不必要权限。
3. 隔离受影响工作负载，排查横向移动。
4. 检查 Secret/数据是否被读取。

**修复**：

- 每个工作负载独立 SA，最小权限。
- 关闭不必要的 automount，使用短期 token/工作负载身份。
- 启用审计与异常行为告警。
- 网络策略限制出站到 API Server/外部。
- 镜像/日志/CI 中禁止出现 token。

**预防**：

- 定期 access review。
- 把“谁能读 Secret”视为高权限。
- 做红队/故障演练，验证检测与响应。

**教训**：Kubernetes 的 RBAC 是“能做什么”，不是“身份是否可信”；token 泄露会把权限直接交给攻击者。

## M.15 案例：节点维护卡住，drain 一直失败

**背景**：计划升级节点，执行 `kubectl drain` 后卡住，PDB 阻止驱逐。

**症状**：

- `drain` 报 `Cannot evict pod as it would violate the pod's disruption budget`。
- 节点一直处于 cordon 状态，维护窗口被拖长。
- 业务副本数不足或 PDB 配置不合理。

**证据**：

```bash
kubectl get pdb -A
kubectl describe pdb <pdb>
kubectl get pods -o wide
kubectl get deployment <deploy> -o yaml
```

- PDB `minAvailable` 等于副本数，任何驱逐都被阻止。
- 副本集中在一个节点，PDB 无法满足。
- 有状态服务/单副本没有冗余。
- DaemonSet/本地存储 Pod 未处理。

**根因**：PDB 与副本分布/维护流程不匹配；缺少冗余与演练。

**止血**：

1. 临时扩容副本/迁移 Pod，满足 PDB。
2. 调整 PDB（谨慎，评估可用性）。
3. 对无法迁移的 Pod 制定例外流程。

**修复**：

- PDB 按真实可用性目标设置，不要“minAvailable=replicas”。
- 用反亲和/拓扑分布保证跨节点冗余。
- 维护前检查 PDB 与副本分布。
- 对单副本/有状态服务设计专门流程。

**预防**：

- 把 drain 演练纳入常态。
- 监控 PDB 的 `disruptionsAllowed`。
- 变更窗口预留缓冲。

**教训**：PDB 保护可用性，也会阻止维护；它是约束，不是“自动高可用”。

## M.16 案例：节点磁盘压力触发连锁驱逐

**背景**：一个日志量大的节点磁盘接近满，kubelet 开始驱逐 Pod，业务副本被集中打崩。

**症状**：

- 节点 DiskPressure=True，多个 Pod 被 Evicted。
- 其他节点资源不足，无法接收全部被驱逐 Pod。
- 业务错误率上升，HPA 扩容但调度失败。

**证据**：

```bash
kubectl describe node <node>
kubectl get pods -A -o wide | grep Evicted
journalctl -u kubelet | grep -i eviction
df -h; df -i
```

- 日志/镜像/emptyDir 占满磁盘。
- 驱逐阈值与预留资源不合理。
- 副本分布集中，缺少反亲和。
- 集群没有足够余量承接驱逐。

**根因**：节点容量治理缺失 + 驱逐连锁 + 调度余量不足。

**止血**：

1. 清理磁盘/扩容，解除 DiskPressure。
2. 临时扩容节点/迁移负载。
3. 降低日志写入/采样。

**修复**：

- 日志轮转、镜像清理、emptyDir 限额。
- 调整 kubelet 驱逐阈值与系统预留。
- 用拓扑分布/反亲和避免副本集中。
- 保留 N+1 容量与优先级/抢占策略。

**预防**：

- 监控磁盘/inode/驱逐事件。
- 做节点故障与容量演练。
- 把日志/监控 agent 的资源与磁盘占用纳入容量模型。

**教训**：驱逐不是“单 Pod 故障”，可能引发集群级连锁；容量与分布要一起设计。

## M.17 案例复盘模板（简版）

每个案例都可以用下面的模板落到自己的环境：

```text
标题：
影响：
时间线：
直接原因：
系统原因：
为什么没提前发现：
止血动作：
修复动作：
改进项（预防/检测/止血/恢复）：
负责人/截止时间：
验证方式：
```

复盘时重点回答：

1. 哪个信号最早出现？为什么没被看到？
2. 哪个变更/配置让故障成为可能？
3. 哪个自动化/流程可以在下次提前拦截？
4. 恢复过程是否可重复？是否演练过？
5. 改进项是否可验证、有负责人、有截止时间？

## 延伸阅读

- [第 31 章 故障排查方法论](../chapters/31-troubleshooting.md)
- [第 28 章 高可用与灾难恢复](../chapters/28-ha-disaster-recovery.md)
- [第 29 章 升级与节点运维](../chapters/29-upgrade-node-ops.md)
- [附录 C 故障排查清单](C-troubleshooting-checklist.md)
- [附录 K etcd 深入](K-etcd-operations.md)
- [附录 J 自测题库与实验手册](J-self-assessment-labs.md)
