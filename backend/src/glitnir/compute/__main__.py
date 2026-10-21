"""Asynchronous computation commands for individual agents and worker services."""

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from glitnir.compute.client import Client, Settings, result_from_job, save_receipt
from glitnir.compute.protocol import make_request, validate_request
from glitnir.compute.worker import Worker, execute
from glitnir.lean.limits import DEFAULT_MEMORY_LIMIT_MB


def main(argv: Sequence[str] | None = None) -> int:
    """Submit without waiting, inspect/cancel receipts, or run a prepared worker."""
    parser = argparse.ArgumentParser(prog="python -m glitnir.compute")
    parser.add_argument("--server", default=os.environ.get("GLITNIR_TASKVALET_URL", ""))
    parser.add_argument(
        "--project", default=os.environ.get("GLITNIR_TASKVALET_PROJECT", "")
    )
    parser.add_argument(
        "--environment", default=os.environ.get("GLITNIR_TASKVALET_ENVIRONMENT", "")
    )
    parser.add_argument("--credential-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("source", type=Path)
    submit.add_argument("--receipt", type=Path, required=True)
    submit.add_argument("--timeout-seconds", type=int, default=600)
    submit.add_argument("--memory-mb", type=int, default=8192)
    submit.add_argument("--through-line", type=int)
    for name in ("retry", "status", "result", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("receipt", type=Path)
    worker = commands.add_parser("worker")
    worker.add_argument("--prepared", type=Path, required=True)
    worker.add_argument("--journal", type=Path, required=True)
    worker.add_argument("--max-seconds", type=int, default=1800)
    worker.add_argument("--max-memory-mb", type=int, default=DEFAULT_MEMORY_LIMIT_MB)
    recovery = commands.add_parser("retry-completion")
    recovery.add_argument("journal", type=Path)
    child = commands.add_parser("execute", help=argparse.SUPPRESS)
    for name in ("prepared", "request", "output", "scratch"):
        child.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        if args.command == "execute":
            request = json.loads(args.request.read_text())
            save_receipt(
                args.output, execute(args.prepared, request, args.scratch, stop)
            )
            return 0
        token = os.environ.get("GLITNIR_TASKVALET_TOKEN", "")
        if args.credential_file:
            if args.credential_file.stat().st_mode & 0o077:
                raise ValueError("Credential file must be private (chmod 600)")
            credential = json.loads(args.credential_file.read_text())
            if credential.get("url", "").rstrip("/") != args.server.rstrip(
                "/"
            ) or args.project not in credential.get("projects", []):
                raise ValueError(
                    "Credential file belongs to a different server or project"
                )
            token = credential["token"]
        client = Client(
            Settings(
                args.server,
                args.project,
                args.environment,
                token,
                os.environ.get("GLITNIR_TASKVALET_CA_FILE"),
            )
        )
        if args.command == "retry-completion":
            journal = json.loads((args.journal / "lease.json").read_text())
            completion = json.loads((args.journal / "completion.json").read_text())
            if (
                journal.get("url") != client.settings.url.rstrip("/")
                or journal.get("project") != client.settings.project
            ):
                raise ValueError(
                    "Worker journal belongs to a different server or project"
                )
            request = journal["job"]["request"]
            validate_request(request)
            if any(
                completion.get(key) != journal.get(key)
                for key in ("worker_id", "lease")
            ):
                raise ValueError("Completion does not match its original worker lease")
            value = client.request(
                "POST", f"/jobs/{request['request_id']}/complete", completion
            )
            print(json.dumps(value))
            return 0
        if args.command == "worker":
            Worker(
                client,
                args.prepared,
                args.journal,
                max_seconds=args.max_seconds,
                max_memory_mb=args.max_memory_mb,
            ).run(stop)
            return 0
        if args.command == "submit":
            request = make_request(
                args.source,
                args.environment,
                args.timeout_seconds,
                args.memory_mb,
                args.through_line,
            )
            value = client.submit(request, args.receipt)
        else:
            journal = json.loads(args.receipt.read_text())
            if (
                journal.get("format") != "glitnir.compute-receipt/v1"
                or journal.get("url") != client.settings.url.rstrip("/")
                or journal.get("project") != client.settings.project
            ):
                raise ValueError(
                    "Receipt does not belong to the configured server and project"
                )
            request = journal["request"]
            validate_request(request)
            identity = request["request_id"]
            if args.command == "retry":
                value = client.submit(request, args.receipt)
            else:
                value = (
                    client.request("POST", f"/jobs/{identity}/cancel", {})
                    if args.command == "cancel"
                    else client.request("GET", f"/jobs/{identity}")
                )
                if value.get("id") != identity or value.get("request") != request:
                    raise ValueError("Server job does not match this receipt")
                save_receipt(args.receipt, {**journal, "job": value})
                if args.command == "result":
                    value = result_from_job(value, request)
        print(json.dumps(value))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
