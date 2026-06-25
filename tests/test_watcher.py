import time
from queue import Empty, Queue

import pytest

from scribe_crop.watcher import (
    EventTarget,
    UploadEventRouter,
    WatchEvent,
    Watcher,
)


@pytest.fixture
def dirs(tmp_path):
    root = tmp_path / "ScribeCrop"
    upload = root / "upload"
    upload.mkdir(parents=True)
    config_path = root / "config.toml"
    return root, upload, config_path


def _router(upload, config_path, sink, *, debounce=0.0, clock=None):
    kw = {"debounce_seconds": debounce}
    if clock is not None:
        kw["clock"] = clock
    return UploadEventRouter(
        upload_dir=upload, config_path=config_path, sink=sink, **kw
    )


def test_pdf_event_maps_to_relpath(dirs):
    _, upload, config_path = dirs
    events = []
    r = _router(upload, config_path, events.append)
    r.handle_paths(upload / "sub" / "a.pdf")
    assert events == [WatchEvent(EventTarget.PDF, "sub/a.pdf")]


def test_toml_event_maps_to_pdf(dirs):
    _, upload, config_path = dirs
    events = []
    r = _router(upload, config_path, events.append)
    r.handle_paths(upload / "a.pdf.toml")
    assert events == [WatchEvent(EventTarget.PDF, "a.pdf")]


def test_config_event_maps_to_config_target(dirs):
    _, upload, config_path = dirs
    events = []
    r = _router(upload, config_path, events.append)
    r.handle_paths(config_path)
    assert events == [WatchEvent(EventTarget.CONFIG)]


def test_unrelated_files_ignored(dirs):
    _, upload, config_path = dirs
    events = []
    r = _router(upload, config_path, events.append)
    r.handle_paths(upload / "notes.txt", upload / "image.png", config_path.parent / "other.toml")
    assert events == []


def test_debounce_collapses_rapid_bursts_per_path(dirs):
    _, upload, config_path = dirs
    events = []
    now = [100.0]
    r = _router(upload, config_path, events.append, debounce=5.0, clock=lambda: now[0])
    r.handle_paths(upload / "a.pdf")
    r.handle_paths(upload / "a.pdf")
    assert len(events) == 1
    now[0] += 10.0
    r.handle_paths(upload / "a.pdf")
    assert len(events) == 2


def test_debounce_is_per_path(dirs):
    _, upload, config_path = dirs
    events = []
    now = [0.0]
    r = _router(upload, config_path, events.append, debounce=5.0, clock=lambda: now[0])
    r.handle_paths(upload / "a.pdf")
    r.handle_paths(upload / "b.pdf")
    assert {e.relpath for e in events} == {"a.pdf", "b.pdf"}


def test_debouncer_prunes_stale_entries():
    from scribe_crop.watcher import _Debouncer

    t = {"v": 0.0}
    d = _Debouncer(window=1.0, clock=lambda: t["v"])
    d.allow("a")
    t["v"] = 0.5
    d.allow("b")
    t["v"] = 10.0
    d.allow("c")
    assert set(d._last) == {"c"}


def test_real_observer_end_to_end(dirs):
    _, upload, config_path = dirs
    queue: Queue[WatchEvent] = Queue()
    watcher = Watcher(
        upload_dir=upload,
        config_path=config_path,
        sink=queue.put,
        debounce_seconds=0.0,
    )
    watcher.start()
    try:
        time.sleep(0.2)
        (upload / "dropped.pdf").write_bytes(b"%PDF-1.4")
        seen = _drain_for(queue, "dropped.pdf", timeout=5.0)
        assert seen, "expected dropped.pdf to be enqueued by the real observer"
    finally:
        watcher.stop()


def _drain_for(queue, relpath, *, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = queue.get(timeout=0.2)
        except Empty:
            continue
        if event.target is EventTarget.PDF and event.relpath == relpath:
            return True
    return False
