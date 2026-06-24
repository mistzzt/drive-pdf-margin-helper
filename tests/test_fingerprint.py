from scribe_crop.fingerprint import (
    ToolVersion,
    compute_fingerprint,
    probe_tool_version,
)

TV = ToolVersion(pdfcropmargins="2.2.1", ghostscript="10.0")


def _fp(pdf, **kw):
    base = dict(
        sidecar_path=None,
        drive_config_bytes=b"[crop]\np=8",
        tool_version=TV,
        profile_version=1,
    )
    base.update(kw)
    return compute_fingerprint(pdf, **base)


def test_identical_inputs_identical_fp(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 content")
    assert _fp(pdf) == _fp(pdf)


def test_changed_pdf_changes_fp(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"one")
    f1 = _fp(pdf)
    pdf.write_bytes(b"two")
    assert _fp(pdf) != f1


def test_changed_config_changes_fp(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    assert _fp(pdf, drive_config_bytes=b"a") != _fp(pdf, drive_config_bytes=b"b")


def test_profile_version_changes_fp(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    assert _fp(pdf, profile_version=1) != _fp(pdf, profile_version=2)


def test_tool_version_changes_fp(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    other = ToolVersion(pdfcropmargins="2.2.2", ghostscript="10.0")
    assert _fp(pdf, tool_version=other) != _fp(pdf)


def test_ghostscript_version_changes_fp(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    other = ToolVersion(pdfcropmargins="2.2.1", ghostscript="9.9")
    assert _fp(pdf, tool_version=other) != _fp(pdf)


def test_sidecar_presence_changes_fp(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    sidecar = tmp_path / "a.pdf.toml"
    sidecar.write_text("percent_retain = 15\n")
    assert _fp(pdf, sidecar_path=sidecar) != _fp(pdf, sidecar_path=None)


def test_changed_sidecar_changes_fp(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    sidecar = tmp_path / "a.pdf.toml"
    sidecar.write_text("percent_retain = 15\n")
    f1 = _fp(pdf, sidecar_path=sidecar)
    sidecar.write_text("percent_retain = 20\n")
    assert _fp(pdf, sidecar_path=sidecar) != f1


def test_missing_sidecar_path_treated_as_absent(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    missing = tmp_path / "gone.pdf.toml"
    assert _fp(pdf, sidecar_path=missing) == _fp(pdf, sidecar_path=None)


def test_probe_tool_version_runs():
    tv = probe_tool_version()
    # pdfcropmargins is on PATH in the dev shell; gs may or may not be.
    assert tv.pdfcropmargins is None or isinstance(tv.pdfcropmargins, str)
    assert tv.ghostscript is None or isinstance(tv.ghostscript, str)
