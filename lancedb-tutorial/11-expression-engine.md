# 第 11 章　表达式引擎

前两章反复用到 `WHERE n > 19000` 这句话。但它在引擎内部不是一个字符串，
而是一棵被改了又改的树：先被解析成 AST，再被化简和折叠，
再被拆成"能交给索引的部分"和"必须逐行算的部分"，
最后变成一段在整列上跑出布尔掩码的 numpy 代码。

这一章把这棵树拆开。它值得一整章，是因为**过滤条件在一张表里
出现的次数远超任何其他语句**：每次查询、每次更新、每次删除、
每次 upsert 都带着一个谓词。谓词处理的每一处语义错误都会
静默地多返回或少返回几行，而这类错误只在数据恰好有 NULL、
恰好有 NaN、恰好有重复时才发作。

## 11.1 为什么载体不能是一段字符串

一个自然的偷懒做法是"把过滤条件当 LIKE 字符串处理"——
匹配得上就交给数据库，匹配不上就报错。在向量库里这个做法立刻崩掉，
因为过滤条件在本实现里有**四个不同的消费者**：

**索引。** BTREE / BITMAP / LABEL_LIST 只能回答特定形状的问题。
`col = 5` 能翻译成一次区间查询，`col + 1 = 5` 不能——而索引
要的是"一个代数表达式"，不是一段文本。翻译必须**理解**谓词的结构。

**统计剪枝。** `WHERE ts > '2024-01-01'` 要能被翻译成
"这一片的 `max <= '2024-01-01'` 就跳过"。这需要知道
谓词的**方向**（谁在左边、谁在右边）和**常量在哪**。

**类型推导。** `a > 1` 里的 `1` 是 `int64` 还是 `float64`？
`'x' || 1` 能不能算？这些决定必须**在求值之前**就定下来，
否则每一行都要重新判断一次类型。

**选择性估计。** 扫描器要拿一个数字决定 prefilter 还是 postfilter
（第 10 章）。这个数字来自谓词的形状，不是来自它的文本长度。

四个消费者要的东西加起来，就是"必须有一棵树"。
而一旦有了一棵树，顺带还得到两个好处：**报错可以指到字符位置**
（因为每个 token 都带着 `pos`），**改写可以做在树上**
（把 `a > 1 + 2` 变成 `a > 3` 之后索引才认得出它）。

这也解释了为什么真实 LanceDB 用的是 Rust 的 DataFusion SQL 解析器
（`sqlparser` crate）而不是 `LIKE` 匹配。用一个真正的解析器，
换来的不是"支持更多语法"，而是**后面那四步都有东西可以操作**。

## 11.2 三层加一层

本实现把它拆成四个模块，边界划得很清楚：

`nodes.py` 是 AST：节点定义、类型推导、SQL 渲染（`to_sql()`）、序列化。
它负责"这棵树长什么样"。

`parser.py` 是分词器加递归下降解析器。它负责"字符串怎么变成树"。

`eval.py` 是向量化求值：三值逻辑、LIKE 编译、常量折叠。它负责"树怎么变成掩码"。

`rewrite.py` 是改写：索引翻译、zone map 提取、化简、选择性估计。
它负责"树怎么变成别的树、或者变成一个非树的答案"。

数据流是单向的，没有回边：

```
"a > 1 AND name LIKE 'x%'"
    │  parser.parse(sql, schema)
    ▼
  Expr 树 ──rewrite.simplify──▶ 更短的 Expr 树
    │                              │
    │                              ├─rewrite.to_index_expr──▶ ScalarIndexExpr
    │                              ├─rewrite.split_conjuncts──▶ [合取项, …]
    │                              │      └─ scanner._stats_can_match ─▶ 剪片段
    │                              └─ 剩下的留给 eval
    ▼
eval.evaluate_predicate(expr, batch) ──▶ np.ndarray[bool]
```

**注意 `rewrite.py` 里还有一对函数叫 `extract_zone_map_predicate` /
`_zone_map_usable`，它们不在上面这条链上**——没有任何调用方。
剪枝真正走的是 `split_conjuncts` + `scanner._stats_can_match`。
两者都"没有 `Like` 分支"，所以对 `LIKE` 的结论是一样的；
但读代码时要跟的是后一条。§11.9 末尾把这件事单独讲了一遍，
因为它是"导出了不等于在跑"的典型。

**单向是硬要求。** 如果求值阶段能反过来改树（比如"发现某行是 NULL 就删掉这个分支"），
那么同一棵树在不同批次上就会变成不同的形状，缓存、计划复用、
可解释性全部失效。

还有一个刻意的设计：`parse(sql)` 不带 schema 也能用——列名原样保留、
类型标成"未知"，求值时再按 batch 里的实际列类型走。
带 schema 解析的好处是**列名写错立刻报错**：

```
parse('age > 1', schema)
-> PlanError: 没有名为 'age' 的列 (available=['id', 'name'],
   hint='Valid fields are `id`, `name`')
```

那句 `Valid fields are \`id\`, \`name\`` 是照抄真实 LanceDB 的措辞——
一个用户会反复看到的错误信息，措辞值得对齐。

## 11.3 解析器：优先级、引号、报错位置

优先级从低到高（和 SQL 标准一致，和 C 不同）：

```
OR
AND
NOT              （前缀，右结合）
比较 = != <> < <= > >=  LIKE  IN  IS  BETWEEN
+ - ||
* / %
一元 - +         （前缀，右结合）
::               （后缀，绑定最紧）
基本表达式
```

几个和 C 不一样的、必须记牢的点，用 `to_sql()` 打印规范形式验证。
**这张表不是凭印象记的**：`mini_lancedb/expr/parser.py` 的模块 docstring 把它逐层写了出来，
连"`::` 比一元负号更紧"这一条都单独列了一行；解析器按它递归下降，
`to_sql()` 的加括号判断也读它（`mini_lancedb/expr/nodes.py` 里的 `PREC_*`）：

```
a AND b = c      -> a AND b = c        （是 a AND (b = c)，不是 (a AND b) = c）
a OR b AND c     -> a OR b AND c       （AND 比 OR 紧）
NOT a = 1        -> NOT a = 1          （是 NOT (a = 1)，不是 (NOT a) = 1）
NOT NOT a        -> NOT NOT a          （右结合）
-a :: int        -> -CAST(a AS int32)  （:: 比一元负号更紧）
1 + 2 * 3        -> 1 + 2 * 3
a || b || c      -> a || b || c        （字符串拼接，和加减同级）
```

**为什么用递归下降而不是算符优先（shunting-yard）。** 因为 SQL 的表达式
语法不是纯运算符语法：`IN` 后面要跟括号列表、`LIKE` 后面可以跟 `ESCAPE`、
`BETWEEN` 中间夹着 `AND`、`CAST(x AS t)` 里 `AS` 的位置不能错。
递归下降可以给每一层配人话报错（"这里期待 `)`，实际读到 `AND`"），
而算符优先表做不到——它只会说"语法错误"。

### 词法层面的细节

**引号有三种。** 单引号是字符串字面量；双引号和反引号都是**标识符**引号，
内部引号翻倍转义。实测带空格的列名：

```
tokenize('"Upper Name" = 1 AND `a` = 2')

   ident 'Upper Name' 0
   op    '='          13
   number 1           15
   keyword 'AND'      17
   ident 'a'          21
   op    '='          25
   number 2           27
   eof   None         28
```

**多字符运算符必须按长度倒序匹配**，否则 `<=` 会被吃成 `<` 加 `=`：

```python
_OPERATORS = ("::", "||", "<=", ">=", "<>", "!=", "=", "<", ">",
              "+", "-", "*", "/", "%", "(", ")", "[", "]", ",", ".",)
```

**数字识别。** 整数不带小数点，带小数点或指数的一律是浮点。
实测：

```
a > 1e3      -> a > 1000.0
a > 1.5E-3   -> a > 0.0015
a > .5       -> a > 0.5          （小数点开头的字面量）
a = X'00ff'  -> a = X'00ff'      （十六进制 bytes 字面量，位数必须是偶数）
```

`X'..'` 在优先级表里的位置是**最紧的一档**：分词器把它切成一个 `bytes` token，
`parse_primary` 再把它变成 `Literal(value, BINARY)`——和 `1`、`'a'`、`NULL` 同级，
都归在"基本表达式"里，所以它上面不会挂任何运算符。位数必须是偶数这条
由分词器当场检查（奇数位直接抛 `ExpressionSyntaxError`），不是等到求值才发现。

**`ESCAPE` 只能是单个字符**，而且 `ESCAPE` 后面必须真的是字符串字面量。
这两条都在解析阶段检查，而不是等到匹配时才发现。

**函数名是一张白名单。** 表里没有的名字直接当成语法错误拒绝，
而不是"运行时再报未知函数"：

```
ExpressionSyntaxError: 未知函数 'unknown_fn'（可用函数: abs, array_contains,
array_has_all, array_has_any, array_length, ceil, ceiling, ...
```

白名单表是 `mini_lancedb/expr/parser.py` 里的 `FUNCTIONS`，一共 **33 个函数名**，
按它自己的分组注释分成四类：**数组 5 个**（`array_contains`、`array_has_any`、
`array_has_all`、`array_length`、`list_extract`）、**字符串 16 个**、**数值 9 个**、
**空值/结构体 3 个**（`coalesce`、`nullif`、`struct_field`）。
**33 个名字不等于 33 种实现**：`length` / `char_length` / `character_length` 是同一套
（`_fn_length`，`array_length` 也复用它），`ceil` / `ceiling` 是同一个，
`substr` / `substring` 是同一个。别名的作用是让习惯不同的人写得出自己那一版，
代价是白名单里多几行、`function` 分发时多几个 key。

**把可用函数列在报错里**看起来很啰嗦，
但它把"我该用什么"和"我写错了什么"合成了一次交互。

### 报错位置

每个 token 都带 `pos`（在原始字符串里的字符下标），出错时抛
`ExpressionSyntaxError`，它把 `position` 渲染成一行插入符。
实测输出：

```
parse('a = (1 + 2')
ExpressionSyntaxError: 括号没有闭合：这里期待 ')'，实际读到 表达式末尾
  a = (1 + 2
            ^

parse('a BETWEEN 1')
ExpressionSyntaxError: BETWEEN 中间缺少 AND，实际读到 表达式末尾
  a BETWEEN 1
             ^

parse("a LIKE 'x' ESCAPE 'ab'")
ExpressionSyntaxError: ESCAPE 只能是单个字符
  a LIKE 'x' ESCAPE 'ab'
                    ^

parse('a && b')
ExpressionSyntaxError: 无法识别的字符 '&'
  a && b
    ^

parse("a = 'unterminated")
ExpressionSyntaxError: 字符串字面量没有闭合（缺少结尾的单引号）
  a = 'unterminated
      ^

parse('a = 1 AND (b = 2))')
ExpressionSyntaxError: 表达式解析完了但还剩 ')' 没处理
  a = 1 AND (b = 2))
                   ^
```

这里有一处**文档和实现对不上**，值得如实记下来。
`parser.py` 的模块 docstring 里写这两行的形态是：

```
ExpressionSyntaxError: 这里期待 ')'
  a = (1 + 2
           ^
```

但 `str(e)` 的真实输出是把位置信息当成 context 渲染在括号里的：

```
ExpressionSyntaxError: 括号没有闭合：这里期待 ')'，实际读到 表达式末尾
(caret='          ^', expression='a = (1 + 2')
```

异常对象上确实有 `expression` 和 `position` 两个属性
（`e.position == 10`、`e.expression == 'a = (1 + 2'`），
上面那两行就是拿这两个属性自己拼出来的；但**默认的 `str()` 不会拼成两行**。
位置信息是完整的，展示层没有把它用完。

**设计课：** 报错质量是 `.where()` 这类 API 好不好用的决定性因素，
所以"信息齐全"和"呈现正确"要分开检查。一个异常里存着精确的
`position`，和用户在终端里看到插入符对齐，中间隔着一层渲染，
而那一层最容易在重构里掉队。

## 11.4 三值逻辑

SQL 的 `WHERE` 里，一个条件有三种结果而不是两种：TRUE、FALSE、UNKNOWN。
`WHERE` 的语义是"只有 TRUE 才留下"，所以 **UNKNOWN 和 FALSE 在筛选结果上
没有区别**——但它们在其他地方的区别是致命的。

本实现用一个 `ValueArray` 表达一列值：`values`（numpy 数组）
加一个独立的 `valid` 位图（`valid[i] = False` 就是 NULL）。
两个数组分开存，是为了让"值是 False 的行"和"值是 NULL 的行"
在内存里可区分——把 NULL 折进"值是 False"里就丢掉了三值逻辑。

**完整的真值表**（代码逐个格子跑出来，不是抄的）：

```
        AND     TRUE   FALSE   NULL      OR     TRUE   FALSE   NULL      NOT
  TRUE          TRUE   FALSE    NULL             TRUE    TRUE    TRUE    FALSE
 FALSE         FALSE   FALSE   FALSE             TRUE   FALSE    NULL     TRUE
  NULL          NULL   FALSE    NULL             TRUE    NULL    NULL     NULL
```

读法是：**AND 里 FALSE 是吸收元**（只要有一边 FALSE，结果就是 FALSE，
不管另一边是不是 NULL）；**OR 里 TRUE 是吸收元**。
`NOT NULL` 还是 NULL。

### 最容易错的几条

在真表上逐条验证（表是 `[{a:1}, {a:2}, {a:NULL}, {a:3}]`）：

```
WHERE a = 1                  命中 1 行 [1]
WHERE a <> 1                 命中 2 行 [2, 4]
WHERE NOT (a = 1)            命中 2 行 [2, 4]
WHERE a IS NULL              命中 1 行 [3]
WHERE a IS NOT NULL          命中 3 行 [1, 2, 4]
WHERE a IN (1, 2)            命中 2 行 [1, 2]
WHERE a NOT IN (1, 2)        命中 1 行 [4]
WHERE a NOT IN (1, NULL)     命中 0 行 []
WHERE a IN (1, NULL)         命中 1 行 [1]
WHERE a = NULL               命中 0 行 []
WHERE a <> NULL              命中 0 行 []
WHERE a BETWEEN 1 AND 2      命中 2 行 [1, 2]
WHERE a NOT BETWEEN 1 AND 2  命中 1 行 [4]
```

**第一条：`NULL = 1` 是 UNKNOWN，所以 `WHERE NOT (a = 1)` 会排除 NULL 行。**
`<>`、`!=`、`NOT(...)` 三种写法在这里**完全一致**（都是 2 行），
因为 `NOT UNKNOWN` 还是 UNKNOWN。一个 `a` 有 NULL 的表上，
`WHERE a <> 1` 永远拿不到 NULL 行——连 `NOT` 也救不回来。

**第二条：`x NOT IN (1, NULL)` 永远不为真。**
这不是特例，是展开式的自动结果：

```
x IN (a, b)     <=>  x = a OR  x = b
x NOT IN (a, b) <=>  x <> a AND x <> b
```

于是 `x NOT IN (1, NULL)` 变成 `x <> 1 AND x <> NULL`。
右边那个是 UNKNOWN，而 UNKNOWN 在 AND 里**只在另一边是 FALSE 时才被吸收**，
否则它会吃掉整个结果。所以只要 `x <> 1` 不是 FALSE，答案就是 UNKNOWN，
行被排除。这就是"为什么写了 NOT IN 反而一行都不剩"。

实现上没有任何特判：

```python
# IN 是等值比较的 OR，NOT IN 是不等比较的 AND。
# 这样 NULL 的传播就是三值逻辑自动的结果，不需要任何特判 ——
# 特判恰恰是 NOT IN + NULL 这类 bug 的温床。
```

**第三条：`NULL AND FALSE` 是 FALSE，但 `NULL AND TRUE` 是 NULL。**
从真值表的第三行能直接读出来：`NULL AND FALSE = FALSE`
（FALSE 是吸收元），`NULL AND TRUE = NULL`。这个不对称性是
"短路能不能做"的全部依据——下面 11.6 节会用到它。

**第四条：除零是 NULL，不是异常。**

```
a / b                  -> [2, None, -3, None]
a % b                  -> [0, None, -1, None]
a / b > 1              -> [True, None, False, None]
b <> 0 AND a / b > 1   -> [True, False, False, None]
```

`7 / 0` 在 SQL 里没有定义值，向量化执行时抛异常会让整个查询挂掉。
返回 NULL 才是可用的行为，而且它让 `WHERE b <> 0 AND a / b > 1`
这种写法自然成立（除零的行是 UNKNOWN，被 AND 吃掉）。

顺带两条本实现刻意对齐 DataFusion / PostgreSQL 的规则：
**两个整数相除做截断整除**（`7/2 = 3`、`-7/2 = -3`，不是 `-4`），
只要有一边是浮点就走真除法；`%` 用 `np.fmod`（余数符号跟被除数，
`-7 % 2 = -1`），而不是 numpy 的 `np.mod`（符号跟除数）。

还有一条很容易漏的实现细节：除法/取模的前面要把除数里的 0
**换成 1 再算**，然后再把那些位置标成 NULL。直接算会触发 numpy 的
`RuntimeWarning`，而警告在批量执行里会变成日志噪声甚至异常升级。

**`IS NULL` 是唯一一个永远返回 TRUE/FALSE 的谓词。**

```python
out = ~child.valid if not node.negated else child.valid
return ValueArray.wrap(out, np.ones(len(out), dtype=bool), BOOL)
```

`wrap` 的第二个参数是**全 True 的 valid 位图**——`x IS NULL`
的答案一定是确定的，要么是、要么不是。`NULL IS NULL` 为 TRUE
就是这条规则的结果，也是它和 `x = NULL`（永远是 UNKNOWN）最本质的区别。

## 11.5 NaN 与 null：一个真实的语义分歧

浮点里有个比 NULL 更麻烦的东西：NaN。它有两条特殊规则——
和任何值（包括自己）比较都是 false，且不等于自己。
SQL 的三值逻辑和这个规则**不兼容**，于是各家实现分成了两派。

本实现把 NaN 当"未知"处理：**任何一边是 NaN，比较结果就是 NULL。**

```
列: [{'f': 1.0}, {'f': nan}, {'f': None}, {'f': 2.0}]
f = f       -> [True, None, None, True]
f = 1.0     -> [True, None, None, False]
f > 1.0     -> [False, None, None, True]
f <> f      -> [False, None, None, False]
f IS NULL   -> [False, False, True, False]
f IS NOT NULL -> [True, True, False, True]
```

注意第三行 `f = f`：第一行（1.0）是 TRUE，第二行（NaN）是 NULL。
如果按 IEEE 754 走，`NaN = NaN` 应该是 false；
本实现给的是 NULL。第四行 `f <> f` 同样。

**真实 LanceDB 0.40.0 走的是另一派：Arrow 的比较内核，用全序。**
在全序里 NaN 被排到所有数之后，`NaN = NaN` 为真、`NaN > 1.0` 也为真。

**这两边的证据强度不一样，别读成"两边都验证过"。** 上面那一列是本实现的实测，
`examples/verify/ch11_expression_engine.py` 的第 5 节当场把它跑出来，随时可以复跑；
右边（真实 LanceDB 的全序）是**引用值**——它来自一份对 0.40.0 的 API 实测记录，
那份记录没有随本章脚本复跑，本章脚本也只跑了左边一侧（它把右边这条当成一行字符串打印出来）。
所以这一节说的"分歧"是：**左边本机可复现，右边引用自别处**。要把它当实验结论用，
得先自己在 0.40.0 上跑一遍 `n > 1.0` 看它会不会把 NaN 行选出来。

这个分歧有一个非常具体的后果，**落在向量列上**：

```
f IS NOT NULL -> [True, True, False, True]
```

一个存着 NaN 的向量，`IS NOT NULL` 判它**是**非空。
所以在"用全序"的那一派里，NaN 行会照常参与比较和排序；
在"当未知"的这一派里，NaN 行会被所有比较条件排除，但**躲得过 `IS NOT NULL`**。

两种都自洽，但不能混。混了会出现"这一层说这行是活的、那一层说这行比不了"
的状态。真实 LanceDB 的向量列有一条相关约束：
写入时 NaN 向量会被 `on_bad_vectors` 拦下（`error` 拒绝、
`fill` 用填充值替换、`null` 置空），**这是比"查询时怎么比"更根本的一道闸**。

选择哪一派是个工程决定：**全序**让排序和范围扫描有一致的语义，
代价是 `NaN = NaN` 为真这件事会让用户困惑；
**当未知**符合 SQL 用户的直觉，代价是 NaN 行"存在但不可比"，
在 `IS NOT NULL` 和 `NOT (x = y)` 之间会产生不对称。

## 11.6 向量化求值

求值必须在整列上做。`evaluate_predicate` 返回一个长度等于行数的
布尔掩码 `np.asarray(value.values, dtype=bool) & value.valid`——
最后那个 `& value.valid` 就是"只有 TRUE 留下"的落地点。

第 10 章 §10.10 量过这件事的收益，也量过它的边界：数值谓词
（`n % 7 = 0`、`n * 3 + 1 < 100`）numpy 比逐行 Python 快约 1.9 倍，
纯字符串谓词（`starts_with(s, 'item-1')`）反而**慢约 14 倍**。
数字和量法都在那一节，这里不重复——要记住的是结论的形状：
**"向量化"不是一个可以整体宣称的性质，而是一份逐算子的清单。**
11.9 节会讲字符串那一格为什么是反过来，以及它跟"函数写在哪一层"的关系。

### 短路有两层

**常量折叠层**（编译期）。左操作数是常量时，右操作数可以不求值：

```python
if op in ("AND", "OR"):
    if _is_constant(node.left):
        left = _eval(node.left, ctx)
        const_true  = bool(left.valid[0]) and bool(left.values[0])
        const_false = bool(left.valid[0]) and not bool(left.values[0])
        if op == "AND":
            if const_false: return 整列 FALSE     # FALSE 是吸收元
            if const_true:  return _eval(node.right, ctx)
        else:
            if const_true:  return 整列 TRUE      # TRUE 是吸收元
            if const_false: return _eval(node.right, ctx)
```

这里严格按真值表来：`FALSE AND x` 直接是 FALSE，`TRUE OR x` 直接是 TRUE。
但 `NULL AND x` **不能**短路——因为 x 为 FALSE 时答案是 FALSE，
x 为 TRUE/NULL 时答案是 NULL，必须求值才知道。

**运行期层**（整列短路）。左边整列都是 FALSE（AND）或整列都是 TRUE（OR）时，
右边不用算：

```python
if op == "AND" and not _true_mask(left).any() and _false_mask(left).any():
    return left
if op == "OR" and not _false_mask(left).any() and _true_mask(left).any():
    return left
```

实测收益（20 万行，每档跑 20 次取最小值）：

```
FALSE AND n % 7 = 0      0.032 ms
TRUE  AND n % 7 = 0      3.252 ms      （跑 20 次的中位数是 5.379 ms）
TRUE  OR  n % 7 = 0      0.031 ms
FALSE OR  n % 7 = 0      3.237 ms
```

**两个数量级的差距。** 左操作数是常量时，右边的整列计算完全不发生：
0.03 毫秒是"造一个 20 万行的常量掩码"的时间，
3.2 毫秒才是真正算 `n % 7` 的时间。

### 常量折叠

`fold_constants` 自底向上重建，遇到"全是字面量"的子树就丢进一个
**1 行**的上下文里跑一遍真正的向量化求值器：

```python
def _to_literal(expr):
    ctx = _Ctx(None, 1)
    value = _eval(expr, ctx)
    ...
```

**不另写一套标量求值**是这个实现里最重要的一处取舍。
折叠的结果和运行时的结果必须用的是同一份语义（尤其三值逻辑），
而"再写一个简单版求值器"是这类系统里最容易产生
"折叠前后行为不一致"的地方。

实测（`simplify` 之后再 `fold_constants`）：

```
a > 1 + 2                simplify -> a > 3        fold -> a > 3
a > 2 * 3 + 4            simplify -> a > 10       fold -> a > 10
TRUE AND a = 1           simplify -> a = 1        fold -> a = 1
FALSE AND a = 1          simplify -> FALSE        fold -> FALSE
a = 1 AND a = 1          simplify -> a = 1        fold -> a = 1
a = 1 OR a = 1           simplify -> a = 1        fold -> a = 1
NOT NOT (a = 1)          simplify -> a = 1        fold -> a = 1
1 + 2 * 3 > a            simplify -> 7 > a        fold -> 7 > a
NULL AND a = 1           simplify -> NULL AND a = 1   fold -> NULL AND a = 1
```

`a > 1 + 2` 变成 `a > 3` 不只是好看——**索引改写只认
"列 op 字面量"这个形状**，`a > 1 + 2` 里的右边是一棵加法树，
不折叠就翻译不出来。

最后一行是文档和实现的一处小出入：模块 docstring 说
"`NULL AND x` 这类整棵子树直接塌缩"，但实测它**没有**塌缩。
这是对的——按真值表，`NULL AND x` 在 `x` 为 FALSE 时是 FALSE、
否则是 NULL，不能塌缩成一个常量。能塌缩的只有 `FALSE AND x`（→FALSE）
和 `TRUE AND x`（→x），代码做的正是这两件。

## 11.7 索引改写：`to_index_expr` 的契约

索引拿到的不是表达式树，而是 `ScalarIndexExpr`——区间、集合、与或非的代数。
`to_index_expr(expr, column)` 负责翻译，**答不了就返回 `None`**。
"返回 `None`"是这一层最重要的契约：它明确把责任交回给调用方
（退化成全表扫描），而不是返回一个"大概能用"的东西。

实测的翻译结果：

```
n = 5                     -> RangeQuery(start=5, end=5, ...)
n != 5                    -> Not(RangeQuery(5, 5, ...))
n < 5                     -> RangeQuery(start=None, end=5, inclusive_end=False)
n >= 5                    -> RangeQuery(start=5, end=None, ...)
n IN (1, 2, 3)            -> IsIn(values=[1, 2, 3])
n NOT IN (1, 2)           -> Not(IsIn(values=[1, 2]))
n BETWEEN 1 AND 9         -> RangeQuery(start=1, end=9, ...)
s LIKE 'abc'              -> RangeQuery(start='abc', end='abc', ...)
s LIKE 'abc%'             -> RangeQuery(start='abc', end='abc￿', ...)
s LIKE '%abc'             -> None
s LIKE 'a_c'              -> None
n = 5 AND s = 'x'         -> RangeQuery(5, 5)          （部分下推）
n = 5 OR s = 'x'          -> None                       （全有全无）
n = 5 OR n = 7            -> Or([RangeQuery(5,5), RangeQuery(7,7)])
NOT n = 5                 -> Not(RangeQuery(5, 5))
n NOT BETWEEN 1 AND 9     -> Not(RangeQuery(1, 9))
n IS NULL                 -> None
n + 1 = 5                 -> None
n = NULL                  -> None
```

几条要读出来的取舍：

**`AND` 返回的是超集。** `n = 5 AND s = 'x'` 只翻译出 `n = 5` 那一半，
`Or` 的部分交给过滤器。这是安全的，因为**超集再交一次精确过滤仍然精确**。

**`OR` 只要有一个分支翻译不了就整体返回 `None`。** 少下推一边会让结果
**变小**，也就是静默丢数据。第 10 章量过这个不对称的代价：
同一个列、同一个索引，`AND` 把扫描砍到 4,286 行，`OR` 直接掉回 30,000 行。

**`n = NULL` 不翻译。** `_scalar_literal` 明确把 `None` 和列表都排除在外：

```python
if ok and value is not None and not isinstance(value, (list, tuple, dict)):
    return value
return _MISSING
```

原因写在注释里：`col = NULL` 在 SQL 里永远不成立（不是"等于空"，
是"未知"），把它翻译成索引区间会得到一个**语义完全不同的结果**。
数组值也不翻译，因为标量索引不索引数组值（数组要走 LABEL_LIST）。

**`n = 5 AND n < 3` 会翻译成一个恒空的交集。** 实测它老老实实给出
`And([RangeQuery(5,5), RangeQuery(None,3)])`——模拟器没有做
"区间相交为空就整体短路"的化简，也就是**这个矛盾谓词仍然会去查一次索引**。
这不影响正确性（交集为空，返回 0 行），但是一次可以省掉的查询。

**`LIKE 'abc%'` 翻译成 `[abc, abc￿]` 是本实现比真实 Lance
多走的一步。** 它的正确性依赖"字符串比较等价于字节序比较"，
这在 UTF-8 上是成立的。真实 Lance 的 BTREE 对 `LIKE` 前缀的处理
放在 ngram / fts 那一侧。

**`s LIKE 'a_c'` 翻译不了。** 下划线匹配"任意单个字符"，
在按字节序排的索引上没有对应的区间（它不是一个连续范围）。

## 11.8 选择性估计

**先说清楚这一节的位置，免得读出一份不存在的依赖关系。**
真实优化器需要估计"这个条件能筛掉多少行"，因为**它要自己决定**
用 prefilter 还是 postfilter，估错了的代价是十倍以上的延迟差距。
本实现里有这个估计器（`estimate_selectivity`，下面全部是它的实测输出），
但**扫描器目前并不调用它**——prefilter / postfilter 在本实现里是
用户显式设的开关：`Query.where(where, prefilter=True)`
（`mini_lancedb/query.py`）。所以这一节讲的是
"一个确定性的、可回归的选择性估计器怎么设计、能给出什么"，
而不是"查询规划器怎么靠它做决策"。这两件事在真实系统里是连起来的，
在这里**还没有连**。

这个定位很重要，因为它直接决定哪些结论可以信：下面的每一个数字
都能逐位复现，但**"所以扫描器会更聪明地选择"这一步是不成立的**——
没有调用方。判断一个优化器组件有没有生效，和 §11.9 末尾那条教训
是同一条：看调用链，别看它有没有被导出。

设计上，这个估计量必须**确定、可解释、可回归测试**，
而不是"差不多就行"。

实测的估计值：

```
n = 1                  -> 0.1000       （等值，无统计量 → 默认 1/10）
n > 1                  -> 0.3000       （范围，默认 0.3）
n > 999999             -> 0.3000       （没有统计量，猜不出常量落在值域外）
n IS NULL              -> 0.1000
s LIKE 'item-%'        -> 0.2500       （前缀能砍掉一大部分）
s LIKE '%x%'           -> 0.5000       （子串匹配几乎筛不掉东西）
n > 1 AND n < 5        -> 0.0900       （两个估计值相乘）
n > 1 OR n < 5         -> 0.6000       （两个相加，截断到 1）
NOT n = 1              -> 0.9000       （1 - 内层估计）
n IN (1,2,3)           -> 0.3000       （3 × 等值选择率）
```

有统计量的时候，它能给出比默认值有用得多的答案：

```
带统计量 n = 1        -> 0.001000      （1/ndv = 1/1000）
带统计量 n > 5000     -> 0.000000      （常量落在值域外 → 直接 0）
带统计量 n < 0        -> 0.000000
```

那两行 `0.000000` 是这个估计器能给出的**最有价值的一类答案**：
常量完全落在 `[min, max]` 之外时，返回 0 会让扫描器直接把
这一列的数据页全跳过。无统计量时它只能给 0.3——差了一个数量级。

三条设计原则写在代码注释里，值得抄出来：

**宁可不准，也不能因为"没有统计量"就给出 0 或 1 这种会误导扫描器的极端值。**
所以缺统计量时一律回退到默认值（等值 0.1、范围 0.3、子串 0.5）。

**估计必须是确定函数。** 同样的输入必得同样的输出，
不带随机、不带时钟、不带自适应——否则没法回归测试。

**它会污染计划的选择。** `AND` 的估计是相乘、`OR` 是相加（截到 1）、
`NOT` 是取补。乘法会让"三个都还行的条件"的估计值变成 0.027，
把一个实际上中等选择性的谓词误判成"极度挑食"。这是
独立假设的经典误差，真实优化器要靠直方图和列相关性来修正，
而一个"粗但可解释"的估计器**明确放弃了这个精度**，
换来的是可回归、可推理。

## 11.9 LIKE 的两个快速路径

`LIKE` 是本实现里唯一一类"先看模式形状再挑算法"的谓词。
`LikeMatcher` 在构造时就把模式分类：

```
pattern='abc'    kind=eq             literal='abc'      regex='^abc$'
pattern='abc%'   kind=prefix         literal='abc'      regex='^abc.*$'
pattern='%abc'   kind=suffix         literal='abc'      regex='^.*abc$'
pattern='%abc%'  kind=contains       literal='abc'      regex='^.*abc.*$'
pattern='a%c'    kind=prefix_suffix  literal=None       regex='^a.*c$'
pattern='a_c'    kind=regex          literal=None       regex='^a.c$'
pattern='a%c%'   kind=regex          literal=None       regex='^a.*c.*$'
```

**为什么这件事对索引使用是决定性的：**

`kind == "eq"` —— 模式里一个通配符都没有。它是**等值比较**，
`to_index_expr` 给的正是 `RangeQuery('abc', 'abc')`，
所以在 BTREE 上能吃到范围查询。

**这里有一个容易想当然的地方：它吃不到 zone map 剪枝。**
`LIKE 'abc'` 和 `= 'abc'` 在表达式层面是两棵不同的树，
而**统计剪枝那一层只认其中一棵**——实测在同一个片段上，
`s = 'abc'` 能被剪掉、`s LIKE 'abc'` 不会。

原因在**扫描器**这一侧。剪枝的入口是
`Scanner` 先把过滤条件 `split_conjuncts()` 拆成一串合取项，
再对每一项调 `scanner._stats_can_match(conjunct, fragment, schema)`
（`mini_lancedb/scanner.py`）。这个函数只认四种形状，
而且**没有 `Like` 分支**：

`BinaryOp(列, op, 常量)`——拿片段的 min/max 比一下；
`Between`——同理；`IsNull` / `IsNotNull`——看 `null_count`；
`IsIn`——**直接返回 `True`**（代码注释写着"小集合不值得再细算"，
意思是"不剪"）。返回 `None` 代表"不知道"，也不算剪。

于是结论成立：`LIKE` 无论长成什么样，都返回 `None`，
一个片段都剪不掉。这是一个"两个优化各自都对、合起来有缺口"的例子：
`LikeMatcher` 认得"`s LIKE 'abc'` 其实就是等值"，
但剪枝那一层不认得——它只看 AST 的形状，不看语义。
写查询时的实用结论：**确定是等值就别写成 `LIKE 'abc'`**，
写成 `= 'abc'` 才能同时拿到索引和统计剪枝。

**顺带记一个我在核对这一节时发现的自己的问题：这里引用错了函数。**
`mini_lancedb/expr/rewrite.py` 里确实有
`extract_zone_map_predicate()` 和它内部的 `_zone_map_usable()`，
它们的文档串写的正是"只认 列 op 常量 / IN / BETWEEN 三种形状"——
和我们上面推出来的结论一模一样。**但没有任何代码调用它们**：
`Scanner` 走的是 `split_conjuncts` + `_stats_can_match` 这条路径。
换句话说，`rewrite.py` 里那对函数是**死代码**：文档齐全、语义正确、
导出在 `expr/__init__.py` 里，就是没人用。

这件事本身值得记，因为它同时是两种错：
**一是文档误导**——照那个函数名去读剪枝逻辑，会读到一套没生效的规则；
**二是"导出了就等于在用"的错觉**——`__all__` 里有名字、
`import` 得进来、注释写得像模像样，都不代表它在执行路径上。
判断"一段代码有没有生效"，唯一可靠的办法是从入口往下跟一遍调用链，
或者直接改一行、看输出变不变。

`kind == "prefix"` —— `'abc%'`。前缀是**有序的**，
在 BTREE 上就是一次范围查询 `[abc, abc￿]`。
退一步说，`startswith` 也比跑正则快一个数量级。

`kind == "contains"` —— `'%abc%'`。**无解**，只能全表扫。
BTREE 帮不上忙（子串不保序），得靠 NGRAM 索引或者倒排 / FTS。

其它形状只能编译成正则逐行跑。

前三种有**向量化实现**，并且保证和正则路径**逐位相同**
（测试里用生成器随机造模式做过对照）。

### 真实数据上的验证

```
s LIKE 'abc'                     -> ['abc']
s LIKE 'abc%'                    -> ['abc', 'abcd']
s LIKE '%abc'                    -> ['abc', 'xabc']
s LIKE '%abc%'                   -> ['abc', 'abcd', 'xabc']
s LIKE 'a_c'                     -> ['abc', 'a%c', 'aXc']
s LIKE 'a\%c' ESCAPE '\'         -> ['a%c']
s LIKE 'a%c'                     -> ['abc', 'a%c', 'aXc']
s LIKE '%'                       -> 全部 9 行（包括空字符串）
```

要读出三件事。

**`a_c` 里的 `_` 匹配"任意单个字符"**，所以它同时命中 `abc`、
`a%c`、`aXc`。**`_` 不是"字母"**——这是用户最容易误解的一条。

**`ESCAPE` 把 `%` 变成字面量。** 带 `ESCAPE '\'` 时 `'a\%c'`
只命中 `a%c`；不带 `ESCAPE` 时 `\` 只是普通字符，`%` 仍然是通配符。
实测两种解释的编译结果不同：

```
compile_like('50%', '\')  -> prefix '50'    正则 '^50.*$'
compile_like('50\%')      -> prefix '50\'   正则 '^50\\.*$'
```

**`'%'` 匹配所有行，包括空字符串。** 翻译成 `^.*$`，
而 `.*` 匹配空串。这是正确的（SQL 的 `%` 匹配零个或多个字符），
但"用 `LIKE '%'` 代替 `IS NOT NULL`"这种写法会意外地把空串也算进来。

还有一个容易漏的实现细节写在 `match` 里：**`%` 必须编译成 `.*`
并开启 `re.DOTALL`**，因为 Python 的 `.` 默认不匹配换行。
一个多行文本字段上的 `LIKE '%x%'`，忘了这个 flag 就会
"明明有 x 却匹配不到"，而且极难定位。

`prefix_suffix`（`'a%b'`）那条路径有一个只在边界上出现的坑，
源码注释写得很清楚：

```python
# prefix_suffix（``'a%b'``）：先按前缀筛，再在命中的行里查后缀。
# **必须同时要求长度 >= len(前缀) + len(后缀)** —— 否则 ``'ba'`` 会被
# 判成匹配 ``'ba%ba'``（它确实既以 ba 开头又以 ba 结尾，只是前后缀
# 重叠了）。这个 bug 只在"待匹配串比前后缀之和还短"时出现，
# 是最容易被漏掉的一类。
```

也就是说，`'ba%ba'` 这个模式**不能**只用"以 ba 开头 且 以 ba 结尾"来筛——
`'ba'` 两个条件都满足（前缀和后缀重叠在了同一段字符上），
但要前后缀各占一段不重叠的字符，长度至少得是 4。
实测两条路都把长度条件算进去了，且逐位一致：

```
  'ba'    快速路径=False  正则=False
  'baba'  快速路径=True   正则=True      （% 匹配空串）
  'baXba' 快速路径=True   正则=True      （% 匹配 'X'）
  'bab'   快速路径=False  正则=False
```

这段逻辑还顺带说明了 `%` 的一个容易忘的性质：**它匹配零个或多个字符**，
所以 `baba` 是 `ba%ba` 的匹配。

### 快速路径写在哪里，是有讲究的

第 10 章 §10.10 量到过一件反直觉的事：同样语义的两对写法，
**`LIKE 'item-1%'` 比 `starts_with(s, 'item-1')` 快 3~5 倍**，
`LIKE '%1%'` 比 `contains(s, '1')` 快约 3 倍；等值形状（`LIKE 'item-1'`
对 `lower(s) = 'item-1'`）是同一个故事。数字和量法都在那一节，这里只回答
一句"为什么"——因为两组写法虽然语义相同，**跑的不是同一条代码路径**。

原因不在这两个函数的名字里，而在实现里：
`LikeMatcher.match` 对 `prefix` / `suffix` / `contains` 三种形状
分别调 `_str_startswith` / `_str_endswith` / `_str_contains`，
它们走的是 `np.char.*` 的 C 实现；
而 `startswith` / `ends_with` / `contains` 这三个**同名同义的 SQL 函数**
在函数表里走的是 `_fn_string_pred`，一个**逐行 Python 循环**：

```python
def impl(args, n):
    ...
    for i in range(n):          # 逐行
        if not valid[i]:
            continue
        ...
        out[i] = fn(x, y)
```

同一个引擎里，两个做到同一件事的入口，性能差 3~5 倍，
而且这个差距**完全看不出来**——函数文档里不会写，
测试里两者结果一致所以测试全绿，只有拿计时器量才会暴露。

顺带看那个循环里的 `a.values[i]`：`values` 是个**属性**，
每次取都在做一次 `.view()`。用 `cProfile` 跑 3 次（每次 20 万行），
`values` 一共被调用了 120 万次，累计耗时占整个调用的 **39%**
（`tottime` 也有 23%）。把它提到循环外面（`av = a.values` 取一次）
就能省掉其中大部分——三行以内、纯收益的改动。

一个附带发现：`_fn_string_pred` 里的 try/except 是**用来兜类型错误的**
（`str(x).startswith(str(y))` 对非字符串参数），
所以它每行都包在异常处理里。异常在 CPython 里代价很低
（没抛出来的时候接近零成本），这不是这里的瓶颈；
真正的瓶颈是循环本身和重复的属性访问。

**设计课：** "这个引擎是向量化的"这句话没有任何信息量。
真正有用的问题是**"这个算子在哪一层实现"**——
是按模式分类的 LIKE 里、还是在通用函数表里。
后者是"加一个新函数只要写几行"的代价，
而性能恰恰会在用户最常用的那几个函数上被收走。

## 11.10 类型、时间，以及这台引擎的边界

这一节回答 §11.1 里被顺口带过、却从没正面回答的问题：`a > 1` 里的 `1` 是
`int64` 还是 `float64`？`'x' || 1` 能不能算？时间列上的比较到底在比什么？
顺带把"这个引擎**不**做什么"一次说清，免得读者拿它当完整 SQL 实现期待。

### 时间列就是整数列

`mini_lancedb/types.py` 里的时间类型有五个：`Date32`（自 1970-01-01 起的**天数**，
内存里是 `int32`）、`Date64`（毫秒，`int64`）、`Time64[us|ns]`、`Timestamp[unit, tz]`
（epoch 计数，`int64`）、`Duration[unit]`。它们的共同点是 `is_temporal = True`，
而更重要的共同点写在 `numpy_dtype` 上：**除 `date32` 是 int32 外，其余全是 int64**。

所以时间列上的比较就是**整数比较**。没有日历运算，没有时区换算，
没有"月末"这种东西——`ts > 1780000000000000` 比的是两个整数。
`Timestamp.coerce_scalar` / `Date32.coerce_scalar` 定义了 Python `datetime` /
`date` → epoch 计数的换算规则，但**这条规则在本实现里没有被调用**：
`PrimitiveArray.from_pylist` 走的是 `buf[i] = v`，所以往 timestamp 列写一个
`datetime` 会抛 `TypeMismatchError`，能写进去的只有整数。

这解释了 §11.1 里那个看起来很自然的例子为什么在这台引擎上不成立。
`WHERE ts > '2024-01-01'` 在解析阶段完全合法（右边就是个 utf8 字面量），
但求值时 `_compare` 会让 `np.greater(int64 数组, 字符串数组)` 抛
`TypeError: '>' not supported between instances of 'int' and 'str'`——
**没有隐式转换**。绕道 `CAST('2024-01-01' AS timestamp)` 也不行：
`_eval_cast` 对"字符串 → 整数"走 `int(raw)`，失败、再试 `float(raw)`，再失败，
于是按 TRY_CAST 语义把这行**静默置成 NULL**，比较结果恒为 NULL，一行都不返回。
另外 `CAST(a AS date32)` 和 `a::date` 会在解析阶段直接抛
`SchemaError: 无法解析类型字符串 'date32'`——`parse_type_name` 的别名表里有
`date → date32`，但 `parse_type("date32")` 这条分支并不存在。

**结论：这台引擎能存时间、能比时间，但没有日期字面量、没有字符串与时间的互转。**
时间语义完全由写入方负责——写进去的整数必须已经是 epoch 计数。真实 LanceDB 的
时间类型建在 pyarrow 那一层（第 5 章），换算由它负责；本实现没有把这一层复刻下来，
所以时间列只能用整数写、用整数比。

### 算术的类型提升

下面拿 `i` 表示 `int64` 列、`n32` 表示 `int32` 列、`f32` / `f64` 表示两个浮点列。

`a > 1` 里的 `1` 是 `int64`：`_infer_literal_type`（`mini_lancedb/expr/nodes.py`）
刻意和 `infer_type` 对齐——Python `int` → `int64`、`float` → `float64`，
**不做"挑最窄的整数"那种优化**，理由是类型一旦写进 AST 就会被下游当契约用，
随常量值漂移只会带来惊喜。所以 `1.0` 是 `float64`，`nan` / `inf` 裸写也是 `float64`。

接下来这一段要说清一个很容易被写错的机制：**算术的结果类型有两套规则，
它们对同一个表达式会给出不同的答案。**

一套在 AST 层，`infer_binary_type`（`mini_lancedb/expr/nodes.py`），
它走 `promote_types`，取两侧的**公共超类型**。
这套规则决定了 `parse()` 出来的那个表达式**声称**自己是什么类型。

另一套在求值层，`_arith`（`mini_lancedb/expr/eval.py`），
它按下面的规则算，决定了**实际**算出什么类型。

实测同一批表达式在两套规则下的结果（`i` 是 int64、`n32` 是 int32、`f32` 是 float32）：

| 表达式 | `parse().type`（AST 契约） | `evaluate().type`（运行时） | 底层 `dtype` |
|---|---|---|---|
| `n32 + i` | int64 | **int32** | int64 |
| `n32 % i` | int64 | **int32** | int64 |
| `n32 / i` | int64 | int64 | int64 |
| `n32 * n32` | int32 | int32 | int32 |
| `i + f32` | float32 | float32 | **float64** |

**前两行是不一致的**：AST 说 int64，运行时说 int32。
这不是笔误，是两条路径各自实现的后果——
`infer_binary_type` 走提升，`_arith` 走下面的"取左操作数"规则。

这个分叉的后果要具体看。**如果你拿 `parse(...).type` 去做决策**
（比如判断"这个表达式能不能用在只接受 int32 的地方"），
你得到的是提升后的宽类型；而实际执行出来的值是窄类型。
在大多数查询里两者都能跑通，但在做**类型驱动的优化决策**时，
这个差异会变成"计划说的和执行的不是一回事"。

回到 `_arith` 自己的规则，三条：

**两个整数运算，结果类型取左操作数的类型。** 不是更宽的那个——
`n32 + i`（int32 + int64）得到 int32，反过来 `i + n32` 得到 int64，
`n32 % i` 得到 int32。唯一的例外是整数除法 `/`：它走 `promote_types`，
`n32 / i` 得到 int64。

**只要有一边是浮点，结果类型取浮点那一侧**，而不是更宽的那侧：
`i + f32` 得到 `float32`，`i + f64` 得到 `float64`。这是 `promote_types` 里
"整数 × 浮点 → 浮点"这条分支直接返回浮点侧的结果。

**逻辑类型和实际计算精度是两件事。** 计算走 numpy 的 ufunc，
`int64 + float32` 在 numpy 里升到 float64 算，算完再按上面算出的
`result_type` 记逻辑类型。所以 `i + f32` 的 `type.name` 是 `float32`，
底层 `values.dtype` 却是 `float64`——读 `type` 看的是契约，读 `dtype` 看的是实际。

拼接则是另一条路：`'x' || 1` **能算**（`_concat` 对两侧都做 `str()`，结果是 `'x1'`，
类型 utf8，NULL 照常传播），但 `'x' + 1` 不行——`+` 只认数值，求值时抛
`PlanError: 运算符 + 不能作用在 utf8 和 int64 上`，提示本身就写着"字符串拼接请用 `||`"。

### 没有 collation，只有一个 ILIKE

引擎里没有排序规则这个概念：所有字符串比较都是 numpy 的字节序
（UTF-8 下等价于码点序），所以 `'Z' < 'a'` 恒成立、非 ASCII 也照码点排。
第 10 章 §10.6 说过字节序剪枝是**安全的**，原因就在这里——它和求值用的是同一套序。

唯一一处"规范化"是 `ILIKE`。解析器在 `_finish_like` 里把它改写成
`lower(s) LIKE lower('ab%')`，`lower` 走 Python 的 `str.lower()`：
它认 Unicode 的大小写映射，但不认 locale，也不是真正的 collation
（没有重音折叠、没有 `ß → ss` 这类规则）。够用，但别把它当 ICU。

### 这个引擎不做的事

**没有绑定参数。** 分词器不认 `$1`、`?`、`:name`（`:` 甚至不在运算符表里），
`parse('a $1')` 会报"无法识别的字符 '$'"。要参数化只能自己拼字符串，
然后自己负责转义——这也是为什么 `.where()` 的第一道防线是"根本不 `eval` 用户输入"
（见 `mini_lancedb/expr/parser.py` 的模块 docstring）。

**没有跨查询的计划缓存。** 表达式这一层里叫"缓存"的东西都是
**单次求值内部**的：`_Ctx._cache` 保证同一批里重复引用的列只取一次
（`ValueArray.values` 是个属性，每次取都在 `.view()`），
`LikeMatcher` 在"模式也是常量"的那条路径上编译一次复用。
`serialize()` / `deserialize()` 能把 AST 变成纯 JSON（`Literal` 的值都过
`_jsonable_scalar`，`bytes` 用 `{"__bytes__": ...}` 标记），这是"计划缓存"的地基，
但本实现没有在上面盖房子。

**但别把这句话推成"这个实现没有跨查询的缓存"——有一处是有的，
只是不在这一层。** `Dataset.load_index(name, *, use_cache=True)` 带一个
**进程级、跨查询**的索引缓存，key 是 `(uri, 索引名, 索引版本, 文件清单)`。
第 10 章 §10.7 量过它的效果：同一个点查，冷的那次比热的那次多读
**正好一个索引文件的字节数**（609,127），清缓存后又回到冷值。
所以准确的表述是分层的：**求值层没有跨查询缓存，索引层有；
计划层两者都没有。** 这一处特别容易说错，因为"没有缓存"是个很强的
否定判断，而它一旦被写宽了，就会让读者错过那个真实存在的、
量得出来的优化。

**没有 `ORDER BY`。** `Scanner` 只按片段顺序产出行；唯一的排序在检索路径里——
按 `_distance` 升序或 `_score` 降序取 top-k（`np.argsort(..., kind="stable")`）。
所以 SQL 里那个"NULL 排前面还是后面"的问题（Postgres 默认 `NULLS LAST`，
MySQL / SQLite 反过来）在这里没有对应物。

**没有子查询和聚合。** `parse` 只解析**表达式**。同样的输入报什么错，
取决于你在哪一层调用它：不带 schema 调 `parse(sql)`，`(SELECT 1)` 里
`SELECT` 被当成标识符、括号对不上，报"括号没有闭合"；带 schema 调
（`count_rows`、`_predicate_mask` 这些工程路径走的就是这条），列绑定发生在
leftover 检查之前，`SELECT` 先被拿去查列名，直接报"没有名为 'SELECT' 的列"。
`CASE WHEN ... THEN ... ELSE ... END` 同理——`CASE` 不在关键字表里，
不带 schema 时它当标识符、报"表达式解析完了但还剩 'WHEN' 没处理"，
带 schema 时先撞上列绑定，报"没有名为 'CASE' 的列"。只有 `count(*)`
两条路径一致，都是"未知函数 'count'"——它在列绑定之前就被函数名分派挡下了。
这些都是刻意的范围声明：一台向量库的过滤层要回答的是"这一行留不留"，
不是"怎么把行算成一个报表"。

### `_distance` / `_score` / `_rowid` 从哪来

`_distance` / `_score` / `_rowid` 这三列（再加上混合检索的 `_relevance_score`）
**不是表达式引擎里的列**，所以拿它们当过滤目标会
在**解析阶段**就失败：`WHERE _distance < 0.5` 抛
`PlanError: 没有名为 '_distance' 的列`。它们是查询层在**结果整形**阶段挂上去的：
`Query._assemble`（`mini_lancedb/query.py`）在回表之后把 `_distance`（`float32`，
越小越好）、`_score`（`float64`，越大越好）作为普通列塞进输出 schema。
能对 `_distance` 做筛选的只有 `VectorQuery.distance_range(lo, hi)`，那是查询层
自己实现的，不走 `evaluate`。

`_rowid` 更值得说一句：`Scanner.with_row_id()` / `Query.with_row_id()` 挂出来的
`_rowid`，值就是**当前的逻辑行号**（搜索路径里直接写的是候选行号本身）。
第 9 章 §9.4 讲过"索引存的是建索引那一刻的逻辑行号"——`_rowid` 是同一个东西：
**更新一行之后它会变**（第 9 章 §9.10 有实测）。它不是 stable row id，
后者在第 7 章 §7.8 才有。

## 11.11 不写字符串行不行：程序化构造表达式

前面十章讲的都是"给一段 SQL 字符串，引擎把它解析成 AST"。
但 AST 既然是结构，就**可以不经过字符串直接造出来**——这条路在
0.40 里是一等公民，而且越来越重要。这一节把它补上。

### 两套入口，同一棵树

真实 LanceDB 的入口是 `lancedb.expr` 里的 `col()` / `lit()` / `func()`，
配合 `Expr` 上重载的运算符。本教程的迷你实现有对应的
`mini_lancedb.expr.nodes.col()` / `lit()`，形状几乎一样：

```python
>>> from mini_lancedb.expr.nodes import col, lit
>>> (col("id") > lit(1)).to_sql()
'id > 1'
>>> (col("id") * lit(2) + lit(1)).to_sql()
'id * 2 + 1'
>>> ((col("id") > lit(1)) & (col("n") < lit(5))).to_sql()
'id > 1 AND n < 5'
>>> (~(col("id") == lit(1))).to_sql()
'NOT id = 1'
```

真实库那边的 `repr()` 长得更漂亮一点
（`Expr(((id > 1) AND (lower(txt) = 'alice')))`），带完整的括号和单引号，
而本实现直接给 `to_sql()`。**两者产出的是同一类东西**：
一棵带类型标注的 AST，可以直接喂给引擎。

注意 `&` / `|` / `~` 这三个运算符。Python 的 `and` / `or` / `not`
**不行**——它们是短路求值、返回操作数本身，没法重载成构造 AST。
所以表达式里的布尔连接必须写位运算符，这在两种实现里都一样，
是这套 API 最容易被写错的一处。

### 它到底解决了什么

"不就是省了引号吗"——不止。程序化构造带来三件字符串做不到的事。

**第一，值不需要转义，也就不存在注入。**
`where(f"name = '{user_input}'")` 是教科书级别的注入漏洞：
用户输入里一个单引号就能改变整条谓词的语义。写成
`col("name") == lit(user_input)` 之后，值走的是参数通道，
**它永远是一个值，不会被当成语法**。这条在真实 LanceDB 里同样成立，
而且是推荐这套 API 的主要理由之一。

**第二，错误提前到构造期。**字符串要等到解析才知道对不对；
程序化构造在 Python 这一层就可能先撞上类型检查。实测本实现的
动态类型推断：`(col("id") > lit(1)).type` 直接是 `bool`，
`col()` 拿到的类型来自 schema，不需要等运行时。

**第三，可以被程序拼装。**"给用户勾选的几个过滤条件动态拼一个 `AND`"
这种需求，用字符串要小心翼翼地处理"是不是第一个条件"（否则会多一个
`AND`）；用 AST 就是 `functools.reduce(operator.and_, conds)`，
结构上不可能拼错。

### 真实库那边多出来的那一块

先说清楚一件事：**`where()` 收 `Expr` 这件事两个实现都支持**，
不是差距。实测本实现的 `t.search().where(col("id") > lit(1)).to_list()`
返回正确的一行，不需要先 `to_sql()`。

真正的差距在**投影**这一侧。

**真实 LanceDB 的 `Expr` 能当投影用**：

```python
# 真实 lancedb 0.40.0
>>> tbl.search().select({"double": col("id") * lit(2)}).to_arrow().to_pylist()
[{'double': 2}, {'double': 4}, {'double': 6}]
```

也就是**计算列可以直接写在 `select` 里**，引擎算完再返回。

**本实现不行。**它的 `select` 只接受一个**列名列表**，
传字典会被当成"要找一个叫 `double` 的列"：

```
select({"double": col("id") * lit(2)})
# PlanError: 列不存在: 'double' (available=['id', 'v'])
```

所以在本实现里想算 `id * 2` 有两条替代路：要么取出来在 Python 里算
（丢了"在引擎里算"的向量化收益），要么用附录 F 讲的
`add_columns` 把它落成真列（要写一次盘）。

这个差距的成因值得点一句：**`select` 的实现在本实现里停在"列投影"
这一层**，而真实库的 `select` 已经是一个"输出表达式"接口。
两者在第 19 章讲的投影下推里是同一件事的不同终点——
本实现推的是"读哪些列"（§19.3），真实库还能推"算什么表达式"。

### 和字符串那条路的关系

一个自然的疑问：既然 AST 更好，为什么还需要 SQL 字符串？

因为**字符串是"人写的"形态，AST 是"程序生成的"形态**，
两者的使用场景不同。写给人看的文档、日志、调试输出、以及
`explain()` 里打印出来的计划，都是字符串形态；而应用代码里
动态拼装的查询、以及任何带用户输入的值，都该走 AST。
**最危险的是两者混用**——把用户输入拼进字符串再交给解析器，
就等于把前面那三个好处全部退回。

本实现的 `to_sql()` 让这两条路可以互相转换，也顺带说明了
它们的关系：**AST 是本体，字符串只是它的一种序列化。**

## 11.12 小结

**一个向量数据库必须自己写表达式引擎**，因为过滤条件有四个消费者
（索引改写、统计剪枝、类型推导、选择性估计），它们要的不是一段文本，
而是一棵可操作的结构。

**四层架构是单向的：** AST → 解析器 → 改写/下推 → 向量化求值。
单向是硬要求，否则同一棵树在不同批次上会变成不同形状。

**解析器用递归下降**，因为 SQL 的表达式语法不是纯运算符语法；
每个 token 带 `pos`，报错能指到具体字符。位置信息齐全，
但默认的 `str()` 没有把它渲染成文档里承诺的两行形态——
**"信息齐全"和"呈现正确"要分开检查**。

**三值逻辑的四条易错点**：`NOT (a = 1)` 仍然排除 NULL 行；
`x NOT IN (1, NULL)` 永远不为真（它是 `x<>1 AND x<>NULL` 的自动结果，
不该特判）；`NULL AND FALSE` 是 FALSE 而 `NULL AND TRUE` 是 NULL
（这个不对称决定了短路能不能做）；除零是 NULL 不是异常。

**NaN 是真实的语义分歧。** 本实现把 NaN 当未知（任何比较都是 NULL），
真实 LanceDB 走 Arrow 的全序（`NaN = NaN` 为真）。
两者自洽但不能混，而分歧的落点是向量列：NaN 行躲得过 `IS NOT NULL`。
11.5 节两侧的证据强度不同——**本实现那一侧是脚本实测，真实那一侧是引用值**。

**类型与时间在这台引擎里是"整数 + 一份契约"。** 时间列就是整数列
（除 `date32` 是 int32，其余都是 int64），没有日期字面量、没有字符串与时间的互转；
算术的结果类型由三条规则定：两个整数取左操作数那一侧、有一边浮点就取浮点那一侧、
逻辑类型（`type`）不等于实际计算精度（`values.dtype`）。

**边界要说在明处。** 没有 collation（只有 `ILIKE` 这一点点规范化）、
没有绑定参数、没有跨查询的计划缓存、没有 `ORDER BY`、没有子查询和聚合；
`_distance` / `_score` / `_rowid` 是查询层在结果整形阶段挂的列，
不是表达式引擎里的列。这些不是缺陷清单，是范围声明——
说清楚"不做什么"，比让读者自己撞上去再猜要好。

**向量化是一份逐算子的清单，不是一句形容词。**
数值谓词快约 1.9 倍，字符串谓词慢约 14 倍；
`LIKE 'item-1%'` 比 `starts_with(s,'item-1')` 快 3~5 倍——
**快速路径写在哪一层，比"用没用 numpy"重要得多。**

**常量折叠必须复用同一套求值语义。** 本实现把常量子树丢进
一个 1 行的上下文里跑真正的向量化求值器，而不是另写一套标量求值——
这是"折叠前后行为不一致"这类 bug 的唯一根治法。

**`to_index_expr` 的核心契约是"答不了就返回 None"。**
`AND` 可以返回超集（下游过滤补齐仍然精确），
`OR` 必须全有全无（漏一支就丢数据），`col = NULL` 一概不翻译。
`LIKE` 的 `eq` / `prefix` 两个快速路径是"能不能吃上索引"的分水岭。

**选择性估计要"粗但可解释"。** 缺统计量时宁可回退到默认值，
也不给 0 或 1 这种会误导扫描器的极端值；有 `[min, max]` 时
"常量落在值域外 → 0"是最有价值的一类答案（实测 0.3 → 0.000000）。

到此为止，写入侧（第 7~9 章）和读取侧的过滤（第 10~11 章）都讲完了。
下一章转向另一个完全不同的查询形态：向量检索，
以及"先过滤还是先搜"这个决定了十倍延迟差距的选择。
