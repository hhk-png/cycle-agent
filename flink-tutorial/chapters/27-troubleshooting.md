# 第27章 故障排查手册

## 27.1 先建立时间线

排障最忌“看到一个指标就调参数”。先回答：

1. 什么时候开始？
2. 之前发生了什么变更（代码、配置、流量、依赖）？
3. 影响范围（所有作业/单作业/单 subtask）？
4. 是延迟、吞吐、正确性还是可用性问题？
5. 最近是否重启/恢复/扩缩容？

把时间线和变更记录对齐，往往比看指标更快定位。

```text
T0 发布新版本
T1 30 分钟后 checkpoint 开始变慢
T2 延迟上升，Kafka lag 增长
T3 作业重启
→ 根因很可能是新版本状态变大/序列化变慢
```

## 27.2 作业起不来

检查：

- JobManager/TaskManager 日志是否有异常；
- 资源是否足够（slot、CPU、内存）；
- 镜像/依赖是否可拉取；
- 配置项是否被识别（版本差异）；
- source/sink 连接是否可达；
- 权限/凭证；
- savepoint 是否兼容；
- 端口/网络策略。

常见错误：

- `NoResourceAvailableException`：slot 不足；
- `ClassNotFoundException`：依赖缺失/冲突；
- `NoSuchMethodError`：版本不匹配；
- RPC 超时：网络/DNS；
- `InvalidProgramException`：拓扑/序列化问题。

```bash
# 看 JobManager 日志
kubectl logs deploy/flink-jobmanager -n realtime

# 看 Pod 事件
kubectl describe pod flink-taskmanager-0 -n realtime

# 看资源
kubectl top pod -n realtime
```

### 启动失败的分层排查

```text
Pod 是否 Running？
├─ 否 → 镜像/资源/调度/权限
└─ 是
   ├─ JM 是否启动？→ 日志/配置/端口
   ├─ TM 是否注册？→ 网络/DNS/RBAC
   ├─ 作业是否提交？→ jar/依赖/main
   └─ 是否恢复失败？→ savepoint/uid/schema
```

## 27.3 Checkpoint 失败

看 Checkpoint 页面：

- 失败原因（异常栈）；
- duration/alignment/size；
- 哪个 subtask 失败；
- 存储是否可写；
- 是否超时；
- 反压是否严重。

常见原因：

| 现象 | 可能原因 |
|---|---|
| 超时 | 状态大、反压、存储慢 |
| 对齐时间长 | 反压、慢输入 |
| 上传失败 | S3 权限/限流/网络 |
| 序列化失败 | 状态对象不可序列化/类型变更 |
| 某个 subtask 总失败 | 数据倾斜/OOM/磁盘满 |
| checkpoint 越来越慢 | 状态膨胀、RocksDB compaction |

```bash
# 触发 savepoint（验证状态是否可快照）
flink savepoint <job-id> s3://bucket/savepoints

# 看 checkpoint 统计
curl http://jm:8081/jobs/<job-id>/checkpoints
```

排查顺序：

1. 看失败原因和 subtask；
2. 看反压和 GC；
3. 看状态大小和增长趋势；
4. 看存储权限/带宽；
5. 看磁盘（RocksDB 本地目录）；
6. 最后才调 timeout/间隔。

## 27.4 反压与延迟

排查顺序：

1. Web UI 找反压最高的算子；
2. 看该算子 busy、records in/out、state size；
3. 看下游 sink 是否慢；
4. 看 key 倾斜；
5. 看 GC/CPU/磁盘；
6. 看外部系统限流。

处理：

- 优化 sink 批量/并发；
- 解决热点 key；
- 增加并行度（非热点）；
- 调整 buffer timeout；
- 异步化外部调用；
- 降低 checkpoint 频率/大小。

```text
反压 HIGH 的算子 → 它的下游一定慢？不一定
要看它自己的 busy：如果 busy 高，是自身慢；如果 idle 高，是下游慢
```

## 27.5 OOM 与内存

区分：

- JVM heap OOM：堆内状态、缓存、用户对象；
- direct memory OOM：Netty/RocksDB；
- 容器 OOMKill：总内存超限；
- RocksDB 本地磁盘满。

处理：

- 看 GC 日志和 heap dump；
- 检查状态大小；
- 降低缓存；
- 调整内存模型；
- 用 RocksDB 替代堆内大状态；
- 加磁盘/清理临时文件。

```bash
# 看容器是否被 OOMKill
kubectl describe pod <pod> | grep -i oom

# 生成 heap dump（需提前配置）
jmap -dump:format=b,file=/tmp/heap.hprof <pid>
```

### 内存问题的常见误判

- 堆没满但容器被杀 → 看堆外/网络/RocksDB；
- GC 频繁但堆不大 → 看对象分配速率/缓存；
- RocksDB 内存超限 → 看 managed memory/block cache；
- 磁盘满 → 看本地 rocksdb 目录/日志。

## 27.6 Watermark 不推进

现象：窗口不触发、join 无输出、数据新鲜度指标差。

原因：

- 某个 Kafka 分区无数据且未开 idleness；
- source 并行度 > 分区数，空闲 subtask 拖住最小值；
- 自定义 source 没生成 watermark；
- 某条流没有 watermark；
- 算子链中 watermark 被吞掉；
- 时间戳字段解析错误（全是 0/负值）。

```text
排查：
1. 看每个 source subtask 的 currentOutputWatermark
2. 看每个算子输入的 currentInputWatermark
3. 找最小的那个输入
4. 看它为什么不动（无数据/解析错误/idle）
```

## 27.7 数据正确性

- 重复：sink 非幂等、checkpoint 恢复重放；
- 丢失：source offset 未进 checkpoint、sink 异步未 flush；
- 乱序：watermark 太激进、窗口不允许迟到；
- 结果抖动：changelog 下游不支持更新；
- 实时/离线对不上：口径、时区、迟到处理不同。

正确性问题往往不能靠重启解决，要回到数据链路和语义。

```text
对账步骤：
1. 抽样比对源/目标主键
2. 统计行数/金额/去重数
3. 检查时间范围/时区
4. 检查是否有重复/丢失
5. 回溯 checkpoint/offset
```

## 27.8 作业频繁重启

- 代码异常（空指针、解析失败）；
- 外部系统超时；
- checkpoint 持续失败；
- OOM；
- 磁盘满；
- 序列化不兼容；
- 资源被抢占。

看 restart 次数、异常栈、失败 task、最近变更。不要用无限重启掩盖 bug。

```yaml
# 避免无限重启
restart-strategy.type: failure-rate
restart-strategy.failure-rate.max-failures-per-interval: 3
restart-strategy.failure-rate.failure-rate-interval: 10min
```

## 27.9 性能突然下降

- 流量突增/倾斜；
- 状态膨胀；
- 外部系统变慢；
- checkpoint 变大；
- 网络/磁盘故障；
- 版本升级回归；
- GC 参数变化；
- 资源被其他作业挤占。

```text
对比基线：
- 吞吐/延迟
- checkpoint 时长/大小
- 状态大小
- GC/CPU/磁盘
- Kafka lag
```

如果需要在预发环境复现瓶颈，按[附录 J](../appendix/J-performance-engineering.md) 的负载模型、指标口径和故障注入矩阵设计实验。

## 27.10 SQL 作业特有问题

- 计划退化：join 从 interval 变成 regular；
- 状态 TTL 配置错误；
- changelog 模式与 sink 不匹配；
- 物化表刷新失败；
- UDF 异常/超时；
- Catalog/权限问题。

```sql
EXPLAIN SELECT ...;                 -- 看计划
SHOW CREATE TABLE t;                -- 看 DDL
SELECT * FROM t LIMIT 10;           -- 用小结果验证表/字段（替换成真实表名）
```

## 27.11 MiniFlink 排障

MiniFlink 是单进程，问题通常是 Python 异常：

- `JobExecutionError` 会带算子名和原始异常；
- 检查 `execution.metrics()["tasks"]` 的 records/watermark；
- 检查 `side_outputs`；
- 检查 checkpoint 的 task states；
- 用 `env.explain()` 看拓扑；
- 用 pytest 单测复现。

```python
try:
    env.execute()
except JobExecutionError as e:
    print(e)
    print(env.last_execution.metrics())
```

它没有 JVM 日志、线程 dump、网络指标，排障范围有限。

## 27.12 排障工具箱

```bash
# 作业列表/详情
flink list
flink info <jar>
curl http://jm:8081/jobs/overview
curl http://jm:8081/jobs/<job-id>

# 反压
curl http://jm:8081/jobs/<job-id>/vertices/<vertex-id>/backpressure

# checkpoint
curl http://jm:8081/jobs/<job-id>/checkpoints

# savepoint
flink savepoint <job-id> <dir>
flink stop --savepointPath <dir> <job-id>

# 指标
curl http://tm:9249/metrics
```

## 27.13 诊断决策树与故障演练

真实排障不是“把所有指标看一遍”，而是用最少的信息把问题缩小到一类。下面给出五棵决策树和一张演练矩阵，可以直接放进 on-call runbook。

### 27.13.1 作业起不来

```text
提交失败？
├─ 客户端报错 → 依赖/参数/权限/目标集群
└─ 提交成功但一直 INITIALIZING
   ├─ 没有可用 slot → 资源/队列/slot 配置
   ├─ TaskManager 不注册 → 网络/DNS/RBAC/镜像
   ├─ 恢复失败 → savepoint/uid/schema/状态存储
   └─ 用户代码初始化异常 → main/open/静态初始化
```

先看 **JobManager 日志** 和 **Pod/容器事件**，再看 Web UI 的 `Exceptions`。不要一上来就改并行度。

### 27.13.2 Checkpoint 超时

```text
duration 大
├─ alignment 大
│  ├─ 反压 → 找最慢算子/sink
│  └─ 空闲输入 → idleness/watermark
├─ snapshot 大
│  ├─ 状态大 → TTL/窗口/join 设计
│  └─ 序列化慢 → 类型/对象/后端
├─ upload 慢
│  ├─ 对象存储限流/带宽 → 换 bucket/错峰
│  └─ 全量快照 → 增量 checkpoint
└─ 固定某 subtask 失败
   ├─ 倾斜 → 看 state size/records
   ├─ 磁盘满 → 本地 RocksDB 目录
   └─ OOM → 堆/堆外/容器
```

先拆 duration，再决定是“减状态”“换存储”还是“治反压”。调大 timeout 只能推迟失败。

### 27.13.3 延迟高/反压

```text
端到端延迟高
├─ source 没数据/watermark 不推进 → 时间语义问题
├─ 某算子 busy 100% → 自身 CPU/状态/序列化
├─ 某算子 idle 但下游 HIGH → 下游慢
├─ 所有 subtask 不均 → 热点 key/倾斜
└─ 周期性尖刺 → checkpoint/GC/外部系统
```

反压是“下游慢”的症状。先用 Web UI 找反压最高的算子，再看它的下游、busy、state、GC 和外部调用。

### 27.13.4 OOM/容器被杀

```text
容器 OOMKilled
├─ 堆满 → 堆内状态/缓存/大对象
├─ 堆没满 → 堆外/网络/RocksDB/元空间
├─ 磁盘满 → RocksDB 本地目录/日志
└─ 节点压力 → 其他 Pod/内核/限制
```

不要只看 `-Xmx`。Flink 的内存模型包括 heap、managed、network、metaspace、overhead；容器限制要大于这些之和。

### 27.13.5 数据重复/丢失

```text
重复
├─ sink 非幂等 + 恢复重放 → 幂等键/事务
├─ source offset 未进 checkpoint → 修 source
├─ 下游重复消费 → 消费位点/去重
└─ 外部副作用重试 → request_id
丢失
├─ watermark 太激进 → 迟到侧输出/增容忍
├─ sink 异步未 flush → 提交协议
├─ source 漏分片 → 分片分配/恢复
└─ 过滤条件/反序列化静默丢 → 死信/计数对账
```

正确性问题不要靠“重启试试”。先确定是 at-least-once 重复、watermark 丢迟到，还是 sink 提交边界。

### 27.13.6 排障时不要做的五件事

1. 不要在没有时间线的情况下改参数；
2. 不要一次改多个变量；
3. 不要把 `timeout` 调大当作修复；
4. 不要用 `read_uncommitted` 掩盖重复；
5. 不要在生产直接删 checkpoint/savepoint 目录。

### 27.13.7 故障演练矩阵

| 演练 | 注入方式 | 期望 |
|---|---|---|
| TaskManager 崩溃 | kill pod/进程 | 自动重启，RPO/RTO 达标 |
| JobManager 崩溃 | kill JM/leader | HA 接管，作业恢复 |
| Kafka 抖动 | 限流/断网 | 反压/重试，不丢不重 |
| sink 限流 | 降速/返回 5xx | 退避，checkpoint 不超时 |
| 大状态恢复 | 从 savepoint 恢复 | 恢复时间在 SLO 内 |
| 磁盘满 | 填满本地盘 | 告警/清理，不静默失败 |
| 坏数据 | 注入 schema 错误 | 死信/隔离，不拖垮作业 |
| 扩缩容 | 改并行度恢复 | 状态正确重分配 |

演练要有**观测指标、判定标准、回滚步骤和复盘记录**。没有演练的“高可用”只是配置，不是能力。

排障时如果能拿到 REST 端点返回的状态、异常和 checkpoint 信息，定位会快很多；对应的接口地图、异步 savepoint 轮询和发布回滚 Runbook 见[附录 M](../appendix/M-rest-operations.md)。如果问题由变更引起，发布前门禁、标准升级/回滚流程和六个常见失败案例见[附录 P](../appendix/P-release-change-management.md)。

## 本章小结

排障的关键是“时间线 + 指标 + 变更 + 根因”，而不是背错误码。先判断是资源、反压、状态、时间还是正确性问题，再进入对应工具。

下一章进入 MiniFlink 的实现：把前面讲过的运行时、时间、状态、checkpoint 落到可运行的代码。
