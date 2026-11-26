# 附录 E CLI 参考

> 第 46 章的 AgentOps（会话、模拟、scorer、分诊、比较）只提供 Python API，没有 CLI/REST；它复用已有 trace/assessment 表，因此也不需要新的服务端端点。

## E.1 入口

```bash
python -m mini_mlflow --help
python -m mini_mlflow --version
# 安装后也可以：
mini-mlflow --help
```

## E.2 server

```bash
python -m mini_mlflow server \
  --backend-store-uri sqlite:///mlflow.db \
  --artifact-root ./mlartifacts \
  --host 127.0.0.1 \
  --port 5000 \
  --auth-token secret \
  --config ./mini-mlflow.yaml \
  --tls-cert ./cert.pem \
  --tls-key ./key.pem
```

行为：

- 启动 HTTP + UI；
- 自动创建 schema/Default 实验/artifact 根；
- `SIGINT` 优雅关闭（仅主线程）。

## E.3 db

```bash
python -m mini_mlflow db init \
  --backend-store-uri sqlite:///mlflow.db \
  --artifact-root ./mlartifacts
```

输出示例：

```json
{"status":"ok","backend":"...","artifact_root":"...","schema_version":"7"}
```

## E.4 doctor

```bash
python -m mini_mlflow doctor
```

检查数据库、artifact 根、Default 实验、Registry 查询。退出码 0/1。

## E.5 experiments

```bash
python -m mini_mlflow experiments create demo --tag team=nlp
python -m mini_mlflow experiments list --filter 'tags.team = "nlp"'
python -m mini_mlflow experiments get 1
python -m mini_mlflow experiments delete 1
```

## E.6 runs

```bash
python -m mini_mlflow runs list --experiment-id 1 --max-results 50
python -m mini_mlflow runs list --filter 'metrics.auc > 0.8'
python -m mini_mlflow runs get <run_id>
python -m mini_mlflow runs metrics <run_id> auc
```

## E.7 registry

```bash
python -m mini_mlflow registry list
python -m mini_mlflow registry versions demo-model
python -m mini_mlflow registry set-alias demo-model champion 3
```

## E.8 run（MLproject）

```bash
python -m mini_mlflow run ./examples/projects/churn \
  -e main \
  -P alpha=0.1 \
  --experiment-name churn-demo \
  --run-name baseline
```

参数：

- `--experiment-name` 或 `--experiment-id` 二选一；两个都传时以显式解析到的实验为准，建议只传一个；
- `-P key=value` 可重复；
- `--timeout` 秒；
- `--tracking-uri` 覆盖环境。

输出：

```json
{
  "run_id": "...",
  "status": "FINISHED",
  "exit_code": 0,
  "command": ["python", "train.py", "--alpha", "0.1"]
}
```

## E.9 models

```bash
python -m mini_mlflow models serve \
  -m models:/house-price-linear@champion \
  --host 0.0.0.0 --port 5001 --auth-token secret
```

```bash
python -m mini_mlflow models predict \
  -m models:/house-price-linear@champion \
  --input '{"dataframe_split":{"columns":["floor"],"data":[[7.0]]}}'
```

`--input-file` 从文件读取 JSON；否则从 stdin。

## E.9.1 prompts

```bash
python -m mini_mlflow prompts register support-reply \
  --template "Hello {{name}}" --message "initial" --tag owner=qa
python -m mini_mlflow prompts register support-reply --template-file prompt.txt
python -m mini_mlflow prompts get 'prompts:/support-reply@production'
python -m mini_mlflow prompts list --filter 'tags.owner = "qa"'
python -m mini_mlflow prompts versions support-reply
python -m mini_mlflow prompts set-alias support-reply production 2
```

`--template` 与 `--template-file` 互斥；标签用 `key=value`。

## E.9.2 feedback

```bash
python -m mini_mlflow feedback add \
  --trace-id <32-hex> --name quality --value 0.9 \
  --value-type float --source-type HUMAN --source-id reviewer-7 \
  --rationale "accurate"

python -m mini_mlflow feedback list --trace-id <32-hex>
```

`--value-type` 支持 str/float/int/bool；bool 接受 true/false/1/0/yes/no。

## E.9.3 gateway

```bash
python -m mini_mlflow gateway validate --config examples/gateway.yaml
python -m mini_mlflow gateway invoke --config examples/gateway.yaml \
  --route support --message "hello"
```

`validate` 输出解析后的配置；`invoke` 输出完整响应（provider、usage、cost、attempts、cache_hit）。

## E.9.4 webhooks

```bash
python -m mini_mlflow webhooks create promote-on-alias \
  --url https://ci.example.com/hooks/mlflow \
  --event model_version.created \
  --event model_version_alias.created \
  --secret shared-secret

python -m mini_mlflow webhooks list
python -m mini_mlflow webhooks get <WEBHOOK_ID>
python -m mini_mlflow webhooks update <WEBHOOK_ID> --status DISABLED
python -m mini_mlflow webhooks test <WEBHOOK_ID>
python -m mini_mlflow webhooks deliveries <WEBHOOK_ID> --limit 50
python -m mini_mlflow webhooks delete <WEBHOOK_ID>
```

`--event` 可重复；事件名是 `entity.action`。`test` 会同步投递一次并返回 delivery 状态；`deliveries` 显示 outbox 历史。

## E.9.5 logged-models

```bash
python -m mini_mlflow logged-models create agent \
  --model-type Agent --source-run-id <RUN_ID> \
  --tag team=nlp --param temperature=0.2

python -m mini_mlflow logged-models list \
  --experiment-id 0 --filter "status = 'READY'" --order-by "metrics.quality DESC"

python -m mini_mlflow logged-models get m-<32hex>
python -m mini_mlflow logged-models params m-<32hex> --param max_tokens=128
python -m mini_mlflow logged-models metrics m-<32hex> --metric quality=0.8 --step 1
python -m mini_mlflow logged-models tag m-<32hex> --set stage=candidate --delete reviewed
python -m mini_mlflow logged-models finalize m-<32hex> --status READY --message "upload ok"
python -m mini_mlflow logged-models delete m-<32hex>
```

`create` 默认创建 PENDING；`finalize` 接受 READY/FAILED/PENDING，短名会被归一化为 REST 的 `LOGGED_MODEL_*` 拼写。`list` 的 `--experiment-id` 可重复，但最多 10 个。

## E.9.6 datasets

```bash
# records.json 是一个 JSON 数组；每个元素是 {inputs, expectations, tags}
python -m mini_mlflow datasets create support-eval \
  --records-file records.json --source s3://bucket/eval.jsonl --source-type s3 \
  --tag stage=golden --tag owner=cx

python -m mini_mlflow datasets list \
  --filter "tags.stage = 'golden'" --order-by "attributes.last_updated_time DESC"

python -m mini_mlflow datasets get d-<32hex>

python -m mini_mlflow datasets merge d-<32hex> --records-file more.json

python -m mini_mlflow datasets delete d-<32hex>
```

`--records-file -` 从 stdin 读取。`list` 返回元数据（记录数组为空，`record_count` 准确）；`get` 返回完整记录。合并会递增 version 并重算 digest。详见第 44 章。

## E.9.7 monitoring

```bash
# baseline.json / window.json 都是 JSON 数组；每个元素是一行特征
python -m mini_mlflow monitoring create churn-monitor \
  --baseline-file baseline.json \
  --feature tenure:numeric --feature plan:categorical \
  --model-name churn --model-version 3 \
  --threshold psi_alert=0.3 --tag owner=risk

python -m mini_mlflow monitoring list
python -m mini_mlflow monitoring get <monitor_id>

python -m mini_mlflow monitoring window <monitor_id> \
  --records-file window.json --window-start 1000 --window-end 2000

python -m mini_mlflow monitoring windows <monitor_id> --limit 20
python -m mini_mlflow monitoring delete <monitor_id>
```

`--feature` 可重复，格式为 `name:numeric` 或 `name:categorical`；`--threshold` 可重复，键必须在允许集合内。也可以传 `--dataset-id` 代替 `--baseline-file`，直接绑定第 44 章的受管数据集。详见第 45 章。

## E.10 全局选项与退出码

- `--tracking-uri`：客户端命令可用；优先级高于环境。
- 环境变量见附录 C。
- 退出码：
  - `0` 成功；
  - `2` 业务/输入错误（`MiniMlflowError` 或 ValueError/OSError）；
  - `1` 未预期错误（Python 异常）。

JSON 错误示例：

```json
{"error_code":"RESOURCE_DOES_NOT_EXIST","message":"run '...' not found"}
```

## E.11 脚本化建议

```bash
set -euo pipefail
RUN_ID=$(python -m mini_mlflow runs list --filter 'tags.candidate = "true"' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["runs"][0]["info"]["run_id"])')
python -m mini_mlflow runs get "$RUN_ID" | jq '.data.metrics'
```

不要依赖人类可读文本解析；用 JSON。

---

返回：[教程首页](../README.md) · [附录 F](appendix-f-troubleshooting.md)
