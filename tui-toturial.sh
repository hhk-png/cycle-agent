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
for i in $(seq 3 "$MAX_ITERATIONS"); do

    echo "━━━ 第 ${i}/${MAX_ITERATIONS} 轮 ━━━"

    # 构造提示词
    if [ "$i" -eq 1 ]; then
        PROMPT="用户初始描述：${INITIAL_DESCRIPTION}

请根据上述描述生成完整的教程。为了教程更完美，可以适当的删减、增加和修改章节和章节的内容。
直接输出最终内容，并将内容保存到 tui-toturial 目录下。
只能操作 tui-toturial 目录下的文件，不能操作其他目录下的文件。"
    else
        PROMPT="用户初始描述：${INITIAL_DESCRIPTION}
请读取 tui-toturial 目录下的文件章节内容，在此基础上结合用户的输入进一步细化和完善，输出更详细的版本。可以适当的删减、增加和修改章节和章节的内容，使教程更完美，章节所覆盖的内容更完善。
直接输出最终内容，并将内容保存到 tui-toturial目录下。
每次先检查章节，看有没有可以增加和删除、修改的章节，如果有，可以对章节进行操作。
只能操作 tui-toturial 目录下的文件，不能操作其他目录下的文件。"
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
