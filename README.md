# iterative_refine.sh — 迭代精炼工具

对初始描述反复精炼，以上一轮输出为基础逐轮细化。

## 用法

```bash
./iterative_refine.sh "<初始描述>" [最大迭代次数]
```

| 参数 | 说明 | 必填 |
|---|---|---|
| `初始描述` | 任意文本描述 | 是 |
| `最大迭代次数` | 最多跑多少轮（默认 5） | 否 |

```bash
# 使用默认 5 轮
./iterative_refine.sh "你的描述"

# 指定跑 3 轮
./iterative_refine.sh "你的描述" 3
```

## 运行流程

```
第 1 轮:  初始描述           ──→  Claude 生成 → 保存到 result.md
第 2 轮:  读取 result.md     ──→  Claude 精炼 → 保存到 result.md
第 N 轮:  读取 result.md     ──→  Claude 精炼 → 保存到 result.md
```

每轮 Claude 自行读写 `result.md`，脚本只负责编排轮次和计时。

## 输出

最终结果保存在 `./result.md`。
