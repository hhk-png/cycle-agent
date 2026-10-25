# 第 56 章 Ray 活体诊断：State API、ray CLI 与证据链

上一章讲怎么安全地改；本章讲改坏了、或者没改却坏了的时候，怎么在**不重启、不猜**的前提下定位。

KubeRay 的 status 只回答“控制面认为集群是什么状态”；Ray 的 State API 回答“数据面现在有什么”。两者之间还隔着 Pod、raylet、GCS 和网络。一个成熟的 on-call 流程不是“多打几条命令”，而是按证据成本从低到高，逐步缩小范围。

## 56.1 诊断的第一原则：先证据，后动作

排障时最常见的二次伤害是“先重启一下试试”。重启会销毁现场：

- Pod UID 变了，无法再区分“原来就坏”和“重建后坏”；
- GCS 元数据可能丢失；
- 事件被新事件淹没；
- 正在跑的 task/actor 被杀死，问题无法复现。

因此顺序应该是：

```text
读状态 -> 读事件 -> 读数据面状态 -> 读日志 -> 做最小复现 -> 才考虑重启
```

本章把这条路径拆成五层证据：

| 层 | 回答 | 主要工具 | 保留时间 |
|---|---|---|---|
| L1 控制面 | KubeRay 认为发生了什么？ | `kubectl get/describe raycluster`、conditions | 与 CR 同生命周期 |
| L2 调度/容器 | Pod 有没有被调度、启动？ | `kubectl get/describe pod`、events | Pod 生命周期 + 事件 TTL |
| L3 Ray 节点 | raylet 有没有注册？资源多少？ | `ray status`、`ray list nodes` | 运行时 |
| L4 应用 | task/actor/job/Serve 在做什么？ | `ray list tasks/actors/jobs`、State API | 运行时/历史 |
| L5 进程 | 具体卡在哪一行？ | `ray logs`、`py-spy`、`ray debug`、trace | 日志保留策略 |

先看 L1/L2，再看 L3/L4，最后才看 L5。反过来做，通常会被海量日志淹没。

## 56.2 证据地图：症状对应哪一层

| 症状 | 先看 | 再看 | 常见根因 |
|---|---|---|---|
| RayCluster 不 Ready | `status.conditions`、`observedGeneration` | Pod events、head 日志 | 镜像、资源、RBAC、校验失败 |
| Pod Pending | `kubectl describe pod` | 节点容量/配额/亲和性 | 资源不足、taint、PVC、PDB |
| Pod Running 但 Ray 看不到节点 | `ray status`、`ray list nodes` | raylet 日志、`wait-gcs-ready` | GCS 地址、版本、网络、启动参数 |
| task 一直 PENDING | `ray status` 的 Demands | `ray list tasks --detail` | 资源形状不匹配、placement group、调度策略 |
| actor 反复重启 | `ray list actors` | actor 日志、OOM | 异常退出、内存、依赖 |
| RayJob 卡住 | `status.jobDeploymentStatus` | submitter Job/Pod、`ray job status` | 提交模式、entrypoint、dashboard 不可达 |
| RayService 503 | `status.conditions`、Serve status | deployment 状态、proxy 日志 | 升级未完成、副本不足、路由错误 |
| 延迟突然升高 | 指标、`ray status` | `ray memory`、对象溢出 | 对象存储、跨节点传输、GC |
| 集群整体不可用 | head Pod、GCS | Redis/PVC、`ray status` | head 故障、GCS FT 未启用/恢复失败 |

## 56.3 `ray status`：一眼看资源与需求

`ray status` 是 Ray 自带的集群状态命令，通常在 head Pod 内执行：

```bash
HEAD=$(kubectl get pod -l ray.io/cluster=<name>,ray.io/node-type=head \
  -o jsonpath='{.items[0].metadata.name}')
kubectl exec "$HEAD" -- ray status
```

示例输出（字段随 Ray 版本略有差异）：

```text
======== Autoscaler status: 2026-10-18 12:00:00 ========
Node status
-------------------------------------------------------
Active:
 1 node_0f3c... (head)
 2 node_7a91... (worker)
Pending:
 (no pending nodes)
Recent failures:
 (no failures)

Resources
-------------------------------------------------------
Usage:
 0.0/8.0 CPU
 0.0/2.0 GPU
 0B/10.00GiB memory
 0B/2.00GiB object_store_memory

Demands:
 (no resource demands)
```

读法：

1. **Active**：Ray 认为已经加入集群的节点。数量应等于 head + ready worker；
2. **Pending**：Ray autoscaler 正在等待的节点。KubeRay 下它对应“已 patch replicas 但 Pod 还没注册”；
3. **Recent failures**：最近失败的节点/启动尝试，常与云配额、镜像、启动脚本有关；
4. **Resources Usage**：Ray 视角的逻辑资源，不是 Pod requests 的简单复制；
5. **Demands**：当前排队等待的资源。**这是“为什么任务不跑”的第一现场**。

三个常见误读：

- `ray status` 只看到 2 CPU，但 Pod request 是 4：检查 `rayStartParams.num-cpus` 或顶层 `resources` 是否覆盖了自动检测；
- Active 节点数少于 Pod 数：Pod Running 但 raylet 没注册，去 L5 看日志；
- Demands 为空但任务没跑：任务可能已经调度到某个 worker，卡在应用代码/对象传输，而不是资源不足。

`ray status --verbose` 会给出更多节点细节；具体字段以当前 Ray 版本为准。

## 56.4 `ray list`、`ray get`：把运行时对象列出来

Ray 2.x 的 `ray list` 是 State API 的 CLI 前端：

```bash
ray list nodes
ray list actors
ray list tasks
ray list jobs
ray list placement-groups
```

常用选项（以 `ray list <dataset> --help` 为准）：

```bash
ray list actors --filter "state=ALIVE"
ray list tasks --filter "state=RUNNING" --detail
ray list nodes --format json
ray list jobs --limit 20
```

`ray get` 用来取单个对象的完整详情：

```bash
ray get nodes <node_id>
ray get actors <actor_id>
ray get tasks <task_id>
ray get jobs <job_id>
ray get placement-groups <placement_group_id>
```

### 56.4.1 五个最常用的过滤

| 目标 | 命令 | 说明 |
|---|---|---|
| 哪些 actor 还活着 | `ray list actors --filter "state=ALIVE"` | 判断是否有常驻进程 |
| 哪些 task 在跑 | `ray list tasks --filter "state=RUNNING"` | 看实际负载 |
| 哪些 task 在等资源 | `ray list tasks --filter "state=PENDING_NODE_ASSIGNMENT"` | 与 `ray status` 的 Demands 对照 |
| 某个节点的 actor | `ray list actors --filter "node_id=<id>"` | 缩容前确认 |
| 失败的 task | `ray list tasks --filter "state=FAILED"` | 看错误与重试 |

过滤字段名在不同 Ray 版本可能略有差异；如果 `node_id` 不生效，先用 `--detail` 看返回字段，再按字段名过滤。

### 56.4.2 `ray list` 与 Dashboard 的关系

三者通常读同一份 State API：

```text
Ray Dashboard (HTTP/UI)
        │
        ├── ray list / ray get / ray status
        │
        └── curl /api/v0/...
```

所以：

- Dashboard 看不到的节点，`ray list nodes` 通常也看不到；
- `ray list` 能用而 Dashboard 打不开，说明 Dashboard/端口转发有问题，不是 Ray 集群坏了；
- `ray status` 是 autoscaler 视角，`ray list nodes` 是 State API 视角，两者在“节点正在启动”阶段可能短暂不一致。

## 56.5 State API：把运行时状态变成可查询的数据

Ray Dashboard 在 head 的 8265 端口暴露 State API。它是**只读**的，适合脚本、告警和排障，不适合作为控制接口。

### 56.5.1 端点

| 端点 | 数据集 | 典型用途 |
|---|---|---|
| `/api/v0/nodes` | 节点 | raylet 是否注册、资源、状态 |
| `/api/v0/tasks` | task | 排队/运行/失败、调度原因 |
| `/api/v0/actors` | actor | 常驻进程、重启、所在节点 |
| `/api/v0/jobs` | Job Submission | 作业状态、入口、错误 |
| `/api/v0/placement_groups` | placement group | gang scheduling/资源预留 |
| `/api/v0/workers` | worker 进程 | worker 池、PID、节点 |
| `/api/v0/objects` | 对象 | 对象位置/大小（视版本） |
| `/api/v0/runtime_envs` | runtime env | 依赖环境状态 |
| `/api/v0/serve/applications` | Serve 应用 | 应用状态、部署 |
| `/api/v0/serve/deployments` | Serve deployment | 副本、健康 |
| `/api/v0/logs` | 日志 | 通过 API 取日志 |
| `/api/v0/events` | 事件 | 组件事件 |
| `/api/v0/healthz` | 健康 | Dashboard/State API 是否可用 |

端点集合随 Ray 版本演进；以目标版本的 Dashboard 源码/文档为准。

### 56.5.2 响应信封

State API 的响应不是裸数组，而是带统计信息的信封：

```json
{
  "result": true,
  "msg": "",
  "data": {
    "result": {
      "result": [
        {"node_id": "...", "state": "ALIVE", "resources": {}}
      ],
      "total": 1
    },
    "partial_failure_warning": ""
  }
}
```

用 `jq` 取值：

```bash
curl -s "http://127.0.0.1:8265/api/v0/nodes?format=json" \
  | jq '.data.result.result[] | {node_id, state, resources}'
```

### 56.5.3 查询参数

| 参数 | 作用 | 例子 |
|---|---|---|
| `filter` | 字段过滤 | `?filter=state=ALIVE` |
| `detail` | 返回更多字段 | `?detail=true` |
| `limit` | 限制条数 | `?limit=20` |
| `timeout` | 服务端超时（秒） | `?timeout=10` |
| `format` | 输出格式 | `?format=json` / `yaml` / `table` |

注意：

- State API 是**最终一致**的：刚提交的 task 可能短暂看不到；
- `limit` 截断的是返回条数，`total` 仍是总数；
- 大集群上 `detail=true` 可能很重，排障时先用 `limit` 收敛。

### 56.5.4 用 State API 做最小巡检

```bash
# 节点是否 ALIVE
curl -s "$RAY/api/v0/nodes?format=json" \
  | jq '[.data.result.result[] | select(.state!="ALIVE")] | length'

# 当前 PENDING 的 task
curl -s "$RAY/api/v0/tasks?format=json&detail=true" \
  | jq '[.data.result.result[] | select(.state|startswith("PENDING"))] | length'

# 失败的 job
curl -s "$RAY/api/v0/jobs?format=json" \
  | jq '[.data.result.result[] | select(.status=="FAILED")]'
```

把这三条放进定时巡检，比“每天打开 Dashboard 看一眼”可靠。

## 56.6 状态机：PENDING 到底在等什么

### 56.6.1 Task 状态

| 状态 | 含义 | 下一步看什么 |
|---|---|---|
| `PENDING_ARGS_AVAIL` | 参数还没就绪 | 上游 task/对象 |
| `PENDING_NODE_ASSIGNMENT` | 等节点资源 | `ray status` 的 Demands、节点容量 |
| `PENDING_OBJ_STORE_MEM_AVAIL` | 等对象存储内存 | 对象大小、spill、并发度 |
| `PENDING_ARGS_FETCH` | 正在拉取参数 | 网络、对象位置 |
| `SUBMITTED_TO_WORKER` | 已交给 worker | worker 进程/日志 |
| `RUNNING` | 正在执行 | 应用日志、py-spy |
| `RUNNING_IN_RAY_GET/WAIT` | 在 `ray.get`/`ray.wait` 中 | 依赖对象、死锁 |
| `FINISHED` | 完成 | 结果/下游 |
| `FAILED` | 失败 | 错误、重试、OOM |

最常见的两个卡点：

- **`PENDING_NODE_ASSIGNMENT`**：不是代码问题，是资源形状问题。看 Demands 的 CPU/GPU/自定义资源，和节点实际可分配资源对比；
- **`RUNNING_IN_RAY_WAIT`**：任务没死，在等一个永远不会来的对象。检查 driver 逻辑、actor 死锁、对象被回收。

### 56.6.2 Actor 状态

| 状态 | 含义 | 常见原因 |
|---|---|---|
| `DEPENDENCIES_UNREADY` | 依赖未就绪 | runtime_env/包导入 |
| `PENDING_CREATION` | 等待创建 | 资源不足、调度 |
| `ALIVE` | 正常 | — |
| `RESTARTING` | 正在重启 | 异常退出、`max_restarts` |
| `DEAD` | 已死亡 | 不可恢复、超过重启次数 |

Actor 的 `RESTARTING` 会掩盖应用层错误：control plane 看它“还在”，但状态一直在丢。看 actor 日志和 `max_restarts`。

### 56.6.3 Job 与 Serve

Job Submission 状态：`PENDING`、`RUNNING`、`STOPPED`、`SUCCEEDED`、`FAILED`。

Serve 应用常见状态：`DEPLOYING`、`RUNNING`、`DEPLOY_FAILED`（以及删除中的状态）。deployment 级别还有 `UPDATING`、`HEALTHY`、`UNHEALTHY` 等。不同 Ray 版本可能增加状态，排障时以实际输出为准。

一个实用判据：

```text
应用 RUNNING + deployment UNHEALTHY -> 副本/健康检查问题
应用 DEPLOYING 很久             -> import/依赖/资源
应用 RUNNING + 503              -> 路由/Service/proxy，不是应用状态
```

## 56.7 日志：`ray logs` 与 `kubectl logs` 的分工

### 56.7.1 先分清三类日志

| 日志 | 位置 | 回答 |
|---|---|---|
| Kubernetes/容器日志 | `kubectl logs` | 容器启动、raylet/GCS 进程输出 |
| Ray session 日志 | `/tmp/ray/session_latest/logs/` | Ray 组件/worker 的详细日志 |
| 应用日志 | driver/worker stdout | 业务代码 |

`ray logs` 同时能取 Ray 组件和 worker 日志：

```bash
ray logs cluster                 # 集群级日志（视版本）
ray logs raylet                  # raylet
ray logs gcs_server              # GCS
ray logs dashboard               # Dashboard
ray logs worker --pid <pid>      # 指定 worker
ray logs serve                   # Serve
ray logs --follow                # 跟踪
```

子命令名称随版本变化，先跑 `ray logs --help`。

### 56.7.2 在 Pod 内直接看

```bash
kubectl exec "$HEAD" -- ls -l /tmp/ray/session_latest/logs
kubectl exec "$HEAD" -- tail -n 200 /tmp/ray/session_latest/logs/raylet.out
kubectl exec "$HEAD" -- tail -n 200 /tmp/ray/session_latest/logs/gcs_server.out
kubectl exec "$HEAD" -- tail -n 200 /tmp/ray/session_latest/logs/dashboard.log
```

`session_latest` 是指向当前 session 的符号链接。Pod 重启后会指向新 session，旧日志可能还在磁盘上，但不会再被 `session_latest` 引用。

### 56.7.3 作业日志

```bash
ray job list
ray job status <job_id>
ray job logs <job_id>
```

KubeRay 的 RayJob status 里通常有 `jobId` 和 `dashboardURL`；即使 `kubectl` 看不到 driver，也可以用 job id 从 Dashboard/State API 反查。

### 56.7.4 容器里没有 shell 怎么办

Ray 镜像通常有 shell；但加固镜像/ distroless 镜像可能没有。此时不要为了排障重建 Pod，先用临时容器：

```bash
kubectl debug -it <pod> --image=busybox:1.36 --target=ray -- sh
```

注意：

- `--target` 共享目标容器的进程命名空间，才能看到 Ray 进程；
- 临时容器不改变原 Pod 的 spec，不会触发 KubeRay 重建；
- 需要集群允许 EphemeralContainers；
- 排障完成后临时容器会随 Pod 消失，不要把它当长期工具。

### 56.7.5 日志的“最后一公里”

如果日志显示进程正常、但任务不跑，不要继续加日志级别。改用：

- `py-spy dump --pid <pid>` 看 Python 栈；
- `ray debug`/`ray.util.pdb.set_trace()` 在 worker 里下断点；
- `ray timeline --address http://127.0.0.1:8265 --output /tmp/ray-timeline.json` 导出 Chrome trace；
- 指标（第 8、48 章）看趋势，而不是单点。

## 56.8 内存、对象存储与 spill

Ray 的很多“卡住”不是 CPU 不足，而是对象存储：

```bash
ray memory
ray memory --group-by "node_ip"
ray status
```

关注：

- `object_store_memory` 使用量；
- spill 是否发生、spill 到哪里；
- 是否有大对象被反复传递；
- `PENDING_OBJ_STORE_MEM_AVAIL` 的 task 数。

对象存储的上限与 `/dev/shm` 的关系见[第 6 章](kuberay教程-06-Pod、网络、存储与调度.md)；指标名与告警见[第 8 章](kuberay教程-08-状态、条件、事件与可观测性.md)。

## 56.9 MiniKubeRay 的 State API 子集

MiniKubeRay 在 API Server（默认 8080）上实现了一个**教学子集**，响应信封与 Ray 一致：

| 端点 | 是否实现 | 数据来源 |
|---|---|---|
| `/api/v0/nodes` | 是 | `RayRuntime` 的 `NodeRecord` |
| `/api/v0/jobs` | 是 | `JobRecord` |
| `/api/v0/serve/applications` | 是 | Serve 状态 |
| `/api/v0/events` | 是 | Kubernetes Event store |
| `/api/v0/healthz` | 是 | 固定返回 |
| `/api/v0/tasks`、`/actors`、`/objects` 等 | 否 | 教学实现不执行真实 task/actor |

启动：

```bash
python -m minikuberay server --history-path ./history.json
```

查询：

```bash
python -m minikuberay state nodes --cluster demo
python -m minikuberay state jobs -o json
python -m minikuberay state serve/applications
python -m minikuberay state events --filter Invalid
```

也可以直接访问 HTTP：

```bash
curl -s "http://127.0.0.1:8080/api/v0/nodes?cluster=demo&format=json" \
  | jq '.data.result.result[] | {podName, nodeType, state, cpu, gpu}'
```

注意三个教学差异：

1. MiniKubeRay 的 State API 在 operator API Server 上，真实 Ray 在 Dashboard（8265）；
2. MiniKubeRay 不模拟 task/actor/object，因此这些端点会返回 404，而不是伪造空数据；
3. MiniKubeRay 的 `filter` 是对整条 JSON 记录做子串匹配，真实 State API 是字段过滤。

实现分别在 `minikuberay/apiserver/server.py`（`_state_route`）、`minikuberay/kube/client.py`（`state`）和 `minikuberay/cli/main.py`（`cmd_state`）。

## 56.10 诊断剧本

### 56.10.1 RayCluster 不 Ready

```text
1. kubectl get raycluster <name> -o yaml
   - status.observedGeneration == metadata.generation?
   - conditions: HeadPodReady / RayClusterProvisioned / ReplicaFailure
2. kubectl get pods -l ray.io/cluster=<name> -o wide
3. kubectl describe pod <head>；看 Events
4. kubectl logs <head> -c ray
5. kubectl exec <head> -- ray status
```

判据：

- `observedGeneration` 落后：控制器没处理到最新 spec；
- `ValidationFailed`：spec 错误，不要看 Pod；
- `HeadPodReady=False`：head 启动失败；
- head Ready 但 worker 不足：看 worker Pending/注册。

### 56.10.2 Pod Running，但 Ray 看不到节点

```text
1. ray status / ray list nodes
2. kubectl logs <pod> -c ray | grep -i "gcs\|raylet\|version\|address"
3. kubectl logs <pod> -c wait-gcs-ready（如果注入）
4. 检查 head Service DNS 与端口
5. 检查 head/worker 的 Ray 版本与镜像 digest
```

常见根因：GCS 地址错误、head Service 不存在、NetworkPolicy 阻断、版本不一致、`rayStartParams` 让 raylet 监听错误端口。

### 56.10.3 task 一直 PENDING

```text
1. ray status -> Demands 是哪类资源？
2. ray list tasks --detail -> state 与 scheduling 信息
3. 节点实际可分配资源 vs 请求资源
4. placement group 是否 ready
5. 是否被 maxReplicas/节点池上限截断
```

注意：Ray 的 `num-cpus` 是逻辑资源，可能和 Pod requests 不一致。先对齐两者，再谈扩容。

### 56.10.4 RayJob 卡住

```text
1. kubectl get rayjob <name> -o yaml
   - status.jobDeploymentStatus
   - status.reason/message
2. kubectl get jobs,pods -l ray.io/rayjob=<name>
3. submitter Job/Pod 日志
4. ray job list / ray job status <jobId>
5. RayCluster 是否存在且 Ready
```

四种提交模式的生命周期不同（第 10 章）；先确认“卡在提交”还是“卡在执行”，不要直接删 submitter。

### 56.10.5 RayService 503

```text
1. kubectl get rayservice <name> -o yaml
   - conditions: Ready / UpgradeInProgress
   - active/pending service
2. ray serve status
3. kubectl get endpoints <service>
4. kubectl get httproute/gateway（如果使用增量升级）
5. Serve proxy/controller 日志
```

判据：应用 `RUNNING` 但 503，优先看 Service/EndpointSlice/路由，而不是应用代码。

### 56.10.6 head 丢失与 GCS FT 恢复

```text
1. 确认是否启用 GCS FT（spec.gcsFaultToleranceOptions）
2. 新 head Pod 是否创建、Ready
3. ray status 是否看到 worker 重新注册
4. Redis/PVC 是否健康
5. 是否有 job/actor 在恢复后丢失
```

没有 GCS FT 时，head 恢复后是“新集群”；不要把“Pod 回来了”当成“状态回来了”。

## 56.11 实验：把证据链跑出来

```bash
python examples/diagnosis_lab.py
```

输出：

```text
1 status      state=ready desired=2 ready=2
2 nodes       total=3 states=['ALIVE']
3 jobs        total=1 status=SUCCEEDED entrypoint=python train.py
4 restart     pod=diag-workers-0 replaced=True workers=2
5 invalid     reason=ValidationFailed provisioned=False message=worker group workers has minReplicas 5 greater than maxRepli
6 events      total=1 reasons=['InvalidRayClusterSpec']

order: status/conditions -> Pods -> runtime nodes -> jobs -> events -> logs

OK: status, State API nodes/jobs, self-healing and validation events form one evidence chain
```

它演示了：

- 正常集群的 status 与 State API 节点数一致；
- job 记录可以从 State API 查到终态；
- 删除 worker 后控制器用新 UID 重建；
- 非法 spec 不产生 Pod，只产生 status/condition/event；
- 事件里的 `reason` 是“为什么”的稳定锚点。

## 56.12 小结

- 诊断顺序是控制面 → Pod → Ray 节点 → 应用 → 进程，不要一上来重启。
- `ray status` 看资源与 Demands；`ray list` 看对象状态；State API 把两者变成可脚本化的数据。
- PENDING 的不同前缀对应不同的等待原因；`RUNNING_IN_RAY_WAIT` 常是应用死锁。
- `ray logs`、`kubectl logs`、session 日志和 trace 各有分工。
- MiniKubeRay 的 `/api/v0` 子集可以离线演练同样的证据链。
- 好的排障不是“找到一条报错”，而是让 status、事件、节点、作业、日志互相印证。

下一章进入 2026 年的新方向：垂直弹性。KubeRay 的 In-Place Pod Resizing（IPPR）让 worker 在不重建 Pod 的情况下扩 CPU/内存；它会改变你对“扩容=加副本”的默认假设。

→ [第 57 章 垂直弹性：KubeRay IPPR 与原地 Pod 扩缩](kuberay教程-57-垂直弹性-KubeRay-IPPR与原地Pod扩缩.md)

速查资料从附录 A 开始：API 字段、端口、事件、术语和实验索引。
