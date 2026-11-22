# 第 18 章 控制器模式与 Informer/Workqueue

## 18.1 控制循环的本质

控制器是一个无限循环：

```text
for {
    desired := read spec
    actual := observe cluster
    if desired != actual {
        take action
    }
}
```

它必须是：

- **幂等**：重复执行不会产生重复资源。
- **水平触发（level-triggered）**：基于当前状态，而不是只响应事件。
- **可重入**：任意时刻重启都能继续。
- **可观测**：记录事件、指标、日志。

Kubernetes 的控制器大多不是“收到创建事件就创建 Pod”，而是“发现 Pod 数量不足就补”。这样即使漏掉事件，下一轮也会修正。

## 18.2 从事件到调谐

真实控制器通常不直接在事件回调里写业务逻辑，而是：

```text
Informer Event
   → 把对象 key 放入 Workqueue
   → Worker 取出 key
   → 从缓存读取最新对象
   → Reconcile
   → 成功则 Forget，失败则重新入队（限速）
```

为什么？

- 事件可能重复/乱序。
- 多个事件可能针对同一对象，合并处理。
- API 写失败需要重试。
- 限速避免打爆 API Server。

## 18.3 List/Watch 与 Reflector

Informer 的底层：

1. **List**：获取全量对象 + `resourceVersion`。
2. **Watch**：从该版本开始接收 ADDED/MODIFIED/DELETED。
3. **DeltaFIFO**：保存对象变更。
4. **Indexer**：本地缓存，支持按 namespace/name/label 索引。
5. **EventHandler**：把 key 放入 workqueue。

watch 断开：

- 从最后 resourceVersion 重连。
- 如果版本被 etcd compaction，重新 List。
- `BOOKMARK` 事件推进版本，减少资源消耗。

MiniK8s 的 `ResourceStore.watch` 实现了简化版：保留事件历史、按 GVR/namespace 过滤、从 resourceVersion 重放。API Server 的 `/watch` 流式输出 JSON lines。

## 18.4 Workqueue

Kubernetes 的 workqueue 特性：

- **去重**：同一个 key 在队列中只出现一次。
- **延迟**：`AddAfter` 延迟入队。
- **限速**：指数退避（ItemExponentialFailureRateLimiter）。
- **并发**：多个 worker 并行处理不同 key。
- **公平**：多个队列/优先级。

典型重试：

```text
失败 1 次：5ms
失败 2 次：10ms
失败 3 次：20ms
...
最大：1000s
```

成功处理后调用 `Forget` 重置退避。

## 18.5 级联与 ownerReference

控制器创建下级对象时必须设置 ownerReference：

```python
owner = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "name": deployment["metadata"]["name"],
    "uid": deployment["metadata"]["uid"],
    "controller": True,
    "blockOwnerDeletion": True,
}
```

这样：

- GC 能级联删除。
- 控制器能识别“我的”对象。
- 避免误认领别人的 Pod。

注意 uid 必须匹配；同名重建的对象是不同 uid，旧 ownerReference 不会错误关联。

## 18.6 finalizer 与清理

控制器需要清理外部资源时：

1. 创建对象时添加 finalizer。
2. 删除时检测 `deletionTimestamp`。
3. 清理外部资源（负载均衡器、DNS、云盘、数据库）。
4. 移除 finalizer。
5. 对象被 GC 删除。

常见错误：

- 清理逻辑不幂等。
- 外部 API 超时导致 finalizer 永久阻塞。
- 没有超时/告警。
- 删除对象前忘记移除 finalizer。

## 18.7 事件与日志

事件（Event）是控制器的用户可见反馈：

```bash
kubectl describe pod web
kubectl get events --sort-by=.lastTimestamp
```

事件不是日志：

- 事件有 TTL（默认 1 小时）。
- 事件可能被聚合/丢弃。
- 事件用于“发生了什么”，日志用于“为什么”。

控制器应该：

- 对关键状态变化发事件（Normal/Warning）。
- 避免高频重复事件（会打爆 etcd）。
- 日志包含对象 key、错误、重试次数。
- 暴露指标（reconcile 次数、错误数、延迟）。

## 18.8 领导选举

Controller Manager/Scheduler 多副本部署时，同一时刻只能有一个 leader 执行调谐，否则会重复创建资源。

Kubernetes 用 Lease 对象：

```yaml
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: kube-controller-manager
  namespace: kube-system
spec:
  holderIdentity: controller-0
  leaseDurationSeconds: 15
  renewTime: "..."
  leaseTransitions: 3
```

候选者：

1. 读取 Lease。
2. 如果为空/过期，写入自己的身份。
3. 如果自己是 holder，续约。
4. 否则等待。

MiniK8s 的 `mini_k8s/coordination/leader.py` 实现了简化版，测试 `test_failover_after_expiry_and_transition` 验证过期接管。

真实实现还需要：

- 时钟偏差处理。
- 观察 leader 变化并停止工作。
- 释放租约。
- 与工作队列的协调。

## 18.9 控制器设计原则

### 单一职责

一个控制器负责一种资源/关系。不要让一个控制器同时管理 Deployment、Service、Ingress。

### 声明式

不要依赖“事件发生了几次”，只依赖当前状态。

### 乐观并发

写冲突时重新读取、重试，不要覆盖。

### 分级状态

status 反映观察结果；不要用 status 驱动 spec。

### 可测试

把调谐逻辑与 API 客户端解耦，使用 fake client/内存 store 测试。MiniK8s 的控制器直接操作 `ResourceStore`，测试无需 HTTP。

### 可观测

- 事件。
- 指标。
- 结构化日志。
- 健康检查。

## 18.10 Operator 模式

Operator = CRD + 控制器 + 领域知识：

```text
用户创建 Website CR
  → WebsiteController 创建 Deployment/Service
  → 观察 status
  → 更新 Website.status
```

Operator 适合：

- 有状态服务（数据库、消息队列）。
- 需要领域编排（备份、升级、故障切换）。
- 复杂应用（Prometheus、Cert Manager）。

不适合：

- 简单 CRUD。
- 没有领域知识的包装。
- 可以用 Helm/Kustomize 解决的部署。

第 19 章用 MiniK8s 实现一个 Website Operator。

## 18.11 控制器反模式

**在事件回调里做长任务**：阻塞 informer，导致事件堆积。应放入 workqueue。

**忽略 AlreadyExists/NotFound**：重试时这些是正常状态，不是错误。

**用时间戳判断状态**：时钟偏差/重启会出错，应使用 resourceVersion/observedGeneration。

**无限重试不可恢复错误**：例如无效配置，应记录事件并停止重试或降级。

**每次调谐都全量 List**：大规模下昂贵，应使用缓存/索引。MiniK8s 为了简单每轮 List，教学可接受。

**不设置 ownerReference**：GC 无法清理，孤儿资源堆积。

**没有 leader election 就跑多副本**：重复创建资源。

## 18.12 MiniK8s 的控制器运行时

`mini_k8s/controllers/base.py`：

```python
class Controller:
    def start(self):
        thread = threading.Thread(target=self._run)
        thread.start()

    def _run(self):
        while not stop:
            try:
                self.reconcile()
            except Exception as exc:
                self.event("Warning", f"{self.name}Error", str(exc))
            stop.wait(interval)
```

每个控制器：

- 独立线程。
- 固定间隔调谐。
- 异常不退出循环。
- 写事件。

这不是生产级运行时（没有 workqueue、限速、缓存），但保留了“调谐循环 + 事件 + 幂等”的核心。

## 18.13 Informer 源码级机制

如果只看概念，容易把 informer 当成“List+Watch 的封装”。它的可靠性来自几个具体机制：

### Reflector

`client-go/tools/cache/reflector.go`：

1. 调用 `ListWatch.List` 获取全量对象和 `resourceVersion`。
2. 把对象放入 DeltaFIFO（`Sync`/`Replaced`）。
3. 从该版本 `Watch`，把 `ADDED/MODIFIED/DELETED/BOOKMARK` 转成 Delta。
4. watch 断开/过期时重新 List；`watchErrorHandler` 记录错误并退避。
5. 用 `resourceVersion` 衔接，避免漏事件。

### DeltaFIFO

`DeltaFIFO` 保存“对象 key → Delta 列表”，每个 Delta 记录一次变更类型（Added/Updated/Deleted/Replaced/Sync）与对象快照：

- 同一个 key 的多次变更**不会**在这里合并；`Pop` 会依次取出每个 Delta，`sharedIndexInformer` 逐条更新 Indexer 并分发事件。
- 真正的去重发生在后面的 workqueue：多个事件最终只产生一个 key，控制器按最新状态调谐。
- 处理失败时把 key 重新入队，不会丢；但业务对象状态以最后一次 List/watch 为准。
- 队列的 key 相同不代表 Delta 相同；如果控制器依赖“中间状态”，需要自己记录，不能假设 informer 只给终态。

### Indexer

本地缓存，支持：

- 按 namespace/name 取对象。
- 按 label 索引（`Indexers`）。
- 按自定义索引函数（例如按 owner UID、节点名）查询。
- 读操作不加锁到 API Server，大幅降低延迟。

### SharedInformer 与事件分发

- 多个控制器可以共享同一个 informer，避免重复 List/Watch。
- `EventHandler` 的 `Add/Update/Delete` 通常只把 key 放入 workqueue，不做重活。
- `processorListener` 有缓冲；消费太慢会积压并增加延迟，控制器仍应基于当前状态调谐，不能假设事件不丢。

### Workqueue 的限速

`client-go/util/workqueue`：

- `RateLimitingQueue` 组合 `ItemExponentialFailureRateLimiter` 与 `BucketRateLimiter`。
- 失败重试按 key 指数退避；成功后 `Forget` 重置。
- 队列去重，但**不保证顺序**；控制器不能依赖事件顺序。
- `ShutDown` 要配合 context，避免 goroutine 泄漏。

### 为什么控制器仍然要“水平触发”

即使 informer 保证最终一致，事件也可能：

- 被合并/丢弃（慢消费者）。
- 在控制器重启期间错过。
- 因缓存未同步而读到旧对象。

所以 Reconcile 必须从缓存读取**当前**对象并重新计算，而不是依赖事件内容。这就是“level-triggered”的工程含义。

MiniK8s 用 `ResourceStore.watch` + 每轮 list 模拟，保留了事件历史和重放，但没有 DeltaFIFO/Indexer/限速队列；第 40 章给出替换为 client-go 的路线。

## 本章小结

- 控制器是水平触发的调谐循环，不是事件脚本。
- Informer + workqueue 解决缓存、去重、重试和并发。
- ownerReference/finalizer 管理生命周期，Lease 管理 leader。
- 好的控制器幂等、可观测、可测试、可重入。
- Operator 把领域知识编码进控制器，适合复杂有状态系统。

## 延伸阅读

- [第 19 章 CRD 与 Operator 工程化](19-crd-operator.md)
- [第 28 章 高可用与灾难恢复](28-ha-disaster-recovery.md)
- [第 36 章 调度器与控制器实现](36-scheduler-controllers.md)
- [附录 Q 扩展点与交付物开发实战](../appendices/Q-extension-development.md)：用 client-go/controller-runtime 把本章机制写成可部署的控制器。
