"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .app import AppOptions, PrometheusApp
from .config import load_config
from .model import PrometheusError
from .otlp import STRATEGY_NO_UTF8, TRANSLATION_STRATEGIES
from .rules.manager import load_rule_groups
from .util.timeutil import parse_duration, parse_timestamp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-prometheus",
        description="A small, runnable Prometheus-compatible server.",
    )
    parser.add_argument("--version", action="version", version=f"mini-prometheus {__version__}")
    parser.add_argument("--config.file", dest="config_file", default="prometheus.yml", help="configuration file")
    parser.add_argument("--storage.tsdb.path", dest="storage_tsdb_path", default="data", help="data directory")
    parser.add_argument(
        "--storage.tsdb.retention.time", dest="storage_tsdb_retention_time", default="15d", help="block retention (e.g. 15d)"
    )
    parser.add_argument(
        "--storage.tsdb.min-block-duration", dest="storage_tsdb_min_block_duration", default="2h", help="block range / flush boundary"
    )
    parser.add_argument(
        "--web.listen-address", dest="web_listen_address", default="127.0.0.1:9090", help="host:port to listen on"
    )
    parser.add_argument("--web.enable-admin-api", dest="web_enable_admin_api", action="store_true")
    parser.add_argument("--web.enable-lifecycle", dest="web_enable_lifecycle", action="store_true")
    parser.add_argument("--web.enable-otlp-receiver", dest="web_enable_otlp_receiver", action="store_true")
    parser.add_argument(
        "--storage.otlp.translation-strategy",
        dest="otlp_translation_strategy",
        default=STRATEGY_NO_UTF8,
        choices=TRANSLATION_STRATEGIES,
    )
    parser.add_argument(
        "--enable-feature",
        dest="enable_features",
        action="append",
        default=[],
        metavar="FEATURE[,FEATURE...]",
        help="comma-separated feature names (e.g. otlp-deltatocumulative)",
    )
    parser.add_argument("--log.level", dest="log_level", default="info", choices=["debug", "info", "warn", "error"])
    parser.add_argument("--log.requests", dest="log_requests", action="store_true", help="log every HTTP request")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("check", "tsdb", "query"):
        return _subcommand(argv)
    args = build_parser().parse_args(argv)
    features = {
        item.strip()
        for group in args.enable_features
        for item in group.split(",")
        if item.strip()
    }
    if args.web_enable_otlp_receiver and "otlp-native-delta-ingestion" in features:
        print(
            "mini-prometheus: otlp-native-delta-ingestion is not implemented; "
            "use otlp-deltatocumulative",
            file=sys.stderr,
        )
        return 2
    options = AppOptions(
        config_file=args.config_file,
        data_dir=args.storage_tsdb_path,
        retention=args.storage_tsdb_retention_time,
        listen_address=args.web_listen_address,
        enable_admin_api=args.web_enable_admin_api,
        enable_lifecycle=args.web_enable_lifecycle,
        log_requests=args.log_requests,
        block_range=args.storage_tsdb_min_block_duration,
        enable_otlp_receiver=args.web_enable_otlp_receiver,
        otlp_translation_strategy=args.otlp_translation_strategy,
        otlp_delta_to_cumulative="otlp-deltatocumulative" in features,
    )
    app = PrometheusApp(options)
    try:
        host, port = app.start()
    except Exception as exc:
        app.shutdown()
        print(f"mini-prometheus: startup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Mini-Prometheus {__version__}")
    print(f"  config:  {Path(options.config_file).resolve()}")
    print(f"  storage: {Path(options.data_dir).resolve()}")
    print(f"  listen:  http://{host}:{port}")
    print(f"  graph:   http://{host}:{port}/graph")
    print("  ready   (Ctrl+C to stop)")

    def stop(signum, frame):
        app.shutdown()

    try:
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
    except ValueError:
        pass
    app.wait()
    app.shutdown()
    return 0


def _subcommand(argv: list[str]) -> int:
    command = argv[0]
    if command == "check":
        if len(argv) < 2 or argv[1] not in ("config", "rules"):
            print("usage: mini-prometheus check {config|rules} <file...>", file=sys.stderr)
            return 2
        files = argv[2:]
        if not files:
            files = ["prometheus.yml"] if argv[1] == "config" else []
        if not files:
            print("no files given", file=sys.stderr)
            return 2
        try:
            if argv[1] == "config":
                for filename in files:
                    config = load_config(filename)
                    base = str(Path(filename).parent or Path("."))
                    groups = load_rule_groups(
                        config.rule_files,
                        base_dir=base,
                        evaluation_interval_ms=parse_duration(
                            config.global_config.evaluation_interval
                        ),
                        default_query_offset_ms=parse_duration(
                            config.global_config.rule_query_offset
                        ),
                    )
                    print(
                        f"SUCCESS: {filename} is valid; "
                        f"{len(config.scrape_configs)} scrape job(s), "
                        f"{len(groups)} rule group(s)"
                    )
            else:
                # Explicit paths on the command line are resolved relative to
                # the current directory, not to the first file's directory.
                groups = load_rule_groups(files, base_dir=".")
                rules = sum(len(group.rules) for group in groups)
                print(f"SUCCESS: {len(groups)} rule group(s), {rules} rule(s)")
            return 0
        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
    if command == "tsdb":
        return _tsdb_command(argv[1:])
    if command == "query":
        return _query_command(argv[1:])
    print(f"unknown command {command}", file=sys.stderr)
    return 2


def _tsdb_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mini-prometheus tsdb")
    parser.add_argument("action", choices=["list", "analyze", "snapshot"])
    parser.add_argument("--storage.tsdb.path", dest="storage_tsdb_path", default="data")
    args = parser.parse_args(argv)
    path = Path(args.storage_tsdb_path)
    if not path.exists():
        print(f"no such directory: {path}", file=sys.stderr)
        return 1
    blocks_dir = path / "blocks"
    blocks = []
    if blocks_dir.exists():
        for block in sorted(blocks_dir.iterdir()):
            meta = block / "meta.json"
            if meta.exists():
                blocks.append(json.loads(meta.read_text("utf-8")))
    if args.action == "list":
        print(f"TSDB blocks in {path}: {len(blocks)}")
        for meta in blocks:
            print(
                f"  {meta['ulid']}  {meta['minTime']}..{meta['maxTime']}  "
                f"series={meta['stats']['numSeries']} samples={meta['stats']['numSamples']} "
                f"level={meta.get('compaction', {}).get('level', 1)}"
            )
        checkpoint = path / "checkpoint.json"
        if checkpoint.exists():
            data = json.loads(checkpoint.read_text("utf-8"))
            print(f"  head checkpoint: {len(data.get('series', []))} series")
        return 0
    if args.action == "analyze":
        total_series = sum(meta["stats"]["numSeries"] for meta in blocks)
        total_samples = sum(meta["stats"]["numSamples"] for meta in blocks)
        size = sum(
            file.stat().st_size
            for block in (blocks_dir.iterdir() if blocks_dir.exists() else [])
            for file in block.iterdir()
            if file.is_file()
        )
        print(f"blocks={len(blocks)} series={total_series} samples={total_samples} bytes={size}")
        if total_samples:
            print(f"bytes/sample={size / total_samples:.2f}")
        return 0
    print("snapshot requires a running server: POST /api/v1/admin/tsdb/snapshot", file=sys.stderr)
    return 2


def _query_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mini-prometheus query")
    parser.add_argument("kind", choices=["instant", "range"])
    parser.add_argument("url", help="server URL, e.g. http://127.0.0.1:9090")
    parser.add_argument("query")
    parser.add_argument("--time", default="")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--step", default="60s")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    if args.kind == "instant":
        params = {"query": args.query}
        if args.time:
            params["time"] = str(parse_timestamp(args.time) / 1000.0)
        url = f"{base}/api/v1/query"
    else:
        params = {
            "query": args.query,
            "start": str(parse_timestamp(args.start) / 1000.0),
            "end": str(parse_timestamp(args.end) / 1000.0),
            "step": args.step,
        }
        url = f"{base}/api/v1/query_range"
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(params).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"query failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
