# 第 26 章　生态与集成

这一章要回答的问题是：**LanceDB 能接到哪些东西上，接的方式是什么，
以及"官方支持某某"这句话到底值多少钱。**

写这一章有一条纪律：**以官方文档索引为准，不以厂商的合作方 logo 页为准。**
lancedb.com/partners 上列了几十家公司的名字，其中不少只意味着
"贴了 logo"，不意味着有任何一行集成代码。判断真伪最硬的办法，
是看官方文档的机器可读索引 `docs.lancedb.com/llms.txt` 里有没有它自己的页面。

## 26.1 最关键的一条分界线：检索层还是格式层

这一章最重要的一件事，是把两类完全不同的"集成"分开。
把它们混为一谈，会得出完全错误的结论：

**第一类：LanceDB（检索层）的集成。**
LanceDB 是嵌入式向量 / 多模态检索库，提供 vector / FTS / hybrid 检索、
reranker、embedding 注册表。它的集成方式是
Python/TypeScript/Rust SDK、各框架里的 `VectorStore` 适配类。
典型消费者是 LangChain、LlamaIndex、各类 Agent 框架。

**第二类：Lance（格式层）的集成。**
Lance 是开源列式湖仓格式（file format + table format），只管读写与版本。
它的集成方式是**别的引擎里实现的 reader / writer connector**。
典型消费者是 Spark、Trino、DuckDB、Ray、PyTorch、Flink。

两者的组织都不在同一处：Lance 子项目都在 `lance-format` 组织下
（`lance-spark`、`lance-ray`、`lance-duckdb`…），
LanceDB 本体、插件、recipes 在 `lancedb` 组织下。

**这个区分不是学究式的。** 它决定了三件事：

第一，**Lance 格式的消费者远多于 LanceDB 的集成方**。
用 SQL 查 Lance 表不需要安装 lancedb、不需要 Python、不需要任何
LanceDB 的代码——`lance-spark` 是一个纯 JVM 侧的 connector。
反过来，装上 lancedb 也**不会**让你的表自动能被 Spark 读——
读的是 Lance 格式，不是 LanceDB。

第二，**版本演进的方向是反的**。LanceDB 升级会改变 Python API 的写法；
Lance 格式升级会影响所有 reader 的兼容性（比如
`new_table_enable_v2_manifest_paths` 要求读取端 ≥ 0.10.0）。

第三，**"LanceDB 支持 Spark"这句话本身是错误归因**。
官方 `integrations/` 索引里**没有** Spark、Trino、Flink、Ray 的独立页面——
它们是 Lance 格式层的连接器。教程里写这一条时措辞必须精确。

## 26.2 官方集成页的完整清单

下面是 `docs.lancedb.com/llms.txt`（抓取于 2026-10-17）里
`integrations/` 下的**全部**条目，去掉重复后按目录归类。
这是判断"某个集成真不真"的 ground truth：

| 分类 | 官方有独立页面的集成 |
|---|---|
| AI 框架 / Agent | `agno`、`genkit`、`hermes-agent`、`huggingface`、`kiln`、`langchain`、`llamaindex`、`prompttools`、`synthetic-data-kit` |
| 数据 / DataFrame | `dlt`、`duckdb`、`pandas_and_pyarrow`、`polars_arrow`、`pydantic`、`voxel51` |
| Embedding provider | `aws`、`cohere`、`colpali`、`gemini`、`huggingface`、`ibm`、`imagebind`、`instructor`、`jina`、`ollama`、`openai`、`openclip`、`sentence-transformers`、`superlinked`、`voyageai` |
| Reranker | `answerdotai`、`cohere`、`colbert`、`jina`、`openai`、`voyageai`、`watsonx` |
| 训练 / 数据集 | `lerobotdataset`、`stable-worldmodel` |

**这张表里的"Embedding provider"那一行，不要和运行时注册表对照着读。**
两者是不同的东西，而且**拼写故意不一样**：这张表记的是**文档页面的名字**
（`llms.txt` 的目录项），而第 22 章 §22.4 那张表记的是
**`get_registry()` 里真实注册的名字**。本机实测后者的 17 个名字是

```
bedrock-text  cohere  colbert  colpali  gemini-text  gte-text  huggingface
imagebind  instructor  jina  ollama  open-clip  openai  sentence-transformers
siglip  voyageai  watsonx
```

两边对不上的地方至少有五处：文档里叫 `aws`、注册表里叫 `bedrock-text`；
文档里 `openclip`、注册表 `open-clip`；文档里 `gemini`、注册表 `gemini-text`；
文档里 `ibm`、注册表 `watsonx`；文档里有 `superlinked`（那是一个
**独立安装的集成包** `sie-lancedb`，不在核心注册表里），
而注册表里有 `siglip` / `gte-text` 这两个**没有独立文档页**的名字。
**拿文档页去查代码里的注册名会查不到**——反过来也一样。
第 21 章 §21.3 讲到多模态选型时用的是注册表那套名字（`siglip`）。

**表里另有一个名字要特别说明：`duckdb` 同时出现在这张表和 §26.6 里，
而那两个位置说的不是同一件事。**这张表里的 `duckdb` 指的是
"官方有一页讲怎么从 DuckDB 那边用 LanceDB"；§26.6 里的 DuckDB 指的是
**格式层的 `lance` 扩展**——在 DuckDB 进程里直接用 Lance 的 Rust 库读文件，
完全不经过 LanceDB 的 Python API。

这个区分不是抠字眼，它决定了你**能拿到什么**：走格式层那条路，
你拿到的是 SQL 和 join，**拿不到 ANN / BM25 / 重排**（第 19.9 节末尾
那条"检索算子过不去"的边界说的就是这个）。所以
`duckdb` 是 §26.1 那条分界线上少见的"两边都占"的名字，
按 §26.9 的清单检查它时，要先问一句"你说的是哪一条路"。

**由此可以明确下结论的"没有官方集成"清单**——
不是"未确认"，是**在官方索引里确实不存在**。

**Haystack** 在官方索引里没有页。Haystack 社区侧做了 LanceDB document store，
属于**反向集成**，引用时必须注明是社区侧，不能写成"LanceDB 支持 Haystack"。

**Daft** 在官方 `integrations/` 里**不存在**。Daft 是 Lance 的**格式层消费者**
（Daft 自己支持读写 Lance），但没有任何 LanceDB 官方集成 —— 这正是 26.1 那条分界线的又一个例子。

**Pathway** 在索引中不存在，官方从未发布 Pathway 集成。

**MLflow / Airflow / Dagster** 也**没有独立集成页**。这三者只在博客或示例语境里出现
（例如用 Airflow 编排 ILM、用 MLflow 记录实验），**不是官方集成**；
dlt 能间接接 Airflow / Dagster / dbt，但那是 dlt 的能力，不是 LanceDB 的。

**n8n / Zapier / Dify / Coze** 这类低代码平台侧**没有任何官方节点**。

**Kubeflow / Feast / 其他特征库**无官方集成。LanceDB 在这块的自研对应物是 Geneva
（特征工程层），分布式部署走 KubeRay。

**LangGraph / CrewAI / Pydantic AI / AutoGen** 情况各不相同，见 26.3 ——
**不能一概写成"官方集成"**。

官方合作伙伴页（lancedb.com/partners）把合作方分三档，其中战略档是
AWS、Azure、Google Cloud、NVIDIA；生态档里有 DuckDB、LlamaIndex、
HuggingFace、CrewAI、LangChain、TwelveLabs、Encord、Anyscale、
Databricks、Snowflake、Nebius、Tigris。**这份名单是"贴了 logo"的名单，
不等于深度集成**——下一节逐个给出它们到底提供了什么。

## 26.3 AI 框架与 Agent 框架：集成到底提供了什么

几乎所有框架集成的形态都是同一个：**一层 `VectorStore` 适配类**，
把 LanceDB 的"建表 / 加数据 / 相似度查询 / 按元数据过滤"映射成
框架自己约定的接口。看一个集成是否靠谱，就看它有没有覆盖
"增删改查 + 过滤 + 混合检索"这几件事，还是只有 `similarity_search`。

**LangChain** 提供的是一等的 `LanceDB` VectorStore 类，
现行导入路径是 `from langchain_community.vectorstores import LanceDB`。
旧的 `from langchain.vectorstores import LanceDB` **已被弃用**
（源码里有一条 `DEPRECATED_LOOKUP`，会动态重定向到 `langchain_community` 并打弃用警告），
2023–2024 年的教程几乎全是老路径，照抄会踩坑。能力包括：
`from_documents()` / `from_texts()` 建库、`similarity_search`、
`similarity_search_with_relevance_scores`、
`max_marginal_relevance_search`、`add_texts`、`add_images`（多模态）、
`create_index()`（可为向量列或标量列建索引）、`get_table()` 拿到原生表对象，
以及一个 `reranker` 参数可以直接挂 LanceDB 的 reranker。
它还能通过 `uri` / `api_key` / `region` 连到 LanceDB Enterprise。

它的参数表里有几个默认值必须记住，因为它们会静默决定你的索引形态：
`uri` 默认 `/tmp/lancedb`（**不是当前目录**），
`table_name` 默认 `vectorstore`，`text_key` 默认 `text`，
`id_key` 默认 `id`，`vector_key` 默认 `vector`，`distance` 默认 `l2`，
返回条数默认 `DEFAULT_K`（**4**）。
而它自己那个 `create_index()` 的默认值是
`num_partitions=256` / `num_sub_vectors=96`——
**这就是"256"这个数字最常见的来源之一**，
它和"什么时候该建索引"没有关系。

**LlamaIndex** 由独立包 `llama-index-vector-stores-lancedb` 提供
`LanceDBVectorStore`。**官方发行名是全小写**，文档与代码里都统一用小写引用
（PyPI 按 PEP 503 对名字做了归一化，大小写与 `-`/`_` 都不敏感，
所以旧教程里的 `llama-index-vector-stores-LanceDB` 也能装上，
但写法本身是不规范的）。它比 LangChain 那层更贴 LanceDB：
`query_type` 可以在 `vector` / `fts` / `hybrid` 三种之间切，
支持 metadata filter（通过 `vector_store_kwargs={"where": lance_filter}` 传
LanceDB 原生的过滤表达式字符串），并且可以 `_add_reranker()` 挂 LanceDB 的
reranker（官方示例用的是 ColBERT）。

**CrewAI** 是一个罕见的深度案例：LanceDB 是 **CrewAI OSS 所有 memory
与 embedding 功能的默认后端**。团队公开的改造理由是"原生多模态、
无服务进程（纯文件）、依赖面小、强类型 schema 能把 scope key /
importance / 时间戳 / 向量放在同一行并建 B-tree 索引"，
而改造前的痛点是"向量库 + 独立结构化 DB 两套系统，
Agent 会记住互相矛盾的事实"。厂商口径的数字是：
12M 月下载、20 亿+ 次 agent 执行、召回约 200 ms 且不需要 LLM 调用。

**Hermes Agent（Nous Research）** 是官方维护的记忆插件
`lancedb/hermes-agent-memory`：单表持久化长期记忆，
向量 + BM25 + hybrid + metadata filter，**没有独立的 memory 服务进程**。

**Agno** 有官方文档页：`agno.vectordb.lancedb.LanceDb` 作为 Knowledge 后端，
支持 `SearchType.hybrid` 并直接用 LanceDB 的原生 FTS。

**Genkit（Google）** 是 TypeScript 侧的插件 `genkitx-lancedb`（npm），
把 LanceDB 注册成 Genkit 的 retriever + indexer，可在 Genkit Dev UI 里调。

**Kiln AI** 的集成方式比较特别：它用 LanceDB 做 vector / BM25 FTS / hybrid
三种检索的**对比评测**，然后一键把数据接到 LanceDB Enterprise。
对做检索质量调优的人来说，这是比"VectorStore 适配器"更有价值的一种集成。

**PromptTools** 是社区侧的评测工具；**Meta 的 Synthetic Data Kit**
把 `ingest` 阶段的数据直接存成 Lance 数据集，全流程读写 Lance，
支持 text + image 列的多模态 QA 生成——但它是 Meta 维护、Lance 作为存储，
属于"别人用 Lance 格式"，不是"LanceDB 的集成"。

**LangGraph / Pydantic AI / AutoGen 三个要说清楚：**
LangGraph 在官方合作方页上被列为
"stateful agentic workflows with LanceDB as the persistent memory layer"，
但文档层没有独立的 `integrations/ai/langgraph` 页面——
它的支撑材料是**分支/版本文档**（agent-branch-experiments）。
Pydantic **AI**（不是 Pydantic 本身）**未确认**有官方集成；
社区项目（如 MCP + LanceDB 索引）可作为间接路径。
AutoGen **没有官方集成**，社区项目 `memory-lancedb-pro` 声称可以插入
AutoGen 工作流（未验证）。

官方还自己维护了一个 Agent 插件市场 `lancedb/lancedb-agent-plugins`，
可以装进 Claude Code / Codex / Cursor / Copilot CLI 等，
给编码 Agent 提供 API 参考与分支操作说明。这是"给 Agent 用的集成"，
和"给 Agent 框架用的存储"是两回事。

## 26.4 Embedding 注册表与 Reranker

LanceDB 内置一个 embedding 注册表，用 `get_registry().get("<name>")` 取。
**这是 LanceDB 侧的集成**（写入/查询时自动算向量），不是 Lance 格式的。
官方支持的 provider 覆盖面相当宽，按"通用文本 → 本地 → 多模态"三段看。

**通用文本 embedding。** **OpenAI** 默认 `text-embedding-ada-002`，
`text-embedding-3` 系可指定更小维度，并支持 Azure（`use_azure=True`）。
**Cohere** 用 `embed-english-v3.0` / `embed-multilingual-v3.0`，
并区分 `source_input_type` 与 `query_input_type`；但**它的默认模型在文档里自相矛盾**
（概览页写 `embed-english-v3.0`，专页写 `embed-english-v2.0`），引用时不要只抄一处。
**Hugging Face** 通过 `transformers` 加载任意 HF 模型，默认 `colbert-ir/colbertv2.0`，
另有 `get("colbert")`。**Sentence-Transformers** 默认 `all-MiniLM-L6-v2`，
而**它的 `trust_remote_code` 默认为 `True`** ——
会执行 HF 仓库里的代码，这在企业环境里是要过安全评审的一件事，写代码时最好显式关掉。
**Gemini** 有五种 task type（`retrieval_query` / `retrieval_document` /
`semantic_similarity` / `classification` / `clustering`）。
**VoyageAI** 的 Voyage 4 系列（2026-01-15 发布）含 `voyage-4-large` / `voyage-4` /
`voyage-4-lite`，默认 1024 维、可切 256/512/1024/2048。
**IBM watsonx** 推荐默认 `ibm/granite-embedding-278m-multilingual`（768 维），
`ibm/slate-*` 与部分 sentence-transformers 模型仍可解析但不再推荐用于新表。
**Instructor** 是指令微调嵌入，可给 domain / text_type / task_objective 模板。
**AWS Bedrock** 用 `amazon.titan-embed-text-v1`，也支持 Cohere 的 Bedrock 版本。

**本地与自托管。** **Ollama** 指向 `http://localhost:11434`、默认 `nomic-embed-text`，
可配 `keep_alive`。**Superlinked SIE** 以插件形式注册 `"sie"` 与 `"sie-multivector"`
两个函数（包名 `sie-lancedb` / `@superlinked/sie-lancedb`），需要自建 SIE 实例。

**多模态与跨模态。** **Jina** 默认 `jina-clip-v1`，同时支持文本与图像。
**ImageBind（Meta）** 支持 Audio / Video / Text 并可扩展到 Thermal / Depth / IMU，
**同一张表就能做跨模态检索**。**OpenCLIP** 默认 `ViT-B-32` + `laion2b_s34b_b79k`，
**支持直接从 URL 摄取图片**。**ColPali / ColQwen** 是**多向量（late interaction）**，
schema 必须用 `MultiVector(dim)` 而不是 `Vector(dim)` —— 写错了会在建模阶段就失败。

Reranker 分两类。**免模型**的三个都能在本地算：
`LinearCombinationReranker`（归一化向量分与 FTS 分后加权，`weight` 默认 0.7
偏向向量分）、`RRFReranker`（Reciprocal Rank Fusion，按排名融合，
是 hybrid search 的默认 reranker）、`MRRReranker`（加权倒数排名）。
**需要模型**的包括 `CohereReranker`、`CrossEncoderReranker`（默认
`cross-encoder/ms-marco-TinyBERT-L-6`）、`ColbertReranker`、
`AnswerdotaiRerankers`、`JinaReranker`、`VoyageAIReranker`、
`WatsonxReranker`，以及官方标注为 **Experimental** 的 `OpenaiReranker`
（OpenAI 没有专用 rerank 模型，它底层是一个 chat 模型，属于"用 LLM 冒充
reranker"，不要在生产里当主力）。

**一条容易踩的限制**：provider 专属 reranker **目前只有 Python 版**。
TypeScript 与 Rust SDK 只暴露通用的 `Reranker` 接口
（`rerankHybrid` / `rerank_hybrid`）和内置 `RRFReranker`——
想在 TS/Rust 里用 Cohere 或 ColBERT 重排，得自己实现。

## 26.5 数据侧：DataFrame、摄入与可视化

**Pandas / PyArrow / Polars / Pydantic** 都是"进得来出得去"的一等公民。
`db.create_table()` 直接吃 `pd.DataFrame`，查询结果可以回落成 DataFrame；
因为 Lance 建在 Arrow 之上，`pa.Table` 进 `create_table` 理论上零拷贝；
Polars 的 DataFrame 与 **LazyFrame**（比内存大也能链式变换）都能直接读写。

Pydantic 侧是 `lancedb.pydantic.LanceModel` / `Vector` / `MultiVector`：
用 Pydantic 模型定义 schema、写入、把查询结果 cast 回去。
它只认 `LanceModel` 的子类（本身派生自 `pydantic.BaseModel`），
嵌套的 Pydantic 模型会映射成 struct 列。

**dlt（dlthub）** 是一个目的地在 LanceDB 的 ETL 工具：
从任意源（数据库 / API / CSV / JSON）出发，自动规范化 + schema 推断 +
增量加载，可选"边写入边嵌向量"。它是 dlt 官方维护的 destination，
所以是"真集成"。

**它为什么值得单独点一句：它把第 9 章那套语义接到了"来源"这一侧。**
dlt 的增量加载需要一个**声明式的主键**来判断"这条是新的还是旧的"，
而 LanceDB 那一侧接收它的动作正是 `merge_insert`——
一个按 join 键判断更新还是插入的算子。所以这条管道能不能做对增量，
取决于三件事能不能对齐：

**主键在两边是同一个。**dlt 声明的键必须就是 `merge_insert` 用的键，
否则"增量"会退化成"全量追加"——表会越长越大，而且没有一行被更新。

**写入模式要显式选。** dlt 有自己的写入策略概念（追加 / 替换 / 合并），
它到 LanceDB 的映射不是自动的：选"追加"意味着表会累积历史版本、
每一条改动都留一份。

**写进去的行不在索引里。**这是第 12.6 节那条：`merge_insert` 之后
新行对向量索引是不可见的，得等索引更新。dlt 这类工具最容易踩的地方
就是"一次性灌完几十万行、然后以为索引自动覆盖了"。

**本节只做定性。**具体的 destination 名称、配置字段、`write_disposition`
的取值在 dlt 官方页上，本教程**没有在本机复跑过这条管道**
（没装 dlt），所以不给配置示例——给了也只能是从文档抄的。
要用的读者请照官方页配，然后用上面三条核对一遍。

**FiftyOne / Voxel51** 是计算机视觉数据集的标注与可视化平台，
它的集成方式是把 LanceDB 当**相似度后端**：
`compute_similarity(model=..., backend="lancedb")` 建相似度索引（brain key），
`sort_by_similarity()` 用 ID / 向量 / ID 列表 / **文本 prompt** 查询，
Python API 与 App 点选都能用。给 CV 数据集做去重和相似检索，
这是最短的一条路。

## 26.6 格式层互操作：为什么这条路径和 Python API 是两回事

从这一节开始是另一条线：**别的引擎直接读 Lance 文件**。
它和"通过 LanceDB Python API 访问"是两件独立的事，值得单独讲清楚。

**DuckDB。** DuckDB 有官方核心扩展 `lance`：

```sql
INSTALL lance;
LOAD lance;
ATTACH './local_lancedb' AS lance_ns (TYPE LANCE);
SELECT * FROM lance_ns.main.my_table;
```

挂载之后，SQL 的谓词与投影能下推到 Lance 层，可以 JOIN，
也能用表函数做检索：`lance_vector_search` / `lance_fts` / `lance_hybrid_search`，
返回列是 `_distance` / `_score` / `_hybrid_score`，
参数包括 `k`、`prefilter`、`alpha`、`oversample_factor`。
写回也支持（`CREATE TABLE`）。

这里有一条**官方明确标注的用法变更**：早期 LanceDB 推荐
"先 `table.to_arrow()` 把 Lance 表转成 Arrow 表再给 DuckDB 扫"，
这条路径**仍然可用但已不再是推荐工作流**——现在应该用 Lance 扩展
加 namespace attach，让 SQL 查询直接下推到 Lance 层。
老教程里到处是 `to_arrow()` 的写法，看到要警惕。

**这条路径和 Python API 的区别在哪？** 最要紧的两条是**进程模型**与**能力面**。
进程模型上，DuckDB 的 Lance 扩展是在 DuckDB 进程里直接用 Lance 的 Rust 库读文件，
Python API 走 lancedb 的 Python 包 —— 两条路读的是**同一份文件**，但走的是两套代码。
能力面上，SQL 路径给你"用 SQL 表达查询"（JOIN 别的表、窗口函数、聚合，
这些是 Python API 做起来很别扭的事），Python API 给你完整的检索能力
（多向量 late interaction、自定义 reranker、分支与 tag 操作），这是 SQL 路径没有的。

第三条区别是**版本语义的归属**：DuckDB 自己**不提供** Lance 式的时间旅行，
在 SQL 里读到的历史版本是**通过 Lance 格式获得的**，不是 DuckDB 的能力。
这条在第 27 章 §27.16 讲 DuckDB VSS 时会再展开，本节不重复。

**其余格式层连接器**（都在 `lance-format` 组织下）覆盖了主流计算生态，
按"查询引擎 → 训练与管道 → 其他"三段看。

**查询引擎侧。** **Apache DataFusion** 是**内置**的（Rust 与 Python 都有），
把 Lance 数据集注册成 DataFusion 表跑 SQL，带谓词/投影下推。
**Apache Spark**（`lance-format/lance-spark`）功能面最宽 ——
DDL 覆盖 CREATE/DROP NAMESPACE、CREATE TABLE、ALTER TABLE、CREATE/DROP INDEX、
**CREATE BRANCH/TAG**、OPTIMIZE、VACUUM；DQL 覆盖 SELECT、FTS、VECTOR_SEARCH、
SEARCH、HYBRID_SEARCH；DML 覆盖 INSERT、INSERT OVERWRITE、UPDATE、DELETE、
ADD COLUMNS FROM、UPDATE COLUMNS FROM，另有 Databricks 专页。
**Trino**（`lance-format/lance-trino`）把 Lance 联邦进 Trino 与其他 connector
一起查（SELECT / INSERT / UPDATE / DELETE / MERGE）。
**Apache Doris** 走 filesystem 与 REST catalog，支持谓词下推 + 向量检索，但**只读**。

**训练与数据管道侧。** **Ray**（`lance-format/lance-ray`）用 Ray Data 分布式
读写 Lance，含分布式索引、compaction、data evolution；**PyTorch** 走**内置**的
`lance.torch.data.LanceDataset`，直接当 `torch.utils.data.IterableDataset` 用；
**TensorFlow**（`lance-format/lance-tensorflow`）用 `lance_tensorflow.from_lance`
流式喂进 `tf.data.Dataset`；**Hugging Face**（`lance-format/lance-huggingface`）
一句调用在 HF datasets ↔ Lance 之间互转，LanceDB 也能用
`hf://datasets/<org>/<name>/data` URI 直接扫 Hub 上的 Lance 数据集。

**其他。** **PostgreSQL**（`lance-format/pglance`）用 pglance 扩展从 Postgres
读 Lance 数据集；**Apache Flink**（`lance-format/lance-flink`）从 Flink 作业把流
数据写入 Lance；**Catalog** 侧由 `lance-format/lance-namespace-impls` 提供
Apache Hive、Apache Polaris、Apache Gravitino、Unity Catalog、AWS Glue 等参考实现。

**LanceDB 侧还有两个训练相关的集成都值得一提**：
`Table` 自身实现了 `torch.utils.data.Dataset` 契约，
可以直接 `DataLoader(table, collate_fn=...)`；
更推荐用 `lancedb.permutation.Permutation`（支持 arrow / polars / numpy /
pandas / torch_col 等 transform，**arrow 与 polars 永远零拷贝**，
python / torch 至少一次全量拷贝、最慢）。
另外 **LeRobot（Hugging Face 机器人栈）原生支持 Lance**：
配置里写 `"storage_format": "lance"` 即切到 Lance reader，其余流程不变。

## 26.7 迁移路径

需要先说一句实话：**LanceDB 官方没有公开的"从 Milvus/Pinecone 迁移到
LanceDB"技术指南**（多方检索未见）。实际路径就是**重新摄取、重新索引**：
例如 AnythingLLM 是通过 `VECTOR_DB` 环境变量切 provider 后
`resetAllVectorStores` 并重建索引。**没有可以"就地转换"的缆线。**

真正有工具的是**格式侧**的迁移。Lance 官方提供了从 Parquet / Iceberg
这类表格式迁到 Lance 的路径与 `migration` 指南，
这类迁移的输入输出都是文件、与运行中的服务无关，所以可以做双跑和校验。

LanceDB 有一个反方向的优势，会显著压低迁移成本：
**换 embedding 模型不需要迁移数据。** 传统向量库换模型要做一次
"DB migration"（Dosu 的公开案例里说，随着模型迭代，"从偶发变成了日常"），
而 LanceDB 的做法是 `add_columns` 加一列向量再回填——
原始数据一个字节都不用重写。前面章节讲过的分支实验模式
（从 `main` fork 出 `embed-minilm` 与 `embed-nomic` 两个分支、
各加一列向量、在固定评测集上比、只把赢的合并回 main）
就是这条路径的标准写法。

## 26.8 一个具体的集成代码示例：LangChain

下面的骨架是从官方文档的参数表推出来的，写的是本地路径的完整用法。

**这段代码本教程没有实际运行**，原因很直接：**本机没有安装
`langchain` / `langchain-community`**（这套环境里只装了
`lancedb` 0.40.0、`pyarrow`、`numpy`）。而且即使装上，
`LanceDB.from_documents(...)` 需要一个 `Embeddings` 实现，
官方示例用的是 `OpenAIEmbeddings`，**需要 API key**。
所以这一节给出的是经过核对参数表之后的代码骨架，
其中的输出与行为**均未经运行验证**。

```python
# 需要: pip install langchain langchain-community lancedb openai
from langchain_community.vectorstores import LanceDB
from langchain_openai import OpenAIEmbeddings          # 需要 OPENAI_API_KEY
from langchain_core.documents import Document

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

docs = [
    Document(page_content="Lance 为随机访问优化，Parquet 为扫描优化",
             metadata={"source": "ch03", "lang": "zh"}),
    Document(page_content="HNSW 在 LanceDB 里不是顶层索引类型",
             metadata={"source": "ch12", "lang": "zh"}),
]

# 1) 建库。注意默认参数：uri 是 /tmp/lancedb，表名是 vectorstore，
#    文本列是 text，向量列是 vector，返回条数默认 4。
docsearch = LanceDB.from_documents(
    docs,
    embeddings,
    uri="./langchain_db",
    table_name="tutorial_docs",
    distance="cosine",          # 默认是 l2，这里显式改成 cosine
)

# 2) 朴素检索。注意它**不返回 relevance score**，
#    要分数得用 similarity_search_with_relevance_scores。
hits = docsearch.similarity_search("LanceDB 的索引类型", k=3)
print(hits[0].page_content)

# 3) 拿到原生 LanceDB 表，做 LangChain 这层没暴露的事
table = docsearch.get_table()
table.create_index(
    index_type="IVF_PQ",
    vector_column_name="vector",
    metric="cosine",
    num_partitions=16,          # 不传的话 LangChain 会调它自己的默认 256
    num_sub_vectors=8,          # 1536 维的 1/16 是 96，小表上没必要
)

# 4) 建索引之后再加数据 —— 新行不在索引里，
#    index_stats 的 num_unindexed_rows 会告诉你这个事实
table.add([{"id": "99", "vector": embeddings.embed_query("新的一行"),
            "text": "新的一行"}])
print(table.index_stats("vector_idx"))
```

几个从官方参数表里核对出来的、容易踩的点：

**`uri` 的默认值是 `/tmp/lancedb`**，不是当前目录。不传它的话，
数据会跑到一个你可能没有权限、也不在备份范围里的地方。

**`mode` 默认是 `overwrite`。** 用 `LanceDB(...)` 建实例再 `add_texts`
时如果不注意，可能把已有表覆盖掉。

**`api_key` / `region` 只对 Enterprise 生效**，本地 API 不要传。
传了不会报错，但语义是错的。

**`create_index()` 的默认 `num_partitions=256`、`num_sub_vectors=96`
是 LangChain 这一层的默认值**，不是 LanceDB 的。
在一个只有几万行的表上按默认值建索引，会得到一个 256 个分区、
每分区一两百行的退化结构。**这一层适配器不替你做规模判断**，
规模判断永远在你手上。

**`similarity_search` 不返回分数。** 很多 RAG 流程需要分数做阈值或重排，
用错方法会得到一个静默缺了 `_relevance_score` 列的结果。

需要说明的是，**LanceDB 官方文档里已经不存在 `embedding_functions=`
这个参数了**（旧教程里常见），当前只有 `SourceField()` / `VectorField()`
这类 schema 级标记。照抄 2023–2024 年的博客会在这里踩坑。
完整的注册表用法、自动向量化的静默陷阱与迁移写法见第 22 章 §22.5，本节不重复。

## 26.9 怎么判断一个集成"真不真"

把这一章压成一个可操作的检查顺序：

**第一步，先问层。** 是 LanceDB（检索）还是 Lance（格式）？
Spark / Trino / Doris / Ray / Flink 属于后者，
LangChain / LlamaIndex / CrewAI 属于前者。
把"Lance 支持 Spark"写成"LanceDB 集成 Spark"是这一章最常见的错误归因。

**第二步，查 `docs.lancedb.com/llms.txt` 有没有独立页。**
有独立页 → 官方集成；只有一段介绍、没有参数表和代码 → 存疑；
完全没有 → 不是官方集成。

**第三步，看页面里有没有可核对的代码与参数表。**
官方维护的集成页会给出完整的参数表和默认值（LangChain、LlamaIndex、
dlt、Voxel51 都是这样）。只有一句"我们支持 LanceDB"的，是合作伙伴关系。

**第四步，看仓库属于哪个组织。** `lancedb/*` 或 `lance-format/*` = 一方；
其他地方 = 社区。

**第五步，分清方向。** 是"LanceDB 主动集成别人"还是
"别人读 Lance 格式"？**后者的数量远大于前者**，
所以看到"某某工具支持 Lance"时，先怀疑它是后者。

## 26.10 组织、治理与"该依赖谁"（摘要）

最后一件事和集成无关、但和长期依赖有关：**这套技术的组织和治理**，
一句话版是 —— **格式（Lance）由社区 PMC 治理，实现（LanceDB OSS）由公司控制**，
所以"我该把谁当长期依赖"的答案本身是分裂的：法定实体是 **Eto Labs, Inc.**
（dba LanceDB）、**YC W22** 批次，公司仓库搬过两次
（`eto-ai/lance` → `lancedb/lance` → **`lance-format/lance`**，
2025-11-18 宣布），Lance 格式有对标"The Apache Way"的 PMC 与否决权，
而 LanceDB OSS 侧官方原话是 "it doesn't, **at least not yet**"，
PMC 20 个席位里公司员工占 9 席。完整的融资记录、治理公告原文、发布流程，
以及"LanceDB 不在 VectorDBBench / ANN-Benchmarks 里、所以没有可横比的第三方数据"
这件事，**都在第 28 章 §28.6**（治理与基准缺失的完整版；基准对照的实验细节在第 27 章 §27.25），
本节只做指针，不重复。

## 26.11 语言 SDK 与前端生态

前面讲的集成几乎都是 Python 侧。三条语言线各有分工，值得单独说清。

**Rust 是上游。** `lancedb` crate 是原生实现，Python 与 TypeScript SDK 都是它的绑定。
Rust 侧能拿到最完整的能力（分支、tag、索引、namespace），
但**公共 API 仍标为实验状态**（出处见第 28 章 §28.7.5），
而且 `lance` / `lancedb` 两个 crate 与 Python 包的版本节奏不同步（第 28 章 §28.6）。

**TypeScript** 的官方包是 `@lancedb/lancedb`（npm）。它覆盖建表、向量 / FTS / hybrid
检索，以及和 Python 侧同名的 `VectorQuery` / `rerankHybrid` 接口。
**但 provider 专属的 reranker 只有 Python 版**（26.4 已经点过）：TS 侧只暴露通用
`Reranker` 接口和内置 `RRFReranker`，想在 TS 里用 Cohere 或 ColBERT 重排得自己实现。
TypeScript 目前**没有一个可核实的产品化 WASM / 浏览器构建**（核对过程见第 29 章 §29.3.3）。

**JS / 前端生态的适配** 分两条路。检索侧靠 LangChain JS
（`@langchain/community/vectorstores/lancedb`）与 LlamaIndex TS 的 LanceDB vector store，
两者都只是把上面那个 npm 包再包一层；应用层则常见于 Vercel AI SDK、Mastra 这类框架里
"自己调 `embed()` 生成向量、再把向量写进 LanceDB"的组合 ——
LanceDB 在这里提供的是一条纯文件的后端，不需要另起一个驻留的向量服务进程。

**一条要记住的话**：无论哪个 SDK，"嵌入式、无服务端"这个卖点都一样成立，
但**能力面不等价** —— 遇到 provider 专属重排、分支实验这类功能，
先确认你的语言 SDK 有没有暴露它，不要假设 Python 文档写了就到处都有。

## 26.12 AI 工程周边：MCP、评测与可观测性

这三块是"集成"里最容易被忽略、但在真实项目里天天用到的。

**MCP。** 官方维护了 LanceDB MCP Server（仓库 `lancedb/lancedb-mcp-server`），
给 LLM 三个工具：`ingest_docs`（把文档灌进表）、`query_table`（按查询串做向量检索、
返回 top-k）、`table_details`。社区侧还有一个 MIT 许可的 `lance-mcp`，
把"本地磁盘上的文档"直接变成 agentic RAG 的检索后端，不需要把数据传到云上 ——
这条对"数据不能出本机"的场景是有价值的。MCP 让 LanceDB 从"一个库"
变成"一个 LLM 能直接调用的工具"，和 26.3 讲的 `lancedb-agent-plugins` 是配套的两件事。

**评测框架。** 需要说清口径：**本教程没有找到 Ragas 或 TruLens 的官方 LanceDB 集成**
（未经确认，属于"没找到"而不是"不存在"）。这两类工具（RAG 评测、LLM 可观测）通常只要求
一个"能返回检索结果与分数"的检索器，所以接 LanceDB 一般是应用侧写几行适配代码的事 ——
但这属于应用代码，不是生态的一部分，引用时不要写成"官方集成"。

**可观测性。** 这一块有官方实现。`pip install "lancedb[otel]"` 之后，Python 侧调
`instrument_lancedb_metrics()`、TypeScript 侧调 `instrumentLanceDbMetrics()`，
就能把 LanceDB 的内部指标桥接到任意 OpenTelemetry 后端。指标覆盖对象存储请求数、
字节数、延迟、可重试错误与限流 —— **这些正是第 6 章讲的对象存储成本、
第 25 章讲的运维监控要对齐的东西**。实现上它是对 Rust `metrics` facade 的拉取式适配
（直方图按 Prometheus 风格导出 `_bucket` / `_count` / `_sum`，因为 OTel 没有异步直方图），
Rust 侧对应 `lancedb::metrics_otel` 模块，企业版集群另有一套服务端的 Prometheus / OTel 指标。
社区侧还有两套做客户端调用链自动埋点的包：Traceloop 的
`opentelemetry-instrumentation-lancedb`（属于 OpenLLMetry 项目），以及 Future AGI 的
traceAI 系列里的 `@traceai/lancedb`。

## 26.13 部署、管道与消费方

**部署与云市场。** 开源版是"库"，没有部署形态；企业版是 BYOC 或托管，
分布式部署走 KubeRay。需要如实标注：**没有找到官方公开的 Helm chart 或 Terraform
provider 页面，是否上架 AWS / GCP / Azure 云市场本轮也未确认** ——
和企业版的 region、SLA、价格一样，属于"要直接问厂商"的一类信息
（第 27 章 §27.26 有同样的缺口清单）。

**ETL 管道。** 现有的官方管道集成是 **dlt**（26.5 讲过的 destination）。
**Airbyte / Fivetran 没有官方 LanceDB destination**：Airbyte 可以靠自定义 destination
接入，但那属于社区或自建。这类工具读写的往往是"表"，而 LanceDB 在这条链路上的角色
更接近 dlt 那种"直接落成 Lance 数据集"的 destination。

**BI / 数仓消费方。** LanceDB 本身不做 BI。要做仪表盘的正确路径是**经过 DuckDB 的
`lance` 扩展**：DuckDB 把 Lance 数据集挂成表，SQL 层之上再接 Superset、Metabase、
Tableau 这类消费方。这条路径的代价是"BI 侧看到的是快照而不是实时检索结果"，
而且要求 DuckDB 能装 Lance 扩展（26.6 讲过的进程模型与版本语义归属问题同样适用）。
换句话说，**LanceDB 的 BI 能力是"借 DuckDB 的手"实现的**，不是它自己的卖点。

**身份 / 安全。** 这一块是**明确的缺口**：开源版没有 RBAC / SSO / 审计的现成实现
（2025-02-28 一位维护者在讨论区的原话是 "We haven't looked into any RBAC like
system yet"，见第 28 章 §28.6），嵌入式的访问控制就是文件系统权限本身。
企业版有 RBAC / SSO / 审计，但那属于商业能力。要在开源版上做多租户，
只能靠 namespace 隔离 + 操作系统层权限，或者干脆每个租户一份数据目录。

## 26.14 小结

**这一章只有一条主线：先分清层，再看真假。** LanceDB（检索层）的集成方式是
SDK 与框架适配类；Lance（格式层）的集成方式是别的引擎里实现的 reader / writer。
把"Lance 支持 Spark"写成"LanceDB 集成 Spark"是这一章最常见的错误归因。
判断真假最硬的依据是官方索引 `docs.lancedb.com/llms.txt` 里有没有独立页、
以及仓库属于哪个组织。

**官方集成的清单短而具体**：AI/Agent 框架、DataFrame 工具、embedding 注册表、
reranker、格式层连接器。被点名但没有官方页的（Haystack、Daft、Pathway、
MLflow/Airflow/Dagster、低代码平台、特征库）要如实标注为"官方没有"。

**默认值与参数要留神**：LangChain 那层的 `num_partitions=256`、`uri=/tmp/lancedb`、
`mode=overwrite` 都不是 LanceDB 的默认值，照抄会在小表上建出退化索引、
把数据写到备份范围之外。

**能力的边界在语言上**：Rust 最全但 API 实验、TypeScript 够用但缺 provider 专属重排、
Python 是事实上的完整体。周边生态里，MCP 与 OTel 有官方实现，BI 靠 DuckDB 借道，
RBAC / SSO / 审计是开源版的明确缺口。

**组织与治理是分裂的**：格式有社区 PMC，实现由公司控制。完整的公司史与
第三方基准缺失见第 28 章 §28.6 与第 27 章 §27.25。

下一章把 LanceDB 放进竞品里对比：它在这条赛道上赢什么、输什么，
以及在什么约束下该选别人。
