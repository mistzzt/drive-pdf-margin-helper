"""Unit tests for the pure-logic header/footer detector.

These build text geometry directly (no PDF) for exact, deterministic
expectations, exercising the detection algorithm in isolation.
"""

from scribe_crop.detector import (
    DEFAULT_PARAMS,
    DetectorParams,
    PageText,
    detect_bands,
    page_text_from_mupdf,
)

H = 792.0
W = 612.0

# MuPDF top-left line boxes (y grows downward). A header sits near the top, body
# lines fill the middle, a footer sits near the bottom.
HEADER = (100.0, 40.0, 300.0, 52.0)  # inner edge (y_bottom) = 52
FOOTER = (300.0, 760.0, 360.0, 772.0)  # inner edge (y_top) = 760


def _body_lines(n=18, start=120.0, leading=25.0):
    return [(100.0, start + i * leading, 400.0, start + i * leading + 12.0) for i in range(n)]


def _page(*, header=True, footer=True, body=True, height=H, width=W):
    boxes = []
    if header:
        boxes.append(HEADER)
    if body:
        boxes.extend(_body_lines())
    if footer:
        boxes.append(FOOTER)
    return PageText(page_height=height, page_width=width, line_boxes=boxes)


def test_detects_recurring_band():
    pages = [_page() for _ in range(5)]
    res = detect_bands(pages)
    assert res.top is not None
    assert res.bottom is not None
    # Top cut: inner edge mupdf y=52, nudged by min gap (4) -> dist 56.
    assert abs(res.top.dist - (52.0 + DEFAULT_PARAMS.min_isolation_gap)) < 1e-6
    # Bottom cut: footer inner edge dist-from-bottom = 792-760 = 32; +4 nudge = 36.
    assert abs(res.bottom.dist - (32.0 + DEFAULT_PARAMS.min_isolation_gap)) < 1e-6


def test_text_agnostic_alternation():
    # Header x-position alternates (verso/recto) and footer text changes per page,
    # but the y geometry recurs, so both are still detected.
    pages = []
    for i in range(6):
        header = (50.0 + (i % 2) * 200.0, 40.0, 250.0 + (i % 2) * 200.0, 52.0)
        footer = (300.0, 760.0, 320.0 + i * 3, 772.0)
        boxes = [header, *_body_lines(), footer]
        pages.append(PageText(page_height=H, page_width=W, line_boxes=boxes))
    res = detect_bands(pages)
    assert res.top is not None
    assert res.bottom is not None


def test_abstains_no_running_header():
    pages = [_page(header=False, footer=False) for _ in range(5)]
    res = detect_bands(pages)
    assert res.top is None
    assert res.bottom is None


def test_abstains_figure_at_top_one_page():
    # All pages have a header EXCEPT one interior page that instead has a tall
    # figure block at the top (a single large box). The figure does not recur at
    # the header's y, but the real header still does on the other pages -> the
    # header is detected, and the figure never confirms a separate band.
    pages = [_page() for _ in range(5)]
    # Replace one interior page with a figure (one big top block, no header line).
    figure_page = PageText(
        page_height=H,
        page_width=W,
        line_boxes=[(50.0, 30.0, 560.0, 250.0), *_body_lines(start=300.0)],
    )
    pages[2] = figure_page
    res = detect_bands(pages)
    # The header still recurs on the 4 non-figure voting pages.
    assert res.top is not None


def test_figure_only_no_header_abstains():
    # No recurring header; each page has a differently-sized top block. Even if a
    # block starts in the top zone, the inner edges do not cluster.
    pages = []
    for i in range(5):
        block = (50.0, 30.0, 560.0, 80.0 + i * 30.0)  # varying inner edge
        pages.append(PageText(page_height=H, page_width=W, line_boxes=[block, *_body_lines(start=300.0)]))
    res = detect_bands(pages)
    assert res.top is None


def test_title_page_does_not_vote():
    # First page has a header at a DIFFERENT y; it must not pollute the cluster.
    odd_header = (100.0, 40.0, 300.0, 120.0)  # much taller, different inner edge
    pages = [PageText(page_height=H, page_width=W, line_boxes=[odd_header, *_body_lines()])]
    pages += [_page(footer=False) for _ in range(4)]
    res = detect_bands(pages)
    assert res.top is not None
    # Cut reflects the recurring header (52), not the title page's 120.
    assert abs(res.top.dist - (52.0 + DEFAULT_PARAMS.min_isolation_gap)) < 1e-6


def test_too_few_voters_two_page_doc():
    # A 2-page doc: only page 2 votes -> 1 voter < min_voter_count (3). Abstain.
    pages = [_page(), _page()]
    res = detect_bands(pages)
    assert res.top is None
    assert res.bottom is None


def test_single_page_abstains():
    res = detect_bands([_page()])
    assert res.top is None
    assert res.bottom is None


def test_no_text_layer_abstains():
    pages = [PageText(page_height=H, page_width=W, line_boxes=[]) for _ in range(5)]
    res = detect_bands(pages)
    assert res.top is None
    assert res.bottom is None


def test_footnote_guard_multiline_footer_not_taken():
    # A multi-line footnote block at the bottom (more than max_band_rows tightly
    # packed rows with no isolation gap) must not be taken as a footer band.
    footnote = [
        (100.0, 700.0, 500.0, 712.0),
        (100.0, 714.0, 500.0, 726.0),
        (100.0, 728.0, 500.0, 740.0),
        (100.0, 742.0, 500.0, 754.0),
        (100.0, 756.0, 500.0, 768.0),
    ]
    pages = [
        PageText(page_height=H, page_width=W, line_boxes=[*_body_lines(), *footnote])
        for _ in range(5)
    ]
    res = detect_bands(pages)
    assert res.bottom is None


def test_coverage_below_fraction_abstains():
    # Header on only 2 of 6 voting pages -> coverage 2/6 = 0.33 < 0.40. Abstain
    # even though 2 >= ... (it also fails min voters). Make 3 voters but coverage
    # still below threshold by adding more no-header pages.
    pages = [_page(footer=False)]  # title
    # 3 with header, 5 without -> coverage 3/8 = 0.375 < 0.40
    pages += [_page(footer=False) for _ in range(3)]
    pages += [_page(header=False, footer=False) for _ in range(5)]
    res = detect_bands(pages)
    assert res.top is None


def test_modal_size_restriction():
    # Mostly Letter pages with a header; a couple of odd-size pages with a header
    # at a different distance-from-edge must not pollute the modal cluster.
    pages = [_page() for _ in range(5)]
    odd = PageText(
        page_height=1000.0,
        page_width=700.0,
        line_boxes=[(100.0, 200.0, 300.0, 260.0), *_body_lines(start=400.0)],
    )
    pages += [odd, odd]
    res = detect_bands(pages)
    assert res.top is not None
    # Cut still reflects the modal (Letter) header.
    assert abs(res.top.dist - (52.0 + DEFAULT_PARAMS.min_isolation_gap)) < 1e-6


def test_header_only_footer_left_untouched():
    pages = [_page(footer=False) for _ in range(5)]
    res = detect_bands(pages)
    assert res.top is not None
    assert res.bottom is None


def test_cuts_measured_from_their_own_edge():
    # Each cut is a small distance from its own edge (not a shared absolute y);
    # the y-flip to PDF coords is exercised at the shim level.
    pages = [_page() for _ in range(5)]
    res = detect_bands(pages)
    assert res.top.dist < H / 2
    assert res.bottom.dist < H / 2


def test_sparse_page_abstains_no_false_header():
    # Each page has only a top line plus ONE body row at normal leading (2 rows).
    # The body leading cannot be estimated (< 3 rows), so the isolation guard
    # must abstain rather than fall back to the flat min gap and treat the first
    # body row as a header.
    pages = []
    for _ in range(5):
        boxes = [
            (100.0, 40.0, 300.0, 52.0),  # top line
            (100.0, 65.0, 400.0, 77.0),  # single body row, ~13pt below
        ]
        pages.append(PageText(page_height=H, page_width=W, line_boxes=boxes))
    res = detect_bands(pages)
    assert res.top is None
    assert res.bottom is None


def test_params_token_changes_with_constant():
    base = DetectorParams()
    other = DetectorParams(coverage_fraction=0.5)
    assert base.token() != other.token()


def test_page_text_from_mupdf_skips_empty_spans():
    class FakePage:
        def get_text(self, kind):
            assert kind == "dict"
            return {
                "blocks": [
                    {"lines": [{"bbox": (1.0, 2.0, 3.0, 4.0), "spans": [{"text": "x"}]}]},
                    {"lines": [{"bbox": (5.0, 6.0, 7.0, 8.0), "spans": []}]},
                    {},  # image block, no lines
                ]
            }

    pt = page_text_from_mupdf(FakePage(), 792.0, 612.0)
    assert pt.line_boxes == [(1.0, 2.0, 3.0, 4.0)]
    assert pt.page_height == 792.0
