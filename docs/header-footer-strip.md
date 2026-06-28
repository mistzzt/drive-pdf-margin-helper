# Plan: Strip running headers and footers

Status: proposed (reviewed). This plan defines what to build and why. It does not
prescribe the implementation. Read `docs/design.md` first; this feature extends the
crop step and the fingerprint, and it must preserve every invariant in that
document and in `CLAUDE.md`. The pdfcropmargins integration claims below were
verified against the vendored source (version 2.2.1, pinned in
`nix/pdfcropmargins.nix`); file:line citations are to that source.

## Goal

`pdfcropmargins` removes whitespace margins only. On academic papers it keeps the
running header (conference/journal line, running title, author list) and footer
(page number, publication line) because those are real ink inside the content
bounding box, not whitespace. On a Kindle Scribe that band wastes vertical space
and adds noise on every page.

Add an opt-in capability that detects a document's running header and footer and
trims them as part of the normal crop, losslessly (box rewrite only, no
re-render), per page, defaulting to off. It must trim nothing when it is not
confident, so that papers without a running header, or pages with figures at the
top edge, are never damaged.

## Why this approach (decision record)

### Detection signal

Three candidate signals were prototyped against eight real papers (ACM two-column
conference, ACM journal verso/recto, IEEE conference, anonymized double-spaced
submissions with margin line numbers):

| Signal | Verdict |
|---|---|
| Fixed band height (hardcode N points off the top/bottom) | Rejected. Templates, page sizes, single vs double spacing, and presence/absence of a header all differ. No constant generalizes. |
| Per-page geometric gap (header is text followed by an abnormally large whitespace gap before the body) | Rejected as a primary signal. In dense two-column layouts the header-to-body gap is barely larger than body leading; in double-spaced submissions body gaps are as large as the header gap. The per-page gap fired on only a minority of pages. |
| Cross-page positional recurrence (the isolated edge line that recurs at the same y-position across most pages is the header/footer) | Chosen. Detected the band on 100% of pages on all seven papers that have one, with y-position stable to about 0.1pt, and correctly abstained on the IEEE paper whose interior pages have figures at the top and no running header. |

Key properties of the chosen signal that the implementation must keep:

- **Text-agnostic.** The decision is on geometry (recurring y of an isolated edge
  line), not on text content, so verso/recto alternation and changing page numbers
  do not defeat it. This was the original concern that ruled out text-identity
  matching.
- **Abstains by default.** No stable recurring band means no crop. A figure at the
  top of one page does not recur at a fixed y, so it is never mistaken for a
  header. This is the single most important safety property.
- **Conservative on partial evidence.** If only the header recurs and the footer
  does not, only the header is trimmed; the other edge is left untouched.

The per-page gap is retained but demoted to a per-page *feature* (used to decide
whether a candidate edge line is isolated from the body and to place the exact cut
inside the whitespace), not as the cross-page decision.

### Crop architecture: one crop, via a wrapped bounding-box detector

The header/footer constraint is composed with the whitespace crop at the
**bounding-box** level, producing a single crop. `pdfcropmargins` cleanly
separates detection from application: `get_bounding_box_list(...)`
(`calculate_bounding_boxes.py:73`) computes per-page tight whitespace boxes, and
the rest of `process_pdf_file` (`main_pdfCropMargins.py:955`) turns those into the
final crop and writes the output.

The injection mechanism is a monkeypatch of that one function, driven through the
**public** `crop()` API, not a call into private internals:

- `get_bounding_box_list` is imported into the main module's namespace
  (`main_pdfCropMargins.py:109`) and called by bare name (`:1052`), so replacing
  `main_pdfCropMargins.get_bounding_box_list` with a wrapper takes effect. The
  non-GUI `crop()` -> `main_crop` -> `process_pdf_file` path always passes
  `bounding_box_list=None` (`:1232-1234`), so the wrapper always runs; the only
  other importer is the GUI, which is not on our path.
- The wrapper calls the original, runs our detector, tightens the returned per-page
  boxes (top to the header cut, bottom to the footer cut; left/right untouched),
  and returns them. They then flow through the normal crop math and the single
  save.

This is one crop, one metadata write, no second box rewrite, and it reuses the
tested whitespace detection and crop math verbatim.

| Decision | Choice | Rationale |
|---|---|---|
| Composition point | Tighten the per-page whitespace boxes before the crop math | Single crop; reuses `pdfcropmargins` detection and crop math; the cut is computed in the same axes the crop math uses. |
| Injection seam | Monkeypatch `main_pdfCropMargins.get_bounding_box_list`, invoke public `crop()` | The public `crop()` cannot take a box list and the box-list parameter on `process_pdf_file` is only reached by the GUI; reconstructing the ~56-attribute global `args` to call it directly is heavy coupling. Patching one symbol and using `crop()` (which builds `args` itself) is the minimal seam. Couples us to one function name + its signature + the public entry, all pinned via nix. |
| Rejected: two-pass harvest/re-inject | Not used | Running the crop once to harvest boxes then again to apply is broken: pass one writes restore metadata / a "Cropped by pdfCropMargins" Producer string that is not reliably cleared (`main_pdfCropMargins.py:52-55`, `check_and_set_crop_metadata` `:981`), so pass two registers as already-cropped; harvested boxes are also post-`absolutePreCrop`, so mixing args between passes silently misaligns. |
| Where it runs | A crop shim of ours that installs the patch and calls `crop()`, invoked by the service as a subprocess | Keeps the existing process isolation, timeout, and OOM/`BinaryNotFound` handling the daemon relies on; a native (MuPDF/Ghostscript) crash or hang stays contained. Same shape as today, where the `pdfcropmargins` console script is itself `crop()` in a subprocess. |
| Dependency | Reuse PyMuPDF, already in the `pdfcropmargins` closure | `pdfcropmargins` parses/renders/saves via MuPDF (`pymupdf_routines.py`), so PyMuPDF is already pulled in; no new heavy dependency. |

## Detection algorithm (specification)

The detector returns, independently for the top and the bottom edge, either a
confirmed band cut position or nothing. It is pure logic over text-line geometry,
so it is deterministic and testable.

### Hard constraint: read the doc the wrapper is handed

By the time the wrapper runs, the document passed to it
(`input_doc_mupdf_wrapper`, `main_pdfCropMargins.py:1052`) has already been
mutated by `get_full_page_box_list_assigning_media_and_crop`
(`:1029`, `pymupdf_routines.py:442-552`): every page's rotation is set to 0
(`:472`) and its MediaBox/CropBox are overwritten to the full-page box with any
`absolutePreCrop` applied (`:518-519,537`). The detector **must** extract text
from that handed wrapper document, not from a fresh open of the original bytes.
Consequences, all of which make the design simpler and correct:

- **Rotation is handled for free.** Pages are already un-rotated, so text is in the
  visual orientation the crop math uses. No separate rotation normalization is
  needed (and a fresh open would reintroduce the problem).
- **One consistent frame.** The returned `bbox_list` is in PDF bottom-left coords
  corrected for nonzero origin (`calculate_bounding_boxes.py:104`) and matches
  `full_page_box_list`; reading the same wrapper doc keeps the detector's text
  coords, the boxes, and the page height in the same post-precrop frame.

### Coordinate convention

PyMuPDF text bounding boxes are top-left origin, y increasing downward; the boxes
the wrapper tightens are PDF bottom-left, y increasing upward. The detector must
convert its cut before tightening: `y_pdf = mediabox_height - y_mupdf`, using the
wrapper page's **current** (post-precrop) MediaBox height, not the original page
height. A fixture must assert the cut lands on the intended edge.

### Per-page candidate extraction

Merge text lines that vertically overlap into rows (y-overlap only, so a two-column
body yields one row per line, and a short isolated left-margin line number does not
fuse into a header). For each page, identify at most one top candidate and one
bottom candidate:

- **Top candidate:** the topmost row, only if it begins within the top zone of the
  page. Record its inner edge and its isolation gap (distance to the next row).
- **Bottom candidate:** the bottommost row, only if it ends within the bottom zone.
  Record its inner edge and its isolation gap (distance to the previous row).

A candidate counts toward confirmation only if its isolation gap is at least a
minimum separation and the band is short (bounded rows), so a multi-line footnote
block is not mistaken for a footer.

### Cross-page band confirmation

Independently for top and bottom:

1. Collect candidate inner-edge positions across all pages except the first (the
   title page differs and must not vote). Restrict to the document's modal page
   size and work in distance-from-edge, so differing or outlier page sizes do not
   pollute the cluster.
2. Cluster with a small tolerance; take the densest cluster.
3. Confirm only if the cluster covers at least a minimum fraction of pages **and**
   at least a minimum absolute number of pages (so a 2-page document cannot
   self-confirm with a single voter). Otherwise return nothing for that edge.
4. The confirmed cut is the robust center (median) of the cluster, nudged into the
   whitespace gap by the minimum separation so it never clips a descender or the
   page-number glyphs.

### Per-page application and the percentRetain interaction

`pdfcropmargins` adds back a percentage of each margin around the supplied box
(`calculate_crop_list`, `main_pdfCropMargins.py:362-378,516-517`), so a naively
tightened edge would be re-expanded back over the band. The wrapper compensates
per page, using the `full_page_box_list` it receives: for a matching edge, return
the tightened coordinate that, after re-expansion, lands exactly on the cut
(invert `final = fbox - (fbox - bbox)*(1 - p/100)`). Use the resolved
`percentRetain4` per-margin value (after `-p` expands to the 4-tuple,
`:738-739`), not the scalar. This keeps the user's retain on left/right and on
non-matching pages, and leaves the title page and header-less pages exactly as
`pdfcropmargins` produced them. Fallback if pre-compensation would push the edge
out of range (or `p` is 100): set `percentRetain4` top/bottom to 0 for the run and
inject the final edges directly, accepting that strip-enabled then makes top/bottom
tight on all pages. Either path is a single crop.

One caller-side caveat: a per-margin `absolute4` offset (existing sidecar schema,
`design.md:153`) is added to the delta after the percentRetain expansion
(`:376`), so it shifts the landed edge. Its default is none (a no-op for the
default profile), and `cropSafe`/`percentText` are off by default and not in the
schema, so neither interferes. But `absolute4` on a stripped (top/bottom) edge
would move the cut: fold the stripped edge's `absolute4` term into the inversion
(it is a simple additive offset), or document strip + top/bottom `absolute4` as
best-effort. A fixture should cover strip combined with a top/bottom `absolute4`.

### Constraint: uniform / same-size crop

With `-u`/`-s`, the crop math collapses to a document-wide box or min/max deltas
across pages **before** per-page boxes are formed (`:270-300,467-468`), which
flattens both per-page cuts and the per-page pre-compensation. Because the band is
at a consistent y by construction, the resolution is: when `-u`/`-s` are set,
compute a single document-wide cut and tighten uniformly. A fixture must cover
strip combined with `-u`.

### Tunable parameters (data)

v1 exposes only the on/off switch in user config; the rest are fixed, documented
constants chosen from the validation run. They are part of the algorithm's
contract and feed the fingerprint, so changing any of them must re-crop.

| Parameter | Role | Starting value |
|---|---|---|
| top zone fraction | a top candidate must start within this fraction of page height | 0.18 |
| bottom zone fraction | a bottom candidate must end below this fraction | 0.84 |
| y cluster tolerance | how close inner edges must be to share a band | 6 pt |
| min isolation gap | min whitespace separating an edge line from the body | 4 pt |
| coverage fraction | min fraction of (non-first) pages that must show the band | 0.40 |
| min voter count | min absolute number of pages that must show the band | 3 |
| max band rows | upper bound on band height, guards against footnotes | 3 |

## Integration into the pipeline

Today `process_pdf` shells out to the `pdfcropmargins` binary through the
injectable `runner` seam. This feature replaces that target with our crop shim,
invoked the same way (subprocess, timeout, runner seam, version probing unchanged).
The shim installs the monkeypatch **only when stripping is enabled** (when
disabled it is a pass-through / no-op) and calls `crop()` with the effective
profile's argv. The service side is otherwise unchanged: same temp path, same
atomic publish, same publish-then-record ordering, same failure classification. A
strip run that finds no band, or a PDF with no text layer, yields exactly the
whitespace-only crop.

### Failure handling

- **No text layer (scanned/image PDF), or no confirmed band:** not a failure; the
  output is the whitespace-only crop.
- **Detector or library error:** the shim emits a structured exit code that
  distinguishes content from environmental failure rather than leaking a raw
  traceback into the existing `_CONTENT_STDERR_PATTERNS` substring matcher
  (`processor.py:89-104`), so a transient PyMuPDF traceback containing a word like
  "bounding box" cannot become a permanent content suppression. Keep the subprocess
  boundary so a native crash or hang is contained and surfaces as
  timeout/OOM/nonzero exit.

## Configuration surface

One new crop-profile key, following the documented precedence and reload rules
(built-in default < drive `config.toml [crop]` < per-file `.pdf.toml` sidecar):

```toml
[crop]
strip_header_footer = true   # default false
```

It is the first profile key that is not a `pdfcropmargins` CLI flag; it is a
directive to our shim. The profile system assumes every key maps through
`FLAG_MAP` to an argv flag (`profile.py:22-35,144-161`), so the model must
distinguish flag keys from shim-directive keys while keeping one
validate/coerce/merge path. The new key must also be accepted by the two callers
of `validate_and_coerce`: `load_drive_config` (`config.py:147-156`) and the
sidecar parse path.

## Fingerprint integration

The dedup key must move when, and only when, the produced output would change.

- Do **not** add PyMuPDF to `tool_version`. The crop runs through
  `pdfcropmargins`, whose version (pinned via nix) already implies its PyMuPDF, so
  the existing toolchain token covers the crop path.
- When stripping is **disabled**, the shim installs no patch and calls `crop()`
  with argv identical to today's direct invocation. Both go through the same
  `crop()` code (today's `pdfcropmargins` console script *is* `crop()`), so the
  output is at parity with today's tool output (not identical to the source bytes:
  `pdfcropmargins` always rewrites the Producer string and restore metadata, today
  and tomorrow alike, `pymupdf_routines.py:605,694`). The fingerprint keys on input
  bytes + argv + `tool_version` (`fingerprint.py:65-84`), none of which change, and
  the skip check does not depend on output bytes (`processor.py:270-272`), so
  disabled files keep their current key and do not re-crop on rollout. This relies
  on the disabled path being today's path exactly; the patch must be conditional /
  no-op when disabled.
- When stripping is **enabled**, fold a `DETECTOR_VERSION` plus the strip
  parameters (switch and the constants) into the key. The strip directive folds in
  separately, since the existing `profile_token` is argv-only
  (`processor.py:265`). A change to the detector logic or constants bumps
  `DETECTOR_VERSION` and re-crops affected files.

Keep the command-matches-fingerprint invariant: resolve the effective profile
once, fingerprint what the shim will do (its argv plus the resolved strip
directive/version), and drive the shim from that same resolution.

## Dependency and packaging

- No new heavy dependency: reuse PyMuPDF from the `pdfcropmargins` closure. Declare
  it explicitly via `uv` so the import is intentional, and ensure the Nix package
  and dev shell expose it to the shim.
- The NixOS module needs no new system tool; Ghostscript and the crop toolchain are
  unchanged.

## Task breakdown

1. **Profile model:** allow a non-flag crop key (`strip_header_footer`) alongside
   the flag keys; one validate/coerce/merge path; `profile_to_argv` still emits
   only `pdfcropmargins` flags; accept the key in `load_drive_config` and the
   sidecar path.
2. **Detector module:** pure-logic header/footer band detection per the spec
   (reads the handed wrapper doc, modal-size + min-voter guards, coordinate
   conversion), importable and unit-tested directly.
3. **Crop shim:** installs the conditional monkeypatch and calls public `crop()`;
   wrapper tightens per-page (or document-wide under `-u`/`-s`) boxes with retain
   pre-compensation; emits structured exit codes.
4. **Service wiring:** point the crop step at the shim through the existing runner
   seam; keep timeout, OOM, `BinaryNotFound`, atomic publish, and classification.
5. **Fingerprint extension:** fold `DETECTOR_VERSION` + strip params into the key
   only when enabled; leave `tool_version` and the disabled-path fingerprint as
   today.
6. **Config + docs:** document the new key in `docs/design.md` (crop profile and
   sidecar schema) and `README.md`; default off.
7. **Packaging:** declare PyMuPDF in `uv`; expose it in the Nix package/dev shell;
   keep `nix build` and `nix flake check` green.

## Acceptance criteria and verification

The detector is exercised with small synthetic PDFs built in-process (text at known
positions) for exact, deterministic expectations. Service-level tests keep using
the runner seam (cloud-free, no real binary); shim-level tests run against the
vendored `pdfcropmargins`/PyMuPDF.

- **Detects a recurring band:** a synthetic multi-page doc with fixed-position
  header and footer yields confirmed top/bottom cuts at the expected y, and the
  output box is tightened on the body pages by the expected amount.
- **Text-agnostic / alternation:** header text alternating per parity and an
  incrementing footer page number is still fully detected.
- **Abstains:** no running header, and a tall figure at the top of one interior
  page, produce no top crop.
- **Title page and header-less pages preserved.**
- **Too few voters:** a 2-page doc does not self-confirm (min voter guard).
- **No text layer:** image-only PDF yields the whitespace-only crop.
- **Footnote guard:** a multi-line footnote block is not taken as a footer.
- **Retain pre-compensation:** the published crop lands exactly at the cut and does
  not re-include the band; left/right retain matches the no-strip case.
- **Uniform crop:** strip combined with `-u`/`-s` produces a single document-wide
  cut, not a flattened/under-crop.
- **Coordinate edge:** a fixture asserts the cut lands on the correct (header vs
  footer) edge, guarding the y-flip.
- **Lossless:** no re-render (content streams unchanged); the crop only shrinks the
  visible box.
- **Parity when disabled:** with the feature off, the shim output is identical to
  today's direct `pdfcropmargins` call for the same argv (parity with the current
  tool, not with the source bytes), and the fingerprint is unchanged.
- **Fingerprint behavior:** toggling the switch or bumping `DETECTOR_VERSION`
  re-crops affected files; a comment-only config edit re-crops nothing; a disabled
  file does not re-crop on a PyMuPDF/pdfcropmargins-internal bump beyond what the
  existing toolchain token already implies.
- **Failure classification:** a forced shim error on a valid input is environmental
  (retried), not a permanent content suppression; a native crash is contained by
  the subprocess boundary.
- **Invariants intact:** atomic publish, publish-then-record ordering, sole-writer
  of `processed/`/`failed/`, gated reverse-GC unchanged.
- **Packaging:** `uv run pytest`, `nix build .#scribe-crop`, `nix flake check` pass.

## Decision boundaries for the implementer

- Proceed without asking on: the internal structure of the detector and profile
  refactor, the exact retain pre-compensation vs `percentRetain4`-zero fallback,
  test fixtures, and the documented constant values (start from the table; adjust
  if a fixture proves one wrong).
- Stop and ask before: exposing any detection constant as user config beyond the
  on/off switch; changing the default to on; reading text from a fresh open of the
  original instead of the handed wrapper doc; re-rendering content or altering
  MediaBox semantics beyond what `pdfcropmargins` already does; removing the
  subprocess boundary; or changing the fingerprint so disabled files re-crop.

## Open questions / future

- Whether to expose a sensitivity knob (coverage fraction) per file if a real
  document needs a looser/stricter threshold. Deferred.
- Whether to also strip side margins beyond whitespace (e.g. submission line
  numbers in the left margin). Out of scope; top/bottom only.
- Whether to default the feature on once proven safe on a larger corpus. Deferred.
