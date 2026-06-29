from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from .config import load_server_config
from .service import MirrorReadiness, Service

log = logging.getLogger("scribe_crop")

# The crop step shells out to our shim (it wraps pdfcropmargins and optionally
# strips running headers/footers), not to pdfcropmargins directly. The shim is
# installed as a console script alongside the daemon.
_BINARY = "scribe-crop-shim"


class BinaryMissing(RuntimeError):
    pass


def resolve_binary(
    name: str = _BINARY, *, which=shutil.which, argv0: str | None = None
) -> str:
    path = which(name)
    if path is not None:
        return path
    # The shim console script is installed in the same bin directory as this
    # daemon. When scribe-crop is launched by absolute path (e.g.
    # ./result/bin/scribe-crop) that directory may not be on PATH, so fall back
    # to the sibling next to our own entry point before giving up.
    entry = argv0 if argv0 is not None else sys.argv[0]
    if entry:
        sibling = Path(entry).resolve().parent / name
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
    raise BinaryMissing(f"{name} not found on PATH or next to {entry!r}")


def _readiness(args: argparse.Namespace) -> MirrorReadiness:
    marker = Path(args.readiness_marker) if args.readiness_marker else None
    return MirrorReadiness(assume_current=args.mirror_current, readiness_marker=marker)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scribe-crop")
    parser.add_argument("-c", "--config", required=True, help="server config TOML path")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="debug logging"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("run", "reconcile"):
        p = sub.add_parser(name)
        p.add_argument("--mirror-current", action="store_true")
        p.add_argument("--readiness-marker", default=None)
    return parser


def _make_service(args: argparse.Namespace, binary: str) -> Service:
    config = load_server_config(args.config)
    return Service(config, binary=binary, readiness=_readiness(args))


def cmd_run(args: argparse.Namespace, binary: str) -> int:
    service = _make_service(args, binary)
    service.run()
    return 0


def cmd_reconcile(args: argparse.Namespace, binary: str) -> int:
    service = _make_service(args, binary)
    try:
        service.ensure_dirs()
        report = service.reconcile()
        log.info(
            "reconcile: processed=%d skipped=%d failed=%d gc_removed=%d",
            len(report.processed),
            len(report.skipped),
            len(report.failed),
            len(report.gc_removed),
        )
    finally:
        service.close()
    return 0


_COMMANDS = {"run": cmd_run, "reconcile": cmd_reconcile}


def main(
    argv: list[str] | None = None, *, which=shutil.which, argv0: str | None = None
) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        binary = resolve_binary(which=which, argv0=argv0)
    except BinaryMissing as exc:
        log.error("%s", exc)
        return 2
    return _COMMANDS[args.command](args, binary)


if __name__ == "__main__":
    sys.exit(main())
