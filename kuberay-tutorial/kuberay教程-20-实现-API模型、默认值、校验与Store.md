# 第 20 章 实现：API 模型、默认值、校验与 Store

本章拆开 MiniKubeRay 的“上半身”：对象如何表示、默认值/校验如何对齐 KubeRay、Store 如何提供 Kubernetes 语义。

## 20.1 常量与命名

`minikuberay/constants.py` 是唯一的“事实来源”：

- API group/version；
- 四个 CRD 的 kind；
- 标签（`ray.io/cluster`、`ray.io/node-type`、`ray.io/group`、`ray.io/serve` 等）；
- 注解（GCS FT、overwrite command、serve service、cron timestamp 等）；
- finalizer；
- 端口（6379/8265/10001/8080/8000/52365）；
- 默认值（Requeue 2s/300s、idle 60s、TTL、镜像）；
- 状态枚举（JobStatus、JobDeploymentStatus、conditions）。

好处：

- 控制器/Builder/测试不散落字符串；
- 关键常量与真实 KubeRay 对齐；少数模拟器专用项（如通用 `ray.io/raycluster-finalizer`、`minikuberay.io/*` 注解）明确标注为教学扩展，不冒充上游；
- 文档可以直接引用。

`minikuberay/version.py` 保存：

- `__version__`（本实现版本）；
- `KUBERAY_VERSION = "v1.7.1"`；
- `DEFAULT_RAY_VERSION = "2.59.0"`。

## 20.2 对象表示

所有对象都是 JSON 兼容 dict：

```text
{
  "apiVersion": "ray.io/v1",
  "kind": "RayCluster",
  "metadata": {
    "name": "demo",
    "namespace": "default",
    "uid": "...",
    "resourceVersion": "1",
    "generation": 1,
    "creationTimestamp": "...",
    "labels": {...},
    "annotations": {...},
    "finalizers": [...],
    "ownerReferences": [...]
  },
  "spec": {...},
  "status": {...}
}
```

`minikuberay/models/meta.py` 提供：

- `new_object` / `new_metadata`；
- `name` / `namespace` / `labels` / `annotations` / `uid`；
- `add_finalizer` / `remove_finalizer` / `is_being_deleted`；
- `owner_reference` / `owned_by`；
- `get_condition` / `set_condition` / `is_condition_true`；
- `merge_patch` / `update_status` / `status_equal`。

### 20.2.1 Condition 语义

`set_condition` 的行为：

- 不存在 -> 追加，设置 `lastTransitionTime`；
- 存在且 status 变化 -> 更新时间；
- reason/message 变化但 status 不变 -> 只改 reason/message，不动时间；
- `observedGeneration` 更新。

这与 Kubernetes `meta.SetStatusCondition` 接近，避免 status 抖动。

## 20.3 默认值

`minikuberay/models/defaults.py` 对四个 CRD 做默认：

### 20.3.1 RayCluster

- `rayVersion` 默认 `2.59.0`；
- head/worker template 没有容器时插入 `ray` 容器；
- `restartPolicy` 默认 `Never`；
- `serviceType` 默认 `ClusterIP`；
- worker `replicas` 0、`minReplicas` 0、`maxReplicas` maxInt32、`numOfHosts` 1、`priority` 0；
- autoscaling 开启时补 `autoscalerOptions`（idle 60、Default、version 按 Ray 版本）；
- GCS FT 后端默认 redis；
- TLS enabled 默认 false。

### 20.3.2 RayJob

- `submissionMode` 默认 K8sJobMode；
- `backoffLimit` 0、`ttlSecondsAfterFinished` 0、`shutdownAfterJobFinishes` false、`suspend` false；
- `submitterConfig.backoffLimit` 默认 2（SidecarMode 不注入 submitterConfig）；
- 对 `rayClusterSpec` 递归应用 RayCluster 默认值。

### 20.3.3 RayService

- `suspend` false、`excludeHeadPodFromServeSvc` false；
- `rayClusterDeletionDelaySeconds` 60；
- `upgradeStrategy.type` 默认 NewCluster；
- 增量升级补 `maxSurgePercent` 100、`stepSizePercent` 10、`intervalSeconds` 10；
- 对 `rayClusterConfig` 递归默认。

### 20.3.4 RayCronJob

- `suspend` false；
- 对 `jobTemplate` 递归默认 RayJob。

### 20.3.5 为什么默认值要集中

真实 KubeRay 的默认值分散在：

- CRD OpenAPI `default`；
- Go `setDefaults`；
- Pod builder 的兜底。

MiniKubeRay 集中到一处，便于测试“默认后的对象是否合法”。代价是与 CRD schema 的默认值可能不完全一致；`check_docs.py` 会校验样例。

## 20.4 校验

`minikuberay/models/validation.py` 的 `validate_object` 按 kind 分派。

### 20.4.1 元数据

- 名称必填；
- 长度：RayCluster 53、RayJob/RayService 47、RayCronJob 36；
- **Ray CR 名称使用 DNS-1035**（必须以小写字母开头，不能以数字开头）；worker group 名/namespace 仍用 DNS-1123；
- namespace；
- label 长度。

### 20.4.2 RayCluster

- `rayVersion` 语义版本；
- head 至少一个容器，Ray 容器必须是第一个（上游按位置识别；MiniKubeRay 额外要求名字为 `ray`）；
- worker group 名称唯一、DNS；
- `minReplicas <= maxReplicas`；
- `replicas` 会按 `[minReplicas, maxReplicas]` clamp（无论是否开启 autoscaling）；开启 autoscaling 时 min/max 必须有值（CRD 默认会补）；关闭时 autoscaler 不会改写 `replicas`，但 clamp 仍然生效；
- `numOfHosts >= 1`；
- autoscaler mode/version/idle；
- v2 从 Ray 2.47.0 起可用，2.48 起成为默认；MiniKubeRay 的校验下限是 2.47.0（`util.ray_supports_autoscaler_v2`）；
- GCS FT：redis 需要 address，rocksdb 不能有 address，active-passive 时间顺序；
- auth mode；
- networkPolicy mode；
- historyServer collector image；
- `managedBy` 枚举；
- upgradeStrategy 枚举；
- 顶层 resources/labels 与 rayStartParams 冲突检测；
- `labelRefs` 需要 Ray >= 2.45（MiniKubeRay 的前向兼容字段；v1.7.1 发布 API 未包含）。

### 20.4.3 RayJob

- submissionMode 枚举；
- suspend 需要 shutdown；
- clusterSelector 必须有 `ray.io/cluster`；
- clusterSelector 与 suspend/Sidecar/backoff 互斥；
- entrypoint（Interactive 除外）；
- TTL 需要 shutdown；
- deadline/backoff 正数；
- deletion strategy 的 legacy/rules 互斥；
- Sidecar 不支持 submitterPodTemplate/submitterConfig；
- RayClusterSpec 递归校验；
- RayJob 不支持 K8s token auth。

### 20.4.4 RayService

- rayClusterConfig 必填且校验；
- 不允许自定义 headService 名称；
- upgradeStrategy 枚举；
- 增量升级需要 autoscaling；
- maxSurge/step/interval/gateway 校验；
- K8s token auth 不支持。

### 20.4.5 RayCronJob

- schedule 五段；
- 不允许 TZ/CRON_TZ；
- timeZone 非空且可解析；
- jobTemplate 递归校验 RayJob；
- 名称长度。

### 20.4.6 校验失败的行为

- CLI `validate`：打印错误，退出码 1；
- 控制器：发 Warning Event，RayCluster/RayJob 写 `ValidationFailed` status（MiniKubeRay 为了可观察性）；
- API Server：目前只做默认，不做完整校验（与 KubeRay “无 webhook”一致）。

## 20.5 Store 的 Kubernetes 语义

`minikuberay/store/store.py` 的 `ApiStore` 是全书最重要的基础设施。

### 20.5.1 create

- 深拷贝对象；
- 补 kind/apiVersion；
- 补 namespace/uid/creationTimestamp；
- 分配 resourceVersion（从 1 开始）；
- generation = 1；
- 记录 ADDED 事件；
- 返回深拷贝。

### 20.5.2 update

- 乐观并发：比较 `resourceVersion`；
- spec 变化 -> generation++；
- status subresource：只替换 status，不碰 spec；
- metadata 的 uid/creationTimestamp 由 store 保留；
- 分配新 resourceVersion；
- 如果对象有 deletionTimestamp 且 finalizers 为空 -> 删除；
- 记录 MODIFIED/DELETED。

### 20.5.3 delete

- 没有 finalizer/force -> 删除，分配 tombstone resourceVersion，记录 DELETED；
- 有 finalizer -> 设置 deletionTimestamp，记录 MODIFIED；
- finalizer 清空后由 update 删除。

### 20.5.4 list

- 支持 namespace、label selector、field selector；
- label selector 同时支持：
  - flat `{key: value}`；
  - `{"matchLabels": {...}}`；
  - `matchExpressions`（In/NotIn/Exists/DoesNotExist）；
- 按 namespace/name 排序。

### 20.5.5 watch

- 默认先发送当前对象的 ADDED；
- 然后按 resourceVersion 追事件；
- 事件对象是深拷贝；
- 支持 sinceResourceVersion；
- `close()` 让生成器退出。

这足以支撑 API Server 的 `?watch=true` 和测试。

### 20.5.6 patch

- merge patch：RFC 7386；
- strategic/apply：递归 dict 合并，列表替换；
- JSON patch 未实现（返回 BadRequest）。

### 20.5.7 dump/load

- 用于快照/测试；
- load 恢复对象和 resourceVersion，不恢复事件日志。

## 20.6 LocalClient 与 HTTPClient

`minikuberay/kube/client.py`：

- `LocalClient`：直接调用 Store；控制器/测试用；
- `HTTPClient`：走 REST；CLI/跨进程用；
- `resource_path`：kind -> `/api/v1`、`/apis/batch/v1`、`/apis/ray.io/v1` 等；
- HTTPClient 把 404/409/400 映射为 `NotFound`/`Conflict`/`BadRequest`。

控制器直接通过 `ApiStore`（并经由 `LocalClient` 暴露同一套语义）读写对象，因此不需要网络；API Server 和 CLI 走 `HTTPClient`，因此可以独立进程运行。

## 20.7 测试

`tests/test_store.py` 覆盖：

- create/get/list；
- 命名空间/selector；
- 乐观并发；
- generation/status subresource；
- finalizer 两阶段；
- force delete；
- patch；
- watch current/new/since；
- dump/load；
- field selector。

`tests/test_defaults_validation.py` 覆盖默认值与校验。

## 20.8 Store 的不变量与常见 bug

`ApiStore` 是控制器的“地基”。下面这些不变量一旦破坏，控制器行为会变得难以复现：

| 不变量 | 含义 | 破坏后的症状 |
|---|---|---|
| resourceVersion 单调递增 | 每次写都分配新值 | watch 丢事件/重放错乱 |
| generation 只在 spec 变化时递增 | status/metadata 不增加 | 控制器误以为 spec 变了 |
| status 子资源不碰 spec | `update_status` 只替换 status | spec 被覆盖/漂移 |
| finalizer 两阶段删除 | 有 finalizer 只设 deletionTimestamp | 对象立即消失/子资源泄漏 |
| watch 按 rv 追事件 | 不重复/不跳过 | 控制器看到旧状态 |
| create 深拷贝 | 外部修改不影响 store | 隐式共享/竞态 |
| update 乐观并发 | rv 不匹配返回 Conflict | 最后写入者覆盖 |
| delete force 才跳过 finalizer | 非 force 遵守 finalizer | 清理逻辑被绕过 |

常见 bug：

1. **忘记深拷贝**：控制器修改返回对象，store 里的对象也跟着变；
2. **update 不带 rv**：覆盖别人的写入；
3. **status 写入触发 spec generation**：status 抖动；
4. **watch 从 0 开始**：重放全部历史；
5. **list 过滤掉 terminating 对象**：控制器看不到删除中的资源；
6. **patch 列表合并错误**：把列表当 dict 合并；
7. **finalizer 删除顺序错**：先移除 finalizer 再清理子资源。

测试这些不变量比测试“某个字段是否正确”更重要。

## 20.9 小结

- 常量集中，对象用 dict，默认/校验集中。
- Store 提供 resourceVersion、generation、status subresource、finalizer、watch——这是控制器正确性的基础。
- LocalClient/HTTPClient 分离，让同一套控制器既能进程内测试，也能走 REST。
- 默认值与校验的边界：API Server 宽松，控制器严格，CLI 显式。

下一章拆控制器、调度器、Kubelet 与自动扩缩容。

→ [第 21 章 实现：控制器、调度器、Kubelet 与自动扩缩容](kuberay教程-21-实现-控制器、调度器、Kubelet与自动扩缩容.md)
