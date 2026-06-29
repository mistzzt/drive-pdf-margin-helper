from scribe_crop.fingerprint import (
    ToolVersion,
    compute_fingerprint,
    compute_oversize_fingerprint,
    probe_tool_version,
)

TV = ToolVersion(pdfcropmargins="2.2.1", ghostscript="10.0")


def _fp(pdf, **kw):
    base = dict(
        tool_version=TV,
        profile_token="-p 10",
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


def test_profile_token_changes_fp(tmp_path):
    # The effective profile's argv is the only profile input: any layer change
    # (built-in/drive/sidecar) that alters the flags reaches the key through here.
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    assert _fp(pdf, profile_token="-p 10") != _fp(pdf, profile_token="-p 5")


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


def test_pdf_bytes_match_file_read(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 content")
    assert _fp(pdf) == _fp(pdf, pdf_bytes=b"%PDF-1.4 content")


def test_oversize_fingerprint_differs_from_normal(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 content")
    oversize = compute_oversize_fingerprint(size=pdf.stat().st_size)
    assert oversize != _fp(pdf)


def test_oversize_fingerprint_changes_with_size():
    assert compute_oversize_fingerprint(size=100) != compute_oversize_fingerprint(
        size=200
    )


def test_strip_token_none_is_parity_with_disabled(tmp_path):
    # When stripping is disabled (strip_token=None) the key must be byte-identical
    # to a call without the parameter at all, so disabled files do not re-crop.
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    assert _fp(pdf) == _fp(pdf, strip_token=None)


def test_strip_token_changes_fp(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    assert _fp(pdf) != _fp(pdf, strip_token="v=1;...")


def test_strip_token_value_changes_fp(tmp_path):
    # Bumping DETECTOR_VERSION (or any constant) changes the token and the key.
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    assert _fp(pdf, strip_token="v=1;x") != _fp(pdf, strip_token="v=2;x")


def test_probe_tool_version_runs():
    tv = probe_tool_version()
    # pdfcropmargins is on PATH in the dev shell; gs may or may not be.
    assert tv.pdfcropmargins is None or isinstance(tv.pdfcropmargins, str)
    assert tv.ghostscript is None or isinstance(tv.ghostscript, str)


def test_pdfcropmargins_version_prefers_cli_string(monkeypatch):
    # When the binary is on PATH, the CLI version string is recorded verbatim so
    # the token matches earlier releases (no re-crop of unchanged files on rollout).
    import scribe_crop.fingerprint as fp

    monkeypatch.setattr(fp, "_probe", lambda argv: "pdfCropMargins 2.2.1")
    assert fp.probe_tool_version().pdfcropmargins == "pdfCropMargins 2.2.1"


def test_pdfcropmargins_version_falls_back_to_package(monkeypatch):
    # In the sibling-shim deployment the binary is absent from PATH; the version
    # must still resolve from the installed package (what the shim imports) rather
    # than being None, so a library upgrade still invalidates fingerprints.
    import importlib.metadata

    import scribe_crop.fingerprint as fp

    monkeypatch.setattr(fp, "_probe", lambda argv: None)  # no binaries on PATH
    tv = fp.probe_tool_version()
    assert tv.pdfcropmargins == importlib.metadata.version("pdfcropmargins")
