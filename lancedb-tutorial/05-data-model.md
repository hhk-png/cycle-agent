# 第 5 章　数据模型与 Schema

上一章讲的是列怎么被编码成字节。这一章往上走一层：
这些字节在 LanceDB 眼里"是什么类型"，表结构怎么表达，以及结构变了会怎样。

如果只记一句话，就记这句：**LanceDB 的 schema 就是 Arrow schema，没有第二套类型系统。**
向量列不是一种新类型，它是 `fixed_size_list<float32>[D]` —— 一个普通的嵌套类型。
这句话听起来平淡，但它解释了后面几乎所有的便利和所有的限制。

## 5.1 为什么类型系统可以"借用" Arrow

真实 Lance 有一条容易被忽略的设计原则：**容器本身没有类型系统。**
`mini_lancedb/format/lance_file.py` 的文件头把它列成了第三条：

```
**3. 容器没有类型系统。**
列元数据只记录"这一列有几个值、每页怎么编码"，**类型信息在 dataset
的 manifest 里**。于是同一个文件可以被不同 schema 解释 ——
这也是"改列类型不用重写数据"能成立的原因。
```

这条原则有代价也有回报。代价是读一个文件时你光看文件本身不知道它在存什么，
必须配合 manifest 里的 schema；回报是格式变得极其简单 ——
容器只管"值 + 编码"，类型语义交给上层，于是加类型、改语义都不需要动文件格式。

**但迷你实现并没有完全守住这条原则，这点得按代码说。** `_write_column()`
写列元数据时把类型一起写了进去（`entry` 里的 `"type": type.to_json()`），
`LanceFileReader.schema()` 又从 `c["type"]` 把它读回来：

```python
Field(c["name"], DataType.from_json(c["type"]),
      nullable=int(c.get("null_count") or 0) > 0, field_id=c["field_id"])
```

也就是说，**在本实现里列元数据是存了类型的**，单个文件是自描述的 ——
打开一个文件不必先拿到 manifest 就知道每列是什么类型。真实 Lance 的做法是
类型语义的权威在 manifest、文件本身不承担这件事；文件头注释复述了原则，
代码却没有完全照做，这是一处需要点明的偏离。它对行为有直接影响：
读取时数组的类型来自文件，而 `RecordBatch` 的 schema 来自 manifest，
两者一旦不一致就留下了缝（第 5.2.2 节会用到这一点）。

Lance 官方规范里的说法是：逻辑类型是**映射到 Arrow 类型的字符串**
（`null`、`bool`、`int8`…`uint64`、`halffloat`/`float`/`double`、
`string`/`binary`/`large_string`/`large_binary`、`decimal:128:P:S`、
`date32:day`、`timestamp:<unit>:<timezone>`、`fixed_size_list:<元素类型>:<大小>`、
`dict:<值类型>:<键类型>:<ordered>`、`map`、`blob`、`json`）。
规范文档自己列的对照表就写着 "map to Apache Arrow data types"。

所以"LanceDB 的 schema 就是 Arrow schema"不是比喻，是字面事实：
如果你手里已经有一个 `pyarrow.Schema`，那它就是一个 LanceDB 的 schema。

`mini_lancedb/types.py` 用一个结构完整的方式复刻了这套映射。每个 `DataType`
要提供三样东西，正好对应它要穿过三个边界：

```
* 每一种类型都知道自己的 **numpy 物理表示**（``numpy_dtype``）；
* 每一种类型都能和 Arrow 双向转换（装了 pyarrow 才有用，见 ``to_arrow``）；
* 每一种类型都能序列化成 JSON（写进 manifest 用）。
```

`numpy_dtype` 是内存里的样子，`to_arrow` 是交给生态的样子，`to_json` 是落盘的样子。
三者的边界划得清不清楚，决定了这个类型系统能不能撑住 schema 演进。

**时间、时区与十进制：能描述、能落盘，但不做语义换算。**
上面那串映射字符串里提到的 `date32`、`timestamp`、`decimal` 等，`types.py`
各写了一个类（`Date32`、`Date64`、`Time64`、`Timestamp`、`Duration`、
`Decimal128`），每个都有 numpy 物理表示、Arrow 映射和 JSON 序列化。
但要在读写路径上真正用起来，只有一部分能走通：

**物理表示全是定宽整数。** `date32` 是 int32（自 epoch 起的天数），
`date64` 是 int64（毫秒），`time64` / `timestamp` / `duration` / `decimal128`
都是 int64。于是 `arrays.py` 把它们统统当 `PrimitiveArray` 处理，
落在普通整数列的编码路径上，没有任何专门的时间编码。

**类型字符串的解析面比类型本身窄。** `parse_type` 只认
`timestamp[us]` / `timestamp[ns, tz=UTC]` 和 `decimal128(P, S)` 两种写法
（`timestamp` 的 `unit` 是 `s|ms|us|ns`）。`date32` / `date64` / `time64` /
`duration` 都没有解析分支，拿不到就只能走 `DataType.from_arrow()`、
推断路径（Python 的 `date` 推断成 `date32`、`datetime` 推断成
`timestamp[us, tz=UTC]`），或者直接构造类。顺带一提，这里用的是
`decimal128(P, S)` / `timestamp[unit, tz=…]` 这套写法，和 Lance 规范里
`decimal:128:P:S` / `timestamp:<unit>:<timezone>` 的记法并不一样 ——
规范那套是给人读的映射说明，本实现这套是 `parse_type` 真正吃的语法。

**写入路径只接受整数。** `Date32.coerce_scalar` 这些"把 `datetime.date`
换算成天数"的辅助函数虽然写了，但 `array_from_pylist` 并不调用
`coerce_scalar`，它直接走 `PrimitiveArray.from_pylist` 的整数检查。
于是往 `date32` 列里写 `datetime.date(2026, 10, 17)` 会报
`TypeMismatchError`，写 `19000`（天数）才行；`timestamp` 同理，要写 epoch 计数。
`decimal128` 更明显：`numpy_dtype` 是 int64，写 `"1.23"` 报错、
写 `1.23` 会被截成 `1` —— **没有做 scale 换算**，`S` 只是记在类型名里。

一句话概括：**时间与十进制类型在本实现里是"能描述、能落盘、能读回，
但不做语义换算"的整数列**，语义换算被留在 `coerce_scalar` 里而没有接到
写入路径上。真实 Lance 把它们编成定宽整数、用 Arrow 的时间类型表达，
换算交给 Arrow 侧处理。

**和 pyarrow 的互操作：schema 层直通，数据层是拷贝。**
`Schema.to_arrow()` / `from_arrow()`、`Field.to_arrow()` / `from_arrow()`、
`DataType.to_arrow()` / `from_arrow()` 都是双向的：
`create_table(schema=...)` 直接接受一个 `pyarrow.Schema`
（`connection.py` 判断到带 `to_arrow` 的对象就转），`Schema.from_arrow`
也能把 Arrow 的 `field_id` 元数据读回来，`RecordBatch.from_arrow(pa.Table)`
和 `RecordBatch.to_arrow()` 同样都有。但要说清代价：**数据层的导出是走
`to_pylist()` 再 `pa.array(...)` 重建的，是一次完整拷贝**（`RecordBatch.from_arrow`
的注释也写着"慢但正确"）。本实现没有实现 Arrow 的 C data interface
（`__arrow_c_array__` / `__arrow_c_stream__`），所以拿不到"零拷贝导出到
pyarrow / Polars / DuckDB"这条能力；真实 Lance 是 Rust 侧直接借用 Arrow
buffer 的，一个字节都不搬。

## 5.2 向量列不是一种特殊类型

这是本章最重要的一节。

真实 LanceDB 里，一个 768 维 float32 向量列的类型写作
`fixed_size_list<float32>[768]`。注意这里没有任何 "vector" 字样 ——
`FixedSizeListArray` 就是"每行恰好 N 个子元素"的列，和 `list` 的唯一区别是长度固定。

我们实测一下这个类型在系统里长什么样：

```
=== 向量列判定 ===
  fixed_size_list<float32>[768]      is_vector=True  dim=768
  fixed_size_list<float16>[128]      is_vector=True  dim=128
  fixed_size_list<float64>[1536]     is_vector=True  dim=1536
  list<float32>                      is_vector=True  dim=None
  fixed_size_list<int32>[128]        is_vector=False dim=None
  binary                             is_vector=False dim=None
  utf8                               is_vector=False dim=None
```

判定逻辑只有一条：**外层是定长列表，内层是浮点**（`is_vector_type()`）。
`list<float32>`（变长）也算向量，只是没有静态维度。注意 `fixed_size_list<int32>[128]`
**不算** —— 我们的判定要求内层是浮点。

`fixed_size_list` 的物理布局是：**子列长度恒等于 `行数 × list_size`**。
实测：

```
=== 各嵌套类型的内存布局 ===
  fixed_size_list<float32>[4]    class=FixedSizeListArray     nbytes=   32 len=2
     子列 float32 长度 = 8 = 行数 2 × 4
     to_matrix() 形状 = (2, 4)
```

2 行 × 4 维 = 8 个 float32 = 32 字节，不多一个字节的 offsets。第 `i` 行的向量就是
`values[i*D : (i+1)*D]` —— 一个 view，不需要拷贝。这就是为什么
"向量检索"可以退化成一次 `reshape(n, D)` 然后走 BLAS。

### 5.2.1 后果一：向量能参与投影、切片、join

因为向量列在物理上就是一个两层的普通列，它没有特殊待遇，也没有特殊限制。
投影当然可以：

```python
ds.to_pylist(columns=["id", "vector"], filter="bucket = 3")   # 只读这两列
```

切片也是零拷贝的。实测：

```
  take([2,0]) 两个探测命中： [[9.0, 10.0, 11.0, 12.0], [1.0, 2.0, 3.0, 4.0]]
  slice(1,2)： [[5.0, 6.0, 7.0, 8.0], [9.0, 10.0, 11.0, 12.0]] （numpy 侧是 view）
  vector_matrix 形状： (3, 4)
```

`take` 是 ANN 召回之后"回表"的形态：索引返回行号，`take` 把那些行取出来。
如果向量是一种"特殊类型"，这一步就得有专门的代码路径；
因为它不是，所以召回和回表可以复用同一套列式原语。

真实 Lance 更进一层：文件格式在**结构编码阶段就把 FixedSizeList 展平**
（flatten）成扁平的 primitive 子列 —— 展平后的物理布局（父列不存值、
子列长度 = 行数 × D）第 3.5 节已经摊开讲过，这里只取它对**类型语义**的
结论：FSL 的"定长"完全是结构编码的问题，Lance 规范的精神是"不希望压缩库
关心 FSL 细节"，所以压缩技术可以假装 FSL 类型不存在。于是向量列在压缩器
眼里就是一个普通的 float 数组，第 4 章讲的那些 byte-stream-split、FOR、
位打包直接就能用 —— "向量是一种嵌套类型"这件事没有给编码层增加任何负担。

### 5.2.2 后果二：维度写死在类型里，改维度必须走 schema 演进

`fixed_size_list<float32>[768]` 里的 `768` 是**类型的一部分**，
和 `decimal` 的 precision 一样。这意味着 768 维和 1536 维**不是同一个类型**，
两者之间不存在隐式转换。

这一点在官方文档里有一句很直白的表述：`alter_columns` 可以在
"兼容的类型"之间做 cast，但**改变 `FixedSizeList` 的维度（比如 384 → 1024）
不是一次兼容的 cast**。

所以换 embedding 模型（768 维 → 1536 维）在 LanceDB 里是一件要动结构的事。
官方给的做法是三步：加一列新类型、删掉旧列、把新列改名回原名。
这三步里只有第一步要写数据。

**我们的写入路径和 `alter_columns` 都守住了这条边界**：前者在构造数组时
就比对维度，后者遇到维度变更直接报错（第 5.5.1 节给例子）。
不过底层仍留着一条能造出"schema 与数据不一致"批次的缝，值得看一眼。实测：

```
schema 声明: fixed_size_list<float32>[8]
实际数组类型: fixed_size_list<float64>[4]  行数: 3
batch.schema: Schema(vec: fixed_size_list<float32>[8])
schema 与数组类型一致吗: False
validate() 报错: TypeMismatchError 列 vec 的数组类型 fixed_size_list<float64>[4] 与 schema 的 fixed_size_list<float32>[8] 不符
读向量矩阵: [{'id': 0, 'vec': [0.0, 0.0, 0.0, 0.0]}, ...]
```

这个批次不是从表里读出来的，而是**手工构造**的：`RecordBatch.__init__`
不做任何校验，你可以把一个 `fixed_size_list<float64>[4]` 的数组挂到声明为
`fixed_size_list<float32>[8]` 的 schema 下，它就成了这个样子。
`RecordBatch.validate()` 抓得住这个不一致，但正常读取路径并不主动调用
`validate()`，所以只要没人显式校验，它就会一路传到用户手上。这就是
"维度是类型的一部分"在实现层面的分量 —— 它必须由**写入路径在构造数组时**
守住，而不是靠事后校验。

顺便：往一个 4 维的列里写 8 维向量，写入路径是拦得住的：

```
   TypeMismatchError: 第 0 行的向量维度是 8，schema 声明的是 4
```

这个错误发生在**构造数组时**，也就是落盘之前。
`arrays.py` 的注释把它和 LanceDB 的 `on_bad_vectors` 参数联系起来：
写入路径是类型检查最严的地方，"这正是 `create_table` 时 `on_bad_vectors`
参数要处理的场景"。

`on_bad_vectors` 有四个取值（我们实现了和真实 LanceDB 相同的语义）：
`error`（默认，抛错）、`drop`（丢掉整行）、`fill`（用 `fill_value` 补齐）、
`null`（置为 null）。真实 LanceDB 文档对"坏向量"的定义是三条：
维度不对、含 NaN、在非空列上是 null。
NaN 之所以被单列出来，是因为"NaN 在距离比较里永远为 `False`，
不显式处理的话它会**随机地**出现在结果里或消失"。

### 5.2.3 后果三：内层类型可以换，但只有浮点那几种真能换

因为向量列就是"定长列表 + 某种内层类型"，换个内层类型就换了一种向量：

- `float16` —— 省一半空间，精度损失可控，适合大规模召回；
- `float64` —— 需要高精度的科学计算场景；
- 定长 `binary` / `uint8` —— 二进制向量（汉明距离）。

真实 LanceDB 的多模态部分明确写了向量值类型可以是 `float16` / `float32` / `float64`
（对应 `pa.list_(pa.list_(pa.float32(), 256))` 这种多层结构就是 multivector，
多向量列怎么建模、怎么建索引见第 21 章）。
二进制向量则要求维度是 8 的倍数，一个 128 维的二进制向量存成
16 个 `uint8`，并且只在 `IVF_FLAT` 索引上加 `hamming` 距离。

我们的 `is_vector_type()` 只认**浮点**内层：`float16` / `float32` / `float64`
都算（上面实测里 `fixed_size_list<float16>[128]` 和 `float64` 都是 `True`），
而 `fixed_size_list<int32>[128]` 和 `binary` 不算。源码注释把
"真实 LanceDB 还允许 `float16`/`float64`"也列成了差距，这两个其实本实现
是认的；真正的差距只剩**固定长度的 `binary`**（二进制向量）——
**它在实现里未经确认能跑通**。

## 5.3 嵌套类型总览

除了定长列表，Lance 还支持这几种嵌套/半结构化类型。它们的共同点是
"物理布局是事先定好的、和逻辑语义严格对应的"，这是列存能高效随机访问的前提。

**先说清楚这一节的范围。** 下面实测的四种（`fixed_size_list` / `list` /
`struct` / `map`）加字典编码，是**本实现真正实现并落地验证过的**那一组。
§5.1 抄的那份官方逻辑类型清单里还有三类**不在这一节**，
按理由分成两拨：`large_string` / `large_binary` 是同一套布局换更宽的偏移量
（64 位对 32 位，超过 2 GB 的列才需要），差别是参数不是结构；
而 **`blob` 和 `json` 是真的一层新东西**——`blob` 是"大字段当一等公民"
（第 21.4 节整节在讲它，包括外部存储的 blob URI），
`json` 是半结构化列。**三种都不在这一章的"嵌套"范畴里**，
所以这一节叫"总览"指的是**结构化布局这一族**，不是"所有类型"。
把它们和下面这四种并列会误导读者以为它们是同一种东西。

把这几种放在一起实测，物理布局的差异一眼可见：

```
  fixed_size_list<float32>[4]    nbytes=   32 len=2
     子列 float32 长度 = 8 = 行数 2 × 4
  list<int64>                    nbytes=   48 len=2
     offsets = [0, 2, 3]  子列 n=3
  struct<a: int64, b: utf8>      nbytes=   42 len=2
     子列: {'a': 'int64(n=2)', 'b': 'utf8(n=2)'}
  map<utf8, int64>               nbytes=   86 len=2
     offsets = [0, 2, 3]  keys=['k1', 'k2', 'k3'] items=[1, 2, 3]
```

逐条读：

**`fixed_size_list`** 没有 offsets，子列长度 = 行数 × N。省掉了 offsets 数组，
而且第 `i` 行固定落在 `[i*N, (i+1)*N)`，随机访问是纯算术。

**`list`** 是一个 `offsets` 数组（长 `n+1`）加一个扁平子列。
上面 2 行 `[[1,2],[3]]` 的 offsets 是 `[0, 2, 3]`，子列是 `[1,2,3]`。
48 字节 = offsets 3×8 + 子列 3×8。变长的代价就是这个 offsets 数组。

**`struct`** 是若干等长的平列。2 行的 struct 就是"两个长度 2 的子列"，
按名字寻址。42 字节 = `a` 两个 int64（16） + `b` 两个 UTF-8（"x"/"y" 各 1 字节，
offsets 3×8=24 + 2 字节；数字大体对得上，差别在 offsets 的存在与否）。

**`map`** 在物理上**就是 `list<struct<key, value>>`**，
所以布局和 `list` 一样是"offsets + 两个平列"。
上面那个 2 行、`offsets = [0, 2, 3]` 的例子，86 字节拆开是：

```
外层 offsets   3 × 8  = 24
keys 子列      offsets 4 × 8 = 32，加 'k1'+'k2'+'k3' 共 6 字节  = 38
items 子列     3 × 8  = 24
                        ----
                          86
```

**这里我第一版写错过**：写成 `offsets(24) + keys(24+3) + items(24)`，
三项加起来只有 75，和打印出来的 86 差 11 字节。原因是把 keys 子列
按 `list<int64>` 的 3×8 来算了——可 keys 是**字符串**列，
它自己还要一层 `n+1` 长的 offsets（4 个，不是 3 个），
字符串本体只有 6 字节。三层结构套在一起时，"哪个子列带不带 offsets"
必须逐层数，凭印象算一定会错。

**`dictionary`** 这里需要小心，因为它涉及逻辑类型和物理编码的区分，
而且规范文档和我们的注释说法不完全一样。

Lance 规范的 logical type 表里**有** `dict:<value_type>:<key_type>:<ordered>`
这一项，映射到 Arrow 的 Dictionary 类型，并且注明"ordered 当前未完全支持"。
所以严格说，dictionary 在规范里**是**一个逻辑类型。

但另一方面，字典**编码**又确实是一种物理技术，Lance 规范把它归在编码层，
并且明确说"若当作普通压缩技术，会变成 opaque，限制其在 full zip 场景的可用性。
因此**在任何结构编码之前应用**"。

这两件事不矛盾，但需要分开讲：**规范承认 dictionary 逻辑类型的存在，
而低基数列的字典化在实际写入时是编码层自动做的选择，不需要用户声明。**
我们的实现把这个区分保留在注释里：

```
在 Lance 里这不是一种"用户声明"的类型，而是**物理编码**的一种选择
（低基数列自动走 dictionary encoding）。
```

实测它在内存里的样子（这是一次真正的两段式结构）：

```
  原列 nbytes = 65
  编码后 nbytes = 71  压缩比 = 0.44
  dictionary = ['red', 'blue']  indices = [0, 1, 0, 0, 1]
  decode() 回来 = ['red', 'blue', 'red', 'red', 'blue']
```

**那个 `0.44` 得解释一句，否则一定会被读错。** 它**不是** `原列 / 编码后` ——
那算出来是 `65/71 = 0.915`，比 1 小一点点。`DictionaryArray.compression_ratio`
的定义是 `字典本体字节 / 编码后总字节`，也就是 `31/71 = 0.4366 ≈ 0.44`：
它回答的是"这 71 字节里有多少是字典本身"，而不是"省了多少"。
（`mini_lancedb/arrays.py:914-921`，注释里明说了这个 helper 是"调试时挺有用"，
不是一个正式的压缩率指标。）

真正该看的对比是 **65 → 71：编码后反而大了 9%**。
5 个元素、2 个唯一值的规模下，字典化是纯亏的，逐项拆开是：

```
原列 65  = offsets 6×8 = 48  +  文本 3+4+3+3+4 = 17
                （5 个值 → 6 个 offsets）
编码后 71 = indices 5×8 = 40  +  字典 31
                （indices 是 int64，5 个值 = 40 字节）
字典 31   = offsets 3×8 = 24  +  文本 3+4 = 7
                （2 个唯一值 → 3 个 offsets）
```

**逐项相减，正好得到那多出来的 6 字节**——注意这里有一个容易被漏掉
的抵消项：原来那个 48 字节的 offsets 数组**被 40 字节的 indices 取代了**，
所以它是"换掉"而不是"白付"：

```
原列 offsets      48  →  indices 40      −8
原列文本          17  →  字典文本 7      −10
（新增）字典 offsets                +24
                                      ----
                                       +6
```

所以这 6 字节不是"某个字段变贵了"，而是**三个方向相反的变化加出来的**：
offsets 换 indices 省了 8 字节（int64 的 indices 比 n+1 个 int64 的
offsets 少一个元素），文本省了 10 字节，但字典自己那 24 字节的
offsets 是新加的。**只数"代价"不数"抵消项"，就会得出一个对不上的数**——
我第一版就是这么写的（写成"40 + 24 − 10 = 54 的净亏"），
而 65 + 54 显然不等于 71。凡是"这里比那里贵/便宜多少"的论断，
都要能用加减法闭合回原始数字。

**这里我连着写错过两次，值得记一下形状。** 第一版把 `map` 的三项
加起来只有 75（见上一节）；改这一节时又把 indices 按 5×4=20 算，
理由是想当然地以为"索引数组用 int32 就够"——而 `DictionaryArray`
在 `mini_lancedb/arrays.py` 里是 `np.asarray(indices, dtype=np.int64)`，
**它固定用 int64**。两次错误的共同形状是：
**算字节数时凭"这个类型应该用多少位"去估，而不是去看代码里写的
`dtype`。** 列存里类型宽度是最不能靠直觉的东西。

真实 Lance 的字典编码有一整套阈值来判断值不值得
（规范里记的默认是"字典条目少于 100 条就不值得"、
`dict-size-ratio` 0.8、`dict-divisor` 2），
小数据上开字典是典型的过度优化。

还有一个跨节的观察：**`map` 类型在不同地方的支持度不一样。**
Lance 格式 2.2 才把 map 加进来（规范里 2.2 的说明写着"含 map 支持"），
而我们的读取路径直接抛 `NotSupportedError("Map 列的读取路径尚未实现（写入已支持）")` ——
写入能跑、读回来不行。这是个诚实的现状，不是遗漏。

## 5.4 字段要有稳定 id

现在讲本章第二个重点：**为什么 Lance 的每一列都有一个永不变更的整数 id。**

先看如果**没有**会怎样。假设表里是 `[a, b, c]`，数据文件里的物理列顺序就是字段顺序。
执行 `drop_columns(["b"])`，新 schema 变成 `[a, c]`。
但老数据文件里还是三列 `[a, b, c]`，而且我们不打算重写它（那就是重写的代价）。
这时按位置读：老文件的第 3 列（`c`）会被当成新 schema 的第 2 列（`c`）——
**看起来刚好对**。但如果删的是第一列呢？`[a, b, c]` 删 `a` → `[b, c]`，
老文件第 1 列（`a`）被读成 `b`，第 2 列（`b`）被读成 `c`。**整表串位。**

Lance 的解法是给每个字段在**创建时**分配一个永不复用的整数 id，
数据文件按 id 而不是按位置定位列。

我们的 `schema.py` 文件头把这条写成了模块存在的理由：

```
Lance 的解法是：每个字段在**创建时**分配一个永不复用的整数 id，
数据文件按 id 而不是按位置来定位列，于是删列只是把 id 从 schema 里摘掉，
旧文件原样保留，读取时按 id 对齐即可。这也让"加列"变成纯元数据操作。
```

读取路径确实按 id 找文件：`read_fragment()` 先建 `{列名: field_id}` 的映射，
再对每一列问"这个片段里哪个数据文件装了它"：

```python
field_ids = {f.name: (f.field_id or 0) for f in self.schema.fields}
groups: Dict[str, List[str]] = {}
for name in cols:
    df = fragment.file_for_field(field_ids[name])
    if df is None:
        continue  # 这个片段里没有这一列 → 后面补 null
```

`DataFile.fields` 是一个 field_id 列表，`Fragment.file_for_field()` 按 id 查。
**找不到就说明这个片段里没这一列，补 null 即可** —— 这就是"免重写加列"的全部机制。

实测：删掉中间一列 `label`，别的列一个都没串：

```
=== drop_columns：删掉中间一列 label ===
 新版本 = 4
  id     int64    id=0   not null
  x      float64  id=2   not null
  score  float32  id=3
  note   utf8     id=4
 只剩 id/x，读出来标签没串位：
    {'id': 0, 'x': 0.0, 'score': None, 'note': None}
    {'id': 1, 'x': 1.0, 'score': None, 'note': None}
```

`label` 的 id=1 被摘掉了，`x` 仍然是 id=2。id 出现空洞（0,2,3,4）是正常的 ——
规范里把这条性质叫 **sparse（同版本内可以不连续）**。

### 5.4.1 真实 Lance 的 field_id 规则要更严格

我们的实现只覆盖了 id 机制的骨架。真实 Lance 的规则在规范里写得很细，
值得完整列一遍，因为每一条都是踩过坑才有的：

**分配顺序**：建表时按 **depth-first 顺序**从 0 开始，给**所有字段（含嵌套）**分配。
规范给的例子是顶层字段 `a:0, b:1, c:2`，`c` 的子字段 `c.x:3, c.y:4, c.z:5`，
然后才是 `d:6`。嵌套字段用 protobuf 的 `parent_id` 串起来，
**顶层字段的 `parent_id = -1`**。

**三条基本性质**：Preserved（字段存在期间 id 不变）、Unique（同版本内不重复）、
Sparse（同版本内可不连续）。激活不可复用标志之后再加两条：
Not reassigned（不复用）、Increasing（单调增）。

**作用域**：id 只在"**一个 dataset 的一个 branch**"内唯一，
跨 dataset、跨独立演进的分支**不唯一**。规范说如果两个分支各自加了列，
未来的 merge 必须**报错**，而不是去挑一个 id、改一个 id、或者按名字对齐。

**保留的负 id**：`-1` 表示"未分配"（只在内存里，永不落盘）、
`-2` 是 tombstone（重写某一列时用来标记"忽略这一列"）、
`-3`/`-4`/`-5` 是隐藏的 `_rowid` / `_row_created_at_version` /
`_row_last_updated_at_version` 行血脉列，而且"**一个 fragment 至多一个 file 带每个 id**"。

**这些隐藏列在本实现里的实际形态，得单独说清。** 上面那三条行血脉列
本实现一个都没落盘。它只定义了一个"概念列"：`schema.py` 里的
`ROW_ID_FIELD = Field("_rowid", ROW_ID_TYPE, nullable=False,
metadata={"virtual": "true"})`，而实现里没有任何地方用它。

用户真正能拿到的 `_rowid` 来自查询层：`Query.with_row_id(True)`（`Table.search(...)`
上同名的方法）会在结果里挂一列 `_rowid`，值是**逻辑行号**（0..count_rows-1），
不是 64 位地址。对应 Lance 那个 `(fragment_id << 32) | offset` 的是
`Dataset.row_address(logical_row)`（行号到地址的换算见第 7.8 节）。
所以本实现里"稳定行地址"和"暴露给用户的 `_rowid`"是**两样东西**：
前者稳定、要显式调用，后者可读、但压实或删除之后就会变 —— 这也是为什么
`row_address()` 的注释提醒不要把它持久化。

**`max_allocated_field_id` 与不可复用标志**：manifest 里有一个高水位字段。
如果它**没设**（老格式），写入者可以从"当前还被引用的字段"里挑下一个 id，
于是**可以复用已经 drop 掉的 id**。如果它**设了**，新 id **必须大于高水位**，
**不得复用**被 drop 或被替换掉的 id。这个字段和格式标志
`FLAG_NON_REUSABLE_FIELD_IDS`（bit `2^15`）必须**同时存在或同时不存在**，
而且**只能通过一次显式的迁移提交激活，激活后不可撤销**。

这条"不可复用"为什么值得单独立一个格式标志？因为复用 id 会在
"分支 + 时间旅行"的组合下产生歧义：老版本里 id=3 是 `name`，
新版本里 id=3 是 `score`，一个跨版本读的读者就没有办法用 id 判断
"我读到的这一列在我的版本里是什么"。关掉复用，id 就成了永久的身份。

### 5.4.2 我们的实现：没有高水位，也没有开关

`Schema._assign_field_ids()` 的做法是"取**当前还在的**字段里最大的 id 加一"：

```python
used = {f.field_id for f in self.fields if f.field_id is not None}
next_id = 0 if not used else max(used) + 1
```

`used` 只收集**当前 schema 还引用着的** id，所以"删一列再加回一列"
会不会复用旧 id，取决于被删的是不是当前最大的那个 id。实测两种情况：

```
  初始 id: {'a': 0, 'b': 1, 'c': 2}
  删 b 再加 b 之后: {'a': 0, 'c': 2, 'b': 3}
```

删中间列 `b`（id=1）时，剩下的最大值还是 2，重加的 `b` 拿到 id=3，id=1 成了空洞。
但删掉**id 最大**的 `c`（id=2）时，剩下的最大值掉回 1，重加的 `c` 拿到的
正是 **id=2 —— 被删掉的那个 id**。所以"删列再重加"**不是**一条无条件的
不复用规则：真正的规则是"新 id = 当前最大 id + 1"，被删的 id 只有在它不是
最大值时才变成空洞。

这跟真实 Lance 的差别是实打实的。真实 Lance 的 manifest 里有一个
`max_allocated_field_id` 高水位（见第 5.4.1 节）：**没设**它时（老格式），
写入者可以从"当前还被引用的字段"里挑下一个 id，于是**可以复用**已经被
drop 掉的 id；**设了**它时，新 id **必须大于高水位**、不得复用。
我们的实现只有"当前最大 id + 1"这一条，既没有高水位，也就无从表达
"这个 id 以前用过、永不发第二次"——它是在删掉最大 id 时**碰巧会复用**的。

代价是被删字段的 id 可能永远空洞（删中间列的情况），永远不回收。
这在小表上无所谓，在一个每天 add/drop 列的表上 id 会一路涨到 int32 上限
（规范明确要求"分配的 id 装不进 `int32` 时必须失败"）。**我们没实现这个检查。**

还有一处必须点明的差异：真实 Lance 里 **`alter_columns` 改类型会产生
"新 id 的替换字段"**（规范的原话是 cast 会"产生新 ID 的替换字段并退役旧身份"），
改名和改 nullable 则保留 id；本实现的 `alter_columns` 一律保留 id。
留到第 5.5.1 节一起说。

## 5.5 schema 演进的四种操作与它们的代价

真实 LanceDB 的 schema 演进有四类入口：加列、改列、改字段元数据、删列，
官方文档的说法是"通过细粒度操作支持 **ACID 兼容的 schema 演进**"。
代价分成两档，官方文档写得很清楚：
**改数据类型需要重写列数据，表大了会很贵；改名和改 nullable 只改元数据，便宜得多。**

我们实测跑一遍完整的演进流水（从 v2 到 v7）：

```
=== add_columns：加一列 ===
 新版本 = 3
  id     int64    id=0   not null
  ...
  score  float32  id=3
  note   utf8     id=4
 老数据文件路径变了吗: True      <- 没变，一个字节都没重写
 老字段 id 变了吗: True
 读出来（新列应为 null）：
    {'id': 0, 'label': 'row0', 'x': 0.0, 'score': None, 'note': None}
```

注意 `score`/`note` 在旧行上读出来是 `None`。这就是"加列是纯元数据操作"的正面和反面：
正面是不花钱，反面是**旧行在新列上一定是 null**。
规范对此的表述是"不要求 dataset schema 的每个 field id 都出现在某个 data file 里
—— 没有对应 data file 的列读作**全 NULL**"。

真实 LanceDB 的 `add_columns` 支持"带值加列"，但**只接受 SQL 表达式或 NULL**：

```
LanceDB add_columns does not currently accept Python callables, batch UDFs,
or PyArrow RecordBatch iterators for populating new columns.
```

也就是说 `table.add_columns({"discounted_price": "cast((price * 0.9) as float)"})`
是可以的（它会去重写那一列的值），但你不能传一个 Python 函数进去。
我们在这个位置做了一个折中：`Dataset.add_columns` 接一个可调用对象，
但**只是惰性地取它的返回类型**，不填值；真正要填值走 `Table.add_columns(computed=...)`，
它是"先加空列，再 `update(where='true')`"实现的，
代价是 O(行数) **而且会打乱行序**。真实 LanceDB 走的是 Lance 的
`add_columns` 物理算子，只重写一次、不产生删除文件 —— 这是我们的实现明显更差的地方。

### 5.5.1 alter_columns：唯一昂贵的演进

实测把 `score` 从 float32 改成 float64：

```
=== alter_columns：改类型 ===
 新版本 = 6  score 类型 = float64
 field_id 是否保持: 3
 读出来：
    {'id': 0, 'x': 0.0, 'score': None, 'note': None} -> NoneType
```

这里 field_id 保持为 3。我们 `alter_columns` 的注释承认了这个差异：

```
真实 Lance 的 ``alter_columns`` 会因为类型是否"二进制兼容"
（比如 int32 → int64 可以零拷贝解释，int32 → string 不行）
而决定是"只改元数据"还是"重写数据"。本实现一律重写，
并在文档里点明这个差异。
```

**本实现的选择和代码是一致的：类型一变就整表重写。** `alter_columns`
里有一个 `needs_rewrite` 开关，只要任何一个目标类型与现有类型不同就置位；
置位之后走的是"读出所有片段 → 逐列 `cast` 到新类型 → 重新写片段 →
在**同一次提交**里换掉片段列表"这条路，收尾还调用 `rewritten.validate()`
确认结果自洽。只改名或只改 nullable 时 `needs_rewrite` 不置位，走另一条
只动 manifest 的路径。所以不存在"改完类型、数据还是旧的"这种中间态 ——
要么正确重写，要么在 `cast` 那一步直接报错。

**维度变更就是会报错的那一类。** `fixed_size_list<…>[4]` 到
`fixed_size_list<…>[8]` 不是一次 `cast` 能表达的（长度变了是数据变换），
`Array.cast()` 会直接拒绝：`TypeMismatchError: 不能把
fixed_size_list<float64>[4] 直接转成 fixed_size_list<float32>[8]：
列表长度从 4 变成 8 是数据变换而不是类型转换`，并给出
"先取出数据、按新维度重建列，再写回"的提示。这正是第 5.2.2 节说
"改维度必须走 schema 演进"在实现层面的落点，也正是下一节那条
"加新列 + 删旧列 + 改名"配方要出现的原因。

还有一处用词要纠正：**Lance 的文档和规范里没有 "binary compatible" 这个说法**。
规范用的是 "compatible types" 和 "not a compatible cast"（兼容的类型 /
不是兼容的 cast）。"二进制兼容"是我们为了讲清"能不能零拷贝重解释"引入的，
**不是官方术语，标记为未经确认**。

顺带回到第 5.4.2 节那个 field_id 差异：真实 Lance 在 `alter_columns`
改类型时会**退役旧身份、发一个新 id**（规范的原话是 cast 会产生
"新 ID 的替换字段"），改名和改 nullable 则保留 id。本实现的 `alter_columns`
**一律保留 id**（`set_column()` 里显式写 `nf.field_id = f.field_id`），
连"改类型整表重写"这条路径也一样 —— 重写出来的新片段用的还是同一个 id。
这是一处真实的差异，不是缺陷：它意味着在本实现里"列的身份"比"列的数据"
活得久，id 可以跨越一次类型变更。

### 5.5.2 官方推荐的做法：加新列 + 删旧列 + 改名

维度变更那条路官方给的三步操作，其实是这个设计下最自然的产物：
既然改类型贵、加列便宜、删列免费、改名免费，
那就**永远不要改类型**，而是"造一个新的、扔掉的旧的、把新的改个名"。
代价是短时间内表里有两列向量（空间翻倍），
好处是中间任何一步失败都不会让表不可读。

顺带，`add_columns` + `drop_columns` 的组合也是"回填"的标准做法：
规范里把这条机制叫 **Data Evolution**：加新列时给每个 fragment **附加一个新数据文件**，
为 fragment 内所有既有行计算值，**不需要重写整表**。
官方博客的措辞是 "**Zero-copy** data evolution ... **without full table rewrites**.
Only new data is written; expensive existing data (like images/videos) remain untouched."

**第四节：改字段元数据。** 官方文档把 schema 演进列成加列、改列、改字段元数据、
删列四类，第四类走的是**一条独立的方法**，不混在 `alter_columns` 里：
真实 Lance 用 `LanceDataset.update_field_metadata(schema, metadata, replace=False)`
（LanceDB 侧也有对应的 `Table.update_field_metadata(column, metadata, replace=...)`），
作用就是给某一列挂一段任意 key/value —— `replace=False` 合并、`True` 替换。
它只动元数据，不动数据。

**我们的实现没有这个入口，四类操作只做了三类。** 字段级的元数据在
`Field.metadata` 里是有的：`field_id`、`lance-encoding:structural`、
`lance-schema:vector-column` 这些内部标记都存在那儿，`Schema.set_column()`
也会把旧字段的 metadata 原样带过去（`dict(old_field.metadata)`）。
但**没有任何方法能改它**。用户在表上能写元数据的地方只有一处 ——
`schema.metadata`（表级），embedding 配置就存在那里。
所以"给某一列加一段自定义元数据"这件事在本实现里做不到，
这是与真实 Lance 的一处能力缺口，不是被有意略去的细节。

## 5.6 类型推断与类型提升

最后一节讲"表在第一次见到数据时怎么选类型"，以及"两批数据类型不一致时谁赢"。

### 5.6.1 推断规则

实测 `infer_type` 的完整行为：

```
=== 单列类型推断 ===
  全是 None        -> null
  Python int     -> int64                              → 可建 BTREE / BITMAP 索引
  Python float   -> float64
  bool           -> bool                               → 可建 BTREE / BITMAP 索引
  str            -> utf8                               → 可建 FTS / BITMAP / NGRAM 索引
  bytes          -> binary
  同长 list        -> fixed_size_list<float64>[3]        → 向量列，维度 3
  不等长 list       -> list<int64>
  dict           -> struct<a: int64, b: utf8>
  混合 int/None    -> int64
```

两条最值得注意：

**`list` 长度全相同时会被推断成 `fixed_size_list`。** 这就是"自动识别向量列"的入口 ——
用户传的是 Python 列表的列表，实现自动把它认成向量。真实 LanceDB 也是这样：
一份原始 `pa.list_(pa.float32(), 2)` 在摄入时会被规范化为 `fixed_size_list`。
这条规则的实用含义是：**只要有一行长度不一样，整列就退化成 `list`，向量索引就建不了**。
所以列里混进行长度不同的数据，症状往往不是"报错"，而是"索引建不出来"。

**`bool` 不参与数值推断。** `promote(bool, int32)` 返回 `None`（下面会看到），
所以一列里混了 True 和 1，推断出来是 `bool`，后面提升会失败。
Arrow 里 bool 同样不算整数，这是有意的对齐。

**可空性：默认 `True`，推断时按数据定，但只在 `validate()` 里被强制。**
`Field("x", "int64")` 不传 `nullable` 就是可空；`infer_schema` / `infer_type`
推断时看"这一列出现过 `None` 没有"——出现过就是 `True`、一次都没有就是 `False`
（所以 `[{"a": 1, "b": None}]` 推出来是 `a` 不可空、`b` 可空）。
改可空性属于"纯元数据变更"，`alter_columns` 不重写数据；真实 LanceDB
也只支持"不可空 → 可空"这一个方向，反向会被拒绝（它无法证明现存数据里没有 null）。

但别把 `nullable=False` 当成一条强约束。本实现里唯一强制它的地方是
`RecordBatch.validate()`（非空列出现 null 就报 `SchemaError`），而
`Schema.compatible_with()` 里那段可空性检查只写了"安全方向"
（源不可空、目标可空 → 放行）就 `continue` 了，另一个方向落到函数末尾
照样返回 `True` —— 实测"目标不可空 × 源可空"同样放行。所以"往非空列塞 null"
这件事靠的是 `validate()` 兜底，不是 schema 校验。

上面那张推断表顺带给出了类型到索引的对应：整数和 `bool` 可建 BTREE / BITMAP，
字符串（`utf8`）可建 FTS / BITMAP / NGRAM。这三类标量索引的构造与查询在
第 18 章，全文检索（FTS）在第 17 章。

### 5.6.2 为什么 Python int 永远是 int64

这是本章最容易被问的一个问题：`[1, 2, 3]` 明明能装进 `int8`，
为什么推断成 `int64`？

我们的实现有意复刻了 pyarrow 的行为，注释把理由写清了：

```
刻意和 Arrow 对齐：pyarrow 从 Python int 推断出的永远是 ``int64``，
不会去"猜最小能装下的类型"。原因很实际 —— 表是长期存在的，
按当前数据选最小类型意味着**下一批数据就可能装不下**，
于是要重写整列。稳定的类型比省几字节重要。
```

这个理由值得展开。一个"按当前数据选最小类型"的系统，
在第一批数据是 `[1,2,3]` 时选 `int8`，第二批来了 `[1000]`，
就得把整列从 int8 提升到 int16 —— 一次 O(表大小) 的重写。
而 int64 对 int8 的浪费是每行 7 个字节，压缩之后（第 4 章讲过 FOR 和位打包）
实际差异小得多。**这是一个"稳定胜过紧凑"的典型取舍**：
把不确定性推给编码层（编码器可以逐页选位宽），而不是推给类型层。

实测这两层确实是分开的：

```
=== 整数推断永远是 int64 ===
  [1, 2]             -> int64
=== 但编码器用 narrowest_int_type 选位宽 ===
  [1, 2, 3]          -> uint8
  [0, 255]           -> uint8
  [-1, 100]          -> int8
  [1099511627776]    -> uint64
```

`narrowest_int_type()` 的注释把分工说得很清楚：
"写文件时不用它（那会让类型随数据漂移），但**编码器**会用它来决定'位宽'——
Lance 的 `bitpack` / `FOR` 编码就是这么选 bit width 的"。

所以正确的心理模型是：**schema 里的类型是长期契约，页里的位宽是短期优化。**
前者要稳定，后者要贴着数据。混起来就会做出错误的决策。

### 5.6.3 提升规则

`promote_types(a, b)` 求两个类型的公共超类型，不可兼容时返回 `None`。
实测（这组是抽样，下面每条规则都补齐了它没覆盖到的取值）：

```
=== 类型提升 promote_types ===
  promote(int8                            , int32                           ) = int32
  promote(int32                           , int64                           ) = int64
  promote(int32                           , float64                         ) = float64
  promote(float64                         , int32                           ) = float64
  promote(int32                           , utf8                            ) = None（不可提升）
  promote(utf8                            , utf8                            ) = utf8
  promote(bool                            , int32                           ) = None（不可提升）
  promote(fixed_size_list<float32>[128]   , fixed_size_list<float32>[128]   ) = fixed_size_list<float32>[128]
  promote(fixed_size_list<float32>[128]   , fixed_size_list<float32>[768]   ) = None（不可提升）
  promote(list<int32>                     , list<float64>                   ) = list<float64>
```

规则可以补全成四条：

**整数 × 整数 → 更宽的那个；有符号和无符号混时，一律升到"能装下无符号那个值域"的有符号类型。**
上面那组没有覆盖有符号/无符号这一支，补齐的实测值是：
`promote(uint8, int8) = int16`、`promote(uint8, uint16) = uint16`、
`promote(uint8, int16) = int16`、`promote(uint32, int32) = int64`、
`promote(uint64, int64) = int64`。注意后两条不会"就地把宽度减半给 unsigned"，
而是直接跳到更宽的有符号类型 —— 一个 32 位的无符号值域装不进任何 32 位
有符号类型，所以 `uint32 × int32` 只能是 `int64`。补充两条边界：
`promote(bool, int32) = None`（bool 不算整数，`Bool.is_integer` 是 `False`），
`promote(null, T) = T`（`null` 与任何类型都能提升成对方）。

**整数 × 浮点 → 浮点，浮点之间的宽度取大的。** 这条只保证"结果是浮点"，
不保证精度够用：`promote(float32, int64) = float32`，int64 被塞进只有 24 位
尾数的 float32。另一个方向的 `int64 × float64` 提升成 `float64` 也有陷阱 ——
float64 只有 53 位有效尾数，超过 2^53 的 int64 会丢精度。这和 Arrow / pandas
的行为一致，但值得知道。

**字符串 × 任何非字符串 → `None`，不做隐式提升。** `promote(int32, utf8)`、
`promote(binary, utf8)` 都是 `None`，交给上层报错。

**`fixed_size_list` 之间：维度不同直接 `None`，维度相同则递归提升内层。**
`promote(list<int32>, list<float64>) = list<float64>`；`struct` 之间一律
返回 `None`（本实现不支持结构体归并）。维度不同返回 `None` 这一点和上一节
呼应：**维度是类型的一部分，不兼容就是不兼容，不能被"提升"掉。**

最后一句诚实声明：**上面这套提升规则是我们实现定义的，
在 Lance 的格式规范和 LanceDB 的文档里都没有对应的明文表格。**
规范里只有 SQL→Arrow 和 Pydantic→Arrow 两张映射表
（比如 `tinyint`→Int8、`bigint`→Int64、Pydantic 的 `int`→`int64`），
并没有"int8 遇上 int32 会怎样"的规则。**真实 LanceDB 在这类混合场景下的具体行为，未经确认。**

## 5.7 用 Pydantic 声明 schema：`LanceModel`

前面六节讲的都是"schema 是什么、怎么演进"。这一节补上**最常用的那个
声明方式**——它在前面的章节里一直没出现，但官方文档里它是首选写法：
`lancedb.pydantic.LanceModel`。

**它解决的问题很具体：`pyarrow.Schema` 和你的业务对象是两套东西。**
用 pyarrow 声明，你得先写一份 schema、再写一份（或一堆）类型注解，
两边靠人保持同步。`LanceModel` 让它们变成同一份东西——
一个 Pydantic 模型既是校验规则，也是表结构：

```python
from lancedb.pydantic import LanceModel, Vector

class Doc(LanceModel):
    id: int
    text: str
    vector: Vector(4)
```

它能直接当 schema 用，也能直接当行用：

```python
db.create_table("docs", schema=Doc)
t.add([Doc(id=1, text="hello", vector=[1.0, 0.0, 0.0, 0.0])])
t.search([1.0, 0.0, 0.0, 0.0]).limit(1).to_pydantic(Doc)
# [Doc(id=1, text='hello', vector=FixedSizeList(dim=4))]
```

**往返是闭的**：模型声明结构 → 模型实例写入 → 查询结果还原成模型。
这是它相对 `pyarrow.Schema` 唯一真正的增值，也是它值得优先用的理由。

**`Vector(dim)` 是 §5.2 那个结论的 API 形态。** 上一节说"向量列不是
一种特殊类型，而是 `fixed_size_list<float32>[D]`"——`Vector(4)`
就是这句话的可写形式，它展开成的正是 `fixed_size_list<item: float>[4]`。
签名是 `Vector(dim, value_type=pa.float32(), nullable=True)`，
**维度是类型的一部分**，所以 §5.2.2 那条"改维度必须走 schema 演进"
在这里一字不差地适用。

**一个容易踩的默认值：可空性跟着 Python 注解走，不跟着直觉走。**
上面那个模型的 schema 实测是：

```
id: int64 not null
text: string not null
vector: fixed_size_list<item: float>[4]
  child 0, item: float
```

`id` 和 `text` 都带 `not null`——**因为注解写的是 `int` 而不是
`Optional[int]`**。而 `vector` 没有 `not null`，因为 `Vector` 的
`nullable` 默认是 `True`。**同一份模型里两个方向的可空性来自两套规则**，
声明的时候要一起想到，否则会在写入 null 时才撞上约束。

**`VectorField()` / `SourceField()` 是另一套东西，别和 `Vector(dim)` 混。**
前两个属于**嵌入函数**：`text: str = SourceField()` 声明"嵌入从这一列算"，
`vector: list[float] = VectorField()` 声明"这是算出来的向量列"——
它们的维度来自模型本身、不写在注解里。所以同一个位置有两种写法，
**选哪一种取决于"这个向量是你算好传进来的，还是表帮你算的"**：

```python
class ByHand(LanceModel):          # 维度写死在类型里
    vector: Vector(768)

class ByModel(LanceModel):         # 维度来自嵌入函数
    text: str = SourceField()
    vector: list[float] = VectorField()
```

第二种写法还解释了第 22 章那条主线（"表的自描述：向量是哪来的"）——
嵌入函数的配置会被写进表元数据，所以换模型时库里存着"当初是谁算的"。

**本实现没有这一层，这里要照实说清缺口有多大。**
`mini_lancedb` **没有 `LanceModel`、没有 `Vector`、也没有 `to_pydantic()`**
（实测三个都取不到）。它和这一层的关系只有一半：
`func.SourceField()` / `func.VectorField()` **那两个标记是有的**，
但它们在本实现里只做"标记 + 识别"（`mini_lancedb/connection.py`），
**没有配套的模型基类把它接起来**——也就是说，
第 22 章那套写法在真实库里能写成 `class Doc(LanceModel)`，
在本实现里只能回到显式 schema 加注册表。

**不补这一层是刻意的，理由是它和本书的验证纪律冲突。** `LanceModel`
的价值几乎全部来自"Pydantic 的运行时校验"，而校验语义是 Pydantic
的行为、不是 LanceDB 的行为——把 Pydantic 当依赖引进来，
本实现多出来的行数**测的却是别人的库**，而且第 5.7 节这种
"两套可空性规则"的坑会立刻变成"两套 Pydantic 版本的差异"。
**要学的是"为什么用声明式 schema"这个判断**，
它在真实库上的正确用法上面已经给全了。

## 5.8 小结

**真实 Lance 的容器没有类型系统。** 类型语义的权威在 manifest 里，
容器只记录"值 + 编码"。**但迷你实现偏离了这一点**：列元数据里存了
`type`，`LanceFileReader.schema()` 就靠它重建类型，所以单个文件是自描述的。
读取时数组类型来自文件、`RecordBatch` 的 schema 来自 manifest，
两者不一致就留下一条缝。

**向量列不是一种特殊类型，而是 `fixed_size_list<float32>[D]`。**
于是它天然能参与投影、切片、回表；代价是维度写死在类型里，
改维度必须走 schema 演进，而且是唯一昂贵的那类演进。

**固定长度这件事本身是性能的一部分。** 子列长度 = 行数 × D，
第 i 行的向量是一个 view，距离计算退化成一次 reshape。真实 Lance 还在
结构编码阶段就把 FSL 展平，让压缩器完全不必知道 FSL 的存在。

**字段要有稳定 id，但"永不复用"是有条件的。** 本实现的规则是
"新 id = 当前最大 id + 1"：删掉非最大 id 的列会留下空洞，删掉最大 id 的列
再加回来会**复用**那个 id。真实 Lance 的规则严格得多：depth-first 分配、
跨 dataset/分支不唯一、保留负 id 给隐藏列、`max_allocated_field_id` 高水位加
`FLAG_NON_REUSABLE_FIELD_IDS` 才能开启不复用，且激活不可撤销。

**演进的代价分两档：** 加列 / 删列 / 改名 / 改 nullable 只动元数据；
改类型要整表重写。官方推荐的做法是"加新列 + 删旧列 + 改名"，因为前两步都便宜。
四类操作里本实现缺了"改字段元数据"这一类 —— 真实 Lance 有独立的
`update_field_metadata`，我们没有对应的入口。

**类型稳定优先于类型紧凑。** Python int 一律推断成 int64，
因为表是长期存在的；省下来的字节交给编码层去逐页决定。

**声明 schema 有三条路，各有各的位置。** `pyarrow.Schema` 最底层、
最灵活；`LanceModel` 让校验规则和表结构变成同一份东西，
而且**往返是闭的**（声明 → 写入 → `to_pydantic()` 还原）；
什么都不写则走推断，代价是推断规则要符合预期。三者的可空性规则不一样——
`LanceModel` 里 `int` 是 `not null` 而 `Vector(dim)` 默认可空。
**本实现支持前两条和第三条（推断），但没有 `LanceModel` 这一层**，
缺口的边界和理由写在 §5.7。

下一章往下走一层：这些 schema、这些数据文件，究竟被放在什么样的存储上，
以及"LanceDB 不假设自己有本地磁盘"这句话会带来什么。
