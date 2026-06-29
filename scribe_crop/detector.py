"""Running-header / running-footer band detection (pure geometry, see
``docs/header-footer-strip.md``).

The signal is cross-page positional recurrence: an isolated edge line that
recurs at the same distance-from-edge across most pages is the header/footer.
Text-agnostic and abstains on weak evidence, so a header-less doc or a
figure-at-top page is never trimmed. Cuts are carried as distance-from-edge;
the caller converts to PDF bottom-left per page (``y_pdf = height - y_mupdf``,
PyMuPDF bboxes are top-left/y-down).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

# Bump when the algorithm or any constant below changes: the value is folded
# into the dedup fingerprint when stripping is enabled, so a logic/constant
# change re-crops the affected files.
DETECTOR_VERSION = "1"


@dataclass(frozen=True)
class DetectorParams:
    """Fixed algorithm constants (not user-tunable); feed the fingerprint, so a
    change must bump :data:`DETECTOR_VERSION`."""

    top_zone_fraction: float = 0.18
    bottom_zone_fraction: float = 0.84
    y_cluster_tolerance: float = 6.0
    min_isolation_gap: float = 4.0
    coverage_fraction: float = 0.40
    min_voter_count: int = 3
    max_band_rows: int = 3
    # Two page sizes count as the same modal size within this tolerance (bp).
    page_size_tolerance: float = 2.0
    # A true header is followed by a gap far larger than the body's leading;
    # without this factor a header-less first body row masquerades as a header.
    isolation_gap_factor: float = 1.8

    def token(self) -> str:
        return (
            f"v={DETECTOR_VERSION};"
            f"tz={self.top_zone_fraction};bz={self.bottom_zone_fraction};"
            f"ct={self.y_cluster_tolerance};ig={self.min_isolation_gap};"
            f"cf={self.coverage_fraction};mv={self.min_voter_count};"
            f"mr={self.max_band_rows};ps={self.page_size_tolerance};"
            f"gf={self.isolation_gap_factor}"
        )


DEFAULT_PARAMS = DetectorParams()


@dataclass(frozen=True)
class _Row:
    """A merged text row in MuPDF top-left coords (y grows downward)."""

    y_top: float
    y_bottom: float


@dataclass(frozen=True)
class _Candidate:
    """A per-page header/footer candidate. ``inner_edge`` is the distance from the
    page edge to the band's body-facing boundary; ``isolation_gap`` is the
    whitespace to the nearest body row."""

    inner_edge: float
    isolation_gap: float


@dataclass(frozen=True)
class PageText:
    """Per-page text geometry handed to the detector.

    ``page_height`` and ``page_width`` are the page's *current* (post-precrop)
    MediaBox dimensions. ``line_boxes`` are text-line bounding boxes in MuPDF
    top-left coords as ``(x0, y0, x1, y1)`` with y growing downward.
    """

    page_height: float
    page_width: float
    line_boxes: Sequence[tuple[float, float, float, float]]


@dataclass(frozen=True)
class EdgeCut:
    """A confirmed cut for one edge, as a distance from the edge so the wrapper
    can place it per page using each page's own MediaBox height.

    - ``dist``: distance from the edge to the final cut (cluster inner edge
      nudged into the whitespace by the min isolation gap).
    - ``inner_dist``: the confirmed cluster inner edge (no nudge); the wrapper
      uses it to decide, per page, whether that page exhibits the band.
    """

    dist: float
    inner_dist: float


@dataclass(frozen=True)
class DetectionResult:
    """Per-edge cuts. ``None`` means abstain (do not trim that edge)."""

    top: EdgeCut | None
    bottom: EdgeCut | None


def _merge_rows(
    line_boxes: Sequence[tuple[float, float, float, float]],
) -> list[_Row]:
    """Merge text lines that vertically overlap into rows (y-overlap only).

    A two-column body yields one row per visual line; a short isolated
    left-margin line number does not fuse into a header because the merge is on
    vertical overlap, not horizontal proximity.
    """
    spans = sorted(((box[1], box[3]) for box in line_boxes), key=lambda yb: yb[0])
    rows: list[_Row] = []
    for y_top, y_bottom in spans:
        if rows and y_top <= rows[-1].y_bottom:
            prev = rows[-1]
            rows[-1] = _Row(
                y_top=min(prev.y_top, y_top),
                y_bottom=max(prev.y_bottom, y_bottom),
            )
        else:
            rows.append(_Row(y_top=y_top, y_bottom=y_bottom))
    return rows


def _body_gap(rows: list[_Row]) -> float | None:
    """Median vertical gap between consecutive rows (the body's typical leading).

    Used to decide whether a candidate edge line is *abnormally* isolated from
    the body. Returns ``None`` when there are too few rows to estimate the body's
    leading, in which case the page cannot provide a reliable candidate.
    """
    if len(rows) < 3:
        return None
    gaps = [rows[i + 1].y_top - rows[i].y_bottom for i in range(len(rows) - 1)]
    return statistics.median(gaps)


def _isolation_threshold(rows: list[_Row], params: DetectorParams) -> float | None:
    """The gap a candidate must clear to count as isolated from the body.

    Returns ``None`` when the body leading cannot be estimated (sparse page):
    without it the per-page isolation guard would degenerate to the flat minimum
    gap and a header-less page's first body row could masquerade as a header, so
    the page abstains instead.
    """
    body_gap = _body_gap(rows)
    if body_gap is None:
        return None
    return max(params.min_isolation_gap, body_gap * params.isolation_gap_factor)


def _top_candidate(
    rows: list[_Row], page_height: float, params: DetectorParams
) -> _Candidate | None:
    """The topmost row, only if it begins within the top zone and is isolated.

    The band may span up to ``max_band_rows`` consecutive rows (separated by less
    than the isolation threshold); its inner edge is the bottom of the last band
    row, and its isolation gap is the whitespace down to the next body row, which
    must be abnormally large relative to the body's own line spacing.
    """
    if not rows:
        return None
    threshold = _isolation_threshold(rows, params)
    if threshold is None:
        return None
    top = rows[0]
    if top.y_top > page_height * params.top_zone_fraction:
        return None
    band_bottom = top.y_bottom
    band_rows = 1
    next_idx = 1
    while next_idx < len(rows):
        gap = rows[next_idx].y_top - band_bottom
        if gap >= threshold:
            break
        band_rows += 1
        if band_rows > params.max_band_rows:
            return None
        band_bottom = rows[next_idx].y_bottom
        next_idx += 1
    if next_idx >= len(rows):
        # The band is the whole page; not an isolated header.
        return None
    isolation_gap = rows[next_idx].y_top - band_bottom
    if isolation_gap < threshold:
        return None
    # Distance from the top edge down to the band's inner (body-facing) edge.
    return _Candidate(inner_edge=band_bottom, isolation_gap=isolation_gap)


def _bottom_candidate(
    rows: list[_Row], page_height: float, params: DetectorParams
) -> _Candidate | None:
    """The bottommost row, only if it ends within the bottom zone and is isolated."""
    if not rows:
        return None
    threshold = _isolation_threshold(rows, params)
    if threshold is None:
        return None
    bottom = rows[-1]
    if bottom.y_bottom < page_height * params.bottom_zone_fraction:
        return None
    band_top = bottom.y_top
    band_rows = 1
    prev_idx = len(rows) - 2
    while prev_idx >= 0:
        gap = band_top - rows[prev_idx].y_bottom
        if gap >= threshold:
            break
        band_rows += 1
        if band_rows > params.max_band_rows:
            return None
        band_top = rows[prev_idx].y_top
        prev_idx -= 1
    if prev_idx < 0:
        return None
    isolation_gap = band_top - rows[prev_idx].y_bottom
    if isolation_gap < threshold:
        return None
    # Distance from the bottom edge up to the band's inner (body-facing) edge.
    return _Candidate(
        inner_edge=page_height - band_top, isolation_gap=isolation_gap
    )


def _modal_page_size(
    pages: Sequence[PageText], params: DetectorParams
) -> tuple[float, float]:
    """Return the most common (height, width) within the size tolerance."""
    buckets: list[tuple[tuple[float, float], list[tuple[float, float]]]] = []
    for page in pages:
        key = (page.page_height, page.page_width)
        for rep, members in buckets:
            if (
                abs(rep[0] - key[0]) <= params.page_size_tolerance
                and abs(rep[1] - key[1]) <= params.page_size_tolerance
            ):
                members.append(key)
                break
        else:
            buckets.append((key, [key]))
    rep, members = max(buckets, key=lambda b: len(b[1]))
    heights = [m[0] for m in members]
    widths = [m[1] for m in members]
    return statistics.median(heights), statistics.median(widths)


def _densest_cluster(
    values: list[float], tolerance: float
) -> list[float] | None:
    """Return the largest set of values within ``tolerance`` of a common center.

    A simple sweep: sort, then for each starting value greedily take every value
    within ``tolerance`` of it; keep the largest such window.
    """
    if not values:
        return None
    ordered = sorted(values)
    best: list[float] = []
    for i, anchor in enumerate(ordered):
        window = [v for v in ordered[i:] if v - anchor <= tolerance]
        if len(window) > len(best):
            best = window
    return best or None


def _confirm_edge(
    candidates: list[_Candidate],
    *,
    voting_page_count: int,
    params: DetectorParams,
) -> float | None:
    """Confirm a band for one edge and return its inner-edge distance, or None.

    ``voting_page_count`` is the number of non-first pages at the modal size,
    i.e. the denominator for the coverage fraction.
    """
    if voting_page_count <= 0:
        return None
    inner_edges = [c.inner_edge for c in candidates]
    cluster = _densest_cluster(inner_edges, params.y_cluster_tolerance)
    if cluster is None:
        return None
    coverage = len(cluster) / voting_page_count
    if coverage < params.coverage_fraction:
        return None
    if len(cluster) < params.min_voter_count:
        return None
    return statistics.median(cluster)  # robust center (nudged by the caller)


def page_band_inner_dists(
    page: PageText, params: DetectorParams = DEFAULT_PARAMS
) -> tuple[float | None, float | None]:
    """Return ``(top, bottom)`` inner-edge distances for this page's own bands.

    Each value is the distance from the relevant page edge to that page's
    isolated edge-band inner boundary, or ``None`` if the page exhibits no such
    band on that edge. The wrapper uses these to decide, per page, whether a page
    actually shows the confirmed band before tightening its box, so a title page,
    a figure-at-top page, or any header-less page is left untouched.
    """
    rows = _merge_rows(page.line_boxes)
    top = _top_candidate(rows, page.page_height, params)
    bottom = _bottom_candidate(rows, page.page_height, params)
    return (
        top.inner_edge if top is not None else None,
        bottom.inner_edge if bottom is not None else None,
    )


def detect_bands(
    pages: Sequence[PageText], params: DetectorParams = DEFAULT_PARAMS
) -> DetectionResult:
    """Detect the running header/footer bands. Either edge may be ``None``
    (abstain). The first (title) page never votes and only modal-size pages
    contribute.
    """
    if len(pages) < 2:
        # No non-first page can vote; cannot self-confirm.
        return DetectionResult(top=None, bottom=None)

    modal_h, modal_w = _modal_page_size(pages, params)

    def at_modal_size(page: PageText) -> bool:
        return (
            abs(page.page_height - modal_h) <= params.page_size_tolerance
            and abs(page.page_width - modal_w) <= params.page_size_tolerance
        )

    voting_pages = [p for p in pages[1:] if at_modal_size(p)]
    voting_page_count = len(voting_pages)

    top_candidates: list[_Candidate] = []
    bottom_candidates: list[_Candidate] = []
    for page in voting_pages:
        rows = _merge_rows(page.line_boxes)
        top = _top_candidate(rows, page.page_height, params)
        if top is not None:
            top_candidates.append(top)
        bottom = _bottom_candidate(rows, page.page_height, params)
        if bottom is not None:
            bottom_candidates.append(bottom)

    top_inner = _confirm_edge(
        top_candidates, voting_page_count=voting_page_count, params=params
    )
    bottom_inner = _confirm_edge(
        bottom_candidates, voting_page_count=voting_page_count, params=params
    )

    # Nudge the confirmed inner edge into the whitespace toward the body so the
    # cut never clips a descender or page-number glyph.
    top_cut = (
        EdgeCut(dist=top_inner + params.min_isolation_gap, inner_dist=top_inner)
        if top_inner is not None
        else None
    )
    bottom_cut = (
        EdgeCut(dist=bottom_inner + params.min_isolation_gap, inner_dist=bottom_inner)
        if bottom_inner is not None
        else None
    )
    return DetectionResult(top=top_cut, bottom=bottom_cut)


def page_text_from_mupdf(page, page_height: float, page_width: float) -> PageText:
    """Build :class:`PageText` from a PyMuPDF page's text dict.

    ``page_height``/``page_width`` must be the page's current (post-precrop)
    MediaBox dimensions, matching the frame the crop boxes live in. Lines with no
    spans (e.g. image-only pages) contribute no boxes, so an image-only PDF
    yields an empty text geometry and the detector abstains.
    """
    text = page.get_text("dict")
    line_boxes: list[tuple[float, float, float, float]] = []
    for block in text.get("blocks", []):
        for line in block.get("lines", []):
            if not line.get("spans"):
                continue
            x0, y0, x1, y1 = line["bbox"]
            line_boxes.append((x0, y0, x1, y1))
    return PageText(
        page_height=page_height, page_width=page_width, line_boxes=line_boxes
    )
