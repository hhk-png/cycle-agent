# 附录 K. 可运行实验与验证索引

本附录把仓库里所有“可以跑的东西”集中到一张表：命令、耗时量级、证明了什么、输出里该看到什么、对应哪一章。它也是 CI 与新人 onboarding 的入口。

## K.1 五分钟全量验证

在教程根目录依次执行：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python check_docs.py
python -m minikuberay demo --fast
python examples/incident_lab.py
python examples/observability_lab.py
python examples/elasticity_lab.py
python examples/lifecycle_lab.py
python examples/dev_loop_lab.py
python examples/operator_reliability_lab.py
python examples/maintenance_lab.py
python examples/diagnosis_lab.py
python examples/ippr_lab.py
python examples/k8s_primitives_lab.py
```

预期：

```text
272 passed
OK: documentation, manifests and facts are consistent
Demo complete.
11/11 scenarios passed (deterministic ManualClock)
OK: spans form a trace tree, the SLO math is reproducible, and History survives deletion.
OK: node autoscaling packed Pods cost-aware, honored quotas, preempted by priority, and respected PDBs during Spot drain.
OK: grace budgets, preStop/SIGTERM, head failure, PDB drains, rolling upgrades and Spot warnings are reproducible.
OK: the dev loop has a deterministic packaging, submission, connection and idle-culling contract.
OK: cache sync, leader election, retry backoff, panic recovery, probe semantics and the reconcile SLO are reproducible.
OK: upgrade strategy, spec-hash scope, mixed-version risk and head maintenance are observable
OK: status, State API nodes/jobs, self-healing and validation events form one evidence chain
OK: IPPR resized a running Pod in place, fell back to horizontal capacity, rolled back a timed-out resize, and rejected an invalid annotation.
OK: sidecars ordered correctly, Jobs completed, gates blocked scheduling, PreferSameZone stayed in-zone, and Pod-level resources were visible to the scheduler but not to Ray's container-based detection.
```

一条命令跑完（更慢，包含基准）：

```bash
python scripts/verify.py
```

最后一行应为 `All verification checks passed.`。

## K.2 命令速查表

| 命令 | 量级 | 证明了什么 | 关键输出 |
|---|---|---|---|
| `python -m pytest -q` | ~40s | 模型/控制器/接口/文档全部一致 | `272 passed` |
| `python check_docs.py` | <2s | 链接、代码路径、YAML、样例、章节引用/下一章链接、facts 不腐烂 | `OK: ... consistent` |
| `python -m minikuberay demo --fast` | ~1s | 四个 CRD 在同一控制面收敛 | `Demo complete` |
| `python -m minikuberay server` | 常驻 | REST/Dashboard/History/指标可用 | 打印监听地址 |
| `python examples/autoscaling_smoke.py` | <1s | demand 触发扩容、idle 缩容 | `after demand` / `after idle` |
| `python examples/autoscaler_walkthrough.py` | <1s | 四组确定性扩缩容决策 | `All decisions were produced by ...` |
| `python examples/rayjob_modes.py` | ~1s | 四种提交模式都到 Complete | `OK: all four submission modes ...` |
| `python examples/cluster_lifecycle.py` | ~1s | suspend/resume/Recreate/删除指定 Pod/扩缩容 | `OK: suspend/resume ...` |
| `python examples/cronjob_lab.py` | <1s | cron/时区/suspend/补跑/幂等 | `OK: cron scheduling ...` |
| `python examples/incident_lab.py` | ~2s | 11 个故障场景可复现 | `11/11 scenarios passed` |
| `python examples/scale_benchmark.py` | 秒级 | 对象数/写入数/收敛轮数 | `clusters=... objects=...` |
| `python examples/run_platform.py --seconds 5` | ~5s | API + operator + HTTP 客户端 | `platform running at ...` |
| `python examples/data_execution_lab.py` | <1s | 背压、shuffle、spill、流式峰值 | `OK: streaming execution ...` |
| `python examples/elastic_training_lab.py` | <1s | checkpoint/resume、world size、gang | `OK: elastic resume replayed 0 steps ...` |
| `python examples/llm_scheduler_lab.py` | <1s | continuous batching、前缀缓存、PD | `OK: prefix caching cut TTFT ...` |
| `python examples/observability_lab.py` | ~1s | trace 树、RED、SLO/错误预算、History | `OK: spans form a trace tree ...` |
| `python examples/policy_lab.py` | <1s | 策略扫描、enforce/audit、回归 | `OK: the admission contract ...` |
| `python examples/governance_lab.py` | <1s | PIT 特征、skew、promotion、rollback | `OK: point-in-time features ...` |
| `python examples/elasticity_lab.py` | <1s | 节点装箱、CA 扩容、抢占、PDB、Spot、配额 | `OK: node autoscaling packed Pods ...` |
| `python examples/lifecycle_lab.py` | <1s | preStop/SIGTERM/grace、PDB drain、滚动升级、Spot 通知 | `OK: grace budgets, preStop/SIGTERM ...` |
| `python examples/dev_loop_lab.py` | <1s | runtime_env 打包、幂等提交、连接矩阵、idle 回收 | `OK: the dev loop has a deterministic packaging ...` |
| `python examples/operator_reliability_lab.py` | <1s | cache sync、选主、workqueue、panic、探针、SLO | `OK: cache sync, leader election ...` |
| `python examples/maintenance_lab.py` | <1s | `Recreate`/`None`、spec hash、混合版本、head 按住 | `OK: upgrade strategy, spec-hash scope ...` |
| `python examples/diagnosis_lab.py` | ~1s | status/State API/自愈/校验事件的证据链 | `OK: status, State API nodes/jobs ...` |
| `python examples/ippr_lab.py` | <1s | 原地扩容 UID 不变、水平兜底、超时回滚、注解校验 | `OK: IPPR resized a running Pod in place ...` |
| `python examples/k8s_primitives_lab.py` | <1s | 原生 sidecar 顺序、调度门、`PreferSameZone`、Pod 级资源 | `OK: sidecars ordered correctly ...` |
| `python -m minikuberay state nodes --cluster demo` | 需 server | State API 子集 + 表渲染 | 节点表与 `total=` |
| `python -m minikuberay traces --tree` | 需 server | 查询 operator trace | span 树 |
| `python scripts/verify.py` | ~45s | 上述核心检查的合集 | `All verification checks passed.` |

## K.3 实验与章节/代码/测试映射

| 实验 | 主要章节 | 代码入口 | pytest 覆盖 |
|---|---|---|---|
| `autoscaling_smoke.py` | 9、34 | `autoscaler/policy.py` | `tests/test_raycluster.py`（间接） |
| `autoscaler_walkthrough.py` | 9、34 | `autoscaler/policy.py` | 无（verify.py） |
| `rayjob_modes.py` | 10、22 | `controllers/rayjob.py` | `tests/test_examples.py` |
| `cluster_lifecycle.py` | 5、7、21 | `controllers/raycluster.py` | `tests/test_examples.py` |
| `cronjob_lab.py` | 12、22 | `controllers/raycronjob.py` | `tests/test_raycronjob.py`、`tests/test_examples.py` |
| `incident_lab.py` | 37、42 | 多个控制器 | 无（verify.py） |
| `scale_benchmark.py` | 41 | `store/store.py`、`controllers/base.py` | 无（verify.py） |
| `run_platform.py` | 16、23 | `apiserver/server.py` | `tests/test_apiserver_cli.py` |
| `data_execution_lab.py` | 45 | 实验内 `StreamingPipeline` | `tests/test_examples.py` |
| `elastic_training_lab.py` | 46 | 实验内 `ElasticTrainer` | `tests/test_examples.py` |
| `llm_scheduler_lab.py` | 47 | 实验内 `Scheduler` | `tests/test_examples.py` |
| `observability_lab.py` | 48 | `tracing.py`、`controllers/base.py` | `tests/test_examples.py`、`tests/test_tracing.py` |
| `policy_lab.py` | 49 | 实验内策略引擎 | `tests/test_examples.py` |
| `governance_lab.py` | 50 | 实验内 `Registry`/`FeatureView` | `tests/test_examples.py` |
| `elasticity_lab.py` | 51 | 实验内 `Fleet`/`ClusterAutoscaler` 模型 | `tests/test_examples.py` |
| `lifecycle_lab.py` | 52 | 实验内 `terminate()`/`ReplicaSet` 模型 | `tests/test_examples.py` |
| `dev_loop_lab.py` | 53 | 实验内 `package_working_dir`/`JobServer` | `tests/test_examples.py` |
| `operator_reliability_lab.py` | 54 | 实验内 `WorkQueue`/`InformerCache`/`Candidate` | `tests/test_examples.py` |
| `maintenance_lab.py` | 7、55 | `controllers/raycluster.py`（spec hash/upgradeStrategy） | `tests/test_examples.py` |
| `diagnosis_lab.py` | 8、56 | `apiserver/server.py`（`/api/v0/*`）、`runtime/cluster_runtime.py` | `tests/test_examples.py`、`tests/test_apiserver_cli.py` |
| `ippr_lab.py` | 57 | `models/ippr.py`、`autoscaler/policy.py`、`controllers/raycluster.py`、`controllers/batch.py` | `tests/test_ippr.py`、`tests/test_examples.py` |
| `k8s_primitives_lab.py` | 58 | 独立语义模型（不依赖 `minikuberay`） | `tests/test_examples.py` |

## K.4 CLI 端到端演练

MiniKubeRay 的 CLI 与 kubectl-ray 的常见操作对齐。除 `server`/`demo`/`validate` 外，CLI 命令默认连接 `http://127.0.0.1:8080`，所以先在终端 A 启动服务：

```bash
# 终端 A
python -m minikuberay server --history-path ./history.json
```

终端 B：

```bash
S=http://127.0.0.1:8080

# 1) 校验清单（不创建对象；validate 必须用 -f）
python -m minikuberay validate -f manifests/samples/ray-cluster.yaml

# 2) 创建集群并观察收敛
python -m minikuberay create cluster demo --worker-replicas 2 --server $S
python -m minikuberay get rayclusters --server $S
python -m minikuberay describe raycluster/demo --server $S

# 3) 扩缩容（GROUP=REPLICAS）
python -m minikuberay scale raycluster/demo workers=3 --server $S

# 4) 提交作业并看日志
python -m minikuberay job submit nightly --cluster demo \
  --entrypoint "python -c 'print(1)'" --server $S
python -m minikuberay logs rayjob/nightly --server $S

# 5) History：删除集群后仍能回看
python -m minikuberay history list --server $S
python -m minikuberay history show default/demo --server $S

# 6) State API 子集：节点/作业/Serve/事件
python -m minikuberay state nodes --cluster demo --server $S
python -m minikuberay state jobs -o json --server $S
python -m minikuberay state events --filter Invalid --server $S
```

预期：`create` 后 `get` 显示 `ready`，`scale` 后 worker 数变为 3，`logs` 里能看到 `RUNNING -> SUCCEEDED`，`history list` 能看到 `demo` 会话。

> 不指定 `--server` 时，CLI 不会隐式创建本地状态；它直接连接默认地址。要练习“跨命令保持状态”，必须让 server 常驻，或者用 `MINIKUBERAY_SERVER` 环境变量指向别处。

## K.5 REST/Dashboard 演练

```bash
python -m minikuberay server --history-path ./history.json
```

另开终端：

```bash
curl -s http://127.0.0.1:8080/api/v1/rayclusters | jq
curl -s http://127.0.0.1:8080/api/v1/namespaces/default/rayclusters | jq
curl -s http://127.0.0.1:8080/metrics | head
curl -s http://127.0.0.1:8080/api/history/sessions | jq
```

Dashboard：`http://127.0.0.1:8080/dashboard`。

| 路径 | 用途 |
|---|---|
| `/api/v1/<kind>` | 全 namespace 列表/创建 |
| `/api/v1/namespaces/<ns>/<kind>` | namespace 级列表/创建 |
| `/api/v1/namespaces/<ns>/<kind>/<name>` | get/update/delete |
| `/api/v1/namespaces/<ns>/<kind>?watch=true` | watch 流 |
| `/metrics` | Prometheus 文本 |
| `/api/history/sessions` | History 会话 |
| `/api/traces` | 最近 trace 列表与统计 |
| `/api/traces/<trace_id>` | 单条 trace 的 spans |
| `/api/v0/nodes` | Ray 风格 State API：节点 |
| `/api/v0/jobs` | Ray 风格 State API：作业 |
| `/api/v0/serve/applications` | Ray 风格 State API：Serve 应用 |
| `/api/v0/events` | Ray 风格 State API：事件 |
| `/dashboard` | 只读 Dashboard |

具体路由以 `minikuberay/apiserver/server.py` 为准；接口字段见[附录 A](appendix-a-api-reference.md)。

## K.6 实验失败时怎么查

| 失败点 | 先看 | 常见原因 |
|---|---|---|
| `pytest` 收集失败 | import 错误/路径 | 在错误目录运行、缺依赖 |
| `pytest` 断言失败 | 第一个失败用例 | 改动改变了状态机/默认值 |
| `check_docs.py` 失败 | 打印的 `FAILED` 列表 | 断链、代码路径不存在、facts 过期 |
| `validate` 失败 | 输出的 problems | YAML 字段/互斥规则 |
| demo 卡住 | 是否用了 `--fast` | 真实时钟 + 长 TTL |
| 实验输出与文档不一致 | 对照章节/`docs/facts.json` | 文档过期或代码回归 |
| server 端口占用 | `--port` | 本地已有进程 |

原则：先跑最小失败命令，再看它对应的章节和代码入口；不要同时改多个层。

## K.7 CI 最小集合

```bash
python -m pytest -q
python check_docs.py
python -m minikuberay demo --fast
python examples/rayjob_modes.py
python examples/cluster_lifecycle.py
python examples/cronjob_lab.py
python examples/incident_lab.py
python examples/data_execution_lab.py
python examples/elastic_training_lab.py
python examples/llm_scheduler_lab.py
python examples/observability_lab.py
python examples/policy_lab.py
python examples/governance_lab.py
```

`scale_benchmark.py` 的时间受机器影响，建议只做趋势/上限断言，不放进 PR 的硬门禁。

## K.8 新增实验的模板

```python
#!/usr/bin/env python3
"""One sentence: what invariant does this lab prove?"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minikuberay.clock import ManualClock
from minikuberay.controllers.base import Operator, OperatorConfig

def main() -> int:
    clock = ManualClock()
    operator = Operator(clock=clock, config=OperatorConfig())
    # 1) 创建对象  2) 推进时钟  3) 断言状态/UID/事件
    # 4) 失败返回 1，成功打印 OK 并返回 0

if __name__ == "__main__":
    raise SystemExit(main())
```

要求：

1. 不依赖网络、K8s、Ray；
2. 用 `ManualClock`，不 `sleep`；
3. 断言“对象/状态/事件”，不只打印；
4. 失败返回非零；
5. 在 `tests/test_examples.py` 加一行测试，并同步 `docs/facts.json`。

← [附录 J. 端到端时序图集](appendix-j-sequence-atlas.md) ｜ [返回目录](README.md) ｜ [附录 A. API 字段速查](appendix-a-api-reference.md)
