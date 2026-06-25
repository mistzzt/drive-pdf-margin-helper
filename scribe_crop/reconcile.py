from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import ServerConfig
from .processor import ProcessResult, ResultKind, _failed_log_path
from .state import StateStore

Processor = Callable[[str], ProcessResult]


@dataclass
class ReconcileReport:
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    gc_removed: list[str] = field(default_factory=list)


def iter_upload_pdfs(upload_dir: Path) -> Iterable[Path]:
    if not upload_dir.is_dir():
        return
    for path in sorted(upload_dir.rglob("*.pdf")):
        if path.is_file():
            yield path


def forward_pass(
    config: ServerConfig,
    *,
    process: Processor,
    report: ReconcileReport | None = None,
) -> ReconcileReport:
    report = report or ReconcileReport()
    for pdf in iter_upload_pdfs(config.upload_dir):
        relpath = pdf.relative_to(config.upload_dir).as_posix()
        # An unexpected exception (e.g. the source vanished mid-pass) is a
        # transient failure for this file only: skip it and let a later pass
        # retry, never blocking the remaining files or recording suppression.
        try:
            result = process(relpath)
        except Exception:  # noqa: BLE001
            report.failed.append(relpath)
            continue
        if result.kind is ResultKind.SKIPPED:
            report.skipped.append(relpath)
        else:
            report.processed.append(relpath)
    return report


def reverse_gc(
    config: ServerConfig,
    *,
    store: StateStore,
    mirror_current: bool,
    report: ReconcileReport | None = None,
) -> ReconcileReport:
    report = report or ReconcileReport()
    if not mirror_current:
        return report
    for record in store.list_all():
        relpath = record.relpath
        source = config.upload_dir / relpath
        if source.exists():
            continue
        _remove_outputs(config, relpath)
        store.delete(relpath)
        report.gc_removed.append(relpath)
    return report


def _remove_outputs(config: ServerConfig, relpath: str) -> None:
    rel = Path(relpath)
    processed = config.processed_dir / rel
    failed = config.failed_dir / rel
    for path in (processed, failed, _failed_log_path(failed)):
        path.unlink(missing_ok=True)


def reconcile(
    config: ServerConfig,
    *,
    store: StateStore,
    process: Processor,
    mirror_current: bool,
) -> ReconcileReport:
    report = ReconcileReport()
    forward_pass(config, process=process, report=report)
    reverse_gc(config, store=store, mirror_current=mirror_current, report=report)
    return report
