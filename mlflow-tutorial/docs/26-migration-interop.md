# 第 26 章 迁移与互操作：从文件存储到服务端

## 学习目标

- 理解本地文件存储、SQLite 后端、数据库后端之间的迁移边界。
- 设计一个可回滚的 MLflow 升级/迁移计划。
- 识别模型 URI、artifact 路径和版本字段的兼容性风险。
- 知道 MiniMLflow 与真实 MLflow 不能直接互换。

## 26.1 迁移的四种场景

1. **本地文件 → 服务器**：从单人 `mlruns/` 到团队服务。
2. **SQLite → Postgres**：提升并发/HA。
3. **本地 artifact → 对象存储**：容量与共享。
4. **MLflow 版本升级**：schema/API/行为变化。

每种都可能涉及：

- 元数据格式；
- artifact URI；
- 认证；
- 客户端版本；
- 正在运行的实验；
- 历史引用。

## 26.2 本地文件存储 vs 数据库

真实 MLflow 的本地文件模式把 run 元数据写成目录中的文件（`meta.yaml`、`metrics/` 等），服务器模式使用数据库。两者不是简单复制：

- 文件结构是后端特定格式；
- 迁移工具/脚本依版本；
- 运行中的 run 可能不一致；
- artifact 路径可能仍指向旧目录。

安全迁移步骤：

```text
1. 停止写入/训练任务
2. 备份 db + artifact
3. 用官方迁移工具/脚本导入
4. 校验 run/metric/artifact 数量与抽样内容
5. 切换客户端 tracking_uri
6. 保留旧存储只读一段时间
7. 回滚演练
```

Mini 只有 SQLite + 本地目录，不提供真实 MLflow 文件格式导入；不要假设可直接读取 `mlruns/`。

## 26.3 SQLite → Postgres

需要考虑：

- 自增 ID/序列；
- 布尔/时间类型；
- JSON 列；
- 索引/约束名；
- 大小写/排序规则；
- 事务隔离；
- 连接池；
- 迁移期间的停机/双写。

测试策略：

- 用生产规模子集做迁移演练；
- 校验行数/校验和；
- 抽样验证最新指标/artifact URI；
- 压测搜索/写入；
- 准备回滚到旧库（只读/快照）。

## 26.4 Artifact 迁移

从本地目录到 S3：

```text
file:///data/mlartifacts/1/run/artifacts
→ s3://bucket/mlflow/1/run/artifacts
```

难点：

- 数据库里存的是旧 URI，需要批量改写；
- 正在运行的 run 可能继续写旧路径；
- 模型版本 source 可能指向旧 URI；
- 权限/加密/生命周期策略；
- 校验和/去重。

方案：

- 双写过渡；
- 迁移后批量更新 URI（事务/脚本）；
- 保留旧路径只读；
- 验证每个模型版本可加载；
- 记录迁移前后 digest。

Mini 的 `artifact_location` 在实验创建时固定；修改需要显式数据库操作，没有迁移工具。

## 26.5 版本升级

MLflow 3.x 可能带来：

- API 字段变化；
- Stage 弃用/别名推荐；
- 新 tracing/eval 模型；
- 数据库迁移；
- 客户端/服务端兼容矩阵。

策略：

- 固定客户端/服务端版本范围；
- 先在 staging 升级；
- 备份数据库；
- 运行迁移命令；
- 验证旧客户端；
- 观察错误率；
- 准备回滚镜像/备份。

不要在生产直接 `pip install -U mlflow`。

## 26.6 URI 与引用

常见 URI：

```text
runs:/<run_id>/<artifact_path>
models:/<name>/<version>
models:/<name>@<alias>
models:/<name>/<stage>
file:///...
s3://...
```

迁移时要检查：

- 是否有硬编码的 `runs:/` 被外部系统引用；
- 别名是否仍指向存在的版本；
- source 是否可解析；
- 模型加载是否依赖旧环境；
- 本地绝对路径是否出现在生产配置。

Mini 的 `models:/` 解析支持 version/alias/stage/latest；真实 MLflow 的 URI 语义可能更丰富，不要把 Mini 的解析器当成通用实现。

## 26.7 导出与归档

需要长期归档时：

- 导出 run 元数据（JSON/Parquet）；
- 导出 artifact 清单 + digest；
- 导出模型版本/别名；
- 记录 schema 版本；
- 保留可读文档；
- 定期校验可恢复性。

只导出数据库 dump 不够：没有 artifact，模型无法加载；没有依赖，模型无法运行。

## 26.8 避免锁定

- 不在业务代码中硬编码 MLflow 内部表；
- 通过 API/客户端访问；
- 保存模型时保留原始框架格式；
- 记录模型 URI 与 digest；
- 对关键模型做独立于平台的归档；
- 定期演练“没有 MLflow 也能加载模型”。

## 26.9 Mini 与真实 MLflow 的互操作

不能直接互操作：

- REST 字段/路径子集不同；
- artifact 上传协议不同；
- 数据库 schema 不同；
- MLmodel 只实现 linear flavor；
- Trace 结构自定义。

可以借鉴：

- 概念映射；
- 迁移脚本思路；
- 安全/测试方法；
- UI/CLI 设计。

如果要从 Mini 迁移到真实 MLflow：

1. 导出 runs/params/metrics/artifacts；
2. 用真实客户端重新记录（或写导入器）；
3. 重新打包模型为真实 flavor；
4. 重建 Registry 版本/别名；
5. 校验 source/lineage。

## 26.10 与真实 MLflow 的差距

- 真实 MLflow 有官方迁移工具/文档（版本相关）；
- 真实后端/云存储迁移更复杂；
- Mini 不提供导入/导出命令；
- 真实系统可能需要处理多租户/权限迁移。

## 小结

- 迁移是“元数据 + artifact + 客户端 + 权限”的整体切换，不是复制数据库。
- 先备份、再演练、再切换、保留回滚路径。
- URI 和 source 是迁移中最容易断裂的引用。
- Mini 与真实 MLflow 不兼容，但概念和安全方法可复用。

## 练习

1. 写一个 SQLite → Postgres 的迁移检查清单（至少 10 项）。
2. 设计 artifact URI 批量改写的回滚方案。
3. 如何验证“模型在没有 MLflow 的环境仍可加载”？

---

下一章：[第 27 章 生态集成](27-ecosystem.md)
