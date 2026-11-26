# 附录 A REST API 速查

> 除健康检查、UI 与 `/gateway/*` 外，MLflow API 路径以 `/api/2.0/mlflow/` 为前缀。响应是 MiniMLflow 的简化字段，不保证与真实 MLflow 逐字节兼容。鉴权启用时加 `Authorization: Bearer <token>`。

> 第 46 章的 AgentOps 不新增 REST 端点：会话由 `traces` 的 session tag 派生，评分写入既有 assessment 接口。真实 MLflow 的 Issues、在线 scorer 与统计比较是 3.18 的实验性服务端能力，路径与契约以版本文档为准。

## A.1 健康与 UI

```bash
curl -s http://127.0.0.1:5000/health | python -m json.tool
```

```http
GET  /                    # 302 -> /ui
GET  /health
GET  /favicon.ico
GET  /ui
GET  /ui/experiments/{experiment_id}
GET  /ui/runs/{run_id}
GET  /ui/traces
GET  /ui/traces/{trace_id}
GET  /ui/registry
GET  /ui/prompts
```

## A.2 实验

| 方法 | 路径 | 关键字段 |
|---|---|---|
| POST | `experiments/create` | name、artifact_location、tags |
| GET | `experiments/get` | experiment_id |
| GET | `experiments/get-by-name` | experiment_name |
| POST | `experiments/search` | filter、max_results、order_by、page_token |
| POST | `experiments/delete` | experiment_id |
| POST | `experiments/restore` | experiment_id |
| POST | `experiments/set-experiment-tag` | experiment_id、key、value |
| POST | `experiments/update` | experiment_id、name |

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/experiments/create \
  -H 'Content-Type: application/json' \
  -d '{"name":"demo","tags":[{"key":"team","value":"nlp"}]}'
```

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/experiments/search \
  -H 'Content-Type: application/json' \
  -d '{"filter":"tags.team = \"nlp\"","max_results":20,"order_by":["attributes.creation_time DESC"]}'
```

## A.3 Run

| 方法 | 路径 | 关键字段 |
|---|---|---|
| POST | `runs/create` | experiment_id/experiment_name、run_name、tags、run_id |
| POST | `runs/get` | run_id |
| POST | `runs/update` | run_id、run_name、status |
| POST | `runs/end` | run_id、status |
| POST | `runs/search` | filter、experiment_ids、max_results、order_by、page_token |
| POST | `runs/delete` / `restore` | run_id |
| POST | `runs/log-parameter` | run_id、key、value |
| POST | `runs/log-metric` | run_id、key、value、timestamp、step |
| POST | `runs/log-batch` | run_id（必填）、metrics、params、tags |
| POST | `runs/set-tag` / `delete-tag` | run_id、key、value |
| GET | `metrics/get-history` | run_id、metric_key |
| POST | `runs/log-input` | run_id、dataset_* |

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/runs/create \
  -H 'Content-Type: application/json' \
  -d '{"experiment_id":"1","run_name":"baseline"}'
```

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/runs/log-batch \
  -H 'Content-Type: application/json' \
  -d '{
    "run_id":"<RUN_ID>",
    "metrics":[{"key":"auc","value":0.91,"step":1}],
    "params":[{"key":"alpha","value":"0.1"}],
    "tags":[{"key":"stage","value":"train"}]
  }'
```

## A.4 Artifact

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `artifacts/list?run_id=...&path=...` | 列目录/单文件 |
| POST | `artifacts/log` | `{"run_id","path","content_b64"}` |
| GET | `artifacts/get?run_id=...&path=...` | 原始字节 |

```bash
B64=$(python -c "import base64;print(base64.b64encode(b'hello').decode())")
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/artifacts/log \
  -H 'Content-Type: application/json' \
  -d "{\"run_id\":\"<RUN_ID>\",\"path\":\"notes/a.txt\",\"content_b64\":\"$B64\"}"
```

```bash
curl -s -o out.txt \
  "http://127.0.0.1:5000/api/2.0/mlflow/artifacts/get?run_id=<RUN_ID>&path=notes/a.txt"
```

## A.5 Registry

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `registered-models/create` | name、description、tags |
| GET | `registered-models/get?name=` | 详情 |
| POST | `registered-models/search` | filter、order_by、max_results |
| POST | `registered-models/rename` | name、new_name |
| POST | `registered-models/update` | name、description |
| POST | `registered-models/delete` | name、force |
| POST | `registered-models/set-tag` / `delete-tag` | name、key、value |
| POST | `model-versions/create` | name、source、run_id、tags |
| GET | `model-versions/get?name=&version=` | 详情 |
| GET | `model-versions/get-by-alias?name=&alias=` | 别名解析 |
| POST | `model-versions/search` | filter、name、order_by |
| POST | `model-versions/update` / `delete` | name、version、description |
| POST | `model-versions/set-tag` / `delete-tag` | name、version、key、value |
| POST | `model-versions/transition-stage` | name、version、stage |
| POST | `registered-models/aliases/set` | name、alias、version |
| POST | `registered-models/aliases/delete` | name、alias |

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/registered-models/aliases/set \
  -H 'Content-Type: application/json' \
  -d '{"name":"demo-model","alias":"champion","version":3}'
```

## A.5.1 LoggedModel（MLflow 3）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `logged-models` | experiment_id（必填）、name、model_type、source_run_id、tags、params；无 status |
| GET | `logged-models/{model_id}` | 详情 |
| PATCH | `logged-models/{model_id}` | finalize；status + status_message |
| DELETE | `logged-models/{model_id}` | 删除元数据（制品保留） |
| POST | `logged-models/search` | experiment_ids（≤10）、filter、order_by、max_results、page_token |
| PATCH | `logged-models/{model_id}/tags` | 批量设置标签 |
| DELETE | `logged-models/{model_id}/tags/{key}` | 删除标签 |
| POST | `logged-models/{model_id}/params` | 记录不可变参数 |
| POST | `logged-models/{model_id}/metrics` | 追加指标 |

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/logged-models \
  -H 'Content-Type: application/json' \
  -d '{"experiment_id":"0","name":"agent","model_type":"Agent","params":[{"key":"temperature","value":"0.2"}]}'

curl -s -X PATCH http://127.0.0.1:5000/api/2.0/mlflow/logged-models/<MODEL_ID> \
  -H 'Content-Type: application/json' \
  -d '{"status":"LOGGED_MODEL_READY"}'

curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/logged-models/search \
  -H 'Content-Type: application/json' \
  -d '{"experiment_ids":["0"],"filter":"status = '\''READY'\'' AND metrics.quality > 0.8"}'
```

搜索方言：裸属性（`name`、`model_type`、`status`、`source_run_id`、`creation_time` 等）、`params.*`（值必须是字符串）、`metrics.*`（数值）；Mini 不支持 `tags.*`、`OR`、一元 `NOT`、`name LIKE`。官方指南写不支持 tags，但实现/docstring 存在 tags 示例（mlflow#17920）；以锁定版本实测为准。

## A.6 Trace

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `traces/log` | `{"trace": {...}}` |
| GET | `traces/get?trace_id=` | 详情 |
| POST | `traces/search` | experiment_ids、filter、max_results、order_by |

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/traces/search \
  -H 'Content-Type: application/json' \
  -d '{"filter":"status = \"ERROR\"","max_results":20}'
```

## A.6.1 Assessment（Feedback / Expectation）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `traces/assessments/log` | trace_id、name、value、source_type、source_id、rationale、metadata、is_expectation、valid |
| GET | `traces/assessments/get?assessment_id=` | 单条 |
| GET | `traces/assessments/list?trace_id=` | 某 trace 全部 |
| POST | `traces/assessments/delete` | assessment_id |

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/traces/assessments/log \
  -H 'Content-Type: application/json' \
  -d '{"trace_id":"...","name":"quality","value":0.9,"source_type":"HUMAN","rationale":"good"}'
```

Trace 响应中的 `assessments` 字段会包含这些记录。

## A.6.2 Prompt

| 方法 | 路径 | 关键字段 |
|---|---|---|
| POST | `prompts/create` | name、template、commit_message、tags |
| GET | `prompts/get` | name + version 或 alias |
| GET | `prompts/versions/list` | name |
| POST | `prompts/search` | filter、max_results、order_by |
| POST | `prompts/aliases/set` / `delete` | name、alias、version |
| POST | `prompts/delete` | name |

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/prompts/create \
  -H 'Content-Type: application/json' \
  -d '{"name":"support","template":"Hi {{name}}","tags":[{"key":"owner","value":"qa"}]}'

curl -s 'http://127.0.0.1:5000/api/2.0/mlflow/prompts/get?name=support&alias=production'
```

## A.6.3 评测数据集

| 方法 | 路径 | 关键字段 |
|---|---|---|
| POST | `datasets/create` | name、records、source、source_type、tags |
| GET | `datasets/get?dataset_id=` | dataset_id |
| POST | `datasets/search` | filter、max_results、order_by |
| POST | `datasets/merge-records` | dataset_id、records |
| POST | `datasets/delete` | dataset_id |

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/datasets/create \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"support-eval",
    "records":[{"inputs":{"question":"refund window?"},"expectations":{"answer":"30 days"}}],
    "source":"s3://warehouse/eval.jsonl",
    "source_type":"s3",
    "tags":[{"key":"stage","value":"golden"}]
  }'

curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/datasets/search \
  -H 'Content-Type: application/json' \
  -d '{"filter":"tags.stage = '\''golden'\''","order_by":["attributes.last_updated_time DESC"]}'
```

搜索返回元数据（`records` 为空、`record_count` 准确）；完整记录用 `datasets/get`。详见第 44 章。

## A.6.4 Gateway

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/gateway/routes` | 路由与 provider（无密钥） |
| GET | `/gateway/stats` | 请求/成本/延迟 |
| POST | `/gateway/invocations` | route、messages、params |

```bash
curl -s -X POST http://127.0.0.1:5000/gateway/invocations \
  -H 'Content-Type: application/json' \
  -d '{"route":"support","messages":[{"role":"user","content":"hi"}]}'
```

未配置 `--gateway-config` 时返回 404。401/429/502 分别对应鉴权、限流、provider 失败。

## A.6.5 Webhook

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `webhooks/create` | name、url、events、secret、description |
| GET | `webhooks/get?webhook_id=` | 详情（不返回 secret） |
| GET | `webhooks/list` | 全部订阅 |
| POST | `webhooks/update` | webhook_id + 可选字段 |
| POST | `webhooks/delete` | webhook_id |
| POST | `webhooks/test` | 发送 `webhook.test` 事件 |
| GET | `webhooks/deliveries?webhook_id=&limit=` | 投递历史 |

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/webhooks/create \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"promote-on-alias",
    "url":"https://ci.example.com/hooks/mlflow",
    "events":["model_version.created","model_version_alias.created"],
    "secret":"shared-secret"
  }'
```

事件名是 `entity.action`，不是 UPPER_SNAKE。投递请求带：

```text
X-Mlflow-Delivery-ID: <32-hex>
X-Mlflow-Timestamp: <unix-seconds>
X-Mlflow-Event: model_version.created
X-Mlflow-Signature: v1,<base64(HMAC-SHA256(secret, delivery_id.timestamp.payload))>
```

接收方必须按 `X-Mlflow-Delivery-ID` 去重，并校验时间窗。详见第 38 章。

## A.6.6 监控

| 方法 | 路径 | 关键字段 |
|---|---|---|
| POST | `monitors/create` | name、baseline_records 或 dataset_id、features、model_name/model_version、thresholds、tags |
| GET | `monitors/get?monitor_id=` | monitor_id |
| GET | `monitors/list` | 无 |
| POST | `monitors/delete` | monitor_id（级联删除窗口） |
| POST | `monitors/windows/create` | monitor_id、records、window_start、window_end |
| GET | `monitors/windows/list?monitor_id=&limit=` | monitor_id、limit（1–1000） |

```bash
curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/monitors/create \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"churn-monitor",
    "baseline_records":[{"tenure":12,"plan":"pro"}],
    "features":{"tenure":"numeric","plan":"categorical"},
    "model_name":"churn",
    "model_version":"3",
    "thresholds":{"psi_alert":0.3}
  }'

curl -s -X POST http://127.0.0.1:5000/api/2.0/mlflow/monitors/windows/create \
  -H 'Content-Type: application/json' \
  -d '{"monitor_id":"<ID>","records":[{"tenure":1,"plan":"enterprise"}],
       "window_start":1000,"window_end":2000}'
```

窗口响应包含 `status`（STABLE/WARN/ALERT）、`metrics`（逐特征统计量）与 `report`（阈值、原因、样本数）。详见第 45 章。

## A.7 错误

```json
{"error_code":"RESOURCE_DOES_NOT_EXIST","message":"run '...' not found"}
```

| HTTP | 典型错误码 |
|---|---|
| 400 | INVALID_PARAMETER_VALUE / BAD_REQUEST / RESOURCE_ALREADY_EXISTS |
| 401/403 | PERMISSION_DENIED |
| 404 | RESOURCE_DOES_NOT_EXIST |
| 413 | REQUEST_TOO_LARGE |
| 500 | INTERNAL_ERROR |

## A.8 与真实 MLflow 的差异

- 真实 API 的字段更完整（dataset inputs、tags 结构、model version metadata）。
- 真实评测数据集走 `mlflow.genai.datasets` 的 SDK/REST，支持 inputs 哈希去重、记录级操作与分页；Mini 是 `datasets/*` 的教学子集（追加语义、元数据搜索）。
- `monitors/*` 是 Mini 的教学扩展，不是真实 MLflow 的 REST；真实平台应把漂移报告写 run/artifact 或外部监控系统（第 45 章）。
- 真实 artifact 上传/下载协议不同；Mini 用 base64 JSON。
- 真实鉴权支持更多方式。
- 真实分页 token 不透明且可能携带排序键。

---

返回：[教程首页](../README.md) · [附录 B](appendix-b-schema.md)
