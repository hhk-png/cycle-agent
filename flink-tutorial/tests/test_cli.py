import json

from miniflink.cli import build_parser, main


def test_parser_requires_command():
    parser = build_parser()
    try:
        parser.parse_args([])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("应当要求子命令")


def test_sql_cli(tmp_path, capsys):
    path = tmp_path / "data.csv"
    path.write_text("name,amount\nalice,10\nbob,20\nalice,5\n", encoding="utf-8")
    code = main(
        [
            "sql",
            "--table",
            f"orders={path}",
            "--query",
            "SELECT name, SUM(amount) AS total FROM orders GROUP BY name ORDER BY name",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == [{"name": "alice", "total": 15}, {"name": "bob", "total": 20}]


def test_sql_cli_empty_csv_keeps_header(tmp_path, capsys):
    path = tmp_path / "empty.csv"
    path.write_text("name,amount\n", encoding="utf-8")
    code = main(["sql", "--table", f"t={path}", "--query", "SELECT * FROM t"])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_sql_cli_preserves_leading_zero_mixed_column(tmp_path, capsys):
    path = tmp_path / "codes.csv"
    path.write_text("code,amount\n8,9\n007,10\n", encoding="utf-8")
    code = main(
        [
            "sql",
            "--table",
            f"t={path}",
            "--query",
            "SELECT code FROM t ORDER BY code",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == [{"code": "007"}, {"code": "8"}]
