# 附录 M Flink REST API 与作业运维自动化

第 22 章讲指标与可观测性，第 25 章讲 Kubernetes 上的部署，第 27 章讲排障。它们回答的是“怎么看见”和“怎么修”；本附录回答另一个在平台化阶段一定会遇到的问题：

> 当 Flink 作业从“一个人在 Web UI 上点按钮”变成“几十个团队、几百个作业、每天多次发布”时，如何用稳定的接口完成查询、保存点、升级、回滚和审计？

答案不是“会几条 curl”，而是把 Flink 的 REST API 当成**控制面**来设计：明确它的能力边界、异步语义、幂等规则、版本差异和安全模型，再在其上封装内部发布系统。

```text
本附录的位置
  第 2 章    运行时与作业生命周期
  第 8/9 章  checkpoint、savepoint 与恢复
  第 22 章   指标、日志、trace
  第 25 章   Kubernetes Operator 与升级模式
  第 27 章   故障排查
  本附录     REST 控制面、自动化发布、平台 API 与 Runbook
```

## M.1 控制面与数据面

先把两类流量分开：

| 平面 | 内容 | 走哪里 | 典型量级 |
|---|---|---|---|
| 数据面 | 用户记录、watermark、checkpoint 数据 | Source/Sink、网络栈、对象存储 | GB/s～TB/s |
| 控制面 | 作业状态、指标、savepoint 触发、配置 | JobManager REST、Operator CRD、元数据服务 | 每秒几次～几十次 |

REST API 只适合控制面。不要用 REST 逐条发送业务数据，也不要把大文件、SQL 结果集或完整状态通过 REST 搬运。控制面请求应当满足三个条件：

1. **低频**：相对数据流而言，调用次数少几个数量级；
2. **可重试**：失败后能用同一个操作 ID 安全重试；
3. **可审计**：谁、在什么时间、对哪个作业、做了什么、结果如何。

平台化的核心不是“把 Web UI 的按钮翻译成 HTTP”，而是把“期望状态”变成可对账的对象。例如“订单风控作业应该是 8 并行度、运行版本 v42、从 savepoint S 恢复”，这比“调用一次 savepoint 接口”更接近运维的真实需求。

## M.2 版本基线与 2.x 的变化

本教程以 Flink 2.3 为基线。REST API 在 2.0 做过一轮清理，升级自动化脚本时必须知道这些差异：

| 变化 | 影响 | 应对 |
|---|---|---|
| 移除在线 rescale REST 端点 | 不能靠 `PATCH .../rescaling` 动态改并行度 | 用 savepoint + 重新提交，或 Operator autoscaler |
| 取消作业移除 `terminationMode` | `?mode=cancel` 在 2.x 不再是推荐写法 | 直接 `PATCH /jobs/:jobid`，以发行版文档为准 |
| 部分字段统一为 camelCase | 响应解析、请求体字段名可能变化 | 不要凭字段风格猜，按版本做契约测试 |
| Jar 运行请求移除 `programArgs` | 旧的提交脚本会 400 | 改用 `programArgsList` |
| 移除 YARN 专用可变 GET | 旧运维脚本失效 | 改用标准作业接口或 YARN 客户端 |
| `stop` 使用独立端点 | 不能再用取消接口“顺便 stop” | `POST /jobs/:jobid/stop` |

需要特别提醒：**2.x 的 REST 字段并不是“全部 camelCase”**。例如触发 savepoint 的请求体仍使用 `target-directory`、`cancel-job`，而 stop-with-savepoint 的请求体使用 `targetDirectory`、`drain`。这不是笔误，而是历史兼容的结果。写自动化时最可靠的做法是：

- 以所用发行版的 REST API 文档和 Java 请求体类为准；
- 对关键请求做一次真实的集成测试；
- 把请求体字段集中在一个适配层，不要散落在脚本里；
- 升级 Flink 时把 REST 契约测试纳入发布门禁。

```text
经验规则
  能先用 Flink CLI / Operator 完成的操作，不要直接拼 REST。
  REST 适合查询、轻量控制和平台集成；
  复杂生命周期（升级、恢复、回滚）优先交给声明式控制器。
```

## M.3 端点地图

下面按“运维会问的问题”组织端点，而不是按 HTTP 路径字母序。所有路径都相对于 JobManager 的 REST 基址，例如 `http://flink-jm:8081`。

### M.3.1 集群与资源

| 问题 | 端点 | 关键返回 |
|---|---|---|
| 集群活着吗 | `GET /overview` | Flink 版本、TM 数、slot 总数/可用数、作业计数 |
| 有哪些 TaskManager | `GET /taskmanagers` | TM ID、地址、slot 数、资源 |
| 某 TM 的指标 | `GET /taskmanagers/:tm-id/metrics?get=...` | 堆、GC、网络、磁盘 |
| JobManager 配置 | `GET /jobmanager/config` | 当前生效配置（注意脱敏） |
| 集群配置 | `GET /config` | 部分发行版提供，字段随版本变化 |

`GET /overview` 适合做**存活与容量**的第一跳检查，但它不告诉你作业为什么慢。容量问题要继续看 `/taskmanagers` 和作业级指标。

### M.3.2 作业查询

| 问题 | 端点 | 说明 |
|---|---|---|
| 有哪些作业 | `GET /jobs/overview` | 所有作业的摘要，含状态、开始时间、持续时间 |
| 某个作业的详情 | `GET /jobs/:jobid` | 状态、时间戳、任务统计 |
| 执行计划 | `GET /jobs/:jobid/plan` | JSON 形式的算子/边，可用于对比发布前后计划 |
| 异常 | `GET /jobs/:jobid/exceptions?maxExceptions=20` | 根异常与历史异常 |
| 顶点列表 | `GET /jobs/:jobid` | 作业详情已返回 `vertices` 数组；单顶点再走下一行 |
| 单个顶点 | `GET /jobs/:jobid/vertices/:vertexid` | 各 subtask 的输入/输出/状态 |
| 单个 subtask | `GET /jobs/:jobid/vertices/:vertexid/subtasks/:index` | 当前/最近一次执行尝试 |
| 累加器 | `GET /jobs/:jobid/accumulators` | 用户自定义计数器 |

作业 ID 是 32 位十六进制字符串。平台内部不要把它当业务主键：一次发布可能产生新的 job id，真正的稳定标识应该是“部署 ID + 代次（generation）”。

### M.3.3 指标、反压与 checkpoint

| 问题 | 端点 | 备注 |
|---|---|---|
| 作业级指标 | `GET /jobs/:jobid/metrics?get=a,b` | 不传 `get` 会返回大量指标 |
| 顶点指标 | `GET /jobs/:jobid/vertices/:vertexid/metrics?get=...` | 聚合到顶点 |
| 子任务指标 | `GET /jobs/:jobid/vertices/:vertexid/subtasks/:index/metrics?get=...` | 定位倾斜 |
| 子任务聚合 | `GET /jobs/:jobid/vertices/:vertexid/subtasks/metrics?get=...` | 支持 `subtask=1,2` 过滤 |
| 反压 | `GET /jobs/:jobid/vertices/:vertexid/backpressure` | 顶点及所有子任务 |
| checkpoint 历史 | `GET /jobs/:jobid/checkpoints` | 计数、最近成功/失败、大小、耗时 |
| checkpoint 配置 | `GET /jobs/:jobid/checkpoints/config` | 间隔、超时、模式等（版本相关） |

常用指标名：

```text
numRecordsIn / numRecordsOut
numRecordsInPerSecond / numRecordsOutPerSecond
busyTimeMsPerSecond
backPressuredTimeMsPerSecond
idleTimeMsPerSecond
currentInputWatermark
numRestarts
lastCheckpointDuration
lastCheckpointSize
lastCheckpointRestoreTimestamp
```

指标名会随连接器、算子和版本变化。正确的做法不是背一张永久表，而是先请求 `/metrics` 拿到该作业实际暴露的键，再把需要的键写入监控白名单。否则升级后会出现“看板还在，指标全是 NaN”。

反压端点返回的是**采样窗口内的比例/等级**，不是因果证明。它回答“哪个顶点在背压”，不回答“为什么”。根因还要结合：

- 该顶点的 `busyTime` 是否高；
- 下游是否 `backPressured`；
- checkpoint 是否长时间处于 `IN_PROGRESS`；
- Kafka lag、外部系统 RT、GC、磁盘 IO 是否异常。

### M.3.4 快照与生命周期

| 操作 | 端点 | 语义 |
|---|---|---|
| 触发 savepoint | `POST /jobs/:jobid/savepoints` | 异步，返回 `request-id` |
| 查询 savepoint 状态 | `GET /jobs/:jobid/savepoints/:triggerid` | `IN_PROGRESS` / `COMPLETED` / 失败 |
| 停止并做 savepoint | `POST /jobs/:jobid/stop` | 异步，支持 `drain` |
| 取消 | `PATCH /jobs/:jobid` | 不保证保存状态 |
| 提交 Jar | `POST /jars/upload` | multipart 上传 |
| 运行 Jar | `POST /jars/:jarid/run` | 2.x 用 `programArgsList` |
| 删除 Jar | `DELETE /jars/:jarid` | 清理制品 |

一个容易忽略的事实：**savepoint 是异步操作**。`POST` 返回 202 和 `request-id` 只表示“请求已接受”，不表示“savepoint 已完成”。发布系统必须在 `COMPLETED` 且拿到 `location` 之后，才允许进入下一步。

## M.4 异步操作模型

Flink 的 savepoint、stop-with-savepoint 等操作采用“触发 + 轮询”模型：

```text
POST /jobs/:jobid/savepoints
  -> 202 {"request-id": "7d273f5a62eb4730b9dea8e833733c1e"}

GET /jobs/:jobid/savepoints/7d273f5a62eb4730b9dea8e833733c1e
  -> {"status": {"id": "IN_PROGRESS"}}
  -> {"status": {"id": "COMPLETED"},
      "operation": {"location": "s3://bucket/savepoints/savepoint-..."}}
```

工程上要处理好五件事：

1. **超时**：不要无限轮询。按状态大小、存储带宽和 SLO 设置上限，例如 10 分钟；
2. **幂等**：请求体可以带 `triggerId`。同一个 ID 重试不会重复创建 savepoint；这比“失败后盲目重试”安全。幂等窗口受 `rest.async.store-duration` 限制（常见默认 5 分钟），超过窗口后旧结果可能已不可查询；
3. **结果持久化**：`request-id`、`location`、作业 ID、代码版本、并行度要写进发布记录；
4. **失败分类**：`IN_PROGRESS` 超时、`COMPLETED` 但 location 不可读、请求 4xx/5xx 是不同问题；
5. **并发控制**：同一作业同一时间只允许一个 savepoint 操作，避免存储和网络被打满。

`drain` 参数要单独解释：

- `drain=false`：在当前进度做 savepoint，恢复后从该进度继续；
- `drain=true`：先发出 `MAX_WATERMARK`，等待在途数据/计时器处理完，再**在当前位点**做 savepoint。它不是把消费位置“推进到末尾”，但 savepoint 之后新写入的数据不会被消费，恢复后会出现一段空洞；对某些 source，收尾时的位点可能已经接近末尾。

所以 `drain=true` 不是“更安全的停止”，而是“带冲刷的终止”。用于日终结算、批式收尾可以；用于发布暂停通常不合适。

## M.5 查询作业与执行计划

发布前最值得自动化的检查之一，是对比新旧执行计划：

```bash
curl -s "$JM/jobs/$JOB/plan" > plan-new.json
# 在 staging 提交新版本后
curl -s "$JM/jobs/$JOB_NEW/plan" > plan-new.json
```

关注：

- 算子链是否变化；
- join 策略是否从 temporal 变成 regular；
- 是否出现意外的 `Exchange`（网络 shuffle）；
- 并行度、slot 需求是否变化；
- 状态算子（聚合、去重、窗口、join）是否新增或消失。

执行计划不是语义证明，但能快速发现“改了一行 SQL，状态翻倍”这类问题。对比时要把计划归一化：去掉 job id、时间戳、随机算子 ID，再 diff。

`GET /jobs/:jobid/exceptions` 适合做告警聚合的入口，但不要只看第一条异常。Flink 的异常链可能把根因包在 `stackTrace` 里；平台应提取：

- 异常类名；
- 顶层 message；
- 首次/最近发生时间；
- 关联的 task/vertex；
- 重启次数。

## M.6 指标、反压与 checkpoint 诊断

### M.6.1 一套最小诊断顺序

```text
1. GET /jobs/overview                 作业是否 RUNNING？是否频繁重启？
2. GET /jobs/:id                      状态、时间、任务统计
3. GET /jobs/:id/exceptions           最近根异常
4. GET /jobs/:id/checkpoints          checkpoint 是否持续失败/超时
5. GET /jobs/:id/vertices/:v/backpressure
                                      哪个顶点背压
6. 子任务指标                         是否数据倾斜/单点慢
7. TM/JM 指标 + 日志                   GC、磁盘、网络、外部系统
```

这个顺序的价值是“先排除作业级故障，再进入算子级性能”。直接看 CPU 往往会误判，因为反压会把上游也拖慢。

### M.6.2 checkpoint 诊断

`GET /jobs/:jobid/checkpoints` 至少要看：

- `counts.completed`、`counts.failed`、`counts.in_progress`；
- `latest.completed` 的 `duration`、`size`、`trigger_timestamp`；
- 最近失败的原因（通常在 `latest.failed` 或异常端点）；
- checkpoint 间隔与超时的关系。

典型判断：

| 现象 | 可能原因 | 下一步 |
|---|---|---|
| duration 持续接近 timeout | 状态大、存储慢、反压 | 看 size/带宽/对齐时间 |
| size 突然翻倍 | 新增长期状态、join 未设 TTL | 对比执行计划与 state TTL |
| failed 后立刻重试 | 存储权限/配额/网络 | 查 JM/TM 日志与对象存储 |
| 只有部分 subtask 慢 | 数据倾斜 | 子任务指标、key 分布 |
| checkpoint 期间业务延迟尖刺 | 同步快照/背压 | 非对齐 checkpoint、增量/分离式状态 |

### M.6.3 反压诊断

反压是“下游处理不过来，向上游传播”的结果。REST 的 backpressure 端点给的是采样比例，适合定位**第一跳**：

```text
source -> map -> keyBy -> window -> sink
                              ^
                        这里 backpressured 高
```

如果 window 顶点 backpressured 高，而 sink 的 busyTime 也高，根因通常在下游或外部系统；如果 sink 的 busyTime 低但 backpressured 高，可能是网络/序列化/缓冲区问题。继续看：

- `busyTimeMsPerSecond`：真正在计算的时间；
- `backPressuredTimeMsPerSecond`：被下游阻塞的时间；
- `idleTimeMsPerSecond`：没有输入；
- `numRecordsOutPerSecond`：吞吐是否下降；
- Kafka consumer lag / source idle。

## M.7 Savepoint、Stop 与升级

### M.7.1 触发 savepoint

2.x 的请求体字段（以发行版为准）：

```bash
curl -s -X POST "$JM/jobs/$JOB/savepoints" \
  -H 'Content-Type: application/json' \
  -d '{
        "target-directory": "s3://bucket/flink/savepoints",
        "cancel-job": false
      }'
```

返回：

```json
{"request-id": "7d273f5a62eb4730b9dea8e833733c1e"}
```

轮询：

```bash
curl -s "$JM/jobs/$JOB/savepoints/$TRIGGER_ID"
```

注意：

- `target-directory` 是**父目录**，Flink 会在其中创建 `savepoint-<jobid>-<random>`；
- `cancel-job=true` 表示 savepoint 成功后取消作业，适合“停止并保存”；
- 大状态作业的 savepoint 可能持续数分钟到数十分钟，必须纳入发布窗口；
- 2.x 的 ForSt 不支持 canonical savepoint 等能力，使用前核对第 34 章和附录 L。

### M.7.2 stop-with-savepoint

```bash
curl -s -X POST "$JM/jobs/$JOB/stop" \
  -H 'Content-Type: application/json' \
  -d '{"drain": false, "targetDirectory": "s3://bucket/flink/savepoints"}'
```

与“先 savepoint 再 cancel”相比，stop 是一个原子语义的编排：作业在保存点完成后停止。它仍然返回异步操作 ID，要轮询到完成。

选择建议：

| 目标 | 推荐 |
|---|---|
| 发布前保存状态，随后立即用新版本恢复 | savepoint（`cancel-job=true`）或 stop（`drain=false`） |
| 日终/批式收尾，确保计时器触发 | stop（`drain=true`），并接受可能跳过停摆期数据 |
| 只取消，不需要恢复 | cancel，不产生 savepoint |
| 升级失败要快速回滚 | 保留旧 savepoint + 旧镜像/旧 jar |

### M.7.3 一次可回滚的升级

```text
1. 在 staging 用生产 savepoint 恢复新版本，验证 UID/状态/输出
2. 生产触发 savepoint，记录 location 与代码版本
3. 确认 savepoint COMPLETED 且可读
4. 停止旧作业（cancel 或 stop）
5. 用新版本从 savepoint 恢复，指定并行度/资源
6. 观察 checkpoint、反压、业务对账
7. 失败则停止新作业，用旧版本 + 旧 savepoint 回滚
```

关键控制点：

- **不要在 savepoint 未完成时停旧作业**；
- **不要同时改代码、并行度和状态后端**，否则故障时无法判断变量；
- **不要用 `--allowNonRestoredState` 掩盖状态丢失**，除非明确知道丢了什么；
- **保留上一版 savepoint**，不要用“最新覆盖”策略把回滚路径删掉；
- **记录 maxParallelism**，恢复时不要随意改变。

### M.7.4 2.x 没有在线 rescale REST

Flink 2.0 移除了长期不可用的 rescale REST 端点。今天做并行度调整，现实路径是：

1. **savepoint + 重新提交**：最通用，适合手工/平台发布；
2. **Kubernetes Operator 的升级/重启**：通过 CRD 改并行度，Operator 负责 savepoint/恢复；
3. **Operator autoscaler**：基于指标自动调整，但仍是滚动重启，不是原地改并发；
4. **应用内拆分**：把可独立扩缩的部分拆成多个作业。

不要写一个依赖 `/jobs/:jobid/rescaling` 的平台。它在 2.x 上不可用；即使某些旧版本存在，也不是可靠的长期接口。

## M.8 用 Python 标准库写一个最小客户端

下面的客户端只依赖标准库，适合放进运维脚本或作为平台 SDK 的骨架。它演示四件事：统一错误、异步轮询、savepoint 幂等、超时控制。

```python
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class FlinkRestError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class FlinkRest:
    def __init__(self, base_url: str, timeout_s: float = 10.0, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.token = token

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise FlinkRestError(method, path, exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} 连接失败: {exc.reason}") from exc

    def jobs(self) -> list[dict[str, Any]]:
        return self._request("GET", "/jobs/overview")["jobs"]

    @staticmethod
    def _completed_location(status: dict[str, Any]) -> str:
        operation = status.get("operation") or {}
        location = operation.get("location")
        if isinstance(location, str):
            return location
        result = operation.get("result")
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("location", "savepoint"):
                value = result.get(key)
                if isinstance(value, str):
                    return value
        raise RuntimeError(f"操作完成但无法解析 savepoint 位置: {status}")

    def savepoint(
        self,
        job_id: str,
        target_directory: str,
        *,
        cancel_job: bool = False,
        trigger_id: str | None = None,
        timeout_s: float = 600.0,
        poll_s: float = 2.0,
    ) -> str:
        body: dict[str, Any] = {
            "target-directory": target_directory,
            "cancel-job": cancel_job,
        }
        if trigger_id:
            body["triggerId"] = trigger_id
        response = self._request("POST", f"/jobs/{job_id}/savepoints", body)
        request_id = response["request-id"]
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self._request("GET", f"/jobs/{job_id}/savepoints/{request_id}")
            state = status["status"]["id"]
            if state == "COMPLETED":
                return self._completed_location(status)
            if state != "IN_PROGRESS":
                raise RuntimeError(f"savepoint 失败: {json.dumps(status, ensure_ascii=False)}")
            time.sleep(poll_s)
        raise TimeoutError(f"savepoint 超时: job={job_id} trigger={request_id}")

    def stop_with_savepoint(
        self,
        job_id: str,
        target_directory: str,
        *,
        drain: bool = False,
        timeout_s: float = 600.0,
        poll_s: float = 2.0,
    ) -> str:
        response = self._request(
            "POST",
            f"/jobs/{job_id}/stop",
            {"targetDirectory": target_directory, "drain": drain},
        )
        request_id = response["request-id"]
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self._request("GET", f"/jobs/{job_id}/savepoints/{request_id}")
            state = status["status"]["id"]
            if state == "COMPLETED":
                return self._completed_location(status)
            if state != "IN_PROGRESS":
                raise RuntimeError(f"stop 失败: {json.dumps(status, ensure_ascii=False)}")
            time.sleep(poll_s)
        raise TimeoutError(f"stop 超时: job={job_id} trigger={request_id}")
```

使用示例：

```python
client = FlinkRest("http://flink-jm:8081", token="<internal-token>")
for job in client.jobs():
    print(job["jid"], job["name"], job["state"])
```

这段代码刻意没有做自动重试。原因不是重试不重要，而是**重试语义必须由调用方决定**：

- 查询可以安全重试；
- savepoint 必须复用 `triggerId`，否则可能产生多份；
- cancel 对已结束作业返回 409，应该视为“已达到目标状态”；
- 4xx 通常不要重试，5xx/502/503 才考虑退避重试。

如果要把这段代码用于生产，至少还要补：TLS 校验、连接池、超时分层、结构化日志、指标、限流、审计和请求体契约测试。

## M.9 平台化：把 REST 封装成内部 API

一个可维护的流平台，通常不会让业务直接调用 JobManager。它会在 REST 之上再包一层**发布 API**，把 Flink 的作业模型翻译成业务能理解的对象。

### M.9.1 资源模型

```text
Deployment（部署）
  ├── name / namespace / owner
  ├── artifact（jar/sql/image tag）
  ├── flinkVersion
  ├── parallelism / resources
  ├── checkpoint/savepoint policy
  ├── desiredState: RUNNING | SUSPENDED
  └── generation（每次变更 +1）

Operation（操作）
  ├── type: DEPLOY | SAVEPOINT | STOP | ROLLBACK
  ├── deploymentId + generation
  ├── idempotencyKey
  ├── status: PENDING | RUNNING | SUCCEEDED | FAILED
  └── result: jobId / savepointLocation / error
```

不要让业务直接持有 job id。job id 是一次运行的产物，deployment id 才是长期身份。发布系统应维护“deployment → 当前 job → 当前 savepoint → 上一版 savepoint”的映射。

### M.9.2 控制器循环

```text
observe:  查 Flink 作业状态、checkpoint、savepoint
compare:  与 desiredState / generation 比较
act:      提交、停止、恢复、回滚
record:   写 operation、审计、指标
repeat
```

关键设计：

- **幂等键**：每个变更请求带唯一 key，重试不产生第二次发布；
- **串行化**：同一 deployment 的变更串行执行，避免两个控制器同时 stop；
- **租约/锁**：多副本控制器用租约选主，或依赖 Kubernetes leader election；
- **状态机**：`PENDING → RUNNING → SUSPENDED/FAILED`，非法迁移要拒绝；
- **观测**：控制器自己的 lag、失败率、操作时长要纳入 SLO；
- **人工闸门**：生产发布可要求审批，但审批结果要落成 operation 状态。

### M.9.3 多租户

| 维度 | 做法 |
|---|---|
| 命名空间 | 每团队/环境独立 namespace，Flink 资源隔离 |
| 配额 | 按 slot/CPU/内存/状态存储做配额 |
| 网络 | NetworkPolicy 限制 JM/TM 只访问必要系统 |
| 镜像 | 只允许受信任仓库，禁止任意 UDF jar |
| 元数据 | Catalog/库表权限与租户绑定 |
| 审计 | 记录提交人、制品 hash、配置 diff、savepoint |
| 计费 | 按 slot-hour、状态存储、流量分摊 |

平台 API 的返回值要稳定：不要把 Flink 原始 JSON 直接透传给业务。原始 JSON 的字段会随版本变化，业务应该只依赖你定义的 `Deployment`/`Operation` 契约。

## M.10 安全

REST API 是强控制面，暴露它等于暴露“停止生产作业”的能力。最低要求：

1. **不要公网暴露 8081**；只开放在管理网/VPN/服务网格内；
2. **显式绑定地址**：用 `rest.bind-address` / `rest.address` 控制监听与对外地址；
3. **TLS**：开启 `security.ssl.rest.enabled`，并配置可信证书；
4. **认证**：开源 Flink 的 REST 本身不提供完整的用户体系，通常用反向代理、API 网关、mTLS 或企业 SSO 前置；
5. **授权**：按租户/环境限制“查询、savepoint、cancel、提交”四类权限，不要只分“能登录/不能登录”；
6. **最小暴露**：关闭不需要的调试端点（如 flame graph/profiling），或仅在排障窗口开放；
7. **审计**：记录操作者、来源 IP、job id、操作类型、请求体摘要、结果；
8. **制品安全**：限制 jar 上传，校验 hash/签名，禁止把密钥打进 jar；
9. **密钥管理**：REST token、S3/HDFS 凭据、Kafka SASL 密码放 Secret 管理系统，不进 Git；
10. **网络分段**：JM/TM 到对象存储、Kafka、数据库的出口按需放行。

一个常见误区是“内网所以不用认证”。内网横向移动、误操作和供应链攻击同样会造成生产事故；控制面至少要有身份和审计。

## M.11 排障 Runbook

### M.11.1 HTTP 层

| 现象 | 常见原因 | 处理 |
|---|---|---|
| 401/403 | 网关/token/TLS 客户端证书 | 检查代理与证书链，不要绕过认证 |
| 404 job not found | 集群/namespace 错、job 已结束、job id 截断 | 先查 `/jobs/overview`，确认 JM |
| 409 cancel | 作业已结束/已取消 | 视为幂等成功或核对状态 |
| 400 body | 字段名/类型/版本不匹配 | 对照发行版请求体类，做契约测试 |
| 502/503 | JM 未就绪、leader 切换、网关超时 | 退避重试，查 JM 日志与 HA |
| 308 redirect | 用了已移除的旧端点/错误 mode | 改用 2.x 端点，更新脚本 |

### M.11.2 操作层

| 现象 | 优先检查 | 说明 |
|---|---|---|
| savepoint 一直 IN_PROGRESS | 反压、状态大小、存储带宽、超时 | 不要重复触发；复用 triggerId |
| savepoint COMPLETED 但 location 不可读 | 权限、生命周期、跨集群路径 | 恢复演练要验证可读性 |
| stop 后作业没停 | 异步未完成、drain 卡住、source 不响应 | 轮询状态，必要时 cancel |
| 升级后状态不兼容 | UID、maxParallelism、序列化器、版本 | 见附录 L |
| 升级后重复写 | sink 事务/幂等、savepoint 边界 | 见第 11 章 |
| checkpoint 持续失败 | 存储、反压、超时、状态大小 | 见第 8/27 章 |
| 新版本吞吐下降 | 计划变化、并行度、资源、序列化 | 对比 `/plan` 与指标 |

### M.11.3 发布失败的回滚顺序

```text
1. 停止新作业（若还在运行）
2. 确认旧 savepoint 可读
3. 用旧版本 + 旧 savepoint 恢复
4. 验证 checkpoint、输出、对账
5. 冻结新版本制品，保留现场
6. 复盘：UID/状态/计划/配置/依赖/流量
```

不要在回滚时同时“修一下配置”。回滚的目标是恢复已知状态，不是顺便做实验。

## M.12 检查清单

- [ ] 自动化脚本使用固定 Flink 版本，并在升级时跑 REST 契约测试；
- [ ] 所有时间/超时/重试有上限，savepoint 使用幂等 trigger id；
- [ ] 发布记录包含 job id、savepoint location、代码版本、并行度、maxParallelism；
- [ ] 生产 savepoint 在恢复前做过可读性验证；
- [ ] 不依赖 2.x 已移除的 rescale REST 端点；
- [ ] stop 的 `drain` 语义经过业务确认；
- [ ] 控制面不暴露公网，具备认证、授权、审计；
- [ ] 平台 API 使用 deployment/operation 抽象，不透传易变 JSON；
- [ ] checkpoint、反压、异常、savepoint 操作都有告警；
- [ ] 升级/回滚演练至少覆盖一次真实状态恢复；
- [ ] 多租户配额与网络策略生效；
- [ ] 失败操作保留现场，不在回滚时夹带变更。

## M.13 小结

Flink REST API 的价值不在于“能 curl”，而在于它把作业生命周期暴露成可编排的控制面。要把这件事做稳，需要同时处理：

- **版本差异**：2.x 移除了旧端点，字段风格也不完全统一；
- **异步语义**：savepoint/stop 是操作，不是一次 HTTP 调用；
- **状态契约**：savepoint 的可用性取决于 UID、并行度、序列化器和版本；
- **平台抽象**：deployment/operation 比 job id 更稳定；
- **安全与审计**：控制面必须按生产系统对待。

把[第 8 章](../chapters/08-checkpoint.md)、[第 9 章](../chapters/09-recovery.md)、[第 25 章](../chapters/25-kubernetes.md)、[第 27 章](../chapters/27-troubleshooting.md) 的机制与本附录的接口结合起来，才能把“发布一个 Flink 作业”从手工操作变成可回滚的工程流程。接口字段、状态码和版本差异以所用发行版的 [Flink REST API 文档](https://nightlies.apache.org/flink/flink-docs-stable/docs/ops/rest_api/) 为准。实时数仓的建模方法见[附录 N](N-realtime-warehouse-modeling.md)。
