"""CLI 测试 —— ``minich`` 命令行入口与进程内 ``Client``。

CLI 的价值在于"一条命令跑通整条链路":``local`` 起一个进程内 Context、
``format`` 做格式转换、``benchmark`` 计时、``demo`` 自解释地跑一遍。
这些都是**用户第一次接触这个项目**会敲的命令,所以必须有回归测试。

``main(argv)`` 返回**退出码**而不是自己 ``sys.exit()`` —— 这让它可以被
直接当函数调用测试(也是文档里承诺的性质)。
"""

from __future__ import annotations

import io
import sys

import pytest

from minich.cli import build_parser, main


class _RunResult:
    __slots__ = ("code", "out", "err")

    def __init__(self, code, out, err):
        self.code = code
        self.out = out
        self.err = err


@pytest.fixture
def run_cli(monkeypatch):
    """在进程内调用 ``main(argv)`` 并捕获 stdout/stderr 与退出码。"""
    def _run(argv, stdin: str = ""):
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = main(argv)
        return _RunResult(code, out.getvalue(), err.getvalue())

    return _run


# ══════════════════════════════════════════════════════════════════════════
# 解析器
# ══════════════════════════════════════════════════════════════════════════


def test_build_parser_has_expected_subcommands():
    parser = build_parser()
    # 用 --help 的输出侧证子命令都在(不实际执行)
    help_text = parser.format_help()
    for cmd in ("client", "server", "local", "benchmark", "format", "status", "demo"):
        assert cmd in help_text


def test_no_command_prints_help_and_returns_zero(run_cli):
    r = run_cli([])
    assert r.code == 0
    assert "minich" in r.out


# ══════════════════════════════════════════════════════════════════════════
# local —— 一次性的进程内查询
# ══════════════════════════════════════════════════════════════════════════


def test_local_query_returns_zero_and_prints_value(run_cli):
    r = run_cli(["local", "-q", "SELECT 1"])
    assert r.code == 0
    assert r.out.strip() == "1"


def test_local_explicit_tsv_format(run_cli):
    r = run_cli(["local", "-q", "SELECT 1 + 1", "--format", "TSV"])
    assert r.code == 0
    assert r.out == "2\n"


def test_local_prettycompact_format(run_cli):
    r = run_cli(["local", "-q", "SELECT 42", "-f", "PrettyCompact"])
    assert r.code == 0
    assert "42" in r.out


def test_local_error_returns_nonzero_with_clickhouse_message(run_cli):
    r = run_cli(["local", "-q", "SELECT * FROM does_not_exist"])
    assert r.code == 1
    assert "UNKNOWN_TABLE" in r.out
    assert "Code: 60" in r.out


def test_local_multiquery_runs_several_statements(run_cli):
    r = run_cli([
        "local", "-n", "-q",
        "CREATE TABLE t (x UInt8) ENGINE = Memory; INSERT INTO t VALUES (7); SELECT x FROM t",
    ])
    assert r.code == 0
    assert "7" in r.out


def test_local_reads_sql_from_stdin(run_cli):
    r = run_cli(["local"], stdin="SELECT 123\n")
    assert r.code == 0
    assert r.out.strip() == "123"


def test_local_data_path_creates_mergetree_on_disk(run_cli, tmp_path):
    data_dir = tmp_path / "cli_data"
    r = run_cli([
        "local", "--data-path", str(data_dir), "-n", "-q",
        "CREATE TABLE t (id UInt64) ENGINE = MergeTree ORDER BY id; "
        "INSERT INTO t VALUES (1),(2); SELECT count() FROM t",
    ])
    assert r.code == 0
    assert r.out.strip() == "2"


def test_local_verbose_prints_query_id(run_cli):
    r = run_cli(["local", "-q", "SELECT 1", "--verbose"])
    assert r.code == 0
    assert "query_id" in r.out and "rows" in r.out


def test_local_time_prints_elapsed(run_cli):
    r = run_cli(["local", "-q", "SELECT 1", "--time"])
    assert r.code == 0
    assert "Elapsed" in r.out


# ══════════════════════════════════════════════════════════════════════════
# format —— 格式转换
# ══════════════════════════════════════════════════════════════════════════


def test_format_converts_tsv_to_jsoneachrow(run_cli, tmp_path):
    src = tmp_path / "in.tsv"
    src.write_bytes("1\ta\n2\tb\n".encode("utf-8"))
    r = run_cli([
        "format", "--in", "TSV", "--out", "JSONEachRow",
        "--structure", "id UInt8, name String", str(src),
    ])
    assert r.code == 0
    assert '{"id":1,"name":"a"}' in r.out
    assert '{"id":2,"name":"b"}' in r.out


def test_format_tsv_to_csv_roundtrip(run_cli, tmp_path):
    src = tmp_path / "in.tsv"
    src.write_bytes("x\ty\n".encode("utf-8"))
    r = run_cli([
        "format", "--in", "TSV", "--out", "CSV",
        "--structure", "a String, b String", str(src),
    ])
    assert r.code == 0
    assert "x" in r.out and "y" in r.out


# ══════════════════════════════════════════════════════════════════════════
# benchmark
# ══════════════════════════════════════════════════════════════════════════


def test_benchmark_runs_and_returns_zero(run_cli):
    r = run_cli(["benchmark", "-q", "SELECT 1", "--iterations", "2", "--warmup", "1"])
    assert r.code == 0
    # 输出里应包含某种统计(QPS / rows 之类)
    assert r.out  # 非空


def test_benchmark_requires_query(run_cli):
    r = run_cli(["benchmark"])
    assert r.code == 2
    assert "query" in r.err.lower()


# ══════════════════════════════════════════════════════════════════════════
# status / demo
# ══════════════════════════════════════════════════════════════════════════


def test_status_command_runs(run_cli):
    r = run_cli(["status"])
    assert r.code == 0
    assert r.out


def test_demo_command_runs(run_cli):
    r = run_cli(["demo"])
    assert r.code == 0
    assert r.out


# ══════════════════════════════════════════════════════════════════════════
# 进程内 Client
# ══════════════════════════════════════════════════════════════════════════


def test_client_local_in_process():
    from minich.client import Client

    client = Client.local()
    try:
        r = client.execute("SELECT 1 + 1 AS x")
        assert r.rows() == [(2,)]
        assert r.scalar() == 2
    finally:
        client.close()


def test_client_local_uses_data_path(tmp_path):
    from minich.client import Client

    client = Client.local(data_path=str(tmp_path / "cdata"))
    try:
        client.execute("CREATE TABLE t (id UInt64) ENGINE = MergeTree ORDER BY id")
        client.execute("INSERT INTO t VALUES (5), (6)")
        assert client.execute("SELECT sum(id) FROM t").scalar() == 11
    finally:
        client.close()
    # 目录里应当真的落了 part 文件(不是"没报错"就算过)
    part_files = list((tmp_path / "cdata").rglob("id.bin"))
    assert part_files, "MergeTree 应当把 part 的列文件落盘"


def test_client_error_propagates_as_minich_error():
    from minich.client import Client
    from minich.errors import MiniChError

    client = Client.local()
    try:
        with pytest.raises(MiniChError) as ei:
            client.execute("SELECT * FROM nope")
        assert ei.value.code == 60   # UNKNOWN_TABLE
    finally:
        client.close()
