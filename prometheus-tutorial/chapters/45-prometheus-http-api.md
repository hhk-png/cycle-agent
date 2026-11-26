# 第 45 章 Prometheus HTTP API、UI 与自动化

## 45.1 API 是 Prometheus 的第二个产品界面

Grafana、告警平台、发布系统、成本报表、混沌实验工具，最终都通过 HTTP API 与 Prometheus 交互。理解 API 的语义，比记住端点列表更重要：

- 它返回的是**求值结果**，不是原始存储行；同一个查询在不同时间、不同 step 下结果不同；
- 它区分**即时向量、区间矩阵、标量、字符串**四种结果类型；
- 它用统一的错误信封表达 `bad_data`、`timeout`、`execution` 等失败；
- 它把"查询"和"管理"分开：查询默认开放，管理端点需要显式开启；
- 它不是为大规模导出设计的：分页、增量拉取、全量 dump 都要自己设计。

本章按"查询 → 元数据 → 状态 → 管理 → 联邦/远程 → 自动化"的顺序展开。端点细节速查见附录 V。

## 45.2 请求与响应的统一形状

所有 `/api/v1` 端点共享 JSON 信封。成功：

```json
{
  "status": "success",
  "data": {}
}
```

失败：

```json
{
  "status": "error",
  "errorType": "bad_data",
  "error": "invalid parameter \"query\": 1:1: parse error: unexpected end of input"
}
```

常见 `errorType`：

| errorType | 含义 | 典型原因 |
|---|---|---|
| `bad_data` | 请求参数/表达式不合法 | PromQL 语法错误、时间格式错误、缺少 `match[]` |
| `execution` | 查询执行失败 | 样本数超限、函数参数不合法 |
| `timeout` | 查询超时 | 大范围/高基数查询 |
| `canceled` | 客户端取消 | 连接断开、context 取消 |
| `unavailable` | 服务不可用 | 存储未就绪、remote read 失败 |
| `internal` | 内部错误 | 不应出现的 bug |

HTTP 状态码与 `status` 不总是一一对应：参数错误通常 400，执行错误可能 422，超时 503。客户端应同时检查 HTTP 状态码和 `errorType`，不要只看 `status`。

请求方式：

- `GET` 适合短查询，参数放 query string；
- `POST` 适合长表达式/复杂参数，使用 `application/x-www-form-urlencoded`；
- 时间参数可以是 Unix 秒（浮点）或 RFC3339，例如 `1700000000.123` 或 `2026-11-26T10:00:00Z`；
- 所有参数都要 URL 编码，尤其是 PromQL 里的 `{`、`}`、`"`、`|`。

## 45.3 即时查询与区间查询

### 45.3.1 `/api/v1/query`

```bash
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=sum by (job) (rate(http_requests_total[5m]))' \
  --data-urlencode 'time=2026-11-26T10:00:00Z'
```

参数：

- `query`（必需）：PromQL 表达式；
- `time`（可选）：求值时间，默认当前时间；
- `timeout`（可选）：查询超时，受服务端上限约束。

返回 `data.resultType` 为 `vector`、`scalar` 或 `string`。即时查询的 `vector` 结果：

```json
{
  "resultType": "vector",
  "result": [
    {
      "metric": {"job": "api"},
      "value": [1700000000.123, "12.5"]
    }
  ]
}
```

样本值永远是**字符串**，因为要表达 `NaN`、`+Inf`、`-Inf`；时间戳是秒（浮点）。不要把值直接当 JSON number 解析。

### 45.3.2 `/api/v1/query_range`

```bash
curl -sG 'http://localhost:9090/api/v1/query_range' \
  --data-urlencode 'query=sum(rate(http_requests_total[5m]))' \
  --data-urlencode 'start=2026-11-26T09:00:00Z' \
  --data-urlencode 'end=2026-11-26T10:00:00Z' \
  --data-urlencode 'step=60s'
```

参数：

- `start`/`end`（必需）：区间；
- `step`（必需）：求值步长，可以是 `60s`、`1m` 或浮点秒；
- `query`、`timeout` 同上。

返回 `matrix`：

```json
{
  "resultType": "matrix",
  "result": [
    {
      "metric": {"job": "api"},
      "values": [[1700000000, "12.5"], [1700000060, "13.1"]]
    }
  ]
}
```

关键语义：

- `step` 决定求值网格，不是存储分辨率；Prometheus 在网格点上做 lookback/range 选择；
- `start`/`end`/`step` 的组合会产生 `(end-start)/step + 1` 个点；点太多会触发样本限制；
- 区间查询的 `rate()` 窗口与 `step` 无关，仍由 `[5m]` 决定；
- 结果按序列分组，不保证序列顺序；客户端要自己排序；
- 没有分页；大结果只能缩小时间范围、降低基数或增加 step。

### 45.3.3 结果类型

| 类型 | 端点 | 形状 |
|---|---|---|
| `vector` | query | 每个序列一个 `(metric, value)` |
| `matrix` | query_range | 每个序列一个 `(metric, values[])` |
| `scalar` | query | `[timestamp, "value"]` |
| `string` | query | `[timestamp, "string"]` |

PromQL 的字符串结果很少见（如 `label_replace` 不会返回字符串；主要是某些函数/实验特性），但客户端应能处理。

## 45.4 元数据端点

### 45.4.1 `/api/v1/series`

查找匹配的序列：

```bash
curl -sG 'http://localhost:9090/api/v1/series' \
  --data-urlencode 'match[]=http_requests_total{job="api"}' \
  --data-urlencode 'start=2026-11-26T09:00:00Z' \
  --data-urlencode 'end=2026-11-26T10:00:00Z'
```

- `match[]` 至少一个，可重复；
- `start`/`end` 可选；
- 返回标签集列表，不返回值；
- 高基数/大范围查询很贵，可能超时；
- 它不是"列出所有指标"的廉价方式；看指标名用 `/api/v1/label/__name__/values` 或 metadata。

### 45.4.2 `/api/v1/labels` 与 `/api/v1/label/<name>/values`

```bash
curl -s 'http://localhost:9090/api/v1/labels'
curl -s 'http://localhost:9090/api/v1/label/job/values'
```

- 可选 `match[]`、`start`、`end` 限定范围；
- `labels` 返回标签名列表；
- `label/<name>/values` 返回某个标签的值列表；
- 对高基数标签（如 `instance`、`pod`）要加 `match[]` 和时间范围。

### 45.4.3 `/api/v1/metadata`

返回指标元数据：

```json
{
  "status": "success",
  "data": {
    "http_requests_total": [
      {"type": "counter", "help": "Total HTTP requests.", "unit": ""}
    ]
  }
}
```

- `metric` 可选，限定指标名；
- `limit` 可选，限制返回的指标数；
- 元数据来自最近抓取的 `# HELP`/`# TYPE`/`# UNIT`，不是历史全量；
- 同一指标可能有多个元数据条目（不同目标/版本），客户端要合并。

### 45.4.4 `/api/v1/targets` 与 `/api/v1/targets/metadata`

`targets` 是排障入口：

```bash
curl -s 'http://localhost:9090/api/v1/targets?state=active'
```

关键字段：

- `scrapePool`：job 名；
- `scrapeUrl`：实际抓取 URL；
- `globalUrl`：考虑 external URL 后的地址；
- `health`：`up`/`down`/`unknown`；
- `lastError`：最近一次错误；
- `lastScrape`、`lastScrapeDuration`；
- `labels`：合并后的目标标签；
- `discoveredLabels`：relabel 前的标签（排障 relabel 的关键）。

`state` 可取 `active`、`dropped`、`any`。`droppedTargets` 展示被 relabel 丢弃的目标，是"为什么没有抓这个 Pod"的第一手证据。

`targets/metadata` 按目标和指标查元数据，适合做指标目录/审计。

### 45.4.5 `/api/v1/rules` 与 `/api/v1/alerts`

```bash
curl -s 'http://localhost:9090/api/v1/rules?type=alert'
curl -s 'http://localhost:9090/api/v1/alerts'
```

`rules` 返回规则组、规则表达式、健康状态、最近求值时间、活跃告警；`type` 可过滤 `alert`/`record`，`rule_name[]`/`file[]` 可过滤。

`alerts` 只返回**当前活跃**告警（pending/firing），不返回历史。历史在 Alertmanager 或外部事件系统。

### 45.4.6 `/api/v1/alertmanagers`

返回 Prometheus 配置的 Alertmanager 实例（active/dropped）。用于确认 notifier 是否发现目标、URL 是否正确。它不返回 Alertmanager 的内部状态；那要查 Alertmanager 自己的 API。

## 45.5 状态与自监控端点

这些端点用于运维和自动化，不用于业务查询。

| 端点 | 内容 |
|---|---|
| `/api/v1/status/buildinfo` | 版本、revision、branch、build date、Go 版本 |
| `/api/v1/status/runtimeinfo` | 启动时间、CWD、配置重载状态、goroutine 数、GOMAXPROCS、存储保留期 |
| `/api/v1/status/config` | 当前生效配置的 YAML |
| `/api/v1/status/flags` | 启动参数 |
| `/api/v1/status/tsdb` | head 统计、按指标/标签的序列数、内存占用 |
| `/api/v1/status/walreplay` | WAL 重放进度/统计 |
| `/api/v1/status/tsdb` + `seriesCountByMetricName` | 找高基数指标的第一站 |

`/api/v1/status/config` 返回的是**已加载**配置，不是磁盘文件；如果重载失败，它仍是旧配置。配合 `runtimeinfo.reloadConfigSuccess` 和 `lastConfigTime` 判断。

`/api/v1/status/tsdb` 的 `seriesCountByMetricName`、`labelValueCountByLabelName`、`memoryInBytesByLabelName` 是基数治理的核心输入。注意它统计的是 head/TSDB 当前状态，不是账单；不同版本字段可能变化。

## 45.6 管理端点：强大但危险

管理端点默认关闭（或需要显式 flag），因为它们可以删除数据、创建快照、改变运行状态。

### 45.6.1 快照

```bash
curl -s -X POST 'http://localhost:9090/api/v1/admin/tsdb/snapshot'
```

返回快照名，目录在 `snapshots/<name>`。快照是备份/迁移的首选方式：

- 不停止写入；
- 包含 head 的持久化视图；
- 可用于 `tsdb` 工具或对象存储上传；
- 需要足够的磁盘空间；
- 不要在快照目录上直接跑生产查询。

### 45.6.2 删除序列

```bash
curl -s -X POST 'http://localhost:9090/api/v1/admin/tsdb/delete_series' \
  --data-urlencode 'match[]={__name__="debug_metric"}'
```

删除是**逻辑删除**（tombstone），不会立刻释放磁盘；需要后续 compaction 或 `clean_tombstones`。误删的恢复成本很高，生产应：

- 限制 admin API 的访问；
- 先在小范围验证 `match[]`；
- 记录操作审计；
- 优先用 retention/relabel 从源头治理。

### 45.6.3 清理 tombstone

```bash
curl -s -X POST 'http://localhost:9090/api/v1/admin/tsdb/clean_tombstones'
```

触发清理，可能带来 IO 峰值。不要在高负载时段执行。

### 45.6.4 生命周期端点

```text
GET  /-/healthy     健康检查（进程活着）
GET  /-/ready       就绪检查（可以查询）
POST /-/reload      重载配置（需 --web.enable-lifecycle）
```

`/-/reload` 也可以用 `SIGHUP`。重载失败时旧配置继续生效；自动化要检查响应和 `runtimeinfo.reloadConfigSuccess`。

## 45.7 联邦、Remote Write/Read 与 OTLP

### 45.7.1 `/federate`

联邦端点返回文本格式的瞬时向量：

```bash
curl -sG 'http://prometheus-a:9090/federate' \
  --data-urlencode 'match[]=job:http_requests:rate5m' \
  --data-urlencode 'match[]=up{job="api"}'
```

- `match[]` 至少一个；
- 返回的是**当前值**，不是历史；
- 适合抓取聚合后的 recording rules，不适合全量拉取；
- 上游会以抓取方式写入下游，因此标签合并、`honor_labels`、stale 语义都适用；
- 联邦不是长期存储/全局查询方案。

### 45.7.2 Remote Write/Read

```text
POST /api/v1/write   Content-Type: application/x-protobuf
POST /api/v1/read    Content-Type: application/x-protobuf
```

Remote Write receiver 需要显式开启；协议是 protobuf + Snappy，带 `X-Prometheus-Remote-Write-Version`。Remote Read 返回查询结果，常用于全局查询层。安全上：

- 这两个端点接受外部写入/读取，必须加认证和网络策略；
- 写入端要控制租户/标签，避免伪造 `job`/`instance`；
- 读取端可能被用于 SSRF/数据外泄；
- 版本/协议兼容性要验证（RW 1.0 vs 2.0）。

### 45.7.3 OTLP

启用 `--web.enable-otlp-receiver` 后：

```text
POST /api/v1/otlp/v1/metrics
Content-Type: application/x-protobuf   # 常见
Content-Type: application/json         # OTLP/HTTP 规范允许
```

OTLP 端点适合小规模/实验；生产通常用 Collector 做缓冲、重试、转换和基数治理。delta temporality 默认不能直接摄入，需要 `--enable-feature=otlp-deltatocumulative`（或交给支持 delta 的后端）。Mini 实现了 OTLP/JSON 子集，protobuf/NHCB/exemplar 见附录 AG。

## 45.8 错误处理、超时与限流

客户端必须处理：

- **400/422**：表达式/参数错误，重试无意义；记录请求和错误信息；
- **503/timeout**：服务端过载或查询太大；退避重试，缩小范围；
- **连接断开**：查询被取消；区分客户端取消和服务端超时；
- **部分结果**：Prometheus 不返回"部分成功"的查询结果；要么成功，要么错误；
- **样本限制**：`query.max-samples` 超限会返回 execution 错误；不要用超大查询探测容量；
- **成本上限（3.16，实验）**：启用 `--enable-feature=query-cost` 后，`query_max_series`/`query_max_samples_read` 超限返回 422、`errorType=cost_limit`；`/api/v1/query_cost` 与 `/api/v1/query_range_cost` 可在执行前只做估算；
- **并发**：Prometheus 没有每租户 QPS 限制；高并发查询会拖垮单机，需要网关/限流。

一个健壮的查询客户端应：

1. 设置 HTTP timeout，略大于服务端 `query.timeout`；
2. 对 5xx/超时做指数退避；
3. 对 4xx 不重试，直接报错；
4. 限制响应体大小；
5. 记录 `errorType`、查询文本、时间范围、耗时；
6. 对高频查询做缓存/预聚合，而不是反复打 API。

## 45.9 UI：API 的可视化外壳

Prometheus 自带 UI，常见路由：

```text
/graph            查询与图表
/alerts           活跃告警
/targets          抓取目标
/rules            规则组
/status           运行时状态
/tsdb-status      TSDB 统计
/flags            启动参数
/config           当前配置
/service-discovery 服务发现
```

UI 的价值在排障：

- `/targets` 看 `lastError`、`discoveredLabels`、`scrapeUrl`；
- `/graph` 的 Table/Graph 切换能看原始样本和序列标签；
- `/rules` 看规则健康、最近求值、活跃告警；
- `/tsdb-status` 看 head 序列数和高基数指标；
- `/config` 确认重载后的实际配置。

UI 不是 API 的替代品：自动化要直接调 API，UI 只用于人工排查。Prometheus 3 的 UI 有较大改版，路由和交互可能随版本变化，生产自动化不要依赖 UI 路由。

## 45.10 Mini 的 API 覆盖

Mini 实现了教程所需的 API 子集：

```text
/api/v1/query
/api/v1/query_range
/api/v1/series
/api/v1/labels
/api/v1/label/<name>/values
/api/v1/metadata
/api/v1/targets
/api/v1/targets/metadata
/api/v1/rules
/api/v1/alerts
/api/v1/alertmanagers
/api/v1/query_exemplars
/api/v1/status/buildinfo
/api/v1/status/runtimeinfo
/api/v1/status/config
/api/v1/status/flags
/api/v1/status/tsdb
/api/v1/status/walreplay
/api/v1/admin/tsdb/snapshot
/api/v1/admin/tsdb/delete_series
/api/v1/admin/tsdb/clean_tombstones
/api/v1/write
/api/v1/read
/api/v1/otlp/v1/metrics   # OTLP/JSON，需 --web.enable-otlp-receiver
/federate
```

差异：

- 没有 TLS/认证/多租户；
- 没有 `format_query`/`parse_query`、OTLP/protobuf、NHCB、remote read streaming；OTLP 只支持 JSON 子集；
- 查询结果在内存中物化，没有生产级限流；
- 管理端点默认关闭，需 `--web.enable-admin-api`（与上游一致）；生产仍需额外认证与网络策略；
- API 错误信封与真实 Prometheus 对齐，但细节字段可能更少。

Mini 的 API 实现见第 41 章，端点速查见附录 J。

## 45.11 自动化示例：一个最小查询客户端

```python
from __future__ import annotations

import json
import urllib.parse
import urllib.request


def query(base: str, expression: str, when: str | None = None, timeout: float = 10.0):
    params = {"query": expression}
    if when:
        params["time"] = when
    url = f"{base.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(
            f"{payload.get('errorType')}: {payload.get('error')}"
        )
    return payload["data"]["result"]


if __name__ == "__main__":
    for sample in query("http://127.0.0.1:9090", "up == 0"):
        print(sample["metric"], sample["value"])
```

生产客户端还应加：

- 重试/退避；
- 连接池；
- 请求超时与总预算；
- 结果缓存；
- 指标化的客户端自监控（请求数、错误率、延迟）；
- 认证（Bearer/Basic/mTLS）与代理配置。

## 45.12 检查表

- [ ] 客户端是否同时检查 HTTP 状态码和 `errorType`？
- [ ] 是否对查询设置超时、限制响应大小、避免超大范围？
- [ ] 是否使用 `match[]` 和时间范围限制 series/labels 查询？
- [ ] 管理端点是否关闭或加了认证/网络策略？
- [ ] 重载配置后是否检查 `reloadConfigSuccess`？
- [ ] 备份是否用 snapshot API，而不是直接拷数据目录？
- [ ] 是否监控 API 延迟/错误率/查询并发？
- [ ] 联邦/remote read 是否只暴露必要数据？

## 本章小结

- `/api/v1` 用统一 JSON 信封，错误用 `errorType` 区分；值以字符串返回。
- 即时查询返回 vector/scalar/string，区间查询返回 matrix；`step` 是求值网格。
- series/labels/metadata/targets/rules/alerts 是排障与自动化的核心元数据端点。
- 管理端点能删数据/做快照，必须加保护；快照是备份首选。
- 联邦只适合聚合指标，remote write/read 和 OTLP 是规模化与互操作路径。
- 客户端要处理超时、限流、错误类型和响应大小；UI 只用于人工排障。
- Mini 覆盖了主要查询/元数据/管理端点，但没有生产级认证与限流。

## 延伸阅读

- Prometheus 官方文档 "HTTP API"。
- Prometheus 源码 `web/api/v1/api.go`。
- Grafana Prometheus data source 文档。
- 本教程第 18 章（remote write/read）、第 41 章（Mini API）与附录 J/V。
