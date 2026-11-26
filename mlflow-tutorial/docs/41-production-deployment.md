# 第 41 章 真实 MLflow 生产部署实战：Compose、Postgres、MinIO 与反向代理

## 学习目标

- 把“MLflow 生产部署”从概念清单变成一份可逐行检查的参考编排。
- 理解 `--serve-artifacts`、S3 兼容存储与反向代理三者的责任边界。
- 设计数据库迁移、备份恢复、升级回滚的先后顺序，而不是只写一条 `docker compose up`。
- 识别最常见的五类线上故障：数据库连接耗尽、制品 403、请求体超限、worker 内存、时钟偏移。
- 说清这套编排与 MiniMLflow 的映射关系，以及哪些能力仍然只能由托管平台或自研控制面补齐。

> 本章的 `examples/deploy/` 是一份**参考编排**。教程 CI 会通过 `scripts/check_deploy.py` 检查 Compose YAML、Nginx 关键指令与 shell 语法，但不会启动 Docker；它也无法发现 healthcheck 选错、镜像未固定、`.env` 未加载这类运行时问题。因此文中命令需要在一次性环境中执行并完成 41.9 的验收清单后，才能作为生产模板。不要因为“静态检查通过”就认为它已经被验证。

## 41.1 为什么是这四个组件

一个团队级 MLflow 部署，最小可信拓扑是：

```text
┌──────────┐    HTTPS     ┌──────────────┐
│ Client   │ ───────────> │ Nginx        │
│ CLI/UI   │              │ TLS / limits │
└──────────┘              └──────┬───────┘
                                 │ HTTP
                          ┌──────▼────────┐
                          │ MLflow server │  2+ workers
                          │ tracking API  │
                          └───┬───────┬───┘
                              │       │
                   ┌──────────▼─┐  ┌──▼─────────────┐
                   │ Postgres   │  │ S3 / MinIO     │
                   │ metadata   │  │ artifacts      │
                   └────────────┘  └────────────────┘
```

四个组件的职责必须分开：

| 组件 | 负责 | 不负责 |
|---|---|---|
| Postgres | experiment/run/metric/registry/trace 元数据 | 大文件、模型权重 |
| S3/MinIO | artifact 字节、模型目录、trace 附件 | 事务、搜索、权限语义 |
| MLflow server | 协议、业务规则、artifact 代理、UI | 长期保存字节、TLS 终止 |
| Nginx | TLS、请求体上限、超时、访问日志 | 业务鉴权、实验隔离 |

常见的错误拓扑是“把 artifact 也放进容器本地卷”。单机试用没问题，但一旦要扩 worker 或换机器，模型就会“消失”。第 2 章讲过元数据与制品分离的原因，这里是它的部署形态。

## 41.2 第一步：数据库不是 SQLite

> 本节与第 36 章的升级/备份流程互补：第 36 章讲“什么时候做什么”，本节讲“参考编排里具体怎么写”。

SQLite 适合个人和教学；团队服务器的默认后端应该是 Postgres。原因不是“SQLite 不能并发”，而是：

- 多 worker、多副本需要共享同一份元数据；
- 备份、点恢复、只读副本依赖数据库工具；
- 迁移由 Alembic 管理，SQLite 的 `ALTER TABLE` 能力有限；
- 监控、连接池、慢查询分析在 Postgres 上有成熟生态。

`examples/deploy/docker-compose.yml` 里 Postgres 的关键配置只有三项：用户/密码/库名、持久卷、健康检查。真正的生产还要加：

- 连接数上限与 PgBouncer（当 worker 数量上来后）；
- 定期 `VACUUM`/autovacuum 参数；
- 备份保留与异地副本；
- 监控 `pg_stat_activity`、锁等待、磁盘增长。

### 迁移必须在启动前显式执行

MLflow 的 schema 由 Alembic 管理。升级版本时，不要指望“服务启动自动迁移”：

```bash
# 在能访问数据库的容器/机器上执行
mlflow db upgrade postgresql://mlflow:***@postgres:5432/mlflow
```

顺序是：

```text
备份 -> 在副本上 db upgrade -> 启动新版本服务 -> 冒烟 -> 切流量
```

如果跳过备份，迁移失败时你只有两个选择：回滚镜像（schema 可能已经变了）或手工修表。两者都比一次 `pg_dump` 贵。

## 41.3 第二步：对象存储与 artifact 代理

MLflow 支持两种 artifact 访问模式：

| 模式 | 客户端拿到什么 | 适合 |
|---|---|---|
| 直连对象存储 | 预签名 URL 或 S3 凭证 | 客户端已在同一 VPC、能管好凭证 |
| 服务端代理（`--serve-artifacts`） | 指向 MLflow 的 `/api/2.0/mlflow-artifacts/...` | 客户端不应持有对象存储密钥 |

参考编排选择代理模式：

```bash
mlflow server \
  --backend-store-uri postgresql://... \
  --artifacts-destination s3://mlflow \
  --serve-artifacts
```

这样客户端只需要 `MLFLOW_TRACKING_URI`，不需要 `AWS_ACCESS_KEY_ID`。服务端用环境变量访问 MinIO：

```text
MLFLOW_S3_ENDPOINT_URL=http://minio:9000
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
```

三个容易踩的坑：

1. **`--artifacts-destination` 与 `--default-artifact-root` 不是一回事。** 前者是多实验共享的根，后者是旧版的单实验默认路径；新部署用前者。
2. **MinIO 的 bucket 必须先存在。** 参考编排用 `minio-init` 一次性创建；生产应把 bucket 创建纳入 IaC，而不是依赖容器启动顺序。
3. **代理模式会把字节穿过 MLflow 进程。** 大模型上传会占用 worker 的内存/带宽；如果你的模型是几十 GB，考虑直连对象存储或独立的 `--artifacts-only` 服务。

较新版本提供了 presigned artifact 方案（3.18 的 release notes 提到 presigned artifact flows/server mode；具体行为以锁定版本为准），允许服务端签发短期 URL，把字节传输从应用进程移走。是否启用取决于你的客户端版本与网络边界；本章的参考编排保持代理模式，因为它最容易解释和排错。

## 41.4 第三步：worker 数量与“无状态”

`--workers N` 启动多个服务 worker。3.3 起 tracking server 默认是 FastAPI + uvicorn（旧版/显式配置下才走 Flask/gunicorn），所以不要把 gunicorn 参数当成通用事实；以 `mlflow server --help` 和你锁定的版本为准。可以水平扩展的前提是：

- 元数据在 Postgres，而不是本地 SQLite；
- artifact 在 S3，而不是本地卷；
- 没有依赖进程内缓存做正确性判断；
- 定时任务（GC、webhook 投递）有单实例锁或独立进程。

MLflow server 的 worker 之间不共享内存。多副本下的一致性陷阱在第 38 章已经展开；这里只列部署层必须回答的问题：

| 能力 | 单进程 | 多 worker |
|---|---|---|
| 请求限流 | 进程内计数 | Redis/网关限流 |
| Webhook 投递 | 后台线程 | 独立 dispatcher + 数据库 claiming |
| Trace 采样 | 进程内随机 | 一致性哈希/集中配置 |
| artifact 缓存 | 本地磁盘 | 共享缓存或 CDN |

Mini 的 webhook outbox 是单进程 `drain_once`，第 38 章已经说明多副本怎么改。部署章节要记住同一句话：**“能启动两个副本”不等于“能安全运行两个副本”**。

## 41.5 第四步：反向代理的四个数字

Nginx 不是“加个 TLS 就完事”。至少要对齐四个数字：

1. **`client_max_body_size`**：必须大于最大的 artifact/trace 请求体，但小于磁盘/内存能承受的上限。参考配置是 100 MB；如果你的模型更大，调大它，同时在应用/ASGI server 层设置对应的请求体上限（不要照抄未经验证的环境变量名）。
2. **`proxy_read_timeout`**：模型下载、trace 查询、UI 长请求可能超过默认 60 秒；参考配置给 300 秒。
3. **`proxy_buffering off`**：大文件下载时避免 Nginx 先落盘再转发；代价是占用连接更久。
4. **`X-Forwarded-*`**：MLflow 生成回调/重定向 URL 时需要知道外部 scheme 与 host；不传会导致 UI 里的链接指向内部容器名。

参考配置还做了两件事：

- 把 `/health` 单独放行，便于负载均衡器探活；
- 关闭 `proxy_redirect`，避免把内部 `mlflow:5000` 泄漏到响应头。

### TLS 与证书

参考编排从 `examples/deploy/certs/` 读取证书。生产应使用 ACME/企业 CA，并把私钥放在 secret manager，而不是镜像或 Git。证书轮换要纳入值班流程：Nginx reload 不会自动读取新文件，除非你用 `nginx -s reload` 或 sidecar 监听。

## 41.6 第五步：认证、授权与审计

这一层最容易被“内网所以不用认证”糊弄过去。至少要区分三件事：

| 问题 | 最低要求 | 更强做法 |
|---|---|---|
| 谁在调用 | Bearer token / Basic Auth | OIDC + 短期 token |
| 能做什么 | 全库读写或只读 | workspace + RBAC + 子资源 DENY |
| 做过什么 | 访问日志 | 审计日志 + 不可篡改存储 |

MLflow OSS 自带 basic auth（`--app-name basic-auth`），适合小团队；它提供用户、权限与实验级授权，但不是完整的 OIDC/SSO。需要 SSO 时常见做法是：

- 在 Nginx/网关层接 OIDC（oauth2-proxy、企业 API 网关）；
- 或使用社区插件 `mlflow-oidc-auth`；
- 把“认证”放在代理层、“授权”放在 MLflow/插件层，不要让代理只做登录不做权限。

3.13 之后的开源 RBAC、3.17 的子资源 DENY、3.10 的 workspace 是另一条路线（第 37 章）。两者不要混用同一套假设：workspace 不是安全边界本身，必须和认证、授权、存储隔离同时成立。

### 一个最小权限矩阵

```text
数据科学家：experiment READ/EDIT，artifact READ/WRITE，registry READ
发布系统：  registry READ，model-version alias READ，artifact READ
审计员：    全库 READ，禁止 EDIT/DELETE
CI：        指定 experiment WRITE，禁止改生产 alias
```

把这张表写进 IaC 或启动配置，而不是口头约定。第 23 章的发布治理需要它作为基础。

## 41.7 第六步：备份、恢复与升级

> 备份/恢复的原则见第 36.3 节；这里给出与参考编排配套的可执行脚本和一致性窗口。

### 备份对象

| 对象 | 方式 | 一致性要点 |
|---|---|---|
| Postgres | `pg_dump -Fc` 或物理备份 | 与 artifact 写入的时间窗口要对齐 |
| S3/MinIO | 版本化 bucket + 跨区复制 | 对象不可变时最简单；覆盖写要留版本 |
| 配置/密钥 | secret manager 导出 | 不与数据放在同一备份域 |
| 迁移版本 | `alembic_version` / Alembic revision | 恢复后先确认版本匹配 |

参考脚本 `examples/deploy/backup.sh` 做的是“逻辑 dump + `mc mirror`”。它适合中小规模；大规模对象存储应使用 bucket replication，而不是每次全量 mirror。

### 恢复演练

恢复不是“把 dump 灌回去”，而是一组可验证的步骤：

1. 在隔离环境启动同版本 Postgres 与 MinIO；
2. 恢复数据库与 artifact；
3. 启动**同版本** MLflow，确认 `db upgrade` 不需要再跑；
4. 跑冒烟：读 experiment、读 run、下载 artifact、加载模型、查 trace；
5. 记录 RTO/RPO，和业务约定对比；
6. 清理隔离环境，确认没有把测试流量写回生产。

`examples/deploy/restore.sh` 会 drop 并重建数据库，这是故意的破坏性操作；只允许在隔离环境执行。

### 升级顺序

```text
1. 读 release notes，标记 breaking changes 与 schema 变更
2. 备份数据库 + 记录 artifact 版本
3. 在副本上 db upgrade，跑冒烟
4. 升级服务端，保留旧镜像
5. 升级客户端（先读后写）
6. 观察错误率、延迟、迁移版本
7. 超过回滚窗口后再删旧镜像/旧备份
```

第 34 章讲过 3.x 的不可逆写入风险；部署章节要把它变成操作约束：**回滚窗口内禁止产生旧版本读不了的新格式**，或者保留双写/导出。

## 41.8 第七步：监控与容量

四个黄金信号仍然适用。第 25 章讲容量模型、第 36.4 节讲监控清单；本节只保留部署验收最相关的指标：

| 层 | 指标 | 告警建议 |
|---|---|---|
| 服务 | 请求率、5xx、P95/P99、worker 重启 | 5xx > 1% 持续 5 分钟 |
| 数据库 | 连接数、慢查询、锁等待、磁盘 | 连接数 > 80% 上限 |
| Artifact | 4xx/5xx、上传下载字节、bucket 容量 | 403 突增通常意味着凭证/策略漂移 |
| Trace | 写入量、平均 span 数、payload 大小 | 单 trace 超限会直接丢数据 |
| Registry | 版本创建、alias 变更、webhook 投递失败 | alias 变更应该有审计事件 |

容量估算的粗粒度公式：

```text
元数据增长 ≈ runs/day × (params + tags + metric points)
artifact 增长 ≈ 模型大小 × 模型数 × 保留份数
trace 增长 ≈ traces/day × spans/trace × 平均 payload
```

真正容易失控的是 trace payload 和模型 checkpoint，而不是 run 行数。第 25 章给出了更细的拆解。

## 41.9 验收清单

在把参考编排用于任何真实流量前，逐项打勾：

- [ ] `.env` 中没有任何默认密码；
- [ ] Postgres 数据卷在持久磁盘上，且已做一次恢复演练；
- [ ] MinIO bucket 已创建，匿名访问已关闭；
- [ ] `--serve-artifacts` 生效：客户端不配置 S3 凭证也能上传/下载；
- [ ] Nginx 的 body size、超时与 MLflow 的限制一致；
- [ ] `/health` 可从负载均衡器访问，且不暴露内部信息；
- [ ] 认证已开启；至少有一个只读账号和一个发布账号；
- [ ] `mlflow db upgrade` 在副本上成功，并记录了版本；
- [ ] 备份脚本在 cron/CI 中运行过，且能从备份启动一套隔离环境；
- [ ] 升级/回滚演练完成，回滚窗口与不可逆写入策略写进 runbook；
- [ ] 监控覆盖 5xx、数据库连接、artifact 403、trace 丢弃；
- [ ] 日志中不出现 token、S3 密钥、用户 prompt 原文。

## 41.10 与 MiniMLflow 的映射

| 生产组件 | Mini 对应 | 差距 |
|---|---|---|
| Postgres | SQLite | 无连接池、无只读副本、迁移是 `CREATE TABLE IF NOT EXISTS` |
| S3/MinIO | 本地 `file://` | 无分片上传、无预签名、无跨区复制 |
| Nginx | 无 | 无 TLS 终止、无限流、无审计日志 |
| 多 worker | 单进程 `ThreadingHTTPServer` | 无 ASGI/多 worker、无 graceful reload |
| basic auth/OIDC | 单 Bearer token | 无用户、角色、workspace |
| 备份 | 复制 `.db` 与 artifact 目录 | 无在线一致性保证 |
| 监控 | `/health` + 日志 | 无指标暴露、无 trace |

Mini 的价值不是“替代这套编排”，而是让你在读 `docker-compose.yml` 时知道每一行在保护什么：`--serve-artifacts` 保护凭证边界，`client_max_body_size` 保护内存，`db upgrade` 保护 schema 兼容性。

模型服务本身的协议、Kubernetes 编排与灰度回滚见第 42 章；本章的编排是它的上游（元数据/制品），不是替代品。

## 41.11 常见故障与排查顺序

### 故障 1：所有请求 500，日志里是 `too many connections`

先看 worker 数 × 每 worker 连接数是否超过 Postgres `max_connections`。临时把 worker 降到 1，确认是否恢复；长期方案是 PgBouncer 或调连接池，而不是无限调大数据库上限。

### 故障 2：上传 artifact 返回 403

按这个顺序查：

1. 客户端是否仍在直连 S3，而 bucket 策略只允许服务端角色；
2. `MLFLOW_S3_ENDPOINT_URL` 是否指向了外部不可达的地址；
3. MinIO 的 access key 是否在重启后变化；
4. 代理模式的 URL 是否被 Nginx 改写。

### 故障 3：UI 能打开，但大模型下载到一半断开

检查 Nginx `proxy_read_timeout`、`proxy_buffering`、`client_max_body_size`，以及 MLflow worker 的内存。大文件下载不是“打开页面”的同一类请求。

### 故障 4：升级后旧客户端写入失败

先确认客户端/服务端 minor 是否匹配，再看服务端日志里的 `INVALID_PARAMETER_VALUE`。不要用“回滚客户端”掩盖 schema 已经升级的事实；必要时回滚服务端并恢复备份。

### 故障 5：trace 时间戳乱序

容器时钟漂移会让 trace/span 的先后关系错乱。所有节点接 NTP；对跨服务 trace，优先使用单调时钟 + 逻辑顺序，而不是只信墙钟。

## 小结

- 生产部署的最小可信拓扑是“Postgres + 对象存储 + 无状态 server + 反向代理”，四者职责不能混。
- `--serve-artifacts` 把对象存储凭证留在服务端；代价是字节穿过应用进程。
- 多 worker 的前提是元数据、制品、缓存、定时任务都不依赖单进程内存。
- Nginx 的 body size、超时、buffering、forwarded headers 是最容易出错也最容易验证的四个数字。
- 备份要同时覆盖数据库与制品；恢复必须在隔离环境演练；升级要先备份、再迁移、再切流量。
- 参考编排没有被教程 CI 启动过；41.9 的验收清单才是“可用”的定义。

## 练习

1. 把参考编排里的 MinIO 换成真实 S3，列出需要改动的环境变量、IAM 策略与网络规则。
2. 设计一个“只读副本 + 报表”的拓扑，说明哪些查询可以走副本，哪些必须走主库。
3. 给 `backup.sh` 增加校验：dump 能被 `pg_restore --list` 读取，artifact 数量与数据库记录对账。
4. 写一个升级演练脚本：从 v3.17 备份恢复到 v3.18，跑 41.9 的冒烟清单，再回滚。
5. 估算你所在团队的 trace 月增长量，给出保留 30/90/365 天的三种成本。
6. 解释为什么“workspace 开启”不等于“多租户安全”，并举出三个仍然可能越权的路径。

---

下一章：[第 42 章 模型服务与推理协议](42-model-serving-protocol.md)
