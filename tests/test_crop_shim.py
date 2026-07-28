"""Shim-level tests against the real vendored pdfcropmargins/PyMuPDF: build
synthetic PDFs with known content positions and assert on the published boxes."""

import fitz
import pytest

from scribe_crop.crop_shim import (
    EXIT_CONTENT,
    EXIT_ENVIRONMENTAL,
    EXIT_SUCCESS,
    ReaderFit,
    run_crop,
)

W, H = 612.0, 792.0

# Colorsoft preset in points; the floor at 1.15 is 413.22 x 550.96 pt.
SCREEN_W, SCREEN_H = 6.6 * 72, 8.8 * 72
MAX_SCALE = 1.15
FLOOR_W = SCREEN_W / MAX_SCALE
FLOOR_H = SCREEN_H / MAX_SCALE

DOC_FIT = ReaderFit(
    scope="document",
    reader=True,
    exclude_first_page=True,
    screen_w_pt=SCREEN_W,
    screen_h_pt=SCREEN_H,
    max_scale=MAX_SCALE,
)
PAGE_FIT = ReaderFit(
    scope="page",
    reader=True,
    screen_w_pt=SCREEN_W,
    screen_h_pt=SCREEN_H,
    max_scale=MAX_SCALE,
)
OFF_FIT = ReaderFit(scope="page", reader=False)


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
            page.insert_text(
                (100, body_top + j * 25), f"body line {j} pg{i}", fontsize=11
            )
        if footer:
            page.insert_text((300, 770), f"Page {i + 1}", fontsize=9)
    doc.save(str(path))
    doc.close()


def _block_doc(path, *, pages, width, height, rect, extra=None, rotate=None):
    """Pages carrying one filled rectangle, so the tight box is exactly known."""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.draw_rect(fitz.Rect(*rect), fill=(0, 0, 0))
        if extra is not None and i == 0:
            page.draw_rect(fitz.Rect(*extra), fill=(0, 0, 0))
        if rotate is not None and i in rotate:
            page.set_rotation(rotate[i])
    doc.save(str(path))
    doc.close()


def _mediaboxes(path):
    doc = fitz.open(str(path))
    boxes = [[round(v, 1) for v in page.mediabox] for page in doc]
    doc.close()
    return boxes


def _dims(box):
    return round(box[2] - box[0], 2), round(box[3] - box[1], 2)


# --------------------------------------------------------------------------
# Pass-through / parity
# --------------------------------------------------------------------------


def test_disabled_matches_direct_crop(tmp_path):
    src = tmp_path / "in.pdf"
    _build_doc(src)
    shim_out = tmp_path / "shim.pdf"
    direct_out = tmp_path / "direct.pdf"

    rc = run_crop(["-p", "10", "-o", str(shim_out), str(src)], strip=False, fit=OFF_FIT)
    assert rc == EXIT_SUCCESS

    # Direct pdfcropmargins crop with the same argv (the parity baseline).
    from pdfCropMargins import crop

    crop(argv_list=["-p", "10", "-o", str(direct_out), str(src)], quiet=True)

    assert _mediaboxes(shim_out) == _mediaboxes(direct_out)


def test_both_features_off_installs_no_patch(tmp_path):
    import pdfCropMargins.main_pdfCropMargins as main_mod

    before = main_mod.get_bounding_box_list
    src = tmp_path / "in.pdf"
    _build_doc(src)
    out = tmp_path / "out.pdf"
    run_crop(["-p", "10", "-o", str(out), str(src)], strip=False, fit=OFF_FIT)
    assert main_mod.get_bounding_box_list is before


def test_no_fit_argument_is_pass_through(tmp_path):
    # fit=None (a strip-only caller) must not activate the pipeline.
    src = tmp_path / "in.pdf"
    _build_doc(src)
    shim_out = tmp_path / "shim.pdf"
    direct_out = tmp_path / "direct.pdf"
    run_crop(["-p", "10", "-o", str(shim_out), str(src)], strip=False)
    from pdfCropMargins import crop

    crop(argv_list=["-p", "10", "-o", str(direct_out), str(src)], quiet=True)
    assert _mediaboxes(shim_out) == _mediaboxes(direct_out)


def test_enabled_run_restores_patch(tmp_path):
    import pdfCropMargins.main_pdfCropMargins as main_mod

    before = main_mod.get_bounding_box_list
    src = tmp_path / "in.pdf"
    _build_doc(src)
    out = tmp_path / "out.pdf"
    run_crop(["-p", "10", "-o", str(out), str(src)], strip=True, fit=DOC_FIT)
    assert main_mod.get_bounding_box_list is before


def test_lossless_no_rerender(tmp_path):
    # The crop only shrinks the box; the page content stream is unchanged.
    src = tmp_path / "in.pdf"
    _build_doc(src)
    out = tmp_path / "out.pdf"
    run_crop(["-p", "10", "-o", str(out), str(src)], strip=True, fit=DOC_FIT)

    sdoc = fitz.open(str(src))
    odoc = fitz.open(str(out))
    src_stream = sdoc[1].read_contents()
    out_stream = odoc[1].read_contents()
    sdoc.close()
    odoc.close()
    assert src_stream == out_stream


# --------------------------------------------------------------------------
# Reader-fit acceptance criteria (geometric, against the real tool)
# --------------------------------------------------------------------------


def test_letter_paper_floor_does_not_bind(tmp_path):
    # 612x792 with a 468x666 content block: min(475.2/468, 633.6/666) = 0.95,
    # under the 1.15 cap, so nothing grows and every cohort page is identical.
    src = tmp_path / "letter.pdf"
    _block_doc(src, pages=6, width=W, height=H, rect=(72, 63, 540, 729))
    out = tmp_path / "out.pdf"
    assert (
        run_crop(["-p", "0", "-o", str(out), str(src)], strip=False, fit=DOC_FIT)
        == EXIT_SUCCESS
    )
    boxes = _mediaboxes(out)
    assert all(b == boxes[0] for b in boxes)  # one shared box
    width, height = _dims(boxes[0])
    # Tight around the block (plus a point of render rounding), not grown.
    assert 465.0 < width < 475.0
    assert 663.0 < height < 673.0
    assert min(SCREEN_W / width, SCREEN_H / height) < MAX_SCALE


def test_a5_book_height_binds_width_untouched(tmp_path):
    # 320x500 block: 633.6/500 = 1.27 > 1.15, so the height grows to 550.96;
    # the non-binding width buys no whitespace, so scale lands exactly at the cap.
    src = tmp_path / "a5.pdf"
    _block_doc(src, pages=6, width=420, height=595, rect=(50, 47.5, 370, 547.5))
    out = tmp_path / "out.pdf"
    assert (
        run_crop(["-p", "0", "-o", str(out), str(src)], strip=False, fit=DOC_FIT)
        == EXIT_SUCCESS
    )
    boxes = _mediaboxes(out)
    assert all(b == boxes[0] for b in boxes)
    width, height = _dims(boxes[0])
    assert abs(height - FLOOR_H) < 0.1  # grown to exactly screen/max_scale
    assert abs(width - 322.0) < 0.1  # non-binding dimension untouched
    assert abs(min(SCREEN_W / width, SCREEN_H / height) - MAX_SCALE) < 0.001


def test_first_page_badge_does_not_widen_shared_box(tmp_path):
    # An artifact badge in page 0's margin must not loosen the box for pages 2+;
    # page 0 alone deviates to keep it.
    plain = tmp_path / "plain.pdf"
    _block_doc(plain, pages=6, width=W, height=H, rect=(72, 63, 540, 729))
    badged = tmp_path / "badged.pdf"
    _block_doc(
        badged,
        pages=6,
        width=W,
        height=H,
        rect=(72, 63, 540, 729),
        extra=(20, 20, 60, 60),
    )
    plain_out = tmp_path / "plain_out.pdf"
    badged_out = tmp_path / "badged_out.pdf"
    run_crop(["-p", "0", "-o", str(plain_out), str(plain)], strip=False, fit=DOC_FIT)
    run_crop(["-p", "0", "-o", str(badged_out), str(badged)], strip=False, fit=DOC_FIT)

    pb = _mediaboxes(plain_out)
    bb = _mediaboxes(badged_out)
    # Pages 2+ are as tight as in the badge-free document.
    assert bb[1:] == pb[1:]
    # Page 0 deviates alone, expanded to keep the badge.
    assert bb[0] != bb[1]
    assert bb[0][0] <= 20.0 and bb[0][3] >= H - 20.0


def test_first_page_votes_when_exemption_disabled(tmp_path):
    # Documented behavior: with fit_exclude_first_page = false the badge widens
    # the shared box for every page.
    badged = tmp_path / "badged.pdf"
    _block_doc(
        badged,
        pages=6,
        width=W,
        height=H,
        rect=(72, 63, 540, 729),
        extra=(20, 20, 60, 60),
    )
    out = tmp_path / "out.pdf"
    fit = ReaderFit(
        scope="document",
        reader=True,
        exclude_first_page=False,
        screen_w_pt=SCREEN_W,
        screen_h_pt=SCREEN_H,
        max_scale=MAX_SCALE,
    )
    run_crop(["-p", "0", "-o", str(out), str(badged)], strip=False, fit=fit)
    boxes = _mediaboxes(out)
    assert all(b == boxes[0] for b in boxes)  # one box, widened by the badge
    assert boxes[0][0] <= 20.0


def test_mixed_size_insert_gets_page_treatment(tmp_path):
    # An A5 insert in a letter document is off-modal: it is handled per page,
    # keeps all its ink, and does not perturb the cohort.
    plain = tmp_path / "plain.pdf"
    _block_doc(plain, pages=6, width=W, height=H, rect=(72, 63, 540, 729))
    mixed = tmp_path / "mixed.pdf"
    doc = fitz.open()
    for i in range(6):
        if i == 3:
            page = doc.new_page(width=420, height=595)
            page.draw_rect(fitz.Rect(50, 47.5, 370, 547.5), fill=(0, 0, 0))
        else:
            page = doc.new_page(width=W, height=H)
            page.draw_rect(fitz.Rect(72, 63, 540, 729), fill=(0, 0, 0))
    doc.save(str(mixed))
    doc.close()

    plain_out = tmp_path / "plain_out.pdf"
    mixed_out = tmp_path / "mixed_out.pdf"
    run_crop(["-p", "0", "-o", str(plain_out), str(plain)], strip=False, fit=DOC_FIT)
    run_crop(["-p", "0", "-o", str(mixed_out), str(mixed)], strip=False, fit=DOC_FIT)

    mb = _mediaboxes(mixed_out)
    pb = _mediaboxes(plain_out)
    # Cohort pages unaffected by the insert.
    assert mb[1] == pb[1]
    # The insert keeps its ink (the 320x500 block at 50,47.5) and stays on-page.
    insert = mb[3]
    assert insert[0] <= 50.0 and insert[1] <= 47.5
    assert insert[2] >= 370.0 and insert[3] >= 547.5
    assert insert[0] >= 0.0 and insert[2] <= 420.0


def test_rotated_page_floors_with_swapped_axes(tmp_path):
    # A 90-rotated page is transposed on display, so it is off-cohort and floored
    # on its own content box with the screen axes swapped. No ink is clipped.
    src = tmp_path / "rot.pdf"
    _block_doc(
        src,
        pages=7,
        width=420,
        height=595,
        rect=(50, 47.5, 370, 547.5),
        rotate={6: 90},
    )
    out = tmp_path / "out.pdf"
    assert (
        run_crop(["-p", "0", "-o", str(out), str(src)], strip=False, fit=DOC_FIT)
        == EXIT_SUCCESS
    )
    boxes = _mediaboxes(out)
    rotated = boxes[6]
    # Off-cohort: it does not share the upright pages' box.
    assert rotated != boxes[0]
    # All its ink survives.
    assert rotated[0] <= 50.0 and rotated[1] <= 47.5
    assert rotated[2] >= 370.0 and rotated[3] >= 547.5
    # Swapped axes: the unrotated height is the displayed width, capped against
    # the screen width, so it is not grown to the portrait height floor.
    assert (rotated[3] - rotated[1]) < FLOOR_H


def test_content_against_one_margin_translates_not_shrinks(tmp_path):
    # A cohort page whose content sits hard against the left edge: the shared box
    # slides into the page rather than being cut down, so the floor holds.
    src = tmp_path / "edge.pdf"
    doc = fitz.open()
    for i in range(6):
        page = doc.new_page(width=420, height=595)
        rect = (0, 47.5, 320, 547.5) if i == 2 else (50, 47.5, 370, 547.5)
        page.draw_rect(fitz.Rect(*rect), fill=(0, 0, 0))
    doc.save(str(src))
    doc.close()
    out = tmp_path / "out.pdf"
    run_crop(["-p", "0", "-o", str(out), str(src)], strip=False, fit=DOC_FIT)
    boxes = _mediaboxes(out)
    edge = boxes[2]
    width, height = _dims(edge)
    # Full size preserved (translated, not shrunk) and inside the page.
    assert abs(height - FLOOR_H) < 0.1
    assert edge[0] >= 0.0 and edge[2] <= 420.0
    assert abs(min(SCREEN_W / width, SCREEN_H / height) - MAX_SCALE) < 0.001


def test_page_smaller_than_floor_clamps_to_page_box(tmp_path):
    # A genuinely tiny page cannot satisfy the floor; the box clamps to the page
    # box rather than claiming whitespace beyond the page edge.
    src = tmp_path / "tiny.pdf"
    _block_doc(src, pages=5, width=200, height=300, rect=(20, 20, 180, 280))
    out = tmp_path / "out.pdf"
    assert (
        run_crop(["-p", "0", "-o", str(out), str(src)], strip=False, fit=DOC_FIT)
        == EXIT_SUCCESS
    )
    for x0, y0, x1, y1 in _mediaboxes(out):
        assert 0.0 <= x0 < x1 <= 200.0
        assert 0.0 <= y0 < y1 <= 300.0


def test_single_page_degrades_to_page_scope_but_floors(tmp_path):
    # Page 0 is exempt, leaving an empty cohort: the document degrades to page
    # scope, and the floor still applies.
    src = tmp_path / "one.pdf"
    _block_doc(src, pages=1, width=420, height=595, rect=(50, 47.5, 370, 547.5))
    out = tmp_path / "out.pdf"
    assert (
        run_crop(["-p", "0", "-o", str(out), str(src)], strip=False, fit=DOC_FIT)
        == EXIT_SUCCESS
    )
    box = _mediaboxes(out)[0]
    assert abs((box[3] - box[1]) - FLOOR_H) < 0.1


def test_page_scope_floors_each_page_independently(tmp_path):
    # Under fit_scope = "page" pages with different content keep different boxes,
    # each floored on its own.
    src = tmp_path / "pagescope.pdf"
    doc = fitz.open()
    for i in range(4):
        page = doc.new_page(width=420, height=595)
        right = 370 if i % 2 == 0 else 300
        page.draw_rect(fitz.Rect(50, 47.5, right, 547.5), fill=(0, 0, 0))
    doc.save(str(src))
    doc.close()
    out = tmp_path / "out.pdf"
    run_crop(["-p", "0", "-o", str(out), str(src)], strip=False, fit=PAGE_FIT)
    boxes = _mediaboxes(out)
    assert boxes[0] != boxes[1]  # per-page, not shared
    for box in boxes:
        assert abs((box[3] - box[1]) - FLOOR_H) < 0.1  # each floored


def test_fit_reader_false_still_shares_one_box(tmp_path):
    # Scope and the floor are independent knobs: document scope with the floor
    # off yields one shared box that is simply not grown.
    src = tmp_path / "a5.pdf"
    _block_doc(src, pages=6, width=420, height=595, rect=(50, 47.5, 370, 547.5))
    out = tmp_path / "out.pdf"
    fit = ReaderFit(scope="document", reader=False, exclude_first_page=True)
    assert (
        run_crop(["-p", "0", "-o", str(out), str(src)], strip=False, fit=fit)
        == EXIT_SUCCESS
    )
    boxes = _mediaboxes(out)
    assert all(b == boxes[0] for b in boxes)  # shared
    assert (boxes[0][3] - boxes[0][1]) < FLOOR_H  # not floored


def test_absolute4_tightens_shim_side_and_floor_holds(tmp_path):
    # -a4 crops inward as an additive term. It must be applied shim-side (so the
    # published box never crops past the computed one) and neutralized downstream.
    src = tmp_path / "a5.pdf"
    _block_doc(src, pages=6, width=420, height=595, rect=(50, 47.5, 370, 547.5))
    base = tmp_path / "base.pdf"
    offset = tmp_path / "offset.pdf"
    run_crop(["-p", "0", "-o", str(base), str(src)], strip=False, fit=DOC_FIT)
    assert (
        run_crop(
            ["-p", "0", "-a4", "0", "0", "12", "0", "-o", str(offset), str(src)],
            strip=False,
            fit=DOC_FIT,
        )
        == EXIT_SUCCESS
    )
    b = _mediaboxes(base)[0]
    o = _mediaboxes(offset)[0]
    # The right edge came in by exactly 12pt: the offset reached the content box
    # once, and was not applied a second time downstream.
    assert abs((b[2] - o[2]) - 12.0) < 0.2
    # The floor still holds on the binding (height) dimension.
    assert abs((o[3] - o[1]) - FLOOR_H) < 0.1


def test_negative_absolute4_widens_content_box(tmp_path):
    src = tmp_path / "a5.pdf"
    _block_doc(src, pages=6, width=420, height=595, rect=(50, 47.5, 370, 547.5))
    base = tmp_path / "base.pdf"
    widened = tmp_path / "wide.pdf"
    run_crop(["-p", "0", "-o", str(base), str(src)], strip=False, fit=DOC_FIT)
    run_crop(
        ["-p", "0", "-a4", "0", "0", "-10", "0", "-o", str(widened), str(src)],
        strip=False,
        fit=DOC_FIT,
    )
    b = _mediaboxes(base)[0]
    w = _mediaboxes(widened)[0]
    assert abs((w[2] - b[2]) - 10.0) < 0.2


def test_percent_retain_is_applied_shim_side_once(tmp_path):
    # The retain is applied in step 3 and then zeroed downstream, so a larger
    # retain widens the box by exactly the extra margin fraction, not twice.
    src = tmp_path / "letter.pdf"
    _block_doc(src, pages=6, width=W, height=H, rect=(100, 100, 500, 700))
    zero = tmp_path / "p0.pdf"
    fifty = tmp_path / "p50.pdf"
    run_crop(["-p", "0", "-o", str(zero), str(src)], strip=False, fit=DOC_FIT)
    run_crop(["-p", "50", "-o", str(fifty), str(src)], strip=False, fit=DOC_FIT)
    z = _mediaboxes(zero)[0]
    f = _mediaboxes(fifty)[0]
    # The left margin is ~99pt tight; retaining 50% puts the edge at ~49.5.
    assert abs(f[0] - z[0] / 2.0) < 1.0


# --------------------------------------------------------------------------
# Strip composed with reader-fit
# --------------------------------------------------------------------------


def test_strip_trims_header_and_footer(tmp_path):
    src = tmp_path / "in.pdf"
    _build_doc(src)
    nostrip = tmp_path / "nostrip.pdf"
    strip = tmp_path / "strip.pdf"
    run_crop(["-p", "10", "-o", str(nostrip), str(src)], strip=False, fit=DOC_FIT)
    rc = run_crop(["-p", "10", "-o", str(strip), str(src)], strip=True, fit=DOC_FIT)
    assert rc == EXIT_SUCCESS

    ns = _mediaboxes(nostrip)
    st = _mediaboxes(strip)
    for n, s in zip(ns[1:], st[1:]):
        # Top is lower (header removed) and bottom is higher (footer removed).
        assert s[3] < n[3]
        assert s[1] > n[1]


def test_strip_no_band_equals_whitespace_crop(tmp_path):
    # No running header/footer: strip-enabled output equals the plain fit crop.
    src = tmp_path / "in.pdf"
    _build_doc(src, header=False, footer=False)
    nostrip = tmp_path / "nostrip.pdf"
    strip = tmp_path / "strip.pdf"
    run_crop(["-p", "10", "-o", str(nostrip), str(src)], strip=False, fit=DOC_FIT)
    run_crop(["-p", "10", "-o", str(strip), str(src)], strip=True, fit=DOC_FIT)
    assert _mediaboxes(strip) == _mediaboxes(nostrip)


def test_strip_image_only_pdf_is_whitespace_crop(tmp_path):
    src = tmp_path / "in.pdf"
    _build_doc(src, image_only=True)
    nostrip = tmp_path / "nostrip.pdf"
    strip = tmp_path / "strip.pdf"
    run_crop(["-p", "10", "-o", str(nostrip), str(src)], strip=False, fit=DOC_FIT)
    rc = run_crop(["-p", "10", "-o", str(strip), str(src)], strip=True, fit=DOC_FIT)
    assert rc == EXIT_SUCCESS
    assert _mediaboxes(strip) == _mediaboxes(nostrip)


def test_strip_document_scope_figure_page_deviates_alone(tmp_path):
    # Header on every page but one figure-top page: the shared top sits at the
    # strip cut, the figure page deviates alone, no page re-acquires the band.
    src = tmp_path / "fig.pdf"
    doc = fitz.open()
    for i in range(6):
        page = doc.new_page(width=W, height=H)
        if i == 3:
            page.draw_rect(fitz.Rect(60, 30, 560, 300), fill=(0, 0, 0))
            for j in range(8):
                page.insert_text((100, 360 + j * 25), f"caption {j}", fontsize=11)
        else:
            page.insert_text((100, 50), "Running Header Conf 2024", fontsize=9)
            for j in range(18):
                page.insert_text((100, 120 + j * 25), f"body {j} pg{i}", fontsize=11)
        page.insert_text((300, 770), f"Page {i + 1}", fontsize=9)
    doc.save(str(src))
    doc.close()

    out = tmp_path / "out.pdf"
    assert (
        run_crop(["-p", "10", "-o", str(out), str(src)], strip=True, fit=DOC_FIT)
        == EXIT_SUCCESS
    )
    boxes = _mediaboxes(out)
    shared = boxes[1]
    # Every page but the figure page shares one box.
    assert [b for i, b in enumerate(boxes) if i != 3] == [shared] * 5
    # The figure page deviates upward alone to keep its figure.
    assert boxes[3][3] > shared[3]
    assert boxes[3][3] >= H - 35.0
    # The shared top is below the header band: no page re-acquires it.
    assert shared[3] < H - 50.0


def test_strip_respects_page_range(tmp_path):
    # With -g 2-4 pdfcropmargins crops only pages 2..4, so page 1 must be
    # untouched even though it carries the recurring header.
    src = tmp_path / "in.pdf"
    _build_doc(src, pages=4)
    out = tmp_path / "s.pdf"
    assert (
        run_crop(
            ["-p", "10", "-g", "2-4", "-o", str(out), str(src)],
            strip=True,
            fit=DOC_FIT,
        )
        == EXIT_SUCCESS
    )
    mb = _mediaboxes(out)
    assert mb[0] == [0.0, 0.0, W, H]  # excluded from -g, left untouched
    assert mb[1][3] < H  # an in-range page was cropped


def test_high_retain_yields_valid_boxes(tmp_path):
    # A large retain is now applied shim-side with no inversion that could go out
    # of range; every published box must still be valid and inside the page.
    src = tmp_path / "in.pdf"
    _build_doc(src)
    out = tmp_path / "s.pdf"
    assert (
        run_crop(["-p", "96", "-o", str(out), str(src)], strip=True, fit=DOC_FIT)
        == EXIT_SUCCESS
    )
    for x0, y0, x1, y1 in _mediaboxes(out):
        assert 0.0 <= y0 < y1 <= H
        assert 0.0 <= x0 < x1 <= W


def test_rotated_page_with_asymmetric_retain_keeps_ink(tmp_path):
    # The old strip path abstained on asymmetric-retain-plus-rotation; now the
    # quads permute per page, so the rotated page is handled and keeps its ink.
    src = tmp_path / "in.pdf"
    doc = fitz.open()
    for i in range(5):
        page = doc.new_page(width=W, height=H)
        page.insert_text((100, 50), "Running Header Conf 2024", fontsize=9)
        for j in range(18):
            page.insert_text((100, 120 + j * 25), f"body {j} pg{i}", fontsize=11)
    rot = doc.new_page(width=W, height=H)
    rot.draw_rect(fitz.Rect(100, 100, 500, 700), fill=(0, 0, 0))
    rot.set_rotation(180)
    doc.save(str(src))
    doc.close()

    out = tmp_path / "s.pdf"
    assert (
        run_crop(
            ["-p4", "10", "20", "10", "5", "-o", str(out), str(src)],
            strip=True,
            fit=DOC_FIT,
        )
        == EXIT_SUCCESS
    )
    rotated = _mediaboxes(out)[5]
    assert rotated[0] <= 100.0 and rotated[1] <= 100.0
    assert rotated[2] >= 500.0 and rotated[3] >= 700.0


# --------------------------------------------------------------------------
# Failure classification and CLI parsing
# --------------------------------------------------------------------------


def test_content_failure_on_corrupt_input(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 not really a pdf at all")
    out = tmp_path / "out.pdf"
    rc = run_crop(["-p", "10", "-o", str(out), str(bad)], strip=True, fit=DOC_FIT)
    assert rc == EXIT_CONTENT


def test_nonzero_exit_with_env_stderr_is_environmental(tmp_path, monkeypatch):
    # A nonzero exit whose stderr is NOT a content pattern (e.g. a missing
    # Ghostscript) must be environmental (retryable), not suppressed.
    import pdfCropMargins

    def fake_crop(argv_list, quiet):
        return (None, 1, "", "No Ghostscript executable was found")

    monkeypatch.setattr(pdfCropMargins, "crop", fake_crop, raising=False)
    rc = run_crop(
        ["-gs", "-o", str(tmp_path / "o.pdf"), str(tmp_path / "i.pdf")], strip=False
    )
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
    rc = run_crop(["-p", "10", "-o", str(out), str(src)], strip=True, fit=DOC_FIT)
    assert rc == EXIT_ENVIRONMENTAL


def test_fit_pipeline_error_is_environmental(tmp_path, monkeypatch):
    # Our own reader-fit bug must be retried, never suppress an input.
    src = tmp_path / "in.pdf"
    _build_doc(src)

    import scribe_crop.crop_shim as shim_mod

    def boom(*a, **k):
        raise RuntimeError("floor blew up")

    monkeypatch.setattr(shim_mod, "apply_floor", boom)
    out = tmp_path / "out.pdf"
    rc = run_crop(["-p", "10", "-o", str(out), str(src)], strip=False, fit=DOC_FIT)
    assert rc == EXIT_ENVIRONMENTAL


def test_main_parses_strip_flag(monkeypatch):
    captured = {}

    import scribe_crop.crop_shim as shim_mod

    def fake_run_crop(crop_argv, *, strip, fit=None, params=None):
        captured["argv"] = list(crop_argv)
        captured["strip"] = strip
        captured["fit"] = fit
        return 0

    monkeypatch.setattr(shim_mod, "run_crop", fake_run_crop)
    rc = shim_mod.main(["--strip-header-footer", "-p", "10", "-o", "x", "y"])
    assert rc == 0
    assert captured["strip"] is True
    assert captured["fit"] is None
    assert captured["argv"] == ["-p", "10", "-o", "x", "y"]


def test_main_parses_fit_flag(monkeypatch):
    captured = {}
    import scribe_crop.crop_shim as shim_mod

    def fake_run_crop(crop_argv, *, strip, fit=None, params=None):
        captured["argv"] = list(crop_argv)
        captured["fit"] = fit
        captured["strip"] = strip
        return 0

    monkeypatch.setattr(shim_mod, "run_crop", fake_run_crop)
    rc = shim_mod.main([shim_mod.FIT_FLAG, DOC_FIT.token(), "-p", "10", "-o", "x", "y"])
    assert rc == 0
    assert captured["strip"] is False
    assert captured["fit"] == DOC_FIT
    assert captured["argv"] == ["-p", "10", "-o", "x", "y"]


def test_main_parses_both_directives(monkeypatch):
    captured = {}
    import scribe_crop.crop_shim as shim_mod

    def fake_run_crop(crop_argv, *, strip, fit=None, params=None):
        captured.update(argv=list(crop_argv), strip=strip, fit=fit)
        return 0

    monkeypatch.setattr(shim_mod, "run_crop", fake_run_crop)
    shim_mod.main(
        ["--strip-header-footer", shim_mod.FIT_FLAG, PAGE_FIT.token(), "-p", "10", "z"]
    )
    assert captured["strip"] is True
    assert captured["fit"] == PAGE_FIT
    assert captured["argv"] == ["-p", "10", "z"]


def test_main_without_directives(monkeypatch):
    captured = {}
    import scribe_crop.crop_shim as shim_mod

    def fake_run_crop(crop_argv, *, strip, fit=None, params=None):
        captured.update(strip=strip, fit=fit)
        return 0

    monkeypatch.setattr(shim_mod, "run_crop", fake_run_crop)
    shim_mod.main(["-p", "10", "-o", "x", "y"])
    assert captured["strip"] is False
    assert captured["fit"] is None


@pytest.mark.parametrize("flag", ["--strip-header-footer", "--reader-fit"])
def test_main_does_not_treat_option_value_as_directive(monkeypatch, flag):
    # A pdfcropmargins option value equal to a shim directive (e.g. a password)
    # must be forwarded verbatim, not consumed as our own flag.
    captured = {}
    import scribe_crop.crop_shim as shim_mod

    def fake_run_crop(crop_argv, *, strip, fit=None, params=None):
        captured.update(argv=list(crop_argv), strip=strip, fit=fit)
        return 0

    monkeypatch.setattr(shim_mod, "run_crop", fake_run_crop)
    rc = shim_mod.main(["-pw", flag, "-o", "x", "y"])
    assert rc == 0
    assert captured["strip"] is False
    assert captured["fit"] is None
    assert captured["argv"] == ["-pw", flag, "-o", "x", "y"]


def test_main_bad_fit_payload_is_environmental():
    import scribe_crop.crop_shim as shim_mod

    rc = shim_mod.main([shim_mod.FIT_FLAG, "scope=chapter", "-o", "x", "y"])
    assert rc == EXIT_ENVIRONMENTAL
