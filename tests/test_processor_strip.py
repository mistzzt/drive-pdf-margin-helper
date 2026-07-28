"""Processor-level tests for the shim directives, via the fake-runner seam;
the geometric acceptance criteria live in test_crop_shim.py against the tool."""

from dataclasses import replace
from pathlib import Path

import pytest

from scribe_crop.config import ReaderConfig, ServerConfig
from scribe_crop.crop_shim import FIT_FLAG, STRIP_FLAG, ReaderFit
from scribe_crop.detector import DEFAULT_PARAMS
from scribe_crop.fingerprint import ToolVersion, compute_fingerprint
from scribe_crop.processor import (
    ResultKind,
    RunResult,
    process_pdf,
    resolve_reader_fit,
)
from scribe_crop.profile import BUILTIN_PROFILE
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


def test_disabled_fingerprint_omits_the_strip_token(env):
    config, store = env
    pdf = _drop(config, "foo.pdf", data=b"%PDF-1.4 hello")
    captured = {}
    res = _run(
        config, store, "foo.pdf",
        drive_crop={},  # feature absent -> default off
        runner=_capturing_runner(captured),
    )
    # No strip token is folded in; the fit token is (reader-fit defaults on).
    expected = compute_fingerprint(
        pdf,
        pdf_bytes=pdf.read_bytes(),
        profile_token="-p 10",
        tool_version=TV,
        fit_token=resolve_reader_fit(BUILTIN_PROFILE, config).token(),
    )
    assert res.fingerprint == expected


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
        fit_token=resolve_reader_fit(BUILTIN_PROFILE, config).token(),
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


# --------------------------------------------------------------------------
# Reader-fit directive plumbing and fingerprint rule
# --------------------------------------------------------------------------


def _fit_payload(argv):
    return argv[argv.index(FIT_FLAG) + 1]


def test_fit_directive_in_argv_by_default(env):
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    res = _run(config, store, "foo.pdf", drive_crop={}, runner=_capturing_runner(captured))
    assert res.kind is ResultKind.SUCCESS
    argv = captured["argv"]
    assert FIT_FLAG in argv
    fit = ReaderFit.parse(_fit_payload(argv))
    assert fit.scope == "document"
    assert fit.reader is True
    assert fit.max_scale == 1.15
    # The screen geometry comes from the server config, not the crop profile.
    assert fit.screen_w_pt == config.reader.screen_width_pt
    assert fit.screen_h_pt == config.reader.screen_height_pt
    # The fit_* keys never reach the pdfcropmargins argv.
    assert not any(a.startswith("fit_") for a in argv)


def test_fit_directive_absent_when_nothing_would_change(env):
    # Page scope with the floor off is what pdfcropmargins does natively, so the
    # shim stays a pass-through and the argv is the bare-tool argv.
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    _run(
        config, store, "foo.pdf",
        drive_crop={"fit_reader": False, "fit_scope": "page"},
        runner=_capturing_runner(captured),
    )
    assert FIT_FLAG not in captured["argv"]


def test_fit_directive_carries_sidecar_overrides(env):
    config, store = env
    _drop(config, "foo.pdf")
    sidecar = config.upload_dir / "foo.pdf.toml"
    sidecar.write_text('fit_scope = "page"\nfit_max_scale = 1.4\n')
    captured = {}
    _run(config, store, "foo.pdf", drive_crop={}, runner=_capturing_runner(captured))
    fit = ReaderFit.parse(_fit_payload(captured["argv"]))
    assert fit.scope == "page"
    assert fit.max_scale == 1.4


def test_reader_dimensions_reach_the_directive(env, tmp_path):
    # A different [reader] table changes the directive payload (and the key).
    config, store = env
    _drop(config, "foo.pdf")
    other = replace(
        config, reader=ReaderConfig(screen_width_in=5.0, screen_height_in=7.0)
    )
    captured = {}
    process_pdf(
        "foo.pdf",
        config=other,
        drive_crop={},
        store=store,
        tool_version=TV,
        binary="scribe-crop-shim",
        runner=_capturing_runner(captured),
    )
    fit = ReaderFit.parse(_fit_payload(captured["argv"]))
    assert fit.screen_w_pt == 360.0
    assert fit.screen_h_pt == 504.0


def test_fit_max_scale_change_recrops_when_reader_is_on(env):
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    a = _run(config, store, "foo.pdf", drive_crop={}, runner=_capturing_runner(captured))
    b = _run(
        config, store, "foo.pdf",
        drive_crop={"fit_max_scale": 1.4},
        runner=_capturing_runner(captured),
    )
    assert a.fingerprint != b.fingerprint


def test_fit_max_scale_change_does_not_recrop_when_reader_is_off(env):
    # The key must move iff the output would change: with the floor off,
    # fit_max_scale is inert.
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    a = _run(
        config, store, "foo.pdf",
        drive_crop={"fit_reader": False},
        runner=_capturing_runner(captured),
    )
    b = _run(
        config, store, "foo.pdf",
        drive_crop={"fit_reader": False, "fit_max_scale": 2.5},
        runner=_capturing_runner(captured),
    )
    assert a.fingerprint == b.fingerprint


def test_screen_size_change_does_not_recrop_when_reader_is_off(env):
    config, store = env
    _drop(config, "foo.pdf")
    other = replace(
        config, reader=ReaderConfig(screen_width_in=5.0, screen_height_in=7.0)
    )
    captured = {}

    def run(cfg):
        return process_pdf(
            "foo.pdf",
            config=cfg,
            drive_crop={"fit_reader": False},
            store=store,
            tool_version=TV,
            binary="scribe-crop-shim",
            runner=_capturing_runner(captured),
        )

    assert run(config).fingerprint == run(other).fingerprint


def test_screen_size_change_recrops_when_reader_is_on(env):
    config, store = env
    _drop(config, "foo.pdf")
    other = replace(
        config, reader=ReaderConfig(screen_width_in=5.0, screen_height_in=7.0)
    )
    captured = {}

    def run(cfg):
        return process_pdf(
            "foo.pdf",
            config=cfg,
            drive_crop={},
            store=store,
            tool_version=TV,
            binary="scribe-crop-shim",
            runner=_capturing_runner(captured),
        )

    assert run(config).fingerprint != run(other).fingerprint


def test_fit_scope_change_recrops_even_with_reader_off(env):
    # Scope changes geometry independently of the floor.
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    a = _run(
        config, store, "foo.pdf",
        drive_crop={"fit_reader": False, "fit_scope": "page"},
        runner=_capturing_runner(captured),
    )
    b = _run(
        config, store, "foo.pdf",
        drive_crop={"fit_reader": False, "fit_scope": "document"},
        runner=_capturing_runner(captured),
    )
    assert a.fingerprint != b.fingerprint


def test_exclude_first_page_change_recrops_under_document_scope(env):
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    a = _run(config, store, "foo.pdf", drive_crop={}, runner=_capturing_runner(captured))
    b = _run(
        config, store, "foo.pdf",
        drive_crop={"fit_exclude_first_page": False},
        runner=_capturing_runner(captured),
    )
    assert a.fingerprint != b.fingerprint


def test_exclude_first_page_is_inert_under_page_scope(env):
    config, store = env
    _drop(config, "foo.pdf")
    captured = {}
    a = _run(
        config, store, "foo.pdf",
        drive_crop={"fit_scope": "page"},
        runner=_capturing_runner(captured),
    )
    b = _run(
        config, store, "foo.pdf",
        drive_crop={"fit_scope": "page", "fit_exclude_first_page": False},
        runner=_capturing_runner(captured),
    )
    assert a.fingerprint == b.fingerprint


def test_directive_payload_matches_the_recorded_fingerprint(env):
    # The command-matches-fingerprint invariant: one resolution drives both, so
    # the payload sent to the shim is exactly what the key folded in.
    config, store = env
    pdf = _drop(config, "foo.pdf", data=b"%PDF-1.4 hello")
    captured = {}
    res = _run(
        config, store, "foo.pdf",
        drive_crop={"fit_max_scale": 1.3},
        runner=_capturing_runner(captured),
    )
    payload = _fit_payload(captured["argv"])
    expected = compute_fingerprint(
        pdf,
        pdf_bytes=pdf.read_bytes(),
        profile_token="-p 10",
        tool_version=TV,
        fit_token=payload,
    )
    assert res.fingerprint == expected


def test_removed_keys_are_a_malformed_profile(env):
    # A sidecar still using uniform/same_size fails validation before any crop.
    config, store = env
    _drop(config, "foo.pdf")
    (config.upload_dir / "foo.pdf.toml").write_text("uniform = true\n")
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return RunResult(0, "", "")

    res = _run(config, store, "foo.pdf", drive_crop={}, runner=runner)
    assert res.kind is ResultKind.CONTENT_FAILURE
    assert "uniform" in res.reason
    assert calls == []  # failed fast, before pdfcropmargins ran
