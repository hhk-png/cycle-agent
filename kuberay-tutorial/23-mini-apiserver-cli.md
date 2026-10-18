# 第 23 章 实现：APIServer、Dashboard、CLI 与指标

MiniKubeRay 的接口层让同一套对象可以被 HTTP、CLI、Dashboard 访问。本章拆 `minikuberay/apiserver/`、`minikuberay/cli/`、`minikuberay/metrics.py` 和 `minikuberay/demo.py`。

## 23.1 ApiServer 结构

`minikuberay/apiserver/server.py`：

- `ApiServer`：绑定 host/port、启动/停止、URL；
- `_Handler`：HTTP 方法分派；
- `_parse_resource_path`：Kubernetes 风格路径；
- `_watch`：chunked JSON 流；
- `_pod_log`：Pod/RayJob 日志；
- `_stop_rayjob`：停止作业；
- `_authorized`：Bearer token；
- `_read_body`/`_drain_unread_body`：安全读 body；
- Dashboard HTML 来自 `dashboard.py`。

## 23.2 路由

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康 |
| GET | `/readyz` | 就绪 |
| GET | `/version` | 版本 |
| GET | `/metrics` | Prometheus |
| GET | `/dashboard` | Dashboard |
| GET | `/api/v1/namespaces` | namespace 列表 |
| GET | `/api/dashboard/summary` | 汇总 |
| GET | `/api/v1/namespaces/{ns}/{plural}` | 核心资源列表 |
| POST | 同上 | 创建 |
| GET/PUT/PATCH/DELETE | `.../{name}` | 读/更新/patch/删除 |
| PUT | `.../{name}/status` | 更新 status |
| GET | `...?watch=true` | watch |
| GET | `.../pods/{name}/log` | Pod 日志 |
| GET | `.../rayjobs/{name}/logs` | RayJob 日志 |
| POST | `.../rayjobs/{name}/stop` | 停止作业 |
| GET | `.../rayclusters/{name}/nodes` | 模拟 Ray 节点 |
| GET | `.../rayclusters/{name}/workergroups` | worker group 汇总 |
| GET | `/api/history/sessions` | History Server 会话（`?q=` 搜索） |
| GET | `/api/history/sessions/{ns}/{cluster}` | 会话详情 |
| GET | `.../events` / `.../snapshot` / `.../replay` | 事件/快照/回放 |
| GET | `/api/traces` | trace 列表（`?limit=`） |
| GET | `/api/traces/{trace_id}` | 单条 trace 的 span 树 |
| GET | `/apis/ray.io/v1` | APIResourceList |
| GET | `/openapi/v3` | 空 OpenAPI（占位） |

路径同时支持：

- `/api/v1/namespaces/default/pods`
- `/apis/ray.io/v1/namespaces/default/rayclusters`
- 全 namespace 列表（例如 `/api/v1/pods`）

## 23.3 创建/默认/校验

- POST 时，Ray kind 会 `apply_defaults`；
- POST/PUT 会忽略/剥离 `status`，status 只能走 `/status` 子资源；
- PUT 校验 path/body 的 kind/name/namespace 一致；
- 不做完整校验（与 KubeRay 无 webhook 一致）；
- 控制器负责校验并写 status/事件；
- `PUT` 支持 status subresource；
- `PATCH` 支持 merge/strategic（递归合并，列表替换）；
- `DELETE` 支持 `?force=true`。

## 23.4 Watch

`?watch=true`：

- `ApiStore.watch` 先发送当前对象；
- 然后持续发送 `{"type":"ADDED|MODIFIED|DELETED","object":...}`；
- 使用 HTTP/1.1 chunked；
- 客户端断开时捕获 BrokenPipe；
- 支持 `resourceVersion` 参数（since）。

这是控制器 watch 的教学版；真实 Kubernetes 还有 bookmarks、resourceVersion 过期、410 Gone 等。

## 23.5 认证与只读

```bash
python -m minikuberay server --token secret
```

- 除 `/healthz`、`/readyz`、`/metrics`、`/version`、`/dashboard`（静态页面）和 `/api/traces`（调试用）外，要求 `Authorization: Bearer secret`；
- trace 端点刻意不鉴权，方便本地调试；生产部署应关闭 trace 或放到受保护的 sidecar/端口后面；
- `--read-only` 拒绝 POST/PUT/PATCH/DELETE（403）；
- 没有实现 RBAC/namespace 授权，生产需要补。

## 23.6 日志与停止

- Pod 日志：返回 phase/容器状态；
- RayJob 日志：从 Runtime 取 job logs；
- stop：调用 `runtime.stop_job`，RayJob 控制器随后看到 STOPPED。

## 23.7 Dashboard

`minikuberay/apiserver/dashboard.py` 是一个单页 HTML：

- 每 3 秒 fetch 四个 CR 列表 + `/api/history/sessions`（共五个请求）；
- 展示 RayClusters/RayJobs/RayServices/RayCronJobs 和 History Server 会话；
- History 请求失败时只把该表留空/显示错误，不影响四个 CR 表；
- 状态颜色；
- 无构建步骤，方便改。

启动：

```bash
python -m minikuberay server
# http://127.0.0.1:8080/dashboard
```

## 23.8 CLI

`minikuberay/cli/main.py`：

| 命令 | 作用 |
|---|---|
| `server` | API + operator |
| `operator` | 仅 operator（进程内） |
| `apply -f` | 创建/更新 YAML |
| `get` | 列表/单对象（table/json/yaml）；`get nodes/workergroups -c` |
| `describe` | spec/status/events |
| `logs` | RayJob/Pod |
| `delete` | 删除 |
| `scale` | RayCluster worker group |
| `create cluster` | 创建 RayCluster |
| `job stop` / `job submit` | 停止/提交 RayJob |
| `history list/show/events/replay` | History Server |
| `traces [trace_id] [--tree]` | 查询 operator trace 树 |
| `validate` | 本地默认+校验 |
| `demo` | 端到端演示 |
| `version` | 版本 |

示例（除 `server`/`demo`/`validate` 外，命令默认连接 `http://127.0.0.1:8080`，所以先在另一个终端启动 `server`；也可用 `--server`/`MINIKUBERAY_SERVER` 覆盖）：

```bash
python -m minikuberay validate -f manifests/samples/ray-cluster.yaml
python -m minikuberay server --port 8080
python -m minikuberay apply -f manifests/samples/ray-cluster.yaml
python -m minikuberay get rayclusters
python -m minikuberay describe raycluster/raycluster-sample
python -m minikuberay scale raycluster/raycluster-sample workers=4
python -m minikuberay logs rayjob/hello-rayjob --tail 100
python -m minikuberay job stop hello-rayjob
python -m minikuberay create cluster demo --worker-replicas 2
python -m minikuberay job submit hello --cluster demo --entrypoint "python -c 'print(1)'"
python -m minikuberay get nodes -c demo
python -m minikuberay get workergroups -c demo
python -m minikuberay history list
python -m minikuberay history replay default/demo
python -m minikuberay delete raycluster/raycluster-sample --force
```

### 23.8.1 apply 语义

- 读取多文档 YAML；
- Ray kind 先默认；
- 存在则 PUT（带 resourceVersion），不存在则 POST；
- 输出 `created`/`configured`。

### 23.8.2 get 语义

- 支持别名：`rc`/`raycluster`、`rj`/`rayjob`、`rs`/`rayservice`、`cron`、`po`/`pod`、`svc`；
- 单对象 `-o json/yaml` 输出对象本身；
- 列表输出 table 或 List；
- `-A` 全 namespace。

### 23.8.3 scale

- 读取 RayCluster；
- 修改指定 group 的 `replicas`；
- PUT 回去；
- 控制器随后创建/删除 Pod。

## 23.9 指标

`minikuberay/metrics.py`：

- `MetricsRegistry`：counter/gauge/histogram；
- `OperatorMetrics`：reconcile/ready/job/service/autoscaler；
- `render()`：Prometheus 文本。

示例：

```bash
curl http://127.0.0.1:8080/metrics | grep kuberay_
```

## 23.10 Demo

`minikuberay/demo.py`：

1. RayCluster（2 worker）；
2. RayJob（成功）；
3. autoscaler（0 -> 3 -> 0）；
4. RayService（Running + 升级）；
5. RayCronJob（创建子作业）；
6. 指标。

```bash
python -m minikuberay demo --fast
```

它用 `ManualClock`，所以几秒内完成数小时的调度/缩容语义。

## 23.11 测试

`tests/test_apiserver_cli.py`：

- health/version/dashboard；
- CRUD；
- status subresource；
- patch；
- label selector；
- watch；
- RayJob logs/stop；
- token；
- read-only；
- CLI apply/get/scale/delete；
- validate；
- demo/version。

## 23.12 小结

- ApiServer 提供 Kubernetes 风格 REST + watch + auth + read-only。
- Dashboard 是 REST 的另一个客户端。
- CLI 覆盖 apply/get/describe/logs/scale/delete/validate/demo。
- 指标用 Prometheus 文本暴露。
- Demo 是端到端验证入口。

下一章测试与验证。

→ [第 24 章 测试、验证与故障注入](24-testing-verification.md)
