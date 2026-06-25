import pytest

from scribe_crop.config import ServerConfig
from scribe_crop.fingerprint import ToolVersion
from scribe_crop.processor import (
    BinaryNotFound,
    OutOfMemory,
    ResultKind,
    RunResult,
    RunTimeout,
    classify_run_failure,
    process_pdf,
)
from scribe_crop.state import Outcome, StateStore

TV = ToolVersion(pdfcropmargins="2.2.1", ghostscript="10.0")


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "ScribeCrop"
    for sub in ("upload", "processed", "failed"):
        (root / sub).mkdir(parents=True)
    config = ServerConfig(root=root, state_path=root / "state.db")
    store = StateStore(config.resolved_state_path)
    yield config, store
    store.close()


def _drop_pdf(config, relpath, data=b"%PDF-1.4 fake"):
    p = config.upload_dir / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _run(config, store, relpath, runner, **kw):
    base = dict(
        config=config,
        drive_crop={},
        store=store,
        tool_version=TV,
        binary="pdfcropmargins",
        runner=runner,
    )
    base.update(kw)
    return process_pdf(relpath, **base)


def make_runner(returncode=0, stderr="", output=b"%PDF cropped"):
    def runner(argv, timeout):
        if returncode == 0:
            out_idx = argv.index("-o") + 1
            from pathlib import Path

            Path(argv[out_idx]).write_bytes(output)
        return RunResult(returncode, "", stderr)

    return runner


def test_classify_zero_is_success():
    assert classify_run_failure(RunResult(0, "", "")) is ResultKind.SUCCESS


def test_classify_content_patterns():
    r = RunResult(1, "", "Error: file is encrypted, password required")
    assert classify_run_failure(r) is ResultKind.CONTENT_FAILURE


def test_classify_unknown_nonzero_is_environmental():
    r = RunResult(3, "", "some weird transient error")
    assert classify_run_failure(r) is ResultKind.ENVIRONMENTAL_FAILURE


def test_success_publishes_and_records(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")
    res = _run(config, store, "foo.pdf", make_runner())
    assert res.kind is ResultKind.SUCCESS
    out = config.processed_dir / "foo.pdf"
    assert out.read_bytes() == b"%PDF cropped"
    rec = store.get("foo.pdf")
    assert rec.outcome is Outcome.SUCCESS
    assert rec.fingerprint == res.fingerprint
    # no temp files left behind
    leftover = [p.name for p in config.processed_dir.iterdir() if p.suffix == ".tmp"]
    assert leftover == []


def test_success_clears_stale_failed(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")
    stale = config.failed_dir / "foo.pdf"
    stale.write_bytes(b"old")
    (config.failed_dir / "foo.pdf.log").write_text("old log")
    _run(config, store, "foo.pdf", make_runner())
    assert not stale.exists()
    assert not (config.failed_dir / "foo.pdf.log").exists()


def test_content_failure_routes_to_failed_with_log(env):
    config, store = env
    _drop_pdf(config, "bad.pdf", data=b"%PDF broken")
    runner = make_runner(returncode=1, stderr="No detectable bounding box found")
    res = _run(config, store, "bad.pdf", runner)
    assert res.kind is ResultKind.CONTENT_FAILURE
    failed = config.failed_dir / "bad.pdf"
    assert failed.read_bytes() == b"%PDF broken"  # original copied
    log = (config.failed_dir / "bad.pdf.log").read_text()
    assert "command:" in log
    assert "bounding box" in log.lower()
    assert not (config.processed_dir / "bad.pdf").exists()
    rec = store.get("bad.pdf")
    assert rec.outcome is Outcome.CONTENT_FAILURE


def test_content_failure_suppressed_on_identical_bytes(env):
    config, store = env
    _drop_pdf(config, "bad.pdf", data=b"%PDF broken")
    runner = make_runner(returncode=1, stderr="file is encrypted")
    _run(config, store, "bad.pdf", runner)

    calls = []

    def counting_runner(argv, timeout):
        calls.append(argv)
        return RunResult(1, "", "file is encrypted")

    res = _run(config, store, "bad.pdf", counting_runner)
    assert res.kind is ResultKind.SKIPPED
    assert calls == []  # binary not invoked again


def test_environmental_failure_does_not_record_and_is_retryable(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")

    def runner(argv, timeout):
        raise RunTimeout("timed out")

    res = _run(config, store, "foo.pdf", runner)
    assert res.kind is ResultKind.ENVIRONMENTAL_FAILURE
    assert res.kind.retryable
    assert store.get("foo.pdf") is None
    assert not (config.failed_dir / "foo.pdf").exists()
    assert not (config.processed_dir / "foo.pdf").exists()


def test_missing_binary_is_environmental(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")

    def runner(argv, timeout):
        raise BinaryNotFound("no such file")

    res = _run(config, store, "foo.pdf", runner)
    assert res.kind is ResultKind.ENVIRONMENTAL_FAILURE
    assert store.get("foo.pdf") is None


def test_oom_is_environmental(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")

    def runner(argv, timeout):
        raise OutOfMemory("killed")

    res = _run(config, store, "foo.pdf", runner)
    assert res.kind is ResultKind.ENVIRONMENTAL_FAILURE
    assert store.get("foo.pdf") is None


def test_unknown_nonzero_exit_is_environmental(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")
    runner = make_runner(returncode=42, stderr="mysterious")
    res = _run(config, store, "foo.pdf", runner)
    assert res.kind is ResultKind.ENVIRONMENTAL_FAILURE
    assert store.get("foo.pdf") is None


def test_oversize_input_is_content_failure(env):
    config, store = env
    small_cfg = ServerConfig(
        root=config.root, state_path=config.resolved_state_path, max_input_bytes=4
    )
    _drop_pdf(small_cfg, "big.pdf", data=b"way too many bytes")

    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return RunResult(0, "", "")

    res = _run(small_cfg, store, "big.pdf", runner)
    assert res.kind is ResultKind.CONTENT_FAILURE
    assert calls == []  # never invoked the binary
    assert (small_cfg.failed_dir / "big.pdf").exists()
    assert "max_input_bytes" in (small_cfg.failed_dir / "big.pdf.log").read_text()
    assert store.get("big.pdf").outcome is Outcome.CONTENT_FAILURE


def test_oversize_reprocesses_after_limit_raised(env):
    config, store = env
    data = b"way too many bytes"
    small_cfg = ServerConfig(
        root=config.root, state_path=config.resolved_state_path, max_input_bytes=4
    )
    _drop_pdf(small_cfg, "big.pdf", data=data)

    res = _run(small_cfg, store, "big.pdf", make_runner())
    assert res.kind is ResultKind.CONTENT_FAILURE

    # Operator raises the limit so the file now fits: it must reprocess rather
    # than stay suppressed as a content failure forever.
    big_cfg = ServerConfig(
        root=config.root,
        state_path=config.resolved_state_path,
        max_input_bytes=10_000,
    )
    res2 = _run(big_cfg, store, "big.pdf", make_runner())
    assert res2.kind is ResultKind.SUCCESS
    assert (big_cfg.processed_dir / "big.pdf").exists()
    assert store.get("big.pdf").outcome is Outcome.SUCCESS


def test_oversize_does_not_read_pdf_contents(env):
    config, store = env
    small_cfg = ServerConfig(
        root=config.root, state_path=config.resolved_state_path, max_input_bytes=4
    )
    pdf = _drop_pdf(small_cfg, "big.pdf", data=b"way too many bytes")

    read_calls = []
    orig_read_bytes = type(pdf).read_bytes

    def spy(self):
        read_calls.append(self.name)
        return orig_read_bytes(self)

    import scribe_crop.processor as proc_mod

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(proc_mod.Path, "read_bytes", spy)
    try:
        res = _run(small_cfg, store, "big.pdf", make_runner())
    finally:
        monkeypatched.undo()

    assert res.kind is ResultKind.CONTENT_FAILURE
    # The oversize PDF itself is never read into memory (only the sidecar, if any).
    assert "big.pdf" not in read_calls


def test_temp_output_written_to_scratch_dir_not_synced_dirs(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")
    seen = {}

    def runner(argv, timeout):
        from pathlib import Path

        out = Path(argv[argv.index("-o") + 1])
        seen["parent"] = out.parent
        out.write_bytes(b"cropped")
        return RunResult(0, "", "")

    res = _run(config, store, "foo.pdf", runner)
    assert res.kind is ResultKind.SUCCESS
    assert seen["parent"] == config.tmp_dir
    assert not any(p.suffix == ".tmp" for p in config.processed_dir.rglob("*"))
    assert not any(p.suffix == ".tmp" for p in config.failed_dir.rglob("*"))


def test_oversize_repeat_skips_without_recopy(env):
    config, store = env
    small_cfg = ServerConfig(
        root=config.root, state_path=config.resolved_state_path, max_input_bytes=4
    )
    _drop_pdf(small_cfg, "big.pdf", data=b"way too many bytes")
    r1 = _run(small_cfg, store, "big.pdf", make_runner())
    assert r1.kind is ResultKind.CONTENT_FAILURE

    import scribe_crop.processor as proc_mod

    copies = []
    orig = proc_mod.shutil.copyfile
    mp = pytest.MonkeyPatch()
    mp.setattr(
        proc_mod.shutil,
        "copyfile",
        lambda s, d: copies.append((s, d)) or orig(s, d),
    )
    try:
        r2 = _run(small_cfg, store, "big.pdf", make_runner())
    finally:
        mp.undo()
    assert r2.kind is ResultKind.SKIPPED
    assert copies == []


def test_oversize_republishes_if_failed_artifact_missing(env):
    config, store = env
    small_cfg = ServerConfig(
        root=config.root, state_path=config.resolved_state_path, max_input_bytes=4
    )
    _drop_pdf(small_cfg, "big.pdf", data=b"way too many bytes")
    _run(small_cfg, store, "big.pdf", make_runner())
    (small_cfg.failed_dir / "big.pdf").unlink()
    res = _run(small_cfg, store, "big.pdf", make_runner())
    assert res.kind is ResultKind.CONTENT_FAILURE
    assert (small_cfg.failed_dir / "big.pdf").exists()


def test_sidecar_unknown_key_logs_to_failed_without_pdf_copy(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")
    (config.upload_dir / "foo.pdf.toml").write_text("bogus_key = 5\n")

    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return RunResult(0, "", "")

    res = _run(config, store, "foo.pdf", runner)
    assert res.kind is ResultKind.CONTENT_FAILURE
    assert calls == []  # never invokes the crop tool
    # Malformed input: a .log explains it, but the PDF is not copied to failed/.
    assert not (config.failed_dir / "foo.pdf").exists()
    log = (config.failed_dir / "foo.pdf.log").read_text()
    assert "bogus_key" in log
    rec = store.get("foo.pdf")
    assert rec.outcome is Outcome.CONTENT_FAILURE
    assert rec.fingerprint == ""  # no content-hash suppression


def test_malformed_sidecar_log_is_idempotent_across_passes(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")
    (config.upload_dir / "foo.pdf.toml").write_text("not = valid = toml\n")

    res1 = _run(config, store, "foo.pdf", make_runner())
    log_path = config.failed_dir / "foo.pdf.log"
    mtime1 = log_path.stat().st_mtime_ns
    res2 = _run(config, store, "foo.pdf", make_runner())
    assert res1.kind is res2.kind is ResultKind.CONTENT_FAILURE
    # Re-checked each pass, but the unchanged log is not rewritten (no sync churn).
    assert log_path.stat().st_mtime_ns == mtime1


def test_sidecar_overrides_reach_argv(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")
    (config.upload_dir / "foo.pdf.toml").write_text("percent_retain = 25\n")

    captured = {}

    def runner(argv, timeout):
        captured["argv"] = argv
        out_idx = argv.index("-o") + 1
        from pathlib import Path

        Path(argv[out_idx]).write_bytes(b"out")
        return RunResult(0, "", "")

    _run(config, store, "foo.pdf", runner)
    argv = captured["argv"]
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "25"


def test_subdir_relpaths_preserved_no_collision(env):
    config, store = env
    _drop_pdf(config, "a/foo.pdf", data=b"AAA")
    _drop_pdf(config, "b/foo.pdf", data=b"BBB")
    _run(config, store, "a/foo.pdf", make_runner(output=b"cropA"))
    _run(config, store, "b/foo.pdf", make_runner(output=b"cropB"))
    assert (config.processed_dir / "a" / "foo.pdf").read_bytes() == b"cropA"
    assert (config.processed_dir / "b" / "foo.pdf").read_bytes() == b"cropB"
    assert store.get("a/foo.pdf") is not None
    assert store.get("b/foo.pdf") is not None


def test_changed_bytes_reprocess_after_success(env):
    config, store = env
    _drop_pdf(config, "foo.pdf", data=b"v1")
    r1 = _run(config, store, "foo.pdf", make_runner())
    _drop_pdf(config, "foo.pdf", data=b"v2 different")
    r2 = _run(config, store, "foo.pdf", make_runner())
    assert r2.kind is ResultKind.SUCCESS
    assert r2.fingerprint != r1.fingerprint


def test_skip_when_success_recorded_and_output_present(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")
    _run(config, store, "foo.pdf", make_runner())

    calls = []

    def counting(argv, timeout):
        calls.append(argv)
        return RunResult(0, "", "")

    res = _run(config, store, "foo.pdf", counting)
    assert res.kind is ResultKind.SKIPPED
    assert calls == []


def test_content_failure_clears_stale_processed_output(env):
    config, store = env
    _drop_pdf(config, "foo.pdf", data=b"v1")
    _run(config, store, "foo.pdf", make_runner(output=b"cropped v1"))
    assert (config.processed_dir / "foo.pdf").exists()

    _drop_pdf(config, "foo.pdf", data=b"v2 corrupt")
    runner = make_runner(returncode=1, stderr="file is encrypted")
    res = _run(config, store, "foo.pdf", runner)
    assert res.kind is ResultKind.CONTENT_FAILURE
    assert (config.failed_dir / "foo.pdf").exists()
    assert not (config.processed_dir / "foo.pdf").exists()
    assert store.get("foo.pdf").outcome is Outcome.CONTENT_FAILURE


def test_reprocess_when_success_recorded_but_output_missing(env):
    config, store = env
    _drop_pdf(config, "foo.pdf")
    _run(config, store, "foo.pdf", make_runner())
    (config.processed_dir / "foo.pdf").unlink()
    res = _run(config, store, "foo.pdf", make_runner())
    assert res.kind is ResultKind.SUCCESS
    assert (config.processed_dir / "foo.pdf").exists()
