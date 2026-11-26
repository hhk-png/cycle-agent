# 附录 V Prometheus HTTP API 速查

本附录是第 45 章的配套速查。端点/参数以 Prometheus 3.x 为主；不同小版本可能有增减，生产升级前以官方文档和 `/api/v1/status/buildinfo` 为准。

## V.1 通用约定

- 基础路径：`/api/v1`；
- 成功：`{"status":"success","data":...}`；
- 失败：`{"status":"error","errorType":"...","error":"..."}`；
- 时间：Unix 秒（可带小数）或 RFC3339；
- 样本值：JSON 字符串（`"1"`、`"NaN"`、`"+Inf"`）；
- 时间戳：秒（浮点），内部存储是毫秒；
- `GET` 适合短查询，`POST` 用 `application/x-www-form-urlencoded`；
- 所有 PromQL 参数都要 URL 编码。

错误类型：

```text
bad_data      参数/表达式错误
execution     执行错误（样本超限、函数参数等）
timeout       查询超时
canceled      客户端取消
unavailable   服务/存储不可用
internal      内部错误
```

## V.2 查询

### V.2.1 即时查询

```text
GET|POST /api/v1/query
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `query` | 是 | PromQL 表达式 |
| `time` | 否 | 求值时间；默认 now |
| `timeout` | 否 | 超时，受服务端上限约束 |

返回 `vector`/`scalar`/`string`。

3.16 实验特性（`--enable-feature=query-cost`）：

- 加 `cost=true` 返回估算与实际成本；
- `GET|POST /api/v1/query_cost` 只估算、不执行；
- 超过 `global.query_max_series`/`query_max_samples_read` 返回 422、`errorType=cost_limit`。

```bash
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=up == 0' \
  --data-urlencode 'time=2026-11-26T10:00:00Z'

curl -sG 'http://localhost:9090/api/v1/query_cost' \
  --data-urlencode 'query=sum(rate(http_requests_total[1h]))'
```

### V.2.2 区间查询

```text
GET|POST /api/v1/query_range
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `query` | 是 | PromQL 表达式 |
| `start` | 是 | 起始时间 |
| `end` | 是 | 结束时间 |
| `step` | 是 | 求值步长（`60s` 或秒） |
| `timeout` | 否 | 超时 |

返回 `matrix`。

```bash
curl -sG 'http://localhost:9090/api/v1/query_range' \
  --data-urlencode 'query=sum(rate(http_requests_total[5m]))' \
  --data-urlencode 'start=2026-11-26T09:00:00Z' \
  --data-urlencode 'end=2026-11-26T10:00:00Z' \
  --data-urlencode 'step=60s'
```

### V.2.3 格式化/解析查询

```text
GET|POST /api/v1/format_query
GET|POST /api/v1/parse_query
```

- `format_query`：把表达式格式化成可读形式；
- `parse_query`：返回 AST/结构信息；
- 属于较新/实验性接口，版本间可能变化；自动化不要强依赖。

## V.3 元数据

### V.3.1 序列

```text
GET|POST /api/v1/series
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `match[]` | 是 | 序列选择器，可重复 |
| `start` | 否 | 起始时间 |
| `end` | 否 | 结束时间 |
| `limit` | 否 | 结果数限制（版本相关） |

```bash
curl -sG 'http://localhost:9090/api/v1/series' \
  --data-urlencode 'match[]=http_requests_total{job="api"}' \
  --data-urlencode 'start=2026-11-26T09:00:00Z' \
  --data-urlencode 'end=2026-11-26T10:00:00Z'
```

### V.3.2 标签

```text
GET|POST /api/v1/labels
GET      /api/v1/label/<label_name>/values
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `match[]` | 否 | 限定序列 |
| `start`/`end` | 否 | 时间范围 |
| `limit` | 否 | 结果数限制 |

```bash
curl -s 'http://localhost:9090/api/v1/labels'
curl -s 'http://localhost:9090/api/v1/label/job/values'
```

### V.3.3 元数据

```text
GET /api/v1/metadata
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `metric` | 否 | 指标名 |
| `limit` | 否 | 返回指标数上限 |

返回 `{metric: [{type, help, unit}]}`。元数据来自最近抓取，不是历史全量。

### V.3.4 目标

```text
GET /api/v1/targets?state=active|dropped|any
GET /api/v1/targets/metadata
```

`targets` 关键字段：

```text
scrapePool, scrapeUrl, globalUrl, health,
lastError, lastScrape, lastScrapeDuration,
labels, discoveredLabels
```

`targets/metadata` 参数：`match_target`、`metric`、`limit`。

### V.3.5 规则与告警

```text
GET /api/v1/rules?type=alert|record
GET /api/v1/alerts
GET /api/v1/alertmanagers
```

`rules` 支持 `rule_name[]`、`file[]` 过滤。`alerts` 只返回活跃告警，不含历史。

## V.4 状态

| 端点 | 内容 |
|---|---|
| `/api/v1/status/buildinfo` | version/revision/branch/buildDate/goVersion |
| `/api/v1/status/runtimeinfo` | startTime/CWD/reloadConfigSuccess/lastConfigTime/corruptionCount/goroutineCount/GOMAXPROCS/GOGC/storageRetention |
| `/api/v1/status/config` | 当前生效配置 YAML |
| `/api/v1/status/flags` | 启动参数 |
| `/api/v1/status/tsdb` | headStats/seriesCountByMetricName/labelValueCountByLabelName/memoryInBytesByLabelName |
| `/api/v1/status/walreplay` | WAL 重放进度/统计 |

```bash
curl -s 'http://localhost:9090/api/v1/status/tsdb' | python -m json.tool
```

`/status/config` 是已加载配置；重载失败时仍是旧配置。

## V.5 管理

管理端点需要显式开启（如 `--web.enable-admin-api`），且应加认证/网络策略。

| 端点 | 方法 | 作用 |
|---|---|---|
| `/api/v1/admin/tsdb/snapshot` | POST | 创建 TSDB 快照 |
| `/api/v1/admin/tsdb/delete_series` | POST | 逻辑删除序列（tombstone） |
| `/api/v1/admin/tsdb/clean_tombstones` | POST | 清理 tombstone |

### V.5.1 快照

```bash
curl -s -X POST 'http://localhost:9090/api/v1/admin/tsdb/snapshot'
```

可选 `skip_head=true`。返回 `{"name":"..."}`；目录在 `snapshots/<name>`。

### V.5.2 删除

```bash
curl -s -X POST 'http://localhost:9090/api/v1/admin/tsdb/delete_series' \
  --data-urlencode 'match[]={__name__="debug_metric"}' \
  --data-urlencode 'start=2026-11-01T00:00:00Z' \
  --data-urlencode 'end=2026-11-26T00:00:00Z'
```

删除是逻辑删除；磁盘释放依赖 compaction/clean_tombstones。

### V.5.3 清理

```bash
curl -s -X POST 'http://localhost:9090/api/v1/admin/tsdb/clean_tombstones'
```

可能产生 IO 峰值，避开高峰。

## V.6 生命周期

```text
GET  /-/healthy
GET  /-/ready
POST /-/reload
```

- `/-/healthy`：进程活着；
- `/-/ready`：可以查询（存储就绪）；
- `/-/reload`：重载配置，需要 `--web.enable-lifecycle`；失败保留旧配置。

```bash
curl -s -X POST http://localhost:9090/-/reload
curl -s http://localhost:9090/-/ready
```

## V.7 联邦

```text
GET /federate
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `match[]` | 是 | 序列选择器，可重复 |

返回文本格式当前值；适合抓取 recording rules 聚合指标，不适合全量历史。

```bash
curl -sG 'http://prometheus-a:9090/federate' \
  --data-urlencode 'match[]=job:http_requests:rate5m'
```

## V.8 Remote Write/Read 与 OTLP

| 端点 | 协议 | 说明 |
|---|---|---|
| `/api/v1/write` | protobuf + Snappy | Remote Write receiver；需显式开启 |
| `/api/v1/read` | protobuf + Snappy | Remote Read |
| `/api/v1/otlp/v1/metrics` | OTLP protobuf | 需 `--web.enable-otlp-receiver` |

Remote Write 关键 header：

```text
Content-Type: application/x-protobuf
Content-Encoding: snappy
X-Prometheus-Remote-Write-Version: 0.1.0
```

RW 2.0 使用不同的版本/能力协商，接收端需支持。

## V.9 查询参数与限制

- 查询超时：`--query.timeout`（服务端上限）+ 请求 `timeout`；
- 样本限制：`--query.max-samples`，超过返回 execution 错误；
- 并发：无每租户 QPS 限制；需要网关/限流；
- 区间点数：大范围 + 小 step 会产生大量点；缩小范围/增大 step；
- `series`/`labels` 的大范围全匹配很贵；加 `match[]` 和时间范围；
- 结果不保证序列顺序；客户端要排序；
- 没有分页；大结果要靠查询设计。

## V.10 认证与安全

Prometheus 自身支持：

- TLS：`--web.config.file` 的 `tls_server_config`；
- Basic Auth：`basic_auth_users`；
- 反向代理/SSO：推荐生产；
- CORS：`--web.cors.origin`（正则）；
- Admin API：默认关闭，开启后必须加保护；
- Remote Write/Read receiver：必须加认证/网络策略。

```yaml
tls_server_config:
  cert_file: /etc/prometheus/server.crt
  key_file: /etc/prometheus/server.key

basic_auth_users:
  admin: $2y$12$...   # bcrypt hash
```

不要把 `/api/v1/admin/*`、`/api/v1/write`、`/api/v1/read` 暴露到公网。

## V.11 客户端示例

```bash
# 健康检查
curl -fsS http://localhost:9090/-/healthy

# 查询并提取第一个序列
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=up' \
  | python -c 'import json,sys; d=json.load(sys.stdin); print(d["data"]["result"][0])'

# 查看目标错误
curl -s 'http://localhost:9090/api/v1/targets?state=active' \
  | python -c 'import json,sys; d=json.load(sys.stdin); [print(t["labels"].get("job"), t["health"], t["lastError"]) for t in d["data"]["activeTargets"]]'

# 快照备份
curl -s -X POST http://localhost:9090/api/v1/admin/tsdb/snapshot
```

## V.12 Mini 的端点差异

Mini 实现：

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
/federate
```

差异：

- 无 TLS/认证/多租户；
- 无 `format_query`/`parse_query`/OTLP；
- 无 3.16 的 `query_cost`/`cost=true`/成本上限（Mini 不做查询成本模型）；
- 无生产级限流/分页；
- admin 端点无 flag 保护（教学环境）；
- 响应字段是真实 API 的子集。

Mini API 的实现与测试见第 41 章、`tests/test_api.py`。

## V.13 排障顺序

1. `/-/healthy`、`/-/ready` 是否正常？
2. `/api/v1/status/runtimeinfo` 的 `reloadConfigSuccess`/`corruptionCount`？
3. `/api/v1/targets` 的 `lastError`/`health`？
4. `/api/v1/rules` 的 `health`/`lastError`？
5. `/api/v1/status/tsdb` 是否有高基数指标？
6. 查询错误是 `bad_data` 还是 `timeout`/`execution`？
7. 管理操作是否在低峰、有备份、有审计？

## 延伸阅读

- Prometheus 官方文档 "HTTP API"。
- Prometheus 源码 `web/api/v1/api.go`。
- 本教程第 45 章（API/UI/自动化）与第 41 章（Mini API）。
