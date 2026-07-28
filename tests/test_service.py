import threading
import time

import pytest

from scribe_crop.config import RetryBackoff, ServerConfig
from scribe_crop.fingerprint import ToolVersion
from scribe_crop.processor import ProcessResult, ResultKind
from scribe_crop.service import MirrorReadiness, Service
from scribe_crop.state import Outcome
from scribe_crop.watcher import EventTarget, WatchEvent

TV = ToolVersion(pdfcropmargins="2.2.1", ghostscript="10.0")


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "ScribeCrop"
    for sub in ("upload", "processed", "failed"):
        (root / sub).mkdir(parents=True)
    return ServerConfig(
        root=root,
        state_path=root / "state.db",
        stability_seconds=2.0,
        retry_backoff=RetryBackoff(initial_seconds=10.0, max_seconds=100.0, multiplier=2.0),
    )


def _service(config, *, readiness=None, process_fn=None, clock=None):
    clock = clock or FakeClock()
    fn = process_fn or (lambda rp, **kw: ProcessResult(rp, ResultKind.SUCCESS))
    return Service(
        config,
        binary="pdfcropmargins",
        readiness=readiness or MirrorReadiness(),
        tool_version=TV,
        process_fn=fn,
        clock=clock.now,
        sleep=clock.sleep,
    )


def test_retry_environmental_then_terminal(config):
    clock = FakeClock()
    calls = {"n": 0}

    def fn(rp, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return ProcessResult(rp, ResultKind.ENVIRONMENTAL_FAILURE, reason="boom")
        return ProcessResult(rp, ResultKind.SUCCESS)

    svc = _service(config, process_fn=fn, clock=clock)
    result = svc.process_with_retry("a.pdf")
    svc.close()
    assert result.kind is ResultKind.SUCCESS
    assert calls["n"] == 3
    # initial 10, then 20 (multiplier), bounded by max 100.
    assert clock.slept == [10.0, 20.0]


def test_terminal_results_are_not_retried(config):
    for kind in (ResultKind.SUCCESS, ResultKind.CONTENT_FAILURE, ResultKind.SKIPPED):
        clock = FakeClock()
        calls = {"n": 0}

        def fn(rp, *, _kind=kind, **kw):
            calls["n"] += 1
            return ProcessResult(rp, _kind)

        svc = _service(config, process_fn=fn, clock=clock)
        svc.process_with_retry("a.pdf")
        svc.close()
        assert calls["n"] == 1, kind
        assert clock.slept == [], kind


def test_environmental_failure_deferred_after_max_attempts(config):
    clock = FakeClock()
    small = ServerConfig(
        root=config.root,
        state_path=config.resolved_state_path,
        retry_backoff=RetryBackoff(
            initial_seconds=1.0, max_seconds=4.0, multiplier=2.0, max_attempts=3
        ),
    )
    calls = {"n": 0}

    def fn(rp, **kw):
        calls["n"] += 1
        return ProcessResult(rp, ResultKind.ENVIRONMENTAL_FAILURE, reason="always")

    svc = _service(small, process_fn=fn, clock=clock)
    result = svc.process_with_retry("a.pdf")
    svc.close()
    assert result.kind is ResultKind.ENVIRONMENTAL_FAILURE
    assert calls["n"] == 3  # capped, not infinite
    assert clock.slept == [1.0, 2.0]  # no sleep after the final, deferred attempt


def test_backoff_is_bounded_by_max(config):
    clock = FakeClock()

    def fn(rp, **kw):
        if len(clock.slept) < 5:
            return ProcessResult(rp, ResultKind.ENVIRONMENTAL_FAILURE, reason="x")
        return ProcessResult(rp, ResultKind.SUCCESS)

    svc = _service(config, process_fn=fn, clock=clock)
    svc.process_with_retry("a.pdf")
    svc.close()
    assert clock.slept == [10.0, 20.0, 40.0, 80.0, 100.0]


def test_stability_waits_for_size_to_settle(config):
    clock = FakeClock()
    pdf = config.upload_dir / "a.pdf"
    pdf.write_bytes(b"abc")

    sizes = iter([b"abcde", b"abcdefgh", b"abcdefgh", b"abcdefgh"])

    real_sleep = clock.sleep

    def grow_then_sleep(seconds):
        try:
            pdf.write_bytes(next(sizes))
        except StopIteration:
            pass
        real_sleep(seconds)

    clock.sleep = grow_then_sleep
    svc = _service(config, clock=clock)
    assert svc.wait_for_stable("a.pdf") is True
    svc.close()
    # It kept polling while the size changed, then returned once stable.
    assert len(clock.slept) >= 3


def test_stability_returns_false_when_file_missing(config):
    svc = _service(config)
    assert svc.wait_for_stable("nope.pdf") is False
    svc.close()


def test_stability_cap_does_not_block_forever(config):
    clock = FakeClock()
    pdf = config.upload_dir / "a.pdf"
    counter = {"n": 0}

    def ever_growing(seconds):
        counter["n"] += 1
        pdf.write_bytes(b"x" * counter["n"])
        clock.t += seconds

    pdf.write_bytes(b"x")
    clock.sleep = ever_growing
    svc = _service(config, clock=clock)
    assert svc.wait_for_stable("a.pdf") is True
    svc.close()


def test_config_reload_writes_error_on_bad_config(config):
    config.drive_config_path.write_text("this is = = not valid toml ][")
    svc = _service(config)
    svc.reload_drive_config()
    svc.close()
    assert config.config_error_path.exists()
    assert config.config_error_path.read_text().strip() != ""


def test_config_reload_removes_error_on_good_config(config):
    config.config_error_path.write_text("stale error\n")
    config.drive_config_path.write_text("[crop]\npercent_retain = 12\n")
    svc = _service(config)
    svc.reload_drive_config()
    svc.close()
    assert not config.config_error_path.exists()


def test_bad_config_at_construction_writes_error_and_uses_builtin(config):
    config.drive_config_path.write_text("][ bad")
    svc = _service(config)
    try:
        assert config.config_error_path.exists()
        # Built-in fallback: empty crop dict.
        captured = {}
        svc._process_fn = lambda rp, **kw: captured.update(kw) or ProcessResult(rp, ResultKind.SUCCESS)
        svc._process("a.pdf")
        assert captured["drive_crop"] == {}
    finally:
        svc.close()


def test_reconcile_startup_runs_forward_and_skips_gc_when_not_current(config):
    (config.processed_dir / "gone.pdf").write_bytes(b"out")
    from scribe_crop.state import Outcome

    svc = _service(config, readiness=MirrorReadiness(assume_current=False))
    svc.store.upsert("gone.pdf", "fp", Outcome.SUCCESS)
    (config.upload_dir / "keep.pdf").write_bytes(b"%PDF")
    seen = []
    svc._process_fn = lambda rp, **kw: seen.append(rp) or ProcessResult(rp, ResultKind.SUCCESS)
    svc.reconcile_startup()
    svc.close()
    assert "keep.pdf" in seen
    # GC must not have removed the orphan output because mirror is not current.
    assert (config.processed_dir / "gone.pdf").exists()


def test_reconcile_startup_runs_gc_when_mirror_current(config):
    from scribe_crop.state import Outcome

    (config.processed_dir / "gone.pdf").write_bytes(b"out")
    svc = _service(config, readiness=MirrorReadiness(assume_current=True))
    svc.store.upsert("gone.pdf", "fp", Outcome.SUCCESS)
    svc.reconcile_startup()
    svc.close()
    assert not (config.processed_dir / "gone.pdf").exists()


def test_readiness_marker_gates_gc(tmp_path):
    marker = tmp_path / "synced.marker"
    r = MirrorReadiness(readiness_marker=marker)
    assert r.is_current() is False
    marker.write_text("")
    assert r.is_current() is True


def test_process_passes_loaded_crop_profile(config):
    config.drive_config_path.write_text("[crop]\npercent_retain = 7\n")
    svc = _service(config)
    captured = {}
    svc._process_fn = lambda rp, **kw: captured.update(kw) or ProcessResult(rp, ResultKind.SUCCESS)
    svc._process("a.pdf")
    svc.close()
    assert captured["drive_crop"] == {"percent_retain": 7}


def test_worker_thread_accesses_store_cross_thread(config):
    # The store is created on the main thread but used from the worker thread;
    # this drives a real event through _worker_loop to cover that access.
    cfg = ServerConfig(
        root=config.root,
        state_path=config.state_path,
        stability_seconds=0.0,
        retry_backoff=config.retry_backoff,
    )
    done = threading.Event()

    def fn(rp, *, store, **kw):
        store.upsert(rp, "fp", Outcome.SUCCESS)
        done.set()
        return ProcessResult(rp, ResultKind.SUCCESS)

    svc = _service(cfg, process_fn=fn)
    svc._clock = time.monotonic
    svc._sleep = time.sleep
    (cfg.upload_dir / "a.pdf").write_bytes(b"%PDF")
    worker = threading.Thread(target=svc._worker_loop, daemon=True)
    worker.start()
    try:
        svc._queue.put(WatchEvent(EventTarget.PDF, "a.pdf"))
        assert done.wait(timeout=5.0)
    finally:
        svc.stop()
        worker.join(timeout=5.0)
    assert svc.store.get("a.pdf") is not None
    svc.close()


def test_process_with_retry_returns_cancelled_when_stopped(config):
    svc = _service(config)
    svc.stop()
    result = svc.process_with_retry("a.pdf")
    svc.close()
    assert result.kind is ResultKind.CANCELLED


def test_backoff_is_interrupted_by_stop(config):
    # The default wait observes _stop, so a worker in backoff unblocks promptly
    # on shutdown rather than sleeping up to retry_backoff.max_seconds.
    svc = _service(config, process_fn=lambda rp, **kw: ProcessResult(rp, ResultKind.ENVIRONMENTAL_FAILURE, reason="x"))
    svc._sleep = svc._stop.wait  # real stop-aware wait, not the FakeClock seam

    def stop_soon():
        time.sleep(0.05)
        svc.stop()

    t = threading.Thread(target=stop_soon)
    t.start()
    start = time.monotonic()
    result = svc.process_with_retry("a.pdf")
    elapsed = time.monotonic() - start
    t.join()
    svc.close()
    assert result.kind is ResultKind.CANCELLED
    # initial_seconds is 10s; interruption must return well before that.
    assert elapsed < 5.0


def test_heartbeat_reports_since_last_change(config, caplog):
    clock = FakeClock()
    svc = _service(config, clock=clock)
    clock.t = 42.0
    with caplog.at_level("INFO", logger="scribe_crop.service"):
        svc.heartbeat()
    svc.close()
    assert any("heartbeat" in r.message for r in caplog.records)


def test_run_logs_the_resolved_reader_screen(config, caplog):
    # A deployment that forgot the [reader] table on a different device must be
    # visible in the log rather than silently cropping to the default panel.
    from dataclasses import replace

    from scribe_crop.config import ReaderConfig

    other = replace(
        config, reader=ReaderConfig(screen_width_in=5.0, screen_height_in=7.0)
    )
    svc = _service(other)
    svc.stop()  # run() exits its wait loop immediately
    with caplog.at_level("INFO", logger="scribe_crop.service"):
        svc.run(heartbeat_seconds=3600.0)
    messages = [r.getMessage() for r in caplog.records]
    assert any("reader screen" in m and "5x7 in" in m for m in messages)
