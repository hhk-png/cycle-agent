# LanceDB 教程：从文件格式到一个能跑起来的迷你实现

这份教程讲 **LanceDB** 和它底下的 **Lance 列式格式**。
它有两个并行的目标，缺一不可。

**第一，把 LanceDB 讲透。** 不是"怎么调 API"，而是
"为什么这样设计、代价是什么、什么时候会失效"。文件格式的每一个字节、
索引里每一次距离计算、提交协议里每一次条件写，都尽量落到可验证的层面。

**第二，给出一个能跑的迷你实现。** `mini_lancedb/` 是一个纯 Python
（只依赖 numpy）的 LanceDB 复刻：它有自己的列式文件格式、版本 manifest、
事务协议、IVF/PQ/HNSW 索引、BM25 全文检索、表达式引擎和查询规划器。
**不是玩具** —— 它有 1144 个测试，并且在写它的过程中抓出了几十个真实的 bug。

## 怎么验证

这份教程的每一句话都尽量落到"可以自己跑一遍"上，所以它有**三条验证命令**，
而且三条都要能过：

```bash
cd lancedb-tutorial
python -X utf8 check_docs.py        # 1. 文档自检：引用、目录、硬数字是否对得上
python -m pytest tests/ -q          # 2. 测试：1144 个用例
python -X utf8 examples/01_quickstart.py   # 3. 例子：7 个教学脚本 + 8 个测量脚本
```

第一条最容易被忽略，但它抓的恰好是人眼审不出来的东西：改写章节会让小节号漂移、
让"1144 个测试"这种硬数字过期、让交叉引用指到不存在的地方。
`check_docs.py` 把这些和仓库里的事实对一遍——**它的价值不在检查本身，
而在于"每次改完都跑一次"这个习惯。**

在 Windows 上 `-X utf8` 不是可选项：控制台默认是 GBK 编码，
而输出里有中文，不加这个参数会直接抛 `UnicodeEncodeError`。
库本身没有问题，是终端的默认编码问题。

想对照真实的 LanceDB，另外装一个：

```bash
pip install lancedb pyarrow
```

本教程里所有"真实 LanceDB 的行为"都是在一台装了
`lancedb 0.40.0` + Python 3.14.6 + pyarrow 26.0.0 的机器上实际跑出来的，
不是从文档里抄的。文档和实现对不上的地方都点明了，而且每一条都给了复现命令。

## 目录

**导读与上手**

- [第 0 章　导读：这份教程怎么读](00-preface.md)
- [第 1 章　为什么需要 LanceDB](01-why-lancedb.md)
- [第 2 章　上手：一个最小的完整流程](02-quickstart.md)

**存储层：字节、文件、版本**

- [第 3 章　Lance 文件格式](03-lance-file-format.md)
- [第 4 章　列式编码与压缩](04-columnar-encoding.md)
- [第 5 章　数据模型与 Schema](05-data-model.md)
- [第 6 章　存储抽象与对象存储](06-storage-layer.md)
- [第 7 章　版本 manifest 与时间旅行](07-versioning.md)
- [第 8 章　事务与并发](08-transactions.md)

**数据操作**

- [第 9 章　删除、更新与 upsert](09-delete-update-upsert.md)
- [第 10 章　标量过滤与谓词下推](10-filter-and-pushdown.md)
- [第 11 章　表达式引擎](11-expression-engine.md)

**索引**

- [第 12 章　向量索引总览](12-vector-index-overview.md)
- [第 13 章　IVF 与 k-means](13-ivf.md)
- [第 14 章　乘积量化](14-pq.md)
- [第 15 章　HNSW](15-hnsw.md)
- [第 16 章　标量量化与二值量化](16-sq-bq.md)
- [第 17 章　全文检索与 BM25](17-full-text-search.md)
- [第 18 章　标量索引与 data skipping](18-scalar-indexes.md)

**查询与检索**

- [第 19 章　查询规划与执行](19-query-planning.md)
- [第 20 章　混合检索与重排](20-hybrid-and-rerank.md)
- [第 21 章　多向量与多模态](21-multivector-multimodal.md)
- [第 22 章　嵌入函数](22-embedding-functions.md)

**运维、生态与未来**

- [第 23 章　压实与维护](23-compaction-maintenance.md)
- [第 24 章　性能调优](24-performance-tuning.md)
- [第 25 章　生产实践](25-production.md)
- [第 26 章　生态与集成](26-ecosystem.md)
- [第 27 章　竞品对比](27-comparison.md)
- [第 28 章　版本号与路线图](28-versions-roadmap.md)
- [第 29 章　未来方向](29-future.md)

**附录**

- [附录 A　API 速查表](30-appendix-api.md)
- [附录 B　术语表](31-appendix-glossary.md)
- [附录 C　迷你实现代码导读](32-appendix-from-mini-to-real.md)
- [附录 D　坑与常见问题](33-appendix-pitfalls.md)
- [附录 E　怎么验证一个存储引擎](34-appendix-verification.md)
- [附录 F　物化视图与派生数据](35-appendix-materialized-views.md)
- [附录 G　运行时面：异步、作业与写路径](36-appendix-runtime-surface.md)
- [附录 H　摄入路径：从数据源到一次提交](37-appendix-ingestion.md)
- [附录 I　执行引擎：计划之后发生了什么](38-appendix-execution-engine.md)
- [附录 J　互操作：零拷贝、C Data Interface 与格式层边界](39-appendix-interop.md)
- [附录 K　失败与重试：哪些错误该重试，哪些重试只会更糟](40-appendix-failure-and-retry.md)

附录 F 到 K 补的是正文结构里空着的六层，
它们要么在 0.40 里已经**发布**、要么就是执行路径本身的一段，
但都落不进任何单独一章：

**附录 F 讲"用户可见的派生表"**——物化视图、计算列、字段元数据，
以及它们依赖的稳定行 id。这一层横跨第 7 章（版本）、第 8 章（提交）
和第 23 章（维护），放进任何一章都会突兀。
它会**修正正文判断**：第 29.1.4 节把稳定行 id 归为"未来方向"，
而物化视图今天就强制要求它。

**附录 G 讲"跑起来需要什么"**——异步接口、作业句柄、LSM 写路径、
克隆、预热、连接序列化，以及一张"哪些能力只在 Cloud/Enterprise 有"的清单。
这一层是第 19、25 章的背面，同样无处安放。
它也修正了一处正文判断：第 29.1.3 节把 MemWAL 放在"未来方向"，
而 0.40 已经把它做成了一条可配置的写路径。

**附录 H 讲"数据是怎么进来的"**——`Scannable` 契约、可重扫与一次性
数据源的区别、宽行摄入时那个**与总行数无关**的内存上限，
以及本实现为什么选择"全部物化"。
这一层是第 2、6、24、25 章各自的片段的粘合处，原来散在四处、
谁也没有完整讲过。

**附录 I 讲"计划之后发生了什么"**——第 19 章标题里写着"查询规划与执行"，
十个小节却全是规划。这一篇把缺掉的那半补上：真实引擎是 DataFusion
的物理计划（实测 `analyze_plan()` 里那些 `*Exec` 算子）、
每个算子自带的账本（`elapsed_compute` 与 `elapsed` 差在哪、
`task_wait_time` 为什么是排查第一步）、并行度写在 `output_batches` 上而
合并点写在 `preserve_partitioning=[false]` 上、以及延迟物化在真引擎里
是**两个物理读取算子**而不是一个开关。它还照实指出执行层的旋钮
（并发度、预读深度）根本不在 `lancedb` 包里——**一条由缺件划出的边界，
比一句"未确认"有用。**

**附录 J 讲"数据怎么出去"**——附录 I 停在 `RecordBatch`，
这一篇接上零拷贝那一段：Arrow 的 C Data Interface 有 `array` 和
`stream` 两条路且语义不同、LanceDB 的结果导出走的是哪一条（实测
`__arrow_c_stream__` 有而 `__arrow_c_array__` 没有）、
`to_df` 已经弃用、以及**API 层互操作与格式层互操作是两条不能互推的路径**。
它是全书**缺口写得最坦白**的一篇：`pandas` / `polars` / `duckdb`
三个包本机都没装，所以"到 DataFrame 的零拷贝成不成立"**照实写成没有实测结论**。

**附录 K 讲"出错时该怎么办"**——附录 D 那 55 条坑按**症状**分类
（【报错】【静默】【崩溃】…），这一篇按**动作**分类：
这个异常属于哪一族、该不该重试。实测出来的分布很反直觉——
十个探针里八个是 `ValueError`，**所以"按类型分流"在真实库上根本做不到**，
可重试性只能按"归责方"判断。它还专门讲**最危险的一类失败是没有异常**：
`add()` 少给一列会静默写 `null`，8 个线程并发写 321 行**零异常**。
**"没有报错"在这个系统里不构成正确性证据。**

六篇都遵守同一条纪律：**每个数字都标了是本机实跑还是引用文档**，
而"本地跑不通"的那些**照实写成跑不通**——附录 G 里那张
"哪些是开源版没有的"清单，每一条都带着本机实测的报错文本
（注意它**压缩**过，完整原文都带
`NotImplementedError: LanceDBError: not supported: ` 前缀，
所以别按截断后的字符串去 `except` 匹配）。
附录 H 还多做了一件事：它是全书唯一一处**把"我需要拒绝这种输入"
的两种实现路线并排摆出来**的地方（真实库接受但降级，
本实现拒绝但报错清楚），因为它讲的那一层正好是
"取舍"而不是"正确答案"。

## 目录结构

```
lancedb-tutorial/
  README.md              本文件
  00-preface.md …        教程正文（第 0–29 章 + 11 篇附录，共约 2.8 万行）
  mini_lancedb/          迷你实现（纯 Python + numpy，约 3.6 万行）
  tests/                 测试套件（1144 个用例，约 1.1 万行）
  examples/              可以直接跑的示例脚本
  examples/verify/       正文直接引用的原始测量脚本
  measurements/          索引实测数字的唯一来源
  check_docs.py          文档自检
```

三层脚本的分工不是随手分的，它直接决定了"正文里的数字能不能复现"：

`examples/` 里的 7 个脚本是**面向读者的教学脚本**，每个对应若干章节，
输出是人读的（带标题、带解释）。`examples/verify/` 里的 8 个脚本是
**正文直接引用的原始测量脚本**（第 9、10、11、12、18、20、22 章）。两者分开的原因很实际：
正文引用的是"当时跑出那个数字的东西"，需要保持原样可复现；
而教学脚本会随着叙述需要调整输出格式。混在一起的结果是——
正文引用的数字和脚本输出慢慢对不上，而没人会发现。

`measurements/vector_index_measurements.py` 是第 13 到 16 章全部索引数字的唯一来源，
一章一节，可以按章号单独跑（`python -X utf8 measurements/vector_index_measurements.py 13`）。
把它集中在**一处**的原因同样很实际：索引参数是互相影响的，
散在四个脚本里就无法保证四章引用的是同一份数据、同一组参数、同一台机器。

`examples/` 里的脚本：

| 脚本 | 对应章节 | 演示什么 |
|---|---|---|
| `01_quickstart.py` | 第 2 章 | 建表、建索引、三种检索、增删改、时间旅行、维护 |
| `02_file_format_tour.py` | 第 3 章 | 把 `.lance` 文件拆开看：footer、页表、编码、对齐 |
| `03_encoding_report.py` | 第 4 章 | 每种编码的实际字节数，含 LZ4 与 C 实现对拍 |
| `04_index_benchmark.py` | 第 12–16、24 章 | 六种向量索引的召回率/延迟/体积实测 |
| `05_embedding_search.py` | 第 22 章 | 离线嵌入 + 把"向量是谁生成的"写进表元数据 |
| `06_versioning_and_maintenance.py` | 第 7、8、9、23 章 | 版本历史、删除的真实代价、压实反而变慢 |
| `07_pushdown_and_io.py` | 第 10、19、24 章 | 四层下推的收益量化，以及索引缓存为什么必须存在 |
| `examples/verify/ch*.py` | 第 9、10、11、12、18、20、22 章 | 那几章正文里引用的原始测量脚本 |

迷你实现的模块划分，以及每个模块对应哪一章：

```
mini_lancedb/
  types.py       类型系统（Arrow 类型的精简复刻）        第 5 章
  schema.py      Field / Schema / 稳定 field_id          第 5 章
  arrays.py      Arrow 风格的列式数组                    第 5 章
  record.py      RecordBatch / 行组与片段切分            第 5 章
  errors.py      异常体系                                贯穿全书
  storage/
    base.py      对象存储抽象 + 内存/本地/S3 模拟 + I/O 计量   第 6 章
  encoding/
    buffer.py    缓冲区、页表、64 字节对齐                第 3 章
    codecs.py    页编码（bitpack/for/delta/rle/dict/fsst/bss） 第 4 章
    compress.py  LZ4 块格式（自实现，与 C 实现对拍）       第 4 章
  format/
    lance_file.py  .lance 容器读写 + 页表 + 40 字节 footer  第 3 章
    manifest.py    版本 manifest、标签、latest hint        第 7 章
    transaction.py 事务文件与冲突矩阵                      第 8 章
    deletion.py    删除文件（差分 varint）                 第 9 章
  fragment.py    片段与数据文件、稳定行地址                 第 7 章
  expr/          表达式 AST / 解析器 / 向量化求值 / 改写     第 11 章
  scanner.py     扫描器：投影下推、统计剪枝、延迟物化         第 10、19 章
  dataset.py     数据集：写入、删除、更新、merge_insert、版本  第 8、9 章
  index/
    metric.py    距离度量与 Top-K                         第 12 章
    pq.py        k-means + 乘积量化                       第 13、14 章
    ivf.py       IVF 倒排列表                             第 13 章
    hnsw.py      分层可导航小世界                         第 15 章
    sq.py / bq.py 标量量化与二值量化                      第 16 章
    inverted.py  倒排索引 + BM25                          第 17 章
    btree.py 等  标量索引与 data skipping                 第 18 章
  fts/tokenizer.py 分词器管线 + Porter 词干化             第 17 章
  rerank/        RRF / 线性融合 / cross-encoder / ColBERT   第 20、21 章
  embedding/     嵌入函数注册表与离线实现                   第 22 章
  query.py       查询构建器（向量 / 全文 / 混合）            第 19 章
  table.py       LanceTable：面向用户的表                   第 2 章
  connection.py  连接与数据库                               第 2 章
  optimize.py    压实与清理                                 第 23 章
  stats.py       表统计与索引统计                            第 23、24 章
  namespace.py   命名空间（多租户的本地模型）                第 25 章
  permissions.py 权限模型                                   第 25 章
```

`namespace.py` 与 `permissions.py` 对应第 25 章，但正文里只讲了大致的取舍，
想看它们的接口格局要去附录 A。

## 读这份教程需要什么

不需要向量数据库的经验，但需要：

- 会 Python，看得懂 numpy 的向量化写法；
- 知道"列存"和"行存"的区别大概是什么；
- 对"索引"有直觉（知道 B 树或倒排索引做过什么就行）。

数学部分（k-means、PQ 的距离表、BM25 的公式、HNSW 的启发式）
都是从零推的，不需要预先学过。

## 关于"验证"

这份教程里出现的每一个数字、每一段输出，都是在写它的过程中
实际运行得到的。有几条是特意保留的"打脸"结果，因为它们比
漂亮的说法有用：

- 第 4 章：`byte_stream_split` 常被说成能省 20% 到 30%，
  我们实测是 **7.1%**；而在量化过的浮点上，它反而**更大 12%**。
- 第 23 章：压实**不一定**让查询变快。实测同一个查询在压实后
  读了 **约 27 倍**的字节——因为 100 组 min/max 统计被合并成了一组。
- 第 24 章：`IVF_PQ` 的召回率在 `nprobes` 从 1 加到 64 的过程中
  只从 0.42 升到 0.47 就卡住不动了。它是一条平台，上限由量化误差决定——
  该拧的旋钮是 `refine_factor`，不是 `nprobes`。
- 第 4 章：我们自己实现的 LZ4 曾经写出了一个"谁也读不了"的文件，
  而**自洽的往返测试没有发现它**——最后是拿 C 实现对拍才抓住。

**一轮独立审计又抓出了两个会丢数据的 bug**，它们比上面那些更值得记，
因为形状都是"写成功了、当时不报错、事后才炸"：

- **第 7 章**：`cleanup_old_versions` 会把**标签指向的版本的
  数据文件删掉**。标签保住了 manifest，却没保住数据，
  于是清理成功返回、几天后 `checkout("release-1")` 报
  `StoreError: 对象不存在`。根因是 `keep_set` 算在标签过滤**之前**——
  顺序一换语义就反了。测试没抓住它的原因很具体：
  有测标签的用例、有测清理的用例，**但没有一条把两者串起来**。
- **第 5 章**：把一列**全 null** 的列改类型，会写出一张
  **任何列都读不出来**的表。两个 bug 叠在一起：
  字典编码让空字典通过校验（`len(data)` 不是 0），
  修掉它之后常量编码的元数据又塞进一个 0 维 ndarray 让 JSON 序列化炸掉。
  两次修复都补了回归用例，并且都验证过"把修复回退，用例确实失败"。

这两条和上面那些的区别在于：**它们不是"数字对不上"，是"数据会没"。**
第 7 章那条现在有专门的篇幅讲"为什么测试全绿还是漏了它"。

写这个迷你实现的过程里抓出的真实 bug 分布在每一章里：
delta 解码漏加基值（只在首值非零时暴露）、B+ 树内部节点的分隔键
维护错位、LIKE 前缀+后缀快速路径在短字符串上误命中、
IVF 分区查找把行号当成了分区下标、等等。它们都写在对应的章节里，
因为"bug 长什么样、为什么测试没抓住"本身就是工程内容。

一套手段支撑着上面所有结论，它被单独写在附录 E 里：
对拍（差分测试）、不变量、回归测试，
以及三种"测试全绿但实现是错的"的假绿。
**那份附录也是这份教程能被检验的原因** —— 它说明了每个数字
是在什么条件下、用什么方法得到的，以及哪些数字**不能**当断言用。

## 许可与出处

教程正文与 `mini_lancedb/` 代码为本教程原创。
文中引用的真实 LanceDB / Lance 的行为、性能数字与设计决策，
来源包括官方文档（`docs.lancedb.com` / `lance.org`）、
`lance-format/lance` 仓库的格式规范与 Rust 源码、
以及本机 `lancedb 0.40.0` 的实测。厂商自报的性能数字都标注了口径。

引用外部数字的方式是固定的三条：**标来源、标日期、标口径**。
厂商口径的数字永远不和本机实测混在同一句话里；
查不到出处的标"未经确认"，从实测外推的标"推算"。
这不是学术洁癖——混在一起之后，读者就没有任何办法区分
哪一部分可以信、可以信到什么程度。
