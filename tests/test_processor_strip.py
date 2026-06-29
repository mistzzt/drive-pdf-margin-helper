"""Processor-level tests for the header/footer strip directive.

These use the fake-runner seam (cloud-free, no real binary) to assert the shim
argv and fingerprint behavior.
"""

from pathlib import Path

import pytest

from scribe_crop.config import ServerConfig
from scribe_crop.crop_shim import STRIP_FLAG
from scribe_crop.detector import DEFAULT_PARAMS
from scribe_crop.fingerprint import ToolVersion, compute_fingerprint
from scribe_crop.processor import ResultKind, RunResult, process_pdf
from scribe_crop.state import StateStore

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


def _drop(config, relpath, data=b"%PDF-1.4 fake"):
    p = config.upload_dir / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _capturing_runner(captured):
    def runner(argv, timeout):
        captured["argv"] = argv
        out_idx = argv.index("-o") + 1
        Path(argv[out_idx]).write_bytes(b"cropped")
        return RunResult(0, "", "")

    return runner


def _run(config, store, relpath, *, drive_crop, runner):
    return process_pdf(
        relpath,
        config=config,
        drive_crop=drive_crop,
        store=store,
        tool_version=TV,
        binary="scribe-crop-shim",
        runner=runner,
    )


def test_strip_flag_in_argv_when_enabled(env):
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    res = _run(
        config, store, "foo.pdf",
        drive_crop={"strip_header_footer": True},
        runner=_capturing_runner(captured),
    )
    assert res.kind is ResultKind.SUCCESS
    argv = captured["argv"]
    assert STRIP_FLAG in argv
    # The directive is not emitted as a pdfcropmargins flag.
    assert "strip_header_footer" not in " ".join(argv)


def test_strip_flag_absent_when_disabled(env):
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    _run(
        config, store, "foo.pdf",
        drive_crop={"strip_header_footer": False},
        runner=_capturing_runner(captured),
    )
    assert STRIP_FLAG not in captured["argv"]


def test_disabled_fingerprint_is_parity_with_no_feature(env):
    config, store = env
    pdf = _drop(config, "foo.pdf", data=b"%PDF-1.4 hello")
    captured = {}
    res = _run(
        config, store, "foo.pdf",
        drive_crop={},  # feature absent -> default off
        runner=_capturing_runner(captured),
    )
    # The recorded key equals the legacy key (no strip token folded in).
    legacy = compute_fingerprint(
        pdf,
        pdf_bytes=pdf.read_bytes(),
        profile_token="-p 10",
        tool_version=TV,
    )
    assert res.fingerprint == legacy


def test_enabled_fingerprint_differs_from_disabled(env):
    config, store = env
    _drop(config, "foo.pdf", data=b"%PDF-1.4 hello")
    captured = {}
    off = _run(
        config, store, "foo.pdf",
        drive_crop={"strip_header_footer": False},
        runner=_capturing_runner(captured),
    )
    on = _run(
        config, store, "foo.pdf",
        drive_crop={"strip_header_footer": True},
        runner=_capturing_runner(captured),
    )
    assert on.fingerprint != off.fingerprint


def test_enabled_fingerprint_folds_detector_token(env):
    config, store = env
    pdf = _drop(config, "foo.pdf", data=b"%PDF-1.4 hello")
    captured = {}
    on = _run(
        config, store, "foo.pdf",
        drive_crop={"strip_header_footer": True},
        runner=_capturing_runner(captured),
    )
    expected = compute_fingerprint(
        pdf,
        pdf_bytes=pdf.read_bytes(),
        profile_token="-p 10",
        tool_version=TV,
        strip_token=DEFAULT_PARAMS.token(),
    )
    assert on.fingerprint == expected


def test_strip_via_sidecar_file_end_to_end(env):
    # The new key supplied through a real <name>.pdf.toml sidecar file must route
    # through _parse_sidecar -> merge_profiles and reach the shim argv.
    config, store = env
    _drop(config, "foo.pdf")
    sidecar = config.upload_dir / "foo.pdf.toml"
    sidecar.write_text("strip_header_footer = true\n")
    captured = {}
    res = _run(
        config, store, "foo.pdf",
        drive_crop={},  # not in drive config; only the sidecar enables it
        runner=_capturing_runner(captured),
    )
    assert res.kind is ResultKind.SUCCESS
    assert STRIP_FLAG in captured["argv"]


def test_enabled_recrops_when_toggled_on(env):
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    # First run disabled -> recorded.
    _run(config, store, "foo.pdf", drive_crop={}, runner=_capturing_runner(captured))
    # Toggling strip on changes the key, so it is not skipped: the binary runs.
    calls = []

    def counting(argv, timeout):
        calls.append(argv)
        out_idx = argv.index("-o") + 1
        Path(argv[out_idx]).write_bytes(b"x")
        return RunResult(0, "", "")

    res = _run(
        config, store, "foo.pdf",
        drive_crop={"strip_header_footer": True},
        runner=counting,
    )
    assert res.kind is ResultKind.SUCCESS
    assert len(calls) == 1  # re-cropped, not skipped


def test_comment_only_change_does_not_recrop(env):
    # The fingerprint keys on argv + strip token, neither of which a comment-only
    # config edit changes, so an unchanged effective profile is skipped.
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    _run(
        config, store, "foo.pdf",
        drive_crop={"strip_header_footer": True, "percent_retain": 10},
        runner=_capturing_runner(captured),
    )
    calls = []

    def counting(argv, timeout):
        calls.append(argv)
        return RunResult(0, "", "")

    res = _run(
        config, store, "foo.pdf",
        drive_crop={"strip_header_footer": True, "percent_retain": 10},
        runner=counting,
    )
    assert res.kind is ResultKind.SKIPPED
    assert calls == []


def test_shim_content_marker_classified_as_content(env):
    from scribe_crop.crop_shim import _CONTENT_MARKER
    from scribe_crop.state import Outcome

    config, store = env
    _drop(config, "bad.pdf", data=b"%PDF broken")

    def runner(argv, timeout):
        return RunResult(2, "", f"{_CONTENT_MARKER} is not a valid pdf")

    res = _run(
        config, store, "bad.pdf",
        drive_crop={"strip_header_footer": True},
        runner=runner,
    )
    assert res.kind is ResultKind.CONTENT_FAILURE
    assert store.get("bad.pdf").outcome is Outcome.CONTENT_FAILURE


def test_shim_env_marker_classified_as_environmental(env):
    from scribe_crop.crop_shim import _ENV_MARKER

    config, store = env
    _drop(config, "foo.pdf")

    def runner(argv, timeout):
        # An environmental marker even though stderr mentions "bounding box":
        # must NOT be a content suppression.
        return RunResult(3, "", f"{_ENV_MARKER} RuntimeError bounding box")

    res = _run(
        config, store, "foo.pdf",
        drive_crop={"strip_header_footer": True},
        runner=runner,
    )
    assert res.kind is ResultKind.ENVIRONMENTAL_FAILURE
    assert store.get("foo.pdf") is None  # not suppressed
