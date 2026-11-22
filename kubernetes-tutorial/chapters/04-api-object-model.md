# 第 4 章 API 对象模型：Kubernetes 的“类型系统”

## 4.1 一切皆对象

Kubernetes 的 API 不是一组 RPC，而是一个**对象存储接口**。Deployment、Pod、Service、Node、Role 都是对象，遵循统一的结构：

```yaml
apiVersion: apps/v1          # 组 + 版本
kind: Deployment             # 类型
metadata:                    # 身份与关联
  name: web
  namespace: default
  labels:
    app: web
  annotations:
    example.com/owner: team-a
spec:                        # 期望状态（用户/控制器写）
  replicas: 3
status:                      # 实际状态（控制器/节点写）
  availableReplicas: 3
```

这四个顶层字段构成 Kubernetes 的“对象协议”。理解它们，比记住几十种资源更重要。

## 4.2 GVK、GVR 与 API 组

Kubernetes 用三元组定位类型：

- **Group**：API 组，例如 `apps`、`batch`、`networking.k8s.io`；核心组为空字符串。
- **Version**：版本，例如 `v1`、`v1beta1`。
- **Kind**：类型名，例如 `Deployment`。

`apiVersion` 是 `group/version` 的字符串形式；核心组只有 `v1`。

资源还有复数名（resource/plural），用于 URL：

| Kind | apiVersion | 复数 | 作用域 |
|---|---|---|---|
| Pod | v1 | pods | Namespaced |
| Deployment | apps/v1 | deployments | Namespaced |
| Node | v1 | nodes | Cluster |
| ClusterRole | rbac.authorization.k8s.io/v1 | clusterroles | Cluster |
| CustomResourceDefinition | apiextensions.k8s.io/v1 | customresourcedefinitions | Cluster |

**GVK**（Group/Version/Kind）描述“这是什么类型”；**GVR**（Group/Version/Resource）描述“存在哪里”。REST 路径使用 GVR：

```text
GET /api/v1/namespaces/default/pods/web-abc
GET /apis/apps/v1/namespaces/default/deployments/web
GET /apis/rbac.authorization.k8s.io/v1/clusterroles/cluster-admin
```

Discovery 接口让客户端在运行时发现类型：

```bash
python -m mini_k8s.cli api-resources
curl -s http://127.0.0.1:8080/api/v1 | python -m json.tool
curl -s http://127.0.0.1:8080/apis/apps/v1 | python -m json.tool
```

MiniK8s 的 `mini_k8s/api/registry.py` 维护 GVR → `ResourceDefinition` 的映射，包含是否命名空间、短名、是否支持 scale/status、动词集合。API Server 用它在运行时解析 URL，而不是硬编码每个 handler。

## 4.3 metadata：身份、关联与生命周期

`metadata` 是对象最容易被低估的部分。

### name、namespace、uid

- `name`：命名空间内唯一（集群级资源全局唯一），必须符合 DNS 规则，创建后不可变。
- `namespace`：逻辑隔离与权限边界；不是 Linux namespace。
- `uid`：服务器分配的唯一 ID。**名字可以复用，uid 不会**。ownerReference、事件、审计都依赖 uid。

删除 Pod 再创建同名 Pod，是两个不同对象；旧对象的 uid 不会复活。

### labels 与 annotations

- **labels**：用于选择和分组，要求短、可索引、值受限制。Service selector、Deployment selector、NetworkPolicy、PDB 都依赖 labels。
- **annotations**：任意字符串元数据，不参与选择；用于配置、工具标记、Ingress 参数、构建信息等。

```yaml
metadata:
  labels:
    app.kubernetes.io/name: web
    app.kubernetes.io/version: "1.27"
  annotations:
    deployment.kubernetes.io/revision: "3"
    nginx.ingress.kubernetes.io/proxy-body-size: "10m"
```

不要把大对象塞进 annotations：etcd 有单对象大小限制（默认约 1.5 MiB），API Server 也有请求体限制。

### ownerReferences

所有者引用表达“谁创建了我”：

```yaml
ownerReferences:
  - apiVersion: apps/v1
    kind: ReplicaSet
    name: web-7d9f8c6b5
    uid: 0f2c...
    controller: true
    blockOwnerDeletion: true
```

垃圾回收器（GC）据此级联删除。Deployment → ReplicaSet → Pod 是典型链。`controller: true` 表示“这个所有者是管理控制器”，一个对象最多一个 controller owner。

MiniK8s 的 `GarbageCollector` 解析 ownerReference 的 GVK，查对应 GVR；所有者不存在或 uid 不匹配时删除依赖对象。

### finalizers

finalizer 是“删除前的钩子”：

```yaml
metadata:
  finalizers:
    - example.com/cleanup
```

删除流程：

1. 客户端 DELETE。
2. 对象被设置 `deletionTimestamp`，但**不物理删除**。
3. 控制器完成清理，移除 finalizer。
4. finalizer 为空后，对象真正消失。

这保证了“先清理外部资源，再删对象”。PV/PVC 保护、Namespace 清理、CRD 清理都依赖它。滥用 finalizer 会导致对象永久 Terminating。

### 删除传播：Background、Foreground 与 Orphan

`DELETE` 不只有“删不删”一个问题，还有“依赖对象怎么办”。策略在 `DeleteOptions.propagationPolicy` 里，而不是对象的 `metadata`：

| 策略 | 行为 | 常见现象 |
|---|---|---|
| `Background`（默认） | 先删除所有者，GC 在后台删除依赖 | DELETE 立即返回，Pod 可能还会存活一小段时间 |
| `Foreground` | 先给依赖加 `foregroundDeletion` finalizer，全部删完才删所有者 | `kubectl delete` 会等待；依赖卡住时所有者一直 Terminating |
| `Orphan` | 删除所有者，但把依赖的 `ownerReferences` 移除 | ReplicaSet/Pod 被“放飞”，需要手工清理 |

```bash
kubectl delete deployment web --cascade=background   # 默认
kubectl delete deployment web --cascade=foreground   # 等依赖删完
kubectl delete deployment web --cascade=orphan       # 保留 ReplicaSet/Pod
```

finalizer 会延长 Foreground 路径：依赖对象上的业务 finalizer 不清除，所有者就永远等不到删除完成。排障时先看 `kubectl get <child> -o yaml` 的 `metadata.finalizers` 和 `deletionTimestamp`，再看所有者。MiniK8s 记录传播策略并实现基本的级联删除，但不实现完整的 `foregroundDeletion` 协调。

## 4.4 spec 与 status：期望与观测分离

Kubernetes 把对象分成两半：

- `spec`：期望状态，用户或高层控制器写。
- `status`：实际状态，底层控制器/节点写。

这种分离带来两个重要机制：

1. **status 子资源**：`PUT /.../status` 只改 status，不增加 `generation`，避免控制器写状态触发新的发布。
2. **权限隔离**：普通用户通常可以改 Deployment spec，但不能直接改 status；kubelet 只能改自己节点的 Pod status。

一个对象的典型生命周期：

```text
用户创建 spec
   ↓
控制器观察 spec，创建下级对象
   ↓
节点/控制器写 status
   ↓
上层控制器读取 status，更新自己的 status
   ↓
用户看到 Ready / Available
```

例如 Deployment 的 status 不是它自己“跑”出来的，而是汇总 ReplicaSet 的 status；ReplicaSet 的 status 又汇总 Pod 的 status。

## 4.5 resourceVersion、generation 与乐观并发

### resourceVersion

每次写对象，服务器分配一个全局递增的 `resourceVersion`（真实 etcd 中是 revision 字符串）。它用于：

- **watch 断点续传**：从某个版本之后接收事件。
- **乐观并发**：更新时带上版本，若已被别人修改则返回 409 Conflict。
- **缓存一致性**：控制器判断本地缓存是否落后。

更新冲突示例：

```text
客户端 A 读取 Deployment (rv=100)
客户端 B 读取同一对象 (rv=100)
B 修改 replicas=5，写入成功 (rv=101)
A 修改 image，带 rv=100 写入 → 409 Conflict
```

正确做法是重新读取、合并、重试，而不是盲目覆盖。`kubectl apply` 和控制器都会处理冲突。

### generation

`generation` 只在 `spec` 变化时递增，status 变化不递增。控制器用：

```yaml
status:
  observedGeneration: 7
```

表示“我已经处理到第 7 代 spec”。如果 `observedGeneration < metadata.generation`，说明控制器还没追上最新期望，不能据此判断发布完成。

### uid 与 resourceVersion 的区别

- uid：对象身份，永不复用。
- resourceVersion：对象内容版本，每次写都变。
- generation：spec 版本，只有 spec 变才变。

## 4.6 命名空间与作用域

命名空间资源（Pod、Service、Deployment、ConfigMap…）的 URL 包含 namespace：

```text
/api/v1/namespaces/team-a/pods
```

集群级资源（Node、PV、ClusterRole、CRD…）没有 namespace：

```text
/api/v1/nodes/mini-node-1
```

命名空间不是网络隔离，也不是内核隔离。它主要提供：

- 名字作用域。
- RBAC 边界（RoleBinding 只在一个 namespace 生效）。
- ResourceQuota / LimitRange 边界。
- 默认网络策略、PSA 标签等策略作用域。

真正的多租户隔离要靠节点池、网络策略、准入、运行时沙箱等组合，第 26 章展开。

## 4.7 API 版本与兼容性

Kubernetes 的版本策略：

- **alpha**（`v1alpha1`）：默认关闭，可能删除，不保证兼容。
- **beta**（`v1beta1`）：默认开启，接口可能变化，但通常会提供迁移路径。
- **stable**（`v1`）：向后兼容，弃用需经过多个版本。

同一个 Kind 可以有多个版本，API Server 做转换。存储版本（storage version）只有一个，通常是某个 stable 版本。CRD 也可以声明多个 served 版本。

### 4.7.1 转换、存储版本与 CRD conversion

内置类型的版本转换由 API Server 的 conversion 代码完成；CRD 则需要显式配置：

```yaml
spec:
  conversion:
    strategy: Webhook   # None 表示所有版本共享同一 schema，不做字段转换
    webhook:
      conversionReviewVersions: ["v1"]
      clientConfig:
        service:
          name: my-conversion-webhook
          namespace: my-system
```

要点：

- `served` 决定客户端能否用该版本读写；`storage: true` 只能有一个版本。
- 多版本 CRD 升级的正确顺序是：先加新版本 `served: true, storage: false` → 部署/验证 conversion webhook 的 round-trip → 再把 storage 切到新版本 → 迁移对象 → 最后把旧版本设为 `served: false`。不要先删旧版本。
- conversion webhook 必须是无状态、可横向扩展、能处理“旧版本对象”的；它挂掉时，相关版本的读写都会失败。
- 用 `kubectl get --raw /apis/<group>/<version>` 和 `kubectl get <cr> -o yaml` 对照转换结果；升级前用 `pluto`/`kubent` 扫描旧 API。

升级集群时最常见的坑之一，就是使用了已移除的 beta API。例如 `extensions/v1beta1` Ingress、`policy/v1beta1` PDB 都已被移除。生产升级前要扫描 manifest 和 Helm chart。

## 4.8 CRD：把类型系统开放给用户

CRD（CustomResourceDefinition）让用户注册新类型：

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: websites.example.com
spec:
  group: example.com
  scope: Namespaced
  names:
    plural: websites
    singular: website
    kind: Website
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required: [image]
              properties:
                image: {type: string}
                replicas: {type: integer}
```

注册后：

```bash
python -m mini_k8s.cli apply -f examples/14-crd-operator.yaml
python -m mini_k8s.cli get websites
```

CRD 只是“类型 + 存储”，不会自动产生行为。行为由 Operator/控制器实现，第 19 章展开。

## 4.9 Server-Side Apply 与 managedFields

传统 `kubectl apply` 在客户端做三方合并（last-applied-configuration）。Server-Side Apply（SSA）把字段所有权交给服务器：

```yaml
metadata:
  managedFields:
    - manager: kubectl
      operation: Apply
      apiVersion: apps/v1
      fieldsType: FieldsV1
      fieldsV1:
        f:spec:
          f:replicas: {}
```

好处：

- 多个管理者可以各自拥有字段，冲突时明确报错。
- 不再依赖客户端保存 last-applied。
- 更适合 GitOps 和 Operator。

代价：

- 字段所有权模型复杂，冲突排查需要 `kubectl apply --server-side --field-manager=...`。
- 并非所有字段都适合 SSA。

MiniK8s 从本版起实现了**简化但可观察的 SSA**：`apply_object` 维护每个 manager 的字段集合，冲突返回 409，`force=true` 转移所有权，省略字段会被删除，`metadata.managedFields` 由服务器生成并拒绝客户端伪造。它不实现关联列表键（列表按整体拥有）、schema 默认值 ownership 和 status 子资源的独立 manager；这些差异在第 35 章说明。

### 冲突到底长什么样

两个 manager 修改同一字段时，服务器返回 `409 Conflict`，而不是“后写覆盖”：

```text
Apply failed with 1 conflict: conflict with "team-a": .spec.replicas
```

处理方式：

```bash
# 1. 先看谁拥有字段
kubectl get deployment web -o yaml | yq '.metadata.managedFields'

# 2. 如果确认要接管，显式 force
kubectl apply --server-side --field-manager=team-b --force-conflicts -f deployment.yaml

# 3. 如果是误改，撤回自己的改动，保留原 owner
```

注意：

- 相同值不算冲突；两个 manager 可以把字段“收敛”到同一个值。
- `force` 只转移冲突字段的所有权，不是“关闭 SSA”。
- 字段所有权按 manager 记录，不按人；CI 和人都用 `kubectl` 时会共享同一个默认 manager，容易互相覆盖。生产应给不同流水线设置不同的 `--field-manager`。
- **SSA 删除字段靠“省略”**：从 apply 配置里去掉该字段，manager 就放弃所有权；如果没人再拥有，字段被删除。不要用显式 `null` 模拟删除——`null` 会尝试把字段设成空值，对某些字段非法，而且语义与 merge patch 不同。`kubectl patch --type=merge` 才用 `null` 删除。这正是 SSA 与 merge patch 最容易被忽略的区别。

## 4.10 对象校验发生在哪一层

一个对象从提交到落库，通常经过：

1. **解码**：JSON/YAML → 内部结构；未知字段处理取决于严格模式。
2. **默认值**：API Server 的 defaulting，例如 `restartPolicy: Always`、Service `type: ClusterIP`。
3. **准入（mutating）**：LimitRange 注入、sidecar 注入、标签补全。
4. **校验**：必填字段、枚举、范围、selector 匹配、资源格式。
5. **准入（validating）**：配额、Pod Security、策略引擎、webhook。
6. **持久化**。

MiniK8s 把这些集中在 `mini_k8s/admission/admission.py`：

```python
def _default_pod(self, obj):
    obj.setdefault("status", {"phase": "Pending"})
    spec = obj.setdefault("spec", {})
    spec.setdefault("restartPolicy", "Always")
    spec.setdefault("serviceAccountName", "default")
    ...
```

以及 `_validate_pod`、`_pod_security`、`_quota`。真实 API Server 把这些拆成独立插件和 webhook，但顺序与职责类似。

## 4.11 用 curl 观察对象

启动 MiniK8s 后：

```bash
TOKEN=mini-admin-token
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8080/api/v1/namespaces/default/pods | python -m json.tool
```

创建对象：

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8080/api/v1/namespaces/default/configmaps \
  -d '{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"demo"},"data":{"k":"v"}}'
```

观察：

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8080/api/v1/namespaces/default/configmaps/demo
```

你会看到服务器补充的 `uid`、`resourceVersion`、`creationTimestamp`、`generation`、空 labels/annotations/finalizers。这些不是客户端写的，是 API 契约的一部分。

## 4.12 常见设计错误

**把 status 当配置写入**：用户不应该依赖手写 status；控制器会覆盖。

**用 name 做长期关联**：名字可复用，关联应使用 uid。

**把大文件塞进 ConfigMap/Secret**：对象大小有限，且每次 watch 都会传输。

**忽略 resourceVersion**：并发更新会互相覆盖。

**在 finalizer 中做无超时的外部调用**：外部系统挂起会导致对象永久 Terminating。

**假设 API 版本永远存在**：升级前必须检查弃用 API。

## 4.13 API 请求语义：分页、字段选择与协议协商

对象模型之外，还有一组“请求层”语义。它们不改变对象结构，却直接影响客户端正确性和大规模性能。

### 分页

列表请求可能返回海量对象。Kubernetes 用 `limit` + `continue` 分页：

```bash
kubectl get pods --chunk-size=500
curl -s "https://.../api/v1/pods?limit=500"
# 响应 metadata.continue 非空时，下一次请求带上它
curl -s "https://.../api/v1/pods?limit=500&continue=<token>"
```

要点：

- `continue` token 是**不透明字符串**，不能解析、不能构造；它带有服务端状态，通常与第一页的 `resourceVersion` 绑定。
- **同一次分页序列是一致快照**：服务端保证 `continue` 返回的对象集合与“不带 limit 的一次 list”一致，即使两次请求之间对象发生变化。不要把“分页”与“多次独立 list”混为一谈；后者才可能看到不同版本。
- token 会过期（默认约 5 分钟），过期或版本被 compaction 后返回 `410 Gone`，客户端需要从第一页重新开始。
- watch 不分页；需要大规模 watch 时用 `resourceVersion` 或按 namespace/标签分片。
- `limit=0` 在部分客户端表示“不分页”，但服务端仍可能限制最大返回量。

| 请求 | 一致性 | 说明 |
|---|---|---|
| 无 `resourceVersion`，不带 `limit` | 最新（通常线性一致） | 可能走 etcd quorum read |
| `limit` + `continue` 序列 | 第一页时刻的快照 | token 绑定快照；跨页不会“看到一半新对象” |
| 两次独立 list | 不保证同一快照 | 中间可能发生写入 |
| `resourceVersion=0` | 任意/可能略旧 | 由 watch cache 提供，延迟最低 |

### 字段选择器

`fieldSelector` 只支持有限的字段（由资源定义），例如：

```bash
kubectl get pods --field-selector status.phase=Running
kubectl get pods --field-selector spec.nodeName=node-1
kubectl get events --field-selector involvedObject.name=web
```

它**不是通用查询语言**：不能比较任意字段，也不能替代 label selector。需要复杂查询时，把索引信息写进 labels，或使用外部数据库/可观测系统。

### Table 输出与协议协商

`kubectl get` 的表格来自服务端的 Table 响应，而不是客户端硬编码列：

```bash
curl -H "Accept: application/json;as=Table;v=v1;g=meta.k8s.io" \
  "https://.../api/v1/namespaces/default/pods"
```

好处：

- CRD 可以通过 `additionalPrinterColumns` 定义列。
- 服务端可以决定哪些列、什么顺序，客户端不需要理解每种资源。
- `kubectl get -o wide` 会请求更宽的列集。

### protobuf 与 JSON

API Server 同时支持 JSON 和 protobuf（`application/vnd.kubernetes.protobuf`）。控制器/客户端使用 protobuf 可以显著降低序列化开销和带宽，尤其是大规模 list/watch。

代价：

- 可读性差，调试通常仍用 JSON。
- **CRD 只支持 JSON**，没有“启用 CRD protobuf”的上游开关；需要 protobuf 只能走内置类型或聚合 API Server。
- 客户端库/语言支持不一致。

### watch 语义

- `watch=true` 从指定 `resourceVersion` 开始推送事件。
- `resourceVersion=0` 表示“从缓存中的任意较新版本开始”，不保证历史。
- `allowWatchBookmarks=true` 让服务端定期发送 BOOKMARK，推进客户端版本，减少重连时的全量 List。
- 版本被 compaction 后返回 `410 Gone`，客户端必须重新 List。
- watch 是**水平触发**的补充，不是控制器正确性的唯一来源；控制器仍应基于当前状态调谐。

MiniK8s 的 watch 实现了 `resourceVersion=0` 的初始快照（把现有对象作为 ADDED 发出）和 `allowWatchBookmarks=true` 的书签事件；书签只在连接建立时发一次，不是真实 apiserver 的周期书签。它用 1000 条事件的队列模拟慢消费者，溢出时发送 `ERROR`（410）并断开。

### 一致性与超时

List 的一致性由 `resourceVersion` 与 `resourceVersionMatch` 共同决定，不要笼统说“list 可能来自缓存”：

| 参数 | 语义 | 一致性/代价 |
|---|---|---|
| 不设置 `resourceVersion` | “最新”（Most Recent） | 通常走 etcd quorum read，可视为线性一致；开启 `ConsistentListFromCache`（1.31 beta，默认开启）后可由缓存提供一致读 |
| `resourceVersion=0` | “任意”（Any） | 通常由 watch cache 提供，可能略旧，延迟最低 |
| 已知 RV + `resourceVersionMatch=Exact` | 该版本的精确快照 | 版本已被 compaction 时返回 410 |
| 已知 RV + `resourceVersionMatch=NotOlderThan` | 不早于该版本的快照 | 允许更新，适合“至少看到某状态” |

- 所有请求都应有超时/取消；控制器和 CLI 不应无限等待。
- 大量小请求会被 APF 限流；批量场景应合并/分页，而不是并发打满。

MiniK8s 实现了简化分页、label/field selector、JSON watch、连接建立时的一次 bookmark 和基本的 410（too old resourceVersion）语义；但不支持 protobuf、Table 协商、周期性 bookmark 和 `resourceVersionMatch`。第 35 章会对照这些差距。

## 4.14 聚合 API 与 APIService

CRD 是“把对象存进 etcd”，聚合层（aggregation layer）是“把请求代理给另一个 API Server”。两者都能扩展 API，但边界不同。

### 工作方式

```text
kubectl → kube-apiserver
            ├── /api, /apis（内置/CRD）
            └── /apis/metrics.k8s.io → APIService → metrics-server
```

`APIService` 对象声明一个 API 组由谁提供：

```yaml
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  name: v1beta1.metrics.k8s.io
spec:
  service:
    namespace: kube-system
    name: metrics-server
    port: 443
  group: metrics.k8s.io
  version: v1beta1
  insecureSkipTLSVerify: false
  caBundle: <base64>
  groupPriorityMinimum: 100
  versionPriority: 100
```

kube-apiserver 充当反向代理，使用 **front-proxy** 客户端证书与后端 API Server 做 mTLS；后端返回的对象格式仍是 Kubernetes API 风格。

### 典型用途

- `metrics.k8s.io`：metrics-server，供 `kubectl top`/HPA。
- `custom.metrics.k8s.io`：Prometheus Adapter 等。
- `external.metrics.k8s.io`：云队列/外部指标。
- 厂商扩展 API（例如某些云厂商的 CRD 之外的 API）。

### 与 CRD 的对比

| 维度 | CRD | 聚合 API |
|---|---|---|
| 存储 | kube-apiserver 写入 etcd | 后端自行存储 |
| 实现 | 只需 schema + 控制器 | 需要实现完整 API Server |
| 升级 | 改 CRD/控制器 | 独立部署/升级后端 |
| 故障域 | 与 API Server 同 | 后端不可用会影响该组 API |
| 适用 | 大多数自定义资源 | 指标、厂商 API、特殊存储/协议 |

### 故障模式

- APIService 后端不可用：`kubectl api-resources` 可能报错/超时，HPA 指标缺失，部分控制器失败。
- `caBundle` 不匹配：TLS 校验失败，整个 API 组不可用。
- 聚合 API 的 discovery 失败会拖慢客户端启动；不是“只影响一个资源”那么简单。
- 后端 API Server 必须实现正确的 discovery/CRUD/watch 语义，否则客户端行为异常。

排查：

```bash
kubectl get apiservices
kubectl get --raw /apis/metrics.k8s.io/v1beta1
kubectl -n kube-system logs deploy/metrics-server
```

MiniK8s 不实现聚合层；它的 metrics API 由进程内 `MetricsController` 直接提供。真实集群中，metrics-server 的可用性直接影响 HPA 和 `kubectl top`。

## 本章小结

- 所有 Kubernetes 资源遵循 GVK + metadata + spec/status 的统一模型。
- uid 标识身份，resourceVersion 标识版本，generation 标识 spec 代次。
- ownerReference 与 finalizer 是级联删除和清理钩子的基础。
- CRD 把类型系统开放给用户，但行为需要控制器实现；聚合 API 是另一种扩展路径，适合指标/厂商 API。
- 分页、字段选择、Table、protobuf 与 watch bookmark 是请求层语义，影响客户端正确性和规模性能。
- 理解对象协议，才能理解控制器、watch、准入和 GitOps。

## 延伸阅读

- [第 5 章 声明式工作流](05-declarative-workflow.md)
- [第 18 章 控制器模式](18-controller-pattern.md)
- [第 35 章 API Server 与存储实现](35-apiserver-store.md)
- [附录 A API 资源速查](../appendices/A-api-resources.md)
- [附录 P 字段语义、默认值与 YAML 陷阱](../appendices/P-defaults-and-yaml-traps.md)：本章提到的默认值/可变性细节。
