# Reader-fit cropping (plan)

Status: proposed, not yet implemented. This is the plan and decision record for the reader-fit feature. `docs/design.md` remains the authoritative system spec; when this feature lands, design.md gets a short section deferring here, mirroring how `docs/header-footer-strip.md` is referenced.

The project is pre-release: no backward compatibility with existing fingerprints, outputs, or config files is required, and defaults may change freely. Existing state and outputs are wiped on rollout, and a live drive `config.toml` still using removed keys will fail validation and fall back to built-in defaults with a `config.error.log` until edited.

## Problem

Sources vary in intended print size (letter/A4 academic papers, 6x9 in trade books, A5 and smaller books) while the reader screen is fixed. The reader scales each page to fit the screen. PDF user-space units are physical (1 pt = 1/72 in), so for a cropped page the on-device physical magnification is:

```
scale = min(screen_width / cropped_width, screen_height / cropped_height)
```

A maximal crop is right for letter-size papers (scale stays below 1, every point of margin helps) but wrong for small-trim books (scale balloons past 1 and the page looks oversized and mis-proportioned). `percent_retain` cannot express this: it is relative to whatever margin the tool found, not to the physical outcome on the device.

Separately, the current built-in default crops each page independently (no `-u`/`-s`), so page boxes differ across a document and the reader's zoom visibly jitters between pages. Consistent boxes should be the default, but per-page cropping stays available as a mode.

## Goals

1. Bound the on-device magnification at a configured `max_scale` using the configured reader screen size, with no per-document configuration. Because PDF coordinates are physical units, the document's intended print size enters this rule implicitly through its page geometry; no separate intended-size lookup is needed or used.
2. Consistent page geometry across each document by default: cropped pages share one output box, deviating per page only where content safety requires it. Per-page cropping remains available as a profile knob.
3. First-page artifacts (artifact-evaluation badges on papers, full-bleed book covers) must not widen the shared box for the rest of the document.
4. Content safety is absolute: reader-fit never clips ink that the whitespace crop (plus strip, when enabled) would have kept, on any page, including mixed-size and rotated pages.
5. Purely geometric: no font detection, no document classification, works identically on scanned/image-only PDFs.

Non-goals: helping small-trim books with unusually small type read larger than the cap (the sidecar can raise `fit_max_scale` per file); generating multiple output variants; talking to the device.

## Decision record

### Why a size floor, not classification or font detection

Document classification (paper vs book) is a coarse two-bucket proxy with bad failure modes on misclassification. Font-size detection is heuristic and fails on scans. The screen size plus a magnification cap gives a continuous, purely geometric rule: crop as tight as content allows, but never so tight that `scale` exceeds `max_scale`. Letter papers never hit the cap and get the full crop; small-trim books hit it and keep exactly enough margin. An earlier draft additionally read the document's declared TrimBox/MediaBox as an "intended size" input; that was dropped as dead spec. The scale formula never consumed it (physical units make intended size implicit), and it is unreadable anyway at the interception point: pdfcropmargins rewrites each page's MediaBox before our hook runs, which resets the other boxes (`set_box` in its `pymupdf_routines.py`; verified empirically that TrimBox reads back as the full page afterward).

### Why the shim owns the final box

The strip feature intercepts `get_bounding_box_list` (see `docs/header-footer-strip.md`) and pre-compensates pdfcropmargins' downstream `percent_retain` expansion per edge, falling back to zeroing `percentRetain4` when inversion is out of range. Reader-fit cannot use pre-compensation: the downstream expansion is per page relative to each page's own margins, so no single injected box survives it as a shared box, and a floor of exactly `screen / max_scale` would always be re-expanded past itself. Verified against the vendored tool: `calculate_crop_list` scales each margin delta by `(1 - pct/100)` and a wrapper-returned box comes back inflated by the retain on every side.

Therefore, when reader-fit is active the wrapper zeroes `argparse_args.percentRetain4` and injects final boxes directly. `percent_retain` keeps its meaning (safety margin against clipped descenders) but becomes a shim-side input: each page's tight box is expanded by the retain fraction of that page's own margins inside the wrapper, before aggregation. `absolute4` gets the same treatment: pdfcropmargins adds its offsets as a separate additive term after the retain scaling, so left un-neutralized a positive offset would crop inward past the shim's computed box (clipping ink, violating Goal 4) and break the floor; the wrapper applies it to the content box shim-side and zeroes `absoluteOffset4` alongside `percentRetain4`. The strip cuts compose in the same pass, before aggregation, so the previous strip pre-compensation/fallback dance is subsumed by one code path with a single owner of both fields. When both strip and reader-fit are disabled, the wrapper installs nothing and output is bit-for-bit the bare tool, as today.

The as-is injection guarantee was verified empirically (a wrapper-returned box publishes byte-exact with the retain zeroed) and depends on the profile schema staying clear of the pdfcropmargins options that post-process the crop list: `-u`/`-m*` (collapse), `-s`/`-ms` (same size), `--cropSafe`, `--setPageRatios`, `--centerText`, and the even/odd variants. None are in the schema after this change; adding any of them later requires revisiting step 9.

### Why not the native `-u`/`-s` flags for consistency

| Option | Verdict |
|---|---|
| Set `-u` + `-s` in the built-in profile | Rejected. Cannot express the floor, and the min-delta semantics let one wide page (badge, figure) loosen the whole document with no way to exempt it. |
| `-m`/`-mp` order-statistic variants | Rejected. They tolerate outlier pages only by silently clipping them, violating Goal 4. |
| Document-level box computed in the crop shim | Chosen. The wrapper sees every page's tight box and full page box before the crop math runs; the floor, uniformity, first-page exemption, and strip composition fall out of one aggregation. |

With document-scope cropping in the shim, the `uniform`/`same_size` profile keys and their `-u`/`-s` flags are removed from `FLAG_MAP` and `CropProfile` (no compatibility shims, per project convention). The shim's existing `collapse` branch for `-u`/`-mmmm` and its tests are deleted with them.

### Shared box with per-page escape, not a hard union

A hard union (max extent across all pages) fails two ways. On mixed-size documents it destroys content: pdfcropmargins computes margin deltas as absolute differences from each page's own box, so a union edge lying outside a smaller page's box crops that page inward (verified: an A5 page in a letter document lost roughly half its width to a letter-sized union). On stripped documents it resurrects the header: pages that do not exhibit the running band keep their full tight box, one such page pushes the union's top edge back above the cut, and the band area returns document-wide.

The chosen rule inverts the priority: the shared box is the default, and content safety is enforced per page. Aggregation runs over a modal-size cohort with stripped, capped votes (details in the behavior spec), and any page whose own content box is not contained by the shared box gets the shared box minimally expanded for that page alone. Conforming pages stay identical; outlier pages (badge page, full-bleed plate, figure-top page, off-modal insert) deviate individually and never lose ink.

### First-page exemption

Artifact-evaluation badges sit in the first page's margin of many papers; book covers are often full-bleed. Both are single-page artifacts that would widen the shared box for every page. By default the first page (page index 0; the knob is a no-op if page 0 is not selected for cropping) does not vote in the aggregation. If its content box fits inside the shared box anyway it simply uses the shared box (free consistency); otherwise it gets the per-page minimal expansion like any outlier. This accepts one zoom change at the most-noticed page turn as the price of a tight shared box for the rest of the document; `fit_exclude_first_page = false` restores voting for documents whose first page is ordinary.

### Reader screen dimensions and units

Config takes the screen size in inches, matching how device specs are published; internally 1 in = 72 pt exactly, so nothing is lost. No `ppi` key: ppi only matters when deriving inches from a pixel count, which is a spec-sheet exercise, not a config concern. Kindle Scribe Colorsoft panel: 11 in Kaleido 3, 1980x2640 px at 300 ppi, so 6.6 x 8.8 in. This is the built-in `device = "scribe-colorsoft"` preset and the default, since this deployment targets that device. The preset uses the full panel; the reader UI may consume some rows in some viewing modes, which is deliberately ignored (explicit dimensions are the escape hatch for that or for other devices).

Screen hardware lives in the server config: it describes the deployment (the NixOS module can set it), changes never, and does not belong to the "what to do to PDFs" surface that syncs through the drive. The behavioral knobs (`fit_*`) are crop profile keys so they flow through the existing three-layer merge, stay adjustable from any device via the drive `config.toml`, and get per-file sidecar overrides for free.

### `fit_max_scale` default

Default 1.15. A cap of 1.0 ("never larger than printed") would leave near-A5 books entirely uncropped on this panel (the panel is slightly larger than A5); 1.15 permits a modest, comfortable tightening while still refusing the blown-up look.

## Configuration

Server config (`server.toml`), new optional table; defaults shown:

```toml
[reader]
device = "scribe-colorsoft"   # preset; or explicit dimensions instead:
# screen_width_in  = 6.6
# screen_height_in = 8.8
```

Validation: explicit presence of the `device` key conflicts with explicit dimensions (the built-in default does not); explicit dimensions require both keys and must be positive; unknown keys rejected (hard failure, like the rest of the server config).

New crop profile keys (shim directives, not pdfcropmargins flags; validated and merged like the existing keys, defaults shown):

```toml
fit_reader             = true        # apply the size floor at all
fit_max_scale          = 1.15        # magnification cap (positive number)
fit_scope              = "document"  # "document" (shared box) or "page" (per-page boxes)
fit_exclude_first_page = true        # document scope: page 0 does not vote in aggregation
```

`uniform` and `same_size` are removed from the profile schema. The built-in profile becomes `percent_retain = 10` plus the fit defaults above.

## Behavior specification

One wrapper handles strip and reader-fit in a single ordered pass per crop invocation. Coordinate frame: all boxes below are in the absolute post-precrop coordinates of `full_page_box_list` and the wrapped `get_bounding_box_list` result, which share a frame per page (origins are not assumed zero). "Selected pages" means the `pages`-restricted crop set; unselected pages are untouched and never vote.

1. **Tight boxes.** Call the original `get_bounding_box_list` for per-page tight whitespace boxes.
2. **Strip cuts.** When strip is enabled, run the detector as today to get the document top/bottom cut distances and per-page exhibit flags. The flags are used only in step 3; unlike the current strip implementation, non-exhibiting pages are not otherwise abstained per page, because vote capping (step 5) plus containment expansion (step 8) protect their ink through a different route. Semantic change, accepted: a non-flagged page whose ink lies inside the band zone deviates individually rather than loosening the whole document.
3. **Retain padding.** Expand each selected page's tight box by `percent_retain` (or `percent_retain4`) of that page's own margins, then apply `absolute4`, the same per-edge arithmetic pdfcropmargins would apply downstream. As downstream does (`mod_box_for_rotation`), permute the 4-tuple values by the page's own rotation before applying them (`[L,B,R,T]` -> `[R,T,L,B]` at 180, `[B,R,T,L]` at 90), so asymmetric quads land on the displayed edges; this matters in-cohort for 180-rotated pages and replaces the old strip abstention on asymmetric-plus-rotated documents. On exhibiting pages, apply the strip cut to the padded box's top/bottom edge first, so the retained margin is real margin, never the dead band. The result is the page's **content box**: the box that page must at minimum receive. Steps below never shrink any page's final box inside its content box (Goal 4).
4. **Cohort.** Document scope only: the voting cohort is the selected pages whose page-box dimensions match the document's modal page size (within a small tolerance) and whose rotation is 0 or 180, minus page 0 when `fit_exclude_first_page` is true. Off-modal and 90/270-rotated pages are handled per-page in step 7. If there is no unique modal size, or the cohort has fewer than 2 pages, the whole document degrades to page scope.
5. **Aggregation.** The shared box is the union (max extent per edge) of the cohort's content boxes, except that when a strip cut is confirmed, the corresponding top/bottom edge of each vote is capped at the document cut. A non-exhibiting page with real ink in the band zone therefore cannot re-widen the shared box; its own ink is protected by step 8 instead.
6. **Floor.** With `fit_reader` true and `min(screen_w / w, screen_h / h) > fit_max_scale` (screen dimensions are the configured inches times 72), grow the box in the binding dimension to exactly `screen / fit_max_scale`, in closed form (no iteration), symmetrically about its center. The non-binding dimension stays at its aggregated extent; growth is symmetric even under an asymmetric `percent_retain4` (the asymmetry the user asked for lives in the content box from step 3; floor growth is device-driven whitespace and does not preserve the ratio). If both ratios exceed the cap equally, grow the width (deterministic tie-break, either choice satisfies the constraint). If the box already satisfies the cap, nothing grows.
7. **Page scope.** For `fit_scope = "page"`, and for off-modal or 90/270-rotated pages in document scope, apply step 6 to the page's own content box instead. For 90/270-rotated pages swap the floor's axes, because pdfcropmargins computes in unrotated coordinates but restores the rotation on save, so the displayed page is transposed.
8. **Per-page placement.** For each selected page: start from the shared box (or the page's own floored box in step 7 cases); minimally expand it to contain the page's content box if it does not already; translate it to lie inside the page's full page box without changing its size; only if the page box is smaller than the box in a dimension, shrink to the page box (no synthetic whitespace beyond the page edge; `scale > fit_max_scale` on genuinely small pages is accepted). Translation before shrinking is what keeps the floor guarantee on pages whose content sits near one margin.
9. **Injection.** Zero `argparse_args.percentRetain4` and `argparse_args.absoluteOffset4` (both were already applied in step 3) and return the final per-page boxes. Downstream, pdfcropmargins applies them as-is (precondition: the schema exclusions listed in the decision record).

Failure classification: errors raised by this path are environmental (retry, never suppress), matching the existing shim rule that our own bugs must not suppress an input.

## Fingerprint

The fingerprint token folds exactly the resolved parameters that can change the output: `fit_scope` always (scope changes geometry even with the floor off); `fit_exclude_first_page` when scope is document; screen dimensions and `fit_max_scale` only when `fit_reader` is true. This keeps the key-moves-iff-output-changes invariant from design.md: bumping `fit_max_scale` does not re-crop files with `fit_reader = false`. Old fingerprints from before this feature are invalidated wholesale, which is fine pre-release (state is wiped on rollout).

## Acceptance criteria

Colorsoft preset: floor at `fit_max_scale = 1.15` is 6.6x72/1.15 = 413.2 pt by 8.8x72/1.15 = 551.0 pt (550.96 exactly).

- Letter paper (612x792 pt), content boxes around 468x666 pt: `min(475.2/468, 633.6/666) = 0.95 <= 1.15`, floor does not bind; every cohort page gets the identical union box.
- Paper with an artifact badge on page 1: shared box computed from pages 2+ is as tight as a badge-free paper; page 1 gets the shared box minimally expanded to keep the badge, deviating alone. With `fit_exclude_first_page = false`, the badge widens the shared box for all pages (documented behavior).
- A5 book (420x595 pt), content boxes around 320x500 pt: height binds (`633.6/500 = 1.27 > 1.15`), width does not (after height grows to 551.0, `min(475.2/320, 633.6/551.0) = 1.15`). Output boxes are 320 x 551.0 pt: scale exactly 1.15, no whitespace bought in the non-binding dimension.
- Mixed-size document (A5 insert in a letter document): the insert is off-modal, gets per-page treatment, and none of its ink is clipped; cohort pages are unaffected by it.
- Landscape page in a portrait book: off-cohort, per-page floor with swapped axes, no ink clipped.
- Strip + document scope, document with a running header plus one figure-top page: the shared box's top edge sits at the strip cut; the figure page alone deviates upward to keep its figure; no page re-acquires the band.
- Content hard against one margin on a cohort page: the shared box is translated, not shrunk; the floor holds.
- Page physically smaller than the floor: box clamps to the page box, never beyond the page edge.
- Single-page PDF with default knobs: degrades to page scope (empty cohort after exemption), floor still applies.
- `fit_scope = "page"`: per-page content boxes, each floored independently.
- Sidecar `fit_reader = false` on one file: no floor for that file; document scope still yields one shared box; changing `fit_max_scale` does not change that file's fingerprint.
- Sidecar with `absolute4 = [0,0,12,0]` plus reader-fit: the offset tightens the content box shim-side; the published box never crops past it and the floor still holds. Negative offsets widen the content box before aggregation.
- Both features off: output bytes identical to the bare tool.
- Profiles containing `uniform` or `same_size` are rejected as unknown keys.

## Task breakdown

1. Profile layer: remove `uniform`/`same_size` from `FLAG_MAP`/`CropProfile`; add the four `fit_*` keys (needs a string-enum validation kind for `fit_scope`); update `tests/test_profile.py` and the `tests/test_config.py` cases that use `uniform` as the valid/invalid exemplar.
2. Server config: `[reader]` table with preset table and validation (`_ALLOWED_SERVER_KEYS` gains `"reader"`); the service logs the resolved screen size at startup so a deployment that forgot the table on a different device is visible; NixOS module option; note that `process_pdf` grows a reader-dimensions parameter (`DriveConfigResult` is unchanged), so the `process_fn` seam and its test fakes widen.
3. Crop shim: implement the nine-step pipeline as the single wrapper; delete the `collapse` branch and the `_tighten_*` pre-compensation path plus their tests (subsumed by shim-side retain and direct injection); directive plumbing for the resolved reader parameters (leading-token style, like `--strip-header-footer`).
4. Fingerprint: fold the effective fit parameters per the rule above.
5. Docs: design.md default-profile and sidecar sections updated (per-page rationale replaced by scope knob deferring here); README gains `[reader]` and `fit_*` docs and drops `uniform`/`same_size`.
6. Tests: pure-function units for retain padding, cohort selection, aggregation with strip caps, binding-dimension floor, and placement (expand/translate/shrink ordering); integration through the fake-runner seams; the acceptance-criteria scenarios above, including mixed-size, rotation, and single-page. Two emphases from review: step 3 does four things in order (retain, `absolute4`, rotation permutation, strip cut) and an ordering mistake is silent, so its units must assert the ordering, not just the arithmetic; and the acceptance criteria are geometric assertions on published box dimensions, so test them at the shim level against the real pdfcropmargins library (the level where this review's bugs surfaced), not only through the fake-runner seam.

## Open questions

- Cohort tolerance value for "matches the modal page size" (a few pt, to absorb scanner jitter) is an implementation detail to tune against real scans.
- Whether the preset should model the Kindle PDF viewer's effective viewport rather than the raw panel; deferred, explicit dimensions are the escape hatch.
