# 附录 C 故障排查清单

## C.1 通用第一步

- [ ] 影响范围：单 Pod / 单服务 / 单节点 / 单 AZ / 全集群。
- [ ] 开始时间与最近变更（发布/配置/扩缩容/证书/网络）。
- [ ] 用户症状：4xx/5xx/超时/连接拒绝/DNS/Pending/CrashLoop。
- [ ] `kubectl get pods -A -o wide`
- [ ] `kubectl get events -A --sort-by=.lastTimestamp`
- [ ] `kubectl get nodes`
- [ ] 相关 Deployment/Service/Endpoints/PVC 状态。
- [ ] 先止血（回滚/扩容/切流/降级），保留证据。

## C.2 Pod Pending

- [ ] 有 `spec.nodeName` 吗？
- [ ] Events 中的 `FailedScheduling` 原因。
- [ ] 节点资源（CPU/内存/Pod 数）是否足够。
- [ ] 污点/容忍。
- [ ] nodeSelector/nodeAffinity。
- [ ] PVC 是否 Bound；StorageClass/WFFC/拓扑。
- [ ] hostPort 冲突。
- [ ] 所有节点 NotReady/unschedulable。
- [ ] PDB/抢占是否阻止。
- [ ] 调度器是否运行/leader。

## C.3 CrashLoopBackOff

- [ ] `kubectl logs --previous`。
- [ ] `lastState`：OOMKilled/Error/Completed。
- [ ] 退出码：137（OOM/SIGKILL）、1（应用错误）、126/127（权限/命令）。
- [ ] 配置/Secret/ConfigMap 是否存在。
- [ ] 探针是否误杀。
- [ ] 镜像 entrypoint/command 是否正确。
- [ ] 只读文件系统/权限。
- [ ] 依赖服务是否可用。
- [ ] 资源 limit 是否太小。

## C.4 ImagePullBackOff

- [ ] 镜像名/tag 是否正确。
- [ ] 私有仓库 imagePullSecret。
- [ ] 凭据是否过期。
- [ ] 节点能否访问仓库（DNS/网络/代理）。
- [ ] 仓库限流。
- [ ] 架构（amd64/arm64）。
- [ ] 镜像是否被删除。
- [ ] 节点磁盘是否满。

## C.5 Service 不通

- [ ] Endpoints/EndpointSlice 是否为空。
- [ ] Service selector 与 Pod labels。
- [ ] Pod 是否 Ready。
- [ ] targetPort 与容器端口。
- [ ] Pod IP 直连是否通。
- [ ] DNS 是否解析。
- [ ] NetworkPolicy 是否阻止。
- [ ] kube-proxy/eBPF 是否正常。
- [ ] 节点路由/安全组/防火墙。
- [ ] Ingress/Gateway 后端配置。

## C.6 DNS

- [ ] `/etc/resolv.conf`。
- [ ] CoreDNS Pod Ready/副本。
- [ ] CoreDNS 日志。
- [ ] Service/namespace 名字。
- [ ] ndots/search 域。
- [ ] NetworkPolicy UDP/TCP 53。
- [ ] 节点上游 DNS。
- [ ] conntrack/UDP 丢包。

## C.7 节点 NotReady

- [ ] `kubectl describe node` 条件。
- [ ] kubelet 状态/日志。
- [ ] 容器运行时状态。
- [ ] 磁盘/内存/PID 压力。
- [ ] CNI 是否就绪。
- [ ] 节点到 API Server 网络。
- [ ] 证书过期。
- [ ] 系统 OOM/内核日志。

## C.8 控制面

- [ ] `/readyz`、`/livez`。
- [ ] API Server 日志/证书。
- [ ] etcd 健康/leader/quorum/fsync。
- [ ] 控制面节点资源。
- [ ] LB 健康检查。
- [ ] 控制器/调度器 leader。
- [ ] APF 限流/排队。
- [ ] 近期升级/证书轮换。

## C.9 存储

- [ ] PVC/PV 状态。
- [ ] StorageClass 是否存在/默认类冲突。
- [ ] 容量/IOPS/配额。
- [ ] WFFC/拓扑。
- [ ] CSI Controller/Node 日志。
- [ ] attach/detach 限流。
- [ ] 文件系统/权限（fsGroup）。
- [ ] RWO 是否被其他节点挂载。
- [ ] 快照/备份是否可用。

## C.10 性能

- [ ] `kubectl top pod/node`。
- [ ] CPU throttling。
- [ ] 内存压力/OOM。
- [ ] 连接池/线程池。
- [ ] DNS 延迟。
- [ ] 下游依赖。
- [ ] 探针超时/重启。
- [ ] 镜像拉取/启动。
- [ ] 调度 Pending。
- [ ] 网络丢包/重传/MTU。

## C.11 安全

- [ ] 401 vs 403。
- [ ] RBAC `auth can-i --list`。
- [ ] ServiceAccount/token/audience。
- [ ] NetworkPolicy。
- [ ] Pod Security。
- [ ] 镜像签名/仓库。
- [ ] Secret 权限/加密。
- [ ] 证书过期。
- [ ] 审计日志。

## C.12 升级

- [ ] 版本偏斜。
- [ ] 弃用 API。
- [ ] CRD 版本/转换。
- [ ] webhook/CNI/CSI 兼容。
- [ ] etcd 备份。
- [ ] 证书。
- [ ] 容量缓冲。
- [ ] PDB。
- [ ] 回滚计划。

## C.13 事故复盘

- [ ] 时间线。
- [ ] 影响范围/用户。
- [ ] 根因（技术/流程）。
- [ ] 发现方式（监控/用户/随机）。
- [ ] 止血措施。
- [ ] 恢复时间。
- [ ] 改进项/负责人/截止时间。
- [ ] 是否需要更新 runbook/告警/测试。

## C.14 MiniK8s 专用

```bash
python -m mini_k8s.cli get pods -o wide
python -m mini_k8s.cli describe pod <pod>
python -m mini_k8s.cli get events
python -m mini_k8s.cli logs <pod>
python -m mini_k8s.cli top pods
```

检查：

- [ ] Pod 是否调度（spec.nodeName）。
- [ ] status.conditions。
- [ ] containerStatuses。
- [ ] 模拟注解是否设置正确。
- [ ] 查看 Event 对象（`kubectl get events`）中的控制器错误；MiniK8s 的 `--verbose` 只影响 API Server 日志，不记录控制器线程异常。
- [ ] PVC/ConfigMap/Secret 是否存在。
- [ ] NetworkPolicy 判定。

## 延伸阅读

- [第 31 章 故障排查方法论](../chapters/31-troubleshooting.md)
- [第 27 章 可观测性](../chapters/27-observability.md)
- [附录 M 生产事故案例库](M-incident-casebook.md)
- [附录 K etcd 深入](K-etcd-operations.md)
