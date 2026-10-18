# 第 24 章 测试、验证与故障注入

“能跑”不是一句口号。本章给出 MiniKubeRay 的验证体系：测试分层、确定性时钟、故障注入、文档一致性检查和端到端演示。

## 24.1 验证矩阵

| 层 | 命令 | 覆盖 |
|---|---|---|
| 单元 | `python -m pytest -q` | 工具、Store、默认/校验、cron |
| 控制器 | 同上 | RayCluster/RayJob/RayService/RayCronJob |
| 接口 | 同上 | REST/watch/CLI/Dashboard/指标 |
| 端到端 | 同上 + `demo` | 多组件收敛 |
| 文档 | `python check_docs.py` | 链接、路径、YAML、清单、事实数字 |
| 演示 | `python -m minikuberay demo --fast` | 真实可运行流程 |
| 示例 | `python examples/autoscaling_smoke.py` | autoscaler 独立验证 |
| 决策实验 | `python examples/autoscaler_walkthrough.py` | 多资源/Conservative/多主机/缩容决策 |
| 提交模式实验 | `python examples/rayjob_modes.py` | 四种 RayJob 模式的状态机差异 |
| 生命周期实验 | `python examples/cluster_lifecycle.py` | suspend/resume/Recreate/victim deletion/扩缩容 |
| 定时任务实验 | `python examples/cronjob_lab.py` | cron/时区/suspend/幂等 |
| 数据执行实验 | `python examples/data_execution_lab.py` | 背压/shuffle/spill |
| 弹性训练实验 | `python examples/elastic_training_lab.py` | checkpoint/world size/gang |
| LLM 调度实验 | `python examples/llm_scheduler_lab.py` | batching/前缀缓存/PD |
| 可观测性实验 | `python examples/observability_lab.py` | trace/RED/SLO/History |
| 策略实验 | `python examples/policy_lab.py` | enforce/audit/回归 |
| 治理实验 | `python examples/governance_lab.py` | PIT/skew/promotion/rollback |
| 弹性实验 | `python examples/elasticity_lab.py` | 装箱/扩容/抢占/PDB/Spot/配额 |
| 生命周期实验 | `python examples/lifecycle_lab.py` | grace/preStop/drain/滚动升级 |
| 开发回路实验 | `python examples/dev_loop_lab.py` | 打包/幂等提交/连接矩阵/idle |
| Operator 可靠性实验 | `python examples/operator_reliability_lab.py` | cache/选主/重试/探针/SLO |
| 变更管理实验 | `python examples/maintenance_lab.py` | Recreate/None/hash/head 维护 |
| 活体诊断实验 | `python examples/diagnosis_lab.py` | status/State API/自愈/事件 |
| IPPR 实验 | `python examples/ippr_lab.py` | 原地扩容/水平兜底/超时回滚/注解校验 |
| K8s 原语实验 | `python examples/k8s_primitives_lab.py` | sidecar 顺序/调度门/拓扑路由/Pod 级资源 |

当前基线：**272 个测试**（见 `docs/facts.json`），58 章，33 个样例清单，44 个 Python 模块。

十七个 `examples/*.py` 实验同时被 `tests/test_examples.py` 在进程内调用。这样“教程里的命令还能跑”不是靠人工检查，而是 pytest 的一部分：脚本的输出断言失败，CI 就红。

## 24.2 确定性时钟

真实控制器测试最大的敌人是 `sleep` 和挂钟时间。MiniKubeRay 的做法：

- 控制器只通过 `Clock` 取时间；
- 测试用 `ManualClock`；
- `Harness.step(n, advance)` 显式推进；
- TTL/超时/idle 都可以精确控制。

```python
clock = ManualClock()
operator = Operator(clock=clock, config=OperatorConfig())
...
operator.run_once()
clock.advance(60)
```

这样：

- 同一个测试在任何机器上结果一致；
- 不需要等待；
- 可以测试“60 秒后缩容”而不真的等 60 秒。

## 24.3 Harness

`tests/conftest.py` 的 `Harness`：

- 创建 `ManualClock`、`Operator`、`Store`、`Runtime`；
- `apply(kind, name, spec)`；
- `settle(rounds, advance)`；
- `step(rounds, advance)`；
- 访问 `operator.endpoints`。

用法：

```python
def test_cluster_ready(harness):
    harness.apply(C.KIND_RAYCLUSTER, "demo", cluster_spec(workers=2))
    harness.settle(30)
    assert harness.get(C.KIND_RAYCLUSTER, "demo")["status"]["state"] == "ready"
```

## 24.4 测试分层

### 24.4.1 工具/模型

`tests/test_util.py`：

- CPU/内存数量；
- label selector；
- semver/hash/name；
- duration；
- clock/ISO。

`tests/test_defaults_validation.py`：

- 四个 CRD 的默认；
- 名称/容器/副本/autoscaler/GCS FT/NetworkPolicy/History；
- RayJob 模式/selector/deletion；
- RayService 增量；
- RayCronJob schedule/timezone。

`tests/test_cron.py`：

- 步长/范围/列表/名称；
- DOM/DOW OR；
- 时区；
- 闰日；
- 非法。

### 24.4.2 Store

`tests/test_store.py`：

- CRUD/namespace/selector；
- 乐观并发；
- generation/status；
- finalizer 两阶段；
- patch；
- watch；
- dump/load；
- field selector。

### 24.4.3 调度/kubelet

`tests/test_scheduler_kubelet.py`：

- 资源拟合；
- GPU；
- nodeSelector；
- Pending/Running；
- crash；
- 删除；
- EndpointSlice。

### 24.4.4 控制器

`tests/test_raycluster.py`：

- 就绪/条件/资源；
- 手动扩缩；
- clamp；
- suspend/resume；
- 删除；
- 非法 spec；
- Recreate/None；
- autoscaler；
- 多主机/headless；
- Serve Service；
- 自定义 head Service；
- env；
- GCS FT；
- NetworkPolicy gate；
- auth secret。

`tests/test_rayjob.py`：

- 成功/失败/重试；
- submitter；
- shutdown/TTL；
- deadline；
- suspend；
- deletion rules；
- selector；
- 四种模式；
- finalizer；
- runtime env。

`tests/test_rayservice.py`：

- 初始 Ready；
- Service selector；
- exclude head；
- NewCluster/增量/回滚；
- suspend/delete；
- 非法配置；
- 自定义 Service；
- None。

`tests/test_raycronjob.py`：

- 调度/时区/suspend/feature gate/幂等。

`tests/test_ippr.py`：

- `ray.io/ippr` 解析与七类校验（未知 group、缺字段、`num-cpus`、`resizePolicy`、v2、上限低于 request）；
- 原地扩容：Pod UID 不变、request 改变、Raylet 逻辑资源同步；
- 超出 `max-cpu` 后水平兜底；
- 超时回滚 + 水平兜底；
- 收敛后不再重复写 Pod（幂等）。

### 24.4.5 接口/端到端

`tests/test_apiserver_cli.py`：

- REST/status/patch/watch/auth/read-only；
- nodes/workergroups 子资源；
- CLI apply/get/scale/delete/validate/demo/create/job submit/history。

`tests/test_history.py`：

- 事件归属、快照、删除后保留；
- JSON 持久化/重载；
- REST 会话/事件/回放/404。

`tests/test_tracing.py`：

- `operator.round` 根 span 与 `reconcile` 子 span 的父子关系；
- 异常 span 的 `ERROR` 状态与 `exception` 事件；
- 收集上限与 `clear()`；
- `/api/traces`、`/api/traces/<id>` 与 CLI `traces`；
- span tree 的父子顺序与错误标记。

`tests/test_batch_scheduler.py`：

- Volcano/scheduler-plugins PodGroup；
- YuniKorn schedulerName；
- 关闭后清理；
- WAS feature gate。

`tests/test_audit_regressions.py`：

- clusterSelector 不删除共享集群；
- ValidationFailed 可恢复；
- active 在升级中丢失时接管 pending；
- 增量升级 pending 有 Serve endpoint；
- Recreate 忽略 replica 边界、head 删除后仍替换 worker；
- 多主机 autoscaler 容量；
- worker auth env；
- PATCH status 不改 spec；
- 畸形 Pod 不阻塞其他 Pod；
- Initializing 阶段的 deadline。

`tests/test_e2e.py`：

- 全平台场景；
- 收敛后幂等；
- 删除清理；
- 多 namespace。

## 24.5 故障注入

MiniKubeRay 提供注解：

| 注解 | 作用 |
|---|---|
| `minikuberay.io/crash-after-seconds` | Pod 运行 N 秒后 Failed |
| `minikuberay.io/job-duration-seconds` | Ray job 运行时长 |
| `minikuberay.io/job-exit-code` | Ray job 退出码 |
| `minikuberay.io/serve-deploy-seconds` | Serve 部署延迟 |
| `minikuberay.io/pod-start-delay-seconds` | Pod 启动延迟 |
| `minikuberay.io/job-complete-after-seconds` | 模拟 Job Pod 完成（GCS FT cleanup 等） |

示例：

```python
harness.apply(
    C.KIND_RAYCLUSTER,
    "crashy",
    cluster_spec(workers=1),
    annotations={"minikuberay.io/crash-after-seconds": "5"},
)
```

用于测试：

- head/worker 失败重建；
- RayJob 失败/重试；
- RayService 升级中断；
- autoscaler 缩容。

## 24.6 文档一致性检查

`python check_docs.py` 检查：

1. 章节编号连续；
2. README 引用所有章节；
3. 本地链接可解析；
4. 文档中引用的 `minikuberay/...`、`manifests/...` 等路径存在；
5. 含 `apiVersion`/`kind` 的 YAML 块可解析（不是所有代码块）；
6. `manifests/samples` 的 Ray CR 通过默认+校验；
7. CRD 有 schema/status subresource；
8. `docs/facts.json` 的 tests/chapters/samples/modules 与实际一致；
9. README 包含 facts 标记和验证命令；
10. 所有“第 N 章”引用都指向存在的章节；
11. 每章末尾的“→ 第 N+1 章”没有跳号/重复；
12. `manifests/samples/README.md` 与磁盘上的样例文件双向一致。

注意它仍然抓不到语义错误：字段含义写反、数字算错、代码与文字描述不一致，都需要测试或人工审计。

它不能检查：

- 语义正确性（例如某段文字是否说错）；
- 外部链接可用性；
- 真实 Kubernetes 是否能 apply；
- 真实 Ray 的版本兼容。

这些需要人工 review/集成测试。

## 24.7 端到端演示

```bash
python -m minikuberay demo --fast
```

输出包括：

- RayCluster ready；
- RayJob Complete/SUCCEEDED；
- autoscaler 0 -> 3 -> 0；
- RayService Running + 升级；
- RayCronJob 子作业；
- 指标。

它验证了“所有控制器在同一进程内协同工作”。

## 24.8 示例脚本

```bash
python examples/autoscaling_smoke.py
python examples/autoscaler_walkthrough.py
python examples/rayjob_modes.py
python examples/cluster_lifecycle.py
python examples/cronjob_lab.py
python examples/incident_lab.py
python examples/scale_benchmark.py --clusters 30 --workers 2
python -m minikuberay server
python examples/run_platform.py --seconds 5
python examples/maintenance_lab.py
python examples/diagnosis_lab.py
```

| 脚本 | 验证的行为 | 是否进 pytest |
|---|---|---|
| `autoscaling_smoke.py` | 需求触发扩容、idle 缩容 | 否（verify.py） |
| `autoscaler_walkthrough.py` | 四组确定性决策 | 否（verify.py） |
| `rayjob_modes.py` | 四种提交模式都到 Complete | 是 |
| `cluster_lifecycle.py` | suspend/resume/Recreate/删除指定 Pod/扩缩容 | 是 |
| `cronjob_lab.py` | cron/时区/suspend/补跑/幂等 | 是 |
| `incident_lab.py` | 11 个故障场景 | 否（verify.py） |
| `scale_benchmark.py` | 对象数/写入数/收敛轮数 | 否（verify.py） |
| `data_execution_lab.py` | 背压/shuffle/spill | 是 |
| `elastic_training_lab.py` | checkpoint/world size/gang | 是 |
| `llm_scheduler_lab.py` | batching/前缀缓存/PD | 是 |
| `observability_lab.py` | trace/RED/SLO/History | 是 |
| `policy_lab.py` | enforce/audit/回归 | 是 |
| `governance_lab.py` | PIT/skew/promotion/rollback | 是 |
| `elasticity_lab.py` | 装箱/扩容/抢占/PDB/Spot/配额 | 是 |
| `lifecycle_lab.py` | grace/preStop/drain/滚动升级 | 是 |
| `dev_loop_lab.py` | 打包/幂等提交/连接矩阵/idle | 是 |
| `operator_reliability_lab.py` | cache/选主/重试/探针/SLO | 是 |
| `maintenance_lab.py` | Recreate/None/hash/head 维护 | 是 |
| `diagnosis_lab.py` | status/State API/自愈/事件 | 是 |
| `run_platform.py` | API + operator + HTTP 客户端 | 否（手动） |

`examples/run_platform.py` 会启动 API + operator，应用样例，打印状态。

### 24.8.1 为什么把示例纳入 pytest

示例脚本有三种“腐烂”方式：

1. 依赖的 API 改了名字，脚本再也跑不起来；
2. 脚本能跑，但输出/断言不再代表正确行为；
3. README 里的命令和实际文件不一致。

把 `main()` 纳入 pytest 覆盖第 1、2 种；`check_docs.py` 的文件路径检查覆盖第 3 种。代价是示例必须保持“无交互、可重复、退出码有意义”，这恰好也是教程示例应有的品质。

## 24.9 真实控制器的测试：envtest 与 kind

MiniKubeRay 的 272 个测试验证的是模型逻辑；真实 operator 还需要：

### 24.9.1 envtest（无集群的 API server）

controller-runtime 的 envtest 启动一个真实 kube-apiserver + etcd（不启动 kubelet/scheduler），适合测试 Reconcile：

```go
var testEnv *envtest.Environment

func TestMain(m *testing.M) {
    testEnv = &envtest.Environment{
        CRDDirectoryPaths: []string{"../../config/crd/bases"},
    }
    cfg, err := testEnv.Start()
    if err != nil { panic(err) }
    // 用 cfg 创建 manager/client，注册 scheme，跑 Reconcile
    code := m.Run()
    // 注意：os.Exit 不会执行 defer，必须在退出前显式 Stop。
    testEnv.Stop()
    os.Exit(code)
}
```

覆盖：

- 默认值/校验；
- finalizer 两阶段删除；
- status subresource；
- 冲突重试；
- ownerReference/GC 预期。

不覆盖：

- kubelet/scheduler；
- 真实 Ray；
- 网络/存储/GPU。

### 24.9.2 kind e2e

```bash
kind create cluster --name kuberay-e2e
make docker-build IMG=kuberay-operator:e2e
kind load docker-image kuberay-operator:e2e --name kuberay-e2e
make deploy IMG=kuberay-operator:e2e
kubectl apply -f ray-operator/config/samples/ray-cluster.sample.yaml
# v1.7.1 样例对象的 metadata.name 是 raycluster-kuberay
kubectl wait --for=condition=RayClusterProvisioned raycluster/raycluster-kuberay --timeout=5m
go test ./test/e2e/... -v
```

覆盖：

- CRD 安装/升级；
- operator RBAC；
- Pod/Service/EndpointSlice；
- 真实 Ray 进程/端口；
- 作业提交/日志。

### 24.9.3 CI 配方

```yaml
# .github/workflows/test.yaml（示意）
jobs:
  unit:
    steps:
      - run: go test ./ray-operator/...
      - run: python -m pytest -q          # MiniKubeRay
      - run: python check_docs.py
  e2e:
    steps:
      - run: kind create cluster
      - run: make docker-build IMG=... && kind load docker-image ...
      - run: make deploy IMG=...
      - run: go test ./test/e2e/...
```

原则：

- 单元测试快、每次 PR 跑；
- e2e 慢、合并前/夜间跑；
- 文档检查每次跑；
- 失败要保留 operator/Pod 日志。

## 24.10 覆盖率与盲区

建议用 `coverage.py` 看分支覆盖，但不要迷信数字。重点覆盖：

- 幂等（连续调和不写）；
- 冲突/重试；
- finalizer；
- 资源边界；
- 失败路径；
- 时间边界（TTL 前后一秒）。

当前测试的盲区：

- 真实 K8s 的 informer 缓存/限流；
- 真实 Ray 的失败语义；
- 网络/证书/Gateway 数据面；
- 大规模性能；
- 多线程竞态（Store 有锁，但测试主要是单线程）。

## 24.11 如何新增测试

1. 在 `tests/` 建 `test_xxx.py`；
2. 用 `Harness` 或直接 `ApiStore`；
3. 避免 `sleep`，用 `ManualClock`；
4. 断言“状态 + 资源 + 事件”，不要只看返回值；
5. 对时间边界分别测试 TTL 前/后；
6. 更新 `docs/facts.json` 的 tests 数字（如果增加测试）；
7. 跑 `python check_docs.py`。

## 24.12 验证记录（示例）

```text
$ python -m pytest -q
272 passed

$ python check_docs.py
chapters      : 56
links checked : 361
paths checked : 249
chapter refs  : 491
sample index  : 31
yaml blocks   : 48
manifests     : 38
tests         : 272
modules       : 43
OK: documentation, manifests and facts are consistent

$ python -m minikuberay demo --fast
...
Demo complete.

$ python examples/rayjob_modes.py
...
OK: all four submission modes reached Complete

$ python examples/cluster_lifecycle.py
...
OK: suspend/resume, Recreate upgrade, victim deletion and scaling converged

$ python examples/cronjob_lab.py
...
OK: cron scheduling, time zone, suspend and idempotency behaved as specified

$ python examples/data_execution_lab.py
...
OK: streaming execution respected backpressure, spilled under budget, and wrote every retained row.

$ python examples/observability_lab.py
...
OK: spans form a trace tree, the SLO math is reproducible, and History survives deletion.
```

（具体链接/YAML 数量以实际运行输出为准；`check_docs.py` 会打印。）

## 24.13 属性测试与模型检验

示例测试容易只覆盖“想得到的路径”。属性测试（property-based testing）用随机输入验证不变量，适合 Store/调度器/状态机。

### 24.13.1 适合属性的不变量

| 组件 | 不变量 |
|---|---|
| Store | resourceVersion 单调递增；status 写入不改 spec；深拷贝隔离 |
| Scheduler | 已分配资源不超过节点容量；nodeSelector/taint 不被违反 |
| RayCluster | 收敛后 `run_once()` 返回 False；desired 始终在 [min,max] |
| RayJob | 终态不会被重复计数；TTL 到期才删除 |
| RayService | active/pending 不会同时接 100% 流量（NewCluster） |

### 24.13.2 Hypothesis 示例

```python
# pip install hypothesis  （不在 pytest 必需依赖里，按需安装）
from hypothesis import assume, given, strategies as st
from minikuberay.models.raycluster import group_replicas

@given(st.integers(min_value=0, max_value=100),
       st.integers(min_value=0, max_value=100),
       st.integers(min_value=0, max_value=100))
def test_replicas_are_clamped(min_r, max_r, replicas):
    assume(min_r <= max_r)
    group = {"minReplicas": min_r, "maxReplicas": max_r, "replicas": replicas}
    desired = group_replicas(group)
    assert min_r <= desired <= max_r
```

注意：

- 先 `assume` 过滤非法输入；
- 不要生成无法收敛的场景；
- 失败时保存最小反例（Hypothesis 自带 shrink）；
- 属性测试不能替代示例测试，二者互补。

### 24.13.3 模型检验（model checking）

对状态机可以枚举状态转移：

```text
状态集合 × 事件集合 -> 断言：
  - 没有不可达状态
  - 没有终态再转移
  - 每个失败都有 reason
```

MiniKubeRay 的状态机较小，适合手写枚举；真实 operator 可以只对关键状态（RayJob deployment、RayService upgrade）做。

### 24.13.4 故障注入矩阵

| 故障 | 注入点 | 期望 |
|---|---|---|
| Pod crash | Pod 注解 | 重建/重试 |
| 镜像失败 | image tag | ImagePullBackOff + 事件 |
| API 冲突 | Store 并发 | 重试，不丢更新 |
| 网络超时 | 外部 client | 超时/重试，不阻塞调和 |
| 资源不足 | 调度器 | Pending + Unschedulable |
| finalizer 卡住 | cleanup Job | 超时兜底/告警 |

> 已内置的注入点只有注解类（crash/duration/exit code/启动延迟/serve 延迟/job-complete）和调度器资源不足；API 冲突、外部网络超时、镜像失败目前需要用 monkeypatch/自定义 fake 实现，不是现成开关。把这张表当作“该测什么”，而不是“都有现成注解”。

## 24.14 小结

- 确定性时钟 + Harness 是测试可重复的关键。
- 测试要覆盖模型、Store、控制器、接口、端到端、文档六层。
- 故障注入注解让失败路径可测。
- 文档检查抓“腐烂”，但抓不到语义错误。
- 当前基线 272 个测试；新增代码/章节后同步 facts。

下一章练习与扩展路线。

→ [第 25 章 练习、扩展路线与真实 KubeRay 的差距](25-roadmap-exercises.md)
