# 第 53 章 交互式开发与 Notebook 平台

前面的章节大多在讲“作业怎么跑完”。这一章讲另一个同样重要、却经常被平台文档忽略的回路：**人怎么把代码送进集群、怎么调试、怎么在不破坏隔离的前提下反复迭代**。

KubeRay 本身不提供 Notebook。它提供的是三种接入点：

1. **Ray Jobs API**（Dashboard 8265）：提交一次性脚本，支持 `runtime_env`、幂等 `submission_id`；
2. **Ray Client**（10001）：把本地 Python 进程连到集群，**已废弃**，只适合遗留代码；
3. **集群内进程**：Notebook/JupyterHub 直接跑在 Ray 节点里（或注入了 `RAY_ADDRESS` 的同命名空间 Pod 里），用 `ray.init(address="auto")`。

选错接入点，会得到两种典型事故：把 10001 暴露到公网，或者把 600 MiB 的数据目录塞进 `runtime_env`。

## 53.1 四种开发回路

| 回路 | 代码在哪 | 启动成本 | 隔离 | 适用 |
|---|---|---|---|---|
| `kubectl exec` + 本地脚本 | 手工拷贝进 Pod | 低 | 差 | 临时排障 |
| Jobs API + `runtime_env` | 本地目录上传 | 中 | 好 | 日常脚本、调试 |
| 集群内 Notebook | 集群内 PVC/镜像 | 低（已启动） | 取决于租户模型 | 探索式分析 |
| 本地 IDE + 端口转发 | 本地 + 集群 | 中 | 好 | 断点调试、可视化 |

### 53.1.1 连接方式对照

| 方式 | 端口/协议 | 需要版本匹配 | 认证 | 现状 |
|---|---|---|---|---|
| Ray Jobs API | 8265 HTTP | 客户端无需与集群同版本 | token/ingress | **推荐** |
| Dashboard | 8265 HTTP | 否 | token/ingress | 推荐（只读/提交） |
| `ray.init(address="auto")` | 集群内 GCS | 是 | 集群内 | 推荐（Notebook） |
| Ray Client `ray://host:10001` | 10001 gRPC-ish | **是（Ray + Python 次版本）** | token/TLS | **已废弃** |
| `kubectl exec` | apiserver | 否 | kubeconfig/RBAC | 排障 |
| `kubectl port-forward` | apiserver 隧道 | 否 | kubeconfig/RBAC | 本地访问 |

Ray Client 的废弃意味着：新平台不要把 10001 当作对外 API；已有代码逐步迁移到 Jobs API。

## 53.2 用 Jobs API 建立日常回路

### 53.2.1 最小命令

```bash
# 终端 A：把 Dashboard/Jobs API 转发到本地
kubectl port-forward svc/ray-cluster-dev-head-svc 8265:8265

# 终端 B：提交脚本
ray job submit \
  --address http://127.0.0.1:8265 \
  --submission-id train-2026-10-18 \
  --runtime-env-json '{
    "working_dir": ".",
    "excludes": [".git", ".venv", "__pycache__", "data/"],
    "env_vars": {"LOG_LEVEL": "INFO"}
  }' \
  -- python src/train.py --epochs 1
```

三个细节：

- `--submission-id` 让重试幂等：同一个 ID + 同样的内容返回同一个 job；同一个 ID + 不同内容会冲突；
- `working_dir: "."` 会把当前目录打包上传；`excludes` 必须显式排除 `.git`、虚拟环境、数据和缓存；
- Jobs API 的地址是 **Dashboard 的 8265**，不是 GCS 的 6379，也不是 Ray Client 的 10001。

### 53.2.2 `runtime_env` 的组成

| 字段 | 作用 | 常见坑 |
|---|---|---|
| `working_dir` | 打包代码目录 | 太大、包含数据、包含 `.git` |
| `py_modules` | 只打包指定 Python 包 | 路径依赖、命名冲突 |
| `pip` | 安装依赖 | 每次启动都装、版本漂移、与镜像冲突 |
| `conda` | Conda 环境 | 启动慢、镜像里没有 conda |
| `env_vars` | 环境变量 | 把 Secret 写进日志 |
| `excludes` | 排除文件 | 忘了排除 `data/`、`.venv` |
| `config` | 运行时配置（如 pip 镜像） | 与集群网络不匹配 |

原则：

- **基础依赖烘焙进镜像**（Ray、CUDA、常用库），`runtime_env` 只带业务代码和小依赖；
- **数据不要进 `working_dir`**，放对象存储/PVC，用参数或环境变量传路径；
- **`pip` 只装差分依赖**，并固定版本；不要在 `runtime_env` 里安装另一个 `ray`；
- **Secret 用 Kubernetes Secret 挂载**，不要写进 `env_vars`（会出现在 job 详情里）。

### 53.2.3 打包大小与缓存

`working_dir` 每次内容变化都会产生新的包。平台应监控：

- 上传大小（超过几百 MiB 就会显著拖慢启动）；
- 上传耗时；
- 相同 hash 的缓存命中率；
- 包内容是否包含数据/模型权重。

经验阈值：

| 大小 | 建议 |
|---|---|
| < 50 MiB | `runtime_env` 很合适 |
| 50–500 MiB | 只带代码；依赖烘焙进镜像 |
| > 500 MiB | 改用镜像/对象存储 URI，不要每次上传 |

## 53.3 InteractiveMode 与 Jobs API 的分工

RayJob 的 `InteractiveMode` 是另一种形态：

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: interactive
spec:
  submissionMode: InteractiveMode
  rayClusterSpec:
    rayVersion: "2.59.0"
    headGroupSpec:
      rayStartParams: {}
      template:
        spec:
          containers:
            - name: ray
              image: rayproject/ray:2.59.0
```

区别：

| 维度 | K8sJobMode | HTTPMode | InteractiveMode |
|---|---|---|---|
| 谁提交 | operator 创建 submitter Job | operator 通过 Dashboard HTTP 提交 | 用户自己通过 Jobs API |
| 何时提交 | RayJob 创建后自动 | RayJob 创建后自动 | 用户决定 |
| 作业 ID | operator 生成/管理 | operator 生成/管理 | 用户写入 `spec.jobId` |
| 适合 | CI/批处理 | 网络简单的批处理 | 探索、调试、Notebook |

InteractiveMode 的 RayJob 负责“把集群准备好”，不负责“把作业跑起来”。这正好适合 Notebook：

```text
JupyterHub -> 创建/复用 RayCluster（或 InteractiveMode RayJob）
          -> Notebook 里 ray.init(address="auto")
          -> 用户逐段执行
```

## 53.4 Notebook 平台的三种租户模型

### 53.4.1 每用户一个 RayCluster

```text
namespace: user-alice
  RayCluster: alice-ray (head + workers)
  Notebook Pod: alice-notebook (ray.init(address="auto"))
```

优点：

- 隔离最好：配额、网络、镜像、升级互不影响；
- 用户可以自由重启 head；
- 故障半径小。

缺点：

- 每个用户一个 head，资源浪费；
- 大量空闲集群需要 idle 回收；
- operator 对象数随用户数增长（第 41 章）。

适合：GPU 开发、强隔离、受监管团队。

### 53.4.2 共享 RayCluster + 多用户作业

```text
namespace: team-a
  RayCluster: shared-ray
  Notebook Pods: alice, bob, carol (都连同一个 head)
  Jobs: 每个用户用自己的 submission_id / namespace
```

优点：

- 资源利用率高；
- head 只有一个，运维简单。

缺点：

- 一个用户的 OOM/大 task 会影响所有人；
- Ray 的 namespace/actor 隔离有限；
- 权限边界在 Ray 内部较弱。

适合：CPU 探索、可信团队、教学。

### 53.4.3 每次运行一个 RayJob

```text
Notebook -> 生成参数 -> 提交 RayJob -> 等待结果 -> 展示
```

优点：

- 可复现：每次运行都有独立的 CR、镜像、runtime_env；
- 失败不影响其他人；
- 适合把探索结果固化成流水线。

缺点：

- 启动开销；
- 不适合交互式逐段执行。

适合：从 Notebook 走向生产的过渡阶段。

### 53.4.4 选择建议

| 需求 | 推荐 |
|---|---|
| 强隔离、GPU 开发 | 每用户一个 RayCluster |
| 团队共享 CPU 池 | 共享 RayCluster + 配额 |
| 可复现的实验 | 每次一个 RayJob |
| 教学/临时 | 共享集群 + idle culling |

## 53.5 JupyterHub 集成

JupyterHub 负责“给每个用户一个 Notebook Pod”，KubeRay 负责“给每个用户一个 Ray 集群”。两者通过 Kubernetes API 解耦，不需要专用插件。

一个典型的 KubeSpawner 配置片段：

```yaml
# jupyterhub_config.py 的示意，不是完整配置
import sys

c.JupyterHub.spawner_class = "kubespawner.KubeSpawner"
c.KubeSpawner.namespace = "jupyter"
c.KubeSpawner.service_account = "notebook-sa"
c.KubeSpawner.pvc_name_template = "notebook-{username}"
c.KubeSpawner.storage_capacity = "50Gi"
c.KubeSpawner.cpu_limit = 2
c.KubeSpawner.mem_limit = "8G"

# JupyterHub 的 idle culler 是独立 service（jupyterhub-idle-culler），
# 不是 KubeSpawner 的属性。按你的安装方式配置：
c.JupyterHub.services = [
    {
        "name": "idle-culler",
        "command": [sys.executable, "-m", "jupyterhub_idle_culler", "--timeout=3600"],
    }
]
```

配套要点：

- **ServiceAccount 权限**：Notebook 需要创建/读取自己的 RayCluster；用 Role/RoleBinding 限制在用户命名空间，不要给 cluster-admin；
- **命名空间模板**：`{username}` 命名空间 + ResourceQuota，避免一个用户吃光集群；
- **镜像**：Notebook 镜像里预装 `ray[default]`、Jupyter、调试工具；Ray 版本与集群镜像一致；
- **网络**：Notebook Pod 与 Ray head 在同一命名空间，直接用 head Service DNS；
- **idle culling**：JupyterHub 的 idle culler 管 Notebook；RayCluster 的 idle 回收要单独做（`suspend: true` 或删除 CR）。

### 53.5.1 Notebook 里怎么连 Ray

```python
import ray

# 在 Ray 节点内，或同命名空间且注入了 RAY_ADDRESS 的 Pod 内
ray.init(address="auto")

# 或者显式指定 head Service
ray.init(address="ray-cluster-dev-head-svc:6379")
```

`address="auto"` 依赖环境变量 `RAY_ADDRESS`（或本机 `/tmp/ray/ray_current_cluster`）。KubeRay 注入的 `RAY_ADDRESS` 指向 head 的 GCS；在 worker Pod 里也能用。**普通 Notebook Pod 不会自动获得这个变量**：要么在 Pod 里显式设置 `RAY_ADDRESS=<head-svc>:6379`，要么用 `ray.init(address="<head-svc>:6379")`。

**不要**在 Notebook 里写 `ray.init(address="ray://...:10001")`，除非你明确维护 Ray Client 的遗留路径。

### 53.5.2 Notebook 的生命周期

| 资源 | 谁回收 | 触发 |
|---|---|---|
| Notebook Pod | JupyterHub idle culler | 无心跳超时 |
| RayCluster | 平台控制器/定时任务 | 用户登出、空闲、TTL |
| Ray worker | Ray autoscaler | `idleTimeoutSeconds` |
| PVC | 保留策略 | 用户删除/归档 |

注意：JupyterHub 的 idle culler 只看 Notebook 的 kernel 活动，**不会**回收 RayCluster。Ray 集群可能在一个 Notebook 关闭后继续跑很久。平台需要自己的回收器：

```text
if notebook.last_activity > 2h and raycluster has no jobs:
    raycluster.spec.suspend = true   # 或删除
```

## 53.6 代码同步与迭代速度

### 53.6.1 三种同步方式

| 方式 | 速度 | 可复现 | 适合 |
|---|---|---|---|
| 烘焙镜像 | 慢（构建+推送+拉取） | 最强 | 生产、CI |
| `runtime_env.working_dir` | 中（打包+上传） | 强（内容 hash） | 日常开发 |
| `kubectl cp` / 共享 PVC | 快 | 弱 | 临时调试 |

推荐组合：

- **日常**：`runtime_env.working_dir` + `excludes`；
- **联调**：共享 PVC（代码和模型都在 PVC 上），Notebook 直接编辑；
- **上线**：镜像 + 固定 tag/digest。

### 53.6.2 热重载的边界

Ray 的 task/actor 是分布式进程，**不会**像本地 Flask 那样自动热重载：

- 修改 driver 代码：重新提交 job；
- 修改 task 代码：新 task 使用新代码，旧 task 仍跑旧代码；
- 修改 actor 代码：需要重启 actor；
- 修改 `runtime_env`：需要新的 job/submission_id。

因此，Notebook 里的“改一行、跑一次”通常意味着**重新提交一个 job**，而不是热更新进程。平台应把提交封装成一个函数：

```python
def run(entrypoint: str, **kwargs):
    return ray.job_submission.JobSubmissionClient("http://127.0.0.1:8265").submit_job(
        submission_id=f"{user}-{uuid4().hex[:8]}",
        entrypoint=entrypoint,
        runtime_env={"working_dir": ".", "excludes": [".git", ".venv", "data/"]},
    )
```

### 53.6.3 依赖漂移

开发环境与集群镜像的差异是“本地能跑、集群报错”的头号原因：

- Python 次版本不同（3.11 vs 3.13）；
- CUDA/驱动不同；
- 本地 `pip install` 的包版本与镜像不同；
- 系统库（glibc、OpenMPI）不同。

对策：

- Notebook 镜像与 Ray 镜像使用同一基础镜像；
- 用 `pip freeze`/lock 文件固定依赖；
- 在 CI 里用同一镜像跑测试；
- `runtime_env` 只装业务依赖，不装 Ray/CUDA。

## 53.7 调试

### 53.7.1 进入 Pod

```bash
kubectl exec -it <ray-pod> -- bash
# 看 Ray 进程
ray status
ray list nodes
ray list actors
# 看日志
ls /tmp/ray/session_latest/logs
```

### 53.7.2 Ray 的交互式调试

```bash
# 在 head 上启动调试器（Ray 版本支持时）
ray debug
```

或在代码里：

```python
import ray
ray.init()
# RAY_DEBUG=1 时，task 里的 breakpoint() 会进入调试器
```

注意：分布式断点会暂停 worker，可能触发 task 超时；只建议在开发集群使用。

### 53.7.3 py-spy

```bash
# 找到 Python 进程（raylet 是 C++，py-spy 只能看 Python）
kubectl exec <pod> -- ps -ef | grep python

# 采样
kubectl exec <pod> -- py-spy dump --pid <pid>
kubectl exec <pod> -- py-spy top --pid <pid>
kubectl exec <pod> -- py-spy record -o /tmp/profile.svg --pid <pid> --duration 30
```

适合定位：

- driver 卡在哪个调用；
- worker 是否在等对象；
- Serve 副本是否在序列化/反序列化上耗时。

### 53.7.4 本地 IDE + 端口转发

```bash
# Dashboard / Jobs API
kubectl port-forward svc/ray-cluster-dev-head-svc 8265:8265

# Serve（如果启用）
kubectl port-forward svc/ray-cluster-dev-serve-svc 8000:8000

# 调试器（debugpy）
kubectl port-forward <pod> 5678:5678
```

VS Code 的 Remote - Kubernetes / Dev Containers 也可以直接 attach 到 Pod，但要注意：

- 不要把调试端口暴露到公网；
- 调试会话会占用 Pod 资源；
- 生产命名空间禁用调试端口。

### 53.7.5 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| `ray.init(address="auto")` 失败 | 不在 Ray Pod 内/没有 `RAY_ADDRESS` | 用 head Service DNS 显式连接 |
| `ray://` 连接报版本不匹配 | Ray Client 要求精确版本 | 改用 Jobs API |
| job 一直 PENDING | 集群没有资源/队列 | 看 `ray status`、quota、节点池 |
| `working_dir` 上传很慢 | 包含 `.git`/数据 | 加 `excludes`，数据放对象存储 |
| 本地 import 成功、集群失败 | 依赖漂移 | 固定镜像与 lock 文件 |
| Notebook 断开后任务停了 | 任务跑在 kernel 进程里 | 用 Jobs API/actor 提交，不依赖 kernel |

## 53.8 安全与多租户

Notebook 平台通常拥有较高的集群权限，必须单独设计：

- **不要让 Notebook 使用 cluster-admin**；每个命名空间一个 ServiceAccount，只允许管理自己的 RayCluster；
- **不要暴露 10001/8265 到公网**；用 Ingress + 认证，或只允许 port-forward；
- **Ray token auth**（第 14 章）：head 的 Dashboard/Jobs API 需要 token；
- **NetworkPolicy**：Notebook 只能访问同命名空间的 head；worker 之间按需放行；
- **镜像策略**：只允许签名/扫描过的镜像；
- **资源配额**：Notebook 和 RayCluster 都要有 quota；
- **审计**：记录谁提交了哪个 job、用了哪些数据。

一个最小 RBAC 示例：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: notebook-ray
  namespace: user-alice
rules:
  - apiGroups: ["ray.io"]
    resources: ["rayclusters", "rayjobs"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["pods", "pods/log", "services"]
    verbs: ["get", "list", "watch"]
```

注意：允许创建 RayCluster 等于允许消耗该命名空间的资源；必须配合 ResourceQuota 和准入策略（第 49 章）。

## 53.9 实验：`examples/dev_loop_lab.py`

```bash
python examples/dev_loop_lab.py
```

预期输出：

```text
[packaging] 3 files, 10.0 MiB, digest=428b1c5b8c7b; .git/.venv/__pycache__/data excluded
[size] without excluding data/: 610 MiB -> warning raised
[idempotency] same submission_id + same content = same job; different content = conflict
[connect] outside: Jobs API works across versions; Ray Client 10001 is deprecated
[notebook] idle culler fires at last_activity+300s; active kernels survive
[identity] path layout is part of the runtime_env digest; pin the image for stability
OK: the dev loop has a deterministic packaging, submission, connection and idle-culling contract.
```

实验的四条结论：

1. **打包要显式排除**：`.git`、`.venv`、`__pycache__`、数据目录；
2. **hash 只对内容敏感**：mtime 变化不应触发重新上传，代码变化必须触发；
3. **提交要幂等**：`submission_id` 相同且内容相同 → 同一个 job；内容不同 → 冲突；
4. **连接要选对**：Jobs API 跨版本可用，Ray Client 已废弃且要求精确版本。

## 53.10 检查表

- [ ] 新代码默认走 Jobs API，不再新增 Ray Client 依赖；
- [ ] `working_dir` 有 `excludes`，不包含数据/模型/虚拟环境；
- [ ] 基础依赖烘焙进镜像，`runtime_env` 只带差分；
- [ ] `submission_id` 可重试且内容冲突会报错；
- [ ] Notebook 有 idle culler，RayCluster 有独立回收策略；
- [ ] Notebook ServiceAccount 只管理自己的命名空间；
- [ ] 10001/8265 不暴露到公网；
- [ ] 调试端口只在开发集群开放；
- [ ] 开发镜像与 Ray 镜像的基础环境一致；
- [ ] 有一条从 Notebook 到 RayJob 的“固化”路径。

## 53.11 练习

1. 把 `dev_loop_lab.py` 的 `package_working_dir` 改成读取真实目录（只读），并对比 `du -sh` 与打包大小。
2. 给 `JobServer` 增加“同一个 submission_id、内容相同但用户不同”的权限检查。
3. 设计一个 JupyterHub + KubeRay 的命名空间模板：每个用户一个 namespace、一个 RayCluster、一个 ResourceQuota。
4. 用 kind 集群跑通 `ray job submit --address http://...:8265`，再用 `ray://` 复现版本不匹配错误。
5. 解释为什么“Notebook 里 `ray.init(address="auto")`”比“本地 `ray://`”更适合多租户平台。

## 53.12 小结

- 交互式开发有四条回路：exec、Jobs API、集群内 Notebook、本地 IDE + 端口转发。
- Jobs API（8265）是推荐路径；Ray Client（10001）已废弃，要求精确版本匹配。
- `runtime_env` 的关键是“只带代码、不带数据”，并用 `excludes` 控制打包内容。
- Notebook 平台有三种租户模型：每用户集群、共享集群、每次一个 RayJob。
- JupyterHub 的 idle culler 不管 RayCluster，平台要单独回收。
- 调试要靠 `ray status`、日志、py-spy、端口转发和（开发环境的）`ray debug`。
- Notebook 的高权限是最大的安全风险，RBAC/配额/网络策略必须一起上。
- `dev_loop_lab.py` 把打包、幂等提交、连接选择和 idle 回收变成了可断言的结果。

到这里，教程的主线从“为什么需要 KubeRay”一直走到“人怎么用、集群怎么退出、节点怎么扩”。附录提供了字段、端口、排障、术语、时序和实验索引，可以按需查阅。

← [第 52 章 生命周期、驱逐与优雅终止](52-graceful-termination-disruption.md)

→ [第 54 章 Operator 可靠性与控制面 SLO](54-operator-reliability-control-plane-slo.md)
