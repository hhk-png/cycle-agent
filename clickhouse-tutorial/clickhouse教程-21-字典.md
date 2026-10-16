# 第 21 章 字典

有一条查询几乎每个分析系统都会写:给事件流**补维度**。日志里存的是 `region_id`、`device_id`、`sku_id` 这类编码,你要把它们翻成"华东"、"iPhone"、"某品牌某型号",才能按人看得懂的口径聚合。这条查询在生产里每秒要跑几百万次。用 `JOIN` 做,每次都意味着把维表装进内存、建哈希表、探测、处理未命中——开销被放大几百万倍就是个瓶颈。字典(external dictionary)就是为这件事造的:把维表**预先加载进内存、组织成适合查找的结构**,查询时只剩一次内存访问。

## 21.1 字典解决什么问题

先把问题说具体。事件表 `hits` 有十亿行,每条带 `region_id`。维表 `regions` 只有六千行:`region_id → region_name, country, timezone`。你要按 `country` 聚合。

`JOIN` 的做法:每次查询把 `regions` 读出来、建哈希表、逐行探测 `hits`。`hits` 十亿行,探测十亿次;而且这十亿次探测的结果——"带 country 的中间行"——要先物化出来再做聚合,中间结果很大。

字典的做法:启动或首次访问时把 `regions` 一次性加载进内存,按 `region_id` 组织成查找结构;查询时 `dictGet('regions', 'country', region_id)` 就是**一次内存查找**,不建表、不产生中间结果、直接参与聚合。

差别不只是"快一点",而是**数量级**。一个直观的对照:字典查找就是**一次哈希查表**——O(1)、纯内存、既没有网络也没有 IO;而 `JOIN` / 子查询形态的维度补全,**每次都要把右表读进来**、建起哈希表、逐行探测,再把连接后的中间行物化出来,十亿条 `hits` 就走十亿遍"读右表"这套开销。而且这个差距来自架构,不是调参:

- **内存常驻。** 维表只加载一次,之后所有查询共享。`JOIN` 的哈希表是**每次查询重建**的。
- **O(1) 查找。** 给定键,一步到位;`flat` 布局连哈希都不算,直接拿键作数组下标。
- **不产生中间结果。** 字典查找是表达式,直接嵌在聚合里;`JOIN` 要先产出一张连接后的宽表。

代价也要一并记住:**字典和源表不保证一致。** 数据是按 `LIFETIME` 刷新的,两次刷新之间源表改了,字典里还是旧值。字典也不参与事务。所以字典适合"慢变的维表",不适合"读完必须绝对最新"的数据。

## 21.2 字典与 JOIN、Set、Join 引擎:四种"查匹配"手段怎么选

这一节是全书"补维度/找匹配"选型的**总纲**;第 15 章 §15.11 是同一张表从 JOIN 视角的写法(那里还列了 `ASOF JOIN`、`arrayJoin` 等),两处互为交叉引用,内容不重复。

先归成两种模型:

- **按需关联(`JOIN`)**:数据到了才建索引、才找匹配,代价与本次查询的行数成正比。擅长两个大表之间真正的关系运算——多对多、多个键、两侧都要参与聚合。
- **预加载查找(字典)**:维度在查询之前就已经在内存里、组织好了,查询时的代价与"查了多少次"成正比、与"维表多大"无关。擅长"一个反复查的小维表"。

于是选型不是"字典比 JOIN 快",而是看你要什么:

- 要**属性值**、维表小、被高频反复查 → **字典**;
- 只要"某个值**在不在**名单里"、不要属性 → **`Set` 引擎表 + `IN`**(只存去重后的键,比字典还轻);
- 要一个**持久化、可 DDL 管理、能按 key 点查单列**的内存哈希表 → **`Join` 引擎表 + `joinGet`**(§21.14;建表语法与 `joinGet` 的约束见第 15 章 §15.14);
- 两个大表做**关系运算** → **`JOIN`**。

换一种说法:**要属性 → 字典;只要"在不在" → Set;要一张"能管起来"的查找表 → Join 引擎;两张大表做关联 → JOIN。**

## 21.3 CREATE DICTIONARY 的四要素

一个字典由四件事定义:属性、主键、SOURCE、LAYOUT,外加 LIFETIME。看一个完整例子:

```sql
CREATE DICTIONARY geo_dict (
    region_id  UInt64,
    country    String DEFAULT 'Unknown',
    city       String DEFAULT ''
)
PRIMARY KEY region_id
SOURCE(CLICKHOUSE(TABLE 'regions' DB 'ref'))
LAYOUT(HASHED())
LIFETIME(MIN 300 MAX 600);
```

- **属性**:字典对外暴露的列,形如 `名字 类型 [DEFAULT 缺省值] [EXPRESSION expr] [HIERARCHICAL|INJECTIVE|IS_OBJECT_ID]`。`DEFAULT` 在这里特别重要——键找不到时返回的就是它(见 §21.11);其余标志见 §21.4。
- **主键**:`PRIMARY KEY`,用来做查找的键列。可以复合。也可以写成 `PRIMARY KEY (a, b)`。
- **SOURCE**:数据从哪来(§21.5)。
- **LAYOUT**:数据在内存里怎么存(§21.6)。
- **LIFETIME**:多久算过期(§21.9)。

## 21.4 属性标志:EXPRESSION、INJECTIVE、IS_OBJECT_ID、HIERARCHICAL、DEFAULT

属性名后面除了类型,还能挂几个标志。它们不是语法糖,每一个都改变行为:

- **`DEFAULT v`**:键找不到时返回什么值(§21.11)。不写时是该类型的零值(空串、0)。
- **`EXPRESSION expr`**:属性**不由源表列直接给出**,而是在加载时用一条表达式**算出来**。比如源里只有 `region_id`,你想让字典直接暴露一个 `region_id % 100` 的分桶列,就写 `bucket UInt64 EXPRESSION region_id % 100`。这样下游 `dictGet` 拿到的是算好的值,不必每次查询再算。
- **`INJECTIVE`**:声明"这个属性与主键**一一对应**(单射)"。它不改变语义,而是**给优化器一条它本来不知道的信息**:确认映射单射之后,优化器在某些查询里能把对属性的过滤改写成对键的过滤(反之亦然),少做几次查找。**这是一个真实的性能开关**——反过来,把 `INJECTIVE` 标在一个其实是一对多的属性上,会让优化器据此改写而给出错误结果,所以只有确认单射时才加。
- **`IS_OBJECT_ID`**:标在**主键**列上,告诉系统"这列是对象的 ID"。
- **`HIERARCHICAL`**:标在属性上,表示它指向**父键**,用来表达树状维度(§21.12)。

回忆一下 §21.2 的选型:`INJECTIVE` 属于"同样的定义、更快的执行",和"换一种手段(JOIN/Set)"是两条独立的优化路径——两条都可以走。

## 21.5 SOURCE:数据从哪来,以及进阶用法

SOURCE 决定字典的数据源。常见的有:

- `file`——本地文件(配 `path` 和 `format`);
- `executable`——跑一个外部命令,读它的 stdout(每次刷新都现起一个进程);
- `executable_pool`——同一件外壳,但维护一个**常驻的进程池**,避免反复起进程的开销(适合"脚本启动很慢"的场景);
- `http`——从 HTTP 端点拉;
- `clickhouse`——查一张 ClickHouse 表;**省略 `host`/`port` 时就地查本机的表(in-process,零网络),这也是最常用的用法**;带连接参数则查远端(见下);
- `mysql` / `postgresql` / `mongodb` / `redis`——连外部库,拉一张表;
- `cassandra`——从 Cassandra 拉;
- `s3` / `hdfs`——对象存储 / 分布式文件系统。

不同 SOURCE 的参数不同(连接信息、表名、格式等),但它们的**契约是一致的**:返回"键列 + 属性列"的一批行。

**`invalidate_query`:让"源表变了"这件事能被发现。** 光靠 `LIFETIME` 只能"到点重拉",它**无法知道源有没有真的变**。对 `clickhouse` / `mysql` / `postgresql` / `odbc` 这类外部 SQL 源,可以额外给它一条**探测查询**:

```sql
SOURCE(CLICKHOUSE(
    TABLE 'regions' DB 'ref'
    invalidate_query 'SELECT max(updated_at) FROM ref.regions'
))
```

刷新时先跑这条探测查询:结果和上次一样就**跳过重拉**,不一样才重新加载整份。它把"按时间盲刷"变成"按内容变化刷",对"源表很大、平时很少变"的维度特别划算。代价是探测查询自己也要开销——别让它去扫一张大表。

**`clickhouse` 源的远端参数**:省略 `host`/`port` 是就地查本机;查远端要给出 `HOST`/`PORT`/`USER`/`PASSWORD`/`DB` 等(以及是否走 TLS 之类的开关)。**就地查本机表是零网络、最快的**,能用它就别绕远端。

**`CLICKHOUSE` 源除了 `TABLE`,还能直接给一条 `QUERY`。** 把 `SOURCE(CLICKHOUSE(TABLE 'regions' DB 'ref'))` 换成 `SOURCE(CLICKHOUSE(QUERY 'SELECT id, name FROM ref.dict_src WHERE active = 1'))` 时,字典的数据来自这条查询的输出——于是可以在加载前先做过滤、联表、改名,而不是只能整表拉一份。前提不变:这条查询的结果仍要构成"键列 + 属性列",且列名/列序要能和字典的属性对上。

**`update_field` / `update_lag`:增量刷新。** 比 `invalidate_query` 更省的一种做法是"只取新增的行":给源指定一个"最后更新时间"列和一个滞后期,字典下次刷新时只拉比上次新的那几行追加进去。它对"只增不改"的源有效,但**对会更新/删除的维度不适用**(追加反映不了删除)——要用之前先确认你的源是"只追加"的。这个机制支持哪些源、参数名怎么拼,我未能在官方文档里找到逐项确证,以你所用版本的字典文档与 `system.dictionaries` 为准(**未确认**)。

这里有个必须先想清楚的前提:**字典要把整份维度拉到本地内存。** 除了 `direct`/`cache` 这类特殊布局,字典加载时就是"把源表全量读一遍、全塞进内存"。所以"源表很大时字典会占多少内存"是可以估算的:

- 大致是 `行数 × 每行属性总宽度`,再乘以布局自身的开销系数;
- `flat` 布局更极端:内存 ≈ `最大键值 × 每个元素宽度`——键稀疏或键值很大时,这个数字会爆炸(而且会被直接拒绝,见 §21.6)。

结论:字典适合**中小维表**(几万到几百万行),不适合把大表当字典加载——真有大维表,先想是不是该用 `JOIN`。

## 21.6 LAYOUT:内存里怎么存,直接决定代价

LAYOUT 是字典的精髓——同一份维表,用不同布局存,查找和内存的代价完全不同。先分三类:**全量常驻型**(`flat`/`hashed`/`hashed_array`/`sparse_hashed`/`range_hashed`)、**按需查源型**(`direct`)、**缓存型**(`cache`/`ssd_cache`),后两类有各自的 `complex_key_*` 复合键版本。

**全量常驻型:**

- **`flat`。** 属性存成**一个数组,键就是下标**,查找是 O(1)、**连哈希都不用算**,最省 CPU。但它对键有**硬约束**:键必须是 `UInt64`,且**最大键值必须小于 `max_array_size`**(默认 500000)——因为数组长度就等于最大键值。一旦源里出现超过上限的键,ClickHouse **直接拒绝创建/加载这个字典并抛错**;这不是"浪费内存",而是根本建不出来。要让键域更大,就把 `max_array_size` 调大(内存随之线性上涨),或换 `hashed_array`。所以 `flat` 只适合"从 0 开始的稠密整数 ID";键稀疏或键值很大时,别用它。
- **`hashed`。** 通用哈希表,**任意键类型**都行,O(1) 查找,键空间不受限制。比 `flat` 多一点哈希开销。
- **`hashed_array`。** 哈希表只存"键 → 行号",属性值另放一个数组。属性列多时比 `hashed` 更省内存,且键可以很大、很稀疏。**撞上 `flat` 的 `max_array_size` 上限后,这是第一替代。**
- **`sparse_hashed`。** 同样是哈希,但**分块分配、永不 rehash**。为什么?一次性大分配和重哈希都会在高频查询里造成停顿,分块增长把它们摊平。代价是内存略高于 `hashed`。适合键空间稀疏、元素又多的场景。
- **`range_hashed`。** 处理**时变维度**,比别的布局多一对**必填**的源列:`range_min` / `range_max`(在结构里用 `RANGE(MIN … MAX …)` 声明;类型可以不写、默认 `Date`,但取值必须能装进 `Int64`)。查找时除了键还要**多给一个区间参数**——`dictGet('dict','attr', key, ts)` 里那个 `ts` 是**必填**的,少了它连函数签名都不成立。它返回包含 `ts` 的那一行;多个区间重叠时,由布局参数 `RANGE_LOOKUP_STRATEGY`(取 `min` 或 `max`)决定挑哪一个(`min` 取"`range_min` 最小"的那个区间命中,`max` 取"`range_min` 最大"的;不写时的默认值我未找到官方确证,请在你的版本上确认,**未确认**);`range_max` 为 `NULL` 表示"到无穷"。复合键版本是 `complex_key_range_hashed`。

**按需查源型:**

- **`direct`。** **不缓存**,每次查找都去问源。适合维度极大、装不进内存、但查询只碰少数键的场景。复合键版本 `complex_key_direct`。

**缓存型(只留"最近用过"的键):**

- **`cache`。** 在另一种布局外面套一层固定大小的缓存(按近似 LRU 淘汰),只把"最近查过的键"留在内存里;用 `SIZE_IN_CELLS` 限定能装多少个键。它介于 `hashed`(全量常驻)与 `direct`(完全不留)之间,缓存未命中就去查源。
- **`ssd_cache`。** 和 `cache` 同一思路,但**数据落在 SSD 上、内存里只放索引**——这是"维度比内存大、又想比 `direct` 快"的正解。参数有 `BLOCK_SIZE`、`FILE_SIZE`、`READ_BUFFER_SIZE`、`PATH`(缓存文件放哪)。有一个**重要陷阱**:`cache`/`ssd_cache` **不记录"哪些键不存在"**,把它们当表读时只能看到当前装着的格子;而且缓存为空时,`INSERT` 里每来一个键都要去源查一次,写入路径会被拖慢。所以它们更适合"读多写少"的点查,不适合在写入路径上做实时的维表补全。复合键版本是 `complex_key_cache` / `complex_key_ssd_cache`。

**复合键与专款:**

- **`complex_key_hashed` / `complex_key_hashed_array` / `complex_key_range_hashed` / `complex_key_cache` / `complex_key_ssd_cache` / `complex_key_direct`**——上面各布局的复合键版本,键是元组或字符串。
- **`regexp_tree`。** 键是**字符串**,按正则式的**前缀树**组织,专为"字符串前缀 → 属性"的层次匹配设计(典型是 IP 网段到地区);配合 `HIERARCHICAL` 与层次函数(§21.12)使用。
- **`ip_trie`**、**`polygon`** 是真实 ClickHouse 里两种特殊的空间/网络查找布局,单独在 §21.7、§21.8 讲。

**一句选型:** 稠密小整数 ID → `flat`;大多数情况 → `hashed_array`(省内存)或 `hashed`;稀疏且元素多 → `sparse_hashed`;时变维度 → `range_hashed`;维度大到装不进内存 → `ssd_cache`;超大且只碰少数键 → `direct`;网络前缀 → `ip_trie`;地理围栏 → `polygon`。

## 21.7 ip_trie:按 CIDR 前缀做最长匹配

`ip_trie` 专门解决一个问题:**给一个 IP,判断它落在哪个网段/地区**。它把前缀组织成一棵 **trie(前缀树)**,查找时做**最长前缀匹配**——同时有 `10.0.0.0/8` 与 `10.1.0.0/16` 两条规则时,`10.1.2.3` 命中更长的那条。

这里最容易搞错的是"键的类型"和"查找时传什么"。分两边说清:

- **建表时**,键是一列 **CIDR 形式的字符串**(如 `10.1.0.0/16`),属性是"这个网段对应的值";
- **查找时**,传进去的是**一个 IP 值**,而不是 CIDR 字符串。IP 的类型要和键匹配:**IPv4 用 `UInt32`**(`toIPv4(...)` / `IPv4StringToNum(...)`),**IPv6 用 `FixedString(16)`**。传一个普通 `String` 不会匹配键类型。

```sql
CREATE DICTIONARY geo_ip (
    prefix   String,
    region   String DEFAULT 'Unknown'
)
PRIMARY KEY prefix
SOURCE(CLICKHOUSE(TABLE 'cidr_ref'))
LAYOUT(IP_TRIE())
LIFETIME(600);

SELECT dictGet('geo_ip', 'region', toIPv4('10.1.2.3')) FROM hits;
```

注意 `dictGet` 的**第三个**参数(不是第二个)才是**键**——签名是 `dictGet(字典名, 属性名, 键)`。这里的键必须是 IP 值:`toIPv4(ip)` 拿到 `UInt32`、`toIPv6(ip)` 拿到 `FixedString(16)`,而**不是** `toString(ip)`(那是个普通字符串,和键类型对不上)。

最后补一个前提:**`ip_trie` 是全量常驻布局,整份网段表必须装进 RAM**。它的前缀树在加载时一次建好、常驻内存,所以网段表不能太大——和 §21.5 结尾那句"字典适合中小维表"是同一个道理。典型用途:把访问日志里的 IP 翻成国家/城市/运营商/ASN,再按地区聚合。

## 21.8 polygon:判断点是否落在多边形内

`polygon` 做**地理围栏**:给坐标系里的一个点,判断它在哪个多边形内,返回那块多边形的属性(比如所属行政区)。

它的键是一组**多边形顶点**——按顺序排好、首尾相接的点列;属性是"这块多边形代表什么"。查询时传入待判断的点。大致的用法是:

```sql
SELECT dictGet('regions_poly', 'name', tuple(lon, lat)) FROM points;
```

两条最容易踩的坑:一是顶点**顺序与闭合方式**要对(顺序错了会把点判成"在外面",或判成自相交);二是**经纬度别写反**——"经度在前、纬度在后"是常见约定,但不同数据源可能相反,导入前拿一个已知点验一下。`polygon` 适合"批量把坐标归属到区域"这类离线/半离线判断;要对海量坐标做在线空间查询,更稳的做法是**预先算好地理编码存进列**,再用 `MergeTree` 排序键去过滤。这个布局的键在 DDL 里是**嵌套的浮点数组**:文档里"多点/多面"的示例写的是 `Array(Array(Array(Array(Float64))))`,单个多边形则少一层嵌套。`dictGet` 的点参数既可以写成**坐标元组**、也可以写成**坐标数组**,例如 `dictGet('dict','name', tuple(x, y))` 或 `dictGet('dict','name', [x, y])`。另外,想让字典能被 `SELECT` 直接读到,建字典时要带上 `STORE_POLYGON_KEY_COLUMN 1`。

## 21.9 LIFETIME:过期是"懒"的

`LIFETIME(300)` 表示 300 秒后过期;`LIFETIME(MIN 300 MAX 600)` 表示在 300 到 600 秒之间**随机**挑一个时间过期(避免一堆字典同时刷新)。

**最关键的一点:过期是懒加载的。** 没有一个后台定时器盯着时间;只有**下一次访问这个字典时**(下一次 `dictGet`),系统才检查"过期了吗",过期就去源里重新读一遍。也就是说:

- 一个很久没人查的字典**不会**自动刷新——它可能早就过期了,但因为没人碰,一直没重载;
- 过期只是"标记",真正的刷新发生在访问路径上。

这套"懒加载"由一个**服务器设置**控制:**`dictionaries_lazy_load`(默认 `1` = 开)**。它决定字典是"第一次被访问时才加载",还是"服务器启动/创建时立刻全量加载"——本章讲的"过期不刷新、访问才刷新"这个现象,配置根源就是它。把它设成 `0`,字典会在创建/启动阶段就加载:好处是"源连不上"这类问题会早早在启动时报出来、不会等到第一次查询才踩雷,代价是启动变慢、内存一次性占满。

想主动刷新,用 `SYSTEM RELOAD DICTIONARY db.name`(单个)或 `SYSTEM RELOAD DICTIONARIES`(全部)。常见用法是在源表更新后手动触发一次。**注意它只作用于收到这条语句的那一个节点**——集群里要让所有节点一起重载,得写上 `ON CLUSTER`(见 §21.16)。

## 21.10 查询侧函数:dictGet 家族

```sql
SELECT dictGet('geo_dict', 'country', region_id) AS c,
       count()
FROM hits
GROUP BY c;
```

常用函数:

- `dictGet('dict', 'attr', key...)`——返回属性值;键找不到时返回该属性的 `DEFAULT`(见 §21.11);
- `dictGetOrDefault('dict', 'attr', key..., default)`——键找不到时返回你指定的 `default`;
- `dictGetOrNull('dict', 'attr', key...)`——键找不到时返回 `NULL`;
- `dictHas('dict', key...)`——键是否存在,返回 `UInt8`。

**类型化变体**在工程里几乎总比裸 `dictGet` 好用:`dictGetString`、`dictGetUInt8/16/32/64`、`dictGetInt8/16/32/64`、`dictGetFloat32/64`、`dictGetDate`、`dictGetDateTime`、`dictGetUUID`、`dictGetIPv4/IPv6`。它们把返回类型钉死,省掉类型推断的麻烦(为什么这一点重要,见 §21.15)。

层次相关:`dictGetHierarchy` / `dictGetChildren` / `dictGetDescendants` / `dictIsIn`(§21.12)。多值相关:`dictGetAll('dict', 'attr', key[, limit])` 返回一个**数组**,用于"一个键对应多行"的情况。区间相关:`range_hashed` 布局的查找多给一个时间戳参数,`dictGet('dict', 'attr', key, ts)`(§21.6)。

## 21.11 键找不到时:返回 DEFAULT,不是报错

这是外部字典最重要的语义,也是最容易误判的地方:

**字典里没有这个键时,`dictGet` 返回该属性的 `DEFAULT` 值,查询不报错,也不产生 `NULL`。**

后果是:一份有 5% 的键缺维度的数据,查出来的结果里那 5% 会被悄悄归到 `'Unknown'` 之类的缺省桶里,而你不会收到任何错误。这在"缺维度是常态"的日志场景里很方便,但在"缺维度说明数据有问题"的场景里就是隐患。

那**怎么区分"真的没有这一行"和"值恰好等于默认值"?** 用 `dictHas('dict', key)`:它直接告诉你键在不在,与属性值无关。另一个办法是用 `dictGetOrNull` 拿 `NULL` 当"缺失"的显式信号,或给 `dictGetOrDefault` 传一个绝不会等于真实值的哨兵(比如 `-1`)。

## 21.12 层次维度:HIERARCHICAL

很多维度是树状的:省 → 市 → 区,或者"公司 → 部门 → 小组"。字典用**层次属性**表达它:在 CREATE 里给属性加 `HIERARCHICAL` 标志(§21.4),让它指向自己的父键。有了它,一组层次函数就能在树上导航:

- `dictGetHierarchy('dict', key)`——从某个键一路沿层次属性**爬到根**,返回路径上的**键序列**;
- `dictGetChildren('dict', key)`——只取**直接子节点**(第一层),返回的是一个键数组;要逐层下钻时用它,而不是一次拿到整棵子树;
- `dictGetDescendants('dict', key[, level])`——反过来,求某个键的**所有后代**(`level` 限定深度,省掉它就是全部);
- `dictIsIn('dict', child, ancestor)`——判断 `child` 是否落在 `ancestor` 的层次链下。

`dictGetChildren` 与 `dictGetDescendants` 的区别值得记牢:前者是**一层**,后者是**所有层**。典型用途:"统计华东大区及下属城市的销量"——用 `dictGetDescendants` 把"华东"展开成完整的键列表再 `IN` 过滤;而"只统计华东下辖的省、不往下到市"就用 `dictGetChildren`。

还有一条必须记住的标注:**`dictGetDescendants` 被官方标为非确定性函数**。它的返回取决于字典**当前加载的内容**,字典一刷新结果就可能变,所以别把它当常量折叠,也别让它参与需要稳定结果的索引或缓存假设。

## 21.13 监控 system.dictionaries:加载失败是静默的

字典有一个很危险的特性:**它的失败是静默的。** 如果字典加载失败(源连不上、格式不对、内存不够),它可能是**空的**或**停留在旧数据**;而 §21.11 说了,键找不到返回 `DEFAULT`。于是查询**不报错**,但**所有查找都返回默认值**,聚合结果整片整片地错,没有任何异常抛到你脸上。

所以**必须**监控 `system.dictionaries`,而不是等业务发现数据不对。关键列:

- `status`——`NOT_LOADED` / `LOADING` / `LOADED` / `FAILED`。**`FAILED` 就是加载失败了**;
- `last_exception`——最近一次加载的异常文本,空字符串才正常;
- `element_count`——加载了多少元素(和维表行数对不上就要警觉);
- `bytes_allocated`——占了多少内存;
- `last_successful_update_time`——最近一次成功刷新的时间(长时间不前进说明刷新一直失败或没人访问);
- `origin`、`type`(布局名)、`key`、`source`、`lifetime`——看清这个字典是怎么配的。

一条最低成本的告警:

```sql
SELECT database, name, status, last_exception
FROM system.dictionaries
WHERE status != 'LOADED' OR last_exception != '';
```

只要它有行返回,就说明有字典没在正常工作。

## 21.14 字典的兄弟:Join 引擎表与 joinGet

在"补维度/找匹配"的选型里,与字典最接近的兄弟是 **`Join` 引擎表 + `joinGet`**:两者都先把数据组织成一张内存查找表,差别只在**归属**与**刷新模型**——字典是独立对象(`CREATE DICTIONARY`,自带 SOURCE/LAYOUT/LIFETIME,按属性名 `dictGet`),`Join` 引擎表是一张真正的表(靠 `INSERT` 灌数据、`joinGet` 按 key 点查单列),刷新由你自己的 `INSERT` 控制,而不是 `LIFETIME`。一句话选型:**要"自动刷新 + 按属性查"用字典,要"一张能像表一样 DDL 管理、按 key 点查的内存映射"用 `Join` 引擎表**;两者都不保证与源表强一致(字典换来自动刷新、失去"直接写它"的能力;`Join` 表换来可写、失去"自动跟着源表变")。

`Join` 引擎表的完整建表语法(严格度/方向/键列三个参数)与 `joinGet` 的返回类型约束——只取单列、键的类型/个数/顺序必须与建表一致、找不到键时用 `joinGetOrNull` 这些容易踩的点——统一放在第 15 章 §15.14,这里不再重复;与 `JOIN`、`Set` 的四方选型总纲见本章 §21.2。

## 21.15 与 mini-clickhouse 的对照

`minich/dictionaries.py` 实现了本章描述的这套机制,结构正好对应 §21.3 的四要素(源、布局、生命周期、属性)。

**实现了哪些 SOURCE**:`file`、`executable`、`http`、`clickhouse`,以及 mini 自己的 `table` 与 `in-process`/`in_process` 两个名字(注意:真实 ClickHouse 并没有独立的 `in-process` 源——"就地查本机表"用的是**省略连接参数的 `clickhouse` 源**);`mysql`、`postgresql`、`odbc`、`mongodb`、`redis`、`s3`、`hdfs` 都注册了名字,但调用时会抛 `NOT_IMPLEMENTED` 并说明原因(没有绑定对应的客户端)。**实现了哪些 LAYOUT**:`flat`、`hashed`、`hashed_array`、`sparse_hashed`、`range_hashed`、`complex_key_hashed`、`complex_key_direct`、`direct`、`cache` 都有实现;`ssd_cache`(需要磁盘缓存格式)、`polygon`、`ip_trie`、`regexp_tree` 则是显式的"未实现"桩。

几个语义点可以对照代码确认:`Lifetime` 的 `maybe_reload` 就是"**惰性刷新**"——只有 `is_expired()` 且被访问时才重载,和真实行为一致;`Dictionary.get_value` 在键不存在时返回该属性的 `default_value`(而不是 `None`),`has_key` 提供"区分缺失与默认值"的能力;`hierarchy()` / `children()` / `descendants()` / `is_in()` 分别对应 `dictGetHierarchy` / `dictGetChildren` / `dictGetDescendants` / `dictIsIn`;`system.dictionaries` 的那一行由 `describe_row` 按 `status` / `origin` / `type` / `key` / `bytes_allocated` / `element_count` / `load_factor` / `source` / `lifetime` / `last_exception` 等字段构造。

**最值得讲的简化点,在"字典怎么被解析到"这件事上。** 真实 ClickHouse 在**查询分析期**就用字面量(字典名、属性名)解析出字典,并把它**内嵌成一个指针**带进执行期。因此它能精确推导出 `dictGet` 的返回类型——因为指针在分析期就是已知的;这也是"字典不能像表那样跨服务器搬运"的原因(指针不跨机)。mini-clickhouse 的求值器拿不到那个指针,它只把**实参类型**(而不是值)交给返回类型推导,所以普通 `dictGet` 无法在分析期知道属性类型,**退化成在执行期拿到真实值**;模块文档给出的对策是:需要参与运算时用**带类型后缀**的 `dictGetString` / `dictGetUInt64` 这类版本,它们能给出精确类型。这不是教学简化,而是真实工程里"显式写类型"这条经验的直接来源。

另两处简化也值得注意:mini-clickhouse 维护了 `ctx.dictionaries`(**每个上下文一份**)**加**一个模块级 `_REGISTRY`(兜底),`resolve_dictionary(name, ctx)` 先查前者、再查后者,这意味着"同名字典在一个进程里是全局的"——真实的多租户隔离靠的是"字典属于某个 server / 某个数据库",不是进程全局;而且重复 `CREATE DICTIONARY` 同名时 mini 会**直接覆盖**,真实 ClickHouse 会报 `DICTIONARY_ALREADY_EXISTS`。

## 21.16 字典的生产要点:当成表读、集群内刷新、分布式的边界

**一、字典可以像表一样被 `SELECT`。** 一个已加载的字典本身就是一张可查的"表",可以直接 `SELECT * FROM dict_name`(或带库名 `SELECT * FROM db.dict_name`),拿到"主键列 + 属性列"的全部内容:

```sql
SELECT * FROM geo_dict;                 -- 把这份内存查找结构当表读
SELECT count() FROM geo_dict;           -- 现在装了多少元素
SELECT * FROM geo_dict WHERE country = 'CN';
```

它的典型用途是**就地核对**:字典到底加载了些什么、`region_id` 有没有漏、`DEFAULT` 有没有被误当真值——比只看 `system.dictionaries` 里那个计数更直接。但有一条**关键限制**:能读到的多少**取决于 LAYOUT**。全量常驻型(`flat`/`hashed`/`hashed_array`/`sparse_hashed`/`range_hashed` 等)可以整份读出来;而 `cache`/`ssd_cache` 这类"只留最近用过的键"的布局**不记录哪些键不存在**,直接 `SELECT` 时**只能看到当前装在缓存里的那些格子**,读到的是一份不完整的快照(§21.6 已提过)。所以"`SELECT * FROM dict` 的行数对不上"时,先看它的 LAYOUT 是不是缓存型,而不是急着怀疑源表。

**二、集群里刷新要写 `ON CLUSTER`。** §21.9 的 `SYSTEM RELOAD DICTIONARY db.name` 只作用于**收到这条语句的那一个节点**。分片/副本集群里每个节点各自持有一份字典副本,源表更新后想让**全集群**都立刻重载,得写成:

```sql
SYSTEM RELOAD DICTIONARY db.geo_dict ON CLUSTER my_cluster;   -- 全集群
```

漏写 `ON CLUSTER` 是"改了源表、部分节点还是旧值"的常见根因——尤其在字典按 `LIFETIME` 懒加载(§21.9)时,每个节点各刷各的、时间点还不一致。要统一,就显式对集群广播这条重载。还有一个小坑:**`ON CLUSTER` 下尽量写全限定名**(带库名),只写裸字典名时,解析用的是发起节点的当前库,容易在别的节点上错刷同名的另一只字典。

**三、`dictGet` 遇上 `Distributed`:字典是"本地指针",不跨机。** 前面(§21.15)说过,真实 ClickHouse 在**分析期**就把字典解析成一个内嵌引用带进执行。这个引用**只在它所属的 server 上有效,不会随查询被搬到别的节点**。于是在分布式查询里:

- 查询被发到某个分片后,`dictGet` 只能解析**该分片本地**的同名字典;要让整个集群都能查出正确结果,**每个节点都要各自创建同名的字典**(通常就是同样的 `CREATE DICTIONARY ... ON CLUSTER`)。漏建节点的那一侧会退化成"键找不到"、返回 `DEFAULT`——和 §21.11 的静默行为叠加,结果是**大面积悄悄错**。
- 所以"字典能不能跨分片用"的答案是:**能,但前提是每个节点本地都有那份字典**;它**不是**一张会被 `GLOBAL` 广播的维表。如果你要的正是"一份维表广播到所有分片"的语义,用 `GLOBAL JOIN`(第 15 章 §15.10),而不是指望字典替你搬运。这一条与"字典名在 mini 里是进程全局"(§21.15)正是同一处差异的另一面。

## 21.17 小结

- 字典解决的是"**给事件流补维度、每秒几百万次**"这个具体问题;相对 `JOIN` 的差距是**数量级**——内存常驻、O(1) 查找、不产生中间结果。
- 一个字典由四要素定义:**属性(含 `DEFAULT`、`EXPRESSION`、`INJECTIVE` 等标志)、主键、SOURCE、LAYOUT**,外加 **LIFETIME**。`INJECTIVE` 是真实的优化器开关,别乱标。
- SOURCE 的硬前提是"**整份维度能拉到本地**";源表很大时字典内存可按"行数 × 每行宽度 × 布局开销"估算,所以字典只适合中小维表。`invalidate_query` 让刷新从"按时间盲刷"变成"按内容变化刷"。
- LAYOUT 决定代价:`flat` 最省 CPU 但**最大键值超过 `max_array_size`(默认 500000)会直接拒绝创建**;`hashed`/`hashed_array`/`sparse_hashed` 是通用档;`range_hashed` 管时变维度(必填 `range_min`/`range_max`,查找必给区间参数);`direct` 完全不缓存;`cache`/`ssd_cache` 只留最近用过的键(适合读多写少);`ip_trie` 做网段最长前缀匹配;`polygon` 做地理围栏。
- **过期是懒加载的**:只有访问时才刷新(`dictionaries_lazy_load` 默认 `1` 正是这套行为的设置根源),`SYSTEM RELOAD DICTIONARY` 可手动触发;`dictGetDescendants` 被官方标为**非确定性**。
- **字典能像表一样被 `SELECT *` 读**(用来就地核对加载内容),但 `cache`/`ssd_cache` 这类缓存型布局只给出"当前缓存里的格子";`SYSTEM RELOAD DICTIONARY ... ON CLUSTER` 才能在集群里让所有节点一起重载;而且字典是**每节点本地的**(`dictGet` 靠本节点的内嵌引用),分布式查询要求**每个节点都建同名字典**,它不是能靠 `GLOBAL` 广播的维表。
- 键找不到时 **返回属性的 `DEFAULT`,不报错**;要区分"真缺失"和"值恰好是默认值",用 `dictHas`。
- 层次维度用 `dictGetHierarchy`(到根)、`dictGetChildren`(直接子节点)、`dictGetDescendants`(全部后代)、`dictIsIn`(归属判断);`children` 与 `descendants` 的区别是"一层"对"所有层"。
- 加载失败是**静默的**,必须监控 `system.dictionaries`(`status`、`last_exception`、`element_count`、`bytes_allocated`、`last_successful_update_time`)。
- 选型:要属性值用**字典**,只要"在不在"用 **Set + IN**,要一张"能像表一样管理的内存映射"用 **`Join` 引擎表 + `joinGet`**,两张大表做关联用 **JOIN**;总纲见 §21.2,JOIN 视角见第 15 章 §15.11。
- mini-clickhouse 实现了完整的源/布局/惰性刷新/层次/系统表;关键差异是它**在会话上下文里按名字解析**字典,而真实 ClickHouse 在**分析期内嵌指针**——这正是返回类型能否精确推导的根源。
