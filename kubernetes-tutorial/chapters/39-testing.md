# 第 39 章 测试策略：从单元到端到端

## 39.1 测试金字塔

```text
        E2E / examples (17)
      Integration (cluster, API)
    Unit (store, selectors, admission)
```

MiniK8s 的测试分布：

- 单元：apimachinery、store、cron、selector、quantity。
- 集成：cluster + 控制器 + kubelet（进程内）。
- API：HTTP + client + auth/RBAC。
- CLI：subprocess 调用真实 CLI。
- E2E：18 个 YAML 场景。
- 文档：章节链接/代码块/YAML/计数。

当前：**217 个测试 + 18 个示例**。

## 39.2 为什么需要三层

只做单元：

- 无法验证控制器之间的协作。
- 无法发现 watch/status 时序问题。

只做 E2E：

- 慢、难定位。
- 无法覆盖边界条件。

集成层（MiniCluster）是甜点：

- 真实组件、真实 store、真实调度/控制循环。
- 不依赖网络/容器。
- 可用 `wait_until` 等待收敛。

## 39.3 Fixture 设计

`tests/conftest.py`：

```python
@pytest.fixture
def cluster():
    instance = MiniCluster(start_api=False)
    instance.start()
    yield instance
    instance.stop()

@pytest.fixture
def api_cluster():
    instance = MiniCluster()
    instance.start()
    ...
```

- `cluster`：无 HTTP，测试控制器/调度/存储更快。
- `api_cluster`：有 HTTP，测试 API/CLI/RBAC/operator。
- 每个测试独立集群，避免污染。
- 随机端口，避免冲突。
- 测试结束 stop 所有线程。

## 39.4 确定性

测试不能依赖真实时间/网络：

- kubelet tick 固定 0.1s。
- CronJob 注入假时钟。
- HPA 使用模拟 usage。
- 容器完成用 `run-ticks`。
- 镜像/命令是模拟的。
- 节点 IP 固定。

集成/端到端测试优先使用 `wait_until` 轮询最终状态，而不是固定 `sleep(5)`：

```python
cluster.wait_until(lambda: deployment_ready(cluster), timeout=10)
```

超时错误会带最后状态，便于定位。少数测试仍用短 sleep 来验证“某件事没有发生”（例如“不应该被驱逐”），因为 `wait_until` 只等待条件为真，无法证明条件永远为假；这类测试要尽量短、只用于否定断言，并配合明确的描述。不要用 sleep 等待最终状态。

## 39.5 测试命名与覆盖

文件：

- `test_apimachinery.py`：quantity/selector/cron/meta。
- `test_store.py`：CRUD/conflict/finalizer/watch/pagination/并发。
- `test_admission.py`：默认值/校验/Quota/LimitRange/PSA/schema。
- `test_api.py`：HTTP/CRUD/watch/status/scale/logs/exec/auth/dry-run。
- `test_scheduler.py`：资源/标签/污点/亲和/反亲和/hostPort/抢占。
- `test_workloads.py`：Deployment/RS/STS/DS/Job/CronJob/HPA。
- `test_kubelet.py`：运行/探针/重启/Job/卷/日志/exec/QoS。
- `test_network.py`：Service/Endpoint/DNS/NodePort/NetworkPolicy/Ingress。
- `test_storage.py`：动态/静态/WFFC/finalizer。
- `test_governance.py`：Namespace/GC/Quota/PDB/RBAC/SA/NodeLifecycle/CRD。
- `test_cli.py`：CLI 子进程。
- `test_coordination.py`：Leader election。
- `test_operator.py`：Website Operator。
- `test_regressions.py`：审计发现的边界/回归行为（删除时间戳、QoS、抢占、PDB、HPA、扩展资源等）。
- `test_server_side_apply.py`：managedFields、冲突、force、省略字段删除、dry-run。
- `test_watch_semantics.py`：bookmark、`resourceVersion=0` 初始快照、selector 快照与超时。
- `test_store.py`：410/too old、慢消费者/事件溢出、watch 历史窗口。
- `test_api.py`：HTTP watch、selector 事件合成、分页、子资源。

命名尽量描述行为，不写 `test_1`。

## 39.6 断言状态而不是实现

优先：

```python
assert deployment["status"]["availableReplicas"] == 3
```

而不是：

```python
assert controller.internal_counter == 3
```

原因：

- 内部实现会变。
- 状态是用户可见契约。

但也要测试错误路径：

```python
with pytest.raises(ApiError) as exc:
    cluster.apply(invalid_pod)
assert "exceeds limit" in exc.value.message
```

## 39.7 API 契约测试

- Discovery 包含资源。
- 创建返回 201 + 服务器字段。
- 重复创建 409。
- 更新冲突 409。
- 删除 finalizer 返回 deletionTimestamp。
- watch 事件类型/对象。
- status 子资源不增加 generation。
- scale 映射 replicas。
- 401/403 区分。
- dry-run 不落库。

这些测试保证 MiniK8s 不只是“能跑”，而是符合 Kubernetes 语义。

## 39.8 E2E 示例

`examples/run_all.py`：

- 每个场景独立集群。
- 通过 HTTP API apply YAML。
- 等待状态。
- 断言业务结果（DNS、Endpoints、PDB、HPA、Operator）。
- 输出 PASS/FAIL + 耗时。

18 个场景覆盖：

1. hello Deployment/Service/DNS。
2. 滚动更新。
3. StatefulSet/Headless/PVC。
4. DaemonSet 双节点。
5. Job/CronJob。
6. ConfigMap/Secret。
7. 动态存储。
8. Ingress。
9. NetworkPolicy。
10. RBAC/SA。
11. Quota/LimitRange。
12. HPA。
13. PDB。
14. CRD/Operator。
15. 探针。
16. GPU 扩展资源。
17. 拓扑分布约束（跨节点 maxSkew）。
18. Gateway API（GatewayClass/Gateway/HTTPRoute + L7 路由）。

运行：

```bash
python examples/run_all.py
python examples/run_all.py --only 03,14
```

## 39.9 文档测试

`check_docs.py` 检查：

- 章节编号连续。
- README TOC 链接存在。
- 内部 Markdown 链接存在。
- 代码块闭合。
- YAML 代码块可解析。
- 计数（章节/测试/示例）与 README 一致。
- 裸章节引用（“第 N 章”）指向存在的章节，链接标签与目标章号一致。
- 没有未完成的占位段落。
- 每个章节有小结/延伸阅读。

这防止“代码改了文档没改”。

## 39.10 反模式

**只测 happy path**：错误路径（冲突、配额、权限、探针失败）才是生产事故来源。

**测试实现细节**：重构就挂。

**固定 sleep**：慢且不稳定。

**共享集群**：测试互相污染，顺序依赖。

**断言过于宽松**：`assert result` 不能验证业务语义。

**没有清理**：线程/端口泄漏，后续测试挂。

**只跑一遍**：并发/时序 bug 需要重复运行。

## 39.11 并发与时序

MiniK8s 是多线程系统：

- store 用 RLock。
- 控制器/调度器/kubelet 并发写。
- 测试要等待最终一致，不假设立即完成。

已覆盖：

- store 并发创建。
- watch 重放。
- 滚动更新中间状态。
- PDB/配额竞争。

可以进一步（当前尚未系统覆盖）：

- 409 lost-update 后的控制器重试（store 冲突本身有测试，控制器重试没有）。
- watch 410 后的客户端重连/重新 List 流程（服务端 ERROR 事件已覆盖）。
- 控制器抛异常/依赖 API 500 时的退避与恢复。
- 随机延迟注入、模拟 watch 断开。
- 模糊测试 selector/schema。

这些正是“测试全绿但生产会挂”的高发区；第 40 章会说明哪些差距无法靠单元测试弥补。

## 39.12 混沌与故障注入

生产级控制器需要验证：

- API Server 暂时不可用。
- watch 断线重连。
- etcd 慢/leader 切换。
- 节点失联。
- 镜像拉取失败。
- 探针抖动。
- 配额边界。

MiniK8s 可以扩展：

- 在 store 注入延迟/错误。
- 停止/重启 kubelet。
- 删除节点。
- 伪造 metrics 缺失。
- 限制 watch 队列。

## 39.13 CI 建议

```bash
python -m pytest
python examples/run_all.py
python check_docs.py
```

矩阵：

- Python 3.10/3.12/3.14。
- Windows/Linux/macOS。
- 重复运行（flaky 检测）。

缓存：

- pip 依赖。
- pytest 缓存（可选）。

## 39.14 如何新增一个功能

1. 写失败的测试（API/控制器行为）。
2. 实现最小代码。
3. 跑单测。
4. 加示例 YAML。
5. 跑 `examples/run_all.py --only`。
6. 更新文档/计数。
7. 跑 `check_docs.py`。

## 39.15 集群级验证：conformance、envtest、kwok 与压测

MiniK8s 的测试解决“控制面契约”，真实平台还需要更外层的验证。把它们放进一个测试矩阵，才知道每一层能抓到什么。

### 39.15.1 测试层级矩阵

| 层级 | 工具 | 验证什么 | 不验证什么 | 成本 |
|---|---|---|---|---|
| 单元 | pytest/Go test | 纯函数、selector、算法 | 组件协作 | 秒级 |
| 组件集成 | envtest（controller-runtime） | 控制器 + 真实 API Server/etcd | kubelet/CNI/CSI | 分钟级 |
| 本地集群 | kind/k3d/minikube | 真实 API/调度/kubelet/CNI | 云 LB/托管服务/规模 | 分钟级 |
| 一致性 | Sonobuoy conformance | 发行版是否符合 Kubernetes API 行为 | 你的业务/性能 | 小时级 |
| 端到端 | 业务 E2E | 用户路径、依赖、数据 | 规模/故障 | 十分钟级 |
| 规模 | kube-burner/clusterloader2/kwok | API/调度/etcd 拐点 | 真实业务语义 | 小时级 |
| 混沌 | Chaos Mesh/Litmus/自研 | 故障恢复、告警、runbook | 功能正确性 | 小时级 |
| 压测 | k6/Locust/ghz | 应用吞吐/延迟 | 控制面规模 | 分钟级 |

关键原则：**每一层只回答它能回答的问题**。用 kind 测一致性、用 conformance 测业务、用压测测故障恢复，都会得到错误结论。

### 39.15.2 envtest：控制器的最快真实反馈

envtest 启动真实的 kube-apiserver + etcd（不启动 kubelet/controller-manager），让控制器测试使用真实 API 语义：

```go
testEnv = &envtest.Environment{
    CRDDirectoryPaths: []string{filepath.Join("..", "config", "crd", "bases")},
}
cfg, err := testEnv.Start()
```

适合：

- CRD 的 schema/默认值/validation。
- 控制器与 API Server 的交互（status、finalizer、watch）。
- Admission webhook（需要额外配置证书/服务）。

不适合：

- Pod 真正运行、镜像拉取、CNI/CSI。
- 调度器行为（envtest 不启动 scheduler）。

Python 生态没有完全等价的 envtest；MiniK8s 的 `MiniCluster` 是“进程内 API + 控制器 + 模拟 kubelet”，填补的是同一层，但语义是自定义的。真实控制器建议用 envtest + kind 组合。

### 39.15.3 kind/k3d：真实数据面的最小闭环

kind 用容器模拟节点，启动真实 kubelet/containerd/CNI。它能抓到：

- 镜像拉取、entrypoint、探针、优雅终止。
- Service/DNS/Ingress（需要安装控制器）。
- PVC/StorageClass（本地路径 provisioner）。
- RBAC/PSA/NetworkPolicy（取决于 CNI）。

它不能代表：

- 云 LB/云盘/跨 AZ。
- 大规模调度/etcd 性能。
- 托管控制面的升级/故障行为。

CI 中常见的两层：

```text
PR: 单元 + envtest + 少量 kind E2E
Nightly: 全量 kind E2E + conformance + 混沌
```

### 39.15.4 Sonobuoy 与一致性认证

Sonobuoy 在集群中跑 Kubernetes conformance 测试：

```bash
sonobuoy run --mode=certified-conformance
sonobuoy status
sonobuoy retrieve
```

用途：

- 发行版/平台通过 CNCF Kubernetes Conformance。
- 升级后验证 API 行为没有回归。
- 对比不同发行版的差异。

注意：

- conformance 测的是 Kubernetes API 行为，不是你的应用。
- 它需要较长时间和足够资源；不适合每个 PR。
- 托管服务的控制面可能限制某些测试；以厂商支持为准。

### 39.15.5 kwok：大规模调度/控制面模拟

kwok（Kubernetes WithOut Kubelet）用假节点/假 Pod 模拟大规模集群：

- 快速创建数千 Node/Pod，不运行真实容器。
- 测调度器吞吐、控制器行为、API Server 压力。
- 适合容量规划和回归，不适合数据面验证。

与 MiniK8s 的区别：

- kwok 使用真实 kube-apiserver/etcd/scheduler/controller-manager，只替换 kubelet。
- MiniK8s 替换整个控制面实现。
- 前者验证“真实组件在规模下的行为”，后者验证“机制是否被正确实现”。

### 39.15.6 kube-burner / clusterloader2：规模与拐点

压测要分层：

1. **API 层**：创建/更新/删除对象的 QPS 与延迟。
2. **调度层**：Pending→Running 的时间、调度吞吐。
3. **控制面**：apiserver/etcd 的 CPU、内存、fsync、watch 延迟。
4. **数据面**：Pod 启动、网络、存储。

kube-burner 的配置大致是“对象模板 + 数量 + 并发 + 测量”：

```yaml
jobs:
  - name: pods
    jobType: create
    jobIterations: 1000
    qps: 50
    burst: 100
    objects:
      - kind: Pod
        apiVersion: v1
        namespaced: true
        labelSelector: {app: load}
```

判断拐点：

- API p99 超过预算。
- etcd fsync 延迟持续上升。
- 调度队列积压不下降。
- 控制器重试/冲突率上升。
- 节点资源碎片导致大量 Pending。

不要只看“创建了多少 Pod”；要看“在多长时间内、以多少错误/重试、对控制面造成多大压力”。

### 39.15.7 混沌与故障注入

混沌实验要有假设、范围、止损：

```text
假设：杀掉一个副本，SLO 不受影响
范围：namespace=shop，label app=orders
注入：随机删除 1 个 Pod，持续 5 分钟
止损：错误率 > 5% 或 P99 > 1s 立即停止
验证：告警触发、PDB 生效、副本恢复、无数据丢失
```

常见注入：

- Pod kill/container kill。
- 节点 drain/reboot/网络分区。
- DNS 故障、延迟/丢包。
- 磁盘 I/O 延迟/写满。
- 时钟偏移。
- 控制面 API 限流/不可用。

不要在无告警/无回滚的生产环境“试试看”。混沌工程的价值是验证**恢复机制**，不是制造事故。

### 39.15.8 测试数据与清理

- 每个测试用独立 namespace，按 label 清理。
- 避免共享集群上的资源名冲突。
- 测试后检查残留 CRD/PV/Namespace（finalizer 可能卡住）。
- 对云资源（LB/盘/IP）做成本标签与自动清理。
- 失败时保留诊断包（events、logs、describe、metrics 快照）。

## 本章小结

- 测试金字塔：单元 + 集成 + API + CLI + E2E + 文档。
- 集成测试用真实组件但无外部依赖，是 MiniK8s 的核心验证手段。
- 确定性（假时钟、模拟运行时、wait_until）让测试稳定。
- 断言用户可见状态，覆盖错误路径。
- 文档检查防止代码与教程漂移。
- 集群级验证要分层：envtest/kind/conformance/kwok/压测/混沌，每层只回答它能回答的问题。

## 延伸阅读

- [第 34 章 MiniK8s 总体设计](34-minik8s-design.md)
- [第 40 章 与真实 Kubernetes 的差距](40-gap-and-replacement.md)
- [第 31 章 故障排查方法论](31-troubleshooting.md)
- [附录 J 自测题库与实验手册](../appendices/J-self-assessment-labs.md)：实验与结业项目。
