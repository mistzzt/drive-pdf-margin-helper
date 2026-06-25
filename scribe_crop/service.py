from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue

from .config import (
    DriveConfigResult,
    ServerConfig,
    load_drive_config,
)
from .fingerprint import ToolVersion, probe_tool_version
from .processor import ProcessResult, ResultKind, process_pdf
from .reconcile import ReconcileReport, forward_pass, reconcile, reverse_gc
from .state import StateStore
from .watcher import EventTarget, WatchEvent, Watcher

log = logging.getLogger("scribe_crop.service")

Clock = Callable[[], float]
# Returns True if the wait was cut short because stop was requested.
Wait = Callable[[float], bool]
ProcessFn = Callable[..., ProcessResult]

_BUILTIN_DRIVE_CONFIG = DriveConfigResult(crop={}, error=None, raw_bytes=b"")


@dataclass(frozen=True)
class MirrorReadiness:
    """How the service decides the local mirror is current enough to GC.

    GC is destructive, so an absent source is not treated as a deletion unless
    the mirror is explicitly signalled current.
    """

    assume_current: bool = False
    readiness_marker: Path | None = None

    def is_current(self) -> bool:
        if self.assume_current:
            return True
        if self.readiness_marker is not None:
            return self.readiness_marker.exists()
        return False


def _load_drive(config: ServerConfig) -> DriveConfigResult:
    result = load_drive_config(config.drive_config_path)
    if result.ok:
        config.config_error_path.unlink(missing_ok=True)
        return result
    try:
        config.config_error_path.write_text(f"{result.error}\n")
    except OSError:
        log.exception("failed to write config.error.log")
    return result


class Service:
    def __init__(
        self,
        config: ServerConfig,
        *,
        binary: str,
        readiness: MirrorReadiness | None = None,
        tool_version: ToolVersion | None = None,
        process_fn: ProcessFn = process_pdf,
        clock: Clock = time.monotonic,
        sleep: Wait | None = None,
    ) -> None:
        self._config = config
        self._binary = binary
        self._readiness = readiness or MirrorReadiness()
        self._tool_version = tool_version or probe_tool_version()
        self._process_fn = process_fn
        self._clock = clock
        self._store = StateStore(config.resolved_state_path)
        self._queue: Queue[WatchEvent] = Queue()
        self._stop = threading.Event()
        # The wait must observe _stop so a worker in backoff unblocks promptly on
        # shutdown instead of sleeping (up to retry_backoff.max_seconds) past the
        # store close. Returns True when cut short by stop.
        self._sleep: Wait = sleep if sleep is not None else self._stop.wait
        self._last_change = self._clock()
        self._change_lock = threading.Lock()

        # The same load result feeds both the crop dict and the bytes folded
        # into the fingerprint, so the profile applied always matches what was
        # hashed. Last-known-good is retained across reload failures.
        drive = _load_drive(config)
        self._drive = drive if drive.ok else _BUILTIN_DRIVE_CONFIG

    @property
    def store(self) -> StateStore:
        return self._store

    def ensure_dirs(self) -> None:
        for d in (
            self._config.upload_dir,
            self._config.processed_dir,
            self._config.failed_dir,
            self._config.tmp_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def _process(self, relpath: str) -> ProcessResult:
        # Single read of the (atomically rebound) drive config so crop and
        # raw_bytes always come from the same load, even if a reload races us.
        drive = self._drive
        return self._process_fn(
            relpath,
            config=self._config,
            drive_crop=drive.crop or {},
            drive_config_bytes=drive.raw_bytes or b"",
            store=self._store,
            tool_version=self._tool_version,
            binary=self._binary,
        )

    def reconcile_startup(self) -> None:
        forward_pass(self._config, process=self._process)
        mirror_current = self._readiness.is_current()
        if mirror_current:
            reverse_gc(self._config, store=self._store, mirror_current=True)
        else:
            log.info("skipping reverse GC: mirror not known-current")

    def reconcile(self) -> ReconcileReport:
        return reconcile(
            self._config,
            store=self._store,
            process=self._process,
            mirror_current=self._readiness.is_current(),
        )

    def wait_for_stable(self, relpath: str) -> bool:
        path = self._config.upload_dir / relpath
        stability = self._config.stability_seconds
        if stability <= 0:
            return path.exists()
        # Cap total waiting so a perpetually growing/locked file never blocks
        # the worker forever; design treats this as a secondary guard.
        deadline = self._clock() + max(stability * 4, stability + 5.0)
        try:
            last_size = path.stat().st_size
        except FileNotFoundError:
            return False
        stable_since = self._clock()
        while not self._stop.is_set():
            if self._sleep(stability):
                return False
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return False
            now = self._clock()
            if size == last_size:
                if now - stable_since >= stability:
                    return True
            else:
                last_size = size
                stable_since = now
            if now >= deadline:
                log.warning("stability check timed out for %s; processing anyway", relpath)
                return True
        return False

    def process_with_retry(self, relpath: str) -> ProcessResult:
        backoff = self._config.retry_backoff
        delay = backoff.initial_seconds
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            result = self._process(relpath)
            if result.kind is not ResultKind.ENVIRONMENTAL_FAILURE:
                if result.kind in (ResultKind.SUCCESS, ResultKind.SKIPPED):
                    log.info("%s: %s", relpath, result.kind.value)
                else:
                    log.warning("%s: %s (%s)", relpath, result.kind.value, result.reason)
                return result
            # Bound the attempts so one persistently-failing file cannot pin the
            # worker forever and starve the queue; a deferred file is retried on
            # the next reconcile, config reload, or restart.
            if attempt >= backoff.max_attempts:
                log.warning(
                    "%s: environmental failure after %d attempts: %s; deferring",
                    relpath,
                    attempt,
                    result.reason,
                )
                return result
            log.warning(
                "%s: environmental failure (attempt %d): %s; retrying in %.0fs",
                relpath,
                attempt,
                result.reason,
                delay,
            )
            if self._sleep(delay):
                break
            delay = min(delay * backoff.multiplier, backoff.max_seconds)
        return ProcessResult(relpath, ResultKind.CANCELLED, reason="stopped")

    def _note_change(self) -> None:
        with self._change_lock:
            self._last_change = self._clock()

    def reload_drive_config(self) -> None:
        drive = _load_drive(self._config)
        if drive.ok:
            self._drive = drive
            log.info("reloaded drive config; triggering reconcile")
        else:
            log.warning("drive config invalid: %s; keeping last-known-good", drive.error)
        forward_pass(self._config, process=self._process)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                self._handle_event(event)
            except Exception:  # noqa: BLE001
                log.exception("worker failed handling %s", event)
            finally:
                self._queue.task_done()

    def _handle_event(self, event: WatchEvent) -> None:
        self._note_change()
        if event.target is EventTarget.CONFIG:
            self.reload_drive_config()
            return
        assert event.relpath is not None
        if not self.wait_for_stable(event.relpath):
            return
        self.process_with_retry(event.relpath)

    def heartbeat(self) -> None:
        with self._change_lock:
            since = self._clock() - self._last_change
        log.info("heartbeat: %.0fs since last observed change", since)

    def _heartbeat_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            self.heartbeat()

    def run(self, *, heartbeat_seconds: float = 300.0) -> None:
        self.ensure_dirs()
        log.info("tool versions: %s", self._tool_version.as_token())
        if self._tool_version.pdfcropmargins is None or self._tool_version.ghostscript is None:
            log.warning(
                "tool version probe incomplete (%s); a null half weakens "
                "upgrade-triggered re-cropping",
                self._tool_version.as_token(),
            )
        self.reconcile_startup()

        workers = [
            threading.Thread(target=self._worker_loop, name=f"worker-{i}", daemon=True)
            for i in range(self._config.worker_count)
        ]
        for worker in workers:
            worker.start()
        hb = threading.Thread(
            target=self._heartbeat_loop, args=(heartbeat_seconds,),
            name="heartbeat", daemon=True,
        )
        hb.start()

        watcher = Watcher(
            upload_dir=self._config.upload_dir,
            config_path=self._config.drive_config_path,
            sink=self._queue.put,
            debounce_seconds=min(self._config.stability_seconds, 1.0),
        )
        watcher.start()
        log.info("watching %s", self._config.upload_dir)
        try:
            while not self._stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            watcher.stop()
            # Join workers before closing the store so an in-flight worker
            # (possibly inside a subprocess up to process_timeout_seconds) can
            # persist completed work instead of hitting a closed connection.
            join_timeout = self._config.process_timeout_seconds + 30.0
            for worker in workers:
                worker.join(timeout=join_timeout)
            hb.join(timeout=join_timeout)
            self._store.close()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._store.close()
