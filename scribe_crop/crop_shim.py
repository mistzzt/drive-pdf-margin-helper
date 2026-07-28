"""Crop shim: wrap one pdfcropmargins crop, composing header/footer strip and
reader-fit into final per-page boxes that the tool publishes as-is."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .detector import (
    DEFAULT_PARAMS,
    DetectorParams,
    detect_bands,
    page_band_inner_dists,
    page_text_from_mupdf,
)
from .profile import SCOPE_DOCUMENT, SCOPE_PAGE

STRIP_FLAG = "--strip-header-footer"  # leading token the processor prepends
FIT_FLAG = "--reader-fit"  # leading token plus one payload token

EXIT_SUCCESS = 0
EXIT_CONTENT = 2
EXIT_ENVIRONMENTAL = 3

# Page boxes within this tolerance (bp) count as the same modal size,
# absorbing scanner jitter. A fixed algorithm constant, not user config.
COHORT_SIZE_TOLERANCE = 2.0

# 90/270 pages are saved unrotated but displayed transposed, so they can never
# share a box with these rotations.
_UPRIGHT_ROTATIONS = (0, 180)

# Distinct from pdfcropmargins' own messages so the service classifies on the
# marker, not on incidental words in a traceback.
_CONTENT_MARKER = "scribe-crop-shim: content failure:"
_ENV_MARKER = "scribe-crop-shim: environmental failure:"

# pdfcropmargins exits nonzero for both input-bound and environmental causes.
# These substrings mark the input-bound ones; anything else is environmental
# (retryable). The processor imports this so both classify on the same signal.
CONTENT_STDERR_PATTERNS = (
    "could not be decrypted",
    "password",
    "encrypted",
    "no detectable bounding box",
    "bounding box",
    "empty bounding box",
    "could not be read",
    "could not read",
    "is not a valid pdf",
    "error parsing",
    "pdfreaderror",
    "could not be repaired",
    "failed to read",
    "is encrypted",
)

Box = list[float]
Quad = Sequence[float]


@dataclass(frozen=True)
class ReaderFit:
    """Resolved reader-fit parameters for one crop; screen dimensions in points."""

    scope: str = SCOPE_PAGE
    reader: bool = False
    exclude_first_page: bool = True
    screen_w_pt: float = 0.0
    screen_h_pt: float = 0.0
    max_scale: float = 1.0

    @property
    def active(self) -> bool:
        """False iff the pipeline would reduce to pdfcropmargins' native
        behavior, in which case the wrapper need not be installed."""
        return self.reader or self.scope == SCOPE_DOCUMENT

    def token(self) -> str:
        """Serialize as both the directive payload and the fingerprint
        contribution, folding only parameters that can change the output."""
        parts = [f"scope={self.scope}", f"reader={1 if self.reader else 0}"]
        if self.scope == SCOPE_DOCUMENT:
            parts.append(f"first={1 if self.exclude_first_page else 0}")
        if self.reader:
            parts.append(f"sw={self.screen_w_pt!r}")
            parts.append(f"sh={self.screen_h_pt!r}")
            parts.append(f"max={self.max_scale!r}")
        return ";".join(parts)

    @classmethod
    def parse(cls, token: str) -> ReaderFit:
        data: dict[str, str] = {}
        for item in token.split(";"):
            if not item:
                continue
            key, _, value = item.partition("=")
            data[key] = value
        scope = data.get("scope", SCOPE_PAGE)
        if scope not in (SCOPE_DOCUMENT, SCOPE_PAGE):
            raise ValueError(f"bad reader-fit scope: {scope!r}")
        return cls(
            scope=scope,
            reader=data.get("reader") == "1",
            exclude_first_page=data.get("first", "1") == "1",
            screen_w_pt=float(data.get("sw", 0.0)),
            screen_h_pt=float(data.get("sh", 0.0)),
            max_scale=float(data.get("max", 1.0)),
        )


def rotate_quad(quad: Quad, angle: int) -> list[float]:
    """Permute an [L, B, R, T] quad by a page rotation, exactly as
    pdfcropmargins' ``mod_box_for_rotation`` does."""
    values = list(quad)
    turns = {0: 0, 90: 1, 180: 2, 270: 3}.get(int(angle) % 360, 0)
    for _ in range(turns):
        values = [values[1], values[2], values[3], values[0]]
    return values


def content_box(
    tight: Sequence[float],
    page: Sequence[float],
    *,
    percent_retain4: Quad,
    absolute4: Quad,
    rotation: int = 0,
    top_cut: float | None = None,
    bottom_cut: float | None = None,
) -> Box:
    """Step 3: the box a page must at minimum receive. Order is load-bearing:
    rotate the quads, scale the retain, add the offset, then clamp to the cut."""
    rp = rotate_quad(percent_retain4, rotation)
    ra = rotate_quad(absolute4, rotation)
    box: Box = [
        page[0] + abs(tight[0] - page[0]) * (1.0 - rp[0] / 100.0) + ra[0],
        page[1] + abs(tight[1] - page[1]) * (1.0 - rp[1] / 100.0) + ra[1],
        page[2] - abs(tight[2] - page[2]) * (1.0 - rp[2] / 100.0) - ra[2],
        page[3] - abs(tight[3] - page[3]) * (1.0 - rp[3] / 100.0) - ra[3],
    ]
    if top_cut is not None:
        box[3] = min(box[3], top_cut)
    if bottom_cut is not None:
        box[1] = max(box[1], bottom_cut)
    return box


def modal_page_size(
    sizes: Sequence[tuple[float, float]], tolerance: float = COHORT_SIZE_TOLERANCE
) -> tuple[float, float] | None:
    """The unique most-common (width, height) within ``tolerance``; ``None`` on
    a tie, which degrades the document to page scope rather than guessing."""
    buckets: list[tuple[tuple[float, float], int]] = []
    for size in sizes:
        for index, (rep, count) in enumerate(buckets):
            if abs(rep[0] - size[0]) <= tolerance and abs(rep[1] - size[1]) <= tolerance:
                buckets[index] = (rep, count + 1)
                break
        else:
            buckets.append((size, 1))
    if not buckets:
        return None
    best = max(count for _, count in buckets)
    winners = [rep for rep, count in buckets if count == best]
    return winners[0] if len(winners) == 1 else None


def union_boxes(boxes: Sequence[Sequence[float]]) -> Box:
    """Max extent per edge."""
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def apply_floor(
    box: Sequence[float], screen_w: float, screen_h: float, max_scale: float
) -> Box:
    """Step 6: when ``min(screen_w/w, screen_h/h)`` exceeds the cap, grow only
    the binding (smaller-ratio) dimension to screen/max_scale, symmetrically."""
    out: Box = list(box)
    width = out[2] - out[0]
    height = out[3] - out[1]
    if width <= 0.0 or height <= 0.0:
        return out
    ratio_w = screen_w / width
    ratio_h = screen_h / height
    if min(ratio_w, ratio_h) <= max_scale:
        return out
    if ratio_w <= ratio_h:  # width binds; ties grow the width
        new_width = screen_w / max_scale
        centre = (out[0] + out[2]) / 2.0
        out[0] = centre - new_width / 2.0
        out[2] = centre + new_width / 2.0
    else:
        new_height = screen_h / max_scale
        centre = (out[1] + out[3]) / 2.0
        out[1] = centre - new_height / 2.0
        out[3] = centre + new_height / 2.0
    return out


def place_box(
    box: Sequence[float], content: Sequence[float], page: Sequence[float]
) -> Box:
    """Step 8: expand to contain ``content``, translate into ``page``, then
    shrink only where the page itself is smaller. The order is load-bearing."""
    out: Box = [
        min(box[0], content[0]),
        min(box[1], content[1]),
        max(box[2], content[2]),
        max(box[3], content[3]),
    ]
    for lo, hi in ((0, 2), (1, 3)):
        if out[hi] - out[lo] > page[hi] - page[lo]:
            continue  # too big to translate into the page; the clamp shrinks it
        if out[lo] < page[lo]:
            shift = page[lo] - out[lo]
        elif out[hi] > page[hi]:
            shift = page[hi] - out[hi]
        else:
            continue
        out[lo] += shift
        out[hi] += shift
    out[0] = max(out[0], page[0])
    out[1] = max(out[1], page[1])
    out[2] = min(out[2], page[2])
    out[3] = min(out[3], page[3])
    return out


def make_bbox_wrapper(
    original: Callable,
    params: DetectorParams = DEFAULT_PARAMS,
    *,
    strip: bool = False,
    fit: ReaderFit | None = None,
) -> Callable:
    """Build the ``get_bounding_box_list`` wrapper implementing the nine-step
    reader-fit pipeline."""

    settings = fit if fit is not None else ReaderFit()

    def wrapper(
        input_doc_fname,
        input_doc_mupdf_wrapper,
        full_page_box_list,
        set_of_page_nums_to_crop,
        argparse_args,
    ):
        # Step 1: per-page tight whitespace boxes from the unmodified detector.
        bbox_list = original(
            input_doc_fname,
            input_doc_mupdf_wrapper,
            full_page_box_list,
            set_of_page_nums_to_crop,
            argparse_args,
        )

        num_pages = len(full_page_box_list)
        selected = [i for i in range(num_pages) if i in set_of_page_nums_to_crop]
        if not selected:
            return bbox_list

        rotations = [
            int(page.rotationAngle) % 360
            for page in input_doc_mupdf_wrapper.page_list[:num_pages]
        ]

        # Step 2: strip cuts. Non-exhibiting pages are not abstained; the vote
        # cap (step 5) and containment expansion (step 8) protect their ink.
        top_cuts: dict[int, float | None] = dict.fromkeys(selected)
        bottom_cuts: dict[int, float | None] = dict.fromkeys(selected)
        doc_top_cut: dict[int, float] = {}
        doc_bottom_cut: dict[int, float] = {}
        if strip:
            document = input_doc_mupdf_wrapper.document
            # Text comes from the handed wrapper doc (already un-rotated and
            # pre-cropped), using each page's current MediaBox for the y-flip.
            pages = []
            for i in range(num_pages):
                page = document[i]
                mb = page.mediabox
                pages.append(
                    page_text_from_mupdf(
                        page, float(mb.y1 - mb.y0), float(mb.x1 - mb.x0)
                    )
                )
            result = detect_bands(pages, params)
            tol = params.y_cluster_tolerance
            for i in selected:
                f = full_page_box_list[i]
                own_top, own_bottom = page_band_inner_dists(pages[i], params)
                # Cuts are carried as distance-from-edge, so the conversion stays
                # height-correct on non-modal pages too.
                if result.top is not None:
                    doc_top_cut[i] = f[3] - result.top.dist
                    if (
                        own_top is not None
                        and abs(own_top - result.top.inner_dist) <= tol
                    ):
                        top_cuts[i] = doc_top_cut[i]
                if result.bottom is not None:
                    doc_bottom_cut[i] = f[1] + result.bottom.dist
                    if (
                        own_bottom is not None
                        and abs(own_bottom - result.bottom.inner_dist) <= tol
                    ):
                        bottom_cuts[i] = doc_bottom_cut[i]

        # Step 3: content boxes (retain, absolute4, rotation permutation, cut).
        p4 = list(argparse_args.percentRetain4)
        a4 = list(argparse_args.absoluteOffset4)
        content: dict[int, Box] = {
            i: content_box(
                bbox_list[i],
                full_page_box_list[i],
                percent_retain4=p4,
                absolute4=a4,
                rotation=rotations[i],
                top_cut=top_cuts[i],
                bottom_cut=bottom_cuts[i],
            )
            for i in selected
        }

        def floored(box: Box, page_num: int) -> Box:
            if not settings.reader:
                return box
            if rotations[page_num] in _UPRIGHT_ROTATIONS:
                screen_w, screen_h = settings.screen_w_pt, settings.screen_h_pt
            else:
                # The displayed page is transposed, so the floor's axes swap.
                screen_w, screen_h = settings.screen_h_pt, settings.screen_w_pt
            return apply_floor(box, screen_w, screen_h, settings.max_scale)

        # Step 4: the cohort (document scope only). Off-modal and 90/270 pages
        # are step-7 cases; an exempt page 0 still shares the box (see below).
        conforming: list[int] = []
        cohort: list[int] = []
        if settings.scope == SCOPE_DOCUMENT:
            sizes = {
                i: (
                    full_page_box_list[i][2] - full_page_box_list[i][0],
                    full_page_box_list[i][3] - full_page_box_list[i][1],
                )
                for i in selected
            }
            modal = modal_page_size([sizes[i] for i in selected])
            if modal is not None:
                conforming = [
                    i
                    for i in selected
                    if rotations[i] in _UPRIGHT_ROTATIONS
                    and abs(sizes[i][0] - modal[0]) <= COHORT_SIZE_TOLERANCE
                    and abs(sizes[i][1] - modal[1]) <= COHORT_SIZE_TOLERANCE
                ]
            cohort = [
                i
                for i in conforming
                if not (settings.exclude_first_page and i == 0)
            ]
            # Fewer than two voters cannot establish a shared box, so the whole
            # document degrades to page scope.
            if len(cohort) < 2:
                conforming = []
                cohort = []

        shared: Box | None = None
        if cohort:
            # Step 5: union of the cohort's content boxes. Capping each vote at
            # the document cut keeps band-zone ink from re-widening the box.
            votes: list[Box] = []
            for i in cohort:
                vote = list(content[i])
                if i in doc_top_cut:
                    vote[3] = min(vote[3], doc_top_cut[i])
                if i in doc_bottom_cut:
                    vote[1] = max(vote[1], doc_bottom_cut[i])
                votes.append(vote)
            # Step 6: the floor, once; every cohort page is upright.
            shared = floored(union_boxes(votes), cohort[0])

        # Steps 7 and 8: per-page placement.
        new_bbox_list = [list(b) for b in bbox_list]
        conforming_set = set(conforming)
        for i in selected:
            if shared is not None and i in conforming_set:
                # Includes an exempt page 0: it shares the box, deviating only
                # through step 8's minimal expansion.
                start: Box = list(shared)
            else:
                # Step 7: page scope and off-cohort pages, own box floored.
                start = floored(list(content[i]), i)
            new_bbox_list[i] = place_box(start, content[i], full_page_box_list[i])

        # Step 9: both fields were applied in step 3; leaving either set would
        # have pdfcropmargins apply it a second time downstream.
        argparse_args.percentRetain4 = [0.0, 0.0, 0.0, 0.0]
        argparse_args.absoluteOffset4 = [0.0, 0.0, 0.0, 0.0]
        return new_bbox_list

    return wrapper


def run_crop(
    crop_argv: Sequence[str],
    *,
    strip: bool,
    fit: ReaderFit | None = None,
    params: DetectorParams = DEFAULT_PARAMS,
) -> int:
    """Run a single crop, returning a structured exit code. The monkeypatch is
    installed only when a feature needs it, and is always removed afterward."""
    # Import here so a missing dependency is a retryable environmental failure,
    # not an import-time crash.
    try:
        from pdfCropMargins import crop
        from pdfCropMargins import main_pdfCropMargins as main_mod
    except Exception as exc:  # noqa: BLE001
        print(f"{_ENV_MARKER} import failed: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENTAL

    installed = False
    original = None
    if strip or (fit is not None and fit.active):
        original = main_mod.get_bounding_box_list
        main_mod.get_bounding_box_list = make_bbox_wrapper(
            original, params, strip=strip, fit=fit
        )
        installed = True

    try:
        _, exit_code, _, stderr_str = crop(argv_list=list(crop_argv), quiet=True)
    except SystemExit as exc:  # pragma: no cover - crop() traps this itself
        exit_code = exc.code if isinstance(exc.code, int) else 1
        stderr_str = str(exc)
    except Exception as exc:  # noqa: BLE001
        # A shim/PyMuPDF bug escaping crop() must be retried, never suppressed;
        # emit the marker, not the raw traceback.
        print(f"{_ENV_MARKER} {type(exc).__name__}", file=sys.stderr)
        return EXIT_ENVIRONMENTAL
    finally:
        if installed:
            main_mod.get_bounding_box_list = original

    if exit_code in (None, 0):
        return EXIT_SUCCESS

    # Classify the nonzero exit by stderr (content -> suppress, else environmental
    # -> retry); log the matching marker plus the tool's own last line.
    blob = (stderr_str or "").lower()
    detail = (stderr_str or "").strip().splitlines()
    last = detail[-1] if detail else f"exit {exit_code}"
    if any(pat in blob for pat in CONTENT_STDERR_PATTERNS):
        print(f"{_CONTENT_MARKER} {last}", file=sys.stderr)
        return EXIT_CONTENT
    print(f"{_ENV_MARKER} {last}", file=sys.stderr)
    return EXIT_ENVIRONMENTAL


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    # Directives are only ever leading tokens, so consuming them only there never
    # swallows a pdfcropmargins option value that equals one (e.g. a password).
    strip = False
    fit: ReaderFit | None = None
    while raw:
        if raw[0] == STRIP_FLAG:
            strip = True
            raw = raw[1:]
        elif raw[0] == FIT_FLAG and len(raw) >= 2:
            try:
                fit = ReaderFit.parse(raw[1])
            except ValueError as exc:
                print(f"{_ENV_MARKER} bad {FIT_FLAG} payload: {exc}", file=sys.stderr)
                return EXIT_ENVIRONMENTAL
            raw = raw[2:]
        else:
            break
    return run_crop(raw, strip=strip, fit=fit)


if __name__ == "__main__":
    sys.exit(main())
