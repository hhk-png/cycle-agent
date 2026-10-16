# 第 26 章 权限与安全

一个没配权限的 ClickHouse 实例,只要能被网络摸到,任何人就能 `DROP DATABASE`。这一章解决两件事:一是**怎么用 SQL 把这台机器管住**——建用户、发权限、按行过滤、给租户设上限;二是**怎么把攻击面收窄**——哪些端口、哪些表函数、哪些默认配置是必须先处理的。ClickHouse 的访问控制和"改配置文件再重启"是两代思路,这一章会把新那代讲透。

## 26.1 五个对象,一条继承链

ClickHouse 的访问控制由五个可创建的对象组成,它们的关系是理解一切的基础:

- **USER(用户)**:认证与被授权的主体。可以属于若干 ROLE。
- **ROLE(角色)**:权限的打包单位。角色可以授予角色,形成**嵌套**。
- **ROW POLICY(行策略)**:一张表上的行过滤条件,施加给某些用户/角色。
- **QUOTA(配额)**:一个时间窗口内允许消耗多少资源,施加给某些用户/角色。
- **SETTINGS PROFILE(设置档)**:一批设置的取值与约束,施加给某些用户/角色。

后三个都通过 `TO` 子句"绑定"到用户或角色上,所以一条权限问题的排查路径通常是:先看用户直接有什么,再看它挂了哪些角色,再看这些角色又挂了哪些角色(嵌套),最后看有没有 ROW POLICY / SETTINGS PROFILE / QUOTA 在起约束作用。

**角色要先建出来,才能被授予。** 五个对象里,ROLE 最容易被当成"本来就存在"——毕竟通篇都在给角色授权。其实它和 USER 一样是一条 DDL,`CREATE ROLE` 建出的角色默认**没有任何权限**:

```sql
CREATE ROLE [IF NOT EXISTS | OR REPLACE] name1 [, name2 [,...]] [ON CLUSTER cluster_name]
    [SETTINGS variable [= value] [MIN [=] min_value] [MAX [=] max_value] [READONLY | WRITABLE] | PROFILE 'profile_name'] [,...]
```

配套的是另外两条:`ALTER ROLE name SETTINGS ...` 改角色的设置约束、`ALTER ROLE name RENAME TO new_name` 改名;`DROP ROLE [IF EXISTS] names` 删除角色(和删用户一样,要先确保没人还在用它)。一条最小示例把角色的生命周期走一遍:

```sql
CREATE ROLE analyst_role;                          -- 建角色(默认无权限)
GRANT SELECT ON analytics.* TO analyst_role;       -- 给角色授权限(§26.4)
GRANT analyst_role TO analyst;                     -- 把角色授给用户
SET DEFAULT ROLE analyst_role TO analyst;          -- 设为登录默认激活(§26.8)
DROP ROLE IF EXISTS analyst_role;                  -- 删除
```

串起来看:**`CREATE ROLE` 建对象 → `GRANT ... TO role` 授权限 → `GRANT role TO user` 授给用户 → `DEFAULT ROLE` / `SET DEFAULT ROLE` 定默认激活集 → 会话里 `SET ROLE` 临时切换**。本节先把"对象"立起来,授权的粒度在 §26.4,运行时激活那一步在 §26.8。

权限本身是授予在**对象**上的:库、表、列。这给了 ClickHouse 比"库级全有全无"更细的表达力——一个分析师可以只有 `SELECT(a, b)` 而没有 `SELECT(c)`。

## 26.2 为什么用 SQL 管权限,而不是改配置文件

老做法是把用户写在 `users.xml` 里,改完要重启(或用 `SYSTEM RELOAD CONFIG` 重新加载)。ClickHouse 的现代做法是**SQL 驱动**(RBAC):`CREATE USER` / `GRANT` / `CREATE ROW POLICY` / `CREATE QUOTA` 都是普通 SQL,执行完立即生效,**不需要重启**。

它的好处不只是省一次重启:

- **可版本化**:建用户/发权限是一串 SQL,能进 Git、能 code review、能按环境重放。
- **可审计**:`system.grants` / `system.users` 能随时查"谁有什么"。
- **可复制**:SQL 创建的访问实体默认落在 `access_control_path`(默认 `/var/lib/clickhouse/access/`)下的**本地文件**里,启动时重放。要让它们在**多节点集群里自动同步**,靠的**不是** `ON CLUSTER`,而是 `user_directories` 里配一段**复制存储**——把访问实体存到 Keeper 上(`<user_directories><replicated><zookeeper_path>…</zookeeper_path></replicated></user_directories>`,详见 §26.16);这是**改配置 + 重启(或 reload)才生效**的机制。这里有两个必须知道的点:
  - **`ON CLUSTER` 默认并不被忽略,但可以被显式关掉**。有一个**会话 / 查询级**设置 `ignore_on_cluster_for_replicated_access_entities_queries`(属于 `ignore_on_*` 那一族会话设置,默认 **0 / false**)。默认不忽略,即针对访问实体的 `ON CLUSTER` 语句照常按集群语义执行;**只有把它显式设成 1** 时,这类语句才会**什么都不做、却不报错**——你以为在集群上建了用户,其实只有本节点生效。它存在的意义是给"迁移到复制访问存储"这个过程留一个开关(迁移期显式打开,吞掉多余的 `ON CLUSTER`),而不是默认替你同步。所以别把"给访问实体的 DDL 加 `ON CLUSTER` 就能一次同步"当稳妥做法:要么走复制存储,要么逐节点执行。
  - **它的成熟度要打个问号,不能当"已经无忧的稳定功能"**。社区里仍有 `ReplicatedAccessStorage` 报 **`UNKNOWN_ROLE`** 的竞态记录(角色/授权在节点间短暂不一致)。生产启用前务必在目标版本上验证,别默认它和本地 `access_control_path` 一样可靠。
- **与 `users.xml` 共存但有序**:`user_directories` 配置段决定"从哪里读用户",可以既有只读的 XML 用户,又有 SQL 用户。

一句话:把权限从"服务端配置"变成"库里的数据",它就获得了数据该有的一切好处。

## 26.3 `CREATE USER` 的完整写法

官方语法(节选,行内注释是本章加的):

```sql
CREATE USER [IF NOT EXISTS | OR REPLACE] name1 [ON CLUSTER cluster_name1]
        [, name2 [ON CLUSTER cluster_name2] ...]
    [NOT IDENTIFIED | IDENTIFIED {[WITH {no_password | plaintext_password | sha256_password | sha256_hash | double_sha1_password | double_sha1_hash}] BY {'password' | 'hash'}}
        | {WITH ldap SERVER 'server_name'}
        | {WITH kerberos [REALM 'realm']}
        | {WITH ssl_certificate CN 'common_name'}
        | {WITH ssh_key BY KEY 'public_key' TYPE 'ssh-rsa|...'}
        | {WITH http SERVER 'server_name' [SCHEME 'Basic']}]
    [HOST {LOCAL | NAME 'name' | REGEXP 'name_regexp' | IP 'address' | LIKE 'pattern'} [,...] | ANY | NONE]
    [VALID UNTIL datetime]
    [DEFAULT ROLE role [,...]]
    [DEFAULT DATABASE database | NONE]
    [GRANTEES {user | role | ANY | NONE} [,...] [EXCEPT {user | role} [,...]]]
    [SETTINGS variable [= value] [MIN [=] min_value] [MAX [=] max_value] [READONLY | WRITABLE] | PROFILE 'profile_name'] [,...]
```

**认证方式**,按推荐程度从高到低说:

- `no_password`:不认证。只该用在明确不暴露网络的场景,例如本地的一次性实例。
- `sha256_password`:最常用的选项。你给明文,服务端替你算 SHA-256,存下来的是 `sha256_hash` + `salt`——**明文不落盘**。`IDENTIFIED BY 'qwerty'` 不带 `WITH` 时用的就是服务端配置里的 `default_password_type`(本书按 `sha256_password` 写;**该默认值未在 25.3 上逐条核对,请以你版本的 `default_password_type` 服务端配置为准**)。
- `bcrypt_password`:抗暴力破解最强,适合弱密码容忍度低的场景。代价是密码**限长 72 字符**,且每次校验都做一次 bcrypt(工作因子默认 12,可在服务端 `<bcrypt_workfactor>` 调,范围 4–31)。
- `double_sha1_password`:只在需要兼容 MySQL 协议客户端时用,新系统没有理由选它。
- `plaintext_password`:**不推荐**。密码以明文形式写进 `access` 目录下的 SQL 文件——`cat` 一下就看到了。
- 其它:`ldap` / `kerberos` / `ssl_certificate` / `ssh_key` / `http`,对接外部认证体系时用。

**`HOST` 限制**:`HOST IP '192.168.0.0/16'` 只允许来自指定网段;`HOST LOCAL` 只允许本机;`HOST NAME` / `REGEXP` / `LIKE` 按主机名匹配;`HOST ANY`(默认)任意来源。也支持 `CREATE USER mira@'127.0.0.1'` 这种 `@` 简写(等价于 `HOST IP`)。**生产里优先用 `HOST IP`**:按主机名/正则匹配会引入额外的名称解析开销,而且在 DNS 被投毒时不可靠。

**其余子句**:`VALID UNTIL` 到期即失效,适合临时账号;`DEFAULT ROLE` 决定登录后**默认激活**哪些角色(注意:被授予≠默认激活);`DEFAULT DATABASE` 决定登录后落在哪个库;`GRANTEES` 限制这个用户能把权限再授予谁(默认 `ANY`);`SETTINGS` 是直接在用户上设设置,也能写 `MIN`/`MAX`/`READONLY`。

```sql
CREATE USER analyst
    IDENTIFIED WITH sha256_password BY 'a-strong-secret'
    HOST IP '10.0.0.0/8'
    DEFAULT ROLE analyst_role
    DEFAULT DATABASE analytics
    SETTINGS max_memory_usage = 2000000000 READONLY;
```

## 26.4 `GRANT` 的粒度与再授权

```sql
GRANT [ON CLUSTER cluster_name] privilege[(column_name [,...])] [,...]
    ON {db.table | db.* | *.* | table | *}
    TO {user | role | CURRENT_USER} [,...] [WITH GRANT OPTION] [WITH REPLACE OPTION]
```

三个粒度要分清:

```sql
GRANT SELECT ON analytics.* TO analyst_role;            -- 库级:该库所有表
GRANT SELECT ON analytics.events TO analyst_role;      -- 表级
GRANT SELECT(user_id, ts) ON analytics.events TO analyst_role;  -- 列级
```

列级授权有一个必须知道的边界。文档的例子很说明问题:授予 `GRANT SELECT(x, y) ON db.table TO john` 之后,`john` 能 `SELECT x`、`SELECT y`、`SELECT x, y`,**不能** `SELECT z`,也**不能** `SELECT *`——哪怕表里只有 `x`、`y`、`z` 三列,`SELECT *` 也会被拒(唯一例外:当表**只有** `x` 和 `y` 两列时,`SELECT *` 才等同于选中它们)。所以列级授权配 `SELECT *` 的客户端会很难用,要么改客户端,要么给更多列。

**`WITH GRANT OPTION`** 表示拿到权限的人可以**再授予**别人,但只能授予**同等或更小**范围的权限。这是"把授权能力下放"的机制,也是权限扩散的常见源头:一旦有人有了 grant option 又给了另一个角色,撤销时就得顺着链一路清。

`WITH REPLACE OPTION` 则是"用新权限**替换**旧权限",不写它是**追加**。改造一个角色的权限集时,不加 `REPLACE` 很容易留下不该有的旧权限。

给角色加权限、把角色给用户(以及角色给角色)是另一种 `GRANT`:

```sql
GRANT SELECT ON analytics.* TO analyst_role;
GRANT analyst_role TO analyst;                       -- 把角色给用户
GRANT analyst_role TO report_role WITH ADMIN OPTION; -- 角色给角色,并允许再授予
```

注意:把角色授予角色形成嵌套后,"这个用户到底有什么权限"就不再是看他一个人,而是要遍历整条角色链。

## 26.5 `SETTINGS PROFILE`:把设置钉死

设置档解决的问题是:**用户能改自己的设置**。默认情况下,用户 `SET max_memory_usage = 100000000000` 就绕过了你想给他设的上限。设置档用"约束"把这件事堵死:

```sql
CREATE SETTINGS PROFILE [IF NOT EXISTS | OR REPLACE] name1 [ON CLUSTER cluster_name1]
        [, name2 ...]
    [SETTINGS variable [= value] [MIN [=] min_value] [MAX [=] max_value]
        [CONST|READONLY|WRITABLE|CHANGEABLE_IN_READONLY] | INHERIT 'profile_name'] [,...]
```

四种约束的区别,这是本节的核心:

- `CONST`:值被**固定死**——用户改不了,其它更高优先级的档也覆盖不了。
- `READONLY`:用户改不了,但**更高优先级的档**可以覆盖它。
- `WRITABLE`:允许改(默认)。
- `CHANGEABLE_IN_READONLY`:即使在只读会话(`readonly = 1`)里也允许改。

`MIN` / `MAX` 是范围约束:既能给一个默认值,又能划出用户可调的上下界。官方例子:

```sql
CREATE SETTINGS PROFILE max_memory_usage_profile
    SETTINGS max_memory_usage = 100000001 MIN 90000000 MAX 110000000
    TO robin;
```

含义是:给 `robin` 一个默认 100000001 字节的上限,他可以在 90MB–110MB 之间自己调,但不能超出。**这就是租户隔离的关键**:用户改了也无效(超出范围会被 `SETTING_CONSTRAINT_VIOLATION` 拒绝),而不是靠"信任用户不去改"。

`INHERIT 'profile_name'` 让一个档从另一个档继承设置,适合做"基础档 + 特化档"的分层。

## 26.6 `ROW POLICY`:让每个租户只看自己的数据

行策略是"把 `WHERE` 条件绑到人身上"。语法:

```sql
CREATE [ROW] POLICY [IF NOT EXISTS | OR REPLACE] policy_name1 [ON CLUSTER cluster_name1]
        ON [db1.]table1 | db1.*
        [, policy_name2 ...]
    [FOR SELECT] USING condition
    [AS {PERMISSIVE | RESTRICTIVE}]
    [TO {role1 [, role2 ...] | ALL | ALL EXCEPT role1 [, role2 ...]}]
```

- `USING condition`:一行的条件算出来非零,这个用户才看得到这一行。
- `TO`:策略施加给哪些用户/角色。`ALL` 是所有用户(含当前用户),`ALL EXCEPT ...` 是排除若干。
- `AS PERMISSIVE`(默认)/ `AS RESTRICTIVE`:多个策略叠加的方式。

**叠加规则**是行策略最容易踩错的地方。规则是:

```text
行可见 = (至少一个 PERMISSIVE 策略的条件为非零) AND (所有 RESTRICTIVE 策略的条件都为非零)
```

也就是说,**默认策略之间是 OR,标记为 RESTRICTIVE 的用 AND 收紧**。例如 `pol1 USING b=1 TO mira, peter` 与 `pol2 USING c=2 AS RESTRICTIVE TO peter, antonio`,`peter` 只能看到同时满足 `b=1` 和 `c=2` 的行;而 `mira` 只看 `b=1`。

**一个必须知道的坑**:一旦某张表上定义了**任何**行策略,该表的访问就**完全由行策略决定**,不管策略是不是针对当前用户定义的。文档的例子:只写了 `CREATE ROW POLICY pol1 ON mydb.table1 USING b=1 TO mira, peter`,那么 `mira`、`peter` 只看得到 `b=1` 的行,而**没被提到的 `paul` 一行都看不到**。想补救不能靠"再加一条 `USING 1 TO ALL EXCEPT mira, peter`"——那属于在错误的机制上打补丁。正确做法是用 `ALL EXCEPT` 从一开始就把策略范围写全,或者别对这张表用行策略。

还有一个前提:**行策略只对"只能读"的用户有意义**。如果一个用户能 `ALTER TABLE ... DELETE`、能复制分区、或者有 `INSERT`,他就能绕过过滤(删掉看不见的行、或者把数据搬走)。所以行策略必须和"只给 `SELECT` 且禁掉一切写操作"配合使用。

关于 `FOR` 子句:官方 `CREATE ROW POLICY` 语法的 `FOR` 后面**只列了 `SELECT`**——行策略是**读取侧的过滤**,**没有 `FOR INSERT` 这个子句**。写入侧"谁能写哪些行"不由行策略表达:要限制写入,靠的是不给 `INSERT` 权限、把主体约束成只读(这正是上面"行策略只对只能读的用户有意义"那条的另一个侧面)。把 PostgreSQL 的 `CREATE POLICY ... FOR INSERT` 套到 ClickHouse 上,是一个常见的张冠李戴。

一个"每个租户只看自己数据"的完整例子:

```sql
-- 每行带一个 tenant 列,策略把它和当前用户绑起来
CREATE TABLE analytics.events
(
    tenant      String,
    event_time  DateTime,
    payload     String
)
ENGINE = MergeTree ORDER BY (tenant, event_time);

-- 只读角色 + 只读用户
CREATE ROLE tenant_role;
GRANT SELECT ON analytics.events TO tenant_role;

CREATE USER tenant_a IDENTIFIED BY 'pw-a' DEFAULT ROLE tenant_role;
CREATE USER tenant_b IDENTIFIED BY 'pw-b' DEFAULT ROLE tenant_role;

-- 行策略:一个用户只能看自己 tenant 的行
CREATE ROW POLICY tenant_isolation ON analytics.events
    FOR SELECT
    USING tenant = currentUser()
    AS PERMISSIVE
    TO tenant_role;
```

`currentUser()` 是 SQL 里能直接用的函数,返回当前登录用户名,所以策略能写成"数据里的 tenant 等于我"。生产里更常见的做法是让用户表里存 `tenant` 映射,或用一个字典把用户映射到租户 ID——但骨架就是上面这样。

**两条边界必须记住。** 第一,**行策略不作用于 `system` 表**:它绑在你 `ON db.table` 指定的那张表上,`system.*` 里的表不受任何行策略约束。所以"行策略帮我挡住了跨租户数据"这句话,对 `system.query_log` 这类表**不成立**——一个能读 `system.query_log` 的用户能看到所有用户的查询文本,行策略帮不上忙。要隔离这类表的访问,靠的是 `GRANT`(别给他 `SELECT` 权限)而不是行策略。第二,**`currentUser()` 对内部查询返回 `default`**:当服务端自己触发一条查询(后台任务、物化视图的刷新、分布式查询在远端节点上的执行)时,没有"登录用户"这个上下文,`currentUser()` 会返回 `default`。所以如果你的行策略写的是 `tenant = currentUser()`,而数据又不是通过用户直接查询进来的,策略可能按 `default` 去匹配——排查"后台写入或 MV 处理时行为不对"时,这一条是常见根因。

## 26.7 配额

配额(QUOTA)在语法上也是"第五个对象",用 `CREATE QUOTA ... TO role` 绑定,和行策略/设置档一个套路。它的语义、`KEYED BY`、窗口、`system.quotas` / `system.quota_usage` 的用法已经在第 25 章 §25.5 讲完了,这里只补"从权限体系这一侧看配额"的三点,并强调一句:**配额是跨查询累积的,限制设置是单查询的**——两者经常被一起用,但解决的问题不同。

**其一,配额对象本身也在权限模型里。** 建、改、删配额,要对 `QUOTA` 这个对象有 `CREATE QUOTA` / `ALTER QUOTA` / `DROP QUOTA` 权限,而且当前账号的 `access_management` 得为 1(§26.16);否则报的是"没权限"而不是语法错。`CREATE QUOTA` 的形态精简成一行是 `CREATE QUOTA name [KEYED BY ...] FOR INTERVAL n unit MAX ... TO {role | user | ALL}`,细节见 §25.5——记住它也是一条**会被 RBAC 管**的 DDL,不是"配了就在那儿"的全局参数。

**其二,"挂给谁"和行策略 / 设置档一样靠 `TO`。** 排查"为什么这个用户被限速"的路径是:先用 `system.quotas` 看配额定义(含它落在谁头上的 `apply_to_all` / `apply_to_list`),再用 `system.quota_usage` 看该用户当前窗口的累计用量与上限(§25.5),必要时回头查 `system.users` / `system.roles` 确认这个用户挂了哪些配额。顺带一个安全提醒:`TO ALL` 会把额度摊给所有人,包括你没想到的新账号,通常不如显式列角色。

**其三,配额与设置档是"两个方向的约束"。** 设置档管"单条查询能改哪些设置、范围多大"(§26.5),配额管"一个窗口内一个主体一共能用多少";前者防的是"把机器用爆",后者防的是"被某个租户刷爆"。它们常成对配在同一个租户上:一个限单查询的 `max_memory_usage`,一个限窗口内的总查询数。

## 26.8 角色的运行时激活:`SET ROLE` 与 `DEFAULT ROLE`

前面反复强调"被授予 ≠ 默认激活"。这一节把"激活"这件事的运行时操作补齐——它是权限排查里"我明明给了角色为什么查询还报没权限"的常见答案。

一个用户可以被授予很多角色,但一次会话里**只有一部分是激活的**。查看和切换:

```sql
SELECT currentRoles();                    -- 当前会话激活了哪些角色
SELECT * FROM system.current_roles;       -- 同上,系统表视角
SET ROLE analyst_role;                    -- 激活一个角色(只影响当前会话)
SET ROLE ALL;                             -- 激活所有被授予的角色
SET ROLE NONE;                            -- 全部停用
SET ROLE DEFAULT;                         -- 回到默认激活集
```

三件必须分清的事:

**其一,`SET ROLE` 只影响**当前会话**,不改变持久定义。** 它是会话级的:连接断开即失效,下次登录又回到默认激活集。想把"默认激活哪些"改掉,要动的是用户/角色的 `DEFAULT ROLE`,不是 `SET ROLE`。

**其二,`DEFAULT ROLE` 决定登录时激活哪些角色。** 可以在建用户时写 `CREATE USER ... DEFAULT ROLE analyst_role, other_role`,也可以用 `SET DEFAULT ROLE` 在运行时改:

```sql
SET DEFAULT ROLE analyst_role TO analyst;          -- 把默认激活集设为这一个
SET DEFAULT ROLE ALL TO analyst;                   -- 所有被授予的角色都默认激活
SET DEFAULT ROLE ALL EXCEPT report_role TO analyst;-- 除某个之外都默认激活
SET DEFAULT ROLE NONE TO analyst;                  -- 默认一个都不激活
```

`ALL` / `ALL EXCEPT` 是**动态**的:以后新授予的角色会自动纳入默认激活集(或自动被排除)。这里有个安全含义——`ALL EXCEPT` 比逐个列举更"松",新增角色不需要回来改配置就被激活了。

**其三,`system.users` 里能看出默认激活集。** 三列合起来表达:`default_roles_all`(是否 ALL)、`default_roles_list`(显式列出的)、`default_roles_except`(被排除的)。一个账号如果 `default_roles_all = 1`,意味着它被授予的每一个角色登录即生效——这是权限过大最常见的来源,值得定期查。

## 26.9 权限从哪来:解析顺序与 `REVOKE` 的坑

一个用户登录后,他的**有效权限 = 自身的权限 + 其所有角色的权限(含角色嵌套,递归展开)**。所以回答"他为什么能删表",不能只看这个用户,要顺着角色链走一遍。

排查用这几条:

```sql
SHOW GRANTS;                 -- 当前用户的有效权限
SHOW GRANTS FOR analyst;     -- 指定用户
SHOW CREATE USER analyst;    -- 用户的完整定义
```

`SHOW GRANTS` 会返回一条合并后的 `GRANTS FOR CURRENT_USER()` 语句,是"总账"。

**`REVOKE` 的两个坑**:

第一,**角色链要整条考虑**。你从 `analyst_role` 撤掉 `SELECT`,但如果 `analyst` 还挂在另一条角色链上也有 `SELECT`,他照样能读。撤销要覆盖**所有**来源。

第二,**可以用更宽的范围去撤**。文档明确说:如果用户有 `SELECT (x,y)` 权限,你可以用 `REVOKE SELECT(x,y) ...`、`REVOKE SELECT * ...`、甚至 `REVOKE ALL PRIVILEGES ...` 来撤掉它。反过来,也可以只撤一部分——"部分撤销"(partial revoke):

```sql
GRANT SELECT ON *.* TO john;
REVOKE SELECT ON accounts.* FROM john;      -- 除了 accounts 库,其它都能读
```

`REVOKE` 去给角色也要用对应的语法:

```sql
REVOKE [ON CLUSTER cluster_name] [ADMIN OPTION FOR] role [,...]
    FROM {user | role | CURRENT_USER} [,...] | ALL | ALL EXCEPT ...
```

## 26.10 视图的 `SQL SECURITY`:一个被忽视的提权面

视图(`CREATE VIEW`)不是"一只眼睛",而是"一段以某个身份执行的查询"。用谁的身份执行,由 `SQL SECURITY` 决定——这正是提权的入口。

```sql
CREATE VIEW v_secret
    [DEFINER = some_user]
    SQL SECURITY {DEFINER | INVOKER}
AS SELECT ... FROM secret_table;
```

- **`SQL SECURITY DEFINER`**:视图以**定义者**(视图的创建者,或 `DEFINER =` 指定的那个用户)的权限执行。哪怕查询者自己没有 `secret_table` 的 `SELECT`,只要能查这个视图,视图内部的读取就借用**定义者**的权限完成。
- **`SQL SECURITY INVOKER`**:以**调用者**的权限执行。调用者没有底表的权限,查这个视图就会失败。

**为什么这是经典的提权面?** 设想管理员用高权限账号建了一个视图,把某张敏感表的一部分列暴露出来,本意是"只让分析师看这几列"。如果这个视图是 `DEFINER`,而视图的定义里(或它引用的事务里)触及了分析师本不该直接访问的别的表,那么**任何能查这个视图的人,都在借用管理员的权限读取底层数据**。更隐蔽的一种:一个低权限用户若能 `CREATE VIEW`(需要相应权限)且能让**高权限**用户替他建视图、或在 `DEFINER =` 里挂上高位账号,就等于把那份权限"借"了出来。

**防法**:给视图显式写 `SQL SECURITY INVOKER`,让"能不能查视图"和"能不能读底表"**基于同一个身份**,权限不会跨身份泄漏。只有在"确实要以定义者身份统一读取敏感表、且视图对外只暴露安全结果"时才用 `DEFINER`,并把这个视图的 `SELECT` 与 `GRANT` 一起审。

**默认值是什么(本节原先回避了这一点,这里给出)。**

- **普通视图默认 `INVOKER`**,由服务端/查询设置 `default_normal_view_sql_security` 控制;
- **物化视图默认 `DEFINER`**,由 `default_materialized_view_sql_security` 控制;
- `DEFINER` 里的那个"定义者"默认是 **`CURRENT_USER`**(也就是建视图的那个人),也可以显式写成 `DEFINER = some_user`。

所以**普通视图和物化视图的默认安全模型正好相反**——这是最容易被忽略的差异:一条 `CREATE VIEW` 默认以**调用者**身份跑(相对安全),一条 `CREATE MATERIALIZED VIEW` 默认以**定义者**身份跑(可能提权)。

**还有一条废弃提醒:`SQL SECURITY NONE` 已经废弃。** 早期版本里视图可以声明"不做权限检查",今天要走这条路必须被显式授权:先执行 `GRANT ALLOW SQL SECURITY NONE`(把这份能力授予某个角色),之后才允许创建 `SQL SECURITY NONE` 的视图。也就是说,默认状态下你**根本建不出** `SQL SECURITY NONE` 的视图——这是官方把这条危险路径收口的方式。

**一句话**:把每个视图都当成"一段以某个账号运行的代码"来审。审两样——谁建的(`DEFINER`)、以谁执行(`SQL SECURITY`)——而不是只看它 `SELECT` 了什么。既然普通视图与物化视图的默认值相反,写视图时最好**显式写上**,不要依赖默认。

## 26.11 命名集合:把连接凭证集中管理

读 S3、连远程 URL、接 MySQL 时,凭证写哪?直接写进查询 `s3(url, 'access_key', 'secret_key')` 会**泄漏进 `system.query_log`**(§26.15 会讲)。更好的做法是**命名集合(named collection)**:凭证集中定义一次,查询里只引用名字。

```sql
CREATE NAMED COLLECTION s3_prod AS
    url = 'https://bucket.s3.amazonaws.com/data/',
    access_key_id = 'AKIA-SOMETHING',
    secret_access_key = 'secret-value';

SELECT * FROM s3(s3_prod, format = 'Parquet');   -- 用名字引用
```

好处有三:**凭证只有一个副本**(改一次全生效)、**查询里看不到明文**(`query_log` 里记的是集合名)、**可以按集合授权**(让人能用它、不让他看内容)。管理用 `ALTER NAMED COLLECTION` / `DROP NAMED COLLECTION`;当前定义了哪些集合查 `system.named_collections`。它是**所有连接器共享的凭证机制**——S3、URL、远程数据库、消息队列都能引用同一个集合,不用每处重写一遍密钥。

按集合授权的写法是 `GRANT NAMED COLLECTION ON <集合名> TO <用户/角色>`(授到全部集合用 `ON *`)。**注意 `NAMED COLLECTION` 后面必须带 `ON`**——写成 `GRANT NAMED COLLECTION s3_prod TO ...`(漏掉 `ON`)是错的。当前有哪些集合、谁被授了权,查 `system.named_collections` 与 `system.grants`。

**注意边界**:命名集合解决的是"凭证怎么存、怎么引用",不改变"谁有权读哪些数据"。它和 `GRANT` 正交:你可能既给了某人某张表的 `SELECT`,又只让他用某个受限的命名集合。

## 26.12 外部认证与 TLS 的服务端配置

§26.3 里的 `IDENTIFIED WITH ldap SERVER '...'` / `kerberos` / `http SERVER '...'` / `ssl_certificate`,都**只是引用**;真正的连接参数定义在 `config.xml`(或其 `config.d/*.xml`)的服务端配置里。这些不在 SQL 里,但配错了认证根本走不通。

**LDAP**(`<ldap_servers>`)。定义 `host` / `port` / `bind_dn` / `bind_password`(绑定账号)/ 怎么把用户名拼成 DN 等;用户用 `CREATE USER ... IDENTIFIED WITH ldap SERVER 'ldap_server_1'` 引用这个名字。更进一步,LDAP 能做**用户目录与角色映射**:通过 `user_directories` 里的一段 `<ldap>`(带 `search_filter` / `role_mapping`),把 LDAP 组映射成 ClickHouse 角色——"在 LDAP 里加人"就等于"在 ClickHouse 里授权",两边不用各维护一遍。

**Kerberos**(`<kerberos>`)。服务端侧配 `principal` 与 `realm`,用户用 `IDENTIFIED WITH kerberos REALM '...'` 引用。

**HTTP 外部认证**(`<http_authentication_servers>`)。把认证交给一个外部 HTTP 服务,用户用 `IDENTIFIED WITH http SERVER '...'` 引用。

**OIDC(OpenID Connect)——现代 SSO 的主流方式。** 如果说 LDAP / Kerberos 是老一代企业目录,今天对接 Google / Azure AD / Okta / Keycloak 这类 SSO,靠的几乎都是 OIDC。用户用 `IDENTIFIED WITH openid` 声明走 OIDC;真正的连接参数配在服务端配置里的一组 `oidc_*` 键(`issuer`、`client_id`、`client_secret` 以及若干端点/claim 映射项)。典型用法是"SSO 侧认证、ClickHouse 侧只认身份"——用户在 IdP 登录拿到 token,ClickHouse 校验 token 并把它映射成本地用户/角色。**OIDC 的配置键名与默认值随版本仍在演进,本书不逐项展开,请以你版本的 OIDC 服务端配置文档为准(未确认)。** 要点是**先知道这条路存在**:要把 ClickHouse 接进企业 SSO,优先查 `openid`,而不是回头去搭 LDAP 或自研认证网关。

**客户端证书认证**(`<openSSL>` + `ssl_certificate`)。服务端在 `<openSSL>` 段配 CA(`caConfig`)、证书文件、私钥文件,开启 TLS 端口;用户用 `IDENTIFIED WITH ssl_certificate CN '...'`,以客户端证书的 CN 认人——机器到机器用证书互相认证、免密码的常见做法。

**TLS 深化:别只开客户端端口。** 三个常被漏掉的点:

- **节点间流量默认是明文的。** 副本之间传 part 走 interserver 端口,老配置下不加密。要加密,除了 `https_port`(HTTP over TLS)和原生 TLS 端口(如 9440),还要开 **`interserver_https_port`** 这条**节点间加密**通道,否则"客户端到节点"加密、"节点到节点"裸奔,内网嗅探照样拿到 part 内容。
- **证书要能轮换。** 证书不是配一次就不管:到期前要能换。做法是提前把新证书放到 `<openSSL>` 指向的路径(或临时双份配置),reload 后灰度验证,再撤旧的。没有轮换流程的团队,往往在证书过期那天才发现整个集群连不上。
- **Keeper 要配同一套。** 用内置 `clickhouse-keeper` 时,它的 `<openSSL>` 是**独立的一段**——给 server 配了证书不会自动带给 Keeper。客户端到 Keeper、以及 Keeper 节点之间的 TLS,要在 Keeper 的配置里**再写一遍**。漏这一步,加密链路就断在协调服务上。

## 26.13 静态加密(encryption at rest)

TLS 保护"传输中"(in transit),静态加密保护"落盘时"(at rest)——磁盘被偷、快照被拿走、云盘被误挂载时,没有它,数据就是明文。

ClickHouse 的静态加密在**磁盘层**做:定义一个 `encrypted` 类型的 disk,它**包住**一块底层盘,写下去的数据(part、merge 产物、临时文件)都加密后再落底层盘。

```xml
<storage_configuration>
  <disks>
    <disk_local>
      <type>local</type>
      <path>/var/lib/clickhouse/</path>
    </disk_local>
    <disk_encrypted>
      <type>encrypted</type>
      <disk>disk_local</disk>                       <!-- 被包住的底层盘 -->
      <path>/var/lib/clickhouse_enc/</path>
      <key>000102030405060708090a0b0c0d0e0f</key>   <!-- 16 字节十六进制密钥 -->
    </disk_encrypted>
  </disks>
</storage_configuration>
```

然后用存储策略把表指到 `disk_encrypted` 上。要点:

- **密钥在配置里。** 服务端从 `config.xml` 读密钥,所以 `config.xml` 本身成了最高敏感的物件——必须和磁盘分开保管(进 KMS,或至少单独加密),否则"密文和密钥放一起"等于没加。
- **`encryption_codecs`** 是**列级**的更细一条路径:在服务端的 `<encryption_codecs>` 段里配一个密钥,列上挂加密编解码器,只加密指定列、其余列不付全盘加密的代价。适合"只有少数敏感列要加密"的场景。这块能力的成熟度与是否推荐生产使用,请以目标版本文档为准(**未确认**)。
- **和备份 / `FREEZE` 的关系最容易踩坑。** `encrypted` 盘加的密只保护**这份盘上的数据**;`BACKUP ... TO S3(...)` 导出的是**解密后的逻辑数据**(以目标版本行为为准,**未确认**),所以"本地加密了"不等于"备份也加密了"——对象存储侧要单独上服务端加密(SSE)或客户端加密。而 `FREEZE` 走**硬链接**,链的是加密后的 part 文件本身,所以 `shadow/` 里的副本**仍是加密的**,要读它照样得靠密钥。这两条方向相反,是静态加密落地时最常见的两个误解。

**KMS 集成与密钥轮换。** 上面第一条"密钥在配置里"在生产里是站不住的:把明文密钥写进 `config.xml`,等于把锁和钥匙放一起。正经做法是把密钥交给 **KMS**(云厂商的 Key Management Service,或 HashiCorp Vault 这类密钥管理服务):由它保管主密钥、按需交付数据密钥,`config.xml` 里不再留明文。**这里必须说准:ClickHouse 与具体 KMS 的集成方式(是把 `key` 字段填成 KMS 的 key ID、还是通过外部脚本/凭据代理取密钥)取决于版本与部署形态,本书不在这个取值格式上给死结论(未确认)**,请以你版本的 `storage_configuration` 与磁盘加密文档为准。

**密钥轮换则要提前规划,别等到"密钥要退休"那天才发现解不开历史 part。** `encrypted` 磁盘上的数据是"用写入当时那把钥匙加密的",轮换不是改一个字段就完事——可行路径有两条:要么让**新老密钥并存**(读用旧钥、写用新钥,直到存量都迁完),要么**重写存量数据**(把 part 重新读出来、用新钥落盘,是一次全量 IO)。无论哪条,都要有"先备份、再灰度、可回退"的流程(和 §26.12 的证书轮换一个道理)。这也是为什么加密方案要**在系统上线前**就把轮换设计好,而不是事后补。

## 26.14 用 system 表把权限看清楚

权限配置是"数据",所以能用 SQL 查。几张关键表:

- `system.users`:有哪些用户、`auth_type`(认证方式)、`host_ip` / `host_names` 等来源限制、`default_roles_all` / `default_roles_list` / `default_roles_except`。`default_roles_all = 1`(所有角色都默认激活)是权限过大最常见的来源,值得定期查。
- `system.roles`:定义了哪些角色。
- `system.grants`:权限的**明细表**,每行是一个"授予项"。关键列:`user_name` / `role_name`(授予给谁)、`access_type`(哪种权限)、`database` / `table` / `column`(作用对象)、`grant_option`(是否 `WITH GRANT OPTION`)、`is_partial_revoke`(这一行描述的是授予还是部分撤销)。要看"谁在哪一列上有什么",就查它。
- `system.row_policies`:行策略,含 `select_filter`(即 `USING` 条件)和 `is_restrictive`。
- `system.quotas` / `system.quota_usage`:配额定义与当前用量(见第 25 章)。
- `system.settings_profiles`:设置档,含 `num_elements`(这个档里有几条设置)。
- `system.named_collections`:已定义的命名集合(§26.11);值是否可见受 `display_secrets_*` 一组开关约束(见 §26.15)。

再加上两个函数:`currentUser()`(当前用户名)、`currentRoles()`(当前生效的角色数组)——它们让权限能参与查询逻辑(行策略就是这么用的)。当前角色的数据还能通过 `system.current_roles` 查。

```sql
-- 谁有带 grant option 的权限(权限扩散的源头)
SELECT user_name, role_name, access_type, database, table, column
FROM system.grants WHERE grant_option = 1;
```

## 26.15 安全加固清单

下面每一条都给出"为什么",因为这些默认值不理解清楚就很容易漏。

**网络**。这一条的**机制**比"默认监听什么"更值得记准:**如果配置里完全不写 `<listen_host>`,代码层的默认是监听所有地址**(`0.0.0.0`)。你现在看到的"ClickHouse 默认只监听 localhost",来源**不是代码默认值,而是官方分发的默认 `config.xml` 里本来就写了 `<listen_host>127.0.0.1</listen_host>`**——它把监听收窄到了本机。这两件事的差别在运维上很致命:**如果你自己写/覆盖了一份 config、又漏掉这一行,服务器就直接对所有网卡打开**——不是你主动暴露的,是"没写"造成的。所以要同时记住两件事:(1) 出厂 `config.xml` 是 localhost-only,这是**安全基线**,不是"引擎天生如此";(2) 一旦写了 `<listen_host>0.0.0.0</listen_host>` 或 `<listen_host>::</listen_host>`,服务器就暴露到网络上,这时**必须有防火墙**只放行可信网段访问 8123(HTTP)和 9000(原生 TCP)。老资料说的"ClickHouse 默认监听所有网卡"其实说的是**代码默认**,和出厂配置的表现不一样——两者都记着,才不会在自写配置时踩坑。基线始终是"先不暴露,要用再最小化放行"。

**TLS**。明文端口上的密码和查询内容都是裸奔。用 `config.xml` 的 `<openSSL>` 段配证书/私钥(以及 CA),把 HTTPS(如 8443)和原生 TLS(如 9440)开起来,然后把明文端口关掉。**还要单独给节点间流量开 `interserver_https_port`,并给 Keeper 配同一套 `<openSSL>`**——细节与证书轮换见 §26.12。为什么:内网也不等于可信,横向移动是常态,节点间和协调服务同样是攻击面。

**`default` 用户**。出厂配置里的 `default` 用户默认无密码,是最大的风险点。要么给它设强密码,要么直接禁用它、改用专门的账号。为什么:攻击者第一件事就是拿 `default` 试。

**只读账号**。只给分析/看板的账号 `SELECT`,并按需配行策略。设置侧可以用 `readonly = 1`(只读且不许改设置)或 `2`(只读但允许改设置)。为什么:行策略能被写权限绕过(§26.6)。

**密码不要上命令行**。`clickhouse-client --password 'my-pass'` 会把密码留在 shell 历史和进程列表里。用交互输入的 `--password`(不带值),或者用客户端配置文件。为什么:`ps`、`~/.bash_history`、CI 日志都是密码泄漏的常见出口。

**别让 secret 从查询里漏出去**。`system.query_log` 的 `query` 列会**原样保存查询文本**——里面的 URL、`INSERT ... VALUES` 内容、表函数参数都可能带凭据。ClickHouse 提供三件套来控制"显示 secret":格式设置 `format_display_secrets_in_show_and_select`、服务端配置 `display_secrets_in_show_and_select`、以及 `displaySecretsInShowAndSelect` 权限,**三者都满足**才看得到 secret。这组开关针对的是 `SHOW` / `SELECT` 输出里的 secret,不改变 `query_log` 里已经记下的查询文本——所以更稳的做法是:别把凭据写进查询,`log_queries` 该开还是开(它是排障的生命线)。

**名字上的一个提醒**:网上流传的设置名 `format_display_secrets_in_queries` 我在 25.3 文档里**没有确认到**;可确认的是上面这一组(`format_display_secrets_in_show_and_select` / `display_secrets_in_show_and_select` / `displaySecretsInShowAndSelect`),别把名字写错。

**Keeper 端口**。Keeper/ZooKeeper 用的端口(常见 2181、9181)如果暴露,攻击者可以直接改集群元数据。只让集群内部节点访问。为什么:它没有像客户端接口那样的细粒度权限。

**表函数的滥用面**。`remote()` 能发起跨节点查询;`url()` 能对任意 URL 发请求——**这是 SSRF**(服务端请求伪造)的经典入口,能拿它去打内网元数据接口、探测只有内网能到的服务;`file()` / `executable` 能读服务器上的文件。默认配置下这几类都可能可用,而且常常是"只给了 `SELECT` 权限"就能调用的。

这里必须纠正一个流传很广的错误对策:网上常说要"把危险函数的执行权与普通 `SELECT` 分开,收紧权限"——**ClickHouse 根本没有针对表函数的独立 `GRANT`**。你无法"授 `url()` 不给 `remote()`",因为权限模型里没有"表函数"这个对象。真正能用的控制是三样:其一,**`readonly` 级别**(§25.10)——`readonly = 1` / `2` 会限制会话能改什么,再配合别给不可信用户 `CREATE` / `INSERT` / `ALTER` 这类写权限;其二,**一批 `allow_*` 类设置**(用 `SELECT name, value FROM system.settings WHERE name LIKE 'allow_%'` 找到你这个版本里有的那些),把不需要的能力关掉;其三,**网络出口策略**——在服务器或防火墙层禁止 ClickHouse 进程去连不该连的地址(云元数据端点 `169.254.169.254`、内网管理口),这是对 SSRF 最直接有效的一道防线。为什么:ClickHouse 的权限模型管的是"能读哪些数据、能建哪些对象",粒度和"允许调用哪个函数"不对齐——所以收口只能在**设置**和**网络**这两层,不在 `GRANT` 里。

**通配符授权**。`GRANT SELECT ON *.* TO someone` 看着省事,但它把未来新建的每一张表也一并暴露了。为什么:权限的默认面会随时间扩大,而没人回来收紧。

**CVE 与升级策略**。ClickHouse 发版快,安全修复会随小版本发出来。要给版本定一条"多久必须跟上一次"的制度,订阅官方 release notes 里的安全条目,并在灰度环境验证后再升。为什么:等出事再升,修的是事故不是漏洞。

上面这些点,按"最容易被利用"排序排一遍——这就是优先处理顺序:

1. **暴露到网络的默认端口 8123 / 9000**——默认虽是 localhost,但很多部署为了图方便直接开 `0.0.0.0`,又没有防火墙。这是绝大多数"裸奔 ClickHouse"事故的入口。
2. **无密码的 `default` 用户**——与第 1 条叠加就是"开门迎客"。
3. **`URL` 表函数做 SSRF**——只给 `SELECT` 也能发起任意 HTTP 请求,打内网。
4. **`file()` / `executable` 表函数读任意文件**——配置不当就可能读到 `/etc/passwd`、私钥。
5. **过宽的通配符授予**——`*.*` 加上 `WITH GRANT OPTION`,权限会在不知不觉中扩散。
6. **查询日志里的敏感值**——`query_log.query` 会留下带凭据的查询文本,凡是能读 `system.query_log` 的人都能看到。
7. **Keeper 端口暴露**——集群元数据可被直接改写。

## 26.16 SQL 管权限的总开关:`access_management` 与 `user_directories`

这一章讲了一整套 `CREATE USER` / `GRANT` / `ROW POLICY` 的 SQL RBAC,却一直没回答一个前置问题:**这些 SQL 为什么能用、怎么打开、默认是否可用。** 这一节补齐——它是"能不能用 SQL 管权限"的总闸。

**总开关:`access_management`。** 它写在 `users.xml` 里的**用户**定义上(不是 `config.xml`),决定这个用户能不能执行访问控制类 SQL(`CREATE USER` / `GRANT` / `CREATE ROW POLICY` / `CREATE QUOTA` / `CREATE SETTINGS PROFILE` 等)。**默认是 0**(关)——出厂状态下,一个用户即便 `CREATE` 权限齐全,也**改不了访问控制**。要拿 SQL 管权限,先把管理账号的这个开关打开:

```xml
<users>
    <admin>
        <access_management>1</access_management>
    </admin>
</users>
```

一旦某个用户有了 `access_management = 1`,后续所有授权都可以用 SQL 完成,不必再回头改 XML。**新手最常见的困惑"为什么 `CREATE USER` 报权限错",答案几乎总是"当前账号的 `access_management` 还是 0"。**

**相关的服务端设置:`access_control_improvements`。** 它是 `config.xml` 里的一段,用来微调访问控制的行为(角色/授权在集群里的可见性、`SELECT *` 的权限判定方式等,具体子键随版本演进)。它不常动,但要知道它存在——很多"某个授权在集群里表现不一致"的问题,根子在这段配置。

**`user_directories`:用户从哪来,以及优先级。** §26.2 说 XML 用户与 SQL 用户能共存,机制就在这段配置:它是一个**有序列表**,ClickHouse 按**从上到下**的顺序查找用户,先匹配到的目录说了算。典型结构是"只读的 `users.xml` → SQL 建的本地目录(或 Keeper 复制存储,§26.2)→ 外部目录(LDAP)":

```xml
<user_directories>
    <users_xml><path>users.xml</path></users_xml>
    <local_directory><path>/var/lib/clickhouse/access/</path></local_directory>
    <!-- 或 <replicated><zookeeper_path>...</zookeeper_path></replicated> 走 Keeper 复制存储(§26.2) -->
    <ldap>...</ldap>
</user_directories>
```

当前生效的目录清单与顺序查 **`system.user_directories`**(列包括 `type` / `params` / `precedence`,细节以版本为准)——它是"为什么这个用户/角色没生效"最直接的证据:哪个目录排在前面、哪个目录根本没被解析到,一看便知。

**改完配置怎么不重启就生效。** 访问控制相关配置改完后:

```sql
SYSTEM RELOAD CONFIG;   -- 重新加载 config.xml(含 user_directories、access_control_improvements 等)
SYSTEM RELOAD USERS;    -- 重新加载用户/角色/权限定义(users.xml、本地 access 目录、外部目录)
```

改了 `users.xml`(XML 用户)用 `SYSTEM RELOAD USERS`;改了 `user_directories` 结构本身这类"配置骨架",要 `SYSTEM RELOAD CONFIG`(必要时再跟一条 `RELOAD USERS`)。两条都不是重启服务器,但当前账号要有对应权限。

## 26.17 `CREATE FUNCTION`:SQL UDF 与可执行 UDF 的攻击面

除了表函数,ClickHouse 还有一类容易被安全评审漏掉的扩展点:**用户自定义函数(UDF)**。它有两种,风险级别完全不同。

**其一,SQL UDF(表达式别名)。** `CREATE FUNCTION` 可以把一段表达式起个名字复用:

```sql
CREATE FUNCTION linear_equation AS (x, k, b) -> k*x + b;
SELECT linear_equation(1, 2, 3);   -- 7
```

它就是"存储的表达式宏",**不执行任意代码**,风险与你写在那条表达式里的函数等同——本身不是提权面,但要注意:**它有权限对象**。能不能建/删这类函数,由对 **`FUNCTION`** 这个对象的 `CREATE FUNCTION` / `DROP FUNCTION` 权限控制(在 `system.functions` 里能查到它们,区分它们的是 **`origin`** 那一列——取值如 `System` / `SQLUserDefined` / `ExecutableUserDefined`;注意 `system.functions` 里**没有** `source` 列)。**要留意:`origin` 这一列上游(含 25.3)已标注为 Obsolete / Unused(列的注释就是 "Obsolete.")——也就是它当前仍可用、但属于计划弃用的列,别在自动化脚本这类会长期依赖它的地方硬依赖它。**审查"谁能定义函数"时别漏了它。

**其二,可执行 UDF(executable UDF)——这才是真正的攻击面。** 它让 ClickHouse **调用服务器上的一个外部程序**(脚本或二进制),把数据管道过去、读回结果。配置在 `config.xml` 里 `<user_defined_executable_functions_config>` 指向的 XML 中,声明 `type="executable"`、`command`、`format` 等:

```xml
<functions>
    <function>
        <type>executable</type>
        <name>some_function</name>
        <return_type>String</return_type>
        <argument><type>UInt64</type></argument>
        <format>TabSeparated</format>
        <command>your_script.py</command>
    </function>
</functions>
```

风险在于:**这段配置让你(或任何能改配置的人)指挥 ClickHouse 服务进程去执行一个本地命令**。它带来的不是"读某张表的越权",而是**在数据库服务器上执行代码**——攻击面从"数据层"直接升到"进程层"。三个要点:

- **它的武器化程度取决于 ClickHouse 进程的权限。** 它和 §26.15 里的 `file()` / `executable` 表函数是同一类风险(能读文件、能起进程),而且是**常驻**的:配一次,每个调用它的查询都会触发。
- **它只能靠服务端配置控制,不能靠 `GRANT` 粒度收口**——权限模型里根本没有"某个具体的可执行 UDF"这个对象。能改 `config.xml` / `config.d/*.xml` 的人,加一个可执行 UDF 就等价于拿到服务器上的命令执行。**所以对 `config.xml` 的写权限本身就是最高权限,要像管理密钥一样管理它。**
- 官方把这类能力定位成**受信任的运维能力**,不是给普通用户的功能。生产里若有它,务必限制配置文件的可写范围,并在安全评审时把它和表函数 `file()` / `url()` 一起过一遍。

## 26.18 与 mini-clickhouse 的对照

**这一节最重要的一句是:mini-clickhouse 默认不强制鉴权、也没有网络认证。**

**对齐点**(mini 做出来的):`minich/access.py` 是 `AccessControl` 的实现,持有 `users` / `roles` / `quotas` / `row_policies` / `settings_profiles` / `grants` 六类集合;`CREATE USER` / `CREATE ROLE` / `GRANT` / `CREATE QUOTA` 这些解释器都调它,而 `system.users` / `roles` / `grants` / `quotas` 一族权限系统表从同一个 `ctx.access` 读出来(`Context.clone()` 让子上下文**共享**它)。所以只要建过对象,这些表**立即有数据**——一个对象都没建时它们才为空,那是合法的单用户教学状态,不是"权限系统表恒空"。

**简化点**(mini 留白的):

1. **它不做任何鉴权检查。** `ctx.user` 默认是 `"default"`,没有任何地方会因为"你没权限"拒绝一条 `SELECT` 或 `DROP`。**必须把两件事分开**:权限对象(User / Role / Quota / RowPolicy / SettingsProfile)都能建、`system.*` 里都查得到;**但查询执行路径上不做强制**——`access.py` 里虽有 `check_access()` / `has_access()` / `apply_row_policies()` / `apply_column_policies()`,解释器**从不调用它们**。"建得出权限"是一回事,"查询时按权限拦人"是另一回事,mini 只做了前者。
2. **它没有网络认证。** 即便走 `minich/client.py` 起一个 TCP 服务,也不是 `clickhouse-server` 那套 TLS + 用户目录认证,别拿它当"能挡人的服务端"。
3. **系统表 schema 有意收窄。** 真实 `system.users.auth_type` 是 `Array(Enum8)`(一个用户可以有多种认证方式),`host_ip` 是 `Array(String)`;mini 里分别是 `String` 与单个 `String`(文档里如实标了"只保留核心列")。
4. **配额只被记录、不执行**(见第 25 章 §25.12)。

想在这个实现上做**真正的强制鉴权**,你得自己在解释器的执行路径上接一个检查点(比如调用 `access.py` 里已经写好、却没被解释器用起来的 `check_access()` / `apply_row_policies()` / `apply_column_policies()`)。第 34 章 B.7 的练习里并没有现成的一道(它那九个练习都不是关于访问控制的),但可以照着同样的路子给自己出一道:在改写解释器时顺手把这条检查点接上,再用一条本该被拒的 `SELECT` 验证它确实拦得住。

## 26.19 小结

- 访问控制是五个对象:USER / ROLE / ROW POLICY / QUOTA / SETTINGS PROFILE。后三个通过 `TO` 绑定到用户或角色,角色可以嵌套。
- SQL 驱动是这一代的做法:改权限不用重启、可版本化、可审计。它把权限从"服务端配置"变成了"库里的数据"。**前提是总开关 `access_management`(默认 0)为当前账号打开**(§26.16)。
- 多节点同步访问实体靠 `user_directories` 里的**复制存储**(`<replicated><zookeeper_path>`),**不是 `ON CLUSTER`**;那个能让 `ON CLUSTER` 静默失效的 `ignore_on_cluster_for_replicated_access_entities_queries` 是**会话 / 查询级**设置、默认 **0(不忽略)**,只有**显式设成 1** 才让针对访问实体的 `ON CLUSTER` 什么都不做却不报错。另外 `ReplicatedAccessStorage` 仍有 `UNKNOWN_ROLE` 竞态记录,成熟度需打问号(§26.2、§26.16)。
- `CREATE USER` 里,认证方式选 `sha256_password` 或 `bcrypt_password`;`HOST IP` 做来源限制;`DEFAULT ROLE` 决定默认激活哪些角色;注意"被授予≠默认激活"——运行时用 `SET ROLE` / `SET DEFAULT ROLE` 切换。
- `GRANT` 的粒度到**列**:`GRANT SELECT(a, b)` 能拦住 `SELECT *` 和未授权的列。`WITH GRANT OPTION` 允许再授同等或更小范围——它也是权限扩散的源头。
- `SETTINGS PROFILE` 用 `CONST` / `READONLY` / `MIN` / `MAX` 把设置**钉死**,这是租户隔离里"用户改了也无效"的关键。
- `ROW POLICY` 用 `USING` 过滤行;多个策略默认为 OR,RESTRICTIVE 为 AND;**在表上定义了任何策略就会改变所有用户的可见性**。两条边界:行策略**不作用于 `system` 表**,`currentUser()` 对内部查询返回 `default`。
- 用户的有效权限 = 自身 + 所有角色(递归)。`REVOKE` 要顺着整条角色链撤,并且可以用更宽的范围去撤。
- 视图是提权面:审每个视图的 `DEFINER` 与 `SQL SECURITY`。**普通视图默认 `INVOKER`、物化视图默认 `DEFINER`(定义者默认 `CURRENT_USER`),两者相反**;`SQL SECURITY NONE` 已废弃,要 `GRANT ALLOW SQL SECURITY NONE` 才能建(§26.10)。
- 凭证集中管理用**命名集合**(`CREATE NAMED COLLECTION`),让 `query_log` 里看不到明文;外部认证(LDAP / Kerberos / HTTP / **OIDC** / 客户端证书)的参数在服务端配置里;静态加密用 `encrypted` 磁盘,密钥应交给 **KMS** 并规划**轮换**,且**备份不自动继承加密**、`FREEZE` 的硬链接**仍是加密的**。
- **扩展点也要审**:`CREATE FUNCTION` 的 SQL UDF 有独立权限对象;可执行 UDF(executable)能指挥服务进程执行本地命令,只能靠"配置文件的写权限"收口,等于最高权限(§26.17)。
- 安全加固的优先级:别把服务暴露到网络(**不写 `<listen_host>` 时代码默认监听所有地址,"只监听 localhost"来自出厂 `config.xml`**,§26.15)、别留无密码 `default`、上 TLS(含 `interserver_https_port` 与 Keeper)、管好 Keeper 端口、别用 `*.*` 通配授权、盯住 `system.query_log` 里的敏感值。表函数的滥用面**没有函数级的 `GRANT` 可收**,只能在 `readonly` / `allow_*` 设置与网络出口两层收口。
- mini-clickhouse **默认不鉴权、无网络认证**,但这不等于"没有访问控制":权限对象(User / Role / Quota / RowPolicy / SettingsProfile)是**实现并接通**的——`access.py` 与 `get_access_control` 都在,建完之后 `system.users` / `roles` / `grants` / `quotas` 都有数据,只是**查询路径上不做强制检查**(见 §26.18)。
