# 第 31 章 升级、迁移、离线安装与灾难恢复

生产 KubeRay 平台最危险的动作不是创建集群，而是升级和迁移：CRD 变更、operator 行为变化、Ray 镜像升级、API 版本弃用、离线环境、灾备切换。本章给出可执行的流程和回滚预案。

## 31.1 版本矩阵

升级前先列出三组版本：

| 组件 | 例子 | 兼容性来源 |
|---|---|---|
| Kubernetes | 1.30+（`SidecarSubmitterRestart` 需 1.35+） | KubeRay release notes |
| KubeRay operator/CRD | v1.6.x → v1.7.1 | KubeRay release notes |
| Ray | 2.55 → 2.59 | Ray/KubeRay 兼容矩阵 |
| 批调度器 | Volcano/Kueue | 各自版本 |
| Gateway API | v1.3 | 增量升级要求 |

建议维护一个 `versions.yaml`：

```yaml
kubernetes: "1.32"
kuberay: "v1.7.1"
ray: "2.59.0"
gateway_api: "v1.3.0"
volcano: "v1.10.0"
kueue: "v0.10.0"
```

升级时一次只动一个维度，否则无法定位回归。

## 31.2 升级顺序

```text
1. 备份 CR/CRD/配置
2. 读 release notes / breaking changes
3. 在 staging 升级 CRD
4. 升级 operator
5. 观察 reconcile/错误/重启
6. 升级 Ray 镜像（按工作负载分批）
7. 验证 RayJob/RayService/autoscaler
8. 回滚预案
```

### 31.2.1 CRD 升级

Helm 不会自动升级 `crds/`。必须单独 apply：

```bash
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/ray-project/kuberay/v1.7.1/ray-operator/config/crd/bases/ray.io_rayclusters.yaml
# rayjobs/rayservices/raycronjobs 同理
```

注意：

- `--server-side` 避免 client-side apply 的注解大小限制；
- 先升级 CRD 再升级 operator，还是反过来？通常 **CRD 先**，确保新 operator 能读新字段；但旧 operator 要能容忍新 schema（只增字段通常没问题）；
- 备份 CRD：

```bash
kubectl get crd rayclusters.ray.io -o yaml > rayclusters.crd.backup.yaml
```

### 31.2.2 Operator 升级

```bash
helm upgrade kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system
kubectl -n kuberay-system rollout status deploy/kuberay-operator
```

高可用：

- `replicas: 2` + leader election；
- 滚动升级期间短暂切换；
- 控制器必须容忍重复调和；
- 监控 `controller_runtime_reconcile_errors_total`。

### 31.2.3 Ray 升级

RayCluster 不会原地滚动升级 Ray。升级方式：

| 场景 | 做法 |
|---|---|
| RayCluster | 改镜像/`rayVersion`，设置 `upgradeStrategy: Recreate` 或手动删 Pod |
| RayJob | 用新模板提交新作业 |
| RayService | 用新集群升级（NewCluster/增量） |
| 共享集群 | 建新集群，迁移作业，再删旧集群 |

注意：

- Python 依赖/ABI/CUDA 兼容；
- Ray 版本与 KubeRay 版本匹配；
- worker 与 head 必须同版本；
- 客户端 `ray` 版本与集群一致；
- runtime_env 里的包版本要重新验证。

“不改集群、只改 spec”的字段级语义（spec hash、`Recreate`/`None`、head 按住、worker 按组维护）见[第 55 章](55-change-management-upgrades.md)。

## 31.3 v1alpha1 → v1 迁移

早期 KubeRay 使用 `ray.io/v1alpha1`，后来稳定为 `ray.io/v1`。迁移要点：

### 31.3.1 确认存储版本

```bash
kubectl get crd rayclusters.ray.io -o jsonpath='{.spec.versions[*].name}'
kubectl get crd rayclusters.ray.io -o jsonpath='{.spec.versions[?(@.storage==true)].name}'
kubectl get crd rayclusters.ray.io -o jsonpath='{.spec.conversion.strategy}'
```

读这三个值：

- `served: true` 的版本可以被客户端读写；`storage: true` 的版本是 etcd 中的存储版本；
- v1.7 通常 `v1` 为 storage，`v1alpha1` 可能仍 served；
- 如果 `conversion.strategy` 是 `None`，API server **不做字段转换**，只是按目标版本 schema 序列化/反序列化；不要假设“升级 CRD 会自动把所有对象迁移成 v1”；
- 需要真正的 schema 转换时才有 `conversion.webhook`（KubeRay 核心 CRD 通常没有）。

### 31.3.2 字段差异

常见变化（以实际版本为准）：

- `spec.rayVersion` 的语义/默认；
- `spec.headGroupSpec`/`workerGroupSpecs` 的字段；
- `suspend`、`autoscalerOptions`；
- `gcsFaultToleranceOptions`（旧版可能是 annotation）；
- status 字段/conditions。

迁移步骤：

1. 导出所有 CR，并去掉服务端字段（否则 `kubectl apply` 会报 resourceVersion/uid/managedFields 冲突）：

```bash
kubectl get rayclusters,rayjobs,rayservices,raycronjobs -A -o yaml | \
  yq 'del(.items[].metadata.resourceVersion,
          .items[].metadata.uid,
          .items[].metadata.creationTimestamp,
          .items[].metadata.managedFields,
          .items[].status)' > all-ray.yaml
```

2. 用脚本/`yq` 把 `apiVersion` 改为 `ray.io/v1`；
3. 在 staging apply，观察默认值/校验/schema 裁剪；
4. 对比 status/子资源；
5. 分批切换生产；保留旧导出作为回滚输入。

### 31.3.3 旧 annotation

旧版 GCS FT 使用：

```yaml
metadata:
  annotations:
    ray.io/ft-enabled: "true"
    ray.io/external-storage-namespace: demo
```

新版推荐：

```yaml
spec:
  gcsFaultToleranceOptions:
    backend: redis
    redisAddress: ...
    externalStorageNamespace: demo
```

两者不能同时设置（校验会拒绝）。迁移时先改 spec，再删 annotation。

## 31.4 离线/air-gapped 安装

### 31.4.1 镜像清单

需要镜像：

- `kuberay/operator:v1.7.1`；
- Ray 镜像（CPU/GPU/ML）；
- autoscaler 镜像（通常与 Ray 相同）；
- History Server collector；
- Redis（如果 GCS FT）；
- 示例/业务镜像；
- 批调度器/Kueue/Gateway（如果使用）。

导出/导入：

```bash
docker pull quay.io/kuberay/operator:v1.7.1
docker save quay.io/kuberay/operator:v1.7.1 -o operator.tar
# 传输到离线环境
docker load -i operator.tar
docker tag quay.io/kuberay/operator:v1.7.1 registry.local/kuberay/operator:v1.7.1
docker push registry.local/kuberay/operator:v1.7.1
```

或用 `skopeo copy`：

```bash
skopeo copy docker://quay.io/kuberay/operator:v1.7.1 \
  docker://registry.local/kuberay/operator:v1.7.1
```

### 31.4.2 Helm chart 离线

```bash
helm pull kuberay/kuberay-operator --version 1.7.1
helm pull kuberay/kuberay-apiserver --version 1.7.1
```

把 chart 和 values 一起放进 Git/制品库。

### 31.4.3 values 覆盖镜像

```yaml
image:
  repository: registry.local/kuberay/operator
  tag: v1.7.1
  pullPolicy: IfNotPresent
imagePullSecrets:
  - name: registry-credential
```

### 31.4.4 Ray 作业的依赖

离线环境没有公网：

- 预装 Python 包到镜像；
- `working_dir` 放内部对象存储/MinIO；
- 模型权重放内部存储；
- `runtime_env.pip` 指向内部 PyPI mirror 或禁用；
- HF/模型下载设置离线缓存。

### 31.4.5 验证

```bash
kubectl -n kuberay-system get pods
kubectl apply -f manifests/samples/ray-cluster.yaml
kubectl get raycluster
kubectl exec <head> -- ray status
```

## 31.5 多集群与灾备

### 31.5.1 模式

| 模式 | 说明 | RTO/RPO |
|---|---|---|
| 单集群多 namespace | 最常见 | 集群故障=全挂 |
| 多集群按环境 | dev/staging/prod | 环境隔离 |
| 多集群按地域 | 数据本地性/容灾 | 依赖切换 |
| Kueue MultiKueue | 跨集群调度 | 需 Kueue 配置 |
| 双活 | 业务层路由 | 复杂 |

KubeRay 本身不提供跨集群复制；需要：

- GitOps 同步 CR；
- 对象存储跨区复制；
- 镜像仓库多区；
- DNS/Gateway 切换；
- 数据层复制。

### 31.5.2 备份对象

| 对象 | 方式 |
|---|---|
| CR（RayCluster/RayJob/RayService/RayCronJob） | Git/定期导出 |
| CRD/operator 配置 | Git/Helm values |
| GCS RocksDB PVC | VolumeSnapshot |
| Redis | AOF/RDB/托管备份 |
| 对象存储 | 版本/跨区复制 |
| 模型/checkpoint | 对象存储版本 |
| Secret | Sealed Secrets/External Secrets/Vault |

### 31.5.3 恢复顺序

```text
1. Kubernetes/节点/CNI/CSI/GPU
2. 镜像仓库/Secret
3. CRD
4. operator
5. 基础设施（Redis/对象存储/Gateway）
6. 租户 CR
7. 验证 RayCluster/RayJob/RayService
```

### 31.5.4 RTO/RPO 设计

- RPO：CR/对象存储/checkpoint 的备份频率；
- RTO：集群/operator/存储恢复时间；
- Ray 对象存储是节点本地，节点丢失会丢对象；关键数据必须落对象存储；
- GCS FT 只保护元数据，不保护用户数据/对象存储；
- Redis 故障时 GCS FT 失效，需要恢复 Redis 或重建集群。

## 31.6 升级/迁移检查表

- [ ] 版本矩阵已确认
- [ ] release notes/breaking changes 已读
- [ ] CR/CRD/Helm values 已备份
- [ ] staging 已升级并跑通 RayCluster/RayJob/RayService
- [ ] CRD 已单独 apply
- [ ] operator 滚动升级完成
- [ ] Ray 镜像分批升级
- [ ] feature gate 一次只开一个
- [ ] 监控/告警正常
- [ ] 回滚预案演练
- [ ] 离线环境镜像/chart/依赖齐全
- [ ] 灾备恢复演练

## 31.7 回滚

### 31.7.1 Operator 回滚

```bash
helm rollback kuberay-operator -n kuberay-system
kubectl -n kuberay-system rollout status deploy/kuberay-operator
```

注意：

- 新 CRD 字段旧 operator 可能忽略；
- 新 operator 写入的 status 旧 operator 可能不认识；
- CRD 回滚通常不可行（schema 只增不减），要确认兼容性。

### 31.7.2 业务 CR 回滚

- Git 回滚到上一版本；
- `kubectl apply`；
- RayService 回滚会删除 pending/切回 active；
- RayJob 回滚通常重跑；
- RayCluster 模板回滚可能需要删 Pod（`Recreate`）。

## 31.8 CRD Diff 与兼容性检查

升级前不要只看版本号，要看 schema 实际变化。

### 31.8.1 Schema diff

```bash
# 备份当前 CRD
kubectl get crd rayclusters.ray.io -o yaml > old-crd.yaml

# 取新 CRD
curl -sL https://raw.githubusercontent.com/ray-project/kuberay/v1.7.1/ray-operator/config/crd/bases/ray.io_rayclusters.yaml > new-crd.yaml

# 比较（需要 yq）
diff <(yq '.spec.versions[0].schema' old-crd.yaml) \
     <(yq '.spec.versions[0].schema' new-crd.yaml) | head -100
```

关注：

- 新增字段（通常兼容）；
- 删除/重命名字段（breaking）；
- 默认值变化（行为变化）；
- 枚举/范围收紧（旧对象可能非法）；
- `x-kubernetes-preserve-unknown-fields` 变化；
- storage/served 版本。

### 31.8.2 Dry-run

```bash
kubectl apply --server-side --dry-run=server -f new-crd.yaml
kubectl apply --dry-run=server -f all-ray-objects.yaml
```

Dry-run 能发现：

- schema 不合法；
- 不可变字段；
- 准入策略拒绝；
- 默认值冲突。

### 31.8.3 对象兼容性

```bash
# 导出所有 CR，并剥离服务端字段
kubectl get rayclusters,rayjobs,rayservices,raycronjobs -A -o yaml | \
  yq 'del(.items[].metadata.resourceVersion, .items[].metadata.uid,
          .items[].metadata.creationTimestamp, .items[].metadata.managedFields,
          .items[].status)' > all-ray.yaml

# 在 staging apply
kubectl --context staging apply -f all-ray.yaml

# 对比 status/子资源
kubectl --context staging get rayclusters -A -o yaml > staged.yaml
diff <(yq '.items[].status' all-ray.yaml) <(yq '.items[].status' staged.yaml)
```

不要假设“schema 只增字段”就一定安全：默认值/枚举/校验收紧也会破坏旧对象。

### 31.8.4 Helm 渲染 diff

```bash
helm template kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system -f values-prod.yaml > new-rendered.yaml

# 与当前 release 比较
kubectl -n kuberay-system get deploy kuberay-operator -o yaml > current.yaml
diff <(yq '.spec' current.yaml) <(yq 'select(.kind=="Deployment") | .spec' new-rendered.yaml)
```

## 31.9 练习

1. 在 kind 上从 KubeRay v1.6.x 升级到 v1.7.1，记录 CRD/operator 步骤。
2. 导出一个 v1alpha1 RayCluster，改成 v1 并 apply。
3. 搭一个离线 kind 集群，只用本地镜像安装 operator。
4. 用 VolumeSnapshot 备份 RocksDB PVC，删除集群后恢复。
5. 设计一个跨区灾备方案，写出 RTO/RPO。
6. 演练一次 operator 回滚，记录失败点。

## 31.10 回滚决策树

回滚不是“再 helm rollback 一次”这么简单。先判断你回滚的是哪一层。

```text
出问题了，先问：哪一层变了？
├─ 业务 CR（镜像/资源/Serve 配置）
│   ├─ RayService：Git 回滚 + apply；pending 会被删除
│   ├─ RayJob：通常重跑，不能“回滚”已完成的作业
│   └─ RayCluster：模板回滚 + Recreate/删 Pod
├─ operator 镜像/参数
│   ├─ helm rollback（版本可回）
│   └─ 注意新 CRD 字段旧 operator 可能忽略
├─ CRD schema
│   ├─ 只增字段：通常可不回滚
│   ├─ 删除/改语义：不能简单回滚，需要迁移
│   └─ storage version 变化：高风险，先备份
├─ Ray 版本
│   ├─ 新建集群用旧镜像
│   └─ 已运行集群需要重建/迁移
└─ 基础设施（Redis/对象存储/Gateway）
    ├─ 数据层回滚可能不可逆
    └─ 先切流量/停写，再恢复
```

### 31.10.1 可以回滚的

- operator Deployment/Chart（保留上一 revision）；
- 业务 CR 的 spec（Git/备份）；
- RayService 的 `serveConfigV2`/镜像（回滚会删除 pending）；
- 节点池/副本数；
- Gateway/HTTPRoute 权重。

### 31.10.2 不能简单回滚的

- 已经执行的数据迁移/格式变更；
- 已写入对象存储的新格式；
- CRD 删除字段/收紧枚举后，旧对象可能不合法；
- Ray 版本升级后已写入的新序列化格式；
- 已删除的 PVC/Redis 数据。

### 31.10.3 回滚演练清单

- [ ] 回滚目标版本明确
- [ ] 上一版本镜像/Chart 可用
- [ ] CRD 兼容性已确认
- [ ] 数据迁移有反向脚本/备份
- [ ] 流量切换/停写步骤
- [ ] 回滚后的 smoke test
- [ ] 回滚失败的二次预案

### 31.10.4 回滚的“时间窗口”

- `rayClusterDeletionDelaySeconds` 决定旧集群还能保留多久；
- Helm revision 默认保留有限数量；
- 对象存储版本/生命周期决定旧数据可恢复多久；
- Redis 备份频率决定 RPO。

回滚预案必须写清楚“在多长时间内有效”。

## 31.11 小结

- 升级顺序：备份 → CRD → operator → Ray → 验证。
- Helm 不升级 CRD；CRD 必须单独 apply。
- v1alpha1→v1 迁移要处理 apiVersion、字段、annotation。
- 离线安装的核心是镜像/chart/依赖三件事。
- KubeRay 不提供跨集群复制；灾备要自己设计 RTO/RPO。
- 回滚预案要在升级前演练。

下一章把前面所有内容收束成一个多租户 Ray 平台设计。

→ [第 32 章 平台实战：多租户 Ray 平台设计](32-platform-capstone.md)
