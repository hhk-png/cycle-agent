# 第 5 章 声明式工作流与 kubectl

## 5.1 kubectl 不是“客户端 SDK”

`kubectl` 是一个**API 客户端 + 资源编排器**。它做的事情远不止发 HTTP：

1. 读取 kubeconfig，选择 context、集群、用户。
2. Discovery，确定资源类型、是否命名空间、支持的动词。
3. 读取文件/目录/标准输入，解析多文档 YAML。
4. 对每个对象构造请求（create、replace、patch、apply）。
5. 处理服务端默认值、冲突、重试。
6. 格式化输出（table/json/yaml/name/custom-columns）。
7. 对 rollout、logs、exec、port-forward 等做长连接和流式处理。

MiniK8s 的 `mini_k8s/cli/main.py` 是一个缩小版：支持 apply/get/describe/delete/logs/exec/scale/rollout/set/run/expose/cordon/drain/taint/auth/api-resources/explain/top/wait。

## 5.2 四种写入方式

### create

```bash
kubectl create -f deployment.yaml
```

对象不存在则创建，存在则报 `AlreadyExists`。适合“一次性创建”。

### replace

```bash
kubectl replace -f deployment.yaml
```

全量替换。`kubectl replace` 默认会先 GET 现有对象，把当前 `resourceVersion` 注入请求，因此文件里不写 RV 仍然是“有条件更新”，并发修改会返回 409。要真正无条件覆盖，必须显式写 `metadata.resourceVersion: "0"`；`--force` 是 delete + create，不是 replace。适合控制器或需要精确控制全量字段的场景，不建议用于日常发布。

### patch

```bash
kubectl patch deployment web -p '{"spec":{"replicas":5}}'
```

只发送变化部分。常见类型：

- `application/merge-patch+json`：JSON Merge Patch，null 删除字段。
- `application/json-patch+json`：JSON Patch，操作列表（add/remove/replace/test）。
- `application/strategic-merge-patch+json`：Kubernetes 扩展的合并策略，能按 key 合并 list（例如 containers 按 name）。
- `application/apply-patch+yaml`：Server-Side Apply。

### apply

```bash
kubectl apply -f deployment.yaml
```

声明式创建或更新。传统客户端 apply 的逻辑：

```text
读取 last-applied-configuration annotation
读取当前对象
计算三方合并：
  用户新配置 vs 上次应用 vs 当前集群
只修改用户拥有的字段
PATCH 回服务器
```

Server-Side Apply 把合并移到服务器，用 `managedFields` 跟踪字段所有权：

```bash
kubectl apply --server-side --field-manager=deploy-bot -f deployment.yaml
```

MiniK8s 有两条路径：默认的 `ApiClient.apply` 是客户端简化版（GET 现有对象 → 深合并 → PUT），不维护 last-applied、也不实现字段所有权；显式使用 `--server-side`（或 `apply(..., server_side=True)`）时，服务端实现 `managedFields`、字段级冲突 409 和 `force` 接管。默认路径足以支撑教程示例，但不适合多管理者并发；多管理者场景请用 Server-Side Apply。

## 5.3 YAML 多文档与 List

一个文件可以包含多个对象：

```yaml
apiVersion: v1
kind: ConfigMap
metadata: {name: cfg}
data: {k: v}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
...
```

也可以使用 List：

```yaml
apiVersion: v1
kind: List
items:
  - apiVersion: v1
    kind: ConfigMap
    metadata: {name: cfg-a}
    data: {k: v}
  - apiVersion: v1
    kind: ConfigMap
    metadata: {name: cfg-b}
    data: {k: v2}
```

`kubectl apply -f dir/` 会递归读取 `.yaml/.yml/.json`。MiniK8s CLI 支持目录和多文档。

## 5.4 标签选择器：声明式的“胶水”

Deployment 管理哪些 Pod？Service 转发到哪些 Pod？NetworkPolicy 保护哪些 Pod？答案都是 labels + selector。

```yaml
# Deployment
selector:
  matchLabels: {app: web, tier: frontend}

# Service
selector: {app: web}

# NetworkPolicy
podSelector:
  matchLabels: {app: web}
```

选择器一旦创建通常不可变（Deployment 的 selector 在 `apps/v1` 中不可变）。原因：改变 selector 会让控制器“认领”另一批 Pod，造成不可预测的级联删除。

常见错误：

- Deployment selector 与 Pod 模板 labels 不匹配 → 准入拒绝或控制器无法管理。
- 两个 Deployment 使用相同 selector → 互相认领 Pod，行为混乱。
- Service selector 太宽 → 把无关 Pod 加入后端。
- 使用 `app=web` 这种过于通用的标签 → 多团队冲突。推荐 `app.kubernetes.io/name`、`app.kubernetes.io/instance` 等约定。

## 5.5 命名空间与 context

```bash
kubectl config get-contexts
kubectl config use-context prod
kubectl -n team-a get pods
kubectl --all-namespaces get pods
```

`kubectl` 的默认 namespace 来自 context。生产环境建议：

- 每个环境一个 context，名字清晰（`prod-cn-north`）。
- 不要在 context 里保存长期高权限 token；使用 OIDC/SSO。
- CI 使用独立 ServiceAccount，最小权限。
- 禁止在脚本里依赖“当前 context”，显式传 `--context`。

MiniK8s CLI 用 `--server`/`--token`/`-n`，没有 kubeconfig；这是教学简化。

## 5.6 试运行与 diff

```bash
kubectl apply --dry-run=client -f app.yaml    # 客户端校验
kubectl apply --dry-run=server -f app.yaml    # 服务端准入与默认值
kubectl diff -f app.yaml                      # 显示将发生的变更
```

`--dry-run=server` 很有价值：它会经过准入、默认值、配额检查，但不落库。MiniK8s 支持 `?dryRun=All`。

在 CI 中，推荐：

1. `kubectl apply --dry-run=server` 验证 schema 和策略。
2. `kubectl diff` 生成变更预览。
3. 策略引擎（OPA/Kyverno）做组织规则检查。
4. 人工审批后再 apply。

## 5.7 Kustomize 与 Helm

### Kustomize

Kustomize 用 overlay 组合 YAML，不引入模板语言：

```text
base/
  deployment.yaml
  service.yaml
overlays/
  dev/
    kustomization.yaml
    patch-replicas.yaml
  prod/
    kustomization.yaml
    patch-resources.yaml
```

```bash
kubectl apply -k overlays/prod
```

适合“同一套基础资源，多环境小差异”。

### Helm

Helm 是包管理器 + 模板引擎：

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm install my-redis bitnami/redis -f values.yaml
helm upgrade --install ...
helm rollback ...
```

适合分发第三方应用、管理复杂参数。代价是模板调试困难、values 膨胀、渲染结果与集群状态可能漂移。

### 选择建议

- 自己团队的应用、环境差异小：Kustomize。
- 分发给别人、需要版本化打包：Helm。
- 多集群、GitOps：Argo CD / Flux，底层仍可用 Kustomize/Helm。

## 5.8 GitOps：把集群状态放进 Git

GitOps 的核心不是“用 Git”，而是：

1. **单一事实源**：Git 中的声明是期望状态。
2. **持续调谐**：控制器比较 Git 与集群，自动同步。
3. **可审计**：每次变更都有 commit、review、回滚。
4. **漂移检测**：手动修改会被发现并纠正（或告警）。

典型工具：

- **Argo CD**：Application CRD、UI、同步策略、多集群。
- **Flux**：GitRepository/Kustomization/HelmRelease CRD，偏控制器组合。

GitOps 的难点：

- Secret 管理（SOPS、Sealed Secrets、External Secrets）。
- 多环境晋升（dev → staging → prod）与审批。
- 渲染顺序（CRD 先于 CR）。
- 漂移策略：自动纠正还是只告警。
- 大规模仓库的渲染性能。

## 5.9 发布策略

Kubernetes 原生提供滚动更新，但生产发布通常还需要：

- **RollingUpdate**：逐步替换，零中断（配合 readinessProbe）。
- **Recreate**：先停后起，适合不能并存的单实例。
- **Blue/Green**：两套环境切换 Service selector。
- **Canary**：按流量比例导入新版本（Ingress/Service Mesh/Argo Rollouts）。
- **Feature Flag**：代码级开关，与部署解耦。

滚动更新不是“自动安全发布”。没有 readinessProbe、没有 PDB、没有回滚预案，滚动更新也可能全挂。

## 5.10 回滚

```bash
kubectl rollout history deployment/web
kubectl rollout undo deployment/web
kubectl rollout undo deployment/web --to-revision=2
kubectl rollout status deployment/web
kubectl rollout restart deployment/web
```

Deployment 保留旧 ReplicaSet（受 `revisionHistoryLimit` 限制）。回滚本质是把旧 RS 的 Pod 模板写回 Deployment，触发一次新的滚动更新。

MiniK8s CLI 支持 `rollout status/history/undo/restart`，实现见 `mini_k8s/cli/main.py` 的 `cmd_rollout`。

## 5.11 服务端字段管理与冲突

使用 SSA 时，冲突示例：

```text
字段 spec.replicas 同时被 deploy-bot 和 hpa 管理
→ Apply 报 conflict
```

解决：

- 明确字段所有者，避免多个管理者写同一字段。
- 用 `--force-conflicts` 强制接管（谨慎）。
- HPA 管理的 replicas 不应再由 GitOps 直接管理；可用 `ignoreDifferences` 或分离对象。

这类冲突是“声明式系统的边界问题”，不是 bug。它迫使团队明确谁拥有哪个字段。

## 5.12 MiniK8s 实操

```bash
python -m mini_k8s.cli apply -f examples/01-hello.yaml
python -m mini_k8s.cli get all
python -m mini_k8s.cli describe deployment web
python -m mini_k8s.cli scale deployment web --replicas 4
python -m mini_k8s.cli set image deployment/web web=nginx:1.28
python -m mini_k8s.cli rollout status deployment web
python -m mini_k8s.cli rollout history deployment web
python -m mini_k8s.cli rollout undo deployment web --to-revision=1
```

`get all` 会列出 Pod、Service、Deployment、ReplicaSet、StatefulSet、DaemonSet、Job、CronJob。MiniK8s 的 `all` 是简化集合，真实集群的 `all` 也不包含所有资源（例如 Secret、Ingress 不在其中）。

## 5.13 生产清单

- 所有资源带 `app.kubernetes.io/name`、`instance`、`version`、`managed-by` 标签。
- 所有工作负载设置 requests/limits（至少 requests）。
- 所有长期服务配置 readinessProbe；关键服务配置 liveness/startupProbe。
- 使用 `--dry-run=server` 与策略检查。
- 禁止 `latest` 标签；使用不可变 tag 或 digest。
- 为 Deployment 设置 `revisionHistoryLimit`、`progressDeadlineSeconds`、PDB。
- Git 与集群的差异可观测；手动变更可追踪。
- 回滚演练过，而不是写在 runbook 里没跑过。

## 本章小结

- kubectl 是 API 客户端，apply 的核心是“计算差异并提交”。
- labels/selector 是对象之间的胶水，selector 设计要稳定、唯一。
- Kustomize/Helm 解决配置组合与分发，GitOps 解决持续同步与审计。
- 声明式不等于自动安全；探针、PDB、回滚和策略同样重要。

## 延伸阅读

- [第 4 章 API 对象模型](04-api-object-model.md)
- [第 8 章 Deployment 与 ReplicaSet](08-deployment-replicaset.md)
- [第 24 章 准入控制与供应链安全](24-admission-supply-chain.md)
- [附录 B kubectl 速查](../appendices/B-kubectl.md)
- [附录 J 自测题库与实验手册](../appendices/J-self-assessment-labs.md)：用可重复实验验证 apply/diff/回滚。
