"""Crop shim: run pdfcropmargins, optionally stripping running headers/footers.

The service shells out to this in place of the ``pdfcropmargins`` console
script. Disabled, it installs no patch and calls ``crop()`` so output is
bit-for-bit the bare tool. Enabled, it monkeypatches
``main_pdfCropMargins.get_bounding_box_list`` (only for the one crop) with a
wrapper that runs the detector and tightens each page's box to exclude the band,
then lets the normal crop math and save run once.

Exit codes are structured so the service classifies without scraping tracebacks:
0 success, 2 content failure (suppress on identical bytes), 3 environmental
(retry, never suppress).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from .detector import (
    DEFAULT_PARAMS,
    DetectorParams,
    detect_bands,
    page_band_inner_dists,
    page_text_from_mupdf,
)

STRIP_FLAG = "--strip-header-footer"  # leading token the processor prepends

EXIT_SUCCESS = 0
EXIT_CONTENT = 2
EXIT_ENVIRONMENTAL = 3

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


def _tighten_top(
    f_bottom: float, f_top: float, b_top: float, cut: float, p_top: float, a_top: float
) -> float:
    """Invert pdfcropmargins' top expansion so the final top lands at ``cut``.

    Final top is ``f_top - (f_top - b_top)*(1 - p/100) - a_top``, so
    ``b_top = f_top - (f_top - cut - a_top)/(1 - p/100)``. Out of the page box
    (large retain, or p>=100) signals the caller to fall back to retain 0.
    """
    scale = 1.0 - p_top / 100.0
    if scale <= 0.0:
        raise _PreCompUnavailable
    b = f_top - (f_top - cut - a_top) / scale
    if not (f_bottom <= b <= f_top):
        raise _PreCompUnavailable
    return b


def _tighten_bottom(
    f_bottom: float, f_top: float, b_bottom: float, cut: float, p_bottom: float, a_bottom: float
) -> float:
    """Invert pdfcropmargins' bottom expansion so the final bottom lands at ``cut``.

    Final bottom is ``f_bottom + (b_bottom - f_bottom)*(1 - p/100) + a_bottom``, so
    ``b_bottom = f_bottom + (cut - f_bottom - a_bottom)/(1 - p/100)``. Out of the
    page box signals the caller to fall back to retain 0.
    """
    scale = 1.0 - p_bottom / 100.0
    if scale <= 0.0:
        raise _PreCompUnavailable
    b = f_bottom + (cut - f_bottom - a_bottom) / scale
    if not (f_bottom <= b <= f_top):
        raise _PreCompUnavailable
    return b


class _PreCompUnavailable(Exception):
    """Pre-compensation would push the injected edge out of range."""


def make_bbox_wrapper(
    original: Callable, params: DetectorParams = DEFAULT_PARAMS
) -> Callable:
    """Build the get_bounding_box_list wrapper that strips header/footer bands."""

    def wrapper(
        input_doc_fname,
        input_doc_mupdf_wrapper,
        full_page_box_list,
        set_of_page_nums_to_crop,
        argparse_args,
    ):
        bbox_list = original(
            input_doc_fname,
            input_doc_mupdf_wrapper,
            full_page_box_list,
            set_of_page_nums_to_crop,
            argparse_args,
        )

        document = input_doc_mupdf_wrapper.document
        num_pages = len(full_page_box_list)
        # Text comes from the handed wrapper doc (already un-rotated + pre-cropped),
        # using each page's current MediaBox height for the y-flip.
        pages = []
        for i in range(num_pages):
            page = document[i]
            mb = page.mediabox
            height = float(mb.y1 - mb.y0)
            width = float(mb.x1 - mb.x0)
            pages.append(page_text_from_mupdf(page, height, width))

        result = detect_bands(pages, params)
        if result.top is None and result.bottom is None:
            return bbox_list

        # Resolved per-margin retain/offset (after -p expands to the 4-tuple): [L,B,R,T].
        p4 = list(argparse_args.percentRetain4)
        a4 = list(argparse_args.absoluteOffset4)
        p_bottom, p_top = float(p4[1]), float(p4[3])
        a_bottom, a_top = float(a4[1]), float(a4[3])

        # pdfcropmargins rotates the retain/offset per page; our inversion uses the
        # unrotated values, exact only when they are symmetric or no page is rotated.
        # Otherwise abstain rather than mis-place the cut.
        asymmetric = len(set(p4)) > 1 or len(set(a4)) > 1
        if asymmetric and any(
            page.rotationAngle for page in input_doc_mupdf_wrapper.page_list
        ):
            return bbox_list

        # -u/-s collapse every page's delta to the smallest, so the cut can't be
        # pre-compensated per page; handled by a shared retain-0 cut below.
        collapse = bool(argparse_args.uniform or argparse_args.uniformOrderStat4)

        tol = params.y_cluster_tolerance  # cluster tolerance also matches per-page bands

        def page_exhibits(i: int) -> tuple[bool, bool]:
            """Whether page ``i`` itself shows the confirmed top/bottom band, so a
            title/figure/header-less page is never clipped to the document cut."""
            top_dist, bottom_dist = page_band_inner_dists(pages[i], params)
            has_top = (
                result.top is not None
                and top_dist is not None
                and abs(top_dist - result.top.inner_dist) <= tol
            )
            has_bottom = (
                result.bottom is not None
                and bottom_dist is not None
                and abs(bottom_dist - result.bottom.inner_dist) <= tol
            )
            return has_top, has_bottom

        # Cut for page ``i`` in its OWN post-precrop frame; distance-from-edge keeps
        # the conversion height-correct even on non-modal pages.
        def top_cut_for(i: int) -> float | None:
            if result.top is None:
                return None
            return full_page_box_list[i][3] - result.top.dist

        def bottom_cut_for(i: int) -> float | None:
            if result.bottom is None:
                return None
            return full_page_box_list[i][1] + result.bottom.dist

        # Exhibit flags + cuts computed once per cropped page; reused by every path.
        info = {
            i: (*page_exhibits(i), top_cut_for(i), bottom_cut_for(i))
            for i in range(num_pages)
            if i in set_of_page_nums_to_crop
        }
        new_bbox_list = [list(b) for b in bbox_list]

        if collapse:
            # One box wins (the min delta), so take an edge only if no non-exhibiting
            # page would lose real content; then inject the final edge at retain 0.
            do_top = result.top is not None
            do_bottom = result.bottom is not None
            for i, (ht, hb, tc, bc) in info.items():
                if do_top and not ht and bbox_list[i][3] > tc + tol:
                    do_top = False
                if do_bottom and not hb and bbox_list[i][1] < bc - tol:
                    do_bottom = False
            new_p4 = list(p4)
            if do_top:
                new_p4[3] = 0.0
            if do_bottom:
                new_p4[1] = 0.0
            argparse_args.percentRetain4 = new_p4
            for i, (ht, hb, tc, bc) in info.items():
                f = full_page_box_list[i]
                if do_top:
                    new_bbox_list[i][3] = min(new_bbox_list[i][3], tc + a_top)
                if do_bottom:
                    new_bbox_list[i][1] = max(new_bbox_list[i][1], bc - a_bottom)
                new_bbox_list[i][3] = min(new_bbox_list[i][3], f[3])  # keep in page
                new_bbox_list[i][1] = max(new_bbox_list[i][1], f[1])
            return new_bbox_list

        # Per-page path (default profile and -s, which crop independently): tighten
        # only band-exhibiting pages with retain pre-compensation, leaving every
        # other page as pdfcropmargins produced it. Page 0 (title) is never touched.
        # If pre-comp is out of range, fall back to retain 0 on that edge.
        fallback_top = False
        fallback_bottom = False
        for i, (has_top, has_bottom, tc, bc) in info.items():
            if i == 0:
                continue
            f = full_page_box_list[i]
            b = new_bbox_list[i]
            if has_top and tc is not None and tc < b[3]:
                try:
                    b[3] = _tighten_top(f[1], f[3], b[3], tc, p_top, a_top)
                except _PreCompUnavailable:
                    fallback_top = True
            if has_bottom and bc is not None and bc > b[1]:
                try:
                    b[1] = _tighten_bottom(f[1], f[3], b[1], bc, p_bottom, a_bottom)
                except _PreCompUnavailable:
                    fallback_bottom = True

        if fallback_top or fallback_bottom:
            new_p4 = list(p4)
            if fallback_top:
                new_p4[3] = 0.0
            if fallback_bottom:
                new_p4[1] = 0.0
            argparse_args.percentRetain4 = new_p4
            # Retain on the fallback edge is now 0, so re-inject the cut directly
            # (from the original whitespace box) on every exhibiting page.
            for i, (has_top, has_bottom, tc, bc) in info.items():
                if i == 0:
                    continue
                b = new_bbox_list[i]
                f = full_page_box_list[i]
                if fallback_top and has_top and tc is not None and tc < f[3]:
                    b[3] = min(bbox_list[i][3], tc + a_top)
                if fallback_bottom and has_bottom and bc is not None and bc > f[1]:
                    b[1] = max(bbox_list[i][1], bc - a_bottom)

        return new_bbox_list

    return wrapper


def run_crop(
    crop_argv: Sequence[str],
    *,
    strip: bool,
    params: DetectorParams = DEFAULT_PARAMS,
) -> int:
    """Run a single crop, returning a structured exit code. The monkeypatch is
    installed only when ``strip`` is true and always removed afterward."""
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
    if strip:
        original = main_mod.get_bounding_box_list
        main_mod.get_bounding_box_list = make_bbox_wrapper(original, params)
        installed = True

    try:
        _, exit_code, _, stderr_str = crop(argv_list=list(crop_argv), quiet=True)
    except SystemExit as exc:  # pragma: no cover - crop() traps this itself
        exit_code = exc.code if isinstance(exc.code, int) else 1
        stderr_str = str(exc)
    except Exception as exc:  # noqa: BLE001
        # A detector/PyMuPDF bug escaping crop() is environmental, not input-bound;
        # emit a marker (never the raw traceback) so it is retried, not suppressed.
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
    # The flag is only ever the leading token, so consuming it only there can
    # never swallow a pdfcropmargins option value that equals it (e.g. a password).
    if raw and raw[0] == STRIP_FLAG:
        return run_crop(raw[1:], strip=True)
    return run_crop(raw, strip=False)


if __name__ == "__main__":
    sys.exit(main())
