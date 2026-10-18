# 第 8 章 状态、条件、事件与可观测性

Ray 集群出问题时，最常见的情况不是“完全没日志”，而是“有太多日志但不知道看哪”。本章把可观测性拆成五层：status、conditions、events、logs、metrics，再加上 Ray Dashboard 和 History Server。

## 8.1 五层模型

| 层 | 回答的问题 | 保留时间 | 适合谁 |
|---|---|---|---|
| status | 现在是什么状态？ | 与对象同生命周期 | 控制器、GitOps、自动化 |
| conditions | 为什么是这个状态？ | 与对象同生命周期 | 用户、告警 |
| events | 发生过什么？ | 通常 1 小时 | 排查、审计 |
| logs | 进程内部发生了什么？ | 取决于日志系统 | 开发者、SRE |
| metrics | 趋势、告警、容量 | 取决于 Prometheus | SRE、平台 |

不要用 events 做控制逻辑，也不要用 logs 做状态判断。

## 8.2 Status 与 Conditions

### 8.2.1 读 status 的顺序

```bash
kubectl get raycluster demo -o yaml
kubectl get raycluster demo -o jsonpath='{.status.conditions}'
kubectl get raycluster demo -o jsonpath='{.status.endpoints}'
```

先看：

1. `observedGeneration` 是否等于 `metadata.generation`；
2. `HeadPodReady` 是否 True；
3. `RayClusterProvisioned` 是否 True（注意它是粘性的，只说明“曾经就绪”）；
4. `desiredWorkerReplicas` 与 `readyWorkerReplicas` 的差；
5. `endpoints` 是否存在；
6. `status.state` 是否 `ready`（v1.7.1 已标记 deprecated，仅作兼容参考）。

如果 `observedGeneration` 落后，说明控制器还没处理最新 spec。

### 8.2.2 常见 condition 组合

| 组合 | 含义 | 下一步 |
|---|---|---|
| HeadPodReady=False, Provisioned=False | head 没起来 | 看 head Pod events/logs |
| HeadPodReady=True, ready < desired | worker 不足 | 看 worker 调度/镜像/资源 |
| Provisioned=True, state=suspended | 被 suspend | 检查 spec.suspend |
| ReplicaFailure=True | 创建/删除 Pod 失败 | 看 operator 日志和 RBAC |
| RayService Ready=False, UpgradeInProgress=True | 升级中 | 看 pending cluster |
| RayJob JobDeploymentStatus=ValidationFailed | spec 非法 | 看 message |

## 8.3 Events

KubeRay 的典型事件：

- `CreatedHeadPod` / `CreatedWorkerPod`
- `DeletedHeadPod` / `DeletedWorkerPod`
- `FailedToCreateHeadPod` / `FailedToCreateWorkerPod`
- `InvalidRayClusterSpec` / `InvalidRayJobSpec` / `InvalidRayServiceSpec`
- `CreatedRayJobSubmitter`
- `AutoscalerScaleUp` / `AutoscalerScaleDown`（MiniKubeRay）
- RayService 的 `UpdatedServeApplications`、`UpdatedHTTPRoute`

查看：

```bash
kubectl get events --sort-by=.lastTimestamp | tail -30
kubectl describe raycluster demo | sed -n '/Events/,$p'
```

事件过多时，按 `reason` 和 `involvedObject` 过滤。`FailedToCreateHeadPod` / `FailedToCreateWorkerPod` 常见的根因：

- ResourceQuota 不足；
- GPU 资源不足；
- ServiceAccount 不存在；
- PodSecurity 拒绝；
- 镜像拉取失败；
- 节点 affinity/taint 不匹配。

## 8.4 日志

### 8.4.1 Operator 日志

```bash
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=200
kubectl -n kuberay-system logs deploy/kuberay-operator -f
```

关注：

- `Reconcile` 错误；
- `Invalid...Spec`；
- `Failed to create/delete`；
- `RayClusterStatus` 不一致；
- 限速重试。

### 8.4.2 Ray 容器日志

```bash
# head Pod 名默认是 <cluster>-head-<随机后缀>，不要假设它叫 <cluster>-head
HEAD=$(kubectl get pod -l ray.io/cluster=demo,ray.io/node-type=head -o jsonpath='{.items[0].metadata.name}')
kubectl logs "$HEAD" -c ray

# worker：按 group 标签取
WORKER=$(kubectl get pod -l ray.io/cluster=demo,ray.io/node-type=worker -o jsonpath='{.items[0].metadata.name}')
kubectl logs "$WORKER" -c ray

# autoscaler sidecar 在 head Pod 里
kubectl logs "$HEAD" -c autoscaler

# init container（注入时）
kubectl logs "$WORKER" -c wait-gcs-ready
```

Ray 自身日志在容器内 `/tmp/ray/session_latest/logs/`：

```bash
kubectl exec demo-head -- ls /tmp/ray/session_latest/logs
kubectl exec demo-head -- tail -n 200 /tmp/ray/session_latest/logs/raylet.out
```

关键文件：

| 文件 | 内容 |
|---|---|
| `raylet.out` | Raylet 启动/心跳/调度 |
| `gcs_server.out` | GCS 元数据/注册 |
| `dashboard.log` | Dashboard/Job API |
| `serve/` | Serve controller/proxy |
| `python-core-driver-*.log` | driver 输出 |
| `raylet_monitor.log` | 节点监控 |

### 8.4.3 Job 日志

```bash
ray job logs <job-id> --address http://localhost:8265
# 或
kubectl exec demo-head -- ray job logs <job-id>
```

KubeRay RayJob 的状态也会带 `dashboardURL` 和 `jobId`。

## 8.5 指标

### 8.5.1 KubeRay Operator 指标

先确认指标真的导出了：

```bash
# Helm 安装时开启
helm upgrade kuberay-operator kuberay/kuberay-operator \
  --reuse-values --set metrics.enabled=true
# 验证
kubectl -n kuberay-system port-forward svc/kuberay-operator 8080:8080
curl -s http://127.0.0.1:8080/metrics | grep '^kuberay_'
```

如果 `metrics.enabled=true` 但 `/metrics` 为空，检查 operator 的 `--enable-metrics`/`--metrics-addr` 参数，以及 Helm `configuration.enabled` 的 ConfigMap 是否覆盖了命令行参数（不同 chart 版本行为不同）。

v1.4+ 提供 SLI 指标。常见族：

| 指标 | 类型 | 说明 |
|---|---|---|
| `kuberay_cluster_info` | gauge | RayCluster 信息 |
| `kuberay_cluster_condition_provisioned` | gauge | RayClusterProvisioned 条件 |
| `kuberay_cluster_provisioned_duration_seconds` | gauge | 首次 provisioned 耗时 |
| `kuberay_job_info` | gauge | RayJob 信息 |
| `kuberay_job_deployment_status` | gauge | JobDeploymentStatus one-hot |
| `kuberay_job_execution_duration_seconds` | gauge | 作业执行时长 |
| `kuberay_service_info` | gauge | RayService 信息 |
| `kuberay_service_condition_ready` | gauge | Ready 条件 |
| `kuberay_service_condition_upgrade_in_progress` | gauge | 升级中条件 |
| `controller_runtime_reconcile_total` | counter | controller-runtime 调和次数 |
| `controller_runtime_reconcile_errors_total` | counter | controller-runtime 调和错误 |
| `controller_runtime_reconcile_time_seconds` | histogram | controller-runtime 调和耗时 |

MiniKubeRay 还提供简化的 `kuberay_raycluster_ready`、`kuberay_rayjob_status`、`kuberay_rayservice_ready`、`kuberay_autoscaler_actions_total`，便于本地练习；生产告警应以 KubeRay v1.7 metrics reference 为准。

> 标签集不要混用：上游 `kuberay_cluster_info` / `kuberay_job_info` 使用 `name`、`owner_kind` 等标签，`kuberay_job_deployment_status` 使用 `deployment_status`；MiniKubeRay 为了教学简化使用 `raycluster`/`rayjob` 等标签。复制 PromQL 前先 `curl /metrics` 看实际标签。

### 8.5.2 Ray 指标

Ray 在 8080 暴露 Prometheus 指标，包括：

- `ray_tasks`、`ray_actors`；
- `ray_object_store_memory`；
- `ray_node_cpu_*`、`ray_node_gpus_*`；
- `ray_component_*`；
- Serve 的 `serve_*` 指标。

### 8.5.3 推荐 PromQL

```promql
# controller-runtime 错误率
sum(rate(controller_runtime_reconcile_errors_total[5m]))
/
sum(rate(controller_runtime_reconcile_total[5m]))

# RayService / RayJob 的当前状态
kuberay_service_condition_ready == 0
kuberay_job_deployment_status{deployment_status="Failed"} == 1

# 注意：kuberay_cluster_condition_provisioned 是粘性的，
# 只适合“首次 provision 是否完成”，不能用来判断“当前是否 Ready”。
# 当前可用性请用 HeadPodReady/Pod Ready 或自定义 ray_cluster_status 指标。

# 升级中
kuberay_service_condition_upgrade_in_progress == 1

# MiniKubeRay 扩展
sum by (raycluster, action) (rate(kuberay_autoscaler_actions_total[1h]))

# Ray 对象存储用量（按实例求和）
sum by (instance) (ray_object_store_memory)
```

对象存储的“上限”没有稳定的 `ray_object_store_memory_limit` 指标；用 `ray_resources{Name="object_store_memory"}`（或 Ray 配置/`ray status`）对照，不要在告警里引用不存在的指标。

### 8.5.4 告警建议

- `controller_runtime_reconcile_errors_total` 持续增长；
- RayCluster Ready=0 超过 10 分钟；
- RayJob FAILED 比例 > 5%；
- RayService Ready=0 超过 1 分钟；
- autoscaler 扩容失败/频繁抖动；
- head Pod 重启次数 > 0（GCS 丢失风险）；
- GPU 利用率长期 < 10%（成本）。

## 8.6 Ray Dashboard

Ray Dashboard（8265）提供：

- Nodes：节点、CPU/GPU、内存、对象存储；
- Jobs：提交、状态、日志；
- Serve：应用、deployment、replica、流量；
- Actors/Tasks：运行中/排队；
- Metrics：内置图表；
- Logs：按节点/文件查看；
- Cluster：autoscaler 状态、资源需求。

KubeRay Dashboard 是另一个组件（第 16 章），用于集中查看多个 RayCluster/RayJob/RayService，不是 Ray Dashboard 的替代。

## 8.7 History Server

Ray Dashboard 的数据通常来自运行中的集群；集群删除后，RayJob 的调试信息就没了。History Server（v1.6 alpha，v1.7 发布说明称 beta；feature gate 阶段以目标 tag 为准）解决这个问题：

- collector sidecar 采集 Ray 事件；
- 写入 S3/GCS/Azure/OSS/本地存储；
- History Server 读取事件，回放 Dashboard 快照；
- 支持按集群选择、task logs、Serve/placement group/Ray Data 端点。

启用：

```bash
helm upgrade kuberay-operator kuberay/kuberay-operator \
  -f feature-gates.yaml  # featureGates: [{name: RayClusterHistoryServer, enabled: true}]
```

RayCluster 加：

```yaml
spec:
  historyServerOptions:
    collectorOptions:
      image: quay.io/kuberay/collector:v1.7.1
```

collector 的存储配置（字段名以 v1.7.1 CRD/chart 为准；未声明字段会被裁剪）：

```yaml
spec:
  historyServerOptions:
    collectorOptions:
      image: quay.io/kuberay/collector:v1.7.1
      env:
        # 与 History Server Deployment 保持一致，否则会话找不到
        - name: STORAGE_BACKEND
          value: s3
        - name: S3_BUCKET
          value: my-ray-history
        - name: STORAGE_ROOT_DIR
          value: ray-history
```

凭据使用标准 AWS 变量 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`。v1.7.0 的 breaking change 已把旧的 `AWS_S3ID`/`AWS_S3SECRET` 换掉；collector 与 History Server 两侧要使用同一组变量。collector 侧通常用 `--runtime-class-name`/`--ray-root-dir` 等 args 选择后端和根目录。

History Server 本身是一个独立 Deployment/Service，读取同一个存储。上游 API 与 MiniKubeRay 不同：

```bash
# 上游 History Server（示例 Service 的端口是 30080，目标 8080）
kubectl -n kuberay-system port-forward svc/historyserver 8080:30080
curl http://127.0.0.1:8080/clusters
curl http://127.0.0.1:8080/enter_cluster/<namespace>/<resourceType>/<resourceName>/<session>
# 浏览器打开 UI，选择已删除的集群

# MiniKubeRay（本仓库）
kubectl -n default port-forward svc/ray-history-server 8080:8080
curl http://127.0.0.1:8080/api/history/sessions
curl http://127.0.0.1:8080/api/history/sessions/<namespace>/<cluster>/events
```

不要把 MiniKubeRay 的 `/api/history/sessions` 当成上游 API；两套路径是刻意分开的。

生产注意：

- 事件存储的权限/生命周期（S3 lifecycle、对象锁）；
- collector 的资源开销（每个节点一个 sidecar）；
- 敏感信息（日志、环境变量、prompt）不要进入共享存储；
- v1.7.1 默认关闭 live-cluster proxying，避免意外代理到运行中的集群；
- 保留策略：事件量随集群规模/时长增长，设置 TTL/分层存储；
- 多租户：按 namespace/集群前缀隔离 bucket/prefix。

### 8.7.1 MiniKubeRay 的 History Server 实验

MiniKubeRay 实现了简化版：

```bash
python -m minikuberay server --history-path ./history.json
python -m minikuberay apply -f manifests/samples/ray-cluster.yaml
python -m minikuberay get rayclusters
python -m minikuberay history list
python -m minikuberay history show default/raycluster-sample
python -m minikuberay history replay default/raycluster-sample
# 删除集群后，会话仍然存在
python -m minikuberay delete raycluster/raycluster-sample
python -m minikuberay history list
```

对比：真实 History Server 采集 Ray 事件并写对象存储；MiniKubeRay 采集 operator 事件 + 运行时快照，语义（事件归属、快照、删除后回放）一致，数据面是模拟的。

## 8.8 py-spy 与性能诊断

> operator 自身的指标（reconcile 错误率/p99、workqueue depth、leader 切换）与控制面 SLO 见[第 54 章](54-operator-reliability-control-plane-slo.md)。

Ray 镜像通常包含 `py-spy`。注意容器 PID 1 往往是 `/bin/bash` 包装器（KubeRay 生成的 `ray start ... --block` 命令由 shell 执行），直接 `--pid 1` 看到的不是 Ray。先找 Python/Raylet 进程：

```bash
# 在 Pod 内列出进程，找到 driver/worker/raylet 的 PID
kubectl exec -it <head-pod> -- bash -c 'ps -eo pid,ppid,comm,args | grep -E "python|raylet|gcs"'

# 再 attach；Raylet 是 C++ 进程，py-spy 只对 Python 进程有意义
kubectl exec -it <head-pod> -- py-spy top --pid <python-pid>
kubectl exec -it <worker-pod> -- py-spy dump --pid <worker-pid>
```

也可以按标签找 Pod：

```bash
HEAD=$(kubectl get pod -l ray.io/cluster=demo,ray.io/node-type=head -o jsonpath='{.items[0].metadata.name}')
kubectl exec -it "$HEAD" -- bash -c 'pgrep -af python'
```

用于定位：

- driver 卡在序列化；
- worker 在等锁；
- Serve replica 的 Python 热点；
- 对象传输/反序列化瓶颈。

生产建议把 py-spy 作为临时诊断工具，不要长期开启采样。

## 8.9 分布式追踪

Ray 本身不提供完整的 OpenTelemetry 追踪；常见做法：

- 在应用层用 OpenTelemetry SDK；
- 通过 Ray Serve 的 middleware 注入 trace context；
- 在 driver 入口创建 span；
- 把 trace ID 写入日志；
- 用 Jaeger/Tempo 关联。

KubeRay 的 operator 日志可以加 trace ID，但控制平面与数据平面的追踪通常是分开的。

## 8.10 MiniKubeRay 的可观测性

- `EventRecorder`：写 Kubernetes Event 对象；
- `OperatorMetrics`：reconcile/ready/job/service/autoscaler 指标；
- `/metrics`：Prometheus 文本；
- `/dashboard`：单页 Dashboard；
- `describe`：打印 status、conditions、events；
- `logs`：RayJob/Pod 日志。

运行：

```bash
python -m minikuberay server
curl http://127.0.0.1:8080/metrics
curl http://127.0.0.1:8080/api/dashboard/summary
```

## 8.11 从指标到 SLO

### 8.11.1 SLI 选择

| 对象 | SLI | 数据源 |
|---|---|---|
| RayCluster | 创建到 Ready 的 P95 | operator 指标/事件 |
| RayJob | 成功率、排队时间、执行时长 | `kuberay_job_*`/Ray |
| RayService | 可用性、P99 延迟、5xx | Serve/Gateway |
| 控制面 | reconcile 错误率/延迟 | controller-runtime |
| 成本 | GPU 小时、闲置率 | DCGM/计费 |

### 8.11.2 告警设计

- **症状告警**优先于原因告警：用户看到 5xx，比“GCS CPU 高”更直接；
- 每个告警都要有 runbook 链接；
- 避免“每个 Pod 重启都 P1”；
- 用 `for` 抑制抖动；
- 分级：P1 全站不可用/P2 单租户/P3 容量/P4 趋势。

PromQL 见 8.5.3；本节只给出告警阈值和分级。示例：

```promql
# 控制面错误率（同 8.5.3，这里加阈值/for）
(
  sum(rate(controller_runtime_reconcile_errors_total[5m]))
  /
  sum(rate(controller_runtime_reconcile_total[5m]))
) > 0.01

# RayService 不可用（持续 1 分钟）
kuberay_service_condition_ready == 0

# RayJob 失败（单次失败不告警，连续/比例告警）
kuberay_job_deployment_status{deployment_status="Failed"} == 1
```

### 8.11.3 基数与成本

- 不要把 task/actor ID 放进标签；
- 用 relabel/drop 删除高基数维度；
- 分开抓取 operator/Ray；
- 设置保留时间/降采样；
- 监控 Prometheus 自身。

### 8.11.4 可观测性闭环

```text
指标 -> 告警 -> runbook -> 诊断 -> 修复 -> 复盘 -> 新指标/新告警
```

没有闭环的仪表盘只是“好看的图”。

具体的告警规则/阈值/值班流程以[第 17 章](17-production-runbook.md)为准；本章讲“指标怎么来、怎么选”，第 17 章讲“怎么用它值班”。

## 8.12 小结

- status 是状态，conditions 是原因，events 是时间线，logs 是细节，metrics 是趋势。
- 先看 `observedGeneration` 和 conditions，再看 Pod/事件/日志。
- Operator 与 Ray 的指标要一起看：控制面错误和数据面资源问题经常互相伪装。
- History Server 解决“集群删了还想看 Dashboard”的问题。
- 告警要覆盖 Ready、错误率、作业失败率、扩缩容抖动和 GPU 利用率。

OpenTelemetry trace、日志管道、SLO/错误预算与成本归因见[第 48 章](48-observability-2.md)；MiniKubeRay 的 `/api/traces` 与 `minikuberay traces` 也在那里。Ray 运行时的 State API、`ray status/list/logs` 与“五层证据链”见[第 56 章](56-live-diagnostics-state-api.md)。

下一章进入自动扩缩容：Ray autoscaler 如何计算需求，KubeRay 如何把需求变成副本数。

→ [第 9 章 自动扩缩容](09-autoscaling.md)
