"""MiniFlink 命令行：run / sql / version。"""

from __future__ import annotations

import argparse
import csv
import json
import re
import runpy
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .table import Schema, TableEnvironment


def _cmd_run(args: argparse.Namespace) -> int:
    path = Path(args.script)
    if not path.exists():
        print(f"脚本不存在: {path}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(path.parent.resolve()))
    try:
        runpy.run_path(str(path), run_name="__main__")
    except Exception as exc:
        print(f"作业执行失败: {exc}", file=sys.stderr)
        return 1
    return 0


_INT_RE = re.compile(r"-?(?:0|[1-9]\d*)$")
_FLOAT_RE = re.compile(r"-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?$")


def _coerce_csv_value(value: str) -> Any:
    """CSV 没有类型；对明显的整数/小数做推断，避免 WHERE/ORDER BY 按字符串比较。

    保留前导零（如 "007"）为字符串，避免把编号/邮编误转成数字。
    """
    text = value.strip()
    if text == "":
        return None
    if _INT_RE.fullmatch(text):
        return int(text)
    if _FLOAT_RE.fullmatch(text) and any(char in text for char in ".eE"):
        return float(text)
    return value


def _read_csv(path: str) -> tuple[list[dict], list[str]]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            raise ValueError("CSV 缺少表头")
        rows = [
            {key: _coerce_csv_value(value or "") for key, value in row.items()}
            for row in reader
        ]
        return rows, fieldnames


def _cmd_sql(args: argparse.Namespace) -> int:
    env = TableEnvironment()
    for spec in args.table or []:
        if "=" not in spec:
            print("--table 格式应为 name=path.csv", file=sys.stderr)
            return 2
        name, path = spec.split("=", 1)
        try:
            rows, fieldnames = _read_csv(path)
        except Exception as exc:
            print(f"读取 CSV 失败: {exc}", file=sys.stderr)
            return 1
        # 空表也要保留表头；非空表按整列推断类型。
        schema = Schema.from_names(fieldnames) if not rows else None
        env.from_elements(rows, schema=schema, name=name)
    if args.file:
        query = Path(args.file).read_text(encoding="utf-8")
    else:
        query = args.query
    if not query:
        print("必须提供 --query 或 --file", file=sys.stderr)
        return 2
    try:
        table = env.sql(query)
    except Exception as exc:
        print(f"SQL 失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(table.execute(), ensure_ascii=False, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miniflink", description="MiniFlink 教学运行时")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="运行一个 Python 作业文件")
    run.add_argument("script")
    run.set_defaults(func=_cmd_run)

    sql = subparsers.add_parser("sql", help="对 CSV 表执行 SQL 子集")
    sql.add_argument("--table", action="append", default=[], help="name=path.csv，可重复")
    sql.add_argument("--query", default="")
    sql.add_argument("--file", default="")
    sql.set_defaults(func=_cmd_sql)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
