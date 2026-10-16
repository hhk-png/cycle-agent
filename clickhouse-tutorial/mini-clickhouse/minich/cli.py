"""命令行界面 —— ``clickhouse-client`` / ``clickhouse-local`` / ``clickhouse-benchmark`` 的迷你版。

真实的 ClickHouse 发布物是一堆**独立的可执行程序**,而在新版本里它们被收进
了一个统一的入口 ``clickhouse``(所以你现在既可以用 ``clickhouse-client``,
也可以用 ``clickhouse client``):

.. code-block:: text

   真实程序                        minich 子命令        干什么
   ─────────────────────────────  ──────────────────  ───────────────────────────────
   clickhouse-client              minich client        连服务端,开 REPL 或跑 -q
   clickhouse-server              minich server        起 TCP 服务端(9000)
   clickhouse-local               minich local         不起服务端,直接跑一条查询
   clickhouse-benchmark           minich benchmark     反复跑一条查询并报 QPS/延迟
   clickhouse-format(近似)         minich format        格式转换(TSV → JSONEachRow …)
   (无对应)                        minich status        看一眼"服务端"概览
   (无对应)                        minich demo          自包含的教学演示

**本模块的三条设计原则**

1. **可非交互使用**。测试与脚本要能 ``python -m minich local -q "SELECT 1"``
   直接拿到 ``1``。所以"输出"是 :class:`QueryResult` 的文本渲染,而不是
   一堆带提示符的打印。
2. **库不许 ``sys.exit()``**。:func:`main` 返回**退出码**;只有它自己被
   argparse 的 ``--help`` / 解析失败带着抛 ``SystemExit``。这样嵌到别的
   程序里也不会被"半路退出"坑到。
3. **Windows 安全**。不假设 ANSI 转义可用(看 ``TERM`` 与 ``isatty``);
   打印表格前统一 ``reconfigure`` 成 UTF-8,免得中文/框线在 GBK 控制台上炸。

.. note::

   ``-h`` 的归属是个**刻意选择**:真实 ``clickhouse-client`` 里 ``-h`` 是
   ``--host``、帮助只有 ``--help``。这里**顶层** ``minich -h`` 显示帮助
   (符合"没有上下文时 -h 就该是帮助"的直觉),而**子命令**里 ``-h`` 又是
   主机名(符合真实客户端)。两处都写出来,是为了让读者知道这不是笔误。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from typing import Any, List, Optional, Sequence, Tuple

from . import __version__, TARGET_CLICKHOUSE_VERSION
from .client import DEFAULT_PORT, Client, split_statements
from .errors import MiniChError
from .execution.context import QueryResult

__all__ = ["main"]

#: 真实客户端的默认输出格式。
DEFAULT_FORMAT = "PrettyCompact"

#: 需要写**字节**而不是文本的格式(输出侧)。
_BINARY_FORMATS = frozenset(
    {"RowBinary", "RowBinaryWithNames", "RowBinaryWithNamesAndTypes", "Native"}
)


# ══════════════════════════════════════════════════════════════════════════
# 终端适配
# ══════════════════════════════════════════════════════════════════════════


def _configure_stdout() -> None:
    """把 stdout 调成 UTF-8 + 替换非法字节 —— 打印框线表/中文前的第一件事。

    Windows 的控制台历史上默认是 GBK(``cp936``),``↔`` 或框线字符
    ``┌─┐`` 会直接 ``UnicodeEncodeError``。``errors="replace"`` 保证
    最坏情况下也只是打出 `?`,而不是整个命令崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):  # pragma: no branch - Py3.7+ 都有
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 非常老的流
                pass


def _supports_ansi() -> bool:
    """能不能安全地输出 ANSI 转义?

    三个条件都满足才敢用:``stdout`` 是终端(不是管道/文件)、``TERM``
    被设成了有意义的值(Windows 上常常根本没有 ``TERM``)、用户没设
    ``NO_COLOR``(事实标准:设了就禁用颜色)。
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") in (None, "", "dumb"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):  # pragma: no cover
        return False


def _emit(text: str) -> None:
    """打印一段结果文本 —— 必要时补一个换行,避免两段输出粘在一起。"""
    if text and not text.endswith("\n"):
        text += "\n"
    sys.stdout.write(text)
    sys.stdout.flush()


def _default_output_format() -> str:
    """没有 ``--format`` 时用哪个格式 —— **看输出是不是终端**。

    .. code-block:: text

       输出到终端(isatty)      → PrettyCompact   给人看的框线表
       输出被重定向/管道        → TSV             给脚本/测试用的裸值

    真实 ``clickhouse-client`` 默认永远是 PrettyCompact。这里对**被重定向**
    的情况改默认成 ``TSV``,是一个**刻意的可用性取舍**:它让
    ``python -m minich local -q "SELECT 1"`` 直接打印 ``1``(而不是一张
    带框线的表),于是整条 CLI 天然可被 shell 脚本与测试消费。
    想看框线表照样可以显式 ``-f PrettyCompact``,或在终端里交互使用。
    """
    try:
        if sys.stdout.isatty():
            return DEFAULT_FORMAT
    except (AttributeError, ValueError):  # pragma: no cover
        pass
    return "TSV"


# ══════════════════════════════════════════════════════════════════════════
# 连接:把命令行参数变成一个 Client
# ══════════════════════════════════════════════════════════════════════════


def _make_client(args: argparse.Namespace) -> Client:
    """按参数建 Client:给了 ``--host``/``--port`` 就走 socket,否则 in-process。

    ``--host`` 与 ``--port`` 的默认值都是 ``None``(而不是 ``localhost``/9000)
    —— 只有这样才分得清"用户没提"和"用户说了 localhost"。没提 → 用
    in-process(不需要你先把服务端起起来);提了 → 走真正的网络协议。
    """
    database = getattr(args, "database", None) or "default"
    user = getattr(args, "user", None) or "default"
    password = getattr(args, "password", None) or ""
    host = getattr(args, "host", None)
    port = getattr(args, "port", None)
    if host is not None or port is not None:
        return Client.connect(
            host=host or "localhost", port=port or DEFAULT_PORT,
            database=database, user=user, password=password,
        )
    return Client.local(
        database=database, user=user, data_path=getattr(args, "data_path", None)
    )


# ══════════════════════════════════════════════════════════════════════════
# 查询执行 + 输出
# ══════════════════════════════════════════════════════════════════════════


def _render(result: QueryResult, fmt_override: Optional[str]) -> str:
    """按 ``--format`` 渲染结果。**覆盖值优先于查询自身的 FORMAT 子句。**"""
    return result.text(fmt_override) if fmt_override else result.text()


def _statement_has_format(sql: str) -> bool:
    """这段 SQL 自己带了 ``FORMAT`` 子句吗(解析不出来就当没有)。"""
    from .sql import parse

    try:
        node = parse(sql)
    except MiniChError:
        return False
    return getattr(node, "format", None) is not None


def _pick_format(sql: str, args: argparse.Namespace) -> Optional[str]:
    """决定这条结果用哪个格式渲染 —— 优先级从高到低。

    .. code-block:: text

       ① --format 显式给了           → 用它(它**赢过**查询自己的 FORMAT)
       ② 查询自己写了 FORMAT          → 用查询的(返回 None,交给 QueryResult)
       ③ 都没有                       → tty 感知默认(PrettyCompact / TSV)

    第 ① 条正是任务要求的"--format override that wins over the query's own
    FORMAT";第 ② 条保证没给 --format 时**不篡改**查询作者的意图;
    第 ③ 条只在两者都缺席时才兜底。
    """
    if getattr(args, "format", None):
        return args.format
    if _statement_has_format(sql):
        return None
    return _default_output_format()


def _execute_and_print(
    client: Client,
    sql: str,
    args: argparse.Namespace,
    fmt_override: Optional[str] = None,
    interactive: bool = False,
) -> bool:
    """执行一条语句并打印结果。返回 ``True`` 表示成功。

    ``interactive=True``(REPL 里)时,错误**不抛出**而是打印出来,
    让会话继续 —— 这正是真实 ``clickhouse-client`` 的行为:敲错一条
    SQL 不该把你踹出客户端。

    ``fmt_override`` 是 REPL 当前生效的格式(可能来自 ``-f`` 或 ``\\G``):
    给了就当成"显式覆盖";没给则按 :func:`_pick_format` 的优先级决定。
    """
    fmt = fmt_override if fmt_override is not None else _pick_format(sql, args)
    try:
        start = time.perf_counter()
        result = client.execute(sql)
        text = _render(result, fmt)
        elapsed = time.perf_counter() - start
    except MiniChError as exc:
        _print_error(exc, getattr(args, "stacktrace", False))
        return False
    except Exception as exc:  # pragma: no cover - 兜底
        _print_error(exc, getattr(args, "stacktrace", False))
        return False

    _emit(text)
    if getattr(args, "time", False):
        sys.stdout.write(f"Elapsed: {elapsed:.3f} sec.  "
                         f"Processed {result.row_count()} rows.\n")
    if getattr(args, "print_profile_events", False):
        events = {k: v for k, v in result.profile_events().items() if v}
        if events:
            for name, value in sorted(events.items()):
                sys.stdout.write(f"ProfileEvent\t{name}\t{value}\n")
    if getattr(args, "verbose", False):
        sys.stdout.write(
            f"query_id: {result.query_id}  rows: {result.row_count()}  "
            f"columns: {len(result.column_names())}\n"
        )
    return True


def _print_error(exc: Exception, stacktrace: bool = False) -> None:
    """按真实客户端的样式打印错误 —— ``Code: N. DB::Exception: …. (NAME)``。"""
    if isinstance(exc, MiniChError):
        sys.stdout.write(str(exc) + "\n")
    else:  # pragma: no cover
        sys.stdout.write(f"{type(exc).__name__}: {exc}\n")
    if stacktrace:
        import traceback

        traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════
# 子命令:client(REPL)
# ══════════════════════════════════════════════════════════════════════════

_REPL_HELP = """\
\\?              show this help
\\q              quit
\\l              list databases
\\dt             list tables in the current database
\\d [table]      describe a table (or list tables if no name given)
\\G              switch the default output format to Vertical
\\g              execute the pending multi-line statement
--multiquery    allow several ;-separated statements on one line
--multiline     accumulate lines until one ends with ';'
"""


def _run_client_command(args: argparse.Namespace) -> int:
    """``minich client`` —— REPL、``-q`` 单发,或 ``-n`` 读脚本。"""
    client = _make_client(args)
    try:
        if args.query:
            return _run_query_text(client, args)
        if not sys.stdin.isatty():
            return _run_piped(client, args)
        return _run_repl(client, args)
    finally:
        client.close()


def _run_query_text(client: Client, args: argparse.Namespace) -> int:
    """``-q "SELECT …"`` —— 一条(或 ``-n`` 下多条)语句后退出。"""
    statements = split_statements(args.query) if args.multiquery else [args.query]
    ok = True
    for statement in statements:
        ok = _execute_and_print(client, statement, args) and ok
    return 0 if ok else 1


def _run_piped(client: Client, args: argparse.Namespace) -> int:
    """stdin 不是终端(被管道/重定向) —— 当成一个脚本读完执行。

    这正是 ``printf 'SELECT 1;\\nSELECT 2\\n' | python -m minich client -n``
    这条用法的落地:``-n`` 允许一个输入里有多条语句。
    """
    text = sys.stdin.read()
    statements = split_statements(text) if args.multiquery else [text.strip()]
    ok = True
    for statement in statements:
        if not statement.strip():
            continue
        if not _execute_and_print(client, statement, args):
            ok = False
            break  # 与 execute_script 一致:遇到第一个错误就停
    return 0 if ok else 1


def _run_repl(client: Client, args: argparse.Namespace) -> int:
    """交互式 REPL —— 提示符 ``hostname :)``,多行模式 ``;`` 收尾。"""
    _configure_stdout()
    hostname = _short_hostname()
    #: REPL 当前生效的格式。默认 ``None`` = "没显式指定",于是每条查询按
    #: :func:`_pick_format` 自己决定(查询带 FORMAT 就用查询的);``\G`` 或
    #: ``-f`` 会把它设成一个具体值,从此覆盖所有后续查询。
    default_format = args.format
    buffer = ""
    while True:
        prompt = f"{hostname} :) " if not buffer else f"{hostname} :-] "
        try:
            line = input(prompt)
        except EOFError:
            # Ctrl-D —— 干净退出(真实客户端打一个换行再退出)
            sys.stdout.write("\nBye.\n")
            return 0
        except KeyboardInterrupt:
            # Ctrl-C —— **取消当前正在输入的那条语句**,不是退出进程
            sys.stdout.write("\n")
            buffer = ""
            continue

        stripped = line.strip()

        # ── 反斜杠命令(空缓冲时才当命令;\\g/\\G 任何时候都认) ───────────
        if stripped in ("\\q", "\\quit", "exit", "quit"):
            return 0
        if stripped in ("\\?", "\\help"):
            sys.stdout.write(_REPL_HELP)
            continue
        if stripped == "\\G":
            default_format = "Vertical"
            sys.stdout.write("Default format set to Vertical.\n")
            continue
        if stripped == "\\g":
            if buffer.strip():
                _execute_and_print(client, _strip_semicolon(buffer), args, default_format,
                                   interactive=True)
                buffer = ""
            else:
                sys.stdout.write("Nothing to execute.\n")
            continue
        if not buffer and stripped.startswith("\\"):
            _handle_backslash_meta(client, stripped, args, default_format)
            continue

        # ── 累积 / 执行 ────────────────────────────────────────────────
        if args.multiline:
            buffer = line if not buffer else buffer + "\n" + line
            if buffer.rstrip().endswith(";"):
                statement = _strip_semicolon(buffer)
                buffer = ""
                if statement.strip():
                    _exec_repl(client, statement, args, default_format)
            continue

        # 非多行模式:一行一条(可选 -n 允许一行里多条)
        statement = _strip_semicolon(line)
        if not statement.strip():
            continue
        if args.multiquery:
            for part in split_statements(line):
                _exec_repl(client, part, args, default_format)
        else:
            _exec_repl(client, statement, args, default_format)


def _exec_repl(client: Client, statement: str, args: argparse.Namespace, fmt: str) -> None:
    """REPL 里执行一条语句 —— 失败也**不让会话结束**。"""
    _execute_and_print(client, statement, args, fmt, interactive=True)


def _handle_backslash_meta(
    client: Client, stripped: str, args: argparse.Namespace, fmt: str
) -> None:
    """处理 ``\\l`` / ``\\dt`` / ``\\d`` 这类元命令 —— 它们只是 SQL 的糖。"""
    if stripped == "\\l":
        _execute_and_print(client, "SHOW DATABASES", args, fmt, interactive=True)
        return
    if stripped == "\\dt":
        _execute_and_print(client, "SHOW TABLES", args, fmt, interactive=True)
        return
    if stripped == "\\d" or stripped.startswith("\\d "):
        target = stripped[2:].strip()
        if target:
            _execute_and_print(client, f"DESCRIBE TABLE {target}", args, fmt, interactive=True)
        else:
            _execute_and_print(client, "SHOW TABLES", args, fmt, interactive=True)
        return
    sys.stdout.write(f"Unknown backslash command: {stripped}\n{_REPL_HELP}")


def _strip_semicolon(text: str) -> str:
    """去掉语句末尾的那个分号(REPL 用它当"执行"的终止符)。"""
    text = text.strip()
    if text.endswith(";"):
        text = text[:-1]
    return text


def _short_hostname() -> str:
    """提示符里用的短主机名(取第一个点之前的部分,和真实客户端一致)。"""
    try:
        name = socket.gethostname() or "localhost"
    except OSError:  # pragma: no cover
        name = "localhost"
    return name.split(".")[0]


# ══════════════════════════════════════════════════════════════════════════
# 子命令:server
# ══════════════════════════════════════════════════════════════════════════


def _run_server_command(args: argparse.Namespace) -> int:
    """``minich server`` —— 起 TCP 服务端并阻塞。"""
    host = args.listen or "0.0.0.0"
    port = args.port if args.port is not None else DEFAULT_PORT
    server = Client.create_server(
        host=host, port=port, data_path=args.data_path, max_threads=args.max_threads
    )
    actual_port = server.server_address[1]
    sys.stdout.write(
        f"mini-clickhouse server listening on {host}:{actual_port}"
        + (f"  data_path={args.data_path}" if args.data_path else "  (in-memory)")
        + "\nPress Ctrl-C to stop.\n"
    )
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nShutting down.\n")
    finally:
        server.shutdown()
        server.server_close()
        server.context.shutdown()
    return 0


# ══════════════════════════════════════════════════════════════════════════
# 子命令:local
# ══════════════════════════════════════════════════════════════════════════


def _run_local_command(args: argparse.Namespace) -> int:
    """``minich local`` —— 起一个进程内 Context,跑完就退(不开端口)。"""
    client = Client.local(database=args.database, data_path=args.data_path)
    try:
        if args.query:
            statements = split_statements(args.query) if args.multiquery else [args.query]
        elif not sys.stdin.isatty():
            text = sys.stdin.read()
            statements = split_statements(text) if args.multiquery else [text.strip()]
        else:
            statements = []
        if not statements:
            sys.stderr.write("minich local: nothing to run (use -q or pipe SQL in)\n")
            return 2
        ok = True
        for statement in statements:
            if not statement.strip():
                continue
            ok = _execute_and_print(client, statement, args) and ok
        return 0 if ok else 1
    finally:
        client.close()


# ══════════════════════════════════════════════════════════════════════════
# 子命令:benchmark
# ══════════════════════════════════════════════════════════════════════════


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """线性插值分位数 —— 与 ClickHouse 的 ``quantile()`` 默认算法同源。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def _run_benchmark_command(args: argparse.Namespace) -> int:
    """``minich benchmark`` —— 热身 + 计时循环,报 QPS/RPS/MiB/s 与分位数。"""
    query = args.query
    if not query:
        sys.stderr.write("minich benchmark: --query/-q is required\n")
        return 2

    timings: List[float] = []
    rows_total = [0]
    bytes_total = [0]
    errors: List[str] = []
    lock = threading.Lock()

    def worker() -> None:
        client = _make_client(args)
        try:
            for _ in range(args.warmup):
                try:
                    client.execute(query)
                except MiniChError:
                    pass
            for _ in range(args.iterations):
                start = time.perf_counter()
                try:
                    result = client.execute(query)
                    elapsed = time.perf_counter() - start
                    with lock:
                        timings.append(elapsed)
                        rows_total[0] += result.row_count()
                        bytes_total[0] += int(result.info.read_bytes or 0)
                except MiniChError as exc:
                    with lock:
                        errors.append(str(exc))
                    break
        finally:
            client.close()

    wall_start = time.perf_counter()
    if args.concurrency <= 1:
        worker()
    else:
        threads = [threading.Thread(target=worker) for _ in range(args.concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    wall = time.perf_counter() - wall_start

    if errors:
        sys.stderr.write("benchmark query failed:\n  " + errors[0] + "\n")
        return 1

    n = len(timings)
    if n == 0:
        sys.stderr.write("benchmark: no iterations completed\n")
        return 1

    total_rows = rows_total[0]
    total_bytes = bytes_total[0]
    qps = n / wall if wall > 0 else 0.0
    rps = total_rows / wall if wall > 0 else 0.0
    mib = (total_bytes / (1024 * 1024)) / wall if wall > 0 else 0.0
    ordered = sorted(timings)
    rows_per_query = total_rows / n if n else 0

    lines = [
        "mini-clickhouse benchmark",
        f"  query:        {query}",
        f"  iterations:   {n} (warmup {args.warmup}, concurrency {args.concurrency})",
        f"  total time:   {wall:.3f} s   ({rows_per_query:.0f} rows/query)",
        "  " + "-" * 46,
        f"  QPS:          {qps:12.2f}   queries/second",
        f"  RPS:          {rps:12.2f}   rows/second (rows in the result set)",
        f"  MiB/s:        {mib:12.2f}",
        "  " + "-" * 46,
        "  latency (ms) by percentile:",
    ]
    for label, pct in (("50%", 0.50), ("90%", 0.90), ("95%", 0.95), ("99%", 0.99)):
        lines.append(f"    {label:>4}:  {_percentile(ordered, pct) * 1000:9.3f}")
    lines.append(f"    min:   {ordered[0] * 1000:9.3f}")
    lines.append(f"    max:   {ordered[-1] * 1000:9.3f}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# 子命令:format
# ══════════════════════════════════════════════════════════════════════════


def _structure_block(structure: Optional[str]) -> Any:
    """把 ``--structure "id UInt64, name String"`` 解析成一个零行 Block。

    实现小技巧:把这个片段塞进一句 ``CREATE TABLE`` 里,借**真正的建表
    解析器**把列名与类型解析出来 —— 于是 ``--structure`` 支持的类型语法
    与建表完全一致(``Array(Nullable(UInt8))`` 之类都能写),不用再写一份
    "类型名解析器"。
    """
    from .block import Block, ColumnWithTypeAndName
    from .sql import parse
    from .types import get_data_type

    if not structure:
        return Block()
    node = parse(f"CREATE TABLE _fmt ({structure}) ENGINE = Memory")
    columns = [
        ColumnWithTypeAndName(col.name, get_data_type(col.type_name))
        for col in node.columns
    ]
    return Block(columns)


def _run_format_command(args: argparse.Namespace) -> int:
    """``minich format --in TSV --out JSONEachRow file.tsv`` —— 格式转换。

    这是**调试格式**最顺手的小工具:想知道 ``TSV`` 里的转义到底对不对、
    ``JSONEachRow`` 会不会丢 NULL,直接转一遍看输出就行,不用起服务端。
    """
    from .formats import get_input_format, get_output_format

    # ── 读输入 ────────────────────────────────────────────────────────
    if args.file and args.file != "-":
        with open(args.file, "rb") as fp:
            raw = fp.read()
    else:
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()

    header = _structure_block(args.structure)
    in_fmt = get_input_format(args.in_format)
    blocks = list(in_fmt.read(raw, header))

    # ── 写输出 ────────────────────────────────────────────────────────
    out_fmt = get_output_format(args.out_format)
    out_header = blocks[0].clone_empty() if blocks else header
    settings = None
    if args.out_format in _BINARY_FORMATS:
        buf = bytearray()
        out_fmt.write_prefix(out_header, buf, settings)
        for block in blocks:
            out_fmt.write(block, buf, settings)
        out_fmt.write_suffix(out_header, buf, settings)
        out = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
        out.write(bytes(buf))
        out.flush()
    else:
        parts: List[str] = []
        writer = _StringSink(parts)
        out_fmt.write_prefix(out_header, writer, settings)
        for block in blocks:
            out_fmt.write(block, writer, settings)
        out_fmt.write_suffix(out_header, writer, settings)
        _emit("".join(parts))
    return 0


class _StringSink:
    """一个最小的文本汇 —— 格式输出器只要有 ``write(str)`` 就行。"""

    __slots__ = ("_parts",)

    def __init__(self, parts: List[str]) -> None:
        self._parts = parts

    def write(self, text: str) -> int:
        self._parts.append(text)
        return len(text)


# ══════════════════════════════════════════════════════════════════════════
# 子命令:status / demo
# ══════════════════════════════════════════════════════════════════════════


def _count_value(client: Client, sql: str) -> str:
    """跑一条计数/求和查询,返回要显示的字符串。

    两种情况都要兜住:

    * **查询失败**(``system.parts`` 还没实现之类)→ ``"n/a"``;
    * **成功但没有任何行** —— ``count()`` 在空源上当下一版实现里可能返回
      "零行结果"而不是"一行 0"。对计数类查询来说,零行就等价于 0,
      这里显式补上,免得 ``status`` 莫名其妙地显示 ``n/a``。
    """
    try:
        rows = client.execute(sql).rows()
    except MiniChError:
        return "n/a"
    if not rows or rows[0][0] is None:
        return "0"
    return _fmt_num(rows[0][0])


def _run_status_command(args: argparse.Namespace) -> int:
    """``minich status`` —— 从 ``system.*`` 拼一个"服务端概览"。"""
    client = _make_client(args)
    try:
        rows: List[Tuple[str, str]] = [
            ("databases", _count_value(client, "SELECT count() FROM system.databases")),
            ("tables", _count_value(client, "SELECT count() FROM system.tables")),
            ("parts", _count_value(client, "SELECT count() FROM system.parts")),
            ("rows (best-effort)", _count_value(client, "SELECT sum(total_rows) FROM system.parts")),
            ("columns", _count_value(client, "SELECT count() FROM system.columns")),
            ("active queries", _count_value(client, "SELECT count() FROM system.processes")),
            ("logged queries", _count_value(client, "SELECT count() FROM system.query_log")),
        ]
        # uptime 只有 in-process 客户端能直接读(服务端不在同一个进程里)
        if client.context is not None:
            rows.append(("uptime", f"{client.context.uptime()} s"))
        else:
            rows.append(("uptime", "n/a (remote)"))
        rows.append(("server", "in-process" if client.context is not None
                     else f"{client.host}:{client.port}"))
        _print_box("mini-clickhouse status", rows)
        return 0
    finally:
        client.close()


def _run_demo_command(args: argparse.Namespace) -> int:
    """``minich demo`` —— 一段自包含的教学导览。

    README 会让读者先跑这个。所以它必须:**自己建表、自己插数、
    每一步都把 SQL 打出来、并且在关键处展示"列存里才有意义"的数字**
    (行数、part 数、``GROUP BY`` 的结果)。所有查询都用同一个
    in-process Context,结束即销毁。
    """
    import shutil
    import tempfile

    # 用**每次新建的临时目录**当 data_path:MergeTree 默认会把 part 写到
    # 系统临时目录下(以 database/table 命名),如果不隔离,反复跑 demo
    # 会把上一次的数据读进来,行数越滚越多。给一个干净的临时目录,
    # 结束时连目录一起删掉 —— demo 就变成幂等的了。
    data_dir = tempfile.mkdtemp(prefix="minich-demo-")
    client = Client.local(database="default", data_path=data_dir)
    ctx = client.context
    assert ctx is not None
    fmt = args.format or DEFAULT_FORMAT

    def step(title: str) -> None:
        sys.stdout.write(f"\n{'-' * 68}\n== {title}\n{'-' * 68}\n")

    def run(sql: str, note: str = "") -> QueryResult:
        sys.stdout.write(f"\nSQL> {sql}\n")
        if note:
            sys.stdout.write(f"     ({note})\n")
        result = client.execute(sql)
        _emit(_render(result, fmt))
        return result

    try:
        sys.stdout.write(
            "mini-clickhouse demo —— 建表 → 插入 → 查询,一步一步来\n"
            f"(target ClickHouse {TARGET_CLICKHOUSE_VERSION}, minich {__version__})\n"
        )

        step("1. 建一张 MergeTree 表")
        run(
            "CREATE TABLE hits ("
            "event_time DateTime, url String, status UInt16, bytes UInt64"
            ") ENGINE = MergeTree ORDER BY event_time",
            "MergeTree 是 ClickHouse 的招牌引擎:数据按 ORDER BY 有序落盘",
        )

        step("2. 流式插入 10000 行(生成器,边生成边写)")
        import datetime as _dt

        base = _dt.datetime(2024, 1, 1)

        def rows():
            for i in range(10000):
                yield (
                    base + _dt.timedelta(seconds=i),
                    "/page/" + str(i % 100),
                    (200, 404, 500)[i % 3],
                    (i * 37) % 4096,
                )

        inserted = client.insert("hits", rows())
        sys.stdout.write(f"inserted {inserted} rows via a generator (not materialized)\n")

        table = ctx.catalog.get_table("default", "hits")
        sys.stdout.write(
            f"row count (from metadata, cheap): {table.row_count()}\n"
            f"part count (MergeTree writes one part per INSERT block): "
            f"{table.engine.part_count()}\n"
        )

        step("3. 计数与聚合")
        run("SELECT count() AS n, sum(bytes) AS total_bytes, uniq(url) AS urls FROM hits")
        run(
            "SELECT status, count() AS hits, round(avg(bytes), 1) AS avg_bytes "
            "FROM hits GROUP BY status ORDER BY status",
            "GROUP BY 是列存的主场:只读 status/bytes 两列",
        )

        step("4. 表函数与表达式(numbers 表,不落盘)")
        run("SELECT number, number * number AS sq FROM numbers(5)")
        run(
            "SELECT k, count() AS n FROM (SELECT number % 3 AS k FROM numbers(12)) "
            "GROUP BY k ORDER BY k",
            "numbers(N) 是一张凭空产生的表,常用于造测试数据",
        )

        step("5. 看一张 system 表")
        run("SELECT database, name, engine FROM system.tables ORDER BY name")

        sys.stdout.write("\nDemo complete. Try: python -m minich client\n")
        return 0
    finally:
        client.close()
        shutil.rmtree(data_dir, ignore_errors=True)


def _fmt_num(value: Optional[Any]) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):  # pragma: no cover
        return str(value)


def _print_box(title: str, rows: Sequence[Tuple[str, str]]) -> None:
    """画一个两列的小框 —— 不依赖任何外部库,纯框线字符。

    宽度是**算出来的**:先看最长的一对 key/value,再保证标题放得下。
    这样不管度量名多长,右边框都对齐。
    """
    key_w = max((len(k) for k, _ in rows), default=1)
    val_w = max((len(v) for _, v in rows), default=1)
    inner = key_w + val_w + 3  # "│ " + key + value + " │"
    inner = max(inner, len(title) + 4)
    top = "┌─ " + title + " " + "─" * (inner - len(title) - 3) + "┐"
    bottom = "└" + "─" * inner + "┘"
    sys.stdout.write(top + "\n")
    for key, value in rows:
        sys.stdout.write("│ " + key.ljust(key_w) + " " + value.rjust(val_w) + " │\n")
    sys.stdout.write(bottom + "\n")


# ══════════════════════════════════════════════════════════════════════════
# argparse
# ══════════════════════════════════════════════════════════════════════════


def _add_common_output_args(parser: argparse.ArgumentParser, with_format: bool = True) -> None:
    if with_format:
        parser.add_argument(
            "-f", "--format", default=None,
            help=f"output format (default: {DEFAULT_FORMAT}); overrides the query's own FORMAT",
        )
    parser.add_argument("--time", action="store_true", help="print query elapsed time")
    parser.add_argument("--verbose", action="store_true", help="print extra diagnostics")
    parser.add_argument("--stacktrace", action="store_true", help="print a Python stack trace on error")
    parser.add_argument(
        "--print-profile-events", action="store_true", dest="print_profile_events",
        help="print ProfileEvents counters after each query",
    )


def _add_connection_args(parser: argparse.ArgumentParser, short_host: bool = True) -> None:
    flags = ("-h", "--host") if short_host else ("--host",)
    parser.add_argument(*flags, default=None, dest="host",
                        help="server host (omit for in-process mode)")
    parser.add_argument("--port", type=int, default=None, help=f"server port (default {DEFAULT_PORT})")
    parser.add_argument("-u", "--user", default="default", help="user name")
    parser.add_argument("--password", default="", help="password (not verified by mini-clickhouse)")
    parser.add_argument("-d", "--database", default="default", help="default database")


def _subparser(sub: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    """建一个子解析器:``--help`` 手动加(因为我们要把 ``-h`` 让给 ``--host``)。"""
    parser = sub.add_parser(name, help=help_text, add_help=False)
    parser.add_argument("--help", action="help", help="show this help message and exit")
    return parser


def build_parser() -> argparse.ArgumentParser:
    """构造整个命令行接口 —— 单独成一个函数,便于测试与文档生成。"""
    parser = argparse.ArgumentParser(
        prog="minich",
        add_help=False,
        description="mini-clickhouse — a teaching re-implementation of ClickHouse.",
    )
    parser.add_argument("-h", "--help", action="help",
                        help="show this help message and exit")
    parser.add_argument("--version", action="version",
                        version=f"minich {__version__} (target ClickHouse {TARGET_CLICKHOUSE_VERSION})")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ── client ────────────────────────────────────────────────────────
    p = _subparser(sub, "client", "connect and run queries (interactive REPL by default)")
    _add_connection_args(p)
    p.add_argument("-q", "--query", default=None, help="run this query and exit")
    p.add_argument("-n", "--multiquery", action="store_true", help="allow several ;-separated statements")
    p.add_argument("-m", "--multiline", action="store_true", help="accumulate lines until ';'")
    p.add_argument("--data-path", default=None, dest="data_path",
                   help="in-process mode: directory for MergeTree parts")
    _add_common_output_args(p)
    p.set_defaults(func=_run_client_command)

    # ── server ────────────────────────────────────────────────────────
    p = _subparser(sub, "server", "run the TCP server")
    p.add_argument("--listen", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=None, help=f"port (default {DEFAULT_PORT})")
    p.add_argument("--data-path", default=None, dest="data_path", help="directory for MergeTree parts")
    p.add_argument("--max-threads", type=int, default=None, dest="max_threads",
                   help="cap on concurrent client connections")
    p.set_defaults(func=_run_server_command)

    # ── local ─────────────────────────────────────────────────────────
    p = _subparser(sub, "local", "run a query without a server (one-shot)")
    p.add_argument("-q", "--query", default=None, help="query to run")
    p.add_argument("-n", "--multiquery", action="store_true", help="allow several ;-separated statements")
    p.add_argument("--data-path", default=None, dest="data_path", help="directory for MergeTree parts")
    p.add_argument("-d", "--database", default="default", help="default database")
    _add_common_output_args(p)
    p.set_defaults(func=_run_local_command)

    # ── benchmark ─────────────────────────────────────────────────────
    p = _subparser(sub, "benchmark", "run a query in a warmup + timed loop")
    _add_connection_args(p)
    p.add_argument("-q", "--query", default=None, help="query to benchmark")
    p.add_argument("--iterations", type=int, default=10, help="timed iterations (default 10)")
    p.add_argument("--warmup", type=int, default=3, help="warmup iterations (default 3)")
    p.add_argument("--concurrency", type=int, default=1, help="parallel clients (default 1)")
    p.add_argument("--data-path", default=None, dest="data_path", help="in-process mode data directory")
    _add_common_output_args(p, with_format=False)
    p.set_defaults(func=_run_benchmark_command)

    # ── format ────────────────────────────────────────────────────────
    p = _subparser(sub, "format", "convert data between formats")
    p.add_argument("--in", default="TSV", dest="in_format", help="input format (default TSV)")
    p.add_argument("--out", default="PrettyCompact", dest="out_format", help="output format")
    p.add_argument("--structure", default=None,
                   help='input schema, e.g. "id UInt64, name String"')
    p.add_argument("file", nargs="?", default="-", help="input file ('-' or omitted = stdin)")
    p.set_defaults(func=_run_format_command)

    # ── status ────────────────────────────────────────────────────────
    p = _subparser(sub, "status", "show a small server overview built from system.*")
    _add_connection_args(p)
    p.add_argument("--data-path", default=None, dest="data_path", help="in-process mode data directory")
    p.set_defaults(func=_run_status_command)

    # ── demo ──────────────────────────────────────────────────────────
    p = _subparser(sub, "demo", "run a self-contained tour of mini-clickhouse")
    p.add_argument("-f", "--format", default=None, help=f"output format (default {DEFAULT_FORMAT})")
    p.set_defaults(func=_run_demo_command)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口 —— 返回**退出码**,不主动 ``sys.exit()``。

    .. code-block:: python

       # 命令行
       python -m minich local -q "SELECT 1"

       # 当成库用
       from minich.cli import main
       code = main(["local", "-q", "SELECT 1"])
    """
    _configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args))


#: 没有子命令时,argparse 的 ``--help`` 与这里的 ``--version`` 会抛 ``SystemExit`` ——
#: 这是 argparse 的内建行为,不算"库内部擅自退出"。
