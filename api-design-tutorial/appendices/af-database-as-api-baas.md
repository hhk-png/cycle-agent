# 附录 AF：数据库即 API、BaaS 与自动生成接口

> 把数据库直接暴露成 API，是过去十年交付速度提升最大、也最容易在半年后变成技术债的做法之一。它让一个前端在一下午连上数据库，也让一次 `ALTER TABLE` 变成对所有客户端的破坏性变更。
>
> 本附录讨论四类“自动生成接口”：BaaS、数据库自动 REST/GraphQL、Headless CMS/内部工具、低代码与工作流平台；重点不是推荐某个产品，而是回答三个问题：**自动生成的契约到底是谁的契约？安全边界在哪里？什么时候必须收回控制权？**

---

## AF.1 原型的速度与生产的代价

一个典型场景：

```text
周五下午：产品要验证一个想法。
前端直接用 Supabase / Firebase / Hasura 连数据库。
周一：功能上线，用户开始使用。
三个月后：安全审计发现 RLS 有一个表没开。
六个月后：公开 API 上线，第三方要求版本与 SLA。
九个月后：一次加索引的迁移锁表 40 分钟。
```

自动生成接口的价值是真实的：

- 不需要为每个 CRUD 写控制器、序列化、分页和校验；
- 前端可以独立推进，不被后端排期阻塞；
- 实时订阅、文件存储、认证等能力开箱即用；
- 数据模型变更可以立即反映到 API；
- 对内部工具、原型、数据密集型后台，开发成本可能下降一个数量级。

代价同样真实：

| 维度 | 自动生成接口的默认状态 | 生产环境需要 |
|---|---|---|
| 契约 | 数据库 schema 即接口 | 稳定、可版本化、与内部模型解耦 |
| 授权 | 角色/规则/RLS，容易被绕过 | 默认拒绝、对象级、可审计 |
| 校验 | 数据库约束 + 少量规则 | 业务规则、跨字段、跨聚合 |
| 性能 | 通用查询编译器 | 索引、查询计划、限流、隔离 |
| 演进 | 迁移即变更 | Expand–Contract、弃用、兼容窗口 |
| 可观测性 | 数据库日志 | 请求级 trace、租户级指标、审计 |
| 计费 | 基本没有 | 计量、配额、账单 |

**核心判断**：自动生成接口不是“没有 API 设计”，而是把 API 设计转移到了**数据库 schema、权限策略和生成器配置**里。这些位置一旦被忽视，风险比手写 API 更高，因为变更看起来只是“改表”。

---

## AF.2 四种形态，不要混为一谈

| 形态 | 代表 | 接口来源 | 典型消费者 | 控制点 |
|---|---|---|---|---|
| 托管 BaaS | Supabase、Firebase、AWS Amplify | 数据库/文档存储 + 客户端 SDK | Web/移动前端 | RLS、Security Rules、云 IAM |
| 数据库自动 API | PostgREST、Hasura、OData 服务 | 表/视图/函数 → REST/GraphQL | 前端、内部服务、BI | 数据库权限、生成器权限模型 |
| Headless CMS / 内部工具 | Directus、Strapi、Appsmith、Retool | 内容模型/集合 → REST/GraphQL | 内容团队、运营、内部员工 | 字段权限、角色、工作流 |
| 低代码 / 工作流 | n8n、Zapier、Make、内部低代码平台 | 连接器 + 触发器 | 业务人员、集成场景 | 连接凭据、审批、审计 |

它们共享一个特征：**API 表面由元数据生成，而不是由手写代码定义。** 但信任模型差别很大：

- Firebase 的客户端直接访问文档数据库，安全规则是唯一防线；
- PostgREST 把 PostgreSQL 的表、视图、函数映射成 REST，数据库角色和 RLS 是防线；
- Hasura 在数据库之上增加自己的权限模型，`x-hasura-role` 与会话变量是防线；
- Directus/Strapi 通常还有应用层权限，但字段级与关联查询仍可能泄露；
- 低代码平台的连接器往往持有高权限凭据，风险集中在凭据管理与审计。

选型时先问：**谁持有数据库凭证？消费者能直接触达数据面吗？** 如果答案是“前端直接连”，安全设计就必须按“数据库暴露在公网”的标准来做。

---

## AF.3 请求路径：从 HTTP 到 SQL

自动生成接口的本质是一个**查询编译器**：

```text
HTTP/gRPC/GraphQL 请求
  → 认证（JWT / API Key / Session）
  → 角色与声明解析（role、tenant、user id）
  → 权限策略（RLS / Hasura permissions / Firebase rules）
  → 查询编译（过滤、排序、分页、关联、聚合）
  → SQL / 文档查询
  → 序列化与响应
```

以 PostgREST 为例：

```http
GET /orders?status=eq.paid&select=id,total,customer(name)&order=created_at.desc&limit=20
Authorization: Bearer <JWT>
```

大致编译为：

```sql
SELECT o.id, o.total, row_to_json(c.*) AS customer
FROM orders o
LEFT JOIN customers c ON c.id = o.customer_id
WHERE o.status = 'paid'
ORDER BY o.created_at DESC
LIMIT 20;
```

但真正决定“能读到什么”的不是这条 SQL，而是：

```sql
-- 数据库角色
SET ROLE authenticated;

-- RLS 策略
CREATE POLICY orders_tenant_isolation ON orders
  FOR SELECT
  USING (tenant_id = current_setting('request.jwt.claims', true)::json->>'tenant_id');
```

**关键结论**：自动生成接口的契约由四层共同构成：

1. **数据模型**：表、列、类型、约束、索引、注释；
2. **权限模型**：角色、RLS、Security Rules、生成器权限；
3. **生成器配置**：暴露哪些 schema/表/函数、分页上限、最大行数、CORS、缓存；
4. **运行时**：连接池、语句超时、限流、网关。

只评审“接口文档”是不够的，因为文档往往由这四层自动推导，推导不出来的部分才是风险。

---

## AF.4 自动生成的契约长什么样

### AF.4.1 PostgREST：表就是资源

PostgREST 把 PostgreSQL 的 schema 暴露为 REST：

- 表/视图 → 集合资源；
- 主键 → 单资源；
- 外键 → 资源嵌入（embedding）；
- 函数 → RPC；
- 查询参数 → 过滤、排序、分页、字段选择。

常见能力：

```http
GET /orders?select=id,total,customer(name,email)
GET /orders?id=in.(1,2,3)
GET /orders?created_at=gte.2026-01-01
GET /orders?order=total.desc.nullslast
Range: 0-19
Prefer: count=exact
```

响应头可能包含：

```http
Content-Range: 0-19/1532
```

契约上的隐含承诺：

- 表名、列名、外键关系会成为公开标识；
- 列类型决定 JSON 表示；`numeric`/`bigint` 的精度与客户端解析行为必须显式验证，不能假设“数据库里是什么，JSON 里就是什么”；
- 视图可以成为稳定的“公开表面”，但必须注意 `security_invoker`；
- 函数参数与返回类型是 RPC 契约；
- `search_path`、角色、RLS 决定可见性。

PostgREST 本身通常不提供 `/v1` 版本前缀。版本化要靠：

- 独立 schema（`api_v1.orders`）并只暴露该 schema；
- 稳定视图；
- 反向代理/网关做路径重写；
- 或者在接受“数据库 schema 即版本”的前提下，用 Expand–Contract 管理变更。

### AF.4.2 Hasura：GraphQL 权限模型

Hasura 从数据库生成 GraphQL schema，并在其上定义：

- 角色（role）；
- 表/视图/函数的 select/insert/update/delete 权限；
- 行级过滤（基于会话变量）；
- 列级可见性；
- 关系与聚合；
- Actions（自定义业务逻辑）；
- Event Triggers（变更事件）；
- Remote Schemas（联邦）。

一个权限配置的概念示例：

```yaml
# 概念示意，不是完整 Hasura 元数据格式
table: orders
role: customer
select:
  columns: [id, status, total, created_at]
  filter:
    customer_id:
      _eq: X-Hasura-User-Id
```

关键风险：

- 如果把 `x-hasura-role` 当作可信身份、却不校验 JWT 中的 `allowed-roles`，客户端可能选择更高权限的角色；
- admin secret 泄露等于全库权限；
- 会话变量必须由可信认证层注入，不能直接信任请求头；
- GraphQL introspection 可能暴露内部模型；
- 关联查询可能绕过表级权限，必须在关系层验证；
- 事件触发器与 Action 的凭据要有独立权限。

### AF.4.3 OData：查询语言即接口

OData 用一组标准查询选项表达查询：

```http
GET /Orders?$filter=Status eq 'Paid'&$select=Id,Total&$expand=Customer&$orderby=CreatedAt desc&$top=20
```

优点是标准化、工具生态成熟；缺点是：

- `$expand` 可能形成深层关联爆炸；
- `$filter` 表达能力接近查询语言，权限与成本控制更难；
- 不同实现的方言与分页行为不一致；
- 复杂查询可能绕过应用层缓存与限流假设。

### AF.4.4 Firebase：Security Rules 是唯一防线

Firebase 客户端直接访问 Firestore/RTDB，安全规则决定读写：

```text
// 概念示意
match /orders/{orderId} {
  allow read: if request.auth != null
              && request.auth.uid == resource.data.customerId;
  allow write: if false;
}
```

必须理解：

- **规则不是过滤器**。查询必须“可证明地”满足规则，否则整个查询被拒绝；
- 规则中不能安全地做跨文档任意查询；
- 自定义 claims 需要服务端签发，客户端不能自封；
- App Check 减少滥用，但不替代授权；
- 规则测试要覆盖“越权读”“越权写”“枚举”“批量导出”。

### AF.4.5 Headless CMS 与内部工具

Directus、Strapi 等通常提供：

- 内容模型 → REST/GraphQL；
- 角色与字段级权限；
- 工作流与审批；
- 文件与媒体管理；
- Webhook 与扩展。

风险集中在：

- 草稿/预览接口泄露未发布内容；
- 关联展开绕过字段权限；
- 管理端与公开端共用同一套 API；
- 富文本/HTML 字段的 XSS；
- 媒体 URL 可猜测、无签名、无过期。

---

## AF.5 安全：自动生成接口最容易出错的十个位置

### 1. 默认放行

很多生成器在“未配置策略”时不是默认拒绝。上线前必须验证：

```sql
-- 以匿名角色查询，必须返回空或错误
SET ROLE anon;
SELECT * FROM orders LIMIT 1;
```

### 2. `service_role` / admin key 进入前端

Supabase 的 `service_role` key、Hasura 的 admin secret、Firebase 的 Admin SDK 凭据，都能绕过权限模型。它们只能存在于服务端，且要有：

- 独立的 secret 管理；
- 使用范围限制；
- 轮换与泄露响应；
- 绝不进入客户端 bundle、日志、URL、错误信息。

### 3. 视图绕过 RLS

PostgreSQL 15 之前，视图默认以视图所有者权限执行，可能绕过底层表的 RLS。使用：

```sql
CREATE VIEW api.orders_public
WITH (security_invoker = true)
AS SELECT id, status, total FROM public.orders;
```

并确认数据库版本与生成器行为。

### 4. `SECURITY DEFINER` 函数与 `search_path`

```sql
-- 危险：search_path 可被调用方影响
CREATE FUNCTION get_user(uid uuid)
RETURNS users
LANGUAGE sql
SECURITY DEFINER
AS $$ SELECT * FROM users WHERE id = uid $$;
```

攻击者可能通过 `search_path` 注入同名对象。防御：

```sql
CREATE FUNCTION api.get_user(uid uuid)
RETURNS api.users
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, api
AS $$ SELECT * FROM api.users WHERE id = uid $$;
```

并限制函数执行权限：

```sql
REVOKE ALL ON FUNCTION api.get_user(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api.get_user(uuid) TO authenticated;
```

### 5. 批量赋值（Mass Assignment）

自动生成的 insert/update 往往接受整行字段。客户端可能提交：

```json
{
  "status": "paid",
  "total": 0,
  "role": "admin"
}
```

防御：

- 列级权限；
- 只暴露视图或 RPC；
- 用触发器/策略校验状态迁移；
- 对敏感字段使用独立端点与审批。

### 6. 列级泄露

即使行级权限正确，列也可能泄露：

- `select=*` 返回内部字段；
- GraphQL introspection 暴露 schema；
- 错误信息包含列名/约束名；
- 排序/过滤允许探测不存在列。

### 7. 实时订阅的授权

实时通道常在建立连接时认证，但不会在每次数据变更时重新授权。必须：

- 订阅时按租户/资源过滤；
- 权限变更时断开或重新认证；
- 不把整个表广播给所有客户端；
- 限制订阅数量与消息速率。

### 8. 文件与媒体

- 桶/目录是否公开？
- 下载 URL 是否可猜测？
- 上传是否校验类型、大小、内容？
- 是否允许覆盖他人文件？
- 图片处理是否可能 SSRF/DoS？

### 9. 边缘函数与自定义逻辑

BaaS 的边缘函数常常持有高权限数据库连接。需要：

- 每个函数独立的最小权限角色；
- 输入校验与超时；
- 不把请求头原样转发；
- 不把数据库错误直接返回；
- 依赖与 secret 的供应链安全。

### 10. 多租户隔离

只有 `tenant_id` 列不等于隔离。必须验证：

- 每个查询都强制租户过滤；
- 跨租户 ID 枚举返回 404 而非 403（避免存在性泄露）；
- 缓存键包含租户；
- 实时订阅、导出、搜索、聚合都带租户；
- 管理后台不能跨租户“顺便看看”。

---

## AF.6 性能与容量：编译器不会替你设计索引

自动生成 API 常见的性能问题：

| 问题 | 表现 | 处理 |
|---|---|---|
| 缺索引 | 列表接口随数据量线性变慢 | 按过滤/排序组合建索引 |
| `count=exact` | 每次列表都全表计数 | 改用估算、缓存或去掉精确计数 |
| Offset 分页 | 深页越来越慢且漂移 | 游标/keyset 分页 |
| 关联爆炸 | `select=*,*.*` 拉取笛卡尔积 | 限制嵌套深度与返回字段 |
| N+1 | GraphQL/嵌入关联逐条查询 | DataLoader、批量查询、视图 |
| 连接耗尽 | 每个请求一个连接 | 连接池/PgBouncer、最大连接数 |
| 长查询 | 一个报表拖垮在线流量 | 语句超时、队列、只读副本 |
| 实时扇出 | 一次写入广播给十万订阅者 | 分级频道、合并、背压 |
| 大结果 | 导出把内存打满 | 流式、分片、对象存储 |

### 语句超时与行数上限

```sql
ALTER ROLE authenticated SET statement_timeout = '5s';
ALTER ROLE authenticated SET idle_in_transaction_session_timeout = '10s';
```

生成器侧同时设置：

- 最大返回行数；
- 最大关联深度；
- 最大查询复杂度（GraphQL）；
- 单租户并发；
- 响应体大小上限。

### 查询计划要真的看

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT ...;
```

不要只看“有没有索引”，要看：

- 是否命中预期索引；
- 扫描行数与返回行数比例；
- 排序是否落盘；
- 关联是否 Nested Loop 放大；
- 是否有隐式类型转换导致索引失效。

---

## AF.7 Schema 演进：数据库迁移就是 API 变更

在自动生成接口里，`ALTER TABLE` 的破坏性远大于手写 API 的字段变更，因为：

- 列名直接是 JSON 字段名；
- 类型变化直接改变客户端解析；
- 删除列直接让旧客户端报错；
- 约束变化直接改变错误语义；
- 视图/函数签名是公开契约。

### 用 Expand–Contract 管理

```text
1. Expand：新增列/新视图，旧列保留，双写/回填
2. Migrate：客户端迁移，监控旧列使用
3. Contract：确认无调用后删除旧列/旧视图
```

### 用视图固定公开表面

```sql
CREATE VIEW api.orders AS
SELECT
  id,
  status,
  total_minor,
  currency,
  created_at
FROM public.orders;
```

公开 API 只读 `api.*` 视图；内部表可以自由重构。写操作通过函数或服务层暴露，避免直接暴露表。

### 迁移的安全检查

- 加索引用 `CREATE INDEX CONCURRENTLY`；
- 加非空列先加默认值/回填，再设约束；
- 大表变更评估锁时间；
- 迁移与应用发布解耦；
- 有回滚脚本；
- 在 CI 中对 schema 做 diff，检测破坏性变更；
- 生成类型（TypeScript/Go）并纳入构建。

### 版本化

如果必须保留多个版本：

```text
api_v1.orders  （视图）
api_v2.orders  （视图）
```

或者由网关把 `/v1/orders` 路由到不同 schema/服务。不要把版本号塞进表名再到处复制逻辑。

---

## AF.8 多租户、配额与计费

自动生成接口在多租户下要额外回答：

- 租户 ID 从哪里来？（JWT claim、子域名、API Key）
- 每条查询是否强制带租户？
- 每个租户的连接数、并发、行数、存储是否隔离？
- 超限时返回什么错误？
- 用量如何计量？

### 租户过滤的强制方式

```sql
CREATE POLICY tenant_isolation ON orders
USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

应用层每次请求设置：

```sql
SET LOCAL app.tenant_id = '...';
```

不要让客户端直接传 `tenant_id` 查询参数作为唯一依据。

### 配额

- 按角色/租户限制请求速率；
- 限制并发查询数；
- 限制单次扫描行数/字节数；
- 限制实时订阅数；
- 限制导出频率与大小。

### 计量点

- 请求数；
- 返回行数；
- 扫描字节；
- 存储；
- 实时消息数；
- 函数执行时间。

计量事件要能去重、迟到补算、按租户归因，详见[附录 V](v-metering-billing.md)。

---

## AF.9 可观测性与审计

数据库日志不等于 API 可观测性。至少需要：

- 请求级 request ID，并透传到数据库注释/日志；
- 按路由、角色、租户的延迟与错误率；
- 慢查询与查询计划；
- 权限拒绝事件；
- 实时连接数、订阅数、消息积压；
- 导出/批量任务；
- 管理操作审计。

PostgreSQL 侧：

```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

配合 `pgaudit` 或触发器记录敏感表的写操作。

### 审计要回答的问题

- 谁在什么时间、从哪里、读了/改了哪条数据？
- 用的是哪个角色/令牌？
- 是否经过审批？
- 是否能证明没有越权？

如果这些问题答不上来，自动生成接口不适合承载受监管数据。

---

## AF.10 决策矩阵：什么时候能用

| 场景 | 适合度 | 理由 |
|---|---|---|
| MVP / 原型验证 | 高 | 速度优先，失败成本低 |
| 内部管理后台 | 高 | 消费者可控，权限模型简单 |
| 内容型站点 / CMS | 高 | 模型稳定，读写模式清晰 |
| 数据密集型内部工具 | 高 | CRUD 为主，团队小 |
| 实时协作原型 | 中高 | 订阅开箱即用，但要评估扇出 |
| 面向合作伙伴的公开 API | 低 | 需要稳定契约、版本、SLA、配额 |
| 支付/金融/医疗核心路径 | 低 | 需要强审计、复杂规则、不可绕过的边界 |
| 多步骤业务工作流 | 低 | 生成器不擅长编排与补偿 |
| 高并发写入/复杂一致性 | 低 | 需要专门的数据访问与事务设计 |
| 需要精细计费的 API 产品 | 低 | 计量与配额通常要自建 |

一个实用判断：

> 如果“数据库 schema 就是产品契约”可以接受，自动生成接口是合理的；如果你需要在不改变数据库的情况下改变 API，或者需要在不改变 API 的情况下重构数据库，就应该加一层稳定的契约表面。

---

## AF.11 从自动生成迁移到手写/服务化

迁移的目标不是“全部重写”，而是**把不可控的部分逐步收回**。

### 第一步：固定公开表面

- 建 `api` schema；
- 用视图暴露稳定字段；
- 用函数暴露写操作；
- 撤销对内部表的直接访问；
- 生成 OpenAPI/GraphQL 文档并纳入评审。

### 第二步：加网关

- 统一认证、限流、配额、审计；
- 路径版本化；
- 隐藏内部 schema 与错误细节；
- 对 deprecated 字段加响应头。

### 第三步：抽出关键写路径

优先迁移：

- 支付/退款/库存等强一致写；
- 跨聚合工作流；
- 需要幂等键与状态机的操作；
- 需要审批与审计的管理操作。

读路径可以继续由视图/生成器提供。

### 第四步：对拍与切流

- 双读：新旧接口同时读，比较结果；
- 影子写：新路径写但不对外，比较副作用；
- 灰度：按租户/流量比例切；
- 回滚：保留旧路径与数据回滚方案。

### 第五步：退役

- 监控旧接口调用；
- 通知消费者；
- 设截止日期；
- 撤销权限；
- 保留审计记录。

```text
阶段 1：数据库直出
阶段 2：稳定视图 + 网关
阶段 3：关键写路径服务化
阶段 4：公开 API 契约 + SDK
阶段 5：数据库仅对服务开放
```

---

## AF.12 案例：从 Supabase 原型到生产 API

**背景**：一个 SaaS 团队用 Supabase 做 MVP，前端直接读写 `orders`、`customers`、`invoices`。

**第 1 个月：原型**

- 开启 RLS，但只对 `orders` 写了策略；
- 前端使用 anon key；
- 管理后台使用 service_role key（放在服务端）；
- 实时订阅订单状态。

**第 3 个月：第一次审计**

发现：

- `customers` 表未开 RLS，anon 可读全部客户；
- `invoices` 的视图以所有者权限执行，绕过 RLS；
- 前端 bundle 中出现了一个测试用 service_role key；
- `select=*` 把内部 `risk_score` 返回给客户；
- 列表接口 `count=exact` 在 50 万行时超时。

**修复**：

- 所有表默认拒绝，逐表加策略；
- 视图改为 `security_invoker`；
- 轮换所有密钥，加入 secret 扫描；
- 建立 `api` schema 与稳定视图；
- 列表改用游标分页，去掉精确计数；
- 增加语句超时与最大行数。

**第 6 个月：公开 API**

- 加网关，统一 API Key、限流、配额；
- 关键写路径（下单、退款）迁移到服务层，使用幂等键；
- 数据库只对服务与只读报表开放；
- 生成 OpenAPI 并对合作伙伴发布；
- 建立计量与账单。

**结论**：自动生成接口不是“临时方案”，而是**不同阶段的控制权分配**。原型阶段把控制权交给生成器是合理的；生产阶段必须逐步收回契约、权限、性能和计费的控制权。

---

## AF.13 检查清单

### 契约

- [ ] 明确公开表面是表、视图还是函数，并写入文档。
- [ ] 公开字段有稳定命名、类型与空值语义。
- [ ] 生成器版本与配置纳入版本控制。
- [ ] 有 OpenAPI/GraphQL schema 导出与 diff。
- [ ] 破坏性 schema 变更有 CI 检测与迁移计划。

### 安全

- [ ] 默认拒绝；匿名角色无法读取任何未授权数据。
- [ ] RLS/Security Rules 覆盖所有暴露的表/集合。
- [ ] 视图使用 `security_invoker`（或等价机制）。
- [ ] `SECURITY DEFINER` 函数设置安全 `search_path` 并限制执行权限。
- [ ] service_role/admin key 只在服务端，且有轮换与泄露响应。
- [ ] 列级权限阻止敏感字段泄露。
- [ ] 实时订阅按租户/资源授权，权限变更可撤销。
- [ ] 文件桶与下载 URL 有访问控制、类型校验与过期。
- [ ] 边缘函数使用最小权限角色，不转发不可信头。
- [ ] 有跨租户越权测试与自动化回归。

### 性能

- [ ] 列表接口有索引支持，避免全表扫描。
- [ ] 分页使用游标/keyset，避免深 offset。
- [ ] 关联深度与返回字段有上限。
- [ ] 有语句超时、最大行数、响应体上限。
- [ ] 连接池与最大连接数已配置。
- [ ] 实时扇出有背压与频道分级。
- [ ] 有慢查询监控与查询计划评审。

### 演进与运营

- [ ] 用 Expand–Contract 管理 schema 变更。
- [ ] 关键写路径有幂等与状态机。
- [ ] 有租户级配额、用量计量与计费归因。
- [ ] 有请求级可观测性与审计。
- [ ] 有从自动生成迁移到服务化的路线图。
- [ ] 公开 API 有版本、弃用与迁移指南。

---

## AF.14 小结

- 自动生成接口把 API 设计转移到数据库 schema、权限策略与生成器配置中，风险更隐蔽。
- 数据库 schema 就是契约；`ALTER TABLE` 就是公开 API 变更。
- 安全的核心是默认拒绝、最小权限、对象级授权与不可绕过的边界。
- 性能的核心是索引、分页、关联深度、超时与连接池，生成器不会替你做这些。
- 自动生成适合原型、内部工具与数据密集型后台；公开、强监管、强一致与复杂工作流需要稳定契约层。
- 迁移不是重写，而是逐步收回契约、权限、性能与计费的控制权。

相关章节：[第 3 章 契约的四个层次](../part-1-foundations/03-contract-boundary-product.md)、[第 10 章 写操作、幂等、并发与一致性](../part-1-foundations/10-writes-idempotency-concurrency.md)、[第 14 章 授权](../part-2-security-runtime/14-authorization.md)、[第 19 章 多租户与数据隔离](../part-2-security-runtime/19-multi-tenancy.md)、[第 20 章 安全纵深](../part-2-security-runtime/20-security-in-depth.md)、[第 30 章 契约优先](../part-3-protocols-architecture/30-contract-first.md)、[第 41 章 SaaS 平台 API](../part-5-domain-playbooks/41-saas.md)、[第 43 章 数据平台与分析 API](../part-5-domain-playbooks/43-data-platform.md)、[附录 R 实现架构与框架选型](r-implementation-architecture.md)、[附录 S 批量操作、导入导出与数据可携带性](s-bulk-import-export.md)

---

返回：[目录](../SUMMARY.md)
