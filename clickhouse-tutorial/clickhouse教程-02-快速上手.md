# 第 02 章 快速上手

读完前一章,你应该已经认同"它和 MySQL 不是一回事"。但从"认同"到"手上真有一张能查的表",中间还隔着四道小坎:装起来、配明白、会连着用、建出第一张表。

这一章把这四道坎一次跨过:先给三条安装路径(选哪条取决于你要的是长期实例、CI 里的一个二进制,还是连服务端都不要的嵌入式),再给最小可用的客户端操作与 HTTP 接口,然后是第一张事件流表与五条典型查询,最后收成一份"最小可用命令清单"。

这一章刻意**不讲原理**——排序键为什么该这么选、`LowCardinality` 到底省在哪,全部留到后面。它只求一件事:**让你先有一张真能查的表**。因为后面每讲一个机制,你都需要一个地方能立刻验证它;没有那张表,所有机制都只能停在被相信的层面。

## 02.1 安装与启动

想验证一个 ClickHouse 行为到底是不是这样,最省事的办法是先把它跑起来。装法有好几条,选哪条取决于你要的是"一台长期可用的实例""一个能塞进 CI 的二进制",还是"连服务端都不要、只想跑一句 SQL"。

**包管理器(.deb / .rpm)。** 官方为 Debian/Ubuntu 和 RHEL 系维护了 apt / yum 仓库(主机是 `packages.clickhouse.com`;添加源要导入 GPG key,那几行命令随发行版和版本变,请照官方安装页抄)。装完 `clickhouse-server` 和 `clickhouse-client` 之后,你得到的是一个标准的系统服务:配置在 `/etc/clickhouse-server/`,数据在 `/var/lib/clickhouse/`,日志在 `/var/log/clickhouse-server/`,可以用 `systemctl start clickhouse-server` 起停。要长期在一台机器上用、并且希望升级也走包管理器,这是首选。代价是它会改动系统状态(注册服务、建用户和目录),容器和 CI 里不合适。

**静态二进制(`.tgz`,或者 `curl https://clickhouse.com/ | sh`)。** 下载到的是一整个 `clickhouse` 可执行文件,它是**多合一的**:同一个二进制既能当服务端、当客户端,也能当免服务端的查询工具。适合 CI、容器和"我不想动系统"的场景。代价是它什么都不替你安排:没有默认配置目录、不注册服务、不建数据目录,路径都要自己给。

**Docker。** 官方镜像 `clickhouse/clickhouse-server`,适合"我想要一个能反复起停、随时删掉重来的本地实例"。镜像里的配置已经放开了监听地址,所以宿主机可以直接连上。

**`clickhouse install`。** 刚下载的多合一二进制自带这个子命令:它把二进制装进系统目录、生成一份默认配置、建好数据目录,并注册 `clickhouse` 这个管理入口(之后可以用 `clickhouse start` / `clickhouse stop` / `clickhouse status` / `clickhouse restart` 控制服务)。可以把它当成"没有包管理器时的 `.deb` 等价物"。

装好之后,日常真正会用到的跑法只有三种,区别在于"你要不要一个常驻的服务端"。

**方式一:Docker 单机(或前面装好的系统服务)。** 适合"我想要一个能反复用的本地实例"。一条命令拉起服务端,数据落在挂在容器外的卷里:

```bash
docker run -d --name ch \
  --ulimit nofile=262144:262144 \
  -v clickhouse-data:/var/lib/clickhouse \
  -p 8123:8123 -p 9000:9000 \
  clickhouse/clickhouse-server:25.3
```

`-v clickhouse-data:/var/lib/clickhouse` 这一行不能省:`/var/lib/clickhouse` 正是服务端存数据(以及库表元数据)的地方。不挂卷的话,数据只活在这个容器里,`docker rm` 一下就没了;`-v` 把它落到一个具名卷上,容器删掉重建数据依然在。想落到宿主机的某个目录,把 `clickhouse-data` 换成绝对路径即可。

`9000` 是原生 TCP 协议端口(`clickhouse-client` 默认连这里),`8123` 是 HTTP 端口(浏览器、`curl`、JDBC/ODBC 走这里)。另外两个常用默认端口是 `9004`(MySQL 协议)和 `9005`(PostgreSQL 协议)——这两个协议端口默认是关闭的,要手动打开(改法见 §02.2)。

`--ulimit nofile` 不是可选项:ClickHouse 一个 part 会打开很多文件描述符,默认的 1024 太低了,会在写入时随机报错。

`clickhouse/clickhouse-server:25.3` 这条 tag 是存在的(Docker Hub 上另有 `25.3-jammy` 以及更细的 `25.3.x.y` 之类变体)。如果某个具体 tag 拉不到(标签命名偶尔会变),先试 `:latest`,或者改用前面的二进制安装,并以镜像仓库当前列出的 tag 为准。

**方式二:多合一二进制前台跑。** 适合"我不想装 Docker,也不想配系统服务":

```bash
curl https://clickhouse.com/ | sh
./clickhouse server    # 启动服务端(前台)
./clickhouse client    # 另开一个终端连上去
```

前台跑的服务端,监听端口和数据目录都由配置文件决定:不指定就用它内建的一份默认配置(实际落在了哪里,以启动日志为准),想固定下来就用 `--config-file` 指一份自己的——把包管理器装的那套 `/etc/clickhouse-server/config.xml` 拿过来用也行。

**方式三:`clickhouse-local`(免服务端)。** 适合"我只想试一句 SQL,不想起任何东西"。它是同一个二进制的第三个入口,不监听端口、不起服务端进程,直接在本地文件上跑查询:

```bash
./clickhouse local -q "SELECT toTypeName(1), toTypeName(1.5)"
```

本书所有"验证一个行为"的片段,都可以用 `clickhouse local -q '...'` 跑。它和真实服务端的差别只在于:没有你建的库表、没有用户与权限。系统表这一层要说准确——`local` **默认就挂着 system 表**,`system.settings`、`system.functions`、`system.one` 之类可以查(不想挂就加 `--no-system-tables`);它缺的是服务端**运行期生成**的那些,`system.query_log`、`system.parts`、`system.processes` 在本地没有进程在写,因此为空或不存在。要建表灌数据,用方式一或方式二。

## 02.2 配置文件在哪、怎么改

服务端的配置分成两个文件,职责不同:

- `config.xml` —— **服务端自己的**设置:监听哪些地址和端口、数据目录、日志、磁盘与集群定义、并发与内存上限。
- `users.xml` —— **用户侧**的设置:用户、密码、来源网络、权限、配额,以及每个用户可以改哪些设置(第 26 章)。

用包管理器或 `clickhouse install` 装的实例,这两个文件都在 `/etc/clickhouse-server/` 下。**不要直接改它们**:默认配置是随包升级的,你改过的那几行在下次升级时会冲突或被打回。正确做法是往两个覆盖目录里丢片段:

- `/etc/clickhouse-server/config.d/*.xml`
- `/etc/clickhouse-server/users.d/*.xml`

服务端启动时先把 `config.xml` 读进来,再把 `config.d/` 下所有 `.xml` 按文件名顺序合并进去,同名节点默认**递归合并**(两边的子节点都在),标量值冲突时以后者为准;`users.d/` 对 `users.xml` 同理。可以把它理解成"配置的目录式补丁":升级时你只需要保证自己的片段还在,主文件长什么样与你无关。任意层级的节点上都能加 `replace="replace"`(整份替换)或 `remove="remove"`(删掉这个节点)来改变默认的合并行为。

举个最常用的例子:默认配置里服务端只监听回环地址,MySQL / PostgreSQL 协议端口也是关着的。想让别的机器连进来、顺便开掉那两个端口,就建一个片段:

```xml
<!-- /etc/clickhouse-server/config.d/network.xml -->
<clickhouse>
    <listen_host>0.0.0.0</listen_host>
    <http_port>8123</http_port>
    <tcp_port>9000</tcp_port>
    <mysql_port>9004</mysql_port>
    <postgresql_port>9005</postgresql_port>
</clickhouse>
```

改完重启服务端生效。这里有个必须连着一块改的地方:**放开监听地址只解决了"网络通不通"**,能不能从远程登进来还取决于 `users.xml` 里的 `networks` 段(第 26 章);只开端口不改用户配置,典型现象就是"端口通了但登录被拒"。另外,如果本机的 `localhost` 解析到 IPv6,只写 `0.0.0.0` 会让本机的 `clickhouse-client` 反而连不上,这时候把 `::` 也加上。

数据的落盘位置由 `path` 决定,包管理器装的默认是 `/var/lib/clickhouse/`:库表元数据、每个 part 的列文件,都在这个目录下按表分目录摆着。想"整个搬走"做备份或迁移,搬的就是它(第 29 章);日志默认在 `/var/log/clickhouse-server/`。

起停方式随装法而变:包管理器装的是 systemd 服务(`systemctl start|stop|restart clickhouse-server`);`clickhouse install` 装的用 `clickhouse start|stop|status|restart`;Docker 就是 `docker stop|start|restart ch`;前台跑的直接 `Ctrl-C`。

## 02.3 clickhouse-client 的必备操作

`clickhouse-client` 是最常用的入口。下面这些用法覆盖了日常九成场景。

**连接。** 默认连 `localhost:9000`、用户 `default`、空密码:

```bash
clickhouse-client --host localhost --port 9000 --user default --password
```

不带参数直接运行会进入交互模式,提示符形如 `hostname :)`。

**执行一条查询后立刻退出。** 用 `-q`(等价于 `--query`):

```bash
clickhouse-client -q "SELECT version()"
```

**一次执行多条语句。** 现版本默认就接受多条:用分号隔开即可,不需要任何开关(旧文档里的 `-n` / `--multiquery` 在 25.3 上已经废弃、什么都不做):

```bash
clickhouse-client -q "DROP TABLE IF EXISTS t; CREATE TABLE t (a UInt8) ENGINE = Memory; SELECT count() FROM t"
```

**参数化查询(`--param_<name>`)。** 如果你把一条查询当成模板反复调用,别用字符串拼接塞值,改用参数占位符:查询里写 `{名字:类型}`,命令行用 `--param_<名字>=值` 传:

```bash
clickhouse-client --param_name='Alice' \
  -q "SELECT * FROM users WHERE name = {name:String}"
```

好处是**值由服务端按声明的类型代入,不经拼接**,天然绕开引号转义与注入;占位符名字大小写敏感,`{name:String}` 与 `--param_name=...` 必须严丝合缝。类型也可以写成 `Array(UInt16)`、`Date` 这类,HTTP 接口上是同一套机制,只是参数放进 URL(`?...&param_name=Alice`)。名字对不上或类型不匹配会直接报错——比默默拼出一条错的 SQL 好排查得多。

**交互模式里直接输入 SQL。** 以分号结尾才会执行;分号可以换行分隔,适合粘贴多行语句。想重跑上一条查询,用上箭头从历史里调回来(要按关键词搜索历史用 `Ctrl+R`)。

**控制输出格式。** 交互模式下默认是 `PrettyCompact`(带边框表格);非交互模式下默认是 `TabSeparated`。要看别的格式,可以加 `--format`,也可以直接写在查询末尾:

```bash
clickhouse-client -q "SELECT 1 AS a, 'x' AS b FORMAT JSONEachRow"
clickhouse-client -q "SELECT number FROM numbers(3) FORMAT CSV"
```

**竖排输出。** 行很宽、想一行一个字段时,用 `\G` 结尾(等价于 `FORMAT Vertical`):

```sql
SHOW CREATE TABLE events\G
```

**切换当前数据库。** `\c <数据库>`,等价于 SQL 的 `USE <数据库>`。

**看有哪些库、哪些表。** `\l` 执行 `SHOW DATABASES`;`\d` 执行 `SHOW TABLES`。这些反斜杠别名是 2021 年前后陆续补进客户端的,现在的版本里都在。psql 那一套 `\dt`、`\di` 真正不存在。

容易踩的是 MySQL 习惯的 `\d <表名>`。它**不**翻译成 `DESCRIBE TABLE`,而且是版本敏感的,别照抄别处的经验。在本书基线 25.3 上,客户端对反斜杠命令做的是**纯文本前缀替换**:把开头的 `\d` 换成 `SHOW TABLES`,再接上后面那串字符,于是 `\d events` 被拼成一条**非法语句**。让它真正翻译成 `DESCRIBE TABLE` 是官方在**更晚的版本**里才补上的能力。所以要看表结构,别赌别名,直接写 `DESCRIBE TABLE <表名>` 最稳。写错的反斜杠命令也不会被识别成客户端命令,而是当成 SQL 原样发给服务端,然后给你一个语法错误。

这里有个限制必须提前知道:**这些反斜杠命令只在交互模式里生效。** 用 `-q`、管道或 `--queries-file` 跑批处理时,它们不会被当作客户端命令解释,只会原样发给服务端。所以脚本里最稳的写法永远是标准 SQL:`SHOW DATABASES`、`SHOW TABLES`、`SHOW DICTIONARIES`、`DESCRIBE TABLE t`——这些在任何版本、任何接口(HTTP、各种驱动)里都一样。

**看错误。** 出错的格式是 `Code: NNN. DB::Exception: ...`。`NNN` 是错误码,比如 `60` 是 `UNKNOWN_TABLE`、`81` 是 `UNKNOWN_DATABASE`。错误码比错误文案稳定得多,脚本里判断错误类型应该看码而不是看文案。

**看进度。** 交互模式下,长查询会实时刷新一个进度条(已读行数、字节数、耗时)。非交互模式默认不显示。

**判断它是不是真的卡住了。** 进度条不动不等于卡死。别干等,去服务端这一侧看:最直接的是 `SHOW PROCESSLIST`(等价于查 `system.processes`),它列出每条正在执行的查询——发起用户、已经跑了多久、读了多少行多少字节,以及它的 `query_id`。

```sql
SHOW PROCESSLIST;

SELECT query_id, user, elapsed, read_rows, formatReadableSize(read_bytes) AS read_bytes
FROM system.processes
ORDER BY elapsed DESC;
```

读得出来的三类信息:`read_rows` 还在涨,说明它只是慢,不是在等什么;涨了一阵就停住、`elapsed` 继续走,才可能是真的卡在某处(最常见的是在等写入侧的资源,而不是在等锁——ClickHouse 的读路径本来就不靠行锁);同一条 SQL 反复出现在列表里,那是上游在重试。

要停掉某一条,用 `KILL QUERY`,按 `query_id` 打:

```sql
KILL QUERY WHERE query_id = '<上面查到的 id>';
```

`KILL QUERY` 是"请求取消":查询会走到下一个检查点后退出,所以命令返回不代表它已经停了;要等它确实退出,加 `SYNC`。`SHOW` / `KILL` / `DESCRIBE` 这一组语句的完整用法在 `第 09 章 §09.12`,**中断别人发起的查询需要相应的权限**(第 26 章)。查询结束之后的追查是另一个入口:靠 `query_id` 去 `system.query_log` 里翻(§02.4 讲过怎么给查询起名字)。

**批量跑一个文件里的语句。** 语句写在 `.sql` 文件里、要一次跑完,用 `--queries-file`(可以给多个,按给出的顺序执行):

```bash
clickhouse-client --queries-file schema.sql
clickhouse-client --queries-file 01_schema.sql --queries-file 02_load.sql
```

它和 `-q` 的区别只在"SQL 从哪来",由此带来两个实际影响:一是**文件里的反斜杠别名不会被当成客户端命令**(本节开头那条限制同样适用),二是文件里可以自由换行、写注释,不必和 shell 的引号搏斗。`cat x.sql | clickhouse-client` 是同一件事的另一种写法。要一次灌大批数据则用 `INSERT ... FORMAT` 配 `--data-binary`(第 08 章 §08.5)。

**退出。** `\q` 或 `exit`,也可以 `Ctrl-D`。

## 02.4 HTTP 接口

除了 `clickhouse-client` 走的那条原生协议,服务端同时开着一条 HTTP 接口(默认 `8123`)。`curl`、浏览器、JDBC / ODBC,以及大多数语言的客户端库,默认走的都是它。判断它活着没有,最省事的是 `/ping`:

```bash
curl 'http://localhost:8123/ping'      # 返回 Ok.
```

在这条接口上,SQL 有两种给法:拼进 URL 的 `query` 参数(GET),或者直接当作 POST 的请求体。后者不用操心 URL 编码,长查询和 `INSERT` 都推荐用它:

```bash
# GET,空格要编码成 %20
curl 'http://localhost:8123/?query=SELECT%201'

# POST 请求体:SQL 原样写
curl 'http://localhost:8123/' --data-binary 'SELECT count() FROM events'

# 指定数据库与默认输出格式
curl 'http://localhost:8123/?database=demo&default_format=JSONEachRow' \
  --data-binary 'SELECT device, count() FROM events GROUP BY device'
```

认证有两种给法:URL 参数(`user=` / `password=`)或请求头。用头更安全,因为 URL 会进服务端日志、也会留在 shell 历史里:

```bash
curl -H 'X-ClickHouse-User: default' -H 'X-ClickHouse-Key: secret' \
  'http://localhost:8123/?query=SELECT%201'
```

默认安装里 `default` 用户是空密码,加上服务端默认只监听回环地址,所以在本机上 `curl` 通常什么都不用带;一旦按 §02.2 放开了监听地址,这两件事就都得重新审视。

写入走 `INSERT ... FORMAT <格式>`,数据放在请求体里:

```bash
curl 'http://localhost:8123/?query=INSERT%20INTO%20demo.events%20FORMAT%20CSV' \
  --data-binary @events.csv
```

这里用 `--data-binary` 而不是 `-d` 是有意为之:`-d` 会顺手把数据里的换行吃掉,文本和二进制格式都会被它改坏,只有 `--data-binary` 是原样上传。另外,会改数据的查询(INSERT、CREATE 等)必须用 POST 发——GET 请求在这个接口上按只读处理。

还有三个上手就会踩到的点,值得先记住。

**HTTP 是无状态的。** 默认每个请求都自成一个会话:上一条请求里 `SET` 过的设置、`USE` 过的库,下一条请求不会记得。要让多次请求共享一个会话,得在请求里带上 `session_id=<任意字符串>`,服务端就会把它们归到同一会话(默认 60 秒不活动就结束,可用 `session_timeout` 参数调整;同一会话里同一时刻只能跑一条查询)。想确认某个会话还在不在,可以加 `session_check=1`。

**给查询起名字(`query_id`)。** 请求里可以带一个 `query_id=<任意字符串>`,它会被记进 `system.query_log`。事后排查"这条慢查询是什么时候、谁发的"就靠它;驱动和监控通常会自动生成这个 ID,手工调试时自己起一个能认出来的名字,查日志会轻松很多。

**响应压缩。** 8123 上的大结果集默认是明文返回的。要让服务端压缩响应,请求得带上 `Accept-Encoding: gzip`(或别的压缩算法),同时服务端这一侧要打开 `enable_http_compression`(**默认关闭**);压缩级别由 `http_zlib_compression_level` 控制(默认 3)。压缩换来带宽、付出一点 CPU——跨机房取大结果集时通常很划算。

最后把几个端口记牢:`8123` 是 HTTP,`9000` 是原生 TCP,`8443` 是 HTTPS,`9440` 是原生协议的 TLS 端口。后两个要先配好证书才会监听。原生协议是二进制的、为批量数据块设计,通常比 HTTP 少一层开销,所以客户端和驱动应该优先走 `9000` / `9440`;而 `8123` / `8443` 的好处是哪儿都能用——调试、一次性脚本、语言没有对应驱动时,它就是保底手段。

## 02.5 第一张表:一份事件流

建一张最典型的分析表——事件流。注意每一列的选择都是有理由的,后面章节会逐条展开:

```sql
CREATE TABLE events
(
    event_time  DateTime,
    event_date  Date DEFAULT toDate(event_time),
    user_id     UInt64,
    url         String,
    referrer    LowCardinality(String),
    device      LowCardinality(String),
    country     LowCardinality(String),
    duration_ms UInt32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_date)
ORDER BY (event_date, device, user_id);
```

几个已经埋进去的决定:`DateTime` 而非 `String`,是因为时间列要被用来做分区和范围过滤;`referrer`/`device`/`country` 用 `LowCardinality`,是因为它们的取值只有几十个;`duration_ms` 用 `UInt32`,是因为毫秒数不会超过 40 亿;`ORDER BY (event_date, device, user_id)` 的顺序是"过滤频繁度"决定的——先按天、再按设备、再按用户。

灌 100 万行。不用生成文件,`numbers()` 直接造:

```sql
INSERT INTO events
SELECT
    now() - toIntervalSecond((number % 604800))                    AS event_time,
    toDate(event_time)                                             AS event_date,
    number % 100000                                                AS user_id,
    concat('/page/', toString(number % 1000))                      AS url,
    ['google', 'direct', 'twitter', 'weibo'][(number % 4) + 1]     AS referrer,
    ['mobile', 'desktop', 'tablet'][(number % 3) + 1]              AS device,
    ['CN', 'US', 'JP', 'DE'][(number % 4) + 1]                     AS country,
    number % 5000                                                  AS duration_ms
FROM numbers(1000000);
```

`numbers(1000000)` 是一个表函数,返回 0 到 999999 的单列。`number % 4` 这类取模让数据有重复,聚合才有意义。数组字面量加下标是造"枚举值"最方便的写法——注意**下标从 1 开始**,所以 `(number % 4) + 1`。

100 万行是刻意选的数字:它小到几秒钟就能灌完,又大到能看出聚合与点查的差别。要看数字更漂亮,把 1000000 换成 100000000——`numbers_mt()` 是多线程版本,更快。

## 02.6 五条典型查询

下面五条不是"SQL 示例集",而是分析型 SQL 的五块积木。每一条都只说清一件事:**它在教你什么、它和 MySQL / PostgreSQL 里的同类写法差在哪。** 后面凡是复杂查询,都是这五块拼出来的。

**其一,数行数。**

```sql
SELECT count() FROM events;
```

ClickHouse 的 `count()` 不带参数就是数行,不读任何列——行数直接从 part 的元数据里拿。它教的是这条最容易建立直觉:**"要读多少数据"是一个可以事先知道的量,不必先把数据读出来才数得清。** 和 MySQL / PostgreSQL 对照:那边的 `COUNT(*)` 也有优化,但那是"挑一个最小的索引扫一遍"换来的;这里连索引都不碰,元数据里就写着行数。顺手记一条纪律:要行数就用不带参数的 `count()`——`count(列)` 的语义是"数这一列里非 NULL 的行数",会退化成先读那一列再数。

**其二,分组聚合。**

```sql
SELECT device, count() AS c
FROM events
GROUP BY device
ORDER BY c DESC;
```

它教的是这台引擎的**基本形状**:`GROUP BY` 在内存里建一张哈希表,每个分组桶里存的是聚合的**中间状态**,不是最终值(`count` 的状态就是一个计数器)。这条决定了后面所有事——分组数一多,内存就和"分组数 × 状态大小"成正比,这是 `GROUP BY` 把内存打爆的根因(第 12 章、第 25 章)。和 MySQL / PostgreSQL 的差别不在语法,而在**它假设你总在写这种查询**:行存那边要扫全表、还要靠索引避免排序;这里扫的只是列,末尾那句 `ORDER BY c DESC` 排的也只是分组结果那一小段,而不是几百万行明细。

**其三,时间序列。** 按小时分桶——这是"时间序列查询"的最小骨架:

```sql
SELECT toStartOfHour(event_time) AS h, count() AS c
FROM events
GROUP BY h
ORDER BY h;
```

它教两件事。一是**时间分桶是一族具名函数**:`toStartOfHour`、`toStartOfDay`、`toYYYYMM`,粒度写在函数名里,不像多数数据库只有一个通用的截断函数需要多想一层参数。二是**分桶只能出现在 `GROUP BY` / `SELECT` 里**:一旦把列包进函数再放进 `WHERE`(`WHERE toStartOfHour(event_time) > ...`),列上的稀疏索引就用不上了。要按时间过滤,把区间谓词写回**原始时间列**:`WHERE event_time >= '...' AND event_time < '...'`(第 11 章)。这条纪律对从任何数据库迁过来的人都成立,只是在这里的代价特别明显——因为它会从"慢一点"变成"全表扫"。

**其四,去重计数。**

```sql
SELECT country, uniq(user_id) AS uv
FROM events
GROUP BY country
ORDER BY uv DESC;
```

`uniq` 是**近似**去重,用 HLL 之类的结构压内存,误差通常在千分之几;要精确就用 `uniqExact`,但它在大基数上会吃光内存。日常报表默认选 `uniq`,这是第 12 章的一条重要取舍。它教的是**这里最不同的一种迁移心态**:MySQL / PostgreSQL 里这件事只有 `COUNT(DISTINCT x)` 一条路,它必须维护一个精确集合,基数一大就把内存填满;ClickHouse 把它拆成"近似"和"精确"两个函数,把选择权交回给你——**多出来的不是语法,是一个必须自己做的取舍**。

**其五,排序 + 分位。** 找出访问量最高的十个页面,并给出响应耗时的 P95:

```sql
SELECT url, count() AS c, quantile(0.95)(duration_ms) AS p95
FROM events
GROUP BY url
ORDER BY c DESC
LIMIT 10;
```

`quantile(0.95)` 是**参数化聚合函数**——括号里是"参数",后面再跟一个括号才是"被聚合的列"。这个双括号的形态是 ClickHouse 函数家族的一个标志。它教的还有一件对照:分位数在 MySQL / PostgreSQL 里通常要绕一圈(`PERCENTILE_CONT` 得配窗口函数、`NTILE` 得先分批,或者把明细捞到应用层算),而在这里它**就是一个聚合函数**,和 `count`、`sum` 平起平坐——这正说明聚合状态是可自定义的,而不只是几个内置计数器。第 12 章会讲清楚它和 `quantileExact`、`quantiles(0.5, 0.9, 0.99)` 的关系。

## 02.7 五个立刻会用到的词

下面五个词在后面的每一章里都会出现。这一节给的不是定义,而是**每个词的第一个例子**——先会写,再讲为什么。选型与机制分别在对应章节里展开。

**`ENGINE`。** 决定这张表"怎么存、怎么写、怎么读",写在建表语句的末尾:`ENGINE = MergeTree ORDER BY id`。`MergeTree` 是默认答案,但还有 `ReplacingMergeTree`(按排序键去重)、`SummingMergeTree`(合并时预聚合)、`Memory`(只放内存)、`Distributed`(分片视图)等一大家子。要注意的是**它不是一个可以事后随手改的参数**:换引擎基本等于重建表。怎么选在 `第 04 章`,`MergeTree` 内部在 `第 05 章`。

**`ORDER BY`(排序键)。** 同两个词,位置不同、含义完全不同:出现在 `ENGINE = MergeTree` 后面的 `ORDER BY` **不是**查询里的输出排序,它声明的是"数据在磁盘上按这些列排序";出现在 `SELECT` 末尾的才是输出排序。作为存储决定,它同时是三样东西:排序键、稀疏索引的键、压缩的聚簇依据,列的顺序直接决定查询能裁剪掉多少数据。一个够用的判断法则:把**过滤最频繁、能砍掉最多数据**的列放前面。这是全书最容易被低估的一句话,展开在 `第 06 章`。

**`PARTITION BY`。** 把数据切成互相独立的物理分区,典型写法 `PARTITION BY toYYYYMM(ts)`。它的价值是**生命周期管理**:删掉一个月的数据等于删掉一个目录,比分批 `DELETE` 快几个数量级。代价是分区太多会拖慢元数据操作,而且每个分区至少一个 part——**分区数是"按月还是按天"这种数量级的选择,不是越细越好**。经验做法是分区粒度比查询最细粒度粗一档(按月分区、按天排序),见 `第 06 章`、`第 18 章`。

**`MATERIALIZED VIEW`。** 不是"缓存查询结果",而是"插入时顺带把新数据加工一遍写进另一张表":`CREATE MATERIALIZED VIEW mv TO dst AS SELECT ... FROM src`。用它可以做实时汇总:原始数据进来,汇总表立刻更新。两个必须记住的点:触发点是 **INSERT** 而不是 SELECT(查询 `mv` 不产生任何计算);因此它**不回填**建视图之前已经存在的数据——要补历史得另外做一次(`POPULATE` 能补一次,但它和并发写入的关系有坑)。`第 13 章` 整章讲它。

**`FORMAT`。** 查询末尾的 `FORMAT JSON`、`FORMAT CSV` 等,决定结果的序列化方式;不写时,交互模式默认 `PrettyCompact`(带边框表格)、非交互默认 `TabSeparated`。上手阶段只需要记住一条:`FORMAT JSONEachRow` 最容易喂给下游脚本。每种格式能不能原样读回来、有没有精度损失,是 `第 19 章` 的主题——它讲的是"可往返性",而不只是格式名清单。

## 02.8 从 MySQL / PostgreSQL 迁过来要改的五个心态

**没有主键约束。** `PRIMARY KEY` 在 ClickHouse 里只是"稀疏索引的声明",**不做唯一性检查**。插进去一样的行,就是进去两行。别指望数据库帮你防重复。

**没有唯一约束,去重靠引擎。** 要"同一主键只留最新一条",不是加约束,而是换 `ReplacingMergeTree` 引擎——而且它的去重是**后台合并时**发生的,查询时可能还没合并完。想立刻看到去重结果要加 `FINAL`,但那是有成本的(第 18 章)。

**`JOIN` 要小心。** ClickHouse 的 `JOIN` 默认把**右表**装进内存建哈希表。右表大了就爆内存、就慢。这和 MySQL 的优化器会自己选方向完全不同——在 ClickHouse 里,你得自己把大表放左边、小表放右边。第 15 章整章讲这件事。

**`NULL` 是贵的。** `Nullable(T)` 每行多一个字节的空位图,并且关掉一整类优化,还不能当排序键。ClickHouse 的惯例是**用类型自带的零值代替 NULL**:空字符串表示未知字符串,`0` 表示未知数值。只有"真的区分不了零值和缺失"时才用 `Nullable`。第 03 章 §03.9 有详细分析。

**更新是异步的。** `ALTER TABLE ... UPDATE` 不会立刻改数据,它提交一个 Mutation,后台把受影响的 part 重写一遍。提交后立刻查可能还是旧值。大批量修数据前先想清楚:这到底是不是一次"追加修正事件"更合适?能不能让查询侧用 `ReplacingMergeTree` 处理?

## 02.9 配套实现怎么跑

本书配了一份纯 Python 的简化实现 `mini-clickhouse`,用来把内部机制讲成能跑的代码。它只需要 Python ≥ 3.10 和 NumPy,不装 ClickHouse、不联网也能跑。下面这几条就是全部入口,`cd` 进 `mini-clickhouse/` 直接执行即可:

```bash
# ① 交互式 REPL(相当于 clickhouse-client 的交互模式)
python -m minich client

# ② 一次性跑一句(相当于 clickhouse local -q)
python -m minich local -q "SELECT count() FROM numbers(1000000)"

# ③ 一个自包含的导览:建表、插入、合并、查询一路演示过去
python -m minich demo

# ④ 全部测试
python -m pytest tests/

# ⑤ 全部可运行示例(每个都自包含:自己建表、插数据、打印)
for f in examples/*.py; do python "$f"; done

# ⑥ 文档结构自检(检查的就是这本书本身的一致性)
cd .. && python mini-clickhouse/tools/check_docs.py
```

前三条的形状和 §02.1 那三种跑法一一对应:常驻服务 + 交互客户端、免服务端跑一句、以及一段演示。区别只在把 `clickhouse` 换成了 `python -m minich`。书的每一章涉及 mini-clickhouse 的地方,都会注明用哪一条来跑;跑不了的会说明原因。

`examples/` 下的脚本是上手最快的路径。第一个是 hello world;第二个会把 part 目录、granule 目录和 `primary.idx` 的**实际字节数**打给你看——`第 05 章` 讲的磁盘布局,跑一遍就不再是抽象名词。

除了命令行,直接当库用也行,做的事完全一样:

```python
import minich as ch

ctx = ch.Context()                    # 一个"服务端";省略 data_path 就是纯内存
ctx.execute("CREATE DATABASE IF NOT EXISTS demo")
ctx.execute(
    "CREATE TABLE demo.hits (id UInt64, url String, ts DateTime) "
    "ENGINE = MergeTree ORDER BY id"
)
ctx.execute("INSERT INTO demo.hits VALUES (1, '/a', '2024-01-01 00:00:00')")
print(ctx.execute("SELECT count() FROM demo.hits").text())
```

对照关系很清楚:`Context` 对应一个服务端实例,`execute` 对应一次查询,返回的对象带一个 `text()` 把结果渲染成文本——就像客户端打印出来的那样。

**这份实现是教具,不是替代品。** 它实现了 `DataType` / `IColumn` 两段式类型系统、MergeTree 的 part 与稀疏索引、向量化算子和一部分函数;它没有实现真实 ClickHouse 的性能特性,函数覆盖也远不完整。别用它测性能,只用它验证"机制是不是这样"。`第 31 章` 讲它的设计,`第 34 章` 是工程手册——文件树、能力矩阵、API 签名和一组练习都在那里。

## 02.10 最小可用命令清单

把上面这些压成一份可以贴在终端旁边的清单。

```bash
# 起一个本地实例(数据落在具名卷 clickhouse-data 上,容器删了也不丢)
docker run -d --name ch --ulimit nofile=262144:262144 \
  -v clickhouse-data:/var/lib/clickhouse \
  -p 8123:8123 -p 9000:9000 clickhouse/clickhouse-server:25.3

# 下载多合一二进制
curl https://clickhouse.com/ | sh

# 连上服务端
clickhouse-client --host localhost --port 9000 --user default

# 跑一句就退出
clickhouse-client -q "SELECT version()"

# 一次跑多条语句(分号分隔即可,-n / --multiquery 已废弃)
clickhouse-client -q "SELECT 1; SELECT 2"

# 语句写在文件里,一次跑完(可给多个文件,按顺序执行)
clickhouse-client --queries-file schema.sql

# 不起服务端,直接跑
clickhouse local -q "SELECT toTypeName(1)"

# 指定输出格式
clickhouse-client -q "SELECT number FROM numbers(3) FORMAT JSONEachRow"

# 直接打 HTTP 接口(8123)
curl 'http://localhost:8123/?query=SELECT%20count()%20FROM%20events'
curl 'http://localhost:8123/' --data-binary 'SELECT count() FROM events'
```

```sql
-- 交互模式里最常用的几条
SHOW DATABASES;          -- 有哪些库
SHOW TABLES;             -- 当前库有哪些表
DESCRIBE TABLE events;   -- 表结构
SELECT version();        -- 版本号
SELECT count() FROM events;

SHOW PROCESSLIST;        -- 正在跑的查询(等价于查 system.processes)
SELECT query_id, elapsed, read_rows FROM system.processes ORDER BY elapsed DESC;
KILL QUERY WHERE query_id = '...';   -- 取消某一条查询

-- \l            → SHOW DATABASES
-- \d            → SHOW TABLES
-- \d <表名>     → 25.3 上会被拼成非法语句(纯文本前缀替换,不翻成 DESCRIBE)
-- \c <库名>     → USE <库名>
-- \G            → 竖排输出
-- \q            → 退出
-- (\dt 不存在;\d <表名> 要到更晚的版本才描述表。以上别名只在交互模式生效,批处理模式下都不生效)
```

## 02.11 小结

装法有好几条(包管理器、静态二进制、Docker、`clickhouse install`),选哪条取决于你要的是"长期可用的服务""能塞进 CI 的二进制"还是"连服务端都不要"。装完之后的跑法只有三种:Docker 或系统服务起常驻实例、多合一二进制前台跑、`clickhouse local` 免服务端验证行为。

配置不要去改主文件,往 `config.d/` / `users.d/` 丢片段;默认只监听回环地址,端口和监听范围都在那里改,数据默认落在 `/var/lib/clickhouse/`。服务端对外有两个接口:原生协议(`9000` / `9440`,客户端和驱动走它)与 HTTP(`8123` / `8443`,`curl`、浏览器和调试走它)。

客户端层面,记住 `-q`、`--format`、`--queries-file`、`\l`、`\d`、`\G` 就够日常用了——但要知道 `\dt` 这类 psql 习惯**不存在**,`\d <表名>` 在 25.3 上也**不会**描述表(要到更晚的版本才支持),而且这些反斜杠别名只在交互模式生效(批处理模式下失效),所以在脚本里一律用 `SHOW TABLES` 这种标准 SQL。要反复调用带参数的查询,用 `--param_<name>` 而不是拼接字符串。

查询跑久了要判断它是不是卡住,不要靠等:看 `SHOW PROCESSLIST`(即 `system.processes`)里 `read_rows` 有没有在涨,要停就 `KILL QUERY`;事后复盘靠 `query_id` 去 `system.query_log` 里翻。

第一张表用 `MergeTree`,分区按 `toYYYYMM`、排序键按"过滤频繁度"排列,枚举值用 `LowCardinality`。五条查询覆盖了 `count()`、`GROUP BY`、时间分桶、近似去重、分位数——它们就是分析型 SQL 的全部骨架。

从别的数据库迁过来,最先要改的五个心态是:没有主键约束、去重靠引擎、`JOIN` 要自己安排方向、`NULL` 很贵、更新是异步的。

下一章把类型系统拆开讲。在 ClickHouse 里,选类型不只是"填个字段声明"——它同时决定了存储布局、序列化方式、可用函数和压缩编码,甚至决定了某些查询能不能跑快。
