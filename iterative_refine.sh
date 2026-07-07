#!/bin/bash

set -euo pipefail

# ============ config ============
MAX_ITERATIONS=5
INITIAL_DESCRIPTION="${1:-}"

if [ -z "$INITIAL_DESCRIPTION" ]; then
    echo "prompt should not be empty"
    exit 1
fi

if [ -n "${2:-}" ]; then
    MAX_ITERATIONS="$2"
fi

echo "━━━ 迭代精炼 ━━━"
echo "  描述: $INITIAL_DESCRIPTION"
echo "  轮次: $MAX_ITERATIONS 轮"
echo ""

# ============ 统一循环 ============
for i in $(seq 1 "$MAX_ITERATIONS"); do

    echo "━━━ 第 ${i}/${MAX_ITERATIONS} 轮 ━━━"

    # 构造提示词
    if [ "$i" -eq 1 ]; then
        PROMPT="用户初始描述：${INITIAL_DESCRIPTION}

请根据上述描述生成完整的章节内容。为了章节更完美，可以适当的删减、增加和修改。
直接输出最终内容，并将内容保存到 result.md 文件中。"
    else
        PROMPT="请读取 result.md 中的内容，在此基础上进一步细化和完善，输出更详细、更结构化的版本。可以适当的删减、增加和修改，使章节更完美。
直接输出最终内容，并将内容保存到 result.md 文件中。"
    fi

    # 执行 Claude
    START_TS=$(date +%s)
    echo "$PROMPT" | claude -p --output-format text --effort high --dangerously-skip-permissions || {
        echo "❌ 第 $i 轮失败，退出" >&2
        exit 1
    }
    END_TS=$(date +%s)
    DURATION=$((END_TS - START_TS))

    echo "✔ 完成（${DURATION}s）"
    echo ""

done

echo "━━━ 迭代结束 ━━━"
echo "结果: ./result.md"
