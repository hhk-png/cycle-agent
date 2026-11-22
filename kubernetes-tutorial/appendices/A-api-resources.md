# 附录 A API 资源速查

## A.1 核心组（v1）

| 资源 | 复数/短名 | 作用域 | 说明 |
|---|---|---|---|
| Namespace | namespaces/ns | 集群 | 逻辑隔离/权限边界 |
| Node | nodes/no | 集群 | 节点注册与状态 |
| Pod | pods/po | 命名空间 | 最小运行单元 |
| Service | services/svc | 命名空间 | 稳定虚拟 IP/DNS |
| Endpoints | endpoints/ep | 命名空间 | 传统后端列表 |
| EndpointSlice | endpointslices | 命名空间 | 分片后端列表（discovery.k8s.io/v1） |
| ConfigMap | configmaps/cm | 命名空间 | 非敏感配置 |
| Secret | secrets | 命名空间 | 敏感数据（默认仅 base64） |
| ServiceAccount | serviceaccounts/sa | 命名空间 | Pod 身份 |
| PersistentVolume | persistentvolumes/pv | 集群 | 存储供给 |
| PersistentVolumeClaim | persistentvolumeclaims/pvc | 命名空间 | 存储请求 |
| Event | events/ev | 命名空间 | 集群事件 |
| ResourceQuota | resourcequotas/quota | 命名空间 | 总量配额 |
| LimitRange | limitranges/limits | 命名空间 | 默认/范围限制 |
| Binding | bindings | 命名空间 | 调度绑定子资源 |
| PodTemplate | podtemplates | 命名空间 | Pod 模板（少用） |

## A.2 apps/v1

| 资源 | 短名 | 作用域 | 说明 |
|---|---|---|---|
| Deployment | deployments/deploy | 命名空间 | 无状态发布 |
| ReplicaSet | replicasets/rs | 命名空间 | 副本保证 |
| StatefulSet | statefulsets/sts | 命名空间 | 稳定身份/存储 |
| DaemonSet | daemonsets/ds | 命名空间 | 每节点一个 |
| ControllerRevision | controllerrevisions | 命名空间 | 版本历史 |

## A.3 batch/v1

| 资源 | 短名 | 作用域 | 说明 |
|---|---|---|---|
| Job | jobs | 命名空间 | 运行到完成 |
| CronJob | cronjobs/cj | 命名空间 | 定时 Job |

## A.4 网络

| 资源 | API 组/版本 | 短名 | 说明 |
|---|---|---|---|
| Ingress | networking.k8s.io/v1 | ing | L7 路由 |
| IngressClass | networking.k8s.io/v1 | | 控制器类别 |
| NetworkPolicy | networking.k8s.io/v1 | netpol | L3/L4 策略 |
| EndpointSlice | discovery.k8s.io/v1 | | 后端分片 |
| GatewayClass/Gateway/HTTPRoute | gateway.networking.k8s.io/v1 | | Gateway API（需安装 CRD） |

## A.5 存储

| 资源 | API 组/版本 | 短名 | 说明 |
|---|---|---|---|
| StorageClass | storage.k8s.io/v1 | sc | 动态供给 |
| CSIDriver | storage.k8s.io/v1 | | CSI 驱动 |
| CSINode | storage.k8s.io/v1 | | 节点 CSI 信息 |
| VolumeAttachment | storage.k8s.io/v1 | | 卷挂载 |
| VolumeSnapshot | snapshot.storage.k8s.io/v1 | | 快照（需 CRD） |

## A.6 RBAC

| 资源 | 作用域 | 说明 |
|---|---|---|
| Role | 命名空间 | 命名空间权限 |
| RoleBinding | 命名空间 | 绑定 Role/ClusterRole |
| ClusterRole | 集群 | 集群级权限 |
| ClusterRoleBinding | 集群 | 绑定 ClusterRole |

## A.7 策略/调度/伸缩

| 资源 | API 组/版本 | 短名 | 说明 |
|---|---|---|---|
| PodDisruptionBudget | policy/v1 | pdb | 自愿驱逐保护 |
| PriorityClass | scheduling.k8s.io/v1 | pc | 优先级 |
| HorizontalPodAutoscaler | autoscaling/v2 | hpa | 水平伸缩 |
| VerticalPodAutoscaler | autoscaling.k8s.io/v1 | vpa | 垂直伸缩（需安装） |
| ResourceQuota | v1 | quota | 配额 |
| LimitRange | v1 | limits | 默认/范围 |

## A.8 扩展/协调

| 资源 | API 组/版本 | 说明 |
|---|---|---|
| CustomResourceDefinition | apiextensions.k8s.io/v1 | 注册 CRD |
| Lease | coordination.k8s.io/v1 | 选主/租约 |
| ValidatingWebhookConfiguration | admissionregistration.k8s.io/v1 | 校验 webhook |
| MutatingWebhookConfiguration | admissionregistration.k8s.io/v1 | 变更 webhook |
| ValidatingAdmissionPolicy | admissionregistration.k8s.io/v1 | CEL 校验策略（1.30 GA） |
| MutatingAdmissionPolicy | admissionregistration.k8s.io/v1 | CEL 变更策略（1.36 GA） |
| RuntimeClass | node.k8s.io/v1 | 选择容器运行时/沙箱 |
| ResourceClaim/ResourceClaimTemplate | resource.k8s.io/v1（1.34 GA；1.32/1.33 为 v1beta1/v1beta2，1.31 为 v1alpha3） | DRA 设备请求（版本相关） |
| DeviceClass/ResourceSlice | resource.k8s.io/v1（同上） | DRA 设备类别与节点设备清单 |

## A.9 指标/认证/授权

| 资源 | API 组/版本 | 说明 |
|---|---|---|
| NodeMetrics/PodMetrics | metrics.k8s.io/v1beta1 | metrics-server |
| TokenReview | authentication.k8s.io/v1 | 验证 token |
| TokenRequest | authentication.k8s.io/v1 | 签发 SA token |
| SelfSubjectAccessReview | authorization.k8s.io/v1 | 我能做什么 |
| SubjectAccessReview | authorization.k8s.io/v1 | 某人能做什么 |
| SelfSubjectRulesReview | authorization.k8s.io/v1 | 我的规则 |

## A.10 常见子资源

| 子资源 | 作用 |
|---|---|
| `/status` | 读写状态 |
| `/scale` | 读写副本数 |
| `/log` | 容器日志 |
| `/exec` | 执行命令 |
| `/attach` | attach 到容器 |
| `/portforward` | 端口转发 |
| `/eviction` | 驱逐 Pod |
| `/binding` | 调度绑定 |
| `/token` | 签发 SA token |
| `/finalize` | Namespace 清理 |
| `/ephemeralcontainers` | 临时容器 |

## A.11 命名空间 vs 集群

判断资源作用域：

```bash
kubectl api-resources --namespaced=true
kubectl api-resources --namespaced=false
python -m mini_k8s.cli api-resources
```

常见错误：

- 对集群资源使用 `-n`（无效）。
- 对命名空间资源忘记 namespace（落到 default）。
- RBAC 只在一个 namespace 生效，误以为全局。

## A.12 版本与弃用

- 使用 `kubectl api-resources` 查看当前集群支持的版本。
- 升级前用 `pluto`/`kube-no-trouble` 扫描弃用 API。
- 不要使用已移除的 `extensions/v1beta1`、`policy/v1beta1`。
- CRD 的 served/storage 版本要明确。

## 延伸阅读

- Kubernetes API Reference：`https://kubernetes.io/docs/reference/kubernetes-api/`
- [第 4 章 API 对象模型](../chapters/04-api-object-model.md)
- [附录 B kubectl 速查](B-kubectl.md)
