import pytest

from scribe_crop.config import ServerConfig
from scribe_crop.processor import ProcessResult, ResultKind
from scribe_crop.reconcile import forward_pass, reconcile, reverse_gc
from scribe_crop.state import Outcome, StateStore


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "ScribeCrop"
    for sub in ("upload", "processed", "failed"):
        (root / sub).mkdir(parents=True)
    config = ServerConfig(root=root, state_path=root / "state.db")
    store = StateStore(config.resolved_state_path)
    yield config, store
    store.close()


def _drop(config, relpath, data=b"%PDF"):
    p = config.upload_dir / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _fwd(config, store, process):
    return forward_pass(config, process=process)


def test_forward_processes_new_files(env):
    config, store = env
    _drop(config, "a.pdf")
    _drop(config, "sub/b.pdf")
    seen = []
    report = _fwd(config, store, lambda rp: seen.append(rp) or ProcessResult(rp, ResultKind.SUCCESS))
    assert sorted(seen) == ["a.pdf", "sub/b.pdf"]
    assert sorted(report.processed) == ["a.pdf", "sub/b.pdf"]


def test_forward_routes_skipped_result_to_skipped(env):
    config, store = env
    _drop(config, "a.pdf")
    report = _fwd(config, store, lambda rp: ProcessResult(rp, ResultKind.SKIPPED))
    assert report.skipped == ["a.pdf"]
    assert report.processed == []


def test_forward_routes_non_skipped_result_to_processed(env):
    config, store = env
    _drop(config, "a.pdf")
    _drop(config, "b.pdf")
    report = _fwd(
        config,
        store,
        lambda rp: ProcessResult(
            rp,
            ResultKind.SKIPPED if rp == "a.pdf" else ResultKind.SUCCESS,
        ),
    )
    assert report.skipped == ["a.pdf"]
    assert report.processed == ["b.pdf"]


def test_gc_removes_previously_seen_now_gone_when_mirror_current(env):
    config, store = env
    (config.processed_dir / "a.pdf").write_bytes(b"out")
    (config.failed_dir / "a.pdf").write_bytes(b"failcopy")
    (config.failed_dir / "a.pdf.log").write_text("log")
    store.upsert("a.pdf", "fp", Outcome.SUCCESS)
    report = reverse_gc(config, store=store, mirror_current=True)
    assert report.gc_removed == ["a.pdf"]
    assert not (config.processed_dir / "a.pdf").exists()
    assert not (config.failed_dir / "a.pdf").exists()
    assert not (config.failed_dir / "a.pdf.log").exists()
    assert store.get("a.pdf") is None


def test_gc_removes_malformed_log_with_empty_fingerprint(env):
    config, store = env
    # Mirrors what _record_malformed leaves behind: only a .log (no PDF copy) and
    # an empty-fingerprint CONTENT_FAILURE row whose sole purpose is to let GC
    # remove the orphaned log once the source is deleted.
    (config.failed_dir / "bad.pdf.log").write_text("invalid sidecar: ...")
    store.upsert("bad.pdf", "", Outcome.CONTENT_FAILURE)
    report = reverse_gc(config, store=store, mirror_current=True)
    assert report.gc_removed == ["bad.pdf"]
    assert not (config.failed_dir / "bad.pdf.log").exists()
    assert store.get("bad.pdf") is None


def test_gc_does_not_run_when_mirror_not_current(env):
    config, store = env
    (config.processed_dir / "a.pdf").write_bytes(b"out")
    store.upsert("a.pdf", "fp", Outcome.SUCCESS)
    report = reverse_gc(config, store=store, mirror_current=False)
    assert report.gc_removed == []
    assert (config.processed_dir / "a.pdf").exists()
    assert store.get("a.pdf") is not None


def test_gc_never_touches_still_present_source(env):
    config, store = env
    _drop(config, "a.pdf")
    (config.processed_dir / "a.pdf").write_bytes(b"out")
    store.upsert("a.pdf", "fp", Outcome.SUCCESS)
    report = reverse_gc(config, store=store, mirror_current=True)
    assert report.gc_removed == []
    assert (config.processed_dir / "a.pdf").exists()
    assert store.get("a.pdf") is not None


def test_gc_ignores_absent_source_without_state(env):
    config, store = env
    # An output exists but there is no state record: not a confirmed deletion.
    (config.processed_dir / "orphan.pdf").write_bytes(b"out")
    report = reverse_gc(config, store=store, mirror_current=True)
    assert report.gc_removed == []
    assert (config.processed_dir / "orphan.pdf").exists()


def test_reconcile_combines_forward_and_gc(env):
    config, store = env
    _drop(config, "keep.pdf")
    (config.processed_dir / "gone.pdf").write_bytes(b"out")
    store.upsert("gone.pdf", "fp", Outcome.SUCCESS)
    seen = []
    report = reconcile(
        config,
        store=store,
        process=lambda rp: seen.append(rp) or ProcessResult(rp, ResultKind.SUCCESS),
        mirror_current=True,
    )
    assert seen == ["keep.pdf"]
    assert report.gc_removed == ["gone.pdf"]
    assert not (config.processed_dir / "gone.pdf").exists()


def test_reconcile_gc_off_when_mirror_not_current(env):
    config, store = env
    (config.processed_dir / "gone.pdf").write_bytes(b"out")
    store.upsert("gone.pdf", "fp", Outcome.SUCCESS)
    report = reconcile(
        config,
        store=store,
        process=lambda rp: ProcessResult(rp, ResultKind.SUCCESS),
        mirror_current=False,
    )
    assert report.gc_removed == []
    assert (config.processed_dir / "gone.pdf").exists()
