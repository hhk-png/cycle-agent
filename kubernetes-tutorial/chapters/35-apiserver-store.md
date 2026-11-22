# 第 35 章 API Server 与存储：对象如何被保存和观察

## 35.1 两个核心模块

- `mini_k8s/store/store.py`：对象存储、resourceVersion、watch。
- `mini_k8s/api/server.py`：HTTP、路由、认证/授权、准入、子资源、discovery。

它们对应真实 Kubernetes 的：

- etcd + API Server 的 storage layer。
- API Server 的 REST handler + watch cache。

## 35.2 ResourceStore 数据结构

```python
self._data: dict[GVR, dict[(namespace, name), dict]]
self._resource_version: int
self._watchers: list[Watcher]
self._events: deque[WatchEvent]
```

每个 GVR（group/version/plural）一个 bucket：

```python
GVR = ("apps", "v1", "deployments")
key = ("default", "web")
```

object 是 deep copy 的 dict，包含：

- 原始字段。
- 服务器补充的 `metadata.uid`、`resourceVersion`、`creationTimestamp`、`generation`。
- 默认的 labels/annotations/finalizers/ownerReferences。

## 35.3 create/update/delete

### create

```python
def create(gvr, obj, kind, namespace):
    validate_name(...)
    rv = next_rv()
    meta["resourceVersion"] = str(rv)
    meta["uid"] = new_uid()
    meta["creationTimestamp"] = now_iso()
    meta["generation"] = 1
    store
    emit ADDED
```

特点：

- 重名返回 409 AlreadyExists。
- 不执行准入（由 API Server 或调用方负责）。
- 写入时 deep copy，读取时也 deep copy，避免外部修改。
- `uid`、`resourceVersion`、`creationTimestamp`、`generation` 由服务器强制覆盖，客户端伪造无效；`apiVersion`/`kind` 必须与 URL 资源一致，否则 400。

### update

```python
if supplied_rv and supplied_rv != existing.rv: raise 409
meta.resourceVersion = next_rv()
if obj.spec != existing.spec: generation += 1
else: generation unchanged
preserve uid/creationTimestamp/deletionTimestamp
emit MODIFIED
```

这是乐观并发的核心。MiniK8s 的 resourceVersion 是整数，真实 etcd 是 revision 字符串；客户端不应解析。

### delete

```python
if finalizers and not force:
    set deletionTimestamp
    emit MODIFIED
    return False
remove object
emit DELETED
return True
```

finalizer 语义与真实 Kubernetes 一致：对象先标记删除，清理后物理删除。控制器通过 `update` 把 `finalizers` 清空时，`update` 会在同一次写事务里完成物理删除并发出 DELETED 事件，不需要再额外调用 `delete`。

## 35.4 list 与 selector

```python
objects, token = store.list(
    gvr, namespace=None, *,
    label_selector=None, field_selector=None,
    limit=None, continue_token=None,
)
```

- namespace=None 表示所有命名空间。
- `label_selector`/`field_selector` 等是**关键字参数**；返回值是 `(objects, next_token)`。
- label selector 支持 `=`, `!=`, `in`, `notin`, `exists`, `!`。
- field selector 支持 `metadata.name`、`spec.nodeName`、`status.phase` 等。
- 排序按 `(namespace, name)`，保证测试稳定。
- 分页用 offset token（真实 Kubernetes 用 continue token + etcd 分页）。

**分页不是快照一致的**：MiniK8s 每次分页都重新排序当前对象再按 offset 切片，并返回当前全局 resourceVersion；分页之间对象增删会让结果漂移。真实 Kubernetes 的 continue token 绑定到一个快照/资源版本，语义更强。做替换实现时，不能把 MiniK8s 的 offset 分页当成真实契约。

MiniK8s 的 selector 实现在 `apimachinery/selectors.py`。

## 35.5 watch

```python
watcher = store.watch(gvr, namespace, resource_version=rv, send_initial=False)
for event in watcher.events():
    ...
```

`resource_version` 与 `send_initial` 是**关键字参数**；`send_initial=True` 且 `resource_version=0` 时先发快照。

实现：

1. 新 watcher 注册到 `_watchers`。
2. 从 `_events` 历史中重放 `rv` 之后的事件。
3. 新事件通过 `queue.Queue` 推送。
4. 慢消费者队列满时清空积压并发送 ERROR（`code: 410`），随后停止推送（模拟 “too slow”），客户端应重新 List。
5. 事件包含 `type`、`object`、`resource_version`、`gvr`。

API Server 的 `/watch`：

- 设置 `Transfer-Encoding: chunked`。
- 每个事件写一行 JSON。
- 支持 timeoutSeconds、labelSelector、fieldSelector。
- **带 selector 的 watch 会合成事件**：对象从不匹配变为匹配时发 ADDED，从匹配变为不匹配时发 DELETED，匹配且更新时发 MODIFIED；非匹配更新被抑制。这样客户端只靠事件就能维护“当前匹配集合”，不需要自己做 diff。实现见 `api/server.py` 的 watch 循环，`tests/test_api.py` 覆盖。
- `resourceVersion=0` 时先发送现有对象（ADDED 快照），再进入实时事件。
- `allowWatchBookmarks=true` 时在连接建立时发送一个 BOOKMARK，携带当前 resourceVersion。
- 客户端断开时关闭 watcher。

真实 Kubernetes 还有：

- watch cache（减少 etcd watch）。
- 周期性 bookmark（不是只在连接建立时）。
- resourceVersion 匹配/一致读。
- 事件压缩/缓存淘汰策略。

MiniK8s 保留最近 5000 个事件。若请求的 `resourceVersion` 早于保留窗口，watcher 会收到 `ERROR` 事件（`code: 410`, `too old resource version`），客户端应重新 List 并从头建立 watch——不会静默跳过一个空洞。测试 `test_watch_too_old_resource_version_returns_410` 覆盖。

## 35.6 API 路由

`APIHandler._parse_route` 把 URL 解析为：

```python
Route(kind="resource", group="apps", version="v1", plural="deployments",
      namespace="default", name="web", subresource=None)
```

支持：

```text
/api/v1/namespaces/default/pods
/api/v1/namespaces/default/pods/web/log
/apis/apps/v1/namespaces/default/deployments/web/status
/apis/apps/v1/namespaces/default/deployments/web/scale
/apis/rbac.authorization.k8s.io/v1/clusterroles/cluster-admin
```

Discovery：

- `/api`：核心版本。
- `/apis`：API 组。
- `/api/v1`、`/apis/{group}/{version}`：资源列表。
- `/version`、`/healthz`、`/livez`、`/readyz`。
- `/openapi/v2`：占位。
- `/metrics`：Prometheus 文本。

## 35.7 CRD 动态注册

API Server 每个请求前调用 `sync_crds()`：

1. 列出所有 CRD。
2. `registry.from_crd(crd)` 生成 ResourceDefinition。
3. 注册到 registry。
4. 删除不再存在的 CRD 定义（只删除非内置组）。

这样：

```yaml
kind: CustomResourceDefinition
metadata:
  name: websites.example.com
```

注册后立即可访问：

```text
/apis/example.com/v1/namespaces/default/websites
```

删除 CRD 时，`CRDController` 清理所有 CR，移除 finalizer，最后删除 CRD。

## 35.8 认证与 RBAC

`APIHandler._dispatch`：

1. `authenticator.authenticate(Authorization)`。
2. 解析 route。
3. discovery/health 直接返回（部分公开）。
4. `_authorize(route, user, query)`。
5. 资源处理。

RBAC 属性：

```python
RequestAttributes(
    verb="get", group="apps", resource="deployments",
    namespace="default", name="web", subresource="status"
)
```

`RBACAuthorizer` 遍历 ClusterRoleBinding/RoleBinding，匹配：

- subject（User/Group/ServiceAccount）。
- rule verbs/apiGroups/resources/resourceNames。
- namespace。

默认拒绝。

## 35.9 子资源

MiniK8s 支持：

- `status`：GET/PUT/PATCH，只改 status。
- `scale`：GET/PUT/PATCH，映射到 `spec.replicas`。
- `log`：GET，文本日志。
- `exec`：POST，模拟命令。
- `binding`：POST，设置 Pod nodeName。
- `eviction`：POST，检查 PDB 后删除 Pod。
- `token`：POST，签发 SA token。
- `proxy`：模拟。
- `finalize`：Namespace 清理。
- access review：SelfSubjectAccessReview 等。

真实 Kubernetes 还有：

- `attach`、`portforward`、`ephemeralcontainers`。
- `resize`、`scale` 的 autoscaling/v1 转换。
- `status` 的字段过滤。

## 35.10 准入链

`APIHandler._create/_update/_patch` 调用：

```python
request = AdmissionRequest(operation, gvr, kind, obj, old_obj, namespace, name, user)
admitted = admission.admit(request)
store.create/update(admitted)
```

`AdmissionChain.admit`：

1. namespace 存在性。
2. 默认值（Pod/Deployment/Service/...）。
3. LimitRange。
4. 校验（必填、selector、资源、Service 端口、CronJob schedule）。
5. Pod Security。
6. CRD schema。
7. ResourceQuota。
8. 自定义 validator。

顺序与真实 API Server 类似，但真实实现还有 mutating webhook、validating webhook、审计。

### 35.10.1 默认值与校验的几条具体规则

这些规则容易在阅读/替换时漏掉，但都有测试覆盖：

- **limits-only 会补 request**：容器只写 limits 时，request 被默认成同一个值；如果 CPU/内存都这样，Pod 可能被判为 Guaranteed。不要以为“不写 request 就是 0”。
- **PriorityClass 继承**：Pod 引用 PriorityClass 时，`priority` 与 `preemptionPolicy` 从类继承；直接写 `priority` 与引用类冲突时要校验。
- **扩展资源**：必须是整数、request=limit（对称），且节点未声明时视为 0；调度器拒绝而不是忽略。
- **LimitRange**：除了注入默认 request/limit，还校验 `min`/`max`/`maxLimitRequestRatio`；更新时按新值重新校验。
- **PSA restricted**：检查非 root、allowPrivilegeEscalation=false、drop ALL、seccompProfile、卷类型、宿主命名空间等；不是只看一个字段。
- **Quota**：按“旧对象贡献 → 新对象贡献”做差量，避免更新重复计数；init/sidecar 的资源账本单独处理。

## 35.11 错误模型

`ApiError`：

```python
ApiError(status=404, reason="NotFound", message='Pod "web" not found')
```

API Server 返回 Kubernetes Status：

```json
{
  "kind": "Status",
  "apiVersion": "v1",
  "status": "Failure",
  "message": "...",
  "reason": "NotFound",
  "code": 404,
  "details": {...}
}
```

客户端/CLI 解析 message 显示。

## 35.12 一个完整例子

```bash
curl -X POST -H "Authorization: Bearer mini-admin-token" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8080/api/v1/namespaces/default/configmaps \
  -d '{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"demo"},"data":{"k":"v"}}'
```

流程：

1. 认证：token → `system:mini-admin`（system:masters）。
2. 授权：RBAC 放行。
3. 路由：核心组 v1，configmaps，default namespace。
4. 准入：namespace 存在、名称合法、默认 metadata。
5. store.create：分配 uid/rv，emit ADDED。
6. 返回 201 + 对象。

## 35.13 MiniK8s 的简化

- 没有 etcd/raft/持久化。
- 没有 watch cache（只有事件历史）。
- 没有 protobuf。
- 没有完整 OpenAPI。
- 没有完整 SSA：列表按整体拥有、不跟踪 schema 默认值。
- 没有审计。
- 没有 APF。
- 没有 webhook 执行。
- 没有多版本转换（CRD 只服务声明版本）。

第 40 章给出替换路线。

## 35.14 SSA 的实现：字段所有权如何落地

`mini_k8s/apimachinery/apply.py` 把 SSA 压缩成四个步骤。它不是生产实现，但覆盖了最容易出错的契约。

### 35.14.1 扁平化与字段路径

```python
def _flatten(value, prefix=()):
    if isinstance(value, dict):
        if not value:
            return {}
        result = {}
        for key, child in value.items():
            result.update(_flatten(child, prefix + (str(key),)))
        return result
    return {prefix: value}
```

一个 Deployment 的 apply 文档会变成：

```text
("spec", "replicas")                  -> 3
("spec", "template", "spec", "containers") -> [...]
("metadata", "labels", "app")         -> "web"
```

列表是一个叶子：`spec.template.spec.containers` 整体归一个 manager 所有。真实 SSA 用关联键（`k:{"name":"web"}`）让列表元素可以独立拥有；MiniK8s 不做，这是文档化的简化。

`metadata.managedFields` 用 `f:` 前缀的嵌套树表示：

```python
def _fields_tree(paths):
    tree = {}
    for path in sorted(paths):
        node = tree
        for part in path:
            node = node.setdefault(f"f:{part}", {})
    return tree
```

### 35.14.2 冲突检测

```python
for path, value in applied.items():
    for other, paths in owners.items():
        if other == manager:
            continue
        if not _related(paths, path):
            continue
        current = _get(live_obj, path)
        if current is _MISSING or current != value:
            conflicts.append((other, path))
```

三个细节：

- `_related` 处理“父路径 vs 子路径”的重叠（例如列表整体 vs 元素）。
- 相同值不冲突：两个 manager 可以声明同一个值。
- 冲突只阻止**修改**；如果另一 manager 拥有字段但当前值与你 apply 的值一致，允许通过，但所有权不转移。

### 35.14.3 合并与删除省略字段

```python
merged = _merge(live_obj, patch)
previous = owners.get(manager, set())
others = {p for m, ps in owners.items() if m != manager for p in ps}
for path in sorted(previous - set(applied), key=len, reverse=True):
    if not _related(others, path):
        _delete(merged, path)
```

这段是 SSA 与 merge patch 的分水岭：

- merge patch 只“加/改”你写的字段，永远不会删除你没写的字段。
- SSA 认为“这个 manager 的声明就是全集”；上次拥有、这次没写的字段会被删除，除非别的 manager 拥有它。

`null` 值也会删除字段，但不会被登记为“拥有一个空字段”。

### 35.14.4 所有权转移与 managedFields

```python
for path in held:
    overlapping = [other for other, paths in new_owners.items() if _related(paths, path)]
    if overlapping and not force:
        continue
    for other in overlapping:
        new_owners[other] = {p for p in new_owners[other] if not _related({p}, path)}
    new_owners.setdefault(manager, set()).add(path)
```

- 无冲突且无其他 owner：当前 manager 拥有。
- 其他 manager 拥有且值相同：不夺取所有权（避免“看一眼就接管”）。
- `force=true`：从其他 manager 删除重叠路径，再归当前 manager。
- 每个 manager 生成一个 `managedFields` 条目：manager、operation、apiVersion、time、fieldsType、fieldsV1。

### 35.14.5 API 层接入

`APIHandler._patch` 按 Content-Type 分流：

```python
if content_type == APPLY_CONTENT_TYPE:
    self._server_side_apply(route, definition, user, query, body)
    return
```

`_server_side_apply`：

1. 要求 `fieldManager` query 参数。
2. 读取 `force`。
3. 调用 `apply_object` 得到合并结果与 managedFields。
4. 对象不存在 → 走 CREATE 准入；存在 → 走 UPDATE 准入。
5. `dryRun=All` 不落库。
6. 写入 store。

普通 PUT/PATCH 会保留旧 managedFields，但不会更新所有权；这是教学简化。真实 apiserver 对 Update 操作也维护 manager（operation=Update），并参与冲突。

### 35.14.6 测试固定的行为

`tests/test_server_side_apply.py` 覆盖：

- 创建即记录 manager/fieldsV1。
- 同一 manager 更新不冲突。
- 不同 manager 改同一字段 → 409，消息包含 owner 与字段。
- 相同值不冲突、不转移所有权。
- `force` 夺取所有权。
- 省略字段被删除。
- `null` 删除字段。
- 缺少 `fieldManager` → 400。
- 客户端伪造 managedFields 被忽略。
- `dryRun` 不持久化。
- 普通 PUT 保留 managedFields。
- 主资源 apply 不写 status。

这些测试是“实现与文档同步”的一部分：新增 SSA 行为时，先改测试，再改实现。

## 本章小结

- ResourceStore 用 resourceVersion、乐观并发、finalizer 和 watch 模拟 etcd + storage layer。
- API Server 负责路由、认证、授权、准入、子资源和 discovery。
- CRD 动态注册让自定义类型立即可用。
- 准入链保证对象合法；SSA 的字段所有权已实现一个教学子集，webhook/审计/APF 仍未实现。
- 理解这一层，才能理解控制器为什么能“只看对象”工作。

## 延伸阅读

- [第 4 章 API 对象模型](04-api-object-model.md)
- [第 18 章 控制器模式](18-controller-pattern.md)
- [第 40 章 与真实 Kubernetes 的差距](40-gap-and-replacement.md)
