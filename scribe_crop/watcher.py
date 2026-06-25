from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


class EventTarget(Enum):
    PDF = "pdf"
    CONFIG = "config"


@dataclass(frozen=True)
class WatchEvent:
    target: EventTarget
    relpath: str | None = None


def _map_to_relpath(upload_dir: Path, path: Path) -> str | None:
    name = path.name
    if name.endswith(".pdf.toml"):
        path = path.with_name(name[: -len(".toml")])
    elif not name.endswith(".pdf"):
        return None
    try:
        rel = path.relative_to(upload_dir)
    except ValueError:
        return None
    return rel.as_posix()


class _Debouncer:
    def __init__(self, *, window: float, clock: Callable[[], float]) -> None:
        self._window = window
        self._clock = clock
        self._last: dict[str, float] = {}
        self._last_sweep = 0.0
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self._window <= 0:
            return True
        now = self._clock()
        with self._lock:
            self._sweep(now)
            last = self._last.get(key)
            if last is not None and now - last < self._window:
                self._last[key] = now
                return False
            self._last[key] = now
            return True

    def _sweep(self, now: float) -> None:
        # Entries older than the window no longer suppress anything; drop them so
        # a long-running daemon does not retain one entry per path forever.
        if now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        cutoff = now - self._window
        for key in [k for k, t in self._last.items() if t < cutoff]:
            del self._last[key]


class UploadEventRouter:
    def __init__(
        self,
        *,
        upload_dir: Path,
        config_path: Path,
        sink: Callable[[WatchEvent], None],
        debounce_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._upload_dir = upload_dir
        self._config_path = config_path
        self._sink = sink
        self._debounce = _Debouncer(window=debounce_seconds, clock=clock)

    def handle_paths(self, *paths: Path | str) -> None:
        for raw in paths:
            self._handle_one(Path(raw))

    def _handle_one(self, path: Path) -> None:
        if path == self._config_path:
            if self._debounce.allow("\0config"):
                self._sink(WatchEvent(EventTarget.CONFIG))
            return
        relpath = _map_to_relpath(self._upload_dir, path)
        if relpath is None:
            return
        if self._debounce.allow(relpath):
            self._sink(WatchEvent(EventTarget.PDF, relpath))


class _WatchdogAdapter(FileSystemEventHandler):
    def __init__(self, router: UploadEventRouter) -> None:
        self._router = router

    def _dispatch(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths: list[Path | str] = [event.src_path]
        dest = getattr(event, "dest_path", None)
        if dest:
            paths.append(dest)
        self._router.handle_paths(*paths)

    on_created = _dispatch
    on_modified = _dispatch
    on_moved = _dispatch


class Watcher:
    def __init__(
        self,
        *,
        upload_dir: Path,
        config_path: Path,
        sink: Callable[[WatchEvent], None],
        debounce_seconds: float = 0.5,
    ) -> None:
        self._upload_dir = upload_dir
        self._config_path = config_path
        self._router = UploadEventRouter(
            upload_dir=upload_dir,
            config_path=config_path,
            sink=sink,
            debounce_seconds=debounce_seconds,
        )
        self._observer = Observer()
        self._started = False

    def start(self) -> None:
        handler = _WatchdogAdapter(self._router)
        self._observer.schedule(handler, str(self._upload_dir), recursive=True)
        # The root config.toml lives outside upload/; watch its directory.
        self._observer.schedule(
            handler, str(self._config_path.parent), recursive=False
        )
        self._observer.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._observer.stop()
        self._observer.join()
        self._started = False
