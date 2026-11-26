# 第 37 章 Mini 实现（六）：PromQL 词法、语法与类型检查

## 37.1 词法分析

`promql/lexer.py` 把文本转成 token：

```python
class TokenKind(str, Enum):
    EOF, IDENT, NUMBER, DURATION, STRING,
    LPAREN, RPAREN, LBRACE, RBRACE, LBRACKET, RBRACKET,
    COMMA, COLON, AT, OPERATOR
```

特殊处理：

- `1h30m` → DURATION（值为毫秒）；
- `5m3` → 报错（duration 后还有数字）；
- `1e3` → NUMBER；
- `0x10` → NUMBER（hex）；
- `Inf`/`NaN` → NUMBER；
- 字符串支持 `"..."`/`'...'`/`` `...` ``，Go 风格转义；
- 注释 `#` 到行尾；
- 运算符最长匹配（`=~`、`!~`、`==`、`!=`、`<=`、`>=`）。

duration 与 number 的歧义：

```text
5m     → DURATION
5      → NUMBER
5 * 60 → 两个 NUMBER + OPERATOR
```

因为 PromQL 不允许数字和标识符相邻（没有隐式乘法），所以"数字+单位"可以无条件识别为 duration。

## 37.2 语法分析

`promql/parser.py` 是递归下降：

```python
def parse_expr(min_precedence):
    lhs = parse_unary()
    while op and precedence >= min_precedence:
        rhs = parse_expr(precedence+1)  # ^ 右结合
        lhs = build_binary(...)
    return lhs
```

优先级：

```text
1  or
2  and, unless
3  == != <= < >= >
4  + -
5  * / % atan2
6  ^
```

一元 `+/-` 在 `parse_unary` 中处理，绑定强度低于 `^`：

```text
-2^2 = -(2^2) = -4
```

## 37.3 选择器

```python
def parse_selector(name, position):
    expect("{")
    while ...:
        label = IDENT
        op = = != =~ !~
        value = STRING
    return _new_selector(name, matchers, position)
```

`_new_selector()`：

- 如果没有 `__name__` matcher，补一个；
- 检查至少一个 matcher 不匹配空串；
- 冲突指标名报错。

支持：

```promql
foo
foo{job="api"}
{job="api"}
{"http.requests.total",job="api"}
```

## 37.4 后置修饰符

`parse_postfix()` 循环处理：

```text
[5m]         → MatrixSelector（仅 VectorSelector）
[1h:5m]      → SubqueryExpr（仅 instant vector）
offset 5m    → selector/subquery 的 offset
@ 1609746000 → selector/subquery 的 at
```

顺序可以交错，但 `@`/`offset` 只能作用于选择器/子查询。`(a+b) offset 5m` 报错。

## 37.5 聚合

```python
sum by (job) (rate(x[5m]))
sum(rate(x[5m])) by (job)
topk(3, x)
count_values("code", x)
```

`parse_aggregation()`：

- 可选的 `by/without` 前缀；
- `(` 参数 `)`；
- 可选的后缀 `by/without`；
- 参数类型检查：
  - `topk/bottomk/quantile/limitk/limit_ratio` 第一个参数是 scalar；
  - `count_values` 第一个参数是 string；
  - 其余聚合只接受一个 instant vector。

聚合表达式结果永远是 instant vector。

## 37.6 函数调用

`FUNCTIONS` 注册表：

```python
FunctionSpec(name, arg_types, return_type, min_args, max_args, handler, variadic)
```

解析时：

- 未知函数报错；
- 参数数量检查；
- 参数类型检查（vector/matrix/scalar/string）；
- 返回类型写入 AST。

例子：

```python
@_register("rate", (RANGE,), INSTANT)
def _rate(args, ctx): ...
```

`rate(foo)` 会在解析期报"argument must be matrix"，而不是运行期。

## 37.7 AST

`promql/ast.py`：

```text
NumberLiteral, StringLiteral
VectorSelector, MatrixSelector, SubqueryExpr
ParenExpr, UnaryExpr, BinaryExpr
AggregateExpr, Call
AtModifier, VectorMatching
```

每个节点有 `value_type` 和 `position`（错误定位）。

`walk()` 用于遍历；`selector_matchers()` 用于 `absent()` 合成标签。

## 37.8 类型系统

类型检查发生在：

- 二元运算：matrix/string 不允许；
- 比较 scalar-scalar 必须 bool；
- set 运算必须 vector-vector；
- group 修饰必须 vector-vector；
- 函数参数；
- 聚合参数；
- 子查询内部必须是 instant vector。

严格类型检查让很多错误在查询提交时就返回 400，而不是执行到一半 500。

## 37.9 错误信息

`ParseError(message, position)`：

```text
expected '}', got ']' at char 17
unknown function 'ratee' at char 0
vector selector must contain at least one non-empty matcher
comparisons between scalars must use the bool modifier
```

位置是字符偏移，便于定位。

## 37.10 测试

`tests/test_promql_parser.py` 覆盖：

- 字面量/字符串/原始字符串；
- 选择器/标签/引号指标名；
- 空选择器/全匹配拒绝；
- range/subquery/offset/@；
- 聚合前后缀/参数类型；
- 优先级/结合性；
- bool/on/ignoring/group；
- 非法组合（matrix 二元、unary matrix、offset binary）；
- 未知函数/参数数量；
- 注释/尾随 token。

## 37.11 与真实 PromQL 的差异

- Mini 不支持 `info()`、native histogram 函数、部分实验函数；
- 字符串/数字字面量支持子集；
- 错误消息格式不同；
- 没有 `@ start()/end()` 之外的 `@` 语法扩展。

解析器结构（lexer → Pratt/递归下降 → AST → 类型检查）与真实实现一致。

## 37.12 UTF-8 选择器与分组标签

词法层不需要为 UTF-8 增加新 token：引号字符串已经是 `STRING`，难点在语法层的歧义。Mini 的规则是：

- 花括号内第一个 `STRING`，如果后面不是匹配运算符，就是指标名；
- 如果后面是 `=`/`!=`/`=~`/`!~`，就是标签名；
- 其他位置的 `STRING` 一律视为标签名；
- 分组 `by (...)`/`without (...)`、`on (...)`/`group_left (...)` 都接受 `IDENT` 或 `STRING`。

解析器核心：

```python
if token.kind == TokenKind.STRING and self.peek(1).kind == TokenKind.OPERATOR:
    label_name = str(self.next().value)          # "service.name"="api"
elif token.kind == TokenKind.STRING:
    quoted_name = str(self.next().value)         # {"http.server.duration", ...}
elif token.kind == TokenKind.IDENT:
    label_name = self.next().text                # job="api"
```

这样以下查询都能解析：

```promql
{"http.server.duration", "service.name"="checkout"}
sum by ("service.name") ({"http.server.duration_count"})
{"http.server.duration_count"} * on ("service.name") group_left ("deployment.environment") {"service.target_info"}
```

错误处理：

- 空引号标签名 `{""="x"}` → `ParseError`；
- 指标名重复（`{"a.b", __name__="a.b"}`）→ `ParseError`；
- 引号指标名后面必须是 `,` 或 `}`，不能直接跟标签；
- 非空 matcher 约束保持不变：`{"a.b"}` 自带 `__name__` 等值匹配，合法；`{__name__=~".*"}` 仍然非法。

测试在 `tests/test_utf8_names.py`：选择器、只含引号标签名、分组、`on`/`group_left` 引号标签、重复名、空名、端到端 `sum by ("service.name")`。转义协商与迁移清单见附录 AF，词法/语法/类型检查的其他规则不受影响。

## 本章小结

- 词法区分 NUMBER/DURATION/STRING，支持 Go 风格转义。
- 递归下降 + 优先级表实现二元运算，`^` 右结合。
- 选择器/后置修饰符/聚合/函数调用分别解析，类型在解析期检查。
- AST 节点携带类型与位置，便于求值和报错。
- Mini 是 PromQL 子集，但结构/语义对齐。

## 延伸阅读

- 本教程第 6～8 章（PromQL 语义）与第 38 章（求值）。
- `mini_prometheus/promql/lexer.py`、`parser.py`、`ast.py`。
- 真实 Prometheus `promql/parser/`。
