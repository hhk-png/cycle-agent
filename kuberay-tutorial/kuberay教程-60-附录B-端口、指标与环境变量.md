# 附录 B. 端口、指标与环境变量

## B.1 端口

| 端口 | 名称 | 组件 | 协议/用途 |
|---|---|---|---|
| 6379 | `gcs-server` | head Ray | GCS/Raylet 注册 |
| 8265 | `dashboard` | head Ray | Dashboard/Job API |
| 10001 | `client` | head Ray | Ray client |
| 8080 | `metrics` | Ray 容器 | Prometheus |
| 8000 | `serve` | Ray Serve proxy | HTTP |
| 52365 | dashboard agent | 每个 Ray 节点 | 本地指标/日志/健康 |
| 8080 | operator metrics | kuberay-operator | Prometheus |
| 8082 | operator probes | kuberay-operator | 健康探针（`--health-probe-bind-address`，默认 `:8082`） |
| 8888 | APIServer | kuberay-apiserver | REST/代理 API（以 chart 配置为准） |
| 8080 | History Server API/UI | ray-history-server | 会话/事件/回放（可配置） |

MiniKubeRay APIServer 默认 8080（可配置）。

## B.2 Kubernetes Service 端口

head Service：

- `client` 10001
- `dashboard` 8265
- `gcs-server` 6379
- `metrics` 8080
- `serve` 8000

Serve Service：

- `serve` 8000

### B.2.1 连接方向速查

| 源 | 目标 | 端口 | 说明 |
|---|---|---|---|
| worker | head | 6379 | GCS 注册/心跳 |
| submitter | head | 8265 | Job API |
| 客户端 | head | 10001/8265 | Ray Client/Dashboard |
| Gateway | Serve proxy | 8000 | HTTP 推理 |
| operator/autoscaler | kube-apiserver | 443 | watch/patch |
| Prometheus | operator/Ray | 8080 | 指标 |
| Dashboard | dashboard agent | 52365 | 节点指标/日志 |
| Ray 节点之间 | 动态端口 | TCP/UDP | 对象传输/raylet RPC |

完整解释与 NetworkPolicy 注意事项见[第 40 章](kuberay教程-40-Kubernetes网络深潜-CNI、DNS、Service、NetworkPolicy与mTLS.md)。

## B.3 Operator 指标

| 指标 | 类型 | 说明 |
|---|---|---|
| `kuberay_cluster_info` | gauge | RayCluster 信息 |
| `kuberay_cluster_condition_provisioned` | gauge | Provisioned 条件 |
| `kuberay_cluster_provisioned_duration_seconds` | gauge | Provisioned 耗时 |
| `kuberay_job_info` | gauge | RayJob 信息 |
| `kuberay_job_deployment_status` | gauge | 部署状态 |
| `kuberay_job_execution_duration_seconds` | gauge | 执行时长 |
| `kuberay_service_info` | gauge | RayService 信息 |
| `kuberay_service_condition_ready` | gauge | Ready |
| `kuberay_service_condition_upgrade_in_progress` | gauge | 升级中 |
| `controller_runtime_reconcile_*` | counter/histogram | controller-runtime 调和 |

MiniKubeRay 额外提供 `kuberay_raycluster_ready`、`kuberay_rayjob_status`、`kuberay_rayservice_ready`、`kuberay_autoscaler_actions_total`、`kuberay_operator_reconcile_*` 作为教学简化。标签集与上游不同（例如 Mini 用 `raycluster`/`rayjob`），查询前先看 `/metrics`。

IPPR 的动作也计入 `kuberay_autoscaler_actions_total`：

```text
kuberay_autoscaler_actions_total{action="resize-up",...}
kuberay_autoscaler_actions_total{action="resize-rollback",...}
```

真实集群还要看 Pod 的 `PodResizeInProgress` condition、`ray.io/ippr-status` 注解和 `ray status` 里节点的 CPU/memory；四者对不上就是 resize 没有完整完成（第 57.5.3 节）。

## B.4 Ray 指标（示例）

| 指标 | 说明 |
|---|---|
| `ray_tasks` | task 状态 |
| `ray_actors` | actor 状态 |
| `ray_object_store_memory` | 对象存储使用（按 `Location` 区分） |
| `ray_resources{Name="object_store_memory"}` | 对象存储容量（不是 `_limit` 指标） |
| `ray_node_cpu_utilization` | CPU |
| `ray_node_gpus_utilization` | GPU |
| `ray_component_*` | 组件健康 |
| `serve_*` | Serve 指标 |

具体名称随 Ray 版本变化，查 Ray Dashboard/Metrics 文档。

## B.5 常用 PromQL

```promql
sum(rate(controller_runtime_reconcile_errors_total[5m]))
/
sum(rate(controller_runtime_reconcile_total[5m]))

# 注意：Provisioned 是粘性的，不能用它判断“当前不可用”；
# 它是一组 one-hot 序列，每个 condition 值一条
kuberay_cluster_condition_provisioned{condition="false"} == 1

kuberay_job_deployment_status{deployment_status="Failed"} == 1

kuberay_service_condition_ready == 0

# 仅 MiniKubeRay；上游 KubeRay 没有这个指标
sum by (namespace, raycluster) (rate(kuberay_autoscaler_actions_total[1h]))

# 没有 ray_object_store_memory_limit；容量用 ray_resources 或 ray status 对照
sum by (instance, Location) (ray_object_store_memory) / 1024 / 1024 / 1024

sum by (node) (ray_node_gpus_utilization)
```

## B.6 环境变量（Ray Pod）

| 变量 | 说明 |
|---|---|
| `RAY_CLUSTER_NAME` | 集群名 |
| `RAY_CLUSTER_NAMESPACE` | namespace |
| `RAY_ROLE` | head/worker（真实 KubeRay） |
| `RAY_NODE_TYPE_NAME` | worker group 名（真实 KubeRay） |
| `RAY_NODE_TYPE` | MiniKubeRay 教学扩展，不是上游变量 |
| `RAY_IP` / `FQ_RAY_IP` | worker 侧的 head 地址；head 不注入 |
| `RAY_PORT` | GCS 端口 |
| `RAY_ADDRESS` | GCS 地址（worker 为 head-svc:6379） |
| `RAY_USAGE_STATS_KUBERAY_IN_USE` | 统计 |
| `RAY_DASHBOARD_ENABLE_K8S_DISK_USAGE` | 磁盘指标 |
| `RAY_enable_autoscaler_v2` | autoscaler v2（注意小写；KubeRay v2 会自动注入） |
| `RAY_REDIS_ADDRESS` | GCS FT Redis |
| `REDIS_PASSWORD` / `REDIS_USERNAME` | Redis 凭据 |
| `RAY_external_storage_namespace` | Redis 命名空间 |
| `RAY_gcs_storage` | rocksdb |
| `RAY_gcs_storage_path` | rocksdb 路径 |
| `RAY_gcs_rpc_server_reconnect_timeout_s` | worker 重连 |
| `RAY_USE_TLS` | mTLS |
| `RAY_TLS_SERVER_CERT/KEY/CA_CERT` | 证书路径 |
| `RAY_AUTH_MODE` | token |
| `RAY_AUTH_TOKEN` | token |
| `RAY_ENABLE_K8S_TOKEN_AUTH` | K8s RBAC 委托 |
| `KUBERAY_GEN_RAY_START_CMD` | 生成的 ray start 命令 |
| `RAY_START_ULIMIT_OPEN_FILES` | ulimit |
| `RAY_DASHBOARD_ADDRESS` | submitter 用 |
| `RAY_JOB_SUBMISSION_ID` | submitter Pod/容器；entrypoint 内不可依赖（Ray 会清理内部 env），用 `ray.get_runtime_context().get_job_id()` |
| `RAY_CLOUD_INSTANCE_ID` | autoscaler v2 |
| `RAY_NODE_TYPE_NAME` | autoscaler v2 |

## B.7 Operator 环境变量

| 变量 | 说明 |
|---|---|
| `ENABLE_INIT_CONTAINER_INJECTION` | 注入等待 GCS 的 init container |
| `ENABLE_PROBES_INJECTION` | 注入探针 |
| `ENABLE_LOGIN_SHELL` | 旧版 login shell（1.4 起默认关闭） |
| `ENABLE_DETERMINISTIC_HEAD_POD_NAME` | 确定性 head 名 |
| `ENABLE_RANDOM_POD_DELETE` | autoscaling 时允许随机删除 |
| `ENABLE_GCS_FT_REDIS_CLEANUP` | Redis cleanup |
| `RAYCLUSTER_DEFAULT_REQUEUE_SECONDS_ENV` | 默认 resync |
| `CLUSTER_DOMAIN` | 集群域名 |
| `ENABLE_RAY_HEAD_CLUSTER_IP_SERVICE` | 非 headless head Service |
| `DELETE_RAYJOB_CR_AFTER_JOB_FINISHES` | 完成后删除 RayJob |
| `RAYJOB_DEPLOYMENT_STATUS_TRANSITION_GRACE_PERIOD_SECONDS` | 状态转换宽限 |
| `RAYJOB_STATUS_CHECK_TIMEOUT_SECONDS` | 状态检查超时 |
| `ENABLE_WEBHOOKS` | 启用可选 validating webhook（v1.7；NodeLabelDelivery 是 v1.8+） |
| `USE_INGRESS_ON_OPENSHIFT` | OpenShift |
| `MINIKUBERAY_TOKEN` | MiniKubeRay API token |
| `MINIKUBERAY_SERVER` | MiniKubeRay CLI server |

### B.7.1 Operator 启动参数（常用）

| 参数 | 作用 | 备注 |
|---|---|---|
| `--watch-namespace` | 只监听一个 namespace | 多租户/降低权限；空值表示全集群 |
| `--enable-leader-election` | 多副本选主 | 副本 > 1 时必须开 |
| `--feature-gates` | 开/关 alpha/beta 功能 | 逗号分隔 `Gate=true` |
| `--batch-scheduler` | 启用 Volcano/YuniKorn/scheduler-plugins/WAS | 与 Kueue 的 `managedBy` 是不同层 |
| `--metrics-addr` | metrics 监听地址 | 常见 `:8080` |
| `--health-probe-bind-address` | 健康探针地址 | 默认 `:8082` |

参数名/默认值随版本变化，部署前以 `kubectl exec deploy/kuberay-operator -- /manager --help`（或镜像 entrypoint）为准。Helm values 到参数的映射见[附录 G](kuberay教程-65-附录G-Helm-Values与安装矩阵.md)。

## B.8 Feature Gate

见第 4 章表。开启示例：

```bash
--feature-gates=RayCronJob=true,RayClusterNetworkPolicy=true
```

## B.9 注解

上游 `ray.io/*` 标签/注解统一见[附录 A.5](kuberay教程-59-附录A-API字段速查.md)。这里只列 MiniKubeRay 专属的故障注入注解：

| 注解 | 说明 |
|---|---|
| `minikuberay.io/crash-after-seconds` | 模拟崩溃 |
| `minikuberay.io/job-duration-seconds` | 作业时长 |
| `minikuberay.io/job-exit-code` | 作业退出码 |
| `minikuberay.io/serve-deploy-seconds` | Serve 部署延迟 |
| `minikuberay.io/pod-start-delay-seconds` | Pod 启动延迟 |
| `minikuberay.io/job-complete-after-seconds` | 模拟 Job 完成（GCS FT cleanup 等） |

## B.10 MiniKubeRay 的 trace 接口

| 接口 | 说明 |
|---|---|
| `GET /api/traces?limit=N` | 最近 trace 列表 + `stats`（spans/errors/p50/p95/p99） |
| `GET /api/traces/<trace_id>` | 单条 trace 的全部 spans |
| `python -m minikuberay traces --limit N --tree` | CLI 形式 |

trace 的保留上限由 `OperatorConfig.trace_max_spans` 控制（默认 5000），超过后按 FIFO 淘汰。trace 与 metrics 一样是进程内状态，重启后清空；需要长期保留时导出到 OTel Collector/后端。

## B.11 Ray 风格 State API 子集

| 接口 | 说明 |
|---|---|
| `GET /api/v0/healthz` | `{"result": true, "msg": ""}` |
| `GET /api/v0/nodes?cluster=&namespace=&filter=&limit=` | 运行时节点（`state=ALIVE/DEAD`） |
| `GET /api/v0/jobs?...` | Job Submission 记录 |
| `GET /api/v0/serve/applications?...` | Serve 应用与 deployment 状态 |
| `GET /api/v0/events?...` | Kubernetes Event（按 `involvedObject.name` 的集群名/前缀过滤） |
| `python -m minikuberay state <dataset>` | CLI 形式 |

响应信封与 Ray 一致：`data.result.result` 是数组，`data.result.total` 是过滤后的总数。真实 Ray 的 State API 在 Dashboard 8265，额外提供 tasks/actors/workers/objects/placement_groups 等数据集；MiniKubeRay 只实现它能诚实回答的四类。

← [返回目录](README.md)
