# 第 12 章 Model Registry：版本、别名与发布状态

## 学习目标

- 理解注册模型、模型版本、source、alias、stage 的关系。
- 设计一个可回滚、可审计的发布指针模型。
- 识别并发创建版本、删除引用、阶段迁移中的一致性风险。
- 能解释为什么“注册”不等于“部署”。

## 12.1 Registry 解决什么问题

没有 Registry：

```text
/models/best_final.pkl
/models/best_final_v2.pkl
/models/best_final_v2_USE_THIS.pkl
```

问题：

- 谁是当前生产？
- 这个文件从哪个 run 来？
- 谁批准上线？
- 如何回滚？

Registry 把这些变成结构化数据：

```text
RegisteredModel: fraud-detector
  ├─ v1  source=runs:/aaa/model  status=READY
  ├─ v2  source=runs:/bbb/model  status=READY  aliases=[champion]
  └─ v3  source=runs:/ccc/model  status=READY  aliases=[canary]
```

- **RegisteredModel** 是稳定的逻辑名称；
- **ModelVersion** 是不可变的内容指针（版本号递增）；
- **source** 指向 artifact（通常是 `runs:/...`）；
- **alias** 是可变的发布指针。

## 12.2 版本不可变，别名的语义

“版本不可变”意味着：

- v2 的 source 一旦创建不应改；
- 重新训练产生新版本 v3；
- 回滚只是把 `champion` 指向 v2，不复制文件。

为什么不用 Stage 作为主发布机制？

- Stage 是固定枚举，无法表达 canary/shadow/previous；
- 一个版本只能有一个 Stage，而现实需要多个视角；
- 事务性迁移 Stage 容易产生“谁才是生产”的歧义；
- MLflow 已推荐 alias，Stage 作为遗留兼容。

Mini 同时支持：

```text
Staging / Production / Archived / None
```

以及任意 alias：

```text
champion、challenger、canary、shadow、previous
```

### 别名不是版本号

Mini 拒绝纯数字别名，避免 `models:/m/1` 到底是版本还是别名产生歧义。

## 12.3 创建版本

`create_model_version` 的关键步骤：

1. 校验注册模型存在；
2. 若提供 run_id，校验 run 存在且未删除；
3. 分配 `MAX(version)+1`；
4. 写入 source、run_id、status=READY；
5. 返回版本。

Mini 的 source 可以是任意 URI 字符串，但不会校验 artifact 是否真的存在。生产应在注册时：

- 解析 source；
- 验证 `MLmodel` 可读；
- 验证签名/依赖；
- 计算 digest；
- 写入审批状态。

### 并发

两个客户端同时注册：

```text
T1: SELECT MAX(version) -> 2
T2: SELECT MAX(version) -> 2
T1: INSERT version=3  ✅
T2: INSERT version=3  ❌ UNIQUE
```

Mini 在单写者 SQLite 下多数时候安全，但没有彻底消除。生产方案：

- `INSERT ... SELECT MAX+1` 单语句（仍有隔离级别问题）；
- 单独的序列表 + `UPDATE ... RETURNING`；
- 应用层重试（捕获唯一冲突，重新读 MAX）；
- 数据库序列。

版本创建是“读-改-写”，必须显式处理冲突。

### 真实 MLflow 客户端写法

```python
from mlflow import MlflowClient

client = MlflowClient()

# 1) 逻辑模型
client.create_registered_model("churn")

# 2) 不可变版本；3.x 的 source 常为 models:/<model_id>，2.x 兼容 runs:/...
version = client.create_model_version(
    name="churn",
    source="models:/m-abc123",
    run_id="<source_run_id>",          # 可选，用于血缘
)

# 3) 发布指针（推荐）与解析
client.set_registered_model_alias("churn", "champion", version.version)
resolved = client.get_model_version_by_alias("churn", "champion")
print(resolved.version, resolved.source)
```

`transition_model_version_stage` 仍可能存在，但 stages 已不是 3.x 的推荐发布方式；新代码用 alias。注意 `latest` 是 URI 关键字，不是可分配的普通 alias。

## 12.4 Source 的血缘

source 里可以编码：

```text
runs:/<run_id>/<artifact_path>
```

Registry 同时存 `run_id`，方便：

- 从版本反查训练 run；
- 从 run 查产出的模型版本；
- 审计数据/代码/参数。

局限：

- source 不保证 artifact 永久存在；
- 删除 run 可能导致版本不可加载；
- 外部 source（本地路径、S3）不受 Registry 管理；
- 多模型/一个 run 的场景需要 artifact_path 区分。

删除策略应先检查引用：

```text
run 是否被某个 ModelVersion.source 引用？
  是 → 拒绝物理删除 / 标记 tombstone
  否 → 允许 GC
```

Mini 的 `delete_run` 不检查 Registry 引用，这是教学简化，生产必须补。

## 12.5 阶段迁移

`transition_model_version_stage` 只改 `current_stage`。Mini 不做自动归档旧 Production，也不保证唯一性。真实 MLflow 的行为也依版本/配置，不应假设“切 Production 会自动下线旧版本”。

发布系统应明确：

- 同一 alias 是覆盖式指针；
- 同一 stage 是否允许并存；
- 回滚是 alias 切换还是版本重建；
- 旧版本保留多久。

## 12.6 删除

Mini 的 `delete_registered_model(name, force=False)`：

- 有版本且 `force=False` → 拒绝；
- `force=True` → 级联删除版本、标签、别名。

真实系统不建议物理删除模型版本：

- 审计需要；
- 外部引用可能仍在使用；
- 别名/部署配置可能未更新。

更安全的做法是“归档 + 保留期 + 引用检查 + 审计”，而不是直接 DELETE。

## 12.7 搜索

Mini 支持：

```text
名称/创建时间/更新时间/描述 + tags
```

模型版本支持：

```text
name/version/run_id/source/status/current_stage/tags
```

真实 MLflow 的过滤字段依版本不同；生产应把 Registry 元数据同步到数据仓库，做更复杂的治理报表。

## 12.8 注册与部署的边界

Registry 只回答“哪个版本存在、指向哪里、叫什么别名”。它不：

- 启动推理服务；
- 做流量切分；
- 监控延迟/错误率；
- 自动回滚；
- 保证模型可加载。

部署系统需要：

- 解析 `models:/name@alias`；
- 拉取 artifact；
- 验证依赖/签名；
- 启动/滚动更新；
- 健康检查；
- 记录部署版本（回写到 Registry 标签或外部 CMDB）。

## 12.9 一个发布流程

```text
1. 训练 run 结束，模型制品通过评测门槛
2. 注册 vN，source=runs:/...
3. 自动打标签：eval_report、approval_status=pending
4. 人工/策略审批
5. 设置 alias canary → vN
6. 观察线上指标
7. 设置 alias champion → vN，previous → vN-1
8. 记录部署事件（环境、集群、镜像 digest）
9. 异常时 champion → previous
```

每一步都应可审计：谁、何时、为什么。第 38 章的 Webhook 可以把 `model_version.created`、`model_version_alias.created` 等事件推给评测/部署/审批系统，避免轮询 Registry。

## 12.10 与真实 MLflow 的差距

- 真实 Registry 支持更完整的 REST/UI、审批、描述、标签和模型版本元数据；
- 真实实现可能与云目录/Unity Catalog 集成；
- 真实 Stage 已不推荐，alias 的 API 更丰富；
- 真实删除/归档策略依后端；
- Mini 不提供版本比较、模型卡片、自动评测。

## 小结

- Registry 是“不可变制品指针 + 可变发布指针”的组合。
- alias 比 stage 更适合现代发布（canary/shadow/回滚）。
- 版本创建有并发风险，source 血缘不保证制品永久存在。
- 注册不等于部署；部署系统要负责可加载性、流量与监控。

## 练习

1. 设计一套 alias 命名和权限（谁能改 champion/canary）。
2. 如何检测“某模型版本被删除但其 alias 仍指向它”？
3. 为版本创建设计幂等键，避免网络重试导致重复版本。

---

下一章：[第 13 章 REST Server](13-rest-server.md)
