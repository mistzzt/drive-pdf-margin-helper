"""Shim-level tests: run the real shim against vendored pdfcropmargins/PyMuPDF.

These build small synthetic PDFs in-process with text at known positions and
assert on the resulting CropBox/MediaBox.
"""

import fitz
import pytest

from scribe_crop.crop_shim import (
    EXIT_CONTENT,
    EXIT_ENVIRONMENTAL,
    EXIT_SUCCESS,
    run_crop,
)

W, H = 612.0, 792.0


def _build_doc(
    path,
    *,
    pages=4,
    header=True,
    footer=True,
    body_top=120.0,
    image_only=False,
):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=W, height=H)
        if image_only:
            # A filled rectangle (vector drawing), no text layer.
            page.draw_rect(fitz.Rect(80, 80, 520, 700), fill=(0, 0, 0))
            continue
        if header:
            page.insert_text((100, 50), "Running Header Conf 2024", fontsize=9)
        for j in range(18):
            page.insert_text((100, body_top + j * 25), f"body line {j} pg{i}", fontsize=11)
        if footer:
            page.insert_text((300, 770), f"Page {i + 1}", fontsize=9)
    doc.save(str(path))
    doc.close()


def _mediaboxes(path):
    doc = fitz.open(str(path))
    boxes = [[round(v, 1) for v in page.mediabox] for page in doc]
    doc.close()
    return boxes


def test_disabled_matches_direct_crop(tmp_path):
    src = tmp_path / "in.pdf"
    _build_doc(src)
    shim_out = tmp_path / "shim.pdf"
    direct_out = tmp_path / "direct.pdf"

    rc = run_crop(["-p", "10", "-o", str(shim_out), str(src)], strip=False)
    assert rc == EXIT_SUCCESS

    # Direct pdfcropmargins crop with the same argv (the parity baseline).
    from pdfCropMargins import crop

    crop(argv_list=["-p", "10", "-o", str(direct_out), str(src)], quiet=True)

    assert _mediaboxes(shim_out) == _mediaboxes(direct_out)


def test_strip_trims_header_and_footer(tmp_path):
    src = tmp_path / "in.pdf"
    _build_doc(src)
    nostrip = tmp_path / "nostrip.pdf"
    strip = tmp_path / "strip.pdf"
    run_crop(["-p", "10", "-o", str(nostrip), str(src)], strip=False)
    rc = run_crop(["-p", "10", "-o", str(strip), str(src)], strip=True)
    assert rc == EXIT_SUCCESS

    ns = _mediaboxes(nostrip)
    st = _mediaboxes(strip)
    for n, s in zip(ns[1:], st[1:]):  # skip the title page
        # Top is lower (header removed) and bottom is higher (footer removed).
        assert s[3] < n[3]
        assert s[1] > n[1]
        # Left/right are untouched.
        assert s[0] == n[0]
        assert s[2] == n[2]


def test_strip_no_band_equals_whitespace_crop(tmp_path):
    # No running header/footer: strip-enabled output equals the whitespace crop.
    src = tmp_path / "in.pdf"
    _build_doc(src, header=False, footer=False)
    nostrip = tmp_path / "nostrip.pdf"
    strip = tmp_path / "strip.pdf"
    run_crop(["-p", "10", "-o", str(nostrip), str(src)], strip=False)
    run_crop(["-p", "10", "-o", str(strip), str(src)], strip=True)
    assert _mediaboxes(strip) == _mediaboxes(nostrip)


def test_strip_image_only_pdf_is_whitespace_crop(tmp_path):
    src = tmp_path / "in.pdf"
    _build_doc(src, image_only=True)
    nostrip = tmp_path / "nostrip.pdf"
    strip = tmp_path / "strip.pdf"
    run_crop(["-p", "10", "-o", str(nostrip), str(src)], strip=False)
    rc = run_crop(["-p", "10", "-o", str(strip), str(src)], strip=True)
    assert rc == EXIT_SUCCESS
    assert _mediaboxes(strip) == _mediaboxes(nostrip)


def test_retain_precompensation_lands_at_cut(tmp_path):
    # The published top edge must equal the detector cut (header excluded), and
    # left/right retain must match the no-strip case exactly.
    from scribe_crop.detector import DEFAULT_PARAMS, detect_bands, page_text_from_mupdf

    src = tmp_path / "in.pdf"
    _build_doc(src)

    # Compute the expected cut the way the wrapper does (on the un-rotated doc).
    doc = fitz.open(str(src))
    pages = [page_text_from_mupdf(p, H, W) for p in doc]
    doc.close()
    result = detect_bands(pages, DEFAULT_PARAMS)
    expected_top = H - result.top.dist  # y-flip; full-page origin is 0 here

    strip = tmp_path / "strip.pdf"
    nostrip = tmp_path / "nostrip.pdf"
    run_crop(["-p", "25", "-o", str(strip), str(src)], strip=True)
    run_crop(["-p", "25", "-o", str(nostrip), str(src)], strip=True)

    sdoc = fitz.open(str(strip))
    # Body page top edge lands at the cut (within rounding).
    assert abs(sdoc[1].mediabox.y1 - expected_top) < 0.5
    sdoc.close()

    # Left/right retain matches the no-strip case (compare against whitespace-only).
    ws = tmp_path / "ws.pdf"
    run_crop(["-p", "25", "-o", str(ws), str(src)], strip=False)
    smb = _mediaboxes(strip)
    wmb = _mediaboxes(ws)
    for s, w in zip(smb[1:], wmb[1:]):
        assert s[0] == w[0]
        assert s[2] == w[2]


def test_uniform_single_document_wide_cut(tmp_path):
    # With -u every page shares one box, and the band is still removed (the top is
    # below the header band, not flattened back over it).
    src = tmp_path / "in.pdf"
    _build_doc(src)
    strip = tmp_path / "strip.pdf"
    nostrip = tmp_path / "nostrip.pdf"
    rc = run_crop(["-p", "10", "-u", "-o", str(strip), str(src)], strip=True)
    assert rc == EXIT_SUCCESS
    run_crop(["-p", "10", "-u", "-o", str(nostrip), str(src)], strip=False)

    st = _mediaboxes(strip)
    # All pages share one box (uniform).
    assert all(b == st[0] for b in st)
    # The strip top is below the no-strip top (header removed).
    assert st[0][3] < _mediaboxes(nostrip)[0][3]


def test_title_page_preserved_byte_for_byte(tmp_path):
    # The title page has a large title near the very top and does not vote in
    # detection; the strip output for it must equal the whitespace-only crop
    # exactly (its top content is never clipped to the recurring header cut).
    src = tmp_path / "in.pdf"
    doc = fitz.open()
    # Page 0: title page with a big title block near the top, no running header.
    title = doc.new_page(width=W, height=H)
    title.insert_text((100, 40), "A GRAND TITLE NEAR THE TOP", fontsize=22)
    for j in range(18):
        title.insert_text((100, 200 + j * 25), f"abstract line {j}", fontsize=11)
    # Pages 1..: ordinary body pages with a recurring header and footer.
    for i in range(4):
        page = doc.new_page(width=W, height=H)
        page.insert_text((100, 50), "Running Header Conf 2024", fontsize=9)
        for j in range(18):
            page.insert_text((100, 120 + j * 25), f"body line {j} pg{i}", fontsize=11)
        page.insert_text((300, 770), f"Page {i + 1}", fontsize=9)
    doc.save(str(src))
    doc.close()

    nostrip = tmp_path / "nostrip.pdf"
    strip = tmp_path / "strip.pdf"
    run_crop(["-p", "10", "-o", str(nostrip), str(src)], strip=False)
    rc = run_crop(["-p", "10", "-o", str(strip), str(src)], strip=True)
    assert rc == EXIT_SUCCESS

    ns = _mediaboxes(nostrip)
    st = _mediaboxes(strip)
    # Title page (index 0) is identical to the whitespace-only crop.
    assert st[0] == ns[0]
    # Body pages are still trimmed (header removed).
    assert st[1][3] < ns[1][3]


def test_figure_top_interior_page_preserved(tmp_path):
    # An interior page whose top is a tall figure (no header) must not be clipped
    # to the recurring header cut; it equals the whitespace-only crop.
    src = tmp_path / "in.pdf"
    doc = fitz.open()
    for i in range(5):
        page = doc.new_page(width=W, height=H)
        if i == 2:
            # Figure-top page: a tall filled block at the top, no header line.
            page.draw_rect(fitz.Rect(60, 30, 560, 300), fill=(0, 0, 0))
            for j in range(8):
                page.insert_text((100, 360 + j * 25), f"caption {j}", fontsize=11)
        else:
            page.insert_text((100, 50), "Running Header Conf 2024", fontsize=9)
            for j in range(18):
                page.insert_text((100, 120 + j * 25), f"body line {j} pg{i}", fontsize=11)
        page.insert_text((300, 770), f"Page {i + 1}", fontsize=9)
    doc.save(str(src))
    doc.close()

    nostrip = tmp_path / "nostrip.pdf"
    strip = tmp_path / "strip.pdf"
    run_crop(["-p", "10", "-o", str(nostrip), str(src)], strip=False)
    rc = run_crop(["-p", "10", "-o", str(strip), str(src)], strip=True)
    assert rc == EXIT_SUCCESS

    ns = _mediaboxes(nostrip)
    st = _mediaboxes(strip)
    # The figure-top page (index 2) top is NOT clipped: equals whitespace crop.
    assert st[2][3] == ns[2][3]
    # A header-bearing body page is still trimmed at the top.
    assert st[1][3] < ns[1][3]


def test_strip_with_top_absolute4(tmp_path):
    # absolute4 on the stripped top edge is folded into the inversion, so the cut
    # is independent of a_top: running with -a4 0 0 0 6 lands the stripped top at
    # the same y as -a4 0 0 0 0. This fails if the a_top term is dropped from the
    # inversion in _tighten_top.
    src = tmp_path / "in.pdf"
    _build_doc(src)
    with_off = tmp_path / "with.pdf"
    no_off = tmp_path / "no.pdf"
    rc = run_crop(
        ["-p", "10", "-a4", "0", "0", "0", "6", "-o", str(with_off), str(src)],
        strip=True,
    )
    assert rc == EXIT_SUCCESS
    rc = run_crop(
        ["-p", "10", "-a4", "0", "0", "0", "0", "-o", str(no_off), str(src)],
        strip=True,
    )
    assert rc == EXIT_SUCCESS
    mb_with = _mediaboxes(with_off)
    mb_no = _mediaboxes(no_off)
    # Header still removed.
    assert mb_with[1][3] < H - 30
    # The fold neutralizes the a_top offset: the stripped top lands at the same y.
    assert abs(mb_with[1][3] - mb_no[1][3]) < 0.5


def test_lossless_no_rerender(tmp_path):
    # The crop only shrinks the box; the page content stream is unchanged.
    src = tmp_path / "in.pdf"
    _build_doc(src)
    out = tmp_path / "out.pdf"
    run_crop(["-p", "10", "-o", str(out), str(src)], strip=True)

    sdoc = fitz.open(str(src))
    odoc = fitz.open(str(out))
    src_stream = sdoc[1].read_contents()
    out_stream = odoc[1].read_contents()
    sdoc.close()
    odoc.close()
    assert src_stream == out_stream


def test_content_failure_on_corrupt_input(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 not really a pdf at all")
    out = tmp_path / "out.pdf"
    rc = run_crop(["-p", "10", "-o", str(out), str(bad)], strip=True)
    assert rc == EXIT_CONTENT


def test_nonzero_exit_with_env_stderr_is_environmental(tmp_path, monkeypatch):
    # A nonzero pdfcropmargins exit whose stderr is NOT a content pattern (e.g. a
    # missing/failed Ghostscript) must be environmental (retryable), not a
    # permanent content suppression.
    import pdfCropMargins

    def fake_crop(argv_list, quiet):
        return (None, 1, "", "No Ghostscript executable was found")

    # run_crop does `from pdfCropMargins import crop` at call time.
    monkeypatch.setattr(pdfCropMargins, "crop", fake_crop, raising=False)
    rc = run_crop(["-gs", "-o", str(tmp_path / "o.pdf"), str(tmp_path / "i.pdf")], strip=False)
    assert rc == EXIT_ENVIRONMENTAL


def test_nonzero_exit_with_content_stderr_is_content(tmp_path, monkeypatch):
    import pdfCropMargins

    def fake_crop(argv_list, quiet):
        return (None, 1, "", "Error: input is not a valid PDF document")

    monkeypatch.setattr(pdfCropMargins, "crop", fake_crop, raising=False)
    rc = run_crop(["-o", str(tmp_path / "o.pdf"), str(tmp_path / "i.pdf")], strip=False)
    assert rc == EXIT_CONTENT


def test_detector_error_is_environmental(tmp_path, monkeypatch):
    # A forced detector bug on a valid input must be environmental (retryable),
    # not a content suppression.
    src = tmp_path / "in.pdf"
    _build_doc(src)

    import scribe_crop.crop_shim as shim_mod

    def boom(*a, **k):
        raise RuntimeError("detector blew up")

    monkeypatch.setattr(shim_mod, "detect_bands", boom)
    out = tmp_path / "out.pdf"
    rc = run_crop(["-p", "10", "-o", str(out), str(src)], strip=True)
    assert rc == EXIT_ENVIRONMENTAL


def test_disabled_run_does_not_install_patch(tmp_path):
    import pdfCropMargins.main_pdfCropMargins as main_mod

    before = main_mod.get_bounding_box_list
    src = tmp_path / "in.pdf"
    _build_doc(src)
    out = tmp_path / "out.pdf"
    run_crop(["-p", "10", "-o", str(out), str(src)], strip=False)
    assert main_mod.get_bounding_box_list is before


def test_enabled_run_restores_patch(tmp_path):
    import pdfCropMargins.main_pdfCropMargins as main_mod

    before = main_mod.get_bounding_box_list
    src = tmp_path / "in.pdf"
    _build_doc(src)
    out = tmp_path / "out.pdf"
    run_crop(["-p", "10", "-o", str(out), str(src)], strip=True)
    assert main_mod.get_bounding_box_list is before


def test_main_parses_strip_flag(tmp_path, monkeypatch):
    captured = {}

    import scribe_crop.crop_shim as shim_mod

    def fake_run_crop(crop_argv, *, strip, params=None):
        captured["argv"] = list(crop_argv)
        captured["strip"] = strip
        return 0

    monkeypatch.setattr(shim_mod, "run_crop", fake_run_crop)
    rc = shim_mod.main(["--strip-header-footer", "-p", "10", "-o", "x", "y"])
    assert rc == 0
    assert captured["strip"] is True
    assert captured["argv"] == ["-p", "10", "-o", "x", "y"]


def test_main_without_strip_flag(tmp_path, monkeypatch):
    captured = {}
    import scribe_crop.crop_shim as shim_mod

    monkeypatch.setattr(
        shim_mod,
        "run_crop",
        lambda crop_argv, *, strip, params=None: captured.update(strip=strip) or 0,
    )
    shim_mod.main(["-p", "10", "-o", "x", "y"])
    assert captured["strip"] is False


def test_main_does_not_treat_option_value_as_strip_flag(monkeypatch):
    # A pdfcropmargins option value equal to the shim flag (e.g. a password) must
    # be forwarded verbatim, not consumed as the shim's own flag.
    captured = {}
    import scribe_crop.crop_shim as shim_mod

    def fake_run_crop(crop_argv, *, strip, params=None):
        captured["argv"] = list(crop_argv)
        captured["strip"] = strip
        return 0

    monkeypatch.setattr(shim_mod, "run_crop", fake_run_crop)
    rc = shim_mod.main(["-pw", "--strip-header-footer", "-o", "x", "y"])
    assert rc == 0
    assert captured["strip"] is False
    assert captured["argv"] == ["-pw", "--strip-header-footer", "-o", "x", "y"]


@pytest.mark.parametrize("flag", ["-u", "-s"])
def test_uniform_and_samesize_remove_band(tmp_path, flag):
    # Both -u and -s take the single document-wide strip cut path; the band is
    # removed (top below the header, bottom above the footer), not flattened back
    # over it. (-u additionally makes every box identical; -s only collapses the
    # page size, so it is not asserted here.)
    src = tmp_path / "in.pdf"
    _build_doc(src)
    strip = tmp_path / "strip.pdf"
    nostrip = tmp_path / "nostrip.pdf"
    rc = run_crop(["-p", "10", flag, "-o", str(strip), str(src)], strip=True)
    assert rc == EXIT_SUCCESS
    run_crop(["-p", "10", flag, "-o", str(nostrip), str(src)], strip=False)
    st = _mediaboxes(strip)
    ns = _mediaboxes(nostrip)
    for s, n in zip(st[1:], ns[1:]):
        assert s[3] < n[3]  # header removed
        assert s[1] > n[1]  # footer removed


def test_uniform_strips_with_safe_headerless_title_page(tmp_path):
    # Title page has no running header but its content sits BELOW the body header
    # band, so the uniform cut removes the header from body pages without clipping
    # the title. The feature must NOT over-abstain just because one page lacks the
    # band.
    src = tmp_path / "in.pdf"
    doc = fitz.open()
    title = doc.new_page(width=W, height=H)
    title.insert_text((100, 160), "A GRAND TITLE", fontsize=20)  # below the band
    for j in range(15):
        title.insert_text((100, 230 + j * 25), f"abstract {j}", fontsize=11)
    for i in range(4):
        page = doc.new_page(width=W, height=H)
        page.insert_text((100, 50), "Running Header Conf 2024", fontsize=9)
        for j in range(18):
            page.insert_text((100, 120 + j * 25), f"body {j} pg{i}", fontsize=11)
    doc.save(str(src))
    doc.close()

    strip = tmp_path / "s.pdf"
    nostrip = tmp_path / "n.pdf"
    assert run_crop(["-p", "10", "-u", "-o", str(strip), str(src)], strip=True) == EXIT_SUCCESS
    run_crop(["-p", "10", "-u", "-o", str(nostrip), str(src)], strip=False)
    st = _mediaboxes(strip)
    ns = _mediaboxes(nostrip)
    assert all(b == st[0] for b in st)  # uniform: one box for all pages
    assert st[0][3] < ns[0][3] - 3.0  # header band actually trimmed


def test_uniform_abstains_when_cut_would_clip_nonband_page(tmp_path):
    # Title page content sits ABOVE the body header band. Under -u one box is
    # shared, so trimming to the band would clip the title; the feature must
    # abstain (leave the plain whitespace -u crop) rather than damage the title.
    src = tmp_path / "in.pdf"
    doc = fitz.open()
    title = doc.new_page(width=W, height=H)
    title.insert_text((100, 30), "A GRAND TITLE", fontsize=20)  # above the band
    for j in range(15):
        title.insert_text((100, 230 + j * 25), f"abstract {j}", fontsize=11)
    for i in range(4):
        page = doc.new_page(width=W, height=H)
        page.insert_text((100, 70), "Running Header Conf 2024", fontsize=9)
        for j in range(18):
            page.insert_text((100, 130 + j * 25), f"body {j} pg{i}", fontsize=11)
    doc.save(str(src))
    doc.close()

    strip = tmp_path / "s.pdf"
    nostrip = tmp_path / "n.pdf"
    assert run_crop(["-p", "10", "-u", "-o", str(strip), str(src)], strip=True) == EXIT_SUCCESS
    run_crop(["-p", "10", "-u", "-o", str(nostrip), str(src)], strip=False)
    # Abstained: identical to the plain -u crop, title not clipped.
    assert _mediaboxes(strip) == _mediaboxes(nostrip)


def test_uniform_header_only_keeps_footer_retain(tmp_path):
    # Header on every page, no footer. Under -u the footer edge abstains, so its
    # retain must be preserved (bottom unchanged vs no-strip), not zeroed.
    src = tmp_path / "in.pdf"
    _build_doc(src, footer=False)
    strip = tmp_path / "s.pdf"
    nostrip = tmp_path / "n.pdf"
    assert run_crop(["-p", "10", "-u", "-o", str(strip), str(src)], strip=True) == EXIT_SUCCESS
    run_crop(["-p", "10", "-u", "-o", str(nostrip), str(src)], strip=False)
    st = _mediaboxes(strip)
    ns = _mediaboxes(nostrip)
    assert st[0][3] < ns[0][3]  # header trimmed
    assert abs(st[0][1] - ns[0][1]) < 0.6  # footer edge retain preserved (abstained)


def test_samesize_preserves_headerless_page_retain(tmp_path):
    # Under -s (pages cropped independently) a page that does not exhibit the band
    # (a title page) must be left exactly as the whitespace-only crop, retain
    # included, while body headers are still removed. (Regression: routing -s
    # through the global retain-0 path zeroed the title page's margin.)
    src = tmp_path / "in.pdf"
    doc = fitz.open()
    title = doc.new_page(width=W, height=H)  # no running header
    title.insert_text((100, 90), "A GRAND TITLE", fontsize=20)
    for j in range(18):
        title.insert_text((100, 200 + j * 25), f"abstract {j}", fontsize=11)
    for i in range(4):
        page = doc.new_page(width=W, height=H)
        page.insert_text((100, 50), "Running Header Conf 2024", fontsize=9)
        for j in range(18):
            page.insert_text((100, 120 + j * 25), f"body {j} pg{i}", fontsize=11)
    doc.save(str(src))
    doc.close()

    strip = tmp_path / "s.pdf"
    nostrip = tmp_path / "n.pdf"
    assert run_crop(["-p", "10", "-s", "-o", str(strip), str(src)], strip=True) == EXIT_SUCCESS
    run_crop(["-p", "10", "-s", "-o", str(nostrip), str(src)], strip=False)
    st = _mediaboxes(strip)
    ns = _mediaboxes(nostrip)
    assert st[0] == ns[0]  # title page identical to whitespace-only crop (retain kept)
    assert st[1][3] < ns[1][3]  # body header still removed


def test_strip_respects_page_range(tmp_path):
    # With -g 2-4 pdfcropmargins crops only pages 2..4, so the strip wrapper must
    # not modify page 1 even though it carries the recurring header.
    src = tmp_path / "in.pdf"
    _build_doc(src, pages=4)  # header on every page
    strip = tmp_path / "s.pdf"
    assert (
        run_crop(["-p", "10", "-g", "2-4", "-o", str(strip), str(src)], strip=True)
        == EXIT_SUCCESS
    )
    mb = _mediaboxes(strip)
    assert mb[0] == [0.0, 0.0, W, H]  # page 1 excluded from -g, left untouched
    assert mb[1][3] < H  # an in-range page had its header removed


def test_high_retain_bottom_falls_back_to_valid_box(tmp_path):
    # A large percent_retain drives the pre-compensation out of range; the wrapper
    # must fall back (retain 0 on that edge) and never emit an invalid/out-of-page
    # box.
    src = tmp_path / "in.pdf"
    _build_doc(src)  # header + footer on every page
    strip = tmp_path / "s.pdf"
    assert run_crop(["-p", "96", "-o", str(strip), str(src)], strip=True) == EXIT_SUCCESS
    mb = _mediaboxes(strip)
    for x0, y0, x1, y1 in mb:
        assert 0.0 <= y0 < y1 <= H  # every box valid and within the page
    nostrip = tmp_path / "n.pdf"
    run_crop(["-p", "96", "-o", str(nostrip), str(src)], strip=False)
    nmb = _mediaboxes(nostrip)
    assert mb[1][1] > nmb[1][1]  # footer still removed on a body page


def test_rotated_pages_with_asymmetric_retain_abstains(tmp_path):
    # pdfcropmargins rotates percentRetain4/absoluteOffset4 per page; our inversion
    # uses the unrotated values, so with asymmetric retain AND a rotated page the
    # cut could land wrong. The shim must abstain (identical to the no-strip crop)
    # rather than mis-place the cut.
    src = tmp_path / "in.pdf"
    doc = fitz.open()
    for i in range(5):  # portrait body pages with a running header -> band confirms
        page = doc.new_page(width=W, height=H)
        page.insert_text((100, 50), "Running Header Conf 2024", fontsize=9)
        for j in range(18):
            page.insert_text((100, 120 + j * 25), f"body {j} pg{i}", fontsize=11)
    rot = doc.new_page(width=W, height=H)  # one rotated page
    rot.insert_text((100, 120), "rotated content", fontsize=11)
    rot.set_rotation(90)
    doc.save(str(src))
    doc.close()

    p4 = ["-p4", "10", "20", "10", "5"]  # asymmetric L B R T
    strip = tmp_path / "s.pdf"
    nostrip = tmp_path / "n.pdf"
    assert run_crop([*p4, "-o", str(strip), str(src)], strip=True) == EXIT_SUCCESS
    run_crop([*p4, "-o", str(nostrip), str(src)], strip=False)
    assert _mediaboxes(strip) == _mediaboxes(nostrip)  # abstained, nothing mis-cropped
