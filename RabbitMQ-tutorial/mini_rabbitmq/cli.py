"""命令行入口：mini-rabbitmq serve / version / diagnose。"""

from __future__ import annotations

import argparse
import logging
import os
import ssl
import sys
from pathlib import Path

from .broker import Broker
from .management import ManagementServer
from .server import AMQPServer, SERVER_VERSION


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "var" / "data"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-rabbitmq",
        description="A readable, testable AMQP 0-9-1 broker for the RabbitMQ tutorial.",
    )
    parser.add_argument("--version", action="version", version=SERVER_VERSION)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the AMQP and management listeners")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5672, help="AMQP port (0 = ephemeral)")
    serve.add_argument(
        "--management-port",
        type=int,
        default=15672,
        help="HTTP management port (0 = ephemeral, -1 = disabled)",
    )
    serve.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    serve.add_argument("--no-persistence", action="store_true")
    serve.add_argument("--fsync", action="store_true", help="fsync the snapshot on each save")
    serve.add_argument("--node-name", default="mini@localhost")
    serve.add_argument("--heartbeat", type=int, default=60)
    serve.add_argument("--frame-max", type=int, default=131072)
    serve.add_argument("--channel-max", type=int, default=2047)
    serve.add_argument("--tls-cert")
    serve.add_argument("--tls-key")
    serve.add_argument("--username", default="guest")
    serve.add_argument(
        "--password",
        default=os.environ.get("MINI_RABBITMQ_PASSWORD", "guest"),
        help="password for --username (env: MINI_RABBITMQ_PASSWORD)",
    )
    serve.add_argument("--log-level", default="INFO")

    sub.add_parser("diagnose", help="print environment and verification commands")
    sub.add_parser("version", help="print version")
    return parser


def _ssl_context(cert: str | None, key: str | None) -> ssl.SSLContext | None:
    if not cert and not key:
        return None
    if not cert or not key:
        raise SystemExit("--tls-cert and --tls-key must be supplied together")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


def serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    data_file = None if args.no_persistence else Path(args.data_dir) / "broker.json"
    broker = Broker(
        data_file=data_file,
        auto_checkpoint=not args.no_persistence,
        fsync=args.fsync,
        node_name=args.node_name,
    )
    if args.username != "guest" or args.password != "guest":
        if args.username not in broker.auth.users:
            broker.add_user(args.username, args.password, tags=("administrator", "management"))
            broker.set_permissions(args.username, "/")
        else:
            broker.auth.add_user(
                args.username, args.password, tags=("administrator", "management")
            )
            broker.set_permissions(args.username, "/")
    amqp = AMQPServer(
        broker,
        args.host,
        args.port,
        heartbeat=args.heartbeat,
        frame_max=args.frame_max,
        channel_max=args.channel_max,
        ssl_context=_ssl_context(args.tls_cert, args.tls_key),
    )
    management = None
    amqp_port = amqp.start()
    broker.start_maintenance(0.5)
    if args.management_port >= 0:
        management = ManagementServer(
            broker, host=args.host, port=args.management_port, amqp_server=amqp
        )
        management_port = management.start()
    else:
        management_port = None
    print(f"mini-rabbitmq {SERVER_VERSION} listening", flush=True)
    print(f"  AMQP:       {'amqps' if args.tls_cert else 'amqp'}://{args.host}:{amqp_port}", flush=True)
    if management_port:
        print(f"  Management: http://{args.host}:{management_port}/api/overview", flush=True)
    print(f"  Data:       {data_file or 'in-memory (no persistence)'}", flush=True)
    print("Press Ctrl+C to stop. This process intentionally has no watchdog.", flush=True)
    try:
        while True:
            import time

            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if management is not None:
            management.stop()
        amqp.stop()
        broker.stop_maintenance()
        broker.close()
    return 0


def diagnose() -> int:
    print(f"mini-rabbitmq {SERVER_VERSION}")
    print(f"python: {sys.version.split()[0]}")
    print(f"project root: {ROOT}")
    print("Verification commands (run from the tutorial root):")
    print("  python -m pytest -q")
    print("  python examples/run_all.py")
    print("  python check_docs.py")
    print("Server smoke test:")
    print("  python -m mini_rabbitmq serve --no-persistence --port 5673 --management-port 15673")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "version"):
        if args.command is None:
            parser.print_help()
            return 2
        print(SERVER_VERSION)
        return 0
    if args.command == "diagnose":
        return diagnose()
    if args.command == "serve":
        return serve(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
