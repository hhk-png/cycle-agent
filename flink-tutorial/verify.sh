#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "== pytest =="
python -m pytest -q

echo "== examples =="
count=0
for file in examples/*.py; do
  python "$file" >/dev/null
  count=$((count + 1))
done
echo "examples passed: $count"

echo "== docs =="
python check_docs.py
